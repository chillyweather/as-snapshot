"""Issue #584 source-specific onboarding contract for Cúspide."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from autoscout.config import load_validated_config
from autoscout.ranking_detail_route import RankingDetailRoute
from autoscout.ranking_detail_route import ranking_detail_fields_from_html
from autoscout.ranking_freshness import RankingFreshnessLedger
from autoscout.ranking_freshness import ranking_freshness_state_key
from autoscout.source_collection import SourceCollection
from autoscout.source_page_identity import ranking_rows

ROOT = Path(__file__).resolve().parents[1]
SOURCE_NAME = "Cúspide 100 más vendidos"
SOURCE_URL = "https://cuspide.com/100-mas-vendidos/"
ALLOWLIST = [
    "Autoayuda, pensamiento y prácticas",
    "Historia de América",
    "Ciencias políticas y teoría política",
]
CONFIG_PATHS = {
    "portfolio": ROOT / "configs/current-weekly-portfolio.json",
    "pilot": ROOT / "configs/current-weekly-pilot.json",
    "pilot_live": ROOT / "configs/current-weekly-pilot-live.json",
    "live": ROOT / "configs/current-weekly-live.json",
}


def _cuspide_source(config: dict[str, Any], label: str) -> dict[str, Any]:
    matches = [source for source in config.get("sources", []) if source.get("name") == SOURCE_NAME]
    assert len(matches) == 1, f"{label} must contain exactly one Cúspide source"
    source = matches[0]
    assert source.get("url") == SOURCE_URL
    return source


def test_cuspide_confirmed_onboarding_contract_and_replay(tmp_path: Path) -> None:
    resolved_configs = {label: load_validated_config(path) for label, path in CONFIG_PATHS.items()}
    sources = {label: _cuspide_source(config, label) for label, config in resolved_configs.items()}
    portfolio_source = sources["portfolio"]

    assert portfolio_source["language"] == "es"
    assert portfolio_source["country"] == "Argentina"
    assert portfolio_source["market"] == "Argentina"
    assert portfolio_source["family"] == "bestseller/ranking"
    assert portfolio_source["signal_family"] == "bestseller/ranking"
    assert portfolio_source["active"] is True
    assert portfolio_source["access_status"] == "open"
    assert portfolio_source["check_frequency"] == "weekly"
    assert portfolio_source["expected_category"] == "adult nonfiction"
    assert portfolio_source["lifecycle_status"] == "established"
    assert portfolio_source["metadata_ranking_table"] is True
    assert portfolio_source["ranking_author_optional"] is True
    assert portfolio_source["ranking_reporting_period_undated"] is True
    assert all(
        not str(portfolio_source.get(field, "")).strip()
        for field in (
            "ranking_reporting_period_pattern",
            "ranking_reporting_period_date_format",
            "ranking_reporting_period_max_age_days",
        )
    )
    assert portfolio_source["ranking_row_selector"] == ".category-page-row > .col"
    assert portfolio_source["ranking_rank_source"] == "selector"
    assert portfolio_source["ranking_rank_selector"] == ".livriz-ranking"
    assert portfolio_source["ranking_title_selector"] == ".name"
    assert portfolio_source["ranking_author_selector"] == ".author-product-loop"
    assert portfolio_source["ranking_expected_raw_row_count"] == 100
    assert portfolio_source["ranking_evidence_url"] == "detail_page"
    assert portfolio_source["ranking_detail_link_selector"] == ".name"
    assert portfolio_source["ranking_detail_scope_selector"] == ".product_meta"
    assert portfolio_source["ranking_detail_max_pages"] == 100
    assert portfolio_source["ranking_detail_fields"] == ["isbn", "category"]
    assert portfolio_source["ranking_detail_identifier_selector"] == ".sku"
    assert portfolio_source["ranking_detail_category_selector"] == ".posted_in"
    assert portfolio_source["ranking_detail_category_allowlist"] == ALLOWLIST

    from test_source_onboarding_contract import LEGACY_PORTFOLIO_SOURCES

    assert SOURCE_NAME not in LEGACY_PORTFOLIO_SOURCES

    assert portfolio_source.get("extraction_method") != "codex-extraction-replay"
    assert sources["pilot"]["extraction_method"] == "codex-extraction-replay"
    for live_label in ("pilot_live", "live"):
        assert sources[live_label]["extraction_method"] == "agent-extraction-live"
        assert sources[live_label]["enable_live"] is True
        assert sources[live_label]["extraction_backend"] == "real"
        assert sources[live_label]["enable_real_backend"] is True

    replay_source = sources["pilot"]
    page_path = (CONFIG_PATHS["pilot"].parent / replay_source["source_page_fixture_file"]).resolve()
    extraction_path = (CONFIG_PATHS["pilot"].parent / replay_source["extraction_response_file"]).resolve()
    assert page_path.is_file()
    assert extraction_path.is_file()
    page = json.loads(page_path.read_text(encoding="utf-8"))
    extraction = json.loads(extraction_path.read_text(encoding="utf-8"))

    rows = ranking_rows(page["html"], replay_source)
    assert len(rows) == 100
    assert len(page["detail_pages"]) == 100
    assert page["source_page"] == SOURCE_URL

    expected_mentions = [
        {
            "current_rank": "11",
            "title": "LOS DIAS DE LA CONSTITUCION",
            "isbn": "9786313013401",
            "evidence_url": "https://cuspide.com/producto/los-dias-de-la-constitucion/",
            "primary_authors": ["SACHERI, EDUARDO"],
            "english_title": "The Days of the Constitution",
            "search_translation": "Дни Конституции",
            "author_transliteration_note": "Эдуардо Сачери",
        },
        {
            "current_rank": "12",
            "title": "76",
            "isbn": "9789504992752",
            "evidence_url": "https://cuspide.com/producto/76/",
            "primary_authors": ["PIGNA, FELIPE"],
            "english_title": "Seventy-Six",
            "search_translation": "Семьдесят шесть",
            "author_transliteration_note": "Фелипе Пинья",
        },
        {
            "current_rank": "13",
            "title": "MI PSICOLOGA ME DIJO",
            "isbn": "9789801867272",
            "evidence_url": "https://cuspide.com/producto/mi-psicologa-me-dijo/",
            "primary_authors": ["HOYER, KATHERINE", "R.G, YULIBET..."],
            "english_title": "My Psychologist Told Me",
            "search_translation": "Мой психолог сказал мне",
            "author_transliteration_note": "Кэтрин Хойер; Юлибет Р. Г.",
        },
    ]
    mentions = extraction["mentions"]
    assert len(mentions) == 3
    assert [mention["current_rank"] for mention in mentions] == ["11", "12", "13"]
    expected_detail_categories = [
        "Historia de América",
        "Historia de América",
        "Autoayuda, pensamiento y prácticas",
    ]
    detail_route = RankingDetailRoute.from_source(replay_source)
    for mention, expected, expected_detail_category in zip(
        mentions, expected_mentions, expected_detail_categories, strict=True
    ):
        for field, value in expected.items():
            assert mention[field] == value
        detail_page = next(
            detail for detail in page["detail_pages"] if detail.get("detail_url") == mention["evidence_url"]
        )
        detail_values, detail_error = ranking_detail_fields_from_html(
            detail_page["html"], detail_route, enforce_category_allowlist=False
        )
        assert not detail_error
        assert detail_values["category"] == expected_detail_category
        assert detail_values["category"] in ALLOWLIST
        assert "category" not in mention
        assert mention["candidate_category"] == "adult nonfiction"
        assert mention["source_url"] == SOURCE_URL
        assert mention["source_week"] == "2026-08-24"
        assert mention["signal_type"] == "bestseller/ranking"
        assert mention["signal_strength"] == "top ranking"

    authorless_row = next((row for row in rows if not row.author), None)
    assert authorless_row is not None
    assert authorless_row.author == ""
    detail_page = next(
        detail for detail in page["detail_pages"] if detail.get("detail_url") == authorless_row.detail_url
    )
    detail_values, detail_error = ranking_detail_fields_from_html(
        detail_page["html"], detail_route, enforce_category_allowlist=False
    )
    assert not detail_error
    assert detail_values["category"] not in ALLOWLIST
    assert authorless_row.detail_url not in {mention["evidence_url"] for mention in mentions}

    first_output = tmp_path / "first-output"
    first_result = SourceCollection(
        CONFIG_PATHS["pilot"],
        first_output,
        "2026-08-24",
        RankingFreshnessLedger(),
    ).collect([replay_source])
    assert first_result.source_runs[0]["status"] == "collected"
    assert first_result.failed_source_count == 0
    assert len(first_result.source_mentions) == 3
    assert all("category" not in mention for mention in first_result.source_mentions)
    assert first_result.ranking_freshness is not None
    assert first_result.ranking_freshness.record_for(ranking_freshness_state_key(replay_source)) is not None

    second_result = SourceCollection(
        CONFIG_PATHS["pilot"],
        tmp_path / "second-output",
        "2026-08-24",
        first_result.ranking_freshness,
    ).collect([replay_source])
    # The weekly list run again the same day has not published its next edition:
    # skipped, never re-emitted.
    assert second_result.source_runs[0]["status"] == "skipped"
    assert second_result.source_runs[0]["debug_metadata"]["safe_failure_status"] == "ranking_list_not_yet_refreshed"
    assert second_result.failed_source_count == 0
    assert second_result.skipped_source_count == 1
    assert second_result.source_mentions == []

    # Unchanged for its whole weekly cadence, the list is frozen.
    third_result = SourceCollection(
        CONFIG_PATHS["pilot"],
        tmp_path / "third-output",
        "2026-08-31",
        second_result.ranking_freshness,
    ).collect([replay_source])
    assert third_result.source_runs[0]["status"] == "failed"
    assert third_result.source_runs[0]["debug_metadata"]["safe_failure_status"] == "ranking_list_frozen"
    assert third_result.failed_source_count == 1
    assert third_result.source_mentions == []
