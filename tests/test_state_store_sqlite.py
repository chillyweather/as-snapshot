"""The SQLite backend's rebuild, export and storage promises (#1074, ADR 0023).

The State Store contract itself runs over both backends in
``test_state_store.py``. These tests cover what the SQLite backend adds:
the JSON export Git keeps, the rebuild of a database from JSON, the decision
log, the change marker, concurrent writers, and where the database may live.
They observe state through the State Store, the exported JSON and the
commands, except the one test that reads the decision log as the readers of
#1053 will, through the ``decision_log`` view.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from state_store_support import LAYOUTS
from state_store_support import SHARDED
from state_store_support import SINGLE_FILE
from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import store_selection
from state_store_support import use_layout

from autoscout.__main__ import main
from autoscout.editorial_taste_profile import TasteProfileError
from autoscout.state_store import JSON_BACKEND
from autoscout.state_store import SQLITE_BACKEND
from autoscout.state_store import STATE_BACKEND_ENV
from autoscout.state_store import STATE_BACKENDS
from autoscout.state_store import STATE_DATABASE_ENV
from autoscout.state_store import BackendSelection
from autoscout.state_store import Collection
from autoscout.state_store import StateStoreError
from autoscout.state_store import export_json
from autoscout.state_store import open_state_store
from autoscout.state_store import rebuild_from_json
from autoscout.state_store import select_state_backend
from autoscout.state_store import state_database_path
from autoscout.state_store.conversion import convert_to_sharded
from autoscout.state_store.json_backend import FILE_NAMES
from autoscout.state_store.layout import CANDIDATE_HISTORY_DIR
from autoscout.state_store.layout import CANDIDATE_HISTORY_FILE
from autoscout.state_store.layout import shard_relative_path
from autoscout.state_store.sqlite_location import DATABASE_DIR_ENV
from autoscout.state_store.sqlite_location import DEFAULT_DATABASE_DIR
from autoscout.state_store.sqlite_location import LOCAL
from autoscout.state_store.sqlite_location import NETWORK
from autoscout.state_store.sqlite_location import REPOSITORY_ROOT
from autoscout.state_store.sqlite_location import SYNCED
from autoscout.state_store.sqlite_location import database_path_class
from autoscout.state_store.sqlite_location import default_database_path

GOLDEN_STATE = Path(__file__).resolve().parent / "fixtures" / "golden-run" / "state-after-each-step.json"
GOLDEN_STEPS: dict[str, dict[str, str]] = json.loads(GOLDEN_STATE.read_text(encoding="utf-8"))

ALPHA = {"candidate_id": "candidate-aaaaaaaaaaaaaaaaaaaaaaaa", "identity_key": "alpha|one", "title": "Alpha"}
BETA = {"candidate_id": "candidate-bbbbbbbbbbbbbbbbbbbbbbbb", "identity_key": "beta|two", "title": "Бета"}
GAMMA = {"candidate_id": "candidate-cccccccccccccccccccccccc", "identity_key": "gamma|three", "title": "Gamma"}

DECISIONS = [
    {"decision_date": None, "description_digest": None, "note": "", "status": "maybe_watch"},
    {"decision_date": "2026-11-01", "description_digest": "", "note": "Позже", "status": "reject"},
    {"decision_date": "2026-11-01", "description_digest": "", "note": "Позже", "status": "reject"},
    {"decision_date": "2026-11-02", "description_digest": "0123456789abcdef", "note": "Да", "status": "interested"},
]
DECIDED = {
    **BETA,
    "editor_decisions": DECISIONS,
    "editor_note": "Да",
    "editor_status": "interested",
    "primary_authors": ["Анна Автор", "B. Writer"],
    "zz_last": {"b": 1, "a": [2.5, None, True]},
}


def _state_files(state_dir: Path) -> dict[str, bytes]:
    """Every file under *state_dir* by its relative path."""
    return {
        path.relative_to(state_dir).as_posix(): path.read_bytes()
        for path in sorted(state_dir.rglob("*"))
        if path.is_file()
    }


def _write_golden_step(state_dir: Path, step: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    for name, content in GOLDEN_STEPS[step].items():
        (state_dir / name).write_text(content, encoding="utf-8")


def _wipe_canonical_files(state_dir: Path) -> None:
    """Remove every canonical file; the single-file layout keeps an empty history file so it stays that layout."""
    single_file = (state_dir / CANDIDATE_HISTORY_FILE).exists()
    for name in FILE_NAMES.values():
        (state_dir / name).unlink(missing_ok=True)
    for path in sorted((state_dir / CANDIDATE_HISTORY_DIR).rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    if (state_dir / CANDIDATE_HISTORY_DIR).exists():
        (state_dir / CANDIDATE_HISTORY_DIR).rmdir()
    if single_file:
        (state_dir / CANDIDATE_HISTORY_FILE).write_text("[]\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def _explicit_selections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test here chooses its stores' backend itself, so the run's ``--state-backend`` does not apply (#1075).

    A default database a test opens lands in its own directory.
    """
    monkeypatch.delenv(STATE_BACKEND_ENV, raising=False)
    monkeypatch.setenv(DATABASE_DIR_ENV, str(tmp_path / "default-databases"))


def _sqlite(tmp_path: Path) -> BackendSelection:
    return store_selection(SQLITE_BACKEND, tmp_path)


# ── export(rebuild(json)) ─────────────────────────────────────────────────────


@pytest.mark.parametrize("step", list(GOLDEN_STEPS))
@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("backend", STATE_BACKENDS)
def test_export_of_a_rebuild_reproduces_the_golden_state_byte_for_byte(tmp_path, backend, layout, step):
    state_dir = tmp_path / "state"
    _write_golden_step(state_dir, step)
    if layout == SHARDED:
        convert_to_sharded(state_dir)
    original = _state_files(state_dir)
    selection = store_selection(backend, tmp_path)

    if backend == SQLITE_BACKEND:
        report = rebuild_from_json(state_dir, selection=selection, require_canonical=True)
        assert report.non_canonical == []
        # The database alone is enough: the export writes every file back.
        _wipe_canonical_files(state_dir)
        assert export_json(state_dir, selection, force=True).status == "exported"
    else:
        # The JSON backend's state directory is its own export.
        assert export_json(state_dir, selection) is None

    assert _state_files(state_dir) == original


