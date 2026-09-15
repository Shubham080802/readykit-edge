"""Environment checks.

`readykit doctor` runs when someone is standing at unfamiliar hardware with
limited bench time, so its job is to be right and specific. A check that says
"something is wrong" without naming the fix has wasted the trip.

These tests pin the two properties that matter: it never raises, whatever the
machine looks like, and a missing optional dependency is not reported as a
blocking failure.
"""

from __future__ import annotations

import os
from pathlib import Path

from readykit.doctor import Check, Status, run_checks, venv_prefix, worst


class TestItNeverRaises:
    def test_running_all_checks_succeeds(self, tmp_path: Path) -> None:
        """Whatever is or is not installed, doctor reports rather than blows
        up. It is the tool you reach for when things are already broken."""
        cwd = Path.cwd()
        os.chdir(tmp_path)
        try:
            checks = run_checks()
        finally:
            os.chdir(cwd)
        assert checks
        assert all(isinstance(c, Check) for c in checks)

    def test_every_check_has_a_name_and_detail(self) -> None:
        for check in run_checks():
            assert check.name
            assert check.detail

    def test_anything_not_ok_names_a_remedy(self) -> None:
        """A finding with no fix attached is a finding that wastes time."""
        for check in run_checks():
            if check.status in (Status.FAIL, Status.WARN):
                assert check.remedy, f"{check.name} reports {check.status} with no remedy"


class TestSeverity:
    def test_worst_reports_fail_over_warn(self) -> None:
        checks = [
            Check("a", Status.OK, "x"),
            Check("b", Status.WARN, "y", "fix"),
            Check("c", Status.FAIL, "z", "fix"),
        ]
        assert worst(checks) is Status.FAIL

    def test_worst_reports_warn_over_ok(self) -> None:
        checks = [Check("a", Status.OK, "x"), Check("b", Status.WARN, "y", "fix")]
        assert worst(checks) is Status.WARN

    def test_all_ok_is_ok(self) -> None:
        assert worst([Check("a", Status.OK, "x")]) is Status.OK

    def test_info_does_not_count_against_the_run(self) -> None:
        """Running on a laptop rather than a Snapdragon host is worth saying,
        but it is not a problem - simulation is the supported path there."""
        assert worst([Check("a", Status.INFO, "x")]) is Status.OK


class TestTheConsoleIsOptional:
    def test_a_missing_console_extra_is_not_blocking(self) -> None:
        """You can run the whole inspection pipeline without the console. Only
        GenieX and capture are on the critical path."""
        by_name = {c.name: c for c in run_checks()}
        if "fastapi" in by_name and by_name["fastapi"].status is not Status.OK:
            assert by_name["fastapi"].status is Status.WARN


class TestPlatformAwareness:
    def test_the_command_prefix_matches_the_platform(self) -> None:
        """Every command in the docs is written POSIX-first. On a Windows
        ARM64 Snapdragon host they are all wrong in the same small way, so
        doctor prints the form that actually works here."""
        prefix = venv_prefix()
        if os.name == "nt":
            assert prefix == r".venv\Scripts\readykit"
        else:
            assert prefix == ".venv/bin/readykit"


class TestManifestChecking:
    def test_a_broken_manifest_is_a_blocking_failure(self, tmp_path: Path) -> None:
        """A manifest that will not parse is a hard error at startup, so it is
        better found now than during the demo."""
        (tmp_path / "manifests").mkdir()
        (tmp_path / "manifests" / "broken.json").write_text("{not json", encoding="utf-8")

        cwd = Path.cwd()
        os.chdir(tmp_path)
        try:
            checks = {c.name: c for c in run_checks()}
        finally:
            os.chdir(cwd)

        assert checks["Manifests"].status is Status.FAIL
        assert "broken.json" in " ".join(checks["Manifests"].items)

    def test_valid_manifests_report_their_item_counts(self, tmp_path: Path) -> None:
        (tmp_path / "manifests").mkdir()
        (tmp_path / "manifests" / "ok.json").write_text(
            '{"manifest_id":"k","name":"K","items":[{"key":"a","label":"A"}]}',
            encoding="utf-8",
        )
        cwd = Path.cwd()
        os.chdir(tmp_path)
        try:
            checks = {c.name: c for c in run_checks()}
        finally:
            os.chdir(cwd)

        assert checks["Manifests"].status is Status.OK
        assert "1 items" in " ".join(checks["Manifests"].items)
