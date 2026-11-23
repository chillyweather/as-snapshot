"""The doctor and run-health report the State Store (#1076, ADR 0023).

Both take one read-only look at the store: the active backend, the database's
path class and schema version, and whether the exported files are what the
database would export. The doctor must change nothing on either backend, and a
mismatch is one finding with one recommended action, reported by both.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from state_store_support import read_state

from autoscout.__main__ import main
from autoscout.doctor import EXIT_BLOCKED
from autoscout.doctor import EXIT_HEALTHY
from autoscout.doctor import DiagnosisClass
from autoscout.doctor import DoctorSeams
from autoscout.doctor import diagnose
from autoscout.doctor import exit_code_for
from autoscout.doctor import render_russian
from autoscout.doctor_state_store import STATE_EXPORT_COMMAND
from autoscout.doctor_state_store import STATE_REBUILD_COMMAND
from autoscout.doctor_state_store import STATE_REBUILD_OVERWRITE_COMMAND
from autoscout.run_health import run_health
from autoscout.state_store import EXPORT_BEHIND
from autoscout.state_store import EXPORT_CHANGED_OUTSIDE
from autoscout.state_store import EXPORT_CURRENT
from autoscout.state_store import EXPORT_NEVER
from autoscout.state_store import EXPORT_NO_DATABASE
from autoscout.state_store import EXPORT_NOT_APPLICABLE
from autoscout.state_store import JSON_BACKEND
from autoscout.state_store import SQLITE_BACKEND
from autoscout.state_store import STATE_BACKEND_ENV
from autoscout.state_store import STATE_DATABASE_ENV
from autoscout.state_store import BackendSelection
from autoscout.state_store import Collection
from autoscout.state_store import export_json
from autoscout.state_store import inspect_state_store
from autoscout.state_store import open_state_store
from autoscout.state_store import rebuild_from_json
from autoscout.state_store.committed_state import state_files_against_head
from autoscout.state_store.layout import shard_relative_path
from autoscout.state_store.sqlite_backend import SqliteStateStore
from autoscout.state_store.sqlite_location import database_path_class

ALPHA = {"candidate_id": "candidate-aaaaaaaaaaaaaaaaaaaaaaaa", "identity_key": "alpha|one", "title": "Alpha"}
# What the doctor must not recommend: it writes the database over files Git holds (#1098).
RESTORE_ALPHA = "git restore --source=HEAD --staged --worktree -- state/candidate-history"
EXPORT_FORCE_COMMAND = "./scripts/state-database export --force"
BETA = {"candidate_id": "candidate-bbbbbbbbbbbbbbbbbbbbbbbb", "identity_key": "beta|two", "title": "Бета"}


@pytest.fixture(autouse=True)
def _no_backend_override(monkeypatch):
    monkeypatch.delenv(STATE_BACKEND_ENV, raising=False)
    monkeypatch.delenv(STATE_DATABASE_ENV, raising=False)


class Setup:
    """A weekly config, output directory and state directory under *tmp_path*, on one backend."""

    def __init__(self, tmp_path: Path, backend: str, *, database: Path | None = None) -> None:
        self.tmp_path = tmp_path
        self.database = database if database is not None else tmp_path / "databases" / "state.sqlite3"
        self.config = tmp_path / "config.json"
        document: dict[str, Any] = {"run_date": "2026-01-01", "sources": []}
        if backend != JSON_BACKEND:
            document.update({"state_backend": backend, "state_database": str(self.database)})
        self.config.write_text(json.dumps(document), encoding="utf-8")
        self.output_dir = tmp_path / "out"
        self.output_dir.mkdir()
        self.state_dir = tmp_path / "state"
        self.selection = BackendSelection(backend, "config", self.database if backend == SQLITE_BACKEND else None)

    def store(self):
        return open_state_store(self.state_dir, self.selection)

    def seed(self, *, export: bool = True) -> None:
        self.state_dir.mkdir(exist_ok=True)
        self.store().replace(Collection.CANDIDATE_HISTORY, [ALPHA])
        if export:
            export_json(self.state_dir, self.selection)

    def diagnose(self, **seams: Any):
        defaults: dict[str, Any] = {"today": lambda: date(2026, 1, 1), "guard_status": lambda: {"guard_exists": False}}
        return diagnose(self.config, self.output_dir, self.state_dir, seams=DoctorSeams(**{**defaults, **seams}))

    def run_health(self, capsys) -> tuple[int, list[str]]:
        capsys.readouterr()
        returncode = run_health(self.output_dir, "2026-01-01", state_dir=self.state_dir, config_path=self.config)
        return returncode, capsys.readouterr().out.splitlines()


def _healthy_artifacts(output: Path) -> None:
    (output / "pilot-status.json").write_text(
        json.dumps({"ready_for_editor": True, "run_date": "2026-01-01"}), encoding="utf-8"
    )
    (output / "run-summary.json").write_text(
        json.dumps({"run_date": "2026-01-01", "editor_facing_count": 0}), encoding="utf-8"
    )
    (output / "source-runs.json").write_text("[]", encoding="utf-8")


def _tree(root: Path) -> dict[str, tuple[Any, ...]]:
    """Every file and directory under *root*: content and modification time for a file.

    SQLite's shared-memory file is mapped by every reader and carries no state, so only its presence counts.
    """
    snapshot: dict[str, tuple[Any, ...]] = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if path.is_dir():
            snapshot[name] = ("directory",)
        elif path.name.endswith("-shm"):
            snapshot[name] = ("shared memory",)
        else:
            snapshot[name] = ("file", hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
    return snapshot


def _russian_part(output: str) -> list[str]:
    """The editor-facing lines of the doctor's output: everything before the JSON report."""
    return output.split("\n\n", 1)[0].splitlines()


