"""Where the JSON backend keeps Candidate History on disk and in Git (#1073, ADR 0023).

Candidate History is kept in one of two layouts, and the layout follows what
is in the state directory:

- **Single file** (the layout before #1073): ``candidate-history.json``, every
  record in one key-sorted JSON list. A state directory that has this file and
  no ``candidate-history/`` directory is in this layout, and the backend keeps
  reading and writing the single file exactly as before.
- **Sharded**: one key-sorted JSON file per book,
  ``candidate-history/<first two hex characters>/<candidate_id>.json``, where
  ``candidate_id`` is ``candidate-`` plus 24 lowercase hex characters. Every
  other state directory uses this layout, a new or empty one included.

A state directory that holds both is refused: the conversion command moves a
directory from the single file to the shards, all or nothing, and never runs
on its own.

This module also reads Candidate History as a Git revision holds it, in
either layout, for the modules that read earlier commits, and gives the
names the Git envelope and the protected-path checks need.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from pathlib import PurePath
from pathlib import PurePosixPath
from typing import Any

from autoscout.json_io import serialize_json
from autoscout.sort_utils import sort_by_key
from autoscout.state_store import StateStoreError

CANDIDATE_HISTORY_FILE = "candidate-history.json"
CANDIDATE_HISTORY_DIR = "candidate-history"
SHARD_SUFFIX = ".json"
CONVERSION_COMMAND = "./scripts/shard-candidate-history"

_CANDIDATE_ID = re.compile(r"candidate-([0-9a-f]{24})")


class HistoryLayout(Enum):
    SINGLE_FILE = "single_file"
    SHARDED = "sharded"


def _present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def history_layout(state_dir: Path) -> HistoryLayout:
    """The layout Candidate History has in *state_dir*; both at once is refused."""
    has_file = _present(state_dir / CANDIDATE_HISTORY_FILE)
    has_dir = _present(state_dir / CANDIDATE_HISTORY_DIR)
    if has_file and has_dir:
        raise StateStoreError(
            f"the state directory holds Candidate History twice, as {CANDIDATE_HISTORY_FILE} and as "
            f"{CANDIDATE_HISTORY_DIR}/; finish the conversion with {CONVERSION_COMMAND} --force, "
            "or remove the copy that is not current"
        )
    return HistoryLayout.SINGLE_FILE if has_file else HistoryLayout.SHARDED


def is_single_file_layout(state_dir: Path) -> bool:
    return history_layout(state_dir) is HistoryLayout.SINGLE_FILE


def valid_candidate_id(candidate_id: Any) -> bool:
    return isinstance(candidate_id, str) and _CANDIDATE_ID.fullmatch(candidate_id) is not None


def checked_candidate_id(candidate_id: Any) -> str:
    """The Candidate ID, or a refusal naming why a record cannot have a file of its own."""
    if not valid_candidate_id(candidate_id):
        raise StateStoreError(
            f"a Candidate History record needs a candidate_id of the form candidate-<24 hex characters>, "
            f"got {candidate_id!r}; the record is not written under a guessed name"
        )
    return str(candidate_id)


def shard_relative_path(candidate_id: str) -> PurePosixPath:
    """``candidate-history/<xx>/<candidate_id>.json`` for a valid Candidate ID."""
    match = _CANDIDATE_ID.fullmatch(checked_candidate_id(candidate_id))
    assert match is not None
    return PurePosixPath(CANDIDATE_HISTORY_DIR, match.group(1)[:2], f"{candidate_id}{SHARD_SUFFIX}")


def candidate_id_of_shard(relative: PurePath) -> str | None:
    """The Candidate ID a shard path names, or ``None`` when the path is not a well-formed shard."""
    parts = relative.parts
    if len(parts) != 3 or parts[0] != CANDIDATE_HISTORY_DIR or not parts[2].endswith(SHARD_SUFFIX):
        return None
    candidate_id = parts[2].removesuffix(SHARD_SUFFIX)
    match = _CANDIDATE_ID.fullmatch(candidate_id)
    if match is None or match.group(1)[:2] != parts[1]:
        return None
    return candidate_id


def is_candidate_history_path(path: str | PurePath) -> bool:
    """Whether a path names Candidate History in either layout: the single file or anything under the shard directory."""
    pure = PurePosixPath(path) if isinstance(path, str) else PurePosixPath(*PurePath(path).parts)
    return pure.name == CANDIDATE_HISTORY_FILE or CANDIDATE_HISTORY_DIR in pure.parts[:-1]


def serialize_record(record: Any) -> str:
    """A shard's bytes: the same key-sorted JSON formatting as every state file."""
    return serialize_json(record)


