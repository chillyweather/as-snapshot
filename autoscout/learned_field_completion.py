"""The write envelope shared by passes that learn fields for Candidate History.

A completion pass spends minutes on network lookups, so what it learned must
reach Candidate History without erasing what the editor or another pass wrote
meanwhile (issue #968). This module holds that envelope, generalised over a
:class:`LearnedFieldSpec`: the learned field names, how a record that already
holds its value is recognised, how the learned value is copied onto a snapshot
row, and the pass's own report and pending-seal file names.

History is read again and merged fill-only, and written with the latest
Scouting Candidates in one State Store transaction, all under the report
directory's lock. A sealed feedback-finalization marker stays sealed,
with a pending note for interrupted retries and a fingerprint blind to the
learned fields. The learned fields are then projected onto the candidate
snapshots and a report file is written. Every step is a pure function of
history, so an interruption recovers by running the pass again.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autoscout.json_io import read_json_or_default
from autoscout.json_io import write_json
from autoscout.state_store import Collection
from autoscout.state_store import StateStore

SCOPE_CURRENT = "current"
SCOPE_UNDECIDED = "undecided"
# Every record, decided or not: a fact about the book, not a lead to triage (#1138).
SCOPE_ALL = "all"


@dataclass(frozen=True)
class LearnedFieldSpec:
    """What one completion pass learns, and how its envelope writes it.

    ``fields`` are the record fields the pass owns: they are the only fields
    merged into fresh history and the only ones the blind fingerprint leaves
    out. ``already_filled`` tells a fresh history record that already holds
    the learned value, so it is never overwritten, and ``copy_to_row`` copies
    the learned value and its provenance onto a snapshot row, fill-only.
    ``label`` names the pass in error messages; ``report_filename`` and
    ``seal_pending_filename`` are the files it writes in the output directory.
    ``project`` and ``seal`` are the envelope's two interruptible steps, named
    by the pass itself so they resolve through its own module: ``project``
    copies the learned fields from history onto snapshot rows and returns the
    rows with the count it copied, and ``seal`` reseals the feedback marker
    over the store.
    ``merge_record``, when set, replaces the generic field loop of
    :func:`merge_into_fresh_history`: it receives the fresh history record and
    the completed source record, and decides what to write. The pass owns that
    decision, so a field it does not set is never erased.
    """

    fields: tuple[str, ...]
    already_filled: Callable[[Mapping[str, Any]], bool]
    copy_to_row: Callable[[Mapping[str, Any], dict[str, Any]], bool]
    report_filename: str
    seal_pending_filename: str
    label: str
    project: Callable[
        [Sequence[dict[str, Any]], Sequence[Mapping[str, Any]], Mapping[str, Any] | None],
        tuple[list[dict[str, Any]], int],
    ]
    seal: Callable[[Path, StateStore], None]
    merge_record: Callable[[dict[str, Any], Mapping[str, Any]], None] | None = None


def load_aliases(store: StateStore) -> Mapping[str, Any]:
    """The candidate identity alias table, or an empty one when it cannot be read."""
    from autoscout.candidate_identity import empty_alias_table

    try:
        aliases: Mapping[str, Any] = store.read(Collection.CANDIDATE_IDENTITY_ALIASES)
    except (OSError, ValueError):
        return empty_alias_table()
    return aliases


def read_history(store: StateStore) -> list[dict[str, Any]] | None:
    """Candidate History as the store holds it, or ``None`` when it is not a list of records."""
    history = store.read(Collection.CANDIDATE_HISTORY)
    if not isinstance(history, list) or any(not isinstance(record, dict) for record in history):
        return None
    return history


def history_match(
    row: Mapping[str, Any], history: Sequence[dict[str, Any]], aliases: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The history record of one snapshot row: exact identity key, else alias or durable ID."""
    from autoscout.candidate_identity import resolve_history_match

    key = str(row.get("identity_key", "")).strip()
    exact = next((record for record in history if key and str(record.get("identity_key", "")) == key), None)
    if exact is not None:
        return exact
    try:
        matched, _durable_id = resolve_history_match(dict(row), list(history), dict(aliases))
    except (TypeError, ValueError):
        return None
    if matched is None:
        return None
    # Return the record object itself, so completion fills history in place.
    return next((record for record in history if record is matched or record == matched), None)


