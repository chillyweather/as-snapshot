"""The SQLite backend of the State Store (#1074, ADR 0023).

Canonical state lives in one SQLite database file (``sqlite_location``), in
the tables ``sqlite_schema`` describes, through the standard library's
``sqlite3`` only. Every connection runs with a busy timeout of
``BUSY_TIMEOUT_SECONDS``, foreign keys on and ``synchronous=FULL``, and the
database is in WAL mode, so readers never wait for a writer and a second
writer waits for the first instead of failing.

The store keeps the JSON backend's observable behaviour: the same values
come back from a read, a collection never written reads as its empty value,
and records come back in the same order with their keys sorted at every
level. A record in the database does not need a Candidate ID of the form the
one-file-per-book layout requires; only the export into that layout does.

A write outside a transaction is one SQLite transaction. ``transaction()``
takes the write lock at its first read or write and holds it to the end of
the block, so a read inside it decides a write safely (on a state directory
with no database yet, that first read creates the empty database); an
exception inside the block rolls back every write in it.

Git keeps a JSON export of the database, never the database itself.
``export_json`` writes it with the JSON backend's own writer, in the layout
the state directory already has, rewriting only the files whose records
changed since the last export. ``rebuild_from_json`` builds a database from
a state directory of JSON files, with the JSON backend's validation, and
creates nothing when a record is refused.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import time
from collections import Counter
from collections import defaultdict
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace as updated
from pathlib import Path
from typing import Any
from typing import NamedTuple

from autoscout.candidate_identity import empty_alias_table
from autoscout.candidate_identity import validate_alias_table
from autoscout.editor_decisions import DECISION_LOG_FIELD
from autoscout.editorial_taste_profile import empty_profile
from autoscout.sort_utils import sort_by_key
from autoscout.state_store import EXPORT_BEHIND
from autoscout.state_store import EXPORT_CHANGED_OUTSIDE
from autoscout.state_store import EXPORT_CURRENT
from autoscout.state_store import EXPORT_NEVER
from autoscout.state_store import EXPORT_NO_DATABASE
from autoscout.state_store import EXPORT_UNREADABLE
from autoscout.state_store import JSON_BACKEND
from autoscout.state_store import MAX_DIFFERING_NAMES
from autoscout.state_store import SQLITE_BACKEND
from autoscout.state_store import STATE_DATABASE_ENV
from autoscout.state_store import BackendSelection
from autoscout.state_store import Collection
from autoscout.state_store import StateInspection
from autoscout.state_store import StateStoreError
from autoscout.state_store.json_backend import LEGACY_PROFILE_FILE_NAME
from autoscout.state_store.json_backend import JsonStateStore
from autoscout.state_store.json_backend import canonical_bytes
from autoscout.state_store.json_backend import canonical_value
from autoscout.state_store.json_backend import collection_path
from autoscout.state_store.json_backend import holds_json_state
from autoscout.state_store.json_backend import stored_state_digest
from autoscout.state_store.layout import CANDIDATE_HISTORY_DIR
from autoscout.state_store.layout import HistoryLayout
from autoscout.state_store.layout import history_layout
from autoscout.state_store.layout import iter_shard_files
from autoscout.state_store.layout import serialize_record
from autoscout.state_store.layout import shard_relative_path
from autoscout.state_store.layout import valid_candidate_id
from autoscout.state_store.marker import BACKEND_MARKER_FILE
from autoscout.state_store.marker import marked_backend
from autoscout.state_store.sqlite_location import DEFAULT_DATABASE_DIR
from autoscout.state_store.sqlite_location import NETWORK
from autoscout.state_store.sqlite_location import REPOSITORY_ROOT
from autoscout.state_store.sqlite_location import SYNCED
from autoscout.state_store.sqlite_location import database_path_class
from autoscout.state_store.sqlite_location import display_path
from autoscout.state_store.sqlite_location import state_dir_key
from autoscout.state_store.sqlite_schema import LIST_TABLES
from autoscout.state_store.sqlite_schema import MIGRATIONS
from autoscout.state_store.sqlite_schema import SCHEMA_VERSION
from autoscout.state_store.sqlite_schema import list_table_version

BUSY_TIMEOUT_SECONDS = 30.0
# The layout an export record names after a rebuild from files not in the form
# the store writes: the state directory's digest is recorded, so the first
# export still refuses a change made outside the database since the rebuild,
# but no layout matches it, so that export is a full one that rewrites those
# files (#1093).
_REBUILT_NOT_EXPORTED = "rebuilt_not_exported"
REBUILD_COMMAND = "./scripts/state-database rebuild"

# The table of each collection kept as ordered rows of JSON text.
_LIST_TABLES: dict[Collection, str] = {
    Collection.EXCLUDED_CANDIDATE_HISTORY: "excluded_candidate_history",
    Collection.CANDIDATE_VISIBILITY: "candidate_visibility",
    Collection.PENDING_EDITOR_ROWS: "pending_editor_rows",
    Collection.ARCHIVED_EDITOR_ROWS: "archived_editor_rows",
    Collection.EDITORIAL_PROFILE: "editorial_profile_entries",
    Collection.SOURCE_PROPOSALS: "source_proposals",
    Collection.RANKING_FRESHNESS: "ranking_freshness",
    Collection.LATEST_SCOUTING_CANDIDATES: "latest_scouting_candidates",
    Collection.SOURCE_FAILURE_STREAKS: "source_failure_streaks",
}
_LIST_KEYS: dict[Collection, str | None] = {
    collection: LIST_TABLES[table] for collection, table in _LIST_TABLES.items()
}
_ALIAS_TABLES = ("candidate_identity_aliases", "candidate_identity_candidates", "candidate_identity_alias_table")

# A decision log entry's fields (#1053), in the order its keys are serialized.
_DECISION_FIELDS = ("decision_date", "description_digest", "note", "status")


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _author_display(record: Mapping[str, Any]) -> str | None:
    authors = record.get("primary_authors")
    if isinstance(authors, list):
        return ", ".join(str(author) for author in authors if str(author).strip())
    return _text(authors)


def _checked_candidate_id(candidate_id: Any) -> str:
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise StateStoreError("a Candidate History record needs a nonblank candidate_id")
    return candidate_id


# ── Candidate History rows ────────────────────────────────────────────────────


class _HistoryRow(NamedTuple):
    """One Candidate History record as the database keeps it."""

    candidate_id: str | None
    identity_key: str | None
    title: str | None
    author_display: str | None
    editor_status: str | None
    first_seen_date: str | None
    last_seen_date: str | None
    last_material_change_date: str | None
    has_decision_log: int
    record_json: str
    decisions: tuple[tuple[str | None, ...], ...]

    @property
    def content(self) -> tuple[int, str, tuple[tuple[str | None, ...], ...]]:
        return self.has_decision_log, self.record_json, self.decisions

    def columns(self) -> tuple[Any, ...]:
        return tuple(self)[:10]


def _decision_rows(candidate_id: Any, log: Any) -> tuple[tuple[str | None, ...], ...]:
    """The decision log as table rows; anything but flat entries of exactly the four fields is refused."""
    if isinstance(log, list):
        rows = []
        for entry in log:
            if not isinstance(entry, Mapping) or set(entry) != set(_DECISION_FIELDS):
                break
            values = tuple(entry[name] for name in _DECISION_FIELDS)
            if not all(value is None or isinstance(value, str) for value in values):
                break
            rows.append(values)
        else:
            return tuple(rows)
    raise StateStoreError(
        f"the decision log of Candidate History record {candidate_id!r} must be a list of entries with exactly "
        f"{', '.join(sorted(_DECISION_FIELDS))}, each a string or null"
    )


def _history_row(record: Any) -> _HistoryRow:
    if not isinstance(record, Mapping):
        raise StateStoreError("every Candidate History record must be a JSON object")
    body = dict(record)
    has_log = DECISION_LOG_FIELD in body
    decisions = _decision_rows(body.get("candidate_id"), body.pop(DECISION_LOG_FIELD)) if has_log else ()
    return _HistoryRow(
        candidate_id=_text(body.get("candidate_id")),
        identity_key=_text(body.get("identity_key")),
        title=_text(body.get("title")),
        author_display=_author_display(body),
        editor_status=_text(body.get("editor_status")),
        first_seen_date=_text(body.get("first_seen_date")),
        last_seen_date=_text(body.get("last_seen_date")),
        last_material_change_date=_text(body.get("last_material_change_date")),
        has_decision_log=int(has_log),
        record_json=_compact(body),
        decisions=decisions,
    )


def _history_rows(value: Any) -> list[_HistoryRow]:
    if not isinstance(value, list):
        raise StateStoreError(f"candidate_history must be a list of records, got {type(value).__name__}")
    return [_history_row(record) for record in value]


def _assemble(record_json: str, has_decision_log: int, decisions: list[tuple[str | None, ...]]) -> dict[str, Any]:
    """The record as stored: its keys sorted at every level, its decision log back in place."""
    record: dict[str, Any] = json.loads(record_json)
    if not has_decision_log:
        return record
    record[DECISION_LOG_FIELD] = [dict(zip(_DECISION_FIELDS, entry, strict=True)) for entry in decisions]
    return {key: record[key] for key in sorted(record)}


def _sorted_history(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The JSON backend's order (``sort_by_key`` on ``identity_key``), without its tie-breaker when no key repeats."""
    keys = [str(record.get("identity_key", "")) for record in records]
    if len(set(keys)) != len(keys):
        return sort_by_key(records, "identity_key")
    return [record for _key, record in sorted(zip(keys, records, strict=True), key=lambda pair: pair[0])]


