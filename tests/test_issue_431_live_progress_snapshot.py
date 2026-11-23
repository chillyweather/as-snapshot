"""Focused acceptance tests for issue #431 live progress snapshots."""

from __future__ import annotations

import hashlib
import inspect
import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from autoscout import source_collection
from autoscout.codex_recovery import CODEX_NO_PROGRESS_TIMEOUT_SECONDS
from autoscout.codex_recovery import CodexCliExtractionAgent
from autoscout.codex_recovery import CodexProgressLease
from autoscout.codex_recovery import ExtractionAttempt
from autoscout.codex_recovery import ExtractionStatus
from autoscout.codex_recovery import ProductionCodexAdapter
from autoscout.extraction_round import execute_bounded_extraction_round
from autoscout.progress_snapshot import PROGRESS_SNAPSHOT_FILE
from autoscout.progress_snapshot import ProgressSnapshotWriter
from autoscout.runtime_budget import CAPTURE_RUNTIME_BUDGET_SECONDS
from autoscout.runtime_budget import CAPTURE_SOURCE_TIMEOUT_SECONDS
from autoscout.runtime_budget import capture_runtime_budget_for
from autoscout.secret_redaction import redact_secrets
from autoscout.source_collection import prepare_codex_extraction_worklist
from autoscout.weekly_workflow import deterministic_adapters
from autoscout.weekly_workflow import run_weekly_workflow


def _write_worklist(output_dir: Path, count: int = 2) -> list[dict[str, str]]:
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
                "operation_id": "operation-431",
                "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
            }
        )
    (output_dir / "codex-extraction-worklist.json").write_text(json.dumps({"work_items": items}), encoding="utf-8")
    return items


def test_snapshot_writer_redacts_and_replaces_atomically(tmp_path: Path) -> None:
    writer = ProgressSnapshotWriter(tmp_path, monotonic=lambda: 1.0)
    secret = "sk-" + "a" * 24
    assert writer.publish(
        stage="extraction",
        status="running",
        elapsed_seconds=1,
        rows=[
            {
                "source_name": "Source",
                "source_url": f"https://example.test/?token={secret}",
                "state": "running",
            }
        ],
        now=1.0,
    )

    snapshot = tmp_path / PROGRESS_SNAPSHOT_FILE
    assert json.loads(snapshot.read_text(encoding="utf-8"))["stage"] == "extraction"
    assert secret not in snapshot.read_text(encoding="utf-8")
    assert not any(path.name.startswith(f".{PROGRESS_SNAPSHOT_FILE}.") for path in tmp_path.iterdir())


def test_snapshot_write_failure_is_nonfatal_and_retains_previous_bytes(tmp_path: Path, monkeypatch) -> None:
    writer = ProgressSnapshotWriter(tmp_path)
    assert writer.publish(stage="capture", status="finished", elapsed_seconds=1, rows=[], now=1)
    before = (tmp_path / PROGRESS_SNAPSHOT_FILE).read_bytes()

    def fail(_serialized: str) -> None:
        raise OSError("advisory storage failed")

    monkeypatch.setattr(writer, "_atomic_replace", fail)
    assert not writer.publish(stage="capture", status="finished", elapsed_seconds=2, rows=[], now=2)
    assert (tmp_path / PROGRESS_SNAPSHOT_FILE).read_bytes() == before


def test_snapshot_success_does_not_record_a_failure(tmp_path: Path) -> None:
    records: list[tuple[str, str]] = []
    writer = ProgressSnapshotWriter(tmp_path, failure_recorder=lambda stage, reason: records.append((stage, reason)))

    assert writer.publish(stage="capture", status="finished", elapsed_seconds=1, rows=[], now=1)
    assert records == []


def test_snapshot_write_failure_records_once_per_stage_and_redacts_reason(tmp_path: Path, monkeypatch) -> None:
    records: list[tuple[str, str]] = []
    writer = ProgressSnapshotWriter(tmp_path, failure_recorder=lambda stage, reason: records.append((stage, reason)))
    secret = "token=" + "s" * 20

    def fail(_serialized: str) -> None:
        raise OSError(f"advisory storage failed: {secret}")

    monkeypatch.setattr(writer, "_atomic_replace", fail)
    for now in (1, 2, 3):
        assert not writer.publish(stage="capture", status="running", elapsed_seconds=now, rows=[], now=now)
    assert len(records) == 1
    assert records[0][0] == "capture"
    assert "OSError" in records[0][1]
    assert secret not in records[0][1]
    assert "[REDACTED:credential_assignment]" in records[0][1]

    assert not writer.publish(stage="extraction", status="running", elapsed_seconds=4, rows=[], now=4)
    assert [stage for stage, _reason in records] == ["capture", "extraction"]


