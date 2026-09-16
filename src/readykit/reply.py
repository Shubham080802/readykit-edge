"""Turning a vision-language model's reply into Sightings.

A VLM is a text generator, not an oracle. It will wrap JSON in prose, invent
fields, omit items it forgot about, and occasionally apologise instead of
answering. Every one of those has to degrade towards INDETERMINATE, never
towards PASS.

Two rules govern this module:

1. **Only extract what the model committed to.** An item it did not mention
   produces no Sighting at all, which `resolve_verdict` reads as unresolved.
2. **Ambiguity resolves to UNREADABLE.** A presence value we do not recognise
   is not a guess in either direction - it is a failure to observe.
"""

from __future__ import annotations

import calendar
import json
import re
from dataclasses import replace
from datetime import date, datetime
from typing import Any

from .domain import Manifest, Presence, Sighting


class ReplyParseError(ValueError):
    """The model's reply carried no usable structure.

    The caller must treat this as INDETERMINATE. It is never a pass and never
    a failure - it is the absence of an observation.
    """


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

_SCHEMA_HINT = """Reply with JSON only, in exactly this shape:

{"kit_present": "<yes|no|unclear>",
 "items": [{"key": "<item key>", "presence": "<found|absent|damaged|unreadable>",
            "confidence": <0.0-1.0>, "note": "<short observation>",
            "expiry": "<YYYY-MM or YYYY-MM-DD, omit if not required or not legible>",
            "count": <how many you counted, omit if not required or unsure>}]}
"""

_KIT_ESTABLISHED = "yes"
"""The only `kit_present` value that lets a per-item reading stand."""

_OUTPUT_RULES = """Output rules, which matter as much as the readings:
- Emit the JSON on ONE line. No newlines, no indentation, no space after
  punctuation.
- Keep every 'note' to four words or fewer, and omit 'note' entirely when an
  item is simply found and unremarkable.
- NEVER write a placeholder. Omit the whole field instead. Do not write
  "expiry": "unreadable" or "expiry": "" - leave 'expiry' out. Do not write
  "count": 0 to mean you did not count - leave 'count' out. A count of 0
  states that you looked and saw none, which is a different claim, and a
  field you omit is read correctly as "not established"."""
"""Shape of the reply, which is most of what an inspection costs.

An inspection here is decode-bound: the frame prefills at about a thousand
tokens a second, then every output token is generated one at a time. Measured
on a Snapdragon X Elite against a seven-item manifest, pretty-printed JSON
with sentence-long notes runs 408 generated tokens and 24.5s of decode; the
same readings under these rules run 226 and 13.6s. The rules cost 95 extra
prompt tokens, which prefill in about a tenth of a second.

Nothing here touches what is reported - `json.loads` is indifferent to
whitespace, and a four-word note carries an operator as far as a sentence
does. It only stops the model spending eleven seconds on indentation while
somebody stands at a latch waiting for it."""


def build_prompt(manifest: Manifest) -> str:
    """The instruction sent to the model alongside the frame.

    Naming `unreadable` explicitly matters: without a sanctioned way to say
    "I could not see it", a model under instruction to fill every field will
    guess, and half of those guesses are `found`.
    """
    lines = [
        "You are examining an image that may or may not contain an equipment kit.",
        f"Kit specification: {manifest.name}.",
        "",
        "FIRST, establish what you are actually looking at. Do not assume the kit",
        "is there. The image may show a room, a person, a desk, or nothing",
        "relevant at all. Report this in 'kit_present':",
        "  yes     - the kit is in view: its tray, case, or the items below",
        "            laid out together, even if some of them are missing",
        "  no      - you can see the scene, and this kit is not in it",
        "  unclear - you cannot tell",
        "",
        "If kit_present is anything other than 'yes', report EVERY item as",
        "'unreadable'. You cannot have seen items belonging to a kit you have",
        "not established is there.",
        "",
        "For EACH item listed below, report exactly one presence value:",
        "  found      - the item is visibly present and undamaged",
        "  absent     - you can see where it belongs and it is not there",
        "  damaged    - the item is present but broken, torn, or unusable",
        "  unreadable - you cannot tell; it is occluded, out of frame, or unclear",
        "",
        "Do not guess. If you are unsure, answer 'unreadable'. Reporting",
        "'unreadable' is always correct when you cannot see the item clearly.",
        "",
        "Items:",
    ]
    expiry_items = [item for item in manifest.items if item.expiry_checked]
    counted_items = [item for item in manifest.items if item.quantity > 1]
    for item in manifest.items:
        quantity = (
            f" [COUNT THEM - {item.quantity} required]" if item.quantity > 1 else ""
        )
        expiry = " [READ THE EXPIRY DATE]" if item.expiry_checked else ""
        lines.append(f"  - key={item.key}: {item.label}{quantity}{expiry}")

    if counted_items:
        lines.extend(
            [
                "",
                "For items marked [COUNT THEM], report how many you can see in",
                "the 'count' field. Count what is actually visible. Do not assume",
                "the required number is present, and omit the field if you cannot",
                "count them - an omitted count is handled safely, a guessed one is",
                "not.",
            ]
        )

    if expiry_items:
        lines.extend(
            [
                "",
                "For items marked [READ THE EXPIRY DATE], also report the "
                "printed use-by date",
                "in the 'expiry' field, as YYYY-MM-DD, or YYYY-MM when only a "
                "month is printed.",
                "Transcribe what is printed. Do not infer a date from the "
                "packaging's appearance,",
                "and omit the field entirely if you cannot read it - a missing "
                "date is handled",
                "safely, an invented one is not.",
            ]
        )
    lines.extend(["", _SCHEMA_HINT, "", _OUTPUT_RULES])
    return "\n".join(lines)


