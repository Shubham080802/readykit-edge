"""Cross-language conformance: the MCU parser vs the Python encoder.

The safety argument rests on two implementations of one protocol agreeing -
one in Python on the Snapdragon host, one in C on an STM32U585. They are
written in different languages by different hands and nothing but a test keeps
them honest.

This compiles `firmware/mcu_actuator/protocol.h` against a stub Arduino.h and
feeds it frames produced by `readykit.protocol`, then checks the firmware
reaches the same conclusion.

Skipped when no C++ compiler is available.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from readykit.protocol import Command, CommandFrame, crc8, encode_command

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "firmware" / "test" / "test_protocol.cpp"
STUB_DIR = REPO / "firmware" / "test"

_COMMAND_ORDINAL = {
    Command.RELEASE: 1,
    Command.REJECT: 2,
    Command.HOLD: 3,
    Command.PING: 4,
    Command.RESET: 5,
}


def _compiler() -> str | None:
    for candidate in ("c++", "g++", "clang++"):
        if shutil.which(candidate):
            return candidate
    return None


@pytest.fixture(scope="module")
def firmware(tmp_path_factory: pytest.TempPathFactory) -> Path:
    compiler = _compiler()
    if compiler is None:
        pytest.skip("no C++ compiler available to build the firmware harness")

    binary = tmp_path_factory.mktemp("firmware") / "rk_proto_test"
    result = subprocess.run(
        [
            compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-O1",
            "-I", str(STUB_DIR), "-o", str(binary), str(HARNESS),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"firmware harness failed to compile:\n{result.stderr}")
    return binary


def run(firmware: Path, lines: list[bytes]) -> list[str]:
    result = subprocess.run(
        [str(firmware)], input=b"".join(lines), capture_output=True
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.decode().splitlines()


class TestChecksumAgreement:
    @pytest.mark.parametrize(
        "text",
        [
            "123456789",
            "RK1 1 RELEASE 5000",
            "RK1 65535 PING ",
            "RK1 7 REJECT Kit non-compliant: missing Trauma Shears",
            "",
        ],
    )
    def test_c_and_python_compute_the_same_crc(
        self, firmware: Path, text: str
    ) -> None:
        output = run(firmware, [f"CRC {text}\n".encode()])
        assert output == [f"CRC {crc8(text.encode()):02X}"]


class TestTheFirmwareAcceptsWhatThePythonHostSends:
    @pytest.mark.parametrize("command", list(Command))
    def test_every_command_is_accepted(
        self, firmware: Path, command: Command
    ) -> None:
        wire = encode_command(CommandFrame(seq=11, command=command, payload="5000"))
        output = run(firmware, [wire])
        assert output == [f"ACCEPT 11 {_COMMAND_ORDINAL[command]} 5000"]

    def test_empty_payload_round_trips(self, firmware: Path) -> None:
        wire = encode_command(CommandFrame(seq=3, command=Command.PING, payload=""))
        assert run(firmware, [wire]) == ["ACCEPT 3 4 "]

    def test_payload_with_spaces_survives(self, firmware: Path) -> None:
        reason = "Kit non-compliant: missing Trauma Shears"
        wire = encode_command(
            CommandFrame(seq=4, command=Command.REJECT, payload=reason)
        )
        assert run(firmware, [wire]) == [f"ACCEPT 4 2 {reason}"]

    def test_maximum_sequence_number_is_accepted(self, firmware: Path) -> None:
        wire = encode_command(CommandFrame(seq=65535, command=Command.PING))
        assert run(firmware, [wire])[0].startswith("ACCEPT 65535")


class TestTheFirmwareRejectsWhatPythonWouldReject:
    def test_corrupted_payload_is_rejected_as_bad_crc(self, firmware: Path) -> None:
        wire = bytearray(encode_command(CommandFrame(3, Command.RELEASE, "5000")))
        wire[wire.index(ord("5"))] = ord("9")
        assert run(firmware, [bytes(wire)]) == ["REJECT BAD_CRC"]

    def test_unknown_command_is_rejected(self, firmware: Path) -> None:
        body = "RK1 3 LAUNCH x"
        wire = f"{body} {crc8(body.encode()):02X}\n".encode()
        assert run(firmware, [wire]) == ["REJECT BAD_FRAME"]

    def test_wrong_version_is_rejected(self, firmware: Path) -> None:
        body = "RK9 3 PING "
        wire = f"{body} {crc8(body.encode()):02X}\n".encode()
        assert run(firmware, [wire]) == ["REJECT BAD_FRAME"]

    def test_the_blueprints_unframed_command_is_rejected(
        self, firmware: Path
    ) -> None:
        """`PASS_KIT\\n` has no version, sequence, or checksum. The firmware
        must not honour it."""
        assert run(firmware, [b"PASS_KIT\n"]) == ["REJECT BAD_FRAME"]

    def test_line_noise_is_rejected(self, firmware: Path) -> None:
        for junk in (b"\n", b"garbage\n", b"RK1 x PING 00\n", b"   \n"):
            assert run(firmware, [junk])[0].startswith("REJECT")


class TestSequenceIndependence:
    def test_a_batch_of_frames_all_decode(self, firmware: Path) -> None:
        frames = [
            encode_command(CommandFrame(seq=i, command=Command.PING, payload=""))
            for i in range(1, 21)
        ]
        output = run(firmware, frames)
        assert len(output) == 20
        assert all(line.startswith("ACCEPT") for line in output)
