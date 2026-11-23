"""The age of the work behind every book: first publication year and edition count (#1138).

AutoScout holds the year of the listed edition only, so *Sapiens* found on a
Spanish list dated 2026 reads as a new book. This pass records, per Candidate
History record, how old the underlying work is, from Open Library:

1. A book with a confirmed original edition is looked up by the original's
   title, so a translation takes the age of its original.
2. Otherwise a book with an ISBN is looked up by that ISBN, and then, when no
   work agrees, by its title and author.
3. A book without an ISBN is looked up by its title and author.

Every answer must agree on title and author through the strict match the
original-edition confirmation uses
(:func:`autoscout.original_edition.title_and_author_agree`). A work agrees
when its own title, or the title of the edition Open Library matched, is the
book's title. A doubtful match is never a guess: with no agreeing work the
book is recorded ``not_found`` and its year and edition count stay unknown.
A catalogue that did not answer leaves the record unchecked for the next run.

The facts are stored in one ``work_age`` block. They are shown on the review
card; they are not an input of the fit score (:func:`fit_model_state`).
"""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from datetime import timedelta
from pathlib import Path
from typing import Any
from typing import Protocol
from urllib.parse import urlencode
from urllib.parse import urlsplit

from autoscout.candidate_schema import CandidateIsbn
from autoscout.isbn_completion import OPEN_LIBRARY_SEARCH_URL
from autoscout.isbn_completion import BoundedPageGetter
from autoscout.isbn_completion import PageGetter
from autoscout.isbn_completion import isbn13
from autoscout.json_io import read_json_or_default
from autoscout.learned_field_completion import SCOPE_ALL
from autoscout.learned_field_completion import SCOPE_CURRENT
from autoscout.learned_field_completion import SCOPE_UNDECIDED
from autoscout.learned_field_completion import LearnedFieldSpec
from autoscout.learned_field_completion import load_aliases
from autoscout.learned_field_completion import persist_learned_fields
from autoscout.learned_field_completion import project_fields
from autoscout.learned_field_completion import read_history
from autoscout.learned_field_completion import seal_finalization
from autoscout.learned_field_completion import select_records
from autoscout.original_edition import STATUS_CONFIRMED as ORIGINAL_CONFIRMED
from autoscout.original_edition import open_library_search_url
from autoscout.original_edition import title_and_author_agree
from autoscout.original_edition import validate_original_edition
from autoscout.state_store import StateStore
from autoscout.state_store import open_state_store

STAGE_NAME = "work-age-completion"
REPORT_FILENAME = "work-age-completion.json"
_SEAL_PENDING_FILENAME = ".work-age-completion-seal-pending.json"

WORK_AGE_FIELD = "work_age"
LEARNED_FIELDS = (WORK_AGE_FIELD,)

STATUS_FOUND = "found"
STATUS_NOT_FOUND = "not_found"
STATUS_NOT_CHECKED = "not_checked"
STATUSES = (STATUS_FOUND, STATUS_NOT_FOUND, STATUS_NOT_CHECKED)

# What the lookup was keyed on.
BASIS_ORIGINAL_EDITION = "original_edition"
BASIS_ISBN = "isbn"
BASIS_TITLE_AUTHOR = "title_author"
BASES = (BASIS_ORIGINAL_EDITION, BASIS_ISBN, BASIS_TITLE_AUTHOR)

SOURCE_OPEN_LIBRARY = "open_library"

WORK_AGE_KEYS = ("status", "first_publish_year", "edition_count", "basis", "source", "source_url", "lookup_date")

# A book the catalogue does not know yet is asked again after this many days:
# a new book usually has no record when it is first seen.
RETRY_AFTER_DAYS = 28
# The weekly stage looks up this run's books only; the backfill command takes
# every undecided book and may run longer.
STAGE_BUDGET_SECONDS = 600.0
BACKFILL_BUDGET_SECONDS = 3600.0
# Open Library asks unidentified clients for at most one request per second.
# The getter keeps at least this spacing to openlibrary.org.
MIN_REQUEST_INTERVAL_SECONDS = 2.0
# After this many unavailable answers in a row the catalogue is taken to be
# down or rate-limiting, and the pass stops asking it (as the library lookups
# of #1139 do). An answer resets the count.
UNAVAILABLE_LIMIT = 3

_SEARCH_FIELDS = "key,title,author_name,first_publish_year,edition_count,editions,editions.key,editions.title"
_ISBN_SEARCH_LIMIT = 5


