"""Verdict resolution - the seam that decides whether a Latch opens.

Every test here is written from the operator's point of view: given what the
model saw, should this enclosure have unlocked?
"""

from __future__ import annotations

import pytest

from readykit.domain import (
    Manifest,
    Presence,
    RequiredItem,
    Severity,
    Sighting,
    Verdict,
    resolve_verdict,
)

TRAUMA_KIT = Manifest(
    manifest_id="trauma-a",
    name="Field Trauma Kit A",
    items=(
        RequiredItem(key="tourniquet", label="Tourniquet"),
        RequiredItem(key="chest_seal", label="Chest Seal"),
        RequiredItem(key="shears", label="Trauma Shears"),
    ),
    confidence_floor=0.6,
)


def found(key: str, confidence: float = 0.95) -> Sighting:
    return Sighting(key=key, presence=Presence.FOUND, confidence=confidence)


def all_found() -> list[Sighting]:
    return [found(key) for key in TRAUMA_KIT.keys]


class TestPassRequiresPositiveEvidence:
    def test_every_item_confidently_found_passes(self) -> None:
        result = resolve_verdict(TRAUMA_KIT, all_found())
        assert result.verdict is Verdict.PASS

    def test_empty_reply_is_indeterminate_not_pass(self) -> None:
        """A model that said nothing has not cleared the kit.

        This is the blueprint's fail-open bug: an empty or unparseable reply
        contains neither "missing" nor "no", so substring matching would call
        it PASS and release the latch on an unseen kit.
        """
        result = resolve_verdict(TRAUMA_KIT, [])
        assert result.verdict is Verdict.INDETERMINATE
        assert set(result.unresolved) == set(TRAUMA_KIT.keys)

    def test_item_the_model_never_mentioned_is_indeterminate(self) -> None:
        partial = [found("tourniquet"), found("chest_seal")]
        result = resolve_verdict(TRAUMA_KIT, partial)
        assert result.verdict is Verdict.INDETERMINATE
        assert result.unresolved == ("shears",)

    def test_unreadable_item_is_indeterminate_not_fail(self) -> None:
        sightings = [
            found("tourniquet"),
            found("chest_seal"),
            Sighting("shears", Presence.UNREADABLE, 0.9, "occluded by strap"),
        ]
        result = resolve_verdict(TRAUMA_KIT, sightings)
        assert result.verdict is Verdict.INDETERMINATE
        assert result.unresolved == ("shears",)
        assert result.missing == ()


class TestConfidenceFloor:
    def test_low_confidence_found_does_not_pass(self) -> None:
        sightings = [found("tourniquet"), found("chest_seal"), found("shears", 0.4)]
        result = resolve_verdict(TRAUMA_KIT, sightings)
        assert result.verdict is Verdict.INDETERMINATE

    def test_low_confidence_absent_is_indeterminate_not_fail(self) -> None:
        """A bad look at the kit is a reason to look again, not an accusation."""
        sightings = [
            found("tourniquet"),
            found("chest_seal"),
            Sighting("shears", Presence.ABSENT, 0.3),
        ]
        result = resolve_verdict(TRAUMA_KIT, sightings)
        assert result.verdict is Verdict.INDETERMINATE
        assert result.missing == ()

    def test_confidence_exactly_at_floor_counts(self) -> None:
        sightings = [found(key, 0.6) for key in TRAUMA_KIT.keys]
        result = resolve_verdict(TRAUMA_KIT, sightings)
        assert result.verdict is Verdict.PASS


class TestFailIsAPositiveFinding:
    def test_confidently_absent_critical_item_fails(self) -> None:
        sightings = [
            found("tourniquet"),
            found("chest_seal"),
            Sighting("shears", Presence.ABSENT, 0.97),
        ]
        result = resolve_verdict(TRAUMA_KIT, sightings)
        assert result.verdict is Verdict.FAIL
        assert result.missing == ("shears",)
        assert "Trauma Shears" in result.reason

    def test_damaged_item_fails_and_is_reported_apart_from_missing(self) -> None:
        sightings = [
            found("tourniquet"),
            found("chest_seal"),
            Sighting("shears", Presence.DAMAGED, 0.9, "blade bent"),
        ]
        result = resolve_verdict(TRAUMA_KIT, sightings)
        assert result.verdict is Verdict.FAIL
        assert result.damaged == ("shears",)
        assert result.missing == ()

    def test_unresolved_item_outranks_a_confirmed_failure(self) -> None:
        """If we cannot see the whole kit we cannot characterise it, even when
        part of what we did see was clearly bad."""
        sightings = [
            found("tourniquet"),
            Sighting("chest_seal", Presence.ABSENT, 0.99),
            Sighting("shears", Presence.UNREADABLE, 0.9),
        ]
        result = resolve_verdict(TRAUMA_KIT, sightings)
        assert result.verdict is Verdict.INDETERMINATE
        assert result.missing == ("chest_seal",)


class TestSeverity:
    def test_advisory_item_absent_still_passes(self) -> None:
        manifest = Manifest(
            manifest_id="tb-1",
            name="Toolbox",
            items=(
                RequiredItem(key="multimeter", label="Multimeter"),
                RequiredItem(
                    key="spare_fuse",
                    label="Spare Fuse",
                    severity=Severity.ADVISORY,
                ),
            ),
        )
        sightings = [found("multimeter"), Sighting("spare_fuse", Presence.ABSENT, 0.99)]
        result = resolve_verdict(manifest, sightings)
        assert result.verdict is Verdict.PASS
        assert result.advisories == ("spare_fuse",)
        assert result.missing == ()