@pytest.mark.parametrize("layout", LAYOUTS)
def test_two_exports_of_the_same_state_are_byte_identical(tmp_path, layout):
    state_dir = tmp_path / "state"
    use_layout(state_dir, layout)
    selection = _sqlite(tmp_path)
    rebuild_from_json(state_dir, selection=selection)
    store = open_state_store(state_dir, selection)
    store.replace(Collection.CANDIDATE_HISTORY, [GAMMA, DECIDED, ALPHA])
    store.replace(Collection.ARCHIVED_EDITOR_ROWS, [{"identity_key": "beta|two", "Editor Status": "reject"}])

    first = export_json(state_dir, selection)
    exported = _state_files(state_dir)
    second = export_json(state_dir, selection)

    assert first.status == "exported" and second.status == "unchanged"
    assert _state_files(state_dir) == exported
    _wipe_canonical_files(state_dir)
    export_json(state_dir, selection, force=True)
    assert _state_files(state_dir) == exported


def test_the_export_is_the_json_backend_s_own_bytes_for_the_same_writes(tmp_path):
    json_dir = tmp_path / "json"
    sqlite_dir = tmp_path / "sqlite"
    selection = _sqlite(tmp_path)
    writes: list[tuple[Collection, Any]] = [
        (Collection.CANDIDATE_HISTORY, [GAMMA, DECIDED]),
        (Collection.CANDIDATE_VISIBILITY, [{"identity_key": "gamma|three", "visibility": "hidden"}]),
        (Collection.EDITORIAL_PROFILE, {"entries": []}),
        (Collection.PENDING_EDITOR_ROWS, []),
    ]
    for store in (
        open_state_store(json_dir, BackendSelection(JSON_BACKEND, "default")),
        open_state_store(sqlite_dir, selection),
    ):
        with store.transaction():
            for collection, value in writes:
                store.replace(collection, value)
            store.upsert_candidate(ALPHA)
            store.delete_candidate(GAMMA["candidate_id"])

    export_json(sqlite_dir, selection)

    assert _state_files(sqlite_dir) == _state_files(json_dir)


