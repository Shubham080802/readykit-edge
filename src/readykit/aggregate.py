"""Combining several frames into one set of Sightings.

Deciding a kit's fate on a single glance is fragile in a way that shows up
immediately on real hardware: one glare frame, one hand passing over the tray,
one autofocus hunt, and a compliant kit reads as INDETERMINATE. Looking more
than once fixes that without weakening anything.

The aggregation rule is chosen to preserve the core property rather than to
maximise the pass rate:

* An item resolves to a presence only when enough frames **agree**. Below that
  threshold it is UNREADABLE, which upstream is unresolved, which keeps the
  latch engaged.
* Disagreement is therefore not averaged away - it is reported as doubt. Two
  frames saying FOUND and two saying ABSENT is not "probably fine", it is a
  kit nobody has established anything about.
* Confidence is the mean over the agreeing frames only, so a strong reading is
  not diluted by frames that saw something else entirely.

Taking the single most pessimistic frame would have been the other obvious
rule. It is rejected here because it makes the system hostage to its worst
glance: one frame with a thumb over the tourniquet fails an intact kit, and an
inspection station that cries wolf gets propped open.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import date

from .domain import Presence, Sighting

DEFAULT_AGREEMENT = 0.6


def aggregate_sightings(
    per_frame: Sequence[Sequence[Sighting]],
    min_agreement: float = DEFAULT_AGREEMENT,
) -> list[Sighting]:
    """Reduce several frames' Sightings to one set.

    `min_agreement` is the fraction of frames that reported the item which
    must agree on a presence for it to stand. It is a fraction of the frames
    that spoke about the item, not of all frames: an item genuinely out of
    shot in one frame should not poison the reading from the frames that saw
    it clearly.
    """
    if not 0.0 < min_agreement <= 1.0:
        raise ValueError(
            f"min_agreement must be in (0,1], got {min_agreement}"
        )

    if not per_frame:
        return []
    if len(per_frame) == 1:
        return list(per_frame[0])

    grouped: dict[str, list[Sighting]] = defaultdict(list)
    for frame in per_frame:
        for sighting in frame:
            grouped[sighting.key].append(sighting)

    aggregated: list[Sighting] = []
    for key, sightings in grouped.items():
        aggregated.append(_combine(key, sightings, min_agreement))
    return aggregated


def _combine(
    key: str, sightings: list[Sighting], min_agreement: float
) -> Sighting:
    by_presence: dict[Presence, list[Sighting]] = defaultdict(list)
    for sighting in sightings:
        by_presence[sighting.presence].append(sighting)

    # Most-reported presence wins, ties broken pessimistically so that an
    # even split never resolves in the kit's favour.
    winner = max(
        by_presence.items(),
        key=lambda entry: (len(entry[1]), _pessimism(entry[0])),
    )
    presence, agreeing = winner

    share = len(agreeing) / len(sightings)
    if share < min_agreement:
        return Sighting(
            key=key,
            presence=Presence.UNREADABLE,
            confidence=0.0,
            note=(
                f"frames disagreed: "
                f"{_describe_split(by_presence)} over {len(sightings)} frames"
            ),
            expiry=None,
        )

    mean_confidence = sum(s.confidence for s in agreeing) / len(agreeing)
    return Sighting(
        key=key,
        presence=presence,
        confidence=mean_confidence,
        note=_first_note(agreeing),
        expiry=_agreed_expiry(agreeing),
    )


def _agreed_expiry(agreeing: list[Sighting]) -> date | None:
    """A date only stands if every frame that read one read the same one.

    Two frames disagreeing about a printed date means the date was not
    established, which upstream is unresolved for an expiry-checked item. This
    is the same rule as presence, applied to the other kind of evidence.
    """
    dates = {s.expiry for s in agreeing if s.expiry is not None}
    if len(dates) != 1:
        return None
    return dates.pop()


def _first_note(agreeing: list[Sighting]) -> str:
    for sighting in agreeing:
        if sighting.note:
            return sighting.note
    return ""


def _describe_split(by_presence: dict[Presence, list[Sighting]]) -> str:
    parts = sorted(
        ((presence.value, len(items)) for presence, items in by_presence.items()),
        key=lambda entry: -entry[1],
    )
    return ", ".join(f"{count}x {name}" for name, count in parts)


_PESSIMISM = {
    Presence.FOUND: 0,
    Presence.DAMAGED: 1,
    Presence.ABSENT: 2,
    Presence.UNREADABLE: 3,
}


def _pessimism(presence: Presence) -> int:
    return _PESSIMISM[presence]
