from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from autoscout.capture_bounds import CAPTURE_SERIALIZATION_HEADROOM_FACTOR
from autoscout.config import validate_config
from autoscout.source_collection import SourceCollection
from autoscout.source_collection import prepare_codex_extraction_worklist
from autoscout.source_page_identity import ranking_detail_fetch_plan
from autoscout.source_page_identity import ranking_detail_fields_from_html
from autoscout.source_page_identity import ranking_detail_scope
from autoscout.source_page_identity import ranking_rows
from autoscout.source_page_identity import validate_page_shape
from autoscout.source_page_identity import validate_ranking_contract_metadata
from autoscout.source_page_identity import validate_ranking_detail_pages

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "issue-585-ranking-detail-route"


def ranking_source(**overrides: Any) -> dict[str, Any]:
    source: dict[str, Any] = {
        "name": "Synthetic detail ranking",
        "family": "bestseller/ranking",
        "url": "https://example.test/ranking",
        "active": True,
        "access_status": "open",
        "language": "en",
        "contract_family": "ranking-table",
        "metadata_ranking_table": True,
        "ranking_evidence_url": "source_page",
        "ranking_row_selector": ".row",
        "ranking_rank_source": "selector",
        "ranking_rank_selector": ".rank",
        "ranking_title_selector": ".title",
        "ranking_author_selector": ".author",
        "ranking_category_marker": "Adult nonfiction",
        "ranking_reporting_period_pattern": r"(?P<period>2026-08-24)",
        "ranking_reporting_period_date_format": "%Y-%m-%d",
        "ranking_reporting_period_max_age_days": 14,
        "ranking_identifier_pattern": r"(?P<identifier>97[89][0-9-]{10,20})",
        "ranking_expected_raw_row_count": 1,
        "adult_nonfiction_exclusions": ["activity book"],
        "expected_page_heading": "Adult nonfiction 2026-08-24",
        "visible_title_selector": ".heading",
    }
    source.update(overrides)
    return source


def detail_source(**overrides: Any) -> dict[str, Any]:
    source = ranking_source(
        ranking_evidence_url="detail_page",
        ranking_detail_link_selector=".detail-link",
        ranking_detail_url_pattern=r"https://example\.test/books/[0-9]+",
        ranking_detail_scope_selector=".product-detail",
        ranking_detail_fields=["isbn", "category"],
        ranking_detail_identifier_selector=".isbn",
        ranking_detail_identifier_pattern=r"ISBN:\s*(?P<identifier>97[89][0-9-]{10,20})",
        ranking_detail_category_selector=".category",
        ranking_detail_category_pattern=r"Category:\s*(?P<category>[^<]+)",
        ranking_detail_category_allowlist=["Historia de América"],
        ranking_detail_max_pages=1,
        eligible_candidate_category="adult nonfiction",
        ranking_identifier_selector="",
        ranking_identifier_attribute="",
        ranking_identifier_pattern="",
    )
    source.update(overrides)
    return source


def test_detail_route_requires_canonical_category_separately_from_raw_allowlist() -> None:
    source = detail_source()
    assert validate_config({"sources": [source]}) == []
    assert source["eligible_candidate_category"] == "adult nonfiction"
    assert source["ranking_detail_category_allowlist"] == ["Historia de América"]
    assert validate_config({"sources": [detail_source(eligible_candidate_category="Historia de América")]}) == []


def test_detail_route_requires_configured_canonical_category() -> None:
    errors = validate_config({"sources": [detail_source(eligible_candidate_category=" ")]})
    assert any("eligible_candidate_category" in error for error in errors)


def test_malformed_detail_url_pattern_fails_closed_outside_config_validation() -> None:
    from datetime import date

    from autoscout.ranking_detail_route import RankingDetailRoute
    from autoscout.source_feasibility import evaluate_ranking_table_feasibility

    source = detail_source(ranking_detail_url_pattern="[")
    with pytest.raises(ValueError, match="ranking_detail_url_pattern is malformed"):
        RankingDetailRoute.from_source(source)

    rows, fetch_error = ranking_detail_fetch_plan(source, list_html())
    assert rows == []
    assert "ranking_detail_url_pattern is malformed" in fetch_error
    detail_error = validate_ranking_detail_pages(source, {"html": list_html(), "detail_pages": []})
    assert "ranking_detail_url_pattern is malformed" in detail_error
    verdict = evaluate_ranking_table_feasibility(
        source,
        head_status=200,
        get_status=200,
        resolved_url=source["url"],
        source_page=source["url"],
        html=list_html(),
        reference_date=date(2026, 8, 24),
    )
    assert not verdict.accepted


def test_detail_prompt_never_offers_raw_category_as_candidate_category() -> None:
    from autoscout.source_collectors.contract import agent_extraction_prompt

    invalid_prompt = agent_extraction_prompt(
        detail_source(eligible_candidate_category=""), {"html": list_html(), "detail_pages": []}, 4_000
    )
    assert "Exact allowed category captured from the bounded detail page" not in invalid_prompt
    valid_prompt = agent_extraction_prompt(detail_source(), {"html": list_html(), "detail_pages": []}, 4_000)
    assert "candidate_category" in valid_prompt
    assert "adult nonfiction" in valid_prompt


def test_category_detail_route_allows_empty_or_absent_title_exclusions() -> None:
    assert validate_config({"sources": [detail_source(adult_nonfiction_exclusions=[])]}) == []
    source = detail_source()
    source.pop("adult_nonfiction_exclusions")
    assert validate_config({"sources": [source]}) == []


def test_single_page_ranking_still_requires_nonempty_title_exclusions() -> None:
    errors = validate_config({"sources": [ranking_source(adult_nonfiction_exclusions=[])]})
    assert any("adult_nonfiction_exclusions" in error for error in errors)


