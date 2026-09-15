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
import re
import shutil
import subprocess
import sys
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


class FfmpegCameraSource(FrameSource):
    """A camera via ffmpeg, for hosts where OpenCV will not install.

    `opencv-python` publishes no `win_arm64` wheel, and the Snapdragon X Elite
    is `win_arm64`. So on the machine this project actually targets,
    `CameraSource` cannot be used at all, and installing an x64 Python under
    emulation to get around it would break the GenieX runtime - the wrong
    trade.

    ffmpeg ships a native ARM64 Windows build and reads webcams on every
    platform, so it is shelled out to instead. One process per frame: about
    600ms on an M3 Pro, almost all of it spawning the process and opening the
    device rather than capturing, so the warmup frames below are close to
    free. Slower than holding a handle open, and worth it for capture that
    works on the target hardware at all.

    At that cost the camera is not the bottleneck - a 7B VLM takes longer per
    frame than this does - but it does put a floor under the sentinel's
    reaction time, so keep `--interval` above about a second when using it.

    It yields **encoded JPEG bytes**, not a pixel array, which suits both
    engines better than `CameraSource` does. GenieX takes image file paths and
    Ollama takes base64 - so a numpy array has to be re-encoded for either,
    while these bytes are already in the right shape.
    """

    def __init__(
        self,
        device: str = "0",
        warmup_frames: int = 4,
        quality: int = 3,
        framerate: int = 30,
        timeout: float = 30.0,
    ) -> None:
        if warmup_frames < 0:
            raise CaptureError(f"warmup_frames must be >= 0, got {warmup_frames}")
        self.device = device
        # The first frames off a webcam are underexposed while auto-gain
        # settles. Unlike the OpenCV source we cannot discard them once at
        # startup, because each read() is a fresh process - so every capture
        # grabs this many and keeps the last.
        self._frames = warmup_frames + 1
        self._quality = quality
        self._framerate = framerate
        self._timeout = timeout

        if shutil.which("ffmpeg") is None:
            raise CaptureError(
                "ffmpeg is not on PATH. Install it "
                "(winget install Gyan.FFmpeg / brew install ffmpeg), or run "
                "with --scene for the simulator."
            )

    def read(self) -> Frame:
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            *_ffmpeg_input_args(self.device, self._framerate),
            "-frames:v", str(self._frames),
            "-f", "image2pipe", "-vcodec", "mjpeg",
            "-q:v", str(self._quality),
            "pipe:1",
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, timeout=self._timeout, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise CaptureError(
                f"ffmpeg did not return a frame within {self._timeout:g}s. "
                "Another application may be holding the camera."
            ) from exc
        except OSError as exc:
            raise CaptureError(f"could not run ffmpeg: {exc}") from exc

        if result.returncode != 0:
            raise CaptureError(
                f"ffmpeg failed capturing from {self.device!r}: "
                f"{_last_error_line(result.stderr)}"
            )

        payload = _last_jpeg(result.stdout)
        if payload is None:
            raise CaptureError(
                f"ffmpeg produced no image from {self.device!r}. "
                f"{_last_error_line(result.stderr)}"
            )

        width, height = _jpeg_size(payload)
        return Frame(
            image=payload,
            digest=hashlib.blake2b(payload, digest_size=8).hexdigest(),
            width=width,
            height=height,
        )


def _ffmpeg_input_args(device: str, framerate: int) -> list[str]:
    """The platform's way of naming a camera to ffmpeg.

    Every platform spells this differently, and getting it wrong produces an
    ffmpeg error that reads like a missing file rather than a wrong flag.
    """
    if sys.platform == "darwin":
        return ["-f", "avfoundation", "-framerate", str(framerate), "-i", device]
    if sys.platform == "win32":
        # DirectShow wants the device's display name, not an index - the one
        # `readykit doctor` prints. Quoting is handled by not going through a
        # shell.
        name = device if device.startswith("video=") else f"video={device}"
        return ["-f", "dshow", "-i", name]
    node = device if device.startswith("/dev/") else f"/dev/video{device}"
    return ["-f", "v4l2", "-i", node]


def _last_jpeg(payload: bytes) -> bytes | None:
    """The final complete JPEG in an MJPEG stream.

    Frames arrive concatenated, so they are split on the start-of-image
    marker. That marker cannot occur inside entropy-coded data - a literal
    0xFF there is escaped as 0xFF00 - so this is exact rather than heuristic.

    The last frame is the one wanted: the earlier ones are the warmup.
    """
    marker = b"\xff\xd8\xff"
    index = payload.rfind(marker)
    if index < 0:
        return None
    frame = payload[index:]
    return frame if frame.endswith(b"\xff\xd9") else None


def _jpeg_size(payload: bytes) -> tuple[int, int]:
    """Width and height from the JPEG's frame header, or (0, 0).

    Worth recording: whether a printed expiry date is legible depends on how
    many pixels it occupies, and that question comes up every time a date
    reads as unverifiable.
    """
    index = 2
    end = len(payload)
    while index + 9 < end:
        if payload[index] != 0xFF:
            index += 1
            continue
        kind = payload[index + 1]
        # SOF0-SOF15 carry the dimensions; C4/C8/CC are other tables.
        if 0xC0 <= kind <= 0xCF and kind not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(payload[index + 5 : index + 7], "big")
            width = int.from_bytes(payload[index + 7 : index + 9], "big")
            return width, height
        if kind in (0xD8, 0x01) or 0xD0 <= kind <= 0xD7:
            index += 2
            continue
        segment = int.from_bytes(payload[index + 2 : index + 4], "big")
        if segment < 2:
            break
        index += 2 + segment
    return 0, 0


def _last_error_line(stderr: bytes) -> str:
    lines = [line.strip() for line in stderr.decode(errors="replace").splitlines()]
    meaningful = [line for line in lines if line]
    return meaningful[-1] if meaningful else "no error output"


def list_cameras() -> list[str]:
    """Camera names ffmpeg can see, for `readykit doctor`.

    Best-effort: ffmpeg prints these to stderr and exits non-zero by design,
    so a failure here means "could not enumerate", never "no cameras".
    """
    if shutil.which("ffmpeg") is None:
        return []
    if sys.platform == "darwin":
        probe = ["-f", "avfoundation", "-list_devices", "true", "-i", ""]
    elif sys.platform == "win32":
        probe = ["-f", "dshow", "-list_devices", "true", "-i", "dummy"]
    else:
        return sorted(str(path) for path in Path("/dev").glob("video*"))

    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", *probe],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    found: list[str] = []
    for line in result.stderr.decode(errors="replace").splitlines():
        if "audio devices" in line.lower():
            break
        match = re.search(r"\[(\d+)\]\s+(.+?)\s*$", line)
        if match and "video devices" not in line.lower():
            found.append(f"{match.group(1)}: {match.group(2)}")
    return found
