"""A simulated engine, so the whole pipeline runs with no NPU and no camera.

This is not a toy. It exists to exercise the paths that are hardest to produce
on demand with real hardware and that matter most: an occluded lens, a model
that returns prose, a model that omits an item, a model that is confidently
wrong.

Scenes emit **realistic raw model output** - a prose narration followed by a
JSON block, which is what instruction-tuned VLMs actually do - and that text is
then run through the real `readykit.reply.parse_reply`. Two consequences worth
knowing:

* The simulated path exercises the production parser rather than bypassing it,
  so a parser bug shows up in simulation.
* `readykit.naive` can be replayed against genuine model text, which is what
  makes the comparison against the original blueprint honest rather than a
  strawman. The prose is written the way a model actually phrases these
  findings, not the way that would most embarrass the blueprint - and on two
  scenes the blueprint accordingly gets the right answer.

Scenes are deterministic so demos and tests are reproducible.
"""

from __future__ import annotations

import json
import random

from ..capture import Frame
from ..domain import Manifest, Presence
from ..reply import ReplyParseError, parse_reply
from .base import InferenceEngine, InferenceError, Observation

COMPLETE = "complete"
COMPLETE_NEGATED = "complete-negated"
OCCLUDED = "occluded"
EMPTY = "empty"
LOW_CONFIDENCE = "low-confidence"
GARBLED = "garbled"
ENGINE_FAULT = "engine-fault"

_BUILTIN_SCENES = {
    COMPLETE: "every required item present and clearly visible",
    COMPLETE_NEGATED: 'a complete kit, reported as "no items are missing"',
    OCCLUDED: "the tray is covered - nothing can be confirmed either way",
    EMPTY: "the kit is missing everything",
    LOW_CONFIDENCE: "a poor viewing angle - readings below the confidence floor",
    GARBLED: "the model returned prose instead of JSON",
    ENGINE_FAULT: "the NPU context died mid-inference",
}


class SimulatedEngine(InferenceEngine):
    """Produces model output from a scene name rather than from pixels.

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

    def infer(self, frame: Frame, manifest: Manifest) -> Observation:
        scene = frame.image
        if not isinstance(scene, str):
            raise InferenceError(
                "SimulatedEngine expects a ScriptedSource frame carrying a "
                "scene name, but received raw image data. Pair --engine "
                "simulated with --scene, or use --engine geniex with a camera."
            )

        if scene == ENGINE_FAULT:
            # The NPU died before producing any text at all. There is nothing
            # for either parser to read, so no comparison is possible.
            raise InferenceError("simulated NPU fault: Hexagon context lost")

        raw = self._compose_reply(scene, manifest)

        try:
            sightings = parse_reply(raw, manifest)
        except ReplyParseError as exc:
            raise InferenceError(
                f"model reply was unusable: {exc}", raw_reply=raw
            ) from exc

        return Observation(sightings=tuple(sightings), raw_reply=raw)

    # -- scene composition ---------------------------------------------------

    def _compose_reply(self, scene: str, manifest: Manifest) -> str:
        if scene == GARBLED:
            # Prose only, no JSON. The single most common real failure.
            return "The kit appears to be in good order, I think."

        if scene == COMPLETE:
            return self._reply(
                "All required items are present and appear serviceable.",
                {key: (Presence.FOUND, 0.94) for key in manifest.keys},
            )

        if scene == COMPLETE_NEGATED:
            # A compliant kit, phrased as a denial. The blueprint trips over
            # its own trigger words here and rejects a good kit.
            return self._reply(
                "No items are missing from this kit; everything is present.",
                {key: (Presence.FOUND, 0.94) for key in manifest.keys},
            )

        if scene == EMPTY:
            return self._reply(
                "The tray is empty. Every required item is missing.",
                {key: (Presence.ABSENT, 0.93) for key in manifest.keys},
            )

        if scene == OCCLUDED:
            return self._reply(
                "The tray is obscured; I am unable to assess its contents.",
                {key: (Presence.UNREADABLE, 0.88) for key in manifest.keys},
            )

        if scene == LOW_CONFIDENCE:
            floor = max(0.0, manifest.confidence_floor - 0.15)
            return self._reply(
                "The image is dim and my assessment is tentative.",
                {key: (Presence.FOUND, floor) for key in manifest.keys},
            )

        # Phrased without singular/plural agreement - "Trauma Shears" and
        # "Hard Hat" both have to read correctly, and a model listing findings
        # writes them this way anyway.
        for prefix, presence, phrasing in (
            ("missing-", Presence.ABSENT, "Absent from the tray"),
            ("damaged-", Presence.DAMAGED, "Damaged and unserviceable"),
        ):
            if scene.startswith(prefix):
                target = scene[len(prefix) :]
                item = manifest.item(target)
                if item is None:
                    raise InferenceError(
                        f"scene {scene!r} refers to {target!r}, which is not on "
                        f"manifest {manifest.manifest_id!r}. Items: "
                        f"{', '.join(manifest.keys)}"
                    )
                return self._reply(
                    f"{phrasing}: {item.label}. "
                    "All other required items are present.",
                    {
                        key: (
                            presence if key == target else Presence.FOUND,
                            0.93,
                        )
                        for key in manifest.keys
                    },
                )

        raise InferenceError(
            f"unknown scene {scene!r}. Built-in scenes: "
            f"{', '.join(sorted(_BUILTIN_SCENES))}; or missing-<key> / "
            f"damaged-<key> for {', '.join(manifest.keys)}"
        )

    def _reply(
        self, narration: str, readings: dict[str, tuple[Presence, float]]
    ) -> str:
        """Prose narration followed by a fenced JSON block.

        The `note` field is omitted rather than emitted empty - both because
        that is what a real model does, and because a field that is always
        present would make the naive comparison an artifact of this module's
        schema rather than of the blueprint's logic.
        """
        items = [
            {
                "key": key,
                "presence": presence.value,
                "confidence": round(self._wobble(confidence), 3),
            }
            for key, (presence, confidence) in readings.items()
        ]
        block = json.dumps({"items": items}, indent=2)
        return f"{narration}\n\n```json\n{block}\n```"

    def _wobble(self, confidence: float) -> float:
        drift = self._random.uniform(-self._jitter, self._jitter)
        return max(0.0, min(1.0, confidence + drift))
