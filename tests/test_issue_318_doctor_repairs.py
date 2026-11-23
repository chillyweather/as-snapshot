"""Tests for issue #318: allowlisted idempotent local repairs to doctor.

Covers:
- All four repairs: remove-stale-lock, rerun-health, rerender-review, open-review.
- Lock: dead-pid+old → removed; second invocation → already-complete;
  live pid → blocked; young → blocked; non-integer content → blocked;
  unreadable → blocked; pid probe unknown → blocked; unresolvable repo root → blocked.
- Health: valid canonical artifacts → changed; missing artifact → blocked;
  date inconsistency → blocked; missing run_date → blocked (no_run_date).
- Review: valid inputs → changed; second run → already-complete;
  missing scouting-candidates.json → blocked; renderer rc!=0 → failed.
- Open: valid review → changed; missing html → blocked.
- Ordering/partial failure: health blocked stops rerender-review.
- Idempotency: full set twice shows already-complete for both lock and review.
- Forbidden mutations: state files, config, workflow-log.json, and git HEAD
  remain byte-for-byte identical after all repairs.
- Output contract: Russian text contains no tmp_path, no raw Git directives,
  no absolute paths; JSON parses correctly.
- Default invocation unchanged: doctor without --repair still read-only.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from typing import ClassVar

from state_store_support import state_databases

from autoscout.__main__ import main as cli_main
from autoscout.doctor import EXIT_BLOCKED
from autoscout.doctor import EXIT_HEALTHY
from autoscout.doctor import DoctorSeams
from autoscout.doctor_repairs import CANONICAL_REPAIR_ORDER
from autoscout.doctor_repairs import VALID_REPAIR_NAMES
from autoscout.doctor_repairs import RepairSeams
from autoscout.doctor_repairs import _build_plan
from autoscout.doctor_repairs import _render_json_report
from autoscout.doctor_repairs import _render_russian_report
from autoscout.doctor_repairs import run_repairs
from autoscout.git_envelope import GUARD_FILENAME
from autoscout.git_envelope import GUARD_STALE_SECONDS

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_DOCTOR_LAUNCHER = REPO_ROOT / "scripts" / "doctor"


# ── Helpers ───────────────────────────────────────────────────────────────


def _init_git_repo(path: Path, with_remote: bool = True) -> None:
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
    if with_remote:
        # The remote lives inside the repository's .git directory, so it goes
        # with the directory the caller owns; beside it, in the temporary
        # directory's root, nothing removed it (#1119).
        remote_path = path / ".git" / "test-remote.git"
        subprocess.run(
            ["git", "init", "--bare", str(remote_path)],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", str(remote_path)],
            cwd=str(path),
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "push", "--set-upstream", "origin", "main"],
            cwd=str(path),
            capture_output=True,
            check=True,
        )


def _make_output_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _make_doctor_seams_ok() -> DoctorSeams:
    """Return DoctorSeams with a fake repo_inspect that reports 'ok'."""

    def _ok_inspect(_allowlist=None):
        return {"status": "ok", "branch": "main", "reasons": []}

    return DoctorSeams(
        repo_inspect=_ok_inspect,
        guard_status=lambda: {"guard_exists": False},
    )


def _write_artifact(output_dir: Path, name: str, data: object) -> None:
    (output_dir / name).write_text(json.dumps(data), encoding="utf-8")


def _make_valid_canonical_artifacts(output_dir: Path, run_date: str = "2026-01-01") -> None:
    """Create the minimal set of valid canonical artifacts for health + review."""
    _write_artifact(
        output_dir,
        "pilot-status.json",
        {"ready_for_editor": True, "run_date": run_date},
    )
    _write_artifact(
        output_dir,
        "run-summary.json",
        {
            "editor_facing_count": 10,
            "run_date": run_date,
            "update_delta_counts": {"new": 5, "upgraded": 2, "changed": 1, "unchanged": 2},
        },
    )
    _write_artifact(
        output_dir,
        "source-runs.json",
        [
            {"source_name": "S1", "status": "codex-captured", "source_check_date": run_date},
        ],
    )
    _write_artifact(
        output_dir,
        "scouting-candidates.json",
        [
            {
                "identity_key": "Test Book|Author Name",
                "title": "Test Book",
                "primary_authors": ["Author Name"],
                "update_delta": "new",
                "source_evidence": [],
            },
        ],
    )


def _make_review_html(output_dir: Path) -> None:
    (output_dir / "weekly-review.html").write_text("<html>review</html>", encoding="utf-8")


def _snapshot_files(output_dir: Path, *extra_paths: Path) -> dict[str, str]:
    """SHA-256 hex digests of key files in output_dir plus extra paths.

    Returns a dict of str(path_relative_to_output_dir_or_abs) -> hex.
    """
    snap: dict[str, str] = {}
    for fpath in sorted(output_dir.rglob("*")):
        if fpath.is_file():
            snap[str(fpath.relative_to(output_dir))] = hashlib.sha256(fpath.read_bytes()).hexdigest()
    for ep in extra_paths:
        if ep.is_file():
            snap[str(ep)] = hashlib.sha256(ep.read_bytes()).hexdigest()
    snap.update({name: checksum.hex() for name, checksum in state_databases().items()})
    return snap


def _fake_health_runner_factory(rc: int = 0, stdout: str = "") -> any:
    def _runner(output_dir, effective_date):
        return rc, stdout or ("run_health_status=ready\nclassification=ready\nready_for_editor=true\n")

    return _runner


def _fake_health_runner_failing() -> any:
    def _runner(output_dir, effective_date):
        return 1, "run_health_status=incomplete\nmissing_artifact=pilot-status.json\n"

    return _runner


def _fake_review_renderer_factory(rc: int = 0, stdout: str = "") -> any:
    def _runner(output_dir, state_dir, effective_date):
        # Actually write files so the hash comparison detects changes.
        (Path(output_dir) / "weekly-review.md").write_text("# Review\n", encoding="utf-8")
        (Path(output_dir) / "weekly-review.html").write_text("<html>review</html>\n", encoding="utf-8")
        return rc, stdout or "Review rendered\n"

    return _runner


def _fake_review_opener(status: str = "ok") -> any:
    def _opener(path):
        return {"status": status, "opened": status == "ok", "path": str(path)}

    return _opener


# ── Redaction contract helpers ────────────────────────────────────────────


def _assert_no_secrets_or_paths(output: str) -> None:
    for directive in ("::git-stage", "::git-commit", "::git-push"):
        assert directive not in output, f"Raw Git directive in output: {directive}"
    assert "Traceback" not in output, f"Stack trace in output: {output}"


def _assert_no_tmp_path(output: str, tmp_path: str) -> None:
    assert tmp_path not in output, f"tmp_path leaked into output: {output[:200]}"


# ── Repair Selection & Plan Tests ─────────────────────────────────────────


class RepairSelectionTests(unittest.TestCase):
    def test_valid_repair_names_match_canonical_order(self):
        self.assertEqual(set(VALID_REPAIR_NAMES), set(CANONICAL_REPAIR_ORDER))
        self.assertEqual(len(VALID_REPAIR_NAMES), len(CANONICAL_REPAIR_ORDER))

    def test_build_plan_orders_canonically(self):
        plan = _build_plan(["rerender-review", "remove-stale-lock", "rerun-health"])
        self.assertEqual(
            [e.name for e in plan],
            ["remove-stale-lock", "rerun-health", "rerender-review"],
        )

    def test_build_plan_deduplicates(self):
        plan = _build_plan(["rerun-health", "rerun-health", "rerun-health"])
        self.assertEqual([e.name for e in plan], ["rerun-health"])

    def test_build_plan_empty(self):
        self.assertEqual(_build_plan([]), [])


# ── Remove Stale Lock Tests ───────────────────────────────────────────────


class RepoRootHelperTests(unittest.TestCase):
    """One repo-root walker, not four near-copies free to drift."""

    def test_no_module_defines_its_own_walker(self):
        import autoscout.doctor as doctor_mod
        import autoscout.doctor_recovery as recovery_mod
        import autoscout.doctor_repairs as repairs_mod

        for module in (doctor_mod, repairs_mod, recovery_mod):
            self.assertFalse(
                hasattr(module, "_resolve_repo_root"),
                f"{module.__name__} should use git_envelope.resolve_repo_root, not its own copy",
            )

    def test_all_callers_share_one_implementation(self):
        from autoscout.cli.weekly import _find_repo_root
        from autoscout.doctor import resolve_repo_root as from_doctor
        from autoscout.doctor_recovery import resolve_repo_root as from_recovery
        from autoscout.doctor_repairs import resolve_repo_root as from_repairs
        from autoscout.git_envelope import resolve_repo_root as canonical

        self.assertIs(from_doctor, canonical)
        self.assertIs(from_repairs, canonical)
        self.assertIs(from_recovery, canonical)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            nested = tmp_path / "a" / "b"
            nested.mkdir(parents=True)

            self.assertEqual(canonical(nested), tmp_path.resolve())
            # The CLI wrapper agrees, differing only in its non-optional
            # fallback when no repository is found.
            self.assertEqual(_find_repo_root(nested), tmp_path.resolve())

        with tempfile.TemporaryDirectory() as bare:
            bare_path = Path(bare)
            self.assertIsNone(canonical(bare_path))
            self.assertEqual(_find_repo_root(bare_path), bare_path.resolve())


class GuardStatusAgreementTests(unittest.TestCase):
    """``guard_status`` and ``remove_stale_guard`` must agree on liveness.

    Regression: ``guard_status`` swallowed every ``OSError`` and reported
    ``guard_pid_alive`` False, so an EPERM lock — a live process owned by
    another user, which ``remove_stale_guard`` correctly treats as ALIVE —
    was reported dead. That undercut the doctor's lock-ownership check.
    """

    def _adapter(self, tmp_path: Path, pid_text: str = "4242"):
        from autoscout.git_envelope import GitRepositoryAdapter

        _init_git_repo(tmp_path)
        lockfile = tmp_path / GUARD_FILENAME
        lockfile.write_text(pid_text, encoding="utf-8")
        return GitRepositoryAdapter(tmp_path)

    def test_eperm_reported_alive_matching_the_repair(self):
        def _eperm(pid, sig):
            raise PermissionError()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            adapter = self._adapter(tmp_path)

            status = adapter.guard_status(pid_probe=_eperm)
            self.assertIs(status["guard_pid_alive"], True)

            # The repair's independent verdict for the same probe: alive.
            old = time.time() - GUARD_STALE_SECONDS - 100
            os.utime(str(tmp_path / GUARD_FILENAME), (old, old))
            repair = adapter.remove_stale_guard(pid_probe=_eperm)
            self.assertEqual(repair["reason"], "guard_pid_alive")
            self.assertTrue((tmp_path / GUARD_FILENAME).exists())

    def test_esrch_reported_dead(self):
        def _esrch(pid, sig):
            raise ProcessLookupError()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            adapter = self._adapter(tmp_path)
            self.assertIs(adapter.guard_status(pid_probe=_esrch)["guard_pid_alive"], False)

    def test_unknown_oserror_reported_as_ambiguous_not_dead(self):
        def _weird(pid, sig):
            raise OSError(errno.EIO, "io error")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            adapter = self._adapter(tmp_path)
            # None, not False — ownership is unknown, so callers must not
            # treat it as a dead lock.
            self.assertIsNone(adapter.guard_status(pid_probe=_weird)["guard_pid_alive"])

    def test_malformed_pid_reported_as_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            adapter = self._adapter(tmp_path, pid_text="not-a-pid")
            status = adapter.guard_status()
            self.assertIsNone(status["guard_pid_alive"])
            self.assertTrue(status["guard_malformed"])

    def test_foreign_live_lock_is_diagnosed_as_running(self):
        """End of the chain: an EPERM lock must read running, not healthy.

        Product-rule change, authorized by #706: a live lock (including one
        owned by a foreign process the current user cannot signal) is a
        running operation, reported from the live snapshot — no longer a
        ``repository-blocker`` with an escalation question.
        """
        from autoscout.doctor import DiagnosisClass
        from autoscout.doctor import DoctorSeams
        from autoscout.doctor import diagnose

        def _eperm(pid, sig):
            raise PermissionError()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            adapter = self._adapter(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            report = diagnose(
                config,
                output_dir,
                seams=DoctorSeams(
                    today=lambda: date(2026, 1, 1),
                    repo_inspect=lambda allowlist=None: {"status": "ok", "branch": "main"},
                    codex_probe=lambda: {"codex_available": True, "codex_version": "1"},
                    codex_compat_probe=lambda: {"codex_compatible": True},
                    guard_status=lambda: adapter.guard_status(pid_probe=_eperm),
                ),
            )
            self.assertEqual(report.diagnosis, DiagnosisClass.RUN_IN_PROGRESS)
            self.assertIn("выполняется", report.verdict.lower())
            self.assertEqual(report.editor_question, "")


class RemoveStaleLockTests(unittest.TestCase):
    def test_dead_pid_old_lock_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            # Create a stale lock with a dead pid.
            lockfile = tmp_path / GUARD_FILENAME
            lockfile.write_text("99999", encoding="utf-8")
            old_mtime = time.time() - GUARD_STALE_SECONDS - 100
            os.utime(str(lockfile), (old_mtime, old_mtime))

            def _dead_pid_probe(pid, sig):
                raise ProcessLookupError()

            seams = RepairSeams(
                now=lambda: time.time(),
                pid_probe=_dead_pid_probe,
                doctor_seams=_make_doctor_seams_ok(),
            )

            report, exit_code = run_repairs(
                config,
                output_dir,
                None,
                ["remove-stale-lock"],
                seams=seams,
            )

            self.assertEqual(exit_code, EXIT_HEALTHY)  # post-diagnosis is healthy (no run)
            self.assertFalse(lockfile.exists(), "Lock should be removed")

            repairs = report.repairs
            self.assertEqual(len(repairs), 1)
            self.assertEqual(repairs[0]["name"], "remove-stale-lock")
            self.assertEqual(repairs[0]["outcome"], "changed")
            self.assertIn(GUARD_FILENAME, repairs[0]["changed_artifacts"])

    def test_second_invocation_already_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            # No lock — already removed.
            seams = RepairSeams()

            report, exit_code = run_repairs(
                config,
                output_dir,
                None,
                ["remove-stale-lock"],
                seams=seams,
            )
            repairs = report.repairs
            self.assertEqual(repairs[0]["outcome"], "already-complete")

    def test_live_pid_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            lockfile = tmp_path / GUARD_FILENAME
            lockfile.write_text("1", encoding="utf-8")  # pid 1 (init/systemd) is usually alive
            old_mtime = time.time() - GUARD_STALE_SECONDS - 100
            os.utime(str(lockfile), (old_mtime, old_mtime))

            def _live_pid_probe(pid, sig):
                raise PermissionError()  # EPERM → alive

            seams = RepairSeams(
                now=lambda: time.time(),
                pid_probe=_live_pid_probe,
            )

            report, exit_code = run_repairs(
                config,
                output_dir,
                None,
                ["remove-stale-lock"],
                seams=seams,
            )

            self.assertEqual(exit_code, EXIT_BLOCKED)
            self.assertTrue(lockfile.exists(), "Lock should NOT be removed for live pid")
            self.assertEqual(report.repairs[0]["outcome"], "blocked")
            self.assertEqual(report.repairs[0]["precondition_reason"], "guard_pid_alive")

    def test_young_lock_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            lockfile = tmp_path / GUARD_FILENAME
            lockfile.write_text("12345", encoding="utf-8")
            # Fresh mtime — age zero.
            fresh_time = time.time()
            os.utime(str(lockfile), (fresh_time, fresh_time))

            seams = RepairSeams(now=lambda: fresh_time)

            report, exit_code = run_repairs(
                config,
                output_dir,
                None,
                ["remove-stale-lock"],
                seams=seams,
            )

            self.assertEqual(exit_code, EXIT_BLOCKED)
            self.assertTrue(lockfile.exists())
            self.assertEqual(report.repairs[0]["precondition_reason"], "guard_held_young")

    def test_non_integer_content_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            lockfile = tmp_path / GUARD_FILENAME
            lockfile.write_text("not-a-pid", encoding="utf-8")
            old_mtime = time.time() - GUARD_STALE_SECONDS - 100
            os.utime(str(lockfile), (old_mtime, old_mtime))

            seams = RepairSeams(now=lambda: time.time())

            report, exit_code = run_repairs(
                config,
                output_dir,
                None,
                ["remove-stale-lock"],
                seams=seams,
            )

            self.assertEqual(exit_code, EXIT_BLOCKED)
            self.assertTrue(lockfile.exists())
            self.assertEqual(report.repairs[0]["precondition_reason"], "guard_malformed")

    def test_unreadable_lock_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            lockfile = tmp_path / GUARD_FILENAME
            lockfile.write_text("12345", encoding="utf-8")
            old_mtime = time.time() - GUARD_STALE_SECONDS - 100
            os.utime(str(lockfile), (old_mtime, old_mtime))

            # Inject a failing stat via the now callable — actually we simulate
            # unreadable via a pid probe that never gets called because the file is
            # readable. Let's test unreadable by making the lock a directory instead:
            lockfile.unlink()
            lockfile.mkdir()

            seams = RepairSeams(now=lambda: time.time())

            # When lockfile is a directory, read_text will fail with IsADirectoryError
            # which is an OSError subclass → guard_unreadable.
            # Actually our implementation calls stat() first which succeeds for dirs,
            # then read_text fails. Let's see the actual behavior...

            # The stat() call should succeed for a dir, but read_text() would raise.
            # But our code does stat first for age, so the dir will pass stat,
            # then read_text will raise IsADirectoryError which IS an OSError subclass.
            # That should produce guard_unreadable.

            report, exit_code = run_repairs(
                config,
                output_dir,
                None,
                ["remove-stale-lock"],
                seams=seams,
            )

            self.assertEqual(exit_code, EXIT_BLOCKED)
            self.assertEqual(report.repairs[0]["precondition_reason"], "guard_unreadable")

    def test_pid_probe_unknown_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            lockfile = tmp_path / GUARD_FILENAME
            lockfile.write_text("12345", encoding="utf-8")
            old_mtime = time.time() - GUARD_STALE_SECONDS - 100
            os.utime(str(lockfile), (old_mtime, old_mtime))

            # OSError with errno that is neither ESRCH nor EPERM → unknown.
            def _unknown_probe(pid, sig):
                ex = OSError()
                ex.errno = errno.EACCES  # Permission denied, not EPERM
                raise ex

            seams = RepairSeams(
                now=lambda: time.time(),
                pid_probe=_unknown_probe,
            )

            report, exit_code = run_repairs(
                config,
                output_dir,
                None,
                ["remove-stale-lock"],
                seams=seams,
            )

            self.assertEqual(exit_code, EXIT_BLOCKED)
            self.assertTrue(lockfile.exists())
            self.assertEqual(report.repairs[0]["precondition_reason"], "guard_pid_unknown")

    def test_unresolvable_repo_root_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # No .git directory → repo root unresolvable from config.
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            seams = RepairSeams()

            report, exit_code = run_repairs(
                config,
                output_dir,
                None,
                ["remove-stale-lock"],
                seams=seams,
            )

            self.assertEqual(exit_code, EXIT_BLOCKED)
            self.assertEqual(report.repairs[0]["outcome"], "blocked")
            self.assertEqual(report.repairs[0]["precondition_reason"], "no_repo_root")


# ── Rerun Health Tests ────────────────────────────────────────────────────


class RerunHealthTests(unittest.TestCase):
    def test_valid_artifacts_health_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")
            _make_valid_canonical_artifacts(output_dir)

            seams = RepairSeams(
                health_runner=_fake_health_runner_factory(rc=0),
            )

            report, exit_code = run_repairs(
                config,
                output_dir,
                None,
                ["rerun-health"],
                seams=seams,
            )

            repairs = report.repairs
            self.assertEqual(len(repairs), 1)
            self.assertEqual(repairs[0]["name"], "rerun-health")
            # Verification only — nothing was written, so nothing "changed".
            self.assertEqual(repairs[0]["outcome"], "already-complete")
            self.assertEqual(repairs[0]["precondition"], "passed")
            self.assertIn("run_health_status", repairs[0]["detail"])

    def test_missing_run_date_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")
            # run-summary.json with blank run_date.
            _write_artifact(output_dir, "pilot-status.json", {"ready_for_editor": True, "run_date": "2026-01-01"})
            _write_artifact(output_dir, "run-summary.json", {"editor_facing_count": 10, "run_date": ""})
            _write_artifact(output_dir, "source-runs.json", [])

            seams = RepairSeams()

            report, exit_code = run_repairs(
                config,
                output_dir,
                None,
                ["rerun-health"],
                seams=seams,
            )

            self.assertEqual(exit_code, EXIT_BLOCKED)
            self.assertEqual(report.repairs[0]["precondition_reason"], "no_run_date")
            self.assertEqual(report.repairs[0]["outcome"], "blocked")

    def test_missing_run_summary_no_run_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")
            # No run-summary.json at all.

            seams = RepairSeams()

            report, exit_code = run_repairs(
                config,
                output_dir,
                None,
                ["rerun-health"],
                seams=seams,
            )

            self.assertEqual(exit_code, EXIT_BLOCKED)
            self.assertEqual(report.repairs[0]["precondition_reason"], "no_run_date")

    def test_health_check_failed_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")
            _make_valid_canonical_artifacts(output_dir)

            seams = RepairSeams(
                health_runner=_fake_health_runner_failing(),
            )

            report, exit_code = run_repairs(
                config,
                output_dir,
                None,
                ["rerun-health"],
                seams=seams,
            )

            self.assertEqual(exit_code, EXIT_BLOCKED)
            self.assertEqual(report.repairs[0]["precondition_reason"], "health_check_failed")

    def test_date_inconsistency_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")
            # run_date in source-runs doesn't match run-summary.
            _write_artifact(output_dir, "pilot-status.json", {"ready_for_editor": True, "run_date": "2026-01-01"})
            _write_artifact(output_dir, "run-summary.json", {"editor_facing_count": 10, "run_date": "2026-01-01"})
            _write_artifact(
                output_dir,
                "source-runs.json",
                [{"source_name": "S1", "status": "codex-captured", "source_check_date": "2025-12-31"}],
            )

            report, exit_code = run_repairs(
                config,
                output_dir,
                None,
                ["rerun-health"],
                seams=RepairSeams(),
            )

            self.assertEqual(exit_code, EXIT_BLOCKED)
            self.assertEqual(report.repairs[0]["precondition_reason"], "health_check_failed")

    def test_repeated_invocations_are_idempotent_and_write_nothing(self):
        """``rerun-health`` verifies; it never mutates, so it never reports changed.

        ``run_health`` reads the canonical artifacts and prints a verdict
        without writing anything, so a passing rerun has changed no artifact.
        Reporting ``already-complete`` on every pass makes the repair
        idempotent by construction (#318) and keeps the run directory clean —
        caching a verdict to manufacture a changed/already-complete transition
        would leave an untracked file that dirties the working tree.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")
            _make_valid_canonical_artifacts(output_dir)

            seams = RepairSeams(
                health_runner=_fake_health_runner_factory(rc=0),
            )

            before = sorted(p.name for p in output_dir.iterdir())

            for attempt in range(2):
                report, _ = run_repairs(
                    config,
                    output_dir,
                    None,
                    ["rerun-health"],
                    seams=seams,
                )
                with self.subTest(attempt=attempt):
                    self.assertEqual(report.repairs[0]["outcome"], "already-complete")
                    self.assertEqual(report.repairs[0]["changed_artifacts"], [])

            # No new artifact appeared in the run directory.
            self.assertEqual(sorted(p.name for p in output_dir.iterdir()), before)


# ── Rerender Review Tests ─────────────────────────────────────────────────


class RerenderReviewTests(unittest.TestCase):
    def test_valid_inputs_review_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")
            _make_valid_canonical_artifacts(output_dir)

            seams = RepairSeams(
                health_runner=_fake_health_runner_factory(rc=0),
                review_renderer=_fake_review_renderer_factory(rc=0, stdout="Review rendered to weekly-review.md\n"),
                doctor_seams=_make_doctor_seams_ok(),
            )

            report, exit_code = run_repairs(
                config,
                output_dir,
                None,
                ["rerender-review"],
                seams=seams,
            )

            repairs = report.repairs
            self.assertEqual(len(repairs), 1)
            self.assertEqual(repairs[0]["name"], "rerender-review")
            self.assertEqual(repairs[0]["outcome"], "changed")

    def test_missing_scouting_candidates_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")
            # Valid health artifacts but NO scouting-candidates.json.
            _make_valid_canonical_artifacts(output_dir)
            (output_dir / "scouting-candidates.json").unlink()

            seams = RepairSeams(
                health_runner=_fake_health_runner_factory(rc=0),
            )

            report, exit_code = run_repairs(
                config,
                output_dir,
                None,
                ["rerender-review"],
                seams=seams,
            )

            self.assertEqual(exit_code, EXIT_BLOCKED)
            self.assertEqual(report.repairs[0]["precondition_reason"], "missing_scouting_candidates")

    def test_render_failed_reports_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")
            _make_valid_canonical_artifacts(output_dir)

            seams = RepairSeams(
                health_runner=_fake_health_runner_factory(rc=0),
                review_renderer=_fake_review_renderer_factory(rc=1, stdout="error=something"),
            )

            report, exit_code = run_repairs(
                config,
                output_dir,
                None,
                ["rerender-review"],
                seams=seams,
            )

            self.assertEqual(exit_code, EXIT_BLOCKED)
            self.assertEqual(report.repairs[0]["outcome"], "failed")

    def test_health_gate_fails_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")
            _make_valid_canonical_artifacts(output_dir)

            seams = RepairSeams(
                health_runner=_fake_health_runner_failing(),
            )

            report, exit_code = run_repairs(
                config,
                output_dir,
                None,
                ["rerender-review"],
                seams=seams,
            )

            self.assertEqual(exit_code, EXIT_BLOCKED)
            self.assertEqual(report.repairs[0]["precondition_reason"], "health_check_failed")


# ── Open Review Tests ─────────────────────────────────────────────────────


class OpenReviewTests(unittest.TestCase):
    def test_valid_review_open_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")
            _make_valid_canonical_artifacts(output_dir)
            _make_review_html(output_dir)

            opened_paths: list[str] = []

            def _tracking_opener(path):
                opened_paths.append(str(path))
                return {"status": "ok", "opened": True, "path": str(path)}

            seams = RepairSeams(
                health_runner=_fake_health_runner_factory(rc=0),
                review_opener=_tracking_opener,
            )

            report, exit_code = run_repairs(
                config,
                output_dir,
                None,
                ["open-review"],
                seams=seams,
            )

            self.assertEqual(report.repairs[0]["outcome"], "changed")
            self.assertEqual(len(opened_paths), 1)
            self.assertIn("weekly-review.html", opened_paths[0])

            # Finding 2: opener detail must NOT contain the absolute path.
            detail = report.repairs[0]["detail"]
            self.assertNotIn("opener_result", detail, "opener_result must not leak into detail")
            self.assertNotIn("path", detail, "absolute path must not leak into detail")
            self.assertEqual(detail.get("opener_status"), "ok")
            self.assertEqual(detail.get("opener_opened"), True)

    def test_missing_html_blocked_opener_not_called(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")
            _make_valid_canonical_artifacts(output_dir)
            # No weekly-review.html.

            opened_calls: list[str] = []

            def _no_call_opener(path):
                opened_calls.append(str(path))
                return {"status": "ok", "opened": True, "path": str(path)}

            seams = RepairSeams(
                health_runner=_fake_health_runner_factory(rc=0),
                review_opener=_no_call_opener,
            )

            report, exit_code = run_repairs(
                config,
                output_dir,
                None,
                ["open-review"],
                seams=seams,
            )

            self.assertEqual(exit_code, EXIT_BLOCKED)
            self.assertEqual(report.repairs[0]["precondition_reason"], "review_html_missing")
            self.assertEqual(len(opened_calls), 0, "Opener must NOT be called when html missing")

    def test_opener_failure_reports_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")
            _make_valid_canonical_artifacts(output_dir)
            _make_review_html(output_dir)

            seams = RepairSeams(
                health_runner=_fake_health_runner_factory(rc=0),
                review_opener=_fake_review_opener("failed"),
            )

            report, exit_code = run_repairs(
                config,
                output_dir,
                None,
                ["open-review"],
                seams=seams,
            )

            self.assertEqual(report.repairs[0]["outcome"], "failed")


# ── Ordering and Partial Failure Tests ────────────────────────────────────


class OrderingPartialFailureTests(unittest.TestCase):
    def test_health_first_regardless_of_cli_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")
            _make_valid_canonical_artifacts(output_dir)
            _make_review_html(output_dir)

            seams = RepairSeams(
                health_runner=_fake_health_runner_factory(rc=0),
                review_renderer=_fake_review_renderer_factory(rc=0),
                review_opener=_fake_review_opener("ok"),
            )

            # Request in reverse order
            report, exit_code = run_repairs(
                config,
                output_dir,
                None,
                ["open-review", "rerender-review", "rerun-health"],
                seams=seams,
            )

            plan_names = [p["name"] for p in report.plan]
            self.assertEqual(
                plan_names,
                ["rerun-health", "rerender-review", "open-review"],
            )

    def test_health_blocked_stops_rerender(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")
            _make_valid_canonical_artifacts(output_dir)

            seams = RepairSeams(
                health_runner=_fake_health_runner_failing(),
                review_renderer=_fake_review_renderer_factory(rc=0),
            )

            report, exit_code = run_repairs(
                config,
                output_dir,
                None,
                ["rerun-health", "rerender-review"],
                seams=seams,
            )

            self.assertEqual(exit_code, EXIT_BLOCKED)
            self.assertEqual(report.repairs[0]["outcome"], "blocked")  # health
            self.assertEqual(report.repairs[1]["outcome"], "not-executed")  # review

            # Post-diagnosis should have a recommended_action and editor_question.
            self.assertIn("recommended_action", report.post_diagnosis)
            # When blocked, the Russian text should contain question.
            russian = _render_russian_report(report)
            self.assertIn("?", russian)


# ── Idempotency Tests ─────────────────────────────────────────────────────


class IdempotencyTests(unittest.TestCase):
    def test_full_set_twice_second_is_already_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")
            _make_valid_canonical_artifacts(output_dir)
            _make_review_html(output_dir)

            # Create a stale lock for first-run removal.
            lockfile = tmp_path / GUARD_FILENAME
            lockfile.write_text("99999", encoding="utf-8")
            old_mtime = time.time() - GUARD_STALE_SECONDS - 100
            os.utime(str(lockfile), (old_mtime, old_mtime))

            def _dead_pid_probe(pid, sig):
                raise ProcessLookupError()

            seams = RepairSeams(
                now=lambda: time.time(),
                pid_probe=_dead_pid_probe,
                health_runner=_fake_health_runner_factory(rc=0),
                review_renderer=_fake_review_renderer_factory(rc=0),
                review_opener=_fake_review_opener("ok"),
            )

            all_repairs = list(CANONICAL_REPAIR_ORDER)

            # First run.
            report1, _ = run_repairs(
                config,
                output_dir,
                None,
                all_repairs,
                seams=seams,
            )
            outcomes1 = {r["name"]: r["outcome"] for r in report1.repairs}
            self.assertEqual(outcomes1["remove-stale-lock"], "changed")

            # Second run — lock already gone.
            report2, _ = run_repairs(
                config,
                output_dir,
                None,
                all_repairs,
                seams=seams,
            )
            outcomes2 = {r["name"]: r["outcome"] for r in report2.repairs}
            self.assertEqual(outcomes2["remove-stale-lock"], "already-complete")

            # rerender-review second run should also be already-complete (hashes match)
            # But with fake renderer that doesn't write files, it depends.
            # The default renderer writes real files. Our fake just returns rc=0.
            # Since files don't change between runs, second run is already-complete.
            self.assertEqual(outcomes2["rerender-review"], "already-complete")


# ── Forbidden Mutations Tests ─────────────────────────────────────────────


class ForbiddenMutationsTests(unittest.TestCase):
    def test_state_files_and_git_unchanged(self):
        """Prove that state files, config, workflow-log, and git HEAD remain
        byte-for-byte identical after running all repairs."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            state_dir = _make_output_dir(tmp_path / "state")
            output_dir = _make_output_dir(tmp_path / "output")

            # Create state files that must NOT be mutated.
            state_files = {
                "candidate-history.json": [{"identity_key": "A|B", "title": "A"}],
                "candidate-visibility.json": [{"identity_key": "A|B", "visibility": "visible"}],
                "editorial-taste-profile.json": {"entries": []},
            }
            for name, data in state_files.items():
                (state_dir / name).write_text(json.dumps(data), encoding="utf-8")

            # Create workflow-log.json in output_dir (must not be mutated).
            (output_dir / "workflow-log.json").write_text(
                json.dumps([{"stage": "terminal", "status": "completed/no-attention"}]),
                encoding="utf-8",
            )

            _make_valid_canonical_artifacts(output_dir)

            # Capture initial HEAD
            head_before = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(tmp_path),
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

            # Snapshot state files and workflow-log and config.
            snap_before = {}
            for name in state_files:
                path = state_dir / name
                snap_before[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            snap_before["workflow-log.json"] = hashlib.sha256(
                (output_dir / "workflow-log.json").read_bytes()
            ).hexdigest()
            snap_before["config.json"] = hashlib.sha256(config.read_bytes()).hexdigest()

            # Run all repairs.
            seams = RepairSeams(
                health_runner=_fake_health_runner_factory(rc=0),
                review_renderer=_fake_review_renderer_factory(rc=0),
                review_opener=_fake_review_opener("ok"),
            )

            run_repairs(
                config,
                output_dir,
                state_dir,
                list(CANONICAL_REPAIR_ORDER),
                seams=seams,
            )

            # Verify HEAD unchanged.
            head_after = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(tmp_path),
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(head_before, head_after, "git HEAD must not change")

            # Verify state files byte-for-byte identical.
            for name in state_files:
                path = state_dir / name
                after_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                self.assertEqual(
                    snap_before[name],
                    after_hash,
                    f"{name} must be byte-for-byte identical after repairs",
                )

            # Verify workflow-log and config unchanged.
            self.assertEqual(
                snap_before["workflow-log.json"],
                hashlib.sha256((output_dir / "workflow-log.json").read_bytes()).hexdigest(),
                "workflow-log.json must be byte-for-byte identical",
            )
            self.assertEqual(
                snap_before["config.json"],
                hashlib.sha256(config.read_bytes()).hexdigest(),
                "config.json must be byte-for-byte identical",
            )


# ── Output Contract Tests ─────────────────────────────────────────────────


class OutputContractTests(unittest.TestCase):
    def test_russian_contains_no_tmp_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")
            _make_valid_canonical_artifacts(output_dir)

            seams = RepairSeams(
                health_runner=_fake_health_runner_factory(rc=0),
            )

            report, _ = run_repairs(
                config,
                output_dir,
                None,
                ["rerun-health"],
                seams=seams,
            )

            russian = _render_russian_report(report)
            _assert_no_secrets_or_paths(russian)
            _assert_no_tmp_path(russian, str(tmp_path))
            # Should NOT contain absolute path-like strings.
            self.assertNotIn("artifact=", russian)

    def test_json_output_parseable_and_consistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")
            _make_valid_canonical_artifacts(output_dir)

            seams = RepairSeams(
                health_runner=_fake_health_runner_factory(rc=0),
            )

            report, exit_code = run_repairs(
                config,
                output_dir,
                None,
                ["rerun-health"],
                seams=seams,
            )

            json_str = _render_json_report(report)
            parsed = json.loads(json_str)

            self.assertIn("plan", parsed)
            self.assertIn("repairs", parsed)
            self.assertIn("post_diagnosis", parsed)
            self.assertEqual(len(parsed["repairs"]), 1)
            # rerun-health verifies without writing, so it reports no change.
            self.assertEqual(parsed["repairs"][0]["outcome"], "already-complete")

            # JSON should not contain tmp_path.
            _assert_no_tmp_path(json_str, str(tmp_path))

    def test_blocked_repairs_exit_code_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            # Create a young lock to block.
            lockfile = tmp_path / GUARD_FILENAME
            lockfile.write_text("12345", encoding="utf-8")
            fresh_time = time.time()
            os.utime(str(lockfile), (fresh_time, fresh_time))

            seams = RepairSeams(now=lambda: fresh_time)

            _, exit_code = run_repairs(
                config,
                output_dir,
                None,
                ["remove-stale-lock"],
                seams=seams,
            )

            self.assertEqual(exit_code, EXIT_BLOCKED)

    def test_blocked_russian_has_exactly_one_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            lockfile = tmp_path / GUARD_FILENAME
            lockfile.write_text("12345", encoding="utf-8")
            fresh_time = time.time()
            os.utime(str(lockfile), (fresh_time, fresh_time))

            seams = RepairSeams(now=lambda: fresh_time)

            report, _ = run_repairs(
                config,
                output_dir,
                None,
                ["remove-stale-lock"],
                seams=seams,
            )

            russian = _render_russian_report(report)
            # Finding 3: blocked repair must have exactly one question line ending with "?"
            lines = russian.splitlines()
            question_lines = [ln for ln in lines if ln.strip().endswith("?")]
            self.assertEqual(len(question_lines), 1, f"Expected exactly 1 question line, got {question_lines}")
            # Finding 3: post-diagnosis must NOT appear when blocked
            self.assertNotIn("AutoScout в порядке", russian)
            self.assertNotIn("ещё не запускался", russian)
            self.assertNotIn("поиск не завершён", russian)
            # Report-level fields must be populated.
            json_parsed = json.loads(_render_json_report(report))
            self.assertIn("recommended_action", json_parsed)
            self.assertIn("editor_question", json_parsed)
            self.assertTrue(json_parsed["recommended_action"])
            self.assertTrue(json_parsed["editor_question"])


# ── Default Invocation Unchanged Tests ────────────────────────────────────


class DefaultInvocationUnchangedTests(unittest.TestCase):
    def test_doctor_without_repair_is_read_only(self):
        """Running doctor without --repair must still work as read-only."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            # Snapshot output dir.
            databases_before = state_databases()
            before_snap = {}
            for fpath in sorted(output_dir.rglob("*")):
                if fpath.is_file():
                    before_snap[str(fpath.relative_to(output_dir))] = fpath.read_bytes()

            # Run doctor WITHOUT --repair.
            stdout_buf = __import__("io").StringIO()
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

            # Output dir should be identical.
            after_snap = {}
            for fpath in sorted(output_dir.rglob("*")):
                if fpath.is_file():
                    after_snap[str(fpath.relative_to(output_dir))] = fpath.read_bytes()
            self.assertEqual(before_snap, after_snap, "doctor without --repair must not mutate files")
            self.assertEqual(state_databases(), databases_before, "doctor without --repair must not write a database")

    def test_cli_doctor_with_repair_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")
            _make_valid_canonical_artifacts(output_dir)

            stdout_buf = __import__("io").StringIO()
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
            # Should be JSON parseable.
            parsed = json.loads(output)
            self.assertIn("repairs", parsed)
            self.assertIn("post_diagnosis", parsed)


# ── Russian vs JSON Consistency Tests ─────────────────────────────────────


class RussianJsonConsistencyTests(unittest.TestCase):
    def test_repair_blocked_consistent_russian_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            # Young lock → blocked.
            lockfile = tmp_path / GUARD_FILENAME
            lockfile.write_text("12345", encoding="utf-8")
            fresh_time = time.time()
            os.utime(str(lockfile), (fresh_time, fresh_time))

            seams = RepairSeams(now=lambda: fresh_time)

            report, exit_code = run_repairs(
                config,
                output_dir,
                None,
                ["remove-stale-lock"],
                seams=seams,
            )

            russian = _render_russian_report(report)
            json_str = _render_json_report(report)
            parsed = json.loads(json_str)

            self.assertEqual(exit_code, EXIT_BLOCKED)
            self.assertEqual(parsed["repairs"][0]["outcome"], "blocked")
            self.assertIn("заблокирован", russian.lower())

    def test_repair_changed_consistent_russian_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")
            _make_valid_canonical_artifacts(output_dir)
            _make_review_html(output_dir)

            seams = RepairSeams(
                health_runner=_fake_health_runner_factory(rc=0),
                review_opener=_fake_review_opener("ok"),
            )

            report, _ = run_repairs(
                config,
                output_dir,
                None,
                ["open-review"],
                seams=seams,
            )

            russian = _render_russian_report(report)
            json_str = _render_json_report(report)
            parsed = json.loads(json_str)

            self.assertEqual(parsed["repairs"][0]["outcome"], "changed")
            self.assertIn("выполнено", russian.lower())


# ── RepairSeams Tests ─────────────────────────────────────────────────────


class RepairSeamsTests(unittest.TestCase):
    def test_all_seams_default_to_none(self):
        seams = RepairSeams()
        self.assertIsNone(seams.now)
        self.assertIsNone(seams.pid_probe)
        self.assertIsNone(seams.health_runner)
        self.assertIsNone(seams.review_renderer)
        self.assertIsNone(seams.review_opener)
        self.assertIsNone(seams.doctor_seams)

    def test_seams_are_injectable(self):
        called = []

        def fake_now():
            called.append("now")
            return 100.0

        seams = RepairSeams(now=fake_now, health_runner=_fake_health_runner_factory(0))
        self.assertEqual(seams.now(), 100.0)
        self.assertEqual(called, ["now"])


# ── Finding 1: No English tokens in Russian output ────────────────────────


class Finding1NoEnglishTokensTests(unittest.TestCase):
    """Prove that Russian output never contains English reason tokens."""

    _ENGLISH_TOKENS: ClassVar[list[str]] = [
        "no_repo_root",
        "guard_held_young",
        "guard_pid_alive",
        "guard_malformed",
        "guard_unreadable",
        "guard_pid_unknown",
        "guard_remove_failed",
        "no_run_date",
        "health_check_failed",
        "missing_scouting_candidates",
        "review_html_missing",
        "render_failed",
        "open_failed",
        "guard_not_found",
        "earlier_repair_blocked_or_failed",
        "unknown_repair",
    ]

    def test_blocked_lock_no_english_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            lockfile = tmp_path / GUARD_FILENAME
            lockfile.write_text("12345", encoding="utf-8")
            fresh_time = time.time()
            os.utime(str(lockfile), (fresh_time, fresh_time))

            seams = RepairSeams(now=lambda: fresh_time)
            report, _ = run_repairs(config, output_dir, None, ["remove-stale-lock"], seams=seams)

            russian = _render_russian_report(report)
            for token in self._ENGLISH_TOKENS:
                self.assertNotIn(token, russian, f"English token '{token}' leaked into Russian output")

            # Must contain exactly one question line.
            lines = [ln for ln in russian.splitlines() if ln.strip().endswith("?")]
            self.assertEqual(len(lines), 1, f"Expected exactly 1 question line: {russian}")

    def test_blocked_health_no_english_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")
            # No run-summary.json → no_run_date.
            seams = RepairSeams()
            report, _ = run_repairs(config, output_dir, None, ["rerun-health"], seams=seams)

            russian = _render_russian_report(report)
            for token in self._ENGLISH_TOKENS:
                self.assertNotIn(token, russian, f"English token '{token}' leaked into Russian output")

    def test_blocked_review_no_english_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")
            _make_valid_canonical_artifacts(output_dir)
            (output_dir / "scouting-candidates.json").unlink()  # missing_scouting_candidates

            seams = RepairSeams(health_runner=_fake_health_runner_factory(rc=0))
            report, _ = run_repairs(config, output_dir, None, ["rerender-review"], seams=seams)

            russian = _render_russian_report(report)
            for token in self._ENGLISH_TOKENS:
                self.assertNotIn(token, russian, f"English token '{token}' leaked into Russian output")

    def test_remove_stale_lock_success_no_guard_filename(self):
        """A successful remove-stale-lock Russian line must NOT name .auto-scout-workflow.lock."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            lockfile = tmp_path / GUARD_FILENAME
            lockfile.write_text("99999", encoding="utf-8")
            old_mtime = time.time() - GUARD_STALE_SECONDS - 100
            os.utime(str(lockfile), (old_mtime, old_mtime))

            seams = RepairSeams(
                now=lambda: time.time(),
                pid_probe=lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()),
                doctor_seams=_make_doctor_seams_ok(),
            )
            report, _ = run_repairs(config, output_dir, None, ["remove-stale-lock"], seams=seams)

            russian = _render_russian_report(report)
            self.assertNotIn(
                GUARD_FILENAME, russian, f"Guard filename '{GUARD_FILENAME}' must not appear in Russian output"
            )
            self.assertIn("выполнено", russian)

    def test_russian_has_no_equals_diagnostic_lines(self):
        """Russian output must not contain 'key=value' diagnostic lines."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")
            _make_valid_canonical_artifacts(output_dir)

            seams = RepairSeams(health_runner=_fake_health_runner_factory(rc=0))
            report, _ = run_repairs(config, output_dir, None, ["rerun-health"], seams=seams)

            russian = _render_russian_report(report)
            for line in russian.splitlines():
                if "=" in line and not line.startswith("✓") and not line.startswith("✗") and not line.startswith("—"):
                    self.fail(f"Diagnostic-style line in Russian output: {line}")


# ── Finding 2: No absolute paths in JSON ──────────────────────────────────


class Finding2NoPathsInJsonTests(unittest.TestCase):
    """Prove the JSON report never contains absolute filesystem paths."""

    def test_open_review_json_no_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")
            _make_valid_canonical_artifacts(output_dir)
            _make_review_html(output_dir)

            seams = RepairSeams(
                health_runner=_fake_health_runner_factory(rc=0),
                review_opener=_fake_review_opener("ok"),
            )
            report, _ = run_repairs(config, output_dir, None, ["open-review"], seams=seams)

            json_str = _render_json_report(report)
            _assert_no_tmp_path(json_str, str(tmp_path))
            parsed = json.loads(json_str)
            detail = parsed["repairs"][0]["detail"]
            self.assertNotIn("path", detail, "opener path must not leak into JSON")
            self.assertNotIn("opener_result", detail, "raw opener_result must not leak")

    def test_rerun_health_failure_json_no_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")
            _make_valid_canonical_artifacts(output_dir)

            def _failing_with_paths(od, ed):
                return 1, (
                    f"run_health_status=incomplete\n"
                    f"missing_artifact=pilot-status.json\n"
                    f"inspect_dir={od}\n"
                    f"artifact=pilot-status.json:{od}/pilot-status.json\n"
                    f"shareable_artifact=weekly-review.md:{od}/weekly-review.md\n"
                )

            seams = RepairSeams(health_runner=_failing_with_paths)
            report, _ = run_repairs(config, output_dir, None, ["rerun-health"], seams=seams)

            json_str = _render_json_report(report)
            _assert_no_tmp_path(json_str, str(tmp_path))
            parsed = json.loads(json_str)
            sanitized = parsed["repairs"][0]["detail"]["sanitized_reasons"]
            for line in sanitized:
                self.assertFalse(
                    line.startswith("inspect_dir=")
                    or line.startswith("artifact=")
                    or line.startswith("shareable_artifact="),
                    f"Path-bearing line in sanitized_reasons: {line}",
                )
                self.assertNotIn(str(output_dir), line, f"output_dir path in sanitized_reasons: {line}")

    def test_rerender_review_failure_json_no_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")
            _make_valid_canonical_artifacts(output_dir)

            def _failing_render(od, sd, ed):
                return (
                    1,
                    f"error=run_date_mismatch\neffective_date=2026-01-01\nReview rendered to {od}/weekly-review.md\n",
                )

            def _ok_health(od, ed):
                return 0, "run_health_status=ready\nclassification=ready\nready_for_editor=true\n"

            seams = RepairSeams(
                health_runner=_ok_health,
                review_renderer=_failing_render,
            )
            report, _ = run_repairs(config, output_dir, None, ["rerender-review"], seams=seams)

            json_str = _render_json_report(report)
            _assert_no_tmp_path(json_str, str(tmp_path))
            parsed = json.loads(json_str)
            sanitized = parsed["repairs"][0]["detail"]["sanitized_output"]
            for line in sanitized:
                self.assertNotIn(str(output_dir), line, f"output_dir path in sanitized_output: {line}")


# ── Finding 3: Blocked repair + healthy post-diagnosis produces no contradictory output ──


class Finding3BlockedOutputNoContradictionTests(unittest.TestCase):
    """Prove blocked repairs suppress contradictory post-diagnosis verdict."""

    def test_blocked_health_suppresses_healthy_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")
            # Empty dir — no_run_date → blocked, post-diagnosis is HEALTHY (no run).
            seams = RepairSeams()
            report, _ = run_repairs(config, output_dir, None, ["rerun-health"], seams=seams)

            russian = _render_russian_report(report)
            # Must NOT contain the contradictory healthy post-diagnosis verdict.
            self.assertNotIn(
                "AutoScout в порядке", russian, "Blocked repair must not show healthy post-diagnosis verdict"
            )
            self.assertNotIn("ещё не запускался", russian, "Blocked repair must not show 'never ran' verdict")
            # Must contain exactly one question.
            self.assertEqual(russian.count("?"), 1, f"Expected 1 question: {russian}")
            # Must contain the report-level recommendation.
            json_parsed = json.loads(_render_json_report(report))
            self.assertTrue(json_parsed.get("recommended_action"), "Report must have recommended_action when blocked")
            self.assertTrue(json_parsed.get("editor_question"), "Report must have editor_question when blocked")

    def test_blocked_lock_suppresses_post_diagnosis_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")

            lockfile = tmp_path / GUARD_FILENAME
            lockfile.write_text("12345", encoding="utf-8")
            fresh_time = time.time()
            os.utime(str(lockfile), (fresh_time, fresh_time))

            seams = RepairSeams(now=lambda: fresh_time)
            report, _ = run_repairs(config, output_dir, None, ["remove-stale-lock"], seams=seams)

            russian = _render_russian_report(report)
            # Lock-blocked should not show any post-diagnosis verdict text.
            self.assertNotIn("AutoScout в порядке", russian)
            self.assertNotIn("ещё не запускался", russian)
            self.assertNotIn("Запуск поиска заблокирован", russian, "Repository-blocker post-diagnosis must not appear")
            self.assertEqual(russian.count("?"), 1)

    def test_all_successful_shows_post_diagnosis(self):
        """When all repairs succeed, post-diagnosis IS shown."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)
            config = tmp_path / "config.json"
            config.write_text(json.dumps({"run_date": "2026-01-01", "sources": []}), encoding="utf-8")
            output_dir = _make_output_dir(tmp_path / "output")
            _make_valid_canonical_artifacts(output_dir)

            seams = RepairSeams(
                health_runner=_fake_health_runner_factory(rc=0),
                doctor_seams=_make_doctor_seams_ok(),
            )
            report, _ = run_repairs(config, output_dir, None, ["rerun-health"], seams=seams)

            russian = _render_russian_report(report)
            self.assertIn("AutoScout", russian)  # Post-diagnosis verdict appears

            json_parsed = json.loads(_render_json_report(report))
            # Post-diagnosis is always in JSON.
            self.assertIn("post_diagnosis", json_parsed)
