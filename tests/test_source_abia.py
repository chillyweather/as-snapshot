"""ABIA 2026 nonfiction shortlists source-specific tests (onboarding #578).

The shared award-list contract is covered by tests/test_issue_577_award_list.py
and tests/test_issue_578_award_list_categories.py. This file pins what is
specific to this source: the two declared categories, the twelve rows, the
title that contains a comma, and the categories of the same page that are not
collected. It reads the committed replay fixtures and makes no network request.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from autoscout.annual_source_audit import REASON_NEWER_EDITION_EXPECTED
from autoscout.annual_source_audit import audit_annual_source
from autoscout.award_list import award_rows
from autoscout.award_list import mention_violation
from autoscout.config import load_validated_config
from autoscout.source_page_identity import validate_page_shape

ROOT = Path(__file__).resolve().parents[1]
CONFIGS = ROOT / "configs"
SOURCE_NAME = "Australian Book Industry Awards (ABIA) 2026 — Nonfiction"
URL = "https://abiawards.com.au/2026-book-awards-shortlist-announcement/"
BIOGRAPHY = "Biography Book of the Year"
NONFICTION = "General Nonfiction Book of the Year"

BIOGRAPHY_TITLES = [
    "Always Home, Always Homesick",
    "Average at Best",
    "Destination Moon",
    "Gather Up Your World in One Long Breath",
    "Guts",
    "Memorial Days",
]
NONFICTION_TITLES = [
    "The Australian Wars",
    "Broken Brains",
    "Conspiracy Nation",
    "The Mushroom Tapes",
    "The Power of Choice",
    "She Shapes History",
]
# Categories of the same page that this source does not collect.
OTHER_CATEGORIES = (
    "<h2><strong>General Fiction Book of the Year</strong></h2><ul><li><em>The Detective, </em>Matthew Reilly "
    "(Macmillan, Pan Macmillan Australia)</li></ul>"
    "<h2><strong>Book of the Year for Older Children (ages 13+)</strong></h2><ul><li><em>Wandering Wild, </em>"
    "Lynette Noni (Penguin, Penguin Random House)</li></ul>"
    "<h2><strong>Social Impact Book of the Year</strong></h2><ul><li><em>Eros</em>, Zoe Terakes "
    "(Hachette Australia)</li></ul>"
)


def _source(config_name: str = "current-weekly-pilot.json") -> dict[str, Any]:
    config = load_validated_config(CONFIGS / config_name)
    return next(source for source in config["sources"] if source["name"] == SOURCE_NAME)


def _fixture(field: str) -> dict[str, Any]:
    return json.loads((CONFIGS / _source()[field]).read_text(encoding="utf-8"))


def _page(html: str | None = None, **overrides: Any) -> dict[str, Any]:
    page = _fixture("source_page_fixture_file")
    if html is not None:
        page["html"] = html
    page.update(overrides)
    return page


def _mention(title: str, authors: list[str]) -> dict[str, Any]:
    return {
        **_fixture("extraction_response_file")["mentions"][0],
        "title": title,
        "english_title": title,
        "primary_authors": authors,
    }


def test_the_source_declares_two_nonfiction_categories_of_one_page() -> None:
    source = _source()

    assert source["url"] == URL
    assert source["contract_family"] == "award-list"
    assert source["award_year"] == 2026
    assert source["award_category_heading"] == BIOGRAPHY
    assert source["award_additional_category_headings"] == [NONFICTION]
    assert source["award_expected_row_count"] == 6
    assert source["award_row_title_element"] == "em"
    assert source["lifecycle_status"] == "established"
    assert source["check_frequency"] == "annual"
    # Categories are bounded by their headings, not by title terms.
    assert "adult_nonfiction_exclusions" not in source


def test_the_replay_page_holds_six_rows_in_each_category() -> None:
    rows = award_rows(_page()["html"], _source())

    assert [row.title for row in rows if row.category == BIOGRAPHY] == BIOGRAPHY_TITLES
    assert [row.title for row in rows if row.category == NONFICTION] == NONFICTION_TITLES
    assert len(rows) == 12
    assert validate_page_shape(_source(), _page()) == ""


def test_a_title_that_contains_a_comma_is_read_from_its_element() -> None:
    source = _source()
    row = award_rows(_page()["html"], source)[0]

    assert (row.title, row.author) == ("Always Home, Always Homesick", "Hannah Kent")
    assert mention_violation(*[_mention(row.title, ["Hannah Kent"])] * 2, _page(), source) == ""
    assert "not grounded" in mention_violation(
        *[_mention("Always Home", ["Always Homesick", "Hannah Kent"])] * 2, _page(), source
    )


def test_coauthors_are_each_one_complete_name() -> None:
    source = _source()
    authors = ["Rachel Perkins", "Stephen Gapps", "Mina Murray", "Henry Reynolds"]

    assert mention_violation(*[_mention("The Australian Wars", authors)] * 2, _page(), source) == ""
    assert "not grounded" in mention_violation(*[_mention("The Australian Wars", authors[:3])] * 2, _page(), source)


def test_fiction_children_and_social_impact_rows_on_the_page_are_not_rows_of_the_source() -> None:
    source = _source()
    html = _page()["html"].replace("</body>", OTHER_CATEGORIES + "</body>")
    page = _page(html)

    assert len(award_rows(html, source)) == 12
    assert validate_page_shape(source, page) == ""
    for title, author in (
        ("The Detective", "Matthew Reilly"),
        ("Wandering Wild", "Lynette Noni"),
        ("Eros", "Zoe Terakes"),
    ):
        assert "not grounded" in mention_violation(*[_mention(title, [author])] * 2, page, source)


def test_a_changed_row_count_or_heading_fails_closed() -> None:
    source = _source()
    html = _page()["html"]
    short = html.replace("<li><em>Memorial Days</em>, Geraldine Brooks (Hachette Australia)</li>\n", "")
    renamed = html.replace(f"<h2>{NONFICTION}</h2>", "<h2>General Non-Fiction Book of the Year</h2>")

    assert short != html and renamed != html
    assert f"'{BIOGRAPHY}' exposes 5 rows; expected exactly 6" in validate_page_shape(source, _page(short))
    assert f"'{NONFICTION}' appears 0 times" in validate_page_shape(source, _page(renamed))


def test_a_stale_or_superseded_award_year_is_reported() -> None:
    source = _source()
    html = _page()["html"]
    replaced = html.replace("2026 Book Awards", "2027 Book Awards")
    announced = html.replace("</h1>", "</h1>\n<h2>2027 shortlists announced</h2>")

    assert validate_page_shape(source, _page(reference_date="2027-06-01")) == ""
    assert audit_annual_source(source, today=date(2027, 6, 1)).reason == REASON_NEWER_EDITION_EXPECTED
    assert "award year 2026 is stale" in validate_page_shape(source, _page(reference_date="2028-01-01"))
    # A page that moved on to the next edition is refused, never re-read as 2026.
    assert "expected page heading" in validate_page_shape(source, _page(replaced))
    assert "newer award year 2027" in validate_page_shape(source, _page(announced))


def test_replay_mentions_cite_the_configured_page_and_nothing_a_row_does_not_state() -> None:
    extraction = _fixture("extraction_response_file")

    assert [mention["title"] for mention in extraction["mentions"]] == BIOGRAPHY_TITLES[:3]
    for mention in extraction["mentions"]:
        assert mention["source_url"] == mention["evidence_url"] == URL
        assert mention["signal_strength"] == "shortlist"
        assert not {"current_rank", "isbn", "source_week", "publisher"} & mention.keys()


def test_live_collection_is_opt_in_in_both_live_overlays() -> None:
    for config_name in ("current-weekly-pilot-live.json", "current-weekly-live.json"):
        source = _source(config_name)
        assert source["enable_live"] is True
        assert source["extraction_method"] == "agent-extraction-live"
        assert source["award_additional_category_headings"] == [NONFICTION]
