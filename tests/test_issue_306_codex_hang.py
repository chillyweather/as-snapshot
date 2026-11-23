"""Deterministic regression coverage for issue #306.

Every test uses injected fake agents, clocks, and subprocess mocks.
No real Codex or network calls.  One short local sys.executable subprocess
timeout test is included (no Codex dependency).  No test thread may survive
the test function — all workers are daemon or released in a finally block so
pytest always exits normally.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from autoscout.codex_recovery import _SUBTERMINATE_GRACE_SECONDS
from autoscout.codex_recovery import ExtractionAttempt
from autoscout.codex_recovery import ExtractionStatus
from autoscout.codex_recovery import ProductionCodexAdapter
from autoscout.codex_recovery import _DaemonFuture
from autoscout.codex_recovery import _DaemonThreadPool
from autoscout.codex_recovery import _terminate_process_tree

# ── helpers ─────────────────────────────────────────────────────────────────


def _make_worklist(
    tmp_path: Path,
    source_count: int,
    *,
    capture_html: str = "page",
) -> list[dict[str, Any]]:
    items = []
    for idx in range(source_count):
        capture = tmp_path / f"capture-{idx}.json"
        source_url = f"https://example.test/source-{idx}"
        capture.write_text(
            json.dumps({"source_url": source_url, "html": capture_html}),
            encoding="utf-8",
        )
        items.append(
            {
                "source_name": f"Source-{idx}",
                "source_url": source_url,
                "capture_file": capture.name,
                "expected_extraction_file": f"result-{idx}.json",
                "operation_id": f"op-{idx}",
                "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
            }
        )
    (tmp_path / "codex-extraction-worklist.json").write_text(json.dumps({"work_items": items}), encoding="utf-8")
    return items


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _staging_files(tmp_path: Path, canonical_name: str) -> list[Path]:
    """Return any hidden staging files for a canonical artifact name."""
    return sorted(tmp_path.glob("." + canonical_name + ".*"))


# ── 1. Never-returning future/agent is bounded and reported as timeout ──────


def test_never_returning_worker_is_bounded_and_reported_as_timeout(
    tmp_path: Path,
) -> None:
    _make_worklist(tmp_path, source_count=1)
    clock = FakeClock()

    class HangingAgent:
        def extract(self, item, capture_path, artifact_path):
            raise AssertionError("deadline-aware path expected")

        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            clock.advance(0.10)
            while True:
                time.sleep(0.01)

    start = time.monotonic()
    result = ProductionCodexAdapter(
        HangingAgent(),
        monotonic=clock,
        sleeper=lambda seconds: clock.advance(seconds),
        runtime_budget_seconds=0.10,
        per_attempt_timeout_seconds=0.05,
        max_attempts=1,
        max_workers=1,
    ).fulfill(tmp_path)
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, f"fulfill hung for {elapsed:.2f}s"
    assert result["ready_items"] == 0
    assert result["status"] == "failed"
    assert result["reason"] == "no_extraction_produced"

    timeout_diags = [
        d
        for d in result["diagnostics"]
        if d.get("status") == "timeout" and d.get("diagnostic") == "worker_collection_timeout"
    ]
    assert len(timeout_diags) == 1
    assert timeout_diags[0]["source_name"] == "Source-0"


# ── 2. Subprocess timeout and cleanup are bounded ───────────────────────────


def test_subprocess_timeout_and_cleanup_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminate_calls: list[tuple[str, float]] = []
    wait_calls: list[float] = []

    class FakeProcess:
        def __init__(self):
            self.pid = 12345

        def terminate(self):
            terminate_calls.append(("terminate", time.monotonic()))

        def kill(self):
            terminate_calls.append(("kill", time.monotonic()))

        def wait(self, timeout=None):
            wait_calls.append(timeout or -1)
            if timeout is not None:
                raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
            return 0

    monkeypatch.setattr("os.getpgid", lambda pid: 99999)
    monkeypatch.setattr("os.killpg", lambda pgid, sig: None)
    monkeypatch.setattr("signal.SIGTERM", signal.SIGTERM)
    monkeypatch.setattr("signal.SIGKILL", signal.SIGKILL)

    proc = FakeProcess()
    _terminate_process_tree(proc)  # type: ignore[arg-type]

    assert len(terminate_calls) >= 1
    assert len(wait_calls) >= 2
    for w in wait_calls:
        if w != -1:
            assert w == _SUBTERMINATE_GRACE_SECONDS


def test_codex_cli_agent_popen_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    from autoscout.codex_recovery import CodexCliExtractionAgent

    popen_kwargs: dict[str, Any] = {}

    class FakeProcess:
        def __init__(self, command, **kwargs):
            popen_kwargs.update(kwargs)
            self.returncode = 0
            self.pid = 99999

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(subprocess, "Popen", FakeProcess)
    monkeypatch.setattr("os.getpgid", lambda pid: 99999)

    agent = CodexCliExtractionAgent(timeout_seconds=10)

    with tempfile.TemporaryDirectory(prefix="autoscout-test-") as tmp:
        capture = Path(tmp) / "capture.json"
        capture.write_text("{}", encoding="utf-8")
        artifact = Path(tmp) / "artifact.json"
        _result = agent.extract({}, capture, artifact)

    assert popen_kwargs.get("start_new_session") is True
    assert popen_kwargs.get("stdin") is subprocess.DEVNULL


def test_codex_cli_agent_json_stderr_is_not_progress(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from autoscout.codex_recovery import CodexCliExtractionAgent

    class FakeProcess:
        pid = 99999
        returncode = 0

        def __init__(self, command, **kwargs):
            del command
            os.write(kwargs["stderr"], b'{"type":"response_item","payload":{"type":"reasoning"}}\\n')

        def poll(self):
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", FakeProcess)
    agent = CodexCliExtractionAgent(timeout_seconds=10)
    _returncode, diagnostic = agent._run_codex(["codex"], tmp_path, tmp_path / "response.json", 10.0)

    assert "reasoning" in diagnostic
    assert agent._telemetry.progress_event_counts == {}


def test_codex_cli_agent_tempfile_stderr_redaction() -> None:
    from unittest import mock

    from autoscout.codex_recovery import CodexCliExtractionAgent

    agent = CodexCliExtractionAgent(timeout_seconds=10)
    raw_stderr = "sensitive: figd_abc123-secret-token-longenough\n"

    def fake_run_codex(command, isolated, response_path, bounded_timeout):
        return (1, raw_stderr)

    with mock.patch.object(CodexCliExtractionAgent, "_run_codex", side_effect=fake_run_codex):
        with tempfile.TemporaryDirectory(prefix="autoscout-test-") as tmp:
            capture = Path(tmp) / "capture.json"
            capture.write_text("{}", encoding="utf-8")
            artifact = Path(tmp) / "artifact.json"
            result = agent.extract({}, capture, artifact)

    assert result.status == ExtractionStatus.COMMAND_FAILED
    assert "figd_" not in result.diagnostic
    assert "REDACTED" in result.diagnostic


def test_run_codex_cleans_tempfiles_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_run_codex must unlink both stdout and stderr temp files even when
    the subprocess times out (not just on success)."""
    from autoscout.codex_recovery import CodexCliExtractionAgent

    temp_files_created: list[str] = []

    _real_mkstemp = tempfile.mkstemp

    def _fake_mkstemp(prefix="", suffix="", dir=None):
        fd, path = _real_mkstemp(prefix=prefix, suffix=suffix, dir=dir)
        temp_files_created.append(path)
        return fd, path

    monkeypatch.setattr(tempfile, "mkstemp", _fake_mkstemp)

    class TimeoutProcess:
        def __init__(self, command, **kwargs):
            self.pid = 99999

        def poll(self):
            return None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)

    monkeypatch.setattr(subprocess, "Popen", TimeoutProcess)
    monkeypatch.setattr("os.getpgid", lambda pid: 99999)
    monkeypatch.setattr("os.killpg", lambda pgid, sig: None)
    monkeypatch.setattr("signal.SIGTERM", signal.SIGTERM)
    monkeypatch.setattr("signal.SIGKILL", signal.SIGKILL)

    class AdvancingClock:
        def __init__(self):
            self.value = 0.0

        def __call__(self):
            self.value += 11.0
            return self.value

    agent = CodexCliExtractionAgent(timeout_seconds=10, clock=AdvancingClock())

    with tempfile.TemporaryDirectory(prefix="autoscout-test-") as tmp:
        capture = Path(tmp) / "capture.json"
        capture.write_text("{}", encoding="utf-8")
        artifact = Path(tmp) / "artifact.json"
        result = agent.extract({}, capture, artifact)

    assert result.status == ExtractionStatus.TIMEOUT
    # All temp files should be unlinked after the timeout.
    for path_str in temp_files_created:
        assert not os.path.exists(path_str), f"Temp file leaked after timeout: {path_str}"


