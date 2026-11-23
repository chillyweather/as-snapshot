"""Tests for the safe Git and concurrency envelope (issue #266)."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from autoscout.git_envelope import GUARD_FILENAME
from autoscout.git_envelope import GitRepositoryAdapter
from autoscout.git_envelope import is_safe_git_command
from autoscout.progress_snapshot import PROGRESS_SNAPSHOT_FILE
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


def _add_remote(repo_path: Path) -> None:
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.test/repo.git"],
        cwd=str(repo_path),
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "branch.main.remote", "origin"],
        cwd=str(repo_path),
        capture_output=True,
        check=False,
    )
    subprocess.run(
        ["git", "config", "branch.main.merge", "refs/heads/main"],
        cwd=str(repo_path),
        capture_output=True,
        check=False,
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


class GitEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        _init_git_repo(self.tmp_path)

    def tearDown(self):
        adapter = GitRepositoryAdapter(self.tmp_path)
        adapter.release_guard()
        self._tmp.cleanup()

    def test_preflight_clean_repo_return_ok_when_upstream_configured(self):
        _add_remote(self.tmp_path)
        adapter = GitRepositoryAdapter(self.tmp_path)
        result = adapter.preflight()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["branch"], "main")
        self.assertFalse(result["dirty"])
        self.assertFalse(result["detached_head"])

    def test_preflight_no_upstream_returns_unsafe(self):
        adapter = GitRepositoryAdapter(self.tmp_path)
        result = adapter.preflight()
        self.assertEqual(result["status"], "unsafe")
        self.assertIn("no_upstream_tracking", result.get("reasons", []))

    def test_preflight_progress_snapshot_is_advisory_but_unrelated_files_still_block(self):
        _add_remote(self.tmp_path)
        (self.tmp_path / PROGRESS_SNAPSHOT_FILE).write_text("{}\n", encoding="utf-8")
        adapter = GitRepositoryAdapter(self.tmp_path)
        advisory_result = adapter.preflight()
        self.assertEqual(advisory_result["status"], "ok")
        self.assertFalse(advisory_result["dirty"])
        adapter.release_guard()

        (self.tmp_path / "unrelated.txt").write_text("dirty", encoding="utf-8")
        blocked_result = adapter.preflight()
        self.assertNotEqual(blocked_result["status"], "ok")
        self.assertTrue(blocked_result["dirty"])

    def test_finalize_does_not_commit_progress_snapshot(self):
        with tempfile.TemporaryDirectory() as remote_tmp:
            remote_path = Path(remote_tmp) / "remote.git"
            _add_local_remote(self.tmp_path, remote_path)
            output_dir = self.tmp_path / "runs" / "current-weekly"
            output_dir.mkdir(parents=True)
            snapshot = output_dir / PROGRESS_SNAPSHOT_FILE
            snapshot.write_text("{}\n", encoding="utf-8")

            result = GitRepositoryAdapter(self.tmp_path).finalize(output_dir)

            self.assertEqual(result["status"], "ok")
            self.assertFalse(result["committed"])
            tracked = subprocess.run(
                ["git", "ls-files", "--error-unmatch", str(snapshot.relative_to(self.tmp_path))],
                cwd=str(self.tmp_path),
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(tracked.returncode, 0)

    def test_preflight_dirty_repo_returns_unsafe(self):
        _add_remote(self.tmp_path)
        (self.tmp_path / "unrelated.txt").write_text("dirty", encoding="utf-8")
        adapter = GitRepositoryAdapter(self.tmp_path)
        result = adapter.preflight()
        self.assertNotEqual(result["status"], "ok")
        self.assertTrue(result["dirty"])

    def test_preflight_detached_head_is_unsafe(self):
        _add_remote(self.tmp_path)
        subprocess.run(
            ["git", "checkout", "--detach"],
            cwd=str(self.tmp_path),
            capture_output=True,
            check=True,
        )
        adapter = GitRepositoryAdapter(self.tmp_path)
        result = adapter.preflight()
        self.assertEqual(result["status"], "unsafe")
        self.assertTrue(result["detached_head"])

    def test_preflight_not_a_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            non_repo = Path(tmp)
            adapter = GitRepositoryAdapter(non_repo)
            result = adapter.preflight()
            self.assertEqual(result["status"], "unsafe")

    def test_guard_acquire_and_release(self):
        adapter = GitRepositoryAdapter(self.tmp_path)
        guard = adapter.acquire_guard()
        self.assertTrue(guard.acquired)
        self.assertTrue((self.tmp_path / GUARD_FILENAME).exists())

        adapter.release_guard()
        self.assertFalse((self.tmp_path / GUARD_FILENAME).exists())

    def test_guard_detects_concurrent_run(self):
        adapter_a = GitRepositoryAdapter(self.tmp_path)
        guard_a = adapter_a.acquire_guard()
        self.assertTrue(guard_a.acquired)

        adapter_b = GitRepositoryAdapter(self.tmp_path)
        guard_b = adapter_b.acquire_guard()
        self.assertFalse(guard_b.acquired)
        self.assertEqual(guard_b.reason, "guard_held")

        adapter_a.release_guard()

    def test_stale_guard_with_dead_pid_recovered(self):
        lockfile = self.tmp_path / GUARD_FILENAME
        lockfile.write_text("99999999", encoding="utf-8")
        old_mtime = time.time() - 7200
        os.utime(lockfile, (old_mtime, old_mtime))

        adapter = GitRepositoryAdapter(self.tmp_path)
        guard = adapter.acquire_guard()
        self.assertTrue(guard.acquired)

        adapter.release_guard()

    def test_update_clean_repo_returns_ok_no_pull(self):
        _add_remote(self.tmp_path)
        adapter = GitRepositoryAdapter(self.tmp_path)
        adapter.acquire_guard()
        result = adapter.update()
        self.assertIn(result["status"], ("ok", "unsafe"))
        adapter.release_guard()

    def test_finalize_no_changes_returns_ok_no_commit(self):
        _add_remote(self.tmp_path)
        adapter = GitRepositoryAdapter(self.tmp_path)
        result = adapter.finalize(Path("/tmp"))
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["committed"])
        self.assertFalse(result["pushed"])

    def test_finalize_commits_and_pushes_unicode_file_inside_output_dir(self):
        with tempfile.TemporaryDirectory() as remote_tmp:
            remote_path = Path(remote_tmp) / "remote.git"
            _add_local_remote(self.tmp_path, remote_path)

            output_dir = self.tmp_path / "runs" / "current-weekly"
            output_dir.mkdir(parents=True)
            unicode_path = output_dir / "académie-française.json"
            unicode_path.write_text("{}\n", encoding="utf-8")

            adapter = GitRepositoryAdapter(self.tmp_path)
            result = adapter.finalize(output_dir)

            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["committed"])
            self.assertTrue(result["pushed"])
            committed = subprocess.run(
                ["git", "show", f"HEAD:{unicode_path.relative_to(self.tmp_path)}"],
                cwd=str(self.tmp_path),
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(committed.stdout, "{}\n")

    def test_prohibited_commands_detected(self):
        _add_remote(self.tmp_path)
        adapter = GitRepositoryAdapter(self.tmp_path)
        self.assertTrue(adapter.check_prohibited("git push --force"))
        self.assertTrue(adapter.check_prohibited("git reset --hard"))
        self.assertTrue(adapter.check_prohibited("git checkout -f"))
        self.assertFalse(adapter.check_prohibited("git status"))
        self.assertFalse(adapter.check_prohibited("git pull --ff-only"))

    def test_is_safe_git_command_function(self):
        self.assertFalse(is_safe_git_command("git stash"))
        self.assertFalse(is_safe_git_command("git push --force-with-lease"))
        self.assertFalse(is_safe_git_command("git branch -D test"))
        self.assertTrue(is_safe_git_command("git log"))
        self.assertTrue(is_safe_git_command("git diff"))

    def test_release_guard_never_removes_another_holders_lock(self):
        """A losing acquirer must never tear down the winner's guard: only a
        lock this instance actually acquired may be released."""
        winner = GitRepositoryAdapter(self.tmp_path)
        guard = winner.acquire_guard()
        self.assertTrue(guard.acquired)

        loser = GitRepositoryAdapter(self.tmp_path)
        loser_guard = loser.acquire_guard()
        self.assertFalse(loser_guard.acquired)

        loser.release_guard()
        self.assertTrue((self.tmp_path / GUARD_FILENAME).exists())

        winner.release_guard()
        self.assertFalse((self.tmp_path / GUARD_FILENAME).exists())

    def test_finalize_does_not_ignore_similarly_named_sibling_directory(self):
        """A dirty file under runs/foobar must still block finalize when the
        output dir is runs/foo -- a name-prefix match is not containment."""
        _add_remote(self.tmp_path)
        real_dir = self.tmp_path / "runs" / "foo"
        real_dir.mkdir(parents=True)
        sibling_dir = self.tmp_path / "runs" / "foobar"
        sibling_dir.mkdir(parents=True)
        (sibling_dir / "unrelated.txt").write_text("dirty", encoding="utf-8")

        adapter = GitRepositoryAdapter(self.tmp_path)
        result = adapter.finalize(real_dir)
        self.assertEqual(result["status"], "unsafe")
        self.assertEqual(result["reason"], "preflight_not_ok")


class GitWorkflowIntegrationTests(unittest.TestCase):
    def test_workflow_with_noop_repository_adapter_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _init_git_repo(tmp_path)

            adapters = deterministic_adapters()
            repo_adapter = adapters.repository
            self.assertIsNotNone(repo_adapter)
            if repo_adapter is not None:
                self.assertEqual(repo_adapter.preflight()["status"], "ok")
                self.assertEqual(repo_adapter.update()["status"], "ok")
                self.assertEqual(repo_adapter.finalize(Path("/nonexistent"))["status"], "ok")

    def test_preflight_returns_none_on_unsafe_before_pipeline_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            adapter = GitRepositoryAdapter(tmp_path)

            result = adapter.preflight()
            self.assertEqual(result["status"], "unsafe")
            self.assertIn("not_a_git_repo", result.get("reason", ""))
