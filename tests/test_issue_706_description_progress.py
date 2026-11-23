"""Tests for issue #706: live progress during the description stages.

The capture/extraction heartbeat (#452) never reached the two description
stages (#640, #641), so most of a weekly run was silent. This file covers:

- The per-candidate snapshot tracker both description stages publish
  through (counts move forward only, heartbeat cadence, thread safety).
- The Russian heartbeat line for the ``enrichment`` and
  ``description-adjudication`` stages (labels, in-flight wording, no stall
  warning), with the capture/extraction output pinned unchanged.
- The file-payload stage budget and the derived snapshot-staleness
  threshold the doctor's run-in-progress verdict reads.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any
from unittest import mock

from state_store_support import seed_state

from autoscout.progress_snapshot import PROGRESS_HEARTBEAT_INTERVAL_SECONDS
from autoscout.progress_snapshot import PROGRESS_SNAPSHOT_FILE
from autoscout.progress_snapshot import CandidateProgressSnapshot
from autoscout.progress_snapshot import snapshot_stale_after_seconds
from autoscout.russian_workflow import snapshot_progress_line


class _ManualClock:
    """Injected monotonic clock for deterministic cadence tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _read_snapshot_payload(output_dir: Path) -> dict[str, Any]:
    return json.loads((output_dir / PROGRESS_SNAPSHOT_FILE).read_text(encoding="utf-8"))


