"""The run server's gate on new code (#1124).

The scheduled run takes whatever ``main`` holds, and GitHub checks nothing
(CI is disabled). So a scheduled run executes code only after that code has
passed ``./scripts/check`` on the machine that runs it. The gate runs after
the run's own repository update and before any stage that reads or writes
state; a failing gate stops the run with the state untouched.

What counts as new code: the run server commits its own state every night
under ``runs/``. Those commits must not cost three minutes of tests, so the
gate is keyed to a *code version*: a hash of the committed tree with the
``runs/`` entry left out. Two commits that differ only in ``runs/`` are the
same code version. Any other change (code, tests, configs, docs) is a new
code version and is checked once.

The last code version that passed is recorded in ``data/code-gate/`` of the
checkout, which Git ignores, so the record is per machine and never
committed. Only passes are recorded: a failed version is checked again by
the next scheduled run, so a fix merged to ``main``, or a repaired
environment, clears a failed gate without any manual step.

The check runs with its own temporary directory on disk, removed after the
check, so it neither depends on the system temporary directory nor leaves
lock files in it (#1119). The ``AUTOSCOUT_*`` variables of the service (mail
credentials, the live search endpoint) are not passed to it; files under the
user's home stay readable, as in any test run on that machine.

An environment behind the pins (#1130): a merge that bumps a pin or adds a
dependency leaves the server's ``.venv`` behind ``pyproject.toml``, and
``./scripts/check`` then refuses to test anything. That is not a defect of the
new code, so before the check the gate compares the ``.venv`` with the pins
and, when it is behind, installs the checked-out project into it
(``pip install -e ".[dev,...]"``) with a time limit, logs every package that
changed, and then runs the check. The extras installed are ``dev`` and every
other extra of which at least one package is already in the ``.venv`` or was
on an earlier check (``data/code-gate/extras.json``; on the run server that is
``pw``): the gate keeps the extras the machine uses and adds none. A failed or timed-out install stops the run with its own reason;
nothing is recorded, so the next run tries again. Only the ``.venv`` of the
checkout is ever touched; without one the gate installs nothing.

What the gate does not cover: a change made only under ``runs/`` counts as
state and is never checked, and the code of a failed version stays checked
out, so the next night starts on it and relies on that code's own gate to
check it again (#1124, review). Moving the checkout back was rejected: it
leaves ``main`` behind origin, and a daytime state commit then diverges. A
failed night runs the last passing code from a snapshot instead, when that is
safe (#1159, ``autoscout.code_gate_fallback``).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import tomllib
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from autoscout.json_io import read_json
from autoscout.json_io import write_json

GATE_DIR = Path("data") / "code-gate"
RECORD_FILE = "passed.json"
CHECK_LOG_FILE = "last-check.log"
INSTALL_LOG_FILE = "last-install.log"
# The extras this machine was seen to use (#1130). A killed install can remove
# an extra's packages; the extra is still installed next time.
EXTRAS_FILE = "extras.json"
# The top-level entries the run server's own commits write. A change only
# here is not new code.
STATE_TREE_ENTRIES = frozenset({"runs"})
# The base directory of the check's temporary files; the default is on disk
# under the user's cache directory.
TMPDIR_ENV = "AUTOSCOUT_CODE_GATE_TMPDIR"
# 6 minutes 24 seconds on the run server on 2026-11-17; a hung check must not
# hold the night.
CHECK_TIMEOUT_SECONDS = 30 * 60
# An install of the project and its pinned extras into the .venv (#1130). It
# downloads only what changed; ten minutes covers a slow mirror, and pip's own
# network timeout keeps one stuck connection from using all of it.
INSTALL_TIMEOUT_SECONDS = 10 * 60
INSTALL_NETWORK_TIMEOUT_SECONDS = 60
# The extra the check needs; it is always installed.
DEV_EXTRA = "dev"

STATUS_PASSED = "passed"
STATUS_NOT_CHECKED = "not_checked"
STATUS_FAILED = "failed"

REASON_CHECK_FAILED = "new_code_failed_check"
# ./scripts/check refuses a .venv behind the dev pins of pyproject.toml before
# testing anything; a merge that bumps a pin needs a reinstall on the server.
REASON_ENVIRONMENT_OUT_OF_DATE = "server_venv_out_of_date"
_STALE_VENV_MARKER = "does not match the dev pins"
# The .venv was behind and the install that should have updated it failed or
# timed out (#1130): the environment, not the code, stopped the night.
REASON_ENVIRONMENT_UPDATE_FAILED = "server_venv_update_failed"
REASON_ENVIRONMENT_UPDATE_TIMED_OUT = "server_venv_update_timed_out"
ENVIRONMENT_UPDATE_REASONS = frozenset({REASON_ENVIRONMENT_UPDATE_FAILED, REASON_ENVIRONMENT_UPDATE_TIMED_OUT})
REASON_CHECK_TIMED_OUT = "new_code_check_timed_out"
REASON_CHECK_NOT_STARTED = "new_code_check_not_started"
REASON_VERSION_UNKNOWN = "code_version_unknown"
# The check tests the working tree, the record names HEAD's tree: they must agree.
REASON_CHECKOUT_CHANGED = "checkout_has_uncommitted_changes"

_COMMIT = re.compile(r"[0-9a-f]{7,64}")
_REQUIREMENT = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(?:==\s*([^\s,;]+))?")
_LIST_PACKAGES = (
    "import json, importlib.metadata as m; "
    "print(json.dumps({d.metadata['Name']: d.version for d in m.distributions() if d.metadata['Name']}))"
)

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class CodeVersion:
    commit: str
    fingerprint: str


def is_commit(value: Any) -> bool:
    """Whether *value* is a commit id as Git prints it."""
    return isinstance(value, str) and _COMMIT.fullmatch(value) is not None


def short_commit(value: Any) -> str:
    """The first 12 hex digits of a commit id; anything else is empty, never text."""
    return value[:12] if isinstance(value, str) and _COMMIT.fullmatch(value) else ""


def code_version(repo_root: Path) -> CodeVersion | None:
    """The checked-out commit and its code fingerprint; ``None`` when Git cannot tell."""
    head = _git(repo_root, "rev-parse", "HEAD")
    listing = _git(repo_root, "ls-tree", "--full-tree", "HEAD")
    if head is None or listing is None:
        return None
    commit = head.strip()
    if not _COMMIT.fullmatch(commit):
        return None
    code_entries = [line for line in listing.splitlines() if line.split("\t", 1)[-1] not in STATE_TREE_ENTRIES]
    fingerprint = hashlib.sha256("\n".join(code_entries).encode("utf-8")).hexdigest()
    return CodeVersion(commit=commit, fingerprint=fingerprint)


def _git(repo_root: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(["git", *args], cwd=repo_root, capture_output=True, text=True, check=False, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def last_passed(repo_root: Path) -> dict[str, Any]:
    """The record of the last code version that passed here; empty when there is none or it is unreadable."""
    try:
        record = read_json(repo_root / GATE_DIR / RECORD_FILE, default={})
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def status(repo_root: Path) -> dict[str, Any]:
    """Whether the checked-out code version passed the gate on this machine. Read-only."""
    version = code_version(repo_root)
    if version is None:
        return {"status": STATUS_NOT_CHECKED, "reason": REASON_VERSION_UNKNOWN, "commit": ""}
    record = last_passed(repo_root)
    passed = record.get("code_fingerprint") == version.fingerprint
    result: dict[str, Any] = {
        "status": STATUS_PASSED if passed else STATUS_NOT_CHECKED,
        "commit": version.commit,
    }
    if passed:
        result["passed_commit"] = short_commit(record.get("commit"))
        result["passed_at"] = str(record.get("passed_at", ""))
    return result


def is_passed(repo_root: Path) -> bool:
    return status(repo_root).get("status") == STATUS_PASSED


def _check_environment(environ: Mapping[str, str], tmpdir: Path) -> dict[str, str]:
    # The search paths a run on the last passing code sets (#1159) would test that code instead of this one.
    env = {
        key: value
        for key, value in environ.items()
        if not key.startswith("AUTOSCOUT_") and key not in ("PYTHONPATH", "PYTHONSAFEPATH")
    }
    env["TMPDIR"] = str(tmpdir)
    return env


def _tmp_base(environ: Mapping[str, str]) -> Path:
    configured = environ.get(TMPDIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    cache = environ.get("XDG_CACHE_HOME", "").strip()
    return (Path(cache) if cache else Path.home() / ".cache") / "autoscout" / "code-gate"


def _run_in_own_group(args: list[str], *, timeout: float, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run *args* in a new process group; on timeout kill the whole group, test workers included."""
    kwargs.pop("check", None)
    proc = subprocess.Popen(args, start_new_session=True, **kwargs)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        out, _ = proc.communicate()
        raise subprocess.TimeoutExpired(args, timeout, output=out) from None
    return subprocess.CompletedProcess(args, proc.returncode, stdout=out)


