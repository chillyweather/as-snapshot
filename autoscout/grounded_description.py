"""Fail-closed, source-grounded description stage.

The stage is deliberately separate from Candidate History and from the legacy
Codex description path.  It accepts only bounded evidence that passed the
universal identity and subject agreement gate.  Discovery, fetching, and
synthesis are injected seams so the contract can be tested without a model or
network.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from autoscout.description_stage_limits import STAGE_BUDGET_SECONDS as STAGE_BUDGET_SECONDS
from autoscout.description_stage_limits import description_stage_limits
from autoscout.enrichment_lifecycle import BAND_NEW
from autoscout.enrichment_lifecycle import BAND_UPDATED
from autoscout.enrichment_lifecycle import lifecycle_band
from autoscout.repository_root import repository_root

if TYPE_CHECKING:
    from autoscout.source_owner import OwnerPolicy
    from autoscout.state_store import StateStore

import inspect
import itertools
import json
import math
import re
import threading
import time
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from enum import Enum
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from typing import Protocol
from typing import cast
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import parse_qsl
from urllib.parse import quote_plus
from urllib.parse import unquote
from urllib.parse import urlencode
from urllib.parse import urlsplit
from urllib.parse import urlunsplit
from urllib.request import HTTPRedirectHandler
from urllib.request import Request
from urllib.request import build_opener

from autoscout.autobiographical_context import _SUBJECT_CONTEXT_FIELD_MAX_CHARS
from autoscout.bounded_workers import DaemonThreadPool
from autoscout.candidate_identity import KeyedHistory
from autoscout.candidate_identity import resolve_candidate_id
from autoscout.candidate_identity import resolve_history_match
from autoscout.content_description import ENGLISH_DESCRIPTION_FIELD
from autoscout.content_description import description_provenance_record
from autoscout.content_description import validate_content_description
from autoscout.content_description import validate_english_description
from autoscout.description_depth import DEPTH_MIN_CHARS
from autoscout.description_depth import DEPTH_MIN_SENTENCES
from autoscout.description_depth import assess_description_depth
from autoscout.description_depth import meets_description_depth
from autoscout.enrichment_lifecycle import lifecycle_band_rank
from autoscout.http_decoding import read_bounded_http_bytes
from autoscout.identity import AUTHOR_LIST_SEPARATOR
from autoscout.identity import identity_isbn
from autoscout.identity import normalize_author_display
from autoscout.identity import normalize_author_identity
from autoscout.identity import normalize_identity_text
from autoscout.identity import normalize_isbn
from autoscout.identity import normalize_work_title
from autoscout.json_io import read_json_or_default
from autoscout.json_io import write_json
from autoscout.language_codes import normalize_language_code
from autoscout.pm_publication import is_pm_url
from autoscout.progress_snapshot import CandidateProgressSnapshot
from autoscout.progress_snapshot import ProgressSnapshotFailureRecorder
from autoscout.progress_snapshot import ProgressSnapshotPresentation
from autoscout.title_evidence import title_is_identity_evidence
from autoscout.vendor_rate_limit import MIN_VENDOR_INTERVAL_SECONDS
from autoscout.vendor_rate_limit import VendorRateLimiter

STAGE_VERSION = "grounded-description-v1"

# Measured on 2026-08-23 over the live repair run for the books that fell back
# on 2026-08-22: discovery 5.4 to 14.8 seconds, page fetching 4.3 to 11.5
# seconds, and synthesis 23.7 to 27.1 seconds. The worst candidate needed 52.3
# seconds and completed inside the 90-second candidate envelope.
#
# The envelope must fit the slowest stage in sequence, not the median. The
# bound still holds: three workers share the stage budget, and a candidate
# that stalls is still cut off (issue #563). Admission no longer reserves
# the full envelope (issue #633).
CANDIDATE_ENVELOPE_SECONDS = 90.0

# Measured on the 2026-09-03 weekly run: per-candidate totals were 4.9 s
# median, 8.8 s mean, 23.5 s at p95, and 25.7 s maximum. This is the seed
# reserve only. It carries admission until the current run has samples of
# its own.
#
# A fixed constant cannot carry admission alone. That run's mean is low
# because 92 percent of its page rejections were fast off-allowlist
# refusals, and the number excludes the shared vendor queue wait, which the
# stage clock still pays. Real cost per candidate rises as soon as more
# pages reach synthesis. Admission therefore reserves the larger of this
# seed and the observed p95 of the candidates this run already finished
# (issue #633).
MEASURED_CANDIDATE_COST_SECONDS = 9.0

# The seed reserve carries admission until the run has this many finished
# candidates. Below it one slow outlier would distort the observed p95.
ADMISSION_SAMPLE_FLOOR = 5

REQUEST_INACTIVITY_TIMEOUT_SECONDS = 10.0
# Discovery has its own bounded phase so a slow search cannot consume the
# post-discovery candidate envelope. Keep the default tied to the transport's
# inactivity limit, while allowing deterministic tests and local adapters to
# choose a smaller bound.
DISCOVERY_BUDGET_SECONDS = REQUEST_INACTIVITY_TIMEOUT_SECONDS
MAX_WORKERS = 3
MAX_FETCHES_PER_CANDIDATE = 3
# The full-title query, plus one main-title query only when the full title with
# its subtitle found nothing (issue #708). Both share the discovery deadline.
MAX_DISCOVERY_REQUESTS_PER_CANDIDATE = 2

# An eligible candidate receives at most two discovery requests, then up to
# three source requests, including challenge retries. The shipped policy list makes several
# candidates use the same retailer. Three workers run in parallel, so a
# retailer can answer with a bot check. Spacing requests per host removes the
# burst, while the shared request budget keeps retries inside the canonical
# 90-second envelope.
MIN_HOST_INTERVAL_SECONDS = 1.0

# One retry is enough for an observed challenge: the second request after the
# spacing interval has returned the real page in every reproduction.
PAGE_CHALLENGE_ATTEMPTS = 2
MAX_RESPONSE_BYTES = 1_000_000

MAX_EVIDENCE_METADATA_TEXT = 500
# Text of the page's first author link, kept in memory only to confirm that a
# page prints an author (issue #709). It is never written to an artifact.
MAX_AUTHOR_EVIDENCE_TEXT = 200
# The page's visible text, kept in memory only so adjudication can find a
# cited book's ISBN, title, or author surname wherever the page prints it
# (issue #791). It is never written to an artifact.
MAX_IDENTITY_EVIDENCE_TEXT = 200_000
MAX_SOURCE_SUBJECT_TEXT = 1_500
SOURCE_SUBJECT_SEPARATOR = "; subject="
MAX_SYNTHESIS_EVIDENCE_TEXT = MAX_EVIDENCE_METADATA_TEXT + MAX_SOURCE_SUBJECT_TEXT + len(SOURCE_SUBJECT_SEPARATOR)
# Compatibility alias for callers that used the pre-#493 metadata budget.
MAX_EVIDENCE_TEXT = MAX_EVIDENCE_METADATA_TEXT
MAX_DISPLAY_TEXT = 300
MAX_DISCOVERY_TITLE_TEXT = 300
MAX_URL_LENGTH = 500
MAX_METADATA_KEYS = 20
MAX_METADATA_KEY_LENGTH = 100
MAX_METADATA_VALUE_LENGTH = 300
MAX_TITLE_EVIDENCE_DEPTH = 4

SOURCE_INDEPENDENCE_REASON = "distinct_canonical_urls_owner_ids"

PREFLIGHT_HEALTHY = "discovery_adapter_healthy"
PREFLIGHT_ENDPOINT_NOT_CONFIGURED = "discovery_endpoint_not_configured"
PREFLIGHT_ADAPTER_UNAVAILABLE = "discovery_adapter_unavailable"
PREFLIGHT_ADAPTER_NOT_RUNNING = "discovery_adapter_not_running"
PREFLIGHT_ADAPTER_NO_RESPONSE = "discovery_adapter_no_response"
PREFLIGHT_ADAPTER_KEY_MISSING = "discovery_adapter_key_missing"
PREFLIGHT_ADAPTER_VENDOR_ERROR = "discovery_adapter_vendor_error"

OUTCOME_GROUNDED = "grounded"
OUTCOME_SINGLE_SOURCE = "single_source"
OUTCOME_ATTEMPTED_NO_DESCRIPTION = "attempted_no_description"
OUTCOME_NOT_ATTEMPTED = "not_attempted"
OUTCOME_EVIDENCE_UNCHANGED = "evidence_unchanged"
OUTCOME_ORDER = (
    OUTCOME_GROUNDED,
    OUTCOME_SINGLE_SOURCE,
    OUTCOME_ATTEMPTED_NO_DESCRIPTION,
    OUTCOME_NOT_ATTEMPTED,
    OUTCOME_EVIDENCE_UNCHANGED,
)
OUTCOME_CLASSES = frozenset(OUTCOME_ORDER)

REASON_NO_RESULTS = "no_results"
REASON_SEARCH_OR_FETCH_FAILURE = "search_or_fetch_failure"
# The source classifier refused the page: its host is on the denylist of
# configs/source-owner-policy.json, or it is a Publishers Marketplace deal
# page, which describes a rights deal rather than the book (issue #947).
# Owner derivation admits every other host, so this is the only refusal the
# classifier can make (issues #689, #691).
REASON_SOURCE_DENYLISTED = "source_denylisted"
REASON_FEWER_THAN_TWO = "fewer_than_two_agreeing_sources"
REASON_TITLE_MISMATCH = "title_mismatch"
REASON_AUTHOR_MISMATCH = "author_mismatch"
REASON_SOURCE_IDENTITY_DISAGREEMENT = "source_identity_disagreement"
REASON_SUBJECT_DISAGREEMENT = "subject_content_disagreement"
REASON_INSUFFICIENT_SUBJECT = "insufficient_subject_text"
REASON_TRUNCATED_PAGE_NO_EVIDENCE = "truncated_page_no_evidence"
# A page that answered 200 but carried no book evidence at all: no
# schema.org record, no author, and no subject text. A retailer bot check
# has exactly this shape, and it is transient, so the fetcher retries it.
# The retry consumes another slot from the shared three-request fetch budget.
REASON_PAGE_CHALLENGE_NO_EVIDENCE = "page_challenge_no_evidence"
REASON_AMBIGUITY = "ambiguity"
# The agent found the book announced or offered for rights but not yet
# published, and the record carries nothing that says otherwise (issue #791).
REASON_NOT_YET_PUBLISHED = "work_not_yet_published"
REASON_NOT_ATTEMPTED_STAGE_BUDGET = "not_attempted_stage_budget"
REASON_CANDIDATE_BUDGET_EXHAUSTED = "candidate_budget_exhausted"
REASON_SEARCH_SNIPPET = "search_snippet"
REASON_INVALID_SOURCE = "invalid_source"
REASON_SYNTHESIS_FAILURE = "synthesis_failure"
REASON_INVALID_DESCRIPTION = "invalid_description"
# The synthesizer returned safe Russian prose that clears neither the
# character nor the sentence floor of the shared depth policy (issue
# #673). A below-floor synthesis is never a successful grounded outcome;
# the attempt is reported and the retained description (if any) survives.
REASON_INSUFFICIENT_DEPTH = "insufficient_description_depth"
REASON_EVIDENCE_UNCHANGED = "evidence_unchanged"
REASON_STAGE_EXECUTION_FAILURE = "stage_execution_failure"

REASON_AUTHOR_UNVERIFIABLE = "author_unverifiable"

REASON_CODES = frozenset(
    {
        REASON_NO_RESULTS,
        REASON_SEARCH_OR_FETCH_FAILURE,
        REASON_SOURCE_DENYLISTED,
        REASON_FEWER_THAN_TWO,
        REASON_TITLE_MISMATCH,
        REASON_AUTHOR_MISMATCH,
        REASON_AUTHOR_UNVERIFIABLE,
        REASON_SOURCE_IDENTITY_DISAGREEMENT,
        REASON_SUBJECT_DISAGREEMENT,
        REASON_INSUFFICIENT_SUBJECT,
        REASON_TRUNCATED_PAGE_NO_EVIDENCE,
        REASON_PAGE_CHALLENGE_NO_EVIDENCE,
        REASON_AMBIGUITY,
        REASON_NOT_ATTEMPTED_STAGE_BUDGET,
        REASON_CANDIDATE_BUDGET_EXHAUSTED,
        REASON_SEARCH_SNIPPET,
        REASON_INVALID_SOURCE,
        REASON_SYNTHESIS_FAILURE,
        REASON_INVALID_DESCRIPTION,
        REASON_INSUFFICIENT_DEPTH,
        REASON_EVIDENCE_UNCHANGED,
        REASON_STAGE_EXECUTION_FAILURE,
    }
)

REASON_LABELS_RU = {
    REASON_NO_RESULTS: "Источники не найдены",
    REASON_SEARCH_OR_FETCH_FAILURE: "Не удалось проверить источники",
    REASON_SOURCE_DENYLISTED: "Источник в стоп-списке источников",
    REASON_FEWER_THAN_TWO: "Недостаточно независимых источников",
    REASON_TITLE_MISMATCH: "Название источника не совпадает",
    REASON_AUTHOR_MISMATCH: "Автор источника не совпадает",
    REASON_AUTHOR_UNVERIFIABLE: "Автор источника не указан",
    REASON_SOURCE_IDENTITY_DISAGREEMENT: "Источники противоречат друг другу",
    REASON_SUBJECT_DISAGREEMENT: "Источники противоречат друг другу",
    REASON_INSUFFICIENT_SUBJECT: "Недостаточно предметного текста",
    REASON_TRUNCATED_PAGE_NO_EVIDENCE: "Обрезанная страница не содержит подтверждения",
    REASON_PAGE_CHALLENGE_NO_EVIDENCE: "Страница источника не отдала подтверждение",
    REASON_AMBIGUITY: "Не удалось однозначно определить книгу",
    REASON_NOT_YET_PUBLISHED: "Книга ещё не опубликована",
    REASON_NOT_ATTEMPTED_STAGE_BUDGET: "Поиск источников еще не запускался",
    REASON_CANDIDATE_BUDGET_EXHAUSTED: "Проверка источников не завершена вовремя",
    REASON_SEARCH_SNIPPET: "Поисковый фрагмент не является источником",
    REASON_INVALID_SOURCE: "Источник непригоден для проверки",
    REASON_SYNTHESIS_FAILURE: "Не удалось синтезировать описание",
    REASON_INVALID_DESCRIPTION: "Синтезированное описание недействительно",
    REASON_INSUFFICIENT_DEPTH: "Описание короче порога глубины",
    REASON_EVIDENCE_UNCHANGED: "Описание уже подтверждено теми же источниками",
    REASON_STAGE_EXECUTION_FAILURE: "Этап проверки описаний не завершился",
}


def reason_label(reason_code: str) -> str:
    """Return the bounded Russian editor label for a raw stage reason."""
    return REASON_LABELS_RU.get(str(reason_code).strip(), "Не удалось подтвердить описание")


_SOURCE_PREFERENCE = {
    "review": 0,
    # Issue #689: a host that owns itself, fetched before storefronts so the
    # two-owner rule is reachable inside the per-candidate fetch budget.
    "independent": 0,
    "review_page": 0,
    "author_official": 1,
    "author_official_page": 1,
    "publisher": 2,
    "publisher_page": 2,
    "retail_listing": 2,
}


class OutcomeClass(str, Enum):
    GROUNDED = OUTCOME_GROUNDED
    SINGLE_SOURCE = OUTCOME_SINGLE_SOURCE
    ATTEMPTED_NO_DESCRIPTION = OUTCOME_ATTEMPTED_NO_DESCRIPTION
    NOT_ATTEMPTED = OUTCOME_NOT_ATTEMPTED
    EVIDENCE_UNCHANGED = OUTCOME_EVIDENCE_UNCHANGED


class DiscoveryProvider(Protocol):
    def discover(self, candidate: Mapping[str, Any]) -> Sequence[Mapping[str, Any] | DiscoveryResult]: ...


class FetchProvider(Protocol):
    def fetch(self, result: Mapping[str, Any] | DiscoveryResult) -> Mapping[str, Any] | FetchedSource: ...


@dataclass(frozen=True)
class SynthesisResult:
    """Typed synthesis output: one description plus optional raw claim mappings.

    Claims stay untrusted mappings here; only the stage validates them with
    ``validate_prominence_claims`` before they reach a ``StageOutcome``.
    """

    content_description: str
    prominence_claims: tuple[Mapping[str, Any], ...] = ()
    # Untrusted until the stage validates it; see ``_validated_english``.
    english_description: str = ""


class DescriptionSynthesizer(Protocol):
    def synthesize(
        self,
        candidate: Mapping[str, Any],
        sources: Sequence[AcceptedSource],
        *,
        remaining_seconds: float | None = None,
    ) -> SynthesisResult: ...


@dataclass(frozen=True)
class DurableTarget:
    candidate_id: str
    title: str
    primary_authors: tuple[str, ...]
    record: Mapping[str, Any] = field(repr=False)

    def mention(self) -> dict[str, Any]:
        return {"title": self.title, "primary_authors": list(self.primary_authors)}


@dataclass(frozen=True)
class TargetResolution:
    target: DurableTarget | None
    reason_code: str = ""

    @property
    def resolved(self) -> bool:
        return self.target is not None and not self.reason_code


@dataclass(frozen=True)
class DiscoveryResult:
    url: str
    source_kind: str
    source_family: str
    owner_id: str
    source_language: str = ""
    classification: str = "classified"
    is_search_snippet: bool = False
    title_evidence_route: str = "direct_original"
    title: str = ""
    primary_authors: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False)
    title_evidence: Mapping[str, Any] | None = field(default=None, repr=False)
    classification_evidence: Mapping[str, Any] = field(default_factory=dict, repr=False)
    route_kind: str = ""

    # Issue #760: row provenance and fail-closed Independent Signal ownership.
    origin: str = "discovery"
    signal_own: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "origin", "row_evidence" if self.origin == "row_evidence" else "discovery")
        object.__setattr__(self, "title", _bounded(self.title, MAX_DISCOVERY_TITLE_TEXT))
        object.__setattr__(self, "route_kind", _bounded(self.route_kind, 50))
        object.__setattr__(self, "title_evidence", _bounded_title_evidence(self.title_evidence))
        object.__setattr__(
            self,
            "classification_evidence",
            _classification_evidence_from_mapping(
                self.classification_evidence or self.metadata,
                url=self.url,
                source_kind=self.source_kind,
                source_family=self.source_family,
                owner_id=self.owner_id,
                source_language=self.source_language,
                classification=self.classification,
            ),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | DiscoveryResult) -> DiscoveryResult:
        if isinstance(value, cls):
            return value
        value = cast(Mapping[str, Any], value)
        authors = value.get("primary_authors", value.get("authors", []))
        if isinstance(authors, str):
            authors = [authors]
        if not isinstance(authors, Sequence) or isinstance(authors, bytes | bytearray):
            authors = []
        evidence = value.get("title_evidence")
        if evidence is None and isinstance(value.get("publication_title_evidence"), Mapping):
            evidence = value.get("publication_title_evidence")
        url = str(value.get("url", value.get("canonical_url", ""))).strip()
        source_kind = str(value.get("source_kind", "")).strip()
        source_family = str(value.get("source_family", "")).strip()
        owner_id = str(value.get("owner_id", "")).strip()
        source_language = str(value.get("source_language", "")).strip()
        classification = str(value.get("classification", "classified")).strip()
        return cls(
            origin="row_evidence" if value.get("origin") == "row_evidence" else "discovery",
            signal_own=value.get("signal_own") is True,
            url=url,
            source_kind=source_kind,
            source_family=source_family,
            owner_id=owner_id,
            source_language=source_language,
            classification=classification,
            is_search_snippet=value.get("is_search_snippet") is True
            or str(value.get("result_kind", "")).strip() == "search_snippet",
            title_evidence_route=str(value.get("title_evidence_route", "direct_original")).strip(),
            title_evidence=_bounded_title_evidence(evidence),
            title=_bounded(value.get("title", ""), MAX_DISCOVERY_TITLE_TEXT),
            route_kind=_bounded(value.get("route_kind", ""), 50),
            primary_authors=tuple(str(author).strip() for author in authors if str(author).strip()),
            metadata=dict(value),
            classification_evidence=_classification_evidence_from_mapping(
                value,
                url=url,
                source_kind=source_kind,
                source_family=source_family,
                owner_id=owner_id,
                source_language=source_language,
                classification=classification,
            ),
        )


@dataclass(frozen=True)
class FetchedSource:
    url: str
    source_kind: str
    source_family: str
    owner_id: str
    source_language: str
    title: str
    primary_authors: tuple[str, ...]
    subject_text: str
    subject_key: str = ""
    checked_date: str = ""
    title_evidence_route: str = "direct_original"
    title_evidence: Mapping[str, Any] | None = field(default=None, repr=False)
    is_search_snippet: bool = False
    classification: str = "classified"
    display_text: str = ""
    isbn: str = ""
    title_type: str = ""
    source_preference: int = 99
    classification_evidence: Mapping[str, Any] = field(default_factory=dict, repr=False)
    relevance_tier: int = 2
    discovery_ordinal: int = 0
    discovery_title: str = ""
    # Text of the page's first visible author link outside a related-books section,
    # used only to check whether the page prints a given author name for its
    # own book (issue #709). In memory only, never serialized.
    author_evidence_text: str = field(default="", repr=False, compare=False)
    # The page's visible text outside site chrome, hidden elements, and
    # related-books sections, used only to check that the page prints a cited
    # book's ISBN, title, or author surname (issue #791). In memory only,
    # never serialized.
    identity_evidence_text: str = field(default="", repr=False, compare=False)
    # False when the page's only stated author is its ``<meta name="author">``
    # value. That names whoever wrote the page, and on a review it is the
    # reviewer, so it cannot refute a book author the page prints in its text
    # (issue #791). In memory only, never serialized.
    states_book_author: bool = field(default=True, repr=False, compare=False)
    # The ISBN read from a fetched product page's own address (#755). It is
    # weaker than an ISBN the page states: a bot check answers at the same
    # address, so it proves identity only beside a matching page title, and
    # it is never learned into the candidate. In memory only, never serialized.
    url_isbn: str = field(default="", repr=False, compare=False)

    # Issue #760: row provenance and fail-closed Independent Signal ownership.
    origin: str = "discovery"
    signal_own: bool = False
    origin_recorded: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "origin", "row_evidence" if self.origin == "row_evidence" else "discovery")
        object.__setattr__(self, "discovery_title", _bounded(self.discovery_title, MAX_DISCOVERY_TITLE_TEXT))
        object.__setattr__(self, "title_evidence", _bounded_title_evidence(self.title_evidence))
        object.__setattr__(
            self,
            "classification_evidence",
            _classification_evidence_from_mapping(
                self.classification_evidence,
                url=self.url,
                source_kind=self.source_kind,
                source_family=self.source_family,
                owner_id=self.owner_id,
                source_language=self.source_language,
                classification=self.classification,
            ),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | FetchedSource) -> FetchedSource:
        if isinstance(value, cls):
            return value
        value = cast(Mapping[str, Any], value)
        authors = value.get("primary_authors", value.get("authors", []))
        if isinstance(authors, str):
            authors = [authors]
        if not isinstance(authors, Sequence) or isinstance(authors, bytes | bytearray):
            authors = []
        evidence = value.get("title_evidence")
        if evidence is None and isinstance(value.get("publication_title_evidence"), Mapping):
            evidence = value.get("publication_title_evidence")
        url = str(value.get("url", value.get("canonical_url", ""))).strip()
        source_kind = str(value.get("source_kind", "")).strip()
        source_family = str(value.get("source_family", "")).strip()
        owner_id = str(value.get("owner_id", "")).strip()
        source_language = str(value.get("source_language", "")).strip()
        classification = str(value.get("classification", "classified")).strip()
        return cls(
            origin_recorded="origin" in value,
            origin="row_evidence" if value.get("origin") == "row_evidence" else "discovery",
            signal_own=value.get("signal_own") is True,
            url=url,
            source_kind=source_kind,
            source_family=source_family,
            owner_id=owner_id,
            source_language=source_language,
            title=str(value.get("title", "")).strip(),
            primary_authors=tuple(str(author).strip() for author in authors if str(author).strip()),
            subject_text=str(value.get("subject_text", "")).strip(),
            subject_key=str(value.get("subject_key", value.get("subject", ""))).strip(),
            checked_date=str(value.get("checked_date", value.get("source_check_date", ""))).strip(),
            title_evidence_route=str(value.get("title_evidence_route", "direct_original")).strip() or "direct_original",
            title_evidence=_bounded_title_evidence(evidence),
            is_search_snippet=value.get("is_search_snippet") is True
            or str(value.get("result_kind", "")).strip() == "search_snippet",
            classification=classification,
            display_text=str(value.get("display_text", "")).strip(),
            isbn=str(value.get("isbn", "")).strip(),
            title_type=str(value.get("title_type", value.get("english_title_type", ""))).strip(),
            source_preference=int(value.get("source_preference", 99) or 99),
            classification_evidence=_classification_evidence_from_mapping(
                value,
                url=url,
                source_kind=source_kind,
                source_family=source_family,
                owner_id=owner_id,
                source_language=source_language,
                classification=classification,
            ),
            relevance_tier=_bounded_int(value.get("relevance_tier", 2), 2),
            discovery_ordinal=_bounded_int(value.get("discovery_ordinal", 0), 0),
            discovery_title=_bounded(value.get("discovery_title", ""), MAX_DISCOVERY_TITLE_TEXT),
        )


@dataclass(frozen=True)
class AcceptedSource:
    canonical_url: str
    source_kind: str = ""
    source_family: str = ""
    owner_id: str = ""
    source_language: str = ""
    title_evidence_route: str = "direct_original"
    checked_date: str = ""
    raw_title: str = ""
    raw_authors: tuple[str, ...] = ()
    subject_key: str = ""
    display_text: str = ""
    isbn: str = ""
    title_evidence: Mapping[str, Any] | None = field(default=None, repr=False)
    classification_evidence: Mapping[str, Any] = field(default_factory=dict, repr=False)
    relevance_tier: int = 2
    discovery_ordinal: int = 0
    discovery_title: str = ""
    subject_text: str = ""

    # Issue #760: row provenance and fail-closed Independent Signal ownership.
    origin: str = "discovery"
    signal_own: bool = False
    # Preserve the shape of legacy evidence records when round-tripping.
    origin_recorded: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "origin", "row_evidence" if self.origin == "row_evidence" else "discovery")
        object.__setattr__(self, "discovery_title", _bounded(self.discovery_title, MAX_DISCOVERY_TITLE_TEXT))
        object.__setattr__(self, "display_text", _bounded_subject(self.display_text))
        object.__setattr__(self, "subject_text", _bounded_subject(self.subject_text))
        object.__setattr__(self, "title_evidence", _bounded_title_evidence(self.title_evidence))
        object.__setattr__(
            self,
            "classification_evidence",
            _classification_evidence_from_mapping(
                self.classification_evidence,
                url=self.canonical_url,
                source_kind=self.source_kind,
                source_family=self.source_family,
                owner_id=self.owner_id,
                source_language=self.source_language,
                classification="classified",
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        classification_evidence = _classification_evidence_from_mapping(
            self.classification_evidence,
            url=self.canonical_url,
            source_kind=self.source_kind,
            source_family=self.source_family,
            owner_id=self.owner_id,
            source_language=self.source_language,
            classification="classified",
        )
        result: dict[str, Any] = {
            **(
                {"origin": self.origin, "signal_own": self.signal_own}
                if self.origin_recorded or self.origin == "row_evidence"
                else {}
            ),
            "url": self.canonical_url[:MAX_URL_LENGTH],
            "source_kind": self.source_kind[:MAX_METADATA_VALUE_LENGTH],
            "source_family": self.source_family[:MAX_METADATA_VALUE_LENGTH],
            "owner_id": self.owner_id[:MAX_METADATA_VALUE_LENGTH],
            "source_language": self.source_language[:MAX_METADATA_VALUE_LENGTH],
            "title_evidence_route": self.title_evidence_route[:100],
            "checked_date": self.checked_date[:50],
            "raw_title": self.raw_title[:MAX_EVIDENCE_METADATA_TEXT],
            "raw_authors": [author[:200] for author in self.raw_authors[:10]],
            "subject_key": self.subject_key[:MAX_EVIDENCE_METADATA_TEXT],
            "classification": classification_evidence["classification"],
            "classification_evidence": classification_evidence,
            **({"title_evidence": _bounded_title_evidence(self.title_evidence)} if self.title_evidence else {}),
            **({"display_text": _bounded_subject(self.display_text)} if self.display_text else {}),
            **({"isbn": self.isbn[:50]} if self.isbn else {}),
            "relevance_tier": int(self.relevance_tier),
            "discovery_ordinal": int(self.discovery_ordinal),
            **(
                {"discovery_title": _bounded(self.discovery_title, MAX_DISCOVERY_TITLE_TEXT)}
                if self.discovery_title
                else {}
            ),
        }
        return result


@dataclass(frozen=True)
class SourceIndependenceEvidence:
    canonical_urls: tuple[str, ...]
    owner_ids: tuple[str, ...]
    source_families: tuple[str, ...]
    reason: str = SOURCE_INDEPENDENCE_REASON

    @classmethod
    def from_sources(cls, sources: Sequence[AcceptedSource]) -> SourceIndependenceEvidence:
        return cls(
            canonical_urls=tuple(source.canonical_url for source in sources),
            owner_ids=tuple(source.owner_id for source in sources),
            source_families=tuple(source.source_family for source in sources),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> SourceIndependenceEvidence:
        def _strings(key: str) -> tuple[str, ...]:
            raw = value.get(key, [])
            if isinstance(raw, str):
                raw = [raw]
            if not isinstance(raw, Sequence) or isinstance(raw, bytes | bytearray):
                return ()
            return tuple(_bounded(item, MAX_URL_LENGTH) for item in raw if str(item).strip())

        return cls(
            canonical_urls=_strings("canonical_urls"),
            owner_ids=_strings("owner_ids"),
            source_families=_strings("source_families"),
            reason=_bounded(value.get("reason", ""), 100),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_urls": list(self.canonical_urls),
            "owner_ids": list(self.owner_ids),
            "source_families": list(self.source_families),
            "reason": self.reason[:100],
        }


@dataclass(frozen=True)
class RejectedSource:
    canonical_url: str
    reason_code: str
    # Issue #689: the tier that refused this URL, when one did. Omitted when
    # empty so artifacts predating the tiered policy stay byte-identical.
    tier: str = ""

    def to_dict(self) -> dict[str, str]:
        result = {"url": self.canonical_url, "reason_code": self.reason_code}
        if self.tier:
            result["tier"] = self.tier
        return result

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> RejectedSource:
        """Rebuild one rejection, keeping the tier a run recorded."""
        return cls(
            str(value.get("url", "")),
            str(value.get("reason_code", "")),
            tier=str(value.get("tier", "")),
        )


def _classification_tier(result: Any) -> str:
    """Read the derived tier off one discovery result, or "" when absent."""
    for source in (
        getattr(result, "classification_evidence", None),
        getattr(result, "metadata", None),
    ):
        if not isinstance(source, Mapping):
            continue
        metadata = source.get("source_metadata")
        if isinstance(metadata, Mapping) and metadata.get("tier"):
            return str(metadata["tier"])
        if source.get("tier"):
            return str(source["tier"])
    return ""


def _merge_rejected_sources(*groups: Sequence[RejectedSource]) -> tuple[RejectedSource, ...]:
    merged: list[RejectedSource] = []
    seen_urls: set[str] = set()
    for group in groups:
        for source in group:
            if source.canonical_url in seen_urls:
                continue
            seen_urls.add(source.canonical_url)
            merged.append(source)
    return tuple(merged)


@dataclass(frozen=True)
class GroundingGateResult:
    accepted: bool
    reason_code: str
    accepted_sources: tuple[AcceptedSource, ...] = ()
    rejected_sources: tuple[RejectedSource, ...] = ()
    source_independence_evidence: SourceIndependenceEvidence | None = None

    @property
    def research_sources(self) -> tuple[str, ...]:
        return tuple(source.canonical_url for source in self.accepted_sources)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reason_code": self.reason_code,
            "accepted_sources": [source.to_dict() for source in self.accepted_sources],
            "rejected_sources": [source.to_dict() for source in self.rejected_sources],
            **(
                {"source_independence_evidence": self.source_independence_evidence.to_dict()}
                if self.source_independence_evidence is not None
                else {}
            ),
        }


@dataclass(frozen=True)
class ProminenceClaim:
    claim_type: str
    evidence_urls: tuple[str, ...]
    source_kind: str
    checked_date: str
    display_text: str
    evidence_span: str
    title: str
    primary_authors: tuple[str, ...]
    title_evidence_route: str = "direct_original"
    title_evidence: Mapping[str, Any] | None = field(default=None, repr=False)
    scope: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ProminenceClaim:
        urls = value.get("evidence_urls", value.get("evidence_url", []))
        if isinstance(urls, str):
            urls = [urls]
        if not isinstance(urls, Sequence) or isinstance(urls, bytes | bytearray):
            urls = []
        authors = value.get("primary_authors", value.get("authors", []))
        if isinstance(authors, str):
            authors = [authors]
        if not isinstance(authors, Sequence) or isinstance(authors, bytes | bytearray):
            authors = []
        evidence = value.get("title_evidence")
        return cls(
            claim_type=str(value.get("claim_type", "")).strip(),
            evidence_urls=tuple(str(url).strip() for url in urls if str(url).strip()),
            source_kind=str(value.get("source_kind", "")).strip(),
            checked_date=str(value.get("checked_date", "")).strip(),
            display_text=str(value.get("display_text", "")).strip(),
            evidence_span=str(value.get("evidence_span", "")).strip(),
            title=str(value.get("title", "")).strip(),
            primary_authors=tuple(str(author).strip() for author in authors if str(author).strip()),
            title_evidence_route=str(value.get("title_evidence_route", "direct_original")).strip(),
            title_evidence=dict(evidence) if isinstance(evidence, Mapping) else None,
            scope=str(value.get("scope", "")).strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        urls = tuple(canonicalize_url(url) for url in self.evidence_urls if canonicalize_url(url))
        result: dict[str, Any] = {
            "claim_type": self.claim_type[:100],
            "evidence_urls": list(urls),
            "source_kind": self.source_kind[:100],
            "checked_date": self.checked_date[:50],
            "display_text": self.display_text[:MAX_DISPLAY_TEXT],
            "evidence_span": _bounded(self.evidence_span, _SUBJECT_CONTEXT_FIELD_MAX_CHARS),
            "title": self.title[:MAX_EVIDENCE_METADATA_TEXT],
            "primary_authors": [author[:200] for author in self.primary_authors[:10]],
            "title_evidence_route": self.title_evidence_route[:100],
            **({"scope": self.scope[:100]} if self.scope else {}),
        }
        if self.title_evidence:
            result["title_evidence"] = _bounded_title_evidence(self.title_evidence)
        return result


@dataclass(frozen=True)
class ProminenceValidation:
    accepted: bool
    reason_code: str
    claim: ProminenceClaim | None = None


@dataclass(frozen=True)
class ProminenceClaimsValidation:
    accepted: bool
    reason_code: str
    claims: tuple[ProminenceClaim, ...] = ()


@dataclass(frozen=True)
class QueueEntry:
    candidate_id: str
    queue_entry_operation: str
    queue_entry_date: str
    first_seen_date: str
    priority_label: str
    attempt_status: str = "waiting"
    reason_code: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> QueueEntry:
        return cls(
            candidate_id=str(value.get("candidate_id", "")).strip(),
            queue_entry_operation=str(value.get("queue_entry_operation", "")).strip(),
            queue_entry_date=str(value.get("queue_entry_date", "")).strip(),
            first_seen_date=str(value.get("first_seen_date", "")).strip(),
            priority_label=str(value.get("priority_label", "D")).strip().upper() or "D",
            attempt_status=str(value.get("attempt_status", "waiting")).strip() or "waiting",
            reason_code=str(value.get("reason_code", "")).strip(),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "candidate_id": self.candidate_id,
            "queue_entry_operation": self.queue_entry_operation,
            "queue_entry_date": self.queue_entry_date,
            "first_seen_date": self.first_seen_date,
            "priority_label": self.priority_label,
            "attempt_status": self.attempt_status,
            "reason_code": self.reason_code,
        }

    @property
    def waiting(self) -> bool:
        return self.attempt_status in {"waiting", OUTCOME_NOT_ATTEMPTED}


class CandidateRequestBudget:
    """Phase-local deadline and actual HTTP request budget.

    Production providers call :meth:`before_request` immediately before
    each transport call.
    Discovery and post-discovery work receive separate instances, so a
    deadline is fixed for the lifetime of one phase budget.
    The object counts discovery requests and source-fetch requests separately.
    The deadline is exposed through a read-only property, while the request
    counters remain ordinary mutable fields updated by ``before_request``.
    """

    def __init__(
        self,
        deadline: float,
        max_fetches: int,
        monotonic: Callable[[], float],
        discovery_requests: int = 0,
        fetch_requests: int = 0,
        max_discovery_requests: int = 1,
    ) -> None:
        self._deadline = float(deadline)
        self.max_fetches = max_fetches
        self.max_discovery_requests = max_discovery_requests
        self.monotonic = monotonic
        self.discovery_requests = discovery_requests
        self.fetch_requests = fetch_requests

    def __repr__(self) -> str:
        return (
            "CandidateRequestBudget("
            f"deadline={self.deadline!r}, "
            f"max_fetches={self.max_fetches!r}, "
            f"max_discovery_requests={self.max_discovery_requests!r}, "
            f"monotonic={self.monotonic!r}, "
            f"discovery_requests={self.discovery_requests!r}, "
            f"fetch_requests={self.fetch_requests!r})"
        )

    @property
    def deadline(self) -> float:
        """Return the immutable phase deadline."""
        return self._deadline

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline - float(self.monotonic()))

    def timeout_seconds(self, inactivity_timeout: float) -> float:
        remaining = self.remaining_seconds()
        if remaining <= 0.0:
            raise BoundedHTTPError(
                "candidate deadline expired",
                reason_code=REASON_CANDIDATE_BUDGET_EXHAUSTED,
            )
        return min(max(0.0, float(inactivity_timeout)), remaining)

    def ensure_request_available(self, request_kind: str) -> None:
        """Check deadline and request capacity without consuming a slot."""
        if self.remaining_seconds() <= 0.0:
            raise BoundedHTTPError(
                "candidate deadline expired",
                reason_code=REASON_CANDIDATE_BUDGET_EXHAUSTED,
            )
        if request_kind == "discovery" and self.discovery_requests >= max(0, int(self.max_discovery_requests)):
            raise BoundedHTTPError("discovery request budget exhausted", reason_code=REASON_SEARCH_OR_FETCH_FAILURE)
        if request_kind == "fetch" and self.fetch_requests >= max(0, int(self.max_fetches)):
            raise BoundedHTTPError("fetch request budget exhausted", reason_code=REASON_SEARCH_OR_FETCH_FAILURE)
        if request_kind not in {"discovery", "fetch"}:
            raise BoundedHTTPError("unknown request kind", reason_code=REASON_SEARCH_OR_FETCH_FAILURE)

    def before_request(self, request_kind: str, inactivity_timeout: float) -> float:
        """Check the deadline and reserve one actual request slot."""
        self.ensure_request_available(request_kind)
        if request_kind == "discovery":
            self.discovery_requests += 1
        else:
            self.fetch_requests += 1
        return self.timeout_seconds(inactivity_timeout)

    def before_rate_limit_wait(self, delay_seconds: float) -> None:
        """Refuse a wait that cannot finish before the candidate deadline."""
        remaining = self.remaining_seconds()
        if remaining <= 0.0 or max(0.0, float(delay_seconds)) >= remaining:
            raise BoundedHTTPError(
                "candidate deadline expired",
                reason_code=REASON_CANDIDATE_BUDGET_EXHAUSTED,
            )


@dataclass(frozen=True)
class CandidateTiming:
    """Wall-clock timing for one candidate attempt.

    ``None`` means that the phase was not measurable, not that it took zero
    seconds.
    Per-candidate wall-clock intervals may overlap when up to ``MAX_WORKERS``
    candidates run concurrently, so their totals can exceed the stage total.
    """

    discovery_seconds: float | None = None
    fetch_seconds: float | None = None
    synthesis_seconds: float | None = None
    total_seconds: float | None = None

    _FIELDS = frozenset({"discovery_seconds", "fetch_seconds", "synthesis_seconds", "total_seconds"})

    @staticmethod
    def _parse_seconds(value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return max(0.0, parsed) if math.isfinite(parsed) else None

    @staticmethod
    def _is_valid_seconds(value: Any) -> bool:
        if value is None or isinstance(value, bool):
            return value is None
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return False
        return math.isfinite(parsed) and parsed >= 0.0

    @classmethod
    def mapping_is_valid(cls, value: Any) -> bool:
        if value is None:
            return True
        if not isinstance(value, Mapping) or set(value) - cls._FIELDS:
            return False
        return all(cls._is_valid_seconds(item) for item in value.values())

    @classmethod
    def from_mapping(cls, value: Any) -> CandidateTiming:
        if not isinstance(value, Mapping):
            return cls()
        return cls(
            discovery_seconds=cls._parse_seconds(value.get("discovery_seconds")),
            fetch_seconds=cls._parse_seconds(value.get("fetch_seconds")),
            synthesis_seconds=cls._parse_seconds(value.get("synthesis_seconds")),
            total_seconds=cls._parse_seconds(value.get("total_seconds")),
        )

    def is_valid(self) -> bool:
        return all(
            self._is_valid_seconds(value)
            for value in (self.discovery_seconds, self.fetch_seconds, self.synthesis_seconds, self.total_seconds)
        )

    @staticmethod
    def _serialize_seconds(value: float | None) -> float | None:
        if value is None or not math.isfinite(value):
            return None
        return round(max(0.0, value), 3)

    def describe(self) -> str:
        """Render the bounded four-field timing detail for diagnostics."""
        return "; ".join(
            f"{phase}={value if value is not None else 'unknown'}"
            for phase, value in (
                ("discovery", self.discovery_seconds),
                ("fetch", self.fetch_seconds),
                ("synthesis", self.synthesis_seconds),
                ("total", self.total_seconds),
            )
        )

    def to_dict(self) -> dict[str, float | None]:
        return {
            "discovery_seconds": self._serialize_seconds(self.discovery_seconds),
            "fetch_seconds": self._serialize_seconds(self.fetch_seconds),
            "synthesis_seconds": self._serialize_seconds(self.synthesis_seconds),
            "total_seconds": self._serialize_seconds(self.total_seconds),
        }


@dataclass(frozen=True)
class AuthorTypoEvidence:
    """Advisory metadata from one ISBN-proven page, never a correction (#869)."""

    url: str
    stored_authors: tuple[str, ...]
    printed_authors: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "stored_authors": list(self.stored_authors),
            "printed_authors": list(self.printed_authors),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> AuthorTypoEvidence:
        return cls(
            url=str(value.get("url", "")),
            stored_authors=tuple(value.get("stored_authors", [])),
            printed_authors=tuple(value.get("printed_authors", [])),
        )

    @staticmethod
    def mapping_is_valid(value: Any) -> bool:
        if not isinstance(value, Mapping):
            return False
        url = value.get("url")
        if not isinstance(url, str) or not url or canonicalize_url(url) != url:
            return False
        for key in ("stored_authors", "printed_authors"):
            authors = value.get(key)
            if not isinstance(authors, list) or not 1 <= len(authors) <= 10:
                return False
            if any(not isinstance(author, str) or not author.strip() or len(author) > 200 for author in authors):
                return False
        return True


@dataclass(frozen=True)
class StageOutcome:
    candidate_id: str
    outcome: str
    reason_code: str = ""
    identity_key: str = ""
    content_description: str = ""
    research_sources: tuple[AcceptedSource, ...] = ()
    prominence_claims: tuple[ProminenceClaim, ...] = ()
    scheduling: QueueEntry | None = None
    rejected_source_reasons: tuple[RejectedSource, ...] = ()
    source_independence_evidence: SourceIndependenceEvidence | None = None
    reason_detail: str = ""
    applied_date: str = ""
    candidate_timing: CandidateTiming = field(default_factory=CandidateTiming)
    # Validated English Description (issue #727). Persisted only inside the
    # provenance record, beside the Russian text it belongs to.
    english_description: str = ""
    # Stage artifact only: Candidate identity and stored authors remain facts.
    likely_author_typos: tuple[AuthorTypoEvidence, ...] = ()
    row_evidence_fetched: int = 0

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "candidate_id": self.candidate_id,
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "candidate_timing": self.candidate_timing.to_dict(),
        }
        # A non-admitted record intentionally has scheduling metadata and timing only.
        if self.outcome == OUTCOME_NOT_ATTEMPTED:
            if self.scheduling is not None:
                result["scheduling"] = self.scheduling.to_dict()
            return result
        result["row_evidence_fetched"] = min(2, max(0, self.row_evidence_fetched))
        if self.likely_author_typos:
            result["likely_author_typos"] = [item.to_dict() for item in self.likely_author_typos]
        if self.outcome == OUTCOME_EVIDENCE_UNCHANGED:
            result["identity_key"] = self.identity_key
            if self.scheduling is not None:
                result["scheduling"] = self.scheduling.to_dict()
            if self.rejected_source_reasons:
                result["rejected_source_reasons"] = [item.to_dict() for item in self.rejected_source_reasons]
            return result
        if self.reason_detail:
            result["reason_detail"] = _bounded(self.reason_detail, MAX_METADATA_VALUE_LENGTH)
        result["identity_key"] = self.identity_key
        if self.content_description:
            result["content_description"] = self.content_description
        if self.research_sources:
            result["research_sources"] = [source.canonical_url for source in self.research_sources]
            result["research_source_records"] = [source.to_dict() for source in self.research_sources]
        # A provenance record claims a description exists, so it is written
        # only with one: a refusal that retains verified sources (issue
        # #684) carries the URLs and records, never a provenance claim.
        if self.content_description:
            if self.research_sources:
                result["description_provenance"] = description_provenance_record(
                    self.identity_key,
                    self.content_description,
                    "grounded",
                    evidence_basis=[source.canonical_url for source in self.research_sources],
                    applied_date=self.applied_date or self.research_sources[0].checked_date,
                    grounding_evidence_url=self.research_sources[0].canonical_url,
                    english_description=self.english_description or None,
                )
                independence = self.source_independence_evidence or SourceIndependenceEvidence.from_sources(
                    self.research_sources
                )
                result["source_independence_evidence"] = independence.to_dict()
        if self.prominence_claims:
            result["prominence_claims"] = [claim.to_dict() for claim in self.prominence_claims]
        if self.rejected_source_reasons:
            result["rejected_source_reasons"] = [item.to_dict() for item in self.rejected_source_reasons]
        if self.scheduling is not None:
            result["scheduling"] = self.scheduling.to_dict()
        return result


