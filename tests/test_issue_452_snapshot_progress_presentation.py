"""Focused acceptance tests for issue #452: surface the live progress snapshot.

All rows are constructed Goodreads-shaped fixtures. No test here runs the
weekly workflow or makes a network request: the presentation boundary, the
snapshot publishers, and the real CLI/recovery bindings are driven directly.
"""

from __future__ import annotations

import json
import types
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from autoscout import doctor_recovery
from autoscout import weekly_workflow
from autoscout.codex_recovery import ProductionCodexAdapter
from autoscout.codex_recovery import ProductionCodexCaptureAdapter
from autoscout.codex_recovery import _ExtractionProgressSnapshot
from autoscout.doctor_recovery import RecoveryReport
from autoscout.doctor_recovery import RecoverySeams
from autoscout.doctor_recovery import _default_workflow_runner
from autoscout.progress_snapshot import PROGRESS_HEARTBEAT_INTERVAL_SECONDS
from autoscout.progress_snapshot import PROGRESS_PRESENTATION_MAX_RUNNING_ITEMS
from autoscout.progress_snapshot import PROGRESS_SNAPSHOT_VERSION
from autoscout.progress_snapshot import ProgressSnapshotWriter
from autoscout.progress_snapshot import presentation_warning_threshold_seconds
from autoscout.progress_snapshot import summarize_snapshot_rows
from autoscout.runtime_budget import CODEX_NO_PROGRESS_TIMEOUT_SECONDS
from autoscout.russian_workflow import extraction_progress_message
from autoscout.russian_workflow import snapshot_progress_line
from autoscout.russian_workflow import stage_message
from autoscout.source_collection import _CaptureProgressSnapshot
from autoscout.weekly_workflow import WeeklyWorkflowAdapters
from autoscout.weekly_workflow import wire_snapshot_progress_presentation


class _ManualClock:
    """Injected monotonic clock for deterministic cadence tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _goodreads_rows(
    *,
    goodreads_idle: float = 183.4,
    neighbor_idle: float = 12.0,
    finished: int = 10,
    not_started: int = 17,
) -> list[dict[str, Any]]:
    """The measured #449 shape: a silent Goodreads item next to a healthy one."""
    rows: list[dict[str, Any]] = []
    for index in range(finished):
        rows.append(
            {
                "source_name": f"Finished {index}",
                "source_url": f"https://example.test/finished-{index}",
                "state": "finished",
                "status": "validated",
                "idle_seconds": None,
                "event_counts": {"task_started": 3, "page_read": 10, "extract_rows": 7},
            }
        )
    rows.append(
        {
            "source_name": "Goodreads Choice Awards 2025 Nonfiction",
            "source_url": "https://www.goodreads.com/choiceawards/best-nonfiction-books-2025",
            "state": "running",
            "status": "running",
            "idle_seconds": goodreads_idle,
            "event_counts": {"task_started": 1},
        }
    )
    rows.append(
        {
            "source_name": "NPR",
            "source_url": "https://example.test/npr",
            "state": "running",
            "status": "running",
            "idle_seconds": neighbor_idle,
            "event_counts": {"task_started": 2, "page_read": 8, "extract_rows": 5, "validated_rows": 4},
        }
    )
    for index in range(not_started):
        rows.append(
            {
                "source_name": f"Pending {index}",
                "source_url": f"https://example.test/pending-{index}",
                "state": "not_started",
                "status": "pending",
                "idle_seconds": None,
                "event_counts": {},
            }
        )
    return rows


def _capture_rows(in_flight_elapsed: float = 125.4) -> list[dict[str, Any]]:
    """The capture-stage shape: in-flight rows carry elapsed seconds, not idle."""
    rows: list[dict[str, Any]] = [
        {
            "source_name": "BookPage",
            "source_url": "https://example.test/bookpage",
            "state": "finished",
            "status": "finished",
            "outcome": "codex-captured",
            "elapsed_seconds": 2.0,
            "remaining_runtime_budget_seconds": 100.0,
            "per_source_timeout_seconds": 10.0,
        },
        {
            "source_name": "Goodreads Choice Awards 2025 Nonfiction",
            "source_url": "https://www.goodreads.com/choiceawards/best-nonfiction-books-2025",
            "state": "in_flight",
            "status": "in_flight",
            "outcome": None,
            "elapsed_seconds": in_flight_elapsed,
            "remaining_runtime_budget_seconds": 100.0,
            "per_source_timeout_seconds": 10.0,
        },
    ]
    for index in range(3):
        rows.append(
            {
                "source_name": f"Pending {index}",
                "source_url": f"https://example.test/pending-{index}",
                "state": "pending",
                "status": "pending",
                "outcome": None,
                "elapsed_seconds": 0.0,
                "remaining_runtime_budget_seconds": 100.0,
                "per_source_timeout_seconds": 10.0,
            }
        )
    return rows


# ── The derived warning threshold ─────────────────────────────────────────


def test_warning_threshold_is_one_heartbeat_before_the_no_progress_lease() -> None:
    assert PROGRESS_HEARTBEAT_INTERVAL_SECONDS == 60.0
    # Derived from the live lease constant, never a second runtime constant.
    assert presentation_warning_threshold_seconds() == pytest.approx(
        CODEX_NO_PROGRESS_TIMEOUT_SECONDS - PROGRESS_HEARTBEAT_INTERVAL_SECONDS
    )


# ── Snapshot row summarizing (pure data shaping) ──────────────────────────


