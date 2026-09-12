"""An in-process Actuator Node.

`VirtualActuatorNode` is a faithful software twin of
`firmware/mcu_actuator/mcu_actuator.ino`: same protocol, same state machine,
same watchdog, same refusal rules. Keeping it faithful is the point - it is
where latch behaviour gets tested, because you cannot easily ask a real
solenoid "would you have opened if the link had gone quiet for 3 seconds?"

If you change the firmware state machine, change this too. `tests/test_link.py`
pins the behaviours they must share.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from ..protocol import (
    Ack,
    AckStatus,
    Command,
    CommandFrame,
    ProtocolError,
    decode_ack,
    decode_command,
    encode_ack,
    encode_command,
)
from .base import HostLink, LinkError, LinkResult


class LatchState(StrEnum):
    ENGAGED = "engaged"
    """The safe resting state. The enclosure is locked."""

    RELEASED = "released"
    """Granted only by an accepted RELEASE, and only for its hold."""


class Indicator(StrEnum):
    OFF = "off"
    PASS = "pass"
    FAIL = "fail"
    HOLD = "hold"
    STALE = "stale"
    """The link has gone quiet. Distinct from FAIL: nothing is known to be
    wrong with the kit, but nothing is known to be right either."""


@dataclass
class VirtualActuatorNode:
    """The MCU's behaviour, in Python.

    Time is injected rather than read from the clock so that watchdog and hold
    expiry can be tested without sleeping.
    """

    link_timeout_ms: float = 2000.0
    """How long the Actuator Node tolerates silence before treating the Host
    Link as stale. On going stale it engages the Latch - it does not hold its
    last instruction."""

    clock: Callable[[], float] = field(default_factory=lambda: _monotonic_ms)

    latch: LatchState = LatchState.ENGAGED
    indicator: Indicator = Indicator.OFF
    buzzer: bool = False

    last_seq: int | None = None
    last_contact_ms: float = field(default=0.0)
    release_expires_ms: float | None = None
    events: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.last_contact_ms = self.clock()

    # -- the MCU's main loop -------------------------------------------------

    def tick(self) -> None:
        """Run the periodic safety checks. Called by the firmware's loop()."""
        now = self.clock()

        if (
            self.release_expires_ms is not None
            and now >= self.release_expires_ms
        ):
            self._engage("hold expired")

        if now - self.last_contact_ms > self.link_timeout_ms:
            if self.latch is LatchState.RELEASED:
                self._engage("host link went stale")
            if self.indicator is not Indicator.STALE:
                self.indicator = Indicator.STALE
                self.events.append("link stale")

    @property
    def link_is_stale(self) -> bool:
        return self.clock() - self.last_contact_ms > self.link_timeout_ms

    def handle(self, wire: bytes) -> bytes:
        """Process one inbound frame and return the acknowledgement bytes."""
        self.tick()

        try:
            frame = decode_command(wire)
        except ProtocolError as exc:
            self.events.append(f"rejected frame: {exc}")
            status = (
                AckStatus.BAD_CRC if "checksum" in str(exc) else AckStatus.BAD_FRAME
            )
            return encode_ack(Ack(seq=0, status=status))

        # Contact is only recorded for frames that verified. A stream of
        # corrupt bytes must not keep the watchdog satisfied.
        self.last_contact_ms = self.clock()

        if self._is_replay(frame):
            self.events.append(f"refused replayed seq {frame.seq}")
            return encode_ack(Ack(seq=frame.seq, status=AckStatus.REFUSED))
        self.last_seq = frame.seq

        return encode_ack(Ack(seq=frame.seq, status=self._apply(frame)))

    # -- command handling ----------------------------------------------------

    def _apply(self, frame: CommandFrame) -> AckStatus:
        if frame.command is Command.PING:
            return AckStatus.OK

        if frame.command is Command.RESET:
            self._engage("reset requested")
            self.indicator = Indicator.OFF
            self.buzzer = False
            return AckStatus.OK

        if frame.command is Command.RELEASE:
            return self._release(frame.payload)

        if frame.command is Command.REJECT:
            self._engage("reject")
            self.indicator = Indicator.FAIL
            self.buzzer = True
            self.events.append(f"reject: {frame.payload}")
            return AckStatus.OK

        if frame.command is Command.HOLD:
            self._engage("hold")
            self.indicator = Indicator.HOLD
            self.buzzer = False
            self.events.append(f"hold: {frame.payload}")
            return AckStatus.OK

        return AckStatus.BAD_FRAME

    def _release(self, payload: str) -> AckStatus:
        try:
            hold_ms = float(payload)
        except ValueError:
            self.events.append(f"refused RELEASE with bad hold {payload!r}")
            return AckStatus.BAD_FRAME

        if hold_ms <= 0 or hold_ms > 60_000:
            # An unbounded or absurd hold is a bug on the host side, and the
            # consequence is an enclosure left open. Refuse it here.
            self.events.append(f"refused RELEASE with out-of-range hold {hold_ms}")
            return AckStatus.REFUSED

        self.latch = LatchState.RELEASED
        self.release_expires_ms = self.clock() + hold_ms
        self.indicator = Indicator.PASS
        self.buzzer = False
        self.events.append(f"released for {hold_ms:.0f}ms")
        return AckStatus.OK

    def _is_replay(self, frame: CommandFrame) -> bool:
        """Refuse a sequence number we have already acted on.

        Only RELEASE is actually dangerous to replay, but refusing uniformly
        keeps the rule simple enough to hold in the firmware too.
        """
        return self.last_seq is not None and frame.seq == self.last_seq

    def _engage(self, reason: str) -> None:
        if self.latch is LatchState.RELEASED:
            self.events.append(f"latch engaged: {reason}")
        self.latch = LatchState.ENGAGED
        self.release_expires_ms = None


class LoopbackLink(HostLink):
    """Talks to a VirtualActuatorNode in this process."""

    def __init__(self, node: VirtualActuatorNode | None = None) -> None:
        self.node = node if node is not None else VirtualActuatorNode()
        self._seq = 0

    def send(self, command: Command, payload: str = "") -> LinkResult:
        self._seq = (self._seq + 1) % 65536
        seq = self._seq
        try:
            wire = encode_command(CommandFrame(seq=seq, command=command, payload=payload))
        except ProtocolError as exc:
            return LinkResult(command=command, seq=seq, ack=None, error=str(exc))

        try:
            ack = decode_ack(self.node.handle(wire))
        except ProtocolError as exc:
            return LinkResult(command=command, seq=seq, ack=None, error=str(exc))

        if ack.seq != seq:
            return LinkResult(
                command=command,
                seq=seq,
                ack=None,
                error=f"acknowledgement was for seq {ack.seq}, expected {seq}",
            )
        if ack.status is not AckStatus.OK:
            return LinkResult(
                command=command,
                seq=seq,
                ack=ack,
                error=f"actuator node returned {ack.status.value}",
            )
        return LinkResult(command=command, seq=seq, ack=ack)


def _monotonic_ms() -> float:
    import time

    return time.monotonic() * 1000.0


__all__ = [
    "Indicator",
    "LatchState",
    "LinkError",
    "LoopbackLink",
    "VirtualActuatorNode",
]
