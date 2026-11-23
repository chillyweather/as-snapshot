from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

from autoscout.json_io import write_json
from autoscout.mvp_run_evidence import evidence_from_log
from autoscout.mvp_run_evidence import inspect as inspect_run_evidence
from autoscout.mvp_run_evidence import operation_id
from autoscout.mvp_run_evidence import stamp_entry


def _current_evidence(tmp_path: Path) -> tuple[Path, Path, str]:
    config = tmp_path / "config.json"
    capture = tmp_path / "capture.json"
    capture.write_text('{"html": "<html></html>"}', encoding="utf-8")
    write_json(
        config,
        {
            "sources": [
                {
                    "name": "Source",
                    "family": "award",
                    "url": "https://example.test/source",
                    "active": True,
                    "extraction_method": "codex-extraction-replay",
                }
            ]
        },
    )
    output = tmp_path / "run"
    output.mkdir()
    output_capture = output / "capture.json"
    output_capture.write_bytes(capture.read_bytes())
    run_id = operation_id(config, date(2026, 7, 31))
    write_json(
        output / "codex-extraction-worklist.json",
        {
            "run_date": "2026-07-31",
            "operation_id": run_id,
            "work_items": [
                {
                    "source_name": "Source",
                    "source_family": "award",
                    "source_url": "https://example.test/source",
                    "operation_id": run_id,
                    "capture_file": "capture.json",
                    "capture_sha256": hashlib.sha256(output_capture.read_bytes()).hexdigest(),
                    "expected_extraction_file": "extraction.json",
                }
            ],
            "precollected_sources": [],
        },
    )
    write_json(
        output / "codex-workflow-resume.json",
        {"run_id": run_id, "completed_stages": ["capture"], "validated_artifacts": []},
    )
    write_json(
        output / "source-runs.json",
        [
            {
                "source_name": "Source",
                "family": "award",
                "url": "https://example.test/source",
                "source_check_date": "2026-07-31",
                "status": "codex-captured",
            }
        ],
    )
    return config, output, run_id


def test_operation_evidence_is_current_and_completion_uses_latest_finalize(tmp_path: Path) -> None:
    config, output, run_id = _current_evidence(tmp_path)
    write_json(
        output / "workflow-log.json",
        [
            stamp_entry({"stage": "operation-start", "status": "ok"}, run_id),
            stamp_entry({"stage": "repository-finalize", "status": "failed"}, run_id),
            stamp_entry({"stage": "repository-finalize", "status": "ok"}, run_id),
            stamp_entry({"stage": "terminal", "status": "completed/no-attention"}, run_id),
        ],
    )

    evidence = inspect_run_evidence(output, config, date(2026, 7, 31))

    assert evidence.capture_current is True
    assert evidence.persistence_ok is True
    assert evidence.resumable is False
    assert evidence.latest_repository_finalization is not None
    assert evidence.latest_repository_finalization["status"] == "ok"


def test_legacy_and_changed_identity_evidence_is_fail_closed(tmp_path: Path) -> None:
    config, output, run_id = _current_evidence(tmp_path)
    write_json(output / "workflow-log.json", [{"stage": "terminal", "status": "completed/no-attention"}])
    legacy = inspect_run_evidence(output, config, date(2026, 7, 31))
    assert legacy.legacy_evidence_present is True
    assert legacy.has_completed_outcome is True
    assert legacy.persistence_ok is False
    assert legacy.resumable is False

    changed = json.loads(config.read_text(encoding="utf-8"))
    changed["sources"][0]["url"] = "https://example.test/changed"
    config.write_text(json.dumps(changed), encoding="utf-8")
    changed_evidence = inspect_run_evidence(output, config, date(2026, 7, 31))
    assert changed_evidence.operation_id != run_id
    assert changed_evidence.capture_current is False
    assert changed_evidence.allowlist == frozenset()
    assert changed_evidence.resumable is False


def test_legacy_completed_current_report_remains_diagnosable(tmp_path: Path) -> None:
    config, output, _run_id = _current_evidence(tmp_path)
    write_json(
        output / "workflow-log.json",
        [
            {"stage": "capture", "status": "ok"},
            {"stage": "repository-finalize", "status": "ok"},
            {"stage": "terminal", "status": "completed/no-attention"},
        ],
    )

    evidence = inspect_run_evidence(output, config, date(2026, 7, 31))

    assert evidence.has_completed_outcome is True
    assert evidence.persistence_ok is True
    assert evidence.stamped_current is False
    assert [entry["stage"] for entry in evidence.log] == [
        "capture",
        "repository-finalize",
        "terminal",
    ]


def test_stamped_hard_kill_cannot_inherit_legacy_terminal_or_finalize(tmp_path: Path) -> None:
    config, output, run_id = _current_evidence(tmp_path)
    write_json(
        output / "workflow-log.json",
        [
            {"stage": "repository-finalize", "status": "ok"},
            {"stage": "terminal", "status": "completed/no-attention"},
            stamp_entry({"stage": "operation-start", "status": "ok"}, run_id),
            stamp_entry({"stage": "codex", "status": "ok"}, run_id),
        ],
    )

    evidence = inspect_run_evidence(output, config, date(2026, 7, 31))

    assert evidence.stamped_current is True
    assert [entry["stage"] for entry in evidence.log] == ["operation-start", "codex"]
    assert evidence.has_completed_outcome is False
    assert evidence.latest_repository_finalization is None
    assert evidence.resumable is True


def test_two_stamped_hard_kills_with_distinct_operations_do_not_mix(tmp_path: Path) -> None:
    config, output, run_id = _current_evidence(tmp_path)
    old_id = "older-operation"
    write_json(
        output / "workflow-log.json",
        [
            stamp_entry({"stage": "codex", "status": "failed"}, old_id),
            stamp_entry({"stage": "terminal", "status": "intervention-required"}, old_id),
            stamp_entry({"stage": "operation-start", "status": "ok"}, run_id),
            stamp_entry({"stage": "pipeline", "status": "failed"}, run_id),
        ],
    )

    evidence = inspect_run_evidence(output, config, date(2026, 7, 31))

    assert [entry["stage"] for entry in evidence.log] == ["operation-start", "pipeline"]
    assert evidence.terminal is None
    assert evidence.failed_stage == "pipeline"
    assert evidence.has_terminal_intervention is False


def test_supplied_legacy_result_log_is_interpreted_without_authorization() -> None:
    evidence = evidence_from_log(
        [
            {"stage": "repository-finalize", "status": "failed"},
            {"stage": "repository-finalize", "status": "ok"},
            {"stage": "terminal", "status": "completed/no-attention"},
        ],
        "known-operation",
        "2026-07-31",
        allow_legacy=True,
    )

    assert evidence.persistence_ok is True
    assert evidence.has_completed_outcome is True
    assert evidence.capture_current is False
    assert evidence.allowlist == frozenset()
