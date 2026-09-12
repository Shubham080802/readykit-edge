"""The blueprint comparison.

Two jobs here. Pin that `readykit.naive` reproduces the original blueprint
faithfully - if it does not, the whole comparison is a strawman and deserves to
be thrown out. And pin that it can never influence what the hardware does.
"""

from __future__ import annotations

import pytest

from readykit.bridge import LoopbackLink, VirtualActuatorNode
from readykit.bridge.loopback import LatchState
from readykit.capture import ScriptedSource
from readykit.domain import Manifest, RequiredItem, Verdict
from readykit.engine import InspectionEngine
from readykit.inference.simulated import SimulatedEngine
from readykit.naive import (
    ERR_MISSING_TOOL,
    PASS_KIT,
    Divergence,
    compare,
    naive_signal,
)

KIT = Manifest(
    manifest_id="naive-demo",
    name="Naive Demo Kit",
    items=(
        RequiredItem(key="shears", label="Trauma Shears"),
        RequiredItem(key="gauze", label="Hemostatic Gauze"),
    ),
)


def build(scene: str, node: VirtualActuatorNode | None = None) -> InspectionEngine:
    return InspectionEngine(
        manifest=KIT,
        source=ScriptedSource(scene),
        engine=SimulatedEngine(seed=7),
        link=LoopbackLink(node) if node is not None else None,
    )


class TestFaithfulToTheBlueprint:
    """The blueprint's rule, verbatim:

        if "missing" in result.lower() or "no" in result.lower():
            signal_code = b'ERR_MISSING_TOOL'
        else:
            signal_code = b'PASS_KIT'
    """

    @pytest.mark.parametrize(
        "reply",
        [
            "The tourniquet is missing.",
            "MISSING ITEMS DETECTED",
            "no items found",
            "Nothing appears to be in the tray.",
        ],
    )
    def test_replies_containing_a_trigger_substring_reject(self, reply: str) -> None:
        assert naive_signal(reply) == ERR_MISSING_TOOL

    @pytest.mark.parametrize(
        "reply",
        [
            "All items are present.",
            "The kit is complete.",
            "",
            "The trauma shears are absent from the tray.",
        ],
    )
    def test_everything_else_passes(self, reply: str) -> None:
        assert naive_signal(reply) == PASS_KIT

    def test_matching_is_case_insensitive(self) -> None:
        assert naive_signal("MISSING") == naive_signal("missing")


class TestTheFailOpenCases:
    """The cases that motivated replacing it. Each is a reply a real VLM
    plausibly produces, on which the blueprint releases a physical lock."""

    def test_an_empty_reply_unlocks(self) -> None:
        """Neither substring occurs in an empty string, so a model that said
        nothing at all falls through to PASS_KIT."""
        assert naive_signal("") == PASS_KIT

    def test_a_refusal_to_answer_unlocks(self) -> None:
        assert naive_signal("The image is too blurry to assess.") == PASS_KIT

    def test_an_occluded_tray_unlocks(self) -> None:
        reply = "The tray is obscured; I am unable to assess its contents."
        assert naive_signal(reply) == PASS_KIT

    def test_absent_is_not_missing(self) -> None:
        """The single most damning case: the model correctly reports an item
        is gone, using a word the blueprint does not look for."""
        reply = "The Trauma Shears are absent from the tray."
        assert naive_signal(reply) == PASS_KIT

    def test_an_apology_unlocks(self) -> None:
        assert naive_signal("Sorry, I am unable to process that image.") == PASS_KIT


class TestTheFalseRejectionCases:
    def test_a_denial_of_absence_is_read_as_absence(self) -> None:
        """"No items are missing" contains both trigger substrings, so a
        compliant kit is rejected."""
        assert naive_signal("No items are missing.") == ERR_MISSING_TOOL

    def test_no_inside_an_ordinary_word_rejects(self) -> None:
        """`"no" in text` matches inside "nominal", among many others."""
        assert naive_signal("All readings nominal.") == ERR_MISSING_TOOL

    def test_some_failures_are_caught_purely_by_luck(self) -> None:
        """"Could not" happens to contain "no", so this apology is rejected -
        correctly, and entirely by accident.

        Worth pinning explicitly. The blueprint is not wrong about everything,
        and a comparison that pretended otherwise would not survive scrutiny.
        What is wrong with it is that whether a latch opens depends on which
        synonym the model reached for.
        """
        assert naive_signal("Sorry, I could not process that image.") == (
            ERR_MISSING_TOOL
        )