# ── 3. Short local subprocess timeout test (no Codex, no network) ──────────


def test_real_subprocess_timeout_is_bounded() -> None:
    with tempfile.TemporaryDirectory(prefix="autoscout-test-") as tmp:
        tmp_path = Path(tmp)
        capture = tmp_path / "capture.json"
        capture.write_text(json.dumps({"source_url": "https://example.test/s", "html": ""}))
        worklist = tmp_path / "codex-extraction-worklist.json"
        import hashlib as _hl

        cap_hash = _hl.sha256(capture.read_bytes()).hexdigest()
        worklist.write_text(
            json.dumps(
                {
                    "run_date": "2026-01-01",
                    "work_items": [
                        {
                            "source_name": "Test",
                            "source_url": "https://example.test/s",
                            "capture_file": "capture.json",
                            "expected_extraction_file": "artifact.json",
                            "operation_id": "op",
                            "capture_sha256": cap_hash,
                        }
                    ],
                }
            )
        )

        class _SleepyAgent:
            def extract(self, item, capture_path, artifact_path):
                raise AssertionError("deadline-aware path expected")

            def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
                proc = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    start_new_session=True,
                )
                try:
                    proc.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    from autoscout.codex_recovery import _terminate_process_tree as _tpt

                    _tpt(proc)
                    return ExtractionAttempt(ExtractionStatus.TIMEOUT, True, diagnostic="codex_timeout")
                return ExtractionAttempt(ExtractionStatus.PRODUCED, True, 0)

        start = time.monotonic()
        result = ProductionCodexAdapter(
            _SleepyAgent(),
            runtime_budget_seconds=5.0,
            per_attempt_timeout_seconds=5.0,
            max_attempts=1,
            max_workers=1,
        ).fulfill(tmp_path)
        elapsed = time.monotonic() - start

        assert elapsed < 4.0, f"subprocess timeout blocked for {elapsed:.2f}s"
        assert result["status"] == "failed"


