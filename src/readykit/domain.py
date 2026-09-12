"""Core domain types for ReadyKit Edge.

Vocabulary is defined in CONTEXT.md at the repo root. The names here are the
names there - if you rename something, rename it in both places.

This module is pure: no I/O, no hardware, no SDK imports. Everything that
touches the world lives behind an interface in `capture`, `inference`, or
`bridge`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class Presence(StrEnum):
    """What the model reports about one Required Item in one frame."""

    FOUND = "found"
    ABSENT = "absent"
    DAMAGED = "damaged"
    UNREADABLE = "unreadable"


class Verdict(StrEnum):
    """The outcome of an Inspection. Only PASS may release the Latch."""

    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"


class Severity(StrEnum):
    """How much a Required Item's absence matters."""

    CRITICAL = "critical"
    """Absence or damage disqualifies the whole Kit."""

    ADVISORY = "advisory"
    """Absence is recorded but does not by itself fail the Kit."""


@dataclass(frozen=True, slots=True)
class RequiredItem:
    """One entry in a Manifest - a thing that must be found in the Kit."""

    key: str
    """Stable identifier, used in the model reply and the Inspection Record."""

    label: str
    """Human-readable name, and what the model is actually asked about."""

    severity: Severity = Severity.CRITICAL

    quantity: int = 1

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("RequiredItem.key must not be empty")
        if self.quantity < 1:
            raise ValueError(
                f"RequiredItem {self.key!r} quantity must be >= 1, got {self.quantity}"
            )


@dataclass(frozen=True, slots=True)
class Manifest:
    """The specification of what a compliant Kit contains."""

    manifest_id: str
    name: str
    items: tuple[RequiredItem, ...]

    confidence_floor: float = 0.55
    """A Sighting below this contributes INDETERMINATE, never FAIL."""

    hold_seconds: float = 5.0
    """How long the Latch stays Released after a Pass."""

    def __post_init__(self) -> None:
        if not self.items:
            raise ValueError(f"Manifest {self.manifest_id!r} has no items")
        if not 0.0 <= self.confidence_floor <= 1.0:
            raise ValueError(
                f"Manifest {self.manifest_id!r} confidence_floor must be in [0,1], "
                f"got {self.confidence_floor}"
            )
        if self.hold_seconds <= 0:
            raise ValueError(
                f"Manifest {self.manifest_id!r} hold_seconds must be > 0, "
                f"got {self.hold_seconds}"
            )
        seen: set[str] = set()
        for item in self.items:
            if item.key in seen:
                raise ValueError(
                    f"Manifest {self.manifest_id!r} has duplicate item key {item.key!r}"
                )
            seen.add(item.key)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.items)

    def item(self, key: str) -> RequiredItem | None:
        for candidate in self.items:
            if candidate.key == key:
                return candidate
        return None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> Manifest:
        """Build a Manifest from parsed JSON. Raises ValueError on bad input."""
        try:
            raw_items = raw["items"]
            if not isinstance(raw_items, list):
                raise ValueError("'items' must be a list")
            items = tuple(
                RequiredItem(
                    key=str(entry["key"]),
                    label=str(entry["label"]),
                    severity=Severity(str(entry.get("severity", "critical"))),
                    quantity=_as_int(entry.get("quantity", 1)),
                )
                for entry in raw_items
            )
            return cls(
                manifest_id=str(raw["manifest_id"]),
                name=str(raw["name"]),
                items=items,
                confidence_floor=_as_float(raw.get("confidence_floor", 0.55)),
                hold_seconds=_as_float(raw.get("hold_seconds", 5.0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Malformed manifest: {exc}") from exc

    @classmethod
    def from_json(cls, text: str) -> Manifest:
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Manifest is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("Manifest JSON must be an object")
        return cls.from_mapping(raw)


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"expected a number, got {value!r}")
    return int(value)


def _as_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"expected a number, got {value!r}")
    return float(value)


@dataclass(frozen=True, slots=True)
class Sighting:
    """What the model reports about one Required Item. An observation, never a
    decision - `resolve_verdict` is the only thing that decides."""

    key: str
    presence: Presence
    confidence: float
    note: str = ""

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"Sighting {self.key!r} confidence must be in [0,1], "
                f"got {self.confidence}"
            )


