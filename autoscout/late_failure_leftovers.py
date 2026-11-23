"""The leftovers of a weekly run that failed late, carried into the next run (#1161).

A run that fails after it saved its state leaves the run directory changed:
its reports, its artifacts and the State Store's files. The next run's
preflight, under the run lock, proves that the directory holds the leftovers
of one failed earlier run (``prove_leftovers``); any doubt leaves the tree as
it is, and the preflight blocks as before.

Nothing is restored, removed or moved. The run carries the leftovers: it
overwrites what it regenerates, and its finalization publishes the rest,
which is what the failed run would have published. The State Store's
database under SQLite has moved on with the failed run, and durable records
such as the source lists, the row history and the portal ledger must never
roll back, so carrying is the only change that loses nothing. A file a
person dropped into the run directory is carried and committed too, never
deleted.

Once proven, the directory is awaiting publication: one small marker under
the ignored ``data/`` folder names it and the failed operation. While the
marker exists, the dirty paths inside that directory that a run may carry
are allowed by the preflight and the update, so a start that then fails
early (the code gate, the update, a temporary blocker elsewhere) or restarts
on updated code does not block the next one; the marker does not depend on
the run log, which a new start overwrites. Only a finalization that
published the directory removes it. A dirty path outside the directory, a
staged change or a deletion the run never makes still blocks.

This module runs ``git`` only through the runner it is given, so it never
imports the envelope.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from autoscout.state_store.layout import candidate_id_of_shard
from autoscout.state_store.marker import BACKEND_MARKER_FILE

# Runs ``git`` in a repository: ``runner(repo, *args)``.
GitRunner = Callable[..., subprocess.CompletedProcess[str]]

# The workflow log stage recording that a run carries a late failure's leftovers.
LATE_FAILURE_LEFTOVERS_STAGE = "late-failure-leftovers"
# Where the marker of a directory awaiting publication is kept; Git ignores it.
AWAITING_PUBLICATION_DIR = Path("data") / "late-failure-awaiting-publication"

_MODIFIED = " M"
_DELETED = " D"
_UNTRACKED = "??"
_CARRIED_CODES = frozenset({_MODIFIED, _DELETED, _UNTRACKED})
# The tracked files a run itself deletes from its run directory: a book's
# Candidate History file (the State Store removes it when a book goes) and the
# Search Optimization Review, which the pipeline removes when the review is off
# or fails. Any other deletion is not the run's and blocks.
_RUN_DELETED_NAMES = frozenset({"search-optimization-review.json", "search-optimization-review.md"})
# The entries that prove the failed run started from a tree its preflight
# judged clean, and that it saved its candidate state.
_PREFLIGHT_STAGE = "repository-preflight"
_STATE_SAVED_STAGE = "pipeline"
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

# How the State Store stands behind the files of the state directory: under
# SQLite the database does when its export is current, or only behind (the
# files are what its last export wrote, and the run's export brings them up to
# date); on the JSON backend the files are the state. Anything else, or a look
# that cannot be made, blocks.
STATE_VOUCHED_BY_DATABASE = "database"
STATE_KEPT_IN_FILES = "files"
STATE_UNVERIFIED = "unverified"


@dataclass(frozen=True)
class LeftoverScope:
    """What the preflight needs to recognise and carry a late failure's leftovers.

    ``output_dir`` holds the run log, ``state_dir`` the State Store's files
    (the output directory for a run without one); only a run whose two are
    one directory qualifies. ``state_verdict`` is one of the ``STATE_*``
    values. The leftovers may be the run's own operation's: a later start on
    the same date carries them on the same terms.
    """

    output_dir: Path
    state_dir: Path
    state_verdict: Callable[[], str]


@dataclass(frozen=True)
class Leftovers:
    """A late failure's leftovers, proven or awaiting publication.

    ``carried`` is every dirty path the run carries, relative to the
    repository root; ``log`` the run log among them, empty when the directory
    was already awaiting publication.
    """

    operation_id: str
    earlier_operation_date: str
    carried: tuple[str, ...]
    log: str = ""

    def summary(self) -> dict[str, Any]:
        """The earlier run's date and a count only: the log and the reports name no path."""
        return {"earlier_operation_date": self.earlier_operation_date, "carried": len(self.carried)}


