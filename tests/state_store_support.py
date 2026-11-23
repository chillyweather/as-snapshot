"""Seed and read canonical state through the State Store, for tests (#1068, #1072).

A test sets up state with one keyword per ``Collection`` value::

    seed_state(state_dir, candidate_history=[...], candidate_visibility=[...])

and reads what a command left behind with
``read_state(state_dir, "candidate_history")``. A zero-mutation test compares
``state_snapshot(state_dir)`` before and after (stored bytes, so a rewrite with
other formatting counts). None of them names a state file,
so the sharded layout of Candidate History (#1073) and the SQLite backend
(#1074) need no change to the tests that use them. A new state directory
keeps Candidate History one file per book; ``use_layout`` puts one in the
single-file layout an existing state directory may still have.

Seeding stores the backend's canonical form: records come back in the
backend's order, and an invalid Editorial Taste Profile or alias table is
refused. A test about malformed state on disk writes that file itself.

The helpers follow the backend the test run selects (``--state-backend``,
#1075). Under SQLite the database is canonical and the state directory's JSON
files are its export: ``seed_state`` builds the database from the JSON a test
wrote first (``rebuild_from_json``), writes through the store and exports, so
the files a test or a command reads beside the database agree with it. A test
that changes a state file itself after that calls ``sync_state`` so the
database holds the change too.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from autoscout.state_store import JSON_BACKEND
from autoscout.state_store import SQLITE_BACKEND
from autoscout.state_store import BackendSelection
from autoscout.state_store import Collection
from autoscout.state_store import export_json
from autoscout.state_store import open_run_output_store
from autoscout.state_store import open_state_store
from autoscout.state_store import rebuild_from_json
from autoscout.state_store import resolve_state_backend
from autoscout.state_store import state_database_path
from autoscout.state_store.json_backend import FILE_NAMES
from autoscout.state_store.json_backend import LEGACY_PROFILE_FILE_NAME
from autoscout.state_store.json_backend import holds_json_state
from autoscout.state_store.json_backend import stored_state_digest
from autoscout.state_store.layout import CANDIDATE_HISTORY_DIR
from autoscout.state_store.layout import CANDIDATE_HISTORY_FILE
from autoscout.state_store.layout import valid_candidate_id
from autoscout.state_store.sqlite_location import DATABASE_DIR_ENV
from autoscout.state_store.sqlite_location import DATABASE_SUFFIX
from autoscout.state_store.sqlite_location import state_dir_key

# The two layouts Candidate History can have in a state directory (#1073).
SINGLE_FILE = "single_file"
SHARDED = "sharded"
LAYOUTS = (SINGLE_FILE, SHARDED)


def store_selection(backend: str, tmp_path: Path) -> BackendSelection:
    """A selection of *backend* for a test; the SQLite database lives in *tmp_path*, never under ``data/``."""
    database = tmp_path / "state-database.sqlite3" if backend == SQLITE_BACKEND else None
    return BackendSelection(backend, "default", database)


def use_layout(state_dir: Path, layout: str) -> None:
    """Put an empty or new *state_dir* in *layout*.

    A state directory is sharded unless it already holds the single file, so
    the single-file layout is chosen the way an existing state directory
    has it: with an empty ``candidate-history.json``.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    if layout == SINGLE_FILE:
        (state_dir / CANDIDATE_HISTORY_FILE).write_text("[]\n", encoding="utf-8")
    elif layout != SHARDED:
        raise ValueError(f"unknown layout {layout!r}")


def history_shards(state_dir: Path) -> dict[str, bytes]:
    """Every Candidate History shard file of *state_dir* by its path relative to the state directory."""
    root = state_dir / CANDIDATE_HISTORY_DIR
    if not root.is_dir():
        return {}
    return {
        path.relative_to(state_dir).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()
    }


def _shardable(history: Any) -> bool:
    return isinstance(history, list) and all(
        isinstance(record, dict) and valid_candidate_id(record.get("candidate_id")) for record in history
    )


