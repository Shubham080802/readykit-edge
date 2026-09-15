"""The link that actually drives the Arduino.

This had no tests at all, which is the wrong place to have none: every other
path in this project can be exercised on any laptop, but this one only runs
when a board is attached - so on the day it is the least-rehearsed code in the
system, and it is the code holding a lock shut.

Everything here runs against a fake serial port. That is enough to pin the
behaviour that matters, all of which is about what happens when the wire
misbehaves rather than when it works:

  - a retry must not be able to actuate twice
  - a silent board must not read as success
  - an ack for somebody else's command must not be accepted
  - closing must leave the enclosure safe

None of it needs pyserial installed.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from readykit.bridge.base import LinkError
from readykit.protocol import Ack, AckStatus, Command, decode_command, encode_ack


class FakePort:
    """Stands in for `serial.Serial`."""

    def __init__(self, replies: list[bytes | None] | None = None) -> None:
        self.port = ""
        self.baud = 0
        self.timeout = 0.0
        self.written: list[bytes] = []
        self.replies = replies
        self.is_open = True
        self.closed = False
        self.input_resets = 0
        self.raise_on_write: Exception | None = None

    # -- the pyserial surface SerialLink touches --------------------------
    def reset_input_buffer(self) -> None:
        self.input_resets += 1

    def reset_output_buffer(self) -> None:
        pass

    def write(self, data: bytes) -> int:
        if self.raise_on_write is not None:
            raise self.raise_on_write
        self.written.append(data)
        return len(data)

    def flush(self) -> None:
        pass

    def readline(self) -> bytes:
        """Echo a well-formed ACK unless a script says otherwise."""
        if self.replies is not None:
            reply = self.replies.pop(0) if self.replies else b""
            return reply if reply is not None else b""
        if not self.written:
            return b""
        frame = decode_command(self.written[-1])
        return encode_ack(Ack(seq=frame.seq, status=AckStatus.OK))

    def close(self) -> None:
        self.closed = True
        self.is_open = False

    # -- helpers ----------------------------------------------------------
    @property
    def commands(self) -> list[Any]:
        return [decode_command(frame) for frame in self.written]


@pytest.fixture
def fake_serial(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install a fake `serial` module for the duration of a test."""
    created: list[FakePort] = []
    module = types.ModuleType("serial")

    def Serial(port: str, baud: int, timeout: float = 1.0) -> FakePort:
        if port == "/dev/nonexistent":
            raise OSError("no such device")
        fake = created[0] if created else FakePort()
        if not created:
            created.append(fake)
        fake.port, fake.baud, fake.timeout = port, baud, timeout
        return fake

    module.Serial = Serial  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "serial", module)
    module.created = created  # type: ignore[attr-defined]
    return module


def build(fake_serial: Any, port: FakePort | None = None, **kwargs: Any) -> Any:
    from readykit.bridge.serial_link import SerialLink

    if port is not None:
        fake_serial.created.clear()
        fake_serial.created.append(port)
    kwargs.setdefault("settle_seconds", 0)
    return SerialLink("/dev/fake", **kwargs)


class TestOpening:
    def test_a_dead_port_is_reported_not_raised_raw(self, fake_serial: Any) -> None:
        from readykit.bridge.serial_link import SerialLink

        with pytest.raises(LinkError, match="could not open"):
            SerialLink("/dev/nonexistent", settle_seconds=0)

    def test_missing_pyserial_names_the_fix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "serial", None)
        from readykit.bridge.serial_link import SerialLink

        with pytest.raises(LinkError, match="pyserial is not installed"):
            SerialLink("/dev/fake", settle_seconds=0)

    def test_buffers_are_cleared_after_the_bootloader_window(
        self, fake_serial: Any
    ) -> None:
        """Opening the port resets the MCU on most Arduino boards. Anything
        left in the buffers from before is from a different session."""
        port = FakePort()
        build(fake_serial, port)
        assert port.input_resets >= 1


class TestSendingSucceeds:
    def test_a_command_is_acknowledged(self, fake_serial: Any) -> None:
        link = build(fake_serial, FakePort())
        result = link.send(Command.RELEASE, "5000")
        assert result.acknowledged
        assert result.error == ""

    def test_the_payload_reaches_the_wire(self, fake_serial: Any) -> None:
        port = FakePort()
        link = build(fake_serial, port)
        link.send(Command.REJECT, "missing shears")
        assert port.commands[-1].payload == "missing shears"
        assert port.commands[-1].command is Command.REJECT

    def test_sequence_numbers_advance(self, fake_serial: Any) -> None:
        port = FakePort()
        link = build(fake_serial, port)
        for _ in range(3):
            link.send(Command.PING)
        assert [c.seq for c in port.commands] == [1, 2, 3]

    def test_sequence_wraps_without_colliding(self, fake_serial: Any) -> None:
        """65536 is not a valid sequence number on the wire."""
        port = FakePort()
        link = build(fake_serial, port)
        link._seq = 65535
        link.send(Command.PING)
        assert port.commands[-1].seq == 0


