"""Deterministic regression coverage for issue #283."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import autoscout.observed_extraction as observed_extraction
import autoscout.source_collection as source_collection
from autoscout.codex_recovery import DEFAULT_EXTRACTION_WORKERS
from autoscout.codex_recovery import ExtractionAttempt
from autoscout.codex_recovery import ExtractionStatus
from autoscout.codex_recovery import ProductionCodexAdapter
from autoscout.codex_recovery import ProductionCodexCaptureAdapter
from autoscout.runtime_budget import CODEX_ABSOLUTE_CEILING_SECONDS
from autoscout.runtime_budget import CODEX_TIMEOUT_SECONDS
from autoscout.runtime_budget import PORTFOLIO_RUNTIME_BUDGET_SECONDS
from autoscout.russian_workflow import render_workflow_result
from autoscout.weekly_workflow import WeeklyWorkflowResult
from autoscout.weekly_workflow import WorkflowOutcome
from autoscout.weekly_workflow import _sanitize_log_entry
from autoscout.weekly_workflow import _source_problems_from_extraction


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class DeadlineAwareAgent:
    def __init__(self, clock: FakeClock, retryable: bool = False) -> None:
        self.clock = clock
        self.timeouts: list[float] = []
        self._retryable = retryable

    def extract(self, item, capture_path, artifact_path):
        raise AssertionError("deadline-aware path expected")

    def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
        self.timeouts.append(timeout_seconds)
        self.clock.advance(timeout_seconds)
        return ExtractionAttempt(ExtractionStatus.COMMAND_FAILED, self._retryable)


class NearlyExhaustingAgent:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls = 0

    def extract(self, item, capture_path, artifact_path):
        raise AssertionError("deadline-aware path expected")

    def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
        self.calls += 1
        self.clock.advance(min(2.0, timeout_seconds))
        return ExtractionAttempt(ExtractionStatus.COMMAND_FAILED, True)


class TimedAgent:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[str] = []

    def extract(self, item, capture_path, artifact_path):
        name = item["source_name"]
        self.calls.append(name)
        if name.startswith("Slow"):
            self.clock.advance(4)
            return ExtractionAttempt(ExtractionStatus.TIMEOUT, True, diagnostic="internal timeout")
        self.clock.advance(1)
        artifact_path.write_text(
            json.dumps(
                {
                    "source_name": name,
                    "source_url": item["source_url"],
                    "operation_id": item["operation_id"],
                    "capture_sha256": item["capture_sha256"],
                    "entries": [],
                }
            ),
            encoding="utf-8",
        )
        return ExtractionAttempt(ExtractionStatus.PRODUCED, True, 0)


def _portfolio(tmp_path: Path) -> None:
    items = []
    for index, name in enumerate(("Slow One", "Useful Source", "Slow Two")):
        capture = tmp_path / f"capture-{index}.json"
        source_url = f"https://example.test/{index}"
        capture.write_text(json.dumps({"source_url": source_url, "html": ""}), encoding="utf-8")
        items.append(
            {
                "source_name": name,
                "source_url": source_url,
                "capture_file": capture.name,
                "expected_extraction_file": f"result-{index}.json",
                "operation_id": "operation",
                "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
                # Issue #700: the fixture agent writes an empty artifact to
                # stand for "an attempt completed". Declaring the source as
                # one that may report no entries keeps this suite measuring
                # runtime budget rather than the empty-extraction retry.
                "source_metadata": {"may_report_no_entries": True},
            }
        )
    (tmp_path / "codex-extraction-worklist.json").write_text(json.dumps({"work_items": items}), encoding="utf-8")


def test_multiple_slow_sources_are_bounded_fair_and_keep_completed_result(tmp_path: Path) -> None:
    _portfolio(tmp_path)
    clock = FakeClock()
    agent = TimedAgent(clock)
    result = ProductionCodexAdapter(
        agent, monotonic=clock, sleeper=lambda seconds: clock.advance(seconds), runtime_budget_seconds=9, max_attempts=3
    ).fulfill(tmp_path)

    assert result["status"] == "ok"
    assert result["ready_items"] == 1
    assert agent.calls == ["Slow One", "Useful Source", "Slow Two"]  # initial chance before retries
    assert result["duration_seconds"] == 9
    assert result["duration_seconds"] <= result["runtime_budget_seconds"]
    assert {timing["source_name"] for timing in result["source_timings"]} == {"Slow One", "Useful Source", "Slow Two"}
    assert (tmp_path / "result-1.json").exists()
    assert {d["source_name"] for d in result["diagnostics"] if d["status"] == "timeout"} == {"Slow One", "Slow Two"}
    assert all("duration_seconds" in d for d in result["diagnostics"])

    problems = _source_problems_from_extraction(result)
    rendered = render_workflow_result(
        WeeklyWorkflowResult(
            outcome=WorkflowOutcome.COMPLETED_ATTENTION_REQUIRED,
            readiness_facts={"attention_required": True},
            source_problems=problems,
            review_available=True,
        ),
        include_progress=False,
    )
    assert "Slow One" in rendered and "Slow Two" in rendered
    for internal in ("timeout", "retry", "поряд", "страниц"):
        assert internal not in rendered.lower()


def test_remaining_budget_bounds_attempt_and_backoff_without_overrun(tmp_path: Path) -> None:
    _portfolio(tmp_path)
    clock = FakeClock()
    agent = DeadlineAwareAgent(clock, retryable=False)
    result = ProductionCodexAdapter(
        agent,
        monotonic=clock,
        sleeper=lambda seconds: clock.advance(seconds),
        runtime_budget_seconds=2.1,
        max_attempts=2,
        max_workers=1,
    ).fulfill(tmp_path)

    assert result["duration_seconds"] <= result["runtime_budget_seconds"]
    assert result["duration_seconds"] <= 2.1
    assert agent.timeouts == [2.1]
    assert sum(d["status"] == "budget_exhausted" for d in result["diagnostics"]) == 2

    # Single-source case: the entire remaining budget is offered in one attempt.
    worklist_path = tmp_path / "codex-extraction-worklist.json"
    worklist = json.loads(worklist_path.read_text(encoding="utf-8"))
    worklist["work_items"] = worklist["work_items"][:1]
    worklist_path.write_text(json.dumps(worklist), encoding="utf-8")
    backoff_clock = FakeClock()
    backoff_agent = NearlyExhaustingAgent(backoff_clock)
    backoff_result = ProductionCodexAdapter(
        backoff_agent,
        monotonic=backoff_clock,
        sleeper=lambda seconds: backoff_clock.advance(seconds),
        runtime_budget_seconds=2.1,
        max_attempts=2,
    ).fulfill(tmp_path)
    assert backoff_agent.calls == 1
    # Duration includes 2.0s attempt + 0.1s backoff sleep (remaining budget).
    assert backoff_result["duration_seconds"] == 2.1


def test_capture_records_per_source_timings_with_bounded_timeouts(tmp_path: Path, monkeypatch) -> None:
    clock = FakeClock()
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "run_date": "2026-01-01",
                "sources": [
                    {
                        "name": name,
                        "family": "award",
                        "url": f"https://example.test/{index}",
                        "active": True,
                        "access_status": "open",
                        "extraction_method": "codex-extraction-replay",
                        "check_frequency": "weekly",
                        "language": "en",
                        "timeout_seconds": 999,
                    }
                    for index, name in enumerate(("One", "Two"))
                ],
            }
        ),
        encoding="utf-8",
    )
    observed_timeouts = []

    def fake_page(config_path, output_dir, source, *, live=False):
        observed_timeouts.append(source["timeout_seconds"])
        clock.advance(2)
        return {"source_page": source["url"], "html": ""}, {}

    monkeypatch.setattr(source_collection, "codex_source_page", fake_page)
    result = ProductionCodexCaptureAdapter(
        clock,
        runtime_budget_seconds=6,
        per_source_timeout_seconds=3,
    ).prepare(config, tmp_path / "run", live=True)

    assert observed_timeouts == [3, 3]
    assert result["duration_seconds"] == 4
    assert [run["duration_seconds"] for run in result["source_runs"]] == [2, 2]


def test_large_portfolio_budget_is_capped_by_per_attempt_ceiling(tmp_path: Path) -> None:
    """With large remaining budget, each attempt is capped at the per-attempt
    ceiling (CODEX_ABSOLUTE_CEILING_SECONDS), not the full remaining budget."""
    _portfolio(tmp_path)
    clock = FakeClock()
    agent = DeadlineAwareAgent(clock, retryable=False)
    result = ProductionCodexAdapter(
        agent,
        monotonic=clock,
        sleeper=lambda seconds: clock.advance(seconds),
        runtime_budget_seconds=PORTFOLIO_RUNTIME_BUDGET_SECONDS,
        max_attempts=1,
        max_workers=1,
    ).fulfill(tmp_path)

    # The new default absolute ceiling equals the compatibility portfolio
    # budget, so the injected additive fake clock admits one attempt.
    assert agent.timeouts == [CODEX_ABSOLUTE_CEILING_SECONDS]
    assert result["per_attempt_timeout_seconds"] == CODEX_ABSOLUTE_CEILING_SECONDS


def test_recovered_retry_does_not_create_final_caveat() -> None:
    result = {
        "source_timings": [{"source_name": "Recovered", "status": "ready", "duration_seconds": 2}],
        "diagnostics": [
            {"source_name": "Recovered", "status": "invalid_output"},
            {"source_name": "Recovered", "status": "validated"},
        ],
    }
    assert _source_problems_from_extraction(result) == []


def test_invalid_runtime_policies_fail_clearly() -> None:
    for kwargs in (
        {"runtime_budget_seconds": 0},
        {"max_attempts": 0},
        {"per_attempt_timeout_seconds": 0},
    ):
        try:
            ProductionCodexAdapter(**kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid extraction policy accepted")


def test_nested_workflow_diagnostics_are_recursively_sanitized() -> None:
    clean = _sanitize_log_entry({"detail": {"items": [{"api_key": "secret", "duration_seconds": 2}]}})
    assert clean == {"detail": {"items": [{"duration_seconds": 2}]}}


# ---------------------------------------------------------------------------
# Issue #286 regression tests
# ---------------------------------------------------------------------------


def _staging_files(tmp_path: Path, canonical_name: str) -> list[Path]:
    """Return any hidden staging files for the given canonical artifact name."""
    return sorted(tmp_path.glob("." + canonical_name + ".*"))


def _portfolio_15(tmp_path: Path) -> None:
    """Create a 15-source worklist with distinct captures."""
    items = []
    for index in range(15):
        capture = tmp_path / f"capture-{index}.json"
        source_url = f"https://example.test/source-{index}"
        capture.write_text(json.dumps({"source_url": source_url, "html": "page"}), encoding="utf-8")
        items.append(
            {
                "source_name": f"Source-{index}",
                "source_url": source_url,
                "capture_file": capture.name,
                "expected_extraction_file": f"result-{index}.json",
                "operation_id": f"op-{index}",
                "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
                # Issue #700: the fixture agent writes an empty artifact to
                # stand for "an attempt completed". Declaring the source as
                # one that may report no entries keeps this suite measuring
                # runtime budget rather than the empty-extraction retry.
                "source_metadata": {"may_report_no_entries": True},
            }
        )
    (tmp_path / "codex-extraction-worklist.json").write_text(json.dumps({"work_items": items}), encoding="utf-8")


class Uniform35sAgent:
    """Agent where every extraction takes exactly 35 seconds and succeeds.
    Writes to whatever path the adapter provides (staging path)."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls = 0

    def extract(self, item, capture_path, artifact_path):
        raise AssertionError("deadline-aware path expected")

    def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
        self.calls += 1
        name = item["source_name"]
        # Fail if timeout is too short (< 35s) — old 30s cap would cause this
        if timeout_seconds < 35:
            self.clock.advance(timeout_seconds)
            return ExtractionAttempt(ExtractionStatus.TIMEOUT, True, diagnostic="budget_too_short")
        self.clock.advance(35)
        artifact_path.write_text(
            json.dumps(
                {
                    "source_name": name,
                    "source_url": item["source_url"],
                    "operation_id": item["operation_id"],
                    "capture_sha256": item["capture_sha256"],
                    "entries": [],
                }
            ),
            encoding="utf-8",
        )
        return ExtractionAttempt(ExtractionStatus.PRODUCED, True, 0)