def test_summarize_counts_finished_running_not_started_and_caps_named_items() -> None:
    rows = _goodreads_rows()
    rows.extend(
        {
            "source_name": f"Extra runner {index}",
            "source_url": f"https://example.test/extra-{index}",
            "state": "running",
            "status": "running",
            "idle_seconds": 5.0,
            "event_counts": {},
        }
        for index in range(3)
    )
    overview = summarize_snapshot_rows(rows)
    assert overview["finished"] == 10
    assert overview["running"] == 5
    assert overview["not_started"] == 17
    assert len(overview["running_items"]) == PROGRESS_PRESENTATION_MAX_RUNNING_ITEMS == 3
    assert overview["omitted_running"] == 2
    assert overview["running_items"] == [
        {
            "name": "Goodreads Choice Awards 2025 Nonfiction",
            "state": "running",
            "idle_seconds": pytest.approx(183.4),
        },
        {"name": "NPR", "state": "running", "idle_seconds": pytest.approx(12.0)},
        {"name": "Extra runner 0", "state": "running", "idle_seconds": pytest.approx(5.0)},
    ]


def test_summarize_covers_capture_in_flight_rows_via_elapsed_seconds() -> None:
    overview = summarize_snapshot_rows(_capture_rows())
    assert overview["finished"] == 1
    assert overview["running"] == 1
    assert overview["not_started"] == 3
    assert overview["running_items"] == [
        {
            "name": "Goodreads Choice Awards 2025 Nonfiction",
            "state": "in_flight",
            "idle_seconds": pytest.approx(125.4),
        }
    ]
    assert overview["omitted_running"] == 0


def test_summarize_tolerates_malformed_rows() -> None:
    # Non-dict rows are ignored, matching the redacted presentation payload
    # filtering: they are never counted as not-started items.
    overview = summarize_snapshot_rows([None, {"state": "running"}, {"state": "running", "idle_seconds": "bad"}])  # type: ignore[list-item]
    assert overview["running"] == 2
    assert overview["not_started"] == 0
    assert overview["finished"] == 0
    assert len(overview["running_items"]) == 2
    assert overview["running_items"][0]["idle_seconds"] is None
    assert overview["running_items"][1]["idle_seconds"] == 0.0


def test_summarize_counts_valid_rows_around_ignored_non_dict_rows() -> None:
    rows: list[dict[str, Any] | None] = [
        None,
        {
            "source_name": "Done",
            "source_url": "https://example.test/done",
            "state": "finished",
            "status": "validated",
            "idle_seconds": None,
            "event_counts": {},
        },
        "garbage",  # type: ignore[list-item]
        {
            "source_name": "Runner",
            "source_url": "https://example.test/runner",
            "state": "running",
            "status": "running",
            "idle_seconds": 3.0,
            "event_counts": {},
        },
        None,
        {
            "source_name": "Pending",
            "source_url": "https://example.test/pending",
            "state": "not_started",
            "status": "pending",
            "idle_seconds": None,
            "event_counts": {},
        },
    ]
    overview = summarize_snapshot_rows(rows)  # type: ignore[arg-type]
    assert overview == {
        "finished": 1,
        "running": 1,
        "not_started": 1,
        "running_items": [{"name": "Runner", "state": "running", "idle_seconds": pytest.approx(3.0)}],
        "omitted_running": 0,
    }


# ── The Russian message builder ───────────────────────────────────────────


def test_snapshot_progress_line_pins_the_goodreads_transcript() -> None:
    line = snapshot_progress_line(
        {"version": PROGRESS_SNAPSHOT_VERSION, "stage": "extraction", "status": "running", "rows": _goodreads_rows()}
    )
    assert line == (
        "Обработка источников: готово 10, выполняется 2, ожидают 17. "
        "Выполняется: Goodreads Choice Awards 2025 Nonfiction (без событий 183 с, возможна остановка), "
        "NPR (без событий 12 с)."
    )


def test_snapshot_progress_line_pins_the_capture_transcript() -> None:
    line = snapshot_progress_line(
        {"version": PROGRESS_SNAPSHOT_VERSION, "stage": "capture", "status": "running", "rows": _capture_rows()}
    )
    # Capture rows carry elapsed in-flight time, not event-idle time, so the
    # transcript says "в работе" and never the false "без событий" (issue #452).
    # The possible-stall warning is extraction event-idle only: capture has a
    # 10-second source timeout, so the 120-second stall threshold can never
    # mean anything for capture and the label is never appended.
    assert line == (
        "Проверка источников: готово 1, выполняется 1, ожидают 3. "
        "Выполняется: Goodreads Choice Awards 2025 Nonfiction (в работе 125 с)."
    )


def test_snapshot_progress_line_never_labels_capture_rows_as_stalled() -> None:
    """Even a constructed malformed out-of-bound capture row stays unlabelled."""
    rows = _capture_rows(in_flight_elapsed=presentation_warning_threshold_seconds() + 600)
    line = snapshot_progress_line({"stage": "capture", "rows": rows})
    assert "возможна остановка" not in line
    assert "в работе" in line
    assert "без событий" not in line


def test_snapshot_progress_line_never_labels_a_capture_payload_with_running_state() -> None:
    """The stall gate is the snapshot stage, not the inferred row wording.

    Even a malformed constructed capture payload that uses ``state="running"``
    instead of "in_flight" must never show the possible-stall label.
    """
    rows = _goodreads_rows(goodreads_idle=presentation_warning_threshold_seconds() + 600)
    line = snapshot_progress_line({"stage": "capture", "rows": rows})
    assert "возможна остановка" not in line
    assert "без событий" in line