# ── Reports ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExportReport:
    status: str  # exported, unchanged or no_database
    database: str
    layout: str = ""
    mode: str = ""  # incremental or full
    files_written: int = 0
    files_deleted: int = 0
    change_seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RebuildReport:
    database: str
    layout: str
    record_count: int
    collection_counts: dict[str, int]
    database_bytes: int
    change_seq: int
    # The canonical files whose bytes are not the form the JSON backend writes;
    # the first export rewrites them. Empty means export(rebuild) is byte-identical.
    non_canonical: list[str] = field(default_factory=list)
    replaced_existing: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── The store ─────────────────────────────────────────────────────────────────


def _refuse_committable_location(state_dir: Path, database: Path) -> None:
    """Refuse a database Git could pick up: inside the state directory, or in the repository outside its ignored directory.

    The database is never committed (ADR 0023). Its default directory is
    git-ignored, a state directory is committed, and any other path inside
    the repository would show as a change the envelope refuses or commits.
    """
    path = database.resolve()
    if path.is_relative_to(state_dir.resolve()):
        raise StateStoreError(f"the state database must not be inside the state directory: {display_path(database)}")
    if path.is_relative_to(REPOSITORY_ROOT) and not path.is_relative_to(DEFAULT_DATABASE_DIR):
        raise StateStoreError(
            f"the state database {display_path(database)} is inside the repository but not in the git-ignored "
            f"{display_path(DEFAULT_DATABASE_DIR)}/; put it there or outside the repository"
        )


def _schema_version(conn: sqlite3.Connection) -> int:
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'").fetchone()
    if exists is None:
        return 0
    row = conn.execute("SELECT max(version) FROM schema_version").fetchone()
    return int(row[0] or 0)


def _use_wal(conn: sqlite3.Connection) -> None:
    """Put the database in WAL mode, waiting out another connection doing the same.

    Changing the journal mode takes a lock that the busy timeout does not
    wait for, so two processes opening a new database at once retry here.
    """
    deadline = time.monotonic() + BUSY_TIMEOUT_SECONDS
    while True:
        try:
            if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
                return
            conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def _statements(script: str) -> list[str]:
    return [statement.strip() for statement in script.split(";") if statement.strip()]


