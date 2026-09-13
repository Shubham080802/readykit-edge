"""Multi-frame aggregation.

Deciding on a single glance is fragile: one glare frame, one hand over the
tray, one autofocus hunt, and a compliant kit reads as INDETERMINATE. Looking
more than once fixes that - but only if disagreement between looks is reported
as doubt rather than averaged away.
"""

from __future__ import annotations

from datetime import date

import pytest

from readykit.aggregate import aggregate_sightings
from readykit.bridge import LoopbackLink, VirtualActuatorNode
from readykit.bridge.loopback import LatchState
from readykit.capture import Frame, FrameSource
from readykit.domain import Manifest, Presence, RequiredItem, Sighting, Verdict
from readykit.engine import InspectionEngine
from readykit.inference.base import InferenceEngine, InferenceError, Observation

KIT = Manifest(
    manifest_id="agg",
    name="Aggregation Kit",
    items=(
        RequiredItem(key="shears", label="Trauma Shears"),
        RequiredItem(key="gauze", label="Hemostatic Gauze"),
    ),
)


def s(
    key: str,
    presence: Presence = Presence.FOUND,
    confidence: float = 0.9,
    expiry: date | None = None,
) -> Sighting:
    return Sighting(key=key, presence=presence, confidence=confidence, expiry=expiry)


def only(sightings: list[Sighting], key: str) -> Sighting:
    return next(x for x in sightings if x.key == key)


class TestAgreement:
    def test_unanimous_frames_keep_their_reading(self) -> None:
        result = aggregate_sightings([[s("shears")], [s("shears")], [s("shears")]])
        assert only(result, "shears").presence is Presence.FOUND

    def test_a_single_frame_passes_through_unchanged(self) -> None:
        frame = [s("shears", Presence.ABSENT, 0.8)]
        assert aggregate_sightings([frame]) == frame

    def test_a_clear_majority_wins(self) -> None:
        """One glare frame out of three must not condemn an intact kit."""
        result = aggregate_sightings(
            [
                [s("shears", Presence.FOUND)],
                [s("shears", Presence.FOUND)],
                [s("shears", Presence.UNREADABLE)],
            ]
        )
        assert only(result, "shears").presence is Presence.FOUND

    def test_confidence_averages_over_agreeing_frames_only(self) -> None:
        """A strong reading must not be diluted by frames that saw something
        else entirely."""
        result = aggregate_sightings(
            [
                [s("shears", Presence.FOUND, 0.9)],
                [s("shears", Presence.FOUND, 0.7)],
                [s("shears", Presence.UNREADABLE, 0.1)],
            ]
        )
        assert only(result, "shears").confidence == pytest.approx(0.8)


class TestDisagreementBecomesDoubt:
    def test_an_even_split_is_unreadable(self) -> None:
        """Two frames saying FOUND and two saying ABSENT is not "probably
        fine" - it is a kit nobody has established anything about."""
        result = aggregate_sightings(
            [
                [s("shears", Presence.FOUND)],
                [s("shears", Presence.FOUND)],
                [s("shears", Presence.ABSENT)],
                [s("shears", Presence.ABSENT)],
            ]
        )
        assert only(result, "shears").presence is Presence.UNREADABLE

    def test_disagreement_is_explained_in_the_note(self) -> None:
        result = aggregate_sightings(
            [
                [s("shears", Presence.FOUND)],
                [s("shears", Presence.ABSENT)],
            ]
        )
        assert "disagreed" in only(result, "shears").note

    def test_a_bare_majority_below_the_threshold_is_unreadable(self) -> None:
        result = aggregate_sightings(
            [
                [s("shears", Presence.FOUND)],
                [s("shears", Presence.FOUND)],
                [s("shears", Presence.ABSENT)],
                [s("shears", Presence.ABSENT)],
                [s("shears", Presence.DAMAGED)],
            ],
            min_agreement=0.6,
        )
        assert only(result, "shears").presence is Presence.UNREADABLE

    def test_agreement_is_measured_against_frames_that_spoke(self) -> None:
        """An item genuinely out of shot in one frame must not poison the
        reading from the frames that saw it clearly."""
        result = aggregate_sightings(
            [
                [s("shears"), s("gauze")],
                [s("shears"), s("gauze")],
                [s("shears")],
            ]
        )
        assert only(result, "gauze").presence is Presence.FOUND


