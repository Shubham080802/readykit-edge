"""The watch loop, and the debounce that makes it usable.

Two questions decide whether this works in a room full of people:

  - does a deliberate removal close the latch quickly?
  - does a hand passing over the tray leave it alone?

Getting one without the other is easy. The tests below pin both, and the
asymmetry between opening and closing that they depend on.
"""

from __future__ import annotations

import pytest

from readykit.bridge import LoopbackLink, VirtualActuatorNode
from readykit.bridge.loopback import LatchState
from readykit.capture import ScriptedSource
from readykit.domain import Manifest, RequiredItem, Verdict
from readykit.engine import InspectionEngine
from readykit.inference.simulated import SimulatedEngine
from readykit.sentinel import (
    Action,
    Sentinel,
    SentinelConfig,
    SentinelPolicy,
    SentinelState,
)

KIT = Manifest(
    manifest_id="sentinel",
    name="Sentinel Kit",
    items=(
        RequiredItem(key="shears", label="Trauma Shears"),
        RequiredItem(key="gauze", label="Hemostatic Gauze"),
    ),
    hold_seconds=5.0,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def policy(clock: FakeClock) -> SentinelPolicy:
    return SentinelPolicy(
        config=SentinelConfig(open_after=2, close_after=2, hold_seconds=5.0),
        clock=clock,
    )


def feed(policy: SentinelPolicy, *verdicts: Verdict) -> list[Action]:
    return [policy.decide(v).action for v in verdicts]


class TestSlowToOpen:
    def test_one_pass_is_not_enough(self, policy: SentinelPolicy) -> None:
        """A single lucky frame must never release a physical lock."""
        assert policy.decide(Verdict.PASS).action is Action.NONE
        assert policy.state is SentinelState.WATCHING

    def test_the_required_streak_opens_it(self, policy: SentinelPolicy) -> None:
        actions = feed(policy, Verdict.PASS, Verdict.PASS)
        assert actions == [Action.NONE, Action.RELEASE]
        assert policy.state is SentinelState.RELEASED

    def test_a_broken_streak_starts_over(self, policy: SentinelPolicy) -> None:
        """The kit has to be good for the whole streak, not good on average."""
        actions = feed(
            policy, Verdict.PASS, Verdict.INDETERMINATE, Verdict.PASS
        )
        assert Action.RELEASE not in actions
        assert policy.state is SentinelState.WATCHING

    def test_a_longer_streak_can_be_demanded(self, clock: FakeClock) -> None:
        strict = SentinelPolicy(
            config=SentinelConfig(open_after=4, close_after=2), clock=clock
        )
        assert feed(strict, *([Verdict.PASS] * 3)) == [Action.NONE] * 3
        assert strict.decide(Verdict.PASS).action is Action.RELEASE


class TestQuickToClose:
    def open_it(self, policy: SentinelPolicy) -> None:
        feed(policy, Verdict.PASS, Verdict.PASS)
        assert policy.state is SentinelState.RELEASED

    def test_a_sustained_failure_closes_it(self, policy: SentinelPolicy) -> None:
        """Someone lifted a required item out while the door was open."""
        self.open_it(policy)
        actions = feed(policy, Verdict.FAIL, Verdict.FAIL)
        assert actions == [Action.NONE, Action.REJECT]
        assert policy.state is SentinelState.WATCHING

    def test_closing_is_quicker_than_opening(self, clock: FakeClock) -> None:
        """The asymmetry is the design. Granting access is conservative;
        retracting it is not."""
        config = SentinelConfig(open_after=4, close_after=2)
        assert config.close_after < config.open_after

    def test_one_bad_frame_does_not_close_it(self, policy: SentinelPolicy) -> None:
        self.open_it(policy)
        assert policy.decide(Verdict.FAIL).action is Action.NONE
        assert policy.state is SentinelState.RELEASED

    def test_a_recovered_frame_resets_the_streak(
        self, policy: SentinelPolicy
    ) -> None:
        """Fail, recover, fail must not accumulate into a close."""
        self.open_it(policy)
        actions = feed(policy, Verdict.FAIL, Verdict.PASS, Verdict.FAIL)
        assert Action.REJECT not in actions
        assert policy.state is SentinelState.RELEASED


class TestReachingInDoesNotSlamTheDoor:
    """The cabinet is meant to be used while it is open."""

    def open_it(self, policy: SentinelPolicy) -> None:
        feed(policy, Verdict.PASS, Verdict.PASS)

    def test_occlusion_while_open_is_not_a_reason_to_close(
        self, policy: SentinelPolicy
    ) -> None:
        """A hand over the tray reads as INDETERMINATE. Closing on that would
        slam the door every time somebody used the thing properly."""
        self.open_it(policy)
        actions = feed(
            policy, *([Verdict.INDETERMINATE] * 6)
        )
        assert set(actions) == {Action.NONE}
        assert policy.state is SentinelState.RELEASED

    def test_occlusion_is_still_counted_for_the_operator(
        self, policy: SentinelPolicy
    ) -> None:
        """Not acted on, but not hidden either - the camera was struggling and
        somebody should be able to see that."""
        self.open_it(policy)
        feed(policy, Verdict.INDETERMINATE, Verdict.INDETERMINATE)
        assert policy.occluded_while_open == 2

    def test_occlusion_does_not_bank_progress_towards_closing(
        self, policy: SentinelPolicy
    ) -> None:
        """One real failure after a run of obscured frames must still need the
        full close streak."""
        self.open_it(policy)
        feed(policy, Verdict.FAIL, Verdict.INDETERMINATE)
        assert policy.decide(Verdict.FAIL).action is Action.NONE

    def test_an_obscured_kit_still_cannot_open_one(
        self, policy: SentinelPolicy
    ) -> None:
        """The permissive rule applies only while already released. Opening on
        doubt stays forbidden."""
        assert feed(policy, *([Verdict.INDETERMINATE] * 5)) == [Action.HOLD] * 5
        assert policy.state is SentinelState.WATCHING


class TestTheHoldExpires:
    def test_the_policy_follows_the_firmware_back_to_watching(
        self, policy: SentinelPolicy, clock: FakeClock
    ) -> None:
        """The firmware re-engages on its own timer. Believing the latch is
        still open after that would mean missing the next release."""
        feed(policy, Verdict.PASS, Verdict.PASS)
        assert policy.state.value == "released"

        clock.advance(5.1)
        policy.decide(Verdict.PASS)
        assert policy.state is SentinelState.WATCHING

    def test_it_can_open_again_after_the_hold(
        self, policy: SentinelPolicy, clock: FakeClock
    ) -> None:
        feed(policy, Verdict.PASS, Verdict.PASS)
        clock.advance(5.1)
        assert feed(policy, Verdict.PASS, Verdict.PASS)[-1] is Action.RELEASE


class TestWhileWatching:
    def test_a_failing_kit_is_rejected_immediately(
        self, policy: SentinelPolicy
    ) -> None:
        """No debounce needed here - the latch is already shut, so acting
        costs nothing and the operator gets told at once."""
        assert policy.decide(Verdict.FAIL).action is Action.REJECT

    def test_an_unreadable_kit_holds(self, policy: SentinelPolicy) -> None:
        assert policy.decide(Verdict.INDETERMINATE).action is Action.HOLD


class TestConfigGuards:
    @pytest.mark.parametrize("bad", [0, -1])
    def test_open_after_must_be_positive(self, bad: int) -> None:
        with pytest.raises(ValueError, match="open_after"):
            SentinelConfig(open_after=bad)

    @pytest.mark.parametrize("bad", [0, -1])
    def test_close_after_must_be_positive(self, bad: int) -> None:
        with pytest.raises(ValueError, match="close_after"):
            SentinelConfig(close_after=bad)


class TestEndToEndAgainstTheLatch:
    """Through the real engine and the virtual actuator node."""

    def build(self, node: VirtualActuatorNode, scene: str) -> Sentinel:
        engine = InspectionEngine(
            manifest=KIT,
            source=ScriptedSource(scene),
            engine=SimulatedEngine(seed=11),
            link=LoopbackLink(node),
        )
        return Sentinel(
            engine, config=SentinelConfig(open_after=2, close_after=2)
        )

    def test_a_good_kit_opens_the_latch(self) -> None:
        node = VirtualActuatorNode()
        sentinel = self.build(node, "complete")
        for _ in range(2):
            sentinel.step()
        assert node.latch is LatchState.RELEASED

    def test_removing_an_item_closes_it_under_your_hand(self) -> None:
        """The demonstration, end to end: open on a good kit, then take
        something out and watch it engage."""
        node = VirtualActuatorNode()
        sentinel = self.build(node, "complete")
        for _ in range(2):
            sentinel.step()
        assert node.latch.value == "released"

        sentinel.engine.source = ScriptedSource("missing-shears")
        for _ in range(2):
            sentinel.step()

        assert node.latch is LatchState.ENGAGED
        assert node.buzzer is True

    def test_it_reopens_when_the_item_comes_back(self) -> None:
        """No manual reset. Put it back and the next passes reopen it."""
        node = VirtualActuatorNode()
        sentinel = self.build(node, "complete")
        for _ in range(2):
            sentinel.step()

        sentinel.engine.source = ScriptedSource("missing-shears")
        for _ in range(2):
            sentinel.step()
        assert node.latch.value == "engaged"

        sentinel.engine.source = ScriptedSource("complete")
        for _ in range(2):
            sentinel.step()
        assert node.latch is LatchState.RELEASED

    def test_the_latch_does_not_move_on_every_frame(self) -> None:
        """The engine must not be acting on its own behind the policy, or the
        debounce would be decorative."""
        node = VirtualActuatorNode()
        sentinel = self.build(node, "complete")
        sentinel.step()
        assert node.latch is LatchState.ENGAGED

    def test_an_obscured_kit_never_opens_the_latch(self) -> None:
        node = VirtualActuatorNode()
        sentinel = self.build(node, "occluded")
        for _ in range(6):
            sentinel.step()
        assert node.latch is LatchState.ENGAGED

    def test_run_yields_one_event_per_inspection(self) -> None:
        node = VirtualActuatorNode()
        sentinel = self.build(node, "complete")
        events = list(sentinel.run(limit=3))
        assert len(events) == 3
        assert events[-1].verdict is Verdict.PASS