class SqliteStateStore:
    """The State Store over one SQLite database file."""

    def __init__(self, state_dir: Path, selection: BackendSelection, database: Path, *, building: bool = False) -> None:
        self._state_dir = state_dir
        # A rebuild fills a new database from the JSON beside it, which an ordinary store refuses.
        self._building = building
        self._selection = selection
        self._database = database
        self._state_key = state_dir_key(state_dir)
        self._path_class = database_path_class(database)
        if self._path_class in {SYNCED, NETWORK}:
            raise StateStoreError(
                f"the state database {display_path(database)} is on a {self._path_class} path, which can corrupt "
                "it; configure a local path with state_database or AUTOSCOUT_STATE_DATABASE"
            )
        _refuse_committable_location(state_dir, database)
        self._in_transaction = False
        # The transaction's connection, opened at its first read or write.
        self._conn: sqlite3.Connection | None = None
        # The change marker of the current write, allocated at its first change.
        self._change_seq: int | None = None

    @property
    def database(self) -> Path:
        return self._database

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    # ── Connections ───────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        """A connection to the database, created and migrated to the current schema as needed."""
        self._database.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = sqlite3.connect(self._database, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
        except sqlite3.Error as exc:
            raise StateStoreError(f"cannot open the state database {display_path(self._database)}: {exc}") from exc
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA synchronous = FULL")
            _use_wal(conn)
            self._prepare(conn)
        except sqlite3.DatabaseError as exc:
            conn.close()
            raise StateStoreError(f"the state database {display_path(self._database)} is unusable: {exc}") from exc
        except BaseException:
            conn.close()
            raise
        return conn

    def _prepare(self, conn: sqlite3.Connection) -> None:
        """Apply the missing migrations, and refuse a database of another state directory or a newer schema."""
        if _schema_version(conn) != SCHEMA_VERSION:
            conn.execute("BEGIN IMMEDIATE")
            try:
                version = _schema_version(conn)
                if version > SCHEMA_VERSION:
                    raise StateStoreError(
                        f"the state database {display_path(self._database)} has schema version {version}, newer "
                        f"than this AutoScout's {SCHEMA_VERSION}"
                    )
                if version == 0:
                    conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
                for number in range(version + 1, SCHEMA_VERSION + 1):
                    for statement in _statements(MIGRATIONS[number - 1]):
                        conn.execute(statement)
                    conn.execute(
                        "INSERT INTO schema_version (version, applied_at) VALUES (?, datetime('now'))", (number,)
                    )
                if version == 0:
                    conn.execute("INSERT INTO store_meta (name, value) VALUES ('state_dir', ?)", (self._state_key,))
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        owner = conn.execute("SELECT value FROM store_meta WHERE name = 'state_dir'").fetchone()
        if owner is not None and owner[0] != self._state_key:
            raise StateStoreError(
                f"the state database {display_path(self._database)} belongs to the state directory {owner[0]}, "
                f"not to {self._state_key}"
            )

    def _refuse_unbuilt_database(self) -> None:
        """Refuse to start an empty database beside JSON state it would silently ignore, or in a directory cut over to SQLite.

        A checkout that pulled a cut-over state directory has its export but
        not its database, which is never committed; reading it as empty would
        lose every record.
        """
        if self._building:
            return
        if marked_backend(self._state_dir) == SQLITE_BACKEND:
            raise StateStoreError(
                f"the state directory is kept in SQLite ({BACKEND_MARKER_FILE}) but its state database "
                f"{display_path(self._database)} does not exist on this machine; build it from the committed JSON "
                f"with {REBUILD_COMMAND}, or set {STATE_DATABASE_ENV} to the "
                "database if it is kept elsewhere"
            )
        if holds_json_state(self._state_dir):
            raise StateStoreError(
                f"the state directory holds JSON state but the state database {display_path(self._database)} "
                f"does not exist; build it from the JSON with {REBUILD_COMMAND}"
            )

    @contextmanager
    def _reading(self) -> Iterator[sqlite3.Connection | None]:
        """A connection with a consistent view, or ``None`` while there is no database (every collection empty)."""
        if self._in_transaction:
            # A transaction takes the write lock at its first read, creating an
            # empty database if there is none yet, so a write it decides on that
            # read can never undo a concurrent writer's.
            if self._conn is None:
                self._begin_transaction_connection()
            yield self._conn
            return
        if not self._database.exists():
            self._refuse_unbuilt_database()
            yield None
            return
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            yield conn
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            conn.close()

    @contextmanager
    def _writing(self) -> Iterator[sqlite3.Connection]:
        """A connection inside a write transaction; one operation is all-or-nothing even inside a larger one."""
        if not self._in_transaction:
            with self.transaction(), self._writing() as conn:
                yield conn
            return
        if self._conn is None:
            self._begin_transaction_connection()
        connection = self._conn
        assert connection is not None
        connection.execute("SAVEPOINT state_store_operation")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK TO state_store_operation")
            connection.execute("RELEASE state_store_operation")
            raise
        connection.execute("RELEASE state_store_operation")

    def _begin_transaction_connection(self) -> None:
        if not self._database.exists():
            self._refuse_unbuilt_database()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            conn.close()
            raise
        self._conn = conn

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._in_transaction:
            raise StateStoreError("State Store transactions do not nest")
        self._in_transaction = True
        self._change_seq = None
        try:
            yield
        except BaseException:
            if self._conn is not None:
                self._conn.execute("ROLLBACK")
            raise
        else:
            if self._conn is not None:
                self._conn.execute("COMMIT")
        finally:
            if self._conn is not None:
                self._conn.close()
            self._conn = None
            self._in_transaction = False
            self._change_seq = None

    # ── Change markers ────────────────────────────────────────────────────

    def _marker(self, conn: sqlite3.Connection) -> int:
        """The change marker of this write: one new counter value per transaction."""
        if self._change_seq is None:
            conn.execute("UPDATE store_meta SET value = CAST(value AS INTEGER) + 1 WHERE name = 'change_seq'")
            self._change_seq = _current_change_seq(conn)
        return self._change_seq

    def _mark_stored(self, conn: sqlite3.Connection, collection: Collection) -> None:
        conn.execute(
            "INSERT INTO collection_state (collection, stored, change_seq) VALUES (?, 1, ?) "
            "ON CONFLICT (collection) DO UPDATE SET stored = 1, change_seq = excluded.change_seq",
            (collection.value, self._marker(conn)),
        )

    # ── The State Store interface ─────────────────────────────────────────

    def describe(self) -> dict[str, str]:
        """The backend, its database and whether the last export into the state directory is current."""
        description = {
            **self._selection.describe(),
            "database": display_path(self._database),
            "database_path_class": self._path_class,
            "schema_version": "",
            "change_seq": "",
            "export_change_seq": "",
            "export_current": "no_database",
        }
        if not self._database.exists():
            return description
        with self._reading() as conn:
            assert conn is not None
            change_seq = _current_change_seq(conn)
            export = _export_record(conn, self._state_key)
            description["schema_version"] = str(_schema_version(conn))
        description["change_seq"] = str(change_seq)
        if export is None or export[0] == _REBUILT_NOT_EXPORTED:
            description["export_current"] = "never"
        else:
            description["export_change_seq"] = str(export[1])
            description["export_current"] = "yes" if export[1] == change_seq else "no"
        return description

    def _connect_read_only(self) -> tuple[sqlite3.Connection, tempfile.TemporaryDirectory[str] | None]:
        """A connection that changes nothing beside the database, not even the WAL and shared-memory files.

        A read-only connection to a WAL database creates the ``-shm`` (and an
        empty ``-wal``) it lacks. So a database with no WAL content, every
        connection having closed cleanly, is opened ``immutable``, which has
        no WAL to miss and touches nothing. A database with WAL content and
        its ``-shm`` is opened read-only, so the writes in it are seen and
        nothing is created. A database with WAL content and no ``-shm`` (a
        recovered database and WAL pair) is read from a scratch copy, which
        the caller removes, so SQLite creates its files there.
        """
        # SQLite keeps the WAL and shared memory beside the file a symbolic link points to.
        database = self._database.resolve()
        uri = database.as_uri()
        wal = database.with_name(database.name + "-wal")
        shm = database.with_name(database.name + "-shm")
        scratch: tempfile.TemporaryDirectory[str] | None = None
        if wal.exists() and wal.stat().st_size and not shm.exists():
            scratch = tempfile.TemporaryDirectory(prefix="autoscout-inspect-")
            copy = Path(scratch.name) / database.name
            shutil.copy2(database, copy)
            shutil.copy2(wal, copy.with_name(copy.name + "-wal"))
            uri, mode = copy.as_uri(), "mode=ro"
        else:
            mode = "mode=ro" if wal.exists() and shm.exists() else "immutable=1"
        try:
            conn = sqlite3.connect(f"{uri}?{mode}", uri=True, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
        except BaseException:
            if scratch is not None:
                scratch.cleanup()
            raise
        return conn, scratch

    def inspect(self) -> StateInspection:
        """Compare the database with its export, changing nothing (#1076, the doctor's and run-health's look).

        The database is opened read-only: no migration, no WAL switch, no
        transaction of the store, and a missing database is not created. The
        comparison is by content, so a database whose change marker moved
        without changing a record still matches its export.
        """
        found = StateInspection(
            backend=self._selection.backend,
            selected_by=self._selection.selected_by,
            database=display_path(self._database),
            database_path_class=self._path_class,
        )
        if not self._database.exists():
            if marked_backend(self._state_dir) == SQLITE_BACKEND or holds_json_state(self._state_dir):
                return updated(found, export_status=EXPORT_UNREADABLE, problem="database_missing")
            return updated(found, export_status=EXPORT_NO_DATABASE)
        try:
            conn, scratch = self._connect_read_only()
        except sqlite3.Error:
            return updated(found, export_status=EXPORT_UNREADABLE, problem="database_unusable")
        except OSError:
            return updated(found, export_status=EXPORT_UNREADABLE, problem="state_unreadable")
        try:
            conn.execute("PRAGMA query_only = ON")
            conn.execute("BEGIN")
            version = _schema_version(conn)
            # An older schema is one the next store to open it migrates; until then it is read as it is (#1125).
            if not 1 <= version <= SCHEMA_VERSION:
                return updated(
                    found, schema_version=version, export_status=EXPORT_UNREADABLE, problem="schema_version_mismatch"
                )
            owner = conn.execute("SELECT value FROM store_meta WHERE name = 'state_dir'").fetchone()
            if owner is not None and owner[0] != self._state_key:
                return updated(
                    found, schema_version=version, export_status=EXPORT_UNREADABLE, problem="other_state_directory"
                )
            change_seq = _current_change_seq(conn)
            export = _export_record(conn, self._state_key)
            differing = _differing_export_files(conn, self._state_dir, version)
            # Whether the files are as the last export left them, so a difference is the database's or an outside edit.
            changed_outside = (
                bool(differing) and export is not None and stored_state_digest(self._state_dir) != export[2]
            )
        except (sqlite3.Error, TypeError, AttributeError, KeyError):
            # A damaged database: a missing change marker row, a record that is not an object.
            return updated(found, export_status=EXPORT_UNREADABLE, problem="database_unusable")
        except (ValueError, OSError):
            return updated(found, export_status=EXPORT_UNREADABLE, problem="state_unreadable")
        finally:
            conn.close()
            if scratch is not None:
                scratch.cleanup()
        status = EXPORT_CURRENT
        if differing:
            if export is None:
                status = EXPORT_NEVER
            elif changed_outside:
                status = EXPORT_CHANGED_OUTSIDE
            else:
                status = EXPORT_BEHIND
        return updated(
            found,
            schema_version=version,
            change_seq=change_seq,
            export_change_seq=None if export is None else export[1],
            export_status=status,
            differing=tuple(differing[:MAX_DIFFERING_NAMES]),
            differing_count=len(differing),
        )

    def read(self, collection: Collection) -> Any:
        with self._reading() as conn:
            return _read_collection(conn, collection)

    def replace(self, collection: Collection, value: Any) -> None:
        if collection is Collection.CANDIDATE_HISTORY:
            rows = _history_rows(value)
            with self._writing() as conn:
                self._replace_history(conn, rows)
            return
        if collection is Collection.CANDIDATE_IDENTITY_ALIASES:
            table = validate_alias_table(value)
            with self._writing() as conn:
                self._replace_alias_table(conn, table)
            return
        canonical = canonical_value(collection, value)
        records = canonical["entries"] if collection is Collection.EDITORIAL_PROFILE else canonical
        key = _LIST_KEYS[collection]
        list_rows = [
            (position, _text(record.get(key)) if key and isinstance(record, Mapping) else None, _compact(record))
            for position, record in enumerate(records)
        ]
        table_name = _LIST_TABLES[collection]
        with self._writing() as conn:
            existing = conn.execute(f"SELECT position, record_key, record_json FROM {table_name} ORDER BY position")
            if [tuple(row) for row in existing] == list_rows and _is_stored(conn, collection):
                return
            conn.execute(f"DELETE FROM {table_name}")
            conn.executemany(
                f"INSERT INTO {table_name} (position, record_key, record_json) VALUES (?, ?, ?)", list_rows
            )
            self._mark_stored(conn, collection)

    def get_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        _checked_candidate_id(candidate_id)
        with self._reading() as conn:
            if conn is None:
                return None
            records = _history_records(conn, "WHERE candidate_id = ?", (candidate_id,))
        return _sorted_history(records)[0] if records else None

    def upsert_candidate(self, record: Mapping[str, Any]) -> None:
        candidate_id = _checked_candidate_id(record.get("candidate_id"))
        row = _history_row(record)
        with self._writing() as conn:
            existing = conn.execute(
                "SELECT record_id, has_decision_log, record_json FROM candidate_history WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchall()
            if len(existing) == 1:
                record_id = int(existing[0][0])
                stored_decisions = _decisions_by_record(conn, [record_id]).get(record_id, [])
                if (existing[0][1], existing[0][2], tuple(stored_decisions)) == row.content and _is_stored(
                    conn, Collection.CANDIDATE_HISTORY
                ):
                    return
                self._update_history_row(conn, record_id, row)
            else:
                conn.execute("DELETE FROM candidate_history WHERE candidate_id = ?", (candidate_id,))
                self._insert_history_row(conn, row)
            self._mark_stored(conn, Collection.CANDIDATE_HISTORY)

    def delete_candidate(self, candidate_id: str) -> bool:
        _checked_candidate_id(candidate_id)
        with self._reading() as conn:
            if conn is None:
                return False
            found = conn.execute(
                "SELECT 1 FROM candidate_history WHERE candidate_id = ? LIMIT 1", (candidate_id,)
            ).fetchone()
        if found is None:
            return False
        with self._writing() as conn:
            if not conn.execute("DELETE FROM candidate_history WHERE candidate_id = ?", (candidate_id,)).rowcount:
                return False
            self._mark_stored(conn, Collection.CANDIDATE_HISTORY)
        return True

    # ── Writes ────────────────────────────────────────────────────────────

    def _insert_history_row(self, conn: sqlite3.Connection, row: _HistoryRow) -> None:
        cursor = conn.execute(
            "INSERT INTO candidate_history (candidate_id, identity_key, title, author_display, editor_status, "
            "first_seen_date, last_seen_date, last_material_change_date, has_decision_log, record_json, change_seq) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*row.columns(), self._marker(conn)),
        )
        assert cursor.lastrowid is not None
        self._insert_decisions(conn, cursor.lastrowid, row)

    def _update_history_row(self, conn: sqlite3.Connection, record_id: int, row: _HistoryRow) -> None:
        conn.execute(
            "UPDATE candidate_history SET candidate_id = ?, identity_key = ?, title = ?, author_display = ?, "
            "editor_status = ?, first_seen_date = ?, last_seen_date = ?, last_material_change_date = ?, "
            "has_decision_log = ?, record_json = ?, change_seq = ? WHERE record_id = ?",
            (*row.columns(), self._marker(conn), record_id),
        )
        conn.execute("DELETE FROM editor_decisions WHERE record_id = ?", (record_id,))
        self._insert_decisions(conn, record_id, row)

    def _insert_decisions(self, conn: sqlite3.Connection, record_id: int, row: _HistoryRow) -> None:
        conn.executemany(
            "INSERT INTO editor_decisions (record_id, position, decision_date, description_digest, note, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(record_id, position, *entry) for position, entry in enumerate(row.decisions)],
        )

    def _replace_history(self, conn: sqlite3.Connection, rows: list[_HistoryRow]) -> None:
        """Keep each record whose content is unchanged, so its change marker stays; rewrite the rest."""
        existing = conn.execute(
            "SELECT record_id, candidate_id, has_decision_log, record_json FROM candidate_history"
        ).fetchall()
        decisions = _decisions_by_record(conn, None)
        new_ids = Counter(row.candidate_id for row in rows if row.candidate_id is not None)
        old_ids = Counter(candidate_id for _record_id, candidate_id, _log, _json in existing if candidate_id)
        old_by_id = {
            candidate_id: (int(record_id), (has_log, record_json, tuple(decisions.get(int(record_id), []))))
            for record_id, candidate_id, has_log, record_json in existing
            if candidate_id is not None and old_ids[candidate_id] == 1
        }
        kept: set[int] = set()
        updates: list[tuple[int, _HistoryRow]] = []
        inserts: list[_HistoryRow] = []
        for row in rows:
            match = old_by_id.get(row.candidate_id) if row.candidate_id and new_ids[row.candidate_id] == 1 else None
            if match is None:
                inserts.append(row)
                continue
            record_id, content = match
            kept.add(record_id)
            if content != row.content:
                updates.append((record_id, row))
        removed = [int(record_id) for record_id, *_rest in existing if int(record_id) not in kept]
        if not (updates or inserts or removed) and _is_stored(conn, Collection.CANDIDATE_HISTORY):
            return
        conn.executemany("DELETE FROM candidate_history WHERE record_id = ?", [(record_id,) for record_id in removed])
        for record_id, row in updates:
            self._update_history_row(conn, record_id, row)
        for row in inserts:
            self._insert_history_row(conn, row)
        self._mark_stored(conn, Collection.CANDIDATE_HISTORY)

    def _replace_alias_table(self, conn: sqlite3.Connection, table: dict[str, Any]) -> None:
        aliases = sorted(table["aliases"].items())
        candidates = sorted(
            (candidate_id, _compact(metadata)) for candidate_id, metadata in table["candidates"].items()
        )
        document = (
            int(table["version"]),
            _compact(table["redirects"]) if "redirects" in table else None,
            _compact(table["durable_merges"]) if "durable_merges" in table else None,
        )
        current = (
            [
                tuple(row)
                for row in conn.execute("SELECT alias, candidate_id FROM candidate_identity_aliases ORDER BY alias")
            ],
            [
                tuple(row)
                for row in conn.execute(
                    "SELECT candidate_id, metadata_json FROM candidate_identity_candidates ORDER BY candidate_id"
                )
            ],
            conn.execute(
                "SELECT version, redirects_json, durable_merges_json FROM candidate_identity_alias_table"
            ).fetchone(),
        )
        if current == (aliases, candidates, document) and _is_stored(conn, Collection.CANDIDATE_IDENTITY_ALIASES):
            return
        for name in _ALIAS_TABLES:
            conn.execute(f"DELETE FROM {name}")
        conn.executemany("INSERT INTO candidate_identity_aliases (alias, candidate_id) VALUES (?, ?)", aliases)
        conn.executemany(
            "INSERT INTO candidate_identity_candidates (candidate_id, metadata_json) VALUES (?, ?)", candidates
        )
        conn.execute(
            "INSERT INTO candidate_identity_alias_table (singleton, version, redirects_json, durable_merges_json) "
            "VALUES (1, ?, ?, ?)",
            document,
        )
        self._mark_stored(conn, Collection.CANDIDATE_IDENTITY_ALIASES)

    def _set_stored(self, stored: Mapping[Collection, bool]) -> None:
        """Record which collections the JSON backend keeps a file for (rebuild only)."""
        with self._writing() as conn:
            conn.executemany(
                "UPDATE collection_state SET stored = ? WHERE collection = ?",
                [(int(flag), collection.value) for collection, flag in stored.items()],
            )

    # ── Export ────────────────────────────────────────────────────────────

    def export_json(self, *, force: bool = False) -> ExportReport:
        """Write the database into the state directory as the JSON backend would keep it.

        Every collection is written with the JSON backend's writer, in the
        layout the state directory has (one file per book for a new or empty
        one). A file is written only when its bytes change, a removed book's
        file is deleted, and when the last export into this state directory
        is known only the books changed since it are considered.

        A state directory changed since the last export by anything but the
        export is refused, because writing the database over it would lose
        that change; *force* writes the database over it.
        """
        if self._in_transaction:
            raise StateStoreError("the State Store exports outside a transaction")
        if not self._database.exists():
            if marked_backend(self._state_dir) == SQLITE_BACKEND:
                self._refuse_unbuilt_database()
            return ExportReport(status="no_database", database=display_path(self._database))
        with self.transaction():
            return self._export(force)

    @contextmanager
    def replacing_and_exporting(self, collection: Collection, value: Any) -> Iterator[ExportReport]:
        """Replace *collection* and export it, in one transaction that stays open for the block.

        See ``autoscout.state_store.replace_and_export``. The export must be
        current before the replacement, so the export writes *collection*'s
        file and nothing else; an exception in the block rolls back the
        replacement and the export record, and leaves the files to the caller.

        A new state directory, with no state in the database and none in its
        files, has nothing an export could miss, so it counts as current.
        """
        if self._in_transaction:
            raise StateStoreError("the State Store exports outside a transaction")
        with self.transaction():
            with self._reading() as conn:
                assert conn is not None
                record = _export_record(conn, self._state_key)
                change_seq = _current_change_seq(conn)
                if record is None:
                    current = change_seq == 0 and not holds_json_state(self._state_dir)
                else:
                    current = (
                        record[0] != _REBUILT_NOT_EXPORTED
                        and record[1] == change_seq
                        and record[2] == stored_state_digest(self._state_dir)
                    )
            if not current:
                raise StateStoreError(
                    "the state directory's JSON export is not current with the state database; export it with "
                    "./scripts/state-database export and commit it first"
                )
            self.replace(collection, value)
            yield self._export(force=False)

    def _export(self, force: bool) -> ExportReport:
        """The export, inside this store's transaction; see ``export_json``."""
        database = display_path(self._database)
        # The write lock is held from the snapshot to the watermark, so two
        # exports, or an export and a write, never interleave.
        with self._writing() as conn:
            layout = history_layout(self._state_dir)
            on_disk = {candidate_id for candidate_id, _path in iter_shard_files(self._state_dir)}
            change_seq = _current_change_seq(conn)
            since = self._export_watermark(conn, layout, force)
            stored = {collection: _is_stored(conn, collection) for collection in Collection}
            documents = {
                collection: _read_collection(conn, collection)
                for collection in Collection
                if collection is not Collection.CANDIDATE_HISTORY or layout is HistoryLayout.SINGLE_FILE
            }
            shard_writes, shard_deletes = (
                _shard_plan(conn, on_disk, since) if layout is HistoryLayout.SHARDED else ([], set())
            )
            writes = {
                collection: value
                for collection, value in documents.items()
                if _file_differs(self._state_dir, collection, value, stored[collection])
            }
            shard_writes = [record for record in shard_writes if self._shard_differs(record)]
            if any(stored.values()):
                # The JSON backend creates the state directory with any write,
                # an empty one-file-per-book history included.
                self._state_dir.mkdir(parents=True, exist_ok=True)
            if writes or shard_writes or shard_deletes:
                exporter = JsonStateStore(self._state_dir, BackendSelection(JSON_BACKEND, "export"), exporting=True)
                with exporter.transaction():
                    for collection, value in writes.items():
                        exporter.replace(collection, value)
                    for record in shard_writes:
                        exporter.upsert_candidate(record)
                    for candidate_id in sorted(shard_deletes):
                        exporter.delete_candidate(candidate_id)
            self._write_export_record(conn, layout.value, change_seq)
        changed = len(writes) + len(shard_writes) + len(shard_deletes)
        return ExportReport(
            status="exported" if changed else "unchanged",
            database=database,
            layout=layout.value,
            mode="full" if since is None else "incremental",
            files_written=len(writes) + len(shard_writes),
            files_deleted=len(shard_deletes),
            change_seq=change_seq,
        )

    def _export_watermark(self, conn: sqlite3.Connection, layout: HistoryLayout, force: bool) -> int | None:
        """The change marker the state directory was last exported at, or ``None`` for a full export."""
        record = _export_record(conn, self._state_key)
        if record is None:
            return None
        # The digest does not depend on the layout, so a conversion between the
        # layouts passes and an edit made with one does not.
        if stored_state_digest(self._state_dir) != record[2]:
            if force:
                return None
            raise StateStoreError(
                "the state directory changed since the database was last exported into it, by something other "
                "than the export; rebuild the database from it with "
                f"{REBUILD_COMMAND} --overwrite, or export with --force to write the database over the change"
            )
        # The marker says which books changed, not which files a new layout lacks.
        return int(record[1]) if record[0] == layout.value else None

    def _shard_differs(self, record: dict[str, Any]) -> bool:
        path = self._state_dir.joinpath(*shard_relative_path(record["candidate_id"]).parts)
        return not (path.is_file() and path.read_bytes() == serialize_record(record).encode("utf-8"))

    def _write_export_record(self, conn: sqlite3.Connection, layout: str, change_seq: int) -> None:
        """Record that the state directory now holds the export of *change_seq* in *layout*, with its digest."""
        conn.execute(
            "INSERT INTO json_exports (state_dir, layout, change_seq, tree_digest) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (state_dir) DO UPDATE SET layout = excluded.layout, change_seq = excluded.change_seq, "
            "tree_digest = excluded.tree_digest",
            (self._state_key, layout, change_seq, stored_state_digest(self._state_dir)),
        )


# ── Reads ─────────────────────────────────────────────────────────────────────


def _current_change_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM store_meta WHERE name = 'change_seq'").fetchone()
    return int(row[0])


def _export_record(conn: sqlite3.Connection, state_key: str) -> tuple[str, int, str] | None:
    row = conn.execute(
        "SELECT layout, change_seq, tree_digest FROM json_exports WHERE state_dir = ?", (state_key,)
    ).fetchone()
    return None if row is None else (str(row[0]), int(row[1]), str(row[2]))


def _is_stored(conn: sqlite3.Connection, collection: Collection) -> bool:
    row = conn.execute("SELECT stored FROM collection_state WHERE collection = ?", (collection.value,)).fetchone()
    return bool(row and row[0])


def _decisions_by_record(
    conn: sqlite3.Connection, record_ids: list[int] | None
) -> dict[int, list[tuple[str | None, ...]]]:
    query = "SELECT record_id, decision_date, description_digest, note, status FROM editor_decisions"
    parameters: tuple[Any, ...] = ()
    if record_ids is not None:
        query += f" WHERE record_id IN ({', '.join('?' for _ in record_ids)})"
        parameters = tuple(record_ids)
    grouped: dict[int, list[tuple[str | None, ...]]] = defaultdict(list)
    for record_id, *entry in conn.execute(query + " ORDER BY record_id, position", parameters):
        grouped[int(record_id)].append(tuple(entry))
    return grouped


def _history_records(
    conn: sqlite3.Connection, where: str = "", parameters: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT record_id, has_decision_log, record_json FROM candidate_history {where}", parameters
    ).fetchall()
    logged = [int(record_id) for record_id, has_log, _json in rows if has_log]
    decisions = _decisions_by_record(conn, logged if where else None) if logged else {}
    return [
        _assemble(record_json, has_log, decisions.get(int(record_id), [])) for record_id, has_log, record_json in rows
    ]


def _read_alias_table(conn: sqlite3.Connection) -> dict[str, Any]:
    document = conn.execute(
        "SELECT version, redirects_json, durable_merges_json FROM candidate_identity_alias_table"
    ).fetchone()
    if document is None:
        return empty_alias_table()
    version, redirects, durable_merges = document
    table: dict[str, Any] = {
        "aliases": dict(conn.execute("SELECT alias, candidate_id FROM candidate_identity_aliases ORDER BY alias")),
        "candidates": {
            candidate_id: json.loads(metadata)
            for candidate_id, metadata in conn.execute(
                "SELECT candidate_id, metadata_json FROM candidate_identity_candidates ORDER BY candidate_id"
            )
        },
    }
    if durable_merges is not None:
        table["durable_merges"] = json.loads(durable_merges)
    if redirects is not None:
        table["redirects"] = json.loads(redirects)
    table["version"] = int(version)
    return table


def _read_collection(conn: sqlite3.Connection | None, collection: Collection) -> Any:
    if collection is Collection.CANDIDATE_HISTORY:
        return [] if conn is None else _sorted_history(_history_records(conn))
    if collection is Collection.CANDIDATE_IDENTITY_ALIASES:
        return empty_alias_table() if conn is None else _read_alias_table(conn)
    records = (
        []
        if conn is None
        else [
            json.loads(record_json)
            for (record_json,) in conn.execute(f"SELECT record_json FROM {_LIST_TABLES[collection]} ORDER BY position")
        ]
    )
    if collection is Collection.EDITORIAL_PROFILE:
        return {"entries": records} if records else empty_profile()
    return records


def _shard_plan(
    conn: sqlite3.Connection, on_disk: set[str], since: int | None
) -> tuple[list[dict[str, Any]], set[str]]:
    """The records whose files may need writing, and the Candidate IDs whose files must go."""
    rows = conn.execute("SELECT record_id, candidate_id, change_seq FROM candidate_history").fetchall()
    ids = [candidate_id for _record_id, candidate_id, _seq in rows]
    invalid = sorted({str(candidate_id) for candidate_id in ids if not valid_candidate_id(candidate_id)})
    repeated = sorted(candidate_id for candidate_id, count in Counter(ids).items() if count > 1)
    if invalid or repeated:
        raise StateStoreError(
            "Candidate History cannot be exported one file per book: "
            + "; ".join(
                part
                for part in (
                    f"records without a candidate-<24 hex characters> ID: {', '.join(invalid[:5])}" if invalid else "",
                    f"Candidate IDs held by two records: {', '.join(repeated[:5])}" if repeated else "",
                )
                if part
            )
        )
    selected = [
        int(record_id)
        for record_id, candidate_id, change_seq in rows
        if since is None or change_seq > since or candidate_id not in on_disk
    ]
    records: list[dict[str, Any]] = []
    for start in range(0, len(selected), 500):
        batch = selected[start : start + 500]
        records.extend(_history_records(conn, f"WHERE record_id IN ({', '.join('?' for _ in batch)})", tuple(batch)))
    return records, on_disk - set(ids)


def _file_differs(state_dir: Path, collection: Collection, value: Any, stored: bool) -> bool:
    """Whether the collection's file must be written: it differs from the database, or is missing although stored."""
    path = collection_path(state_dir, collection)
    if not path.is_file():
        return stored
    return path.read_bytes() != canonical_bytes(collection, value).encode("utf-8")


def _canonical_text(document: str | bytes) -> str:
    return json.dumps(json.loads(document), sort_keys=True, ensure_ascii=False)


def _shard_holds(path: Path, record: dict[str, Any]) -> bool:
    """Whether the shard file holds *record*, whatever its formatting.

    The export leaves a shard whose value is unchanged alone (the digest that
    guards the state directory does not see formatting either), so a
    re-formatted shard is not a mismatch an export could or should repair.
    """
    try:
        # Compared as canonical text, not as Python values, so false and 0 stay different.
        return _canonical_text(path.read_bytes()) == _canonical_text(serialize_record(record))
    except ValueError:
        return False


def _differing_export_files(
    conn: sqlite3.Connection, state_dir: Path, schema_version: int = SCHEMA_VERSION
) -> list[str]:
    """The exported files that are not what the database would export, relative to *state_dir*, sorted.

    A collection file that differs or is missing although stored; in the
    sharded layout a book's file whose record differs, or that is missing, a file of no
    book in the database, and the whole of Candidate History when its records
    cannot be exported one file per book. In a database at an older
    *schema_version*, a collection whose table a later migration adds is
    empty and not stored, as the migration would leave it.
    """
    layout = history_layout(state_dir)
    differing: list[str] = []
    for collection in Collection:
        if collection is Collection.CANDIDATE_HISTORY and layout is HistoryLayout.SHARDED:
            continue
        table = _LIST_TABLES.get(collection)
        if table is not None and list_table_version(table) > schema_version:
            value, stored = _read_collection(None, collection), False
        else:
            value, stored = _read_collection(conn, collection), _is_stored(conn, collection)
        if _file_differs(state_dir, collection, value, stored):
            differing.append(collection_path(state_dir, collection).relative_to(state_dir).as_posix())
    if layout is HistoryLayout.SHARDED:
        on_disk = dict(iter_shard_files(state_dir))
        records = _history_records(conn)
        ids = [record.get("candidate_id") for record in records]
        if any(not valid_candidate_id(candidate_id) for candidate_id in ids) or len(set(ids)) != len(ids):
            differing.append(CANDIDATE_HISTORY_DIR + "/")
        else:
            for record in records:
                path = on_disk.get(record["candidate_id"])
                if path is None or not _shard_holds(path, record):
                    differing.append(shard_relative_path(record["candidate_id"]).as_posix())
            for candidate_id in set(on_disk) - set(ids):
                differing.append(shard_relative_path(candidate_id).as_posix())
    return sorted(differing)


# ── Rebuild ───────────────────────────────────────────────────────────────────


def _remove_database_files(database: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = database.with_name(database.name + suffix)
        if path.exists() or path.is_symlink():
            path.unlink()


def _install_database(staging: Path, database: Path) -> None:
    """Move the staged database into place, keeping any existing one until the move succeeded.

    The existing database is moved aside together with its WAL and journal
    files, so a WAL is never applied to the wrong database; a failed move puts
    them all back.
    """
    moved: list[tuple[Path, Path]] = []
    try:
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = database.with_name(database.name + suffix)
            if path.exists() or path.is_symlink():
                aside = path.with_name(f".{path.name}.replaced-{os.getpid()}")
                os.replace(path, aside)
                moved.append((aside, path))
        os.replace(staging, database)
    except BaseException:
        for aside, path in reversed(moved):
            os.replace(aside, path)
        raise
    for aside, _path in moved:
        aside.unlink(missing_ok=True)


def _non_canonical_files(state_dir: Path, layout: HistoryLayout, values: Mapping[Collection, Any]) -> list[str]:
    """The canonical files whose bytes are not what the JSON backend writes for their values."""
    found: list[str] = []
    for collection in Collection:
        if collection is Collection.CANDIDATE_HISTORY and layout is HistoryLayout.SHARDED:
            by_id = {record["candidate_id"]: record for record in values[collection]}
            for candidate_id, path in iter_shard_files(state_dir):
                if path.read_bytes() != serialize_record(by_id[candidate_id]).encode("utf-8"):
                    found.append(path.relative_to(state_dir).as_posix())
            continue
        path = collection_path(state_dir, collection)
        if collection is Collection.EDITORIAL_PROFILE and not path.exists():
            path = state_dir / LEGACY_PROFILE_FILE_NAME
        if path.is_file() and path.read_bytes() != canonical_bytes(collection, values[collection]).encode("utf-8"):
            found.append(path.name)
    return found


def rebuild_database(
    state_dir: Path,
    database: Path,
    selection: BackendSelection,
    *,
    overwrite: bool = False,
    require_canonical: bool = False,
) -> RebuildReport:
    """Build the database of *state_dir* from its JSON files; see ``autoscout.state_store.rebuild_from_json``."""
    existed = database.exists() or database.is_symlink()
    if existed and not overwrite:
        raise StateStoreError(
            f"the state database {display_path(database)} already exists; pass --overwrite to replace it"
        )
    store_selection = BackendSelection(SQLITE_BACKEND, selection.selected_by, selection.database)
    # Refuses a synced or network path before anything is read or written.
    SqliteStateStore(state_dir, store_selection, database)
    layout = history_layout(state_dir)
    source = JsonStateStore(state_dir, BackendSelection(JSON_BACKEND, "rebuild"))
    values = {collection: source.read(collection) for collection in Collection}
    _history_rows(values[Collection.CANDIDATE_HISTORY])
    non_canonical = _non_canonical_files(state_dir, layout, values)
    if require_canonical and non_canonical:
        raise StateStoreError(
            "these state files are not in the form the State Store writes, so an export would change them: "
            + ", ".join(non_canonical)
        )
    stored = {collection: collection_path(state_dir, collection).is_file() for collection in Collection}
    staging = database.with_name(f".{database.name}.rebuild-{os.getpid()}")
    _remove_database_files(staging)
    try:
        builder = SqliteStateStore(state_dir, store_selection, staging, building=True)
        with builder.transaction():
            for collection in Collection:
                builder.replace(collection, values[collection])
            builder._set_stored(stored)
        for collection in Collection:
            expected = values[collection]
            if collection is Collection.CANDIDATE_HISTORY:
                expected = sort_by_key(expected, "identity_key")
            elif collection is not Collection.CANDIDATE_IDENTITY_ALIASES:
                expected = canonical_value(collection, expected)
            else:
                expected = validate_alias_table(expected)
            if builder.read(collection) != expected:
                raise StateStoreError(f"{collection.value} read back from the new database differs from the JSON")
        # The digest is recorded either way, so a change made outside the
        # database before the first export is refused; files not in the store's
        # form get a record no layout matches, so that export rewrites them all.
        record_layout = _REBUILT_NOT_EXPORTED if non_canonical else layout.value
        with builder.transaction(), builder._writing() as conn:
            builder._write_export_record(conn, record_layout, _current_change_seq(conn))
        conn = sqlite3.connect(staging, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode = DELETE")
            change_seq = int(conn.execute("SELECT value FROM store_meta WHERE name = 'change_seq'").fetchone()[0])
        finally:
            conn.close()
        _install_database(staging, database)
    except BaseException:
        _remove_database_files(staging)
        raise
    counts = {collection.value: _count(values[collection]) for collection in Collection}
    return RebuildReport(
        database=display_path(database),
        layout=layout.value,
        record_count=counts[Collection.CANDIDATE_HISTORY.value],
        collection_counts=counts,
        database_bytes=database.stat().st_size,
        change_seq=change_seq,
        non_canonical=non_canonical,
        replaced_existing=existed,
    )


def _count(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, Mapping) and isinstance(value.get("entries"), list):
        return len(value["entries"])
    if isinstance(value, Mapping) and isinstance(value.get("aliases"), Mapping):
        return len(value["aliases"])
    return 0
