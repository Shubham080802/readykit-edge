"""The remaining edges: factories, the ffmpeg subprocess, Ollama's HTTP call.

These are the seams where this project meets something outside it - another
process, a socket, a name resolved from a string. All of them were reachable
only with hardware or a server attached, so all of them are faked here.

The common theme in what is asserted: when the outside world misbehaves, this
code has to produce something the latch can act on safely, and has to say what
went wrong specifically enough to fix it.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from readykit.bridge import LinkError, LoopbackLink, open_link
from readykit.capture import CaptureError, FfmpegCameraSource, list_cameras
from readykit.domain import Manifest, RequiredItem
from readykit.inference import InferenceError, SimulatedEngine, load_engine

KIT = Manifest(
    manifest_id="io-test",
    name="IO Test Kit",
    items=(RequiredItem(key="shears", label="Trauma Shears"),),
)

JPEG = b"\xff\xd8\xff" + b"\x00" * 32 + b"\xff\xd9"


class TestTheLinkFactory:
    def test_loopback_resolves(self) -> None:
        assert isinstance(open_link("loopback"), LoopbackLink)

    def test_an_unknown_link_names_the_options(self) -> None:
        with pytest.raises(LinkError, match="expected 'serial' or 'loopback'"):
            open_link("carrier-pigeon")

    def test_serial_is_imported_lazily(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The simulated path has to work with no host extras installed, so
        pyserial must not be imported until a serial link is actually asked
        for."""
        import sys

        monkeypatch.setitem(sys.modules, "serial", None)
        with pytest.raises(LinkError, match="pyserial is not installed"):
            open_link("serial", port="/dev/fake", settle_seconds=0)


