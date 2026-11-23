"""Parallel Kirkus progress stays visible under one weekly heartbeat cadence."""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest

from autoscout.doctor import DiagnosisClass
from autoscout.doctor import DoctorSeams
from autoscout.doctor import diagnose
from autoscout.progress_snapshot import PROGRESS_SNAPSHOT_FILE
from autoscout.progress_snapshot import ProgressSnapshotWriter
from autoscout.russian_workflow import snapshot_progress_line
from autoscout.weekly_progress import KIRKUS_PROGRESS_SNAPSHOT_FILE
from autoscout.weekly_progress import KIRKUS_WAIT_STAGE
from autoscout.weekly_progress import WeeklyProgressPresentation


def _main(stage="extraction", *, status="running"):
    return {
        "stage": stage,
        "status": status,
        "elapsed_seconds": 120.0,
        "stage_budget_seconds": 1200.0,
        "rows": [
            {"source_name": "Done", "state": "finished"},
            {"source_name": "Current", "state": "in_flight", "elapsed_seconds": 10.0},
            {"source_name": "Next", "state": "not_started"},
        ],
    }


def _kirkus(*, status="running", pages=4):
    return {
        "stage": "kirkus-precollection",
        "status": status,
        "elapsed_seconds": 180.0,
        "stage_budget_seconds": 2400.0,
        "rows": [{"source_name": "Kirkus", "state": "finished"} for _ in range(pages - 1)]
        + [{"source_name": "Kirkus", "state": "in_flight" if status == "running" else "finished"}],
    }


def _writers(tmp_path, clock, presentation):
    progress = WeeklyProgressPresentation(tmp_path, presentation, monotonic=lambda: clock[0])
    main = ProgressSnapshotWriter(tmp_path, monotonic=lambda: clock[0], presentation=progress)
    kirkus = ProgressSnapshotWriter(tmp_path, monotonic=lambda: clock[0], presentation=progress, write_snapshot=False)
    return progress, main, kirkus


@pytest.mark.parametrize("stage,label", [("capture", "Проверка источников"), ("extraction", "Обработка источников")])
def test_combined_line_and_independent_atomic_snapshots(tmp_path, stage, label):
    lines = []
    _, main, kirkus = _writers(tmp_path, [0.0], lambda snapshot: lines.append(snapshot_progress_line(snapshot)))
    kirkus.publish(**_kirkus())
    assert not lines  # The first combined line waits for main-stage counts.
    main.publish(**_main(stage))
    assert len(lines) == 1
    assert lines[0].startswith(f"{label}: готово 1, выполняется 1, ожидают 1.")
    assert lines[0].endswith("Сбор Kirkus: запрошено страниц 4.")
    assert "\n" not in lines[0]
    assert json.loads((tmp_path / PROGRESS_SNAPSHOT_FILE).read_text())["stage"] == stage
    assert json.loads((tmp_path / KIRKUS_PROGRESS_SNAPSHOT_FILE).read_text())["elapsed_seconds"] == 180.0


def test_one_sixty_second_limit_across_threads_and_stage_writers(tmp_path):
    clock = [0.0]
    emissions = []
    callback_entered = threading.Event()
    callback_release = threading.Event()

    def present(snapshot):
        emissions.append((clock[0], snapshot_progress_line(snapshot)))
        callback_entered.set()
        assert callback_release.wait(5)

    progress, main, kirkus = _writers(tmp_path, clock, present)
    kirkus.publish(**_kirkus())
    with ThreadPoolExecutor(2) as pool:
        # Hold the first callback open while the other thread attempts an
        # update: it must not claim a second heartbeat slot before it returns.
        first = pool.submit(main.publish, **_main("capture"))
        assert callback_entered.wait(5)
        attempted = threading.Event()

        def concurrent_update():
            attempted.set()
            return kirkus.publish(**_kirkus(pages=5))

        second = pool.submit(concurrent_update)
        assert attempted.wait(5)
        callback_release.set()
        assert first.result(timeout=5) and second.result(timeout=5)
        # Extraction creates a fresh writer, but not a fresh cadence.
        main = ProgressSnapshotWriter(tmp_path, monotonic=lambda: clock[0], presentation=progress)
        for now in (59.0, 60.0, 61.0, 119.0, 120.0):
            clock[0] = now
            barrier = threading.Barrier(2)

            def publish(writer, snapshot, barrier=barrier):
                barrier.wait(timeout=5)
                return writer.publish(**snapshot)

            futures = [pool.submit(publish, main, _main()), pool.submit(publish, kirkus, _kirkus(pages=6))]
            assert all(future.result(timeout=5) for future in futures)
    assert [at for at, _ in emissions] == [0.0, 60.0, 120.0]
    assert all("Сбор Kirkus: запрошено страниц" in line for _, line in emissions)
    assert "страниц 6." in emissions[-1][1]


