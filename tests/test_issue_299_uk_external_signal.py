"""Source-specific pinned coverage for Issue #299 (Baillie Gifford), moved to the 2026 winner for Issue #996.

Issue #681 moved the configured source from the 2025 shortlist to the 2026
cycle, at the longlist stage, and Issue #762 moved it to the shortlist. Issue
#996 moves it to the last stage of the cycle: the year page announced the
winner on 4 November 2026 in its own section, and keeps the five other
shortlisted books and the seven remaining longlist books in their own sections
below it. The source tracks the latest available annual stage, now the 2026
winner, bounded to that section.
"""

import json
from pathlib import Path
from typing import Any

from autoscout.config import load_validated_config
from autoscout.source_collectors.base import _validate_exact_book_list_mention
from autoscout.source_page_identity import link_anchor_pairs
from autoscout.source_page_identity import validate_page_shape

ROOT = Path(__file__).resolve().parents[1]
SOURCE_NAME = "Baillie Gifford Prize 2026 Winner"
SOURCE_URL = "https://www.thebailliegiffordprize.co.uk/year-by-year/2026"
EXPECTED_CATEGORY = "Baillie Gifford Prize for Non-Fiction 2026 winner"
RETAINED_CAPTURE = (
    ROOT / "tests/fixtures/issue-996-retained-capture/baillie-gifford-prize-2026-year-page-2026-11-08.json"
)
WINNER_URL = "https://www.thebailliegiffordprize.co.uk/books-and-authors/alexander-by-edmund-richardson"
# The winner card is one link around the whole box: its heading prints the
# title and subtitle as two lines, then the author and the publisher.
WINNER_LINKS = ((WINNER_URL, "Alexander God, King, Man Edmund Richardson Published by: Bloomsbury Publishing Plc"),)
# The page fixture keeps the live listing-card shape: each link wraps year +
# title + author (issue #715), so the pinned anchor text is the card's visible
# text, not a bare title.
OTHER_SHORTLIST_LINKS = (
    (
        "https://www.thebailliegiffordprize.co.uk/books-and-authors/baldwin-a-love-story-by-nicholas-bloggs",
        "2026 Baldwin: A Love Story Nicholas Boggs",
    ),
    (
        "https://www.thebailliegiffordprize.co.uk/books-and-authors/days-of-love-and-rage-by-anand-gopal",
        "2026 Days of Love and Rage Anand Gopal",
    ),
    (
        "https://www.thebailliegiffordprize.co.uk/books-and-authors/london-falling-by-patrick-radden-keefe",
        "2026 London Falling Patrick Radden Keefe",
    ),
    (
        "https://www.thebailliegiffordprize.co.uk/books-and-authors/the-great-dictator-by-matthew-sweet",
        "2026 The Great Dictator Matthew Sweet",
    ),
    (
        "https://www.thebailliegiffordprize.co.uk/books-and-authors/the-traveller-by-andrea-wulf",
        "2026 The Traveller Andrea Wulf",
    ),
)
REMAINING_LONGLIST_LINKS = (
    (
        "https://www.thebailliegiffordprize.co.uk/books-and-authors/stay-alive-by-ian-buruma",
        "2026 Stay Alive Ian Buruma",
    ),
    (
        "https://www.thebailliegiffordprize.co.uk/books-and-authors/murder-in-paris-68-by-edward-chisholm",
        "2026 Murder In Paris '68 Edward Chisholm",
    ),
    (
        "https://www.thebailliegiffordprize.co.uk/books-and-authors/ukrainian-lessons-by-charlotte-higgins",
        "2026 Ukrainian Lessons Charlotte Higgins",
    ),
    (
        "https://www.thebailliegiffordprize.co.uk/books-and-authors/redwood-by-ben-macintyre",
        "2026 Redwood Ben Macintyre",
    ),
    (
        "https://www.thebailliegiffordprize.co.uk/books-and-authors/the-nord-stream-conspiracy-by-bojan-pancevski",
        "2026 The Nord Stream Conspiracy Bojan Pancevski",
    ),
    (
        "https://www.thebailliegiffordprize.co.uk/books-and-authors/hotel-exile-by-jane-rogoyska",
        "2026 Hotel Exile Jane Rogoyska",
    ),
    (
        "https://www.thebailliegiffordprize.co.uk/books-and-authors/never-say-war-by-åsne-seierstad",
        "2026 Never Say War Åsne Seierstad",
    ),
)
# Title and primary author behind each card text above.
CARD_BOOKS = {
    WINNER_LINKS[0][1]: ("Alexander", "Edmund Richardson"),
    "2026 Baldwin: A Love Story Nicholas Boggs": ("Baldwin: A Love Story", "Nicholas Boggs"),
    "2026 Days of Love and Rage Anand Gopal": ("Days of Love and Rage", "Anand Gopal"),
    "2026 London Falling Patrick Radden Keefe": ("London Falling", "Patrick Radden Keefe"),
    "2026 The Great Dictator Matthew Sweet": ("The Great Dictator", "Matthew Sweet"),
    "2026 The Traveller Andrea Wulf": ("The Traveller", "Andrea Wulf"),
    "2026 Stay Alive Ian Buruma": ("Stay Alive", "Ian Buruma"),
    "2026 Murder In Paris '68 Edward Chisholm": ("Murder In Paris '68", "Edward Chisholm"),
    "2026 Ukrainian Lessons Charlotte Higgins": ("Ukrainian Lessons", "Charlotte Higgins"),
    "2026 Redwood Ben Macintyre": ("Redwood", "Ben Macintyre"),
    "2026 The Nord Stream Conspiracy Bojan Pancevski": ("The Nord Stream Conspiracy", "Bojan Pancevski"),
    "2026 Hotel Exile Jane Rogoyska": ("Hotel Exile", "Jane Rogoyska"),
    "2026 Never Say War Åsne Seierstad": ("Never Say War", "Åsne Seierstad"),
}
# "Alexander" is the title the shortlist card printed (#762), so the winner
# keeps the identity the book already has in Candidate History.
EXTRACTION_MENTIONS = (("Alexander", "Edmund Richardson", WINNER_URL),)
HELPER_SHA256 = "a78d05bbbbc0dbf50cfe9b1bdd35538fc57cccd90cf1d1d09b4c269d4426e95e"


