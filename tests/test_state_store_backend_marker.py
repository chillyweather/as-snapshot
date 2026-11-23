"""A state directory decides its own backend; onboarding publishes through the State Store (#1093).

A state directory cut over to SQLite carries a committed backend marker, so
every command that opens it uses SQLite, with or without the weekly config
or the environment variable, and the JSON backend cannot write it by
accident. A directory without the marker behaves as before. The database of
these tests lives in the test's own directory: the default database
directory is pointed there, so "no environment variable" also means no
``AUTOSCOUT_STATE_DATABASE``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_snapshot

from autoscout.__main__ import main
from autoscout.state_store import JSON_BACKEND
from autoscout.state_store import SQLITE_BACKEND
from autoscout.state_store import STATE_BACKEND_ENV
from autoscout.state_store import STATE_DATABASE_ENV
from autoscout.state_store import STATE_DIRECTORY_SELECTION
from autoscout.state_store import BackendSelection
from autoscout.state_store import Collection
from autoscout.state_store import StateStoreError
from autoscout.state_store import cut_over_to_sqlite
from autoscout.state_store import export_json
from autoscout.state_store import open_configured_state_store
from autoscout.state_store import open_state_store
from autoscout.state_store import rebuild_from_json
from autoscout.state_store import replace_and_export
from autoscout.state_store import roll_back_to_json
from autoscout.state_store import state_database_path
from autoscout.state_store.json_backend import FILE_NAMES
from autoscout.state_store.json_backend import JsonStateStore
from autoscout.state_store.marker import BACKEND_MARKER_FILE
from autoscout.state_store.marker import marker_bytes

ALPHA = {"candidate_id": "candidate-aaaaaaaaaaaaaaaaaaaaaaaa", "identity_key": "alpha|one", "title": "Alpha"}
BETA = {"candidate_id": "candidate-bbbbbbbbbbbbbbbbbbbbbbbb", "identity_key": "beta|two", "title": "Beta"}
SQLITE = BackendSelection(SQLITE_BACKEND, "default")


@pytest.fixture(autouse=True)
def database_dir(tmp_path, monkeypatch) -> Path:
    """Keep every default database in the test's directory, and select nothing through the environment."""
    from autoscout.state_store import sqlite_location

    directory = tmp_path / "databases"
    monkeypatch.setattr(sqlite_location, "DEFAULT_DATABASE_DIR", directory)
    monkeypatch.delenv(STATE_BACKEND_ENV, raising=False)
    monkeypatch.delenv(STATE_DATABASE_ENV, raising=False)
    monkeypatch.delenv(sqlite_location.DATABASE_DIR_ENV, raising=False)
    return directory


def _cut_over(state_dir: Path) -> None:
    rebuild_from_json(state_dir, selection=SQLITE)
    cut_over_to_sqlite(state_dir)


def _sqlite_read(state_dir: Path, collection: Collection) -> Any:
    return open_state_store(state_dir, SQLITE).read(collection)


def _json_file(state_dir: Path, collection: Collection) -> Any:
    return json.loads((state_dir / FILE_NAMES[collection]).read_text(encoding="utf-8"))


def _hide(state_dir: Path) -> int:
    return main(["record-visibility", "--identity-key", "alpha|one", "--action", "hide", "--state-dir", str(state_dir)])


# ── Which backend serves a state directory ──────────────────────────────────


