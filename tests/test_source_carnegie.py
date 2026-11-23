"""Andrew Carnegie Medals for Excellence - Nonfiction 2026 source-specific tests (Issue #366).

The shared exact-book-list contract battery in
tests/test_exact_book_list_source_contract.py covers grounding, metadata
safety, replay behavior, and four-config parity for this source. This file
only pins genuinely source-specific facts: the H1 identity, the winner
inside the finalists block, the deterministic fiction-section exclusion,
and the surname-first longlist author rendering.
"""

from __future__ import annotations

import json
from pathlib import Path

from autoscout.source_page_identity import parse_heading_texts

ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = ROOT / "data" / "current-weekly-pilot"
PAGE_FIXTURE = FIXTURES_DIR / "andrew-carnegie-medals-2026-nonfiction-page.json"
EXTRACTION_FIXTURE = FIXTURES_DIR / "andrew-carnegie-medals-2026-nonfiction-extraction.json"

# Fiction entries present in the page capture as genuine mixed-page leakage.
# They must never appear in extraction.
FICTION_TITLES = ("A Guardian and a Thief", "The Unworthy", "Flashlight", "We Do Not Part")

WINNER_TITLE = "Things in Nature Merely Grow"
WINNER_AUTHOR = "Yiyun Li"


def _page() -> dict:
    return json.loads(PAGE_FIXTURE.read_text(encoding="utf-8"))


def _extraction() -> dict:
    return json.loads(EXTRACTION_FIXTURE.read_text(encoding="utf-8"))


def test_page_has_exact_heading_identity() -> None:
    headings = parse_heading_texts(_page()["html"])
    assert headings[0] == "2026 Winners"
    assert headings.count("2026 Winners") == 1


def test_page_carries_nonfiction_and_fiction_sections() -> None:
    html = _page()["html"]
    for marker in (
        "Nonfiction Winner",
        "Nonfiction Finalists",
        "Nonfiction Longlist",
        "Fiction Winner",
        "Fiction Finalists",
        "Fiction Longlist",
    ):
        assert marker in html


def test_fiction_section_titles_are_in_page_but_never_extracted() -> None:
    html = _page()["html"]
    titles = {m["title"] for m in _extraction()["mentions"]}
    for title in FICTION_TITLES:
        assert title in html
        assert title not in titles


def test_winner_is_listed_inside_finalists_block_and_carries_finalist_strength() -> None:
    html = _page()["html"]
    finalists_block = html.split("Nonfiction Finalists")[1].split("Nonfiction Longlist")[0]
    assert WINNER_TITLE in finalists_block
    mention = next(m for m in _extraction()["mentions"] if m["title"] == WINNER_TITLE)
    assert mention["primary_authors"] == [WINNER_AUTHOR]
    assert mention["signal_strength"] == "finalist"
    assert "Finalists block" in mention["evidence_note"]


def test_nonfiction_mentions_use_finalist_strength_and_grounding_links() -> None:
    for mention in _extraction()["mentions"]:
        assert mention["signal_strength"] == "finalist"
        assert mention["signal_type"] == "award"
        assert mention["candidate_category"] == "adult nonfiction"
        assert "booklistonline.com" in mention["evidence_url"]


def test_longlist_mentions_preserve_surname_first_author_rendering() -> None:
    by_title = {m["title"]: m for m in _extraction()["mentions"]}
    assert by_title["The Martians: The True Story of an Alien Craze that Captured Turn-of-the-Century America"][
        "primary_authors"
    ] == ["Baron, David"]
    assert by_title["We the People: A History of the U.S. Constitution"]["primary_authors"] == ["Lepore, Jill"]
