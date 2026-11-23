"""Where the SQLite backend keeps its database file (#1074, ADR 0023).

The database is never committed. By default it lives in the repository's
git-ignored ``data/state-store/`` directory, one file per state directory:
``runs/current-weekly-pilot-russian`` is kept in
``data/state-store/runs--current-weekly-pilot-russian.sqlite3``. A state
directory outside the repository gets a name made from its own name and a
digest of its absolute path. The ``AUTOSCOUT_STATE_DATABASE_DIR`` environment
variable moves these default files to another directory (the test suite keeps
each test's databases in its own temporary directory with it, #1075). The
weekly config's ``state_database`` key or the ``AUTOSCOUT_STATE_DATABASE``
environment variable names one file instead; a relative path in any of them is
taken from the repository root.

A database on a network file system or in a folder a cloud service syncs
(iCloud, Dropbox, OneDrive, Google Drive and the like) can be corrupted by
the sync, so such a path is refused when it can be detected.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from autoscout.repository_root import repository_root

REPOSITORY_ROOT = repository_root()
DEFAULT_DATABASE_DIR = REPOSITORY_ROOT / "data" / "state-store"
DATABASE_SUFFIX = ".sqlite3"
DATABASE_DIR_ENV = "AUTOSCOUT_STATE_DATABASE_DIR"

LOCAL = "local"
SYNCED = "synced"
NETWORK = "network"
UNKNOWN = "unknown"

# Path components that only a cloud-synced folder has.
_SYNCED_MARKERS = (
    ("library", "mobile documents"),
    ("library", "cloudstorage"),
    ("icloud drive",),
    ("icloud",),
    ("dropbox",),
    ("onedrive",),
    ("google drive",),
    ("googledrive",),
    ("my drive",),
    ("box sync",),
    ("pcloud drive",),
    ("mega",),
    ("nextcloud",),
    ("owncloud",),
    ("seafile",),
    ("sync",),
    ("syncthing",),
    ("yandex.disk",),
)

_NETWORK_FILE_SYSTEMS = frozenset(
    {
        "nfs",
        "nfs4",
        "cifs",
        "smb",
        "smb2",
        "smb3",
        "smbfs",
        "afpfs",
        "afs",
        "9p",
        "ceph",
        "glusterfs",
        "lustre",
        "davfs",
        "webdav",
        "fuse.sshfs",
        "sshfs",
        "fuse.rclone",
        "fuse.s3fs",
        "fuse.gcsfuse",
        "fuse.davfs",
        "fuse.glusterfs",
        "fuse.cephfs",
        "fuse.onedriver",
        "fuse.dropbox",
    }
)


def state_dir_key(state_dir: Path) -> str:
    """How a database names the state directory it belongs to: repository-relative when inside it."""
    resolved = state_dir.resolve()
    try:
        return resolved.relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def default_database_dir() -> Path:
    """The directory of the default database files: ``AUTOSCOUT_STATE_DATABASE_DIR``, else ``data/state-store``."""
    configured = os.environ.get(DATABASE_DIR_ENV, "").strip()
    return configured_database_path(configured) if configured else DEFAULT_DATABASE_DIR


def default_database_path(state_dir: Path) -> Path:
    """The database file of *state_dir* when no other is configured."""
    resolved = state_dir.resolve()
    try:
        relative = resolved.relative_to(REPOSITORY_ROOT)
    except ValueError:
        digest = hashlib.sha256(resolved.as_posix().encode("utf-8")).hexdigest()[:12]
        name = f"{resolved.name or 'state'}-{digest}"
    else:
        name = "--".join(relative.parts) or "repository"
    return default_database_dir() / f"{name}{DATABASE_SUFFIX}"


def configured_database_path(value: str | os.PathLike[str]) -> Path:
    """A configured database path; a relative one is taken from the repository root."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else REPOSITORY_ROOT / path


def display_path(path: Path) -> str:
    """The database path as the operator sees it: repository-relative when inside the repository."""
    try:
        return path.resolve().relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _existing_ancestor(path: Path) -> Path:
    current = path.absolute()
    while not current.exists() and current != current.parent:
        current = current.parent
    return current.resolve()


def _has_synced_marker(path: Path) -> bool:
    parts = [part.lower() for part in path.parts]
    for marker in _SYNCED_MARKERS:
        width = len(marker)
        for start in range(len(parts) - width + 1):
            window = tuple(parts[start : start + width])
            if window == marker:
                return True
            # "Dropbox (Personal)", "OneDrive - Company", "Google Drive" with a suffix.
            if width == 1 and marker[0] not in {"sync", "mega"} and window[0].startswith(marker[0] + " "):
                return True
    return False


@lru_cache(maxsize=1)
def _mount_table() -> tuple[tuple[str, str], ...]:
    """Mount points and their file system types, longest mount point first; empty when unreadable."""
    mounts: list[tuple[str, str]] = []
    try:
        if sys.platform.startswith("linux"):
            for line in Path("/proc/self/mounts").read_text(encoding="utf-8", errors="replace").splitlines():
                fields = line.split()
                if len(fields) >= 3:
                    mounts.append((fields[1].replace("\\040", " "), fields[2].lower()))
        elif sys.platform == "darwin":
            output = subprocess.run(["/sbin/mount"], capture_output=True, text=True, check=False, timeout=5).stdout
            for line in output.splitlines():
                # "//user@host/share on /Volumes/share (smbfs, nodev, ...)"
                if " on " not in line or "(" not in line:
                    continue
                mount_point = line.split(" on ", 1)[1].rsplit(" (", 1)[0]
                fs_type = line.rsplit("(", 1)[1].split(",", 1)[0].strip(") ").lower()
                mounts.append((mount_point, fs_type))
    except (OSError, subprocess.SubprocessError):
        return ()
    return tuple(sorted(mounts, key=lambda mount: len(mount[0]), reverse=True))


def _file_system_type(path: Path) -> str | None:
    text = path.as_posix()
    for mount_point, fs_type in _mount_table():
        if text == mount_point or text.startswith(mount_point.rstrip("/") + "/"):
            return fs_type
    return None


def database_path_class(path: Path) -> str:
    """``synced``, ``network``, ``local`` or ``unknown`` (no mount table to read)."""
    anchor = _existing_ancestor(path)
    if _has_synced_marker(path.absolute()) or _has_synced_marker(anchor):
        return SYNCED
    fs_type = _file_system_type(anchor)
    if fs_type is None:
        return UNKNOWN
    if fs_type in _NETWORK_FILE_SYSTEMS or fs_type.startswith(("nfs", "smb", "cifs")):
        return NETWORK
    return LOCAL
