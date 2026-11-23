from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from pathlib import Path
from typing import Any
from typing import Literal
from typing import Protocol
from urllib.parse import urljoin
from urllib.parse import urlparse

from autoscout.autobiographical_context import NARRATIVE_CLASSIFICATIONS
from autoscout.capture_bounds import DEFAULT_CAPTURE_MAX_RESPONSE_BYTES
from autoscout.capture_bounds import CaptureKind
from autoscout.capture_bounds import retain_ranking_detail_pages
from autoscout.capture_bounds import write_bounded_capture
from autoscout.capture_sanitization import sanitize_captured_html
from autoscout.helper_metadata import HELPER_METADATA_FIELDS
from autoscout.helper_metadata import validate_helper_metadata
from autoscout.identity import normalize_identity_text
from autoscout.ranking_author_policy import mention_author_exempt
from autoscout.ranking_detail_route import RankingDetailRoute
from autoscout.ranking_evidence import RankingPageEvidence
from autoscout.ranking_freshness import ranking_undated_declared
from autoscout.source_eligibility import award_title_has_no_specific_work  # noqa: F401
from autoscout.source_eligibility import source_eligibility_exclusion_reason
from autoscout.source_language import requires_english_helper_fields
from autoscout.source_page_identity import RANKING_DETAIL_SKIPPED_FIELD
from autoscout.source_page_identity import ParsedCapture
from autoscout.source_page_identity import amazon_charts_dated_url_matches
from autoscout.source_page_identity import amazon_charts_mention_is_grounded
from autoscout.source_page_identity import capture_for
from autoscout.source_page_identity import ranking_freshness_declared
from autoscout.source_proposals import normalize_source_identity
from autoscout.source_week_freshness import lenient_reference_date
from autoscout.source_week_freshness import source_week_freshness_declared
from autoscout.title_evidence import normalize_title_fields

FORBIDDEN_COLLECTOR_RESPONSE_FIELDS = {
    "article_body",
    "page_body",
    "full_body",
    "full_description",
    "description",
    "full_review",
    "review",
    "publisher_blurb",
    "jacket_copy",
    "copied_excerpt",
    "long_excerpt",
    "book_sample",
    "sample_text",
}

DEFAULT_MODEL_MAX_OUTPUT_TOKENS = 1200
# The collector names itself and gives a contact address on every source page
# request, and never presents itself as a browser.
COLLECTOR_USER_AGENT = "AutoScout/1.0 (+https://github.com/chillyweather)"
NON_ENGLISH_STRICT_CHART_SEARCH_TRANSLATION_RULE = (
    "For every non-English strict chart entry, search_translation must be nonblank and different "
    "from the authoritative title."
)


@dataclass
class CollectorResult:
    mentions: list[dict[str, Any]]
    raw_mention_count: int
    valid_mention_count: int
    skipped_item_count: int = 0
    debug_metadata: dict[str, Any] = field(default_factory=dict)


class CollectorRunError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: str = "failed",
        raw_mention_count: int = 0,
        valid_mention_count: int = 0,
        skipped_item_count: int = 0,
        debug_metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.raw_mention_count = raw_mention_count
        self.valid_mention_count = valid_mention_count
        self.skipped_item_count = skipped_item_count
        self.debug_metadata = debug_metadata or {}


def collector_request_headers() -> dict[str, str]:
    """Return the headers every source page request carries.

    USA TODAY answered HTTP 402 to the earlier request, which sent the user
    agent "AutoScout metadata collector" and no Accept header (Issue #764).
    """
    return {"User-Agent": COLLECTOR_USER_AGENT, "Accept": "*/*"}


def capture_content_evidence(page_data: Mapping[str, Any] | None) -> dict[str, int]:
    """Return the capture-size evidence one capture page actually holds.

    Issue #700 part A: the zero-contribution seam must tell "the capture
    held no list content" from "extraction returned nothing", and that
    distinction needs evidence, not inference. The collector holds the
    capture, so it attaches the byte count; the seam reads it. Only bytes:
    visible-text volume cannot separate a shell from a recoverable page
    (BookPage recovered 11 entries from fewer text chars than the
    Audiobooks shell holds), so any text-based rule would misclassify.
    A missing or non-string capture body counts as zero bytes held."""
    if isinstance(page_data, Mapping) and page_data.get("contract_family") == "bookinfo-weekly-json":
        bundle = page_data.get("bookinfo_evidence")
        ranking = bundle.get("ranking") if isinstance(bundle, Mapping) else None
        body = ranking.get("body") if isinstance(ranking, Mapping) else None
        return {
            "capture_html_bytes": 0,
            "capture_content_bytes": len(body.encode("utf-8")) if isinstance(body, str) else 0,
        }
    if isinstance(page_data, Mapping) and page_data.get("contract_family") == "ipc-top40-csv":
        bundle = page_data.get("ipc_top40_evidence")
        observed = bundle.get("file") if isinstance(bundle, Mapping) else None
        body = observed.get("body") if isinstance(observed, Mapping) else None
        return {
            "capture_html_bytes": 0,
            "capture_content_bytes": len(body.encode("utf-8")) if isinstance(body, str) else 0,
        }
    if isinstance(page_data, Mapping) and page_data.get("contract_family") in {
        "perlentaucher-daily-digest",
        "kirkus-coming-soon",
    }:
        # The page bodies are not retained, so the validated rows are the content this capture holds.
        text = page_data.get("text")
        return {
            "capture_html_bytes": 0,
            "capture_content_bytes": len(text.encode("utf-8")) if isinstance(text, str) else 0,
        }
    html = page_data.get("html", "") if isinstance(page_data, Mapping) else ""
    return {"capture_html_bytes": len(html) if isinstance(html, str) else 0}


def _collector_error(
    message: str,
    *,
    status: str = "failed",
    raw_mention_count: int = 0,
    valid_mention_count: int = 0,
    skipped_item_count: int = 0,
    debug_metadata: dict[str, Any] | None = None,
) -> CollectorRunError:
    """Shared helper for constructing CollectorRunError instances.

    All collector failure paths flow through this single construction site
    so that error defaults, logging, or telemetry can be added in one place."""
    return CollectorRunError(
        message,
        status=status,
        raw_mention_count=raw_mention_count,
        valid_mention_count=valid_mention_count,
        skipped_item_count=skipped_item_count,
        debug_metadata=debug_metadata or {},
    )


class Collector(Protocol):
    """Contract for a registered collection-mode adapter.

    A collector is looked up from ``COLLECTOR_REGISTRY`` by the dispatcher in
    ``source_candidates()`` using the source's normalized (stripped,
    lowercased) ``extraction_method``. It is therefore only ever invoked for a
    source it is registered to own, and must not re-inspect the raw
    ``extraction_method`` itself to decide whether to run.

    Inputs: the collection context -- the resolved weekly config file path
    (used to resolve relative fixture/response file paths), the run's output
    directory (used for any capture files written during collection), the
    source's configuration dict, and the run date string used to stamp
    mentions.

    Result: a ``CollectorResult`` carrying the mentions collected, the raw /
    valid / skipped item counts, and any debug metadata to merge into the
    source's run record. An empty ``CollectorResult`` means the collector ran
    and legitimately found nothing to collect.

    Failure: any inability to collect -- a missing or malformed fixture file,
    a blocked or CAPTCHA-like live response, forbidden copied-text fields, or
    any other safe-failure condition -- is raised as a ``CollectorRunError``,
    whose ``status`` ("failed" or "skipped") and count/debug fields flow
    directly into the source-run record.
    """

    def collect(
        self,
        config_path: Path,
        output_dir: Path,
        source: dict[str, Any],
        run_date: str,
    ) -> CollectorResult: ...


# Scanned against the separator-folded category (see the audiobook-ranking
# branch below), so ``adult_fiction`` and ``kids_books`` are caught the same as
# their spaced spellings. The lookbehinds keep the eligible negation eligible:
# Audiobooks.com titles its own category "Non-Fiction" and an extractor that
# copies that label must not have every mention dropped (Issue #375).
_AUDIOBOOK_EXCLUDED_TERMS = re.compile(
    r"(?<!non )(?<!non-)\b(fiction|thriller|kids?|children|teen|young adult|ya|summary|study guide|"
    r"course|lecture|non[- ]book|sleep sounds?|coming soon|forthcoming)\b",
    re.IGNORECASE,
)
_AUDIOBOOK_UNSUPPORTED_CLAIMS = re.compile(
    r"(?:\b(?:rank(?:ed)?|position)\s*#?\s*\d+\b|#\s*\d+|\bno\.\s*\d+\b|"
    r"\bnumber\s+(?:one|two|three)\b|\b(?:sales?|sold|units?|copies)\b|"
    r"\b(?:first|second|third) place\b|\bweek (?:of|ending)\b|\bdebut(?:ed)? at\b)",
    re.IGNORECASE,
)


