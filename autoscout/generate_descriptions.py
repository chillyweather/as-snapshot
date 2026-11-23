"""Resumable standalone generation of grounded Content Descriptions."""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from concurrent.futures import FIRST_COMPLETED
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

from autoscout.content_description import description_provenance_record
from autoscout.content_description import publication_recheck_pending
from autoscout.description_depth import meets_description_depth
from autoscout.description_repair import REPAIR_EVIDENCE_SHORTFALL_REASONS
from autoscout.description_repair import is_repair_candidate
from autoscout.description_repair import record_repair_attempt
from autoscout.description_repair import repair_due
from autoscout.description_repair import repair_sort_key
from autoscout.description_repair import select_due_repairs
from autoscout.grounded_description import CANDIDATE_ENVELOPE_SECONDS
from autoscout.grounded_description import GROUNDING_STAGE_ARTIFACT_NAME
from autoscout.grounded_description import MAX_FETCHES_PER_CANDIDATE
from autoscout.grounded_description import MAX_WORKERS
from autoscout.grounded_description import OUTCOME_ATTEMPTED_NO_DESCRIPTION
from autoscout.grounded_description import OUTCOME_EVIDENCE_UNCHANGED
from autoscout.grounded_description import OUTCOME_GROUNDED
from autoscout.grounded_description import OUTCOME_NOT_ATTEMPTED
from autoscout.grounded_description import OUTCOME_ORDER
from autoscout.grounded_description import OUTCOME_SINGLE_SOURCE
from autoscout.grounded_description import PREFLIGHT_ADAPTER_KEY_MISSING
from autoscout.grounded_description import PREFLIGHT_ADAPTER_NO_RESPONSE
from autoscout.grounded_description import PREFLIGHT_ADAPTER_NOT_RUNNING
from autoscout.grounded_description import PREFLIGHT_ADAPTER_UNAVAILABLE
from autoscout.grounded_description import PREFLIGHT_ADAPTER_VENDOR_ERROR
from autoscout.grounded_description import PREFLIGHT_ENDPOINT_NOT_CONFIGURED
from autoscout.grounded_description import PREFLIGHT_HEALTHY
from autoscout.grounded_description import REASON_INSUFFICIENT_DEPTH
from autoscout.grounded_description import REASON_NOT_ATTEMPTED_STAGE_BUDGET
from autoscout.grounded_description import GroundedDescriptionQueue
from autoscout.grounded_description import GroundedDescriptionStageService
from autoscout.grounded_description import PreflightResult
from autoscout.grounded_description import QueueEntry
from autoscout.grounded_description import StageArtifact
from autoscout.grounded_description import StageOutcome
from autoscout.grounded_description import accepted_evidence_urls
from autoscout.grounded_description import write_stage_artifact
from autoscout.json_io import write_json
from autoscout.repository_root import repository_root
from autoscout.review_rendering import render_review
from autoscout.state_store import Collection
from autoscout.state_store import StateStore
from autoscout.state_store import open_run_output_store
from autoscout.state_store import open_state_store

STANDALONE_TOTAL_BUDGET_SECONDS = 10_800.0
STANDALONE_MAX_WORKERS = MAX_WORKERS
ATTEMPT_FIELD = "grounded_description_attempts"
GENERATE_DESCRIPTIONS_ARTIFACT_NAME = "generate-descriptions.json"
FAILED_RETRY_AGE_DAYS = 7
# A grounded candidate is re-verified on this cadence, not on every run.
# Re-verifying all of them every run cost roughly 650 to 850 extra Brave
# queries and up to 3240 seconds of the standalone budget against a
# 659-candidate history, which starved new candidates and made the envelope
# contention of issue #542 worse.  Thirty days on a weekly cadence means each
# grounded candidate is re-checked about monthly, which is often enough to
# catch a source whose page changed and rare enough to leave the budget for
# new work.  Same shape as FAILED_RETRY_AGE_DAYS above.
GROUNDED_REVERIFY_AGE_DAYS = 30
# At most this many stale grounded candidates are re-verified in one run.
# The history's grounded candidates were all grounded within days of each
# other, so a staleness rule alone makes all 216 come due on the same day and
# re-creates the starvation it was meant to prevent, once a month instead of
# weekly.  The cap spreads them over consecutive runs, oldest first, and the
# number deferred is reported rather than dropped silently.
GROUNDED_REVERIFY_MAX_PER_RUN = 20
# At most this many depth-repair candidates are attempted in one standalone
# run. Repair never starves fresh work: the re-verification cap above still
# bounds re-verification, and repairs take the fewest-attempts-first order
# from the shared repair policy, so recurring failures sink behind untouched
# records across runs (issue #674).
DEPTH_REPAIR_MAX_PER_RUN = 20


class DescriptionGenerationPreflightError(RuntimeError):
    """Raised before Candidate History is eligible for mutation."""

    def __init__(self, reason_code: str, message: str | None = None) -> None:
        self.reason_code = reason_code
        self.message = message or _preflight_message(reason_code)
        super().__init__(self.message)