def _description_rows(
    finished: int = 1,
    running: tuple[tuple[str, float], ...] = (("夜の本", 75.0),),
    not_started: int = 1,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index in range(finished):
        rows.append(
            {
                "candidate_key": f"done-{index}",
                "source_name": f"Done {index}",
                "state": "finished",
                "status": "finished",
                "elapsed_seconds": 12.0,
            }
        )
    for key, elapsed in running:
        rows.append(
            {
                "candidate_key": key,
                "source_name": key,
                "state": "in_flight",
                "status": "in_flight",
                "elapsed_seconds": elapsed,
            }
        )
    for index in range(not_started):
        rows.append(
            {
                "candidate_key": f"todo-{index}",
                "source_name": f"Todo {index}",
                "state": "not_started",
                "status": "pending",
                "elapsed_seconds": 0.0,
            }
        )
    return rows


class SnapshotStaleThresholdTests:
    def test_stale_threshold_is_derived_from_the_heartbeat_cadence(self) -> None:
        assert snapshot_stale_after_seconds() == 3 * PROGRESS_HEARTBEAT_INTERVAL_SECONDS


class CandidateProgressSnapshotTests:
    def test_initial_publishes_all_rows_not_started_without_a_heartbeat_line(self, tmp_path: Path) -> None:
        presented: list[dict[str, Any]] = []
        tracker = CandidateProgressSnapshot(
            tmp_path, stage="enrichment", monotonic=_ManualClock(10.0), presentation=presented.append
        )
        tracker.initial([("a", "Alpha"), ("b", "Beta")], now=10.0)

        payload = _read_snapshot_payload(tmp_path)
        assert payload["stage"] == "enrichment"
        assert payload["status"] == "running"
        assert payload["elapsed_seconds"] == 0.0
        assert [row["state"] for row in payload["rows"]] == ["not_started", "not_started"]
        assert [row["source_name"] for row in payload["rows"]] == ["Alpha", "Beta"]
        # No running items, so no heartbeat line.
        assert presented == []

    def test_begin_and_finish_advance_counts_forward_only(self, tmp_path: Path) -> None:
        clock = _ManualClock(100.0)
        presented: list[dict[str, Any]] = []
        tracker = CandidateProgressSnapshot(
            tmp_path, stage="description-adjudication", monotonic=clock, presentation=presented.append
        )
        tracker.initial([("a", "Alpha"), ("b", "Beta"), ("c", "Gamma")], now=100.0)

        clock.advance(5.0)
        tracker.begin("a", now=105.0)
        payload = _read_snapshot_payload(tmp_path)
        assert [(row["candidate_key"], row["state"]) for row in payload["rows"]] == [
            ("a", "in_flight"),
            ("b", "not_started"),
            ("c", "not_started"),
        ]

        clock.advance(10.0)
        tracker.finish("a", now=115.0)
        tracker.begin("b", now=115.0)
        finished_counts = [sum(1 for row in _read_snapshot_payload(tmp_path)["rows"] if row["state"] == "finished")]
        clock.advance(3.0)
        tracker.finish("b", now=118.0)
        finished_counts.append(sum(1 for row in _read_snapshot_payload(tmp_path)["rows"] if row["state"] == "finished"))
        assert finished_counts == [1, 2]

        # A finished row keeps its total elapsed time.
        rows = {row["candidate_key"]: row for row in _read_snapshot_payload(tmp_path)["rows"]}
        assert rows["a"]["elapsed_seconds"] == 10.0
        assert rows["b"]["elapsed_seconds"] == 3.0
        assert rows["c"]["elapsed_seconds"] == 0.0

    def test_repeated_begin_keeps_the_first_start_time(self, tmp_path: Path) -> None:
        clock = _ManualClock(50.0)
        tracker = CandidateProgressSnapshot(tmp_path, stage="enrichment", monotonic=clock)
        tracker.initial([("a", "Alpha")], now=50.0)
        tracker.begin("a", now=55.0)
        tracker.begin("a", now=65.0)
        # poll() is file-throttled: the write lands once past the 12 s file
        # interval, still measured from the first begin.
        tracker.poll(now=70.0)
        tracker.poll(now=80.0)
        rows = _read_snapshot_payload(tmp_path)["rows"]
        assert rows[0]["state"] == "in_flight"
        assert rows[0]["elapsed_seconds"] == 25.0

    def test_unknown_keys_never_raise_from_worker_threads(self, tmp_path: Path) -> None:
        tracker = CandidateProgressSnapshot(tmp_path, stage="enrichment", monotonic=_ManualClock())
        tracker.initial([("a", "Alpha")], now=0.0)
        tracker.begin("ghost", now=1.0)
        tracker.finish("ghost", now=2.0)
        rows = _read_snapshot_payload(tmp_path)["rows"]
        assert [(row["candidate_key"], row["state"]) for row in rows] == [("a", "not_started")]

    def test_blank_titles_fall_back_to_the_unnamed_book_label(self, tmp_path: Path) -> None:
        tracker = CandidateProgressSnapshot(tmp_path, stage="enrichment", monotonic=_ManualClock())
        tracker.initial([("a", "  "), ("b", "")], now=0.0)
        rows = _read_snapshot_payload(tmp_path)["rows"]
        assert [row["source_name"] for row in rows] == ["книга без названия", "книга без названия"]

    def test_final_publishes_the_finished_stage(self, tmp_path: Path) -> None:
        presented: list[dict[str, Any]] = []
        tracker = CandidateProgressSnapshot(
            tmp_path, stage="enrichment", monotonic=_ManualClock(200.0), presentation=presented.append
        )
        tracker.initial([("a", "Alpha")], now=200.0)
        tracker.begin("a", now=210.0)
        tracker.finish("a", now=220.0)
        tracker.final(now=221.0)
        payload = _read_snapshot_payload(tmp_path)
        assert payload["stage"] == "enrichment"
        assert payload["status"] == "finished"
        assert payload["elapsed_seconds"] == 21.0
        assert [row["state"] for row in payload["rows"]] == ["finished"]
        # The final publish names no running items, so no extra heartbeat line
        # beyond the one from the running publish.
        assert len(presented) == 1

    def test_first_running_publish_presents_immediately_then_throttles(self, tmp_path: Path) -> None:
        clock = _ManualClock()
        presented: list[dict[str, Any]] = []
        tracker = CandidateProgressSnapshot(
            tmp_path, stage="enrichment", monotonic=clock, presentation=presented.append
        )
        tracker.initial([("a", "Alpha"), ("b", "Beta")], now=0.0)
        assert presented == []

        tracker.begin("a", now=5.0)
        assert len(presented) == 1
        assert presented[0]["stage"] == "enrichment"

        clock.advance(30.0)
        tracker.finish("a", now=35.0)
        tracker.begin("b", now=35.0)
        assert len(presented) == 1

        clock.advance(30.0)
        tracker.poll(now=65.0)
        assert len(presented) == 2

    def test_concurrent_begin_finish_from_workers_stays_consistent(self, tmp_path: Path) -> None:
        tracker = CandidateProgressSnapshot(tmp_path, stage="description-adjudication")
        keys = [f"candidate-{index}" for index in range(30)]
        tracker.initial([(key, f"Title {key}") for key in keys])

        def _work(key: str) -> None:
            tracker.begin(key)
            tracker.finish(key)

        threads = [threading.Thread(target=_work, args=(key,)) for key in keys]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        rows = _read_snapshot_payload(tmp_path)["rows"]
        assert len(rows) == 30
        assert all(row["state"] == "finished" for row in rows)
        assert sorted(row["candidate_key"] for row in rows) == sorted(keys)

    def test_collect_returns_results_in_submit_order(self, tmp_path: Path) -> None:
        from autoscout.bounded_workers import DaemonThreadPool

        tracker = CandidateProgressSnapshot(tmp_path, stage="enrichment")

        def _slow() -> str:
            time.sleep(0.3)
            return "slow"

        pool = DaemonThreadPool(2, thread_name_prefix="test-706-")
        try:
            futures = [pool.submit(_slow), pool.submit(lambda: "fast")]
            assert tracker.collect(futures, tick_seconds=0.05) == ["slow", "fast"]
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def test_collect_propagates_a_worker_timeout_error(self, tmp_path: Path) -> None:
        """A worker's own TimeoutError must propagate, not spin forever.

        Code-review finding on #706: ``DaemonFuture.result(timeout=…)``
        raises the same bare ``TimeoutError`` for an expired wait slice and
        for the worker's own exception — and since Python 3.10+
        ``TimeoutError`` is an ``OSError`` subclass (``socket.timeout`` is
        an alias), a worker's unwrapped ETIMEDOUT raises that same class.
        Keying the retry on ``future.done()`` is the only honest
        discriminator: a completed future is re-read exactly once and its
        exception propagates, matching the plain ``future.result()`` loop
        that feeds the stage's honest failed-stage report.
        """
        from autoscout.bounded_workers import DaemonThreadPool

        tracker = CandidateProgressSnapshot(tmp_path, stage="enrichment")

        def _worker() -> None:
            time.sleep(0.2)
            raise TimeoutError("worker timed out")

        pool = DaemonThreadPool(1, thread_name_prefix="test-706-collect-")
        try:
            future = pool.submit(_worker)
            outcome: dict[str, BaseException | None] = {}

            def _run() -> None:
                try:
                    tracker.collect([future], tick_seconds=0.05)
                except BaseException as exc:  # the pin is the propagated exception type
                    outcome["exc"] = exc

            thread = threading.Thread(target=_run)
            thread.start()
            thread.join(timeout=2.0)
            assert not thread.is_alive(), "collect spun forever on a completed future"
            assert isinstance(outcome.get("exc"), TimeoutError)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def test_collect_propagates_any_worker_exception_once(self, tmp_path: Path) -> None:
        """Non-timeout worker exceptions keep the plain-loop behavior."""
        from autoscout.bounded_workers import DaemonThreadPool

        tracker = CandidateProgressSnapshot(tmp_path, stage="enrichment")

        def _worker() -> None:
            time.sleep(0.1)
            raise ValueError("worker boom")

        pool = DaemonThreadPool(1, thread_name_prefix="test-706-err-")
        try:
            future = pool.submit(_worker)
            outcome: dict[str, BaseException | None] = {}

            def _run() -> None:
                try:
                    tracker.collect([future], tick_seconds=0.05)
                except BaseException as exc:
                    outcome["exc"] = exc

            thread = threading.Thread(target=_run)
            thread.start()
            thread.join(timeout=2.0)
            assert not thread.is_alive()
            assert isinstance(outcome.get("exc"), ValueError)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def test_collect_polls_while_workers_are_busy(self, tmp_path: Path) -> None:
        from autoscout.bounded_workers import DaemonThreadPool

        presented: list[dict[str, Any]] = []
        tracker = CandidateProgressSnapshot(tmp_path, stage="enrichment", presentation=presented.append)
        tracker.initial([("a", "Alpha")])
        tracker.begin("a")

        def _slow() -> str:
            time.sleep(0.4)
            return "slow"

        pool = DaemonThreadPool(1, thread_name_prefix="test-706-poll-")
        try:
            poll_calls: list[None] = []
            original_poll = tracker.poll

            def _counting_poll(*args: Any, **kwargs: Any) -> None:
                poll_calls.append(None)
                original_poll(*args, **kwargs)

            with mock.patch.object(tracker, "poll", _counting_poll):
                assert tracker.collect([pool.submit(_slow)], tick_seconds=0.05) == ["slow"]
                assert len(poll_calls) >= 1
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def test_empty_worklist_publishes_the_stage_without_lines(self, tmp_path: Path) -> None:
        presented: list[dict[str, Any]] = []
        tracker = CandidateProgressSnapshot(
            tmp_path, stage="enrichment", monotonic=_ManualClock(), presentation=presented.append
        )
        tracker.initial([], now=0.0)
        tracker.final(now=1.0)
        payload = _read_snapshot_payload(tmp_path)
        assert payload["stage"] == "enrichment"
        assert payload["rows"] == []
        assert presented == []

    def test_stage_budget_is_published_in_the_file_payload(self, tmp_path: Path) -> None:
        tracker = CandidateProgressSnapshot(
            tmp_path, stage="description-adjudication", stage_budget_seconds=5400.0, monotonic=_ManualClock()
        )
        tracker.initial([("a", "Alpha")], now=0.0)
        payload = _read_snapshot_payload(tmp_path)
        assert payload["stage_budget_seconds"] == 5400.0

    def test_file_payload_without_a_budget_has_no_budget_key(self, tmp_path: Path) -> None:
        tracker = CandidateProgressSnapshot(tmp_path, stage="enrichment", monotonic=_ManualClock())
        tracker.initial([("a", "Alpha")], now=0.0)
        assert "stage_budget_seconds" not in _read_snapshot_payload(tmp_path)


class DescriptionHeartbeatLineTests:
    def test_enrichment_line_reports_counts_with_in_flight_wording(self) -> None:
        line = snapshot_progress_line({"stage": "enrichment", "rows": _description_rows()})
        assert line == (
            "Составление описаний: готово 1, выполняется 1, ожидают 1. Выполняется: 夜の本 (в работе 75 с)."
        )

    def test_adjudication_line_reports_counts_with_in_flight_wording(self) -> None:
        line = snapshot_progress_line({"stage": "description-adjudication", "rows": _description_rows()})
        assert line == ("Проверка описаний: готово 1, выполняется 1, ожидают 1. Выполняется: 夜の本 (в работе 75 с).")

    def test_description_stages_never_carry_the_stall_warning(self) -> None:
        rows = _description_rows(running=(("Slow Book", 600.0),), finished=0, not_started=0)
        for stage in ("enrichment", "description-adjudication"):
            line = snapshot_progress_line({"stage": stage, "rows": rows})
            assert "возможна остановка" not in line
            assert "(в работе 600 с)" in line

    def test_an_extraction_running_row_without_idle_is_never_in_flight_wording(self) -> None:
        # The in-flight elapsed fallback is reserved for ``in_flight`` rows:
        # a malformed extraction ``running`` row without ``idle_seconds``
        # must never present its elapsed time as event-idle time.
        rows = [
            {"source_name": "NPR", "state": "running", "idle_seconds": None, "elapsed_seconds": 400.0},
            {"source_name": "Done", "state": "finished"},
            {"source_name": "Todo", "state": "not_started"},
        ]
        line = snapshot_progress_line({"stage": "extraction", "rows": rows})
        assert line == "Обработка источников: готово 1, выполняется 1, ожидают 1. Выполняется: NPR."

    def test_capture_and_extraction_lines_are_unchanged(self) -> None:
        capture_rows = [
            {"source_name": "NPR", "state": "in_flight", "elapsed_seconds": 5.0},
            {"source_name": "Done", "state": "finished"},
            {"source_name": "Todo", "state": "pending"},
        ]
        assert snapshot_progress_line({"stage": "capture", "rows": capture_rows}) == (
            "Проверка источников: готово 1, выполняется 1, ожидают 1. Выполняется: NPR (в работе 5 с)."
        )
        extraction_rows = [
            {"source_name": "NPR", "state": "running", "idle_seconds": 12.0},
            {"source_name": "Done", "state": "finished"},
            {"source_name": "Todo", "state": "not_started"},
        ]
        assert snapshot_progress_line({"stage": "extraction", "rows": extraction_rows}) == (
            "Обработка источников: готово 1, выполняется 1, ожидают 1. Выполняется: NPR (без событий 12 с)."
        )


class _FakeEnrichmentDiscovery:
    """One classified discovery per candidate, no network."""

    def __init__(self, url: str) -> None:
        self._url = url

    def discover(self, _candidate: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "url": self._url,
                "source_kind": "publisher",
                "source_family": "publisher_copy",
                "owner_id": "owner-a",
                "source_language": "en",
                "classification": "classified",
                "title": "Found Title",
                "route_kind": "",
            }
        ]


