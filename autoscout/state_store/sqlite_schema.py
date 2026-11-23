"""The SQLite backend's schema and its ordered forward migrations (#1074).

``MIGRATIONS[n - 1]`` takes a database from schema version ``n - 1`` to
``n``; version 0 is an empty file. Each applied version is a row of
``schema_version``. A migration is never edited once released: a change of
schema is a new migration appended to the list, and a database written by a
newer AutoScout than this one is refused rather than read.

Version 1:

- ``store_meta``: the change counter and the state directory the database
  belongs to.
- ``collection_state``: per collection, whether it has been stored (the JSON
  backend keeps a file for it) and the change marker of its last change.
- ``candidate_history``: one row per record. ``record_id`` is the row's own
  key, because a test or a legacy single file may hold a record without a
  ``candidate_id`` or two records with one; ``candidate_id``, ``identity_key``,
  ``title``, ``author_display``, ``editor_status`` and the dates are query
  columns copied from the record, and ``record_json`` is the record without
  its decision log. ``change_seq`` is the change marker: the counter value of
  the write that last changed the record, so an export or a derived index
  refreshes only the records changed since it last ran.
- ``editor_decisions``: the decision log (#1053), one row per entry, in order
  (``position``); the only copy of the log in the database. The view
  ``decision_log`` names each entry's Candidate ID.
- One table per other collection, ordered by ``position``, with its stable
  key as an indexed column and the record as JSON text. The identity alias
  table is kept as its aliases, its candidates and one row for the rest.
- ``json_exports``: per state directory, the change marker, layout and
  digest of the last export, so the next one rewrites only what changed and
  notices a state directory changed outside the database.

Version 2 (#1125):

- ``source_failure_streaks``: per source, the attempts in a row that failed,
  keyed by the source name; a list table like those of version 1.

A database at an older version is migrated by the first store that opens it
for a read or a write. Until then the read-only inspection of the doctor and
run-health reads it as it is: a collection whose table a later migration adds
reads as empty and not stored, so the export of a database built before
version 2 is still current (``list_table_version``).
"""

from __future__ import annotations

_VERSION_1_LIST_TABLES: dict[str, str | None] = {
    # table name: the indexed key column copied from each record
    "excluded_candidate_history": "identity_key",
    "candidate_visibility": "identity_key",
    "pending_editor_rows": "identity_key",
    "archived_editor_rows": "identity_key",
    "editorial_profile_entries": "id",
    "source_proposals": "id",
    "ranking_freshness": "state_key",
    "latest_scouting_candidates": "identity_key",
}


def _list_table(name: str, key: str | None) -> str:
    key_column = "\n    record_key TEXT," if key else ""
    index = f"\nCREATE INDEX {name}_record_key ON {name} (record_key);" if key else ""
    return f"""
CREATE TABLE {name} (
    position INTEGER PRIMARY KEY,{key_column}
    record_json TEXT NOT NULL
);{index}
"""


_VERSION_1 = """
CREATE TABLE store_meta (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO store_meta (name, value) VALUES ('change_seq', '0');

CREATE TABLE collection_state (
    collection TEXT PRIMARY KEY,
    stored INTEGER NOT NULL CHECK (stored IN (0, 1)),
    change_seq INTEGER NOT NULL
);

CREATE TABLE candidate_history (
    record_id INTEGER PRIMARY KEY,
    candidate_id TEXT,
    identity_key TEXT,
    title TEXT,
    author_display TEXT,
    editor_status TEXT,
    first_seen_date TEXT,
    last_seen_date TEXT,
    last_material_change_date TEXT,
    has_decision_log INTEGER NOT NULL CHECK (has_decision_log IN (0, 1)),
    record_json TEXT NOT NULL,
    change_seq INTEGER NOT NULL
);
CREATE INDEX candidate_history_candidate_id ON candidate_history (candidate_id);
CREATE INDEX candidate_history_identity_key ON candidate_history (identity_key);
CREATE INDEX candidate_history_title ON candidate_history (title);
CREATE INDEX candidate_history_editor_status ON candidate_history (editor_status);
CREATE INDEX candidate_history_last_seen_date ON candidate_history (last_seen_date);
CREATE INDEX candidate_history_change_seq ON candidate_history (change_seq);

CREATE TABLE editor_decisions (
    record_id INTEGER NOT NULL REFERENCES candidate_history (record_id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    status TEXT,
    note TEXT,
    decision_date TEXT,
    description_digest TEXT,
    PRIMARY KEY (record_id, position)
);
CREATE INDEX editor_decisions_decision_date ON editor_decisions (decision_date);

CREATE VIEW decision_log AS
SELECT history.candidate_id, decisions.position, decisions.status, decisions.note,
       decisions.decision_date, decisions.description_digest
FROM editor_decisions AS decisions
JOIN candidate_history AS history ON history.record_id = decisions.record_id;

CREATE TABLE candidate_identity_aliases (
    alias TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL
);
CREATE INDEX candidate_identity_aliases_candidate_id ON candidate_identity_aliases (candidate_id);

CREATE TABLE candidate_identity_candidates (
    candidate_id TEXT PRIMARY KEY,
    metadata_json TEXT NOT NULL
);

CREATE TABLE candidate_identity_alias_table (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL,
    redirects_json TEXT,
    durable_merges_json TEXT
);

CREATE TABLE json_exports (
    state_dir TEXT PRIMARY KEY,
    layout TEXT NOT NULL,
    change_seq INTEGER NOT NULL,
    tree_digest TEXT NOT NULL
);
""" + "".join(_list_table(name, key) for name, key in _VERSION_1_LIST_TABLES.items())

_VERSION_2_LIST_TABLES: dict[str, str | None] = {
    "source_failure_streaks": "source_name",
}
_VERSION_2 = "".join(_list_table(name, key) for name, key in _VERSION_2_LIST_TABLES.items())

MIGRATIONS: tuple[str, ...] = (_VERSION_1, _VERSION_2)
SCHEMA_VERSION = len(MIGRATIONS)

# Every list table with its indexed key column, whichever migration added it.
LIST_TABLES: dict[str, str | None] = {**_VERSION_1_LIST_TABLES, **_VERSION_2_LIST_TABLES}
# The schema version that added each list table after version 1.
_LIST_TABLE_VERSIONS: dict[str, int] = {name: 2 for name in _VERSION_2_LIST_TABLES}


def list_table_version(name: str) -> int:
    """The schema version whose migration created the list table *name*."""
    return _LIST_TABLE_VERSIONS.get(name, 1)