def _detail_operator_metadata(**overrides: Any) -> dict[str, Any]:
    from autoscout.source_metadata import SOURCE_SCAFFOLD_REQUIRED_METADATA_FIELDS

    metadata: dict[str, Any] = {field: "x" for field in SOURCE_SCAFFOLD_REQUIRED_METADATA_FIELDS}
    metadata.update(
        {
            "source_name": "Cúspide detail ranking",
            "url": "https://example.test/ranking",
            "family": "bestseller/ranking",
            "signal_family": "bestseller/ranking",
            "language": "en",
            "expected_page_heading": "Adult nonfiction 2026-08-24",
            "expected_signal_strength": "top ranking",
            "eligible_candidate_category": "adult nonfiction",
            "lifecycle_note": "Issue #585: active and reversible probation.",
            "contract_family": "ranking-table",
            "metadata_ranking_table": True,
            "ranking_evidence_url": "detail_page",
            "ranking_row_selector": ".row",
            "ranking_rank_source": "selector",
            "ranking_rank_selector": ".rank",
            "ranking_title_selector": ".title",
            "ranking_author_selector": ".author",
            "ranking_category_marker": "Adult nonfiction",
            "ranking_reporting_period_pattern": r"(?P<period>2026-08-24)",
            "ranking_reporting_period_date_format": "%Y-%m-%d",
            "ranking_reporting_period_max_age_days": 14,
            "ranking_expected_raw_row_count": 1,
            "ranking_detail_link_selector": ".name",
            "ranking_detail_url_pattern": r"https://example\.test/books/[0-9]+",
            "ranking_detail_scope_selector": ".product-detail",
            "ranking_detail_fields": ["isbn", "category"],
            "ranking_detail_identifier_selector": ".isbn",
            "ranking_detail_identifier_pattern": r"ISBN:\s*(?P<identifier>97[89][0-9-]{10,20})",
            "ranking_detail_category_selector": ".category",
            "ranking_detail_category_pattern": r"Category:\s*(?P<category>[^<]+)",
            "ranking_detail_category_allowlist": ["Historia de América"],
            "ranking_detail_max_pages": 100,
            "adult_nonfiction_exclusions": [],
        }
    )
    metadata.update(overrides)
    return metadata


def test_scaffold_accepts_empty_category_exclusions_and_enforces_detail_page_ceiling() -> None:
    from autoscout.source_scaffold import ScaffoldError
    from autoscout.source_scaffold import normalize_operator_metadata

    normalized = normalize_operator_metadata(_detail_operator_metadata())
    assert normalized["adult_nonfiction_exclusions"] == []
    absent = _detail_operator_metadata()
    absent.pop("adult_nonfiction_exclusions")
    absent_normalized = normalize_operator_metadata(absent)
    from autoscout.source_scaffold import _source_entry

    assert "adult_nonfiction_exclusions" not in _source_entry(absent_normalized)
    with pytest.raises(ScaffoldError, match="must not exceed 200"):
        normalize_operator_metadata(_detail_operator_metadata(ranking_detail_max_pages=201))


def test_detail_route_page_cap_accepts_cuspide_100_and_rejects_201() -> None:
    assert validate_config({"sources": [detail_source(ranking_detail_max_pages=100)]}) == []
    errors = validate_config({"sources": [detail_source(ranking_detail_max_pages=201)]})
    assert any("must not exceed 200" in error for error in errors)


def test_capture_live_response_scopes_new_shape_and_sanitization_to_detail_routes(tmp_path: Path) -> None:
    from autoscout.source_collectors.base import capture_live_response

    generic_path = tmp_path / "generic.json"
    generic_source = {
        "name": "Generic source",
        "url": "https://example.test/list",
        "capture_live_response_file": generic_path.name,
    }
    generic_data = {
        "source_page": generic_source["url"],
        "resolved_url": "https://example.test/redirected",
        "html": '<input name="csrf-token" value="secret">',
    }
    capture_live_response(tmp_path / "config.json", tmp_path, generic_source, generic_data)
    generic_capture = json.loads(generic_path.read_text(encoding="utf-8"))
    assert generic_capture == {
        "collection_mode": "live",
        "source_url": generic_source["url"],
        "source_name": generic_source["name"],
        "source_page": generic_source["url"],
        "html": generic_data["html"],
    }

    detail_path = tmp_path / "detail.json"
    detail_source_with_capture = detail_source(capture_live_response_file=detail_path.name)
    capture_live_response(
        tmp_path / "config.json",
        tmp_path,
        detail_source_with_capture,
        {
            "source_page": detail_source_with_capture["url"],
            "resolved_url": detail_source_with_capture["url"],
            "html": generic_data["html"],
            "detail_pages": detail_page(),
        },
    )
    detail_capture = json.loads(detail_path.read_text(encoding="utf-8"))
    assert detail_capture["resolved_url"] == detail_source_with_capture["url"]
    assert "REDACTED" in detail_capture["html"]
    assert detail_capture["detail_pages"][0]["detail_url"] == detail_source_with_capture["url"].replace(
        "/ranking", "/books/1"
    )


def test_detail_route_can_supply_isbn_without_list_identifier() -> None:
    assert validate_config({"sources": [detail_source()]}) == []


def test_detail_route_requires_all_route_declarations() -> None:
    errors = validate_config({"sources": [detail_source(ranking_detail_category_allowlist=[])]})
    assert any("ranking_detail_category_allowlist" in error for error in errors)


def test_detail_route_rejects_partial_or_contradictory_fields() -> None:
    errors = validate_config(
        {
            "sources": [
                detail_source(
                    ranking_detail_fields=["isbn"],
                    ranking_detail_category_selector=".category",
                )
            ]
        }
    )
    assert any("category" in error for error in errors)


def test_detail_route_rejects_malformed_field_pattern_without_capture() -> None:
    error = validate_ranking_contract_metadata(detail_source(ranking_detail_identifier_pattern=r"ISBN: 978"))
    assert "capture group" in error


def test_detail_route_keeps_list_identifier_requirement_without_isbn_field() -> None:
    errors = validate_config(
        {
            "sources": [
                detail_source(
                    ranking_detail_fields=["category"],
                    ranking_detail_identifier_selector="",
                    ranking_detail_identifier_pattern="",
                    ranking_identifier_pattern="",
                )
            ]
        }
    )
    assert any("ranking_identifier" in error for error in errors)


def test_uppercase_detail_path_matches_the_case_insensitive_route_policy() -> None:
    rows = ranking_rows(
        '<div class="row"><span class="rank">1</span><span class="title">A Book</span>'
        '<span class="author">An Author</span><a class="detail-link" href="/BOOKS/1">Details</a></div>',
        detail_source(),
    )
    assert rows[0].detail_url == "https://example.test/BOOKS/1"
    assert rows[0].detail_link_error == ""


def test_ranking_rows_parse_one_grounded_detail_link_per_row() -> None:
    rows = ranking_rows(
        '<div class="row"><span class="rank">1</span>'
        '<span class="title">A Book</span><span class="author">An Author</span>'
        '<a class="detail-link" href="/books/1">Details</a></div>',
        detail_source(),
    )
    assert len(rows) == 1
    assert rows[0].detail_url == "https://example.test/books/1"
    assert rows[0].detail_link_error == ""


