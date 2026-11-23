"""Report how many useful books each source delivers (issue #1055).

Some sources deliver many books and almost none the editor wants, and nothing
measured it. This module counts, per source, what the editor decided about the
books it reported, and marks the sources that meet the proposed retirement rule
and the ones close to it. It is a report only: it reads Candidate History
through the State Store and the configured portfolio, writes nothing, and
retires nothing. The operator and the editor decide.

Counting rules:

* A book belongs to every source that reported it: the sources named in its
  ``source_evidence`` and in ``earlier_source_names``, which keeps the sources
  of earlier runs. One book therefore counts once for each of its sources.
* A book is *decided* when it carries one of the editor's statuses. Undecided
  books say nothing about a source and are left out of every yield count.
* A book is *useful* when the editor's status is ``interested``,
  ``maybe_watch`` or ``needs_deeper_analysis``. ``already_known`` is reported
  but is not useful: it shows a source that picks to her taste and arrives
  late, which is a different problem.
* A source's useful share is useful divided by decided.
* "Only here" counts every book the source reported, decided or not, whose
  only source is that source. First and last book are the earliest and latest
  ``first_seen_date`` among those books.

The rule's thresholds are the named values below and nowhere else. They wait
for the editor's approval.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

from autoscout.annual_edition import is_annual_source
from autoscout.config import resolve_weekly_portfolio_or_raise
from autoscout.editor_decisions import EDITOR_STATUSES
from autoscout.json_io import read_json_or_default
from autoscout.review_hiding import EARLIER_SOURCE_NAMES_FIELD
from autoscout.review_hiding import evidence_source_names
from autoscout.state_store import Collection
from autoscout.state_store import open_state_store

# ── The proposed retirement rule (#1055). Not yet approved by the editor. ──
# A source meets the rule when the editor decided on at least
# MIN_DECIDED_BOOKS of its books and at most MAX_USEFUL_PERCENT of them were useful.
MIN_DECIDED_BOOKS = 50
MAX_USEFUL_PERCENT = 2

# A source that does not meet the rule is "close" when it has at least
# NEAR_MIN_DECIDED_BOOKS decided books and at most NEAR_MAX_USEFUL_PERCENT of
# them useful. This only widens what the report points out; it retires nothing.
NEAR_MIN_DECIDED_BOOKS = 40
NEAR_MAX_USEFUL_PERCENT = 5

USEFUL_STATUSES = frozenset({"interested", "maybe_watch", "needs_deeper_analysis"})

VERDICT_MEETS_RULE = "meets rule"
VERDICT_CLOSE = "close"
VERDICT_NONE = ""
# Yearly prize lists never reach MIN_DECIDED_BOOKS. The issue proposes a test of
# their own, no useful book across two editions, but each edition is a separate
# configured source with no recorded link to the other editions of the same
# prize, so the report cannot apply it reliably and gives them no verdict.

SECTION_ACTIVE = "active"
SECTION_ANNUAL = "annual"
SECTION_RETIRED = "retired"
SECTION_UNLISTED = "unlisted"

SECTION_TITLES = {
    SECTION_ACTIVE: "Active sources",
    SECTION_ANNUAL: "Yearly prize lists (no verdict)",
    SECTION_RETIRED: "Retired sources (no verdict)",
    SECTION_UNLISTED: "Sources in Candidate History but not in the portfolio (no verdict)",
}


class SourceYieldError(ValueError):
    """The report's input is unusable."""


@dataclass
class SourceYield:
    """What the editor decided about the books one source reported."""

    name: str
    section: str = SECTION_ACTIVE
    books: int = 0
    interested: int = 0
    maybe_watch: int = 0
    needs_deeper_analysis: int = 0
    already_known: int = 0
    rejected: int = 0
    only_here: int = 0
    first_book: str = ""
    last_book: str = ""
    verdict: str = field(default=VERDICT_NONE)

    @property
    def useful(self) -> int:
        return self.interested + self.maybe_watch + self.needs_deeper_analysis

    @property
    def decided(self) -> int:
        return self.useful + self.already_known + self.rejected

    @property
    def useful_percent(self) -> float | None:
        return None if not self.decided else 100 * self.useful / self.decided


