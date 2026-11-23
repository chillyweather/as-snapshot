"""Tell an empty page from an extraction that abstained (Issue #740).

Codex extraction regularly answered ``entries: []`` from captures whose rows
its own tool calls had already parsed.  An empty artifact then validated as a
healthy empty page, so the failure was only visible as a missing source.

Where code can count a capture's listing rows itself, an empty extraction
from a capture that holds rows is not evidence of an empty page.  It is
refused as ``unexpected_empty_extraction``, the class BookMarks already used
(Issue #401), so the attempt counts as failed and is eligible for a retry.

Rows are counted only from declarations, never inferred: the BookMarks
listing marker, and the ``ranking_row_selector`` of a declarative ranking
table after its declared title exclusions.  Where eligibility comes from a
detail-page category, a row counts only when its captured detail page passes
the exact category allowlist, so a page whose rows are all excluded is a
correct empty result and is never refused.  Every other source returns
``None`` here, so its empty result is judged exactly as before.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from autoscout.bookmarks_contract import capture_contains_bookmarks_listing_rows
from autoscout.bookmarks_contract import is_bookmarks_source_identity


def _source_declaration(identity: Mapping[str, Any]) -> dict[str, Any]:
    """Return the flat source declaration from a work item or a source."""
    metadata = identity.get("source_metadata")
    if isinstance(metadata, Mapping):
        return {**identity, **metadata}
    return dict(identity)


def capture_listing_row_count(identity: Mapping[str, Any], capture: Mapping[str, Any]) -> int | None:
    """Return the eligible listing rows code can count, or ``None``.

    ``None`` means the source declares no countable listing, which is
    different from zero rows.
    """
    html = capture.get("html")
    if is_bookmarks_source_identity(identity):
        return 1 if capture_contains_bookmarks_listing_rows(identity, capture) else 0
    source = _source_declaration(identity)
    from autoscout.source_collectors.base import contract_family

    if contract_family(source) == "bookinfo-weekly-json":
        from autoscout.bookinfo_ranking import BookInfoError
        from autoscout.bookinfo_ranking import qualifying_rows as bookinfo_qualifying_rows
        from autoscout.bookinfo_ranking import validate_bundle as validate_bookinfo_bundle

        # A work item names the source page as source_url; a source declares it as url.
        declared = {**source, "url": source.get("url") or identity.get("source_url")}
        try:
            return len(
                bookinfo_qualifying_rows(
                    validate_bookinfo_bundle(capture.get("bookinfo_evidence", {}), declared), declared
                )
            )
        except BookInfoError:
            return None
    if contract_family(source) == "ipc-top40-csv":
        from autoscout.ipc_top40_ranking import EVIDENCE_FIELD
        from autoscout.ipc_top40_ranking import IpcTop40Error
        from autoscout.ipc_top40_ranking import qualifying_rows
        from autoscout.ipc_top40_ranking import validate_bundle

        # A work item names the source page as source_url; a source declares it as url.
        declared = {**source, "url": source.get("url") or identity.get("source_url")}
        try:
            return len(qualifying_rows(validate_bundle(capture.get(EVIDENCE_FIELD, {}), declared), declared))
        except IpcTop40Error:
            return None
    if contract_family(source) == "publisher-frontlist":
        from autoscout.publisher_frontlist import frontlist_cards as publisher_frontlist_cards

        try:
            return len(
                publisher_frontlist_cards(str(html or ""), str(source.get("url") or identity.get("source_url", "")))
            )
        except ValueError:
            return None
    if contract_family(source) == "nuevoslibros-frontlist":
        from autoscout.nuevoslibros_frontlist import capture_reference_date
        from autoscout.nuevoslibros_frontlist import recent_cards

        # Only cards in the recent window are expected in a weekly extraction (#993).
        try:
            return len(
                recent_cards(
                    str(html or ""),
                    str(source.get("url") or identity.get("source_url", "")),
                    capture_reference_date(capture),
                )
            )
        except ValueError:
            return None
    if contract_family(source) == "award-list":
        from autoscout.award_list import award_rows

        if not isinstance(html, str) or not html:
            return 0
        try:
            return len(award_rows(html, source))
        except ValueError:
            return None
    if contract_family(source) == "perlentaucher-daily-digest":
        from autoscout.perlentaucher_digest import EVIDENCE_FIELD as PERLENTAUCHER_EVIDENCE_FIELD
        from autoscout.perlentaucher_digest import PerlentaucherError
        from autoscout.perlentaucher_digest import qualifying_rows as perlentaucher_qualifying_rows
        from autoscout.perlentaucher_digest import validate_bundle as validate_perlentaucher_bundle

        declared = {**source, "url": source.get("url") or identity.get("source_url")}
        try:
            return len(
                perlentaucher_qualifying_rows(
                    validate_perlentaucher_bundle(capture.get(PERLENTAUCHER_EVIDENCE_FIELD, {}), declared), declared
                )
            )
        except PerlentaucherError:
            return None
    if contract_family(source) == "kirkus-coming-soon":
        from autoscout.kirkus_listing import EVIDENCE_FIELD as KIRKUS_EVIDENCE_FIELD
        from autoscout.kirkus_listing import KirkusError
        from autoscout.kirkus_listing import qualifying_rows as kirkus_qualifying_rows
        from autoscout.kirkus_listing import validate_bundle as validate_kirkus_bundle

        declared = {**source, "url": source.get("url") or identity.get("source_url")}
        try:
            return len(
                kirkus_qualifying_rows(
                    validate_kirkus_bundle(capture.get(KIRKUS_EVIDENCE_FIELD, {}), declared), declared
                )
            )
        except KirkusError:
            return None
    if contract_family(source) != "ranking-table" or not source.get("ranking_row_selector"):
        return None
    if not isinstance(html, str) or not html:
        return 0
    from autoscout.ranking_detail_route import RankingDetailRoute
    from autoscout.source_page_identity import ranking_rows
    from autoscout.source_page_identity import ranking_rows_after_title_exclusions

    try:
        route = RankingDetailRoute.from_source(source)
        rows = ranking_rows_after_title_exclusions(ranking_rows(html, source), source)
    except (RuntimeError, ValueError):
        return None
    if route is not None and route.has_category:
        # Title exclusions deliberately keep every row for a detail-category
        # route; eligibility is decided by the captured detail page.
        return _rows_in_allowed_detail_category(rows, route, capture)
    return len(rows)


def _rows_in_allowed_detail_category(rows: list[Any], route: Any, capture: Mapping[str, Any]) -> int:
    """Count rows whose own captured detail page passes the category allowlist.

    A row without a matching captured detail page cannot be shown eligible,
    so it is not counted: the count may only ever understate, because an
    overstated count refuses a correct empty result.
    """
    from autoscout.ranking_detail_route import ranking_detail_fields_from_html
    from autoscout.source_proposals import normalize_source_identity

    pages: dict[str, str] = {}
    detail_pages = capture.get("detail_pages")
    for page in detail_pages if isinstance(detail_pages, list) else []:
        if not isinstance(page, Mapping):
            continue
        html = page.get("html")
        if not isinstance(html, str):
            continue
        for key in ("detail_url", "resolved_url", "source_page"):
            url = str(page.get(key, "")).strip()
            if url:
                pages.setdefault(normalize_source_identity(url), html)
    count = 0
    for row in rows:
        url = str(getattr(row, "detail_url", "") or "").strip()
        html = pages.get(normalize_source_identity(url)) if url else None
        if html is None:
            continue
        _values, error = ranking_detail_fields_from_html(html, route, enforce_category_allowlist=True)
        if not error:
            count += 1
    return count


def capture_has_listing_rows(identity: Mapping[str, Any], capture: Mapping[str, Any]) -> bool:
    """Return whether an empty extraction from *capture* contradicts it."""
    return bool(capture_listing_row_count(identity, capture))


# An extractor's stated reason for returning nothing is diagnostic text only.
MAX_EMPTY_REASON_CHARS = 300


def extractor_empty_reason(extraction_data: Any) -> str:
    """Return the bounded one-line ``empty_reason`` an extractor supplied."""
    if not isinstance(extraction_data, Mapping):
        return ""
    raw = extraction_data.get("empty_reason")
    if not isinstance(raw, str):
        return ""
    return " ".join(raw.split())[:MAX_EMPTY_REASON_CHARS]


__all__ = [
    "MAX_EMPTY_REASON_CHARS",
    "capture_has_listing_rows",
    "capture_listing_row_count",
    "extractor_empty_reason",
]