class _FakeEnrichmentFetch:
    def fetch(self, result: Any, **_kwargs: Any) -> dict[str, Any]:
        url = result.url if hasattr(result, "url") else str(result.get("url", ""))
        return {
            "url": url,
            "source_kind": "publisher",
            "source_family": "publisher_copy",
            "owner_id": "owner-a",
            "source_language": "en",
            "title": "Found Title",
            "primary_authors": ["Found Author"],
            "subject_text": "A novel about beauty, family, and the power of art.",
            "subject_key": "beauty family art",
            "checked_date": "2026-09-07",
            "classification": "classified",
        }


class _FakeEnrichmentSynthesizer:
    def synthesize(self, _candidate: Any, _sources: Any) -> Any:
        from autoscout.grounded_description import SynthesisResult

        return SynthesisResult(
            "Книга о красоте, семье и силе искусства. "
            "Второй фрагмент раскрывает документальную основу, называет проверенные "
            "страницы и уточняет редакционный ракурс книги. "
            "Третье предложение закрепляет вывод и показывает, почему книга попала в обзор."
        )


def _enrichment_candidate(index: int) -> dict[str, Any]:
    return {
        "candidate_id": f"enrich-candidate-{index}",
        "identity_key": f"enrich title {index}|enrich author",
        "title": f"Enrich Title {index}",
        "primary_authors": ["Enrich Author"],
        "first_seen_date": "2026-09-07",
        "priority_label": "A",
        "update_delta": "new",
    }