class TestClassification:
    def test_both_unlocking_is_agreement(self) -> None:
        result = compare("All items are present.", Verdict.PASS)
        assert result.divergence is Divergence.AGREED
        assert not result.disagreed

    def test_both_refusing_is_agreement(self) -> None:
        result = compare("The gauze is missing.", Verdict.FAIL)
        assert result.divergence is Divergence.AGREED

    def test_blueprint_unlocking_a_failed_kit_is_unsafe(self) -> None:
        result = compare("The shears are absent.", Verdict.FAIL)
        assert result.divergence is Divergence.UNSAFE
        assert result.blueprint_unlocks
        assert not result.readykit_unlocks

    def test_blueprint_unlocking_an_unreadable_kit_is_unsafe(self) -> None:
        result = compare("Too blurry to tell.", Verdict.INDETERMINATE)
        assert result.divergence is Divergence.UNSAFE
        assert "refus" in result.reason or "declin" in result.reason

    def test_blueprint_rejecting_a_good_kit_is_spurious_not_unsafe(self) -> None:
        """A false rejection is annoying, not dangerous, and the two must not
        be counted together."""
        result = compare("No items are missing.", Verdict.PASS)
        assert result.divergence is Divergence.SPURIOUS


class TestItCannotActuate:
    def test_an_unsafe_divergence_leaves_the_latch_engaged(self) -> None:
        """The blueprint's verdict is recorded, never enacted. If replaying it
        could move hardware, this module would be a liability rather than a
        demonstration."""
        node = VirtualActuatorNode()
        outcome = build("missing-shears", node).run_once()

        assert outcome.comparison is not None
        assert outcome.comparison.divergence is Divergence.UNSAFE
        assert outcome.comparison.blueprint_unlocks

        assert outcome.verdict is Verdict.FAIL
        assert node.latch is LatchState.ENGAGED
        assert "REJECT" in outcome.record.commanded

    def test_a_spurious_divergence_still_releases_the_latch(self) -> None:
        """Symmetrically: the blueprint refusing must not block a real pass."""
        node = VirtualActuatorNode()
        outcome = build("complete-negated", node).run_once()

        assert outcome.comparison is not None
        assert outcome.comparison.divergence is Divergence.SPURIOUS
        assert outcome.verdict is Verdict.PASS
        assert node.latch is LatchState.RELEASED


class TestRecordedForAudit:
    def test_the_record_carries_the_verbatim_reply(self) -> None:
        outcome = build("complete").run_once()
        assert "present" in outcome.record.raw_reply
        assert "items" in outcome.record.raw_reply

    def test_the_record_carries_the_blueprint_signal(self) -> None:
        outcome = build("occluded").run_once()
        assert outcome.record.blueprint_signal == PASS_KIT
        assert outcome.record.blueprint_divergence == "unsafe"

    def test_a_crashed_engine_produces_no_comparison(self) -> None:
        """The NPU died before emitting any text. The blueprint's loop had
        nothing to parse and would have crashed too - claiming it would have
        unlocked would be overstating the case."""
        outcome = build("engine-fault").run_once()
        assert outcome.comparison is None
        assert outcome.record.blueprint_divergence == ""

    def test_an_unparseable_reply_is_still_compared(self) -> None:
        """Here the model DID speak, so the blueprint had text to act on."""
        outcome = build("garbled").run_once()
        assert outcome.comparison is not None
        assert outcome.comparison.divergence is Divergence.UNSAFE


class TestTheComparisonIsNotRigged:
    def test_the_blueprint_gets_a_clean_kit_right(self) -> None:
        assert compare(
            "All required items are present and appear serviceable.", Verdict.PASS
        ).divergence is Divergence.AGREED

    def test_the_blueprint_gets_an_empty_kit_right(self) -> None:
        assert compare(
            "The tray is empty. Every required item is missing.", Verdict.FAIL
        ).divergence is Divergence.AGREED

    def test_simulated_scenes_do_not_all_diverge(self) -> None:
        """If every scene made the blueprint look bad, the scene set would be
        the argument rather than the evidence."""
        divergences = {
            scene: build(scene).run_once().record.blueprint_divergence
            for scene in ("complete", "empty", "missing-shears", "occluded")
        }
        assert divergences["complete"] == "agreed"
        assert divergences["empty"] == "agreed"
        assert divergences["missing-shears"] == "unsafe"
        assert divergences["occluded"] == "unsafe"
