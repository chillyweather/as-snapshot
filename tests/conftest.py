"""Shared fixtures for the offline suite.

The State Store backend the suite runs under (#1075) is chosen with
``pytest --state-backend json|sqlite`` (``json`` by default; ``./scripts/check
--state-backend sqlite`` passes it on). Under ``sqlite`` every test runs with
``AUTOSCOUT_STATE_BACKEND=sqlite``, so the commands and subprocesses it starts
open the SQLite backend on every state directory without a marker, and with
``AUTOSCOUT_STATE_DATABASE_DIR`` set to a directory of its own, so each test
gets its own databases outside the repository and parallel workers share
none. A test about the JSON backend's files carries
``@pytest.mark.json_backend_only("<why>")`` and is skipped under ``sqlite``.

Every pytest process (the controller and each xdist worker) works in a
temporary directory of its own, removed when the process ends (#1119): see
``pytest_configure``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from autoscout.git_envelope import GIT_CEILING_DIRECTORIES_ENV
from autoscout.state_store import JSON_BACKEND
from autoscout.state_store import STATE_BACKEND_ENV
from autoscout.state_store import STATE_BACKENDS
from autoscout.state_store import STATE_DATABASE_ENV
from autoscout.state_store.sqlite_location import DATABASE_DIR_ENV
from autoscout.state_store.sqlite_location import DEFAULT_DATABASE_DIR

STATE_BACKEND_OPTION = "--state-backend"
JSON_BACKEND_ONLY = "json_backend_only"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        STATE_BACKEND_OPTION,
        choices=STATE_BACKENDS,
        default=JSON_BACKEND,
        help="the State Store backend every test runs under (#1075); json by default",
    )


TEMPORARY_ROOT_ENV = "PYTEST_DEBUG_TEMPROOT"
TEMPORARY_DIRECTORY_PREFIX = "autoscout-pytest-"


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{JSON_BACKEND_ONLY}(reason): the test is about the JSON backend's files; skipped under --state-backend sqlite",
    )
    _use_own_temporary_directory(config)


def pytest_unconfigure(config: pytest.Config) -> None:
    _remove_own_temporary_directory(config)


def _use_own_temporary_directory(config: pytest.Config) -> None:
    """Point ``tempfile`` and ``TMPDIR`` at a directory this process owns (#1119).

    Production lock files are named after the paths they guard and live in
    the system's temporary directory, and every test guards paths of its own,
    so each run used to leave thousands of lock files (and anything else a
    test wrote to the temporary directory) behind. Here everything the
    process and its subprocesses put in the temporary directory goes into one
    directory that ``pytest_unconfigure`` removes. xdist workers inherit
    ``TMPDIR`` and make their own directory inside the controller's.

    pytest's own ``tmp_path`` root stays where it was (``PYTEST_DEBUG_TEMPROOT``),
    so a failed test's ``tmp_path`` is kept as before. What a test wrote with
    ``tempfile`` directly (``mkdtemp``, ``TemporaryDirectory``, a quarantine
    under ``gettempdir()``) is removed with the directory, pass or fail. A
    killed pytest process leaves its directory behind.
    """
    config.stash[_PREVIOUS_TEMPORARY] = {name: os.environ.get(name) for name in (TEMPORARY_ROOT_ENV, "TMPDIR")}
    config.stash[_PREVIOUS_TEMPDIR] = tempfile.tempdir
    os.environ.setdefault(TEMPORARY_ROOT_ENV, tempfile.gettempdir())
    own = Path(tempfile.mkdtemp(prefix=TEMPORARY_DIRECTORY_PREFIX)).resolve()
    config.stash[_OWN_TEMPORARY_DIRECTORY] = own
    os.environ["TMPDIR"] = str(own)
    tempfile.tempdir = str(own)


def _remove_own_temporary_directory(config: pytest.Config) -> None:
    own = config.stash.get(_OWN_TEMPORARY_DIRECTORY, None)
    if own is None:
        return
    for name, value in config.stash[_PREVIOUS_TEMPORARY].items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    tempfile.tempdir = config.stash[_PREVIOUS_TEMPDIR]
    try:
        remove_tree(own)
    except OSError as error:
        reporter = config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(f"could not remove the run's temporary directory {own}: {error}", yellow=True)


def remove_tree(root: Path) -> None:
    """Remove *root*, including what a test left read-only or locked down.

    Only a permission error is repaired, only below *root*, and each path at
    most once; any other error, or a second failure, is raised.
    """
    repaired: set[str] = set()

    def make_writable_and_retry(function: Callable[..., object], path: str, error: BaseException) -> None:
        if isinstance(error, FileNotFoundError):
            return
        target = Path(path)
        if not isinstance(error, PermissionError) or path in repaired or not _strictly_below(target, root):
            raise error
        repaired.add(path)
        if target.parent != root:
            os.chmod(target.parent, stat.S_IRWXU)
        if target.is_dir() and not target.is_symlink():
            os.chmod(target, stat.S_IRWXU)
            shutil.rmtree(target, onexc=make_writable_and_retry)
        else:
            target.unlink()

    shutil.rmtree(root, onexc=make_writable_and_retry)


def _strictly_below(path: Path, root: Path) -> bool:
    return path != root and path.is_relative_to(root)


_OWN_TEMPORARY_DIRECTORY = pytest.StashKey[Path]()
_PREVIOUS_TEMPORARY = pytest.StashKey[dict[str, str | None]]()
_PREVIOUS_TEMPDIR = pytest.StashKey[str | None]()


def _default_databases() -> set[str]:
    if not DEFAULT_DATABASE_DIR.is_dir():
        return set()
    return {path.name for path in DEFAULT_DATABASE_DIR.iterdir()}


def pytest_sessionstart(session: pytest.Session) -> None:
    session.config.stash[_DEFAULT_DATABASES] = _default_databases()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail a run that left a database in the repository's ``data/state-store/`` (#1075)."""
    added = _default_databases() - session.config.stash.get(_DEFAULT_DATABASES, set())
    if added:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(
                "the test run left state databases in data/state-store/: " + ", ".join(sorted(added)), red=True
            )


_DEFAULT_DATABASES = pytest.StashKey[set[str]]()


@pytest.fixture(scope="session")
def state_backend(request: pytest.FixtureRequest) -> str:
    """The State Store backend this run selects for every test (``--state-backend``)."""
    return str(request.config.getoption(STATE_BACKEND_OPTION))


@pytest.fixture(autouse=True)
def _select_state_backend(
    request: pytest.FixtureRequest,
    state_backend: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Select the run's State Store backend for this test and nothing a developer's shell exported.

    Under SQLite the test's default databases live in a directory of its own,
    so no test sees another's database and nothing is written under the
    repository. The selection has its own ``MonkeyPatch``, so a test that
    calls ``monkeypatch.undo()`` keeps it.
    """
    marker = request.node.get_closest_marker(JSON_BACKEND_ONLY)
    if state_backend != JSON_BACKEND and marker is not None:
        pytest.skip(f"JSON backend only: {marker.args[0] if marker.args else marker.kwargs.get('reason', '')}")
    with pytest.MonkeyPatch.context() as patch:
        for name in (STATE_BACKEND_ENV, STATE_DATABASE_ENV, DATABASE_DIR_ENV):
            patch.delenv(name, raising=False)
        if state_backend != JSON_BACKEND:
            patch.setenv(STATE_BACKEND_ENV, state_backend)
            patch.setenv(DATABASE_DIR_ENV, str(tmp_path_factory.mktemp("state-databases")))
        yield


@pytest.fixture(autouse=True)
def _no_repository_above_temporary_directories(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop every repository lookup at the system's temporary directory (#1099).

    A test that builds its files in a temporary directory expects no
    repository above them. A stray ``.git`` in the temporary directory itself
    (left by any tool that ran ``git init`` there) would otherwise be taken
    for the repository by ``resolve_repo_root`` and by every ``git`` command
    the test starts. A repository a test creates below it is still found.
    Both this process's own temporary directory and the root that holds
    ``tmp_path`` are ceilings (#1119).
    """
    ceilings = dict.fromkeys(
        str(Path(directory).resolve()) for directory in (tempfile.gettempdir(), os.environ[TEMPORARY_ROOT_ENV])
    )
    monkeypatch.setenv(GIT_CEILING_DIRECTORIES_ENV, os.pathsep.join(ceilings))


# Live grounded discovery is configured through the environment. A developer
# machine that exports these keeps them in the pytest process, where they make
# ``weekly_adapters`` build the real production enrichment against a temporary
# repository that holds no ``configs`` directory. The suite is offline by
# contract, so no test may inherit them.
LIVE_DISCOVERY_ENV_VARS = (
    "AUTOSCOUT_GROUNDED_DISCOVERY_ENDPOINT",
    "AUTOSCOUT_SEARCH_API_KEY",
)


@pytest.fixture(autouse=True)
def _clear_live_discovery_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear live discovery configuration before every test.

    A test that needs one of these sets it with ``monkeypatch.setenv``, which
    still wins because this fixture runs first. Without the clear, the suite
    passes in CI and fails on any machine that runs the search adapter.
    """
    for name in LIVE_DISCOVERY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _clear_typesafe_credential(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Hide the operator's TypeSafe key from every test (issue #725).

    The weekly command binds the live editorial-fit shadow stage, which reads
    the key from the environment or the operator's key file. Without a key
    the stage makes no call, so no test can spend vendor quota by accident.
    """
    from autoscout import jev_adapter

    monkeypatch.delenv(jev_adapter.JEV_API_KEY_ENV, raising=False)
    monkeypatch.setattr(jev_adapter, "JEV_API_KEY_FILE", tmp_path_factory.getbasetemp() / "no-typesafe.env")


@pytest.fixture
def calibration_run_factory() -> Callable[[Path, int], tuple[Path, Path, list[dict[str, Any]]]]:
    """Create a deterministic local calibration run with captured work items."""

    def create(tmp_path: Path, source_count: int = 2) -> tuple[Path, Path, list[dict[str, Any]]]:
        run_dir = tmp_path / "run"
        output_dir = tmp_path / "diagnostics"
        run_dir.mkdir()
        items: list[dict[str, Any]] = []
        for index in range(source_count):
            url = f"https://example.test/source-{index}"
            capture = run_dir / f"capture-{index}.json"
            capture.write_text(
                json.dumps({"source_name": f"Source {index}", "source_url": url, "html": ""}),
                encoding="utf-8",
            )
            items.append(
                {
                    "source_name": f"Source {index}",
                    "source_family": "media",
                    "source_url": url,
                    "capture_file": capture.name,
                    "expected_extraction_file": f"result-{index}.json",
                    "operation_id": "operation",
                    "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
                }
            )
        (run_dir / "codex-extraction-worklist.json").write_text(
            json.dumps({"operation_id": "operation", "work_items": items}),
            encoding="utf-8",
        )
        return run_dir, output_dir, items

    return create