# ── The scope of a run ─────────────────────────────────────────────────────


def leftover_scope(config_path: Path | None, output_dir: Path, state_dir: Path | None) -> LeftoverScope:
    """The scope of the run about to start; its State Store is looked at once, and only when asked."""
    verdicts: list[str] = []

    def state_verdict() -> str:
        if not verdicts:
            verdicts.append(leftover_state_verdict(config_path, state_dir))
        return verdicts[0]

    return LeftoverScope(output_dir, output_dir if state_dir is None else state_dir, state_verdict)


def leftover_state_verdict(config_path: Path | None, state_dir: Path | None) -> str:
    """How the State Store stands behind the files it keeps in *state_dir*, one of the ``STATE_*`` values.

    Read-only, as the doctor's look is: the backend is the one that serves the
    directory. A run without a state directory keeps its collections as JSON
    beside its report.
    """
    from autoscout.state_store import EXPORT_BEHIND
    from autoscout.state_store import EXPORT_CURRENT
    from autoscout.state_store import JSON_BACKEND
    from autoscout.state_store import SQLITE_BACKEND
    from autoscout.state_store import StateStoreError
    from autoscout.state_store import inspect_state_store
    from autoscout.state_store import select_state_backend_from_file

    if state_dir is None:
        return STATE_KEPT_IN_FILES
    try:
        inspection = inspect_state_store(state_dir, select_state_backend_from_file(config_path))
    except (StateStoreError, OSError, ValueError):
        return STATE_UNVERIFIED
    if inspection.problem:
        return STATE_UNVERIFIED
    if inspection.backend == JSON_BACKEND:
        return STATE_KEPT_IN_FILES
    if inspection.backend == SQLITE_BACKEND and inspection.export_status in (EXPORT_CURRENT, EXPORT_BEHIND):
        return STATE_VOUCHED_BY_DATABASE
    return STATE_UNVERIFIED


def _run_directory(repo: Path, scope: LeftoverScope | None) -> Path | None:
    """The scope's run directory, resolved, when it is one directory strictly inside *repo*."""
    if scope is None:
        return None
    try:
        output_dir, state_dir = scope.output_dir.resolve(), scope.state_dir.resolve()
    except (OSError, RuntimeError):
        return None
    if output_dir != state_dir or repo not in output_dir.parents:
        return None
    return output_dir


def _state_stands_behind(scope: LeftoverScope) -> bool:
    try:
        return scope.state_verdict() in (STATE_VOUCHED_BY_DATABASE, STATE_KEPT_IN_FILES)
    except Exception:
        return False


def _carriable(repo: Path, code: str, rel_file: str, run_dir: Path) -> bool:
    """Whether a dirty entry is one the run may carry: an unstaged change, a run's own deletion or a new file inside *run_dir*."""
    path = repo / rel_file
    if code not in _CARRIED_CODES or path.is_symlink():
        return False
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return False
    if run_dir not in resolved.parents or resolved == run_dir / BACKEND_MARKER_FILE:
        return False
    if code == _DELETED:
        relative = resolved.relative_to(run_dir)
        return candidate_id_of_shard(relative) is not None or relative.as_posix() in _RUN_DELETED_NAMES
    return True


# ── The proof ──────────────────────────────────────────────────────────────


