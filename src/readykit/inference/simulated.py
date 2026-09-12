"""A simulated engine, so the whole pipeline runs with no NPU and no camera.

This is not a toy. It exists to exercise the paths that are hardest to produce
on demand with real hardware and that matter most: an occluded lens, a model
that returns prose, a model that omits an item, a model that is confidently
wrong. Those are the scenarios the safety rule is built for, and they should
be reachable from a laptop.

Scenes are deterministic by default so tests and demos are reproducible.
"""

from __future__ import annotations

import random

from ..capture import Frame
from ..domain import Manifest, Presence, Sighting
from .base import InferenceEngine, InferenceError

COMPLETE = "complete"
OCCLUDED = "occluded"
EMPTY = "empty"
LOW_CONFIDENCE = "low-confidence"
GARBLED = "garbled"
ENGINE_FAULT = "engine-fault"

_BUILTIN_SCENES = {
    COMPLETE: "every required item present and clearly visible",
    OCCLUDED: "the tray is partially covered - nothing can be confirmed",
    EMPTY: "the kit is missing everything",
    LOW_CONFIDENCE: "a poor viewing angle - readings below the confidence floor",
    GARBLED: "the model returned prose instead of JSON",
    ENGINE_FAULT: "the NPU context died mid-inference",
}


class SimulatedEngine(InferenceEngine):
    """Produces Sightings from a scene name rather than from pixels.

    Pass `missing-<key>` or `damaged-<key>` to knock out a specific item, e.g.
    `--scene missing-shears`.
    """

    name = "simulated"

    def __init__(self, seed: int | None = 1729, jitter: float = 0.03) -> None:
        self._random = random.Random(seed)
        self._jitter = jitter

    @staticmethod
    def scenes() -> dict[str, str]:
        return dict(_BUILTIN_SCENES)

    def infer(self, frame: Frame, manifest: Manifest) -> list[Sighting]:
        scene = frame.image
        if not isinstance(scene, str):
            raise InferenceError(
                "SimulatedEngine expects a ScriptedSource frame carrying a "
                "scene name, but received raw image data. Pair --engine "
                "simulated with --scene, or use --engine geniex with a camera."
            )

        if scene == ENGINE_FAULT:
            raise InferenceError("simulated NPU fault: Hexagon context lost")

        if scene == GARBLED:
            # The engine ran and returned something unusable. Upstream this is
            # indistinguishable from a real model apologising in prose.
            raise InferenceError(
                "model reply was unusable: no JSON object found in reply: "
                "'The kit appears to be in good order, I think.'"
            )

        if scene == EMPTY:
            return [
                self._sighting(key, Presence.ABSENT, 0.93) for key in manifest.keys
            ]

        if scene == OCCLUDED:
            # Nothing is asserted either way - the strongest test of the
            # safety rule, because a naive implementation reads "no failures
            # reported" as a pass.
            return [
                self._sighting(key, Presence.UNREADABLE, 0.88, "occluded")
                for key in manifest.keys
            ]

        if scene == LOW_CONFIDENCE:
            floor = manifest.confidence_floor
            return [
                self._sighting(key, Presence.FOUND, max(0.0, floor - 0.15))
                for key in manifest.keys
            ]

        if scene == COMPLETE:
            return [
                self._sighting(key, Presence.FOUND, 0.94) for key in manifest.keys
            ]

        for prefix, presence in (
            ("missing-", Presence.ABSENT),
            ("damaged-", Presence.DAMAGED),
        ):
            if scene.startswith(prefix):
                target = scene[len(prefix) :]
                if manifest.item(target) is None:
                    raise InferenceError(
                        f"scene {scene!r} refers to {target!r}, which is not on "
                        f"manifest {manifest.manifest_id!r}. Items: "
                        f"{', '.join(manifest.keys)}"
                    )
                return [
                    self._sighting(
                        key,
                        presence if key == target else Presence.FOUND,
                        0.93,
                        "simulated defect" if key == target else "",
                    )
                    for key in manifest.keys
                ]

        raise InferenceError(
            f"unknown scene {scene!r}. Built-in scenes: "
            f"{', '.join(sorted(_BUILTIN_SCENES))}; or missing-<key> / "
            f"damaged-<key> for {', '.join(manifest.keys)}"
        )

    def _sighting(
        self, key: str, presence: Presence, confidence: float, note: str = ""
    ) -> Sighting:
        wobble = self._random.uniform(-self._jitter, self._jitter)
        return Sighting(
            key=key,
            presence=presence,
            confidence=max(0.0, min(1.0, confidence + wobble)),
            note=note,
        )
