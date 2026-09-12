"""The operator console's HTTP surface.

The console shows an operator whether an enclosure is open. The tests that
matter are the ones about it telling the truth - particularly that it never
reports a latch as released on the strength of a verdict alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from readykit.bridge import LoopbackLink, VirtualActuatorNode
from readykit.domain import Manifest, RequiredItem

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