def test_waiting_line_persists_and_terminal_pass_stops_appearing(tmp_path):
    clock = [0.0]
    lines = []
    progress, main, kirkus = _writers(tmp_path, clock, lambda snapshot: lines.append(snapshot_progress_line(snapshot)))
    kirkus.publish(**_kirkus())
    main.publish(**_main())
    clock[0] = 10.0
    main.publish(stage=KIRKUS_WAIT_STAGE, status="running", elapsed_seconds=1.0, rows=[])
    assert len(lines) == 1  # Waiting does not bypass the cadence either.
    clock[0] = 60.0
    main.publish(stage=KIRKUS_WAIT_STAGE, status="running", elapsed_seconds=51.0, rows=[])
    assert lines[-1] == "Ожидание завершения сбора Kirkus. Сбор Kirkus: запрошено страниц 4."
    assert json.loads((tmp_path / PROGRESS_SNAPSHOT_FILE).read_text())["stage"] == KIRKUS_WAIT_STAGE
    kirkus.publish(**_kirkus(status="finished"))
    assert json.loads((tmp_path / KIRKUS_PROGRESS_SNAPSHOT_FILE).read_text())["status"] == "finished"
    progress.finish_kirkus()
    # A timed-out worker's late updates must not resurrect the pass.
    kirkus.publish(**_kirkus(pages=5))
    clock[0] = 120.0
    main.publish(**_main())
    assert "Kirkus" not in lines[-1]
    assert json.loads((tmp_path / KIRKUS_PROGRESS_SNAPSHOT_FILE).read_text())["status"] == "finished"


def test_finished_main_is_not_reannounced_by_background_updates(tmp_path):
    clock = [0.0]
    lines = []
    _, main, kirkus = _writers(tmp_path, clock, lines.append)
    main.publish(**_main())
    main.publish(**_main(status="finished"))
    clock[0] = 60.0
    kirkus.publish(**_kirkus())
    assert len(lines) == 1


@pytest.mark.parametrize("stage", ["capture", "extraction", KIRKUS_WAIT_STAGE])
def test_doctor_reports_both_live_stages_without_escalation(tmp_path, stage):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"run_date": "2026-10-28", "sources": []}))
    _, main, kirkus = _writers(tmp_path, [0.0], None)
    snapshot = _main(stage)
    if stage == KIRKUS_WAIT_STAGE:
        snapshot["rows"] = []
    main.publish(**snapshot)
    kirkus.publish(**_kirkus())
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    seams = DoctorSeams(
        today=lambda: date(2026, 10, 28),
        guard_status=lambda: {"guard_exists": True, "guard_pid_alive": True, "guard_age_seconds": 300.0},
    )
    report = diagnose(config, tmp_path, seams=seams)
    assert report.diagnosis == DiagnosisClass.RUN_IN_PROGRESS
    assert report.editor_question == ""
    assert report.facts["stage"] == stage
    expected_count = 0 if stage == KIRKUS_WAIT_STAGE else 1
    assert report.facts["finished"] == expected_count
    assert report.facts["running"] == expected_count
    assert report.facts["not_started"] == expected_count
    assert report.facts["elapsed_seconds"] == 120.0
    assert report.facts["kirkus"]["finished"] == 3
    assert report.facts["kirkus"]["running"] == 1
    assert report.facts["kirkus"]["elapsed_seconds"] == 180.0
    assert "Сбор Kirkus: запрошено страниц 4." in report.verdict
    assert "Этап идёт 2 мин из 20 мин." in report.verdict
    assert "Сбор Kirkus идёт 3 мин из 40 мин." in report.verdict
    assert "Kirkus: обновлено только что." in report.verdict
    assert before == {path.name: path.read_bytes() for path in tmp_path.iterdir()}


@pytest.mark.parametrize("sidecar", ["prior-run", "finished", "malformed", "missing"])
def test_doctor_ignores_prior_or_unavailable_background_progress(tmp_path, sidecar):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"run_date": "2026-10-28", "sources": []}))
    _, main, kirkus = _writers(tmp_path, [0.0], None)
    main.publish(**_main())
    if sidecar != "missing":
        kirkus.publish(**_kirkus(status="finished" if sidecar == "finished" else "running"))
    path = tmp_path / KIRKUS_PROGRESS_SNAPSHOT_FILE
    if sidecar == "prior-run":
        old = path.stat().st_mtime - 600.0
        os.utime(path, (old, old))
    elif sidecar == "malformed":
        path.write_text("{")
    report = diagnose(
        config,
        tmp_path,
        seams=DoctorSeams(
            guard_status=lambda: {"guard_exists": True, "guard_pid_alive": True, "guard_age_seconds": 300.0}
        ),
    )
    assert report.diagnosis == DiagnosisClass.RUN_IN_PROGRESS
    assert report.editor_question == ""
    assert "kirkus" not in report.facts
    assert "Kirkus" not in report.verdict


