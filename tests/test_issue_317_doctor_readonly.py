"""Tests for issue #317: read-only doctor diagnostics and blessed launcher.

Covers:
- Every DiagnosisClass with deterministic fixture coverage.
- Read-only behavior: state files and Git refs unchanged before/after.
- No git fetch/network calls.
- Tool incompatibility distinct from other classes.
- Resume classification agrees with _capture_is_current (#315).
- Russian output contains verdict, recommended action, question when blocked.
- JSON output contains same verdict/action as Russian.
- Redaction contract: no absolute paths, no stack traces, no secrets in output.
- Exit codes per class.
- Launcher behavior matches existing launcher conventions.
- Classification fallthrough: intervention-required without resumable op (#4a).
- Classification fallthrough: completed outcome without repo-finalize entries (#4b).
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import date
from pathlib import Path

from state_store_support import state_databases

from autoscout.__main__ import main as cli_main
from autoscout.doctor import EXIT_BLOCKED
from autoscout.doctor import EXIT_HEALTHY
from autoscout.doctor import EXIT_RECOVERABLE
from autoscout.doctor import DiagnosisClass
from autoscout.doctor import DoctorReport
from autoscout.doctor import DoctorSeams
from autoscout.doctor import _codex_compat_probe
from autoscout.doctor import _codex_probe
from autoscout.doctor import diagnose
from autoscout.doctor import exit_code_for
from autoscout.doctor import render_json_report
from autoscout.doctor import render_russian
from autoscout.weekly_workflow import _capture_is_current

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_DOCTOR_LAUNCHER = REPO_ROOT / "scripts" / "doctor"


# ── Helpers ───────────────────────────────────────────────────────────────


def _init_bare_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.test"], cwd=str(path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), capture_output=True, check=True)


def _fake_repo_inspect(result: dict) -> dict:
    def _inspect(_allowlist: set[Path] | None = None) -> dict:
        return dict(result)

    return _inspect


def _make_output_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_workflow_log(output_dir: Path, entries: list[dict]) -> None:
    (output_dir / "workflow-log.json").write_text(json.dumps(entries), encoding="utf-8")


def _write_artifact(output_dir: Path, name: str, data: object) -> None:
    (output_dir / name).write_text(json.dumps(data), encoding="utf-8")


# Sanitized codex probe fakes (matching the new sanitized key names).
def _fake_codex_probe_ok() -> dict:
    return {"codex_available": True, "codex_version": "0.1.0"}


def _fake_codex_probe_missing() -> dict:
    return {"codex_available": False, "error": "codex_not_on_path"}


# ── Redaction contract ────────────────────────────────────────────────────


def _assert_no_secrets_or_paths(output: str) -> None:
    """Defense-in-depth checks that output follows the clean editor contract."""
    # No absolute paths (Unix or macOS style).
    for line in output.splitlines():
        if line.startswith("/") and len(line) > 1 and line[1] != "/":
            if any(segment in line for segment in ["/Users/", "/tmp/", "/private/", "/home/"]):
                raise AssertionError(f"Absolute path in doctor output: {line}")
    # No stack-trace markers.
    assert "Traceback" not in output, f"Stack trace in output: {output}"
    # No raw Git directives.
    for directive in ("::git-stage", "::git-commit", "::git-push"):
        assert directive not in output, f"Raw Git directive in output: {directive}"
    # No secret-shaped strings (redaction defense).
    from autoscout.secret_redaction import contains_secret

    assert not contains_secret(output), f"Secret pattern found in output: {output[:200]}"


def _assert_rendered_output_contains_no_paths(output: str) -> None:
    """Assert neither rendered output contains an absolute filesystem path."""
    for line in output.splitlines():
        if line.startswith("/") and any(c.isalpha() for c in line[1:4] if len(line) > 4):
            raise AssertionError(f"Path-like line in output: {line}")


# ── Tests ─────────────────────────────────────────────────────────────────


class DiagnosisClassEnumTests(unittest.TestCase):
    def test_all_expected_classes_present(self):
        expected = {
            "tool-incompatible",
            "resumable-operation",
            "source-failure",
            "artifact-failure",
            "repository-blocker",
            "persistence-failure",
            "completed-review-refresh",
            "run-in-progress",
            # #1076: a State Store database that disagrees with its export.
            "state-store-finding",
            "healthy",
        }
        actual = {member.value for member in DiagnosisClass}
        self.assertEqual(actual, expected)

    def test_values_are_stable_strings(self):
        for member in DiagnosisClass:
            self.assertIsInstance(member.value, str)
            self.assertEqual(member.value, member.value.lower())


class ExitCodeMappingTests(unittest.TestCase):
    def test_healthy_class_returns_exit_healthy(self):
        report = DoctorReport(diagnosis=DiagnosisClass.HEALTHY, verdict="ok")
        self.assertEqual(exit_code_for(report), EXIT_HEALTHY)

    def test_resumable_returns_exit_recoverable(self):
        report = DoctorReport(diagnosis=DiagnosisClass.RESUMABLE_OPERATION, verdict="resume")
        self.assertEqual(exit_code_for(report), EXIT_RECOVERABLE)

    def test_source_failure_returns_exit_recoverable(self):
        report = DoctorReport(diagnosis=DiagnosisClass.SOURCE_FAILURE, verdict="degraded")
        self.assertEqual(exit_code_for(report), EXIT_RECOVERABLE)

    def test_persistence_failure_returns_exit_recoverable(self):
        report = DoctorReport(diagnosis=DiagnosisClass.PERSISTENCE_FAILURE, verdict="save failed")
        self.assertEqual(exit_code_for(report), EXIT_RECOVERABLE)

    def test_review_refresh_returns_exit_recoverable(self):
        report = DoctorReport(diagnosis=DiagnosisClass.COMPLETED_REVIEW_REFRESH, verdict="refresh")
        self.assertEqual(exit_code_for(report), EXIT_RECOVERABLE)

    def test_repo_blocker_returns_exit_blocked(self):
        report = DoctorReport(diagnosis=DiagnosisClass.REPOSITORY_BLOCKER, verdict="blocked")
        self.assertEqual(exit_code_for(report), EXIT_BLOCKED)

    def test_tool_incompatible_returns_exit_blocked(self):
        report = DoctorReport(diagnosis=DiagnosisClass.TOOL_INCOMPATIBLE, verdict="no codex")
        self.assertEqual(exit_code_for(report), EXIT_BLOCKED)

    def test_artifact_failure_returns_exit_blocked(self):
        report = DoctorReport(diagnosis=DiagnosisClass.ARTIFACT_FAILURE, verdict="bad artifact")
        self.assertEqual(exit_code_for(report), EXIT_BLOCKED)

    def test_run_in_progress_returns_exit_healthy(self):
        report = DoctorReport(diagnosis=DiagnosisClass.RUN_IN_PROGRESS, verdict="running")
        self.assertEqual(exit_code_for(report), EXIT_HEALTHY)


class NoRunEvidenceTests(unittest.TestCase):
    def test_missing_output_dir_is_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = tmp_path / "nonexistent"

            report = diagnose(config, output_dir)
            self.assertEqual(report.diagnosis, DiagnosisClass.HEALTHY)
            self.assertFalse(report.facts.get("has_run_evidence", True))
            self.assertFalse(report.facts.get("output_dir_exists", True))
            self.assertIn("поиск", report.verdict.lower())

    def test_empty_output_dir_is_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            report = diagnose(config, output_dir)
            self.assertEqual(report.diagnosis, DiagnosisClass.HEALTHY)
            self.assertFalse(report.facts.get("has_run_evidence", True))
            self.assertIn("поиск", report.verdict.lower())


class RepositoryBlockerTests(unittest.TestCase):
    def test_repo_status_unsafe_yields_blocker(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            seams = DoctorSeams(
                today=lambda: date(2026, 1, 1),
                repo_inspect=_fake_repo_inspect(
                    {"status": "unsafe", "reasons": ["detached_head"], "branch": "HEAD", "detached_head": True}
                ),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )

            report = diagnose(config, output_dir, seams=seams)
            self.assertEqual(report.diagnosis, DiagnosisClass.REPOSITORY_BLOCKER)
            self.assertIn("заблокирован", report.verdict.lower())
            # Technical reason tokens must NOT be in the verdict.
            self.assertNotIn("detached_head", report.verdict)
            self.assertNotIn("unexpected_branch", report.verdict)
            # They must be in facts.
            self.assertIn("detached_head", report.facts.get("reasons", []))

    def test_live_lock_pid_alive_is_run_in_progress(self):
        """A live lock is a running operation, not a blocker (issue #706).

        Product-rule change, authorized by #706: the old rule reported a
        live lock as ``repository-blocker`` with an escalation question,
        which scared the editor during normal long runs. The live stage now
        comes from the progress snapshot with no escalation question.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            seams = DoctorSeams(
                today=lambda: date(2026, 1, 1),
                repo_inspect=_fake_repo_inspect({"status": "unsafe", "reasons": ["guard_held"], "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": True, "guard_pid_alive": True, "guard_pid": "99999"},
            )

            report = diagnose(config, output_dir, seams=seams)
            self.assertEqual(report.diagnosis, DiagnosisClass.RUN_IN_PROGRESS)
            self.assertIn("выполняется", report.verdict.lower())
            self.assertEqual(report.editor_question, "")
            self.assertNotIn("Димой", report.verdict)

    def test_live_lock_with_clean_git_is_run_in_progress(self):
        """A live lock must report running regardless of Git cleanliness.

        ``inspect_local`` never reads the lockfile, so the lock-ownership
        check still runs before the clean-repository shortcut — but since
        issue #706 it yields ``run-in-progress`` (with the live snapshot)
        instead of ``repository-blocker``.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            seams = DoctorSeams(
                today=lambda: date(2026, 1, 1),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": True, "guard_pid_alive": True, "guard_pid": "99999"},
            )

            report = diagnose(config, output_dir, seams=seams)
            self.assertEqual(report.diagnosis, DiagnosisClass.RUN_IN_PROGRESS)
            self.assertIn("выполняется", report.verdict.lower())
            self.assertEqual(report.editor_question, "")

    def test_ambiguous_lock_blocks_even_when_git_is_clean(self):
        """A lock of undeterminable ownership is unsafe ambiguity, so it blocks."""
        for guard in (
            {"guard_exists": True, "guard_error": "unreadable"},
            {"guard_exists": True},  # no liveness verdict at all
        ):
            with self.subTest(guard=guard), tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                config = tmp_path / "config.json"
                config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
                output_dir = _make_output_dir(tmp_path / "output")

                seams = DoctorSeams(
                    today=lambda: date(2026, 1, 1),
                    repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                    codex_probe=_fake_codex_probe_ok,
                    guard_status=lambda g=guard: g,
                )

                report = diagnose(config, output_dir, seams=seams)
                self.assertEqual(report.diagnosis, DiagnosisClass.REPOSITORY_BLOCKER)
                self.assertTrue(report.recommended_action)
                self.assertTrue(report.editor_question.endswith("?"))

    def test_dead_lock_with_clean_git_is_not_a_blocker(self):
        """A provably dead lock must not block — that is #318's repair, not Level 3."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            seams = DoctorSeams(
                today=lambda: date(2026, 1, 1),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": True, "guard_pid_alive": False, "guard_pid": "1"},
            )

            report = diagnose(config, output_dir, seams=seams)
            self.assertNotEqual(report.diagnosis, DiagnosisClass.REPOSITORY_BLOCKER)

    def test_repo_blocker_verdict_never_contains_technical_tokens(self):
        """The editor-facing verdict must be plain Russian, no reason tokens."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            # Multiple reasons — verdict must never embed them.
            seams = DoctorSeams(
                today=lambda: date(2026, 1, 1),
                repo_inspect=_fake_repo_inspect(
                    {
                        "status": "unsafe",
                        "reasons": ["unexpected_branch", "no_upstream_tracking", "dirty_working_tree"],
                        "branch": "feature",
                    }
                ),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )

            report = diagnose(config, output_dir, seams=seams)
            self.assertEqual(report.diagnosis, DiagnosisClass.REPOSITORY_BLOCKER)
            # Verdict must be plain Russian.
            self.assertNotIn("unexpected_branch", report.verdict)
            self.assertNotIn("no_upstream_tracking", report.verdict)
            self.assertNotIn("dirty_working_tree", report.verdict)
            self.assertNotIn("(", report.verdict)
            # Facts must have the tokens.
            self.assertIn("unexpected_branch", report.facts.get("reasons", []))


class ToolIncompatibleTests(unittest.TestCase):
    def test_codex_missing_with_resumable_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            _write_workflow_log(
                output_dir,
                [
                    {"stage": "capture", "status": "ok", "at": "2026-01-01T00:00:00Z"},
                    {"stage": "codex", "status": "failed", "at": "2026-01-01T00:01:00Z"},
                    {"stage": "terminal", "status": "intervention-required", "at": "2026-01-01T00:02:00Z"},
                ],
            )

            seams = DoctorSeams(
                today=lambda: date(2026, 1, 1),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_missing,
                guard_status=lambda: {"guard_exists": False},
            )

            report = diagnose(config, output_dir, seams=seams)
            self.assertEqual(report.diagnosis, DiagnosisClass.TOOL_INCOMPATIBLE)
            self.assertEqual(exit_code_for(report), EXIT_BLOCKED)

    def test_incompatible_codex_is_distinguished_from_missing_codex(self):
        """An installed-but-too-old Codex is a distinct diagnosis with its own action.

        #317 requires an outdated/incompatible installation to be identified,
        not merely a missing one, and the two need different single actions.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            _write_workflow_log(
                output_dir,
                [
                    {"stage": "capture", "status": "ok", "at": "2026-01-01T00:00:00Z"},
                    {"stage": "codex", "status": "failed", "at": "2026-01-01T00:01:00Z"},
                    {"stage": "terminal", "status": "intervention-required", "at": "2026-01-01T00:02:00Z"},
                ],
            )

            seams = DoctorSeams(
                today=lambda: date(2026, 1, 1),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                codex_compat_probe=lambda: {
                    "codex_compatible": False,
                    "codex_missing_flags": ["--output-last-message"],
                },
                guard_status=lambda: {"guard_exists": False},
            )

            report = diagnose(config, output_dir, seams=seams)
            self.assertEqual(report.diagnosis, DiagnosisClass.TOOL_INCOMPATIBLE)
            self.assertEqual(exit_code_for(report), EXIT_BLOCKED)
            # Editor-facing text names the version problem, not absence.
            self.assertIn("версия", report.verdict.lower())
            self.assertIn("обновите", report.recommended_action.lower())
            # Structured evidence records which flags are unsupported.
            self.assertEqual(report.facts.get("codex_missing_flags"), ["--output-last-message"])
            self.assertEqual(report.facts.get("error"), "codex_incompatible_version")

    def test_compatible_codex_does_not_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            _write_workflow_log(
                output_dir,
                [
                    {"stage": "capture", "status": "ok", "at": "2026-01-01T00:00:00Z"},
                    {"stage": "codex", "status": "failed", "at": "2026-01-01T00:01:00Z"},
                    {"stage": "terminal", "status": "intervention-required", "at": "2026-01-01T00:02:00Z"},
                ],
            )

            seams = DoctorSeams(
                today=lambda: date(2026, 1, 1),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                codex_compat_probe=lambda: {"codex_compatible": True},
                guard_status=lambda: {"guard_exists": False},
            )

            report = diagnose(config, output_dir, seams=seams)
            self.assertNotEqual(report.diagnosis, DiagnosisClass.TOOL_INCOMPATIBLE)

    def test_tool_incompatible_distinct_from_other_classes(self):
        """Tool incompatibility must not be reported as source/artifact/repo/persistence failure."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            _write_workflow_log(
                output_dir,
                [
                    {"stage": "capture", "status": "ok", "at": "2026-01-01T00:00:00Z"},
                    {"stage": "terminal", "status": "intervention-required", "at": "2026-01-01T00:02:00Z"},
                ],
            )

            seams = DoctorSeams(
                today=lambda: date(2026, 1, 1),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_missing,
                guard_status=lambda: {"guard_exists": False},
            )

            report = diagnose(config, output_dir, seams=seams)
            self.assertEqual(report.diagnosis, DiagnosisClass.TOOL_INCOMPATIBLE)
            self.assertNotEqual(report.diagnosis, DiagnosisClass.SOURCE_FAILURE)
            self.assertNotEqual(report.diagnosis, DiagnosisClass.ARTIFACT_FAILURE)
            self.assertNotEqual(report.diagnosis, DiagnosisClass.REPOSITORY_BLOCKER)
            self.assertNotEqual(report.diagnosis, DiagnosisClass.PERSISTENCE_FAILURE)

    def test_codex_probe_sanitized_no_absolute_paths(self):
        """_codex_probe must never include absolute paths in its result dict."""
        # Test with a real probe — the result dict must never contain path-like values.
        result = _codex_probe(timeout_seconds=2.0)
        for key, value in result.items():
            if isinstance(value, str):
                self.assertFalse(
                    value.startswith("/"),
                    f"_codex_probe result key '{key}' contains absolute path: {value}",
                )
        # Check the key names are sanitized (not "executable").
        self.assertNotIn("executable", result)

    def test_tool_incompatible_facts_no_absolute_paths(self):
        """Tool-incompatible report facts must never leak absolute paths."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            _write_workflow_log(
                output_dir,
                [
                    {"stage": "capture", "status": "ok", "at": "2026-01-01T00:00:00Z"},
                    {"stage": "terminal", "status": "intervention-required", "at": "2026-01-01T00:02:00Z"},
                ],
            )

            seams = DoctorSeams(
                today=lambda: date(2026, 1, 1),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=lambda: {"codex_available": False, "error": "codex_os_error"},
                guard_status=lambda: {"guard_exists": False},
            )

            report = diagnose(config, output_dir, seams=seams)
            self.assertEqual(report.diagnosis, DiagnosisClass.TOOL_INCOMPATIBLE)
            russian = render_russian(report)
            json_out = render_json_report(report)
            _assert_rendered_output_contains_no_paths(russian)
            _assert_rendered_output_contains_no_paths(json_out)


