"""Cundill History Prize 2026 longlist source-specific tests (Issue #366).

The shared exact-book-list contract battery in
tests/test_exact_book_list_source_contract.py covers grounding, metadata
safety, replay behavior, and four-config parity for this source. This file
only pins genuinely source-specific facts: the bounded 15-entry longlist
shape, the exact H1 identity, the book-level publisher-link pattern, and
the deterministic exclusion of navigation/topic-only links.
"""

from __future__ import annotations

import json
from pathlib import Path

from autoscout.source_page_identity import link_anchor_pairs
from autoscout.source_page_identity import parse_heading_texts

ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = ROOT / "data" / "current-weekly-pilot"
PAGE_FIXTURE = FIXTURES_DIR / "cundill-history-prize-2026-longlist-page.json"
EXTRACTION_FIXTURE = FIXTURES_DIR / "cundill-history-prize-2026-longlist-extraction.json"

# Navigation/topic-only link present in the live page capture; it must never
# be extracted as a book-level entry.
NAVIGATION_LINK_TEXT = "The Cundill History Hub"
NAVIGATION_LINK_URL = "https://www.cundillprize.com/cundill-history-hub"


def _page() -> dict:
    return json.loads(PAGE_FIXTURE.read_text(encoding="utf-8"))


def _extraction() -> dict:
    return json.loads(EXTRACTION_FIXTURE.read_text(encoding="utf-8"))


def test_page_has_exact_year_specific_h1_identity() -> None:
    headings = parse_heading_texts(_page()["html"])
    assert headings == ["The 2026 Cundill History Prize Longlist"]


def test_page_exposes_exactly_15_book_level_publisher_links() -> None:
    links = link_anchor_pairs(_page()["html"], _page()["source_page"])
    book_links = [(href, text) for href, text in links if href != NAVIGATION_LINK_URL]
    assert len(book_links) == 15
    # Every longlist row carries a book-level publisher link, never the
    # news-page URL itself.
    for href, _text in book_links:
        assert href != _page()["source_url"]


def test_extraction_keeps_only_the_15_entry_shape_and_real_longlist_books() -> None:
    mentions = _extraction()["mentions"]
    assert len(mentions) == 6
    by_title = {m["title"]: m for m in mentions}
    assert "The Painter’s Fire: A Forgotten History of the Artists Who Championed the American Revolution" in by_title
    assert by_title["The Painter’s Fire: A Forgotten History of the Artists Who Championed the American Revolution"][
        "primary_authors"
    ] == ["Zara Anishanslin"]
    assert "The Fallen: The Magdalene Laundries and Ireland’s Legacy of Silence" in by_title
    assert "Korean Messiah: Kim Il Sung and the Christian Roots of North Korea’s Personality Cult" in by_title
    assert by_title["Korean Messiah: Kim Il Sung and the Christian Roots of North Korea’s Personality Cult"][
        "primary_authors"
    ] == ["Jonathan Cheng"]


def test_evidence_links_are_book_level_publisher_pages() -> None:
    page_links = {href for href, _text in link_anchor_pairs(_page()["html"], _page()["source_page"])}
    for mention in _extraction()["mentions"]:
        assert mention["evidence_url"] in page_links
        assert mention["evidence_url"] != mention["source_url"]


def test_navigation_and_topic_only_links_are_not_extracted() -> None:
    html = _page()["html"]
    assert NAVIGATION_LINK_TEXT in html
    titles = {m["title"] for m in _extraction()["mentions"]}
    assert NAVIGATION_LINK_TEXT not in titles
