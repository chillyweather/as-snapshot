"""Best-effort live progress snapshots for one weekly operation."""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from autoscout.runtime_budget import CODEX_NO_PROGRESS_TIMEOUT_SECONDS
from autoscout.secret_redaction import redact_secrets

PROGRESS_SNAPSHOT_FILE = "progress-snapshot.json"
PROGRESS_SNAPSHOT_VERSION = 1
PROGRESS_SNAPSHOT_INTERVAL_SECONDS = 12.0
# Operator-facing heartbeat cadence (issue #452): at most one presentation
# line every 60 seconds, independent of how often the file snapshot is
# (force-)published.
PROGRESS_HEARTBEAT_INTERVAL_SECONDS = 60.0
# A snapshot older than this many heartbeats means the stage stopped
# publishing: either it finished without a final publish or it hung
# (issue #706). The doctor's run-in-progress verdict flags such a stage as
# possibly stalled; anything fresher is a healthy running stage.
PROGRESS_SNAPSHOT_STALE_HEARTBEATS = 3
# One heartbeat line names at most this many running items.
PROGRESS_PRESENTATION_MAX_RUNNING_ITEMS = 3
# Display name for a per-candidate row whose title is blank (issue #706):
# the shared "source without a name" fallback belongs to source rows.
CANDIDATE_SNAPSHOT_UNNAMED_LABEL = "книга без названия"
# Capture rows run under "in_flight"; extraction rows run under "running".
_RUNNING_SNAPSHOT_STATES = frozenset({"running", "in_flight"})
ProgressSnapshotFailureRecorder = Callable[[str, str], None]
ProgressSnapshotPresentation = Callable[[dict[str, Any]], None]


def presentation_warning_threshold_seconds() -> float:
    """Return the idle value at which an extraction item is reported as stalled.

    The threshold is derived, never a second runtime constant: it is exactly
    one heartbeat before the extraction no-progress lease kills an item, so
    the operator sees one distinct warning before the lease acts
    (``CODEX_NO_PROGRESS_TIMEOUT_SECONDS`` is currently 180 seconds, so the
    derived threshold is 120 seconds).

    This threshold applies only to extraction event-idle time. It is not
    meaningful for capture rows: capture runs under a much shorter
    per-source timeout, so the stall warning is never applied to
    ``in_flight`` capture rows.
    """
    return max(0.0, float(CODEX_NO_PROGRESS_TIMEOUT_SECONDS) - PROGRESS_HEARTBEAT_INTERVAL_SECONDS)


def snapshot_stale_after_seconds() -> float:
    """Return the snapshot age at which a running stage looks stalled.

    The threshold is derived, never a second runtime constant: it is three
    missed operator heartbeats, so a stage that stopped publishing (hung
    workers, a crashed publisher) is flagged while a stage between two
    heartbeats never is.
    """
    return PROGRESS_HEARTBEAT_INTERVAL_SECONDS * PROGRESS_SNAPSHOT_STALE_HEARTBEATS


