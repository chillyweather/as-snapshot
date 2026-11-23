"""Card-shaped exact-book-list grounding (Issue #715).

An exact-book-list mention requires a matching Evidence URL, a title equal to
the whole anchor or one whole block segment (including an author-prefixed
title), and every primary author in that link's bounded entry context. A
whole book card can ground through its title block without accepting title
fragments, year metadata, or the author name as a title.

These tests pin the predicate through its public seam
(``exact_book_list_mention_is_grounded``) against synthetic first-party
shaped markup, plus the real failing evidence: all thirteen Baillie Gifford
Prize 2026 Longlist mentions of the committed 2026-09-20 extraction
grounded against the committed 2026-09-20 capture (0/13 before #715).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from autoscout.config import load_validated_config
from autoscout.source_page_identity import exact_book_list_mention_is_grounded
from autoscout.source_page_identity import link_anchor_pairs

ROOT = Path(__file__).resolve().parents[1]
# Byte-identical pinned copies of the committed 2026-09-20 run artifacts
# (the capture and extraction under the weekly-run alias). Tests must not
# read the moving weekly-run alias (issue #475), so the failing evidence is
# retained here like the other issue-*-retained-run fixtures.
CAPTURE_PATH = (
    ROOT
    / "tests"
    / "fixtures"
    / "issue-715-retained-capture"
    / "codex-capture-baillie-gifford-prize-2026-longlist-a081bf19.json"
)
EXTRACTION_PATH = (
    ROOT
    / "tests"
    / "fixtures"
    / "issue-715-retained-capture"
    / "baillie-gifford-prize-2026-longlist-a081bf19-codex-extraction.json"
)
# The year page as served on 2026-10-05, after the shortlist was published
# (issue #762). The replay page fixture is derived from this capture.
STAGE_CAPTURE_PATH = (
    ROOT / "tests" / "fixtures" / "issue-996-retained-capture" / "baillie-gifford-prize-2026-year-page-2026-11-08.json"
)
SOURCE_PAGE = "https://example.test/prize/2026"
STAY_ALIVE_URL = "https://example.test/books/stay-alive"


def _grounded(html: str, *, title: str, authors: list[str], evidence_url: str) -> bool:
    return exact_book_list_mention_is_grounded(
        html,
        SOURCE_PAGE,
        title=title,
        authors=authors,
        evidence_url=evidence_url,
    )


# ── Accepted anchor shapes ─────────────────────────────────────────────────


def test_whole_book_card_anchor_grounds() -> None:
    """One link wrapping year, title and author grounds: the modern listing
    card shape the Baillie Gifford page serves (issue #715)."""
    html = (
        '<article class="listing">'
        '<a class="listing__inner" href="https://example.test/books/baldwin-a-love-story">'
        '<p class="listing__meta">2026</p>'
        '<h2 class="listing__title">Baldwin: A Love Story</h2>'
        '<p class="listing__sub">Nicholas Boggs</p>'
        "</a></article>"
    )
    assert _grounded(
        html,
        title="Baldwin: A Love Story",
        authors=["Nicholas Boggs"],
        evidence_url=("https://example.test/books/baldwin-a-love-story"),
    )


@pytest.mark.parametrize("title", ["Baldwin", "2026", "Love", "Nicholas Boggs"])
def test_card_metadata_and_title_fragments_are_refused(title: str) -> None:
    url = "https://example.test/books/baldwin-a-love-story"
    html = (
        '<article class="listing">'
        f'<a href="{url}"><p>2026</p><h2>Baldwin: A Love Story</h2><p>Nicholas Boggs</p></a>'
        "</article>"
    )
    assert not _grounded(html, title=title, authors=["Nicholas Boggs"], evidence_url=url)


@pytest.mark.parametrize("title_tag", ["h2", "div", "p", "span"])
def test_leading_card_year_is_refused_whatever_element_holds_the_title(title_tag: str) -> None:
    """A leading year is listing metadata even when the title that follows it
    is not a heading (PR #729 re-review)."""
    html = f'<li><a href="{STAY_ALIVE_URL}"><p>2026</p><{title_tag}>Actual Book</{title_tag}><p>Jane Doe</p></a></li>'
    assert not _grounded(html, title="2026", authors=["Jane Doe"], evidence_url=STAY_ALIVE_URL)
    assert _grounded(html, title="Actual Book", authors=["Jane Doe"], evidence_url=STAY_ALIVE_URL)


def test_numeric_book_title_in_card_heading_can_ground() -> None:
    html = f'<li><a href="{STAY_ALIVE_URL}"><p>2026</p><h2>1984</h2><p>George Orwell</p></a></li>'
    assert _grounded(html, title="1984", authors=["George Orwell"], evidence_url=STAY_ALIVE_URL)


def test_leading_numeric_heading_is_a_book_title() -> None:
    html = f'<li><a href="{STAY_ALIVE_URL}"><h2>1984</h2><p>George Orwell</p></a></li>'
    assert _grounded(html, title="1984", authors=["George Orwell"], evidence_url=STAY_ALIVE_URL)


def test_inline_formatting_keeps_punctuation_attached_in_both_anchor_readers() -> None:
    html = (
        f'<li><a href="{STAY_ALIVE_URL}"><h3><b>Dune</b>, Book 1</h3></a><span class="author">Frank Herbert</span></li>'
    )
    assert link_anchor_pairs(html, SOURCE_PAGE) == [(STAY_ALIVE_URL, "Dune, Book 1")]
    assert _grounded(html, title="Dune, Book 1", authors=["Frank Herbert"], evidence_url=STAY_ALIVE_URL)


@pytest.mark.parametrize(
    ("inner_html", "anchor_text", "title", "author"),
    [
        ('<span class="dc">T</span>he Road', "The Road", "The Road", "Cormac McCarthy"),
        ("<span>Jean</span>-Paul Sartre", "Jean-Paul Sartre", "Jean-Paul Sartre", "Test Author"),
        ("<i>Beloved</i>, a novel", "Beloved, a novel", "Beloved, a novel", "Toni Morrison"),
        (
            "<p>2026</p><h2>Baldwin: A Love Story</h2><p>Nicholas Boggs</p>",
            "2026 Baldwin: A Love Story Nicholas Boggs",
            "Baldwin: A Love Story",
            "Nicholas Boggs",
        ),
    ],
)
def test_anchor_readers_agree_on_rendered_inline_and_block_text(
    inner_html: str, anchor_text: str, title: str, author: str
) -> None:
    html = f'<li><a href="{STAY_ALIVE_URL}">{inner_html}</a><span class="author">{author}</span></li>'
    assert link_anchor_pairs(html, SOURCE_PAGE) == [(STAY_ALIVE_URL, anchor_text)]
    assert _grounded(html, title=title, authors=[author], evidence_url=STAY_ALIVE_URL)


def test_adjacent_inline_siblings_join_as_rendered() -> None:
    """Adjacent inline siblings render with no inserted space."""
    html = (
        f'<li><a href="{STAY_ALIVE_URL}"><span>Stay</span><span>Alive</span></a>'
        '<span class="author"> Ian Buruma </span></li>'
    )
    assert link_anchor_pairs(html, SOURCE_PAGE) == [(STAY_ALIVE_URL, "StayAlive")]
    assert _grounded(html, title="StayAlive", authors=["Ian Buruma"], evidence_url=STAY_ALIVE_URL)
    assert not _grounded(html, title="Stay Alive", authors=["Ian Buruma"], evidence_url=STAY_ALIVE_URL)


def test_whitespace_between_inline_siblings_is_retained() -> None:
    html = f'<li><a href="{STAY_ALIVE_URL}"><i>Stay</i> <i>Alive</i></a><span class="author"> Ian Buruma </span></li>'
    assert link_anchor_pairs(html, SOURCE_PAGE) == [(STAY_ALIVE_URL, "Stay Alive")]
    assert _grounded(html, title="Stay Alive", authors=["Ian Buruma"], evidence_url=STAY_ALIVE_URL)


def test_bare_title_anchor_grounds() -> None:
    html = f'<li><a href="{STAY_ALIVE_URL}">Stay Alive</a><span class="author"> Ian Buruma </span></li>'
    assert _grounded(html, title="Stay Alive", authors=["Ian Buruma"], evidence_url=STAY_ALIVE_URL)


def test_author_prefixed_anchor_grounds() -> None:
    """An author prefix qualifies the exact title after its first colon."""
    html = f'<li><a href="{STAY_ALIVE_URL}">Ian Buruma: Stay Alive</a></li>'
    assert _grounded(html, title="Stay Alive", authors=["Ian Buruma"], evidence_url=STAY_ALIVE_URL)


def test_author_prefixed_title_inside_card_heading_grounds() -> None:
    html = f'<li><a href="{STAY_ALIVE_URL}"><p>2026</p><h2>Nicholas Boggs: Baldwin: A Love Story</h2></a></li>'
    assert _grounded(html, title="Baldwin: A Love Story", authors=["Nicholas Boggs"], evidence_url=STAY_ALIVE_URL)


def test_author_prefix_must_name_every_primary_author() -> None:
    html = f'<li><a href="{STAY_ALIVE_URL}">Someone Else: Stay Alive</a><span class="author">Ian Buruma</span></li>'
    assert not _grounded(html, title="Stay Alive", authors=["Ian Buruma"], evidence_url=STAY_ALIVE_URL)


def test_earlier_same_url_link_without_author_does_not_block_a_later_entry() -> None:
    """Every same-URL link is judged on its own: an earlier feature link
    whose text contains the title but whose context lacks the author must not
    stop a later, fully grounded entry for the same book (#715 review)."""
    html = (
        f'<li><a href="{STAY_ALIVE_URL}">Feature: Stay Alive</a></li>'
        f'<li><a href="{STAY_ALIVE_URL}">Stay Alive</a><span class="author"> Ian Buruma </span></li>'
    )
    assert _grounded(html, title="Stay Alive", authors=["Ian Buruma"], evidence_url=STAY_ALIVE_URL)


# ── Refusals ───────────────────────────────────────────────────────────────


def test_title_outside_the_matching_link_is_refused() -> None:
    """The title on the page but not inside the link matching the Evidence
    URL is refused: whole-segment containment must not degrade into
    "anywhere on the page"."""
    html = (
        f'<li><a href="{STAY_ALIVE_URL}">A completely different anchor</a>'
        '<span class="author"> Ian Buruma </span></li>'
        '<li><a href="https://example.test/books/other">Stay Alive</a>'
        '<span class="author"> Someone Else </span></li>'
    )
    assert not _grounded(html, title="Stay Alive", authors=["Ian Buruma"], evidence_url=STAY_ALIVE_URL)


def test_author_absent_from_entry_context_is_refused() -> None:
    """A grounded title whose author appears nowhere in the link's bounded
    entry context is refused: author-in-context is the binding."""
    html = f'<article><a href="{STAY_ALIVE_URL}">Stay Alive</a></article><article>Ian Buruma</article>'
    assert not _grounded(html, title="Stay Alive", authors=["Ian Buruma"], evidence_url=STAY_ALIVE_URL)


def test_evidence_url_absent_from_capture_is_refused() -> None:
    html = '<li><a href="https://example.test/books/other">Stay Alive</a><span class="author"> Ian Buruma </span></li>'
    assert not _grounded(html, title="Stay Alive", authors=["Ian Buruma"], evidence_url=STAY_ALIVE_URL)


def test_partial_token_title_match_is_refused() -> None:
    """A title that matches only part of an anchor token is refused:
    ``Art`` must not ground inside ``Artemis Rising Jane Doe``."""
    html = '<li><a href="https://example.test/books/artemis-rising">Artemis Rising Jane Doe</a></li>'
    assert not _grounded(
        html, title="Art", authors=["Jane Doe"], evidence_url="https://example.test/books/artemis-rising"
    )


def test_truncated_title_on_bare_anchor_is_refused() -> None:
    html = f'<li><a href="{STAY_ALIVE_URL}">Days of Love and Rage</a><span class="author">Ian Buruma</span></li>'
    assert not _grounded(html, title="Days of Love", authors=["Ian Buruma"], evidence_url=STAY_ALIVE_URL)


def test_same_url_teaser_with_author_nearby_is_refused() -> None:
    html = (
        f'<li><a href="{STAY_ALIVE_URL}">Review: Why Stay Alive matters</a><span class="author">Ian Buruma</span></li>'
    )
    assert not _grounded(html, title="Stay Alive", authors=["Ian Buruma"], evidence_url=STAY_ALIVE_URL)


# ── Real failing evidence: the thirteen Baillie Gifford longlist books ─────


def test_all_thirteen_longlist_mentions_ground_in_committed_capture() -> None:
    """Every mention of the committed 2026-09-20 extraction grounds against
    the committed 2026-09-20 capture (0/13 before issue #715)."""
    capture = json.loads(CAPTURE_PATH.read_text(encoding="utf-8"))
    extraction = json.loads(EXTRACTION_PATH.read_text(encoding="utf-8"))
    entries = extraction["entries"]
    assert len(entries) == 13
    ungrounded = [
        entry["title"]
        for entry in entries
        if not exact_book_list_mention_is_grounded(
            capture["html"],
            capture["source_page"],
            title=entry["title"],
            authors=entry["primary_authors"],
            evidence_url=entry["evidence_url"],
        )
    ]
    assert ungrounded == []


# ── Replay fixture keeps the live entry markup (issue #715) ────────────────


def _capture_book_cards(html: str) -> list[str]:
    """The capture's qualifying book cards minus their textless cover figure.

    Book cards carry ``<h2 class="listing__title">``; the page's News
    interview cards use ``<h3>`` and are not qualifying entries."""
    cards = []
    for block in re.findall(r'<article class="listing">.*?</article>', html, flags=re.S):
        if '<h2 class="listing__title">' not in block:
            continue
        cards.append(re.sub(r'\s*<figure class="listing__figure">.*?</figure>', "", block, flags=re.S))
    return cards


def test_page_fixture_cards_keep_live_capture_entry_markup() -> None:
    """The committed replay page fixture keeps the live element nesting and
    element boundaries of the qualifying entries: each fixture card is the
    capture's card without its textless cover figure, never a restructured
    flat ``<li><a>title</a><span>author</span></li>`` row (issue #715).

    Issue #996 moved the source to the winner stage, so the fixture is
    compared with the year page as served on 2026-11-08: five other shortlist
    cards followed by the seven remaining longlist cards. The winner box is a
    separate card shape, pinned by the Baillie Gifford source test."""
    capture = json.loads(STAGE_CAPTURE_PATH.read_text(encoding="utf-8"))
    pilot = load_validated_config(ROOT / "configs" / "current-weekly-pilot.json")
    source = next(value for value in pilot["sources"] if value["name"] == "Baillie Gifford Prize 2026 Winner")
    page = json.loads((ROOT / "configs" / source["source_page_fixture_file"]).read_text(encoding="utf-8"))
    fixture_cards = re.findall(r'<article class="listing">.*?</article>', page["html"], flags=re.S)
    assert fixture_cards == _capture_book_cards(capture["html"])
    assert len(fixture_cards) == 12