def test_15_source_portfolio_with_35s_extractions_completes_within_budget(tmp_path: Path) -> None:
    """15 sources * 35s each = 525s fits comfortably within the portfolio
    budget since each attempt is offered up to the per-attempt ceiling
    (well above 35s). Under the old 30s cap this would timeout every
    source."""
    _portfolio_15(tmp_path)
    clock = FakeClock()
    agent = Uniform35sAgent(clock)
    result = ProductionCodexAdapter(
        agent,
        monotonic=clock,
        sleeper=lambda seconds: clock.advance(seconds),
        runtime_budget_seconds=PORTFOLIO_RUNTIME_BUDGET_SECONDS,
        max_attempts=1,
    ).fulfill(tmp_path)

    assert result["status"] == "ok"
    # All 15 sources should complete — each attempt is well within the
    # per-attempt ceiling (35s << CODEX_TIMEOUT_SECONDS).
    assert result["ready_items"] == 15
    assert result["failed_items"] == 0
    assert agent.calls == 15
    assert result["duration_seconds"] <= PORTFOLIO_RUNTIME_BUDGET_SECONDS

    for d in result["diagnostics"]:
        assert d["status"] == "validated"


def test_fully_slow_portfolio_terminates_at_budget_and_gives_every_source_initial_attempt(
    tmp_path: Path,
) -> None:
    """A fully slow 15-source portfolio terminates at or before the explicit
    total budget, gives every source an initial attempt, and emits a named
    timeout diagnostic for each."""
    _portfolio_15(tmp_path)
    clock = FakeClock()

    class AllSlowAgent:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def extract(self, item, capture_path, artifact_path):
            raise AssertionError("deadline-aware path expected")

        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            name = item["source_name"]
            self.calls.append(name)
            clock.advance(timeout_seconds)
            return ExtractionAttempt(ExtractionStatus.TIMEOUT, True, diagnostic="slow_source")

    budget = CODEX_TIMEOUT_SECONDS * 5
    agent = AllSlowAgent()
    result = ProductionCodexAdapter(
        agent,
        monotonic=clock,
        sleeper=lambda seconds: clock.advance(seconds),
        runtime_budget_seconds=budget,
        max_attempts=1,
        max_workers=1,  # deterministic fake-clock compatibility
        # Issue #411: the production default ceiling is now the absolute
        # 1800-second ceiling; this test models the old-style 180-second
        # per-attempt boundary, so it sets that ceiling explicitly.
        per_attempt_timeout_seconds=CODEX_TIMEOUT_SECONDS,
    ).fulfill(tmp_path)

    assert result["duration_seconds"] <= budget
    assert len(agent.calls) == 5
    assert result["duration_seconds"] == budget
    timeout_diags = [d for d in result["diagnostics"] if d["status"] == "timeout"]
    assert len(timeout_diags) == 5
    budget_exhausted = [d for d in result["diagnostics"] if d["status"] == "budget_exhausted"]
    assert len(budget_exhausted) == 10


