"""Offline tests for issue #595, capture budgets that follow the declaration.

The base capture budget assumes one fetch per source.  A declared detail route
fetches one page per row, so the stage budget must grow by that declaration and
by nothing else.  No test here makes a network request.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from autoscout.runtime_budget import CAPTURE_DETAIL_PAGE_ALLOWANCE_SECONDS
from autoscout.runtime_budget import CAPTURE_RUNTIME_BUDGET_SECONDS
from autoscout.runtime_budget import capture_runtime_budget_for
from autoscout.source_collection import prepare_codex_extraction_worklist

# The live measurement recorded in issue #595 on 2026-08-24: one list page and
# 100 product pages took 133.8s in total, with a 2.62s worst-case page.
MEASURED_CUSPIDE_SECONDS = 133.8

DETAIL_ROUTE: dict[str, Any] = {
    "ranking_detail_link_selector": ".name",
    "ranking_detail_url_pattern": r"https://example\.test/producto/[^?#]+/?",
    "ranking_detail_scope_selector": ".product_meta",
    "ranking_detail_fields": ["isbn"],
    "ranking_detail_identifier_selector": ".sku",
    "ranking_detail_identifier_pattern": "(?P<identifier>97[89][0-9]{10})",
    "ranking_detail_max_pages": 100,
}


def _single_page_source(name: str = "Single page source") -> dict[str, Any]:
    return {"name": name, "url": "https://example.test/list", "contract_family": "ranking-table"}


def _detail_route_source(max_pages: int = 100) -> dict[str, Any]:
    return {
        "name": "Detail route source",
        "url": "https://example.test/list",
        "contract_family": "ranking-table",
        **DETAIL_ROUTE,
        "ranking_detail_max_pages": max_pages,
    }


def test_a_portfolio_without_detail_routes_keeps_the_base_budget() -> None:
    sources = [_single_page_source(f"source {index}") for index in range(31)]
    assert capture_runtime_budget_for(sources) == CAPTURE_RUNTIME_BUDGET_SECONDS


def test_an_empty_portfolio_keeps_the_base_budget() -> None:
    assert capture_runtime_budget_for([]) == CAPTURE_RUNTIME_BUDGET_SECONDS


def test_a_detail_route_adds_exactly_its_declared_pages() -> None:
    expected = CAPTURE_RUNTIME_BUDGET_SECONDS + 100 * CAPTURE_DETAIL_PAGE_ALLOWANCE_SECONDS
    assert capture_runtime_budget_for([_detail_route_source()]) == expected


def test_the_budget_follows_the_declaration_not_the_source_count() -> None:
    small = capture_runtime_budget_for([_detail_route_source(max_pages=10)])
    large = capture_runtime_budget_for([_detail_route_source(max_pages=100)])
    assert large - small == 90 * CAPTURE_DETAIL_PAGE_ALLOWANCE_SECONDS


def test_two_detail_routes_each_earn_their_own_allowance() -> None:
    one = _detail_route_source(max_pages=100)
    two = {**_detail_route_source(max_pages=50), "name": "Second detail source"}
    expected = CAPTURE_RUNTIME_BUDGET_SECONDS + 150 * CAPTURE_DETAIL_PAGE_ALLOWANCE_SECONDS
    assert capture_runtime_budget_for([one, two, _single_page_source()]) == expected


def test_the_derived_budget_covers_the_measured_cuspide_capture() -> None:
    """The whole point of the issue: 100 declared pages must fit 133.8s."""
    budget = capture_runtime_budget_for([_detail_route_source(max_pages=100)])
    assert budget >= MEASURED_CUSPIDE_SECONDS
    # The single-page sources keep their own base budget on top of it.
    assert budget - MEASURED_CUSPIDE_SECONDS >= CAPTURE_RUNTIME_BUDGET_SECONDS


def test_the_configured_portfolio_now_fits_its_detail_route() -> None:
    path = Path("configs/current-weekly-pilot-live.json")
    from autoscout.config import resolve_weekly_portfolio

    config = resolve_weekly_portfolio(json.loads(path.read_text(encoding="utf-8")), path)
    budget = capture_runtime_budget_for(config["sources"])
    assert budget >= CAPTURE_RUNTIME_BUDGET_SECONDS + MEASURED_CUSPIDE_SECONDS


def test_the_derivation_reads_no_clock_and_makes_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the budget derivation must be a pure function of configuration")

    monkeypatch.setattr("time.monotonic", forbidden)
    monkeypatch.setattr("time.time", forbidden)
    assert capture_runtime_budget_for([_detail_route_source()]) > 0


def _write_config(tmp_path: Path, sources: list[dict[str, Any]]) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"sources": sources}, ensure_ascii=False), encoding="utf-8")
    return path


def _snapshot_budget(tmp_path: Path) -> float:
    snapshot = json.loads((tmp_path / "progress-snapshot.json").read_text(encoding="utf-8"))
    return float(snapshot["rows"][0]["remaining_runtime_budget_seconds"])


def test_the_worklist_derives_its_budget_from_the_configuration(tmp_path: Path) -> None:
    path = _write_config(tmp_path, [_detail_route_source()])
    prepare_codex_extraction_worklist(path, tmp_path, "2026-08-24")
    expected = CAPTURE_RUNTIME_BUDGET_SECONDS + 100 * CAPTURE_DETAIL_PAGE_ALLOWANCE_SECONDS
    assert _snapshot_budget(tmp_path) == pytest.approx(expected, abs=1.0)


def test_an_explicit_budget_still_wins(tmp_path: Path) -> None:
    path = _write_config(tmp_path, [_detail_route_source()])
    prepare_codex_extraction_worklist(path, tmp_path, "2026-08-24", runtime_budget_seconds=42.0)
    assert _snapshot_budget(tmp_path) == pytest.approx(42.0, abs=1.0)


def test_an_invalid_explicit_budget_is_still_rejected(tmp_path: Path) -> None:
    path = _write_config(tmp_path, [_single_page_source()])
    with pytest.raises(ValueError, match="must be positive"):
        prepare_codex_extraction_worklist(path, tmp_path, "2026-08-24", runtime_budget_seconds=0)
    with pytest.raises(ValueError, match="must be positive"):
        prepare_codex_extraction_worklist(path, tmp_path, "2026-08-24", per_source_timeout_seconds=-1)
