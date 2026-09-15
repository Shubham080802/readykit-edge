"""The GenieX engine, against a fake SDK.

This is the engine that runs on the day, and it was the least-covered module
in the project - because the Qualcomm SDK only exists on the Snapdragon host,
so none of it could run anywhere else. That is exactly backwards: code that
can only be exercised on the machine you have least access to is the code most
worth testing in advance.

So the SDK is faked. That cannot prove GenieX behaves as documented - only the
hardware settles that - but it does prove our side: that a text-only model is
refused before it can answer confidently about an image it never saw, that a
frame reaches the model as a file path and the file is cleaned up afterwards,
and that every failure resolves to something the latch can act on safely.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from readykit.capture import Frame
from readykit.domain import Manifest, RequiredItem
from readykit.inference.base import InferenceError

KIT = Manifest(
    manifest_id="geniex-test",
    name="GenieX Test Kit",
    items=(
        RequiredItem(key="shears", label="Trauma Shears"),
        RequiredItem(key="gauze", label="Hemostatic Gauze"),
    ),
)

GOOD_REPLY = """Looking at the tray.

```json
{"items": [
  {"key": "shears", "presence": "found", "confidence": 0.95},
  {"key": "gauze", "presence": "found", "confidence": 0.93}
]}
```"""

JPEG = b"\xff\xd8\xff" + b"\x00" * 32 + b"\xff\xd9"


class FakeTokenizer:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.seen: list[Any] = []

    def apply_chat_template(
        self, messages: list[dict[str, str]], add_generation_prompt: bool = False
    ) -> str:
        if self.fail:
            raise RuntimeError("no chat template for this model")
        self.seen.append(messages)
        return f"<|user|>{messages[0]['content']}<|assistant|>"


class FakeModel:
    """A GenieX vision model."""

    def __init__(
        self,
        reply: Any = GOOD_REPLY,
        raise_on_generate: Exception | None = None,
        tokenizer_fails: bool = False,
        device: str | None = "qnn-htp",
    ) -> None:
        self.tokenizer = FakeTokenizer(fail=tokenizer_fails)
        self._reply = reply
        self._raise = raise_on_generate
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        if device is not None:
            self.device = device

    def generate(self, prompt: str, **kwargs: Any) -> Any:
        if self._raise is not None:
            raise self._raise
        self.calls.append({"prompt": prompt, **kwargs})
        return self._reply

    def close(self) -> None:
        self.closed = True


class GenieXLLM(FakeModel):
    """A text-only model. The class name is how the real SDK signals this."""


def install_sdk(
    monkeypatch: pytest.MonkeyPatch,
    model: Any,
    raise_on_load: Exception | None = None,
) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    module = types.ModuleType("geniex")

    class AutoModelForCausalLM:
        @staticmethod
        def from_pretrained(name: str, device_map: str = "auto") -> Any:
            if raise_on_load is not None:
                raise raise_on_load
            seen["model"] = name
            seen["device_map"] = device_map
            return model

    module.AutoModelForCausalLM = AutoModelForCausalLM  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "geniex", module)
    return seen


def build(monkeypatch: pytest.MonkeyPatch, model: Any, **kwargs: Any) -> Any:
    install_sdk(monkeypatch, model)
    from readykit.inference.geniex import GenieXEngine

    return GenieXEngine(**kwargs)


class TestRefusingAModelThatCannotSee:
    def test_a_text_only_model_is_refused_at_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The worst possible failure: a model that accepts the prompt,
        silently ignores the image, and answers confidently about a kit it
        never saw. Refuse before it can do that, not after."""
        with pytest.raises(InferenceError, match="vision-language model"):
            build(monkeypatch, GenieXLLM())

    def test_a_model_without_generate_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(InferenceError, match="no generate"):
            build(monkeypatch, object())

    def test_a_load_failure_names_the_model_and_device(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_sdk(monkeypatch, FakeModel(), raise_on_load=RuntimeError("boom"))
        from readykit.inference.geniex import GenieXEngine

        with pytest.raises(InferenceError, match="could not load"):
            GenieXEngine(model="some/model", device_map="htp")

    def test_a_missing_sdk_names_the_fix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "geniex", None)
        from readykit.inference.geniex import GenieXEngine

        with pytest.raises(InferenceError, match="pip install geniex"):
            GenieXEngine()