def normalized_candidate_category(value: Any) -> str:
    """Return a candidate category folded to its separator-insensitive form.

    Extractors write the same category with different separators -- the
    2026-08-01 Audiobooks.com artifact used ``adult_nonfiction`` where the
    portfolio declares ``adult nonfiction`` (Issue #375). Comparing on this
    form accepts the token-identical value without widening the vocabulary:
    a genuinely different category still fails.

    Equality comparisons and the audiobook excluded-term scan both fold, so an
    underscore spelling behaves identically everywhere. Folding is what lets
    ``adult_fiction`` be caught -- ``_`` is a word character, so ``\\bfiction\\b``
    never matched it -- and ``_AUDIOBOOK_EXCLUDED_TERMS`` carries the lookbehinds
    that keep ``non fiction`` from being read as the ``fiction`` exclusion.
    """
    return re.sub(r"[\s_\-]+", " ", str(value)).strip().casefold()


CONTRACT_FAMILIES: tuple[str, ...] = (
    "ranking-table",
    "bookinfo-weekly-json",
    "perlentaucher-daily-digest",
    "kirkus-coming-soon",
    "pm-deal-email",
    "pw-issue-pdf",
    "ipc-top40-csv",
    "audible-chart",
    "amazon-charts",
    "audiobook-ranking",
    "exact-book-list",
    "nuevoslibros-frontlist",
    "publisher-frontlist",
    "award-list",
    "generic",
)


def contract_family(source: dict[str, Any]) -> str:
    """Name the single contract that governs one source.

    The families are mutually exclusive and ordered. Five portfolio sources set
    both a chart flag and ``metadata_exact_book_list``; the chart contract wins
    and the exact-book-list checks never run for them. Both this module's
    validator and ``extraction_contract`` resolve the family here, so an
    extractor is never asked for a shape that a lower-precedence branch
    described and the validator then rejects (Issue #375).
    """
    declared = str(source.get("contract_family", "")).strip().casefold()
    if declared:
        return declared
    if source.get("metadata_ranking_table") is True:
        return "ranking-table"
    if source.get("audible_explicit_chart") is True:
        return "audible-chart"
    if source.get("amazon_charts_weekly") is True:
        return "amazon-charts"
    if str(source.get("family", "")).strip().casefold() == "audiobook ranking":
        return "audiobook-ranking"
    if source.get("metadata_exact_book_list") is True:
        return "exact-book-list"
    return "generic"