def test_workflow_publishes_wait_before_joining_the_pass(tmp_path, monkeypatch):
    from autoscout import source_collection
    from autoscout.weekly_workflow import WeeklyWorkflow
    from tests.test_issue_880_parallel_kirkus import _adapters
    from tests.test_issue_880_parallel_kirkus import _config
    from tests.test_issue_880_parallel_kirkus import _Pass

    original_capture = source_collection.codex_source_page

    def offline_capture(config, output_dir, source, *, live=False):
        return original_capture(config, output_dir, source, live=False if source["name"] == "Test Source" else live)

    monkeypatch.setattr(source_collection, "codex_source_page", offline_capture)
    waiting_published = threading.Event()

    class WaitingPass(_Pass):
        def collect(self, *args, **options):
            self.started.set()
            assert self.capture_started.wait(5)
            assert self.main_finished.wait(5)
            assert waiting_published.wait(5), "workflow joined the pass without publishing its waiting state"
            return super().collect(*args, **options)

    config = _config(tmp_path)
    stage = WaitingPass("after")
    output_dir = tmp_path / "run"
    original = ProgressSnapshotWriter.publish
    waiting_snapshots = []

    def publish(writer, **snapshot):
        result = original(writer, **snapshot)
        if snapshot["stage"] == KIRKUS_WAIT_STAGE:
            waiting_snapshots.append(json.loads((output_dir / PROGRESS_SNAPSHOT_FILE).read_text()))
            waiting_published.set()
        return result

    monkeypatch.setattr(ProgressSnapshotWriter, "publish", publish)
    result = WeeklyWorkflow(_adapters(stage)).run(config, output_dir)
    assert result.outcome.value.startswith("completed/")
    assert waiting_snapshots and waiting_snapshots[0]["stage"] == KIRKUS_WAIT_STAGE
    assert json.loads((output_dir / KIRKUS_PROGRESS_SNAPSHOT_FILE).read_text())["status"] == "finished"


def test_sidecar_failure_is_advisory_and_recorded_once(tmp_path, monkeypatch):
    failures = []
    lines = []
    progress = WeeklyProgressPresentation(tmp_path, lines.append, failure_recorder=lambda *args: failures.append(args))

    def fail(*args):
        raise OSError("test sidecar failure")

    monkeypatch.setattr("autoscout.weekly_progress.write_json", fail)
    progress.start_kirkus(2400.0)
    writer = ProgressSnapshotWriter(tmp_path, presentation=progress)
    assert writer.publish(**_main())
    progress.update_snapshot(_kirkus())
    progress.finish_kirkus()
    assert len(failures) == 1
    assert failures[0][0] == "kirkus-precollection"
    assert len(lines) == 1
    assert json.loads((tmp_path / PROGRESS_SNAPSHOT_FILE).read_text())["stage"] == "extraction"


def test_finishing_pass_does_not_emit_an_empty_waiting_line(tmp_path):
    clock = [0.0]
    lines = []
    _, main, kirkus = _writers(tmp_path, clock, lambda snapshot: lines.append(snapshot_progress_line(snapshot)))
    kirkus.publish(**_kirkus())
    main.publish(stage=KIRKUS_WAIT_STAGE, status="running", elapsed_seconds=1.0, rows=[])
    clock[0] = 60.0
    kirkus.publish(**_kirkus(status="finished"))
    assert lines == ["Ожидание завершения сбора Kirkus. Сбор Kirkus: запрошено страниц 4."]


def test_failing_presenter_retries_under_one_shared_limit(tmp_path):
    clock = [0.0]
    calls = []
    failures = []

    def fail(snapshot):
        calls.append(clock[0])
        raise RuntimeError("test presentation failure")

    progress = WeeklyProgressPresentation(tmp_path, fail, monotonic=lambda: clock[0])
    main = ProgressSnapshotWriter(tmp_path, presentation=progress, failure_recorder=lambda *args: failures.append(args))
    kirkus = ProgressSnapshotWriter(tmp_path, presentation=progress, write_snapshot=False)
    kirkus.publish(**_kirkus())
    for now in (0.0, 1.0, 11.0, 12.0, 13.0, 23.0, 24.0):
        clock[0] = now
        assert main.publish(**_main())
        assert kirkus.publish(**_kirkus())
    assert calls == [0.0, 12.0, 24.0]
    assert len(failures) == 1