# ---------------------------------------------------------------------------
# Resume marker & staging-path tests
# ---------------------------------------------------------------------------


def _write_stale_canonical(
    artifact_path: Path,
    source_url: str,
    source_name: str = "Test Source",
) -> bytes:
    """Write an artifact that references a *different* operation / capture
    so it will NOT validate against the current work item."""
    payload = json.dumps(
        {
            "source_name": source_name,
            "source_url": source_url,
            "operation_id": "previous-run",
            "capture_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
            "entries": [],
        }
    )
    artifact_path.write_text(payload, encoding="utf-8")
    return artifact_path.read_bytes()


def _setup_stale_source(
    tmp_path: Path,
    expected_file: str = "result-0.json",
    source_name: str = "Test Source",
) -> tuple[Path, str, str, bytes, list[dict]]:
    """Create a single-source worklist + stale canonical artifact +
    resume marker."""
    from autoscout.codex_recovery import _write_resume_marker

    capture = tmp_path / "capture-0.json"
    source_url = "https://example.test/source-0"
    capture.write_text(json.dumps({"source_url": source_url, "html": "page"}), encoding="utf-8")
    capture_sha256 = hashlib.sha256(capture.read_bytes()).hexdigest()

    artifact_path = tmp_path / expected_file
    items = [
        {
            "source_name": source_name,
            "source_url": source_url,
            "capture_file": capture.name,
            "expected_extraction_file": expected_file,
            "operation_id": "current-op",
            "capture_sha256": capture_sha256,
        }
    ]
    (tmp_path / "codex-extraction-worklist.json").write_text(json.dumps({"work_items": items}), encoding="utf-8")

    # Stale artifact: different operation id / hash so it won't validate.
    original_bytes = _write_stale_canonical(artifact_path, source_url, source_name)

    _write_resume_marker(
        tmp_path,
        {
            "run_id": "previous-run",
            "completed_stages": ["capture"],
            "validated_artifacts": [
                {
                    "source_url": source_url,
                    "expected_extraction_file": expected_file,
                    "capture_file": capture.name,
                    "run_id": "previous-run",
                    "artifact_sha256": hashlib.sha256(original_bytes).hexdigest(),
                }
            ],
        },
    )
    return artifact_path, source_url, expected_file, original_bytes, items


def test_current_valid_artifact_is_reused_regardless_of_resume_marker(
    tmp_path: Path,
) -> None:
    """A canonical artifact that validates against the current work item is
    reused and counted ready regardless of the resume marker. The agent is
    never called."""
    # Use _setup_stale_source to get worklist + resume marker, then
    # overwrite the stale canonical with current-valid bytes.
    artifact_path, source_url, expected_file, _stale_bytes, items = _setup_stale_source(tmp_path)
    current_payload = json.dumps(
        {
            "source_name": items[0]["source_name"],
            "source_url": source_url,
            "operation_id": items[0]["operation_id"],
            "capture_sha256": items[0]["capture_sha256"],
            "entries": [],
        }
    )
    artifact_path.write_text(current_payload, encoding="utf-8")

    class NeverCalledAgent:
        def extract(self, item, capture_path, artifact_path):
            raise AssertionError("agent must not be called")

        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            raise AssertionError("agent must not be called")

    clock = FakeClock()
    result = ProductionCodexAdapter(
        NeverCalledAgent(),
        monotonic=clock,
        sleeper=lambda seconds: clock.advance(seconds),
        runtime_budget_seconds=120,
        max_attempts=1,
    ).fulfill(tmp_path)

    assert result["status"] == "ok"
    assert result["ready_items"] == 1
    assert result["failed_items"] == 0
    diag_statuses = [d["status"] for d in result["diagnostics"]]
    assert "reused_validated_artifact" in diag_statuses


def test_stale_validated_artifact_preserved_when_fresh_extraction_fails(
    tmp_path: Path,
) -> None:
    """A stale canonical artifact (different operation/hash, not valid against
    the current work item) but recorded as previously validated in the resume
    marker remains byte-for-byte at its canonical path when fresh extraction
    times out/fails.  The agent receives a non-canonical staging path."""
    artifact_path, source_url, expected_file, original_bytes, _items = _setup_stale_source(tmp_path)

    agent_paths: list[str] = []
    clock = FakeClock()

    class FailingFreshAgent:
        def extract(self, item, capture_path, artifact_path):
            raise AssertionError("deadline-aware path expected")

        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            agent_paths.append(str(artifact_path))
            clock.advance(timeout_seconds)
            return ExtractionAttempt(ExtractionStatus.TIMEOUT, True, diagnostic="failed_fresh_extraction")

    result = ProductionCodexAdapter(
        FailingFreshAgent(),
        monotonic=clock,
        sleeper=lambda seconds: clock.advance(seconds),
        runtime_budget_seconds=120,
        max_attempts=1,
    ).fulfill(tmp_path)

    # Agent received a staging path, not the canonical path.
    assert len(agent_paths) == 1
    assert agent_paths[0] != str(artifact_path)
    assert artifact_path.parent == Path(agent_paths[0]).parent
    # Canonical artifact untouched.
    assert artifact_path.exists()
    assert artifact_path.read_bytes() == original_bytes
    # Not counted as ready.
    assert result["ready_items"] == 0
    # No staging file leaked.
    assert _staging_files(tmp_path, expected_file) == []


