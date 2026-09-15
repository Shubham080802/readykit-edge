"""Qualcomm GenieX engine - runs a vision-language model on the Hexagon NPU.

This is the field path, written against the documented GenieX Python API:

    from geniex import AutoModelForCausalLM

    model = AutoModelForCausalLM.from_pretrained(
        "ai-hub-models/Qwen2.5-VL-7B-Instruct", device_map="auto"
    )                                   # -> GenieXVLM for a multimodal model
    prompt = model.tokenizer.apply_chat_template(messages, add_generation_prompt=True)
    reply  = model.generate(prompt, images=["/path/to/frame.jpg"], stream=False)
    model.close()

Three things about that API drive the shape of this module:

* **A model is a repo id, not a file.** GenieX pulls a GGUF from Hugging Face
  or a pre-compiled bundle from Qualcomm AI Hub. There is no `.qnn` path to
  point at, so `--model` takes something like
  `ai-hub-models/Qwen2.5-VL-7B-Instruct`.
* **Images are passed as file paths.** Not arrays, not PIL objects. A frame
  captured from the camera therefore has to be written to disk before it can
  be inspected, which this module does to a temporary file it owns and
  deletes.
* **Hardware is chosen with `device_map`.** `"auto"` takes the first available
  runtime; an explicit `"<runtime>:<compute_unit>"` pins it. That string is
  recorded on every Inspection Record, because "it ran on the NPU" is a claim
  that should be evidenced rather than assumed.

Every failure here - model load, inference, an unparseable reply - ends as
InferenceError, which resolves to INDETERMINATE and leaves the Latch engaged.

Docs: https://geniex.aihub.qualcomm.com/en/run/python/api-reference
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from ..capture import Frame
from ..domain import Manifest
from ..reply import ReplyParseError, build_prompt, parse_reply
from .base import InferenceEngine, InferenceError, Observation

DEFAULT_MODEL = "ai-hub-models/Qwen2.5-VL-7B-Instruct"
"""A vision-language bundle precompiled for the Hexagon NPU. Overridable -
nothing here depends on this particular model, only on it being multimodal."""


class GenieXEngine(InferenceEngine):
    name = "geniex"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        device_map: str = "auto",
        max_new_tokens: int = 768,
        temperature: float = 0.1,
    ) -> None:
        """
        `temperature` defaults low on purpose. This model is deciding whether a
        latch opens, and a compliance verdict that varies between runs on an
        unchanged kit is not a verdict. Sampling entropy buys nothing here.

        `max_new_tokens` has to cover one JSON object per Required Item. A
        truncated reply is not silently mishandled - it fails to parse, which
        is INDETERMINATE - but it wastes an inspection, so the default is
        generous.
        """
        # A repo id legitimately contains a slash ("ai-hub-models/..."), so
        # only a model-file extension or an actual file on disk is a mistake.
        if str(model).endswith((".qnn", ".bin", ".onnx", ".gguf")) or Path(
            model
        ).is_file():
            raise InferenceError(
                f"{model!r} looks like a file path. GenieX loads models by "
                "repo id, not by file - for example "
                f"{DEFAULT_MODEL!r} (Qualcomm AI Hub) or "
                "'unsloth/Qwen3.5-2B-GGUF' (Hugging Face). See "
                "docs/deployment.md."
            )

        try:
            from geniex import AutoModelForCausalLM
        except ImportError as exc:
            raise InferenceError(
                "GenieX is not installed. On the Snapdragon host: "
                "pip install geniex. Off-device, use --engine simulated."
            ) from exc

        try:
            self._model: Any = AutoModelForCausalLM.from_pretrained(
                model, device_map=device_map
            )
        except Exception as exc:
            raise InferenceError(
                f"could not load {model!r} with device_map={device_map!r}: {exc}"
            ) from exc

        # A text-only model would accept the prompt and silently ignore the
        # frame, which is the worst possible failure: confident answers about
        # an image it never saw. Refuse at construction instead.
        if not hasattr(self._model, "generate"):
            raise InferenceError(f"{model!r} exposes no generate()")
        if type(self._model).__name__ == "GenieXLLM":
            raise InferenceError(
                f"{model!r} loaded as a text-only model. ReadyKit needs a "
                "vision-language model - it has to look at the kit, not read "
                f"about it. Try {DEFAULT_MODEL!r}."
            )

        self.model_id = model
        self.device_map = device_map
        # Goes onto every Inspection Record. "It ran on the NPU" is a claim an
        # auditor should be able to check, not one they take on trust.
        self.name = f"geniex:{model}@{device_map}"
        self._max_new_tokens = max_new_tokens
        self._temperature = temperature

    def infer(self, frame: Frame, manifest: Manifest) -> Observation:
        prompt = self._render_prompt(manifest)

        with _frame_on_disk(frame) as image_path:
            try:
                raw = self._model.generate(
                    prompt,
                    images=[str(image_path)],
                    max_new_tokens=self._max_new_tokens,
                    temperature=self._temperature,
                    stream=False,
                )
            except Exception as exc:
                raise InferenceError(f"NPU inference failed: {exc}") from exc

        if not isinstance(raw, str):
            # stream=False should return a string; if a build returns chunks,
            # join them rather than stringifying a generator's repr.
            try:
                raw = "".join(raw)
            except TypeError:
                raw = str(raw)

        try:
            sightings = parse_reply(raw, manifest)
        except ReplyParseError as exc:
            # The model ran but produced nothing we can act on - the single
            # most likely real-world failure. The text is kept: it is exactly
            # what the blueprint's substring matcher would have acted on.
            raise InferenceError(
                f"model reply was unusable: {exc}", raw_reply=raw
            ) from exc

        return Observation(sightings=tuple(sightings), raw_reply=raw)

    def _render_prompt(self, manifest: Manifest) -> str:
        """Apply the model's own chat template to our inspection prompt.

        Skipping the template and passing the bare string mostly works and
        occasionally does not, in ways that look like the model ignoring
        instructions rather than a formatting bug.
        """
        messages = [{"role": "user", "content": build_prompt(manifest)}]
        try:
            rendered = self._model.tokenizer.apply_chat_template(
                messages, add_generation_prompt=True
            )
        except Exception as exc:
            raise InferenceError(f"could not apply the chat template: {exc}") from exc
        return rendered if isinstance(rendered, str) else str(rendered)

    def close(self) -> None:
        model = getattr(self, "_model", None)
        if model is not None and hasattr(model, "close"):
            model.close()


class _frame_on_disk:
    """Write a captured Frame to a temporary JPEG for the duration of a call.

    GenieX takes image file paths, so a frame that exists only as an array in
    memory has to be materialised. The file is this module's to create and
    delete - leaving frames behind on an air-gapped appliance would accumulate
    images of every kit it has ever inspected.
    """

    def __init__(self, frame: Frame) -> None:
        self._frame = frame
        self._path: Path | None = None

    def __enter__(self) -> Path:
        payload = self._frame.image

        # An ImageFileSource already has the encoded bytes; a CameraSource has
        # a numpy array that needs encoding.
        if isinstance(payload, (bytes, bytearray)):
            data = bytes(payload)
        else:
            data = _encode_jpeg(payload)

        handle, name = tempfile.mkstemp(prefix="readykit-frame-", suffix=".jpg")
        try:
            with os.fdopen(handle, "wb") as out:
                out.write(data)
        except Exception:
            os.unlink(name)
            raise
        self._path = Path(name)
        return self._path

    def __exit__(self, *exc: object) -> None:
        if self._path is not None:
            self._path.unlink(missing_ok=True)
            self._path = None


def _encode_jpeg(image: Any) -> bytes:
    try:
        import cv2
    except ImportError as exc:
        raise InferenceError(
            "opencv-python is needed to encode camera frames for GenieX. "
            'Install the host extras: pip install -e ".[host]"'
        ) from exc

    ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
    if not ok:
        raise InferenceError("could not encode the captured frame as JPEG")
    return bytes(buffer.tobytes())
