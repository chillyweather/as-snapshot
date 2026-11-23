"""A Codex session that stalls for the whole no-progress lease earns one retry (issue #824)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from autoscout.codex_recovery import SEGMENT_BUDGET_SECONDS
from autoscout.codex_recovery import ExtractionAttempt
from autoscout.codex_recovery import ExtractionStatus
from autoscout.codex_recovery import ProductionCodexAdapter
from autoscout.codex_workflow import _extraction_outcomes


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _worklist(tmp_path: Path) -> None:
    capture = tmp_path / "capture.json"
    capture.write_text(json.dumps({"source_url": "https://example.test/list", "html": "page"}), encoding="utf-8")
    item = {
        "source_name": "Stalling list",
        "source_url": "https://example.test/list",
        "capture_file": capture.name,
        "expected_extraction_file": "extraction.json",
        "operation_id": "operation",
        "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
    }
    (tmp_path / "codex-extraction-worklist.json").write_text(json.dumps({"work_items": [item]}), encoding="utf-8")


class _Agent:
    """Return a scripted terminal per attempt; ``valid`` writes a bound, validated artifact."""

    def __init__(self, clock: _Clock, script: list[str], *, expire_before_retry: float = 0.0) -> None:
        self.clock = clock
        self.script = script
        self.calls = 0
        self.rounds = 0
        self.expire_before_retry = expire_before_retry

    def begin_round(self, names: list[str]) -> None:
        self.rounds += 1
        if self.rounds == 2 and self.expire_before_retry:
            self.clock.advance(self.expire_before_retry)

    def extract(self, item, capture_path, artifact_path):
        raise AssertionError("deadline-aware execution expected")

    def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
        outcome = self.script[self.calls]
        self.calls += 1
        self.clock.advance(1.0)
        if outcome == "no_progress":
            return ExtractionAttempt(ExtractionStatus.TIMEOUT, False, diagnostic="codex_no_progress_timeout")
        if outcome == "safety_stop":
            return ExtractionAttempt(ExtractionStatus.TIMEOUT, False, diagnostic="codex_absolute_ceiling")
        artifact_path.write_text(
            json.dumps(
                {
                    **{key: item[key] for key in ("source_name", "source_url", "operation_id", "capture_sha256")},
                    "entries": [],
                }
            ),
            encoding="utf-8",
        )
        return ExtractionAttempt(ExtractionStatus.PRODUCED, True, 0)


def _run(tmp_path: Path, agent: _Agent, clock: _Clock, *, budget: float, per_attempt_timeout: float = 20.0) -> dict:
    adapter = ProductionCodexAdapter(
        agent,
        monotonic=clock,
        sleeper=clock.advance,
        max_workers=1,
        max_attempts=2,
        runtime_budget_seconds=budget,
        per_attempt_timeout_seconds=per_attempt_timeout,
        retry_headroom_seconds=0.0,
    )
    return adapter.fulfill(tmp_path)


@pytest.mark.parametrize("segmented", [False, True])
def test_no_progress_timeout_is_retried_once_and_recovers(tmp_path: Path, segmented: bool) -> None:
    _worklist(tmp_path)
    clock = _Clock()
    agent = _Agent(clock, ["no_progress", "valid"])
    budget = SEGMENT_BUDGET_SECONDS + 100.0 if segmented else 100.0

    metadata = _run(tmp_path, agent, clock, budget=budget)

    assert agent.calls == 2, "a no-progress timeout must get a second, fresh attempt"
    assert metadata["retry_admission"]["admitted_after_no_progress"] == ["Stalling list"]
    outcome = _extraction_outcomes(tmp_path)["Stalling list"]
    assert outcome["terminal_class"] == "validated"
    assert outcome["no_progress_retry"] is True
    assert outcome["no_progress_recovered"] is True
    assert "no_progress_retry_refused" not in outcome


@pytest.mark.parametrize("segmented", [False, True])
def test_a_second_no_progress_timeout_falls_back_without_a_third_attempt(tmp_path: Path, segmented: bool) -> None:
    _worklist(tmp_path)
    clock = _Clock()
    agent = _Agent(clock, ["no_progress", "no_progress", "valid"])
    budget = SEGMENT_BUDGET_SECONDS + 100.0 if segmented else 100.0

    _run(tmp_path, agent, clock, budget=budget)

    assert agent.calls == 2
    outcome = _extraction_outcomes(tmp_path)["Stalling list"]
    assert outcome["terminal_class"] == "no_progress_timeout"
    assert outcome["no_progress_retry"] is True
    assert outcome["no_progress_recovered"] is False


@pytest.mark.parametrize("segmented", [False, True])
def test_no_progress_retry_refused_by_budget_is_reported(tmp_path: Path, segmented: bool) -> None:
    """A retry is still costed at the full per-attempt timeout, so a short budget refuses it."""
    _worklist(tmp_path)
    clock = _Clock()
    agent = _Agent(clock, ["no_progress", "valid"])
    budget = SEGMENT_BUDGET_SECONDS + 100.0 if segmented else 100.0

    # The retry is costed at a per-attempt timeout longer than the whole budget.
    _run(tmp_path, agent, clock, budget=budget, per_attempt_timeout=budget + 1.0)

    assert agent.calls == 1
    outcome = _extraction_outcomes(tmp_path)["Stalling list"]
    assert outcome["terminal_class"] == "no_progress_timeout"
    assert outcome["no_progress_retry_refused"] is True
    assert "no_progress_retry" not in outcome


@pytest.mark.parametrize("segmented", [False, True])
def test_execution_safety_stop_stays_non_retryable(tmp_path: Path, segmented: bool) -> None:
    _worklist(tmp_path)
    clock = _Clock()
    agent = _Agent(clock, ["safety_stop", "valid"])
    budget = SEGMENT_BUDGET_SECONDS + 100.0 if segmented else 100.0

    _run(tmp_path, agent, clock, budget=budget)

    assert agent.calls == 1
    outcome = _extraction_outcomes(tmp_path)["Stalling list"]
    assert outcome["terminal_class"] == "execution_safety_stop"
    assert "no_progress_retry" not in outcome
    assert "no_progress_retry_refused" not in outcome
