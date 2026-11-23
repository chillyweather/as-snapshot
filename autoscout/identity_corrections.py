"""Guarded Candidate identity correction and state-reference migration.

Merging two Candidate identities is a confirmation boundary: the plan is
computed in full, shown to the editor, and only then written.  Durable
content that disagrees between the two records is never resolved silently.

Two independently generated Russian descriptions are the one disagreement
the editor can settle without hand-editing canonical state (issue #559).
The correction artifact carries a ``description_resolution`` block that
names the winning record and repeats the exact description text the editor
saw, so a description that changed since the preview fails the correction
instead of being merged away.

Two different editor decisions on one work are settled the same way (issue
#917): an ``editor_decision_resolution`` block names the record whose
status and note become canonical and repeats every decision the editor saw.
The decisions it does not keep are preserved word for word in an audit
field on the merged record, never dropped.
"""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autoscout.archive_backfill import ARCHIVE_BACKFILL_FIELD
from autoscout.archive_backfill import earliest_archive_backfill
from autoscout.candidate_identity import candidate_id_for
from autoscout.candidate_identity import effective_candidate_id
from autoscout.candidate_identity import empty_alias_table
from autoscout.candidate_identity import record_durable_id_merge
from autoscout.editor_decisions import DECISION_LOG_FIELD
from autoscout.editor_decisions import merge_decision_logs
from autoscout.editor_decisions import record_resolved_decision
from autoscout.editor_feedback import FEEDBACK_ID_COLUMNS
from autoscout.json_io import read_json_or_default
from autoscout.json_io import serialize_json
from autoscout.pm_publication import CITED_BEYOND_PM_DEALS_FIELD
from autoscout.pm_publication import PM_RECHECK_DATE_FIELD
from autoscout.review_hiding import EARLIER_SOURCE_NAMES_FIELD
from autoscout.review_hiding import evidence_source_names
from autoscout.safe_artifacts import bundle_write_lock
from autoscout.safe_artifacts import publish_bundle
from autoscout.safe_artifacts import staged_replace
from autoscout.scouting_candidates import SIGNAL_STRENGTH_RANKS
from autoscout.sort_utils import sort_by_key
from autoscout.state_publication import publish_files_with_state
from autoscout.state_store import Collection
from autoscout.state_store import StateStore
from autoscout.state_store import open_state_store

# The places that reference a Candidate identity: State Store collections,
# then the run files a weekly run leaves in the state directory.
REFERENCE_FILES: tuple[Collection | str, ...] = (
    Collection.EXCLUDED_CANDIDATE_HISTORY,
    Collection.PENDING_EDITOR_ROWS,
    Collection.ARCHIVED_EDITOR_ROWS,
    Collection.LATEST_SCOUTING_CANDIDATES,
    Collection.CANDIDATE_VISIBILITY,
    "scouting-candidates.json",
    "memo-queue.json",
    "suppressed-candidates.json",
    "proposed-corrections.json",
)

CONFLICT_SENSITIVE_FIELDS = (
    "editor_status",
    "editor_note",
    "content_description",
    "visibility",
)


DESCRIPTION_RESOLUTION_FIELD = "description_resolution"
DESCRIPTION_RESOLUTION_RECORD_FIELD = "description_merge_resolution"
DESCRIPTION_PROVENANCE_RESOLUTION_FIELD = "observed_description_provenance"
DESCRIPTION_ATTEMPTS_FIELD = "grounded_description_attempts"
DISCARDED_ATTEMPTS_FIELD = "discarded_description_attempts"
EDITOR_DECISION_RESOLUTION_FIELD = "editor_decision_resolution"
EDITOR_DECISION_RESOLUTION_RECORD_FIELD = "editor_decision_merge_resolution"
# The fields one editor decision consists of. A resolution moves them together.
EDITOR_DECISION_FIELDS = ("editor_status", "editor_note", "feedback_dimensions")
MEMO_QUEUE_STATUS = "needs_deeper_analysis"
SUPPRESSED_CANDIDATES_FILE = "suppressed-candidates.json"
# The decision fields the review reads from editor-facing.csv when a record lacks them.
CSV_FALLBACK_DECISION_FIELDS = ("editor_status", "editor_note")
MEMO_QUEUE_FILE = "memo-queue.json"


def _state_write_lock(state_dir: Path) -> AbstractContextManager[None]:
    """Serialize one whole correction against every other on the same state.

    The lock identity is the state directory, not the planned file set, so two
    corrections that plan different files still exclude each other.  It is held
    across planning and publication together, because a plan computed from
    state that another correction is about to replace publishes a stale merge
    over a fresh one (issue #559 review).
    """
    return bundle_write_lock(state_dir)


def reference_label(filename: Collection | str) -> str:
    return filename.value if isinstance(filename, Collection) else filename


def _replace_state_file(source: Path, destination: Path) -> None:
    """Move one staged run file of the state directory over its destination.

    A module-level seam, matching the convention in ``safe_artifacts``: tests
    inject a write failure here to prove the bundle rolls back whole.
    """
    staged_replace(source, destination)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class DescriptionResolution:
    """One editor decision about which generated description survives a merge.

    ``chosen_identity_key`` names the source record whose description and
    provenance become canonical.  The description is never rewritten or
    recombined, so the retained provenance keeps describing the exact text it
    was generated for.
    """

    chosen_identity_key: str
    content_description: str
    description_provenance: dict[str, Any] | None
    discarded_identity_keys: tuple[str, ...]

    def apply_to(self, merged: dict[str, Any], canonical_identity_key: str) -> None:
        merged["content_description"] = self.content_description
        if self.description_provenance is None:
            merged.pop("description_provenance", None)
        else:
            provenance = deepcopy(self.description_provenance)
            provenance["identity_key"] = canonical_identity_key
            merged["description_provenance"] = provenance

    def audit(self, canonical_identity_key: str) -> dict[str, Any]:
        """The durable record of which alternative the editor kept.

        Identity keys only: a discarded description is generated text, and
        state keeps bounded references rather than copied prose.
        """
        return {
            "canonical_identity_key": canonical_identity_key,
            "chosen_source_identity_key": self.chosen_identity_key,
            "discarded_source_identity_keys": list(self.discarded_identity_keys),
        }

    def report(self) -> dict[str, Any]:
        provenance = self.description_provenance or {}
        evidence_basis = provenance.get("evidence_basis", [])
        return {
            "chosen_source_identity_key": self.chosen_identity_key,
            "provenance": str(provenance.get("provenance", "")),
            "evidence_basis": list(evidence_basis) if isinstance(evidence_basis, list) else [],
            "discarded_source_identity_keys": list(self.discarded_identity_keys),
        }


