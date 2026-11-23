"""Feedback finalization: consume confirmed conversational feedback in-place.

When safe finalization conditions are met — matching snapshot date, no new
factual corrections, no source re-collection — this module applies pending
editor decisions without recomputing the weekly snapshot.

Return values from `attempt_finalization`:
    - ``"applied"`` — feedback was consumed and artifacts updated.
    - ``"noop"`` — no pending feedback to consume and marker matches.
    - ``"skipped"`` — conditions not met; fall through to full pipeline.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from autoscout.editor_decisions import DECISION_LOG_FIELD
from autoscout.editor_decisions import DECISION_ROW_COLUMNS
from autoscout.editor_decisions import record_returned_decisions
from autoscout.editor_feedback import EDITOR_STATUSES
from autoscout.feedback_dimensions import dimensions_from_row
from autoscout.feedback_dimensions import has_specified_dimensions
from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.sort_utils import sort_by_key
from autoscout.state_store import LEGACY_DIGEST_LABELS
from autoscout.state_store import Collection
from autoscout.state_store import StateStore
from autoscout.state_store import open_state_store
from autoscout.weekly_report import RUN_HISTORY_COPY
from autoscout.weekly_report import WeeklyReportSnapshot
from autoscout.weekly_report import WeeklyReportStore
from autoscout.weekly_report import keeps_run_history_copy

# Columns that are purely editorial decisions — their presence alone does
# not disqualify safe finalization.  Any other column (e.g. Title, Author,
# Russian Edition Status) requires the full pipeline.
_DECISION_ONLY_COLUMNS = frozenset(
    {"identity_key", "Editor Status", "Editor Note", "Feedback Dimensions", *DECISION_ROW_COLUMNS}
)

# Canonical collections whose content (minus editor decisions and feedback
# bookkeeping) must be fingerprinted for the no-op/marker guard, in guard
# order, each with the label it is framed under. The labels are the store's
# legacy digest labels, the names of the files the guard hashed before the
# State Store (#1069), and are only hash input: a marker sealed by an earlier
# version then still matches unchanged state (see the known limit in
# ``_state_guard_digest``).
_GUARDED_COLLECTIONS = tuple(
    (collection, LEGACY_DIGEST_LABELS[collection])
    for collection in (
        Collection.CANDIDATE_HISTORY,
        Collection.LATEST_SCOUTING_CANDIDATES,
        Collection.CANDIDATE_VISIBILITY,
        Collection.EDITORIAL_PROFILE,
        Collection.SOURCE_PROPOSALS,
        Collection.EXCLUDED_CANDIDATE_HISTORY,
    )
)

# Per-record fields to strip from state-guard digests so that feedback
# finalization remains stable after it consumed pending rows.
_STRIP_FROM_STATE_GUARD = frozenset(
    {"editor_status", "editor_note", "memo_queue_marker", "feedback_dimensions", DECISION_LOG_FIELD}
)


def _snapshot_digest(output_dir: Path, snapshot: WeeklyReportSnapshot | None = None) -> str | None:
    """Derive a deterministic, byte-stable digest of the current snapshot.

    Returns ``None`` when the snapshot artifacts are not present.
    The digest covers source-mentions.json (canonical source evidence)
    and scouting-candidates.json (excluding editor_status/editor_note to
    remain stable after feedback).

    Pure Python — no subprocesses, no timestamps, no random values.
    """
    hasher = hashlib.sha256()
    if snapshot is not None:
        candidates = snapshot.json("scouting-candidates.json", None)
        mentions_bytes = snapshot.bytes("source-mentions.json")
        if not isinstance(candidates, list):
            return None
        if mentions_bytes is not None:
            hasher.update(mentions_bytes)
    else:
        scouting_path = output_dir / "scouting-candidates.json"
        mentions_path = output_dir / "source-mentions.json"
        if not scouting_path.is_file():
            return None
        if mentions_path.is_file():
            hasher.update(mentions_path.read_bytes())
        # Digest scouting-candidates without editor-decision fields so the
        # digest is stable after feedback is consumed.
        candidates = read_json(scouting_path, default=[])
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            stripped = {
                key: value
                for key, value in candidate.items()
                if key not in ("editor_status", "editor_note", "memo_queue_marker", "feedback_dimensions")
            }
            hasher.update(json.dumps(stripped, sort_keys=True, ensure_ascii=False).encode())
    return hasher.hexdigest()


def _state_guard_digest(store: StateStore) -> str | None:
    """Derive a deterministic digest over non-feedback state inputs.

    Covers candidate history, latest candidates, visibility, editorial
    taste profile, source proposals, and excluded candidate history.
    Strips editor_status/editor_note from each record so that feedback
    finalization itself does not invalidate the guard.

    Every guarded collection is framed, empty or not, under the label of
    the file it was kept in before the State Store (#1069). The guard then
    equals the one the previous version computed whenever every guarded file
    existed, which a weekly run ensures for the record collections and which
    holds for any recorded Editorial Taste Profile, empty or not; the real
    state keeps ``editorial-profile.json`` with entries.

    Known limit, accepted: the store cannot tell a missing collection from
    an empty one, nor which file name holds the profile. For state whose
    profile file never existed, or whose profile is still in the legacy
    ``editorial-taste-profile.json``, a marker sealed by the previous version
    stops matching once after the upgrade. Its only effect is that one repeat
    invocation is classified ``scouted`` instead of ``feedback-noop``; the
    marker written by that run matches from then on.

    Returns ``None`` only when a collection cannot be read: without a guard,
    finalization falls through to the full pipeline. Empty state has a guard,
    so a completed run with nothing in it is sealed like any other.
    """
    hasher = hashlib.sha256()
    for collection, label in _GUARDED_COLLECTIONS:
        try:
            data = store.read(collection)
        except ValueError:
            return None
        if isinstance(data, list):
            framed: Any = [
                {key: value for key, value in record.items() if key not in _STRIP_FROM_STATE_GUARD}
                if isinstance(record, dict)
                else record
                for record in data
            ]
        else:
            framed = data
        # Frame every collection independently, including empty ones.  This
        # prevents equal records moved between collections from producing the
        # same guard.
        payload = json.dumps(framed, sort_keys=True, ensure_ascii=False, default=str).encode()
        name = label.encode()
        hasher.update(len(name).to_bytes(4, "big"))
        hasher.update(name)
        hasher.update(len(payload).to_bytes(8, "big"))
        hasher.update(payload)
    return hasher.hexdigest()


def _marker_path(output_dir: Path) -> Path:
    return output_dir / ".feedback-finalization-marker.json"


def _read_marker(output_dir: Path) -> dict[str, Any] | None:
    """Read the feedback-finalization no-op marker if present."""
    path = _marker_path(output_dir)
    data = read_json(path, default=None)
    if isinstance(data, dict):
        return data
    return None


def _write_marker(output_dir: Path, snapshot_digest: str, state_guard: str) -> None:
    """Persist a deterministic no-op marker for idempotency."""
    write_json(
        _marker_path(output_dir),
        {
            "snapshot_digest": snapshot_digest,
            "state_guard": state_guard,
            "finalization_version": 1,
        },
    )


def _marker_matches(output_dir: Path, snapshot_digest: str, state_guard: str | None) -> bool:
    """Return True when the marker matches both snapshot and state guard."""
    marker = _read_marker(output_dir)
    return bool(
        marker and marker.get("snapshot_digest") == snapshot_digest and marker.get("state_guard") == state_guard
    )


def _pending_rows_are_decision_only(pending: list[dict[str, Any]]) -> bool:
    """Return True when every pending row contains only decision columns."""
    for row in pending:
        if not isinstance(row, dict):
            return False
        for key in row:
            if key not in _DECISION_ONLY_COLUMNS:
                return False
    return True


_SM_DATE_UNAVAILABLE = "unavailable"  # absent or empty artifact
_SM_DATE_UNIFORM = "uniform"  # single unambiguous date
_SM_DATE_MIXED = "mixed"  # multiple different dates present
_SM_DATE_UNIFORM_EMPTY = "uniform-empty"  # all dates blank (degenerate)


def _source_mentions_date_status(snapshot: WeeklyReportSnapshot | Path) -> tuple[str, str]:
    """Classify the source-mentions.json date evidence.

    Returns ``(status, date)`` where *status* is one of:

    - ``"unavailable"`` — file absent, not a list, or empty.
    - ``"uniform"`` — exactly one nonblank date across all mentions.
    - ``"mixed"`` — multiple different nonblank dates.
    - ``"uniform-empty"`` — list is non-empty but every mention has a
      blank source_check_date and run_date.
    """
    if isinstance(snapshot, Path):
        mentions = WeeklyReportStore(snapshot).snapshot("feedback").json("source-mentions.json", None)
    else:
        mentions = snapshot.json("source-mentions.json", None)
    if not isinstance(mentions, list) or not mentions:
        return (_SM_DATE_UNAVAILABLE, "")

    dates: set[str] = set()
    for mention in mentions:
        if not isinstance(mention, dict):
            continue
        for field in ("source_check_date", "run_date"):
            date_str = str(mention.get(field, "")).strip()
            if date_str:
                dates.add(date_str)

    if not dates:
        return (_SM_DATE_UNIFORM_EMPTY, "")
    if len(dates) == 1:
        return (_SM_DATE_UNIFORM, dates.pop())
    return (_SM_DATE_MIXED, "")


def _candidate_run_date_matches(snapshot: WeeklyReportSnapshot, config_run_date: str) -> bool:
    """Check that the stored source-mentions evidence date matches the config run_date.

    - If source-mentions.json is present and non-empty, its dates must be
      unambiguous and match config_run_date.  Mixed dates → immediate False.
    - If source-mentions.json is absent/empty (genuinely unavailable), fall
      back to run-summary.json, then scouting-candidates.json evidence dates.
      Those fallback paths also reject mixed dates.
    """
    status, date_value = _source_mentions_date_status(snapshot)

    if status == _SM_DATE_UNIFORM:
        return date_value == config_run_date

    if status == _SM_DATE_MIXED:
        # Artifact present but corrupted/mixed → reject immediately.
        return False

    if status == _SM_DATE_UNIFORM_EMPTY:
        # All dates blank — treat as unavailable for date purposes.
        pass

    # status is "unavailable" or "uniform-empty" → fall back.

    # 2. Fallback: check run-summary.json.
    run_summary = snapshot.json("run-summary.json", {})
    if isinstance(run_summary, dict) and str(run_summary.get("run_date", "")) == config_run_date:
        return True

    # 3. Last-resort fallback: scouting-candidates.json source_evidence dates.
    # Only used when source-mentions cannot establish a date.
    candidates = snapshot.json("scouting-candidates.json", [])
    if not isinstance(candidates, list):
        return False
    dates: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        evidence_list = candidate.get("source_evidence", [])
        if not isinstance(evidence_list, list):
            continue
        for evidence in evidence_list:
            if not isinstance(evidence, dict):
                continue
            evidence_date = str(
                evidence.get("source_check_date", "") or evidence.get("run_date", "") or evidence.get("check_date", "")
            ).strip()
            if evidence_date:
                dates.add(evidence_date)
    if len(dates) == 1:
        return dates.pop() == config_run_date
    return False


def _pending_has_decisions(pending: list[dict[str, Any]]) -> bool:
    """Return True when at least one pending row carries an actual decision:
    an Editor Status, an Editor Note, or any specified structured dimension."""
    for row in pending:
        if not isinstance(row, dict):
            continue
        if row.get("Editor Status") or row.get("Editor Note"):
            return True
        dimensions = dimensions_from_row(row)
        if dimensions is not None and has_specified_dimensions(dimensions):
            return True
    return False


def _apply_feedback_to_candidates(
    candidates: list[dict[str, Any]],
    pending_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Apply editor_status and editor_note to scouting-candidates in-place.

    Returns the (possibly modified) candidate list — same ordering preserved.
    """
    for candidate in candidates:
        identity_key = str(candidate.get("identity_key", "")).strip()
        pending = pending_by_id.get(identity_key)
        if pending is None:
            continue
        editor_status = str(pending.get("Editor Status", "")).strip()
        if editor_status in EDITOR_STATUSES:
            candidate["editor_status"] = editor_status
        if "Editor Note" in pending:
            candidate["editor_note"] = str(pending.get("Editor Note", ""))
        dimensions = dimensions_from_row(pending)
        if dimensions is not None:
            candidate["feedback_dimensions"] = dimensions
        candidate["memo_queue_marker"] = (
            "needs_deeper_analysis" if candidate.get("editor_status") == "needs_deeper_analysis" else ""
        )
    return candidates


def _apply_feedback_to_history(
    history: list[dict[str, Any]],
    pending_by_id: dict[str, dict[str, Any]],
    rows_by_id: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Apply editor_status and editor_note to candidate-history records in-place.

    With ``rows_by_id`` (every pending row per identity, in order) each
    record also logs its decisions (issue #1053); only Candidate History
    keeps that log.

    Returns the same list object — ordering unchanged.
    """
    for record in history:
        identity_key = str(record.get("identity_key", "")).strip()
        pending = pending_by_id.get(identity_key)
        if pending is None:
            continue
        previous_status = str(record.get("editor_status", "") or "")
        previous_note = str(record.get("editor_note", "") or "")
        editor_status = str(pending.get("Editor Status", "")).strip()
        if editor_status in EDITOR_STATUSES:
            record["editor_status"] = editor_status
        if "Editor Note" in pending:
            record["editor_note"] = str(pending.get("Editor Note", ""))
        dimensions = dimensions_from_row(pending)
        if dimensions is not None:
            record["feedback_dimensions"] = dimensions
        record["memo_queue_marker"] = (
            "needs_deeper_analysis" if record.get("editor_status") == "needs_deeper_analysis" else ""
        )
        if rows_by_id is not None:
            record_returned_decisions(record, previous_status, previous_note, rows_by_id.get(identity_key, [pending]))
    return history


def _archive_and_clear_pending(state_dir: Path, store: StateStore, pending: list[dict[str, Any]]) -> None:
    """Move pending rows to the archive, with a retry-safe transaction record."""
    transaction_path = state_dir / ".feedback-archive-transaction.json"
    pending_digest = hashlib.sha256(json.dumps(pending, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    transaction = read_json(transaction_path, default={})
    if isinstance(transaction, dict) and transaction.get("pending_digest") == pending_digest:
        desired_archive = transaction.get("desired_archive")
    else:
        archive = store.read(Collection.ARCHIVED_EDITOR_ROWS)
        if not isinstance(archive, list):
            archive = []
        desired_archive = sort_by_key([*archive, *pending], "identity_key")
        write_json(
            transaction_path,
            {"pending_digest": pending_digest, "desired_archive": desired_archive},
        )
    if not isinstance(desired_archive, list):
        raise ValueError("invalid feedback archive transaction")
    with store.transaction():
        store.replace(Collection.ARCHIVED_EDITOR_ROWS, desired_archive)
        store.replace(Collection.PENDING_EDITOR_ROWS, [])
    transaction_path.unlink(missing_ok=True)


def _write_editor_csv_patch(
    output_dir: Path,
    rows: list[dict[str, str]],
    fieldnames: list[str],
    pending_by_id: dict[str, dict[str, Any]],
) -> None:
    """Patch editor-facing.csv in-place: preserve row set, ordering, and
    every non-editor field.  Update only ``Editor status`` and ``Editor note``
    for Candidate IDs in *pending_by_id*.

    Hidden candidates (absent from the original row set) stay absent.

    Writes atomically via a temp file + rename so a failed write cannot
    truncate the existing CSV.
    """
    csv_path = WeeklyReportStore(output_dir).path("editor-facing.csv")

    for row in rows:
        candidate_id = str(row.get("Candidate ID", "") or "").strip()
        pending = pending_by_id.get(candidate_id)
        if pending is None:
            continue
        editor_status = str(pending.get("Editor Status", "")).strip()
        if editor_status in EDITOR_STATUSES:
            row["Editor status"] = editor_status
        if "Editor Note" in pending:
            row["Editor note"] = str(pending.get("Editor Note", ""))

    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=csv_path.parent,
            prefix=f".{csv_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_name = tmp.name
            writer = csv.DictWriter(tmp, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp_name, csv_path)
    finally:
        if tmp_name and os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _update_per_run_artifacts(
    output_dir: Path,
    candidates: list[dict[str, Any]],
    history: list[dict[str, Any]],
    pending_by_id: dict[str, dict[str, Any]],
    csv_rows: list[dict[str, str]],
    csv_fieldnames: list[str],
) -> None:
    """Update same-run artifacts to reflect editor decisions.

    Writes updated scouting-candidates.json and patches editor-facing.csv
    in-place (preserving row set, ordering, and non-editor fields).
    Does NOT touch source-mentions.json, source-runs.json, or other
    non-editorial artifacts.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    report_store = WeeklyReportStore(output_dir)
    report_store.write_json("scouting-candidates.json", candidates)
    _write_editor_csv_patch(output_dir, csv_rows, csv_fieldnames, pending_by_id)

    queue_path = report_store.path("memo-queue.json")
    existing_queue = read_json(queue_path, default=[])
    queue = existing_queue if isinstance(existing_queue, list) else []
    pending_ids = set(pending_by_id)
    queue = [
        row for row in queue if isinstance(row, dict) and str(row.get("identity_key", "")).strip() not in pending_ids
    ]
    history_by_id = {str(row.get("identity_key", "")).strip(): row for row in history}
    candidates_by_id = {str(row.get("identity_key", "")).strip(): row for row in candidates}
    for identity_key in sorted(pending_ids):
        record = candidates_by_id.get(identity_key) or history_by_id.get(identity_key)
        if record is None or record.get("editor_status") != "needs_deeper_analysis":
            continue
        queue.append(
            {
                "identity_key": identity_key,
                "title": str(record.get("title", "")),
                "editor_note": str(record.get("editor_note", "")),
                "status": "queued_for_future_memo",
            }
        )
    write_json(queue_path, sort_by_key(queue, "identity_key"))


def attempt_finalization(
    output_dir: Path,
    state_dir: Path | None,
    config_run_date: str,
    *,
    state_store: StateStore | None = None,
) -> str:
    """Try to finalize pending feedback in-place.

    Returns:
        ``"applied"`` — feedback was consumed, artifacts updated.
        ``"noop"`` — no pending feedback and marker matches; nothing to do.
        ``"skipped"`` — conditions not met; use full pipeline instead.

    Whether the stored report answers this run at all is the caller's question,
    not this function's: only the caller knows what evidence and settings it
    would scout with. ``stamps_permit_short_circuit`` decides that, and
    ``run_pipeline`` asks it before calling here (Issue #388).

    Canonical state goes through *state_store*, the run's store for
    *state_dir*; without one, a store is opened with the selected backend.
    *state_dir* keeps the archive's retry record.
    """
    if state_dir is None:
        return "skipped"
    store = state_store if state_store is not None else open_state_store(state_dir)

    report_store = WeeklyReportStore(output_dir)
    # Refuse a stale or active report transaction before reading state and
    # before any state mutation.
    if report_store.transaction_in_progress():
        return "skipped"
    snapshot = report_store.snapshot("feedback", config_run_date or None)
    required_names = {"scouting-candidates.json", "editor-facing.csv"}
    if (
        snapshot.transaction_in_progress
        or snapshot.read_inconsistent
        or not snapshot.coherent
        or any(name in snapshot.missing or name in snapshot.malformed for name in required_names)
    ):
        return "skipped"

    # ── Run date must match stored evidence ──
    if not config_run_date or not _candidate_run_date_matches(snapshot, config_run_date):
        return "skipped"

    # ── Pending feedback ──
    pending = store.read(Collection.PENDING_EDITOR_ROWS)
    if not isinstance(pending, list):
        pending = []

    # A transaction left after pending was successfully cleared is complete.
    # Remove it before no-op evaluation so a future identical event starts a
    # fresh transaction and remains a separate audit record.
    if not pending:
        (state_dir / ".feedback-archive-transaction.json").unlink(missing_ok=True)

    # No-op check: marker + state guard match AND no pending feedback.
    # NEVER short-circuit when pending feedback exists — new confirmed rows
    # must be consumed even if a previous finalization created a marker.
    digest = _snapshot_digest(output_dir, snapshot)
    state_guard = _state_guard_digest(store)
    if not pending and digest and _marker_matches(output_dir, digest, state_guard):
        return "noop"

    # ── Pending rows must be decision-only ──
    if not pending or not _pending_rows_are_decision_only(pending):
        if not pending:
            # No pending rows at all and marker doesn't match → skipped.
            return "skipped"
        # Factual corrections present → skipped.
        return "skipped"

    # ── At least one row must have an actual decision ──
    if not _pending_has_decisions(pending):
        return "skipped"

    # ── Compute snapshot digest for idempotency marker ──
    if not digest:
        return "skipped"

    if state_guard is None:
        return "skipped"

    # ══ State-guard validation: when a previous finalization recorded
    # a marker but non-feedback state (candidate visibility, editorial
    # taste profile, etc.) has since changed, fall through to the full
    # pipeline so the state change is properly applied to the weekly
    # snapshot.  Legacy snapshots with no marker are allowed through
    # (safe migration), but a known mismatched marker is never accepted.
    existing_marker = _read_marker(output_dir)
    if existing_marker is not None and not _marker_matches(output_dir, digest, state_guard):
        return "skipped"

    # ── Apply feedback ──

    # Build lookup
    pending_by_id: dict[str, dict[str, Any]] = {}
    rows_by_id: dict[str, list[dict[str, Any]]] = {}
    for row in pending:
        identity_key = str(row.get("identity_key", "")).strip()
        if identity_key:
            pending_by_id[identity_key] = row
            rows_by_id.setdefault(identity_key, []).append(row)

    if not pending_by_id:
        return "skipped"

    # ══ Validate outputs before any state or file mutation.
    # Centrally validate and load editor-facing.csv against the canonical
    # schema.  Reuse the parsed rows and fieldnames for the in-place patch
    # so we never re-read a CSV that was already validated.
    _csv_rows = snapshot.csv("editor-facing.csv", [])
    if not isinstance(_csv_rows, list) or not _csv_rows:
        return "skipped"
    # The validated snapshot can include optional enrichment columns. Keep
    # its actual header order instead of dropping those fields (or raising
    # from DictWriter after history has already been written).
    _csv_fieldnames = list(_csv_rows[0])

    # 1. Update scouting-candidates.json from the captured snapshot.
    candidates = snapshot.json("scouting-candidates.json", [])
    if not isinstance(candidates, list):
        return "skipped"
    _apply_feedback_to_candidates(candidates, pending_by_id)

    # 2-4. Prepare state changes in memory. All writes happen inside the
    # report transaction so readers fail closed while either side changes.
    history = store.read(Collection.CANDIDATE_HISTORY)
    if not isinstance(history, list):
        history = []
    _apply_feedback_to_history(history, pending_by_id, rows_by_id)

    output_history: list[dict[str, Any]] | None = None
    output_history_path = report_store.path(RUN_HISTORY_COPY)
    if keeps_run_history_copy(output_dir, state_dir, store) and output_history_path.is_file():
        loaded_output_history = read_json(output_history_path, default=[])
        if isinstance(loaded_output_history, list):
            output_history = loaded_output_history
            _apply_feedback_to_history(output_history, pending_by_id, rows_by_id)

    # A latest snapshot without records stays as it is.
    latest = store.read(Collection.LATEST_SCOUTING_CANDIDATES)
    if isinstance(latest, list) and latest:
        _apply_feedback_to_history(latest, pending_by_id)
    else:
        latest = None

    with report_store.transaction("feedback"):
        with store.transaction():
            store.replace(Collection.CANDIDATE_HISTORY, history)
            if latest is not None:
                store.replace(Collection.LATEST_SCOUTING_CANDIDATES, latest)
        if output_history is not None:
            write_json(output_history_path, output_history)
        _update_per_run_artifacts(output_dir, candidates, history, pending_by_id, _csv_rows, _csv_fieldnames)
        _archive_and_clear_pending(state_dir, store, pending)
        _write_marker(output_dir, digest, state_guard)

    return "applied"
