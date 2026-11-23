"""Focused tests for the public observed extraction seam in issue #348."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from autoscout.codex_recovery import CodexCliExtractionAgent
from autoscout.codex_recovery import ExtractionAttempt
from autoscout.codex_recovery import ExtractionStatus
from autoscout.observed_extraction import observe_extraction


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _item_and_capture(tmp_path: Path) -> tuple[dict[str, str], Path]:
    capture = tmp_path / "capture.json"
    capture.write_text(
        json.dumps({"source_name": "Source", "source_url": "https://example.test/source", "html": ""}),
        encoding="utf-8",
    )
    item = {
        "source_name": "Source",
        "source_url": "https://example.test/source",
        "operation_id": "operation",
        "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
    }
    return item, capture


def test_issue_348_valid_observation_has_phase_metrics_and_does_not_publish(tmp_path: Path) -> None:
    item, capture = _item_and_capture(tmp_path)
    staging = tmp_path / ".unique-staging.json"
    clock = _Clock()

    class Agent:
        def extract_with_timeout(self, work_item, capture_path, artifact_path, *, timeout_seconds):
            clock.advance(3)
            artifact_path.write_text(
                json.dumps(
                    {
                        **{
                            key: work_item[key]
                            for key in ("source_name", "source_url", "operation_id", "capture_sha256")
                        },
                        "entries": [],
                    }
                ),
                encoding="utf-8",
            )
            return ExtractionAttempt(ExtractionStatus.PRODUCED, False, 0)

    result = observe_extraction(item, capture, staging, timeout_seconds=600, clock=clock, agent=Agent())

    assert result.final_status == "validated"
    assert result.diagnostic_class == "validated"
    assert result.capture_bytes == capture.stat().st_size
    assert result.capture_unicode_chars is not None
    assert result.generated_prompt_chars is not None
    assert result.output_bytes is not None
    assert result.raw_extracted_count == 0
    assert result.validated_count == 0
    assert result.subprocess_duration_seconds == 3
    assert result.validation_duration_seconds is not None
    assert result.total_duration_seconds is not None
    assert staging.exists()


def test_issue_348_timeout_has_separate_diagnostic_and_null_validation_fields(tmp_path: Path) -> None:
    item, capture = _item_and_capture(tmp_path)

    class Agent:
        def extract_with_timeout(self, work_item, capture_path, artifact_path, *, timeout_seconds):
            return ExtractionAttempt(ExtractionStatus.TIMEOUT, False, diagnostic="codex_timeout")

    result = observe_extraction(item, capture, tmp_path / ".staging.json", timeout_seconds=600, agent=Agent())

    assert result.final_status == "timeout"
    assert result.diagnostic_class == "codex_timeout"
    assert result.raw_extracted_count is None
    assert result.validated_count is None
    assert result.validation_duration_seconds is None


def test_issue_348_invalid_capture_is_rejected_before_agent(tmp_path: Path) -> None:
    item, capture = _item_and_capture(tmp_path)
    capture.write_text("not json", encoding="utf-8")
    calls: list[str] = []

    class Agent:
        def extract(self, work_item, capture_path, artifact_path):
            calls.append("called")
            raise AssertionError("invalid input must not reach the agent")

    result = observe_extraction(item, capture, tmp_path / ".staging.json", timeout_seconds=600, agent=Agent())

    assert result.final_status == "input_invalid"
    assert result.diagnostic_class == "capture_malformed_json"
    assert calls == []


def test_issue_348_submission_and_child_telemetry_define_phase_boundaries(tmp_path: Path) -> None:
    item, capture = _item_and_capture(tmp_path)
    clock = _Clock()
    clock.advance(8)
    submitted_at = 0.0

    class Agent:
        def extract_with_timeout(self, work_item, capture_path, artifact_path, *, timeout_seconds):
            child_started = clock()
            clock.advance(3)
            child_finished = clock()
            artifact_path.write_text(
                json.dumps(
                    {
                        **{
                            key: work_item[key]
                            for key in ("source_name", "source_url", "operation_id", "capture_sha256")
                        },
                        "entries": [],
                    }
                ),
                encoding="utf-8",
            )
            return ExtractionAttempt(
                ExtractionStatus.PRODUCED,
                False,
                0,
                subprocess_duration_seconds=3,
                subprocess_started_at=child_started,
                subprocess_finished_at=child_finished,
            )

    result = observe_extraction(
        item,
        capture,
        tmp_path / ".timed-staging.json",
        timeout_seconds=600,
        clock=clock,
        agent=Agent(),
        submitted_at=submitted_at,
    )

    assert result.queue_wait_seconds == 8
    assert result.subprocess_duration_seconds == 3
    assert result.total_duration_seconds == 11


def test_issue_348_real_agent_reports_child_start_and_bounded_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    item, capture = _item_and_capture(tmp_path)
    clock = _Clock()

    class Process:
        returncode = 0
        pid = 1234

        def __init__(self, command, **kwargs):
            response_path = Path(command[command.index("--output-last-message") + 1])
            response_path.write_text(json.dumps({"entries": []}), encoding="utf-8")

        def poll(self):
            clock.advance(4)
            return 0

        def wait(self, timeout=None):
            clock.advance(4)
            return 0

    monkeypatch.setattr(subprocess, "Popen", Process)
    result = CodexCliExtractionAgent(timeout_seconds=10, clock=clock).extract(
        item, capture, tmp_path / ".agent-stage.json"
    )

    assert result.status is ExtractionStatus.PRODUCED
    assert result.subprocess_started_at == 0
    assert result.subprocess_finished_at == 4
    assert result.subprocess_duration_seconds == 4


def test_issue_348_real_agent_oserror_has_no_child_phase_metrics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    item, capture = _item_and_capture(tmp_path)
    clock = _Clock()

    def raise_oserror(command, **kwargs):
        raise OSError("codex unavailable")

    monkeypatch.setattr(subprocess, "Popen", raise_oserror)
    result = observe_extraction(
        item,
        capture,
        tmp_path / ".oserror-staging.json",
        timeout_seconds=10,
        clock=clock,
        agent=CodexCliExtractionAgent(timeout_seconds=10, clock=clock),
    )

    assert result.final_status == "unavailable"
    assert result.diagnostic_class == "command_unavailable"
    assert result.queue_wait_seconds is None
    assert result.subprocess_duration_seconds is None
    assert result.validation_duration_seconds is None
    assert result.total_duration_seconds == 0


def test_issue_348_maps_remaining_statuses_to_public_contract(tmp_path: Path) -> None:
    item, capture = _item_and_capture(tmp_path)
    cases = [
        (ExtractionStatus.INVALID_OUTPUT, "invalid_output", "invalid_output"),
        (ExtractionStatus.COMMAND_FAILED, "command_failed", "command_failed_permission"),
        (ExtractionStatus.UNAVAILABLE, "unavailable", "command_unavailable"),
    ]
    for index, (status, expected_status, expected_class) in enumerate(cases):

        class Agent:
            def extract_with_timeout(self, work_item, capture_path, artifact_path, *, timeout_seconds, status=status):
                return ExtractionAttempt(status, False, diagnostic="permission denied")

        result = observe_extraction(
            item,
            capture,
            tmp_path / f".status-{index}.json",
            timeout_seconds=600,
            agent=Agent(),
        )
        assert result.final_status == expected_status
        assert result.diagnostic_class == expected_class


def test_issue_348_capture_digest_mismatch_is_rejected_before_agent(tmp_path: Path) -> None:
    item, capture = _item_and_capture(tmp_path)
    item["capture_sha256"] = "0" * 64

    class Agent:
        def extract_with_timeout(self, work_item, capture_path, artifact_path, *, timeout_seconds):
            raise AssertionError("a capture that fails its digest must not reach the agent")

    result = observe_extraction(item, capture, tmp_path / ".staging.json", timeout_seconds=600, agent=Agent())

    assert result.final_status == "input_invalid"
    assert result.diagnostic_class == "capture_digest_mismatch"


def test_issue_348_expired_deadline_exhausts_budget_without_agent_call(tmp_path: Path) -> None:
    item, capture = _item_and_capture(tmp_path)
    clock = _Clock()
    clock.advance(100)

    class Agent:
        def extract_with_timeout(self, work_item, capture_path, artifact_path, *, timeout_seconds):
            raise AssertionError("an expired deadline must not start the agent")

    result = observe_extraction(
        item,
        capture,
        tmp_path / ".staging.json",
        timeout_seconds=600,
        deadline=90,
        submitted_at=95,
        clock=clock,
        agent=Agent(),
    )

    assert result.final_status == "budget_exhausted"
    assert result.diagnostic_class == "portfolio_budget_exhausted"
    assert result.boundary_supplier == "portfolio_deadline"
    assert result.scheduled_before_budget_exhaustion is False


def test_issue_348_remaining_deadline_bounds_the_agent_timeout(tmp_path: Path) -> None:
    item, capture = _item_and_capture(tmp_path)
    clock = _Clock()
    offered: list[float] = []

    class Agent:
        def extract_with_timeout(self, work_item, capture_path, artifact_path, *, timeout_seconds):
            offered.append(timeout_seconds)
            return ExtractionAttempt(ExtractionStatus.TIMEOUT, False, diagnostic="codex_timeout")

    observe_extraction(
        item, capture, tmp_path / ".staging.json", timeout_seconds=600, deadline=5, clock=clock, agent=Agent()
    )

    assert offered == [5.0]


def test_issue_348_zero_timeout_exhausts_budget_without_agent_call(tmp_path: Path) -> None:
    item, capture = _item_and_capture(tmp_path)

    class Agent:
        def extract_with_timeout(self, work_item, capture_path, artifact_path, *, timeout_seconds):
            raise AssertionError("a zero timeout must not start the agent")

    result = observe_extraction(item, capture, tmp_path / ".staging.json", timeout_seconds=0, agent=Agent())

    assert result.final_status == "budget_exhausted"
    assert result.diagnostic_class == "portfolio_budget_exhausted"