def seed_state(state_dir: Path, **collections: Any) -> None:
    """Replace each named collection in one State Store transaction.

    With no collection named it leaves an empty state directory as a command
    finds it: nothing on the JSON backend, an empty database with a current
    export under SQLite.

    A new state directory keeps Candidate History one file per book, as in
    production. Many tests seed made-up records without a real Candidate ID
    (``candidate-<24 hex characters>``), which the sharded layout refuses;
    seeded into a state directory without the single file, such records are
    kept in the single-file layout a state directory from before #1073 has, which the
    backend still reads and writes byte for byte.
    """
    sqlite = _uses_sqlite(state_dir)
    if sqlite:
        # The files a test wrote itself go into the database; otherwise the
        # files are brought up to date with what commands wrote to it.
        _absorb_file_changes(state_dir)
        export_json(state_dir)
    history = collections.get(Collection.CANDIDATE_HISTORY.value)
    if history is not None and not _shardable(history) and not (state_dir / CANDIDATE_HISTORY_FILE).exists():
        # The seed replaces the whole history, so shards seeded earlier go.
        shutil.rmtree(state_dir / CANDIDATE_HISTORY_DIR, ignore_errors=True)
        use_layout(state_dir, SINGLE_FILE)
        if sqlite:
            # The export follows the new layout; the other collections are what the database holds.
            rebuild_from_json(state_dir, overwrite=True)
    store = open_state_store(state_dir)
    with store.transaction():
        # Under SQLite the first read creates the database, so even a seed of
        # nothing leaves a state directory with a database and a current export.
        store.read(Collection.CANDIDATE_HISTORY)
        for name, value in collections.items():
            store.replace(Collection(name), value)
    export_json(state_dir)


def sync_state(state_dir: Path) -> None:
    """Make the database hold the state files a test wrote itself; nothing to do on the JSON backend.

    Under SQLite the state directory's JSON is an export of the database, so
    a file a test changes there is an outside change the next export
    refuses. The database is rebuilt from the files instead.
    """
    if _uses_sqlite(state_dir):
        rebuild_from_json(state_dir, overwrite=True)
        export_json(state_dir)


def _uses_sqlite(state_dir: Path) -> bool:
    return resolve_state_backend(state_dir).backend == SQLITE_BACKEND


def _absorb_file_changes(state_dir: Path) -> None:
    """Build the database from the state files when a test wrote them itself since the last export.

    A state directory with JSON and no database is built from it. One whose
    files no longer match the last export (a test wrote one) is rebuilt from
    them. Otherwise the database is kept, with every write a command made to
    it, and so is a database a command created and nothing exported yet.
    """
    database = state_database_path(state_dir)
    if not database.exists():
        if holds_json_state(state_dir):
            rebuild_from_json(state_dir)
        return
    exported = _exported_digest(database, state_dir)
    if exported is not None and exported != stored_state_digest(state_dir):
        rebuild_from_json(state_dir, overwrite=True)


def _exported_digest(database: Path, state_dir: Path) -> str | None:
    """The state directory's digest the database recorded at its last export or rebuild into it."""
    conn = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT tree_digest FROM json_exports WHERE state_dir = ?", (state_dir_key(state_dir),)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return None if row is None else str(row[0])


def read_state(state_dir: Path, collection: str | Collection) -> Any:
    """Return one whole collection as the State Store reads it."""
    return open_state_store(state_dir).read(Collection(collection))


def read_run_output(output_dir: Path, collection: str | Collection) -> Any:
    """Return one collection as a run without a state directory left it in its output directory.

    Such a run keeps its state as the JSON files beside its report whatever
    backend the run selects (``open_run_output_store``), so a test of it
    reads them there, not from a database.
    """
    return open_run_output_store(output_dir).read(Collection(collection))


def read_fixture_state(directory: Path, collection: str | Collection) -> Any:
    """Return one collection of a committed fixture directory, which holds JSON, with the JSON backend.

    A fixture is JSON on disk whatever backend the run selects, and is never
    given a database.
    """
    return open_state_store(directory, BackendSelection(JSON_BACKEND, "fixture")).read(Collection(collection))


