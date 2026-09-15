"""Environment checks, for the first ten minutes on unfamiliar hardware.

This exists because of a specific situation: a Snapdragon X Elite AI PC and an
Arduino UNO Q you have never touched, a room full of other people, and a
limited amount of bench time. The questions that eat that time are always the
same ones - is GenieX installed, which COM port is the board on, does the
camera open, will the console start - and every one of them is answerable in
software before anyone starts guessing.

Every check reports what it found and, when something is missing, the exact
command that fixes it. Nothing here changes anything; it only looks.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import platform
import shutil
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    """Works, but something downstream will be limited."""

    FAIL = "fail"
    """This blocks the path it belongs to."""

    INFO = "info"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: Status
    detail: str
    remedy: str = ""
    items: tuple[str, ...] = field(default=())


def run_checks(probe_cameras: bool = False) -> list[Check]:
    """Everything, in the order it matters on the day."""
    return [
        _platform(),
        _python(),
        *_package("geniex", "on-device inference", "pip install geniex",
                  blocking=True),
        _serial_ports(),
        *_package("cv2", "camera capture", 'pip install -e ".[host]"',
                  blocking=True, distribution="opencv-python"),
        *(_cameras() if probe_cameras else []),
        *_package("fastapi", "operator console", 'pip install -e ".[console]"',
                  blocking=False),
        _compiler(),
        _manifests(),
        _records_writable(),
    ]


# -- individual checks -------------------------------------------------------


def _platform() -> Check:
    machine = platform.machine()
    system = platform.system()
    arm = machine.lower() in {"arm64", "aarch64"}
    detail = f"{system} {platform.release()} on {machine}"

    if system == "Windows" and arm:
        return Check("Platform", Status.OK, detail + " - Snapdragon host")
    if arm:
        return Check("Platform", Status.OK, detail)
    return Check(
        "Platform",
        Status.INFO,
        detail,
        "Not an ARM64 host. Simulation runs fine here; GenieX will not.",
    )


TESTED_PYTHON = ((3, 11), (3, 13))


def _python() -> Check:
    """An older interpreter cannot get this far - the package requires 3.11 and
    would fail at import - so the only useful signal is running ahead of what
    CI covers."""
    version = ".".join(str(v) for v in sys.version_info[:3])
    if sys.version_info[:2] > TESTED_PYTHON[1]:
        return Check(
            "Python",
            Status.WARN,
            f"{version} - newer than CI tests (3.11-3.13)",
            "Likely fine, but nothing here has been verified on it.",
        )
    return Check("Python", Status.OK, f"{version} at {sys.executable}")


def _package(
    module: str,
    purpose: str,
    remedy: str,
    blocking: bool,
    distribution: str | None = None,
) -> list[Check]:
    label = distribution or module
    try:
        importlib.import_module(module)
    except Exception as exc:
        return [
            Check(
                label,
                Status.FAIL if blocking else Status.WARN,
                f"not importable - {purpose} unavailable ({type(exc).__name__})",
                remedy,
            )
        ]

    try:
        version = importlib.metadata.version(label)
    except importlib.metadata.PackageNotFoundError:
        version = "unknown version"
    return [Check(label, Status.OK, f"{version} - {purpose}")]


def _serial_ports() -> Check:
    """The question that actually wastes bench time: which port is the board?"""
    try:
        from serial.tools import list_ports
    except ImportError:
        return Check(
            "Serial ports",
            Status.FAIL,
            "pyserial not installed - cannot reach the actuator node",
            'pip install -e ".[host]"',
        )

    ports = list(list_ports.comports())
    if not ports:
        return Check(
            "Serial ports",
            Status.WARN,
            "none found",
            "Plug in the UNO Q and re-run. On Windows it appears as COMn.",
        )

    described = []
    likely = []
    for port in ports:
        line = f"{port.device} - {port.description}"
        described.append(line)
        haystack = f"{port.description} {port.manufacturer or ''}".lower()
        if any(k in haystack for k in ("arduino", "qualcomm", "usb serial", "acm")):
            likely.append(port.device)

    remedy = (
        f"Looks like the board: --port {likely[0]}"
        if likely
        else "No obviously-Arduino port; try each with `readykit inspect --link serial`."
    )
    return Check(
        "Serial ports",
        Status.OK,
        f"{len(ports)} found",
        remedy,
        tuple(described),
    )


def _cameras(limit: int = 3) -> list[Check]:
    try:
        import cv2
    except ImportError:
        return []

    found = []
    for index in range(limit):
        capture = cv2.VideoCapture(index)
        try:
            if capture.isOpened():
                ok, frame = capture.read()
                if ok and frame is not None:
                    h, w = frame.shape[:2]
                    found.append(f"index {index} - {w}x{h}")
        finally:
            capture.release()

    if not found:
        return [
            Check(
                "Cameras",
                Status.WARN,
                f"none responded on indices 0-{limit - 1}",
                "Plug in the USB camera, or use --engine simulated --scene.",
            )
        ]
    return [
        Check(
            "Cameras",
            Status.OK,
            f"{len(found)} responding",
            f"Use --camera {found[0].split()[1]}",
            tuple(found),
        )
    ]


def _compiler() -> Check:
    """Only the firmware conformance test needs this, so its absence is a
    warning rather than a failure - but it is the test that proves the C
    parser and the Python encoder still agree."""
    for candidate in ("c++", "g++", "clang++", "cl"):
        path = shutil.which(candidate)
        if path:
            return Check("C++ compiler", Status.OK, f"{candidate} at {path}")
    return Check(
        "C++ compiler",
        Status.WARN,
        "none found - the firmware conformance test will skip",
        "Everything else runs. Install one to re-verify the protocol agreement.",
    )


def _manifests() -> Check:
    from .domain import Manifest

    root = Path("manifests")
    if not root.is_dir():
        return Check(
            "Manifests",
            Status.WARN,
            "no manifests/ directory here",
            "Run from the repository root.",
        )

    good, bad = [], []
    for path in sorted(root.glob("*.json")):
        try:
            manifest = Manifest.from_json(path.read_text(encoding="utf-8"))
            good.append(f"{path.name} - {len(manifest.items)} items")
        except (ValueError, OSError) as exc:
            bad.append(f"{path.name} - {exc}")

    if bad:
        return Check(
            "Manifests",
            Status.FAIL,
            f"{len(bad)} will not parse",
            "Fix before the demo - a bad manifest is a hard error at startup.",
            tuple(bad + good),
        )
    if not good:
        return Check("Manifests", Status.WARN, "none found", "")
    return Check("Manifests", Status.OK, f"{len(good)} parse", "", tuple(good))


def _records_writable() -> Check:
    target = Path("records")
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check(
            "Audit log",
            Status.FAIL,
            f"cannot write to {target.resolve()} - {exc}",
            "Inspections would run but leave no record.",
        )
    return Check("Audit log", Status.OK, f"writable at {target.resolve()}")


def worst(checks: list[Check]) -> Status:
    for status in (Status.FAIL, Status.WARN):
        if any(c.status is status for c in checks):
            return status
    return Status.OK


def venv_prefix() -> str:
    """How this project's commands should be typed on this platform.

    The docs are written POSIX-first, and on a Windows ARM64 Snapdragon host
    every one of them is wrong in the same small way.
    """
    if os.name == "nt":
        return r".venv\Scripts\readykit"
    return ".venv/bin/readykit"
