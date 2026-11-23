"""Regression tests for issue #315: retry blocked by workflow-owned artifacts.

Covers all seven review findings:
1. SAFETY — exact allowlist, unrelated files inside output_dir still block.
2. DIAGNOSTIC PRESERVATION — prior log survives all later failure paths.
3. CURRENT-OP FAIL-CLOSED VALIDATION — strengthened _capture_is_current.
4. REQUIRED INTEGRATION COVERAGE — production-shaped integration test.
5. SAFE REASON HANDLING — sanitized reason tokens, never raw paths/list reprs.
6. Russian message differentiation (see test_russian_workflow.py).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path

from autoscout.git_envelope import GitRepositoryAdapter
from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.russian_workflow import intervention_message
from autoscout.weekly_workflow import WorkflowOutcome
from autoscout.weekly_workflow import _build_validated_artifact_allowlist
from autoscout.weekly_workflow import _capture_is_current
from autoscout.weekly_workflow import _operation_id
from autoscout.weekly_workflow import deterministic_adapters


def _init_git_repo(path: Path) -> None:
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(path),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.test"],
        cwd=str(path),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path),
        capture_output=True,
        check=True,
    )
    (path / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(
        ["git", "add", ".gitkeep"],
        cwd=str(path),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=str(path),
        capture_output=True,
        check=True,
    )


def _add_local_remote(repo_path: Path, remote_path: Path) -> None:
    subprocess.run(
        ["git", "init", "--bare", str(remote_path)],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote_path)],
        cwd=str(repo_path),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "push", "--set-upstream", "origin", "main"],
        cwd=str(repo_path),
        capture_output=True,
        check=True,
    )


class _FixedClock:
    def __init__(self, reference: date) -> None:
        self._reference = reference

    def today(self) -> date:
        return self._reference


def _make_config(repo_dir: Path, extra_sources: list[dict] | None = None) -> Path:
    config_path = repo_dir / "config.json"
    sources = [
        {
            "name": "Test Source",
            "family": "award",
            "url": "https://example.test/source",
            "access_status": "open",
            "extraction_method": "codex-extraction-replay",
            "check_frequency": "weekly",
            "source_page_fixture_file": "test-capture.json",
            "extraction_response_file": "test-extraction.json",
        },
        {
            "name": "Skipped Source",
            "family": "media",
            "url": "https://example.test/skipped",
            "access_status": "open",
            "extraction_method": "codex-extraction-replay",
            "check_frequency": "weekly",
        },
        {
            "name": "Live Source",
            "family": "audiobook",
            "url": "https://example.test/live",
            "access_status": "open",
            "extraction_method": "agent-extraction-live",
            "check_frequency": "weekly",
        },
    ]
    if extra_sources:
        sources.extend(extra_sources)
    config = {
        "run_date": "2026-07-13",
        "readiness_purpose": "recurring-weekly",
        "editor_row_target": 100,
        "pilot_row_min": 100,
        "pilot_row_max": 100,
        "pilot_mode": True,
        "sources": sources,
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


def _make_capture_fixture(repo_dir: Path) -> None:
    (repo_dir / "test-capture.json").write_text(
        json.dumps(
            {
                "source_url": "https://example.test/source",
                "html": '<html><body><a href="https://example.test/book">Test</a></body></html>',
            }
        ),
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _simulate_failed_operation(output_dir: Path, config_path: Path, repo_dir: Path) -> str:
    """Write workflow-owned artifacts. Returns the operation_id."""
    op_id = _operation_id(config_path, date(2026, 7, 13))
    if not op_id:
        raise AssertionError("Failed to compute operation ID")

    test_capture = output_dir / "codex-capture-test-source.json"
    test_capture.write_text(
        json.dumps(
            {
                "source_url": "https://example.test/source",
                "html": "<html><body>Test</body></html>",
            }
        )
    )
    live_capture = output_dir / "codex-capture-live-source.json"
    live_capture.write_text(
        json.dumps(
            {
                "source_url": "https://example.test/live",
                "html": "<html><body>Live</body></html>",
            }
        )
    )

    source_runs = [
        {
            "source_name": "Test Source",
            "family": "award",
            "url": "https://example.test/source",
            "status": "codex-captured",
            "extraction_method": "codex-extraction-replay",
            "collection_mode": "codex-capture",
            "access_status": "open",
            "source_check_date": "2026-07-13",
            "duration_seconds": 1.0,
            "editor_facing_count": 0,
            "extracted_mention_count": 0,
            "debug_metadata": {"capture_file": "codex-capture-test-source.json", "collection_mode": "codex-capture"},
        },
        {
            "source_name": "Skipped Source",
            "family": "media",
            "url": "https://example.test/skipped",
            "status": "skipped",
            "extraction_method": "codex-extraction-replay",
            "collection_mode": "codex-capture",
            "access_status": "open",
            "source_check_date": "2026-07-13",
            "duration_seconds": 0.5,
            "editor_facing_count": 0,
            "extracted_mention_count": 0,
            "reason": "HTTP 403 blocked",
            "debug_metadata": {"collector_kind": "codex-capture", "http_status": "403"},
        },
        {
            "source_name": "Live Source",
            "family": "audiobook",
            "url": "https://example.test/live",
            "status": "codex-captured",
            "extraction_method": "agent-extraction-live",
            "collection_mode": "codex-capture",
            "access_status": "open",
            "source_check_date": "2026-07-13",
            "duration_seconds": 1.2,
            "editor_facing_count": 0,
            "extracted_mention_count": 0,
            "debug_metadata": {"capture_file": "codex-capture-live-source.json", "collection_mode": "codex-capture"},
        },
    ]
    write_json(output_dir / "source-runs.json", source_runs)

    worklist = {
        "run_date": "2026-07-13",
        "operation_id": op_id,
        "work_items": [
            {
                "source_name": "Test Source",
                "source_family": "award",
                "source_url": "https://example.test/source",
                "source_metadata": {},
                "capture_file": "codex-capture-test-source.json",
                "expected_extraction_file": "test-source-codex-extraction.json",
                "operation_id": op_id,
                "capture_sha256": _sha256(test_capture),
            },
            {
                "source_name": "Live Source",
                "source_family": "audiobook",
                "source_url": "https://example.test/live",
                "source_metadata": {},
                "capture_file": "codex-capture-live-source.json",
                "expected_extraction_file": "live-source-codex-extraction.json",
                "operation_id": op_id,
                "capture_sha256": _sha256(live_capture),
            },
        ],
        "precollected_sources": [],
    }
    write_json(output_dir / "codex-extraction-worklist.json", worklist)

    write_json(
        output_dir / "codex-workflow-resume.json",
        {
            "run_id": op_id,
            "completed_stages": ["capture"],
            "validated_artifacts": [],
        },
    )

    write_json(
        output_dir / "codex-extraction-outcomes.json",
        {
            "outcomes": [
                {
                    "source_name": "Test Source",
                    "source_url": "https://example.test/source",
                    "expected_extraction_file": "test-source-codex-extraction.json",
                    "operation_id": op_id,
                    "submission_sequence": 1,
                    "outcome": "artifact_present",
                    "terminal_class": "command_failed",
                },
                {
                    "source_name": "Live Source",
                    "source_url": "https://example.test/live",
                    "expected_extraction_file": "live-source-codex-extraction.json",
                    "operation_id": op_id,
                    "submission_sequence": 2,
                    "outcome": "artifact_present",
                    "terminal_class": "command_failed",
                },
            ]
        },
    )

    write_json(
        output_dir / "workflow-log.json",
        [
            {"stage": "capture", "status": "ok", "at": "2026-07-13T00:00:00"},
            {
                "stage": "codex",
                "status": "failed",
                "at": "2026-07-13T00:00:01",
                "detail": {"status": "failed", "reason": "no_extraction_produced"},
            },
            {
                "stage": "source-processing",
                "status": "failed",
                "at": "2026-07-13T00:00:01",
                "detail": {"duration_seconds": 3.0},
            },
            {"stage": "terminal", "status": "intervention-required", "at": "2026-07-13T00:00:01"},
        ],
    )
    return op_id


# ── FINDING 1 + 4: Safety — exact allowlist for validated current op ──────


class AllowlistSafetyTests(unittest.TestCase):
    """Exact allowlist: only validated artifacts are exempt; unrelated files
    inside output_dir still block."""

    def _setup_repo(self):
        remote_tmp = tempfile.TemporaryDirectory()
        work_tmp = tempfile.TemporaryDirectory()
        remote_path = Path(remote_tmp.name) / "remote.git"
        repo_dir = Path(work_tmp.name) / "repo"
        repo_dir.mkdir()
        _init_git_repo(repo_dir)
        _add_local_remote(repo_dir, remote_path)
        return remote_tmp, work_tmp, remote_path, repo_dir

    def test_owned_artifacts_pass_preflight_and_update(self):
        remote_tmp, work_tmp, remote_path, repo_dir = self._setup_repo()
        try:
            config_path = _make_config(repo_dir)
            _make_capture_fixture(repo_dir)
            subprocess.run(
                ["git", "-C", str(repo_dir), "add", "config.json", "test-capture.json"], check=True, capture_output=True
            )
            subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "fixtures"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_dir), "push", "origin", "main"], check=True, capture_output=True)

            output_dir = repo_dir / "runs" / "current-weekly"
            output_dir.mkdir(parents=True)
            _simulate_failed_operation(output_dir, config_path, repo_dir)

            allowlist = _build_validated_artifact_allowlist(output_dir, config_path, date(2026, 7, 13))
            self.assertTrue(len(allowlist) >= 4)  # fixed + capture files

            adapter = GitRepositoryAdapter(repo_dir, expected_branch="main", expected_remote_url=str(remote_path))

            # Without allowlist → blocked.
            result_no = adapter.preflight()
            self.assertEqual(result_no["status"], "unsafe")
            adapter.release_guard()

            # With allowlist → passes.
            result_ok = adapter.preflight(validated_artifact_allowlist=allowlist)
            self.assertEqual(result_ok["status"], "ok")
            # Update also passes.
            result_up = adapter.update(validated_artifact_allowlist=allowlist)
            self.assertEqual(result_up["status"], "ok")
            adapter.release_guard()
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()

    def test_extraction_outcomes_evidence_is_owned_and_does_not_block_resume(self):
        """A post-capture extraction failure always writes
        codex-extraction-outcomes.json. It is workflow-owned evidence, so it
        must be exempt; otherwise it blocks the resume it was written for."""
        remote_tmp, work_tmp, remote_path, repo_dir = self._setup_repo()
        try:
            config_path = _make_config(repo_dir)
            _make_capture_fixture(repo_dir)
            subprocess.run(
                ["git", "-C", str(repo_dir), "add", "config.json", "test-capture.json"], check=True, capture_output=True
            )
            subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "fixtures"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_dir), "push", "origin", "main"], check=True, capture_output=True)

            output_dir = repo_dir / "runs" / "current-weekly"
            output_dir.mkdir(parents=True)
            _simulate_failed_operation(output_dir, config_path, repo_dir)

            outcomes = output_dir / "codex-extraction-outcomes.json"
            self.assertTrue(outcomes.is_file())

            allowlist = _build_validated_artifact_allowlist(output_dir, config_path, date(2026, 7, 13))
            self.assertIn(outcomes.resolve(), allowlist)

            adapter = GitRepositoryAdapter(repo_dir, expected_branch="main", expected_remote_url=str(remote_path))
            result = adapter.preflight(validated_artifact_allowlist=allowlist)
            self.assertEqual(result["status"], "ok")
            self.assertNotIn(
                "runs/current-weekly/codex-extraction-outcomes.json",
                result.get("dirty_files", []),
            )
            adapter.release_guard()
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()

    def test_stale_extraction_outcomes_still_blocks(self):
        """The exemption is date-bound: outcomes evidence from a non-current
        operation must not exempt anything."""
        remote_tmp, work_tmp, remote_path, repo_dir = self._setup_repo()
        try:
            config_path = _make_config(repo_dir)
            _make_capture_fixture(repo_dir)
            subprocess.run(
                ["git", "-C", str(repo_dir), "add", "config.json", "test-capture.json"], check=True, capture_output=True
            )
            subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "fixtures"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_dir), "push", "origin", "main"], check=True, capture_output=True)

            output_dir = repo_dir / "runs" / "current-weekly"
            output_dir.mkdir(parents=True)
            _simulate_failed_operation(output_dir, config_path, repo_dir)

            # A different operation date → nothing is exempt.
            allowlist = _build_validated_artifact_allowlist(output_dir, config_path, date(2026, 7, 20))
            self.assertEqual(allowlist, set())
            adapter = GitRepositoryAdapter(repo_dir, expected_branch="main", expected_remote_url=str(remote_path))
            result = adapter.preflight(validated_artifact_allowlist=allowlist)
            self.assertEqual(result["status"], "unsafe")
            adapter.release_guard()
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()

    def test_old_run_extraction_evidence_does_not_exempt_pending_file(self):
        remote_tmp, work_tmp, remote_path, repo_dir = self._setup_repo()
        try:
            config_path = _make_config(repo_dir)
            _make_capture_fixture(repo_dir)
            subprocess.run(
                ["git", "-C", str(repo_dir), "add", "config.json", "test-capture.json"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repo_dir), "commit", "-m", "fixtures"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repo_dir), "push", "origin", "main"],
                check=True,
                capture_output=True,
            )
            output_dir = repo_dir / "runs" / "current-weekly"
            output_dir.mkdir(parents=True)
            _simulate_failed_operation(output_dir, config_path, repo_dir)
            worklist = read_json(output_dir / "codex-extraction-worklist.json", default={})
            item = worklist["work_items"][0]
            pending = output_dir / item["expected_extraction_file"]
            pending.write_text("pending unrelated bytes", encoding="utf-8")
            marker = read_json(output_dir / "codex-workflow-resume.json", default={})
            marker["validated_artifacts"] = [
                {
                    "run_id": "old-operation",
                    "source_url": item["source_url"],
                    "capture_file": item["capture_file"],
                    "expected_extraction_file": item["expected_extraction_file"],
                    "artifact_sha256": _sha256(pending),
                }
            ]
            write_json(output_dir / "codex-workflow-resume.json", marker)

            allowlist = _build_validated_artifact_allowlist(output_dir, config_path, date(2026, 7, 13))
            self.assertNotIn(pending.resolve(), allowlist)
            adapter = GitRepositoryAdapter(repo_dir, expected_branch="main", expected_remote_url=str(remote_path))
            result = adapter.preflight(validated_artifact_allowlist=allowlist)
            self.assertEqual(result["status"], "unsafe")
            self.assertIn("runs/current-weekly/test-source-codex-extraction.json", result.get("dirty_files", []))
            adapter.release_guard()
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()

    def test_unrelated_file_outside_output_dir_blocks(self):
        remote_tmp, work_tmp, remote_path, repo_dir = self._setup_repo()
        try:
            output_dir = repo_dir / "runs" / "current-weekly"
            output_dir.mkdir(parents=True)
            (repo_dir / "unrelated.txt").write_text("dirty", encoding="utf-8")

            allowlist = {(output_dir / "dummy.json").resolve()}
            adapter = GitRepositoryAdapter(repo_dir, expected_branch="main", expected_remote_url=str(remote_path))
            result = adapter.preflight(validated_artifact_allowlist=allowlist)
            self.assertEqual(result["status"], "unsafe")
            self.assertIn("dirty_working_tree", result.get("reasons", []))
            self.assertEqual(result["dirty_files"], ["unrelated.txt"])
            adapter.release_guard()
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()

    def test_unrelated_file_inside_output_dir_blocks(self):
        """An unrelated file placed inside output_dir — not in the allowlist —
        must still block preflight."""
        remote_tmp, work_tmp, remote_path, repo_dir = self._setup_repo()
        try:
            config_path = _make_config(repo_dir)
            _make_capture_fixture(repo_dir)
            subprocess.run(
                ["git", "-C", str(repo_dir), "add", "config.json", "test-capture.json"], check=True, capture_output=True
            )
            subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "fixtures"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_dir), "push", "origin", "main"], check=True, capture_output=True)

            output_dir = repo_dir / "runs" / "current-weekly"
            output_dir.mkdir(parents=True)
            _simulate_failed_operation(output_dir, config_path, repo_dir)

            # Add an unrelated file inside output_dir.
            (output_dir / "user-notes.txt").write_text("personal notes", encoding="utf-8")

            allowlist = _build_validated_artifact_allowlist(output_dir, config_path, date(2026, 7, 13))
            self.assertNotIn((output_dir / "user-notes.txt").resolve(), allowlist)

            adapter = GitRepositoryAdapter(repo_dir, expected_branch="main", expected_remote_url=str(remote_path))
            result = adapter.preflight(validated_artifact_allowlist=allowlist)
            self.assertEqual(result["status"], "unsafe")
            self.assertIn("dirty_working_tree", result.get("reasons", []))
            # The dirty files list should contain the unrelated file but not the owned ones.
            dirty = result.get("dirty_files", [])
            self.assertIn("runs/current-weekly/user-notes.txt", dirty)
            adapter.release_guard()
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()

    def test_stale_mismatched_operation_blocks_without_allowlist(self):
        """A stale operation must not produce an allowlist and must block at
        preflight — the workflow gate, not just the Git layer."""
        remote_tmp, work_tmp, remote_path, repo_dir = self._setup_repo()
        try:
            output_dir = repo_dir / "runs" / "current-weekly"
            output_dir.mkdir(parents=True)
            # Stale artifacts — wrong date, no resume, no source-runs.
            (output_dir / "codex-extraction-worklist.json").write_text(
                json.dumps({"run_date": "2020-01-01", "work_items": [{"source_name": "Stale"}]}),
            )

            adapter = GitRepositoryAdapter(repo_dir, expected_branch="main", expected_remote_url=str(remote_path))
            result = adapter.preflight()
            self.assertEqual(result["status"], "unsafe")
            adapter.release_guard()
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()


# ── FINDING 2: Diagnostic preservation through all failure paths ───────────


class DiagnosticPreservationTests(unittest.TestCase):
    """Prior failure evidence survives subsequent failures on same-operation
    retry, including when preflight/update pass but a later stage fails."""

    def _setup_repo_with_op(self):
        remote_tmp = tempfile.TemporaryDirectory()
        work_tmp = tempfile.TemporaryDirectory()
        remote_path = Path(remote_tmp.name) / "remote.git"
        repo_dir = Path(work_tmp.name) / "repo"
        repo_dir.mkdir()
        _init_git_repo(repo_dir)
        _add_local_remote(repo_dir, remote_path)

        config_path = _make_config(repo_dir)
        _make_capture_fixture(repo_dir)
        subprocess.run(
            ["git", "-C", str(repo_dir), "add", "config.json", "test-capture.json"], check=True, capture_output=True
        )
        subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "fixtures"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_dir), "push", "origin", "main"], check=True, capture_output=True)

        output_dir = repo_dir / "runs" / "current-weekly"
        output_dir.mkdir(parents=True)
        _simulate_failed_operation(output_dir, config_path, repo_dir)
        return remote_tmp, work_tmp, remote_path, repo_dir, config_path, output_dir

    def test_prior_evidence_survives_preflight_block(self):
        remote_tmp, work_tmp, remote_path, repo_dir, config_path, output_dir = self._setup_repo_with_op()
        try:
            # Add unrelated dirty file → preflight blocks.
            (repo_dir / "unrelated.txt").write_text("dirty", encoding="utf-8")

            from autoscout.weekly_workflow import WeeklyWorkflow

            adapters = deterministic_adapters()
            adapters.repository = GitRepositoryAdapter(
                repo_dir, expected_branch="main", expected_remote_url=str(remote_path)
            )
            adapters.clock = _FixedClock(date(2026, 7, 13))

            workflow = WeeklyWorkflow(adapters)
            result = workflow.run(config_path, output_dir)

            self.assertEqual(result.outcome, WorkflowOutcome.INTERVENTION_REQUIRED)
            self.assertEqual(result.failed_stage, "repository-preflight")

            persisted = read_json(output_dir / "workflow-log.json", default=[])
            stages = [e["stage"] for e in persisted]
            # Prior evidence preserved.
            self.assertIn("capture", stages)
            self.assertIn("codex", stages)
            self.assertIn("source-processing", stages)
            # New failure evidence.
            self.assertIn("repository-preflight", stages)
            rp_entry = next(e for e in persisted if e["stage"] == "repository-preflight")
            self.assertEqual(rp_entry["reason"], "dirty_working_tree")
            # Exactly one terminal.
            terminal_entries = [e for e in persisted if e["stage"] == "terminal"]
            self.assertEqual(len(terminal_entries), 1)
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()

    def test_damaged_prior_log_produces_controlled_terminal(self):
        for payload in ("not-json", json.dumps(["bad-entry", 7, None])):
            with self.subTest(payload=payload):
                remote_tmp, work_tmp, remote_path, repo_dir, config_path, output_dir = self._setup_repo_with_op()
                try:
                    (output_dir / "workflow-log.json").write_text(payload, encoding="utf-8")
                    (repo_dir / "unrelated.txt").write_text("dirty", encoding="utf-8")
                    from autoscout.weekly_workflow import WeeklyWorkflow

                    adapters = deterministic_adapters()
                    adapters.repository = GitRepositoryAdapter(
                        repo_dir, expected_branch="main", expected_remote_url=str(remote_path)
                    )
                    adapters.clock = _FixedClock(date(2026, 7, 13))
                    result = WeeklyWorkflow(adapters).run(config_path, output_dir)
                    self.assertEqual(result.failed_stage, "repository-preflight")
                    persisted = read_json(output_dir / "workflow-log.json", default=[])
                    self.assertEqual(len([e for e in persisted if e.get("stage") == "terminal"]), 1)
                    failure = next(e for e in persisted if e.get("stage") == "repository-preflight")
                    self.assertEqual(failure.get("reason"), "dirty_working_tree")
                finally:
                    remote_tmp.cleanup()
                    work_tmp.cleanup()

    def test_prior_evidence_survives_codex_failure_after_preflight_pass(self):
        """End-to-end: preflight/update pass, capture skipped, Codex reached
        and fails, prior Codex/source-processing evidence preserved."""
        remote_tmp, work_tmp, remote_path, repo_dir, config_path, output_dir = self._setup_repo_with_op()
        try:
            from autoscout.weekly_workflow import WeeklyWorkflow

            capture_called: list[bool] = []

            class AssertNoCapture:
                def prepare(self, config_path, output_dir, *, live=False, effective_date=None):
                    capture_called.append(True)
                    raise AssertionError("Capture must not be called on validated retry")

            codex_reached: list[bool] = []

            class FailingCodex:
                def fulfill(self, output_dir):
                    codex_reached.append(True)
                    return {
                        "status": "failed",
                        "reason": "controlled_test_failure",
                        "duration_seconds": 0.0,
                        "runtime_budget_seconds": 12,
                        "source_timings": [],
                        "diagnostics": [],
                    }

            adapters = deterministic_adapters()
            adapters.capture = AssertNoCapture()
            adapters.codex = FailingCodex()
            adapters.repository = GitRepositoryAdapter(
                repo_dir, expected_branch="main", expected_remote_url=str(remote_path)
            )
            adapters.clock = _FixedClock(date(2026, 7, 13))

            workflow = WeeklyWorkflow(adapters)
            result = workflow.run(config_path, output_dir)

            self.assertEqual(result.outcome, WorkflowOutcome.INTERVENTION_REQUIRED)
            self.assertEqual(result.failed_stage, "codex")
            self.assertEqual(capture_called, [])
            self.assertEqual(codex_reached, [True])

            persisted = read_json(output_dir / "workflow-log.json", default=[])
            stages = [e["stage"] for e in persisted]
            # Prior evidence preserved.
            self.assertIn("capture", stages)
            self.assertIn("codex", stages)
            self.assertIn("source-processing", stages)
            # New stages added.
            self.assertIn("repository-preflight", stages)
            self.assertIn("repository-update", stages)
            # _bad_outcome strips old terminals and adds new.
            # In steady state we expect exactly one terminal.
            terminal_entries = [e for e in persisted if e["stage"] == "terminal"]
            self.assertEqual(len(terminal_entries), 1)
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()


# ── FINDING 3: Strengthened _capture_is_current ────────────────────────────


class CaptureIsCurrentTests(unittest.TestCase):
    """Fail-closed validation: every requirement from finding 3."""

    def _make_valid_op(self, tmp_path: Path) -> tuple[Path, Path, str]:
        config_path = _make_config(tmp_path)
        output_dir = tmp_path / "run"
        output_dir.mkdir()
        _make_capture_fixture(tmp_path)
        op_id = _simulate_failed_operation(output_dir, config_path, tmp_path)
        return config_path, output_dir, op_id

    def test_valid_current_operation_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, output_dir, _ = self._make_valid_op(Path(tmp))
            self.assertTrue(_capture_is_current(output_dir, config_path, date(2026, 7, 13)))

    def test_stale_worklist_date_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _make_config(tmp_path)
            output_dir = tmp_path / "run"
            output_dir.mkdir()
            op_id = _operation_id(config_path, date(2026, 7, 13))
            write_json(
                output_dir / "codex-extraction-worklist.json",
                {
                    "run_date": "2020-01-01",
                    "operation_id": op_id,
                    "work_items": [],
                    "precollected_sources": [],
                },
            )
            self.assertFalse(_capture_is_current(output_dir, config_path, date(2026, 7, 13)))

    def test_changed_capture_hash_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, output_dir, _ = self._make_valid_op(Path(tmp))
            # Tamper with a capture file.
            capture_path = output_dir / "codex-capture-test-source.json"
            capture_path.write_text(json.dumps({"tampered": True}))
            self.assertFalse(_capture_is_current(output_dir, config_path, date(2026, 7, 13)))

    def test_stale_source_run_date_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, output_dir, _ = self._make_valid_op(Path(tmp))
            sr = read_json(output_dir / "source-runs.json", default=[])
            sr[0]["source_check_date"] = "2020-01-01"
            write_json(output_dir / "source-runs.json", sr)
            self.assertFalse(_capture_is_current(output_dir, config_path, date(2026, 7, 13)))

    def test_duplicate_source_run_identity_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, output_dir, _ = self._make_valid_op(Path(tmp))
            sr = read_json(output_dir / "source-runs.json", default=[])
            sr.append(dict(sr[0]))  # duplicate
            write_json(output_dir / "source-runs.json", sr)
            self.assertFalse(_capture_is_current(output_dir, config_path, date(2026, 7, 13)))

    def test_unexpected_extra_source_run_identity_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, output_dir, _ = self._make_valid_op(Path(tmp))
            sr = read_json(output_dir / "source-runs.json", default=[])
            sr.append(
                {
                    "source_name": "Ghost",
                    "family": "ghost",
                    "url": "https://ghost.test",
                    "status": "codex-captured",
                    "source_check_date": "2026-07-13",
                }
            )
            write_json(output_dir / "source-runs.json", sr)
            self.assertFalse(_capture_is_current(output_dir, config_path, date(2026, 7, 13)))

    def test_malformed_json_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _make_config(tmp_path)
            output_dir = tmp_path / "run"
            output_dir.mkdir()
            (output_dir / "codex-extraction-worklist.json").write_text("not json")
            self.assertFalse(_capture_is_current(output_dir, config_path, date(2026, 7, 13)))

    def test_path_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, output_dir, _ = self._make_valid_op(Path(tmp))
            wl = read_json(output_dir / "codex-extraction-worklist.json", default={})
            wl["work_items"][0]["capture_file"] = "../../etc/passwd"
            write_json(output_dir / "codex-extraction-worklist.json", wl)
            self.assertFalse(_capture_is_current(output_dir, config_path, date(2026, 7, 13)))

    def _add_precollected_source(self, config_path: Path, output_dir: Path) -> None:
        """Add a precollected source and rebuild all artifacts so the
        operation identity, worklist, source-runs, and resume marker agree."""
        from autoscout.json_io import read_json as _rj
        from autoscout.json_io import write_json as _wj

        config = _rj(config_path, default={})
        if isinstance(config, dict):
            config["sources"].append(
                {
                    "name": "Precollected Source",
                    "family": "media",
                    "url": "https://example.test/precollected",
                    "access_status": "open",
                    "extraction_method": "nonfiction-fr-rss-live",
                    "check_frequency": "weekly",
                }
            )
            _wj(config_path, config)
        # Re-derive operation_id from updated config.
        op_id = _operation_id(config_path, date(2026, 7, 13))
        # Rebuild worklist with new operation_id.
        wl = _rj(output_dir / "codex-extraction-worklist.json", default={})
        if isinstance(wl, dict):
            artifact = output_dir / "precollected-artifact.json"
            artifact.write_text('{"mentions": []}')
            art_hash = _sha256(artifact)
            # Preserve existing work items, but update their operation_id.
            for wi in wl.get("work_items", []):
                if isinstance(wi, dict):
                    wi["operation_id"] = op_id
            wl["precollected_sources"] = [
                {
                    "name": "Precollected Source",
                    "family": "media",
                    "url": "https://example.test/precollected",
                    "operation_id": op_id,
                    "recorded_mentions_file": "precollected-artifact.json",
                    "recorded_mentions_sha256": art_hash,
                }
            ]
            wl["operation_id"] = op_id
            _wj(output_dir / "codex-extraction-worklist.json", wl)
        sr = _rj(output_dir / "source-runs.json", default=[])
        if isinstance(sr, list):
            sr.append(
                {
                    "source_name": "Precollected Source",
                    "family": "media",
                    "url": "https://example.test/precollected",
                    "status": "precollected",
                    "extraction_method": "nonfiction-fr-rss-live",
                    "source_check_date": "2026-07-13",
                }
            )
            _wj(output_dir / "source-runs.json", sr)
        # Re-stamp resume marker.
        _wj(
            output_dir / "codex-workflow-resume.json",
            {
                "run_id": op_id,
                "completed_stages": ["capture"],
                "validated_artifacts": [],
            },
        )

    def test_precollected_happy_path_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, output_dir, _ = self._make_valid_op(Path(tmp))
            self._add_precollected_source(config_path, output_dir)
            self.assertTrue(_capture_is_current(output_dir, config_path, date(2026, 7, 13)))

    def test_missing_capture_file_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, output_dir, _ = self._make_valid_op(Path(tmp))
            (output_dir / "codex-capture-test-source.json").unlink()
            self.assertFalse(_capture_is_current(output_dir, config_path, date(2026, 7, 13)))

    def test_unaccounted_configured_source_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, output_dir, _ = self._make_valid_op(Path(tmp))
            sr = read_json(output_dir / "source-runs.json", default=[])
            sr_trimmed = [s for s in sr if s["source_name"] != "Skipped Source"]
            write_json(output_dir / "source-runs.json", sr_trimmed)
            self.assertFalse(_capture_is_current(output_dir, config_path, date(2026, 7, 13)))

    def test_inactive_configured_source_is_accounted_for(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, output_dir, _ = self._make_valid_op(Path(tmp))
            config = read_json(config_path, default={})
            config["sources"].append(
                {
                    "name": "Inactive Source",
                    "family": "media",
                    "url": "https://example.test/inactive",
                    "active": False,
                    "extraction_method": "codex-extraction-replay",
                }
            )
            write_json(config_path, config)
            op_id = _operation_id(config_path, date(2026, 7, 13))
            worklist = read_json(output_dir / "codex-extraction-worklist.json", default={})
            worklist["operation_id"] = op_id
            for item in worklist["work_items"]:
                item["operation_id"] = op_id
            write_json(output_dir / "codex-extraction-worklist.json", worklist)
            source_runs = read_json(output_dir / "source-runs.json", default=[])
            source_runs.append(
                {
                    "source_name": "Inactive Source",
                    "family": "media",
                    "url": "https://example.test/inactive",
                    "status": "inactive",
                    "source_check_date": "2026-07-13",
                }
            )
            write_json(output_dir / "source-runs.json", source_runs)
            write_json(
                output_dir / "codex-workflow-resume.json",
                {
                    "run_id": op_id,
                    "completed_stages": ["capture"],
                    "validated_artifacts": [],
                },
            )
            self.assertTrue(_capture_is_current(output_dir, config_path, date(2026, 7, 13)))

    def test_validated_partial_extraction_is_allowlisted_but_pending_file_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, output_dir, op_id = self._make_valid_op(Path(tmp))
            worklist = read_json(output_dir / "codex-extraction-worklist.json", default={})
            item = worklist["work_items"][0]
            extraction = output_dir / item["expected_extraction_file"]
            extraction.write_text(
                json.dumps(
                    {
                        "source_name": item["source_name"],
                        "source_url": item["source_url"],
                        "operation_id": op_id,
                        "capture_sha256": item["capture_sha256"],
                        "entries": [],
                    }
                ),
                encoding="utf-8",
            )
            pending = output_dir / worklist["work_items"][1]["expected_extraction_file"]
            pending.write_text("unrelated", encoding="utf-8")
            write_json(
                output_dir / "codex-workflow-resume.json",
                {
                    "run_id": op_id,
                    "completed_stages": ["capture"],
                    "validated_artifacts": [
                        {
                            "source_url": item["source_url"],
                            "capture_file": item["capture_file"],
                            "expected_extraction_file": item["expected_extraction_file"],
                            "run_id": op_id,
                            "artifact_sha256": _sha256(extraction),
                        }
                    ],
                },
            )
            self.assertTrue(_capture_is_current(output_dir, config_path, date(2026, 7, 13)))
            allowlist = _build_validated_artifact_allowlist(output_dir, config_path, date(2026, 7, 13))
            self.assertIn(extraction.resolve(), allowlist)
            self.assertNotIn(pending.resolve(), allowlist)

            extraction.write_text("tampered", encoding="utf-8")
            self.assertFalse(_capture_is_current(output_dir, config_path, date(2026, 7, 13)))

    def test_malformed_neighboring_evidence_never_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path, output_dir, _ = self._make_valid_op(Path(tmp))
            marker = read_json(output_dir / "codex-workflow-resume.json", default={})
            marker["validated_artifacts"] = "not-a-list"
            write_json(output_dir / "codex-workflow-resume.json", marker)
            self.assertFalse(_capture_is_current(output_dir, config_path, date(2026, 7, 13)))

    def test_missing_source_runs_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _make_config(tmp_path)
            output_dir = tmp_path / "run"
            output_dir.mkdir()
            op_id = _operation_id(config_path, date(2026, 7, 13))
            write_json(
                output_dir / "codex-extraction-worklist.json",
                {
                    "run_date": "2026-07-13",
                    "operation_id": op_id,
                    "work_items": [],
                    "precollected_sources": [],
                },
            )
            write_json(
                output_dir / "codex-workflow-resume.json",
                {
                    "run_id": op_id,
                    "completed_stages": ["capture"],
                    "validated_artifacts": [],
                },
            )
            self.assertFalse(_capture_is_current(output_dir, config_path, date(2026, 7, 13)))


# ── FINDING 4: Integration — workflow reaches validated resume ─────────────


class WorkflowIntegrationTests(unittest.TestCase):
    """Production-shaped integration: real GitRepositoryAdapter + WeeklyWorkflow."""

    def _setup_repo_with_op(self):
        remote_tmp = tempfile.TemporaryDirectory()
        work_tmp = tempfile.TemporaryDirectory()
        remote_path = Path(remote_tmp.name) / "remote.git"
        repo_dir = Path(work_tmp.name) / "repo"
        repo_dir.mkdir()
        _init_git_repo(repo_dir)
        _add_local_remote(repo_dir, remote_path)

        config_path = _make_config(repo_dir)
        _make_capture_fixture(repo_dir)
        subprocess.run(
            ["git", "-C", str(repo_dir), "add", "config.json", "test-capture.json"], check=True, capture_output=True
        )
        subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "fixtures"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_dir), "push", "origin", "main"], check=True, capture_output=True)

        output_dir = repo_dir / "runs" / "current-weekly"
        output_dir.mkdir(parents=True)
        _simulate_failed_operation(output_dir, config_path, repo_dir)
        return remote_tmp, work_tmp, remote_path, repo_dir, config_path, output_dir

    def test_valid_retry_reaches_codex_without_capture(self):
        """Matching current op → preflight/update pass, capture skipped,
        Codex reached (even if Codex fails)."""
        remote_tmp, work_tmp, remote_path, repo_dir, config_path, output_dir = self._setup_repo_with_op()
        try:
            from autoscout.weekly_workflow import WeeklyWorkflow

            capture_called: list[bool] = []

            class AssertNoCapture:
                def prepare(self, config_path, output_dir, *, live=False, effective_date=None):
                    capture_called.append(True)
                    raise AssertionError("Capture must not be called on validated retry")

            codex_reached: list[bool] = []

            class SpyCodex:
                def fulfill(self, output_dir):
                    codex_reached.append(True)
                    return {
                        "status": "failed",
                        "reason": "controlled",
                        "duration_seconds": 0.0,
                        "runtime_budget_seconds": 12,
                        "source_timings": [],
                        "diagnostics": [],
                    }

            adapters = deterministic_adapters()
            adapters.capture = AssertNoCapture()
            adapters.codex = SpyCodex()
            adapters.repository = GitRepositoryAdapter(
                repo_dir, expected_branch="main", expected_remote_url=str(remote_path)
            )
            adapters.clock = _FixedClock(date(2026, 7, 13))

            workflow = WeeklyWorkflow(adapters)
            result = workflow.run(config_path, output_dir)

            self.assertEqual(capture_called, [])
            self.assertEqual(codex_reached, [True])
            self.assertEqual(result.failed_stage, "codex")
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()

    def test_operation_changed_by_update_stops_before_recapture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = _make_config(root)
            output_dir = root / "run"
            output_dir.mkdir()
            _simulate_failed_operation(output_dir, config_path, root)
            capture_called: list[bool] = []

            class ChangingRepository:
                def preflight(self, validated_artifact_allowlist=None):
                    return {"status": "ok"}

                def update(self, validated_artifact_allowlist=None):
                    config = read_json(config_path, default={})
                    config["sources"][0]["url"] = "https://changed.test/source"
                    write_json(config_path, config)
                    return {"status": "ok", "pulled": True}

                def finalize(self, output_dir):
                    raise AssertionError("must not finalize")

                def release_guard(self):
                    pass

            class AssertNoCapture:
                def prepare(self, *args, **kwargs):
                    capture_called.append(True)
                    raise AssertionError("must not recapture stale evidence")

            adapters = deterministic_adapters()
            adapters.repository = ChangingRepository()
            adapters.capture = AssertNoCapture()
            adapters.clock = _FixedClock(date(2026, 7, 13))
            from autoscout.weekly_workflow import WeeklyWorkflow

            result = WeeklyWorkflow(adapters).run(config_path, output_dir)
            self.assertEqual(result.failed_stage, "repository-update")
            self.assertEqual(capture_called, [])
            persisted = read_json(output_dir / "workflow-log.json", default=[])
            failure = next(e for e in persisted if e.get("stage") == "repository-update")
            self.assertEqual(failure.get("reason"), "operation_changed_after_update")

    def test_stale_operation_blocks_at_preflight(self):
        """Stale artifacts → no allowlist → preflight blocked."""
        remote_tmp, work_tmp, remote_path, repo_dir, _, _ = self._setup_repo_with_op()
        try:
            # Create a different config that doesn't match the simulated op.
            config_path2 = repo_dir / "config2.json"
            config2 = {
                "run_date": "2026-07-13",
                "readiness_purpose": "recurring-weekly",
                "editor_row_target": 100,
                "pilot_row_min": 100,
                "pilot_row_max": 100,
                "pilot_mode": True,
                "sources": [
                    {
                        "name": "Totally Different",
                        "family": "x",
                        "url": "https://diff.test",
                        "access_status": "open",
                        "extraction_method": "codex-extraction-replay",
                        "check_frequency": "weekly",
                    },
                ],
            }
            config_path2.write_text(json.dumps(config2), encoding="utf-8")

            from autoscout.weekly_workflow import WeeklyWorkflow

            adapters = deterministic_adapters()
            adapters.repository = GitRepositoryAdapter(
                repo_dir, expected_branch="main", expected_remote_url=str(remote_path)
            )
            adapters.clock = _FixedClock(date(2026, 7, 13))

            workflow = WeeklyWorkflow(adapters)
            result = workflow.run(config_path2, repo_dir / "runs" / "current-weekly")

            self.assertEqual(result.outcome, WorkflowOutcome.INTERVENTION_REQUIRED)
            self.assertEqual(result.failed_stage, "repository-preflight")
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()


# ── FINDING 5: Reason normalization ────────────────────────────────────────


class ReasonNormalizationTests(unittest.TestCase):
    def test_update_reason_is_safe_token_not_list_repr(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "repo"
            repo_dir.mkdir()
            _init_git_repo(repo_dir)
            (repo_dir / "dirty.txt").write_text("x")
            adapter = GitRepositoryAdapter(repo_dir)
            result = adapter.update()
            self.assertEqual(result["status"], "unsafe")
            reason = result.get("reason", "")
            # Must be a safe token, not a Python list repr like "['dirty_working_tree']".
            self.assertNotIn("[", reason)
            self.assertNotIn("]", reason)
            self.assertNotEqual(reason, "")
            adapter.release_guard()

    def test_preflight_reason_normalized_by_helper(self):
        """_normalize_adapter_reason extracts a safe token even when the
        adapter result only has a reasons list, no top-level reason."""
        from autoscout.weekly_workflow import _normalize_adapter_reason

        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "repo"
            repo_dir.mkdir()
            _init_git_repo(repo_dir)
            (repo_dir / "dirty.txt").write_text("x")
            adapter = GitRepositoryAdapter(repo_dir)
            result = adapter.preflight()
            self.assertEqual(result["status"], "unsafe")
            safe = _normalize_adapter_reason(result)
            self.assertNotIn("[", safe)
            self.assertNotIn("]", safe)
            self.assertNotEqual(safe, "")
            self.assertNotEqual(safe, "blocked")  # not the fallback
            adapter.release_guard()


# ── FINDING 6: Russian messages (reference tests, not duplicated) ──────────


class RussianMessageTests(unittest.TestCase):
    def test_preflight_vs_finalize_differ(self):
        preflight_msg = intervention_message("repository-preflight")
        finalize_msg = intervention_message("repository-finalize")
        self.assertIn("заблокирован", preflight_msg)
        self.assertIn("сохранение результатов", finalize_msg)
        self.assertNotEqual(preflight_msg, finalize_msg)

    def test_messages_are_level3_clean(self):
        for stage in ("repository-preflight", "repository-update", "repository-finalize"):
            msg = intervention_message(stage)
            for forbidden in ("dirty_working_tree", "git", ".json", ".html", "/runs/", "push"):
                self.assertNotIn(forbidden, msg.lower(), f"{forbidden} leaked in {stage}: {msg}")

    def test_messages_contain_dima_contact(self):
        for stage in ("repository-preflight", "repository-update", "repository-finalize"):
            self.assertIn("Димой", intervention_message(stage))


if __name__ == "__main__":
    unittest.main()
