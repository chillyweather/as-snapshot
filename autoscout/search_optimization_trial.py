"""Preserve a Search Optimization trial run into an immutable archive.

The Search Optimization trial is read-only by design. It never changes
canonical artifacts. This module copies the trial run's three evidence
files byte-for-byte into a deterministic archive directory, so a later
three-run comparison can read the preserved inputs.

The archive write is staged inside the archive directory and published
with one atomic directory rename. A failed attempt leaves no staging
directory behind and never touches the source run directory.

A per-destination persistent advisory lock prevents two concurrent
preservation calls from overwriting each other's archive. The lock file
lives outside the archive directory and its inode is never removed or
replaced. A process crash releases the advisory lock, so a retry is
never permanently blocked. The lock is held for the whole staging and
publish operation and is released on every path.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.search_optimization import SEARCH_OPTIMIZATION_JSON_FILENAME
from autoscout.search_optimization import SEARCH_OPTIMIZATION_MD_FILENAME
from autoscout.search_optimization import SearchOptimizationReview

RUN_SUMMARY_FILENAME = "run-summary.json"
MANIFEST_FILENAME = "trial-run-manifest.json"
MANIFEST_ARTIFACT_TYPE = "search-optimization-trial-run"
MANIFEST_SCHEMA_VERSION = 1

PRESERVED_FILENAMES = (
    SEARCH_OPTIMIZATION_JSON_FILENAME,
    SEARCH_OPTIMIZATION_MD_FILENAME,
    RUN_SUMMARY_FILENAME,
)

_STAGING_PREFIX = ".search-optimization-trial-"
_LOCK_DIRECTORY = "autoscout-search-optimization-trial-locks"


def _read_required_json(path: Path, label: str) -> Any:
    """Read one required JSON artifact, refusing missing and malformed input."""
    if not path.is_file():
        raise ValueError(f"Missing required {label} file: {path.name}")
    try:
        return read_json(path)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed {label} JSON in {path.name}: {exc.msg}") from exc
    except OSError as exc:
        raise ValueError(f"Cannot read {label} file {path.name}: {exc}") from exc


def _parse_summary_run_date(value: Any) -> str:
    """Return a validated summary run date, refusing blank or malformed values."""
    if value is None or not str(value).strip():
        raise ValueError("run-summary.json has a blank run_date.")
    run_date = str(value).strip()
    try:
        date.fromisoformat(run_date)
    except ValueError as exc:
        raise ValueError(f"run-summary.json has a malformed run_date '{run_date}'.") from exc
    return run_date


def _validate_operation_id(operation_id: str) -> None:
    """Refuse an operation id that can form an unsafe path component."""
    if operation_id in (".", "..") or "/" in operation_id or "\\" in operation_id:
        raise ValueError("run-summary.json has an unsafe operation_id.")
    if any(ord(char) < 32 or ord(char) == 127 for char in operation_id):
        raise ValueError("run-summary.json has an unsafe operation_id.")


def _read_summary_identity(path: Path, label: str) -> tuple[str, str, str]:
    """Read and validate the identity fields of one run-summary.json.

    ``label`` prefixes the validation messages so the source read and
    the staged read are distinguishable: pass "" for the source file and
    "Staged " for the staged copy.
    """
    summary = _read_required_json(path, "run-summary")
    if not isinstance(summary, dict):
        raise ValueError(f"{label}run-summary.json must be a JSON object.")
    summary_run_date = _parse_summary_run_date(summary.get("run_date"))
    operation_id_value = summary.get("operation_id")
    if not isinstance(operation_id_value, str):
        raise ValueError(f"{label}run-summary.json operation_id must be a string.")
    operation_id = operation_id_value.strip()
    if not operation_id:
        raise ValueError(f"{label}run-summary.json has a blank operation_id.")
    evidence_digest_value = summary.get("evidence_digest")
    if not isinstance(evidence_digest_value, str):
        raise ValueError(f"{label}run-summary.json evidence_digest must be a string.")
    evidence_digest = evidence_digest_value.strip()
    if not evidence_digest:
        raise ValueError(f"{label}run-summary.json has a blank evidence_digest.")
    _validate_operation_id(operation_id)
    return summary_run_date, operation_id, evidence_digest


class _StableDestinationLockBusy(RuntimeError):
    """Raised when another process owns the stable destination lock."""


class _StableDestinationLock:
    """A persistent lock file whose inode is never replaced or removed."""

    def __init__(self, file_descriptor: int) -> None:
        self._file_descriptor: int | None = file_descriptor

    @classmethod
    def try_acquire(cls, path: Path, *, create: bool) -> _StableDestinationLock | None:
        flags = os.O_RDWR
        if create:
            flags |= os.O_CREAT
        try:
            file_descriptor = os.open(path, flags, 0o600)
        except FileNotFoundError:
            if create:
                raise
            return None

        try:
            _lock_file_descriptor(file_descriptor)
        except BaseException:
            _close_file_descriptor(file_descriptor)
            raise
        return cls(file_descriptor)

    def release(self) -> None:
        file_descriptor = self._file_descriptor
        if file_descriptor is None:
            return
        self._file_descriptor = None
        try:
            _unlock_file_descriptor(file_descriptor)
        finally:
            _close_file_descriptor(file_descriptor)


def _lock_file_descriptor(file_descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt_module: Any = msvcrt
        os.lseek(file_descriptor, 0, os.SEEK_SET)
        try:
            msvcrt_module.locking(file_descriptor, msvcrt_module.LK_NBLCK, 1)
        except OSError as exc:
            raise _StableDestinationLockBusy from exc
        return

    import fcntl

    try:
        fcntl.flock(file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            raise _StableDestinationLockBusy from exc
        raise


def _unlock_file_descriptor(file_descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt_module: Any = msvcrt
        os.lseek(file_descriptor, 0, os.SEEK_SET)
        msvcrt_module.locking(file_descriptor, msvcrt_module.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(file_descriptor, fcntl.LOCK_UN)


def _close_file_descriptor(file_descriptor: int) -> None:
    try:
        os.close(file_descriptor)
    except OSError:
        pass


def trial_destination_lock_path(destination: Path) -> Path:
    """Return the stable advisory lock path for one archive destination.

    The lock file is persistent: its inode is never removed or replaced,
    so a crashed process never leaves a stale reservation that blocks a
    retry.
    """
    resolved_destination = destination.resolve()
    directory_key = hashlib.sha256(os.fsencode(str(resolved_destination))).hexdigest()
    return Path(tempfile.gettempdir()) / _LOCK_DIRECTORY / f"{directory_key}.lock"


def _acquire_destination_lock(lock_path: Path, destination_name: str) -> _StableDestinationLock:
    """Hold one archive destination's stable advisory lock.

    Raises ValueError when another process already holds the lock for
    the same destination.
    """
    try:
        stable_lock = _StableDestinationLock.try_acquire(lock_path, create=True)
    except _StableDestinationLockBusy as exc:
        raise ValueError(f"Archive preservation already in progress for {destination_name}") from exc
    if stable_lock is None:
        raise ValueError(f"Archive preservation already in progress for {destination_name}")
    return stable_lock


def _validate_staged_preservation(
    staging: Path,
    *,
    expected_run_date: str,
    expected_operation_id: str,
    expected_evidence_digest: str,
) -> tuple[str, str, str]:
    """Re-validate the staged copies immediately before the atomic publish.

    The source files are copied while the run directory may still be
    written by another process. The staged review JSON is validated
    through the public SearchOptimizationReview domain facade again, and
    the staged run-summary.json identity fields must match the values
    already committed to the destination name and the manifest. A
    malformed staged review or a changed identity fails preservation, and
    the caller removes the staging directory without publishing.

    Returns the validated staged identity fields.
    """
    review_data = _read_required_json(staging / SEARCH_OPTIMIZATION_JSON_FILENAME, "staged search-optimization-review")
    try:
        staged_review = SearchOptimizationReview.from_mapping(review_data)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid staged search-optimization-review.json: {exc}") from exc

    staged_run_date, staged_operation_id, staged_evidence_digest = _read_summary_identity(
        staging / RUN_SUMMARY_FILENAME,
        "Staged ",
    )
    if staged_run_date != staged_review.run_date:
        raise ValueError(
            f"Staged run-summary.json run_date '{staged_run_date}' does not match "
            f"review run_date '{staged_review.run_date}'."
        )
    if staged_run_date != expected_run_date:
        raise ValueError(
            f"Staged run-summary.json run_date '{staged_run_date}' does not match "
            f"the preserved run date '{expected_run_date}'."
        )
    if staged_operation_id != expected_operation_id:
        raise ValueError(
            f"Staged run-summary.json operation_id '{staged_operation_id}' does not match "
            f"the preserved operation_id '{expected_operation_id}'."
        )
    if staged_evidence_digest != expected_evidence_digest:
        raise ValueError(
            f"Staged run-summary.json evidence_digest '{staged_evidence_digest}' does not match "
            f"the preserved evidence_digest '{expected_evidence_digest}'."
        )
    return staged_run_date, staged_operation_id, staged_evidence_digest


def preserve_search_optimization_run(run_dir: Path, archive_dir: Path) -> Path:
    """Preserve one Search Optimization trial run into ``archive_dir``.

    Reads search-optimization-review.json, search-optimization-review.md,
    and run-summary.json from *run_dir*. The review JSON is validated
    through the public SearchOptimizationReview domain facade. The archive
    destination is the deterministic direct child
    ``<run-date>-<first-12-operation-id-characters>`` of *archive_dir*.

    The three source files are copied byte-for-byte, and a
    trial-run-manifest.json records their sha256 digests plus the run
    identity fields. The manifest's canonical_effect is always "none":
    the trial and this archive never change canonical state.

    Returns the preserved directory path. Raises ValueError for missing,
    malformed, invalid, unsafe, mismatched, or duplicate input, and never
    modifies the source run directory. Publishing is guarded by a
    persistent per-destination advisory lock, so a concurrent duplicate
    attempt cannot overwrite an existing archive and a crashed attempt
    never blocks a retry.
    """
    run = run_dir.resolve()
    archive = archive_dir.resolve()
    if archive == run or archive.is_relative_to(run):
        raise ValueError("archive_dir must not be the run directory or a descendant of it.")

    review_path = run_dir / SEARCH_OPTIMIZATION_JSON_FILENAME
    markdown_path = run_dir / SEARCH_OPTIMIZATION_MD_FILENAME
    summary_path = run_dir / RUN_SUMMARY_FILENAME

    review_data = _read_required_json(review_path, "search-optimization-review")
    try:
        review = SearchOptimizationReview.from_mapping(review_data)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid search-optimization-review.json: {exc}") from exc

    if not markdown_path.is_file():
        raise ValueError(f"Missing required {SEARCH_OPTIMIZATION_MD_FILENAME} file: {markdown_path.name}")

    summary_run_date, operation_id, evidence_digest = _read_summary_identity(summary_path, "")
    if summary_run_date != review.run_date:
        raise ValueError(
            f"run-summary.json run_date '{summary_run_date}' does not match review run_date '{review.run_date}'."
        )

    destination_name = f"{summary_run_date}-{operation_id[:12]}"
    destination = archive_dir / destination_name
    if os.path.lexists(destination):
        raise ValueError(f"Archive destination already exists: {destination_name}")
    if not destination.resolve().is_relative_to(archive):
        raise ValueError("Archive destination escapes the archive directory.")

    lock_path = trial_destination_lock_path(destination)
    staging: Path | None = None
    stable_lock: _StableDestinationLock | None = None
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(lock_path.parent, 0o700)
        except OSError:
            pass
        stable_lock = _acquire_destination_lock(lock_path, destination_name)
        # The lock is exclusive. Re-check the destination so a publisher
        # that won a race before we acquired the lock cannot be
        # overwritten by the publish below.
        if os.path.lexists(destination):
            raise ValueError(f"Archive destination already exists: {destination_name}")
        staging = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=archive_dir))
        for name in PRESERVED_FILENAMES:
            shutil.copyfile(run_dir / name, staging / name)
        # The staged copies are re-validated before the publish: a source
        # mutation during the copy must not publish a malformed review or
        # a changed identity.
        staged_run_date, staged_operation_id, staged_evidence_digest = _validate_staged_preservation(
            staging,
            expected_run_date=summary_run_date,
            expected_operation_id=operation_id,
            expected_evidence_digest=evidence_digest,
        )
        # Hash the staged copies, not the source files: the staged bytes
        # are exactly what the atomic rename publishes, so the manifest
        # digest always describes the archive content even if a source
        # file changes after the copy.
        files_manifest = {
            name: hashlib.sha256((staging / name).read_bytes()).hexdigest() for name in PRESERVED_FILENAMES
        }
        manifest = {
            "artifact_type": MANIFEST_ARTIFACT_TYPE,
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "run_date": staged_run_date,
            "operation_id": staged_operation_id,
            "evidence_digest": staged_evidence_digest,
            "files": files_manifest,
            "canonical_effect": "none",
            "preservation_statement": (
                "This archive preserves the Search Optimization trial run artifacts byte-for-byte "
                "and applies no canonical effect to candidate history, feedback, visibility, "
                "ranking, sources, or any other state."
            ),
        }
        write_json(staging / MANIFEST_FILENAME, manifest)
        os.replace(staging, destination)
        staging = None
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if stable_lock is not None:
            stable_lock.release()
    return destination