class TestDoubtAboutAnAdvisoryItemDoesNotHoldTheLatch:
    """An advisory item cannot be worth more unseen than it is worth gone.

    The Manifest says this kit is serviceable without a spare fuse - a
    confident ABSENT passes it. So an occluded look at that same fuse must
    not hold the latch, or doubt about something that does not matter ends
    up stricter than certainty about it, and every inspection of a kit with
    one hard-to-see nice-to-have is INDETERMINATE forever.

    The failure this prevents is not a fail-open. It is an inspection station
    that cries wolf, which operators fix by propping it open.
    """

    TOOLBOX = Manifest(
        manifest_id="tb-2",
        name="Toolbox",
        items=(
            RequiredItem(key="multimeter", label="Multimeter"),
            RequiredItem(
                key="spare_fuse",
                label="Spare Fuses",
                severity=Severity.ADVISORY,
                quantity=4,
                expiry_checked=True,
            ),
        ),
        confidence_floor=0.6,
    )

    def test_an_unreadable_advisory_item_still_passes(self) -> None:
        sightings = [
            found("multimeter"),
            Sighting("spare_fuse", Presence.UNREADABLE, 0.9, "behind the lid"),
        ]
        result = resolve_verdict(self.TOOLBOX, sightings)
        assert result.verdict is Verdict.PASS
        assert result.unresolved == ()
        assert result.advisories == ("spare_fuse",)

    def test_an_advisory_item_the_model_never_mentioned_still_passes(self) -> None:
        result = resolve_verdict(self.TOOLBOX, [found("multimeter")])
        assert result.verdict is Verdict.PASS
        assert result.advisories == ("spare_fuse",)

    def test_a_low_confidence_advisory_reading_still_passes(self) -> None:
        sightings = [found("multimeter"), found("spare_fuse", 0.2)]
        result = resolve_verdict(self.TOOLBOX, sightings)
        assert result.verdict is Verdict.PASS
        assert result.advisories == ("spare_fuse",)

    def test_an_uncounted_advisory_item_still_passes(self) -> None:
        """Counting four small identical fuses is the model's weakest axis,
        and it is the one item on this manifest that does not matter."""
        sightings = [
            found("multimeter"),
            Sighting("spare_fuse", Presence.FOUND, 0.95, count=None, expiry=None),
        ]
        result = resolve_verdict(self.TOOLBOX, sightings)
        assert result.verdict is Verdict.PASS
        assert result.advisories == ("spare_fuse",)

    def test_an_advisory_item_is_named_once_however_many_findings_it_has(
        self,
    ) -> None:
        """Present, but neither counted nor dated - two findings, one item."""
        sightings = [
            found("multimeter"),
            Sighting("spare_fuse", Presence.FOUND, 0.95),
        ]
        result = resolve_verdict(self.TOOLBOX, sightings)
        assert result.advisories == ("spare_fuse",)

    def test_a_pass_does_not_claim_an_unseen_advisory_item_was_serviceable(
        self,
    ) -> None:
        """"All 2 required items present and serviceable" would be a false
        statement on the record when one of the two was never seen."""
        result = resolve_verdict(self.TOOLBOX, [found("multimeter")])
        assert result.reason == (
            "All 1 critical items present and serviceable; advisory: Spare Fuses"
        )

    def test_doubt_about_a_critical_item_still_holds_the_latch(self) -> None:
        """The rule bends for advisory items only."""
        sightings = [
            Sighting("multimeter", Presence.UNREADABLE, 0.9),
            found("spare_fuse", 0.95),
        ]
        result = resolve_verdict(self.TOOLBOX, sightings)
        assert result.verdict is Verdict.INDETERMINATE
        assert result.unresolved == ("multimeter",)


class TestAdversarialReplies:
    def test_duplicate_sightings_keep_the_least_favourable(self) -> None:
        sightings = [
            found("tourniquet"),
            found("chest_seal"),
            found("shears"),
            Sighting("shears", Presence.ABSENT, 0.99),
        ]
        result = resolve_verdict(TRAUMA_KIT, sightings)
        assert result.verdict is Verdict.FAIL

    def test_items_outside_the_manifest_are_ignored(self) -> None:
        sightings = [*all_found(), Sighting("coffee_mug", Presence.FOUND, 0.99)]
        result = resolve_verdict(TRAUMA_KIT, sightings)
        assert result.verdict is Verdict.PASS

    def test_extra_items_cannot_substitute_for_a_required_one(self) -> None:
        sightings = [
            found("tourniquet"),
            found("chest_seal"),
            Sighting("decorative_shears", Presence.FOUND, 0.99),
        ]
        result = resolve_verdict(TRAUMA_KIT, sightings)
        assert result.verdict is Verdict.INDETERMINATE


class TestManifestValidation:
    def test_manifest_rejects_duplicate_keys(self) -> None:
        with pytest.raises(ValueError, match="duplicate item key"):
            Manifest(
                manifest_id="dup",
                name="Dup",
                items=(
                    RequiredItem(key="a", label="A"),
                    RequiredItem(key="a", label="A again"),
                ),
            )

    def test_manifest_rejects_empty_item_list(self) -> None:
        with pytest.raises(ValueError, match="no items"):
            Manifest(manifest_id="empty", name="Empty", items=())

    def test_sighting_rejects_out_of_range_confidence(self) -> None:
        with pytest.raises(ValueError, match="confidence"):
            Sighting("x", Presence.FOUND, 1.4)
