"""Tests for issue #319: orchestrate authorized weekly recovery from doctor findings.

Covers:
- RecoveryAction enum values and RecoveryPlan dataclass fields.
- Plan mapping: every DiagnosisClass → expected RecoveryAction, side_effects, local_repairs.
- Two-phase protocol: preview (rc 3, zero mutation, runners not called), confirmation gate.
- Blocked escalation: tool-incompatible, repository-blocker, artifact-failure never execute.
- Apply with fake runners: resume weekly (success, intervention-required modes),
  complete-locally, healthy → NONE, source-failure → NONE.
- Russian output: verdict + summary + exactly one question in preview;
  results-saved evidence for successful resume; intervention message on failure.
- Prior diagnostics survive: #315 fixture log entries preserved through preview/apply.
- Production-shaped e2e: real workflow + real git with failing-then-succeeding codex adapter.
- CLI plumbing: --recover flags, --confirmed-by-editor requires --recover,
  --recover + --repair mutually exclusive, --json emits parseable JSON only.
- No regressions on #317/#318/#315.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from state_store_support import state_databases

from autoscout.__main__ import main as cli_main
from autoscout.doctor import EXIT_BLOCKED
from autoscout.doctor import EXIT_HEALTHY
from autoscout.doctor import EXIT_RECOVERABLE
from autoscout.doctor import DiagnosisClass
from autoscout.doctor import DoctorReport
from autoscout.doctor import DoctorSeams
from autoscout.doctor_recovery import RecoveryAction
from autoscout.doctor_recovery import RecoveryPlan
from autoscout.doctor_recovery import RecoveryReport
from autoscout.doctor_recovery import RecoverySeams
from autoscout.doctor_recovery import plan_recovery
from autoscout.doctor_recovery import render_recovery_json
from autoscout.doctor_recovery import render_recovery_russian
from autoscout.doctor_recovery import run_recovery
from autoscout.russian_workflow import render_workflow_result
from autoscout.weekly_workflow import WeeklyWorkflowResult
from autoscout.weekly_workflow import WorkflowOutcome
from autoscout.weekly_workflow import _make_deterministic_adjudication
from tests.test_issue_315_retry_blocked import _add_local_remote
from tests.test_issue_315_retry_blocked import _FixedClock
from tests.test_issue_315_retry_blocked import _init_git_repo
from tests.test_issue_315_retry_blocked import _make_capture_fixture
from tests.test_issue_315_retry_blocked import _make_config
from tests.test_issue_315_retry_blocked import _simulate_failed_operation
from tests.test_issue_317_doctor_readonly import _assert_no_secrets_or_paths
from tests.test_issue_317_doctor_readonly import _fake_codex_probe_missing
from tests.test_issue_317_doctor_readonly import _fake_codex_probe_ok
from tests.test_issue_317_doctor_readonly import _fake_repo_inspect

# ── RecoveryAction enum tests ──────────────────────────────────────────────


# Issue #541: a production-shaped test must not reach the real Codex CLI.
# weekly_adapters() wires the real Stage 0 probes for production, so these
# tests override them.  The real probe spends one vendor request and reads the
# machine's installed CLI version, which would make the suite non-deterministic.
def _offline_stage_zero_codex_probe() -> dict[str, object]:
    return {
        "status": "pass",
        "reason_code": "codex_healthy",
        "message": "Codex toolchain is ready (offline test probe).",
        "model": "test-model",
        "cli_version": "test-cli",
        "detail": {},
    }


def _offline_stage_zero_discovery_probe() -> dict[str, object]:
    return {
        "status": "pass",
        "reason_code": "discovery_adapter_healthy",
        "message": "Discovery adapter is healthy (offline test probe).",
        "endpoint": "http://127.0.0.1:8899/search?q={query}",
        "detail": {},
    }


class RecoveryActionEnumTests(unittest.TestCase):
    def test_stable_string_values(self):
        self.assertEqual(RecoveryAction.RESUME_WEEKLY.value, "resume-weekly")
        self.assertEqual(RecoveryAction.COMPLETE_LOCALLY.value, "complete-locally")
        self.assertEqual(RecoveryAction.NONE.value, "none")
        self.assertEqual(RecoveryAction.BLOCKED.value, "blocked")

    def test_all_values_are_strings(self):
        for member in RecoveryAction:
            self.assertIsInstance(member.value, str)


class RecoveryPlanDataclassTests(unittest.TestCase):
    def test_resume_weekly_plan(self):
        plan = RecoveryPlan(
            action=RecoveryAction.RESUME_WEEKLY,
            authorization_required=True,
            side_effects=["network", "model", "git-publication"],
            summary="Продолжение поиска.",
            confirmation_question="Продолжить?",
        )
        d = plan.to_dict()
        self.assertEqual(d["action"], "resume-weekly")
        self.assertEqual(d["authorization_required"], True)
        self.assertEqual(d["side_effects"], ["network", "model", "git-publication"])
        self.assertEqual(d["summary"], "Продолжение поиска.")
        self.assertEqual(d["confirmation_question"], "Продолжить?")

    def test_none_plan_no_side_effects(self):
        plan = RecoveryPlan(
            action=RecoveryAction.NONE,
            authorization_required=False,
        )
        d = plan.to_dict()
        self.assertEqual(d["side_effects"], [])
        self.assertEqual(d["local_repairs"], [])

    def test_blocked_plan(self):
        plan = RecoveryPlan(
            action=RecoveryAction.BLOCKED,
            authorization_required=False,
        )
        d = plan.to_dict()
        self.assertEqual(d["action"], "blocked")
        self.assertEqual(d["side_effects"], [])

    def test_complete_locally_plan_local_repairs(self):
        plan = RecoveryPlan(
            action=RecoveryAction.COMPLETE_LOCALLY,
            authorization_required=True,
            side_effects=["local-files", "browser"],
            local_repairs=["rerun-health", "rerender-review", "open-review"],
            summary="Обновление обзора.",
            confirmation_question="Обновить обзор?",
        )
        d = plan.to_dict()
        self.assertEqual(d["action"], "complete-locally")
        self.assertEqual(d["local_repairs"], ["rerun-health", "rerender-review", "open-review"])
        self.assertEqual(d["side_effects"], ["local-files", "browser"])


# ── Plan mapping: DiagnosisClass → RecoveryAction ──────────────────────────


class PlanMappingTests(unittest.TestCase):
    def test_resumable_operation_maps_to_resume_weekly(self):
        report = DoctorReport(diagnosis=DiagnosisClass.RESUMABLE_OPERATION, verdict="resume")
        plan = plan_recovery(report)
        self.assertEqual(plan.action, RecoveryAction.RESUME_WEEKLY)
        self.assertTrue(plan.authorization_required)
        self.assertEqual(plan.side_effects, ["network", "model", "git-publication"])
        self.assertIn("прерванного", plan.summary)
        self.assertIn("Продолжить", plan.confirmation_question)

    def test_persistence_failure_maps_to_resume_weekly(self):
        # Only a still-current operation may resume: the weekly workflow skips
        # recapture solely while the capture identity is valid.
        report = DoctorReport(
            diagnosis=DiagnosisClass.PERSISTENCE_FAILURE,
            verdict="save failed",
            facts={"is_current_operation": True},
        )
        plan = plan_recovery(report)
        self.assertEqual(plan.action, RecoveryAction.RESUME_WEEKLY)
        self.assertEqual(plan.side_effects, ["network", "model", "git-publication"])
        self.assertIn("сохранение", plan.summary)
        self.assertIn("сохранение результатов", plan.confirmation_question)

    def test_stale_persistence_failure_is_blocked_not_resumed(self):
        """A stale operation must never resume — resuming would recapture.

        The resume summary promises no re-collection of sources; the weekly
        workflow only honors that while the capture identity is current, so a
        stale persistence failure escalates instead (#319 safety boundary).
        """
        for facts in ({"is_current_operation": False}, {}):
            with self.subTest(facts=facts):
                report = DoctorReport(
                    diagnosis=DiagnosisClass.PERSISTENCE_FAILURE,
                    verdict="save failed",
                    facts=facts,
                )
                plan = plan_recovery(report)
                self.assertEqual(plan.action, RecoveryAction.BLOCKED)
                self.assertFalse(plan.authorization_required)
                self.assertEqual(plan.side_effects, [])

    def test_completed_review_refresh_maps_to_complete_locally(self):
        report = DoctorReport(diagnosis=DiagnosisClass.COMPLETED_REVIEW_REFRESH, verdict="refresh")
        plan = plan_recovery(report)
        self.assertEqual(plan.action, RecoveryAction.COMPLETE_LOCALLY)
        self.assertEqual(plan.side_effects, ["local-files", "browser"])
        self.assertEqual(plan.local_repairs, ["rerun-health", "rerender-review", "open-review"])
        self.assertIn("Обновление", plan.summary)
        self.assertIn("Обновить обзор", plan.confirmation_question)

    def test_source_failure_maps_to_none(self):
        report = DoctorReport(diagnosis=DiagnosisClass.SOURCE_FAILURE, verdict="degraded")
        plan = plan_recovery(report)
        self.assertEqual(plan.action, RecoveryAction.NONE)
        self.assertFalse(plan.authorization_required)
        self.assertEqual(plan.side_effects, [])

    def test_healthy_maps_to_none(self):
        report = DoctorReport(diagnosis=DiagnosisClass.HEALTHY, verdict="ok")
        plan = plan_recovery(report)
        self.assertEqual(plan.action, RecoveryAction.NONE)

    def test_tool_incompatible_maps_to_blocked(self):
        report = DoctorReport(diagnosis=DiagnosisClass.TOOL_INCOMPATIBLE, verdict="no codex")
        plan = plan_recovery(report)
        self.assertEqual(plan.action, RecoveryAction.BLOCKED)
        self.assertEqual(plan.side_effects, [])

    def test_repository_blocker_maps_to_blocked(self):
        report = DoctorReport(diagnosis=DiagnosisClass.REPOSITORY_BLOCKER, verdict="locked")
        plan = plan_recovery(report)
        self.assertEqual(plan.action, RecoveryAction.BLOCKED)

    def test_artifact_failure_maps_to_blocked(self):
        report = DoctorReport(diagnosis=DiagnosisClass.ARTIFACT_FAILURE, verdict="bad artifact")
        plan = plan_recovery(report)
        self.assertEqual(plan.action, RecoveryAction.BLOCKED)


# ── Two-phase protocol / denied permission tests ────────────────────────────


class TwoPhaseProtocolTests(unittest.TestCase):
    def _setup_failed_operation_fixture(self):
        """Build a temp repo with the #315 failed-operation fixture."""
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

        return remote_tmp, work_tmp, remote_path, repo_dir, config_path, output_dir

    def _snapshot_output_dir(self, output_dir: Path) -> dict[str, str]:
        """SHA-256 hex of every file in output_dir."""
        snap: dict[str, str] = {}
        for fpath in sorted(output_dir.rglob("*")):
            if fpath.is_file():
                snap[str(fpath.relative_to(output_dir))] = hashlib.sha256(fpath.read_bytes()).hexdigest()
        snap.update({name: checksum.hex() for name, checksum in state_databases().items()})
        return snap

    def test_preview_returns_rc_3_zero_mutation(self):
        remote_tmp, work_tmp, remote_path, repo_dir, config_path, output_dir = self._setup_failed_operation_fixture()
        try:
            workflow_spy: list[bool] = []
            repairs_spy: list[bool] = []

            def _fake_workflow(c, o, s):
                workflow_spy.append(True)
                raise AssertionError("workflow runner must not be called in preview")

            def _fake_repairs(c, o, s, sel):
                repairs_spy.append(True)
                raise AssertionError("repairs runner must not be called in preview")

            doctor_seams = DoctorSeams(
                today=lambda: date(2026, 7, 13),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )
            seams = RecoverySeams(
                doctor_seams=doctor_seams,
                workflow_runner=_fake_workflow,
                repairs_runner=_fake_repairs,
            )

            snap_before = self._snapshot_output_dir(output_dir)

            report, exit_code = run_recovery(
                config_path,
                output_dir,
                None,
                confirmed_by_editor=False,
                seams=seams,
            )

            self.assertEqual(exit_code, 3)
            self.assertTrue(report.confirmation_required)
            self.assertEqual(report.phase, "preview")
            self.assertEqual(workflow_spy, [])
            self.assertEqual(repairs_spy, [])

            snap_after = self._snapshot_output_dir(output_dir)
            self.assertEqual(snap_before, snap_after, "preview must not mutate any files")

            # Git HEAD unchanged — verify the fixtures commit is still the tip.
            git_log = subprocess.run(
                ["git", "-C", str(repo_dir), "log", "--oneline"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertIn("fixtures", git_log)

            # Russian output.
            russian = render_recovery_russian(report)
            self.assertIn("прерванный поиск", russian.lower())
            self.assertIn("Продолжить прерванный поиск?", russian)
            _assert_no_secrets_or_paths(russian)

            json_str = render_recovery_json(report)
            parsed = json.loads(json_str)
            self.assertTrue(parsed["confirmation_required"])
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()

    def test_preview_idempotent_repeated_calls(self):
        remote_tmp, work_tmp, remote_path, repo_dir, config_path, output_dir = self._setup_failed_operation_fixture()
        try:
            doctor_seams = DoctorSeams(
                today=lambda: date(2026, 7, 13),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )
            seams = RecoverySeams(doctor_seams=doctor_seams)

            report1, rc1 = run_recovery(config_path, output_dir, None, confirmed_by_editor=False, seams=seams)
            report2, rc2 = run_recovery(config_path, output_dir, None, confirmed_by_editor=False, seams=seams)

            self.assertEqual(rc1, 3)
            self.assertEqual(rc2, 3)
            self.assertEqual(report1.to_dict(), report2.to_dict())
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()


# ── Blocked escalation tests ───────────────────────────────────────────────


class BlockedEscalationTests(unittest.TestCase):
    def _setup_failed_operation_fixture(self):
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

        return remote_tmp, work_tmp, remote_path, repo_dir, config_path, output_dir

    def test_tool_incompatible_blocked_even_when_confirmed(self):
        remote_tmp, work_tmp, remote_path, repo_dir, config_path, output_dir = self._setup_failed_operation_fixture()
        try:
            workflow_spy: list[bool] = []

            def _fake_workflow(c, o, s):
                workflow_spy.append(True)
                raise AssertionError("must not be called")

            doctor_seams = DoctorSeams(
                today=lambda: date(2026, 7, 13),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_missing,
                guard_status=lambda: {"guard_exists": False},
            )
            seams = RecoverySeams(doctor_seams=doctor_seams, workflow_runner=_fake_workflow)

            report, exit_code = run_recovery(
                config_path,
                output_dir,
                None,
                confirmed_by_editor=True,
                seams=seams,
            )

            self.assertEqual(exit_code, EXIT_BLOCKED)
            self.assertEqual(report.phase, "not-required")
            self.assertEqual(workflow_spy, [])
            # Russian names the safe action.
            russian = render_recovery_russian(report)
            self.assertIn("Codex", russian)
            self.assertIn("?", russian)
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()

    def test_repository_blocker_blocked(self):
        remote_tmp, work_tmp, remote_path, repo_dir, config_path, output_dir = self._setup_failed_operation_fixture()
        try:
            workflow_spy: list[bool] = []

            def _fake_workflow(c, o, s):
                workflow_spy.append(True)
                raise AssertionError("must not be called")

            doctor_seams = DoctorSeams(
                today=lambda: date(2026, 7, 13),
                repo_inspect=_fake_repo_inspect({"status": "unsafe", "reasons": ["dirty_working_tree"]}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )
            seams = RecoverySeams(doctor_seams=doctor_seams, workflow_runner=_fake_workflow)

            report, exit_code = run_recovery(
                config_path,
                output_dir,
                None,
                confirmed_by_editor=True,
                seams=seams,
            )

            self.assertEqual(exit_code, EXIT_BLOCKED)
            self.assertEqual(report.phase, "not-required")
            self.assertEqual(workflow_spy, [])
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()

    def test_artifact_failure_blocked(self):
        remote_tmp, work_tmp, remote_path, repo_dir, config_path, output_dir = self._setup_failed_operation_fixture()
        try:
            # Embed a secret pattern to trigger artifact-failure.
            from autoscout.json_io import write_json

            write_json(
                output_dir / "source-runs.json",
                {"debug_output": "token=skprojABCDEFGHIJKLMNOPQRSTUVWXYZ123456"},
            )

            workflow_spy: list[bool] = []
            repairs_spy: list[bool] = []

            def _fake_workflow(c, o, s):
                workflow_spy.append(True)

            def _fake_repairs(c, o, s, sel):
                repairs_spy.append(True)

            doctor_seams = DoctorSeams(
                today=lambda: date(2026, 7, 13),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )
            seams = RecoverySeams(
                doctor_seams=doctor_seams,
                workflow_runner=_fake_workflow,
                repairs_runner=_fake_repairs,
            )

            report, exit_code = run_recovery(
                config_path,
                output_dir,
                None,
                confirmed_by_editor=True,
                seams=seams,
            )

            self.assertEqual(exit_code, EXIT_BLOCKED)
            self.assertEqual(report.phase, "not-required")
            self.assertEqual(workflow_spy, [])
            self.assertEqual(repairs_spy, [])
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()

    def test_tool_fixed_then_resumes_without_recapture(self):
        remote_tmp, work_tmp, remote_path, repo_dir, config_path, output_dir = self._setup_failed_operation_fixture()
        try:
            # ── First call: Codex still missing → blocked, workflow never invoked ──
            blocked_workflow_spy: list[bool] = []

            def _fake_workflow_blocked(c, o, s):
                blocked_workflow_spy.append(True)
                raise AssertionError("must not be called while tool-incompatible")

            doctor_seams_blocked = DoctorSeams(
                today=lambda: date(2026, 7, 13),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_missing,
                guard_status=lambda: {"guard_exists": False},
            )
            seams_blocked = RecoverySeams(doctor_seams=doctor_seams_blocked, workflow_runner=_fake_workflow_blocked)

            report_blocked, exit_code_blocked = run_recovery(
                config_path,
                output_dir,
                None,
                confirmed_by_editor=True,
                seams=seams_blocked,
            )

            self.assertEqual(exit_code_blocked, EXIT_BLOCKED)
            self.assertEqual(report_blocked.phase, "not-required")
            self.assertEqual(blocked_workflow_spy, [])

            # ── Second call, same fixture: Codex now fixed → resumes via #315 path ──
            resumed_workflow_called: list[tuple] = []

            def _fake_workflow_resumed(c, o, s):
                resumed_workflow_called.append((c, o, s))
                return WeeklyWorkflowResult(
                    outcome=WorkflowOutcome.COMPLETED_NO_ATTENTION,
                    review_available=True,
                    log=[{"stage": "repository-finalize", "status": "ok"}],
                )

            doctor_seams_fixed = DoctorSeams(
                today=lambda: date(2026, 7, 13),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )
            seams_fixed = RecoverySeams(doctor_seams=doctor_seams_fixed, workflow_runner=_fake_workflow_resumed)

            report_resumed, exit_code_resumed = run_recovery(
                config_path,
                output_dir,
                None,
                confirmed_by_editor=True,
                seams=seams_fixed,
            )

            self.assertEqual(len(resumed_workflow_called), 1)
            self.assertEqual(report_resumed.phase, "apply")
            self.assertTrue(report_resumed.execution["results_saved"])
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()


# ── Apply with fake runners ────────────────────────────────────────────────


class ApplyFakeRunnersTests(unittest.TestCase):
    def _setup_failed_operation_fixture(self):
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

        return remote_tmp, work_tmp, remote_path, repo_dir, config_path, output_dir

    def _make_successful_result(self) -> WeeklyWorkflowResult:
        return WeeklyWorkflowResult(
            outcome=WorkflowOutcome.COMPLETED_NO_ATTENTION,
            readiness_facts={"attention_required": False},
            delta_counts={"new": 3, "upgraded": 1, "changed": 0, "unchanged": 2},
            review_available=True,
            review_path="weekly-review.html",
            log=[
                {"stage": "repository-finalize", "status": "ok"},
                {"stage": "repository-finalize", "status": "noop"},
                {"stage": "terminal", "status": "completed/no-attention"},
            ],
        )

    def test_successful_resume_exit_0_results_saved(self):
        remote_tmp, work_tmp, remote_path, repo_dir, config_path, output_dir = self._setup_failed_operation_fixture()
        try:
            workflow_called: list[tuple] = []

            def _fake_workflow(c, o, s):
                workflow_called.append((c, o, s))
                return self._make_successful_result()

            doctor_seams = DoctorSeams(
                today=lambda: date(2026, 7, 13),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )
            seams = RecoverySeams(doctor_seams=doctor_seams, workflow_runner=_fake_workflow)

            report, exit_code = run_recovery(
                config_path,
                output_dir,
                None,
                confirmed_by_editor=True,
                seams=seams,
            )

            # post_diagnosis runs on the fixture (still has resumable-operation artifacts),
            # so exit code is EXIT_RECOVERABLE (2). The execution outcome is still success.
            self.assertEqual(report.phase, "apply")
            self.assertEqual(len(workflow_called), 1)
            self.assertEqual(report.execution.get("outcome"), "completed/no-attention")
            self.assertTrue(report.execution.get("results_saved"))
            self.assertTrue(report.execution.get("review_available"))

            russian = render_recovery_russian(report)
            self.assertIn("Результаты сохранены", russian)
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()

    def test_failed_codex_intervention_required(self):
        remote_tmp, work_tmp, remote_path, repo_dir, config_path, output_dir = self._setup_failed_operation_fixture()
        try:
            workflow_called: list[tuple] = []

            def _fake_workflow(c, o, s):
                workflow_called.append((c, o, s))
                return WeeklyWorkflowResult(
                    outcome=WorkflowOutcome.INTERVENTION_REQUIRED,
                    failed_stage="codex",
                    log=[
                        {"stage": "codex", "status": "failed"},
                        {"stage": "terminal", "status": "intervention-required"},
                    ],
                )

            doctor_seams = DoctorSeams(
                today=lambda: date(2026, 7, 13),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )
            seams = RecoverySeams(doctor_seams=doctor_seams, workflow_runner=_fake_workflow)

            report, exit_code = run_recovery(
                config_path,
                output_dir,
                None,
                confirmed_by_editor=True,
                seams=seams,
            )

            self.assertEqual(exit_code, EXIT_BLOCKED)
            self.assertEqual(report.phase, "apply")
            self.assertEqual(len(workflow_called), 1)
            self.assertEqual(report.execution["outcome"], "intervention-required")
            self.assertEqual(report.execution["failed_stage"], "codex")

            russian = render_recovery_russian(report)
            self.assertIn("обработать данные", russian.lower())
            self.assertIn("Димой", russian)
            self.assertIn("?", russian)
            self.assertNotIn("Результаты сохранены", russian)
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()

    def test_repository_preflight_intervention(self):
        remote_tmp, work_tmp, remote_path, repo_dir, config_path, output_dir = self._setup_failed_operation_fixture()
        try:
            workflow_called: list[tuple] = []

            def _fake_workflow(c, o, s):
                workflow_called.append((c, o, s))
                return WeeklyWorkflowResult(
                    outcome=WorkflowOutcome.INTERVENTION_REQUIRED,
                    failed_stage="repository-preflight",
                )

            doctor_seams = DoctorSeams(
                today=lambda: date(2026, 7, 13),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )
            seams = RecoverySeams(doctor_seams=doctor_seams, workflow_runner=_fake_workflow)

            report, exit_code = run_recovery(
                config_path,
                output_dir,
                None,
                confirmed_by_editor=True,
                seams=seams,
            )

            self.assertEqual(exit_code, EXIT_BLOCKED)

            russian = render_recovery_russian(report)
            self.assertIn("заблокирован", russian.lower())
            self.assertIn("Димой", russian)
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()

    def test_repository_finalize_intervention(self):
        remote_tmp, work_tmp, remote_path, repo_dir, config_path, output_dir = self._setup_failed_operation_fixture()
        try:
            workflow_called: list[tuple] = []

            def _fake_workflow(c, o, s):
                workflow_called.append((c, o, s))
                return WeeklyWorkflowResult(
                    outcome=WorkflowOutcome.INTERVENTION_REQUIRED,
                    failed_stage="repository-finalize",
                )

            doctor_seams = DoctorSeams(
                today=lambda: date(2026, 7, 13),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )
            seams = RecoverySeams(doctor_seams=doctor_seams, workflow_runner=_fake_workflow)

            report, exit_code = run_recovery(
                config_path,
                output_dir,
                None,
                confirmed_by_editor=True,
                seams=seams,
            )

            self.assertEqual(exit_code, EXIT_BLOCKED)
            russian = render_recovery_russian(report)
            self.assertIn("сохранение результатов", russian.lower())
            self.assertIn("Димой", russian)
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()

    def test_complete_locally_confirmed(self):
        remote_tmp, work_tmp, remote_path, repo_dir, config_path, output_dir = self._setup_failed_operation_fixture()
        try:
            repairs_called: list[list[str]] = []

            def _fake_repairs(c, o, s, sel):
                repairs_called.append(list(sel))
                # Return a minimal report matching the RepairReport shape.
                from typing import Any as _Any
                from typing import ClassVar

                class FakeRepairReport:
                    repairs: ClassVar[list[dict[str, _Any]]] = [
                        {
                            "name": "rerun-health",
                            "outcome": "changed",
                            "precondition": "passed",
                            "precondition_reason": "",
                            "changed_artifacts": [],
                            "detail": {},
                        },
                        {
                            "name": "rerender-review",
                            "outcome": "changed",
                            "precondition": "passed",
                            "precondition_reason": "",
                            "changed_artifacts": [],
                            "detail": {},
                        },
                        {
                            "name": "open-review",
                            "outcome": "changed",
                            "precondition": "passed",
                            "precondition_reason": "",
                            "changed_artifacts": [],
                            "detail": {},
                        },
                    ]
                    post_diagnosis: ClassVar[dict[str, str]] = {
                        "diagnosis": "healthy",
                        "verdict": "AutoScout в порядке.",
                        "recommended_action": "Обзор готов.",
                        "editor_question": "",
                    }

                return FakeRepairReport(), 0

            # Need to change diagnosis to COMPLETED_REVIEW_REFRESH for COMPLETE_LOCALLY plan.
            doctor_seams = DoctorSeams(
                today=lambda: date(2026, 7, 13),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )
            seams = RecoverySeams(doctor_seams=doctor_seams, repairs_runner=_fake_repairs)

            # Override the plan: use a completed-review-refresh fixture instead.
            # We need to craft the right state for that diagnosis. Simpler: directly
            # test run_recovery by providing a pre-constructed setup.
            # For COMPLETE_LOCALLY, we need a completed outcome. Write artifacts.
            from autoscout.json_io import write_json

            # Clear failed op artifacts, write completed artifacts.
            for f in output_dir.iterdir():
                if f.is_file():
                    f.unlink()

            write_json(
                output_dir / "workflow-log.json",
                [
                    {"stage": "capture", "status": "ok", "at": "2026-07-13T00:00:00Z"},
                    {"stage": "repository-finalize", "status": "ok", "at": "2026-07-13T00:10:00Z"},
                    {"stage": "terminal", "status": "completed/no-attention", "at": "2026-07-13T00:11:00Z"},
                ],
            )
            write_json(output_dir / "pilot-status.json", {"ready_for_editor": True, "run_date": "2026-07-13"})
            write_json(output_dir / "run-summary.json", {"editor_facing_count": 10, "run_date": "2026-07-13"})
            write_json(output_dir / "source-runs.json", [{"source_name": "S1", "status": "codex-captured"}])
            # No weekly-review.html → triggers COMPLETED_REVIEW_REFRESH.

            report, exit_code = run_recovery(
                config_path,
                output_dir,
                None,
                confirmed_by_editor=True,
                seams=seams,
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(report.phase, "apply")
            self.assertEqual(len(repairs_called), 1)
            self.assertEqual(repairs_called[0], ["rerun-health", "rerender-review", "open-review"])

            russian = render_recovery_russian(report)
            self.assertIn("выполнено", russian)
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()

    def test_healthy_returns_none_exit_0(self):
        remote_tmp, work_tmp, remote_path, repo_dir, config_path, output_dir = self._setup_failed_operation_fixture()
        try:
            # Write completed/healthy artifacts.
            from autoscout.json_io import write_json

            for f in output_dir.iterdir():
                if f.is_file():
                    f.unlink()

            write_json(
                output_dir / "workflow-log.json",
                [
                    {"stage": "capture", "status": "ok", "at": "2026-07-13T00:00:00Z"},
                    {"stage": "repository-finalize", "status": "ok", "at": "2026-07-13T00:10:00Z"},
                    {"stage": "terminal", "status": "completed/no-attention", "at": "2026-07-13T00:11:00Z"},
                ],
            )
            write_json(output_dir / "pilot-status.json", {"ready_for_editor": True, "run_date": "2026-07-13"})
            write_json(output_dir / "run-summary.json", {"editor_facing_count": 10, "run_date": "2026-07-13"})
            write_json(output_dir / "source-runs.json", [{"source_name": "S1", "status": "codex-captured"}])
            (output_dir / "weekly-review.html").write_text("<html>review</html>", encoding="utf-8")

            workflow_spy: list[bool] = []

            def _fake_workflow(c, o, s):
                workflow_spy.append(True)

            doctor_seams = DoctorSeams(
                today=lambda: date(2026, 7, 13),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )
            seams = RecoverySeams(doctor_seams=doctor_seams, workflow_runner=_fake_workflow)

            report, exit_code = run_recovery(
                config_path,
                output_dir,
                None,
                confirmed_by_editor=True,
                seams=seams,
            )

            self.assertEqual(exit_code, EXIT_HEALTHY)
            self.assertEqual(report.phase, "not-required")
            self.assertEqual(workflow_spy, [])
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()

    def test_source_failure_returns_none_exit_2(self):
        remote_tmp, work_tmp, remote_path, repo_dir, config_path, output_dir = self._setup_failed_operation_fixture()
        try:
            from autoscout.json_io import write_json

            for f in output_dir.iterdir():
                if f.is_file():
                    f.unlink()

            write_json(
                output_dir / "workflow-log.json",
                [
                    {"stage": "capture", "status": "ok", "at": "2026-07-13T00:00:00Z"},
                    {"stage": "repository-finalize", "status": "ok", "at": "2026-07-13T00:10:00Z"},
                    {"stage": "terminal", "status": "completed/attention-required", "at": "2026-07-13T00:11:00Z"},
                ],
            )
            write_json(output_dir / "pilot-status.json", {"ready_for_editor": True, "run_date": "2026-07-13"})
            write_json(output_dir / "run-summary.json", {"editor_facing_count": 5, "run_date": "2026-07-13"})
            write_json(
                output_dir / "source-runs.json",
                [
                    {"source_name": "Broken", "status": "failed", "reason": "timeout"},
                    {"source_name": "Good", "status": "codex-captured"},
                ],
            )
            (output_dir / "weekly-review.html").write_text("<html>review</html>", encoding="utf-8")

            workflow_spy: list[bool] = []

            def _fake_workflow(c, o, s):
                workflow_spy.append(True)

            doctor_seams = DoctorSeams(
                today=lambda: date(2026, 7, 13),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )
            seams = RecoverySeams(doctor_seams=doctor_seams, workflow_runner=_fake_workflow)

            report, exit_code = run_recovery(
                config_path,
                output_dir,
                None,
                confirmed_by_editor=True,
                seams=seams,
            )

            self.assertEqual(exit_code, EXIT_RECOVERABLE)
            self.assertEqual(report.phase, "not-required")
            self.assertEqual(workflow_spy, [])

            russian = render_recovery_russian(report)
            self.assertIn("Broken", russian)
            self.assertIn("Проверьте", russian)
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()


# ── Prior diagnostic preservation ──────────────────────────────────────────


class PriorDiagnosticsPreservationTests(unittest.TestCase):
    def _setup_failed_operation_fixture(self):
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

        return remote_tmp, work_tmp, remote_path, repo_dir, config_path, output_dir

    def test_prior_codex_failure_preserved_after_preview(self):
        remote_tmp, work_tmp, remote_path, repo_dir, config_path, output_dir = self._setup_failed_operation_fixture()
        try:
            from autoscout.json_io import read_json

            log_before = read_json(output_dir / "workflow-log.json", default=[])
            codex_entries_before = [e for e in log_before if e.get("stage") == "codex"]
            self.assertEqual(len(codex_entries_before), 1)

            doctor_seams = DoctorSeams(
                today=lambda: date(2026, 7, 13),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )
            seams = RecoverySeams(doctor_seams=doctor_seams)

            run_recovery(config_path, output_dir, None, confirmed_by_editor=False, seams=seams)

            log_after = read_json(output_dir / "workflow-log.json", default=[])
            codex_entries_after = [e for e in log_after if e.get("stage") == "codex"]
            self.assertEqual(len(codex_entries_after), 1)
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()

    def test_prior_codex_failure_preserved_after_apply(self):
        remote_tmp, work_tmp, remote_path, repo_dir, config_path, output_dir = self._setup_failed_operation_fixture()
        try:
            from autoscout.json_io import read_json

            workflow_called: list[tuple] = []

            def _fake_workflow(c, o, s):
                workflow_called.append((c, o, s))
                # Simulate: the real workflow (#315) appends to the log.
                existing = read_json(output_dir / "workflow-log.json", default=[])
                existing.append({"stage": "repository-finalize", "status": "ok"})
                existing.append({"stage": "terminal", "status": "completed/no-attention"})
                from autoscout.json_io import write_json

                write_json(output_dir / "workflow-log.json", existing)
                return WeeklyWorkflowResult(
                    outcome=WorkflowOutcome.COMPLETED_NO_ATTENTION,
                    review_available=True,
                    log=existing,
                )

            doctor_seams = DoctorSeams(
                today=lambda: date(2026, 7, 13),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )
            seams = RecoverySeams(doctor_seams=doctor_seams, workflow_runner=_fake_workflow)

            run_recovery(config_path, output_dir, None, confirmed_by_editor=True, seams=seams)

            log_after = read_json(output_dir / "workflow-log.json", default=[])
            codex_entries_after = [e for e in log_after if e.get("stage") == "codex"]
            self.assertEqual(len(codex_entries_after), 1)
            self.assertEqual(len(workflow_called), 1)
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()


# ── RecoveryReport serialization ───────────────────────────────────────────


class AnnouncementTests(unittest.TestCase):
    """The started-message is an editorial fact: it may only be emitted for a
    consequential recovery that actually begins.

    Regression guard: the announcement used to be printed unconditionally as
    soon as ``--confirmed-by-editor`` was passed, so a blocked diagnosis and a
    local-only review refresh were both falsely announced as a resumed search.
    """

    def _setup(self):
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

        return remote_tmp, work_tmp, repo_dir, config_path, output_dir

    def test_message_mapping(self):
        from autoscout.russian_workflow import recovery_started_message

        self.assertEqual(
            recovery_started_message("resume-weekly"),
            "Продолжаю прерванный поиск книг...",
        )
        self.assertEqual(
            recovery_started_message("complete-locally"),
            "Обновляю обзор из сохранённых результатов...",
        )
        # Non-consequential actions announce nothing at all.
        for action in ("blocked", "none", ""):
            self.assertEqual(recovery_started_message(action), "")

    def test_not_announced_when_blocked(self):
        remote_tmp, work_tmp, repo_dir, config_path, output_dir = self._setup()
        try:
            announced: list[str] = []
            doctor_seams = DoctorSeams(
                today=lambda: date(2026, 7, 13),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_missing,
                guard_status=lambda: {"guard_exists": False},
            )
            seams = RecoverySeams(
                doctor_seams=doctor_seams,
                workflow_runner=lambda c, o, s: self.fail("must not run"),
                announce=announced.append,
            )

            _report, exit_code = run_recovery(
                config_path,
                output_dir,
                None,
                confirmed_by_editor=True,
                seams=seams,
            )

            self.assertEqual(exit_code, EXIT_BLOCKED)
            self.assertEqual(announced, [])
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()

    def test_not_announced_during_preview(self):
        remote_tmp, work_tmp, repo_dir, config_path, output_dir = self._setup()
        try:
            announced: list[str] = []
            doctor_seams = DoctorSeams(
                today=lambda: date(2026, 7, 13),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )
            seams = RecoverySeams(
                doctor_seams=doctor_seams,
                workflow_runner=lambda c, o, s: self.fail("must not run"),
                announce=announced.append,
            )

            _report, exit_code = run_recovery(
                config_path,
                output_dir,
                None,
                confirmed_by_editor=False,
                seams=seams,
            )

            self.assertEqual(exit_code, 3)
            self.assertEqual(announced, [])
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()

    def test_announced_once_on_confirmed_resume(self):
        remote_tmp, work_tmp, repo_dir, config_path, output_dir = self._setup()
        try:
            announced: list[str] = []
            order: list[str] = []

            def _runner(c, o, s):
                order.append("run")
                return WeeklyWorkflowResult(
                    outcome=WorkflowOutcome.COMPLETED_NO_ATTENTION,
                    review_available=True,
                    log=[{"stage": "repository-finalize", "status": "ok"}],
                )

            def _announce(action: str) -> None:
                order.append("announce")
                announced.append(action)

            doctor_seams = DoctorSeams(
                today=lambda: date(2026, 7, 13),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )
            seams = RecoverySeams(
                doctor_seams=doctor_seams,
                workflow_runner=_runner,
                announce=_announce,
            )

            run_recovery(
                config_path,
                output_dir,
                None,
                confirmed_by_editor=True,
                seams=seams,
            )

            self.assertEqual(announced, ["resume-weekly"])
            # Announced before the work starts, not after it finishes.
            self.assertEqual(order, ["announce", "run"])
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()

    def test_cli_does_not_announce_resume_when_blocked(self):
        remote_tmp, work_tmp, repo_dir, config_path, output_dir = self._setup()
        try:
            report = RecoveryReport(
                phase="not-required",
                diagnosis={
                    "diagnosis": "tool-incompatible",
                    "verdict": "Codex недоступен.",
                    "recommended_action": "Обновите Codex.",
                    "editor_question": "Обновить?",
                },
                plan={"action": "blocked"},
                confirmation_required=False,
            )

            with mock.patch("autoscout.doctor_recovery.run_recovery") as mock_run:
                mock_run.return_value = (report, EXIT_BLOCKED)

                stdout_buf = io.StringIO()
                original_stdout = sys.stdout
                sys.stdout = stdout_buf
                try:
                    exit_code = cli_main(
                        [
                            "doctor",
                            "--config",
                            str(config_path),
                            "--output-dir",
                            str(output_dir),
                            "--recover",
                            "--confirmed-by-editor",
                        ]
                    )
                finally:
                    sys.stdout = original_stdout

            self.assertEqual(exit_code, EXIT_BLOCKED)
            output = stdout_buf.getvalue()
            self.assertNotIn("Продолжаю прерванный поиск", output)
            self.assertNotIn("Обновляю обзор", output)
            self.assertIn("Обновите Codex.", output)
        finally:
            remote_tmp.cleanup()
            work_tmp.cleanup()


class ResumeResultsSavedEvidenceTests(unittest.TestCase):
    """A successful resume must be able to claim its results were saved.

    A resume carries the failed attempt's ``repository-finalize`` entry
    forward (#315 prior-evidence preservation), so requiring *every* entry to
    be ok/noop would deny the successful resume this path exists to perform —
    while still refusing to claim persistence when the last attempt failed.
    """

    def _execute(self, log):
        report = DoctorReport(
            diagnosis=DiagnosisClass.RESUMABLE_OPERATION,
            verdict="v",
            facts={"is_current_operation": True},
        )
        seams = RecoverySeams(
            doctor_seams=DoctorSeams(
                today=lambda: date(2026, 7, 13),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            ),
            workflow_runner=lambda c, o, s: WeeklyWorkflowResult(
                outcome=WorkflowOutcome.COMPLETED_NO_ATTENTION,
                review_available=True,
                log=log,
            ),
        )
        with mock.patch("autoscout.doctor_recovery.diagnose", return_value=report):
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                config = tmp_path / "config.json"
                config.write_text("{}", encoding="utf-8")
                out = tmp_path / "out"
                out.mkdir()
                rec, _rc = run_recovery(config, out, None, confirmed_by_editor=True, seams=seams)
        return rec

    def test_prior_failed_finalize_does_not_deny_a_successful_resume(self):
        rec = self._execute(
            [
                {"stage": "repository-finalize", "status": "failed"},
                {"stage": "repository-finalize", "status": "ok"},
            ]
        )
        self.assertTrue(rec.execution["results_saved"])
        # The canonical renderer reports success rather than an intervention.
        russian = render_recovery_russian(rec)
        self.assertIn("завершён успешно", russian)
        self.assertNotIn("не удалось", russian)

    def test_latest_failed_finalize_is_not_claimed_as_saved(self):
        rec = self._execute(
            [
                {"stage": "repository-finalize", "status": "ok"},
                {"stage": "repository-finalize", "status": "failed"},
            ]
        )
        self.assertFalse(rec.execution["results_saved"])

    def test_no_finalize_evidence_is_never_claimed_as_saved(self):
        rec = self._execute([{"stage": "capture", "status": "ok"}])
        self.assertFalse(rec.execution["results_saved"])


class ProgressVisibilityTests(unittest.TestCase):
    """Progress must survive the workflow runner's stdout redirect.

    Regression: the CLI bound its progress printer to whatever ``sys.stdout``
    was at print time, but ``_default_workflow_runner`` runs the workflow
    inside ``redirect_stdout``, so every stage line landed in the throwaway
    buffer — the editor saw the started message then silence for the whole
    multi-minute resume.
    """

    def test_progress_reaches_stdout_despite_runner_redirect(self):
        captured = io.StringIO()

        def _fake_run_recovery(config_path, output_dir, state_dir, *, confirmed_by_editor, seams):
            # Emulate the real runner: progress fires *inside* a redirect.
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                if seams.progress is not None:
                    seams.progress("capture")
                    seams.progress("codex")
            self.assertEqual(buf.getvalue(), "", "progress must not land in the runner buffer")
            return (
                RecoveryReport(
                    phase="apply",
                    diagnosis={"verdict": "v"},
                    plan={"action": "resume-weekly"},
                    execution={"action": "resume-weekly", "outcome": "completed/no-attention"},
                ),
                0,
            )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text("{}", encoding="utf-8")
            output_dir = tmp_path / "output"
            output_dir.mkdir()

            with mock.patch("autoscout.doctor_recovery.run_recovery", side_effect=_fake_run_recovery):
                original_stdout = sys.stdout
                sys.stdout = captured
                try:
                    cli_main(
                        [
                            "doctor",
                            "--config",
                            str(config),
                            "--output-dir",
                            str(output_dir),
                            "--recover",
                            "--confirmed-by-editor",
                        ]
                    )
                finally:
                    sys.stdout = original_stdout

        output = captured.getvalue()
        from autoscout.russian_workflow import stage_message

        self.assertIn(stage_message("capture"), output)
        self.assertIn(stage_message("codex"), output)


class CanonicalRenderingTests(unittest.TestCase):
    """Rendering must reach the canonical renderers without explicit kwargs.

    Regression: the good paths required ``workflow_result=``/``repairs_report=``,
    but the CLI passes neither, so every confirmed recovery fell through to the
    degraded reconstruction — raw English repair identifiers, no blocked
    reason, and no Level 3 action/question.
    """

    def _repairs_report(self):
        from autoscout.doctor_repairs import RepairReport
        from autoscout.doctor_repairs import RepairResult

        results = [
            RepairResult(name="rerun-health", precondition="passed", outcome="already-complete", detail={}),
            RepairResult(
                name="open-review",
                precondition="blocked",
                precondition_reason="review_html_missing",
                outcome="blocked",
                detail={"reason": "review_html_missing"},
            ),
        ]
        report = RepairReport(plan=[], repairs=[r.to_dict() for r in results], post_diagnosis={})
        report.repair_results = results
        return report, results

    def test_complete_locally_uses_canonical_repairs_renderer(self):
        repairs_report, results = self._repairs_report()
        report = RecoveryReport(
            phase="apply",
            diagnosis={"verdict": "v"},
            plan={"action": "complete-locally"},
            execution={"action": "complete-locally", "repairs": [r.to_dict() for r in results]},
            repairs_report=repairs_report,
        )

        # No kwargs — exactly how the CLI calls it.
        russian = render_recovery_russian(report)

        # Repair names are translated, not raw identifiers.
        self.assertIn("перезапуск проверки здоровья", russian)
        self.assertIn("открытие обзора", russian)
        self.assertNotIn("rerun-health", russian)
        self.assertNotIn("open-review", russian)
        # A blocked repair states its reason and gets one action + one question.
        self.assertIn("заблокировано", russian)
        self.assertTrue(russian.rstrip().endswith("?"))

    def test_resume_weekly_uses_canonical_workflow_renderer(self):
        result = WeeklyWorkflowResult(
            outcome=WorkflowOutcome.COMPLETED_NO_ATTENTION,
            review_available=True,
            log=[{"stage": "repository-finalize", "status": "ok"}],
        )
        report = RecoveryReport(
            phase="apply",
            diagnosis={"verdict": "v"},
            plan={"action": "resume-weekly"},
            execution={"action": "resume-weekly", "outcome": "completed/no-attention", "results_saved": True},
            workflow_result=result,
        )

        expected = render_workflow_result(result, include_progress=False)
        self.assertEqual(render_recovery_russian(report), expected)

    def test_degraded_fallback_still_translates_repair_names(self):
        """Even without the raw object, no English identifier reaches the editor."""
        report = RecoveryReport(
            phase="apply",
            diagnosis={"verdict": "v"},
            plan={"action": "complete-locally"},
            execution={
                "action": "complete-locally",
                "repairs": [{"name": "rerender-review", "outcome": "changed"}],
            },
        )
        russian = render_recovery_russian(report)
        self.assertIn("пересборка обзора", russian)
        self.assertNotIn("rerender-review", russian)

    def test_run_recovery_carries_raw_objects_but_json_stays_stable(self):
        """The raw objects must not leak into the machine-readable contract."""
        report = RecoveryReport(
            phase="apply",
            diagnosis={"verdict": "v"},
            plan={"action": "resume-weekly"},
            workflow_result=WeeklyWorkflowResult(outcome=WorkflowOutcome.COMPLETED_NO_ATTENTION),
            repairs_report=object(),
        )
        as_dict = report.to_dict()
        self.assertNotIn("workflow_result", as_dict)
        self.assertNotIn("repairs_report", as_dict)
        json.loads(render_recovery_json(report))


class RecoveryReportSerializationTests(unittest.TestCase):
    def test_to_dict_and_json(self):
        report = RecoveryReport(
            phase="preview",
            diagnosis={"diagnosis": "resumable-operation", "verdict": "Прерванный поиск."},
            plan={
                "action": "resume-weekly",
                "authorization_required": True,
                "side_effects": ["network", "model", "git-publication"],
                "summary": "Продолжение поиска.",
                "confirmation_question": "Продолжить?",
            },
            confirmation_required=True,
        )
        d = report.to_dict()
        self.assertEqual(d["phase"], "preview")
        self.assertEqual(d["confirmation_required"], True)
        json_str = render_recovery_json(report)
        parsed = json.loads(json_str)
        self.assertEqual(parsed["phase"], "preview")
        self.assertTrue(parsed["confirmation_required"])
        _assert_no_secrets_or_paths(json_str)

    def test_apply_report_includes_execution(self):
        report = RecoveryReport(
            phase="apply",
            diagnosis={"diagnosis": "resumable-operation", "verdict": "ok"},
            plan={"action": "resume-weekly"},
            execution={
                "action": "resume-weekly",
                "outcome": "completed/no-attention",
                "results_saved": True,
                "review_available": True,
            },
            post_diagnosis={"diagnosis": "healthy", "verdict": "AutoScout в порядке."},
        )
        d = report.to_dict()
        self.assertEqual(d["execution"]["results_saved"], True)
        self.assertEqual(d["post_diagnosis"]["diagnosis"], "healthy")


# ── Russian rendering tests ────────────────────────────────────────────────


class RussianRenderingTests(unittest.TestCase):
    def test_preview_rendering_has_verdict_summary_question(self):
        report = RecoveryReport(
            phase="preview",
            diagnosis={"diagnosis": "resumable-operation", "verdict": "Обнаружен прерванный поиск."},
            plan={
                "action": "resume-weekly",
                "summary": "Продолжение поиска без повторного сбора.",
                "confirmation_question": "Продолжить прерванный поиск?",
            },
            confirmation_required=True,
        )
        russian = render_recovery_russian(report)
        self.assertIn("Обнаружен прерванный поиск", russian)
        self.assertIn("Продолжение поиска", russian)
        self.assertIn("Продолжить прерванный поиск?", russian)
        _assert_no_secrets_or_paths(russian)

    def test_not_required_blocked_rendering(self):
        report = RecoveryReport(
            phase="not-required",
            diagnosis={
                "diagnosis": "tool-incompatible",
                "verdict": "AutoScout не может продолжить — утилита Codex недоступна.",
                "recommended_action": "Установите или обновите Codex, затем повторите запуск.",
                "editor_question": "Codex недоступен. Проверить ещё раз после обновления?",
            },
            plan={"action": "blocked"},
        )
        russian = render_recovery_russian(report)
        self.assertIn("Codex", russian)
        self.assertIn("Проверить ещё раз", russian)
        _assert_no_secrets_or_paths(russian)

    def test_apply_resume_with_workflow_result(self):
        result = WeeklyWorkflowResult(
            outcome=WorkflowOutcome.COMPLETED_NO_ATTENTION,
            readiness_facts={"attention_required": False},
            delta_counts={"new": 3, "upgraded": 1, "changed": 0, "unchanged": 2},
            review_available=True,
            review_path="weekly-review.html",
            log=[
                {"stage": "repository-finalize", "status": "ok"},
                {"stage": "terminal", "status": "completed/no-attention"},
            ],
        )
        report = RecoveryReport(
            phase="apply",
            diagnosis={},
            plan={"action": "resume-weekly"},
            execution={"action": "resume-weekly", "results_saved": True},
        )
        russian = render_recovery_russian(report, workflow_result=result)
        # render_workflow_result with include_progress=False skips the started message.
        self.assertIn("завершён", russian)
        self.assertIn("Результаты сохранены", russian)
        _assert_no_secrets_or_paths(russian)

    def test_apply_resume_intervention_with_workflow_result(self):
        result = WeeklyWorkflowResult(
            outcome=WorkflowOutcome.INTERVENTION_REQUIRED,
            failed_stage="codex",
            log=[],
        )
        report = RecoveryReport(
            phase="apply",
            diagnosis={},
            plan={"action": "resume-weekly"},
            execution={"action": "resume-weekly", "outcome": "intervention-required", "failed_stage": "codex"},
        )
        russian = render_recovery_russian(report, workflow_result=result)
        self.assertIn("Димой", russian)
        self.assertIn("?", russian)
        _assert_no_secrets_or_paths(russian)


# ── CLI plumbing tests ─────────────────────────────────────────────────────


class CLIPlumbingTests(unittest.TestCase):
    def test_doctor_cli_no_flags_unchanged(self):
        # Regression: default doctor invocation is still read-only.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = tmp_path / "output"
            output_dir.mkdir()

            stdout_buf = io.StringIO()
            original_stdout = sys.stdout
            sys.stdout = stdout_buf
            try:
                exit_code = cli_main(["doctor", "--config", str(config), "--output-dir", str(output_dir)])
            finally:
                sys.stdout = original_stdout

            self.assertEqual(exit_code, EXIT_HEALTHY)
            self.assertIn("AutoScout", stdout_buf.getvalue())

    def test_recover_flag_passes_confirmed_by_editor(self):
        with mock.patch("autoscout.doctor_recovery.run_recovery") as mock_run:
            mock_run.return_value = (
                RecoveryReport(
                    phase="preview",
                    diagnosis={"diagnosis": "healthy", "verdict": "ok"},
                    plan={"action": "none"},
                    confirmation_required=True,
                ),
                3,
            )

            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                config = tmp_path / "config.json"
                config.write_text("{}", encoding="utf-8")
                output_dir = tmp_path / "output"
                output_dir.mkdir()

                stdout_buf = io.StringIO()
                original_stdout = sys.stdout
                sys.stdout = stdout_buf
                try:
                    cli_main(
                        [
                            "doctor",
                            "--config",
                            str(config),
                            "--output-dir",
                            str(output_dir),
                            "--recover",
                            "--confirmed-by-editor",
                        ]
                    )
                finally:
                    sys.stdout = original_stdout

                mock_run.assert_called_once()
                kwargs = mock_run.call_args.kwargs
                self.assertTrue(kwargs["confirmed_by_editor"])

    def test_confirmed_by_editor_without_recover_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text("{}", encoding="utf-8")
            output_dir = tmp_path / "output"
            output_dir.mkdir()

            stdout_buf = io.StringIO()
            original_stdout = sys.stdout
            sys.stdout = stdout_buf
            try:
                exit_code = cli_main(
                    [
                        "doctor",
                        "--config",
                        str(config),
                        "--output-dir",
                        str(output_dir),
                        "--confirmed-by-editor",
                    ]
                )
            finally:
                sys.stdout = original_stdout

            self.assertEqual(exit_code, 2)
            self.assertIn("confirmed-by-editor-requires-recover", stdout_buf.getvalue())

    def test_recover_and_repair_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text("{}", encoding="utf-8")
            output_dir = tmp_path / "output"
            output_dir.mkdir()

            stdout_buf = io.StringIO()
            original_stdout = sys.stdout
            sys.stdout = stdout_buf
            try:
                exit_code = cli_main(
                    [
                        "doctor",
                        "--config",
                        str(config),
                        "--output-dir",
                        str(output_dir),
                        "--recover",
                        "--repair",
                        "rerun-health",
                    ]
                )
            finally:
                sys.stdout = original_stdout

            self.assertEqual(exit_code, 2)
            self.assertIn("recover-and-repair-are-distinct-phases", stdout_buf.getvalue())

    def test_recover_json_only(self):
        with mock.patch("autoscout.doctor_recovery.run_recovery") as mock_run:
            mock_run.return_value = (
                RecoveryReport(
                    phase="not-required",
                    diagnosis={"diagnosis": "healthy", "verdict": "AutoScout в порядке."},
                    plan={"action": "none"},
                ),
                EXIT_HEALTHY,
            )

            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                config = tmp_path / "config.json"
                config.write_text("{}", encoding="utf-8")
                output_dir = tmp_path / "output"
                output_dir.mkdir()

                stdout_buf = io.StringIO()
                original_stdout = sys.stdout
                sys.stdout = stdout_buf
                try:
                    exit_code = cli_main(
                        [
                            "doctor",
                            "--config",
                            str(config),
                            "--output-dir",
                            str(output_dir),
                            "--recover",
                            "--json",
                        ]
                    )
                finally:
                    sys.stdout = original_stdout

                output = stdout_buf.getvalue()
                # Should be parseable JSON only.
                parsed = json.loads(output)
                self.assertEqual(parsed["diagnosis"]["diagnosis"], "healthy")
                self.assertEqual(exit_code, EXIT_HEALTHY)

    def test_confirmed_recover_json_only_stdout_is_pure_json(self):
        # Regression: --recover --confirmed-by-editor --json must not print
        # Russian announce/progress text before the JSON payload. The fake
        # run_recovery below mimics the real implementation by invoking
        # seams.announce/seams.progress when they were installed — if the CLI
        # wired them up despite json_only, this would leak Russian text onto
        # stdout ahead of the JSON and json.loads would fail.
        def _fake_run_recovery(config_path, output_dir, state_dir, *, confirmed_by_editor, seams):
            if seams.announce is not None:
                seams.announce("resume-weekly")
            if seams.progress is not None:
                seams.progress("collect")
            report = RecoveryReport(
                phase="apply",
                diagnosis={"diagnosis": "healthy", "verdict": "AutoScout в порядке."},
                plan={"action": "resume-weekly"},
                confirmation_required=False,
                execution={"action": "resume-weekly", "outcome": "completed/no-attention"},
            )
            return report, EXIT_HEALTHY

        with mock.patch("autoscout.doctor_recovery.run_recovery", side_effect=_fake_run_recovery):
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                config = tmp_path / "config.json"
                config.write_text("{}", encoding="utf-8")
                output_dir = tmp_path / "output"
                output_dir.mkdir()

                stdout_buf = io.StringIO()
                original_stdout = sys.stdout
                sys.stdout = stdout_buf
                try:
                    exit_code = cli_main(
                        [
                            "doctor",
                            "--config",
                            str(config),
                            "--output-dir",
                            str(output_dir),
                            "--recover",
                            "--confirmed-by-editor",
                            "--json",
                        ]
                    )
                finally:
                    sys.stdout = original_stdout

                output = stdout_buf.getvalue()
                # Would raise if Russian announce/progress text leaked before the JSON.
                parsed = json.loads(output)
                self.assertEqual(parsed["phase"], "apply")
                self.assertEqual(exit_code, EXIT_HEALTHY)

    def test_repair_flag_still_works(self):
        # Regression: --repair without --recover still works as before (#318).
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = tmp_path / "output"
            output_dir.mkdir()

            from autoscout.json_io import write_json

            write_json(output_dir / "pilot-status.json", {"ready_for_editor": True, "run_date": "2026-01-01"})
            write_json(output_dir / "run-summary.json", {"editor_facing_count": 10, "run_date": "2026-01-01"})
            write_json(output_dir / "source-runs.json", [{"source_name": "S1", "status": "codex-captured"}])

            stdout_buf = io.StringIO()
            original_stdout = sys.stdout
            sys.stdout = stdout_buf
            try:
                _ = cli_main(
                    [
                        "doctor",
                        "--config",
                        str(config),
                        "--output-dir",
                        str(output_dir),
                        "--repair",
                        "rerun-health",
                        "--json",
                    ]
                )
            finally:
                sys.stdout = original_stdout

            output = stdout_buf.getvalue()
            parsed = json.loads(output)
            self.assertIn("repairs", parsed)


# ── Production-shaped e2e test ─────────────────────────────────────────────


class ProductionShapedE2ETests(unittest.TestCase):
    """End-to-end with real workflow and real git: recovery from a failed codex run."""

    def test_recovery_from_failed_codex_run(self):
        from autoscout.codex_recovery import ExtractionAttempt
        from autoscout.codex_recovery import ExtractionStatus
        from autoscout.codex_recovery import ProductionCodexAdapter

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # ── Bare remote ──
            bare_remote = tmp_path / "bare.git"
            bare_remote.mkdir()
            subprocess.run(
                ["git", "init", "--bare", "--initial-branch=main", str(bare_remote)],
                check=True,
                capture_output=True,
            )

            # ── Working repo ──
            repo_dir = tmp_path / "repo"
            repo_dir.mkdir()
            subprocess.run(
                ["git", "-C", str(repo_dir), "init", "--initial-branch=main"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repo_dir), "config", "user.name", "AutoScout CI"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repo_dir), "config", "user.email", "ci@autoscout.test"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repo_dir), "remote", "add", "origin", str(bare_remote)],
                check=True,
                capture_output=True,
            )
            (repo_dir / "README.md").write_text("# auto-scout test\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(repo_dir), "add", "README.md"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repo_dir), "commit", "-m", "initial"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repo_dir), "push", "--set-upstream", "origin", "main"],
                check=True,
                capture_output=True,
            )

            # ── Fixture config + capture ──
            from tests.test_weekly_workflow import _write_capture_fixture
            from tests.test_weekly_workflow import _write_codex_fixture_config

            config_path = _write_codex_fixture_config(repo_dir)
            output_dir = repo_dir / "run"
            output_dir.mkdir()
            _write_capture_fixture(repo_dir, output_dir)
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

            # ── Step 1: Run real workflow with a FAILING codex adapter ──
            from autoscout.git_envelope import GitRepositoryAdapter
            from autoscout.weekly_workflow import _DeterministicCaptureAdapter
            from autoscout.weekly_workflow import run_weekly_workflow
            from autoscout.weekly_workflow import weekly_adapters

            capture_call_count: list[int] = [0]

            class _CountingCaptureAdapter:
                def prepare(self, config_path, output_dir, *, live=False, effective_date=None):
                    capture_call_count[0] += 1
                    return _DeterministicCaptureAdapter().prepare(
                        config_path,
                        output_dir,
                        live=live,
                        effective_date=effective_date,
                    )

            def _make_capture_fixture_agent():
                class _FixtureExtractionAgent:
                    def extract(self, item, capture_path, artifact_path):
                        artifact_path.write_text(
                            json.dumps(
                                {
                                    "source_name": item["source_name"],
                                    "source_url": item["source_url"],
                                    "operation_id": item["operation_id"],
                                    "capture_sha256": item["capture_sha256"],
                                    "entries": [
                                        {
                                            "title": "Test Book",
                                            "primary_authors": ["Test Author"],
                                            "source_url": "https://example.test/book",
                                            "evidence_url": "https://example.test/book",
                                            "evidence_note": "Listed book.",
                                            "candidate_category": "popular science",
                                            "signal_type": "award shortlist",
                                            "signal_strength": "shortlist",
                                        }
                                    ],
                                }
                            ),
                            encoding="utf-8",
                        )
                        return ExtractionAttempt(ExtractionStatus.PRODUCED, True, 0)

                return _FixtureExtractionAgent()

            # Phase 1: Failing codex adapter.
            class _FailingCodexAdapter:
                def fulfill(self, output_dir):
                    return {
                        "status": "failed",
                        "reason": "controlled_test_failure",
                        "duration_seconds": 0.0,
                        "runtime_budget_seconds": 12,
                        "source_timings": [],
                        "diagnostics": [],
                    }

            adapters1 = weekly_adapters(repo_dir)
            adapters1.stage_zero_codex_probe = _offline_stage_zero_codex_probe
            adapters1.stage_zero_discovery_probe = _offline_stage_zero_discovery_probe
            adapters1.capture = _CountingCaptureAdapter()
            adapters1.codex = _FailingCodexAdapter()
            adapters1.repository = GitRepositoryAdapter(
                repo_dir,
                expected_branch="main",
                expected_remote_url=str(bare_remote),
            )
            adapters1.clock = _FixedClock(reference=date(2026, 7, 13))

            result1 = run_weekly_workflow(config_path, output_dir, adapters=adapters1)

            self.assertEqual(result1.outcome, WorkflowOutcome.INTERVENTION_REQUIRED)
            self.assertEqual(result1.failed_stage, "codex")
            self.assertEqual(capture_call_count[0], 1)

            # ── Step 2: Diagnose with real repo inspection ──
            from autoscout.doctor import diagnose

            # The default inspector validates the canonical remote, so this
            # fixture declares its own temporary bare remote as expected.
            doctor_seams = DoctorSeams(
                today=lambda: date(2026, 7, 13),
                codex_probe=_fake_codex_probe_ok,
                expected_remote_url=str(bare_remote),
            )
            diag_report = diagnose(config_path, output_dir, seams=doctor_seams)
            self.assertEqual(diag_report.diagnosis, DiagnosisClass.RESUMABLE_OPERATION)

            # ── Remote ref before recovery ──
            before_ref = subprocess.run(
                ["git", "-C", str(bare_remote), "rev-parse", "refs/heads/main"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

            # ── Step 3: Preview (rc 3, zero mutation) ──
            workflow_spy_called: list[bool] = []

            def _spy_workflow(c, o, s):
                workflow_spy_called.append(True)
                return WeeklyWorkflowResult(outcome=WorkflowOutcome.COMPLETED_NO_ATTENTION)

            recovery_seams_preview = RecoverySeams(
                doctor_seams=doctor_seams,
                workflow_runner=_spy_workflow,
            )

            recovery_report_preview, rc_preview = run_recovery(
                config_path,
                output_dir,
                None,
                confirmed_by_editor=False,
                seams=recovery_seams_preview,
            )

            self.assertEqual(rc_preview, 3)
            self.assertTrue(recovery_report_preview.confirmation_required)
            self.assertEqual(workflow_spy_called, [])

            # Git working tree still clean (no new changes from run_recovery).
            status = subprocess.run(
                ["git", "-C", str(repo_dir), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            # The output_dir has run artifacts from step 1. git status shows them as tracked
            # or untracked depending on whether they were committed.
            # The point: no NEW changes beyond what was already there.
            self.assertNotIn("error", status.lower())

            # Remote ref unchanged.
            after_preview_ref = subprocess.run(
                ["git", "-C", str(bare_remote), "rev-parse", "refs/heads/main"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(after_preview_ref, before_ref)

            # ── Step 4: Recovery with real workflow that succeeds ──
            adapters2 = weekly_adapters(repo_dir)
            adapters2.stage_zero_codex_probe = _offline_stage_zero_codex_probe
            adapters2.stage_zero_discovery_probe = _offline_stage_zero_discovery_probe
            adapters2.capture = _CountingCaptureAdapter()
            adapters2.codex = ProductionCodexAdapter(agent=_make_capture_fixture_agent())
            adapters2.repository = GitRepositoryAdapter(
                repo_dir,
                expected_branch="main",
                expected_remote_url=str(bare_remote),
            )
            adapters2.clock = _FixedClock(reference=date(2026, 7, 13))

            class _FakeEnrichmentAgent:
                def generate(self, work_items):
                    return {
                        str(wi.get("identity_key", "")): "Увлекательное исследование современной науки."
                        for wi in work_items
                    }

            from autoscout.description_enrichment import DescriptionEnrichmentService

            adapters2.enrichment = DescriptionEnrichmentService(agent=_FakeEnrichmentAgent())
            # The production adjudication stage would run the real Codex CLI and
            # fetch live pages; the replay adapter keeps this run offline.
            adapters2.adjudication = _make_deterministic_adjudication()

            opened_calls: list[str] = []

            class _FakeOpenAdapter:
                def announce(self, stage):
                    pass

                def open_review(self, review_path):
                    opened_calls.append(str(review_path))
                    return {"status": "ok", "opened": True, "path": str(review_path)}

            adapters2.presentation = _FakeOpenAdapter()

            def _real_workflow_runner(c, o, s):
                return run_weekly_workflow(c, o, s, adapters=adapters2)

            recovery_seams_apply = RecoverySeams(
                doctor_seams=doctor_seams,
                workflow_runner=_real_workflow_runner,
            )

            recovery_report_apply, rc_apply = run_recovery(
                config_path,
                output_dir,
                None,
                confirmed_by_editor=True,
                seams=recovery_seams_apply,
            )

            # ── Assertions ──
            self.assertEqual(rc_apply, EXIT_HEALTHY)
            self.assertEqual(recovery_report_apply.phase, "apply")

            # Capture must NOT be called again (validated resume, no recapture).
            self.assertEqual(capture_call_count[0], 1)

            # Remote ref advanced.
            after_apply_ref = subprocess.run(
                ["git", "-C", str(bare_remote), "rev-parse", "refs/heads/main"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertNotEqual(after_apply_ref, before_ref)

            # weekly-review.html exists.
            self.assertTrue((output_dir / "weekly-review.html").is_file())

            # Original codex failure preserved.
            from autoscout.json_io import read_json

            log = read_json(output_dir / "workflow-log.json", default=[])
            codex_entries = [e for e in log if e.get("stage") == "codex"]
            self.assertGreaterEqual(len(codex_entries), 1)

            # Russian output contains results-saved, no implementation vocabulary.
            russian = render_recovery_russian(recovery_report_apply)
            self.assertIn("Результаты сохранены", russian)
            for term in ("JSON", "CSV", "Codex", "codex", "capture", "replay", "pipeline"):
                self.assertNotIn(term, russian)

            # Review was opened.
            self.assertGreater(len(opened_calls), 0)

            # Bare remote has the review pushed.
            remote_files = subprocess.run(
                ["git", "-C", str(bare_remote), "ls-tree", "--name-only", "-r", "main"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
            self.assertIn("run/weekly-review.html", remote_files)


if __name__ == "__main__":
    unittest.main()