def test_snapshot_failure_recorder_is_nonfatal(tmp_path: Path, monkeypatch) -> None:
    def raise_recorder(_stage: str, _reason: str) -> None:
        raise RuntimeError("recorder failed")

    writer = ProgressSnapshotWriter(tmp_path, failure_recorder=raise_recorder)

    def fail(_serialized: str) -> None:
        raise OSError("advisory storage failed")

    monkeypatch.setattr(writer, "_atomic_replace", fail)
    assert not writer.publish(stage="capture", status="running", elapsed_seconds=1, rows=[], now=1)
    assert not writer.publish(stage="capture", status="running", elapsed_seconds=2, rows=[], now=2)


def test_repeated_readers_only_observe_complete_json(tmp_path: Path) -> None:
    writer = ProgressSnapshotWriter(tmp_path)
    errors: list[Exception] = []
    stop = threading.Event()

    def reader() -> None:
        path = tmp_path / PROGRESS_SNAPSHOT_FILE
        while not stop.is_set():
            if path.exists():
                try:
                    json.loads(path.read_text(encoding="utf-8"))
                except Exception as exc:  # pragma: no cover - only runs on an atomicity failure
                    errors.append(exc)
                    stop.set()

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for index in range(100):
            assert writer.publish(
                stage="extraction",
                status="running",
                elapsed_seconds=index,
                rows=[{"source_name": str(index), "source_url": "https://example.test", "state": "running"}],
                now=float(index),
            )
    finally:
        stop.set()
        thread.join()
    assert not errors


def test_round_poll_observer_runs_on_collector_thread_and_context_fallback_grows() -> None:
    started = threading.Event()
    release = threading.Event()
    observed: list[tuple[str, float, str]] = []
    contexts = []

    def run(context):
        contexts.append(context)
        started.set()
        release.wait(timeout=2)
        return {"status": "done"}

    def poll(current_contexts, now):
        observed.extend(
            (context.item["source_name"], context.progress_snapshot(now)[0] or 0.0, threading.current_thread().name)
            for context in current_contexts
        )
        if started.is_set():
            release.set()

    completions = execute_bounded_extraction_round(
        [{"source_name": "Source"}],
        worker_count=1,
        per_attempt_ceiling=5,
        portfolio_deadline=time.monotonic() + 5,
        run=run,
        boundary=lambda *_args: {"status": "boundary"},
        on_poll=poll,
    )

    assert len(completions) == 1
    assert observed
    assert all(name == threading.current_thread().name for _, _, name in observed)
    assert max(value for _, value, _ in observed) >= 0
    context = contexts[0]
    context.mark_attempt_started(10.0)
    assert context.progress_snapshot(12.0)[0] == 2.0
    context.update_progress_snapshot(0.0, {"reasoning": 1}, 12.0)
    assert context.progress_snapshot(14.0) == (2.0, {"reasoning": 1})
    context.update_progress_snapshot(0.0, {"reasoning": 2}, 14.0)
    assert context.progress_snapshot(14.0)[0] == 0.0


def test_snapshot_observation_does_not_change_the_no_progress_boundary(monkeypatch, tmp_path: Path) -> None:
    class Process:
        pid = 431

        def __init__(self, *_args, **_kwargs):
            pass

        def poll(self):
            return None

    class Clock:
        def __init__(self) -> None:
            self.value = 0.0

        def __call__(self) -> float:
            self.value += 1.0
            return self.value

    clock = Clock()
    monkeypatch.setattr(subprocess, "Popen", Process)
    monkeypatch.setattr("autoscout.codex_recovery._terminate_process_tree", lambda _process: None)
    agent = CodexCliExtractionAgent(
        timeout_seconds=CODEX_NO_PROGRESS_TIMEOUT_SECONDS + 10,
        clock=clock,
        sleeper=lambda _seconds: None,
    )
    seen: list[tuple[float | None, dict[str, int], float]] = []
    presented: list[dict] = []
    writer = ProgressSnapshotWriter(tmp_path, monotonic=clock, presentation=presented.append)
    isolated = tmp_path / "isolated"
    isolated.mkdir()

    def observe(idle, counts, now):
        seen.append((idle, counts, now))
        assert writer.publish(
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
            force=False,
        )

    with agent.progress_observer(observe):
        with pytest.raises(subprocess.TimeoutExpired):
            agent._run_codex(["codex"], isolated, isolated / "response.json", CODEX_NO_PROGRESS_TIMEOUT_SECONDS + 10)

    assert seen
    assert presented  # the presentation callback receives the live snapshots
    assert (tmp_path / PROGRESS_SNAPSHOT_FILE).is_file()
    # Announcing never renews the no-progress lease: the timeout boundary is
    # still the unchanged CODEX_NO_PROGRESS_TIMEOUT_SECONDS quiet period.
    assert agent._telemetry.boundary_reason == "no_progress_timeout"
    assert agent._telemetry.no_progress_quiet_seconds == CODEX_NO_PROGRESS_TIMEOUT_SECONDS