class GroundedHeartbeatIntegrationTests:
    def test_stage_run_with_tracker_publishes_candidate_rows_and_a_line(self, tmp_path: Path) -> None:
        from autoscout.grounded_description import GroundedDescriptionStage

        presented: list[dict[str, Any]] = []
        tracker = CandidateProgressSnapshot(
            tmp_path, stage="enrichment", stage_budget_seconds=900.0, presentation=presented.append
        )
        stage = GroundedDescriptionStage(
            discovery=_FakeEnrichmentDiscovery("https://example.test/book"),
            fetch=_FakeEnrichmentFetch(),  # type: ignore[arg-type]
            synthesizer=_FakeEnrichmentSynthesizer(),  # type: ignore[arg-type]
        )
        artifact = stage.run(
            [_enrichment_candidate(index) for index in range(3)],
            operation_id="issue-706",
            operation_date="2026-09-07",
            force_candidates=True,
            snapshot_tracker=tracker,
        )
        assert len(artifact.outcomes) == 3

        payload = _read_snapshot_payload(tmp_path)
        assert payload["stage"] == "enrichment"
        assert payload["stage_budget_seconds"] == 900.0
        assert len(payload["rows"]) == 3
        assert all(row["state"] == "finished" for row in payload["rows"])
        assert sorted(row["source_name"] for row in payload["rows"]) == [
            "Enrich Title 0",
            "Enrich Title 1",
            "Enrich Title 2",
        ]
        assert len(presented) >= 1
        assert presented[0]["stage"] == "enrichment"
        assert snapshot_progress_line(presented[0]).startswith("Составление описаний:")

    def test_stage_run_without_tracker_writes_no_snapshot(self, tmp_path: Path) -> None:
        from autoscout.grounded_description import GroundedDescriptionStage

        stage = GroundedDescriptionStage(
            discovery=_FakeEnrichmentDiscovery("https://example.test/book"),
            fetch=_FakeEnrichmentFetch(),  # type: ignore[arg-type]
            synthesizer=_FakeEnrichmentSynthesizer(),  # type: ignore[arg-type]
        )
        stage.run(
            [_enrichment_candidate(0)],
            operation_id="issue-706",
            operation_date="2026-09-07",
            force_candidates=True,
        )
        assert not (tmp_path / PROGRESS_SNAPSHOT_FILE).exists()

    def test_enrichment_service_builds_a_tracker_only_after_opt_in(self, tmp_path: Path) -> None:
        from autoscout.grounded_description import GroundedDescriptionStageService

        service = GroundedDescriptionStageService()
        assert service._snapshot_tracker(tmp_path) is None
        service.set_snapshot_progress_presentation(lambda _snapshot: None)
        tracker = service._snapshot_tracker(tmp_path)
        assert tracker is not None
        assert tracker.stage == "enrichment"


