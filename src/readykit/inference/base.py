"""The engine interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..capture import Frame
from ..domain import Manifest, Sighting


@dataclass(frozen=True, slots=True)
class Observation:
    """What an engine saw, plus what the model verbatim said.

    The raw reply is kept for two reasons. It goes into the Inspection Record,
    because "the model cleared this kit" is a weak claim unless you can show
    its actual words. And it is what `readykit.naive` is replayed against, so
    the comparison against the original blueprint runs on real model output
    rather than on a reconstruction of it.
    """

    sightings: tuple[Sighting, ...] = ()
    raw_reply: str = ""


class InferenceError(RuntimeError):
    """Inference could not be completed.

    Always resolves to INDETERMINATE upstream. A model that crashed has not
    cleared a kit.

    Carries `raw_reply` when the model did produce output that simply could
    not be used - an apology, prose instead of JSON. That text is exactly the
    input the blueprint's substring matcher would have acted on, so it is
    preserved rather than discarded.

    `raw_reply` stays None when no text existed at all - the NPU context died,
    the model never ran. That is a different situation from a model returning
    an empty string, and only the latter is something the blueprint could have
    parsed, so the two must not be conflated.
    """

    def __init__(self, message: str, raw_reply: str | None = None) -> None:
        super().__init__(message)
        self.raw_reply = raw_reply


class InferenceEngine(ABC):
    """Turns a Frame into an Observation against a Manifest."""

    name: str = "unknown"

    @abstractmethod
    def infer(self, frame: Frame, manifest: Manifest) -> Observation:
        """Observe the frame. Raise InferenceError if observation failed.

        An Observation with no sightings is legitimate and distinct from
        raising: it means the model ran and committed to nothing. Both keep the
        latch engaged.
        """

    def close(self) -> None:  # noqa: B027 - optional hook, the simulator holds nothing
        """Release the NPU context or model handle."""

    def __enter__(self) -> InferenceEngine:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