def test_snapshot_failure_does_not_change_no_progress_timeout(monkeypatch, tmp_path: Path) -> None:
    class Process:
        pid = 431

        def __init__(self, *_args, **_kwargs):
            pass

        def poll(self):
            return None

    class Clock:
        def __init__(self) -> None:
            self.value = 0.0

        def __call__(self) -> float:
            self.value += 1.0
            return self.value

    def fail(_self, _serialized):
        raise OSError("snapshot storage failed")

    clock = Clock()
    monkeypatch.setattr(subprocess, "Popen", Process)
    monkeypatch.setattr("autoscout.codex_recovery._terminate_process_tree", lambda _process: None)
    monkeypatch.setattr(ProgressSnapshotWriter, "_atomic_replace", fail)
    agent = CodexCliExtractionAgent(
        timeout_seconds=CODEX_NO_PROGRESS_TIMEOUT_SECONDS + 10,
        clock=clock,
        sleeper=lambda _seconds: None,
    )
    writer = ProgressSnapshotWriter(tmp_path, monotonic=clock)
    isolated = tmp_path / "isolated"
    isolated.mkdir()

    def observe(idle, counts, now):
        assert (
            writer.publish(
                stage="codex",
                status="running",
                elapsed_seconds=now,
                rows=[{"idle_seconds": idle, "event_counts": counts}],
                now=now,
            )
            is False
        )

    with agent.progress_observer(observe):
        with pytest.raises(subprocess.TimeoutExpired):
            agent._run_codex(["codex"], isolated, isolated / "response.json", CODEX_NO_PROGRESS_TIMEOUT_SECONDS + 10)

    assert agent._telemetry.boundary_reason == "no_progress_timeout"
    assert agent._telemetry.no_progress_quiet_seconds == CODEX_NO_PROGRESS_TIMEOUT_SECONDS


def test_codex_observer_reads_the_lease_without_renewing_it() -> None:
    lease = CodexProgressLease()
    lease.begin(10.0)
    lease.feed(b'{"type":"reasoning"}\n', 11.0)
    agent = CodexCliExtractionAgent()
    seen: list[tuple[float | None, dict[str, int], float]] = []

    with agent.progress_observer(lambda idle, counts, now: seen.append((idle, counts, now))):
        agent._observe_progress(lease, 12.0)

    assert seen == [(1.0, {"reasoning": 1}, 12.0)]
    assert lease.last_progress_at == 11.0
    assert lease.snapshot_counts() == {"reasoning": 1}