@dataclass(frozen=True)
class StageArtifact:
    operation_id: str
    operation_date: str
    outcomes: tuple[StageOutcome, ...]
    queue: tuple[QueueEntry, ...]
    stage_version: str = STAGE_VERSION
    elapsed_seconds: float = 0.0
    stage_budget_seconds: float = STAGE_BUDGET_SECONDS
    finalization_preserved: bool = True
    report: dict[str, Any] = field(default_factory=dict)

    @property
    def counts(self) -> dict[str, int]:
        return {outcome: sum(item.outcome == outcome for item in self.outcomes) for outcome in OUTCOME_ORDER}

    @property
    def reason_counts(self) -> dict[str, int]:
        """Return deterministic counts for every non-empty raw reason code."""
        counts: dict[str, int] = {}
        for item in self.outcomes:
            reason = str(item.reason_code).strip()
            if reason:
                counts[reason] = counts.get(reason, 0) + 1
        return {reason: counts[reason] for reason in sorted(counts)}

    @property
    def call_latency(self) -> dict[str, Any]:
        from autoscout.description_diagnostics import call_latency_summary

        return call_latency_summary(
            [
                item.candidate_timing.synthesis_seconds
                for item in self.outcomes
                if item.candidate_timing.synthesis_seconds is not None
            ],
            self.reason_counts.get(REASON_CANDIDATE_BUDGET_EXHAUSTED, 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": "grounded-description",
            "stage_version": self.stage_version,
            "operation_id": self.operation_id,
            "operation_date": self.operation_date,
            "timing": {
                "elapsed_seconds": round(self.elapsed_seconds, 3),
                "stage_budget_seconds": self.stage_budget_seconds,
                "finalization_preserved": self.finalization_preserved,
            },
            "counts": self.counts,
            "reason_counts": self.reason_counts,
            "call_latency": self.call_latency,
            "outcomes": [item.to_dict() for item in self.outcomes],
            "queue": [item.to_dict() for item in self.queue],
            **({"report": self.report} if self.report else {}),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> StageArtifact:
        raw_outcomes = value.get("outcomes", [])
        outcomes: list[StageOutcome] = []
        if isinstance(raw_outcomes, list):
            for raw in raw_outcomes:
                if not isinstance(raw, Mapping):
                    continue
                scheduling = raw.get("scheduling")
                queue_entry = QueueEntry.from_mapping(scheduling) if isinstance(scheduling, Mapping) else None
                raw_source_records = raw.get("research_source_records", raw.get("accepted_sources", []))
                if not isinstance(raw_source_records, list):
                    raw_source_records = []
                if not raw_source_records and isinstance(raw.get("research_sources"), list):
                    raw_source_records = [item for item in raw["research_sources"] if isinstance(item, str | Mapping)]
                parsed_sources: list[AcceptedSource] = []
                for source in raw_source_records:
                    if isinstance(source, str) and source.strip():
                        parsed_sources.append(AcceptedSource(canonical_url=source.strip()))
                    elif isinstance(source, Mapping):
                        raw_authors = source.get("raw_authors", [])
                        if isinstance(raw_authors, str):
                            raw_authors = [raw_authors]
                        if not isinstance(raw_authors, Sequence) or isinstance(raw_authors, bytes | bytearray):
                            raw_authors = []
                        title_evidence = source.get("title_evidence")
                        parsed_sources.append(
                            AcceptedSource(
                                origin_recorded="origin" in source,
                                origin="row_evidence" if source.get("origin") == "row_evidence" else "discovery",
                                signal_own=source.get("signal_own") is True,
                                canonical_url=str(source.get("url", "")),
                                source_kind=str(source.get("source_kind", "")),
                                source_family=str(source.get("source_family", "")),
                                owner_id=str(source.get("owner_id", "")),
                                source_language=str(source.get("source_language", "")),
                                title_evidence_route=str(source.get("title_evidence_route", "direct_original")).strip()
                                or "direct_original",
                                checked_date=str(source.get("checked_date", "")),
                                raw_title=str(source.get("raw_title", "")),
                                raw_authors=tuple(str(author) for author in raw_authors),
                                subject_key=str(source.get("subject_key", "")),
                                display_text=str(source.get("display_text", "")),
                                subject_text=str(source.get("subject_text", "")),
                                isbn=str(source.get("isbn", "")),
                                title_evidence=title_evidence if isinstance(title_evidence, Mapping) else None,
                                classification_evidence=source,
                                relevance_tier=_bounded_int(source.get("relevance_tier", 2), 2),
                                discovery_ordinal=_bounded_int(source.get("discovery_ordinal", 0), 0),
                                discovery_title=_bounded(source.get("discovery_title", ""), MAX_DISCOVERY_TITLE_TEXT),
                            )
                        )
                sources = tuple(parsed_sources)
                claims = tuple(
                    ProminenceClaim.from_mapping(claim)
                    for claim in raw.get("prominence_claims", [])
                    if isinstance(claim, Mapping)
                )
                rejected = tuple(
                    RejectedSource.from_mapping(item)
                    for item in raw.get("rejected_source_reasons", [])
                    if isinstance(item, Mapping)
                )
                raw_independence = raw.get("source_independence_evidence")
                independence = (
                    SourceIndependenceEvidence.from_mapping(raw_independence)
                    if isinstance(raw_independence, Mapping)
                    else (SourceIndependenceEvidence.from_sources(sources) if sources else None)
                )
                candidate_timing = CandidateTiming.from_mapping(raw.get("candidate_timing"))
                raw_provenance = raw.get("description_provenance")
                applied_date = (
                    str(raw_provenance.get("applied_date", "")) if isinstance(raw_provenance, Mapping) else ""
                )
                # A persisted artifact is re-validated like model output: a
                # hand-edited or stale English value is dropped, never trusted.
                # English belongs to the Russian it was written with, so it is
                # dropped when the outcome's Russian no longer matches.
                english_description = ""
                raw_typos = raw.get("likely_author_typos", [])
                if not isinstance(raw_typos, list):
                    raw_typos = []
                outcome_russian = str(raw.get("content_description", "")).strip()
                if (
                    isinstance(raw_provenance, Mapping)
                    and str(raw_provenance.get("content_description", "")).strip() == outcome_russian
                ):
                    english_description = (
                        validate_english_description(raw_provenance.get(ENGLISH_DESCRIPTION_FIELD, ""))[0] or ""
                    )
                outcomes.append(
                    StageOutcome(
                        candidate_id=str(raw.get("candidate_id", "")),
                        outcome=str(raw.get("outcome", "")),
                        reason_code=str(raw.get("reason_code", "")),
                        reason_detail=_bounded(raw.get("reason_detail", ""), MAX_METADATA_VALUE_LENGTH),
                        identity_key=str(raw.get("identity_key", "")),
                        content_description=str(raw.get("content_description", "")),
                        research_sources=sources,
                        prominence_claims=claims,
                        scheduling=queue_entry,
                        rejected_source_reasons=rejected,
                        source_independence_evidence=independence,
                        applied_date=applied_date,
                        candidate_timing=candidate_timing,
                        row_evidence_fetched=min(2, max(0, _bounded_int(raw.get("row_evidence_fetched", 0), 0))),
                        english_description=english_description,
                        likely_author_typos=tuple(
                            AuthorTypoEvidence.from_mapping(item)
                            for item in raw_typos
                            if AuthorTypoEvidence.mapping_is_valid(item)
                        ),
                    )
                )
        raw_queue = value.get("queue", [])
        queue = tuple(QueueEntry.from_mapping(item) for item in raw_queue if isinstance(item, Mapping))
        timing = value.get("timing", {})
        timing = timing if isinstance(timing, Mapping) else {}
        return cls(
            operation_id=str(value.get("operation_id", "")),
            operation_date=str(value.get("operation_date", "")),
            outcomes=tuple(outcomes),
            queue=queue,
            stage_version=str(value.get("stage_version", STAGE_VERSION)),
            elapsed_seconds=float(timing.get("elapsed_seconds", 0.0) or 0.0),
            stage_budget_seconds=float(
                timing.get("stage_budget_seconds", STAGE_BUDGET_SECONDS) or STAGE_BUDGET_SECONDS
            ),
            finalization_preserved=timing.get("finalization_preserved", True) is True,
            report=(
                dict(cast(Mapping[str, Any], value.get("report"))) if isinstance(value.get("report"), Mapping) else {}
            ),
        )


def canonicalize_url(value: str) -> str:
    """Return a deterministic HTTP(S) URL identity, or an empty string."""
    raw = str(value or "").strip()
    if len(raw) > MAX_URL_LENGTH:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    host = parsed.hostname.casefold()
    netloc = host
    if ":" in host and not host.startswith("["):
        netloc = f"[{host}]"
    if port and not (
        (parsed.scheme.casefold() == "http" and port == 80) or (parsed.scheme.casefold() == "https" and port == 443)
    ):
        netloc = f"{netloc}:{port}"
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.casefold(), netloc, path, parsed.query, ""))


def canonicalize_hostname(hostname: str) -> str:
    """Return a lowercase hostname with exactly one leading ``www.`` removed."""
    host = str(hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


# One author value can name several people (issue #756); the separator rule
# is shared with identity matching.
_AUTHOR_LIST_SEPARATOR = AUTHOR_LIST_SEPARATOR

# Explicit leading-token allowlist for grounding author matching only (#869).
# Do not move this into identity.py: its display normalizer also builds durable
# Candidate keys and History. Accents are folded for the rank lookup alone;
# author-evidence terms retain their existing accent-sensitive behavior.
_AUTHOR_MATCH_RANKS = frozenset(
    "captain capt colonel col major maj general gen lieutenant lt sergeant sgt admiral adm commander cmdr "
    "professor prof dr capitaine commandant general sergent amiral professeur docteur docteure "
    "capitao capita coronel tenente sargento almirante comandante professora doutor doutora "
    "capitan capitana mayor teniente profesor profesora doctor doctora "
    "kapitan hauptmann oberst leutnant feldwebel kommandant doktor "
    "kapitein kolonel majoor generaal luitenant admiraal commandeur hoogleraar dokter "
    "capitano colonnello maggiore generale sergente ammiraglio professore professoressa dottore dottoressa dott".split()
)
_AUTHOR_MATCH_PREFIX = re.compile(r"^([^\W\d_]+)\.?(?:\s+|,\s*)", flags=re.UNICODE)


def _author_match_display(value: str) -> str:
    """Remove leading ranks while retaining at least two name tokens.

    Keep the shared display normalizer unchanged, including its surname-first
    and credential handling. A short name also guards its existing Dr./Prof.
    stripping: a whole name or a rank plus one token is not enough evidence.
    """
    display = normalize_author_display(value)
    original_prefix = _AUTHOR_MATCH_PREFIX.match(value.strip())
    if (
        len(display.split()) < 2
        and original_prefix is not None
        and normalize_author_identity(original_prefix.group(1)) in _AUTHOR_MATCH_RANKS
    ):
        # Put the rank back, but keep the normalizer's credential/suffix handling.
        return " ".join((original_prefix.group(1), *display.split()))
    while match := _AUTHOR_MATCH_PREFIX.match(display):
        if normalize_author_identity(match.group(1)) not in _AUTHOR_MATCH_RANKS:
            break
        remainder = display[match.end() :].strip()
        if len(remainder.split()) < 2:
            break
        display = remainder
    return display


def _author_components(author: Any) -> list[str]:
    """Return the people named by one author value, as printed."""
    return [part.strip() for part in _AUTHOR_LIST_SEPARATOR.split(str(author)) if part.strip()]


def _author_keys(authors: Iterable[str]) -> frozenset[str]:
    keys: set[str] = set()
    for author in authors:
        for component in _author_components(author):
            normalized = normalize_author_identity(_author_match_display(component))
            if normalized:
                keys.add(normalized)
    return frozenset(keys)


def _fold_author_evidence_text(value: str) -> str:
    """Fold text for author-evidence matching, using the identity punctuation rule.

    ``normalize_identity_text`` is the same punctuation and case fold the
    identity keys already apply, so a straight and a curly apostrophe name the
    same person on both paths (issue #533).  Accents are deliberately kept:
    only ``normalize_author_identity`` strips them, and the evidence path must
    not start doing that silently.  Both the term and the text it is searched
    in must pass through here, otherwise a folded term cannot match unfolded
    text.
    """
    return normalize_identity_text(str(value).casefold())


def _author_subject_terms(authors: Iterable[str]) -> frozenset[str]:
    terms: set[str] = set()
    for author in authors:
        for component in _author_components(author):
            display = _author_match_display(component)
            if not display:
                continue
            # The surname is selected before folding, then folded whole.  Folding
            # first would split ``O'Malley`` into ``o malley`` and leave ``malley``
            # as the surname term, so a page about a different Malley would count
            # as evidence.  That widens matching beyond punctuation equivalence,
            # which issue #533 forbids and "fail closed on evidence" outranks.
            surname = display.split()[-1]
            terms.add(_fold_author_evidence_text(display))
            terms.add(_fold_author_evidence_text(surname))
    return frozenset(term for term in terms if term)


def _subject_names_target_author(target: DurableTarget, source: FetchedSource) -> bool:
    subject = _fold_author_evidence_text(source.subject_text)
    if not subject:
        return False
    return any(
        re.search(rf"(?<!\w){re.escape(term)}(?!\w)", subject, flags=re.UNICODE)
        for term in _author_subject_terms(target.primary_authors)
    )


def _headline_names_target_author(target: DurableTarget, source: FetchedSource) -> bool:
    """The fetched page title names a target author in full (#866).

    A headline repeats the book title, in whatever variant the page uses,
    so a name counts only when the headline names it more often than the
    canonical title does. Neither a surname that is a title word (Jean
    Smart, "Smart Mouth") nor a name inside the title ("Who Is Michael
    Ovitz?", "Diary of Anne Frank") proves anything; failing closed on an
    unusual title variant is acceptable.
    """
    headline = _fold_author_evidence_text(source.title)
    if not headline:
        return False
    title = _fold_author_evidence_text(target.title)
    full_names = {
        _fold_author_evidence_text(_author_match_display(component))
        for author in target.primary_authors
        for component in _author_components(author)
    }

    def occurrences(name: str, text: str) -> int:
        return len(re.findall(rf"(?<!\w){re.escape(name)}(?!\w)", text, flags=re.UNICODE))

    return any(occurrences(name, headline) > occurrences(name, title) for name in full_names if name)


def _source_isbn_relation(target: DurableTarget, source: FetchedSource) -> bool | None:
    """Matching ISBN proof, explicit ISBN conflict, or no comparable ISBN evidence.

    Use #531's plausibility rule and the existing ISBN-10/13 forms. A
    selected Book record (or fetched URL) takes precedence over unstructured
    text, so a different stated ISBN cannot be overridden by a passing mention.
    """
    # A learned ISBN marked discovery-only helps search but never proves
    # identity (#531), so it gives no proof and no conflict here.
    target_isbn = _checked_isbn13(identity_isbn(dict(target.record)))
    if not target_isbn:
        return None
    # Each side is checksum-validated and compared as one ISBN-13. The
    # ISBN-10/13 conversion recomputes a check digit, so comparing the
    # converted forms would equate two ISBN-13s that differ only in it.
    source_isbn = _checked_isbn13(source.isbn)
    if source_isbn:
        return source_isbn == target_isbn
    if page_prints_isbn(target_isbn, source.identity_evidence_text):
        return True
    # An address ISBN proves only beside a matching page title, and a
    # different one is no conflict: product addresses of other editions are
    # common, and a challenge page answers at the book's own address.
    if (
        _checked_isbn13(source.url_isbn) == target_isbn
        and source.title
        and _titles_match_for_authors(target.title, source.title, target.primary_authors)
    ):
        return True
    return None


def _source_isbn_conflict(target: DurableTarget, source: FetchedSource) -> bool:
    """A different plausible ISBN on a page with no target-author evidence.

    The ISBN then contradicts a title-only match (#755). A page that also
    names the target author may describe another edition of the same work,
    so it falls back to the ordinary title and author checks.
    """
    return _source_isbn_relation(target, source) is False and not _source_has_target_author_evidence(target, source)


def _source_has_target_identity_evidence(target: DurableTarget, source: FetchedSource) -> bool:
    return _source_isbn_relation(target, source) is True or _source_has_target_author_evidence(target, source)


def _source_has_target_author_evidence(target: DurableTarget, source: FetchedSource) -> bool:
    source_authors = _author_keys(source.primary_authors)
    if source_authors:
        return bool(_author_keys(target.primary_authors) & source_authors)
    return _subject_names_target_author(target, source) or _headline_names_target_author(target, source)


def _likely_author_typos(target: DurableTarget, sources: Sequence[FetchedSource]) -> tuple[AuthorTypoEvidence, ...]:
    """Report disagreements even when #755 accepts ISBN proof over the author.

    Any page with positive author evidence suppresses the advisory. Only
    ISBN-proven pages with extracted primary authors supply printed names.
    This is independent of the gate verdict and never edits its inputs.
    """
    # A reviewer byline is not book-author evidence here either.
    book_sources = [source if source.states_book_author else replace(source, primary_authors=()) for source in sources]
    if not sources or any(_source_has_target_author_evidence(target, source) for source in book_sources):
        return ()
    return tuple(
        AuthorTypoEvidence(
            url=url,
            stored_authors=tuple(_bounded(author, 200) for author in target.primary_authors[:10]),
            printed_authors=tuple(_bounded(author, 200) for author in source.primary_authors[:10]),
        )
        for source in sources
        # A page's own byline (a reviewer) is not the book's author.
        if source.states_book_author
        and _author_keys(source.primary_authors)
        and _source_isbn_relation(target, source) is True
        and (url := canonicalize_url(source.url))
    )


def _source_is_benign_authorless(target: DurableTarget, source: FetchedSource) -> bool:
    """An extracted-authorless source carrying positive target identity evidence.

    Such a source is benign identity evidence: its title or identity
    mismatch can never poison an agreeing subset the way a named source
    with a conflicting title or author can.
    """
    return not _author_keys(source.primary_authors) and _source_has_target_identity_evidence(target, source)


def _source_is_silent_on_identity(target: DurableTarget, source: FetchedSource) -> bool:
    """A source that carries no author evidence at all.

    It has no extracted primary authors, matching ISBN proof, or target
    author evidence in its fetched subject or headline. Such a source must
    not veto a source that does carry evidence. A source that names a
    different author in extracted metadata is not silent and keeps its veto;
    another name in prose is not an authorship claim (#866).
    """
    return (
        _source_isbn_relation(target, source) is None
        and not _author_keys(source.primary_authors)
        and not _source_has_target_author_evidence(target, source)
    )


_RECOVERED_TITLE_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "by",
        "but",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "with",
    }
)