class TestTheEngineFactory:
    def test_simulated_resolves(self) -> None:
        assert isinstance(load_engine("simulated"), SimulatedEngine)

    def test_an_unknown_engine_names_the_options(self) -> None:
        with pytest.raises(InferenceError, match="geniex"):
            load_engine("telepathy")

    def test_geniex_is_imported_lazily(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The Qualcomm SDK only exists on the Snapdragon host."""
        import sys

        monkeypatch.setitem(sys.modules, "geniex", None)
        with pytest.raises(InferenceError, match="pip install geniex"):
            load_engine("geniex")

    def test_ollama_resolves_through_the_factory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(InferenceError, match="no Ollama server"):
            load_engine("ollama", host="http://127.0.0.1:1")


class FakeCompleted:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", code: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = code


class TestFfmpegCaptureFailureModes:
    """Every one of these has to raise CaptureError, which resolves to
    INDETERMINATE upstream and leaves the latch shut."""

    @pytest.fixture
    def source(self, monkeypatch: pytest.MonkeyPatch) -> FfmpegCameraSource:
        monkeypatch.setattr("readykit.capture.shutil.which", lambda _: "/usr/bin/ffmpeg")
        return FfmpegCameraSource(device="0", warmup_frames=1)

    def test_a_good_capture_returns_the_last_frame(
        self, source: FfmpegCameraSource, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stream = (b"\xff\xd8\xff" + b"\x01" * 16 + b"\xff\xd9") + JPEG
        monkeypatch.setattr(
            "readykit.capture.subprocess.run",
            lambda *a, **k: FakeCompleted(stdout=stream),
        )
        frame = source.read()
        assert frame.image == JPEG
        assert frame.digest

    def test_a_nonzero_exit_names_the_device(
        self, source: FfmpegCameraSource, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "readykit.capture.subprocess.run",
            lambda *a, **k: FakeCompleted(stderr=b"Input/output error\n", code=1),
        )
        with pytest.raises(CaptureError, match="Input/output error"):
            source.read()

    def test_empty_output_is_refused(
        self, source: FfmpegCameraSource, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "readykit.capture.subprocess.run", lambda *a, **k: FakeCompleted()
        )
        with pytest.raises(CaptureError, match="no image"):
            source.read()

    def test_a_hung_camera_times_out_with_a_useful_message(
        self, source: FfmpegCameraSource, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def hang(*a: Any, **k: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=30)

        monkeypatch.setattr("readykit.capture.subprocess.run", hang)
        with pytest.raises(CaptureError, match="holding the camera"):
            source.read()

    def test_ffmpeg_vanishing_is_reported(
        self, source: FfmpegCameraSource, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def missing(*a: Any, **k: Any) -> Any:
            raise OSError("No such file or directory: 'ffmpeg'")

        monkeypatch.setattr("readykit.capture.subprocess.run", missing)
        with pytest.raises(CaptureError, match="could not run ffmpeg"):
            source.read()

    def test_listing_cameras_survives_a_broken_ffmpeg(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Enumeration failing means "could not enumerate", never "no
        cameras" - so it must not raise."""
        monkeypatch.setattr("readykit.capture.shutil.which", lambda _: "/usr/bin/ffmpeg")

        def broken(*a: Any, **k: Any) -> Any:
            raise OSError("boom")

        monkeypatch.setattr("readykit.capture.subprocess.run", broken)
        assert list_cameras() == []

    def test_no_ffmpeg_means_an_empty_listing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("readykit.capture.shutil.which", lambda _: None)
        assert list_cameras() == []


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class TestOllamaInference:
    def engine(self, monkeypatch: pytest.MonkeyPatch, reply: str) -> Any:
        import json

        from readykit.inference.ollama import OllamaEngine

        def urlopen(request: Any, timeout: float = 0) -> FakeResponse:
            url = request if isinstance(request, str) else request.full_url
            if url.endswith("/api/tags"):
                return FakeResponse(
                    json.dumps({"models": [{"name": "qwen2.5vl:7b"}]}).encode()
                )
            return FakeResponse(
                json.dumps({"message": {"role": "assistant", "content": reply}}).encode()
            )

        monkeypatch.setattr(
            "readykit.inference.ollama.urllib.request.urlopen", urlopen
        )
        return OllamaEngine()

    def test_a_good_reply_becomes_sightings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from readykit.capture import Frame

        engine = self.engine(
            monkeypatch,
            '{"items": [{"key": "shears", "presence": "found", "confidence": 0.9}]}',
        )
        observation = engine.infer(Frame(image=JPEG, digest="d"), KIT)
        assert [s.key for s in observation.sightings] == ["shears"]

    def test_unusable_prose_keeps_the_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from readykit.capture import Frame

        engine = self.engine(monkeypatch, "I am unable to assist with that.")
        with pytest.raises(InferenceError) as caught:
            engine.infer(Frame(image=JPEG, digest="d"), KIT)
        assert caught.value.raw_reply == "I am unable to assist with that."

    def test_a_text_only_model_returning_nothing_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        from readykit.capture import Frame
        from readykit.inference.ollama import OllamaEngine

        def urlopen(request: Any, timeout: float = 0) -> FakeResponse:
            url = request if isinstance(request, str) else request.full_url
            if url.endswith("/api/tags"):
                return FakeResponse(
                    json.dumps({"models": [{"name": "qwen2.5vl:7b"}]}).encode()
                )
            return FakeResponse(json.dumps({"done": True}).encode())

        monkeypatch.setattr(
            "readykit.inference.ollama.urllib.request.urlopen", urlopen
        )
        engine = OllamaEngine()
        with pytest.raises(InferenceError, match="vision model"):
            engine.infer(Frame(image=JPEG, digest="d"), KIT)

    def test_a_missing_model_is_named_with_what_is_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        from readykit.inference.ollama import OllamaEngine

        monkeypatch.setattr(
            "readykit.inference.ollama.urllib.request.urlopen",
            lambda *a, **k: FakeResponse(
                json.dumps({"models": [{"name": "llama3.2:latest"}]}).encode()
            ),
        )
        with pytest.raises(InferenceError, match="ollama pull"):
            OllamaEngine(model="qwen2.5vl:7b")

    def test_a_bare_name_matches_its_tag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ollama resolves a bare name to :latest, so reporting it missing
        when it is installed would be wrong."""
        import json

        from readykit.inference.ollama import OllamaEngine

        monkeypatch.setattr(
            "readykit.inference.ollama.urllib.request.urlopen",
            lambda *a, **k: FakeResponse(
                json.dumps({"models": [{"name": "moondream:latest"}]}).encode()
            ),
        )
        OllamaEngine(model="moondream")
