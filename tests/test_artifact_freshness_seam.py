from __future__ import annotations

import re
from pathlib import Path

import pytest

import autoscout.artifact_freshness as artifact_freshness
from autoscout.artifact_freshness import ALL_FRESHNESS_VALUES
from autoscout.artifact_freshness import FRESH
from autoscout.artifact_freshness import REUSED
from autoscout.artifact_freshness import UNBOUND
from autoscout.artifact_freshness import is_stale_artifact_freshness
from autoscout.config import validate_config
from autoscout.html_review import build_html_review
from autoscout.json_io import write_json
from autoscout.reporting import COLLECTING_STATUSES
from autoscout.reporting import major_warnings
from autoscout.reporting import source_runs_with_pipeline_counts
from autoscout.review_rendering import render_review
from autoscout.run_health import codex_workflow_health
from autoscout.russian_workflow import completion_message
from autoscout.source_collection import _collected_status
from autoscout.source_collection import source_run_status_from_artifact_freshness
from autoscout.source_run_status import ALL_SOURCE_RUN_STATUSES
from autoscout.source_run_status import SOURCE_STATUS_COLLECTED
from autoscout.source_run_status import SOURCE_STATUS_REUSED
from autoscout.source_run_status import is_stale_source_run
from autoscout.source_run_status import normalized_source_run_status
from autoscout.source_run_status import summarize_source_runs

PRODUCTION_DIR = Path(__file__).parents[1] / "autoscout"
FAULT_SURFACES = (
    "run_health.py",
    "reporting.py",
    "russian_workflow.py",
    "review_rendering.py",
    "html_review.py",
)
EXPECTED_ARTIFACT_PREDICATE_CALLERS = {
    "codex_workflow.py",
    "source_collection.py",
}


def test_freshness_values_and_fault_predicate_are_closed() -> None:
    assert ALL_FRESHNESS_VALUES == (FRESH, REUSED, UNBOUND)
    assert is_stale_artifact_freshness(REUSED) is True
    assert is_stale_artifact_freshness(FRESH) is False
    assert is_stale_artifact_freshness(UNBOUND) is False


def test_fault_surfaces_use_only_the_shared_predicates() -> None:
    production_files = tuple(PRODUCTION_DIR.rglob("*.py"))
    artifact_predicate_callers = {
        path.relative_to(PRODUCTION_DIR).as_posix()
        for path in production_files
        if any(
            token in path.read_text(encoding="utf-8")
            for token in ("is_stale_artifact_freshness(", "_is_stale_artifact_freshness(")
        )
        and path.name != "artifact_freshness.py"
    }
    assert artifact_predicate_callers == EXPECTED_ARTIFACT_PREDICATE_CALLERS

    for path in production_files:
        source = path.read_text(encoding="utf-8")
        assert not re.search(r'(?<![A-Za-z_])==\s*["\']reused["\']', source)
        assert not re.search(r'(?<![A-Za-z_])["\']reused["\']\s*==', source)
        assert not re.search(r"(?<![A-Za-z_])==\s*REUSED\b", source)
        assert not re.search(r"(?<![A-Za-z_])REUSED\s*==", source)

    for filename in FAULT_SURFACES:
        source = (PRODUCTION_DIR / filename).read_text(encoding="utf-8")
        if filename == "run_health.py":
            assert "is_stale_source_run(" in source
        else:
            assert "summarize_source_runs(" in source or "_summarize_source_runs(" in source


def test_source_run_domain_owns_stale_evidence_classification() -> None:
    assert is_stale_source_run(SOURCE_STATUS_REUSED) is True
    assert is_stale_source_run(SOURCE_STATUS_COLLECTED) is False
    assert normalized_source_run_status(" cache-hit ") is None
    assert [is_stale_source_run(status) for status in ALL_SOURCE_RUN_STATUSES] == [
        False,
        False,
        False,
        False,
        False,
        True,
        False,
        False,
        False,
    ]


def test_source_run_summary_normalizes_once_and_counts_the_closed_policy() -> None:
    summary = summarize_source_runs([" collected ", SOURCE_STATUS_REUSED, "failed", "cache-hit", None, "precollected"])

    assert summary.statuses == (SOURCE_STATUS_COLLECTED, SOURCE_STATUS_REUSED, "failed", "precollected")
    assert summary.stale_source_count == 1
    assert summary.collecting_source_count == 2
    assert summary.freshly_collected_count == 1
    assert tuple(ALL_SOURCE_RUN_STATUSES) == (
        "inactive",
        "skipped",
        "ignored",
        "failed",
        "collected",
        "reused",
        "precollected",
        "codex-captured",
        "not-due",
    )


