"""Counting, for items a Manifest requires more than one of.

A trauma kit specifying two tourniquets and holding one is a kit that runs out
halfway through. Presence alone cannot catch that - "I can see tourniquets"
does not establish that there are two.

Counting obeys the same rule as presence and expiry: a count that was never
taken is unresolved, not assumed sufficient.
"""

from __future__ import annotations

import pytest

from readykit.aggregate import aggregate_sightings
from readykit.bridge import LoopbackLink, VirtualActuatorNode
from readykit.bridge.loopback import LatchState
from readykit.capture import ScriptedSource
from readykit.domain import (
    Manifest,
    Presence,
    RequiredItem,
    Severity,
    Sighting,
    Verdict,
    resolve_verdict,
)
from readykit.engine import InspectionEngine
from readykit.inference.simulated import SimulatedEngine
from readykit.reply import build_prompt, parse_reply

PAIRED = Manifest(
    manifest_id="paired",
    name="Paired Kit",
    items=(
        RequiredItem(key="tourniquet", label="Tourniquet", quantity=2),
        RequiredItem(key="shears", label="Trauma Shears"),
    ),
)


def found(key: str, count: int | None = None) -> Sighting:
    return Sighting(
        key=key, presence=Presence.FOUND, confidence=0.95, count=count
    )


class TestEnoughIsEnough:
    def test_the_required_number_passes(self) -> None:
        result = resolve_verdict(PAIRED, [found("tourniquet", 2), found("shears")])
        assert result.verdict is Verdict.PASS
        assert result.short == ()

    def test_more_than_required_passes(self) -> None:
        """A kit someone over-stocked is not non-compliant."""
        result = resolve_verdict(PAIRED, [found("tourniquet", 5), found("shears")])
        assert result.verdict is Verdict.PASS

    def test_single_quantity_items_need_no_count(self) -> None:
        """Presence already establishes "at least one"."""
        result = resolve_verdict(PAIRED, [found("tourniquet", 2), found("shears")])
        assert result.verdict is Verdict.PASS


class TestTooFewFails:
    def test_one_of_a_required_pair_fails(self) -> None:
        result = resolve_verdict(PAIRED, [found("tourniquet", 1), found("shears")])
        assert result.verdict is Verdict.FAIL
        assert result.short == ("tourniquet",)
        assert "short on Tourniquet" in result.reason

    def test_short_is_reported_apart_from_missing(self) -> None:
        """Different remedies - top up versus replace - so different buckets."""
        result = resolve_verdict(PAIRED, [found("tourniquet", 1), found("shears")])
        assert result.missing == ()
        assert result.damaged == ()
        assert result.short == ("tourniquet",)

    def test_a_count_of_zero_with_presence_found_still_fails(self) -> None:
        """A contradictory reply must not resolve in the kit's favour."""
        result = resolve_verdict(PAIRED, [found("tourniquet", 0), found("shears")])
        assert result.verdict is Verdict.FAIL

    def test_an_advisory_item_short_does_not_fail_the_kit(self) -> None:
        manifest = Manifest(
            manifest_id="adv",
            name="Advisory",
            items=(
                RequiredItem(key="shears", label="Trauma Shears"),
                RequiredItem(
                    key="gloves",
                    label="Nitrile Gloves",
                    quantity=4,
                    severity=Severity.ADVISORY,
                ),
            ),
        )
        result = resolve_verdict(manifest, [found("shears"), found("gloves", 1)])
        assert result.verdict is Verdict.PASS
        assert result.advisories == ("gloves",)


class TestAnUncountedItemIsNotAPass:
    def test_no_count_on_a_multi_item_is_indeterminate(self) -> None:
        """The case this whole check exists for. The item is present and
        undamaged; how many there are is simply unknown."""
        result = resolve_verdict(PAIRED, [found("tourniquet", None), found("shears")])
        assert result.verdict is Verdict.INDETERMINATE
        assert result.unresolved == ("tourniquet",)
        assert result.short == ()

    def test_an_uncounted_item_is_not_a_failure_either(self) -> None:
        """Nothing is known to be wrong. Sending someone to restock a kit that
        is probably fine is its own kind of error."""
        result = resolve_verdict(PAIRED, [found("tourniquet", None), found("shears")])
        assert result.verdict is not Verdict.FAIL

    def test_a_missing_item_is_not_also_reported_as_short(self) -> None:
        """Counting only applies to something that is actually there."""
        result = resolve_verdict(
            PAIRED,
            [Sighting("tourniquet", Presence.ABSENT, 0.95), found("shears")],
        )
        assert result.missing == ("tourniquet",)
        assert result.short == ()