def shard_contents(value: Any) -> dict[str, str]:
    """Each record of a whole Candidate History, serialized and keyed by its Candidate ID.

    A value that is not a list of objects, a record without a valid
    Candidate ID and two records with the same Candidate ID are refused:
    each record needs a file of its own.
    """
    if not isinstance(value, list):
        raise StateStoreError(f"candidate_history must be a list of records, got {type(value).__name__}")
    serialized: dict[str, str] = {}
    for record in value:
        if not isinstance(record, Mapping):
            raise StateStoreError("every Candidate History record must be a JSON object")
        candidate_id = checked_candidate_id(record.get("candidate_id"))
        if candidate_id in serialized:
            raise StateStoreError(f"two Candidate History records share the candidate_id {candidate_id}")
        serialized[candidate_id] = serialize_record(dict(record))
    return serialized


def _refuse_symlink(path: Path, state_dir: Path) -> None:
    if path.is_symlink():
        raise StateStoreError(f"Candidate History must not be a symlink: {path.relative_to(state_dir).as_posix()}")


def iter_shard_files(state_dir: Path) -> Iterator[tuple[str, Path]]:
    """Every shard file as ``(candidate_id, path)`` in path order.

    A symlink anywhere under the shard directory, a stray file and a file
    whose name is not ``<candidate_id>.json`` in its right directory are
    refused: the store never follows a link out of the state directory and
    never reads a record it would not have written there.
    """
    root = state_dir / CANDIDATE_HISTORY_DIR
    if not _present(root):
        return
    _refuse_symlink(root, state_dir)
    if not root.is_dir():
        raise StateStoreError(f"{CANDIDATE_HISTORY_DIR} in the state directory must be a directory")
    for bucket in sorted(root.iterdir()):
        _refuse_symlink(bucket, state_dir)
        if not bucket.is_dir():
            raise StateStoreError(f"unexpected file in {CANDIDATE_HISTORY_DIR}/: {bucket.name}")
        for path in sorted(bucket.iterdir()):
            _refuse_symlink(path, state_dir)
            relative = PurePosixPath(*path.relative_to(state_dir).parts)
            candidate_id = candidate_id_of_shard(relative)
            if candidate_id is None or not path.is_file():
                raise StateStoreError(f"unexpected entry in {CANDIDATE_HISTORY_DIR}/: {relative.as_posix()}")
            yield candidate_id, path


def shard_files(state_dir: Path) -> list[Path]:
    """Every file under the shard directory of *state_dir*, best effort and without refusing anything.

    For scans that must see every file a commit would carry, whatever its
    name; reads of records go through ``iter_shard_files``.
    """
    root = state_dir / CANDIDATE_HISTORY_DIR
    if not root.is_dir() or root.is_symlink():
        return []
    return sorted(path for path in root.rglob("*") if path.is_file() and not path.is_symlink())


def parse_shard(raw: bytes, label: str, candidate_id: str) -> dict[str, Any]:
    """One shard's record; its ``candidate_id`` must be the one its file name gives."""
    try:
        record = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise json.JSONDecodeError(f"{label}: {exc.msg}", exc.doc, exc.pos) from exc
    if not isinstance(record, dict):
        raise StateStoreError(f"{label} must hold one JSON object")
    if record.get("candidate_id") != candidate_id:
        raise StateStoreError(f"{label} holds the record of {record.get('candidate_id')!r}, not of its file name")
    return record


def read_sharded_records(state_dir: Path) -> list[dict[str, Any]]:
    """Every record of a sharded Candidate History, in the store's order (by identity key)."""
    records = [
        parse_shard(path.read_bytes(), path.relative_to(state_dir).as_posix(), candidate_id)
        for candidate_id, path in iter_shard_files(state_dir)
    ]
    return sort_by_key(records, "identity_key")


def stored_history_bytes(state_dir: Path) -> bytes | None:
    """Candidate History of *state_dir* in its single-file form, whatever its layout; ``None`` when absent.

    Two state directories that hold the same records give the same bytes,
    one sharded and one not, so a byte comparison of run directories keeps
    meaning the same thing across the conversion.
    """
    single = state_dir / CANDIDATE_HISTORY_FILE
    if history_layout(state_dir) is HistoryLayout.SINGLE_FILE:
        return single.read_bytes() if single.is_file() else None
    if not (state_dir / CANDIDATE_HISTORY_DIR).is_dir():
        return None
    return serialize_json(read_sharded_records(state_dir)).encode("utf-8")


def candidate_history_location(state_dir: Path) -> Path:
    """The path that holds Candidate History in *state_dir*'s layout: the file or the shard directory."""
    if history_layout(state_dir) is HistoryLayout.SINGLE_FILE:
        return state_dir / CANDIDATE_HISTORY_FILE
    return state_dir / CANDIDATE_HISTORY_DIR