class TestPessimisticTieBreak:
    def test_a_tie_resolves_against_the_kit(self) -> None:
        """With min_agreement satisfied by either side, optimism must not
        win the coin toss."""
        result = aggregate_sightings(
            [
                [s("shears", Presence.FOUND)],
                [s("shears", Presence.ABSENT)],
            ],
            min_agreement=0.5,
        )
        assert only(result, "shears").presence is Presence.ABSENT


class TestExpiryAcrossFrames:
    def test_an_agreed_date_survives(self) -> None:
        result = aggregate_sightings(
            [
                [s("gauze", expiry=date(2027, 3, 31))],
                [s("gauze", expiry=date(2027, 3, 31))],
            ]
        )
        assert only(result, "gauze").expiry == date(2027, 3, 31)

    def test_frames_reading_different_dates_establish_no_date(self) -> None:
        """Two frames disagreeing about a printed date means the date was not
        established - the same rule as presence, on the other evidence."""
        result = aggregate_sightings(
            [
                [s("gauze", expiry=date(2027, 3, 31))],
                [s("gauze", expiry=date(2021, 3, 31))],
            ]
        )
        assert only(result, "gauze").expiry is None

    def test_a_date_read_in_only_one_frame_still_counts(self) -> None:
        result = aggregate_sightings(
            [[s("gauze", expiry=date(2027, 3, 31))], [s("gauze")]]
        )
        assert only(result, "gauze").expiry == date(2027, 3, 31)


class TestGuards:
    def test_no_frames_yields_nothing(self) -> None:
        assert aggregate_sightings([]) == []

    @pytest.mark.parametrize("bad", [0.0, -0.5, 1.5])
    def test_an_out_of_range_threshold_is_rejected(self, bad: float) -> None:
        with pytest.raises(ValueError, match="min_agreement"):
            aggregate_sightings([[s("shears")]], min_agreement=bad)


class FlickeringSource(FrameSource):
    """Alternates between seeing the shears and not."""

    def __init__(self) -> None:
        self.count = 0

    def read(self) -> Frame:
        self.count += 1
        return Frame(image=f"frame-{self.count}", digest=f"d{self.count}")


class FlickeringEngine(InferenceEngine):
    name = "flickering"

    def infer(self, frame: Frame, manifest: Manifest) -> Observation:
        odd = frame.digest.endswith(("1", "3", "5"))
        return Observation(
            sightings=(
                s("shears", Presence.FOUND if odd else Presence.ABSENT),
                s("gauze"),
            ),
            raw_reply="{}",
        )


class SometimesBrokenEngine(InferenceEngine):
    name = "sometimes-broken"

    def __init__(self) -> None:
        self.calls = 0

    def infer(self, frame: Frame, manifest: Manifest) -> Observation:
        self.calls += 1
        if self.calls == 2:
            raise InferenceError("transient NPU hiccup")
        return Observation(
            sightings=(s("shears"), s("gauze")), raw_reply="{}"
        )


class TestThroughTheEngine:
    def test_a_flickering_kit_does_not_pass(self) -> None:
        node = VirtualActuatorNode()
        engine = InspectionEngine(
            KIT,
            FlickeringSource(),
            FlickeringEngine(),
            LoopbackLink(node),
            frames=4,
        )
        outcome = engine.run_once()
        assert outcome.verdict is Verdict.INDETERMINATE
        assert node.latch is LatchState.ENGAGED

    def test_one_failed_frame_does_not_abort_the_inspection(self) -> None:
        """We already have a good look; a transient hiccup on a later frame
        should cost us that frame, not the whole inspection."""
        engine = InspectionEngine(
            KIT, FlickeringSource(), SometimesBrokenEngine(), None, frames=3
        )
        outcome = engine.run_once()
        assert outcome.verdict is Verdict.PASS

    def test_latency_is_recorded_per_frame(self) -> None:
        engine = InspectionEngine(
            KIT, FlickeringSource(), FlickeringEngine(), None, frames=3
        )
        engine.run_once()
        assert len(engine.latencies_ms) == 3