# ── The .venv behind the pins (#1130) ──────────────────────────────────────


def package_key(name: str) -> str:
    """A package name as pip compares them: lower case, runs of ``-_.`` as one ``-``."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class EnvironmentPlan:
    """What the .venv lacks: the extras to install and each requirement it does not meet."""

    extras: tuple[str, ...]
    stale: tuple[tuple[str, str, str], ...]  # (package, wanted, installed or "")


def _requirements(lines: Any) -> list[tuple[str, str, bool]]:
    """Each requirement's package, its ``==`` pin or ``""``, and whether it carries an environment marker."""
    found: list[tuple[str, str, bool]] = []
    for line in lines if isinstance(lines, list) else []:
        text, marker, _ = str(line).partition(";")
        match = _REQUIREMENT.match(text)
        if match:
            found.append((match.group(1), match.group(2) or "", bool(marker.strip())))
    return found


def environment_plan(
    project: Mapping[str, Any], installed: Mapping[str, str], remembered: Iterable[str] = ()
) -> EnvironmentPlan:
    """Compare the ``[project]`` table of pyproject.toml with the installed packages.

    The extras are ``dev``, every other extra with at least one of its
    packages installed, and every extra in *remembered* that the project still
    has. A pinned requirement (``==``) is met by that version only, any other
    by the package being present. A requirement with an environment marker
    is compared only when its package is installed: the gate does not
    evaluate markers, and pip leaves out what does not apply here.
    """
    have = {package_key(name): version for name, version in installed.items()}
    optional = project.get("optional-dependencies")
    optional = optional if isinstance(optional, Mapping) else {}
    kept = set(remembered)
    in_use = sorted(
        name
        for name, lines in optional.items()
        if name != DEV_EXTRA and (name in kept or any(package_key(pkg) in have for pkg, _, _ in _requirements(lines)))
    )
    extras = [DEV_EXTRA, *in_use]
    wanted = _requirements(project.get("dependencies"))
    for extra in extras:
        wanted.extend(_requirements(optional.get(extra)))
    stale: list[tuple[str, str, str]] = []
    # The project itself: an install killed between pip's uninstall and install leaves every pin met.
    own = str(project.get("name", ""))
    if own and package_key(own) not in have:
        stale.append((own, "", ""))
    for name, pin, has_marker in wanted:
        found = have.get(package_key(name), "")
        if has_marker and not found:
            continue
        if not found or (pin and not _same_version(found, pin)):
            stale.append((name, pin, found))
    return EnvironmentPlan(extras=tuple(extras), stale=tuple(stale))


