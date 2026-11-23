"""A State Store refusal is one clean line and exit 2 from every command that opens a state directory (#1098).

A state directory marked as kept in SQLite (``state-backend.json``) on a
machine that has no database for it, and a marker that disagrees with the
backend the environment selects, are refused by the State Store. The CLI turns
that refusal into the line ``state-database`` prints: one ``error=`` line,
exit 2, no traceback, and nothing written. A command that maps the refusal
to its own documented output keeps it, and the tests below pin which.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from state_store_support import state_snapshot

from autoscout.__main__ import main
from autoscout.git_envelope import GUARD_FILENAME
from autoscout.state_store import JSON_BACKEND
from autoscout.state_store import SQLITE_BACKEND
from autoscout.state_store import STATE_BACKEND_ENV
from autoscout.state_store import STATE_DATABASE_ENV
from autoscout.state_store import BackendSelection
from autoscout.state_store import Collection
from autoscout.state_store import cut_over_to_sqlite
from autoscout.state_store import rebuild_from_json
from autoscout.state_store import state_database_path
from autoscout.state_store.marker import BACKEND_MARKER_FILE

REPOSITORY = Path(__file__).resolve().parents[1]
FIXTURE_CONFIG = REPOSITORY / "configs" / "weekly-mvp-fixture.json"
SQLITE = BackendSelection(SQLITE_BACKEND, "default")
REBUILD_COMMAND = "./scripts/state-database rebuild"


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


def _run_directory(tmp_path: Path) -> Path:
    """A run directory with the fixture run's artifacts, which is also its state directory."""
    directory = tmp_path / "run"
    directory.mkdir()
    assert main(["run-mvp", "--config", str(FIXTURE_CONFIG), "--output-dir", str(directory)]) == 0
    return directory


def _marked_without_database(tmp_path: Path) -> Path:
    """A checkout that pulled a cut-over state directory: its marker and JSON, and no database."""
    directory = _run_directory(tmp_path)
    rebuild_from_json(directory, selection=SQLITE)
    cut_over_to_sqlite(directory)
    state_database_path(directory).unlink()
    return directory


def _candidate_key(directory: Path) -> str:
    """The identity key of a book of the fixture run, from its Candidate History file."""
    history = json.loads((directory / "candidate-history.json").read_text(encoding="utf-8"))
    return str(history[0]["identity_key"])


def _written(directory: Path) -> tuple[dict[str, bytes], Path]:
    return state_snapshot(directory), state_database_path(directory)


def _assert_one_clean_refusal(capsys, expected_text: str) -> None:
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1 and lines[0].startswith("error="), captured.out
    assert expected_text in lines[0]
    assert "Traceback" not in captured.out + captured.err
    assert captured.err == ""


# The commands that open a state directory and leave a State Store refusal to the CLI.
# ``{key}`` is a candidate of the fixture run; ``{state}`` is the state directory.
TOP_LEVEL_COMMANDS = {
    "editorial-profile list": "editorial-profile list --state-dir {state}",
    "editorial-profile add preview": (
        "editorial-profile add --kind hard-rule --scope author --subject X --quote q --provenance p --state-dir {state}"
    ),
    "editorial-profile add confirmed": (
        "editorial-profile add --kind hard-rule --scope author --subject X --quote q --provenance p "
        "--state-dir {state} --confirmed-by-editor"
    ),
    "editorial-profile revoke": "editorial-profile revoke --id x --reason r --state-dir {state}",
    "editorial-profile set-terms": "editorial-profile set-terms --id x --match-term t --state-dir {state}",
    "record-visibility": "record-visibility --identity-key {key} --action hide --state-dir {state}",
    "record-feedback preview": "record-feedback --identity-key {key} --status interested --state-dir {state}",
    "record-feedback confirmed": (
        "record-feedback --identity-key {key} --status interested --state-dir {state} --confirmed-by-editor"
    ),
    "render-review": "render-review --output-dir {state}",
    "render-review with state": "render-review --output-dir {state} --state-dir {state}",
    "run-mvp": f"run-mvp --config {FIXTURE_CONFIG} --output-dir {{state}} --state-dir {{state}}",
    "ingest-feedback": "ingest-feedback --csv {state}/editor-facing.csv --state-dir {state}",
    "source-proposal add": "source-proposal add --source-url https://example.com --reason r --state-dir {state}",
    "source-proposal reject": "source-proposal reject --id x --reason r --state-dir {state}",
    "complete-isbns": "complete-isbns --state-dir {state}",
    "complete-originals": "complete-originals --state-dir {state}",
    "complete-work-age": "complete-work-age --state-dir {state}",
    "refresh-russian-editions": "refresh-russian-editions --state-dir {state}",
    "prepare-description-backfill": "prepare-description-backfill --state-dir {state} --output-file {state}/o.json",
    "apply-description-backfill": "apply-description-backfill --state-dir {state} --artifacts-file {state}/a.json",
    "editorial-fit-shadow report": "editorial-fit-shadow report --output-dir {state} --state-dir {state}",
    "ranking-benchmark": "ranking-benchmark --output-dir {state} --state-dir {state}",
    "work-age-report": "work-age-report --output-dir {state} --state-dir {state} --report-file {state}/../r.md",
}


def _argv(template: str, state: Path, key: str) -> list[str]:
    return shlex.split(template.format(state=state, key=shlex.quote(key)))