def test_legacy_digestless_marker_preserves_bytes_and_is_upgraded(tmp_path: Path) -> None:
    artifact_path, _source_url, expected_file, original_bytes, _items = _setup_stale_source(tmp_path)
    from autoscout.codex_recovery import _read_resume_marker
    from autoscout.codex_recovery import _write_resume_marker

    marker = _read_resume_marker(tmp_path)
    marker["validated_artifacts"][0].pop("artifact_sha256")
    _write_resume_marker(tmp_path, marker)

    class FailingFreshAgent:
        def extract_with_timeout(self, item, capture_path, staging_path, *, timeout_seconds):
            return ExtractionAttempt(ExtractionStatus.COMMAND_FAILED, False, diagnostic="fresh_failed")

    result = ProductionCodexAdapter(FailingFreshAgent(), runtime_budget_seconds=1, max_attempts=1).fulfill(tmp_path)

    assert artifact_path.read_bytes() == original_bytes
    assert any(d["status"] == "preserved_validated_artifact" for d in result["diagnostics"])
    evidence = [
        entry
        for entry in _read_resume_marker(tmp_path)["validated_artifacts"]
        if entry["expected_extraction_file"] == expected_file
    ]
    assert len(evidence) == 1
    assert evidence[0]["artifact_sha256"] == hashlib.sha256(original_bytes).hexdigest()


def test_legacy_marker_upgrade_failure_still_preserves_canonical(tmp_path: Path) -> None:
    artifact_path, _source_url, _expected_file, original_bytes, _items = _setup_stale_source(tmp_path)
    from unittest import mock

    from autoscout.codex_recovery import _read_resume_marker
    from autoscout.codex_recovery import _write_resume_marker

    marker = _read_resume_marker(tmp_path)
    marker["validated_artifacts"][0].pop("artifact_sha256")
    _write_resume_marker(tmp_path, marker)

    class FailingFreshAgent:
        def extract_with_timeout(self, item, capture_path, staging_path, *, timeout_seconds):
            return ExtractionAttempt(ExtractionStatus.COMMAND_FAILED, False)

    with mock.patch("autoscout.codex_recovery._write_resume_marker", side_effect=OSError("read only")):
        result = ProductionCodexAdapter(FailingFreshAgent(), runtime_budget_seconds=1, max_attempts=1).fulfill(tmp_path)

    assert artifact_path.read_bytes() == original_bytes
    assert any(d["status"] == "preserved_validated_artifact" for d in result["diagnostics"])


def test_fresh_extraction_atomically_replaces_stale_canonical(tmp_path: Path) -> None:
    """Fresh extraction writes to a unique staging path, validates, then
    atomically replaces the stale canonical artifact. On success the new
    artifact is at the canonical path and the stale bytes are gone."""
    artifact_path, source_url, expected_file, original_bytes, items = _setup_stale_source(tmp_path)

    agent_paths: list[str] = []
    clock = FakeClock()

    class ProducingAgent:
        def extract(self, item, capture_path, artifact_path):
            raise AssertionError("deadline-aware path expected")

        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            agent_paths.append(str(artifact_path))
            clock.advance(10)
            # Write current-valid data to the provided staging path.
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
        ProducingAgent(),
        monotonic=clock,
        sleeper=lambda seconds: clock.advance(seconds),
        runtime_budget_seconds=120,
        max_attempts=1,
    ).fulfill(tmp_path)

    assert result["status"] == "ok"
    assert result["ready_items"] == 1
    # Agent wrote to a staging path, not canonical.
    assert len(agent_paths) == 1
    assert agent_paths[0] != str(artifact_path)
    assert artifact_path.parent == Path(agent_paths[0]).parent
    # Canonical artifact now holds current-valid data.
    assert artifact_path.exists()
    data = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert data["operation_id"] == "current-op"
    assert data["capture_sha256"] == items[0]["capture_sha256"]
    # Stale bytes are gone.
    assert artifact_path.read_bytes() != original_bytes
    # No staging file leaked.
    assert _staging_files(tmp_path, expected_file) == []

    # The stale marker identity is rotated to the fresh operation and repeated
    # fulfilment reuses the artifact without another extraction or duplicate
    # evidence entry.
    from autoscout.codex_recovery import _read_resume_marker

    marker = _read_resume_marker(tmp_path)
    assert marker["run_id"] == "current-op"
    artifact_evidence = [
        entry
        for entry in marker["validated_artifacts"]
        if (
            entry["source_url"],
            entry["expected_extraction_file"],
            entry["capture_file"],
        )
        == (source_url, expected_file, "capture-0.json")
    ]
    assert len(artifact_evidence) == 1
    assert artifact_evidence[0]["run_id"] == "current-op"
    repeated = ProductionCodexAdapter(
        ProducingAgent(),
        monotonic=clock,
        sleeper=lambda seconds: clock.advance(seconds),
        runtime_budget_seconds=120,
        max_attempts=1,
    ).fulfill(tmp_path)
    assert repeated["ready_items"] == 1
    assert len(agent_paths) == 1
    marker = _read_resume_marker(tmp_path)
    artifact_evidence = [
        entry for entry in marker["validated_artifacts"] if entry["expected_extraction_file"] == expected_file
    ]
    assert len(artifact_evidence) == 1
    assert artifact_evidence[0]["run_id"] == "current-op"


def test_changed_stale_bytes_are_not_trusted_from_identity_only_marker(tmp_path: Path) -> None:
    artifact_path, _source_url, _expected_file, _original_bytes, _items = _setup_stale_source(tmp_path)
    artifact_path.write_bytes(artifact_path.read_bytes() + b" changed")

    class FailingAgent:
        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            return ExtractionAttempt(ExtractionStatus.COMMAND_FAILED, False)

    result = ProductionCodexAdapter(FailingAgent(), runtime_budget_seconds=1, max_attempts=1).fulfill(tmp_path)

    assert not artifact_path.exists()
    assert any(d["status"] == "rejected_artifact_preserved" for d in result["diagnostics"])
    assert not any(d["status"] == "preserved_validated_artifact" for d in result["diagnostics"])


