"""Canonical Candidate History application for grounded descriptions.

This module owns weekly enrichment orchestration while
``grounded_description`` keeps the public stage and service seams.
The implementation separates input loading, eligibility planning, stage
execution, outcome application, report construction, and bundle publication.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace as dataclass_replace
from datetime import date
from pathlib import Path
from typing import Any

from autoscout.candidate_identity import KeyedHistory
from autoscout.candidate_identity import empty_alias_table
from autoscout.candidate_identity import resolve_history_match
from autoscout.candidate_schema import CandidateIsbn
from autoscout.content_description import DESCRIPTION_FALLBACK
from autoscout.content_description import DESCRIPTION_NOT_YET_PUBLISHED_DATE_FIELD
from autoscout.content_description import DESCRIPTION_PM_LOGLINE_PROVENANCE
from autoscout.content_description import description_provenance_record
from autoscout.content_description import publication_recheck_pending
from autoscout.content_description import record_has_usable_description
from autoscout.content_description import validate_content_description
from autoscout.description_depth import DEPTH_MIN_CHARS
from autoscout.description_depth import DEPTH_MIN_SENTENCES
from autoscout.description_depth import assess_description_depth
from autoscout.description_depth import meets_description_depth
from autoscout.description_projection import project_history_descriptions
from autoscout.description_repair import REPAIR_EVIDENCE_SHORTFALL_REASONS
from autoscout.description_repair import is_repair_candidate
from autoscout.description_repair import pending_repair_handoff
from autoscout.description_repair import record_repair_attempt
from autoscout.description_repair import repair_exhausted
from autoscout.description_repair import repair_sort_key
from autoscout.description_repair import select_due_repairs
from autoscout.enrichment_lifecycle import COVERAGE_MEASURED
from autoscout.enrichment_lifecycle import COVERAGE_UNKNOWN
from autoscout.enrichment_lifecycle import lifecycle_band
from autoscout.enrichment_lifecycle import lifecycle_yield
from autoscout.enrichment_lifecycle import order_by_lifecycle_band
from autoscout.grounded_description import CANDIDATE_ENVELOPE_SECONDS
from autoscout.grounded_description import DISCOVERY_BUDGET_SECONDS
from autoscout.grounded_description import GROUNDING_QUEUE_FILENAME
from autoscout.grounded_description import GROUNDING_STAGE_ARTIFACT_NAME
from autoscout.grounded_description import MAX_FETCHES_PER_CANDIDATE
from autoscout.grounded_description import MAX_METADATA_VALUE_LENGTH
from autoscout.grounded_description import MAX_URL_LENGTH
from autoscout.grounded_description import MAX_WORKERS
from autoscout.grounded_description import OUTCOME_ATTEMPTED_NO_DESCRIPTION
from autoscout.grounded_description import OUTCOME_EVIDENCE_UNCHANGED
from autoscout.grounded_description import OUTCOME_GROUNDED
from autoscout.grounded_description import OUTCOME_NOT_ATTEMPTED
from autoscout.grounded_description import OUTCOME_ORDER
from autoscout.grounded_description import OUTCOME_SINGLE_SOURCE
from autoscout.grounded_description import REASON_AMBIGUITY
from autoscout.grounded_description import REASON_INSUFFICIENT_DEPTH
from autoscout.grounded_description import REASON_STAGE_EXECUTION_FAILURE
from autoscout.grounded_description import STAGE_BUDGET_SECONDS
from autoscout.grounded_description import GroundedDescriptionQueue
from autoscout.grounded_description import QueueEntry
from autoscout.grounded_description import StageArtifact
from autoscout.grounded_description import StageOutcome
from autoscout.grounded_description import accepted_evidence_urls
from autoscout.grounded_description import canonicalize_url
from autoscout.grounded_description import plausible_isbn
from autoscout.grounded_description import validate_stage_artifact
from autoscout.json_io import read_json_or_default
from autoscout.json_io import serialize_json
from autoscout.pm_publication import CITED_BEYOND_PM_DEALS_FIELD
from autoscout.pm_publication import PM_RECHECK_DATE_FIELD
from autoscout.progress_snapshot import CandidateProgressSnapshot
from autoscout.safe_artifacts import SafeArtifactRecoveryError
from autoscout.safe_artifacts import bundle_write_lock
from autoscout.safe_artifacts import publish_bundle
from autoscout.safe_artifacts import staged_replace
from autoscout.state_publication import publish_files_with_state
from autoscout.state_store import Collection
from autoscout.state_store import StateStore
from autoscout.state_store import open_run_output_store
from autoscout.state_store import open_state_store
from autoscout.weekly_report import RUN_HISTORY_COPY
from autoscout.weekly_report import history_in_output_shards
from autoscout.weekly_report import keeps_run_history_copy
from autoscout.weekly_report import read_run_history

# This is an internal replace seam for deterministic rollback tests.
_staged_replace = staged_replace

_DESCRIPTION_FIELDS = (
    "content_description",
    "description_provenance",
    "grounded_outcome",
    "content_description_rejection_reason",
    "grounded_description_attempts",
)

# Learned-ISBN fields copied under the enrichment-copy contract. The copy is
# deliberately fill-only (issue #531): a nonblank current or historical ISBN
# is never replaced, so these fields cannot join the plain replace loop above.
_ISBN_DISAGREEMENT_FIELD = "isbn_disagreement"

# Bounded disagreement evidence (issue #531). The applied date is a plain
# ISO operation date, so its honest bound is the short checked-date length,
# not the generic metadata limit. Entries are bounded by the per-candidate
# fetch budget; each source URL keeps the URL length bound.
_DISAGREEMENT_APPLIED_DATE_LENGTH = 50
_DISAGREEMENT_ENTRY_LIMIT = MAX_FETCHES_PER_CANDIDATE
_DISAGREEMENT_SOURCE_URL_LENGTH = MAX_URL_LENGTH

REASON_INVALID_SCOUTING_CANDIDATES = "invalid_scouting_candidates"
REASON_INVALID_CANDIDATE_IDENTITY_ALIASES = "invalid_candidate_identity_aliases"


@dataclass
class _LoadedInputs:
    output_dir: Path
    state_dir: Path | None
    current_candidates: list[dict[str, Any]]
    history: list[dict[str, Any]]
    queue_path: Path
    visibility: list[Mapping[str, Any]]
    aliases: Mapping[str, Any]
    operation_id: str
    operation_date: str
    # The latest-candidates snapshot is derived publication, not canonical
    # state. It is loaded when present so description facts can be projected
    # onto it (issue #671); ``None`` means there is no snapshot to project
    # onto and none is created here.
    latest_candidates: list[dict[str, Any]] | None = None
    # The store canonical state is read from and written to: the run's, or
    # one over the state directory. Without a state directory it is a store
    # over the output directory, which then holds no Candidate History of
    # its own: the run's copy stands in for it and travels with the run's
    # files.
    store: StateStore | None = None
    # Repair admissions for this run (issue #674): candidate IDs whose
    # thin-but-usable descriptions were admitted for depth repair. Used to
    # log repair attempts separately from content, and to report repair
    # progress. Populated by planning, after inputs are loaded.
    repair_candidate_ids: set[str] = field(default_factory=set)


@dataclass
class _StageExecution:
    artifact: StageArtifact
    status: str
    failure_reason: str = ""


@dataclass
class _AppliedOutcomes:
    history: list[dict[str, Any]]
    current_candidates: list[dict[str, Any]]
    report_records: list[dict[str, Any]]
    applied_ids: set[str]
    failed_ids: set[str]
    ambiguous_ids: set[str]
    still_fallback_ids: set[str]
    skip_reasons: dict[str, str]
    current_projection: dict[str, Any] | None = None
    latest_projection: dict[str, Any] | None = None
    repair_summary: dict[str, Any] | None = None
    lifecycle_summary: dict[str, Any] | None = None


def _candidate_id(record: Mapping[str, Any]) -> str:
    return str(record.get("candidate_id", "")).strip()


def _isbn_disagreement_record(
    values: Sequence[str], learned: Sequence[tuple[str, str]], applied_date: str
) -> dict[str, Any]:
    """Build the bounded disagreement record for one candidate (issue #531)."""
    return {
        "applied_date": applied_date[:_DISAGREEMENT_APPLIED_DATE_LENGTH],
        "values": [
            {"isbn": isbn, "source_url": url[:_DISAGREEMENT_SOURCE_URL_LENGTH]}
            for isbn in values
            for url in sorted({item for value, item in learned if value == isbn})
        ][:_DISAGREEMENT_ENTRY_LIMIT],
    }


def _learn_candidate_isbn(record: dict[str, Any], sources: Sequence[Mapping[str, Any]], applied_date: str) -> None:
    """Learn one candidate ISBN from accepted grounded sources (issue #531).

    Only normalized plausible ISBN-10/ISBN-13 values from accepted
    ``StageOutcome.research_sources`` count, and learning is gated to the
    ``OUTCOME_GROUNDED`` and ``OUTCOME_SINGLE_SOURCE`` outcomes by the
    caller.  When every plausible learned ISBN normalizes to one value,
    that value fills an empty candidate ISBN and is marked discovery-only
    so identity matching never treats it as proof of the same work, and a
    stale disagreement record is cleared.  When two accepted sources
    disagree, a deterministic bounded disagreement record is persisted in
    the candidate-only ``isbn_disagreement`` field.  A previously learned
    discovery-only ISBN is cleared with its marker before the disagreement
    is persisted; a trusted unmarked existing ISBN is neither cleared nor
    overwritten.
    """
    learned: list[tuple[str, str]] = []
    for source in sources:
        isbn = plausible_isbn(str(source.get("isbn", "")))
        if isbn:
            learned.append((isbn, str(source.get("url", source.get("canonical_url", "")))))
    if not learned:
        return
    values = sorted({isbn for isbn, _url in learned})
    existing = CandidateIsbn.from_record(record)
    if len(values) > 1:
        if existing.discovery_only:
            # The learned ISBN lost its single agreed value.
            existing.clear_from(record)
        record[_ISBN_DISAGREEMENT_FIELD] = _isbn_disagreement_record(values, learned, applied_date)
        return
    if existing:
        # Fill-only: an existing candidate ISBN is never overwritten.
        return
    CandidateIsbn(value=values[0], discovery_only=True).apply_to(record)
    # A later single agreed value clears a stale disagreement.
    record.pop(_ISBN_DISAGREEMENT_FIELD, None)


def _copy_isbn_fields(source: Mapping[str, Any], target: dict[str, Any]) -> None:
    """Copy the ISBN pair and disagreement record inside the copy contract.

    Fill-only: a nonblank target ISBN is never replaced, and a trusted
    unmarked target ISBN is never cleared.  When the history record no
    longer holds an ISBN because a disagreement cleared a learned value,
    the stale learned copy in the target is cleared with its marker, so
    the two persisted copies never disagree (issue #531).
    """
    source_isbn = CandidateIsbn.from_record(source)
    target_isbn = CandidateIsbn.from_record(target)
    if source_isbn and not target_isbn:
        source_isbn.apply_to(target)
    elif not source_isbn and target_isbn.discovery_only:
        target_isbn.clear_from(target)
    if _ISBN_DISAGREEMENT_FIELD in source:
        target[_ISBN_DISAGREEMENT_FIELD] = source[_ISBN_DISAGREEMENT_FIELD]
    else:
        target.pop(_ISBN_DISAGREEMENT_FIELD, None)


def _description_provenance(record: Mapping[str, Any]) -> str:
    raw = record.get("description_provenance")
    if isinstance(raw, Mapping):
        return str(raw.get("provenance", "")).strip()
    return str(raw or "").strip()


def _valid_codex_description(record: Mapping[str, Any]) -> bool:
    return _description_provenance(record) == "codex-generated" and record_has_usable_description(record)


def _eligible_description(record: Mapping[str, Any]) -> bool:
    if _description_provenance(record) == "editor-corrected":
        return False
    # A description written from the PM deal logline waits for one grounded
    # on its own, which replaces it (issue #950).
    return not record_has_usable_description(record) or _description_provenance(record) in {
        "codex-generated",
        DESCRIPTION_PM_LOGLINE_PROVENANCE,
    }


def _operation_day(operation_date: str) -> date:
    try:
        return date.fromisoformat(str(operation_date).strip())
    except ValueError:
        return date.today()


def _attempt_history(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = record.get("grounded_description_attempts", [])
    return [item for item in raw if isinstance(item, Mapping)] if isinstance(raw, list) else []


def _hidden_candidate_keys(records: Sequence[Mapping[str, Any]]) -> tuple[set[str], set[str]]:
    identities: set[str] = set()
    candidate_ids: set[str] = set()
    for record in records:
        if str(record.get("visibility", "")).strip().casefold() != "hidden":
            continue
        identity_key = str(record.get("identity_key", "")).strip()
        candidate_id = str(record.get("candidate_id", "")).strip()
        if identity_key:
            identities.add(identity_key)
        if candidate_id:
            candidate_ids.add(candidate_id)
    return identities, candidate_ids


def _enrichment_candidates(
    current_candidates: Sequence[Mapping[str, Any]],
    history: Sequence[Mapping[str, Any]],
    aliases: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Merge current facts with authoritative historical description fields."""
    history_records = [dict(item) for item in history if isinstance(item, Mapping)]
    table = dict(aliases)
    try:
        keyed_history = KeyedHistory.of(history_records)
    except (TypeError, ValueError):
        keyed_history = None
    merged: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for source in (*current_candidates, *history_records):
        candidate_id = _candidate_id(source)
        if not candidate_id:
            continue
        item = dict(source)
        try:
            matched, durable_id = resolve_history_match(
                item, keyed_history if keyed_history is not None else history_records, table
            )
        except (TypeError, ValueError):
            matched, durable_id = None, candidate_id
        if matched is not None:
            matched_id = _candidate_id(matched) or str(durable_id or candidate_id).strip()
            if matched_id:
                item["candidate_id"] = matched_id
            matched_key = str(matched.get("identity_key", "")).strip()
            if matched_key:
                item["identity_key"] = matched_key
            for field_name in _DESCRIPTION_FIELDS:
                if field_name in matched:
                    item[field_name] = matched[field_name]
            # Candidate History owns the not-yet-published date, so a book
            # collected again this week still waits for its re-check date,
            # and a date history no longer holds does not linger (issue #791).
            if DESCRIPTION_NOT_YET_PUBLISHED_DATE_FIELD in matched:
                item[DESCRIPTION_NOT_YET_PUBLISHED_DATE_FIELD] = matched[DESCRIPTION_NOT_YET_PUBLISHED_DATE_FIELD]
            else:
                item.pop(DESCRIPTION_NOT_YET_PUBLISHED_DATE_FIELD, None)
            # History remembers an earlier non-PM citation the weekly
            # snapshot no longer shows (issue #947).
            if matched.get(CITED_BEYOND_PM_DEALS_FIELD) is True:
                item[CITED_BEYOND_PM_DEALS_FIELD] = True
            if matched.get(PM_RECHECK_DATE_FIELD):
                item[PM_RECHECK_DATE_FIELD] = matched[PM_RECHECK_DATE_FIELD]
            _copy_isbn_fields(matched, item)
        resolved_candidate_id = _candidate_id(item)
        if not resolved_candidate_id or resolved_candidate_id in seen_ids:
            continue
        seen_ids.add(resolved_candidate_id)
        merged.append(item)
    return merged


def _load_inputs(
    output_dir: Path,
    state_dir: Path | None,
    effective_date: str | None,
    operation_id: str | None,
    state_store: StateStore | None = None,
) -> _LoadedInputs | dict[str, Any] | None:
    try:
        candidates_value = read_json_or_default(output_dir / "scouting-candidates.json", [])
    except (OSError, TypeError, ValueError):
        return {"status": "input_failed", "reason": REASON_INVALID_SCOUTING_CANDIDATES}
    if not isinstance(candidates_value, list) or any(not isinstance(item, Mapping) for item in candidates_value):
        return {"status": "input_failed", "reason": REASON_INVALID_SCOUTING_CANDIDATES}
    current_candidates = [dict(item) for item in candidates_value]
    if state_dir is not None:
        store = state_store if state_store is not None else open_state_store(state_dir)
        history_value = store.read(Collection.CANDIDATE_HISTORY)
    else:
        store = open_run_output_store(output_dir)
        history_value = read_run_history(output_dir)
    history = (
        [dict(item) for item in history_value if isinstance(item, Mapping)] if isinstance(history_value, list) else []
    )
    if not current_candidates and not history:
        return None

    aliases = empty_alias_table()
    if state_dir is not None:
        try:
            aliases = store.read(Collection.CANDIDATE_IDENTITY_ALIASES)
        except (OSError, ValueError):
            return {"status": "input_failed", "reason": REASON_INVALID_CANDIDATE_IDENTITY_ALIASES}
    visibility_value = store.read(Collection.CANDIDATE_VISIBILITY)
    visibility = (
        [item for item in visibility_value if isinstance(item, Mapping)] if isinstance(visibility_value, list) else []
    )
    resolved_operation_id = str(operation_id or effective_date or "operation").strip() or "operation"
    resolved_operation_date = str(effective_date or "").strip() or date.today().isoformat()
    # The latest-candidates snapshot is derived publication owned by the
    # pipeline's run-state save. It is loaded best-effort here so the
    # description projection (issue #671) can refresh it alongside the
    # current candidates. A store without latest candidates has no snapshot
    # to refresh; an invalid one fails closed like the other inputs.
    try:
        latest_value = store.read(Collection.LATEST_SCOUTING_CANDIDATES)
    except (OSError, TypeError, ValueError):
        return {"status": "input_failed", "reason": "invalid_latest_scouting_candidates"}
    if not isinstance(latest_value, list) or any(not isinstance(item, Mapping) for item in latest_value):
        return {"status": "input_failed", "reason": "invalid_latest_scouting_candidates"}
    latest_candidates = [dict(item) for item in latest_value] if latest_value else None
    return _LoadedInputs(
        output_dir=output_dir,
        state_dir=state_dir,
        current_candidates=current_candidates,
        history=history,
        queue_path=(state_dir or output_dir) / GROUNDING_QUEUE_FILENAME,
        visibility=visibility,
        aliases=aliases,
        operation_id=resolved_operation_id,
        operation_date=resolved_operation_date,
        latest_candidates=latest_candidates,
        store=store,
    )


def _plan_eligibility(
    inputs: _LoadedInputs,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str], dict[str, Any]]:
    hidden_keys, hidden_ids = _hidden_candidate_keys(inputs.visibility)
    table = dict(inputs.aliases)
    try:
        keyed_history = KeyedHistory.of(inputs.history)
    except (TypeError, ValueError):
        keyed_history = None
    for hidden_record in inputs.visibility:
        if str(hidden_record.get("visibility", "")).strip().casefold() != "hidden":
            continue
        try:
            _matched, hidden_candidate_id = resolve_history_match(
                dict(hidden_record), keyed_history if keyed_history is not None else inputs.history, table
            )
        except (TypeError, ValueError):
            hidden_candidate_id = None
        if hidden_candidate_id:
            hidden_ids.add(str(hidden_candidate_id).strip())

    candidates = _enrichment_candidates(inputs.current_candidates, inputs.history, inputs.aliases)
    skip_reasons: dict[str, str] = {}
    eligible: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_id = _candidate_id(candidate)
        identity_key = str(candidate.get("identity_key", "")).strip()
        if not candidate_id:
            continue
        if candidate_id in hidden_ids or identity_key in hidden_keys:
            skip_reasons[candidate_id] = "hidden_candidate"
            continue
        if _description_provenance(candidate) == "editor-corrected":
            skip_reasons[candidate_id] = "editor_corrected"
            continue
        # A book found not yet published has no page to search for until
        # its re-check date, as fresh work or as a repair (issue #791).
        if publication_recheck_pending(candidate, _operation_day(inputs.operation_date)):
            skip_reasons[candidate_id] = "awaiting_publication"
            continue
        # Repair classification runs before fresh-work eligibility (review
        # finding on #674): a thin-but-usable legacy record — including a
        # thin codex-generated one — is repair work with bounded retries
        # and honest accounting, never unbounded fresh work. Records with
        # missing text and sufficient codex records still flow below.
        if is_repair_candidate(candidate):
            continue
        if not _eligible_description(candidate):
            skip_reasons[candidate_id] = "valid_existing_description"
            continue
        eligible.append(candidate)
    repair_plan = _plan_depth_repair(
        inputs,
        candidates,
        {id for id in (_candidate_id(item) for item in eligible) if id},
        hidden_ids,
        hidden_keys,
        skip_reasons,
    )
    eligible.extend(repair_plan["admitted"])
    # Lifecycle band ordering (issue #699): new candidates first, then
    # updated, then previously seen. The sort is stable, so fresh work
    # still precedes repairs inside a band and repairs keep the fairness
    # order planned above. This is a reordering, not a filter — the
    # eligible set is unchanged, only the order the budget consumes it in.
    eligible = order_by_lifecycle_band(eligible)
    return candidates, eligible, skip_reasons, repair_plan


def _plan_depth_repair(
    inputs: _LoadedInputs,
    candidates: list[dict[str, Any]],
    eligible_ids: set[str],
    hidden_ids: set[str],
    hidden_keys: set[str],
    skip_reasons: dict[str, str],
) -> dict[str, Any]:
    """Admit due depth repairs behind fresh work (issue #674).

    Repair candidates are thin-but-usable history records of the three
    approved legacy provenances. Fresh work (missing/fallback/codex) keeps
    priority: repairs append after it in stage order. Each admitted repair
    carries ``_force_synthesis`` on its attempt copy — never on the
    record — so unchanged evidence URLs still reach the model. Hidden and
    editor-corrected rows are never admitted. Cooling and exhausted
    records are reported as skipped with their own reasons.
    """
    from datetime import date as _date

    try:
        today = _date.fromisoformat(str(inputs.operation_date).strip())
    except ValueError:
        today = _date.today()
    # Partition only records the repair route can actually admit (review
    # finding): hidden and editor-corrected rows are excluded before the
    # partition, so backlog and cooldown/exhaustion counts never include
    # candidates no route is eligible to touch. is_repair_candidate
    # already excludes editor-corrected records; hidden filtering must
    # happen here because select_due_repairs knows nothing about
    # visibility.
    visible_history = [
        record
        for record in inputs.history
        if isinstance(record, Mapping)
        and str(record.get("candidate_id", "")).strip() not in hidden_ids
        and str(record.get("identity_key", "")).strip() not in hidden_keys
        and _description_provenance(record) != "editor-corrected"
        # A book that is not yet published has no page a repair could
        # find either, so it waits here as it does for fresh work.
        and not publication_recheck_pending(record, today)
    ]
    partition = select_due_repairs(visible_history, today)
    by_candidate_id = {_candidate_id(item): item for item in candidates if _candidate_id(item)}
    admitted: list[dict[str, Any]] = []
    admitted_ids: set[str] = set()
    deferred_handoff: list[str] = []
    # Handoff deferral runs before due/cooling routing: a record the
    # residual route owns is usually still cooling from the grounded
    # failure that handed it off, so partition order alone would hide it.
    for item in inputs.history:
        if not isinstance(item, Mapping) or not pending_repair_handoff(item, today):
            continue
        candidate_id = str(item.get("candidate_id", "")).strip()
        identity_key = str(item.get("identity_key", "")).strip()
        merged = by_candidate_id.get(candidate_id) if candidate_id else None
        if merged is None and identity_key:
            merged = next(
                (entry for entry in candidates if str(entry.get("identity_key", "")).strip() == identity_key),
                None,
            )
        if merged is None:
            continue
        merged_id = _candidate_id(merged)
        if not merged_id or merged_id in hidden_ids or str(merged.get("identity_key", "")).strip() in hidden_keys:
            continue
        if _description_provenance(merged) == "editor-corrected":
            continue
        # This run's residual stage owns the next try: grounded yields
        # instead of consuming another attempt (review finding on #675).
        deferred_handoff.append(merged_id)
        if merged_id not in skip_reasons:
            skip_reasons[merged_id] = "repair_handoff"
    handoff_ids = set(deferred_handoff)
    for record in sorted(partition["due"], key=lambda item: (repair_sort_key(item), str(item.get("candidate_id", "")))):
        candidate_id = str(record.get("candidate_id", "")).strip()
        identity_key = str(record.get("identity_key", "")).strip()
        merged = by_candidate_id.get(candidate_id) if candidate_id else None
        if merged is None and identity_key:
            merged = next(
                (item for item in candidates if str(item.get("identity_key", "")).strip() == identity_key),
                None,
            )
        if merged is None:
            continue
        merged_id = _candidate_id(merged)
        if not merged_id or merged_id in eligible_ids or merged_id in admitted_ids:
            continue
        if merged_id in hidden_ids or str(merged.get("identity_key", "")).strip() in hidden_keys:
            continue
        if _description_provenance(merged) == "editor-corrected":
            continue
        if merged_id in handoff_ids:
            continue
        merged["_force_synthesis"] = True
        admitted.append(merged)
        admitted_ids.add(merged_id)
        # The admission overrides the fresh-work skip verdict recorded
        # above: this record runs as a repair, so the report must show one
        # result for it, not "skipped" beside "applied".
        skip_reasons.pop(merged_id, None)
    for record in (*partition["cooling_down"], *partition["exhausted"]):
        candidate_id = str(record.get("candidate_id", "")).strip()
        if not candidate_id or candidate_id in skip_reasons:
            continue
        skip_reasons[candidate_id] = "repair_exhausted" if repair_exhausted(record) else "repair_cooldown"
    return {
        "backlog": len(partition["due"]) + len(partition["cooling_down"]) + len(partition["exhausted"]),
        "admitted": admitted,
        "admitted_ids": admitted_ids,
        "deferred_cooldown": [str(item.get("candidate_id", "")) for item in partition["cooling_down"]],
        "deferred_exhausted": [str(item.get("candidate_id", "")) for item in partition["exhausted"]],
        "deferred_handoff": deferred_handoff,
    }


def _stage_failure_artifact(
    candidates: Sequence[Mapping[str, Any]],
    queue: GroundedDescriptionQueue,
    *,
    operation_id: str,
    operation_date: str,
    reason_code: str,
    stage_budget_seconds: float,
) -> StageArtifact:
    outcomes: list[StageOutcome] = []
    for candidate in candidates:
        candidate_id = _candidate_id(candidate)
        if not candidate_id:
            continue
        old = queue.entries.get(candidate_id)
        if old is None:
            old = QueueEntry(
                candidate_id=candidate_id,
                queue_entry_operation=operation_id,
                queue_entry_date=operation_date,
                first_seen_date=str(candidate.get("first_seen_date", "") or operation_date),
                priority_label=str(candidate.get("priority_label", "D")).strip().upper() or "D",
            )
        waiting = dataclass_replace(old, attempt_status="waiting", reason_code=reason_code)
        queue.update(waiting)
        outcomes.append(
            StageOutcome(
                candidate_id=candidate_id,
                outcome=OUTCOME_ATTEMPTED_NO_DESCRIPTION,
                reason_code=reason_code,
                identity_key=str(candidate.get("identity_key", "")),
                scheduling=waiting,
            )
        )
    return StageArtifact(
        operation_id=operation_id,
        operation_date=operation_date,
        outcomes=tuple(outcomes),
        queue=tuple(queue.entries[key] for key in sorted(queue.entries)),
        stage_budget_seconds=stage_budget_seconds,
    )


def _failed_stage_execution(
    stage: Any,
    stage_candidates: Sequence[Mapping[str, Any]],
    inputs: _LoadedInputs,
    queue: GroundedDescriptionQueue,
) -> _StageExecution:
    return _StageExecution(
        _stage_failure_artifact(
            stage_candidates,
            queue,
            operation_id=inputs.operation_id,
            operation_date=inputs.operation_date,
            reason_code=REASON_STAGE_EXECUTION_FAILURE,
            stage_budget_seconds=float(getattr(stage, "stage_budget_seconds", STAGE_BUDGET_SECONDS)),
        ),
        "failed",
        REASON_STAGE_EXECUTION_FAILURE,
    )


def _execute_stage(
    stage: Any,
    stage_candidates: Sequence[Mapping[str, Any]],
    inputs: _LoadedInputs,
    queue: GroundedDescriptionQueue,
    repair_rank: Mapping[str, int] | None = None,
    snapshot_tracker: CandidateProgressSnapshot | None = None,
) -> _StageExecution:
    history_by_id = {_candidate_id(record): record for record in inputs.history if _candidate_id(record)}
    eligible_ids = {_candidate_id(candidate) for candidate in stage_candidates}
    try:
        artifact = stage.run(
            stage_candidates,
            operation_id=inputs.operation_id,
            operation_date=inputs.operation_date,
            queue=queue,
            existing_candidate_ids=set(history_by_id),
            eligible_candidate_ids=eligible_ids,
            history=inputs.history,
            aliases=inputs.aliases,
            force_candidates=False,
            repair_rank=repair_rank,
            snapshot_tracker=snapshot_tracker,
        )
    except Exception:
        if snapshot_tracker is not None:
            # A legacy stage double that ignores the tracker seam can still
            # raise here; the stage's own run() already finalizes when it
            # honors the seam, and a second final is an idempotent publish.
            snapshot_tracker.final(status="failed")
        return _failed_stage_execution(stage, stage_candidates, inputs, queue)
    if not validate_stage_artifact(artifact):
        return _failed_stage_execution(stage, stage_candidates, inputs, queue)
    return _StageExecution(artifact, "ok")


def _record_attempt(record: dict[str, Any], outcome: StageOutcome, inputs: _LoadedInputs) -> None:
    attempts = _attempt_history(record)
    attempt: dict[str, Any] = {
        "date": inputs.operation_date,
        "operation_id": inputs.operation_id,
        "outcome": outcome.outcome,
        "reason_code": outcome.reason_code,
    }
    if outcome.rejected_source_reasons:
        reason_counts: dict[str, int] = {}
        for rejected in outcome.rejected_source_reasons:
            reason_counts[rejected.reason_code] = reason_counts.get(rejected.reason_code, 0) + 1
        attempt["rejected_reason_counts"] = reason_counts
    replacement_index = next(
        (
            index
            for index, item in enumerate(attempts)
            if str(item.get("operation_id", "")).strip() == inputs.operation_id
        ),
        None,
    )
    updated_attempts = list(attempts)
    if replacement_index is None:
        updated_attempts.append(attempt)
    else:
        updated_attempts[replacement_index] = attempt
    record["grounded_description_attempts"] = updated_attempts
    # Repair attempts are logged separately from content (issue #674), but
    # only when synthesis was actually attempted: budget and unchanged
    # outcomes defer without starting cooldown.
    if _candidate_id(record) in inputs.repair_candidate_ids and outcome.outcome not in {
        OUTCOME_NOT_ATTEMPTED,
        OUTCOME_EVIDENCE_UNCHANGED,
    }:
        record_repair_attempt(
            record,
            operation_date=inputs.operation_date,
            operation_id=inputs.operation_id,
            outcome=outcome.outcome,
            reason_code=outcome.reason_code,
            stage="grounded",
        )


def _set_fallback(record: dict[str, Any], reason_code: str, operation_date: str) -> None:
    record["content_description"] = DESCRIPTION_FALLBACK
    record["description_provenance"] = description_provenance_record(
        str(record.get("identity_key", "")),
        DESCRIPTION_FALLBACK,
        "fallback",
        applied_date=operation_date,
    )
    if reason_code:
        record["content_description_rejection_reason"] = reason_code[:200]


def _record_for(
    history: Sequence[dict[str, Any]],
    history_by_id: Mapping[str, dict[str, Any]],
    candidate_id: str,
    identity_key: str,
) -> dict[str, Any] | None:
    direct = history_by_id.get(candidate_id)
    if direct is not None and str(direct.get("identity_key", "")).strip() == identity_key:
        return direct
    return next(
        (
            record
            for record in history
            if _candidate_id(record) == candidate_id and str(record.get("identity_key", "")).strip() == identity_key
        ),
        None,
    )


def _apply_outcomes(
    inputs: _LoadedInputs,
    candidates: Sequence[Mapping[str, Any]],
    stage_candidates: Sequence[Mapping[str, Any]],
    execution: _StageExecution,
    skip_reasons: dict[str, str],
    repair_plan: dict[str, Any] | None = None,
) -> _AppliedOutcomes:
    history = inputs.history
    outcomes_by_id = {outcome.candidate_id: outcome for outcome in execution.artifact.outcomes}
    history_by_id = {_candidate_id(record): record for record in history if _candidate_id(record)}
    applied_ids: set[str] = set()
    failed_ids: set[str] = set()
    ambiguous_ids: set[str] = set()
    still_fallback_ids: set[str] = set()

    for candidate in stage_candidates:
        candidate_id = _candidate_id(candidate)
        outcome = outcomes_by_id.get(candidate_id)
        record = _record_for(history, history_by_id, candidate_id, str(candidate.get("identity_key", "")).strip())
        if record is None:
            if outcome is not None and outcome.reason_code == REASON_AMBIGUITY:
                ambiguous_ids.add(candidate_id)
            elif outcome is not None and outcome.outcome == OUTCOME_ATTEMPTED_NO_DESCRIPTION:
                failed_ids.add(candidate_id)
            continue
        if execution.failure_reason:
            if outcome is not None:
                _record_if_unconfirmed(record, outcome, inputs)
                failed_ids.add(candidate_id)
            continue
        # Learning is explicitly gated to the two successful outcomes: an
        # attempted or other outcome that happens to carry research sources
        # must never learn (issue #531).
        if (
            outcome is not None
            and outcome.outcome in {OUTCOME_GROUNDED, OUTCOME_SINGLE_SOURCE}
            and outcome.research_sources
        ):
            _learn_candidate_isbn(
                record, [source.to_dict() for source in outcome.research_sources], inputs.operation_date
            )
        if outcome is None:
            if _valid_codex_description(record):
                # No stage outcome is itself a bounded stage failure for this ID.
                synthetic = StageOutcome(
                    candidate_id=candidate_id,
                    outcome=OUTCOME_ATTEMPTED_NO_DESCRIPTION,
                    reason_code=REASON_STAGE_EXECUTION_FAILURE,
                    identity_key=str(record.get("identity_key", "")),
                )
                _record_attempt(record, synthetic, inputs)
                failed_ids.add(candidate_id)
            continue
        if outcome.outcome in {OUTCOME_GROUNDED, OUTCOME_SINGLE_SOURCE}:
            validated, _reason = validate_content_description(outcome.content_description)
            if validated is not None and not meets_description_depth(validated):
                # Depth backstop at the application boundary (review finding
                # on #673): a shallow success that reached application
                # outside the synthesis gate — replay, service double, or
                # hand-built artifact — is reported as a failed attempt,
                # never persisted as grounded. It flows through the ordinary
                # failure handling below, including preservation and repair
                # bookkeeping.
                depth = assess_description_depth(validated)
                outcome = dataclass_replace(
                    outcome,
                    outcome=OUTCOME_ATTEMPTED_NO_DESCRIPTION,
                    reason_code=REASON_INSUFFICIENT_DEPTH,
                    reason_detail=(
                        f"below depth floor: {depth['char_count']} chars, "
                        f"{depth['sentence_count']} sentences "
                        f"(floor {DEPTH_MIN_CHARS} chars, {DEPTH_MIN_SENTENCES} sentences)"
                    ),
                    content_description="",
                    research_sources=(),
                    prominence_claims=(),
                )
                outcomes_by_id[candidate_id] = outcome
        if outcome.outcome in {OUTCOME_GROUNDED, OUTCOME_SINGLE_SOURCE}:
            validated, _reason = validate_content_description(outcome.content_description)
            # A repair admission carries its own write permission (issue
            # #674): the record was thin-but-usable, so the fresh
            # eligibility check alone would drop the qualified replacement
            # the repair run just produced.
            repair_admitted = candidate_id in inputs.repair_candidate_ids
            if (_eligible_description(record) or repair_admitted) and validated is not None:
                urls = accepted_evidence_urls(outcome)
                record["content_description"] = validated
                record["grounded_outcome"] = outcome.outcome
                record["description_provenance"] = description_provenance_record(
                    str(record.get("identity_key", outcome.identity_key)),
                    validated,
                    "grounded",
                    evidence_basis=urls,
                    applied_date=inputs.operation_date,
                    grounding_evidence_url=urls[0] if urls else None,
                    english_description=outcome.english_description or None,
                )
                record.pop("content_description_rejection_reason", None)
                _record_attempt(record, outcome, inputs)
                applied_ids.add(candidate_id)
            else:
                skip_reasons[candidate_id] = "valid_existing_description"
            continue

        if outcome.outcome not in {OUTCOME_NOT_ATTEMPTED, OUTCOME_EVIDENCE_UNCHANGED} or _valid_codex_description(
            record
        ):
            _record_attempt(record, outcome, inputs)
        if outcome.reason_code == REASON_AMBIGUITY:
            ambiguous_ids.add(candidate_id)
        elif outcome.outcome == OUTCOME_ATTEMPTED_NO_DESCRIPTION:
            failed_ids.add(candidate_id)
        # An unsuccessful attempt never destroys usable text (issue #673):
        # the attempt is logged above as diagnostics, while the retained
        # description and its provenance survive until a qualified
        # replacement succeeds. Only a record with nothing usable keeps the
        # standard fallback with the attempt's bounded reason.
        if not record_has_usable_description(record):
            _set_fallback(record, outcome.reason_code, inputs.operation_date)

    for candidate in stage_candidates:
        candidate_id = _candidate_id(candidate)
        record = _record_for(history, history_by_id, candidate_id, str(candidate.get("identity_key", "")).strip())
        if record is not None and not record_has_usable_description(record):
            still_fallback_ids.add(candidate_id)

    def _project_isbn_on_match(row: dict[str, Any], matched: Mapping[str, Any]) -> None:
        # The learned-ISBN fill-only copy contract (issue #531) travels with
        # the description projection for current candidates, exactly as the
        # bespoke copy loop did before the shared projection (issue #671).
        # A nonblank target ISBN is never replaced; disagreement records
        # travel with the source record.
        _copy_isbn_fields(matched, row)

    projected_current, current_projection = project_history_descriptions(
        history,
        inputs.current_candidates,
        inputs.aliases,
        on_match=_project_isbn_on_match,
    )
    inputs.current_candidates = projected_current
    latest_projection: dict[str, Any] | None = None
    if inputs.latest_candidates is not None:
        # The ISBN stage (issue #979) fills the latest snapshot too, so a
        # disagreement that clears a learned history ISBN must clear it
        # there as well, through the same fill-only copy contract.
        projected_latest, latest_projection = project_history_descriptions(
            history,
            inputs.latest_candidates,
            inputs.aliases,
            on_match=_project_isbn_on_match,
        )
        inputs.latest_candidates = projected_latest

    report_records: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_id = _candidate_id(candidate)
        if not candidate_id:
            continue
        identity_key = str(candidate.get("identity_key", "")).strip()
        outcome = outcomes_by_id.get(candidate_id)
        if candidate_id in skip_reasons:
            result = "skipped"
            reason_code = skip_reasons[candidate_id]
        elif candidate_id in applied_ids:
            result = "applied"
            reason_code = ""
        elif candidate_id in ambiguous_ids:
            result = "ambiguous"
            reason_code = outcome.reason_code if outcome else REASON_AMBIGUITY
        elif candidate_id in failed_ids:
            result = "failed"
            reason_code = outcome.reason_code if outcome else REASON_STAGE_EXECUTION_FAILURE
        else:
            result = "skipped"
            reason_code = outcome.reason_code if outcome else "not_eligible"
        record = _record_for(history, history_by_id, candidate_id, identity_key)
        final_state = (
            "still_fallback"
            if candidate_id in still_fallback_ids
            else (
                "description_present"
                if record is not None and record_has_usable_description(record)
                else "not_evaluated"
            )
        )
        report_record: dict[str, Any] = {
            "candidate_id": candidate_id,
            "identity_key": identity_key,
            "result": result,
            "final_state": final_state,
            "reason_code": reason_code,
        }
        if candidate_id in applied_ids and outcome is not None:
            ordered_evidence_urls = [
                canonicalize_url(url)[:MAX_URL_LENGTH]
                for url in accepted_evidence_urls(outcome)
                if canonicalize_url(url)
            ][:MAX_FETCHES_PER_CANDIDATE]
            report_record["evidence_urls"] = ordered_evidence_urls
            report_record["applied_date"] = str(outcome.applied_date or inputs.operation_date)[
                :MAX_METADATA_VALUE_LENGTH
            ]
        report_records.append(report_record)
    report_records.sort(key=lambda item: (item["candidate_id"], item["identity_key"]))
    return _AppliedOutcomes(
        history=history,
        current_candidates=inputs.current_candidates,
        report_records=report_records,
        applied_ids=applied_ids,
        failed_ids=failed_ids,
        ambiguous_ids=ambiguous_ids,
        still_fallback_ids=still_fallback_ids,
        skip_reasons=skip_reasons,
        current_projection=current_projection,
        latest_projection=latest_projection,
        repair_summary=_repair_summary(repair_plan, outcomes_by_id, applied_ids),
        lifecycle_summary=_lifecycle_summary(
            stage_candidates,
            outcomes_by_id,
            applied_ids,
            failure_reason=execution.failure_reason,
        ),
    )


def _lifecycle_summary(
    stage_candidates: Sequence[Mapping[str, Any]],
    outcomes_by_id: Mapping[str, StageOutcome],
    applied_ids: set[str],
    *,
    failure_reason: str = "",
) -> dict[str, Any]:
    """First-pass description yield per lifecycle band (issue #699).

    Reported beside the all-rows total rather than replacing it, so the
    figure the editor cares about — yield on the new and updated rows they
    will actually read — is visible without losing comparability with
    earlier runs.

    ``attempted`` follows the repair summary's semantics: a candidate the
    stage clock never reached, and one whose evidence was unchanged, never
    ran synthesis, so neither counts as an attempt. ``described`` is an
    applied qualified description. ``not_attempted_budget`` isolates the
    rows the clock never reached from the unchanged-evidence ones, so
    "every new candidate was attempted" is readable directly.

    A whole-stage failure reports unknown attempt coverage instead of
    counts. The recovery artifact synthesizes one failed outcome per
    eligible candidate, which is indistinguishable here from a real
    attempt that found nothing — so counting it would claim every new
    candidate was attempted, at yield 0.0, for a stage that may have
    crashed before starting any. The stage keeps no admission evidence
    across the exception, so "unknown" is the strongest honest answer.
    """
    coverage = COVERAGE_UNKNOWN if str(failure_reason).strip() else COVERAGE_MEASURED
    entries: list[dict[str, Any]] = []
    for candidate in stage_candidates:
        candidate_id = _candidate_id(candidate)
        if not candidate_id:
            continue
        outcome = outcomes_by_id.get(candidate_id)
        attempted = outcome is not None and outcome.outcome not in {
            OUTCOME_NOT_ATTEMPTED,
            OUTCOME_EVIDENCE_UNCHANGED,
        }
        entries.append(
            {
                "band": lifecycle_band(candidate),
                "attempted": attempted,
                "described": attempted and candidate_id in applied_ids,
                # A candidate with no outcome at all was never reported by
                # the stage, which is the budget case too: the clock ended
                # the run before it produced one.
                "not_attempted_budget": outcome is None or outcome.outcome == OUTCOME_NOT_ATTEMPTED,
            }
        )
    return lifecycle_yield(entries, attempt_coverage=coverage)


def _repair_summary(
    repair_plan: dict[str, Any] | None,
    outcomes_by_id: Mapping[str, StageOutcome],
    applied_ids: set[str],
) -> dict[str, Any]:
    """Summarize depth-repair progress with documented count semantics (#674).

    * ``backlog``: thin-but-usable legacy records at planning time, whatever
      their cooldown/exhaustion state and whichever route (fresh or repair)
      would attempt them. Progress is honest: this number only shrinks
      through improvement or exhaustion, never by redefinition.
    * ``attempted``: admitted repairs the stage actually ran synthesis for
      (excludes budget-deferred and unchanged-evidence outcomes).
    * ``improved``: admitted repairs that applied a qualified replacement.
    * ``deferred_budget``: admitted repairs the stage clock never reached.
    * ``deferred_cooldown`` / ``deferred_exhausted``: never admitted.
    * ``deferred_handoff``: admitted by policy but yielded to the residual
      route after a grounded evidence-shortfall failure (review finding on
      #675).
    * ``unresolved_insufficient_evidence``: attempted repairs whose evidence
      could not support a description (gate rejections and below-floor
      synthesis). Model-side failures stay in the backlog via cooldown.
    """
    if not repair_plan:
        return {
            "backlog": 0,
            "attempted": 0,
            "improved": 0,
            "deferred_budget": 0,
            "deferred_cooldown": 0,
            "deferred_exhausted": 0,
            "deferred_handoff": 0,
            "unresolved_insufficient_evidence": 0,
        }
    admitted_ids = set(repair_plan.get("admitted_ids", set()))
    attempted = 0
    improved = 0
    deferred_budget = 0
    unresolved = 0
    for candidate_id in admitted_ids:
        outcome = outcomes_by_id.get(candidate_id)
        if outcome is None or outcome.outcome == OUTCOME_NOT_ATTEMPTED:
            deferred_budget += 1
            continue
        if outcome.outcome == OUTCOME_EVIDENCE_UNCHANGED:
            continue
        attempted += 1
        if candidate_id in applied_ids:
            improved += 1
        elif (
            outcome.outcome == OUTCOME_ATTEMPTED_NO_DESCRIPTION
            and outcome.reason_code in REPAIR_EVIDENCE_SHORTFALL_REASONS
        ):
            unresolved += 1
    return {
        "backlog": int(repair_plan.get("backlog", 0) or 0),
        "attempted": attempted,
        "improved": improved,
        "deferred_budget": deferred_budget,
        "deferred_cooldown": len(repair_plan.get("deferred_cooldown", [])),
        "deferred_exhausted": len(repair_plan.get("deferred_exhausted", [])),
        "deferred_handoff": len(repair_plan.get("deferred_handoff", [])),
        "unresolved_insufficient_evidence": unresolved,
    }


def _record_if_unconfirmed(record: dict[str, Any], outcome: StageOutcome, inputs: _LoadedInputs) -> None:
    if _valid_codex_description(record):
        _record_attempt(record, outcome, inputs)


def _build_report(
    inputs: _LoadedInputs,
    execution: _StageExecution,
    applied: _AppliedOutcomes,
    stage: Any,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "version": 1,
        "operation_id": inputs.operation_id,
        "operation_date": inputs.operation_date,
        "status": execution.status,
        "call_latency": execution.artifact.call_latency,
        "row_evidence_fetched": execution.artifact.report.get("row_evidence_fetched", 0),
        "row_evidence_accepted": execution.artifact.report.get("row_evidence_accepted", 0),
        "counts": {
            "applied": len(applied.applied_ids),
            "skipped": sum(item["result"] == "skipped" for item in applied.report_records),
            "failed": len(applied.failed_ids),
            "ambiguous": len(applied.ambiguous_ids),
            "still_fallback": len(applied.still_fallback_ids),
        },
        "count_semantics": {
            "classification_counts": ["applied", "skipped", "failed", "ambiguous"],
            "overlapping_final_state_counts": ["still_fallback"],
        },
        "records": applied.report_records,
        "attention_required": bool(applied.failed_ids or applied.ambiguous_ids),
        "projection": {
            "current": dict(applied.current_projection) if applied.current_projection is not None else None,
            "latest": dict(applied.latest_projection) if applied.latest_projection is not None else None,
        },
        "repair": dict(applied.repair_summary) if applied.repair_summary is not None else None,
        # Issue #699: first-pass yield on the rows the editor reads, beside
        # the all-rows total. Answers "was every new candidate attempted
        # before the budget ran out" from the run artifact alone — but only
        # when ``attempt_coverage`` is "measured"; a failed stage reports
        # "unknown" rather than counting its synthetic failure outcomes.
        "lifecycle": dict(applied.lifecycle_summary) if applied.lifecycle_summary is not None else None,
        "budget": {
            "stage_budget_seconds": float(getattr(execution.artifact, "stage_budget_seconds", STAGE_BUDGET_SECONDS)),
            "candidate_envelope_seconds": float(
                getattr(stage, "candidate_envelope_seconds", CANDIDATE_ENVELOPE_SECONDS)
            ),
            "discovery_budget_seconds": float(getattr(stage, "discovery_budget_seconds", DISCOVERY_BUDGET_SECONDS)),
            "request_inactivity_timeout_seconds": float(getattr(stage, "request_inactivity_timeout_seconds", 10.0)),
            "max_workers": int(getattr(stage, "max_workers", MAX_WORKERS)),
            "max_fetches_per_candidate": int(getattr(stage, "max_fetches", MAX_FETCHES_PER_CANDIDATE)),
        },
    }
    if execution.failure_reason:
        report["failure_reason"] = execution.failure_reason
    return report


def _publication_bundle(inputs: _LoadedInputs, artifact: StageArtifact) -> tuple[list[Path], list[str]]:
    current_candidates = inputs.current_candidates
    destinations: list[Path] = []
    contents: list[str] = []
    seen: set[Path] = set()

    def add(path: Path, value: Any) -> None:
        resolved = path.resolve(strict=False)
        if resolved in seen:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        seen.add(resolved)
        destinations.append(path)
        contents.append(serialize_json(value))

    stage_payload = artifact.to_dict()
    add(inputs.output_dir / GROUNDING_STAGE_ARTIFACT_NAME, stage_payload)
    add(
        inputs.queue_path,
        {
            "version": 1,
            "entries": [entry.to_dict() for entry in sorted(artifact.queue, key=lambda item: item.candidate_id)],
        },
    )
    if keeps_run_history_copy(inputs.output_dir, inputs.state_dir, inputs.store):
        add(inputs.output_dir / RUN_HISTORY_COPY, inputs.history)
    add(inputs.output_dir / "scouting-candidates.json", current_candidates)
    return destinations, contents


def _publish_outputs(
    inputs: _LoadedInputs,
    artifact: StageArtifact,
    *,
    replace: Callable[[Path, Path], None] | None,
) -> None:
    """Publish the run's files and canonical state together.

    Candidate History and the latest snapshot are written in one State Store
    transaction, and the run's files are published as one guarded bundle
    inside it, so state and files land together: the latest snapshot can
    never visibly disagree with the history it is projected from after a
    published run (issue #671), a failed bundle writes no state, and a
    failed state commit puts the files back. A run with no latest snapshot
    publishes none.
    """
    destinations, contents = _publication_bundle(inputs, artifact)
    replacement = replace or _staged_replace
    if inputs.store is not None:
        store = inputs.store
    elif inputs.state_dir is not None:
        store = open_state_store(inputs.state_dir)
    else:
        store = open_run_output_store(inputs.output_dir)

    def write_state() -> None:
        # Without state the store is the output directory's; it takes the
        # history only when that directory keeps it one file per book (#1073).
        if inputs.state_dir is not None or history_in_output_shards(inputs.output_dir, None):
            store.replace(Collection.CANDIDATE_HISTORY, inputs.history)
        if inputs.latest_candidates is not None:
            store.replace(Collection.LATEST_SCOUTING_CANDIDATES, inputs.latest_candidates)

    with bundle_write_lock(*destinations):
        publish_files_with_state(
            store, write_state, lambda: publish_bundle(destinations, contents, replace=replacement), destinations
        )


def _service_counts(
    *,
    status: str,
    reason: str = "",
    counts: Mapping[str, Any] | None = None,
    reason_counts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_counts = {outcome: 0 for outcome in OUTCOME_ORDER}
    if isinstance(counts, Mapping):
        for outcome in normalized_counts:
            try:
                normalized_counts[outcome] = int(counts.get(outcome, 0) or 0)
            except (TypeError, ValueError):
                normalized_counts[outcome] = 0
    normalized_reasons: dict[str, int] = {}
    if isinstance(reason_counts, Mapping):
        for raw_key, raw_count in sorted(reason_counts.items(), key=lambda item: str(item[0]).strip()):
            raw_reason = str(raw_key).strip()
            if not raw_reason:
                continue
            try:
                normalized_reasons[raw_reason] = int(raw_count or 0)
            except (TypeError, ValueError):
                normalized_reasons[raw_reason] = 0
    result: dict[str, Any] = {
        "status": status,
        "grounded_description_counts": normalized_counts,
        "grounded_description_reason_counts": normalized_reasons,
        **normalized_counts,
    }
    if reason:
        result["reason"] = reason
    return result


def enrich_grounded_descriptions(
    stage: Any,
    output_dir: Path,
    state_dir: Path | None = None,
    effective_date: str | None = None,
    *,
    operation_id: str | None = None,
    enabled: bool = True,
    disabled_reason: str = "",
    replace: Callable[[Path, Path], None] | None = None,
    snapshot_tracker: CandidateProgressSnapshot | None = None,
    state_store: StateStore | None = None,
) -> dict[str, Any]:
    """Run, apply, and atomically publish one grounded-description operation.

    Canonical state is read and written through *state_store*, the run's
    store, or a store opened over *state_dir*.
    """
    if not enabled:
        return _service_counts(status="skipped", reason=disabled_reason or "grounded description stage disabled")
    loaded = _load_inputs(output_dir, state_dir, effective_date, operation_id, state_store)
    if isinstance(loaded, dict):
        return _service_counts(status=loaded["status"], reason=loaded.get("reason", ""))
    if loaded is None:
        return _service_counts(status="ok")
    inputs = loaded
    candidates, eligible, skip_reasons, repair_plan = _plan_eligibility(inputs)
    inputs.repair_candidate_ids = set(repair_plan["admitted_ids"])
    # Planned admission order, enforced at actual stage admission (review
    # finding on #674): fresh work first in eligible order, then repairs
    # fewest-attempts-first. Without the rank map the stage queue's
    # oldest-first sort silently inverts repair fairness.
    repair_rank = {
        candidate_id: ordinal
        for ordinal, item in enumerate(repair_plan["admitted"])
        if (candidate_id := _candidate_id(item))
    }
    queue = GroundedDescriptionQueue.load(inputs.queue_path)
    execution = _execute_stage(
        stage, eligible, inputs, queue, repair_rank=repair_rank or None, snapshot_tracker=snapshot_tracker
    )
    applied = _apply_outcomes(inputs, candidates, eligible, execution, skip_reasons, repair_plan)
    report = _build_report(inputs, execution, applied, stage)
    artifact = dataclass_replace(execution.artifact, report=report)
    try:
        _publish_outputs(inputs, artifact, replace=replace)
    except SafeArtifactRecoveryError:
        return {
            **_service_counts(
                status="recovery_failed",
                counts=artifact.counts,
                reason_counts=artifact.reason_counts,
                reason="description_persistence_recovery_failure",
            ),
            "stage_artifact": GROUNDING_STAGE_ARTIFACT_NAME,
            "description_stage_report": report,
        }
    except Exception:
        return {
            **_service_counts(
                status="persistence_failed",
                counts=artifact.counts,
                reason_counts=artifact.reason_counts,
                reason="description_persistence_failure",
            ),
            "stage_artifact": GROUNDING_STAGE_ARTIFACT_NAME,
            "description_stage_report": report,
        }
    return {
        **_service_counts(status=execution.status, counts=artifact.counts, reason_counts=artifact.reason_counts),
        "stage_artifact": GROUNDING_STAGE_ARTIFACT_NAME,
        "description_stage_report": report,
    }