# ── 4. Partial success survives one stalled worker ─────────────────────────


class OneStallsAgent:
    def __init__(self, clock: FakeClock, hang_index: int = 1) -> None:
        self.clock = clock
        self.hang_index = hang_index
        self.calls = 0
        self.started = threading.Barrier(3, action=lambda: self.clock.advance(0.20))

    def extract(self, item, capture_path, artifact_path):
        raise AssertionError("deadline-aware path expected")

    def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
        idx = int(item["source_name"].rsplit("-", 1)[-1])
        self.calls += 1
        self.started.wait()
        if idx == self.hang_index:
            while True:
                time.sleep(0.01)
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


def test_partial_success_survives_one_stalled_worker(tmp_path: Path) -> None:
    _make_worklist(tmp_path, source_count=3)
    clock = FakeClock()
    agent = OneStallsAgent(clock, hang_index=1)

    result = ProductionCodexAdapter(
        agent,
        monotonic=clock,
        sleeper=lambda seconds: clock.advance(seconds),
        runtime_budget_seconds=0.20,
        per_attempt_timeout_seconds=0.05,
        max_attempts=1,
        max_workers=3,
    ).fulfill(tmp_path)

    assert result["ready_items"] == 2
    assert result["status"] == "ok"
    assert result["failed_items"] == 0
    assert result["skipped_items"] == 1

    timeout_diags = [
        d
        for d in result["diagnostics"]
        if d.get("status") == "timeout" and d.get("diagnostic") == "worker_collection_timeout"
    ]
    assert len(timeout_diags) == 1
    assert timeout_diags[0]["source_name"] == "Source-1"

    validated_diags = [d for d in result["diagnostics"] if d.get("status") == "validated"]
    assert len(validated_diags) == 2

    assert (tmp_path / "result-0.json").exists()
    assert not (tmp_path / "result-1.json").exists()
    assert (tmp_path / "result-2.json").exists()


