"""Publishers Weekly issue reviews as Source Mentions (Issue #955).

The PW issue pass (#954) leaves one Git-ignored result file of bibliographic
rows parsed from the Nonfiction Reviews of each collected issue. A weekly run
binds that result to its run date, keeps the rows the source declares (no
excluded subsection, not reported by an earlier run) and turns each into one
Source Mention.

No model reads or writes any of it. Every identity and display field is the
parser's, and every mention is checked against exactly one qualifying row
before it leaves the collector. The pass keeps no review text, and the
Evidence Note is written in AutoScout's own words from the structured fields,
so no PW review text reaches Git, state or the review (permission, #935).

The evidence link is the public review permalink, built from the ISBN.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any
from typing import NoReturn

from autoscout.content_description import validate_content_description
from autoscout.description_depth import meets_description_depth
from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.pw_issue_parser import SUBSECTIONS
from autoscout.pw_issue_parser import valid_isbn

FAMILY = "pw-issue-pdf"
SOURCE_URL = "https://www.publishersweekly.com/pw/reviews/"
EVIDENCE_URL_PREFIX = "https://www.publishersweekly.com/"
SIGNAL_TYPE = "media/curated list"
DEFAULT_SIGNAL_STRENGTH = "minor mention"
CANDIDATE_CATEGORY = "adult nonfiction"
# A PW review runs up to about eight months ahead of publication, and rarely a few months after it.
MONTHS_BEFORE_ISSUE = 3
RESULT_SCHEMA_VERSIONS = (1,)
MAX_ISSUE_AGE_DAYS = 21
# The digital edition arrives a few days before its cover date.
MAX_DAYS_BEFORE_COVER_DATE = 7
DECLARATION_FIELDS = ("pw_max_issue_age_days", "pw_excluded_subsections")
LIVE_METHOD = "pw-issue-pdf-live"
REPLAY_METHOD = "pw-issue-pdf-replay"
METHODS = frozenset({LIVE_METHOD, REPLAY_METHOD})
# What a capture artifact carries to the extraction stage, beside its mentions.
COVERAGE_FIELD = "pw_coverage"
COVERAGE_DEBUG_KEYS = frozenset(
    {
        "pw_dropped_rows",
        "pw_newest_issue_date",
        "pw_missing_issues",
        "incomplete_ranking",
        "incomplete_ranking_warning",
        "raw_row_count",
        "expected_raw_row_count",
    }
)
NEW_SEEN_COUNT_KEY = "pw_new_seen_item_count"

# Why a parsed row is not reported. Counted, never named: the counts are safe to log.
DROP_EXCLUDED_SUBSECTION = "excluded_subsection"
DROP_SEEN = "seen_in_earlier_run"

MONTHS = ("Jan.", "Feb.", "Mar.", "Apr.", "May", "June", "July", "Aug.", "Sept.", "Oct.", "Nov.", "Dec.")
_ISSUE_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# Everything a PW mention may carry: the row's identity, the configured signal fields, the generated
# note, and the non-authoritative search helpers the pipeline may add later.
_MENTION_FIELDS = frozenset(
    {
        "title",
        "primary_authors",
        "publisher",
        "publication_year",
        "isbn",
        "source_week",
        "source_url",
        "evidence_url",
        "source_check_date",
        "signal_type",
        "signal_strength",
        "evidence_note",
        "candidate_category",
        "publication_status",
        "content_description",
        "description_provenance",
        "english_title",
        "english_title_type",
        "english_title_provenance",
        "search_translation",
        "author_transliteration_note",
    }
)


class PwIssueCaptureError(ValueError):
    """A named refusal that callers report for PW alone, without repairing evidence."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(f"{reason_code}: {message}")
        self.reason_code = reason_code


CAPTURE_ERROR = PwIssueCaptureError


def _fail(code: str, message: str) -> NoReturn:
    raise PwIssueCaptureError(code, message)


def _max_age(source: Mapping[str, Any]) -> int:
    value = source.get("pw_max_issue_age_days")
    if type(value) is not int or not 1 <= value <= MAX_ISSUE_AGE_DAYS:
        _fail("pw_declaration", f"PW issue reviews require pw_max_issue_age_days between 1 and {MAX_ISSUE_AGE_DAYS}")
    return value


def excluded_subsections(source: Mapping[str, Any]) -> set[str]:
    """The declared subsections whose reviews are left out, such as Food & Cooking."""
    value = source.get("pw_excluded_subsections", [])
    if not isinstance(value, list) or any(item not in SUBSECTIONS for item in value):
        _fail("pw_declaration", f"pw_excluded_subsections may name only: {', '.join(SUBSECTIONS)}")
    return set(value)


