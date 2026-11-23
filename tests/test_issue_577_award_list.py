"""Offline tests for issue #577, the bounded annual award list.

The markup is synthetic first-party-shaped HTML.  It is not live evidence and
it does not claim that any source has passed a live probe or an onboarding
confirmation gate.  No test here makes a network request.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from autoscout.annual_source_audit import REASON_NEWER_EDITION_EXPECTED
from autoscout.annual_source_audit import audit_annual_source
from autoscout.award_list import AwardListError
from autoscout.award_list import _row_segments
from autoscout.award_list import author_display
from autoscout.award_list import author_names
from autoscout.award_list import award_page
from autoscout.award_list import compile_row_format
from autoscout.award_list import first_reading
from autoscout.award_list import mention_violation
from autoscout.award_list import row_readings
from autoscout.award_list import validate_declaration
from autoscout.config import validate_config
from autoscout.replay_fixtures import ReplayFixtureError
from autoscout.replay_fixtures import build_replay_fixture_pair
from autoscout.source_collection import SourceCollection
from autoscout.source_collectors.base import contract_family
from autoscout.source_collectors.contract import extraction_contract
from autoscout.source_feasibility import OUTCOME_ACCEPT
from autoscout.source_feasibility import OUTCOME_REJECT
from autoscout.source_feasibility import evaluate_source_feasibility
from autoscout.source_page_identity import validate_page_shape
from autoscout.source_page_identity import validate_ranking_contract_metadata
from autoscout.source_probe import probe_source_candidates
from autoscout.source_scaffold import ScaffoldError
from autoscout.source_scaffold import normalize_operator_metadata

URL = "https://example.test/2026-book-awards-shortlist/"
SOURCE_NAME = "Synthetic Book Awards 2026 General Nonfiction Shortlist"
HEADING = "2026 Book Awards Shortlist Announcement"
CATEGORY = "General Nonfiction Book of the Year"
RUN_DATE = "2026-10-11"

AWARD_DECLARATION: dict[str, Any] = {
    "contract_family": "award-list",
    "award_year": 2026,
    "award_category_heading": CATEGORY,
    "award_row_selector": "li",
    "award_row_format": "{title}, {author} ({ignored})",
    "award_expected_row_count": 3,
    "award_author_conjunction": "and",
}
# The author displays the probe reports: each row's names joined with " / ".
DISPLAYS = ("Mara Ellison", "Daniel Mercer / Ines Okoro", "Tomas Reyes")

ROWS = (
    ("The Long Coast", "Mara Ellison", "Harbour Press"),
    ("Salt and Iron", "Daniel Mercer and Ines Okoro", "Northlight Books"),
    ("A Quiet Ledger", "Tomas Reyes", "Harbour Press"),
)


def _row(title: str, author: str, publisher: str) -> str:
    return f"<li>{title}, {author} ({publisher})</li>"


def _html(rows: str | None = None, *, heading: str = HEADING, category: str = CATEGORY, extra: str = "") -> str:
    body = "".join(_row(*row) for row in ROWS) if rows is None else rows
    return (
        "<!doctype html><html><head><title>Stale title</title></head><body>"
        f"<h1>{heading}</h1>"
        "<h2>Biography Book of the Year</h2><ul><li>Another Life, Someone Else (Other House)</li></ul>"
        f"<h2>{category}</h2><ul>{body}</ul>"
        "<h2>Small Publishers Book of the Year</h2><ul><li>Third Thing, A Writer (Tiny Press)</li></ul>"
        f"{extra}</body></html>"
    )


def _source(**overrides: Any) -> dict[str, Any]:
    return {
        "name": SOURCE_NAME,
        "family": "award",
        "signal_family": "award",
        "url": URL,
        "language": "en",
        "check_frequency": "annual",
        "refresh_cadence": "annual",
        "expected_page_heading": HEADING,
        "expected_signal_strength": "shortlist",
        "eligible_candidate_category": "adult nonfiction",
        **AWARD_DECLARATION,
        **overrides,
    }


def _evidence(html: str, *, reference_date: str = RUN_DATE) -> dict[str, Any]:
    return {
        "collection_mode": "source-candidate-probe",
        "source_url": URL,
        "source_page": URL,
        "resolved_url": URL,
        "head_status": 200,
        "get_status": 200,
        "reference_date": reference_date,
        "html": html,
    }


def _mention(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "title": "The Long Coast",
        "primary_authors": ["Mara Ellison"],
        "source_url": URL,
        "evidence_url": URL,
        "signal_type": "award",
        "signal_strength": "shortlist",
        "candidate_category": "adult nonfiction",
        "evidence_note": "Bounded award row on the captured source page.",
        "english_title": "The Long Coast",
        "search_translation": "Длинный берег",
        "author_transliteration_note": "Мара Эллисон",
    }
    value.update(overrides)
    return value


def _violation(mention: dict[str, Any], html: str | None = None, **source_overrides: Any) -> str:
    page = {"html": _html() if html is None else html, "source_page": URL}
    return mention_violation(mention, mention, page, _source(**source_overrides))


# --- Declaration -----------------------------------------------------------


def test_an_award_list_is_declarable_without_a_period_a_window_or_exclusions() -> None:
    source = _source()
    assert validate_declaration(source) == ""
    assert validate_config({"run_date": RUN_DATE, "sources": [source]}) == []
    assert contract_family(source) == "award-list"
    assert not any(field.startswith("ranking_") or field == "adult_nonfiction_exclusions" for field in source)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"award_year": None}, "award-list requires award_year"),
        ({"award_year": "2026"}, "award_year must be a four-digit year"),
        ({"award_category_heading": " "}, "award_category_heading must be a nonblank string"),
        ({"award_row_selector": "span"}, "award_row_selector must be one of"),
        ({"award_row_format": "{title} {author}"}, ""),
        ({"award_row_format": "{title}{author}"}, "placeholders must be separated by literal text"),
        ({"award_row_format": "{title} by someone"}, "award_row_format must contain {author}"),
        ({"award_row_format": "{title}, {author}, {publisher}"}, "placeholder {publisher} is not one of"),
        ({"award_expected_row_count": 0}, "award_expected_row_count must be a positive integer"),
        ({"award_year_selector": "h1"}, "selector"),
        ({"award_author_conjunction": "and also"}, "award_author_conjunction must be one word"),
        ({"award_row_split_first": "yes"}, "award_row_split_first must be true or false"),
        (
            {"award_year_selector": ".award", "award_row_selector": ".award > .book"},
            "award_year_selector must name the element that states the year",
        ),
        ({"check_frequency": "weekly", "refresh_cadence": "weekly"}, "requires an annual check_frequency"),
        ({"metadata_ranking_table": True}, "must not set metadata_ranking_table"),
        ({"metadata_exact_book_list": True}, "must not set metadata_exact_book_list"),
        (
            {"ranking_reporting_period_max_age_days": 365},
            "does not use ranking-table declarations: ranking_reporting_period_max_age_days",
        ),
        (
            {"adult_nonfiction_exclusions": ["filler"]},
            "does not use ranking-table declarations: adult_nonfiction_exclusions",
        ),
    ],
)
def test_declaration_errors_are_specific(overrides: dict[str, Any], expected: str) -> None:
    error = validate_declaration(_source(**overrides))
    if expected:
        assert expected in error
    else:
        assert error == ""


def test_configuration_reports_the_same_declaration_error() -> None:
    source = _source(award_expected_row_count=0)
    errors = validate_config({"run_date": RUN_DATE, "sources": [source]})
    assert any(validate_declaration(source) in error for error in errors)


# --- Scaffold and validator agreement --------------------------------------

_OPERATOR_METADATA: dict[str, Any] = {
    "source_name": SOURCE_NAME,
    "url": URL,
    "family": "award",
    "access_status": "open",
    "check_frequency": "annual",
    "language": "en",
    "country": "Australia",
    "market": "Australia",
    "expected_category": CATEGORY,
    "signal_family": "award",
    "refresh_cadence": "annual",
    "url_stability": "year-specific",
    "expected_page_heading": HEADING,
    "expected_signal_strength": "shortlist",
    "eligible_candidate_category": "adult nonfiction",
    "extraction_notes": "Metadata only: preserve the exact title and primary author of each row.",
    "lifecycle_note": "Issue #577 probation source: active and reversible pending source metrics.",
    "notes": "Synthetic award list.",
}


def test_the_scaffold_accepts_an_award_list_without_an_identifier() -> None:
    normalized = normalize_operator_metadata({**_OPERATOR_METADATA, **AWARD_DECLARATION})
    assert normalized["contract_family"] == "award-list"
    assert normalized["award_expected_row_count"] == 3


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"award_expected_row_count": 0},
        {"award_row_format": "{title}{author}"},
        {"award_row_selector": "span"},
        {"check_frequency": "weekly", "refresh_cadence": "weekly"},
    ],
)
def test_the_scaffold_and_the_validator_apply_one_award_predicate(overrides: dict[str, Any]) -> None:
    metadata = {**_OPERATOR_METADATA, **AWARD_DECLARATION, **overrides}
    validator_error = validate_declaration(metadata)
    if validator_error:
        with pytest.raises(ScaffoldError) as raised:
            normalize_operator_metadata(metadata)
        assert str(raised.value) == validator_error
    else:
        normalize_operator_metadata(metadata)


_RANKING_METADATA: dict[str, Any] = {
    "contract_family": "ranking-table",
    "ranking_row_selector": ".row",
    "ranking_rank_source": "document_order",
    "ranking_title_selector": ".row-title",
    "ranking_author_selector": ".row-author",
    "ranking_category_marker": "Nonfiction",
    "ranking_reporting_period_undated": True,
    "ranking_expected_raw_row_count": 2,
    "ranking_evidence_url": "source_page",
    "adult_nonfiction_exclusions": ["activity book"],
}


@pytest.mark.parametrize("identifier", [{}, {"ranking_identifier_selector": ".row-isbn"}])
def test_the_scaffold_and_the_validator_agree_on_the_ranking_identifier(identifier: dict[str, Any]) -> None:
    """A ranking table still needs an identifier, and both paths say so."""
    metadata = {**_OPERATOR_METADATA, "check_frequency": "weekly", **_RANKING_METADATA, **identifier}
    validator_error = validate_ranking_contract_metadata({**metadata, "metadata_ranking_table": True})
    if identifier:
        assert validator_error == ""
        normalize_operator_metadata(metadata)
    else:
        assert "ranking_identifier_selector" in validator_error
        with pytest.raises(ScaffoldError, match="ranking_identifier_selector"):
            normalize_operator_metadata(metadata)


# --- Row format ------------------------------------------------------------


@pytest.mark.parametrize(
    ("row_format", "row", "title", "author"),
    [
        (
            "{title}, {author} ({ignored})",
            "Memorial Days, Geraldine Brooks (Hachette Australia)",
            "Memorial Days",
            "Geraldine Brooks",
        ),
        ("“{title}.” – {author}", "“Vitön.” – Bea Uusma", "Vitön", "Bea Uusma"),
        ("{author}: {title}", "Lisbeth M. Imer: Danmarks runesten", "Danmarks runesten", "Lisbeth M. Imer"),
    ],
)
def test_the_three_reported_row_shapes_parse(row_format: str, row: str, title: str, author: str) -> None:
    match = compile_row_format(row_format).fullmatch(row)
    assert match is not None
    assert (match["title"], match["author"]) == (title, author)


def test_a_row_may_state_an_optional_rank_and_identifier() -> None:
    html = _html("<li>1. First Book by An Author, ISBN 978-0-306-40615-7</li>")
    source = _source(award_row_format="{rank}. {title} by {author}, ISBN {identifier}", award_expected_row_count=1)
    page = award_page(html, source, reference_date=date.fromisoformat(RUN_DATE))
    assert (page.rows[0].rank, page.rows[0].identifier) == ("1", "9780306406157")
    grounded = _mention(title="First Book", primary_authors=["An Author"], english_title="First Book")
    assert mention_violation(grounded, grounded, {"html": html}, source) == ""
    ranked = {**grounded, "current_rank": 1, "isbn": "9780306406157"}
    assert mention_violation(ranked, ranked, {"html": html}, source) == ""
    wrong_rank = {**grounded, "current_rank": 2}
    assert "current_rank" in mention_violation(wrong_rank, wrong_rank, {"html": html}, source)
    wrong_isbn = {**grounded, "isbn": "9780140328721"}
    assert "isbn" in mention_violation(wrong_isbn, wrong_isbn, {"html": html}, source)


# --- Feasibility -----------------------------------------------------------


def test_an_unlinked_unranked_award_list_passes_feasibility() -> None:
    verdict = evaluate_source_feasibility(_source(), _evidence(_html()))
    assert verdict.outcome == OUTCOME_ACCEPT
    assert verdict.contract_family == "award-list"
    assert verdict.detected_heading == HEADING
    assert verdict.ranking_category == CATEGORY
    assert verdict.bounded_entry_count == 3
    assert verdict.reporting_period == ""
    assert [(sample.title, sample.author) for sample in verdict.samples] == [
        (row[0], display) for row, display in zip(ROWS, DISPLAYS, strict=True)
    ]
    assert {sample.url for sample in verdict.samples} == {URL}
    assert {(sample.rank, sample.identifier) for sample in verdict.samples} == {("", "")}


@pytest.mark.parametrize(
    ("html", "reason_code"),
    [
        (_html(category="General Non-Fiction Book of the Year"), "award_category_heading"),
        (_html(extra=f"<h2>{CATEGORY}</h2><ul><li>Dup, Someone (Press)</li></ul>"), "award_category_heading"),
        (_html("".join(_row(*row) for row in ROWS[:2])), "award_row_count"),
        (_html("".join(_row(*row) for row in ROWS) + _row("Fourth", "Extra Author", "Press")), "award_row_count"),
        (_html(_row(*ROWS[0]) + _row(*ROWS[1]) + "<li>A Quiet Ledger (Harbour Press)</li>"), "award_row_shape"),
        (_html(_row(*ROWS[0]) + _row(*ROWS[1]) + "<li>, Tomas Reyes (Harbour Press)</li>"), "award_row_shape"),
        (_html(_row(*ROWS[0]) + _row(*ROWS[1]) + "<li>A Quiet Ledger, & (Harbour Press)</li>"), "award_row_shape"),
        (_html("".join(_row(*row) for row in ROWS) + "<li></li>"), "award_row_shape"),
        (_html("".join(_row(*row) for row in ROWS) + "<li>&nbsp;</li>"), "award_row_shape"),
        (_html(heading="Book Awards Shortlist Announcement"), "award_year_missing"),
        (_html(heading="2025 Book Awards Shortlist Announcement"), "award_year_missing"),
        (_html(heading="2027 Book Awards Shortlist Announcement"), "award_year_superseded"),
        (_html(extra="<h2>2027 Book Awards entries are open</h2>"), "award_year_superseded"),
    ],
)
def test_feasibility_fails_closed_on_drift(html: str, reason_code: str) -> None:
    verdict = evaluate_source_feasibility(_source(), _evidence(html))
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == reason_code
    assert verdict.contract_family == "award-list"


def test_feasibility_rejects_an_incomplete_declaration() -> None:
    source = _source()
    del source["award_row_format"]
    verdict = evaluate_source_feasibility(source, _evidence(_html()))
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "award_declaration"


def test_hidden_rows_and_hidden_headings_are_not_read() -> None:
    hidden_row = '<li hidden>Hidden Book, Hidden Author (Press)</li><li style="display:none">X, Y (Z)</li>'
    html = _html("".join(_row(*row) for row in ROWS) + hidden_row, extra=f"<h2 hidden>{CATEGORY}</h2>")
    assert evaluate_source_feasibility(_source(), _evidence(html)).outcome == OUTCOME_ACCEPT


def test_unclosed_rows_end_at_the_next_row_and_at_the_next_heading() -> None:
    rows = "".join(f"<p>{title}, {author} ({publisher})" for title, author, publisher in ROWS)
    html = (
        f"<html><body><h1>{HEADING}</h1><h2>{CATEGORY}</h2>{rows}"
        "<h2>Small Publishers Book of the Year</h2><p>Third Thing, A Writer (Tiny Press)</body></html>"
    )
    page = award_page(html, _source(award_row_selector="p"), reference_date=date.fromisoformat(RUN_DATE))
    assert [row.title for row in page.rows] == [row[0] for row in ROWS]


def test_unclosed_class_rows_are_separate_rows_and_a_nested_list_stays_in_its_row() -> None:
    rows = "".join(f'<li class="book">{title}, {author} ({publisher})' for title, author, publisher in ROWS)
    html = f"<html><body><h1>{HEADING}</h1><h2>{CATEGORY}</h2><ul>{rows}</ul></body></html>"
    source = _source(award_row_selector=".book")
    page = award_page(html, source, reference_date=date.fromisoformat(RUN_DATE))
    assert [row.title for row in page.rows] == [row[0] for row in ROWS]
    nested = html.replace("(Harbour Press)<li", "(Harbour Press <ul><li>imprint</li></ul>)<li", 1)
    page = award_page(nested, source, reference_date=date.fromisoformat(RUN_DATE))
    assert [row.title for row in page.rows] == [row[0] for row in ROWS]


@pytest.mark.parametrize(
    "row",
    [
        "<li>The Long Coast, A Memoir, Mara Ellison (Harbour Press)</li>",
        "<li>The Long Coast, Mara Ellison, Tomas Reyes (Harbour Press)</li>",
        "<li>The Long Coast, Mara Ellison (ed.) (Harbour Press)</li>",
    ],
)
def test_a_row_that_divides_in_more_than_one_way_is_refused_unless_declared(row: str) -> None:
    html = _html(row + _row(*ROWS[1]) + _row(*ROWS[2]))
    verdict = evaluate_source_feasibility(_source(), _evidence(html))
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "award_row_shape"
    assert "more than one way" in verdict.reason
    declared = evaluate_source_feasibility(_source(award_row_split_first=True), _evidence(html))
    assert declared.outcome == OUTCOME_ACCEPT
    assert declared.samples[0].title == "The Long Coast"


def test_an_ambiguous_reading_through_ignored_text_is_refused() -> None:
    row = "<li>Finalist: A Story: The Coast, Writer</li>"
    source = _source(award_row_format="{ignored}: {title}, {author}", award_expected_row_count=1)
    verdict = evaluate_source_feasibility(source, _evidence(_html(row)))
    assert verdict.outcome == OUTCOME_REJECT
    assert "more than one way" in verdict.reason
    plain = evaluate_source_feasibility(source, _evidence(_html("<li>Finalist: The Coast, Writer</li>")))
    assert plain.outcome == OUTCOME_ACCEPT
    assert (plain.samples[0].title, plain.samples[0].author) == ("The Coast", "Writer")


def test_a_repeated_trailing_group_is_ambiguous_until_the_format_names_both() -> None:
    row = "<li>The Long Coast, Mara Ellison (Harbour Press) (2025)</li>"
    source = _source(award_row_format="{title}, {author} ({ignored})", award_expected_row_count=1)
    verdict = evaluate_source_feasibility(source, _evidence(_html(row)))
    assert verdict.outcome == OUTCOME_REJECT
    assert "more than one way" in verdict.reason
    two_ignored = _source(award_row_format="{title}, {author} ({ignored}) ({ignored})", award_expected_row_count=1)
    assert evaluate_source_feasibility(two_ignored, _evidence(_html(row))).outcome == OUTCOME_ACCEPT


def test_the_ambiguity_search_stays_fast_on_many_ignored_fields() -> None:
    """Every division of the ignored text gives the same title and author."""
    segments = _row_segments("{title} | {author} ({ignored}" + ", {ignored}" * 7 + ")")
    text = "Title | Author (" + ", ".join(f"v{index}" for index in range(60)) + ")"
    started = time.perf_counter()
    assert row_readings(text, segments) == {("Title", "Author")}
    assert time.perf_counter() - started < 1.0


def test_a_row_that_almost_fits_many_ignored_fields_is_refused_quickly() -> None:
    """A missing closing bracket must not make the page check retry every division."""
    row_format = "{title} | {author} (" + ", ".join(["{ignored}"] * 8) + ")"
    text = "Title | Author (" + ", ".join(f"v{index}" for index in range(60))
    started = time.perf_counter()
    assert first_reading(text, _row_segments(row_format)) is None
    source = _source(award_row_format=row_format, award_expected_row_count=1)
    verdict = evaluate_source_feasibility(source, _evidence(_html(f"<li>{text}</li>")))
    assert time.perf_counter() - started < 1.0
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "award_row_shape"


@pytest.mark.parametrize(
    ("row_format", "text"),
    [
        ("{title}, {author} ({ignored})", "The Long Coast, Mara Ellison (Harbour Press)"),
        ("{title}, {author} ({ignored})", "Salt, Sand, and Stone, Mara Ellison (Harbour (AU) Press)"),
        ("{rank}. {title} by {author}", "12. Stand by Me by Mara Ellison"),
        ("{title} - {author} - {identifier}", "The Long Coast - Mara Ellison - 978-1-76000-123-4"),
        ("{title}, {author} ({ignored}, {ignored})", "The Long Coast, Mara Ellison (Harbour, Press, 2026)"),
        ("{title}, {author} ({ignored})", "The Long Coast by Mara Ellison"),
        ("{rank}. {title} by {author}", "First. Stand by Me by Mara Ellison"),
    ],
)
def test_the_bounded_reading_is_the_one_the_format_pattern_describes(row_format: str, text: str) -> None:
    match = compile_row_format(row_format).fullmatch(text)
    assert first_reading(text, _row_segments(row_format)) == (match.groupdict() if match else None)


@pytest.mark.parametrize(
    "author", ["Martin Luther King, Jr.", "Mara Ellison, PhD", "Daniel Mercer, III, Ines Okoro", "Mara Ellison, AM"]
)
def test_a_name_suffix_is_never_read_as_a_coauthor(author: str) -> None:
    source = _source(award_row_format="{title} by {author}", award_expected_row_count=1, award_row_split_first=True)
    verdict = evaluate_source_feasibility(source, _evidence(_html(f"<li>My Life by {author}</li>")))
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "award_row_shape"
    assert "suffix of a name" in verdict.reason


def test_a_suffix_written_without_a_separator_stays_inside_the_name() -> None:
    source = _source(award_row_format="{title} by {author}", award_expected_row_count=1)
    html = _html("<li>My Life by Daniel Mercer III, Ines Okoro</li>")
    assert evaluate_source_feasibility(source, _evidence(html)).outcome == OUTCOME_ACCEPT
    assert author_names("Daniel Mercer III, Ines Okoro", source) == ("Daniel Mercer III", "Ines Okoro")


def test_an_unclosed_paragraph_row_ends_at_the_next_block() -> None:
    """A footer after an unclosed ``<p>`` must not become part of the author."""
    rows = "<p>My Life by Mara Ellison<div>Copyright 2026 Award Foundation</div>"
    source = _source(award_row_selector="p", award_row_format="{title} by {author}", award_expected_row_count=1)
    page = award_page(_html(rows), source, reference_date=date.fromisoformat(RUN_DATE))
    assert [(row.title, row.author) for row in page.rows] == [("My Life", "Mara Ellison")]


def test_an_unclosed_hidden_row_does_not_hide_the_next_row() -> None:
    rows = "<li hidden>Hidden by Nobody<li>My Life by Mara Ellison"
    source = _source(award_row_format="{title} by {author}", award_expected_row_count=1)
    page = award_page(_html(rows), source, reference_date=date.fromisoformat(RUN_DATE))
    assert [(row.title, row.author) for row in page.rows] == [("My Life", "Mara Ellison")]


def test_an_unclosed_row_keeps_the_list_nested_inside_it() -> None:
    rows = "<li>My Life by Mara Ellison<ul><li>shortlisted<li>debut</ul><li>Salt by Ines Okoro"
    source = _source(award_row_format="{title} by {author}", award_expected_row_count=2)
    page = award_page(_html(rows), source, reference_date=date.fromisoformat(RUN_DATE))
    assert [row.title for row in page.rows] == ["My Life", "Salt"]
    assert page.rows[0].author == "Mara Ellison shortlisted debut"


def test_a_year_element_inside_another_categorys_row_is_refused() -> None:
    """A book title of another category must never supply the award year."""
    rows = "<li>The Long Coast, Mara Ellison (Press)</li>"
    html = (
        "<html><body><h1>Book Awards</h1>"
        '<h2>Biography</h2><ul><li><span class="edition">Life in 2026</span>, Someone Else (House)</li></ul>'
        f"<h2>{CATEGORY}</h2><ul>{rows}</ul></body></html>"
    )
    source = _source(award_year_selector=".edition", award_expected_row_count=1, expected_page_heading="Book Awards")
    verdict = evaluate_source_feasibility(source, _evidence(html))
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "award_year_missing"
    assert "overlaps the award rows" in verdict.reason


def test_a_conjunction_after_a_comma_is_one_separator() -> None:
    source = _source(award_author_conjunction="and")
    names = ("Alice Doe", "Bob Roe", "Carol Moe")
    assert author_names("Alice Doe, Bob Roe, and Carol Moe", source) == names
    assert author_names("Alice Doe, Bob Roe and Carol Moe", source) == names
    assert author_names("Alice Doe, Andy Roe", source) == ("Alice Doe", "Andy Roe")
    undeclared = _source(award_author_conjunction="")
    assert author_names("Alice Doe, Bob Roe, and Carol Moe", undeclared) == ("Alice Doe", "Bob Roe", "and Carol Moe")


def test_a_standalone_year_paragraph_is_a_label_on_a_paragraph_row_page() -> None:
    html = (
        '<html><body><h1>Book Awards</h1><p class="edition">Edition 2026</p>'
        f"<h2>{CATEGORY}</h2><p>The Long Coast, Mara Ellison (Press)</p></body></html>"
    )
    source = _source(
        award_row_selector="p",
        award_year_selector=".edition",
        award_expected_row_count=1,
        expected_page_heading="Book Awards",
    )
    assert evaluate_source_feasibility(source, _evidence(html)).outcome == OUTCOME_ACCEPT


def test_an_unclosed_year_paragraph_ends_at_the_next_heading() -> None:
    rows = "".join(f"<li>Book {index}, Mara Ellison (Harbour Press)</li>" for index in range(12))
    html = (
        '<html><body><h1>Book Awards</h1><p class="year-label">2026 edition'
        f"<h2>{CATEGORY}</h2><ul>{rows}</ul></body></html>"
    )
    source = _source(
        award_year_selector=".year-label", award_expected_row_count=12, expected_page_heading="Book Awards"
    )
    assert evaluate_source_feasibility(source, _evidence(html)).outcome == OUTCOME_ACCEPT


def test_a_year_selector_matching_two_elements_states_no_year() -> None:
    html = (
        '<html><body><h1>Book Awards</h1><p class="edition">2026</p><p class="edition">2026</p>'
        f"<h2>{CATEGORY}</h2><ul><li>The Long Coast, Mara Ellison (Press)</li></ul></body></html>"
    )
    source = _source(award_year_selector=".edition", award_expected_row_count=1, expected_page_heading="Book Awards")
    verdict = evaluate_source_feasibility(source, _evidence(html))
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "award_year_missing"


def test_evidence_urls_must_name_the_same_query_selected_list() -> None:
    url = "https://awards.example/list?year=2026&category=nonfiction"
    other = "https://awards.example/list?year=2025&category=fiction"
    assert _violation(_mention(source_url=url, evidence_url=url), url=url) == ""
    assert "source_url is not the configured page" in _violation(_mention(source_url=other, evidence_url=url), url=url)
    assert "evidence_url is not the configured page" in _violation(
        _mention(source_url=url, evidence_url=other), url=url
    )
    assert _violation(_mention(source_url=url + "#winners", evidence_url=url), url=url) == ""


def test_a_heading_inside_another_categorys_row_does_not_open_the_category() -> None:
    html = (
        "<html><body><h1>Book Awards 2026</h1><h2>Fiction</h2><ul>"
        f"<li><h3>{CATEGORY}</h3>, A Novelist (House)</li><li>A Novel, Another Novelist (House)</li>"
        "</ul></body></html>"
    )
    source = _source(award_expected_row_count=1, expected_page_heading="Book Awards 2026")
    verdict = evaluate_source_feasibility(source, _evidence(html))
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "award_category_heading"


@pytest.mark.parametrize("field", ["source_page", "resolved_url"])
def test_a_capture_of_another_query_selected_list_is_not_the_configured_page(field: str) -> None:
    url = "https://awards.example/list?category=nonfiction"
    source = _source(url=url)
    page = {"html": _html(), "source_page": url, "resolved_url": url, "reference_date": RUN_DATE}
    assert validate_page_shape(source, page) == ""
    page[field] = "https://awards.example/list?category=fiction"
    assert f"captured {field} is not the configured award page" in validate_page_shape(source, page)


def test_a_hidden_duplicate_of_the_year_label_is_not_a_second_label() -> None:
    html = (
        '<html><body><h1>Book Awards</h1><p class="edition">Edition 2026</p>'
        '<p class="edition" hidden>Edition 2026</p><div hidden><p class="edition">Edition 2026</p></div>'
        f"<h2>{CATEGORY}</h2><ul><li>The Long Coast, Mara Ellison (Press)</li></ul></body></html>"
    )
    source = _source(award_year_selector=".edition", award_expected_row_count=1, expected_page_heading="Book Awards")
    assert evaluate_source_feasibility(source, _evidence(html)).outcome == OUTCOME_ACCEPT


def test_an_award_list_must_declare_its_signal_strength() -> None:
    assert validate_declaration(_source(expected_signal_strength="")) == "award-list requires expected_signal_strength"


def test_an_award_list_declares_no_sort_label() -> None:
    assert "does not use expected_sort_label" in validate_declaration(_source(expected_sort_label="Newest first"))
    assert validate_declaration(_source(expected_sort_label="")) == ""


def test_an_overlong_row_is_not_a_title_and_author_line() -> None:
    row = f"<li>The Long Coast, Mara Ellison ({'prose ' * 120})</li>"
    verdict = evaluate_source_feasibility(_source(award_expected_row_count=1), _evidence(_html(row)))
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "award_row_shape"


def test_a_year_element_that_contains_the_rows_is_refused() -> None:
    """A row title such as "Life in 2026" must never supply the award year."""
    rows = "<li>Life in 2026, Mara Ellison (Press)</li>"
    html = f'<html><body><h1>Book Awards</h1><div class="award"><h2>{CATEGORY}</h2><ul>{rows}</ul></div></body></html>'
    source = _source(award_year_selector=".award", award_expected_row_count=1, expected_page_heading="Book Awards")
    verdict = evaluate_source_feasibility(source, _evidence(html))
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "award_year_missing"
    assert "overlaps the award rows" in verdict.reason


def test_a_year_element_inside_a_row_is_refused() -> None:
    rows = '<li><span class="year">Life in 2026</span>, Mara Ellison (Press)</li>'
    html = f"<html><body><h1>Book Awards</h1><h2>{CATEGORY}</h2><ul>{rows}</ul></body></html>"
    source = _source(award_year_selector=".year", award_expected_row_count=1, expected_page_heading="Book Awards")
    verdict = evaluate_source_feasibility(source, _evidence(html))
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "award_year_missing"
    assert "overlaps the award rows" in verdict.reason


def test_a_heading_inside_a_row_never_states_the_award_year() -> None:
    rows = "<li><h3>Life in 2026</h3>, Mara Ellison (Press)</li>"
    html = f"<html><body><h1>Book Awards</h1><h2>{CATEGORY}</h2><ul>{rows}</ul></body></html>"
    source = _source(award_expected_row_count=1, expected_page_heading="Book Awards")
    verdict = evaluate_source_feasibility(source, _evidence(html))
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "award_year_missing"
    other = html.replace("</ul>", "</ul><h2>Biography</h2><ul><li><h3>Born in 2026</h3>, A Writer (Press)</li></ul>")
    assert evaluate_source_feasibility(source, _evidence(other)).reason_code == "award_year_missing"
    classed = (
        f"<html><body><h1>Book Awards</h1><h2>{CATEGORY}</h2>"
        '<div class="book">Plain Title, Mara Ellison (Press)</div>'
        '<h2>Biography</h2><div class="book"><h3>Born in 2026</h3>, A Writer (Press)</div></body></html>'
    )
    classed_source = {**source, "award_row_selector": ".book"}
    assert evaluate_source_feasibility(classed_source, _evidence(classed)).reason_code == "award_year_missing"
    dated = html.replace("<h1>Book Awards</h1>", "<h1>Book Awards</h1><h2>Shortlist 2026</h2>")
    accepted = evaluate_source_feasibility(source, _evidence(dated))
    assert accepted.outcome == OUTCOME_ACCEPT
    assert accepted.samples[0].title == "Life in 2026"


def test_an_unclosed_paragraph_before_the_year_heading_does_not_hide_it() -> None:
    rows = "".join(f"<p>{title}, {author} ({publisher})" for title, author, publisher in ROWS)
    html = f"<html><body><p>Welcome<h1>{HEADING}</h1><h2>{CATEGORY}</h2>{rows}</body></html>"
    assert evaluate_source_feasibility(_source(award_row_selector="p"), _evidence(html)).outcome == OUTCOME_ACCEPT


def test_a_line_break_inside_the_category_heading_keeps_its_space() -> None:
    html = _html().replace(f"<h2>{CATEGORY}</h2>", "<h2>General<br>Nonfiction Book of the Year</h2>")
    assert evaluate_source_feasibility(_source(), _evidence(html)).outcome == OUTCOME_ACCEPT


def test_a_year_selector_that_matches_a_container_is_refused() -> None:
    prose = "Our judges read widely this year. " * 8
    html = _html().replace("<h1>", f'<div class="intro">Edition 2026. {prose}</div><h1>')
    verdict = evaluate_source_feasibility(_source(award_year_selector=".intro"), _evidence(html))
    assert verdict.outcome == OUTCOME_REJECT
    assert verdict.reason_code == "award_year_missing"
    assert "matches a container" in verdict.reason


def test_a_declared_year_selector_reads_only_that_element() -> None:
    html = _html(heading="Book Awards Shortlist Announcement", extra="<h2>2027 Book Awards entries are open</h2>")
    html = html.replace("<h1>", '<p class="edition">Edition 2026</p><h1>')
    source = _source(expected_page_heading="Book Awards Shortlist Announcement", award_year_selector=".edition")
    assert evaluate_source_feasibility(source, _evidence(html)).outcome == OUTCOME_ACCEPT
    assert evaluate_source_feasibility(source, _evidence(html.replace("Edition 2026", "Edition"))).reason_code == (
        "award_year_missing"
    )


# --- Stale award year ------------------------------------------------------


@pytest.mark.parametrize(
    ("reference_date", "error"),
    [
        ("2026-10-11", ""),
        ("2027-06-01", ""),
        ("2028-01-05", "award year 2026 is stale on 2028-01-05"),
        ("2024-12-31", "award year 2026 is not a published edition on 2024-12-31"),
    ],
)
def test_a_stale_award_year_is_refused(reference_date: str, error: str) -> None:
    page = {"html": _html(), "source_page": URL, "reference_date": reference_date}
    result = validate_page_shape(_source(), page)
    if error:
        assert error in result
    else:
        assert result == ""


def test_an_award_page_without_a_reference_date_is_refused() -> None:
    with pytest.raises(AwardListError) as raised:
        award_page(_html(), _source(), reference_date=None)
    assert raised.value.reason_code == "award_reference_date"
    assert "requires a reference date" in validate_page_shape(_source(), {"html": _html(), "source_page": URL})


def test_the_annual_audit_reports_a_list_one_edition_behind() -> None:
    """Last year's list still collects, and the audit is what says so."""
    source = _source(name="Synthetic Book Awards General Nonfiction Shortlist", url="https://example.test/shortlist/")
    assert (
        validate_page_shape(source, {"html": _html(), "source_page": source["url"], "reference_date": "2027-06-01"})
        == ""
    )
    undeclared = audit_annual_source(source, today=date(2027, 6, 1))
    assert (undeclared.edition_year, undeclared.year_source) == (2026, "award_year")
    declared = {
        **source,
        "url_stability": "year-specific",
        "annual_edition": {
            "edition_year": 2026,
            "landing_url": "https://example.test/",
            "release_window": {"opens": "04-01", "closes": "04-30"},
            "stages": ["shortlist", "winner"],
            "tracked_stage": "shortlist",
        },
    }
    verdict = audit_annual_source(declared, today=date(2027, 6, 1))
    assert (verdict.reason, verdict.edition_year) == (REASON_NEWER_EDITION_EXPECTED, 2026)


