"""Name a source whose row count departs from its own recent runs (Issue #765).

A source that returns far fewer or far more rows than usual used to be
reported as collected with no warning. The seven cases that motivated this
are in the issue: a fixed 15-book longlist returning 13, a 20-row list
returning 42 or 1, a chart keeping 3 of 16 extracted rows.

Each fresh collection is compared with the same source's earlier fresh
collections, kept in the weekly state as ``source-row-history.json``. The
check is a warning in the run caveats; it never discards rows. A source with
too little history is not flagged.

A list kept without the rows that failed validation (Issue #839) is always
named. Otherwise three rules, all on validated rows:

- **Dropped rows.** At least ``MIN_EXTRACTED_FOR_DROP_RULE`` rows extracted and
  more than half dropped by validation.
- **A fixed list.** Every one of at least ``MIN_HISTORY`` earlier runs had the
  same count, and this run differs.
- **A varying list.** Below half or above double the median of at least
  ``MIN_HISTORY`` earlier runs, when that median is at least
  ``MIN_USUAL_FOR_VARYING_RULE``: for a list of one to four books, halving
  is ordinary variation.

A source with a seen-item index (Nonfiction.fr, Perlentaucher, Kirkus)
reports only books it has not shown before, so its count is not a list size
and it is neither compared nor recorded.
"""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

from autoscout.json_io import read_json_or_default
from autoscout.json_io import write_json
from autoscout.partial_list import PARTIAL_LIST_DEBUG_FIELD
from autoscout.source_run_status import COLLECTING_SOURCE_STATUSES
from autoscout.source_run_status import SOURCE_STATUS_COLLECTED
from autoscout.source_run_status import normalized_source_run_status

STATE_SOURCE_ROW_HISTORY = "source-row-history.json"
HISTORY_LENGTH = 8
MIN_HISTORY = 3
MIN_EXTRACTED_FOR_DROP_RULE = 5
MIN_USUAL_FOR_VARYING_RULE = 5

RULES = frozenset({"partial_list", "dropped_rows", "fixed_list", "varying_list"})

RowHistory = dict[str, list[dict[str, Any]]]


@dataclass(frozen=True)
class RowCountDeparture:
    """One source whose validated row count left its usual range this run."""

    source_name: str
    valid_rows: int
    extracted_rows: int
    rule: str
    expected: str
    usual: float | None = None

    def editor_detail(self) -> str:
        """Short Russian detail for the editor-facing caveats, with no codes or paths."""
        if self.rule in {"partial_list", "dropped_rows"}:
            return f"оставлено {self.valid_rows} из {self.extracted_rows} найденных"
        if self.usual is None:
            return str(self.valid_rows)
        if self.rule == "fixed_list":
            return f"{self.valid_rows} вместо обычных {self.usual:g}"
        return f"{self.valid_rows}, обычно около {self.usual:g}"

    def warning(self) -> str:
        if self.rule == "partial_list":
            return (
                f"{self.source_name}: kept {self.valid_rows} of {self.extracted_rows} extracted rows; "
                "the rows that failed validation were dropped and the rest of the list kept"
            )
        if self.rule == "dropped_rows":
            return (
                f"{self.source_name}: kept {self.valid_rows} of {self.extracted_rows} extracted rows; "
                "most rows were dropped by validation"
            )
        rows = "row" if self.valid_rows == 1 else "rows"
        return (
            f"{self.source_name}: {self.valid_rows} {rows}, {self.expected}; possibly a partial list or a page change"
        )

    @classmethod
    def from_dict(cls, record: Mapping[str, Any]) -> RowCountDeparture | None:
        """Rebuild a recorded departure, or None for a malformed record."""
        valid = _count(record.get("valid_rows"))
        extracted = _count(record.get("extracted_rows"))
        name = str(record.get("source_name", "")).strip()
        rule = str(record.get("rule", ""))
        if not name or valid is None or extracted is None or rule not in RULES:
            return None
        raw_usual = record.get("usual")
        usual = float(raw_usual) if isinstance(raw_usual, int | float) and not isinstance(raw_usual, bool) else None
        return cls(name, valid, extracted, rule, str(record.get("expected", "")), usual)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "valid_rows": self.valid_rows,
            "extracted_rows": self.extracted_rows,
            "rule": self.rule,
            "expected": self.expected,
            "usual": self.usual,
        }


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _fresh_counts(run: Mapping[str, Any]) -> tuple[str, int, int] | None:
    """Return (name, validated, extracted) for a freshly collected run, or None."""
    if normalized_source_run_status(run.get("status")) != SOURCE_STATUS_COLLECTED:
        return None
    name = str(run.get("source_name", "")).strip()
    valid = _count(run.get("valid_mention_count"))
    extracted = _count(run.get("extracted_mention_count"))
    if not name or valid is None:
        return None
    return name, valid, extracted if extracted is not None else valid


