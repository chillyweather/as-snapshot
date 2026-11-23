"""Strict source-specific contracts for the BookMarks category page."""

from __future__ import annotations

from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any

BOOKMARKS_SOURCE_NAME = "BookMarks (LitHub) – New Nonfiction"
BOOKMARKS_SOURCE_URL = "https://bookmarks.reviews/category/non-fiction/"


def is_bookmarks_source_identity(identity: Mapping[str, Any]) -> bool:
    """Return whether *identity* is exactly the configured BookMarks source."""
    source_name = identity.get("source_name", identity.get("name"))
    source_url = identity.get("source_url", identity.get("url"))
    return (
        isinstance(source_name, str)
        and isinstance(source_url, str)
        and source_name == BOOKMARKS_SOURCE_NAME
        and source_url == BOOKMARKS_SOURCE_URL
    )


class _BookMarksListingParser(HTMLParser):
    """Detect BookMarks listing rows without interpreting their contents."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.has_listing_row = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "div":
            return
        classes: set[str] = set()
        for name, value in attrs:
            if name.casefold() == "class" and isinstance(value, str):
                classes.update(value.split())
        if "latest_book" in classes:
            self.has_listing_row = True


def capture_contains_bookmarks_listing_rows(identity: Mapping[str, Any], capture: Mapping[str, Any]) -> bool:
    """Return whether the exact BookMarks capture contains a listing row.

    The check is intentionally narrow and fail-closed.
    It recognizes only the configured BookMarks identity and the source's
    ``div.latest_book`` listing-row marker in the bound capture HTML.
    """
    if not is_bookmarks_source_identity(identity):
        return False
    html = capture.get("html")
    if not isinstance(html, str) or not html:
        return False
    parser = _BookMarksListingParser()
    try:
        parser.feed(html)
        parser.close()
    except (RuntimeError, ValueError):
        return False
    return parser.has_listing_row
