"""The command surface, driven the way a person drives it.

The CLI was the least-covered module in the project and it is the only part
anyone actually touches. Everything below calls `main(argv)` in-process, so
these exercise the real parser, the real handlers and the real exit codes.

Exit codes are the contract, and they are three-valued for the same reason the
verdicts are:

    0  PASS            the kit is compliant
    1  a usage or setup error - nothing was inspected
    2  FAIL or INDETERMINATE - inspected, and the latch stayed shut
    3  the audit chain could not be verified
  130  interrupted

A script that treats "the latch stayed shut" as success would be exactly the
fail-open mistake this project exists to argue against, so these are pinned.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from readykit.cli import (
    _build_parser,
    _console_input,
    _describe_source,
    main,
)

REPO = Path(__file__).resolve().parent.parent
KIT = str(REPO / "manifests" / "trauma-kit-a.json")
TOOLBOX = str(REPO / "manifests" / "electrical-toolbox.json")


def run(*argv: str) -> int:
    return main(list(argv))


def inspect(*argv: str, manifest: str = KIT) -> int:
    """`readykit inspect` against a manifest, never touching the real log."""
    return main(["inspect", "--manifest", manifest, "--no-log", *argv])


class TestInspectExitCodes:
    def test_a_complete_kit_passes(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert inspect("--scene", "complete") == 0
        assert "PASS" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "scene",
        ["missing-shears", "expired", "short", "damaged-gauze"],
    )
    def test_a_non_compliant_kit_never_exits_zero(self, scene: str) -> None:
        assert inspect("--scene", scene) == 2

    @pytest.mark.parametrize(
        "scene",
        ["occluded", "garbled", "engine-fault", "low-confidence", "expiry-unreadable"],
    )
    def test_an_unreadable_kit_never_exits_zero(self, scene: str) -> None:
        """The whole argument, at the exit code. "I could not tell" must not
        be success to a shell script any more than it is to the latch."""
        assert inspect("--scene", scene) == 2

    def test_a_negated_phrasing_still_passes(self) -> None:
        """"no items are missing" is a pass, and the substring matcher this
        project replaced got it backwards."""
        assert inspect("--scene", "complete-negated") == 0

    def test_a_second_manifest_works(self) -> None:
        assert inspect("--scene", "complete", manifest=TOOLBOX) == 0

    def test_a_missing_manifest_is_a_setup_error_not_a_verdict(self) -> None:
        """Exit 1, not 2. Nothing was inspected, so there is no verdict to
        report, and conflating the two would put a phantom refusal in a
        script's logs."""
        assert inspect("--scene", "complete", manifest="/tmp/nope.json") == 1

    def test_a_malformed_manifest_is_reported(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        assert inspect("--scene", "complete", manifest=str(bad)) == 1

    def test_a_manifest_with_no_items_is_refused(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.json"
        empty.write_text(json.dumps({"manifest_id": "x", "name": "X", "items": []}))
        assert inspect("--scene", "complete", manifest=str(empty)) == 1


class TestEnginesRefuseTheWrongInput:
    @pytest.mark.parametrize("engine", ["geniex", "ollama"])
    def test_a_real_engine_refuses_a_scene_name(self, engine: str) -> None:
        """A real model pointed at a scene name would be looking at a string."""
        assert inspect("--engine", engine, "--scene", "complete") == 1

    def test_the_simulator_refuses_real_image_bytes(self, tmp_path: Path) -> None:
        """And fails closed while doing it - exit 2, latch shut."""
        image = tmp_path / "frame.jpg"
        image.write_bytes(b"\xff\xd8\xff" + b"\x00" * 64 + b"\xff\xd9")
        assert inspect("--image", str(image), "--engine", "simulated") == 2

    def test_an_absent_image_fails_closed(self) -> None:
        assert inspect(
            "--image", "/tmp/definitely-absent.jpg", "--engine", "simulated"
        ) == 2


class TestTheOtherCommands:
    def test_scenes_lists_them(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert run("scenes") == 0
        out = capsys.readouterr().out
        assert "complete" in out and "occluded" in out

    def test_compare_reports_the_divergence(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("compare", "--manifest", KIT) == 0
        assert "would have released the latch" in capsys.readouterr().out

    def test_watch_honours_its_limit(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert run(
            "watch", "--manifest", KIT, "--scene", "complete",
            "--limit", "2", "--no-log",
        ) == 0

    def test_sentinel_opens_on_a_good_kit(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(
            "sentinel", "--manifest", KIT, "--scene", "complete", "--limit", "3"
        ) == 0
        assert "release" in capsys.readouterr().out

    def test_sentinel_never_opens_on_an_unreadable_kit(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(
            "sentinel", "--manifest", KIT, "--scene", "occluded", "--limit", "5"
        ) == 0
        assert "release" not in capsys.readouterr().out

    def test_bench_reports_percentiles(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("bench", "--manifest", KIT, "--runs", "3") == 0
        assert "p50" in capsys.readouterr().out

    def test_demo_runs_the_sequence(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert run("demo", "--manifest", KIT, "--dwell", "0") == 0

    def test_doctor_reports(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = run("doctor")
        assert code in (0, 1)
        assert "Platform" in capsys.readouterr().out


class TestTheConsoleRefusesToBeServedRemotely:
    @pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.9", "::"])
    def test_a_routable_host_exits_two(
        self, host: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("console", "--manifest", KIT, "--host", host) == 2
        assert "refusing to bind" in capsys.readouterr().err


class TestAudit:
    def make_log(self, tmp_path: Path, scenes: list[str]) -> Path:
        log = tmp_path / "records.jsonl"
        for scene in scenes:
            run("inspect", "--manifest", KIT, "--scene", scene, "--log", str(log))
        return log

    def test_an_intact_chain_verifies(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        log = self.make_log(tmp_path, ["complete", "missing-shears"])
        assert run("audit", "--log", str(log)) == 0
        assert "chain intact" in capsys.readouterr().out

    def test_an_edited_record_breaks_the_chain(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The cover-up this defends against: turning a recorded FAIL into a
        PASS after the fact."""
        log = self.make_log(tmp_path, ["complete", "missing-shears", "complete"])
        lines = log.read_text().splitlines()
        assert '"verdict":"fail"' in lines[1]
        lines[1] = lines[1].replace('"verdict":"fail"', '"verdict":"pass"')
        log.write_text("\n".join(lines) + "\n")

        assert run("audit", "--log", str(log)) == 3
        assert "CHAIN BROKEN" in capsys.readouterr().out

    def test_a_deleted_record_breaks_the_chain(self, tmp_path: Path) -> None:
        log = self.make_log(tmp_path, ["complete", "missing-shears", "complete"])
        lines = log.read_text().splitlines()
        log.write_text("\n".join(lines[:1] + lines[2:]) + "\n")
        assert run("audit", "--log", str(log)) == 3

    def test_reordered_records_break_the_chain(self, tmp_path: Path) -> None:
        log = self.make_log(tmp_path, ["complete", "missing-shears", "complete"])
        lines = log.read_text().splitlines()
        log.write_text("\n".join([lines[1], lines[0], lines[2]]) + "\n")
        assert run("audit", "--log", str(log)) == 3

    def test_a_deleted_log_is_not_success(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Removing the evidence wholesale must not report a clean bill of
        health - the claim is that this detects deletion."""
        assert run("audit", "--log", str(tmp_path / "never-existed.jsonl")) == 3
        assert "no audit log at all" in capsys.readouterr().out

    def test_a_truncated_final_line_is_not_tampering(self, tmp_path: Path) -> None:
        """Power loss mid-write. Distinct from an edit, and reported as such."""
        log = self.make_log(tmp_path, ["complete", "complete"])
        text = log.read_text()
        log.write_text(text[: len(text) - 20])
        assert run("audit", "--log", str(log)) == 3

    def test_records_prints_recent_inspections(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        log = self.make_log(tmp_path, ["complete", "missing-shears"])
        assert run("records", "--log", str(log)) == 0
        assert "pass" in capsys.readouterr().out.lower()


class TestTheConsoleWiring:
    """`readykit console` had no way to look at anything real. These cover the
    argument wiring, which is the part someone actually types - the console's
    own behaviour once wired is in tests/test_console.py.
    """

    def test_a_camera_without_a_real_model_is_refused(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The simulated engine answers from a scene name and never looks at
        the frame. Refused up front, not one button press later."""
        assert run("console", "--manifest", KIT, "--camera", "0") == 1
        assert "a real camera needs a real model" in capsys.readouterr().err

    def test_a_real_model_without_frames_is_refused(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Already the rule for `inspect`; the console must not be the one
        door where a real model gets handed a scene name."""
        assert run("console", "--manifest", KIT, "--engine", "ollama") == 1
        assert "needs real frames" in capsys.readouterr().err

    def test_the_scripted_default_still_needs_nothing(self) -> None:
        """A bare `console` must keep working with no camera and no model, or
        every existing demo breaks. Parsed and wired, not served."""
        args = _build_parser().parse_args(["console", "--manifest", KIT])
        assert _console_input(args) == (None, None, "", "")

    def test_describing_the_source_names_the_device(self) -> None:
        parser = _build_parser()
        camera = parser.parse_args(
            ["console", "--manifest", KIT, "--camera", "2"]
        )
        assert _describe_source(camera) == "camera 2 via OpenCV"

        ffmpeg = parser.parse_args(
            ["console", "--manifest", KIT, "--ffmpeg-camera", "0"]
        )
        assert _describe_source(ffmpeg) == "camera 0 via ffmpeg"

        images = parser.parse_args(
            ["console", "--manifest", KIT, "--image", "a.jpg", "b.jpg"]
        )
        assert _describe_source(images) == "2 still images"

        one = parser.parse_args(
            ["console", "--manifest", KIT, "--image", "/long/path/to/tray.jpg"]
        )
        assert _describe_source(one) == "still image tray.jpg"

    def test_browser_camera_needs_a_real_model(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run("console", "--manifest", KIT, "--browser-camera") == 1
        assert "needs a real model" in capsys.readouterr().err

    def test_browser_camera_does_not_mix_with_a_host_camera(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(
            "console", "--manifest", KIT, "--browser-camera",
            "--camera", "0", "--engine", "ollama",
        ) == 1
        assert "drop --camera" in capsys.readouterr().err
