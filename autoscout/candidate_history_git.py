"""Read earlier versions of Candidate History from Git history (issue #929).

This is the one place that reads past versions of Candidate History as Git
holds them, for the decision-corpus export. Current state is read through the
State Store (ADR 0023); a past version only exists in Git, so it is read here
with ``git log`` and the layout's reader, never through the working tree.

Both committed layouts are covered: the single JSON file, and one file per
book since #1073. Where the files live is known only to
``autoscout.state_store.layout``, which reads each version.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

from autoscout.state_store import StateStoreError
from autoscout.state_store.layout import committed_candidate_history
from autoscout.state_store.layout import committed_history_pathspecs


class GitHistoryError(RuntimeError):
    """Git could not answer a question about the repository or one of its commits."""


@dataclass(frozen=True)
class HistoryVersion:
    """Candidate History as one commit on the main line holds it."""

    commit: str
    # The commit's author date, as the calendar date in the commit's own time zone.
    date: str
    # A merge commit brought the version in from another branch.
    is_merge: bool
    records: list[dict[str, Any]]


def _git(repo_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args], capture_output=True, text=True, encoding="utf-8", check=False
    )
    if completed.returncode != 0:
        raise GitHistoryError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout


def repository_root(path: Path) -> Path:
    """Return the root of the Git work tree that holds ``path``."""
    return Path(_git(path, "rev-parse", "--show-toplevel").strip())


def resolve_commit(repo_root: Path, ref: str) -> str:
    """Return the full commit ID ``ref`` names."""
    return _git(repo_root, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def commit_date(repo_root: Path, commit: str) -> str:
    """Return a commit's author date as the calendar date in its own time zone."""
    return _git(repo_root, "show", "-s", "--format=%as", commit).strip()


def history_versions(repo_root: Path, commit: str, state_dir: PurePosixPath) -> Iterator[HistoryVersion]:
    """Yield every version of Candidate History on ``commit``'s main line, oldest first.

    The main line is the first-parent chain, so each version follows the one
    before it, as the canonical state did. Only commits that changed
    Candidate History are yielded, in either layout it was committed under.
    A commit that holds no Candidate History (before it was first written, or
    after it was deleted) is skipped. Each version is read and parsed once.
    """
    state = state_dir.as_posix()
    log = _git(
        repo_root,
        "log",
        "--first-parent",
        "--reverse",
        "--format=%H %P|%as",
        commit,
        "--",
        *committed_history_pathspecs(state),
    )
    for line in log.splitlines():
        if not line.strip():
            continue
        shas, date = line.split("|", 1)
        ids = shas.split()
        try:
            records = committed_candidate_history(repo_root, ids[0], state)
        except StateStoreError as error:
            raise GitHistoryError(str(error)) from error
        if records is None:
            continue
        if not isinstance(records, list):
            raise GitHistoryError(f"Candidate History at {ids[0]} is not a list of records")
        yield HistoryVersion(ids[0], date.strip(), len(ids) > 2, [r for r in records if isinstance(r, dict)])
