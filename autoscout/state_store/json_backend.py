"""The JSON backend of the State Store: key-sorted JSON files in the state directory.

Every collection but Candidate History is one file, with exactly the bytes
the state module wrote before the State Store existed (#1068): each list
collection keeps its historical record order, the Editorial Taste Profile and
the identity alias table are validated, and a missing file reads as the
collection's empty value.

Candidate History is kept in the layout the state directory already has
(``autoscout.state_store.layout``, #1073): the single file, written as
before, or one file per book. In the sharded layout a record's file is
written only when its bytes change, a removed record's file is deleted, and
``get_candidate``, ``upsert_candidate`` and ``delete_candidate`` touch one
file without loading the collection.

A transaction serializes every staged collection and every staged record
while the block runs and publishes them as one bundle when it exits: every
file is staged first, then each is moved over its destination atomically and
each removed record's file is deleted, and a failure on the way restores the
files already replaced and the files already deleted.

A state directory cut over to SQLite (its backend marker, #1093) is never
written by this backend except as the SQLite backend's exporter: any other
write is refused before a file changes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from autoscout.candidate_identity import load_alias_table
from autoscout.candidate_identity import validate_alias_table
from autoscout.editorial_taste_profile import TasteProfileError
from autoscout.editorial_taste_profile import empty_profile
from autoscout.editorial_taste_profile import validate_taste_profile
from autoscout.json_io import read_json_or_default
from autoscout.json_io import serialize_json
from autoscout.safe_artifacts import publish_bundle
from autoscout.safe_artifacts import publish_single
from autoscout.sort_utils import sort_by_key
from autoscout.state_store import ROLLBACK_COMMAND
from autoscout.state_store import BackendSelection
from autoscout.state_store import Collection
from autoscout.state_store import StateStoreError
from autoscout.state_store.layout import CANDIDATE_HISTORY_DIR
from autoscout.state_store.layout import CANDIDATE_HISTORY_FILE
from autoscout.state_store.layout import HistoryLayout
from autoscout.state_store.layout import checked_candidate_id
from autoscout.state_store.layout import history_layout
from autoscout.state_store.layout import iter_shard_files
from autoscout.state_store.layout import parse_shard
from autoscout.state_store.layout import serialize_record
from autoscout.state_store.layout import shard_contents
from autoscout.state_store.layout import shard_relative_path
from autoscout.state_store.layout import stored_history_bytes
from autoscout.state_store.layout import valid_candidate_id
from autoscout.state_store.marker import BACKEND_MARKER_FILE
from autoscout.state_store.marker import marked_backend

FILE_NAMES: dict[Collection, str] = {
    # In the single-file layout; the sharded layout keeps it under CANDIDATE_HISTORY_DIR.
    Collection.CANDIDATE_HISTORY: CANDIDATE_HISTORY_FILE,
    Collection.EXCLUDED_CANDIDATE_HISTORY: "excluded-candidate-history.json",
    Collection.CANDIDATE_VISIBILITY: "candidate-visibility.json",
    Collection.PENDING_EDITOR_ROWS: "pending-returned-editor-rows.json",
    Collection.ARCHIVED_EDITOR_ROWS: "archived-returned-editor-rows.json",
    Collection.EDITORIAL_PROFILE: "editorial-profile.json",
    Collection.CANDIDATE_IDENTITY_ALIASES: "candidate-identity-aliases.json",
    Collection.SOURCE_PROPOSALS: "source-proposals.json",
    Collection.RANKING_FRESHNESS: "ranking-freshness.json",
    Collection.LATEST_SCOUTING_CANDIDATES: "latest-scouting-candidates.json",
    Collection.SOURCE_FAILURE_STREAKS: "source-failure-streaks.json",
}

# The name the Editorial Taste Profile was kept under before it was renamed.
# It is read while the current file does not exist, and never written.
LEGACY_PROFILE_FILE_NAME = "editorial-taste-profile.json"

# The record key each list collection has always been sorted by. Pending
# editor rows keep the order they were staged in, and the ranking-freshness
# ledger keeps the order the ledger gives.
_SORT_KEYS: dict[Collection, str | None] = {
    Collection.CANDIDATE_HISTORY: "identity_key",
    Collection.EXCLUDED_CANDIDATE_HISTORY: "identity_key",
    Collection.CANDIDATE_VISIBILITY: "identity_key",
    Collection.PENDING_EDITOR_ROWS: None,
    Collection.ARCHIVED_EDITOR_ROWS: "identity_key",
    Collection.SOURCE_PROPOSALS: "id",
    Collection.RANKING_FRESHNESS: None,
    Collection.LATEST_SCOUTING_CANDIDATES: "identity_key",
    Collection.SOURCE_FAILURE_STREAKS: "source_name",
}


def _canonical_profile(value: Any) -> dict[str, Any]:
    validate_taste_profile(value)
    return {"entries": sorted(value["entries"], key=lambda entry: str(entry["id"]))}


_CANONICAL_DOCUMENTS: dict[Collection, Callable[[Any], Any]] = {
    Collection.EDITORIAL_PROFILE: _canonical_profile,
    Collection.CANDIDATE_IDENTITY_ALIASES: validate_alias_table,
}


def canonical_value(collection: Collection, value: Any) -> Any:
    """The value as the JSON backend stores it: validated, and each list collection in its record order."""
    canonical_document = _CANONICAL_DOCUMENTS.get(collection)
    if canonical_document is not None:
        return canonical_document(value)
    if not isinstance(value, list):
        raise StateStoreError(f"{collection.value} must be a list of records, got {type(value).__name__}")
    sort_key = _SORT_KEYS[collection]
    return list(value) if sort_key is None else sort_by_key(value, sort_key)


def canonical_bytes(collection: Collection, value: Any) -> str:
    """The bytes of the file the JSON backend writes for a whole collection (Candidate History in the single-file layout)."""
    return serialize_json(canonical_value(collection, value))


def collection_path(state_dir: Path, collection: Collection) -> Path:
    """The file a collection is kept in; Candidate History's in the single-file layout."""
    return state_dir / FILE_NAMES[collection]