def _same_version(found: str, pin: str) -> bool:
    """Whether *found* meets ``==pin`` as pip judges it (``1.0`` is ``1.0.0``, ``6.*`` a prefix).

    ``packaging`` comes with the dev tools; without it the strings are compared.
    """
    try:
        from packaging.specifiers import InvalidSpecifier
        from packaging.specifiers import SpecifierSet
    except ImportError:
        return found == pin
    try:
        return SpecifierSet(f"=={pin}").contains(found, prereleases=True)
    except (InvalidSpecifier, ValueError):
        return found == pin


def remembered_extras(repo_root: Path) -> list[str]:
    try:
        record = read_json(repo_root / GATE_DIR / EXTRAS_FILE, default={})
    except (OSError, ValueError):
        return []
    extras = record.get("extras") if isinstance(record, dict) else None
    return [str(name) for name in extras] if isinstance(extras, list) else []


def _remember_extras(repo_root: Path, extras: tuple[str, ...]) -> None:
    try:
        (repo_root / GATE_DIR).mkdir(parents=True, exist_ok=True)
        write_json(repo_root / GATE_DIR / EXTRAS_FILE, {"extras": [name for name in extras if name != DEV_EXTRA]})
    except OSError:
        pass


def venv_python(repo_root: Path) -> Path:
    """The Python of the checkout's own ``.venv``."""
    return repo_root / ".venv" / "bin" / "python"


