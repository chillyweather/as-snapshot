"""Regression coverage for issue #416 two-sided offered-timeout bounds.

The offered extraction timeout is the per-attempt ceiling reduced only by the
time left against the portfolio deadline::

    offered = min(per_attempt_ceiling, portfolio_deadline - attempt_started)

A one-sided upper bound stays green while a defect collapses the offered
timeout toward zero, so both binding constraints are pinned exactly here.
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


class SerialFakeClock:
    """A monotonic clock advanced only by the work it is told about."""

    def __init__(self) -> None:
        self.value = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.value

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.value += seconds


def _write_worklist(tmp_path: Path, count: int) -> None:
    items = []
    for index in range(count):
        capture = tmp_path / f"capture-{index}.json"
        source_url = f"https://example.test/issue-416-{index}"
        capture.write_text(json.dumps({"source_url": source_url, "html": "page"}), encoding="utf-8")
        items.append(
            {
                "source_name": f"Source-{index}",
                "source_url": source_url,
                "capture_file": capture.name,
                "expected_extraction_file": f"result-{index}.json",
                "operation_id": f"op-{index}",
                "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
                # Issue #700: this fixture's agent deliberately writes an
                # empty artifact to stand for "an attempt completed", which
                # is exactly what the empty-extraction retry now reattempts.
                # Declaring the source as one that may report no entries
                # states that intent, so this suite keeps measuring timeout
                # accounting rather than the retry.
                "source_metadata": {"may_report_no_entries": True},
            }
        )
    (tmp_path / "codex-extraction-worklist.json").write_text(json.dumps({"work_items": items}), encoding="utf-8")


def _run(tmp_path: Path, durations: list[float], **adapter_kwargs: Any) -> tuple[dict[str, Any], list[float]]:
    """Run one bounded portfolio serially and return the offered timeouts in order."""
    _write_worklist(tmp_path, len(durations))
    clock = SerialFakeClock()
    offered: list[float] = []

    class RecordingAgent:
        def extract(self, item, capture_path, artifact_path):
            raise AssertionError("deadline-aware path expected")

        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            offered.append(timeout_seconds)
            index = int(item["source_name"].rsplit("-", 1)[-1])
            clock.advance(durations[index])
            artifact_path.write_text(
                json.dumps(
                    {
                        "source_name": item["source_name"],
                        "source_url": item["source_url"],
                        "operation_id": item["operation_id"],
                        "capture_sha256": item["capture_sha256"],
                        "entries": [],
                    }
                ),
                encoding="utf-8",
            )
            return ExtractionAttempt(ExtractionStatus.PRODUCED, True, 0)

    result = ProductionCodexAdapter(
        RecordingAgent(),
        monotonic=clock,
        sleeper=lambda seconds: clock.advance(seconds),
        max_workers=1,
        retry_headroom_seconds=0.0,
        **adapter_kwargs,
    ).fulfill(tmp_path)
    return result, offered


def test_remaining_segment_time_binds_the_offered_timeout(tmp_path: Path) -> None:
    """When the budget runs shorter than the ceiling, the remaining time is offered."""
    durations = [500.0, 100.0]

    result, offered = _run(
        tmp_path,
        durations,
        runtime_budget_seconds=1000.0,
        per_attempt_timeout_seconds=600.0,
    )

    assert result["status"] == "ok"
    assert result["failed_items"] == 0
    # First attempt starts at 0, so the 600-second ceiling binds.
    # Second attempt starts at 500, so the remaining 500 seconds binds instead.
    assert offered == [600.0, 500.0]


def test_the_per_attempt_ceiling_binds_the_offered_timeout(tmp_path: Path) -> None:
    """When the budget is ample, the ceiling is offered and never exceeded."""
    durations = [100.0, 100.0]

    result, offered = _run(
        tmp_path,
        durations,
        runtime_budget_seconds=10_000.0,
        per_attempt_timeout_seconds=300.0,
    )

    assert result["status"] == "ok"
    assert result["failed_items"] == 0
    assert offered == [300.0, 300.0]


def test_a_collapsed_offered_timeout_fails_the_two_sided_bound() -> None:
    """The guard added for #416 must reject what the one-sided guard accepted."""
    ceiling = 600.0
    minimum = 500.0
    collapsed = [1.0, 1.0]

    # The bound the test carried before #416.
    assert all(timeout <= ceiling for timeout in collapsed)

    # The two-sided bound this issue restores.
    assert not all(minimum <= timeout <= ceiling for timeout in collapsed)
