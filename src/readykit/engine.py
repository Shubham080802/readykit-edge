"""The inspection loop - capture, infer, resolve, enact, record.

This module is deliberately boring. Every interesting decision has already
been made by `resolve_verdict`, and this just carries it out. The one rule it
adds is that **every failure of its own machinery resolves to INDETERMINATE**:
a dead camera, a crashed NPU, and an unparseable reply all keep the Latch
engaged, and all say so in the Inspection Record.

It also replays `readykit.naive` - the original blueprint's substring matcher -
against the same model reply, purely to record what that design would have
done. That result is written to the Inspection Record and shown in the console.
It never touches the Host Link.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic

from .aggregate import DEFAULT_AGREEMENT, aggregate_sightings
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
from .naive import Comparison, compare
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
    comparison: Comparison | None = None
    """What the original blueprint would have done with the same reply, or
    None when there was no reply for it to parse."""

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


@dataclass(frozen=True, slots=True)
class _Observed:
    frame: Frame | None
    resolution: Resolution
    sightings: list[Sighting]
    raw_reply: str | None


class InspectionEngine:
    """Runs Inspections against one Manifest."""

    def __init__(
        self,
        manifest: Manifest,
        source: FrameSource,
        engine: InferenceEngine,
        link: HostLink | None = None,
        frames: int = 1,
        min_agreement: float = DEFAULT_AGREEMENT,
    ) -> None:
        self.manifest = manifest
        self.source = source
        self.engine = engine
        self.link = link
        self.frames = max(1, frames)
        self.min_agreement = min_agreement
        self.latencies_ms: list[float] = []
        """Every inference latency this engine has measured, for `readykit
        bench` and the console's NPU panel. Measured, never estimated."""

    def run_once(self) -> InspectionOutcome:
        started_at = datetime.now(UTC)
        began = monotonic()

        observed = self._observe()
        latency_ms = (monotonic() - began) * 1000.0

        link_result = self._enact(observed.resolution)

        # Replayed for the record only. `compare` cannot actuate - it is a pure
        # function returning a dataclass, and nothing downstream of here reads
        # it when deciding what to send.
        comparison = (
            compare(observed.raw_reply, observed.resolution.verdict)
            if observed.raw_reply is not None
            else None
        )

        record = InspectionRecord(
            inspection_id=uuid.uuid4().hex[:12],
            manifest_id=self.manifest.manifest_id,
            started_at=started_at,
            resolution=observed.resolution,
            sightings=tuple(observed.sightings),
            commanded=(
                link_result.describe()
                if link_result is not None
                else "no host link configured - nothing was actuated"
            ),
            engine=self.engine.name,
            latency_ms=latency_ms,
            frame_digest=observed.frame.digest if observed.frame else "",
            raw_reply=observed.raw_reply or "",
            blueprint_signal=comparison.signal if comparison else "",
            blueprint_divergence=(
                comparison.divergence.value if comparison else ""
            ),
        )
        return InspectionOutcome(
            record=record, link_result=link_result, comparison=comparison
        )

    def _observe(self) -> _Observed:
        """Capture and infer, converting every failure into INDETERMINATE."""
        try:
            frame = self.source.read()
        except CaptureError as exc:
            return _Observed(
                None, _indeterminate(f"Capture failed: {exc}", self.manifest), [], None
            )

        try:
            began = monotonic()
            observation = self.engine.infer(frame, self.manifest)
            self.latencies_ms.append((monotonic() - began) * 1000.0)
        except InferenceError as exc:
            return _Observed(
                frame,
                _indeterminate(f"Inference failed: {exc}", self.manifest),
                [],
                exc.raw_reply,
            )
        except Exception as exc:
            # An engine that raised something unexpected is an engine we
            # cannot reason about. Same posture: do not open the latch.
            return _Observed(
                frame,
                _indeterminate(
                    f"Inference raised {type(exc).__name__}: {exc}", self.manifest
                ),
                [],
                None,
            )

        per_frame: list[list[Sighting]] = [list(observation.sightings)]
        raw = observation.raw_reply

        # Additional looks. A frame that fails to capture or infer is skipped
        # rather than fatal - we already have at least one good look, and
        # disagreement among the looks we did get is handled by aggregation.
        for _ in range(self.frames - 1):
            try:
                extra_frame = self.source.read()
                began = monotonic()
                extra = self.engine.infer(extra_frame, self.manifest)
                self.latencies_ms.append((monotonic() - began) * 1000.0)
            except (CaptureError, InferenceError):
                continue
            per_frame.append(list(extra.sightings))

        sightings = aggregate_sightings(per_frame, self.min_agreement)
        return _Observed(
            frame,
            resolve_verdict(self.manifest, sightings),
            sightings,
            raw,
        )

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
