"""Issue #665: Cúspide short-ranking regression evidence.

On 2026-09-07 the live health check observed 93 raw rows for
"Cúspide 100 más vendidos" while the contract requires exactly 100.
The capture itself was discarded, so no retained HTML accompanies the
report. The historical cause is therefore undetermined: a later
100-row response shows current recovery but cannot distinguish a
transient incomplete publisher ranking from a transient capture or
markup problem. This change is diagnostic hardening that preserves
the fail-closed exact-100 rule, not a demonstrated root-cause repair.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from autoscout.source_check import check_sources
from autoscout.source_page_identity import attach_ranking_row_diagnostics
from autoscout.source_page_identity import ranking_raw_row_count_error
from autoscout.source_page_identity import ranking_raw_row_diagnostics
from autoscout.source_page_identity import ranking_rows
from autoscout.source_page_identity import validate_page_shape

SOURCE_URL = "https://cuspide.com/100-mas-vendidos/"


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
        "ranking_detail_link_selector": ".name",
        "ranking_detail_url_pattern": r"https://cuspide\.com/producto/[^?#]+/?",
        "ranking_detail_scope_selector": ".product_meta",
        "ranking_detail_max_pages": 100,
        "ranking_detail_fields": ["isbn", "category"],
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


def test_short_93_row_ranking_fails_closed_with_exact_100_rule() -> None:
    source = _cuspide_source()
    html = _ranking_html(row_count=93)

    rows = ranking_rows(html, dict(source))
    assert len(rows) == 93

    assert ranking_raw_row_count_error(source, len(rows)) == ("ranking page exposes 93 raw rows; expected exactly 100")

    page_data = {"source_page": SOURCE_URL, "resolved_url": SOURCE_URL, "html": html}
    assert validate_page_shape(dict(source), page_data) == (
        "page shape validation failed: ranking page exposes 93 raw rows; expected exactly 100"
    )

    diagnostics = ranking_raw_row_diagnostics(html, source)
    assert diagnostics["raw_row_count"] == 93
    assert diagnostics["observed_ranks"] == [str(rank) for rank in range(1, 94)]
    assert diagnostics["missing_ranks"] == [str(rank) for rank in range(94, 101)]
    assert diagnostics["duplicate_ranks"] is False


def test_full_100_row_ranking_passes_row_count_gate() -> None:
    source = _cuspide_source()
    html = _ranking_html(row_count=100)

    rows = ranking_rows(html, dict(source))
    assert len(rows) == 100
    assert ranking_raw_row_count_error(source, len(rows)) == ""

    diagnostics = ranking_raw_row_diagnostics(html, source)
    assert diagnostics["raw_row_count"] == 100
    assert diagnostics["missing_ranks"] == []


def test_diagnostics_compute_findings_before_truncating_display() -> None:
    """Ranks beyond the display bound must not read as missing (review #666)."""
    source = _cuspide_source(ranking_expected_raw_row_count=201)
    html = _ranking_html(row_count=202)

    diagnostics = ranking_raw_row_diagnostics(html, source)

    assert diagnostics["raw_row_count"] == 202
    # Ranks 1-201 are all present, so nothing is missing even though the
    # displayed observed list is truncated.
    assert diagnostics["missing_ranks"] == []
    assert diagnostics["missing_rank_count"] == 0
    assert diagnostics["duplicate_ranks"] is False
    assert diagnostics["observed_rank_count"] == 202
    assert len(diagnostics["observed_ranks"]) == 200  # type: ignore[arg-type]
    assert diagnostics["observed_ranks_truncated"] is True


def test_default_health_check_prints_row_diagnostics_without_retention(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default no-retention check must still surface the #665 diagnostics."""
    fixture_path = tmp_path / "cuspide-93-page.json"
    fixture_path.write_text(
        json.dumps({"html": _ranking_html(row_count=93)}, ensure_ascii=False),
        encoding="utf-8",
    )
    source = _cuspide_source(
        name="Cúspide 100 más vendidos",
        family="bestseller/ranking",
        active=True,
        access_status="open",
        check_frequency="weekly",
        language="es",
        extraction_method="codex-extraction-replay",
        source_page_fixture_file=str(fixture_path),
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"run_date": "2026-09-08", "sources": [source]}, ensure_ascii=False),
        encoding="utf-8",
    )

    exit_code = check_sources(config_path, live=False)

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "discarded" in out
    assert "Cúspide 100 más vendidos" in out
    assert "ranking page exposes 93 raw rows; expected exactly 100" in out
    assert "rows=93" in out
    assert "94" in out and "100" in out


def test_shared_enrichment_only_touches_raw_row_failures_and_never_raises() -> None:
    """Live and replay paths share one enrichment seam with identical behavior."""
    source = _cuspide_source()
    html = _ranking_html(row_count=93)
    base = {"collector_kind": "agent-extraction", "safe_failure_status": "page_shape"}

    enriched = attach_ranking_row_diagnostics(
        base, html, source, "page shape validation failed: ranking page exposes 93 raw rows; expected exactly 100"
    )
    assert enriched["collector_kind"] == "agent-extraction"
    assert enriched["safe_failure_status"] == "page_shape"
    assert enriched["raw_row_count"] == 93
    assert enriched["missing_ranks"] == [str(rank) for rank in range(94, 101)]
    assert base == {"collector_kind": "agent-extraction", "safe_failure_status": "page_shape"}

    untouched = attach_ranking_row_diagnostics(base, html, source, "page shape validation failed: something else")
    assert untouched == base

    degenerate = attach_ranking_row_diagnostics(base, "", {"ranking_expected_raw_row_count": "not-an-int"}, "raw rows?")
    assert degenerate["safe_failure_status"] == "page_shape"