@dataclass(frozen=True)
class EditorDecisionResolution:
    """The editor's explicit choice between different decisions on one work (issue #917).

    ``discarded`` keeps every other decision verbatim, so an editor note is
    never lost by a merge, only moved into the audit record.
    """

    chosen_identity_key: str
    decision: dict[str, Any]
    discarded: tuple[dict[str, Any], ...]

    @property
    def suppresses(self) -> bool:
        """Whether the chosen status hides a work without Material Change (pipeline rule)."""
        # Local import: the pipeline imports this module.
        from autoscout.pipeline import SUPPRESSING_EDITOR_STATUSES

        return str(self.decision.get("editor_status", "")) in SUPPRESSING_EDITOR_STATUSES

    @property
    def queued_for_memo(self) -> bool:
        """Whether the chosen decision keeps the work in the research memo queue (pipeline rule)."""
        return self.decision.get("editor_status") == MEMO_QUEUE_STATUS

    def apply_to(self, merged: dict[str, Any]) -> None:
        for field in EDITOR_DECISION_FIELDS:
            if field in self.decision:
                merged[field] = deepcopy(self.decision[field])
            else:
                merged.pop(field, None)
        # The marker follows the status, exactly as the pipeline derives it (review of #917).
        if "memo_queue_marker" in merged or self.queued_for_memo:
            merged["memo_queue_marker"] = MEMO_QUEUE_STATUS if self.queued_for_memo else ""

    def audit(self, canonical_identity_key: str) -> dict[str, Any]:
        return {
            "canonical_identity_key": canonical_identity_key,
            "chosen_source_identity_key": self.chosen_identity_key,
            "discarded_decisions": [deepcopy(entry) for entry in self.discarded],
        }

    def report(self) -> dict[str, Any]:
        return {
            "chosen_source_identity_key": self.chosen_identity_key,
            "editor_status": str(self.decision.get("editor_status", "")),
            "discarded_decisions": [deepcopy(entry) for entry in self.discarded],
        }


@dataclass(frozen=True)
class CanonicalMerge:
    """What the confirmed correction says every merged record must become.

    The canonical identity, the durable Candidate ID that identity owns, and
    the editor's description decision travel together through every merge in
    one correction, so history and the state files it rewrites cannot end up
    disagreeing about any of them.
    """

    identity_key: str
    title: str
    primary_authors: list[str]
    candidate_id: str = ""
    description_resolution: DescriptionResolution | None = None
    editor_decision_resolution: EditorDecisionResolution | None = None


@dataclass(frozen=True)
class CanonicalProjection:
    """The canonical facts every surviving reference to the identity must show.

    Built from the merged Candidate History record, so a reference file can
    never disagree with history about the title, the durable Candidate ID, or
    the description.  Stamping updates only the fields a record already
    carries, so an editor row that never had a title does not gain one.
    """

    identity_key: str
    title: str
    primary_authors: list[str]
    candidate_id: str
    content_description: str
    description_provenance: dict[str, Any] | None

    @classmethod
    def from_merged(cls, merged: dict[str, Any]) -> CanonicalProjection:
        authors = merged.get("primary_authors", [])
        provenance = merged.get("description_provenance")
        return cls(
            identity_key=str(merged.get("identity_key", "")).strip(),
            title=str(merged.get("title", "")),
            primary_authors=list(authors) if isinstance(authors, list) else [],
            candidate_id=str(merged.get("candidate_id", "")).strip(),
            content_description=str(merged.get("content_description", "")),
            description_provenance=deepcopy(provenance) if isinstance(provenance, dict) else None,
        )

    def stamp(self, record: dict[str, Any]) -> None:
        record["identity_key"] = self.identity_key
        if "title" in record:
            record["title"] = self.title
        if "primary_authors" in record:
            record["primary_authors"] = list(self.primary_authors)
        if "candidate_id" in record and self.candidate_id:
            record["candidate_id"] = self.candidate_id
        if "content_description" in record:
            record["content_description"] = self.content_description
        if "description_provenance" in record:
            # A reference must never keep provenance for text it no longer
            # shows. When the merged record has none, the reference loses its
            # own rather than describing the new text with the old evidence.
            if self.description_provenance is None:
                record.pop("description_provenance")
            else:
                provenance = deepcopy(self.description_provenance)
                provenance["identity_key"] = self.identity_key
                record["description_provenance"] = provenance


def _identity_key(record: Any) -> str:
    return str(record.get("identity_key", "")).strip() if isinstance(record, dict) else ""


def _description_text(record: dict[str, Any]) -> str:
    value = record.get("content_description")
    return str(value).strip() if isinstance(value, str) else ""


def _description_alternative(record: dict[str, Any]) -> str | None:
    """Return the record's whole description offer, or ``None`` when it has none.

    Two records that carry the same text under a different provenance still
    offer the editor two different things, so the provenance is part of what
    one merged record can keep and the others lose.
    """
    text = _description_text(record)
    provenance = record.get("description_provenance")
    provenance = provenance if isinstance(provenance, dict) else None
    if not text and provenance is None:
        return None
    return json.dumps([text, provenance], ensure_ascii=False, sort_keys=True)


