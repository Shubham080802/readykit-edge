"""Expiry and serviceability.

Presence is not serviceability. A sealed, undamaged, correctly-placed packet of
expired haemostatic gauze satisfies every visual check and is still not
something to hand a medic.

Expiry deliberately obeys the same rule as everything else rather than getting
a special case: a date that was never read is unresolved, not assumed fine.
"""

from __future__ import annotations

from datetime import date, timedelta

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
from readykit.reply import build_prompt, parse_reply

TODAY = date(2026, 6, 15)

KIT = Manifest(
    manifest_id="perishable",
    name="Perishable Kit",
    items=(
        RequiredItem(key="shears", label="Trauma Shears"),
        RequiredItem(key="gauze", label="Hemostatic Gauze", expiry_checked=True),
    ),
    expiry_warning_days=30,
)


def found(key: str, expiry: date | None = None) -> Sighting:
    return Sighting(
        key=key, presence=Presence.FOUND, confidence=0.95, expiry=expiry
    )


def at(days: int) -> date:
    return TODAY + timedelta(days=days)


class TestAnInDateKitPasses:
    def test_comfortably_in_date_passes(self) -> None:
        result = resolve_verdict(
            KIT, [found("shears"), found("gauze", at(400))], as_of=TODAY
        )
        assert result.verdict is Verdict.PASS
        assert result.expired == ()

    def test_an_item_expiring_today_is_still_serviceable(self) -> None:
        """Printed use-by dates are inclusive of the day itself - stock is
        usable through the printed date, not up to the day before it."""
        result = resolve_verdict(
            KIT, [found("shears"), found("gauze", TODAY)], as_of=TODAY
        )
        assert result.verdict is Verdict.PASS


class TestAnExpiredKitFails:
    def test_an_expired_critical_item_fails_the_whole_kit(self) -> None:
        """Every item is present. Every item is undamaged. It still fails."""
        result = resolve_verdict(
            KIT, [found("shears"), found("gauze", at(-1))], as_of=TODAY
        )
        assert result.verdict is Verdict.FAIL
        assert result.expired == ("gauze",)
        assert result.missing == ()
        assert result.damaged == ()
        assert "expired" in result.reason
        assert "Hemostatic Gauze" in result.reason

    def test_expiry_failure_is_reported_apart_from_missing_and_damaged(
        self,
    ) -> None:
        """Three different remedies - restock, replace, rotate - so they are
        three different buckets on the operator's screen."""
        result = resolve_verdict(
            KIT,
            [
                Sighting("shears", Presence.ABSENT, 0.95),
                found("gauze", at(-90)),
            ],
            as_of=TODAY,
        )
        assert result.verdict is Verdict.FAIL
        assert result.missing == ("shears",)
        assert result.expired == ("gauze",)

    def test_an_expired_advisory_item_does_not_fail_the_kit(self) -> None:
        manifest = Manifest(
            manifest_id="adv",
            name="Advisory",
            items=(
                RequiredItem(key="shears", label="Trauma Shears"),
                RequiredItem(
                    key="wipes",
                    label="Alcohol Wipes",
                    severity=Severity.ADVISORY,
                    expiry_checked=True,
                ),
            ),
        )
        result = resolve_verdict(
            manifest, [found("shears"), found("wipes", at(-10))], as_of=TODAY
        )
        assert result.verdict is Verdict.PASS
        assert result.advisories == ("wipes",)


class TestAnUnreadableDateIsNotAPass:
    def test_a_date_that_was_never_read_is_indeterminate(self) -> None:
        """The single most important case here. The item is present and
        undamaged, and its serviceability is simply unknown - so the kit
        cannot be cleared, exactly as with an occluded item."""
        result = resolve_verdict(
            KIT, [found("shears"), found("gauze", None)], as_of=TODAY
        )
        assert result.verdict is Verdict.INDETERMINATE
        assert result.unresolved == ("gauze",)
        assert result.expired == ()

    def test_an_unreadable_date_is_not_a_failure_either(self) -> None:
        """Nothing is known to be wrong with the item. Sending an operator to
        replace serviceable stock is its own kind of error."""
        result = resolve_verdict(
            KIT, [found("shears"), found("gauze", None)], as_of=TODAY
        )
        assert result.verdict is not Verdict.FAIL

    def test_an_item_not_expiry_checked_needs_no_date(self) -> None:
        result = resolve_verdict(
            KIT, [found("shears", None), found("gauze", at(400))], as_of=TODAY
        )
        assert result.verdict is Verdict.PASS