def test_publication_failure_preserves_canonical_bytes(tmp_path: Path) -> None:
    """If atomic replacement fails (os.replace raises OSError), the old
    canonical bytes survive untouched and the staging output is quarantined."""
    artifact_path, source_url, expected_file, original_bytes, items = _setup_stale_source(tmp_path)

    agent_paths: list[str] = []
    clock = FakeClock()

    class ProducingButReplacementFailsAgent:
        def extract(self, item, capture_path, artifact_path):
            raise AssertionError("deadline-aware path expected")

        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            agent_paths.append(str(artifact_path))
            clock.advance(10)
            # Write valid data to the staging path.
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

    from unittest import mock as _mock

    def failing_replace(*args, **kwargs):
        raise OSError("simulated replace failure")

    # Simulate replace failure.
    with _mock.patch.object(Path, "replace", side_effect=failing_replace):
        result = ProductionCodexAdapter(
            ProducingButReplacementFailsAgent(),
            monotonic=clock,
            sleeper=lambda seconds: clock.advance(seconds),
            runtime_budget_seconds=120,
            max_attempts=1,
        ).fulfill(tmp_path)

    # Canonical artifact must survive untouched.
    assert artifact_path.exists()
    assert artifact_path.read_bytes() == original_bytes
    # Source not counted as ready.
    assert result["ready_items"] == 0
    # No staging file leaked.
    assert _staging_files(tmp_path, expected_file) == []


def test_serial_attempts_exhaust_budget_at_the_per_attempt_ceiling(tmp_path: Path) -> None:
    """With a single worker, each attempt is offered up to the per-attempt
    ceiling (not the full remaining budget), so a portfolio larger than
    budget/ceiling exhausts the budget partway through and the remaining
    sources are reported budget_exhausted rather than attempted."""
    _portfolio_15(tmp_path)
    clock = FakeClock()

    class RetryableCommandFailAgent:
        def __init__(self) -> None:
            self.timeouts: list[float] = []

        def extract(self, item, capture_path, artifact_path):
            raise AssertionError("deadline-aware path expected")

        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            self.timeouts.append(timeout_seconds)
            clock.advance(timeout_seconds)
            # Retryable COMMAND_FAILED, but max_attempts=1 prevents retries.
            return ExtractionAttempt(ExtractionStatus.COMMAND_FAILED, True, diagnostic="transient")

    budget = CODEX_TIMEOUT_SECONDS * 5
    agent = RetryableCommandFailAgent()
    result = ProductionCodexAdapter(
        agent,
        monotonic=clock,
        sleeper=lambda seconds: clock.advance(seconds),
        runtime_budget_seconds=budget,
        max_attempts=1,
        max_workers=1,  # deterministic fake-clock compatibility
        # Issue #411: this test models the old-style 180-second per-attempt
        # boundary explicitly; the production default is now the absolute
        # 1800-second ceiling.
        per_attempt_timeout_seconds=CODEX_TIMEOUT_SECONDS,
    ).fulfill(tmp_path)

    assert agent.timeouts == [CODEX_TIMEOUT_SECONDS] * 5
    assert result["duration_seconds"] <= budget
    budget_exhausted = [d for d in result["diagnostics"] if d["status"] == "budget_exhausted"]
    assert len(budget_exhausted) == 10


def test_parallel_extraction_is_bounded_and_allows_longer_work(tmp_path: Path) -> None:
    """Concurrency both bounds the worker pool and widens each attempt's slice.

    This one runs on the real clock, because the fake-clock tests cannot
    observe genuine concurrency. The numbers below are therefore chosen for
    margin rather than tightness: dispatch is real-clock too, so a budget only
    slightly above the parallel makespan makes a loaded machine exhaust it
    mid-run and fail assertions that have nothing to do with the behaviour
    under test.
    """
    items = 15
    workers = 3
    work_seconds = 0.04  # scaled analogue of a >30-second extraction
    budget_seconds = 1.2  # 6x the parallel makespan of ceil(15/3) * 0.04
    attempt_ceiling_seconds = 0.12
    _portfolio_15(tmp_path)

    class ParallelAgent:
        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.active = 0
            self.peak = 0
            self.timeouts: list[float] = []
            self.attempt_windows: list[tuple[float, float]] = []

        def extract(self, item, capture_path, artifact_path):
            raise AssertionError("deadline-aware path expected")

        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            with self.lock:
                started = time.monotonic()
                self.active += 1
                self.peak = max(self.peak, self.active)
                self.timeouts.append(timeout_seconds)
            time.sleep(work_seconds)
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
            with self.lock:
                self.attempt_windows.append((started, time.monotonic()))
                self.active -= 1
            return ExtractionAttempt(ExtractionStatus.PRODUCED, True)

    agent = ParallelAgent()
    result = ProductionCodexAdapter(
        agent,
        runtime_budget_seconds=budget_seconds,
        per_attempt_timeout_seconds=attempt_ceiling_seconds,
        max_attempts=1,
        max_workers=workers,
    ).fulfill(tmp_path)

    assert result["ready_items"] == items
    assert agent.peak == workers
    assert len(agent.timeouts) == items
    # "Allows longer work": every attempt, including the last dispatched, gets
    # more than the equal slice a one-at-a-time scheduler could have offered.
    assert min(agent.timeouts) > budget_seconds / items
    # "Bounded": the attempts overlapped. Running them one at a time needs the
    # serial floor, which no amount of scheduling luck can beat. Asserting
    # under the budget itself was wrong as well as flaky -- the adapter
    # dispatches while the budget is unspent, so a run may legitimately
    # overshoot it by the attempt still in flight. The serial floor is the sum
    # of the attempts' own measured durations, not the nominal sleep: a real
    # attempt runs well past work_seconds on a busy machine, and attempts run
    # one at a time can never span less than their summed durations.
    extraction_window = max(end for _, end in agent.attempt_windows) - min(start for start, _ in agent.attempt_windows)
    serial_floor = sum(end - start for start, end in agent.attempt_windows)
    assert extraction_window < serial_floor