def _preflight_message(reason_code: str) -> str:
    return {
        PREFLIGHT_ENDPOINT_NOT_CONFIGURED: "discovery endpoint is not configured",
        PREFLIGHT_ADAPTER_UNAVAILABLE: "discovery adapter is unavailable",
        PREFLIGHT_ADAPTER_NOT_RUNNING: "discovery adapter is not running",
        PREFLIGHT_ADAPTER_NO_RESPONSE: "discovery adapter did not answer preflight",
        PREFLIGHT_ADAPTER_KEY_MISSING: "AUTOSCOUT_SEARCH_API_KEY is not set",
        PREFLIGHT_ADAPTER_VENDOR_ERROR: "discovery adapter vendor error",
        PREFLIGHT_HEALTHY: "discovery adapter is healthy",
    }.get(reason_code, reason_code)


def _run_preflight(
    service: GroundedDescriptionStageService,
    preflight: Callable[[], Any] | Any | None,
) -> None:
    if preflight is not None:
        method = preflight if callable(preflight) else getattr(preflight, "preflight", None)
        if not callable(method):
            raise DescriptionGenerationPreflightError(PREFLIGHT_ADAPTER_UNAVAILABLE)
        try:
            answer = method()
        except Exception as exc:
            raise DescriptionGenerationPreflightError(PREFLIGHT_ADAPTER_NO_RESPONSE) from exc
        if isinstance(answer, PreflightResult):
            if not answer.ok:
                raise DescriptionGenerationPreflightError(answer.reason_code, answer.message)
            return
        if isinstance(answer, tuple) and len(answer) == 2:
            ok, reason = answer
            if not ok:
                reason_code = str(reason or PREFLIGHT_ADAPTER_NO_RESPONSE)
                raise DescriptionGenerationPreflightError(reason_code)
            return
        if answer is False or answer is None:
            raise DescriptionGenerationPreflightError(PREFLIGHT_ADAPTER_NO_RESPONSE)
        if isinstance(answer, Mapping) and answer.get("status") in {"failed", "error", "unavailable"}:
            raise DescriptionGenerationPreflightError(PREFLIGHT_ADAPTER_NO_RESPONSE)
        return

    try:
        result = service.preflight()
    except Exception as exc:
        raise DescriptionGenerationPreflightError(PREFLIGHT_ADAPTER_NO_RESPONSE) from exc
    if isinstance(result, PreflightResult):
        if not result.ok:
            raise DescriptionGenerationPreflightError(result.reason_code, result.message)
        return
    if isinstance(result, tuple) and len(result) == 2:
        ok, reason = result
    else:
        ok, reason = bool(result), PREFLIGHT_ADAPTER_NO_RESPONSE
    if not ok:
        raise DescriptionGenerationPreflightError(str(reason or PREFLIGHT_ADAPTER_NO_RESPONSE))