# ── What the doctor prints ────────────────────────────────────────────────────


def test_the_doctor_names_the_json_backend_and_changes_nothing_else(tmp_path):
    setup = Setup(tmp_path, JSON_BACKEND)
    setup.seed()

    report = setup.diagnose()

    assert report.diagnosis is DiagnosisClass.HEALTHY
    assert report.facts["state_store"]["backend"] == JSON_BACKEND
    assert report.facts["state_store"]["export_status"] == EXPORT_NOT_APPLICABLE
    assert render_russian(report).splitlines()[1] == "Хранилище состояния: JSON."
    assert exit_code_for(report) == EXIT_HEALTHY


def test_the_doctor_reports_the_database_by_its_path_class_and_never_by_a_path(tmp_path):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    setup.seed()

    report = setup.diagnose()
    facts = report.facts["state_store"]
    text = render_russian(report)

    assert report.diagnosis is DiagnosisClass.HEALTHY
    assert facts["backend"] == SQLITE_BACKEND
    assert facts["database_path_class"] == database_path_class(setup.database)
    assert facts["schema_version"] == 2
    assert facts["export_status"] == EXPORT_CURRENT
    assert "database" not in facts
    line = text.splitlines()[1]
    assert line.startswith("Хранилище состояния: SQLite (") and "схема 2" in line and "экспорт в Git актуален" in line
    assert str(tmp_path) not in text and str(tmp_path) not in json.dumps(report.to_dict(), ensure_ascii=False)


def test_a_sqlite_state_directory_without_a_database_yet_is_nothing_to_report(tmp_path):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    setup.state_dir.mkdir()

    report = setup.diagnose()

    assert report.diagnosis is DiagnosisClass.HEALTHY
    assert report.facts["state_store"]["export_status"] == EXPORT_NO_DATABASE
    assert render_russian(report).splitlines()[1] == "Хранилище состояния: SQLite, база ещё не создана."


def test_the_environment_override_selects_the_backend_the_doctor_reports(tmp_path, monkeypatch):
    setup = Setup(tmp_path, JSON_BACKEND)
    monkeypatch.setenv(STATE_BACKEND_ENV, SQLITE_BACKEND)
    monkeypatch.setenv(STATE_DATABASE_ENV, str(setup.database))
    setup.state_dir.mkdir()

    report = setup.diagnose()

    assert report.facts["state_store"]["backend"] == SQLITE_BACKEND
    assert report.facts["state_store"]["selected_by"] == "environment"


# ── The doctor writes nothing ─────────────────────────────────────────────────


def _plant_behind(setup: Setup) -> None:
    setup.seed()
    setup.store().upsert_candidate(BETA)


def _plant_outside_edit(setup: Setup) -> None:
    setup.seed()
    shard = setup.state_dir / shard_relative_path(ALPHA["candidate_id"])
    shard.write_text(shard.read_text(encoding="utf-8").replace("Alpha", "Edited"), encoding="utf-8")


SCENARIOS = {
    "json_backend": (JSON_BACKEND, lambda setup: setup.seed()),
    "sqlite_without_database": (SQLITE_BACKEND, lambda setup: setup.state_dir.mkdir()),
    "sqlite_current": (SQLITE_BACKEND, lambda setup: setup.seed()),
    "sqlite_export_behind": (SQLITE_BACKEND, _plant_behind),
    "sqlite_never_exported": (SQLITE_BACKEND, lambda setup: setup.seed(export=False)),
    "sqlite_files_edited_outside": (SQLITE_BACKEND, _plant_outside_edit),
    "sqlite_json_state_without_database": (
        SQLITE_BACKEND,
        lambda setup: (setup.seed(), setup.database.unlink(), setup.database.parent.rmdir()),
    ),
}


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_the_doctor_and_run_health_write_nothing_on_either_backend(tmp_path, capsys, scenario):
    backend, plant = SCENARIOS[scenario]
    setup = Setup(tmp_path, backend)
    plant(setup)
    before = _tree(tmp_path)

    setup.diagnose()
    setup.run_health(capsys)
    assert main(
        [
            "doctor",
            "--config",
            str(setup.config),
            "--output-dir",
            str(setup.output_dir),
            "--state-dir",
            str(setup.state_dir),
        ]
    ) in {0, 1, 2}

    assert _tree(tmp_path) == before


def test_the_doctor_creates_no_database_wal_file_or_directory_for_a_new_sqlite_state_directory(tmp_path, capsys):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    # Neither the state directory nor the database's directory exists.
    before = _tree(tmp_path)

    setup.diagnose()
    setup.run_health(capsys)

    assert not setup.state_dir.exists() and not setup.database.parent.exists()
    assert _tree(tmp_path) == before
    assert not list(tmp_path.rglob("*.sqlite3*"))


