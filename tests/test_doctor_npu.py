"""The NPU probe, which only ever runs on a machine we do not have.

"Is it on the NPU" is the question this whole track is about, and `doctor` is
what answers it before anything else is tried. The probe is Windows-only, so
it could not run on any development machine and had no tests at all - which
means the first time it ever executed would have been on the hackathon floor.

Windows and the PowerShell call are faked so every branch runs here.

The distinction being pinned throughout: **an NPU that is present and OK** is
not the same as **one Windows will not talk about**, and neither is the same
as **a probe that failed**. Reporting any of those three as another is how you
end up quietly on the CPU with latency numbers that mean nothing.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from readykit.doctor import Status, _npu


class FakeResult:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


@pytest.fixture
def on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("readykit.doctor.os.name", "nt")


def with_output(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    monkeypatch.setattr(
        "readykit.doctor.subprocess.run", lambda *a, **k: FakeResult(stdout)
    )


class TestOffWindows:
    def test_it_says_it_did_not_check_rather_than_that_there_is_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Absence of evidence is not evidence of absence, here as everywhere
        else in this project."""
        monkeypatch.setattr("readykit.doctor.os.name", "posix")
        check = _npu()
        assert check.status is Status.INFO
        assert "Windows-only" in check.detail


class TestOnWindows:
    def test_a_healthy_npu_is_reported_ok(
        self, on_windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with_output(monkeypatch, "OK|Snapdragon(R) X Elite - Hexagon(TM) NPU\n")
        check = _npu()
        assert check.status is Status.OK
        assert "Hexagon" in check.detail or any(
            "Hexagon" in item for item in check.items
        )

    def test_nothing_reported_is_a_warning_not_a_pass(
        self, on_windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The silicon is on the die of every X Elite, so "Windows lists
        nothing" means a missing driver, not missing hardware - and it must
        never read as a clean result."""
        with_output(monkeypatch, "")
        check = _npu()
        assert check.status is Status.WARN
        assert "driver" in check.remedy.lower()

    def test_a_degraded_device_is_not_reported_as_ok(
        self, on_windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A device that is present but in error would still be listed. It
        cannot be used, so it must not read as usable."""
        with_output(monkeypatch, "Error|Hexagon(TM) NPU\n")
        assert _npu().status is not Status.OK

    def test_one_healthy_among_several_is_still_reported_honestly(
        self, on_windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with_output(
            monkeypatch, "OK|Hexagon(TM) NPU\nUnknown|Intel(R) AI Boost\n"
        )
        check = _npu()
        assert check.status in (Status.OK, Status.WARN)

    def test_blank_lines_are_ignored(
        self, on_windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with_output(monkeypatch, "\n\n   \n")
        assert _npu().status is Status.WARN

    @pytest.mark.parametrize(
        "failure",
        [
            OSError("powershell not found"),
            subprocess.TimeoutExpired(cmd="powershell", timeout=25),
            subprocess.SubprocessError("broke"),
        ],
    )
    def test_a_failed_probe_is_distinct_from_a_negative_result(
        self,
        on_windows: None,
        monkeypatch: pytest.MonkeyPatch,
        failure: Exception,
    ) -> None:
        """"I could not ask" and "the answer was no" are different, and only
        one of them is about the hardware."""

        def boom(*a: Any, **k: Any) -> Any:
            raise failure

        monkeypatch.setattr("readykit.doctor.subprocess.run", boom)
        check = _npu()
        assert check.status is Status.WARN
        assert "could not query" in check.detail

    def test_the_probe_never_raises(
        self, on_windows: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """doctor has to finish and print the rest of its report even when one
        check cannot run - it is the thing you reach for when something is
        already broken."""

        def boom(*a: Any, **k: Any) -> Any:
            raise OSError("no powershell")

        monkeypatch.setattr("readykit.doctor.subprocess.run", boom)
        assert _npu() is not None