@pytest.mark.parametrize("name", list(TOP_LEVEL_COMMANDS))
def test_a_marked_directory_without_a_database_is_one_clean_refusal_from_every_command(tmp_path, capsys, name):
    directory = _marked_without_database(tmp_path)
    argv = _argv(TOP_LEVEL_COMMANDS[name], directory, _candidate_key(directory))
    before, database = _written(directory)
    databases_before = sorted(database.parent.iterdir())

    returncode = main(argv)

    assert returncode == 2
    _assert_one_clean_refusal(capsys, REBUILD_COMMAND)
    assert state_snapshot(directory) == before
    assert not database.exists() and sorted(database.parent.iterdir()) == databases_before


def test_the_refusal_is_the_same_line_state_database_prints(tmp_path, capsys):
    directory = _marked_without_database(tmp_path)
    capsys.readouterr()

    assert main(["state-database", "export", "--state-dir", str(directory)]) == 2
    from_state_database = [line for line in capsys.readouterr().out.splitlines() if line.startswith("error=")]
    assert main(["editorial-profile", "list", "--state-dir", str(directory)]) == 2

    assert capsys.readouterr().out.splitlines() == from_state_database


def test_a_real_process_ends_without_a_traceback(tmp_path):
    directory = _marked_without_database(tmp_path)
    environment = {**os.environ, "AUTOSCOUT_STATE_DATABASE_DIR": str(tmp_path / "databases")}

    completed = subprocess.run(
        [sys.executable, "-m", "autoscout", "editorial-profile", "list", "--state-dir", str(directory)],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        cwd=REPOSITORY,
    )

    assert completed.returncode == 2
    assert completed.stderr == "" and "Traceback" not in completed.stdout
    assert len(completed.stdout.splitlines()) == 1 and REBUILD_COMMAND in completed.stdout


def test_a_backend_conflict_is_one_clean_refusal_and_writes_nothing(tmp_path, capsys, monkeypatch):
    directory = _run_directory(tmp_path)
    rebuild_from_json(directory, selection=SQLITE)
    cut_over_to_sqlite(directory)
    before, _database = _written(directory)
    monkeypatch.setenv(STATE_BACKEND_ENV, JSON_BACKEND)
    capsys.readouterr()

    key = _candidate_key(directory)
    returncode = main(["record-visibility", "--identity-key", key, "--action", "hide", "--state-dir", str(directory)])

    assert returncode == 2
    _assert_one_clean_refusal(capsys, "state-database rollback")
    assert state_snapshot(directory) == before


def test_an_unmarked_directory_is_unaffected(tmp_path, capsys):
    directory = _run_directory(tmp_path)
    capsys.readouterr()

    assert main(["editorial-profile", "list", "--state-dir", str(directory)]) == 0
    assert not (directory / BACKEND_MARKER_FILE).exists()


def test_a_preview_still_exits_3_on_a_cut_over_directory_with_its_database(tmp_path, capsys):
    directory = _run_directory(tmp_path)
    rebuild_from_json(directory, selection=SQLITE)
    cut_over_to_sqlite(directory)
    key = _candidate_key(directory)
    capsys.readouterr()

    returncode = main(
        ["record-feedback", "--identity-key", key, "--status", "interested", "--state-dir", str(directory)]
    )

    assert returncode == 3
    assert "confirmation_required=true" in capsys.readouterr().out


def test_a_live_weekly_run_still_exits_6_before_the_store_is_opened(tmp_path, capsys):
    directory = _marked_without_database(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / GUARD_FILENAME).write_text(str(os.getppid()))
    capsys.readouterr()

    returncode = main(["record-visibility", "--identity-key", "x", "--action", "hide", "--state-dir", str(directory)])

    assert returncode == 6
    assert capsys.readouterr().out.splitlines()[0] == "reason_code=weekly_run_in_progress"


# The commands that already map the refusal to their own documented output keep it.
OWN_CONTRACT_COMMANDS = {
    "probable-duplicates": ("probable-duplicates --state-dir {state}", 2, '"status": "input_error"'),
    "resolve-review-rows": (
        "resolve-review-rows --state-dir {state} --rows {state}/editor-facing.csv",
        2,
        '"status": "input_error"',
    ),
    "source-yield-report": ("source-yield-report --state-dir {state}", 2, "error: "),
    "migrate-candidate-history dry run": (
        "migrate-candidate-history --state-dir {state}",
        1,
        "error=candidate_history_",
    ),
    "migrate-candidate-history apply": (
        "migrate-candidate-history --state-dir {state} --apply",
        2,
        "error=candidate_history_",
    ),
    "adjudicate-descriptions": (
        "adjudicate-descriptions --state-dir {state} --output-dir {state}",
        1,
        "adjudicate-descriptions: ",
    ),
    "state-database": ("state-database export --state-dir {state}", 2, "state_database_export=refused"),
}


@pytest.mark.parametrize("name", list(OWN_CONTRACT_COMMANDS))
def test_a_command_that_maps_the_refusal_itself_keeps_its_output_and_exit_code(tmp_path, capsys, name):
    template, expected_code, expected_text = OWN_CONTRACT_COMMANDS[name]
    directory = _marked_without_database(tmp_path)
    before, database = _written(directory)
    capsys.readouterr()

    returncode = main(_argv(template, directory, ""))

    captured = capsys.readouterr()
    assert returncode == expected_code
    assert expected_text in captured.out + captured.err
    assert REBUILD_COMMAND in captured.out + captured.err
    assert "Traceback" not in captured.out + captured.err
    assert state_snapshot(directory) == before
    assert not database.exists()


def test_a_refusal_the_store_makes_without_a_database_leaves_the_collections_unread_as_empty(tmp_path):
    directory = _marked_without_database(tmp_path)

    from autoscout.state_store import StateStoreError
    from autoscout.state_store import open_state_store

    with pytest.raises(StateStoreError, match="state-database rebuild"):
        open_state_store(directory).read(Collection.EDITORIAL_PROFILE)