@pytest.mark.parametrize(
    "container",
    [
        '<h3 class="name" hidden><a href="/books/1">Title</a></h3>',
        '<h3 class="name"><a href="/books/1" hidden>Title</a></h3>',
    ],
)
def test_ranking_rows_ignore_hidden_detail_link_evidence(container: str) -> None:
    source = detail_source(ranking_detail_link_selector=".name")
    rows = ranking_rows(
        '<div class="row"><span class="rank">1</span><span class="title">A Book</span>'
        '<span class="author">An Author</span>' + container + "</div>",
        source,
    )
    assert rows[0].detail_url == ""
    assert "detail link" in rows[0].detail_link_error


def test_ranking_rows_accepts_a_link_inside_the_selected_cuspide_title_container() -> None:
    source = detail_source(
        ranking_detail_link_selector=".name", ranking_detail_url_pattern=r"https://example\.test/product/[a-z]+"
    )
    rows = ranking_rows(
        '<div class="row"><span class="rank">1</span><span class="title">A Book</span>'
        '<span class="author">An Author</span><h3 class="name"><a href="/product/one">Title</a></h3>'
        '<a href="/product/two">Footer-like sibling</a></div>',
        source,
    )
    assert rows[0].detail_url == "https://example.test/product/one"
    assert rows[0].detail_link_error == ""


@pytest.mark.parametrize(
    "markup",
    [
        '<div class="row"><span class="rank">1</span><span class="title">A</span><span class="author">B</span></div>',
        '<div class="row"><span class="rank">1</span><span class="title">A</span>'
        '<span class="author">B</span><a class="detail-link" href="/books/1">1</a>'
        '<a class="detail-link" href="/books/1">2</a></div>',
        '<div class="row"><span class="rank">1</span><span class="title">A</span>'
        '<span class="author">B</span><a class="detail-link" href="https://other.test/books/1">1</a></div>',
    ],
)
def test_ranking_rows_fail_closed_for_invalid_detail_link_shape(markup: str) -> None:
    rows = ranking_rows(markup, detail_source())
    assert len(rows) == 1
    assert rows[0].detail_url == ""
    assert rows[0].detail_link_error


def grounded_detail_source(**overrides: Any) -> dict[str, Any]:
    return detail_source(
        ranking_reporting_period_pattern=r"(?P<period>2026-08-17 - 2026-08-24)",
        expected_page_heading="Adult nonfiction 2026-08-17 - 2026-08-24",
        eligible_candidate_category="adult nonfiction",
        **overrides,
    )


def list_html(detail_url: str = "/books/1") -> str:
    return (
        '<h1 class="heading">Adult nonfiction 2026-08-17 - 2026-08-24</h1>'
        '<div class="row"><span class="rank">1</span><span class="title">A Book</span>'
        '<span class="author">An Author</span><a class="detail-link" href="' + detail_url + '">Details</a></div>'
        "<p>Source: Circana BookScan 2026-08-17 - 2026-08-24</p>"
    )


def detail_page(
    *,
    detail_url: str = "https://example.test/books/1",
    isbn: str = "9780306406157",
    category: str = "Historia de América",
    scope_class: str = "product-detail",
) -> list[dict[str, str]]:
    return [
        {
            "source_page": detail_url,
            "detail_url": detail_url,
            "resolved_url": detail_url,
            "html": (
                f'<div class="{scope_class}"><span class="isbn">ISBN: {isbn}</span>'
                f'<span class="category">Category: {category}</span></div>'
            ),
        }
    ]


@pytest.mark.parametrize(
    "markup, expected_error",
    [
        (
            '<div class="product-detail"><span class="isbn">ISBN: 9780306406157</span></div>'
            '<span class="category">Category: Historia de América</span>',
            "category",
        ),
        (
            '<div class="product-detail"><span class="isbn">ISBN: 9780306406157</span>'
            '<span class="category">Category: Historia de América</span></div>'
            '<span class="isbn">ISBN: 9780140328721</span>',
            "",
        ),
    ],
)
def test_detail_scope_and_fields_do_not_read_after_their_closing_element(markup: str, expected_error: str) -> None:
    scope = ranking_detail_scope(markup, ".product-detail")
    assert scope.failure_reason == ""
    assert "9780140328721" not in scope.html
    values, error = ranking_detail_fields_from_html(markup, grounded_detail_source())
    assert expected_error in error
    if not expected_error:
        assert values == {"isbn": "9780306406157", "category": "Historia de América"}


def test_detail_scope_rejects_ambiguous_and_hidden_matches() -> None:
    ambiguous = ranking_detail_scope(
        '<div class="product-detail">One</div><div class="product-detail">Two</div>', ".product-detail"
    )
    hidden = ranking_detail_scope('<div class="product-detail" hidden>Hidden</div>', ".product-detail")
    assert ambiguous.failure_reason == "detail scope is ambiguous"
    assert hidden.failure_reason == "detail scope is hidden"


@pytest.mark.parametrize(
    "markup",
    [
        '<div class="product-detail"><span class="isbn">ISBN: 9780306406157</span>'
        '<span class="category">Category: Historia de América</span>'
        '<span class="category">Category: Adult fiction</span></div>',
        '<div class="product-detail"><span class="isbn">ISBN: 9780306406157</span>'
        '<span class="category" hidden>Category: Historia de América</span></div>',
    ],
)
def test_detail_fields_reject_ambiguous_or_hidden_matches(markup: str) -> None:
    values, error = ranking_detail_fields_from_html(markup, grounded_detail_source())
    assert values == {}
    assert "category" in error


@pytest.mark.parametrize(
    "markup",
    [
        '<div class="product-detail"><span class="isbn" hidden>ISBN: 9780306406157</span>'
        '<span class="category">Category: Historia de América</span></div>',
        '<div class="product-detail"><script>ISBN: 9780306406157</script>'
        '<span class="category">Category: Historia de América</span></div>',
        '<div class="product-detail"><!-- ISBN: 9780306406157 -->'
        '<span class="category">Category: Historia de América</span></div>',
    ],
)
def test_detail_identifier_pattern_reads_only_visible_scope_text(markup: str) -> None:
    source = grounded_detail_source(
        ranking_detail_fields=["isbn", "category"],
        ranking_detail_identifier_selector="",
        ranking_detail_identifier_attribute="",
    )
    values, error = ranking_detail_fields_from_html(markup, source)
    assert values == {}
    assert "identifier" in error