def test_the_doctor_leaves_a_database_with_a_live_wal_unchanged(tmp_path):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    _plant_behind(setup)
    held = sqlite3.connect(setup.database)
    try:
        # A connection kept open keeps the write-ahead log and shared-memory files in place.
        held.execute("SELECT count(*) FROM store_meta").fetchone()
        assert (setup.database.parent / (setup.database.name + "-wal")).exists()
        before = _tree(tmp_path)

        report = setup.diagnose()

        assert report.diagnosis is DiagnosisClass.STATE_STORE_FINDING
        assert _tree(tmp_path) == before
    finally:
        held.close()


def test_the_doctor_never_opens_a_transaction_or_a_writing_connection(tmp_path, monkeypatch):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    _plant_behind(setup)

    def refuse(*_args, **_kwargs):
        raise AssertionError("the doctor must only read")

    monkeypatch.setattr(SqliteStateStore, "transaction", refuse)
    monkeypatch.setattr(SqliteStateStore, "_connect", refuse)
    monkeypatch.setattr(SqliteStateStore, "_writing", refuse)

    assert setup.diagnose().diagnosis is DiagnosisClass.STATE_STORE_FINDING


def test_the_doctor_does_not_migrate_a_database_of_another_schema_version(tmp_path):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    setup.seed()
    with sqlite3.connect(setup.database) as conn:
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (99, 'later')")
    before = _tree(tmp_path)

    report = setup.diagnose()

    assert report.facts["state_store"]["problem"] == "schema_version_mismatch"
    assert report.facts["state_store"]["schema_version"] == 99
    assert _tree(tmp_path) == before


# ── A planted mismatch is one finding with one action, in the doctor and in run-health ─


def _assert_one_finding(report, command: str) -> list[str]:
    assert report.diagnosis is DiagnosisClass.STATE_STORE_FINDING
    assert exit_code_for(report) == EXIT_BLOCKED
    assert report.facts["state_store"]["recommended_command"] == command
    lines = render_russian(report).splitlines()
    # The verdict, the backend line, exactly one action naming the command, exactly one question.
    assert sum(command in line for line in lines) == 1
    assert report.recommended_action in lines and lines.count(report.editor_question) == 1
    assert len(lines) == 4, lines
    assert lines[0] == report.verdict
    return lines


def test_a_database_changed_after_the_last_export_is_one_finding_with_the_export_command(tmp_path, capsys):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    _plant_behind(setup)

    report = setup.diagnose()

    _assert_one_finding(report, STATE_EXPORT_COMMAND)
    assert report.facts["state_store"]["export_status"] == EXPORT_BEHIND
    assert report.facts["state_store"]["differing"] == [shard_relative_path(BETA["candidate_id"]).as_posix()]
    returncode, lines = setup.run_health(capsys)
    assert returncode == 1
    assert "state_export=behind" in lines and f"state_export_command={STATE_EXPORT_COMMAND}" in lines
    assert "state_backend=sqlite" in lines and "state_schema_version=2" in lines
    # The command the finding names repairs it, and then both are clean.
    assert main(["state-database", "export", "--state-dir", str(setup.state_dir), "--config", str(setup.config)]) == 0
    assert setup.diagnose().diagnosis is DiagnosisClass.HEALTHY
    assert setup.run_health(capsys)[1].count("state_export=current") == 1


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        # No background maintenance: its lock files would show up in the tree snapshots.
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "-c",
            "maintenance.auto=false",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def _commit_state(setup: Setup) -> None:
    """Make *tmp_path* a repository whose HEAD holds the state directory as it is."""
    _git(setup.tmp_path, "init", "-q", "-b", "main")
    _git(setup.tmp_path, "add", "state")
    _git(setup.tmp_path, "commit", "-q", "-m", "state")


def _pull_newer_state(setup: Setup) -> None:
    """What a pull does to a checkout whose database is older: newer files arrive, committed, the database stays."""
    setup.seed()
    _commit_state(setup)
    shard = setup.state_dir / shard_relative_path(ALPHA["candidate_id"])
    shard.write_text(shard.read_text(encoding="utf-8").replace("Alpha", "Pulled"), encoding="utf-8")
    _git(setup.tmp_path, "commit", "-q", "-am", "newer state")


SAFE_REPOSITORY = {"repo_inspect": lambda _allowlist=None: {"status": "ok", "branch": "main"}}


def test_files_committed_in_git_that_the_database_has_not_seen_are_rebuilt_from_not_overwritten(tmp_path, capsys):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    _pull_newer_state(setup)

    report = setup.diagnose(**SAFE_REPOSITORY)

    lines = _assert_one_finding(report, STATE_REBUILD_OVERWRITE_COMMAND)
    assert EXPORT_FORCE_COMMAND not in render_russian(report)
    assert "будут потеряны" in lines[-1] and "базе" in lines[-1]
    assert report.facts["state_store"]["export_status"] == EXPORT_CHANGED_OUTSIDE
    assert report.facts["state_store"]["files_against_git"] == "committed"
    returncode, output = setup.run_health(capsys)
    assert returncode == 1 and "state_export=changed_outside" in output
    assert f"state_export_command={STATE_REBUILD_OVERWRITE_COMMAND}" in output
    assert "state_files_against_git=committed" in output
    assert not any("export --force" in line for line in output)
    # The named command keeps the pulled state, and then both are clean.
    rebuild = ["state-database", "rebuild", "--overwrite", "--state-dir", str(setup.state_dir)]
    assert main([*rebuild, "--config", str(setup.config)]) == 0
    assert setup.diagnose(**SAFE_REPOSITORY).diagnosis is DiagnosisClass.HEALTHY
    assert setup.run_health(capsys)[1].count("state_export=current") == 1
    assert "Pulled" in (setup.state_dir / shard_relative_path(ALPHA["candidate_id"])).read_text(encoding="utf-8")