def test_per_attempt_timeout_respects_ceiling_when_budget_is_large(tmp_path: Path) -> None:
    """When the remaining budget is large relative to sources, each attempt
    is capped by the per-attempt ceiling (CODEX_TIMEOUT_SECONDS), not the
    much larger remaining budget."""
    items = []
    for index in range(3):
        capture = tmp_path / f"capture-{index}.json"
        source_url = f"https://example.test/source-{index}"
        capture.write_text(json.dumps({"source_url": source_url, "html": "page"}), encoding="utf-8")
        items.append(
            {
                "source_name": f"Source-{index}",
                "source_url": source_url,
                "capture_file": capture.name,
                "expected_extraction_file": f"result-{index}.json",
                "operation_id": f"op-{index}",
                "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
                # Issue #700: the fixture agent writes an empty artifact to
                # stand for "an attempt completed". Declaring the source as
                # one that may report no entries keeps this suite measuring
                # runtime budget rather than the empty-extraction retry.
                "source_metadata": {"may_report_no_entries": True},
            }
        )
    (tmp_path / "codex-extraction-worklist.json").write_text(json.dumps({"work_items": items}), encoding="utf-8")

    clock = FakeClock()

    class TimeoutRecordingAgent:
        def __init__(self) -> None:
            self.timeouts: list[float] = []

        def extract(self, item, capture_path, artifact_path):
            raise AssertionError("deadline-aware path expected")

        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            self.timeouts.append(timeout_seconds)
            clock.advance(timeout_seconds)
            return ExtractionAttempt(ExtractionStatus.TIMEOUT, True, diagnostic="recorded")

    agent = TimeoutRecordingAgent()
    ProductionCodexAdapter(
        agent,
        monotonic=clock,
        sleeper=lambda seconds: clock.advance(seconds),
        runtime_budget_seconds=PORTFOLIO_RUNTIME_BUDGET_SECONDS,
        max_attempts=1,
        # This scheduler test keeps the historical small per-attempt ceiling
        # explicit; the production default is now 1800 seconds.
        per_attempt_timeout_seconds=CODEX_TIMEOUT_SECONDS,
    ).fulfill(tmp_path)

    assert agent.timeouts == [CODEX_TIMEOUT_SECONDS] * 3


# ---------------------------------------------------------------------------
# Worker-exception hardening regression tests
# ---------------------------------------------------------------------------


def test_worker_exception_is_sanitized_and_source_local(tmp_path: Path) -> None:
    """A worker that raises an unexpected Exception produces a source-local
    diagnostic with only exception class name (no message/traceback/paths) and
    does not prevent other sources from succeeding."""
    capture_good = tmp_path / "capture-good.json"
    source_url_good = "https://example.test/good"
    capture_good.write_text(json.dumps({"source_url": source_url_good, "html": ""}), encoding="utf-8")
    capture_bad = tmp_path / "capture-bad.json"
    source_url_bad = "https://example.test/bad"
    capture_bad.write_text(json.dumps({"source_url": source_url_bad, "html": ""}), encoding="utf-8")

    items = [
        {
            "source_name": "GoodSource",
            "source_url": source_url_good,
            "capture_file": "capture-good.json",
            "expected_extraction_file": "result-good.json",
            "operation_id": "op",
            "capture_sha256": hashlib.sha256(capture_good.read_bytes()).hexdigest(),
        },
        {
            "source_name": "BadSource",
            "source_url": source_url_bad,
            "capture_file": "capture-bad.json",
            "expected_extraction_file": "result-bad.json",
            "operation_id": "op",
            "capture_sha256": hashlib.sha256(capture_bad.read_bytes()).hexdigest(),
        },
    ]
    (tmp_path / "codex-extraction-worklist.json").write_text(json.dumps({"work_items": items}), encoding="utf-8")

    class OneRaisesAgent:
        def __init__(self) -> None:
            self.calls = 0

        def extract(self, item, capture_path, artifact_path):
            raise AssertionError("deadline-aware path expected")

        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            self.calls += 1
            name = item["source_name"]
            if name == "BadSource":
                raise RuntimeError(f"/secret/path/to/{name}: sensitive details")
            artifact_path.write_text(
                json.dumps(
                    {
                        "source_name": name,
                        "source_url": item["source_url"],
                        "operation_id": item["operation_id"],
                        "capture_sha256": item["capture_sha256"],
                        "entries": [],
                    }
                ),
                encoding="utf-8",
            )
            return ExtractionAttempt(ExtractionStatus.PRODUCED, True, 0)

    clock = FakeClock()
    agent = OneRaisesAgent()
    result = ProductionCodexAdapter(
        agent,
        monotonic=clock,
        sleeper=lambda seconds: clock.advance(seconds),
        runtime_budget_seconds=120,
        max_attempts=1,
    ).fulfill(tmp_path)

    # GoodSource succeeded.
    assert result["ready_items"] == 1
    assert (tmp_path / "result-good.json").exists()

    # BadSource diagnostic is sanitized: no message, traceback, or path.
    bad_diags = [d for d in result["diagnostics"] if d["source_name"] == "BadSource"]
    assert len(bad_diags) == 1
    bad_diag = bad_diags[0]
    assert bad_diag["status"] == "worker_exception"
    assert bad_diag["diagnostic"] == "RuntimeError"
    assert "secret" not in str(bad_diag)
    assert "/path" not in str(bad_diag)
    assert "sensitive" not in str(bad_diag)
    assert "duration_seconds" in bad_diag

    # Weekly summarization recognizes the status.
    from autoscout.weekly_workflow import _source_problems_from_extraction

    problems = _source_problems_from_extraction(result)
    bad_problem = [p for p in problems if p["source_name"] == "BadSource"]
    assert len(bad_problem) == 1
    assert bad_problem[0]["reason"] == "worker_exception"

    # Outcome is in worklist order.
    source_names = [d["source_name"] for d in result["diagnostics"] if "attempt" in d]
    assert source_names == ["GoodSource", "BadSource"]