def test_detail_field_attribute_can_read_only_a_visible_attribute_match() -> None:
    source = grounded_detail_source(
        ranking_detail_identifier_selector="",
        ranking_detail_identifier_attribute="data-isbn",
        ranking_detail_identifier_pattern="",
    )
    values, error = ranking_detail_fields_from_html(
        '<div class="product-detail"><span data-isbn="9780306406157">ISBN</span>'
        '<span class="category">Category: Historia de América</span></div>',
        source,
    )
    assert error == ""
    assert values["isbn"] == "9780306406157"


def test_detail_field_does_not_read_an_undeclared_attribute() -> None:
    source = grounded_detail_source(
        ranking_detail_identifier_selector=".isbn",
        ranking_detail_identifier_attribute="isbn-value",
        ranking_detail_identifier_pattern="",
    )
    values, error = ranking_detail_fields_from_html(
        '<div class="product-detail"><span class="isbn" data-isbn-value="9780306406157">ISBN</span>'
        '<span class="category">Category: Historia de América</span></div>',
        source,
    )
    assert values == {}
    assert "identifier" in error


def test_detail_field_attribute_reads_only_a_visible_selected_element() -> None:
    source = grounded_detail_source(
        ranking_detail_identifier_selector=".isbn",
        ranking_detail_identifier_attribute="data-isbn",
        ranking_detail_identifier_pattern="",
    )
    values, error = ranking_detail_fields_from_html(
        '<div class="product-detail"><span class="isbn" hidden data-isbn="9780306406157">ISBN</span>'
        '<span class="category">Category: Historia de América</span></div>',
        source,
    )
    assert values == {}
    assert "visible" in error


def test_detail_grounding_uses_canonical_url_identity_for_evidence_urls() -> None:
    from autoscout.source_page_identity import ranking_mention_is_grounded

    assert ranking_mention_is_grounded(
        list_html(),
        title="A Book",
        primary_authors=["An Author"],
        isbn="9780306406157",
        current_rank="1",
        source_week="2026-08-17 - 2026-08-24",
        ranking_source=grounded_detail_source(),
        detail_pages=detail_page(),
        evidence_url="https://example.test/books/1/",
        candidate_category="Adult nonfiction",
    )


def test_two_page_ranking_grounding_requires_list_and_detail_evidence() -> None:
    from autoscout.source_page_identity import ranking_mention_is_grounded

    assert ranking_mention_is_grounded(
        list_html(),
        title="A Book",
        primary_authors=["An Author"],
        isbn="9780306406157",
        current_rank="1",
        source_week="2026-08-17 - 2026-08-24",
        ranking_source=grounded_detail_source(),
        detail_pages=detail_page(),
        evidence_url="https://example.test/books/1",
        candidate_category="Adult nonfiction",
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"isbn": "9780306406158"},
        {"evidence_url": "https://example.test/books/2"},
        {"detail_pages": detail_page(scope_class="other-scope")},
        {"candidate_category": "Adult fiction"},
    ],
)
def test_two_page_ranking_grounding_fails_closed(kwargs: dict[str, Any]) -> None:
    from autoscout.source_page_identity import ranking_mention_is_grounded

    values: dict[str, Any] = {
        "title": "A Book",
        "primary_authors": ["An Author"],
        "isbn": "9780306406157",
        "current_rank": "1",
        "source_week": "2026-08-17 - 2026-08-24",
        "ranking_source": grounded_detail_source(),
        "detail_pages": detail_page(),
        "evidence_url": "https://example.test/books/1",
        "candidate_category": "Adult nonfiction",
    }
    values.update(kwargs)
    assert not ranking_mention_is_grounded(list_html(), **values)


def ranking_entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "title": "A Book",
        "primary_authors": ["An Author"],
        "isbn": "9780306406157",
        "current_rank": "1",
        "source_week": "2026-08-17 - 2026-08-24",
        "source_url": "https://example.test/ranking",
        "evidence_url": "https://example.test/books/1",
        "signal_type": "bestseller/ranking",
        "signal_strength": "top ranking",
        "evidence_note": "Ranked in the configured weekly table.",
        "candidate_category": "Adult nonfiction",
    }
    entry.update(overrides)
    return entry


def test_extraction_contract_violation_accepts_grounded_two_page_entry() -> None:
    from autoscout.source_collectors.base import extraction_contract_violation

    source = grounded_detail_source()
    assert (
        extraction_contract_violation(
            ranking_entry(),
            ranking_entry(),
            source,
            {
                "source_page": "https://example.test/ranking",
                "html": list_html(),
                "detail_pages": detail_page(),
            },
        )
        == ""
    )


def test_ranking_contract_always_requires_isbn_even_when_detail_supplies_it() -> None:
    from autoscout.source_collectors.base import extraction_contract_violation
    from autoscout.source_collectors.contract import extraction_contract

    source = grounded_detail_source()
    assert "isbn" in extraction_contract(source)["required_fields"]
    reason = extraction_contract_violation(
        ranking_entry(isbn=""),
        ranking_entry(isbn=""),
        source,
        {"source_page": source["url"], "html": list_html(), "detail_pages": detail_page()},
    )
    assert reason == "ranking table mention missing required metadata: isbn"


def test_detail_feasibility_deduplicates_by_grounded_isbn_not_detail_url() -> None:
    from datetime import date

    from autoscout.source_feasibility import evaluate_ranking_table_feasibility

    source = grounded_detail_source(
        ranking_expected_raw_row_count=2, ranking_detail_max_pages=2, adult_nonfiction_exclusions=[]
    )
    list_markup = list_html().replace(
        "</p>",
        '<div class="row"><span class="rank">2</span><span class="title">Another Book</span>'
        '<span class="author">Another Author</span><a class="detail-link" href="/books/2">Details</a></div></p>',
    )
    pages = detail_page(isbn="9780306406157") + detail_page(
        detail_url="https://example.test/books/2", isbn="9780306406157"
    )
    verdict = evaluate_ranking_table_feasibility(
        source,
        head_status=200,
        get_status=200,
        resolved_url=source["url"],
        source_page=source["url"],
        html=list_markup,
        reference_date=date(2026, 8, 24),
        detail_pages=pages,
    )
    assert verdict.accepted
    assert len(verdict.samples) == 1
    assert verdict.samples[0].url == "https://example.test/books/1"