class TestInference:
    def test_a_good_reply_becomes_sightings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = build(monkeypatch, FakeModel())
        observation = engine.infer(Frame(image=JPEG, digest="d"), KIT)
        assert {s.key for s in observation.sightings} == {"shears", "gauze"}
        assert observation.raw_reply == GOOD_REPLY

    def test_the_frame_arrives_as_a_file_path_that_exists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GenieX takes paths, not arrays. The file has to be real at the
        moment generate() is called."""
        seen: dict[str, Any] = {}

        class Checking(FakeModel):
            def generate(self, prompt: str, **kwargs: Any) -> Any:
                path = Path(kwargs["images"][0])
                seen["existed"] = path.exists()
                seen["bytes"] = path.read_bytes()
                seen["path"] = path
                return GOOD_REPLY

        engine = build(monkeypatch, Checking())
        engine.infer(Frame(image=JPEG, digest="d"), KIT)
        assert seen["existed"] is True
        assert seen["bytes"] == JPEG

    def test_the_frame_is_deleted_afterwards(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An air-gapped appliance that accumulated an image of every kit it
        ever inspected would be a different product."""
        seen: dict[str, Any] = {}

        class Remembering(FakeModel):
            def generate(self, prompt: str, **kwargs: Any) -> Any:
                seen["path"] = Path(kwargs["images"][0])
                return GOOD_REPLY

        engine = build(monkeypatch, Remembering())
        engine.infer(Frame(image=JPEG, digest="d"), KIT)
        assert not seen["path"].exists()

    def test_the_frame_is_deleted_even_when_inference_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        holder: dict[str, Any] = {}

        class Failing(FakeModel):
            def generate(self, prompt: str, **kwargs: Any) -> Any:
                holder["path"] = Path(kwargs["images"][0])
                raise RuntimeError("NPU context died")

        engine = build(monkeypatch, Failing())
        with pytest.raises(InferenceError):
            engine.infer(Frame(image=JPEG, digest="d"), KIT)
        assert not holder["path"].exists()

    def test_a_crashed_model_raises_inference_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = build(
            monkeypatch, FakeModel(raise_on_generate=RuntimeError("context lost"))
        )
        with pytest.raises(InferenceError, match="NPU inference failed") as caught:
            engine.infer(Frame(image=JPEG, digest="d"), KIT)
        # No text ever existed, so there is nothing the blueprint could have
        # parsed - distinct from a model that returned unusable prose.
        assert caught.value.raw_reply is None

    def test_unusable_prose_keeps_the_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exactly what the blueprint's substring matcher would have acted on,
        so it is preserved rather than discarded."""
        engine = build(monkeypatch, FakeModel(reply="I'm sorry, I can't help."))
        with pytest.raises(InferenceError) as caught:
            engine.infer(Frame(image=JPEG, digest="d"), KIT)
        assert caught.value.raw_reply == "I'm sorry, I can't help."

    def test_streamed_chunks_are_joined_not_stringified(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A build that returns chunks must not end up with a generator's repr
        in the audit trail."""
        engine = build(monkeypatch, FakeModel(reply=iter(["{\"items\"", ": []}"])))
        observation = engine.infer(Frame(image=JPEG, digest="d"), KIT)
        assert observation.raw_reply == '{"items": []}'

    def test_a_chat_template_failure_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = build(monkeypatch, FakeModel(tokenizer_fails=True))
        with pytest.raises(InferenceError, match="chat template"):
            engine.infer(Frame(image=JPEG, digest="d"), KIT)

    def test_the_manifest_reaches_the_prompt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        model = FakeModel()
        engine = build(monkeypatch, model)
        engine.infer(Frame(image=JPEG, digest="d"), KIT)
        assert "Trauma Shears" in model.calls[0]["prompt"]

    def test_temperature_is_low_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A latch decision is not a place for creative sampling."""
        model = FakeModel()
        engine = build(monkeypatch, model)
        engine.infer(Frame(image=JPEG, digest="d"), KIT)
        assert model.calls[0]["temperature"] <= 0.2
        assert model.calls[0]["stream"] is False


class TestTheRecordedDevice:
    def test_a_verified_device_is_recorded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = build(monkeypatch, FakeModel(device="qnn-htp"), device_map="auto")
        assert engine.resolved_device == "qnn-htp"
        assert engine.name.endswith("@qnn-htp")

    def test_an_unreadable_device_is_marked_unverified(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Never pass the request off as the result."""
        engine = build(monkeypatch, FakeModel(device=None), device_map="auto")
        assert engine.resolved_device is None
        assert "unverified" in engine.name

    def test_require_npu_accepts_an_npu(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = build(monkeypatch, FakeModel(device="htp"), require_npu=True)
        assert engine.resolved_device == "htp"

    def test_require_npu_refuses_a_cpu_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(InferenceError, match="not an NPU backend"):
            build(monkeypatch, FakeModel(device="cpu"), require_npu=True)

    def test_require_npu_refuses_an_unverifiable_build(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(InferenceError, match="cannot be established"):
            build(monkeypatch, FakeModel(device=None), require_npu=True)

    def test_without_the_flag_a_cpu_still_runs_but_is_recorded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = build(monkeypatch, FakeModel(device="cpu"))
        assert engine.name.endswith("@cpu")


class TestClosing:
    def test_close_releases_the_context(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        model = FakeModel()
        engine = build(monkeypatch, model)
        engine.close()
        assert model.closed

    def test_close_is_safe_before_a_model_loaded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from readykit.inference.geniex import GenieXEngine

        engine = GenieXEngine.__new__(GenieXEngine)
        engine.close()
