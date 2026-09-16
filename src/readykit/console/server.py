"""The operator console - a local web view of the inspection pipeline.

Served on localhost only. This is an air-gapped device; the console is a
window onto it, not a remote control plane, and it deliberately binds to the
loopback interface so that plugging the device into a network does not expose
a latch-release endpoint.

Visual language: DESIGN.md at the repo root.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..bridge import LoopbackLink, VirtualActuatorNode
from ..bridge.base import HostLink
from ..bridge.loopback import Indicator, LatchState
from ..capture import FrameSource, ScriptedSource
from ..domain import Manifest, Verdict
from ..engine import InspectionEngine
from ..inference.base import InferenceEngine
from ..inference.simulated import SimulatedEngine
from ..protocol import Command
from ..recorder import InspectionLog

STATIC = Path(__file__).parent / "static"


@dataclass
class ConsoleState:
    """Everything the console needs to render, in one place.

    Guarded by a lock: inspections are triggered from request handlers and the
    latch state is read by pollers, so two requests can touch this at once.
    """

    manifest: Manifest
    log: InspectionLog
    node: VirtualActuatorNode | None
    scene: str = "complete"
    last: dict[str, Any] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


def create_app(
    manifest: Manifest,
    log_path: Path,
    source_factory: Any = None,
    engine_factory: Any = None,
    link: HostLink | None = None,
    source_label: str = "",
    engine_label: str = "",
) -> Any:
    """Build the FastAPI app.

    FastAPI is imported here rather than at module load so the core package
    installs without the console extras.

    Pass `source_factory` to inspect real frames and `engine_factory` to run a
    real model; either one makes the console *live*, and the labels are what it
    says it is looking at. A factory is called once per inspection, so hand in
    one that returns the same camera rather than opening the device again every
    time somebody presses the button.
    """
    live = source_factory is not None or engine_factory is not None
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import FileResponse, JSONResponse
    except ImportError as exc:  # pragma: no cover - depends on console extras
        raise RuntimeError(
            "FastAPI is not installed. Install the console extras: "
            'pip install -e ".[console]"'
        ) from exc

    node = link.node if isinstance(link, LoopbackLink) else None
    state = ConsoleState(
        manifest=manifest, log=InspectionLog(log_path), node=node
    )

    app = FastAPI(title="ReadyKit Edge", docs_url=None, redoc_url=None)

    def build_engine(scene: str) -> InspectionEngine:
        source: FrameSource = (
            source_factory() if source_factory else ScriptedSource(scene)
        )
        engine: InferenceEngine = (
            engine_factory() if engine_factory else SimulatedEngine(seed=None)
        )
        return InspectionEngine(manifest, source, engine, link)

    # The console must never serve a stale asset. A cached index.html against
    # a fresh console.js is how a page half-updates and throws, which is not
    # something to discover during a demo - and this is a local, air-gapped
    # device where caching buys nothing anyway.
    no_store = {"Cache-Control": "no-store, max-age=0"}

    @app.get("/")
    def index() -> Any:
        return FileResponse(STATIC / "index.html", headers=no_store)

    @app.get("/static/{filename}")
    def static_asset(filename: str) -> Any:
        target = (STATIC / filename).resolve()
        if target.parent != STATIC.resolve() or not target.is_file():
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(target, headers=no_store)

    @app.get("/api/manifest")
    def get_manifest() -> Any:
        return {
            "manifest_id": manifest.manifest_id,
            "name": manifest.name,
            "confidence_floor": manifest.confidence_floor,
            "hold_seconds": manifest.hold_seconds,
            "items": [
                {
                    "key": item.key,
                    "label": item.label,
                    "severity": item.severity.value,
                    "quantity": item.quantity,
                    "expiry_checked": item.expiry_checked,
                }
                for item in manifest.items
            ],
        }

    @app.get("/api/source")
    def get_source() -> Any:
        """What this console is actually looking at.

        The page asks before it draws the controls. A scene picker in front of
        a live camera would be the same lie as a simulated latch beside a real
        one: the operator reads the dropdown, believes the input is scripted,
        and trusts the verdict less than it deserves - or more.
        """
        return {
            "live": live,
            "source": source_label or "scripted scenes",
            "engine": engine_label or "simulated model",
        }

    @app.get("/api/scenes")
    def get_scenes() -> Any:
        # A live console has no scenes to offer. Returning the scripted list
        # anyway would invite a caller to pick one that cannot be honoured.
        if live:
            return {"scenes": []}
        builtin = [
            {"name": name, "description": description}
            for name, description in sorted(SimulatedEngine.scenes().items())
        ]
        per_item = [
            {
                "name": f"missing-{item.key}",
                "description": f"{item.label} removed from the kit",
            }
            for item in manifest.items
        ]
        return {"scenes": builtin + per_item}

    @app.post("/api/inspect")
    def inspect(body: dict[str, Any] | None = None) -> Any:
        requested = (body or {}).get("scene")
        if live:
            # Refused rather than ignored. Accepting a scene and then looking
            # at the camera instead would report a verdict about one thing
            # under the name of another.
            if requested is not None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"this console inspects {source_label or 'real frames'}; "
                        "a scene cannot be selected"
                    ),
                )
            scene = state.scene
        else:
            scene = requested if requested is not None else state.scene
            if not isinstance(scene, str) or not scene:
                raise HTTPException(
                    status_code=400, detail="scene must be a string"
                )

        with state.lock:
            state.scene = scene
            engine = build_engine(scene)
            outcome = engine.run_once()
            state.log.append(outcome.record)

            payload = _serialise(outcome, manifest)
            state.last = payload
            return JSONResponse(payload)

    @app.get("/api/state")
    def get_state() -> Any:
        with state.lock:
            # The console process IS the Inspection Host, and it is alive - so
            # it must heartbeat like one. Without this the node's watchdog
            # trips between inspections and the console reports a stale link
            # while the host is sitting right here polling it.
            if link is not None:
                link.send(Command.PING)
            if state.node is not None:
                state.node.tick()
            return {
                "last": state.last,
                "scene": state.scene,
                "live": live,
                "telemetry": _telemetry(state.node),
                "tally": state.log.tally(),
                "blueprint_tally": _blueprint_tally(state.log),
                "chain": _chain(state.log),
            }

    @app.get("/api/records")
    def get_records(limit: int = 25) -> Any:
        return {"records": state.log.read(limit=max(1, min(limit, 200)))}

    return app


def _serialise(outcome: Any, manifest: Manifest) -> dict[str, Any]:
    record = outcome.record
    resolution = record.resolution
    blamed = (
        set(resolution.missing)
        | set(resolution.damaged)
        | set(resolution.expired)
        | set(resolution.short)
    )
    unresolved = set(resolution.unresolved)
    expired = set(resolution.expired)
    soon = set(resolution.expiring_soon)
    short = set(resolution.short)

    sightings = {s.key: s for s in record.sightings}
    items = []
    for item in manifest.items:
        sighting = sightings.get(item.key)
        items.append(
            {
                "key": item.key,
                "label": item.label,
                "severity": item.severity.value,
                "presence": sighting.presence.value if sighting else "unreported",
                "confidence": round(sighting.confidence, 3) if sighting else None,
                "note": sighting.note if sighting else "",
                "blamed": item.key in blamed,
                "unresolved": item.key in unresolved or sighting is None,
                "expiry_checked": item.expiry_checked,
                "expiry": (
                    sighting.expiry.isoformat()
                    if sighting and sighting.expiry
                    else None
                ),
                "expired": item.key in expired,
                "expiring_soon": item.key in soon,
                "count": sighting.count if sighting else None,
                "required": item.quantity,
                "short": item.key in short,
            }
        )

    return {
        "inspection_id": record.inspection_id,
        "verdict": resolution.verdict.value,
        "reason": resolution.reason,
        "items": items,
        "advisories": list(resolution.advisories),
        "expired": list(resolution.expired),
        "expiring_soon": list(resolution.expiring_soon),
        "short": list(resolution.short),
        "commanded": record.commanded,
        "enacted": outcome.enacted,
        "engine": record.engine,
        "latency_ms": round(record.latency_ms, 1),
        "frame_digest": record.frame_digest,
        "started_at": record.started_at.astimezone(UTC).isoformat(),
        "latch_expected": (
            "released" if resolution.verdict is Verdict.PASS and outcome.enacted
            else "engaged"
        ),
        "raw_reply": record.raw_reply,
        "blueprint": _blueprint(outcome),
    }


def _blueprint(outcome: Any) -> dict[str, Any] | None:
    """What the original design would have done with this same model reply."""
    comparison = outcome.comparison
    if comparison is None:
        return None
    return {
        "signal": comparison.signal,
        "unlocks": comparison.blueprint_unlocks,
        "divergence": comparison.divergence.value,
        "reason": comparison.reason,
    }


def _chain(log: InspectionLog) -> dict[str, Any]:
    """Whether the audit trail still verifies.

    Surfaced live rather than only on demand: a broken chain means the record
    of what this device decided can no longer be trusted, and that is not
    something an operator should have to run a command to discover.
    """
    result = log.verify()
    return {
        "status": result.status.value,
        "ok": result.ok,
        "verified": result.verified,
        "total": result.total,
        "detail": result.detail,
    }


def _blueprint_tally(log: InspectionLog) -> dict[str, int]:
    """How often the original design would have opened an enclosure it
    should not have, across everything recorded so far."""
    counts = {"agreed": 0, "unsafe": 0, "spurious": 0, "not_comparable": 0}
    for row in log.read():
        divergence = row.get("blueprint_divergence") or "not_comparable"
        if divergence in counts:
            counts[divergence] += 1
    return counts


def _telemetry(node: VirtualActuatorNode | None) -> dict[str, Any]:
    if node is None:
        return {
            "available": False,
            "latch": "unknown",
            "indicator": "unknown",
            "link_stale": None,
            "events": [],
        }
    return {
        "available": True,
        "latch": LatchState(node.latch).value,
        "indicator": Indicator(node.indicator).value,
        "link_stale": node.link_is_stale,
        "link_timeout_ms": node.link_timeout_ms,
        "buzzer": node.buzzer,
        "last_seq": node.last_seq,
        "events": node.events[-8:],
        "now": datetime.now(UTC).isoformat(),
    }
