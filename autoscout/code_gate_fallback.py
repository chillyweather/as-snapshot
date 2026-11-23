"""A failed gate runs the last code that passed here (#1159).

When new code fails the run server's gate (#1124, #1130), the scheduled run
does not lose the night: it runs the code of the commit recorded in
``data/code-gate/passed.json`` against the production checkout's configs,
state and Git publication, when that can be shown safe, and stops as before
when it cannot.

The production checkout is never moved back (a ``main`` behind origin would
make the next state commit diverge). The earlier code runs from a snapshot
instead: ``git archive`` of the passing commit's ``autoscout/`` package into
``data/code-gate/last-passed/``, written afresh at each fallback. The run
starts again with the production ``.venv``'s Python (the one whose packages
were compared with the pins, whatever Python decided), the snapshot first on
``PYTHONPATH`` and ``PYTHONSAFEPATH`` set (so neither the working directory
nor the editable install puts the new package first, also in a child
``python -m autoscout``), and ``AUTOSCOUT_REPOSITORY_ROOT`` naming the
production checkout, so every module reads ``configs/``, ``data/`` and
``runs/`` there and the run commits and pushes state from there as always.

What has to hold, judged before the run starts again (any doubt stops the
night as before, with the gate's own reason):

- The gate failed on the new code itself or on its environment: the check
  failed or timed out, or the ``.venv`` could not be brought to its pins.
- A pass is recorded, and its commit's package has this mechanism (it
  declares ``FALLBACK_PROTOCOL``); a pass recorded before #1159 cannot run as
  fallback, because its modules read the checkout from their own location.
- Between the passing commit and the checked-out one, nothing changed that
  the earlier code would read from the checkout other than state: every
  changed path is under ``autoscout/`` (the snapshot brings its own), under
  ``runs/`` (state), or in ``NOT_READ_BY_A_RUN``. A change to ``configs/``,
  to tracked ``data/`` or to ``.gitignore`` (which decides what the run's
  Git envelope counts as dirty) stops the night.
- Nothing under ``autoscout/state_store/`` changed: the State Store's code
  defines the state's format (the SQLite schema version, the JSON layout, the
  backend marker), so a new commit that changes it may expect state the
  earlier code cannot read. A database already migrated past the earlier
  code's schema is also refused by the store itself when it opens.
- The production ``.venv`` meets the passing commit's runtime pins (its
  dependencies, every extra in use and every extra the pass was checked with;
  the ``dev`` extra is only for the check), listed with an isolated Python so
  no inherited ``PYTHONPATH`` counts as installed. A new commit whose pins the gate installed therefore stops.

The started-again run confirms that it is what was planned before it writes
anything: the checkout is still at the commit judged, the record still names
the commit it runs, the snapshot is of that commit, its own package was
imported from the snapshot, and its interpreter is the production ``.venv``'s.
A checkout that moved while the gate checked it is not judged at all: the
failure belongs to the commit checked, not to the one now checked out.

What this does not judge: state written by hand, or by a manual run on the
failed code (manual runs are never gated), beyond the State Store's code and
schema; caches under ``data/`` that the earlier code reads with its own
version checks.
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from collections.abc import Mapping
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

from autoscout.code_gate import DEV_EXTRA
from autoscout.code_gate import GATE_DIR
from autoscout.code_gate import REASON_CHECK_FAILED
from autoscout.code_gate import REASON_CHECK_TIMED_OUT
from autoscout.code_gate import REASON_ENVIRONMENT_OUT_OF_DATE
from autoscout.code_gate import REASON_ENVIRONMENT_UPDATE_FAILED
from autoscout.code_gate import REASON_ENVIRONMENT_UPDATE_TIMED_OUT
from autoscout.code_gate import ServerVenv
from autoscout.code_gate import code_version
from autoscout.code_gate import environment_plan
from autoscout.code_gate import is_commit
from autoscout.code_gate import last_passed
from autoscout.code_gate import package_key
from autoscout.code_gate import remembered_extras
from autoscout.code_gate import short_commit
from autoscout.code_gate import venv_python
from autoscout.repository_root import REPOSITORY_ROOT_ENV

# The version of the hand-over between the deciding start and the started-again
# one. A passing commit whose package does not declare it cannot run as fallback.
FALLBACK_PROTOCOL = 1
FALLBACK_ENV = "AUTOSCOUT_CODE_GATE_FALLBACK"
SNAPSHOT_DIR = GATE_DIR / "last-passed"
SNAPSHOT_STAMP = "snapshot.json"

# A scheduled start whose checked-out version cannot even be imported (#1169).
# The server's launcher, ``deploy/server/autoscout-scheduled-start``, installed
# outside the checkout, then starts the run from a snapshot of the last passing
# commit's package in ``START_SNAPSHOT_DIR`` and marks it with ``START_ENV``.
# That passing code takes the lock, updates the checkout and passes the gate as
# any start does; on a pass it restarts on the checked-out code (leaving the
# snapshot), on a failure it falls back as above or stops with its report. The
# launcher starts only a commit whose package declares ``START_PROTOCOL``, read
# without running it; it holds its own copy of these three values, and a test
# keeps the copies equal.
START_PROTOCOL = 1
START_ENV = "AUTOSCOUT_START_FROM_LAST_PASSED"
START_SNAPSHOT_DIR = GATE_DIR / "start"
PACKAGE = "autoscout"
STATE_STORE_PACKAGE = "autoscout/state_store/"
STATE_PREFIX = "runs/"

# The gate's failures that are about the new code or its environment. Any other
# (an unknown version, a checkout with uncommitted changes) is a problem of the
# checkout, and the night stops as before.
ELIGIBLE_REASONS = frozenset(
    {
        REASON_CHECK_FAILED,
        REASON_CHECK_TIMED_OUT,
        REASON_ENVIRONMENT_OUT_OF_DATE,
        REASON_ENVIRONMENT_UPDATE_FAILED,
        REASON_ENVIRONMENT_UPDATE_TIMED_OUT,
    }
)

# Paths of the checkout that a run never reads: a change in them between the
# passing commit and the checked-out one leaves the earlier code's inputs as
# they were. ``pyproject.toml`` is judged by its pins below; ``scripts/`` only
# start a run, and the run in progress is already started.
NOT_READ_BY_A_RUN_DIRS = ("tests/", "docs/", ".github/", ".claude/", ".agents/", "deploy/", "scripts/")
NOT_READ_BY_A_RUN_FILES = frozenset(
    {
        "pyproject.toml",
        "AGENTS.md",
        "CODING_STANDARDS.md",
        "GLOSSARY.md",
        "PLAN.md",
        "README.md",
        "LICENSE",
        "skills-lock.json",
        ".gitguardian.yml",
    }
)

STATUS_READY = "ready"
STATUS_REFUSED = "refused"
# The code-gate entry of a night that ran on the last passing code.
STATUS_RAN_LAST_PASSED = "ran_last_passed"

REFUSED_NO_PASS = "no_recorded_pass"
REFUSED_PREDATES_FALLBACK = "last_pass_predates_fallback"
REFUSED_UNAVAILABLE = "last_pass_unavailable"
REFUSED_FILES_CHANGED = "new_commit_changes_files_the_run_reads"
REFUSED_STATE_STORE_CHANGED = "new_commit_changes_state_store"
REFUSED_VENV = "venv_does_not_meet_last_pass_pins"
# The checkout moved while the gate checked it: the commit that failed is no longer the one checked out.
REFUSED_CHECKOUT_MOVED = "checkout_moved_during_check"
# The started-again run is not what the deciding start planned.
REFUSED_NOT_CONFIRMED = "fallback_not_confirmed"

GIT_TIMEOUT_SECONDS = 120


def _git(repo_root: Path, *args: str) -> bytes | None:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=repo_root, capture_output=True, check=False, timeout=GIT_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _refused(reason: str, **detail: Any) -> dict[str, Any]:
    return {"status": STATUS_REFUSED, "reason": reason, **detail}


def declares_protocol(source: str) -> bool:
    """Whether the module text *source* sets ``FALLBACK_PROTOCOL`` to this protocol, read without running it."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "FALLBACK_PROTOCOL" for target in node.targets
        ):
            return isinstance(node.value, ast.Constant) and node.value.value == FALLBACK_PROTOCOL
    return False