_TRUNCATED_TITLE_MARKER_RE = re.compile(r"(?:…|\.\.\.)\s*$")


def _has_terminal_truncation_marker(title: str) -> bool:
    """Return whether a title ends with a supported truncation marker."""
    return _TRUNCATED_TITLE_MARKER_RE.search(str(title or "").strip()) is not None


def _recovered_title_is_useful(title: str) -> bool:
    """Return whether a recovered title has enough signal for identity use."""
    words = re.findall(r"\w+", title.casefold(), flags=re.UNICODE)
    if not words:
        return False
    if len(words) == 1:
        word = words[0]
        if word in _RECOVERED_TITLE_STOP_WORDS or (word.isascii() and len(word) < 4):
            return False
    return True


def _recover_truncated_title(title: str) -> str:
    """Recover intact bounded title text for discovery and identity checks.

    Terminal Unicode and ASCII ellipses mark the bounded value as truncated.
    The recovery keeps all text before the marker, except for an attached
    one-character terminal fragment when useful text precedes it.
    """
    value = str(title or "").strip()
    marker = _TRUNCATED_TITLE_MARKER_RE.search(value)
    if marker is None:
        return value

    recovered = value[: marker.start()].rstrip()
    if marker.start() > 0 and not value[marker.start() - 1].isspace():
        terminal = re.search(r"\w+$", recovered, flags=re.UNICODE)
        prefix = recovered[: terminal.start()].rstrip() if terminal is not None else ""
        if terminal is not None and len(terminal.group()) == 1 and _recovered_title_is_useful(prefix):
            recovered = prefix
    return recovered


def _grounding_title_key(value: str) -> str:
    """Return a normalized work key with leading articles ignored."""
    normalized = normalize_work_title(_recover_truncated_title(value))
    if _has_terminal_truncation_marker(value) and not _recovered_title_is_useful(normalized):
        return ""
    for article in ("the", "a", "an"):
        if normalized.startswith(f"{article} "):
            return normalized[len(article) :].lstrip()
    return normalized


