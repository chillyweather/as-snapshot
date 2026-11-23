"""Offline tests for issue #578: several categories and a marked title on one award page.

The markup is synthetic first-party-shaped HTML.  No test here makes a network
request.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from autoscout.award_list import AwardListError
from autoscout.award_list import award_page
from autoscout.award_list import mention_violation
from autoscout.award_list import page_declaration_error
from autoscout.award_list import render_replay_html
from autoscout.award_list import validate_declaration
from autoscout.source_collectors.contract import extraction_contract
from autoscout.source_feasibility import OUTCOME_ACCEPT
from autoscout.source_feasibility import evaluate_source_feasibility
from autoscout.source_scaffold import ScaffoldError
from autoscout.source_scaffold import normalize_operator_metadata

URL = "https://example.test/2026-book-awards-shortlist/"
HEADING = "2026 Book Awards Shortlist Announcement"
BIOGRAPHY = "Biography Book of the Year"
NONFICTION = "General Nonfiction Book of the Year"
REFERENCE_DATE = date(2026, 10, 11)

BIOGRAPHY_ROWS = (
    "<li><em>Always Home, Always Homesick</em>, Mara Ellison (Harbour, Harbour Press)</li>",
    "<li><em>Guts</em>, Tomas Reyes (Northlight Books)</li>",
)
NONFICTION_ROWS = (
    "<li><em>The Long Wars, </em>Daniel Mercer, Ines Okoro and Sam Hale (Harbour Press)</li>",
    "<li><em>Salt and Iron,</em> Ada Quinn (Northlight Books)</li>",
)


def _html(biography: tuple[str, ...] = BIOGRAPHY_ROWS, nonfiction: tuple[str, ...] = NONFICTION_ROWS) -> str:
    return (
        f"<!doctype html><html><body><h1>{HEADING}</h1>"
        f"<h2><strong>{BIOGRAPHY}</strong></h2><ul>{''.join(biography)}</ul>"
        "<h2>General Fiction Book of the Year</h2><ul><li><em>A Novel, </em>Some Writer (Other House)</li></ul>"
        f"<h2><strong>{NONFICTION}</strong></h2><ul>{''.join(nonfiction)}</ul>"
        "<h2>Small Publishers Book of the Year</h2><ul><li><em>Third Thing, </em>A Writer (Tiny Press)</li></ul>"
        "</body></html>"
    )


def _source(**overrides: Any) -> dict[str, Any]:
    return {
        "name": "Synthetic Book Awards 2026 Nonfiction Shortlists",
        "family": "award",
        "signal_family": "award",
        "url": URL,
        "language": "en",
        "check_frequency": "annual",
        "refresh_cadence": "annual",
        "expected_page_heading": HEADING,
        "expected_signal_strength": "shortlist",
        "eligible_candidate_category": "adult nonfiction",
        "contract_family": "award-list",
        "award_year": 2026,
        "award_category_heading": BIOGRAPHY,
        "award_additional_category_headings": [NONFICTION],
        "award_row_selector": "li",
        "award_row_format": "{title}, {author} ({ignored})",
        "award_expected_row_count": 2,
        "award_author_conjunction": "and",
        "award_row_title_element": "em",
        **overrides,
    }


def _refusal(html: str, **overrides: Any) -> AwardListError:
    with pytest.raises(AwardListError) as refused:
        award_page(html, _source(**overrides), reference_date=REFERENCE_DATE)
    return refused.value


def _mention(title: str, authors: list[str]) -> dict[str, Any]:
    return {
        "title": title,
        "primary_authors": authors,
        "source_url": URL,
        "evidence_url": URL,
        "signal_type": "award",
        "signal_strength": "shortlist",
        "candidate_category": "adult nonfiction",
        "english_title": title,
        "search_translation": "Русский поисковый перевод",
        "author_transliteration_note": "Русская транслитерация автора",
    }


def test_one_page_yields_the_rows_of_every_declared_category_and_no_other() -> None:
    page = award_page(_html(), _source(), reference_date=REFERENCE_DATE)

    assert page.category_headings == (BIOGRAPHY, NONFICTION)
    assert [(row.category, row.title) for row in page.rows] == [
        (BIOGRAPHY, "Always Home, Always Homesick"),
        (BIOGRAPHY, "Guts"),
        (NONFICTION, "The Long Wars"),
        (NONFICTION, "Salt and Iron"),
    ]


def test_a_marked_title_keeps_its_own_comma_and_drops_the_separator() -> None:
    rows = award_page(_html(), _source(), reference_date=REFERENCE_DATE).rows

    # The comma sits outside the element, inside it, and inside it before a space.
    assert [row.title for row in rows] == ["Always Home, Always Homesick", "Guts", "The Long Wars", "Salt and Iron"]
    assert rows[0].author == "Mara Ellison"
    assert rows[2].author == "Daniel Mercer, Ines Okoro and Sam Hale"


def test_the_same_rows_without_a_declared_title_element_are_refused_as_ambiguous() -> None:
    refusal = _refusal(_html(), award_row_title_element="")

    assert refusal.reason_code == "award_row_shape"
    assert "more than one way" in str(refusal)


@pytest.mark.parametrize(
    "row",
    [
        # No title element at all.
        "<li>Guts, Tomas Reyes (Northlight Books)</li>",
        # Two title elements.
        "<li><em>Guts</em>, <em>Tomas Reyes</em> (Northlight Books)</li>",
        # An empty title element.
        "<li><em> </em>Guts, Tomas Reyes (Northlight Books)</li>",
        # The element marks only part of the title.
        "<li><em>Guts</em>: A Memoir, Tomas Reyes (Northlight Books)</li>",
        # The element runs into the author's name.
        "<li><em>Guts, T</em>omas Reyes (Northlight Books)</li>",
        # The element marks the author, not the title.
        "<li>Guts, <em>Tomas Reyes</em> (Northlight Books)</li>",
        # The only title element is hidden.
        "<li><em hidden>Guts</em>Guts, Tomas Reyes (Northlight Books)</li>",
    ],
)
def test_a_row_whose_title_is_not_the_one_marked_element_refuses_the_page(row: str) -> None:
    refusal = _refusal(_html(biography=(BIOGRAPHY_ROWS[0], row)))

    assert refusal.reason_code == "award_row_shape"


def test_every_declared_category_must_hold_the_declared_row_count() -> None:
    refusal = _refusal(_html(nonfiction=NONFICTION_ROWS[:1]))

    assert refusal.reason_code == "award_row_count"
    assert f"'{NONFICTION}' exposes 1 rows; expected exactly 2" in str(refusal)


def test_a_missing_or_repeated_additional_heading_refuses_the_page() -> None:
    missing = _refusal(_html(), award_additional_category_headings=["Social Impact Book of the Year"])
    repeated = _refusal(_html() + f"<h2>{NONFICTION}</h2>")

    assert missing.reason_code == repeated.reason_code == "award_category_heading"
    assert "appears 0 times" in str(missing)
    assert "appears 2 times" in str(repeated)


def test_a_book_listed_under_two_declared_categories_refuses_the_page() -> None:
    repeated = "<li><em>Guts</em>, Tomas Reyes (Northlight Books)</li>"
    refusal = _refusal(_html(nonfiction=(NONFICTION_ROWS[0], repeated)))

    assert refusal.reason_code == "award_row_shape"
    assert f"repeats a book listed under the declared category '{BIOGRAPHY}'" in str(refusal)


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"award_additional_category_headings": [BIOGRAPHY.upper()]}, "must not repeat"),
        ({"award_additional_category_headings": [NONFICTION, NONFICTION]}, "must not repeat"),
        ({"award_additional_category_headings": [" "]}, "list of nonblank headings"),
        ({"award_additional_category_headings": NONFICTION}, "list of nonblank headings"),
        ({"award_row_title_element": "span"}, "must be one of"),
        ({"award_row_title_element": "em", "award_row_split_first": True}, "must not both be declared"),
    ],
)
def test_the_scaffold_and_the_validator_refuse_the_same_declarations(overrides: dict[str, Any], fragment: str) -> None:
    source = _source(**overrides)

    assert fragment in page_declaration_error(source)
    assert fragment in validate_declaration(source)
    operator = {
        "source_name": source["name"],
        "access_status": "open",
        "country": "Australia",
        "market": "Australia",
        "expected_category": "adult nonfiction",
        "url_stability": "year-specific",
        "extraction_notes": "Metadata only.",
        "lifecycle_note": "Issue #578 active and reversible during probation.",
        "notes": "Issue #578.",
    }
    with pytest.raises(ScaffoldError):
        normalize_operator_metadata({**source, **operator})
    accepted = normalize_operator_metadata({**_source(), **operator})
    assert accepted["award_additional_category_headings"] == [NONFICTION]
    assert accepted["award_row_title_element"] == "em"


def test_a_mention_is_grounded_in_a_row_of_any_declared_category() -> None:
    page = {"html": _html()}
    source = _source()

    assert mention_violation(*[_mention("Always Home, Always Homesick", ["Mara Ellison"])] * 2, page, source) == ""
    assert (
        mention_violation(*[_mention("The Long Wars", ["Daniel Mercer", "Ines Okoro", "Sam Hale"])] * 2, page, source)
        == ""
    )
    # The comma reading the declared element rules out is not a row.
    assert "not grounded" in mention_violation(
        *[_mention("Always Home", ["Always Homesick", "Mara Ellison"])] * 2, page, source
    )
    # A row of an undeclared category on the same page is not a row of this source.
    assert "not grounded" in mention_violation(*[_mention("A Novel", ["Some Writer"])] * 2, page, source)


def test_the_replay_page_keeps_each_category_and_the_title_element() -> None:
    source = _source()
    replay = render_replay_html(_html(), source, HEADING)

    assert "<em>Always Home, Always Homesick</em>, Mara Ellison" in replay
    assert "A Novel" not in replay
    assert "Third Thing" not in replay
    original = award_page(_html(), source, reference_date=REFERENCE_DATE).rows
    replayed = award_page(replay, source, reference_date=REFERENCE_DATE).rows
    assert [(row.category, row.title, row.author) for row in replayed] == [
        (row.category, row.title, row.author) for row in original
    ]


def test_feasibility_accepts_the_page_and_names_each_row_category() -> None:
    verdict = evaluate_source_feasibility(
        _source(),
        {
            "collection_mode": "source-candidate-probe",
            "source_url": URL,
            "source_page": URL,
            "resolved_url": URL,
            "head_status": 200,
            "get_status": 200,
            "reference_date": REFERENCE_DATE.isoformat(),
            "html": _html(),
        },
    )

    assert verdict.outcome == OUTCOME_ACCEPT, verdict.reason
    assert verdict.bounded_entry_count == 4
    assert verdict.ranking_category == f"{BIOGRAPHY}; {NONFICTION}"
    assert [sample.category for sample in verdict.samples[:3]] == [BIOGRAPHY, BIOGRAPHY, NONFICTION]
    assert verdict.samples[0].title == "Always Home, Always Homesick"


def test_the_extraction_contract_names_every_category_and_the_title_element() -> None:
    rules = " ".join(extraction_contract(_source())["rules"])

    assert repr(BIOGRAPHY) in rules
    assert repr(NONFICTION) in rules
    assert "<em> element" in rules
    single = " ".join(
        extraction_contract(_source(award_additional_category_headings=[], award_row_title_element=""))["rules"]
    )
    assert f"the visible heading {BIOGRAPHY!r}" in single
    assert "<em>" not in single


def test_the_replay_page_puts_a_category_named_by_the_page_heading_first() -> None:
    """Such a category has no heading of its own, so its rows must sit under the page heading."""
    page_heading = f"2026 {NONFICTION}"
    html = (
        f"<html><body><h1>{page_heading}</h1><ul>{''.join(NONFICTION_ROWS)}</ul>"
        f"<h2>{BIOGRAPHY}</h2><ul>{''.join(BIOGRAPHY_ROWS)}</ul></body></html>"
    )
    source = _source(award_additional_category_headings=[page_heading], expected_page_heading=page_heading)
    replay = render_replay_html(html, source, page_heading)

    original = award_page(html, source, reference_date=REFERENCE_DATE).rows
    replayed = award_page(replay, source, reference_date=REFERENCE_DATE).rows
    assert sorted((row.category, row.title) for row in replayed) == sorted(
        (row.category, row.title) for row in original
    )
    assert {row.category for row in replayed} == {BIOGRAPHY, page_heading}


@pytest.mark.parametrize(
    "row",
    [
        "<li><em>Guts </em>, Tomas Reyes (Northlight Books)</li>",
        "<li><em>Guts\n</em> , Tomas Reyes (Northlight Books)</li>",
        "<li> <em> Guts</em>, Tomas Reyes (Northlight Books)</li>",
    ],
)
def test_space_around_the_marked_title_does_not_refuse_the_row(row: str) -> None:
    rows = award_page(_html(biography=(BIOGRAPHY_ROWS[0], row)), _source(), reference_date=REFERENCE_DATE).rows

    assert (rows[1].title, rows[1].author) == ("Guts", "Tomas Reyes")
