"""Deterministic scheduler, calibration, and retry-policy tests for #357-#360."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from autoscout.codex_recovery import ExtractionAttempt
from autoscout.codex_recovery import ExtractionStatus
from autoscout.codex_recovery import ProductionCodexAdapter


def _worklist(root: Path, count: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for index in range(count):
        source_url = f"https://example.test/source-{index}"
        capture = root / f"capture-{index}.json"
        capture.write_text(
            json.dumps({"source_name": f"Source {index}", "source_url": source_url, "html": "page"}),
            encoding="utf-8",
        )
        items.append(
            {
                "source_name": f"Source {index}",
                "source_url": source_url,
                "capture_file": capture.name,
                "expected_extraction_file": f"result-{index}.json",
                "operation_id": "operation",
                "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
            }
        )
    (root / "codex-extraction-worklist.json").write_text(json.dumps({"work_items": items}), encoding="utf-8")
    return items


def _payload(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_name": item["source_name"],
        "source_url": item["source_url"],
        "operation_id": item["operation_id"],
        "capture_sha256": item["capture_sha256"],
        "entries": [],
    }


def test_issue_357_shared_round_returns_worklist_order_and_real_slots(tmp_path: Path) -> None:
    items = _worklist(tmp_path, 4)
    for worker_count in (1, 2, 3):
        root = tmp_path / f"workers-{worker_count}"
        root.mkdir()
        local_items = _worklist(root, len(items))
        calls: list[str] = []

        class Agent:
            def extract_with_timeout(
                self,
                item,
                capture_path,
                artifact_path,
                *,
                timeout_seconds,
                call_log=calls,
            ):
                call_log.append(item["source_name"])
                artifact_path.write_text(json.dumps(_payload(item)), encoding="utf-8")
                return ExtractionAttempt(ExtractionStatus.PRODUCED, False, 0)

        result = ProductionCodexAdapter(
            Agent(),
            runtime_budget_seconds=100,
            per_attempt_timeout_seconds=10,
            max_attempts=1,
            max_workers=worker_count,
        ).fulfill(root)

        assert result["status"] == "ok"
        assert [timing["source_name"] for timing in result["source_timings"]] == [
            item["source_name"] for item in local_items
        ]
        assert [timing["submission_sequence"] for timing in result["source_timings"]] == [1, 2, 3, 4]
        assert all(1 <= timing["worker_slot"] <= worker_count for timing in result["source_timings"])
        assert sorted(calls) == [item["source_name"] for item in local_items]


def test_issue_357_portfolio_queue_is_budget_exhausted_without_agent_call(tmp_path: Path) -> None:
    _worklist(tmp_path, 2)

    class Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

        def advance(self, seconds: float) -> None:
            self.value += seconds

    clock = Clock()
    calls: list[str] = []

    class Agent:
        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            calls.append(item["source_name"])
            clock.advance(1.0)
            return ExtractionAttempt(ExtractionStatus.COMMAND_FAILED, False, diagnostic="transient")

    result = ProductionCodexAdapter(
        Agent(),
        monotonic=clock,
        sleeper=clock.advance,
        runtime_budget_seconds=1.0,
        per_attempt_timeout_seconds=10.0,
        max_attempts=1,
        max_workers=1,
    ).fulfill(tmp_path)

    assert calls == ["Source 0"]
    queued = [item for item in result["diagnostics"] if item["source_name"] == "Source 1"]
    assert len(queued) == 1
    assert queued[0]["status"] == "budget_exhausted"
    assert queued[0]["diagnostic"] == "portfolio_budget_exhausted"
    assert queued[0]["started"] is False
    assert queued[0]["worker_slot"] is None
    assert not (tmp_path / "result-1.json").exists()
    assert not (tmp_path / "codex-workflow-resume.json").exists()


def test_issue_358_first_round_completes_before_retry_round_and_non_retryable_failures_stop(tmp_path: Path) -> None:
    items = _worklist(tmp_path, 3)
    calls: list[str] = []
    attempts: dict[str, int] = {}

    class RetryAgent:
        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            source_name = item["source_name"]
            calls.append(source_name)
            attempts[source_name] = attempts.get(source_name, 0) + 1
            if attempts[source_name] == 1:
                return ExtractionAttempt(ExtractionStatus.COMMAND_FAILED, True, diagnostic="transient")
            artifact_path.write_text(json.dumps(_payload(item)), encoding="utf-8")
            return ExtractionAttempt(ExtractionStatus.PRODUCED, False, 0)

    result = ProductionCodexAdapter(
        RetryAgent(),
        runtime_budget_seconds=1000,
        per_attempt_timeout_seconds=10,
        max_attempts=2,
        max_workers=1,
        sleeper=lambda seconds: None,
    ).fulfill(tmp_path)

    assert calls == [item["source_name"] for item in items] * 2
    assert result["ready_items"] == 3
    # Three first attempts and three retries: only the retries count (Issue #1176).
    assert result["retries_used"] == 3

    non_retry_root = tmp_path / "non-retry"
    non_retry_root.mkdir()
    non_retry_items = _worklist(non_retry_root, 2)
    non_retry_calls: list[str] = []

    class NonRetryAgent:
        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            non_retry_calls.append(item["source_name"])
            status = ExtractionStatus.TIMEOUT if item["source_name"] == "Source 0" else ExtractionStatus.COMMAND_FAILED
            diagnostic = "codex_timeout" if status is ExtractionStatus.TIMEOUT else "structural failure"
            return ExtractionAttempt(status, True, diagnostic=diagnostic)

    non_retry_result = ProductionCodexAdapter(
        NonRetryAgent(),
        runtime_budget_seconds=1000,
        per_attempt_timeout_seconds=10,
        max_attempts=2,
        max_workers=1,
        sleeper=lambda seconds: None,
        retry_headroom_seconds=0,
    ).fulfill(non_retry_root)
    assert non_retry_calls == [item["source_name"] for item in non_retry_items]
    # Two first attempts and no retry (Issue #1176: retries_used once counted first attempts too).
    assert non_retry_result["retries_used"] == 0


def _run_exact_reserve_case(root: Path, reserve: float) -> tuple[dict[str, Any], list[str]]:
    item = _worklist(root, 1)[0]
    calls: list[str] = []

    class Clock:
        def __call__(self) -> float:
            return 0.0

    clock = Clock()

    class Agent:
        def extract_with_timeout(self, current, capture_path, artifact_path, *, timeout_seconds):
            calls.append(current["source_name"])
            if len(calls) == 2:
                artifact_path.write_text(json.dumps(_payload(current)), encoding="utf-8")
                return ExtractionAttempt(ExtractionStatus.PRODUCED, False, 0)
            return ExtractionAttempt(ExtractionStatus.COMMAND_FAILED, True, diagnostic="transient")

    result = ProductionCodexAdapter(
        Agent(),
        monotonic=clock,
        runtime_budget_seconds=200,
        per_attempt_timeout_seconds=20,
        max_attempts=2,
        max_workers=1,
        sleeper=lambda seconds: None,
        retry_headroom_seconds=reserve,
    ).fulfill(root)
    assert item["source_name"] == "Source 0"
    return result, calls


def test_issue_358_exact_reserve_equality_admits_and_below_equality_refuses_without_mutation(tmp_path: Path) -> None:
    equal_root = tmp_path / "equal"
    equal_root.mkdir()
    equal_result, equal_calls = _run_exact_reserve_case(equal_root, 180.0)
    assert equal_calls == ["Source 0", "Source 0"]
    assert equal_result["retry_admission"]["admitted_count"] == 1
    assert (equal_root / "result-0.json").exists()

    below_root = tmp_path / "below"
    below_root.mkdir()
    below_result, below_calls = _run_exact_reserve_case(below_root, 180.01)
    assert below_calls == ["Source 0"]
    assert below_result["retry_admission"]["refused_sources"] == ["Source 0"]
    assert not (below_root / "result-0.json").exists()
    assert not (below_root / "codex-workflow-resume.json").exists()
    refusal = [item for item in below_result["diagnostics"] if item.get("status") == "retry_not_admitted_budget"]
    assert len(refusal) == 1
    assert refusal[0]["started"] is False


def _retrying_agent(retry_sources: set[str]):
    attempts: dict[str, int] = {}

    class Agent:
        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            name = item["source_name"]
            attempts[name] = attempts.get(name, 0) + 1
            if name in retry_sources and attempts[name] == 1:
                return ExtractionAttempt(ExtractionStatus.COMMAND_FAILED, True, diagnostic="transient")
            artifact_path.write_text(json.dumps(_payload(item)), encoding="utf-8")
            return ExtractionAttempt(ExtractionStatus.PRODUCED, False, 0)

    return Agent()


@pytest.mark.parametrize("budget", [1000, 3600], ids=["single_pass", "segmented"])
def test_issue_1176_retries_used_counts_only_attempts_after_a_sources_first(tmp_path: Path, budget: int) -> None:
    """Three sources, one retried: ``retries_used`` is 1, and 0 when none is retried."""
    for retry_sources, expected in (({"Source 1"}, 1), (set(), 0)):
        root = tmp_path / f"retry-{expected}"
        root.mkdir()
        items = _worklist(root, 3)
        # A source declared to stand empty is not retried for an empty list (#700),
        # so the only retry is the one the agent forces.
        for item in items:
            item["source_metadata"] = {"may_report_no_entries": True}
        (root / "codex-extraction-worklist.json").write_text(json.dumps({"work_items": items}), encoding="utf-8")
        result = ProductionCodexAdapter(
            _retrying_agent(retry_sources),
            runtime_budget_seconds=budget,
            per_attempt_timeout_seconds=10,
            max_attempts=2,
            max_workers=1,
            sleeper=lambda seconds: None,
            retry_headroom_seconds=0,
        ).fulfill(root)
        assert result["ready_items"] == 3
        assert result["retries_used"] == expected
