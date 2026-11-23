"""Find out whether a Russian edition of each undecided book exists (#1181).

The pass mirrors the original-edition completion pass (#968): one pass over
Candidate History, the sources and the model as injected dependencies,
writing learned fields through the shared learned-field envelope, fill-only.
For each selected book:

1. **LiveLib** is searched by the book's lookup title alone (the confirmed
   original title, else the English title, else the title; no subtitle). A
   result is kept when its title is Cyrillic and its author corresponds to
   the book's author (surname and, when both carry one, given name; the
   stored name never vetoes a result). A LiveLib search actually made is
   dated (``russian_edition_livelib_search_date``). LiveLib only locates: its book pages are never fetched,
   and a LiveLib result alone never confirms an edition. LiveLib refuses a
   client after about thirty searches, so a pass makes at most
   :data:`MAX_LIVELIB_SEARCHES` of them, in the order the books come.
2. **The Cyrillic author name** prefers the published spelling: the author
   of the kept LiveLib result, else the stored ``russian_author_name`` when it
   corresponds to the book's author (one that does not is ignored and
   replaced), else
   the Russian label of the author's Wikidata entry (asked only when the
   first two gave nothing). Without one, the author route is skipped. A
   LiveLib spelling is stored as ``russian_author_name`` only when the book
   ends ``confirmed`` or ``unconfirmed`` with it; a Wikidata one whenever it
   is learned.
3. **The Russian State Library** (by author; when LiveLib located a title,
   by author and that title as well, and both must answer) and 4.
   **LitRes** list the author's Russian-language books by that name; an
   edition is kept when a credited author is both that name and the book's
   author. When neither lists anything and a
   second, different spelling is already known, they are asked once more
   with it: at most two spellings per book. The State Library answers ten
   records: an author-only answer with more hits is partial, and settles a
   miss only when LiveLib answered for the book in this pass.
5. **A model** is asked one closed question per book, in batches: which of
   the numbered Russian titles, if any, is the same work. Only editions that
   can pass the guards of step 6 are numbered
   (:class:`SameWorkMatcher`). Its input holds bibliographic fields only
   (:data:`MATCH_ITEM_FIELDS`, :data:`MATCH_ENTRY_FIELDS`).
6. :func:`autoscout.russian_edition.decide` applies the guards that do not
   depend on the model and builds the ``russian_edition`` record.

Each source is paced, named honestly, and given up for the rest of the pass
after three failures or blocks in a row. LiveLib is optional for
completeness (:meth:`_Lookup.complete`): a book with a Cyrillic author name
is complete when every enabled catalogue answered for it, and a book without
one when LiveLib and Wikidata, each when enabled, answered for it; either way
at least one edition search (LiveLib, the State Library or LitRes) must have
answered for it, so ``no_find`` always follows a search. A book
that is not complete is recorded only when it is confirmed; otherwise it
gets no record and no lookup date, and is asked again next time. A confirmed
record is never replaced. The pass never reads or writes the editorial
``russian_edition_status`` or the ``russian_edition_verification`` record.
Every book the pass tries gets ``russian_edition_attempt_date``, completed
or not, so a book that could not be completed waits behind the books never
tried. Only what the pass itself set is merged into Candidate History, and
never a lookup or attempt date older than the one there. ``dry_run`` writes
nothing; an interruption recovers by running again. A weekly run found
under the lock immediately before the first write (``weekly_run_check``)
leaves everything unwritten.
"""

from __future__ import annotations

import contextlib
import copy
import http.client
import json
import tempfile
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
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.request import Request
from urllib.request import urlopen

from autoscout.json_io import read_json
from autoscout.json_io import read_json_or_default
from autoscout.learned_field_completion import SCOPE_CURRENT
from autoscout.learned_field_completion import SCOPE_UNDECIDED
from autoscout.learned_field_completion import LearnedFieldSpec
from autoscout.learned_field_completion import history_match
from autoscout.learned_field_completion import load_aliases
from autoscout.learned_field_completion import persist_learned_fields
from autoscout.learned_field_completion import project_fields
from autoscout.learned_field_completion import read_history
from autoscout.learned_field_completion import seal_finalization
from autoscout.learned_field_completion import select_records
from autoscout.model_client import ModelCallStatus
from autoscout.model_client import ModelRequest
from autoscout.model_client import description_model_client
from autoscout.russian_edition import ATTEMPT_DATE_FIELD
from autoscout.russian_edition import AUTHOR_NAME_FIELD
from autoscout.russian_edition import LEARNED_FIELDS
from autoscout.russian_edition import LIVELIB_SEARCH_DATE_FIELD
from autoscout.russian_edition import LOOKUP_DATE_FIELD
from autoscout.russian_edition import MATCH_RESPONSE_SCHEMA
from autoscout.russian_edition import RUSSIAN_EDITION_FIELD
from autoscout.russian_edition import STATUS_CONFIRMED
from autoscout.russian_edition import STATUS_UNCONFIRMED
from autoscout.russian_edition import TitleGroup
from autoscout.russian_edition import build_no_find
from autoscout.russian_edition import credits_author
from autoscout.russian_edition import cyrillic_name
from autoscout.russian_edition import decide
from autoscout.russian_edition import eligible_titles
from autoscout.russian_edition import is_confirmed
from autoscout.russian_edition import is_retelling
from autoscout.russian_edition import model_entry
from autoscout.russian_edition import names_correspond
from autoscout.russian_edition import russian_main_title
from autoscout.russian_edition import validate_match_response
from autoscout.russian_edition import validate_russian_edition
from autoscout.russian_edition import wikidata_russian_name
from autoscout.russian_edition_settings import WeeklySettings
from autoscout.russian_edition_settings import weekly_settings
from autoscout.russian_edition_sources import CATALOGUE_SOURCES
from autoscout.russian_edition_sources import SOURCE_LITRES
from autoscout.russian_edition_sources import SOURCE_LIVELIB
from autoscout.russian_edition_sources import SOURCE_RSL
from autoscout.russian_edition_sources import SOURCE_WIKIDATA
from autoscout.russian_edition_sources import SOURCES
from autoscout.russian_edition_sources import EditionEntry
from autoscout.russian_edition_sources import LiveLibResult
from autoscout.russian_edition_sources import has_cyrillic
from autoscout.russian_edition_sources import is_block_page
from autoscout.russian_edition_sources import litres_art_url
from autoscout.russian_edition_sources import litres_search_url
from autoscout.russian_edition_sources import livelib_search_url
from autoscout.russian_edition_sources import parse_litres_art
from autoscout.russian_edition_sources import parse_litres_search
from autoscout.russian_edition_sources import parse_livelib_search
from autoscout.russian_edition_sources import parse_rsl_author_answer
from autoscout.russian_edition_sources import parse_wikidata_search
from autoscout.russian_edition_sources import rsl_author_query
from autoscout.russian_edition_sources import wikidata_search_url
from autoscout.state_store import StateStore
from autoscout.state_store import open_state_store

STAGE_NAME = "russian-edition-completion"
REPORT_FILENAME = "russian-edition-completion.json"
_SEAL_PENDING_FILENAME = ".russian-edition-completion-seal-pending.json"

