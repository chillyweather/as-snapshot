"""Publishers Marketplace deals as rights/deal Source Mentions (Issue #945).

The PM deals pass (#943) leaves one Git-ignored result file of parsed deal
rows. A weekly run binds that result to its run date, keeps the deals the
source declares (adult Non-fiction categories, not reported by an earlier
run) and turns each into one Source Mention with signal type ``rights/deal``.

No model reads or writes any of it. Every identity and display field is the
parser's, and every mention is checked against exactly one qualifying row
before it leaves the collector. The deal's logline is never read here: the
Evidence Note is written in AutoScout's own words from the structured fields,
so no PM deal text reaches Git, state or the review.

The email carries no per-deal link. The evidence link is the agent's PM
dealmaker profile, or the agency's when no agent has one, or the publisher's
when the deal names neither; a deal with none of them is left out by name.

Each mention carries the deal as a rights lead (#948): size band, deal type,
expected publication, territory, territories already sold, contact addresses,
acquiring editors, PM category and the author's earlier titles, all from the
parser's structured fields. An optional declared minimum size band, off by
default, leaves out smaller deals and deals with no disclosed size by name.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import NoReturn

from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.pm_deal_email import DEAL_SIZE_BANDS
from autoscout.pm_deal_email import DEAL_TYPES
from autoscout.pm_deal_email import PUBLICATION_SEASONS

FAMILY = "pm-deal-email"
SOURCE_URL = "https://www.publishersmarketplace.com/deals/"
SIGNAL_TYPE = "rights/deal"
DEFAULT_SIGNAL_STRENGTH = "rights/deal mention"
CANDIDATE_CATEGORY = "adult nonfiction"
# A deal is one to three years from publication: its date is expected, not known.
PUBLICATION_STATUS = "forthcoming"
NONFICTION_SECTION = "Non-fiction"
INTERNATIONAL_SECTION = "International rights"
# Version 1 rows predate the rights-lead fields (#948): they stay readable, with those fields empty.
RESULT_SCHEMA_VERSIONS = (1, 2)
MAX_COLLECTION_AGE_DAYS = 14
DECLARATION_FIELDS = ("pm_max_collection_age_days", "pm_category_labels", "pm_min_deal_size_band")
LIVE_METHOD = "pm-deal-email-live"
REPLAY_METHOD = "pm-deal-email-replay"
METHODS = frozenset({LIVE_METHOD, REPLAY_METHOD})
# What a capture artifact carries to the extraction stage, beside its mentions (#945).
COVERAGE_FIELD = "pm_coverage"
COVERAGE_DEBUG_KEYS = frozenset(
    {
        "pm_dropped_rows",
        "pm_result_status",
        "pm_stop_reason",
        "incomplete_ranking",
        "incomplete_ranking_warning",
        "raw_row_count",
        "expected_raw_row_count",
    }
)
NEW_SEEN_COUNT_KEY = "pm_new_seen_item_count"

# Why a parsed deal is not reported. Counted, never named: the counts are safe to log.
DROP_NOT_NONFICTION = "not_nonfiction_section"
DROP_INTERNATIONAL = "international_rights"
DROP_UNDECLARED_CATEGORY = "undeclared_category"
DROP_NO_EVIDENCE_URL = "no_profile_link"
DROP_BELOW_MIN_SIZE = "below_minimum_deal_size"
DROP_SIZE_UNDISCLOSED = "deal_size_not_disclosed"
DROP_SEEN = "seen_in_earlier_run"

_PROFILE_URL = re.compile(r"https://www\.publishersmarketplace\.com/dealmakers/detail\.cgi\?id=[0-9]+")
_DEAL_KEY = re.compile(r"[0-9a-f]{24}")
# Everything a PM mention may carry: the row's identity, the configured signal fields, the generated
# note, and the non-authoritative search helpers the pipeline may add later.
_MENTION_FIELDS = frozenset(
    {
        "title",
        "primary_authors",
        "publisher",
        "source_week",
        "source_url",
        "evidence_url",
        "source_check_date",
        "signal_type",
        "signal_strength",
        "evidence_note",
        "candidate_category",
        "publication_status",
        "rights_deal",
        "english_title",
        "english_title_type",
        "english_title_provenance",
        "search_translation",
        "author_transliteration_note",
    }
)


class PmDealCaptureError(ValueError):
    """A named refusal that callers report for PM alone, without repairing evidence."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(f"{reason_code}: {message}")
        self.reason_code = reason_code


