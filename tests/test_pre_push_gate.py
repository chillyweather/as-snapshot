"""The pre-push hook and the pass record ./scripts/check leaves for it.

The hook lets a task branch reach the remote only when the gate passed on the
tree of the pushed commit. These tests drive the real hook and the real
launcher in throwaway repositories; the gate's steps are replaced by a stub
interpreter, because what is under test is the record and the push decision.
"""

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PASS_RECORD = "autoscout-check-pass"

STUB_PYTHON = (
    "#!/bin/sh\n"
    'if [ "$1" = "-c" ]; then\n'
    '  case "$2" in\n'
    '    *autoscout.__file__*) printf "%s\\n" "$MOCK_REPO_ROOT";;\n'
    '    *sysconfig.get_path*) printf "%s\\n" "$MOCK_REPO_ROOT/.venv/bin/autoscout";;\n'
    "  esac\n"
    "fi\n"
    'if [ -n "$MOCK_GATE_LOG" ]; then echo run >> "$MOCK_GATE_LOG"; fi\n'
    'exit "${MOCK_GATE_EXIT:-0}"\n'
)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=check)


def _env(repo: Path, **extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return {**env, "MOCK_REPO_ROOT": str(repo), **extra}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository on a task branch with the real gate and hook, and a bare remote."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / ".githooks").mkdir()
    for relative in ("scripts/check", ".githooks/pre-push"):
        target = repo / relative
        target.write_bytes((REPO_ROOT / relative).read_bytes())
        target.chmod(0o755)
    (repo / "AGENTS.md").write_text("rules\n", encoding="utf-8")
    (repo / ".gitignore").write_text("/.venv/\n", encoding="utf-8")
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    for name in ("python", "autoscout"):
        (venv_bin / name).write_text(STUB_PYTHON, encoding="utf-8")
        (venv_bin / name).chmod(0o755)

    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@test.test")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "core.hooksPath", ".githooks")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "start")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True, check=True)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "checkout", "-b", "task/example")
    return repo


def _run_gate(repo: Path, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(repo / "scripts" / "check"), *args],
        cwd=repo,
        env=_env(repo, **extra),
        capture_output=True,
        text=True,
        check=False,
    )


def _push(repo: Path, branch: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "push", "origin", branch],
        cwd=repo,
        env=_env(repo, **extra),
        capture_output=True,
        text=True,
        check=False,
    )


def _record(repo: Path) -> str | None:
    path = repo / ".git" / PASS_RECORD
    return path.read_text(encoding="utf-8").strip() if path.exists() else None


def _head_tree(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()


def _remote_has(repo: Path, branch: str) -> bool:
    return bool(_git(repo, "ls-remote", "origin", f"refs/heads/{branch}").stdout.strip())


def _gate_runs(log: Path) -> int:
    return len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0


def test_passing_gate_records_the_tree_it_checked(repo: Path) -> None:
    result = _run_gate(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _record(repo) == _head_tree(repo)


def test_record_differs_from_the_commit_when_an_uncommitted_file_was_checked(repo: Path) -> None:
    (repo / "not-committed.py").write_text("x = 1\n", encoding="utf-8")

    result = _run_gate(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _record(repo) not in (None, _head_tree(repo))


def test_failing_gate_records_nothing(repo: Path) -> None:
    result = _run_gate(repo, MOCK_GATE_EXIT="1")

    assert result.returncode == 1
    assert _record(repo) is None


def test_sqlite_only_run_records_nothing(repo: Path) -> None:
    result = _run_gate(repo, "--state-backend", "sqlite")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _record(repo) is None


def test_gate_leaves_the_index_and_working_tree_alone(repo: Path) -> None:
    (repo / "not-committed.py").write_text("x = 1\n", encoding="utf-8")
    before = _git(repo, "status", "--porcelain").stdout

    _run_gate(repo)

    assert _git(repo, "status", "--porcelain").stdout == before


def test_push_of_a_checked_commit_does_not_run_the_gate_again(repo: Path, tmp_path: Path) -> None:
    log = tmp_path / "gate.log"
    assert _run_gate(repo).returncode == 0

    result = _push(repo, "task/example", MOCK_GATE_LOG=str(log))

    assert result.returncode == 0, result.stderr
    assert _remote_has(repo, "task/example")
    assert _gate_runs(log) == 0


def test_push_of_an_unchecked_commit_runs_the_gate_first(repo: Path, tmp_path: Path) -> None:
    log = tmp_path / "gate.log"

    result = _push(repo, "task/example", MOCK_GATE_LOG=str(log))

    assert result.returncode == 0, result.stderr
    assert _remote_has(repo, "task/example")
    assert _gate_runs(log) > 0


def test_push_is_refused_when_the_gate_fails(repo: Path) -> None:
    result = _push(repo, "task/example", MOCK_GATE_EXIT="1")

    assert result.returncode != 0
    assert not _remote_has(repo, "task/example")


def test_push_is_refused_when_the_gate_passed_on_other_files(repo: Path) -> None:
    (repo / "not-committed.py").write_text("x = 1\n", encoding="utf-8")

    result = _push(repo, "task/example")

    assert result.returncode != 0
    assert not _remote_has(repo, "task/example")


def test_push_of_a_commit_that_is_not_checked_out_is_refused(repo: Path) -> None:
    _git(repo, "checkout", "-b", "task/other")
    (repo / "later.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "later")

    result = _push(repo, "task/example")

    assert result.returncode != 0
    assert not _remote_has(repo, "task/example")


def test_push_to_main_is_not_gated(repo: Path, tmp_path: Path) -> None:
    log = tmp_path / "gate.log"
    _git(repo, "checkout", "main")

    result = _push(repo, "main", MOCK_GATE_LOG=str(log), MOCK_GATE_EXIT="1")

    assert result.returncode == 0, result.stderr
    assert _remote_has(repo, "main")
    assert _gate_runs(log) == 0


def test_deleting_a_task_branch_is_not_gated(repo: Path, tmp_path: Path) -> None:
    log = tmp_path / "gate.log"
    assert _run_gate(repo).returncode == 0
    assert _push(repo, "task/example").returncode == 0

    result = subprocess.run(
        ["git", "push", "origin", "--delete", "task/example"],
        cwd=repo,
        env=_env(repo, MOCK_GATE_LOG=str(log), MOCK_GATE_EXIT="1"),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not _remote_has(repo, "task/example")
    assert _gate_runs(log) == 0
