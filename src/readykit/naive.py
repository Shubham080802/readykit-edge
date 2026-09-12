"""The original blueprint's verdict logic, preserved so it can be run.

This module exists to be compared against, not to be used. It is a faithful
reproduction of how the ReadyKit Edge blueprint decided whether to release the
latch:

    # vision_inspector.py, as specified
    result = engine.infer(image=frame, text=prompt)

    if "missing" in result.lower() or "no" in result.lower():
        signal_code = b'ERR_MISSING_TOOL\\n'
    else:
        signal_code = b'PASS_KIT\\n'

    arduino.write(signal_code)

Two substrings, matched against free-form model prose, deciding a physical
lock. Keeping it executable means the claim "this design fails open" is
something the system demonstrates on live frames rather than something the
README asserts.

**Nothing here is ever allowed to actuate.** `readykit.engine` runs it purely
for the record, and only `resolve_verdict` decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .domain import Verdict

PASS_KIT = "PASS_KIT"
ERR_MISSING_TOOL = "ERR_MISSING_TOOL"


class Divergence(StrEnum):
    """How the two verdict logics differed on one frame."""

    AGREED = "agreed"
    """Both reached the same decision about the latch."""

    UNSAFE = "unsafe"
    """The blueprint would have released the latch where ReadyKit would not.

    The failure that matters. Every instance is a frame on which the original
    design would have opened an enclosure containing a kit that was either
    known to be non-compliant or was never actually established as compliant.
    """

    SPURIOUS = "spurious"
    """The blueprint would have rejected a kit ReadyKit passed.

    Annoying rather than dangerous - an operator is turned away from a good
    kit. Usually a reply like "no items are missing", where the substring
    match trips over its own negation.
    """


@dataclass(frozen=True, slots=True)
class Comparison:
    """What each logic concluded from the same model reply."""

    signal: str
    """The command the blueprint would have written to the serial port."""

    blueprint_unlocks: bool
    verdict: Verdict
    readykit_unlocks: bool
    divergence: Divergence
    reason: str

    @property
    def disagreed(self) -> bool:
        return self.divergence is not Divergence.AGREED


def naive_signal(raw_reply: str) -> str:
    """The blueprint's parser, unchanged.

    Note what this cannot see. It has no manifest, so it does not know what was
    supposed to be in the kit. It has no notion of confidence. It cannot tell
    an answer from a refusal to answer. It is two `in` checks against whatever
    the model happened to say.
    """
    result = raw_reply
    if "missing" in result.lower() or "no" in result.lower():
        return ERR_MISSING_TOOL
    return PASS_KIT


def compare(raw_reply: str, verdict: Verdict) -> Comparison:
    """Run the blueprint's logic beside a resolved Verdict on the same reply."""
    signal = naive_signal(raw_reply)
    blueprint_unlocks = signal == PASS_KIT
    readykit_unlocks = verdict is Verdict.PASS

    if blueprint_unlocks == readykit_unlocks:
        divergence = Divergence.AGREED
        reason = (
            "Both would have released the latch"
            if blueprint_unlocks
            else "Both would have kept the latch engaged"
        )
    elif blueprint_unlocks:
        divergence = Divergence.UNSAFE
        reason = _explain_unsafe(raw_reply, verdict)
    else:
        divergence = Divergence.SPURIOUS
        reason = _explain_spurious(raw_reply)

    return Comparison(
        signal=signal,
        blueprint_unlocks=blueprint_unlocks,
        verdict=verdict,
        readykit_unlocks=readykit_unlocks,
        divergence=divergence,
        reason=reason,
    )


def _explain_unsafe(raw_reply: str, verdict: Verdict) -> str:
    lowered = raw_reply.lower()
    if not lowered.strip():
        return (
            "The model said nothing at all. Neither trigger substring appears "
            "in an empty string, so the blueprint falls through to PASS_KIT "
            "and releases the latch on a kit it never saw"
        )

    if verdict is Verdict.INDETERMINATE:
        return (
            "The model declined to establish compliance, but said so in words "
            "containing neither \"missing\" nor \"no\" - so the blueprint reads "
            "its refusal as a pass and releases the latch"
        )

    return (
        "The model reported a non-compliant kit in words containing neither "
        "trigger substring, so the blueprint releases the latch on it"
    )


def _explain_spurious(raw_reply: str) -> str:
    lowered = raw_reply.lower()
    if "no" in lowered and "missing" not in lowered:
        return (
            "The substring \"no\" appears inside an ordinary word or a "
            "negation, so the blueprint rejects a compliant kit"
        )
    return (
        "The word \"missing\" appears in a reply that reports nothing missing "
        "- the blueprint cannot tell a denial from an assertion"
    )
