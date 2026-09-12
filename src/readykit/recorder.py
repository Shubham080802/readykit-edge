"""Append-only Inspection Records.

For a medical or disaster kit, "the system said it was fine" is only worth
something if you can show what the system actually saw. Records are written as
JSON Lines: one inspection per line, appended and flushed immediately, never
rewritten.

The format is deliberately dumb so that a partially-written file after a power
cut is still readable up to the last complete line - which is exactly the
situation an air-gapped field device will eventually be in.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .domain import InspectionRecord


class InspectionLog:
    """Appends Inspection Records to a JSONL file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: InspectionRecord) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(record.to_json_line() + "\n")
            handle.flush()

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
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    # A torn final line from an interrupted write. Everything
                    # before it is still good.
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