class TestTheWireMisbehaving:
    def test_silence_is_not_success(self, fake_serial: Any) -> None:
        """The failure that matters most. A board that never answered has not
        released anything, and must never be recorded as though it did."""
        port = FakePort(replies=[b"", b"", b""])
        link = build(fake_serial, port, retries=2)
        result = link.send(Command.RELEASE, "5000")
        assert not result.acknowledged
        assert "did not acknowledge" in result.error

    def test_a_retry_reuses_the_sequence_number(self, fake_serial: Any) -> None:
        """So a RELEASE that landed but whose ack was lost cannot be applied
        twice - the Actuator Node refuses a repeat."""
        port = FakePort(replies=[b"", b"", b""])
        link = build(fake_serial, port, retries=2)
        link.send(Command.RELEASE, "5000")
        assert len({c.seq for c in port.commands}) == 1, port.commands

    def test_it_stops_after_the_configured_retries(self, fake_serial: Any) -> None:
        port = FakePort(replies=[b"", b"", b"", b"", b""])
        link = build(fake_serial, port, retries=2)
        link.send(Command.PING)
        assert len(port.written) == 3  # one attempt plus two retries

    def test_no_retries_means_one_attempt(self, fake_serial: Any) -> None:
        port = FakePort(replies=[b""])
        link = build(fake_serial, port, retries=0)
        link.send(Command.PING)
        assert len(port.written) == 1

    def test_a_recovered_retry_is_a_success(self, fake_serial: Any) -> None:
        port = FakePort(replies=[b"", encode_ack(Ack(seq=1, status=AckStatus.OK))])
        link = build(fake_serial, port, retries=2)
        assert link.send(Command.PING).acknowledged

    def test_garbage_is_not_an_ack(self, fake_serial: Any) -> None:
        port = FakePort(replies=[b"line noise\n"] * 3)
        link = build(fake_serial, port, retries=2)
        result = link.send(Command.PING)
        assert not result.acknowledged
        assert "unreadable ack" in result.error

    def test_an_ack_for_another_command_is_refused(self, fake_serial: Any) -> None:
        """Out-of-order acks would otherwise let one command's success be read
        as another's."""
        port = FakePort(
            replies=[encode_ack(Ack(seq=99, status=AckStatus.OK))] * 3
        )
        link = build(fake_serial, port, retries=2)
        result = link.send(Command.RELEASE, "5000")
        assert not result.acknowledged
        assert "expected" in result.error

    def test_a_refusal_from_the_board_is_surfaced(self, fake_serial: Any) -> None:
        port = FakePort(
            replies=[encode_ack(Ack(seq=1, status=AckStatus.REFUSED))] * 3
        )
        link = build(fake_serial, port, retries=2)
        result = link.send(Command.RELEASE, "999999")
        assert not result.acknowledged
        assert "REFUSED" in result.error
        assert result.ack is not None

    def test_io_failure_is_caught_not_propagated(self, fake_serial: Any) -> None:
        """A yanked USB cable mid-write must resolve to INDETERMINATE
        upstream, not crash the inspection loop."""
        port = FakePort()
        port.raise_on_write = OSError("device disconnected")
        link = build(fake_serial, port, retries=1)
        result = link.send(Command.PING)
        assert not result.acknowledged
        assert "serial I/O failed" in result.error

    def test_an_unencodable_payload_never_reaches_the_wire(
        self, fake_serial: Any
    ) -> None:
        port = FakePort()
        link = build(fake_serial, port)
        result = link.send(Command.REJECT, "payload with \n a newline")
        assert not result.acknowledged
        assert port.written == []


class TestClosing:
    def test_it_resets_the_latch_on_the_way_out(self, fake_serial: Any) -> None:
        """Leave the enclosure safe rather than however the last inspection
        left it."""
        port = FakePort()
        link = build(fake_serial, port)
        link.send(Command.RELEASE, "5000")
        link.close()
        assert port.commands[-1].command is Command.RESET
        assert port.closed

    def test_closing_twice_is_safe(self, fake_serial: Any) -> None:
        port = FakePort()
        link = build(fake_serial, port)
        link.close()
        link.close()
        assert port.closed

    def test_a_failing_reset_still_closes_the_port(self, fake_serial: Any) -> None:
        """A board that has already gone away must not leave the port open."""
        port = FakePort()
        link = build(fake_serial, port)
        port.raise_on_write = OSError("gone")
        link.close()
        assert port.closed