CAPTURE_ERROR = PmDealCaptureError


def _fail(code: str, message: str) -> NoReturn:
    raise PmDealCaptureError(code, message)


def _max_age(source: Mapping[str, Any]) -> int:
    value = source.get("pm_max_collection_age_days")
    if type(value) is not int or not 1 <= value <= MAX_COLLECTION_AGE_DAYS:
        _fail(
            "pm_declaration",
            f"PM deals require pm_max_collection_age_days between 1 and {MAX_COLLECTION_AGE_DAYS}",
        )
    return value


def category_labels(source: Mapping[str, Any]) -> set[str]:
    return {" ".join(label.split()).casefold() for label in source.get("pm_category_labels", [])}


def minimum_size_band(source: Mapping[str, Any]) -> str:
    """The declared minimum deal size band, or '' when the switch is off (the default)."""
    value = source.get("pm_min_deal_size_band")
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or " ".join(value.split()).casefold() not in DEAL_SIZE_BANDS:
        _fail("pm_declaration", f"pm_min_deal_size_band must be one of: {', '.join(DEAL_SIZE_BANDS)}")
    return " ".join(value.split()).casefold()


def signal_strength(source: Mapping[str, Any]) -> str:
    return str(source.get("expected_signal_strength", "")).strip() or DEFAULT_SIGNAL_STRENGTH


def validate_declaration(source: Mapping[str, Any]) -> None:
    if source.get("url") != SOURCE_URL:
        _fail("pm_declaration", f"PM deals require the source URL {SOURCE_URL}")
    _max_age(source)
    minimum_size_band(source)
    labels = source.get("pm_category_labels")
    if not isinstance(labels, list) or not labels or any(not isinstance(x, str) or not x.strip() for x in labels):
        _fail("pm_declaration", "PM deals require a nonempty list of accepted Non-fiction category labels")


def collection_path(config_path: Path, source: Mapping[str, Any]) -> Path:
    """The declared result file of the PM deals pass, resolved beside the config that names it."""
    raw_path = str(source.get("pm_collection_file", "")).strip()
    if not raw_path:
        _fail("pm_declaration", "PM deals require pm_collection_file, the result of the PM deals pass")
    return (config_path.parent / raw_path).resolve()


def read_collection(path: Path) -> dict[str, Any] | None:
    """The last pass's result, or None when no pass has written it."""
    if not path.exists():
        return None
    try:
        data = read_json(path, default=None)
    except (json.JSONDecodeError, OSError):
        _fail("pm_collection_state", "PM deals result is unreadable")
    if not isinstance(data, dict):
        _fail("pm_collection_state", "PM deals result has an invalid shape")
    return data