def stored_history_digest(state_dir: Path) -> str | None:
    """SHA-256 of Candidate History as stored, ``None`` when absent or reached through a symlink.

    The single file's digest is the digest of its bytes, as before #1073. The
    sharded layout's digest covers every shard's relative path and bytes in
    path order, so it changes when any shard is added, removed or changed.
    """
    location = candidate_history_location(state_dir)
    try:
        if not location.exists() or location.is_symlink():
            return None
        if location.is_file():
            return hashlib.sha256(location.read_bytes()).hexdigest()
        digest = hashlib.sha256()
        for _candidate_id, path in iter_shard_files(state_dir):
            digest.update(path.relative_to(state_dir).as_posix().encode("utf-8") + b"\0")
            digest.update(path.read_bytes() + b"\0")
        return digest.hexdigest()
    except (OSError, StateStoreError):
        return None


# ── Candidate History as a Git revision holds it ──────────────────────────────

GitShow = Callable[[Path, str, str], bytes | None]


def git_show(repo_root: Path, revision: str, path: str) -> bytes | None:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"{revision}:{path}"],
        capture_output=True,
        check=False,
    )
    return completed.stdout if completed.returncode == 0 else None


def _git_shard_paths(repo_root: Path, revision: str, state_dir: str) -> list[str]:
    prefix = f"{state_dir}/{CANDIDATE_HISTORY_DIR}/" if state_dir else f"{CANDIDATE_HISTORY_DIR}/"
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "ls-tree", "-r", "-z", "--name-only", revision, "--", prefix],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return []
    return sorted(path for path in completed.stdout.decode("utf-8").split("\0") if path)


def _git_blobs(repo_root: Path, revision: str, paths: list[str]) -> list[bytes]:
    """The blobs of *paths* at *revision*, in order, through one ``git cat-file --batch``."""
    request = "".join(f"{revision}:{path}\n" for path in paths).encode("utf-8")
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "--batch"],
        input=request,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise StateStoreError("git cannot read the Candidate History shards of the revision")
    output = completed.stdout
    blobs: list[bytes] = []
    offset = 0
    for path in paths:
        header_end = output.index(b"\n", offset)
        header = output[offset:header_end].split()
        if len(header) != 3 or header[1] != b"blob":
            raise StateStoreError(f"git cannot read the Candidate History shard {path}")
        size = int(header[2])
        start = header_end + 1
        blobs.append(output[start : start + size])
        offset = start + size + 1
    return blobs


def committed_history_pathspecs(state_dir: str) -> list[str]:
    """The repository paths Candidate History of *state_dir* is or was committed under, for ``git log``."""
    state = state_dir.strip("/")
    prefix = f"{state}/" if state else ""
    return [f"{prefix}{CANDIDATE_HISTORY_FILE}", f"{prefix}{CANDIDATE_HISTORY_DIR}"]


def committed_candidate_history(
    repo_root: Path,
    revision: str,
    state_dir: str,
    *,
    show: GitShow = git_show,
) -> Any:
    """Candidate History of the state directory *state_dir* (repository-relative) at *revision*.

    A revision that holds shards gives their records in the store's order;
    one that holds the single file gives that file's parsed JSON, as before
    #1073; one that holds neither gives ``None``. *show* reads the single
    file and is a seam for callers that fake Git; shards are always read from
    the repository, in one ``git cat-file --batch``.
    """
    state = state_dir.strip("/")
    shard_paths = _git_shard_paths(repo_root, revision, state)
    single = show(repo_root, revision, f"{state}/{CANDIDATE_HISTORY_FILE}" if state else CANDIDATE_HISTORY_FILE)
    if shard_paths and single is not None:
        raise StateStoreError(f"revision {revision} holds Candidate History in both layouts")
    if not shard_paths:
        if single is None:
            return None
        try:
            return json.loads(single.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StateStoreError(f"Candidate History at {revision} is not valid JSON") from exc
    records = []
    for path, raw in zip(shard_paths, _git_blobs(repo_root, revision, shard_paths), strict=True):
        relative = PurePosixPath(path).relative_to(state) if state else PurePosixPath(path)
        candidate_id = candidate_id_of_shard(relative)
        if candidate_id is None:
            raise StateStoreError(f"unexpected entry in {CANDIDATE_HISTORY_DIR}/ at {revision}: {path}")
        try:
            records.append(parse_shard(raw, path, candidate_id))
        except json.JSONDecodeError as exc:
            raise StateStoreError(f"Candidate History at {revision} is not valid JSON: {exc.msg}") from exc
    return sort_by_key(records, "identity_key")