def parse_reply(raw: str, manifest: Manifest) -> list[Sighting]:
    """Extract Sightings from a model reply.

    Raises ReplyParseError when nothing structured can be recovered. Returns a
    possibly-empty list when the structure was valid but described nothing on
    the Manifest - both outcomes keep the Latch engaged, via different paths.
    """
    document = _extract_json_object(raw)

    entries = document.get("items")
    if not isinstance(entries, list):
        raise ReplyParseError("reply JSON has no 'items' array")

    known = set(manifest.keys)
    sightings: list[Sighting] = []
    for entry in entries:
        sighting = _parse_entry(entry, known)
        if sighting is not None:
            sightings.append(sighting)

    if not _kit_established(document):
        return [_unestablished(sighting) for sighting in sightings]
    return sightings


def _kit_established(document: dict[str, Any]) -> bool:
    """Whether the model said it can actually see the kit.

    A model handed a list of expected items and told it is looking at a kit
    will pattern-complete one that is not there - observed on this hardware
    reporting all seven items of a trauma kit as `found` at 0.95+, in three
    consecutive frames of an empty room. Confidence does not catch it, because
    the invented readings are confident, and neither does aggregating frames,
    because the invention is stable across them.

    So the kit itself is treated as the first thing to establish, on the same
    rule as everything else here: items belonging to a kit nobody has
    established is present are not evidence of anything.

    A reply that omits the field entirely has not been asked the question -
    an older model, or one that dropped it - and falls back to the per-item
    readings rather than failing every inspection outright.
    """
    stated = document.get("kit_present")
    if not isinstance(stated, str):
        return True
    return stated.strip().lower() == _KIT_ESTABLISHED


def _unestablished(sighting: Sighting) -> Sighting:
    return replace(
        sighting,
        presence=Presence.UNREADABLE,
        confidence=0.0,
        note="kit itself not established in frame",
        expiry=None,
        count=None,
    )


def _extract_json_object(raw: str) -> dict[str, Any]:
    if not raw or not raw.strip():
        raise ReplyParseError("reply was empty")

    for candidate in _candidates(raw):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise ReplyParseError(
        f"no JSON object found in reply: {raw.strip()[:120]!r}"
    )


def _candidates(raw: str) -> list[str]:
    """Progressively looser attempts at locating the JSON payload."""
    found = [raw.strip()]
    found.extend(match.strip() for match in _FENCE.findall(raw))
    # Last resort: the widest brace-delimited span in the text.
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        found.append(raw[start : end + 1])
    return found


def _parse_entry(entry: Any, known: set[str]) -> Sighting | None:
    if not isinstance(entry, dict):
        return None

    key = entry.get("key")
    if not isinstance(key, str) or key not in known:
        return None

    presence = _parse_presence(entry.get("presence"))
    confidence = _parse_confidence(entry.get("confidence"))

    note = entry.get("note", "")
    if not isinstance(note, str):
        note = ""

    return Sighting(
        key=key,
        presence=presence,
        confidence=confidence,
        note=note[:200],
        count=_parse_count(entry.get("count")),
        expiry=_parse_expiry(entry.get("expiry")),
    )


def _parse_count(value: Any) -> int | None:
    """Read a counted quantity. Anything unusable becomes None.

    None means "no quantity established", which upstream is unresolved for an
    item the Manifest requires more than one of. Coercing a bad value to 1 -
    or to the required number - is the one thing that would manufacture a pass,
    so every ambiguity resolves to None.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != int(value) or value < 0:
        return None
    return int(value)


def _parse_expiry(value: Any) -> date | None:
    """Read a printed use-by date. Anything unrecognised becomes None.

    None means "no date established", which upstream is unresolved for an item
    that needs one. Guessing a date here would be the one place a parser could
    manufacture compliance, so every ambiguity resolves to None.

    Month precision is resolved to the **last day of that month**, which is the
    pharmaceutical convention: "EXP 2026-03" means usable through 31 March.
    Resolving it to the first of the month instead would retire stock up to a
    month early - safe, but wrong, and it would erode trust in the system.
    """
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue

    for pattern in ("%Y-%m", "%Y/%m", "%m/%Y", "%b %Y", "%B %Y"):
        try:
            parsed = datetime.strptime(text, pattern).date()
        except ValueError:
            continue
        last_day = calendar.monthrange(parsed.year, parsed.month)[1]
        return parsed.replace(day=last_day)

    return None


def _parse_presence(value: Any) -> Presence:
    if isinstance(value, str):
        try:
            return Presence(value.strip().lower())
        except ValueError:
            pass
    # Anything we do not recognise is a failure to observe, not an observation.
    return Presence.UNREADABLE


def _parse_confidence(value: Any) -> float:
    """Absent or unusable confidence is 0.0.

    Defaulting to 1.0 here would let a model that omits the field unlock a
    latch, so the default is maximum doubt.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return max(0.0, min(1.0, float(value)))