def _adjudication_history_record(index: int) -> dict[str, Any]:
    return {
        "candidate_id": f"adjud-candidate-{index:03d}",
        "identity_key": f"adjud title {index}|adjud author",
        "title": f"Adjud Title {index}",
        "primary_authors": ["Adjud Author"],
        "content_description": "",
        "first_seen_date": "2026-09-07",
    }


class AdjudicationHeartbeatIntegrationTests:
    def _run_adjudication(
        self, state_dir: Path, output_dir: Path, tracker: CandidateProgressSnapshot | None
    ) -> dict[str, Any]:
        from autoscout.description_adjudication import ReplayDescriptionAdjudicationAdapter
        from autoscout.description_adjudication import adjudicate_descriptions
        from autoscout.grounded_description import BoundedHTTPFetcher
        from autoscout.grounded_description import NoNetworkHTTPTransport

        seed_state(
            state_dir,
            candidate_history=[_adjudication_history_record(0), _adjudication_history_record(1)],
            candidate_visibility=[],
        )
        return adjudicate_descriptions(
            state_dir,
            output_dir,
            adapter=ReplayDescriptionAdjudicationAdapter(),
            fetcher=BoundedHTTPFetcher(transport=NoNetworkHTTPTransport()),
            apply=False,
            snapshot_tracker=tracker,
        )

    def test_adjudicate_with_tracker_publishes_rows_and_a_line(self, tmp_path: Path) -> None:
        from autoscout.description_adjudication import ADJUDICATION_STAGE_BUDGET_SECONDS

        state_dir = tmp_path / "state"
        output_dir = tmp_path / "run"
        state_dir.mkdir()
        output_dir.mkdir()
        presented: list[dict[str, Any]] = []
        tracker = CandidateProgressSnapshot(
            output_dir,
            stage="description-adjudication",
            stage_budget_seconds=ADJUDICATION_STAGE_BUDGET_SECONDS,
            presentation=presented.append,
        )
        result = self._run_adjudication(state_dir, output_dir, tracker)
        assert result["considered"] == 2

        payload = _read_snapshot_payload(output_dir)
        assert payload["stage"] == "description-adjudication"
        assert payload["stage_budget_seconds"] == ADJUDICATION_STAGE_BUDGET_SECONDS
        assert len(payload["rows"]) == 2
        assert all(row["state"] == "finished" for row in payload["rows"])
        assert sorted(row["source_name"] for row in payload["rows"]) == ["Adjud Title 0", "Adjud Title 1"]
        assert len(presented) >= 1
        assert snapshot_progress_line(presented[0]).startswith("Проверка описаний:")

    def test_adjudicate_without_tracker_writes_no_snapshot(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        output_dir = tmp_path / "run"
        state_dir.mkdir()
        output_dir.mkdir()
        self._run_adjudication(state_dir, output_dir, None)
        assert not (output_dir / PROGRESS_SNAPSHOT_FILE).exists()

    def test_adjudication_service_threads_budget_and_presentation(self, tmp_path: Path) -> None:
        from autoscout.description_adjudication import DescriptionAdjudicationStageService
        from autoscout.description_adjudication import ReplayDescriptionAdjudicationAdapter
        from autoscout.grounded_description import BoundedHTTPFetcher
        from autoscout.grounded_description import NoNetworkHTTPTransport

        state_dir = tmp_path / "state"
        output_dir = tmp_path / "run"
        state_dir.mkdir()
        output_dir.mkdir()
        seed_state(state_dir, candidate_history=[_adjudication_history_record(0)], candidate_visibility=[])

        service = DescriptionAdjudicationStageService(
            adapter=ReplayDescriptionAdjudicationAdapter(),
            fetcher=BoundedHTTPFetcher(transport=NoNetworkHTTPTransport()),
            stage_budget_seconds=123.0,
            max_workers=1,
        )
        presented: list[dict[str, Any]] = []
        service.set_snapshot_progress_presentation(presented.append)
        service.adjudicate_stage(output_dir, state_dir, effective_date="2026-09-07")

        payload = _read_snapshot_payload(output_dir)
        assert payload["stage"] == "description-adjudication"
        assert payload["stage_budget_seconds"] == 123.0
        assert len(presented) >= 1


class HeartbeatWiringTests:
    def test_wire_binds_the_heartbeat_to_the_description_services(self) -> None:
        from autoscout.description_adjudication import DescriptionAdjudicationStageService
        from autoscout.description_adjudication import ReplayDescriptionAdjudicationAdapter
        from autoscout.description_enrichment import DescriptionEnrichmentService
        from autoscout.description_enrichment import NoOpDescriptionEnrichmentAgent
        from autoscout.grounded_description import GroundedDescriptionStageService
        from autoscout.weekly_workflow import WeeklyWorkflowAdapters
        from autoscout.weekly_workflow import wire_snapshot_progress_presentation

        received: list[dict[str, Any]] = []

        class _Presentation:
            def announce_progress_snapshot(self, snapshot: dict[str, Any]) -> None:
                received.append(snapshot)

        enrichment = GroundedDescriptionStageService()
        adjudication = DescriptionAdjudicationStageService(adapter=ReplayDescriptionAdjudicationAdapter())
        legacy_enrichment = DescriptionEnrichmentService(agent=NoOpDescriptionEnrichmentAgent())
        adapters = WeeklyWorkflowAdapters(
            capture=mock.Mock(),
            codex=mock.Mock(),
            presentation=_Presentation(),  # type: ignore[arg-type]
            enrichment=enrichment,  # type: ignore[arg-type]
            adjudication=adjudication,  # type: ignore[arg-type]
        )
        wire_snapshot_progress_presentation(adapters)
        assert enrichment._snapshot_progress_presentation is not None
        assert adjudication._snapshot_progress_presentation is not None
        # The legacy enrichment service has no heartbeat setter and stays silent.
        assert not hasattr(legacy_enrichment, "_snapshot_progress_presentation")


def _write_doctor_config(path: Path) -> Path:
    import json as _json

    config = path / "config.json"
    config.write_text(_json.dumps({"run_date": "2026-09-16", "sources": []}), encoding="utf-8")
    return config


def _live_guard(guard_age_seconds: float | None = 12.0) -> dict[str, Any]:
    guard: dict[str, Any] = {"guard_exists": True, "guard_pid_alive": True, "guard_pid": "99999"}
    if guard_age_seconds is not None:
        guard["guard_age_seconds"] = guard_age_seconds
    return guard


def _running_snapshot_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "candidate_key": "done-1",
            "source_name": "Done Book One",
            "state": "finished",
            "status": "finished",
            "elapsed_seconds": 12.0,
        },
        {
            "candidate_key": "done-2",
            "source_name": "Done Book Two",
            "state": "finished",
            "status": "finished",
            "elapsed_seconds": 30.0,
        },
        {
            "candidate_key": "running-1",
            "source_name": "Running Book",
            "state": "in_flight",
            "status": "in_flight",
            "elapsed_seconds": 75.0,
        },
        {
            "candidate_key": "todo-1",
            "source_name": "Todo Book",
            "state": "not_started",
            "status": "pending",
            "elapsed_seconds": 0.0,
        },
    ]
    return rows