def test_fault_surfaces_use_actual_source_statuses() -> None:
    warning_args = {
        "skipped_source_count": 0,
        "failed_source_count": 0,
        "ignored_source_count": 0,
        "editor_facing_count": 10,
        "editor_row_target": 10,
        "reused_source_count": 0,
        "collecting_source_count": 1,
    }
    assert not any(
        "earlier run" in warning
        for warning in major_warnings(**warning_args, source_statuses=[SOURCE_STATUS_COLLECTED])
    )
    stale_warnings = major_warnings(**warning_args, source_statuses=[SOURCE_STATUS_REUSED])
    assert any("earlier run" in warning for warning in stale_warnings)

    fresh_message = completion_message(
        outcome="completed/no-attention",
        reused_source_count=1,
        collecting_source_count=1,
        source_statuses=[SOURCE_STATUS_COLLECTED],
    )
    stale_message = completion_message(
        outcome="completed/no-attention",
        reused_source_count=0,
        collecting_source_count=1,
        source_statuses=[SOURCE_STATUS_REUSED],
    )
    assert "предыдущего запуска" not in fresh_message
    assert "предыдущего запуска" in stale_message

    fresh_html = build_html_review(
        [],
        {
            "reused_source_count": 1,
            "collecting_source_count": 1,
            "source_statuses": [SOURCE_STATUS_COLLECTED],
        },
    )
    stale_html = build_html_review(
        [],
        {
            "reused_source_count": 0,
            "collecting_source_count": 1,
            "source_statuses": [SOURCE_STATUS_REUSED],
        },
    )
    assert "предыдущего запуска" not in fresh_html
    assert "предыдущего запуска" in stale_html


def test_presentation_uses_legacy_aggregates_without_source_statuses() -> None:
    assert any(
        "earlier run" in warning
        for warning in major_warnings(
            skipped_source_count=0,
            failed_source_count=0,
            ignored_source_count=0,
            editor_facing_count=10,
            reused_source_count=1,
            collecting_source_count=2,
        )
    )
    assert "предыдущего запуска" in completion_message(
        outcome="completed/no-attention",
        reused_source_count=1,
        collecting_source_count=2,
    )
    assert "предыдущего запуска" in build_html_review(
        [],
        {"reused_source_count": 1, "collecting_source_count": 2},
    )


def test_artifact_to_source_adapter_is_exhaustive() -> None:
    assert source_run_status_from_artifact_freshness(FRESH) == SOURCE_STATUS_COLLECTED
    assert source_run_status_from_artifact_freshness(REUSED) == SOURCE_STATUS_REUSED
    assert source_run_status_from_artifact_freshness(UNBOUND) == SOURCE_STATUS_COLLECTED

    zero_common = {"extracted_count": 0, "valid_mention_count": 0, "admissible_mention_count": 0}
    fresh_zero_status, _ = _collected_status({"artifact_freshness": FRESH}, **zero_common)
    reused_zero_status, _ = _collected_status({"artifact_freshness": REUSED}, **zero_common)
    assert fresh_zero_status == "failed"
    assert reused_zero_status == SOURCE_STATUS_REUSED


def test_render_review_uses_source_run_statuses(tmp_path: Path) -> None:
    write_json(tmp_path / "scouting-candidates.json", [])
    write_json(tmp_path / "source-runs.json", [{"status": SOURCE_STATUS_COLLECTED}])
    write_json(
        tmp_path / "run-summary.json",
        {
            "run_date": "2026-08-05",
            "editor_facing_count": 0,
            "reused_artifact_count": 1,
            "freshly_collected_count": 0,
        },
    )

    assert render_review(tmp_path) == 0

    assert "предыдущего запуска" not in (tmp_path / "weekly-review.md").read_text(encoding="utf-8")
    assert "предыдущего запуска" not in (tmp_path / "weekly-review.html").read_text(encoding="utf-8")


def test_codex_health_uses_source_run_statuses() -> None:
    fresh_runs = [{"collection_mode": "codex-capture", "status": SOURCE_STATUS_COLLECTED}]
    stale_runs = [{"collection_mode": "codex-capture", "status": SOURCE_STATUS_REUSED}]
    whitespace_stale_runs = [{"collection_mode": "codex-capture", "status": " reused "}]

    assert codex_workflow_health(fresh_runs, {}) == "healthy"
    assert codex_workflow_health(stale_runs, {}) == "stale-evidence"
    assert codex_workflow_health(whitespace_stale_runs, {}) == "stale-evidence"


def test_artifact_freshness_converts_to_a_source_run_status_at_collection_boundary() -> None:
    common = {
        "extracted_count": 1,
        "valid_mention_count": 1,
        "admissible_mention_count": 1,
    }

    reused_status, _ = _collected_status({"artifact_freshness": REUSED}, **common)
    collected_status, _ = _collected_status({"artifact_freshness": FRESH}, **common)

    assert reused_status == SOURCE_STATUS_REUSED
    assert collected_status == SOURCE_STATUS_COLLECTED


def test_collecting_status_accounting_is_independent_of_fault_predicate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(artifact_freshness, "is_stale_artifact_freshness", lambda value: False)

    updated = source_runs_with_pipeline_counts(
        [{"source_name": "source", "status": SOURCE_STATUS_REUSED}],
        [
            {
                "identity_key": "book|author",
                "source_evidence": [{"source_name": "source"}],
            }
        ],
        [],
    )

    assert COLLECTING_STATUSES == frozenset({"collected", "reused"})
    assert updated[0]["editor_facing_count"] == 1


@pytest.mark.parametrize("freshness", (FRESH, REUSED, UNBOUND))
def test_config_accepts_each_serialized_freshness_value(freshness: str) -> None:
    assert validate_config({"sources": [{"name": "source", "language": "en", "artifact_freshness": freshness}]}) == []


def test_config_rejects_a_value_outside_the_closed_domain() -> None:
    errors = validate_config({"sources": [{"name": "source", "language": "en", "artifact_freshness": "cache-hit"}]})

    assert any("artifact_freshness" in error for error in errors)
