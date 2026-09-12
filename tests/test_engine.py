"""The inspection loop, end to end over fakes.

The theme: every way this pipeline can break must end with the latch shut and
the Inspection Record saying honestly what happened.
"""

from __future__ import annotations

import json

import pytest

from readykit.bridge import LoopbackLink, VirtualActuatorNode
from readykit.bridge.loopback import LatchState
from readykit.capture import CaptureError, Frame, FrameSource, ScriptedSource
from readykit.domain import Manifest, RequiredItem, Sighting, Verdict
from readykit.engine import InspectionEngine
from readykit.inference.base import InferenceEngine, InferenceError
from readykit.inference.simulated import SimulatedEngine

KIT = Manifest(
    manifest_id="demo",
    name="Demo Kit",
    items=(
        RequiredItem(key="multimeter", label="Multimeter"),
        RequiredItem(key="hardhat", label="Hard Hat"),
    ),
    hold_seconds=3.0,
)


def build(scene: str, node: VirtualActuatorNode | None = None) -> InspectionEngine:
    return InspectionEngine(
        manifest=KIT,
        source=ScriptedSource(scene),
        engine=SimulatedEngine(seed=7),
        link=LoopbackLink(node) if node is not None else None,
    )


class BrokenSource(FrameSource):
    def read(self) -> Frame:
        raise CaptureError("camera disconnected")


class ExplodingEngine(InferenceEngine):
    name = "exploding"

    def infer(self, frame: Frame, manifest: Manifest) -> list[Sighting]:
        raise ZeroDivisionError("something nobody anticipated")


class TestHappyPath:
    def test_complete_kit_passes_and_releases_the_latch(self) -> None:
        node = VirtualActuatorNode()
        outcome = build("complete", node).run_once()
        assert outcome.verdict is Verdict.PASS
        assert outcome.enacted
        assert node.latch is LatchState.RELEASED

    def test_the_release_carries_the_manifests_hold(self) -> None:
        node = VirtualActuatorNode()
        build("complete", node).run_once()
        assert "released for 3000ms" in node.events


class TestFailurePaths:
    def test_missing_item_fails_and_leaves_the_latch_shut(self) -> None:
        node = VirtualActuatorNode()
        outcome = build("missing-hardhat", node).run_once()
        assert outcome.verdict is Verdict.FAIL
        assert node.latch is LatchState.ENGAGED

    def test_occluded_scene_is_indeterminate_not_pass(self) -> None:
        node = VirtualActuatorNode()
        outcome = build("occluded", node).run_once()
        assert outcome.verdict is Verdict.INDETERMINATE
        assert node.latch is LatchState.ENGAGED

    def test_low_confidence_is_indeterminate(self) -> None:
        outcome = build("low-confidence").run_once()
        assert outcome.verdict is Verdict.INDETERMINATE

    def test_garbled_model_reply_is_indeterminate(self) -> None:
        node = VirtualActuatorNode()
        outcome = build("garbled", node).run_once()
        assert outcome.verdict is Verdict.INDETERMINATE
        assert node.latch is LatchState.ENGAGED


class TestMachineryFailsClosed:
    def test_a_dead_camera_is_indeterminate(self) -> None:
        node = VirtualActuatorNode()
        engine = InspectionEngine(
            KIT, BrokenSource(), SimulatedEngine(), LoopbackLink(node)
        )
        outcome = engine.run_once()
        assert outcome.verdict is Verdict.INDETERMINATE
        assert "camera disconnected" in outcome.record.resolution.reason
        assert node.latch is LatchState.ENGAGED

    def test_a_crashed_npu_is_indeterminate(self) -> None:
        node = VirtualActuatorNode()
        outcome = build("engine-fault", node).run_once()
        assert outcome.verdict is Verdict.INDETERMINATE
        assert node.latch is LatchState.ENGAGED

    def test_an_unexpected_engine_exception_is_indeterminate_not_a_crash(self) -> None:
        """An engine raising something we never anticipated must still fail
        closed rather than take the process down mid-inspection."""
        node = VirtualActuatorNode()
        engine = InspectionEngine(
            KIT, ScriptedSource("complete"), ExplodingEngine(), LoopbackLink(node)
        )
        outcome = engine.run_once()
        assert outcome.verdict is Verdict.INDETERMINATE
        assert "ZeroDivisionError" in outcome.record.resolution.reason
        assert node.latch is LatchState.ENGAGED


class TestHonestRecording:
    def test_an_unenacted_pass_is_not_reported_as_enacted(self) -> None:
        """If the RELEASE never landed, nothing opened - and the record must
        not imply otherwise."""

        class DeafNode(VirtualActuatorNode):
            def handle(self, wire: bytes) -> bytes:
                return b"ACK 999 OK\n"  # wrong sequence number

        outcome = build("complete", DeafNode()).run_once()
        assert outcome.verdict is Verdict.PASS
        assert not outcome.enacted
        assert "NOT-ACKNOWLEDGED" in outcome.record.commanded

    def test_record_without_a_link_says_nothing_was_actuated(self) -> None:
        outcome = build("complete").run_once()
        assert "no host link configured" in outcome.record.commanded

    def test_record_serialises_to_one_json_line(self) -> None:
        outcome = build("missing-multimeter").run_once()
        line = outcome.record.to_json_line()
        assert "\n" not in line
        payload = json.loads(line)
        assert payload["verdict"] == "fail"
        assert payload["missing"] == ["multimeter"]
        assert payload["frame_digest"]

    def test_record_captures_every_sighting_for_audit(self) -> None:
        outcome = build("complete").run_once()
        payload = json.loads(outcome.record.to_json_line())
        assert {s["key"] for s in payload["sightings"]} == set(KIT.keys)

    def test_each_inspection_gets_its_own_id(self) -> None:
        engine = build("complete")
        assert engine.run_once().record.inspection_id != (
            engine.run_once().record.inspection_id
        )


class TestSimulatorGuards:
    def test_unknown_scene_is_reported_clearly(self) -> None:
        outcome = build("banana").run_once()
        assert outcome.verdict is Verdict.INDETERMINATE
        assert "unknown scene" in outcome.record.resolution.reason

    def test_scene_naming_an_item_not_on_the_manifest_is_reported(self) -> None:
        outcome = build("missing-elephant").run_once()
        assert "not on manifest" in outcome.record.resolution.reason

    def test_simulated_engine_refuses_raw_image_data(self) -> None:
        """Pairing the simulator with a real camera is a config error, not a
        silent pass."""
        engine = SimulatedEngine()
        with pytest.raises(InferenceError, match="scene name"):
            engine.infer(Frame(image=b"\x89PNG", digest="x"), KIT)