def test_snapshot_progress_line_never_labels_an_extraction_payload_with_in_flight_state() -> None:
    """The stall gate needs both the extraction stage and a running row state.

    Even a malformed extraction payload that uses capture-style
    "in_flight" state with elapsed time must never be labelled by the
    extraction event-idle threshold.
    """
    rows = _capture_rows(in_flight_elapsed=presentation_warning_threshold_seconds() + 600)
    line = snapshot_progress_line({"stage": "extraction", "rows": rows})
    assert "возможна остановка" not in line
    assert "в работе" in line


def test_snapshot_progress_line_labels_a_valid_extraction_running_row_at_the_threshold() -> None:
    """Valid extraction running rows keep the warning at the derived threshold."""
    threshold = presentation_warning_threshold_seconds()
    at_threshold = snapshot_progress_line({"stage": "extraction", "rows": _goodreads_rows(goodreads_idle=threshold)})
    assert "возможна остановка" in at_threshold


def test_snapshot_progress_line_keeps_extraction_wording_for_running_rows() -> None:
    line = snapshot_progress_line(
        {"version": PROGRESS_SNAPSHOT_VERSION, "stage": "extraction", "status": "running", "rows": _goodreads_rows()}
    )
    assert "Goodreads Choice Awards 2025 Nonfiction (без событий 183 с, возможна остановка)" in line
    assert "NPR (без событий 12 с)" in line
    assert "в работе" not in line


def test_snapshot_progress_line_labels_stall_only_at_or_above_the_derived_threshold() -> None:
    threshold = presentation_warning_threshold_seconds()
    at_threshold = snapshot_progress_line({"stage": "extraction", "rows": _goodreads_rows(goodreads_idle=threshold)})
    below_threshold = snapshot_progress_line(
        {"stage": "extraction", "rows": _goodreads_rows(goodreads_idle=threshold - 1)}
    )
    assert "возможна остановка" in at_threshold
    assert "возможна остановка" not in below_threshold
    assert f"(без событий {int(threshold - 1)} с)" in below_threshold


def test_snapshot_progress_line_reports_omitted_running_items() -> None:
    rows = _goodreads_rows(finished=1, not_started=1)
    rows.extend(
        {
            "source_name": f"Runner {index}",
            "source_url": f"https://example.test/runner-{index}",
            "state": "running",
            "status": "running",
            "idle_seconds": 4.0,
            "event_counts": {},
        }
        for index in range(5)
    )
    line = snapshot_progress_line({"stage": "extraction", "rows": rows})
    assert line.endswith("и ещё 4.")
    assert "Runner 1" not in line
    assert "Runner 0" in line


def test_snapshot_progress_line_handles_missing_idle_and_empty_snapshots() -> None:
    line = snapshot_progress_line(
        {
            "stage": "extraction",
            "rows": [
                {
                    "source_name": "Mystery",
                    "source_url": "https://example.test/m",
                    "state": "running",
                    "idle_seconds": None,
                }
            ],
        }
    )
    assert line == "Обработка источников: готово 0, выполняется 1, ожидают 0. Выполняется: Mystery."
    empty = snapshot_progress_line({"stage": "extraction", "rows": []})
    assert empty == "Обработка источников: готово 0, выполняется 0, ожидают 0."
    fallback = snapshot_progress_line({"stage": "", "rows": []})
    assert fallback.startswith("Прогресс: ")


def test_existing_stage_and_extraction_wording_is_preserved() -> None:
    assert stage_message("capture") == "Проверяю источники..."
    assert stage_message("codex") == "Обрабатываю полученные данные..."
    assert stage_message("finalize") == "Подготавливаю результаты поиска..."
    assert extraction_progress_message(10, 29) == "Обработано 10 из 29 источников..."
    assert extraction_progress_message(10, 29, timed_out=1, retrying=2) == (
        "Обработано 10 из 29 источников (не успели: 1) (повторная попытка: 2)..."
    )


# ── Writer-side cadence, redaction, and failure containment ───────────────


def _running_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"stage": "extraction", "status": "running", "elapsed_seconds": 1.0, "rows": rows}


def test_writer_presents_the_first_running_snapshot_immediately_then_throttles(tmp_path: Path) -> None:
    clock = _ManualClock()
    presented: list[dict[str, Any]] = []
    writer = ProgressSnapshotWriter(
        tmp_path, monotonic=clock, presentation=presented.append, failure_recorder=lambda stage, reason: None
    )

    writer.publish(now=0.0, **_running_payload(_goodreads_rows()))
    assert len(presented) == 1

    # Forced file publishes never produce one presentation line each.
    for moment in (10.0, 30.0, 59.9):
        writer.publish(now=moment, force=True, **_running_payload(_goodreads_rows()))
    assert len(presented) == 1

    writer.publish(now=60.0, force=True, **_running_payload(_goodreads_rows()))
    assert len(presented) == 2
    assert presented[0]["stage"] == "extraction"
    assert presented[0]["version"] == PROGRESS_SNAPSHOT_VERSION


def test_writer_presentation_is_independent_of_the_file_publish_throttle(tmp_path: Path) -> None:
    clock = _ManualClock()
    presented: list[dict[str, Any]] = []
    writer = ProgressSnapshotWriter(tmp_path, monotonic=clock, presentation=presented.append)

    # The presentation cadence is independent of the 12-second file throttle:
    # a non-forced publish still reaches the presentation boundary on the first
    # running snapshot, and presenting does not advance the file-publish clock.
    assert writer.publish(now=0.0, force=False, **_running_payload(_goodreads_rows()))
    assert len(presented) == 1
    assert not writer.publish(now=0.5, force=False, **_running_payload(_goodreads_rows()))
    assert len(presented) == 1