def prove_leftovers(
    repo: Path, run_log_path: Path | None, scope: LeftoverScope | None, entries: list[tuple[str, str]], git: GitRunner
) -> Leftovers | None:
    """The leftovers the dirty *entries* are, when they are provably those of a run that failed late.

    Every condition must hold, or ``None`` and the tree blocks as before:

    - the output directory is the state directory, strictly inside the
      repository;
    - each entry is an unstaged change, an untracked file, or an unstaged
      deletion of a file the run itself deletes, inside that directory; no
      symbolic link, and not the state directory's backend marker;
    - the run log is among them, changed or new, and ends with entries
      ``HEAD`` does not hold, of one operation, that show a run which passed
      its repository preflight and its pipeline and never published its end
      (``_failed_run``); that operation may be the one about to run;
    - the State Store stands behind its files: under SQLite its export is
      current or behind.

    Modification times prove nothing and are not looked at. The checkout and
    the run lock are the caller's to check first.
    """
    run_dir = _run_directory(repo, scope)
    if scope is None or run_dir is None or run_log_path is None or run_log_path.parent != run_dir or not entries:
        return None
    if not all(_carriable(repo, code, rel_file, run_dir) for code, rel_file in entries):
        return None
    log_entry = next(
        ((code, rel_file) for code, rel_file in entries if (repo / rel_file).resolve() == run_log_path), None
    )
    if log_entry is None or log_entry[0] == _DELETED:
        return None
    failed = _failed_run(repo, log_entry, git)
    if failed is None or not _state_stands_behind(scope):
        return None
    operation, day = failed
    return Leftovers(operation, day, tuple(rel_file for _, rel_file in entries), log_entry[1])


