"""Local Weekly Report artifact catalog and coherent snapshot store.

The store knows run-local report artifacts only.  It does not interpret
workflow evidence, Codex resume markers, or canonical cross-week state.
"""

from __future__ import annotations

import csv
import errno
import hashlib
import json
import os
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.state_store import StateStore
from autoscout.state_store import keeps_json_in_state_dir
from autoscout.state_store.layout import CANDIDATE_HISTORY_DIR
from autoscout.state_store.layout import is_single_file_layout
from autoscout.state_store.layout import read_sharded_records
from autoscout.weekly_report_row import EDITOR_FIELDNAMES
from autoscout.weekly_report_row import OPTIONAL_EDITOR_FIELDNAMES
from autoscout.weekly_report_row import editor_csv_fieldnames

TRANSACTION_MARKER = ".weekly-report-transaction.json"
_TRANSACTION_LOCK_DIRECTORY = "autoscout-weekly-report-locks"


class _StableLockBusy(RuntimeError):
    """Raised when another process owns the stable report lock."""


class _StableReportLock:
    """A persistent lock file whose inode is never replaced or removed.

    Removing it on release would let a process that had already opened the old inode
    and one that created a new file both hold the lock. There is one file per
    output directory, so the scheduled run reuses the same one (#1119).
    """

    def __init__(self, file_descriptor: int) -> None:
        self._file_descriptor: int | None = file_descriptor

    @classmethod
    def try_acquire(cls, path: Path, *, create: bool) -> _StableReportLock | None:
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
            raise _StableLockBusy from exc
        return

    import fcntl

    try:
        fcntl.flock(file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            raise _StableLockBusy from exc
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


@dataclass(frozen=True)
class WeeklyReportArtifact:
    name: str
    kind: str
    required_for: frozenset[str] = frozenset()
    date_fields: tuple[str, ...] = ()
    shape: str = "any"


# The run-local copy of Candidate History a weekly run writes beside its
# report. It is a run artifact, not canonical state: canonical Candidate
# History is read and written only through the State Store (#1071), and a
# module that needs this copy names it with this constant.
RUN_HISTORY_COPY = "candidate-history.json"


def keeps_run_history_copy(output_dir: Path, state_dir: Path | None, store: StateStore | None = None) -> bool:
    """Whether the run writes its copy of Candidate History beside the report.

    It does, except when the report directory is the state directory and the
    State Store keeps Candidate History there one file per book (#1073). The
    copy and the canonical history are then the same thing, and a single
    file beside the shards would hold Candidate History twice in one state
    directory, which the store refuses, and would put the whole history back
    into every commit. In the single-file layout the copy is that file, as
    before, when the JSON backend keeps it; a *store* of another backend
    writes that file when it exports, so the run writes no copy there (#1074).

    A run without a state directory whose report directory already holds
    the shards keeps no copy either: its history goes to those shards, as it
    went to the single file there before (``history_in_output_shards``).
    """
    if state_dir is None:
        return not history_in_output_shards(output_dir, state_dir)
    try:
        same_directory = output_dir.resolve() == state_dir.resolve()
    except OSError:
        same_directory = False
    if not same_directory:
        return True
    return is_single_file_layout(state_dir) and (store is None or keeps_json_in_state_dir(store))


def history_in_output_shards(output_dir: Path, state_dir: Path | None) -> bool:
    """Whether a run without a state directory writes its history to shards its report directory already holds."""
    return state_dir is None and (output_dir / CANDIDATE_HISTORY_DIR).is_dir() and not is_single_file_layout(output_dir)


def write_run_history(
    output_dir: Path, state_dir: Path | None, history: list[Any], store: StateStore | None = None
) -> None:
    """Write the run's copy of Candidate History, or the shards of a report directory that holds them (#1073)."""
    if keeps_run_history_copy(output_dir, state_dir, store):
        write_json(output_dir / RUN_HISTORY_COPY, history)
    elif history_in_output_shards(output_dir, state_dir):
        from autoscout.state_store import Collection
        from autoscout.state_store import open_run_output_store

        open_run_output_store(output_dir).replace(Collection.CANDIDATE_HISTORY, history)


def read_run_history(output_dir: Path) -> Any:
    """The run's Candidate History for a run without state: its copy, ``[]`` when it has none.

    A report directory that is also a state directory in the sharded layout
    holds the history as shards and no copy (#1073); the shards are read
    then, as the single file was read before.
    """
    # A directory holding both layouts is refused, as the State Store refuses it.
    if is_single_file_layout(output_dir) or not (output_dir / CANDIDATE_HISTORY_DIR).is_dir():
        return read_json(output_dir / RUN_HISTORY_COPY, default=[])
    return read_sharded_records(output_dir)


# The run's pending Source Proposals, written beside the report; a run
# artifact like the history copy above.
RUN_SOURCE_PROPOSALS = "source-proposals.json"

# Operation evidence and canonical cross-week state are deliberately absent.
# Candidate History is included only as a run-local copy when present.
WEEKLY_REPORT_ARTIFACTS: tuple[WeeklyReportArtifact, ...] = (
    WeeklyReportArtifact("editor-facing.csv", "csv", frozenset({"feedback", "completed"}), shape="editor-csv"),
    WeeklyReportArtifact(
        "scouting-candidates.json", "json", frozenset({"renderable", "feedback", "completed"}), shape="list"
    ),
    WeeklyReportArtifact("source-mentions.json", "json", date_fields=("source_check_date", "run_date"), shape="list"),
    WeeklyReportArtifact(
        "source-runs.json",
        "json",
        frozenset({"health", "completed"}),
        date_fields=("source_check_date", "run_date"),
        shape="list",
    ),
    WeeklyReportArtifact(
        "run-summary.json", "json", frozenset({"health", "completed"}), date_fields=("run_date",), shape="object"
    ),
    WeeklyReportArtifact(
        "pilot-status.json", "json", frozenset({"health", "completed"}), date_fields=("run_date",), shape="object"
    ),
    WeeklyReportArtifact("description-coverage.json", "json", shape="object"),
    WeeklyReportArtifact("internal-exclusions.json", "json", shape="list"),
    WeeklyReportArtifact("suppressed-candidates.json", "json", shape="list"),
    WeeklyReportArtifact("internal-pre-suppression-candidates.json", "json", shape="list"),
    WeeklyReportArtifact("hidden-by-rule.json", "json", shape="list"),
    WeeklyReportArtifact("hidden-from-review.json", "json", shape="list"),
    WeeklyReportArtifact("memo-queue.json", "json", shape="list"),
    WeeklyReportArtifact("russian-review-queue.csv", "csv", shape="any"),
    WeeklyReportArtifact("russian-edition-checks.json", "json", shape="object"),
    WeeklyReportArtifact("source-metrics.json", "json", shape="object"),
    WeeklyReportArtifact("report-qa.json", "json", shape="object"),
    WeeklyReportArtifact("proposed-corrections.json", "json", shape="list"),
    WeeklyReportArtifact(RUN_SOURCE_PROPOSALS, "json", shape="list"),
    WeeklyReportArtifact("description-enrichment.json", "json", shape="object"),
    WeeklyReportArtifact("grounded-description-stage.json", "json", shape="object"),
    WeeklyReportArtifact("description-adjudication-stage.json", "json", shape="object"),
    WeeklyReportArtifact("description-adjudication-preview.json", "json", shape="object"),
    WeeklyReportArtifact("derived-rule-suggestions.json", "json", shape="object"),
    WeeklyReportArtifact("derived-rule-suggestions.md", "text"),
    WeeklyReportArtifact("source-review.md", "text"),
    WeeklyReportArtifact("search-optimization-review.json", "json", shape="object"),
    WeeklyReportArtifact("search-optimization-review.md", "text"),
    WeeklyReportArtifact("editor-review-guide.md", "text"),
    WeeklyReportArtifact("weekly-review.md", "text"),
    WeeklyReportArtifact("weekly-review.html", "text"),
    WeeklyReportArtifact(RUN_HISTORY_COPY, "json", shape="list"),
)

_ARTIFACTS_BY_NAME = {artifact.name: artifact for artifact in WEEKLY_REPORT_ARTIFACTS}


@dataclass(frozen=True)
class ArtifactCheck:
    name: str
    present: bool
    valid: bool
    reason: str = ""
    run_dates: tuple[str, ...] = ()


@dataclass(frozen=True)
class WeeklyReportSnapshot:
    """Coherent, captured interpretation of the report files at one instant."""

    output_dir: Path
    purpose: str
    effective_date: str | None
    artifacts: dict[str, ArtifactCheck]
    values: dict[str, Any]
    raw_values: dict[str, bytes]
    transaction_in_progress: bool = False
    read_inconsistent: bool = False

    @property
    def coherent(self) -> bool:
        if self.transaction_in_progress or self.read_inconsistent:
            return False
        return all(check.valid for check in self.artifacts.values())

    def json(self, name: str, default: Any = None) -> Any:
        self._check_kind(name, "json")
        return deepcopy(self.values.get(name, default))

    def csv(self, name: str, default: Any = None) -> Any:
        self._check_kind(name, "csv")
        return deepcopy(self.values.get(name, default))

    def text(self, name: str, default: str = "") -> str:
        self._check_kind(name, "text")
        value = self.values.get(name, default)
        return str(value)

    def bytes(self, name: str, default: bytes | None = None) -> bytes | None:
        if name not in _ARTIFACTS_BY_NAME:
            raise ValueError(f"Unknown Weekly Report artifact: {name}")
        value = self.raw_values.get(name, default)
        return bytes(value) if value is not None else None

    @staticmethod
    def _check_kind(name: str, expected: str) -> None:
        artifact = _ARTIFACTS_BY_NAME.get(name)
        if artifact is None:
            raise ValueError(f"Unknown Weekly Report artifact: {name}")
        if artifact.kind != expected:
            raise TypeError(f"Weekly Report artifact '{name}' is not {expected}")

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(name for name, check in self.artifacts.items() if not check.present)

    @property
    def malformed(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, check in self.artifacts.items()
            if check.present and not check.valid and check.reason != "date_inconsistent"
        )

    @property
    def date_inconsistent(self) -> tuple[str, ...]:
        return tuple(name for name, check in self.artifacts.items() if check.reason == "date_inconsistent")

    def is_valid_for(self, purpose: str | None = None) -> bool:
        """Check one purpose without changing the stored interpretation."""
        if purpose is None or purpose == self.purpose:
            return self.coherent
        return WeeklyReportStore(self.output_dir).snapshot(purpose, self.effective_date).coherent

    def path(self, name: str) -> Path:
        return WeeklyReportStore(self.output_dir).path(name)


class WeeklyReportTransactionInProgress(RuntimeError):
    """Raised when a reader sees an unfinished multi-file report update."""


class WeeklyReportStore:
    """Own report names, validation, coherent reads, and allowed writes."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir

    def path(self, name: str) -> Path:
        artifact = _ARTIFACTS_BY_NAME.get(name)
        if artifact is None:
            raise ValueError(f"Unknown Weekly Report artifact: {name}")
        return self.output_dir / artifact.name

    def transaction_marker_path(self) -> Path:
        return self.output_dir / TRANSACTION_MARKER

    def transaction_lock_path(self) -> Path:
        """Return the stable lock path without creating any filesystem entry."""
        resolved_output_dir = self.output_dir.resolve()
        directory_key = hashlib.sha256(os.fsencode(str(resolved_output_dir))).hexdigest()
        return Path(tempfile.gettempdir()) / _TRANSACTION_LOCK_DIRECTORY / f"{directory_key}.lock"

    def transaction_in_progress(self) -> bool:
        if self.transaction_marker_path().is_file():
            return True
        try:
            stable_lock = _StableReportLock.try_acquire(self.transaction_lock_path(), create=False)
        except _StableLockBusy:
            return True
        except OSError:
            return True
        if stable_lock is None:
            return False
        try:
            stable_lock.release()
        except OSError:
            return True
        return False

    @contextmanager
    def transaction(self, purpose: str = "feedback") -> Iterator[None]:
        """Guard a multi-file update with a hidden marker and stable lock.

        Every individual write is atomic.  If the process stops after the
        marker is created, readers reject the directory instead of exposing
        a partial report set.  The stable lock is persistent and remains at
        the same path for the lifetime of the output directory identity, so
        a competing transaction cannot replace the visible marker while it
        is being verified and removed.
        """
        self.output_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.transaction_lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(lock_path.parent, 0o700)
        except OSError:
            pass

        try:
            stable_lock = _StableReportLock.try_acquire(lock_path, create=True)
        except _StableLockBusy as exc:
            raise WeeklyReportTransactionInProgress("Weekly Report transaction is in progress") from exc
        if stable_lock is None:
            raise WeeklyReportTransactionInProgress("Weekly Report transaction is in progress")

        try:
            marker = self.transaction_marker_path()
            owner_token = self._create_transaction_marker(marker, purpose)
            try:
                yield
            except BaseException:
                raise
            else:
                self._remove_owned_transaction_marker(marker, owner_token, purpose)
        finally:
            stable_lock.release()

    @staticmethod
    def _create_transaction_marker(marker: Path, purpose: str) -> str:
        """Create a marker before its contents are written, without a race."""
        owner_token = uuid.uuid4().hex
        try:
            file_descriptor = os.open(
                marker,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError as exc:
            raise WeeklyReportTransactionInProgress("Weekly Report transaction is in progress") from exc

        payload = {"purpose": purpose, "status": "in-progress", "format": 1, "owner_token": owner_token}
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        return owner_token

    @staticmethod
    def _remove_owned_transaction_marker(marker: Path, owner_token: str, purpose: str) -> None:
        """Remove only the marker still owned by this successful transaction."""
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return
        if (
            not isinstance(payload, dict)
            or payload.get("format") != 1
            or payload.get("purpose") != purpose
            or payload.get("status") != "in-progress"
            or payload.get("owner_token") != owner_token
        ):
            return
        try:
            marker.unlink()
        except FileNotFoundError:
            return

    def write_json(self, name: str, value: Any) -> None:
        write_json(self.path(name), value)

    def write_text(self, name: str, value: str) -> None:
        self._write_bytes(name, value.encode("utf-8"))

    def write_bytes(self, name: str, value: bytes) -> None:
        self._write_bytes(name, value)

    def write_editor_csv(self, rows: list[dict[str, str]]) -> None:
        handle = StringIO(newline="")
        writer = csv.DictWriter(handle, fieldnames=editor_csv_fieldnames(rows), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        self.write_text("editor-facing.csv", handle.getvalue())

    def _write_bytes(self, name: str, value: bytes) -> None:
        path = self.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
            ) as handle:
                temp_name = handle.name
                handle.write(value)
            os.replace(temp_name, path)
        finally:
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)

    def read(self, name: str, default: Any = None) -> Any:
        if self.transaction_in_progress():
            raise WeeklyReportTransactionInProgress("Weekly Report transaction is in progress")
        path = self.path(name)
        artifact = _ARTIFACTS_BY_NAME[name]
        if not path.is_file():
            return default
        if artifact.kind == "json":
            return read_json(path, default=default)
        if artifact.kind == "csv":
            with path.open("r", encoding="utf-8", newline="") as handle:
                return list(csv.DictReader(handle))
        return path.read_text(encoding="utf-8")

    def snapshot(self, purpose: str = "completed", effective_date: str | None = None) -> WeeklyReportSnapshot:
        if purpose not in {"renderable", "health", "completed", "feedback"}:
            raise ValueError(f"Unknown Weekly Report purpose: {purpose}")

        marker_before = self.transaction_in_progress()
        signatures_before, signature_error_before = self._catalog_signatures()
        values: dict[str, Any] = {}
        raw_values: dict[str, bytes] = {}
        for artifact in WEEKLY_REPORT_ARTIFACTS:
            path = self.output_dir / artifact.name
            if not path.is_file():
                continue
            try:
                raw = path.read_bytes()
            except (OSError, UnicodeError):
                raw_values[artifact.name] = b""
                values[artifact.name] = None
                continue
            raw_values[artifact.name] = raw
            try:
                values[artifact.name] = _parse_value(artifact, raw)
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                values[artifact.name] = None

        signatures_after, signature_error_after = self._catalog_signatures()
        read_inconsistent = signature_error_before or signature_error_after or signatures_before != signatures_after

        run_summary = values.get("run-summary.json")
        derived_date = effective_date
        date_was_derived = effective_date is None
        if date_was_derived and isinstance(run_summary, dict):
            run_date = str(run_summary.get("run_date", "")).strip()
            derived_date = run_date or None

        checks: dict[str, ArtifactCheck] = {}
        for artifact in WEEKLY_REPORT_ARTIFACTS:
            if artifact.name not in raw_values:
                if purpose in artifact.required_for:
                    checks[artifact.name] = ArtifactCheck(artifact.name, False, False, "missing")
                continue
            checks[artifact.name] = self._validate_captured(
                artifact,
                raw_values[artifact.name],
                values.get(artifact.name),
                derived_date,
                require_derived_dates=date_was_derived,
            )

        marker_after = self.transaction_in_progress()
        return WeeklyReportSnapshot(
            self.output_dir,
            purpose,
            derived_date,
            checks,
            values,
            raw_values,
            marker_before or marker_after,
            read_inconsistent,
        )

    def _catalog_signatures(self) -> tuple[dict[str, tuple[int, int, int, int]], bool]:
        signatures: dict[str, tuple[int, int, int, int]] = {}
        had_error = False
        for artifact in WEEKLY_REPORT_ARTIFACTS:
            path = self.output_dir / artifact.name
            try:
                stat_result = path.stat()
            except FileNotFoundError:
                continue
            except OSError:
                had_error = True
                continue
            if path.is_file():
                signatures[artifact.name] = _stat_signature(stat_result)
        return signatures, had_error

    def date_errors(
        self,
        effective_date: str | None = None,
        snapshot: WeeklyReportSnapshot | None = None,
    ) -> list[str]:
        """Return deterministic date diagnostics for one captured snapshot."""
        errors: list[str] = []
        snapshot = snapshot or self.snapshot("health", effective_date)
        explicit_date = effective_date is not None
        expected_date = effective_date or snapshot.effective_date
        if not expected_date:
            return errors
        effective_date = expected_date
        run_summary = snapshot.json("run-summary.json", {})
        pilot_status = snapshot.json("pilot-status.json", {})
        source_runs = snapshot.json("source-runs.json", [])
        source_mentions = snapshot.json("source-mentions.json", [])
        candidates = snapshot.json("scouting-candidates.json", [])
        if isinstance(run_summary, dict):
            value = str(run_summary.get("run_date", ""))
            if value and value != effective_date:
                errors.append(f"run_summary run_date={value} expected={effective_date}")
            elif not value:
                errors.append("run_summary missing run_date")
        if isinstance(pilot_status, dict):
            value = str(pilot_status.get("run_date", ""))
            if value and value != effective_date:
                errors.append(f"pilot_status run_date={value} expected={effective_date}")
            elif not value:
                errors.append("pilot_status missing run_date")
        if isinstance(source_runs, list):
            for source in source_runs:
                if not isinstance(source, dict):
                    continue
                value = str(source.get("source_check_date", ""))
                name = source.get("source_name", "?")
                if not value:
                    if explicit_date:
                        errors.append(f"source-runs {name} source_check_date is blank")
                elif value != effective_date:
                    errors.append(f"source-runs {name} source_check_date={value} expected={effective_date}")
        if isinstance(source_mentions, list):
            for mention in source_mentions:
                if not isinstance(mention, dict):
                    continue
                value = str(mention.get("source_check_date", ""))
                title = mention.get("title", "?")
                if not value:
                    if explicit_date:
                        errors.append(f"source-mentions {title} source_check_date is blank")
                elif value != effective_date:
                    errors.append(f"source-mentions {title} source_check_date={value} expected={effective_date}")
        if isinstance(candidates, list):
            for candidate in candidates:
                if not isinstance(candidate, dict) or str(candidate.get("update_delta", "")).lower() != "new":
                    continue
                provenance = candidate.get("description_provenance")
                if not isinstance(provenance, dict):
                    continue
                if str(provenance.get("provenance", "")) not in {"codex-generated", "fallback"}:
                    continue
                applied_date = str(provenance.get("applied_date", ""))
                if (applied_date and applied_date != effective_date) or (explicit_date and not applied_date):
                    identity_key = str(candidate.get("identity_key", "?"))
                    errors.append(
                        f"scouting-candidates {identity_key} description applied_date={applied_date or 'blank'} "
                        f"expected={effective_date}"
                    )
        return errors

    def _validate_captured(
        self,
        artifact: WeeklyReportArtifact,
        raw: bytes,
        value: Any,
        effective_date: str | None,
        *,
        require_derived_dates: bool = False,
    ) -> ArtifactCheck:
        if value is None:
            return ArtifactCheck(artifact.name, True, False, "malformed")
        if artifact.kind == "json":
            if artifact.shape == "object" and not isinstance(value, dict):
                return ArtifactCheck(artifact.name, True, False, "malformed")
            if artifact.shape == "list" and not isinstance(value, list):
                return ArtifactCheck(artifact.name, True, False, "malformed")
            if (
                artifact.shape == "list"
                and isinstance(value, list)
                and any(not isinstance(item, dict) for item in value)
            ):
                return ArtifactCheck(artifact.name, True, False, "malformed")
            if artifact.name == "pilot-status.json" and (
                not isinstance(value, dict) or "ready_for_editor" not in value
            ):
                return ArtifactCheck(artifact.name, True, False, "malformed")
            if artifact.name == "run-summary.json" and (
                not isinstance(value, dict) or not ({"editor_facing_count", "raw_candidate_count"} & set(value))
            ):
                return ArtifactCheck(artifact.name, True, False, "malformed")
            dates = _dates_in_json(artifact, value)
            unique_dates = tuple(sorted(set(dates)))
            if (
                require_derived_dates
                and effective_date is not None
                and artifact.name in {"run-summary.json", "pilot-status.json"}
                and not dates
            ):
                return ArtifactCheck(artifact.name, True, False, "date_inconsistent", unique_dates)
            if effective_date is not None and artifact.date_fields and isinstance(value, list) and value:
                # Missing dates remain readable for legacy Doctor fixtures.
                # Any dates that are present must still agree with the
                # canonical report date.
                if any(not isinstance(item, dict) for item in value):
                    return ArtifactCheck(artifact.name, True, False, "malformed", unique_dates)
            if effective_date is not None and dates and set(dates) != {effective_date}:
                return ArtifactCheck(artifact.name, True, False, "date_inconsistent", unique_dates)
            if effective_date is not None and artifact.name in {"run-summary.json", "pilot-status.json"}:
                if dates and set(dates) != {effective_date}:
                    return ArtifactCheck(artifact.name, True, False, "date_inconsistent", unique_dates)
            return ArtifactCheck(artifact.name, True, True, "", unique_dates)
        if artifact.kind == "csv":
            try:
                reader = csv.DictReader(StringIO(raw.decode("utf-8")))
                headers = reader.fieldnames
                rows = list(reader)
            except (UnicodeError, csv.Error, ValueError):
                return ArtifactCheck(artifact.name, True, False, "malformed")
            header_tuple = tuple(headers or ())
            allowed_headers = {*EDITOR_FIELDNAMES, *OPTIONAL_EDITOR_FIELDNAMES}
            if artifact.shape == "editor-csv" and (
                header_tuple[: len(EDITOR_FIELDNAMES)] != tuple(EDITOR_FIELDNAMES)
                or len(set(header_tuple)) != len(header_tuple)
                or any(header not in allowed_headers for header in header_tuple)
                or any(None in row for row in rows)
            ):
                return ArtifactCheck(artifact.name, True, False, "malformed")
            return ArtifactCheck(artifact.name, True, True)
        return ArtifactCheck(artifact.name, True, True)


def _stat_signature(stat_result: os.stat_result) -> tuple[int, int, int, int]:
    return (stat_result.st_ino, stat_result.st_size, stat_result.st_mtime_ns, stat_result.st_ctime_ns)


def _parse_value(artifact: WeeklyReportArtifact, raw: bytes) -> Any:
    if artifact.kind == "json":
        return json.loads(raw.decode("utf-8"))
    if artifact.kind == "csv":
        return list(csv.DictReader(StringIO(raw.decode("utf-8"))))
    return raw.decode("utf-8")


def _dates_in_json(artifact: WeeklyReportArtifact, value: Any) -> list[str]:
    if not artifact.date_fields:
        return []
    records = value if isinstance(value, list) else [value]
    dates: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        for field_name in artifact.date_fields:
            value_text = str(record.get(field_name, "")).strip()
            if value_text:
                dates.append(value_text)
    return dates


def report_snapshot(
    output_dir: Path,
    purpose: str = "completed",
    effective_date: str | None = None,
) -> WeeklyReportSnapshot:
    """Read a coherent report snapshot through the public store interface."""
    return WeeklyReportStore(output_dir).snapshot(purpose, effective_date)


def artifact_path(output_dir: Path, name: str) -> Path:
    """Resolve one catalogued report artifact path."""
    return WeeklyReportStore(output_dir).path(name)
