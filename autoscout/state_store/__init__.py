"""The State Store: the one seam for canonical cross-run state (ADR 0023, #1011).

Canonical state is read and written through a ``StateStore``. A caller names
a ``Collection`` and gets or gives its records; it never learns how or where
the backend keeps them. Each backend owns the canonical form of every
collection (record order, validation, the empty value of a missing
collection), so the same state always produces the same stored bytes.

Callers get a store with ``open_state_store(state_dir)``. The backends are
``json`` (the default until cutover) and ``sqlite`` (#1074).

Which backend serves a state directory (#1093):

1. A state directory cut over to SQLite says so itself, in its committed
   backend marker (``marker``). Every command that opens it uses SQLite,
   with or without the weekly config or the environment variable. A
   selection that names JSON there, from ``AUTOSCOUT_STATE_BACKEND``, the
   weekly config's ``state_backend`` or a caller, is refused for reads and
   writes alike: the JSON files there are an export that may lag the
   database. The JSON backend itself also refuses to write into a marked
   directory, except as the SQLite backend's exporter.
2. A state directory without a marker is kept as before: the environment
   variable, else the weekly config's key, else ``json``. An explicit
   selection of SQLite there is allowed (tests and rehearsals use it), but
   only the directory it is given to sees it.

An entry point that has the weekly config opens the store once with
``open_configured_state_store`` and passes it down, so every stage of a run
uses one store. The SQLite database file is chosen by the
``state_database`` config key or the ``AUTOSCOUT_STATE_DATABASE``
environment variable, else it is the default file of the state directory
(``sqlite_location``); a marker never names it, because the path differs
between machines. A marked directory whose database does not exist on this
machine is refused with the rebuild command, never read as empty.

``cut_over_to_sqlite`` exports the database into the directory and writes
the marker; ``roll_back_to_json`` exports again and removes it.

Under the SQLite backend Git keeps a JSON export of the database:
``export_json`` writes it into the state directory in the JSON backend's
layout before the Git envelope commits, and is a no-op on the JSON backend.
``rebuild_from_json`` builds the database from a state directory's JSON.

``inspect_state_store`` is the read-only look the doctor and run-health take
(#1076): the backend, the database's path class and schema version, and
whether the exported files are exactly what the database would export. It
opens no transaction and creates no database, WAL file or directory.

Shapes, until typed records replace them:

- ``EDITORIAL_PROFILE`` is ``{"entries": [...]}``; the store validates it on
  read and on write.
- ``CANDIDATE_IDENTITY_ALIASES`` is the alias table; validated on read and
  on write.
- Every other collection is a list of records, returned as stored.

Writes outside a transaction are applied at once. Inside
``with store.transaction():`` they are staged, later reads in the block see
them, and they are written together when the block exits normally; an
exception inside the block writes nothing. Transactions do not nest.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import AbstractContextManager
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from typing import Protocol


class Collection(Enum):
    """A canonical collection the State Store owns (the list fixed by #1011)."""

    CANDIDATE_HISTORY = "candidate_history"
    EXCLUDED_CANDIDATE_HISTORY = "excluded_candidate_history"
    CANDIDATE_VISIBILITY = "candidate_visibility"
    PENDING_EDITOR_ROWS = "pending_editor_rows"
    ARCHIVED_EDITOR_ROWS = "archived_editor_rows"
    EDITORIAL_PROFILE = "editorial_profile"
    CANDIDATE_IDENTITY_ALIASES = "candidate_identity_aliases"
    SOURCE_PROPOSALS = "source_proposals"
    RANKING_FRESHNESS = "ranking_freshness"
    LATEST_SCOUTING_CANDIDATES = "latest_scouting_candidates"
    # Issue #1125: per source, how many attempts in a row failed; schema version 2 under SQLite.
    SOURCE_FAILURE_STREAKS = "source_failure_streaks"


# The label each guarded collection is framed under in digests that were
# sealed before the State Store existed: the name of the JSON file it was kept
# in. A digest that must stay equal across the move (the feedback marker's
# state guard, #1069) frames a collection under this label. It is hash input
# only and names no file a caller may open; the layout belongs to the backend.
LEGACY_DIGEST_LABELS: dict[Collection, str] = {
    Collection.CANDIDATE_HISTORY: "candidate-history.json",
    Collection.LATEST_SCOUTING_CANDIDATES: "latest-scouting-candidates.json",
    Collection.CANDIDATE_VISIBILITY: "candidate-visibility.json",
    Collection.EDITORIAL_PROFILE: "editorial-profile.json",
    Collection.SOURCE_PROPOSALS: "source-proposals.json",
    Collection.EXCLUDED_CANDIDATE_HISTORY: "excluded-candidate-history.json",
}


class StateStoreError(ValueError):
    """A State Store refused an operation: an unknown backend, a malformed value, a nested transaction."""


class StateStore(Protocol):
    def read(self, collection: Collection) -> Any:
        """Return the whole collection; a collection never written reads as its empty value."""
        ...

    def replace(self, collection: Collection, value: Any) -> None:
        """Replace the whole collection with *value*."""
        ...

    def get_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        """Return the Candidate History record with this Candidate ID, or ``None``."""
        ...

    def upsert_candidate(self, record: Mapping[str, Any]) -> None:
        """Insert or replace the Candidate History record with the record's ``candidate_id``."""
        ...

    def delete_candidate(self, candidate_id: str) -> bool:
        """Remove the Candidate History record with this Candidate ID; return whether one existed."""
        ...

    def transaction(self) -> AbstractContextManager[None]:
        """Stage every write inside the block and apply them together, or none of them."""
        ...

    def describe(self) -> dict[str, str]:
        """The active backend and how it was chosen, for the doctor and the workflow log.

        The SQLite backend adds its database, the database path's class, the
        schema version, its change marker and that of the last export into the
        state directory, and ``export_current`` (yes, no, never or no_database).
        """
        ...


# What an inspection found about the export of a SQLite database into its state directory.
EXPORT_NOT_APPLICABLE = "not_applicable"  # the JSON backend keeps the JSON itself
EXPORT_NO_DATABASE = "no_database"  # no database yet and no JSON state: nothing to compare
EXPORT_CURRENT = "current"  # every exported file is what the database would export
EXPORT_BEHIND = "behind"  # the database changed since the last export; the files are as that export left them
EXPORT_CHANGED_OUTSIDE = "changed_outside"  # the files were changed by something other than the export
EXPORT_NEVER = "never_exported"  # no export is recorded and the files differ from the database
EXPORT_UNREADABLE = "unreadable"  # the comparison could not be made (see ``problem``)
EXPORT_NOT_CHECKED = "not_checked"  # no comparison was asked for (the doctor skips it while a run writes)
CONSISTENT_EXPORT_STATUSES = frozenset({EXPORT_NOT_APPLICABLE, EXPORT_NO_DATABASE, EXPORT_CURRENT})

JSON_BACKEND = "json"
SQLITE_BACKEND = "sqlite"
STATE_BACKENDS = (JSON_BACKEND, SQLITE_BACKEND)
# How a selection was made when the state directory's backend marker made it.
STATE_DIRECTORY_SELECTION = "state_directory"
ROLLBACK_COMMAND = "./scripts/state-database rollback"
STATE_BACKEND_CONFIG_KEY = "state_backend"
STATE_BACKEND_ENV = "AUTOSCOUT_STATE_BACKEND"
STATE_DATABASE_CONFIG_KEY = "state_database"
STATE_DATABASE_ENV = "AUTOSCOUT_STATE_DATABASE"


@dataclass(frozen=True)
class BackendSelection:
    """Which backend serves canonical state and what chose it: ``environment``, ``config`` or ``default``.

    ``database`` is the SQLite database file the config or the environment
    named, ``None`` for the state directory's default file.
    """

    backend: str
    selected_by: str
    database: Path | None = None

    def describe(self) -> dict[str, str]:
        return {"backend": self.backend, "selected_by": self.selected_by}


@dataclass(frozen=True)
class StateInspection:
    """What a read-only look at a state directory's store found (#1076).

    ``export_status`` is one of the ``EXPORT_*`` values; ``consistent`` says
    the database and the exported files agree (always, on the JSON backend).
    ``problem`` is empty, or why the comparison could not be made:
    ``backend_unknown``, ``backend_conflict`` (the state directory's marker
    says SQLite but the config or environment selects JSON),
    ``backend_marker_unreadable``, ``database_location`` (a synced, network
    or committable path), ``database_missing`` (JSON state, or a marker that
    says SQLite, without a database on this machine),
    ``database_unusable``, ``schema_version_mismatch``,
    ``other_state_directory`` or ``state_unreadable``.
    ``differing`` names up to ``MAX_DIFFERING_NAMES`` exported files that
    differ, relative to the state directory; ``differing_count`` is how many.
    """

    backend: str
    selected_by: str = ""
    database: str = ""
    database_path_class: str = ""
    schema_version: int | None = None
    change_seq: int | None = None
    export_change_seq: int | None = None
    export_status: str = EXPORT_NOT_APPLICABLE
    problem: str = ""
    differing: tuple[str, ...] = ()
    differing_count: int = 0

    @property
    def consistent(self) -> bool:
        return not self.problem and self.export_status in CONSISTENT_EXPORT_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "selected_by": self.selected_by,
            "database": self.database,
            "database_path_class": self.database_path_class,
            "schema_version": self.schema_version,
            "change_seq": self.change_seq,
            "export_change_seq": self.export_change_seq,
            "export_status": self.export_status,
            "problem": self.problem,
            "differing": list(self.differing),
            "differing_count": self.differing_count,
        }


