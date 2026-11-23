"""Issue #668: validated books from incomplete Cúspide rankings.

Product-rule change (editor-authorized in #668): Cúspide declares
``ranking_accept_incomplete: true`` alongside its exact-100 bound, so a
short but non-empty capture is collected as *incomplete* — valid rows flow
through the normal scouting pipeline while the source is visibly reported
with its actual coverage (observed count plus missing/duplicate-rank
diagnostics) instead of being rejected wholesale.

Sources without the flag keep the fail-closed exact gate (see
test_issue_665_cuspide.py, which is intentionally left unchanged).
Over-count captures and empty captures still fail everywhere.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from autoscout.source_check import check_sources
from autoscout.source_page_identity import incomplete_ranking_debug
from autoscout.source_page_identity import incomplete_ranking_debug_for_page
from autoscout.source_page_identity import ranking_detail_fetch_plan
from autoscout.source_page_identity import ranking_incomplete_row_count
from autoscout.source_page_identity import ranking_incomplete_warning
from autoscout.source_page_identity import ranking_raw_row_count_error
from autoscout.source_page_identity import ranking_raw_row_diagnostics
from autoscout.source_page_identity import validate_page_shape
from autoscout.source_page_identity import validate_ranking_contract_metadata
from autoscout.source_page_identity import validate_ranking_detail_pages

SOURCE_URL = "https://cuspide.com/100-mas-vendidos/"
SOURCE_NAME = "Cúspide 100 más vendidos"


def _cuspide_source(**overrides: Any) -> dict[str, object]:
    source: dict[str, object] = {
        "contract_family": "ranking-table",
        "metadata_ranking_table": True,
        "url": SOURCE_URL,
        "ranking_row_selector": ".category-page-row > .col",
        "ranking_rank_source": "selector",
        "ranking_rank_selector": ".livriz-ranking",
        "ranking_title_selector": ".name",
        "ranking_author_selector": ".author-product-loop",
        "ranking_author_optional": True,
        "ranking_category_marker": "Los 100 más vendidos",
        "ranking_reporting_period_undated": True,
        "ranking_expected_raw_row_count": 100,
        "ranking_evidence_url": "detail_page",
        "eligible_candidate_category": "adult nonfiction",
        "ranking_detail_link_selector": ".name",
        "ranking_detail_url_pattern": r"https://cuspide\.com/producto/[^?#]+/?",
        "ranking_detail_scope_selector": ".product_meta",
        "ranking_detail_max_pages": 100,
        "ranking_detail_fields": ["isbn", "category"],
        "ranking_detail_identifier_selector": ".sku",
        "ranking_detail_identifier_pattern": "(?P<identifier>97[89][0-9]{10})",
        "ranking_detail_category_selector": ".posted_in",
        "ranking_detail_category_pattern": "Categoría:\\s*(?P<category>[^<]+)",
        "ranking_detail_category_allowlist": [
            "Autoayuda, pensamiento y prácticas",
            "Historia de América",
            "Ciencias políticas y teoría política",
        ],
    }
    source.update(overrides)
    return source


def _ranking_html(*, row_count: int) -> str:
    rows = []
    for index in range(1, row_count + 1):
        rows.append(
            '<div class="category-page-row"><div class="col">'
            f'<p class="livriz-ranking">#{index} ranking en venta</p>'
            f'<h3 class="name"><a href="https://cuspide.com/producto/book-{index}/">Book {index}</a></h3>'
            f'<p class="author-product-loop">Author {index}</p>'
            "</div></div>"
        )
    return "<h1>Los 100 más vendidos de la semana</h1>" + "".join(rows)


def _detail_pages(row_count: int) -> list[dict[str, str]]:
    return [
        {
            "source_page": f"https://cuspide.com/producto/book-{index}/",
            "detail_url": f"https://cuspide.com/producto/book-{index}/",
            "resolved_url": f"https://cuspide.com/producto/book-{index}/",
            "html": '<div class="product_meta"><span class="sku">9780306406157</span></div>',
        }
        for index in range(1, row_count + 1)
    ]


# --- Gate behavior -----------------------------------------------------------


def test_short_93_row_capture_is_accepted_as_incomplete_with_the_flag() -> None:
    source = _cuspide_source(ranking_accept_incomplete=True)
    html = _ranking_html(row_count=93)

    assert ranking_raw_row_count_error(source, 93) == ""
    assert ranking_incomplete_row_count(source, 93) is True
    assert ranking_incomplete_warning(source, 93) == (
        "incomplete ranking: exposes 93 of 100 expected raw rows; valid rows accepted with partial coverage"
    )
    page_data = {"source_page": SOURCE_URL, "resolved_url": SOURCE_URL, "html": html}
    assert validate_page_shape(dict(source), page_data) == ""


def test_short_93_row_capture_still_fails_closed_without_the_flag() -> None:
    source = _cuspide_source()
    assert ranking_raw_row_count_error(source, 93) == ("ranking page exposes 93 raw rows; expected exactly 100")
    assert ranking_incomplete_row_count(source, 93) is False
    assert ranking_incomplete_warning(source, 93) == ""
    page_data = {"source_page": SOURCE_URL, "resolved_url": SOURCE_URL, "html": _ranking_html(row_count=93)}
    assert validate_page_shape(dict(source), page_data) == (
        "page shape validation failed: ranking page exposes 93 raw rows; expected exactly 100"
    )


def test_full_100_row_control_has_no_incomplete_warning() -> None:
    source = _cuspide_source(ranking_accept_incomplete=True)
    html = _ranking_html(row_count=100)
    assert ranking_raw_row_count_error(source, 100) == ""
    assert ranking_incomplete_row_count(source, 100) is False
    assert ranking_incomplete_warning(source, 100) == ""
    page_data = {"source_page": SOURCE_URL, "resolved_url": SOURCE_URL, "html": html}
    assert validate_page_shape(dict(source), page_data) == ""
    assert incomplete_ranking_debug(html, source, 100) == {}
    assert incomplete_ranking_debug_for_page(source, page_data) == {}


def test_over_count_capture_still_fails_with_the_flag() -> None:
    source = _cuspide_source(ranking_accept_incomplete=True)
    assert ranking_raw_row_count_error(source, 101) == ("ranking page exposes 101 raw rows; expected exactly 100")
    assert ranking_incomplete_row_count(source, 101) is False
    page_data = {"source_page": SOURCE_URL, "resolved_url": SOURCE_URL, "html": _ranking_html(row_count=101)}
    assert "expected exactly 100" in validate_page_shape(dict(source), page_data)


def test_empty_capture_still_fails_with_the_flag() -> None:
    source = _cuspide_source(ranking_accept_incomplete=True)
    assert ranking_incomplete_row_count(source, 0) is False
    page_data = {"source_page": SOURCE_URL, "resolved_url": SOURCE_URL, "html": ""}
    assert validate_page_shape(dict(source), page_data) != ""


def test_wrong_page_still_fails_with_the_flag() -> None:
    source = _cuspide_source(ranking_accept_incomplete=True)
    html = "<h1>Unrelated bookshop homepage</h1>" + "".join(
        '<div class="category-page-row"><div class="col">'
        f'<p class="livriz-ranking">#{index} ranking en venta</p>'
        f'<h3 class="name"><a href="https://cuspide.com/producto/book-{index}/">Book {index}</a></h3>'
        f'<p class="author-product-loop">Author {index}</p>'
        "</div></div>"
        for index in range(1, 94)
    )
    page_data = {"source_page": SOURCE_URL, "resolved_url": SOURCE_URL, "html": html}
    assert validate_page_shape(dict(source), page_data) == (
        "page shape validation failed: ranking category or reporting period is not current"
    )


# --- Incomplete debug metadata -------------------------------------------------


def test_incomplete_debug_carries_coverage_and_missing_rank_diagnostics() -> None:
    source = _cuspide_source(ranking_accept_incomplete=True)
    html = _ranking_html(row_count=93)
    debug = incomplete_ranking_debug(html, source, 93)
    assert debug["incomplete_ranking"] is True
    assert debug["expected_raw_row_count"] == 100
    assert debug["raw_row_count"] == 93
    assert debug["incomplete_ranking_warning"] == ranking_incomplete_warning(source, 93)
    assert debug["missing_ranks"] == [str(rank) for rank in range(94, 101)]
    assert debug["duplicate_ranks"] is False

    page_data = {"source_page": SOURCE_URL, "resolved_url": SOURCE_URL, "html": html}
    assert incomplete_ranking_debug_for_page(source, page_data) == debug

    diagnostics = ranking_raw_row_diagnostics(html, source)
    assert diagnostics["raw_row_count"] == 93
    assert diagnostics["missing_ranks"] == [str(rank) for rank in range(94, 101)]


# --- Detail-route gates stay intact ----------------------------------------------


def test_detail_fetch_plan_admits_93_valid_rows_with_the_flag() -> None:
    from autoscout.ranking_detail_route import RankingDetailRoute

    source = _cuspide_source(ranking_accept_incomplete=True)
    route = RankingDetailRoute.from_source(source)
    rows, plan_error = ranking_detail_fetch_plan(source, _ranking_html(row_count=93), route=route)
    assert plan_error == ""
    assert len(rows) == 93


def test_detail_fetch_plan_rejects_93_rows_without_the_flag() -> None:
    from autoscout.ranking_detail_route import RankingDetailRoute

    source = _cuspide_source()
    route = RankingDetailRoute.from_source(source)
    rows, plan_error = ranking_detail_fetch_plan(source, _ranking_html(row_count=93), route=route)
    assert rows == []
    assert "expected exactly 100" in plan_error


def test_detail_evidence_bound_accepts_93_matching_pages_with_the_flag() -> None:
    from autoscout.ranking_detail_route import RankingDetailRoute

    source = _cuspide_source(ranking_accept_incomplete=True)
    route = RankingDetailRoute.from_source(source)
    page_data: dict[str, Any] = {
        "source_page": SOURCE_URL,
        "resolved_url": SOURCE_URL,
        "html": _ranking_html(row_count=93),
        "detail_pages": _detail_pages(93),
    }
    assert validate_ranking_detail_pages(source, page_data, route=route) == ""


def test_detail_evidence_mismatch_still_fails_with_the_flag() -> None:
    from autoscout.ranking_detail_route import RankingDetailRoute

    source = _cuspide_source(ranking_accept_incomplete=True)
    route = RankingDetailRoute.from_source(source)
    page_data: dict[str, Any] = {
        "source_page": SOURCE_URL,
        "resolved_url": SOURCE_URL,
        "html": _ranking_html(row_count=93),
        "detail_pages": _detail_pages(92),
    }
    assert "does not match surviving ranking rows" in validate_ranking_detail_pages(source, page_data, route=route)


def test_malformed_row_still_fails_with_the_flag() -> None:
    from autoscout.ranking_detail_route import RankingDetailRoute

    source = _cuspide_source(ranking_accept_incomplete=True)
    route = RankingDetailRoute.from_source(source)
    rows_html = "".join(
        '<div class="category-page-row"><div class="col">'
        f'<p class="livriz-ranking">#{index} ranking en venta</p>'
        f'<h3 class="name"><a href="https://cuspide.com/producto/book-{index}/">Book {index}</a></h3>'
        f'<p class="author-product-loop">Author {index}</p>'
        "</div></div>"
        for index in range(1, 93)
    )
    broken_row = (
        '<div class="category-page-row"><div class="col">'
        '<p class="livriz-ranking">#93 ranking en venta</p>'
        '<h3 class="name"><a href="https://cuspide.com/producto/book-93/"></a></h3>'
        '<p class="author-product-loop">Author 93</p>'
        "</div></div>"
    )
    html = "<h1>Los 100 más vendidos de la semana</h1>" + rows_html + broken_row
    rows, plan_error = ranking_detail_fetch_plan(source, html, route=route)
    assert rows == []
    assert "ranking row missing title" in plan_error


# --- Declaration validation ------------------------------------------------------


def test_flag_declaration_requires_the_exact_bound_alone() -> None:
    assert validate_ranking_contract_metadata(_cuspide_source(ranking_accept_incomplete=True)) == ""
    assert validate_ranking_contract_metadata(_cuspide_source()) == ""

    range_source = _cuspide_source(ranking_accept_incomplete=True)
    del range_source["ranking_expected_raw_row_count"]
    range_source["ranking_min_raw_row_count"] = 25
    range_source["ranking_max_raw_row_count"] = 30
    assert validate_ranking_contract_metadata(range_source) == (
        "ranking_accept_incomplete requires ranking_expected_raw_row_count alone"
    )

    assert (
        validate_ranking_contract_metadata(_cuspide_source(ranking_accept_incomplete="yes"))
        == "ranking_accept_incomplete must be true or omitted"
    )


# --- Collection: valid rows reach scouting -----------------------------------------
#
# A compact source_page-evidence ranking contract stands in for the Cúspide
# shape here so the replay needs no detail pages; the Cúspide-shaped unit
# tests above already prove the flag gates the exact-100 rule itself.


def _collection_source(tmp_path: Path, *, row_count: int, flag: bool) -> dict[str, Any]:
    rows_html = "".join(
        '<div class="ranked-row">'
        f'<div class="row-title">BOOK {index}</div>'
        f'<div class="row-author">AUTHOR {index}</div>'
        f'<span class="row-isbn" hidden>9780306406157</span>'
        "</div>"
        for index in range(1, row_count + 1)
    )
    html = (
        "<!doctype html><html><body>"
        '<div class="page-heading">Los 100 más vendidos de la semana</div>'
        f"{rows_html}</body></html>"
    )
    page_path = tmp_path / f"page-{row_count}.json"
    page_path.write_text(
        json.dumps(
            {
                "source_page": SOURCE_URL,
                "resolved_url": SOURCE_URL,
                "reference_date": "2026-08-24",
                "html": html,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    extraction_path = tmp_path / f"extraction-{row_count}.json"
    extraction_path.write_text(
        json.dumps(
            {
                "source_page": SOURCE_URL,
                "mentions": [
                    {
                        "title": "BOOK 11",
                        "primary_authors": ["AUTHOR 11"],
                        "isbn": "9780306406157",
                        "current_rank": "11",
                        "source_week": "2026-08-24",
                        "source_url": SOURCE_URL,
                        "evidence_url": SOURCE_URL,
                        "signal_type": "bestseller/ranking",
                        "signal_strength": "top ranking",
                        "candidate_category": "adult nonfiction",
                        "evidence_note": "Rank #11 in the incomplete Cúspide ranking.",
                        "english_title": "Book Eleven",
                        "search_translation": "Книга одиннадцать",
                        "author_transliteration_note": "Автор одиннадцать",
                    },
                    # A genuinely new eligible book at the tail of the short ranking.
                    {
                        "title": "BOOK 93",
                        "primary_authors": ["AUTHOR 93"],
                        "isbn": "9780306406157",
                        "current_rank": "93",
                        "source_week": "2026-08-24",
                        "source_url": SOURCE_URL,
                        "evidence_url": SOURCE_URL,
                        "signal_type": "bestseller/ranking",
                        "signal_strength": "top ranking",
                        "candidate_category": "adult nonfiction",
                        "evidence_note": "Rank #93 in the incomplete Cúspide ranking.",
                        "english_title": "Book Ninety-Three",
                        "search_translation": "Книга девяносто три",
                        "author_transliteration_note": "Автор девяносто три",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    source: dict[str, Any] = {
        "name": SOURCE_NAME,
        "family": "bestseller/ranking",
        "url": SOURCE_URL,
        "active": True,
        "access_status": "open",
        "check_frequency": "weekly",
        "language": "es",
        "contract_family": "ranking-table",
        "metadata_ranking_table": True,
        "ranking_row_selector": ".ranked-row",
        "ranking_rank_source": "document_order",
        "ranking_title_selector": ".row-title",
        "ranking_author_selector": ".row-author",
        "ranking_identifier_selector": ".row-isbn",
        "ranking_category_marker": "más vendidos",
        "ranking_reporting_period_undated": True,
        "ranking_expected_raw_row_count": 100,
        "ranking_evidence_url": "source_page",
        "adult_nonfiction_exclusions": ["libro de actividades"],
        "visible_title_selector": ".page-heading",
        "expected_page_heading": "Los 100 más vendidos de la semana",
        "expected_signal_strength": "top ranking",
        "eligible_candidate_category": "adult nonfiction",
        "extraction_method": "codex-extraction-replay",
        "source_page_fixture_file": str(page_path),
        "extraction_response_file": str(extraction_path),
    }
    if flag:
        source["ranking_accept_incomplete"] = True
    return source


def test_93_row_replay_collects_new_book_with_incomplete_warning(tmp_path: Path) -> None:
    from autoscout.ranking_freshness import RankingFreshnessLedger
    from autoscout.source_collection import SourceCollection

    source = _collection_source(tmp_path, row_count=93, flag=True)
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-08-24", RankingFreshnessLedger()).collect(
        [source]
    )
    assert result.failed_source_count == 0
    run = result.source_runs[0]
    assert run["status"] == "collected"
    assert "incomplete ranking" in run["reason"]
    assert "93" in run["reason"] and "100" in run["reason"]
    debug = run["debug_metadata"]
    assert debug["incomplete_ranking"] is True
    assert debug["expected_raw_row_count"] == 100
    assert debug["raw_row_count"] == 93
    assert debug["missing_ranks"] == [str(rank) for rank in range(94, 101)]
    titles = {mention["title"] for mention in result.source_mentions}
    assert {"BOOK 11", "BOOK 93"} <= titles


def test_93_row_replay_fails_closed_without_the_flag(tmp_path: Path) -> None:
    from autoscout.ranking_freshness import RankingFreshnessLedger
    from autoscout.source_collection import SourceCollection

    source = _collection_source(tmp_path, row_count=93, flag=False)
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-08-24", RankingFreshnessLedger()).collect(
        [source]
    )
    assert result.failed_source_count == 1
    assert result.source_runs[0]["status"] == "failed"
    assert "expected exactly 100" in result.source_runs[0]["reason"]
    assert result.source_mentions == []


def test_100_row_control_collects_cleanly_with_the_flag(tmp_path: Path) -> None:
    from autoscout.ranking_freshness import RankingFreshnessLedger
    from autoscout.source_collection import SourceCollection

    source = _collection_source(tmp_path, row_count=100, flag=True)
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-08-24", RankingFreshnessLedger()).collect(
        [source]
    )
    assert result.failed_source_count == 0
    run = result.source_runs[0]
    assert run["status"] == "collected"
    assert run["reason"] == ""
    assert run["debug_metadata"].get("incomplete_ranking") is not True
    assert {mention["title"] for mention in result.source_mentions} == {"BOOK 11", "BOOK 93"}


# --- Source health: accepted incomplete vs failed collection -------------------------


def _health_config(tmp_path: Path, *, row_count: int, flag: bool) -> Path:
    fixture_path = tmp_path / f"cuspide-{row_count}-page.json"
    fixture_path.write_text(
        json.dumps(
            {
                "source_page": SOURCE_URL,
                "resolved_url": SOURCE_URL,
                "html": _ranking_html(row_count=row_count),
                "detail_pages": _detail_pages(row_count),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    source: dict[str, Any] = {
        "contract_family": "ranking-table",
        "metadata_ranking_table": True,
        "url": SOURCE_URL,
        "name": SOURCE_NAME,
        "family": "bestseller/ranking",
        "active": True,
        "access_status": "open",
        "check_frequency": "weekly",
        "language": "es",
        "extraction_method": "codex-extraction-replay",
        "source_page_fixture_file": str(fixture_path),
        "ranking_row_selector": ".category-page-row > .col",
        "ranking_rank_source": "selector",
        "ranking_rank_selector": ".livriz-ranking",
        "ranking_title_selector": ".name",
        "ranking_author_selector": ".author-product-loop",
        "ranking_author_optional": True,
        "ranking_category_marker": "Los 100 más vendidos",
        "ranking_reporting_period_undated": True,
        "ranking_expected_raw_row_count": 100,
        "ranking_evidence_url": "detail_page",
        "eligible_candidate_category": "adult nonfiction",
        "ranking_detail_link_selector": ".name",
        "ranking_detail_url_pattern": r"https://cuspide\.com/producto/[^?#]+/?",
        "ranking_detail_scope_selector": ".product_meta",
        "ranking_detail_max_pages": 100,
        "ranking_detail_fields": ["isbn", "category"],
    }
    if flag:
        source["ranking_accept_incomplete"] = True
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"run_date": "2026-09-08", "sources": [source]}, ensure_ascii=False),
        encoding="utf-8",
    )
    return config_path


def test_health_check_accepts_incomplete_93_row_capture_with_success_status(tmp_path: Path, capsys: Any) -> None:
    exit_code = check_sources(_health_config(tmp_path, row_count=93, flag=True), live=False)
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "codex-captured" in out
    assert "incomplete" in out
    assert "rows=93" in out
    assert "discarded" in out


def test_health_check_still_fails_93_row_capture_without_the_flag(tmp_path: Path, capsys: Any) -> None:
    exit_code = check_sources(_health_config(tmp_path, row_count=93, flag=False), live=False)
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "ranking page exposes 93 raw rows; expected exactly 100" in out


def test_health_check_full_100_row_capture_has_no_incomplete_marker(tmp_path: Path, capsys: Any) -> None:
    exit_code = check_sources(_health_config(tmp_path, row_count=100, flag=True), live=False)
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "codex-captured" in out
    assert "incomplete" not in out


# --- Configured portfolio rule ---------------------------------------------------------
#
# The Cúspide portfolio entry opts into incomplete acceptance while keeping
# its exact-100 coverage target. This asserts the declared rule itself so a
# silent config regression cannot reintroduce the all-or-nothing gate.


def test_configured_cuspide_source_accepts_incomplete_below_exact_100() -> None:
    from autoscout.config import load_validated_config

    root = Path(__file__).resolve().parents[1]
    config = load_validated_config(root / "configs/current-weekly-portfolio.json")
    matches = [source for source in config.get("sources", []) if source.get("name") == SOURCE_NAME]
    assert len(matches) == 1
    source = matches[0]
    assert source.get("ranking_expected_raw_row_count") == 100
    assert source.get("ranking_accept_incomplete") is True
    assert validate_ranking_contract_metadata(source) == ""


# --- Weekly reporting: incomplete coverage stays visible -----------------------------------
#
# Storing the warning in source-runs.json alone is insufficient: weekly
# completion and both review formats must name the source and its 93/100
# coverage.


def _incomplete_run(status: str = "collected") -> dict[str, Any]:
    return {
        "source_name": SOURCE_NAME,
        "status": status,
        "reason": "incomplete ranking: exposes 93 of 100 expected raw rows; valid rows accepted",
        "debug_metadata": {
            "incomplete_ranking": True,
            "incomplete_ranking_warning": "incomplete ranking: exposes 93 of 100 expected raw rows",
            "expected_raw_row_count": 100,
            "raw_row_count": 93,
            "missing_ranks": [str(rank) for rank in range(94, 101)],
            "duplicate_ranks": False,
        },
    }


def test_incomplete_coverage_extraction_rejects_failed_runs_and_malformed_records() -> None:
    from autoscout.source_page_identity import incomplete_coverage_from_run

    assert incomplete_coverage_from_run(_incomplete_run()) == {
        "source_name": SOURCE_NAME,
        "observed_rows": 93,
        "expected_rows": 100,
    }
    assert incomplete_coverage_from_run(_incomplete_run(status="codex-captured")) is not None
    failed = _incomplete_run(status="failed")
    assert incomplete_coverage_from_run(failed) is None
    assert incomplete_coverage_from_run({"source_name": SOURCE_NAME, "status": "collected"}) is None
    assert incomplete_coverage_from_run({}) is None


def test_capture_problems_name_incomplete_coverage_beside_failures() -> None:
    from autoscout.weekly_workflow import _source_problems_from_capture

    result = {
        "source_runs": [
            _incomplete_run(status="codex-captured"),
            {"source_name": "Broken Source", "status": "failed", "reason": "boom"},
            {"source_name": "Healthy Source", "status": "collected", "reason": ""},
        ]
    }
    problems = _source_problems_from_capture(result)
    by_name = {problem["source_name"]: problem for problem in problems}
    assert set(by_name) == {SOURCE_NAME, "Broken Source"}
    incomplete = by_name[SOURCE_NAME]
    assert incomplete["status"] == "incomplete"
    assert incomplete["observed_rows"] == "93"
    assert incomplete["expected_rows"] == "100"
    assert by_name["Broken Source"]["status"] == "failed"


def test_completion_message_names_incomplete_coverage_in_russian() -> None:
    from autoscout.russian_workflow import source_problem_message

    problems = [
        {"source_name": "Broken Source", "status": "failed", "reason": "boom"},
        {
            "source_name": SOURCE_NAME,
            "status": "incomplete",
            "reason": "incomplete ranking: exposes 93 of 100 expected raw rows",
            "observed_rows": "93",
            "expected_rows": "100",
        },
    ]
    message = source_problem_message(problems)
    assert "Broken Source" in message
    assert "неполный охват" in message
    assert SOURCE_NAME in message
    assert "93 из 100" in message


def _review_candidate() -> dict[str, Any]:
    return {
        "identity_key": "book-one|alex-example",
        "title": "Book One",
        "primary_authors": ["Alex Example"],
        "update_delta": "new",
        "priority_label": "A",
        "independent_signal_count": 1,
        "russian_edition_status": "not found in Russian",
        "content_description": "Описание книги.",
        "source_evidence": [
            {
                "source_name": "PW Best Books 2025",
                "source_url": "https://example.test/pw",
                "evidence_url": "https://example.test/pw/book-one",
                "evidence_note": "Listed among the year's best.",
            }
        ],
    }


def _write_review_snapshot(tmp_path: Path, runs: list[dict[str, Any]]) -> None:
    from autoscout.json_io import write_json

    write_json(tmp_path / "scouting-candidates.json", [_review_candidate()])
    write_json(
        tmp_path / "run-summary.json",
        {"run_date": "2026-09-08", "editor_facing_count": 1},
    )
    write_json(tmp_path / "source-runs.json", runs)


def test_markdown_review_names_incomplete_coverage(tmp_path: Path) -> None:
    from autoscout.review_rendering import render_review

    _write_review_snapshot(tmp_path, [_incomplete_run()])
    assert render_review(tmp_path) == 0
    markdown = (tmp_path / "weekly-review.md").read_text(encoding="utf-8")
    assert "неполный охват" in markdown
    assert SOURCE_NAME in markdown
    assert "93 из 100" in markdown


def test_markdown_review_without_incomplete_runs_has_no_coverage_warning(tmp_path: Path) -> None:
    from autoscout.review_rendering import render_review

    run = _incomplete_run()
    del run["debug_metadata"]
    run["reason"] = ""
    _write_review_snapshot(tmp_path, [run])
    assert render_review(tmp_path) == 0
    markdown = (tmp_path / "weekly-review.md").read_text(encoding="utf-8")
    assert "неполный охват" not in markdown


def test_html_review_names_incomplete_coverage(tmp_path: Path) -> None:
    from autoscout.html_review import build_html_review
    from autoscout.review_rendering import render_review

    html = build_html_review(
        [],
        {
            "total_count": 0,
            "incomplete_source_coverages": [
                {"source_name": SOURCE_NAME, "observed_rows": 93, "expected_rows": 100},
            ],
        },
    )
    assert "Неполный охват" in html
    assert SOURCE_NAME in html
    assert "93 из 100" in html

    assert "Неполный охват" not in build_html_review([], {"total_count": 0})

    _write_review_snapshot(tmp_path, [_incomplete_run()])
    assert render_review(tmp_path) == 0
    rendered = (tmp_path / "weekly-review.html").read_text(encoding="utf-8")
    assert "Неполный охват" in rendered
    assert SOURCE_NAME in rendered
    assert "93 из 100" in rendered
