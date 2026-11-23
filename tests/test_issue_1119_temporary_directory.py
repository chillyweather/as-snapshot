"""Nothing the suite or a repeated run writes piles up in the temporary directory (#1119).

Lock files are named after the paths they guard and stay in the system's
temporary directory after release, by design: removing one on release would let
a waiting process lock a file that is no longer the lock. That is safe only
while the guarded paths are the same from run to run, as they are for the
scheduled run. Tests guard new paths every time, so each pytest process works
in a temporary directory of its own that is removed when it ends
(``tests/conftest.py``).
"""

from __future__ import annotations

import errno
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from autoscout.safe_artifacts import bundle_write_lock
from autoscout.safe_artifacts import pair_write_lock
from autoscout.weekly_report import WeeklyReportStore

REPO_ROOT = Path(__file__).resolve().parents[1]


def _lock_files(directory: Path) -> set[Path]:
    return {path.relative_to(directory) for path in directory.rglob("*.lock")}


def test_the_process_works_in_a_temporary_directory_of_its_own() -> None:
    own = Path(tempfile.gettempdir())

    assert own.name.startswith("autoscout-pytest-")
    assert own != Path(os.environ["PYTEST_DEBUG_TEMPROOT"]).resolve()
    assert os.environ["TMPDIR"] == str(own)


def test_a_subprocess_uses_the_same_temporary_directory() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import tempfile; print(tempfile.gettempdir())"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == tempfile.gettempdir()


@pytest.mark.parametrize(
    "lock",
    [
        lambda paths: bundle_write_lock(*paths),
        lambda paths: pair_write_lock(*paths),
        lambda paths: WeeklyReportStore(paths[0].parent).transaction("test"),
    ],
    ids=["bundle", "pair", "weekly-report"],
)
def test_taking_the_same_lock_again_adds_no_lock_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock) -> None:
    locks = tmp_path / "locks"
    locks.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(locks))
    paths = (tmp_path / "out" / "a.json", tmp_path / "out" / "b.json")
    paths[0].parent.mkdir()

    with lock(paths):
        pass
    first = _lock_files(locks)
    with lock(paths):
        pass

    assert len(first) == 1
    assert _lock_files(locks) == first


def test_a_repeated_run_on_the_same_output_directory_adds_no_lock_file(tmp_path: Path) -> None:
    """The scheduled run guards the same paths every night, so its lock files do not grow."""
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    output = tmp_path / "output"
    env = {**os.environ, "TMPDIR": str(temporary)}

    def run_and_render() -> None:
        for command in (
            ["run-mvp", "--config", "configs/weekly-mvp-fixture.json", "--output-dir", str(output)],
            ["render-review", "--output-dir", str(output)],
        ):
            subprocess.run(
                [sys.executable, "-m", "autoscout", *command], cwd=REPO_ROOT, env=env, capture_output=True, check=True
            )

    run_and_render()
    first = _lock_files(temporary)
    run_and_render()

    assert first
    assert _lock_files(temporary) == first


_LEFTOVER_SUITE = """
import stat
import tempfile
from pathlib import Path

from autoscout.safe_artifacts import bundle_write_lock


def test_leaves_files_in_the_temporary_directory():
    root = Path(tempfile.mkdtemp())
    with bundle_write_lock(root / "a.json"):
        pass
    object_file = root / "object"
    object_file.write_text("x")
    object_file.chmod(stat.S_IRUSR)
    locked = root / "locked"
    (locked / "inner").mkdir(parents=True)
    (locked / "inner" / "file").write_text("x")
    locked.chmod(0)
"""


@pytest.mark.parametrize("workers", [[], ["-n", "2"]], ids=["serial", "xdist"])
def test_a_suite_leaves_nothing_in_the_temporary_directory(tmp_path: Path, workers: list[str]) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "conftest.py").write_text((REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8"))
    (suite / "test_leftovers.py").write_text(_LEFTOVER_SUITE, encoding="utf-8")
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    env = {name: value for name, value in os.environ.items() if not name.startswith("PYTEST_")}
    env["TMPDIR"] = str(temporary)

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(suite), *workers, str(suite)],
        cwd=suite,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "could not remove" not in result.stdout
    assert [path.name for path in temporary.iterdir() if not path.name.startswith("pytest-of-")] == []


def test_the_cleanup_raises_a_persistent_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from conftest import remove_tree

    root = tmp_path / "own"
    busy = root / "busy"
    (busy / "inner").mkdir(parents=True)
    real_rmdir = os.rmdir

    def rmdir(path, *args, **kwargs):
        if os.fspath(path) in {str(busy), "busy"}:
            raise OSError(errno.EBUSY, "busy", str(busy))
        return real_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "rmdir", rmdir)

    with pytest.raises(OSError) as raised:
        remove_tree(root)

    assert raised.value.errno == errno.EBUSY


def test_the_cleanup_never_repairs_the_run_directory_or_its_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import remove_tree

    parent = tmp_path / "parent"
    root = parent / "own"
    (root / "child").mkdir(parents=True)
    parent.chmod(0o755)
    root.chmod(0o750)
    real_rmdir = os.rmdir

    def rmdir(path, *args, **kwargs):
        if os.fspath(path) == str(root):
            raise PermissionError(errno.EACCES, "denied", str(root))
        return real_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "rmdir", rmdir)

    with pytest.raises(PermissionError):
        remove_tree(root)

    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(root.stat().st_mode) == 0o750


def test_the_cleanup_removes_a_locked_down_directory(tmp_path: Path) -> None:
    from conftest import remove_tree

    root = tmp_path / "own"
    locked = root / "locked"
    (locked / "inner").mkdir(parents=True)
    (locked / "inner" / "file").write_text("x")
    locked.chmod(0)
    root.parent.chmod(0o755)

    remove_tree(root)

    assert not root.exists()
    assert stat.S_IMODE(root.parent.stat().st_mode) == 0o755