def test_writer_stays_silent_without_running_items(tmp_path: Path) -> None:
    presented: list[dict[str, Any]] = []
    writer = ProgressSnapshotWriter(tmp_path, monotonic=_ManualClock(), presentation=presented.append)
    rows = _goodreads_rows()
    for row in rows:
        row["state"] = "finished"
    writer.publish(now=0.0, **_running_payload(rows))
    writer.publish(now=120.0, **_running_payload(rows))
    assert presented == []


def test_writer_redacts_the_snapshot_before_the_presentation_callback(tmp_path: Path) -> None:
    secret = "sk-" + "a" * 24
    rows = _goodreads_rows()
    rows[0]["source_url"] = f"https://example.test/finished-0?token={secret}"
    presented: list[dict[str, Any]] = []
    writer = ProgressSnapshotWriter(tmp_path, monotonic=_ManualClock(), presentation=presented.append)
    writer.publish(now=0.0, **_running_payload(rows))
    assert len(presented) == 1
    assert secret not in json.dumps(presented[0], ensure_ascii=False)


def test_raising_presentation_callback_is_recorded_once_per_stage_and_never_raises(tmp_path: Path) -> None:
    records: list[tuple[str, str]] = []

    def raising_presentation(_snapshot: dict[str, Any]) -> None:
        raise RuntimeError("presentation exploded")

    writer = ProgressSnapshotWriter(
        tmp_path,
        monotonic=_ManualClock(),
        presentation=raising_presentation,
        failure_recorder=lambda s, r: records.append((s, r)),
    )
    for moment in (0.0, 60.0, 120.0):
        assert writer.publish(now=moment, **_running_payload(_goodreads_rows()))
    assert len(records) == 1
    assert records[0][0] == "extraction"
    assert "RuntimeError" in records[0][1]


def test_raising_presentation_callback_is_retried_on_the_next_publish(tmp_path: Path) -> None:
    """A failed attempt does not consume the heartbeat slot (review finding)."""
    attempts: list[dict[str, Any]] = []
    records: list[tuple[str, str]] = []

    def raising_presentation(snapshot: dict[str, Any]) -> None:
        attempts.append(snapshot)
        raise RuntimeError("presentation exploded")

    writer = ProgressSnapshotWriter(
        tmp_path,
        monotonic=_ManualClock(),
        presentation=raising_presentation,
        failure_recorder=lambda s, r: records.append((s, r)),
    )
    # Two publishes 12 seconds apart: inside the 60-second heartbeat, so a
    # successful callback would present only once. The raising callback must
    # be attempted at every publish instead of being suppressed for 60 s.
    assert writer.publish(now=0.0, **_running_payload(_goodreads_rows()))
    assert writer.publish(now=12.0, **_running_payload(_goodreads_rows()))
    assert len(attempts) == 2
    assert len(records) == 1  # the once-per-stage failure recorder
    assert records[0][0] == "extraction"
    # File publication and the publish result stay successful throughout.
    assert json.loads((tmp_path / "progress-snapshot.json").read_text(encoding="utf-8"))["status"] == "running"


def test_failed_presentation_retries_no_faster_than_the_snapshot_interval(tmp_path: Path) -> None:
    """A raising callback is retried at the file-publish interval, not per publish."""
    attempts: list[dict[str, Any]] = []
    presented: list[dict[str, Any]] = []
    records: list[tuple[str, str]] = []

    def flaky_presentation(snapshot: dict[str, Any]) -> None:
        attempts.append(snapshot)
        if len(attempts) <= 2:
            raise RuntimeError("presentation exploded")
        presented.append(snapshot)

    writer = ProgressSnapshotWriter(
        tmp_path,
        monotonic=_ManualClock(),
        presentation=flaky_presentation,
        failure_recorder=lambda s, r: records.append((s, r)),
    )
    # Fail at time 0; the once-per-stage recorder notes it once.
    assert writer.publish(now=0.0, **_running_payload(_goodreads_rows()))
    assert len(attempts) == 1
    assert len(records) == 1
    # A forced publish before the 12-second snapshot interval does not retry.
    writer.publish(now=5.0, force=True, **_running_payload(_goodreads_rows()))
    assert len(attempts) == 1
    # At 12 seconds the failed attempt is retried and still recorded once.
    assert writer.publish(now=12.0, force=True, **_running_payload(_goodreads_rows()))
    assert len(attempts) == 2
    assert len(records) == 1
    # A later successful callback presents and consumes the 60-second
    # success heartbeat.
    writer.publish(now=24.0, force=True, **_running_payload(_goodreads_rows()))
    assert len(attempts) == 3
    assert len(presented) == 1
    writer.publish(now=30.0, force=True, **_running_payload(_goodreads_rows()))
    writer.publish(now=59.0, force=True, **_running_payload(_goodreads_rows()))
    assert len(presented) == 1
    writer.publish(now=84.0, force=True, **_running_payload(_goodreads_rows()))
    assert len(presented) == 2


def test_successful_presentation_stays_throttled_to_one_line_per_heartbeat(tmp_path: Path) -> None:
    presented: list[dict[str, Any]] = []
    writer = ProgressSnapshotWriter(tmp_path, monotonic=_ManualClock(), presentation=presented.append)
    writer.publish(now=0.0, **_running_payload(_goodreads_rows()))
    writer.publish(now=12.0, force=True, **_running_payload(_goodreads_rows()))
    writer.publish(now=59.0, force=True, **_running_payload(_goodreads_rows()))
    assert len(presented) == 1
    writer.publish(now=60.0, force=True, **_running_payload(_goodreads_rows()))
    assert len(presented) == 2