def test_extraction_contract_violation_reports_ungrounded_detail_field() -> None:
    from autoscout.source_collectors.base import extraction_contract_violation

    source = grounded_detail_source()
    reason = extraction_contract_violation(
        ranking_entry(isbn="9780306406158"),
        ranking_entry(isbn="9780306406158"),
        source,
        {
            "source_page": "https://example.test/ranking",
            "html": list_html(),
            "detail_pages": detail_page(),
        },
    )
    assert "not grounded" in reason


def test_codex_replay_collects_two_page_ranking_fixture_without_network(tmp_path: Any) -> None:
    import json

    source = grounded_detail_source(name="Replay detail ranking")
    page_path = tmp_path / "page.json"
    page_path.write_text(
        json.dumps(
            {
                "source_page": source["url"],
                "resolved_url": source["url"],
                "reference_date": "2026-08-24",
                "html": list_html(),
                "detail_pages": detail_page(),
            }
        ),
        encoding="utf-8",
    )
    extraction_path = tmp_path / "extraction.json"
    extraction_path.write_text(json.dumps({"mentions": [ranking_entry()]}), encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")

    result = SourceCollection(config_path, tmp_path / "runs", "2026-08-24").collect(
        [
            {
                **source,
                "extraction_method": "codex-extraction-replay",
                "source_page_fixture_file": str(page_path),
                "extraction_response_file": str(extraction_path),
            }
        ]
    )
    assert [mention["title"] for mention in result.source_mentions] == ["A Book"]
    assert result.source_runs[0]["status"] == "collected"


def test_probe_without_detail_pages_can_accept_list_shape_but_replay_refuses(tmp_path: Path) -> None:
    from autoscout.replay_fixtures import ReplayFixtureError
    from autoscout.source_feasibility import evaluate_source_feasibility

    page = json.loads((FIXTURE_DIR / "page.json").read_text(encoding="utf-8"))
    page.pop("detail_pages")
    source = grounded_detail_source(
        ranking_detail_link_selector=".name",
        ranking_expected_raw_row_count=2,
        ranking_detail_max_pages=2,
        adult_nonfiction_exclusions=["never matches"],
    )
    evidence = {
        "collection_mode": "source-candidate-probe",
        "source_url": source["url"],
        "source_page": source["url"],
        "resolved_url": source["url"],
        "head_status": 200,
        "get_status": 200,
        "reference_date": "2026-08-24",
        "html": page["html"],
        "contract_family": "ranking-table",
        "contract_metadata": source,
    }
    verdict = evaluate_source_feasibility(source, evidence)
    assert verdict.accepted
    assert [sample.title for sample in verdict.samples] == ["A Fiction Book", "A History Book"]
    assert [sample.url for sample in verdict.samples] == [
        "https://example.test/books/1",
        "https://example.test/books/2",
    ]
    from autoscout.replay_fixtures import build_replay_fixture_pair

    with pytest.raises(ReplayFixtureError, match="complete detail_pages"):
        build_replay_fixture_pair(
            source["url"],
            "detail-without-pages",
            evidence,
            signal_strength="top ranking",
            candidate_category="adult nonfiction",
            source_name="Detail ranking",
            signal_family="bestseller/ranking",
            contract_family="ranking-table",
            contract_metadata=source,
            language="en",
            helper_metadata={
                "version": 1,
                "source_url": source["url"],
                "language": "en",
                "entries": [
                    {
                        "title": "A Fiction Book",
                        "author": "Fiction Author",
                        "evidence_url": "https://example.test/books/1",
                        "english_title": "A Fiction Book",
                        "search_translation": "Fiction book",
                        "author_transliteration_note": "Fiction Author",
                    },
                    {
                        "title": "A History Book",
                        "author": "History Author",
                        "evidence_url": "https://example.test/books/2",
                        "english_title": "A History Book",
                        "search_translation": "History book",
                        "author_transliteration_note": "History Author",
                    },
                ],
            },
        )


def test_committed_mixed_category_fixture_replays_only_the_allowlisted_row(tmp_path: Path) -> None:
    page_fixture = json.loads((FIXTURE_DIR / "page.json").read_text(encoding="utf-8"))
    extraction_fixture = json.loads((FIXTURE_DIR / "extraction.json").read_text(encoding="utf-8"))
    source = grounded_detail_source(
        name="Committed mixed detail ranking",
        ranking_detail_link_selector=".name",
        ranking_expected_raw_row_count=2,
        ranking_detail_max_pages=2,
        adult_nonfiction_exclusions=[],
        source_page_fixture_file=str(FIXTURE_DIR / "page.json"),
        extraction_response_file=str(FIXTURE_DIR / "extraction.json"),
        extraction_method="codex-extraction-replay",
    )
    assert page_fixture["detail_pages"][1]["detail_url"] == "https://example.test/books/2"
    assert extraction_fixture["mentions"][0]["candidate_category"] == "adult nonfiction"
    retained = {
        **page_fixture,
        "collection_mode": "source-candidate-probe",
        "source_url": source["url"],
        "head_status": 200,
        "get_status": 200,
        "contract_family": "ranking-table",
        "contract_metadata": source,
    }
    from autoscout.replay_fixtures import build_replay_fixture_pair

    pair = build_replay_fixture_pair(
        source["url"],
        "committed-mixed-detail",
        retained,
        signal_strength="top ranking",
        candidate_category="adult nonfiction",
        source_name="Committed mixed detail ranking",
        signal_family="bestseller/ranking",
        contract_family="ranking-table",
        contract_metadata=source,
        language="es",
        helper_metadata={
            "version": 1,
            "source_url": source["url"],
            "language": "es",
            "entries": [
                {
                    "title": "A History Book",
                    "author": "History Author",
                    "evidence_url": "https://example.test/books/2",
                    "english_title": "History Book",
                    "search_translation": "Историческая книга",
                    "author_transliteration_note": "History Author",
                }
            ],
        },
    )
    assert [entry["title"] for entry in pair.extraction["mentions"]] == ["A History Book"]
    assert pair.extraction["mentions"][0]["isbn"] == "9780140328721"
    assert pair.extraction["mentions"][0]["candidate_category"] == "adult nonfiction"
    assert "category" not in pair.extraction["mentions"][0]
    from autoscout.source_collectors.base import extraction_contract_violation

    validation_source = {**source, "language": "es"}
    assert (
        extraction_contract_violation(
            pair.extraction["mentions"][0],
            pair.extraction["mentions"][0],
            validation_source,
            pair.page,
        )
        == ""
    )
    assert len(pair.page["detail_pages"]) == 2
    result = SourceCollection(tmp_path / "config.json", tmp_path / "runs", "2026-08-24").collect([source])
    assert result.source_runs[0]["status"] == "collected"
    assert [mention["title"] for mention in result.source_mentions] == ["A History Book"]
    assert result.source_mentions[0]["isbn"] == "9780140328721"
    assert result.source_mentions[0]["candidate_category"] == "adult nonfiction"
    assert "category" not in result.source_mentions[0]
    assert result.source_mentions[0]["source_url"] == source["url"]
    assert result.source_mentions[0]["evidence_url"] == "https://example.test/books/2"


def _worklist_detail_source(page_fixture: Path) -> dict[str, Any]:
    return grounded_detail_source(
        name="Worklist detail ranking",
        ranking_detail_link_selector=".name",
        ranking_expected_raw_row_count=2,
        ranking_detail_max_pages=2,
        adult_nonfiction_exclusions=[],
        source_page_fixture_file=str(page_fixture),
        extraction_method="codex-extraction-replay",
    )


def test_codex_worklist_validates_committed_detail_pages_before_capture(tmp_path: Path) -> None:
    source = _worklist_detail_source(FIXTURE_DIR / "page.json")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"sources": [source]}), encoding="utf-8")
    output_dir = tmp_path / "worklist"
    worklist = prepare_codex_extraction_worklist(config_path, output_dir, "2026-08-24")
    assert worklist.failed_source_count == 0
    assert len(worklist.work_items) == 1
    capture = json.loads((output_dir / worklist.work_items[0]["capture_file"]).read_text(encoding="utf-8"))
    assert len(capture["detail_pages"]) == 2
    assert [page["source_page"] for page in capture["detail_pages"]] == [
        "https://example.test/books/1",
        "https://example.test/books/2",
    ]