def select_records(
    history: Sequence[dict[str, Any]], scope: str, current: Sequence[Any], aliases: Mapping[str, Any]
) -> list[dict[str, Any]]:
    if scope == SCOPE_ALL:
        return list(history)
    if scope == SCOPE_UNDECIDED:
        return [record for record in history if not str(record.get("editor_status", "") or "").strip()]
    selected: list[dict[str, Any]] = []
    for row in current:
        if not isinstance(row, Mapping):
            continue
        record = history_match(row, history, aliases)
        if record is not None and all(record is not chosen for chosen in selected):
            selected.append(record)
    return selected


def merge_into_fresh_history(
    store: StateStore, completed: Sequence[Mapping[str, Any]], spec: LearnedFieldSpec
) -> list[dict[str, Any]]:
    """Merge the learned fields into Candidate History as the store holds it now.

    Lookups can take minutes, and feedback or another completion may write
    history meanwhile. History is read again, and only the learned fields
    of records that do not hold their value there yet are written, so no
    newer field and no value another writer filled is lost. The caller
    writes the result in the same store transaction as the read.
    """
    fresh = read_history(store)
    if fresh is None:
        raise ValueError(f"Candidate History changed into an invalid shape during {spec.label}")
    learned = {str(record.get("identity_key", "")): record for record in completed if record.get("identity_key")}
    for record in fresh:
        source = learned.get(str(record.get("identity_key", "")))
        if source is None or spec.already_filled(record):
            continue
        if spec.merge_record is not None:
            spec.merge_record(record, source)
            continue
        for field_name in spec.fields:
            if field_name in source:
                record[field_name] = source[field_name]
            else:
                record.pop(field_name, None)
    return fresh


def blind_fingerprint(output_dir: Path, store: StateStore, spec: LearnedFieldSpec) -> str:
    """A digest of everything the feedback marker guards, with the learned fields left out."""
    import hashlib

    from autoscout.feedback_finalization import _GUARDED_COLLECTIONS

    def blind(value: Any) -> Any:
        if isinstance(value, list):
            return [
                {key: item for key, item in entry.items() if key not in spec.fields}
                if isinstance(entry, Mapping)
                else entry
                for entry in value
            ]
        return value

    content = {f"{label}@state": blind(store.read(collection)) for collection, label in _GUARDED_COLLECTIONS}
    for name in ("scouting-candidates.json", "source-mentions.json"):
        content[name] = blind(read_json_or_default(output_dir / name, None))
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def current_marker(output_dir: Path) -> dict[str, Any] | None:
    from autoscout.feedback_finalization import _read_marker

    return _read_marker(output_dir)


def finalization_sealed(output_dir: Path, store: StateStore) -> bool:
    from autoscout.feedback_finalization import _marker_matches
    from autoscout.feedback_finalization import _snapshot_digest
    from autoscout.feedback_finalization import _state_guard_digest

    digest = _snapshot_digest(output_dir)
    return digest is not None and _marker_matches(output_dir, digest, _state_guard_digest(store))


def seal_finalization(output_dir: Path, store: StateStore) -> None:
    from autoscout.feedback_finalization import _snapshot_digest
    from autoscout.feedback_finalization import _state_guard_digest
    from autoscout.feedback_finalization import _write_marker

    digest, guard = _snapshot_digest(output_dir), _state_guard_digest(store)
    if digest and guard:
        _write_marker(output_dir, digest, guard)