def grounding_titles_match(left: str, right: str) -> bool:
    """Match normalized work titles exactly or by bounded whole-phrase containment.

    Containment is allowed only when the contained normalized title has at least
    two words. This is a plausible-work-title boundary: a generic one-word title
    such as ``Salt`` must not match ``The Salt Path``. Exact matches are always
    allowed, including exact one-word titles.
    """
    left_key = _grounding_title_key(left)
    right_key = _grounding_title_key(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True

    def contains_phrase(container: str, contained: str) -> bool:
        contained_words = contained.split()
        if len(contained_words) < 2:
            return False
        container_words = container.split()
        width = len(contained_words)
        return any(
            container_words[index : index + width] == contained_words
            for index in range(len(container_words) - width + 1)
        )

    return contains_phrase(left_key, right_key) or contains_phrase(right_key, left_key)


def _bounded(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _normalized_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _normalized_token_text(value: str) -> str:
    return _normalized_text(re.sub(r"[^\w]+", " ", value, flags=re.UNICODE))


def _prose_tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\w+", value.casefold(), flags=re.UNICODE))


def _description_copies_accepted_source_prose(
    description: str,
    sources: Sequence[AcceptedSource],
    *,
    max_copied_tokens: int | None = None,
) -> bool:
    """Reject exact or substantial contiguous copies of accepted source prose.

    ``max_copied_tokens`` caps the longest tolerated contiguous copy. The
    Russian text needs no cap because translation already breaks long runs;
    English written from English evidence does (issue #727).
    """
    description_tokens = _prose_tokens(description)
    if not description_tokens:
        return False
    for source in sources:
        for evidence in (source.display_text, source.subject_text):
            source_tokens = _prose_tokens(evidence)
            if not source_tokens:
                continue
            if _normalized_token_text(description) == _normalized_token_text(evidence):
                return True
            minimum_span = max(8, math.ceil(min(len(description_tokens), len(source_tokens)) * 0.6))
            if max_copied_tokens is not None:
                minimum_span = min(minimum_span, max_copied_tokens)
            previous_row = [0] * (len(source_tokens) + 1)
            longest = 0
            for description_token in description_tokens:
                current_row = [0] * (len(source_tokens) + 1)
                for source_index, source_token in enumerate(source_tokens, start=1):
                    if description_token == source_token:
                        current_row[source_index] = previous_row[source_index - 1] + 1
                        longest = max(longest, current_row[source_index])
                previous_row = current_row
            if longest >= minimum_span:
                return True
    return False


# The longest contiguous run an English Description may share with accepted
# source prose: room for a long title plus an author, not for a sentence.
MAX_ENGLISH_COPIED_TOKENS = 12


def screen_synthesized_description(description: str, sources: Sequence[AcceptedSource]) -> tuple[str, str, str]:
    """Apply the gate a synthesized Russian description must clear.

    Returns the validated text with an empty reason, or an empty text with the
    refusal reason and its detail. The stage and description model
    qualification (issue #843) share this, so a qualified model is judged by
    the gate production applies.
    """
    validated, validation_reason = validate_content_description(description)
    if not validated:
        return "", REASON_INVALID_DESCRIPTION, str(validation_reason)
    if _description_copies_accepted_source_prose(validated, sources):
        return "", REASON_INVALID_DESCRIPTION, "rejected as copied publisher/review prose"
    # Depth acceptance (issue #673): safe prose that clears neither the
    # character nor the sentence floor is never a successful grounded
    # outcome. The attempt is reported with its own reason; whatever the
    # record already holds is decided downstream, where an unsuccessful
    # attempt never destroys usable text.
    depth = assess_description_depth(validated)
    if not depth["meets_floor"]:
        return (
            "",
            REASON_INSUFFICIENT_DEPTH,
            (
                f"below depth floor: {depth['char_count']} chars, "
                f"{depth['sentence_count']} sentences "
                f"(floor {DEPTH_MIN_CHARS} chars, {DEPTH_MIN_SENTENCES} sentences)"
            ),
        )
    return validated, "", ""


def _validated_english(value: str, sources: Sequence[AcceptedSource]) -> str:
    """Return a safe English Description, or "" to drop it.

    Never refuses the outcome: the Russian description stands on its own
    (issue #727).
    """
    validated, _reason = validate_english_description(value)
    if validated is None:
        return ""
    if _description_copies_accepted_source_prose(validated, sources, max_copied_tokens=MAX_ENGLISH_COPIED_TOKENS):
        return ""
    return validated


_SPAN_SENTENCE_BREAK = re.compile(r"[.!?]\s")


def _span_is_single_sentence(evidence_span: str) -> bool:
    """Return whether the span is one sentence.

    The model chooses where a span begins and ends, so without this a span can
    be widened until it happens to contain the title. A verbatim 449-character
    span of the real bookshop page reaches the title four sentences after
    "bestselling author Aaron Mahnke", which made the work-level check
    meaningless (issue #554).

    An abbreviation such as "U.S." reads as a break here, so a span containing
    one is refused. That is the safe direction: a missing claim costs the
    editor nothing.
    """
    return not _SPAN_SENTENCE_BREAK.search(evidence_span.strip().rstrip(".!?"))


def _span_names_target_author(evidence_span: str, primary_authors: Sequence[str]) -> bool:
    """Return whether the span names one of the work's authors."""
    normalized_span = f" {_normalized_token_text(evidence_span)} "
    for author in primary_authors:
        for token in _normalized_token_text(normalize_author_identity(str(author))).split():
            if len(token) > 2 and f" {token} " in normalized_span:
                return True
    return False


def _span_supports_work_claim(evidence_span: str, work_title: str, primary_authors: Sequence[str]) -> bool:
    """Return whether a supporting span makes its claim about the work itself.

    Three conditions, all required:

    1. The span is one sentence, so it cannot be widened to borrow a title from
       elsewhere on the page.
    2. The normalized work title appears in it as distinct tokens.
    3. It does not name one of the work's authors.

    The third condition is what separates "Exhumed is a New York Times
    bestseller" from "Aaron Mahnke, bestselling author of The World of Lore,
    returns with Exhumed". Both name the work; only the first says the work is
    the bestseller. A sentence naming both the work and the author leaves the
    claim ambiguous, and an ambiguous claim is refused rather than resolved,
    because a false claim costs an acquisition decision while a missing one
    costs nothing.

    This refuses some genuine spans that merely mention the author in passing.
    That is accepted: zero claims from a page is a correct outcome.
    """
    if not evidence_span.strip():
        return False
    if not _span_is_single_sentence(evidence_span):
        return False
    normalized_title = _normalized_token_text(normalize_work_title(work_title))
    if not normalized_title:
        return False
    if f" {normalized_title} " not in f" {_normalized_token_text(evidence_span)} ":
        return False
    return not _span_names_target_author(evidence_span, primary_authors)


def _span_present_in_evidence(evidence_span: str, evidence_text: str) -> bool:
    """Return whether the supporting span occurs in the accepted source text."""
    normalized_span = _normalized_text(evidence_span)
    return bool(normalized_span) and normalized_span in _normalized_text(evidence_text)


def _bounded_int(value: Any, default: int) -> int:
    """Return a tolerant integer for persisted ordering metadata."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_SENTENCE_END_CHARS = frozenset(".!?。！？")
_SENTENCE_CLOSING_CHARS = frozenset("\\\"'»”’)]")


def _bounded_subject(value: Any, limit: int = MAX_SOURCE_SUBJECT_TEXT) -> str:
    """Bound source prose without cutting a complete sentence."""
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    end = 0
    for index, character in enumerate(text):
        if character not in _SENTENCE_END_CHARS:
            continue
        candidate_end = index + 1
        while candidate_end < len(text) and text[candidate_end] in _SENTENCE_CLOSING_CHARS:
            candidate_end += 1
        if candidate_end > limit:
            break
        if candidate_end == len(text) or text[candidate_end].isspace():
            end = candidate_end
    return text[:end].rstrip() if end else ""


def _bounded_mapping(value: Any, *, depth: int = 0) -> dict[str, Any]:
    """Keep only small JSON-like metadata mappings.

    Classification and title evidence are metadata, not source prose.  This
    helper still treats them as untrusted input and prevents an adapter from
    turning an audit record into an unbounded copy of a fetched page.
    """
    if not isinstance(value, Mapping) or depth > MAX_TITLE_EVIDENCE_DEPTH:
        return {}
    result: dict[str, Any] = {}
    for raw_key in sorted(value, key=lambda item: str(item))[:MAX_METADATA_KEYS]:
        key = _bounded(raw_key, MAX_METADATA_KEY_LENGTH)
        if not key:
            continue
        raw_value = value[raw_key]
        if isinstance(raw_value, Mapping):
            nested = _bounded_mapping(raw_value, depth=depth + 1)
            if nested:
                result[key] = nested
        elif isinstance(raw_value, list | tuple):
            bounded_list: list[Any] = []
            for item in raw_value[:10]:
                if isinstance(item, Mapping):
                    nested = _bounded_mapping(item, depth=depth + 1)
                    if nested:
                        bounded_list.append(nested)
                elif isinstance(item, str) and item.strip():
                    bounded_list.append(_bounded(item, MAX_METADATA_VALUE_LENGTH))
            if bounded_list:
                result[key] = bounded_list
        elif isinstance(raw_value, bool | int | float):
            result[key] = raw_value
        elif isinstance(raw_value, str) and raw_value.strip():
            result[key] = _bounded(raw_value, MAX_METADATA_VALUE_LENGTH)
    return result


def _bounded_title_evidence(value: Any) -> Mapping[str, Any] | None:
    bounded = _bounded_mapping(value)
    return bounded or None


def _classification_evidence_from_mapping(
    value: Mapping[str, Any] | None,
    *,
    url: str,
    source_kind: str,
    source_family: str,
    owner_id: str,
    source_language: str,
    classification: str,
) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    nested = raw.get("classification_evidence")
    if not isinstance(nested, Mapping):
        nested = raw.get("classification_metadata")
    nested = nested if isinstance(nested, Mapping) else {}
    metadata = nested.get("source_metadata")
    if not isinstance(metadata, Mapping):
        metadata = raw.get("source_metadata")
    source_metadata = _bounded_mapping(metadata)
    defaults = {
        "source_kind": source_kind,
        "source_family": source_family,
        "owner_id": owner_id,
        "source_language": source_language,
    }
    for key, default in defaults.items():
        if default and key not in source_metadata:
            source_metadata[key] = _bounded(default, MAX_METADATA_VALUE_LENGTH)
    canonical = canonicalize_url(url)
    host = canonicalize_hostname(
        _bounded(
            nested.get("host", raw.get("host", urlsplit(canonical).hostname if canonical else "")),
            MAX_METADATA_VALUE_LENGTH,
        )
    )
    path_rule = _bounded(nested.get("path_rule", raw.get("path_rule", raw.get("path_prefix", ""))), 200)
    metadata_version = _bounded(nested.get("policy_metadata_version", raw.get("policy_metadata_version", "")), 100)
    resolved_classification = _bounded(
        nested.get("classification", raw.get("classification", classification)) or classification,
        50,
    )
    return {
        "host": host,
        "path_rule": path_rule,
        "policy_metadata_version": metadata_version,
        "classification": resolved_classification,
        "source_metadata": source_metadata,
    }


def _classification_evidence_for_fetched(source: FetchedSource, canonical_url: str) -> dict[str, Any]:
    return _classification_evidence_from_mapping(
        source.classification_evidence,
        url=canonical_url,
        source_kind=source.source_kind,
        source_family=source.source_family,
        owner_id=source.owner_id,
        source_language=source.source_language,
        classification=source.classification or "classified",
    )


def _candidate_id(record: Mapping[str, Any]) -> str:
    return str(record.get("candidate_id", "")).strip()


def resolve_durable_target(
    record: Mapping[str, Any],
    *,
    history: Sequence[Mapping[str, Any]] = (),
    keyed_history: KeyedHistory | None = None,
    aliases: Mapping[str, Any] | None = None,
    durable_candidates: Sequence[Mapping[str, Any]] | None = None,
) -> TargetResolution:
    """Resolve one durable target through the existing #443 seams only."""
    table = dict(aliases) if isinstance(aliases, Mapping) else {"aliases": {}, "candidates": {}}
    try:
        resolved_id = resolve_candidate_id(dict(record), table)
        if history:
            _matched, history_id = resolve_history_match(
                dict(record), keyed_history if keyed_history is not None else [dict(item) for item in history], table
            )
            resolved_id = history_id or resolved_id
    except (TypeError, ValueError):
        return TargetResolution(None, REASON_AMBIGUITY)
    if not resolved_id:
        return TargetResolution(None, REASON_AMBIGUITY)

    known_ids: set[str] = set()
    candidates_table = table.get("candidates", {})
    if isinstance(candidates_table, Mapping):
        known_ids.update(str(key) for key in candidates_table)
    known_ids.update(_candidate_id(item) for item in history if _candidate_id(item))
    if durable_candidates is not None:
        known_ids.update(_candidate_id(item) for item in durable_candidates if _candidate_id(item))
    if known_ids and str(resolved_id) not in known_ids:
        return TargetResolution(None, REASON_AMBIGUITY)

    title = str(record.get("title", "")).strip()
    authors = record.get("primary_authors", [])
    if isinstance(authors, str):
        authors = [authors]
    if not title or not isinstance(authors, Sequence) or not _author_keys(authors):
        return TargetResolution(None, REASON_AMBIGUITY)
    return TargetResolution(
        DurableTarget(
            str(resolved_id), title, tuple(str(author).strip() for author in authors if str(author).strip()), record
        ),
    )


def _source_identity_mention(source: FetchedSource) -> tuple[dict[str, Any] | None, str]:
    """Return the exact identity mention used by the route, never a rendering."""
    if source.title_type in {"generated_rendering", "marked_rendering", "transliteration", "literal_translation"}:
        return None, REASON_TITLE_MISMATCH
    if source.title_evidence_route == "verified_published_title":
        if source.title_type and source.title_type != "sourced_published":
            return None, REASON_TITLE_MISMATCH
        evidence = source.title_evidence
        if not isinstance(evidence, Mapping) or not title_is_identity_evidence(dict(evidence)):
            return None, REASON_TITLE_MISMATCH
        provenance = evidence.get("english_title_provenance")
        mapping = provenance.get("mapping") if isinstance(provenance, Mapping) else None
        if not isinstance(mapping, Mapping):
            return None, REASON_TITLE_MISMATCH
        original_title = str(mapping.get("original_title", "")).strip()
        # The publication evidence proves only the title mapping.  The author
        # gate must use metadata fetched from this source, not authors copied
        # from the candidate's publication evidence.
        return {"title": original_title, "primary_authors": list(source.primary_authors)}, ""
    if source.title_evidence_route != "direct_original":
        return None, REASON_TITLE_MISMATCH
    # A translated/published field cannot silently become the direct title.
    title = source.title
    if not title:
        return None, REASON_TITLE_MISMATCH
    return {"title": title, "primary_authors": list(source.primary_authors)}, ""


def _title_readings(title: str, authors: Iterable[str]) -> tuple[str, ...]:
    """Return a page title, plus its text after a leading target-author name.

    Review pages print ``Juma Iraki: For mine sønner``, and the work key keeps
    only the text before the first colon, which is the author's name (issue
    #757).  When that text is exactly one target author's name, the title after
    the colon is a second reading.  The full title stays a reading too: a
    corporate author can also open the title, as ``H Mart: The Cookbook``
    does for *Cooking with H Mart*.  Any other colon title has one reading.
    """
    headline = re.fullmatch(r"([^:：]+)[:：](.+)", title.strip(), flags=re.DOTALL)
    if headline is None or not headline.group(2).strip():
        return (title,)
    head_key = normalize_author_identity(_author_match_display(headline.group(1).strip()))
    if head_key and head_key in _author_keys(authors):
        return (title, headline.group(2).strip())
    return (title,)


def _titles_match_for_authors(left: str, right: str, authors: Iterable[str]) -> bool:
    """Match two titles when any of their target-author readings match."""
    authors = tuple(authors)
    return any(
        grounding_titles_match(left_reading, right_reading)
        for left_reading in _title_readings(left, authors)
        for right_reading in _title_readings(right, authors)
    )


def _identity_reason(target: DurableTarget, source: FetchedSource) -> str:
    isbn_relation = _source_isbn_relation(target, source)
    if isbn_relation is True:
        # ISBN proof replaces title wording, but not the verified-title
        # route's own evidence: the stage artifact validates that route on
        # persistence, and a source accepted here but refused there would
        # discard the whole stage.
        if source.title_evidence_route == "verified_published_title" and not (
            isinstance(source.title_evidence, Mapping) and title_is_identity_evidence(dict(source.title_evidence))
        ):
            return REASON_TITLE_MISMATCH
        return ""
    mention, route_reason = _source_identity_mention(source)
    if mention is None:
        return route_reason or REASON_TITLE_MISMATCH
    if not _titles_match_for_authors(target.title, str(mention.get("title", "")), target.primary_authors):
        return REASON_TITLE_MISMATCH
    if isbn_relation is False and not _source_has_target_author_evidence(target, source):
        return REASON_SOURCE_IDENTITY_DISAGREEMENT
    target_authors = _author_keys(target.primary_authors)
    source_authors = _author_keys(mention.get("primary_authors", []))
    if not target_authors:
        return REASON_AUTHOR_MISMATCH
    if not source_authors:
        # A matching title with no extracted source author is unknown, not a conflict.
        return ""
    if not target_authors & source_authors:
        return REASON_AUTHOR_MISMATCH
    return ""


def _subject_signature(source: FetchedSource) -> frozenset[str]:
    explicit = normalize_work_title(source.subject_key) if source.subject_key else ""
    if explicit:
        return frozenset(token for token in explicit.split() if len(token) >= 3)
    common = {
        "the",
        "and",
        "book",
        "work",
        "author",
        "about",
        "explores",
        "examines",
        "discusses",
        "describes",
        "presents",
        "this",
        "with",
        "from",
    }
    return frozenset(
        token for token in normalize_work_title(source.subject_text).split() if len(token) >= 4 and token not in common
    )


def _subject_reason(source: FetchedSource) -> str:
    if (not source.subject_text.strip() and not source.subject_key.strip()) or len(_subject_signature(source)) < 1:
        return REASON_INSUFFICIENT_SUBJECT
    return ""


def _source_pair_identity_disagrees(target: DurableTarget, first: FetchedSource, second: FetchedSource) -> bool:
    """Reject title conflicts and author conflicts between source mentions.

    A source carries identity evidence when its extracted primary authors
    include a target author, or when its fetched subject or headline names one. Two
    sources agree on identity when BOTH carry that evidence - the same
    evidence standard the single-source path applies.  A named source that
    names a different author therefore conflicts with every parsed source,
    and a benign authorless source is handled by the gate. Matching ISBN
    proof replaces the title/author standard for that source only.
    """
    if _source_isbn_relation(target, first) is True or _source_isbn_relation(target, second) is True:
        # ISBN proof replaces title/author wording for that page only. Its
        # peer must still independently name the target book.
        return any(
            _identity_reason(target, source) or not _source_has_target_identity_evidence(target, source)
            for source in (first, second)
        )
    if any(_source_isbn_conflict(target, source) for source in (first, second)):
        return True
    first_mention, first_reason = _source_identity_mention(first)
    second_mention, second_reason = _source_identity_mention(second)
    if first_mention is None or second_mention is None or first_reason or second_reason:
        return True
    if not _titles_match_for_authors(
        str(first_mention.get("title", "")),
        str(second_mention.get("title", "")),
        target.primary_authors,
    ):
        return True
    return not (
        _source_has_target_author_evidence(target, first) and _source_has_target_author_evidence(target, second)
    )


_GENERIC_DISCOVERY_ROUTE_KINDS = frozenset({"category", "bestseller", "new_release", "author", "catalog", "search"})


def _discovery_relevance_tier(
    target: DurableTarget | Mapping[str, Any],
    result: DiscoveryResult | Mapping[str, Any],
) -> int:
    """Rank one classified discovery result for the candidate fetch budget.

    Tier 0 requires both a normalized work-title match and a target-author
    token inside the bounded discovery title and is denied to known generic
    routes.  Tier 1 is a product route that did not meet tier 0.  Tier 2 is
    everything else, including generic routes.  The evaluation is
    deterministic: no stemming, translation, transliteration, fuzzy, or
    model calls.
    """
    if isinstance(target, DurableTarget):
        title = target.title
        authors = target.primary_authors
    else:
        title = str(target.get("title", "")).strip()
        raw_authors = target.get("primary_authors", [])
        if isinstance(raw_authors, str):
            raw_authors = [raw_authors]
        authors = tuple(str(author).strip() for author in raw_authors if str(author).strip())
    discovery = result if isinstance(result, DiscoveryResult) else DiscoveryResult.from_mapping(result)
    route = discovery.route_kind.casefold()
    generic_route = route in _GENERIC_DISCOVERY_ROUTE_KINDS
    terms = _author_subject_terms(authors)
    author_match = bool(terms) and any(
        re.search(rf"(?<!\w){re.escape(term)}(?!\w)", _fold_author_evidence_text(discovery.title), flags=re.UNICODE)
        for term in terms
    )
    if not generic_route and _titles_match_for_authors(title, discovery.title, authors) and author_match:
        return 0
    if route == "product":
        return 1
    return 2


def _discovery_ordering_key(
    relevance_tier: int,
    source_kind: str,
    discovery_ordinal: int,
    canonical_url: str,
    origin: str = "discovery",
) -> tuple[int, int, int, str]:
    """Return the shared ordering key for eligible classified results.

    The same key orders the stage fetch budget and the persisted artifact
    validation.  Legacy records default to tier 2 and ordinal 0, which sorts
    identically to the historical ``(preference, canonical URL)`` rule.
    """
    if origin == "row_evidence":
        return -1, 0, int(discovery_ordinal), canonicalize_url(canonical_url)
    return (
        int(relevance_tier),
        _SOURCE_PREFERENCE.get(str(source_kind).casefold(), 99),
        int(discovery_ordinal),
        canonicalize_url(canonical_url),
    )


def _source_sort_key(source: FetchedSource, canonical_url: str) -> tuple[int, str, str]:
    if source.origin == "row_evidence":
        return -1, f"{source.discovery_ordinal:02d}", canonical_url
    preference = source.source_preference
    if preference == 99:
        preference = _SOURCE_PREFERENCE.get(source.source_kind.casefold(), 99)
    return preference, canonical_url, source.owner_id


def _accepted_source(source: FetchedSource, canonical_url: str) -> AcceptedSource:
    return AcceptedSource(
        origin_recorded=source.origin_recorded,
        origin=source.origin,
        signal_own=source.signal_own,
        canonical_url=canonical_url,
        source_kind=_bounded(source.source_kind, MAX_METADATA_VALUE_LENGTH),
        source_family=_bounded(source.source_family, MAX_METADATA_VALUE_LENGTH),
        owner_id=_bounded(source.owner_id, MAX_METADATA_VALUE_LENGTH),
        source_language=_bounded(source.source_language, MAX_METADATA_VALUE_LENGTH),
        title_evidence_route=_bounded(source.title_evidence_route, 100),
        checked_date=_bounded(source.checked_date, 50),
        raw_title=_bounded(source.title, MAX_EVIDENCE_METADATA_TEXT),
        raw_authors=tuple(_bounded(author, 200) for author in source.primary_authors[:10]),
        subject_key=_bounded(" ".join(sorted(_subject_signature(source))), MAX_EVIDENCE_METADATA_TEXT),
        display_text=_bounded_subject(source.display_text),
        subject_text=_bounded_subject(source.subject_text),
        isbn=_bounded(source.isbn, 50),
        title_evidence=_bounded_title_evidence(source.title_evidence),
        classification_evidence=_classification_evidence_for_fetched(source, canonical_url),
        relevance_tier=int(source.relevance_tier),
        discovery_ordinal=int(source.discovery_ordinal),
        discovery_title=_bounded(source.discovery_title, MAX_DISCOVERY_TITLE_TEXT),
    )


def evaluate_grounding_gate(
    target: DurableTarget | Mapping[str, Any],
    sources: Sequence[Mapping[str, Any] | FetchedSource],
) -> GroundingGateResult:
    """Apply the universal one-to-three-source gate without fuzzy widening.

    When no valid independent set exists, an identity disagreement between the
    parsed sources is not always a refusal.  If no rejected checkable source is
    dangerous, the gate grounds from the largest eligible agreeing subset of
    the parsed sources.  An eligible subset is pairwise consistent (no identity
    or subject disagreement inside it), contains at least one source with
    target-author evidence or matching ISBN proof, and excludes only parsed
    sources that agree with every member or are benign authorless or silent
    sources. Silent sources never join through subset selection. The accepted
    single source prefers a named or ISBN-proven member, then a member whose
    fetched subject or headline names the target author; every other parsed
    source is recorded as fewer-than-two.
    """
    if isinstance(target, DurableTarget):
        resolved_target = target
    else:
        resolution = resolve_durable_target(target, durable_candidates=[target])
        if not resolution.resolved or resolution.target is None:
            return GroundingGateResult(False, REASON_AMBIGUITY)
        resolved_target = resolution.target

    parsed: list[tuple[FetchedSource, str]] = []
    individually_checkable: list[tuple[FetchedSource, str]] = []
    rejected: list[RejectedSource] = []
    for raw_source in sources:
        source = FetchedSource.from_mapping(raw_source)
        url = canonicalize_url(source.url)
        if not url:
            rejected.append(RejectedSource("", REASON_INVALID_SOURCE))
            continue
        if source.is_search_snippet:
            rejected.append(RejectedSource(url, REASON_SEARCH_SNIPPET))
            continue
        if source.classification.casefold() in {"unknown", "unclassifiable", ""}:
            rejected.append(RejectedSource(url, REASON_SOURCE_DENYLISTED))
            continue
        if not source.source_kind or not source.source_family or not source.owner_id:
            rejected.append(RejectedSource(url, REASON_INVALID_SOURCE))
            continue
        subject_reason = _subject_reason(source)
        if subject_reason:
            rejected.append(RejectedSource(url, subject_reason))
            continue
        if not source.checked_date:
            rejected.append(RejectedSource(url, REASON_INVALID_SOURCE))
            continue

        # Keep sources that passed every non-identity gate long enough to detect
        # a real disagreement.  A disagreeing source may be excluded from a
        # valid three-source or two-source set, but it must never be silently
        # discarded in order to create a single-source result.
        individually_checkable.append((source, url))
        identity_reason = _identity_reason(resolved_target, source)
        if identity_reason:
            rejected.append(RejectedSource(url, identity_reason))
            continue
        parsed.append((source, url))

    def identity_disagreement(items: Sequence[tuple[FetchedSource, str]]) -> bool:
        return any(
            _source_pair_identity_disagrees(resolved_target, first[0], second[0])
            for first, second in itertools.combinations(items, 2)
        )

    def subject_disagreement(items: Sequence[tuple[FetchedSource, str]]) -> bool:
        non_encyclopedia_sources = [item for item in items if item[0].source_family.casefold() != "encyclopedia"]
        if not non_encyclopedia_sources:
            return False
        if len(non_encyclopedia_sources) < len(items):
            # An identity-verified encyclopedia page may corroborate a work
            # without forcing its own subject vocabulary onto another source.
            # Other source families must still agree with each other, so an
            # encyclopedia page cannot hide a conflict between them.
            items = non_encyclopedia_sources
        signatures = [_subject_signature(source) for source, _url in items]
        if not signatures:
            return False
        common = set(signatures[0])
        for signature in signatures[1:]:
            common.intersection_update(signature)
        return not common

    def legacy_subject_corroboration(
        items: Sequence[tuple[FetchedSource, str]],
        checkable: Sequence[tuple[FetchedSource, str]],
    ) -> tuple[tuple[FetchedSource, str], ...] | None:
        """Keep the unchanged #483 direct-seam case without granting identity.

        The old test supplies one named source and one authorless source with
        identical fetched subject text.  The named source establishes identity;
        the authorless source is retained only as corroborating subject evidence.
        This path is limited to that exact two-source shape and is never used by
        identity-agreement pair selection.
        """
        if any(source.signal_own for source, _url in items):
            return None
        if len(items) != 2 or len(checkable) != 2:
            return None
        urls = [url for _source, url in items]
        owners = [source.owner_id for source, _url in items]
        if len(set(urls)) != 2 or len(set(owners)) != 2:
            return None
        first, second = items
        first_authors = _author_keys(first[0].primary_authors)
        second_authors = _author_keys(second[0].primary_authors)
        if bool(first_authors) == bool(second_authors):
            return None
        missing = first[0] if not first_authors else second[0]
        named = second[0] if not first_authors else first[0]
        if _source_has_target_identity_evidence(resolved_target, missing):
            return None
        if not _source_has_target_identity_evidence(resolved_target, named):
            return None
        if not missing.subject_text.strip() or missing.subject_text != named.subject_text:
            return None
        return tuple(sorted(items, key=lambda item: _source_sort_key(*item)))

    def _is_consistent_group(items: Sequence[tuple[FetchedSource, str]]) -> bool:
        """A parsed group is consistent when no pair disagrees on identity or subject.

        Shared by the valid 3-source / 2-source path and the agreeing-subset
        fallback so neither one duplicates the pair-wise checks.
        """
        if identity_disagreement(items):
            return False
        if subject_disagreement(items):
            return False
        return True

    def _deterministic_group_key(
        group: Sequence[tuple[FetchedSource, str]],
    ) -> tuple[tuple[int, str, str], ...]:
        """Stable sort key shared by every group selection in the gate."""
        return tuple(
            _source_sort_key(source, url) for source, url in sorted(group, key=lambda item: _source_sort_key(*item))
        )

    def _excluded_checkable_is_compatible(
        item: tuple[FetchedSource, str],
        members: Sequence[tuple[FetchedSource, str]],
    ) -> bool:
        """An excluded individually-checkable source is compatible with the subset.

        Subject compatibility: adding it must not introduce subject disagreement.
        Identity compatibility: it agrees with every member, OR it has no
        extracted primary authors and has positive target identity evidence,
        OR its own identity failure is a title
        mismatch, OR it is silent on identity. A different-book source is
        irrelevant evidence and must not veto an otherwise agreeing subset,
        while a matching title with a conflicting author is genuine ambiguity
        and keeps its veto.
        """
        source = item[0]
        if _identity_reason(resolved_target, source) == REASON_TITLE_MISMATCH:
            return True
        if _source_isbn_conflict(resolved_target, source):
            return False
        if subject_disagreement((*members, item)):
            return False
        if _source_is_silent_on_identity(resolved_target, source) and any(
            _source_has_target_identity_evidence(resolved_target, member[0]) for member in members
        ):
            # One evidenced source suffices (#866). The silent peer stays
            # excluded: pair consistency still requires evidence on both pages.
            # Subject disagreement is checked first because silence is an
            # identity property and must not bypass a subject contradiction
            # for a title-matching source.
            return True
        if all(not _source_pair_identity_disagrees(resolved_target, source, member[0]) for member in members):
            return True
        return _source_is_benign_authorless(resolved_target, source)

    def _select_consistent_group(
        candidates: Sequence[tuple[FetchedSource, str]],
        sizes: Iterable[int],
        *,
        require_independence: bool,
        extra_eligibility: Callable[[tuple[tuple[FetchedSource, str], ...]], bool] | None = None,
    ) -> tuple[tuple[FetchedSource, str], ...] | None:
        """Return the deterministic best consistent group, or ``None``.

        Enumerates combinations, applies the shared `_is_consistent_group`
        predicate, and picks the deterministic best group in one place so the
        valid three-source / two-source path and the agreeing-subset fallback
        do not duplicate selector logic.  Independence (distinct canonical
        URLs and distinct owner IDs) is applied only when
        ``require_independence`` is true.  An optional
        ``extra_eligibility`` predicate lets the agreeing-subset fallback add
        its identity-evidence and excluded-checkable compatibility rules on
        top of consistency.
        """
        for size in sizes:
            eligible: list[tuple[tuple[FetchedSource, str], ...]] = []
            for combination in itertools.combinations(candidates, size):
                if require_independence:
                    if any(item[0].signal_own for item in combination):
                        continue
                    urls = [item[1] for item in combination]
                    owners = [item[0].owner_id for item in combination]
                    if len(set(urls)) != size or len(set(owners)) != size:
                        continue
                if not _is_consistent_group(combination):
                    continue
                if extra_eligibility is not None and not extra_eligibility(combination):
                    continue
                eligible.append(combination)
            if eligible:
                return min(eligible, key=_deterministic_group_key)
        return None

    def _agreeing_subset_eligibility(
        combination: tuple[tuple[FetchedSource, str], ...],
    ) -> bool:
        """Identity-evidence and excluded-checkable rules for agreeing subsets.

        Considers every individually-checkable source outside the candidate
        subset so a benign identity-rejected source such as the Amazon
        product page keeps its original ``title_mismatch`` reason instead
        of poisoning a valid Bookshop singleton.
        """
        if not any(_source_has_target_identity_evidence(resolved_target, source) for source, _url in combination):
            return False
        combination_ids = {id(member[0]) for member in combination}
        excluded = [item for item in individually_checkable if id(item[0]) not in combination_ids]
        if any(not _excluded_checkable_is_compatible(item, combination) for item in excluded):
            return False
        return True

    # Prefer a valid three-source set, then a valid two-source set.  Only
    # canonical URL and owner identity define independence.  Source family is
    # retained as observed metadata, not as an acceptance condition.
    selected_valid_set = _select_consistent_group(parsed, (3, 2), require_independence=True)

    if selected_valid_set is not None:
        accepted_sources = tuple(
            _accepted_source(source, url)
            for source, url in sorted(selected_valid_set, key=lambda item: _source_sort_key(*item))
        )
        independence = SourceIndependenceEvidence.from_sources(accepted_sources)
        selected_source_ids = {id(source) for source, _url in selected_valid_set}
        for source, url in individually_checkable:
            if id(source) in selected_source_ids:
                continue
            reason = REASON_FEWER_THAN_TWO
            if any(
                _source_pair_identity_disagrees(resolved_target, source, selected_source)
                for selected_source, _url in selected_valid_set
            ):
                reason = REASON_SOURCE_IDENTITY_DISAGREEMENT
            elif subject_disagreement((*selected_valid_set, (source, url))):
                reason = REASON_SUBJECT_DISAGREEMENT
            rejected.append(RejectedSource(url, reason))
        return GroundingGateResult(
            True,
            "",
            accepted_sources=accepted_sources,
            rejected_sources=tuple(rejected),
            source_independence_evidence=independence,
        )

    compatibility_set = legacy_subject_corroboration(tuple(parsed), tuple(individually_checkable))
    if compatibility_set is not None:
        accepted_sources = tuple(_accepted_source(source, url) for source, url in compatibility_set)
        return GroundingGateResult(
            True,
            "",
            accepted_sources=accepted_sources,
            source_independence_evidence=SourceIndependenceEvidence.from_sources(accepted_sources),
            rejected_sources=tuple(rejected),
        )

    # Single source with author or ISBN evidence and no other checkable sources
    # remains a direct legacy case. Author evidence can come from the fetched
    # subject or headline even when no primary authors were extracted.
    if parsed and len(individually_checkable) == 1:
        selected_single = parsed[0]
        if _source_has_target_identity_evidence(resolved_target, selected_single[0]):
            accepted_single = (_accepted_source(*selected_single),)
            return GroundingGateResult(
                True,
                "",
                accepted_sources=accepted_single,
                source_independence_evidence=SourceIndependenceEvidence.from_sources(accepted_single),
                rejected_sources=tuple(rejected),
            )

    selected_subset = _select_consistent_group(
        parsed,
        range(len(parsed), 0, -1),
        require_independence=False,
        extra_eligibility=_agreeing_subset_eligibility,
    )
    if selected_subset is not None:
        # Preserve the named/ISBN-proven preference; subject or headline
        # evidence can also ground a singleton beside silent peers (#866).
        preferred = tuple(
            item
            for item in selected_subset
            if _author_keys(item[0].primary_authors) or _source_isbn_relation(resolved_target, item[0]) is True
        )
        selected = min(
            preferred
            or tuple(
                item for item in selected_subset if _source_has_target_identity_evidence(resolved_target, item[0])
            ),
            key=lambda item: _source_sort_key(*item),
        )
        accepted_agreeing = (_accepted_source(*selected),)
        remaining = tuple(
            RejectedSource(url, REASON_FEWER_THAN_TWO) for source, url in parsed if source is not selected[0]
        )
        return GroundingGateResult(
            True,
            "",
            accepted_sources=accepted_agreeing,
            rejected_sources=tuple(rejected) + remaining,
            source_independence_evidence=SourceIndependenceEvidence.from_sources(accepted_agreeing),
        )

    def dangerous(source: FetchedSource) -> bool:
        """A rejected source whose evidence still conflicts with the parsed set.

        A rejected source with no extracted authors whose text names the
        target author is benign, not dangerous. A different-book source
        whose title does not match is also not dangerous, since it is
        irrelevant and must not veto an otherwise agreeing subset. A silent
        source with no author evidence is also not dangerous, since it is
        silent on identity.
        """
        if any(source is parsed_source for parsed_source, _url in parsed):
            return False
        if _source_is_benign_authorless(resolved_target, source):
            return False
        if _identity_reason(resolved_target, source) == REASON_TITLE_MISMATCH:
            return False
        if _source_is_silent_on_identity(resolved_target, source):
            return False
        return True

    def dangerous_checkable_conflict() -> bool:
        """A disagreeing checkable pair with at least one dangerous member.

        The pair must involve a parsed source.  A conflict between two
        rejected sources that fails only the shared evidence standard is
        already refused through their individual mismatch reasons, and
        overriding those with the pair-level code would hide which source
        mismatched the target.
        """
        parsed_ids = {id(source) for source, _url in parsed}
        return any(
            _source_pair_identity_disagrees(resolved_target, first[0], second[0])
            and (dangerous(first[0]) or dangerous(second[0]))
            and (id(first[0]) in parsed_ids or id(second[0]) in parsed_ids)
            for first, second in itertools.combinations(individually_checkable, 2)
        )

    if parsed and identity_disagreement(tuple(parsed)):
        reason = REASON_SOURCE_IDENTITY_DISAGREEMENT
    elif parsed and subject_disagreement(tuple(parsed)):
        reason = REASON_SUBJECT_DISAGREEMENT
    elif dangerous_checkable_conflict():
        reason = REASON_SOURCE_IDENTITY_DISAGREEMENT
    elif (
        parsed
        and len(individually_checkable) >= 2
        and any(not _author_keys(source.primary_authors) for source, _url in individually_checkable)
        and any(item.reason_code == REASON_AUTHOR_MISMATCH for item in rejected)
    ):
        # Preserve the pair-level disagreement when the only named source conflicts with the target.
        reason = REASON_SOURCE_IDENTITY_DISAGREEMENT
    elif parsed and any(not _source_has_target_identity_evidence(resolved_target, source) for source, _url in parsed):
        reason = REASON_AUTHOR_MISMATCH
    elif len(parsed) >= 2:
        # All individually verified sources agree, but repeated owners are
        # the only remaining reason that no independent set exists.  Keep
        # one deterministic source and record the rest as fewer-than-two.
        selected_repeated = min(parsed, key=lambda item: _source_sort_key(*item))
        accepted_repeated = (_accepted_source(*selected_repeated),)
        repeated = tuple(
            RejectedSource(url, REASON_FEWER_THAN_TWO) for source, url in parsed if source is not selected_repeated[0]
        )
        return GroundingGateResult(
            True,
            "",
            accepted_sources=accepted_repeated,
            rejected_sources=tuple(rejected) + repeated,
            source_independence_evidence=SourceIndependenceEvidence.from_sources(accepted_repeated),
        )
    else:
        base_reasons = {
            REASON_TITLE_MISMATCH,
            REASON_AUTHOR_MISMATCH,
            REASON_INSUFFICIENT_SUBJECT,
            REASON_SOURCE_DENYLISTED,
        }
        has_judged = bool(individually_checkable)
        reason_codes = base_reasons if has_judged else base_reasons | {REASON_SEARCH_SNIPPET, REASON_INVALID_SOURCE}
        # An individual ISBN conflict (#755) is an identity conflict, not
        # missing corroboration, so it outranks the other per-source reasons.
        reason = next(
            (item.reason_code for item in rejected if item.reason_code == REASON_SOURCE_IDENTITY_DISAGREEMENT),
            "",
        ) or next(
            (item.reason_code for item in rejected if item.reason_code in reason_codes),
            REASON_FEWER_THAN_TWO,
        )
    if reason in {REASON_SOURCE_IDENTITY_DISAGREEMENT, REASON_SUBJECT_DISAGREEMENT}:
        conflict_urls = {url for _source, url in individually_checkable}
        rejected = [item for item in rejected if item.canonical_url not in conflict_urls]
        rejected.extend(RejectedSource(url, reason) for _source, url in individually_checkable)
    return GroundingGateResult(False, reason, rejected_sources=tuple(rejected))


# Compatibility names make the seam discoverable without duplicating logic.
check_grounding_gate = evaluate_grounding_gate
universal_grounding_gate = evaluate_grounding_gate


def validate_prominence_claim(
    target: DurableTarget | Mapping[str, Any], claim: ProminenceClaim | Mapping[str, Any]
) -> ProminenceValidation:
    """Validate a typed prominence claim independently from descriptions.

    The stage checks the span against the cited AcceptedSource because this
    standalone validator does not receive captured page text.
    """
    if isinstance(target, DurableTarget):
        resolved_target = target
    else:
        resolution = resolve_durable_target(target, durable_candidates=[target])
        if not resolution.resolved or resolution.target is None:
            return ProminenceValidation(False, REASON_AMBIGUITY)
        resolved_target = resolution.target
    parsed = claim if isinstance(claim, ProminenceClaim) else ProminenceClaim.from_mapping(claim)
    if parsed.claim_type not in {"bestseller", "debut", "prize", "mentor_or_backing"}:
        return ProminenceValidation(False, REASON_SEARCH_OR_FETCH_FAILURE)
    if not parsed.evidence_urls or any(not canonicalize_url(url) for url in parsed.evidence_urls):
        return ProminenceValidation(False, REASON_SEARCH_OR_FETCH_FAILURE)
    canonical_urls = tuple(canonicalize_url(url) for url in parsed.evidence_urls)
    if len(set(canonical_urls)) != len(canonical_urls) or not parsed.source_kind or not parsed.checked_date:
        return ProminenceValidation(False, REASON_SEARCH_OR_FETCH_FAILURE)
    if not parsed.display_text or len(parsed.display_text) > MAX_DISPLAY_TEXT:
        return ProminenceValidation(False, REASON_SEARCH_OR_FETCH_FAILURE)
    if not parsed.evidence_span or len(parsed.evidence_span) > _SUBJECT_CONTEXT_FIELD_MAX_CHARS:
        return ProminenceValidation(False, REASON_SEARCH_OR_FETCH_FAILURE)
    if not _span_supports_work_claim(parsed.evidence_span, resolved_target.title, resolved_target.primary_authors):
        return ProminenceValidation(False, REASON_SEARCH_OR_FETCH_FAILURE)
    source = FetchedSource(
        url=canonical_urls[0],
        source_kind=parsed.source_kind,
        source_family=parsed.source_kind,
        owner_id=parsed.source_kind,
        source_language="",
        title=parsed.title,
        primary_authors=parsed.primary_authors,
        subject_text="prominence claim",
        title_evidence_route=parsed.title_evidence_route,
        title_evidence=parsed.title_evidence,
    )
    identity_reason = _identity_reason(resolved_target, source)
    if identity_reason:
        return ProminenceValidation(False, identity_reason)
    return ProminenceValidation(
        True,
        "",
        ProminenceClaim(
            claim_type=parsed.claim_type,
            evidence_urls=canonical_urls,
            source_kind=parsed.source_kind,
            checked_date=parsed.checked_date,
            display_text=parsed.display_text[:MAX_DISPLAY_TEXT],
            evidence_span=parsed.evidence_span[:_SUBJECT_CONTEXT_FIELD_MAX_CHARS],
            title=parsed.title[:MAX_EVIDENCE_METADATA_TEXT],
            primary_authors=tuple(author[:200] for author in parsed.primary_authors[:10]),
            title_evidence_route=parsed.title_evidence_route,
            title_evidence=parsed.title_evidence,
            scope=parsed.scope,
        ),
    )


def validate_prominence_claims(
    target: DurableTarget | Mapping[str, Any], claims: Sequence[ProminenceClaim | Mapping[str, Any]]
) -> ProminenceClaimsValidation:
    """Validate typed claims and reject conflicting scope or claim evidence."""
    accepted: list[ProminenceClaim] = []
    for raw_claim in claims:
        validation = validate_prominence_claim(target, raw_claim)
        if not validation.accepted or validation.claim is None:
            return ProminenceClaimsValidation(False, validation.reason_code)
        accepted.append(validation.claim)
    for first, second in itertools.combinations(accepted, 2):
        if first.claim_type != second.claim_type:
            continue
        if (
            first.scope != second.scope
            or first.display_text != second.display_text
            or first.checked_date != second.checked_date
        ):
            return ProminenceClaimsValidation(False, REASON_SOURCE_IDENTITY_DISAGREEMENT)
    return ProminenceClaimsValidation(True, "", tuple(accepted))


@dataclass
class GroundedDescriptionQueue:
    entries: dict[str, QueueEntry] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> GroundedDescriptionQueue:
        value = read_json_or_default(path, {})
        raw_entries = value.get("entries", []) if isinstance(value, Mapping) else []
        entries = {
            entry.candidate_id: entry
            for entry in (QueueEntry.from_mapping(item) for item in raw_entries if isinstance(item, Mapping))
            if entry.candidate_id
        }
        return cls(entries)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(
            path,
            {
                "version": 1,
                "entries": [self.entries[key].to_dict() for key in sorted(self.entries)],
            },
        )

    def worklist(
        self,
        candidates: Sequence[Mapping[str, Any]],
        *,
        operation_id: str,
        operation_date: str,
        existing_candidate_ids: set[str] | None = None,
        eligible_candidate_ids: set[str] | None = None,
        repair_rank: Mapping[str, int] | None = None,
    ) -> list[tuple[Mapping[str, Any], QueueEntry]]:
        existing_candidate_ids = existing_candidate_ids or set()
        eligible_candidate_ids = set(eligible_candidate_ids) if eligible_candidate_ids is not None else None
        by_id: dict[str, Mapping[str, Any]] = {}
        for candidate in candidates:
            candidate_id = _candidate_id(candidate)
            if not candidate_id:
                continue
            by_id[candidate_id] = candidate
        selected: dict[str, QueueEntry] = {}
        for candidate_id, candidate in by_id.items():
            delta = str(candidate.get("update_delta", "")).strip().casefold()
            old = self.entries.get(candidate_id)
            if eligible_candidate_ids is not None:
                if candidate_id not in eligible_candidate_ids:
                    continue
            else:
                if candidate_id in existing_candidate_ids and delta != "new":
                    continue
                is_new = delta == "new"
                is_carryover = old is not None and old.attempt_status not in {
                    OUTCOME_GROUNDED,
                    OUTCOME_SINGLE_SOURCE,
                }
                if not is_new and not is_carryover:
                    continue
            if old is None:
                entry = QueueEntry(
                    candidate_id=candidate_id,
                    queue_entry_operation=operation_id,
                    queue_entry_date=operation_date,
                    first_seen_date=str(candidate.get("first_seen_date", "") or operation_date),
                    priority_label=str(candidate.get("priority_label", "D")).strip().upper() or "D",
                )
            else:
                # Queue age is durable scheduling state. Admission and a
                # budget-not-admitted outcome must not reset it.
                entry = QueueEntry(
                    candidate_id=candidate_id,
                    queue_entry_operation=old.queue_entry_operation,
                    queue_entry_date=old.queue_entry_date,
                    first_seen_date=old.first_seen_date,
                    priority_label=str(candidate.get("priority_label", old.priority_label)).strip().upper() or "D",
                    attempt_status=old.attempt_status,
                    reason_code=old.reason_code,
                )
            selected[candidate_id] = entry
            self.entries[candidate_id] = entry
        # Lifecycle band is the outermost key (issue #699): new candidates
        # are attempted before updated ones, and previously-seen candidates
        # only with budget left over. The stage budget runs out mid-queue,
        # so without this an unchanged June candidate displaces a row the
        # editor will actually read. Nothing is filtered — every selected
        # candidate stays in the worklist, just further down it.
        #
        # Within a band: drain the oldest waiting candidates first —
        # priority label, then oldest first-seen date, then candidate id.
        # The candidate id tie-break keeps the ordering total, so two runs
        # sort the same way and artifacts stay byte-identical.
        #
        # A caller-planned repair order overrides the queue order for the
        # ranked ids only (review finding on #674): without the override,
        # the queue's oldest-first sort silently inverts repair fairness,
        # admitting previously-failed (older) repairs ahead of untouched
        # ones. Fresh work keeps the historical queue order in group zero;
        # ranked repairs follow in group one by their planned rank. A
        # missing rank map behaves exactly as before. Because the planned
        # rank is monotone, restricting it to one band preserves repair
        # fairness inside that band exactly.
        rank = dict(repair_rank) if repair_rank else {}
        return sorted(
            ((by_id[candidate_id], entry) for candidate_id, entry in selected.items()),
            key=lambda item: (
                lifecycle_band_rank(item[0]),
                (1, rank[item[1].candidate_id], item[1].candidate_id)
                if item[1].candidate_id in rank
                else (
                    0,
                    {"A": 0, "B": 1, "C": 2, "D": 3}.get(item[1].priority_label, 9),
                    item[1].first_seen_date,
                    item[1].candidate_id,
                ),
            ),
        )

    def update(self, entry: QueueEntry) -> None:
        self.entries[entry.candidate_id] = entry


def _invoke_provider(method: Callable[..., Any], value: Any, **kwargs: Any) -> Any:
    """Call simple fakes and bounded providers through one stable seam."""
    try:
        return method(value, **kwargs)
    except TypeError as first_error:
        try:
            return method(value)
        except TypeError:
            raise first_error


def _invoke_synthesizer(
    method: Callable[..., Any],
    candidate: Mapping[str, Any],
    sources: Sequence[AcceptedSource],
    remaining_seconds: float,
) -> Any:
    """Pass the live synthesis bound without breaking simple injected fakes."""
    try:
        parameters = inspect.signature(method).parameters.values()
        supports_remaining = any(
            parameter.name == "remaining_seconds" or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
    except (TypeError, ValueError):
        supports_remaining = False
    kwargs: dict[str, Any] = {"sources": sources}
    if supports_remaining:
        kwargs["remaining_seconds"] = remaining_seconds
    return _invoke_provider(method, candidate, **kwargs)


class NoNetworkDiscoveryProvider:
    """Deterministic provider used by the safe default and offline tests."""

    def discover(self, candidate: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
        return []


class NoNetworkHTTPTransport:
    """Refusing HTTP transport used by the offline adjudication stage.

    Every request fails closed with a bounded error, so a fixture run
    cannot reach the network whatever the fixture holds (issue #640).
    """

    def request(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
        max_redirects: int,
        allowed_hosts: set[str] | None = None,
        remaining_seconds: Callable[[], float] | None = None,
    ) -> HTTPResponse:
        raise BoundedHTTPError("network is disabled in the offline fixture run")


class NoOpDescriptionSynthesizer:
    def synthesize(self, candidate: Mapping[str, Any], sources: Sequence[AcceptedSource]) -> SynthesisResult:
        return SynthesisResult("")


GROUNDED_SYNTHESIS_PROMPT = (
    "Read work_items.json. Return only a JSON object keyed by the exact identity_key. "
    "For each item, you may synthesize only from the accepted bounded evidence supplied in "
    "accepted_bounded_evidence. Candidate title and authors identify the item but are not evidence. "
    "You must not search, browse, follow links, use outside knowledge, or invent URLs/evidence. "
    "Use no evidence that is not present in accepted_bounded_evidence. "
    'Return each value as {"content_description": "..."} with original Russian prose of 2-4 sentences '
    "that states the evidence-backed subject of the book and one distinguishing angle or editorial hook "
    "taken from the same evidence. Do not pad with generic filler about the title, and do not invent "
    "specifics the evidence does not state. "
    "When the accepted evidence cannot support two substantive sentences, omit content_description. "
    'Each value with a content_description should also carry "english_description": 2-4 sentences of '
    "original English prose from the same accepted evidence, stating the same subject and angle as the Russian. "
    "Paraphrase; never copy sentences or long phrases from the evidence. It is never shown to the editor. "
    "Omit english_description rather than guess. "
    'Each value may optionally carry "prominence_claims": a JSON array of claim objects drawn only '
    "from the same accepted_bounded_evidence. Every claim object must use exactly these fields: "
    "claim_type (one of bestseller, debut, prize, mentor_or_backing), evidence_urls (only URLs copied "
    "verbatim from accepted_bounded_evidence), source_kind, checked_date, display_text (concise "
    "original Russian prose stating the claim), evidence_span (a non-empty verbatim source span copied "
    "from accepted_bounded_evidence, no more than 500 characters, and containing the normalized work title), "
    "title and primary_authors naming the same work and authors as the item. A phrase naming only an "
    "author does not support a claim about the work. Omit every claim when the accepted evidence is missing, "
    "unclear, or contradictory. Never infer a claim from the title, the authors, metadata, or outside knowledge. "
    "Never infer a supporting span from the title, the authors, metadata, or outside knowledge: a claim without "
    "a supporting URL and verbatim evidence_span in "
    "accepted_bounded_evidence must not be produced."
)


def grounded_synthesis_work_item(candidate: Mapping[str, Any], sources: Sequence[AcceptedSource]) -> dict[str, Any]:
    """Return the one work item grounded synthesis sends to the model.

    Description model qualification (issue #843) rebuilds this from frozen
    evidence, so the replayed item is the one production would send.
    """
    identity_key = _bounded(candidate.get("identity_key", ""), 200)
    evidence = []
    accepted_evidence = []
    for source in sources:
        authors = ", ".join(source.raw_authors[:10])
        subject = _bounded_subject(source.display_text or source.subject_key)
        metadata = _bounded(
            "; ".join(
                (
                    f"url={_bounded(source.canonical_url, MAX_URL_LENGTH)}",
                    f"source_kind={_bounded(source.source_kind, 100)}",
                    f"source_family={_bounded(source.source_family, 100)}",
                    f"owner={_bounded(source.owner_id, 100)}",
                    f"title={_bounded(source.raw_title, MAX_EVIDENCE_METADATA_TEXT)}",
                    f"authors={_bounded(authors, 400)}",
                )
            ),
            MAX_EVIDENCE_METADATA_TEXT,
        )
        evidence.append(f"{metadata}{SOURCE_SUBJECT_SEPARATOR}{subject}")
        accepted_evidence.append(source.to_dict())
    candidate_authors = candidate.get("primary_authors", [])
    if isinstance(candidate_authors, str):
        candidate_authors = [candidate_authors]
    if not isinstance(candidate_authors, Sequence) or isinstance(candidate_authors, bytes | bytearray):
        candidate_authors = []
    return {
        "identity_key": identity_key,
        "title": _bounded(candidate.get("title", ""), 300),
        "metadata": {
            "title": _bounded(candidate.get("title", ""), 300),
            "primary_authors": [_bounded(author, 200) for author in candidate_authors if str(author).strip()][:10],
            "signal_summary": evidence,
        },
        "accepted_bounded_evidence": accepted_evidence,
        "description_requirements": [],
        "replaces_existing_description": False,
    }


def synthesis_result_from_response(generated: Any, identity_key: str) -> SynthesisResult:
    """Read one item's untrusted synthesis answer into a ``SynthesisResult``."""
    if not isinstance(generated, Mapping):
        return SynthesisResult("")
    value = generated.get(identity_key)
    if isinstance(value, str):
        return SynthesisResult(value)
    if isinstance(value, Mapping):
        raw_claims = value.get("prominence_claims")
        claims: list[Mapping[str, Any]] = []
        if isinstance(raw_claims, Sequence) and not isinstance(raw_claims, str | bytes | bytearray):
            claims = [item for item in raw_claims if isinstance(item, Mapping)]
        return SynthesisResult(
            str(value.get("content_description", "")),
            tuple(claims),
            english_description=str(value.get(ENGLISH_DESCRIPTION_FIELD, "") or ""),
        )
    return SynthesisResult("")


class PwTextNotPermitted(Exception):
    """The description backend may not receive this candidate's Publishers Weekly evidence (Issue #1149).

    Raised out of the synthesizer, so the stage records the candidate as a
    synthesis failure under this name instead of an empty description.
    """


class CodexGroundedDescriptionSynthesizer:
    """Synthesize only from the stage's accepted, bounded source evidence."""

    def __init__(self, agent: Any | None = None) -> None:
        if agent is None:
            from autoscout.description_enrichment import CodexDescriptionEnrichmentAgent

            agent = CodexDescriptionEnrichmentAgent(purpose="grounded_description")
        self.agent = agent

    def synthesize(
        self,
        candidate: Mapping[str, Any],
        sources: Sequence[AcceptedSource],
        *,
        remaining_seconds: float | None = None,
    ) -> SynthesisResult:
        work_item = grounded_synthesis_work_item(candidate, sources)
        identity_key = work_item["identity_key"]
        try:
            parameters = inspect.signature(self.agent.generate).parameters.values()
            supports_prompt_override = any(
                parameter.name == "_prompt_override" or parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
            supports_timeout_override = any(
                parameter.name == "_timeout_seconds" or parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
            supports_failure_callback = any(parameter.name == "_on_failure" for parameter in parameters)
        except (TypeError, ValueError):
            supports_prompt_override = False
            supports_timeout_override = False
            supports_failure_callback = False
        agent_kwargs: dict[str, Any] = {}
        failures: list[str] = []
        if supports_prompt_override:
            agent_kwargs["_prompt_override"] = GROUNDED_SYNTHESIS_PROMPT
        if supports_timeout_override and remaining_seconds is not None:
            agent_kwargs["_timeout_seconds"] = remaining_seconds
        if supports_failure_callback:
            agent_kwargs["_on_failure"] = failures.append
        if agent_kwargs:
            generated = self.agent.generate([work_item], **agent_kwargs)
        else:
            # Small injected fakes from existing offline fixtures only accept
            # the historical one-argument seam. Production Codex receives the
            # dedicated prompt and timeout through the internal overrides above.
            generated = self.agent.generate([work_item])
        from autoscout.description_enrichment import CALL_FAILURE_PW_TEXT_NOT_PERMITTED

        if CALL_FAILURE_PW_TEXT_NOT_PERMITTED in failures:
            raise PwTextNotPermitted(CALL_FAILURE_PW_TEXT_NOT_PERMITTED)
        return synthesis_result_from_response(generated, identity_key)


def _row_evidence_entries(candidate: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Use the primary signal only when the evidence collection is absent."""
    if "source_evidence" in candidate:
        entries = candidate["source_evidence"]
        return [entry for entry in entries if isinstance(entry, Mapping)] if isinstance(entries, list) else []
    primary = candidate.get("primary_signal")
    return [primary] if isinstance(primary, Mapping) else []


class GroundedDescriptionStage:
    """Run bounded discovery and post-discovery candidate work.

    Admission reserves the measured per-candidate cost against the
    worker-seconds the stage can still spend, without mutating Candidate
    History. An optional shared vendor limiter runs before candidate timing,
    so admitted queue waits consume overall stage time but not candidate
    timing.
    """

    def __init__(
        self,
        discovery: DiscoveryProvider | None = None,
        fetch: FetchProvider | None = None,
        synthesizer: DescriptionSynthesizer | None = None,
        *,
        classifier: SourceClassifier | None = None,
        rate_limiter: VendorRateLimiter | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        stage_budget_seconds: float = STAGE_BUDGET_SECONDS,
        scale_weekly_budget: bool = False,
        candidate_envelope_seconds: float = CANDIDATE_ENVELOPE_SECONDS,
        discovery_budget_seconds: float = DISCOVERY_BUDGET_SECONDS,
        request_inactivity_timeout_seconds: float = REQUEST_INACTIVITY_TIMEOUT_SECONDS,
        max_workers: int = MAX_WORKERS,
        max_fetches: int = MAX_FETCHES_PER_CANDIDATE,
    ) -> None:
        self.discovery = discovery or NoNetworkDiscoveryProvider()
        self.classifier = classifier if classifier is not None else getattr(self.discovery, "classifier", None)
        self._shared_evidence_urls: set[str] = set()
        self.fetch = fetch
        self.synthesizer = synthesizer or NoOpDescriptionSynthesizer()
        self.rate_limiter = rate_limiter
        self.monotonic = monotonic
        self.stage_budget_seconds = stage_budget_seconds
        self.scale_weekly_budget = scale_weekly_budget
        self.candidate_envelope_seconds = candidate_envelope_seconds
        self.discovery_budget_seconds = discovery_budget_seconds
        self.request_inactivity_timeout_seconds = request_inactivity_timeout_seconds
        self.max_workers = min(max_workers, MAX_WORKERS)
        self.max_fetches = min(max_fetches, MAX_FETCHES_PER_CANDIDATE)

    def _evidence_seeds(self, candidate: Mapping[str, Any]) -> list[DiscoveryResult]:
        if self.classifier is None:
            return []
        seeds: list[DiscoveryResult] = []
        evidence_by_url: dict[str, list[Mapping[str, Any]]] = {}
        for evidence in _row_evidence_entries(candidate):
            url = canonicalize_url(str(evidence.get("evidence_url", "")))
            evidence_by_url.setdefault(url, []).append(evidence)
        for url, entries in evidence_by_url.items():
            path = urlsplit(url).path.casefold()
            if (
                not url
                or url in self._shared_evidence_urls
                or urlsplit(url).scheme not in {"http", "https"}
                or not urlsplit(url).hostname
                or path in {"", "/"}
                or path.endswith((".csv", ".json", ".xml", ".rss", ".txt", ".pdf", ".xls", ".xlsx"))
                or any(
                    segment in {"feed", "rss", "api", "wp-json", "admin-ajax.php", "admin-ajax"}
                    for segment in path.split("/")
                )
            ):
                continue
            classification = self.classifier.classify(url)
            if classification.get("classification") != "classified":
                continue
            signal_own = False
            for evidence in entries:
                source_url = canonicalize_url(str(evidence.get("source_url", "")))
                signal = self.classifier.classify(source_url) if source_url else {}
                if (
                    signal.get("classification") != "classified"
                    or not signal.get("owner_id")
                    or signal.get("owner_id") == classification.get("owner_id")
                ):
                    signal_own = True
                    break
            seeds.append(
                DiscoveryResult.from_mapping(
                    {
                        **classification,
                        "url": url,
                        "origin": "row_evidence",
                        "source_language": classification.get("source_language", "")
                        or candidate_source_language(candidate),
                        "signal_own": signal_own,
                    }
                )
            )
            if len(seeds) == 2:
                break
        return seeds

    def _wait_for_discovery_capacity(self, stage_deadline: float) -> bool:
        """Wait at the stage boundary, outside candidate timing."""
        if self.rate_limiter is None:
            return True
        if self.monotonic() >= stage_deadline:
            return False
        return self.rate_limiter.wait(deadline=stage_deadline) is not None

    def run(
        self,
        candidates: Sequence[Mapping[str, Any]],
        *,
        operation_id: str,
        operation_date: str,
        queue: GroundedDescriptionQueue | None = None,
        existing_candidate_ids: set[str] | None = None,
        history: Sequence[Mapping[str, Any]] = (),
        aliases: Mapping[str, Any] | None = None,
        force_candidates: bool = False,
        eligible_candidate_ids: set[str] | None = None,
        repair_rank: Mapping[str, int] | None = None,
        snapshot_tracker: CandidateProgressSnapshot | None = None,
    ) -> StageArtifact:
        evidence_owners: dict[str, set[str]] = {}
        # Weekly eligibility and standalone per-book calls narrow candidates;
        # history retains the other rows that identify shared list pages.
        for candidate in (*candidates, *history):
            owner = _candidate_id(candidate) or str(candidate.get("identity_key", "")).strip()
            if not owner:
                continue
            for evidence in _row_evidence_entries(candidate):
                url = canonicalize_url(str(evidence.get("evidence_url", "")))
                if url:
                    evidence_owners.setdefault(url, set()).add(owner)
        self._shared_evidence_urls = {url for url, owners in evidence_owners.items() if len(owners) > 1}
        queue = queue or GroundedDescriptionQueue()
        started = self.monotonic()
        if force_candidates:
            # The standalone generator works from durable Candidate History,
            # not from the weekly "new candidate" queue. Keep the same
            # attempt seam and queue entry shape, but deliberately bypass the
            # weekly queue-selection rule for this explicit operation. The
            # stage budget gate below still applies.
            worklist = []
            for candidate in candidates:
                candidate_id = _candidate_id(candidate)
                if not candidate_id:
                    continue
                old = queue.entries.get(candidate_id)
                if old is None:
                    entry = QueueEntry(
                        candidate_id=candidate_id,
                        queue_entry_operation=operation_id,
                        queue_entry_date=operation_date,
                        first_seen_date=str(candidate.get("first_seen_date", "") or operation_date),
                        priority_label=str(candidate.get("priority_label", "D")).strip().upper() or "D",
                    )
                else:
                    entry = old
                worklist.append((candidate, entry))
        else:
            worklist = queue.worklist(
                candidates,
                operation_id=operation_id,
                operation_date=operation_date,
                existing_candidate_ids=existing_candidate_ids,
                eligible_candidate_ids=eligible_candidate_ids,
                repair_rank=repair_rank,
            )
        budget = self.stage_budget_seconds
        if self.scale_weekly_budget and not force_candidates:
            count = sum(lifecycle_band(candidate) in {BAND_NEW, BAND_UPDATED} for candidate, _ in worklist)
            budget = description_stage_limits(count).grounded_stage_budget_seconds
        deadline = started + budget
        # Admission is continuous, not one up-front batch. A worker decides
        # at the moment it picks a candidate up, so the decision uses the
        # real clock rather than a prediction made before any work ran.
        # Forced candidates bypass queue selection but not this budget gate.
        if snapshot_tracker is not None:
            snapshot_tracker.set_stage_budget_seconds(budget)
            snapshot_tracker.initial(
                [(_candidate_id(candidate), str(candidate.get("title", "") or "")) for candidate, _ in worklist]
            )
        try:
            outcomes = self._attempt_worklist(
                worklist,
                deadline,
                operation_date,
                history=history,
                aliases=aliases,
                durable_candidates=candidates,
                snapshot_tracker=snapshot_tracker,
            )
        except Exception:
            if snapshot_tracker is not None:
                snapshot_tracker.final(status="failed")
            raise
        if snapshot_tracker is not None:
            snapshot_tracker.final()
        for outcome in outcomes:
            if outcome.scheduling is not None:
                queue.update(outcome.scheduling)
        # Concurrency must not change output bytes: collect every outcome
        # first, then sort by candidate id before writing any artifact.
        outcomes.sort(key=lambda outcome: outcome.candidate_id)
        return StageArtifact(
            operation_id=operation_id,
            operation_date=operation_date,
            outcomes=tuple(outcomes),
            queue=tuple(queue.entries[key] for key in sorted(queue.entries)),
            elapsed_seconds=max(0.0, self.monotonic() - started),
            stage_budget_seconds=budget,
            report={
                "row_evidence_fetched": sum(item.row_evidence_fetched for item in outcomes),
                "row_evidence_accepted": sum(
                    source.origin == "row_evidence" for item in outcomes for source in item.research_sources
                ),
            },
        )

    @staticmethod
    def _not_attempted(entry: QueueEntry) -> StageOutcome:
        """Build the outcome for a candidate the stage budget never reached.

        A not-attempted candidate keeps its queue age and makes no claim
        about its sources. It must never be reported as an attempt that
        failed, because the editor reads the two differently and the stage
        report counts a failure as a problem that needs attention.
        """
        waiting = QueueEntry(
            **{**entry.to_dict(), "attempt_status": "waiting", "reason_code": REASON_NOT_ATTEMPTED_STAGE_BUDGET}
        )
        return StageOutcome(
            candidate_id=entry.candidate_id,
            outcome=OUTCOME_NOT_ATTEMPTED,
            reason_code=REASON_NOT_ATTEMPTED_STAGE_BUDGET,
            scheduling=waiting,
        )

    def _attempt_worklist(
        self,
        worklist: Sequence[tuple[Mapping[str, Any], QueueEntry]],
        stage_deadline: float,
        operation_date: str,
        *,
        history: Sequence[Mapping[str, Any]],
        aliases: Mapping[str, Any] | None,
        durable_candidates: Sequence[Mapping[str, Any]],
        snapshot_tracker: CandidateProgressSnapshot | None = None,
    ) -> list[StageOutcome]:
        """Run the worklist through the bounded daemon worker pool.

        This is the same pool ``autoscout.extraction_round`` drives: daemon
        threads at the stage's own worker count, so a late bounded attempt
        cannot block process exit.

        Every candidate is submitted, and admission happens inside the
        worker. A worker checks the remaining stage time when it picks a
        candidate up, and starts the candidate only when that time can still
        hold one. So the stage cannot admit more work than the clock serves,
        and a candidate it never starts reports ``not_attempted_stage_budget``
        instead of claiming its sources were checked.

        The reserve grows with the run. It is the seed constant until the run
        has ``ADMISSION_SAMPLE_FLOOR`` finished candidates, and after that the
        larger of the seed and the observed p95. A run whose candidates cost
        more than the seed therefore stops admitting sooner, with no constant
        to re-tune.

        The samples are candidate totals, so they exclude the shared vendor
        queue wait, which is bounded by one minimum vendor interval per
        candidate. Admission reads the clock exactly once per candidate, so
        an injected clock sees one added call and no more.
        """
        if not worklist:
            return []
        # Matching is read-only. Share one isolated copy across attempts,
        # retaining full history for the known-ID check, including unkeyed rows.
        try:
            keyed_history = KeyedHistory.of([dict(item) for item in history])
        except (TypeError, ValueError):
            # Preserve the per-candidate ambiguity handling for invalid history.
            keyed_history = None
        durations: list[float] = []
        lock = threading.Lock()

        def reserve_seconds() -> float:
            with lock:
                samples = sorted(durations)
            if len(samples) < ADMISSION_SAMPLE_FLOOR:
                return MEASURED_CANDIDATE_COST_SECONDS
            index = min(len(samples) - 1, int(0.95 * len(samples)))
            return max(MEASURED_CANDIDATE_COST_SECONDS, samples[index])

        def attempt(candidate: Mapping[str, Any], entry: QueueEntry) -> StageOutcome:
            if self.monotonic() + reserve_seconds() > stage_deadline:
                return self._not_attempted(entry)
            # The heartbeat key uses the same derivation as the tracker's
            # initial worklist, so begin/finish can never miss their row.
            tracker_key = _candidate_id(candidate)
            if snapshot_tracker is not None:
                snapshot_tracker.begin(tracker_key)
            try:
                outcome = self._attempt_candidate(
                    candidate,
                    entry,
                    stage_deadline,
                    operation_date,
                    history=history,
                    keyed_history=keyed_history,
                    aliases=aliases,
                    durable_candidates=durable_candidates,
                )
            finally:
                if snapshot_tracker is not None:
                    snapshot_tracker.finish(tracker_key)
            total_seconds = outcome.candidate_timing.total_seconds
            if total_seconds is not None:
                with lock:
                    durations.append(max(0.0, float(total_seconds)))
            return outcome

        pool = DaemonThreadPool(max(1, self.max_workers), thread_name_prefix="autoscout-grounded-description-")
        try:
            futures = [pool.submit(attempt, candidate, entry) for candidate, entry in worklist]
            if snapshot_tracker is not None:
                return snapshot_tracker.collect(futures)
            return [future.result() for future in futures]
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _attempt_candidate(
        self,
        candidate: Mapping[str, Any],
        entry: QueueEntry,
        stage_deadline: float,
        operation_date: str,
        *,
        history: Sequence[Mapping[str, Any]],
        keyed_history: KeyedHistory | None = None,
        aliases: Mapping[str, Any] | None,
        durable_candidates: Sequence[Mapping[str, Any]],
    ) -> StageOutcome:
        # Queueing for the shared vendor quota consumes the overall stage clock,
        # but is not candidate work. Sample the candidate start only after it.
        candidate_started: float | None = None
        discovery_seconds: float | None = None
        fetch_seconds: float | None = None
        synthesis_seconds: float | None = None
        likely_author_typos: tuple[AuthorTypoEvidence, ...] = ()
        fetched: list[FetchedSource] = []

        def measure_start() -> float | None:
            try:
                return float(self.monotonic())
            except Exception:
                return None

        def measure_end(start: float | None) -> float | None:
            if start is None:
                return None
            try:
                elapsed = float(self.monotonic()) - start
            except Exception:
                return None
            return max(0.0, elapsed)

        def finish(outcome: StageOutcome) -> StageOutcome:
            timing = CandidateTiming(
                discovery_seconds=discovery_seconds,
                fetch_seconds=fetch_seconds,
                synthesis_seconds=synthesis_seconds,
                total_seconds=measure_end(candidate_started),
            )
            reason_detail = outcome.reason_detail
            if outcome.reason_code == REASON_CANDIDATE_BUDGET_EXHAUSTED:
                reason_detail = (
                    f"{reason_detail}; timing: {timing.describe()}" if reason_detail else f"timing: {timing.describe()}"
                )
            return replace(
                outcome,
                reason_detail=_bounded(reason_detail, MAX_METADATA_VALUE_LENGTH),
                candidate_timing=timing,
                likely_author_typos=likely_author_typos,
                row_evidence_fetched=sum(source.origin == "row_evidence" for source in fetched),
            )

        if not self._wait_for_discovery_capacity(stage_deadline):
            return finish(
                self._refusal(
                    candidate,
                    entry,
                    REASON_CANDIDATE_BUDGET_EXHAUSTED,
                    reason_detail="vendor queue could not fit before the stage deadline; candidate work did not start",
                )
            )
        candidate_started = self.monotonic()
        discovery_deadline = min(stage_deadline, candidate_started + self.discovery_budget_seconds)
        if self.monotonic() >= stage_deadline:
            return finish(self._refusal(candidate, entry, REASON_CANDIDATE_BUDGET_EXHAUSTED))
        seeds = self._evidence_seeds(candidate)
        signal_owners: set[str] = set()
        if self.classifier is not None:
            for evidence in _row_evidence_entries(candidate):
                source_url = canonicalize_url(str(evidence.get("source_url", "")))
                signal = self.classifier.classify(source_url) if source_url else {}
                if signal.get("classification") == "classified" and signal.get("owner_id"):
                    signal_owners.add(signal["owner_id"])
        seeds = [replace(seed, signal_own=seed.signal_own or seed.owner_id in signal_owners) for seed in seeds]
        discovery_failed = False
        discovery_started = measure_start()
        discovery_request_budget = CandidateRequestBudget(
            deadline=discovery_deadline,
            max_fetches=0,
            monotonic=self.monotonic,
            max_discovery_requests=MAX_DISCOVERY_REQUESTS_PER_CANDIDATE,
        )
        try:
            discovered_raw = _invoke_provider(
                self.discovery.discover,
                candidate,
                timeout_seconds=self.request_inactivity_timeout_seconds,
                request_budget=discovery_request_budget,
            )
        except Exception as exc:
            discovery_seconds = measure_end(discovery_started)
            now = self.monotonic()
            if (
                now >= discovery_deadline
                or now >= stage_deadline
                or (isinstance(exc, BoundedHTTPError) and exc.reason_code == REASON_CANDIDATE_BUDGET_EXHAUSTED)
            ):
                return finish(self._refusal(candidate, entry, REASON_CANDIDATE_BUDGET_EXHAUSTED))
            if not seeds:
                return finish(self._refusal(candidate, entry, REASON_SEARCH_OR_FETCH_FAILURE))
            discovered_raw = []
            discovery_failed = True
        discovery_seconds = measure_end(discovery_started)
        discovery_completed = self.monotonic()
        if discovery_completed >= discovery_deadline or discovery_completed >= stage_deadline:
            return finish(self._refusal(candidate, entry, REASON_CANDIDATE_BUDGET_EXHAUSTED))
        candidate_deadline = min(stage_deadline, discovery_completed + self.candidate_envelope_seconds)
        post_discovery_request_budget = CandidateRequestBudget(
            deadline=candidate_deadline,
            max_fetches=self.max_fetches,
            monotonic=self.monotonic,
        )
        discovered: list[DiscoveryResult] = []
        denylisted_sources: list[RejectedSource] = []
        # Discovery keeps its existing clock window. Seeds lead the shared
        # post-discovery fetch loop and consume the same request/host budgets.
        seed_indices = {result.url: index for index, result in enumerate(seeds)}
        discovered_urls: set[str] = set(seed_indices)
        ordered: list[tuple[int, int, DiscoveryResult]] = []
        for provider_ordinal, item in enumerate(discovered_raw or []):
            result = DiscoveryResult.from_mapping(item)
            canonical = canonicalize_url(result.url)
            if result.is_search_snippet or not canonical:
                continue
            if canonical in seed_indices:
                index = seed_indices[canonical]
                seed = seeds[index]
                seeds[index] = replace(
                    seed,
                    title_evidence_route=result.title_evidence_route,
                    title_evidence=result.title_evidence,
                    title=result.title,
                    route_kind=result.route_kind,
                    source_language=seed.source_language or result.source_language,
                )
                continue
            if canonical in discovered_urls:
                continue
            discovered_urls.add(canonical)
            if result.owner_id in signal_owners:
                result = replace(result, signal_own=True)
            if result.classification.casefold() != "classified":
                denylisted_sources.append(
                    RejectedSource(
                        canonical,
                        REASON_SOURCE_DENYLISTED,
                        tier=str(_classification_tier(result)),
                    )
                )
                continue
            discovered.append(result)
            ordered.append((_discovery_relevance_tier(candidate, result), provider_ordinal, result))
        ordered.sort(key=lambda item: _discovery_ordering_key(item[0], item[2].source_kind, item[1], item[2].url))
        ordered = [(0, ordinal, result) for ordinal, result in enumerate(seeds)] + ordered
        if not discovered and not seeds:
            return finish(
                self._refusal(
                    candidate,
                    entry,
                    REASON_SOURCE_DENYLISTED if denylisted_sources else REASON_NO_RESULTS,
                    rejected=denylisted_sources,
                )
            )
        if self.fetch is None:
            return finish(
                self._refusal(
                    candidate,
                    entry,
                    REASON_SEARCH_OR_FETCH_FAILURE,
                    rejected=denylisted_sources,
                )
            )

        fetch_failure_reason = ""
        fetch_started = measure_start()
        for fetch_ordinal, (tier, ordinal, result) in enumerate(ordered):
            if fetch_ordinal >= self.max_fetches:
                break
            if self.monotonic() >= candidate_deadline or self.monotonic() >= stage_deadline:
                fetch_seconds = measure_end(fetch_started)
                return finish(
                    self._refusal(
                        candidate,
                        entry,
                        REASON_CANDIDATE_BUDGET_EXHAUSTED,
                        rejected=denylisted_sources,
                    )
                )
            try:
                fetched_raw = _invoke_provider(
                    self.fetch.fetch,
                    result,
                    timeout_seconds=self.request_inactivity_timeout_seconds,
                    max_bytes=MAX_RESPONSE_BYTES,
                    checked_date=operation_date,
                    request_budget=post_discovery_request_budget,
                )
                fetched.append(
                    replace(
                        FetchedSource.from_mapping(fetched_raw),
                        origin_recorded=True,
                        origin=result.origin,
                        signal_own=result.signal_own,
                        relevance_tier=tier,
                        discovery_ordinal=ordinal,
                        discovery_title=_bounded(result.title, MAX_DISCOVERY_TITLE_TEXT),
                    )
                )
            except Exception as exc:
                reason = str(exc.reason_code).strip() if isinstance(exc, BoundedHTTPError) else ""
                if reason == REASON_CANDIDATE_BUDGET_EXHAUSTED:
                    fetch_seconds = measure_end(fetch_started)
                    return finish(
                        self._refusal(
                            candidate,
                            entry,
                            REASON_CANDIDATE_BUDGET_EXHAUSTED,
                            rejected=denylisted_sources,
                        )
                    )
                if reason in REASON_CODES:
                    fetch_failure_reason = reason
        fetch_seconds = measure_end(fetch_started)
        if self.monotonic() >= candidate_deadline:
            return finish(
                self._refusal(
                    candidate,
                    entry,
                    REASON_CANDIDATE_BUDGET_EXHAUSTED,
                    rejected=denylisted_sources,
                )
            )
        if not fetched:
            return finish(
                self._refusal(
                    candidate,
                    entry,
                    (REASON_SEARCH_OR_FETCH_FAILURE if discovery_failed else fetch_failure_reason)
                    or REASON_SEARCH_OR_FETCH_FAILURE,
                    rejected=denylisted_sources,
                )
            )

        target_resolution = resolve_durable_target(
            candidate,
            history=history,
            keyed_history=keyed_history,
            aliases=aliases,
            durable_candidates=durable_candidates,
        )
        if not target_resolution.resolved:
            return finish(self._refusal(candidate, entry, REASON_AMBIGUITY, rejected=denylisted_sources))
        target = target_resolution.target
        assert target is not None
        likely_author_typos = _likely_author_typos(target, fetched)
        gate = evaluate_grounding_gate(target, fetched)
        if not gate.accepted:
            return finish(
                self._refusal(
                    candidate,
                    entry,
                    gate.reason_code,
                    rejected=_merge_rejected_sources(denylisted_sources, gate.rejected_sources),
                )
            )
        if self.monotonic() >= candidate_deadline or self.monotonic() >= stage_deadline:
            return finish(
                self._refusal(
                    candidate,
                    entry,
                    REASON_CANDIDATE_BUDGET_EXHAUSTED,
                    rejected=_merge_rejected_sources(denylisted_sources, gate.rejected_sources),
                    sources=gate.accepted_sources,
                )
            )
        # Evidence-unchanged gate: skip synthesis when the accepted evidence
        # matches the stored evidence_basis and the gate outcome is unchanged.
        # This is the synthesis site: discovery and fetching have run, but
        # Codex is not called. The comparison is strict list equality, order
        # significant, exactly as validate_stage_artifact compares
        # provenance.get("evidence_basis") != raw_urls at :3931. Any
        # difference in URL set, order, or grounded outcome forces
        # re-synthesis. A missing or non-list evidence_basis is not a match.
        ordered_for_comparison = tuple(
            sorted(
                gate.accepted_sources,
                key=lambda source: _discovery_ordering_key(
                    source.relevance_tier,
                    source.source_kind,
                    source.discovery_ordinal,
                    source.canonical_url,
                    source.origin,
                ),
            )
        )
        new_urls = [source.canonical_url for source in ordered_for_comparison]
        new_outcome = OUTCOME_SINGLE_SOURCE if len(ordered_for_comparison) == 1 else OUTCOME_GROUNDED
        provenance = candidate.get("description_provenance")
        if isinstance(provenance, Mapping) and provenance.get("provenance") == "grounded":
            stored_basis = provenance.get("evidence_basis")
            stored_outcome = str(candidate.get("grounded_outcome", "")).strip()
            # Strict: stored_basis must be a list and byte-equal to new_urls.
            if isinstance(stored_basis, list) and stored_basis == new_urls and stored_outcome == new_outcome:
                # Explicit bypass: when the candidate was explicitly forced
                # via --only, the stored flag "_force_synthesis" is set by
                # generate_descriptions to allow the editor to force a
                # re-synthesis even on unchanged evidence.
                if not candidate.get("_force_synthesis"):
                    accepted_entry = QueueEntry(
                        **{
                            **entry.to_dict(),
                            "attempt_status": OUTCOME_EVIDENCE_UNCHANGED,
                            "reason_code": REASON_EVIDENCE_UNCHANGED,
                        }
                    )
                    return finish(
                        StageOutcome(
                            candidate_id=entry.candidate_id,
                            outcome=OUTCOME_EVIDENCE_UNCHANGED,
                            reason_code=REASON_EVIDENCE_UNCHANGED,
                            identity_key=str(candidate.get("identity_key", "")),
                            scheduling=accepted_entry,
                            rejected_source_reasons=_merge_rejected_sources(denylisted_sources, gate.rejected_sources),
                        )
                    )
        synthesis_detail = ""
        synthesis: SynthesisResult = SynthesisResult("")
        synthesis_bound = min(candidate_deadline, stage_deadline) - self.monotonic()
        if synthesis_bound <= 0:
            return finish(
                self._refusal(
                    candidate,
                    entry,
                    REASON_CANDIDATE_BUDGET_EXHAUSTED,
                    rejected=_merge_rejected_sources(denylisted_sources, gate.rejected_sources),
                    sources=gate.accepted_sources,
                )
            )
        synthesis_started = measure_start()
        try:
            synthesis = _invoke_synthesizer(
                self.synthesizer.synthesize,
                candidate,
                gate.accepted_sources,
                synthesis_bound,
            )
            if not isinstance(synthesis, SynthesisResult):
                raise TypeError("synthesizer returned a bare value instead of SynthesisResult")
            description = synthesis.content_description
        except Exception as exc:
            description = ""
            synthesis_detail = f"synthesizer raised {type(exc).__name__}"
        synthesis_seconds = measure_end(synthesis_started)
        if self.monotonic() >= candidate_deadline or self.monotonic() >= stage_deadline:
            return finish(
                self._refusal(
                    candidate,
                    entry,
                    REASON_CANDIDATE_BUDGET_EXHAUSTED,
                    reason_detail=synthesis_detail,
                    rejected=_merge_rejected_sources(denylisted_sources, gate.rejected_sources),
                    sources=gate.accepted_sources,
                )
            )
        if synthesis_detail:
            return finish(
                self._refusal(
                    candidate,
                    entry,
                    REASON_SYNTHESIS_FAILURE,
                    reason_detail=synthesis_detail,
                    rejected=_merge_rejected_sources(denylisted_sources, gate.rejected_sources),
                    sources=gate.accepted_sources,
                )
            )
        validated, refusal_reason, refusal_detail = screen_synthesized_description(description, gate.accepted_sources)
        if refusal_reason:
            return finish(
                self._refusal(
                    candidate,
                    entry,
                    refusal_reason,
                    reason_detail=refusal_detail,
                    rejected=_merge_rejected_sources(denylisted_sources, gate.rejected_sources),
                    sources=gate.accepted_sources,
                )
            )
        outcome_class = OUTCOME_SINGLE_SOURCE if len(gate.accepted_sources) == 1 else OUTCOME_GROUNDED
        # Present the accepted evidence in the same shared ordering rule that
        # validated artifacts require; the gate's own group selection is
        # untouched.
        ordered_sources = tuple(
            sorted(
                gate.accepted_sources,
                key=lambda source: _discovery_ordering_key(
                    source.relevance_tier,
                    source.source_kind,
                    source.discovery_ordinal,
                    source.canonical_url,
                    source.origin,
                ),
            )
        )
        prominence_claims, claim_refusal_detail = self._validated_prominence_claims(synthesis, target, ordered_sources)
        english_description = _validated_english(synthesis.english_description, ordered_sources)
        accepted_entry = QueueEntry(**{**entry.to_dict(), "attempt_status": outcome_class, "reason_code": ""})
        return finish(
            StageOutcome(
                candidate_id=entry.candidate_id,
                outcome=outcome_class,
                identity_key=str(candidate.get("identity_key", "")),
                content_description=validated,
                research_sources=ordered_sources,
                applied_date=operation_date,
                prominence_claims=prominence_claims,
                scheduling=accepted_entry,
                rejected_source_reasons=_merge_rejected_sources(denylisted_sources, gate.rejected_sources),
                source_independence_evidence=SourceIndependenceEvidence.from_sources(ordered_sources),
                reason_detail=claim_refusal_detail,
                english_description=english_description,
            )
        )

    @staticmethod
    def _validated_prominence_claims(
        synthesis: SynthesisResult,
        target: DurableTarget,
        ordered_sources: Sequence[AcceptedSource],
    ) -> tuple[tuple[ProminenceClaim, ...], str]:
        """Validate returned claims; never let a bad claim reject the description.

        Every rule here refuses a claim, none produces one: an evidence URL
        outside the accepted set, a missing or overlong supporting span, a span
        naming only an author, a span absent from its cited accepted source, an
        unknown claim type, a different work or author, and conflicting same-type
        claims all end with no claim on the outcome.
        Refusals are recorded in the bounded reason detail so a silent no-claims
        outcome cannot hide a refused batch.
        """
        if not synthesis.prominence_claims:
            return (), ""
        by_url = {source.canonical_url: source for source in ordered_sources}
        kept: list[ProminenceClaim] = []
        refusals: list[str] = []
        for raw_claim in synthesis.prominence_claims:
            claim = ProminenceClaim.from_mapping(raw_claim)
            urls = tuple(canonicalize_url(url) for url in claim.evidence_urls)
            if not claim.evidence_urls or any(not url or url not in by_url for url in urls):
                refusals.append(f"{claim.claim_type or 'unknown'}:evidence_url_not_accepted")
                continue
            # source_kind and checked_date are facts the stage already holds for
            # this URL, so they are stamped from the accepted source rather than
            # taken from the model. The validator only checks them for
            # non-emptiness, and it reuses source_kind as source_family and
            # owner_id, so a model-supplied string would become an owner
            # identity. Nothing about a claim may be invented.
            evidence_source = by_url[urls[0]]
            stamped_claim = replace(
                claim,
                evidence_urls=urls,
                source_kind=evidence_source.source_kind,
                checked_date=evidence_source.checked_date,
            )
            if not claim.evidence_span:
                refusals.append(f"{claim.claim_type or 'unknown'}:evidence_span_missing")
                continue
            if len(claim.evidence_span) > _SUBJECT_CONTEXT_FIELD_MAX_CHARS:
                refusals.append(f"{claim.claim_type or 'unknown'}:evidence_span_too_long")
                continue
            if not _span_supports_work_claim(claim.evidence_span, target.title, target.primary_authors):
                refusals.append(f"{claim.claim_type or 'unknown'}:evidence_span_not_work_level")
                continue
            # display_text is bounded prose captured from the accepted page.
            # subject_key is only an identity-gate signature, so it is not a
            # fallback: it is not real page evidence and cannot ground a span.
            if not _span_present_in_evidence(claim.evidence_span, evidence_source.display_text):
                refusals.append(f"{claim.claim_type or 'unknown'}:evidence_span_not_in_source")
                continue
            kept.append(stamped_claim)
        validation = validate_prominence_claims(target, kept)
        accepted_claims: tuple[ProminenceClaim, ...] = ()
        if validation.accepted:
            accepted_claims = validation.claims
        else:
            # The batch is all-or-nothing once per-claim URL filtering has run,
            # because a model that returned one malformed claim has cast doubt
            # on the rest. Name the claim types so a reader can tell which
            # claim spoiled the batch: "batch:<reason>" alone is not legible,
            # and the validator reports an unknown claim type as
            # search_or_fetch_failure, which does not describe what happened.
            spoiled = ",".join(sorted({claim.claim_type or "unknown" for claim in kept}))
            refusals.append(f"batch[{spoiled}]:{validation.reason_code}")
        detail = ""
        if refusals:
            detail = _bounded("prominence_claims_refused=" + ";".join(refusals), MAX_METADATA_VALUE_LENGTH)
        return accepted_claims, detail

    @staticmethod
    def _refusal(
        candidate: Mapping[str, Any],
        entry: QueueEntry,
        reason: str,
        *,
        reason_detail: str = "",
        rejected: Sequence[RejectedSource] = (),
        sources: Sequence[AcceptedSource] = (),
    ) -> StageOutcome:
        if reason not in REASON_CODES:
            raise ValueError(f"unknown grounded-description reason: {reason}")
        updated = QueueEntry(
            **{**entry.to_dict(), "attempt_status": OUTCOME_ATTEMPTED_NO_DESCRIPTION, "reason_code": reason}
        )
        return StageOutcome(
            candidate_id=entry.candidate_id,
            outcome=OUTCOME_ATTEMPTED_NO_DESCRIPTION,
            reason_code=reason,
            reason_detail=_bounded(reason_detail, MAX_METADATA_VALUE_LENGTH),
            identity_key=str(candidate.get("identity_key", "")),
            scheduling=updated,
            rejected_source_reasons=tuple(rejected),
            # Post-gate refusals keep the verified accepted sources (issue
            # #684): the gate proved these pages about this work, so a later
            # repair pass can start from them instead of re-discovering.
            # Pre-gate refusals pass nothing — there is nothing verified.
            research_sources=tuple(sources),
        )


# Stable service-style alias for callers that prefer an adapter name.
GroundedDescriptionStageRunner = GroundedDescriptionStage


@dataclass(frozen=True)
class PreflightResult:
    """Bounded discovery adapter health result."""

    ok: bool
    reason_code: str
    message: str

    def __iter__(self) -> Iterator[bool | str]:
        """Keep the former ``ok, reason = preflight()`` seam usable."""
        yield self.ok
        yield self.reason_code


@dataclass(frozen=True)
class HTTPResponse:
    """Bounded response returned by the standard-library transport seam."""

    url: str
    status: int
    headers: Mapping[str, str]
    body: bytes
    truncated: bool = False


def _header_content_length(headers: Mapping[str, str]) -> int | None:
    raw_value = next(
        (value for key, value in headers.items() if str(key).casefold() == "content-length"),
        "",
    )
    try:
        value = str(raw_value).strip()
        return max(0, int(value)) if value else None
    except (TypeError, ValueError):
        return None


class HTTPTransport(Protocol):
    def request(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
        max_redirects: int,
        allowed_hosts: set[str] | None = None,
        remaining_seconds: Callable[[], float] | None = None,
    ) -> HTTPResponse: ...


class BoundedHTTPError(RuntimeError):
    """A bounded request failed before it produced accepted evidence."""

    def __init__(self, message: str, *, reason_code: str = "") -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _request_timeout(timeout_seconds: float, remaining_seconds: Callable[[], float] | None) -> float:
    if remaining_seconds is None:
        return float(timeout_seconds)
    remaining = remaining_seconds()
    if remaining <= 0:
        raise BoundedHTTPError("candidate deadline expired", reason_code=REASON_CANDIDATE_BUDGET_EXHAUSTED)
    return min(float(timeout_seconds), remaining)


class _BoundedRedirectHandler(HTTPRedirectHandler):
    def __init__(
        self,
        max_redirects: int,
        allowed_hosts: set[str] | None,
        remaining_seconds: Callable[[], float] | None = None,
    ) -> None:
        super().__init__()
        self._max_redirects = max_redirects
        self._allowed_hosts = allowed_hosts
        self._remaining_seconds = remaining_seconds

    def http_request(self, req: Any) -> Any:
        # urllib reopens redirects with the preceding hop's timeout. Narrow it
        # after each reopen so no connection gets a fresh candidate allowance.
        req.timeout = _request_timeout(req.timeout, self._remaining_seconds)
        return req

    https_request = http_request

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Request | None:
        redirect_count = len(getattr(req, "redirect_dict", {}))
        if redirect_count >= self._max_redirects:
            raise BoundedHTTPError("redirect limit exceeded")
        parsed = urlsplit(newurl)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            raise BoundedHTTPError("redirect left HTTP(S) reach")
        if self._allowed_hosts is not None and parsed.hostname.casefold() not in self._allowed_hosts:
            raise BoundedHTTPError("redirect left the allowed host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class StandardLibraryHTTPTransport:
    """Bounded HTTP(S) transport with no third-party or search dependencies."""

    def __init__(self, *, max_redirects: int = 3) -> None:
        self.max_redirects = min(max(0, max_redirects), 3)

    def request(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
        max_redirects: int | None = None,
        allowed_hosts: set[str] | None = None,
        remaining_seconds: Callable[[], float] | None = None,
    ) -> HTTPResponse:
        def get() -> HTTPResponse:
            return self._request(
                url,
                timeout_seconds=timeout_seconds,
                max_response_bytes=max_response_bytes,
                max_redirects=max_redirects,
                allowed_hosts=allowed_hosts,
                remaining_seconds=remaining_seconds,
            )

        if remaining_seconds is None:
            return get()
        _request_timeout(timeout_seconds, remaining_seconds)
        answers: list[HTTPResponse | BaseException] = []

        def run() -> None:
            try:
                answers.append(get())
            except BaseException as exc:
                answers.append(exc)

        # Socket timeouts cannot bound trickling headers (or redirect bodies).
        # As in Kirkus pre-collection, abandon the daemon at the deadline; any
        # late response is closed by _request and cannot become evidence.
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(max(0.0, remaining_seconds()))
        if worker.is_alive() or remaining_seconds() <= 0:
            raise BoundedHTTPError("candidate deadline expired", reason_code=REASON_CANDIDATE_BUDGET_EXHAUSTED)
        answer = answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def _request(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
        max_redirects: int | None,
        allowed_hosts: set[str] | None,
        remaining_seconds: Callable[[], float] | None,
    ) -> HTTPResponse:
        canonical = canonicalize_url(url)
        if not canonical:
            raise BoundedHTTPError("only canonical HTTP(S) URLs may be fetched")
        parsed = urlsplit(canonical)
        hosts = {host.casefold() for host in allowed_hosts} if allowed_hosts is not None else None
        if hosts is not None and parsed.hostname and parsed.hostname.casefold() not in hosts:
            raise BoundedHTTPError("URL is outside the allowed host set")
        request = Request(canonical, headers={"User-Agent": "AutoScout grounded-description/1"})
        handler = _BoundedRedirectHandler(
            self.max_redirects if max_redirects is None else min(max(0, max_redirects), self.max_redirects),
            hosts,
            remaining_seconds,
        )
        opener = build_opener(handler)
        try:
            response = opener.open(request, timeout=_request_timeout(timeout_seconds, remaining_seconds))
            with response:
                headers = {
                    str(key).casefold(): str(value)
                    for key, value in response.headers.items()
                    if str(key).casefold() in {"content-type", "content-length"}
                }
                limit = max(0, int(max_response_bytes))
                content_length = _header_content_length(headers)

                def read(size: int) -> bytes:
                    raw, timed_out = read_bounded_http_bytes(response, size, remaining_seconds)
                    if timed_out:
                        raise BoundedHTTPError(
                            "candidate deadline expired", reason_code=REASON_CANDIDATE_BUDGET_EXHAUSTED
                        )
                    return cast(bytes, raw)

                chunks: list[bytes] = []
                total = 0
                truncated = content_length is not None and content_length > limit
                while total < limit:
                    remaining = limit - total
                    chunk = read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    if len(chunk) > remaining:
                        truncated = True
                        chunk = chunk[:remaining]
                    chunks.append(chunk)
                    total += len(chunk)
                if total == limit and content_length is None and not truncated:
                    probe = read(1)
                    truncated = bool(probe)
                return HTTPResponse(
                    url=str(response.geturl()),
                    status=int(response.getcode() or 0),
                    headers=headers,
                    body=b"".join(chunks)[:limit],
                    truncated=truncated,
                )
        except HTTPError as exc:
            if remaining_seconds is not None and remaining_seconds() <= 0:
                exc.close()
                raise BoundedHTTPError(
                    "candidate deadline expired", reason_code=REASON_CANDIDATE_BUDGET_EXHAUSTED
                ) from exc
            raise
        except BoundedHTTPError:
            raise
        except (OSError, URLError, TimeoutError) as exc:
            raise BoundedHTTPError("bounded HTTP request failed") from exc


class SourceClassifier:
    """Classify one source URL for admission and owner identity.

    The tiered derivation of issue #687 decides, under the committed owner
    policy (``configs/source-owner-policy.json``): every host discovery finds
    is readable unless it is denylisted, retail and catalogue hosts share one
    owner, and every other host is owned by its registrable domain. The
    grounded stage and the description adjudication stage both classify this
    way (issues #689 and #1123).

    Without an ``owner_policy`` nothing is classified: every URL is
    ``unknown``. Production always passes the committed policy; see
    :func:`committed_source_classifier`.
    """

    def __init__(self, owner_policy: OwnerPolicy | None = None) -> None:
        self.owner_policy = owner_policy

    def _classify_by_owner(self, url: str) -> dict[str, Any]:
        from autoscout.source_owner import TIER_SHARED_OWNER
        from autoscout.source_owner import derive_source_owner

        assert self.owner_policy is not None  # callers check before dispatching
        derived = derive_source_owner(url, self.owner_policy)
        host = derived["host"]
        if not derived["admitted"]:
            return {
                "classification": "unknown",
                "host": host,
                "tier": derived["tier"],
                "source_metadata": {"tier": derived["tier"]},
            }
        shared = derived["tier"] == TIER_SHARED_OWNER
        # source_kind only orders the per-candidate fetch budget. Storefronts
        # keep the existing retail preference so they are fetched after
        # independent hosts; an independent host claims nothing beyond being
        # its own owner, which is all derivation actually knows.
        source_kind = "retail_listing" if shared else "independent"
        source_family = "retail_listing" if shared else "independent"
        return {
            "classification": "classified",
            "source_kind": source_kind,
            "source_family": source_family,
            "owner_id": derived["owner_id"],
            "tier": derived["tier"],
            "policy_metadata_version": "owner-derived-1",
            "path_rule": "",
            "host": host,
            "source_metadata": {
                "source_kind": source_kind,
                "source_family": source_family,
                "owner_id": derived["owner_id"],
                "tier": derived["tier"],
            },
        }

    def classify(self, url: str) -> dict[str, Any]:
        canonical = canonicalize_url(url)
        if not canonical:
            return {"classification": "unknown"}
        host = canonicalize_hostname(urlsplit(canonical).hostname or "")
        # A Publishers Marketplace page describes a rights deal, not the
        # book, so no policy can admit it as grounding evidence (issue #947).
        if is_pm_url(canonical) or self.owner_policy is None:
            return {"classification": "unknown", "host": host}
        return self._classify_by_owner(canonical)


def committed_source_classifier(repo_path: Path | None = None) -> SourceClassifier:
    """Build the classifier under the committed owner policy.

    The policy is read from ``configs/source-owner-policy.json`` under
    ``repo_path`` (the repository root by default), and a malformed policy
    raises, so a broken file stops the stage at startup.
    """
    from autoscout.source_owner import load_owner_policy

    root = repo_path if repo_path is not None else repository_root()
    return SourceClassifier(load_owner_policy(root / "configs" / "source-owner-policy.json"))


# One shared ISBN plausibility rule with two callers: discovery queries and
# grounded page extraction (issue #531). The stage must never learn an ISBN
# that discovery would refuse to search with.
_ISBN_PLAUSIBILITY_RE = re.compile(r"(?:\d{9}[\dX]|\d{13})", flags=re.IGNORECASE | re.ASCII)


def plausible_isbn(raw: str) -> str:
    """Return the normalized ISBN when it is plausible for discovery use, else ``''``.

    Discovery queries and grounded extraction share this single rule, so a
    learned ISBN is always a normalized ISBN-10 or ISBN-13 value that
    ``_build_discovery_queries`` accepts.
    """
    isbn = normalize_isbn(raw)
    if isbn and _ISBN_PLAUSIBILITY_RE.fullmatch(isbn):
        return isbn
    return ""


# A subtitle follows a colon, or a spaced hyphen, en dash or em dash. A hyphen
# inside a word ("Self-Healing") separates nothing (issue #708).
_SUBTITLE_SEPARATOR = re.compile(r"[:：]|\s[-–—]\s")
# Nonfiction.fr prints the subtitle after a full stop:
# ``Chief Geopolitical Officer. Agir en entrepreneur, penser en ...``.  With no
# other separator the whole string was quoted, the search returned nothing, and
# no main-title query followed (issue #758).  A full stop also ends an
# abbreviation (``Dr. Seuss``, ``St. Louis``), so it separates a subtitle only
# before a capital letter and after a main title of at least two words whose
# last word has four letters or more.
_FULL_STOP_SUBTITLE_SEPARATOR = re.compile(r"\.\s+(?=\S)")
_MAIN_TITLE_MIN_WORDS = 2
_MAIN_TITLE_LAST_WORD_MIN_LETTERS = 4


def _discovery_main_title(title: str) -> str:
    """Return the title without its subtitle, or the title when it has none."""
    main_title = _SUBTITLE_SEPARATOR.split(title, maxsplit=1)[0].strip()
    if main_title != title:
        return main_title
    for match in _FULL_STOP_SUBTITLE_SEPARATOR.finditer(title):
        if not title[match.end()].isupper():
            continue
        words = re.findall(r"\w+", title[: match.start()], flags=re.UNICODE)
        if len(words) >= _MAIN_TITLE_MIN_WORDS and len(words[-1]) >= _MAIN_TITLE_LAST_WORD_MIN_LETTERS:
            return title[: match.start()].strip()
    return title


def _discovery_author_names(authors: Iterable[Any]) -> list[str]:
    """Return each author as one searchable name in reading order.

    A source can print ``Surname, First`` and can put several people in one
    value (issue #756).  Sent as printed, ``Iraki, Juma / Oksvold, Ronny`` is a
    phrase no page contains.  The stored author values are not changed.
    """
    names: list[str] = []
    for author in authors:
        for component in _author_components(author):
            name = normalize_author_display(component)
            if name and name not in names:
                names.append(name)
    return names


def _build_discovery_queries(candidate: Mapping[str, Any]) -> list[str]:
    """Return one deterministic, high-signal query for one candidate.

    Complete titles use the exact quoted phrase when they contain no quote.
    A title ending in a Unicode ellipsis or three ASCII dots is treated as
    truncated and recovered at the terminal marker while preserving useful
    intact text. The query also includes each author as one name in reading
    order, the literal word ``book``, and a plausible ISBN when one is present.
    This keeps title precision and identifier reachability.

    A complete quoted title with a subtitle (after a colon, after a hyphen,
    en dash or em dash with a space on each side, or after a full stop that
    ends a main title and not an abbreviation) adds a second query that
    quotes only the main title. Subtitles change between announcement and
    publication, so the full phrase can match nothing for a book the web knows
    well (issue #708). The provider issues it only after the first query came
    back empty.
    """

    raw_title = str(candidate.get("title", "")).strip()
    title = _recover_truncated_title(raw_title)
    authors = candidate.get("primary_authors", [])
    if isinstance(authors, str):
        authors = [authors]
    author = ", ".join(_discovery_author_names(authors))
    if _has_terminal_truncation_marker(raw_title) and not _recovered_title_is_useful(title):
        title_term = ""
    elif not title:
        title_term = ""
    elif '"' in title:
        title_term = title
    else:
        title_term = f'"{title}"'
    isbn = plausible_isbn(str(candidate.get("isbn", "")))

    def query(title_phrase: str) -> str:
        parts = [part for part in (title_phrase, author) if part]
        parts.append("book")
        if isbn:
            parts.append(isbn)
        return " ".join(parts)

    if not title_term and not author and not isbn:
        return []
    queries = [query(title_term)]
    if title_term == f'"{title}"' and not _has_terminal_truncation_marker(raw_title):
        main_title = _discovery_main_title(title)
        if main_title and main_title != title:
            queries.append(query(f'"{main_title}"'))
    return queries


_LANGUAGE_FIELDS = ("source_language", "language", "content_language", "original_language")


class LanguageResolutionState(str, Enum):
    ABSENT = "absent"
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class LanguageResolution:
    state: LanguageResolutionState
    language: str = ""


def _language_from_mapping(value: Mapping[str, Any]) -> LanguageResolution:
    """Resolve one language with an explicit absent, resolved, or ambiguous state."""
    values: list[str] = []
    for key in _LANGUAGE_FIELDS:
        language = normalize_language_code(value.get(key))
        if language:
            values.append(language)
    nested = value.get("source_metadata")
    if isinstance(nested, Mapping):
        for key in _LANGUAGE_FIELDS:
            language = normalize_language_code(nested.get(key))
            if language:
                values.append(language)
    unique = sorted(set(values))
    if len(unique) > 1:
        return LanguageResolution(LanguageResolutionState.AMBIGUOUS)
    if unique:
        return LanguageResolution(LanguageResolutionState.RESOLVED, unique[0])
    return LanguageResolution(LanguageResolutionState.ABSENT)


def candidate_source_language(candidate: Mapping[str, Any]) -> str:
    """Resolve a candidate language without guessing from titles or URLs.

    Explicit candidate fields take precedence over ``primary_signal``.
    Conflicting valid explicit fields fail closed instead of falling through.
    Source evidence is used only when every usable evidence language agrees.
    """
    explicit = _language_from_mapping(candidate)
    if explicit.state is LanguageResolutionState.AMBIGUOUS:
        return ""
    if explicit.state is LanguageResolutionState.RESOLVED:
        return explicit.language
    primary_signal = candidate.get("primary_signal")
    if isinstance(primary_signal, Mapping):
        primary = _language_from_mapping(primary_signal)
        if primary.state is LanguageResolutionState.AMBIGUOUS:
            return ""
        if primary.state is LanguageResolutionState.RESOLVED:
            return primary.language
    evidence = candidate.get("source_evidence")
    if not isinstance(evidence, Sequence) or isinstance(evidence, bytes | bytearray):
        return ""
    languages: set[str] = set()
    for item in evidence:
        if not isinstance(item, Mapping):
            continue
        resolution = _language_from_mapping(item)
        if resolution.state is LanguageResolutionState.AMBIGUOUS:
            return ""
        if resolution.state is LanguageResolutionState.RESOLVED:
            languages.add(resolution.language)
    return next(iter(languages)) if len(languages) == 1 else ""


def _endpoint_with_search_language(endpoint: str, language: str) -> str:
    """Replace endpoint ``search_lang`` values with one validated value."""
    parsed = urlsplit(endpoint)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() != "search_lang"
    ]
    if language:
        query.append(("search_lang", language))
    return parsed._replace(query=urlencode(query)).geturl()


class HTTPDiscoveryProvider:
    """Perform at most one bounded discovery request and classify page URLs.

    The endpoint is an operator-supplied HTTP(S) URL template.
    Its ``query``, ``title``, and ``author`` fields are encoded before
    interpolation.
    Search snippets are returned as discovery metadata only and are rejected
    by the grounding gate.
    When a query can be built, discovery uses one deterministic query
    containing the exact quoted phrase for a complete title when safe, or an
    intact quoted prefix for a title with a terminal truncation marker, plus
    the author, ``book``, and a plausible ISBN when present.
    The stage owns vendor spacing for discovery requests.
    The optional limiter is also used here before the compatibility fallback
    in :meth:`preflight`, because that fallback spends vendor quota.
    The health request never uses the limiter.
    """

    def __init__(
        self,
        endpoint_template: str,
        *,
        transport: HTTPTransport | None = None,
        classifier: SourceClassifier | None = None,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        rate_limiter: VendorRateLimiter | None = None,
    ) -> None:
        self.endpoint_template = endpoint_template
        self.transport = transport or StandardLibraryHTTPTransport()
        self.classifier: SourceClassifier = classifier or SourceClassifier()
        self.max_response_bytes = max_response_bytes
        self.rate_limiter = rate_limiter

    def preflight(self, **kwargs: Any) -> PreflightResult:
        """Check adapter health without vendor quota when ``/health`` works.

        Older adapters that do not expose ``/health`` are given one bounded,
        deterministic search request after a 400 or 404 health response.
        Meaningful adapter errors are returned directly and are never retried.
        """
        timeout_seconds = float(kwargs.get("timeout_seconds", REQUEST_INACTIVITY_TIMEOUT_SECONDS))
        health_endpoint = self._health_endpoint()
        try:
            response = self._request(health_endpoint, timeout_seconds=timeout_seconds)
            if response.status in {400, 404}:
                if self.rate_limiter is not None:
                    self.rate_limiter.wait()
                response = self._request(
                    self.endpoint_template.format(
                        query=quote_plus("autoscout preflight"),
                        title=quote_plus("autoscout preflight"),
                        author="",
                    ),
                    timeout_seconds=timeout_seconds,
                )
        except Exception:
            return PreflightResult(False, PREFLIGHT_ADAPTER_NOT_RUNNING, "discovery adapter is not running")
        return self._preflight_result(response)

    def _health_endpoint(self) -> str:
        parsed = urlsplit(self.endpoint_template)
        path = parsed.path.rstrip("/")
        base_path = path.rsplit("/", 1)[0] if path else ""
        health_path = f"{base_path}/health" if base_path else "/health"
        return urlunsplit((parsed.scheme, parsed.netloc, health_path, "", ""))

    def _request(self, endpoint: str, *, timeout_seconds: float) -> HTTPResponse:
        try:
            return self.transport.request(
                endpoint,
                timeout_seconds=timeout_seconds,
                max_response_bytes=self.max_response_bytes,
                max_redirects=3,
            )
        except HTTPError as exc:
            limit = max(0, int(self.max_response_bytes))
            body = exc.read(limit)
            headers = {
                str(key).casefold(): str(value)
                for key, value in (exc.headers.items() if exc.headers is not None else [])
            }
            content_length = _header_content_length(headers)
            truncated = len(body) > limit or (content_length is not None and content_length > limit)
            if limit > 0 and len(body) == limit and content_length is None:
                truncated = True
            return HTTPResponse(
                str(exc.geturl()),
                int(exc.code),
                headers,
                body[:limit],
                truncated=truncated,
            )

    @staticmethod
    def _response_error(response: HTTPResponse) -> str:
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ""
        if isinstance(payload, Mapping):
            error = payload.get("error")
            if isinstance(error, str) and error.strip():
                return error.strip()
        return ""

    def _preflight_result(self, response: HTTPResponse) -> PreflightResult:
        if 200 <= response.status < 300:
            return PreflightResult(True, PREFLIGHT_HEALTHY, "discovery adapter is healthy")
        error = self._response_error(response)
        if response.status == 503:
            return PreflightResult(
                False,
                PREFLIGHT_ADAPTER_KEY_MISSING,
                error or "discovery adapter key is not configured",
            )
        return PreflightResult(
            False,
            PREFLIGHT_ADAPTER_VENDOR_ERROR,
            error or f"discovery adapter returned HTTP {response.status}",
        )

    def discover(self, candidate: Mapping[str, Any], **kwargs: Any) -> Sequence[DiscoveryResult]:
        title = str(candidate.get("title", "")).strip()
        authors = candidate.get("primary_authors", [])
        if isinstance(authors, str):
            authors = [authors]
        author = ", ".join(_discovery_author_names(authors))
        candidate_language = candidate_source_language(candidate)
        queries = _build_discovery_queries(candidate)
        if not queries:
            return []
        inactivity_timeout = float(kwargs.get("timeout_seconds", REQUEST_INACTIVITY_TIMEOUT_SECONDS))
        request_budget = kwargs.get("request_budget")
        raw_results: list[Any] = []
        for index, query in enumerate(queries):
            if index > 0:
                # A later query is only a fallback for an empty answer, and it
                # must fit the same discovery phase: budget capacity first,
                # then the vendor floor, bounded by the phase deadline (#708).
                if raw_results:
                    break
                if request_budget is not None:
                    try:
                        request_budget.ensure_request_available("discovery")
                    except BoundedHTTPError:
                        break
                if self.rate_limiter is not None:
                    deadline = request_budget.deadline if request_budget is not None else None
                    if self.rate_limiter.wait(deadline=deadline) is None:
                        break
            raw_results = self._search(
                query,
                title=title,
                author=author,
                language=candidate_language,
                inactivity_timeout=inactivity_timeout,
                request_budget=request_budget,
            )
        seen: set[str] = set()
        results: list[DiscoveryResult] = []
        for raw in raw_results:
            if not isinstance(raw, Mapping):
                continue
            url = canonicalize_url(str(raw.get("url", raw.get("link", ""))))
            if not url or url in seen:
                continue
            seen.add(url)
            classification = self.classifier.classify(url)
            is_snippet = raw.get("is_search_snippet") is True or str(raw.get("result_kind", "")) == "search_snippet"
            results.append(
                DiscoveryResult(
                    url=url,
                    source_kind=classification.get("source_kind", ""),
                    source_family=classification.get("source_family", ""),
                    owner_id=classification.get("owner_id", ""),
                    source_language=str(raw.get("source_language", "") or candidate_language),
                    classification=classification.get("classification", "unknown"),
                    is_search_snippet=is_snippet,
                    title_evidence_route=str(raw.get("title_evidence_route", "direct_original")),
                    title=_bounded(raw.get("title", ""), MAX_DISCOVERY_TITLE_TEXT),
                    route_kind=str(classification.get("route_kind", "")),
                    title_evidence=(
                        dict(raw["title_evidence"])
                        if isinstance(raw.get("title_evidence"), Mapping)
                        else (
                            dict(raw["publication_title_evidence"])
                            if isinstance(raw.get("publication_title_evidence"), Mapping)
                            else None
                        )
                    ),
                    metadata={
                        "host": classification.get("host", ""),
                        "path_rule": classification.get("path_rule", ""),
                        "policy_metadata_version": classification.get("policy_metadata_version", ""),
                    },
                    classification_evidence=classification,
                )
            )
        return results

    def _search(
        self,
        query: str,
        *,
        title: str,
        author: str,
        language: str,
        inactivity_timeout: float,
        request_budget: CandidateRequestBudget | None,
    ) -> list[Any]:
        """Issue one bounded discovery request and return its raw result list."""
        endpoint = self.endpoint_template.format(
            query=quote_plus(query),
            title=quote_plus(title),
            author=quote_plus(author),
        )
        endpoint = _endpoint_with_search_language(endpoint, language)
        timeout = inactivity_timeout
        try:
            if request_budget is not None:
                timeout = request_budget.before_request("discovery", timeout)
            deadline_kwargs: dict[str, Any] = (
                {"remaining_seconds": request_budget.remaining_seconds} if request_budget is not None else {}
            )
            response = self.transport.request(
                endpoint,
                timeout_seconds=timeout,
                max_response_bytes=self.max_response_bytes,
                max_redirects=3,
                **deadline_kwargs,
            )
        except BoundedHTTPError:
            raise
        except Exception as exc:
            raise BoundedHTTPError("discovery response was not successful") from exc
        if not 200 <= response.status < 300:
            raise BoundedHTTPError("discovery response was not successful")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BoundedHTTPError("discovery response was not bounded JSON") from exc
        raw_results = payload.get("results", []) if isinstance(payload, Mapping) else []
        return raw_results if isinstance(raw_results, list) else []


def _complete_jsonld_document(value: str) -> str:
    """Keep valid JSON-LD and discard only a trailing incomplete markup suffix."""
    document = value.strip()
    if not document:
        return ""
    try:
        json.loads(document)
        return document
    except (TypeError, json.JSONDecodeError):
        pass

    try:
        _parsed, end = json.JSONDecoder().raw_decode(document)
    except (TypeError, json.JSONDecodeError):
        return ""
    suffix = document[end:].strip()
    if not suffix.startswith("<") or ">" in suffix:
        return ""
    return document[:end]


# The schema.org types that are a book. ``Audiobook`` is a subtype of
# ``Book``, and an audiobook ranking is one of this project's own signal
# families, so a retailer that describes the work with an ``Audiobook``
# record was silently handing over no author at all: the record was never
# read, the page then presented no author, and citation verification
# dropped a correct citation (issue #649). Only true book types belong
# here. ``Product`` and ``Course`` describe a thing that is sold or
# taught, not a work, and would let a store page speak for a book.
_JSONLD_BOOK_TYPES = frozenset({"book", "audiobook"})


class _PageEvidenceParser(HTMLParser):
    """Collect bounded page evidence with narrow detail-container selectors by class and id."""

    _EXCLUDED_TAGS = frozenset({"nav", "header", "footer", "aside", "script", "style", "noscript", "form", "button"})
    _DETAIL_SUBJECT_CLASSES = frozenset(
        {"bookmarks_a_review_pullquote", "book_manual_description", "product-description"}
    )
    # Retailers mark the same kind of container with an id instead of a class.
    # Amazon serves the publisher book description inside this one.
    _DETAIL_SUBJECT_IDS = frozenset({"bookdescription_feature_div"})
    # Containers that list other books. An author link inside one names
    # another book's author, never this page's book (issue #709).
    _RELATED_CONTAINER = re.compile(
        r"related|recommend|similar|carousel|upsell|cross-?sell|also|vitrine|sugest|suggest|customers-who"
    )
    _VOID_TAGS = frozenset(
        {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
    )
    # A hidden element prints nothing, so a link inside one is not printed
    # author evidence (review of #710).
    _HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden")
    _HIDDEN_CLASSES = frozenset(
        {"hidden", "hide", "d-none", "sr-only", "visually-hidden", "visuallyhidden", "screen-reader-text", "invisible"}
    )

    @classmethod
    def _is_hidden(cls, attributes: Mapping[str, str], classes: set[str]) -> bool:
        return (
            "hidden" in attributes
            or attributes.get("aria-hidden", "").strip().casefold() == "true"
            or bool(cls._HIDDEN_STYLE.search(attributes.get("style", "").casefold()))
            or bool(classes & cls._HIDDEN_CLASSES)
        )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.og_title_parts: list[str] = []
        self.author_parts: list[str] = []
        self.book_author_parts: list[str] = []
        self.book_title_parts: list[str] = []
        self.description_parts: list[str] = []
        self.og_description_parts: list[str] = []
        self.site_name_parts: list[str] = []
        self.paragraph_parts: list[str] = []
        self.detail_subject_parts: list[str] = []
        self.author_link_parts: list[str] = []
        self.identity_text_parts: list[str] = []
        self._identity_text_size = 0
        self._author_link_depth = 0
        self._author_link_done = False
        # A related-books container or a hidden element: author links inside
        # never name this page's book author.
        self._not_book_author_tag = ""
        self._not_book_author_depth = 0
        self._active: str = ""
        self._ignored_depth = 0
        self._content_depth = 0
        self._paragraph_depth = 0
        self._detail_subject_depth = 0
        self._jsonld_active = False
        self._jsonld_parts: list[str] = []
        self._jsonld_size = 0
        self._jsonld_documents: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.casefold(): str(value or "") for key, value in attrs}
        tag = tag.casefold()

        if tag == "script":
            self._ignored_depth += 1
            self._jsonld_active = attributes.get("type", "").casefold() == "application/ld+json"
            self._jsonld_parts = []
            self._jsonld_size = 0
            return
        if tag in self._EXCLUDED_TAGS:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return

        classes = set(attributes.get("class", "").casefold().split())
        element_id = attributes.get("id", "").strip().casefold()
        if tag == "div":
            if self._detail_subject_depth:
                self._detail_subject_depth += 1
            elif (classes & self._DETAIL_SUBJECT_CLASSES) or element_id in self._DETAIL_SUBJECT_IDS:
                self._detail_subject_depth = 1

        if self._not_book_author_depth:
            if tag == self._not_book_author_tag:
                self._not_book_author_depth += 1
        elif tag not in self._VOID_TAGS and (
            self._RELATED_CONTAINER.search(f"{attributes.get('class', '')} {element_id}".casefold())
            or self._is_hidden(attributes, classes)
        ):
            # Opened by the link itself too, so a hidden link never counts.
            self._not_book_author_tag = tag
            self._not_book_author_depth = 1
        if tag == "a":
            if self._author_link_depth:
                self._author_link_depth += 1
            elif (
                not self._author_link_done
                and not self._not_book_author_depth
                and _is_author_link(attributes.get("href", ""))
            ):
                self._author_link_depth = 1

        if tag == "title":
            self._active = "title"
        elif tag in {"main", "article"}:
            self._content_depth += 1
        elif tag == "p" and self._content_depth:
            self._paragraph_depth += 1
            self._active = "subject"

        for key, value in attributes.items():
            if not value:
                continue
            if key == "data-book-title" or (
                key == "data-title"
                and attributes.get("data-page-type", "").casefold()
                in {
                    "product detail page",
                    "book",
                }
            ):
                self.book_title_parts.append(value)
            elif key in {"data-book-authors", "primaryauthors"}:
                self.book_author_parts.append(value)
            elif key == "data-author" and self._content_depth:
                self.book_author_parts.append(value)

        if attributes.get("itemprop", "").casefold() == "author" and self._content_depth:
            self._active = "book_author"
        elif tag == "meta":
            name = attributes.get("name", "").casefold()
            property_name = attributes.get("property", "").casefold()
            itemprop = attributes.get("itemprop", "").casefold()
            content = attributes.get("content", "").strip()
            if itemprop == "author" and content:
                self.book_author_parts.append(content)
            elif name in {"author", "dc.creator"} and content:
                self.author_parts.append(content)
            elif name == "description" and content:
                self.description_parts.append(content)
            elif property_name == "og:description" and content:
                self.og_description_parts.append(content)
            elif property_name == "og:site_name" and content:
                self.site_name_parts.append(content[:200])
            elif property_name == "og:title" and content:
                self.og_title_parts.append(content)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "script":
            if self._jsonld_active:
                document = "".join(self._jsonld_parts).strip()
                if document:
                    self._jsonld_documents.append(document)
            self._jsonld_active = False
            self._jsonld_parts = []
            self._jsonld_size = 0
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if self._ignored_depth:
            if tag in self._EXCLUDED_TAGS:
                self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if self._not_book_author_depth and tag == self._not_book_author_tag:
            self._not_book_author_depth -= 1
        if tag == "a" and self._author_link_depth:
            self._author_link_depth -= 1
            # A link with no text (a cover image) is not the author name, so
            # the next author link still gets its turn.
            self._author_link_done = not self._author_link_depth and bool(self.author_link_parts)
        if tag == "div" and self._detail_subject_depth:
            self._detail_subject_depth = max(0, self._detail_subject_depth - 1)
        if tag == "p":
            self._paragraph_depth = max(0, self._paragraph_depth - 1)
            if not self._paragraph_depth:
                self._active = ""
        elif tag in {"main", "article"}:
            self._content_depth = max(0, self._content_depth - 1)
        elif tag == "title" or self._active == "book_author":
            self._active = ""

    def handle_data(self, data: str) -> None:
        if self._jsonld_active:
            remaining = 100_000 - self._jsonld_size
            if remaining > 0:
                chunk = data[:remaining]
                self._jsonld_parts.append(chunk)
                self._jsonld_size += len(chunk)
            return
        if self._ignored_depth:
            return
        text = " ".join(data.split())
        if not text:
            return
        if not self._not_book_author_depth and self._identity_text_size < MAX_IDENTITY_EVIDENCE_TEXT:
            chunk = text[: MAX_IDENTITY_EVIDENCE_TEXT - self._identity_text_size]
            self.identity_text_parts.append(chunk)
            self._identity_text_size += len(chunk)
        if self._author_link_depth and sum(len(part) for part in self.author_link_parts) < MAX_AUTHOR_EVIDENCE_TEXT:
            self.author_link_parts.append(text[:MAX_AUTHOR_EVIDENCE_TEXT])
        if self._active == "title":
            self.title_parts.append(text)
        elif self._active == "book_author":
            self.book_author_parts.append(text)
        elif self._detail_subject_depth:
            # Paragraph text inside a detail-subject container belongs to
            # that container. Testing detail depth before paragraph depth
            # keeps the container's <p> text in detail_subject_parts so
            # the priority order (detail before paragraph) can use it.
            # The alternative - appending to both lists - would duplicate
            # evidence and pollute the generic paragraph fallback with
            # container-scoped text. Detail-scoped text should stay scoped.
            self.detail_subject_parts.append(text)
        elif self._active == "subject" and self._paragraph_depth:
            self.paragraph_parts.append(text)

    def finish(self) -> None:
        """Commit a complete JSON-LD document when its script tag is cut off."""
        if self._jsonld_active and self.rawdata:
            remaining = 100_000 - self._jsonld_size
            if remaining > 0:
                self._jsonld_parts.append(self.rawdata[:remaining])
                self._jsonld_size += min(len(self.rawdata), remaining)
            self.rawdata = ""
        if self._jsonld_active:
            document = _complete_jsonld_document("".join(self._jsonld_parts))
            if document:
                self._jsonld_documents.append(document)
            self._jsonld_active = False
            self._jsonld_parts = []
            self._jsonld_size = 0

    def book_records(self) -> list[Mapping[str, Any]]:
        records: list[Mapping[str, Any]] = []

        def visit(value: Any) -> None:
            if isinstance(value, Mapping):
                raw_type = value.get("@type", "")
                raw_types = raw_type if isinstance(raw_type, list) else [raw_type]
                types = {str(item).rsplit("/", 1)[-1].rsplit("#", 1)[-1].casefold() for item in raw_types}
                if types & _JSONLD_BOOK_TYPES:
                    records.append(value)
                for child in value.values():
                    if isinstance(child, Mapping | list):
                        visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        for document in self._jsonld_documents:
            try:
                visit(json.loads(document))
            except (TypeError, json.JSONDecodeError):
                continue
        return records


def _clean_page_title(value: str) -> str:
    """Remove common page/site suffixes while retaining page-provided wording.

    A spaced en dash or em dash ends the title the same way a spaced hyphen
    does: ``Bakmennene <en dash> TRONSMO`` is the title ``Bakmennene`` (issue #757).
    A real title with a spaced dash therefore keeps only its first part.
    """
    title = " ".join(unescape(value or "").split())
    for delimiter in (" - ", " – ", " — ", " | ", " :: "):
        title = title.split(delimiter, 1)[0].strip()
    return title


# Amazon listing-chrome knowledge lives beside its only consumer. The
# AMAZON_CHARTS_MARKETPLACE_LANGUAGE map in autoscout/config.py governs
# chart-source validation and is deliberately not reused here.
_AMAZON_LISTING_PREFIX_RE = re.compile(r"^amazon(?:\.[a-z]{2,})+\s*:\s*", re.IGNORECASE)
# The same listing also ships with the storefront at the end of the title:
# "<title>: <author>: <isbn>: Amazon.com: Books". Only the leading layout was
# recognized, so the trailing one kept the whole chrome inside the title and
# then disagreed with the clean title of every peer source (issue #563).
_AMAZON_LISTING_DOMAIN_RE = re.compile(r"^amazon(?:\.[a-z]{2,})+$", re.IGNORECASE)
# Only well-known storefront labels are listed, so an unlisted category keeps the
# whole title unchanged. Unknown locales degrade to the safe no-op, not a truncation.
_AMAZON_LISTING_CATEGORY_SEGMENTS = frozenset(
    {
        "books",
        "kindle ebooks",
        "kindle store",
        "livres",
        "bücher",
        "libros",
        "libri",
        "boeken",
        "böcker",
        "livros",
        "książki",
    }
)


def _is_isbn_segment(value: str) -> bool:
    """Match an ISBN-13 or ISBN-10 segment, allowing optional hyphens.

    The ISBN-10 form is deliberately not checksum-verified because the surrounding
    anchors (Amazon prefix, author segment, known category) keep the guard conservative.
    """
    digits = value.replace("-", "")
    return bool(re.fullmatch(r"97[89]\d{10}", digits) or re.fullmatch(r"\d{9}[\dXx]", digits))


def _strip_amazon_trailing_listing_chrome(value: str) -> str:
    """Strip the ``...: <author>: <isbn>: Amazon.<tld>: <category>`` layout.

    Every anchor of the leading layout is required here too, in one fixed
    order: a known category label last, the retailer domain before it, an ISBN
    segment before that, an author segment containing letters before that, and
    at least one title segment before all of them. A title that merely ends in
    a word from the category set is therefore never truncated.
    """
    segments = [segment.strip() for segment in value.split(":")]
    if len(segments) < 5:
        return value
    if segments[-1].casefold() not in _AMAZON_LISTING_CATEGORY_SEGMENTS:
        return value
    if _AMAZON_LISTING_DOMAIN_RE.match(segments[-2]) is None:
        return value
    if not _is_isbn_segment(segments[-3]):
        return value
    if not any(character.isalpha() for character in segments[-4]):
        return value
    book_title = ": ".join(segment for segment in segments[:-4] if segment)
    return book_title or value


def _strip_amazon_listing_chrome(value: str) -> str:
    """Strip the ``Amazon.<tld>: ...`` listing chrome for ISBN and eBook layouts.

    Two anchored layouts are recognized. Both require the retailer domain prefix,
    an author segment containing letters, and a final segment in the known
    category set. The ISBN layout also requires an ISBN segment as the
    third-from-last part and at least one title segment before it. The eBook
    layout also requires a title marker `` eBook`` (case-insensitive) followed
    by whitespace and a colon on the raw remainder, before splitting. The raw
    match preserves the space-before-colon signal that distinguishes the Chrome
    marker `` eBook :`` from a real title colon ``eBook:``. A title missing any
    anchor is returned unchanged, so a real title that merely resembles chrome
    (for example ``Amazon.com: Get Big Fast``) is never truncated. The
    trailing `` eBook`` marker is removed from the returned title. Category
    labels from major non-English marketplaces (``Livres``, ``Bücher``, ``Libros``,
    and so on) are recognized, and an unlisted label keeps the title unchanged.
    """
    trailing = _strip_amazon_trailing_listing_chrome(value)
    if trailing != value:
        return trailing
    match = _AMAZON_LISTING_PREFIX_RE.match(value)
    if not match:
        return value
    remainder = value[match.end() :].strip()
    segments = [segment.strip() for segment in remainder.split(":")]
    if len(segments) < 3:
        return value
    category = segments[-1]
    if category.casefold() not in _AMAZON_LISTING_CATEGORY_SEGMENTS:
        return value
    author = segments[-2]
    if not any(character.isalpha() for character in author):
        return value
    # ISBN layout - title, ISBN, author, category. Keep the ISBN requirement exactly
    # as before and do not loosen ``_is_isbn_segment``.
    if len(segments) >= 4 and _is_isbn_segment(segments[-3]):
        book_title = ": ".join(segment for segment in segments[:-3] if segment)
        return book_title or value
    # eBook layout - title with " eBook" marker plus whitespace before the colon,
    # author, category. Match on the raw remainder before splitting so the
    # space-before-colon signal is not discarded. This keeps a real title such
    # as ``How to Write an eBook: A Guide`` intact while stripping the chrome
    # `` eBook :`` that Amazon adds.
    remainder_raw = value[match.end() :]
    ebook_matches = list(re.finditer(r" eBook\s+:", remainder_raw, flags=re.IGNORECASE))
    for ebook_match in reversed(ebook_matches):
        title_raw = remainder_raw[: ebook_match.start()].strip()
        if not title_raw:
            continue
        after_raw = remainder_raw[ebook_match.end() :]
        after_segments = [segment.strip() for segment in after_raw.split(":")]
        # After the marker we expect at least author and category, e.g.
        # " Wolf, Serena: Kindle Store" -> ["Wolf, Serena", "Kindle Store"]
        if len(after_segments) < 2:
            continue
        # Validate that the author and category after the marker align with the
        # already validated trailing segments. This keeps one rule with two
        # layouts, not two copies of the same walk.
        author_after = after_segments[0]
        category_after = after_segments[-1]
        if not any(character.isalpha() for character in author_after):
            continue
        if category_after.casefold() not in _AMAZON_LISTING_CATEGORY_SEGMENTS:
            continue
        # Residual risk: a book whose title genuinely ends in "eBook" and
        # which Amazon renders with a single " eBook :" marker would be
        # truncated (for example, a hypothetical title "How to Write an
        # eBook"). Amazon's observed Kindle format doubles the marker for
        # such titles ("How to Write an eBook: A Guide eBook : ..."), so a
        # single marker is not expected. An earlier dangling-article guard
        # that kept such single-marker titles was removed because it broke
        # real short titles like "Just Do It" and "Own It" when rendered
        # as Kindle chrome.
        return title_raw
    return value


# A retailer or review page title often reads "<title> by <author>".  A work
# title can contain the same marker - "The 25th Hour: How to Thrive (and Gain
# Ridiculous Amounts of Time) by Getting Out of Your Own Way" - and splitting
# on the first marker then invents the author "Getting Out of Your Own Way"
# and truncates the title.  Both halves of that mistake are fatal: the source
# fails author agreement against the target, and its shortened title fails
# title agreement against every peer source (issue #563).
#
# The split is therefore attempted from the right and is accepted only when
# the tail reads like a personal name.  When no tail qualifies the title is
# kept whole and no headline author is claimed, which is the safe outcome:
# a missing author is handled by the gate, an invented one is not.
_HEADLINE_AUTHOR_MARKER_RE = re.compile(r"\s+by\s+", re.IGNORECASE)
_HEADLINE_AUTHOR_TAIL_RE = re.compile(r"^([^:|–—]+?)(?:\s*(?:[:|]|[–—]).*)?$")

# Longest observed real byline is four words plus a suffix ("Jr.", "III").
_MAX_HEADLINE_AUTHOR_WORDS = 5

# Lowercase words that belong inside a personal name.  Everything else in
# lowercase marks the tail as prose rather than a byline.  "of" is absent on
# purpose: it is the word that lets a title fragment pass as a name.
_HEADLINE_NAME_PARTICLES = frozenset(
    {
        "van",
        "von",
        "der",
        "den",
        "de",
        "del",
        "della",
        "di",
        "da",
        "dos",
        "du",
        "la",
        "le",
        "ter",
        "bin",
        "ibn",
        "al",
        "mac",
        "mc",
        "st",
    }
)


def _reads_like_personal_name(value: str) -> bool:
    words = value.split()
    if not words or len(words) > _MAX_HEADLINE_AUTHOR_WORDS:
        return False
    named = 0
    for word in words:
        stripped = word.strip(".,'\u2019-")
        if not stripped:
            return False
        if stripped.casefold() in _HEADLINE_NAME_PARTICLES:
            continue
        first = stripped[0]
        if not first.isalpha() or not first.isupper():
            return False
        named += 1
    return named > 0


def _title_and_headline_author(value: str) -> tuple[str, str]:
    title = value.strip()
    if not title:
        return "", ""
    markers = list(_HEADLINE_AUTHOR_MARKER_RE.finditer(title))
    for marker in reversed(markers):
        head = title[: marker.start()].strip()
        tail_match = _HEADLINE_AUTHOR_TAIL_RE.match(title[marker.end() :].strip())
        if not head or tail_match is None:
            continue
        author = tail_match.group(1).strip()
        if author and _reads_like_personal_name(author):
            return head, author
    return title, ""


def _structured_book_subject(record: Mapping[str, Any]) -> str:
    description = record.get("description", "")
    if isinstance(description, str) and description.strip():
        raw_subject = description
    else:
        raw_subject = record.get("about", "")
    if not isinstance(raw_subject, str):
        return ""
    return _usable_subject_text(raw_subject)


def _structured_book_isbn(record: Mapping[str, Any]) -> str:
    """Return the plausible normalized ISBN of one schema.org Book record.

    Only the exact record chosen by ``_select_book_record`` is read.  A
    publisher page can list several format-variant ISBNs in other records;
    reading those would turn edition variety into a false disagreement
    (issue #531).

    A multi-value ``isbn`` property has no second winner rule: the one
    plausible normalized value is returned only when every nonblank
    plausible entry normalizes to exactly one value.  Disagreeing values
    fail closed and yield ``''``.
    """
    raw = record.get("isbn", "")
    if isinstance(raw, Sequence) and not isinstance(raw, str | bytes | bytearray):
        candidates = {plausible for item in raw if (plausible := plausible_isbn(str(item)))}
        return next(iter(candidates)) if len(candidates) == 1 else ""
    return plausible_isbn(str(raw))


def _select_book_record(records: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Choose the most complete bounded Book record, preserving document order on ties."""
    scored: list[tuple[int, int, Mapping[str, Any]]] = []
    for index, record in enumerate(records):
        score = 0
        if str(record.get("name", "") or "").strip():
            score += 2
        if _jsonld_authors(record):
            score += 2
        if _structured_book_subject(record):
            score += 2
        if score:
            scored.append((score, -index, record))
    if not scored:
        return {}
    return max(scored, key=lambda item: (item[0], item[1]))[2]


def _jsonld_authors(record: Mapping[str, Any]) -> list[str]:
    raw_authors = record.get("author", [])
    if isinstance(raw_authors, str | Mapping):
        raw_authors = [raw_authors]
    if not isinstance(raw_authors, Sequence) or isinstance(raw_authors, bytes | bytearray):
        return []
    authors: list[str] = []
    for raw_author in raw_authors:
        if isinstance(raw_author, Mapping):
            author = str(raw_author.get("name", "")).strip()
        else:
            author = str(raw_author).strip()
        # A page may HTML-escape the JSON-LD it embeds, so an author who
        # shares a record with another arrives as "A &amp;amp; B". The
        # author-key split is on "&", so an escaped ampersand would hide
        # the second author behind a literal "amp" (issue #649). The title
        # and subject text already unescape; authors simply did not.
        author = " ".join(unescape(author).split())
        if author:
            authors.append(author)
    return authors


def _usable_subject_text(value: str) -> str:
    text = " ".join(unescape(value or "").split())
    if not text:
        return ""
    lowered = text.casefold()
    # Strip a leading retailer loyalty blurb and keep the publisher description that
    # follows it. Observed on Barnes and Noble product pages, where a membership offer
    # precedes the description and ends with a "Learn More" control. The markers are
    # tied to that observed capture, not a general rule, so an unrecognized prefix
    # leaves the text unchanged rather than guessing where a description begins.
    learn_more_match = re.search(r"learn\s+more", text, flags=re.IGNORECASE)
    if learn_more_match is not None:
        prefix_lower = text[: learn_more_match.start()].casefold()
        loyalty_markers = ("premium members", "save an extra", "collect stamps", "rewards", "10 stamps")
        if sum(marker in prefix_lower for marker in loyalty_markers) >= 2:
            remainder = text[learn_more_match.end() :].lstrip(" :.-\t")
            if not remainder.strip():
                return ""
            text = remainder
            lowered = text.casefold()
    commerce_markers = sum(
        marker in lowered
        for marker in ("online bookstore", "free shipping", "shop music", "customer service", "membership")
    )
    page_chrome_markers = (
        "digital library",
        "other retailers",
        "visit other sites",
        "today's top books",
        "want to know what people",
    )
    generic_app_or_device = re.search(r"\b(?:compatible|download)\s+\w+(?:\s+\w+)?\s+(?:app|devices?)\b", lowered)
    if (
        commerce_markers >= 2
        or ("online" in lowered and "bookstore" in lowered)
        or generic_app_or_device
        or any(marker in lowered for marker in page_chrome_markers)
    ):
        return ""
    return text


def _drop_leading_site_label(title: str, *, hostname: str, site_names: Sequence[str] = ()) -> str:
    """Drop a leading segment that only names the site (issue #867).

    ``_clean_page_title`` keeps the text before the first delimiter, which
    suits ``<book> | <site>``. A publisher that prints ``<site> | <book>``
    (``Torrey House Press | ANIMALS TAUGHT ME EVERYTHING by Pam Houston``)
    would keep only the site name, so a leading site label is removed first.
    A title whose only segment is the site name is returned unchanged.
    """
    value = " ".join(unescape(title or "").split())
    changed = True
    while changed:
        changed = False
        for delimiter in (" | ", " - ", " – ", " — ", " :: "):
            head, separator, rest = value.partition(delimiter)
            if separator and rest.strip() and _title_is_site_label(head, hostname=hostname, site_names=site_names):
                value = rest.strip()
                changed = True
                break
    return value


def _title_is_site_label(title: str, *, hostname: str, site_names: Sequence[str] = ()) -> bool:
    """Report a page title that names only the site, never the work.

    A retailer bot check answers with the site name as its whole title
    ("Amazon.com"). The comparison uses the same normalization as author
    site metadata, so a leading "www." and punctuation do not hide the match.
    """
    value = title.strip()
    if not value:
        return True
    ignored = {"www", "com", "org", "net", "and", "the"}
    candidate = "".join(part for part in normalize_author_identity(value).split() if part not in ignored)
    if not candidate:
        return True
    for label in (hostname, *site_names):
        parts = [part for part in normalize_author_identity(label).split() if part not in ignored]
        # A regional storefront answers on a prefixed host ("us.amazon.com")
        # and still titles its bot check "Amazon.com", so every trailing run
        # of host labels counts as the site name, not the whole host alone.
        for start in range(len(parts)):
            if "".join(parts[start:]) == candidate:
                return True
    return False


# Author pages as shops and catalogues link them: ``/autor/alcione-albanesi``
# on Livraria Loyola, a ``q=inauthor:"Celeste Birkhofer"`` search on Google
# Books.
_AUTHOR_LINK_PATH = re.compile(r"(?:^|/)(?:authors?|autor(?:es|a|in)?|auteurs?|autor[ei])(?:/|$)")


def _is_author_link(href: str) -> bool:
    """True when ``href`` points at an author page or an author search."""
    value = unquote(str(href or "")).casefold()
    if "inauthor:" in value:
        return True
    return bool(_AUTHOR_LINK_PATH.search(urlsplit(value).path))


def page_prints_author(author: str, page_text: str) -> bool:
    """True when ``page_text`` contains ``author`` as whole words, ignoring case and punctuation."""
    name = _normalized_token_text(str(author or ""))
    text = _normalized_token_text(str(page_text or ""))
    return bool(name) and bool(text) and f" {name} " in f" {text} "


def page_prints_author_surname(author: str, page_text: str) -> bool:
    """True when ``page_text`` prints the surname of every person ``author`` names.

    The surname is the last word of the normalized display name, matched as
    whole words, so ``Pétré-Grenouilleau, Olivier`` is found under either
    name order (issue #791).
    """
    surnames = [
        display.split()[-1]
        for component in _author_components(author)
        if (display := normalize_author_display(component))
    ]
    return bool(surnames) and all(page_prints_author(surname, page_text) for surname in surnames)


_ISBN_PRINTED_SEPARATOR = re.compile(r"(?<=[\dXx])[-\u2010-\u2015](?=[\dXx])")


def _isbn_check_digit_10(body: str) -> str:
    remainder = sum((10 - index) * int(digit) for index, digit in enumerate(body)) % 11
    return "0123456789X"[(11 - remainder) % 11]


def _isbn_forms(isbn: str) -> frozenset[str]:
    """Return the ISBN-10 and ISBN-13 spellings of one plausible ISBN."""
    forms = {isbn}
    if len(isbn) == 13 and isbn.startswith("978"):
        forms.add(isbn[3:12] + _isbn_check_digit_10(isbn[3:12]))
    elif len(isbn) == 10:
        body = "978" + isbn[:9]
        total = sum(int(digit) * (3 if index % 2 else 1) for index, digit in enumerate(body))
        forms.add(body + str((10 - total % 10) % 10))
    return frozenset(forms)


def _checked_isbn13(raw: str) -> str:
    """Return the ISBN-13 form of a plausible ISBN with a valid check digit, else ``''``."""
    isbn = plausible_isbn(str(raw or "")).upper()
    if len(isbn) == 10:
        if _isbn_check_digit_10(isbn[:9]) != isbn[-1]:
            return ""
        return next(form for form in _isbn_forms(isbn) if len(form) == 13)
    if len(isbn) == 13 and isbn.isdigit():
        if sum(int(digit) * (3 if index % 2 else 1) for index, digit in enumerate(isbn)) % 10 == 0:
            return isbn
    return ""


# Path segments that introduce a search or listing, whose ISBN is the query
# rather than the page's own book.
_URL_ISBN_LISTING_SEGMENTS = frozenset(
    {"s", "search", "find", "catalogsearch", "busca", "buscar", "suche", "recherche"}
)


def _url_isbn(url: str) -> str:
    """Read one ISBN from a fetched product path, never a query or discovery snippet.

    ISBN-13 paths include standalone numbers and slug suffixes. ISBN-10
    product IDs must pass their checksum: a generic Amazon ASIN is not an
    ISBN. Multiple different path ISBNs provide no unambiguous proof.
    """
    path = unquote(urlsplit(url).path)
    if any(segment.casefold() in _URL_ISBN_LISTING_SEGMENTS for segment in path.split("/")):
        return ""
    # A 13-digit path segment counts only with a valid ISBN-13 check digit,
    # so a numeric product ID that happens to start with 978 is not an ISBN.
    values = re.findall(r"(?<![0-9])97[89][0-9]{10}(?![0-9])", path)
    values += re.findall(r"/(?:dp|gp/product)/([0-9]{9}[0-9X])(?=/|$)", path, re.IGNORECASE)
    normalized = {_checked_isbn13(value) for value in values} - {""}
    return next(iter(normalized)) if len(normalized) == 1 else ""


def page_prints_isbn(isbn: str, page_text: str) -> bool:
    """True when ``page_text`` prints ``isbn``, hyphenated or not, in either ISBN length.

    The number must stand alone: a longer digit run that merely contains it
    is another number (issue #791).
    """
    plausible = plausible_isbn(isbn).upper()
    if not plausible:
        return False
    text = _ISBN_PRINTED_SEPARATOR.sub("", str(page_text or "")).upper()
    return any(re.search(rf"(?<![\dX]){re.escape(form)}(?![\dX])", text) for form in _isbn_forms(plausible))


def _without_country_redirect(url: str) -> str:
    """Ask Google Books not to redirect to the fetching machine's country domain.

    ``books.google.com`` answers with a redirect to ``books.google.<cc>``,
    which the same-host redirect rule refuses, so every Google Books page
    failed to fetch from outside the United States (issue #709).
    ``redir_esc=y`` is Google's own parameter for serving the page in place.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").casefold()
    if not re.fullmatch(r"books\.google\.[a-z.]+", host):
        return url
    query = parse_qsl(parts.query, keep_blank_values=True)
    if any(key == "redir_esc" for key, _value in query):
        return url
    return urlunsplit(parts._replace(query=urlencode([*query, ("redir_esc", "y")])))


# E-commerce platforms that stamp their own name into ``<meta name="author">``
# on every shop page they host. Livraria Loyola runs on F-Store, and the
# platform name beat the author its product page prints (issue #709). Names
# are compared with spaces and punctuation removed.
_SHOP_PLATFORM_NAMES = frozenset(
    {
        "bigcommerce",
        "fbits",
        "fstore",
        "lojaintegrada",
        "magento",
        "nuvemshop",
        "prestashop",
        "shopify",
        "vtex",
        "woocommerce",
    }
)


def _author_is_site_metadata(author: str, *, owner_id: str, hostname: str, site_names: Sequence[str] = ()) -> bool:
    value = author.strip()
    parsed = urlsplit(value)
    bare_url = urlsplit(f"//{value}") if " " not in value else parsed
    if (
        parsed.scheme
        or parsed.netloc
        or (bare_url.netloc and "." in bare_url.netloc)
        or value.casefold().startswith(("www.", "//"))
    ):
        return True
    normalized = normalize_author_identity(value)
    tokens = set(normalized.split())
    generic = {
        "site",
        "website",
        "staff",
        "editorial",
        "team",
        "contributors",
        "foundation",
        "organization",
        "publisher",
        "retailer",
        "bookstore",
    }
    if tokens & generic:
        return True
    if "".join(normalized.split()) in _SHOP_PLATFORM_NAMES:
        return True
    ignored_identity_tokens = {"and", "the", "www", "com", "org", "net"}
    context_values = [owner_id, hostname, *site_names]
    context_tokens: set[str] = set()
    context_compact: set[str] = set()
    for context in context_values:
        context_normalized = normalize_author_identity(context)
        context_parts = context_normalized.split()
        context_tokens.update(context_parts)
        context_compact.add("".join(context_parts))
        context_compact.add("".join(part for part in context_parts if part not in ignored_identity_tokens))
    tokens.difference_update(ignored_identity_tokens)
    candidate_compact = "".join(part for part in normalized.split() if part not in ignored_identity_tokens)
    if candidate_compact and candidate_compact in context_compact:
        return True
    context_tokens.difference_update(ignored_identity_tokens)
    return bool(tokens) and len(tokens) >= 2 and tokens <= context_tokens


def _og_repeats_first_body_paragraph(og_description: str, paragraphs: Sequence[str]) -> bool:
    """Prefer fuller body evidence when og:description only repeats its start."""
    normalized_og = " ".join(og_description.casefold().split())
    if not normalized_og:
        return False
    first_paragraph = next((paragraph for paragraph in paragraphs if paragraph.strip()), "")
    normalized_first = " ".join(first_paragraph.casefold().split())
    return normalized_first == normalized_og or (
        len(normalized_first) > len(normalized_og) and normalized_first.startswith(normalized_og)
    )


class HostRateLimiter:
    """Space requests to one host by a minimum interval.

    Shared by the three stage workers, so the lock is what makes the spacing
    real. The clock and the sleeper are injected so a test can prove the
    spacing without waiting, the way the discovery adapter's limiter does.
    """

    def __init__(
        self,
        min_interval_seconds: float = MIN_HOST_INTERVAL_SECONDS,
        *,
        monotonic: Any = time.monotonic,
        sleeper: Any = time.sleep,
    ) -> None:
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._next_allowed: dict[str, float] = {}

    def wait(self, host: str, *, request_budget: CandidateRequestBudget | None = None) -> float:
        """Wait for host spacing without crossing a candidate deadline."""
        key = (host or "").casefold()
        if not self.min_interval_seconds:
            if request_budget is not None:
                request_budget.before_rate_limit_wait(0.0)
            return 0.0
        with self._lock:
            now = float(self._monotonic())
            allowed = self._next_allowed.get(key, now)
            delay = max(0.0, allowed - now)
            if request_budget is not None:
                request_budget.before_rate_limit_wait(delay)
            self._next_allowed[key] = max(now, allowed) + self.min_interval_seconds
        if delay > 0.0:
            self._sleeper(delay)
        if request_budget is not None and request_budget.remaining_seconds() <= 0.0:
            raise BoundedHTTPError(
                "candidate deadline expired",
                reason_code=REASON_CANDIDATE_BUDGET_EXHAUSTED,
            )
        return delay


class BoundedHTTPFetcher:
    """Fetch and parse one classified page with bounded standard-library HTTP."""

    def __init__(
        self,
        *,
        transport: HTTPTransport | None = None,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        timeout_seconds: float = REQUEST_INACTIVITY_TIMEOUT_SECONDS,
        max_redirects: int = 3,
        min_host_interval_seconds: float = MIN_HOST_INTERVAL_SECONDS,
        challenge_attempts: int = PAGE_CHALLENGE_ATTEMPTS,
        monotonic: Any = time.monotonic,
        sleeper: Any = time.sleep,
    ) -> None:
        self.transport = transport or StandardLibraryHTTPTransport(max_redirects=max_redirects)
        self.max_response_bytes = max_response_bytes
        self.timeout_seconds = timeout_seconds
        self.max_redirects = min(max(0, max_redirects), 3)
        self.challenge_attempts = max(1, int(challenge_attempts))
        self._host_limiter = HostRateLimiter(
            min_host_interval_seconds,
            monotonic=monotonic,
            sleeper=sleeper,
        )

    def fetch(self, result: Mapping[str, Any] | DiscoveryResult, **kwargs: Any) -> FetchedSource:
        """Fetch one page, retrying a transient challenge response.

        A retailer answers a burst of requests from one client with a bot
        check: status 200, a site-name title, and no book evidence at all.
        Returning that page as a source spends one of the candidate's few
        fetch slots and can only end as ``insufficient_subject_text``, so
        the challenge is retried instead (issue #563).

        Pass ``allow_unclassified_host=True`` to re-fetch a page the
        classifier left unknown. Under the owner policy every host that is
        not denylisted is classified, so the opt-in only matters for a
        classifier built without a policy. Only the adjudication stage may ask for
        it, in its two re-fetches of a cited page: citation verification
        (issue #636) and the evidence-unchanged comparison that re-fetches
        the same retained pages (issue #639). See :meth:`_fetch_once`.
        """
        last_error: BoundedHTTPError | None = None
        for _ in range(self.challenge_attempts):
            try:
                return self._fetch_once(result, **kwargs)
            except BoundedHTTPError as error:
                if getattr(error, "reason_code", "") != REASON_PAGE_CHALLENGE_NO_EVIDENCE:
                    raise
                last_error = error
        assert last_error is not None
        raise last_error

    def _fetch_once(self, result: Mapping[str, Any] | DiscoveryResult, **kwargs: Any) -> FetchedSource:
        discovery = DiscoveryResult.from_mapping(result)
        url = canonicalize_url(discovery.url)
        # Only classified pages are fetched by default. One stage opts out by
        # name: adjudication re-fetches a page that an agent cited (issue
        # #632); under the owner policy such a page is classified anyway
        # unless its classifier has no policy (#1123). It re-fetches in two places, citation verification and the
        # evidence-unchanged comparison, and both re-read the same cited
        # page. The opt-out is an explicit named argument, so an audit of
        # who may fetch an unnamed host reads the call sites of this one
        # keyword instead of trusting a caller-supplied field.
        classified = discovery.classification.casefold() == "classified"
        if kwargs.get("allow_unclassified_host") is True:
            classified = True
        if not url or discovery.is_search_snippet or not classified:
            raise BoundedHTTPError("only classified page URLs may be fetched")
        hostname = urlsplit(url).hostname
        max_response_bytes = max(
            0,
            min(int(kwargs.get("max_bytes", self.max_response_bytes)), self.max_response_bytes),
        )
        request_budget = kwargs.get("request_budget")
        inactivity_timeout = float(kwargs.get("timeout_seconds", self.timeout_seconds))
        if request_budget is not None:
            request_budget.ensure_request_available("fetch")
        self._host_limiter.wait(hostname or "", request_budget=request_budget)
        timeout = inactivity_timeout
        if request_budget is not None:
            timeout = request_budget.before_request("fetch", inactivity_timeout)
        deadline_kwargs: dict[str, Any] = (
            {"remaining_seconds": request_budget.remaining_seconds} if request_budget is not None else {}
        )
        response = self.transport.request(
            _without_country_redirect(url),
            timeout_seconds=timeout,
            max_response_bytes=max_response_bytes,
            max_redirects=self.max_redirects,
            allowed_hosts={hostname.casefold()} if hostname else set(),
            **deadline_kwargs,
        )
        if not 200 <= response.status < 300:
            raise BoundedHTTPError("page response was not successful")
        content_type = response.headers.get("content-type", "").casefold()
        if content_type and not any(
            value in content_type for value in ("text/html", "application/xhtml", "text/plain")
        ):
            raise BoundedHTTPError("page content type is not readable evidence")
        charset = "utf-8"
        if "charset=" in content_type:
            charset = content_type.split("charset=", 1)[1].split(";", 1)[0].strip() or charset
        raw_body = response.body
        body = raw_body[:max_response_bytes]
        if not isinstance(body, bytes):
            body = bytes(body)
        content_length = _header_content_length(response.headers)
        truncated = bool(response.truncated) or len(raw_body) > max_response_bytes
        if content_length is not None and content_length > max_response_bytes:
            truncated = True
        try:
            text = body.decode(charset, errors="replace")
        except LookupError:
            text = body.decode("utf-8", errors="replace")
        parser = _PageEvidenceParser()
        try:
            parser.feed(text)
        except Exception:
            # HTMLParser is intentionally used as a fail-safe partial parser.
            # Keep evidence collected before malformed input was encountered.
            pass
        parser.finish()
        book_record = _select_book_record(parser.book_records())
        structured_title = _clean_page_title(str(book_record.get("name", "") or ""))
        if not structured_title and parser.book_title_parts:
            structured_title = _clean_page_title(parser.book_title_parts[0])
        structured_authors = _jsonld_authors(book_record)
        structured_subject = _structured_book_subject(book_record)
        # Issue #531: read the ISBN from the same selected Book record that
        # already supplies the structured title, authors, and subject. The
        # page was already fetched and parsed, so extraction adds no request.
        structured_isbn = _structured_book_isbn(book_record)

        hostname = (urlsplit(url).hostname or "").casefold()
        raw_page_title = " ".join(parser.og_title_parts or parser.title_parts)
        if not raw_page_title:
            raw_page_title = " ".join(parser.title_parts)
        raw_page_title = _drop_leading_site_label(raw_page_title, hostname=hostname, site_names=parser.site_name_parts)
        page_title = _clean_page_title(raw_page_title)

        if structured_title:
            # Schema.org titles are publisher-provided and never carry retailer
            # listing chrome, so the chrome guard runs only on the page title.
            selected_title = structured_title
        else:
            selected_title = _strip_amazon_listing_chrome(raw_page_title)
            if selected_title == raw_page_title:
                # No chrome on the raw title: retry the cleaned form so a trailing
                # " - site" / " | site" / " :: site" suffix after a chrome shape
                # cannot hide it. Stripping before page-title cleaning also keeps a
                # delimiter inside the book-title segment from truncating the
                # ISBN/author/category tail away from the matcher.
                selected_title = _strip_amazon_listing_chrome(page_title)
            selected_title = _clean_page_title(selected_title)
        title, headline_author = _title_and_headline_author(selected_title)
        # A schema.org name is the publisher's own title and must be stored whole;
        # only a page title carries a headline "by <author>" split. The split still
        # runs above so headline_author keeps its existing behavior.
        stored_title = selected_title if structured_title else title
        title = _bounded(_clean_page_title(stored_title), MAX_EVIDENCE_METADATA_TEXT)
        headline_author = _bounded(headline_author, 200)

        # Subject evidence is deliberately ordered: bounded schema.org/Book
        # JSON-LD, detail-page subject containers, usable og:description, then
        # paragraphs in main/article. A plain meta description remains a
        # compatibility fallback only.
        detail_subject = _usable_subject_text(" ".join(parser.detail_subject_parts))
        og_subject = _usable_subject_text(" ".join(parser.og_description_parts))
        if _og_repeats_first_body_paragraph(og_subject, parser.paragraph_parts):
            og_subject = ""
        paragraph_subject = _usable_subject_text(" ".join(parser.paragraph_parts))
        meta_subject = _usable_subject_text(" ".join(parser.description_parts))
        raw_subject = structured_subject or detail_subject or og_subject or paragraph_subject or meta_subject
        subject_text = _bounded(raw_subject, MAX_EVIDENCE_METADATA_TEXT)
        display_text = _bounded_subject(raw_subject)

        raw_authors = structured_authors or parser.book_author_parts or ([headline_author] if headline_author else [])
        if not raw_authors:
            raw_authors = parser.author_parts
        authors = tuple(
            _bounded(author, 200)
            for author in raw_authors[:10]
            if author.strip()
            and not _author_is_site_metadata(
                author,
                owner_id=discovery.owner_id,
                hostname=hostname,
                site_names=parser.site_name_parts,
            )
        )
        if truncated and not title and not authors and not subject_text:
            raise BoundedHTTPError(
                "truncated page contained no usable book evidence",
                reason_code=REASON_TRUNCATED_PAGE_NO_EVIDENCE,
            )
        if (
            not book_record
            and not authors
            and not subject_text
            and _title_is_site_label(title, hostname=hostname, site_names=parser.site_name_parts)
        ):
            # No schema.org record, no author, no subject text, and a title
            # that only names the site. A book page never has this shape; a
            # bot check always does. A page that at least names the work is
            # left alone, because a title is still identity evidence.
            raise BoundedHTTPError(
                "page carried no book evidence",
                reason_code=REASON_PAGE_CHALLENGE_NO_EVIDENCE,
            )
        metadata = discovery.metadata
        title_evidence = discovery.title_evidence
        if title_evidence is None and isinstance(metadata, Mapping):
            metadata_evidence = metadata.get("title_evidence")
            title_evidence = metadata_evidence if isinstance(metadata_evidence, Mapping) else None
        return FetchedSource(
            origin=discovery.origin,
            signal_own=discovery.signal_own,
            url=url,
            source_kind=discovery.source_kind,
            source_family=discovery.source_family,
            owner_id=discovery.owner_id,
            source_language=discovery.source_language,
            title=title,
            primary_authors=authors,
            subject_text=subject_text,
            subject_key=subject_text.replace(":", " "),
            checked_date=str(
                kwargs.get("checked_date")
                or (metadata.get("checked_date", "") if isinstance(metadata, Mapping) else "")
            ),
            title_evidence_route=discovery.title_evidence_route,
            title_evidence=_bounded_title_evidence(title_evidence),
            classification=discovery.classification,
            display_text=display_text,
            isbn=structured_isbn,
            # Only a product page's own address names its book; a search or
            # listing route's address carries the query instead.
            url_isbn=""
            if discovery.route_kind.casefold() in _GENERIC_DISCOVERY_ROUTE_KINDS
            else _url_isbn(response.url),
            source_preference=_SOURCE_PREFERENCE.get(discovery.source_kind.casefold(), 99),
            author_evidence_text=" ".join(parser.author_link_parts),
            identity_evidence_text=" ".join(parser.identity_text_parts),
            states_book_author=bool(structured_authors or parser.book_author_parts or headline_author),
            classification_evidence=_classification_evidence_from_mapping(
                discovery.classification_evidence or discovery.metadata,
                url=url,
                source_kind=discovery.source_kind,
                source_family=discovery.source_family,
                owner_id=discovery.owner_id,
                source_language=discovery.source_language,
                classification=discovery.classification,
            ),
        )


# Stable descriptive aliases for adapter discovery.
StandardLibraryHTTPFetcher = BoundedHTTPFetcher
HTTPDiscovery = HTTPDiscoveryProvider


GROUNDING_STAGE_ARTIFACT_NAME = "grounded-description-stage.json"
GROUNDING_QUEUE_FILENAME = "grounded-description-queue.json"


class GroundedDescriptionStageService:
    """Shared entry seam for weekly and standalone grounded descriptions.

    Weekly enrichment owns the canonical application boundary. The stage still
    returns only bounded accepted evidence and validated text, while this
    service applies those results to Candidate History and the current report.
    """

    def __init__(
        self,
        stage: GroundedDescriptionStage | None = None,
        *,
        enabled: bool = True,
        disabled_reason: str = "",
    ) -> None:
        self.stage = stage or GroundedDescriptionStage()
        self.enabled = enabled
        self.disabled_reason = disabled_reason
        self._snapshot_progress_presentation: ProgressSnapshotPresentation | None = None
        self._snapshot_progress_failure_recorder: ProgressSnapshotFailureRecorder | None = None

    def set_snapshot_progress_presentation(self, presentation: ProgressSnapshotPresentation | None) -> None:
        """Bind the optional per-candidate snapshot heartbeat (issue #706)."""
        self._snapshot_progress_presentation = presentation

    def set_progress_snapshot_failure_recorder(self, recorder: ProgressSnapshotFailureRecorder | None) -> None:
        """Bind the optional snapshot-failure recorder (issue #706)."""
        self._snapshot_progress_failure_recorder = recorder

    def _snapshot_tracker(self, output_dir: Path) -> CandidateProgressSnapshot | None:
        """Build the per-candidate heartbeat tracker when anyone opted in.

        The tracker publishes the snapshot file even when the presentation
        is a no-op: the doctor reads the file. With neither seam bound the
        stage stays silent, exactly as before. Duck-typed test doubles
        without stage attributes get safe defaults instead of an error.
        """
        if self._snapshot_progress_presentation is None and self._snapshot_progress_failure_recorder is None:
            return None
        budget = getattr(self.stage, "stage_budget_seconds", None)
        monotonic = getattr(self.stage, "monotonic", None) or time.monotonic
        try:
            budget_seconds = float(budget) if budget is not None else None
        except (TypeError, ValueError, OverflowError):
            budget_seconds = None
        return CandidateProgressSnapshot(
            output_dir,
            stage="enrichment",
            stage_budget_seconds=budget_seconds,
            monotonic=monotonic,
            failure_recorder=self._snapshot_progress_failure_recorder,
            presentation=self._snapshot_progress_presentation,
        )

    def preflight(self, **kwargs: Any) -> PreflightResult:
        """Run the one configured discovery preflight without candidate data."""
        if not self.enabled:
            return PreflightResult(
                False,
                PREFLIGHT_ENDPOINT_NOT_CONFIGURED,
                "discovery endpoint is not configured",
            )
        method = getattr(self.stage.discovery, "preflight", None)
        if not callable(method):
            return PreflightResult(False, PREFLIGHT_ADAPTER_UNAVAILABLE, "discovery adapter is unavailable")
        try:
            answer = method(**kwargs)
        except Exception:
            return PreflightResult(False, PREFLIGHT_ADAPTER_NO_RESPONSE, "discovery adapter did not answer preflight")
        if isinstance(answer, PreflightResult):
            return answer
        if isinstance(answer, tuple) and len(answer) == 2:
            ok, reason = answer
            reason_code = str(reason or (PREFLIGHT_HEALTHY if ok else PREFLIGHT_ADAPTER_NO_RESPONSE))
            message = "discovery adapter is healthy" if ok else "discovery adapter did not answer preflight"
            return PreflightResult(bool(ok), reason_code, message)
        if answer is False or answer is None:
            return PreflightResult(False, PREFLIGHT_ADAPTER_NO_RESPONSE, "discovery adapter did not answer preflight")
        if isinstance(answer, Mapping) and answer.get("status") in {"failed", "error", "unavailable"}:
            return PreflightResult(False, PREFLIGHT_ADAPTER_NO_RESPONSE, "discovery adapter did not answer preflight")
        return PreflightResult(True, PREFLIGHT_HEALTHY, "discovery adapter is healthy")

    def run(
        self,
        candidates: Sequence[Mapping[str, Any]],
        *,
        operation_id: str,
        operation_date: str,
        history: Sequence[Mapping[str, Any]] = (),
        aliases: Mapping[str, Any] | None = None,
        queue: GroundedDescriptionQueue | None = None,
        existing_candidate_ids: set[str] | None = None,
        force_candidates: bool = True,
        eligible_candidate_ids: set[str] | None = None,
        snapshot_tracker: CandidateProgressSnapshot | None = None,
    ) -> StageArtifact:
        """Run the existing stage seam for a selected worklist."""
        return self.stage.run(
            candidates,
            operation_id=operation_id,
            operation_date=operation_date,
            queue=queue,
            existing_candidate_ids=existing_candidate_ids if existing_candidate_ids is not None else set(),
            history=history,
            aliases=aliases,
            force_candidates=force_candidates,
            eligible_candidate_ids=eligible_candidate_ids,
            snapshot_tracker=snapshot_tracker,
        )

    def enrich(
        self,
        output_dir: Path,
        state_dir: Path | None = None,
        effective_date: str | None = None,
        *,
        operation_id: str | None = None,
        state_store: StateStore | None = None,
    ) -> dict[str, Any]:
        from autoscout.grounded_description_enrichment import enrich_grounded_descriptions

        return enrich_grounded_descriptions(
            self.stage,
            output_dir,
            state_dir,
            effective_date,
            operation_id=operation_id,
            enabled=self.enabled,
            disabled_reason=self.disabled_reason,
            snapshot_tracker=self._snapshot_tracker(output_dir),
            state_store=state_store,
        )


def schedule_candidates(
    candidates: Sequence[Mapping[str, Any]],
    queue: GroundedDescriptionQueue,
    *,
    operation_id: str,
    operation_date: str,
    existing_candidate_ids: set[str] | None = None,
) -> list[tuple[Mapping[str, Any], QueueEntry]]:
    return queue.worklist(
        candidates,
        operation_id=operation_id,
        operation_date=operation_date,
        existing_candidate_ids=existing_candidate_ids,
    )


def write_stage_artifact(path: Path, artifact: StageArtifact) -> None:
    write_json(path, artifact.to_dict())


def read_stage_artifact(path: Path) -> StageArtifact | None:
    value = read_json_or_default(path, None)
    return StageArtifact.from_mapping(value) if isinstance(value, Mapping) else None


def _bounded_structure_valid(value: Any, *, depth: int = 0) -> bool:
    if depth > MAX_TITLE_EVIDENCE_DEPTH:
        return False
    if isinstance(value, Mapping):
        if len(value) > MAX_METADATA_KEYS:
            return False
        return all(
            isinstance(key, str)
            and len(key) <= MAX_METADATA_KEY_LENGTH
            and _bounded_structure_valid(item, depth=depth + 1)
            for key, item in value.items()
        )
    if isinstance(value, list | tuple):
        return len(value) <= 10 and all(_bounded_structure_valid(item, depth=depth + 1) for item in value)
    if isinstance(value, str):
        return len(value) <= MAX_METADATA_VALUE_LENGTH
    return isinstance(value, bool | int | float) or value is None


def _validate_raw_source_record(source: Mapping[str, Any]) -> bool:
    classification = source.get("classification_evidence")
    classification = classification if isinstance(classification, Mapping) else source
    for key, limit in (
        ("host", MAX_METADATA_VALUE_LENGTH),
        ("path_rule", 200),
        ("policy_metadata_version", 100),
        ("classification", 50),
    ):
        if key in classification and (not isinstance(classification[key], str) or len(classification[key]) > limit):
            return False
    metadata = classification.get("source_metadata")
    if metadata is not None and not _bounded_structure_valid(metadata):
        return False
    title_evidence = source.get("title_evidence")
    return title_evidence is None or (isinstance(title_evidence, Mapping) and _bounded_structure_valid(title_evidence))


def _validate_classification_evidence(source: AcceptedSource) -> bool:
    evidence = _classification_evidence_from_mapping(
        source.classification_evidence,
        url=source.canonical_url,
        source_kind=source.source_kind,
        source_family=source.source_family,
        owner_id=source.owner_id,
        source_language=source.source_language,
        classification="classified",
    )
    if set(evidence) != {
        "host",
        "path_rule",
        "policy_metadata_version",
        "classification",
        "source_metadata",
    }:
        return False
    if evidence["classification"] != "classified":
        return False
    if any(
        not isinstance(evidence.get(key), str) or len(evidence[key]) > limit
        for key, limit in (
            ("host", MAX_METADATA_VALUE_LENGTH),
            ("path_rule", 200),
            ("policy_metadata_version", 100),
            ("classification", 50),
        )
    ):
        return False
    expected_host = canonicalize_hostname(urlsplit(source.canonical_url).hostname or "")
    if evidence["host"] and evidence["host"] != expected_host:
        return False
    metadata = evidence.get("source_metadata")
    if not isinstance(metadata, Mapping) or len(metadata) > MAX_METADATA_KEYS:
        return False
    return all(
        isinstance(key, str)
        and len(key) <= MAX_METADATA_KEY_LENGTH
        and isinstance(item, str | bool | int | float | dict | list)
        for key, item in metadata.items()
    )


def _validate_title_evidence(source: AcceptedSource) -> bool:
    if source.title_evidence_route != "verified_published_title":
        return source.title_evidence is None or isinstance(source.title_evidence, Mapping)
    return isinstance(source.title_evidence, Mapping) and title_is_identity_evidence(dict(source.title_evidence))


def _validate_independence_evidence(
    evidence: SourceIndependenceEvidence | None,
    sources: Sequence[AcceptedSource],
) -> bool:
    expected = SourceIndependenceEvidence.from_sources(sources)
    if evidence is None:
        evidence = expected
    return (
        evidence.reason == SOURCE_INDEPENDENCE_REASON
        and evidence.canonical_urls == expected.canonical_urls
        and evidence.owner_ids == expected.owner_ids
        and evidence.source_families == expected.source_families
        and all(canonicalize_url(url) == url for url in evidence.canonical_urls)
        and all(str(owner_id).strip() for owner_id in evidence.owner_ids)
        and len(set(evidence.canonical_urls)) == len(sources)
        and len(set(evidence.owner_ids)) == len(sources)
    )


def _count_mapping_matches(raw: Any, expected: Mapping[str, int], *, require_all: bool = False) -> bool:
    if not isinstance(raw, Mapping):
        return False
    if require_all:
        raw_keys = {str(key) for key in raw}
        expected_keys = set(expected)
        if raw_keys != expected_keys:
            # Allow missing keys when their expected count is zero, for
            # backwards compatibility after adding OUTCOME_EVIDENCE_UNCHANGED.
            if not raw_keys.issubset(expected_keys):
                return False
            for missing in expected_keys - raw_keys:
                if expected.get(missing, 0) != 0:
                    return False
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in raw.values()):
        return False
    normalized = {str(key): raw[key] for key in raw}
    # For lenient require_all, compare only the present keys.
    if require_all and set(normalized) != set(expected):
        for key in expected:
            if key not in normalized and expected[key] == 0:
                continue
            if normalized.get(key) != expected[key]:
                return False
        return True
    return normalized == dict(expected)


def validate_stage_artifact(value: StageArtifact | Mapping[str, Any]) -> bool:
    """Validate the bounded persisted shape before a renderer consumes it."""
    if isinstance(value, Mapping):
        raw_outcomes = value.get("outcomes", [])
        if not isinstance(raw_outcomes, list):
            return False
        for raw in raw_outcomes:
            if not isinstance(raw, Mapping):
                continue
            if not CandidateTiming.mapping_is_valid(raw.get("candidate_timing")):
                return False
            if "likely_author_typos" in raw:
                typos = raw["likely_author_typos"]
                if not isinstance(typos, list) or len(typos) > MAX_FETCHES_PER_CANDIDATE:
                    return False
                if any(not AuthorTypoEvidence.mapping_is_valid(item) for item in typos):
                    return False
            if raw.get("outcome") not in {OUTCOME_GROUNDED, OUTCOME_SINGLE_SOURCE}:
                continue
            # Depth acceptance at the artifact boundary (issue #673): no
            # replay, service, or application path may persist a below-floor
            # success as grounded, however the outcome was produced.
            if not meets_description_depth(raw.get("content_description", "")):
                return False
            raw_sources = raw.get("research_source_records", [])
            if not isinstance(raw_sources, list) or not raw_sources:
                raw_sources = raw.get("research_sources", [])
            expected_lengths = {1} if raw.get("outcome") == OUTCOME_SINGLE_SOURCE else {2, 3}
            if not isinstance(raw_sources, list) or len(raw_sources) not in expected_lengths:
                return False
            raw_urls = [
                str(source).strip() if isinstance(source, str) else str(source.get("url", "")).strip()
                for source in raw_sources
                if isinstance(source, str) or isinstance(source, Mapping)
            ]
            if len(raw_urls) != len(raw_sources) or any(canonicalize_url(url) != url for url in raw_urls):
                return False
            declared_urls = raw.get("research_sources")
            if declared_urls is not None:
                if not isinstance(declared_urls, list) or any(
                    not isinstance(source, str | Mapping) for source in declared_urls
                ):
                    return False
                declared_urls = [
                    str(source).strip() if isinstance(source, str) else str(source.get("url", "")).strip()
                    for source in declared_urls
                ]
                if declared_urls != raw_urls:
                    return False
            if any(isinstance(source, Mapping) and not _validate_raw_source_record(source) for source in raw_sources):
                return False
            provenance = raw.get("description_provenance")
            if provenance is not None and (
                not isinstance(provenance, Mapping) or provenance.get("evidence_basis") != raw_urls
            ):
                return False
            raw_independence = raw.get("source_independence_evidence")
            if raw_independence is not None and not isinstance(raw_independence, Mapping):
                return False
    artifact = value if isinstance(value, StageArtifact) else StageArtifact.from_mapping(value)
    if not artifact.operation_id or not artifact.operation_date:
        return False
    if isinstance(value, Mapping):
        raw_counts = value.get("counts")
        if raw_counts is not None and not _count_mapping_matches(raw_counts, artifact.counts, require_all=True):
            return False
        raw_reason_counts = value.get("reason_counts")
        if raw_reason_counts is not None and (
            not isinstance(raw_reason_counts, Mapping)
            or any(not str(key).strip() for key in raw_reason_counts)
            or not _count_mapping_matches(raw_reason_counts, artifact.reason_counts)
        ):
            return False
    for outcome in artifact.outcomes:
        if not outcome.candidate_timing.is_valid():
            return False
        if len(outcome.likely_author_typos) > MAX_FETCHES_PER_CANDIDATE or any(
            not AuthorTypoEvidence.mapping_is_valid(item.to_dict()) for item in outcome.likely_author_typos
        ):
            return False
        if outcome.outcome not in OUTCOME_CLASSES or outcome.reason_code not in REASON_CODES | {""}:
            return False
        if any(item.reason_code not in REASON_CODES for item in outcome.rejected_source_reasons):
            return False
        if outcome.outcome == OUTCOME_NOT_ATTEMPTED:
            if outcome.reason_code != REASON_NOT_ATTEMPTED_STAGE_BUDGET or not outcome.scheduling:
                return False
            continue
        if outcome.outcome == OUTCOME_EVIDENCE_UNCHANGED:
            if outcome.reason_code != REASON_EVIDENCE_UNCHANGED or not outcome.identity_key or not outcome.scheduling:
                return False
            if outcome.content_description or outcome.research_sources:
                return False
            continue
        if outcome.outcome == OUTCOME_ATTEMPTED_NO_DESCRIPTION and not outcome.reason_code:
            return False
        if not outcome.identity_key:
            return False
        if outcome.outcome in {OUTCOME_GROUNDED, OUTCOME_SINGLE_SOURCE}:
            expected_lengths = {1} if outcome.outcome == OUTCOME_SINGLE_SOURCE else {2, 3}
            if len(outcome.research_sources) not in expected_lengths or not outcome.content_description:
                return False
            # Depth acceptance at the artifact boundary (issue #673): no
            # replay, service, or application path may persist a below-floor
            # success as grounded, however the outcome was produced.
            if not meets_description_depth(outcome.content_description):
                return False
            urls = [source.canonical_url for source in outcome.research_sources]
            if len(set(urls)) != len(urls) or any(canonicalize_url(url) != url for url in urls):
                return False
            if len(urls) >= 2 and any(source.signal_own for source in outcome.research_sources):
                return False
            if any(not str(source.owner_id).strip() for source in outcome.research_sources):
                return False
            if len({source.owner_id for source in outcome.research_sources}) != len(urls):
                return False
            if any(not _validate_classification_evidence(source) for source in outcome.research_sources):
                return False
            if any(not _validate_title_evidence(source) for source in outcome.research_sources):
                return False
            expected = [
                source.canonical_url
                for source in sorted(
                    outcome.research_sources,
                    key=lambda source: _discovery_ordering_key(
                        source.relevance_tier,
                        source.source_kind,
                        source.discovery_ordinal,
                        source.canonical_url,
                        source.origin,
                    ),
                )
            ]
            if urls != expected:
                return False
            if accepted_evidence_urls(outcome) != urls:
                return False
            if not _validate_independence_evidence(outcome.source_independence_evidence, outcome.research_sources):
                return False
    return True


is_valid_stage_artifact = validate_stage_artifact


def stage_outcome_map(value: StageArtifact | Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    artifact = value if isinstance(value, StageArtifact) else StageArtifact.from_mapping(value)
    return {item.candidate_id: item.to_dict() for item in artifact.outcomes if item.candidate_id}


def accepted_evidence_urls(outcome: StageOutcome | Mapping[str, Any]) -> list[str]:
    if isinstance(outcome, StageOutcome):
        return [source.canonical_url for source in outcome.research_sources]
    raw = outcome.get("research_sources", [])
    if not isinstance(raw, list):
        return []
    return [
        str(item).strip() if isinstance(item, str) else str(item.get("url", "")).strip()
        for item in raw
        if (isinstance(item, str) and item.strip()) or (isinstance(item, Mapping) and str(item.get("url", "")).strip())
    ]


def prominence_structured_fields(claims: Sequence[ProminenceClaim | Mapping[str, Any]]) -> tuple[str, str]:
    """Return deterministic, bounded claim-type and evidence-URL fields."""
    claim_types: list[str] = []
    evidence_urls: list[str] = []
    for raw_claim in claims:
        claim = raw_claim if isinstance(raw_claim, ProminenceClaim) else ProminenceClaim.from_mapping(raw_claim)
        if claim.claim_type and claim.claim_type not in claim_types:
            claim_types.append(claim.claim_type[:100])
        for raw_url in claim.evidence_urls:
            url = canonicalize_url(raw_url)
            if url and url not in evidence_urls:
                evidence_urls.append(url)
    return " | ".join(claim_types)[:MAX_DISPLAY_TEXT], " | ".join(evidence_urls)[:MAX_URL_LENGTH]


def prominence_summary(claims: Sequence[ProminenceClaim | Mapping[str, Any]]) -> str:
    """Render prominence only as its own bounded presentation line."""
    parts: list[str] = []
    for raw_claim in claims:
        claim = raw_claim if isinstance(raw_claim, ProminenceClaim) else ProminenceClaim.from_mapping(raw_claim)
        if not claim.claim_type or not claim.display_text:
            continue
        urls = ", ".join(canonicalize_url(url) for url in claim.evidence_urls if canonicalize_url(url))
        claim_type = _bounded(claim.claim_type, 100)
        display_text = _bounded(claim.display_text, MAX_DISPLAY_TEXT)
        parts.append(f"{claim_type}: {display_text}" + (f" ({urls})" if urls else ""))
    return _bounded(" | ".join(parts), MAX_EVIDENCE_METADATA_TEXT * 3)


__all__ = [
    "CANDIDATE_ENVELOPE_SECONDS",
    "DISCOVERY_BUDGET_SECONDS",
    "GROUNDED_SYNTHESIS_PROMPT",
    "GROUNDING_QUEUE_FILENAME",
    "GROUNDING_STAGE_ARTIFACT_NAME",
    "MAX_DISPLAY_TEXT",
    "MAX_EVIDENCE_METADATA_TEXT",
    "MAX_EVIDENCE_TEXT",
    "MAX_RESPONSE_BYTES",
    "MAX_SOURCE_SUBJECT_TEXT",
    "MAX_SYNTHESIS_EVIDENCE_TEXT",
    "MIN_VENDOR_INTERVAL_SECONDS",
    "OUTCOME_ATTEMPTED_NO_DESCRIPTION",
    "OUTCOME_CLASSES",
    "OUTCOME_EVIDENCE_UNCHANGED",
    "OUTCOME_GROUNDED",
    "OUTCOME_NOT_ATTEMPTED",
    "OUTCOME_ORDER",
    "OUTCOME_SINGLE_SOURCE",
    "PREFLIGHT_ADAPTER_KEY_MISSING",
    "PREFLIGHT_ADAPTER_NOT_RUNNING",
    "PREFLIGHT_ADAPTER_NO_RESPONSE",
    "PREFLIGHT_ADAPTER_UNAVAILABLE",
    "PREFLIGHT_ADAPTER_VENDOR_ERROR",
    "PREFLIGHT_ENDPOINT_NOT_CONFIGURED",
    "PREFLIGHT_HEALTHY",
    "REASON_AUTHOR_MISMATCH",
    "REASON_AUTHOR_UNVERIFIABLE",
    "REASON_CODES",
    "REASON_EVIDENCE_UNCHANGED",
    "REASON_INSUFFICIENT_DEPTH",
    "REASON_INVALID_DESCRIPTION",
    "REASON_INVALID_SOURCE",
    "REASON_LABELS_RU",
    "REASON_PAGE_CHALLENGE_NO_EVIDENCE",
    "REASON_SEARCH_SNIPPET",
    "REASON_SOURCE_DENYLISTED",
    "REASON_STAGE_EXECUTION_FAILURE",
    "REASON_SYNTHESIS_FAILURE",
    "REASON_TRUNCATED_PAGE_NO_EVIDENCE",
    "SOURCE_INDEPENDENCE_REASON",
    "SOURCE_SUBJECT_SEPARATOR",
    "STAGE_BUDGET_SECONDS",
    "AcceptedSource",
    "BoundedHTTPError",
    "BoundedHTTPFetcher",
    "CandidateRequestBudget",
    "CandidateTiming",
    "CodexGroundedDescriptionSynthesizer",
    "DiscoveryResult",
    "FetchedSource",
    "GroundedDescriptionQueue",
    "GroundedDescriptionStage",
    "GroundedDescriptionStageRunner",
    "GroundedDescriptionStageService",
    "GroundingGateResult",
    "HTTPDiscovery",
    "HTTPDiscoveryProvider",
    "HTTPResponse",
    "HTTPTransport",
    "HostRateLimiter",
    "LanguageResolution",
    "LanguageResolutionState",
    "NoNetworkDiscoveryProvider",
    "NoNetworkHTTPTransport",
    "OutcomeClass",
    "PreflightResult",
    "ProminenceClaim",
    "ProminenceClaimsValidation",
    "ProminenceValidation",
    "QueueEntry",
    "SourceClassifier",
    "SourceIndependenceEvidence",
    "StageArtifact",
    "StageOutcome",
    "StandardLibraryHTTPFetcher",
    "StandardLibraryHTTPTransport",
    "SynthesisResult",
    "TargetResolution",
    "VendorRateLimiter",
    "accepted_evidence_urls",
    "candidate_source_language",
    "canonicalize_hostname",
    "canonicalize_url",
    "check_grounding_gate",
    "committed_source_classifier",
    "evaluate_grounding_gate",
    "grounding_titles_match",
    "is_valid_stage_artifact",
    "prominence_structured_fields",
    "prominence_summary",
    "read_stage_artifact",
    "reason_label",
    "resolve_durable_target",
    "schedule_candidates",
    "stage_outcome_map",
    "universal_grounding_gate",
    "validate_prominence_claim",
    "validate_prominence_claims",
    "validate_stage_artifact",
    "write_stage_artifact",
]