# ── 5. Fully stalled portfolio returns failed/no_extraction_produced ───────


def test_fully_stalled_portfolio_returns_failed_within_tight_bound(
    tmp_path: Path,
) -> None:
    _make_worklist(tmp_path, source_count=5)

    class AllHangAgent:
        def __init__(self) -> None:
            self.started = threading.Barrier(2, action=lambda: clock.advance(0.10))

        def extract(self, item, capture_path, artifact_path):
            raise AssertionError("deadline-aware path expected")

        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            self.started.wait()
            while True:
                time.sleep(0.01)

    clock = FakeClock()
    start = time.monotonic()
    result = ProductionCodexAdapter(
        AllHangAgent(),
        monotonic=clock,
        sleeper=lambda seconds: clock.advance(seconds),
        runtime_budget_seconds=0.10,
        per_attempt_timeout_seconds=0.05,
        max_attempts=1,
        max_workers=2,
    ).fulfill(tmp_path)
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, f"fully stalled portfolio hung for {elapsed:.2f}s"
    assert result["status"] == "failed"
    assert result["reason"] == "no_extraction_produced"
    assert result["ready_items"] == 0

    timeout_diags = [
        d
        for d in result["diagnostics"]
        if d.get("status") == "timeout" and d.get("diagnostic") == "worker_collection_timeout"
    ]
    assert len(timeout_diags) == 2
    budget_diags = [
        d
        for d in result["diagnostics"]
        if d.get("status") == "budget_exhausted" and d.get("diagnostic") == "portfolio_budget_exhausted"
    ]
    assert len(budget_diags) == 3
    assert all(d.get("started") is False for d in budget_diags)


# ── 6. Daemon pool shutdown safety ─────────────────────────────────────────


def test_daemon_pool_shutdown_uses_wait_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_worklist(tmp_path, source_count=1)

    shutdown_calls: list[dict[str, Any]] = []
    original_shutdown = _DaemonThreadPool.shutdown

    def _logging_shutdown(pool_self, *args, **kwargs):
        shutdown_calls.append(dict(kwargs=kwargs))
        return original_shutdown(pool_self, *args, **kwargs)

    monkeypatch.setattr(_DaemonThreadPool, "shutdown", _logging_shutdown)

    class HangAgent:
        def extract(self, item, capture_path, artifact_path):
            raise AssertionError("deadline-aware path expected")

        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            clock.advance(0.10)
            while True:
                time.sleep(0.01)

    clock = FakeClock()
    start = time.monotonic()
    result = ProductionCodexAdapter(
        HangAgent(),
        monotonic=clock,
        sleeper=lambda seconds: clock.advance(seconds),
        runtime_budget_seconds=0.10,
        per_attempt_timeout_seconds=0.05,
        max_attempts=1,
        max_workers=1,
    ).fulfill(tmp_path)
    elapsed = time.monotonic() - start

    assert elapsed < 2.0
    assert len(shutdown_calls) >= 1
    for call in shutdown_calls:
        assert call["kwargs"].get("wait") is False
        assert call["kwargs"].get("cancel_futures") is True
    assert result["status"] == "failed"


