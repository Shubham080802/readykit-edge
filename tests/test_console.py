"""The operator console's HTTP surface.

The console shows an operator whether an enclosure is open. The tests that
matter are the ones about it telling the truth - particularly that it never
reports a latch as released on the strength of a verdict alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from readykit.bridge import LoopbackLink, VirtualActuatorNode
from readykit.capture import Frame
from readykit.domain import Manifest, RequiredItem
from readykit.inference.base import Observation
from readykit.reply import parse_reply

# importorskip only catches ImportError, and starlette raises RuntimeError
# when httpx is absent - so both have to be handled to skip cleanly rather
# than fail collection on a machine without the console extras.
try:
    from fastapi import testclient as fastapi_testclient
except (ImportError, RuntimeError) as exc:  # pragma: no cover
    pytest.skip(
        f"console test client unavailable: {exc}", allow_module_level=True
    )

from readykit.console import create_app

KIT = Manifest(
    manifest_id="console-demo",
    name="Console Demo Kit",
    items=(
        RequiredItem(key="multimeter", label="Multimeter"),
        RequiredItem(key="hardhat", label="Hard Hat"),
    ),
    hold_seconds=2.0,
)


@pytest.fixture
def node() -> VirtualActuatorNode:
    return VirtualActuatorNode()


@pytest.fixture
def client(
    tmp_path: Path, node: VirtualActuatorNode
) -> fastapi_testclient.TestClient:
    app = create_app(
        manifest=KIT,
        log_path=tmp_path / "inspections.jsonl",
        link=LoopbackLink(node),
    )
    return fastapi_testclient.TestClient(app)


class _CountingCamera:
    """Stands in for a camera, and remembers how often it was opened."""

    opens = 0

    def __init__(self) -> None:
        type(self).opens += 1
        self.reads = 0

    def read(self) -> Frame:
        self.reads += 1
        return Frame(image=b"\x00pixels", digest="cafe1234", width=640, height=480)

    def close(self) -> None:
        pass


class _FixedEngine:
    """A real-shaped engine: it reads the frame, not a scene name."""

    name = "stub-vlm"

    def infer(self, frame: Frame, manifest: Manifest) -> Observation:
        assert not isinstance(frame.image, str), "a real engine gets pixels"
        reply = (
            '{"kit_present":"yes","items":['
            '{"key":"multimeter","presence":"found","confidence":0.94},'
            '{"key":"hardhat","presence":"found","confidence":0.91}]}'
        )
        return Observation(
            sightings=tuple(parse_reply(reply, manifest)), raw_reply=reply
        )

    def close(self) -> None:
        pass


@pytest.fixture
def live_client(
    tmp_path: Path, node: VirtualActuatorNode
) -> fastapi_testclient.TestClient:
    """A console wired to real frames and a real-shaped model."""
    _CountingCamera.opens = 0
    camera = _CountingCamera()
    engine = _FixedEngine()
    app = create_app(
        manifest=KIT,
        log_path=tmp_path / "inspections.jsonl",
        link=LoopbackLink(node),
        source_factory=lambda: camera,
        engine_factory=lambda: engine,
        source_label="camera 0 via OpenCV",
        engine_label="stub-vlm",
    )
    return fastapi_testclient.TestClient(app)


@pytest.fixture
def demo_camera_client(
    tmp_path: Path, node: VirtualActuatorNode
) -> fastapi_testclient.TestClient:
    app = create_app(
        manifest=KIT,
        log_path=tmp_path / "inspections.jsonl",
        link=LoopbackLink(node),
        demo_camera=True,
    )
    return fastapi_testclient.TestClient(app)


class TestAConsoleWiredToARealCamera:
    """The console had no way to inspect anything real: `create_app` took
    `source_factory` and `engine_factory`, and the CLI never passed either, so
    the page was permanently a rehearsal.

    Wiring them in makes one thing load-bearing - the page must not offer a
    scene picker in front of a live camera. An operator who reads a dropdown
    of scene names believes the input is scripted, and would see a verdict
    about their actual kit under the name of a rehearsal.
    """

    def test_a_scripted_console_says_it_is_scripted(
        self, client: fastapi_testclient.TestClient
    ) -> None:
        body = client.get("/api/source").json()
        assert body["live"] is False
        assert body["source"] == "scripted scenes"

    def test_a_live_console_names_what_it_is_looking_at(
        self, live_client: fastapi_testclient.TestClient
    ) -> None:
        body = live_client.get("/api/source").json()
        assert body["live"] is True
        assert body["source"] == "camera 0 via OpenCV"
        assert body["engine"] == "stub-vlm"

    def test_a_live_console_offers_no_scenes(
        self, live_client: fastapi_testclient.TestClient
    ) -> None:
        """Nothing to pick, so nothing is listed - rather than listing scenes
        that cannot be honoured."""
        assert live_client.get("/api/scenes").json()["scenes"] == []

    def test_a_live_console_refuses_a_scene_rather_than_ignoring_it(
        self, live_client: fastapi_testclient.TestClient
    ) -> None:
        """Silently looking at the camera instead would report a verdict about
        one thing under the name of another."""
        response = live_client.post("/api/inspect", json={"scene": "complete"})
        assert response.status_code == 400
        assert "camera 0 via OpenCV" in response.json()["detail"]

    def test_a_live_console_inspects_the_real_frame(
        self, live_client: fastapi_testclient.TestClient
    ) -> None:
        body = live_client.post("/api/inspect", json={}).json()
        assert body["verdict"] == "pass"
        assert body["frame_digest"] == "cafe1234"

    def test_the_camera_is_opened_once_however_many_inspections_run(
        self, live_client: fastapi_testclient.TestClient
    ) -> None:
        """`create_app` calls the factory per inspection. Reopening the device
        every time means the console competing with itself for a handle it
        already holds, and on some drivers the second open simply fails."""
        for _ in range(4):
            assert live_client.post("/api/inspect", json={}).status_code == 200
        assert _CountingCamera.opens == 1

    def test_a_scripted_console_still_takes_a_scene(
        self, client: fastapi_testclient.TestClient
    ) -> None:
        """The default path is untouched."""
        body = client.post("/api/inspect", json={"scene": "empty"}).json()
        assert body["verdict"] == "fail"
        assert client.get("/api/scenes").json()["scenes"]


class TestDemoCamera:
    def test_the_demo_feed_is_explicit_and_keeps_scripted_scenes(
        self, demo_camera_client: fastapi_testclient.TestClient
    ) -> None:
        source = demo_camera_client.get("/api/source").json()
        assert source["live"] is False
        assert source["browser_camera"] is False
        assert source["demo_camera"] is True
        assert source["source"] == "simulated camera feed"
        assert demo_camera_client.get("/api/scenes").json()["scenes"]


class TestSeeingWhatTheCameraSees:
    """An operator aiming a camera needs to see what it sees, and a verdict is
    only checkable if you can see the picture it was decided on."""

    def test_the_live_view_serves_the_camera_frame(
        self, live_client: fastapi_testclient.TestClient
    ) -> None:
        response = live_client.get("/api/camera.jpg")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.content == b"\x00pixels"
        assert "no-store" in response.headers["cache-control"]

    def test_a_scripted_console_has_no_camera_to_show(
        self, client: fastapi_testclient.TestClient
    ) -> None:
        """A scene is a name, not a picture. Showing a placeholder image would
        suggest a camera that is not there."""
        assert client.get("/api/camera.jpg").status_code == 404

    def test_there_is_no_judged_frame_before_an_inspection(
        self, live_client: fastapi_testclient.TestClient
    ) -> None:
        assert live_client.get("/api/inspected.jpg").status_code == 404

    def test_the_judged_frame_is_the_one_the_verdict_was_decided_on(
        self, live_client: fastapi_testclient.TestClient
    ) -> None:
        body = live_client.post("/api/inspect", json={}).json()
        response = live_client.get("/api/inspected.jpg")
        assert response.status_code == 200
        assert response.content == b"\x00pixels"
        assert body["frame_digest"] == "cafe1234"

    def test_looking_through_the_live_view_is_not_an_inspection(
        self, live_client: fastapi_testclient.TestClient
    ) -> None:
        """Aiming the camera must not produce a "judged" picture that no model
        ever judged."""
        for _ in range(3):
            live_client.get("/api/camera.jpg")
        assert live_client.get("/api/inspected.jpg").status_code == 404

    def test_the_camera_is_still_opened_once_with_the_live_view_running(
        self, live_client: fastapi_testclient.TestClient
    ) -> None:
        for _ in range(3):
            live_client.get("/api/camera.jpg")
            live_client.post("/api/inspect", json={})
        assert _CountingCamera.opens == 1


class _NoKitEngine(_FixedEngine):
    """Recognises the multimeter, but says no kit is in view."""

    def infer(self, frame: Frame, manifest: Manifest) -> Observation:
        reply = (
            '{"kit_present":"no","items":['
            '{"key":"multimeter","presence":"found","confidence":1.0}]}'
        )
        return Observation(
            sightings=tuple(parse_reply(reply, manifest)), raw_reply=reply
        )


class TestExplainingASetAsideReading:
    def test_the_models_words_are_shown_but_not_counted(
        self, tmp_path: Path, node: VirtualActuatorNode
    ) -> None:
        camera = _CountingCamera()
        app = create_app(
            manifest=KIT,
            log_path=tmp_path / "inspections.jsonl",
            link=LoopbackLink(node),
            source_factory=lambda: camera,
            engine_factory=_NoKitEngine,
            source_label="camera 0 via OpenCV",
            engine_label="stub-vlm",
        )
        body = fastapi_testclient.TestClient(app).post("/api/inspect", json={}).json()
        items = {item["key"]: item for item in body["items"]}

        assert body["verdict"] == "indeterminate"
        assert body["kit_absent"] is True
        assert items["multimeter"]["presence"] == "unreadable"
        assert items["multimeter"]["model_said"] == "found"
        assert items["hardhat"]["model_said"] is None

    def test_nothing_is_marked_set_aside_when_the_kit_is_in_view(
        self, live_client: fastapi_testclient.TestClient
    ) -> None:
        body = live_client.post("/api/inspect", json={}).json()
        assert body["kit_absent"] is False
        assert all(item["model_said"] is None for item in body["items"])


JPEG = b"\xff\xd8\xff\xe0" + b"frame-bytes"


class _BytesEngine(_FixedEngine):
    """Checks it was handed exactly the bytes the browser sent."""

    def infer(self, frame: Frame, manifest: Manifest) -> Observation:
        assert frame.image == JPEG
        return super().infer(Frame(image=b"x", digest="d"), manifest)


@pytest.fixture
def browser_client(
    tmp_path: Path, node: VirtualActuatorNode
) -> fastapi_testclient.TestClient:
    app = create_app(
        manifest=KIT,
        log_path=tmp_path / "inspections.jsonl",
        link=LoopbackLink(node),
        engine_factory=_BytesEngine,
        source_label="browser camera",
        engine_label="stub-vlm",
        browser_camera=True,
    )
    return fastapi_testclient.TestClient(app)


class TestTheBrowserOpensTheCamera:
    """Opening the camera from the host needs camera permission for whatever
    launched the server - on a Mac, a Terminal someone had to open by hand
    every time. The browser already holds that permission, so it captures the
    frame and posts it, and the host never touches a camera."""

    def test_the_page_is_told_to_use_its_own_camera(
        self, browser_client: fastapi_testclient.TestClient
    ) -> None:
        body = browser_client.get("/api/source").json()
        assert body["live"] is True
        assert body["browser_camera"] is True

    def test_a_posted_frame_is_inspected_and_shown_as_judged(
        self, browser_client: fastapi_testclient.TestClient
    ) -> None:
        response = browser_client.post(
            "/api/inspect-frame", content=JPEG, headers={"Content-Type": "image/jpeg"}
        )
        assert response.status_code == 200
        assert response.json()["verdict"] == "pass"
        assert browser_client.get("/api/inspected.jpg").content == JPEG

    def test_the_host_camera_endpoint_has_nothing_to_show(
        self, browser_client: fastapi_testclient.TestClient
    ) -> None:
        assert browser_client.get("/api/camera.jpg").status_code == 404

    def test_an_inspection_without_a_frame_is_refused(
        self, browser_client: fastapi_testclient.TestClient
    ) -> None:
        """Nothing was captured, so there is nothing to judge - not a verdict
        about an empty picture."""
        assert browser_client.post("/api/inspect", json={}).status_code == 400
        assert browser_client.post("/api/inspect-frame", content=b"").status_code == 400

    def test_something_that_is_not_an_image_is_refused(
        self, browser_client: fastapi_testclient.TestClient
    ) -> None:
        response = browser_client.post("/api/inspect-frame", content=b"not a picture")
        assert response.status_code == 415

    def test_a_console_without_the_flag_takes_no_posted_frames(
        self, live_client: fastapi_testclient.TestClient
    ) -> None:
        """A console reading its own camera must not also accept pictures from
        anything that can reach it."""
        response = live_client.post("/api/inspect-frame", content=JPEG)
        assert response.status_code == 404


class TestStaticSurface:
    def test_index_is_served(
        self, client: fastapi_testclient.TestClient
    ) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert "ReadyKit Edge" in response.text

    def test_stylesheet_and_script_are_served(
        self, client: fastapi_testclient.TestClient
    ) -> None:
        for path in ("/static/console.css", "/static/console.js"):
            assert client.get(path).status_code == 200


class TestManifestAndScenes:
    def test_manifest_lists_every_required_item(
        self, client: fastapi_testclient.TestClient
    ) -> None:
        payload = client.get("/api/manifest").json()
        assert [item["key"] for item in payload["items"]] == list(KIT.keys)

    def test_scenes_include_a_knockout_for_each_item(
        self, client: fastapi_testclient.TestClient
    ) -> None:
        names = {s["name"] for s in client.get("/api/scenes").json()["scenes"]}
        assert "occluded" in names
        for key in KIT.keys:
            assert f"missing-{key}" in names


class TestInspecting:
    def test_complete_scene_passes_and_releases(
        self, client: fastapi_testclient.TestClient, node: VirtualActuatorNode
    ) -> None:
        payload = client.post("/api/inspect", json={"scene": "complete"}).json()
        assert payload["verdict"] == "pass"
        assert payload["latch_expected"] == "released"
        assert node.latch.value == "released"

    def test_occluded_scene_is_indeterminate_and_stays_shut(
        self, client: fastapi_testclient.TestClient, node: VirtualActuatorNode
    ) -> None:
        payload = client.post("/api/inspect", json={"scene": "occluded"}).json()
        assert payload["verdict"] == "indeterminate"
        assert payload["latch_expected"] == "engaged"
        assert node.latch.value == "engaged"

    def test_missing_item_is_blamed_in_the_payload(
        self, client: fastapi_testclient.TestClient
    ) -> None:
        payload = client.post(
            "/api/inspect", json={"scene": "missing-hardhat"}
        ).json()
        assert payload["verdict"] == "fail"
        blamed = [i["key"] for i in payload["items"] if i["blamed"]]
        assert blamed == ["hardhat"]

    def test_every_manifest_item_appears_even_when_unreported(
        self, client: fastapi_testclient.TestClient
    ) -> None:
        """The checklist must never silently drop a required item - a row that
        is missing from the screen reads as one that was checked."""
        payload = client.post("/api/inspect", json={"scene": "garbled"}).json()
        assert [i["key"] for i in payload["items"]] == list(KIT.keys)
        assert all(i["unresolved"] for i in payload["items"])

    def test_an_unknown_scene_is_indeterminate_not_a_500(
        self, client: fastapi_testclient.TestClient
    ) -> None:
        payload = client.post("/api/inspect", json={"scene": "nonsense"}).json()
        assert payload["verdict"] == "indeterminate"

    def test_a_non_string_scene_is_rejected(
        self, client: fastapi_testclient.TestClient
    ) -> None:
        assert client.post("/api/inspect", json={"scene": 7}).status_code == 400


class TestStateReporting:
    def test_state_reports_live_latch_and_link(
        self, client: fastapi_testclient.TestClient
    ) -> None:
        client.post("/api/inspect", json={"scene": "complete"})
        telemetry = client.get("/api/state").json()["telemetry"]
        assert telemetry["available"] is True
        assert telemetry["latch"] == "released"

    def test_polling_keeps_the_link_fresh(
        self, client: fastapi_testclient.TestClient
    ) -> None:
        """The console process is the Inspection Host. If polling did not
        heartbeat, the watchdog would trip and the console would report a dead
        link while sitting right there talking to the node."""
        client.post("/api/inspect", json={"scene": "complete"})
        for _ in range(3):
            telemetry = client.get("/api/state").json()["telemetry"]
        assert telemetry["link_stale"] is False

    def test_tally_counts_each_verdict(
        self, client: fastapi_testclient.TestClient
    ) -> None:
        client.post("/api/inspect", json={"scene": "complete"})
        client.post("/api/inspect", json={"scene": "missing-hardhat"})
        client.post("/api/inspect", json={"scene": "occluded"})
        tally = client.get("/api/state").json()["tally"]
        assert tally == {"pass": 1, "fail": 1, "indeterminate": 1}


class TestRecords:
    def test_records_are_returned_most_recent_first(
        self, client: fastapi_testclient.TestClient
    ) -> None:
        client.post("/api/inspect", json={"scene": "complete"})
        client.post("/api/inspect", json={"scene": "missing-hardhat"})
        rows = client.get("/api/records").json()["records"]
        assert [row["verdict"] for row in rows] == ["fail", "pass"]

    def test_records_carry_what_was_actually_commanded(
        self, client: fastapi_testclient.TestClient
    ) -> None:
        client.post("/api/inspect", json={"scene": "complete"})
        row = client.get("/api/records").json()["records"][0]
        assert row["commanded"].startswith("RELEASE")
        assert "ack=OK" in row["commanded"]

    def test_record_limit_is_clamped(
        self, client: fastapi_testclient.TestClient
    ) -> None:
        client.post("/api/inspect", json={"scene": "complete"})
        assert client.get("/api/records?limit=99999").status_code == 200