def _partial_list_counts(run: Mapping[str, Any]) -> tuple[str, int, int] | None:
    """(name, kept, extracted) when a contributing run kept a list without its failing rows (Issue #839)."""
    if normalized_source_run_status(run.get("status")) not in COLLECTING_SOURCE_STATUSES:
        return None
    name = str(run.get("source_name", "")).strip()
    if not name:
        return None
    debug = run.get("debug_metadata")
    marker = debug.get(PARTIAL_LIST_DEBUG_FIELD) if isinstance(debug, Mapping) else None
    if not isinstance(marker, Mapping):
        return None
    kept = _count(marker.get("kept_rows"))
    extracted = _count(marker.get("extracted_rows"))
    if kept is None or extracted is None or kept >= extracted:
        return None
    return name, kept, extracted


def load_row_history(state_dir: Path | None) -> RowHistory:
    """Read the retained history; a missing or malformed file is an empty history."""
    if state_dir is None:
        return {}
    raw = read_json_or_default(state_dir / STATE_SOURCE_ROW_HISTORY, {})
    sources = raw.get("sources") if isinstance(raw, dict) else None
    if not isinstance(sources, dict):
        return {}
    history: RowHistory = {}
    for name, entries in sources.items():
        if not isinstance(name, str) or not isinstance(entries, list):
            continue
        kept = [
            {"run_date": str(entry["run_date"]), "valid_rows": entry["valid_rows"]}
            for entry in entries
            if isinstance(entry, dict)
            and isinstance(entry.get("run_date"), str)
            and _count(entry.get("valid_rows")) is not None
        ]
        if kept:
            history[name] = kept
    return history


def incremental_source_names(sources: Iterable[object]) -> frozenset[str]:
    """Names of configured sources that report only items they have not shown before."""
    return frozenset(
        str(source.get("name", "")).strip()
        for source in sources
        if isinstance(source, Mapping) and str(source.get("seen_guids_file", "")).strip()
    )


