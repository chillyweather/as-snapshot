"""The repository lookup stops where Git's own lookup stops (#1099).

A stray ``.git`` in the system's temporary directory used to be taken for the
repository by every doctor test that built its files below it.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from autoscout.git_envelope import GIT_CEILING_DIRECTORIES_ENV
from autoscout.git_envelope import resolve_repo_root


def _stray_repository_with_work_below(tmp_path: Path) -> tuple[Path, Path]:
    stray = tmp_path / "stray"
    (stray / ".git").mkdir(parents=True)
    work = stray / "work" / "nested"
    work.mkdir(parents=True)
    return stray, work


def test_a_repository_in_a_ceiling_directory_is_not_found_from_below(tmp_path, monkeypatch):
    stray, work = _stray_repository_with_work_below(tmp_path)
    monkeypatch.setenv(GIT_CEILING_DIRECTORIES_ENV, str(stray))

    assert resolve_repo_root(work) is None


def test_without_a_ceiling_the_walk_reaches_the_repository_above(tmp_path, monkeypatch):
    stray, work = _stray_repository_with_work_below(tmp_path)
    monkeypatch.delenv(GIT_CEILING_DIRECTORIES_ENV)

    assert resolve_repo_root(work) == stray.resolve()


def test_a_repository_below_the_ceiling_is_still_found(tmp_path, monkeypatch):
    stray, work = _stray_repository_with_work_below(tmp_path)
    (work.parent / ".git").mkdir()
    monkeypatch.setenv(GIT_CEILING_DIRECTORIES_ENV, os.pathsep.join([str(tmp_path / "other"), str(stray)]))

    assert resolve_repo_root(work) == work.parent.resolve()


def test_the_start_directory_is_examined_even_when_it_is_a_ceiling(tmp_path, monkeypatch):
    stray, _ = _stray_repository_with_work_below(tmp_path)
    monkeypatch.setenv(GIT_CEILING_DIRECTORIES_ENV, str(stray))

    assert resolve_repo_root(stray) == stray.resolve()


def test_the_lookup_agrees_with_git_about_a_ceiling(tmp_path, monkeypatch):
    stray = tmp_path / "stray"
    stray.mkdir()
    subprocess.run(["git", "init", "--quiet", str(stray)], check=True)
    work = stray / "work"
    work.mkdir()
    monkeypatch.setenv(GIT_CEILING_DIRECTORIES_ENV, str(stray))

    git = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=work, capture_output=True, text=True)

    assert git.returncode != 0
    assert resolve_repo_root(work) is None


def test_every_test_runs_with_the_temporary_directory_as_a_ceiling():
    ceilings = os.environ[GIT_CEILING_DIRECTORIES_ENV].split(os.pathsep)

    assert str(Path(tempfile.gettempdir()).resolve()) in ceilings
