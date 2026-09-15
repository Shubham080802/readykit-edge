"""Ollama engine - a real vision-language model, on whatever machine you have.

This exists because the Qualcomm stack only runs on the Snapdragon host, and
waiting for that machine to test anything is a bad way to build.

`GenieXEngine` is the deployment target. This is the same pipeline, the same
prompt and the same parser, pointed at a model running locally through Ollama -
so a laptop with no Hexagon NPU can still answer the questions that actually
matter before the hardware arrives:

  - does the prompt in `reply.py` survive contact with a real model, or was it
    only ever tested against a simulator that answers in the format it was
    asked for?
  - does `parse_reply` cope with what a real VLM returns - the prose preamble,
    the markdown fence, the key it invented?
  - what does a real inspection actually cost in seconds?

None of those can be answered by the simulator, because the simulator was
written by the same hand as the parser and agrees with it by construction.

The default model is deliberately the same family as the GenieX default:
the same Qwen vision family, so switching runtime changes the hardware
without changing the kind of model being asked.

**This is not the NPU and must never be reported as one.** The engine name
recorded on every inspection says `ollama:<model>`, which is unambiguous. Use
`--engine geniex --require-npu` for anything where NPU use is the claim.

Needs no new dependencies: Ollama speaks HTTP and the standard library can
make an HTTP request.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any

from ..capture import Frame
from ..domain import Manifest
from ..reply import ReplyParseError, build_prompt, parse_reply
from .base import InferenceEngine, InferenceError, Observation

DEFAULT_MODEL = "qwen2.5vl:7b"
"""The nearest Ollama equivalent of the GenieX default. Ollama does not
carry Qwen3-VL-4B, so this is a family match rather than an exact one -
which is fine, because this engine exists to prove the prompt and the
parser survive a real model, not to reproduce the NPU's output."""
"""Same model family as the GenieX default, so the runtime is the only thing
that changes between this and the Snapdragon host."""

DEFAULT_HOST = "http://127.0.0.1:11434"
"""Ollama's own default. Loopback, like everything else here."""


class OllamaEngine(InferenceEngine):
    name = "ollama"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        host: str = DEFAULT_HOST,
        timeout: float = 180.0,
        temperature: float = 0.1,
    ) -> None:
        self.model_id = model
        self.host = host.rstrip("/")
        self._timeout = timeout
        self._temperature = temperature

        # Fail at construction rather than on the first frame. A missing model
        # is a setup problem, and finding out about it three seconds into a
        # demonstration is strictly worse than finding out at startup.
        self._check_reachable()

        # Recorded on every inspection. Unambiguous on purpose: nobody reading
        # a record should be able to mistake this for the Hexagon NPU.
        self.name = f"ollama:{model}"

    # -- setup ---------------------------------------------------------------

    def _check_reachable(self) -> None:
        try:
            with urllib.request.urlopen(
                f"{self.host}/api/tags", timeout=10
            ) as response:
                payload = json.loads(response.read())
        except urllib.error.URLError as exc:
            raise InferenceError(
                f"no Ollama server at {self.host} ({exc.reason}). "
                "Start it with `ollama serve`, or use --engine simulated."
            ) from exc
        except (OSError, ValueError) as exc:
            raise InferenceError(f"could not reach Ollama: {exc}") from exc

        installed = {
            entry.get("name", "")
            for entry in payload.get("models", [])
            if isinstance(entry, dict)
        }
        if self.model_id in installed:
            return

        # Ollama resolves a bare name to its :latest tag, so check that too
        # before telling someone their model is missing when it is not.
        bare = self.model_id.split(":")[0]
        if any(name.split(":")[0] == bare for name in installed):
            return

        raise InferenceError(
            f"Ollama has no model {self.model_id!r}. "
            f"Pull it with `ollama pull {self.model_id}`. "
            f"Installed: {', '.join(sorted(installed)) or 'none'}"
        )

    # -- inference -----------------------------------------------------------

    def infer(self, frame: Frame, manifest: Manifest) -> Observation:
        image = _image_bytes(frame)
        request = json.dumps(
            {
                "model": self.model_id,
                "prompt": build_prompt(manifest),
                "images": [base64.b64encode(image).decode("ascii")],
                "stream": False,
                "options": {"temperature": self._temperature},
            }
        ).encode()

        try:
            with urllib.request.urlopen(
                urllib.request.Request(
                    f"{self.host}/api/generate",
                    data=request,
                    headers={"Content-Type": "application/json"},
                ),
                timeout=self._timeout,
            ) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise InferenceError(
                f"Ollama returned {exc.code}: {exc.read().decode()[:200]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise InferenceError(f"Ollama inference failed: {exc.reason}") from exc
        except (OSError, ValueError) as exc:
            raise InferenceError(f"Ollama inference failed: {exc}") from exc

        raw = payload.get("response")
        if not isinstance(raw, str):
            # No text at all is a different failure from unusable text, and
            # only the latter is something the blueprint could have parsed.
            raise InferenceError(
                f"Ollama returned no text for {self.model_id!r}. "
                "A text-only model would do this - it accepts the prompt and "
                "silently ignores the image. Use a vision model."
            )

        try:
            sightings = parse_reply(raw, manifest)
        except ReplyParseError as exc:
            # The model ran but produced nothing we can act on. The text is
            # kept: it is exactly what the blueprint's substring matcher would
            # have acted on, and it is the most interesting failure there is.
            raise InferenceError(
                f"model reply was unusable: {exc}", raw_reply=raw
            ) from exc

        return Observation(sightings=tuple(sightings), raw_reply=raw)


def _image_bytes(frame: Frame) -> bytes:
    """The frame's pixels as encoded image bytes.

    ImageFileSource already yields bytes, which is the path that matters here.
    A raw numpy array from a camera would need encoding first, and that is a
    dependency this module deliberately does not take - capture to a JPEG and
    read it back instead.
    """
    image: Any = frame.image
    if isinstance(image, bytes):
        return image
    if isinstance(image, bytearray | memoryview):
        return bytes(image)
    raise InferenceError(
        f"the ollama engine needs encoded image bytes, got {type(image).__name__}. "
        "Use --image <path> (ImageFileSource), not a scene name or a raw array."
    )