def test_writer_without_presentation_stays_compatible(tmp_path: Path) -> None:
    writer = ProgressSnapshotWriter(tmp_path, monotonic=_ManualClock())
    assert writer.publish(now=0.0, **_running_payload(_goodreads_rows()))


# ── Acceptance: a raising presentation seam never changes a real extraction ──


def _write_local_worklist(output_dir: Path, count: int = 1) -> list[dict[str, str]]:
    """Construct a local capture/worklist shape like the #431 acceptance tests."""
    import hashlib

    items: list[dict[str, str]] = []
    for index in range(count):
        capture = output_dir / f"capture-{index}.json"
        capture.write_text(
            json.dumps(
                {
                    "source_url": f"https://example.test/source-{index}",
                    "html": f'<a href="https://example.test/book-{index}">Book {index}</a>',
                }
            ),
            encoding="utf-8",
        )
        items.append(
            {
                "source_name": f"Source {index}",
                "source_url": f"https://example.test/source-{index}",
                "source_family": "award",
                "capture_file": capture.name,
                "expected_extraction_file": f"extraction-{index}.json",
                "operation_id": "operation-452",
                "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
            }
        )
    (output_dir / "codex-extraction-worklist.json").write_text(json.dumps({"work_items": items}), encoding="utf-8")
    return items


def test_live_announcing_does_not_renew_the_no_progress_lease(tmp_path: Path) -> None:
    """Acceptance (issue #452): announcing is read-only for the no-progress lease."""

    from autoscout.codex_recovery import CODEX_NO_PROGRESS_TIMEOUT_SECONDS as _lease_timeout
    from autoscout.codex_recovery import CodexCliExtractionAgent
    from autoscout.codex_recovery import CodexProgressLease

    assert _lease_timeout == CODEX_NO_PROGRESS_TIMEOUT_SECONDS
    lease = CodexProgressLease()
    lease.begin(10.0)
    lease.feed(b'{"type":"reasoning"}\n', 11.0)
    agent = CodexCliExtractionAgent()
    clock = _ManualClock()
    presented: list[dict[str, Any]] = []
    writer = ProgressSnapshotWriter(tmp_path, monotonic=clock, presentation=presented.append)

    def observe(idle: float | None, counts: dict[str, int], now: float) -> None:
        writer.publish(
            stage="extraction",
            status="running",
            elapsed_seconds=now,
            rows=[
                {
                    "source_name": "Source",
                    "source_url": "https://example.test/source",
                    "state": "running",
                    "idle_seconds": idle,
                    "event_counts": counts,
                }
            ],
            now=now,
        )

    with agent.progress_observer(observe):
        agent._observe_progress(lease, 12.0)
        agent._observe_progress(lease, 13.0)

    assert presented  # the presentation callback received the snapshot
    assert presented[0]["rows"][0]["event_counts"] == {"reasoning": 1}
    # Announcing never renews the lease: the last progress instant and the
    # event counts stay exactly as the Codex stream fed them.
    assert lease.last_progress_at == 11.0
    assert lease.snapshot_counts() == {"reasoning": 1}
    # The timeout boundary itself is unchanged.
    assert agent._no_progress_timeout_seconds == CODEX_NO_PROGRESS_TIMEOUT_SECONDS


def test_raising_presentation_seam_does_not_change_the_extraction_result(tmp_path: Path) -> None:
    """Acceptance (issue #452): announcements never change the canonical outcome.

    Two equivalent constructed extraction runs - presentation unbound and a
    callback that raises on every presentation - produce byte-identical
    extraction result, canonical artifact, and outcome bytes once only the
    advisory snapshot file is excluded. The raised presentation is recorded
    exactly once through the existing progress snapshot recorder and never
    appears as a stage or canonical failure.
    """
    import threading
    import time as time_module

    from autoscout.codex_recovery import EXTRACTION_OUTCOMES_FILE
    from autoscout.codex_recovery import ExtractionAttempt
    from autoscout.codex_recovery import ExtractionStatus
    from autoscout.progress_snapshot import PROGRESS_SNAPSHOT_FILE

    def raising_presentation(_snapshot: dict[str, Any]) -> None:
        raise RuntimeError("presentation exploded")

    def run_case(output_dir: Path, presentation: Any) -> tuple[bytes, bytes, bytes, list[tuple[str, str]]]:
        output_dir.mkdir()
        items = _write_local_worklist(output_dir, count=1)
        records: list[tuple[str, str]] = []
        release = threading.Event()

        class Agent:
            def extract(self, item: dict[str, str], _capture_path: Path, artifact_path: Path) -> ExtractionAttempt:
                # Hold the item running until the snapshot file shows the
                # running row, so every case observes the same publish shape.
                release.wait(timeout=5)
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
                return ExtractionAttempt(ExtractionStatus.PRODUCED, False)

        adapter = ProductionCodexAdapter(
            Agent(),
            max_attempts=1,
            runtime_budget_seconds=5,
            monotonic=lambda: 0.0,
            sleeper=lambda _seconds: None,
            progress_snapshot_failure_recorder=lambda stage, reason: records.append((stage, reason)),
        )
        if presentation is not None:
            adapter.set_snapshot_progress_presentation(presentation)

        snapshot_path = output_dir / PROGRESS_SNAPSHOT_FILE

        def watch_and_release() -> None:
            deadline = time_module.monotonic() + 5
            while time_module.monotonic() < deadline:
                try:
                    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
                    if any(row.get("state") == "running" for row in payload.get("rows", [])):
                        break
                except (OSError, ValueError):
                    pass
                time_module.sleep(0.005)
            release.set()

        watcher = threading.Thread(target=watch_and_release)
        watcher.start()
        result = adapter.fulfill(output_dir)
        watcher.join(timeout=5)

        result_bytes = json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")
        artifact_bytes = (output_dir / items[0]["expected_extraction_file"]).read_bytes()
        outcome_bytes = (output_dir / EXTRACTION_OUTCOMES_FILE).read_bytes()
        return result_bytes, artifact_bytes, outcome_bytes, records

    unbound_dir = tmp_path / "unbound"
    raising_dir = tmp_path / "raising"
    unbound_result, unbound_artifact, unbound_outcome, unbound_records = run_case(unbound_dir, None)
    raising_result, raising_artifact, raising_outcome, raising_records = run_case(raising_dir, raising_presentation)

    # Byte-identical canonical outcome after excluding only the advisory
    # snapshot file and the external failure-recorder observations.
    assert raising_result == unbound_result
    assert raising_artifact == unbound_artifact
    assert raising_outcome == unbound_outcome
    assert json.loads(unbound_result)["status"] == "ok"

    # The unbound case records nothing; the raising case records exactly one
    # contained presentation failure and no stage or canonical failure.
    assert unbound_records == []
    assert len(raising_records) == 1
    assert raising_records[0][0] == "extraction"
    assert "RuntimeError" in raising_records[0][1]
    final = json.loads((raising_dir / PROGRESS_SNAPSHOT_FILE).read_text(encoding="utf-8"))
    assert final["stage"] == "extraction"
    assert final["status"] == "ok"