# --- Mention grounding -----------------------------------------------------


def test_a_mention_grounds_in_one_row_and_cites_the_configured_page() -> None:
    assert _violation(_mention()) == ""
    coauthored = _mention(
        title="Salt and Iron",
        primary_authors=["Daniel Mercer", "Ines Okoro"],
        english_title="Salt and Iron",
        search_translation="Соль и железо",
    )
    assert _violation(coauthored) == ""


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"title": "Another Life", "english_title": "Another Life"}, "not grounded in one row"),
        ({"primary_authors": ["Tomas Reyes"]}, "not grounded in one row"),
        ({"primary_authors": ["Ellis"]}, "not grounded in one row"),
        ({"primary_authors": ["Mara", "Ellison"]}, "not grounded in one row"),
        (
            {
                "title": "Salt and Iron",
                "english_title": "Salt and Iron",
                "primary_authors": ["Daniel Mercer and Ines Okoro"],
            },
            "not grounded in one row",
        ),
        ({"isbn": "N/A"}, "isbn is not the identifier its row states"),
        ({"current_rank": "N/A"}, "current_rank is not the rank its row states"),
        ({"primary_authors": ["Mara"]}, "not grounded in one row"),
        ({"primary_authors": ["Mara Ellison", "Tomas Reyes"]}, "not grounded in one row"),
        (
            {"title": "Salt and Iron", "english_title": "Salt and Iron", "primary_authors": ["Daniel Mercer"]},
            "not grounded in one row",
        ),
        ({"primary_authors": []}, "missing a title or primary author"),
        ({"evidence_url": "https://example.test/book/the-long-coast"}, "evidence_url is not the configured page"),
        ({"source_url": "https://example.test/other"}, "source_url is not the configured page"),
        ({"current_rank": 1}, "current_rank is not the rank its row states"),
        ({"isbn": "9780306406157"}, "isbn is not the identifier its row states"),
        ({"source_week": "2026-10-05"}, "source_week the page does not state"),
        ({"signal_type": "bestseller/ranking"}, "signal_type must be award"),
        ({"signal_strength": "winner"}, "signal_strength must be shortlist"),
        ({"candidate_category": "fiction"}, "candidate_category must be adult nonfiction"),
        ({"search_translation": ""}, "helper metadata is missing: search_translation"),
        ({"publication_year": "2025"}, "prohibited fields: publication_year"),
        ({"content_description": "Книга о побережье."}, "prohibited fields: content_description"),
        ({"publisher": "Harbour Press"}, "prohibited fields: publisher"),
    ],
)
def test_an_ungrounded_mention_is_dropped(overrides: dict[str, Any], expected: str) -> None:
    assert expected in _violation(_mention(**overrides))


