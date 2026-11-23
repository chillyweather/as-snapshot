"""Safe Git and concurrency envelope for the weekly workflow.

Provides a production repository adapter that wraps the weekly-workflow
seam in a non-destructive Git and concurrency envelope. Never performs
hard reset, force checkout, silent stash, force push, state deletion,
or automatic conflict resolution.

One restore is allowed, and only under the run lock: what a run that failed
early leaves behind is its residue, and nothing else is (#1057). That is an
unstaged change to the tracked workflow log of the output directory, alone
or with an unstaged change to the same directory's run summary that only
adds or changes its top-level ``toolchain_preflight`` key (a Stage 0
failure writes both). A staged file is somebody's deliberate act and blocks. The
preflight copies it to an ignored folder first and then restores that one
file, so the residue never blocks the next run and is never lost.

A run that failed late leaves more: its reports and artifacts, and the State
Store's files (#1161). Those are never restored: once the preflight proves
they are that run's (``autoscout.late_failure_leftovers``), it copies the run
log aside and marks the run directory as awaiting publication, and the run
carries the leftovers and publishes them. Neither step happens unless the
checkout's root, remote, branch and Git operation are as expected.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import functools
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from autoscout.late_failure_leftovers import Leftovers
from autoscout.late_failure_leftovers import LeftoverScope
from autoscout.late_failure_leftovers import awaiting_publication
from autoscout.late_failure_leftovers import carried_allowlist
from autoscout.late_failure_leftovers import clear_awaiting_publication
from autoscout.late_failure_leftovers import mark_awaiting_publication
from autoscout.late_failure_leftovers import prove_leftovers
from autoscout.progress_snapshot import PROGRESS_SNAPSHOT_FILE
from autoscout.state_store.layout import is_candidate_history_path

GUARD_FILENAME = ".auto-scout-workflow.lock"
# How many paths one staging call of finalization takes.
_ADD_BATCH = 200
GUARD_STALE_SECONDS = 3600

# Where a set-aside workflow log is kept (#1057). The folder is ignored by
# Git, so its copies never make the working tree dirty.
LOG_RESIDUE_DIR = Path("data") / "workflow-log-residue"
LOG_RESIDUE_KEEP = 30
# A Stage 0 failure records its probe in the run summary under this key.
RUN_SUMMARY_NAME = "run-summary.json"
TOOLCHAIN_SUMMARY_KEY = "toolchain_preflight"
# The prefix each set-aside file gets in the residue folder; one set shares a stamp.
_RESIDUE_PREFIXES = {"workflow-log.json": "workflow-log", RUN_SUMMARY_NAME: "run-summary"}
# Porcelain status of a tracked file modified in the working tree only. A
# run writes its log and never stages it; a staged log ("M ", "MM") has index
# content that a restore would discard unarchived, so it blocks.
_UNSTAGED_MODIFICATION_CODE = " M"


# Canonical repository envelope. Diagnostics must validate against the same
# expected branch and remote as the weekly workflow, otherwise a clean tree
# pointing at the wrong remote would be diagnosed as safe.
CANONICAL_BRANCH = "main"
CANONICAL_REMOTE_URL = "https://github.com/chillyweather/auto-scout.git"


def resolve_repo_root(start: Path) -> Path | None:
    """Walk up from ``start`` to the nearest directory containing ``.git``.

    Accepts a ``.git`` file for worktrees as well as a directory. Returns
    ``None`` when no repository root is found; never writes or runs Git.

    Like Git, the walk never climbs into a directory named in
    ``GIT_CEILING_DIRECTORIES``, so this function and a ``git`` command run
    from ``start`` agree on whether a repository is there (#1099). ``start``
    itself is always examined.
    """
    current = start.resolve()
    ceilings = _ceiling_directories()
    for parent in (current, *current.parents):
        if parent != current and parent in ceilings:
            return None
        if (parent / ".git").exists():
            return parent
    return None


GIT_CEILING_DIRECTORIES_ENV = "GIT_CEILING_DIRECTORIES"


def _ceiling_directories() -> frozenset[Path]:
    """The absolute directories ``GIT_CEILING_DIRECTORIES`` names, resolved."""
    entries = os.environ.get(GIT_CEILING_DIRECTORIES_ENV, "").split(os.pathsep)
    return frozenset(Path(entry).resolve() for entry in entries if os.path.isabs(entry))


def process_is_alive(pid: int, probe: Callable[[int, int], None] | None = None) -> bool | None:
    """Whether *pid* names a live process: ``True``, ``False`` when provably dead, ``None`` when unknown.

    ESRCH is dead; EPERM is a process under another user, so alive; any
    other error leaves ownership unknown. ``probe`` is a seam for tests.
    """
    try:
        (probe or os.kill)(pid, 0)
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            return True
        return None
    return True


def weekly_run_in_progress(
    state_dir: Path,
    *,
    pid_probe: Callable[[int, int], None] | None = None,
) -> bool:
    """Read the containing repository's guard; ambiguous ownership fails closed.

    A dead lock belongs to doctor repair, so leave it untouched. This check
    is not atomic with the mutation: a run can acquire its guard before it
    loads state while the mutation is still writing.
    """
    try:
        root = resolve_repo_root(state_dir)
        if root is None:
            return False
        facts = GitRepositoryAdapter(root).guard_status(pid_probe=pid_probe)
        if not facts["guard_exists"]:
            return False
        if int(facts.get("guard_pid", "")) == os.getpid():
            return False
        return facts.get("guard_pid_alive") is not False
    except (OSError, ValueError, OverflowError):
        # Includes unreadable/non-UTF-8 lock contents and unprobeable PIDs.
        return True


NOT_SAFE_COMMANDS = {
    "reset --hard",
    "checkout -f",
    "checkout --force",
    "stash",
    "push --force",
    "push -f",
    "push --force-with-lease",
    "branch -D",
    "branch --delete --force",
    "clean -f",
    "clean --force",
    "clean -fd",
    "clean -fdx",
}


@dataclass
class GuardResult:
    acquired: bool
    reason: str = ""
    lockfile: str = ""


@dataclass
class PreflightResult:
    status: str
    branch: str = ""
    upstream: str = ""
    behind_count: int = 0
    ahead_count: int = 0
    dirty: bool = False
    dirty_files: list[str] = field(default_factory=list)
    merge_in_progress: bool = False
    detached_head: bool = False
    reasons: list[str] = field(default_factory=list)


@dataclass
class UpdateResult:
    status: str
    pulled: bool = False
    updated: int = 0
    reason: str = ""


@dataclass
class FinalizeResult:
    status: str
    committed: bool = False
    pushed: bool = False
    commit_hash: str = ""
    reason: str = ""


# Time limits of one Git command (#1162), so a stalled command can never hold
# the run lock for ever. Fetch, pull and push talk to the remote and get the
# longer limit; every other command is local. Both are far above a healthy
# command on the run server: a status of the whole checkout takes a tenth of
# a second, and a push of tens of megabytes a few minutes on a slow uplink.
GIT_LOCAL_TIMEOUT_SECONDS = 600
GIT_NETWORK_TIMEOUT_SECONDS = 1200
_GIT_NETWORK_COMMANDS = frozenset({"fetch", "pull", "push"})
# A stopped command first gets SIGTERM, on which Git removes its lock files
# (index.lock, ref locks), then SIGKILL after this many seconds.
_GIT_TERMINATE_GRACE_SECONDS = 10
# No Git command of the workflow may wait for a person to type a credential.
# An empty GIT_ASKPASS also overrides core.askPass and SSH_ASKPASS, so Git
# goes straight to the disabled terminal prompt and fails; ssh never asks.
_GIT_NO_PROMPT_ENVIRONMENT = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "SSH_ASKPASS_REQUIRE": "never",
    "GCM_INTERACTIVE": "never",
}
# The reason a preflight, update or finalize reports for a command over its limit.
GIT_TIMED_OUT = "git_timed_out"


class GitTimedOut(Exception):
    """A Git command ran over its time limit; it and every process it started were stopped."""


def _git(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one Git command with its time limit and without any credential prompt.

    The command runs in its own process group, so on timeout the credential
    helper, ssh or git-remote-https it started are stopped with it; then
    :class:`GitTimedOut` is raised.
    """
    limit = GIT_NETWORK_TIMEOUT_SECONDS if args and args[0] in _GIT_NETWORK_COMMANDS else GIT_LOCAL_TIMEOUT_SECONDS
    with _SigtermRelay() as relay:
        proc = subprocess.Popen(
            ["git", *args],
            cwd=str(repo_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, **_GIT_NO_PROMPT_ENVIRONMENT},
            start_new_session=True,
        )
        deadline = time.monotonic() + limit
        try:
            while True:
                try:
                    stdout, stderr = proc.communicate(
                        timeout=max(0.0, min(_GIT_SIGTERM_POLL_SECONDS, deadline - time.monotonic()))
                    )
                    break
                except subprocess.TimeoutExpired:
                    if relay.received:
                        # The run got SIGTERM: the relay ends it once Git is stopped.
                        _stop_process_group(proc)
                        stdout, stderr = "", ""
                        break
                    if time.monotonic() >= deadline:
                        _stop_process_group(proc)
                        raise GitTimedOut(
                            f"git {args[0] if args else ''} ran over its {limit:g}-second limit"
                        ) from None
        except GitTimedOut:
            raise
        except BaseException:
            # Ctrl-C no longer reaches Git in its own session: stop it before
            # the run ends and its lock is free.
            _stop_process_group(proc)
            raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


# How often a running Git command checks whether the run got SIGTERM.
_GIT_SIGTERM_POLL_SECONDS = 1.0


class _SigtermRelay:
    """While a Git command runs, SIGTERM to the run stops the command first, then ends the run as SIGTERM would.

    Git runs in its own session, so a SIGTERM sent to the run would not reach
    it. The handler only records the signal; the command loop stops Git, and
    on leaving the relay the default action is restored and the signal raised
    again. Nothing is raised asynchronously, so neither the start nor the
    stopping of a command can be cut short. Only in the main thread, the one
    place a handler can be set, and only where SIGTERM has its default action.
    """

    def __init__(self) -> None:
        self.installed = False
        self.received = False

    def __enter__(self) -> _SigtermRelay:
        if threading.current_thread() is threading.main_thread() and signal.getsignal(signal.SIGTERM) is signal.SIG_DFL:
            signal.signal(signal.SIGTERM, self._record)
            self.installed = True
        return self

    def _record(self, signum: int, frame: Any) -> None:
        self.received = True

    def __exit__(self, *exc_info: object) -> None:
        if not self.installed:
            return
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        if self.received:
            signal.raise_signal(signal.SIGTERM)


def _stop_process_group(proc: subprocess.Popen[str]) -> None:
    """SIGTERM the command's process group, then SIGKILL what is left of it after the grace.

    The grace runs until every process of the group has exited, not only Git
    itself, so a child such as the merge of a pull can remove its lock file
    too. A process that SIGKILL cannot end either (stuck in the kernel) is
    left after one more grace rather than waited for without end; the caller
    reports the command as timed out, never as safe.
    """
    for sig in (signal.SIGTERM, signal.SIGKILL):
        _signal_group(proc.pid, sig)
        if _group_exited(proc, _GIT_TERMINATE_GRACE_SECONDS):
            break
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()


def _group_exited(proc: subprocess.Popen[str], within: float) -> bool:
    """Whether every process of *proc*'s group exits within *within* seconds; reaps Git itself."""
    deadline = time.monotonic() + within
    while True:
        proc.poll()
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except OSError:
        pass  # every process of the group has already exited


def _reports_git_timeout(method: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Turn a Git command over its limit into the method's unsafe result with reason ``git_timed_out``."""

    @functools.wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return method(*args, **kwargs)
        except GitTimedOut:
            return {"status": "unsafe", "reason": GIT_TIMED_OUT, "reasons": [GIT_TIMED_OUT]}

    return wrapper


def _git_lines(repo_path: Path, *args: str) -> list[str]:
    proc = _git(repo_path, *args)
    return proc.stdout.strip().splitlines() if proc.returncode == 0 else []


def _git_status_paths(repo_path: Path) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Return real Git status paths without C-style quoting; see ``_git_status_entries``."""
    proc, entries = _git_status_entries(repo_path)
    return proc, [path for _, path in entries]


def _git_status_entries(repo_path: Path) -> tuple[subprocess.CompletedProcess[str], list[tuple[str, str]]]:
    """Return ``(status code, path)`` pairs of Git status without C-style quoting.

    The normal line-oriented porcelain format quotes non-ASCII paths according
    to ``core.quotePath``.  Passing those quoted strings to ``Path`` makes a
    generated Unicode artifact look as though it lives outside its output
    directory.  Porcelain v1 with ``-z`` emits literal, NUL-delimited paths and
    is also safe for spaces, newlines, and rename/copy pairs.
    """
    proc = _git(repo_path, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if proc.returncode != 0:
        return proc, []

    paths: list[tuple[str, str]] = []
    entries = proc.stdout.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        if not entry:
            index += 1
            continue

        status = entry[:2]
        path = entry[3:]
        if path:
            paths.append((status, path))

        if "R" in status or "C" in status:
            index += 1
            if index < len(entries) and entries[index]:
                paths.append((status, entries[index]))

        index += 1

    return proc, paths


def _is_git_repo(repo_path: Path) -> bool:
    return (repo_path / ".git").is_dir()


def _path_within(repo_path: Path, rel_file: str, base_dir: Path) -> bool:
    """Return True only when rel_file resolves to a real path inside base_dir.

    A plain substring test (e.g. ``str(base_dir) in rel_file``) can also match
    an unrelated sibling directory that merely shares a name prefix (e.g.
    ``runs/foo`` matching ``runs/foobar``); resolving both paths and checking
    genuine containment avoids that.
    """
    try:
        resolved = (repo_path / rel_file).resolve()
    except (OSError, RuntimeError):
        return False
    return resolved == base_dir or base_dir in resolved.parents


def _resolved_path_is_allowlisted(repo_path: Path, rel_file: str, allowlist: set[Path]) -> bool:
    """Fail closed when a dirty path cannot be safely resolved."""
    try:
        return (repo_path / rel_file).resolve() in allowlist
    except (OSError, RuntimeError):
        return False


def _prune_residue_sets(archive_dir: Path) -> None:
    """Keep the newest LOG_RESIDUE_KEEP residue sets; a set is every file sharing one stamp."""
    by_stamp: dict[str, list[Path]] = {}
    for prefix in _RESIDUE_PREFIXES.values():
        for path in archive_dir.glob(f"{prefix}-*.json"):
            by_stamp.setdefault(path.stem[len(prefix) + 1 :], []).append(path)
    for stamp in sorted(by_stamp)[:-LOG_RESIDUE_KEEP]:
        for path in by_stamp[stamp]:
            try:
                path.unlink()
            except OSError:
                pass


class GitRepositoryAdapter:
    def __init__(
        self,
        repo_path: Path,
        remote: str = "origin",
        expected_branch: str | None = None,
        expected_remote_url: str | None = None,
        run_log_path: Path | None = None,
    ) -> None:
        self._repo_path = repo_path.resolve()
        self._remote = remote
        self._expected_branch = expected_branch
        self._expected_remote_url = expected_remote_url
        self._guard_acquired = False
        self._guard_lockfile = repo_path / GUARD_FILENAME
        self._run_log_path: Path | None = None
        self._run_summary_path: Path | None = None
        self.set_run_log_path(run_log_path)
        self._leftover_scope: LeftoverScope | None = None

    def set_run_log_path(self, run_log_path: Path | None) -> None:
        """Name the tracked workflow log whose unstaged change is an earlier run's residue (#1057).

        The run summary beside it is the one other file the residue may hold.
        """
        self._run_log_path = None if run_log_path is None else run_log_path.resolve()
        self._run_summary_path = None if self._run_log_path is None else self._run_log_path.parent / RUN_SUMMARY_NAME

    def set_leftover_scope(self, scope: LeftoverScope | None) -> None:
        """Name the operation about to run and the folders it owns, so a late failure's leftovers are known (#1161).

        Without a scope, or without the run log path, no leftovers are ever
        carried; only the #1057 residue is set aside.
        """
        self._leftover_scope = scope

    # -- Guard -----------------------------------------------------------

    def guard_status(
        self,
        *,
        pid_probe: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        """Read-only guard probe — never acquires, never removes.

        Liveness follows exactly the same rules as
        :meth:`remove_stale_guard`, so diagnosis and repair can never
        disagree about who owns a lock:

        - ESRCH → provably dead (``guard_pid_alive`` False)
        - EPERM → the process exists under another user, so ALIVE
        - malformed pid or any other OSError → ownership unknown, reported
          as ``guard_pid_alive`` None so callers treat it as ambiguity
          rather than as a dead lock.

        ``pid_probe`` is an injectable seam for tests, matching
        :meth:`remove_stale_guard`.
        """
        _probe = pid_probe or os.kill

        lockfile = self._guard_lockfile
        if not lockfile.exists():
            return {"guard_exists": False}
        try:
            stat = lockfile.stat()
            age = time.time() - stat.st_mtime
            pid_str = lockfile.read_text(encoding="utf-8").strip()
        except OSError:
            return {"guard_exists": True, "guard_error": "unreadable"}

        facts: dict[str, Any] = {
            "guard_exists": True,
            "guard_age_seconds": round(age, 1),
            "guard_pid": pid_str,
        }

        try:
            pid = int(pid_str)
            if not 0 < pid < 2**31:
                # Zero or below names a process group to os.kill, and a larger one no process (#1175).
                raise ValueError(pid_str)
        except ValueError:
            # Unparseable content: ownership cannot be established.
            facts["guard_pid_alive"] = None
            facts["guard_malformed"] = True
            return facts

        facts["guard_pid_alive"] = process_is_alive(pid, _probe)
        return facts

    def held_by_live_run(
        self,
        *,
        pid_probe: Callable[[int, int], None] | None = None,
    ) -> bool:
        """Whether another process that is verifiably alive holds the run lock.

        A scheduled run skips itself only on this (#1057). It reads the lock
        through :meth:`guard_status`, so ``EPERM`` (a process under another
        user) counts as alive. A dead PID, an unreadable or unparsable lock,
        or no lock at all is not a live run: the scheduled run then goes on
        and is refused by the preflight like a manual run, so the failure is
        reported instead of the night being skipped silently.
        """
        if self._guard_acquired:
            return False
        return self.guard_status(pid_probe=pid_probe).get("guard_pid_alive") is True

    def take_over_dead_guard(
        self,
        *,
        pid_probe: Callable[[int, int], None] | None = None,
    ) -> bool:
        """Remove the run lock when its owner is provably dead, whatever the lock's age (#1175).

        Only the night's second attempt calls it. Its unit starts after the
        first attempt's unit has ended, and systemd ends every process of a
        unit with it, so a lock whose PID no longer exists (ESRCH) is that
        attempt's, killed at its time limit or by the kernel; it must not cost
        the night an hour. A live owner (EPERM included), an unknown one, a
        malformed or unreadable lock, or no lock is left as it is, and the
        lock is then judged as for any scheduled start.
        """
        if self._guard_acquired:
            return False
        try:
            with self._reclaiming():
                # Judged under the reclaim lock, so the lock removed is the one judged dead.
                if self.guard_status(pid_probe=pid_probe).get("guard_pid_alive") is not False:
                    return False
                self._guard_lockfile.unlink()
        except OSError:
            return False
        return True

    @contextlib.contextmanager
    def _reclaiming(self) -> Any:
        """Hold the repository's reclaim lock: one process at a time judges and removes a dead owner's lock (#1175).

        Every path that removes a lock it did not write judges and removes it
        under this ``flock`` on the checkout's directory, so none can remove a
        lock another process wrote after the judgement. A new lock is only
        ever created where none exists, so it never needs this.
        """
        fd = os.open(str(self._repo_path), os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def acquire_guard(self) -> GuardResult:
        # A scheduled run takes the lock before anything else and then runs
        # the preflight, which takes it again; the holder keeps it (#1057).
        if self._guard_acquired and self._guard_lockfile.exists():
            return GuardResult(acquired=True, lockfile=str(self._guard_lockfile))
        try:
            with self._reclaiming():
                return self._acquire_guard_reclaiming()
        except OSError:
            return GuardResult(acquired=False, reason="guard_io_error", lockfile=str(self._guard_lockfile))

    def _acquire_guard_reclaiming(self) -> GuardResult:
        try:
            lockfile = self._guard_lockfile
            if lockfile.exists():
                try:
                    stale_age = time.time() - lockfile.stat().st_mtime
                    if stale_age > GUARD_STALE_SECONDS:
                        pid_str = lockfile.read_text(encoding="utf-8").strip()
                        try:
                            pid = int(pid_str)
                            os.kill(pid, 0)
                        except (OSError, ValueError):
                            lockfile.unlink()
                            return self._write_guard()
                        return GuardResult(
                            acquired=False,
                            reason="stale_guard_with_live_process",
                            lockfile=str(lockfile),
                        )
                    return GuardResult(
                        acquired=False,
                        reason="guard_held",
                        lockfile=str(lockfile),
                    )
                except OSError:
                    lockfile.unlink()
                    return self._write_guard()
            return self._write_guard()
        except OSError:
            return GuardResult(acquired=False, reason="guard_io_error", lockfile=str(self._guard_lockfile))

    def _write_guard(self) -> GuardResult:
        lockfile = self._guard_lockfile
        try:
            fd = os.open(str(lockfile), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return GuardResult(acquired=False, reason="guard_held", lockfile=str(lockfile))
        except OSError:
            return GuardResult(acquired=False, reason="guard_io_error", lockfile=str(lockfile))
        try:
            os.write(fd, str(os.getpid()).encode("utf-8"))
        finally:
            os.close(fd)
        self._guard_acquired = True
        return GuardResult(acquired=True, lockfile=str(lockfile))

    def remove_stale_guard(
        self,
        *,
        now: Callable[[], float] | None = None,
        pid_probe: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        """Remove the workflow guard lock ONLY when it is provably stale.

        Conditions (ALL must hold):
        - repo_root resolvable (method is on the adapter — always true)
        - lock exists
        - lock readable
        - age > GUARD_STALE_SECONDS
        - lock content parses as an integer pid
        - that pid is PROVABLY dead (os.kill(pid, 0) raises OSError with errno ESRCH)

        EPERM (process exists, other user) means ALIVE. Any other OSError
        = unknown → blocked. Live pid, young lock, malformed content,
        unreadable lock → blocked, lock untouched.
        Absent lock → already-complete.

        Injectable seams for tests: ``now`` (seconds since epoch) and
        ``pid_probe`` (takes pid and signal number, raises OSError if dead).
        """

        try:
            with self._reclaiming():
                return self._remove_stale_guard_reclaiming(now or time.time, pid_probe or os.kill)
        except OSError:
            return {"result": "blocked", "reason": "guard_unreadable"}

    def _remove_stale_guard_reclaiming(
        self, _now: Callable[[], float], _probe: Callable[[int, int], None]
    ) -> dict[str, Any]:
        lockfile = self._guard_lockfile
        if not lockfile.exists():
            return {"result": "absent", "reason": "guard_not_found"}

        # Readable?
        try:
            stat = lockfile.stat()
            age = _now() - stat.st_mtime
            pid_str = lockfile.read_text(encoding="utf-8").strip()
        except OSError:
            return {"result": "blocked", "reason": "guard_unreadable"}

        # Parse pid.
        try:
            pid = int(pid_str)
        except ValueError:
            return {"result": "blocked", "reason": "guard_malformed"}

        # Age check.
        if age <= GUARD_STALE_SECONDS:
            return {"result": "blocked", "reason": "guard_held_young"}

        # Pid provably dead?
        try:
            _probe(pid, 0)
        except PermissionError:
            # EPERM → process exists (other user).
            return {"result": "blocked", "reason": "guard_pid_alive"}
        except ProcessLookupError:
            # ESRCH → provably dead.
            pass
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                pass
            elif exc.errno == errno.EPERM:
                return {"result": "blocked", "reason": "guard_pid_alive"}
            else:
                return {"result": "blocked", "reason": "guard_pid_unknown"}

        # Remove the lock.
        try:
            lockfile.unlink()
        except OSError:
            return {"result": "blocked", "reason": "guard_remove_failed"}

        return {"result": "removed", "reason": "guard_removed_stale"}

    def release_guard(self) -> None:
        # Only remove a lock this instance actually acquired -- a lock left
        # behind by a *different* active process must never be unlinked here,
        # or a losing acquirer could tear down the winner's guard.
        if not self._guard_acquired:
            return
        try:
            self._guard_lockfile.unlink()
        except OSError:
            pass
        self._guard_acquired = False

    # -- Preflight -------------------------------------------------------

    @_reports_git_timeout
    def preflight(self, validated_artifact_allowlist: set[Path] | None = None) -> dict[str, Any]:
        if not _is_git_repo(self._repo_path):
            return {"status": "unsafe", "reason": "not_a_git_repo"}

        guard = self.acquire_guard()
        if not guard.acquired:
            return {"status": "unsafe", "reason": guard.reason}

        # The lock is held, so no other run owns the log any more: an earlier
        # run's residue is set aside before the tree is judged (#1057). Only
        # in the expected checkout: a tree on another branch or remote, or in
        # the middle of a merge, is refused below with nothing touched.
        expected = not self._checkout_reasons()[3]
        residue = self._run_residue(validated_artifact_allowlist) if expected else None
        if residue is not None and not self._set_aside_log_residue(residue):
            return {"status": "unsafe", "reason": "log_residue_not_set_aside"}
        # The leftovers of a run that failed late, proved before anything is
        # touched (#1161): the run log is copied aside and the run directory
        # marked as awaiting publication; nothing else changes.
        if expected and residue is None and awaiting_publication(self._repo_path, self._leftover_scope) is None:
            proven = self._run_leftovers()
            if proven is not None and not self._carry_leftovers(proven):
                return {"status": "unsafe", "reason": "late_failure_leftovers_not_recorded"}
        result = self._inspect(validated_artifact_allowlist=validated_artifact_allowlist)
        if residue is not None:
            result["log_residue_set_aside"] = True
            result["log_residue_files"] = sorted(Path(rel_file).name for rel_file in residue)
        return result

    @_reports_git_timeout
    def inspect_local(self, validated_artifact_allowlist: set[Path] | None = None) -> dict[str, Any]:
        """Genuinely local read-only inspection — no fetch, no guard acquisition."""
        return self._inspect(validated_artifact_allowlist=validated_artifact_allowlist, fetch=False)

    # -- Workflow log residue (#1057) --------------------------------------

    def _dirty_entries(
        self, validated_artifact_allowlist: set[Path] | None
    ) -> tuple[subprocess.CompletedProcess[str], list[tuple[str, str]]]:
        """Status entries that make the tree dirty: all but the lock, the snapshot and allowlisted paths."""
        status_out, entries = _git_status_entries(self._repo_path)
        return status_out, self._exempt(entries, validated_artifact_allowlist)

    def _exempt(
        self, entries: list[tuple[str, str]], validated_artifact_allowlist: set[Path] | None
    ) -> list[tuple[str, str]]:
        """*entries* without the lock, the snapshot, allowlisted paths and the paths a run carries."""
        entries = [
            (code, f)
            for code, f in entries
            if f != GUARD_FILENAME and f != f".git/{GUARD_FILENAME}" and Path(f).name != PROGRESS_SNAPSHOT_FILE
        ]
        if validated_artifact_allowlist is not None:
            # Issue #315: only exact resolved-path matches from a validated
            # current-operation worklist are exempt.  Unrelated files placed
            # inside the output directory still block.
            entries = [
                (code, f)
                for code, f in entries
                if not _resolved_path_is_allowlisted(self._repo_path, f, validated_artifact_allowlist)
            ]
        # A run directory awaiting publication of a late failure's leftovers
        # (#1161): the paths the run carries are allowed, like a validated
        # artifact allowlist; nothing outside the directory, staged or deleted
        # by anyone but the run is.
        carried = carried_allowlist(self._repo_path, self._leftover_scope, entries)
        if carried:
            entries = [
                (code, f) for code, f in entries if not _resolved_path_is_allowlisted(self._repo_path, f, carried)
            ]
        return entries

    def _residue_in(self, entries: list[tuple[str, str]]) -> list[str] | None:
        """The relative paths of an early-failed run's residue, when the dirty entries are exactly that.

        Every entry is an unstaged modification; one is the run log, and the
        only other one allowed is the run summary changed in its
        ``toolchain_preflight`` key alone.
        """
        if self._run_log_path is None or not 1 <= len(entries) <= 2:
            return None
        if any(code != _UNSTAGED_MODIFICATION_CODE for code, _ in entries):
            return None
        paths = [rel_file for _, rel_file in entries]
        try:
            resolved = [(self._repo_path / rel_file).resolve() for rel_file in paths]
        except (OSError, RuntimeError):
            return None
        if self._run_log_path not in resolved:
            return None
        if len(paths) == 2:
            summary = paths[1 - resolved.index(self._run_log_path)]
            if (self._repo_path / summary).resolve() != self._run_summary_path:
                return None
            if not self._summary_changed_only_in_toolchain(summary):
                return None
        return paths

    def committed_text(self, path: Path) -> str | None:
        """HEAD's copy of *path*, a file inside the repository; ``None`` when HEAD has none or it cannot be read.

        Read-only. The second attempt of a night (#1160) reads the committed
        run log with it: a terminal that is only in the working tree was never
        published.
        """
        try:
            rel_file = path.resolve().relative_to(self._repo_path)
        except (OSError, RuntimeError, ValueError):
            return None
        try:
            committed = _git(self._repo_path, "show", f"HEAD:{rel_file.as_posix()}")
        except GitTimedOut:
            # Unreadable, like a missing copy: the caller then treats the run as not published (#1162).
            return None
        return committed.stdout if committed.returncode == 0 else None

    def _summary_changed_only_in_toolchain(self, rel_file: str) -> bool:
        """Whether the working-tree summary equals HEAD's once the toolchain key is removed from both."""
        committed = _git(self._repo_path, "show", f"HEAD:{Path(rel_file).as_posix()}")
        if committed.returncode != 0:
            return False
        try:
            before = json.loads(committed.stdout)
            after = json.loads((self._repo_path / rel_file).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return False
        if not isinstance(before, dict) or not isinstance(after, dict):
            return False
        before.pop(TOOLCHAIN_SUMMARY_KEY, None)
        after.pop(TOOLCHAIN_SUMMARY_KEY, None)
        return before == after

    def _run_residue(self, validated_artifact_allowlist: set[Path] | None) -> list[str] | None:
        status_out, entries = self._dirty_entries(validated_artifact_allowlist)
        return self._residue_in(entries) if status_out.returncode == 0 else None

    def _set_aside_log_residue(self, rel_files: list[str]) -> bool:
        """Copy every residue file to the ignored residue folder, then restore them all to HEAD.

        The files of one set share a stamp. Every copy comes first, so a
        failed copy leaves the files untouched (and no partial set behind)
        and the run blocks as before. Only the newest LOG_RESIDUE_KEEP sets
        are kept.
        """
        archive_dir = self._repo_path / LOG_RESIDUE_DIR
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        copies: list[Path] = []
        try:
            archive_dir.mkdir(parents=True, exist_ok=True)
            for rel_file in rel_files:
                target = archive_dir / f"{_RESIDUE_PREFIXES[Path(rel_file).name]}-{stamp}.json"
                shutil.copy2(self._repo_path / rel_file, target)
                copies.append(target)
        except (OSError, KeyError):
            for copy in copies:
                copy.unlink(missing_ok=True)
            return False
        if _git(self._repo_path, "checkout", "HEAD", "--", *rel_files).returncode != 0:
            return False
        _prune_residue_sets(archive_dir)
        return True

    # -- The leftovers of a run that failed late (#1161) --------------------

    def _run_leftovers(self) -> Leftovers | None:
        """The leftovers of a run that failed late, proved over every dirty entry.

        A same-operation retry's validated allowlist is not applied: it hides
        the run log, which the proof needs, and the proof covers every path
        that allowlist would exempt anyway.
        """
        status_out, entries = self._dirty_entries(None)
        return self._prove_leftovers(entries) if status_out.returncode == 0 else None

    def _prove_leftovers(self, entries: list[tuple[str, str]]) -> Leftovers | None:
        return prove_leftovers(self._repo_path, self._run_log_path, self._leftover_scope, entries, _git)

    def _carry_leftovers(self, leftovers: Leftovers) -> bool:
        """Copy the failed run's log aside for diagnosis, then mark its directory as awaiting publication.

        The copy is the one #1057 makes of a residue log, kept and pruned the
        same way; the log itself stays, and the new run overwrites it.
        """
        archive_dir = self._repo_path / LOG_RESIDUE_DIR
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        try:
            archive_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(
                self._repo_path / leftovers.log, archive_dir / f"{_RESIDUE_PREFIXES['workflow-log.json']}-{stamp}.json"
            )
        except OSError:
            return False
        _prune_residue_sets(archive_dir)
        scope = self._leftover_scope
        return scope is not None and mark_awaiting_publication(self._repo_path, scope, leftovers)

    def carried_paths(self, output_dir: Path) -> list[str]:
        """The paths, relative to *output_dir*, the run carries while that directory awaits publication (#1161).

        The secret scan reads them before the finalization publishes them.
        """
        scope = self._leftover_scope
        if scope is None or awaiting_publication(self._repo_path, scope) is None:
            return []
        status_out, entries = _git_status_entries(self._repo_path)
        if status_out.returncode != 0:
            return []
        try:
            run_dir = output_dir.resolve()
        except (OSError, RuntimeError):
            return []
        carried = carried_allowlist(self._repo_path, scope, entries)
        return sorted(
            path.relative_to(run_dir).as_posix() for path in carried if run_dir in path.parents and path.is_file()
        )

    def _awaiting_summary(self, entries: list[tuple[str, str]]) -> dict[str, Any] | None:
        """The summary of the leftovers the run directory awaits publication of, with how many paths are carried."""
        marked = awaiting_publication(self._repo_path, self._leftover_scope)
        if marked is None:
            return None
        carried = carried_allowlist(self._repo_path, self._leftover_scope, entries)
        return {"earlier_operation_date": marked.earlier_operation_date, "carried": len(carried)}

    def _checkout_reasons(self) -> tuple[str, bool, bool, list[str]]:
        """``(branch, detached, merge in progress, reasons)``: the checkout's root, remote, branch and Git operation."""
        reasons: list[str] = []
        top_level = _git_lines(self._repo_path, "rev-parse", "--show-toplevel")
        if not top_level or Path(top_level[0]).resolve() != self._repo_path:
            reasons.append("unexpected_repository_root")
        if self._expected_remote_url is not None:
            remote_urls = _git_lines(self._repo_path, "remote", "get-url", self._remote)
            if not remote_urls or remote_urls[0].removesuffix(".git") != self._expected_remote_url.removesuffix(".git"):
                reasons.append("unexpected_remote_url")

        branch_lines = _git_lines(self._repo_path, "rev-parse", "--abbrev-ref", "HEAD")
        branch = branch_lines[0] if branch_lines else ""
        detached = branch == "HEAD"
        if detached:
            reasons.append("detached_head")
        elif self._expected_branch is not None and branch != self._expected_branch:
            reasons.append("unexpected_branch")

        merge_in_progress = (
            (self._repo_path / ".git" / "MERGE_HEAD").exists()
            or (self._repo_path / ".git" / "rebase-merge").exists()
            or (self._repo_path / ".git" / "rebase-apply").exists()
        )
        if merge_in_progress:
            reasons.append("merge_in_progress")
        return branch, detached, merge_in_progress, reasons

    def _inspect(
        self,
        validated_artifact_allowlist: set[Path] | None = None,
        *,
        fetch: bool = True,
    ) -> dict[str, Any]:
        if not _is_git_repo(self._repo_path):
            return {"status": "unsafe", "reason": "not_a_git_repo"}

        upstream = ""
        dirty = False
        dirty_files: list[str] = []
        branch, detached, merge_in_progress, reasons = self._checkout_reasons()

        if not detached and branch:
            remote = _git_lines(self._repo_path, "config", "--get", f"branch.{branch}.remote")
            upstream = remote[0] if remote else ""
            if not upstream:
                reasons.append("no_upstream_tracking")

        log_residue = False
        leftovers: dict[str, Any] | None = None
        status_out, entries = _git_status_entries(self._repo_path)
        dirty_entries = self._exempt(entries, validated_artifact_allowlist)
        if status_out.returncode == 0:
            # A lone unstaged run log is an earlier run's residue, which the
            # next preflight sets aside (#1057); it does not block on its own.
            # Nor do the leftovers of a run that failed late (#1161): once its
            # directory awaits publication they are allowed above, and before
            # that the proven ones are what the next preflight carries.
            log_residue = self._residue_in(dirty_entries) is not None
            leftovers = self._awaiting_summary(entries)
            proven = (
                None if log_residue or leftovers is not None else self._prove_leftovers(self._exempt(entries, None))
            )
            if proven is not None:
                leftovers = proven.summary()
            dirty_files = [] if log_residue or proven is not None else [f for _, f in dirty_entries]
            dirty = bool(dirty_files)
            if dirty:
                reasons.append("dirty_working_tree")

        behind_count = 0
        ahead_count = 0
        fetch_failed = False
        if upstream:
            if fetch:
                fetch_proc = _git(self._repo_path, "fetch", self._remote, branch)
                if fetch_proc.returncode != 0:
                    fetch_failed = True
                else:
                    behind = _git_lines(self._repo_path, "rev-list", "--count", f"HEAD..{self._remote}/{branch}")
                    ahead = _git_lines(self._repo_path, "rev-list", "--count", f"{self._remote}/{branch}..HEAD")
                    try:
                        behind_count = int(behind[0]) if behind else 0
                        ahead_count = int(ahead[0]) if ahead else 0
                    except (ValueError, IndexError):
                        pass
            else:
                # Local-only mode: compute ahead/behind from local remote-tracking
                # refs only — never fetch.
                behind = _git_lines(self._repo_path, "rev-list", "--count", f"HEAD..{self._remote}/{branch}")
                ahead = _git_lines(self._repo_path, "rev-list", "--count", f"{self._remote}/{branch}..HEAD")
                try:
                    behind_count = int(behind[0]) if behind else 0
                    ahead_count = int(ahead[0]) if ahead else 0
                except (ValueError, IndexError):
                    pass

        if reasons:
            return {
                "status": "unsafe",
                "branch": branch,
                "upstream": upstream,
                "detached_head": detached,
                "merge_in_progress": merge_in_progress,
                "dirty": dirty,
                "dirty_files": dirty_files,
                "behind_count": behind_count,
                "ahead_count": ahead_count,
                "fetch_failed": fetch_failed,
                "log_residue": log_residue,
                "late_failure_leftovers": leftovers,
                "reasons": reasons,
            }

        return {
            "status": "ok",
            "branch": branch,
            "upstream": upstream,
            "detached_head": detached,
            "merge_in_progress": merge_in_progress,
            "dirty": dirty,
            "dirty_files": dirty_files,
            "behind_count": behind_count,
            "ahead_count": ahead_count,
            "fetch_failed": fetch_failed,
            "log_residue": log_residue,
            "late_failure_leftovers": leftovers,
            "reasons": reasons,
        }

    # -- Update ----------------------------------------------------------

    @_reports_git_timeout
    def update(self, validated_artifact_allowlist: set[Path] | None = None) -> dict[str, Any]:
        preflight = self._inspect(validated_artifact_allowlist=validated_artifact_allowlist)
        if preflight["status"] != "ok":
            # Return a single safe reason token, never the raw list repr.
            reasons = preflight.get("reasons")
            safe_reason = str(reasons[0]) if isinstance(reasons, list) and reasons else "preflight_not_ok"
            return {"status": "unsafe", "reason": safe_reason, "reasons": reasons if isinstance(reasons, list) else []}
        if preflight.get("fetch_failed"):
            return {"status": "unsafe", "reason": "fetch_failed"}

        branch = str(preflight.get("branch", ""))
        if not branch or preflight.get("detached_head"):
            return {"status": "unsafe", "reason": "cannot_update_detached_head"}

        remote = _git_lines(self._repo_path, "config", "--get", f"branch.{branch}.remote")
        if not remote:
            return {"status": "unsafe", "reason": "no_upstream_remote"}

        behind = int(preflight.get("behind_count", 0))
        if behind == 0:
            return {"status": "ok", "pulled": False, "updated": 0}

        proc = _git(self._repo_path, "pull", "--ff-only", self._remote, branch)
        if proc.returncode != 0:
            return {"status": "unsafe", "reason": "pull_failed", "detail": proc.stderr.strip()}

        return {"status": "ok", "pulled": True, "updated": behind}

    # -- Finalize --------------------------------------------------------

    def _whole_dir_allowlist(self, output_dir: Path) -> set[Path] | None:
        """Build an allowlist of every dirty file under *output_dir*.

        Used by ``finalize`` to exempt all artifacts in the run directory
        from the dirty check — we are about to commit them."""
        status_out, dirty_files = _git_status_paths(self._repo_path)
        if status_out.returncode != 0:
            return None
        output_resolved = output_dir.resolve()
        return {
            (self._repo_path / f).resolve() for f in dirty_files if _path_within(self._repo_path, f, output_resolved)
        }

    @_reports_git_timeout
    def finalize(self, output_dir: Path) -> dict[str, Any]:
        # Finalization always ignores the output_dir — we are about to commit
        # those artifacts.  The whole-directory exemption is appropriate here
        # because the workflow has already validated the operation.
        preflight = self._inspect(validated_artifact_allowlist=self._whole_dir_allowlist(output_dir))
        if preflight["status"] != "ok":
            return {"status": "unsafe", "reason": "preflight_not_ok"}

        status_out, changed_files = _git_status_paths(self._repo_path)
        if status_out.returncode != 0:
            return {"status": "unsafe", "reason": "status_check_failed"}

        if not changed_files:
            # Nothing in the run directory awaits publication any more (#1161).
            clear_awaiting_publication(self._repo_path, output_dir)
            return {"status": "ok", "committed": False, "pushed": False}

        known_files = {
            "candidate-history.json",
            "excluded-candidate-history.json",
            "candidate-visibility.json",
            "source-proposals.json",
            "editorial-taste-profile.json",
        }
        output_dir_resolved = output_dir.resolve()
        # Candidate History is the single file or, one file per book, every
        # file under candidate-history/ (#1073); a removed book's file shows as
        # a deletion, which ``git add`` stages like any other change.
        relevant = [
            f
            for f in changed_files
            if Path(f).name != PROGRESS_SNAPSHOT_FILE
            and (
                Path(f).name in known_files
                or is_candidate_history_path(f)
                or _path_within(self._repo_path, f, output_dir_resolved)
            )
        ]

        if not relevant:
            # Nothing in the run directory awaits publication any more (#1161).
            clear_awaiting_publication(self._repo_path, output_dir)
            return {"status": "ok", "committed": False, "pushed": False}

        # A night can change thousands of book files; they are staged in
        # batches, and a batch that fails stops the commit, so a reported save
        # never leaves canonical changes behind. ``update-index --add
        # --remove`` stages a new, changed or deleted file, and accepts a path
        # whose deletion or rename is already staged, where ``git add`` would
        # refuse the whole batch. Status lists no ignored file, so none is added.
        for start in range(0, len(relevant), _ADD_BATCH):
            added = _git(
                self._repo_path, "update-index", "--add", "--remove", "--", *relevant[start : start + _ADD_BATCH]
            )
            if added.returncode != 0:
                return {"status": "unsafe", "reason": "stage_failed", "detail": added.stderr.strip()}

        commit_msg = "auto-scout: weekly workflow state update"
        proc = _git(self._repo_path, "commit", "-m", commit_msg)
        if proc.returncode != 0:
            return {"status": "unsafe", "reason": "commit_failed", "detail": proc.stderr.strip()}

        hash_lines = _git_lines(self._repo_path, "rev-parse", "HEAD")
        commit_hash = hash_lines[0] if hash_lines else ""

        branch = str(preflight.get("branch", ""))
        try:
            push_proc = _git(self._repo_path, "push", self._remote, branch)
        except GitTimedOut:
            return {
                "status": "push_failed",
                "committed": True,
                "pushed": False,
                "commit_hash": commit_hash,
                "reason": GIT_TIMED_OUT,
            }
        if push_proc.returncode != 0:
            return {
                "status": "push_failed",
                "committed": True,
                "pushed": False,
                "commit_hash": commit_hash,
                "reason": push_proc.stderr.strip(),
            }

        # The run directory is published, carried leftovers included (#1161).
        clear_awaiting_publication(self._repo_path, output_dir)
        return {
            "status": "ok",
            "committed": True,
            "pushed": True,
            "commit_hash": commit_hash,
        }

    def check_prohibited(self, command: str) -> bool:
        for unsafe in NOT_SAFE_COMMANDS:
            if unsafe in command:
                return True
        return False


def is_safe_git_command(command: str) -> bool:
    cmd_lower = command.strip().lower()
    for unsafe in NOT_SAFE_COMMANDS:
        if unsafe.lower() in cmd_lower:
            return False
    return True
