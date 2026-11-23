"""Issue #680: the annual edition-year audit runs as part of the weekly run.

The audit is only worth having if something calls it. These tests pin the
two properties that make the wiring safe to leave switched on: it cannot
fail a run, and it does not reach the editor.

The second matters as much as the first. `source_problems` feeds the
editor's Russian end-of-run summary and means *collection failed*. A source
that needs a rollover check has not failed, and which URL an award list is
pinned to is an operator's maintenance decision, not an editorial one.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from autoscout.annual_source_audit import ANNUAL_AUDIT_STAGE_NAME
from autoscout.annual_source_audit import annual_audit_stage_result

PORTFOLIO = Path(__file__).resolve().parents[1] / "configs" / "current-weekly-portfolio.json"


def _config(tmp_path: Path, *sources: dict[str, Any]) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"sources": list(sources)}), encoding="utf-8")
    return path


TODAY = date(2026, 11, 13)


def _annual(name: str, url: str) -> dict[str, Any]:
    return {
        "name": name,
        "url": url,
        "family": "award",
        "active": True,
        "check_frequency": "annual",
        "refresh_cadence": "annual",
        "url_stability": "year-specific",
        "language": "en",
        "annual_edition": {
            "edition_year": int(name.rsplit(" ", 1)[1]),
            "landing_url": "https://example.test/",
            "release_window": {"opens": "09-01", "closes": "09-30"},
            "stages": ["winner"],
            "tracked_stage": "winner",
        },
    }


# ── The advisory cannot break a run ─────────────────────────────────────────


def test_a_broken_config_reports_unknown_rather_than_raising(tmp_path: Path) -> None:
    """The weekly run must not die because a maintenance advisory could not
    be computed. Every failure path returns a status, never an exception."""
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not json", encoding="utf-8")

    for path in (malformed, tmp_path / "absent.json", tmp_path):
        result = annual_audit_stage_result(path, today=TODAY)
        assert result["status"] == "unknown", path
        assert result["reason"], path


def test_a_portfolio_reference_that_cannot_resolve_reports_unknown(tmp_path: Path) -> None:
    """resolve_weekly_portfolio_or_raise raises here rather than exiting, and
    the advisory swallows it: a missing portfolio is a config problem for the
    audit command to report, not a reason to stop a weekly run."""
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"weekly_portfolio": "no-such-file.json"}), encoding="utf-8")

    result = annual_audit_stage_result(config, today=TODAY)

    assert result["status"] == "unknown"
    assert "no-such-file.json" in result["reason"]


# ── What it reports ─────────────────────────────────────────────────────────


def test_a_clean_portfolio_reports_ok_with_no_sources_listed(tmp_path: Path) -> None:
    config = _config(tmp_path, _annual("A Prize 2026", "https://example.test/p/2026"))

    result = annual_audit_stage_result(config, today=TODAY)

    assert result["status"] == "ok"
    assert result["rollover_checks_required"] == 0
    assert result["sources"] == []


def test_only_the_sources_needing_a_check_are_listed(tmp_path: Path) -> None:
    """A run artifact repeating all twelve verdicts weekly would bury the
    three that matter in nine that do not."""
    config = _config(
        tmp_path,
        _annual("A Prize 2025", "https://example.test/a/2025"),
        _annual("B Prize 2026", "https://example.test/b/2026"),
        _annual("C Prize 2026", "https://example.test/c/2026"),
    )

    result = annual_audit_stage_result(config, today=TODAY)

    assert result["status"] == "attention_required"
    assert result["annual_source_count"] == 3
    assert [source["source_name"] for source in result["sources"]] == ["A Prize 2025"]
    assert result["sources"][0]["reason"] == "newer-edition-expected-evidence-required"


def test_the_run_date_decides_against_the_release_window(tmp_path: Path) -> None:
    """The run passes its effective date, not only its year: before the 2026
    window opens a 2025 edition is current."""
    config = _config(tmp_path, _annual("A Prize 2025", "https://example.test/a/2025"))

    assert annual_audit_stage_result(config, today=date(2026, 8, 31))["status"] == "ok"
    assert annual_audit_stage_result(config, today=date(2026, 9, 1))["status"] == "attention_required"


def test_the_result_is_json_safe_for_a_run_artifact(tmp_path: Path) -> None:
    """It is written into workflow-log.json, so it must serialize as-is."""
    config = _config(tmp_path, _annual("A Prize 2025", "https://example.test/a/2025"))

    assert json.loads(json.dumps(annual_audit_stage_result(config, today=TODAY)))


def test_the_live_portfolio_reports_its_known_checks() -> None:
    result = annual_audit_stage_result(PORTFOLIO, today=TODAY)

    # National Book Awards left this set when it rolled over to 2026 (Issue
    # #826), and PW Best Books and Goodreads Choice Awards when they did
    # (Issue #996). Release windows (#680) add the Andrew Carnegie Medals:
    # the 2027 longlist would have been published in October 2026.
    assert result["status"] == "attention_required"
    assert result["audit_date"] == "2026-11-13"
    assert result["sources"] == [
        {
            "source_name": "Andrew Carnegie Medals for Excellence - Nonfiction 2026",
            "verdict": "unable-to-determine",
            "reason": "newer-edition-expected-evidence-required",
            "edition_year": 2026,
            "url": "https://www.ala.org/carnegie-medals/2026-winners",
        }
    ]


# ── It is wired in, and it stays out of the editor's channel ────────────────


def test_the_weekly_run_logs_the_audit_as_its_own_stage() -> None:
    """Pinned by reading the source: the value of this work is that something
    calls the audit, and an absent call cannot be caught by exercising a run
    that does not make it."""
    import inspect

    from autoscout import weekly_workflow

    source = inspect.getsource(weekly_workflow)

    assert "annual_audit_stage_result" in source
    assert ANNUAL_AUDIT_STAGE_NAME == "annual-source-audit"


def test_the_advisory_never_becomes_a_source_problem() -> None:
    """`source_problems` reaches the editor's Russian summary and means
    collection failed. A rollover check is neither."""
    import inspect

    from autoscout import weekly_workflow

    source = inspect.getsource(weekly_workflow)
    call_site = source[source.index("annual_audit_stage_result(") :][:400]

    assert "source_problems" not in call_site


def test_run_health_reports_the_advisory_from_the_workflow_log(tmp_path: Path) -> None:
    from autoscout.run_health import annual_rollover_findings

    detail = {
        "status": "attention_required",
        "rollover_checks_required": 1,
        "sources": [{"source_name": "A Prize 2025", "edition_year": 2025, "url": "https://example.test/a/2025"}],
    }
    (tmp_path / "workflow-log.json").write_text(
        json.dumps(
            [{"stage": "operation-start", "status": "ok"}, {"stage": ANNUAL_AUDIT_STAGE_NAME, "detail": detail}]
        ),
        encoding="utf-8",
    )

    assert annual_rollover_findings(tmp_path) == detail


def test_a_run_predating_the_check_reports_nothing_rather_than_a_clean_audit(tmp_path: Path) -> None:
    """Absence of the stage is not evidence that nothing needs checking."""
    from autoscout.run_health import annual_rollover_findings

    assert annual_rollover_findings(tmp_path) == {}

    (tmp_path / "workflow-log.json").write_text(
        json.dumps([{"stage": "operation-start", "status": "ok"}]), encoding="utf-8"
    )
    assert annual_rollover_findings(tmp_path) == {}
