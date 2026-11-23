"""Tests for issues #270 (blessed launcher) and #271 (Codex sandbox permission).

All launcher-behavior tests copy the actual ``scripts/run-weekly`` into a
temporary fake-repo directory and control the interpreter environment via
a fake ``.venv/bin/python`` or PATH manipulation. No network, Git, Codex,
or real ``.venv`` modification occurs.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

REAL_LAUNCHER = REPO_ROOT / "scripts" / "run-weekly"


# ── Helper: set up a temporary repo with a copy of the real launcher ───────


class _FakeRepo:
    """A temporary directory structured like a git repo with a launcher copy."""

    def __init__(self, tmp_root: Path) -> None:
        self.repo_root = tmp_root / "repo"
        self.scripts_dir = self.repo_root / "scripts"
        self.scripts_dir.mkdir(parents=True)

        self.launcher = self.scripts_dir / "run-weekly"
        self.launcher.write_text(REAL_LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8")
        self.launcher.chmod(REAL_LAUNCHER.stat().st_mode)

        # Minimal git repo identity so `_find_repo_root` resolves.
        (self.repo_root / ".git").mkdir()
        (self.repo_root / "configs").mkdir()

    def add_venv_python(self, content: str | None = None) -> Path:
        """Create a fake .venv/bin/python and return its path."""
        venv_bin = self.repo_root / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        fake_python = venv_bin / "python"
        fake_python.write_text(
            content
            or textwrap.dedent("""\
                #!/usr/bin/env bash
                echo "FAKE_PYTHON" "$@"
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


# ── Launcher behavior tests (exercising the real copied launcher) ──────────