# Keep sales detection digit-based. Worded sales claims are a documented
# deterministic-guard gap, not a reason to add broad semantic matching here.
_CHART_EVIDENCE_NOTE_DATA_TERM = r"(?:sales?|sold|units?|copies)"
_CHART_EVIDENCE_NOTE_NUMBER = r"\d(?:[\d,]*(?:\.\d+)?)?"
_CHART_EVIDENCE_NOTE_QUOTED_STRING = re.compile(r"[\"“]([^\"\n”]*?)[\"”]")
# Bare praise and topic terms can be chart headings or book subjects. Explicit
# data contexts and copied-prose markers remain unsafe; ambiguous metadata is kept.
_CHART_EVIDENCE_NOTE_UNSAFE_CONTEXT = re.compile(
    rf"""
    (?:
        \bprint\s+sales?\b
        |\bsales?\s+units?\b
        |\bunits?\s+sold\b
        |\bcopies?\s+sold\b
        |\b{_CHART_EVIDENCE_NOTE_DATA_TERM}\b
            (?:\s+(?:of|were?|are?|by|increased?|decreased?|grew|rose|fell|reached|total(?:ed)?|generated))*
            \s*{_CHART_EVIDENCE_NOTE_NUMBER}\b
        |\b{_CHART_EVIDENCE_NOTE_NUMBER}\b\s+\b(?:units?|copies?|sales?)\b
        |\b(?:sales?|units?|copies?)\s+(?:(?:were?|are?)\s+)?(?:rose|fell|high|low|strong|weak|down|up)\b
        |\b(?:reviews?|reviewers?)\b(?:\s+\w+){{0,4}}\s+
            (?:call|calls|say|says|describe|describes|praise|praises|name|names|rate|rates|dub|dubs)\b
        |\b(?:review|reviews?|reviewers?)\b[^.!?\n]{{0,80}}\b(?:quotation|quote|text)\b
        |\b(?:review|reviews?|reviewers?|quotation|quote)\b[^.!?\n]{{0,80}}[\"“]
        |\b(?:blurb|description|jacket\s+copy|prose)\b[^.!?\n]{{0,80}}[\"“]
        |\b(?:promotional\s+prose|marketing\s+(?:prose|copy)|advertising\s+prose)\b
        |\b(?:description|descriptions?)\s+(?:text|prose|copy)\b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def chart_evidence_note_has_unsafe_data(note: str, entry_title: str) -> bool:
    """Return whether a chart note contains unsafe data or copied prose.

    Sales, units, and copies can be part of a work's subject, so the guard
    rejects only numeric or claim-shaped uses rather than every occurrence of
    those words. A quoted string is safe only when every quoted string is the
    current entry title after the shared identity normalization. An explicit
    blurb or description marker still makes even a title quote copied prose.
    """
    normalized_title = normalize_identity_text(str(entry_title or ""))
    quoted_matches = list(_CHART_EVIDENCE_NOTE_QUOTED_STRING.finditer(note))
    if quoted_matches:
        if not normalized_title or any(
            normalize_identity_text(match.group(1)) != normalized_title for match in quoted_matches
        ):
            return True

        # Keep the quote delimiters so copied-prose markers such as
        # ``The blurb says "Title"`` remain visible to the context guard,
        # while masking title contents that may contain data-shaped tokens.
        masked_note = list(note)
        for match in quoted_matches:
            start, end = match.span(1)
            masked_note[start:end] = [" "] * (end - start)
        note = "".join(masked_note)

    return bool(_CHART_EVIDENCE_NOTE_UNSAFE_CONTEXT.search(note))


def with_observed_ranking_week(
    entry: dict[str, Any],
    source: dict[str, Any],
    page_data: dict[str, Any],
) -> dict[str, Any]:
    """Return *entry* with an undated ranking's week taken from the capture.

    An undated recurring ranking states no reporting period, so it has no
    source week an extractor could read.  Its ``source_week`` is the capture's
    retained observation date, the same value its replay fixtures use, and
    never a value the extractor supplied: a model-supplied week for such a page
    can only be invented (Issue #733).  A capture without a reference date
    leaves the field empty, so the required-field check fails closed.
    """
    if contract_family(source) != "ranking-table" or not ranking_undated_declared(source):
        return entry
    observed = str(page_data.get("reference_date") or "").strip()
    updated = dict(entry)
    if observed:
        updated["source_week"] = observed
    else:
        updated.pop("source_week", None)
    return updated


def extraction_contract_violation(
    raw_entry: dict[str, Any],
    mention: dict[str, Any],
    source: dict[str, Any],
    page_data: dict[str, Any],
    *,
    detail_route: RankingDetailRoute | None = None,
    parsed: ParsedCapture | None = None,
) -> str:
    """Return a drop reason for configured family-specific contract violations.

    *parsed*, when given, is the collection pass's capture of
    ``page_data["html"]``, shared by every mention on the page (Issue #718).
    """
    source_signal_family = (
        str(source.get("signal_family") or source.get("family") or source.get("source_family", "")).strip().casefold()
    )
    source_reason = source_eligibility_exclusion_reason(
        title=raw_entry.get("title", ""),
        source_family=source_signal_family,
    )
    if source_reason:
        return source_reason
    # Source-specific exclusions apply on both extraction paths (Issue #1004).
    from autoscout.source_card_filters import violation as source_card_violation

    reason = source_card_violation(raw_entry, page_data, source, mention)
    if reason:
        return reason
    family = contract_family(source)
    if family == "publisher-frontlist":
        from autoscout.publisher_frontlist import linked_html as publisher_linked_html
        from autoscout.publisher_frontlist import matching_card as publisher_matching_card

        source_url = str(source.get("url", ""))
        markup = str(page_data.get("html", ""))
        authors = raw_entry.get("primary_authors") or raw_entry.get("authors") or []
        if not isinstance(authors, list) or not all(isinstance(author, str) for author in authors):
            return "frontlist primary_authors must be a list of strings"
        try:
            publisher_card = publisher_matching_card(
                markup, source_url, str(raw_entry.get("title", "")), authors, str(raw_entry.get("evidence_url", ""))
            )
            if publisher_card is None:
                return "frontlist title, authors and link do not match one declared publisher_card"
            if raw_entry.get("publication_year") is not None:
                return "publisher frontlist states no publication year"
            normalized = publisher_linked_html(markup, source_url)
        except ValueError as exc:
            return str(exc)
        # The pass's capture parses the shared normal form once for every mention.
        normalized_page = None if parsed is None else parsed.subpage(normalized)
        return _validate_exact_book_list_mention(
            raw_entry, mention, source, {**page_data, "html": normalized}, parsed=normalized_page
        )
    if family == "nuevoslibros-frontlist":
        from autoscout.nuevoslibros_frontlist import linked_html
        from autoscout.nuevoslibros_frontlist import matching_card

        source_url = str(source.get("url", ""))
        markup = str(page_data.get("html", ""))
        authors = raw_entry.get("primary_authors") or raw_entry.get("authors") or []
        if not isinstance(authors, list) or not all(isinstance(author, str) for author in authors):
            return "frontlist primary_authors must be a list of strings"
        try:
            card = matching_card(
                markup, source_url, str(raw_entry.get("title", "")), authors, str(raw_entry.get("evidence_url", ""))
            )
            if card is None:
                return "frontlist title, authors and link do not match one declared card"
            # The extractor writes the year as a number or as its digits; both are the same year.
            year = raw_entry.get("publication_year")
            if year is not None and (isinstance(year, bool) or str(year).strip() != card.release_date[:4]):
                return "frontlist publication_year differs from its stated release date"
            normalized = linked_html(markup, source_url)
        except ValueError as exc:
            return str(exc)
        # The pass's capture parses the shared normal form once for every mention.
        normalized_page = None if parsed is None else parsed.subpage(normalized)
        return _validate_exact_book_list_mention(
            raw_entry, mention, source, {**page_data, "html": normalized}, parsed=normalized_page
        )
    if family == "bookinfo-weekly-json":
        from autoscout.bookinfo_ranking import mention_violation

        return mention_violation(raw_entry, page_data, source)
    if family == "ipc-top40-csv":
        from autoscout.ipc_top40_ranking import mention_violation as ipc_top40_mention_violation

        return ipc_top40_mention_violation(raw_entry, page_data, source)
    if family == "perlentaucher-daily-digest":
        from autoscout.perlentaucher_digest import mention_violation as perlentaucher_mention_violation

        digest_violation = perlentaucher_mention_violation(raw_entry, page_data, source)
        if digest_violation or not requires_english_helper_fields(source):
            return digest_violation
        return _non_english_helper_violation(raw_entry, source, "Perlentaucher digest")
    if family == "kirkus-coming-soon":
        from autoscout.kirkus_listing import mention_violation as kirkus_mention_violation

        return kirkus_mention_violation(raw_entry, page_data, source)
    if family == "award-list":
        from autoscout.award_list import mention_violation as award_list_mention_violation

        return award_list_mention_violation(raw_entry, mention, page_data, source)
    if family == "ranking-table":
        allowed_fields = {
            "title",
            "primary_authors",
            "authors",
            "isbn",
            "current_rank",
            "source_week",
            "source_url",
            "evidence_url",
            "source_check_date",
            "signal_type",
            "signal_strength",
            "evidence_note",
            "candidate_category",
            "english_title_type",
            "english_title_provenance",
        }
        is_english = not requires_english_helper_fields(source)
        if not is_english:
            allowed_fields |= {
                *HELPER_METADATA_FIELDS,
                "english_author_display",
                "english_annotation",
            }
        prohibited = sorted(set(raw_entry) - allowed_fields)
        if prohibited:
            return f"ranking table mention contains prohibited fields: {', '.join(prohibited)}"
        detail_route = detail_route or RankingDetailRoute.from_source(source)
        detail_fields = detail_route.requested_fields if detail_route is not None else ()
        required = ["isbn", "current_rank", "source_week"]
        if "category" in detail_fields:
            required.append("candidate_category")
        missing = [field for field in required if not str(raw_entry.get(field, "")).strip()]
        if missing:
            return f"ranking table mention missing required metadata: {', '.join(missing)}"
        rank = raw_entry.get("current_rank")
        if isinstance(rank, bool) or not str(rank).strip().isdigit() or int(str(rank).strip()) < 1:
            return "ranking table current_rank is not a positive integer"
        authors = raw_entry.get("primary_authors") or raw_entry.get("authors") or []
        primary_authors = [str(author).strip() for author in authors] if isinstance(authors, list) else []
        from autoscout.source_page_identity import ranking_mention_is_grounded

        if not ranking_mention_is_grounded(
            str(page_data.get("html", "")),
            title=str(raw_entry.get("title", "")),
            primary_authors=primary_authors,
            isbn=str(raw_entry.get("isbn", "")),
            current_rank=str(rank),
            source_week=str(raw_entry.get("source_week", "")),
            entry_scope_selector=str(source.get("entry_scope_selector", "")),
            ranking_source_statement=(
                str(source["ranking_source_statement"]) if "ranking_source_statement" in source else None
            ),
            ranking_source=dict(source),
            detail_route=detail_route,
            detail_pages=(page_data.get("detail_pages") if isinstance(page_data.get("detail_pages"), list) else None),
            evidence_url=str(mention.get("evidence_url") or raw_entry.get("evidence_url", "")).strip(),
            candidate_category=str(raw_entry.get("candidate_category", "")),
            identity_html=str(page_data.get("identity_html", "")),
            parsed=parsed,
        ):
            if source.get("entry_scope_selector"):
                return "rank, book metadata, or source week is not grounded in one declared bounded entry row"
            if detail_route is not None:
                return "ranking table detail field or row linkage is not grounded in the linked bounded detail page"
            return "ranking table rank, book metadata, or source week is not grounded in one current table row"
        configured_url = str(source.get("url", "")).strip()
        evidence_url = str(raw_entry.get("evidence_url", "")).strip()
        if detail_route is not None:
            if normalize_source_identity(str(raw_entry.get("source_url", "")).strip()) != normalize_source_identity(
                configured_url
            ):
                return "ranking table source_url is not the configured current page"
            if not evidence_url or normalize_source_identity(evidence_url) == normalize_source_identity(configured_url):
                return "ranking table evidence_url must be the linked detail page"
        else:
            for field in ("source_url", "evidence_url"):
                if normalize_source_identity(str(raw_entry.get(field, "")).strip()) != normalize_source_identity(
                    configured_url
                ):
                    return f"ranking table {field} is not the configured current page"
        if str(mention.get("signal_type", "")).strip().casefold() != "bestseller/ranking":
            return "signal type does not preserve bestseller/ranking"
        note = str(raw_entry.get("evidence_note", "")).strip()
        if not note or re.search(r"\b(?:units?|sold|sales|ytd|review|description)\b", note, re.IGNORECASE):
            return "ranking note is missing or contains forbidden review, description, or sales data"
        expected_category = str(source.get("eligible_candidate_category", "")).strip()
        if expected_category and normalized_candidate_category(
            raw_entry.get("candidate_category", "")
        ) != normalized_candidate_category(expected_category):
            return f"ranking table candidate_category must be {expected_category}"
        if not is_english:
            # A row that legitimately carries no author has no author to
            # transliterate.  Requiring the note would force an invented one.
            helper_error = validate_helper_metadata(
                raw_entry,
                title_relation=None,
                author_note_required=bool(primary_authors) or not mention_author_exempt(source),
            )
            if helper_error:
                return helper_error.replace("exact book list", "ranking table")
        return ""
    if family == "audible-chart":
        allowed = {
            "title",
            "primary_authors",
            "authors",
            "source_url",
            "evidence_url",
            "item_url",
            "signal_type",
            "signal_strength",
            "evidence_note",
            "current_rank",
            "candidate_category",
            "narrative_classification",
            "underlying_work_type",
            "publication_status",
            "english_title_type",
            "english_title_provenance",
        }
        is_english = not requires_english_helper_fields(source)
        if not is_english:
            # Non-English charts carry helper translation fields; the original
            # title and primary author stay authoritative (Issue #366).
            allowed |= {
                *HELPER_METADATA_FIELDS,
                "english_author_display",
                "english_annotation",
            }
        prohibited = sorted(set(raw_entry) - allowed)
        if prohibited:
            return f"Audible explicit chart mention contains prohibited fields: {', '.join(prohibited)}"
        if not is_english:
            helper_error = validate_helper_metadata(raw_entry, title_relation=None)
            if helper_error:
                return helper_error.replace("exact book list", "Audible explicit chart")
        rank = raw_entry.get("current_rank")
        if isinstance(rank, bool) or not str(rank).isdigit() or not 1 <= int(str(rank)) <= 20:
            return "Audible explicit chart current_rank must be an integer from 1 to 20"
        configured_url = str(source.get("url", "")).strip()
        if normalize_source_identity(str(raw_entry.get("source_url", ""))) != normalize_source_identity(configured_url):
            return "Audible explicit chart source_url is not the configured base chart URL"
        evidence_url = str(
            mention.get("evidence_url") or raw_entry.get("evidence_url") or raw_entry.get("item_url") or ""
        )
        evidence_url = normalize_chart_evidence_url(evidence_url, source)
        parsed_evidence = urlparse(evidence_url)
        if not same_domain(configured_url, evidence_url) or "/pd/" not in parsed_evidence.path:
            return "Audible explicit chart evidence URL is not a direct Audible /pd/ product URL"
        if str(mention.get("signal_type", "")).casefold() != "audiobook ranking":
            return "Audible explicit chart signal type does not preserve audiobook ranking"
        if (
            str(raw_entry.get("signal_strength", "")).casefold()
            != str(source.get("expected_signal_strength", "")).casefold()
        ):
            return "Audible explicit chart signal strength does not preserve configured strength"
        if str(raw_entry.get("underlying_work_type", "")).strip().casefold() not in {"book", "work"}:
            return "Audible explicit chart underlying_work_type must be book or work"
        if str(raw_entry.get("publication_status", "")).strip().casefold() != "published":
            return "Audible explicit chart publication_status must be published"
        expected_category = str(source.get("eligible_candidate_category", "")).strip()
        if normalized_candidate_category(raw_entry.get("candidate_category", "")) != normalized_candidate_category(
            expected_category
        ):
            return f"Audible explicit chart candidate_category must be {expected_category}"
        note = str(raw_entry.get("evidence_note", "")).strip()
        if not note or len(note) > 240 or chart_evidence_note_has_unsafe_data(note, raw_entry.get("title", "")):
            return "Audible explicit chart evidence_note must be concise metadata-only without sales, units, reviews, or prose"
        classification = str(raw_entry.get("narrative_classification", "")).strip().casefold()
        if classification not in NARRATIVE_CLASSIFICATIONS:
            return "Audible explicit chart narrative_classification is outside the closed vocabulary"
        from autoscout.source_page_identity import audible_chart_mention_is_grounded
        from autoscout.source_page_identity import audible_de_chart_mention_is_grounded

        authors = raw_entry.get("primary_authors") or raw_entry.get("authors") or []
        grounded = (
            audible_de_chart_mention_is_grounded
            if str(source.get("language", "")).strip().casefold() == "de"
            else audible_chart_mention_is_grounded
        )
        if not grounded(
            str(page_data.get("html", "")),
            str(page_data.get("source_page", "")),
            rank=int(str(rank)),
            title=str(raw_entry.get("title", "")),
            authors=[str(a) for a in authors],
            evidence_url=evidence_url,
            parsed=parsed,
        ):
            return "Audible explicit chart rank, title, primary author, and product URL are not grounded in one entry"
        return ""
    if family == "amazon-charts":
        allowed = {
            "title",
            "primary_authors",
            "authors",
            "source_url",
            "evidence_url",
            "item_url",
            "signal_type",
            "signal_strength",
            "evidence_note",
            "current_rank",
            "candidate_category",
            "weeks_on_list",
            "english_title_type",
            "english_title_provenance",
        }
        is_english = not requires_english_helper_fields(source)
        if not is_english:
            # Non-English charts carry helper translation fields; the original
            # title and primary author stay authoritative (Issue #366, #378).
            allowed |= {
                *HELPER_METADATA_FIELDS,
                "english_author_display",
                "english_annotation",
            }
        prohibited = sorted(set(raw_entry) - allowed)
        if prohibited:
            return f"Amazon Charts weekly mention contains prohibited fields: {', '.join(prohibited)}"
        if not is_english:
            helper_error = validate_helper_metadata(raw_entry, title_relation=None)
            if helper_error:
                return helper_error.replace("exact book list", "Amazon Charts weekly")
        rank = raw_entry.get("current_rank")
        if isinstance(rank, bool) or not str(rank).isdigit() or not 1 <= int(str(rank)) <= 20:
            return "Amazon Charts weekly current_rank must be an integer from 1 to 20"
        rank_value = int(str(rank))
        source_page = str(page_data.get("source_page", "")).strip()
        source_url = str(raw_entry.get("source_url", "")).strip()
        source_parsed = urlparse(source_url)
        page_parsed = urlparse(source_page)
        if (
            source_parsed.scheme.casefold() != page_parsed.scheme.casefold()
            or source_parsed.netloc.casefold() != page_parsed.netloc.casefold()
            or source_parsed.path != page_parsed.path
            or source_parsed.query != page_parsed.query
            or source_parsed.fragment != page_parsed.fragment
        ):
            return "Amazon Charts weekly source_url is not the captured dated chart page"
        evidence_url = str(
            mention.get("evidence_url") or raw_entry.get("evidence_url") or raw_entry.get("item_url") or ""
        ).strip()
        evidence_url = normalize_chart_evidence_url(evidence_url, source)
        parsed_evidence = urlparse(evidence_url)
        if (
            not same_domain(source_page, evidence_url)
            or re.fullmatch(r"/dp/[A-Z0-9]{10}", parsed_evidence.path, flags=re.IGNORECASE) is None
        ):
            return "Amazon Charts weekly evidence URL is not a direct Amazon /dp/ product URL"
        if str(raw_entry.get("signal_type", "")).strip().casefold() != "bestseller/ranking":
            return "Amazon Charts weekly signal type does not preserve bestseller/ranking"
        expected_strength = str(source.get("expected_signal_strength", "")).strip()
        if str(raw_entry.get("signal_strength", "")).strip().casefold() != expected_strength.casefold():
            return "Amazon Charts weekly signal strength does not preserve configured strength"
        expected_category = str(source.get("eligible_candidate_category", "")).strip()
        if normalized_candidate_category(raw_entry.get("candidate_category", "")) != normalized_candidate_category(
            expected_category
        ):
            return f"Amazon Charts weekly candidate_category must be {expected_category}"
        note = str(raw_entry.get("evidence_note", "")).strip()
        if not note or len(note) > 240 or chart_evidence_note_has_unsafe_data(note, raw_entry.get("title", "")):
            return (
                "Amazon Charts weekly evidence_note must be concise metadata-only without print sales, sales units, "
                "units sold, copies sold, promotional prose, review text, or description text"
            )
        weeks = raw_entry.get("weeks_on_list")
        if weeks is None or isinstance(weeks, bool) or not str(weeks).isdigit() or int(str(weeks)) <= 0:
            return "Amazon Charts weekly weeks_on_list must be a positive integer"

        authors = raw_entry.get("primary_authors") or raw_entry.get("authors") or []
        if not isinstance(authors, list):
            return "Amazon Charts weekly primary_authors must be a list"
        if not amazon_charts_mention_is_grounded(
            str(page_data.get("html", "")),
            source_page,
            rank=rank_value,
            title=str(raw_entry.get("title", "")),
            authors=[str(author) for author in authors],
            evidence_url=evidence_url,
            parsed=parsed,
        ):
            return "Amazon Charts weekly rank, title, primary author, and product URL are not grounded in one entry"
        return ""
    if family != "audiobook-ranking":
        if family == "exact-book-list":
            return _validate_exact_book_list_mention(raw_entry, mention, source, page_data, parsed=parsed)
        if family == "generic" and requires_english_helper_fields(source):
            # A non-English generic source must carry the helper fields the
            # contract requests; English books need no English title, so an
            # English generic entry without helpers still passes (Issue #645).
            return _non_english_helper_violation(raw_entry, source, "generic")
        return ""
    if str(raw_entry.get("underlying_work_type", "")).strip().casefold() not in {"book", "work"}:
        return "underlying work is not an eligible book/work"
    category = str(raw_entry.get("candidate_category", "")).strip()
    title = str(raw_entry.get("title", "")).strip()
    if not category or _AUDIOBOOK_EXCLUDED_TERMS.search(f"{normalized_candidate_category(category)} {title}"):
        return "excluded or missing adult nonfiction category"
    publication_status = str(raw_entry.get("publication_status", "")).strip().casefold()
    if publication_status != "published":
        return "item is forthcoming or publication status is unsupported"
    configured_url = str(source.get("url", "")).strip()
    source_url = str(raw_entry.get("source_url", "")).strip()
    if normalize_source_identity(source_url) != normalize_source_identity(configured_url):
        return "source URL is not the configured category URL"
    evidence_url = str(raw_entry.get("evidence_url") or raw_entry.get("item_url") or "").strip()
    parsed_evidence = urlparse(evidence_url)
    if not same_domain(configured_url, evidence_url) or "/audiobook/" not in parsed_evidence.path.casefold():
        return "evidence URL is not a book-level audiobook URL"
    if str(mention.get("signal_type", "")).strip().casefold() != "audiobook ranking":
        return "signal type does not preserve audiobook ranking"
    if str(raw_entry.get("signal_strength", "")).strip().casefold() != "minor mention":
        return "unsupported ranking strength claim"
    evidence_note = str(raw_entry.get("evidence_note", "")).strip()
    if not evidence_note or _AUDIOBOOK_UNSUPPORTED_CLAIMS.search(evidence_note):
        return "unsupported rank, sales, units, or dated-methodology claim"
    if requires_english_helper_fields(source):
        return _non_english_helper_violation(raw_entry, source, "audiobook ranking")
    return ""


def _non_english_helper_violation(raw_entry: dict[str, Any], source: dict[str, Any], family_label: str) -> str:
    """Return the helper violation for one non-English unrouted mention.

    The ``generic`` and ``audiobook-ranking`` families share the chart-style
    helper rule: every helper field must be present and valid, but no
    English-equality relation is policed (``title_relation=None``).
    """
    raw_authors = raw_entry.get("primary_authors") or raw_entry.get("authors") or []
    primary_authors = [str(a).strip() for a in raw_authors] if isinstance(raw_authors, list) else []
    helper_error = validate_helper_metadata(
        raw_entry,
        title_relation=None,
        author_note_required=bool(primary_authors) or not mention_author_exempt(source),
    )
    if helper_error:
        return helper_error.replace("exact book list", family_label)
    return ""


def _validate_exact_book_list_mention(
    raw_entry: dict[str, Any],
    mention: dict[str, Any],
    source: dict[str, Any],
    page_data: dict[str, Any],
    *,
    parsed: ParsedCapture | None = None,
) -> str:
    """Require configured metadata and linked-entry grounding via ``exact_book_list_mention_is_grounded``."""
    from autoscout.source_page_identity import exact_book_list_mention_is_grounded

    configured_url = str(source.get("url", "")).strip()
    # source_url must be the configured source URL.
    source_url = str(raw_entry.get("source_url", "")).strip()
    if not source_url or normalize_source_identity(source_url) != normalize_source_identity(configured_url):
        return "exact book list source_url is not the configured source page"
    # Preserve source's signal_family (or family) as signal_type.
    expected_signal_type = str(source.get("signal_family") or source.get("family", "")).strip()
    if str(mention.get("signal_type", "")).strip().casefold() != expected_signal_type.casefold():
        return f"exact book list signal_type must be {expected_signal_type}"
    # Preserve configured expected signal strength.
    expected_strength = str(source.get("expected_signal_strength", "")).strip()
    if expected_strength:
        if str(raw_entry.get("signal_strength", "")).strip().casefold() != expected_strength.casefold():
            return f"exact book list signal_strength must be {expected_strength}"
    # Preserve configured eligible candidate category.
    eligible_category = str(source.get("eligible_candidate_category", "")).strip()
    if eligible_category:
        actual_category = str(raw_entry.get("candidate_category", "")).strip()
        if normalized_candidate_category(actual_category) != normalized_candidate_category(eligible_category):
            return f"exact book list candidate_category must be {eligible_category}, got {actual_category}"
    # Ground title, all authors, and evidence URL in a single link on the page.
    title = str(raw_entry.get("title", "")).strip()
    raw_authors = raw_entry.get("primary_authors") or raw_entry.get("authors") or []
    authors: list[str] = [str(a).strip() for a in raw_authors if isinstance(raw_authors, list) and str(a).strip()]
    evidence_url = str(mention.get("evidence_url", "")).strip()
    if not title or not authors or not evidence_url:
        return "exact book list entry missing title, authors, or evidence URL"
    source_page = str(page_data.get("source_page", ""))
    grounding_page = capture_for(str(page_data.get("html", "")), source_page, parsed)
    entry_section_heading = str(source.get("entry_section_heading", "")).strip()
    if entry_section_heading:
        # A declared section bounds the list: a book linked elsewhere on the
        # page, such as an earlier award stage kept below the current one, is
        # not an entry of this source.
        section_page = grounding_page.section(entry_section_heading)
        if section_page is None:
            return f"exact book list entry section '{entry_section_heading}' is not present exactly once"
        grounding_page = section_page
    if not exact_book_list_mention_is_grounded(
        grounding_page.html_content,
        source_page,
        title=title,
        authors=authors,
        evidence_url=evidence_url,
        parsed=grounding_page,
    ):
        if entry_section_heading:
            return (
                "exact book list title and authors are not grounded in a linked entry of the "
                f"'{entry_section_heading}' section"
            )
        return "exact book list title and authors are not grounded in the captured linked entry"
    helper_error = validate_helper_metadata(
        raw_entry,
        title_relation=requires_english_helper_fields(source),
    )
    if helper_error:
        return helper_error
    return ""


def extraction_entries(data: Any) -> Any:
    if isinstance(data, dict):
        return data.get("mentions", data.get("entries", []))
    return data


def normalized_agent_mention(entry: dict[str, Any], source: dict[str, Any], run_date: str) -> dict[str, Any]:
    evidence_url = str(entry.get("evidence_url") or entry.get("source_url") or entry.get("item_url") or "").strip()
    evidence_url = normalize_chart_evidence_url(evidence_url, source)
    normalized = {
        **entry,
        "primary_authors": list(entry.get("primary_authors") or entry.get("authors") or []),
        "source_url": str(entry.get("source_url") or evidence_url or source.get("url", "")).strip(),
        "evidence_url": evidence_url,
        "source_check_date": str(entry.get("source_check_date") or run_date),
        "signal_type": str(entry.get("signal_type") or source.get("family", "")).strip(),
        "signal_strength": str(entry.get("signal_strength") or "minor mention").strip(),
        "evidence_note": str(entry.get("evidence_note", "")).strip(),
    }
    if source.get("audible_explicit_chart") is True:
        normalized["retailer_discovery"] = True
    normalize_title_fields(normalized, classify=True)
    return normalized


def _swap_www_host(url: str) -> str | None:
    """Return the URL with its www. prefix toggled, or None if the hostname
    cannot be parsed."""
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    host = parsed.hostname or ""
    if not host:
        return None
    if host.startswith("www."):
        new_host = host[4:]
    else:
        new_host = "www." + host
    netloc = new_host
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    try:
        return parsed._replace(netloc=netloc).geturl()
    except Exception:
        return None


def resolve_provenance_url(url: str, source_url: str) -> str | None:
    """Resolve a provenance URL against the configured source page.

    Absolute non-HTTP URLs are returned unchanged so callers can apply their
    existing scheme policy.  Relative URLs and malformed HTTP(S) URLs resolve
    to ``None`` when no HTTP(S) URL can be produced.
    """
    value = str(url).strip()
    if not value:
        return None
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if parsed.scheme:
        if parsed.scheme.casefold() in {"http", "https"} and not parsed.netloc:
            return None
        return value

    base = str(source_url).strip()
    try:
        base_parsed = urlparse(base)
        resolved = urljoin(base, value)
        resolved_parsed = urlparse(resolved)
    except ValueError:
        return None
    if base_parsed.scheme.casefold() not in {"http", "https"} or not base_parsed.netloc:
        return None
    if resolved_parsed.scheme.casefold() not in {"http", "https"} or not resolved_parsed.netloc:
        return None
    return resolved


def canonical_provenance_url(url: str, source: Mapping[str, Any]) -> str | None:
    """Return the one canonical form of a provenance URL, or ``None``.

    This is the single URL normalisation seam (Issue #454).  Five defects in a
    row (#425, #432, #438, #436, #450) had the same cause: the collector
    normalised a URL one way and a validator normalised it another way, so
    evidence the page genuinely carried was refused and a whole Configured
    Source was discarded.  The repairs were correct and local, and none of them
    stopped the next one, because the choice of helper lived at each call site.

    Every collector and every validator now funnels through here, and the
    chart-specific behaviour is selected from ``source``, never from the call
    site:

    * an Amazon weekly chart resolves relative to the configured URL and drops
      the tracking suffix after a recoverable ASIN (#432, #438, #450);
    * every other source, an explicit Audible chart included, resolves relative
      to the configured URL (#425, #432).

    Consolidating the seam collapsed the Audible rule into the general one: both
    resolve against the configured URL and nothing more.  Keeping a separate
    Audible branch would name a distinction that no longer exists, so there is
    one branch and the ASIN rule is the only special case left.

    The rule itself is untouched: this normalises, it never relaxes.  Contract
    validation still applies the original domain and strict product path rules
    to the returned value.  ``None`` means no HTTP(S) URL can be produced from
    the input at all, and the caller must refuse the evidence.
    """
    value = str(url).strip()
    source_url = str(source.get("url", "")).strip()
    if source.get("amazon_charts_weekly") is not True:
        return resolve_provenance_url(value, source_url)
    resolved = resolve_provenance_url(value, source_url)
    if resolved is None:
        return None
    try:
        parsed = urlparse(resolved)
    except ValueError:
        return resolved
    match = re.match(r"^(/dp/[A-Z0-9]{10})(?:/|$)", parsed.path, flags=re.IGNORECASE)
    if match is None:
        return resolved
    try:
        return parsed._replace(path=match.group(1), query="", fragment="").geturl()
    except ValueError:
        return resolved


def normalize_chart_evidence_url(url: str, source: dict[str, Any]) -> str:
    """Collector-facing view of ``canonical_provenance_url``.

    Collectors need a string so that their own domain and product path rules
    produce the refusal message the operator reads.  When no canonical form
    exists the stripped input is handed back unchanged, which those rules then
    refuse; the seam never invents a URL.
    """
    value = str(url).strip()
    return canonical_provenance_url(value, source) or value


def evidence_url_verified(
    page_data: dict[str, Any], evidence_url: str, *, links: set[str] | None = None
) -> tuple[bool, str]:
    if not evidence_url:
        return False, "evidence URL is empty"
    source_page = str(page_data.get("source_page", "")).strip()
    html_text = str(page_data.get("html", ""))
    if evidence_url == source_page:
        return True, ""
    if page_data.get("contract_family") == "bookinfo-weekly-json":
        from autoscout.bookinfo_ranking import evidence_url_grounded as bookinfo_evidence_url_grounded

        if bookinfo_evidence_url_grounded(page_data, evidence_url):
            return True, ""
    if page_data.get("contract_family") == "ipc-top40-csv":
        from autoscout.ipc_top40_ranking import evidence_url_grounded

        if evidence_url_grounded(page_data, evidence_url):
            return True, ""
    if page_data.get("contract_family") == "perlentaucher-daily-digest":
        from autoscout.perlentaucher_digest import evidence_url_grounded as perlentaucher_evidence_url_grounded

        if perlentaucher_evidence_url_grounded(page_data, evidence_url):
            return True, ""
    if page_data.get("contract_family") == "kirkus-coming-soon":
        from autoscout.kirkus_listing import evidence_url_grounded as kirkus_evidence_url_grounded

        if kirkus_evidence_url_grounded(page_data, evidence_url):
            return True, ""
    if not html_text:
        return False, "source page HTML is empty, cannot verify evidence URL"
    if links is None:
        links = page_links(html_text, source_page)
    if evidence_url in links:
        return True, ""
    # The page may use a different www convention than the evidence URL.
    # Try the www-toggled variant before declaring a domain mismatch.
    evidence_www_swapped = _swap_www_host(evidence_url)
    if evidence_www_swapped is not None and evidence_www_swapped != evidence_url and evidence_www_swapped in links:
        return True, ""
    if not same_domain(source_page, evidence_url):
        return False, "evidence URL not found on source page and domain differs from source"
    return False, "evidence URL not found on source page"


def _normalize_hostname(hostname: str | None) -> str:
    """Normalize a hostname for domain comparison.

    Strips the leading `www.` so that apex and www subdomains compare as the
    same site.  Other subdomains (e.g. books.example.test vs www.example.test)
    are deliberately kept distinct - sibling subdomains may be independently
    operated and are not assumed to belong to the same source."""
    if hostname is None:
        return ""
    lowered = hostname.lower()
    if lowered.startswith("www."):
        return lowered[4:]
    return lowered


def same_domain(url_a: str, url_b: str) -> bool:
    try:
        return _normalize_hostname(urlparse(url_a).hostname) == _normalize_hostname(urlparse(url_b).hostname)
    except Exception:
        return False


_JSON_URL_FIELDS = {"productURL", "url", "canonicalUrl"}

# Pre-sort by descending length so longer field names match before
# shorter prefixes (e.g. "canonicalUrl" before "url" in alternation).
# The regex only captures the raw string value; _json_unescape handles
# all JSON escape forms (\/, \u002F, \uXXXX, etc.) before we check
# whether the decoded value is an http/https URL.
_JSON_URL_PATTERN = re.compile(
    r'"(' + "|".join(map(re.escape, sorted(_JSON_URL_FIELDS, key=len, reverse=True))) + r')"\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)


def _json_unescape(s: str) -> str:
    """Decode common JSON string escapes that may appear in embedded URLs."""
    s = s.replace(r"\/", "/")

    def _unicode_sub(m: re.Match[str]) -> str:
        return chr(int(m.group(1), 16))

    s = re.sub(r"\\u([0-9a-fA-F]{4})", _unicode_sub, s)
    return s


def _json_field_urls(markup: str, *, source_page: str = "") -> set[str]:
    """Extract URLs from JSON-embedded fields like productURL, url, canonicalUrl.

    These fields appear in structured data embedded in captured HTML (common
    in SPA pages like USA TODAY's booklist).  The values are JSON-string-encoded
    URLs; we decode basic escapes and keep only http/https URLs.

    When source_page is provided, only URLs on the same domain are returned.
    This prevents tracker/ad URLs embedded in analytics script tags from
    entering the evidence-URL whitelist."""
    urls: set[str] = set()
    for field_name, url_value in _JSON_URL_PATTERN.findall(markup):
        try:
            decoded = html.unescape(_json_unescape(url_value))
        except Exception:
            continue
        if not decoded.startswith(("http://", "https://")):
            continue
        if field_name.lower() == "url" and source_page and not same_domain(source_page, decoded):
            continue
        urls.add(decoded)
    return urls


def page_links(markup: str, source_page: str) -> set[str]:
    links = set()
    for href in re.findall(r"\bhref=[\"']([^\"']+)[\"']", markup, flags=re.IGNORECASE):
        href = html.unescape(href.strip())
        links.add(href)
        links.add(urljoin(source_page, href))
    for url in _json_field_urls(markup, source_page=source_page):
        links.add(url)
        links.add(urljoin(source_page, url))
    return links


def _codex_artifact_source(item: Mapping[str, Any]) -> dict[str, Any]:
    """Reconstruct the source view needed by Codex provenance validation."""
    metadata = item.get("source_metadata")
    source = dict(metadata) if isinstance(metadata, Mapping) else {}
    source["url"] = str(item.get("source_url", "")).strip()
    return source


def normalize_codex_provenance_url(url: str, item: Mapping[str, Any]) -> str | None:
    """Normalize and resolve a Codex artifact provenance URL.

    The artifact carries source metadata under ``source_metadata`` rather than
    as a collector source object.
    Reconstruct the small source view required by the single normalisation seam
    so Codex validation uses the same URL rules as collection (Issue #454).
    """
    return canonical_provenance_url(url, _codex_artifact_source(item))


def codex_provenance_links(page_data: Mapping[str, Any], item: Mapping[str, Any]) -> set[str]:
    """Return capture links normalized with the Codex artifact's source rules."""
    html_text = str(page_data.get("html", ""))
    source_page = str(page_data.get("source_page", "")).strip()
    if not html_text:
        return set()
    source = _codex_artifact_source(item)
    normalized_links: set[str] = set()
    for link in page_links(html_text, source_page):
        resolved = canonical_provenance_url(link, source)
        if resolved is not None:
            normalized_links.add(resolved)
    return normalized_links


def author_from_card(card: str) -> str:
    patterns = [
        r"(?:class|data-testid)=[\"'][^\"']*(?:author|byline)[^\"']*[\"'][^>]*>(.*?)<",
        r"\bby\s+([^<]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, card, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return clean_text(match.group(1)).removeprefix("by ").strip()
    return ""


_EMBEDDED_SCRIPT_OR_STYLE = re.compile(
    r"<(script|style)\b[^>]*>.*?(?:</\1\s*>|\Z)",
    flags=re.IGNORECASE | re.DOTALL,
)


def likely_blocked_response(markup: str) -> bool:
    lowered = markup.lower()
    if "access denied" in lowered or "robot check" in lowered:
        return True
    # Known CAPTCHA widget markers that are only present on real challenge
    # interstitials (hCaptcha/reCAPTCHA widget classes, Cloudflare challenge-
    # specific CSS classes and cookie markers).  These are distinct from
    # benign markers like `data-sitekey` (appears on any form with reCAPTCHA)
    # or `challenge-platform` (appears in Cloudflare script URLs on ordinary
    # proxied pages).  The widget mark-up and challenge text are often in
    # different HTML elements, so a proximity-based regex may fail - treat
    # any of the interstitial-specific markers as a blocking signal on its
    # own.
    if any(
        marker in lowered
        for marker in (
            'class="h-captcha"',
            'class="g-recaptcha"',
            "cf-challenge",
            "cf-chl-",
            "_cf_chl_",
        )
    ):
        return True
    if "captcha" not in lowered:
        return False
    # The challenge copy is read from the page's own text only.  Embedded
    # script and style bodies are configuration, not what the reader is shown:
    # Squarespace ships form-validation strings such as "captchaFailure" and
    # "Captcha validation failed" on every ordinary content page (Issue #787).
    # A real interstitial rendered by script still carries the widget markers
    # checked above.
    markup = _EMBEDDED_SCRIPT_OR_STYLE.sub(" ", markup)
    captcha_challenge = re.compile(
        r"(?:complete|solve|enter|verify|verification|required|challenge|blocked|human|robot)"
        r"[^<]{0,120}captcha|"
        r"captcha[^<]{0,120}"
        r"(?:complete|solve|enter|verify|verification|required|challenge|blocked|human|robot)",
        flags=re.IGNORECASE,
    )
    return bool(captcha_challenge.search(markup))


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def source_page_url(data: Any) -> str:
    if isinstance(data, dict):
        return str(data.get("source_page", "")).strip()
    return ""


def capture_normalized_mentions(
    config_path: Path,
    output_dir: Path,
    source: dict[str, Any],
    mentions: list[dict[str, Any]],
) -> None:
    raw_path = str(source.get("capture_normalized_mentions_file", "")).strip()
    if not raw_path:
        return
    path = output_path(config_path, output_dir, raw_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mentions": mentions}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def capture_live_response(
    config_path: Path,
    output_dir: Path,
    source: dict[str, Any],
    data: dict[str, Any],
    collection_mode: str = "live",
) -> None:
    raw_path = str(source.get("capture_live_response_file", "")).strip()
    if not raw_path:
        return
    detail_route = RankingDetailRoute.from_source(source)
    identity_present = isinstance(data.get("identity_html"), str) and bool(data.get("identity_html"))
    capture_limit = int(source.get("max_response_bytes", DEFAULT_CAPTURE_MAX_RESPONSE_BYTES))
    retained_evidence = (
        RankingPageEvidence.from_mapping(data).sanitized(
            rows_max_bytes=capture_limit,
            identity_max_bytes=capture_limit,
        )
        if detail_route is not None or identity_present
        else None
    )
    captured = {
        "collection_mode": collection_mode,
        "source_url": source.get("url", ""),
        "source_name": source.get("name", ""),
        "source_page": data.get("source_page", source.get("url", "")),
        "html": retained_evidence.rows_html
        if retained_evidence is not None
        else (sanitize_captured_html(str(data.get("html", ""))) if detail_route is not None else data.get("html", "")),
    }
    if detail_route is not None:
        captured["resolved_url"] = data.get("resolved_url", "")
        if retained_evidence is not None and retained_evidence.identity_html:
            captured["identity_html"] = retained_evidence.identity_html
        detail_pages = data.get("detail_pages")
        if isinstance(detail_pages, list):
            captured["detail_pages"] = retain_ranking_detail_pages(detail_pages, detail_route)
        # The rows skipped for an unreadable detail page travel with the capture,
        # so its replay validates the same partial evidence (Issue #1163).
        if data.get(RANKING_DETAIL_SKIPPED_FIELD):
            captured[RANKING_DETAIL_SKIPPED_FIELD] = data[RANKING_DETAIL_SKIPPED_FIELD]
    if contract_family(source) == "bookinfo-weekly-json":
        from autoscout.bookinfo_ranking import validate_bundle

        bookinfo_rows = validate_bundle(data.get("bookinfo_evidence", {}), source)
        captured["contract_family"] = "bookinfo-weekly-json"
        captured["bookinfo_evidence"] = data["bookinfo_evidence"]
        captured["text"] = json.dumps(bookinfo_rows, ensure_ascii=False)
    if contract_family(source) == "ipc-top40-csv":
        from autoscout.ipc_top40_ranking import validate_bundle as validate_ipc_top40_bundle

        ipc_top40_rows = validate_ipc_top40_bundle(data.get("ipc_top40_evidence", {}), source)
        captured["contract_family"] = "ipc-top40-csv"
        captured["ipc_top40_evidence"] = data["ipc_top40_evidence"]
        captured["text"] = json.dumps(ipc_top40_rows, ensure_ascii=False)
    if contract_family(source) == "perlentaucher-daily-digest":
        from autoscout.perlentaucher_digest import retained_capture_fields as perlentaucher_capture_fields

        captured.update(perlentaucher_capture_fields(data, source))
    if contract_family(source) == "kirkus-coming-soon":
        from autoscout.kirkus_listing import retained_capture_fields as kirkus_capture_fields

        captured.update(kirkus_capture_fields(data, source))
    if data.get("reference_date") is not None:
        captured["reference_date"] = data["reference_date"]
    if detail_route is None and retained_evidence is not None and retained_evidence.identity_html:
        captured["identity_html"] = retained_evidence.identity_html
    write_bounded_capture(
        captured,
        max_response_bytes=capture_limit,
        kind=CaptureKind.RANKING,
        destination=output_path(config_path, output_dir, raw_path),
    )


def capture_extraction_response(
    config_path: Path,
    output_dir: Path,
    source: dict[str, Any],
    data: Any,
    collection_mode: str = "live",
) -> None:
    raw_path = str(source.get("capture_extraction_response_file", "")).strip()
    if not raw_path:
        return
    capture_limit = int(source.get("max_response_bytes", DEFAULT_CAPTURE_MAX_RESPONSE_BYTES))
    captured = {
        "collection_mode": collection_mode,
        "source_url": source.get("url", ""),
        "source_name": source.get("name", ""),
        "extraction_method": source.get("extraction_method", ""),
        "response": data,
    }
    write_bounded_capture(
        captured,
        max_response_bytes=capture_limit,
        kind=CaptureKind.EXTRACTION,
        destination=output_path(config_path, output_dir, raw_path),
    )


FreshnessReference = Literal["active_run_date", "page_reference_date"]


def _freshness_reference_date(
    source: dict[str, Any],
    page_data: dict[str, Any],
    *,
    collector_kind: str,
    run_date: str | None,
    freshness_reference: FreshnessReference,
) -> date | None:
    if contract_family(source) == "award-list":
        # The award year is aged by the page-shape check, which refuses a
        # missing reference date itself.
        return lenient_reference_date(
            page_data.get("reference_date") if freshness_reference == "page_reference_date" else run_date
        )
    if contract_family(source) != "ranking-table":
        return None
    if not ranking_freshness_declared(source):
        if not source_week_freshness_declared(source):
            return None
        # A source-note week is aged by the page-shape check, which refuses a
        # missing reference date itself. Reporting it here would put that
        # reason ahead of a page that is simply the wrong page.
        return lenient_reference_date(
            page_data.get("reference_date") if freshness_reference == "page_reference_date" else run_date
        )
    if freshness_reference == "page_reference_date":
        raw_reference_date = page_data.get("reference_date")
        if not isinstance(raw_reference_date, str) or not raw_reference_date.strip():
            raise _collector_error(
                "ranking replay page is missing a valid retained reference_date",
                debug_metadata={
                    "collector_kind": collector_kind,
                    "safe_failure_status": "missing_replay_reference_date",
                },
            )
        try:
            parsed = date.fromisoformat(raw_reference_date.strip())
        except ValueError as exc:
            raise _collector_error(
                "ranking replay page reference_date is not a valid ISO date",
                debug_metadata={
                    "collector_kind": collector_kind,
                    "safe_failure_status": "invalid_replay_reference_date",
                },
            ) from exc
        if parsed.isoformat() != raw_reference_date.strip():
            raise _collector_error(
                "ranking replay page reference_date is not a canonical ISO date",
                debug_metadata={
                    "collector_kind": collector_kind,
                    "safe_failure_status": "invalid_replay_reference_date",
                },
            )
        return parsed
    if not run_date:
        raise _collector_error(
            "ranking collection requires an active run_date",
            debug_metadata={"collector_kind": collector_kind, "safe_failure_status": "invalid_run_date"},
        )
    try:
        return date.fromisoformat(run_date)
    except ValueError as exc:
        raise _collector_error(
            "ranking collection run_date is not a valid ISO date",
            debug_metadata={"collector_kind": collector_kind, "safe_failure_status": "invalid_run_date"},
        ) from exc


def validate_extraction_page_identity(
    source: dict[str, Any],
    page_data: dict[str, Any],
    *,
    collector_kind: str,
    run_date: str | None = None,
    freshness_reference: FreshnessReference = "active_run_date",
) -> dict[str, Any]:
    """Fail closed when an extraction page has drifted.

    ``active_run_date`` is for a real live response.
    ``page_reference_date`` is for a retained file-backed replay page.

    Returns incomplete-coverage debug metadata (possibly {}) for an accepted
    Issue #668 shortfall so collectors can report partial coverage without
    failing the valid rows.
    """
    from autoscout.source_page_identity import BETWEEN_EDITIONS_SAFE_STATUS
    from autoscout.source_page_identity import BETWEEN_EDITIONS_SKIP_PREFIX
    from autoscout.source_page_identity import between_editions_reason
    from autoscout.source_page_identity import validate_page_shape

    if contract_family(source) == "bookinfo-weekly-json":
        from autoscout.bookinfo_ranking import SOURCE_URL
        from autoscout.bookinfo_ranking import BookInfoError
        from autoscout.bookinfo_ranking import validate_bundle

        try:
            bundle = page_data.get("bookinfo_evidence", {})
            validate_bundle(bundle, source)
            if (
                page_data.get("source_page") != SOURCE_URL
                or page_data.get("reference_date") != bundle["reference_date"]
            ):
                raise BookInfoError("bookinfo_evidence_identity", "BookInfo page identity disagrees with its bundle")
            if freshness_reference == "active_run_date" and run_date and bundle["reference_date"] != run_date:
                raise BookInfoError(
                    "bookinfo_evidence_identity", "BookInfo reference date disagrees with active operation"
                )
        except BookInfoError as exc:
            raise _collector_error(str(exc), debug_metadata={"safe_failure_status": exc.reason_code}) from exc
        return {}
    if contract_family(source) == "ipc-top40-csv":
        from autoscout.ipc_top40_ranking import SOURCE_URL as IPC_TOP40_SOURCE_URL
        from autoscout.ipc_top40_ranking import IpcTop40Error
        from autoscout.ipc_top40_ranking import validate_bundle as validate_ipc_top40_bundle

        try:
            bundle = page_data.get("ipc_top40_evidence", {})
            validate_ipc_top40_bundle(bundle, source)
            if (
                page_data.get("source_page") != IPC_TOP40_SOURCE_URL
                or page_data.get("reference_date") != bundle["reference_date"]
            ):
                raise IpcTop40Error(
                    "ipc_top40_evidence_identity", "Independent Press Top 40 page identity disagrees with its bundle"
                )
            if freshness_reference == "active_run_date" and run_date and bundle["reference_date"] != run_date:
                raise IpcTop40Error(
                    "ipc_top40_evidence_identity",
                    "Independent Press Top 40 reference date disagrees with active operation",
                )
        except IpcTop40Error as exc:
            raise _collector_error(str(exc), debug_metadata={"safe_failure_status": exc.reason_code}) from exc
        return {}
    if contract_family(source) == "perlentaucher-daily-digest":
        from autoscout.perlentaucher_digest import PerlentaucherError
        from autoscout.perlentaucher_digest import validate_page_identity as validate_perlentaucher_page_identity

        try:
            validate_perlentaucher_page_identity(
                page_data, source, (run_date or "") if freshness_reference == "active_run_date" else ""
            )
        except PerlentaucherError as exc:
            raise _collector_error(str(exc), debug_metadata={"safe_failure_status": exc.reason_code}) from exc
        return {}
    if contract_family(source) == "kirkus-coming-soon":
        from autoscout.kirkus_listing import KirkusError
        from autoscout.kirkus_listing import coverage_debug as kirkus_coverage_debug
        from autoscout.kirkus_listing import validate_page_identity as validate_kirkus_page_identity

        try:
            validate_kirkus_page_identity(
                page_data, source, (run_date or "") if freshness_reference == "active_run_date" else ""
            )
        except KirkusError as exc:
            raise _collector_error(str(exc), debug_metadata={"safe_failure_status": exc.reason_code}) from exc
        # A pass the site cut short, or a listed book left out (#873), is collected with a named caveat.
        return kirkus_coverage_debug(page_data, source)
    resolved_url = str(page_data.get("resolved_url", "")).strip()
    configured_url = str(source.get("url", "")).strip()
    if contract_family(source) == "ranking-table":
        source_page = str(page_data.get("source_page", "")).strip()
        if normalize_source_identity(source_page) != normalize_source_identity(configured_url):
            raise _collector_error(
                "ranking-table source_page is not the configured source URL",
                debug_metadata={"collector_kind": collector_kind, "safe_failure_status": "wrong_source_page"},
            )
    if resolved_url:
        if source.get("amazon_charts_weekly") is True:
            target_matches = amazon_charts_dated_url_matches(
                configured_url,
                resolved_url,
                chart_path_segment=str(source.get("chart_path_segment", "")),
                expected_page_heading=str(source.get("expected_page_heading", "")),
            )
        else:
            target_matches = normalize_source_identity(resolved_url) == normalize_source_identity(configured_url)
        if not target_matches:
            raise _collector_error(
                "Extraction page resolved to a different source target",
                debug_metadata={
                    "collector_kind": collector_kind,
                    "safe_failure_status": "wrong_target",
                    "resolved_url": resolved_url,
                },
            )
    reference_date = _freshness_reference_date(
        source,
        page_data,
        collector_kind=collector_kind,
        run_date=run_date,
        freshness_reference=freshness_reference,
    )
    off_season = between_editions_reason(source, page_data)
    if off_season:
        raise _collector_error(
            BETWEEN_EDITIONS_SKIP_PREFIX + off_season,
            status="skipped",
            debug_metadata={
                "collector_kind": collector_kind,
                "safe_failure_status": BETWEEN_EDITIONS_SAFE_STATUS,
            },
        )
    error = validate_page_shape(
        source,
        page_data,
        reference_date=reference_date,
    )
    if error:
        raise _collector_error(
            error,
            debug_metadata={
                "collector_kind": collector_kind,
                "safe_failure_status": "page_shape",
            },
        )
    if contract_family(source) == "ranking-table":
        from autoscout.source_page_identity import validate_ranking_detail_pages

        detail_error = validate_ranking_detail_pages(source, page_data)
        if detail_error:
            raise _collector_error(
                detail_error,
                debug_metadata={
                    "collector_kind": collector_kind,
                    "safe_failure_status": "detail_evidence",
                },
            )
        from autoscout.source_page_identity import incomplete_ranking_debug_for_page

        return dict(incomplete_ranking_debug_for_page(source, page_data))
    return {}


# Contracts that report a book once and keep a seen-item index of the ISBNs already given to the extractor.
SEEN_ITEM_INDEX_FAMILIES = frozenset({"perlentaucher-daily-digest", "kirkus-coming-soon"})


def seen_index_capture_has_no_rows(source: dict[str, Any], page_data: Mapping[str, Any]) -> bool:
    """True when a valid capture of a seen-item source gave the extractor no row, so an empty extraction is correct."""
    if contract_family(source) not in SEEN_ITEM_INDEX_FAMILIES:
        return False
    from autoscout.extraction_completeness import capture_listing_row_count

    return capture_listing_row_count(source, page_data) == 0


def record_seen_index_items(
    config_path: Path,
    source: dict[str, Any],
    page_data: Mapping[str, Any],
    mentions: list[dict[str, Any]],
    refused_entry_count: int,
    run_date: str,
) -> dict[str, Any]:
    """Record the rows an extraction of a seen-item source's capture decided, once its mentions are accepted.

    Every mention is first built as the Source Mention the collection stage
    will build from it. A mention that stage would refuse fails here, before
    the index moves, so a failed collection marks nothing as seen.

    Returns debug metadata, empty for every other source and for a source that
    declares no seen-item index, such as a replay overlay.
    """
    family = contract_family(source)
    if family not in SEEN_ITEM_INDEX_FAMILIES or not str(source.get("seen_guids_file", "")).strip():
        return {}
    from autoscout.source_mentions import SourceMention

    for mention in mentions:
        SourceMention.from_collector(source, mention, run_date)
    accepted = {str(mention.get("isbn", "")) for mention in mentions}
    if family == "kirkus-coming-soon":
        from autoscout.kirkus_listing import KirkusError
        from autoscout.kirkus_listing import record_seen_items as record_kirkus_seen_items

        try:
            recorded = record_kirkus_seen_items(config_path, source, page_data, accepted)
            return {"kirkus_new_seen_item_count": recorded}
        except KirkusError as exc:
            raise _collector_error(str(exc), debug_metadata={"safe_failure_status": exc.reason_code}) from exc
    from autoscout.perlentaucher_digest import PerlentaucherError
    from autoscout.perlentaucher_digest import record_seen_items

    try:
        recorded = record_seen_items(config_path, source, page_data, accepted, refused_entry_count)
        return {"perlentaucher_new_seen_item_count": recorded}
    except PerlentaucherError as exc:
        raise _collector_error(str(exc), debug_metadata={"safe_failure_status": exc.reason_code}) from exc


def resolve_config_path(config_path: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return config_path.parent / path


def output_path(config_path: Path, output_dir: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    if path.parent == Path("."):
        return output_dir / path
    return config_path.parent / path


def collectors_pkg():
    """Return the ``autoscout.source_collectors`` package module.

    Patch seam kept from the pre-split single module: tests monkeypatch
    ``urlopen``, ``fetch_agent_source_page``, ``live_agent_extraction_response``
    and ``REAL_AGENT_EXTRACTION_TRANSPORT`` on the package. Internal call
    sites resolve those names through this function at call time so the
    package attribute stays the single point of truth.
    """
    import autoscout.source_collectors as pkg

    return pkg
