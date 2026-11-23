"""Complete the original edition of translated books (#968).

A book found only on a non-English bestseller list is often a translation:
«Ahora que se acerca el final» by Diana Athill is *Somewhere Towards the End*.
This pass records, per Candidate History record, the grounded original when
Open Library confirms a model's proposal. Every selected record is asked:

0. A book with an ISBN found on a German, French or Spanish list is first
   looked up in that country's national library
   (:mod:`autoscout.library_catalogue`), when the library holds that ISBN's
   range, with the requests to one library spaced as its robots file asks.
   A record that states a translation with its original language answers
   the book without a model call: a stated original title goes to the
   Open Library confirmation below, and an original Open Library does not
   confirm (or a record without an original title) is stored as
   ``library_stated`` with the library record as evidence. A record silent
   about translation, no record, or an unreachable library is no answer, and
   the book goes on to the steps below. A library that keeps failing is not
   asked again in the same pass. The record's page count is stored when the
   book has none.
0b. A book on a Spanish list with an ISBN that no library answered with an
   original language is looked up on the booksellers' portal
   (:mod:`autoscout.bookseller_portal`, issue #1140), under the ceilings and
   the switch CEGAL's permission sets. An ISBN already asked is answered
   from the portal ledger without a request. A page whose «Idioma original»
   is a language other than Spanish and the book's list languages states the
   translation: the page is the credit, and its language goes to the model
   as a fact, so the model is asked only for the original title. When the
   model has no title or Open Library does not confirm it, the book is stored
   as ``library_stated`` with the portal page as evidence. The page's page
   count is stored when the book has none. «Castellano», no page, or a
   portal failure is no answer, and the book goes on as before.
1. A translator credit on a bookshop or publisher page already fetched for
   the book's description (:func:`autoscout.original_edition.translator_credit`)
   is looked up as extra evidence, and it sets ``credit_url`` and
   ``stated_original_title``. A record without a credit, or without evidence
   pages, still goes on: most bookshop pages name no translator, and the
   English original's own page is often the description page.
2. A model proposes the original title and its ISO 639-1 language for every
   record, from the edition's metadata, in bounded batches
   (:class:`CodexOriginalTitleProposer`). Each item says whether a credit was
   found (``translator_credited``).
3. Open Library must confirm the proposed original before it is stored
   (:func:`autoscout.original_edition.confirm_original`). Confirmation is
   mandatory; a credit never replaces it.

Only a confirmed original becomes ``original_edition``; a proposal Open
Library does not confirm is stored as ``not_found``. A confirmed original in
another language counts as proof of translation, with the credit page as its
evidence when there is one and the Open Library page otherwise. A book with
no credit and no confirmed original stays ``is_translation: unknown``. When
the confirmed original is English, the record also gets a sourced published
English title through the existing title-evidence guard, so a helper
rendering or a translated title never stands where a publication title is
proven.

Candidate History and the latest candidate snapshot are written first, in one
State Store transaction of the shared learned-field envelope, fill-only; the
current candidates then receive the same fields. ``dry_run`` writes nothing. Every step is a pure function of history
and of the lookups, so an interruption recovers by running the pass again.
"""

from __future__ import annotations

import copy
import json
import subprocess
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
from urllib.parse import urlsplit

from autoscout.bookseller_portal import MODE_WEEKLY
from autoscout.bookseller_portal import LivePortalFetch
from autoscout.bookseller_portal import PortalAnswer
from autoscout.bookseller_portal import PortalDeadline
from autoscout.bookseller_portal import PortalLedger
from autoscout.bookseller_portal import PortalLookup
from autoscout.bookseller_portal import config_enabled as portal_enabled
from autoscout.codex_model_pin import ModelPinError
from autoscout.codex_model_pin import load_description_model_pin
from autoscout.description_adjudication import _bounded_reasoning_effort
from autoscout.isbn_completion import BoundedPageGetter
from autoscout.isbn_completion import PageGetter
from autoscout.isbn_completion import isbn13
from autoscout.json_io import read_json_or_default
from autoscout.learned_field_completion import SCOPE_CURRENT
from autoscout.learned_field_completion import SCOPE_UNDECIDED
from autoscout.learned_field_completion import LearnedFieldSpec
from autoscout.learned_field_completion import load_aliases
from autoscout.learned_field_completion import persist_learned_fields
from autoscout.learned_field_completion import project_fields
from autoscout.learned_field_completion import read_history
from autoscout.learned_field_completion import seal_finalization
from autoscout.learned_field_completion import select_records
from autoscout.library_catalogue import LIBRARY_CATALOGUES
from autoscout.library_catalogue import LibraryCatalogue
from autoscout.library_catalogue import LibraryRecord
from autoscout.library_catalogue import best_record
from autoscout.model_client import ModelCallStatus
from autoscout.model_client import ModelRequest
from autoscout.model_client import description_model_client
from autoscout.original_edition import OPEN_LIBRARY_LANGUAGE
from autoscout.original_edition import PROPOSAL_RESPONSE_SCHEMA
from autoscout.original_edition import STATUS_CONFIRMED
from autoscout.original_edition import STATUS_LIBRARY_STATED
from autoscout.original_edition import build_original_edition
from autoscout.original_edition import confirm_original
from autoscout.original_edition import library_statement
from autoscout.original_edition import open_library_search_url
from autoscout.original_edition import translator_credit
from autoscout.original_edition import validate_original_edition
from autoscout.original_edition import validate_proposal_response
from autoscout.state_store import StateStore
from autoscout.state_store import open_state_store
from autoscout.title_evidence import TITLE_TYPE_SOURCED_PUBLISHED
from autoscout.title_evidence import is_valid_sourced_published_title
from autoscout.title_evidence import record_has_only_non_english_sources

STAGE_NAME = "original-edition-completion"
REPORT_FILENAME = "original-edition-completion.json"
_SEAL_PENDING_FILENAME = ".original-edition-completion-seal-pending.json"

