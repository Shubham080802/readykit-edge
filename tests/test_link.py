"""Host Link and Actuator Node behaviour.

These tests are the reason the virtual node exists. "Would the latch have
re-engaged if the host died mid-hold?" is a question you can ask software and
cannot easily ask a solenoid.

Every behaviour pinned here must also hold in
`firmware/mcu_actuator/mcu_actuator.ino`.
"""

from __future__ import annotations

import pytest

from readykit.bridge import LoopbackLink, VirtualActuatorNode
from readykit.bridge.loopback import Indicator, LatchState
from readykit.protocol import (
    Ack,
    AckStatus,
    Command,
    CommandFrame,
    crc8,
    decode_ack,
    encode_command,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, ms: float) -> None:
        self.now += ms


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def node(clock: FakeClock) -> VirtualActuatorNode:
    return VirtualActuatorNode(clock=clock, link_timeout_ms=2000.0)


@pytest.fixture
def link(node: VirtualActuatorNode) -> LoopbackLink:
    return LoopbackLink(node)


class TestRestingState:
    def test_latch_starts_engaged(self, node: VirtualActuatorNode) -> None:
        assert node.latch is LatchState.ENGAGED

    def test_a_fresh_node_has_no_indicator_lit(
        self, node: VirtualActuatorNode
    ) -> None:
        assert node.indicator is Indicator.OFF


class TestRelease:
    def test_release_opens_the_latch(
        self, link: LoopbackLink, node: VirtualActuatorNode
    ) -> None:
        result = link.send(Command.RELEASE, "5000")
        assert result.acknowledged
        assert node.latch is LatchState.RELEASED
        assert node.indicator is Indicator.PASS

    def test_latch_re_engages_when_the_hold_expires(
        self, link: LoopbackLink, node: VirtualActuatorNode, clock: FakeClock
    ) -> None:
        """The host must heartbeat through the hold, or the watchdog cuts it
        short - see test_a_hold_outliving_the_link_timeout_is_cut_short."""
        link.send(Command.RELEASE, "5000")

        for _ in range(4):
            clock.advance(1000)
            link.send(Command.PING)
            node.tick()
            assert node.latch is LatchState.RELEASED

        clock.advance(1001)
        node.tick()
        assert node.latch is LatchState.ENGAGED

    def test_a_hold_outliving_the_link_timeout_is_cut_short(
        self, link: LoopbackLink, node: VirtualActuatorNode, clock: FakeClock
    ) -> None:
        """A hold longer than the link timeout is only honoured while the host
        keeps talking. A silent host loses the latch at the watchdog, not at
        the end of the hold - the shorter of the two always wins, which is the
        safe direction.
        """
        link.send(Command.RELEASE, "5000")
        clock.advance(2001)
        node.tick()
        assert node.latch is LatchState.ENGAGED
        assert "host link went stale" in " ".join(node.events)

    def test_release_with_an_absurd_hold_is_refused(
        self, link: LoopbackLink, node: VirtualActuatorNode
    ) -> None:
        """An unbounded hold means an enclosure left standing open."""
        result = link.send(Command.RELEASE, "999999999")
        assert not result.acknowledged
        assert node.latch is LatchState.ENGAGED

    def test_release_with_a_non_numeric_hold_is_refused(
        self, link: LoopbackLink, node: VirtualActuatorNode
    ) -> None:
        result = link.send(Command.RELEASE, "forever")
        assert not result.acknowledged
        assert node.latch is LatchState.ENGAGED


class TestWatchdog:
    def test_silence_re_engages_an_open_latch(
        self, link: LoopbackLink, node: VirtualActuatorNode, clock: FakeClock
    ) -> None:
        """The host dying mid-hold must not leave the enclosure open.

        This is the failure the blueprint's blocking `delay(5000)` cannot
        even detect: during that delay the MCU reads no serial at all.
        """
        link.send(Command.RELEASE, "30000")

        clock.advance(2001)
        node.tick()
        assert node.latch is LatchState.ENGAGED
        assert node.indicator is Indicator.STALE
        assert "host link went stale" in " ".join(node.events)

    def test_heartbeats_keep_the_link_fresh(
        self, link: LoopbackLink, node: VirtualActuatorNode, clock: FakeClock
    ) -> None:
        link.send(Command.RELEASE, "30000")
        for _ in range(5):
            clock.advance(1000)
            link.send(Command.PING)
        assert node.latch is LatchState.RELEASED

    def test_corrupt_traffic_does_not_satisfy_the_watchdog(
        self, node: VirtualActuatorNode, clock: FakeClock
    ) -> None:
        """A noisy line must not read as a live host."""
        clock.advance(1500)
        node.handle(b"RK1 4 RELEASE 5000 00\n")  # wrong checksum
        clock.advance(600)
        node.tick()
        assert node.link_is_stale


