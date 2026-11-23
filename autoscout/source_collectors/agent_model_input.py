"""Bound a captured page to the model's input budget."""

from __future__ import annotations

import html as html_lib
import re
from typing import Any

from autoscout import nuevoslibros_frontlist
from autoscout import publisher_frontlist
from autoscout.ranking_detail_route import RankingDetailRoute
from autoscout.ranking_detail_route import ranking_detail_scope
from autoscout.ranking_evidence import RankingPageEvidence
from autoscout.ranking_identity import ranking_identity_declared
from autoscout.source_collectors.base import _collector_error
from autoscout.source_collectors.base import contract_family
from autoscout.source_collectors.row_families import row_family
from autoscout.source_page_identity import ranking_detail_skipped_entries
from autoscout.source_page_identity import ranking_page_identity
from autoscout.source_page_identity import ranking_rows


def _bound_model_html_parts(
    parts: list[str], max_chars: int, *, omitted_indices: set[int] | None = None
) -> tuple[list[str], list[bool], list[bool], int, bool]:
    """Bound ordered model payload parts under one global character budget."""
    remaining = max(0, max_chars)
    omitted_indices = omitted_indices or set()
    bounded: list[str] = []
    truncated_flags: list[bool] = []
    omitted_flags: list[bool] = []
    omitted = 0
    for index, part in enumerate(parts):
        if index in omitted_indices or remaining <= 0:
            bounded.append("")
            truncated_flags.append(False)
            omitted_flags.append(True)
            omitted += 1
            continue
        value = part[:remaining]
        remaining -= len(value)
        truncated = len(value) < len(part)
        bounded.append(value)
        truncated_flags.append(truncated)
        omitted_flags.append(truncated)
        if truncated:
            omitted += 1
    return bounded, truncated_flags, omitted_flags, omitted, any(truncated_flags)


def _model_input_debug(
    *,
    raw_html_chars: int,
    compacted_chars: int,
    bounded_chars: int,
    compacted: bool,
    truncated: bool,
    raw_detail_page_count: int,
    raw_detail_chars: int,
    detail_pages_included: int,
    detail_pages_omitted: int,
) -> dict[str, Any]:
    """Build the stable bounded model-data diagnostic dictionary."""
    return {
        "raw_model_input_chars": raw_html_chars,
        "compacted_model_input_chars": compacted_chars,
        "model_input_chars": bounded_chars,
        "model_input_compacted": compacted,
        "model_input_truncated": truncated,
        "raw_model_detail_page_count": raw_detail_page_count,
        "raw_model_detail_chars": raw_detail_chars,
        "model_detail_pages_included": detail_pages_included,
        "model_detail_pages_omitted": detail_pages_omitted,
    }


# One frontlist row costs the model about this many output tokens once its
# identity, provenance and translation helper fields are written (#901).
FRONTLIST_OUTPUT_TOKENS_PER_CARD = 250


