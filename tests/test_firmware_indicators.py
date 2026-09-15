"""What the board actually shows, asserted rather than eyeballed.

The panel is the only part of this system a person in front of the cabinet
can act on. "Red means locked" and "amber is not red" are therefore safety
claims and belong in tests, not in a comment next to a `digitalWrite`.

Compiles the real `firmware/mcu_actuator/indicators.h` and drives
`rkRenderPanel()` directly, so these assertions are about the code that gets
flashed.

Skipped when no C++ compiler is available.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "firmware" / "test" / "test_indicators.cpp"

OFF, PASS, FAIL, HOLD, STALE = 0, 1, 2, 3, 4
ENGAGED, RELEASED = 0, 1

ALL_INDICATORS = [OFF, PASS, FAIL, HOLD, STALE]

DARK, RED, GREEN, AMBER, BLUE = "---", "R--", "-G-", "RG-", "--B"

FAIL_BLINK_MS = 250
HOLD_PULSE_MS = 900
STALE_WIGWAG_MS = 600


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

    binary = tmp_path_factory.mktemp("firmware") / "rk_indicator_test"
    result = subprocess.run(
        [
            compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-O1",
            "-o", str(binary), str(HARNESS),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"indicator harness failed to compile:\n{result.stderr}")
    return binary


@dataclass(frozen=True, slots=True)
class Panel:
    latch: str
    verdict: str
    buzzer: bool


def render(
    firmware: Path,
    indicator: int,
    latch: int = ENGAGED,
    buzzer_armed: bool = False,
    now_ms: int = 0,
) -> Panel:
    line = f"{indicator} {latch} {int(buzzer_armed)} {now_ms}\n"
    result = subprocess.run(
        [str(firmware)], input=line.encode(), capture_output=True
    )
    assert result.returncode == 0, result.stderr
    words = result.stdout.decode().split()
    assert words[0] == "LATCH" and words[2] == "VERDICT" and words[4] == "BUZZER"
    return Panel(latch=words[1], verdict=words[3], buzzer=words[5] == "1")


class TestTheLatchLightNeverLies:
    """The one light a person acts on. It reports the lock, nothing else."""

    @pytest.mark.parametrize("indicator", ALL_INDICATORS)
    def test_engaged_reads_red_whatever_the_model_thinks(
        self, firmware: Path, indicator: int
    ) -> None:
        assert render(firmware, indicator, latch=ENGAGED).latch == RED

    @pytest.mark.parametrize("indicator", ALL_INDICATORS)
    def test_released_reads_green_whatever_the_model_thinks(
        self, firmware: Path, indicator: int
    ) -> None:
        assert render(firmware, indicator, latch=RELEASED).latch == GREEN

    @pytest.mark.parametrize("indicator", ALL_INDICATORS)
    @pytest.mark.parametrize("now", [0, 125, 250, 600, 900, 4_001])
    def test_it_never_goes_dark(
        self, firmware: Path, indicator: int, now: int
    ) -> None:
        """It does not blink, ever. A dark latch light is indistinguishable
        from a dead board, and "I don't know if it's locked" is the one thing
        this light is not allowed to say."""
        panel = render(firmware, indicator, latch=ENGAGED, now_ms=now)
        assert panel.latch == RED


class TestCannotSeeIsNotFailed:
    """The distinction the whole project turns on, at the point a human
    reads it."""

    def test_hold_is_amber_not_red(self, firmware: Path) -> None:
        assert render(firmware, HOLD).verdict == AMBER

    def test_fail_is_red(self, firmware: Path) -> None:
        assert render(firmware, FAIL).verdict == RED

    def test_an_unreadable_kit_still_shows_a_locked_latch(
        self, firmware: Path
    ) -> None:
        """Amber verdict, red latch: doubt did not open anything. This is the
        fail-closed rule, visible in one glance."""
        panel = render(firmware, HOLD, latch=ENGAGED)
        assert panel.verdict == AMBER
        assert panel.latch == RED

    def test_hold_is_silent(self, firmware: Path) -> None:
        """An alarm for "I cannot see" trains people to ignore the alarm."""
        for now in (0, 450, 900, 1_350):
            assert render(firmware, HOLD, buzzer_armed=True, now_ms=now).buzzer is False


class TestEveryStateIsDistinguishable:
    def test_lit_colours_are_pairwise_distinct(self, firmware: Path) -> None:
        """At the moment each state is lit, no two look alike - so a still
        photograph of the board is as readable as watching it, and the blink
        rate is a second cue rather than the only one."""
        lit = {
            state: render(firmware, state, now_ms=0).verdict
            for state in (PASS, FAIL, HOLD, STALE)
        }
        assert len(set(lit.values())) == len(lit), lit
        assert OFF not in lit
        assert render(firmware, OFF).verdict == DARK

    def test_stale_is_blue_and_not_an_alarm(self, firmware: Path) -> None:
        """The host being gone is not the kit being wrong."""
        panel = render(firmware, STALE, buzzer_armed=True)
        assert panel.verdict == BLUE
        assert panel.buzzer is False


class TestRates:
    def test_fail_blinks(self, firmware: Path) -> None:
        assert render(firmware, FAIL, now_ms=0).verdict == RED
        assert render(firmware, FAIL, now_ms=FAIL_BLINK_MS).verdict == DARK
        assert render(firmware, FAIL, now_ms=2 * FAIL_BLINK_MS).verdict == RED

    def test_hold_pulses(self, firmware: Path) -> None:
        assert render(firmware, HOLD, now_ms=0).verdict == AMBER
        assert render(firmware, HOLD, now_ms=HOLD_PULSE_MS).verdict == DARK

    def test_hold_is_slower_than_fail(self, firmware: Path) -> None:
        """Urgency is encoded in rate as well as colour, and they agree."""
        assert HOLD_PULSE_MS > FAIL_BLINK_MS

    def test_stale_wig_wags(self, firmware: Path) -> None:
        assert render(firmware, STALE, now_ms=0).verdict == BLUE
        assert render(firmware, STALE, now_ms=STALE_WIGWAG_MS).verdict == DARK

    def test_pass_is_steady(self, firmware: Path) -> None:
        """Nothing about a good kit is urgent."""
        for now in (0, 250, 600, 900, 10_000):
            assert render(firmware, PASS, now_ms=now).verdict == GREEN


class TestTheBuzzer:
    def test_it_sounds_only_for_fail(self, firmware: Path) -> None:
        for state in (OFF, PASS, HOLD, STALE):
            panel = render(firmware, state, buzzer_armed=True)
            assert panel.buzzer is False, state

    def test_it_stays_in_step_with_the_blink(self, firmware: Path) -> None:
        """A buzzer sounding while the light is dark is a panel contradicting
        itself."""
        for now in (0, FAIL_BLINK_MS, 2 * FAIL_BLINK_MS, 3 * FAIL_BLINK_MS):
            panel = render(firmware, FAIL, buzzer_armed=True, now_ms=now)
            assert panel.buzzer is (panel.verdict == RED)

    def test_disarmed_means_silent(self, firmware: Path) -> None:
        assert render(firmware, FAIL, buzzer_armed=False).buzzer is False
