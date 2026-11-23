"""Shared declared Boklista POST ranking contract for focused tests."""

from __future__ import annotations

from urllib.parse import urlsplit


def boklista_post_ranking_contract(url: str, *, expected_raw_row_count: int = 26) -> dict[str, object]:
    """Build the bounded Boklista POST, identity, row, and visibility contract."""
    parsed = urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return {
        "metadata_ranking_table": True,
        "ranking_evidence_url": "source_page",
        "visible_title_selector": ".page-title",
        "ranking_row_selector": ".book-row-cover > .book-row",
        "ranking_rank_source": "document_order",
        "ranking_title_selector": ".col1 > .book-title",
        "ranking_author_selector": ".col1 > .author-name",
        "ranking_request_method": "POST",
        "ranking_request_page_field": "paged",
        "ranking_request_max_pages": 4,
        "ranking_response_html_field": "html",
        "ranking_response_last_page_field": "last",
        "ranking_identity_url_template": (f"{origin}/book-list/{{year}}-{{week}}-generell-litteratur-voksne/"),
        "ranking_identity_fields": ["year=sel_year", "week=sel_week"],
        "ranking_request_body": [
            "action=load_book_archive_list",
            "bookcat=177",
            "refresh=1",
            "paged=1",
        ],
        "ranking_identifier_selector": ".col2 > .isbn",
        "ranking_category_marker": "Generell litteratur voksne",
        "ranking_reporting_period_undated": True,
        "ranking_expected_raw_row_count": expected_raw_row_count,
        "adult_nonfiction_exclusions": ["barn"],
    }