def changed_paths(repo_root: Path, base: str, head: str) -> list[str] | None:
    """Every path that differs between the commits *base* and *head*; ``None`` when Git cannot tell."""
    out = _git(repo_root, "diff", "--name-only", "--no-renames", "-z", base, head, "--")
    if out is None:
        return None
    return [path for path in out.decode("utf-8", "replace").split("\0") if path]


def judge_changes(paths: list[str]) -> dict[str, Any] | None:
    """A refusal when a changed path is one the earlier code would read or one that defines the state's format."""
    state_store = sorted(path for path in paths if path.startswith(STATE_STORE_PACKAGE))
    if state_store:
        return _refused(REFUSED_STATE_STORE_CHANGED, paths=state_store)
    read = sorted(
        path
        for path in paths
        if not path.startswith((f"{PACKAGE}/", STATE_PREFIX, *NOT_READ_BY_A_RUN_DIRS))
        and path not in NOT_READ_BY_A_RUN_FILES
    )
    if read:
        return _refused(REFUSED_FILES_CHANGED, paths=read)
    return None


def runtime_project(project: Mapping[str, Any]) -> dict[str, Any]:
    """The ``[project]`` table without the ``dev`` extra, which only the check needs."""
    optional = project.get("optional-dependencies")
    optional = optional if isinstance(optional, Mapping) else {}
    return {**project, "optional-dependencies": {k: v for k, v in optional.items() if k != DEV_EXTRA}}