@dataclass(frozen=True)
class SourceYieldReport:
    sources: tuple[SourceYield, ...]
    decided: int
    useful: int
    books_without_source: int

    def section(self, name: str) -> list[SourceYield]:
        return [source for source in self.sources if source.section == name]


def book_source_names(record: Mapping[str, Any]) -> set[str]:
    """Every source that reported *record*: its current evidence and the sources of earlier runs."""
    earlier = record.get(EARLIER_SOURCE_NAMES_FIELD)
    names = {str(name).strip() for name in (earlier if isinstance(earlier, list) else [])}
    names |= evidence_source_names(record.get("source_evidence"))
    names.discard("")
    return names


def verdict_for(decided: int, useful: int) -> str:
    """Say whether a source with these counts meets the rule, is close to it, or neither."""
    if decided >= MIN_DECIDED_BOOKS and useful * 100 <= MAX_USEFUL_PERCENT * decided:
        return VERDICT_MEETS_RULE
    if decided >= NEAR_MIN_DECIDED_BOOKS and useful * 100 <= NEAR_MAX_USEFUL_PERCENT * decided:
        return VERDICT_CLOSE
    return VERDICT_NONE


def _section_of(source: Mapping[str, Any]) -> str:
    if source.get("active") is False or str(source.get("lifecycle_status", "")).strip() == "retired":
        return SECTION_RETIRED
    return SECTION_ANNUAL if is_annual_source(source) else SECTION_ACTIVE


def _sort_key(source: SourceYield) -> tuple[Any, ...]:
    share = source.useful_percent
    # Sources with no decided book have nothing to rank by and go last.
    return (share is None, share if share is not None else 0.0, -source.decided, source.name)


def build_source_yield_report(
    history: Iterable[Mapping[str, Any]],
    configured_sources: Iterable[Mapping[str, Any]],
) -> SourceYieldReport:
    """Count, per source, the editor's decisions on the books it reported."""
    by_name: dict[str, SourceYield] = {}
    for source in configured_sources:
        name = str(source.get("name", "")).strip()
        if name:
            by_name[name] = SourceYield(name=name, section=_section_of(source))

    decided_total = useful_total = books_without_source = 0
    for record in history:
        if not isinstance(record, Mapping):
            continue
        status = str(record.get("editor_status") or "").strip()
        decided = status in EDITOR_STATUSES
        names = book_source_names(record)
        if decided:
            decided_total += 1
            useful_total += status in USEFUL_STATUSES
        if not names:
            books_without_source += 1
            continue
        first_seen = str(record.get("first_seen_date") or "").strip()
        for name in names:
            entry = by_name.setdefault(name, SourceYield(name=name, section=SECTION_UNLISTED))
            entry.books += 1
            if len(names) == 1:
                entry.only_here += 1
            if first_seen:
                entry.first_book = min(entry.first_book or first_seen, first_seen)
                entry.last_book = max(entry.last_book, first_seen)
            if decided:
                _count_status(entry, status)

    for entry in by_name.values():
        entry.verdict = _verdict_for_section(entry)
    return SourceYieldReport(
        sources=tuple(sorted(by_name.values(), key=_sort_key)),
        decided=decided_total,
        useful=useful_total,
        books_without_source=books_without_source,
    )


def _count_status(entry: SourceYield, status: str) -> None:
    if status == "reject":
        entry.rejected += 1
    elif status == "already_known":
        entry.already_known += 1
    elif status == "interested":
        entry.interested += 1
    elif status == "maybe_watch":
        entry.maybe_watch += 1
    elif status == "needs_deeper_analysis":
        entry.needs_deeper_analysis += 1


def _verdict_for_section(entry: SourceYield) -> str:
    # Only an active, non-annual source is judged; the others are shown without a verdict.
    return verdict_for(entry.decided, entry.useful) if entry.section == SECTION_ACTIVE else VERDICT_NONE


