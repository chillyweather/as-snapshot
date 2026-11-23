"""Explicit single list dates outside headings, using synthetic ranking markup."""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from autoscout.source_feasibility import evaluate_source_feasibility
from autoscout.source_page_identity import ranking_page_identity
from autoscout.source_page_identity import validate_ranking_contract_metadata
from autoscout.source_probe import run_source_candidate_probe

URL = "https://example.test/sachbuch"
REFERENCE_DATE = date(2026, 10, 6)


def _source() -> dict[str, Any]:
    return {
        "name": "Synthetic Sachbuch ranking",
        "url": URL,
        "contract_family": "ranking-table",
        "metadata_ranking_table": True,
        "eligible_candidate_category": "adult nonfiction",
        "ranking_row_selector": ".rows > .row",
        "ranking_rank_source": "selector",
        "ranking_rank_selector": ".rank",
        "ranking_title_selector": ".title",
        "ranking_author_selector": ".author",
        "ranking_identifier_selector": ".isbn",
        "ranking_identifier_pattern": r"(?P<identifier>97[89][0-9]{10})",
        "ranking_category_marker": "Sachbuch",
        "ranking_reporting_date_selector": ".meta-date",
        "ranking_reporting_period_pattern": r"(?P<period>\d{2}\.\d{2}\.\d{4})",
        "ranking_reporting_period_date_format": "%d.%m.%Y",
        "ranking_reporting_period_max_age_days": 14,
        "ranking_expected_raw_row_count": 1,
        "ranking_evidence_url": "source_page",
        "adult_nonfiction_exclusions": ["Kinderbuch"],
    }


def _html(date_markup: str = '<div class="meta-date">28.09.2026 15:25 Uhr</div>') -> str:
    return (
        f"<html><body><h1>Wochencharts Sachbuch</h1>{date_markup}"
        '<ol class="rows"><li class="row"><span class="rank">1</span>'
        '<h4 class="title">Synthetic Book</h4><div class="author">Jane Author</div>'
        '<span class="isbn">9780306406157</span></li></ol></body></html>'
    )


def test_explicit_date_accepts_grounded_rows_and_retains_original_date() -> None:
    source = _source()
    assert validate_ranking_contract_metadata(source) == ""
    verdict = evaluate_source_feasibility(
        source,
        {
            "source_page": URL,
            "html": _html(),
            "reference_date": REFERENCE_DATE.isoformat(),
            "get_status": 200,
            "head_status": 200,
        },
    )
    assert verdict.outcome == "accept"
    assert verdict.reporting_period == "28.09.2026"
    assert verdict.raw_row_count == 1
    assert verdict.samples[0].title == "Synthetic Book"
    assert verdict.samples[0].author == "Jane Author"


@pytest.mark.parametrize(
    "date_markup",
    [
        "",
        '<div class="meta-date" hidden>28.09.2026</div>',
        '<nav><div class="meta-date">28.09.2026</div></nav>',
        '<div class="meta-date">28.09.2026</div><div class="meta-date">28.09.2026</div>',
        '<div class="meta-date">01.05.2024</div>',
        '<div class="meta-date">07.10.2026</div>',
        '<div class="meta-date">invalid</div>',
        '<div class="meta-date">28.09.2026, previous list 21.09.2026</div>',
    ],
)
def test_date_selector_fails_closed_without_heading_fallback(date_markup: str) -> None:
    html = _html(date_markup).replace("Wochencharts Sachbuch", "Wochencharts Sachbuch 28.09.2026")
    assert ranking_page_identity(html, _source(), REFERENCE_DATE) is None


def test_single_date_declaration_does_not_accept_a_reporting_range() -> None:
    source = _source()
    source["ranking_reporting_period_pattern"] = r"(?P<period>\d{2}\.\d{2}\.\d{4} - \d{2}\.\d{2}\.\d{4})"
    assert (
        ranking_page_identity(_html('<div class="meta-date">21.09.2026 - 28.09.2026</div>'), source, REFERENCE_DATE)
        is None
    )


