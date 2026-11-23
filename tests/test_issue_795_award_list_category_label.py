"""Offline tests for issue #795: an award category labelled by a bold paragraph.

The markup is synthetic first-party-shaped HTML.  No test here makes a network
request.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from autoscout.award_list import REASON_CATEGORY_HEADING
from autoscout.award_list import REASON_ROW_COUNT
from autoscout.award_list import AwardListError
from autoscout.award_list import award_page
from autoscout.award_list import mention_violation
from autoscout.award_list import page_declaration_error
from autoscout.award_list import render_replay_html
from autoscout.award_list import validate_declaration
from autoscout.source_collectors.contract import extraction_contract
from autoscout.source_feasibility import OUTCOME_ACCEPT
from autoscout.source_feasibility import evaluate_source_feasibility
from autoscout.source_scaffold import normalize_operator_metadata

URL = "https://example.test/news/science-prize-2026-nominations/"
HEADING = "Science Prize 2026 nominations announced"
NOMINEES = "Nominees in 2026"
JURY = "Prize jury"
REFERENCE_DATE = date(2026, 10, 12)

NOMINEE_ROWS = (
    "<li><strong>Mara Ellison</strong>, <em>What Birds Fear</em>, Harbour Press</li>",
    "<li><strong>Daniel Mercer, Ines Okoro</strong>, <em>Towards Longevity</em>, Northlight Books</li>",
)
JURY_ROWS = "<li>Ada Quinn, chair, Science Festival</li><li>Tomas Reyes, Book Institute</li>"


def _html(
    nominees: tuple[str, ...] = NOMINEE_ROWS,
    label: str = f"<p><strong>{NOMINEES}</strong></p>",
    after: str = f"<p><strong>{JURY}</strong></p><ul>{JURY_ROWS}</ul>",
) -> str:
    return (
        "<!doctype html><html><body>"
        "<ul><li><a href='/tags/prize/'>Prize</a></li></ul>"
        f"<div class='heading'><h1>{HEADING}</h1></div>"
        "<p>The prize goes to the best popular-science book.</p>"
        f"{label}<ul>{''.join(nominees)}</ul>{after}"
        "<p>The winner is announced on <strong>27 September 2026</strong>, at the festival.</p>"
        "<h2>Read also</h2><h4>Grant applications for 2027</h4>"
        "</body></html>"
    )


def _source(**overrides: Any) -> dict[str, Any]:
    return {
        "name": "Synthetic Science Prize 2026 Nominations",
        "family": "award",
        "signal_family": "award",
        "url": URL,
        "language": "en",
        "check_frequency": "annual",
        "refresh_cadence": "annual",
        "expected_page_heading": HEADING,
        "visible_title_selector": ".heading",
        "expected_signal_strength": "shortlist",
        "eligible_candidate_category": "adult nonfiction",
        "contract_family": "award-list",
        "award_year": 2026,
        "award_year_selector": ".heading",
        "award_category_heading": NOMINEES,
        "award_category_label_element": "strong",
        "award_row_selector": "li",
        "award_row_format": "{author}, {title}, {ignored}",
        "award_expected_row_count": 2,
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


def test_a_bold_paragraph_label_bounds_the_category_up_to_the_next_label() -> None:
    page = award_page(_html(), _source(), reference_date=REFERENCE_DATE)

    # The tag row above the label and the jury rows below the next label are not rows.
    assert [(row.category, row.title, row.author) for row in page.rows] == [
        (NOMINEES, "What Birds Fear", "Mara Ellison"),
        (NOMINEES, "Towards Longevity", "Daniel Mercer, Ines Okoro"),
    ]


def test_without_the_declaration_a_bold_paragraph_is_not_a_category_heading() -> None:
    refusal = _refusal(_html(), award_category_label_element="")

    assert refusal.reason_code == REASON_CATEGORY_HEADING
    assert "appears 0 times" in str(refusal)


def test_a_heading_element_ends_a_labelled_category() -> None:
    html = _html(after=f"<h3>{JURY}</h3><ul>{JURY_ROWS}</ul>")

    assert len(award_page(html, _source(), reference_date=REFERENCE_DATE).rows) == 2


def test_a_category_that_no_label_or_heading_ends_takes_in_the_following_rows_and_is_refused() -> None:
    refusal = _refusal(_html(after=f"<p>The <strong>{JURY}</strong>:</p><ul>{JURY_ROWS}</ul>"))

    # A paragraph that only emphasizes a phrase is prose, so the jury rows
    # stay in the category and do not state a marked title.
    assert "award row 3" in str(refusal)


@pytest.mark.parametrize(
    "label",
    [
        # Text outside the bold element.
        f"<p><strong>{NOMINEES}</strong>:</p>",
        # Two bold runs.
        "<p><strong>Nominees</strong> <strong>in 2026</strong></p>",
        # One bold run nested in another.
        "<p><strong>Nominees <strong>in 2026</strong></strong></p>",
        # A heading element: this page declares that its categories are label paragraphs.
        f"<h2>{NOMINEES}</h2>",
        # Another bold element than the declared one.
        f"<p><b>{NOMINEES}</b></p>",
        # A hidden label.
        f"<p hidden><strong>{NOMINEES}</strong></p>",
        # Bold text that is not a paragraph of its own.
        f"<div><strong>{NOMINEES}</strong></div>",
    ],
)
def test_a_paragraph_that_is_not_one_whole_bold_label_opens_no_category(label: str) -> None:
    refusal = _refusal(_html(label=label))

    assert refusal.reason_code == REASON_CATEGORY_HEADING
    assert "appears 0 times" in str(refusal)


def test_a_paragraph_with_nested_bold_runs_does_not_end_the_category() -> None:
    extra = NOMINEE_ROWS[0].replace("What Birds Fear", "Another Book")
    refusal = _refusal(
        _html(after=f"<p><strong>The <strong>additional nominee</strong> follows</strong></p><ul>{extra}</ul>")
    )

    # The row after the paragraph stays in the category, so the count fails closed.
    assert refusal.reason_code == REASON_ROW_COUNT
    assert "exposes 3 rows; expected exactly 2" in str(refusal)


def test_a_repeated_label_is_refused() -> None:
    repeated = f"<p><strong>{NOMINEES}</strong></p><ul>{''.join(NOMINEE_ROWS)}</ul>"
    refusal = _refusal(_html(after=repeated))

    assert refusal.reason_code == REASON_CATEGORY_HEADING
    assert "appears 2 times" in str(refusal)


def test_a_changed_row_count_under_a_label_is_refused() -> None:
    refusal = _refusal(_html(nominees=NOMINEE_ROWS[:1]))

    assert refusal.reason_code == REASON_ROW_COUNT
    assert "exposes 1 rows; expected exactly 2" in str(refusal)


def test_a_label_with_inline_markup_and_line_breaks_reads_as_its_visible_text() -> None:
    label = "<p>\n <strong>Nominees<br>in <span>2026</span></strong>\n</p>"

    assert len(award_page(_html(label=label), _source(), reference_date=REFERENCE_DATE).rows) == 2


def test_a_paragraph_inside_a_row_is_part_of_that_row() -> None:
    rows = (
        "<li><p><strong>Mara Ellison</strong>, <em>What Birds Fear</em>, Harbour Press</p></li>",
        NOMINEE_ROWS[1],
    )
    page = award_page(_html(nominees=rows), _source(), reference_date=REFERENCE_DATE)

    assert [row.title for row in page.rows] == ["What Birds Fear", "Towards Longevity"]


def test_a_class_selected_paragraph_row_is_a_row_and_not_a_label() -> None:
    rows = (
        "<p class='nominee'><strong>Mara Ellison</strong>, <em>What Birds Fear</em>, Harbour Press</p>",
        "<p class='nominee'><strong>Daniel Mercer</strong>, <em>Towards Longevity</em>, Northlight Books</p>",
    )
    html = _html(nominees=(), after=f"{''.join(rows)}<p><strong>{JURY}</strong></p><p class='nominee'>Ada Quinn</p>")
    page = award_page(html, _source(award_row_selector=".nominee"), reference_date=REFERENCE_DATE)

    assert [row.title for row in page.rows] == ["What Birds Fear", "Towards Longevity"]


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"award_category_label_element": "em"}, "must be one of: b, strong"),
        ({"award_category_label_element": "h2"}, "must be one of: b, strong"),
        ({"award_category_label_element": True}, "must be one of: b, strong"),
        ({"award_row_selector": "p"}, "must not be declared with award_row_selector p"),
    ],
)
def test_an_unusable_label_declaration_is_refused_by_the_page_check_and_the_scaffold(
    overrides: dict[str, Any], fragment: str
) -> None:
    source = _source(**overrides)

    assert fragment in page_declaration_error(source)
    assert fragment in validate_declaration(source)


def test_the_scaffold_keeps_the_declared_label_element() -> None:
    operator = {
        "source_name": "Synthetic Science Prize 2026 Nominations",
        "access_status": "open",
        "country": "Poland",
        "market": "Poland",
        "expected_category": "adult nonfiction",
        "url_stability": "year-specific",
        "extraction_notes": "Metadata only.",
        "lifecycle_note": "Issue #795 active and reversible during probation.",
        "notes": "Issue #795.",
    }

    assert normalize_operator_metadata({**_source(), **operator})["award_category_label_element"] == "strong"


def test_a_mention_is_grounded_only_in_a_row_under_the_label() -> None:
    page = {"html": _html()}
    source = _source()

    assert mention_violation(*[_mention("What Birds Fear", ["Mara Ellison"])] * 2, page, source) == ""
    assert mention_violation(*[_mention("Towards Longevity", ["Daniel Mercer", "Ines Okoro"])] * 2, page, source) == ""
    # A jury member under the next label is not a nominee.
    assert "not grounded" in mention_violation(*[_mention("chair", ["Ada Quinn"])] * 2, page, source)


def test_the_replay_page_keeps_the_label_paragraph_and_reads_back_the_same_rows() -> None:
    source = _source()
    replay = render_replay_html(_html(), source, HEADING)

    assert f"<p><strong>{NOMINEES}</strong></p>" in replay
    assert f"<h2>{NOMINEES}</h2>" not in replay
    assert JURY not in replay
    assert "Ada Quinn" not in replay
    original = award_page(_html(), source, reference_date=REFERENCE_DATE).rows
    replayed = award_page(replay, source, reference_date=REFERENCE_DATE).rows
    assert [(row.category, row.title, row.author) for row in replayed] == [
        (row.category, row.title, row.author) for row in original
    ]


def test_feasibility_accepts_a_labelled_category() -> None:
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
    assert verdict.bounded_entry_count == 2
    assert verdict.detected_heading == HEADING
    assert [(sample.title, sample.author) for sample in verdict.samples] == [
        ("What Birds Fear", "Mara Ellison"),
        ("Towards Longevity", "Daniel Mercer / Ines Okoro"),
    ]


def test_the_extraction_contract_names_the_label_element_only_when_declared() -> None:
    rules = " ".join(extraction_contract(_source())["rules"])

    assert "a paragraph set entirely in one <strong> element" in rules
    assert f"the visible heading {NOMINEES!r}" in rules
    undeclared = " ".join(extraction_contract(_source(award_category_label_element=""))["rules"])
    assert "paragraph set entirely" not in undeclared