_COLUMNS = (
    ("Source", "<"),
    ("Decided", ">"),
    ("Useful", ">"),
    ("Interested", ">"),
    ("Already known", ">"),
    ("Rejected", ">"),
    ("Useful share", ">"),
    ("Only here", ">"),
    ("First book", "<"),
    ("Last book", "<"),
    ("Verdict", "<"),
)


def _row(source: SourceYield, show_verdict: bool) -> list[str]:
    share = source.useful_percent
    return [
        source.name,
        str(source.decided),
        str(source.useful),
        str(source.interested),
        str(source.already_known),
        str(source.rejected),
        "-" if share is None else f"{share:.1f}%",
        str(source.only_here),
        source.first_book or "-",
        source.last_book or "-",
        source.verdict if show_verdict else "",
    ]


def _table(sources: list[SourceYield], show_verdict: bool) -> list[str]:
    columns = _COLUMNS if show_verdict else _COLUMNS[:-1]
    rows = [[title for title, _align in columns]]
    rows += [_row(source, show_verdict)[: len(columns)] for source in sources]
    widths = [max(len(row[index]) for row in rows) for index in range(len(columns))]
    lines = []
    for number, row in enumerate(rows):
        cells = [f"{cell:{align}{width}}" for cell, (_title, align), width in zip(row, columns, widths, strict=True)]
        lines.append("  ".join(cells).rstrip())
        if number == 0:
            lines.append("  ".join("-" * width for width in widths))
    return lines


def render_source_yield_report(report: SourceYieldReport) -> str:
    """Render the report as plain text for the operator."""
    share = f"{100 * report.useful / report.decided:.1f}%" if report.decided else "-"
    lines = [
        "Source yield report (read-only; nothing is retired by this report)",
        "",
        f"All sources together: {report.decided} decided books, {report.useful} useful, {share}.",
        "Counts are per source; a book reported by several sources counts for each of them.",
        "Useful = interested, maybe/watch or needs deeper analysis. Already known is not useful.",
        f"Rule (proposed, not approved): at least {MIN_DECIDED_BOOKS} decided books and at most "
        f"{MAX_USEFUL_PERCENT}% useful. Close: at least {NEAR_MIN_DECIDED_BOOKS} decided "
        f"and at most {NEAR_MAX_USEFUL_PERCENT}% useful.",
        "Only here = books reported by no other source. First and last book = first-seen dates.",
    ]
    for section in (SECTION_ACTIVE, SECTION_ANNUAL, SECTION_RETIRED, SECTION_UNLISTED):
        sources = report.section(section)
        if not sources:
            continue
        lines += ["", f"{SECTION_TITLES[section]}: {len(sources)}"]
        if section == SECTION_ANNUAL:
            lines.append(
                "A yearly prize list is judged by a different test (no useful book across two editions), "
                "but the editions of one prize are separate sources with no recorded link, "
                "so this report cannot apply it. The counts are shown as they are."
            )
        lines += _table(sources, show_verdict=section == SECTION_ACTIVE)
    if report.books_without_source:
        lines += ["", f"{report.books_without_source} books name no source and appear in no row."]
    return "\n".join(lines) + "\n"


def source_yield_report_command(state_dir: Path, config_path: Path) -> int:
    """Print the report. Exit 0 on a report, 2 on an input error. Writes nothing."""
    try:
        if not state_dir.is_dir():
            raise SourceYieldError(f"state directory not found: {state_dir}")
        if not config_path.is_file():
            raise SourceYieldError(f"configuration file not found: {config_path}")
        raw = read_json_or_default(config_path, None)
        if not isinstance(raw, dict):
            raise SourceYieldError(f"configuration is not a JSON object: {config_path}")
        sources = resolve_weekly_portfolio_or_raise(raw, config_path).get("sources", [])
        if not isinstance(sources, list):
            raise SourceYieldError(f"configuration 'sources' must be a list: {config_path}")
        history = open_state_store(state_dir).read(Collection.CANDIDATE_HISTORY)
        if not isinstance(history, list):
            raise SourceYieldError("Candidate History must be a list")
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        render_source_yield_report(build_source_yield_report(history, [s for s in sources if isinstance(s, Mapping)])),
        end="",
    )
    return 0