def test_files_edited_by_hand_on_top_of_the_committed_state_are_undone_first(tmp_path, capsys):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    _plant_outside_edit(setup)
    # The commit holds the exported state; the edit was made after it.
    _git(tmp_path, "init", "-q", "-b", "main")
    shard = setup.state_dir / shard_relative_path(ALPHA["candidate_id"])
    edited = shard.read_text(encoding="utf-8")
    shard.write_text(edited.replace("Edited", "Alpha"), encoding="utf-8")
    _git(tmp_path, "add", "state")
    _git(tmp_path, "commit", "-q", "-m", "state")
    shard.write_text(edited, encoding="utf-8")

    report = setup.diagnose(**SAFE_REPOSITORY)

    assert report.diagnosis is DiagnosisClass.STATE_STORE_FINDING
    assert report.facts["state_store"]["files_against_git"] == "edited"
    assert report.facts["state_store"]["recommended_command"] == RESTORE_ALPHA
    lines = render_russian(report).splitlines()
    assert len(lines) == 4 and "вручную" in lines[0]
    assert RESTORE_ALPHA in lines[2]
    # The question states what is discarded and that committed state is untouched.
    assert "Несохранённые правки" in lines[3] and "нетронутым" in lines[3]
    assert EXPORT_FORCE_COMMAND not in render_russian(report)
    returncode, output = setup.run_health(capsys)
    assert returncode == 1 and "state_files_against_git=edited" in output
    assert f"state_export_command={RESTORE_ALPHA}" in output
    # The action returns the files to the committed ones, which are the export: both are clean.
    subprocess.run(RESTORE_ALPHA.split(), cwd=tmp_path, check=True)
    assert setup.diagnose(**SAFE_REPOSITORY).diagnosis is DiagnosisClass.HEALTHY
    assert setup.run_health(capsys)[1].count("state_export=current") == 1


def test_files_changed_outside_the_export_with_no_git_to_say_which_side_is_newer_ask_for_the_operator(
    tmp_path, monkeypatch, capsys
):
    # No repository above the test directory, whatever sits in the system's temporary directory.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    setup = Setup(tmp_path, SQLITE_BACKEND)
    _plant_outside_edit(setup)

    report = setup.diagnose()

    assert report.diagnosis is DiagnosisClass.STATE_STORE_FINDING
    assert report.facts["state_store"]["files_against_git"] == "unknown"
    lines = render_russian(report).splitlines()
    assert len(lines) == 4 and "не позволяет определить" in lines[0]
    assert "Дим" in report.recommended_action and "export" not in lines[2] and "rebuild" not in lines[2]
    # The one question says what each command would discard.
    assert "export --force" in lines[3] and "rebuild --overwrite" in lines[3]
    assert "recommended_command" not in report.facts["state_store"]
    returncode, output = setup.run_health(capsys)
    assert returncode == 1 and "state_files_against_git=unknown" in output
    assert not any(line.startswith("state_export_command=") for line in output)


def test_the_check_against_git_changes_nothing_in_the_repository_not_even_the_index(tmp_path):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    _pull_newer_state(setup)
    # A file whose modification time Git would record by refreshing the index in a status.
    shard = setup.state_dir / shard_relative_path(ALPHA["candidate_id"])
    os.utime(shard, (1_000_000_000, 1_000_000_000))
    before = _tree(tmp_path)

    found = state_files_against_head(setup.state_dir)

    assert found.relation == "committed" and found.repository_path == "state"
    assert _tree(tmp_path) == before
    assert not (tmp_path / ".git" / "index.lock").exists()


def test_the_check_against_git_sees_an_added_a_deleted_and_an_untouched_file(tmp_path):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    setup.seed()
    _commit_state(setup)
    assert state_files_against_head(setup.state_dir).relation == "committed"

    added = setup.state_dir / "candidate-visibility.json"
    added.write_text("[]\n", encoding="utf-8")
    assert state_files_against_head(setup.state_dir).relation == "edited"
    added.unlink()
    assert state_files_against_head(setup.state_dir).relation == "committed"

    (setup.state_dir / shard_relative_path(ALPHA["candidate_id"])).unlink()
    assert state_files_against_head(setup.state_dir).relation == "edited"


def test_a_staged_hand_edit_is_undone_by_the_recommended_command(tmp_path):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    _plant_outside_edit(setup)
    _git(tmp_path, "init", "-q", "-b", "main")
    shard = setup.state_dir / shard_relative_path(ALPHA["candidate_id"])
    edited = shard.read_text(encoding="utf-8")
    shard.write_text(edited.replace("Edited", "Alpha"), encoding="utf-8")
    _git(tmp_path, "add", "state")
    _git(tmp_path, "commit", "-q", "-m", "state")
    shard.write_text(edited, encoding="utf-8")
    _git(tmp_path, "add", "state")

    command = setup.diagnose(**SAFE_REPOSITORY).facts["state_store"]["recommended_command"]
    subprocess.run(command.split(), cwd=tmp_path, check=True, capture_output=True)

    assert state_files_against_head(setup.state_dir).relation == "committed"
    assert setup.diagnose(**SAFE_REPOSITORY).diagnosis is DiagnosisClass.HEALTHY