@dataclass(frozen=True, slots=True)
class Resolution:
    """A Verdict plus the reasoning that produced it, ready to be recorded."""

    verdict: Verdict
    reason: str
    missing: tuple[str, ...] = ()
    damaged: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    """Required Items the model could not establish either way. Non-empty
    unresolved always means INDETERMINATE."""

    advisories: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class InspectionRecord:
    """The durable, append-only account of one Inspection."""

    inspection_id: str
    manifest_id: str
    started_at: datetime
    resolution: Resolution
    sightings: tuple[Sighting, ...]
    commanded: str
    """The Command actually acknowledged by the Actuator Node, or a reason it
    was not. Never what we merely intended to send."""

    engine: str = "unknown"
    latency_ms: float = 0.0
    frame_digest: str = ""

    raw_reply: str = ""
    """What the model actually said, verbatim. "The model cleared this kit" is
    a weak claim unless its own words are on the record."""

    blueprint_signal: str = ""
    """What the original blueprint's substring matcher would have commanded on
    this same reply, or empty when there was no reply to parse."""

    blueprint_divergence: str = ""
    """agreed | unsafe | spurious. `unsafe` means the blueprint would have
    released the latch where this system did not."""

    def to_json_line(self) -> str:
        payload = {
            "inspection_id": self.inspection_id,
            "manifest_id": self.manifest_id,
            "started_at": self.started_at.astimezone(UTC).isoformat(),
            "verdict": self.resolution.verdict.value,
            "reason": self.resolution.reason,
            "missing": list(self.resolution.missing),
            "damaged": list(self.resolution.damaged),
            "unresolved": list(self.resolution.unresolved),
            "advisories": list(self.resolution.advisories),
            "sightings": [
                {
                    "key": s.key,
                    "presence": s.presence.value,
                    "confidence": round(s.confidence, 4),
                    "note": s.note,
                }
                for s in self.sightings
            ],
            "commanded": self.commanded,
            "engine": self.engine,
            "latency_ms": round(self.latency_ms, 2),
            "frame_digest": self.frame_digest,
            "raw_reply": self.raw_reply,
            "blueprint_signal": self.blueprint_signal,
            "blueprint_divergence": self.blueprint_divergence,
        }
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def resolve_verdict(manifest: Manifest, sightings: Iterable[Sighting]) -> Resolution:
    """Decide a Verdict for one Kit against one Manifest.

    The whole safety posture of ReadyKit Edge is this function, and it rests on
    one rule: **absence of evidence is not evidence of compliance.** A Required
    Item the model did not speak to, spoke about unreadably, or spoke about
    below the Manifest's Confidence Floor is UNRESOLVED - and any unresolved
    item yields INDETERMINATE, which keeps the Latch engaged.

    PASS therefore requires a positive, confident FOUND for every critical item.
    It is never the fallthrough branch.
    """
    by_key: dict[str, Sighting] = {}
    for sighting in sightings:
        if manifest.item(sighting.key) is None:
            # The model spoke about something not on the Manifest. Not an error
            # - kits contain extra things - but it carries no weight here.
            continue
        existing = by_key.get(sighting.key)
        # If the model reported the same item twice, keep the least favourable
        # reading. Optimism must not win a tie.
        if existing is None or _pessimism(sighting) > _pessimism(existing):
            by_key[sighting.key] = sighting

    missing: list[str] = []
    damaged: list[str] = []
    unresolved: list[str] = []
    advisories: list[str] = []

    for item in manifest.items:
        found = by_key.get(item.key)

        if found is None:
            unresolved.append(item.key)
            continue

        if found.presence is Presence.UNREADABLE:
            unresolved.append(item.key)
            continue

        if found.confidence < manifest.confidence_floor:
            # Low-confidence ABSENT is not a failure - it is a bad look at the
            # kit. Ask again rather than accusing the operator.
            unresolved.append(item.key)
            continue

        if found.presence is Presence.FOUND:
            continue

        bucket = missing if found.presence is Presence.ABSENT else damaged
        if item.severity is Severity.CRITICAL:
            bucket.append(item.key)
        else:
            advisories.append(item.key)

    if unresolved:
        return Resolution(
            verdict=Verdict.INDETERMINATE,
            reason=_describe_unresolved(manifest, unresolved),
            missing=tuple(missing),
            damaged=tuple(damaged),
            unresolved=tuple(unresolved),
            advisories=tuple(advisories),
        )

    if missing or damaged:
        return Resolution(
            verdict=Verdict.FAIL,
            reason=_describe_failure(manifest, missing, damaged),
            missing=tuple(missing),
            damaged=tuple(damaged),
            advisories=tuple(advisories),
        )

    reason = f"All {len(manifest.items)} required items present and serviceable"
    if advisories:
        reason += f"; {len(advisories)} advisory item(s) noted"
    return Resolution(
        verdict=Verdict.PASS,
        reason=reason,
        advisories=tuple(advisories),
    )


# Ordered least to most pessimistic. Used to break ties when the model reports
# the same Required Item more than once in a single reply.
_PESSIMISM_ORDER = {
    Presence.FOUND: 0,
    Presence.DAMAGED: 1,
    Presence.ABSENT: 2,
    Presence.UNREADABLE: 3,
}


def _pessimism(sighting: Sighting) -> int:
    return _PESSIMISM_ORDER[sighting.presence]


def _label(manifest: Manifest, key: str) -> str:
    item = manifest.item(key)
    return item.label if item else key


def _describe_unresolved(manifest: Manifest, unresolved: Sequence[str]) -> str:
    labels = ", ".join(_label(manifest, k) for k in unresolved)
    return f"Could not establish compliance for: {labels}"


def _describe_failure(
    manifest: Manifest, missing: Sequence[str], damaged: Sequence[str]
) -> str:
    parts: list[str] = []
    if missing:
        parts.append("missing " + ", ".join(_label(manifest, k) for k in missing))
    if damaged:
        parts.append("damaged " + ", ".join(_label(manifest, k) for k in damaged))
    return "Kit non-compliant: " + "; ".join(parts)
