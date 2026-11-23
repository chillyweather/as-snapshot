"""Issue #584 prerequisite contract tests for the Cúspide ranking source."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from autoscout.__main__ import main
from autoscout.ranking_freshness import ranking_row_fingerprint
from autoscout.source_feasibility import evaluate_source_feasibility
from autoscout.source_page_identity import ranking_mention_is_grounded
from autoscout.source_page_identity import ranking_rows
from autoscout.source_probe import run_source_candidate_probe

CUSPIDE_URL = "https://cuspide.com/100-mas-vendidos/"
RANKING_HEADING = "Los 100 más vendidos de la semana"


def test_selector_rank_text_normalizes_to_canonical_rank_and_passes_the_positive_rank_gate() -> None:
    source = _ranking_source(expected_raw_row_count=1)
    html = (
        f'<h1 class="page-title">{RANKING_HEADING}</h1>'
        '<div class="category-page-row"><div class="col">'
        '<p class="livriz-ranking">#1 ranking en venta</p>'
        '<h3 class="name">Book 1</h3>'
        '<p class="author-product-loop">Author 1</p>'
        '<span class="isbn">9780306406157</span>'
        "</div></div>"
    )

    rows = ranking_rows(html, source)
    verdict = evaluate_source_feasibility(
        source,
        {
            "source_page": CUSPIDE_URL,
            "source_url": CUSPIDE_URL,
            "resolved_url": CUSPIDE_URL,
            "head_status": 200,
            "get_status": 200,
            "html": html,
        },
    )

    assert rows[0].rank == "1"
    assert verdict.accepted
    assert verdict.samples[0].rank == "1"
    assert ranking_mention_is_grounded(
        html,
        title="Book 1",
        isbn="9780306406157",
        current_rank="1",
        source_week="de la semana",
        primary_author="Author 1",
        ranking_source=source,
        evidence_url=CUSPIDE_URL,
    )
    bare_html = html.replace("#1 ranking en venta", "1")
    assert ranking_row_fingerprint(rows) == ranking_row_fingerprint(ranking_rows(bare_html, source))


def test_selector_rank_normalization_preserves_bare_positive_integer() -> None:
    source = _ranking_source(expected_raw_row_count=1)
    html = (
        f'<h1 class="page-title">{RANKING_HEADING}</h1>'
        '<div class="category-page-row"><div class="col">'
        '<p class="livriz-ranking">2</p>'
        '<h3 class="name">Book 2</h3>'
        '<p class="author-product-loop">Author 2</p>'
        '<span class="isbn">9780306406157</span>'
        "</div></div>"
    )

    assert ranking_rows(html, source)[0].rank == "2"


@pytest.mark.parametrize(
    "rank_text",
    ["#0 ranking en venta", "#-1 ranking en venta", "#1.5 ranking en venta", "rank 1 of 100"],
)
def test_invalid_or_ambiguous_selector_rank_text_fails_closed(rank_text: str) -> None:
    source = _ranking_source(expected_raw_row_count=1)
    html = (
        f'<h1 class="page-title">{RANKING_HEADING}</h1>'
        '<div class="category-page-row"><div class="col">'
        f'<p class="livriz-ranking">{rank_text}</p>'
        '<h3 class="name">Book 1</h3>'
        '<p class="author-product-loop">Author 1</p>'
        '<span class="isbn">9780306406157</span>'
        "</div></div>"
    )

    rows = ranking_rows(html, source)
    verdict = evaluate_source_feasibility(
        source,
        {
            "source_page": CUSPIDE_URL,
            "source_url": CUSPIDE_URL,
            "resolved_url": CUSPIDE_URL,
            "head_status": 200,
            "get_status": 200,
            "html": html,
        },
    )

    assert rows[0].rank == ""
    assert not verdict.accepted
    assert verdict.reason == "ranking row missing rank"


def test_probe_cli_aligns_boolean_metadata_per_url_and_preserves_repeatable_lists(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_probe(*args: Any, **kwargs: Any) -> int:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return 0

    monkeypatch.setattr("autoscout.cli.onboarding.probe_source_candidates", fake_probe)

    assert (
        main(
            [
                "probe-source-candidate",
                "--url",
                CUSPIDE_URL,
                "--url",
                "https://example.test/other-ranking",
                "--contract-family",
                "ranking-table",
                "--ranking-author-optional",
                "TrUe",
                "--ranking-author-optional",
                "false",
                "--ranking-reporting-period-undated",
                "FALSE",
                "--ranking-reporting-period-undated",
                "true",
                "--ranking-detail-fields",
                "isbn",
                "--ranking-detail-fields",
                "category",
                "--ranking-detail-category-allowlist",
                "adult nonfiction",
                "--ranking-detail-category-allowlist",
                "public affairs",
                "--candidate-category",
                "adult nonfiction",
                "--candidate-category",
                "public affairs",
            ]
        )
        == 0
    )

    assert captured["kwargs"]["contract_metadata"] == [
        {
            "ranking_author_optional": True,
            "ranking_reporting_period_undated": False,
            "ranking_detail_fields": ["isbn", "category"],
            "ranking_detail_category_allowlist": ["adult nonfiction", "public affairs"],
            "eligible_candidate_category": "adult nonfiction",
        },
        {
            "ranking_author_optional": False,
            "ranking_reporting_period_undated": True,
            "eligible_candidate_category": "public affairs",
        },
    ]
    assert type(captured["kwargs"]["contract_metadata"][0]["ranking_author_optional"]) is bool
    assert type(captured["kwargs"]["contract_metadata"][1]["ranking_reporting_period_undated"]) is bool


def test_probe_cli_rejects_non_boolean_ranking_metadata(monkeypatch: Any, capsys: Any) -> None:
    def fail_if_called(*args: Any, **kwargs: Any) -> int:
        raise AssertionError("probe seam must not be called")

    monkeypatch.setattr("autoscout.cli.onboarding.probe_source_candidates", fail_if_called)

    with pytest.raises(SystemExit) as raised:
        main(
            [
                "probe-source-candidate",
                "--url",
                CUSPIDE_URL,
                "--ranking-author-optional",
                "yes",
            ]
        )

    assert raised.value.code == 2
    assert "expected true or false" in capsys.readouterr().err


def test_probe_cli_forwards_fully_declared_undated_ranking_metadata(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_probe(*args: Any, **kwargs: Any) -> int:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return 0

    monkeypatch.setattr("autoscout.cli.onboarding.probe_source_candidates", fake_probe)

    assert (
        main(
            [
                "probe-source-candidate",
                "--url",
                CUSPIDE_URL,
                "--contract-family",
                "ranking-table",
                "--ranking-row-selector",
                ".category-page-row > .col",
                "--ranking-rank-source",
                "selector",
                "--ranking-rank-selector",
                ".livriz-ranking",
                "--ranking-title-selector",
                ".name",
                "--ranking-author-selector",
                ".author-product-loop",
                "--ranking-author-optional",
                "true",
                "--ranking-identifier-pattern",
                r"(?P<identifier>9780306406157)",
                "--ranking-category-marker",
                "Los 100 más vendidos",
                "--ranking-reporting-period-undated",
                "true",
                "--ranking-expected-raw-row-count",
                "100",
                "--ranking-evidence-url",
                "source_page",
                "--ranking-exclusion-term",
                "libro de actividades",
            ]
        )
        == 0
    )

    assert captured["kwargs"]["contract_families"] == ["ranking-table"]
    metadata = captured["kwargs"]["contract_metadata"][0]
    assert metadata["ranking_author_optional"] is True
    assert metadata["ranking_reporting_period_undated"] is True
    assert "ranking_reporting_period_pattern" not in metadata
    assert "ranking_reporting_period_date_format" not in metadata
    assert "ranking_reporting_period_max_age_days" not in metadata


def test_fully_declared_undated_ranking_probe_accepts_scalar_boolean_metadata(tmp_path: Path) -> None:
    metadata = {
        "ranking_row_selector": ".category-page-row > .col",
        "ranking_rank_source": "selector",
        "ranking_rank_selector": ".livriz-ranking",
        "ranking_title_selector": ".name",
        "ranking_author_selector": ".author-product-loop",
        "ranking_author_optional": True,
        "ranking_identifier_pattern": r"(?P<identifier>9780306406157)",
        "adult_nonfiction_exclusions": ["libro de actividades"],
        "ranking_category_marker": "Los 100 más vendidos",
        "ranking_reporting_period_undated": True,
        "ranking_expected_raw_row_count": 100,
        "ranking_evidence_url": "source_page",
    }
    html = _bounded_ranking_with_blank_author(15).encode()
    calls: list[str] = []

    class Response:
        status = 200

        def read(self, _limit: int) -> bytes:
            return html

        def geturl(self) -> str:
            return CUSPIDE_URL

        def close(self) -> None:
            return None

    def opener(request: Any, **_kwargs: Any) -> Response:
        calls.append(request.get_method())
        return Response()

    report, code = run_source_candidate_probe(
        [CUSPIDE_URL],
        contract_families=["ranking-table"],
        contract_metadata=[metadata],
        ledger_path=tmp_path / "ledger.json",
        feasibility_dir=tmp_path / "drafts",
        opener=opener,
    )

    candidate = report["candidates"][0]
    assert code == 0
    assert candidate["outcome"] == "accept"
    assert candidate["contract_metadata"]["ranking_author_optional"] is True
    assert candidate["contract_metadata"]["ranking_reporting_period_undated"] is True
    assert "ranking_reporting_period_pattern" not in candidate["contract_metadata"]
    assert "ranking_reporting_period_date_format" not in candidate["contract_metadata"]
    assert "ranking_reporting_period_max_age_days" not in candidate["contract_metadata"]
    ledger = json.loads((tmp_path / "ledger.json").read_text(encoding="utf-8"))
    persisted = ledger["entries"][0]["contract_metadata"]
    assert persisted["ranking_author_optional"] is True
    assert persisted["ranking_reporting_period_undated"] is True
    assert calls == ["HEAD", "GET"]


def test_probe_cli_propagates_candidate_category_aliases_per_url(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_probe(*args: Any, **kwargs: Any) -> int:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return 0

    monkeypatch.setattr("autoscout.cli.onboarding.probe_source_candidates", fake_probe)

    assert (
        main(
            [
                "probe-source-candidate",
                "--url",
                CUSPIDE_URL,
                "--url",
                "https://example.test/other-ranking",
                "--candidate-category",
                "adult nonfiction",
                "--eligible-candidate-category",
                "public affairs",
            ]
        )
        == 0
    )

    assert captured["kwargs"]["contract_metadata"] == [
        {"eligible_candidate_category": "adult nonfiction"},
        {"eligible_candidate_category": "public affairs"},
    ]


def _ranking_source(*, expected_raw_row_count: int = 100) -> dict[str, Any]:
    return {
        "contract_family": "ranking-table",
        "metadata_ranking_table": True,
        "url": CUSPIDE_URL,
        "visible_title_selector": ".page-title",
        "ranking_row_selector": ".category-page-row > .col",
        "ranking_rank_source": "selector",
        "ranking_rank_selector": ".livriz-ranking",
        "ranking_title_selector": ".name",
        "ranking_author_selector": ".author-product-loop",
        "ranking_identifier_pattern": r"(?P<identifier>9780306406157)",
        "ranking_category_marker": "Los 100 más vendidos",
        "ranking_reporting_period_pattern": r"(?P<period>de la semana)",
        "ranking_expected_raw_row_count": expected_raw_row_count,
        "ranking_evidence_url": "source_page",
        "eligible_candidate_category": "adult nonfiction",
    }


def _bounded_ranking_with_blank_author(row_index: int) -> str:
    rows = []
    for index in range(1, 101):
        author = "" if index == row_index else f"Author {index}"
        rows.append(
            '<div class="category-page-row"><div class="col">'
            f'<p class="livriz-ranking">#{index} ranking en venta</p>'
            f'<h3 class="name">Book {index}</h3>'
            f'<p class="author-product-loop">{author}</p>'
            '<span class="isbn">9780306406157</span>'
            "</div></div>"
        )
    return f'<h1 class="page-title">{RANKING_HEADING}</h1>' + "".join(rows)


def test_bounded_ranking_with_blank_author_rejects_fail_closed() -> None:
    evidence = {
        "source_page": CUSPIDE_URL,
        "source_url": CUSPIDE_URL,
        "resolved_url": CUSPIDE_URL,
        "head_status": 200,
        "get_status": 200,
        "html": _bounded_ranking_with_blank_author(15),
    }

    verdict = evaluate_source_feasibility(_ranking_source(), evidence)

    assert not verdict.accepted
    assert verdict.outcome == "reject"
    assert verdict.reason_code == "ranking_row_shape"
    assert verdict.reason == "ranking row missing author"
    assert verdict.raw_row_count == 100
    assert verdict.ranking_diagnostics is not None
    assert verdict.ranking_diagnostics["row_index"] == 15