def test_a_new_state_file_is_not_something_the_restore_settles_so_the_operator_is_asked(tmp_path):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    setup.seed()
    _commit_state(setup)
    (setup.state_dir / "candidate-visibility.json").write_text("[]\n", encoding="utf-8")
    shard = setup.state_dir / shard_relative_path(ALPHA["candidate_id"])
    shard.write_text(shard.read_text(encoding="utf-8").replace("Alpha", "Edited"), encoding="utf-8")

    found = state_files_against_head(setup.state_dir)
    report = setup.diagnose(**SAFE_REPOSITORY)

    assert found.relation == "edited" and found.has_added_files
    assert report.facts["state_store"]["files_against_git"] == "edited"
    assert "recommended_command" not in report.facts["state_store"]
    assert "Дим" in report.recommended_action and "не удалит новые файлы" in report.editor_question


def test_the_restore_names_only_the_state_files_so_the_run_artifacts_beside_them_keep_their_changes(tmp_path):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    setup.seed()
    log = setup.state_dir / "workflow-log.json"
    log.write_text("[]\n", encoding="utf-8")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "state")
    _git(tmp_path, "commit", "-q", "-m", "state")
    log.write_text('[{"stage": "newest diagnostics"}]\n', encoding="utf-8")
    (setup.state_dir / "candidate-visibility.json").unlink(missing_ok=True)
    shard = setup.state_dir / shard_relative_path(ALPHA["candidate_id"])
    shard.write_text(shard.read_text(encoding="utf-8").replace("Alpha", "Edited"), encoding="utf-8")

    command = setup.diagnose(**SAFE_REPOSITORY).facts["state_store"]["recommended_command"]
    subprocess.run(command.split(), cwd=tmp_path, check=True)

    assert command == RESTORE_ALPHA and "workflow-log" not in command
    assert log.read_text(encoding="utf-8") == '[{"stage": "newest diagnostics"}]\n'
    assert state_files_against_head(setup.state_dir).relation == "committed"


def test_the_check_against_git_runs_no_clean_filter(tmp_path):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    setup.seed()
    _git(tmp_path, "init", "-q", "-b", "main")
    marker = tmp_path / "filter-ran"
    (tmp_path / ".gitattributes").write_text("*.json filter=spy\n", encoding="utf-8")
    _git(tmp_path, "config", "filter.spy.clean", f"touch {marker}; cat")
    _git(tmp_path, "add", "state")
    _git(tmp_path, "commit", "-q", "-m", "state")
    marker.unlink(missing_ok=True)

    state_files_against_head(setup.state_dir)

    assert not marker.exists()


def test_the_check_against_git_cannot_say_without_a_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    setup = Setup(tmp_path, SQLITE_BACKEND)
    setup.seed()
    assert state_files_against_head(setup.state_dir).relation == "unknown"
    _git(tmp_path, "init", "-q", "-b", "main")
    assert state_files_against_head(setup.state_dir).relation == "unknown"


def test_a_database_never_exported_is_one_finding(tmp_path, capsys):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    setup.seed(export=False)

    report = setup.diagnose()

    _assert_one_finding(report, STATE_EXPORT_COMMAND)
    assert report.facts["state_store"]["export_status"] == EXPORT_NEVER
    assert setup.run_health(capsys)[1].count("state_export=never_exported") == 1


def test_a_deleted_exported_file_is_a_mismatch(tmp_path):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    setup.seed()
    (setup.state_dir / shard_relative_path(ALPHA["candidate_id"])).unlink()

    inspection = inspect_state_store(setup.state_dir, setup.selection)

    assert not inspection.consistent
    assert inspection.differing == (shard_relative_path(ALPHA["candidate_id"]).as_posix(),)


def test_json_state_without_a_database_names_the_rebuild_command(tmp_path, capsys):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    setup.seed()
    setup.database.unlink()

    report = setup.diagnose()

    _assert_one_finding(report, STATE_REBUILD_COMMAND)
    assert report.facts["state_store"]["problem"] == "database_missing"
    returncode, lines = setup.run_health(capsys)
    assert returncode == 1 and "state_export_problem=database_missing" in lines


def test_the_comparison_is_by_content_so_a_write_that_changed_nothing_in_the_end_still_matches(tmp_path):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    setup.seed()
    store = setup.store()
    store.upsert_candidate(BETA)
    store.delete_candidate(BETA["candidate_id"])
    assert store.describe()["export_current"] == "no"

    inspection = inspect_state_store(setup.state_dir, setup.selection)

    assert inspection.export_status == EXPORT_CURRENT and inspection.consistent
    assert inspection.change_seq == 3 and inspection.export_change_seq == 1


def test_a_single_file_candidate_history_is_compared_too(tmp_path):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    setup.state_dir.mkdir()
    (setup.state_dir / "candidate-history.json").write_text("[]\n", encoding="utf-8")
    rebuild_from_json(setup.state_dir, selection=setup.selection)
    setup.store().replace(Collection.CANDIDATE_HISTORY, [ALPHA])
    assert inspect_state_store(setup.state_dir, setup.selection).export_status == EXPORT_BEHIND

    export_json(setup.state_dir, setup.selection)

    assert inspect_state_store(setup.state_dir, setup.selection).consistent
    assert read_state(setup.state_dir, Collection.CANDIDATE_HISTORY) is not None