def _failed_run(repo: Path, log_entry: tuple[str, str], git: GitRunner) -> tuple[str, str] | None:
    """``(operation, date)`` of the run whose unpublished entries end the working-tree log, when it failed late.

    The two logs are compared entry by entry; nothing else is trusted:

    - the working-tree log is entries ``HEAD``'s run log holds (any of them,
      such as an earlier run of the same date whose terminal a same-operation
      retry dropped), followed by entries it does not hold, at least one;
    - those unpublished entries all carry one operation id, have readable
      times, and include a successful repository preflight and a successful
      pipeline, so that run started from a tree judged clean and saved its
      candidate state;
    - every terminal ``HEAD`` holds for that operation is older than the
      earliest of those preflights, so what ``HEAD`` published is an earlier
      run's end, never this one's.

    ``HEAD`` may lack a run log only when the working-tree one is new. The
    operation may be the one about to run: a start later the same date. The
    date is the operation's, from a start entry, or empty.
    """
    code, log_rel = log_entry
    try:
        log = json.loads((repo / log_rel).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    head = _head_log(repo, code, log_rel, git)
    if head is None or not isinstance(log, list) or not log or not all(isinstance(entry, dict) for entry in log):
        return None
    published = {_canonical(entry) for entry in head}
    first = len(log)
    while first > 0 and _canonical(log[first - 1]) not in published:
        first -= 1
    if first == len(log) or any(_canonical(entry) not in published for entry in log[:first]):
        return None
    unpublished = log[first:]
    operation = unpublished[-1].get("operation_id")
    if not isinstance(operation, str) or not operation:
        return None
    if any(entry.get("operation_id") != operation for entry in unpublished):
        return None
    times = [_parse_time(entry.get("at")) for entry in unpublished]
    if any(moment is None for moment in times):
        return None
    starts = [
        moment
        for entry, moment in zip(unpublished, times, strict=True)
        if entry.get("stage") == _PREFLIGHT_STAGE and entry.get("status") == "ok" and moment is not None
    ]
    saved = any(entry.get("stage") == _STATE_SAVED_STAGE and entry.get("status") == "ok" for entry in unpublished)
    if not starts or not saved:
        return None
    for entry in head:
        if isinstance(entry, dict) and entry.get("operation_id") == operation and entry.get("stage") == "terminal":
            ended = _parse_time(entry.get("at"))
            if ended is None or ended >= min(starts):
                return None
    day = next(
        (
            entry["detail"].get("operation_date")
            for entry in reversed(log)
            if entry.get("operation_id") == operation
            and entry.get("stage") == "operation-start"
            and isinstance(entry.get("detail"), dict)
        ),
        None,
    )
    return operation, day if isinstance(day, str) and _DATE.fullmatch(day) else ""


def _head_log(repo: Path, code: str, log_rel: str, git: GitRunner) -> list[Any] | None:
    """``HEAD``'s run log; empty when ``HEAD`` has none and the working-tree log is new; ``None`` when unreadable."""
    spec = f"HEAD:{Path(log_rel).as_posix()}"
    if code == _UNTRACKED:
        absent = git(repo, "cat-file", "-e", spec).returncode != 0
        head_exists = git(repo, "rev-parse", "--verify", "--quiet", "HEAD").returncode == 0
        return [] if absent and head_exists else None
    committed = git(repo, "show", spec)
    if committed.returncode != 0:
        return None
    try:
        log = json.loads(committed.stdout)
    except ValueError:
        return None
    return log if isinstance(log, list) else None


def _canonical(entry: Any) -> str:
    """One log entry as comparable text: the same entry in both logs gives the same text."""
    return json.dumps(entry, sort_keys=True, ensure_ascii=False)


def _parse_time(value: Any) -> float | None:
    """A log entry's ``at`` as seconds since the epoch; ``None`` unless it is an ISO time with its zone."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return None if moment.tzinfo is None else moment.timestamp()


# ── Awaiting publication ───────────────────────────────────────────────────


def _marker_path(repo: Path, run_dir: Path) -> Path:
    key = hashlib.sha256(run_dir.relative_to(repo).as_posix().encode("utf-8")).hexdigest()[:16]
    return repo / AWAITING_PUBLICATION_DIR / f"{key}.json"


def awaiting_publication(repo: Path, scope: LeftoverScope | None) -> Leftovers | None:
    """The leftovers the scope's run directory awaits publication of, from its marker; ``None`` without one."""
    run_dir = _run_directory(repo, scope)
    if run_dir is None:
        return None
    try:
        marker = json.loads(_marker_path(repo, run_dir).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(marker, dict) or marker.get("run_directory") != run_dir.relative_to(repo).as_posix():
        return None
    day = marker.get("earlier_operation_date")
    return Leftovers(
        str(marker.get("operation_id", "")), day if isinstance(day, str) and _DATE.fullmatch(day) else "", ()
    )


def mark_awaiting_publication(repo: Path, scope: LeftoverScope, leftovers: Leftovers) -> bool:
    """Write the marker of the scope's run directory, atomically; ``False`` when it cannot be written."""
    run_dir = _run_directory(repo, scope)
    if run_dir is None:
        return False
    path = _marker_path(repo, run_dir)
    marker = {
        "run_directory": run_dir.relative_to(repo).as_posix(),
        "operation_id": leftovers.operation_id,
        "earlier_operation_date": leftovers.earlier_operation_date,
        "marked_at": datetime.now(UTC).isoformat(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp") as tmp:
            tmp.write(json.dumps(marker, indent=2) + "\n")
        os.replace(tmp.name, path)
    except OSError:
        return False
    return True


def clear_awaiting_publication(repo: Path, output_dir: Path) -> None:
    """Remove the marker of *output_dir*, which a finalization has just published."""
    try:
        run_dir = output_dir.resolve()
        if repo in run_dir.parents:
            _marker_path(repo, run_dir).unlink(missing_ok=True)
    except (OSError, RuntimeError):
        pass


def carried_allowlist(repo: Path, scope: LeftoverScope | None, entries: list[tuple[str, str]]) -> set[Path]:
    """The resolved dirty paths the run carries while its directory awaits publication; empty otherwise.

    Only entries a run may carry count (``_carriable``), and only while the
    State Store still stands behind its files; a staged change, a path
    outside the directory or a deletion the run never makes is not allowed.
    """
    run_dir = _run_directory(repo, scope)
    if scope is None or run_dir is None or awaiting_publication(repo, scope) is None:
        return set()
    carried = [rel_file for code, rel_file in entries if _carriable(repo, code, rel_file, run_dir)]
    if not carried or not _state_stands_behind(scope):
        return set()
    return {(repo / rel_file).resolve() for rel_file in carried}