def test_a_state_directory_without_a_marker_is_served_as_before(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    seed_state(state_dir, candidate_history=[ALPHA])

    assert open_state_store(state_dir).describe() == {"backend": JSON_BACKEND, "selected_by": "default"}
    # An explicit selection of SQLite on an unmarked directory stays allowed.
    monkeypatch.setenv(STATE_BACKEND_ENV, SQLITE_BACKEND)
    rebuild_from_json(state_dir)
    assert open_state_store(state_dir).describe()["backend"] == SQLITE_BACKEND
    assert not (state_dir / BACKEND_MARKER_FILE).exists()


def test_a_cut_over_directory_is_served_by_sqlite_without_config_or_environment(tmp_path):
    state_dir = tmp_path / "state"
    seed_state(state_dir, candidate_history=[ALPHA])
    _cut_over(state_dir)

    description = open_state_store(state_dir).describe()
    assert (description["backend"], description["selected_by"]) == (SQLITE_BACKEND, STATE_DIRECTORY_SELECTION)
    assert description["export_current"] == "yes"
    assert open_configured_state_store(state_dir, {}).describe()["backend"] == SQLITE_BACKEND
    assert (state_dir / BACKEND_MARKER_FILE).read_text(encoding="utf-8") == marker_bytes(SQLITE_BACKEND)


def test_an_editor_command_without_config_or_environment_writes_the_database_and_its_export(tmp_path):
    state_dir = tmp_path / "state"
    seed_state(state_dir, candidate_history=[ALPHA])
    _cut_over(state_dir)

    assert _hide(state_dir) == 0

    in_database = _sqlite_read(state_dir, Collection.CANDIDATE_VISIBILITY)
    assert [entry["identity_key"] for entry in in_database] == ["alpha|one"]
    assert _json_file(state_dir, Collection.CANDIDATE_VISIBILITY) == in_database
    # The JSON is the export of the database, nothing written past it.
    assert open_state_store(state_dir).describe()["export_current"] == "yes"
    assert export_json(state_dir).status == "unchanged"


@pytest.mark.parametrize("source", ["environment", "config", "caller"])
def test_a_json_selection_on_a_cut_over_directory_is_refused_for_reads_and_writes(tmp_path, monkeypatch, source):
    state_dir = tmp_path / "state"
    seed_state(state_dir, candidate_history=[ALPHA])
    _cut_over(state_dir)
    before = state_snapshot(state_dir)

    with pytest.raises(StateStoreError, match="state-database rollback"):
        if source == "environment":
            monkeypatch.setenv(STATE_BACKEND_ENV, JSON_BACKEND)
            open_state_store(state_dir).read(Collection.CANDIDATE_HISTORY)
        elif source == "config":
            open_configured_state_store(state_dir, {"state_backend": JSON_BACKEND})
        else:
            open_state_store(state_dir, BackendSelection(JSON_BACKEND, "staging"))

    assert state_snapshot(state_dir) == before


def test_the_json_backend_itself_refuses_to_write_a_cut_over_directory(tmp_path):
    state_dir = tmp_path / "state"
    seed_state(state_dir, candidate_history=[ALPHA])
    _cut_over(state_dir)
    before = state_snapshot(state_dir)
    store = JsonStateStore(state_dir, BackendSelection(JSON_BACKEND, "default"))

    with pytest.raises(StateStoreError, match="does not write it"):
        store.replace(Collection.CANDIDATE_VISIBILITY, [{"identity_key": "alpha|one"}])
    with pytest.raises(StateStoreError, match="does not write it"):
        store.upsert_candidate(BETA)

    assert state_snapshot(state_dir) == before
    assert _sqlite_read(state_dir, Collection.CANDIDATE_HISTORY) == [ALPHA]


def test_an_editor_command_with_a_json_environment_on_a_cut_over_directory_writes_nothing(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    seed_state(state_dir, candidate_history=[ALPHA])
    _cut_over(state_dir)
    before = state_snapshot(state_dir)
    monkeypatch.setenv(STATE_BACKEND_ENV, JSON_BACKEND)

    returncode = _hide(state_dir)

    assert returncode == 2
    assert state_snapshot(state_dir) == before
    assert _sqlite_read(state_dir, Collection.CANDIDATE_VISIBILITY) == []


def test_a_checkout_without_the_database_is_refused_with_the_rebuild_command(tmp_path):
    state_dir = tmp_path / "state"
    seed_state(state_dir, candidate_history=[ALPHA])
    _cut_over(state_dir)
    state_database_path(state_dir).unlink()

    with pytest.raises(StateStoreError, match="state-database rebuild"):
        open_state_store(state_dir).read(Collection.CANDIDATE_HISTORY)
    with pytest.raises(StateStoreError, match="state-database rebuild"):
        export_json(state_dir)
    assert not state_database_path(state_dir).exists()


def test_a_cut_over_directory_with_no_json_and_no_database_is_refused_rather_than_read_as_empty(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _cut_over(state_dir)
    state_database_path(state_dir).unlink()

    with pytest.raises(StateStoreError, match="state-database rebuild"):
        open_state_store(state_dir).read(Collection.CANDIDATE_HISTORY)


@pytest.mark.parametrize("content", ['{"backend": "json"}\n', '{"backend": "sqlite", "database": "x"}\n', "nope"])
def test_a_marker_that_is_not_exactly_sqlite_is_refused(tmp_path, content):
    state_dir = tmp_path / "state"
    seed_state(state_dir, candidate_history=[ALPHA])
    (state_dir / BACKEND_MARKER_FILE).write_text(content, encoding="utf-8")

    with pytest.raises(StateStoreError, match=BACKEND_MARKER_FILE):
        open_state_store(state_dir)


# ── Cutover and rollback ─────────────────────────────────────────────────────


def test_cutover_without_a_database_is_refused_and_writes_no_marker(tmp_path):
    state_dir = tmp_path / "state"
    seed_state(state_dir, candidate_history=[ALPHA])

    with pytest.raises(StateStoreError, match="state-database rebuild"):
        cut_over_to_sqlite(state_dir)
    assert not (state_dir / BACKEND_MARKER_FILE).exists()


def test_cutover_refuses_a_directory_changed_since_the_rebuild(tmp_path):
    state_dir = tmp_path / "state"
    seed_state(state_dir, candidate_history=[ALPHA])
    rebuild_from_json(state_dir, selection=SQLITE)
    seed_state(state_dir, candidate_history=[ALPHA, BETA])

    with pytest.raises(StateStoreError, match="changed since"):
        cut_over_to_sqlite(state_dir)
    assert not (state_dir / BACKEND_MARKER_FILE).exists()


def test_cutover_after_a_rebuild_from_non_canonical_files_still_refuses_a_change_made_since(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    proposals = state_dir / FILE_NAMES[Collection.SOURCE_PROPOSALS]
    proposals.write_text(json.dumps([{"id": "proposal-1"}]), encoding="utf-8")
    assert rebuild_from_json(state_dir, selection=SQLITE).non_canonical == [proposals.name]
    proposals.write_text(json.dumps([{"id": "proposal-1"}, {"id": "proposal-2"}]), encoding="utf-8")

    with pytest.raises(StateStoreError, match="changed since"):
        cut_over_to_sqlite(state_dir)
    assert not (state_dir / BACKEND_MARKER_FILE).exists()
    assert [proposal["id"] for proposal in json.loads(proposals.read_text(encoding="utf-8"))] == [
        "proposal-1",
        "proposal-2",
    ]


def test_cutover_after_a_rebuild_from_non_canonical_files_rewrites_them_in_the_store_s_form(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    proposals = state_dir / FILE_NAMES[Collection.SOURCE_PROPOSALS]
    proposals.write_text(json.dumps([{"id": "proposal-1"}]), encoding="utf-8")
    rebuild_from_json(state_dir, selection=SQLITE)
    assert open_state_store(state_dir, SQLITE).describe()["export_current"] == "never"

    assert cut_over_to_sqlite(state_dir).files_written == 1
    assert proposals.read_text(encoding="utf-8") == '[\n  {\n    "id": "proposal-1"\n  }\n]\n'
    assert open_state_store(state_dir).describe()["export_current"] == "yes"


def test_the_command_cuts_over_and_rolls_back_in_one_step_each(tmp_path, capsys):
    state_dir = tmp_path / "state"
    seed_state(state_dir, candidate_history=[ALPHA])
    arguments = ("--state-dir", str(state_dir))

    assert main(["state-database", "rebuild", *arguments]) == 0
    assert main(["state-database", "cutover", *arguments]) == 0
    assert "state_database_cutover=cut_over" in capsys.readouterr().out
    assert _hide(state_dir) == 0

    assert main(["state-database", "rollback", *arguments]) == 0
    assert "state_database_rollback=rolled_back" in capsys.readouterr().out
    assert not (state_dir / BACKEND_MARKER_FILE).exists()
    assert open_state_store(state_dir).describe() == {"backend": JSON_BACKEND, "selected_by": "default"}
    assert [entry["identity_key"] for entry in read_state(state_dir, Collection.CANDIDATE_VISIBILITY)] == ["alpha|one"]
    # The JSON backend writes the directory again.
    seed_state(state_dir, candidate_history=[ALPHA, BETA])
    assert read_state(state_dir, Collection.CANDIDATE_HISTORY) == [ALPHA, BETA]

    assert main(["state-database", "rollback", *arguments]) == 0
    assert "state_database_rollback=not_cut_over" in capsys.readouterr().out


def test_rollback_refuses_a_directory_changed_outside_the_database_unless_forced(tmp_path):
    state_dir = tmp_path / "state"
    seed_state(state_dir, candidate_history=[ALPHA])
    _cut_over(state_dir)
    visibility = state_dir / FILE_NAMES[Collection.CANDIDATE_VISIBILITY]
    visibility.write_text('[{"identity_key": "edited|by-hand"}]\n', encoding="utf-8")

    with pytest.raises(StateStoreError, match="changed since"):
        roll_back_to_json(state_dir)
    assert (state_dir / BACKEND_MARKER_FILE).exists()

    assert roll_back_to_json(state_dir, force=True) is not None
    assert not (state_dir / BACKEND_MARKER_FILE).exists()
    assert read_state(state_dir, Collection.CANDIDATE_VISIBILITY) == []


# ── The marker in Git, the scans and the protected names ─────────────────────


def test_the_marker_is_a_protected_name_and_is_read_by_both_secret_scans(tmp_path):
    from autoscout.doctor import _secret_scan_flagged
    from autoscout.safe_artifacts import PROTECTED_BASENAMES
    from autoscout.weekly_workflow import _scan_output_dir_for_secrets

    assert BACKEND_MARKER_FILE in PROTECTED_BASENAMES
    (tmp_path / BACKEND_MARKER_FILE).write_text("ghp_" + "FAKE1234" * 5, encoding="utf-8")
    assert _scan_output_dir_for_secrets(tmp_path) == [BACKEND_MARKER_FILE]
    assert _secret_scan_flagged(tmp_path) == [BACKEND_MARKER_FILE]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


def test_the_envelope_commits_the_marker_and_its_removal(tmp_path):
    from autoscout.git_envelope import GitRepositoryAdapter

    repo = tmp_path / "repo"
    state_dir = repo / "state"
    seed_state(state_dir, candidate_history=[ALPHA])
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@test.test")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "state")
    _git(repo, "init", "-q", "--bare", str(tmp_path / "remote.git"))
    _git(repo, "remote", "add", "origin", str(tmp_path / "remote.git"))
    _git(repo, "push", "-q", "--set-upstream", "origin", "main")
    adapter = GitRepositoryAdapter(repo)

    # The weekly run's report directory is its state directory, which the envelope commits.
    _cut_over(state_dir)
    assert adapter.finalize(state_dir)["committed"]
    assert _git(repo, "show", f"HEAD:state/{BACKEND_MARKER_FILE}") == marker_bytes(SQLITE_BACKEND)

    roll_back_to_json(state_dir)
    assert adapter.finalize(state_dir)["committed"]
    assert BACKEND_MARKER_FILE not in _git(repo, "ls-tree", "--name-only", "HEAD", "state/")
    assert _git(repo, "status", "--porcelain") == ""


# ── The weekly run ───────────────────────────────────────────────────────────


def _weekly_fixture(tmp_path: Path) -> tuple[Path, Path, Any]:
    from test_weekly_workflow import _write_capture_fixture
    from test_weekly_workflow import _write_codex_fixture_config
    from test_weekly_workflow import _write_extraction_fixture

    from autoscout.weekly_workflow import deterministic_adapters

    config = _write_codex_fixture_config(tmp_path)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    _cut_over(output_dir)
    _write_capture_fixture(tmp_path, output_dir)
    adapters = deterministic_adapters()
    adapters.capture.prepare(config, output_dir, live=False)
    _write_extraction_fixture(output_dir)
    return config, output_dir, adapters


def test_a_weekly_run_on_a_cut_over_directory_uses_sqlite_without_config_or_environment(tmp_path):
    from autoscout.weekly_workflow import run_weekly_workflow

    config, output_dir, adapters = _weekly_fixture(tmp_path)

    result = run_weekly_workflow(config, output_dir, output_dir, adapters=adapters)

    assert result.outcome.value.startswith("completed/"), result.failed_stage
    start = next(entry for entry in result.log if entry["stage"] == "operation-start")
    assert start["detail"]["state_backend"] == {"backend": SQLITE_BACKEND, "selected_by": STATE_DIRECTORY_SELECTION}
    assert any(entry["stage"] == "state-export" and entry["status"] == "ok" for entry in result.log)
    assert open_state_store(output_dir).describe()["export_current"] == "yes"
    assert _sqlite_read(output_dir, Collection.LATEST_SCOUTING_CANDIDATES)


def test_a_weekly_run_on_a_cut_over_directory_without_its_database_stops_before_it_collects(tmp_path):
    from autoscout.weekly_workflow import run_weekly_workflow

    config, output_dir, adapters = _weekly_fixture(tmp_path)
    state_database_path(output_dir).unlink()
    before = state_snapshot(output_dir)

    result = run_weekly_workflow(config, output_dir, output_dir, adapters=adapters)

    assert result.failed_stage == "operation-start"
    assert any(entry.get("reason") == "state_store_unavailable" for entry in result.log)
    assert state_snapshot(output_dir) == before
    assert not state_database_path(output_dir).exists()


def test_a_weekly_run_whose_environment_selects_json_on_a_cut_over_directory_stops_before_it_collects(
    tmp_path, monkeypatch
):
    from autoscout.weekly_workflow import run_weekly_workflow

    config, output_dir, adapters = _weekly_fixture(tmp_path)
    before = state_snapshot(output_dir)
    monkeypatch.setenv(STATE_BACKEND_ENV, JSON_BACKEND)

    result = run_weekly_workflow(config, output_dir, output_dir, adapters=adapters)

    assert result.failed_stage == "operation-start"
    assert any(entry.get("reason") == "state_backend_conflict" for entry in result.log)
    assert state_snapshot(output_dir) == before


# ── Source onboarding publishes accepted proposals through the store ────────


def test_replace_and_export_refuses_a_directory_whose_export_is_not_current(tmp_path):
    state_dir = tmp_path / "state"
    seed_state(state_dir, candidate_history=[ALPHA])
    _cut_over(state_dir)
    (state_dir / FILE_NAMES[Collection.CANDIDATE_VISIBILITY]).write_text("[]\n", encoding="utf-8")
    store = open_state_store(state_dir)

    with pytest.raises(StateStoreError, match="not current"):
        with replace_and_export(store, Collection.SOURCE_PROPOSALS, [{"id": "proposal-1"}]):
            pass
    assert _sqlite_read(state_dir, Collection.SOURCE_PROPOSALS) == []
    assert not (state_dir / FILE_NAMES[Collection.SOURCE_PROPOSALS]).exists()


def _onboarding_repo(tmp_path: Path) -> dict[str, Path]:
    from test_issue_339_source_onboarding import _base_repo
    from test_issue_339_source_onboarding import _init_git

    paths = _base_repo(tmp_path)
    _cut_over(paths["proposal"].parent)
    _init_git(paths["repo"])
    return paths


def _onboard(
    paths: dict[str, Path],
    capsys: Any,
    monkeypatch: Any,
    *,
    fail_commit: bool = False,
    fail_database_commit: bool = False,
) -> dict[str, Any]:
    from test_issue_339_source_onboarding import _PASSING_RUNNERS
    from test_issue_339_source_onboarding import _args

    import autoscout.source_onboarding_transaction as module

    assert main(_args(paths)) == 3
    preview = json.loads(capsys.readouterr().out)
    monkeypatch.setattr(module, "default_onboarding_runners", lambda: _PASSING_RUNNERS)
    if fail_commit:
        real_git = module.git

        def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
            if args[:2] == ("commit", "-m"):
                return subprocess.CompletedProcess(["git", *args], 1, "", "commit failed")
            return real_git(repo, *args)

        monkeypatch.setattr(module, "git", git)
    if fail_database_commit:
        import sqlite3
        from contextlib import contextmanager

        real_replace_and_export = module.replace_and_export

        @contextmanager
        def failing_commit(store: Any, collection: Collection, value: Any) -> Any:
            # The block completes, then the database's COMMIT fails and rolls back.
            with real_replace_and_export(store, collection, value):
                yield
                raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(module, "replace_and_export", failing_commit)
    main([*_args(paths), "--confirmation-token", preview["confirmation_token"], "--confirmed-by-editor"])
    return json.loads(capsys.readouterr().out)


def test_an_onboarding_accept_under_sqlite_leaves_the_database_and_the_export_in_agreement(
    tmp_path, capsys, monkeypatch
):
    paths = _onboarding_repo(tmp_path)
    state_dir = paths["proposal"].parent

    report = _onboard(paths, capsys, monkeypatch)

    assert report["outcome"] == "completed", report
    in_database = _sqlite_read(state_dir, Collection.SOURCE_PROPOSALS)
    assert [proposal["status"] for proposal in in_database] == ["accepted"]
    assert _json_file(state_dir, Collection.SOURCE_PROPOSALS) == in_database
    assert open_state_store(state_dir).describe()["export_current"] == "yes"
    # The commit holds exactly the bytes on disk, and nothing is left uncommitted.
    relative = paths["proposal"].relative_to(paths["repo"]).as_posix()
    assert _git(paths["repo"], "show", f"HEAD:{relative}") == paths["proposal"].read_text(encoding="utf-8")
    assert _git(paths["repo"], "status", "--porcelain") == ""


def test_a_failed_onboarding_commit_under_sqlite_rolls_the_database_back_with_the_files(tmp_path, capsys, monkeypatch):
    paths = _onboarding_repo(tmp_path)
    state_dir = paths["proposal"].parent

    report = _onboard(paths, capsys, monkeypatch, fail_commit=True)

    assert report["reason_code"] == "git_commit_failed"
    assert _sqlite_read(state_dir, Collection.SOURCE_PROPOSALS) == []
    assert not paths["proposal"].exists()
    assert open_state_store(state_dir).describe()["export_current"] == "yes"
    assert _git(paths["repo"], "status", "--porcelain") == ""


def test_a_database_commit_that_fails_after_the_onboarding_commit_names_the_rebuild(tmp_path, capsys, monkeypatch):
    paths = _onboarding_repo(tmp_path)
    state_dir = paths["proposal"].parent

    report = _onboard(paths, capsys, monkeypatch, fail_database_commit=True)

    assert report["reason_code"] == "state_database_commit_failed"
    assert "state-database rebuild --overwrite" in report["next_action"]
    relative = paths["proposal"].relative_to(paths["repo"]).as_posix()
    committed = json.loads(_git(paths["repo"], "show", f"HEAD:{relative}"))
    assert [proposal["status"] for proposal in committed] == ["accepted"]
    assert _sqlite_read(state_dir, Collection.SOURCE_PROPOSALS) == []

    # The named recovery brings the database and the committed JSON together.
    assert main(["state-database", "rebuild", "--overwrite", "--state-dir", str(state_dir)]) == 0
    assert _sqlite_read(state_dir, Collection.SOURCE_PROPOSALS) == committed
    assert open_state_store(state_dir).describe()["export_current"] == "yes"