# ── Publisher coverage: capture and extraction ────────────────────────────


def test_capture_snapshot_presents_in_flight_rows_on_the_heartbeat_cadence(tmp_path: Path) -> None:
    clock = _ManualClock()
    presented: list[dict[str, Any]] = []
    sources = [
        {"name": "BookPage", "url": "https://example.test/bookpage"},
        {"name": "Goodreads Choice Awards 2025 Nonfiction", "url": "https://www.goodreads.com/choiceawards"},
        {"name": "NPR", "url": "https://example.test/npr"},
        {"name": "Pending 0", "url": "https://example.test/pending-0"},
    ]
    snapshot = _CaptureProgressSnapshot(
        tmp_path,
        sources,
        started=0.0,
        deadline=10_000.0,
        per_source_timeout_seconds=10.0,
        monotonic=clock,
        presentation=presented.append,
    )
    snapshot.initial()
    assert presented == []  # nothing running yet: no heartbeat

    snapshot.begin(1, now=5.0, timeout_seconds=10.0)
    assert len(presented) == 1
    assert presented[0]["stage"] == "capture"
    running_rows = [row for row in presented[0]["rows"] if row["state"] == "in_flight"]
    assert [row["source_name"] for row in running_rows] == ["Goodreads Choice Awards 2025 Nonfiction"]

    snapshot.begin(2, now=30.0, timeout_seconds=10.0)
    assert len(presented) == 1  # inside the 60-second heartbeat window

    snapshot.begin(3, now=70.0, timeout_seconds=10.0)
    assert len(presented) == 2


class _FakeExtractionContext:
    def __init__(self, item: dict[str, Any], idle_seconds: float, event_counts: dict[str, int]) -> None:
        self.item = item
        self.started = True
        self._idle_seconds = idle_seconds
        self._event_counts = event_counts

    def progress_snapshot(self, now: float) -> tuple[float, dict[str, int]]:
        return self._idle_seconds, self._event_counts


def test_extraction_snapshot_presents_running_rows_with_idle_seconds(tmp_path: Path) -> None:
    clock = _ManualClock()
    presented: list[dict[str, Any]] = []
    items = [
        {
            "source_name": "Goodreads Choice Awards 2025 Nonfiction",
            "source_url": "https://www.goodreads.com/choiceawards",
        },
        {"source_name": "NPR", "source_url": "https://example.test/npr"},
    ]
    snapshot = _ExtractionProgressSnapshot(
        tmp_path, items, started=0.0, monotonic=clock, failure_recorder=None, presentation=presented.append
    )
    snapshot.initial(now=0.0)
    assert presented == []  # nothing running yet

    contexts = [
        _FakeExtractionContext(items[0], idle_seconds=183.4, event_counts={"task_started": 1}),
        _FakeExtractionContext(items[1], idle_seconds=12.0, event_counts={"task_started": 2, "page_read": 8}),
    ]
    snapshot.poll(contexts, now=1.0)
    assert len(presented) == 1
    line = snapshot_progress_line(presented[0])
    assert "Goodreads Choice Awards 2025 Nonfiction (без событий 183 с, возможна остановка)" in line
    assert "NPR (без событий 12 с)" in line

    snapshot.poll(contexts, now=30.0)
    assert len(presented) == 1  # throttled inside the heartbeat window

    snapshot.poll(contexts, now=61.0)
    assert len(presented) == 2


def test_production_adapters_accept_the_optional_snapshot_presentation() -> None:
    def presentation(_snapshot: dict[str, Any]) -> None:
        return None

    capture = ProductionCodexCaptureAdapter()
    capture.set_snapshot_progress_presentation(presentation)
    assert capture._snapshot_progress_presentation is presentation

    extraction = ProductionCodexAdapter(agent=None)
    extraction.set_snapshot_progress_presentation(presentation)
    assert extraction._snapshot_progress_presentation is presentation