class LauncherExistenceAndContentTests(unittest.TestCase):
    """Structural checks on the committed launcher file."""

    def test_launcher_exists_and_is_executable(self):
        self.assertTrue(REAL_LAUNCHER.is_file(), f"Launcher missing: {REAL_LAUNCHER}")
        mode = REAL_LAUNCHER.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR, "Launcher is not user-executable")

    def test_launcher_date_captured_exactly_once_and_passed(self):
        """Issue #280 behavioral regression: the real copied launcher must
        call date +%F exactly once and pass --run-date 2026-07-15 to the
        fake Python. Uses a controlled fake date that records invocations."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _FakeRepo(Path(tmp))

            # Controlled fake date that records invocation count.
            fake_bin = Path(tmp) / "fake-bin"
            fake_bin.mkdir()
            # Symlink the system bash.
            bash = "/bin/bash"
            (fake_bin / "bash").symlink_to(bash)
            for util in ("dirname", "pwd", "mkdir", "rm"):
                for prefix in ("/usr/bin", "/bin"):
                    src = Path(prefix) / util
                    if src.exists():
                        (fake_bin / util).symlink_to(src)
                        break

            # Fake date: records each invocation and outputs 2026-07-15.
            date_log = fake_bin / "date.log"
            fake_date = fake_bin / "date"
            fake_date.write_text(
                textwrap.dedent(f"""\
                    #!/usr/bin/env bash
                    echo "$@" >> {date_log}
                    echo "2026-07-15"
                """),
                encoding="utf-8",
            )
            fake_date.chmod(0o755)

            # Fake python3 that also records what it receives.
            fake_python3 = fake_bin / "python3"
            fake_python3.write_text(
                textwrap.dedent("""\
                    #!/usr/bin/env bash
                    echo "FAKE_PYTHON3_ARGS=$@"
                """),
                encoding="utf-8",
            )
            fake_python3.chmod(0o755)

            # Patch the launcher copy to use our fake-bin.
            lines = repo.launcher.read_text(encoding="utf-8").splitlines(keepends=True)
            new_lines = []
            for i, line in enumerate(lines):
                if i == 0:
                    new_lines.append(f"#!{fake_bin}/bash\n")
                elif line.startswith("set -euo pipefail"):
                    new_lines.append(line)
                    new_lines.append(f'export PATH="{fake_bin}"\n')
                else:
                    new_lines.append(line)
            repo.launcher.write_text("".join(new_lines), encoding="utf-8")

            # Invoke the copied launcher.
            result = subprocess.run(
                [str(repo.launcher)],
                capture_output=True,
                text=True,
                cwd=str(repo.repo_root),
                env={"PATH": str(fake_bin), "HOME": os.environ.get("HOME", "/tmp")},
            )
            self.assertEqual(result.returncode, 0, f"Launcher failed: {result.stderr}")
            # --run-date 2026-07-15 must be present in python args.
            self.assertIn("--run-date 2026-07-15", result.stdout, "Launcher did not pass --run-date 2026-07-15")
            # date must have been called exactly once.
            date_invocations = date_log.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(
                len(date_invocations), 1, f"Expected exactly 1 date invocation, got {len(date_invocations)}"
            )


class LauncherBehaviorTests(unittest.TestCase):
    """Behavior tests that copy the real launcher into a fake repo and
    control the interpreter environment."""

    def test_venv_python_preferred_and_receives_canonical_args(self):
        """The real launcher must exec the project-local .venv/bin/python
        with the canonical autoscout run-weekly arguments."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _FakeRepo(Path(tmp))
            repo.add_venv_python()

            result = repo.invoke_from(repo.repo_root)

            # The fake python echoes its args.
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("FAKE_PYTHON", result.stdout)
            self.assertIn("-m autoscout run-weekly", result.stdout)
            self.assertIn("--config", result.stdout)
            self.assertIn("current-weekly-pilot-live.json", result.stdout)
            # Issue #280: the launcher must pass --run-date.
            self.assertIn("--run-date", result.stdout, "Launcher must pass --run-date to the Python process")

    def test_cwd_is_repo_root_after_chdir(self):
        """When invoked from a deep outside directory, the launcher must
        change cwd to the repo root before exec so the Python process
        sees the correct working directory."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _FakeRepo(Path(tmp))
            repo.add_venv_python(
                textwrap.dedent("""\
                    #!/usr/bin/env bash
                    echo "CWD=$(pwd)"
                """)
            )

            # Invoke from a directory completely outside the repo.
            outside = Path(tmp) / "somewhere" / "else" / "entirely"
            outside.mkdir(parents=True)

            result = repo.invoke_from(outside)
            self.assertEqual(result.returncode, 0, result.stderr)
            # The fake python should report the repo root, not the outside cwd.
            actual_cwd = result.stdout.strip().removeprefix("CWD=")
            self.assertEqual(os.path.realpath(actual_cwd), os.path.realpath(str(repo.repo_root)))

    def test_cwd_is_repo_root_from_subdirectory(self):
        """Even when invoked from a subdirectory inside the repo, cwd is
        the repo root, not the subdirectory."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _FakeRepo(Path(tmp))
            repo.add_venv_python(
                textwrap.dedent("""\
                    #!/usr/bin/env bash
                    echo "CWD=$(pwd)"
                """)
            )

            deep_dir = repo.repo_root / "a" / "b" / "c"
            deep_dir.mkdir(parents=True)

            result = repo.invoke_from(deep_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            actual_cwd = result.stdout.strip().removeprefix("CWD=")
            self.assertEqual(os.path.realpath(actual_cwd), os.path.realpath(str(repo.repo_root)))

    def test_falls_back_to_python3_when_venv_missing(self):
        """When .venv/bin/python is not executable, the launcher should
        fall back to python3 on PATH with canonical args and cwd=repo root."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _FakeRepo(Path(tmp))
            # No venv python.

            # Create a fake python3 on a controlled PATH.
            fake_bin = Path(tmp) / "fake-bin"
            fake_bin.mkdir()
            bash = "/bin/bash"
            (fake_bin / "bash").symlink_to(bash)
            # Fake python3 records cwd and args.
            fake_python3 = fake_bin / "python3"
            fake_python3.write_text(
                textwrap.dedent("""\
                    #!/usr/bin/env bash
                    echo "FAKE_PYTHON3_CWD=$(pwd)"
                    echo "FAKE_PYTHON3_ARGS=$@"
                """),
                encoding="utf-8",
            )
            fake_python3.chmod(0o755)
            for util in ("dirname", "pwd", "date"):
                for prefix in ("/usr/bin", "/bin"):
                    src = Path(prefix) / util
                    if src.exists():
                        (fake_bin / util).symlink_to(src)
                        break

            # Patch the launcher copy to use our fake-bin.
            lines = repo.launcher.read_text(encoding="utf-8").splitlines(keepends=True)
            new_lines = []
            for i, line in enumerate(lines):
                if i == 0:
                    new_lines.append(f"#!{fake_bin}/bash\n")
                elif line.startswith("set -euo pipefail"):
                    new_lines.append(line)
                    new_lines.append(f'export PATH="{fake_bin}"\n')
                else:
                    new_lines.append(line)
            repo.launcher.write_text("".join(new_lines), encoding="utf-8")

            # Invoke from outside the repo root.
            outside = Path(tmp) / "somewhere" / "else"
            outside.mkdir(parents=True)

            result = subprocess.run(
                [str(repo.launcher)],
                capture_output=True,
                text=True,
                cwd=str(outside),
                env={"PATH": str(fake_bin), "HOME": os.environ.get("HOME", "/tmp")},
            )
            self.assertEqual(result.returncode, 0, f"Unexpected failure: {result.stderr}")
            self.assertNotIn("no usable Python", result.stderr)
            # Must have selected the fake python3.
            self.assertIn("FAKE_PYTHON3_CWD=", result.stdout)
            self.assertIn("FAKE_PYTHON3_ARGS=", result.stdout)
            self.assertIn("-m autoscout run-weekly", result.stdout)
            # Issue #280: must pass --run-date.
            self.assertIn("--run-date", result.stdout)
            # cwd must be repo root, not the outside directory.
            actual_cwd = result.stdout.strip().splitlines()[0].removeprefix("FAKE_PYTHON3_CWD=")
            self.assertEqual(os.path.realpath(actual_cwd), os.path.realpath(str(repo.repo_root)))

    def test_fails_clean_when_no_interpreter_found(self):
        """When neither .venv/bin/python nor python3 is available, the
        launcher must exit non-zero with a concise message."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _FakeRepo(Path(tmp))
            # No venv python.

            # Create a fake bin with only bash, no python.
            fake_bin = Path(tmp) / "fake-bin"
            fake_bin.mkdir()
            bash = "/bin/bash"
            (fake_bin / "bash").symlink_to(bash)
            for util in ("dirname", "pwd", "mkdir", "rm", "date"):
                for prefix in ("/usr/bin", "/bin"):
                    src = Path(prefix) / util
                    if src.exists():
                        (fake_bin / util).symlink_to(src)
                        break

            # Patch the launcher copy to use our fake-bin bash and PATH.
            lines = repo.launcher.read_text(encoding="utf-8").splitlines(keepends=True)
            new_lines = []
            for i, line in enumerate(lines):
                if i == 0:
                    # Replace shebang.
                    new_lines.append(f"#!{fake_bin}/bash\n")
                elif line.startswith("set -euo pipefail"):
                    new_lines.append(line)
                    new_lines.append(f'export PATH="{fake_bin}"\n')
                else:
                    new_lines.append(line)
            repo.launcher.write_text("".join(new_lines), encoding="utf-8")

            result = subprocess.run(
                [str(repo.launcher)],
                capture_output=True,
                text=True,
                cwd=str(repo.repo_root),
                env={"PATH": str(fake_bin), "HOME": os.environ.get("HOME", "/tmp")},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no usable Python", result.stderr)