def state_snapshot(state_dir: Path) -> dict[str, bytes | None]:
    """Return the stored bytes of every canonical collection, ``None`` for one not stored.

    A zero-mutation test takes one before and one after and compares them: a
    changed byte, a rewritten file with other formatting and a collection that
    appeared all show as a difference. Files that are not canonical state (run
    artifacts, locks, logs) are not part of it. This is the one place besides
    the backend that knows where a collection is stored; #1074 changes it
    here, not in the tests.

    Candidate History has two entries: ``candidate_history`` holds the single
    file's bytes and ``candidate_history_shards`` every shard's path and
    bytes (#1073), so a rewritten, added or removed shard and a stray single
    file beside the shards all show.

    ``state_database`` is a checksum of the SQLite database's content (every
    table's rows, not the file's bytes, which WAL checkpoints change), or
    ``None`` while the state directory has no database. Under the JSON
    backend there is none, so it stays ``None``; under SQLite a database that
    appeared or changed shows as a difference beside the export's bytes.
    """
    paths = {collection.value: state_dir / FILE_NAMES[collection] for collection in Collection}
    paths["editorial_profile_legacy"] = state_dir / LEGACY_PROFILE_FILE_NAME
    snapshot = {name: path.read_bytes() if path.is_file() else None for name, path in paths.items()}
    shards = history_shards(state_dir)
    snapshot["candidate_history_shards"] = (
        b"".join(name.encode("utf-8") + b"\0" + content + b"\0" for name, content in shards.items()) if shards else None
    )
    snapshot["state_database"] = database_checksum(state_database_path(state_dir))
    return snapshot


def state_databases() -> dict[str, bytes]:
    """The content checksum of every state database the test has, by file name; empty on the JSON backend.

    Under ``--state-backend sqlite`` each test keeps its databases in a
    directory of its own (``conftest.py``), so a zero-mutation test that
    compares the files it watches can add this to see every database a
    command created or changed as well (#1075).
    """
    configured = os.environ.get(DATABASE_DIR_ENV, "").strip()
    if not configured:
        return {}
    databases = sorted(Path(configured).glob(f"*{DATABASE_SUFFIX}"))
    return {f"<state database>/{path.name}": database_checksum(path) or b"" for path in databases}


def database_checksum(database: Path) -> bytes | None:
    """SHA-256 over the content of *database* (its schema and every row, in order); ``None`` when it does not exist."""
    if not database.is_file():
        return None
    conn = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    try:
        digest = hashlib.sha256()
        for statement in conn.iterdump():
            digest.update(statement.encode("utf-8") + b"\n")
        return digest.digest()
    finally:
        conn.close()


class _HookedCommitConnection:
    """A database connection that calls a hook before each COMMIT; a hook that raises rolls the transaction back."""

    def __init__(self, conn: sqlite3.Connection, hook: Callable[[], None]) -> None:
        self._conn = conn
        self._hook = hook

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        if sql.strip().upper() == "COMMIT":
            try:
                self._hook()
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return self._conn.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def before_database_commit(monkeypatch: Any, hook: Callable[[], None]) -> None:
    """Call *hook* before the SQLite backend commits a State Store transaction.

    A test that holds or fails the JSON backend's file publication installs
    the same behaviour here for the SQLite backend, whose commit is the
    database's. On the JSON backend no database is opened, so it changes
    nothing there.
    """
    from autoscout.state_store import sqlite_backend

    real_connect = sqlite_backend.SqliteStateStore._connect

    def connect(self: Any) -> Any:
        return _HookedCommitConnection(real_connect(self), hook)

    monkeypatch.setattr(sqlite_backend.SqliteStateStore, "_connect", connect)


def fail_database_commits(monkeypatch: Any, call: int | None = None, message: str = "no space left on device") -> None:
    """Make the SQLite backend's commits fail with ``OSError(message)``, like a full disk: every one, or only the *call*-th."""
    commits = {"count": 0}

    def fail() -> None:
        commits["count"] += 1
        if call is None or commits["count"] == call:
            raise OSError(message)

    before_database_commit(monkeypatch, fail)