def test_authors_split_on_punctuation_and_the_declared_conjunction_only() -> None:
    english = {"award_author_conjunction": "and"}
    assert author_names("Helen Garner, Chloe Hooper and Sarah Krasnostein", english) == (
        "Helen Garner",
        "Chloe Hooper",
        "Sarah Krasnostein",
    )
    assert author_names("Anderson Cooper", english) == ("Anderson Cooper",)
    assert author_names("José Ortega y Gasset", english) == ("José Ortega y Gasset",)
    assert author_names("Ana Ruiz y Luis Vega", {"award_author_conjunction": "y"}) == ("Ana Ruiz", "Luis Vega")
    assert author_names("Bea Uusma och Lena Sohl", {"award_author_conjunction": "och"}) == ("Bea Uusma", "Lena Sohl")
    assert author_names("Daniel Mercer and Ines Okoro", {}) == ("Daniel Mercer and Ines Okoro",)
    assert author_display("Daniel Mercer and Ines Okoro", english) == "Daniel Mercer / Ines Okoro"


def test_a_mention_is_dropped_when_the_page_has_drifted() -> None:
    drifted = _html("".join(_row(*row) for row in ROWS[:2]))
    assert "expected exactly 3" in _violation(_mention(), drifted)
    assert "newer award year 2027" in _violation(_mention(), _html(extra="<h2>2027 shortlist</h2>"))


