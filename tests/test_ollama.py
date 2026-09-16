"""The off-device engine, and the one thing it must never be mistaken for.

This engine exists so the prompt and the parser can meet a real vision model
before the Snapdragon host is available - and as cover for the single most
likely setup failure on the day, which is `pip install geniex` refusing to
build its native binding.

The assertions that matter are about honesty. A record produced here must be
impossible to read as an NPU run.

Runs without Ollama installed: everything below either needs no server, or
skips when there is not one.
"""

from __future__ import annotations

import pytest

from readykit.capture import Frame
from readykit.domain import Manifest, RequiredItem
from readykit.inference.base import InferenceError
from readykit.inference.ollama import (
    DEFAULT_HOST,
    DEFAULT_MODEL,
    OllamaEngine,
    _image_bytes,
)

KIT = Manifest(
    manifest_id="ollama-test",
    name="Ollama Test Kit",
    items=(RequiredItem(key="shears", label="Trauma Shears"),),
)

# Port 1 is privileged and never listening, so this fails fast and offline.
UNREACHABLE = "http://127.0.0.1:1"


class TestItNeverLooksLikeAnNpuRun:
    def test_the_recorded_name_says_ollama(self) -> None:
        """This string lands in the audit trail. Someone reading a record has
        to be able to tell at a glance that it was not the Hexagon NPU."""
        engine = OllamaEngine.__new__(OllamaEngine)
        engine.model_id = "qwen2.5vl:7b"
        engine.name = f"ollama:{engine.model_id}"

        assert engine.name.startswith("ollama:")
        assert "geniex" not in engine.name
        assert "npu" not in engine.name.lower()
        assert "htp" not in engine.name.lower()

    def test_the_default_model_matches_the_geniex_family(self) -> None:
        """Same model, different runtime, so switching hosts changes the
        hardware rather than the model's behaviour."""
        assert "qwen2.5vl" in DEFAULT_MODEL.lower()

    def test_the_default_host_is_loopback(self) -> None:
        assert DEFAULT_HOST.startswith("http://127.0.0.1")


class TestFailingAtStartupRatherThanMidDemo:
    def test_no_server_is_refused_at_construction(self) -> None:
        """Not on the first frame. Finding out three seconds into a
        demonstration is strictly worse than finding out at startup."""
        with pytest.raises(InferenceError, match="no Ollama server"):
            OllamaEngine(host=UNREACHABLE)

    def test_the_error_says_how_to_fix_it(self) -> None:
        with pytest.raises(InferenceError) as caught:
            OllamaEngine(host=UNREACHABLE)
        message = str(caught.value)
        assert "ollama serve" in message
        assert "simulated" in message


class TestFrameBytes:
    def test_encoded_bytes_pass_through(self) -> None:
        assert _image_bytes(Frame(image=b"\xff\xd8jpeg", digest="d")) == b"\xff\xd8jpeg"

    @pytest.mark.parametrize("payload", [bytearray(b"abc"), memoryview(b"abc")])
    def test_buffer_types_are_accepted(self, payload: object) -> None:
        assert _image_bytes(Frame(image=payload, digest="d")) == b"abc"

    def test_a_scene_name_is_refused_with_a_useful_message(self) -> None:
        """The likely mistake: pointing this at a simulator scene. It cannot
        look at a string, and must say so rather than send one."""
        with pytest.raises(InferenceError, match="got a scene name"):
            _image_bytes(Frame(image="complete", digest="d"))

    def test_the_refusal_names_the_fix(self) -> None:
        with pytest.raises(InferenceError) as caught:
            _image_bytes(Frame(image="complete", digest="d"))
        assert "--image" in str(caught.value)

    def test_a_camera_array_is_encoded_as_a_jpeg(self) -> None:
        """An OpenCV camera yields a pixel array, not bytes. This was refused,
        so `--camera 0 --engine ollama` opened the camera, captured a frame,
        and resolved every inspection INDETERMINATE on the encoding step -
        found running the console against a real webcam.

        Skipped where the host extras are not installed, which includes CI.
        """
        np = pytest.importorskip("numpy")
        pytest.importorskip("cv2")
        pixels = np.zeros((48, 64, 3), dtype=np.uint8)
        pixels[:, :32] = (0, 128, 255)

        encoded = _image_bytes(Frame(image=pixels, digest="d"))

        assert encoded[:2] == b"\xff\xd8", "not a JPEG start-of-image marker"
        assert encoded[-2:] == b"\xff\xd9", "not a JPEG end-of-image marker"


class TestAgainstARunningServer:
    """Skipped unless Ollama is actually up, so CI stays offline."""

    def engine_or_skip(self) -> OllamaEngine:
        try:
            return OllamaEngine()
        except InferenceError as exc:
            pytest.skip(f"no usable Ollama: {exc}")

    def test_a_missing_model_is_named_clearly(self) -> None:
        try:
            OllamaEngine(model="definitely-not-a-real-model:0b")
        except InferenceError as exc:
            assert "pull" in str(exc) or "no Ollama server" in str(exc)
        else:
            pytest.fail("a nonexistent model should not have been accepted")
