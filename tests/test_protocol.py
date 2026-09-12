"""Host Link framing - the seam where a Verdict becomes a Command on the wire.

The Actuator Node is holding a physical lock. Anything it acts on must be
provably intact and provably fresh, so every frame carries a sequence number
and a checksum, and every Command is acknowledged.
"""

from __future__ import annotations

import pytest

from readykit.protocol import (
    Ack,
    AckStatus,
    Command,
    CommandFrame,
    ProtocolError,
    crc8,
    decode_ack,
    decode_command,
    encode_command,
)


class TestChecksum:
    def test_crc8_is_stable_for_known_input(self) -> None:
        # Independent reference value for CRC-8/ATM (poly 0x07, init 0x00)
        # over b"123456789".
        assert crc8(b"123456789") == 0xF4

    def test_crc8_of_empty_is_zero(self) -> None:
        assert crc8(b"") == 0x00

    def test_single_bit_flip_changes_the_checksum(self) -> None:
        assert crc8(b"RELEASE 5000") != crc8(b"RELEASE 5001")


class TestCommandRoundTrip:
    def test_release_survives_the_round_trip(self) -> None:
        frame = CommandFrame(seq=7, command=Command.RELEASE, payload="5000")
        decoded = decode_command(encode_command(frame))
        assert decoded == frame

    def test_encoded_frame_is_a_single_newline_terminated_line(self) -> None:
        wire = encode_command(CommandFrame(seq=1, command=Command.PING, payload=""))
        assert wire.endswith(b"\n")
        assert wire.count(b"\n") == 1

    @pytest.mark.parametrize("command", list(Command))
    def test_every_command_round_trips(self, command: Command) -> None:
        frame = CommandFrame(seq=42, command=command, payload="x")
        assert decode_command(encode_command(frame)) == frame


class TestCorruptionIsRejected:
    def test_flipped_payload_byte_is_rejected(self) -> None:
        wire = bytearray(encode_command(CommandFrame(3, Command.RELEASE, "5000")))
        index = wire.index(ord("5"))
        wire[index] = ord("9")
        with pytest.raises(ProtocolError, match="checksum"):
            decode_command(bytes(wire))

    def test_truncated_frame_is_rejected(self) -> None:
        wire = encode_command(CommandFrame(3, Command.RELEASE, "5000"))
        with pytest.raises(ProtocolError):
            decode_command(wire[:6])

    def test_unknown_command_is_rejected(self) -> None:
        body = "RK1 3 LAUNCH_MISSILE x"
        wire = f"{body} {crc8(body.encode()):02X}\n".encode()
        with pytest.raises(ProtocolError, match=r"[Uu]nknown command"):
            decode_command(wire)

    def test_wrong_protocol_version_is_rejected(self) -> None:
        body = "RK9 3 PING "
        wire = f"{body} {crc8(body.encode()):02X}\n".encode()
        with pytest.raises(ProtocolError, match="version"):
            decode_command(wire)

    def test_garbage_is_rejected_rather_than_guessed_at(self) -> None:
        with pytest.raises(ProtocolError):
            decode_command(b"\x00\xff\xfe nonsense\n")


class TestAck:
    def test_ack_round_trips(self) -> None:
        assert decode_ack(b"ACK 7 OK\n") == Ack(seq=7, status=AckStatus.OK)

    def test_nack_carries_its_status(self) -> None:
        assert decode_ack(b"ACK 7 BAD_CRC\n") == Ack(seq=7, status=AckStatus.BAD_CRC)

    def test_malformed_ack_is_rejected(self) -> None:
        with pytest.raises(ProtocolError):
            decode_ack(b"ACK seven OK\n")

    def test_unknown_ack_status_is_rejected(self) -> None:
        with pytest.raises(ProtocolError):
            decode_ack(b"ACK 7 VIBES\n")


class TestPayloadSafety:
    def test_payload_containing_a_newline_is_refused_at_encode(self) -> None:
        """A newline in the payload would split one Command into two frames."""
        with pytest.raises(ProtocolError, match="newline"):
            encode_command(CommandFrame(1, Command.REJECT, "missing\nshears"))

    def test_sequence_number_must_fit_the_wire_format(self) -> None:
        with pytest.raises(ProtocolError, match="sequence"):
            encode_command(CommandFrame(-1, Command.PING, ""))