def _portfolio_source() -> dict[str, Any]:
    portfolio = load_validated_config(ROOT / "configs/current-weekly-portfolio.json")
    return dict(next(value for value in portfolio["sources"] if value["name"] == SOURCE_NAME))


def _source() -> dict[str, Any]:
    config = load_validated_config(ROOT / "configs/current-weekly-pilot.json")
    return dict(next(value for value in config["sources"] if value["name"] == SOURCE_NAME))


def _fixtures() -> tuple[dict[str, Any], dict[str, Any]]:
    source = _source()
    page = json.loads((ROOT / "configs" / source["source_page_fixture_file"]).read_text(encoding="utf-8"))
    extraction = json.loads((ROOT / "configs" / source["extraction_response_file"]).read_text(encoding="utf-8"))
    return page, extraction


def _mention(title: str, author: str, evidence_url: str) -> dict[str, Any]:
    return {
        "title": title,
        "primary_authors": [author],
        "source_url": SOURCE_URL,
        "evidence_url": evidence_url,
        "signal_type": "award",
        "signal_strength": "winner",
        "candidate_category": "adult nonfiction",
        "english_title": title,
        "search_translation": "Вспомогательный перевод",
        "author_transliteration_note": "Вспомогательная транслитерация",
    }


def test_source_metadata_fields() -> None:
    source = _source()
    expected = {
        "active": True,
        "access_status": "open",
        "check_frequency": "annual",
        "family": "award",
        "language": "en",
        "country": "United Kingdom",
        "market": "United Kingdom",
        "expected_category": EXPECTED_CATEGORY,
        "signal_family": "award",
        "refresh_cadence": "annual",
        "url_stability": "year-specific",
        "expected_page_heading": "2026 The Baillie Gifford Prize for Non-Fiction",
        "expected_visible_markers": ["The winner"],
        "entry_section_heading": "The winner",
        "lifecycle_status": "established",
        "metadata_exact_book_list": True,
        "expected_signal_strength": "winner",
        "eligible_candidate_category": "adult nonfiction",
        "helper_metadata_file": "../data/current-weekly-pilot/baillie-gifford-prize-2026-winner-helper-metadata.json",
        "helper_metadata_sha256": HELPER_SHA256,
    }
    for field, value in expected.items():
        assert source.get(field) == value
    assert "forbidden_visible_markers" not in source
    assert "metadata only" in source["extraction_notes"].lower()


def test_source_tracks_latest_available_stage() -> None:
    """Issue #681: the source policy must state latest-stage tracking, not a
    fixed stage. Issue #996 moved the stage to the winner, the last of the
    cycle; the next rollover is to the 2027 longlist."""
    source = _portfolio_source()
    policy = f"{source.get('extraction_notes', '')} {source.get('notes', '')}".casefold()
    assert "latest available stage" in policy
    assert "4 november 2026" in policy
    assert "2027 longlist" in policy
    assert "mislabel" in policy


def test_url_exact_match() -> None:
    assert _source()["url"] == SOURCE_URL


