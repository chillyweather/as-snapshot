"""Pure feasibility-policy seams for source onboarding evidence evaluation.

``evaluate_audible_chart_feasibility`` and ``evaluate_amazon_charts_feasibility``
are the configured-source policy seams for Audible/Amazon chart onboarding;
``evaluate_captured_source_evidence`` (Issue #334) is the source-independent
policy seam that evaluates already-captured Source Candidate evidence as a
pure, deterministic function: no network, model, configuration, weekly state,
or Git access. All seams share the extended :class:`FeasibilityVerdict` shape.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import replace
from datetime import date
from urllib.parse import urlsplit

from autoscout.http_evidence import HttpEvidencePolicy
from autoscout.http_evidence import HttpStatusEvidence
from autoscout.http_evidence import declared_head_404_fallback
from autoscout.ranking_author_policy import ranking_row_author_required
from autoscout.ranking_detail_route import RankingDetailRoute
from autoscout.ranking_detail_route import ranking_detail_fields_from_html
from autoscout.ranking_detail_route import ranking_detail_requested_fields
from autoscout.ranking_detail_route import ranking_detail_route_declared
from autoscout.ranking_evidence import RankingPageEvidence
from autoscout.ranking_freshness import ranking_undated_declared
from autoscout.ranking_request import ranking_request_from_source
from autoscout.source_collectors.base import CONTRACT_FAMILIES
from autoscout.source_collectors.base import contract_family as configured_contract_family
from autoscout.source_collectors.base import likely_blocked_response
from autoscout.source_page_identity import ACTION_LABELS
from autoscout.source_page_identity import AUTHOR_MARKER_RE
from autoscout.source_page_identity import LINK_KIND_BOOK
from autoscout.source_page_identity import SourceListEntry
from autoscout.source_page_identity import amazon_charts_dated_url_matches
from autoscout.source_page_identity import amazon_charts_entries
from autoscout.source_page_identity import amazon_charts_week_url
from autoscout.source_page_identity import audible_chart_entries
from autoscout.source_page_identity import audible_de_entry_author_names
from autoscout.source_page_identity import is_cover_label
from autoscout.source_page_identity import normalize_visible_text
from autoscout.source_page_identity import page_title_text
from autoscout.source_page_identity import parse_entry_scope
from autoscout.source_page_identity import parse_heading_texts
from autoscout.source_page_identity import parse_hidden_heading_texts
from autoscout.source_page_identity import parse_visible_page_identity
from autoscout.source_page_identity import ranking_incomplete_row_count
from autoscout.source_page_identity import ranking_page_identity
from autoscout.source_page_identity import ranking_raw_row_count_error
from autoscout.source_page_identity import ranking_row_bounds
from autoscout.source_page_identity import ranking_row_diagnostics
from autoscout.source_page_identity import ranking_rows
from autoscout.source_page_identity import ranking_rows_after_title_exclusions
from autoscout.source_page_identity import server_rendered_list_entries
from autoscout.source_page_identity import validate_entry_scope_selector
from autoscout.source_page_identity import validate_page_shape
from autoscout.source_page_identity import validate_ranking_detail_pages
from autoscout.source_page_identity import validate_ranking_detail_row_urls
from autoscout.source_proposals import normalize_source_identity

OUTCOME_ACCEPT = "accept"
OUTCOME_DEFER = "defer"
OUTCOME_REJECT = "reject"

REASON_OK = "ok"
REASON_JS_ONLY = "js_only"
REASON_HEAD_DENIED = "head_denied"
REASON_HTTP_ERROR = "http_error"
REASON_EMPTY_RESPONSE = "empty_response"
REASON_BLOCKED_RESPONSE = "blocked_response"
REASON_UNRELATED_REDIRECT = "unrelated_redirect"
REASON_MISSING_HEADING = "missing_heading"
REASON_VISIBLE_TITLE_SELECTOR = "visible_title_selector"
REASON_ENTRY_SCOPE_MISSING = "entry_scope_missing"
REASON_ENTRY_SCOPE_AMBIGUOUS = "entry_scope_ambiguous"
REASON_ENTRY_SCOPE_HIDDEN = "entry_scope_hidden"
REASON_ENTRY_SCOPE_CHROME = "entry_scope_chrome"
REASON_ENTRY_SCOPE_EMPTY = "entry_scope_empty"
REASON_ENTRY_SCOPE_NO_CANDIDATE_ENTRIES = "entry_scope_no_candidate_entries"
REASON_ENTRY_SCOPE_UNLINKED_ENTRIES = "entry_scope_unlinked_entries"
REASON_ENTRY_SCOPE_MISSING_AUTHORS = "entry_scope_missing_authors"
REASON_UNLINKED_ENTRIES = "unlinked_entries"
REASON_UNBOUNDED_ENTRIES = "unbounded_entries"
REASON_MISSING_AUTHORS = "missing_authors"
REASON_QUERY_PAGINATION = "query_pagination"
REASON_PAGE_SHAPE = "page_shape"
REASON_ENTRY_SHAPE = "entry_shape"
REASON_LANDING_EVIDENCE = "landing_evidence"
REASON_LANDING_LINK = "landing_link"
REASON_RANKING_ROUTE = "ranking_route"
REASON_RANKING_PAGE_IDENTITY = "ranking_page_identity"
REASON_RANKING_ROW_DECLARATION = "ranking_row_declaration"
REASON_RANKING_ROW_SHAPE = "ranking_row_shape"
REASON_RANKING_UNLINKED_ROWS = "ranking_unlinked_rows"
REASON_RANKING_NO_QUALIFYING_ROWS = "ranking_no_qualifying_rows"
REASON_RANKING_EVIDENCE_URL = "ranking_evidence_url"
REASON_RETAINED_SUMMARY_INCONSISTENT = "retained_summary_inconsistent"

# Actual per-entry containers for the source-independent probe. A page-level
# wrapper such as ``section`` can hold several direct anchors and is not one
# bounded entry per book, so it does not count; an entry must sit in a
# per-entry container (li/article/tr) to be bounded (Issue #334).
_PER_ENTRY_BLOCK_TAGS = frozenset({"li", "article", "tr"})
MAX_SAMPLES = 3

_VISIBLE_TITLE_SELECTOR_MESSAGES = {
    "no_match": "visible title selector matched no element",
    "multiple_matches": "visible title selector matched more than one element",
    "hidden": "visible title selector matched a hidden element",
    "chrome": "visible title selector matched an element inside page-level chrome",
    "empty": "visible title selector matched an element with no text",
}
_ENTRY_SCOPE_MESSAGES = {
    "entry_scope_missing": "entry scope selector matched no scope",
    "entry_scope_ambiguous": "entry scope selector matched more than one scope",
    "entry_scope_hidden": "entry scope selector matched a hidden scope",
    "entry_scope_chrome": "entry scope selector matched a scope inside page-level chrome",
    "entry_scope_empty": "entry scope selector matched a scope with no visible text",
    "entry_scope_no_candidate_entries": "entry scope selector matched no visible candidate entry containers",
}


@dataclass(frozen=True)
class EntrySample:
    """One grounded entry sample exposed by an accepted verdict.

    Linked-list samples use title, author, and book URL.  Ranking-table
    samples use the shared page URL and may also carry the bounded rank,
    identifier, category, and reporting-period metadata.
    """

    title: str
    author: str
    url: str
    rank: str = ""
    identifier: str = ""
    category: str = ""
    reporting_period: str = ""


@dataclass(frozen=True)
class FeasibilityVerdict:
    accepted: bool
    reason: str
    use_get_fallback: bool = False
    outcome: str | None = None
    reason_code: str = ""
    detected_heading: str = ""
    bounded_entry_count: int = 0
    raw_row_count: int = 0
    samples: tuple[EntrySample, ...] = ()
    contract_family: str = ""
    ranking_category: str = ""
    reporting_period: str = ""
    ranking_diagnostics: Mapping[str, object] | None = None
    ranking_declaration_diagnostics: Mapping[str, object] | None = None
    structural_diagnostics: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        """Enforce the verdict invariant: accepted must match the outcome.

        *outcome* is the structured source of truth: ``accept`` iff
        *accepted*. Legacy positional constructions
        (``FeasibilityVerdict(False, reason)`` and
        ``FeasibilityVerdict(True, reason, fallback)``) leave *outcome* at
        its None sentinel and derive it from *accepted*, so they keep working
        with consistent values. Any explicit contradiction raises
        ``ValueError`` instead of silently producing an inconsistent
        verdict."""
        if self.outcome is None:
            object.__setattr__(self, "outcome", OUTCOME_ACCEPT if self.accepted else OUTCOME_REJECT)
        elif self.accepted != (self.outcome == OUTCOME_ACCEPT):
            raise ValueError(f"contradictory verdict: accepted={self.accepted} does not match outcome={self.outcome!r}")


def _retained_sample_mapping(sample: EntrySample) -> dict[str, str]:
    """Serialize one verdict sample in the retained-evidence shape."""
    value = {"title": sample.title, "author": sample.author, "url": sample.url}
    for field in ("rank", "identifier", "category", "reporting_period"):
        field_value = getattr(sample, field)
        if field_value:
            value[field] = str(field_value)
    return value


def retained_summary_samples(verdict: FeasibilityVerdict) -> list[dict[str, str]]:
    """Return the exact JSON sample shape derived from a fresh verdict."""
    return [_retained_sample_mapping(sample) for sample in verdict.samples]


def validate_retained_summary(evidence: Mapping[str, object], verdict: FeasibilityVerdict) -> tuple[str, str] | None:
    """Validate optional retained summary fields against a fresh verdict.

    The return value is ``(reason_code, reason)`` for the first deterministic
    mismatch, or ``None`` when every declared field agrees. Missing optional
    fields and a blank outcome remain valid for legacy captures. The sample
    shape is shared by preview, replay generation, and scaffolding.
    """
    declared_outcome = evidence.get("outcome")
    if declared_outcome is not None and str(declared_outcome).strip() and declared_outcome != verdict.outcome:
        reason = (
            f"probe evidence declares non-accepted outcome: {declared_outcome}"
            if verdict.outcome == OUTCOME_ACCEPT
            else "probe evidence outcome is stale or inconsistent with its HTML"
        )
        return REASON_RETAINED_SUMMARY_INCONSISTENT, reason

    if "detected_heading" in evidence and str(evidence["detected_heading"]).strip():
        if str(evidence["detected_heading"]).strip() != verdict.detected_heading:
            return (
                REASON_RETAINED_SUMMARY_INCONSISTENT,
                "probe evidence heading is stale or inconsistent with its HTML",
            )

    if "bounded_entry_count" in evidence:
        count = evidence["bounded_entry_count"]
        if isinstance(count, bool) or not isinstance(count, int) or count != verdict.bounded_entry_count:
            return (
                REASON_RETAINED_SUMMARY_INCONSISTENT,
                "probe evidence entry count is stale or inconsistent with its HTML",
            )

    if "samples" in evidence and evidence["samples"] != retained_summary_samples(verdict):
        return (
            REASON_RETAINED_SUMMARY_INCONSISTENT,
            "probe evidence samples are stale or inconsistent with its HTML",
        )
    return None


def _verdict(
    outcome: str,
    reason_code: str,
    reason: str,
    *,
    use_get_fallback: bool = False,
    detected_heading: str = "",
    bounded_entry_count: int = 0,
    raw_row_count: int = 0,
    samples: tuple[EntrySample, ...] = (),
    contract_family: str = "",
    ranking_category: str = "",
    reporting_period: str = "",
    ranking_diagnostics: Mapping[str, object] | None = None,
    structural_diagnostics: Mapping[str, object] | None = None,
) -> FeasibilityVerdict:
    """Build a verdict with consistent accepted/outcome/reason_code fields.

    ``outcome`` is the structured source of truth; ``accepted`` is derived
    from it by the dataclass invariant."""
    return FeasibilityVerdict(
        accepted=outcome == OUTCOME_ACCEPT,
        reason=reason,
        use_get_fallback=use_get_fallback,
        outcome=outcome,
        reason_code=reason_code,
        detected_heading=detected_heading,
        bounded_entry_count=bounded_entry_count,
        raw_row_count=raw_row_count,
        samples=samples,
        contract_family=contract_family,
        ranking_category=ranking_category,
        reporting_period=reporting_period,
        ranking_diagnostics=ranking_diagnostics,
        structural_diagnostics=structural_diagnostics,
    )


def _legacy_http_status_evidence(*, head_status: int | None, get_status: int | None) -> HttpStatusEvidence:
    """Adapt historical status arguments at the external GET compatibility seam."""
    return HttpStatusEvidence.get_with_head(head_status=head_status, body_status=get_status)


def validate_http_status_evidence(
    evidence: HttpStatusEvidence | None = None,
    *,
    head_status: int | None = None,
    get_status: int | None = None,
) -> tuple[FeasibilityVerdict | None, bool]:
    """Validate structured HTTP status evidence and compute the fallback flag.

    The keyword status arguments remain a compatibility wrapper for callers at
    the public policy seam. Internal evaluators pass ``HttpStatusEvidence``.
    """
    evidence = evidence or _legacy_http_status_evidence(head_status=head_status, get_status=get_status)
    if evidence.policy is HttpEvidencePolicy.DECLARED_POST_WITHOUT_HEAD:
        if evidence.body_status != 200:
            return (
                _verdict(
                    OUTCOME_REJECT,
                    REASON_HTTP_ERROR,
                    f"{evidence.method} must return 200, got {evidence.body_status}",
                ),
                False,
            )
        return None, False
    fallback = (
        head_fallback_allowed(evidence.head_status, allow_head_404=evidence.allow_head_404)
        if evidence.head_status is not None
        else False
    )
    if evidence.head_status is None:
        return _verdict(OUTCOME_REJECT, REASON_HEAD_DENIED, "HEAD evidence is missing"), fallback
    if evidence.head_status != 200 and not fallback:
        return (
            _verdict(OUTCOME_REJECT, REASON_HEAD_DENIED, f"HEAD {evidence.head_status} does not permit collection"),
            fallback,
        )
    if evidence.body_status is None:
        return (
            _verdict(OUTCOME_REJECT, REASON_HTTP_ERROR, "GET evidence is missing", use_get_fallback=fallback),
            fallback,
        )
    if evidence.body_status != 200:
        return (
            _verdict(
                OUTCOME_REJECT,
                REASON_HTTP_ERROR,
                f"GET must return 200, got {evidence.body_status}",
                use_get_fallback=fallback,
            ),
            fallback,
        )
    return None, fallback


def head_fallback_allowed(status: int, *, allow_head_404: bool = False) -> bool:
    """Default method fallback, with a separately declared ranking-source 404 exception."""
    return status in {405, 501} or (status == 404 and allow_head_404 is True)


def audible_head_fallback_allowed(status: int) -> bool:
    """Backward-compatible alias of :func:`head_fallback_allowed`."""
    return head_fallback_allowed(status)


def audible_entries_expose_primary_author(
    html_content: str, entries: list[tuple[str, str, str]], author_marker: str
) -> bool:
    """Return True when every chart entry exposes a nonblank primary author.

    The English marker default is ``by:``, rendered inside the bounded entry
    block.  The German Audible chart pages render the author in a ``Von:``
    flyout after the product link instead, so the German variant checks the
    per-entry ``Von:`` marker windows keyed by raw ``/pd/`` path."""
    marker = normalize_visible_text(author_marker)
    if marker in {"von", "von:"}:
        author_windows = audible_de_entry_author_names(html_content)
        return all(
            any(name.strip() for name in author_windows.get(urlsplit(href).path, []))
            for href, _title, _context in entries
        )
    return all(
        marker in normalize_visible_text(context) and bool(normalize_visible_text(context).split(marker, 1)[1].strip())
        for _href, _title, context in entries
    )


def evaluate_audible_chart_feasibility(
    source: dict[str, object],
    *,
    head_status: int | None = None,
    get_status: int | None = None,
    resolved_url: str,
    html: str,
    author_marker: str = "by:",
    http_evidence: HttpStatusEvidence | None = None,
) -> FeasibilityVerdict:
    """Evaluate captured HTTP/page evidence without performing network requests.

    *author_marker* selects the primary-author marker rendered by the chart
    locale: ``by:`` for Audible.com and ``von:`` for Audible.de.  The default
    keeps existing Audible.com behavior unchanged."""
    status_evidence = http_evidence or _legacy_http_status_evidence(head_status=head_status, get_status=get_status)
    if status_evidence.allow_head_404:
        status_evidence = replace(status_evidence, allow_head_404=False)
    error, fallback = validate_http_status_evidence(status_evidence)
    if error is not None:
        return error
    configured_url = str(source.get("url", ""))
    configured = urlsplit(configured_url)
    resolved = urlsplit(resolved_url)
    if (configured.scheme, configured.netloc, configured.path) != (resolved.scheme, resolved.netloc, resolved.path):
        return _verdict(
            OUTCOME_REJECT, REASON_UNRELATED_REDIRECT, "GET resolved to an unrelated target", use_get_fallback=fallback
        )
    if configured.query or resolved.query:
        return _verdict(
            OUTCOME_REJECT,
            REASON_QUERY_PAGINATION,
            "chart feasibility is base-page-only without query pagination",
            use_get_fallback=fallback,
        )
    if likely_blocked_response(html):
        return _verdict(
            OUTCOME_REJECT, REASON_BLOCKED_RESPONSE, "GET returned a CAPTCHA or bot wall", use_get_fallback=fallback
        )
    shape_error = validate_page_shape(
        dict(source), {"source_page": configured_url, "resolved_url": resolved_url, "html": html}
    )
    if shape_error:
        return _verdict(OUTCOME_REJECT, REASON_PAGE_SHAPE, shape_error, use_get_fallback=fallback)
    entries = audible_chart_entries(html, configured_url)
    if len(entries) != 20 or any(not title.strip() for _href, title, _context in entries):
        return _verdict(
            OUTCOME_REJECT, REASON_ENTRY_SHAPE, "GET must expose 20 titled direct /pd/ links", use_get_fallback=fallback
        )
    if not audible_entries_expose_primary_author(html, entries, author_marker):
        return _verdict(
            OUTCOME_REJECT,
            REASON_MISSING_AUTHORS,
            "every chart entry must expose a nonblank primary author",
            use_get_fallback=fallback,
        )
    return _verdict(OUTCOME_ACCEPT, REASON_OK, "HEAD and plain GET chart evidence accepted", use_get_fallback=fallback)


def evaluate_amazon_charts_feasibility(
    source: dict[str, object],
    *,
    head_status: int | None = None,
    get_status: int | None = None,
    resolved_url: str,
    html: str,
    landing_html: str,
    author_marker: str | None = None,
    reference_date: date,
    http_evidence: HttpStatusEvidence | None = None,
) -> FeasibilityVerdict:
    """Evaluate Amazon Charts landing and dated-page evidence.

    *landing_html* is the recorded landing-page capture and is mandatory
    evidence: an empty capture rejects, and the capture must resolve a
    recent dated link for the configured chart path segment (Issue #378).
    *author_marker* selects the per-entry primary-author marker rendered by
    the chart locale: ``by`` for the English charts and ``von`` for the
    German charts. It defaults to the configured ``author_marker`` and then
    to the existing English marker ``by``. *reference_date* pins the week
    recency window; the seam stays pure and never consults the clock."""
    status_evidence = http_evidence or _legacy_http_status_evidence(head_status=head_status, get_status=get_status)
    error, fallback = validate_http_status_evidence(status_evidence)
    if error is not None:
        return error
    configured = urlsplit(str(source.get("url", "")))
    if not configured.netloc:
        return _verdict(
            OUTCOME_REJECT, REASON_UNRELATED_REDIRECT, "GET resolved to an unrelated target", use_get_fallback=fallback
        )
    chart_segment = str(source.get("chart_path_segment", "")).strip()
    if not amazon_charts_dated_url_matches(
        str(source.get("url", "")),
        resolved_url,
        chart_path_segment=chart_segment,
        expected_page_heading=str(source.get("expected_page_heading", "")),
    ):
        return _verdict(
            OUTCOME_REJECT,
            REASON_UNRELATED_REDIRECT,
            "Amazon Charts resolved URL is not a dated chart for the configured segment",
            use_get_fallback=fallback,
        )
    if configured.query or configured.fragment:
        return _verdict(
            OUTCOME_REJECT,
            REASON_QUERY_PAGINATION,
            "Amazon Charts landing URL must not contain query or fragment",
            use_get_fallback=fallback,
        )
    # The recorded landing-page capture is mandatory evidence: an empty or
    # missing capture must never be accepted on the dated page alone.
    if not landing_html.strip():
        return _verdict(
            OUTCOME_REJECT,
            REASON_LANDING_EVIDENCE,
            "Amazon Charts landing page evidence is required but was not provided",
            use_get_fallback=fallback,
        )
    try:
        amazon_charts_week_url(
            landing_html,
            str(source.get("url", "")),
            chart_path_segment=chart_segment,
            expected_page_heading=str(source.get("expected_page_heading", "")),
            reference_date=reference_date,
        )
    except ValueError:
        return _verdict(
            OUTCOME_REJECT,
            REASON_LANDING_LINK,
            "Amazon Charts landing page has no recent dated link for the configured segment",
            use_get_fallback=fallback,
        )
    if likely_blocked_response(html):
        return _verdict(
            OUTCOME_REJECT, REASON_BLOCKED_RESPONSE, "GET returned a CAPTCHA or bot wall", use_get_fallback=fallback
        )
    expected_heading = str(source.get("expected_page_heading", "")).strip()
    if not expected_heading or normalize_visible_text(expected_heading) not in page_title_text(html).casefold():
        return _verdict(
            OUTCOME_REJECT,
            REASON_MISSING_HEADING,
            "GET page title is not the configured chart heading",
            use_get_fallback=fallback,
        )
    shape_error = validate_page_shape(
        dict(source), {"source_page": resolved_url, "resolved_url": resolved_url, "html": html}
    )
    if shape_error:
        return _verdict(OUTCOME_REJECT, REASON_PAGE_SHAPE, shape_error, use_get_fallback=fallback)
    entries = amazon_charts_entries(html, resolved_url)
    if len(entries) != 20 or any(not title.strip() for _href, title, _context in entries):
        return _verdict(
            OUTCOME_REJECT,
            REASON_ENTRY_SHAPE,
            "GET must expose 20 titled direct Amazon /dp/ links",
            use_get_fallback=fallback,
        )
    # The marker is matched against casefolded visible text, so the
    # configured marker is casefolded too: a configured ``Von`` or ``By``
    # must work exactly like its lowercase spelling.
    marker = str(author_marker or source.get("author_marker") or "by").strip().casefold()
    if not marker or any(
        re.search(rf"\b{re.escape(marker)}\s+\S+", normalize_visible_text(context)) is None
        for _href, _title, context in entries
    ):
        return _verdict(
            OUTCOME_REJECT,
            REASON_MISSING_AUTHORS,
            "every chart entry must expose a nonblank author",
            use_get_fallback=fallback,
        )
    return _verdict(
        OUTCOME_ACCEPT, REASON_OK, "HEAD and plain GET Amazon Charts evidence accepted", use_get_fallback=fallback
    )


def _ranking_declarations_present(source: Mapping[str, object]) -> bool:
    rank_source = str(source.get("ranking_rank_source", "selector")).strip().casefold()
    # The reporting-period pattern is required unless the page prints no date
    # and the source declares that, per ADR 0012.  The author selector stays
    # required even when a row may carry no author, per ADR 0013: an absent
    # author is allowed, an unexamined one is not.
    required = (
        "ranking_row_selector",
        "ranking_title_selector",
        "ranking_author_selector",
        "ranking_category_marker",
        *(() if ranking_undated_declared(source) else ("ranking_reporting_period_pattern",)),
    )
    if rank_source not in {"selector", "document_order"}:
        return False
    if rank_source == "selector" and not str(source.get("ranking_rank_selector", "")).strip():
        return False
    if not all(str(source.get(field, "")).strip() for field in required):
        return False
    if "isbn" in ranking_detail_requested_fields(source):
        return bool(str(source.get("ranking_detail_link_selector", "")).strip())
    return any(
        str(source.get(field, "")).strip()
        for field in (
            "ranking_identifier_selector",
            "ranking_identifier_attribute",
            "ranking_identifier_pattern",
        )
    )


def evaluate_ranking_table_feasibility(
    source: Mapping[str, object],
    *,
    head_status: int | None = None,
    get_status: int | None = None,
    resolved_url: str,
    html: str,
    reference_date: date | None = None,
    source_page: str = "",
    detail_pages: list[Mapping[str, object]] | None = None,
    identity_html: str = "",
    http_evidence: HttpStatusEvidence | None = None,
) -> FeasibilityVerdict:
    """Evaluate a bounded, declarative ranking-table page without network I/O.

    Rows may be unlinked.  Every accepted row must expose title, primary
    author, and identifier through selectors that resolve inside that same
    row.
    Rank comes from the declared row field, or from one-based bounded row
    order when the ranking contract declares ``document_order``.
    The page heading supplies the category and reporting period; these values
    are never inferred from a row or an extraction response.
    """
    status_evidence = http_evidence or HttpStatusEvidence.get_with_head(
        head_status=head_status, body_status=get_status, allow_head_404=declared_head_404_fallback(source)
    )
    if status_evidence.allow_head_404 and not declared_head_404_fallback(source):
        status_evidence = replace(status_evidence, allow_head_404=False)
    error, fallback = validate_http_status_evidence(status_evidence)
    if error is not None:
        return error
    configured_url = str(source.get("url", "")).strip()
    if not _same_resolved_page(configured_url, resolved_url or configured_url):
        return _verdict(
            OUTCOME_REJECT,
            REASON_UNRELATED_REDIRECT,
            "GET resolved to an unrelated target",
            use_get_fallback=fallback,
            contract_family="ranking-table",
        )
    expected_path = str(source.get("ranking_expected_path") or source.get("expected_url_path") or "").strip()
    expected_evidence_url = "detail_page" if ranking_detail_route_declared(source) else "source_page"
    if str(source.get("ranking_evidence_url", "")).strip() != expected_evidence_url:
        return _verdict(
            OUTCOME_REJECT,
            REASON_RANKING_EVIDENCE_URL,
            f"ranking-table evidence_url must be the declared {expected_evidence_url}",
            use_get_fallback=fallback,
            contract_family="ranking-table",
        )
    if source_page and not _same_resolved_page(configured_url, source_page):
        return _verdict(
            OUTCOME_REJECT,
            REASON_RANKING_EVIDENCE_URL,
            "ranking-table source_page does not match the configured source URL",
            use_get_fallback=fallback,
            contract_family="ranking-table",
        )
    if expected_path and urlsplit(resolved_url).path.rstrip("/") != expected_path.rstrip("/"):
        return _verdict(
            OUTCOME_REJECT,
            REASON_RANKING_ROUTE,
            "ranking page path is not the declared first-party category route",
            use_get_fallback=fallback,
            contract_family="ranking-table",
        )
    if not html.strip():
        return _verdict(
            OUTCOME_REJECT,
            REASON_EMPTY_RESPONSE,
            "GET returned an empty response body",
            use_get_fallback=fallback,
            contract_family="ranking-table",
        )
    if likely_blocked_response(html) or _generic_blocked_response(html):
        return _verdict(
            OUTCOME_REJECT,
            REASON_BLOCKED_RESPONSE,
            "GET returned a CAPTCHA or bot wall",
            use_get_fallback=fallback,
            contract_family="ranking-table",
        )
    date_format = str(source.get("ranking_reporting_period_date_format", "")).strip()
    if date_format and reference_date is None:
        return _verdict(
            OUTCOME_REJECT,
            REASON_RANKING_PAGE_IDENTITY,
            "ranking reporting-period freshness requires a probe reference date",
            use_get_fallback=fallback,
            contract_family="ranking-table",
        )
    identity = ranking_page_identity(html, dict(source), reference_date=reference_date, identity_html=identity_html)
    if identity is None:
        return _verdict(
            OUTCOME_REJECT,
            REASON_RANKING_PAGE_IDENTITY,
            "ranking page does not expose the declared visible category and reporting period",
            use_get_fallback=fallback,
            contract_family="ranking-table",
        )
    if not _ranking_declarations_present(source):
        return _verdict(
            OUTCOME_REJECT,
            REASON_RANKING_ROW_DECLARATION,
            "ranking-table contract requires row, title, author, identifier, category, reporting, and rank-source declarations",
            use_get_fallback=fallback,
            detected_heading=identity.heading,
            ranking_category=identity.category,
            reporting_period=identity.reporting_period,
            contract_family="ranking-table",
        )
    detail_route = ranking_detail_route_declared(source)
    try:
        route = RankingDetailRoute.from_source(source) if detail_route else None
        rows = ranking_rows(html, dict(source), detail_route=route)
    except (TypeError, ValueError):
        rows = []
    raw_row_count = len(rows)
    try:
        row_count_error = ranking_raw_row_count_error(source, raw_row_count)
    except ValueError as exc:
        return _verdict(
            OUTCOME_REJECT,
            REASON_RANKING_ROW_DECLARATION,
            str(exc),
            use_get_fallback=fallback,
            detected_heading=identity.heading,
            raw_row_count=raw_row_count,
            ranking_category=identity.category,
            reporting_period=identity.reporting_period,
            contract_family="ranking-table",
        )
    if row_count_error:
        return _verdict(
            OUTCOME_REJECT,
            REASON_RANKING_ROW_SHAPE,
            row_count_error,
            use_get_fallback=fallback,
            detected_heading=identity.heading,
            raw_row_count=raw_row_count,
            ranking_category=identity.category,
            reporting_period=identity.reporting_period,
            contract_family="ranking-table",
        )
    if detail_route:
        try:
            exact_rows, minimum_rows, maximum_rows = ranking_row_bounds(source)
        except ValueError as exc:
            return _verdict(
                OUTCOME_REJECT,
                REASON_RANKING_ROW_DECLARATION,
                str(exc),
                use_get_fallback=fallback,
                detected_heading=identity.heading,
                raw_row_count=raw_row_count,
                ranking_category=identity.category,
                reporting_period=identity.reporting_period,
                contract_family="ranking-table",
            )
        if exact_rows is not None and raw_row_count != exact_rows:
            if not ranking_incomplete_row_count(source, raw_row_count):
                return _verdict(
                    OUTCOME_REJECT,
                    REASON_RANKING_ROW_SHAPE,
                    f"ranking page exposes {raw_row_count} raw rows; expected exactly {exact_rows}",
                    use_get_fallback=fallback,
                    detected_heading=identity.heading,
                    raw_row_count=raw_row_count,
                    ranking_category=identity.category,
                    reporting_period=identity.reporting_period,
                    contract_family="ranking-table",
                )
        if minimum_rows is not None and raw_row_count < minimum_rows:
            return _verdict(
                OUTCOME_REJECT,
                REASON_RANKING_ROW_SHAPE,
                f"ranking page exposes {raw_row_count} raw rows; expected at least {minimum_rows}",
                use_get_fallback=fallback,
                detected_heading=identity.heading,
                raw_row_count=raw_row_count,
                ranking_category=identity.category,
                reporting_period=identity.reporting_period,
                contract_family="ranking-table",
            )
        if maximum_rows is not None and raw_row_count > maximum_rows:
            return _verdict(
                OUTCOME_REJECT,
                REASON_RANKING_ROW_SHAPE,
                f"ranking page exposes {raw_row_count} raw rows; expected at most {maximum_rows}",
                use_get_fallback=fallback,
                detected_heading=identity.heading,
                raw_row_count=raw_row_count,
                ranking_category=identity.category,
                reporting_period=identity.reporting_period,
                contract_family="ranking-table",
            )
    if not rows:
        return _verdict(
            OUTCOME_REJECT,
            REASON_RANKING_UNLINKED_ROWS,
            "ranking page exposes no bounded declarative rows",
            use_get_fallback=fallback,
            detected_heading=identity.heading,
            ranking_category=identity.category,
            reporting_period=identity.reporting_period,
            contract_family="ranking-table",
        )
    if detail_route:
        cap_error = (
            route.validate_page_count(len(ranking_rows_after_title_exclusions(rows, source, route=route)))
            if route is not None
            else ""
        )
        if cap_error:
            return _verdict(
                OUTCOME_REJECT,
                REASON_RANKING_ROW_SHAPE,
                cap_error,
                use_get_fallback=fallback,
                detected_heading=identity.heading,
                raw_row_count=raw_row_count,
                ranking_category=identity.category,
                reporting_period=identity.reporting_period,
                contract_family="ranking-table",
            )
        row_url_error = validate_ranking_detail_row_urls(rows)
        if row_url_error:
            return _verdict(
                OUTCOME_REJECT,
                REASON_RANKING_ROW_SHAPE,
                row_url_error,
                use_get_fallback=fallback,
                detected_heading=identity.heading,
                raw_row_count=raw_row_count,
                ranking_category=identity.category,
                reporting_period=identity.reporting_period,
                contract_family="ranking-table",
            )
    detail_page_by_url: dict[str, Mapping[str, object]] = {}
    if detail_route and detail_pages:
        detail_page_data = {"source_page": configured_url, "html": html, "detail_pages": detail_pages}
        detail_error = validate_ranking_detail_pages(source, detail_page_data, route=route)
        if detail_error:
            return _verdict(
                OUTCOME_REJECT,
                REASON_RANKING_ROW_SHAPE,
                detail_error,
                use_get_fallback=fallback,
                detected_heading=identity.heading,
                raw_row_count=raw_row_count,
                ranking_category=identity.category,
                reporting_period=identity.reporting_period,
                contract_family="ranking-table",
            )
        detail_page_by_url = {
            normalize_source_identity(str(page.get("detail_url", "")).strip()): page
            for page in detail_pages
            if str(page.get("detail_url", "")).strip()
        }
    detail_fields = route.requested_fields if route is not None else ranking_detail_requested_fields(source)
    if detail_route:
        assert route is not None
    eligible_rows = ranking_rows_after_title_exclusions(rows, source, route=route)
    eligible_row_ids = {id(row) for row in eligible_rows}
    seen_identifiers: set[str] = set()
    qualifying: list[EntrySample] = []
    eligible_category = str(source.get("eligible_candidate_category", "")).strip()
    allowlist_values: object = (
        route.category_allowlist
        if detail_route and route is not None
        else source.get("ranking_detail_category_allowlist", [])
    )
    allowlist_items = allowlist_values if isinstance(allowlist_values, list | tuple) else ()
    allowlist = {normalize_visible_text(value) for value in allowlist_items if isinstance(value, str) and value.strip()}
    for row_index, row in enumerate(rows, start=1):
        if id(row) not in eligible_row_ids:
            continue
        required_row_fields: list[tuple[str, str]] = [("rank", row.rank), ("title", row.title)]
        if ranking_row_author_required(source):
            required_row_fields.append(("author", row.author))
        required_row_fields.append(
            ("identifier", row.identifier) if "isbn" not in detail_fields else ("detail_link", row.detail_url)
        )
        missing_fields = [field for field, value in required_row_fields if not value]
        if row.detail_link_error:
            missing_fields.append("detail_link")
        if missing_fields:
            detail = row.identifier_error if "identifier" in missing_fields and row.identifier_error else ""
            reason = "ranking row missing " + ", ".join(missing_fields)
            if detail:
                reason += f": {detail}"
            return _verdict(
                OUTCOME_REJECT,
                REASON_RANKING_ROW_SHAPE,
                reason,
                use_get_fallback=fallback,
                detected_heading=identity.heading,
                bounded_entry_count=len(qualifying),
                raw_row_count=raw_row_count,
                ranking_category=identity.category,
                reporting_period=identity.reporting_period,
                contract_family="ranking-table",
                ranking_diagnostics=ranking_row_diagnostics(html, source, row_index),
            )
        if not row.rank.isdigit() or int(row.rank) < 1:
            return _verdict(
                OUTCOME_REJECT,
                REASON_RANKING_ROW_SHAPE,
                "ranking row rank must be a positive integer",
                use_get_fallback=fallback,
                detected_heading=identity.heading,
                raw_row_count=raw_row_count,
                ranking_category=identity.category,
                reporting_period=identity.reporting_period,
                contract_family="ranking-table",
                ranking_diagnostics=ranking_row_diagnostics(html, source, row_index),
            )
        identifier = normalize_visible_text(row.identifier)
        category = (
            eligible_category
            if detail_route and "category" in detail_fields and eligible_category
            else identity.category
        )
        if detail_route and detail_page_by_url:
            detail_page = detail_page_by_url.get(normalize_source_identity(row.detail_url))
            if detail_page is None:
                return _verdict(
                    OUTCOME_REJECT,
                    REASON_RANKING_ROW_SHAPE,
                    "ranking detail evidence is missing a surviving row page",
                    use_get_fallback=fallback,
                    detected_heading=identity.heading,
                    raw_row_count=raw_row_count,
                    ranking_category=identity.category,
                    reporting_period=identity.reporting_period,
                    contract_family="ranking-table",
                )
            values, detail_error = ranking_detail_fields_from_html(
                str(detail_page.get("html", "")), route or source, enforce_category_allowlist=False
            )
            if detail_error:
                return _verdict(
                    OUTCOME_REJECT,
                    REASON_RANKING_ROW_SHAPE,
                    f"ranking detail field validation failed: {detail_error}",
                    use_get_fallback=fallback,
                    detected_heading=identity.heading,
                    raw_row_count=raw_row_count,
                    ranking_category=identity.category,
                    reporting_period=identity.reporting_period,
                    contract_family="ranking-table",
                )
            if "category" in detail_fields:
                if normalize_visible_text(values.get("category", "")) not in allowlist:
                    continue
            if "isbn" in detail_fields:
                identifier = values.get("isbn", "")
        identity_key = (
            identifier
            if detail_route and "isbn" in detail_fields and detail_page_by_url
            else normalize_source_identity(row.detail_url)
            if detail_route
            else identifier
        )
        if identity_key in seen_identifiers:
            if detail_route and "isbn" not in detail_fields:
                return _verdict(
                    OUTCOME_REJECT,
                    REASON_RANKING_ROW_SHAPE,
                    "ranking rows contain duplicate detail URL",
                    use_get_fallback=fallback,
                    detected_heading=identity.heading,
                    raw_row_count=raw_row_count,
                    ranking_category=identity.category,
                    reporting_period=identity.reporting_period,
                    contract_family="ranking-table",
                )
            continue
        seen_identifiers.add(identity_key)
        qualifying.append(
            EntrySample(
                title=row.title,
                author=row.author,
                url=row.detail_url if detail_route else configured_url,
                rank=row.rank,
                identifier=identifier,
                category=category,
                reporting_period=identity.reporting_period,
            )
        )
    if not qualifying:
        return _verdict(
            OUTCOME_REJECT,
            REASON_RANKING_NO_QUALIFYING_ROWS,
            "ranking page has no qualifying adult-nonfiction rows",
            use_get_fallback=fallback,
            detected_heading=identity.heading,
            ranking_category=identity.category,
            reporting_period=identity.reporting_period,
            contract_family="ranking-table",
        )
    return _verdict(
        OUTCOME_ACCEPT,
        REASON_OK,
        "bounded ranking rows with first-party category and reporting-period evidence accepted",
        use_get_fallback=fallback,
        detected_heading=identity.heading,
        bounded_entry_count=len(qualifying),
        raw_row_count=raw_row_count,
        samples=tuple(qualifying[:MAX_SAMPLES]),
        ranking_category=identity.category,
        reporting_period=identity.reporting_period,
        contract_family="ranking-table",
    )


def evaluate_award_list_feasibility(
    source: Mapping[str, object],
    *,
    resolved_url: str,
    html: str,
    reference_date: date | None = None,
    source_page: str = "",
    http_evidence: HttpStatusEvidence,
) -> FeasibilityVerdict:
    """Evaluate a bounded annual award list without network I/O (Issue #577).

    Rows are unlinked and unranked, so every accepted row cites the configured
    page. The page must state the declared award year, expose each declared
    category heading exactly once, and hold exactly the declared number of
    rows under each, every row stating a title and a primary author in the
    declared format.
    """
    from autoscout.award_list import FAMILY
    from autoscout.award_list import REASON_DECLARATION
    from autoscout.award_list import AwardListError
    from autoscout.award_list import author_display
    from autoscout.award_list import award_page
    from autoscout.award_list import page_declaration_error

    error, fallback = validate_http_status_evidence(http_evidence)
    if error is not None:
        return error
    configured_url = str(source.get("url", "")).strip()

    def reject(reason_code: str, reason: str, *, heading: str = "") -> FeasibilityVerdict:
        return _verdict(
            OUTCOME_REJECT,
            reason_code,
            reason,
            use_get_fallback=fallback,
            detected_heading=heading,
            contract_family=FAMILY,
        )

    if not _same_resolved_page(configured_url, resolved_url or configured_url):
        return reject(REASON_UNRELATED_REDIRECT, "GET resolved to an unrelated target")
    if source_page and not _same_resolved_page(configured_url, source_page):
        return reject(REASON_PAGE_SHAPE, "award-list source_page does not match the configured source URL")
    if not html.strip():
        return reject(REASON_EMPTY_RESPONSE, "GET returned an empty response body")
    if likely_blocked_response(html) or _generic_blocked_response(html):
        return reject(REASON_BLOCKED_RESPONSE, "GET returned a CAPTCHA or bot wall")
    declaration_error = page_declaration_error(source)
    if declaration_error:
        return reject(REASON_DECLARATION, declaration_error)
    try:
        identity = parse_visible_page_identity(html, str(source.get("visible_title_selector", "")).strip())
    except ValueError as exc:
        return reject(REASON_VISIBLE_TITLE_SELECTOR, str(exc))
    if identity.failure_cause is not None or not identity.identity_texts:
        return reject(REASON_MISSING_HEADING, "award page exposes no visible page heading")
    heading = identity.identity_texts[0]
    try:
        page = award_page(html, source, reference_date=reference_date)
    except AwardListError as exc:
        return reject(exc.reason_code, str(exc), heading=heading)
    samples = tuple(
        EntrySample(
            title=row.title,
            # The names joined as collection joins a mention's authors, so the
            # helper sidecar keyed by this display matches at collection.
            author=author_display(row.author, source),
            url=configured_url,
            rank=row.rank,
            identifier=row.identifier,
            category=row.category,
        )
        for row in page.rows
    )
    categories = len(page.category_headings)
    return _verdict(
        OUTCOME_ACCEPT,
        REASON_OK,
        f"bounded {page.year} award rows under "
        + ("one visible category heading" if categories == 1 else f"{categories} visible category headings")
        + " accepted",
        use_get_fallback=fallback,
        detected_heading=heading,
        bounded_entry_count=len(samples),
        raw_row_count=len(samples),
        samples=samples[:MAX_SAMPLES],
        ranking_category="; ".join(page.category_headings),
        contract_family=FAMILY,
    )


def _reference_date_from_source(source: Mapping[str, object]) -> date:
    value = str(source.get("reference_date", "")).strip()
    if value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return date.min
    # A missing reference date is intentionally deterministic.  The Amazon
    # evaluator still fails first when its required landing evidence is absent;
    # callers that need a recency verdict must provide a pinned date.
    return date.min


def _with_contract_family(verdict: FeasibilityVerdict, family: str) -> FeasibilityVerdict:
    return verdict if verdict.contract_family == family else replace(verdict, contract_family=family)


def evaluate_source_feasibility(
    source: Mapping[str, object],
    evidence: CapturedSourceEvidence | Mapping[str, object],
    *,
    http_evidence: HttpStatusEvidence | None = None,
) -> FeasibilityVerdict:
    """Dispatch one pure feasibility evaluation by declared contract family.

    The generic evaluator remains the default.  Legacy configured chart flags
    are supported for configured sources, but no family is inferred from a
    URL.  Probe callers can provide an explicit ``contract_family`` and the
    declaration is retained with the evidence.
    """
    declared = str(source.get("contract_family", "")).strip().casefold()
    family = declared or configured_contract_family(dict(source))
    if family == "bookinfo-weekly-json":
        from autoscout.bookinfo_ranking import feasibility

        if not isinstance(evidence, Mapping):
            return _verdict(OUTCOME_REJECT, REASON_PAGE_SHAPE, "BookInfo requires structured retained evidence")
        return feasibility(evidence, source)
    if family == "ipc-top40-csv":
        from autoscout.ipc_top40_ranking import feasibility as ipc_top40_feasibility

        if not isinstance(evidence, Mapping):
            return _verdict(
                OUTCOME_REJECT, REASON_PAGE_SHAPE, "Independent Press Top 40 requires structured retained evidence"
            )
        return ipc_top40_feasibility(evidence, source)
    if family == "perlentaucher-daily-digest":
        from autoscout.perlentaucher_digest import feasibility as perlentaucher_feasibility

        if not isinstance(evidence, Mapping):
            return _verdict(
                OUTCOME_REJECT, REASON_PAGE_SHAPE, "Perlentaucher digest requires structured retained evidence"
            )
        return perlentaucher_feasibility(evidence, source)
    if family == "kirkus-coming-soon":
        from autoscout.kirkus_listing import feasibility as kirkus_feasibility

        if not isinstance(evidence, Mapping):
            return _verdict(OUTCOME_REJECT, REASON_PAGE_SHAPE, "Kirkus listing requires structured retained evidence")
        return kirkus_feasibility(evidence, source)
    ranking_enabled = family == "ranking-table" and (bool(declared) or _ranking_declarations_present(source))
    if ranking_enabled and isinstance(evidence, Mapping) and "source_page" not in evidence:
        return _verdict(
            OUTCOME_REJECT,
            REASON_RANKING_EVIDENCE_URL,
            "ranking-table evidence must record source_page",
            contract_family="ranking-table",
        )
    if isinstance(evidence, Mapping):
        captured = evidence_from_mapping(evidence)
    else:
        captured = evidence

    if http_evidence is None:
        ranking_request = ranking_request_from_source(source)
        status_evidence = (
            HttpStatusEvidence.declared_post(body_status=captured.get_status)
            if ranking_request.is_post
            else HttpStatusEvidence.get_with_head(
                head_status=captured.head_status,
                body_status=captured.get_status,
                allow_head_404=declared_head_404_fallback(source),
            )
        )
    else:
        status_evidence = http_evidence
        if status_evidence.allow_head_404 and not declared_head_404_fallback(source):
            status_evidence = replace(status_evidence, allow_head_404=False)
    if family not in CONTRACT_FAMILIES:
        return _verdict(OUTCOME_REJECT, REASON_PAGE_SHAPE, f"unsupported contract family: {family}")
    if family == "publisher-frontlist":
        from autoscout.publisher_frontlist import frontlist_cards as publisher_frontlist_cards
        from autoscout.publisher_frontlist import heading as publisher_heading

        # Apply the unchanged HTTP, bot-wall, redirect and visible-publisher_heading
        # policy to the raw response before examining the declared publisher_cards.
        initial = evaluate_captured_source_evidence(captured, http_evidence=status_evidence)
        if initial.reason_code in {
            REASON_HEAD_DENIED,
            REASON_HTTP_ERROR,
            REASON_EMPTY_RESPONSE,
            REASON_BLOCKED_RESPONSE,
            REASON_UNRELATED_REDIRECT,
            REASON_MISSING_HEADING,
            REASON_VISIBLE_TITLE_SELECTOR,
        }:
            return _with_contract_family(initial, family)
        try:
            publisher_cards = publisher_frontlist_cards(captured.html, captured.source_url)
        except ValueError as exc:
            return _verdict(OUTCOME_REJECT, REASON_PAGE_SHAPE, str(exc), contract_family=family)
        return _verdict(
            OUTCOME_ACCEPT,
            REASON_OK,
            "bounded publisher frontlist cards accepted",
            detected_heading=publisher_heading(captured.source_url),
            bounded_entry_count=len(publisher_cards),
            samples=tuple(
                EntrySample(publisher_card.title, " / ".join(publisher_card.authors), publisher_card.url)
                for publisher_card in publisher_cards[:MAX_SAMPLES]
            ),
            contract_family=family,
        )
    if family == "nuevoslibros-frontlist":
        from autoscout.nuevoslibros_frontlist import frontlist_cards
        from autoscout.nuevoslibros_frontlist import heading

        # Apply the unchanged HTTP, bot-wall, redirect and visible-heading
        # policy to the raw response before examining the declared cards.
        initial = evaluate_captured_source_evidence(captured, http_evidence=status_evidence)
        if initial.reason_code in {
            REASON_HEAD_DENIED,
            REASON_HTTP_ERROR,
            REASON_EMPTY_RESPONSE,
            REASON_BLOCKED_RESPONSE,
            REASON_UNRELATED_REDIRECT,
            REASON_MISSING_HEADING,
            REASON_VISIBLE_TITLE_SELECTOR,
        }:
            return _with_contract_family(initial, family)
        try:
            cards = frontlist_cards(captured.html, captured.source_url)
        except ValueError as exc:
            return _verdict(OUTCOME_REJECT, REASON_PAGE_SHAPE, str(exc), contract_family=family)
        return _verdict(
            OUTCOME_ACCEPT,
            REASON_OK,
            "dated nonfiction frontlist cards accepted",
            detected_heading=heading(captured.source_url),
            bounded_entry_count=len(cards),
            samples=tuple(EntrySample(card.title, " / ".join(card.authors), card.url) for card in cards[:MAX_SAMPLES]),
            contract_family=family,
        )
    if family == "ranking-table":
        # Legacy ranking configurations predate the row declarations and must
        # retain the generic baseline until they opt into this contract.
        if ranking_enabled:
            return _with_contract_family(
                evaluate_ranking_table_feasibility(
                    source,
                    resolved_url=captured.resolved_url or captured.source_url,
                    html=captured.html,
                    reference_date=captured.reference_date,
                    source_page=captured.source_page,
                    detail_pages=list(captured.detail_pages),
                    identity_html=captured.identity_html,
                    http_evidence=status_evidence,
                ),
                family,
            )
    if family == "award-list":
        return evaluate_award_list_feasibility(
            source,
            resolved_url=captured.resolved_url or captured.source_url,
            html=captured.html,
            reference_date=captured.reference_date,
            source_page=captured.source_page,
            http_evidence=status_evidence,
        )
    if family == "audible-chart":
        return _with_contract_family(
            evaluate_audible_chart_feasibility(
                dict(source),
                resolved_url=captured.resolved_url or captured.source_url,
                html=captured.html,
                author_marker=str(source.get("author_marker") or "by:"),
                http_evidence=status_evidence,
            ),
            family,
        )
    if family == "amazon-charts":
        return _with_contract_family(
            evaluate_amazon_charts_feasibility(
                dict(source),
                resolved_url=captured.resolved_url or captured.source_url,
                html=captured.html,
                landing_html=captured.landing_html or str(source.get("landing_html", "")),
                author_marker=str(source.get("author_marker") or "by"),
                reference_date=captured.reference_date or _reference_date_from_source(source),
                http_evidence=status_evidence,
            ),
            family,
        )
    return (
        _with_contract_family(evaluate_captured_source_evidence(captured, http_evidence=status_evidence), family)
        if declared
        else evaluate_captured_source_evidence(captured, http_evidence=status_evidence)
    )


# Short alias for callers that describe the input as a captured page.
dispatch_source_feasibility = evaluate_source_feasibility


@dataclass(frozen=True)
class CapturedSourceEvidence:
    """Already-captured Source Candidate evidence for pure evaluation (Issue #334).

    *source_url* is the candidate URL that was requested; *resolved_url* is
    the final URL after redirects and defaults to *source_url* when the
    capture did not record one. *head_status* is the recorded HEAD status and
    *get_status* the recorded GET status; both default to None, because
    missing status evidence must reject before parsing - there is no implicit
    200 (Issue #334 review).
    *visible_title_selector* is an absent-by-default explicit selector made
    of ``.class-token`` parts joined by direct-child ``>`` combinators. When
    supplied, it is authoritative and h1-h6 headings are not used as a fallback.
    *entry_scope_selector* is an absent-by-default selector with at least two
    class-token parts. Its final class selects direct-child entry containers."""

    source_url: str
    html: str
    resolved_url: str = ""
    head_status: int | None = None
    get_status: int | None = None
    visible_title_selector: str = ""
    entry_scope_selector: str = ""
    landing_html: str = ""
    identity_html: str = ""
    reference_date: date | None = None
    source_page: str = ""
    detail_pages: tuple[Mapping[str, object], ...] = ()


def _as_int(value: object) -> int:
    """Narrow a mapping status value to int, rejecting booleans and junk."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"expected an integer status, got {value!r}")
    return value


def evidence_from_mapping(data: Mapping[str, object]) -> CapturedSourceEvidence:
    """Adapt a captured-evidence mapping to :class:`CapturedSourceEvidence`.

    Accepted keys and aliases:

    * ``source_url`` (alias ``url``, ``source_page``): the candidate URL that
      was requested. ``source_page`` is the key carried by existing
      repository page captures; explicit ``source_url`` or ``url`` wins.
    * ``html`` (alias ``html_content``): the captured response body.
    * ``resolved_url`` (alias ``final_url``): the final URL after redirects;
      defaults to *source_url* when absent.
    * ``head_status`` (alias ``head``): recorded HEAD status; None when
      absent. Missing HEAD evidence rejects before parsing.
    * ``get_status`` (alias ``status``, ``get``): recorded GET status; None
      when absent. There is no 200 default: a body-only capture without
      explicit GET status evidence rejects before parsing (Issue #334
      review). An explicit ``get_status: null`` records missing GET evidence
      exactly like an absent key.
    * ``visible_title_selector``: an optional selector made of
      ``.class-token`` parts joined by direct-child ``>`` combinators. When
      supplied, it is authoritative and h1-h6 headings are not a fallback.
    * ``entry_scope_selector``: an optional selector with at least two
      ``.class-token`` parts. Its final class selects direct-child entries.

    Unknown keys are ignored so later probe versions can add fields without
    breaking pure evaluation. Non-integer status values raise ValueError."""
    source_url = str(data.get("source_url", data.get("url", data.get("source_page", ""))))
    source_page = str(data.get("source_page", source_url)) or source_url
    raw_identity_html = data.get("identity_html", "")
    if raw_identity_html is None:
        raw_identity_html = ""
    if not isinstance(raw_identity_html, str):
        raise ValueError("identity_html must be a string")
    page_evidence = RankingPageEvidence(
        rows_html=str(data.get("html", data.get("html_content", ""))),
        identity_html=raw_identity_html,
    )
    html = page_evidence.rows_html
    resolved = str(data.get("resolved_url", data.get("final_url", source_url))) or source_url
    head = data.get("head_status", data.get("head"))
    get = data.get("get_status", data.get("status", data.get("get")))
    visible_title_selector = data.get("visible_title_selector", "")
    if visible_title_selector is None:
        visible_title_selector = ""
    if not isinstance(visible_title_selector, str):
        raise ValueError("visible_title_selector must be a string")
    entry_scope_selector = data.get("entry_scope_selector", "")
    if entry_scope_selector is None:
        entry_scope_selector = ""
    if not isinstance(entry_scope_selector, str):
        raise ValueError("entry_scope_selector must be a string")
    if "entry_scope_selector" in data and entry_scope_selector.strip():
        validate_entry_scope_selector(entry_scope_selector)
    landing_html = data.get("landing_html", "")
    if landing_html is None:
        landing_html = ""
    if not isinstance(landing_html, str):
        raise ValueError("landing_html must be a string")
    identity_html = page_evidence.identity_html
    raw_detail_pages = data.get("detail_pages", [])
    detail_pages: tuple[Mapping[str, object], ...] = ()
    if raw_detail_pages not in (None, [], ()):
        if not isinstance(raw_detail_pages, list | tuple) or any(
            not isinstance(item, Mapping) for item in raw_detail_pages
        ):
            raise ValueError("detail_pages must be a list of objects")
        detail_pages = tuple(dict(item) for item in raw_detail_pages)
    raw_reference_date = data.get("reference_date")
    parsed_reference_date: date | None = None
    if raw_reference_date is not None:
        try:
            parsed_reference_date = date.fromisoformat(str(raw_reference_date))
        except ValueError as exc:
            raise ValueError("reference_date must be an ISO date") from exc
    return CapturedSourceEvidence(
        source_url=source_url,
        html=html,
        resolved_url=resolved,
        source_page=source_page,
        head_status=_as_int(head) if head is not None else None,
        get_status=_as_int(get) if get is not None else None,
        visible_title_selector=visible_title_selector,
        entry_scope_selector=entry_scope_selector,
        landing_html=landing_html,
        identity_html=identity_html,
        reference_date=parsed_reference_date,
        detail_pages=detail_pages,
    )


# Deterministic client-rendering markers: their presence means the book data
# is delivered to the browser as a script payload rather than server markup.
# A page with these markers and no bounded server-rendered list defers.
_JS_APP_MARKERS = (
    "__next_data__",
    "__nuxt__",
    "window.__initial_state__",
    "window.__apollo_state__",
    "__preloaded_state__",
    "data-reactroot",
    "ng-app",
    'id="root"',
    'id="app"',
    'id="__next"',
)


def _js_app_markers(html_content: str) -> bool:
    lowered = html_content.casefold()
    return any(marker in lowered for marker in _JS_APP_MARKERS)


def _same_resolved_page(requested_url: str, resolved_url: str) -> bool:
    """True when the resolved URL is the requested page after redirects.

    The scheme, netloc, path (after harmless trailing-slash normalization),
    and query must match; fragments are client-side and ignored. A redirect
    to a different path on the same site (for example
    ``/nonfiction-shortlist`` to ``/fiction-bestsellers``) or to the same
    path with a different query (``/list?category=nonfiction`` to
    ``/list?category=fiction``) is a different page and counts as unrelated
    (Issue #334 review)."""
    requested = urlsplit(requested_url)
    resolved = urlsplit(resolved_url)
    if not requested.scheme or not requested.netloc:
        return False
    return (
        requested.scheme.casefold() == resolved.scheme.casefold()
        and requested.netloc.casefold() == resolved.netloc.casefold()
        and requested.path.rstrip("/") == resolved.path.rstrip("/")
        and requested.query == resolved.query
    )


def _http_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme.casefold() in {"http", "https"} and bool(parts.netloc)


# Obvious challenge-wall phrases for the source-independent probe. These are
# deliberately narrower than the configured-source markers in
# ``likely_blocked_response``: a bare visible CAPTCHA heading or a
# verify-you-are-human wall must fail closed even when the challenge verb
# sits too far from the word ``captcha`` for the shared heuristic to match.
_GENERIC_BLOCKED_PHRASES = (
    "verify you are human",
    "are you human",
    "you have been blocked",
    "robot check",
    "access denied",
    "attention required",
    "captcha verification",
    "captcha challenge",
)


def _generic_blocked_response(html_content: str) -> bool:
    """True for an obvious CAPTCHA/bot-wall response (Issue #334, scoped).

    This check is used only by the source-independent captured-evidence
    evaluator, never by the configured Audible/Amazon chart seams. It catches
    challenge responses that ``likely_blocked_response`` deliberately does
    not flag, such as a bare visible CAPTCHA heading with no challenge verb
    nearby, and fails closed. A heading is the strongest deterministic signal:
    the page announces itself as a challenge page. The phrase list covers
    verify-you-are-human walls and explicit block notices anywhere in the
    body; benign reCAPTCHA form widgets (``data-sitekey`` or widget scripts)
    are not flagged by themselves, whether they are visible or hidden: a
    hidden CAPTCHA *heading* fails closed exactly as a visible one does, but a
    bare marker in ordinary hidden text does not, because a hidden widget
    badge is not a challenge page (Issue #566 review)."""
    lowered = html_content.casefold()
    if any(phrase in lowered for phrase in _GENERIC_BLOCKED_PHRASES):
        return True
    headings = list(parse_heading_texts(html_content)) + list(parse_hidden_heading_texts(html_content))
    return any("captcha" in heading.casefold() for heading in headings)


_NON_AUTHOR_TOKENS = frozenset(
    {
        "hardcover",
        "paperback",
        "ebook",
        "e-book",
        "audiobook",
        "audio",
        "audible",
        "kindle",
        "nook",
        "edition",
        "volume",
        "boxed",
        "set",
        "signed",
        "copy",
        "reading",
        "listen",
        "listening",
        "preorder",
        "pre-order",
        "buy",
        "shop",
        "read",
        "more",
        "learn",
        "details",
        "excerpt",
        "review",
        "reviews",
        "trailer",
        "series",
        "collection",
        "book",
        "books",
        "novel",
        "novels",
        "out",
        "now",
        "soon",
        "coming",
        "available",
        "new",
        "free",
        "sample",
        "play",
        "add",
        "cart",
        "wishlist",
        "share",
        "save",
        "print",
        "email",
        "isbn",
        "price",
        # Narrative and list-label terms: prose such as "Winner of the
        # annual prize" or "A sweeping history of Europe" must never be read
        # as a primary author (Issue #334 review). Deterministic token
        # rejection only - no model, network, or semantic matching.
        "winner",
        "winners",
        "annual",
        "prize",
        "prizes",
        "shortlist",
        "shortlisted",
        "longlist",
        "longlisted",
        "nominee",
        "nominees",
        "nominated",
        "finalist",
        "finalists",
        "award",
        "awards",
        "awarded",
        "jury",
        "judge",
        "judges",
        "judging",
        "judged",
        "category",
        "categories",
        "selection",
        "selections",
        "selected",
        "announced",
        "announces",
        "announcement",
        "presented",
        "presenting",
        "presentation",
        # Narrative prose terms: a rejected term anywhere in the contiguous
        # fallback span fails the whole span closed, so descriptive copy
        # such as "A sweeping history of Europe" is never stitched into a
        # non-contiguous author (Issue #334).
        "sweeping",
        "history",
        "story",
        "stories",
        "memoir",
        "memoirs",
        "biography",
        "biographies",
        "autobiography",
        "epic",
        # Generic connector/function words: they never carry the author name
        # and must not keep narrative prose above the two-word threshold.
        "of",
        "the",
        "a",
        "an",
        "and",
        "or",
        "for",
        "to",
        "in",
        "on",
        "at",
        "by",
        "with",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "has",
        "have",
        "had",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "their",
        "her",
        "his",
        # Prose prepositions and connective adverbs: the same connector class
        # as above, so descriptive copy such as "Powerful Exploration Across
        # Europe" fails closed in every path - bare text, explicit marker,
        # and author-marked element alike. These are common function words,
        # never name particles and never standalone name words (Issue #334
        # review).
        "across",
        "through",
        "throughout",
        "over",
        "under",
        "above",
        "below",
        "beneath",
        "between",
        "beyond",
        "behind",
        "beside",
        "besides",
        "during",
        "within",
        "without",
        "among",
        "amongst",
        "along",
        "alongside",
        "around",
        "toward",
        "towards",
        "upon",
        "inside",
        "outside",
        "against",
        "despite",
        "except",
        "until",
        "since",
    }
)

# One Unicode letter in any script: accented Latin (é, ü, ō), extended
# Latin (Ł, Đ), and Cyrillic (Ё, й, ґ) all count. Python's ``\w`` in the
# default Unicode mode covers every letter; excluding digits and underscore
# leaves exactly letters, so a name such as "Łukasz Orbitowski" keeps its
# first character (Issue #334 review).
_LETTER = r"[^\W\d_]"
# Unicode combining marks (Mn/Mc): decomposed accents such as the combining
# acute in "Lu\u0301kasz" are not letters and not ``\w``, but they belong to
# the visible spelling of a name and must stay in the contiguous author
# token, never splitting or dropping the decomposed accent (Issue #334
# review). The ranges cover Combining Diacritical Marks, Extended, and
# Supplement blocks plus Combining Half Marks.
_COMBINING_MARKS = r"\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f"
_LETTER_OR_MARK = rf"(?:{_LETTER}|[{_COMBINING_MARKS}])"
_LETTER_OR_PUNCT = rf"(?:{_LETTER_OR_MARK}|'|’|\.|\-)"
_AUTHOR_WORD = rf"{_LETTER}(?:{_LETTER_OR_PUNCT})*"
_AUTHOR_PERSON_RE = re.compile(rf"{_AUTHOR_WORD}(?:\s+{_AUTHOR_WORD})*")
_AUTHOR_SPAN_RE = re.compile(
    rf"(?P<span>{_AUTHOR_WORD}(?:\s+{_AUTHOR_WORD})*(?:,\s*{_AUTHOR_WORD}(?:\s+{_AUTHOR_WORD})*)*)"
)
_AUTHOR_LIST_SEPARATOR_RE = re.compile(r"(?<!\w)(?:and|&)(?!\w)", flags=re.IGNORECASE)


def _strip_rank_prefix(text: str) -> str:
    return re.sub(
        r"^(?:\d{1,3}(?:st|nd|rd|th)?[.)]?\s*|#\s*\d+\s*|№\s*\d+\s*|rank\s+\d+\s*)+",
        "",
        text,
        flags=re.IGNORECASE,
    )


def _marker_author(text: str) -> str:
    """Author text after an explicit ``by:``/``von:``-style marker, if any.

    The marker is grounded author evidence, so the extracted span is
    validated with the relaxed grounded-span policy
    (:func:`_grounded_author_span`) rather than the bare-text fallback: a
    single-word name stays possible, an ordinary multiword name is
    accepted, but narrative/function/list-label terms and connector words
    still fail the span closed, so "By: Powerful Exploration Across Europe"
    never becomes an author (Issue #334 review)."""
    candidate = _strip_rank_prefix(re.sub(r"\s+", " ", text).strip())
    match = AUTHOR_MARKER_RE.match(candidate)
    if match is None:
        return ""
    rest = candidate[match.end() :].strip()
    rest = re.split(r"\s*[·|—–(]\s*", rest, maxsplit=1)[0].strip(" .,;:»«\"'")
    return _grounded_author_span(rest, min_words=1)


# Common lowercase name particles. A grounded author span may contain them
# even though every other word must be title-cased/name-like, so "van der
# Berg" and "Мария фон Трапп" parse while prose such as "Powerful
# exploration across Europe" fails closed on its lowercase non-particle
# word "across" (Issue #334 review).
_NAME_PARTICLES = frozenset(
    {
        # Romance particles.
        "de",
        "di",
        "da",
        "del",
        "della",
        "delle",
        "dei",
        "d'",
        "l'",
        "la",
        "le",
        "du",
        "des",
        "dos",
        "das",
        "y",
        "e",
        "i",
        # Germanic particles.
        "van",
        "von",
        "vom",
        "der",
        "den",
        "ten",
        "ter",
        "zu",
        "zum",
        "zur",
        # Arabic/Semitic particles.
        "al",
        "el",
        "ibn",
        "ben",
        "bin",
        "abu",
        "abd",
        "um",
        # Celtic particles.
        "mac",
        "mc",
        "o'",
        "m'",
        "nic",
        # Slavic name particles (Cyrillic and Latin).
        "фон",
        "ван",
        "ди",
        "дель",
        # Saint marker: "St. Petersburg".
        "st",
        "st.",
    }
)


def _name_like_word(word: str) -> bool:
    """True when one grounded-author word is a plausible name word.

    A word is name-like when it starts with an uppercase letter in any
    script (Latin, accented, extended, or Cyrillic) or when it is a known
    lowercase name particle. Lowercase prose words such as "across" are
    never name-like, so descriptive copy fails the whole span closed
    instead of becoming an invented author (Issue #334 review)."""
    if word and word[0].isupper():
        return True
    return word.casefold() in _NAME_PARTICLES


def _span_author(_text: str) -> str:
    """Reject unmarked text because capitalization is not author evidence.

    Author names are accepted only through an explicit marker, an
    author-marked element, or a typed author link. Keeping this fail-closed
    helper makes accidental fallback reintroduction visible to tests and
    callers (Issue #334 review)."""
    return ""


def _grounded_person_span(text: str, *, min_words: int) -> str:
    """Validate one person or conservative comma-separated person list.

    Commas are structural separators, not disposable punctuation. Validate
    each segment independently and return the normalized captured span, so a
    display such as ``Jane Doe, John Smith`` stays exactly that display.
    """
    person = re.sub(r"\s+", " ", text).strip()
    match = _AUTHOR_SPAN_RE.fullmatch(person)
    if match is None:
        return ""
    span = match.group("span") or ""
    for segment in span.split(","):
        segment_match = _AUTHOR_PERSON_RE.fullmatch(segment.strip())
        if segment_match is None:
            return ""
        words = segment.split()
        if len(words) < min_words:
            return ""
        if any(word.casefold() in _NON_AUTHOR_TOKENS for word in words):
            return ""
        if all(word.islower() for word in words):
            # A deliberate lowercase name ("bell hooks"), at most two words;
            # a longer all-lowercase segment is prose and fails closed.
            if len(words) > 2:
                return ""
        elif not all(_name_like_word(word) for word in words):
            return ""
    return span


def _grounded_author_span(text: str, *, min_words: int = 1) -> str:
    """Validate one grounded author or a deterministic coauthor display.

    Grounded structure is an explicit ``By``/``Author`` marker, an
    author-marked element, or a typed author link. Each person segment must
    be a complete name-like span. ``and`` and ``&`` are the only accepted
    coauthor separators, so connector words are never deleted from prose to
    invent an author list. An Oxford comma immediately before ``and`` or
    ``&`` belongs to that separator and is accepted without changing the
    returned display. The returned display preserves the visible spelling and
    separator text after whitespace normalization (Issue #334 review)."""
    candidate = re.sub(r"\s+", " ", text).strip()
    if not candidate:
        return ""
    separators = list(_AUTHOR_LIST_SEPARATOR_RE.finditer(candidate))
    if not separators:
        return _grounded_person_span(candidate, min_words=min_words)
    start = 0
    for separator in separators:
        segment = candidate[start : separator.start()].rstrip()
        if segment.endswith(","):
            segment = segment[:-1].rstrip()
        if not _grounded_person_span(segment, min_words=min_words):
            return ""
        start = separator.end()
    if not _grounded_person_span(candidate[start:], min_words=min_words):
        return ""
    return candidate


_GENERIC_EXPLICIT_AUTHOR_LABELS = frozenset({"profile", "profiles"})
_GENERIC_EXPLICIT_AUTHOR_PREFIXES = frozenset({"about", "meet", "see", "view", "visit"})


def _is_generic_explicit_author_text(text: str) -> bool:
    """Reject generic author labels at the explicit-evidence boundary.

    These terms are intentionally not part of the global name-shape policy:
    real people can be named ``Page``. They are filtered only when an
    explicit author marker says that the complete visible label is author
    evidence, where a generic label or CTA cannot prove a person name.
    """
    normalized = normalize_visible_text(text)
    if normalized in _GENERIC_EXPLICIT_AUTHOR_LABELS or normalized in ACTION_LABELS:
        return True
    first = normalized.split(maxsplit=1)[0] if normalized else ""
    return first in _GENERIC_EXPLICIT_AUTHOR_PREFIXES


def _entry_primary_author(entry: SourceListEntry) -> str:
    """Detect the deterministic primary-author span of one bounded entry.

    The parser supplies separate evidence for explicit author markers,
    author-marked elements, typed author links, and URL-inferred author
    links. Explicit evidence wins over URL inference, and each candidate is
    validated as a complete grounded name span. Unmarked context is used
    only for explicit author markers; capitalization-only bare text is
    rejected fail-closed. Lowercase names, ordinary multiword names, and
    coauthor displays are accepted when the parser provides grounded author
    evidence, while descriptive prose remains rejected (Issue #334 review)."""
    # Marker runs are collected outside anchor text, so a title word cannot
    # be removed from the marker payload or cause an anchor titled ``By ...``
    # to become a marker.
    for marker_text in entry.marker_evidence:
        marked = _marker_author(marker_text)
        if marked and not _is_generic_explicit_author_text(marked):
            return marked

    # Explicit author structure outranks URL-shape inference. Invalid
    # explicit text does not block a separate valid inferred candidate.
    for grounded_text in entry.author_evidence or entry.author_elements:
        marked = _marker_author(grounded_text)
        if marked and not _is_generic_explicit_author_text(marked):
            return marked
        if _is_generic_explicit_author_text(grounded_text):
            continue
        span = _grounded_author_span(grounded_text, min_words=1)
        if span:
            return span

    inferred: list[str] = []
    for inferred_text in entry.inferred_author_evidence:
        marked = _marker_author(inferred_text)
        span = marked if marked and not _is_generic_explicit_author_text(marked) else ""
        span = span or _grounded_author_span(inferred_text, min_words=1)
        if span:
            inferred.append(span)
    distinct_inferred = {normalize_visible_text(author) for author in inferred}
    if len(distinct_inferred) > 1:
        return ""
    if inferred:
        return inferred[0]

    # Marker text inside an anchor is intentionally not a fallback author
    # source. A lone title such as ``By: Jane Doe`` must remain a book link,
    # not become its own author evidence. Non-anchor marker runs were already
    # captured in ``marker_evidence`` above.
    return ""


_RELATIONAL_TITLE_LABEL_RE = re.compile(
    r"^(?:image\s+of\s+|cover\s*:\s*)(?P<title>.+)$",
    flags=re.IGNORECASE,
)


def _most_title_like_book_link(group: list[SourceListEntry]) -> SourceListEntry | None:
    """Pick a deterministic primary title from one same-URL book group.

    A media/accessibility label can be a book-kind link when its wording is
    not one of the known global action labels. Only relational evidence is
    allowed to classify it: ``Image of <exact title>`` and ``Cover: <exact
    title>`` select the same-URL anchor whose text exactly matches the
    alternate title. The grammar is not applied to a lone anchor, so valid
    titles such as ``Image of Dorian Gray`` and ``Cover Story`` remain
    possible. Conflicting relational claims, or multiple unrelated same-URL
    texts without a deterministic relation, reject instead of guessing
    (Issue #334 review)."""
    normalized_titles = {normalize_visible_text(entry.title) for entry in group}
    relational_targets: dict[str, SourceListEntry] = {}
    relational_claims: set[str] = set()
    relational_labels: set[str] = set()
    unmatched_relational_label = False
    for entry in group:
        match = _RELATIONAL_TITLE_LABEL_RE.fullmatch(normalize_visible_text(entry.title))
        if match is None:
            continue
        alternate = normalize_visible_text(match.group("title"))
        if not alternate:
            continue
        relational_labels.add(normalize_visible_text(entry.title))
        exact = [candidate for candidate in group if normalize_visible_text(candidate.title) == alternate]
        if exact:
            relational_claims.add(alternate)
            relational_targets.setdefault(alternate, exact[0])
        else:
            unmatched_relational_label = True
    if unmatched_relational_label and len(normalized_titles) > 1:
        return None
    if len(relational_claims) > 1:
        return None
    if relational_claims:
        alternate = next(iter(relational_claims))
        if normalized_titles - relational_labels - {alternate}:
            return None
        return relational_targets[alternate]
    if len(normalized_titles) > 1:
        return None
    non_cover = [entry for entry in group if not is_cover_label(normalize_visible_text(entry.title))]
    preferred = non_cover or group
    return max(preferred, key=lambda entry: (len(entry.title.split()), len(entry.title)))


def evaluate_captured_source_evidence(
    evidence: CapturedSourceEvidence | Mapping[str, object],
    source: Mapping[str, object] | None = None,
    *,
    http_evidence: HttpStatusEvidence | None = None,
) -> FeasibilityVerdict:
    """Evaluate already-captured Source Candidate evidence (Issue #334).

    Pure and deterministic: no network, model, configuration, weekly state,
    or Git access. *evidence* may be a :class:`CapturedSourceEvidence` or a
    mapping understood by :func:`evidence_from_mapping` (see that function
    for the accepted keys and aliases).

    Policy, conservative and fail-closed:

    * HEAD 200, or HEAD 405/501 followed by one plain GET 200, is the only
      HTTP path that may proceed. HEAD evidence and GET evidence are both
      mandatory: a capture missing either status rejects before parsing.
      HEAD 403, 429, CAPTCHA/bot walls, and any other HEAD status never fall
      back.
    * The GET must be 200 and resolve to the requested page: the scheme,
      netloc, path, and query must match after harmless trailing-slash
      normalization (an unrelated redirect rejects), the body must be
      nonblank and free of CAPTCHA/bot-wall markers, and the page must
      expose an h1-h6 heading or an explicitly configured visible page title.
    * Anchors inside a navigation, header, or footer landmark (``nav``
      elements, ``role="navigation"``, ``header``/``footer`` elements, and
      their ARIA roles) are ignored when qualifying book entries are found:
      site chrome never counts as a book entry, never causes a
      missing-author rejection, and never blocks a valid bounded book list.
    * One bounded entry container (``li``/``article``/``tr``) qualifies at
      most one primary book-level link: anchors that share the same nearest
      entry block are one book, and obvious non-book links (``Buy now``,
      ``Read more``, ``Add to cart``, explicit author links, and unmarked
      author-page links) are never the primary link. A container that holds
      only action/author links is not an accepted book entry.
    * A header/footer/banner element nested inside an actual per-entry
      container is that entry's own header, not page-level site chrome:
      ``<article><header>…</header></article>`` qualifies. Page-level
      header/footer/banner chrome keeps filtering.
    * Only server-rendered linked book entries with a visible primary author
      inside an actual per-entry container (``li``/``article``/``tr``) can be
      accepted; a page-level wrapper such as ``section`` around several
      direct anchors is not a per-entry container. Any non-empty bounded
      list qualifies; the issue specifies no minimum count. JS-only pages
      defer (``js_only``); unlinked or unbounded lists and entries without a
      visible author reject.

    Accepted verdicts expose the detected heading, the bounded entry count,
    and up to ``MAX_SAMPLES`` grounded title/author/URL samples in document
    order. The primary sample always uses a grounded book-title link and
    book URL; an author link inside the same entry grounds the sample author
    without becoming the title. Parser metadata keeps action, navigation,
    and author links distinct from book-level links and supplies explicit
    author markers, author-marked elements, and URL-inferred author evidence
    at separate trust levels. Every same-URL anchor record is preserved;
    relational media labels select an exact same-URL alternate title and
    ambiguous title groups reject. Capitalization alone is never author
    evidence: a lowercase name ("bell hooks") is valid only inside grounded
    author structure (an explicit marker, an author-marked element, or a
    typed author link), ordinary multiword names ("Mary Ann Evans",
    "Gabriel Garcia Marquez") and grounded mononyms/coauthor displays are
    accepted, and "Powerful Exploration Across Europe" is descriptive prose
    that fails closed in every grounded path (Issue #334 review). Rejected
    and deferred verdicts expose the heading and count when they were
    computed and no samples."""
    if source is not None:
        return evaluate_source_feasibility(source, evidence, http_evidence=http_evidence)
    if isinstance(evidence, Mapping):
        evidence = evidence_from_mapping(evidence)
    status_evidence = http_evidence or _legacy_http_status_evidence(
        head_status=evidence.head_status, get_status=evidence.get_status
    )
    error, fallback = validate_http_status_evidence(status_evidence)
    if error is not None:
        return error
    if not evidence.html.strip():
        return _verdict(
            OUTCOME_REJECT, REASON_EMPTY_RESPONSE, "GET returned an empty response body", use_get_fallback=fallback
        )
    if likely_blocked_response(evidence.html):
        return _verdict(
            OUTCOME_REJECT, REASON_BLOCKED_RESPONSE, "GET returned a CAPTCHA or bot wall", use_get_fallback=fallback
        )
    if _generic_blocked_response(evidence.html):
        return _verdict(
            OUTCOME_REJECT, REASON_BLOCKED_RESPONSE, "GET returned a CAPTCHA or bot wall", use_get_fallback=fallback
        )
    if not _same_resolved_page(evidence.source_url, evidence.resolved_url or evidence.source_url):
        return _verdict(
            OUTCOME_REJECT, REASON_UNRELATED_REDIRECT, "GET resolved to an unrelated target", use_get_fallback=fallback
        )
    identity = parse_visible_page_identity(evidence.html, evidence.visible_title_selector)
    if identity.failure_cause is not None:
        return _verdict(
            OUTCOME_REJECT,
            REASON_VISIBLE_TITLE_SELECTOR,
            _VISIBLE_TITLE_SELECTOR_MESSAGES[identity.failure_cause],
            use_get_fallback=fallback,
        )
    if not identity.identity_texts:
        return _verdict(
            OUTCOME_REJECT, REASON_MISSING_HEADING, "GET page has no h1-h6 heading", use_get_fallback=fallback
        )
    detected_heading = identity.identity_texts[0]
    if evidence.entry_scope_selector:
        scope = parse_entry_scope(evidence.html, evidence.entry_scope_selector)
        if scope.failure_cause is not None:
            reason_code = scope.failure_cause
            return _verdict(
                OUTCOME_REJECT,
                reason_code,
                _ENTRY_SCOPE_MESSAGES[reason_code],
                use_get_fallback=fallback,
                detected_heading=detected_heading,
                bounded_entry_count=0,
            )
    entries = server_rendered_list_entries(
        evidence.html, evidence.source_url, entry_scope_selector=evidence.entry_scope_selector
    )
    # Site chrome - navigation, header, and footer landmarks - is never a
    # book entry: it is ignored when qualifying entries are found, so it can
    # neither cause a missing-author rejection nor block a valid bounded
    # book list (Issue #334 review). Action/commerce links ("Buy now",
    # "Read more", "Add to cart"), explicit author links, and unmarked
    # author-page links are not book-level links either.
    candidates = [
        entry
        for entry in entries
        if not entry.in_nav
        and not entry.in_header
        and not entry.in_footer
        and entry.link_kind == LINK_KIND_BOOK
        and _http_url(entry.url)
        and entry.title.strip()
    ]
    # Every non-chrome anchor inside a per-entry container (li/article/tr)
    # belongs to that container's book entry. Action links ("Share", "Buy
    # now") and author links stay in the group, while their parser metadata
    # keeps them from becoming primary book-level links and records author
    # evidence separately (Issue #334 review).
    groups: dict[int, list[SourceListEntry]] = {}
    group_order: list[int] = []
    for entry in entries:
        if entry.in_nav or entry.in_header or entry.in_footer:
            continue
        if evidence.entry_scope_selector:
            if not entry.block_tag:
                continue
        elif entry.block_tag not in _PER_ENTRY_BLOCK_TAGS:
            continue
        if entry.block_id not in groups:
            groups[entry.block_id] = []
            group_order.append(entry.block_id)
        groups[entry.block_id].append(entry)
    # A container that holds no book-level link (only action/author links)
    # is not a book entry: it neither counts as a bounded entry nor blocks
    # the rest of the page.
    book_groups = {
        block_id: [
            entry
            for entry in group
            if entry.link_kind == LINK_KIND_BOOK and _http_url(entry.url) and entry.title.strip()
        ]
        for block_id, group in groups.items()
    }
    if not any(book_groups.values()):
        if evidence.entry_scope_selector:
            return _verdict(
                OUTCOME_REJECT,
                REASON_ENTRY_SCOPE_UNLINKED_ENTRIES,
                "declared entry scope exposes no server-rendered linked list entries",
                use_get_fallback=fallback,
                detected_heading=detected_heading,
                bounded_entry_count=0,
            )
        if _js_app_markers(evidence.html):
            return _verdict(
                OUTCOME_DEFER,
                REASON_JS_ONLY,
                "page renders book data with JavaScript only; re-probe later",
                use_get_fallback=fallback,
                detected_heading=detected_heading,
                bounded_entry_count=0,
            )
        if candidates:
            return _verdict(
                OUTCOME_REJECT,
                REASON_UNBOUNDED_ENTRIES,
                "linked titles are not inside a per-entry list container",
                use_get_fallback=fallback,
                detected_heading=detected_heading,
                bounded_entry_count=0,
            )
        return _verdict(
            OUTCOME_REJECT,
            REASON_UNLINKED_ENTRIES,
            "page exposes no server-rendered linked list entries",
            use_get_fallback=fallback,
            detected_heading=detected_heading,
            bounded_entry_count=0,
        )
    bounded_entry_count = sum(1 for group in book_groups.values() if group)
    samples: list[EntrySample] = []
    for block_id in group_order:
        group = book_groups[block_id]
        if not group:
            continue
        # Fail-safe primary-link selection: an entry whose book-kind anchors
        # point at several distinct URLs has no deterministically
        # identifiable primary book link (an unrecognized author-page link
        # next to the title link, for example). It rejects instead of
        # accepting the wrong title/author/URL. Duplicate anchors that share
        # one URL (cover plus title link) are the same book link: every
        # same-URL anchor record is preserved, parser-classified cover labels
        # ("Cover", "Cover of Quantum Worlds") stay out of book-level
        # candidates, and the primary title is selected from the remaining
        # same-URL book group (Issue #334 review).
        distinct_urls = {entry.url for entry in group}
        if len(distinct_urls) > 1:
            return _verdict(
                OUTCOME_REJECT,
                REASON_ENTRY_SHAPE,
                f"bounded entry '{group[0].title}' has multiple distinct book-level links and no deterministic primary",
                use_get_fallback=fallback,
                detected_heading=detected_heading,
                bounded_entry_count=bounded_entry_count,
            )
        # The primary book-level link is the most title-like same-URL book
        # anchor (an accessible cover label never becomes the title). The
        # parser preserves the entry's grounded author evidence so a linked
        # author inside the same container can ground the sample author
        # (Issue #334 review).
        primary = _most_title_like_book_link(group)
        if primary is None:
            return _verdict(
                OUTCOME_REJECT,
                REASON_ENTRY_SHAPE,
                f"bounded entry '{group[0].title}' has no deterministic primary title",
                use_get_fallback=fallback,
                detected_heading=detected_heading,
                bounded_entry_count=bounded_entry_count,
            )
        primary_author = _entry_primary_author(primary)
        if not primary_author:
            author_reason_code = (
                REASON_ENTRY_SCOPE_MISSING_AUTHORS if evidence.entry_scope_selector else REASON_MISSING_AUTHORS
            )
            reason = (
                f"declared entry scope bounded entry '{primary.title}' has no visible primary author"
                if evidence.entry_scope_selector
                else f"bounded entry '{primary.title}' has no visible primary author"
            )
            return _verdict(
                OUTCOME_REJECT,
                author_reason_code,
                reason,
                use_get_fallback=fallback,
                detected_heading=detected_heading,
                bounded_entry_count=bounded_entry_count,
            )
        if len(samples) < MAX_SAMPLES:
            samples.append(EntrySample(title=primary.title, author=primary_author, url=primary.url))
    return _verdict(
        OUTCOME_ACCEPT,
        REASON_OK,
        "server-rendered linked list with visible primary authors accepted",
        use_get_fallback=fallback,
        detected_heading=detected_heading,
        bounded_entry_count=bounded_entry_count,
        samples=tuple(samples),
    )
