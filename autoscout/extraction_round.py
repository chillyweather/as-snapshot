"""Shared bounded extraction-round orchestration."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from autoscout.bounded_workers import CancellationToken
from autoscout.bounded_workers import DaemonFuture
from autoscout.bounded_workers import DaemonThreadPool

_PROGRESS_EVENT_TYPES = frozenset({"reasoning", "custom_tool_call", "token_count", "task_started", "task_complete"})


@dataclass
class ExtractionPhaseTelemetry:
    """Thread-safe exact phase boundaries for one extraction attempt."""

    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    attempt_started_at: float | None = None
    agent_invocation_started_at: float | None = None
    subprocess_started_at: float | None = None
    subprocess_finished_at: float | None = None
    validation_started_at: float | None = None
    validation_finished_at: float | None = None
    collection_started_at: float | None = None
    collection_finished_at: float | None = None

    def mark_attempt_started(self, timestamp: float) -> None:
        with self._lock:
            if self.attempt_started_at is None:
                self.attempt_started_at = timestamp

    def mark_agent_invocation_started(self, timestamp: float) -> None:
        with self._lock:
            if self.agent_invocation_started_at is None:
                self.agent_invocation_started_at = timestamp

    def mark_subprocess_started(self, timestamp: float) -> None:
        with self._lock:
            if self.subprocess_started_at is None:
                self.subprocess_started_at = timestamp

    def mark_subprocess_finished(self, timestamp: float) -> None:
        with self._lock:
            if self.subprocess_finished_at is None:
                self.subprocess_finished_at = timestamp

    def mark_subprocess_unavailable(self) -> None:
        with self._lock:
            self.subprocess_started_at = None
            self.subprocess_finished_at = None

    def mark_validation_started(self, timestamp: float) -> None:
        with self._lock:
            if self.validation_started_at is None:
                self.validation_started_at = timestamp

    def mark_validation_finished(self, timestamp: float) -> None:
        with self._lock:
            if self.validation_finished_at is None:
                self.validation_finished_at = timestamp

    def mark_collection_started(self, timestamp: float) -> None:
        with self._lock:
            if self.collection_started_at is None:
                self.collection_started_at = timestamp

    def mark_collection_finished(self, timestamp: float) -> None:
        with self._lock:
            if self.collection_finished_at is None:
                self.collection_finished_at = timestamp

    def snapshot(self) -> dict[str, float | None]:
        with self._lock:
            return {
                "attempt_started_at": self.attempt_started_at,
                "agent_invocation_started_at": self.agent_invocation_started_at,
                "subprocess_started_at": self.subprocess_started_at,
                "subprocess_finished_at": self.subprocess_finished_at,
                "validation_started_at": self.validation_started_at,
                "validation_finished_at": self.validation_finished_at,
                "collection_started_at": self.collection_started_at,
                "collection_finished_at": self.collection_finished_at,
            }

    def current_stage(self) -> str | None:
        with self._lock:
            if self.collection_started_at is not None and self.collection_finished_at is None:
                return "collection"
            if self.validation_started_at is not None and self.validation_finished_at is None:
                return "validation"
            if self.subprocess_started_at is not None and self.subprocess_finished_at is None:
                return "subprocess"
            return None

    @staticmethod
    def _duration(
        snapshot: dict[str, float | None],
        start: str,
        finish: str,
        boundary_at: float | None = None,
    ) -> float | None:
        started = snapshot.get(start)
        finished = snapshot.get(finish)
        if started is None:
            return None
        if finished is None:
            finished = boundary_at
        elif boundary_at is not None:
            finished = min(finished, boundary_at)
        if finished is None:
            return None
        return round(max(0.0, finished - started), 6)

    def known_durations(self, boundary_at: float | None = None) -> dict[str, float | None]:
        """Return completed phase durations and exact active duration at a boundary."""
        snapshot = self.snapshot()
        return {
            "subprocess_duration_seconds": self._duration(
                snapshot, "subprocess_started_at", "subprocess_finished_at", boundary_at
            ),
            "validation_duration_seconds": self._duration(
                snapshot, "validation_started_at", "validation_finished_at", boundary_at
            ),
            "collection_duration_seconds": self._duration(
                snapshot, "collection_started_at", "collection_finished_at", boundary_at
            ),
        }


@dataclass
class ExtractionRoundContext:
    """Scheduling and exact phase telemetry for one submitted work item."""

    item: dict[str, Any]
    submission_sequence: int
    worker_count: int
    worker_slot: int | None
    submitted_at: float
    portfolio_deadline: float
    admission_deadline: float | None = None
    phase_telemetry: ExtractionPhaseTelemetry = field(default_factory=ExtractionPhaseTelemetry)
    worker_started_at: float | None = None
    started_at: float | None = None
    source_deadline: float | None = None
    token: CancellationToken | None = None
    # Only the collector thread claims a boundary.
    boundary_claimed: bool = False
    _progress_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _progress_observed: bool = field(default=False, init=False, repr=False)
    _progress_idle_seconds: float | None = field(default=None, init=False, repr=False)
    _progress_event_counts: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _progress_observed_at: float | None = field(default=None, init=False, repr=False)
    _progress_fallback_started_at: float | None = field(default=None, init=False, repr=False)

    @property
    def started(self) -> bool:
        return self.started_at is not None

    @property
    def effective_deadline(self) -> float:
        if self.source_deadline is None:
            return self.portfolio_deadline
        return min(self.source_deadline, self.portfolio_deadline)

    @property
    def limiting_supplier(self) -> str:
        if self.source_deadline is not None and self.source_deadline <= self.portfolio_deadline:
            return "source_execution_ceiling"
        return "portfolio_deadline"

    def mark_worker_started(self, timestamp: float, worker_slot: int | None) -> None:
        self.worker_started_at = timestamp
        self.worker_slot = worker_slot

    def mark_attempt_started(self, timestamp: float) -> None:
        self.started_at = timestamp
        with self._progress_lock:
            self._progress_fallback_started_at = timestamp
        self.phase_telemetry.mark_attempt_started(timestamp)

    def update_progress_snapshot(
        self,
        idle_seconds: float | None,
        event_counts: dict[str, int],
        observed_at: float,
    ) -> None:
        """Copy agent progress into this context without exposing the lease."""
        with self._progress_lock:
            if idle_seconds is not None:
                try:
                    idle = max(0.0, float(idle_seconds))
                except (TypeError, ValueError, OverflowError):
                    idle = None
                if idle is not None:
                    self._progress_idle_seconds = idle
                    self._progress_observed_at = observed_at
                    self._progress_observed = True
            self._progress_event_counts = {
                str(event_type): int(count)
                for event_type, count in event_counts.items()
                if (
                    str(event_type) in _PROGRESS_EVENT_TYPES
                    and isinstance(count, int)
                    and not isinstance(count, bool)
                    and count >= 0
                )
            }

    def progress_snapshot(self, now: float) -> tuple[float | None, dict[str, int]]:
        """Return a safe, small progress view for the collector thread."""
        with self._progress_lock:
            if self._progress_observed and self._progress_idle_seconds is not None:
                observed_at = self._progress_observed_at if self._progress_observed_at is not None else now
                idle_seconds = self._progress_idle_seconds + max(0.0, now - observed_at)
            elif self._progress_fallback_started_at is None:
                idle_seconds = None
            else:
                idle_seconds = max(0.0, now - self._progress_fallback_started_at)
            return idle_seconds, dict(self._progress_event_counts)


@dataclass(frozen=True)
class ExtractionRoundCompletion:
    """One completed or bounded result from a round."""

    context: ExtractionRoundContext
    result: Any
    completion_sequence: int
    late: bool = False


BoundaryFactory = Callable[[ExtractionRoundContext, str, float, str], Any]
RunFactory = Callable[[ExtractionRoundContext], Any]
ExceptionFactory = Callable[[ExtractionRoundContext, BaseException], Any]
LateResultHandler = Callable[[ExtractionRoundContext, Any], None]
# Called as each result lands. Reporting only. A raising callback must never
# end the round. The observer owns its own counting, because a round sees only
# the items admitted to it, not the caller's whole portfolio.
CompletionObserver = Callable[[ExtractionRoundCompletion], None]
# Called by the collector thread while futures remain pending. Worker threads
# never invoke this callback.
PollObserver = Callable[[list[ExtractionRoundContext], float], None]


@dataclass(frozen=True)
class _BoundaryRequest:
    """Worker result that asks the collector to create the boundary result."""

    context: ExtractionRoundContext


def _worker_slot() -> int | None:
    suffix = threading.current_thread().name.rsplit("-", 1)[-1]
    try:
        return int(suffix) + 1
    except ValueError:
        return None


def _wait_for_progress(future: DaemonFuture) -> None:
    try:
        future.result(timeout=0.01)
    except BaseException:
        pass


def _phase_for_boundary(context: ExtractionRoundContext) -> str:
    return context.phase_telemetry.current_stage() or "subprocess"


def execute_bounded_extraction_round(
    items: list[dict[str, Any]],
    *,
    worker_count: int,
    per_attempt_ceiling: float,
    portfolio_deadline: float,
    clock: Callable[[], float] = time.monotonic,
    run: RunFactory,
    boundary: BoundaryFactory,
    on_exception: ExceptionFactory | None = None,
    on_late_result: LateResultHandler | None = None,
    on_completion: CompletionObserver | None = None,
    on_poll: PollObserver | None = None,
    admission_deadline: float | None = None,
    thread_name_prefix: str = "autoscout-extraction-round-",
) -> list[ExtractionRoundCompletion]:
    """Run one bounded, fair, completion-driven extraction round."""

    if worker_count <= 0:
        raise ValueError("worker_count must be positive")
    if per_attempt_ceiling <= 0:
        raise ValueError("per_attempt_ceiling must be positive")

    def _observe(completion: ExtractionRoundCompletion) -> None:
        if on_completion is None:
            return
        try:
            on_completion(completion)
        except Exception:
            # Progress reporting must never bound or fail the round.
            pass

    def _observe_poll(contexts: list[ExtractionRoundContext], now: float) -> None:
        if on_poll is None:
            return
        try:
            on_poll(contexts, now)
        except Exception:
            # A snapshot observer must never bound or fail the round.
            pass

    pool = DaemonThreadPool(worker_count, thread_name_prefix=thread_name_prefix)
    lock = threading.Lock()
    contexts: dict[DaemonFuture, ExtractionRoundContext] = {}
    futures: list[DaemonFuture] = []
    first_wave_target = min(worker_count, len(items))
    first_wave_ready = threading.Event()
    first_wave_lock = threading.Lock()
    first_wave_count = 0
    try:
        for sequence, item in enumerate(items, start=1):
            context = ExtractionRoundContext(
                item=item,
                submission_sequence=sequence,
                worker_count=worker_count,
                worker_slot=None,
                submitted_at=clock(),
                portfolio_deadline=portfolio_deadline,
                admission_deadline=admission_deadline,
                token=CancellationToken(),
            )

            def _run(current: ExtractionRoundContext = context) -> Any:
                nonlocal first_wave_count
                worker_started = clock()
                with lock:
                    current.mark_worker_started(worker_started, _worker_slot())
                    can_start = worker_started < portfolio_deadline and (
                        admission_deadline is None or worker_started < admission_deadline
                    )
                if not can_start:
                    # The collector owns every boundary.  Returning a request
                    # instead of calling the boundary here prevents a queued
                    # worker from racing the collector and observing a
                    # half-built or None boundary result.
                    return _BoundaryRequest(current)

                wait_for_first_wave = False
                with first_wave_lock:
                    if first_wave_count < first_wave_target:
                        first_wave_count += 1
                        wait_for_first_wave = True
                        if first_wave_count >= first_wave_target:
                            first_wave_ready.set()
                if wait_for_first_wave:
                    first_wave_ready.wait(timeout=1.0)
                attempt_started = clock()
                with lock:
                    current.source_deadline = attempt_started + per_attempt_ceiling
                    current.mark_attempt_started(attempt_started)
                return run(current)

            future = pool.submit(_run)
            contexts[future] = context
            futures.append(future)

        pending = set(futures)
        completions: list[ExtractionRoundCompletion] = []
        completed_contexts: set[int] = set()
        completion_sequence = 0

        def _fallback_result(
            context: ExtractionRoundContext,
            *,
            stage: str,
            supplier: str,
            status: str,
            diagnostic: str | None = None,
            exception_type: str | None = None,
        ) -> dict[str, Any]:
            """Return a non-null, explicitly diagnosed terminal result."""
            entry: dict[str, Any] = {
                "source_name": str(context.item.get("source_name", "")),
                "status": status,
                "diagnostic": diagnostic or status,
                "timeout_stage": stage,
                "boundary_supplier": supplier,
                "worker_slot": context.worker_slot if context.started else None,
                "submission_sequence": context.submission_sequence,
                "started": context.started,
            }
            if exception_type is not None:
                entry["exception_type"] = exception_type
            return {
                "started": context.started,
                "cancel_token": context.token,
                "diagnostics": [entry],
                "_boundary_stage": stage,
                "_boundary_supplier": supplier,
                "terminal_class": status,
            }

        def _boundary_result(
            context: ExtractionRoundContext,
            stage: str,
            threshold: float,
            supplier: str,
        ) -> Any:
            with lock:
                if context.boundary_claimed:
                    return _fallback_result(
                        context,
                        stage=stage,
                        supplier=supplier,
                        status="duplicate_boundary_claim",
                    )
                context.boundary_claimed = True
            if context.started and context.token is not None:
                context.token.cancel()
            try:
                result = boundary(context, stage, threshold, supplier)
            except BaseException as exc:
                return _fallback_result(
                    context,
                    stage=stage,
                    supplier=supplier,
                    status="worker_exception",
                    diagnostic="boundary_callback_exception",
                    exception_type=type(exc).__name__,
                )
            if result is None:
                return _fallback_result(
                    context,
                    stage=stage,
                    supplier=supplier,
                    status="worker_exception",
                    diagnostic="boundary_callback_returned_none",
                )
            return result

        def _append_completion(context: ExtractionRoundContext, result: Any) -> None:
            nonlocal completion_sequence
            identity = id(context)
            if identity in completed_contexts:
                raise RuntimeError(f"duplicate extraction completion for submission {context.submission_sequence}")
            if result is None:
                result = _fallback_result(
                    context,
                    stage=_phase_for_boundary(context),
                    supplier="portfolio_deadline",
                    status="worker_exception",
                )
            completed_contexts.add(identity)
            completion_sequence += 1
            completion = ExtractionRoundCompletion(context, result, completion_sequence)
            completions.append(completion)
            _observe(completion)

        def _queued_boundary(context: ExtractionRoundContext) -> Any:
            boundary_deadline = portfolio_deadline
            boundary_supplier = "portfolio_deadline"
            if admission_deadline is not None and admission_deadline <= portfolio_deadline:
                boundary_deadline = admission_deadline
                boundary_supplier = "segment_deadline"
            return _boundary_result(
                context,
                "queue",
                max(0.0, boundary_deadline - context.submitted_at),
                boundary_supplier,
            )

        while pending:
            progressed = False
            for future in list(pending):
                if not future.done():
                    continue
                context = contexts[future]
                try:
                    result = future.result(timeout=0)
                    if isinstance(result, _BoundaryRequest):
                        result = _queued_boundary(context)
                except BaseException as exc:
                    if on_exception is None:
                        result = _fallback_result(
                            context,
                            stage=_phase_for_boundary(context),
                            supplier="portfolio_deadline",
                            status="worker_exception",
                            diagnostic="worker_exception",
                            exception_type=type(exc).__name__,
                        )
                    else:
                        try:
                            result = on_exception(context, exc)
                        except BaseException as callback_exc:
                            result = _fallback_result(
                                context,
                                stage=_phase_for_boundary(context),
                                supplier="portfolio_deadline",
                                status="worker_exception",
                                diagnostic="exception_callback_exception",
                                exception_type=type(callback_exc).__name__,
                            )
                _append_completion(context, result)
                pending.remove(future)
                progressed = True

            now = clock()

            if pending:
                _observe_poll([contexts[future] for future in pending], now)
                expired: list[tuple[DaemonFuture, str, float, str]] = []
                for future in list(pending):
                    context = contexts[future]
                    if context.started:
                        active_stage = context.phase_telemetry.current_stage()
                        if now >= portfolio_deadline and active_stage is None:
                            boundary_at = portfolio_deadline
                            supplier = "portfolio_deadline"
                            stage = _phase_for_boundary(context)
                        else:
                            boundary_at = context.effective_deadline
                            if now < boundary_at:
                                continue
                            supplier = context.limiting_supplier
                            if supplier == "source_execution_ceiling" and active_stage is None:
                                continue
                            stage = active_stage or "subprocess"
                        started_at = context.started_at if context.started_at is not None else boundary_at
                        threshold = max(0.0, boundary_at - started_at)
                    elif now >= min(
                        portfolio_deadline,
                        admission_deadline if admission_deadline is not None else portfolio_deadline,
                    ):
                        stage = "queue"
                        boundary_deadline = min(
                            portfolio_deadline,
                            admission_deadline if admission_deadline is not None else portfolio_deadline,
                        )
                        threshold = max(0.0, boundary_deadline - context.submitted_at)
                        supplier = "segment_deadline" if admission_deadline is not None else "portfolio_deadline"
                    else:
                        continue
                    expired.append((future, stage, threshold, supplier))

                for future, stage, threshold, supplier in expired:
                    if future.done():
                        continue
                    _wait_for_progress(future)
                    if future.done():
                        continue
                    context = contexts[future]
                    result = _boundary_result(context, stage, threshold, supplier)
                    _append_completion(context, result)
                    pending.remove(future)

                    def _late(
                        done: DaemonFuture = future,
                        current_context: ExtractionRoundContext = context,
                    ) -> None:
                        try:
                            late_result = done.result(timeout=0)
                        except BaseException:
                            return
                        if on_late_result is not None:
                            on_late_result(current_context, late_result)

                    if future.done():
                        _late()
                    else:
                        future.add_done_callback(_late)

                if expired:
                    continue

            if pending and not progressed:
                _wait_for_progress(next(iter(pending)))

        # A cancellation or pool failure must still produce exactly one
        # non-null completion for every submitted item.  This is a fail-closed
        # invariant, not an assertion that can hide a lost work item.
        for future in futures:
            context = contexts[future]
            if id(context) not in completed_contexts:
                _append_completion(
                    context,
                    _fallback_result(
                        context,
                        stage=_phase_for_boundary(context),
                        supplier="portfolio_deadline",
                        status="unfinished",
                    ),
                )
        if len(completions) != len(items) or len({id(c.context) for c in completions}) != len(items):
            raise RuntimeError("extraction round completion invariant violated")
        return sorted(completions, key=lambda completion: completion.context.submission_sequence)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


__all__ = [
    "CompletionObserver",
    "ExtractionPhaseTelemetry",
    "ExtractionRoundCompletion",
    "ExtractionRoundContext",
    "PollObserver",
    "execute_bounded_extraction_round",
]
