"""Issue #732: retries are admitted in waves, not refused in one pass.

On 2026-09-26 retry admission costed every retry at the full 1800-second
per-attempt timeout. Nine of seventeen retries fitted, the other eight were
refused for good, and the stage then ended with about 95 minutes of budget
unused, because the nine admitted retries took 30-77 seconds each.

A retry that does not fit is now deferred and costed again once the admitted
wave has finished. The deadline guarantee is unchanged: every admitted retry
is still costed at the full timeout.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

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


class _FailFirstAgent:
    """Fails every first attempt transiently, then produces a valid artifact."""

    def __init__(self, clock: _Clock | None = None, retry_cost: float = 0.0, first_cost: float = 0.0) -> None:
        self._clock = clock
        self._retry_cost = retry_cost
        self._first_cost = first_cost
        self._lock = threading.Lock()
        self.attempts: dict[str, int] = {}
        self.calls: list[tuple[str, int]] = []

    def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
        del capture_path, timeout_seconds
        with self._lock:
            number = self.attempts.get(item["source_name"], 0) + 1
            self.attempts[item["source_name"]] = number
            self.calls.append((item["source_name"], number))
            if self._clock is not None:
                self._clock.now += self._first_cost if number == 1 else self._retry_cost
        if number == 1:
            return ExtractionAttempt(ExtractionStatus.COMMAND_FAILED, True, diagnostic="transient")
        artifact_path.write_text(json.dumps(_payload(item)), encoding="utf-8")
        return ExtractionAttempt(ExtractionStatus.PRODUCED, False, 0)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_the_2026_09_26_shape_retries_every_source_in_a_second_wave(tmp_path: Path) -> None:
    # Seventeen retry candidates, three workers, a 1800-second per-attempt
    # timeout and a budget with room for only nine retries costed at that
    # timeout. The retries themselves are fast, as they were on 2026-09-26.
    _worklist(tmp_path, 17)
    agent = _FailFirstAgent()

    result = ProductionCodexAdapter(
        agent,
        runtime_budget_seconds=6000,
        per_attempt_timeout_seconds=1800,
        max_attempts=2,
        max_workers=3,
        sleeper=lambda seconds: None,
    ).fulfill(tmp_path)

    admission = result["retry_admission"]
    assert admission["candidate_count"] == 17
    assert admission["admitted_count"] == 17
    assert admission["refused_count"] == 0
    assert admission["refused_sources"] == []
    # The first wave is exactly what the old single pass admitted.
    assert admission["admission_waves"] == 2
    assert result["ready_items"] == 17
    assert not [item for item in result["diagnostics"] if item.get("status") == "retry_not_admitted_budget"]


def test_deferred_retries_run_when_the_admitted_wave_finishes_early(tmp_path: Path) -> None:
    _worklist(tmp_path, 6)
    clock = _Clock()
    # First attempts cost 10 each (t=60), leaving capacity for four retries
    # costed at the full 10-second timeout. Each real retry costs 1.
    agent = _FailFirstAgent(clock, retry_cost=1.0, first_cost=10.0)

    result = ProductionCodexAdapter(
        agent,
        monotonic=clock,
        runtime_budget_seconds=100,
        per_attempt_timeout_seconds=10,
        max_attempts=2,
        max_workers=1,
        sleeper=lambda seconds: None,
        retry_headroom_seconds=0,
    ).fulfill(tmp_path)

    admission = result["retry_admission"]
    assert admission["admission_waves"] == 2
    assert admission["admitted_count"] == 6
    assert admission["refused_count"] == 0
    assert result["ready_items"] == 6
    assert [number for _name, number in agent.calls].count(2) == 6


def test_waves_never_admit_a_retry_past_the_deadline_minus_headroom(tmp_path: Path) -> None:
    _worklist(tmp_path, 6)
    clock = _Clock()
    # Retries use their full timeout, so after the first wave nothing fits.
    agent = _FailFirstAgent(clock, retry_cost=10.0, first_cost=10.0)

    result = ProductionCodexAdapter(
        agent,
        monotonic=clock,
        runtime_budget_seconds=100,
        per_attempt_timeout_seconds=10,
        max_attempts=2,
        max_workers=1,
        sleeper=lambda seconds: None,
        retry_headroom_seconds=0,
    ).fulfill(tmp_path)

    admission = result["retry_admission"]
    assert admission["admission_waves"] == 1
    assert admission["admitted_count"] == 4
    assert admission["refused_count"] == 2
    assert admission["refused_sources"] == ["Source 4", "Source 5"]
    assert clock.now <= 100
    assert admission["preserved_headroom"] is True
    refusals = [item for item in result["diagnostics"] if item.get("status") == "retry_not_admitted_budget"]
    assert [item["source_name"] for item in refusals] == ["Source 4", "Source 5"]
    assert all(item["started"] is False for item in refusals)