# ── 7. Late-released worker: valid artifact, cleanup, no publication ───────


def test_late_valid_worker_cleaned_no_publication(tmp_path: Path) -> None:
    """A late worker that writes a valid artifact (empty entries list —
    valid under current validator) after the collection timeout must:
    - NOT cause the canonical artifact to exist
    - NOT change returned readiness
    - have its staging bytes cleaned (no hidden staging file left)"""
    _make_worklist(tmp_path, source_count=1, capture_html="page")

    clock = FakeClock()
    release_worker = threading.Event()
    worker_wrote = threading.Event()
    fulfill_done = threading.Event()

    class LateReleaseAgent:
        def extract(self, item, capture_path, artifact_path):
            raise AssertionError("deadline-aware path expected")

        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            # Write valid artifact: empty entries passes validation.
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
            worker_wrote.set()
            release_worker.wait(timeout=10)
            return ExtractionAttempt(ExtractionStatus.PRODUCED, True, 0)

    adapter = ProductionCodexAdapter(
        LateReleaseAgent(),
        monotonic=clock,
        sleeper=lambda seconds: clock.advance(seconds),
        runtime_budget_seconds=0.10,
        per_attempt_timeout_seconds=0.05,
        max_attempts=1,
        max_workers=1,
    )

    result_container: dict[str, Any] = {}

    def run_in_thread():
        result_container["result"] = adapter.fulfill(tmp_path)
        fulfill_done.set()

    thread = threading.Thread(target=run_in_thread, daemon=True)
    thread.start()

    try:
        assert worker_wrote.wait(timeout=5.0), "Worker never wrote staging"
        clock.advance(0.20)
        assert fulfill_done.wait(timeout=5.0), "fulfill never returned"
    finally:
        release_worker.set()
        thread.join(timeout=2.0)

    result = result_container["result"]
    # No canonical artifact.
    canonical = tmp_path / "result-0.json"
    assert not canonical.exists(), "Late worker's valid artifact was published to canonical path"
    # Result reflects failure.
    assert result["status"] == "failed"
    assert result["ready_items"] == 0
    timeout_diags = [
        d
        for d in result["diagnostics"]
        if d.get("status") == "timeout" and d.get("diagnostic") == "worker_collection_timeout"
    ]
    assert len(timeout_diags) == 1

    # The late cleanup callback should have quarantined/removed the staging
    # file. Wait briefly for the callback to fire, then check.
    time.sleep(0.1)
    staging = _staging_files(tmp_path, "result-0.json")
    assert len(staging) == 0, f"Staging file leaked after late-worker cleanup: {staging}"


def test_late_worker_raises_after_staging_no_publication(
    tmp_path: Path,
) -> None:
    """A late worker that writes partial staging bytes then raises after
    collection timeout must have its staging cleaned and no canonical
    artifact published."""
    _make_worklist(tmp_path, source_count=1, capture_html="page")

    clock = FakeClock()
    release_worker = threading.Event()
    worker_wrote = threading.Event()
    fulfill_done = threading.Event()

    class WriteThenRaiseAgent:
        def extract(self, item, capture_path, artifact_path):
            raise AssertionError("deadline-aware path expected")

        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            artifact_path.write_text("partial junk", encoding="utf-8")
            worker_wrote.set()
            release_worker.wait(timeout=10)
            raise ValueError("mid-extraction failure after staging")

    adapter = ProductionCodexAdapter(
        WriteThenRaiseAgent(),
        monotonic=clock,
        sleeper=lambda seconds: clock.advance(seconds),
        runtime_budget_seconds=0.10,
        per_attempt_timeout_seconds=0.05,
        max_attempts=1,
        max_workers=1,
    )

    result_container: dict[str, Any] = {}

    def run_in_thread():
        result_container["result"] = adapter.fulfill(tmp_path)
        fulfill_done.set()

    thread = threading.Thread(target=run_in_thread, daemon=True)
    thread.start()

    try:
        assert worker_wrote.wait(timeout=5.0), "Worker never wrote staging"
        clock.advance(0.20)
        assert fulfill_done.wait(timeout=5.0), "fulfill never returned"
    finally:
        release_worker.set()
        thread.join(timeout=2.0)

    result = result_container["result"]
    canonical = tmp_path / "result-0.json"
    assert not canonical.exists(), "Late-raising worker's staging somehow became canonical"
    assert result["status"] == "failed"
    assert result["ready_items"] == 0

    # Staging file should be cleaned by the done callback.
    time.sleep(0.1)
    staging = _staging_files(tmp_path, "result-0.json")
    assert len(staging) == 0, f"Staging file leaked after late-raising worker: {staging}"


