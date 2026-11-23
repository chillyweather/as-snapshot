"""Whether a state directory's canonical files are what Git has committed (#1098).

The doctor and run-health use this when the exported files differ from what
the database last exported. Which side is the newer one decides which
command is safe: a database older than the files a ``git pull`` brought must
be rebuilt from them, a hand edit on top of the committed files must be
undone first, and neither may be settled by writing the database over files
that Git holds.

The look is read-only and offline. It reads Git objects (``ls-tree``) and
hashes the working files (``hash-object`` without ``-w``). It never reads
or refreshes the index (``git status`` and ``git diff`` would), never takes
an optional lock, fetches nothing and writes nothing.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath

from autoscout.state_store.json_backend import FILE_NAMES
from autoscout.state_store.json_backend import LEGACY_PROFILE_FILE_NAME
from autoscout.state_store.layout import CANDIDATE_HISTORY_DIR
from autoscout.state_store.layout import shard_files

# The working files equal the files committed at HEAD.
COMMITTED = "committed"
# They differ from HEAD: uncommitted edits, additions or deletions.
EDITED = "edited"
# Git cannot say: no Git repository, no HEAD, or Git did not answer.
UNKNOWN = "unknown"

_GIT_TIMEOUT_SECONDS = 120
_CANONICAL_NAMES = frozenset({*FILE_NAMES.values(), LEGACY_PROFILE_FILE_NAME})


@dataclass(frozen=True)
class StateFilesAgainstHead:
    """How the canonical files of a state directory compare with HEAD."""

    relation: str
    # The state directory relative to the repository root, in posix form ("." for the root itself).
    repository_path: str = ""
    # A canonical file exists in the working tree that HEAD does not have (new, staged or not): a restore from HEAD leaves it.
    has_added_files: bool = False
    # What a restore from HEAD must name to undo the difference, relative to the state directory:
    # each differing top-level file, and the shard directory when any shard differs.
    differing_paths: tuple[str, ...] = ()


def _is_canonical(relative: str) -> bool:
    path = PurePosixPath(relative)
    return relative in _CANONICAL_NAMES or (len(path.parts) > 1 and path.parts[0] == CANDIDATE_HISTORY_DIR)


def _git(state_dir: Path, *args: str, stdin: bytes | None = None) -> bytes | None:
    """Run a read-only Git command in *state_dir*; ``None`` when it fails."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(state_dir), *args],
            input=stdin,
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def _committed_blobs(state_dir: Path) -> dict[str, str] | None:
    """The canonical files at HEAD as ``relative path -> blob id``; ``None`` when Git cannot say."""
    listing = _git(state_dir, "ls-tree", "-r", "-z", "HEAD", "--", ".")
    if listing is None:
        return None
    blobs: dict[str, str] = {}
    for entry in listing.decode("utf-8", errors="surrogateescape").split("\0"):
        if not entry:
            continue
        meta, _, name = entry.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob" and _is_canonical(name):
            blobs[name] = parts[2]
    return blobs


def _working_files(state_dir: Path) -> list[str]:
    names = [name for name in sorted(_CANONICAL_NAMES) if (state_dir / name).is_file()]
    shards = [path.relative_to(state_dir).as_posix() for path in shard_files(state_dir)]
    return names + shards


def _working_blobs(state_dir: Path) -> dict[str, str] | None:
    """The canonical working files as ``relative path -> blob id`` Git would give them; ``None`` when Git cannot say."""
    files = _working_files(state_dir)
    if not files:
        return {}
    # Absolute paths: ``hash-object`` resolves a relative one from the repository root, not from *state_dir*.
    request = "".join(f"{state_dir.resolve() / name}\n" for name in files).encode()
    # No filters: a configured clean filter is an external program, which a read-only check must not run.
    # The bytes are compared as they are, so a checkout that converts line endings reads as edited.
    hashed = _git(state_dir, "hash-object", "--no-filters", "--stdin-paths", stdin=request)
    if hashed is None:
        return None
    ids = hashed.decode("ascii", errors="replace").split()
    return dict(zip(files, ids, strict=True)) if len(ids) == len(files) else None


def state_files_against_head(state_dir: Path) -> StateFilesAgainstHead:
    """Compare the canonical files of *state_dir* with the commit at HEAD, changing nothing.

    ``COMMITTED`` when every canonical file is byte-identical to its committed
    version and no other canonical file exists on either side, ``EDITED`` when
    one differs, appeared or vanished, ``UNKNOWN`` when Git cannot answer.
    """
    unknown = StateFilesAgainstHead(UNKNOWN)
    if not state_dir.is_dir():
        return unknown
    where = _git(state_dir, "rev-parse", "--is-inside-work-tree", "--show-prefix")
    if where is None:
        return unknown
    lines = where.decode("utf-8", errors="replace").split("\n")
    if lines[0].strip() != "true":
        return unknown
    repository_path = (lines[1].strip().rstrip("/") if len(lines) > 1 else "") or "."
    committed = _committed_blobs(state_dir)
    working = _working_blobs(state_dir)
    if committed is None or working is None:
        return unknown
    if committed == working:
        return StateFilesAgainstHead(COMMITTED, repository_path)
    added = any(name not in committed for name in working)
    differing = {name for name in {*committed, *working} if committed.get(name) != working.get(name)}
    paths = {
        CANDIDATE_HISTORY_DIR if PurePosixPath(name).parts[0] == CANDIDATE_HISTORY_DIR else name for name in differing
    }
    return StateFilesAgainstHead(EDITED, repository_path, added, tuple(sorted(paths)))