def signal_strength(source: Mapping[str, Any]) -> str:
    return str(source.get("expected_signal_strength", "")).strip() or DEFAULT_SIGNAL_STRENGTH


def validate_declaration(source: Mapping[str, Any]) -> None:
    if source.get("url") != SOURCE_URL:
        _fail("pw_declaration", f"PW issue reviews require the source URL {SOURCE_URL}")
    _max_age(source)
    excluded_subsections(source)


def collection_path(config_path: Path, source: Mapping[str, Any]) -> Path:
    """The declared result file of the PW issue pass, resolved beside the config that names it."""
    raw_path = str(source.get("pw_collection_file", "")).strip()
    if not raw_path:
        _fail("pw_declaration", "PW issue reviews require pw_collection_file, the result of the PW issue pass")
    return (config_path.parent / raw_path).resolve()


def read_collection(path: Path) -> dict[str, Any] | None:
    """The pass result, or None when no pass has written it."""
    if not path.exists():
        return None
    try:
        data = read_json(path, default=None)
    except (json.JSONDecodeError, OSError):
        _fail("pw_collection_state", "PW issue result is unreadable")
    if not isinstance(data, dict):
        _fail("pw_collection_state", "PW issue result has an invalid shape")
    return data


def descriptions_path(config_path: Path, source: Mapping[str, Any]) -> Path | None:
    """Optional local-only descriptions prepared from the subscriber PDFs."""
    raw_path = str(source.get("pw_descriptions_file", "")).strip()
    return (config_path.parent / raw_path).resolve() if raw_path else None


