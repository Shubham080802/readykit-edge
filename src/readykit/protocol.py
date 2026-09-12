"""The Host Link wire protocol.

The Actuator Node holds a physical lock closed. It must never act on a frame it
cannot verify, and the Inspection Host must never believe a Command landed
unless it was acknowledged. That gives three requirements:

* **Integrity** - every frame carries a CRC-8. A corrupted RELEASE is dropped,
  not guessed at.
* **Freshness** - every frame carries a sequence number, so a replayed or
  stale RELEASE can be recognised.
* **Acknowledgement** - the Host learns what actually happened. A Command with
  no ACK is recorded as not having happened.

The format is deliberately line-oriented ASCII: it has to be parsed by a
hand-written reader on an STM32U585 with no allocator, and it has to be
readable on a logic analyser at 3am.

    RK1 <seq> <COMMAND> <payload> <CRC8>\\n
    ACK <seq> <STATUS>\\n

The CRC covers everything before the checksum field, excluding the space that
separates them. `firmware/mcu_actuator/protocol.h` implements the same table;
if you change the polynomial here, change it there.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

PROTOCOL_VERSION = "RK1"
MAX_SEQ = 65535
MAX_PAYLOAD = 64


class ProtocolError(ValueError):
    """A frame could not be trusted. Never recoverable by guessing."""


class Command(StrEnum):
    """An instruction from the Inspection Host to the Actuator Node."""

    RELEASE = "RELEASE"
    """Verdict was PASS. Release the Latch for the payload's hold in ms."""

    REJECT = "REJECT"
    """Verdict was FAIL. Keep the Latch engaged and raise the failure alarm."""

    HOLD = "HOLD"
    """Verdict was INDETERMINATE. Keep the Latch engaged, signal 'looking'.

    Distinct from REJECT so the operator can tell 'your kit is wrong' from
    'I cannot see your kit' - two very different things to do next.
    """

    PING = "PING"
    """Heartbeat. Absence of these is what trips the Actuator Node watchdog."""

    RESET = "RESET"
    """Return to the safe resting state immediately."""


class AckStatus(StrEnum):
    OK = "OK"
    BAD_CRC = "BAD_CRC"
    BAD_FRAME = "BAD_FRAME"
    REFUSED = "REFUSED"
    """The Actuator Node understood the Command and declined it - for example a
    RELEASE while its own watchdog considers the link stale."""


@dataclass(frozen=True, slots=True)
class CommandFrame:
    seq: int
    command: Command
    payload: str = ""


@dataclass(frozen=True, slots=True)
class Ack:
    seq: int
    status: AckStatus


def _crc8_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
        table.append(crc)
    return tuple(table)


_CRC8_TABLE = _crc8_table()


def crc8(data: bytes) -> int:
    """CRC-8/ATM: polynomial 0x07, init 0x00, no reflection, no final xor."""
    crc = 0x00
    for byte in data:
        crc = _CRC8_TABLE[crc ^ byte]
    return crc


def encode_command(frame: CommandFrame) -> bytes:
    if not 0 <= frame.seq <= MAX_SEQ:
        raise ProtocolError(
            f"sequence number {frame.seq} outside 0..{MAX_SEQ}"
        )
    if "\n" in frame.payload or "\r" in frame.payload:
        raise ProtocolError("payload must not contain a newline")
    if len(frame.payload) > MAX_PAYLOAD:
        raise ProtocolError(
            f"payload of {len(frame.payload)} exceeds {MAX_PAYLOAD} bytes"
        )
    body = f"{PROTOCOL_VERSION} {frame.seq} {frame.command.value} {frame.payload}"
    return f"{body} {crc8(body.encode('ascii')):02X}\n".encode("ascii")


def decode_command(wire: bytes) -> CommandFrame:
    text = _decode_line(wire)
    body, _, checksum_field = text.rpartition(" ")
    if not body:
        raise ProtocolError(f"frame has no checksum field: {text!r}")
    try:
        received = int(checksum_field, 16)
    except ValueError as exc:
        raise ProtocolError(f"checksum field {checksum_field!r} is not hex") from exc

    expected = crc8(body.encode("ascii"))
    if received != expected:
        raise ProtocolError(
            f"checksum mismatch: frame claims {received:02X}, computed {expected:02X}"
        )

    parts = body.split(" ", 3)
    if len(parts) < 3:
        raise ProtocolError(f"frame is missing fields: {body!r}")
    version, raw_seq, raw_command = parts[0], parts[1], parts[2]
    payload = parts[3] if len(parts) == 4 else ""

    if version != PROTOCOL_VERSION:
        raise ProtocolError(
            f"unsupported protocol version {version!r}, expected {PROTOCOL_VERSION!r}"
        )
    try:
        seq = int(raw_seq)
    except ValueError as exc:
        raise ProtocolError(f"sequence number {raw_seq!r} is not an integer") from exc
    try:
        command = Command(raw_command)
    except ValueError as exc:
        raise ProtocolError(f"unknown command {raw_command!r}") from exc

    return CommandFrame(seq=seq, command=command, payload=payload)


def decode_ack(wire: bytes) -> Ack:
    text = _decode_line(wire)
    parts = text.split(" ")
    if len(parts) != 3 or parts[0] != "ACK":
        raise ProtocolError(f"not an acknowledgement: {text!r}")
    try:
        seq = int(parts[1])
    except ValueError as exc:
        raise ProtocolError(f"ack sequence {parts[1]!r} is not an integer") from exc
    try:
        status = AckStatus(parts[2])
    except ValueError as exc:
        raise ProtocolError(f"unknown ack status {parts[2]!r}") from exc
    return Ack(seq=seq, status=status)


def encode_ack(ack: Ack) -> bytes:
    """Used by the loopback Actuator Node, and mirrored by the firmware."""
    return f"ACK {ack.seq} {ack.status.value}\n".encode("ascii")


def _decode_line(wire: bytes) -> str:
    try:
        text = wire.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ProtocolError("frame is not ASCII") from exc
    text = text.strip("\r\n")
    if not text:
        raise ProtocolError("frame is empty")
    if any(ord(ch) < 0x20 for ch in text):
        raise ProtocolError("frame contains control characters")
    return text