class TestTheWarningWindow:
    def test_an_item_nearing_expiry_passes_but_is_flagged(self) -> None:
        result = resolve_verdict(
            KIT, [found("shears"), found("gauze", at(10))], as_of=TODAY
        )
        assert result.verdict is Verdict.PASS
        assert result.expiring_soon == ("gauze",)
        assert "expiring soon" in result.reason

    def test_an_item_outside_the_window_is_not_flagged(self) -> None:
        result = resolve_verdict(
            KIT, [found("shears"), found("gauze", at(31))], as_of=TODAY
        )
        assert result.expiring_soon == ()

    def test_the_window_boundary_is_inclusive(self) -> None:
        result = resolve_verdict(
            KIT, [found("shears"), found("gauze", at(30))], as_of=TODAY
        )
        assert result.expiring_soon == ("gauze",)


class TestReadingPrintedDates:
    def _parse(self, raw_expiry: str) -> date | None:
        reply = (
            '{"items": [{"key": "gauze", "presence": "found", '
            f'"confidence": 0.9, "expiry": "{raw_expiry}"}}]}}'
        )
        return parse_reply(reply, KIT)[0].expiry

    @pytest.mark.parametrize(
        ("printed", "expected"),
        [
            ("2027-03-14", date(2027, 3, 14)),
            ("2027/03/14", date(2027, 3, 14)),
            ("14 Mar 2027", date(2027, 3, 14)),
        ],
    )
    def test_full_dates(self, printed: str, expected: date) -> None:
        assert self._parse(printed) == expected

    @pytest.mark.parametrize(
        ("printed", "expected"),
        [
            ("2027-02", date(2027, 2, 28)),
            ("2028-02", date(2028, 2, 29)),
            ("03/2027", date(2027, 3, 31)),
            ("Mar 2027", date(2027, 3, 31)),
        ],
    )
    def test_month_precision_resolves_to_the_last_day(
        self, printed: str, expected: date
    ) -> None:
        """"EXP 2027-03" means usable through 31 March - the pharmaceutical
        convention. Resolving to the 1st would retire good stock a month
        early: safe, but wrong, and it would erode trust in the system."""
        assert self._parse(printed) == expected

    @pytest.mark.parametrize(
        "printed", ["soon", "", "next year", "13/2027", "2027-13", "N/A"]
    )
    def test_unreadable_dates_become_none_rather_than_a_guess(
        self, printed: str
    ) -> None:
        """The one place a parser could manufacture compliance. Every
        ambiguity resolves to None, which upstream is unresolved."""
        assert self._parse(printed) is None

    def test_a_missing_expiry_field_is_none(self) -> None:
        reply = '{"items": [{"key": "gauze", "presence": "found", "confidence": 0.9}]}'
        assert parse_reply(reply, KIT)[0].expiry is None

    def test_a_non_string_expiry_is_none(self) -> None:
        reply = (
            '{"items": [{"key": "gauze", "presence": "found", '
            '"confidence": 0.9, "expiry": 2027}]}'
        )
        assert parse_reply(reply, KIT)[0].expiry is None


class TestThePrompt:
    def test_expiry_checked_items_are_marked(self) -> None:
        prompt = build_prompt(KIT)
        assert "READ THE EXPIRY DATE" in prompt
        assert "gauze" in prompt

    def test_the_prompt_tells_the_model_to_omit_rather_than_guess(self) -> None:
        """A model told to fill every field will invent a date, and an invented
        expiry is the one hallucination that manufactures a pass."""
        prompt = build_prompt(KIT)
        assert "omit the field" in prompt
        assert "invented one is not" in prompt

    def test_a_manifest_with_no_dated_items_gets_no_expiry_instructions(
        self,
    ) -> None:
        plain = Manifest(
            manifest_id="plain",
            name="Plain",
            items=(RequiredItem(key="shears", label="Trauma Shears"),),
        )
        assert "READ THE EXPIRY DATE" not in build_prompt(plain)
