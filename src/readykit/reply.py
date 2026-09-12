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

import json
import re
from typing import Any

from .domain import Manifest, Presence, Sighting


class ReplyParseError(ValueError):
    """The model's reply carried no usable structure.

    The caller must treat this as INDETERMINATE. It is never a pass and never
    a failure - it is the absence of an observation.
    """


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

_SCHEMA_HINT = """Reply with JSON only, in exactly this shape:

{"items": [{"key": "<item key>", "presence": "<found|absent|damaged|unreadable>",
            "confidence": <0.0-1.0>, "note": "<short observation>"}]}
"""


def build_prompt(manifest: Manifest) -> str:
    """The instruction sent to the model alongside the frame.

    Naming `unreadable` explicitly matters: without a sanctioned way to say
    "I could not see it", a model under instruction to fill every field will
    guess, and half of those guesses are `found`.
    """
    lines = [
        "You are inspecting an equipment kit in the image.",
        f"Kit specification: {manifest.name}.",
        "",
        "For EACH item listed below, report exactly one presence value:",
        "  found      - the item is visibly present and undamaged",
        "  absent     - you can see where it belongs and it is not there",
        "  damaged    - the item is present but broken, expired, or unusable",
        "  unreadable - you cannot tell; it is occluded, out of frame, or unclear",
        "",
        "Do not guess. If you are unsure, answer 'unreadable'. Reporting",
        "'unreadable' is always correct when you cannot see the item clearly.",
        "",
        "Items:",
    ]
    for item in manifest.items:
        quantity = f" (quantity {item.quantity})" if item.quantity > 1 else ""
        lines.append(f"  - key={item.key}: {item.label}{quantity}")
    lines.extend(["", _SCHEMA_HINT])
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
    return sightings


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
    )


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