def _attempts(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = record.get(ATTEMPT_FIELD, [])
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _is_failed_attempt(attempt: Mapping[str, Any]) -> bool:
    outcome = str(attempt.get("outcome", "")).strip()
    reason = str(attempt.get("reason_code", "")).strip()
    return outcome not in {OUTCOME_GROUNDED, OUTCOME_SINGLE_SOURCE} and reason not in {
        OUTCOME_GROUNDED,
        OUTCOME_SINGLE_SOURCE,
    }


def _attempt_date(attempt: Mapping[str, Any]) -> date | None:
    raw = str(attempt.get("date", "")).strip()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _grounded_needs_reverification(
    record: Mapping[str, Any],
    provenance: Mapping[str, Any],
    *,
    effective_date: date,
) -> bool:
    """Whether an already-grounded candidate should be queued again.

    The evidence-unchanged gate lives at the synthesis site, because only
    discovery can tell changed evidence from unchanged evidence.  But queueing
    every grounded candidate on every run spends discovery on all of them, so
    eligibility stays selective: a candidate is re-queued only when the gate
    could not decide, or when its last verification is old enough that the
    sources may have moved.

    Re-queued when:

    - ``evidence_basis`` is missing or not a list, so the gate has nothing to
      compare and must not skip.
    - ``grounded_outcome`` is missing, for the same reason.
    - the last attempt is undated or at least ``GROUNDED_REVERIFY_AGE_DAYS``
      old.

    Otherwise the stored description is grounded in evidence that was verified
    recently and is left alone.
    """
    stored_basis = provenance.get("evidence_basis")
    if not isinstance(stored_basis, list) or not stored_basis:
        return True
    if not str(record.get("grounded_outcome", "")).strip():
        return True
    attempts = _attempts(record)
    if not attempts:
        return True
    attempted_on = _attempt_date(attempts[-1])
    if attempted_on is None:
        return True
    return (effective_date - attempted_on).days >= GROUNDED_REVERIFY_AGE_DAYS


def _is_grounded_record(record: Mapping[str, Any]) -> bool:
    provenance = record.get("description_provenance")
    return isinstance(provenance, Mapping) and provenance.get("provenance") == "grounded"


def _cap_grounded_reverification(
    eligible_records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Bound how many already-grounded candidates are re-verified in one run.

    Candidates that have never been grounded always pass: new work is never
    deferred behind re-verification.  Stale grounded candidates are taken
    oldest-first up to ``GROUNDED_REVERIFY_MAX_PER_RUN`` and the rest wait for
    the next run.  The caller reports the deferred count, because a silent cap
    reads as "everything was covered" when it was not.

    Thin grounded records are repair candidates, not re-verifications: the
    repair cap admits them on the repair cooldown, so they pass through
    untouched here.
    """
    reverification = [
        record for record in eligible_records if _is_grounded_record(record) and not is_repair_candidate(record)
    ]
    if len(reverification) <= GROUNDED_REVERIFY_MAX_PER_RUN:
        return eligible_records, 0

    def oldest_first(record: Mapping[str, Any]) -> tuple[int, str, str]:
        attempts = _attempts(record)
        raw_date = str(attempts[-1].get("date", "")).strip() if attempts else ""
        # An undated or unattempted record sorts first: it is the least known.
        return (1 if raw_date else 0, raw_date, _candidate_id(record) or "")

    ordered = sorted(reverification, key=oldest_first)
    admitted = set(map(id, ordered[:GROUNDED_REVERIFY_MAX_PER_RUN]))
    deferred = len(ordered) - GROUNDED_REVERIFY_MAX_PER_RUN
    # Preserve the original history order for the records that run. Repair
    # candidates pass through: the repair cap schedules them, not this one.
    kept = [
        record
        for record in eligible_records
        if (not _is_grounded_record(record) or is_repair_candidate(record)) or id(record) in admitted
    ]
    return kept, deferred


def _cap_depth_repair(
    eligible_records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Bound how many depth-repair candidates are attempted in one run.

    Repairs take the shared fewest-attempts-first order, so the deferred
    remainder is exactly the records with the most repair attempts behind
    them. The caller reports the deferred count.
    """
    repairs = [record for record in eligible_records if is_repair_candidate(record)]
    if len(repairs) <= DEPTH_REPAIR_MAX_PER_RUN:
        return eligible_records, 0
    ordered = sorted(repairs, key=lambda record: (repair_sort_key(record), _candidate_id(record) or ""))
    admitted = set(map(id, ordered[:DEPTH_REPAIR_MAX_PER_RUN]))
    deferred = len(ordered) - DEPTH_REPAIR_MAX_PER_RUN
    kept = [record for record in eligible_records if not is_repair_candidate(record) or id(record) in admitted]
    return kept, deferred


def _order_fresh_first_with_repairs(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order fresh work before repairs, repairs fewest-attempts-first (#674).

    Weekly admission runs fresh work first and appends repairs in
    fairness order; standalone must plan the same order before --limit
    and budget admission, otherwise history order lets a
    previously-failed repair win a limit slot over untouched work.
    Fresh records keep their incoming order; repairs sort by the shared
    repair key with a candidate-id tie-break for determinism.
    """
    fresh = [record for record in records if not is_repair_candidate(record)]
    repairs = sorted(
        (record for record in records if is_repair_candidate(record)),
        key=lambda record: (repair_sort_key(record), _candidate_id(record) or ""),
    )
    return [*fresh, *repairs]


def _eligible(
    record: Mapping[str, Any],
    *,
    effective_date: date,
    retry_failed: bool,
) -> bool:
    provenance = record.get("description_provenance")
    provenance_name = str(provenance.get("provenance", "")).strip() if isinstance(provenance, Mapping) else ""
    # Editor-corrected descriptions are authoritative and never attempted,
    # by any route (issues #637, #674).
    if provenance_name == "editor-corrected":
        return False
    # A book found not yet published has no page to ground until its
    # re-check date; only an explicit ``--only`` searches it sooner
    # (issue #791).
    if publication_recheck_pending(record, effective_date):
        return False
    # Depth repair is evaluated before the grounded re-verification
    # cadence: a thin usable description is due on its own cooldown, not
    # on the 30-day re-verification clock (issue #674).
    if is_repair_candidate(record):
        return repair_due(record, effective_date)
    if _is_grounded_record(record):
        provenance = record["description_provenance"]
        return _grounded_needs_reverification(record, provenance, effective_date=effective_date)
    attempts = _attempts(record)
    if not attempts or retry_failed:
        return True
    latest = attempts[-1]
    if str(latest.get("reason_code", "")).strip() == "candidate_budget_exhausted":
        return True
    if not _is_failed_attempt(latest):
        return False
    attempted_on = _attempt_date(latest)
    if attempted_on is None:
        return True
    return (effective_date - attempted_on).days >= FAILED_RETRY_AGE_DAYS


def _candidate_identity(record: Mapping[str, Any]) -> str:
    value = record.get("identity_key", "")
    return "" if value is None else str(value).strip()


def _candidate_id(record: Mapping[str, Any]) -> str:
    value = record.get("candidate_id", "")
    return "" if value is None else str(value).strip()


def _normalized_selection_key(value: object) -> str:
    return str(value).strip().casefold()


def _selection_record_label(record: Mapping[str, Any], index: int) -> str:
    candidate_id = _candidate_id(record)
    return candidate_id or f"<missing candidate_id at history index {index}>"


def _resolve_named_selection(
    history: Sequence[dict[str, Any]],
    *,
    only: Sequence[str] | None,
    only_file: Path | None,
) -> list[dict[str, Any]]:
    supplied: list[str] = []
    if only is not None:
        supplied.extend(str(value) for value in only)
    if only_file is not None:
        for line in only_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                supplied.append(stripped)

    requested: list[tuple[str, str]] = []
    seen: set[str] = set()
    for value in supplied:
        normalized = _normalized_selection_key(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        requested.append((normalized, value))
    if not requested:
        raise ValueError("selection is empty")

    matches: dict[str, list[tuple[int, dict[str, Any]]]] = {key: [] for key, _ in requested}
    for index, record in enumerate(history):
        normalized = _normalized_selection_key(_candidate_identity(record))
        if normalized in matches:
            matches[normalized].append((index, record))

    unmatched = [display for key, display in requested if not matches[key]]
    if unmatched:
        formatted = ", ".join(repr(value) for value in unmatched)
        raise ValueError(f"selection contains unmatched identity key(s): {formatted}")

    ambiguous = []
    for key, display in requested:
        records = matches[key]
        if len(records) > 1:
            labels = ", ".join(_selection_record_label(record, index) for index, record in records)
            ambiguous.append(f"{display!r}: candidate IDs: {labels}")
    if ambiguous:
        raise ValueError("selection is ambiguous for identity key(s): " + "; ".join(ambiguous))

    return [matches[key][0][1] for key, _ in requested]


def _call_service(
    service: GroundedDescriptionStageService,
    candidate: Mapping[str, Any],
    *,
    operation_id: str,
    operation_date: str,
    history: Sequence[Mapping[str, Any]],
    aliases: Mapping[str, Any],
) -> StageArtifact:
    """Call the public service seam while retaining small test fakes."""
    method = service.run
    kwargs: dict[str, Any] = {
        "operation_id": operation_id,
        "operation_date": operation_date,
        "history": history,
        "aliases": aliases,
        "queue": GroundedDescriptionQueue(),
        "force_candidates": True,
    }
    try:
        parameters = inspect.signature(method).parameters
        if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
            return method([candidate], **kwargs)
        return method([candidate], **{key: value for key, value in kwargs.items() if key in parameters})
    except (TypeError, ValueError):
        return method([candidate], **kwargs)


def _result_for(artifact: StageArtifact, candidate: Mapping[str, Any]) -> StageOutcome:
    candidate_id = _candidate_id(candidate)
    for outcome in artifact.outcomes:
        if outcome.candidate_id == candidate_id:
            return outcome
    return StageOutcome(
        candidate_id=candidate_id,
        identity_key=_candidate_identity(candidate),
        outcome=OUTCOME_NOT_ATTEMPTED,
        reason_code="not_attempted_stage_budget",
    )


def _record_attempt(
    record: dict[str, Any], outcome: StageOutcome, effective_date: str, *, operation_id: str = ""
) -> None:
    # Preserve the stage reason exactly.  Successful stage outcomes normally
    # use an empty reason code, which is meaningful and must not be replaced.
    attempt: dict[str, Any] = {
        "date": effective_date,
        "outcome": outcome.outcome,
        "reason_code": outcome.reason_code,
    }
    if outcome.reason_detail:
        attempt["reason_detail"] = outcome.reason_detail
    if outcome.rejected_source_reasons:
        rejected_reason_counts: dict[str, int] = {}
        for rejected_source in outcome.rejected_source_reasons:
            rejected_reason_counts[rejected_source.reason_code] = (
                rejected_reason_counts.get(rejected_source.reason_code, 0) + 1
            )
        attempt["rejected_reason_counts"] = rejected_reason_counts
    existing = record.get(ATTEMPT_FIELD)
    attempts = list(existing) if isinstance(existing, list) else []
    attempts.append(attempt)
    record[ATTEMPT_FIELD] = attempts
    # Repair attempts are logged separately from content (issue #674).
    # This function only runs for attempted outcomes — budget and
    # unchanged-evidence outcomes return before it — so logging here never
    # starts a cooldown the record did not earn. It also runs before the
    # caller applies a successful replacement, while the record still
    # reads as a repair candidate; a post-apply check would miss it.
    if is_repair_candidate(record):
        record_repair_attempt(
            record,
            operation_date=effective_date,
            operation_id=operation_id,
            outcome=outcome.outcome,
            reason_code=outcome.reason_code,
            stage="grounded",
        )


def _apply_outcome(
    record: dict[str, Any], outcome: StageOutcome, effective_date: str, *, operation_id: str = ""
) -> None:
    if outcome.outcome in {OUTCOME_NOT_ATTEMPTED, OUTCOME_EVIDENCE_UNCHANGED}:
        return
    # Editor-corrected descriptions are authoritative: neither selection
    # (see _eligible) nor an explicit --only override may rewrite them, so
    # even a successful outcome for one is dropped without a trace.
    provenance = record.get("description_provenance")
    if isinstance(provenance, Mapping) and str(provenance.get("provenance", "")).strip() == "editor-corrected":
        return
    if outcome.outcome in {OUTCOME_GROUNDED, OUTCOME_SINGLE_SOURCE} and not meets_description_depth(
        outcome.content_description
    ):
        # Depth backstop at the application boundary (review finding on
        # #673): a shallow success that reached application outside the
        # synthesis gate is logged as a failed attempt and preserves the
        # retained text, never persisted as grounded.
        _record_attempt(
            record,
            replace(
                outcome,
                outcome=OUTCOME_ATTEMPTED_NO_DESCRIPTION,
                reason_code=REASON_INSUFFICIENT_DEPTH,
                content_description="",
                research_sources=(),
                prominence_claims=(),
            ),
            effective_date,
            operation_id=operation_id,
        )
        return
    _record_attempt(record, outcome, effective_date, operation_id=operation_id)
    if outcome.outcome not in {OUTCOME_GROUNDED, OUTCOME_SINGLE_SOURCE}:
        return
    urls = accepted_evidence_urls(outcome)
    record["content_description"] = outcome.content_description
    # short_description is separate editor annotation metadata.  This stage
    # never replaces or regenerates it.
    record["grounded_outcome"] = outcome.outcome
    record["description_provenance"] = description_provenance_record(
        str(record.get("identity_key", outcome.identity_key)),
        outcome.content_description,
        "grounded",
        evidence_basis=urls,
        applied_date=effective_date,
        grounding_evidence_url=urls[0] if urls else None,
        english_description=outcome.english_description or None,
    )


def _scheduled_outcome(outcome: StageOutcome, operation_id: str, operation_date: str) -> StageOutcome:
    if outcome.outcome != OUTCOME_NOT_ATTEMPTED or outcome.scheduling is not None:
        return outcome
    return replace(
        outcome,
        reason_code=outcome.reason_code or REASON_NOT_ATTEMPTED_STAGE_BUDGET,
        scheduling=QueueEntry(
            candidate_id=outcome.candidate_id,
            queue_entry_operation=operation_id,
            queue_entry_date=operation_date,
            first_seen_date=operation_date,
            priority_label="D",
            attempt_status="waiting",
            reason_code=outcome.reason_code or REASON_NOT_ATTEMPTED_STAGE_BUDGET,
        ),
    )


def _stage_artifact(
    operation_id: str,
    effective_date: str,
    outcomes: Mapping[str, StageOutcome],
    order: Sequence[str] = (),
    *,
    started: float,
    monotonic: Callable[[], float],
) -> StageArtifact:
    normalized = {key: _scheduled_outcome(outcome, operation_id, effective_date) for key, outcome in outcomes.items()}
    ordered_keys = [key for key in order if key in normalized]
    ordered_keys.extend(key for key in sorted(normalized) if key not in ordered_keys)
    ordered = tuple(normalized[key] for key in ordered_keys)
    queue_entries = tuple(outcome.scheduling for outcome in ordered if outcome.scheduling is not None)
    return StageArtifact(
        operation_id=operation_id,
        operation_date=effective_date,
        outcomes=ordered,
        queue=tuple(entry for entry in queue_entries if entry is not None),
        elapsed_seconds=max(0.0, monotonic() - started),
        stage_budget_seconds=STANDALONE_TOTAL_BUDGET_SECONDS,
    )


def _write_generation_artifact(
    output_dir: Path,
    effective_date: str,
    outcomes: Mapping[str, StageOutcome],
    order: Sequence[str] = (),
) -> None:
    rows = []
    ordered_keys = [key for key in order if key in outcomes]
    ordered_keys.extend(key for key in sorted(outcomes) if key not in ordered_keys)
    for candidate_id in ordered_keys:
        outcome = outcomes[candidate_id]
        rows.append(
            {
                "candidate_id": outcome.candidate_id,
                "identity_key": outcome.identity_key,
                "outcome": outcome.outcome,
                "reason_code": outcome.reason_code,
                **({"reason_detail": outcome.reason_detail} if outcome.reason_detail else {}),
                "accepted_source_urls": accepted_evidence_urls(outcome),
            }
        )
    write_json(
        output_dir / GENERATE_DESCRIPTIONS_ARTIFACT_NAME,
        {
            "artifact": "generate-descriptions",
            "version": 1,
            "operation_date": effective_date,
            "touched_candidates": rows,
        },
    )


def _ensure_render_candidates(
    output_dir: Path,
    history: list[dict[str, Any]],
    *,
    aliases: Mapping[str, Any] | None = None,
    store: StateStore | None = None,
) -> dict[str, Any]:
    """Project authoritative history descriptions onto the render snapshots (#671).

    The current candidates in *output_dir* keep their membership and
    unrelated fields; only the shared description facts travel. A missing
    current file is initialized from history (the standalone run owns no
    other snapshot yet). The latest snapshot is refreshed alongside when
    there is one (in *store*, else in a store over the output directory) —
    never created. Returns the per-file projection summaries (``None`` where
    no snapshot existed).
    """
    from autoscout.description_projection import project_latest_snapshot
    from autoscout.description_projection import project_snapshot_file

    summary: dict[str, Any] = {"current": None, "latest": None}
    path = output_dir / "scouting-candidates.json"
    current_summary = project_snapshot_file(path, history, aliases)
    if current_summary is None:
        write_json(path, history)
    else:
        summary["current"] = current_summary
    if store is not None:
        summary["latest"] = project_latest_snapshot(store, history, aliases)
    if summary["latest"] is None:
        summary["latest"] = project_latest_snapshot(open_run_output_store(output_dir), history, aliases)
    return summary


def generate_descriptions(
    state_dir: Path,
    output_dir: Path,
    *,
    limit: int | None = None,
    retry_failed: bool = False,
    only: Sequence[str] | None = None,
    only_file: Path | None = None,
    effective_date: str | None = None,
    review_config: Path | None = None,
    service: GroundedDescriptionStageService | None = None,
    preflight: Callable[[], Any] | Any | None = None,
    discovery: Any | None = None,
    fetch: Any | None = None,
    synthesizer: Any | None = None,
    max_workers: int = MAX_WORKERS,
    monotonic: Callable[[], float] = time.monotonic,
    state_store: StateStore | None = None,
) -> dict[str, Any]:
    """Generate descriptions from Candidate History and render the review.

    Canonical state is read and written through *state_store*, or a store
    opened over *state_dir*.

    Preflight is intentionally the first external operation.  Candidate
    History is written only after a real stage result completes, one result at
    a time, so an interrupt leaves a resumable prefix of completed work.
    """
    if service is None:
        if discovery is not None or fetch is not None or synthesizer is not None:
            from autoscout.grounded_description import GroundedDescriptionStage

            service = GroundedDescriptionStageService(
                GroundedDescriptionStage(
                    discovery=discovery,
                    fetch=fetch,
                    synthesizer=synthesizer,
                    candidate_envelope_seconds=CANDIDATE_ENVELOPE_SECONDS,
                    monotonic=monotonic,
                    max_workers=MAX_WORKERS,
                    max_fetches=MAX_FETCHES_PER_CANDIDATE,
                    stage_budget_seconds=STANDALONE_TOTAL_BUDGET_SECONDS,
                )
            )
        else:
            from autoscout.weekly_workflow import _make_production_enrichment

            built = _make_production_enrichment(
                repo_path=repository_root(),
                stage_budget_seconds=STANDALONE_TOTAL_BUDGET_SECONDS,
            )
            if not isinstance(built, GroundedDescriptionStageService):
                raise TypeError("production enrichment is not a grounded description service")
            service = built

    _run_preflight(service, preflight)

    named_selection = only is not None or only_file is not None
    if named_selection and limit is not None:
        raise ValueError("--limit cannot be combined with --only or --only-file")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be a positive integer")
    if max_workers <= 0:
        raise ValueError("max_workers must be positive")
    worker_count = min(max_workers, MAX_WORKERS)

    store = state_store if state_store is not None else open_state_store(state_dir)
    raw_history = store.read(Collection.CANDIDATE_HISTORY)
    if not isinstance(raw_history, list) or not all(isinstance(item, Mapping) for item in raw_history):
        raise ValueError("Candidate History must be a list of objects")
    history = [dict(item) for item in raw_history]
    # Visibility is editorial intent: hidden candidates are excluded from
    # automatic and named selection alike, including repair (review
    # finding on #674). No visibility records means nothing is hidden.
    visibility_value = store.read(Collection.CANDIDATE_VISIBILITY)
    visibility_records = visibility_value if isinstance(visibility_value, list) else []
    hidden_identities = {
        str(item.get("identity_key", "")).strip()
        for item in visibility_records
        if isinstance(item, Mapping)
        and str(item.get("identity_key", "")).strip()
        and str(item.get("visibility", "")).strip().casefold() == "hidden"
    }
    hidden_candidate_ids = {
        str(item.get("candidate_id", "")).strip()
        for item in visibility_records
        if isinstance(item, Mapping)
        and str(item.get("candidate_id", "")).strip()
        and str(item.get("visibility", "")).strip().casefold() == "hidden"
    }

    def _is_hidden(record: Mapping[str, Any]) -> bool:
        return (
            str(record.get("identity_key", "")).strip() in hidden_identities
            or str(record.get("candidate_id", "")).strip() in hidden_candidate_ids
        )

    operation_date = effective_date or date.today().isoformat()
    run_date = date.fromisoformat(operation_date)
    # Partition visible history once so the backlog and deferral counts
    # explain every repair candidate, including ones this run can never
    # admit (review finding on #674): cooling/exhausted records never
    # reach eligibility, so counting only eligible repairs would report
    # backlog 1 with all deferrals 0.
    visible_history = [record for record in history if not _is_hidden(record)]
    repair_partition = select_due_repairs(visible_history, run_date)
    repair_backlog = (
        len(repair_partition["due"]) + len(repair_partition["cooling_down"]) + len(repair_partition["exhausted"])
    )
    repair_cooling_ids = {
        str(item.get("candidate_id", "")).strip() for item in repair_partition["cooling_down"] if _candidate_id(item)
    }
    repair_exhausted_ids = {
        str(item.get("candidate_id", "")).strip() for item in repair_partition["exhausted"] if _candidate_id(item)
    }
    repair_deferred_count = 0
    repair_deferred_limit = 0
    if named_selection:
        selected = _resolve_named_selection(history, only=only, only_file=only_file)
        selected = [record for record in selected if not _is_hidden(record)]
        # Named selection preserves the explicit request order: the
        # operator named these candidates, so fairness reordering must
        # not override that intent. Automatic planning below orders
        # fresh-first instead.
        # Explicit selection via --only forces re-synthesis even on unchanged
        # evidence.  The flag is carried on a per-attempt copy of the record,
        # never on the record itself: writing it onto the history document
        # persisted it for every candidate not yet consumed, and the gate then
        # read it back on later runs and forced re-synthesis forever.
        forced_candidate_ids = {candidate_id for record in selected if (candidate_id := _candidate_id(record))}
    else:
        forced_candidate_ids = set()
        eligible_records = [
            record
            for record in history
            if _candidate_id(record)
            and not _is_hidden(record)
            and _eligible(record, effective_date=run_date, retry_failed=retry_failed)
        ]
        selected, reverify_deferred_count = _cap_grounded_reverification(eligible_records)
        if reverify_deferred_count:
            print(
                f"generate-descriptions: deferred {reverify_deferred_count} grounded re-verification(s) "
                f"to a later run (cap {GROUNDED_REVERIFY_MAX_PER_RUN} per run)"
            )
        selected, repair_deferred_count = _cap_depth_repair(selected)
        if repair_deferred_count:
            print(
                f"generate-descriptions: deferred {repair_deferred_count} depth repair(s) "
                f"to a later run (cap {DEPTH_REPAIR_MAX_PER_RUN} per run)"
            )
        # Fresh-work-first / fewest-attempts-first before --limit and
        # budget admission (review finding on #674/#675): without this,
        # history order lets a previously-failed repair win a limit slot
        # over an untouched one. Fresh records keep history order;
        # repairs follow fewest-attempts-first, mirroring weekly admission.
        selected = _order_fresh_first_with_repairs(selected)
        if limit is not None:
            pre_limit_repairs = {
                _candidate_id(record) for record in selected if _candidate_id(record) and is_repair_candidate(record)
            }
            selected = selected[:limit]
            post_limit_repairs = {
                _candidate_id(record) for record in selected if _candidate_id(record) and is_repair_candidate(record)
            }
            repair_deferred_limit = len(pre_limit_repairs - post_limit_repairs)

    selected_repair_ids = {
        _candidate_id(record) for record in selected if _candidate_id(record) and is_repair_candidate(record)
    }

    aliases: dict[str, Any] = {"version": 1, "aliases": {}, "candidates": {}}
    aliases_value = store.read(Collection.CANDIDATE_IDENTITY_ALIASES)
    if isinstance(aliases_value, Mapping):
        aliases = dict(aliases_value)
    operation_id = f"generate-descriptions-{operation_date}"
    outcomes: dict[str, StageOutcome] = {}
    started = monotonic()
    deadline = started + STANDALONE_TOTAL_BUDGET_SECONDS
    selected_ids = [_candidate_id(record) for record in selected]
    index_by_id = {_candidate_id(record): index for index, record in enumerate(history)}

    def attempt(record: dict[str, Any]) -> StageOutcome:
        if monotonic() >= deadline:
            return StageOutcome(
                candidate_id=_candidate_id(record),
                identity_key=_candidate_identity(record),
                outcome=OUTCOME_NOT_ATTEMPTED,
                reason_code="not_attempted_stage_budget",
            )
        artifact = _call_service(
            service,
            record,
            operation_id=operation_id,
            operation_date=operation_date,
            history=history,
            aliases=aliases,
        )
        return _result_for(artifact, record)

    output_dir.mkdir(parents=True, exist_ok=True)
    executor = ThreadPoolExecutor(max_workers=worker_count)
    futures: dict[Future[StageOutcome], dict[str, Any]] = {}
    pending: set[Future[StageOutcome]] = set()
    next_index = 0

    def _attempt_view(record: dict[str, Any]) -> dict[str, Any]:
        """The mapping handed to the stage, never the persisted record."""
        if _candidate_id(record) in forced_candidate_ids:
            return {**record, "_force_synthesis": True}
        # A due repair forces synthesis even on unchanged evidence URLs,
        # so the thin text actually reaches the model (issue #674).
        if is_repair_candidate(record) and repair_due(record, run_date):
            return {**record, "_force_synthesis": True}
        return record

    for record in selected[:worker_count]:
        future = executor.submit(attempt, _attempt_view(record))
        futures[future] = record
        pending.add(future)
        next_index += 1
    interrupted = False

    def consume(future: Future[StageOutcome]) -> None:
        record = futures[future]
        outcome = future.result()
        candidate_id = _candidate_id(record)
        outcomes[candidate_id] = outcome
        if outcome.outcome not in {OUTCOME_NOT_ATTEMPTED, OUTCOME_EVIDENCE_UNCHANGED}:
            _apply_outcome(record, outcome, operation_date, operation_id=operation_id)
            store.replace(Collection.CANDIDATE_HISTORY, history)
        stage_artifact = _stage_artifact(
            operation_id,
            operation_date,
            outcomes,
            selected_ids,
            started=started,
            monotonic=monotonic,
        )
        write_stage_artifact(output_dir / GROUNDING_STAGE_ARTIFACT_NAME, stage_artifact)
        _write_generation_artifact(output_dir, operation_date, outcomes, selected_ids)

    def future_order(future: Future[StageOutcome]) -> int:
        return index_by_id.get(_candidate_id(futures[future]), 10**9)

    try:
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in sorted(done, key=future_order):
                consume(future)
                if next_index < len(selected):
                    record = selected[next_index]
                    next_index += 1
                    new_future = executor.submit(attempt, _attempt_view(record))
                    futures[new_future] = record
                    pending.add(new_future)
    except KeyboardInterrupt:
        interrupted = True
        for future in pending:
            future.cancel()
        # Results that completed before the interrupt are still durable.  A
        # worker raising KeyboardInterrupt itself is not converted to an
        # attempt and is intentionally ignored here.
        for future, record in sorted(futures.items(), key=lambda item: index_by_id.get(_candidate_id(item[1]), 10**9)):
            if _candidate_id(record) in outcomes or not future.done():
                continue
            try:
                consume(future)
            except BaseException:
                continue
        if outcomes:
            stage_artifact = _stage_artifact(
                operation_id,
                operation_date,
                outcomes,
                selected_ids,
                started=started,
                monotonic=monotonic,
            )
            write_stage_artifact(output_dir / GROUNDING_STAGE_ARTIFACT_NAME, stage_artifact)
            _write_generation_artifact(output_dir, operation_date, outcomes, selected_ids)
        raise
    finally:
        executor.shutdown(wait=not interrupted, cancel_futures=interrupted)

    if not outcomes:
        stage_artifact = _stage_artifact(
            operation_id,
            operation_date,
            outcomes,
            selected_ids,
            started=started,
            monotonic=monotonic,
        )
        write_stage_artifact(output_dir / GROUNDING_STAGE_ARTIFACT_NAME, stage_artifact)
        _write_generation_artifact(output_dir, operation_date, outcomes, selected_ids)
    _ensure_render_candidates(output_dir, history, aliases=aliases, store=store)
    # Let WeeklyReportStore resolve the stored report date.
    # operation_date belongs to attempts and stage artifacts.
    # review_config carries the retired sources whose cards stay out of the review (#1025).
    render_code = render_review(
        output_dir, state_dir=state_dir, effective_date=None, config_path=review_config, state_store=store
    )
    if render_code != 0:
        raise RuntimeError("review rendering failed")
    ordered_outcomes = [outcomes[key] for key in sorted(outcomes, key=lambda item: index_by_id.get(item, 10**9))]
    counts = {outcome: sum(item.outcome == outcome for item in ordered_outcomes) for outcome in OUTCOME_ORDER}
    repair_attempted = 0
    repair_improved = 0
    repair_deferred_budget = 0
    repair_unresolved = 0
    for candidate_id in selected_repair_ids:
        outcome = outcomes.get(candidate_id)
        if outcome is None or outcome.outcome == OUTCOME_NOT_ATTEMPTED:
            repair_deferred_budget += 1
            continue
        if outcome.outcome == OUTCOME_EVIDENCE_UNCHANGED:
            continue
        repair_attempted += 1
        if outcome.outcome in {OUTCOME_GROUNDED, OUTCOME_SINGLE_SOURCE}:
            repair_improved += 1
        elif outcome.reason_code in REPAIR_EVIDENCE_SHORTFALL_REASONS:
            repair_unresolved += 1
    return {
        "status": "ok",
        "operation_date": operation_date,
        "eligible_count": len(selected),
        "touched_count": len(ordered_outcomes),
        "counts": counts,
        "rendered": True,
        "repair": {
            "backlog": repair_backlog,
            "attempted": repair_attempted,
            "improved": repair_improved,
            "deferred_budget": repair_deferred_budget,
            "deferred_cap": repair_deferred_count,
            # Named forcing can admit a cooling or exhausted record
            # explicitly; deferral counts only repairs the run never
            # selected, mirroring the adjudication summary.
            "deferred_cooldown": len(repair_cooling_ids - selected_repair_ids),
            "deferred_exhausted": len(repair_exhausted_ids - selected_repair_ids),
            "deferred_limit": repair_deferred_limit,
            "unresolved_insufficient_evidence": repair_unresolved,
        },
    }


__all__ = [
    "ATTEMPT_FIELD",
    "FAILED_RETRY_AGE_DAYS",
    "GENERATE_DESCRIPTIONS_ARTIFACT_NAME",
    "PREFLIGHT_ADAPTER_NO_RESPONSE",
    "PREFLIGHT_ADAPTER_UNAVAILABLE",
    "PREFLIGHT_ENDPOINT_NOT_CONFIGURED",
    "STANDALONE_MAX_WORKERS",
    "STANDALONE_TOTAL_BUDGET_SECONDS",
    "DescriptionGenerationPreflightError",
    "generate_descriptions",
]
