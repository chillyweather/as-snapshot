"""End-to-end regression tests for issue #473 capture outcome reporting."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

from autoscout.weekly_workflow import deterministic_adapters
from autoscout.weekly_workflow import run_weekly_workflow

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "issue-473-capture-outcomes.json"
_CAPTURE_OUTCOMES = ("inactive", "skipped", "ignored", "failed", "precollected", "codex-captured", "not-due")
_SOURCE_OUTCOMES = (*_CAPTURE_OUTCOMES, "unknown")
_PIPELINE_SUCCESS_STATUSES = frozenset({"collected", "reused"})
_RUN_DATE = date(2026, 8, 15)


def _source_identity(run: dict[str, Any]) -> tuple[str, str]:
    url = str(run.get("url") or run.get("source_url") or "").strip().casefold().rstrip("/")
    if url:
        return "url", url
    name = str(run.get("source_name") or run.get("name") or "").strip().casefold()
    return "name", name


def _expected_source_outcome_counts(
    capture_ledger: list[dict[str, Any]], pipeline_ledger: list[dict[str, Any]]
) -> Counter[str]:
    outcomes: dict[tuple[str, str], str] = {}
    for run in capture_ledger:
        status = str(run.get("status", "")).strip()
        outcomes[_source_identity(run)] = status if status in _CAPTURE_OUTCOMES else "unknown"
    for run in pipeline_ledger:
        status = str(run.get("status", "")).strip()
        if status in _PIPELINE_SUCCESS_STATUSES:
            continue
        outcomes[_source_identity(run)] = status if status in _CAPTURE_OUTCOMES else "unknown"
    counts = Counter(outcomes.values())
    return Counter({outcome: counts[outcome] for outcome in _SOURCE_OUTCOMES})


def _load_fixture() -> dict[str, Any]:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _mixed_stage_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    mixed = deepcopy(fixture)
    mixed_source = mixed["mixed_stage_source"]
    mixed["source_runs"] = [run for run in mixed["source_runs"] if run["source_name"] != "Failed Capture Two"] + [
        mixed_source["source_run"]
    ]
    mixed["work_items"].append(mixed_source["work_item"])
    mixed["captured_pages"][mixed_source["work_item"]["source_name"]] = mixed_source["captured_page"]
    mixed["extraction_entries_by_source"][mixed_source["work_item"]["source_name"]] = mixed_source["extraction_entries"]
    return mixed


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "run_date": _RUN_DATE.isoformat(),
                "readiness_purpose": "recurring-weekly",
                "editor_row_target": 1,
                "pilot_row_min": 1,
                "pilot_row_max": 1,
                "pilot_mode": True,
                "sources": [],
            }
        ),
        encoding="utf-8",
    )
    return config_path


def _write_capture_fixture(output_dir: Path, fixture: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for work_item in fixture["work_items"]:
        page = fixture["captured_pages"][work_item["source_name"]]
        capture_path = output_dir / work_item["capture_file"]
        capture_path.write_text(json.dumps(page), encoding="utf-8")
        items.append(
            {
                **work_item,
                "capture_sha256": hashlib.sha256(capture_path.read_bytes()).hexdigest(),
                "source_metadata": {
                    **work_item.get("source_metadata", {}),
                    "source_page_fixture_file": capture_path.name,
                },
            }
        )
    # The precollected source exists here to exercise one outcome label, so it
    # records a real mention. A weekly full-list source that records nothing is
    # its own named failure now (#464), and that failure does not belong in a
    # fixture built to count outcomes.
    (output_dir / fixture["precollected_source"]["recorded_mentions_file"]).write_text(
        json.dumps({"mentions": [fixture["precollected_mention"]]}), encoding="utf-8"
    )
    return items


def _write_extraction_fixture(output_dir: Path, item: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    extraction_path = output_dir / item["expected_extraction_file"]
    extraction_path.write_text(
        json.dumps(
            {
                "source_name": item["source_name"],
                "source_url": item["source_url"],
                "operation_id": item.get("operation_id", ""),
                "capture_sha256": item["capture_sha256"],
                "entries": entries,
            }
        ),
        encoding="utf-8",
    )


class _FixtureCapture:
    def __init__(self, fixture: dict[str, Any]) -> None:
        self._fixture = fixture

    def prepare(self, _config_path: Path, output_dir: Path, *, live: bool = False, effective_date: str | None = None):
        items = _write_capture_fixture(output_dir, self._fixture)
        worklist = {
            "run_date": effective_date or self._fixture["run_date"],
            "work_items": items,
            "precollected_sources": [self._fixture["precollected_source"]],
        }
        (output_dir / "codex-extraction-worklist.json").write_text(json.dumps(worklist), encoding="utf-8")
        (output_dir / "source-runs.json").write_text(json.dumps(self._fixture["source_runs"]), encoding="utf-8")
        return {
            "returncode": 0,
            "duration_seconds": 0.0,
            "runtime_budget_seconds": 150,
            "source_runs": self._fixture["source_runs"],
        }


class _FixtureCodex:
    def __init__(self, fixture: dict[str, Any]) -> None:
        self._fixture = fixture

    def fulfill(self, output_dir: Path, *, extraction_budget_seconds: float | None = None):
        worklist = json.loads((output_dir / "codex-extraction-worklist.json").read_text(encoding="utf-8"))
        entries_by_source = self._fixture["extraction_entries_by_source"]
        for item in worklist["work_items"]:
            _write_extraction_fixture(output_dir, item, entries_by_source[item["source_name"]])
        return {
            "status": "ok",
            "ready_items": len(worklist["work_items"]),
            "total_items": len(worklist["work_items"]),
            "duration_seconds": 0.0,
            "runtime_budget_seconds": 1,
            "source_timings": [],
        }


def _run_fixture(
    tmp_path: Path, fixture: dict[str, Any] | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    fixture = fixture or _load_fixture()
    config_path = _write_config(tmp_path)
    output_dir = tmp_path / "weekly-output"
    adapters = deterministic_adapters()
    adapters.capture = _FixtureCapture(fixture)
    adapters.codex = _FixtureCodex(fixture)
    adapters.clock = type("FixedClock", (), {"today": lambda _self: _RUN_DATE})()

    result = run_weekly_workflow(config_path, output_dir, adapters=adapters)
    assert result.outcome.value.startswith("completed/")
    summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
    pipeline_source_runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
    workflow_log = json.loads((output_dir / "workflow-log.json").read_text(encoding="utf-8"))
    capture_entry = next(
        entry
        for entry in workflow_log
        if entry.get("stage") == "capture"
        and isinstance(entry.get("detail"), dict)
        and isinstance(entry["detail"].get("source_runs"), list)
    )
    assert capture_entry["detail"]["source_runs"] == fixture["source_runs"]
    return summary, capture_entry["detail"]["source_runs"], pipeline_source_runs


def test_shape_source_outcomes_report_two_failures_and_one_skip(tmp_path: Path) -> None:
    summary, capture_ledger, pipeline_ledger = _run_fixture(tmp_path)
    expected = _expected_source_outcome_counts(capture_ledger, pipeline_ledger)

    assert not [run for run in pipeline_ledger if run.get("status") == "failed"]
    assert expected["failed"] == 2
    assert expected["skipped"] == 1
    assert summary["capture_source_outcome_counts"] == dict(expected)
    assert summary["failed_source_count"] == 2
    assert summary["skipped_source_count"] == 1
    for outcome, field in {
        "inactive": "inactive_source_count",
        "skipped": "skipped_source_count",
        "ignored": "ignored_source_count",
        "failed": "failed_source_count",
        "precollected": "precollected_source_count",
        "codex-captured": "codex_captured_source_count",
        "unknown": "unknown_source_count",
    }.items():
        assert summary[field] == expected[outcome]

    assert "2 configured source failed" in summary["major_warnings"]
    assert "1 configured source skipped" in summary["major_warnings"]
    assert any("unknown" in warning for warning in summary["major_warnings"])
    assert not any("inactive" in warning for warning in summary["major_warnings"])
    assert not any("codex-captured" in warning for warning in summary["major_warnings"])


def test_mixed_stage_counts_capture_and_later_pipeline_failures(tmp_path: Path) -> None:
    summary, capture_ledger, pipeline_ledger = _run_fixture(tmp_path, _mixed_stage_fixture(_load_fixture()))
    expected = _expected_source_outcome_counts(capture_ledger, pipeline_ledger)
    capture_failures = [run for run in capture_ledger if run.get("status") == "failed"]
    later_pipeline_failures = [run for run in pipeline_ledger if run.get("status") == "failed"]

    assert len(capture_failures) == 1
    assert capture_failures[0]["source_name"] == "Failed Capture One"
    assert capture_failures[0]["status"] == "failed"
    assert len(later_pipeline_failures) == 1
    assert later_pipeline_failures[0]["source_name"] == "Pipeline Failure Source"
    assert later_pipeline_failures[0]["status"] == "failed"
    assert expected["failed"] == 2
    assert summary["failed_source_count"] == 2
    assert summary["failed_source_count"] == expected["failed"]
    assert "2 configured source failed" in summary["major_warnings"]


def test_both_major_warning_calls_use_the_same_derived_counts(tmp_path: Path, monkeypatch) -> None:
    from autoscout import pipeline as pipeline_module

    calls: list[tuple[int, int, int]] = []
    original = pipeline_module.major_warnings

    def record_call(*args, **kwargs):
        calls.append(tuple(args[:3]))
        return original(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "major_warnings", record_call)
    _summary, capture_ledger, pipeline_ledger = _run_fixture(tmp_path)
    expected = _expected_source_outcome_counts(capture_ledger, pipeline_ledger)

    assert len(calls) >= 2
    assert calls[-2:] == [(expected["skipped"], expected["failed"], expected["ignored"])] * 2


def test_unknown_outcome_is_not_classified_as_success(tmp_path: Path) -> None:
    summary, capture_ledger, pipeline_ledger = _run_fixture(tmp_path)
    expected = _expected_source_outcome_counts(capture_ledger, pipeline_ledger)

    assert expected["unknown"] > 0
    assert summary["unknown_source_count"] == expected["unknown"]
    assert any("unknown" in warning for warning in summary["major_warnings"])


def test_unreadable_capture_ledger_is_reported_not_silent(tmp_path: Path, monkeypatch) -> None:
    """A run that loses its capture ledger must not look like a clean run.

    Without the ledger the counts fall back to the pipeline ledger, which is
    the pre-#473 under-reporting behaviour.  That degraded state has to be
    visible in the summary, so a reader can tell an accurate zero from an
    unknown zero.
    """
    from autoscout import pipeline as pipeline_module

    monkeypatch.setattr(
        pipeline_module, "capture_ledger_from_workflow_log", lambda *_args, **_kwargs: (None, "incomplete")
    )
    summary, _capture_ledger, _pipeline_ledger = _run_fixture(tmp_path)

    assert summary["capture_ledger_status"] == "incomplete"
    assert "capture ledger unreadable; source outcome counts are incomplete" in summary["major_warnings"]
    # The under-report is the point: the fallback loses a capture failure,
    # which is exactly why the run must announce that it is incomplete.
    assert summary["failed_source_count"] < 2


def test_absent_capture_stage_is_not_reported_as_degraded(tmp_path: Path, monkeypatch) -> None:
    """A run with no capture stage is not degraded and must stay quiet.

    Its pipeline ledger is already the complete picture, so warning about a
    missing capture ledger would be noise on every non-replay run.
    """
    from autoscout import pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "capture_ledger_from_workflow_log", lambda *_args, **_kwargs: (None, "absent"))
    summary, _capture_ledger, _pipeline_ledger = _run_fixture(tmp_path)

    assert summary["capture_ledger_status"] == "absent"
    assert not any("capture ledger" in warning for warning in summary["major_warnings"])


def test_present_capture_ledger_reports_present_and_warns_nothing(tmp_path: Path) -> None:
    summary, _capture_ledger, _pipeline_ledger = _run_fixture(tmp_path)

    assert summary["capture_ledger_status"] == "present"
    assert not any("capture ledger" in warning for warning in summary["major_warnings"])


def _write_log(tmp_path: Path, payload: object) -> Path:
    (tmp_path / "workflow-log.json").write_text(json.dumps(payload), encoding="utf-8")
    return tmp_path


def test_capture_ledger_status_classifies_all_three_states(tmp_path: Path) -> None:
    """The three states must be distinguishable, not collapsed into one None.

    ``absent`` and ``incomplete`` both yield no ledger, but only ``incomplete``
    means a capture stage ran and its record was lost.  Collapsing them would
    either hide a degraded run or warn on every run that never captured.
    """
    from autoscout.reporting import capture_ledger_from_workflow_log

    runs = [{"source_name": "One", "status": "failed"}]
    entry = {"stage": "capture", "operation_id": "op-1", "detail": {"source_runs": runs}}

    # No log file at all: this run never captured.
    assert capture_ledger_from_workflow_log(tmp_path / "missing") == (None, "absent")

    # A log with no capture stage: still nothing was captured.
    assert capture_ledger_from_workflow_log(_write_log(tmp_path, [{"stage": "replay"}])) == (None, "absent")

    # A capture stage for a different operation is not this run's ledger.
    assert capture_ledger_from_workflow_log(_write_log(tmp_path, [entry]), "op-2") == (None, "absent")

    # A capture stage whose ledger is unusable is a degraded run.
    assert capture_ledger_from_workflow_log(
        _write_log(tmp_path, [{"stage": "capture", "operation_id": "op-1", "detail": {}}]), "op-1"
    ) == (None, "incomplete")

    # A log that is not a list at all cannot be trusted to be complete.
    assert capture_ledger_from_workflow_log(_write_log(tmp_path, {"stage": "capture"})) == (None, "incomplete")

    # The usable ledger, matched by operation id and unfiltered.
    assert capture_ledger_from_workflow_log(_write_log(tmp_path, [entry]), "op-1") == (runs, "present")
    assert capture_ledger_from_workflow_log(_write_log(tmp_path, [entry])) == (runs, "present")