# A book whose lookup missed is asked again after four weeks: translations
# keep appearing in catalogues, and a weekly retry would ask the model and
# Open Library about the same misses again.
RETRY_AFTER_DAYS = 28
# Lookups (evidence pages, the model, Open Library) get a bounded wall clock.
STAGE_BUDGET_SECONDS = 15 * 60.0
# Only the pages already used for the description are read for a credit, and
# the first credit found decides.
MAX_EVIDENCE_PAGES = 3
# One proposal call carries a bounded batch, so the prompt and the response
# stay small and a failed call loses at most this many books.
PROPOSAL_BATCH_SIZE = 10
PROPOSAL_CALL_TIMEOUT_SECONDS = 120.0
# A library that failed this many times in a row is not asked again in the
# same pass: a library that is down or slow never eats the stage budget.
LIBRARY_FAILURE_LIMIT = 3
# A stalled proposal call usually succeeds on retry, so each batch gets one more try.
PROPOSAL_RETRIES = 1
# A proposal call (or its retry) is started only with this much stage budget
# left; a shorter call would only spend the budget on a timeout.
MIN_PROPOSAL_SECONDS = 30.0

PROPOSAL_PROMPT = (
    "Read items.json. It holds books that were found on non-English bestseller lists. "
    "Some were written in the list's language; some are translations. For each book, if it "
    "is a translation, give the title and ISO 639-1 language code of the original work it was "
    "translated from, exactly as that original work was published. The original title is never "
    "a translation of the edition's title into another language.\n"
    "If the book was written in the language of its list, or you do not know confidently, "
    "answer null for both fields.\n"
    "translator_credited true means the edition's page names a translator. When "
    "stated_original_title is given, use it as the original title.\n"
    "When original_language is given, a bookseller's catalogue states that the book was "
    "translated from that language: give the original title in that language, and answer "
    "original_language with that code.\n"
    "Never guess.\n"
    "Return one entry per candidate_id, echoing the exact candidate_id from the item.\n"
)

PROPOSAL_ITEMS_FILENAME = "items.json"
PROPOSAL_SCHEMA_FILENAME = "response-schema.json"
PROPOSAL_RESPONSE_FILENAME = "response.json"

ORIGINAL_EDITION_FIELD = "original_edition"
LOOKUP_DATE_FIELD = "original_edition_lookup_date"
ENGLISH_TITLE_FIELDS = ("english_title", "english_title_type", "english_title_provenance")
# The edition's page count and the library record it came from (#1139).
PAGE_COUNT_FIELDS = ("page_count", "page_count_source_url")
LEARNED_FIELDS = (ORIGINAL_EDITION_FIELD, LOOKUP_DATE_FIELD, *ENGLISH_TITLE_FIELDS, *PAGE_COUNT_FIELDS)

RESULT_COUNTS = (
    "considered",
    "answered_by_library",
    "answered_by_model",
    "answered_by_portal",
    "library_stated",
    "portal_stated",
    "page_count_applied",
    "no_credit",
    "credited",
    "no_proposal",
    "proposal_unavailable",
    "confirmed",
    "not_found",
    "lookup_unavailable",
    "english_title_applied",
    "english_title_not_applied",
    "skipped_recent",
    "budget_exhausted",
)


class Proposer(Protocol):
    def propose(self, items: Sequence[Mapping[str, Any]], *, timeout: float) -> dict[str, dict[str, str]] | None: ...


class _Progress(Protocol):
    def initial(self, items: list[tuple[str, str]]) -> None: ...
    def begin(self, key: str) -> None: ...
    def finish(self, key: str) -> None: ...
    def final(self, *, status: str = "finished") -> None: ...


class _BudgetExhausted(Exception):
    """The stage deadline passed before the next request."""


class _LookupUnavailable(Exception):
    """The catalogue gave no usable answer (timeout, HTTP error, malformed JSON)."""


# ── Selection ────────────────────────────────────────────────────────────


def _recently_looked_up(record: Mapping[str, Any], today: date) -> bool:
    try:
        looked_up = date.fromisoformat(str(record.get(LOOKUP_DATE_FIELD, "")))
    except ValueError:
        return False
    return today - looked_up < timedelta(days=RETRY_AFTER_DAYS)


def _is_settled(record: Mapping[str, Any], today: date) -> bool:
    """True when the record needs no lookup: its original is confirmed or recently checked."""
    edition = record.get(ORIGINAL_EDITION_FIELD)
    if not isinstance(edition, dict) or not validate_original_edition(edition):
        return False
    if str(edition.get("status", "")) == STATUS_CONFIRMED:
        return True
    return _recently_looked_up(record, today)


def _candidate_id(record: Mapping[str, Any]) -> str:
    return str(record.get("candidate_id") or record.get("identity_key", ""))


def _first_author(record: Mapping[str, Any]) -> str:
    authors = record.get("primary_authors")
    if isinstance(authors, list):
        return str(next((author for author in authors if str(author).strip()), "") or "")
    return str(authors or "")


def _authors(record: Mapping[str, Any]) -> list[str]:
    authors = record.get("primary_authors")
    if isinstance(authors, list):
        return [str(author) for author in authors if str(author).strip()]
    return [str(authors)] if authors else []


def _source_languages(record: Mapping[str, Any]) -> list[str]:
    """Distinct non-blank language codes declared by the record's source evidence."""
    evidence = record.get("source_evidence")
    if not isinstance(evidence, list):
        return []
    languages: list[str] = []
    for entry in evidence:
        if not isinstance(entry, Mapping):
            continue
        language = str(entry.get("language", "") or "").strip()
        if language and language not in languages:
            languages.append(language)
    return languages