def venv_meets_pins(repo_root: Path, commit: str, venv: Any, passed_extras: Any = ()) -> bool:
    """Whether the production ``.venv`` meets *commit*'s runtime pins; ``False`` when it cannot be told.

    The extras compared are those in use now, those ``extras.json`` remembers
    and *passed_extras*, the ones the passing version was checked with: an
    install of newer code killed halfway can have removed both the packages
    and the newer memory of an extra.
    """
    raw = _git(repo_root, "show", f"{commit}:pyproject.toml")
    if raw is None or not venv.exists():
        return False
    try:
        project = tomllib.loads(raw.decode("utf-8")).get("project")
    except (UnicodeDecodeError, ValueError):
        return False
    installed = venv.installed()
    if not isinstance(project, dict) or installed is None:
        return False
    extras = [str(name) for name in passed_extras] if isinstance(passed_extras, list) else []
    runtime = runtime_project(project)
    plan = environment_plan(runtime, installed, [*remembered_extras(repo_root), *extras])
    if plan.stale:
        return False
    return not _missing_marked_requirement_applies(repo_root, runtime, plan.extras, installed)


_REQUIREMENT_NAME = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _missing_marked_requirement_applies(
    repo_root: Path, project: Mapping[str, Any], extras: tuple[str, ...], installed: Mapping[str, str]
) -> bool:
    """Whether a requirement with an environment marker is missing although its marker may hold here.

    ``environment_plan`` leaves such a requirement out when its package is
    absent. A fallback needs to know: the marker is evaluated with
    ``packaging`` when this process runs on the checkout's ``.venv`` (the
    interpreter the run starts again with); otherwise, or when the marker
    cannot be evaluated, it is taken to hold.
    """
    import sys

    have = {package_key(name) for name in installed}
    optional = project.get("optional-dependencies")
    optional = optional if isinstance(optional, Mapping) else {}
    lines: list[Any] = list(project.get("dependencies") or [])
    for extra in extras:
        lines.extend(optional.get(extra) or [])
    same_interpreter = Path(sys.prefix).resolve() == (repo_root / ".venv").resolve()
    for line in lines:
        text, _, marker = str(line).partition(";")
        match = _REQUIREMENT_NAME.match(text)
        if not marker.strip() or match is None or package_key(match.group(1)) in have:
            continue
        if not same_interpreter:
            return True
        try:
            from packaging.markers import Marker

            if Marker(marker.strip()).evaluate():
                return True
        except Exception:
            return True
    return False