@pytest.mark.parametrize(
    "problem,configure",
    [
        ("backend_unknown", lambda setup: _write_config(setup, {"state_backend": "postgres"})),
        ("database_location", lambda setup: _write_config(setup, _synced_database(setup))),
    ],
)
def test_a_misconfigured_store_is_one_finding_asking_for_the_operator_not_a_crash(tmp_path, problem, configure):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    setup.state_dir.mkdir()
    configure(setup)

    report = setup.diagnose()

    assert report.diagnosis is DiagnosisClass.STATE_STORE_FINDING
    assert report.facts["state_store"]["problem"] == problem
    assert "Дим" in report.recommended_action and "Traceback" not in render_russian(report)
    assert len(render_russian(report).splitlines()) == 4


def _write_config(setup: Setup, extra: dict[str, Any]) -> None:
    setup.config.write_text(json.dumps({"run_date": "2026-01-01", "sources": [], **extra}), encoding="utf-8")


def _synced_database(setup: Setup) -> dict[str, Any]:
    return {"state_backend": SQLITE_BACKEND, "state_database": str(setup.tmp_path / "Dropbox" / "state.sqlite3")}


# ── The finding does not hide what else needs the editor ──────────────────────


def test_a_mismatch_is_not_compared_while_a_run_is_writing(tmp_path):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    _plant_behind(setup)

    report = setup.diagnose(guard_status=lambda: {"guard_exists": True, "guard_pid_alive": True})

    assert report.diagnosis is DiagnosisClass.RUN_IN_PROGRESS
    assert report.facts["state_store"]["export_status"] == "not_checked"


def test_a_mismatch_waits_behind_a_repository_blocker_and_stays_in_the_facts(tmp_path):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    _plant_behind(setup)

    report = setup.diagnose(
        repo_inspect=lambda _allowlist=None: {
            "status": "unsafe",
            "reasons": ["detached_head"],
            "branch": "HEAD",
            "detached_head": True,
        }
    )

    assert report.diagnosis is DiagnosisClass.REPOSITORY_BLOCKER
    assert report.facts["state_store"]["export_status"] == EXPORT_BEHIND
    text = render_russian(report)
    assert "SQLite" in text and "экспорт в Git отстаёт от базы" in text
    assert STATE_EXPORT_COMMAND not in text


def test_doctor_recovery_does_not_offer_to_repair_a_state_store_finding(tmp_path, capsys):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    _plant_behind(setup)
    capsys.readouterr()

    code = main(
        [
            "doctor",
            "--recover",
            "--config",
            str(setup.config),
            "--output-dir",
            str(setup.output_dir),
            "--state-dir",
            str(setup.state_dir),
        ]
    )

    assert code == 1
    assert json.loads(capsys.readouterr().out.split("\n\n", 1)[1])["plan"]["action"] == "blocked"


# ── The doctor's command line ─────────────────────────────────────────────────


def test_the_doctor_command_prints_one_verdict_one_action_and_one_question_for_a_mismatch(tmp_path, capsys):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    _plant_behind(setup)
    capsys.readouterr()

    code = main(
        [
            "doctor",
            "--config",
            str(setup.config),
            "--output-dir",
            str(setup.output_dir),
            "--state-dir",
            str(setup.state_dir),
        ]
    )
    output = capsys.readouterr().out

    assert code == EXIT_BLOCKED
    lines = _russian_part(output)
    assert len(lines) == 4 and sum(STATE_EXPORT_COMMAND in line for line in lines) == 1
    assert str(tmp_path) not in "\n".join(lines)
    assert json.loads(output.split("\n\n", 1)[1])["diagnosis"] == "state-store-finding"


# ── Run-health ────────────────────────────────────────────────────────────────


def test_run_health_fails_on_a_mismatch_even_when_the_run_artifacts_are_healthy(tmp_path, capsys):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    _healthy_artifacts(setup.output_dir)
    setup.seed()

    returncode, lines = setup.run_health(capsys)
    assert returncode == 0 and "state_export=current" in lines

    setup.store().upsert_candidate(BETA)
    returncode, lines = setup.run_health(capsys)
    assert returncode == 1 and "state_export=behind" in lines
    assert "run_health_status=ready" in lines


def test_run_health_command_checks_the_state_store_only_when_asked(tmp_path, capsys):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    _healthy_artifacts(setup.output_dir)
    _plant_behind(setup)
    capsys.readouterr()

    assert main(["run-health", "--output-dir", str(setup.output_dir)]) == 0
    assert "state_export" not in capsys.readouterr().out
    assert (
        main(
            [
                "run-health",
                "--output-dir",
                str(setup.output_dir),
                "--state-dir",
                str(setup.state_dir),
                "--config",
                str(setup.config),
            ]
        )
        == 1
    )
    assert "state_export=behind" in capsys.readouterr().out


def test_run_health_has_nothing_to_compare_on_the_json_backend(tmp_path, capsys):
    setup = Setup(tmp_path, JSON_BACKEND)
    _healthy_artifacts(setup.output_dir)
    setup.seed()

    returncode, lines = setup.run_health(capsys)

    assert returncode == 0
    assert "state_backend=json" in lines and "state_export=not_applicable" in lines
    assert not any(line.startswith("state_database") for line in lines)


# ── The weekly run verifies the export once it is written ─────────────────────