def test_codex_worklist_bounds_written_detail_capture_to_declared_scope(tmp_path: Path) -> None:
    from autoscout.http_decoding import MAX_RESPONSE_BYTES

    page = json.loads((FIXTURE_DIR / "page.json").read_text(encoding="utf-8"))
    outside_scope = "outside scope boilerplate " * 23_000
    for detail in page["detail_pages"]:
        detail["html"] = (
            f'<aside>{outside_scope}</aside><main class="product-detail">'
            f'<span class="isbn">ISBN: {"9780306406157" if detail["detail_url"].endswith("/1") else "9780140328721"}</span>'
            f'<span class="category">Category: {"Adult fiction" if detail["detail_url"].endswith("/1") else "Historia de América"}</span>'
            "</main>"
        )
    page_path = tmp_path / "large-detail-page.json"
    page_path.write_text(json.dumps(page), encoding="utf-8")
    source = _worklist_detail_source(page_path)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"sources": [source]}), encoding="utf-8")
    output_dir = tmp_path / "worklist"

    worklist = prepare_codex_extraction_worklist(config_path, output_dir, "2026-08-24")

    assert worklist.failed_source_count == 0
    capture_path = output_dir / worklist.work_items[0]["capture_file"]
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    assert capture_path.stat().st_size <= MAX_RESPONSE_BYTES
    assert [(page["source_page"], page["detail_url"], page["resolved_url"]) for page in capture["detail_pages"]] == [
        ("https://example.test/books/1", "https://example.test/books/1", "https://example.test/books/1"),
        ("https://example.test/books/2", "https://example.test/books/2", "https://example.test/books/2"),
    ]
    assert all('<main class="product-detail">' in page["html"] for page in capture["detail_pages"])
    assert all("outside scope boilerplate" not in page["html"] for page in capture["detail_pages"])


def test_codex_worklist_allows_aggregate_detail_capture_within_declared_limit(tmp_path: Path) -> None:
    from autoscout.http_decoding import MAX_RESPONSE_BYTES

    page = json.loads((FIXTURE_DIR / "page.json").read_text(encoding="utf-8"))
    retained_scope = "valid bounded scope evidence " * 23_000
    for detail in page["detail_pages"]:
        detail["html"] = f'<main class="product-detail">{retained_scope}</main>'
    page_path = tmp_path / "large-detail-page.json"
    page_path.write_text(json.dumps(page), encoding="utf-8")
    source = _worklist_detail_source(page_path)
    source["max_response_bytes"] = 1_500_000
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"sources": [source]}), encoding="utf-8")
    output_dir = tmp_path / "worklist"

    worklist = prepare_codex_extraction_worklist(config_path, output_dir, "2026-08-24")

    assert worklist.failed_source_count == 0
    assert len(worklist.work_items) == 1
    capture_path = output_dir / worklist.work_items[0]["capture_file"]
    capture_bytes = capture_path.read_bytes()
    assert len(capture_bytes) > MAX_RESPONSE_BYTES
    assert len(capture_bytes) <= math.ceil(1_500_000 * CAPTURE_SERIALIZATION_HEADROOM_FACTOR)
    capture = json.loads(capture_bytes)
    assert len(capture["detail_pages"]) == 2
    assert all('<main class="product-detail">' in page["html"] for page in capture["detail_pages"])


def test_codex_worklist_allows_default_capture_when_serialization_exceeds_rows_bound(tmp_path: Path) -> None:
    default_capture_limit = 750_000
    page = json.loads((FIXTURE_DIR / "page.json").read_text(encoding="utf-8"))
    page["html"] += "x" * (default_capture_limit - 100 - len(page["html"].encode("utf-8")))
    page_path = tmp_path / "near-default-limit-page.json"
    page_path.write_text(json.dumps(page), encoding="utf-8")
    source = _worklist_detail_source(page_path)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"sources": [source]}), encoding="utf-8")
    output_dir = tmp_path / "worklist"

    assert "max_response_bytes" not in source
    worklist = prepare_codex_extraction_worklist(config_path, output_dir, "2026-08-24")

    assert worklist.failed_source_count == 0
    capture_path = output_dir / worklist.work_items[0]["capture_file"]
    capture_bytes = capture_path.read_bytes()
    capture = json.loads(capture_bytes)
    retained_rows_bytes = len(capture["html"].encode("utf-8"))
    assert retained_rows_bytes == default_capture_limit - 100
    assert retained_rows_bytes < default_capture_limit
    assert len(capture_bytes) > default_capture_limit
    assert len(capture_bytes) <= math.ceil(default_capture_limit * CAPTURE_SERIALIZATION_HEADROOM_FACTOR)


