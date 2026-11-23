"""Regression tests for the three confirmed review defects in PR #362."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from autoscout import extraction_round
from autoscout.bounded_workers import DaemonFuture


def test_issue_362_worker_exception_during_polling_reaches_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wait_started = threading.Event()
    handled: list[BaseException] = []

    original_result = DaemonFuture.result

    def instrumented_result(future: DaemonFuture, timeout: float | None = None) -> Any:
        if timeout == 0.01 and not future.done():
            wait_started.set()
        return original_result(future, timeout)

    monkeypatch.setattr(DaemonFuture, "result", instrumented_result)

    def run(_context: extraction_round.ExtractionRoundContext) -> Any:
        if not wait_started.wait(timeout=1):
            raise AssertionError("scheduler did not enter its progress wait")
        raise ValueError("worker failure")

    def boundary(*_args: Any) -> Any:
        return "boundary"

    def on_exception(_context: extraction_round.ExtractionRoundContext, exc: BaseException) -> Any:
        handled.append(exc)
        return "handled"

    completions = extraction_round.execute_bounded_extraction_round(
        [{}],
        worker_count=1,
        per_attempt_ceiling=10,
        portfolio_deadline=100,
        clock=lambda: 0.0,
        run=run,
        boundary=boundary,
        on_exception=on_exception,
    )

    assert [completion.result for completion in completions] == ["handled"]
    assert len(handled) == 1
    assert isinstance(handled[0], ValueError)


def test_issue_362_portfolio_deadline_expires_started_worker_without_active_phase() -> None:
    class Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = Clock()
    run_entered = threading.Event()
    release_worker = threading.Event()
    late_result_seen = threading.Event()
    late_results: list[Any] = []
    boundary_calls: list[tuple[str, float, str]] = []

    def run(_context: extraction_round.ExtractionRoundContext) -> Any:
        run_entered.set()
        clock.value = 0.5
        if not release_worker.wait(timeout=1):
            raise AssertionError("scheduler did not return the boundary result")
        return "late-result"

    def boundary(
        _context: extraction_round.ExtractionRoundContext,
        stage: str,
        threshold: float,
        supplier: str,
    ) -> Any:
        boundary_calls.append((stage, threshold, supplier))
        return "boundary-result"

    def on_late_result(_context: extraction_round.ExtractionRoundContext, result: Any) -> None:
        late_results.append(result)
        late_result_seen.set()

    try:
        completions = extraction_round.execute_bounded_extraction_round(
            [{}],
            worker_count=1,
            per_attempt_ceiling=0.1,
            portfolio_deadline=0.5,
            clock=clock,
            run=run,
            boundary=boundary,
            on_late_result=on_late_result,
        )
    finally:
        release_worker.set()

    assert run_entered.is_set()
    assert boundary_calls == [("subprocess", 0.5, "portfolio_deadline")]
    assert [completion.result for completion in completions] == ["boundary-result"]
    assert late_result_seen.wait(timeout=1)
    assert late_results == ["late-result"]
