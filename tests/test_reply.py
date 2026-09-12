"""Parsing a model reply into Sightings.

A VLM returns prose, or fenced JSON, or fenced JSON wrapped in prose, or an
apology. The parser's job is to extract only what the model actually committed
to. Everything it cannot extract must come back as nothing at all, so that
`resolve_verdict` sees an unresolved item and the Latch stays engaged.

The one thing this parser must never do is manufacture a FOUND.
"""

from __future__ import annotations

import pytest

from readykit.domain import Manifest, Presence, RequiredItem, Sighting
from readykit.reply import ReplyParseError, build_prompt, parse_reply

KIT = Manifest(
    manifest_id="k",
    name="Kit",
    items=(
        RequiredItem(key="multimeter", label="Multimeter"),
        RequiredItem(key="hardhat", label="Hard Hat"),
    ),
)


def by_key(sightings: list[Sighting]) -> dict[str, Sighting]:
    return {s.key: s for s in sightings}


class TestWellFormedReplies:
    def test_bare_json_object(self) -> None:
        raw = """
        {"items": [
          {"key": "multimeter", "presence": "found", "confidence": 0.91},
          {"key": "hardhat", "presence": "absent", "confidence": 0.88}
        ]}
        """
        result = by_key(parse_reply(raw, KIT))
        assert result["multimeter"].presence is Presence.FOUND
        assert result["hardhat"].presence is Presence.ABSENT
        assert result["hardhat"].confidence == pytest.approx(0.88)

    def test_json_inside_a_markdown_fence(self) -> None:
        raw = (
            "Here is my assessment:\n\n"
            "```json\n"
            '{"items": [{"key": "multimeter", "presence": "found", '
            '"confidence": 0.9}]}\n'
            "```\n\nHope that helps!"
        )
        result = by_key(parse_reply(raw, KIT))
        assert result["multimeter"].presence is Presence.FOUND

    def test_notes_are_preserved_for_the_audit_trail(self) -> None:
        raw = (
            '{"items": [{"key": "multimeter", "presence": "damaged", '
            '"confidence": 0.8, "note": "cracked display"}]}'
        )
        result = by_key(parse_reply(raw, KIT))
        assert result["multimeter"].note == "cracked display"


class TestNeverInventsAFinding:
    def test_prose_with_no_json_raises(self) -> None:
        with pytest.raises(ReplyParseError):
            parse_reply("Everything looks good to me!", KIT)

    def test_the_blueprints_fail_open_string_yields_nothing(self) -> None:
        """"I cannot determine..." must not become a pass.

        Under substring matching this reply contains neither "missing" nor
        "no", and would have been read as PASS.
        """
        with pytest.raises(ReplyParseError):
            parse_reply("I cannot determine the contents; the tray is occluded.", KIT)

    def test_empty_reply_raises(self) -> None:
        with pytest.raises(ReplyParseError):
            parse_reply("", KIT)

    def test_item_omitted_by_the_model_produces_no_sighting(self) -> None:
        raw = '{"items": [{"key": "multimeter", "presence": "found", "confidence": 0.9}]}'
        result = parse_reply(raw, KIT)
        assert by_key(result).keys() == {"multimeter"}

    def test_unknown_presence_becomes_unreadable_not_found(self) -> None:
        raw = (
            '{"items": [{"key": "multimeter", "presence": "probably there", '
            '"confidence": 0.9}]}'
        )
        result = by_key(parse_reply(raw, KIT))
        assert result["multimeter"].presence is Presence.UNREADABLE

    def test_missing_confidence_is_treated_as_zero_not_certain(self) -> None:
        raw = '{"items": [{"key": "multimeter", "presence": "found"}]}'
        result = by_key(parse_reply(raw, KIT))
        assert result["multimeter"].confidence == 0.0

    def test_out_of_range_confidence_is_clamped(self) -> None:
        raw = '{"items": [{"key": "multimeter", "presence": "found", "confidence": 7}]}'
        result = by_key(parse_reply(raw, KIT))
        assert result["multimeter"].confidence == 1.0

    def test_items_not_on_the_manifest_are_dropped(self) -> None:
        raw = (
            '{"items": [{"key": "sandwich", "presence": "found", "confidence": 1.0}]}'
        )
        assert parse_reply(raw, KIT) == []

    def test_malformed_entries_are_skipped_not_guessed(self) -> None:
        raw = (
            '{"items": [{"presence": "found", "confidence": 0.9}, '
            '{"key": "hardhat", "presence": "found", "confidence": 0.9}]}'
        )
        result = by_key(parse_reply(raw, KIT))
        assert result.keys() == {"hardhat"}

    def test_json_that_is_not_an_object_raises(self) -> None:
        with pytest.raises(ReplyParseError):
            parse_reply("[1, 2, 3]", KIT)


class TestPrompt:
    def test_prompt_names_every_required_item(self) -> None:
        prompt = build_prompt(KIT)
        assert "multimeter" in prompt
        assert "hardhat" in prompt
        assert "Multimeter" in prompt

    def test_prompt_offers_unreadable_as_an_option(self) -> None:
        """The model needs a way to say 'I could not see it' that is not a
        guess in either direction."""
        assert "unreadable" in build_prompt(KIT)