def test_codex_worklist_rejects_oversized_aggregate_detail_capture(tmp_path: Path) -> None:
    declared_limit = 1_200_000
    aggregate_limit = math.ceil(declared_limit * CAPTURE_SERIALIZATION_HEADROOM_FACTOR)

    page = json.loads((FIXTURE_DIR / "page.json").read_text(encoding="utf-8"))
    retained_scope = "valid bounded scope evidence " * 34_000
    for detail in page["detail_pages"]:
        detail["html"] = f'<main class="product-detail">{retained_scope}</main>'
    page_path = tmp_path / "oversized-scope-page.json"
    page_path.write_text(json.dumps(page), encoding="utf-8")
    source = _worklist_detail_source(page_path)
    source["max_response_bytes"] = declared_limit
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"sources": [source]}), encoding="utf-8")
    output_dir = tmp_path / "worklist"

    worklist = prepare_codex_extraction_worklist(config_path, output_dir, "2026-08-24")

    assert worklist.failed_source_count == 1
    assert worklist.work_items == []
    run = worklist.source_runs[0]
    assert run["status"] == "failed"
    assert run["debug_metadata"]["safe_failure_status"] == "ranking_capture_bound"
    assert f"{aggregate_limit}-byte" in run["reason"]
    assert not list(output_dir.glob("*codex-capture*.json"))


def test_codex_worklist_rejects_malformed_replay_detail_evidence_before_capture(tmp_path: Path) -> None:
    page = json.loads((FIXTURE_DIR / "page.json").read_text(encoding="utf-8"))
    page["detail_pages"][1]["source_page"] = "https://example.test/ranking"
    page_path = tmp_path / "malformed-page.json"
    page_path.write_text(json.dumps(page), encoding="utf-8")
    source = _worklist_detail_source(page_path)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"sources": [source]}), encoding="utf-8")
    output_dir = tmp_path / "worklist"
    worklist = prepare_codex_extraction_worklist(config_path, output_dir, "2026-08-24")
    assert worklist.failed_source_count == 1
    assert worklist.work_items == []
    assert not list(output_dir.glob("*codex-capture*.json"))
    assert "source_page" in worklist.source_runs[0]["reason"]