# ── The record ───────────────────────────────────────────────────────────


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return int(value)


def _is_open_library_url(url: Any) -> bool:
    if not isinstance(url, str) or not url:
        return False
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").casefold()
    except ValueError:
        return False
    return parts.scheme == "https" and host == "openlibrary.org"


def build_work_age(
    *,
    status: str,
    lookup_date: str,
    first_publish_year: int | None = None,
    edition_count: int | None = None,
    basis: str = "",
    source_url: str = "",
) -> dict[str, Any]:
    """Assemble one ``work_age`` block; only a found work carries facts."""
    found = status == STATUS_FOUND
    return {
        "status": status,
        "first_publish_year": first_publish_year if found else None,
        "edition_count": edition_count if found else None,
        "basis": basis,
        "source": SOURCE_OPEN_LIBRARY if status != STATUS_NOT_CHECKED else "",
        "source_url": source_url if found else "",
        "lookup_date": lookup_date,
    }


def validate_work_age(value: Any) -> bool:
    """True when ``value`` is a complete, internally consistent ``work_age`` block."""
    if not isinstance(value, dict) or set(value) != set(WORK_AGE_KEYS):
        return False
    if value["status"] not in STATUSES:
        return False
    year, count = value["first_publish_year"], value["edition_count"]
    if year is not None and (isinstance(year, bool) or not isinstance(year, int)):
        return False
    if count is not None and _count(count) is None:
        return False
    if not all(isinstance(value[key], str) for key in ("basis", "source", "source_url", "lookup_date")):
        return False
    if value["status"] == STATUS_NOT_CHECKED:
        return year is None and count is None and not value["basis"] and not value["source"] and not value["source_url"]
    if value["basis"] not in BASES or value["source"] != SOURCE_OPEN_LIBRARY:
        return False
    try:
        date.fromisoformat(value["lookup_date"])
    except ValueError:
        return False
    if value["status"] == STATUS_NOT_FOUND:
        # A book the catalogue does not know, or knows only doubtfully, has no facts.
        return year is None and count is None and not value["source_url"]
    return (year is not None or count is not None) and _is_open_library_url(value["source_url"])