def test_a_record_reads_back_with_its_decision_log_and_key_order_unchanged(tmp_path):
    state_dir = tmp_path / "state"
    selection = _sqlite(tmp_path)
    open_state_store(state_dir, selection).upsert_candidate(DECIDED)

    record = open_state_store(state_dir, selection).get_candidate(DECIDED["candidate_id"])

    assert record == DECIDED
    assert list(record) == sorted(record)
    assert [list(entry) for entry in record["editor_decisions"]] == [sorted(entry) for entry in DECISIONS]
    export_json(state_dir, selection)
    shard = state_dir.joinpath(*shard_relative_path(DECIDED["candidate_id"]).parts)
    expected = json.dumps(DECIDED, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    assert shard.read_bytes() == expected.encode("utf-8")


def test_the_decision_log_is_its_own_table_with_null_and_empty_kept_apart(tmp_path):
    state_dir = tmp_path / "state"
    selection = _sqlite(tmp_path)
    open_state_store(state_dir, selection).replace(Collection.CANDIDATE_HISTORY, [ALPHA, DECIDED])

    with sqlite3.connect(state_database_path(state_dir, selection)) as conn:
        rows = conn.execute(
            "SELECT candidate_id, status, note, decision_date, description_digest FROM decision_log "
            "ORDER BY candidate_id, position"
        ).fetchall()
        stored_blob = conn.execute(
            "SELECT record_json FROM candidate_history WHERE candidate_id = ?", (DECIDED["candidate_id"],)
        ).fetchone()[0]
    conn.close()

    assert rows == [
        (DECIDED["candidate_id"], entry["status"], entry["note"], entry["decision_date"], entry["description_digest"])
        for entry in DECISIONS
    ]
    assert "editor_decisions" not in json.loads(stored_blob)


@pytest.mark.parametrize(
    "log",
    [
        "not a list",
        [{"status": "reject", "note": ""}],
        [{"status": "reject", "note": "", "decision_date": None, "description_digest": None, "extra": 1}],
        [{"status": "reject", "note": 5, "decision_date": None, "description_digest": None}],
    ],
)
def test_a_decision_log_the_table_cannot_hold_is_refused_and_nothing_is_written(tmp_path, log):
    state_dir = tmp_path / "state"
    selection = _sqlite(tmp_path)
    store = open_state_store(state_dir, selection)
    store.replace(Collection.CANDIDATE_HISTORY, [ALPHA])

    with pytest.raises(StateStoreError, match="decision log"):
        store.upsert_candidate({**BETA, "editor_decisions": log})

    assert open_state_store(state_dir, selection).read(Collection.CANDIDATE_HISTORY) == [ALPHA]


def test_records_without_a_candidate_id_or_sharing_one_are_kept_and_exported_to_a_single_file(tmp_path):
    state_dir = tmp_path / "state"
    use_layout(state_dir, SINGLE_FILE)
    selection = _sqlite(tmp_path)
    rebuild_from_json(state_dir, selection=selection)
    history = [
        {"candidate_id": "made-up-1", "identity_key": "a|1"},
        {"candidate_id": "made-up-1", "identity_key": "a|2"},
        {"identity_key": "no|id", "title": "No ID"},
    ]
    open_state_store(state_dir, selection).replace(Collection.CANDIDATE_HISTORY, history)

    assert open_state_store(state_dir, selection).read(Collection.CANDIDATE_HISTORY) == history
    assert open_state_store(state_dir, selection).get_candidate("made-up-1") == history[0]
    export_json(state_dir, selection)
    assert json.loads((state_dir / CANDIDATE_HISTORY_FILE).read_text(encoding="utf-8")) == history


def test_the_export_to_one_file_per_book_refuses_records_it_cannot_name(tmp_path):
    state_dir = tmp_path / "state"
    selection = _sqlite(tmp_path)
    open_state_store(state_dir, selection).replace(Collection.CANDIDATE_HISTORY, [ALPHA, {"identity_key": "no|id"}])

    with pytest.raises(StateStoreError, match="one file per book"):
        export_json(state_dir, selection)

    assert not (state_dir / CANDIDATE_HISTORY_DIR).exists()


# ── The change marker ─────────────────────────────────────────────────────────


def _shard(state_dir: Path, record: dict[str, Any]) -> Path:
    return state_dir.joinpath(*shard_relative_path(record["candidate_id"]).parts)


def test_the_export_rewrites_only_the_files_of_changed_books_and_removes_deleted_ones(tmp_path):
    state_dir = tmp_path / "state"
    selection = _sqlite(tmp_path)
    store = open_state_store(state_dir, selection)
    store.replace(Collection.CANDIDATE_HISTORY, [ALPHA, BETA, GAMMA])
    export_json(state_dir, selection)
    untouched = {path: path.stat().st_ino for path in (_shard(state_dir, ALPHA), _shard(state_dir, GAMMA))}
    before = int(store.describe()["change_seq"])

    store.replace(Collection.CANDIDATE_HISTORY, [ALPHA, {**BETA, "editor_status": "reject"}, GAMMA])
    store.upsert_candidate(ALPHA)
    assert int(store.describe()["change_seq"]) == before + 1
    store.delete_candidate(GAMMA["candidate_id"])
    report = export_json(state_dir, selection)

    assert (report.mode, report.files_written, report.files_deleted) == ("incremental", 1, 1)
    assert _shard(state_dir, ALPHA).stat().st_ino == untouched[_shard(state_dir, ALPHA)]
    assert not _shard(state_dir, GAMMA).exists()
    assert read_state(state_dir, Collection.CANDIDATE_HISTORY) == [ALPHA, {**BETA, "editor_status": "reject"}]


def test_an_unchanged_write_keeps_the_change_marker(tmp_path):
    state_dir = tmp_path / "state"
    store = open_state_store(state_dir, _sqlite(tmp_path))
    store.replace(Collection.CANDIDATE_HISTORY, [ALPHA, DECIDED])
    store.replace(Collection.CANDIDATE_VISIBILITY, [{"identity_key": "alpha|one"}])
    marker = store.describe()["change_seq"]

    store.replace(Collection.CANDIDATE_HISTORY, [DECIDED, ALPHA])
    store.upsert_candidate(dict(DECIDED))
    store.replace(Collection.CANDIDATE_VISIBILITY, [{"identity_key": "alpha|one"}])

    assert store.describe()["change_seq"] == marker


def test_the_export_refuses_a_state_directory_changed_outside_the_database(tmp_path):
    state_dir = tmp_path / "state"
    selection = _sqlite(tmp_path)
    store = open_state_store(state_dir, selection)
    store.replace(Collection.CANDIDATE_HISTORY, [ALPHA, BETA])
    export_json(state_dir, selection)
    changed = json.dumps({**ALPHA, "title": "Changed in Git"}, indent=2, sort_keys=True) + "\n"
    _shard(state_dir, ALPHA).write_text(changed, encoding="utf-8")
    store.upsert_candidate({**BETA, "editor_status": "reject"})

    with pytest.raises(StateStoreError, match="changed since the database was last exported"):
        export_json(state_dir, selection)
    assert _shard(state_dir, ALPHA).read_text(encoding="utf-8") == changed

    assert export_json(state_dir, selection, force=True).mode == "full"
    assert read_state(state_dir, Collection.CANDIDATE_HISTORY) == [ALPHA, {**BETA, "editor_status": "reject"}]


def test_a_layout_conversion_keeps_the_export_current_but_an_edit_with_it_is_refused(tmp_path):
    state_dir = tmp_path / "state"
    use_layout(state_dir, SINGLE_FILE)
    selection = _sqlite(tmp_path)
    rebuild_from_json(state_dir, selection=selection)
    store = open_state_store(state_dir, selection)
    store.replace(Collection.CANDIDATE_HISTORY, [ALPHA, BETA])
    export_json(state_dir, selection)

    convert_to_sharded(state_dir)
    report = export_json(state_dir, selection)
    assert (report.status, report.mode, report.layout) == ("unchanged", "full", SHARDED)

    edited = tmp_path / "edited"
    use_layout(edited, SINGLE_FILE)
    edited_selection = BackendSelection(SQLITE_BACKEND, "default", tmp_path / "edited.sqlite3")
    rebuild_from_json(edited, selection=edited_selection)
    open_state_store(edited, edited_selection).replace(Collection.CANDIDATE_HISTORY, [ALPHA, BETA])
    export_json(edited, edited_selection)
    changed = [ALPHA, {**BETA, "title": "Edited before the conversion"}]
    (edited / CANDIDATE_HISTORY_FILE).write_text(json.dumps(changed, indent=2, sort_keys=True), encoding="utf-8")
    convert_to_sharded(edited)
    with pytest.raises(StateStoreError, match="changed since the database was last exported"):
        export_json(edited, edited_selection)
    assert read_state(edited, Collection.CANDIDATE_HISTORY) == changed


def test_describe_reports_the_database_and_whether_the_export_is_current(tmp_path):
    state_dir = tmp_path / "state"
    selection = _sqlite(tmp_path)
    store = open_state_store(state_dir, selection)
    assert store.describe()["export_current"] == "no_database"

    store.replace(Collection.CANDIDATE_HISTORY, [ALPHA])
    described = store.describe()
    assert {key: described[key] for key in ("backend", "schema_version", "export_current")} == {
        "backend": "sqlite",
        "schema_version": "2",
        "export_current": "never",
    }
    assert described["database"] == state_database_path(state_dir, selection).as_posix()
    assert described["database_path_class"] in {LOCAL, "unknown"}
    export_json(state_dir, selection)
    assert store.describe()["export_current"] == "yes"
    store.upsert_candidate(BETA)
    assert store.describe()["export_current"] == "no"


# ── Rebuild ───────────────────────────────────────────────────────────────────


def test_rebuild_refuses_an_existing_database_unless_told_to_overwrite_it(tmp_path):
    state_dir = tmp_path / "state"
    use_layout(state_dir, SINGLE_FILE)
    selection = _sqlite(tmp_path)
    seed_state(state_dir, candidate_history=[ALPHA])
    rebuild_from_json(state_dir, selection=selection)
    seed_state(state_dir, candidate_history=[ALPHA, BETA])

    with pytest.raises(StateStoreError, match="already exists"):
        rebuild_from_json(state_dir, selection=selection)
    assert open_state_store(state_dir, selection).read(Collection.CANDIDATE_HISTORY) == [ALPHA]

    report = rebuild_from_json(state_dir, overwrite=True, selection=selection)
    assert report.replaced_existing and report.record_count == 2
    assert open_state_store(state_dir, selection).read(Collection.CANDIDATE_HISTORY) == [ALPHA, BETA]


def test_a_failed_overwrite_keeps_the_existing_database(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    use_layout(state_dir, SINGLE_FILE)
    selection = _sqlite(tmp_path)
    rebuild_from_json(state_dir, selection=selection)
    open_state_store(state_dir, selection).upsert_candidate(ALPHA)  # not yet exported
    real_replace = os.replace

    def failing_replace(source: Any, destination: Any) -> None:
        if ".rebuild-" in Path(source).name:
            raise OSError("disk full")
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError, match="disk full"):
        rebuild_from_json(state_dir, overwrite=True, selection=selection)
    monkeypatch.setattr(os, "replace", real_replace)

    assert open_state_store(state_dir, selection).read(Collection.CANDIDATE_HISTORY) == [ALPHA]
    assert sorted(path.name for path in tmp_path.iterdir() if path.name.startswith(".")) == []


def _corrupt_profile(state_dir: Path) -> None:
    (state_dir / FILE_NAMES[Collection.EDITORIAL_PROFILE]).write_text('{"entries": [{"id": "x"}]}', encoding="utf-8")


def _corrupt_aliases(state_dir: Path) -> None:
    (state_dir / FILE_NAMES[Collection.CANDIDATE_IDENTITY_ALIASES]).write_text('{"aliases": []}', encoding="utf-8")


def _corrupt_history_record(state_dir: Path) -> None:
    (state_dir / CANDIDATE_HISTORY_FILE).write_text('[{"identity_key": "a"}, "not a record"]', encoding="utf-8")


def _corrupt_decision_log(state_dir: Path) -> None:
    record = {**ALPHA, "editor_decisions": [{"status": "reject"}]}
    (state_dir / CANDIDATE_HISTORY_FILE).write_text(json.dumps([record]), encoding="utf-8")


def _corrupt_shard(state_dir: Path) -> None:
    (state_dir / CANDIDATE_HISTORY_FILE).unlink()
    seed_state(state_dir, candidate_history=[ALPHA])
    _shard(state_dir, ALPHA).write_text(json.dumps(BETA), encoding="utf-8")


def _corrupt_json(state_dir: Path) -> None:
    (state_dir / FILE_NAMES[Collection.CANDIDATE_VISIBILITY]).write_text("[{", encoding="utf-8")


@pytest.mark.parametrize(
    ("corrupt", "error"),
    [
        (_corrupt_profile, TasteProfileError),
        (_corrupt_aliases, ValueError),
        (_corrupt_history_record, StateStoreError),
        (_corrupt_decision_log, StateStoreError),
        (_corrupt_shard, StateStoreError),
        (_corrupt_json, ValueError),
    ],
    ids=lambda value: getattr(value, "__name__", ""),
)
def test_rebuild_refuses_invalid_state_before_the_database_file_exists(tmp_path, corrupt, error):
    state_dir = tmp_path / "state"
    use_layout(state_dir, SINGLE_FILE)
    corrupt(state_dir)
    selection = _sqlite(tmp_path)

    with pytest.raises(error):
        rebuild_from_json(state_dir, selection=selection)

    assert sorted(path.name for path in tmp_path.iterdir()) == ["state"]


def test_rebuild_reports_and_can_refuse_files_not_in_the_form_the_store_writes(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / CANDIDATE_HISTORY_FILE).write_text(json.dumps([BETA, ALPHA]), encoding="utf-8")
    selection = _sqlite(tmp_path)

    with pytest.raises(StateStoreError, match=CANDIDATE_HISTORY_FILE):
        rebuild_from_json(state_dir, selection=selection, require_canonical=True)
    assert not state_database_path(state_dir, selection).exists()

    report = rebuild_from_json(state_dir, selection=selection)
    assert report.non_canonical == [CANDIDATE_HISTORY_FILE]
    assert open_state_store(state_dir, selection).describe()["export_current"] == "never"
    assert export_json(state_dir, selection).files_written == 1
    assert read_state(state_dir, Collection.CANDIDATE_HISTORY) == [ALPHA, BETA]


def test_a_state_directory_with_json_and_no_database_is_refused_rather_than_read_as_empty(tmp_path):
    state_dir = tmp_path / "state"
    seed_state(state_dir, candidate_history=[ALPHA])
    store = open_state_store(state_dir, _sqlite(tmp_path))

    for operation in (
        lambda: store.read(Collection.CANDIDATE_HISTORY),
        lambda: store.upsert_candidate(BETA),
        lambda: store.get_candidate(ALPHA["candidate_id"]),
    ):
        with pytest.raises(StateStoreError, match="state-database rebuild"):
            operation()
    assert not state_database_path(state_dir, _sqlite(tmp_path)).exists()


def test_reading_a_new_state_directory_outside_a_transaction_creates_no_database(tmp_path):
    state_dir = tmp_path / "state"
    selection = _sqlite(tmp_path)
    store = open_state_store(state_dir, selection)

    assert store.read(Collection.CANDIDATE_HISTORY) == []
    assert store.delete_candidate(ALPHA["candidate_id"]) is False
    assert store.get_candidate(ALPHA["candidate_id"]) is None
    assert export_json(state_dir, selection).status == "no_database"
    assert not state_database_path(state_dir, selection).exists()

    # A transaction takes the write lock at its first read, on an empty database it creates.
    with store.transaction():
        assert store.get_candidate(ALPHA["candidate_id"]) is None
    assert export_json(state_dir, selection).status == "unchanged"
    assert not state_dir.exists() or not any(state_dir.iterdir())


# ── Transactions and concurrent writers ──────────────────────────────────────


def _database_content(database: Path) -> str:
    conn = sqlite3.connect(database)
    try:
        return hashlib.sha256("\n".join(conn.iterdump()).encode("utf-8")).hexdigest()
    finally:
        conn.close()


def test_an_exception_inside_a_transaction_leaves_the_database_content_unchanged(tmp_path):
    state_dir = tmp_path / "state"
    selection = _sqlite(tmp_path)
    store = open_state_store(state_dir, selection)
    store.replace(Collection.CANDIDATE_HISTORY, [ALPHA, DECIDED])
    before = _database_content(state_database_path(state_dir, selection))

    with pytest.raises(RuntimeError):
        with store.transaction():
            store.upsert_candidate({**DECIDED, "editor_decisions": DECISIONS[:1]})
            store.delete_candidate(ALPHA["candidate_id"])
            store.replace(Collection.SOURCE_PROPOSALS, [{"id": "proposal-1"}])
            raise RuntimeError("interrupted")

    assert _database_content(state_database_path(state_dir, selection)) == before


def test_a_refused_write_inside_a_transaction_leaves_the_transaction_s_other_writes(tmp_path):
    state_dir = tmp_path / "state"
    selection = _sqlite(tmp_path)
    store = open_state_store(state_dir, selection)
    with store.transaction():
        store.replace(Collection.CANDIDATE_HISTORY, [ALPHA])
        with pytest.raises(StateStoreError):
            store.replace(Collection.CANDIDATE_HISTORY, [BETA, "not a record"])
        store.upsert_candidate(GAMMA)

    assert open_state_store(state_dir, selection).read(Collection.CANDIDATE_HISTORY) == [ALPHA, GAMMA]


WRITER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from autoscout.state_store import BackendSelection, Collection, open_state_store

    state_dir, database, prefix, start, hold = sys.argv[1:6]
    store = open_state_store(Path(state_dir), BackendSelection("sqlite", "default", Path(database)))
    while not Path(start).exists():
        time.sleep(0.005)
    for number in range(40):
        with store.transaction():
            store.read(Collection.CANDIDATE_HISTORY)
            store.upsert_candidate(
                {"candidate_id": f"candidate-{prefix}{number:023x}", "identity_key": f"{prefix}|{number}"}
            )
            time.sleep(float(hold))
    """
)


def test_two_processes_writing_at_once_both_succeed(tmp_path):
    state_dir = tmp_path / "state"
    database = tmp_path / "state-database.sqlite3"
    start = tmp_path / "start"
    script = tmp_path / "writer.py"
    script.write_text(WRITER, encoding="utf-8")
    writers = [
        subprocess.Popen(
            [sys.executable, str(script), str(state_dir), str(database), prefix, str(start), hold],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for prefix, hold in (("a", "0.002"), ("b", "0"))
    ]
    start.touch()
    results = [writer.communicate(timeout=120) for writer in writers]

    assert [writer.returncode for writer in writers] == [0, 0], results
    history = open_state_store(state_dir, BackendSelection(SQLITE_BACKEND, "default", database)).read(
        Collection.CANDIDATE_HISTORY
    )
    assert len(history) == 80


# ── Where the database lives ──────────────────────────────────────────────────


def test_the_database_defaults_to_the_git_ignored_data_directory(monkeypatch):
    monkeypatch.delenv(DATABASE_DIR_ENV, raising=False)
    assert default_database_path(REPOSITORY_ROOT / "runs" / "current-weekly-pilot-russian") == (
        DEFAULT_DATABASE_DIR / "runs--current-weekly-pilot-russian.sqlite3"
    )
    ignored = subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), "check-ignore", "-q", str(DEFAULT_DATABASE_DIR / "x.sqlite3-wal")],
        check=False,
    )
    assert ignored.returncode == 0


def test_a_state_directory_outside_the_repository_gets_its_own_default_database(tmp_path, monkeypatch):
    monkeypatch.delenv(DATABASE_DIR_ENV, raising=False)
    first = default_database_path(tmp_path / "one" / "state")
    second = default_database_path(tmp_path / "two" / "state")

    assert first.parent == second.parent == DEFAULT_DATABASE_DIR
    assert first != second and first.name.startswith("state-")


def test_the_environment_moves_the_default_databases_to_another_directory(tmp_path, monkeypatch):
    monkeypatch.setenv(DATABASE_DIR_ENV, str(tmp_path / "databases"))

    assert default_database_path(REPOSITORY_ROOT / "runs" / "current-weekly-pilot-russian") == (
        tmp_path / "databases" / "runs--current-weekly-pilot-russian.sqlite3"
    )
    monkeypatch.setenv(DATABASE_DIR_ENV, "data/state-store/elsewhere")
    assert default_database_path(tmp_path / "state").parent == DEFAULT_DATABASE_DIR / "elsewhere"


def test_the_config_and_the_environment_name_the_database(tmp_path):
    config = {"state_backend": "sqlite", "state_database": "data/elsewhere.sqlite3"}

    assert select_state_backend(config, environ={}).database == REPOSITORY_ROOT / "data" / "elsewhere.sqlite3"
    environ = {STATE_DATABASE_ENV: str(tmp_path / "env.sqlite3")}
    assert select_state_backend(config, environ=environ).database == tmp_path / "env.sqlite3"
    assert select_state_backend({}, environ={}).database is None


@pytest.mark.parametrize(
    "folder",
    [
        "Dropbox",
        "Dropbox (Personal)",
        "OneDrive - Company",
        "Library/Mobile Documents/com~apple~CloudDocs",
        "Google Drive",
    ],
)
def test_a_database_in_a_cloud_synced_folder_is_refused(tmp_path, folder):
    database = tmp_path / folder / "state.sqlite3"

    assert database_path_class(database) == SYNCED
    with pytest.raises(StateStoreError, match="synced"):
        open_state_store(tmp_path / "state", BackendSelection(SQLITE_BACKEND, "config", database))
    with pytest.raises(StateStoreError, match="synced"):
        rebuild_from_json(tmp_path / "state", selection=BackendSelection(SQLITE_BACKEND, "config", database))
    assert not database.parent.exists()


def test_a_database_on_a_network_file_system_is_refused(tmp_path, monkeypatch):
    from autoscout.state_store import sqlite_location

    monkeypatch.setattr(sqlite_location, "_mount_table", lambda: ((tmp_path.as_posix(), "nfs4"), ("/", "ext4")))

    assert database_path_class(tmp_path / "state.sqlite3") == NETWORK
    with pytest.raises(StateStoreError, match="network"):
        open_state_store(tmp_path / "state", BackendSelection(SQLITE_BACKEND, "config", tmp_path / "state.sqlite3"))


def test_a_database_belonging_to_another_state_directory_is_refused(tmp_path):
    database = tmp_path / "shared.sqlite3"
    selection = BackendSelection(SQLITE_BACKEND, "config", database)
    open_state_store(tmp_path / "one", selection).upsert_candidate(ALPHA)

    with pytest.raises(StateStoreError, match="belongs to the state directory"):
        open_state_store(tmp_path / "two", selection).read(Collection.CANDIDATE_HISTORY)


def test_a_database_with_a_newer_schema_is_refused(tmp_path):
    state_dir = tmp_path / "state"
    selection = _sqlite(tmp_path)
    open_state_store(state_dir, selection).upsert_candidate(ALPHA)
    conn = sqlite3.connect(state_database_path(state_dir, selection))
    with conn:
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (99, 'later')")
    conn.close()

    with pytest.raises(StateStoreError, match="newer"):
        open_state_store(state_dir, selection).read(Collection.CANDIDATE_HISTORY)


# ── Commands and finalization ─────────────────────────────────────────────────


def _cli(*arguments: str) -> int:
    return main(list(arguments))


def test_the_state_database_command_rebuilds_and_exports(tmp_path, capsys):
    state_dir = tmp_path / "state"
    database = tmp_path / "db.sqlite3"
    use_layout(state_dir, SINGLE_FILE)
    seed_state(state_dir, candidate_history=[ALPHA, BETA])
    arguments = ("--state-dir", str(state_dir), "--database", str(database))

    assert _cli("state-database", "rebuild", *arguments) == 0
    output = capsys.readouterr().out
    assert "state_database_rebuild=built" in output and "record_count=2" in output
    assert _cli("state-database", "rebuild", *arguments) == 2
    assert "state_database_rebuild=refused" in capsys.readouterr().out
    assert _cli("state-database", "rebuild", "--overwrite", *arguments) == 0
    capsys.readouterr()
    assert _cli("state-database", "export", *arguments) == 0
    assert "state_database_export=unchanged" in capsys.readouterr().out


def test_the_rebuild_command_refuses_non_canonical_files_unless_allowed(tmp_path, capsys):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / CANDIDATE_HISTORY_FILE).write_text(json.dumps([ALPHA]), encoding="utf-8")
    arguments = ("--state-dir", str(state_dir), "--database", str(tmp_path / "db.sqlite3"))

    assert _cli("state-database", "rebuild", *arguments) == 2
    assert not (tmp_path / "db.sqlite3").exists()
    assert _cli("state-database", "rebuild", "--allow-non-canonical", *arguments) == 0
    assert f"non_canonical={CANDIDATE_HISTORY_FILE}" in capsys.readouterr().out


def test_a_confirmed_editor_command_exports_its_change_under_the_sqlite_backend(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    database = tmp_path / "db.sqlite3"
    seed_state(state_dir, candidate_history=[ALPHA])
    rebuild_from_json(state_dir, selection=BackendSelection(SQLITE_BACKEND, "config", database))
    monkeypatch.setenv(STATE_BACKEND_ENV, SQLITE_BACKEND)
    monkeypatch.setenv(STATE_DATABASE_ENV, str(database))

    assert (
        _cli("record-visibility", "--identity-key", "alpha|one", "--action", "hide", "--state-dir", str(state_dir)) == 0
    )

    exported = json.loads((state_dir / FILE_NAMES[Collection.CANDIDATE_VISIBILITY]).read_text(encoding="utf-8"))
    assert [entry["identity_key"] for entry in exported] == ["alpha|one"]
    assert open_state_store(state_dir).describe()["export_current"] == "yes"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_a_weekly_run_under_the_sqlite_backend_exports_its_state_before_the_scan_and_the_envelope(
    tmp_path, monkeypatch, layout
):
    from test_weekly_workflow import _write_capture_fixture
    from test_weekly_workflow import _write_codex_fixture_config
    from test_weekly_workflow import _write_extraction_fixture

    from autoscout.weekly_workflow import deterministic_adapters
    from autoscout.weekly_workflow import run_weekly_workflow

    monkeypatch.setenv(STATE_BACKEND_ENV, SQLITE_BACKEND)
    monkeypatch.setenv(STATE_DATABASE_ENV, str(tmp_path / "db.sqlite3"))
    config = _write_codex_fixture_config(tmp_path)
    output_dir = tmp_path / "run"
    # The run's report directory is its state directory, as under the weekly launcher.
    use_layout(output_dir, layout)
    rebuild_from_json(output_dir)
    _write_capture_fixture(tmp_path, output_dir)
    adapters = deterministic_adapters()
    adapters.capture.prepare(config, output_dir, live=False)
    _write_extraction_fixture(output_dir)

    from autoscout import weekly_workflow

    real_scan = weekly_workflow._scan_output_dir_for_secrets
    seen_by_scan: list[str] = []

    def scan(directory: Path) -> Any:
        # The credential scan must see the files the envelope will commit.
        seen_by_scan.append(open_state_store(output_dir).describe()["export_current"])
        return real_scan(directory)

    monkeypatch.setattr(weekly_workflow, "_scan_output_dir_for_secrets", scan)

    result = run_weekly_workflow(config, output_dir, output_dir, adapters=adapters)

    assert result.outcome.value.startswith("completed/"), result.failed_stage
    assert seen_by_scan == ["yes"]
    persisted = json.loads((output_dir / "workflow-log.json").read_text(encoding="utf-8"))
    export = next(entry for entry in persisted if entry["stage"] == "state-export")
    assert export["status"] == "ok" and "database" not in export["detail"]
    assert export["detail"]["layout"] == layout
    store = open_state_store(output_dir)
    assert store.describe()["export_current"] == "yes"
    exported = open_state_store(output_dir, BackendSelection(JSON_BACKEND, "default"))
    for collection in Collection:
        assert exported.read(collection) == store.read(collection), collection


def test_a_weekly_run_under_the_json_backend_logs_no_state_export(tmp_path, monkeypatch):
    from test_weekly_workflow import _write_capture_fixture
    from test_weekly_workflow import _write_codex_fixture_config
    from test_weekly_workflow import _write_extraction_fixture

    from autoscout.weekly_workflow import deterministic_adapters
    from autoscout.weekly_workflow import run_weekly_workflow

    monkeypatch.delenv(STATE_BACKEND_ENV, raising=False)
    config = _write_codex_fixture_config(tmp_path)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    _write_capture_fixture(tmp_path, output_dir)
    adapters = deterministic_adapters()
    adapters.capture.prepare(config, output_dir, live=False)
    _write_extraction_fixture(output_dir)

    result = run_weekly_workflow(config, output_dir, output_dir, adapters=adapters)

    assert result.outcome.value.startswith("completed/"), result.failed_stage
    assert not any(entry.get("stage") == "state-export" for entry in result.log)
    assert not any(path.suffix == ".sqlite3" for path in tmp_path.rglob("*"))
    assert not os.environ.get(STATE_DATABASE_ENV)


def test_an_export_waits_for_a_write_in_progress_and_includes_it(tmp_path):
    import threading

    state_dir = tmp_path / "state"
    selection = _sqlite(tmp_path)
    open_state_store(state_dir, selection).replace(Collection.CANDIDATE_HISTORY, [ALPHA])
    export_json(state_dir, selection)
    writer = open_state_store(state_dir, selection)
    reports: list[Any] = []
    with writer.transaction():
        writer.upsert_candidate(BETA)
        exporting = threading.Thread(target=lambda: reports.append(export_json(state_dir, selection)))
        exporting.start()
        exporting.join(timeout=0.5)
        assert exporting.is_alive()
    exporting.join(timeout=30)

    assert reports[0].files_written == 1
    assert read_state(state_dir, Collection.CANDIDATE_HISTORY) == [ALPHA, BETA]
    assert open_state_store(state_dir, selection).describe()["export_current"] == "yes"


@pytest.mark.parametrize("where", ["state directory", "repository"])
def test_a_database_git_could_commit_is_refused(tmp_path, where):
    state_dir = tmp_path / "state"
    database = state_dir / "db.sqlite3" if where == "state directory" else REPOSITORY_ROOT / "data" / "x.sqlite3"

    with pytest.raises(StateStoreError, match="state database"):
        open_state_store(state_dir, BackendSelection(SQLITE_BACKEND, "config", database))
    assert not database.exists()


def test_onboarding_stages_its_proposals_as_json_whichever_backend_is_active(tmp_path, monkeypatch):
    from autoscout.source_proposal_commands import write_source_proposals_state

    monkeypatch.setenv(STATE_BACKEND_ENV, SQLITE_BACKEND)
    monkeypatch.setenv(STATE_DATABASE_ENV, str(tmp_path / "canonical.sqlite3"))
    staged = tmp_path / "staged"

    write_source_proposals_state(staged, [{"id": "proposal-1"}])

    assert json.loads((staged / FILE_NAMES[Collection.SOURCE_PROPOSALS]).read_text(encoding="utf-8")) == [
        {"id": "proposal-1"}
    ]
    assert not (tmp_path / "canonical.sqlite3").exists()


def test_onboarding_accepts_its_staged_proposal_in_the_staged_json(tmp_path, monkeypatch):
    from autoscout.source_proposal_commands import accept_source_proposal_state
    from autoscout.source_proposal_commands import prepare_source_proposal
    from autoscout.source_proposal_commands import staged_proposals_store
    from autoscout.source_proposal_commands import write_source_proposals_state

    monkeypatch.setenv(STATE_BACKEND_ENV, SQLITE_BACKEND)
    monkeypatch.setenv(STATE_DATABASE_ENV, str(tmp_path / "canonical.sqlite3"))
    staged = tmp_path / "staged"
    proposals, proposal, _merged = prepare_source_proposal(
        [], source_url="https://example.test/list", proposed_by="operator", reason="a curated list"
    )
    write_source_proposals_state(staged, proposals)

    accept_source_proposal_state(staged, proposal["id"], source_name="Example", store=staged_proposals_store(staged))

    written = json.loads((staged / FILE_NAMES[Collection.SOURCE_PROPOSALS]).read_text(encoding="utf-8"))
    assert [entry["status"] for entry in written] == ["accepted"]
    assert not (tmp_path / "canonical.sqlite3").exists()


def test_a_transaction_that_reads_before_the_database_exists_still_serializes_other_writers(tmp_path):
    import threading

    state_dir = tmp_path / "state"
    selection = _sqlite(tmp_path)
    first = open_state_store(state_dir, selection)
    other = threading.Thread(target=lambda: open_state_store(state_dir, selection).upsert_candidate(ALPHA))

    with first.transaction():
        assert first.read(Collection.CANDIDATE_HISTORY) == []
        other.start()
        other.join(timeout=0.5)
        assert other.is_alive()
        first.replace(Collection.CANDIDATE_HISTORY, [BETA])
    other.join(timeout=30)

    assert read_state_sqlite(state_dir, selection) == [ALPHA, BETA]


def read_state_sqlite(state_dir: Path, selection: BackendSelection) -> Any:
    return open_state_store(state_dir, selection).read(Collection.CANDIDATE_HISTORY)


def test_a_command_s_export_uses_the_backend_its_own_store_used(tmp_path, monkeypatch, capsys):
    from autoscout.__main__ import _export_state_after

    state_dir = tmp_path / "state"
    seed_state(state_dir, candidate_history=[ALPHA])
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"state_backend": "sqlite", "state_database": str(tmp_path / "db.sqlite3")}))
    monkeypatch.delenv(STATE_BACKEND_ENV, raising=False)
    import argparse

    # A command whose store ignores --config wrote JSON; its export must not read the config's database.
    args = argparse.Namespace(command="source-proposal", state_dir=state_dir, config=config)
    assert _export_state_after(args, 0) == 0
    assert "state_export=failed" not in capsys.readouterr().err
    assert not (tmp_path / "db.sqlite3").exists()


# ── Found by running the whole suite under SQLite (#1075) ─────────────────────


def test_a_run_without_state_keeps_its_output_directory_as_json_under_a_sqlite_selection(tmp_path, monkeypatch):
    from autoscout.review_rendering import render_review
    from autoscout.state_store import open_run_output_store
    from autoscout.state_store.json_backend import JsonStateStore

    monkeypatch.setenv(STATE_BACKEND_ENV, SQLITE_BACKEND)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    book = {**ALPHA, "primary_authors": ["Anna"], "first_seen_date": "2026-11-01", "last_seen_date": "2026-11-01"}
    hidden = [{"identity_key": ALPHA["identity_key"], "visibility": "hidden"}]
    for name, value in (
        ("scouting-candidates.json", [book]),
        ("candidate-history.json", [book]),
        ("candidate-visibility.json", hidden),
        ("run-summary.json", {"run_date": "2026-11-01", "editor_facing_count": 1}),
    ):
        (output_dir / name).write_text(json.dumps(value), encoding="utf-8")

    store = open_run_output_store(output_dir)

    assert isinstance(store, JsonStateStore)
    assert store.read(Collection.CANDIDATE_VISIBILITY) == hidden
    assert render_review(output_dir, effective_date="2026-11-01") == 0
    assert not (tmp_path / "default-databases").exists()


def test_a_run_output_directory_cut_over_to_sqlite_opens_as_its_state_directory(tmp_path):
    from autoscout.state_store import cut_over_to_sqlite
    from autoscout.state_store import open_run_output_store
    from autoscout.state_store.sqlite_backend import SqliteStateStore

    state_dir = tmp_path / "state"
    seed_state(state_dir, candidate_history=[ALPHA])
    rebuild_from_json(state_dir)
    cut_over_to_sqlite(state_dir)

    store = open_run_output_store(state_dir)

    assert isinstance(store, SqliteStateStore)
    assert store.read(Collection.CANDIDATE_HISTORY) == [ALPHA]


def test_a_marked_run_output_directory_still_refuses_a_conflicting_json_selection(tmp_path, monkeypatch):
    from autoscout.state_store import cut_over_to_sqlite
    from autoscout.weekly_report import write_run_history

    state_dir = tmp_path / "state"
    seed_state(state_dir, candidate_history=[ALPHA])
    rebuild_from_json(state_dir)
    cut_over_to_sqlite(state_dir)
    monkeypatch.setenv(STATE_BACKEND_ENV, JSON_BACKEND)

    with pytest.raises(StateStoreError, match="rollback"):
        write_run_history(state_dir, None, [ALPHA, BETA])
    monkeypatch.delenv(STATE_BACKEND_ENV)
    assert open_state_store(state_dir).read(Collection.CANDIDATE_HISTORY) == [ALPHA]


def test_rendering_a_marked_output_directory_uses_the_database_its_config_names(tmp_path):
    from autoscout.review_rendering import render_review
    from autoscout.state_store import cut_over_to_sqlite

    output_dir = tmp_path / "run"
    database = tmp_path / "elsewhere.sqlite3"
    selection = BackendSelection(SQLITE_BACKEND, "config", database)
    book = {**ALPHA, "primary_authors": ["Anna"], "first_seen_date": "2026-11-01", "last_seen_date": "2026-11-01"}
    seed_state(output_dir, candidate_history=[book])
    for name, value in (
        ("scouting-candidates.json", [book]),
        ("run-summary.json", {"run_date": "2026-11-01", "editor_facing_count": 1}),
    ):
        (output_dir / name).write_text(json.dumps(value), encoding="utf-8")
    rebuild_from_json(output_dir, selection=selection)
    cut_over_to_sqlite(output_dir, selection)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"state_backend": "sqlite", "state_database": str(database)}), encoding="utf-8")

    assert render_review(output_dir, effective_date="2026-11-01", config_path=config) == 0
    assert not (tmp_path / "default-databases").exists()


def test_a_weekly_run_without_state_runs_its_state_stages_under_a_sqlite_selection(tmp_path, monkeypatch):
    from test_weekly_workflow import _write_capture_fixture
    from test_weekly_workflow import _write_codex_fixture_config
    from test_weekly_workflow import _write_extraction_fixture

    from autoscout.weekly_workflow import deterministic_adapters
    from autoscout.weekly_workflow import run_weekly_workflow

    monkeypatch.setenv(STATE_BACKEND_ENV, SQLITE_BACKEND)
    config = _write_codex_fixture_config(tmp_path)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    _write_capture_fixture(tmp_path, output_dir)
    adapters = deterministic_adapters()
    adapters.capture.prepare(config, output_dir, live=False)
    _write_extraction_fixture(output_dir)

    result = run_weekly_workflow(config, output_dir, adapters=adapters)

    refused = [entry["stage"] for entry in result.log if "StateStoreError" in json.dumps(entry.get("detail", {}))]
    assert refused == []
    adjudication = next(entry for entry in result.log if entry["stage"] == "description-adjudication")
    assert adjudication["status"] != "failed"
    assert not (tmp_path / "default-databases").exists()


def test_the_export_of_an_empty_history_creates_the_state_directory_as_the_json_backend_does(tmp_path):
    json_dir, state_dir = tmp_path / "json", tmp_path / "state"
    selection = _sqlite(tmp_path)
    for store in (
        open_state_store(json_dir, BackendSelection(JSON_BACKEND, "default")),
        open_state_store(state_dir, selection),
    ):
        with store.transaction():
            store.replace(Collection.CANDIDATE_HISTORY, [])
    assert not state_dir.exists()

    export_json(state_dir, selection)

    assert json_dir.is_dir() and state_dir.is_dir()
    assert _state_files(state_dir) == _state_files(json_dir) == {}


def test_replacing_and_exporting_into_a_new_state_directory_needs_no_earlier_export(tmp_path):
    from autoscout.state_store import replace_and_export

    state_dir = tmp_path / "state"
    store = open_state_store(state_dir, _sqlite(tmp_path))
    proposals = [{"id": "proposal-1", "source_url": "https://example.test/"}]

    with replace_and_export(store, Collection.SOURCE_PROPOSALS, proposals):
        exported = json.loads((state_dir / FILE_NAMES[Collection.SOURCE_PROPOSALS]).read_text(encoding="utf-8"))
        assert exported == proposals

    assert store.read(Collection.SOURCE_PROPOSALS) == proposals
    assert store.describe()["export_current"] == "yes"


def test_replacing_and_exporting_still_refuses_state_files_the_database_never_exported(tmp_path):
    from autoscout.state_store import replace_and_export

    state_dir = tmp_path / "state"
    store = open_state_store(state_dir, _sqlite(tmp_path))
    with store.transaction():
        store.read(Collection.CANDIDATE_HISTORY)
    state_dir.mkdir()
    (state_dir / FILE_NAMES[Collection.CANDIDATE_VISIBILITY]).write_text("[]\n", encoding="utf-8")

    with pytest.raises(StateStoreError, match="not current"):
        with replace_and_export(store, Collection.SOURCE_PROPOSALS, [{"id": "proposal-1"}]):
            pass
    assert store.read(Collection.SOURCE_PROPOSALS) == []
