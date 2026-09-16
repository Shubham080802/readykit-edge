"""Vercel entry point for the browser-safe ReadyKit Edge demonstration.

The deployed console uses scripted scenes, an animated simulated camera feed,
and a virtual actuator. It deliberately has no hardware configuration.
"""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

bridge = import_module("readykit.bridge")
console = import_module("readykit.console")
domain = import_module("readykit.domain")


manifest = domain.Manifest.from_json(
    (ROOT / "manifests" / "trauma-kit-a.json").read_text(encoding="utf-8")
)
app = console.create_app(
    manifest=manifest,
    log_path=Path("/tmp/readykit-inspections.jsonl"),
    link=bridge.open_link("loopback"),
    demo_camera=True,
)