def work_age_of(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The record's valid ``work_age`` block, or ``None`` (not checked)."""
    value = record.get(WORK_AGE_FIELD)
    return value if validate_work_age(value) else None


def _editions_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "издание"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "издания"
    return "изданий"


def format_work_age(record: Mapping[str, Any]) -> str:
    """Plain card text for a found work, or ``""`` when nothing is known."""
    block = work_age_of(record)
    if block is None or block["status"] != STATUS_FOUND:
        return ""
    parts = []
    if block["first_publish_year"] is not None:
        parts.append(f"впервые издана в {block['first_publish_year']}")
    if block["edition_count"] is not None:
        count = block["edition_count"]
        parts.append(f"{count} {_editions_word(count)} в Open Library")
    text = ", ".join(parts)
    return text[:1].upper() + text[1:]


# ── Matching ─────────────────────────────────────────────────────────────


def isbn_search_url(isbn: str) -> str:
    """The Open Library search for the works that hold one ISBN."""
    return f"{OPEN_LIBRARY_SEARCH_URL}?{urlencode({'q': f'isbn:{isbn}', 'fields': _SEARCH_FIELDS, 'limit': _ISBN_SEARCH_LIMIT})}"


def title_search_url(title: str, author: str) -> str:
    """The Open Library search for a title and its author, as the original-edition confirmation asks it."""
    return open_library_search_url(title, author, fields=_SEARCH_FIELDS)


def _edition_titles(doc: Mapping[str, Any]) -> list[Any]:
    editions = doc.get("editions")
    docs = editions.get("docs") if isinstance(editions, Mapping) else None
    return [edition.get("title") for edition in docs if isinstance(edition, Mapping)] if isinstance(docs, list) else []


def match_work(docs: Sequence[Any], title: str, authors: Sequence[str]) -> tuple[int | None, int | None, str] | None:
    """``(first_publish_year, edition_count, work_url)`` of the agreeing work, or ``None``.

    A work agrees when its title or the title of its matched edition is
    ``title`` and an author matches in full. Open Library sometimes holds one
    work twice; several agreeing works give the earliest year and the sum of
    their editions, with the work that has the most editions as the source.
    """
    agreeing: list[tuple[int | None, int | None, str]] = []
    for doc in docs:
        if not isinstance(doc, Mapping):
            continue
        key = doc.get("key")
        if not isinstance(key, str) or not key.startswith("/works/"):
            continue
        titles = [doc.get("title"), *_edition_titles(doc)]
        if not title_and_author_agree(titles, doc.get("author_name"), title, authors):
            continue
        year = doc.get("first_publish_year")
        year = year if isinstance(year, int) and not isinstance(year, bool) else None
        agreeing.append((year, _count(doc.get("edition_count")), f"https://openlibrary.org{key}"))
    if not agreeing:
        return None
    years = [year for year, _count_value, _url in agreeing if year is not None]
    counts = [count for _year, count, _url in agreeing if count is not None]
    if not years and not counts:
        return None
    source_url = max(agreeing, key=lambda item: item[1] or 0)[2]
    return (min(years) if years else None, sum(counts) if counts else None, source_url)


# ── Selection ────────────────────────────────────────────────────────────


def _authors(record: Mapping[str, Any]) -> list[str]:
    authors = record.get("primary_authors")
    if isinstance(authors, list):
        return [str(author) for author in authors if str(author).strip()]
    return [str(authors)] if authors else []


def _confirmed_original(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    edition = record.get("original_edition")
    if isinstance(edition, dict) and validate_original_edition(edition) and edition["status"] == ORIGINAL_CONFIRMED:
        return edition
    return None


def _is_settled(record: Mapping[str, Any], today: date) -> bool:
    """True when the record needs no lookup.

    A found work is settled unless an original edition was confirmed after
    it was looked up as the listed edition. A book the catalogue did not
    know rests for :data:`RETRY_AFTER_DAYS`.
    """
    block = work_age_of(record)
    if block is None or block["status"] == STATUS_NOT_CHECKED:
        return False
    if block["status"] == STATUS_FOUND:
        return block["basis"] == BASIS_ORIGINAL_EDITION or _confirmed_original(record) is None
    looked_up = date.fromisoformat(block["lookup_date"])
    return today - looked_up < timedelta(days=RETRY_AFTER_DAYS)


# ── Lookups ──────────────────────────────────────────────────────────────


class _BudgetExhausted(Exception):
    """The stage deadline passed before the next request."""


class _LookupUnavailable(Exception):
    """The catalogue gave no usable answer (timeout, HTTP error, malformed JSON)."""


def _docs(url: str, getter: PageGetter, deadline: float, monotonic: Callable[[], float]) -> list[Any]:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise _BudgetExhausted
    body = getter.get(url, timeout=remaining)
    if body is None and monotonic() >= deadline:
        raise _BudgetExhausted
    try:
        response = json.loads(body) if body is not None else None
    except ValueError:
        response = None
    if not isinstance(response, Mapping) or not isinstance(response.get("docs"), list):
        # No answer is not a "no": the record stays unchecked for the next run.
        raise _LookupUnavailable
    return list(response["docs"])


def lookup_work_age(
    record: Mapping[str, Any],
    getter: PageGetter,
    *,
    today: date,
    deadline: float,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any] | None:
    """The record's ``work_age`` block from Open Library, or ``None`` when nothing could be asked.

    Raises :class:`_LookupUnavailable` or :class:`_BudgetExhausted` when the
    catalogue did not answer in time; the record is then left as it was.
    """
    authors = _authors(record)
    original = _confirmed_original(record)
    plan: list[tuple[str, str, str]] = []
    if original is not None:
        title = str(original["original_title"])
        plan.append((BASIS_ORIGINAL_EDITION, title_search_url(title, authors[0] if authors else ""), title))
    else:
        title = str(record.get("title", "") or "")
        isbn = isbn13(CandidateIsbn.from_record(record).value)
        if isbn:
            plan.append((BASIS_ISBN, isbn_search_url(isbn), title))
        plan.append((BASIS_TITLE_AUTHOR, title_search_url(title, authors[0] if authors else ""), title))
    if not authors or not str(plan[0][2]).strip():
        return None
    for basis, url, wanted_title in plan:
        match = match_work(_docs(url, getter, deadline, monotonic), wanted_title, authors)
        if match is not None:
            year, count, source_url = match
            return build_work_age(
                status=STATUS_FOUND,
                lookup_date=today.isoformat(),
                first_publish_year=year,
                edition_count=count,
                basis=basis,
                source_url=source_url,
            )
    return build_work_age(status=STATUS_NOT_FOUND, lookup_date=today.isoformat(), basis=plan[-1][0])


# ── The pass ─────────────────────────────────────────────────────────────


class _Progress(Protocol):
    def initial(self, items: list[tuple[str, str]]) -> None: ...
    def begin(self, key: str) -> None: ...
    def finish(self, key: str) -> None: ...
    def final(self, *, status: str = "finished") -> None: ...


@dataclass
class CompletionReport:
    considered: int = 0
    found: int = 0
    not_found: int = 0
    not_askable: int = 0
    skipped_settled: int = 0
    lookup_unavailable: int = 0
    # Left unchecked because the catalogue stopped answering in this pass.
    skipped_catalogue_unavailable: int = 0
    budget_exhausted: int = 0
    by_basis: dict[str, int] = field(default_factory=lambda: dict.fromkeys(BASES, 0))
    # Identity keys of the records this pass changed; only these are merged back.
    touched: set[str] = field(default_factory=set)

    def as_dict(self) -> dict[str, Any]:
        return {
            "considered": self.considered,
            "found": self.found,
            "not_found": self.not_found,
            "not_askable": self.not_askable,
            "skipped_settled": self.skipped_settled,
            "lookup_unavailable": self.lookup_unavailable,
            "skipped_catalogue_unavailable": self.skipped_catalogue_unavailable,
            "budget_exhausted": self.budget_exhausted,
            "found_by_basis": dict(self.by_basis),
        }


def complete_work_age(
    records: Sequence[dict[str, Any]],
    *,
    getter: PageGetter | None,
    today: date,
    budget_seconds: float = STAGE_BUDGET_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    progress: _Progress | None = None,
) -> CompletionReport:
    """Record the work age of each record in place and report what happened.

    ``getter=None`` learns nothing: every record that needs a lookup counts
    as ``lookup_unavailable`` and stays unchecked. After
    :data:`UNAVAILABLE_LIMIT` unavailable answers in a row the catalogue is
    not asked again in this pass: the remaining records count as
    ``skipped_catalogue_unavailable`` and stay unchecked for the next run.
    """
    report = CompletionReport()
    deadline = monotonic() + budget_seconds
    todo = []
    for record in records:
        report.considered += 1
        if _is_settled(record, today):
            report.skipped_settled += 1
        else:
            todo.append(record)
    keys = [str(record.get("candidate_id") or record.get("identity_key", "")) for record in todo]
    if progress is not None:
        progress.initial([(key, str(record.get("title", ""))) for key, record in zip(keys, todo, strict=True)])
    unavailable_in_a_row = 0
    for key, record in zip(keys, todo, strict=True):
        if progress is not None:
            progress.begin(key)
        if getter is not None and unavailable_in_a_row >= UNAVAILABLE_LIMIT:
            report.skipped_catalogue_unavailable += 1
            if progress is not None:
                progress.finish(key)
            continue
        try:
            if getter is None:
                raise _LookupUnavailable
            block = lookup_work_age(record, getter, today=today, deadline=deadline, monotonic=monotonic)
        except _BudgetExhausted:
            report.budget_exhausted += 1
            block = None
        except _LookupUnavailable:
            report.lookup_unavailable += 1
            unavailable_in_a_row += 1
            block = None
        else:
            unavailable_in_a_row = 0
            if block is None:
                report.not_askable += 1
        if block is not None:
            record[WORK_AGE_FIELD] = block
            report.touched.add(str(record.get("identity_key", "")))
            if block["status"] == STATUS_FOUND:
                report.found += 1
                report.by_basis[block["basis"]] += 1
            else:
                report.not_found += 1
        if progress is not None:
            progress.finish(key)
    if progress is not None:
        progress.final()
    return report


# ── State files ──────────────────────────────────────────────────────────


def _copy_work_age(history_record: Mapping[str, Any], row: dict[str, Any]) -> bool:
    """Copy the learned block onto one snapshot row; True only when the row changed."""
    block = work_age_of(history_record)
    if block is None or row.get(WORK_AGE_FIELD) == block:
        return False
    row[WORK_AGE_FIELD] = copy.deepcopy(block)
    return True


def _never_filled(_record: Mapping[str, Any]) -> bool:
    # This pass is the only writer of ``work_age``, and the report directory's
    # lock keeps two passes apart, so the fresh value is always replaced.
    return False


# The envelope's seal step, kept as this module's own name: a patched
# ``_seal_finalization`` still intercepts it.
_seal_finalization = seal_finalization

_LEARNED_FIELD_SPEC = LearnedFieldSpec(
    fields=LEARNED_FIELDS,
    already_filled=_never_filled,
    copy_to_row=_copy_work_age,
    report_filename=REPORT_FILENAME,
    seal_pending_filename=_SEAL_PENDING_FILENAME,
    label="work age completion",
    project=lambda history, rows, aliases: project_work_age(history, rows, aliases),
    seal=lambda output_dir, store: _seal_finalization(output_dir, store),
)


def project_work_age(
    history: Sequence[dict[str, Any]], rows: Sequence[Mapping[str, Any]], aliases: Mapping[str, Any] | None = None
) -> tuple[list[dict[str, Any]], int]:
    """Copy the learned work age from history onto snapshot rows; return the rows and how many changed."""
    return project_fields(history, rows, aliases, _LEARNED_FIELD_SPEC)


def run_work_age_completion(
    output_dir: Path,
    state_dir: Path,
    *,
    effective_date: str,
    scope: str = SCOPE_ALL,
    getter: PageGetter | None = None,
    dry_run: bool = False,
    publish_lock: Callable[[], Any] | None = None,
    budget_seconds: float = STAGE_BUDGET_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    progress: _Progress | None = None,
    state_store: StateStore | None = None,
) -> dict[str, Any]:
    """Record the work age of the selected records and persist it.

    ``scope="all"`` takes every record, decided or not; ``scope="current"``
    takes this run's candidates; ``scope="undecided"`` takes every record
    without an editor decision. History and the latest
    snapshot are written first, through *state_store* (the run's store) or a
    store opened over *state_dir*, then the current snapshot, then the
    report. ``dry_run`` writes nothing.
    """
    if scope not in {SCOPE_ALL, SCOPE_CURRENT, SCOPE_UNDECIDED}:
        raise ValueError(f"unknown scope: {scope}")
    today = date.fromisoformat(effective_date)
    store = state_store if state_store is not None else open_state_store(state_dir)
    history = read_history(store)
    if history is None:
        return {"status": "input_failed", "reason": "invalid_candidate_history"}
    current = read_json_or_default(output_dir / "scouting-candidates.json", [])
    selected = select_records(history, scope, current if isinstance(current, list) else [], load_aliases(store))
    report = complete_work_age(
        selected, getter=getter, today=today, budget_seconds=budget_seconds, monotonic=monotonic, progress=progress
    )
    result: dict[str, Any] = {"status": "ok", "scope": scope, "dry_run": dry_run, **report.as_dict()}
    if dry_run:
        return result
    persist_learned_fields(
        output_dir,
        store,
        spec=_LEARNED_FIELD_SPEC,
        selected=selected,
        touched=report.touched,
        changed=bool(report.touched),
        result=result,
        effective_date=effective_date,
        publish_lock=publish_lock,
    )
    return result


def open_library_getter() -> BoundedPageGetter:
    """The bounded getter, spaced to Open Library's request-rate guidance."""
    return BoundedPageGetter(min_host_interval_seconds=MIN_REQUEST_INTERVAL_SECONDS)


# ── Weekly stage ─────────────────────────────────────────────────────────


class ProductionWorkAgeCompletionStage:
    """The live weekly stage: this run's candidates, the spaced Open Library getter, the heartbeat."""

    def __init__(self, getter_factory: Callable[[], PageGetter | None] = open_library_getter) -> None:
        self._getter_factory = getter_factory
        self._presentation: Callable[[dict[str, Any]], None] | None = None
        self._failure_recorder: Callable[[str, str], None] | None = None

    def set_snapshot_progress_presentation(self, presentation: Callable[[dict[str, Any]], None] | None) -> None:
        self._presentation = presentation

    def set_progress_snapshot_failure_recorder(self, recorder: Callable[[str, str], None] | None) -> None:
        self._failure_recorder = recorder

    def complete(
        self, output_dir: Path, state_dir: Path, *, effective_date: str, state_store: StateStore | None = None
    ) -> dict[str, Any]:
        from autoscout.progress_snapshot import CandidateProgressSnapshot

        tracker = CandidateProgressSnapshot(
            output_dir,
            stage=STAGE_NAME,
            stage_budget_seconds=STAGE_BUDGET_SECONDS,
            failure_recorder=self._failure_recorder,
            presentation=self._presentation,
        )
        return run_work_age_completion(
            output_dir,
            state_dir,
            effective_date=effective_date,
            scope=SCOPE_CURRENT,
            getter=self._getter_factory(),
            progress=tracker,
            state_store=state_store,
        )