def _dealmaker(value: Any) -> tuple[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("dealmaker")
    name, url = value.get("name"), value.get("profile_url", "")
    if not isinstance(name, str) or not isinstance(url, str):
        raise ValueError("dealmaker")
    return " ".join(name.split()), url.strip()


def _texts(value: Any) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("texts")
    return [" ".join(item.split()) for item in value if item.strip()]


def _publication(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("publication")
    year, season, month = value.get("year"), value.get("season", ""), value.get("month", 0)
    if (
        type(year) is not int
        or not isinstance(season, str)
        or (season and season not in PUBLICATION_SEASONS)
        or type(month) is not int
        or not 0 <= month <= 12
        or (season and month)
    ):
        raise ValueError("publication")
    return {"year": year, "season": season, "month": month}


def _rights_lead(row: Mapping[str, Any]) -> dict[str, Any]:
    """The rights-lead fields of a row. A row collected before #948 has none, so they are empty;
    such a row can sit in a version 2 result until the pass reads its email again."""
    if "deal_size" not in row:
        return {
            "deal_size": "",
            "deal_types": [],
            "book_count": 0,
            "publication": None,
            "rights_also": [],
            "contacts": [],
            "earlier_books": [],
        }
    size, types, book_count = row["deal_size"], _texts(row["deal_types"]), row["book_count"]
    if size not in ("", *DEAL_SIZE_BANDS) or any(kind not in DEAL_TYPES for kind in types):
        raise ValueError("deal terms")
    if type(book_count) is not int or book_count < 0:
        raise ValueError("book count")
    return {
        "deal_size": size,
        "deal_types": types,
        "book_count": book_count,
        "publication": _publication(row["publication"]),
        "rights_also": _texts(row["rights_also"]),
        "contacts": _texts(row["contacts"]),
        "earlier_books": _texts(row["earlier_books"]),
    }


def _checked_row(row: Any) -> dict[str, Any]:
    """The fields of one result row this contract reads. The logline is never copied."""
    if not isinstance(row, Mapping):
        raise ValueError("row")
    texts = {field: row.get(field) for field in ("deal_key", "section", "country", "category", "title", "territory")}
    if any(not isinstance(value, str) for value in texts.values()) or not _DEAL_KEY.fullmatch(str(texts["deal_key"])):
        raise ValueError("row")
    authors = row.get("authors")
    if not isinstance(authors, list) or any(not isinstance(author, str) for author in authors):
        raise ValueError("row")
    agents = row.get("agents")
    editors = row.get("editors", [])
    if not isinstance(agents, list) or not isinstance(editors, list):
        raise ValueError("row")
    agency = row.get("agency")
    message_date = row.get("message_date")
    if not isinstance(message_date, str):
        raise ValueError("row")
    return {
        **{field: " ".join(str(value).split()) for field, value in texts.items()},
        "authors": [" ".join(author.split()) for author in authors],
        "publisher": _dealmaker(row.get("publisher")),
        "agents": [_dealmaker(agent) for agent in agents],
        "editors": [_dealmaker(editor) for editor in editors],
        "agency": None if agency is None else _dealmaker(agency),
        "source_week": datetime.fromisoformat(message_date).date().isoformat(),
        **_rights_lead(row),
    }


def validate_collection(collection: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Revalidate a pass result and return its rows, trimmed to the fields this contract reads."""
    try:
        if collection.get("schema_version") not in RESULT_SCHEMA_VERSIONS or not isinstance(collection["rows"], list):
            raise ValueError("identity")
        datetime.fromisoformat(collection["collected_at"])
        if not isinstance(collection.get("stop_reason", ""), str):
            raise ValueError("stop reason")
        return [_checked_row(row) for row in collection["rows"]]
    except (KeyError, TypeError, ValueError, AttributeError):
        _fail("pm_collection_state", "PM deals result has an invalid shape")


def collected_date(collection: Mapping[str, Any]) -> date:
    try:
        return datetime.fromisoformat(str(collection["collected_at"])).date()
    except (KeyError, TypeError, ValueError):
        _fail("pm_collection_state", "PM deals result has no valid collected_at")


def seen_items_path(config_path: Path, source: Mapping[str, Any]) -> Path | None:
    raw_path = str(source.get("seen_guids_file", "")).strip()
    return (config_path.parent / raw_path).resolve() if raw_path else None


def read_seen_items(path: Path | None) -> dict[str, str]:
    """The seen-deal index: deal key to the run date that first reported it."""
    if path is None:
        return {}
    try:
        data = read_json(path, default={})
    except json.JSONDecodeError:
        _fail("pm_seen_state", "PM seen-deal state is malformed JSON")
    if not isinstance(data, dict) or not isinstance(data.get("seen_guids", []), list):
        _fail("pm_seen_state", "PM seen-deal state has an invalid shape")
    seen: dict[str, str] = {}
    for entry in data.get("seen_guids", []):
        try:
            key, first = entry["guid"], entry["first_seen_date"]
            if not _DEAL_KEY.fullmatch(key) or date.fromisoformat(first).isoformat() != first:
                raise ValueError("entry")
        except (KeyError, TypeError, ValueError):
            _fail("pm_seen_state", "PM seen-deal state has an invalid entry")
        seen[key] = first
    return seen


def record_seen_items(path: Path | None, source: Mapping[str, Any], deal_keys: list[str], run_date: str) -> int:
    """Record the reported deals, keeping an earlier first-seen date. Returns how many were new."""
    if path is None:
        return 0
    seen = read_seen_items(path)
    new = [key for key in dict.fromkeys(deal_keys) if key not in seen]
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


def record_reported_deals(
    config_path: Path, source: Mapping[str, Any], mentions: list[Mapping[str, Any]], run_date: str
) -> int:
    """Record the deals a weekly run reports, once its pipeline consumes their recorded mentions.

    The capture stage writes nothing, so a source health check that runs the
    same capture never marks a deal as reported. Returns how many were new.
    """
    from autoscout.pm_deal_email import deal_key

    keys = [
        deal_key(str(entry.get("title", "")), list(entry.get("primary_authors", [])), str(entry.get("publisher", "")))
        for entry in mentions
    ]
    return record_seen_items(seen_items_path(config_path, source), source, keys, run_date)


record_reported = record_reported_deals


def capture(
    source: Mapping[str, Any],
    reference: date,
    collection: Mapping[str, Any] | None,
    seen: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Bind the last PM deals pass to one run. This reads no mail and requests nothing.

    A missing result and a result older than the declared age are named
    refusals, so the weekly run reports PM and carries on without it.
    The bundle keeps the trimmed rows only: no logline, no contact line.
    """
    validate_declaration(source)
    if collection is None:
        _fail("pm_collection_missing", "PM deals result is missing; run ./scripts/collect-pm-deals")
    rows = validate_collection(collection)
    collected = collected_date(collection)
    max_age = _max_age(source)
    # The pass clock is UTC and the run date is the operator's, so one day ahead is not the future.
    if not -1 <= (reference - collected).days <= max_age:
        _fail("pm_stale_collection", f"PM deals result is outside the {max_age}-day age limit")
    keys = {row["deal_key"] for row in rows}
    return {
        "version": 1,
        "source_url": SOURCE_URL,
        "reference_date": reference.isoformat(),
        "collected_date": collected.isoformat(),
        "status": str(collection.get("status", "")),
        "stop_reason": str(collection.get("stop_reason", "")),
        "rows": rows,
        # A deal reported by an earlier run is seen. One first reported today is shown again, so a
        # second run on the same date does not lose what the first one captured.
        "seen_items": sorted(
            key for key, first in (seen or {}).items() if key in keys and first < reference.isoformat()
        ),
    }


def evidence_url(row: Mapping[str, Any]) -> str:
    """The agent's profile link, else the agency's, else the publisher's; '' when the deal has none."""
    candidates = [url for _name, url in row["agents"]]
    if row["agency"] is not None:
        candidates.append(row["agency"][1])
    candidates.append(row["publisher"][1])
    return next((url for url in candidates if _PROFILE_URL.fullmatch(url)), "")


def _drop_reason(row: Mapping[str, Any], labels: set[str], seen: set[str], minimum_size: str = "") -> str:
    if row["country"] or row["section"] == INTERNATIONAL_SECTION:
        return DROP_INTERNATIONAL
    if row["section"] != NONFICTION_SECTION:
        return DROP_NOT_NONFICTION
    if row["category"].casefold() not in labels:
        return DROP_UNDECLARED_CATEGORY
    if minimum_size:
        if not row["deal_size"]:
            return DROP_SIZE_UNDISCLOSED
        if DEAL_SIZE_BANDS.index(row["deal_size"]) < DEAL_SIZE_BANDS.index(minimum_size):
            return DROP_BELOW_MIN_SIZE
    if not evidence_url(row):
        return DROP_NO_EVIDENCE_URL
    if row["deal_key"] in seen:
        return DROP_SEEN
    return ""


def dropped_row_counts(bundle: Mapping[str, Any], source: Mapping[str, Any]) -> dict[str, int]:
    """How many deals of the bound result are not reported, by reason."""
    labels, seen, minimum = category_labels(source), set(bundle["seen_items"]), minimum_size_band(source)
    counts: dict[str, int] = {}
    for row in bundle["rows"]:
        reason = _drop_reason(row, labels, seen, minimum)
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def qualifying_rows(bundle: Mapping[str, Any], source: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The deals a run reports: a declared Non-fiction category, the declared minimum size when one
    is set, a profile link, not seen before."""
    labels, seen, minimum = category_labels(source), set(bundle["seen_items"]), minimum_size_band(source)
    return [row for row in bundle["rows"] if not _drop_reason(row, labels, seen, minimum)]


def _names(names: list[str]) -> str:
    if len(names) < 3:
        return " and ".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def evidence_note(row: Mapping[str, Any]) -> str:
    """AutoScout's own sentence about the deal, from its structured fields only."""
    parts = [f"Publishers Marketplace rights deal ({row['category']}): sold to {row['publisher'][0]}"]
    if row["territory"]:
        parts.append(f"territory {row['territory']}")
    agents = [name for name, _url in row["agents"]]
    agency = row["agency"][0] if row["agency"] is not None else ""
    if agents:
        parts.append(f"agent {_names(agents)}" + (f" at {agency}" if agency else ""))
    elif agency:
        parts.append(f"agency {agency}")
    return "; ".join(parts) + "."


def mention(row: Mapping[str, Any], source: Mapping[str, Any], run_date: str) -> dict[str, Any]:
    return {
        "title": row["title"],
        "primary_authors": list(row["authors"]),
        "publisher": row["publisher"][0],
        "source_week": row["source_week"],
        "source_url": SOURCE_URL,
        "evidence_url": evidence_url(row),
        "source_check_date": run_date,
        "signal_type": SIGNAL_TYPE,
        "signal_strength": signal_strength(source),
        "evidence_note": evidence_note(row),
        "candidate_category": CANDIDATE_CATEGORY,
        "publication_status": PUBLICATION_STATUS,
        "rights_deal": rights_deal(row),
    }


def rights_deal(row: Mapping[str, Any]) -> dict[str, Any]:
    """The deal as a rights lead (#948): who bought it and who sold it, for where, how, and when.

    ``publication`` is ``{"year", "season", "month"}`` or None, so a later stage can compute a
    date from it; ``rights_also`` lists the territories already sold.
    """
    return {
        "publisher": row["publisher"][0],
        "editors": [name for name, _url in row["editors"]],
        "agents": [name for name, _url in row["agents"]],
        "agency": row["agency"][0] if row["agency"] is not None else "",
        "contacts": list(row["contacts"]),
        "territory": row["territory"],
        "rights_also": list(row["rights_also"]),
        "category": row["category"],
        "deal_size": row["deal_size"],
        "deal_types": list(row["deal_types"]),
        "book_count": row["book_count"],
        "publication": dict(row["publication"]) if row["publication"] is not None else None,
        "earlier_books": list(row["earlier_books"]),
    }


def mention_violation(entry: Mapping[str, Any], bundle: Mapping[str, Any], source: Mapping[str, Any]) -> str:
    """Refuse a mention that does not agree with exactly one qualifying captured row.

    Every identity field is the parser's. Nothing may supply or correct a
    title, an author, a publisher, the evidence link or the note.
    """
    prohibited = sorted(set(entry) - _MENTION_FIELDS)
    if prohibited:
        return f"PM mention contains fields the deal rows do not support: {', '.join(prohibited)}"
    matches = 0
    for row in qualifying_rows(bundle, source):
        expected = mention(row, source, str(entry.get("source_check_date", "")))
        if all(entry.get(field) == value for field, value in expected.items()):
            matches += 1
    if matches == 1:
        return ""
    if matches > 1:
        return "PM mention agrees with more than one qualifying captured deal row"
    return "PM mention does not agree with a qualifying captured deal row"
