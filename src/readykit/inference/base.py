"""The engine interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..capture import Frame
from ..domain import Manifest, Sighting


class InferenceError(RuntimeError):
    """Inference could not be completed.

    Always resolves to INDETERMINATE upstream. A model that crashed has not
    cleared a kit.
    """


class InferenceEngine(ABC):
    """Turns a Frame into Sightings against a Manifest."""

    name: str = "unknown"

    @abstractmethod
    def infer(self, frame: Frame, manifest: Manifest) -> list[Sighting]:
        """Observe the frame. Raise InferenceError if observation failed.

        Returning an empty list is legitimate and distinct from raising: it
        means the model ran and committed to nothing. Both keep the latch
        engaged.
        """

    def close(self) -> None:  # noqa: B027 - optional hook, the simulator holds nothing
        """Release the NPU context or model handle."""

    def __enter__(self) -> InferenceEngine:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
