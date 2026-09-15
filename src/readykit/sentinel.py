"""Watching continuously, and carrying on watching after the latch opens.

An inspection station that checks once and then stops paying attention is a
cabinet that gets emptied. The moment it matters is the one *after* the latch
releases: somebody has the door open and their hands in the tray, and the
system either notices what they took or it does not.

So this runs the inspection on a rolling interval and stays in the loop
through the hold. Lift a required item out and the latch engages under your
hand.

The interesting part is not the loop - it is knowing when *not* to act.

## Slow to open, quick to close

The two directions are not symmetric and must not share a threshold.

Opening is granted, so it is deliberately conservative: several consecutive
PASS verdicts before the latch moves. One lucky frame must never be enough to
release a lock.

Closing is a retraction of something already granted, so it is quicker - but
not instant, or the first hand to cross the tray slams the door. A couple of
consecutive failures, which at the default interval lands around a second:
fast enough to feel like a reaction, slow enough that a passing hand is
ignored.

## Why occlusion does not close the latch

While the latch is released, the kit is *meant* to be reached into. Hands
cover items; frames go INDETERMINATE constantly. Treating that as a reason to
close would make the cabinet unusable - it would slam every time somebody used
it properly.

So during the hold, only a **positive finding** closes it: the model saw the
tray clearly enough to establish that a required item is gone. "I cannot see
right now" is expected during access and is recorded, not acted on.

That is not a hole. The latch is already open by design during the hold, and
the hold expiry bounds the exposure regardless - the firmware re-engages on
its own timer whatever this loop believes. The sentinel adds detection of
definite removal on top of that bound; it does not replace it.

Note this is the opposite posture to `resolve_verdict`, where INDETERMINATE
always keeps the latch shut. That is correct in both places: refusing to open
on doubt is conservative, and refusing to *close* on doubt would be an alarm
that never stops ringing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from time import monotonic
from typing import ClassVar

from .domain import Verdict
from .engine import InspectionEngine, InspectionOutcome
from .protocol import Command


class SentinelState(StrEnum):
    WATCHING = "watching"
    """Latch engaged. Looking for a reason to open."""

    RELEASED = "released"
    """Latch open. Looking for a reason to close."""


class Action(StrEnum):
    RELEASE = "release"
    REJECT = "reject"
    HOLD = "hold"
    NONE = "none"
    """Nothing changed. Heartbeat and look again."""


@dataclass(frozen=True, slots=True)
class SentinelConfig:
    open_after: int = 2
    """Consecutive PASS verdicts before the latch is released. Raising this
    makes opening slower and more certain."""

    close_after: int = 2
    """Consecutive FAIL verdicts, while released, before re-engaging. Lowering
    this makes the reaction sharper and the false-alarm rate worse."""

    hold_seconds: float = 5.0
    """Mirrors the manifest's hold. The firmware owns the real timer; this is
    only so the policy knows when to stop believing the latch is open."""

    def __post_init__(self) -> None:
        if self.open_after < 1:
            raise ValueError(f"open_after must be >= 1, got {self.open_after}")
        if self.close_after < 1:
            raise ValueError(f"close_after must be >= 1, got {self.close_after}")


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    state: SentinelState
    reason: str


@dataclass
class SentinelPolicy:
    """The state machine, with no I/O and no wall clock.

    Time is injected so debounce behaviour can be tested exactly rather than
    by sleeping and hoping.
    """

    config: SentinelConfig = field(default_factory=SentinelConfig)
    clock: Callable[[], float] = monotonic

    state: SentinelState = SentinelState.WATCHING
    pass_streak: int = 0
    fail_streak: int = 0
    released_at: float | None = None

    occluded_while_open: int = 0
    """Frames we could not read during a hold. Not acted on - recorded, so the
    operator can see the camera was struggling even though nothing closed."""

    def decide(self, verdict: Verdict) -> Decision:
        self._expire_hold()
        if self.state is SentinelState.RELEASED:
            return self._while_released(verdict)
        return self._while_watching(verdict)

    # -- watching ------------------------------------------------------------

    def _while_watching(self, verdict: Verdict) -> Decision:
        if verdict is Verdict.PASS:
            self.pass_streak += 1
            if self.pass_streak >= self.config.open_after:
                self.state = SentinelState.RELEASED
                self.released_at = self.clock()
                self.pass_streak = 0
                self.fail_streak = 0
                self.occluded_while_open = 0
                return Decision(
                    Action.RELEASE,
                    self.state,
                    f"{self.config.open_after} consecutive passes",
                )
            return Decision(
                Action.NONE,
                self.state,
                f"pass {self.pass_streak} of {self.config.open_after}",
            )

        # Any non-pass resets progress towards opening. A kit has to be good
        # for the whole streak, not good on average.
        self.pass_streak = 0
        if verdict is Verdict.FAIL:
            return Decision(Action.REJECT, self.state, "kit is non-compliant")
        return Decision(Action.HOLD, self.state, "compliance not established")

    # -- released ------------------------------------------------------------

    def _while_released(self, verdict: Verdict) -> Decision:
        if verdict is Verdict.FAIL:
            self.fail_streak += 1
            if self.fail_streak >= self.config.close_after:
                self.state = SentinelState.WATCHING
                self.released_at = None
                self.fail_streak = 0
                return Decision(
                    Action.REJECT,
                    self.state,
                    "item removed while the latch was open",
                )
            return Decision(
                Action.NONE,
                self.state,
                f"possible removal, {self.fail_streak} of {self.config.close_after}",
            )

        if verdict is Verdict.INDETERMINATE:
            # Expected during access. Recorded, never acted on - see the module
            # docstring for why closing on doubt would make this unusable.
            self.occluded_while_open += 1
            self.fail_streak = 0
            return Decision(
                Action.NONE, self.state, "obscured during access, not acted on"
            )

        self.fail_streak = 0
        return Decision(Action.NONE, self.state, "kit still complete")

    # -- the hold ------------------------------------------------------------

    def _expire_hold(self) -> None:
        """The firmware re-engages on its own timer. Follow it rather than
        assuming the latch is still open."""
        if self.state is not SentinelState.RELEASED or self.released_at is None:
            return
        if self.clock() - self.released_at >= self.config.hold_seconds:
            self.state = SentinelState.WATCHING
            self.released_at = None
            self.fail_streak = 0


@dataclass(frozen=True, slots=True)
class SentinelEvent:
    outcome: InspectionOutcome
    decision: Decision

    @property
    def verdict(self) -> Verdict:
        return self.outcome.verdict


class Sentinel:
    """Drives an InspectionEngine continuously through the policy above."""

    _COMMAND_FOR: ClassVar[dict[Action, Command]] = {
        Action.RELEASE: Command.RELEASE,
        Action.REJECT: Command.REJECT,
        Action.HOLD: Command.HOLD,
    }

    def __init__(
        self,
        engine: InspectionEngine,
        config: SentinelConfig | None = None,
        policy: SentinelPolicy | None = None,
    ) -> None:
        self.engine = engine
        self.config = config or SentinelConfig(
            hold_seconds=engine.manifest.hold_seconds
        )
        self.policy = policy or SentinelPolicy(config=self.config)

    def step(self) -> SentinelEvent:
        """One inspection, one decision, and whatever command it implies.

        The engine already sends a command of its own per inspection; here the
        policy owns actuation instead, so the latch only moves when the state
        machine says so rather than on every frame.
        """
        outcome = self.engine.run_once_without_acting()
        decision = self.policy.decide(outcome.verdict)

        command = self._COMMAND_FOR.get(decision.action)
        if command is not None:
            payload = (
                f"{int(self.config.hold_seconds * 1000)}"
                if command is Command.RELEASE
                else decision.reason[:60]
            )
            self.engine.send(command, payload)
        else:
            self.engine.heartbeat()

        return SentinelEvent(outcome=outcome, decision=decision)

    def run(self, limit: int = 0) -> Iterator[SentinelEvent]:
        """Yield events forever, or for `limit` inspections."""
        completed = 0
        while limit == 0 or completed < limit:
            yield self.step()
            completed += 1
