"""Shared safe artifact destination validation and pair publication.

This module is the single public home for the durable-artifact write policy
that several commands share:

* protected path validation (weekly/config/state directories, protected
  basenames, symlink and hard-link safety);
* exclusive staging of unpredictable temporary files (never following a
  symlink, never reusing a predictable name);
* atomic staged replacement and fail-closed, verified bundle rollback;
* generic atomic publication of one derived pair (two files that must never
  end up half-updated), one derived single file, or a multi-file bundle;
* cross-process OS-aware advisory pair and bundle locks (stable in the system
  temporary directory) held over one complete check-and-publish operation.

The probe ledger (``autoscout.source_probe``), the replay-fixture writer
(``autoscout.replay_fixtures``), and the probation-source scaffold
(``autoscout.source_scaffold``) delegate here; none copies publication or lock
logic. Callers map :class:`SafeArtifactError` to their own operator-facing
error type.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Literal
from typing import cast

# "candidate-history" is the directory that holds Candidate History one file
# per book (#1073): every file under it is canonical state, whatever its name.
PROTECTED_DIRECTORY_NAMES = frozenset(
    {
        "candidate-history",
        "config",
        "configs",
        "run",
        "runs",
        "state",
        "states",
        "weekly",
        "weekly-state",
        "weekly-states",
    }
)
PROTECTED_BASENAMES = frozenset(
    {
        "config.json",
        "configuration.json",
        "weekly-config.json",
        "source-mentions.json",
        "scouting-candidates.json",
        "latest-scouting-candidates.json",
        "candidate-history.json",
        "excluded-candidate-history.json",
        "pending-returned-editor-rows.json",
        "archived-returned-editor-rows.json",
        "candidate-visibility.json",
        "editorial-profile.json",
        "source-proposals.json",
        # Issue #1093: the state directory's backend marker.
        "state-backend.json",
        "source-runs.json",
        "run-summary.json",
        "pilot-status.json",
        "workflow-log.json",
        "codex-replay-config.json",
        "description-coverage.json",
        "internal-exclusions.json",
        "suppressed-candidates.json",
        "internal-pre-suppression-candidates.json",
        "hidden-by-rule.json",
        "hidden-from-review.json",
        "memo-queue.json",
        "russian-edition-checks.json",
        "source-metrics.json",
        "report-qa.json",
        "proposed-corrections.json",
        "description-enrichment.json",
        "grounded-description-stage.json",
        "description-adjudication-stage.json",
        "description-adjudication-preview.json",
        "derived-rule-suggestions.json",
        "search-optimization-review.json",
        "weekly-review.html",
        "weekly-review.md",
        "editor-review-guide.md",
        "source-review.md",
        # Issue #1057: the last collected list of each annual and monthly source.
        "scheduled-source-lists.json",
    }
)

StorageDestinationKind = Literal["file", "directory"]


class SafeArtifactError(ValueError):
    """A durable artifact destination or publication is not safe to use."""


class SafeArtifactRecoveryError(SafeArtifactError):
    """A failed publication could not be restored byte-for-byte."""


@dataclass(frozen=True)
class StorageDestination:
    """A validated storage target whose resolved path is safe to use for writes."""

    requested: Path
    path: Path
    kind: StorageDestinationKind


def _error_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or type(exc).__name__


def protected_storage_basename(path: Path) -> bool:
    protected_names = set(PROTECTED_BASENAMES)
    protected_names.update(Path(name).with_suffix(".md").name for name in PROTECTED_BASENAMES)
    return path.name.casefold() in protected_names


def protected_storage_location(path: Path) -> bool:
    for ancestor in (path, *path.parents):
        name = ancestor.name.casefold()
        if name in PROTECTED_DIRECTORY_NAMES or name.startswith("current-weekly"):
            return True
    return False


def validate_storage_ancestors(path: Path, label: str) -> None:
    """Validate existing lexical ancestors before using the resolved path."""
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            try:
                resolved_ancestor = ancestor.resolve(strict=False)
            except (OSError, RuntimeError) as exc:
                raise SafeArtifactError(f"{label} has an unsafe symlink ancestor") from exc
            if not resolved_ancestor.exists() or not resolved_ancestor.is_dir():
                raise SafeArtifactError(f"{label} has an unsafe symlink ancestor")
            if protected_storage_location(resolved_ancestor):
                raise SafeArtifactError(f"{label} has an unsafe symlink ancestor")
        elif ancestor.exists() and not ancestor.is_dir() and ancestor != path:
            raise SafeArtifactError(f"{label} parent must be a directory")


def resolve_storage_destination(
    path: Path,
    kind: StorageDestinationKind,
    label: str,
) -> StorageDestination:
    """Resolve and validate one artifact file or capture directory destination."""
    requested = path if path.is_absolute() else Path.cwd() / path
    try:
        resolved = requested.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise SafeArtifactError(f"cannot resolve {label}: {_error_text(exc)}") from exc

    validate_storage_ancestors(requested, label)
    if protected_storage_location(requested) or protected_storage_location(resolved):
        raise SafeArtifactError(f"protected weekly/config/state directory cannot contain {label}")
    if protected_storage_basename(resolved):
        raise SafeArtifactError(f"protected file or directory cannot be used as {label}: {resolved.name}")
    if requested.is_symlink():
        raise SafeArtifactError(f"{label} must not be a symlink")

    if kind == "file":
        if resolved.exists() and not resolved.is_file():
            raise SafeArtifactError(f"{label} must be a regular file")
        if resolved.exists() and resolved.stat().st_nlink != 1:
            raise SafeArtifactError(f"{label} must not be a hard link")
    elif resolved.exists() and not resolved.is_dir():
        raise SafeArtifactError(f"{label} must be a directory")
    return StorageDestination(requested=path, path=resolved, kind=kind)


def exclusive_temp(directory: Path, base_name: str, suffix: str) -> Path:
    """Create one exclusive temporary file that never follows a symlink.

    The name is unpredictable and the file is created with O_EXCL, so a
    stale or malicious file at a predictable staging name is never reused
    and never followed. If the descriptor cannot be closed after creation,
    the created file is removed again before the error propagates, so this
    helper never leaves a file behind when it raises."""
    descriptor, name = tempfile.mkstemp(prefix=f".{base_name}.", suffix=suffix, dir=directory)
    try:
        os.close(descriptor)
    except OSError:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise
    return Path(name)


def staged_replace(source: Path, destination: Path) -> None:
    """Atomically move one staged file over its destination."""
    os.replace(source, destination)


def _trusted_recovery_replace(source: Path, destination: Path) -> None:
    """Restore a backup without using a caller-controlled publication seam."""
    os.replace(source, destination)


def _write_content(path: Path, content: str | bytes) -> None:
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def rollback_pair(
    first_path: Path,
    second_path: Path,
    backup_first: Path | None,
    backup_second: Path | None,
    *,
    replace: Callable[[Path, Path], None] = staged_replace,
) -> None:
    """Restore both destinations from their backups, best effort.

    A destination that had no prior file is removed again. Each restore is
    attempted independently so one failing restore never masks the other
    or the original publish failure."""
    try:
        if backup_first is not None:
            replace(backup_first, first_path)
        else:
            first_path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        if backup_second is not None:
            replace(backup_second, second_path)
        else:
            second_path.unlink(missing_ok=True)
    except OSError:
        pass


def publish_pair(
    first_path: Path,
    second_path: Path,
    first_content: str | bytes,
    second_content: str | bytes,
    *,
    replace: Callable[[Path, Path], None] = staged_replace,
) -> None:
    """Publish two files as one atomic pair with rollback on failure.

    Both files are staged with exclusive temporary names first, then
    replaced one by one. If either replacement fails, both destinations
    are rolled back, so the pair never contains one updated file and one
    stale file. The *replace* callback is a seam: callers that must keep
    a module-level injection point pass their own wrapper so tests can
    inject failures.

    Both staged files are created inside the cleanup-protected block, so a
    failure while creating the second staged file removes the first one
    again and never leaves a stray temporary file behind."""
    backup_first: Path | None = None
    backup_second: Path | None = None
    had_first = first_path.exists()
    had_second = second_path.exists()
    staged_first: Path | None = None
    staged_second: Path | None = None
    try:
        staged_first = exclusive_temp(first_path.parent, first_path.name, ".stage")
        staged_second = exclusive_temp(second_path.parent, second_path.name, ".stage")
        _write_content(staged_first, first_content)
        _write_content(staged_second, second_content)
        if had_first:
            backup_first = exclusive_temp(first_path.parent, first_path.name, ".backup")
            backup_first.write_bytes(first_path.read_bytes())
        if had_second:
            backup_second = exclusive_temp(second_path.parent, second_path.name, ".backup")
            backup_second.write_bytes(second_path.read_bytes())
        try:
            replace(staged_first, first_path)
            replace(staged_second, second_path)
        except Exception:
            rollback_pair(first_path, second_path, backup_first, backup_second, replace=replace)
            raise
    finally:
        for leftover in (staged_first, staged_second, backup_first, backup_second):
            if leftover is not None:
                try:
                    leftover.unlink(missing_ok=True)
                except OSError:
                    pass


def _unlink(path: Path) -> None:
    path.unlink()


def publish_bundle(
    paths: Sequence[Path],
    contents: Sequence[str | bytes],
    *,
    replace: Callable[[Path, Path], None] = staged_replace,
    removals: Sequence[Path] = (),
    remove: Callable[[Path], None] = _unlink,
) -> None:
    """Publish a file bundle with verified, fail-closed rollback.

    Every staged file and every original destination is captured before the
    first replacement.
    *removals* are files the bundle deletes: they are removed after every
    replacement succeeded, and a failure on the way restores them with the
    replaced files, so a bundle that writes some files and deletes others is
    all or nothing too. A removal of a file that does not exist is a no-op.
    If forward publication fails, restoration uses the trusted recovery
    replacement path rather than the caller's failure-injection seams.
    Restoration verifies both existence and original bytes for every
    destination.
    Recovery copies are restored through a separate atomic stage, so the
    original backups remain available until verification succeeds.
    Recovery backups are retained when restoration cannot be completed.
    The caller must validate destination policy before calling this primitive.
    """
    if len(paths) != len(contents) or not (paths or removals):
        raise SafeArtifactError("bundle paths and contents must have the same length, and the bundle must do something")
    resolved_paths = [path.resolve(strict=False) for path in (*paths, *removals)]
    if len(set(resolved_paths)) != len(resolved_paths):
        raise SafeArtifactError("bundle destinations must be different files")

    targets = [*paths, *removals]
    staged: list[Path] = []
    backups: list[Path | None] = [None] * len(targets)
    had_files = [path.exists() for path in targets]
    original_bytes = [
        path.read_bytes() if had_file else None for path, had_file in zip(targets, had_files, strict=True)
    ]
    recovery_failed = False
    recovery_staged: list[Path] = []
    try:
        for path, content in zip(paths, contents, strict=True):
            staged_path = exclusive_temp(path.parent, path.name, ".stage")
            staged.append(staged_path)
            _write_content(staged_path, content)
        for index, path in enumerate(targets):
            if had_files[index]:
                created_backup = exclusive_temp(path.parent, path.name, ".backup")
                created_backup.write_bytes(cast(bytes, original_bytes[index]))
                backups[index] = created_backup
        try:
            for staged_path, path in zip(staged, paths, strict=True):
                replace(staged_path, path)
            for index, path in enumerate(removals, start=len(paths)):
                if had_files[index]:
                    remove(path)
        except Exception as forward_error:
            for index, path in enumerate(targets):
                backup = backups[index]
                try:
                    if backup is not None:
                        recovery_path = exclusive_temp(path.parent, path.name, ".recovery")
                        recovery_staged.append(recovery_path)
                        recovery_path.write_bytes(backup.read_bytes())
                        _trusted_recovery_replace(recovery_path, path)
                    elif not had_files[index]:
                        path.unlink(missing_ok=True)
                    expected = original_bytes[index]
                    if had_files[index]:
                        if expected is None or not path.is_file() or path.read_bytes() != expected:
                            raise OSError("destination bytes were not restored")
                    elif path.exists():
                        raise OSError("new destination was not removed")
                except Exception:
                    recovery_failed = True
            if recovery_failed:
                raise SafeArtifactRecoveryError("bundle recovery failed") from forward_error
            raise
    finally:
        for leftover in (*staged, *recovery_staged):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                pass
        if not recovery_failed:
            for backup in backups:
                if backup is not None:
                    try:
                        backup.unlink(missing_ok=True)
                    except OSError:
                        pass


def publish_single(
    path: Path,
    content: str | bytes,
    *,
    replace: Callable[[Path, Path], None] = staged_replace,
) -> None:
    """Publish one derived file with an exclusive staged file.

    The write is atomic, so a failed write leaves the previous file intact
    and never leaves a partial file at the final path. The staged file is
    created inside the cleanup-protected block, so a staging failure cannot
    leave a stray temporary file behind."""
    staged: Path | None = None
    try:
        staged = exclusive_temp(path.parent, path.name, ".stage")
        _write_content(staged, content)
        replace(staged, path)
    finally:
        if staged is not None:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass


def _lock_pair_descriptor(file_descriptor: int) -> None:
    """Acquire the advisory pair lock, choosing the OS API at call time.

    Windows uses the CRT byte-range lock on the first byte; ``LK_LOCK``
    retries once per second and gives up after ten seconds instead of
    racing. POSIX uses ``flock`` which blocks until the lock is free.
    Neither platform-specific module is imported at module load."""
    if os.name == "nt":
        import msvcrt

        msvcrt_module: Any = msvcrt
        os.lseek(file_descriptor, 0, os.SEEK_SET)
        msvcrt_module.locking(file_descriptor, msvcrt_module.LK_LOCK, 1)
        return

    import fcntl

    fcntl.flock(file_descriptor, fcntl.LOCK_EX)


def _unlock_pair_descriptor(file_descriptor: int) -> None:
    """Release the advisory pair lock with the same OS API that acquired it."""
    if os.name == "nt":
        import msvcrt

        msvcrt_module: Any = msvcrt
        os.lseek(file_descriptor, 0, os.SEEK_SET)
        msvcrt_module.locking(file_descriptor, msvcrt_module.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(file_descriptor, fcntl.LOCK_UN)


@contextlib.contextmanager
def pair_write_lock(first_path: Path, second_path: Path) -> Iterator[None]:
    """Serialize one complete pair check-and-publish across processes and threads.

    The lock identity is a deterministic digest of the two resolved output
    paths, so every concurrent writer of the same pair (in any process)
    blocks on the same advisory lock file. The lock file lives in the system
    temporary directory - never in the artifact output directory - and the
    advisory lock is released automatically when the descriptor closes, so a
    crashed writer cannot leave a stale lock. The lock serializes separate
    open file descriptions, so two threads in one process serialize exactly
    like two processes.

    The lock file is never removed. Removing it on release would let a
    writer still waiting on the old file and a writer that created a new one
    hold the lock at the same time. Its name depends only on the guarded
    paths, so a run that guards the same paths reuses it (#1119)."""
    identity = pair_lock_identity(first_path, second_path)
    lock_path = Path(tempfile.gettempdir()) / f"autoscout-pair-{identity}.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        _lock_pair_descriptor(descriptor)
        acquired = True
        yield
    finally:
        try:
            if acquired:
                _unlock_pair_descriptor(descriptor)
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass


def pair_lock_identity(first_path: Path, second_path: Path) -> str:
    """Deterministic lock identity for one destination pair.

    The identity is a digest of the two resolved absolute paths, sorted so
    that argument order does not matter. Two concurrent writers of the same
    pair (in any process) derive the same identity and therefore serialize
    on the same advisory lock file."""
    resolved = sorted(str(path.resolve(strict=False)) for path in (first_path, second_path))
    return hashlib.sha256("\x00".join(resolved).encode("utf-8")).hexdigest()[:32]


@contextlib.contextmanager
def bundle_write_lock(*paths: Path) -> Iterator[None]:
    """Serialize check-and-publish operations for an arbitrary file bundle.

    The lock file stays after release for the reason given in
    :func:`pair_write_lock`."""
    if not paths:
        raise SafeArtifactError("bundle lock requires at least one path")
    resolved = sorted(str(path.resolve(strict=False)) for path in paths)
    identity = hashlib.sha256("\x00".join(resolved).encode("utf-8")).hexdigest()[:32]
    lock_path = Path(tempfile.gettempdir()) / f"autoscout-bundle-{identity}.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        _lock_pair_descriptor(descriptor)
        acquired = True
        yield
    finally:
        try:
            if acquired:
                _unlock_pair_descriptor(descriptor)
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