def test_legacy_declaration_still_requires_two_dates_in_heading() -> None:
    source = _source()
    source.pop("ranking_reporting_date_selector")
    assert ranking_page_identity(_html(), source, REFERENCE_DATE) is None
    assert ranking_page_identity(_html().replace("Sachbuch", "Sachbuch 28.09.2026"), source, REFERENCE_DATE) is None


def test_wrong_category_and_missing_author_are_still_rejected() -> None:
    assert ranking_page_identity(_html().replace("Sachbuch", "Belletristik"), _source(), REFERENCE_DATE) is None
    verdict = evaluate_source_feasibility(
        _source(),
        {
            "source_page": URL,
            "html": _html().replace('<div class="author">Jane Author</div>', ""),
            "reference_date": REFERENCE_DATE.isoformat(),
            "get_status": 200,
            "head_status": 200,
        },
    )
    assert verdict.outcome == "reject"
    assert "author" in verdict.reason


@pytest.mark.parametrize("selector", ["", "div", ".date, .other", None])
def test_invalid_selector_is_rejected_before_fetch(selector: Any) -> None:
    source = _source()
    source["ranking_reporting_date_selector"] = selector
    assert "ranking reporting date" in validate_ranking_contract_metadata(source)


def test_date_selector_cannot_be_combined_with_undated_freshness() -> None:
    source = _source()
    for field in (
        "ranking_reporting_period_pattern",
        "ranking_reporting_period_date_format",
        "ranking_reporting_period_max_age_days",
    ):
        source.pop(field)
    source["ranking_reporting_period_undated"] = True
    assert "cannot be combined" in validate_ranking_contract_metadata(source)


def test_probe_preserves_single_date_declaration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FixedDate(date):
        @classmethod
        def today(cls) -> date:
            return REFERENCE_DATE

    class Response(io.BytesIO):
        status = 200

        def geturl(self) -> str:
            return URL

    calls: list[str] = []

    def opener(request: Any, timeout: float) -> Response:
        calls.append(request.get_method())
        return Response(_html().encode() if request.get_method() == "GET" else b"")

    monkeypatch.setattr("autoscout.source_probe.date", FixedDate)
    report, code = run_source_candidate_probe(
        [URL],
        contract_families=["ranking-table"],
        contract_metadata=[_source()],
        ledger_path=tmp_path / "ledger.json",
        feasibility_dir=tmp_path / "drafts",
        keep_captures=tmp_path / "captures",
        opener=opener,
    )
    assert code == 0
    assert report["candidates"][0]["contract_metadata"]["ranking_reporting_date_selector"] == ".meta-date"
    assert calls == ["HEAD", "GET"]


def test_replay_retains_separate_visible_date_without_fabricating_it() -> None:
    from autoscout.replay_fixtures import build_replay_fixture_pair

    source = _source()
    source["ranking_reporting_period_pattern"] = r"(?P<period>\d{2}\.\d{2}\.\d{4}) \d{2}:\d{2} Uhr"
    evidence = {
        "collection_mode": "source-candidate-probe",
        "source_name": source["name"],
        "source_url": URL,
        "source_page": URL,
        "resolved_url": URL,
        "html": _html(),
        "reference_date": REFERENCE_DATE.isoformat(),
        "head_status": 200,
        "get_status": 200,
        "contract_family": "ranking-table",
        "contract_metadata": source,
    }
    helper = {
        "version": 1,
        "source_url": URL,
        "language": "de",
        "entries": [
            {
                "title": "Synthetic Book",
                "author": "Jane Author",
                "evidence_url": URL,
                "english_title": "Synthetic Volume",
                "search_translation": "Синтетическая книга",
                "author_transliteration_note": "Джейн Автор",
            }
        ],
    }
    pair = build_replay_fixture_pair(
        URL,
        "synthetic-date",
        evidence,
        signal_family="bestseller/ranking",
        signal_strength="top ranking",
        candidate_category="adult nonfiction",
        language="de",
        helper_metadata=helper,
    )
    import json

    page = json.loads(pair.page_bytes)
    assert '<div class="meta-date">28.09.2026 15:25 Uhr</div>' in page["html"]
    assert ranking_page_identity(page["html"], source, reference_date=REFERENCE_DATE).reporting_period == "28.09.2026"
