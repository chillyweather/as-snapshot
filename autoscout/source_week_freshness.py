"""Freshness for a ranking table that states its week in a source note.

A declarative ranking proves its currency from a heading: it declares a
reporting-period pattern, a date format, and a maximum age, and
``ranking_page_identity`` refuses a period outside that window. An undated
ranking proves it by movement (``autoscout.ranking_freshness``).

An older kind of ranking table has neither. Its rows are read from a plain
table and its week is printed once, in a source note under the table, for
example ``Source: Circana BookScan, for the week ended Sep. 19, 2026``. The
extractor copies that week into every mention and grounding confirms the page
really says it. Nothing asks whether the week is recent.

On 2026-10-05 Publishers Weekly still showed the chart for the week ended
Sep. 19, sixteen days old and identical to the one collected on 2026-09-26.
The source reported 25 mentions as ``collected`` and nothing flagged it
(Issue #763).

Such a source declares ``source_week_max_age_days``. The week is then read
from the source note by code and aged against the run's reference date, and a
week older than the limit is refused as not current. The same four rules that
keep the other two contracts honest apply here:

* The week is a page fact. It is read from the visible source note, never
  from an extraction response.
* A declared limit must be enforceable. A note with no readable week, or a
  run with no reference date, is refused instead of passing unaged.
* A week after the reference date is refused too: the page cannot report a
  week that has not ended.
* The limit is a declaration, not a default. A source that does not declare
  it behaves exactly as before.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from typing import Any

SOURCE_WEEK_MAX_AGE_FIELD = "source_week_max_age_days"
DEFAULT_SOURCE_STATEMENT = "Source: Circana BookScan"

# How much visible text after the statement belongs to it. Grounding reads the
# same window (``ranking_mention_is_grounded``), so the week code ages is the
# week a mention is allowed to carry.
SOURCE_STATEMENT_WINDOW = 300

_MONTHS = {
    name: number
    for number, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1
    )
}
# A written English date: "Sep. 19, 2026", "Sept. 19, 2026", "May 2, 2026",
# "September 19, 2026". The month is matched by its first three letters, so
# one house style drifting to another does not make the week unreadable.
_WRITTEN_DATE = re.compile(r"(?<![A-Za-z])([A-Za-z]{3,9})\.?\s+(\d{1,2}),\s*(\d{4})(?!\d)")


def source_week_freshness_declared(source: Mapping[str, Any]) -> bool:
    """Return whether a ranking table declares an aged source-note week."""
    return SOURCE_WEEK_MAX_AGE_FIELD in source


def validate_source_week_freshness_declaration(source: Mapping[str, Any]) -> str:
    """Return an error naming an unusable source-week declaration, or ``""``."""
    if SOURCE_WEEK_MAX_AGE_FIELD not in source:
        return ""
    limit = source[SOURCE_WEEK_MAX_AGE_FIELD]
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        return f"{SOURCE_WEEK_MAX_AGE_FIELD} must be a positive integer"
    if source.get("metadata_ranking_table") is not True:
        return f"{SOURCE_WEEK_MAX_AGE_FIELD} requires metadata_ranking_table true"
    if str(source.get("ranking_row_selector", "")).strip():
        return (
            f"{SOURCE_WEEK_MAX_AGE_FIELD} is for a ranking table read from its source note; "
            "a declarative ranking declares ranking_reporting_period_max_age_days"
        )
    return ""


def lenient_reference_date(value: object) -> date | None:
    """Return a canonical ISO date, or ``None`` when *value* is not one.

    ``None`` is not a pass: :func:`source_week_freshness_error` refuses a
    declared limit that has no reference date to age against.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == text else None


def source_statement_week(visible_text: str, source: Mapping[str, Any]) -> date | None:
    """Return the first written date in the source note, or ``None``.

    The note can carry a second date, the print edition the chart appears in.
    The week comes first, so the first date is the one read.
    """
    statement = str(source.get("ranking_source_statement") or DEFAULT_SOURCE_STATEMENT).strip()
    if not statement:
        return None
    text = " ".join(visible_text.split())
    start = text.casefold().find(" ".join(statement.split()).casefold())
    if start < 0:
        return None
    for match in _WRITTEN_DATE.finditer(text[start : start + SOURCE_STATEMENT_WINDOW]):
        month = _MONTHS.get(match.group(1)[:3].casefold())
        if month is None:
            continue
        try:
            return date(int(match.group(3)), month, int(match.group(2)))
        except ValueError:
            return None
    return None


def source_week_freshness_error(source: Mapping[str, Any], visible_text: str, reference_date: date | None) -> str:
    """Return why the source-note week is not current, or ``""``.

    The message never starts with the shared page-shape prefix; the caller
    adds it, as it does for every other page-shape reason.
    """
    if not source_week_freshness_declared(source) or validate_source_week_freshness_declaration(source):
        return ""
    limit = int(source[SOURCE_WEEK_MAX_AGE_FIELD])
    if reference_date is None:
        return "source week freshness requires a reference date and the capture carries none"
    week = source_statement_week(visible_text, source)
    if week is None:
        return "the source note states no readable week, so the ranking cannot be reported as current"
    age = (reference_date - week).days
    if age < 0:
        return f"source week ended {week.isoformat()} is after the reference date {reference_date.isoformat()}"
    if age > limit:
        return (
            f"source week ended {week.isoformat()} is {age} days old on {reference_date.isoformat()}; "
            f"a ranking older than {limit} days cannot be reported as current"
        )
    return ""