class TestReplayProtection:
    def test_a_repeated_sequence_number_is_refused(
        self, node: VirtualActuatorNode
    ) -> None:
        wire = encode_command(CommandFrame(9, Command.RELEASE, "5000"))
        assert decode_ack(node.handle(wire)).status is AckStatus.OK

        node.tick()
        replayed = decode_ack(node.handle(wire))
        assert replayed.status is AckStatus.REFUSED


class TestCorruption:
    def test_bad_checksum_is_nacked_and_not_acted_on(
        self, node: VirtualActuatorNode
    ) -> None:
        ack = decode_ack(node.handle(b"RK1 4 RELEASE 5000 00\n"))
        assert ack.status is AckStatus.BAD_CRC
        assert node.latch is LatchState.ENGAGED

    def test_unknown_command_is_nacked_and_not_acted_on(
        self, node: VirtualActuatorNode
    ) -> None:
        body = "RK1 4 DETONATE x"
        wire = f"{body} {crc8(body.encode()):02X}\n".encode()
        assert decode_ack(node.handle(wire)).status is AckStatus.BAD_FRAME
        assert node.latch is LatchState.ENGAGED

    def test_line_noise_never_opens_the_latch(
        self, node: VirtualActuatorNode
    ) -> None:
        for junk in (b"\x00\x00\n", b"RELEASE\n", b"PASS_KIT\n", b"\xff\xfe\n"):
            node.handle(junk)
            assert node.latch is LatchState.ENGAGED

    def test_the_blueprints_wire_format_is_not_honoured(
        self, node: VirtualActuatorNode
    ) -> None:
        """`PASS_KIT\\n` carries no sequence number and no checksum, so it
        cannot be trusted and must not open anything."""
        ack = decode_ack(node.handle(b"PASS_KIT\n"))
        assert ack.status is not AckStatus.OK
        assert node.latch is LatchState.ENGAGED


class TestRejectAndHold:
    def test_reject_raises_the_alarm_and_keeps_the_latch_shut(
        self, link: LoopbackLink, node: VirtualActuatorNode
    ) -> None:
        link.send(Command.REJECT, "missing shears")
        assert node.latch is LatchState.ENGAGED
        assert node.indicator is Indicator.FAIL
        assert node.buzzer is True

    def test_hold_is_distinct_from_reject_and_is_silent(
        self, link: LoopbackLink, node: VirtualActuatorNode
    ) -> None:
        """'I cannot see your kit' and 'your kit is wrong' need different
        signals - the operator's next action differs."""
        link.send(Command.HOLD, "occluded")
        assert node.latch is LatchState.ENGAGED
        assert node.indicator is Indicator.HOLD
        assert node.buzzer is False

    def test_reject_closes_a_latch_that_was_open(
        self, link: LoopbackLink, node: VirtualActuatorNode
    ) -> None:
        link.send(Command.RELEASE, "30000")
        link.send(Command.REJECT, "kit swapped")
        assert node.latch is LatchState.ENGAGED


class TestLinkResultReporting:
    def test_an_acknowledged_command_describes_itself_as_such(
        self, link: LoopbackLink
    ) -> None:
        assert "ack=OK" in link.send(Command.PING).describe()

    def test_a_refused_command_is_recorded_as_not_having_happened(
        self, link: LoopbackLink
    ) -> None:
        result = link.send(Command.RELEASE, "999999999")
        assert "NOT-ACKNOWLEDGED" in result.describe()
        assert not result.acknowledged


class TestAckHelpers:
    def test_encode_decode_ack_round_trip(self) -> None:
        from readykit.protocol import encode_ack

        original = Ack(seq=11, status=AckStatus.REFUSED)
        assert decode_ack(encode_ack(original)) == original