class ServerVenv:
    """The checkout's own ``.venv``: list its packages and install the checked-out project into it."""

    def __init__(self, repo_root: Path, *, runner: Runner = _run_in_own_group) -> None:
        self._repo_root = repo_root
        self._python = venv_python(repo_root)
        self._runner = runner

    def exists(self) -> bool:
        return os.access(self._python, os.X_OK)

    def installed(self) -> dict[str, str] | None:
        """Every installed package and its version; ``None`` when they cannot be listed."""
        try:
            proc = self._runner(
                # Isolated (-I): a PYTHONPATH of the caller must not count as installed in the .venv (#1159).
                [str(self._python), "-I", "-c", _LIST_PACKAGES],
                cwd="/",
                env={key: value for key, value in os.environ.items() if not key.startswith("AUTOSCOUT_")},
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=120,
            )
            listing = json.loads(_text(proc.stdout)) if proc.returncode == 0 else None
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        if not isinstance(listing, dict):
            return None
        return {str(name): str(version) for name, version in listing.items()}

    def install(self, extras: tuple[str, ...], *, env: Mapping[str, str], timeout: float) -> tuple[int, str]:
        """``pip install -e ".[extras]"`` from the checkout; the exit code and the output. Raises on timeout."""
        proc = self._runner(
            [
                str(self._python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--timeout",
                str(INSTALL_NETWORK_TIMEOUT_SECONDS),
                "--retries",
                "2",
                "-e",
                f".[{','.join(extras)}]",
            ],
            cwd=self._repo_root,
            env=dict(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, _text(proc.stdout)


def _read_project(repo_root: Path) -> dict[str, Any] | None:
    try:
        with (repo_root / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle).get("project")
    except (OSError, ValueError):
        return None
    return project if isinstance(project, dict) else None


def package_changes(before: Mapping[str, str], after: Mapping[str, str]) -> list[dict[str, str]]:
    """Each package whose version changed, appeared or went away; ``""`` stands for absent."""
    old = {package_key(name): (name, version) for name, version in before.items()}
    new = {package_key(name): (name, version) for name, version in after.items()}
    changes = []
    for key in sorted(old.keys() | new.keys()):
        was, now = old.get(key, ("", ""))[1], new.get(key, ("", ""))[1]
        if was != now:
            name = new.get(key, old.get(key, ("", "")))[0]
            changes.append({"package": name, "before": was, "after": now})
    return changes


def _update_environment(
    repo_root: Path,
    venv: Any,
    version: CodeVersion,
    environ: Mapping[str, str],
    timeout_seconds: float,
) -> dict[str, Any] | None:
    """Bring the .venv up to the pins when it is behind them.

    Returns ``None`` when nothing was needed or nothing could be judged (no
    .venv, no readable pyproject.toml, no package list: the check then
    decides as before), a failed verdict when the install failed, or the
    update to record: when and which packages changed.
    """
    if not venv.exists():
        return None
    project = _read_project(repo_root)
    before = venv.installed()
    if project is None or before is None:
        return None
    plan = environment_plan(project, before, remembered_extras(repo_root))
    _remember_extras(repo_root, plan.extras)
    if not plan.stale:
        return None
    log_path = repo_root / GATE_DIR / INSTALL_LOG_FILE
    started = datetime.now(UTC)
    behind = "".join(
        f"  {name}: want {want or 'any'}, have {have or 'not installed'}\n" for name, want, have in plan.stale
    )
    header = f"[code-gate] installing extras {','.join(plan.extras)}; behind the pins:\n{behind}"
    tmpdir: Path | None = None
    try:
        base = _tmp_base(environ)
        base.mkdir(parents=True, exist_ok=True)
        tmpdir = Path(tempfile.mkdtemp(prefix="install-", dir=base))
        code, output = venv.install(plan.extras, env=_check_environment(environ, tmpdir), timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _write_log(log_path, version, started, header + _text(exc.output) + "\n[code-gate] the install timed out\n")
        return {"status": STATUS_FAILED, "reason": REASON_ENVIRONMENT_UPDATE_TIMED_OUT, "commit": version.commit}
    except OSError as exc:
        _write_log(
            log_path, version, started, header + f"[code-gate] the install did not start: {type(exc).__name__}\n"
        )
        return {"status": STATUS_FAILED, "reason": REASON_ENVIRONMENT_UPDATE_FAILED, "commit": version.commit}
    finally:
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)
    after = venv.installed()
    changes = package_changes(before, after or {})
    still_behind = after is None or environment_plan(project, after, plan.extras).stale
    summary = "".join(f"  {c['package']}: {c['before'] or '-'} -> {c['after'] or '-'}\n" for c in changes)
    footer = f"[code-gate] pip exit code {code}; changed packages:\n{summary or '  none\n'}"
    if code != 0 or still_behind:
        footer += "[code-gate] the .venv is still behind the pins\n" if still_behind else ""
        _write_log(log_path, version, started, header + output + footer)
        return {"status": STATUS_FAILED, "reason": REASON_ENVIRONMENT_UPDATE_FAILED, "commit": version.commit}
    _write_log(log_path, version, started, header + output + footer)
    return {"updated_at": datetime.now(UTC).isoformat(timespec="seconds"), "packages": changes}


def run_check(
    repo_root: Path,
    *,
    runner: Runner = _run_in_own_group,
    environ: Mapping[str, str] | None = None,
    timeout_seconds: float = CHECK_TIMEOUT_SECONDS,
    venv: Any | None = None,
    install_timeout_seconds: float = INSTALL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Bring the .venv up to the pins when it is behind, run ``./scripts/check`` and record a pass.

    Returns ``status`` (``passed`` or ``failed``), ``commit``, on a failure a
    ``reason`` token, and ``environment_update`` when this call updated the
    .venv. The check's output goes to ``data/code-gate/last-check.log``, the
    install's to ``data/code-gate/last-install.log``. *venv* replaces the
    checkout's ``.venv``, for tests.
    """
    version = code_version(repo_root)
    if version is None:
        return {"status": STATUS_FAILED, "reason": REASON_VERSION_UNKNOWN, "commit": ""}
    # Untracked files are left out: the run lock is one while a run checks.
    changes = _git(repo_root, "status", "--porcelain", "--untracked-files=no")
    if changes is None or changes.strip():
        return {"status": STATUS_FAILED, "reason": REASON_CHECKOUT_CHANGED, "commit": version.commit}
    source_env = os.environ if environ is None else environ
    update = _update_environment(
        repo_root, venv if venv is not None else ServerVenv(repo_root), version, source_env, install_timeout_seconds
    )
    if update is not None and update.get("status") == STATUS_FAILED:
        return update
    updated = {"environment_update": update} if update is not None else {}
    gate_dir = repo_root / GATE_DIR
    log_path = gate_dir / CHECK_LOG_FILE
    started = datetime.now(UTC)
    tmpdir: Path | None = None
    try:
        gate_dir.mkdir(parents=True, exist_ok=True)
        base = _tmp_base(source_env)
        base.mkdir(parents=True, exist_ok=True)
        tmpdir = Path(tempfile.mkdtemp(prefix="check-", dir=base))
        proc = runner(
            [str(repo_root / "scripts" / "check")],
            cwd=repo_root,
            env=_check_environment(source_env, tmpdir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        _write_log(log_path, version, started, _text(exc.output) + "\n[code-gate] the check timed out\n")
        return {"status": STATUS_FAILED, "reason": REASON_CHECK_TIMED_OUT, "commit": version.commit, **updated}
    except OSError as exc:
        _write_log(log_path, version, started, f"[code-gate] the check did not start: {type(exc).__name__}\n")
        return {"status": STATUS_FAILED, "reason": REASON_CHECK_NOT_STARTED, "commit": version.commit, **updated}
    finally:
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)
    _write_log(log_path, version, started, _text(proc.stdout))
    if proc.returncode != 0:
        stale_venv = _STALE_VENV_MARKER in _text(proc.stdout)
        reason = REASON_ENVIRONMENT_OUT_OF_DATE if stale_venv else REASON_CHECK_FAILED
        return {"status": STATUS_FAILED, "reason": reason, "commit": version.commit, **updated}
    passed_at = datetime.now(UTC).isoformat(timespec="seconds")
    # The extras this version ran with: a fallback to it later needs them, also when an interrupted install
    # of newer code has since changed what extras.json remembers (#1159).
    write_json(
        gate_dir / RECORD_FILE,
        {
            "commit": version.commit,
            "code_fingerprint": version.fingerprint,
            "passed_at": passed_at,
            "extras": remembered_extras(repo_root),
            **updated,
        },
    )
    return {"status": STATUS_PASSED, "commit": version.commit, "checked": True, "passed_at": passed_at, **updated}


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value if isinstance(value, str) else ""


def _write_log(path: Path, version: CodeVersion, started: datetime, output: str) -> None:
    header = f"[code-gate] commit {version.commit}, started {started.isoformat(timespec='seconds')}\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(header + output, encoding="utf-8")
    except OSError:
        pass


def ensure_passed(repo_root: Path, *, runner: Runner = _run_in_own_group, venv: Any | None = None) -> dict[str, Any]:
    """The scheduled run's gate: check the checked-out code version unless it already passed here.

    ``passed_at`` is when the version passed, also when this call ran no
    check: a run restarted on updated code finds the pass its first start
    recorded minutes earlier. Such a pass also carries the
    ``environment_update`` its check made, if any; the caller decides whether
    that update belongs to this run.
    """
    current = status(repo_root)
    if current.get("status") == STATUS_PASSED:
        verdict = {
            "status": STATUS_PASSED,
            "commit": current["commit"],
            "checked": False,
            "passed_at": current.get("passed_at", ""),
        }
        update = last_passed(repo_root).get("environment_update")
        if isinstance(update, dict):
            verdict["environment_update"] = update
        return verdict
    return run_check(repo_root, runner=runner, venv=venv)


class ProductionCodeGate:
    """The weekly workflow's code gate on the real checkout.

    *announce* prints one line to the operator, for a manual run on code that
    has not passed here.
    """

    def __init__(
        self,
        repo_root: Path,
        announce: Callable[[str], None] | None = None,
        *,
        runner: Runner = _run_in_own_group,
        venv: Any | None = None,
    ) -> None:
        self._repo_root = repo_root
        self._announce = announce
        self._runner = runner
        self._venv = venv

    def status(self) -> dict[str, Any]:
        return status(self._repo_root)

    def ensure_passed(self) -> dict[str, Any]:
        return ensure_passed(self._repo_root, runner=self._runner, venv=self._venv)

    def fallback_plan(self, verdict: Mapping[str, Any]) -> dict[str, Any] | None:
        """Whether a failed gate can run the last passing code (#1159); see ``autoscout.code_gate_fallback``."""
        from autoscout.code_gate_fallback import plan

        return plan(self._repo_root, verdict, venv=self._venv)

    def confirm_fallback(self) -> dict[str, Any] | None:
        """In a run started again on the last passing code, its verdict; ``None`` in any other run."""
        from autoscout.code_gate_fallback import confirm
        from autoscout.code_gate_fallback import handed_over

        if handed_over(os.environ) is None:
            return None
        return confirm(self._repo_root, os.environ)

    def announce(self, line: str) -> None:
        if self._announce is not None:
            self._announce(line)


UNCHECKED_MANUAL_RUN_LINE = (
    "Внимание: эта версия кода не проходила проверку ./scripts/check на этой машине. "
    "Запуск продолжается; проверить: ./scripts/code-gate --run."
)
KIRKUS_SKIPPED_UNCHECKED_LINE = (
    "Сбор Kirkus не запущен: эта версия кода не проходила проверку на этом сервере. "
    "Ночной поиск проверит код и сам соберёт Kirkus."
)


def code_gate_command(repo_root: Path, *, run: bool) -> int:
    """``./scripts/code-gate``: say whether this checkout's code passed here; with *run*, check it now.

    Exit 0 when the code version passed (or passed now), 1 when it has not.
    """
    if run:
        result = run_check(repo_root)
        commit = short_commit(result.get("commit")) or "?"
        update = result.get("environment_update")
        if isinstance(update, dict):
            print(f"code-gate: updated .venv to the pins: {_changes_text(update.get('packages'))}.")
        if result.get("reason") in ENVIRONMENT_UPDATE_REASONS:
            print(
                f"code-gate: could not update .venv to the pins ({result.get('reason')}); the check did not run. "
                f"Output: {GATE_DIR / INSTALL_LOG_FILE}"
            )
            return 1
        if result.get("status") == STATUS_PASSED:
            print(f"code-gate: commit {commit} passed ./scripts/check and is recorded as passed on this machine.")
            return 0
        print(f"code-gate: commit {commit} did not pass ({result.get('reason')}). Output: {GATE_DIR / CHECK_LOG_FILE}")
        return 1
    current = status(repo_root)
    commit = short_commit(current.get("commit")) or "?"
    if current.get("status") == STATUS_PASSED:
        print(f"code-gate: commit {commit} has passed here (recorded at {current.get('passed_at') or '?'}).")
        return 0
    print(
        f"code-gate: commit {commit} has not passed ./scripts/check on this machine. "
        "The next scheduled run checks it first; ./scripts/code-gate --run checks it now."
    )
    return 1


def _changes_text(packages: Any) -> str:
    changes = [entry for entry in packages if isinstance(entry, dict)] if isinstance(packages, list) else []
    if not changes:
        return "no package changed"
    return ", ".join(f"{c.get('package')} {c.get('before') or '-'} -> {c.get('after') or '-'}" for c in changes)
