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
from datetime import UTC, date, datetime
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
    """Absence is recorded but does not by itself fail the Kit.

    Nor does it hold the Latch when the model could not see the item at all.
    An item the Manifest has already said it can live without cannot be worth
    more unseen than it is worth gone: if a positive, confident ABSENT passes
    the Kit, an occluded look at the same item must not block it. Unestablished
    advisory evidence is recorded as an advisory, never as unresolved.
    """


@dataclass(frozen=True, slots=True)
class RequiredItem:
    """One entry in a Manifest - a thing that must be found in the Kit."""

    key: str
    """Stable identifier, used in the model reply and the Inspection Record."""

    label: str
    """Human-readable name, and what the model is actually asked about."""

    severity: Severity = Severity.CRITICAL

    quantity: int = 1

    expiry_checked: bool = False
    """Whether this item's printed use-by date must be read and judged.

    Presence is not serviceability. A sealed, undamaged, correctly-placed
    packet of expired haemostatic gauze satisfies every visual check and is
    still not something you want a medic reaching for.
    """

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

    expiry_warning_days: int = 30
    """An in-date item expiring within this many days is flagged as an
    advisory. It still passes - it is serviceable today - but whoever restocks
    the kit should know."""

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
        if self.expiry_warning_days < 0:
            raise ValueError(
                f"Manifest {self.manifest_id!r} expiry_warning_days must be "
                f">= 0, got {self.expiry_warning_days}"
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
                    expiry_checked=bool(entry.get("expiry_checked", False)),
                )
                for entry in raw_items
            )
            return cls(
                manifest_id=str(raw["manifest_id"]),
                name=str(raw["name"]),
                items=items,
                confidence_floor=_as_float(raw.get("confidence_floor", 0.55)),
                hold_seconds=_as_float(raw.get("hold_seconds", 5.0)),
                expiry_warning_days=_as_int(raw.get("expiry_warning_days", 30)),
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

    count: int | None = None
    """How many of this item the model counted, or None if it did not count.

    None is not "one" - it is "no quantity was established". For an item the
    Manifest requires more than one of, that is unresolved, exactly like an
    unread expiry date. A model that can see the tourniquet pocket but cannot
    tell whether it holds one tourniquet or two has not cleared the kit.
    """

    expiry: date | None = None
    """The use-by date the model read off the item, or None if it read none.

    None is not "does not expire" - it is "no date was established". For an
    item the Manifest expiry-checks, that is unresolved, and unresolved keeps
    the latch engaged like everything else unestablished.
    """

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"Sighting {self.key!r} confidence must be in [0,1], "
                f"got {self.confidence}"
            )
        if self.count is not None and self.count < 0:
            raise ValueError(
                f"Sighting {self.key!r} count must be >= 0, got {self.count}"
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

    expired: tuple[str, ...] = ()
    """Items whose printed use-by date has passed. A positive finding of
    non-compliance, exactly like a missing item."""

    expiring_soon: tuple[str, ...] = ()
    """In date today, but inside the Manifest's warning window. Advisory."""

    short: tuple[str, ...] = ()
    """Items present, serviceable and in date, but fewer than the Manifest
    requires. A positive finding of non-compliance, reported apart from
    missing because the remedy differs: top up, not replace."""


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
        return json.dumps(
            self.to_payload(), separators=(",", ":"), sort_keys=True
        )

    def to_payload(self) -> dict[str, object]:
        """The record's own content, without any chain metadata.

        The InspectionLog adds sequence and hash fields when it writes; a
        record does not know or care where in the chain it lands.
        """
        payload: dict[str, object] = {
            "inspection_id": self.inspection_id,
            "manifest_id": self.manifest_id,
            "started_at": self.started_at.astimezone(UTC).isoformat(),
            "verdict": self.resolution.verdict.value,
            "reason": self.resolution.reason,
            "missing": list(self.resolution.missing),
            "damaged": list(self.resolution.damaged),
            "unresolved": list(self.resolution.unresolved),
            "advisories": list(self.resolution.advisories),
            "expired": list(self.resolution.expired),
            "expiring_soon": list(self.resolution.expiring_soon),
            "short": list(self.resolution.short),
            "sightings": [
                {
                    "key": s.key,
                    "presence": s.presence.value,
                    "confidence": round(s.confidence, 4),
                    "note": s.note,
                    "count": s.count,
                    "expiry": s.expiry.isoformat() if s.expiry else None,
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
        return payload


def resolve_verdict(
    manifest: Manifest,
    sightings: Iterable[Sighting],
    as_of: date | None = None,
) -> Resolution:
    """Decide a Verdict for one Kit against one Manifest.

    The whole safety posture of ReadyKit Edge is this function, and it rests on
    one rule: **absence of evidence is not evidence of compliance.** A critical
    Required Item the model did not speak to, spoke about unreadably, or spoke
    about below the Manifest's Confidence Floor is UNRESOLVED - and any
    unresolved item yields INDETERMINATE, which keeps the Latch engaged.

    PASS therefore requires a positive, confident FOUND for every critical item.
    It is never the fallthrough branch.

    An *advisory* item is the one place that rule stops, because there it
    stops making sense. The Manifest has already said the Kit is serviceable
    without the item - a confident ABSENT passes - so treating an occluded
    look at the same item as grounds to hold the Latch makes doubt about
    something that does not matter stricter than certainty about it. Every
    advisory finding, established or not, is recorded as an advisory, and the
    Verdict turns only on the critical items.

    This is not leniency. An advisory item cannot fail a Kit either; what it
    can do is tell whoever restocks the Kit what was seen. The alternative is
    worse than lenient: one nice-to-have that the camera cannot quite make out
    - a triage marker under the lid, four spare fuses too small to count -
    holds an otherwise-compliant Kit at INDETERMINATE forever, and an
    inspection station that cries wolf gets propped open.

    Expiry obeys the same rule rather than a special case of its own. For an
    item the Manifest expiry-checks, a date that was never read is unresolved -
    not assumed fine. Presence is not serviceability: a sealed, undamaged,
    correctly-placed packet of expired gauze passes every visual check and is
    still not something to hand a medic.

    `as_of` is injectable so expiry behaviour is testable without waiting.
    """
    today = as_of if as_of is not None else datetime.now(UTC).date()
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
    expired: list[str] = []
    expiring_soon: list[str] = []
    short: list[str] = []

    for item in manifest.items:
        found = by_key.get(item.key)

        if found is None:
            _unestablished(item, unresolved, advisories)
            continue

        if found.presence is Presence.UNREADABLE:
            _unestablished(item, unresolved, advisories)
            continue

        if found.confidence < manifest.confidence_floor:
            # Low-confidence ABSENT is not a failure - it is a bad look at the
            # kit. Ask again rather than accusing the operator.
            _unestablished(item, unresolved, advisories)
            continue

        if found.presence is Presence.FOUND:
            if item.quantity > 1:
                _judge_count(item, found, short, unresolved, advisories)
            if item.expiry_checked:
                _judge_expiry(
                    item, found, today, manifest.expiry_warning_days,
                    expired, expiring_soon, unresolved, advisories,
                )
            continue

        bucket = missing if found.presence is Presence.ABSENT else damaged
        if item.severity is Severity.CRITICAL:
            bucket.append(item.key)
        else:
            advisories.append(item.key)

    # One advisory item can collect more than one finding - an unread count
    # and an unread date on the same packet - and the record should name it
    # once.
    advisories = _deduplicate(advisories)

    if unresolved:
        return Resolution(
            verdict=Verdict.INDETERMINATE,
            reason=_describe_unresolved(manifest, unresolved),
            missing=tuple(missing),
            damaged=tuple(damaged),
            unresolved=tuple(unresolved),
            advisories=tuple(advisories),
            expired=tuple(expired),
            expiring_soon=tuple(expiring_soon),
            short=tuple(short),
        )

    if missing or damaged or expired or short:
        return Resolution(
            verdict=Verdict.FAIL,
            reason=_describe_failure(manifest, missing, damaged, expired, short),
            missing=tuple(missing),
            damaged=tuple(damaged),
            advisories=tuple(advisories),
            expired=tuple(expired),
            expiring_soon=tuple(expiring_soon),
            short=tuple(short),
        )

    # Counts the critical items, not every item: an advisory item nobody
    # established cannot be claimed as "present and serviceable", and a PASS
    # that overstates what was seen is the same failure as a fail-open in
    # slower motion.
    critical = sum(1 for i in manifest.items if i.severity is Severity.CRITICAL)
    reason = f"All {critical} critical items present and serviceable"
    notes = []
    if expiring_soon:
        notes.append(f"{len(expiring_soon)} expiring soon")
    if advisories:
        notes.append(
            "advisory: " + ", ".join(_label(manifest, k) for k in advisories)
        )
    if notes:
        reason += "; " + ", ".join(notes)
    return Resolution(
        verdict=Verdict.PASS,
        reason=reason,
        advisories=tuple(advisories),
        expiring_soon=tuple(expiring_soon),
    )


def _unestablished(
    item: RequiredItem, unresolved: list[str], advisories: list[str]
) -> None:
    """Record that nothing was established about one Required Item.

    For a critical item that is UNRESOLVED, and any unresolved item yields
    INDETERMINATE. For an advisory item it is an advisory: the Manifest has
    already said the Kit is serviceable without it, so an unseen one cannot
    weigh more than a confidently absent one.
    """
    bucket = unresolved if item.severity is Severity.CRITICAL else advisories
    bucket.append(item.key)


def _deduplicate(keys: list[str]) -> list[str]:
    """Drop repeats, keeping the first position of each key."""
    return list(dict.fromkeys(keys))


def _judge_count(
    item: RequiredItem,
    sighting: Sighting,
    short: list[str],
    unresolved: list[str],
    advisories: list[str],
) -> None:
    """Judge how many of a multi-quantity item were counted.

    Same rule as everywhere else: a count that was never taken is unresolved,
    not assumed sufficient. "I can see tourniquets" does not establish that
    there are two of them, and a kit with one of a required pair is a kit that
    runs out halfway through.
    """
    if sighting.count is None:
        _unestablished(item, unresolved, advisories)
        return

    if sighting.count < item.quantity:
        bucket = short if item.severity is Severity.CRITICAL else advisories
        bucket.append(item.key)


def _judge_expiry(
    item: RequiredItem,
    sighting: Sighting,
    today: date,
    warning_days: int,
    expired: list[str],
    expiring_soon: list[str],
    unresolved: list[str],
    advisories: list[str],
) -> None:
    """Judge one in-date-checked item's printed use-by date.

    A date that was never read is unresolved, never assumed fine. This is the
    same rule as everywhere else, applied to a different kind of evidence.
    """
    if sighting.expiry is None:
        _unestablished(item, unresolved, advisories)
        return

    if sighting.expiry < today:
        # Expiring *today* is still serviceable today - pharmaceutical use-by
        # dates are inclusive of the printed day.
        bucket = expired if item.severity is Severity.CRITICAL else advisories
        bucket.append(item.key)
        return

    if (sighting.expiry - today).days <= warning_days:
        expiring_soon.append(item.key)


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
    manifest: Manifest,
    missing: Sequence[str],
    damaged: Sequence[str],
    expired: Sequence[str] = (),
    short: Sequence[str] = (),
) -> str:
    parts: list[str] = []
    if missing:
        parts.append("missing " + ", ".join(_label(manifest, k) for k in missing))
    if damaged:
        parts.append("damaged " + ", ".join(_label(manifest, k) for k in damaged))
    if expired:
        parts.append("expired " + ", ".join(_label(manifest, k) for k in expired))
    if short:
        parts.append("short on " + ", ".join(_label(manifest, k) for k in short))
    return "Kit non-compliant: " + "; ".join(parts)