def write_snapshot(repo_root: Path, commit: str) -> Path | None:
    """Write *commit*'s package to ``data/code-gate/last-passed/``, replacing any earlier one; ``None`` on failure."""
    archive = _git(repo_root, "archive", "--format=tar", commit, PACKAGE)
    if archive is None:
        return None
    target = repo_root / SNAPSHOT_DIR
    staging: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".last-passed-", dir=target.parent))
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            tar.extractall(staging, filter="data")
        if not (staging / PACKAGE / "__init__.py").is_file():
            return None
        (staging / SNAPSHOT_STAMP).write_text(json.dumps({"commit": commit}) + "\n", encoding="utf-8")
        shutil.rmtree(target, ignore_errors=True)
        staging.rename(target)
        staging = None
    except (OSError, tarfile.TarError):
        return None
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
    return target


def plan(repo_root: Path, verdict: Mapping[str, Any], *, venv: Any | None = None) -> dict[str, Any] | None:
    """Whether a failed gate's *verdict* can run the last passing code, and what to start.

    ``None`` when the failure is not one the fallback covers. Otherwise a
    refusal with its ``reason``, or ``status: ready`` with the commits, the
    gate's reason and the snapshot the run starts again from.
    """
    reason = verdict.get("reason")
    if reason not in ELIGIBLE_REASONS:
        return None
    record = last_passed(repo_root)
    ran = record.get("commit")
    if not isinstance(ran, str) or not is_commit(ran):
        return _refused(REFUSED_NO_PASS)
    version = code_version(repo_root)
    if version is None:
        return _refused(REFUSED_UNAVAILABLE)
    if verdict.get("commit") != version.commit:
        return _refused(REFUSED_CHECKOUT_MOVED)
    source = _git(repo_root, "show", f"{ran}:{PACKAGE}/code_gate_fallback.py")
    if source is None or not declares_protocol(source.decode("utf-8", "replace")):
        return _refused(REFUSED_PREDATES_FALLBACK)
    paths = changed_paths(repo_root, ran, version.commit)
    if paths is None:
        return _refused(REFUSED_UNAVAILABLE)
    refusal = judge_changes(paths)
    if refusal is not None:
        return refusal
    if not venv_meets_pins(
        repo_root, ran, venv if venv is not None else ServerVenv(repo_root), record.get("extras", [])
    ):
        return _refused(REFUSED_VENV)
    snapshot = write_snapshot(repo_root, ran)
    if snapshot is None:
        return _refused(REFUSED_UNAVAILABLE)
    return {
        "status": STATUS_READY,
        "ran_commit": ran,
        "failed_commit": version.commit,
        "reason": reason,
        "passed_at": str(record.get("passed_at", "")),
        "snapshot": str(snapshot),
        # The interpreter whose packages were compared with the pins, whatever Python made this decision.
        "python": str(venv_python(repo_root)),
        **_environment_update(verdict),
    }


def _environment_update(verdict: Mapping[str, Any]) -> dict[str, Any]:
    """The .venv update the failed gate made before its check (#1130), carried to the started-again run."""
    update = verdict.get("environment_update")
    return {"environment_update": dict(update)} if isinstance(update, Mapping) else {}


def started_from_last_passed(environ: Mapping[str, str]) -> bool:
    """Whether this run was started from the last passing code because the checkout could not start (#1169)."""
    return bool(environ.get(START_ENV, ""))