def test_extraction_titles_match_published_original() -> None:
    _, extraction = _fixtures()
    actual = tuple(
        (mention["title"], mention["primary_authors"][0], mention["evidence_url"]) for mention in extraction["mentions"]
    )
    assert actual == EXTRACTION_MENTIONS
    assert {mention["signal_strength"] for mention in extraction["mentions"]} == {"winner"}


def test_page_fixture_links_are_the_winner_then_the_shortlist_then_the_longlist() -> None:
    page, _ = _fixtures()
    assert (
        tuple(link_anchor_pairs(page["html"], page["source_page"]))
        == WINNER_LINKS + OTHER_SHORTLIST_LINKS + REMAINING_LONGLIST_LINKS
    )


def test_winner_stage_marker_guards_page_contract() -> None:
    """The year-level H1 survives stage changes, so the stage section is the
    deterministic stage guard (#681). A page without a winner section must
    fail closed instead of collecting shortlist rows as the winner."""
    source = _portfolio_source()
    page, _ = _fixtures()
    assert validate_page_shape(source, page) == ""
    regressed = dict(page, html=page["html"].replace('<h2 class="h-4">The winner</h2>', ""))
    assert "The winner" not in regressed["html"]
    assert validate_page_shape(source, regressed) != ""


def test_only_the_winner_row_grounds() -> None:
    """Issue #996: the page keeps the five other shortlisted books and seven
    longlist books beside the winner. Every one of them is a linked, fully
    grounded card, so only the declared section keeps them from being
    collected as the winner."""
    source = _portfolio_source()
    page, _ = _fixtures()
    for url, card_text in WINNER_LINKS:
        title, author = CARD_BOOKS[card_text]
        mention = _mention(title, author, url)
        assert _validate_exact_book_list_mention(mention, mention, source, page) == ""
    for url, card_text in OTHER_SHORTLIST_LINKS + REMAINING_LONGLIST_LINKS:
        title, author = CARD_BOOKS[card_text]
        mention = _mention(title, author, url)
        error = _validate_exact_book_list_mention(mention, mention, source, page)
        assert "'The winner' section" in error
        unbounded = {key: value for key, value in source.items() if key != "entry_section_heading"}
        assert _validate_exact_book_list_mention(mention, mention, unbounded, page) == ""


def test_winner_bound_holds_on_the_retained_live_capture() -> None:
    """The same split on the page as served on 2026-11-08, not only on the
    reduced replay fixture."""
    source = _portfolio_source()
    capture = json.loads(RETAINED_CAPTURE.read_text(encoding="utf-8"))
    assert validate_page_shape(source, capture) == ""
    for links, grounded in (
        (WINNER_LINKS, True),
        (OTHER_SHORTLIST_LINKS, False),
        (REMAINING_LONGLIST_LINKS, False),
    ):
        for url, card_text in links:
            title, author = CARD_BOOKS[card_text]
            mention = _mention(title, author, url)
            assert (_validate_exact_book_list_mention(mention, mention, source, capture) == "") is grounded


def test_the_shortlist_page_no_longer_satisfies_the_winner_source() -> None:
    """The page as served on 2026-10-05 had no winner yet; the winner source
    refuses it rather than collecting a shortlist book as the winner."""
    source = _portfolio_source()
    capture = json.loads(
        (
            ROOT / "tests/fixtures/issue-762-retained-capture/baillie-gifford-prize-2026-year-page-2026-10-05.json"
        ).read_text(encoding="utf-8")
    )
    assert validate_page_shape(source, capture) != ""


def test_helper_sidecar_mirrors_representative_entries() -> None:
    """Issue #681: retained version-1 helper sidecar carries exactly the
    representative entries, with sha256 wired through portfolio and overlays."""
    import hashlib

    source = _portfolio_source()
    raw_path = source.get("helper_metadata_file", "")
    assert raw_path.endswith("baillie-gifford-prize-2026-winner-helper-metadata.json")
    sidecar_path = (ROOT / "configs" / raw_path).resolve()
    assert sidecar_path.is_file()
    assert source.get("helper_metadata_sha256") == hashlib.sha256(sidecar_path.read_bytes()).hexdigest()
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert sidecar["version"] == 1
    assert sidecar["source_url"] == SOURCE_URL
    assert sidecar["language"] == "en"
    _, extraction = _fixtures()
    assert [(e["title"], e["author"], e["evidence_url"]) for e in sidecar["entries"]] == [
        (m["title"], m["primary_authors"][0], m["evidence_url"]) for m in extraction["mentions"]
    ]
    for entry, mention in zip(sidecar["entries"], extraction["mentions"], strict=True):
        assert entry["english_title"] == mention["english_title"]
        assert entry["search_translation"] == mention["search_translation"]
        assert entry["author_transliteration_note"] == mention["author_transliteration_note"]