def parse_description_resolution(
    correction: dict[str, Any],
    records: list[dict[str, Any]],
) -> DescriptionResolution | None:
    """Validate the editor's explicit description choice against live state.

    Returns ``None`` when the artifact resolves nothing, which leaves the
    existing refusal of conflicting durable content in place.  Every failure
    here happens while the plan is still being computed, so no state file is
    written for a missing, stale, or incompatible resolution.
    """
    raw = correction.get(DESCRIPTION_RESOLUTION_FIELD)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("description_resolution must be an object")
    chosen_key = str(raw.get("canonical_source_identity_key", "")).strip()
    if not chosen_key:
        raise ValueError("description_resolution requires a nonblank canonical_source_identity_key")
    observed = raw.get("observed_descriptions")
    if not isinstance(observed, dict):
        raise ValueError("description_resolution requires an observed_descriptions object")

    described = {_identity_key(record): _description_text(record) for record in records if _description_text(record)}
    merged_keys = {_identity_key(record) for record in records}
    named_keys = {str(key).strip() for key in observed} | {chosen_key}
    unknown = sorted(named_keys - merged_keys)
    if unknown:
        raise ValueError(f"description_resolution names an unknown source identity key: {', '.join(unknown)}")
    observed_texts = {str(key).strip(): str(value) if isinstance(value, str) else "" for key, value in observed.items()}
    missing = sorted(set(described) - set(observed_texts))
    if missing:
        raise ValueError(f"description_resolution is stale: no observed content_description for {', '.join(missing)}")
    changed = sorted(key for key, text in observed_texts.items() if described.get(key, "") != text.strip())
    if changed:
        raise ValueError(f"description_resolution is stale: content_description changed for {', '.join(changed)}")

    chosen = next((record for record in records if _identity_key(record) == chosen_key), None)
    if chosen is None or not _description_text(chosen):
        raise ValueError("description_resolution must choose a record that has a content description")

    # The provenance is half of what the editor approved, so it is pinned the
    # same way the text is: a provenance that moved since the preview makes
    # the whole approval stale (issue #559 review).
    current_provenance = {
        _identity_key(record): record["description_provenance"]
        for record in records
        if isinstance(record.get("description_provenance"), dict)
    }
    observed_provenance = raw.get(DESCRIPTION_PROVENANCE_RESOLUTION_FIELD)
    if current_provenance or observed_provenance is not None:
        if not isinstance(observed_provenance, dict):
            raise ValueError(f"description_resolution requires an {DESCRIPTION_PROVENANCE_RESOLUTION_FIELD} object")
        named_provenance = {str(key).strip() for key in observed_provenance}
        unknown = sorted(named_provenance - merged_keys)
        if unknown:
            raise ValueError(f"description_resolution names an unknown source identity key: {', '.join(unknown)}")
        missing = sorted(set(current_provenance) - named_provenance)
        if missing:
            raise ValueError(
                f"description_resolution is stale: no observed description_provenance for {', '.join(missing)}"
            )
        changed = sorted(
            str(key).strip()
            for key, value in observed_provenance.items()
            if _canonical_json(current_provenance.get(str(key).strip())) != _canonical_json(value)
        )
        if changed:
            raise ValueError(
                f"description_resolution is stale: description_provenance changed for {', '.join(changed)}"
            )

    provenance = chosen.get("description_provenance")
    if isinstance(provenance, dict):
        provenance_text = str(provenance.get("content_description", "")).strip()
        if provenance_text and provenance_text != _description_text(chosen):
            raise ValueError(
                "description_resolution chose a description_provenance that describes a different content_description"
            )
    kept = _description_alternative(chosen)
    # A record with no description and no provenance offered nothing, so the
    # merge discarded nothing of its own.
    discarded = tuple(
        sorted(
            _identity_key(record)
            for record in records
            if _identity_key(record) != chosen_key and _description_alternative(record) not in (None, kept)
        )
    )
    return DescriptionResolution(
        chosen_identity_key=chosen_key,
        content_description=str(chosen["content_description"]),
        description_provenance=deepcopy(provenance) if isinstance(provenance, dict) else None,
        discarded_identity_keys=discarded,
    )


def _editor_decision(record: dict[str, Any]) -> dict[str, Any]:
    """The record's editor decision: its nonempty decision fields, verbatim."""
    return {field: record[field] for field in EDITOR_DECISION_FIELDS if _nonempty(record.get(field))}


def _decision_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else _canonical_json(value)


def parse_editor_decision_resolution(
    correction: dict[str, Any],
    records: list[dict[str, Any]],
) -> EditorDecisionResolution | None:
    """Validate the editor's explicit choice between different decisions (issue #917).

    The block names ``canonical_source_identity_key`` and repeats, under
    ``observed_editor_decisions``, the decision of every merged record that
    has one (``editor_status``, ``editor_note``, and ``feedback_dimensions``
    when present), verbatim apart from surrounding whitespace. A missing,
    extra, or stale entry fails the correction before any state is written.
    Returns ``None`` when the artifact resolves nothing, which keeps the
    refusal of conflicting decisions.
    """
    raw = correction.get(EDITOR_DECISION_RESOLUTION_FIELD)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("editor_decision_resolution must be an object")
    chosen_key = str(raw.get("canonical_source_identity_key", "")).strip()
    if not chosen_key:
        raise ValueError("editor_decision_resolution requires a nonblank canonical_source_identity_key")
    observed = raw.get("observed_editor_decisions")
    if not isinstance(observed, dict) or any(not isinstance(value, dict) for value in observed.values()):
        raise ValueError("editor_decision_resolution requires an observed_editor_decisions object of objects")
    merged_keys = {_identity_key(record) for record in records}
    observed = {str(key).strip(): value for key, value in observed.items()}
    unknown = sorted((set(observed) | {chosen_key}) - merged_keys)
    if unknown:
        raise ValueError(f"editor_decision_resolution names an unknown source identity key: {', '.join(unknown)}")
    current = {_identity_key(record): _editor_decision(record) for record in records if _editor_decision(record)}
    missing = sorted(set(current) - set(observed))
    if missing:
        raise ValueError(f"editor_decision_resolution is stale: no observed decision for {', '.join(missing)}")
    extra = sorted(set(observed) - set(current))
    if extra:
        raise ValueError(f"editor_decision_resolution names a record without a decision: {', '.join(extra)}")
    changed = sorted(
        key
        for key, decision in current.items()
        if set(decision) != {field for field, value in observed[key].items() if _nonempty(value)}
        or any(_decision_text(decision[field]) != _decision_text(observed[key].get(field)) for field in decision)
    )
    if changed:
        raise ValueError(f"editor_decision_resolution is stale: the decision changed for {', '.join(changed)}")
    if chosen_key not in current:
        raise ValueError("editor_decision_resolution must choose a record that has an editor decision")
    chosen = current[chosen_key]
    discarded = tuple(
        {"source_identity_key": key, **deepcopy(decision)}
        for key, decision in sorted(current.items())
        if key != chosen_key and decision != chosen
    )
    for field in CSV_FALLBACK_DECISION_FIELDS:
        if not _nonempty(chosen.get(field)) and any(_nonempty(item.get(field)) for item in discarded):
            # The review falls back to the editor-facing CSV status and note
            # when a record has none, so a discarded one would reappear beside
            # the chosen decision (review of #917).
            raise ValueError(
                f"editor_decision_resolution chooses a decision without {field} over one that has it; "
                f"choose a decision that carries {field} or record it first"
            )
    return EditorDecisionResolution(chosen_identity_key=chosen_key, decision=deepcopy(chosen), discarded=discarded)


