"""Append-only, tamper-evident Inspection Records.

For a medical or disaster kit, "the system said it was fine" is only worth
something if you can show what the system actually saw, and show that the
showing has not been edited since. Records are written as JSON Lines - one
inspection per line, appended and flushed immediately, never rewritten - and
each line is hash-chained to the one before it.

    hash(n) = blake2b( canonical_json(record(n) with prev_hash = hash(n-1)) )

Editing any recorded field, deleting a line, or reordering lines breaks the
chain at a detectable point. `readykit audit` walks it and reports where.

**What this is and is not.** It is tamper-*evident*, not tamper-*proof*. It
detects accidental corruption, a torn write after a power cut, and anyone who
edits or removes history without recomputing the rest of the chain. It does
not stop someone with write access to the file and the will to rebuild every
subsequent hash - that needs a signing key in a secure element, or anchoring
the head hash somewhere outside the device. Neither is in scope here, and
claiming otherwise would be worse than not having the chain at all.

The format stays deliberately dumb so a partially-written file after a power
cut is still readable up to the last complete line, which is exactly the
situation an air-gapped field device will eventually be in.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from hashlib import blake2b
from pathlib import Path
from typing import Any

from .domain import InspectionRecord

GENESIS = "0" * 32
"""The prev_hash of the first record in a log. A chain that does not begin
here did not begin at the beginning."""

_DIGEST_SIZE = 16

_TAIL_CHUNK = 65536
"""How much of the log's tail to read at a time when looking for the head.