# The name every request carries, so a site owner can see who is asking.
CLIENT_NAME = "AutoScout Russian-edition check"
# A book without a confirmed edition is asked again after four weeks, as the
# original-edition pass does: translations keep appearing.
RETRY_AFTER_DAYS = 28
# Wikidata's spacing makes 150 books take more than 15 minutes.
STAGE_BUDGET_SECONDS = 30 * 60.0
MAX_BOOKS = 150
# A source that failed or blocked this many times in a row is not asked
# again in the same pass.
SOURCE_FAILURE_LIMIT = 3
# Wikidata refused this client after about ten requests a minute (2026-11-21).
MIN_REQUEST_INTERVAL_SECONDS = {SOURCE_LIVELIB: 6.0, SOURCE_WIKIDATA: 7.0, SOURCE_RSL: 1.0, SOURCE_LITRES: 1.0}
# LiveLib answered 31 searches 3 seconds apart and then sent every search to
# its rate-limit captcha for longer than ten minutes (2026-11-21), so a pass
# stays well below that. A repeat of a search already answered is not counted.
MAX_LIVELIB_SEARCHES = 25
REQUEST_TIMEOUT_SECONDS = 20.0
MAX_RESPONSE_BYTES = 2_000_000
MATCH_BATCH_SIZE = 10
MATCH_CALL_TIMEOUT_SECONDS = 120.0
MATCH_RETRIES = 1
MIN_MATCH_SECONDS = 30.0
# Inside the weekly run the stage starts only with this much time left: one
# request and one model call.
MIN_STAGE_SECONDS = REQUEST_TIMEOUT_SECONDS + MIN_MATCH_SECONDS
# While a model call runs, the stage's progress snapshot is refreshed this
# often (real seconds), so the doctor never reads a live stage as stalled.
KEEPALIVE_TICK_SECONDS = 0.25
DESCRIPTION_LIMIT = 400

# Everything the model is given, field by field. Nothing editorial (editor
# status, note, priority, visibility, taste profile, risk) is ever added.
MATCH_ITEM_FIELDS = (
    "candidate_id",
    "title",
    "subtitle",
    "author",
    "first_published_year",
    "description",
    "russian_titles",
)
MATCH_ENTRY_FIELDS = ("number", "title", "publisher", "year", "sources")

MATCH_PROMPT = (
    "Read items.json. Each item is one book (title, subtitle, author, first_published_year, description) "
    "and a numbered list of Russian-language titles that Russian catalogues and bookshops list for its author.\n"
    "For each book, answer the number of the Russian title that is an edition of this same work: a "
    "Russian translation of the whole book, under whatever Russian title it was published.\n"
    "Answer null when none of them is. Another book by the same author, a summary, abridgement, "
    "'key ideas' retelling or workbook of the book, and a book about the author are not the same work.\n"
    "When several numbers are the same work, prefer one whose sources include rsl or litres.\n"
    "Never guess.\n"
    "Return one entry per candidate_id, echoing the exact candidate_id from the item.\n"
)
MATCH_ITEMS_FILENAME = "items.json"
MATCH_SCHEMA_FILENAME = "response-schema.json"
MATCH_RESPONSE_FILENAME = "response.json"

OUTCOME_ANSWERED = "answered"
OUTCOME_BLOCKED = "blocked"
OUTCOME_FAILED = "failed"

RESULT_COUNTS = (
    "considered",
    "skipped_decided",
    "skipped_settled",
    "over_ceiling",
    "confirmed",
    "unconfirmed",
    "no_find",
    "not_completed",
    "match_unavailable",
    "budget_exhausted",
    "author_name_learned",
)


@dataclass(frozen=True)
class SourceAnswer:
    """What one request got: ``answered`` with a body, ``blocked`` or ``failed``."""

    outcome: str
    body: str = ""


class SourceFetcher(Protocol):
    def fetch(self, source: str, target: str, *, timeout: float) -> SourceAnswer: ...


class SameWorkMatcher(Protocol):
    def match(self, items: Sequence[Mapping[str, Any]], *, timeout: float) -> dict[str, int | None] | None: ...


class _BudgetExhausted(Exception):
    """The stage deadline passed before the next request."""


class _SourceUnavailable(Exception):
    """The source did not answer usably, or was given up for this pass."""


class _WeeklyRunStarted(Exception):
    """A weekly run started while the sources were asked; nothing is written."""


WEEKLY_RUN_IN_PROGRESS = "weekly_run_in_progress"
# The weekly stage's limits in the weekly config are not usable.
INVALID_WEEKLY_CONFIG = "invalid_weekly_config"


def _guarded_lock(
    publish_lock: Callable[[], Any] | None, weekly_run_check: Callable[[], bool] | None
) -> Callable[[], Any] | None:
    """``publish_lock`` that, once taken, refuses to go on when a weekly run is in progress.

    The command checks the weekly run when it starts; the lookups can take
    half an hour, so the check is made again under the lock, immediately
    before anything is written. A refusal leaves the lock normally before it
    is signalled: nothing was written, and a report transaction left by an
    exception keeps its marker as a half-written report.
    """
    if weekly_run_check is None:
        return publish_lock

    @contextlib.contextmanager
    def lock() -> Any:
        held = contextlib.ExitStack()
        if publish_lock is not None:
            held.enter_context(publish_lock())
        try:
            refused = weekly_run_check()
        except BaseException:
            held.close()
            raise
        if refused:
            held.close()
            raise _WeeklyRunStarted
        with held:
            yield

    return lock


@dataclass
class CompletionReport:
    considered: int = 0
    skipped_decided: int = 0
    skipped_settled: int = 0
    over_ceiling: int = 0
    confirmed: int = 0
    unconfirmed: int = 0
    no_find: int = 0
    # Sources could not all answer, or the model gave no answer for the book.
    not_completed: int = 0
    match_unavailable: int = 0
    budget_exhausted: int = 0
    author_name_learned: int = 0
    # Source → answered, blocked and failed requests, and requests not made
    # because the source was given up or its ceiling was reached.
    sources: dict[str, dict[str, int]] = field(
        default_factory=lambda: {
            source: {"answered": 0, "blocked": 0, "failed": 0, "not_asked": 0} for source in SOURCES
        }
    )
    # Identity key → the learned fields this pass set on that record, and
    # their values: the only values merged into Candidate History.
    learned: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def touched(self) -> set[str]:
        return set(self.learned)

    def learn(self, record: dict[str, Any], name: str, value: Any) -> None:
        """Set one learned field on ``record`` and remember that this pass set it."""
        record[name] = copy.deepcopy(value)
        self.learned.setdefault(str(record.get("identity_key", "")), {})[name] = copy.deepcopy(value)

    def as_dict(self) -> dict[str, Any]:
        counts: dict[str, Any] = {name: int(getattr(self, name)) for name in RESULT_COUNTS}
        counts["sources"] = {source: dict(values) for source, values in self.sources.items()}
        return counts


# ── Selection ────────────────────────────────────────────────────────────