def test_executor_shutdown_guaranteed_on_worker_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When a worker raises, the daemon pool's shutdown is invoked with
    wait=False (issue #306: don't re-block on hung workers) and
    cancel_futures=True."""
    _portfolio(tmp_path)

    from autoscout.codex_recovery import _DaemonThreadPool

    shutdown_calls: list[dict[str, Any]] = []
    original_shutdown = _DaemonThreadPool.shutdown

    def _logging_shutdown(pool_self, *shutdown_args, **shutdown_kwargs):
        shutdown_calls.append(dict(kwargs=shutdown_kwargs))
        return original_shutdown(pool_self, *shutdown_args, **shutdown_kwargs)

    monkeypatch.setattr(_DaemonThreadPool, "shutdown", _logging_shutdown)

    class RaisingAgent:
        def extract(self, item, capture_path, artifact_path):
            raise AssertionError("deadline-aware path expected")

        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            raise RuntimeError("boom")

    clock = FakeClock()

    result = ProductionCodexAdapter(
        RaisingAgent(),
        monotonic=clock,
        sleeper=lambda seconds: clock.advance(seconds),
        runtime_budget_seconds=60,
        max_attempts=1,
    ).fulfill(tmp_path)

    assert len(shutdown_calls) >= 1
    for call in shutdown_calls:
        # Issue #306: shutdown with wait=False so a hung worker does not
        # re-block the caller during cleanup. cancel_futures still cancels
        # all queued work.
        assert call["kwargs"].get("wait") is False
        assert call["kwargs"].get("cancel_futures") is True
    # Diagnostics were collected despite the exception.
    assert len(result["diagnostics"]) >= 1


def test_worker_exception_quarantines_partial_staging_artifact(tmp_path: Path) -> None:
    """A staging artifact created before a worker exception is quarantined
    through the established _quarantine_artifact contract (no staging leak),
    and the quarantine diagnostic is emitted alongside the exception diagnostic."""
    capture = tmp_path / "capture-0.json"
    source_url = "https://example.test/source-0"
    capture.write_text(json.dumps({"source_url": source_url, "html": ""}), encoding="utf-8")
    items = [
        {
            "source_name": "TestSource",
            "source_url": source_url,
            "capture_file": "capture-0.json",
            "expected_extraction_file": "result-0.json",
            "operation_id": "op",
            "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
        }
    ]
    (tmp_path / "codex-extraction-worklist.json").write_text(json.dumps({"work_items": items}), encoding="utf-8")

    class WritesThenRaisesAgent:
        def extract(self, item, capture_path, artifact_path):
            raise AssertionError("deadline-aware path expected")

        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            # Write a partial artifact to staging, then raise.
            artifact_path.write_text("partial", encoding="utf-8")
            raise ValueError("mid-extraction failure")

    clock = FakeClock()
    result = ProductionCodexAdapter(
        WritesThenRaisesAgent(),
        monotonic=clock,
        sleeper=lambda seconds: clock.advance(seconds),
        runtime_budget_seconds=120,
        max_attempts=1,
    ).fulfill(tmp_path)

    # No staging files leaked in the working directory.
    assert _staging_files(tmp_path, "result-0.json") == []
    # Source not counted as ready.
    assert result["ready_items"] == 0
    # Quarantine diagnostic emitted (successful preservation).
    quarantine_diags = [d for d in result["diagnostics"] if d["status"] == "rejected_artifact_preserved"]
    assert len(quarantine_diags) == 1
    assert quarantine_diags[0]["source_name"] == "TestSource"
    assert "diagnostic_artifact" in quarantine_diags[0]
    # Exception diagnostic also emitted (sanitized).
    exc_diags = [d for d in result["diagnostics"] if d["status"] == "worker_exception"]
    assert len(exc_diags) == 1
    assert exc_diags[0]["diagnostic"] == "ValueError"


def test_worker_exception_quarantine_preservation_failure_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When quarantine preservation itself fails, the result reports 'failed'
    with reason 'quarantine_preservation_failed', the failed path is reported,
    and the staging bytes are retained (not deleted)."""
    from autoscout.codex_recovery import QuarantineResult
    from autoscout.codex_recovery import QuarantineStatus

    capture = tmp_path / "capture-0.json"
    source_url = "https://example.test/source-0"
    capture.write_text(json.dumps({"source_url": source_url, "html": ""}), encoding="utf-8")
    items = [
        {
            "source_name": "TestSource",
            "source_url": source_url,
            "capture_file": "capture-0.json",
            "expected_extraction_file": "result-0.json",
            "operation_id": "op",
            "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
        }
    ]
    (tmp_path / "codex-extraction-worklist.json").write_text(json.dumps({"work_items": items}), encoding="utf-8")

    class WritesThenRaisesAgent:
        def extract(self, item, capture_path, artifact_path):
            raise AssertionError("deadline-aware path expected")

        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            artifact_path.write_text("partial", encoding="utf-8")
            raise ValueError("mid-extraction failure")

    # Simulate quarantine preservation failure.
    monkeypatch.setattr(
        "autoscout.codex_recovery._quarantine_artifact",
        lambda path: QuarantineResult(QuarantineStatus.PRESERVATION_FAILED, diagnostic="OSError"),
    )

    clock = FakeClock()
    result = ProductionCodexAdapter(
        WritesThenRaisesAgent(),
        monotonic=clock,
        sleeper=lambda seconds: clock.advance(seconds),
        runtime_budget_seconds=120,
        max_attempts=1,
    ).fulfill(tmp_path)

    # Result reports failure for quarantine preservation.
    assert result["status"] == "failed"
    assert result["reason"] == "quarantine_preservation_failed"
    # Failed path reported.
    assert len(result["failed_quarantine_paths"]) == 1
    # Staging bytes retained (not deleted).
    assert len(_staging_files(tmp_path, "result-0.json")) >= 1
    # Quarantine diagnostic reflects preservation failure.
    quarantine_diags = [d for d in result["diagnostics"] if d["status"] == "rejected_artifact_preservation_failed"]
    assert len(quarantine_diags) == 1


def test_production_source_timing_uses_total_attempt_duration(monkeypatch, tmp_path: Path) -> None:
    capture = tmp_path / "capture-0.json"
    source_url = "https://example.test/source-0"
    capture.write_text(json.dumps({"source_url": source_url, "html": ""}), encoding="utf-8")
    items = [
        {
            "source_name": "TimedSource",
            "source_url": source_url,
            "capture_file": capture.name,
            "expected_extraction_file": "result-0.json",
            "operation_id": "op",
            "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
        }
    ]
    (tmp_path / "codex-extraction-worklist.json").write_text(json.dumps({"work_items": items}), encoding="utf-8")
    clock = FakeClock()

    class TimedProducedAgent:
        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            clock.advance(2)
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
            return ExtractionAttempt(ExtractionStatus.PRODUCED, False, 0)

    original_validation = observed_extraction._validation_diagnostic

    def validate_with_time(path, item, capture_path):
        clock.advance(3)
        return original_validation(path, item, capture_path)

    monkeypatch.setattr(observed_extraction, "_validation_diagnostic", validate_with_time)
    result = ProductionCodexAdapter(
        TimedProducedAgent(),
        monotonic=clock,
        sleeper=lambda seconds: clock.advance(seconds),
        runtime_budget_seconds=120,
        max_attempts=2,
        max_workers=1,
    ).fulfill(tmp_path)

    assert result["ready_items"] == 1
    assert result["failed_items"] == 0
    assert result["source_timings"] == [
        {
            "source_name": "TimedSource",
            "status": "ready",
            "terminal": "validated",
            "duration_seconds": 5.0,
            "submission_sequence": 1,
            "worker_slot": 1,
        }
    ]
    assert (tmp_path / "result-0.json").exists()


def test_cumulative_duration_on_retry_with_exception(tmp_path: Path) -> None:
    """Cumulative source_timings duration is correct when a retryable first
    attempt completes and the second attempt raises: total >= sum of both
    attempts, and the per-attempt exception diagnostic carries only the
    second attempt's elapsed time."""
    capture = tmp_path / "capture-0.json"
    source_url = "https://example.test/source-0"
    capture.write_text(json.dumps({"source_url": source_url, "html": ""}), encoding="utf-8")
    items = [
        {
            "source_name": "RetrySource",
            "source_url": source_url,
            "capture_file": "capture-0.json",
            "expected_extraction_file": "result-0.json",
            "operation_id": "op",
            "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
        }
    ]
    (tmp_path / "codex-extraction-worklist.json").write_text(json.dumps({"work_items": items}), encoding="utf-8")

    clock = FakeClock()

    class RetryThenExplodeAgent:
        def __init__(self) -> None:
            self.attempts = 0

        def extract(self, item, capture_path, artifact_path):
            raise AssertionError("deadline-aware path expected")

        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            self.attempts += 1
            if self.attempts == 1:
                clock.advance(10)
                return ExtractionAttempt(ExtractionStatus.COMMAND_FAILED, True, diagnostic="retryable")
            # Second attempt: advance 5s then raise.
            clock.advance(5)
            artifact_path.write_text("partial", encoding="utf-8")
            raise RuntimeError("second attempt boom")

    result = ProductionCodexAdapter(
        RetryThenExplodeAgent(),
        monotonic=clock,
        sleeper=lambda seconds: clock.advance(seconds),
        runtime_budget_seconds=120,
        max_attempts=2,
        per_attempt_timeout_seconds=20,
        max_workers=1,
        retry_headroom_seconds=0,
    ).fulfill(tmp_path)

    # Cumulative duration >= 10 + 5 = 15.
    timings = [t for t in result["source_timings"] if t["source_name"] == "RetrySource"]
    assert len(timings) == 1
    assert timings[0]["duration_seconds"] >= 15
    # Per-attempt exception diagnostic has only the second attempt's elapsed.
    exc_diags = [d for d in result["diagnostics"] if d["status"] == "worker_exception"]
    assert len(exc_diags) == 1
    assert 4 <= exc_diags[0]["duration_seconds"] <= 6
    # Source not ready.
    assert result["ready_items"] == 0


# ---------------------------------------------------------------------------
# Issue #286 regression: realistic attempt durations vs production defaults
# ---------------------------------------------------------------------------


class ThreadSafeFakeClock:
    """A monotonic-style fake clock safe for concurrent access from multiple
    ``ThreadPoolExecutor`` worker threads.

    Advances are additive across threads (no credit is given for true
    wall-clock parallelism), which makes the resulting elapsed time a
    strictly pessimistic upper bound relative to the real deadline-aware
    production path: if the additive total still fits inside the budget,
    a genuinely parallel run comfortably would too. This keeps the test
    deterministic with no real sleeping, matching the fake-clock pattern
    used throughout this file, while still exercising the production
    default of ``DEFAULT_EXTRACTION_WORKERS`` concurrent workers.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.value = 0.0

    def __call__(self) -> float:
        with self._lock:
            return self.value

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.value += seconds


def test_realistic_150_to_180s_attempts_complete_within_production_budget(tmp_path: Path) -> None:
    """Regression for issue #286: production Codex extraction attempts
    observed in the field take roughly 150-180 seconds each. Against the
    production defaults after issue #411 (per-attempt absolute ceiling
    ``CODEX_ABSOLUTE_CEILING_SECONDS`` = 1800, ``PORTFOLIO_RUNTIME_BUDGET_SECONDS``
    = 1800, ``DEFAULT_EXTRACTION_WORKERS`` = 3), a portfolio of
    realistically-slow sources must (a) complete successfully and (b) keep
    the reported wall-clock duration within the portfolio budget."""
    durations = [150.0, 165.0, 180.0, 155.0, 170.0, 160.0]
    items = []
    for index, _ in enumerate(durations):
        capture = tmp_path / f"capture-{index}.json"
        source_url = f"https://example.test/realistic-{index}"
        capture.write_text(json.dumps({"source_url": source_url, "html": "page"}), encoding="utf-8")
        items.append(
            {
                "source_name": f"Realistic-{index}",
                "source_url": source_url,
                "capture_file": capture.name,
                "expected_extraction_file": f"realistic-result-{index}.json",
                "operation_id": f"op-{index}",
                "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
                # Issue #700: the fixture agent writes an empty artifact to
                # stand for "an attempt completed". Declaring the source as
                # one that may report no entries keeps this suite measuring
                # runtime budget rather than the empty-extraction retry.
                "source_metadata": {"may_report_no_entries": True},
            }
        )
    (tmp_path / "codex-extraction-worklist.json").write_text(json.dumps({"work_items": items}), encoding="utf-8")

    clock = ThreadSafeFakeClock()
    offered_timeouts: list[float] = []
    timeouts_lock = threading.Lock()

    class RealisticDurationAgent:
        def extract(self, item, capture_path, artifact_path):
            raise AssertionError("deadline-aware path expected")

        def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
            with timeouts_lock:
                offered_timeouts.append(timeout_seconds)
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

    # No runtime_budget_seconds / per_attempt_timeout_seconds / max_workers
    # overrides: this exercises the unmodified production defaults.
    result = ProductionCodexAdapter(
        RealisticDurationAgent(),
        monotonic=clock,
        sleeper=lambda seconds: clock.advance(seconds),
    ).fulfill(tmp_path)

    # (a) every realistically-slow source completes successfully.
    assert result["status"] == "ok"
    assert result["ready_items"] == len(durations)
    assert result["failed_items"] == 0

    # (b) total wall-clock stays bounded by the production portfolio budget,
    # even under the pessimistic additive-clock model that gives no credit
    # for the 3-way concurrency a real run would benefit from.
    assert result["duration_seconds"] == sum(durations)
    assert result["duration_seconds"] <= PORTFOLIO_RUNTIME_BUDGET_SECONDS

    # Each attempt was offered up to the per-attempt ceiling, clamped by the
    # remaining budget. The realistic 150-180s attempts stay inside the
    # absolute ceiling (issue #411) and complete without ever hitting it.
    #
    # The clamp rule is
    #   offered = min(CODEX_ABSOLUTE_CEILING_SECONDS, portfolio_deadline - attempt_started)
    # so the bound must be two-sided. An upper bound alone stays green while a
    # defect collapses every offered timeout toward zero, which is the exact
    # loss of progress-making work that #411 repaired (issue #416).
    #
    # This clock advances only by the attempt durations, so no attempt can
    # start later than sum(durations) into the run. The lower bound therefore
    # follows from the clamp rule, not from an observed run.
    minimum_offered = PORTFOLIO_RUNTIME_BUDGET_SECONDS - sum(durations)
    assert minimum_offered > 0
    assert len(offered_timeouts) == len(durations)
    for timeout in offered_timeouts:
        assert minimum_offered <= timeout <= CODEX_ABSOLUTE_CEILING_SECONDS
    # The first admitted attempts start with the whole budget in front of them,
    # so the ceiling and not the remaining time binds at least one of them.
    assert max(offered_timeouts) == CODEX_ABSOLUTE_CEILING_SECONDS
    assert max(durations) <= CODEX_ABSOLUTE_CEILING_SECONDS

    # Production runs with bounded concurrency by default.
    assert DEFAULT_EXTRACTION_WORKERS == 3
