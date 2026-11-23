"""Retry reporting follows agent invocation, not scheduler admission."""

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


@pytest.mark.parametrize("segmented", [False, True])
@pytest.mark.parametrize("expire_before_retry", [False, True])
def test_retry_reporting_requires_agent_invocation(tmp_path: Path, segmented: bool, expire_before_retry: bool) -> None:
    """An admitted retry can exhaust its deadline before the worker starts.

    Drive both production loops with valid, capture-bound empty artifacts.
    Advancing time in begin_round models the scheduling delay between
    admission and execution, without mocking the retry or reporting logic.
    """
    budget = SEGMENT_BUDGET_SECONDS + 25.0 if segmented else 25.0
    capture = tmp_path / "capture.json"
    capture.write_text(json.dumps({"source_url": "https://example.test/list", "html": "page"}), encoding="utf-8")
    item = {
        "source_name": "Complete list",
        "source_url": "https://example.test/list",
        "capture_file": capture.name,
        "expected_extraction_file": "extraction.json",
        "operation_id": "operation",
        "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
    }
    (tmp_path / "codex-extraction-worklist.json").write_text(json.dumps({"work_items": [item]}), encoding="utf-8")

    class Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

        def advance(self, seconds: float) -> None:
            self.value += seconds

    clock = Clock()

    class Agent:
        calls = 0
        rounds = 0

        def begin_round(self, names: list[str]) -> None:
            self.rounds += 1
            if self.rounds == 2 and expire_before_retry:
                clock.advance(budget)

        def extract(self, item, capture_path, artifact_path):
            raise AssertionError("deadline-aware execution expected")

        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            self.calls += 1
            clock.advance(1.0)
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

    agent = Agent()
    ProductionCodexAdapter(
        agent,
        monotonic=clock,
        sleeper=clock.advance,
        max_workers=1,
        max_attempts=2,
        runtime_budget_seconds=budget,
        per_attempt_timeout_seconds=20.0,
        retry_headroom_seconds=0.0,
    ).fulfill(tmp_path)

    outcome = _extraction_outcomes(tmp_path)["Complete list"]
    assert agent.rounds == 2, "the retry must reach dispatch after admission"
    if expire_before_retry:
        assert agent.calls == 1
        assert "empty_extraction_retry" not in outcome
        assert "empty_extraction_recovered" not in outcome
        assert outcome["empty_extraction_retry_refused"] is True
    else:
        assert agent.calls == 2
        assert outcome["empty_extraction_retry"] is True
        assert outcome["empty_extraction_recovered"] is False
        assert "empty_extraction_retry_refused" not in outcome