def test_a_weekly_run_stops_before_committing_when_the_export_does_not_match_the_database(tmp_path, monkeypatch):
    from test_weekly_workflow import _write_capture_fixture
    from test_weekly_workflow import _write_codex_fixture_config
    from test_weekly_workflow import _write_extraction_fixture

    from autoscout import weekly_workflow
    from autoscout.weekly_workflow import deterministic_adapters
    from autoscout.weekly_workflow import run_weekly_workflow

    monkeypatch.setenv(STATE_BACKEND_ENV, SQLITE_BACKEND)
    monkeypatch.setenv(STATE_DATABASE_ENV, str(tmp_path / "db.sqlite3"))
    config = _write_codex_fixture_config(tmp_path)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    rebuild_from_json(output_dir)
    _write_capture_fixture(tmp_path, output_dir)
    adapters = deterministic_adapters()
    adapters.capture.prepare(config, output_dir, live=False)
    _write_extraction_fixture(output_dir)

    real_export = weekly_workflow.export_store

    def export_then_corrupt(store):
        report = real_export(store)
        # Something changes a canonical file between the export and the check.
        path = sorted(output_dir.rglob("candidate-history/*/*.json"))[0]
        path.write_text(json.dumps({**json.loads(path.read_text(encoding="utf-8")), "planted": True}), encoding="utf-8")
        return report

    monkeypatch.setattr(weekly_workflow, "export_store", export_then_corrupt)

    result = run_weekly_workflow(config, output_dir, output_dir, adapters=adapters)

    assert result.outcome.value == "intervention-required" and result.failed_stage == "state-export"
    entry = next(entry for entry in result.log if entry["stage"] == "state-export")
    assert entry["status"] == "failed" and entry["detail"]["reason"] == "state_export_mismatch"
    assert entry["detail"]["export_status"] == EXPORT_CHANGED_OUTSIDE and entry["detail"]["differing_count"] == 1
    assert not any(entry.get("stage") == "repository-finalize" for entry in result.log)


def test_a_weekly_run_under_sqlite_ends_with_the_database_and_the_export_in_agreement(tmp_path, monkeypatch):
    from test_weekly_workflow import _write_capture_fixture
    from test_weekly_workflow import _write_codex_fixture_config
    from test_weekly_workflow import _write_extraction_fixture

    from autoscout.weekly_workflow import deterministic_adapters
    from autoscout.weekly_workflow import run_weekly_workflow

    monkeypatch.setenv(STATE_BACKEND_ENV, SQLITE_BACKEND)
    monkeypatch.setenv(STATE_DATABASE_ENV, str(tmp_path / "db.sqlite3"))
    config = _write_codex_fixture_config(tmp_path)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    rebuild_from_json(output_dir)
    _write_capture_fixture(tmp_path, output_dir)
    adapters = deterministic_adapters()
    adapters.capture.prepare(config, output_dir, live=False)
    _write_extraction_fixture(output_dir)

    result = run_weekly_workflow(config, output_dir, output_dir, adapters=adapters)

    assert result.outcome.value.startswith("completed/"), result.failed_stage
    inspection = inspect_state_store(output_dir)
    assert inspection.consistent and inspection.export_status == EXPORT_CURRENT


# ── Review findings of round 1 ────────────────────────────────────────────────


def test_a_reformatted_shard_with_the_same_record_is_not_a_mismatch_an_export_could_repair(tmp_path):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    setup.seed()
    shard = setup.state_dir / shard_relative_path(ALPHA["candidate_id"])
    shard.write_text(json.dumps(json.loads(shard.read_text(encoding="utf-8")), indent=8), encoding="utf-8")
    # The export leaves it alone, so the check must not block on it.
    assert export_json(setup.state_dir, setup.selection).files_written == 0

    assert inspect_state_store(setup.state_dir, setup.selection).consistent


def test_a_recovered_database_and_wal_pair_without_shared_memory_is_read_without_creating_files(tmp_path, monkeypatch):
    import tempfile

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    setup = Setup(tmp_path, SQLITE_BACKEND)
    setup.seed()
    recovered = tmp_path / "recovered" / "state.sqlite3"
    recovered.parent.mkdir()
    held = sqlite3.connect(setup.database)
    try:
        held.execute("PRAGMA wal_autocheckpoint = 0")
        # A write that stays in the write-ahead log: a change marker the main file does not have yet.
        held.execute("UPDATE store_meta SET value = CAST(value AS INTEGER) + 1 WHERE name = 'change_seq'")
        held.commit()
        wal = setup.database.with_name(setup.database.name + "-wal")
        assert wal.stat().st_size
        recovered.write_bytes(setup.database.read_bytes())
        recovered.with_name(recovered.name + "-wal").write_bytes(wal.read_bytes())
    finally:
        held.close()
    before = _tree(recovered.parent)

    inspection = inspect_state_store(setup.state_dir, BackendSelection(SQLITE_BACKEND, "config", recovered))

    assert inspection.change_seq == 2 and inspection.export_change_seq == 1
    assert _tree(recovered.parent) == before
    assert not recovered.with_name(recovered.name + "-shm").exists()
    # The scratch copy the read used is gone.
    assert not list(scratch.iterdir())


# ── Review findings of round 2 ────────────────────────────────────────────────


def test_a_shard_edit_that_changes_a_json_type_is_still_a_mismatch(tmp_path):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    setup.state_dir.mkdir()
    setup.store().replace(Collection.CANDIDATE_HISTORY, [{**ALPHA, "flag": False}])
    export_json(setup.state_dir, setup.selection)
    shard = setup.state_dir / shard_relative_path(ALPHA["candidate_id"])
    shard.write_text(shard.read_text(encoding="utf-8").replace("false", "0"), encoding="utf-8")

    inspection = inspect_state_store(setup.state_dir, setup.selection)

    assert not inspection.consistent and inspection.differing_count == 1


