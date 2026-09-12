"""The inspection loop - capture, infer, resolve, enact, record.

This module is deliberately boring. Every interesting decision has already
been made by `resolve_verdict`, and this just carries it out. The one rule it
adds is that **every failure of its own machinery resolves to INDETERMINATE**:
a dead camera, a crashed NPU, and an unparseable reply all keep the Latch
engaged, and all say so in the Inspection Record.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic

from .bridge.base import HostLink, LinkResult
from .capture import CaptureError, Frame, FrameSource
from .domain import (
    InspectionRecord,
    Manifest,
    Resolution,
    Sighting,
    Verdict,
    resolve_verdict,
)
from .inference.base import InferenceEngine, InferenceError
from .protocol import Command

_COMMAND_FOR = {
    Verdict.PASS: Command.RELEASE,
    Verdict.FAIL: Command.REJECT,
    Verdict.INDETERMINATE: Command.HOLD,
}


@dataclass(frozen=True, slots=True)
class InspectionOutcome:
    """One inspection, start to finish."""

    record: InspectionRecord
    link_result: LinkResult | None

    @property
    def verdict(self) -> Verdict:
        return self.record.resolution.verdict

    @property
    def enacted(self) -> bool:
        """Whether the Actuator Node acknowledged the Command.

        A PASS whose RELEASE was never acknowledged did not open anything, and
        must not be reported to the operator as though it did.
        """
        return self.link_result is not None and self.link_result.acknowledged


class InspectionEngine:
    """Runs Inspections against one Manifest."""

    def __init__(
        self,
        manifest: Manifest,
        source: FrameSource,
        engine: InferenceEngine,
        link: HostLink | None = None,
    ) -> None:
        self.manifest = manifest
        self.source = source
        self.engine = engine
        self.link = link

    def run_once(self) -> InspectionOutcome:
        started_at = datetime.now(UTC)
        began = monotonic()

        frame, resolution, sightings = self._observe()
        latency_ms = (monotonic() - began) * 1000.0

        link_result = self._enact(resolution)

        record = InspectionRecord(
            inspection_id=uuid.uuid4().hex[:12],
            manifest_id=self.manifest.manifest_id,
            started_at=started_at,
            resolution=resolution,
            sightings=tuple(sightings),
            commanded=(
                link_result.describe()
                if link_result is not None
                else "no host link configured - nothing was actuated"
            ),
            engine=self.engine.name,
            latency_ms=latency_ms,
            frame_digest=frame.digest if frame is not None else "",
        )
        return InspectionOutcome(record=record, link_result=link_result)

    def _observe(
        self,
    ) -> tuple[Frame | None, Resolution, list[Sighting]]:
        """Capture and infer, converting every failure into INDETERMINATE."""
        try:
            frame = self.source.read()
        except CaptureError as exc:
            return None, _indeterminate(f"Capture failed: {exc}", self.manifest), []

        try:
            sightings = self.engine.infer(frame, self.manifest)
        except InferenceError as exc:
            return frame, _indeterminate(f"Inference failed: {exc}", self.manifest), []
        except Exception as exc:
            # An engine that raised something unexpected is an engine we
            # cannot reason about. Same posture: do not open the latch.
            return (
                frame,
                _indeterminate(
                    f"Inference raised {type(exc).__name__}: {exc}", self.manifest
                ),
                [],
            )

        return frame, resolve_verdict(self.manifest, sightings), sightings

    def _enact(self, resolution: Resolution) -> LinkResult | None:
        if self.link is None:
            return None

        command = _COMMAND_FOR[resolution.verdict]
        payload = (
            f"{int(self.manifest.hold_seconds * 1000)}"
            if command is Command.RELEASE
            else resolution.reason[:60]
        )
        return self.link.send(command, payload)

    def heartbeat(self) -> LinkResult | None:
        """Keep the Actuator Node's watchdog satisfied.

        Must be called more often than the node's link timeout while a Latch
        is released, or the watchdog will cut the hold short.
        """
        return self.link.send(Command.PING) if self.link is not None else None

    def close(self) -> None:
        for closeable in (self.source, self.engine, self.link):
            if closeable is not None:
                closeable.close()

    def __enter__(self) -> InspectionEngine:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _indeterminate(reason: str, manifest: Manifest) -> Resolution:
    return Resolution(
        verdict=Verdict.INDETERMINATE,
        reason=reason,
        unresolved=manifest.keys,
    )