def read_descriptions(path: Path | None, collection: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Accept only descriptions agreeing with a collected PW bibliographic row."""
    if path is None or not path.exists() or collection is None:
        return {}
    try:
        payload = read_json(path)
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError
        raw = payload["descriptions"]
        if not isinstance(raw, dict):
            raise ValueError
        rows = {row["isbn13"]: row for row in validate_collection(collection)}
        accepted = {}
        for isbn, item in raw.items():
            if isbn not in rows:
                continue  # backfill may describe issues not yet imported
            row = rows[isbn]
            if (
                not isinstance(item, dict)
                or item.get("isbn13") != isbn
                or any(item.get(key) != row[key] for key in ("title", "primary_authors", "issue_date"))
                or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("review_sha256", "")))
            ):
                raise ValueError
            description, _reason = validate_content_description(item.get("content_description"))
            if description is None or not meets_description_depth(description):
                raise ValueError
            accepted[isbn] = {"content_description": description}
        return accepted
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        _fail("pw_description_state", "PW local description result is invalid")


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("text")
    return " ".join(value.split())


def _checked_row(row: Any) -> dict[str, Any]:
    """The fields of one result row this contract reads."""
    if not isinstance(row, Mapping):
        raise ValueError("row")
    authors = row.get("primary_authors")
    if not isinstance(authors, list) or not authors:
        raise ValueError("authors")
    names = [_text(author) for author in authors]
    title, publisher, isbn = _text(row.get("title")), _text(row.get("publisher")), _text(row.get("isbn13"))
    subsection, month, issue_date = (
        _text(row.get("subsection")),
        _text(row.get("publication_month")),
        _text(row.get("issue_date")),
    )
    starred = row.get("starred")
    if (
        not title
        or not publisher
        or not all(names)
        or not valid_isbn(isbn)
        or (subsection and subsection not in SUBSECTIONS)
        or month not in MONTHS
        or not _ISSUE_DATE.fullmatch(issue_date)
        or type(starred) is not bool
    ):
        raise ValueError("row")
    date.fromisoformat(issue_date)
    return {
        "title": title,
        "primary_authors": names,
        "publisher": publisher,
        "isbn13": isbn,
        "subsection": subsection,
        "agent": _text(row.get("agent")),
        "agency": _text(row.get("agency")),
        "publication_month": month,
        "issue_date": issue_date,
        "starred": starred,
    }


def validate_collection(collection: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Revalidate a pass result and return its rows, trimmed to the fields this contract reads."""
    try:
        if collection.get("schema_version") not in RESULT_SCHEMA_VERSIONS:
            raise ValueError("schema")
        if not isinstance(collection["rows"], list) or not isinstance(collection["issues"], list):
            raise ValueError("shape")
        return [_checked_row(row) for row in collection["rows"]]
    except (KeyError, TypeError, ValueError, AttributeError):
        _fail("pw_collection_state", "PW issue result has an invalid shape")


def newest_issue_date(collection: Mapping[str, Any]) -> date:
    """The cover date of the newest collected issue."""
    try:
        dates = [date.fromisoformat(str(issue["issue_date"])) for issue in collection["issues"]]
    except (KeyError, TypeError, ValueError):
        _fail("pw_collection_state", "PW issue result has an invalid issue date")
    if not dates:
        _fail("pw_collection_missing", "PW issue result holds no collected issue; run ./scripts/collect-pw-issue")
    return max(dates)


def seen_items_path(config_path: Path, source: Mapping[str, Any]) -> Path | None:
    raw_path = str(source.get("seen_guids_file", "")).strip()
    return (config_path.parent / raw_path).resolve() if raw_path else None


def read_seen_items(path: Path | None) -> dict[str, str]:
    """The seen-review index: ISBN-13 to the run date that first reported it."""
    if path is None:
        return {}
    try:
        data = read_json(path, default={})
    except json.JSONDecodeError:
        _fail("pw_seen_state", "PW seen-review state is malformed JSON")
    if not isinstance(data, dict) or not isinstance(data.get("seen_guids", []), list):
        _fail("pw_seen_state", "PW seen-review state has an invalid shape")
    seen: dict[str, str] = {}
    for entry in data.get("seen_guids", []):
        try:
            key, first = entry["guid"], entry["first_seen_date"]
            if not valid_isbn(key) or date.fromisoformat(first).isoformat() != first:
                raise ValueError("entry")
        except (KeyError, TypeError, ValueError):
            _fail("pw_seen_state", "PW seen-review state has an invalid entry")
        seen[key] = first
    return seen


def record_reported(config_path: Path, source: Mapping[str, Any], mentions: list[Any], run_date: str) -> int:
    """Record the reviews a weekly run reports, once its pipeline consumes their recorded mentions.

    The capture stage writes nothing, so a source health check that runs the
    same capture never marks a review as reported. Returns how many were new.
    """
    path = seen_items_path(config_path, source)
    if path is None:
        return 0
    seen = read_seen_items(path)
    keys = [str(entry.get("isbn", "")) for entry in mentions if isinstance(entry, Mapping)]
    new = [key for key in dict.fromkeys(keys) if valid_isbn(key) and key not in seen]
    if not new:
        return 0
    for key in new:
        seen[key] = run_date
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(
        path,
        {
            "source": str(source.get("name", "")),
            "updated_at": run_date,
            "seen_guids": [{"guid": key, "first_seen_date": first} for key, first in seen.items()],
        },
    )
    return len(new)


def capture(
    source: Mapping[str, Any],
    reference: date,
    collection: Mapping[str, Any] | None,
    seen: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Bind the PW issue pass result to one run. This reads no mail and requests nothing.

    A missing result, and one whose newest issue is older than the declared
    age, are named refusals, so the weekly run reports PW and carries on.
    """
    validate_declaration(source)
    if collection is None:
        _fail("pw_collection_missing", "PW issue result is missing; run ./scripts/collect-pw-issue")
    rows = validate_collection(collection)
    newest = newest_issue_date(collection)
    max_age = _max_age(source)
    if not -MAX_DAYS_BEFORE_COVER_DATE <= (reference - newest).days <= max_age:
        _fail("pw_stale_collection", f"PW newest collected issue is outside the {max_age}-day age limit")
    keys = {row["isbn13"] for row in rows}
    return {
        "version": 1,
        "source_url": SOURCE_URL,
        "reference_date": reference.isoformat(),
        "newest_issue_date": newest.isoformat(),
        **_issue_coverage(collection, reference, max_age),
        "rows": rows,
        # A review reported by an earlier run is seen. One first reported today is shown again, so a
        # second run on the same date does not lose what the first one captured.
        "seen_items": sorted(
            key for key, first in (seen or {}).items() if key in keys and first < reference.isoformat()
        ),
    }


def _issue_coverage(collection: Mapping[str, Any], reference: date, max_age: int) -> dict[str, list[str]]:
    """The issues inside the age window the pass collected, and those it could not.

    An issue that failed, permanently or for a later retry, leaves its reviews
    out of this run, so the run names it as a coverage caveat instead of
    reporting a complete source.
    """

    def in_window(day: str) -> bool:
        try:
            return -MAX_DAYS_BEFORE_COVER_DATE <= (reference - date.fromisoformat(day)).days <= max_age
        except ValueError:
            return False

    collected = {str(issue["issue_date"]) for issue in collection["issues"]}
    failed: set[str] = set()
    for entry in collection.get("skipped_issues", []) or []:
        if isinstance(entry, Mapping) and isinstance(entry.get("issue_date"), str):
            failed.add(entry["issue_date"])
    # Issues waiting for a retry, or found but never attempted, are missing too.
    for key in ("retry_issues", "untried_issues"):
        for day in collection.get(key, []) or []:
            if isinstance(day, str):
                failed.add(day)
    return {
        "window_issues": sorted(day for day in collected if in_window(day)),
        "missing_issues": sorted(day for day in failed - collected if in_window(day)),
    }


def _drop_reason(row: Mapping[str, Any], excluded: set[str], seen: set[str]) -> str:
    if row["subsection"] in excluded:
        return DROP_EXCLUDED_SUBSECTION
    if row["isbn13"] in seen:
        return DROP_SEEN
    return ""


def dropped_row_counts(bundle: Mapping[str, Any], source: Mapping[str, Any]) -> dict[str, int]:
    """How many rows of the bound result are not reported, by reason."""
    excluded, seen = excluded_subsections(source), set(bundle["seen_items"])
    counts: dict[str, int] = {}
    for row in bundle["rows"]:
        reason = _drop_reason(row, excluded, seen)
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def qualifying_rows(bundle: Mapping[str, Any], source: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The reviews a run reports: not in an excluded subsection, not reported before."""
    excluded, seen = excluded_subsections(source), set(bundle["seen_items"])
    return [row for row in bundle["rows"] if not _drop_reason(row, excluded, seen)]


def publication_year(row: Mapping[str, Any]) -> int:
    """The year of the printed publication month, placed in the twelve months around the issue.

    The window opens three months before the issue: a later-month review of a
    December book in a January issue is the previous year's, an earlier month
    further back is next year's.
    """
    issue = date.fromisoformat(row["issue_date"])
    month = MONTHS.index(row["publication_month"]) + 1
    offset = (month - issue.month) % 12
    if offset >= 12 - MONTHS_BEFORE_ISSUE:
        offset -= 12
    return issue.year + (issue.month - 1 + offset) // 12


def publication_status(row: Mapping[str, Any], run_date: str) -> str:
    """'published' once the publication month has begun by the run date, else 'forthcoming'."""
    run = date.fromisoformat(run_date)
    month = MONTHS.index(row["publication_month"]) + 1
    return "published" if (publication_year(row), month) <= (run.year, run.month) else "forthcoming"


def evidence_note(row: Mapping[str, Any]) -> str:
    """AutoScout's own sentence about the review, from its structured fields only."""
    kind = "★ starred review" if row["starred"] else "review"
    where = f"Nonfiction, {row['subsection']}" if row["subsection"] else "Nonfiction"
    parts = [f"Publishers Weekly {kind} ({where}), issue of {row['issue_date']}"]
    parts.append(f"publication {row['publication_month'].rstrip('.')} {publication_year(row)}")
    if row["agent"]:
        parts.append(f"agent {row['agent']}" + (f" at {row['agency']}" if row["agency"] else ""))
    return "; ".join(parts) + "."


def mention(
    row: Mapping[str, Any], source: Mapping[str, Any], run_date: str, description: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    result = {
        "title": row["title"],
        "primary_authors": list(row["primary_authors"]),
        "publisher": row["publisher"],
        "publication_year": publication_year(row),
        "isbn": row["isbn13"],
        "source_week": row["issue_date"],
        "source_url": SOURCE_URL,
        "evidence_url": EVIDENCE_URL_PREFIX + row["isbn13"],
        "source_check_date": run_date,
        "signal_type": SIGNAL_TYPE,
        "signal_strength": signal_strength(source),
        "evidence_note": evidence_note(row),
        "candidate_category": CANDIDATE_CATEGORY,
        "publication_status": publication_status(row, run_date),
    }
    if description is not None:
        result["content_description"] = description["content_description"]
        result["description_provenance"] = "grounded"
    return result


def mention_violation(
    entry: Mapping[str, Any],
    bundle: Mapping[str, Any],
    source: Mapping[str, Any],
    descriptions: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    """Refuse a mention that does not agree with exactly one qualifying captured row.

    Every identity field is the parser's. Nothing may supply or correct a
    title, an author, a publisher, the ISBN, the evidence link or the note.
    """
    prohibited = sorted(set(entry) - _MENTION_FIELDS)
    if prohibited:
        return f"PW mention contains fields the review rows do not support: {', '.join(prohibited)}"
    matches = 0
    for row in qualifying_rows(bundle, source):
        expected = mention(
            row, source, str(entry.get("source_check_date", "")), (descriptions or {}).get(row["isbn13"])
        )
        if (
            all(entry.get(field) == value for field, value in expected.items())
            and ("content_description" in entry) == ("content_description" in expected)
            and ("description_provenance" in entry) == ("description_provenance" in expected)
        ):
            matches += 1
    if matches == 1:
        return ""
    if matches > 1:
        return "PW mention agrees with more than one qualifying captured review row"
    return "PW mention does not agree with a qualifying captured review row"
