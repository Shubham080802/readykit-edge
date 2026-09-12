"""Qualcomm GenieX engine - runs a quantised VLM on the Hexagon NPU.

This is the field path. It only imports on a host with the Qualcomm AI Engine
Direct SDK present, which is why `inference/__init__.py` loads it lazily.

The contract it must honour is the same one every engine honours: return what
the model actually committed to, and raise rather than invent. Every failure
mode here - model load failure, NPU timeout, a reply that is not JSON - ends
as InferenceError or an incomplete Sighting list, both of which resolve to
INDETERMINATE and leave the Latch engaged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..capture import Frame
from ..domain import Manifest, Sighting
from ..reply import ReplyParseError, build_prompt, parse_reply
from .base import InferenceEngine, InferenceError


class GenieXEngine(InferenceEngine):
    name = "geniex"

    def __init__(
        self,
        model_path: str | Path,
        target_hardware: str = "Hexagon_NPU",
        timeout_seconds: float = 8.0,
    ) -> None:
        self._model_path = Path(model_path)
        if not self._model_path.exists():
            raise InferenceError(
                f"model not found at {self._model_path}. Export one from "
                "Qualcomm AI Hub compiled for Snapdragon X Elite - see "
                "docs/deployment.md"
            )

        try:
            from geniex import GenieXInferenceEngine
        except ImportError as exc:
            raise InferenceError(
                "The Qualcomm GenieX SDK is not available on this machine. It "
                "ships with the Qualcomm AI Engine Direct SDK and only exists "
                "on the Snapdragon host. Use --engine simulated off-device."
            ) from exc

        try:
            self._engine: Any = GenieXInferenceEngine(
                model_path=str(self._model_path),
                target_hardware=target_hardware,
            )
        except Exception as exc:
            raise InferenceError(
                f"could not load {self._model_path} onto {target_hardware}: {exc}"
            ) from exc

        self._timeout = timeout_seconds

    def infer(self, frame: Frame, manifest: Manifest) -> list[Sighting]:
        prompt = build_prompt(manifest)
        try:
            raw = self._engine.infer(image=frame.image, text=prompt)
        except Exception as exc:
            raise InferenceError(f"NPU inference failed: {exc}") from exc

        if not isinstance(raw, str):
            raw = str(raw)

        try:
            return parse_reply(raw, manifest)
        except ReplyParseError as exc:
            # The model ran but produced nothing we can act on. This is the
            # single most likely real-world failure, and it must not be
            # mistaken for a clean kit.
            raise InferenceError(f"model reply was unusable: {exc}") from exc

    def close(self) -> None:
        engine = getattr(self, "_engine", None)
        if engine is not None and hasattr(engine, "close"):
            engine.close()