def holds_json_state(state_dir: Path) -> bool:
    """Whether *state_dir* holds any canonical collection as JSON, in either layout."""
    names = [*FILE_NAMES.values(), LEGACY_PROFILE_FILE_NAME, CANDIDATE_HISTORY_DIR]
    return any((state_dir / name).exists() or (state_dir / name).is_symlink() for name in names)


def stored_state_digest(state_dir: Path) -> str:
    """SHA-256 over the canonical state of *state_dir*, the same whichever layout Candidate History has.

    It covers each collection file's name and bytes, and Candidate History in
    its single-file form (``stored_history_bytes``), so a conversion between
    the layouts leaves it unchanged while any change of a record or a file
    changes it. Files that are not canonical state do not count.
    """
    digest = hashlib.sha256()
    for name in sorted({*FILE_NAMES.values(), LEGACY_PROFILE_FILE_NAME} - {CANDIDATE_HISTORY_FILE}):
        path = state_dir / name
        if path.is_file():
            digest.update(name.encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    history = stored_history_bytes(state_dir)
    digest.update(b"candidate_history\0" + (b"absent" if history is None else b"present\0" + history))
    return digest.hexdigest()


def _checked_candidate_id(candidate_id: Any) -> str:
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise StateStoreError("a Candidate History record needs a nonblank candidate_id")
    return candidate_id


class JsonStateStore:
    """The State Store over a state directory of JSON files."""

    def __init__(self, state_dir: Path, selection: BackendSelection, *, exporting: bool = False) -> None:
        self._state_dir = state_dir
        self._selection = selection
        # The SQLite backend's exporter, the one writer a directory cut over to SQLite accepts.
        self._exporting = exporting
        self._staged: dict[Collection, str] | None = None
        # Inside a transaction over a sharded Candidate History: each staged
        # record's bytes by Candidate ID, ``None`` for a record removed.
        self._staged_shards: dict[str, str | None] | None = None
        self._transaction_layout: HistoryLayout | None = None
        # Whether the transaction wrote Candidate History, even with no record
        # to change: a write creates the state directory, as the single file does.
        self._history_written = False

    def describe(self) -> dict[str, str]:
        return self._selection.describe()

    def read(self, collection: Collection) -> Any:
        if collection is Collection.CANDIDATE_HISTORY and self._layout() is HistoryLayout.SHARDED:
            return self._read_sharded_history()
        if self._staged is not None and collection in self._staged:
            return json.loads(self._staged[collection])
        if collection is Collection.EDITORIAL_PROFILE:
            return self._read_profile()
        if collection is Collection.CANDIDATE_IDENTITY_ALIASES:
            return load_alias_table(self._path(collection))
        return read_json_or_default(self._path(collection), [])

    def replace(self, collection: Collection, value: Any) -> None:
        if collection is Collection.CANDIDATE_HISTORY and self._layout() is HistoryLayout.SHARDED:
            records = shard_contents(value)
            with self._writing() as shards:
                current = {*self._stored_candidate_ids(), *shards}
                shards.clear()
                shards.update(records)
                shards.update({candidate_id: None for candidate_id in current - records.keys()})
            return
        serialized = canonical_bytes(collection, value)
        if self._staged is not None:
            self._staged[collection] = serialized
        else:
            self._publish({collection: serialized}, {})

    def get_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        _checked_candidate_id(candidate_id)
        if self._layout() is HistoryLayout.SHARDED:
            if not valid_candidate_id(candidate_id):
                return None
            if self._staged_shards is not None and candidate_id in self._staged_shards:
                staged = self._staged_shards[candidate_id]
                return None if staged is None else json.loads(staged)
            return self._read_shard(candidate_id)
        for record in self._candidate_history():
            if isinstance(record, dict) and record.get("candidate_id") == candidate_id:
                return record
        return None

    def upsert_candidate(self, record: Mapping[str, Any]) -> None:
        if self._layout() is HistoryLayout.SHARDED:
            candidate_id = checked_candidate_id(record.get("candidate_id"))
            with self._writing() as shards:
                shards[candidate_id] = serialize_record(dict(record))
            return
        candidate_id = _checked_candidate_id(record.get("candidate_id"))
        history = [
            existing
            for existing in self._candidate_history()
            if not (isinstance(existing, dict) and existing.get("candidate_id") == candidate_id)
        ]
        history.append(dict(record))
        self.replace(Collection.CANDIDATE_HISTORY, history)

    def delete_candidate(self, candidate_id: str) -> bool:
        _checked_candidate_id(candidate_id)
        if self._layout() is HistoryLayout.SHARDED:
            if self.get_candidate(candidate_id) is None:
                return False
            with self._writing() as shards:
                shards[candidate_id] = None
            return True
        history = self._candidate_history()
        kept = [
            record
            for record in history
            if not (isinstance(record, dict) and record.get("candidate_id") == candidate_id)
        ]
        if len(kept) == len(history):
            return False
        self.replace(Collection.CANDIDATE_HISTORY, kept)
        return True

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._staged is not None:
            raise StateStoreError("State Store transactions do not nest")
        staged: dict[Collection, str] = {}
        shards: dict[str, str | None] = {}
        self._staged = staged
        self._staged_shards = shards
        self._history_written = False
        try:
            yield
        finally:
            self._staged = None
            self._staged_shards = None
            self._transaction_layout = None
        if staged or shards or self._history_written:
            self._publish(staged, shards)

    # ── Candidate History layout ──────────────────────────────────────────

    def _layout(self) -> HistoryLayout:
        """The layout of Candidate History, fixed for the length of a transaction."""
        if self._staged is None:
            return history_layout(self._state_dir)
        if self._transaction_layout is None:
            self._transaction_layout = history_layout(self._state_dir)
        return self._transaction_layout

    @contextmanager
    def _writing(self) -> Iterator[dict[str, str | None]]:
        """The staged records to change: the transaction's, or a one-write transaction of their own."""
        if self._staged_shards is not None:
            self._history_written = True
            yield self._staged_shards
            return
        with self.transaction():
            assert self._staged_shards is not None
            yield self._staged_shards

    def _shard_path(self, candidate_id: str) -> Path:
        return self._state_dir.joinpath(*shard_relative_path(candidate_id).parts)

    def _unlinked_shard_path(self, candidate_id: str) -> Path:
        """The record's file; a symlink at it or at its directories is refused, so no write or read leaves the state directory."""
        path = self._shard_path(candidate_id)
        for link in (path, path.parent, path.parent.parent):
            if link.is_symlink():
                raise StateStoreError(
                    f"Candidate History must not be a symlink: {link.relative_to(self._state_dir).as_posix()}"
                )
        return path

    def _read_shard(self, candidate_id: str) -> dict[str, Any] | None:
        path = self._unlinked_shard_path(candidate_id)
        if not path.is_file():
            return None
        return parse_shard(path.read_bytes(), path.relative_to(self._state_dir).as_posix(), candidate_id)

    def _stored_candidate_ids(self) -> set[str]:
        return {candidate_id for candidate_id, _path in iter_shard_files(self._state_dir)}

    def _read_sharded_history(self) -> list[dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {
            candidate_id: parse_shard(path.read_bytes(), path.relative_to(self._state_dir).as_posix(), candidate_id)
            for candidate_id, path in iter_shard_files(self._state_dir)
        }
        for candidate_id, staged in (self._staged_shards or {}).items():
            if staged is None:
                records.pop(candidate_id, None)
            else:
                records[candidate_id] = json.loads(staged)
        return sort_by_key(list(records.values()), "identity_key")

    def _candidate_history(self) -> list[Any]:
        history = self.read(Collection.CANDIDATE_HISTORY)
        if not isinstance(history, list):
            raise StateStoreError("candidate_history must be a list of records")
        return history

    def _read_profile(self) -> dict[str, Any]:
        """The profile, from the legacy file name while the current one does not exist."""
        path = self._path(Collection.EDITORIAL_PROFILE)
        legacy_path = self._state_dir / LEGACY_PROFILE_FILE_NAME
        if not path.exists() and legacy_path.exists():
            path = legacy_path
        try:
            profile: dict[str, Any] = read_json_or_default(path, empty_profile())
        except json.JSONDecodeError:
            raise TasteProfileError("malformed editorial taste profile: file contains invalid JSON")
        validate_taste_profile(profile)
        return profile

    def _path(self, collection: Collection) -> Path:
        return self._state_dir / FILE_NAMES[collection]

    # ── Publication ───────────────────────────────────────────────────────

    def _publish(self, serialized: dict[Collection, str], shards: dict[str, str | None]) -> None:
        """Write the staged collections and records together: all of them, or none."""
        if not self._exporting and marked_backend(self._state_dir) is not None:
            raise StateStoreError(
                f"the state directory is kept in SQLite ({BACKEND_MARKER_FILE}), so the JSON backend does not "
                f"write it; open it without a JSON selection, or move it back to JSON with {ROLLBACK_COMMAND}"
            )
        paths = [self._path(collection) for collection in serialized]
        contents: list[str | bytes] = list(serialized.values())
        removals: list[Path] = []
        for candidate_id, content in sorted(shards.items()):
            path = self._unlinked_shard_path(candidate_id)
            if content is None:
                if path.is_file():
                    removals.append(path)
            elif not (path.is_file() and path.read_bytes() == content.encode("utf-8")):
                paths.append(path)
                contents.append(content)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        if not paths and not removals:
            return
        created = self._create_parents(paths)
        try:
            if len(paths) == 1 and not removals:
                publish_single(paths[0], contents[0])
            else:
                publish_bundle(paths, contents, removals=removals)
        except BaseException:
            self._remove_empty(created)
            raise
        self._remove_empty(sorted({path.parent for path in removals}, reverse=True))

    def _create_parents(self, paths: list[Path]) -> list[Path]:
        """Create the missing shard directories, deepest last; return them deepest first."""
        created: list[Path] = []
        for path in paths:
            missing = [parent for parent in (path.parent, path.parent.parent) if not parent.exists()]
            for directory in reversed(missing):
                directory.mkdir()
                created.append(directory)
        return list(reversed(created))

    def _remove_empty(self, directories: list[Path]) -> None:
        """Remove each shard directory a write emptied, and the shard root once it holds none."""
        root = self._state_dir / CANDIDATE_HISTORY_DIR
        for directory in [*directories, root]:
            if directory != root and directory.parent != root:
                continue
            try:
                directory.rmdir()
            except OSError:
                pass