def test_capture_adapter_threads_the_presentation_into_the_capture_publisher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import autoscout.codex_recovery as codex_recovery_module

    captured: dict[str, Any] = {}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"sources": []}), encoding="utf-8")

    def fake_prepare_codex_extraction(*args: Any, **kwargs: Any) -> int:
        captured["snapshot_progress_presentation"] = kwargs.get("snapshot_progress_presentation")
        return 0

    monkeypatch.setattr(codex_recovery_module, "prepare_codex_extraction", fake_prepare_codex_extraction)
    adapter = ProductionCodexCaptureAdapter()
    presentation = lambda snapshot: None  # noqa: E731
    adapter.set_snapshot_progress_presentation(presentation)
    adapter.prepare(config_path, tmp_path / "out")
    assert captured["snapshot_progress_presentation"] is presentation

    adapter.set_snapshot_progress_presentation(None)
    adapter.prepare(config_path, tmp_path / "out")
    assert captured["snapshot_progress_presentation"] is None


# ── Workflow presentation wiring ──────────────────────────────────────────


class _RecordingAdapter:
    def __init__(self) -> None:
        self.received: Any = None

    def set_snapshot_progress_presentation(self, method: Any) -> None:
        self.received = method


class _PresentationWithHeartbeat:
    def announce(self, stage: str) -> None:
        return None

    def announce_progress_snapshot(self, snapshot: dict[str, Any]) -> None:
        return None


class _LegacyPresentation:
    """An old injected adapter that only implements ``announce``."""

    def announce(self, stage: str) -> None:
        return None


def test_wire_binds_the_optional_heartbeat_method_to_opting_in_adapters() -> None:
    capture = _RecordingAdapter()
    codex = _RecordingAdapter()
    presentation = _PresentationWithHeartbeat()
    adapters = WeeklyWorkflowAdapters(capture=capture, codex=codex, presentation=presentation)
    wire_snapshot_progress_presentation(adapters)
    assert capture.received == presentation.announce_progress_snapshot
    assert codex.received == presentation.announce_progress_snapshot


def test_wire_tolerates_legacy_presentations_and_adapters() -> None:
    capture = _RecordingAdapter()
    adapters = WeeklyWorkflowAdapters(capture=capture, codex=_RecordingAdapter(), presentation=_LegacyPresentation())
    wire_snapshot_progress_presentation(adapters)
    assert capture.received is None

    no_presentation = WeeklyWorkflowAdapters(capture=_RecordingAdapter(), codex=_RecordingAdapter(), presentation=None)
    wire_snapshot_progress_presentation(no_presentation)

    class _AdapterWithoutSetter:
        pass

    bare = WeeklyWorkflowAdapters(
        capture=_AdapterWithoutSetter(), codex=_AdapterWithoutSetter(), presentation=_PresentationWithHeartbeat()
    )
    wire_snapshot_progress_presentation(bare)


def test_noop_presentation_implements_the_heartbeat_no_op() -> None:
    presentation = weekly_workflow._NoOpPresentation()
    presentation.announce_progress_snapshot({"stage": "extraction", "rows": []})


def test_real_presentation_adapter_builds_the_russian_line_and_calls_the_printer() -> None:
    lines: list[str] = []
    adapter = weekly_workflow._RealOpenAdapter(snapshot_progress=lines.append)
    adapter.announce_progress_snapshot(
        {"version": PROGRESS_SNAPSHOT_VERSION, "stage": "extraction", "status": "running", "rows": _goodreads_rows()}
    )
    assert lines == [
        "Обработка источников: готово 10, выполняется 2, ожидают 17. "
        "Выполняется: Goodreads Choice Awards 2025 Nonfiction (без событий 183 с, возможна остановка), "
        "NPR (без событий 12 с)."
    ]

    silent = weekly_workflow._RealOpenAdapter()
    silent.announce_progress_snapshot({"stage": "extraction", "rows": _goodreads_rows()})


def test_weekly_adapters_accept_snapshot_progress() -> None:
    adapters = weekly_workflow.weekly_adapters(_find_repo_root_for_tests(), snapshot_progress=lambda line: None)
    assert isinstance(adapters.presentation, weekly_workflow._RealOpenAdapter)
    adapters.presentation.announce_progress_snapshot({"stage": "extraction", "rows": []})


def _find_repo_root_for_tests() -> Path:
    return Path(__file__).resolve().parent.parent


# ── Real CLI and recovery bindings ────────────────────────────────────────


class _StopAfterAdapters(Exception):
    pass


def test_run_weekly_command_binds_the_line_printer_to_the_real_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    presentation = weekly_workflow._RealOpenAdapter()
    captured: dict[str, Any] = {}

    def fake_weekly_adapters(
        repo_path: Path,
        progress: Any = None,
        extraction_progress: Any = None,
    ) -> Any:
        # Old exact pre-#452 factory signature: the CLI must not require a
        # ``snapshot_progress`` keyword on the factory (issue #452 finding).
        captured["factory_kwargs"] = (progress, extraction_progress)
        return types.SimpleNamespace(presentation=presentation)

    def fake_run_weekly_workflow(*args: Any, **kwargs: Any) -> Any:
        raise _StopAfterAdapters

    monkeypatch.setattr(weekly_workflow, "weekly_adapters", fake_weekly_adapters)
    import autoscout.cli.weekly as main_module

    monkeypatch.setattr(main_module, "run_weekly_workflow", fake_run_weekly_workflow)

    from autoscout.cli.weekly import _run_weekly_command

    exit_code = _run_weekly_command(tmp_path / "config.json", tmp_path / "out")
    assert exit_code == 1  # the sentinel stop surfaces as the usual intervention boundary
    assert len(captured["factory_kwargs"]) == 2

    # The live snapshot printer is bound to the real stdout through the
    # optional presentation setter and survives the CLI redirect.
    capsys.readouterr()
    presentation.announce_progress_snapshot(
        {"version": PROGRESS_SNAPSHOT_VERSION, "stage": "capture", "status": "running", "rows": _capture_rows()}
    )
    stdout = capsys.readouterr().out
    assert "Проверка источников: готово 1, выполняется 1, ожидают 3." in stdout
    assert "в работе 125 с" in stdout