def _frontlist_model_data(
    page_data: dict[str, Any], source: dict[str, Any], max_chars: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reduce a frontlist page to the cards its family reads, and refuse one over the input budget."""
    markup = str(page_data.get("html", ""))
    url = str(source.get("url", ""))
    try:
        if contract_family(source) == "publisher-frontlist":
            compacted = publisher_frontlist.replay_html(
                markup, url, [card.url for card in publisher_frontlist.frontlist_cards(markup, url)]
            )
            budget_error = "Publisher publisher_cards exceed model input budget"
        else:
            # Only the cards dated in the recent window reach the model (#993).
            recent = nuevoslibros_frontlist.recent_cards(
                markup, url, nuevoslibros_frontlist.capture_reference_date(page_data)
            )
            compacted = nuevoslibros_frontlist.replay_html(markup, url, [card.url for card in recent])
            budget_error = "NuevosLibros cards exceed model input budget"
    except ValueError as exc:
        raise _collector_error(str(exc)) from exc
    if len(compacted) > max_chars:
        raise _collector_error(budget_error)
    return {**page_data, "html": compacted, "text": ""}, {
        "raw_model_input_chars": len(markup),
        "model_input_chars": len(compacted),
        "model_input_compacted": True,
        "model_input_truncated": False,
    }


def frontlist_model_batches(
    page_data: dict[str, Any], source: dict[str, Any], max_output_tokens: int
) -> list[dict[str, Any]]:
    """Split a NuevosLibros frontlist into model requests whose answers fit the output budget.

    A frontlist can hold hundreds of cards; one request would truncate its
    JSON or leave books out. Every batch keeps whole cards with their own
    dates, so the merged extraction covers each card exactly once.
    """
    markup = str(page_data.get("html", ""))
    url = str(source.get("url", ""))
    try:
        recent = nuevoslibros_frontlist.recent_cards(
            markup, url, nuevoslibros_frontlist.capture_reference_date(page_data)
        )
    except ValueError as exc:
        raise _collector_error(str(exc)) from exc
    urls = [card.url for card in recent]
    size = max(1, max_output_tokens // FRONTLIST_OUTPUT_TOKENS_PER_CARD)
    return [
        {**page_data, "html": nuevoslibros_frontlist.replay_html(markup, url, urls[start : start + size]), "text": ""}
        for start in range(0, len(urls), size)
    ]


def model_bounded_page_data(
    page_data: dict[str, Any],
    max_chars: int,
    *,
    source: dict[str, Any] | None = None,
    detail_route: RankingDetailRoute | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    row = row_family(source) if source is not None else None
    if row is not None and source is not None:
        text = row.model_text(page_data, source)
        if len(text) > max_chars:
            raise _collector_error(row.budget_error)
        return {"source_page": page_data["source_page"], "html": "", "text": text}, {"model_input_truncated": False}
    if source is not None and contract_family(source) in {"publisher-frontlist", "nuevoslibros-frontlist"}:
        return _frontlist_model_data(page_data, source, max_chars)
    page_evidence = RankingPageEvidence.from_mapping(page_data)
    html_text = page_evidence.rows_html
    if source is not None:
        detail_route = detail_route or RankingDetailRoute.from_source(source)
    if source is not None and detail_route is not None:
        return _bounded_ranking_detail_model_data(page_data, source, max_chars, detail_route=detail_route)
    compacted_html = compact_model_html(html_text)
    identity_html = page_evidence.identity_html if ranking_identity_declared(source or {}) else ""
    compacted_identity = compact_model_html(identity_html)
    detail_pages = page_data.get("detail_pages")
    raw_detail_pages = detail_pages if isinstance(detail_pages, list) else []
    valid_detail_pages = [page for page in raw_detail_pages if isinstance(page, dict)]
    compacted_details = [compact_model_html(str(page.get("html", ""))) for page in valid_detail_pages]
    invalid_count = len(raw_detail_pages) - len(valid_detail_pages)
    identity_offset = 1 if identity_html else 0
    bounded_parts, truncated_flags, omitted_flags, omitted, detail_truncated = _bound_model_html_parts(
        [compacted_html, *([compacted_identity] if identity_html else []), *compacted_details], max_chars
    )
    bounded_html = bounded_parts[0]
    bounded_identity = bounded_parts[1] if identity_html else ""
    detail_offset = 1 + identity_offset
    bounded_details = [
        {**page, "html": bounded, "omitted_from_model_input": omitted_flags[index + detail_offset]}
        for index, (page, bounded) in enumerate(zip(valid_detail_pages, bounded_parts[detail_offset:], strict=True))
    ]
    omitted += invalid_count
    debug = _model_input_debug(
        raw_html_chars=len(html_text) + len(identity_html),
        compacted_chars=len(compacted_html) + len(compacted_identity) + sum(len(value) for value in compacted_details),
        bounded_chars=len(bounded_html)
        + len(bounded_identity)
        + sum(len(str(page.get("html", ""))) for page in bounded_details),
        compacted=len(compacted_html) != len(html_text)
        or len(compacted_identity) != len(identity_html)
        or any(
            len(compacted_details[index]) != len(str(page.get("html", "")))
            for index, page in enumerate(valid_detail_pages)
        ),
        truncated=len(compacted_html) > max_chars
        or len(compacted_identity) > max_chars
        or detail_truncated
        or omitted > 0,
        raw_detail_page_count=len(raw_detail_pages),
        raw_detail_chars=sum(len(value) for value in compacted_details),
        detail_pages_included=sum(1 for page in bounded_details if not page.get("omitted_from_model_input")),
        detail_pages_omitted=omitted,
    )
    model_page_data = {**page_data, "html": bounded_html}
    if identity_html:
        model_page_data["identity_html"] = bounded_identity
    if raw_detail_pages:
        model_page_data["detail_pages"] = bounded_details
        model_page_data["detail_pages_complete"] = (
            page_data.get("detail_pages_complete", True) is not False and omitted == 0 and not detail_truncated
        )
    return model_page_data, debug


def _bounded_ranking_detail_model_data(
    page_data: dict[str, Any],
    source: dict[str, Any],
    max_chars: int,
    *,
    detail_route: RankingDetailRoute,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build compact list-row and bounded-scope evidence for detail rankings."""
    page_evidence = RankingPageEvidence.from_mapping(page_data)
    raw_html = page_evidence.rows_html
    raw_identity_html = page_evidence.identity_html
    # A row whose detail page was skipped (Issue #1163) has no evidence to
    # extract from, so the model never sees it.
    skipped_urls = {str(item.get("detail_url", "")).strip() for item in ranking_detail_skipped_entries(page_data)} - {
        ""
    }
    rows = [
        row
        for row in ranking_rows(raw_html, source, detail_route=detail_route)
        if not any(detail_route.same_url_safely(row.detail_url, url) for url in skipped_urls)
    ]
    identity = ranking_page_identity(raw_html, source, identity_html=raw_identity_html)
    heading = identity.heading if identity is not None else str(source.get("expected_page_heading", "")).strip()
    statement = str(source.get("ranking_source_statement", "")).strip()
    list_parts = [f'<h1 class="heading">{html_lib.escape(heading)}</h1>'] if heading else []
    if statement:
        list_parts.append(f"<p>{html_lib.escape(statement)}</p>")
    for row in rows:
        list_parts.append(
            '<div class="ranking-row">'
            f'<span class="rank">{html_lib.escape(row.rank)}</span>'
            f'<span class="title">{html_lib.escape(row.title)}</span>'
            f'<span class="author">{html_lib.escape(row.author)}</span>'
            f'<a class="detail-link" href="{html_lib.escape(row.detail_url, quote=True)}">'
            f"{html_lib.escape(row.detail_url)}</a></div>"
        )
    compact_list = "".join(list_parts)
    raw_detail_pages = page_data.get("detail_pages")
    detail_pages = raw_detail_pages if isinstance(raw_detail_pages, list) else []
    compact_details: list[dict[str, Any]] = []
    for raw_page in detail_pages:
        if not isinstance(raw_page, dict):
            compact_details.append({"html": "", "scope_complete": False})
            continue
        detail_html = str(raw_page.get("html", ""))
        scope = ranking_detail_scope(detail_html, detail_route.scope_selector)
        if scope.failure_reason:
            bounded_scope = ""
        else:
            bounded_scope = scope.html
        compact_details.append(
            {
                "source_page": str(raw_page.get("source_page", "")),
                "detail_url": str(raw_page.get("detail_url", "")),
                "resolved_url": str(raw_page.get("resolved_url", "")),
                "html": bounded_scope,
                "scope_complete": not bool(scope.failure_reason),
            }
        )
    omitted_indices = {
        index + 1 for index, detail in enumerate(compact_details) if not detail.get("scope_complete", True)
    }
    compact_identity = compact_model_html(raw_identity_html) if ranking_identity_declared(source) else ""
    identity_offset = 1 if compact_identity else 0
    shifted_omitted_indices = {index + identity_offset for index in omitted_indices}
    bounded_parts, truncated_flags, omitted_flags, omitted, detail_truncated = _bound_model_html_parts(
        [
            compact_list,
            *([compact_identity] if compact_identity else []),
            *[str(detail.get("html", "")) for detail in compact_details],
        ],
        max_chars,
        omitted_indices=shifted_omitted_indices,
    )
    bounded_list = bounded_parts[0]
    bounded_identity = bounded_parts[1] if compact_identity else ""
    list_complete = not truncated_flags[0] and len(bounded_list) == len(compact_list)
    detail_offset = 1 + identity_offset
    bounded_details = [
        {
            **detail,
            "html": bounded,
            "omitted_from_model_input": omitted_flags[index + detail_offset],
        }
        for index, (detail, bounded) in enumerate(zip(compact_details, bounded_parts[detail_offset:], strict=True))
    ]
    model_page_data = {**page_data, "html": bounded_list}
    if compact_identity:
        model_page_data["identity_html"] = bounded_identity
    model_page_data["detail_pages"] = bounded_details
    model_page_data["detail_pages_complete"] = (
        bool(detail_pages)
        and page_data.get("detail_pages_complete", True) is not False
        and list_complete
        and omitted == 0
        and not detail_truncated
    )
    debug = _model_input_debug(
        raw_html_chars=len(raw_html) + len(raw_identity_html),
        compacted_chars=len(compact_list)
        + len(compact_identity)
        + sum(len(str(item.get("html", ""))) for item in compact_details),
        bounded_chars=len(bounded_list)
        + len(bounded_identity)
        + sum(len(str(item.get("html", ""))) for item in bounded_details),
        compacted=True,
        truncated=not list_complete or detail_truncated or omitted > 0,
        raw_detail_page_count=len(detail_pages),
        raw_detail_chars=sum(len(str(item.get("html", ""))) for item in compact_details),
        detail_pages_included=sum(1 for item in bounded_details if not item.get("omitted_from_model_input")),
        detail_pages_omitted=omitted,
    )
    return model_page_data, debug


def compact_model_html(markup: str) -> str:
    without_blocks = re.sub(
        r"(?is)<(script|style|svg|noscript)\b[^>]*>.*?</\1>",
        " ",
        markup,
    )
    without_comments = re.sub(r"(?s)<!--.*?-->", " ", without_blocks)
    return re.sub(r"\s+", " ", without_comments).strip()