class ResumableOperationTests(unittest.TestCase):
    def test_resumable_agrees_with_capture_is_current(self):
        """When _capture_is_current returns True, doctor reports resumable-operation."""
        from tests.test_issue_315_retry_blocked import _make_capture_fixture
        from tests.test_issue_315_retry_blocked import _make_config
        from tests.test_issue_315_retry_blocked import _simulate_failed_operation

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_bare_git_repo(tmp_path)
            config_path = _make_config(tmp_path)
            _make_capture_fixture(tmp_path)
            output_dir = tmp_path / "run"
            output_dir.mkdir()
            _simulate_failed_operation(output_dir, config_path, tmp_path)

            self.assertTrue(
                _capture_is_current(output_dir, config_path, date(2026, 7, 13)),
                "Shared #315 fixture should be recognized as current by _capture_is_current",
            )

            seams = DoctorSeams(
                today=lambda: date(2026, 7, 13),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )

            report = diagnose(config_path, output_dir, seams=seams)
            self.assertEqual(report.diagnosis, DiagnosisClass.RESUMABLE_OPERATION)
            self.assertEqual(exit_code_for(report), EXIT_RECOVERABLE)
            self.assertIn("прерванный поиск", report.verdict.lower())

    def test_capture_is_current_false_not_classified_as_resumable(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            seams = DoctorSeams(
                today=lambda: date(2026, 1, 1),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )

            report = diagnose(config, output_dir, seams=seams)
            self.assertNotEqual(report.diagnosis, DiagnosisClass.RESUMABLE_OPERATION)

    def _current_operation_fixture(self, tmp_path: Path):
        """Real #315 fixture: a genuinely current operation with a resume marker.

        ``_capture_is_current`` means "same-operation evidence is still valid",
        not "the operation is unfinished", and the resume marker written at
        capture completion is never removed on success — so it stays valid for
        the rest of the operation day.
        """
        from tests.test_issue_315_retry_blocked import _make_capture_fixture
        from tests.test_issue_315_retry_blocked import _make_config
        from tests.test_issue_315_retry_blocked import _simulate_failed_operation

        _init_bare_git_repo(tmp_path)
        config_path = _make_config(tmp_path)
        _make_capture_fixture(tmp_path)
        output_dir = tmp_path / "run"
        output_dir.mkdir()
        _simulate_failed_operation(output_dir, config_path, tmp_path)
        self.assertTrue(_capture_is_current(output_dir, config_path, date(2026, 7, 13)))
        return config_path, output_dir

    def _seams(self):
        return DoctorSeams(
            today=lambda: date(2026, 7, 13),
            repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
            codex_probe=_fake_codex_probe_ok,
            codex_compat_probe=lambda: {"codex_compatible": True},
            guard_status=lambda: {"guard_exists": False},
        )

    def test_successful_same_day_run_is_not_resumable(self):
        """A completed run must never be reported as an interrupted one.

        Regression: precedence (c) keyed on ``is_current`` alone, so for the
        whole same-day window after a *successful* run the editor was told a
        search had been interrupted — and confirming recovery would spend
        network and model budget and publish again.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path, output_dir = self._current_operation_fixture(tmp_path)

            # Same fixture artifacts, but a fully successful completed log.
            _write_workflow_log(
                output_dir,
                [
                    {"stage": "capture", "status": "ok", "at": "2026-07-13T00:00:00Z"},
                    {"stage": "codex", "status": "ok", "at": "2026-07-13T00:01:00Z"},
                    {"stage": "repository-finalize", "status": "ok", "at": "2026-07-13T00:10:00Z"},
                    {"stage": "terminal", "status": "completed/no-attention", "at": "2026-07-13T00:11:00Z"},
                ],
            )
            (output_dir / "weekly-review.html").write_text("<html>review</html>", encoding="utf-8")
            _write_artifact(output_dir, "pilot-status.json", {"ready_for_editor": True, "run_date": "2026-07-13"})
            _write_artifact(output_dir, "run-summary.json", {"editor_facing_count": 5, "run_date": "2026-07-13"})

            report = diagnose(config_path, output_dir, seams=self._seams())

            self.assertNotEqual(report.diagnosis, DiagnosisClass.RESUMABLE_OPERATION)
            self.assertNotIn("прерванный", report.verdict.lower())
            # And no consequential recovery is planned for it.
            from autoscout.doctor_recovery import RecoveryAction
            from autoscout.doctor_recovery import plan_recovery

            plan = plan_recovery(report)
            self.assertNotEqual(plan.action, RecoveryAction.RESUME_WEEKLY)
            self.assertEqual(plan.side_effects, [])

    def test_healthy_is_reachable_during_the_same_day_window(self):
        """HEALTHY must not be unreachable while the resume marker is valid."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path, output_dir = self._current_operation_fixture(tmp_path)

            _write_workflow_log(
                output_dir,
                [
                    {"stage": "capture", "status": "ok", "at": "2026-07-13T00:00:00Z"},
                    {"stage": "repository-finalize", "status": "ok", "at": "2026-07-13T00:10:00Z"},
                    {"stage": "terminal", "status": "completed/no-attention", "at": "2026-07-13T00:11:00Z"},
                ],
            )
            (output_dir / "weekly-review.html").write_text("<html>review</html>", encoding="utf-8")
            _write_artifact(output_dir, "pilot-status.json", {"ready_for_editor": True, "run_date": "2026-07-13"})
            _write_artifact(output_dir, "run-summary.json", {"editor_facing_count": 5, "run_date": "2026-07-13"})
            # All sources succeeded, so nothing degrades the verdict.
            _write_artifact(
                output_dir,
                "source-runs.json",
                [{"source_name": "Test Source", "status": "codex-captured"}],
            )

            report = diagnose(config_path, output_dir, seams=self._seams())
            self.assertEqual(report.diagnosis, DiagnosisClass.HEALTHY)
            self.assertEqual(exit_code_for(report), EXIT_HEALTHY)

    def test_interrupted_current_operation_is_still_resumable(self):
        """The genuine resume path must keep working — no completed terminal."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path, output_dir = self._current_operation_fixture(tmp_path)

            report = diagnose(config_path, output_dir, seams=self._seams())
            self.assertEqual(report.diagnosis, DiagnosisClass.RESUMABLE_OPERATION)
            self.assertEqual(exit_code_for(report), EXIT_RECOVERABLE)

    def test_current_capacity_stop_is_resumable_not_source_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path, output_dir = self._current_operation_fixture(tmp_path)
            _write_workflow_log(
                output_dir,
                [
                    {"stage": "capture", "status": "ok", "at": "2026-07-13T00:00:00Z"},
                    {
                        "stage": "codex",
                        "status": "unfinished",
                        "reason": "portfolio capacity stop: 2 extraction item(s) unfinished",
                        "at": "2026-07-13T00:01:00Z",
                    },
                    {
                        "stage": "terminal",
                        "status": "intervention-required",
                        "at": "2026-07-13T00:01:00Z",
                    },
                ],
            )

            report = diagnose(config_path, output_dir, seams=self._seams())
            self.assertEqual(report.diagnosis, DiagnosisClass.RESUMABLE_OPERATION)
            self.assertNotIn("сбой источника", report.verdict.lower())
            self.assertNotIn("source failure", report.verdict.lower())

    def test_same_day_persistence_failure_reports_current_operation(self):
        """The persistence-recovery path must be reachable, not dead code.

        A same-day push failure after a completed terminal is the real
        persistence-failure shape. It must carry ``is_current_operation:
        True`` so recovery may re-finalize without recapture — otherwise the
        documented "still current → resume" row is unreachable and the
        identity guard is vacuous.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path, output_dir = self._current_operation_fixture(tmp_path)

            # Shape the workflow actually persists: _bad_outcome strips old
            # terminals and always writes exactly one terminal last, so the
            # failed finalize precedes it.
            _write_workflow_log(
                output_dir,
                [
                    {"stage": "capture", "status": "ok", "at": "2026-07-13T00:00:00Z"},
                    {"stage": "codex", "status": "ok", "at": "2026-07-13T00:01:00Z"},
                    {"stage": "outcome-candidate", "status": "completed/no-attention", "at": "2026-07-13T00:10:00Z"},
                    {"stage": "repository-finalize", "status": "push_failed", "at": "2026-07-13T00:11:00Z"},
                    {"stage": "terminal", "status": "completed/no-attention", "at": "2026-07-13T00:12:00Z"},
                ],
            )
            (output_dir / "weekly-review.html").write_text("<html>review</html>", encoding="utf-8")

            report = diagnose(config_path, output_dir, seams=self._seams())
            self.assertEqual(report.diagnosis, DiagnosisClass.PERSISTENCE_FAILURE)
            self.assertTrue(report.facts.get("is_current_operation"))
            # The retry is offerable, so the diagnosis offers it.
            self.assertIn("повторите сохранение", report.recommended_action.lower())

            from autoscout.doctor_recovery import RecoveryAction
            from autoscout.doctor_recovery import plan_recovery

            self.assertEqual(plan_recovery(report).action, RecoveryAction.RESUME_WEEKLY)


class RetryInheritedFinalizeTests(unittest.TestCase):
    """A published retry must not be reported as an unsaved one.

    A validated same-operation retry deliberately carries the previous
    attempt's log entries forward (#315 prior-evidence preservation), so a
    successful retry's log holds the old failed ``repository-finalize`` beside
    the new successful one. They are one operation's entries by construction,
    so the operation slicer cannot separate them — only the latest attempt
    describes the repository's current state.
    """

    def _diagnose(self, log_entries):
        from tests.test_issue_315_retry_blocked import _make_capture_fixture
        from tests.test_issue_315_retry_blocked import _make_config
        from tests.test_issue_315_retry_blocked import _simulate_failed_operation

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_path = Path(tmp.name)
        _init_bare_git_repo(tmp_path)
        config_path = _make_config(tmp_path)
        _make_capture_fixture(tmp_path)
        output_dir = tmp_path / "run"
        output_dir.mkdir()
        _simulate_failed_operation(output_dir, config_path, tmp_path)

        _write_workflow_log(output_dir, log_entries)
        _write_artifact(output_dir, "pilot-status.json", {"ready_for_editor": True, "run_date": "2026-07-13"})
        _write_artifact(output_dir, "run-summary.json", {"editor_facing_count": 5, "run_date": "2026-07-13"})
        _write_artifact(output_dir, "source-runs.json", [{"source_name": "Test Source", "status": "codex-captured"}])
        (output_dir / "weekly-review.html").write_text("<html>review</html>", encoding="utf-8")

        return diagnose(
            config_path,
            output_dir,
            seams=DoctorSeams(
                today=lambda: date(2026, 7, 13),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                codex_compat_probe=lambda: {"codex_compatible": True},
                guard_status=lambda: {"guard_exists": False},
            ),
        )

    def test_successful_retry_reports_results_saved(self):
        report = self._diagnose(
            [
                {"stage": "capture", "status": "ok", "at": "2026-07-13T00:00:00Z"},
                # Carried forward from the failed attempt.
                {"stage": "repository-finalize", "status": "failed", "at": "2026-07-13T00:10:00Z"},
                # This attempt.
                {"stage": "codex", "status": "ok", "at": "2026-07-13T01:00:00Z"},
                {"stage": "repository-finalize", "status": "ok", "at": "2026-07-13T01:10:00Z"},
                {"stage": "terminal", "status": "completed/no-attention", "at": "2026-07-13T01:11:00Z"},
            ]
        )

        self.assertTrue(report.results_saved)
        self.assertNotEqual(report.diagnosis, DiagnosisClass.PERSISTENCE_FAILURE)
        self.assertNotIn("не удалось сохранить", report.verdict)

        from autoscout.doctor_recovery import RecoveryAction
        from autoscout.doctor_recovery import plan_recovery

        plan = plan_recovery(report)
        self.assertNotEqual(plan.action, RecoveryAction.RESUME_WEEKLY)
        self.assertEqual(plan.side_effects, [])

    def test_latest_failed_finalize_still_reports_unsaved(self):
        """The evidence rule must not flip the other way."""
        report = self._diagnose(
            [
                {"stage": "capture", "status": "ok", "at": "2026-07-13T00:00:00Z"},
                {"stage": "repository-finalize", "status": "ok", "at": "2026-07-13T00:10:00Z"},
                {"stage": "outcome-candidate", "status": "completed/no-attention", "at": "2026-07-13T01:00:00Z"},
                {"stage": "repository-finalize", "status": "push_failed", "at": "2026-07-13T01:10:00Z"},
                {"stage": "terminal", "status": "completed/no-attention", "at": "2026-07-13T01:11:00Z"},
            ]
        )
        self.assertEqual(report.diagnosis, DiagnosisClass.PERSISTENCE_FAILURE)
        self.assertFalse(report.results_saved)


class StaleTerminalTests(unittest.TestCase):
    """Facts must describe the current operation, not a previous one.

    The weekly output directory is reused every week and ``workflow-log.json``
    is appended to stage by stage, only being rewritten from memory just
    before ``repository-finalize``. So for nearly a whole run the file holds
    the previous operation's entries — including its ``terminal`` and
    ``repository-finalize`` — followed by this operation's partial entries.

    A hard kill in that window (SIGKILL, power loss) leaves exactly that
    state, with no new terminal written.
    """

    def test_legacy_terminal_projection_is_owned_by_evidence(self):
        from autoscout.mvp_run_evidence import evidence_from_log

        log = [
            {"stage": "capture", "status": "ok"},
            {"stage": "outcome-candidate", "status": "completed/no-attention"},
            {"stage": "repository-finalize", "status": "push_failed"},
            {"stage": "terminal", "status": "completed/no-attention"},
        ]
        evidence = evidence_from_log(log, "legacy-operation", allow_legacy=True)
        self.assertEqual(evidence.log, tuple(log))
        self.assertTrue(evidence.has_completed_outcome)
        self.assertTrue(evidence.persistence_failed)

    def test_legacy_projection_drops_a_previous_completed_operation(self):
        from autoscout.mvp_run_evidence import evidence_from_log

        log = [
            {"stage": "capture", "status": "ok", "at": "week1"},
            {"stage": "repository-finalize", "status": "ok", "at": "week1"},
            {"stage": "terminal", "status": "completed/no-attention", "at": "week1"},
            {"stage": "capture", "status": "ok", "at": "week2"},
            {"stage": "codex", "status": "ok", "at": "week2"},
        ]
        evidence = evidence_from_log(log, "legacy-operation", allow_legacy=True)
        self.assertEqual([entry["at"] for entry in evidence.log], ["week2", "week2"])

    def test_legacy_projection_keeps_the_last_completed_operation(self):
        from autoscout.mvp_run_evidence import evidence_from_log

        log = [
            {"stage": "capture", "status": "ok", "at": "week1"},
            {"stage": "terminal", "status": "completed/no-attention", "at": "week1"},
            {"stage": "capture", "status": "ok", "at": "week2"},
            {"stage": "terminal", "status": "completed/attention-required", "at": "week2"},
        ]
        evidence = evidence_from_log(log, "legacy-operation", allow_legacy=True)
        self.assertEqual([entry["at"] for entry in evidence.log], ["week2", "week2"])

    def test_two_consecutive_kills_still_diagnose_correctly(self):
        from autoscout.mvp_run_evidence import evidence_from_log

        log = [
            {"stage": "capture", "status": "ok", "at": "week1"},
            {"stage": "codex", "status": "failed", "at": "week1"},
            {"stage": "capture", "status": "ok", "at": "week2"},
            {"stage": "pipeline", "status": "failed", "at": "week2"},
        ]
        evidence = evidence_from_log(log, "legacy-operation", allow_legacy=True)
        self.assertEqual(evidence.log, tuple(log))
        self.assertEqual(evidence.failed_stage, "pipeline")
        self.assertFalse(evidence.has_completed_outcome)

    def test_killed_run_is_not_described_by_last_weeks_outcome(self):
        """A run killed mid-extraction must not inherit last week's completion.

        Regression: it was reported as a completed, *saved* run needing only a
        review refresh — «Результаты поиска сохранены» — because both the
        terminal and the finalize status came from the previous operation.
        """
        from tests.test_issue_315_retry_blocked import _make_capture_fixture
        from tests.test_issue_315_retry_blocked import _make_config
        from tests.test_issue_315_retry_blocked import _simulate_failed_operation

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_bare_git_repo(tmp_path)
            config_path = _make_config(tmp_path)
            _make_capture_fixture(tmp_path)
            output_dir = tmp_path / "run"
            output_dir.mkdir()
            _simulate_failed_operation(output_dir, config_path, tmp_path)
            self.assertTrue(_capture_is_current(output_dir, config_path, date(2026, 7, 13)))

            _write_workflow_log(
                output_dir,
                [
                    # Last week, completed and published.
                    {"stage": "capture", "status": "ok", "at": "2026-07-06T00:00:00Z"},
                    {"stage": "repository-finalize", "status": "ok", "at": "2026-07-06T00:10:00Z"},
                    {"stage": "terminal", "status": "completed/no-attention", "at": "2026-07-06T00:11:00Z"},
                    # This week, killed mid-extraction — no new terminal.
                    {"stage": "capture", "status": "ok", "at": "2026-07-13T00:00:00Z"},
                    {"stage": "codex", "status": "ok", "at": "2026-07-13T00:01:00Z"},
                ],
            )
            _write_artifact(output_dir, "pilot-status.json", {"ready_for_editor": True, "run_date": "2026-07-13"})
            _write_artifact(output_dir, "run-summary.json", {"editor_facing_count": 5, "run_date": "2026-07-13"})

            seams = DoctorSeams(
                today=lambda: date(2026, 7, 13),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                codex_compat_probe=lambda: {"codex_compatible": True},
                guard_status=lambda: {"guard_exists": False},
            )
            report = diagnose(config_path, output_dir, seams=seams)

            # The interrupted operation is what gets diagnosed.
            self.assertEqual(report.diagnosis, DiagnosisClass.RESUMABLE_OPERATION)
            self.assertNotIn("завершён", report.verdict.lower())
            self.assertFalse(report.results_saved)
            # Facts describe this operation only — no week-1 stages leak.
            self.assertNotIn("repository-finalize", report.facts.get("log_stages", []))

    def test_prior_evidence_is_still_preserved_on_disk(self):
        """Slicing is a read-time concern — it must never remove evidence."""
        from autoscout.json_io import read_json

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            entries = [
                {"stage": "capture", "status": "ok", "at": "week1"},
                {"stage": "terminal", "status": "completed/no-attention", "at": "week1"},
                {"stage": "capture", "status": "ok", "at": "week2"},
                {"stage": "codex", "status": "failed", "at": "week2"},
            ]
            _write_workflow_log(output_dir, entries)

            diagnose(
                config,
                output_dir,
                seams=DoctorSeams(
                    today=lambda: date(2026, 1, 1),
                    repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                    codex_probe=_fake_codex_probe_ok,
                    guard_status=lambda: {"guard_exists": False},
                ),
            )

            self.assertEqual(read_json(output_dir / "workflow-log.json", default=[]), entries)


class SourceFailureTests(unittest.TestCase):
    def test_completed_run_with_failed_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            _write_workflow_log(
                output_dir,
                [
                    {"stage": "capture", "status": "ok", "at": "2026-01-01T00:00:00Z"},
                    {"stage": "repository-finalize", "status": "ok", "at": "2026-01-01T00:10:00Z"},
                    {"stage": "terminal", "status": "completed/attention-required", "at": "2026-01-01T00:11:00Z"},
                ],
            )

            source_runs = [
                {"source_name": "Broken Source", "status": "failed", "reason": "timeout"},
                {"source_name": "Good Source", "status": "codex-captured"},
            ]
            _write_artifact(output_dir, "source-runs.json", source_runs)

            _write_artifact(output_dir, "pilot-status.json", {"ready_for_editor": True, "run_date": "2026-01-01"})
            _write_artifact(output_dir, "run-summary.json", {"editor_facing_count": 5, "run_date": "2026-01-01"})
            (output_dir / "weekly-review.html").write_text("<html>review</html>", encoding="utf-8")

            seams = DoctorSeams(
                today=lambda: date(2026, 1, 1),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )

            report = diagnose(config, output_dir, seams=seams)
            self.assertEqual(report.diagnosis, DiagnosisClass.SOURCE_FAILURE)
            self.assertEqual(exit_code_for(report), EXIT_RECOVERABLE)
            self.assertIn("Broken Source", report.verdict)
            self.assertEqual(report.facts.get("failed_count"), 1)
            # finalize succeeded here, so persistence may be claimed.
            self.assertTrue(report.results_saved)

    def test_source_failure_does_not_claim_saved_without_evidence(self):
        """``results_saved`` must come from finalize evidence, never be assumed.

        A degraded-but-completed run with no repository-finalize entry has no
        proof of publication.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            _write_workflow_log(
                output_dir,
                [
                    {"stage": "capture", "status": "ok", "at": "2026-01-01T00:00:00Z"},
                    {"stage": "terminal", "status": "completed/attention-required", "at": "2026-01-01T00:11:00Z"},
                ],
            )
            _write_artifact(
                output_dir,
                "source-runs.json",
                [
                    {"source_name": "Broken Source", "status": "failed", "reason": "timeout"},
                    {"source_name": "Good Source", "status": "codex-captured"},
                ],
            )
            _write_artifact(output_dir, "pilot-status.json", {"ready_for_editor": True, "run_date": "2026-01-01"})
            _write_artifact(output_dir, "run-summary.json", {"editor_facing_count": 5, "run_date": "2026-01-01"})
            (output_dir / "weekly-review.html").write_text("<html>review</html>", encoding="utf-8")

            seams = DoctorSeams(
                today=lambda: date(2026, 1, 1),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )

            report = diagnose(config, output_dir, seams=seams)
            self.assertEqual(report.diagnosis, DiagnosisClass.SOURCE_FAILURE)
            self.assertFalse(report.results_saved)

    def test_completed_run_with_skipped_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            _write_workflow_log(
                output_dir,
                [
                    {"stage": "capture", "status": "ok", "at": "2026-01-01T00:00:00Z"},
                    {"stage": "repository-finalize", "status": "ok", "at": "2026-01-01T00:10:00Z"},
                    {"stage": "terminal", "status": "completed/attention-required", "at": "2026-01-01T00:11:00Z"},
                ],
            )

            source_runs = [
                {"source_name": "Skipped Source", "status": "skipped", "reason": "paid"},
                {"source_name": "Good Source", "status": "codex-captured"},
            ]
            _write_artifact(output_dir, "source-runs.json", source_runs)
            _write_artifact(output_dir, "pilot-status.json", {"ready_for_editor": True, "run_date": "2026-01-01"})
            _write_artifact(output_dir, "run-summary.json", {"editor_facing_count": 5, "run_date": "2026-01-01"})
            (output_dir / "weekly-review.html").write_text("<html>review</html>", encoding="utf-8")

            seams = DoctorSeams(
                today=lambda: date(2026, 1, 1),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )

            report = diagnose(config, output_dir, seams=seams)
            self.assertEqual(report.diagnosis, DiagnosisClass.SOURCE_FAILURE)
            self.assertIn("Skipped Source", report.verdict)


class PersistenceFailureTests(unittest.TestCase):
    def test_completed_outcome_with_failed_finalize(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            _write_workflow_log(
                output_dir,
                [
                    {"stage": "capture", "status": "ok", "at": "2026-01-01T00:00:00Z"},
                    {"stage": "pipeline", "status": "ok", "at": "2026-01-01T00:05:00Z"},
                    {"stage": "outcome-candidate", "status": "completed/no-attention", "at": "2026-01-01T00:06:00Z"},
                    {"stage": "repository-finalize", "status": "push_failed", "at": "2026-01-01T00:07:00Z"},
                    {"stage": "terminal", "status": "completed/no-attention", "at": "2026-01-01T00:08:00Z"},
                ],
            )

            _write_artifact(output_dir, "pilot-status.json", {"ready_for_editor": True, "run_date": "2026-01-01"})
            _write_artifact(output_dir, "run-summary.json", {"editor_facing_count": 10, "run_date": "2026-01-01"})
            _write_artifact(output_dir, "source-runs.json", [{"source_name": "S1", "status": "codex-captured"}])

            seams = DoctorSeams(
                today=lambda: date(2026, 1, 1),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )

            report = diagnose(config, output_dir, seams=seams)
            self.assertEqual(report.diagnosis, DiagnosisClass.PERSISTENCE_FAILURE)
            self.assertEqual(exit_code_for(report), EXIT_RECOVERABLE)
            self.assertFalse(report.results_saved)
            self.assertIn("сохранить", report.verdict.lower())


class CompletedReviewRefreshTests(unittest.TestCase):
    def test_completed_run_missing_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            _write_workflow_log(
                output_dir,
                [
                    {"stage": "capture", "status": "ok", "at": "2026-01-01T00:00:00Z"},
                    {"stage": "repository-finalize", "status": "ok", "at": "2026-01-01T00:10:00Z"},
                    {"stage": "terminal", "status": "completed/no-attention", "at": "2026-01-01T00:11:00Z"},
                ],
            )

            _write_artifact(output_dir, "pilot-status.json", {"ready_for_editor": True, "run_date": "2026-01-01"})
            _write_artifact(output_dir, "run-summary.json", {"editor_facing_count": 10, "run_date": "2026-01-01"})
            _write_artifact(output_dir, "source-runs.json", [{"source_name": "S1", "status": "codex-captured"}])

            seams = DoctorSeams(
                today=lambda: date(2026, 1, 1),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )

            report = diagnose(config, output_dir, seams=seams)
            self.assertEqual(report.diagnosis, DiagnosisClass.COMPLETED_REVIEW_REFRESH)
            self.assertEqual(exit_code_for(report), EXIT_RECOVERABLE)
            self.assertTrue(report.results_saved)
            self.assertFalse(report.facts.get("review_present", True))

    def test_verdict_does_not_claim_saved_without_finalize_evidence(self):
        """Never claim persistence in the verdict when evidence does not show it.

        A completed run with no repository-finalize entries has no structured
        proof of publication, so the verdict must not assert results are saved.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            _write_workflow_log(
                output_dir,
                [
                    {"stage": "capture", "status": "ok", "at": "2026-01-01T00:00:00Z"},
                    {"stage": "terminal", "status": "completed/no-attention", "at": "2026-01-01T00:11:00Z"},
                ],
            )
            _write_artifact(output_dir, "pilot-status.json", {"ready_for_editor": True, "run_date": "2026-01-01"})
            _write_artifact(output_dir, "run-summary.json", {"editor_facing_count": 10, "run_date": "2026-01-01"})
            _write_artifact(output_dir, "source-runs.json", [{"source_name": "S1", "status": "codex-captured"}])

            seams = DoctorSeams(
                today=lambda: date(2026, 1, 1),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )

            report = diagnose(config, output_dir, seams=seams)
            self.assertEqual(report.diagnosis, DiagnosisClass.COMPLETED_REVIEW_REFRESH)
            self.assertFalse(report.results_saved)
            self.assertNotIn("сохранены", report.verdict)

    def test_completed_run_with_corrupt_artifacts_is_artifact_failure(self):
        """Corrupt artifacts are Level 3, not something a review refresh can fix."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            _write_workflow_log(
                output_dir,
                [
                    {"stage": "capture", "status": "ok", "at": "2026-01-01T00:00:00Z"},
                    {"stage": "repository-finalize", "status": "ok", "at": "2026-01-01T00:10:00Z"},
                    {"stage": "terminal", "status": "completed/no-attention", "at": "2026-01-01T00:11:00Z"},
                ],
            )
            # pilot-status.json is structurally wrong (missing ready_for_editor).
            _write_artifact(output_dir, "pilot-status.json", {"run_date": "2026-01-01"})
            _write_artifact(output_dir, "run-summary.json", {"editor_facing_count": 10, "run_date": "2026-01-01"})
            _write_artifact(output_dir, "source-runs.json", [{"source_name": "S1", "status": "codex-captured"}])
            (output_dir / "weekly-review.html").write_text("<html></html>", encoding="utf-8")

            seams = DoctorSeams(
                today=lambda: date(2026, 1, 1),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )

            report = diagnose(config, output_dir, seams=seams)
            self.assertEqual(report.diagnosis, DiagnosisClass.ARTIFACT_FAILURE)
            self.assertEqual(exit_code_for(report), EXIT_BLOCKED)
            self.assertFalse(report.facts.get("artifacts_valid", True))


class ExpectedRemoteTests(unittest.TestCase):
    def test_default_seams_expect_the_canonical_envelope(self):
        """Diagnostics must validate the same branch and remote as the weekly adapter.

        Otherwise a clean ``main`` pointing at the wrong remote is diagnosed as
        safe (#316 expected-repository check).
        """
        from autoscout.git_envelope import CANONICAL_BRANCH
        from autoscout.git_envelope import CANONICAL_REMOTE_URL

        seams = DoctorSeams()
        self.assertEqual(seams.expected_branch, CANONICAL_BRANCH)
        self.assertEqual(seams.expected_remote_url, CANONICAL_REMOTE_URL)

    def test_wrong_remote_is_not_diagnosed_safe(self):
        import subprocess as sp

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo = tmp_path / "repo"
            repo.mkdir()
            sp.run(["git", "-C", str(repo), "init", "--initial-branch=main"], check=True, capture_output=True)
            sp.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True, capture_output=True)
            sp.run(["git", "-C", str(repo), "config", "user.email", "t@t.test"], check=True, capture_output=True)
            sp.run(
                ["git", "-C", str(repo), "remote", "add", "origin", "https://example.test/wrong.git"],
                check=True,
                capture_output=True,
            )
            config = repo / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            sp.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
            sp.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)

            output_dir = _make_output_dir(repo / "output")

            # Default (canonical) expectations — the wrong remote must block.
            report = diagnose(
                config,
                output_dir,
                seams=DoctorSeams(
                    today=lambda: date(2026, 1, 1),
                    codex_probe=_fake_codex_probe_ok,
                    guard_status=lambda: {"guard_exists": False},
                ),
            )
            self.assertEqual(report.diagnosis, DiagnosisClass.REPOSITORY_BLOCKER)


class HealthyCompletedTests(unittest.TestCase):
    def test_fully_completed_healthy_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            _write_workflow_log(
                output_dir,
                [
                    {"stage": "capture", "status": "ok", "at": "2026-01-01T00:00:00Z"},
                    {"stage": "repository-finalize", "status": "ok", "at": "2026-01-01T00:10:00Z"},
                    {"stage": "terminal", "status": "completed/no-attention", "at": "2026-01-01T00:11:00Z"},
                ],
            )

            _write_artifact(output_dir, "pilot-status.json", {"ready_for_editor": True, "run_date": "2026-01-01"})
            _write_artifact(output_dir, "run-summary.json", {"editor_facing_count": 10, "run_date": "2026-01-01"})
            _write_artifact(output_dir, "source-runs.json", [{"source_name": "S1", "status": "codex-captured"}])
            (output_dir / "weekly-review.html").write_text("<html>review</html>", encoding="utf-8")

            seams = DoctorSeams(
                today=lambda: date(2026, 1, 1),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )

            report = diagnose(config, output_dir, seams=seams)
            self.assertEqual(report.diagnosis, DiagnosisClass.HEALTHY)
            self.assertEqual(exit_code_for(report), EXIT_HEALTHY)
            self.assertTrue(report.results_saved)
            self.assertTrue(report.review_available)
            self.assertIn("порядке", report.verdict.lower())


class ArtifactFailureTests(unittest.TestCase):
    def test_secret_scan_flags_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            _write_workflow_log(
                output_dir,
                [
                    {"stage": "capture", "status": "ok", "at": "2026-01-01T00:00:00Z"},
                    {"stage": "finalize", "status": "failed", "at": "2026-01-01T00:01:00Z"},
                ],
            )

            # Embed a secret pattern that matches the credential_assignment rule:
            # token= followed by a 16+ char alphanumeric run.
            _write_artifact(
                output_dir,
                "source-runs.json",
                {"debug_output": "token=skprojABCDEFGHIJKLMNOPQRSTUVWXYZ123456"},
            )

            seams = DoctorSeams(
                today=lambda: date(2026, 1, 1),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )

            report = diagnose(config, output_dir, seams=seams)
            self.assertEqual(report.diagnosis, DiagnosisClass.ARTIFACT_FAILURE)
            self.assertEqual(exit_code_for(report), EXIT_BLOCKED)

    def test_missing_required_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            # Pipeline failure (not capture/codex) with no artifacts → artifact-failure.
            _write_workflow_log(
                output_dir,
                [
                    {"stage": "capture", "status": "ok", "at": "2026-01-01T00:00:00Z"},
                    {"stage": "codex", "status": "ok", "at": "2026-01-01T00:01:00Z"},
                    {"stage": "finalize", "status": "ok", "at": "2026-01-01T00:01:30Z"},
                    {"stage": "pipeline", "status": "failed", "at": "2026-01-01T00:02:00Z"},
                ],
            )

            seams = DoctorSeams(
                today=lambda: date(2026, 1, 1),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )

            report = diagnose(config, output_dir, seams=seams)
            self.assertEqual(report.diagnosis, DiagnosisClass.ARTIFACT_FAILURE)
            self.assertEqual(exit_code_for(report), EXIT_BLOCKED)


# ── Finding 4a: Classification fallthrough for intervention-required ──────


class InterventionRequiredFallthroughTests(unittest.TestCase):
    """4a: intervention-required terminal + NOT resumable + codex ok must
    NOT report as healthy/never-ran."""

    def test_capture_failed_not_resumable_is_source_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            # Capture failed, terminal intervention-required, not resumable.
            _write_workflow_log(
                output_dir,
                [
                    {"stage": "capture", "status": "failed", "at": "2026-01-01T00:00:00Z"},
                    {"stage": "terminal", "status": "intervention-required", "at": "2026-01-01T00:01:00Z"},
                ],
            )

            seams = DoctorSeams(
                today=lambda: date(2026, 1, 1),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )

            report = diagnose(config, output_dir, seams=seams)
            # Must be source-failure (capture failed), not healthy.
            self.assertEqual(report.diagnosis, DiagnosisClass.SOURCE_FAILURE)
            self.assertEqual(exit_code_for(report), EXIT_RECOVERABLE)
            # Must NOT claim "ещё не запускался".
            self.assertNotIn("ещё не запускался", report.verdict)
            self.assertIn("сбой", report.verdict.lower())

    def test_codex_failed_not_resumable_is_source_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            _write_workflow_log(
                output_dir,
                [
                    {"stage": "capture", "status": "ok", "at": "2026-01-01T00:00:00Z"},
                    {"stage": "codex", "status": "failed", "at": "2026-01-01T00:01:00Z"},
                    {"stage": "terminal", "status": "intervention-required", "at": "2026-01-01T00:02:00Z"},
                ],
            )

            seams = DoctorSeams(
                today=lambda: date(2026, 1, 1),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )

            report = diagnose(config, output_dir, seams=seams)
            self.assertEqual(report.diagnosis, DiagnosisClass.SOURCE_FAILURE)
            self.assertEqual(exit_code_for(report), EXIT_RECOVERABLE)
            self.assertNotIn("ещё не запускался", report.verdict)

    def test_finalize_failed_not_resumable_is_artifact_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            _write_workflow_log(
                output_dir,
                [
                    {"stage": "capture", "status": "ok", "at": "2026-01-01T00:00:00Z"},
                    {"stage": "codex", "status": "ok", "at": "2026-01-01T00:01:00Z"},
                    {"stage": "finalize", "status": "failed", "at": "2026-01-01T00:02:00Z"},
                    {"stage": "terminal", "status": "intervention-required", "at": "2026-01-01T00:03:00Z"},
                ],
            )

            seams = DoctorSeams(
                today=lambda: date(2026, 1, 1),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )

            report = diagnose(config, output_dir, seams=seams)
            # Finalize failure → artifact failure (blocked).
            self.assertEqual(report.diagnosis, DiagnosisClass.ARTIFACT_FAILURE)
            self.assertEqual(exit_code_for(report), EXIT_BLOCKED)
            self.assertNotIn("ещё не запускался", report.verdict)


# ── Finding 4b: Completed without repo-finalize entries ───────────────────


class CompletedWithoutFinalizeEntriesTests(unittest.TestCase):
    """4b: completed/* outcome with NO repository-finalize entries must be
    classified correctly — never "never ran"."""

    def test_completed_no_finalize_entries_healthy_with_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            # Completed outcome but NO repository-finalize entries (older run
            # without a repository adapter).
            _write_workflow_log(
                output_dir,
                [
                    {"stage": "capture", "status": "ok", "at": "2026-01-01T00:00:00Z"},
                    {"stage": "pipeline", "status": "ok", "at": "2026-01-01T00:05:00Z"},
                    {"stage": "terminal", "status": "completed/no-attention", "at": "2026-01-01T00:06:00Z"},
                ],
            )

            _write_artifact(output_dir, "pilot-status.json", {"ready_for_editor": True, "run_date": "2026-01-01"})
            _write_artifact(output_dir, "run-summary.json", {"editor_facing_count": 10, "run_date": "2026-01-01"})
            _write_artifact(output_dir, "source-runs.json", [{"source_name": "S1", "status": "codex-captured"}])
            (output_dir / "weekly-review.html").write_text("<html>review</html>", encoding="utf-8")

            seams = DoctorSeams(
                today=lambda: date(2026, 1, 1),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )

            report = diagnose(config, output_dir, seams=seams)
            # Must be HEALTHY (not "never ran").
            self.assertEqual(report.diagnosis, DiagnosisClass.HEALTHY)
            self.assertEqual(exit_code_for(report), EXIT_HEALTHY)
            self.assertNotIn("ещё не запускался", report.verdict)
            # results_saved must be False (no evidence).
            self.assertFalse(report.results_saved)

    def test_completed_no_finalize_missing_review_is_review_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            _write_workflow_log(
                output_dir,
                [
                    {"stage": "capture", "status": "ok", "at": "2026-01-01T00:00:00Z"},
                    {"stage": "terminal", "status": "completed/attention-required", "at": "2026-01-01T00:05:00Z"},
                ],
            )

            _write_artifact(output_dir, "pilot-status.json", {"ready_for_editor": True, "run_date": "2026-01-01"})
            _write_artifact(output_dir, "run-summary.json", {"editor_facing_count": 5, "run_date": "2026-01-01"})
            _write_artifact(output_dir, "source-runs.json", [{"source_name": "S1", "status": "codex-captured"}])
            # No weekly-review.html.

            seams = DoctorSeams(
                today=lambda: date(2026, 1, 1),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )

            report = diagnose(config, output_dir, seams=seams)
            # Must be COMPLETED_REVIEW_REFRESH, not "never ran".
            self.assertEqual(report.diagnosis, DiagnosisClass.COMPLETED_REVIEW_REFRESH)
            self.assertNotIn("ещё не запускался", report.verdict)
            self.assertFalse(report.results_saved)


class ReadOnlyBehaviorTests(unittest.TestCase):
    def test_diagnose_does_not_mutate_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            _write_workflow_log(
                output_dir,
                [
                    {"stage": "capture", "status": "ok", "at": "2026-01-01T00:00:00Z"},
                    {"stage": "repository-finalize", "status": "ok", "at": "2026-01-01T00:10:00Z"},
                    {"stage": "terminal", "status": "completed/no-attention", "at": "2026-01-01T00:11:00Z"},
                ],
            )

            pilot_data = {"ready_for_editor": True, "run_date": "2026-01-01"}
            _write_artifact(output_dir, "pilot-status.json", pilot_data)
            _write_artifact(output_dir, "run-summary.json", {"editor_facing_count": 10, "run_date": "2026-01-01"})
            _write_artifact(output_dir, "source-runs.json", [{"source_name": "S1", "status": "codex-captured"}])
            (output_dir / "weekly-review.html").write_text("<html>review</html>", encoding="utf-8")

            def snapshot() -> dict[str, str]:
                result = {}
                for fpath in sorted(output_dir.rglob("*")):
                    if fpath.is_file():
                        result[str(fpath.relative_to(output_dir))] = fpath.read_bytes().hex()
                result.update({name: checksum.hex() for name, checksum in state_databases().items()})
                return result

            before = snapshot()

            seams = DoctorSeams(
                today=lambda: date(2026, 1, 1),
                repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {"guard_exists": False},
            )

            _ = diagnose(config, output_dir, seams=seams)

            after = snapshot()
            self.assertEqual(before, after, "diagnose must not mutate any files")

    def test_diagnose_does_not_call_git_fetch(self):
        """Prove that the local inspect seam never calls git fetch."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            calls: list[list[str]] = []

            original_run = subprocess.run

            def _tracking_run(*args, **kwargs):
                cmd = args[0] if args else kwargs.get("args", [])
                calls.append(list(cmd))
                return original_run(*args, **kwargs)

            subprocess.run = _tracking_run
            try:
                seams = DoctorSeams(
                    today=lambda: date(2026, 1, 1),
                    repo_inspect=None,
                    codex_probe=_fake_codex_probe_ok,
                    guard_status=lambda: {"guard_exists": False},
                )

                report = diagnose(config, output_dir, seams=seams)
                self.assertEqual(report.diagnosis, DiagnosisClass.HEALTHY)

                fetch_calls = [c for c in calls if "fetch" in c]
                self.assertEqual(len(fetch_calls), 0, f"doctor must not call git fetch, got: {fetch_calls}")
            finally:
                subprocess.run = original_run


class RussianAndJsonOutputTests(unittest.TestCase):
    def test_russian_contains_verdict_and_recommended_action(self):
        """render_russian must include verdict and recommended_action."""
        report = DoctorReport(
            diagnosis=DiagnosisClass.HEALTHY,
            verdict="AutoScout в порядке — поиск завершён успешно.",
            recommended_action="Откройте обзор.",
            review_available=True,
            results_saved=True,
        )

        russian = render_russian(report)
        self.assertIn(report.verdict, russian)
        self.assertIn(report.recommended_action, russian)
        # When results_saved is True, Russian must mention results saved.
        self.assertIn("Результаты сохранены", russian)
        # When review_available is True.
        self.assertIn("Обзор доступен", russian)

    def test_json_contains_same_verdict_and_action(self):
        """JSON output contains the same verdict and recommended_action."""
        report = DoctorReport(
            diagnosis=DiagnosisClass.HEALTHY,
            verdict="AutoScout в порядке — поиск завершён успешно.",
            recommended_action="Откройте обзор.",
            review_available=True,
            results_saved=True,
        )

        json_out = json.loads(render_json_report(report))
        self.assertEqual(report.verdict, json_out["verdict"])
        self.assertEqual(report.recommended_action, json_out["recommended_action"])

    def test_blocked_report_contains_question(self):
        """When blocked, render_russian must include the editor_question."""
        report = DoctorReport(
            diagnosis=DiagnosisClass.REPOSITORY_BLOCKER,
            verdict="Запуск поиска заблокирован — репозиторий не в безопасном состоянии.",
            recommended_action="Устраните проблему в репозитории и повторите запуск.",
            editor_question="Связаться с Димой для диагностики репозитория?",
        )

        russian = render_russian(report)
        self.assertIn(report.verdict, russian)
        self.assertIn(report.recommended_action, russian)
        self.assertIn(report.editor_question, russian)

    def test_healthy_output_contains_no_question(self):
        """Healthy reports must have no editor_question in Russian output."""
        report = DoctorReport(
            diagnosis=DiagnosisClass.HEALTHY,
            verdict="AutoScout в порядке — поиск завершён успешно.",
            recommended_action="Обзор готов. Откройте или обновите weekly-review.html.",
            review_available=True,
            results_saved=True,
        )
        self.assertEqual(report.editor_question, "")
        russian = render_russian(report)
        # No question line (no "?" at end — but editor_question is "").
        # Just verify we don't have an orphan question.
        self.assertNotIn("?", russian.splitlines()[-1] if russian.strip() else "")

    def test_output_follows_redaction_contract(self):
        report = DoctorReport(
            diagnosis=DiagnosisClass.RESUMABLE_OPERATION,
            verdict="Обнаружен прерванный поиск, который можно безопасно продолжить.",
            recommended_action="Продолжите прерванный поиск.",
            editor_question="Продолжить прерванный поиск?",
        )

        russian = render_russian(report)
        _assert_no_secrets_or_paths(russian)

        json_out = render_json_report(report)
        _assert_no_secrets_or_paths(json_out)

    def test_verdict_never_exposes_secrets(self):
        report = DoctorReport(
            diagnosis=DiagnosisClass.ARTIFACT_FAILURE,
            verdict="Артефакты поиска повреждены.",
            facts={"secret_found": False},
        )
        russian = render_russian(report)
        _assert_no_secrets_or_paths(russian)
        self.assertNotIn("sk-", russian)

    def test_json_serializable_to_dict(self):
        report = DoctorReport(
            diagnosis=DiagnosisClass.SOURCE_FAILURE,
            verdict="Есть проблемы с источниками: недоступны: NPR.",
            recommended_action="Проверьте источники.",
            facts={"failed_sources": ["NPR"], "failed_count": 1},
        )
        d = report.to_dict()
        self.assertEqual(d["diagnosis"], "source-failure")
        self.assertEqual(d["facts"]["failed_count"], 1)
        self.assertEqual(d["verdict"], report.verdict)
        json_str = json.dumps(d, ensure_ascii=False)
        self.assertIn("source-failure", json_str)
        self.assertIn("NPR", json_str)

    def test_results_saved_only_claimed_when_evidenced(self):
        """results_saved=False must never produce 'Результаты сохранены' in Russian."""
        report = DoctorReport(
            diagnosis=DiagnosisClass.RESUMABLE_OPERATION,
            verdict="Обнаружен прерванный поиск.",
            recommended_action="Продолжите.",
            results_saved=False,
        )
        russian = render_russian(report)
        self.assertNotIn("Результаты сохранены", russian)


class CodexCompatProbeTests(unittest.TestCase):
    """The compatibility probe reads help text only — never invokes a model.

    ``_codex_probe`` is deliberately kept to ``--version`` (see
    ``test_probe_never_calls_codex_exec``); compatibility lives in a separate
    probe so that boundary stays intact. This class enforces the equivalent
    boundary for the new probe.
    """

    def test_compat_probe_never_invokes_a_model(self):
        import autoscout.doctor as doctor_module

        original_run = subprocess.run
        calls: list[list[str]] = []

        def _tracking(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            calls.append(list(cmd))
            return original_run(*args, **kwargs)

        subprocess.run = _tracking
        try:
            doctor_module._codex_compat_probe(timeout_seconds=5.0)
        finally:
            subprocess.run = original_run

        for call in calls:
            # Any codex subcommand must be a help/version query, never a run.
            self.assertIn("--help", call, f"compat probe must only read help text, got: {' '.join(call)}")
            # A prompt argument would mean an actual extraction was attempted.
            positional = [a for a in call[1:] if not a.startswith("-")]
            self.assertEqual(
                positional,
                ["exec"],
                f"compat probe must pass no prompt, got: {' '.join(call)}",
            )

    def test_compat_probe_returns_boolean_verdict(self):
        result = _codex_compat_probe(timeout_seconds=5.0)
        self.assertIsInstance(result, dict)
        self.assertIsInstance(result.get("codex_compatible"), bool)

    def test_compat_probe_is_conservative_when_inconclusive(self):
        """An unparseable or failing help probe must not block a working install."""
        import autoscout.doctor as doctor_module

        original_run = subprocess.run
        original_which = doctor_module.shutil.which

        def _failing_run(*args, **kwargs):
            raise OSError("boom")

        subprocess.run = _failing_run
        doctor_module.shutil.which = lambda _name: "/somewhere/codex"
        try:
            self.assertTrue(doctor_module._codex_compat_probe(timeout_seconds=1.0)["codex_compatible"])
        finally:
            subprocess.run = original_run
            doctor_module.shutil.which = original_which

    def test_compat_probe_reports_incompatible_when_flag_absent(self):
        import autoscout.doctor as doctor_module

        class _Proc:
            returncode = 0
            stdout = "Usage: codex exec [OPTIONS]\n  --sandbox <MODE>\n  --skip-git-repo-check\n"
            stderr = ""

        original_run = subprocess.run
        original_which = doctor_module.shutil.which
        subprocess.run = lambda *a, **k: _Proc()
        doctor_module.shutil.which = lambda _name: "/somewhere/codex"
        try:
            result = doctor_module._codex_compat_probe(timeout_seconds=1.0)
        finally:
            subprocess.run = original_run
            doctor_module.shutil.which = original_which

        self.assertFalse(result["codex_compatible"])
        self.assertEqual(result["codex_missing_flags"], ["--output-last-message"])


class CodexProbeOfflineTests(unittest.TestCase):
    def test_probe_returns_sanitized_dict(self):
        result = _codex_probe(timeout_seconds=2.0)
        self.assertIsInstance(result, dict)
        if result.get("codex_available"):
            self.assertIn("codex_version", result)
            # Must not contain absolute path key.
            self.assertNotIn("executable", result)
        else:
            self.assertIn("error", result)

    def test_probe_never_calls_codex_exec(self):
        import autoscout.doctor as doctor_module

        original_run = subprocess.run
        exec_calls: list[list[str]] = []

        def _tracking(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            exec_calls.append(list(cmd))
            return original_run(*args, **kwargs)

        subprocess.run = _tracking
        try:
            _ = doctor_module._codex_probe(timeout_seconds=1.0)
            for call in exec_calls:
                cmd_str = " ".join(call)
                self.assertNotIn("exec", call, f"_codex_probe must not call codex exec, got: {cmd_str}")
        finally:
            subprocess.run = original_run

    def test_probe_returns_no_absolute_paths_when_codex_on_path(self):
        """Even when codex is on PATH, the result must not contain the absolute path."""
        result = _codex_probe(timeout_seconds=2.0)
        for key, value in result.items():
            self.assertNotEqual(key, "executable", "key 'executable' must not appear in probe result")
            if isinstance(value, str):
                self.assertFalse(
                    value.startswith("/"),
                    f"Probe value for '{key}' looks like an absolute path: {value}",
                )


class CLICommandTests(unittest.TestCase):
    def test_doctor_cli_healthy_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

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
                    ]
                )
            finally:
                sys.stdout = original_stdout

            output = stdout_buf.getvalue()
            self.assertEqual(exit_code, EXIT_HEALTHY)
            _assert_no_secrets_or_paths(output)
            self.assertIn("AutoScout", output)

    def test_doctor_cli_json_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

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
                        "--json",
                    ]
                )
            finally:
                sys.stdout = original_stdout

            output = stdout_buf.getvalue()
            self.assertEqual(exit_code, EXIT_HEALTHY)
            report = json.loads(output)
            self.assertEqual(report["diagnosis"], "healthy")
            _assert_no_secrets_or_paths(output)

    def test_doctor_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

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
                        "--json",
                    ]
                )
            finally:
                sys.stdout = original_stdout

            self.assertEqual(exit_code, EXIT_HEALTHY)


class DoctorSeamsInjectionTests(unittest.TestCase):
    def test_all_seams_injectable(self):
        seams = DoctorSeams(
            today=lambda: date(2026, 6, 15),
            repo_inspect=_fake_repo_inspect({"status": "ok", "branch": "main"}),
            codex_probe=_fake_codex_probe_ok,
            guard_status=lambda: {"guard_exists": False},
        )
        self.assertIsNotNone(seams.today)
        self.assertIsNotNone(seams.repo_inspect)
        self.assertIsNotNone(seams.codex_probe)
        self.assertIsNotNone(seams.guard_status)
        self.assertEqual(seams.today(), date(2026, 6, 15))

    def test_guard_status_facts_propagated(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            seams = DoctorSeams(
                today=lambda: date(2026, 1, 1),
                repo_inspect=_fake_repo_inspect({"status": "unsafe", "reasons": ["guard_held"], "branch": "main"}),
                codex_probe=_fake_codex_probe_ok,
                guard_status=lambda: {
                    "guard_exists": True,
                    "guard_age_seconds": 42.5,
                    "guard_pid": "12345",
                    "guard_pid_alive": False,
                },
            )

            report = diagnose(config, output_dir, seams=seams)
            self.assertEqual(report.diagnosis, DiagnosisClass.REPOSITORY_BLOCKER)
            facts = report.to_dict()["facts"]
            self.assertIn("guard", facts)
            self.assertEqual(facts["guard"]["guard_age_seconds"], 42.5)
            self.assertFalse(facts["guard"]["guard_pid_alive"])


# ── Launcher tests (mirror test_issue_270_271 patterns) ───────────────────


class _FakeDoctorRepo:
    """A temporary directory structured like a git repo with a doctor launcher copy."""

    def __init__(self, tmp_root: Path) -> None:
        self.repo_root = tmp_root / "repo"
        self.scripts_dir = self.repo_root / "scripts"
        self.scripts_dir.mkdir(parents=True)

        self.launcher = self.scripts_dir / "doctor"
        self.launcher.write_text(REAL_DOCTOR_LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8")
        self.launcher.chmod(REAL_DOCTOR_LAUNCHER.stat().st_mode)

        (self.repo_root / ".git").mkdir()
        (self.repo_root / "configs").mkdir()

    def add_venv_python(self, content: str | None = None) -> Path:
        venv_bin = self.repo_root / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        fake_python = venv_bin / "python"
        fake_python.write_text(
            content
            or textwrap.dedent("""\
                #!/usr/bin/env bash
                echo "FAKE_DOCTOR_PYTHON" "$@"
            """),
            encoding="utf-8",
        )
        fake_python.chmod(0o755)
        return fake_python

    def invoke_from(self, cwd: str | Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.launcher)],
            capture_output=True,
            text=True,
            cwd=str(cwd),
        )


class LauncherDoctorTests(unittest.TestCase):
    def test_launcher_exists_and_is_executable(self):
        self.assertTrue(
            REAL_DOCTOR_LAUNCHER.exists(),
            f"Doctor launcher must exist at {REAL_DOCTOR_LAUNCHER}",
        )
        self.assertTrue(
            os.access(str(REAL_DOCTOR_LAUNCHER), os.X_OK),
            "Doctor launcher must be executable",
        )

    def test_launcher_prefers_venv_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _FakeDoctorRepo(Path(tmp))
            repo.add_venv_python()

            result = repo.invoke_from(repo.repo_root)
            self.assertIn("FAKE_DOCTOR_PYTHON", result.stdout)

    def test_launcher_falls_back_to_python3(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _FakeDoctorRepo(Path(tmp))
            result = repo.invoke_from(repo.repo_root)
            self.assertIn("autoscout", result.stdout.lower() or result.stderr.lower())

    def test_launcher_invocation_passes_extra_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _FakeDoctorRepo(Path(tmp))
            repo.add_venv_python(
                textwrap.dedent("""\
                    #!/usr/bin/env bash
                    echo "FAKE" "$@"
                """)
            )

            result = subprocess.run(
                [str(repo.launcher), "--json"],
                capture_output=True,
                text=True,
                cwd=str(repo.repo_root),
            )
            self.assertIn("--json", result.stdout)
            self.assertIn("--config", result.stdout)
            self.assertIn("--output-dir", result.stdout)

    def test_launcher_from_subdirectory(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _FakeDoctorRepo(Path(tmp))
            repo.add_venv_python()
            subdir = repo.repo_root / "some" / "deep" / "subdir"
            subdir.mkdir(parents=True)

            result = repo.invoke_from(subdir)
            self.assertIn("FAKE_DOCTOR_PYTHON", result.stdout)

    def test_launcher_error_on_no_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _FakeDoctorRepo(Path(tmp))
            fake_bin = Path(tmp) / "fake-bin"
            fake_bin.mkdir()
            bash = "/bin/bash"
            if not Path(bash).exists():
                bash = "/usr/bin/bash"
            (fake_bin / "bash").symlink_to(bash)
            (fake_bin / "sh").symlink_to(bash)

            result = subprocess.run(
                [str(repo.launcher)],
                capture_output=True,
                text=True,
                cwd=str(repo.repo_root),
                env={**os.environ, "PATH": str(fake_bin)},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no usable Python interpreter", result.stderr)

    def test_launcher_uses_set_euo_pipefail(self):
        launcher_text = REAL_DOCTOR_LAUNCHER.read_text(encoding="utf-8")
        self.assertIn("set -euo pipefail", launcher_text)

    def test_launcher_resolves_repo_root_with_dirname(self):
        launcher_text = REAL_DOCTOR_LAUNCHER.read_text(encoding="utf-8")
        self.assertIn('$(cd "$(dirname "$0")/.." && pwd)', launcher_text)