def _date_field(record: Mapping[str, Any], name: str) -> date | None:
    try:
        return date.fromisoformat(str(record.get(name, "")))
    except ValueError:
        return None


def _lookup_date(record: Mapping[str, Any]) -> date | None:
    return _date_field(record, LOOKUP_DATE_FIELD)


def _attempt_date(record: Mapping[str, Any]) -> date | None:
    """The last attempt; a record looked up before attempts were recorded counts its lookup."""
    return _date_field(record, ATTEMPT_DATE_FIELD) or _lookup_date(record)


def _is_settled(record: Mapping[str, Any], today: date) -> bool:
    """True when the record holds a confirmed edition or was looked up within the retry interval."""
    if is_confirmed(record.get(RUSSIAN_EDITION_FIELD)):
        return True
    looked_up = _lookup_date(record)
    return looked_up is not None and today - looked_up < timedelta(days=RETRY_AFTER_DAYS)


def _decided(record: Mapping[str, Any]) -> bool:
    return bool(str(record.get("editor_status", "") or "").strip())


def order_records(
    records: Sequence[dict[str, Any]],
    current: Sequence[Any],
    history: Sequence[dict[str, Any]],
    aliases: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Books of the current candidate snapshot first, then the rest.

    Within each: never-attempted books first; then, among attempted books,
    those never searched on LiveLib, then the oldest LiveLib search, then
    the oldest attempt.
    """
    in_current = [
        matched
        for row in current
        if isinstance(row, Mapping) and (matched := history_match(row, history, aliases)) is not None
    ]

    def rank(indexed: tuple[int, dict[str, Any]]) -> tuple[int, int, int, str, str, int]:
        index, record = indexed
        attempted = _attempt_date(record)
        searched = _date_field(record, LIVELIB_SEARCH_DATE_FIELD)
        current_rank = 0 if any(record is matched for matched in in_current) else 1
        return (
            current_rank,
            0 if attempted is None else 1,
            0 if searched is None else 1,
            searched.isoformat() if searched else "",
            attempted.isoformat() if attempted else "",
            index,
        )

    return [record for _, record in sorted(enumerate(records), key=rank)]


# ── The book ─────────────────────────────────────────────────────────────


def _first_author(record: Mapping[str, Any]) -> str:
    authors = record.get("primary_authors")
    if isinstance(authors, list):
        return str(next((author for author in authors if str(author).strip()), "") or "").strip()
    return str(authors or "").strip()


def stored_author_name(record: Mapping[str, Any], author: str | None = None) -> str:
    """The stored ``russian_author_name`` when it is the book's author, else empty.

    A stored name that does not correspond to the book's Latin ``author``
    (by default the record's first author) — a stale «Джон Грин» on a Robert
    Greene record — is ignored, as if absent.
    """
    name = cyrillic_name(record.get(AUTHOR_NAME_FIELD))
    latin = _first_author(record) if author is None else author
    return name if name and latin and names_correspond(name, latin) else ""


def _split_title(title: str) -> tuple[str, str]:
    for separator in (":", "：", " — ", " – ", " - "):
        if separator in title:
            head, tail = title.split(separator, 1)
            if head.strip():
                return head.strip(), tail.strip()
    return title.strip(), ""


def _full_lookup_title(record: Mapping[str, Any]) -> str:
    edition = record.get("original_edition")
    if isinstance(edition, Mapping) and edition.get("status") == STATUS_CONFIRMED:
        original = str(edition.get("original_title", "") or "").strip()
        if original:
            return original
    for key in ("english_title", "title"):
        value = str(record.get(key, "") or "").strip()
        if value:
            return value
    return ""


def lookup_title(record: Mapping[str, Any]) -> str:
    """The original title when confirmed, else the English title, else the title; without the subtitle."""
    return _split_title(_full_lookup_title(record))[0]


def first_published_year(record: Mapping[str, Any]) -> int | None:
    """The work's first publication year: the work-age record, else the confirmed original edition."""
    work_age = record.get("work_age")
    if isinstance(work_age, Mapping) and work_age.get("status") == "found":
        year = work_age.get("first_publish_year")
        if isinstance(year, int) and not isinstance(year, bool):
            return year
    edition = record.get("original_edition")
    if isinstance(edition, Mapping) and edition.get("status") == STATUS_CONFIRMED:
        year = edition.get("original_first_published_year")
        if isinstance(year, int) and not isinstance(year, bool):
            return year
    return None


def _description(record: Mapping[str, Any]) -> str:
    for key in ("content_description", "english_annotation", "short_description"):
        value = " ".join(str(record.get(key, "") or "").split())
        if value:
            return value[:DESCRIPTION_LIMIT]
    return ""


def _candidate_id(record: Mapping[str, Any]) -> str:
    return str(record.get("candidate_id") or record.get("identity_key", ""))


def match_item(record: Mapping[str, Any], groups: Sequence[TitleGroup]) -> dict[str, Any]:
    """One book for the model: :data:`MATCH_ITEM_FIELDS` and nothing else."""
    title, subtitle = _split_title(_full_lookup_title(record))
    return {
        "candidate_id": _candidate_id(record),
        "title": title,
        "subtitle": subtitle,
        "author": _first_author(record),
        "first_published_year": first_published_year(record),
        "description": _description(record),
        "russian_titles": [model_entry(number, group) for number, group in enumerate(groups, start=1)],
    }


# ── Sources ──────────────────────────────────────────────────────────────


class _Sources:
    """Requests of one pass: pacing, the per-source failure limit, a cache and the counts."""

    def __init__(
        self,
        fetcher: SourceFetcher,
        enabled: Sequence[str],
        report: CompletionReport,
        deadline: float,
        monotonic: Callable[[], float],
        sleeper: Callable[[float], None],
        today: date,
        ceilings: Mapping[str, int] | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._ceilings = dict(ceilings or {})
        self._made: dict[str, int] = {}
        self.today = today.isoformat()
        self.enabled = frozenset(enabled)
        self.report = report
        self._deadline = deadline
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._failures: dict[str, int] = {}
        self._last_request: dict[str, float] = {}
        self._cache: dict[tuple[str, str], Any] = {}

    def usable(self, source: str) -> bool:
        return source in self.enabled

    def made(self, source: str) -> int:
        """How many requests were actually sent to ``source`` in this pass."""
        return self._made.get(source, 0)

    def _pace(self, source: str) -> None:
        last = self._last_request.get(source)
        now = self._monotonic()
        if last is not None:
            wait = last + MIN_REQUEST_INTERVAL_SECONDS.get(source, 1.0) - now
            if wait > 0:
                if now + wait >= self._deadline:
                    raise _BudgetExhausted
                self._sleeper(wait)
        self._last_request[source] = self._monotonic()

    def ask(self, source: str, target: str, parse: Callable[[str], Any]) -> Any:
        """The parsed answer of ``source`` for ``target``; raise when it gives none."""
        key = (source, target)
        if key in self._cache:
            return self._cache[key]
        counts = self.report.sources[source]
        given_up = self._failures.get(source, 0) >= SOURCE_FAILURE_LIMIT
        ceiling = self._ceilings.get(source)
        if given_up or (ceiling is not None and self._made.get(source, 0) >= ceiling):
            counts["not_asked"] += 1
            raise _SourceUnavailable
        if self._monotonic() >= self._deadline:
            raise _BudgetExhausted
        self._pace(source)
        remaining = self._deadline - self._monotonic()
        if remaining <= 0:
            raise _BudgetExhausted
        self._made[source] = self._made.get(source, 0) + 1
        answer = self._fetcher.fetch(source, target, timeout=min(REQUEST_TIMEOUT_SECONDS, remaining))
        parsed = parse(answer.body) if answer.outcome == OUTCOME_ANSWERED else None
        if parsed is not None:
            counts["answered"] += 1
            self._failures[source] = 0
            self._cache[key] = parsed
            return parsed
        blocked = answer.outcome == OUTCOME_BLOCKED or (
            answer.outcome == OUTCOME_ANSWERED and is_block_page(answer.body)
        )
        counts["blocked" if blocked else "failed"] += 1
        self._failures[source] = self._failures.get(source, 0) + 1
        raise _SourceUnavailable


@dataclass
class _Lookup:
    """What the sources gave for one book.

    ``unanswered`` names the sources that were needed and gave nothing,
    ``searched`` the edition searches (LiveLib, the catalogues) that answered.
    ``wikidata_name`` is the name Wikidata gave, ``published_name`` LiveLib's.
    ``partial`` names the catalogues whose author-only answer was partial
    (more hits than records read).
    """

    record: dict[str, Any]
    author_name: str = ""
    wikidata_name: str = ""
    published_name: str = ""
    groups: list[TitleGroup] = field(default_factory=list)
    unanswered: set[str] = field(default_factory=set)
    searched: set[str] = field(default_factory=set)
    partial: set[str] = field(default_factory=set)

    @property
    def complete(self) -> bool:
        """True when what is known about the book does not depend on a source that did not answer.

        At least one edition search must have answered. With a Cyrillic
        author name, every enabled catalogue must have answered for it;
        LiveLib and Wikidata no longer matter. Without one, LiveLib and
        Wikidata, each when enabled, must have answered. A partial
        author-only State Library answer counts as an answer only when
        LiveLib answered for the book in this pass; otherwise the book stays
        open (it never stops a confirmed edition, which needs no completeness).
        """
        unanswered = set(self.unanswered)
        if SOURCE_LIVELIB not in self.searched:
            unanswered |= self.partial
        if not self.searched or unanswered & set(CATALOGUE_SOURCES):
            return False
        return bool(self.author_name) or not self.unanswered & {SOURCE_LIVELIB, SOURCE_WIKIDATA}


def _kept_livelib(results: Sequence[LiveLibResult], author: str) -> LiveLibResult | None:
    """The first result with a Cyrillic title whose author corresponds to the book's author; a retelling only when nothing else is.

    Correspondence is decided against the book's own (Latin) author, never
    against a stored Cyrillic name, so a wrong stored name cannot veto it.
    """
    kept = [
        result
        for result in results
        if has_cyrillic(result.title) and result.author and names_correspond(result.author, author)
    ]
    plain = [result for result in kept if not is_retelling(_livelib_entry(result))]
    chosen = plain or kept
    return chosen[0] if chosen else None


def _livelib_entry(result: LiveLibResult) -> EditionEntry:
    return EditionEntry(
        source=SOURCE_LIVELIB,
        title=result.title,
        authors=(result.author,),
        language="ru" if has_cyrillic(result.title) else "",
        url=result.url,
    )


def _wikidata_name(sources: _Sources, author: str) -> str:
    """The author's Russian label from one Wikidata search answered in Russian."""
    hits = sources.ask(SOURCE_WIKIDATA, wikidata_search_url(author), parse_wikidata_search)
    return wikidata_russian_name(hits, author)


def _catalogue_queries(sources: _Sources, name: str, title: str) -> list[tuple[str, str, Callable[[str], Any]]]:
    """The catalogue requests for one spelling: (source, target, parser).

    The State Library answers only its first ten records, so with a located
    Russian ``title`` it is asked by author and title; and, since the
    located title is not known to be the same work, by author alone too.
    """
    queries = [rsl_author_query(name, title), rsl_author_query(name)] if title else [rsl_author_query(name)]

    def rsl_parser(query: str) -> Callable[[str], Any]:
        return lambda body: parse_rsl_author_answer(body, query, sources.today)

    requests: list[tuple[str, str, Callable[[str], Any]]] = [
        (SOURCE_RSL, query, rsl_parser(query)) for query in queries
    ]
    requests.append((SOURCE_LITRES, litres_search_url(name), parse_litres_search))
    return [request for request in requests if sources.usable(request[0])]


def _partial_author_answer(source: str, target: str, listed: Any) -> bool:
    """True for a State Library answer by author alone that was partial; a title-scoped answer never is."""
    return source == SOURCE_RSL and " AND title:(" not in target and bool(getattr(listed, "partial", False))


def _catalogue_entries(sources: _Sources, lookup: _Lookup, name: str, title: str) -> list[EditionEntry]:
    """The author's Russian-language editions in the State Library and on LitRes, by ``name``.

    A catalogue counts as answered for the book only when every request to
    it answered; a partial author-only State Library answer is noted
    (:attr:`_Lookup.partial`). An edition is kept when it is in Russian and a
    credited author is both ``name`` and the book's own author.
    """
    latin = _first_author(lookup.record)
    entries: list[EditionEntry] = []
    seen: set[tuple[str, str]] = set()
    failed: set[str] = set()
    requests = _catalogue_queries(sources, name, title)
    for source, target, parse in requests:
        try:
            listed = sources.ask(source, target, parse)
        except _SourceUnavailable:
            failed.add(source)
            continue
        if _partial_author_answer(source, target, listed):
            lookup.partial.add(source)
        for entry in listed:
            key = (entry.source, entry.url)
            if key not in seen and entry.language == "ru" and credits_author(entry, name, latin):
                seen.add(key)
                entries.append(entry)
    asked = {request[0] for request in requests}
    lookup.unanswered |= failed
    lookup.searched |= asked - failed
    return entries


def _spelling_key(name: str) -> str:
    return " ".join(name.casefold().replace("ё", "е").split())


def _spellings(*names: str) -> list[str]:
    """The distinct non-empty spellings, in order, at most two."""
    distinct: dict[str, str] = {}
    for name in names:
        if name:
            distinct.setdefault(_spelling_key(name), name)
    return list(distinct.values())[:2]


def _locate(record: Mapping[str, Any], sources: _Sources, lookup: _Lookup) -> LiveLibResult | None:
    title = lookup_title(record)
    if not sources.usable(SOURCE_LIVELIB) or not title:
        return None
    before = sources.made(SOURCE_LIVELIB)
    try:
        results = sources.ask(SOURCE_LIVELIB, livelib_search_url(title), parse_livelib_search)
    except _SourceUnavailable:
        lookup.unanswered.add(SOURCE_LIVELIB)
        return None
    finally:
        if sources.made(SOURCE_LIVELIB) > before:
            # A search was sent for this book, whatever came back.
            sources.report.learn(lookup.record, LIVELIB_SEARCH_DATE_FIELD, sources.today)
    lookup.searched.add(SOURCE_LIVELIB)
    return _kept_livelib(results, _first_author(record))


def look_up(record: dict[str, Any], sources: _Sources) -> _Lookup:
    """Ask the sources about one book; :class:`_BudgetExhausted` stops it."""
    lookup = _Lookup(record=record)
    author = _first_author(record)
    stored = stored_author_name(record)
    located = _locate(record, sources, lookup)
    published = cyrillic_name(located.author) if located is not None else ""
    lookup.published_name = published
    if not published and not stored and sources.usable(SOURCE_WIKIDATA) and author:
        try:
            lookup.wikidata_name = _wikidata_name(sources, author)
        except _SourceUnavailable:
            lookup.unanswered.add(SOURCE_WIKIDATA)
    names = _spellings(published, stored, lookup.wikidata_name)
    russian_title = russian_main_title(located.title) if located is not None else ""
    entries: list[EditionEntry] = []
    for name in names:
        entries = _catalogue_entries(sources, lookup, name, russian_title)
        if entries:
            lookup.author_name = name
            break
    if not lookup.author_name and names:
        lookup.author_name = names[0]
    if located is not None:
        entries.append(_livelib_entry(located))
    # Only editions that can pass the guards are numbered: the model never
    # picks one the guards would then reject while an eligible one is listed.
    lookup.groups = eligible_titles(
        entries,
        author_name=lookup.author_name,
        latin_author=author,
        first_year=first_published_year(record),
    )
    return lookup


# ── The pass ─────────────────────────────────────────────────────────────


def _ask_matcher(
    matcher: SameWorkMatcher, items: Sequence[Mapping[str, Any]], deadline: float, monotonic: Callable[[], float]
) -> Any:
    """One match call and its retry, each clamped to the budget left and never started below the floor."""
    answer = matcher.match(items, timeout=min(MATCH_CALL_TIMEOUT_SECONDS, deadline - monotonic()))
    for _ in range(MATCH_RETRIES):
        remaining = deadline - monotonic()
        if isinstance(answer, Mapping) or remaining < MIN_MATCH_SECONDS:
            break
        answer = matcher.match(items, timeout=min(MATCH_CALL_TIMEOUT_SECONDS, remaining))
    return answer


def _mark_attempt(record: dict[str, Any], today: date, report: CompletionReport) -> None:
    """Date the attempt on a book, completed or not: it orders the queue and never settles the book."""
    report.learn(record, ATTEMPT_DATE_FIELD, today.isoformat())


def _name_to_store(lookup: _Lookup, edition: Mapping[str, Any] | None) -> str:
    """The Cyrillic name this book teaches, or empty.

    A Wikidata name is kept whenever it was learned. A LiveLib spelling is
    kept only when the book ends ``confirmed`` with it as the author name,
    or ``unconfirmed`` on the LiveLib result it came from.
    """
    status = edition["status"] if edition is not None else None
    if status == STATUS_CONFIRMED:
        return lookup.author_name
    if status == STATUS_UNCONFIRMED:
        return cyrillic_name(edition["russian_author"]) if edition is not None else ""
    return lookup.wikidata_name


def _store(lookup: _Lookup, edition: Mapping[str, Any] | None, today: date, report: CompletionReport) -> None:
    """Write what one book learned onto its record; ``None`` writes no edition and no lookup date."""
    record = lookup.record
    name = cyrillic_name(_name_to_store(lookup, edition))
    # A stored name that is not the book's author is replaced, as if absent.
    if name and not stored_author_name(record):
        report.learn(record, AUTHOR_NAME_FIELD, name)
        report.author_name_learned += 1
    if edition is None:
        report.not_completed += 1
        return
    report.learn(record, RUSSIAN_EDITION_FIELD, dict(edition))
    report.learn(record, LOOKUP_DATE_FIELD, today.isoformat())
    count = str(edition["status"])
    setattr(report, count, getattr(report, count) + 1)


def _add_litres_detail(edition: dict[str, Any], group: TitleGroup, sources: _Sources) -> None:
    """Fill a confirmed LitRes edition's ISBN and publisher from its detail; on failure they stay empty."""
    evidence = next(
        (entry for entry in group.entries if entry.source == SOURCE_LITRES and entry.url == edition["evidence_url"]),
        None,
    )
    if evidence is None or not evidence.source_id:
        return
    try:
        detail = sources.ask(SOURCE_LITRES, litres_art_url(evidence.source_id), parse_litres_art)
    except (_SourceUnavailable, _BudgetExhausted):
        return
    edition["isbn"], edition["publisher"] = detail.isbn, detail.publisher


def _decide_book(
    lookup: _Lookup, answer: Mapping[str, Any] | None, sources: _Sources, today: date
) -> dict[str, Any] | None:
    """The record for one book, or ``None`` when it could not be completed tonight."""
    if not lookup.groups:
        return build_no_find(today) if lookup.complete else None
    if answer is None or _candidate_id(lookup.record) not in answer:
        return None
    number = answer[_candidate_id(lookup.record)]
    group = lookup.groups[number - 1] if isinstance(number, int) and 1 <= number <= len(lookup.groups) else None
    edition = decide(
        group,
        author_name=lookup.author_name,
        latin_author=_first_author(lookup.record),
        first_year=first_published_year(lookup.record),
        today=today,
    )
    if edition["status"] != STATUS_CONFIRMED:
        return edition if lookup.complete else None
    if group is not None:
        _add_litres_detail(edition, group, sources)
    return edition


def _valid_number(number: Any, count: int) -> bool:
    """True for ``None`` (none of them) or a number of one of the ``count`` titles shown."""
    return number is None or (isinstance(number, int) and not isinstance(number, bool) and 1 <= number <= count)


def _decide_chunk(
    lookups: Sequence[_Lookup],
    matcher: SameWorkMatcher | None,
    sources: _Sources,
    *,
    today: date,
    deadline: float,
    monotonic: Callable[[], float],
    report: CompletionReport,
) -> None:
    asked = [lookup for lookup in lookups if lookup.groups]
    answer: Any = None
    if asked and matcher is not None and deadline - monotonic() >= MIN_MATCH_SECONDS:
        items = [match_item(lookup.record, lookup.groups) for lookup in asked]
        raw = _ask_matcher(matcher, items, deadline, monotonic)
        if isinstance(raw, Mapping):
            counts = {item["candidate_id"]: len(item["russian_titles"]) for item in items}
            answer = {key: value for key, value in raw.items() if key in counts and _valid_number(value, counts[key])}
    # Every asked book without a validated answer: no answer at all, or one
    # that left the book out or named a number outside its list. A null is
    # an answer.
    report.match_unavailable += sum(
        1 for lookup in asked if answer is None or _candidate_id(lookup.record) not in answer
    )
    for lookup in lookups:
        _store(lookup, _decide_book(lookup, answer, sources, today), today, report)


def complete_russian_editions(
    records: Sequence[dict[str, Any]],
    *,
    fetcher: SourceFetcher | None,
    matcher: SameWorkMatcher | None,
    today: date,
    budget_seconds: float = STAGE_BUDGET_SECONDS,
    max_books: int = MAX_BOOKS,
    max_livelib_searches: int = MAX_LIVELIB_SEARCHES,
    enabled_sources: Sequence[str] = SOURCES,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    progress: Callable[[int, int], None] | None = None,
) -> CompletionReport:
    """Learn Russian editions for ``records`` (already ordered) in place and report what happened.

    ``progress``, when given, is told the books tried so far and the editions
    confirmed so far after each book is looked up and after each batch is
    decided. It only observes: it changes nothing the pass does.
    """
    report = CompletionReport()
    deadline = monotonic() + budget_seconds
    todo: list[dict[str, Any]] = []
    for record in records:
        report.considered += 1
        if _decided(record):
            report.skipped_decided += 1
        elif _is_settled(record, today):
            report.skipped_settled += 1
        elif len(todo) >= max_books:
            report.over_ceiling += 1
        else:
            todo.append(record)
    if fetcher is None:
        report.not_completed += len(todo)
        return report
    ceilings = {SOURCE_LIVELIB: max_livelib_searches}
    sources = _Sources(fetcher, enabled_sources, report, deadline, monotonic, sleeper, today, ceilings)
    tried = 0

    def tell() -> None:
        if progress is not None:
            progress(tried, report.confirmed)

    for start in range(0, len(todo), MATCH_BATCH_SIZE):
        chunk = todo[start : start + MATCH_BATCH_SIZE]
        lookups: list[_Lookup] = []
        try:
            for record in chunk:
                _mark_attempt(record, today, report)
                lookups.append(look_up(record, sources))
                tried += 1
                tell()
        except _BudgetExhausted:
            report.budget_exhausted += len(todo) - start - len(lookups)
            _decide_chunk(lookups, matcher, sources, today=today, deadline=deadline, monotonic=monotonic, report=report)
            tell()
            return report
        _decide_chunk(lookups, matcher, sources, today=today, deadline=deadline, monotonic=monotonic, report=report)
        tell()
    return report


# ── Production dependencies ──────────────────────────────────────────────


class LiveSourceFetcher:
    """Production fetcher: one plain request per call with the honest client name.

    The State Library goes through the existing session, token and POST of
    :func:`autoscout.russian_editions.rsl_live_search_response`; ``target``
    is then its query. Every other source gets a GET of ``target``.
    HTTP 401, 403 and 429 are blocks; every other failure is a failure,
    including any transport error from any source (a reset, a timeout, an
    incomplete read): it never ends the pass.
    """

    def fetch(self, source: str, target: str, *, timeout: float) -> SourceAnswer:
        try:
            return self._fetch(source, target, timeout=timeout)
        except (OSError, http.client.HTTPException, ValueError):
            return SourceAnswer(OUTCOME_FAILED)

    def _fetch(self, source: str, target: str, *, timeout: float) -> SourceAnswer:
        if source == SOURCE_RSL:
            from autoscout.russian_editions import rsl_live_search_response

            text, reason = rsl_live_search_response({"timeout_seconds": timeout, "user_agent": CLIENT_NAME}, target)
            if reason:
                return SourceAnswer(OUTCOME_BLOCKED if "blocked" in reason.lower() else OUTCOME_FAILED)
            return SourceAnswer(OUTCOME_ANSWERED, text)
        request = Request(target, headers={"User-Agent": CLIENT_NAME, "Accept": "text/html,application/json"})
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES)
                charset = response.headers.get_content_charset() or "utf-8"
        except HTTPError as error:
            return SourceAnswer(OUTCOME_BLOCKED if error.code in {401, 403, 429} else OUTCOME_FAILED)
        except (URLError, TimeoutError, OSError, ValueError):
            return SourceAnswer(OUTCOME_FAILED)
        return SourceAnswer(OUTCOME_ANSWERED, body.decode(charset, errors="replace"))


def _run_codex_subprocess(command: list[str], **kwargs: Any) -> Any:
    """Run the Codex CLI. Tests replace this seam entirely."""
    import subprocess

    return subprocess.run(command, **kwargs)


class CodexSameWorkMatcher:
    """Production matcher: one batched model call, no web search, built as :class:`CodexOriginalTitleProposer`.

    A timeout, a spawn failure, a non-zero exit, and an unparsable or
    schema-invalid response each answer ``None``: no exception escapes and
    no process output or path is stored or printed.
    """

    def __init__(
        self,
        timeout_seconds: float = MATCH_CALL_TIMEOUT_SECONDS,
        *,
        runner: Callable[..., Any] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        from autoscout.description_adjudication import _bounded_reasoning_effort
        from autoscout.original_edition_completion import _pinned_model_and_effort

        self._timeout_seconds = float(timeout_seconds)
        self._runner = runner or _run_codex_subprocess
        self._client = description_model_client()
        pinned_model, pinned_effort = _pinned_model_and_effort()
        self._model = str(pinned_model if model is None else model).strip()
        self._reasoning_effort = _bounded_reasoning_effort(
            pinned_effort if reasoning_effort is None else reasoning_effort
        )

    def match(self, items: Sequence[Mapping[str, Any]], *, timeout: float) -> dict[str, int | None] | None:
        payload = [{key: item[key] for key in MATCH_ITEM_FIELDS if key in item} for item in items]
        counts = {str(item.get("candidate_id", "")): len(item.get("russian_titles") or []) for item in payload}
        with tempfile.TemporaryDirectory(prefix="autoscout-russian-edition-") as temporary:
            isolated = Path(temporary)
            response_path = isolated / MATCH_RESPONSE_FILENAME
            (isolated / MATCH_ITEMS_FILENAME).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            schema_path = isolated / MATCH_SCHEMA_FILENAME
            schema_path.write_text(json.dumps(MATCH_RESPONSE_SCHEMA), encoding="utf-8")
            request = ModelRequest(
                prompt=MATCH_PROMPT,
                response_path=response_path,
                model=self._model,
                reasoning_effort=self._reasoning_effort,
                working_dir=isolated,
                output_schema_path=schema_path,
                isolated=True,
                input_files=(MATCH_ITEMS_FILENAME,),
                purpose="russian_edition_completion",
            )
            result = self._client.run(request, timeout=min(float(timeout), self._timeout_seconds), runner=self._runner)
            if result.status is not ModelCallStatus.ANSWERED:
                return None
        return validate_match_response(result.payload, counts)


# ── State files ──────────────────────────────────────────────────────────


def _copy_learned_fields(history_record: Mapping[str, Any], row: dict[str, Any]) -> bool:
    """Fill-only copy of the learned fields onto one snapshot row; True when the row changed."""
    copied = False
    edition = history_record.get(RUSSIAN_EDITION_FIELD)
    if validate_russian_edition(edition) and not is_confirmed(row.get(RUSSIAN_EDITION_FIELD)):
        if row.get(RUSSIAN_EDITION_FIELD) != edition:
            row[RUSSIAN_EDITION_FIELD] = copy.deepcopy(edition)
            copied = True
        if LOOKUP_DATE_FIELD in history_record and row.get(LOOKUP_DATE_FIELD) != history_record[LOOKUP_DATE_FIELD]:
            row[LOOKUP_DATE_FIELD] = history_record[LOOKUP_DATE_FIELD]
            copied = True
    # Only a name that is the book's author is projected, over a row's name that is not.
    name = stored_author_name(history_record)
    if name and not stored_author_name(row, _first_author(history_record)):
        row[AUTHOR_NAME_FIELD] = name
        copied = True
    for name in (ATTEMPT_DATE_FIELD, LIVELIB_SEARCH_DATE_FIELD):
        dated = history_record.get(name)
        if isinstance(dated, str) and dated and row.get(name) != dated:
            row[name] = dated
            copied = True
    return copied


def _has_confirmed_edition(record: Mapping[str, Any]) -> bool:
    return is_confirmed(record.get(RUSSIAN_EDITION_FIELD))


def _older_than_fresh(fresh: Mapping[str, Any], learned: Mapping[str, Any], name: str) -> bool:
    """True when Candidate History already holds a later date in ``name`` than this pass learned."""
    there, ours = _date_field(fresh, name), _date_field(learned, name)
    return there is not None and (ours is None or ours < there)


def _merge_into_fresh(fresh: dict[str, Any], learned: Mapping[str, Any]) -> None:
    """Merge what this pass learned for one record into the record as Candidate History holds it now.

    ``learned`` holds only the fields this pass set (:meth:`CompletionReport.learn`).
    The edition and its lookup date follow this pass unless history holds a
    confirmed edition or a later lookup; the attempt date unless history
    holds a later one, and so the LiveLib search date; the author name only fills a field that is empty or
    holds a name that is not the book's author. Nothing
    this pass did not set is written or removed.
    """
    edition = learned.get(RUSSIAN_EDITION_FIELD)
    if (
        validate_russian_edition(edition)
        and not is_confirmed(fresh.get(RUSSIAN_EDITION_FIELD))
        and not _older_than_fresh(fresh, learned, LOOKUP_DATE_FIELD)
    ):
        fresh[RUSSIAN_EDITION_FIELD] = copy.deepcopy(edition)
        if LOOKUP_DATE_FIELD in learned:
            fresh[LOOKUP_DATE_FIELD] = learned[LOOKUP_DATE_FIELD]
    name = cyrillic_name(learned.get(AUTHOR_NAME_FIELD))
    if name and not stored_author_name(fresh):
        fresh[AUTHOR_NAME_FIELD] = name
    for dated in (ATTEMPT_DATE_FIELD, LIVELIB_SEARCH_DATE_FIELD):
        if dated in learned and not _older_than_fresh(fresh, learned, dated):
            fresh[dated] = learned[dated]


_seal_finalization = seal_finalization

_LEARNED_FIELD_SPEC = LearnedFieldSpec(
    fields=LEARNED_FIELDS,
    already_filled=_has_confirmed_edition,
    copy_to_row=_copy_learned_fields,
    report_filename=REPORT_FILENAME,
    seal_pending_filename=_SEAL_PENDING_FILENAME,
    label="Russian-edition completion",
    project=lambda history, rows, aliases: project_russian_editions(history, rows, aliases),
    seal=lambda output_dir, store: _seal_finalization(output_dir, store),
    merge_record=_merge_into_fresh,
)


def project_russian_editions(
    history: Sequence[dict[str, Any]], rows: Sequence[Mapping[str, Any]], aliases: Mapping[str, Any] | None = None
) -> tuple[list[dict[str, Any]], int]:
    """Copy learned Russian editions from history onto snapshot rows; return the rows and how many changed."""
    return project_fields(history, rows, aliases, _LEARNED_FIELD_SPEC)


def run_russian_edition_completion(
    output_dir: Path,
    state_dir: Path,
    *,
    effective_date: str,
    scope: str = SCOPE_UNDECIDED,
    fetcher: SourceFetcher | None = None,
    matcher: SameWorkMatcher | None = None,
    dry_run: bool = False,
    publish_lock: Callable[[], Any] | None = None,
    weekly_run_check: Callable[[], bool] | None = None,
    budget_seconds: float = STAGE_BUDGET_SECONDS,
    max_books: int = MAX_BOOKS,
    max_livelib_searches: int = MAX_LIVELIB_SEARCHES,
    enabled_sources: Sequence[str] = SOURCES,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    state_store: StateStore | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Learn Russian editions for the selected records and persist them.

    ``scope="current"`` takes this run's candidates; ``scope="undecided"``
    every record without an editor decision. Decided books are skipped in
    both. History and the latest snapshot are written first, then the
    current snapshot, then, outside ``publish_lock``, the report file; a
    failure to write it is noted in the result (``report_file``), never
    raised. ``dry_run`` writes nothing.
    ``weekly_run_check`` is asked under the lock immediately before the
    first write; when it answers true nothing is written and the result is
    ``{"status": "refused", "reason": "weekly_run_in_progress"}``.
    ``progress`` is passed to :func:`complete_russian_editions`.
    """
    if scope not in {SCOPE_CURRENT, SCOPE_UNDECIDED}:
        raise ValueError(f"unknown scope: {scope}")
    unknown = set(enabled_sources) - set(SOURCES)
    if unknown:
        raise ValueError(f"unknown sources: {sorted(unknown)}")
    if not set(enabled_sources) & set(CATALOGUE_SOURCES):
        # Without the State Library or LitRes nothing can be confirmed.
        raise ValueError("no catalogue source enabled: enable rsl or litres")
    today = date.fromisoformat(effective_date)
    store = state_store if state_store is not None else open_state_store(state_dir)
    history = read_history(store)
    if history is None:
        return {"status": "input_failed", "reason": "invalid_candidate_history"}
    current = read_json_or_default(output_dir / "scouting-candidates.json", [])
    current = current if isinstance(current, list) else []
    aliases = load_aliases(store)
    selected = order_records(select_records(history, scope, current, aliases), current, history, aliases)
    report = complete_russian_editions(
        selected,
        fetcher=fetcher,
        matcher=matcher,
        today=today,
        budget_seconds=budget_seconds,
        max_books=max_books,
        max_livelib_searches=max_livelib_searches,
        enabled_sources=enabled_sources,
        monotonic=monotonic,
        sleeper=sleeper,
        progress=progress,
    )
    result: dict[str, Any] = {
        "status": "ok",
        "scope": scope,
        "dry_run": dry_run,
        "enabled_sources": [source for source in SOURCES if source in set(enabled_sources)],
        **report.as_dict(),
    }
    if dry_run:
        return result
    try:
        persist_learned_fields(
            output_dir,
            store,
            spec=_LEARNED_FIELD_SPEC,
            # Only what this pass set: a value the selected record already held
            # may be older than what another writer stored meanwhile.
            selected=[{"identity_key": key, **fields} for key, fields in report.learned.items()],
            touched=report.touched,
            changed=bool(report.touched),
            result=result,
            effective_date=effective_date,
            publish_lock=_guarded_lock(publish_lock, weekly_run_check),
            # The report file is a diagnostic: written outside the lock (the
            # weekly run's report transaction), and a failure to write it
            # never stops the run.
            report_outside_lock=True,
        )
    except _WeeklyRunStarted:
        return {"status": "refused", "reason": WEEKLY_RUN_IN_PROGRESS}
    return result


# ── The weekly stage ─────────────────────────────────────────────────────


class _KeptAliveMatcher:
    """A matcher whose call runs on a worker while the caller keeps the progress snapshot fresh.

    A model call can take ``MATCH_CALL_TIMEOUT_SECONDS`` and be retried once,
    longer than the doctor's stale-snapshot threshold. The call is waited on
    in slices, as the description stages wait on theirs
    (:meth:`autoscout.progress_snapshot.CandidateProgressSnapshot.collect`),
    and ``keepalive`` is called between slices; the writer throttles the
    file and the operator's heartbeat lines as it does for every publish.
    The answer and any exception are the matcher's own.
    """

    def __init__(
        self, matcher: SameWorkMatcher, keepalive: Callable[[], None], *, tick_seconds: float = KEEPALIVE_TICK_SECONDS
    ) -> None:
        self._matcher = matcher
        self._keepalive = keepalive
        self._tick_seconds = tick_seconds

    def match(self, items: Sequence[Mapping[str, Any]], *, timeout: float) -> dict[str, int | None] | None:
        from autoscout.bounded_workers import DaemonThreadPool

        pool = DaemonThreadPool(1, thread_name_prefix="autoscout-russian-edition-match-")
        try:
            future = pool.submit(self._matcher.match, items, timeout=timeout)
            while not future.done():
                try:
                    future.result(timeout=self._tick_seconds)
                except TimeoutError:
                    # An expired slice, or the worker's own TimeoutError: done() tells them apart.
                    if future.done():
                        break
                    self._keepalive()
            answer: dict[str, int | None] | None = future.result()
            return answer
        finally:
            pool.shutdown(wait=False)


class ProductionRussianEditionCompletionStage:
    """The live weekly stage: every undecided book, the live sources, the Codex matcher, the heartbeat.

    Built as :class:`autoscout.original_edition_completion.ProductionOriginalEditionCompletionStage`.
    The weekly workflow starts it only when the weekly config's switch is on
    (:mod:`autoscout.russian_edition_settings`) and names its config through
    :meth:`set_config_path`, from which the stage reads its limits. It writes
    through the run's State Store; the report transaction the workflow
    hands it as ``publish_lock`` is taken only around the write, never
    during the lookups. It passes no ``weekly_run_check``: it is the weekly
    run. The workflow may cap its budget at the time left in the run. The
    heartbeat counts books tried and editions confirmed, never a title, and
    the snapshot stays fresh during a model call.
    """

    def __init__(
        self,
        fetcher_factory: Callable[[], SourceFetcher | None] = LiveSourceFetcher,
        matcher_factory: Callable[[], SameWorkMatcher | None] = CodexSameWorkMatcher,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._fetcher_factory = fetcher_factory
        self._matcher_factory = matcher_factory
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._config_path: Path | None = None
        self._presentation: Callable[[dict[str, Any]], None] | None = None
        self._failure_recorder: Callable[[str, str], None] | None = None

    def set_config_path(self, config_path: Path | None) -> None:
        self._config_path = config_path

    def set_snapshot_progress_presentation(self, presentation: Callable[[dict[str, Any]], None] | None) -> None:
        self._presentation = presentation

    def set_progress_snapshot_failure_recorder(self, recorder: Callable[[str, str], None] | None) -> None:
        self._failure_recorder = recorder

    def _settings(self) -> WeeklySettings:
        if self._config_path is None:
            return WeeklySettings()
        config = read_json(Path(self._config_path))
        if not isinstance(config, Mapping):
            raise ValueError("the weekly config is not a JSON object")
        return weekly_settings(config)

    def complete(
        self,
        output_dir: Path,
        state_dir: Path,
        *,
        effective_date: str,
        state_store: StateStore | None = None,
        publish_lock: Callable[[], Any] | None = None,
        budget_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Run the pass; ``budget_seconds``, when given, caps the configured budget (the run's time left)."""
        from autoscout.progress_snapshot import ProgressSnapshotWriter

        try:
            settings = self._settings()
        except (OSError, ValueError):
            # The config validator names the key; the run goes on without this stage.
            return {"status": "failed", "reason": INVALID_WEEKLY_CONFIG}
        budget = STAGE_BUDGET_SECONDS if settings.budget_seconds is None else settings.budget_seconds
        if budget_seconds is not None:
            budget = min(budget, budget_seconds)
        started = self._monotonic()
        writer = ProgressSnapshotWriter(
            output_dir,
            monotonic=self._monotonic,
            failure_recorder=self._failure_recorder,
            presentation=self._presentation,
        )

        counts = {"tried": 0, "found": 0}

        def rows_now(*, running: bool) -> list[dict[str, Any]]:
            # One row per book tried, the first ``found`` of them marked; no title, author or source text.
            rows: list[dict[str, Any]] = [
                {"source_name": "Russian editions", "state": "finished", "found": index < counts["found"]}
                for index in range(counts["tried"])
            ]
            if running:
                # The pass is at work for as long as it runs; the heartbeat counts finished books only.
                rows.append({"source_name": "Russian editions", "state": "in_flight"})
            return rows

        def publish(*, running: bool) -> None:
            writer.publish(
                stage=STAGE_NAME,
                status="running" if running else "finished",
                elapsed_seconds=self._monotonic() - started,
                rows=rows_now(running=running),
                stage_budget_seconds=budget,
            )

        def on_progress(tried: int, found: int) -> None:
            counts.update(tried=tried, found=found)
            publish(running=True)

        def keepalive() -> None:
            # Not forced: the file is rewritten at the writer's interval, the heartbeat at its own.
            writer.publish(
                stage=STAGE_NAME,
                status="running",
                elapsed_seconds=self._monotonic() - started,
                rows=rows_now(running=True),
                force=False,
                stage_budget_seconds=budget,
            )

        publish(running=True)
        matcher = self._matcher_factory()
        result = run_russian_edition_completion(
            output_dir,
            state_dir,
            effective_date=effective_date,
            scope=SCOPE_UNDECIDED,
            fetcher=self._fetcher_factory(),
            matcher=None if matcher is None else _KeptAliveMatcher(matcher, keepalive),
            publish_lock=publish_lock,
            budget_seconds=budget,
            max_books=MAX_BOOKS if settings.max_books is None else settings.max_books,
            max_livelib_searches=(
                MAX_LIVELIB_SEARCHES if settings.max_livelib_searches is None else settings.max_livelib_searches
            ),
            enabled_sources=settings.enabled_sources,
            monotonic=self._monotonic,
            sleeper=self._sleeper,
            state_store=state_store,
            progress=on_progress,
        )
        publish(running=False)
        return result
