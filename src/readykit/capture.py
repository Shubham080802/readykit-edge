"""Frame sources.

A Frame is whatever the inference engine needs to look at, plus a digest so the
Inspection Record can refer to the exact image a Verdict was based on. The
digest matters for audit: "the model passed this kit" is a weak claim unless
you can say which frame it saw.

`opencv-python` is imported lazily so the domain and simulation paths run on a
machine with no camera stack installed.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Frame:
    """One captured image, ready for inference."""

    image: Any
    """The pixel payload. A numpy array from the camera, or a scene name in
    simulation - the engine paired with this source knows how to read it."""

    digest: str
    """Short content hash, recorded alongside the Verdict."""

    width: int = 0
    height: int = 0


class CaptureError(RuntimeError):
    """The frame source could not produce a frame.

    The engine turns this into INDETERMINATE: a camera that cannot see is
    indistinguishable, for safety purposes, from a kit that cannot be read.
    """


class FrameSource(ABC):
    """Where frames come from."""

    @abstractmethod
    def read(self) -> Frame:
        """Return the next frame, or raise CaptureError."""

    def close(self) -> None:  # noqa: B027 - optional hook, scripted sources hold nothing
        """Release any hardware. Safe to call more than once."""

    def __enter__(self) -> FrameSource:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class CameraSource(FrameSource):
    """A USB camera on the Inspection Host, via OpenCV."""

    def __init__(self, index: int = 0, warmup_frames: int = 5) -> None:
        try:
            import cv2
        except ImportError as exc:  # pragma: no cover - depends on host extras
            raise CaptureError(
                "opencv-python is not installed. Install the host extras: "
                'pip install -e ".[host]"'
            ) from exc

        self._cv2: Any = cv2
        self._capture: Any = cv2.VideoCapture(index)
        if not self._capture.isOpened():
            raise CaptureError(f"could not open camera at index {index}")

        # The first frames off a USB camera are typically underexposed while
        # auto-gain settles. Inspecting those produces avoidable
        # INDETERMINATE verdicts, so discard them at startup rather than
        # paying for them on every inspection.
        for _ in range(warmup_frames):
            self._capture.read()

    def read(self) -> Frame:
        ok, image = self._capture.read()
        if not ok or image is None:
            raise CaptureError("camera returned no frame")
        height, width = image.shape[:2]
        return Frame(
            image=image,
            digest=hashlib.blake2b(image.tobytes(), digest_size=8).hexdigest(),
            width=width,
            height=height,
        )

    def close(self) -> None:
        if getattr(self, "_capture", None) is not None:
            self._capture.release()
            self._capture = None


class ImageFileSource(FrameSource):
    """Replays still images from disk - regression fixtures, field captures."""

    def __init__(self, paths: list[Path], loop: bool = True) -> None:
        if not paths:
            raise CaptureError("ImageFileSource needs at least one path")
        self._paths = paths
        self._loop = loop
        self._index = 0

    def read(self) -> Frame:
        if self._index >= len(self._paths):
            if not self._loop:
                raise CaptureError("no frames remaining")
            self._index = 0

        path = self._paths[self._index]
        self._index += 1
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise CaptureError(f"could not read {path}: {exc}") from exc

        return Frame(
            image=payload,
            digest=hashlib.blake2b(payload, digest_size=8).hexdigest(),
        )


class ScriptedSource(FrameSource):
    """A named scene, for driving the pipeline with no camera present.

    The scene name is carried through to the simulated engine, which knows
    what that scene is supposed to contain. Pairing this with a real engine is
    a configuration error and is rejected there.
    """

    def __init__(self, scene: str) -> None:
        self._scene = scene

    def read(self) -> Frame:
        return Frame(
            image=self._scene,
            digest=hashlib.blake2b(
                self._scene.encode(), digest_size=8
            ).hexdigest(),
            width=1280,
            height=720,
        )