def test_the_extraction_contract_matches_what_grounding_enforces() -> None:
    contract = extraction_contract(_source())
    shape = contract["required_output_shape"]["mentions"][0]
    assert shape["source_url"] == shape["evidence_url"] == "The configured source page"
    assert not {"current_rank", "isbn", "source_week", "content_description", "publisher"} & shape.keys()
    assert "publication_year" not in shape
    assert not {"current_rank", "isbn", "source_week"} & set(contract["required_fields"])
    rules = " ".join(contract["rules"])
    assert CATEGORY in rules
    assert "Do not return current_rank" in rules
    ranked = extraction_contract(_source(award_row_format="{rank}. {title} by {author}"))
    assert "current_rank" in ranked["required_output_shape"]["mentions"][0]
    assert "current_rank" not in ranked["required_fields"]


# --- Collection ------------------------------------------------------------


def _write(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _replay_source(tmp_path: Path, html: str, mentions: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    page = _write(
        tmp_path / "page.json",
        {
            "source_page": URL,
            "source_name": SOURCE_NAME,
            "source_url": URL,
            "resolved_url": URL,
            "reference_date": RUN_DATE,
            "html": html,
        },
    )
    extraction = _write(tmp_path / "extraction.json", {"source_page": URL, "mentions": mentions})
    return _source(
        source_page_fixture_file=str(page),
        extraction_response_file=str(extraction),
        extraction_method="codex-extraction-replay",
        **overrides,
    )


def _collect(tmp_path: Path, source: dict[str, Any]) -> Any:
    return SourceCollection(tmp_path / "config.json", tmp_path, RUN_DATE).collect([source])


def test_collection_publishes_grounded_award_mentions(tmp_path: Path) -> None:
    result = _collect(tmp_path, _replay_source(tmp_path, _html(), [_mention()]))
    run = result.source_runs[0]
    assert run["status"] == "collected", run
    assert len(result.source_mentions) == 1
    mention = result.source_mentions[0]
    assert mention["title"] == "The Long Coast"
    assert mention["source_url"] == mention["evidence_url"] == URL
    assert mention["signal_type"] == "award"
    assert not mention.get("current_rank")
    assert not mention.get("isbn")


@pytest.mark.parametrize(
    ("html", "reason"),
    [
        (_html(category="General Non-Fiction Book of the Year"), "appears 0 times"),
        (_html("".join(_row(*row) for row in ROWS[:2])), "exposes 2 rows; expected exactly 3"),
        (_html(_row(*ROWS[0]) + _row(*ROWS[1]) + "<li>A Quiet Ledger</li>"), "award row 3 does not state"),
        (_html(heading="2027 Book Awards Shortlist Announcement"), "newer award year 2027"),
    ],
)
def test_collection_fails_closed_on_a_drifted_page(tmp_path: Path, html: str, reason: str) -> None:
    source = _replay_source(tmp_path, html, [_mention()], expected_page_heading="")
    result = _collect(tmp_path, source)
    run = result.source_runs[0]
    assert run["status"] == "failed"
    assert reason in run["reason"]
    assert result.source_mentions == []


def test_collection_fills_a_coauthored_mention_from_its_helper_sidecar(tmp_path: Path) -> None:
    """The sidecar is keyed by the probe's author display, which collection must reproduce."""
    helper_path = _write(tmp_path / "helper.json", _HELPER)
    bare = {
        key: value
        for key, value in _mention(title="Salt and Iron", primary_authors=["Daniel Mercer", "Ines Okoro"]).items()
        if key not in {"english_title", "search_translation", "author_transliteration_note"}
    }
    source = _replay_source(
        tmp_path,
        _html(),
        [bare],
        helper_metadata_file=str(helper_path),
        helper_metadata_sha256=hashlib.sha256(helper_path.read_bytes()).hexdigest(),
    )
    result = _collect(tmp_path, source)
    assert result.source_runs[0]["status"] == "collected", result.source_runs[0]
    assert result.source_mentions[0]["search_translation"] == "Соль и железо"


def test_collection_refuses_a_stale_award_year(tmp_path: Path) -> None:
    source = _replay_source(tmp_path, _html(), [_mention()])
    page = json.loads(Path(source["source_page_fixture_file"]).read_text(encoding="utf-8"))
    _write(Path(source["source_page_fixture_file"]), {**page, "reference_date": "2028-01-05"})
    result = _collect(tmp_path, source)
    run = result.source_runs[0]
    assert run["status"] == "failed"
    assert "award year 2026 is stale on 2028-01-05" in run["reason"]
    assert result.source_mentions == []


# --- Replay fixtures -------------------------------------------------------

_HELPER: dict[str, Any] = {
    "version": 1,
    "source_url": URL,
    "language": "en",
    "entries": [
        {
            "title": title,
            "author": author,
            "evidence_url": URL,
            "english_title": title,
            "search_translation": translation,
            "author_transliteration_note": transliteration,
        }
        for (title, _author, _publisher), author, translation, transliteration in zip(
            ROWS,
            DISPLAYS,
            ("Длинный берег", "Соль и железо", "Тихая книга учёта"),
            ("Мара Эллисон", "Дэниел Мерсер и Инес Окоро", "Томас Рейес"),
            strict=True,
        )
    ],
}


def _probe_evidence(html: str) -> dict[str, Any]:
    return {
        **_evidence(html),
        "source_name": SOURCE_NAME,
        "signal_family": "award",
        "contract_family": "award-list",
        "contract_metadata": dict(AWARD_DECLARATION),
    }


def _pair(html: str) -> Any:
    return build_replay_fixture_pair(
        URL,
        "synthetic-award-list",
        _probe_evidence(html),
        signal_strength="shortlist",
        candidate_category="adult nonfiction",
        language="en",
        helper_metadata=_HELPER,
    )


def test_the_replay_pair_retains_only_the_declared_category_and_collects(tmp_path: Path) -> None:
    pair = _pair(_html())
    assert pair.page["reference_date"] == RUN_DATE
    assert pair.page["source_page"] == pair.page["resolved_url"] == URL
    assert "Another Life" not in pair.page["html"]
    assert "Third Thing" not in pair.page["html"]
    assert pair.entry_count == 3
    for mention in pair.extraction["mentions"]:
        assert mention["source_url"] == mention["evidence_url"] == URL
        assert not {"current_rank", "isbn", "source_week"} & mention.keys()
    assert pair.extraction["mentions"][1]["primary_authors"] == ["Daniel Mercer", "Ines Okoro"]
    page_path = tmp_path / "page.json"
    page_path.write_bytes(pair.page_bytes)
    extraction_path = tmp_path / "extraction.json"
    extraction_path.write_bytes(pair.extraction_bytes)
    source = _source(
        source_page_fixture_file=str(page_path),
        extraction_response_file=str(extraction_path),
        extraction_method="codex-extraction-replay",
    )
    result = _collect(tmp_path, source)
    assert result.source_runs[0]["status"] == "collected", result.source_runs[0]
    assert [mention["title"] for mention in result.source_mentions] == [row[0] for row in ROWS]


def _replays(evidence: dict[str, Any], tmp_path: Path, **source_overrides: Any) -> None:
    """Generate a pair from *evidence* and collect it under the configured source."""
    pair = build_replay_fixture_pair(
        URL,
        "synthetic-award-list",
        evidence,
        signal_strength="shortlist",
        candidate_category="adult nonfiction",
        language="en",
        helper_metadata=_HELPER,
    )
    (tmp_path / "page.json").write_bytes(pair.page_bytes)
    (tmp_path / "extraction.json").write_bytes(pair.extraction_bytes)
    source = _source(
        source_page_fixture_file=str(tmp_path / "page.json"),
        extraction_response_file=str(tmp_path / "extraction.json"),
        extraction_method="codex-extraction-replay",
        **source_overrides,
    )
    result = _collect(tmp_path, source)
    assert result.source_runs[0]["status"] == "collected", result.source_runs[0]
    assert len(result.source_mentions) == 3


def test_replay_keeps_div_rows_as_separate_rows(tmp_path: Path) -> None:
    rows = "".join(f"<div>{title}, {author} ({publisher})</div>" for title, author, publisher in ROWS)
    html = f"<html><body><h1>{HEADING}</h1><h2>{CATEGORY}</h2>{rows}</body></html>"
    evidence = _probe_evidence(html)
    evidence["contract_metadata"]["award_row_selector"] = "div"
    _replays(evidence, tmp_path, award_row_selector="div")


def test_replay_renders_a_shared_title_and_year_element_once(tmp_path: Path) -> None:
    html = _html().replace(f"<h1>{HEADING}</h1>", f'<div class="award-title">{HEADING}</div>')
    evidence = _probe_evidence(html)
    evidence["contract_metadata"]["award_year_selector"] = ".award-title"
    evidence["visible_title_selector"] = ".award-title"
    _replays(evidence, tmp_path, award_year_selector=".award-title", visible_title_selector=".award-title")


def test_replay_renders_a_page_heading_that_is_the_category_heading_once(tmp_path: Path) -> None:
    category = "2026 General Nonfiction Shortlist"
    rows = "".join(_row(*row) for row in ROWS)
    html = f"<html><body><h1>{category}</h1><ul>{rows}</ul></body></html>"
    evidence = _probe_evidence(html)
    evidence["contract_metadata"]["award_category_heading"] = category
    _replays(evidence, tmp_path, award_category_heading=category, expected_page_heading=category)


def test_replay_keeps_a_standalone_year_element_out_of_the_category(tmp_path: Path) -> None:
    category = "General Nonfiction Shortlist"
    rows = "".join(f"<div>{title}, {author} ({publisher})</div>" for title, author, publisher in ROWS)
    html = f'<html><body><p class="edition">Edition 2026</p><h1>{category}</h1>{rows}</body></html>'
    declaration = {"award_category_heading": category, "award_year_selector": ".edition", "award_row_selector": "div"}
    evidence = _probe_evidence(html)
    evidence["contract_metadata"].update(declaration)
    _replays(evidence, tmp_path, expected_page_heading=category, **declaration)


def test_replay_renders_an_ancestor_shared_by_the_title_and_the_year_once(tmp_path: Path) -> None:
    hero = f'<div class="hero"><p>Edition 2026</p><div class="page-title">{HEADING[5:]}</div></div>'
    html = _html().replace(f"<h1>{HEADING}</h1>", hero)
    evidence = _probe_evidence(html)
    evidence["contract_metadata"]["award_year_selector"] = ".hero"
    evidence["visible_title_selector"] = ".hero > .page-title"
    _replays(
        evidence,
        tmp_path,
        award_year_selector=".hero",
        visible_title_selector=".hero > .page-title",
        expected_page_heading=HEADING[5:],
    )


def test_replay_states_the_title_once_inside_a_year_label_that_contains_it(tmp_path: Path) -> None:
    """A label near the length limit must not grow past it in the replay page."""
    title = "The Synthetic Book Awards for Writing Published in Australia and New Zealand"
    hero = f'<div class="hero"><p>Edition 2026, announced in Sydney</p><div class="page-title">{title}</div></div>'
    html = _html().replace(f"<h1>{HEADING}</h1>", hero)
    evidence = _probe_evidence(html)
    evidence["contract_metadata"]["award_year_selector"] = ".hero"
    evidence["visible_title_selector"] = ".hero > .page-title"
    declaration = {
        "award_year_selector": ".hero",
        "visible_title_selector": ".hero > .page-title",
        "expected_page_heading": title,
    }
    _replays(evidence, tmp_path, **declaration)
    pair = build_replay_fixture_pair(
        URL,
        "synthetic-award-list",
        evidence,
        signal_strength="shortlist",
        candidate_category="adult nonfiction",
        language="en",
        helper_metadata=_HELPER,
    )
    assert pair.page_bytes.decode().count(title) == 1


def test_replay_renders_a_category_heading_that_states_the_year_once(tmp_path: Path) -> None:
    category = "2026 General Nonfiction Shortlist"
    rows = "".join(_row(*row) for row in ROWS)
    html = f"<html><body><h1>Book Awards</h1><h2>{category}</h2><ul>{rows}</ul></body></html>"
    evidence = _probe_evidence(html)
    evidence["contract_metadata"]["award_category_heading"] = category
    _replays(evidence, tmp_path, award_category_heading=category, expected_page_heading="Book Awards")


def test_replay_renders_a_year_label_inside_the_title_element_once(tmp_path: Path) -> None:
    title = '<h1 class="heading"><span class="year">2026</span> Book Awards Shortlist Announcement</h1>'
    html = _html().replace(f"<h1>{HEADING}</h1>", title)
    evidence = _probe_evidence(html)
    evidence["contract_metadata"]["award_year_selector"] = ".heading > .year"
    evidence["visible_title_selector"] = ".heading"
    _replays(evidence, tmp_path, award_year_selector=".heading > .year", visible_title_selector=".heading")


def test_replay_keeps_the_title_selector_a_probe_retained_at_the_top_level(tmp_path: Path) -> None:
    html = _html().replace(f"<h1>{HEADING}</h1>", f'<h1 class="page-title">{HEADING}</h1><h1>Site name</h1>')
    evidence = _probe_evidence(html)
    evidence["visible_title_selector"] = ".page-title"
    _replays(evidence, tmp_path, visible_title_selector=".page-title")


def test_replay_generation_refuses_evidence_that_is_not_an_accepted_award_list() -> None:
    with pytest.raises(ReplayFixtureError, match="award_row_count"):
        _pair(_html("".join(_row(*row) for row in ROWS[:2])))


# --- Probe -----------------------------------------------------------------


class _ProbeDate(date):
    @classmethod
    def today(cls) -> _ProbeDate:
        return cls(2026, 10, 11)


def _response(html: str) -> Any:
    class Response:
        status = 200

        def read(self, _limit: int) -> bytes:
            return html.encode()

        def geturl(self) -> str:
            return URL

        def close(self) -> None:
            return None

    return Response()


def test_the_probe_accepts_an_award_list_and_retains_a_replayable_capture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("autoscout.source_probe.date", _ProbeDate)
    declaration = {key: value for key, value in AWARD_DECLARATION.items() if key != "contract_family"}
    code = probe_source_candidates(
        [URL],
        [SOURCE_NAME],
        ["award"],
        contract_families=["award-list"],
        contract_metadata=[declaration],
        keep_captures=tmp_path / "captures",
        ledger_path=tmp_path / "ledger.json",
        feasibility_dir=tmp_path / "drafts",
        opener=lambda _request, **_kwargs: _response(_html()),
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 0, report
    candidate = report["candidates"][0]
    assert candidate["outcome"] == OUTCOME_ACCEPT
    assert candidate["contract_family"] == "award-list"
    assert candidate["contract_metadata"]["award_row_format"] == AWARD_DECLARATION["award_row_format"]
    capture = json.loads(next((tmp_path / "captures").glob("*.json")).read_text(encoding="utf-8"))
    pair = build_replay_fixture_pair(
        URL,
        "synthetic-award-list",
        capture,
        signal_strength="shortlist",
        candidate_category="adult nonfiction",
        language="en",
        helper_metadata=_HELPER,
    )
    assert pair.entry_count == 3


def test_the_probe_refuses_an_incomplete_award_declaration_before_network(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = probe_source_candidates(
        [URL],
        contract_families=["award-list"],
        contract_metadata=[{"award_year": 2026}],
        ledger_path=tmp_path / "ledger.json",
        opener=lambda *_args, **_kwargs: pytest.fail("network must not be called"),
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 2
    assert "award-list requires award_category_heading" in report["error"]
    assert not (tmp_path / "ledger.json").exists()
