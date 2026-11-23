"""Issue #612: a live source check must judge freshness by the clock.

The two Amazon Charts sources failed the editor-facing source check with
"Amazon Charts landing page has no recent dated nonfiction link" while the
weekly workflow kept collecting them successfully. The landing pages were
healthy. ``check-sources`` read its reference date from the configured
``run_date`` label, which had been pinned to 2026-07-04 and never advanced,
so every live dated chart fell outside the recency window and failed closed.
"""

from __future__ import annotations

import json
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

import autoscout.source_check as source_check_module
import autoscout.source_collectors as collectors_module
from autoscout.source_check import _probe_reference_date
from autoscout.source_check import check_sources

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "tests/fixtures/issue-612/amazon-charts-week-resolution.json").read_text(encoding="utf-8"))
CLOCK = date.fromisoformat(FIXTURE["clock"])
STALE_RUN_DATE = FIXTURE["stale_run_date"]
PORTFOLIO = json.loads((ROOT / "configs/current-weekly-portfolio.json").read_text(encoding="utf-8"))["sources"]


class _FakeResponse:
    def __init__(self, url: str, html: str) -> None:
        self._url = url
        self._body = BytesIO(html.encode("utf-8"))
        self.status = 200
        self.headers = {"Content-Type": "text/html; charset=utf-8"}

    def geturl(self) -> str:
        return self._url

    def read(self, *args: Any) -> bytes:
        return self._body.read(*args)

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None


def _amazon_urlopen(request: Any, **_kwargs: Any) -> _FakeResponse:
    """Serve the recorded landing and dated chart captures, nothing else."""
    url = request.full_url if hasattr(request, "full_url") else str(request)
    for landing_url, capture in FIXTURE["marketplaces"].items():
        if url == landing_url:
            return _FakeResponse(url, capture["landing_html"])
        if url == capture["chart_url"]:
            return _FakeResponse(url, capture["chart_html"])
    raise AssertionError(f"unexpected live request: {url}")


def _amazon_sources() -> list[dict[str, Any]]:
    sources = [dict(source) for source in PORTFOLIO if source.get("amazon_charts_weekly") is True]
    assert len(sources) == 2, "the portfolio must carry both Amazon Charts sources"
    for source in sources:
        source["extraction_method"] = "agent-extraction-live"
        source["enable_live"] = True
        source["timeout_seconds"] = 10
    return sources


def _stale_config(tmp_path: Path, sources: list[dict[str, Any]]) -> Path:
    """Write a config whose run_date label is stale, as the live config was."""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"run_date": STALE_RUN_DATE, "sources": sources}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return config_path


# ── The reference-date seam ──────────────────────────────────────────────


def test_live_probe_ignores_a_stale_configured_run_date() -> None:
    assert (
        _probe_reference_date({"run_date": STALE_RUN_DATE}, live=True, effective_date=None) == date.today().isoformat()
    )


def test_replay_probe_keeps_the_configured_run_date_label() -> None:
    assert _probe_reference_date({"run_date": STALE_RUN_DATE}, live=False, effective_date=None) == STALE_RUN_DATE


def test_an_explicit_caller_date_pins_both_modes() -> None:
    for live in (True, False):
        assert (
            _probe_reference_date({"run_date": STALE_RUN_DATE}, live=live, effective_date="2026-08-26") == "2026-08-26"
        )


# ── The end-to-end probe ─────────────────────────────────────────────────


def test_live_probe_captures_both_amazon_sources_despite_a_stale_run_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression: a stale run_date label must not fail a healthy source."""
    monkeypatch.setattr(collectors_module, "urlopen", _amazon_urlopen)
    monkeypatch.setattr(source_check_module, "date", _FrozenDate)
    config_path = _stale_config(tmp_path, _amazon_sources())
    output_dir = tmp_path / "probe-out"

    exit_code = check_sources(config_path, output_dir=output_dir, live=True)
    out = capsys.readouterr().out
    runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))

    assert exit_code == 0
    assert "failed=0" in out
    assert f"source check (live) — {CLOCK.isoformat()}" in out
    for run in runs:
        assert run["status"] == "codex-captured", run.get("reason")
    captured_pages = {
        json.loads(path.read_text(encoding="utf-8"))["source_page"]
        for path in sorted(output_dir.glob("codex-capture-amazon*.json"))
    }
    assert captured_pages == {capture["chart_url"] for capture in FIXTURE["marketplaces"].values()}


def test_a_stale_reference_date_still_fails_closed_with_the_source_specific_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-closed behaviour is preserved: an out-of-window landing page rejects.

    This is the exact failure Issue #612 reported, reproduced by pinning the
    probe clock back to the stale label.
    """
    monkeypatch.setattr(collectors_module, "urlopen", _amazon_urlopen)
    config_path = _stale_config(tmp_path, _amazon_sources())

    exit_code = check_sources(config_path, live=True, effective_date=STALE_RUN_DATE)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "failed=2" in out
    assert out.count("Amazon Charts landing page has no recent dated nonfiction link") == 2


class _FrozenDate(date):
    """Pin the probe clock without pinning the whole process clock."""

    @classmethod
    def today(cls) -> date:
        return CLOCK