def row_count_departures(
    source_runs: Iterable[object],
    history: Mapping[str, list[dict[str, Any]]],
    run_date: str,
    *,
    incremental_sources: frozenset[str] = frozenset(),
) -> list[RowCountDeparture]:
    """Compare each fresh collection with that source's earlier runs.

    A partial list (Issue #839) is named whether fresh or reused.

    Entries dated *run_date* are not earlier runs: a second run on the same day
    is compared with the days before it, not with the first run of the day.
    """
    departures: list[RowCountDeparture] = []
    for run in source_runs:
        if not isinstance(run, Mapping):
            continue
        # Issue #839: a list kept without its failing rows is always named,
        # whatever the source's history says, and also when a later run
        # reuses it: the editor still reads that partial list.
        partial = _partial_list_counts(run)
        if partial is not None:
            departures.append(
                RowCountDeparture(partial[0], partial[1], partial[2], "partial_list", f"{partial[2]} extracted")
            )
            continue
        counts = _fresh_counts(run)
        if counts is None:
            continue
        name, valid, extracted = counts
        if name in incremental_sources:
            continue
        if extracted >= MIN_EXTRACTED_FOR_DROP_RULE and valid * 2 < extracted:
            departures.append(RowCountDeparture(name, valid, extracted, "dropped_rows", f"{extracted} extracted"))
            continue
        earlier = [int(entry["valid_rows"]) for entry in history.get(name, []) if entry["run_date"] < run_date][
            -HISTORY_LENGTH:
        ]
        if len(earlier) < MIN_HISTORY:
            continue
        if len(set(earlier)) == 1:
            usual = earlier[0]
            if valid != usual:
                departures.append(
                    RowCountDeparture(name, valid, extracted, "fixed_list", f"usually exactly {usual}", float(usual))
                )
            continue
        middle = median(earlier)
        if middle >= MIN_USUAL_FOR_VARYING_RULE and (valid * 2 < middle or valid > middle * 2):
            departures.append(
                RowCountDeparture(
                    name,
                    valid,
                    extracted,
                    "varying_list",
                    f"usually about {middle:g} (recent {min(earlier)}–{max(earlier)})",
                    float(middle),
                )
            )
    return departures


def updated_row_history(
    history: Mapping[str, list[dict[str, Any]]],
    source_runs: Iterable[object],
    run_date: str,
    *,
    incremental_sources: frozenset[str] = frozenset(),
) -> RowHistory:
    """Add this run's fresh collections, one entry per source and day, in date order.

    Up to ``HISTORY_LENGTH`` earlier days are kept beside the run's own day. A
    second run on the same day replaces that day's entry and still compares
    with the same earlier days. Only fresh collections are kept: a reused
    artifact is an earlier run's count.
    """
    updated: RowHistory = {name: [dict(entry) for entry in entries] for name, entries in history.items()}
    for run in source_runs:
        if not isinstance(run, Mapping):
            continue
        counts = _fresh_counts(run)
        if counts is None:
            continue
        name, valid, _extracted = counts
        # A list kept without its failing rows is not the list's usual size.
        if name in incremental_sources or _partial_list_counts(run) is not None:
            continue
        # Keep the full window of earlier days beside today's entry, so a
        # second run on the same day compares with the same earlier runs
        # as the first one did.
        earlier = sorted(
            (entry for entry in updated.get(name, []) if entry["run_date"] < run_date),
            key=lambda entry: entry["run_date"],
        )[-HISTORY_LENGTH:]
        later = [entry for entry in updated.get(name, []) if entry["run_date"] > run_date]
        updated[name] = [*earlier, {"run_date": run_date, "valid_rows": valid}, *later]
    return updated


def write_row_history(state_dir: Path | None, history: Mapping[str, list[dict[str, Any]]]) -> None:
    """Retain the history, when state exists."""
    if state_dir is None:
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        state_dir / STATE_SOURCE_ROW_HISTORY,
        {"version": 1, "sources": {name: list(history[name]) for name in sorted(history)}},
    )


def recorded_departures(records: object) -> list[RowCountDeparture]:
    """The departures a run recorded in its run summary, malformed records skipped."""
    if not isinstance(records, list):
        return []
    rebuilt = [RowCountDeparture.from_dict(record) for record in records if isinstance(record, Mapping)]
    return [departure for departure in rebuilt if departure is not None]


def recorded_departure_warnings(records: object) -> list[str]:
    """Warnings for the departures a run recorded in its run summary."""
    return [departure.warning() for departure in recorded_departures(records)]


def departure_review_lines(records: object) -> list[str]:
    """The review's source-block line naming every recorded departure, or none."""
    departures = recorded_departures(records)
    if not departures:
        return []
    return [
        "Необычное число книг: "
        + "; ".join(f"{departure.source_name} — {departure.editor_detail()}" for departure in departures)
        + "."
    ]