def test_capture_snapshot_uses_config_order_and_runtime_boundaries(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.json"
    sources = [
        {
            "name": "First source",
            "url": "https://example.test/first",
            "family": "award",
            "language": "en",
            "extraction_method": "codex",
        },
        {
            "name": "Disabled source",
            "url": "https://example.test/disabled",
            "family": "award",
            "language": "en",
            "active": False,
            "extraction_method": "codex",
        },
    ]
    config_path.write_text(json.dumps({"sources": sources}), encoding="utf-8")
    capture_writes: list[dict] = []
    in_flight_rows: list[dict] = []
    original_atomic_replace = ProgressSnapshotWriter._atomic_replace

    def record(self, serialized):
        payload = json.loads(serialized)
        if payload["stage"] == "capture":
            capture_writes.append(payload)
        original_atomic_replace(self, serialized)

    monkeypatch.setattr(ProgressSnapshotWriter, "_atomic_replace", record)

    def fake_page(_config, _output, source, *, live):
        current = json.loads((tmp_path / PROGRESS_SNAPSHOT_FILE).read_text(encoding="utf-8"))
        in_flight_rows.extend(row for row in current["rows"] if row["source_name"] == source["name"])
        return (
            {
                "source_url": source["url"],
                "resolved_url": source["url"],
                "source_page": source["url"],
                "html": "vendor diagnostic model prose page content",
            },
            {"vendor_diagnostic": "do not publish"},
        )

    monkeypatch.setattr(source_collection, "codex_source_page", fake_page)
    worklist = prepare_codex_extraction_worklist(config_path, tmp_path, "2026-08-10")
    snapshot = json.loads((tmp_path / PROGRESS_SNAPSHOT_FILE).read_text(encoding="utf-8"))

    assert snapshot["stage"] == "capture"
    assert snapshot["status"] == "finished"
    assert [row["source_name"] for row in snapshot["rows"]] == [source["name"] for source in sources]
    assert all(row["state"] == "finished" for row in snapshot["rows"])
    assert snapshot["rows"][0]["outcome"] == "codex-captured"
    assert snapshot["rows"][0]["per_source_timeout_seconds"] == CAPTURE_SOURCE_TIMEOUT_SECONDS
    assert snapshot["rows"][0]["remaining_runtime_budget_seconds"] <= CAPTURE_RUNTIME_BUDGET_SECONDS
    assert "vendor diagnostic" not in (tmp_path / PROGRESS_SNAPSHOT_FILE).read_text(encoding="utf-8")
    assert len(worklist.work_items) == 1
    assert in_flight_rows and in_flight_rows[0]["state"] == "in_flight"
    assert all(len(payload["rows"]) == len(sources) for payload in capture_writes)


def test_extraction_snapshot_replaces_capture_snapshot_and_keeps_every_row(tmp_path: Path, monkeypatch) -> None:
    items = _write_worklist(tmp_path)
    (tmp_path / PROGRESS_SNAPSHOT_FILE).write_text(
        json.dumps({"stage": "capture", "status": "finished", "rows": []}), encoding="utf-8"
    )
    started = threading.Event()
    release = threading.Event()
    writes: list[dict] = []
    original_atomic_replace = ProgressSnapshotWriter._atomic_replace

    def record(self, serialized):
        payload = json.loads(serialized)
        if payload["stage"] == "extraction":
            writes.append(payload)
        original_atomic_replace(self, serialized)

    monkeypatch.setattr(ProgressSnapshotWriter, "_atomic_replace", record)

    class Agent:
        def extract(self, item, _capture_path, artifact_path):
            started.set()
            release.wait(timeout=2)
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

    def run() -> dict:
        return ProductionCodexAdapter(
            Agent(),
            runtime_budget_seconds=5,
            per_attempt_timeout_seconds=2,
            max_attempts=1,
            max_workers=1,
        ).fulfill(tmp_path)

    result_holder: list[dict] = []
    worker = threading.Thread(target=lambda: result_holder.append(run()))
    worker.start()
    assert started.wait(timeout=2)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not any(payload["rows"][0]["state"] == "running" for payload in writes):
        time.sleep(0.01)
    release.set()
    worker.join(timeout=3)

    assert result_holder and result_holder[0]["status"] == "ok"
    assert writes
    assert all(len(payload["rows"]) == len(items) for payload in writes)
    assert all(
        [row["source_name"] for row in payload["rows"]] == [item["source_name"] for item in items] for payload in writes
    )
    final = json.loads((tmp_path / PROGRESS_SNAPSHOT_FILE).read_text(encoding="utf-8"))
    assert final["stage"] == "extraction"
    assert final["status"] == "ok"
    assert [row["status"] for row in final["rows"]] == ["validated", "validated"]
    assert "entries" not in final
    assert result_holder[0]["ready_items"] == 2


def test_snapshot_failure_does_not_change_canonical_extraction_result(tmp_path: Path, monkeypatch) -> None:
    items = _write_worklist(tmp_path, count=1)

    def fail(_self, _serialized):
        raise OSError("snapshot storage failed")

    monkeypatch.setattr(ProgressSnapshotWriter, "_atomic_replace", fail)

    class Agent:
        def extract(self, item, _capture_path, artifact_path):
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

    result = ProductionCodexAdapter(Agent(), max_attempts=1, runtime_budget_seconds=5).fulfill(tmp_path)

    assert result["status"] == "ok"
    assert result["ready_items"] == 1
    assert (tmp_path / items[0]["expected_extraction_file"]).is_file()


def test_snapshot_publication_does_not_change_canonical_bytes(tmp_path: Path, monkeypatch) -> None:
    def run_case(output_dir: Path) -> tuple[bytes, bytes, bytes]:
        items = _write_worklist(output_dir, count=1)

        class Agent:
            def extract(self, item, _capture_path, artifact_path):
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

        result = ProductionCodexAdapter(
            Agent(),
            monotonic=lambda: 0.0,
            sleeper=lambda _seconds: None,
            runtime_budget_seconds=5,
            max_attempts=1,
        ).fulfill(output_dir)
        result_bytes = json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")
        artifact_bytes = (output_dir / items[0]["expected_extraction_file"]).read_bytes()
        outcome_bytes = (output_dir / "codex-extraction-outcomes.json").read_bytes()
        return result_bytes, artifact_bytes, outcome_bytes

    enabled_dir = tmp_path / "enabled"
    disabled_dir = tmp_path / "disabled"
    enabled_dir.mkdir()
    disabled_dir.mkdir()
    enabled = run_case(enabled_dir)

    def fail(_self, _serialized):
        raise OSError("snapshot storage failed")

    monkeypatch.setattr(ProgressSnapshotWriter, "_atomic_replace", fail)
    disabled = run_case(disabled_dir)

    assert enabled == disabled
    assert (enabled_dir / PROGRESS_SNAPSHOT_FILE).is_file()
    assert not (disabled_dir / PROGRESS_SNAPSHOT_FILE).exists()


def test_weekly_workflow_installs_snapshot_failure_recorders(tmp_path: Path) -> None:
    class Capture:
        recorder = None

        def set_progress_snapshot_failure_recorder(self, recorder) -> None:
            self.recorder = recorder

        def prepare(self, _config_path, output_dir, *, live=True, effective_date=None):
            assert self.recorder is not None
            self.recorder("capture", "OSError: capture snapshot failed")
            (output_dir / "codex-extraction-worklist.json").write_text(
                json.dumps({"work_items": [], "precollected_sources": []}), encoding="utf-8"
            )
            (output_dir / "source-runs.json").write_text("[]", encoding="utf-8")
            return {"returncode": 0, "duration_seconds": 0.0, "source_runs": []}

    class Codex:
        recorder = None

        def set_progress_snapshot_failure_recorder(self, recorder) -> None:
            self.recorder = recorder

        def fulfill(self, _output_dir, *, extraction_budget_seconds=None):
            assert self.recorder is not None
            self.recorder("extraction", "OSError: extraction snapshot failed")
            return {"status": "failed", "reason": "test failure"}

    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    output_dir = tmp_path / "run"
    adapters = deterministic_adapters()
    adapters.capture = Capture()
    adapters.codex = Codex()

    result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

    assert result.failed_stage == "codex"
    persisted = json.loads((output_dir / "workflow-log.json").read_text(encoding="utf-8"))
    failures = [entry for entry in persisted if entry.get("status") == "progress_snapshot_failed"]
    assert [(entry["stage"], entry["detail"]["reason"]) for entry in failures] == [
        ("capture", "OSError: capture snapshot failed"),
        ("codex", "OSError: extraction snapshot failed"),
    ]
    assert all(entry.get("operation_id") for entry in failures)


def test_runtime_defaults_are_bound_to_runtime_budget_constants() -> None:
    capture_signature = inspect.signature(prepare_codex_extraction_worklist)
    # Issue #595: the stage budget is derived from the configured portfolio, so
    # the default is None and the constant is the base the derivation starts
    # from. A portfolio with no detail route still gets exactly the constant.
    assert capture_signature.parameters["runtime_budget_seconds"].default is None
    assert capture_runtime_budget_for([]) == CAPTURE_RUNTIME_BUDGET_SECONDS
    assert capture_signature.parameters["per_source_timeout_seconds"].default == CAPTURE_SOURCE_TIMEOUT_SECONDS
    agent_signature = inspect.signature(CodexCliExtractionAgent)
    assert agent_signature.parameters["no_progress_timeout_seconds"].default == CODEX_NO_PROGRESS_TIMEOUT_SECONDS
    assert redact_secrets("model prose") == "model prose"