Comfortably larger than one record - the seven-item manifests here write
about 1.3KB - so the usual case is one read and done. A log whose final
records are all torn or unchained just reads another block.
"""


class ChainStatus(StrEnum):
    INTACT = "intact"

    EMPTY = "empty"
    """The log exists and holds no records. A real, ordinary state: the file
    was created and nothing has been written to it yet."""

    ABSENT = "absent"
    """There is no log at all.

    Deliberately not the same as EMPTY, and deliberately not `ok`. Deleting
    the file outright is the most obvious move against an audit trail, and
    reporting success for it would make the tamper-evidence claim false -
    editing a record is caught by the chain, but removing the evidence
    wholesale would not be.

    On a machine that has genuinely never run an inspection this is also what
    you get, and that is the right answer too: there is nothing here to
    verify, which is not the same as having verified something."""

    TAMPERED = "tampered"
    """A record's contents do not match its recorded hash, or its prev_hash
    does not match the previous record. Something was edited, removed, or
    reordered."""

    TRUNCATED = "truncated"
    """The final line is incomplete - the expected result of losing power
    mid-write. Everything before it verified, and this is not tampering."""

    UNCHAINED = "unchained"
    """A record carries no chain fields at all. Written by an older version,
    or by something that is not this recorder."""


@dataclass(frozen=True, slots=True)
class ChainResult:
    status: ChainStatus
    verified: int
    """How many records verified before the walk stopped."""

    total: int
    broken_at: int | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        # ABSENT is excluded on purpose - see ChainStatus.ABSENT.
        return self.status in (ChainStatus.INTACT, ChainStatus.EMPTY)


def record_hash(payload: dict[str, Any]) -> str:
    """Hash one record's canonical form.

    `sort_keys` plus the tightest separators make this independent of dict
    ordering and of whitespace, so a record re-serialised by a different
    version of Python still hashes the same.
    """
    body = {k: v for k, v in payload.items() if k != "hash"}
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True)
    return blake2b(canonical.encode("utf-8"), digest_size=_DIGEST_SIZE).hexdigest()


class InspectionLog:
    """Appends hash-chained Inspection Records to a JSONL file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- writing -------------------------------------------------------------

    def append(self, record: InspectionRecord) -> dict[str, Any]:
        """Write one record, chained to the current head."""
        head = self.head()
        payload = record.to_payload()
        payload["seq"] = (head["seq"] + 1) if head else 0
        payload["prev_hash"] = head["hash"] if head else GENESIS
        payload["hash"] = record_hash(payload)

        line = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        # newline="" so Windows does not translate to \r\n. The hash chain
        # survives either way - verification strips line endings - but an
        # audit file that is byte-identical on every platform is one an
        # auditor can diff between machines without explaining the noise.
        with self.path.open("a", encoding="utf-8", newline="") as handle:
            handle.write(line + "\n")
            handle.flush()
        return payload

    def head(self) -> dict[str, Any] | None:
        """The last complete, chain-bearing record, or None.

        Read backwards from the end of the file, because `append` calls this
        once per inspection and the answer is almost always the final line.
        Walking the log front to back instead made every append cost the whole
        history behind it: measured over 4,000 records of a real seven-item
        manifest, the first append took 0.25ms and the four-thousandth 26ms,
        still climbing, 49s of wall clock to write a 5MB log. A device running
        `readykit sentinel --interval 0.4` reaches 4,000 inspections in under
        half an hour and then keeps going, so the log got slower to append to
        the longer the device had been trusted with it - and the audit trail is
        the last part of this system that should punish being used.

        It also stopped holding the entire parsed log in memory to find one
        line of it.

        Still derived from the file on every call rather than cached, so a log
        that changed underneath this process - recovered from a backup, or
        truncated by hand between inspections - is read as it now is. A stale
        cached head would chain new records onto a predecessor that is no
        longer there, which `verify` would report as tampering.
        """
        for line in self._iter_lines_backwards():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # A torn final line from an interrupted write, or anything
                # else unparseable. The head is the last *complete* record, so
                # keep walking back to it.
                continue
            if isinstance(row, dict) and "hash" in row and "seq" in row:
                return row
        return None

    def _iter_lines_backwards(self) -> Iterator[str]:
        """Yield the log's non-blank lines from the end towards the start.

        Splitting on b"\n" is safe for UTF-8 whatever the chunk boundary
        lands on: 0x0A never appears inside a multi-byte sequence, and the
        leading partial line of each block is held back until the block before
        it is read, so no character is ever decoded in halves.

        Undecodable bytes are replaced rather than raising. A torn write can
        leave a half-written character behind, and that line simply fails to
        parse as JSON, which is exactly how it should be treated - the
        alternative is an audit log that cannot be appended to after a power
        cut.
        """
        if not self.path.exists():
            return

        position = self.path.stat().st_size
        with self.path.open("rb") as handle:
            carry = b""
            while position > 0:
                size = min(_TAIL_CHUNK, position)
                position -= size
                handle.seek(position)
                lines = (handle.read(size) + carry).split(b"\n")
                carry = lines.pop(0)
                for raw in reversed(lines):
                    if raw.strip():
                        yield raw.strip().decode("utf-8", errors="replace")
            if carry.strip():
                yield carry.strip().decode("utf-8", errors="replace")

    # -- reading -------------------------------------------------------------

    def read(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Most recent first. Malformed trailing lines are skipped, not fatal."""
        rows: list[dict[str, Any]] = list(self._iter_rows())
        rows.reverse()
        return rows[:limit] if limit is not None else rows

    def _iter_rows(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    row = json.loads(stripped)
                except json.JSONDecodeError:
                    # A torn final line from an interrupted write. Everything
                    # before it is still good; verify() reports it honestly as
                    # truncation rather than as tampering.
                    continue
                if isinstance(row, dict):
                    yield row

    def tally(self) -> dict[str, int]:
        counts = {"pass": 0, "fail": 0, "indeterminate": 0}
        for row in self._iter_rows():
            verdict = row.get("verdict")
            if verdict in counts:
                counts[verdict] += 1
        return counts

    # -- verifying -----------------------------------------------------------

    def verify(self) -> ChainResult:
        """Walk the chain from the genesis record and report the first break."""
        if not self.path.exists():
            return ChainResult(
                ChainStatus.ABSENT,
                0,
                0,
                detail=(
                    "there is no log at this path, so nothing could be "
                    "verified - which is not the same as a verified chain"
                ),
            )

        raw_lines = [
            line.strip()
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if not raw_lines:
            return ChainResult(ChainStatus.EMPTY, 0, 0)

        total = len(raw_lines)
        expected_prev = GENESIS
        verified = 0

        for index, line in enumerate(raw_lines):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                if index == total - 1:
                    return ChainResult(
                        ChainStatus.TRUNCATED,
                        verified,
                        total,
                        broken_at=index,
                        detail=(
                            "the final line is incomplete, consistent with "
                            "power loss during a write; every earlier record "
                            "verified"
                        ),
                    )
                return ChainResult(
                    ChainStatus.TAMPERED,
                    verified,
                    total,
                    broken_at=index,
                    detail=f"line {index} is not valid JSON and is not the last line",
                )

            if not isinstance(row, dict) or "hash" not in row or "seq" not in row:
                return ChainResult(
                    ChainStatus.UNCHAINED,
                    verified,
                    total,
                    broken_at=index,
                    detail=f"record {index} carries no chain fields",
                )

            if row.get("prev_hash") != expected_prev:
                return ChainResult(
                    ChainStatus.TAMPERED,
                    verified,
                    total,
                    broken_at=index,
                    detail=(
                        f"record {index} expects predecessor "
                        f"{str(row.get('prev_hash'))[:12]}… but the chain "
                        f"reached {expected_prev[:12]}… - a record was "
                        "removed, reordered, or inserted"
                    ),
                )

            recomputed = record_hash(row)
            if recomputed != row["hash"]:
                return ChainResult(
                    ChainStatus.TAMPERED,
                    verified,
                    total,
                    broken_at=index,
                    detail=(
                        f"record {index} ({row.get('inspection_id', '?')}) does "
                        "not match its own hash - its contents were edited "
                        "after it was written"
                    ),
                )

            if row["seq"] != index:
                return ChainResult(
                    ChainStatus.TAMPERED,
                    verified,
                    total,
                    broken_at=index,
                    detail=f"record {index} claims sequence {row['seq']}",
                )

            expected_prev = str(row["hash"])
            verified += 1

        return ChainResult(ChainStatus.INTACT, verified, total)