MAX_DIFFERING_NAMES = 20


def select_state_backend(
    config: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> BackendSelection:
    """Choose the backend: the environment override, else the weekly config, else ``json``.

    An unknown backend name is refused rather than replaced by the default,
    so a misconfigured run never writes state where the operator did not
    expect it.
    """
    environ = os.environ if environ is None else environ
    override = environ.get(STATE_BACKEND_ENV, "").strip()
    if override:
        selection = BackendSelection(override, "environment")
    elif config is not None and config.get(STATE_BACKEND_CONFIG_KEY):
        selection = BackendSelection(str(config[STATE_BACKEND_CONFIG_KEY]).strip(), "config")
    else:
        selection = BackendSelection(JSON_BACKEND, "default")
    if selection.backend not in STATE_BACKENDS:
        raise StateStoreError(
            f"unknown state backend {selection.backend!r} from {selection.selected_by}; "
            f"supported: {', '.join(STATE_BACKENDS)}"
        )
    database = environ.get(STATE_DATABASE_ENV, "").strip()
    if not database and config is not None and isinstance(config.get(STATE_DATABASE_CONFIG_KEY), str):
        database = config[STATE_DATABASE_CONFIG_KEY].strip()
    if database:
        from autoscout.state_store.sqlite_location import configured_database_path

        selection = BackendSelection(selection.backend, selection.selected_by, configured_database_path(database))
    return selection


def select_state_backend_from_file(config_path: Path | None) -> BackendSelection:
    """Choose the backend from a weekly config file; an unreadable or absent config counts as no config."""
    config: Any = None
    if config_path is not None:
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = None
    return select_state_backend(config if isinstance(config, Mapping) else None)


def open_configured_state_store(state_dir: Path | None, config: Mapping[str, Any] | Path | None) -> StateStore | None:
    """Open the store a run uses over *state_dir*; ``None`` without state.

    The backend is chosen from the weekly *config*, given as its value or as
    the path of its file.
    """
    if state_dir is None:
        return None
    if isinstance(config, Path):
        return open_state_store(state_dir, select_state_backend_from_file(config))
    return open_state_store(state_dir, select_state_backend(config))


_SELECTION_SOURCES = {
    "environment": STATE_BACKEND_ENV,
    "config": f"the weekly config's {STATE_BACKEND_CONFIG_KEY}",
}


def resolve_state_backend(state_dir: Path, selection: BackendSelection | None = None) -> BackendSelection:
    """The backend that serves *state_dir*: its marker's, else *selection* (the environment's, the config's or the default).

    On a marked state directory a default selection takes the marker's
    backend, a selection of the same backend is kept as it is, and any other
    selection is refused with the two ways out: drop that selection, or roll
    the directory back with ``./scripts/state-database rollback``.
    """
    from autoscout.state_store.marker import BACKEND_MARKER_FILE
    from autoscout.state_store.marker import marked_backend

    selection = select_state_backend() if selection is None else selection
    marked = marked_backend(state_dir)
    if marked is None or selection.backend == marked:
        return selection
    if selection.selected_by == "default":
        return BackendSelection(marked, STATE_DIRECTORY_SELECTION, selection.database)
    source = _SELECTION_SOURCES.get(selection.selected_by, f"the caller ({selection.selected_by})")
    raise StateStoreError(
        f"the state directory {state_dir} is kept in {marked} (its {BACKEND_MARKER_FILE} says so), but {source} "
        f"selects {selection.backend}; drop that selection, or move the state directory back to "
        f"{selection.backend} with {ROLLBACK_COMMAND}"
    )


def open_state_store(state_dir: Path, selection: BackendSelection | None = None) -> StateStore:
    """Open the State Store over *state_dir* with the backend that serves it (``resolve_state_backend``)."""
    from autoscout.state_store.json_backend import JsonStateStore

    selection = resolve_state_backend(state_dir, selection)
    if selection.backend == SQLITE_BACKEND:
        from autoscout.state_store.sqlite_backend import SqliteStateStore

        return SqliteStateStore(state_dir, selection, state_database_path(state_dir, selection))
    if selection.backend != JSON_BACKEND:
        raise StateStoreError(f"unknown state backend {selection.backend!r}")
    return JsonStateStore(state_dir, selection)


def open_run_output_store(output_dir: Path, selection: BackendSelection | None = None) -> StateStore:
    """The store over a run's output directory, for a run without a state directory (#1075).

    Such a run keeps its collections as the JSON files beside its report (its
    copy of Candidate History, a visibility file a test or a fixture wrote), so
    the store is the JSON backend whatever *selection* (else the environment's)
    names: a selection chooses the backend of state directories, and an
    output directory has no database. An output directory cut over to SQLite
    (its marker) is a state directory and opens as one, with *selection*, so
    its configured database is used and a conflicting selection is refused.
    """
    from autoscout.state_store.marker import marked_backend

    selection = select_state_backend() if selection is None else selection
    if marked_backend(output_dir) is not None:
        return open_state_store(output_dir, selection)
    return open_state_store(output_dir, BackendSelection(JSON_BACKEND, "default", selection.database))


def state_database_path(state_dir: Path, selection: BackendSelection | None = None) -> Path:
    """The SQLite database file of *state_dir*: the configured one, else the state directory's default."""
    from autoscout.state_store.sqlite_location import default_database_path

    selection = select_state_backend() if selection is None else selection
    return selection.database if selection.database is not None else default_database_path(state_dir)


def keeps_json_in_state_dir(store: StateStore) -> bool:
    """Whether *store* keeps canonical state as the JSON files of its state directory (the JSON backend).

    A store that does not writes those files only when it exports, so a run
    must not write a canonical file beside its report itself (#1074).
    """
    from autoscout.state_store.json_backend import JsonStateStore

    return isinstance(store, JsonStateStore)


def inspect_state_store(
    state_dir: Path, selection: BackendSelection | None = None, *, compare: bool = True
) -> StateInspection:
    """Look at the store of *state_dir* without changing anything (the doctor's and run-health's view).

    The backend is the one that serves the directory: its marker's (#1093),
    else *selection*. A selection that conflicts with the marker, or a marker
    that cannot be read, is reported as a problem rather than raised. On the
    JSON backend there is nothing to compare. On SQLite it reports the
    database's path class and schema version and, unless *compare* is false,
    compares every exported file with what the database would export. It
    opens no transaction, creates no database, WAL file or directory, and
    never migrates a database.
    """
    from autoscout.state_store.marker import marked_backend
    from autoscout.state_store.sqlite_backend import SqliteStateStore
    from autoscout.state_store.sqlite_location import database_path_class
    from autoscout.state_store.sqlite_location import display_path

    selection = select_state_backend() if selection is None else selection
    try:
        selection = resolve_state_backend(state_dir, selection)
    except StateStoreError:
        try:
            marked = marked_backend(state_dir)
        except StateStoreError:
            return StateInspection(
                backend=selection.backend,
                selected_by=selection.selected_by,
                export_status=EXPORT_UNREADABLE,
                problem="backend_marker_unreadable",
            )
        return StateInspection(
            backend=marked or selection.backend,
            selected_by=STATE_DIRECTORY_SELECTION,
            export_status=EXPORT_UNREADABLE,
            problem="backend_conflict",
        )
    if selection.backend != SQLITE_BACKEND:
        return StateInspection(backend=selection.backend, selected_by=selection.selected_by)
    if not compare:
        return StateInspection(
            backend=selection.backend, selected_by=selection.selected_by, export_status=EXPORT_NOT_CHECKED
        )
    database = state_database_path(state_dir, selection)
    try:
        store = SqliteStateStore(state_dir, selection, database)
    except StateStoreError:
        return StateInspection(
            backend=selection.backend,
            selected_by=selection.selected_by,
            database=display_path(database),
            database_path_class=database_path_class(database),
            export_status=EXPORT_UNREADABLE,
            problem="database_location",
        )
    return store.inspect()


def inspect_store(store: StateStore | None) -> StateInspection | None:
    """``inspect_state_store`` for a store already open; ``None`` for the JSON backend or no store."""
    from autoscout.state_store.sqlite_backend import SqliteStateStore

    return store.inspect() if isinstance(store, SqliteStateStore) else None


def exports_into(store: StateStore | None, directory: Path) -> bool:
    """Whether *directory* is where *store*, of a backend other than JSON, writes its JSON export."""
    from autoscout.state_store.sqlite_backend import SqliteStateStore

    if not isinstance(store, SqliteStateStore):
        return False
    try:
        return directory.resolve() == store.state_dir.resolve()
    except OSError:
        return False


def export_store(store: StateStore, *, force: bool = False) -> Any:
    """Export *store* into its state directory as JSON (``export_json``); ``None`` for the JSON backend."""
    from autoscout.state_store.sqlite_backend import SqliteStateStore

    return store.export_json(force=force) if isinstance(store, SqliteStateStore) else None


def export_json(state_dir: Path, selection: BackendSelection | None = None, *, force: bool = False) -> Any:
    """Write the SQLite database of *state_dir* into it as JSON, before the Git envelope commits it.

    The JSON is what the JSON backend would keep for the same state, in the
    layout the state directory already has, and only files whose bytes change
    are written. Returns the export's report, or ``None`` on the JSON
    backend, where the state directory already is the JSON and nothing is
    done. A state directory changed outside the database since its last
    export is refused unless *force* is given.
    """
    selection = resolve_state_backend(state_dir, selection)
    if selection.backend != SQLITE_BACKEND:
        return None
    return export_store(open_state_store(state_dir, selection), force=force)


@contextmanager
def replace_and_export(store: StateStore, collection: Collection, value: Any) -> Iterator[None]:
    """Replace *collection* so the state directory's JSON holds it when the block starts; undo the store's part if it raises.

    For a caller that commits the collection's file itself (source
    onboarding, #1093). On the JSON backend the file is the store, written at
    once; restoring it undoes the write. Under SQLite the replacement and its
    export into the state directory happen in one database transaction that
    stays open, holding the write lock, until the block ends: an exception in
    the block rolls the database and its export record back, and the caller
    restores the files it snapshotted, so the two agree again. A state
    directory whose export is not current is refused before anything is
    written, so the export changes only *collection*'s file.
    """
    from autoscout.state_store.sqlite_backend import SqliteStateStore

    if isinstance(store, SqliteStateStore):
        with store.replacing_and_exporting(collection, value):
            yield
        return
    store.replace(collection, value)
    yield


def _sqlite_selection(selection: BackendSelection | None) -> BackendSelection:
    selection = select_state_backend() if selection is None else selection
    return BackendSelection(SQLITE_BACKEND, selection.selected_by, selection.database)


def cut_over_to_sqlite(state_dir: Path, selection: BackendSelection | None = None) -> Any:
    """Make SQLite serve *state_dir* for every command: export its database into it, then write the marker.

    The database must exist (``rebuild_from_json`` builds it) and the export
    must succeed, so the committed JSON equals the database at cutover.
    Returns the export's report. A directory already cut over is exported
    again and keeps its marker.
    """
    from autoscout.state_store.marker import write_marker
    from autoscout.state_store.sqlite_backend import REBUILD_COMMAND
    from autoscout.state_store.sqlite_location import display_path

    selection = _sqlite_selection(selection)
    database = state_database_path(state_dir, selection)
    if not database.exists():
        raise StateStoreError(
            f"the state database {display_path(database)} does not exist; build it from the JSON with "
            f"{REBUILD_COMMAND} first"
        )
    report = export_store(open_state_store(state_dir, selection))
    write_marker(state_dir, SQLITE_BACKEND)
    return report


def roll_back_to_json(state_dir: Path, selection: BackendSelection | None = None, *, force: bool = False) -> Any:
    """Make the JSON backend serve *state_dir* again: export its database into it, then remove the marker.

    Returns the export's report, or ``None`` when the directory was not cut
    over (nothing is done). The export refuses a directory changed outside
    the database unless *force* is given, as ``export_json`` does; a
    database that does not exist is refused, because the JSON could not be
    brought up to date. The database file is kept; it is stale from the next
    JSON write on, and a later cutover rebuilds it with ``--overwrite``.
    """
    from autoscout.state_store.marker import marked_backend
    from autoscout.state_store.marker import remove_marker

    if marked_backend(state_dir) is None:
        return None
    report = export_store(open_state_store(state_dir, _sqlite_selection(selection)), force=force)
    remove_marker(state_dir)
    return report


def rebuild_from_json(
    state_dir: Path,
    overwrite: bool = False,
    selection: BackendSelection | None = None,
    *,
    require_canonical: bool = False,
) -> Any:
    """Build the SQLite database of *state_dir* from its JSON files, in either layout.

    Every collection is read and validated by the JSON backend, and every
    record by the SQLite backend, before the database file is created; an
    invalid record creates nothing. An existing database is refused unless
    *overwrite* is given. With *require_canonical*, a state file whose bytes
    are not what the JSON backend writes is refused too, because the first
    export would rewrite it. The database is built whatever backend is
    active, so a rebuild can be rehearsed before cutover.
    """
    from autoscout.state_store.sqlite_backend import rebuild_database

    selection = select_state_backend() if selection is None else selection
    return rebuild_database(
        state_dir,
        state_database_path(state_dir, selection),
        selection,
        overwrite=overwrite,
        require_canonical=require_canonical,
    )