def _write_running_snapshot(output_dir: Path, *, status: str = "running") -> Path:
    import json as _json

    path = output_dir / PROGRESS_SNAPSHOT_FILE
    path.write_text(
        _json.dumps(
            {
                "version": 1,
                "stage": "description-adjudication",
                "status": status,
                "elapsed_seconds": 720.0,
                "stage_budget_seconds": 5400.0,
                "rows": _running_snapshot_rows(),
            }
        ),
        encoding="utf-8",
    )
    return path


def _live_run_seams(guard_age_seconds: float | None = 12.0) -> Any:
    from datetime import date as _date

    from autoscout.doctor import DoctorSeams

    def _inspect(_allowlist: Any) -> dict[str, Any]:
        return {"status": "ok", "branch": "main"}

    return DoctorSeams(
        today=lambda: _date(2026, 9, 16),
        repo_inspect=_inspect,
        codex_probe=lambda: {"codex_available": True, "codex_version": "0.1.0"},
        guard_status=lambda: _live_guard(guard_age_seconds),
    )


class DoctorRunInProgressTests:
    def test_live_lock_with_a_fresh_snapshot_is_running_not_blocked(self, tmp_path: Path) -> None:
        from autoscout.doctor import DiagnosisClass
        from autoscout.doctor import diagnose

        config = _write_doctor_config(tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        _write_running_snapshot(output_dir)

        report = diagnose(config, output_dir, seams=_live_run_seams())
        assert report.diagnosis == DiagnosisClass.RUN_IN_PROGRESS
        assert report.diagnosis.value == "run-in-progress"
        assert "\n" not in report.verdict
        assert "Поиск выполняется" in report.verdict
        assert "Проверка описаний" in report.verdict
        assert "готово 2, выполняется 1, ожидают 1" in report.verdict
        assert "12 мин" in report.verdict
        assert "1 ч 30 мин" in report.verdict
        assert "только что" in report.verdict
        # No escalation: a running run is not a blocker.
        assert report.editor_question == ""
        assert report.recommended_action == "Дождитесь завершения текущего этапа."
        assert "Димой" not in report.verdict
        assert report.facts["stage"] == "description-adjudication"
        assert report.facts["finished"] == 2
        assert report.facts["running"] == 1
        assert report.facts["not_started"] == 1
        assert report.facts["snapshot_stale"] is False
        assert report.results_saved is False

    def test_live_lock_without_a_snapshot_is_still_running(self, tmp_path: Path) -> None:
        from autoscout.doctor import DiagnosisClass
        from autoscout.doctor import diagnose

        config = _write_doctor_config(tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        report = diagnose(config, output_dir, seams=_live_run_seams())
        assert report.diagnosis == DiagnosisClass.RUN_IN_PROGRESS
        assert "Поиск выполняется" in report.verdict
        assert report.editor_question == ""
        assert "Димой" not in report.verdict
        assert report.recommended_action == "Дождитесь завершения текущего этапа."

    def test_live_lock_with_a_malformed_snapshot_is_still_running(self, tmp_path: Path) -> None:
        from autoscout.doctor import DiagnosisClass
        from autoscout.doctor import diagnose

        config = _write_doctor_config(tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        (output_dir / PROGRESS_SNAPSHOT_FILE).write_text("not json", encoding="utf-8")

        report = diagnose(config, output_dir, seams=_live_run_seams())
        assert report.diagnosis == DiagnosisClass.RUN_IN_PROGRESS
        assert report.editor_question == ""

    def test_stale_snapshot_flags_a_possible_hang_without_escalation(self, tmp_path: Path) -> None:
        import os as _os

        from autoscout.doctor import DiagnosisClass
        from autoscout.doctor import diagnose

        config = _write_doctor_config(tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        snapshot_path = _write_running_snapshot(output_dir)
        old = time.time() - 400.0
        _os.utime(snapshot_path, (old, old))
        # The guard is older than the snapshot (a 12-minute run), so the
        # snapshot belongs to this run and merely went quiet past the
        # staleness threshold — flagged, never escalated (issue #706).
        report = diagnose(config, output_dir, seams=_live_run_seams(guard_age_seconds=720.0))
        assert report.diagnosis == DiagnosisClass.RUN_IN_PROGRESS
        assert "возможно, этап завис" in report.verdict
        assert "7 мин" in report.verdict
        assert report.editor_question == ""
        assert report.facts["snapshot_stale"] is True

    def test_prior_run_snapshot_is_not_attributed_to_the_live_run(self, tmp_path: Path) -> None:
        """A snapshot older than the guard describes a previous run.

        Code-review finding on #706: the snapshot file is gitignored and
        never deleted between runs, and its payload carries no absolute
        timestamp, so without the guard-age cross-check a two-day-old
        extraction payload was reported as the live run's stage seconds
        into a brand-new run — the exact stale-payload failure #706
        exists to eliminate.
        """
        import os as _os

        from autoscout.doctor import DiagnosisClass
        from autoscout.doctor import diagnose

        config = _write_doctor_config(tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        snapshot_path = _write_running_snapshot(output_dir)
        old = time.time() - 172800.0  # two days, like the 2026-09-16 file
        _os.utime(snapshot_path, (old, old))
        # The live run took the guard 12 seconds ago.
        report = diagnose(config, output_dir, seams=_live_run_seams(guard_age_seconds=12.0))
        assert report.diagnosis == DiagnosisClass.RUN_IN_PROGRESS
        assert "свежих данных о прогрессе пока нет" in report.verdict
        # The previous run's stage must not be attributed to this run.
        assert "Проверка описаний" not in report.verdict
        assert "возможно, этап завис" not in report.verdict
        assert report.editor_question == ""
        assert report.facts["snapshot_present"] is False
        assert report.facts["prior_run_snapshot"] is True
        assert report.facts["guard_age_seconds"] == 12.0

    def test_fresh_snapshot_with_an_old_guard_is_normal_running(self, tmp_path: Path) -> None:
        """A long healthy run: old lock, live snapshot refreshes."""
        from autoscout.doctor import DiagnosisClass
        from autoscout.doctor import diagnose

        config = _write_doctor_config(tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        _write_running_snapshot(output_dir)

        # A two-hour run holds an old lock; a 12-second-old snapshot proves
        # the stage is alive.
        report = diagnose(config, output_dir, seams=_live_run_seams(guard_age_seconds=7200.0))
        assert report.diagnosis == DiagnosisClass.RUN_IN_PROGRESS
        assert "Проверка описаний" in report.verdict
        assert report.editor_question == ""

    def test_stale_snapshot_with_an_old_guard_escalates(self, tmp_path: Path) -> None:
        """No live progress plus a lock past the stale threshold escalates.

        Code-review finding on #706: a recycled or foreign PID (EPERM reads
        as alive) after a crash leaves a lock that remove-stale-lock rightly
        refuses. Without this corner the editor waits forever with no way
        out, so the old #317 Level 3 question is restored when the run
        shows no live sign of progress and the lock is old.
        """
        import os as _os

        from autoscout.doctor import DiagnosisClass
        from autoscout.doctor import diagnose
        from autoscout.doctor import exit_code_for

        config = _write_doctor_config(tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        snapshot_path = _write_running_snapshot(output_dir)
        old = time.time() - 400.0
        _os.utime(snapshot_path, (old, old))

        report = diagnose(config, output_dir, seams=_live_run_seams(guard_age_seconds=7200.0))
        assert report.diagnosis == DiagnosisClass.REPOSITORY_BLOCKER
        assert "Связаться с Димой" in report.editor_question
        assert "Димой" in report.recommended_action
        assert exit_code_for(report) == 1
        assert report.facts["snapshot_stale"] is True
        assert report.facts["guard_stale"] is True

    def test_missing_snapshot_with_an_old_guard_escalates(self, tmp_path: Path) -> None:
        from autoscout.doctor import DiagnosisClass
        from autoscout.doctor import diagnose

        config = _write_doctor_config(tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()

        report = diagnose(config, output_dir, seams=_live_run_seams(guard_age_seconds=7200.0))
        assert report.diagnosis == DiagnosisClass.REPOSITORY_BLOCKER
        assert report.editor_question.endswith("?")

    def test_finished_prior_run_snapshot_without_guard_age_is_not_attributed(self, tmp_path: Path) -> None:
        """A finished, stale payload with no guard age reads as prior-run.

        Without a guard age no cross-check is possible, so the fallback is
        the payload's own terminal status: a finished stage the run left
        behind cannot be the running stage.
        """
        import os as _os

        from autoscout.doctor import DiagnosisClass
        from autoscout.doctor import diagnose

        config = _write_doctor_config(tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        snapshot_path = _write_running_snapshot(output_dir, status="finished")
        old = time.time() - 400.0
        _os.utime(snapshot_path, (old, old))

        report = diagnose(config, output_dir, seams=_live_run_seams(guard_age_seconds=None))
        assert report.diagnosis == DiagnosisClass.RUN_IN_PROGRESS
        assert "свежих данных о прогрессе пока нет" in report.verdict
        assert "Проверка описаний" not in report.verdict

    def test_run_in_progress_renders_without_a_review_line_or_question(self, tmp_path: Path) -> None:
        from autoscout.doctor import diagnose
        from autoscout.doctor import render_json_report
        from autoscout.doctor import render_russian

        config = _write_doctor_config(tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        _write_running_snapshot(output_dir)

        report = diagnose(config, output_dir, seams=_live_run_seams())
        russian = render_russian(report)
        assert "Поиск выполняется" in russian
        assert "Дождитесь завершения текущего этапа." in russian
        assert "?" not in russian
        assert "Обзор недоступен" not in russian
        assert "Результаты сохранены" not in russian
        assert '"diagnosis": "run-in-progress"' in render_json_report(report)

    def test_run_in_progress_needs_no_recovery_and_exits_zero(self, tmp_path: Path) -> None:
        from autoscout.doctor import diagnose
        from autoscout.doctor import exit_code_for
        from autoscout.doctor_recovery import RecoveryAction
        from autoscout.doctor_recovery import plan_recovery

        config = _write_doctor_config(tmp_path)
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        _write_running_snapshot(output_dir)

        report = diagnose(config, output_dir, seams=_live_run_seams())
        assert exit_code_for(report) == 0
        plan = plan_recovery(report)
        assert plan.action == RecoveryAction.NONE
        assert plan.confirmation_question == ""