# ── 8. Guard-release test with real fake lock file ─────────────────────────


def test_weekly_workflow_guard_released_after_stalled_extraction(
    tmp_path: Path,
) -> None:
    from autoscout.weekly_workflow import WeeklyWorkflow
    from autoscout.weekly_workflow import WorkflowOutcome

    config = tmp_path / "config.json"
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    config.write_text(
        json.dumps(
            {
                "run_date": "2000-01-01",
                "sources": [
                    {
                        "name": "Test",
                        "family": "award",
                        "url": "https://example.test/s",
                        "access_status": "open",
                        "extraction_method": "codex-extraction-replay",
                        "check_frequency": "weekly",
                        "source_page_fixture_file": "test-page.json",
                        "extraction_response_file": "test-extraction.json",
                    }
                ],
            }
        )
    )

    lock_path = tmp_path / ".autoscout-guard.lock"

    class FakeGuardRepo:
        def preflight(self, validated_artifact_allowlist=None):
            lock_path.write_text("locked", encoding="utf-8")
            return {"status": "ok"}

        def update(self, validated_artifact_allowlist=None):
            return {"status": "ok", "pulled": False}

        def finalize(self, output_dir):
            return {"status": "ok"}

        def release_guard(self):
            try:
                lock_path.unlink()
            except OSError:
                pass

    class StallingCodexAdapter:
        def fulfill(self, output_dir):
            worklist = output_dir / "codex-extraction-worklist.json"
            capture = output_dir / "capture-0.json"
            capture.write_text(json.dumps({"source_url": "https://example.test/s", "html": "page"}))
            import hashlib as _hl

            cap_hash = _hl.sha256(capture.read_bytes()).hexdigest()
            worklist.write_text(
                json.dumps(
                    {
                        "run_date": "2000-01-01",
                        "work_items": [
                            {
                                "source_name": "Test",
                                "source_url": "https://example.test/s",
                                "capture_file": "capture-0.json",
                                "expected_extraction_file": "result-0.json",
                                "operation_id": "op",
                                "capture_sha256": cap_hash,
                            }
                        ],
                    }
                )
            )

            class HangAgent:
                def extract(self, item, capture_path, artifact_path):
                    raise AssertionError("deadline-aware path expected")

                def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
                    while True:
                        time.sleep(0.01)

            adapter = ProductionCodexAdapter(
                HangAgent(),
                runtime_budget_seconds=0.10,
                per_attempt_timeout_seconds=0.05,
                max_attempts=1,
                max_workers=1,
            )
            return adapter.fulfill(output_dir)

    from autoscout.weekly_workflow import deterministic_adapters

    adapters = deterministic_adapters()
    adapters.codex = StallingCodexAdapter()
    adapters.repository = FakeGuardRepo()

    workflow = WeeklyWorkflow(adapters)
    result = workflow.run(config, output_dir)

    # Guard must have been released (lock file removed).
    assert not lock_path.exists(), "Guard lock file was NOT released after stalled extraction"
    assert result.outcome == WorkflowOutcome.INTERVENTION_REQUIRED


# ── 9. Zero-remaining-budget uses timeout=0 (not None/unbounded) ───────────