def test_run_weekly_command_tolerates_old_signature_mock_factories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A ``Mock(side_effect=...)`` with the old exact factory signature keeps working."""

    class _AdaptersBundle:
        presentation = None  # legacy presentation without the setter: binding is a no-op

    def factory(repo_path: Path, progress: Any = None, extraction_progress: Any = None) -> Any:
        return _AdaptersBundle()

    import autoscout.cli.weekly as main_module
    from autoscout import russian_workflow

    monkeypatch.setattr(main_module, "run_weekly_workflow", lambda *args, **kwargs: types.SimpleNamespace(outcome=None))
    monkeypatch.setattr(russian_workflow, "render_workflow_result", lambda result, include_progress=True: "обзор готов")

    with mock.patch(
        "autoscout.weekly_workflow.weekly_adapters",
        side_effect=factory,
    ) as factory_mock:
        from autoscout.cli.weekly import _run_weekly_command

        exit_code = _run_weekly_command(tmp_path / "config.json", tmp_path / "out")
    assert exit_code == 0
    factory_mock.assert_called_once()
    assert "snapshot_progress" not in factory_mock.call_args.kwargs


def test_doctor_recovery_binds_the_printer_only_outside_json_only_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    def fake_run_recovery(
        config_path: Path,
        output_dir: Path,
        state_dir: Path | None,
        *,
        confirmed_by_editor: bool = False,
        seams: Any = None,
    ) -> tuple[RecoveryReport, int]:
        captured["seams"] = seams
        return (
            RecoveryReport(
                phase="preview",
                diagnosis={},
                plan={"action": "resume-weekly", "summary": "s", "confirmation_question": "q"},
                confirmation_required=True,
            ),
            3,
        )

    monkeypatch.setattr(doctor_recovery, "run_recovery", fake_run_recovery)

    from autoscout.cli.doctor import _doctor_command

    # json_only stays pure JSON and binds no progress printer.
    exit_code = _doctor_command(
        tmp_path / "config.json", tmp_path / "out", None, json_only=True, recover=True, confirmed_by_editor=True
    )
    assert exit_code == 3
    assert captured["seams"].snapshot_progress is None
    output = capsys.readouterr().out
    assert output.strip().startswith("{")

    # Confirmed non-json recovery binds the heartbeat printer to real stdout.
    exit_code = _doctor_command(
        tmp_path / "config.json", tmp_path / "out", None, json_only=False, recover=True, confirmed_by_editor=True
    )
    assert exit_code == 3
    assert callable(captured["seams"].snapshot_progress)
    capsys.readouterr()
    captured["seams"].snapshot_progress("Проверка источников: готово 1, выполняется 1, ожидают 3.")
    assert "Проверка источников" in capsys.readouterr().out


def test_recovery_default_workflow_runner_binds_the_printer_through_the_setter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    presentation = weekly_workflow._RealOpenAdapter()

    def fake_weekly_adapters(
        repo_root: Path,
        progress: Any = None,
        extraction_progress: Any = None,
    ) -> Any:
        # Old exact pre-#452 factory signature: the recovery runner must not
        # require a ``snapshot_progress`` keyword on the factory (issue #452).
        return types.SimpleNamespace(presentation=presentation)

    monkeypatch.setattr(weekly_workflow, "weekly_adapters", fake_weekly_adapters)
    monkeypatch.setattr(weekly_workflow, "run_weekly_workflow", lambda *args, **kwargs: object())

    printer = lambda line: None  # noqa: E731
    _default_workflow_runner(
        tmp_path / "config.json",
        tmp_path / "out",
        None,
        progress=None,
        extraction_progress=None,
        snapshot_progress=printer,
    )
    assert presentation._snapshot_progress is printer  # type: ignore[attr-defined]


def test_recovery_default_workflow_runner_tolerates_factories_without_the_setter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _AdaptersBundle:
        presentation = None  # legacy presentation without the setter: binding is a no-op

    def fake_weekly_adapters(repo_root: Path, progress: Any = None, extraction_progress: Any = None) -> Any:
        return _AdaptersBundle()

    monkeypatch.setattr(weekly_workflow, "weekly_adapters", fake_weekly_adapters)
    monkeypatch.setattr(weekly_workflow, "run_weekly_workflow", lambda *args, **kwargs: object())

    result = _default_workflow_runner(
        tmp_path / "config.json",
        tmp_path / "out",
        None,
        progress=None,
        extraction_progress=None,
        snapshot_progress=lambda line: None,
    )
    assert result is not None


def test_recovery_seams_carry_the_snapshot_progress_seam() -> None:
    printer = lambda line: None  # noqa: E731
    seams = RecoverySeams(snapshot_progress=printer)
    assert seams.snapshot_progress is printer
    assert RecoverySeams().snapshot_progress is None