def test_a_database_without_its_change_marker_is_a_finding_not_a_crash(tmp_path, capsys):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    setup.seed()
    with sqlite3.connect(setup.database) as conn:
        conn.execute("DELETE FROM store_meta WHERE name = 'change_seq'")

    report = setup.diagnose()

    assert report.diagnosis is DiagnosisClass.STATE_STORE_FINDING
    assert report.facts["state_store"]["problem"] == "database_unusable"
    returncode, lines = setup.run_health(capsys)
    assert returncode == 1 and "state_export_problem=database_unusable" in lines


# ── Review findings of round 3 ────────────────────────────────────────────────


def test_a_database_reached_through_a_symbolic_link_is_read_with_its_own_write_ahead_log(tmp_path):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    setup.seed()
    link = tmp_path / "linked.sqlite3"
    link.symlink_to(setup.database)
    held = sqlite3.connect(setup.database)
    try:
        held.execute("PRAGMA wal_autocheckpoint = 0")
        held.execute("UPDATE store_meta SET value = CAST(value AS INTEGER) + 1 WHERE name = 'change_seq'")
        held.commit()

        inspection = inspect_state_store(setup.state_dir, BackendSelection(SQLITE_BACKEND, "config", link))
    finally:
        held.close()

    assert inspection.change_seq == 2


def test_a_record_that_is_not_an_object_is_a_finding_not_a_crash(tmp_path):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    setup.seed()
    with sqlite3.connect(setup.database) as conn:
        conn.execute("UPDATE candidate_history SET record_json = '[]'")

    report = setup.diagnose()

    assert report.diagnosis is DiagnosisClass.STATE_STORE_FINDING
    assert report.facts["state_store"]["problem"] in {"database_unusable", "state_unreadable"}


# ── A state directory cut over to SQLite (#1093) ──────────────────────────────


def _cut_over(setup: Setup) -> None:
    from autoscout.state_store import cut_over_to_sqlite

    setup.seed(export=False)
    cut_over_to_sqlite(setup.state_dir, setup.selection)


def _config_without_backend(setup: Setup, **extra: Any) -> None:
    """A weekly config that names no backend, only where the database is kept."""
    _write_config(setup, {"state_database": str(setup.database), **extra})


def test_the_marker_decides_the_backend_and_the_doctor_says_so(tmp_path):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    _cut_over(setup)
    _config_without_backend(setup)

    report = setup.diagnose()

    assert report.diagnosis is DiagnosisClass.HEALTHY
    assert report.facts["state_store"]["backend"] == SQLITE_BACKEND
    assert report.facts["state_store"]["selected_by"] == "state_directory"
    line = render_russian(report).splitlines()[1]
    assert "задано отметкой в папке состояния" in line and "экспорт в Git актуален" in line


def test_a_json_selection_over_a_marked_directory_is_one_finding_not_a_crash(tmp_path, capsys):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    _cut_over(setup)
    _config_without_backend(setup, state_backend=JSON_BACKEND)
    before = _tree(tmp_path)

    report = setup.diagnose()

    assert report.diagnosis is DiagnosisClass.STATE_STORE_FINDING
    assert report.facts["state_store"]["problem"] == "backend_conflict"
    lines = render_russian(report).splitlines()
    assert len(lines) == 4 and "state_backend" in report.recommended_action
    returncode, output = setup.run_health(capsys)
    assert returncode == 1 and "state_export_problem=backend_conflict" in output
    assert _tree(tmp_path) == before


def test_the_environment_selecting_json_over_a_marked_directory_is_the_same_finding(tmp_path, monkeypatch):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    _cut_over(setup)
    _config_without_backend(setup)
    monkeypatch.setenv(STATE_BACKEND_ENV, JSON_BACKEND)

    assert setup.diagnose().facts["state_store"]["problem"] == "backend_conflict"


def test_a_marked_directory_without_a_database_on_this_machine_names_the_rebuild_command(tmp_path, capsys):
    setup = Setup(tmp_path, SQLITE_BACKEND)
    _cut_over(setup)
    setup.database.unlink()
    setup.database.parent.rmdir()
    _config_without_backend(setup)
    before = _tree(tmp_path)

    report = setup.diagnose()

    lines = _assert_one_finding(report, STATE_REBUILD_COMMAND)
    assert report.facts["state_store"]["problem"] == "database_missing"
    assert "переведена на SQLite" in lines[0] and "базы на этом компьютере нет" in lines[0]
    returncode, output = setup.run_health(capsys)
    assert returncode == 1 and "state_backend_selected_by=state_directory" in output
    assert _tree(tmp_path) == before


def test_an_unreadable_marker_is_one_finding_for_the_operator(tmp_path):
    from autoscout.state_store.marker import marker_path

    setup = Setup(tmp_path, SQLITE_BACKEND)
    setup.seed()
    marker_path(setup.state_dir).write_text("not json", encoding="utf-8")

    report = setup.diagnose()

    assert report.diagnosis is DiagnosisClass.STATE_STORE_FINDING
    assert report.facts["state_store"]["problem"] == "backend_marker_unreadable"
    assert "Дим" in report.recommended_action