def test_zero_remaining_budget_is_immediate_timeout(tmp_path: Path) -> None:
    _make_worklist(tmp_path, source_count=2)
    clock = FakeClock()

    class BudgetConsumerAgent:
        def __init__(self):
            self.calls = 0

        def extract(self, item, capture_path, artifact_path):
            raise AssertionError("deadline-aware path expected")

        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            self.calls += 1
            idx = int(item["source_name"].rsplit("-", 1)[-1])
            if idx == 0:
                clock.advance(0.20)
                return ExtractionAttempt(ExtractionStatus.TIMEOUT, True, diagnostic="codex_timeout")
            else:
                raise AssertionError("agent should not be called at zero budget")

    result = ProductionCodexAdapter(
        BudgetConsumerAgent(),
        monotonic=clock,
        sleeper=lambda seconds: clock.advance(seconds),
        runtime_budget_seconds=0.10,
        per_attempt_timeout_seconds=0.05,
        max_attempts=1,
        max_workers=1,
    ).fulfill(tmp_path)

    assert result["status"] == "failed"
    assert result["reason"] == "no_extraction_produced"
    budget_diags = [d for d in result["diagnostics"] if d.get("status") == "budget_exhausted"]
    assert len(budget_diags) == 1
    assert budget_diags[0]["source_name"] == "Source-1"


# ── 10. _DaemonThreadPool cancellation / daemon / callbacks ────────────────


def test_daemon_pool_cancel_futures_marks_queued_as_cancelled() -> None:
    pool = _DaemonThreadPool(max_workers=1, thread_name_prefix="test-")
    try:
        started = threading.Event()

        def slow():
            started.set()
            time.sleep(10)
            return 42

        _f1 = pool.submit(slow)
        started.wait(timeout=2)

        f2 = pool.submit(lambda: 99)
        pool.shutdown(wait=False, cancel_futures=True)

        assert f2.done()
        try:
            f2.result(timeout=0)
            raise AssertionError("cancelled future should raise")
        except RuntimeError:
            pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def test_daemon_pool_threads_are_daemon() -> None:
    pool = _DaemonThreadPool(max_workers=2, thread_name_prefix="test-")
    try:
        started = threading.Event()

        def capture_thread_info():
            started.set()
            time.sleep(0.05)
            return threading.current_thread()

        f = pool.submit(capture_thread_info)
        started.wait(timeout=2)
        worker = f.result(timeout=2)
        assert worker.daemon, f"Worker thread is not daemon: {worker}"
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


# ── 11. _DaemonFuture done callbacks ───────────────────────────────────────


def test_daemon_future_done_callback_before_completion() -> None:
    """add_done_callback before the future completes fires exactly once
    when the result is set."""
    results: list[int] = []

    f: _DaemonFuture = _DaemonFuture()
    f.add_done_callback(lambda: results.append(1))
    f._set_result(42)

    assert results == [1]
    assert f.result(timeout=0) == 42


def test_daemon_future_done_callback_after_completion() -> None:
    """add_done_callback after the future is done fires immediately."""
    results: list[int] = []

    f: _DaemonFuture = _DaemonFuture()
    f._set_result(42)
    f.add_done_callback(lambda: results.append(1))

    assert results == [1]


def test_daemon_future_done_callback_exception_suppressed() -> None:
    """A callback that raises is silently suppressed and does not prevent
    other callbacks or the future from completing."""
    results: list[int] = []

    f: _DaemonFuture = _DaemonFuture()
    f.add_done_callback(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    f.add_done_callback(lambda: results.append(1))
    f._set_result(42)

    assert results == [1]
    assert f.result(timeout=0) == 42


# ── 12. _terminate_process_tree Windows fallback ───────────────────────────


def test_terminate_process_tree_windows_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")

    calls: list[str] = []

    class FakeWinProcess:
        def terminate(self):
            calls.append("terminate")

        def kill(self):
            calls.append("kill")

        def wait(self, timeout=None):
            calls.append(f"wait:{timeout}")
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)

    proc = FakeWinProcess()
    _terminate_process_tree(proc)  # type: ignore[arg-type]

    assert "terminate" in calls
    assert "kill" in calls