def project_fields(
    history: Sequence[dict[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    aliases: Mapping[str, Any] | None,
    spec: LearnedFieldSpec,
) -> tuple[list[dict[str, Any]], int]:
    """Copy learned fields from history onto snapshot rows; return the rows and how many changed.

    Rows are matched like every description projection: exact identity key,
    else alias or durable Candidate ID, so a translated or renamed row whose
    key differs from history still gets its fields.
    """
    copied = 0
    updated = []
    for row in rows:
        row = dict(row)
        record = history_match(row, history, aliases or {})
        if record is not None and spec.copy_to_row(record, row):
            copied += 1
        updated.append(row)
    return updated, copied


def _snapshot_rows(rows: Any, name: str) -> list[Mapping[str, Any]]:
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise ValueError(f"{name} must be a list of objects")
    return rows


def persist_learned_fields(
    output_dir: Path,
    store: StateStore,
    *,
    spec: LearnedFieldSpec,
    selected: Sequence[Mapping[str, Any]],
    touched: set[str],
    changed: bool,
    result: dict[str, Any],
    effective_date: str,
    publish_lock: Callable[[], Any] | None,
    report_outside_lock: bool = False,
) -> None:
    """Write what the pass learned: history and the latest snapshot, then the current snapshot, then the report.

    ``touched`` is the set of identity keys whose records changed; ``changed``
    says whether history itself has anything to write. The latest Scouting
    Candidates count as absent (``None``) when the store holds none.

    ``report_outside_lock`` writes the report file, a diagnostic, after
    ``publish_lock`` is released, and a failure to write it is recorded in
    ``result`` (``report_file: "not_written"``) instead of raised: the lock
    a weekly stage passes is the report transaction, whose marker a failed
    diagnostic must not leave behind (#1181).
    """
    current_path = output_dir / "scouting-candidates.json"
    aliases = load_aliases(store)
    # The read, merge and write run under the report directory's lock, so
    # two standalone passes never erase each other's learned fields. The
    # weekly stage already holds that lock and passes none.
    with publish_lock() if publish_lock is not None else contextlib.nullcontext():
        # A sealed feedback-finalization marker stays sealed: the learned
        # writes change the digests it guards, and an unsealed marker would
        # make the next feedback step re-scout and drop the enriched snapshot.
        # A marker that was already stale is left alone.
        pending_path = output_dir / spec.seal_pending_filename
        marker_now = current_marker(output_dir)
        pending = read_json_or_default(pending_path, None)
        # A pass that sealed, wrote history and was then interrupted left this
        # note. The retry may finish the seal only when the marker is still the
        # one it found and nothing but the learned fields changed since: an
        # editorial change in between (a new Hard Rule) must never be sealed
        # unseen.
        resumed = (
            isinstance(pending, Mapping)
            and marker_now is not None
            and pending.get("marker") == marker_now
            and pending.get("fingerprint") == blind_fingerprint(output_dir, store, spec)
        )
        sealed = finalization_sealed(output_dir, store) or resumed
        if sealed and not resumed:
            # Written before any write, history or snapshot projection alike,
            # so every interruption from here on can be finished by a retry.
            write_json(pending_path, {"marker": marker_now, "fingerprint": blind_fingerprint(output_dir, store, spec)})
        projected: dict[str, int | None] = {"current": None, "latest": None}
        # History is always read again before it is merged or projected: other
        # writers may have changed it during the lookups. The read, the merge
        # and the writes of history and the latest snapshot are one
        # transaction, so a record never loses a value written in between.
        with store.transaction():
            history = merge_into_fresh_history(
                store,
                [record for record in selected if str(record.get("identity_key", "")) in touched],
                spec,
            )
            if changed:
                store.replace(Collection.CANDIDATE_HISTORY, history)
            latest = _snapshot_rows(store.read(Collection.LATEST_SCOUTING_CANDIDATES), "the latest Scouting Candidates")
            if latest:
                latest_rows, projected["latest"] = spec.project(history, latest, aliases)
                if projected["latest"]:
                    store.replace(Collection.LATEST_SCOUTING_CANDIDATES, latest_rows)
        if current_path.exists():
            current = _snapshot_rows(read_json_or_default(current_path, []), current_path.name)
            current_rows, projected["current"] = spec.project(history, current, aliases)
            if projected["current"]:
                write_json(current_path, current_rows)
        result["projected"] = {"current": projected["current"], "latest": projected["latest"]}
        if not report_outside_lock:
            write_json(output_dir / spec.report_filename, {"date": effective_date, **result})
        projected_any = any(count for count in result["projected"].values() if count)
        if (changed or projected_any or resumed) and sealed:
            spec.seal(output_dir, store)
        if pending_path.exists():
            pending_path.unlink()
    if report_outside_lock:
        try:
            write_json(output_dir / spec.report_filename, {"date": effective_date, **result})
        except OSError:
            result["report_file"] = "not_written"
