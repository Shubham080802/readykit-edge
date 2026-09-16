"""The tamper-evident audit chain.

An Inspection Record is the answer to "was this kit checked, and what did the
system see?" That answer is only worth something if you can also show the
record has not been edited since.

These tests are written as the attacks: edit a verdict, delete an
inconvenient record, reorder history, append a forgery. Each must be caught,
and a torn write after a power cut must NOT be reported as an attack.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from readykit.domain import (
    InspectionRecord,
    Presence,
    Resolution,
    Sighting,
    Verdict,
)
from readykit.recorder import (
    _TAIL_CHUNK,
    GENESIS,
    ChainStatus,
    InspectionLog,
)


def make_record(verdict: Verdict = Verdict.PASS, ident: str = "a1") -> InspectionRecord:
    return InspectionRecord(
        inspection_id=ident,
        manifest_id="trauma-kit-a",
        started_at=datetime.now(UTC),
        resolution=Resolution(verdict=verdict, reason=f"{verdict.value} reason"),
        sightings=(Sighting("shears", Presence.FOUND, 0.9),),
        commanded="RELEASE seq=1 ack=OK",
    )


@pytest.fixture
def log(tmp_path: Path) -> InspectionLog:
    return InspectionLog(tmp_path / "inspections.jsonl")


def fill(log: InspectionLog, count: int = 4) -> None:
    for index in range(count):
        log.append(make_record(ident=f"rec{index}"))


class TestAnUntouchedChain:
    def test_a_log_that_does_not_exist_is_not_a_clean_bill_of_health(
        self, log: InspectionLog
    ) -> None:
        """Deleting the file outright is the most obvious move against an
        audit trail. Editing a record is caught by the chain; removing the
        evidence wholesale has to be caught here, or the tamper-evidence
        claim is false."""
        result = log.verify()
        assert result.status is ChainStatus.ABSENT
        assert not result.ok
        assert "not the same as a verified chain" in result.detail

    def test_an_existing_but_empty_log_verifies(
        self, log: InspectionLog
    ) -> None:
        """Distinct from absent, and genuinely fine: the file was created and
        nothing has been written to it yet."""
        log.path.parent.mkdir(parents=True, exist_ok=True)
        log.path.write_text("")
        result = log.verify()
        assert result.status is ChainStatus.EMPTY
        assert result.ok

    def test_a_log_of_only_blank_lines_is_empty_not_tampered(
        self, log: InspectionLog
    ) -> None:
        log.path.parent.mkdir(parents=True, exist_ok=True)
        log.path.write_text("\n   \n\n")
        assert log.verify().status is ChainStatus.EMPTY

    def test_a_written_chain_verifies(self, log: InspectionLog) -> None:
        fill(log, 5)
        result = log.verify()
        assert result.status is ChainStatus.INTACT
        assert result.verified == 5
        assert result.ok

    def test_the_first_record_chains_to_genesis(self, log: InspectionLog) -> None:
        payload = log.append(make_record())
        assert payload["prev_hash"] == GENESIS
        assert payload["seq"] == 0

    def test_each_record_chains_to_the_one_before(self, log: InspectionLog) -> None:
        first = log.append(make_record(ident="one"))
        second = log.append(make_record(ident="two"))
        assert second["prev_hash"] == first["hash"]
        assert second["seq"] == 1

    def test_identical_content_at_different_positions_hashes_differently(
        self, log: InspectionLog
    ) -> None:
        """Otherwise two identical inspections could be swapped undetected."""
        first = log.append(make_record(ident="same"))
        second = log.append(make_record(ident="same"))
        assert first["hash"] != second["hash"]


class TestEditingIsCaught:
    def test_changing_a_verdict_breaks_the_record_hash(
        self, log: InspectionLog
    ) -> None:
        """The attack this exists for: turning a recorded FAIL into a PASS."""
        fill(log, 3)
        lines = log.path.read_text().splitlines()
        lines[1] = lines[1].replace('"verdict":"pass"', '"verdict":"fail"')
        log.path.write_text("\n".join(lines) + "\n")

        result = log.verify()
        assert result.status is ChainStatus.TAMPERED
        assert result.broken_at == 1
        assert "edited" in result.detail

    def test_changing_a_reason_is_caught(self, log: InspectionLog) -> None:
        fill(log, 3)
        lines = log.path.read_text().splitlines()
        lines[2] = lines[2].replace("pass reason", "all good, honest")
        log.path.write_text("\n".join(lines) + "\n")
        assert log.verify().status is ChainStatus.TAMPERED

    def test_verification_reports_how_far_it_got(self, log: InspectionLog) -> None:
        fill(log, 5)
        lines = log.path.read_text().splitlines()
        lines[3] = lines[3].replace("pass reason", "tampered")
        log.path.write_text("\n".join(lines) + "\n")

        result = log.verify()
        assert result.verified == 3
        assert result.total == 5


class TestDeletingIsCaught:
    def test_removing_a_record_breaks_the_chain(self, log: InspectionLog) -> None:
        """Deleting the inspection that failed is the obvious way to cheat."""
        fill(log, 4)
        lines = log.path.read_text().splitlines()
        del lines[1]
        log.path.write_text("\n".join(lines) + "\n")

        result = log.verify()
        assert result.status is ChainStatus.TAMPERED
        assert result.broken_at == 1
        assert "removed, reordered, or inserted" in result.detail

    def test_removing_the_first_record_is_caught(self, log: InspectionLog) -> None:
        fill(log, 3)
        lines = log.path.read_text().splitlines()
        del lines[0]
        log.path.write_text("\n".join(lines) + "\n")

        result = log.verify()
        assert result.status is ChainStatus.TAMPERED
        assert result.broken_at == 0

    def test_truncating_history_wholesale_is_caught(
        self, log: InspectionLog
    ) -> None:
        """Keeping only the tail leaves a chain that does not start at
        genesis."""
        fill(log, 5)
        lines = log.path.read_text().splitlines()
        log.path.write_text("\n".join(lines[3:]) + "\n")
        assert log.verify().status is ChainStatus.TAMPERED


class TestReorderingIsCaught:
    def test_swapping_two_records_is_caught(self, log: InspectionLog) -> None:
        fill(log, 4)
        lines = log.path.read_text().splitlines()
        lines[1], lines[2] = lines[2], lines[1]
        log.path.write_text("\n".join(lines) + "\n")
        assert log.verify().status is ChainStatus.TAMPERED

    def test_duplicating_a_record_is_caught(self, log: InspectionLog) -> None:
        fill(log, 3)
        lines = log.path.read_text().splitlines()
        lines.insert(2, lines[1])
        log.path.write_text("\n".join(lines) + "\n")
        assert log.verify().status is ChainStatus.TAMPERED


class TestPowerLossIsNotTampering:
    def test_a_torn_final_line_is_reported_as_truncation(
        self, log: InspectionLog
    ) -> None:
        """An air-gapped field device will lose power mid-write eventually.
        Reporting that as tampering would cry wolf and train operators to
        ignore the alarm."""
        fill(log, 3)
        text = log.path.read_text()
        log.path.write_text(text[: -len(text) // 4])

        result = log.verify()
        assert result.status is ChainStatus.TRUNCATED
        assert result.verified == 2
        assert "power loss" in result.detail

    def test_records_before_a_torn_line_are_still_readable(
        self, log: InspectionLog
    ) -> None:
        fill(log, 3)
        text = log.path.read_text()
        log.path.write_text(text[: -len(text) // 4])
        assert len(log.read()) == 2

    def test_appending_after_a_torn_line_still_chains(
        self, log: InspectionLog
    ) -> None:
        """The head is the last COMPLETE record, so recovery continues the
        chain rather than starting a new one."""
        fill(log, 3)
        text = log.path.read_text()
        log.path.write_text(text[: -len(text) // 4])

        appended = log.append(make_record(ident="after"))
        assert appended["seq"] == 2


class TestUnchainedRecords:
    def test_a_record_with_no_chain_fields_is_flagged(
        self, log: InspectionLog
    ) -> None:
        log.path.write_text('{"inspection_id":"old","verdict":"pass"}\n')
        result = log.verify()
        assert result.status is ChainStatus.UNCHAINED
        assert not result.ok


class TestAppendingStaysCheapAsHistoryGrows:
    """`append` asks for the head once per inspection, so whatever finding the
    head costs, every inspection pays - for the rest of the device's life.

    Walking the log front to back made that the length of the history behind
    it: over 4,000 records of a seven-item manifest, the first append took
    0.25ms and the four-thousandth 26ms and was still climbing. A device
    running `readykit sentinel --interval 0.4` passes 4,000 inspections in
    under half an hour.

    An audit trail that gets slower the longer it has been trusted is one an
    operator eventually turns off, so the cost is pinned here rather than
    left to be rediscovered.
    """

    def test_finding_the_head_parses_one_record_not_the_whole_log(
        self, log: InspectionLog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Six hundred records of history, one record parsed to find the head.

        Counting the parses rather than timing the call, so this states the
        property instead of measuring the machine it runs on.
        """
        fill(log, 600)
        assert log.path.stat().st_size > 2 * _TAIL_CHUNK, "too little history"

        parses = 0
        real_loads = json.loads

        def counting_loads(text: str | bytes, *args: Any, **kwargs: Any) -> Any:
            nonlocal parses
            parses += 1
            return real_loads(text, *args, **kwargs)

        monkeypatch.setattr(json, "loads", counting_loads)
        head = log.head()

        assert head is not None
        assert head["seq"] == 599
        assert parses == 1

    def test_a_head_record_longer_than_one_tail_read_is_still_found(
        self, log: InspectionLog
    ) -> None:
        """A record can be longer than the tail read on its own - the verbatim
        model reply is on the record, and a model can be verbose - so it has
        to be reassembled across reads rather than mistaken for a torn line.

        The padding is multi-byte on purpose: a read boundary will land inside
        a character, and a character decoded in halves would corrupt the line
        that the whole chain hangs off.
        """
        fill(log, 2)
        verbose = replace(
            make_record(ident="verbose"), raw_reply="é" * (2 * _TAIL_CHUNK)
        )
        log.append(verbose)

        head = log.head()
        assert head is not None
        assert head["inspection_id"] == "verbose"
        assert head["seq"] == 2
        assert log.verify().status is ChainStatus.INTACT

    def test_a_mangled_line_early_in_the_log_does_not_block_appending(
        self, log: InspectionLog
    ) -> None:
        """One bad sector, or a line recovered around after a torn write, is
        history the head no longer needs to reach. Decoding the whole file to
        find the last line meant a single undecodable byte anywhere in it
        stopped the device recording anything ever again.
        """
        fill(log, 3)
        lines = log.path.read_bytes().split(b"\n")
        lines[0] = b"\xff\xfe not text at all"
        log.path.write_bytes(b"\n".join(lines))

        appended = log.append(make_record(ident="after"))
        assert appended["seq"] == 3
        assert appended["prev_hash"] != GENESIS

    def test_the_head_is_the_last_chained_record_not_the_last_line(
        self, log: InspectionLog
    ) -> None:
        """Reading backwards has to keep going past anything unchained, the
        same as the forward walk did."""
        fill(log, 3)
        with log.path.open("a", encoding="utf-8") as handle:
            handle.write('{"inspection_id":"unchained","verdict":"pass"}\n')
            handle.write("\n")

        head = log.head()
        assert head is not None
        assert head["seq"] == 2

    def test_a_log_of_only_blank_lines_has_no_head(
        self, log: InspectionLog
    ) -> None:
        log.path.write_text("\n\n\n")
        assert log.head() is None


class TestOrdinaryReadingStillWorks:
    def test_read_returns_most_recent_first(self, log: InspectionLog) -> None:
        log.append(make_record(Verdict.PASS, "first"))
        log.append(make_record(Verdict.FAIL, "second"))
        assert [r["inspection_id"] for r in log.read()] == ["second", "first"]

    def test_tally_counts_verdicts(self, log: InspectionLog) -> None:
        log.append(make_record(Verdict.PASS))
        log.append(make_record(Verdict.FAIL))
        log.append(make_record(Verdict.INDETERMINATE))
        assert log.tally() == {"pass": 1, "fail": 1, "indeterminate": 1}