def summarize_snapshot_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize snapshot rows into aggregate counts and bounded running items.

    Pure data shaping for presentation (issue #452): no Russian text and no
    I/O. Extraction rows carry ``idle_seconds`` while rows running under the
    in-flight wording (capture rows and description-stage candidate rows,
    issue #706) carry ``elapsed_seconds`` on their ``in_flight`` rows; both
    feed the same idle value. Each named item retains its row ``state`` so
    the Russian builder can pick accurate stage-specific wording. At most
    ``PROGRESS_PRESENTATION_MAX_RUNNING_ITEMS`` running items are named;
    ``omitted_running`` counts the rest. Non-dict rows are ignored, matching
    the payload filtering before presentation, and never counted as
    not-started items.
    """
    finished = 0
    running = 0
    not_started = 0
    running_items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        state = str(row.get("state", ""))
        if state == "finished":
            finished += 1
            continue
        if state in _RUNNING_SNAPSHOT_STATES:
            running += 1
            idle = row.get("idle_seconds")
            if idle is None and state == "in_flight":
                idle = row.get("elapsed_seconds")
            if len(running_items) < PROGRESS_PRESENTATION_MAX_RUNNING_ITEMS:
                running_items.append(
                    {
                        "name": str(row.get("source_name", "")),
                        "state": state,
                        "idle_seconds": _safe_seconds(idle) if idle is not None else None,
                    }
                )
            continue
        not_started += 1
    return {
        "finished": finished,
        "running": running,
        "not_started": not_started,
        "running_items": running_items,
        "omitted_running": running - len(running_items),
    }


class ProgressSnapshotWriter:
    """Atomically publish advisory progress without affecting the operation."""

    def __init__(
        self,
        output_dir: Path,
        *,
        monotonic: Any = time.monotonic,
        interval_seconds: float = PROGRESS_SNAPSHOT_INTERVAL_SECONDS,
        failure_recorder: ProgressSnapshotFailureRecorder | None = None,
        presentation: ProgressSnapshotPresentation | None = None,
        presentation_interval_seconds: float = PROGRESS_HEARTBEAT_INTERVAL_SECONDS,
        write_snapshot: bool = True,
    ) -> None:
        self._path = Path(output_dir) / PROGRESS_SNAPSHOT_FILE
        self._write_snapshot = write_snapshot
        self._monotonic = monotonic
        self._interval_seconds = float(interval_seconds)
        self._failure_recorder = failure_recorder
        self._recorded_failure_stages: set[str] = set()
        self._last_published_at: float | None = None
        # Issue #452: optional advisory presentation boundary. The callback
        # receives one redacted snapshot payload at the operator heartbeat
        # cadence; it is throttled independently of ``force`` and every
        # failure is swallowed into the same once-per-stage recorder.
        self._presentation = presentation
        self._presentation_interval_seconds = float(presentation_interval_seconds)
        self._last_presented_at: float | None = None
        # Timestamp of the last failed presentation attempt, used to bound the
        # retry cadence to the snapshot file interval instead of every publish.
        self._last_presentation_attempt_at: float | None = None

    @property
    def path(self) -> Path:
        return self._path

    def publish(
        self,
        *,
        stage: str,
        status: str,
        elapsed_seconds: float,
        rows: list[dict[str, Any]],
        now: float | None = None,
        force: bool = True,
        stage_budget_seconds: float | None = None,
    ) -> bool:
        """Publish one complete snapshot, or return false after any failure.

        ``stage_budget_seconds`` is advisory only (issue #706): when present
        it is stored in the file payload so the doctor can compare stage
        elapsed time against the stage budget. It never changes throttling,
        presentation, or the exit behavior.
        """
        current = self._monotonic() if now is None else now
        self._present_if_due(stage, status, elapsed_seconds, rows, current, stage_budget_seconds)
        if not self._write_snapshot:
            return True
        if not force and self._last_published_at is not None:
            if current - self._last_published_at < self._interval_seconds:
                return False

        try:
            payload: dict[str, Any] = {
                "version": PROGRESS_SNAPSHOT_VERSION,
                "stage": str(stage),
                "status": str(status),
                "elapsed_seconds": _safe_seconds(elapsed_seconds),
                "rows": rows,
            }
            if stage_budget_seconds is not None:
                payload["stage_budget_seconds"] = _safe_seconds(stage_budget_seconds)
            serialized = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
            serialized = redact_secrets(serialized)
            if not isinstance(serialized, str):
                raise TypeError("redact_secrets returned a non-string snapshot")
            self._atomic_replace(serialized)
        except Exception as exc:
            self._record_failure(stage, exc)
            return False
        self._last_published_at = current
        return True

    def _present_if_due(
        self,
        stage: str,
        status: str,
        elapsed_seconds: float,
        rows: list[dict[str, Any]],
        current: float,
        stage_budget_seconds: float | None = None,
    ) -> None:
        """Present the snapshot to the operator at most once per heartbeat.

        Advisory only: a missing callback, a silent (non-running) snapshot,
        and any callback failure must never raise into the workflow, change a
        result, or affect an exit code. A raising callback is routed through
        the same once-per-stage failure recorder as file-publish failures. A
        failed attempt does not consume the heartbeat slot, but it is bounded
        by the snapshot file interval: a raising callback is retried no
        faster than one snapshot interval, never on every forced publish.
        Successful presentations stay throttled by the 60-second heartbeat.
        """
        if self._presentation is None:
            return
        try:
            # A weekly coordinator observes every update, including terminal
            # snapshots, and owns the cadence across concurrent stage writers.
            observer = getattr(self._presentation, "update_snapshot", None)
            if not callable(observer):
                overview = summarize_snapshot_rows(rows)
                if overview["running"] <= 0:
                    return
                if (
                    self._last_presented_at is not None
                    and current - self._last_presented_at < self._presentation_interval_seconds
                ):
                    return
                if (
                    self._last_presentation_attempt_at is not None
                    and current - self._last_presentation_attempt_at < self._interval_seconds
                ):
                    return
            payload = {
                "version": PROGRESS_SNAPSHOT_VERSION,
                "stage": str(stage),
                "status": str(status),
                "elapsed_seconds": _safe_seconds(elapsed_seconds),
                "rows": [dict(row) for row in rows if isinstance(row, dict)],
            }
            if stage_budget_seconds is not None:
                payload["stage_budget_seconds"] = _safe_seconds(stage_budget_seconds)
            serialized = redact_secrets(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            if not isinstance(serialized, str):
                raise TypeError("redact_secrets returned a non-string snapshot")
            if callable(observer):
                observer(json.loads(serialized))
                return
            self._presentation(json.loads(serialized))
            # The heartbeat slot is consumed only by a successful presentation:
            # a raising callback leaves the slot free, retried at the snapshot
            # interval bound below.
            self._last_presented_at = current
        except Exception as exc:
            # A failed attempt does not consume the heartbeat slot, but it is
            # throttled to the snapshot file interval so a permanently raising
            # callback is never invoked on every forced publish.
            self._last_presentation_attempt_at = current
            self._record_failure(stage, exc)

    def _record_failure(self, stage: str, error: Exception) -> None:
        try:
            safe_stage = str(stage)
        except Exception:
            safe_stage = "unknown"
        if safe_stage in self._recorded_failure_stages:
            return
        self._recorded_failure_stages.add(safe_stage)
        if self._failure_recorder is None:
            return
        try:
            try:
                raw_reason = f"{type(error).__name__}: {error}"
            except Exception:
                raw_reason = type(error).__name__
            reason = redact_secrets(raw_reason)
            if not isinstance(reason, str):
                return
            self._failure_recorder(safe_stage, reason)
        except Exception:
            pass

    def _atomic_replace(self, serialized: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                temporary.write(serialized)
            os.replace(temporary_name, self._path)
        finally:
            if temporary_name and os.path.exists(temporary_name):
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass


def _safe_seconds(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return round(max(0.0, number), 3) if math.isfinite(number) else 0.0


class CandidateProgressSnapshot:
    """Thread-safe per-candidate progress for one description stage.

    Issue #706: the grounded-enrichment and description-adjudication stages
    run each candidate on a bounded worker pool with nothing to report
    between the single stage-announce line and the final report. This
    tracker publishes one row per candidate through the same
    :class:`ProgressSnapshotWriter` heartbeat the capture/extraction stages
    use: the file stays current (so the snapshot ``stage`` always matches
    the running stage) and the operator gets at most one heartbeat line
    every 60 seconds with finished/running/not-started counts.

    Rows move forward only (``not_started`` → ``in_flight`` → ``finished``),
    so the finished count never decreases. A candidate in flight runs under
    the same ``in_flight`` state capture rows use — it carries elapsed
    in-flight time, not event-idle time — so the shared Russian wording and
    stall-warning rules follow from the row state alone. Every method is
    safe to call from worker threads: ``begin``/``finish`` are called by the
    workers while ``initial``/``poll``/``final``/``collect`` run on the main
    thread, all serialized by one lock so the writer always sees sequential
    calls.

    All failures are advisory: a tracker that cannot publish (or whose
    presentation raises) records through the writer's once-per-stage
    recorder and never raises into the stage.
    """

    def __init__(
        self,
        output_dir: Path,
        *,
        stage: str,
        stage_budget_seconds: float | None = None,
        monotonic: Any = time.monotonic,
        failure_recorder: ProgressSnapshotFailureRecorder | None = None,
        presentation: ProgressSnapshotPresentation | None = None,
        presentation_interval_seconds: float = PROGRESS_HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self._stage = str(stage)
        self._stage_budget_seconds = stage_budget_seconds
        self._monotonic = monotonic
        self._writer = ProgressSnapshotWriter(
            output_dir,
            monotonic=monotonic,
            failure_recorder=failure_recorder,
            presentation=presentation,
            presentation_interval_seconds=presentation_interval_seconds,
        )
        self._started = monotonic()
        self._lock = threading.Lock()
        # Keyed by the caller's candidate key (a Candidate ID or identity
        # key): display name plus lifecycle timestamps. ``started_at`` is set
        # by the first ``begin`` only, so a repeated begin never rewinds the
        # in-flight clock; ``finished_at`` is set by ``finish``.
        self._names: dict[str, str] = {}
        self._order: list[str] = []
        self._started_at: dict[str, float] = {}
        self._finished_at: dict[str, float] = {}

    @property
    def stage(self) -> str:
        return self._stage

    def set_stage_budget_seconds(self, seconds: float) -> None:
        """Set the budget used in subsequent snapshots."""
        with self._lock:
            self._stage_budget_seconds = seconds

    def initial(self, items: list[tuple[str, str]], *, now: float | None = None) -> None:
        """Publish every candidate as not started. Never presents a line."""
        current = self._monotonic() if now is None else now
        with self._lock:
            for key, name in items:
                if key not in self._names:
                    self._order.append(key)
                self._names[key] = self._display_name(name)
            self._publish(status="running", current=current, force=True)

    def begin(self, key: str, *, now: float | None = None) -> None:
        """Mark one candidate running. Unknown keys are ignored, never raise."""
        current = self._monotonic() if now is None else now
        with self._lock:
            if key not in self._names or key in self._finished_at:
                return
            if key not in self._started_at:
                self._started_at[key] = current
            self._publish(status="running", current=current, force=True)

    def finish(self, key: str, *, now: float | None = None) -> None:
        """Mark one candidate finished. Unknown keys are ignored, never raise."""
        current = self._monotonic() if now is None else now
        with self._lock:
            if key not in self._names or key in self._finished_at:
                return
            self._finished_at[key] = current
            self._publish(status="running", current=current, force=True)

    def poll(self, *, now: float | None = None) -> None:
        """Refresh elapsed times on the throttled cadence (no forced write)."""
        current = self._monotonic() if now is None else now
        with self._lock:
            self._publish(status="running", current=current, force=False)

    def final(self, *, status: str = "finished", now: float | None = None) -> None:
        """Publish the terminal stage snapshot.

        Rows keep their real terminal state: a candidate the stage budget
        never reached stays ``not_started`` rather than inflating the
        finished count. Names no running items, so no heartbeat line.
        """
        current = self._monotonic() if now is None else now
        with self._lock:
            self._publish(status=status, current=current, force=True)

    def collect(self, futures: Any, *, tick_seconds: float = 0.25) -> list[Any]:
        """Collect pool futures in submit order, polling the tracker meanwhile.

        A blind ``future.result()`` loop publishes nothing while every worker
        is inside one long candidate (a 90-135 s adjudication call), which is
        exactly the silence issue #706 removes. Each future is waited on in
        bounded slices; between slices the tracker polls, keeping the file
        fresh and the heartbeat audible.

        The retry is keyed on ``future.done()``, never on the exception
        alone: ``DaemonFuture.result(timeout=…)`` raises the same bare
        ``TimeoutError`` for an expired wait slice and for the worker's own
        exception (Python 3.10+ makes ``TimeoutError`` an ``OSError``
        subclass, so a worker's socket/pipe ETIMEDOUT raises the same
        class). A completed future is re-read exactly once — its result or
        its exception propagates — so a worker timeout can never spin the
        loop forever. Result order and exception behavior therefore match
        the plain ``future.result()`` loop: results arrive in submit order,
        and the first raised exception propagates after the remaining
        futures have run to completion on their daemon workers.
        """
        results: list[Any] = []
        slice_seconds = min(max(0.01, float(tick_seconds)), 0.25)
        for future in futures:
            while not future.done():
                try:
                    future.result(timeout=slice_seconds)
                except TimeoutError:
                    # The slice expired, or the worker finished during the
                    # wait and raised its own TimeoutError. done() is the
                    # only honest discriminator; a re-check after the raise
                    # also covers the future completing between the wait
                    # timeout and this check.
                    if future.done():
                        break
                    self.poll()
            results.append(future.result())
        return results

    @staticmethod
    def _display_name(name: str) -> str:
        text = str(name or "").strip()
        return text if text else CANDIDATE_SNAPSHOT_UNNAMED_LABEL

    def _publish(self, *, status: str, current: float, force: bool) -> None:
        rows = [self._row(key, current) for key in self._order]
        self._writer.publish(
            stage=self._stage,
            status=status,
            elapsed_seconds=current - self._started,
            rows=rows,
            now=current,
            force=force,
            stage_budget_seconds=self._stage_budget_seconds,
        )

    def _row(self, key: str, current: float) -> dict[str, Any]:
        if key in self._finished_at:
            state = "finished"
            finished_at = self._finished_at[key]
            started_at = self._started_at.get(key, finished_at)
            elapsed = max(0.0, finished_at - started_at)
        elif key in self._started_at:
            # Description candidates have no sub-event idle stream, only
            # in-flight time, so they run under the same "in_flight" state
            # capture rows use — the shared wording/threshold rules follow
            # from the state, not from the stage.
            state = "in_flight"
            elapsed = max(0.0, current - self._started_at[key])
        else:
            state = "not_started"
            elapsed = 0.0
        status = {"finished": "finished", "in_flight": "in_flight"}.get(state, "pending")
        return {
            "candidate_key": key,
            "source_name": self._names[key],
            "state": state,
            "status": status,
            "elapsed_seconds": round(elapsed, 3),
        }


__all__ = [
    "CANDIDATE_SNAPSHOT_UNNAMED_LABEL",
    "PROGRESS_HEARTBEAT_INTERVAL_SECONDS",
    "PROGRESS_PRESENTATION_MAX_RUNNING_ITEMS",
    "PROGRESS_SNAPSHOT_FILE",
    "PROGRESS_SNAPSHOT_INTERVAL_SECONDS",
    "PROGRESS_SNAPSHOT_STALE_HEARTBEATS",
    "PROGRESS_SNAPSHOT_VERSION",
    "CandidateProgressSnapshot",
    "ProgressSnapshotFailureRecorder",
    "ProgressSnapshotPresentation",
    "ProgressSnapshotWriter",
    "presentation_warning_threshold_seconds",
    "snapshot_stale_after_seconds",
    "summarize_snapshot_rows",
]