def _is_http_url(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme in {"http", "https"} and bool(parts.hostname)


def _evidence_urls(record: Mapping[str, Any]) -> list[str]:
    """The first bounded evidence page URLs already used for the description."""
    provenance = record.get("description_provenance")
    basis = provenance.get("evidence_basis") if isinstance(provenance, Mapping) else None
    if not isinstance(basis, list):
        return []
    urls: list[str] = []
    for value in basis:
        if not _is_http_url(value) or value in urls:
            continue
        urls.append(value)
        if len(urls) >= MAX_EVIDENCE_PAGES:
            break
    return urls


# ── Lookups ──────────────────────────────────────────────────────────────


def _fetch(
    url: str,
    getter: PageGetter,
    deadline: float,
    monotonic: Callable[[], float],
    cache: dict[str, str | None],
) -> str | None:
    if url in cache:
        return cache[url]
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise _BudgetExhausted
    body = getter.get(url, timeout=remaining)
    if body is None and monotonic() >= deadline:
        # The request ran into the deadline: cut short, not a miss.
        raise _BudgetExhausted
    cache[url] = body
    return body


def _translator_credit(
    urls: Sequence[str],
    getter: PageGetter,
    deadline: float,
    monotonic: Callable[[], float],
    cache: dict[str, str | None],
) -> tuple[str, str]:
    """The ``(credit_url, stated_original_title)`` of the first crediting page, else ``("", "")``."""
    for url in urls:
        body = _fetch(url, getter, deadline, monotonic, cache)
        if body is None:
            continue
        credit = translator_credit(body)
        if credit is not None:
            return url, credit.stated_original_title
    return "", ""


class _LibraryHealth:
    """Consecutive failures per library in one pass, the libraries no longer asked, and request spacing."""

    def __init__(
        self,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._failures: dict[str, int] = {}
        self._last_request: dict[str, float] = {}
        self._monotonic = monotonic
        self._sleeper = sleeper

    def pace(self, library: LibraryCatalogue, deadline: float) -> None:
        """Wait out the library's request spacing; a wait past the deadline stops the pass instead."""
        last = self._last_request.get(library.key)
        now = self._monotonic()
        if last is not None:
            wait = last + library.min_request_interval_seconds - now
            if wait > 0:
                if now + wait >= deadline:
                    raise _BudgetExhausted
                self._sleeper(wait)
        self._last_request[library.key] = self._monotonic()

    def available(self, library: LibraryCatalogue) -> bool:
        return self._failures.get(library.key, 0) < LIBRARY_FAILURE_LIMIT

    def failed(self, library: LibraryCatalogue) -> None:
        self._failures[library.key] = self._failures.get(library.key, 0) + 1

    def answered(self, library: LibraryCatalogue) -> None:
        self._failures[library.key] = 0


def _library_record(
    record: Mapping[str, Any],
    libraries: Sequence[LibraryCatalogue],
    health: _LibraryHealth,
    getter: PageGetter,
    deadline: float,
    monotonic: Callable[[], float],
    cache: dict[str, str | None],
    report: CompletionReport,
) -> LibraryRecord | None:
    """The best library record for the book's ISBN, or ``None`` when no library has one.

    Only the libraries serving one of the book's list languages and holding
    its ISBN range are asked. An
    unreachable library or an unusable answer is counted for that library
    alone and is no answer.
    """
    isbn = isbn13(record.get("isbn"))
    if not isbn:
        return None
    languages = _source_languages(record)
    found: LibraryRecord | None = None
    for library in libraries:
        if not library.serves(languages) or not library.holds(isbn):
            continue
        if not health.available(library):
            report.library_unavailable[library.key] = report.library_unavailable.get(library.key, 0) + 1
            continue
        url = library.search_url(isbn)
        if url not in cache:
            health.pace(library, deadline)
        body = _fetch(url, getter, deadline, monotonic, cache)
        records = library.parse(body) if body is not None else None
        if records is None:
            health.failed(library)
            report.library_unavailable[library.key] = report.library_unavailable.get(library.key, 0) + 1
            continue
        health.answered(library)
        best = best_record(records)
        if best is None:
            continue
        if best.states_translation:
            return best
        found = found or best
    return found


def _library_original_language(answer: LibraryRecord | None, record: Mapping[str, Any]) -> str:
    """The original language a library record states, or empty when it states no usable one.

    A language of the book's own lists proves no translation from another
    language, so it is no answer.
    """
    if answer is None or not answer.states_translation:
        return ""
    language = answer.original_language.strip().casefold()
    if language not in OPEN_LIBRARY_LANGUAGE:
        return ""
    if language in {code.strip().casefold() for code in _source_languages(record)}:
        return ""
    return language


def _apply_page_count(record: dict[str, Any], answer: LibraryRecord | None) -> bool:
    """Store the library record's page count when the book has none; True when it was stored."""
    if answer is None or answer.page_count is None or record.get(PAGE_COUNT_FIELDS[0]) is not None:
        return False
    record[PAGE_COUNT_FIELDS[0]] = answer.page_count
    record[PAGE_COUNT_FIELDS[1]] = answer.record_url
    return True


def _portal_answer(record: Mapping[str, Any], portal: PortalLookup | None, deadline: float) -> PortalAnswer | None:
    """The portal's answer for a book on a Spanish list with an ISBN, or ``None``."""
    if portal is None:
        return None
    isbn = isbn13(record.get("isbn"))
    if not isbn or "es" not in {language.casefold() for language in _source_languages(record)}:
        return None
    try:
        return portal.answer(isbn, deadline)
    except PortalDeadline:
        raise _BudgetExhausted from None


def _apply_portal_page_count(record: dict[str, Any], answer: PortalAnswer | None) -> bool:
    """Store the portal page's page count when the book has none; True when it was stored."""
    if answer is None or answer.page_count is None or record.get(PAGE_COUNT_FIELDS[0]) is not None:
        return False
    record[PAGE_COUNT_FIELDS[0]] = answer.page_count
    record[PAGE_COUNT_FIELDS[1]] = answer.page_url
    return True


def _proposal_item(
    record: Mapping[str, Any], stated_original_title: str, translator_credited: bool, original_language: str = ""
) -> dict[str, Any]:
    """One book for the model: the edition's metadata and, from the portal, at most its original language.

    ``original_language`` is the one fact from the booksellers' portal that
    reaches the model (#1140); the key is there only when the portal gave it.
    """
    item: dict[str, Any] = {
        "candidate_id": _candidate_id(record),
        "title": str(record.get("title", "") or ""),
        "authors": _authors(record),
        "publisher": str(record.get("publisher", "") or ""),
        "publication_year": record.get("publication_year"),
        "source_languages": _source_languages(record),
        "stated_original_title": stated_original_title,
        "translator_credited": translator_credited,
    }
    if original_language:
        item["original_language"] = original_language
    return item


def _usable_proposal(proposal: Any) -> Mapping[str, Any] | None:
    if not isinstance(proposal, Mapping):
        return None
    title = proposal.get("original_title")
    language = proposal.get("original_language")
    if not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(language, str) or not language.strip():
        return None
    return proposal


def _proposed_in_source_language(proposal: Mapping[str, Any], record: Mapping[str, Any]) -> bool:
    """True when the proposal's language is one of the record's source languages."""
    language = str(proposal.get("original_language", "")).strip().casefold()
    if not language:
        return False
    return language in {code.strip().casefold() for code in _source_languages(record)}


def _open_library_docs(
    proposal: Mapping[str, Any],
    record: Mapping[str, Any],
    getter: PageGetter,
    deadline: float,
    monotonic: Callable[[], float],
    cache: dict[str, str | None],
) -> list[Any]:
    url = open_library_search_url(str(proposal.get("original_title", "")), _first_author(record))
    body = _fetch(url, getter, deadline, monotonic, cache)
    try:
        response = json.loads(body) if body is not None else None
    except ValueError:
        response = None
    if not isinstance(response, Mapping) or not isinstance(response.get("docs"), list):
        # No answer is not a "no": the catalogue was unreachable or answered
        # garbage, so the record stays eligible for the next run.
        raise _LookupUnavailable
    return list(response["docs"])


# ── English title ────────────────────────────────────────────────────────


def _sourced_english_title(record: Mapping[str, Any], edition: Mapping[str, Any]) -> dict[str, Any] | None:
    """The sourced published English title fields, or ``None`` when they do not validate."""
    title = str(edition.get("original_title", "") or "")
    # Without a translator credit the translation evidence is the confirmation
    # page itself; title provenance rejects a duplicated source URL.
    research_sources: list[str] = []
    for url in (
        str(edition.get("confirmation_url", "") or ""),
        str(edition.get("translation_evidence_url", "") or ""),
    ):
        if url and url not in research_sources:
            research_sources.append(url)
    provenance = {
        "kind": TITLE_TYPE_SOURCED_PUBLISHED,
        "research_sources": research_sources,
        "mapping": {
            "original_title": record.get("title", ""),
            "original_authors": record.get("primary_authors", []),
            "published_title": title,
            "published_authors": record.get("primary_authors", []),
        },
    }
    fields = {
        "english_title": title,
        "english_title_type": TITLE_TYPE_SOURCED_PUBLISHED,
        "english_title_provenance": provenance,
    }
    candidate = {**dict(record), **fields}
    if not is_valid_sourced_published_title(candidate):
        return None
    return fields


def _apply_english_title(record: dict[str, Any], edition: Mapping[str, Any]) -> bool | None:
    """Set the sourced English title when a confirmed English original proves it.

    Returns ``None`` when the step does not apply (not a confirmed English
    original, or the record already holds a sourced published title), ``True``
    when the fields were set, and ``False`` when the new fields do not pass
    the title-evidence guard, in which case the old helper stays untouched.
    """
    if str(edition.get("status", "")) != STATUS_CONFIRMED:
        return None
    if str(edition.get("original_language", "")) != "en":
        return None
    if is_valid_sourced_published_title(record):
        return None
    fields = _sourced_english_title(record, edition)
    if fields is None:
        return False
    record.update(fields)
    return True


# ── The pass ─────────────────────────────────────────────────────────────


@dataclass
class CompletionReport:
    considered: int = 0
    answered_by_library: int = 0
    answered_by_model: int = 0
    answered_by_portal: int = 0
    library_stated: int = 0
    portal_stated: int = 0
    page_count_applied: int = 0
    no_credit: int = 0
    credited: int = 0
    no_proposal: int = 0
    proposal_unavailable: int = 0
    confirmed: int = 0
    not_found: int = 0
    lookup_unavailable: int = 0
    english_title_applied: int = 0
    english_title_not_applied: int = 0
    skipped_recent: int = 0
    budget_exhausted: int = 0
    # Identity keys of the records this pass changed: only these are merged
    # back into Candidate History.
    touched: set[str] = field(default_factory=set)
    # Library key → how many books that library could not answer because it
    # was unreachable, answered garbage or had failed too often (#1139).
    library_unavailable: dict[str, int] = field(default_factory=dict)
    # What the booksellers' portal did (#1140): counts only; empty when it was not used.
    portal: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        counts: dict[str, Any] = {name: int(getattr(self, name)) for name in RESULT_COUNTS}
        counts["library_unavailable"] = dict(sorted(self.library_unavailable.items()))
        counts["portal"] = dict(self.portal)
        return counts


@dataclass
class _Selected:
    """A selected record awaiting a proposal; ``credit_url`` is empty without a credit.

    ``library_language`` is set when a library record answered the book: the
    record is then the credit, and no model is asked. ``portal_language`` is
    set when the booksellers' portal page at ``portal_url`` states the
    original language: the model is asked for the title only.
    """

    record: dict[str, Any]
    credit_url: str
    stated_original_title: str
    library_language: str = ""
    portal_language: str = ""
    portal_url: str = ""


def complete_originals(
    records: Sequence[dict[str, Any]],
    *,
    getter: PageGetter | None,
    proposer: Proposer | None,
    today: date,
    budget_seconds: float = STAGE_BUDGET_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    progress: _Progress | None = None,
    libraries: Sequence[LibraryCatalogue] = LIBRARY_CATALOGUES,
    sleeper: Callable[[float], None] = time.sleep,
    portal: PortalLookup | None = None,
) -> CompletionReport:
    """Learn original editions for ``records`` in place and report what happened.

    ``getter=None`` and ``proposer=None`` make the pass learn nothing: a
    record that needs a page or a proposal is counted ``lookup_unavailable``
    or ``proposal_unavailable`` and stays eligible for the next run.

    The records that need a lookup are worked in chunks of
    ``PROPOSAL_BATCH_SIZE``. Each chunk runs its credit lookups, its proposal
    call and its Open Library confirmations before the next chunk starts, so
    what a chunk learned is stored even when a later chunk meets the deadline.

    ``portal`` is the booksellers' portal lookup (#1140); ``None`` leaves it out.
    """
    report = CompletionReport()
    deadline = monotonic() + budget_seconds
    cache: dict[str, str | None] = {}
    health = _LibraryHealth(monotonic, sleeper)
    todo: list[dict[str, Any]] = []
    for record in records:
        report.considered += 1
        if _is_settled(record, today):
            report.skipped_recent += 1
        else:
            todo.append(record)
    if progress is not None:
        progress.initial([(_candidate_id(record), str(record.get("title", ""))) for record in todo])
    # Once the budget stops the pass, every later chunk is still walked so its
    # records reach the progress heartbeat as finished; none of them is worked.
    stopped = False
    for start in range(0, len(todo), PROPOSAL_BATCH_SIZE):
        chunk = todo[start : start + PROPOSAL_BATCH_SIZE]
        chunk_keys = [_candidate_id(record) for record in chunk]
        if progress is not None:
            for key in chunk_keys:
                progress.begin(key)
        if stopped:
            report.budget_exhausted += len(chunk)
        elif deadline_passed(deadline, monotonic):
            report.budget_exhausted += len(chunk)
            stopped = True
        elif getter is None:
            # The Open Library step needs a page getter for every selected
            # record, with or without a credit.
            report.lookup_unavailable += len(chunk)
        else:
            unreached = _run_chunk(
                chunk,
                progress,
                getter=getter,
                proposer=proposer,
                libraries=libraries,
                health=health,
                portal=portal,
                today=today,
                deadline=deadline,
                monotonic=monotonic,
                cache=cache,
                report=report,
            )
            if unreached:
                report.budget_exhausted += unreached
                stopped = True
        if progress is not None:
            for key in chunk_keys:
                progress.finish(key)
    if progress is not None:
        progress.final()
    if portal is not None:
        report.portal = portal.stats.as_dict()
    return report


def _run_chunk(chunk: Sequence[dict[str, Any]], progress: _Progress | None, **kwargs: Any) -> int:
    """Learn one chunk, polling the heartbeat while a slow call blocks.

    With a tracker that can ``collect``, the chunk runs on a daemon worker and
    the tracker is polled while it works (the pattern of issue #706 and the
    PM logline stage). Without one, the chunk runs directly.
    """
    collect = getattr(progress, "collect", None)
    if progress is None or not callable(collect):
        return _complete_chunk(chunk, **kwargs)
    from autoscout.bounded_workers import DaemonThreadPool

    pool = DaemonThreadPool(1, thread_name_prefix="autoscout-original-edition-")
    try:
        return int(collect([pool.submit(_complete_chunk, chunk, **kwargs)])[0])
    finally:
        pool.shutdown(wait=False)


def _complete_chunk(
    chunk: Sequence[dict[str, Any]],
    *,
    getter: PageGetter,
    proposer: Proposer | None,
    libraries: Sequence[LibraryCatalogue],
    health: _LibraryHealth,
    portal: PortalLookup | None,
    today: date,
    deadline: float,
    monotonic: Callable[[], float],
    cache: dict[str, str | None],
    report: CompletionReport,
) -> int:
    """Learn one chunk of records; return how many of them the budget stopped short.

    Records the budget stopped before their library or credit lookup, their
    proposal or their Open Library confirmation count as ``budget_exhausted``
    and are not stored. Every other record of the chunk reaches its outcome.
    The books a library answered are confirmed first and never reach the
    model.
    """
    answered: list[_Selected] = []
    pending: list[_Selected] = []
    unreached = 0
    for index, record in enumerate(chunk):
        if deadline_passed(deadline, monotonic):
            unreached = len(chunk) - index
            break
        try:
            library = _library_record(record, libraries, health, getter, deadline, monotonic, cache, report)
            if _apply_page_count(record, library):
                report.page_count_applied += 1
                report.touched.add(str(record.get("identity_key", "")))
            library_language = _library_original_language(library, record)
            if library is not None and library_language:
                report.answered_by_library += 1
                answered.append(
                    _Selected(
                        record=record,
                        credit_url=library.record_url,
                        stated_original_title=library.original_title,
                        library_language=library_language,
                    )
                )
                continue
            answer = _portal_answer(record, portal, deadline)
            if _apply_portal_page_count(record, answer):
                report.page_count_applied += 1
                report.touched.add(str(record.get("identity_key", "")))
            portal_language = answer.original_language(_source_languages(record)) if answer is not None else ""
            portal_url = answer.page_url if answer is not None and portal_language else ""
            if library is not None and library.states_translation:
                # A translation without a usable original language is a
                # credit: the library record is its page.
                credit_url, stated = library.record_url, library.original_title
            elif portal_language:
                # The portal page states the translation: it is the credit.
                credit_url, stated = portal_url, ""
            else:
                credit_url, stated = _translator_credit(_evidence_urls(record), getter, deadline, monotonic, cache)
        except _BudgetExhausted:
            unreached = len(chunk) - index
            break
        # A credit is extra evidence only: with or without one, the record is
        # asked about below.
        if credit_url:
            report.credited += 1
        else:
            report.no_credit += 1
        if portal_language:
            report.answered_by_portal += 1
        pending.append(
            _Selected(
                record=record,
                credit_url=credit_url,
                stated_original_title=stated,
                portal_language=portal_language,
                portal_url=portal_url,
            )
        )
    if answered:
        library_work = [(item, _library_proposal(item)) for item in answered]
        stopped = _confirm_work(library_work, getter, deadline, monotonic, cache, today, report)
        if stopped:
            return unreached + stopped + len(pending)
    if not pending:
        return unreached
    if deadline - monotonic() < MIN_PROPOSAL_SECONDS:
        return unreached + len(pending)
    if proposer is None:
        report.proposal_unavailable += len(pending)
        return unreached
    # One proposal call answers the chunk's bounded batch; a chunk whose call
    # failed leaves its records untouched, so they are asked again next run.
    items = [
        _proposal_item(item.record, item.stated_original_title, bool(item.credit_url), item.portal_language)
        for item in pending
    ]
    answer = _ask_proposer(proposer, items, deadline, monotonic)
    if not isinstance(answer, Mapping):
        report.proposal_unavailable += len(pending)
        return unreached
    work: list[tuple[_Selected, Mapping[str, Any] | None]] = []
    for item in pending:
        proposal = _usable_proposal(answer.get(_candidate_id(item.record)))
        if proposal is not None and item.portal_language:
            # The portal's original language is a stated fact; the model
            # only proposes the title.
            proposal = {**proposal, "original_language": item.portal_language}
        # A proposal in a language the lists use says the book was not
        # translated from another language, so it proves no translation.
        # It is dropped, and the catalogue never confirms the edition as
        # its own original.
        if proposal is not None and _proposed_in_source_language(proposal, item.record):
            proposal = None
        if proposal is not None:
            report.answered_by_model += 1
        work.append((item, proposal))
    return unreached + _confirm_work(work, getter, deadline, monotonic, cache, today, report)


def _library_proposal(item: _Selected) -> Mapping[str, Any] | None:
    """The library's stated original as a proposal for Open Library, or ``None`` without a title."""
    if not item.stated_original_title.strip():
        return None
    return {"original_title": item.stated_original_title, "original_language": item.library_language}


def _library_stated(item: _Selected) -> dict[str, Any]:
    return build_original_edition(
        credit_url=item.credit_url,
        confirmation=library_statement(
            original_title=item.stated_original_title, original_language=item.library_language
        ),
    )


def _portal_stated(item: _Selected) -> dict[str, Any]:
    """The portal page's statement: a translation from its language, the page as evidence (#1140)."""
    return build_original_edition(
        credit_url=item.portal_url,
        confirmation=library_statement(
            original_title=item.stated_original_title, original_language=item.portal_language
        ),
    )


def _stated_edition(item: _Selected) -> tuple[dict[str, Any], str] | None:
    """The edition a library or the portal stated, with the count it adds to; ``None`` when neither did."""
    if item.library_language:
        return _library_stated(item), "library_stated"
    if item.portal_language:
        return _portal_stated(item), "portal_stated"
    return None


def _ask_proposer(
    proposer: Proposer,
    items: Sequence[Mapping[str, Any]],
    deadline: float,
    monotonic: Callable[[], float],
) -> Any:
    """One proposal call and its retry, each clamped to the budget left and never started below the floor."""
    answer = proposer.propose(items, timeout=min(PROPOSAL_CALL_TIMEOUT_SECONDS, deadline - monotonic()))
    for _ in range(PROPOSAL_RETRIES):
        remaining = deadline - monotonic()
        if isinstance(answer, Mapping) or remaining < MIN_PROPOSAL_SECONDS:
            break
        answer = proposer.propose(items, timeout=min(PROPOSAL_CALL_TIMEOUT_SECONDS, remaining))
    return answer


def _confirm_work(
    work: Sequence[tuple[_Selected, Mapping[str, Any] | None]],
    getter: PageGetter,
    deadline: float,
    monotonic: Callable[[], float],
    cache: dict[str, str | None],
    today: date,
    report: CompletionReport,
) -> int:
    """Confirm each proposal with Open Library; return how many records the budget stopped short."""
    for index, (item, proposal) in enumerate(work):
        record = item.record
        stated = _stated_edition(item)
        if proposal is None and stated is not None:
            # A library or the portal stated the language but there is no
            # original title: nothing to confirm, and the statement is the answer.
            record[ORIGINAL_EDITION_FIELD], count = stated
            record[LOOKUP_DATE_FIELD] = today.isoformat()
            report.touched.add(str(record.get("identity_key", "")))
            setattr(report, count, getattr(report, count) + 1)
            continue
        if proposal is None:
            # No usable proposal: the core's not-found confirmation, never a
            # hand-built block, and a lookup date so the book rests a while.
            record[ORIGINAL_EDITION_FIELD] = build_original_edition(
                credit_url=item.credit_url, confirmation=confirm_original({}, record, [])
            )
            record[LOOKUP_DATE_FIELD] = today.isoformat()
            report.touched.add(str(record.get("identity_key", "")))
            report.no_proposal += 1
            continue
        if deadline_passed(deadline, monotonic):
            return len(work) - index
        try:
            docs = _open_library_docs(proposal, record, getter, deadline, monotonic, cache)
        except _BudgetExhausted:
            return len(work) - index
        except _LookupUnavailable:
            report.lookup_unavailable += 1
            continue
        confirmation = confirm_original(proposal, record, docs)
        # A confirmed original in another language is proof of translation: the
        # credit page is its evidence when there is one, else the Open Library
        # page. An unconfirmed book without a credit stays is_translation unknown.
        evidence_url = item.credit_url or (
            confirmation["confirmation_url"] if confirmation["status"] == STATUS_CONFIRMED else ""
        )
        edition = build_original_edition(credit_url=evidence_url, confirmation=confirmation)
        stated_count = ""
        if stated is not None and edition["status"] != STATUS_CONFIRMED:
            # Open Library did not confirm the original: the library's or
            # the portal's statement stands, with its page as evidence.
            edition, stated_count = stated
        record[ORIGINAL_EDITION_FIELD] = edition
        record[LOOKUP_DATE_FIELD] = today.isoformat()
        report.touched.add(str(record.get("identity_key", "")))
        if str(edition.get("status", "")) == STATUS_CONFIRMED:
            report.confirmed += 1
        elif edition["status"] == STATUS_LIBRARY_STATED:
            count = stated_count or "library_stated"
            setattr(report, count, getattr(report, count) + 1)
        else:
            report.not_found += 1
        applied = _apply_english_title(record, edition)
        if applied is True:
            report.english_title_applied += 1
        elif applied is False:
            report.english_title_not_applied += 1
    return 0


def deadline_passed(deadline: float, monotonic: Callable[[], float]) -> bool:
    """True when the stage deadline has passed."""
    return monotonic() >= deadline


# ── Model proposal ───────────────────────────────────────────────────────


def _run_codex_subprocess(command: list[str], **kwargs: Any) -> Any:
    """Run the Codex CLI. Tests replace this seam entirely."""
    return subprocess.run(command, **kwargs)


def _pinned_model_and_effort() -> tuple[str, str]:
    """The repository's description pin, or two empty strings when it cannot be read.

    Stage 0 stops the weekly run on an unreadable pin before any stage
    starts, so the empty answer here only keeps construction from raising.
    """
    try:
        pin = load_description_model_pin()
    except ModelPinError:
        return "", ""
    return pin.model, pin.reasoning_effort


class CodexOriginalTitleProposer:
    """Production proposer: one batched Codex CLI call, no web search.

    The command shape mirrors the description adjudication adapter, minus
    web search: the model reads bounded metadata only, because the edition's
    own pages prove the translation and Open Library proves the original.
    A temporary isolated directory holds the items file, the response schema
    and the response file. A timeout, a spawn failure, a non-zero exit, and
    an unparsable or schema-invalid response each answer ``None``: no
    exception escapes and no process output or path is stored or printed.
    """

    def __init__(
        self,
        timeout_seconds: float = PROPOSAL_CALL_TIMEOUT_SECONDS,
        *,
        runner: Callable[..., Any] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self._timeout_seconds = float(timeout_seconds)
        self._runner = runner or _run_codex_subprocess
        self._client = description_model_client()
        pinned_model, pinned_effort = _pinned_model_and_effort()
        self._model = str(pinned_model if model is None else model).strip()
        self._reasoning_effort = _bounded_reasoning_effort(
            pinned_effort if reasoning_effort is None else reasoning_effort
        )

    def propose(self, items: Sequence[Mapping[str, Any]], *, timeout: float) -> dict[str, dict[str, str]] | None:
        payload = [dict(item) for item in items]
        requested = [str(item.get("candidate_id", "")) for item in payload]
        with tempfile.TemporaryDirectory(prefix="autoscout-original-edition-") as temporary:
            isolated = Path(temporary)
            items_path = isolated / PROPOSAL_ITEMS_FILENAME
            schema_path = isolated / PROPOSAL_SCHEMA_FILENAME
            response_path = isolated / PROPOSAL_RESPONSE_FILENAME
            items_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            schema_path.write_text(json.dumps(PROPOSAL_RESPONSE_SCHEMA), encoding="utf-8")
            request = ModelRequest(
                prompt=PROPOSAL_PROMPT,
                response_path=response_path,
                model=self._model,
                reasoning_effort=self._reasoning_effort,
                working_dir=isolated,
                output_schema_path=schema_path,
                isolated=True,
                # The Claude API backend sends the items in the request (#1149).
                input_files=(PROPOSAL_ITEMS_FILENAME,),
                purpose="original_edition_completion",
            )
            # The caller's budget-clamped timeout is the call's limit; the
            # constructor's timeout is only a ceiling on it.
            call_timeout = min(float(timeout), self._timeout_seconds)
            result = self._client.run(request, timeout=call_timeout, runner=self._runner)
            if result.status is not ModelCallStatus.ANSWERED:
                return None
        return validate_proposal_response(result.payload, requested)


# ── State files ──────────────────────────────────────────────────────────


def _confirmed(edition: Any) -> bool:
    return (
        isinstance(edition, dict)
        and validate_original_edition(edition)
        and str(edition.get("status", "")) == STATUS_CONFIRMED
    )


def _copy_learned_fields(history_record: Mapping[str, Any], row: dict[str, Any]) -> bool:
    """Fill-only copy of the learned fields onto one snapshot row.

    Only a value that actually differs is copied, and the row is reported as
    changed only then: an unchanged row must not rewrite its snapshot file or
    re-seal the finalization marker.
    """
    copied = False
    edition = history_record.get(ORIGINAL_EDITION_FIELD)
    if validate_original_edition(edition) and not _confirmed(row.get(ORIGINAL_EDITION_FIELD)):
        if row.get(ORIGINAL_EDITION_FIELD) != edition:
            row[ORIGINAL_EDITION_FIELD] = copy.deepcopy(edition)
            copied = True
        if LOOKUP_DATE_FIELD in history_record and row.get(LOOKUP_DATE_FIELD) != history_record[LOOKUP_DATE_FIELD]:
            row[LOOKUP_DATE_FIELD] = history_record[LOOKUP_DATE_FIELD]
            copied = True
    if is_valid_sourced_published_title(dict(history_record)) and not is_valid_sourced_published_title(dict(row)):
        for field_name in ENGLISH_TITLE_FIELDS:
            if field_name in history_record and row.get(field_name) != history_record[field_name]:
                row[field_name] = copy.deepcopy(history_record[field_name])
                copied = True
    if _copy_page_count(history_record, row):
        copied = True
    return copied


def _copy_page_count(source: Mapping[str, Any], target: dict[str, Any]) -> bool:
    """Fill-only copy of the page count and its source; True when it was copied."""
    if source.get(PAGE_COUNT_FIELDS[0]) is None or target.get(PAGE_COUNT_FIELDS[0]) is not None:
        return False
    for field_name in PAGE_COUNT_FIELDS:
        if field_name in source:
            target[field_name] = copy.deepcopy(source[field_name])
    return True


def _has_confirmed_original_edition(record: Mapping[str, Any]) -> bool:
    """True when the record already holds a confirmed original edition."""
    return _confirmed(record.get(ORIGINAL_EDITION_FIELD))


def _sourced_by_this_pass(record: Mapping[str, Any]) -> bool:
    """True when the record's sourced English title cites its own confirmation page.

    That is the shape :func:`_sourced_english_title` gives a title this pass
    set; no separate stored marker is needed.
    """
    edition = record.get(ORIGINAL_EDITION_FIELD)
    confirmation_url = str(edition.get("confirmation_url", "") or "") if isinstance(edition, Mapping) else ""
    provenance = record.get("english_title_provenance")
    sources = provenance.get("research_sources") if isinstance(provenance, Mapping) else None
    return (
        bool(confirmation_url)
        and isinstance(sources, list)
        and confirmation_url in sources
        and is_valid_sourced_published_title(dict(record))
    )


def _merge_into_fresh(fresh: dict[str, Any], source: Mapping[str, Any]) -> None:
    """Merge one completed record into its record as Candidate History holds it now (#968).

    The original edition and its lookup date always follow this pass. The
    English title moves only when this pass set it and the fresh record does
    not already hold a sourced published title, so a title another writer put
    there during the lookups is neither overwritten nor removed.
    """
    for field_name in (ORIGINAL_EDITION_FIELD, LOOKUP_DATE_FIELD):
        if field_name in source:
            fresh[field_name] = copy.deepcopy(source[field_name])
        else:
            fresh.pop(field_name, None)
    if _sourced_by_this_pass(source) and not is_valid_sourced_published_title(dict(fresh)):
        for field_name in ENGLISH_TITLE_FIELDS:
            fresh[field_name] = copy.deepcopy(source[field_name])
    _copy_page_count(source, fresh)


# The envelope's seal step, kept as this module's own name: a patched
# ``_seal_finalization`` still intercepts it.
_seal_finalization = seal_finalization

_LEARNED_FIELD_SPEC = LearnedFieldSpec(
    fields=LEARNED_FIELDS,
    already_filled=_has_confirmed_original_edition,
    copy_to_row=_copy_learned_fields,
    report_filename=REPORT_FILENAME,
    seal_pending_filename=_SEAL_PENDING_FILENAME,
    label="original edition completion",
    # The envelope's two interruptible steps are named here through this
    # module's own globals, so a patched ``project_original_editions`` or
    # ``_seal_finalization`` still intercepts them.
    project=lambda history, rows, aliases: project_original_editions(history, rows, aliases),
    seal=lambda output_dir, store: _seal_finalization(output_dir, store),
    merge_record=_merge_into_fresh,
)


def project_original_editions(
    history: Sequence[dict[str, Any]], rows: Sequence[Mapping[str, Any]], aliases: Mapping[str, Any] | None = None
) -> tuple[list[dict[str, Any]], int]:
    """Copy learned original editions from history onto snapshot rows; return the rows and how many changed."""
    return project_fields(history, rows, aliases, _LEARNED_FIELD_SPEC)


def run_original_edition_completion(
    output_dir: Path,
    state_dir: Path,
    *,
    effective_date: str,
    scope: str = SCOPE_UNDECIDED,
    getter: PageGetter | None = None,
    proposer: Proposer | None = None,
    dry_run: bool = False,
    publish_lock: Callable[[], Any] | None = None,
    budget_seconds: float = STAGE_BUDGET_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    progress: _Progress | None = None,
    state_store: StateStore | None = None,
    portal: PortalLookup | None = None,
) -> dict[str, Any]:
    """Learn original editions for the selected records and persist them.

    ``scope="current"`` takes this run's candidates; ``scope="undecided"``
    takes every record without an editor decision, narrowed to the records
    found only on non-English lists. History and the latest snapshot are
    written first, through *state_store* (the run's store) or a store opened
    over *state_dir*, then the current snapshot, then the report.
    ``dry_run`` writes nothing.
    """
    if scope not in {SCOPE_CURRENT, SCOPE_UNDECIDED}:
        raise ValueError(f"unknown scope: {scope}")
    today = date.fromisoformat(effective_date)
    store = state_store if state_store is not None else open_state_store(state_dir)
    history = read_history(store)
    if history is None:
        return {"status": "input_failed", "reason": "invalid_candidate_history"}
    current_path = output_dir / "scouting-candidates.json"
    current = read_json_or_default(current_path, [])
    aliases = load_aliases(store)
    selected = [
        record
        for record in select_records(history, scope, current if isinstance(current, list) else [], aliases)
        if record_has_only_non_english_sources(record)
    ]
    report = complete_originals(
        selected,
        getter=getter,
        proposer=proposer,
        today=today,
        budget_seconds=budget_seconds,
        monotonic=monotonic,
        progress=progress,
        portal=portal,
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


def weekly_portal_lookup(
    config_path: Path | None,
    state_dir: Path,
    *,
    fetch_factory: Callable[[], Any] = LivePortalFetch,
    today: date | None = None,
) -> PortalLookup | None:
    """The booksellers' portal lookup of a weekly pass, or ``None`` when the weekly config switches it off (#1140).

    Weekly lookups count against the weekly ceiling, dated by the real clock.
    """
    if not portal_enabled(config_path):
        return None
    return PortalLookup(
        PortalLedger.in_state_dir(state_dir),
        fetch_factory(),
        mode=MODE_WEEKLY,
        today=today or date.today(),
    )


class ProductionOriginalEditionCompletionStage:
    """The live weekly stage: this run's candidates, the bounded page getter, the Codex proposer, the heartbeat.

    The weekly workflow names its config through :meth:`set_config_path`, so
    the stage can read the portal switch; without a config the portal is off.
    """

    def __init__(
        self,
        getter_factory: Callable[[], PageGetter | None] = BoundedPageGetter,
        proposer_factory: Callable[[], Proposer | None] = CodexOriginalTitleProposer,
        portal_fetch_factory: Callable[[], Any] = LivePortalFetch,
    ) -> None:
        self._getter_factory = getter_factory
        self._proposer_factory = proposer_factory
        self._portal_fetch_factory = portal_fetch_factory
        self._config_path: Path | None = None
        self._presentation: Callable[[dict[str, Any]], None] | None = None
        self._failure_recorder: Callable[[str, str], None] | None = None

    def set_config_path(self, config_path: Path | None) -> None:
        self._config_path = config_path

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
        return run_original_edition_completion(
            output_dir,
            state_dir,
            effective_date=effective_date,
            scope=SCOPE_CURRENT,
            getter=self._getter_factory(),
            proposer=self._proposer_factory(),
            progress=tracker,
            state_store=state_store,
            portal=weekly_portal_lookup(self._config_path, state_dir, fetch_factory=self._portal_fetch_factory),
        )


__all__ = [
    "LEARNED_FIELDS",
    "LIBRARY_FAILURE_LIMIT",
    "MAX_EVIDENCE_PAGES",
    "PROPOSAL_BATCH_SIZE",
    "PROPOSAL_CALL_TIMEOUT_SECONDS",
    "PROPOSAL_PROMPT",
    "PROPOSAL_RESPONSE_SCHEMA",
    "REPORT_FILENAME",
    "RESULT_COUNTS",
    "RETRY_AFTER_DAYS",
    "STAGE_BUDGET_SECONDS",
    "STAGE_NAME",
    "BoundedPageGetter",
    "CodexOriginalTitleProposer",
    "CompletionReport",
    "ProductionOriginalEditionCompletionStage",
    "Proposer",
    "complete_originals",
    "project_original_editions",
    "run_original_edition_completion",
    "weekly_portal_lookup",
]
