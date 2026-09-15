"""The sketch compiles - under every pin configuration it can land in.

The failure this prevents is specific and expensive: arriving at a bench with
one Arduino UNO Q, no spares and a queue, and finding the sketch does not
build because a core spells its onboard LED macros differently than expected.

`mcu_actuator.ino` guards those names, which means a plain compile only ever
reads one branch of the guards and silently skips the rest. So compile it
several times with different macros defined and make the compiler read all of
them, with warnings as errors.

What this does not prove: anything about the real STM32U585 toolchain, or
whether the UNO Q's core actually defines LEDR. Only flashing settles those.
This catches the typo, not the target.

Skipped when no C++ compiler is available.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "firmware" / "test" / "test_sketch_builds.cpp"
STUB_DIR = REPO / "firmware" / "test"

# Each case is (name, extra -D flags). Between them every branch of the
# preprocessor guards in the sketch gets compiled at least once.
PIN_CONFIGURATIONS = [
    pytest.param([], id="bare-board-no-onboard-macros"),
    pytest.param(["-DLED_BUILTIN=13"], id="monochrome-builtin-only"),
    pytest.param(
        ["-DLED_BUILTIN=13", "-DLEDR=20", "-DLEDG=21", "-DLEDB=22"],
        id="rgb-LEDR-spelling",
    ),
    pytest.param(
        ["-DLED_BUILTIN=13", "-DLED_RED=20", "-DLED_GREEN=21", "-DLED_BLUE=22"],
        id="rgb-LED_RED-spelling",
    ),
    pytest.param(
        ["-DRK_VERDICT_R=20", "-DRK_VERDICT_G=21", "-DRK_VERDICT_B=22"],
        id="hand-pinned-override",
    ),
]


def _compiler() -> str | None:
    for candidate in ("c++", "g++", "clang++"):
        if shutil.which(candidate):
            return candidate
    return None


@pytest.mark.parametrize("flags", PIN_CONFIGURATIONS)
def test_the_sketch_builds(tmp_path: Path, flags: list[str]) -> None:
    compiler = _compiler()
    if compiler is None:
        pytest.skip("no C++ compiler available to build the firmware harness")

    binary = tmp_path / "rk_sketch"
    result = subprocess.run(
        [
            compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-O1",
            "-I", str(STUB_DIR), *flags, "-o", str(binary), str(HARNESS),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"sketch failed to compile with {flags or 'no extra flags'}:\n"
        f"{result.stderr}"
    )

    ran = subprocess.run([str(binary)], capture_output=True, text=True)
    assert ran.returncode == 0, ran.stderr
    assert "sketch built and ran" in ran.stdout
