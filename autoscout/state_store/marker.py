"""The backend marker: which backend a state directory is kept in (#1093, ADR 0023).

A state directory cut over to SQLite holds ``state-backend.json`` with exactly
``{"backend": "sqlite"}``. The file is committed with the rest of the state
directory, so every checkout and every command that opens the directory sees
it, whether or not it was given the weekly config or the environment
variable. A state directory without the file is kept as the selection says,
which is the JSON backend unless the environment or the config names another.

The marker names a backend and nothing else: the database lives outside Git
at a path that differs between machines (``sqlite_location``), so the marker
never carries one.

``./scripts/state-database cutover`` writes the marker after exporting the
database into the directory, and ``./scripts/state-database rollback``
exports again and removes it. Nothing else writes or removes it.
"""

from __future__ import annotations

import json
from pathlib import Path

from autoscout.json_io import serialize_json
from autoscout.state_store import SQLITE_BACKEND
from autoscout.state_store import StateStoreError

BACKEND_MARKER_FILE = "state-backend.json"
# The backends a marker may name. JSON is what an unmarked directory is kept in,
# so no marker names it.
MARKED_BACKENDS = (SQLITE_BACKEND,)


def marker_path(state_dir: Path) -> Path:
    return state_dir / BACKEND_MARKER_FILE


def marked_backend(state_dir: Path) -> str | None:
    """The backend *state_dir*'s marker names, ``None`` when it has none.

    A marker that is a symlink, not a file, unreadable, or anything but
    ``{"backend": "sqlite"}`` is refused rather than ignored: a directory
    whose marker cannot be read must not fall back to the JSON backend.
    """
    path = marker_path(state_dir)
    if not (path.exists() or path.is_symlink()):
        return None
    if path.is_symlink() or not path.is_file():
        raise StateStoreError(f"the backend marker {BACKEND_MARKER_FILE} of the state directory must be a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise StateStoreError(f"the backend marker {BACKEND_MARKER_FILE} of the state directory is unreadable") from exc
    backend = value.get("backend") if isinstance(value, dict) and set(value) == {"backend"} else None
    if backend not in MARKED_BACKENDS:
        raise StateStoreError(
            f"the backend marker {BACKEND_MARKER_FILE} of the state directory must hold exactly "
            f'{{"backend": "{SQLITE_BACKEND}"}}'
        )
    return str(backend)


def marker_bytes(backend: str) -> str:
    return serialize_json({"backend": backend})


def write_marker(state_dir: Path, backend: str) -> None:
    """Record that *state_dir* is kept in *backend*; the file is replaced atomically."""
    from autoscout.safe_artifacts import publish_single

    if backend not in MARKED_BACKENDS:
        raise StateStoreError(f"a state directory cannot be marked as kept in {backend!r}")
    publish_single(marker_path(state_dir), marker_bytes(backend))


def remove_marker(state_dir: Path) -> bool:
    """Remove *state_dir*'s marker; return whether there was one."""
    path = marker_path(state_dir)
    if not (path.exists() or path.is_symlink()):
        return False
    path.unlink()
    return True