def leave_start_snapshot(environ: MutableMapping[str, str]) -> None:
    """Take out of *environ* what put the start snapshot first, so a restart runs the checked-out code (#1169).

    Nothing changes in the environment of any other run.
    """
    if environ.pop(START_ENV, None) is None:
        return
    for key in ("PYTHONPATH", "PYTHONSAFEPATH", REPOSITORY_ROOT_ENV):
        environ.pop(key, None)


def environment(ready: Mapping[str, Any], repo_root: Path, base: Mapping[str, str]) -> dict[str, str]:
    """The environment that starts the run again on the snapshot of a ready plan."""
    env = dict(base)
    # A start from the last passing code (#1169) that falls back is a fallback like any other.
    env.pop(START_ENV, None)
    env["PYTHONPATH"] = str(ready["snapshot"])
    env["PYTHONSAFEPATH"] = "1"
    env[REPOSITORY_ROOT_ENV] = str(repo_root.resolve())
    env[FALLBACK_ENV] = json.dumps(
        {
            "protocol": FALLBACK_PROTOCOL,
            "ran_commit": ready["ran_commit"],
            "failed_commit": ready["failed_commit"],
            "reason": ready["reason"],
            "passed_at": ready.get("passed_at", ""),
            **_environment_update(ready),
        }
    )
    return env


def handed_over(environ: Mapping[str, str]) -> dict[str, Any] | None:
    """The hand-over of a run started again on the last passing code; ``None`` for any other run."""
    raw = environ.get(FALLBACK_ENV, "")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def confirm(
    repo_root: Path,
    environ: Mapping[str, str],
    *,
    package_file: Path | None = None,
    interpreter_prefix: Path | None = None,
) -> dict[str, Any]:
    """The started-again run's own verdict: the code-gate entry to log, or a failure.

    *package_file* is a file of the running package and *interpreter_prefix*
    the running interpreter's ``sys.prefix``, for tests.
    """
    import sys

    handover = handed_over(environ) or {}
    reason = handover.get("reason") if handover.get("reason") in ELIGIBLE_REASONS else REASON_CHECK_FAILED
    failed_commit = handover.get("failed_commit")
    ran = handover.get("ran_commit")
    refused = {
        "status": "failed",
        "reason": reason,
        "commit": failed_commit if isinstance(failed_commit, str) else "",
        "fallback_refused": REFUSED_NOT_CONFIRMED,
    }
    if handover.get("protocol") != FALLBACK_PROTOCOL or not is_commit(ran):
        return refused
    version = code_version(repo_root)
    if version is None or version.commit != failed_commit:
        return refused
    if last_passed(repo_root).get("commit") != ran:
        return refused
    snapshot = (repo_root / SNAPSHOT_DIR).resolve()
    try:
        stamp = json.loads((snapshot / SNAPSHOT_STAMP).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return refused
    if not isinstance(stamp, dict) or stamp.get("commit") != ran:
        return refused
    running = (package_file or Path(__file__)).resolve()
    if not running.is_relative_to(snapshot):
        return refused
    # The pins were compared with the checkout's .venv, so the run must be its Python.
    prefix = (interpreter_prefix or Path(sys.prefix)).resolve()
    if prefix != (repo_root / ".venv").resolve():
        return refused
    return {
        "status": STATUS_RAN_LAST_PASSED,
        "commit": short_commit(failed_commit),
        "ran_commit": short_commit(ran),
        "reason": reason,
        "passed_at": str(handover.get("passed_at", "")),
        **_environment_update(handover),
    }


def start_again(ready: Mapping[str, Any], repo_root: Path) -> None:
    """Replace this process with the same command running on the snapshot of a ready plan."""
    import sys

    python = str(ready["python"])
    sys.stdout.flush()
    sys.stderr.flush()
    os.execve(python, [python, *sys.orig_argv[1:]], environment(ready, repo_root, os.environ))
