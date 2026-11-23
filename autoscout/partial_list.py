"""Keep the verified rows of a partially failing extraction (Issue #839).

Several extraction checks used to be all-or-nothing: one row that failed
validation discarded every row of the source for the week. By operator
decision on #751 (option C, 2026-10-16), the rows that pass are kept and the
failing ones dropped with their reasons, as long as at least 80% of the
extracted rows pass. Below that the list fails exactly as before, because
many failures usually mean the page was misread.

Every kept row has passed the same checks it would face on its own; the
threshold only decides whether the failures of other rows sink it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from autoscout.bookmarks_contract import is_bookmarks_source_identity

# kept / extracted >= 4 / 5, compared in integers so 0.8 is never rounded.
MIN_KEPT_NUMERATOR = 4
MIN_KEPT_DENOMINATOR = 5

PARTIAL_LIST_DEBUG_FIELD = "partial_list"

# Reserved artifact keys the validation gate writes when it publishes a
# partial artifact. Codex never writes them; the gate strips any it finds.
GATE_DROPPED_ENTRIES_FIELD = "validation_dropped_entries"
GATE_EXTRACTED_COUNT_FIELD = "validation_extracted_count"
GATE_RESERVED_FIELDS = frozenset({GATE_DROPPED_ENTRIES_FIELD, GATE_EXTRACTED_COUNT_FIELD})


def keeps_partial_list(kept: int, extracted: int) -> bool:
    """Whether *kept* of *extracted* rows are enough to keep the list."""
    if kept <= 0 or extracted <= 0 or kept > extracted:
        return False
    return kept * MIN_KEPT_DENOMINATOR >= extracted * MIN_KEPT_NUMERATOR


def collected_list_refused(kept: int, extracted: int, *, gate_dropped: int, bookmarks_field_dropped: bool) -> bool:
    """Whether a collected list fails the 80% rule once every drop is counted.

    The gate keeps a list at 80% or more, and the collector may drop more of
    it. A BookMarks list with rows missing fields, or a list the gate already
    shortened, then fails as the gate would have refused it. Production
    collection and model qualification both apply this rule.
    """
    if bookmarks_field_dropped:
        return not keeps_partial_list(kept, extracted)
    return bool(gate_dropped) and kept > 0 and not keeps_partial_list(kept, extracted)


def strict_list_source(source: Mapping[str, Any]) -> bool:
    """A source whose list used to fail whole when any row failed."""
    return (
        source.get("audible_explicit_chart") is True
        or source.get("amazon_charts_weekly") is True
        or is_bookmarks_source_identity(source)
    )


def partial_list_marker(source: Mapping[str, Any], *, kept: int, dropped: int, gate_dropped: int) -> dict[str, Any]:
    """The source-run marker for a list kept without its failing rows, or none.

    A list is partial when it would have failed whole before Issue #839: rows
    were refused by the artifact gate, or a strict list dropped any row.
    """
    if not dropped or not (gate_dropped or strict_list_source(source)):
        return {}
    return {PARTIAL_LIST_DEBUG_FIELD: {"kept_rows": kept, "extracted_rows": kept + dropped}}


def gate_dropped_entries(extraction_data: Any) -> list[dict[str, str]]:
    """Rows the validation gate removed from a published partial artifact."""
    if not isinstance(extraction_data, dict):
        return []
    recorded = extraction_data.get(GATE_DROPPED_ENTRIES_FIELD)
    if not isinstance(recorded, list):
        return []
    return [
        {
            "title": str(row.get("title", "")),
            "evidence_url": str(row.get("evidence_url", "")),
            "reason": str(row.get("reason", "")),
        }
        for row in recorded
        if isinstance(row, dict)
    ]


def gate_dropped_ranks(extraction_data: Any) -> list[int | None]:
    """The chart rank each gate-refused row claimed, None where it claimed none."""
    if not isinstance(extraction_data, dict):
        return []
    recorded = extraction_data.get(GATE_DROPPED_ENTRIES_FIELD)
    if not isinstance(recorded, list):
        return []
    ranks: list[int | None] = []
    for row in recorded:
        if not isinstance(row, dict):
            continue
        try:
            ranks.append(int(str(row.get("current_rank", "")).strip()))
        except ValueError:
            ranks.append(None)
    return ranks