def _nonempty(value: Any) -> bool:
    return value not in (None, "", [], {})


def _unique_list(*values: Any) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, list):
            continue
        for item in value:
            key = json.dumps(item, ensure_ascii=False, sort_keys=True)
            if key not in seen:
                seen.add(key)
                result.append(deepcopy(item))
    return result


def _merge_description_attempts(
    records: list[dict[str, Any]],
    *,
    chosen_identity_key: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Union the description-attempt logs of every merged record.

    Attempt history says how each duplicate's description was reached, so a
    merge that keeps one log and drops the other destroys evidence no later
    issue can restore.  Identical entries collapse.  Two different entries
    that claim the same ``operation_id`` cannot both be kept, because the
    enrichment stage replaces an attempt by that ID, so they are a conflict
    the editor must resolve rather than something to merge away (issue #559
    review).

    The editor resolves it with the same ``description_resolution`` that
    settles the description: ``chosen_identity_key`` names the record whose
    attempt is kept for every conflicting operation (issue #1023).  The other
    attempts are returned as the second value, each with the
    ``source_identity_key`` of the record it came from, for the audit record.
    Without a choice, or when the chosen record has no attempt of its own for
    a conflicting operation, or when the chosen record itself carries two
    different attempts for it, the conflict still refuses the merge.
    """
    by_operation: dict[str, list[tuple[str, Any]]] = {}
    for record in records:
        key = _identity_key(record)
        raw = record.get(DESCRIPTION_ATTEMPTS_FIELD)
        if not isinstance(raw, list):
            continue
        for attempt in raw:
            operation_id = str(attempt.get("operation_id", "")).strip() if isinstance(attempt, dict) else ""
            if operation_id:
                by_operation.setdefault(operation_id, []).append((key, attempt))
    kept_by_operation: dict[str, Any] = {}
    discarded: list[dict[str, Any]] = []
    for operation_id, entries in by_operation.items():
        distinct = _unique_list([attempt for _key, attempt in entries])
        if len(distinct) == 1:
            kept_by_operation[operation_id] = distinct[0]
            continue
        chosen = _unique_list([attempt for key, attempt in entries if key == chosen_identity_key])
        if chosen_identity_key is None or len(chosen) != 1:
            raise ValueError(
                f"conflicting {DESCRIPTION_ATTEMPTS_FIELD} for operation {operation_id} require explicit resolution"
            )
        kept_by_operation[operation_id] = chosen[0]
        discarded.extend(
            {"source_identity_key": key, **deepcopy(attempt)} for key, attempt in entries if attempt != chosen[0]
        )

    attempts: list[Any] = []
    for record in records:
        raw = record.get(DESCRIPTION_ATTEMPTS_FIELD)
        if not isinstance(raw, list):
            continue
        for attempt in raw:
            operation_id = str(attempt.get("operation_id", "")).strip() if isinstance(attempt, dict) else ""
            if not operation_id:
                # Two identical attempts are two attempts. Only an operation
                # ID makes one entry the same event as another.
                attempts.append(deepcopy(attempt))
            elif operation_id in kept_by_operation:
                attempts.append(deepcopy(kept_by_operation.pop(operation_id)))
    ordered = sorted(attempts, key=lambda item: str(item.get("date", "")) if isinstance(item, dict) else "")
    return ordered, _unique_list(discarded)


def history_merge_conflicts(
    records: list[dict[str, Any]],
    *,
    description_resolved: bool = False,
    decision_resolved: bool = False,
    description_source_key: str | None = None,
    attempts_settled: bool = False,
) -> list[tuple[str, str]]:
    """Return every conflict that stops these records merging, as (field, message).

    The merge refuses the first one; the probable-duplicates report (#855)
    lists all of them, so the two never disagree. With
    ``description_resolved`` an explicit description_resolution block has
    settled the description and its provenance. With ``decision_resolved``
    an editor_decision_resolution block has settled the editor decision.
    ``description_source_key`` is the record that block chose, whose
    description attempt wins a conflict under one ``operation_id`` (#1023).
    With ``attempts_settled`` the merged history record already decided the
    attempt log, so the records' attempts are not compared again.
    """
    conflicts: list[tuple[str, str]] = []
    for field in CONFLICT_SENSITIVE_FIELDS:
        # An explicitly resolved description is no longer a conflict.
        if description_resolved and field == "content_description":
            continue
        # Nor is an explicitly resolved editor decision (issue #917).
        if decision_resolved and field in EDITOR_DECISION_FIELDS:
            continue
        values = {
            json.dumps(record[field], ensure_ascii=False, sort_keys=True)
            for record in records
            if _nonempty(record.get(field))
        }
        if len(values) > 1:
            conflicts.append((field, f"conflicting {field} values require explicit resolution"))
    try:
        if not attempts_settled:
            _merge_description_attempts(
                records, chosen_identity_key=description_source_key if description_resolved else None
            )
    except ValueError as exc:
        conflicts.append((DESCRIPTION_ATTEMPTS_FIELD, str(exc)))
    if not description_resolved:
        provenance_records = [
            record["description_provenance"]
            for record in records
            if isinstance(record.get("description_provenance"), dict)
        ]
        labels = {str(record.get("provenance", "")) for record in provenance_records}
        texts = {
            str(record.get("content_description", ""))
            for record in provenance_records
            if record.get("content_description")
        }
        if len(labels) > 1 or len(texts) > 1:
            conflicts.append(
                ("description_provenance", "conflicting description_provenance values require explicit resolution")
            )
    return conflicts


# Reference files whose records a merge combines, not only rekeys.
MERGED_REFERENCE_FILES: tuple[Collection | str, ...] = (
    Collection.LATEST_SCOUTING_CANDIDATES,
    "scouting-candidates.json",
    Collection.CANDIDATE_VISIBILITY,
)


def _reference_merge_conflicts(
    filename: Collection | str,
    records: list[dict[str, Any]],
    *,
    description_resolved: bool = False,
    decision_resolved: bool = False,
) -> list[tuple[str, str]]:
    """Return the conflicts that stop the records of one reference file merging."""
    if len(records) < 2:
        return []
    if filename == Collection.CANDIDATE_VISIBILITY:
        values = {str(record.get("visibility", "")).strip() for record in records if record.get("visibility")}
        if len(values) > 1:
            return [("visibility", "identity correction would merge conflicting visibility decisions")]
        return []
    if filename in MERGED_REFERENCE_FILES:
        return history_merge_conflicts(
            records, description_resolved=description_resolved, decision_resolved=decision_resolved
        )
    return []


def identity_correction_conflicts(
    history_records: list[dict[str, Any]], references: dict[Collection | str, Any], identity_keys: set[str]
) -> list[str]:
    """Return every field an identity correction of ``identity_keys`` refuses, without a description_resolution.

    ``history_records`` are the Candidate History records being merged;
    ``references`` maps each of ``MERGED_REFERENCE_FILES`` to its loaded
    records. The merge itself refuses the first of these (#855).
    """
    fields = [field for field, _message in history_merge_conflicts(history_records)]
    for filename in MERGED_REFERENCE_FILES:
        records = references.get(filename)
        if not isinstance(records, list):
            continue
        group = [record for record in records if isinstance(record, dict) and _identity_key(record) in identity_keys]
        fields.extend(field for field, _message in _reference_merge_conflicts(filename, group))
    return list(dict.fromkeys(fields))


def _merge_history_records(
    records: list[dict[str, Any]],
    canonical: CanonicalMerge,
    history_attempts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Merge records into one; ``history_attempts`` gives snapshot rows the merged history's attempt log."""
    resolution = canonical.description_resolution
    decision_resolution = canonical.editor_decision_resolution
    chosen_key = resolution.chosen_identity_key if resolution is not None else None
    conflicts = history_merge_conflicts(
        records,
        description_resolved=resolution is not None,
        decision_resolved=decision_resolution is not None,
        description_source_key=chosen_key,
        attempts_settled=history_attempts is not None,
    )
    if conflicts:
        raise ValueError(conflicts[0][1])

    base = max(records, key=lambda record: (sum(_nonempty(value) for value in record.values()), str(record)))
    merged = deepcopy(base)
    for record in records:
        for field, value in record.items():
            if not _nonempty(merged.get(field)) and _nonempty(value):
                merged[field] = deepcopy(value)
    merged["source_evidence"] = _unique_list(*(record.get("source_evidence") for record in records))
    if history_attempts is not None:
        # A merged snapshot row shows history's log only when one of its rows had a log.
        had_attempts = any(DESCRIPTION_ATTEMPTS_FIELD in record for record in records)
        attempts = deepcopy(history_attempts) if had_attempts else []
    else:
        attempts, _discarded = _merge_description_attempts(records, chosen_identity_key=chosen_key)
    if attempts:
        merged[DESCRIPTION_ATTEMPTS_FIELD] = attempts
    # Every decision any of the records logged stays in the log (issue #1053).
    decisions = merge_decision_logs(records)
    if decisions:
        merged[DECISION_LOG_FIELD] = decisions
    merged["independent_signal_families"] = _unique_list(
        *(record.get("independent_signal_families") for record in records)
    )
    if merged["independent_signal_families"]:
        merged["independent_signal_count"] = len(merged["independent_signal_families"])
    signal_candidates = [
        signal
        for signal in (
            *merged["source_evidence"],
            *(record.get("primary_signal") for record in records),
        )
        if isinstance(signal, dict) and str(signal.get("signal_strength", "")).strip()
    ]
    if signal_candidates:
        primary_signal = max(
            signal_candidates,
            key=lambda signal: SIGNAL_STRENGTH_RANKS.get(str(signal.get("signal_strength", "")).strip().lower(), 0),
        )
        merged["primary_signal"] = deepcopy(primary_signal)
        merged["primary_signal_strength"] = str(primary_signal.get("signal_strength", ""))
    provenance_records = (
        [
            record["description_provenance"]
            for record in records
            if isinstance(record.get("description_provenance"), dict)
        ]
        if resolution is None
        else []
    )
    if provenance_records:
        provenance = deepcopy(provenance_records[0])
        provenance["identity_key"] = canonical.identity_key
        provenance["evidence_basis"] = _unique_list(*(record.get("evidence_basis") for record in provenance_records))
        applied_dates = [
            str(record.get("applied_date", "")) for record in provenance_records if record.get("applied_date")
        ]
        if applied_dates:
            provenance["applied_date"] = min(applied_dates)
        merged["description_provenance"] = provenance
    first_seen = [str(record.get("first_seen_date", "")) for record in records if record.get("first_seen_date")]
    last_seen = [str(record.get("last_seen_date", "")) for record in records if record.get("last_seen_date")]
    last_material_change = [
        str(record.get("last_material_change_date", ""))
        for record in records
        if record.get("last_material_change_date")
    ]
    if first_seen:
        merged["first_seen_date"] = min(first_seen)
    if last_seen:
        merged["last_seen_date"] = max(last_seen)
    if last_material_change:
        merged["last_material_change_date"] = max(last_material_change)
    # A merge never searches a PM-only book later than either record would (#947).
    pm_rechecks = [str(record[PM_RECHECK_DATE_FIELD]) for record in records if record.get(PM_RECHECK_DATE_FIELD)]
    if pm_rechecks:
        merged[PM_RECHECK_DATE_FIELD] = min(pm_rechecks)
    if any(record.get(CITED_BEYOND_PM_DEALS_FIELD) is True for record in records):
        merged[CITED_BEYOND_PM_DEALS_FIELD] = True
    # A merge keeps every source any of the records remembers (#1025).
    earlier_names = sorted(
        {str(name) for record in records for name in record.get(EARLIER_SOURCE_NAMES_FIELD) or [] if str(name).strip()}
        - evidence_source_names(merged.get("source_evidence"))
    )
    if earlier_names:
        merged[EARLIER_SOURCE_NAMES_FIELD] = earlier_names
    else:
        merged.pop(EARLIER_SOURCE_NAMES_FIELD, None)
    # A merge keeps the archive batch a record entered through, the earliest one.
    archive_marker = earliest_archive_backfill(records)
    if archive_marker is not None:
        merged[ARCHIVE_BACKFILL_FIELD] = archive_marker
    else:
        merged.pop(ARCHIVE_BACKFILL_FIELD, None)
    merged["identity_key"] = canonical.identity_key
    merged["title"] = canonical.title
    merged["primary_authors"] = canonical.primary_authors
    # The durable Candidate ID belongs to the canonical identity, not to
    # whichever merged record happened to carry the most fields.  Keeping the
    # retired ID would leave the merged record disagreeing with the alias
    # table that maps the canonical key.
    if canonical.candidate_id:
        merged["candidate_id"] = canonical.candidate_id
    if resolution is not None:
        resolution.apply_to(merged, canonical.identity_key)
    # A snapshot row that never carried a decision does not gain one.
    if decision_resolution is not None and any(_editor_decision(record) for record in records):
        decision_resolution.apply_to(merged)
    return merged


def _relabel_merged_new_row(
    row: dict[str, Any], origin_keys: set[str], matching: list[dict[str, Any]], canonical_key: str
) -> None:
    """Recompute a snapshot row's ``update_delta`` after a merge into an older record (issue #917).

    The pipeline labels a row ``new`` when its identity had no history. A
    merge can fold that row into a record first seen earlier, possibly one
    the editor already decided; the row is then not new. It is labelled
    against the older record exactly as the pipeline labels any row against
    its prior history, so it reads as previously reviewed, changed, or
    upgraded. Rows that were not ``new`` keep their label.
    """
    if str(row.get("update_delta", "")) != "new":
        return
    origin_dates = [
        str(record.get("first_seen_date", ""))
        for record in matching
        if _identity_key(record) in origin_keys and record.get("first_seen_date")
    ]
    if not origin_dates:
        return
    origin_first_seen = min(origin_dates)
    older = [
        record
        for record in matching
        if record.get("first_seen_date") and str(record["first_seen_date"]) < origin_first_seen
    ]
    if not older:
        return
    # Local import: the pipeline is the owner of delta labelling and imports
    # far more than this module needs at load time.
    from autoscout.pipeline import apply_update_delta_labels

    baseline = deepcopy(min(older, key=lambda record: (str(record["first_seen_date"]), _identity_key(record))))
    baseline["identity_key"] = canonical_key
    if row.get("candidate_id"):
        baseline["candidate_id"] = row["candidate_id"]
    apply_update_delta_labels([row], [baseline])


def _stored_alias_table(store: StateStore) -> dict[str, Any]:
    table: dict[str, Any] = store.read(Collection.CANDIDATE_IDENTITY_ALIASES)
    return table


def _pending_feedback_keys(
    rows: list[Any], merged_keys: set[str], matching: list[dict[str, Any]], table: dict[str, Any]
) -> list[str]:
    """Pending feedback keys that name a merged work, directly or through an alias.

    ``run-mvp`` resolves a returned row's key through the alias table and the
    durable-ID redirects, so an older alias of a merged work reaches the
    merged record too (review of #917).
    """

    def durable_id(key: str) -> str:
        aliases = table.get("aliases", {})
        stored = aliases.get(key) if isinstance(aliases, dict) else None
        return effective_candidate_id(str(stored) if stored else candidate_id_for(key), table)

    merged_ids = {
        effective_candidate_id(str(record.get("candidate_id", "")).strip(), table)
        if str(record.get("candidate_id", "")).strip()
        else durable_id(_identity_key(record))
        for record in matching
    } | {durable_id(key) for key in merged_keys}
    found: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        for field in FEEDBACK_ID_COLUMNS:
            key = str(row.get(field, "")).strip()
            if key and (key in merged_keys or key in merged_ids or durable_id(key) in merged_ids):
                found.add(key)
    return sorted(found)


def _plan_durable_redirect(
    store: StateStore,
    canonical_key: str,
    merged: dict[str, Any],
    matching: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str, list[str]]:
    """Redirect the durable IDs this merge retires onto the surviving one.

    Without it the retired Candidate ID keeps its stored aliases but loses its
    Candidate History record, so a later capture under the old title resolves
    to an ID that no longer carries the editor's decision.  The #456 approval
    path cannot repair that afterwards, because the source record is gone by
    then, so the redirect belongs to this operation (issue #559 review).

    Stored aliases are never rewritten; only the append-only redirect is
    recorded, exactly as the approved durable-merge path does. An empty alias
    table counts as no table, which is never created here: the store reads a
    missing table as an empty one.
    """
    survivor = str(merged.get("candidate_id", "")).strip()
    retired = sorted({str(record.get("candidate_id", "")).strip() for record in matching} - {survivor, ""})
    if not survivor or not retired:
        return None, survivor, []
    table = _stored_alias_table(store)
    if table == empty_alias_table():
        return None, survivor, []
    record_durable_id_merge(
        table,
        canonical_identity_key=canonical_key,
        survivor_candidate_id=survivor,
        retired_candidate_ids=retired,
        audit_entry={"source": "merge-candidate-identity"},
    )
    return table, survivor, retired


def plan_identity_correction(
    state_dir: Path,
    correction: dict[str, Any],
    state_store: StateStore | None = None,
) -> tuple[dict[Collection | str, Any], dict[str, Any]]:
    """Plan a correction: the new value of every State Store collection and run file it changes, and its report.

    Planning reads and writes nothing; *state_store* defaults to a store over
    *state_dir*, which also holds the run files.
    """
    store = open_state_store(state_dir) if state_store is None else state_store
    source_keys = {str(value).strip() for value in correction.get("source_identity_keys", []) if str(value).strip()}
    canonical = correction.get("canonical", {})
    canonical_key = str(canonical.get("identity_key", "")).strip()
    title = str(canonical.get("title", "")).strip()
    authors = canonical.get("primary_authors", [])
    if not source_keys or not canonical_key or not title or not isinstance(authors, list) or not authors:
        raise ValueError("correction requires source IDs and canonical identity_key, title, and primary_authors")
    authors = [str(author).strip() for author in authors if str(author).strip()]
    if not authors:
        raise ValueError("correction requires at least one nonblank primary author")

    history = store.read(Collection.CANDIDATE_HISTORY)
    if not isinstance(history, list):
        raise ValueError("candidate history must be a list")
    found = {_identity_key(record) for record in history if _identity_key(record) in source_keys}
    missing = source_keys - found
    if missing:
        raise ValueError(f"unknown source identity key(s): {', '.join(sorted(missing))}")
    matching = [record for record in history if _identity_key(record) in source_keys]
    target = next((record for record in history if _identity_key(record) == canonical_key), None)
    if target is not None and canonical_key not in source_keys:
        matching.append(target)
    canonical_merge = CanonicalMerge(
        identity_key=canonical_key,
        title=title,
        primary_authors=authors,
        candidate_id=str(target.get("candidate_id", "")).strip() if isinstance(target, dict) else "",
        description_resolution=parse_description_resolution(correction, matching),
        editor_decision_resolution=parse_editor_decision_resolution(correction, matching),
    )
    merged = _merge_history_records(matching, canonical_merge)
    resolution = canonical_merge.description_resolution
    _attempts, discarded_attempts = _merge_description_attempts(
        matching, chosen_identity_key=resolution.chosen_identity_key if resolution is not None else None
    )
    # Attempts an earlier merge set aside stay in the audit for good (#1023).
    all_discarded_attempts = _unique_list(
        *(
            record[DESCRIPTION_RESOLUTION_RECORD_FIELD].get(DISCARDED_ATTEMPTS_FIELD)
            for record in matching
            if isinstance(record.get(DESCRIPTION_RESOLUTION_RECORD_FIELD), dict)
        ),
        discarded_attempts,
    )
    if resolution is not None:
        merged[DESCRIPTION_RESOLUTION_RECORD_FIELD] = resolution.audit(canonical_key)
    if all_discarded_attempts:
        audit = dict(merged.get(DESCRIPTION_RESOLUTION_RECORD_FIELD) or {})
        audit["canonical_identity_key"] = canonical_key
        audit[DISCARDED_ATTEMPTS_FIELD] = all_discarded_attempts
        merged[DESCRIPTION_RESOLUTION_RECORD_FIELD] = audit
    decision_resolution = canonical_merge.editor_decision_resolution
    # Decisions an earlier merge set aside stay in the audit for good (review of #917).
    earlier_discarded = _unique_list(
        *(
            record[EDITOR_DECISION_RESOLUTION_RECORD_FIELD].get("discarded_decisions")
            for record in matching
            if isinstance(record.get(EDITOR_DECISION_RESOLUTION_RECORD_FIELD), dict)
        )
    )
    if decision_resolution is not None:
        audit = decision_resolution.audit(canonical_key)
        audit["discarded_decisions"] = _unique_list(earlier_discarded, audit["discarded_decisions"])
        merged[EDITOR_DECISION_RESOLUTION_RECORD_FIELD] = audit
        # Choosing between two decisions is itself a confirmed decision, logged
        # after every decision it replaces (issue #1053).
        merged[DECISION_LOG_FIELD] = merge_decision_logs(matching, keep_unlogged=True)
        record_resolved_decision(merged)
    elif earlier_discarded:
        audit = dict(merged.get(EDITOR_DECISION_RESOLUTION_RECORD_FIELD) or {})
        audit["canonical_identity_key"] = canonical_key
        audit["discarded_decisions"] = earlier_discarded
        merged[EDITOR_DECISION_RESOLUTION_RECORD_FIELD] = audit
    new_history = [record for record in history if _identity_key(record) not in source_keys | {canonical_key}]
    new_history.append(merged)

    planned: dict[Collection | str, Any] = {Collection.CANDIDATE_HISTORY: sort_by_key(new_history, "identity_key")}
    alias_table, survivor_candidate_id, retired_candidate_ids = _plan_durable_redirect(
        store, canonical_key, merged, matching
    )
    if alias_table is not None:
        planned[Collection.CANDIDATE_IDENTITY_ALIASES] = alias_table
    projection = CanonicalProjection.from_merged(merged)
    # With a description_resolution the history merge settled the attempt log,
    # so snapshot rows show exactly that log rather than resolving their own
    # (#1023 review): a row from a discarded record never keeps its attempt.
    settled_attempts = list(merged.get(DESCRIPTION_ATTEMPTS_FIELD) or []) if resolution is not None else None
    rewritten_references = 0
    for filename in REFERENCE_FILES:
        if isinstance(filename, Collection):
            records = store.read(filename)
            if records == []:
                # Nothing references the identity; the store reads a missing
                # collection as this empty one, which is left as it is.
                continue
        else:
            path = state_dir / filename
            if not path.exists():
                continue
            records = read_json_or_default(path, [])
        if not isinstance(records, list):
            raise ValueError(f"{reference_label(filename)} must be a list")
        if decision_resolution is not None and filename == Collection.PENDING_EDITOR_ROWS:
            pending = _pending_feedback_keys(
                records, source_keys | {canonical_key}, matching, alias_table or _stored_alias_table(store)
            )
            if pending:
                # Pending feedback would overwrite the chosen decision when it is
                # consumed, so it is consumed first (review of #917).
                raise ValueError(
                    "editor_decision_resolution cannot run while editor feedback is pending for "
                    f"{', '.join(pending)}; consume it with run-mvp first"
                )
        updated = deepcopy(records)
        # The identities whose rows carried the `new` label before rekeying: the
        # label's own origin, not every merged row, decides what is older
        # (review of #917).
        origin_keys = {
            _identity_key(record)
            for record in updated
            if isinstance(record, dict)
            and _identity_key(record) in source_keys | {canonical_key}
            and str(record.get("update_delta", "")) == "new"
        }
        for record in updated:
            if not isinstance(record, dict):
                continue
            for field in ("identity_key", "Candidate ID"):
                if str(record.get(field, "")).strip() in source_keys:
                    record[field] = canonical_key
                    rewritten_references += 1
        canonical_records = [record for record in updated if _identity_key(record) == canonical_key]
        if len(canonical_records) > 1 and filename in {
            Collection.LATEST_SCOUTING_CANDIDATES,
            "scouting-candidates.json",
        }:
            merged_reference = _merge_history_records(canonical_records, canonical_merge, settled_attempts)
            updated = [record for record in updated if _identity_key(record) != canonical_key]
            updated.append(merged_reference)
        elif len(canonical_records) > 1 and filename == Collection.CANDIDATE_VISIBILITY:
            conflicts = _reference_merge_conflicts(
                filename, canonical_records, decision_resolved=decision_resolution is not None
            )
            if conflicts:
                raise ValueError(conflicts[0][1])
            merged_reference = deepcopy(canonical_records[0])
            merged_reference["identity_key"] = canonical_key
            merged_reference["audit_log"] = _unique_list(*(record.get("audit_log") for record in canonical_records))
            updated = [record for record in updated if _identity_key(record) != canonical_key]
            updated.append(merged_reference)
        # Every surviving reference to the canonical identity is projected onto
        # the merged history record, whether one record was retained or several
        # were combined. A single retained reference would otherwise keep its
        # old durable ID, title, and description under the new identity key.
        for record in updated:
            if _identity_key(record) == canonical_key:
                projection.stamp(record)
                if settled_attempts is not None and DESCRIPTION_ATTEMPTS_FIELD in record:
                    record[DESCRIPTION_ATTEMPTS_FIELD] = deepcopy(settled_attempts)
                # A row that carries a decision shows the chosen one, so a later
                # snapshot-to-history update cannot restore a discarded one
                # (review of #917).
                if decision_resolution is not None and any(field in record for field in EDITOR_DECISION_FIELDS):
                    decision_resolution.apply_to(record)
                if filename in {Collection.LATEST_SCOUTING_CANDIDATES, "scouting-candidates.json"}:
                    _relabel_merged_new_row(record, origin_keys, matching, canonical_key)
        if (
            decision_resolution is not None
            and filename == SUPPRESSED_CANDIDATES_FILE
            and not decision_resolution.suppresses
        ):
            # Only a reject or already_known status suppresses a work, so the
            # entry goes when the editor chose another one (review of #917).
            updated = [record for record in updated if _identity_key(record) != canonical_key]
        if decision_resolution is not None and filename == MEMO_QUEUE_FILE:
            # The queue follows the chosen decision: a work no longer marked for
            # deeper analysis leaves it, one that stays shows the chosen note.
            if decision_resolution.queued_for_memo:
                for record in updated:
                    if _identity_key(record) == canonical_key and "editor_note" in record:
                        record["editor_note"] = str(decision_resolution.decision.get("editor_note", ""))
                kept: list[Any] = []
                for record in updated:
                    if _identity_key(record) == canonical_key and any(
                        _identity_key(item) == canonical_key for item in kept
                    ):
                        continue
                    kept.append(record)
                updated = kept
            else:
                updated = [record for record in updated if _identity_key(record) != canonical_key]
        planned[filename] = (
            sort_by_key(updated, "identity_key") if all(isinstance(item, dict) for item in updated) else updated
        )

    description_report = resolution.report() if resolution is not None else None
    if description_report is not None and discarded_attempts:
        # Present only when attempts were set aside, so every other report
        # keeps its exact shape (#1023).
        description_report["discarded_description_attempts"] = discarded_attempts
    report = {
        "canonical_identity_key": canonical_key,
        "canonical_title": title,
        "canonical_authors": list(authors),
        "source_identity_keys": sorted({_identity_key(record) for record in matching}),
        "merged_history_records": len(matching),
        "rewritten_references": rewritten_references,
        "survivor_candidate_id": survivor_candidate_id,
        "retired_candidate_ids": list(retired_candidate_ids),
        "preserved_editor_status": str(merged.get("editor_status", "")),
        "preserved_editor_note": str(merged.get("editor_note", "")),
        "content_description": str(merged.get("content_description", "")),
        "description_resolution": description_report,
    }
    if decision_resolution is not None:
        # Present only when the artifact resolves a decision, so the report of
        # every other merge keeps its exact shape (issue #917).
        report["editor_decision_resolution"] = decision_resolution.report()
    return planned, report


def apply_identity_correction(
    state_dir: Path,
    correction: dict[str, Any],
    *,
    dry_run: bool = False,
    state_store: StateStore | None = None,
) -> dict[str, Any]:
    """Plan the correction, and outside a dry run apply it in one State Store transaction.

    Planning and publication happen under one state-level lock, so a
    correction always plans from state no other correction is midway through
    replacing.  A dry run takes the same lock, so a preview never straddles
    another correction's publication; it reads and writes nothing else.

    The collections are planned from, and written in, one store transaction,
    and the run files of the state directory are published as one bundle
    inside it. A failure part way through leaves every collection and run
    file as it was, so the merge never leaves history merged and its
    references stale, and repeating the same correction after a failure
    starts from unchanged state (issue #559 review).
    """
    store = open_state_store(state_dir) if state_store is None else state_store
    with _state_write_lock(state_dir):
        if dry_run:
            _planned, preview = plan_identity_correction(state_dir, correction, store)
            return preview
        # The plan rewrites a run file only when it exists.
        destinations = [
            state_dir / filename
            for filename in REFERENCE_FILES
            if isinstance(filename, str) and (state_dir / filename).exists()
        ]
        applied: dict[str, Any] = {}

        def write_state() -> None:
            planned, applied["report"] = plan_identity_correction(state_dir, correction, store)
            applied["files"] = {name: value for name, value in planned.items() if isinstance(name, str)}
            for collection, value in planned.items():
                if isinstance(collection, Collection):
                    store.replace(collection, value)

        def publish_files() -> None:
            files: dict[str, Any] = applied["files"]
            if not files:
                return
            publish_bundle(
                [state_dir / filename for filename in files],
                [serialize_json(value) for value in files.values()],
                replace=lambda source, destination: _replace_state_file(source, destination),
            )

        publish_files_with_state(store, write_state, publish_files, destinations)
    report: dict[str, Any] = applied["report"]
    return report