@pytest.mark.parametrize(
    "detail_pages, expected",
    [([], "count"), (detail_page(detail_url="https://example.test/books/2"), "detail_url")],
)
def test_codex_replay_rejects_missing_or_mismatched_detail_evidence(
    tmp_path: Any, detail_pages: list[dict[str, str]], expected: str
) -> None:
    import json

    source = grounded_detail_source(name="Replay detail ranking")
    page_path = tmp_path / "page.json"
    page_path.write_text(
        json.dumps(
            {
                "source_page": source["url"],
                "resolved_url": source["url"],
                "reference_date": "2026-08-24",
                "html": list_html(),
                "detail_pages": detail_pages,
            }
        ),
        encoding="utf-8",
    )
    extraction_path = tmp_path / "extraction.json"
    extraction_path.write_text(json.dumps({"mentions": [ranking_entry()]}), encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    result = SourceCollection(config_path, tmp_path / "runs", "2026-08-24").collect(
        [
            {
                **source,
                "extraction_method": "codex-extraction-replay",
                "source_page_fixture_file": str(page_path),
                "extraction_response_file": str(extraction_path),
            }
        ]
    )
    assert result.failed_source_count == 1
    assert expected in result.source_runs[0]["reason"]


class _FakeResponse:
    def __init__(self, url: str, body: str) -> None:
        self._url = url
        self._body = body.encode("utf-8")

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self._body

    def geturl(self) -> str:
        return self._url

    def getheader(self, _name: str, _default: str = "") -> str:
        return ""


def two_row_list_html() -> str:
    return list_html().replace(
        "</p>",
        '<div class="row"><span class="rank">2</span><span class="title">Activity book</span>'
        '<span class="author">Another Author</span><a class="detail-link" href="/books/2">Details</a></div></p>',
    )


@pytest.mark.parametrize(
    "bound_field, bound_value, expected",
    [
        ("ranking_expected_raw_row_count", 2, "expected exactly 2"),
        ("ranking_min_raw_row_count", 2, "expected at least 2"),
        ("ranking_max_raw_row_count", 1, "expected at most 1"),
    ],
)
def test_declared_raw_row_bounds_fail_closed_before_detail_requests(
    bound_field: str, bound_value: int, expected: str
) -> None:
    source = grounded_detail_source()
    if bound_field != "ranking_expected_raw_row_count":
        source.pop("ranking_expected_raw_row_count", None)
    source[bound_field] = bound_value
    markup = two_row_list_html() if bound_field == "ranking_max_raw_row_count" else list_html()
    error = validate_page_shape(
        source,
        {
            "source_page": source["url"],
            "resolved_url": source["url"],
            "html": markup,
            # The row bound is what this case is about, so the capture carries
            # the reference date every other case in this file supplies. Without
            # one the freshness refusal fires first, which is correct but is a
            # different test (#599).
            "reference_date": "2026-08-24",
        },
    )
    assert expected in error
    rows, plan_error = ranking_detail_fetch_plan(source, markup)
    assert rows == []
    assert expected in plan_error


def test_duplicate_detail_urls_fail_before_network() -> None:
    source = grounded_detail_source(ranking_expected_raw_row_count=2, ranking_detail_max_pages=2)
    duplicate = list_html().replace('/books/1">Details</a>', '/books/1">Details</a>')
    duplicate = duplicate.replace(
        "</p>",
        '<div class="row"><span class="rank">2</span><span class="title">B</span>'
        '<span class="author">C</span><a class="detail-link" href="/books/1">Details</a></div></p>',
    )
    rows, error = ranking_detail_fetch_plan(source, duplicate)
    assert rows == []
    assert "duplicate detail URL" in error


def test_live_detail_fetch_uses_detail_category_without_title_filtering(monkeypatch: pytest.MonkeyPatch) -> None:
    import autoscout.source_collectors as collectors
    from autoscout.source_collectors import fetch_agent_source_page

    source = grounded_detail_source(
        ranking_expected_raw_row_count=2, ranking_detail_max_pages=2, adult_nonfiction_exclusions=[]
    )
    calls: list[str] = []

    def opener(request: Any, timeout: float) -> _FakeResponse:
        url = request.full_url
        calls.append(url)
        if url.endswith("/ranking"):
            return _FakeResponse(url, two_row_list_html())
        return _FakeResponse(url, detail_page()[0]["html"])

    monkeypatch.setattr(collectors, "urlopen", opener)
    page, _debug = fetch_agent_source_page(
        tmp_path := __import__("pathlib").Path("."),
        tmp_path,
        source,
        reference_date=__import__("datetime").date(2026, 8, 24),
    )
    assert calls == [
        "https://example.test/ranking",
        "https://example.test/books/1",
        "https://example.test/books/2",
    ]
    assert len(page["detail_pages"]) == 2
    assert all(item["source_page"] == item["detail_url"] for item in page["detail_pages"])


def test_live_detail_fetch_enforces_cap_before_any_detail_request(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import date
    from pathlib import Path

    import autoscout.source_collectors as collectors
    from autoscout.source_collectors import CollectorRunError
    from autoscout.source_collectors import fetch_agent_source_page

    source = grounded_detail_source(
        ranking_expected_raw_row_count=2,
        ranking_detail_max_pages=1,
        adult_nonfiction_exclusions=["never matches"],
    )
    calls: list[str] = []

    def opener(request: Any, timeout: float) -> _FakeResponse:
        calls.append(request.full_url)
        return _FakeResponse(request.full_url, two_row_list_html())

    monkeypatch.setattr(collectors, "urlopen", opener)
    with pytest.raises(CollectorRunError, match="max_pages"):
        fetch_agent_source_page(Path("."), Path("."), source, reference_date=date(2026, 8, 24))
    assert calls == ["https://example.test/ranking"]


def test_model_input_accounts_for_bounded_detail_pages() -> None:
    from autoscout.source_collectors.agent_model_input import model_bounded_page_data

    page, debug = model_bounded_page_data(
        {
            "html": "<div>list</div>",
            "detail_pages": [
                {
                    "source_page": "https://example.test/ranking",
                    "detail_url": "https://example.test/books/1",
                    "resolved_url": "https://example.test/books/1",
                    "html": "x" * 100,
                },
                {
                    "source_page": "https://example.test/ranking",
                    "detail_url": "https://example.test/books/2",
                    "resolved_url": "https://example.test/books/2",
                    "html": "y" * 100,
                },
            ],
        },
        80,
    )
    assert debug["raw_model_detail_page_count"] == 2
    assert debug["model_detail_pages_omitted"] > 0
    assert page["detail_pages_complete"] is False


def test_detail_model_input_compacts_rows_and_scopes_under_one_global_bound() -> None:
    from autoscout.source_collectors.agent_model_input import model_bounded_page_data

    page_data = json.loads((FIXTURE_DIR / "page.json").read_text(encoding="utf-8"))
    source = grounded_detail_source(
        ranking_detail_link_selector=".name",
        ranking_expected_raw_row_count=2,
        ranking_detail_max_pages=2,
        adult_nonfiction_exclusions=["never matches"],
    )
    page_data["html"] += "<div>" + ("retailer chrome " * 20_000) + "</div>"
    bounded, debug = model_bounded_page_data(page_data, 2_000, source=source)
    model_text = bounded["html"] + json.dumps(bounded["detail_pages"], ensure_ascii=False)
    assert len(bounded["html"]) + sum(len(page["html"]) for page in bounded["detail_pages"]) <= 2_000
    assert "A Fiction Book" in bounded["html"]
    assert "A History Book" in bounded["html"]
    assert all("product-header" in page["html"] for page in bounded["detail_pages"])
    assert all("product-detail" in page["html"] for page in bounded["detail_pages"])
    assert debug["model_detail_pages_omitted"] == 0
    assert bounded["detail_pages_complete"] is True
    assert "retailer chrome" not in model_text


def test_detail_extraction_contract_forbids_browsing_and_declares_url_semantics() -> None:
    from autoscout.source_collectors.contract import extraction_contract

    contract = extraction_contract(grounded_detail_source(), source_page="https://example.test/ranking")
    rules = " ".join(contract["rules"])
    assert "detail_pages" in rules
    assert "do not browse" in rules.casefold()
    assert contract["required_output_shape"]["mentions"][0]["source_url"] == "The configured ranking page URL"
    assert contract["required_output_shape"]["mentions"][0]["evidence_url"] == "Exact linked detail-page URL"


def test_replay_fixture_builder_preserves_detail_pages() -> None:
    from autoscout.replay_fixtures import build_replay_fixture_pair

    source = grounded_detail_source(
        ranking_expected_raw_row_count=1,
        ranking_detail_max_pages=1,
    )
    evidence = {
        "collection_mode": "source-candidate-probe",
        "source_url": source["url"],
        "source_page": source["url"],
        "resolved_url": source["url"],
        "head_status": 200,
        "get_status": 200,
        "reference_date": "2026-08-24",
        "html": list_html(),
        "detected_heading": source["expected_page_heading"],
        "contract_family": "ranking-table",
        "contract_metadata": source,
        "detail_pages": detail_page(),
    }
    helper = {
        "version": 1,
        "source_url": source["url"],
        "language": "en",
        "entries": [
            {
                "title": "A Book",
                "author": "An Author",
                "evidence_url": "https://example.test/books/1",
                "english_title": "A Book",
                "search_translation": "Книга",
                "author_transliteration_note": "Ан Автор",
            }
        ],
    }
    pair = build_replay_fixture_pair(
        source["url"],
        "detail-ranking",
        evidence,
        signal_strength="top ranking",
        candidate_category="Adult nonfiction",
        source_name="Detail ranking",
        signal_family="bestseller/ranking",
        contract_family="ranking-table",
        contract_metadata=source,
        language="en",
        helper_metadata=helper,
    )
    assert len(pair.page["detail_pages"]) == 1
    assert pair.extraction["mentions"][0]["evidence_url"] == "https://example.test/books/1"
    assert pair.extraction["mentions"][0]["isbn"] == "9780306406157"
    assert pair.extraction["mentions"][0]["candidate_category"] == "Adult nonfiction"
    assert "category" not in pair.extraction["mentions"][0]
    assert pair.page["detail_pages"][0]["source_page"] == "https://example.test/books/1"