class TestReadingCounts:
    def _count(self, raw: str) -> int | None:
        reply = (
            '{"items": [{"key": "tourniquet", "presence": "found", '
            f'"confidence": 0.9, "count": {raw}}}]}}'
        )
        return parse_reply(reply, PAIRED)[0].count

    def test_an_integer_count_is_read(self) -> None:
        assert self._count("2") == 2

    @pytest.mark.parametrize("raw", ['"two"', "null", "1.5", "-1", "true"])
    def test_anything_unusable_becomes_none(self, raw: str) -> None:
        """The one place a parser could manufacture a pass is by coercing a bad
        count to the required number, so every ambiguity resolves to None."""
        assert self._count(raw) is None

    def test_a_missing_count_field_is_none(self) -> None:
        reply = (
            '{"items": [{"key": "tourniquet", "presence": "found", '
            '"confidence": 0.9}]}'
        )
        assert parse_reply(reply, PAIRED)[0].count is None

    def test_a_negative_count_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="count"):
            Sighting("x", Presence.FOUND, 0.9, count=-1)


class TestThePrompt:
    def test_multi_quantity_items_are_marked(self) -> None:
        prompt = build_prompt(PAIRED)
        assert "COUNT THEM" in prompt
        assert "2 required" in prompt

    def test_the_prompt_says_not_to_assume_the_required_number(self) -> None:
        prompt = build_prompt(PAIRED)
        assert "Do not assume" in prompt
        assert "guessed one is" in prompt

    def test_a_manifest_with_no_multi_items_gets_no_count_instructions(self) -> None:
        single = Manifest(
            manifest_id="single",
            name="Single",
            items=(RequiredItem(key="shears", label="Trauma Shears"),),
        )
        assert "COUNT THEM" not in build_prompt(single)


class TestCountsAcrossFrames:
    def test_frames_agreeing_keep_the_count(self) -> None:
        result = aggregate_sightings(
            [[found("tourniquet", 2)], [found("tourniquet", 2)]]
        )
        assert result[0].count == 2

    def test_frames_disagreeing_establish_no_count(self) -> None:
        """Not the minimum - one bad frame would fail an intact kit. Not the
        maximum - that manufactures passes. Disagreement is doubt."""
        result = aggregate_sightings(
            [[found("tourniquet", 1)], [found("tourniquet", 2)]]
        )
        assert result[0].count is None

    def test_a_count_from_one_frame_only_still_stands(self) -> None:
        result = aggregate_sightings(
            [[found("tourniquet", 2)], [found("tourniquet", None)]]
        )
        assert result[0].count == 2


class TestThroughTheEngine:
    def build(self, scene: str, node: VirtualActuatorNode) -> InspectionEngine:
        return InspectionEngine(
            manifest=PAIRED,
            source=ScriptedSource(scene),
            engine=SimulatedEngine(seed=7),
            link=LoopbackLink(node),
        )

    def test_a_short_kit_fails_and_stays_locked(self) -> None:
        node = VirtualActuatorNode()
        outcome = self.build("short", node).run_once()
        assert outcome.verdict is Verdict.FAIL
        assert outcome.record.resolution.short == ("tourniquet",)
        assert node.latch is LatchState.ENGAGED

    def test_an_uncountable_kit_is_indeterminate_and_stays_locked(self) -> None:
        node = VirtualActuatorNode()
        outcome = self.build("count-unreadable", node).run_once()
        assert outcome.verdict is Verdict.INDETERMINATE
        assert node.latch is LatchState.ENGAGED

    def test_a_complete_kit_still_passes_with_counts_supplied(self) -> None:
        node = VirtualActuatorNode()
        outcome = self.build("complete", node).run_once()
        assert outcome.verdict is Verdict.PASS
        assert node.latch is LatchState.RELEASED

    def test_the_record_carries_the_count(self) -> None:
        import json

        node = VirtualActuatorNode()
        outcome = self.build("short", node).run_once()
        payload = json.loads(outcome.record.to_json_line())
        counts = {s["key"]: s["count"] for s in payload["sightings"]}
        assert counts["tourniquet"] == 1
        assert payload["short"] == ["tourniquet"]
