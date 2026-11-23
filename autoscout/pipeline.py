"""MVP Pipeline orchestration.

Runs a single weekly scouting cycle: collect source mentions, build
Scouting Candidates, screen for exclusions and Visible Risk, apply
editor feedback, produce artifacts, and persist state.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from collections.abc import Sequence
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

from autoscout.autobiographical_context import AUTOBIOGRAPHICAL_NARRATIVE_CLASSIFICATIONS
from autoscout.candidate_identity import KeyedHistory
from autoscout.candidate_identity import assign_alias
from autoscout.candidate_identity import assign_aliases
from autoscout.candidate_identity import candidate_id_for
from autoscout.candidate_identity import empty_alias_table
from autoscout.candidate_identity import ensure_alias_table
from autoscout.candidate_identity import has_conflicting_aliases
from autoscout.candidate_identity import resolve_candidate_id
from autoscout.candidate_identity import resolve_history_match
from autoscout.candidate_schema import CandidateIsbn
from autoscout.candidate_verification import apply_reentry_note
from autoscout.candidate_verification import apply_russian_edition_verification
from autoscout.candidate_verification import has_valid_found_in_russian_status
from autoscout.candidate_verification import materialize_verified_russian_evidence
from autoscout.candidate_verification import normalize_legacy_russian_status
from autoscout.candidate_verification import replace_unrun_provider_verification
from autoscout.candidate_verification import restore_russian_edition_from_exclusion
from autoscout.candidate_verification import russian_not_run_verification
from autoscout.config import resolve_weekly_portfolio
from autoscout.content_description import description_coverage
from autoscout.content_description import description_provenance_record
from autoscout.content_description import has_nonfallback_description
from autoscout.content_description import not_yet_published_date
from autoscout.content_description import validate_content_description
from autoscout.editor_decisions import record_returned_decisions
from autoscout.editor_feedback import EDITOR_STATUSES
from autoscout.editorial_taste_profile import SOFT_PREFERENCE_RESULT_FIELDS
from autoscout.editorial_taste_profile import active_hard_rules
from autoscout.editorial_taste_profile import apply_soft_preferences
from autoscout.editorial_taste_profile import editorial_taste_profile_effects
from autoscout.editorial_taste_profile import hide_candidates_by_rules
from autoscout.editorial_taste_profile import reapply_soft_preferences
from autoscout.exclusion_history import merge_exclusion_records
from autoscout.feedback_dimensions import dimensions_from_record
from autoscout.feedback_dimensions import dimensions_from_row
from autoscout.feedback_finalization import attempt_finalization
from autoscout.identity import _match_isbn_pair
from autoscout.identity import _match_shared_author_pair
from autoscout.identity import mention_canonical_rank
from autoscout.identity import normalized_contributor_set
from autoscout.identity import normalized_identity_key
from autoscout.identity import normalized_isbn
from autoscout.identity import pick_canonical_mention
from autoscout.json_io import read_json
from autoscout.json_io import serialize_json
from autoscout.pipeline_identity import RunIdentity
from autoscout.pipeline_identity import stamps_permit_short_circuit
from autoscout.pipeline_path import PipelinePath
from autoscout.pm_publication import CITED_BEYOND_PM_DEALS_FIELD
from autoscout.pm_publication import PM_RECHECK_DATE_FIELD
from autoscout.pm_publication import cited_beyond_pm_deals
from autoscout.pm_publication import is_pm_url
from autoscout.pm_publication import pm_publication_recheck_date
from autoscout.reporting import capture_ledger_from_workflow_log
from autoscout.reporting import capture_source_outcome_counts
from autoscout.reporting import major_warnings
from autoscout.reporting import pilot_status_artifact
from autoscout.reporting import readiness_config
from autoscout.reporting import review_metadata_warnings
from autoscout.reporting import shortfall_reason
from autoscout.reporting import source_editor_facing_contributions
from autoscout.reporting import source_runs_with_pipeline_counts
from autoscout.reporting import write_pilot_editor_review_guide
from autoscout.review_hiding import EARLIER_SOURCE_NAMES_FIELD
from autoscout.review_hiding import remembered_source_names
from autoscout.review_hiding import withdrawn_source_names
from autoscout.review_rendering import assemble_cumulative_review
from autoscout.rule_suggestions import derive_rejection_pattern_suggestions
from autoscout.rule_suggestions import render_rejection_suggestions
from autoscout.run_health import codex_workflow_health
from autoscout.run_health import run_collection_mode
from autoscout.run_health import source_volume_status
from autoscout.russian_artifacts import has_russian_verification
from autoscout.russian_artifacts import russian_candidate_check_artifacts
from autoscout.russian_artifacts import russian_check_unclear_count
from autoscout.russian_artifacts import russian_edition_check_artifact
from autoscout.russian_artifacts import russian_verification_counts
from autoscout.russian_artifacts import write_russian_review_queue
from autoscout.russian_editions import apply_live_rsl_checks
from autoscout.russian_editions import configured_russian_checks
from autoscout.russian_editions import prepare_russian_edition_checks
from autoscout.russian_editions import russian_check_blocked_count
from autoscout.russian_editions import russian_check_failed_count
from autoscout.russian_editions import russian_check_skipped_count
from autoscout.russian_editions import russian_check_unimplemented_count
from autoscout.russian_editions import russian_edition_verification
from autoscout.russian_status import is_found_in_russian
from autoscout.safe_artifacts import bundle_write_lock
from autoscout.safe_artifacts import publish_bundle
from autoscout.scheduled_source_lists import collect_sources

# ---------------------------------------------------------------------------
# Backward-compatible re-exports — keep names importable from
# ``autoscout.pipeline`` for existing callers.
# ---------------------------------------------------------------------------
from autoscout.scoring import INELIGIBLE_CANDIDATE_CATEGORIES  # noqa: F401
from autoscout.scoring import SIGNAL_STRENGTH_RANKS  # noqa: F401
from autoscout.scoring import add_signal_aggregation  # noqa: F401
from autoscout.scoring import candidate_sort_key  # noqa: F401
from autoscout.scoring import exclusion_reason_for  # noqa: F401
from autoscout.scoring import merge_candidate_status  # noqa: F401
from autoscout.scoring import primary_signal  # noqa: F401
from autoscout.scoring import priority_label  # noqa: F401
from autoscout.scoring import russian_search_notes  # noqa: F401
from autoscout.scoring import signal_locator_text  # noqa: F401
from autoscout.scoring import signal_strength_rank  # noqa: F401
from autoscout.scoring import source_evidence  # noqa: F401
from autoscout.scoring import work_identity_key  # noqa: F401
from autoscout.scouting_candidates import VISIBLE_RISK_RESULT_FIELDS
from autoscout.scouting_candidates import ScoutingCandidate
from autoscout.scouting_candidates import append_source_evidence  # noqa: F401
from autoscout.scouting_candidates import authoritative_exclusion_record
from autoscout.search_optimization import SearchOptimizationInputs
from autoscout.search_optimization import SearchOptimizationReview
from autoscout.search_optimization import remove_stale_optimization_artifacts
from autoscout.source_failure_streaks import RUN_SUMMARY_KEY as SOURCE_FAILURE_STREAKS_KEY
from autoscout.source_lifecycle import DEFAULT_SOURCE_METRICS_WINDOW_WEEKS
from autoscout.source_lifecycle import compute_source_metrics
from autoscout.source_lifecycle import render_source_review
from autoscout.source_proposals import agent_proposals_from_collected_evidence
from autoscout.source_row_history import incremental_source_names
from autoscout.source_row_history import load_row_history
from autoscout.source_row_history import recorded_departure_warnings
from autoscout.source_row_history import row_count_departures
from autoscout.source_row_history import updated_row_history
from autoscout.source_row_history import write_row_history
from autoscout.source_run_status import summarize_source_runs
from autoscout.state import read_run_state
from autoscout.state import save_run_state
from autoscout.state_publication import publish_files_with_state
from autoscout.state_store import Collection
from autoscout.state_store import StateStore
from autoscout.state_store import exports_into
from autoscout.state_store import open_configured_state_store
from autoscout.state_store import open_run_output_store
from autoscout.title_evidence import merge_title_metadata
from autoscout.visible_risk import RISK_KEYWORDS  # noqa: F401
from autoscout.visible_risk import apply_visible_risk_screening
from autoscout.visible_risk import is_credible_book_content_signal  # noqa: F401
from autoscout.visible_risk import is_weak_author_context_signal  # noqa: F401
from autoscout.visible_risk import visible_risk_category_for  # noqa: F401
from autoscout.visible_risk_coverage import visible_risk_screen_coverage_counts
from autoscout.weekly_report import RUN_HISTORY_COPY
from autoscout.weekly_report import RUN_SOURCE_PROPOSALS
from autoscout.weekly_report import WeeklyReportStore
from autoscout.weekly_report import history_in_output_shards
from autoscout.weekly_report import read_run_history
from autoscout.weekly_report import write_run_history
from autoscout.weekly_report_row import editor_row_from_candidate

CandidateRecord = dict[str, Any] | ScoutingCandidate


# Excluded from the run identity. Two different reasons.
#
# operation_id and evidence_digest are the identity itself, not inputs to it.
#
# The feedback overrides are tracked separately from the evidence identity.
# State-backed editor decisions must keep using the feedback fast path, while a
# changed config override is compared with the stored run before that path is
# entered (Issue #388).
_FEEDBACK_CONFIG_KEYS = frozenset(
    {
        "candidate_history",
        "excluded_candidate_history",
        "returned_editor_rows",
    }
)

_KEYS_EXCLUDED_FROM_IDENTITY = frozenset(
    {
        "operation_id",
        # Legacy replay configs may carry this value, but the pipeline always
        # recomputes it from current inputs.
        "evidence_digest",
        *_FEEDBACK_CONFIG_KEYS,
        # A diagnostic that is required not to change canonical artifacts. If it
        # entered the identity it would change run-summary.json, which is
        # exactly the invariant it is forbidden to touch.
        "search_optimization_trial",
        # Issue #725: an advisory shadow stage the pipeline never reads.
        "editorial_fit_shadow",
    }
)

_RUSSIAN_CHECK_FILE_KEYS = frozenset(
    {
        "live_response_file",
        "live_response_files",
        "recorded_results_file",
        "recorded_results_files",
    }
)


def _digest_parts(parts: list[str]) -> str:
    return hashlib.sha256("\x1e".join(parts).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    except (OSError, ValueError):
        return None


def _append_file_digest(parts: list[str], label: str, path: Path, resolver: str) -> None:
    digest = _file_sha256(path)
    if digest:
        parts.append(f"{label}\x1f{resolver}\x1f{digest}")


def feedback_config_digest(config: dict[str, Any]) -> str:
    """Fingerprint config-provided feedback overrides when they are present."""
    overrides = {key: config[key] for key in sorted(_FEEDBACK_CONFIG_KEYS) if key in config}
    if not overrides:
        return ""
    return _digest_parts([json.dumps(overrides, sort_keys=True, ensure_ascii=False, default=str)])


def _pending_feedback_exists(store: StateStore | None) -> bool:
    if store is None:
        return False
    pending = store.read(Collection.PENDING_EDITOR_ROWS)
    return isinstance(pending, list) and bool(pending)


def run_evidence_digest(config: dict[str, Any], config_path: Path, effective_date: str | None = None) -> str:
    """Digest what this run will actually consume, at the moment it runs.

    Two things decide whether a run would produce the report already on disk:
    the artifacts it will read, and the settings that shape what it does with
    them. Both belong in the identity, and neither can be taken on trust from a
    value written earlier.

    A digest declared by ``finalize-codex-run`` is a claim about the past. The
    operator workflow documents saving extraction artifacts and re-running
    finalization, so artifacts routinely change after a config exists; a run
    that trusts the declared value cannot notice, and the evidence it reads is
    then not the evidence it claims. The bytes are re-read here instead.

    Settings matter for the same reason in the other direction. Identical
    evidence under a changed ``pilot_row_max`` or a changed
    ``russian_edition_checks`` produces a different report, so a run whose
    identity ignored them would decline to produce it (Issue #388).

    Every string value in a source that names an existing file is hashed, rather
    than a fixed list of artifact keys. A fixed list is one more place to drift
    out of step with the collectors, which is the failure this whole guard keeps
    running into. File-backed Russian-edition checks are included separately
    because they are top-level config inputs, not source inputs.
    """
    from autoscout.source_collectors.base import resolve_config_path
    from autoscout.source_collectors.codex_extraction import resolve_codex_artifact_path

    settings = {key: value for key, value in config.items() if key not in _KEYS_EXCLUDED_FROM_IDENTITY}
    if effective_date is not None:
        settings["run_date"] = effective_date
    parts = [json.dumps(settings, sort_keys=True, ensure_ascii=False, default=str)]

    sources = config.get("sources", [])
    if not isinstance(sources, list):
        sources = []
    for source in sorted(sources, key=lambda entry: str(entry.get("name", "")) if isinstance(entry, dict) else ""):
        if not isinstance(source, dict):
            continue
        for key in sorted(source):
            value = source[key]
            candidates = value if isinstance(value, list) else [value]
            for candidate in candidates:
                if not isinstance(candidate, str) or not candidate.strip():
                    continue
                try:
                    _append_file_digest(
                        parts,
                        f"source:{source.get('name', '')}\x1f{key}",
                        resolve_config_path(config_path, candidate),
                        "config",
                    )
                    if str(source.get("extraction_method", "")).strip().lower() == "codex-extraction-replay":
                        _append_file_digest(
                            parts,
                            f"source:{source.get('name', '')}\x1f{key}",
                            resolve_codex_artifact_path(config_path, source, candidate),
                            "codex",
                        )
                except (OSError, ValueError):
                    pass

    russian_checks = config.get("russian_edition_checks", [])
    if isinstance(russian_checks, list):
        for index, check in enumerate(russian_checks):
            if not isinstance(check, dict):
                continue
            for key in sorted(_RUSSIAN_CHECK_FILE_KEYS):
                value = check.get(key)
                candidates = value if isinstance(value, list) else [value]
                for candidate in candidates:
                    if not isinstance(candidate, str) or not candidate.strip():
                        continue
                    try:
                        _append_file_digest(
                            parts,
                            f"russian-check:{index}\x1f{key}",
                            resolve_config_path(config_path, candidate),
                            "config",
                        )
                    except (OSError, ValueError):
                        pass
    return _digest_parts(parts)


def run_pipeline(
    config_path: Path,
    output_dir: Path,
    state_dir: Path | None = None,
    effective_date: str | None = None,
    *,
    state_store: StateStore | None = None,
) -> dict[str, Any]:
    """Run the pipeline behind the report transaction guard.

    Canonical state goes through *state_store*, the store the caller opened
    for *state_dir*; without one, the store is opened here with the backend
    the weekly config chooses. *state_dir* still holds the run's other
    retained files (source row history, scheduled lists, the feedback
    archive transaction).
    """
    raw_config = read_json(config_path, default={})
    config = resolve_weekly_portfolio(raw_config, config_path) if isinstance(raw_config, dict) else {}
    run_date = effective_date or str(config.get("run_date") or date.today().isoformat())
    if state_store is None:
        state_store = open_configured_state_store(state_dir, raw_config if isinstance(raw_config, dict) else None)
    report_store = WeeklyReportStore(output_dir)
    pending_feedback_before_scout = _pending_feedback_exists(state_store)
    if report_store.transaction_in_progress():
        from autoscout.weekly_report import WeeklyReportTransactionInProgress

        raise WeeklyReportTransactionInProgress("Weekly Report transaction is in progress")
    if output_dir.is_dir() and report_store.path("scouting-candidates.json").is_file():
        # Only this caller knows what evidence and settings it would scout with,
        # so the question of whether the stored report answers this run at all
        # is decided here, before the feedback path is even considered (#388).
        stored_summary = read_json(report_store.path("run-summary.json"), default={})
        if not isinstance(stored_summary, dict):
            stored_summary = {}
        permitted = stamps_permit_short_circuit(
            RunIdentity(
                operation_id=str(config.get("operation_id", "")),
                evidence_digest=run_evidence_digest(config, config_path, effective_date=run_date),
            ),
            RunIdentity(
                operation_id=str(stored_summary.get("operation_id", "")).strip(),
                evidence_digest=str(stored_summary.get("evidence_digest", "")).strip(),
            ),
        )
        current_feedback_config_digest = feedback_config_digest(config)
        stored_feedback_config_digest = str(stored_summary.get("feedback_config_digest", "")).strip()
        if current_feedback_config_digest != stored_feedback_config_digest:
            permitted = False
        result = (
            attempt_finalization(output_dir, state_dir, run_date, state_store=state_store) if permitted else "skipped"
        )
        if result in {"applied", "noop"}:
            # Which path was taken has to survive the return. Flattening all
            # three into None is what let a pipeline that scouted nothing look
            # exactly like one that did, all the way to the editor (#388).
            return {
                "pipeline_path": (PipelinePath.FEEDBACK_APPLIED if result == "applied" else PipelinePath.FEEDBACK_NOOP)
            }
    with report_store.transaction("pipeline"):
        scouted = _run_pipeline(config_path, output_dir, state_dir, state_store, effective_date)
    pipeline_path = PipelinePath.SCOUTED_WITH_FEEDBACK if pending_feedback_before_scout else PipelinePath.SCOUTED
    return {**(scouted or {}), "pipeline_path": pipeline_path}


def _run_pipeline(
    config_path: Path,
    output_dir: Path,
    state_dir: Path | None,
    state_store: StateStore | None,
    effective_date: str | None = None,
) -> dict[str, Any] | None:
    """Run a single weekly scouting cycle and produce all artifacts.

    Returns a diagnostic mapping on trial-only failures (the canonical
    pipeline still completes all normal artifacts).  Returns ``None``
    when no trial is enabled or the trial succeeds.
    """
    raw_config = read_json(config_path, default={})
    config = resolve_weekly_portfolio(raw_config, config_path) if isinstance(raw_config, dict) else {}
    sources = config.get("sources", [])
    # Issue #280: explicit effective_date from weekly workflow wins over stale config.
    run_date = effective_date or str(config.get("run_date") or date.today().isoformat())

    readiness = readiness_config(config)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_store = WeeklyReportStore(output_dir)
    state = read_run_state(state_store)
    visibility_records = state.get("candidate_visibility", [])
    if not isinstance(visibility_records, list):
        visibility_records = []
    alias_table = ensure_alias_table(state.get("candidate_identity_aliases"))
    candidate_history_input = config.get("candidate_history", state["candidate_history"])
    candidate_history_input = _normalize_legacy_russian_history(candidate_history_input, run_date)
    excluded_history_input = config.get("excluded_candidate_history", state["excluded_candidate_history"])
    returned_editor_rows = list(config.get("returned_editor_rows", state["pending_feedback"]))
    russian_edition_checks = prepare_russian_edition_checks(
        config_path, output_dir, config.get("russian_edition_checks", []), run_date
    )

    # Also retains the ranking-freshness ledger and the scheduled sources' lists (#1057).
    source_collection = collect_sources(config_path, output_dir, state_dir, run_date, sources, state_store=state_store)
    source_mentions = source_collection.source_mentions
    source_proposals, new_source_proposal_count = agent_proposals_from_collected_evidence(
        source_mentions,
        sources,
        state["source_proposals"],
        run_date,
    )
    source_runs = source_collection.source_runs
    raw_candidate_count = source_collection.raw_candidate_count

    report_store.write_json("source-mentions.json", source_mentions)
    report_store.write_json("report-qa.json", config.get("report_qa", {}))
    scouting_candidates, internal_exclusions = build_scouting_candidates(
        source_mentions,
        run_date,
        russian_edition_checks,
        excluded_history_input,
        config_path,
        output_dir,
        candidate_history=candidate_history_input,
        candidate_identity_aliases=alias_table,
    )
    candidate_history, proposed_corrections, memo_queue = apply_editor_feedback(
        scouting_candidates,
        candidate_history_input,
        returned_editor_rows,
        run_date,
        candidate_identity_aliases=alias_table,
    )
    all_current_candidates = list(scouting_candidates)
    scouting_candidates, suppressed_candidates = suppress_candidates_without_material_change(
        scouting_candidates,
        candidate_history,
        candidate_identity_aliases=alias_table,
    )
    taste_profile_candidates = list(scouting_candidates)
    scouting_candidates, hidden_by_rule = hide_candidates_by_rules(
        scouting_candidates,
        state["editorial_taste_profile"],
    )
    soft_profile_effects = apply_soft_preferences(scouting_candidates, state["editorial_taste_profile"])
    taste_profile_effects = editorial_taste_profile_effects(
        taste_profile_candidates,
        state["editorial_taste_profile"],
        hidden_by_rule,
        soft_profile_effects,
    )
    apply_update_delta_labels(scouting_candidates, candidate_history, alias_table)
    scouting_candidates = sort_candidates_for_update_digest(scouting_candidates, candidate_history, alias_table)
    # The cap truncates after ranking, so it discards the lowest-ranked tail.
    row_cap_dropped_candidates: list[ScoutingCandidate] = []
    if config.get("pilot_mode"):
        row_cap_dropped_candidates = scouting_candidates[readiness["pilot_row_max"] :]
        scouting_candidates = scouting_candidates[: readiness["pilot_row_max"]]
    candidate_history = update_candidate_history_snapshots(
        candidate_history,
        all_current_candidates,
        run_date,
        hidden_by_rule,
        candidate_identity_aliases=alias_table,
    )
    all_editor_rows = [editor_row_from_candidate(candidate) for candidate in scouting_candidates]
    hidden_visibility_ids = {
        str(record.get("identity_key", "")).strip()
        for record in visibility_records
        if isinstance(record, dict)
        and str(record.get("identity_key", "")).strip()
        and str(record.get("visibility", "")).strip().lower() == "hidden"
    }
    editor_rows = [row for row in all_editor_rows if row.get("Candidate ID", "") not in hidden_visibility_ids]
    # Count the rows the cap cost the editor, using the same hidden-visibility
    # filter as editor_facing_count, so the two numbers share one unit. An
    # editor-hidden candidate was never going to be a row, so it is not a loss.
    row_cap_dropped_count = sum(
        1
        for candidate in candidate_dicts(row_cap_dropped_candidates)
        if str(candidate.get("identity_key", "")) not in hidden_visibility_ids
    )
    scouting_candidate_dicts = candidate_dicts(scouting_candidates)
    report_store.write_editor_csv(editor_rows)
    write_russian_review_queue(report_store.path("russian-review-queue.csv"), scouting_candidates)
    report_store.write_json("scouting-candidates.json", scouting_candidate_dicts)
    # Bounded internal pre-suppression candidate snapshot for the
    # post-enrichment trial refresh (Issue #391). Search Optimization Review
    # enriches Reject resurface findings with pre-suppression author and
    # source evidence that the final candidates no longer carry; the refresh
    # seam reads this snapshot instead of the post-suppression file. The
    # content is the same bounded candidate dicts the pipeline trial itself
    # consumes, never raw source prose.
    report_store.write_json("internal-pre-suppression-candidates.json", candidate_dicts(all_current_candidates))
    write_run_history(output_dir, state_dir, candidate_history, state_store)
    report_store.write_json("proposed-corrections.json", proposed_corrections)
    report_store.write_json("memo-queue.json", memo_queue)
    pending_source_proposals = [proposal for proposal in source_proposals if proposal.get("status") == "proposed"]
    if not exports_into(state_store, output_dir):
        # In a report directory that is a SQLite store's state directory this
        # file is the canonical Source Proposals, which the export writes (#1074).
        report_store.write_json(RUN_SOURCE_PROPOSALS, pending_source_proposals)
    report_store.write_json("suppressed-candidates.json", suppressed_candidates)
    report_store.write_json("hidden-by-rule.json", hidden_by_rule)
    report_store.write_json("internal-exclusions.json", internal_exclusions)
    source_runs = source_runs_with_pipeline_counts(
        source_runs,
        scouting_candidate_dicts,
        internal_exclusions,
    )
    # Issue #765: compare each fresh collection with the source's own earlier
    # runs before this run is added to the retained history.
    row_history = load_row_history(state_dir)
    incremental_sources = incremental_source_names(sources)
    row_departures = row_count_departures(source_runs, row_history, run_date, incremental_sources=incremental_sources)
    write_row_history(
        state_dir,
        updated_row_history(row_history, source_runs, run_date, incremental_sources=incremental_sources),
    )
    capture_ledger, capture_ledger_status = capture_ledger_from_workflow_log(
        output_dir, str(config.get("operation_id", "")).strip()
    )
    source_outcome_counts = capture_source_outcome_counts(
        capture_ledger if capture_ledger is not None else source_runs,
        source_runs if capture_ledger is not None else None,
        allow_pipeline_statuses=capture_ledger is None,
    )
    source_metrics = compute_source_metrics(
        sources=sources,
        source_runs=source_runs,
        candidates=scouting_candidate_dicts,
        candidate_history=candidate_history,
        run_date=run_date,
        window_weeks=int(config.get("source_metrics_window_weeks") or DEFAULT_SOURCE_METRICS_WINDOW_WEEKS),
    )
    report_store.write_json("source-metrics.json", source_metrics)
    report_store.write_text("source-review.md", render_source_review(source_metrics))
    trial_diagnostic: dict[str, Any] | None = None
    if config.get("search_optimization_trial") is True:
        optimization_candidates = [
            candidate
            for candidate in scouting_candidate_dicts
            if str(candidate.get("identity_key", "")) not in hidden_visibility_ids
        ]
        try:
            optimization_inputs = SearchOptimizationInputs(
                candidates=optimization_candidates,
                candidate_history=candidate_history,
                run_date=run_date,
                suppressed_candidates=suppressed_candidates,
                source_mentions=source_mentions,
                internal_exclusions=internal_exclusions,
                all_current_candidates=candidate_dicts(all_current_candidates),
                source_metrics=source_metrics,
            )
            SearchOptimizationReview.construct(optimization_inputs).write(output_dir)
        except Exception as exc:
            remove_stale_optimization_artifacts(output_dir)
            trial_diagnostic = {"search_optimization_trial": {"status": "failed", "exception_type": type(exc).__name__}}
    else:
        remove_stale_optimization_artifacts(output_dir)
    report_store.write_json(
        "russian-edition-checks.json",
        russian_edition_check_artifact(
            russian_edition_checks,
            source_mentions,
            scouting_candidate_dicts,
            internal_exclusions,
            run_date,
        ),
    )
    rejection_suggestions = derive_rejection_pattern_suggestions(
        candidates=candidate_dicts(all_current_candidates),
        candidate_history=candidate_history,
        run_date=run_date,
    )
    report_store.write_json("derived-rule-suggestions.json", rejection_suggestions)
    report_store.write_text("derived-rule-suggestions.md", render_rejection_suggestions(rejection_suggestions))
    report_store.write_json("source-runs.json", source_runs)
    save_run_state(
        state_store,
        candidate_history,
        merge_excluded_candidate_history(excluded_history_input, internal_exclusions),
        scouting_candidate_dicts,
        returned_editor_rows,
        source_proposals,
        candidate_visibility=visibility_records,
        candidate_identity_aliases=alias_table,
    )
    source_contributions = source_editor_facing_contributions(
        source_mentions, scouting_candidate_dicts, all_editor_rows
    )
    editor_facing_count = len(editor_rows)
    # The summary's cumulative counts follow the review, which withholds the
    # cards of Hard Rules and withdrawn sources (#1025).
    withdrawn_sources = withdrawn_source_names(sources if isinstance(sources, list) else [])
    _, cumulative_summary = assemble_cumulative_review(
        scouting_candidate_dicts,
        candidate_history,
        all_editor_rows,
        hidden_by_rule_count=len(hidden_by_rule),
        visibility_records=visibility_records,
        suppressed_candidates=suppressed_candidates,
        current_exclusions=internal_exclusions,
        excluded_candidate_history=excluded_history_input,
        hard_rules=active_hard_rules(state["editorial_taste_profile"]),
        withdrawn_sources=withdrawn_sources,
    )
    description_coverage = cumulative_summary["description_coverage"]
    source_run_summary = summarize_source_runs(run.get("status", "") for run in source_runs)
    source_statuses = list(source_run_summary.statuses)
    reused_source_count = source_run_summary.stale_source_count
    freshly_collected_count = source_run_summary.freshly_collected_count
    russian_verification_records = scouting_candidate_dicts + [
        exclusion
        for exclusion in internal_exclusions
        if isinstance(exclusion, dict) and exclusion.get("russian_edition_verification")
    ]
    russian_verification_summary: dict[str, Any] = {}
    if has_russian_verification(russian_edition_checks, russian_verification_records):
        russian_verification_summary = {
            "russian_verification_breakdown": russian_verification_counts(russian_verification_records),
            "russian_check_blocked_count": russian_check_blocked_count(russian_edition_checks),
            "russian_check_unimplemented_count": russian_check_unimplemented_count(russian_edition_checks),
        }
    run_summary = {
        "run_date": run_date,
        # Which operation these candidates came from, and which captures they
        # were scouted from. The run date answers neither: two runs on one date
        # share a date and an operation id, and differ only in their evidence
        # (Issue #388).
        "operation_id": str(config.get("operation_id", "")),
        # Recomputed from what this run consumed, never the value the config
        # declared: a declared digest is a claim about the past (Issue #388).
        "evidence_digest": run_evidence_digest(config, config_path, effective_date=run_date),
        # Config-provided feedback overrides are excluded from the evidence
        # identity, but changes to them must still trigger a full pipeline.
        "feedback_config_digest": feedback_config_digest(config),
        "readiness_purpose": readiness.get("readiness_purpose", "seed-calibration"),
        "collection_mode": run_collection_mode(source_runs),
        "collection_modes": sorted({str(source.get("collection_mode", "configured")) for source in source_runs}),
        "capture_source_outcome_counts": source_outcome_counts,
        "capture_ledger_status": capture_ledger_status,
        "inactive_source_count": source_outcome_counts["inactive"],
        "skipped_source_count": source_outcome_counts["skipped"],
        "failed_source_count": source_outcome_counts["failed"],
        "ignored_source_count": source_outcome_counts["ignored"],
        "precollected_source_count": source_outcome_counts["precollected"],
        "codex_captured_source_count": source_outcome_counts["codex-captured"],
        "unknown_source_count": source_outcome_counts["unknown"],
        "reused_artifact_count": reused_source_count,
        "freshly_collected_count": freshly_collected_count,
        "russian_check_skipped_count": russian_check_skipped_count(russian_edition_checks),
        "russian_check_failed_count": russian_check_failed_count(russian_edition_checks),
        "russian_check_unclear_count": russian_check_unclear_count(scouting_candidate_dicts),
        **russian_verification_summary,
        "visible_risk_screen_coverage_counts": visible_risk_screen_coverage_counts(scouting_candidate_dicts),
        "pending_source_proposal_count": len(pending_source_proposals),
        "new_source_proposal_count": new_source_proposal_count,
        "raw_candidate_count": raw_candidate_count,
        "excluded_candidate_count": len(internal_exclusions),
        "editor_facing_count": editor_facing_count,
        "row_cap_dropped_count": row_cap_dropped_count,
        "hidden_by_rule_count": len(hidden_by_rule),
        "editorial_taste_profile_effects": taste_profile_effects,
        "source_metrics_window_weeks": source_metrics["window_weeks"],
        "derived_rule_suggestion_count": len(rejection_suggestions["suggestions"]),
        "update_delta_counts": update_delta_counts(scouting_candidate_dicts),
        "editor_row_target": readiness["editor_row_target"],
        "source_contributions": source_contributions,
        "codex_workflow_health": codex_workflow_health(source_runs, {}),
        "source_volume_status": source_volume_status(
            editor_facing_count,
            readiness["editor_row_target"],
            source_outcome_counts["skipped"],
            source_outcome_counts["failed"],
        ),
        "shortfall_reason": shortfall_reason(
            editor_facing_count,
            readiness["editor_row_target"],
            source_outcome_counts["skipped"],
            source_outcome_counts["failed"],
            source_outcome_counts["ignored"],
            source_contributions,
        ),
        "major_warnings": major_warnings(
            source_outcome_counts["skipped"],
            source_outcome_counts["failed"],
            source_outcome_counts["ignored"],
            editor_facing_count,
            readiness["editor_row_target"],
            russian_check_skipped_count(russian_edition_checks),
            russian_check_failed_count(russian_edition_checks),
            source_contributions,
            row_cap_dropped_count,
            bool(readiness.get("pilot_row_max_configured")),
            reused_source_count,
            source_run_summary.collecting_source_count,
            source_outcome_counts["unknown"],
            capture_ledger_status,
            source_statuses=source_statuses,
        )
        + [departure.warning() for departure in row_departures]
        + review_metadata_warnings(scouting_candidate_dicts),
        "row_count_departures": [departure.to_dict() for departure in row_departures],
        "description_coverage": description_coverage,
        "rights_deal_evidence_note": ("absence of open rights/deal mentions is not evidence of no rights activity"),
    }
    # The source failure streaks describe the last collection, which a rerun after feedback does not redo;
    # the weekly run replaces or removes them in its own stage (#1125).
    try:
        previous_summary = read_json(report_store.path("run-summary.json"), default=None)
    except (OSError, ValueError):
        previous_summary = None
    if isinstance(previous_summary, dict) and SOURCE_FAILURE_STREAKS_KEY in previous_summary:
        run_summary[SOURCE_FAILURE_STREAKS_KEY] = previous_summary[SOURCE_FAILURE_STREAKS_KEY]
    report_store.write_json("run-summary.json", run_summary)
    if config.get("pilot_mode"):
        write_pilot_editor_review_guide(report_store.path("editor-review-guide.md"))
        report_store.write_json(
            "pilot-status.json",
            pilot_status_artifact(scouting_candidate_dicts, editor_rows, run_summary, readiness),
        )

    # Refresh the feedback-finalization marker so subsequent no-pending
    # runs are correctly idempotent after a full pipeline re-run.
    # Placed at the very end of a successful pipeline run so an incomplete
    # run cannot leave a stale marker that causes false no-ops.
    _refresh_feedback_marker(output_dir, state_store)

    return trial_diagnostic


def refresh_post_enrichment_artifacts(
    config_path: Path,
    output_dir: Path,
    state_dir: Path | None = None,
    effective_date: str | None = None,
    *,
    state_store: StateStore | None = None,
) -> dict[str, Any]:
    """Refresh pipeline artifacts that depend on the final candidates.

    Description enrichment can remove current candidates (authoritative
    pure-fiction exclusion) and rewrite descriptions after the pipeline has
    already written run-summary, pilot status, source metrics, and the
    Search Optimization trial review (Issue #391). This is the one canonical
    post-enrichment refresh seam: it recomputes exactly the derived
    artifacts that depend on the final scouting candidates and exclusion
    records, and leaves everything else untouched. It is idempotent when
    enrichment changed nothing.

    Canonical state goes through *state_store*, or a store opened here for
    *state_dir* with the backend the weekly config chooses. Without a state
    directory the output directory stands in for it, as it always has: its
    run copy of Candidate History is read, and its other state is read
    through a store over the output directory.
    """
    report_store = WeeklyReportStore(output_dir)
    run_summary_path = report_store.path("run-summary.json")
    if not run_summary_path.exists():
        return {"status": "skipped", "reason": "missing run-summary.json"}
    run_summary = read_json(run_summary_path, default={})
    if not isinstance(run_summary, dict):
        return {"status": "skipped", "reason": "invalid run-summary.json"}

    candidates = read_json(report_store.path("scouting-candidates.json"), default=[])
    if not isinstance(candidates, list) or any(not isinstance(candidate, Mapping) for candidate in candidates):
        return {"status": "skipped", "reason": "invalid scouting-candidates.json"}
    if state_store is None:
        state_store = open_configured_state_store(state_dir, config_path)
    refresh_run_date = str(run_summary.get("run_date", "")) or effective_date or ""
    candidates = _rescreen_visible_risk_after_enrichment(output_dir, state_store, candidates, refresh_run_date)
    taste_profile = state_store.read(Collection.EDITORIAL_PROFILE) if state_store is not None else {"entries": []}
    candidates, late_hidden_by_rule = _reapply_hard_rules_after_enrichment(
        output_dir, state_store, candidates, taste_profile, refresh_run_date
    )
    candidates, soft_profile_effects = _reapply_soft_preferences_after_enrichment(
        output_dir, state_store, candidates, taste_profile
    )
    internal_exclusions = read_json(report_store.path("internal-exclusions.json"), default=[])
    if not isinstance(internal_exclusions, list):
        internal_exclusions = []
    source_runs = read_json(report_store.path("source-runs.json"), default=[])
    if not isinstance(source_runs, list):
        source_runs = []
    source_mentions = read_json(report_store.path("source-mentions.json"), default=[])
    if not isinstance(source_mentions, list):
        source_mentions = []
    candidate_history = _canonical_or_run_copy_history(output_dir, state_store)
    if not isinstance(candidate_history, list):
        candidate_history = []
    visibility_records = _state_or_output_store(output_dir, state_store).read(Collection.CANDIDATE_VISIBILITY)
    if not isinstance(visibility_records, list):
        visibility_records = []

    hidden_visibility_ids = {
        str(record.get("identity_key", "")).strip()
        for record in visibility_records
        if isinstance(record, dict)
        and str(record.get("identity_key", "")).strip()
        and str(record.get("visibility", "")).strip().lower() == "hidden"
    }
    all_editor_rows = [editor_row_from_candidate(candidate) for candidate in candidates]
    editor_rows = [row for row in all_editor_rows if row.get("Candidate ID", "") not in hidden_visibility_ids]
    editor_facing_count = len(editor_rows)
    visible_candidates = [
        candidate
        for candidate in candidates
        if str(candidate.get("identity_key", "")).strip() not in hidden_visibility_ids
    ]

    raw_config = read_json(config_path, default={})
    config = resolve_weekly_portfolio(raw_config, config_path) if isinstance(raw_config, dict) else {}
    readiness = readiness_config(config)
    run_date = (
        str(run_summary.get("run_date", ""))
        or effective_date
        or str(config.get("run_date") or date.today().isoformat())
    )

    updated_source_runs = source_runs_with_pipeline_counts(source_runs, candidates, internal_exclusions)
    capture_ledger, capture_ledger_status = capture_ledger_from_workflow_log(
        output_dir, str(run_summary.get("operation_id", "")).strip()
    )
    source_outcome_counts = capture_source_outcome_counts(
        capture_ledger if capture_ledger is not None else updated_source_runs,
        updated_source_runs if capture_ledger is not None else None,
        allow_pipeline_statuses=capture_ledger is None,
    )
    window_weeks = int(run_summary.get("source_metrics_window_weeks") or DEFAULT_SOURCE_METRICS_WINDOW_WEEKS)
    source_metrics = compute_source_metrics(
        sources=config.get("sources", []),
        source_runs=updated_source_runs,
        candidates=candidates,
        candidate_history=candidate_history,
        run_date=run_date,
        window_weeks=window_weeks,
    )

    editor_row_target = int(run_summary.get("editor_row_target", readiness["editor_row_target"]))
    source_run_summary = summarize_source_runs(run.get("status", "") for run in updated_source_runs)
    source_statuses = list(source_run_summary.statuses)
    reused_source_count = source_run_summary.stale_source_count
    freshly_collected_count = source_run_summary.freshly_collected_count
    row_cap_dropped_count = int(run_summary.get("row_cap_dropped_count", 0) or 0)
    source_contributions = source_editor_facing_contributions(source_mentions, candidates, all_editor_rows)
    russian_check_configs = config.get("russian_edition_checks", [])
    if not isinstance(russian_check_configs, list):
        russian_check_configs = []
    current_candidate_dicts = [dict(candidate) for candidate in candidates]
    current_verification_records = current_candidate_dicts + [
        exclusion
        for exclusion in internal_exclusions
        if isinstance(exclusion, dict) and exclusion.get("russian_edition_verification")
    ]
    if has_russian_verification(russian_check_configs, current_verification_records):
        refreshed_russian_candidate_checks = russian_candidate_check_artifacts(
            russian_check_configs,
            source_mentions,
            current_candidate_dicts,
            internal_exclusions,
            run_date,
        )
        russian_breakdown = russian_verification_counts(refreshed_russian_candidate_checks)
        run_summary["russian_verification_breakdown"] = russian_breakdown
        russian_artifact = read_json(report_store.path("russian-edition-checks.json"), default={})
        if isinstance(russian_artifact, dict):
            russian_artifact["candidate_checks"] = refreshed_russian_candidate_checks
            russian_artifact["verification_breakdown"] = russian_breakdown
            report_store.write_json("russian-edition-checks.json", russian_artifact)
    else:
        run_summary.pop("russian_verification_breakdown", None)
    run_summary["russian_check_unclear_count"] = russian_check_unclear_count(current_candidate_dicts)
    run_summary["visible_risk_screen_coverage_counts"] = visible_risk_screen_coverage_counts(current_candidate_dicts)
    hidden_by_rule = read_json(report_store.path("hidden-by-rule.json"), default=[])
    if not isinstance(hidden_by_rule, list):
        hidden_by_rule = []
    run_summary["hidden_by_rule_count"] = len(hidden_by_rule)
    # A Hard Rule match count comes from the candidates before hiding, which
    # the final candidates no longer contain, so the pipeline's count stands,
    # plus the candidates this refresh hid.
    recorded_effects = run_summary.get("editorial_taste_profile_effects")
    recorded_hard_rule_effects = {
        str(effect.get("id", "")): effect
        for effect in (recorded_effects if isinstance(recorded_effects, list) else [])
        if isinstance(effect, dict) and effect.get("kind") == "hard_rule"
    }
    for record in late_hidden_by_rule:
        late_effect = recorded_hard_rule_effects.get(str(record.get("hidden_by_rule_id", "")))
        if late_effect is not None:
            late_effect["match_count"] = int(late_effect.get("match_count", 0)) + 1
            late_effect["hidden_count"] = int(late_effect.get("hidden_count", 0)) + 1
            late_effect["status"] = "matched"
    run_summary["editorial_taste_profile_effects"] = [
        recorded_hard_rule_effects.get(effect["id"], effect) if effect["kind"] == "hard_rule" else effect
        for effect in editorial_taste_profile_effects(
            [ScoutingCandidate(deepcopy(dict(candidate))) for candidate in candidates],
            taste_profile,
            hidden_by_rule,
            soft_profile_effects,
        )
    ]
    run_summary["capture_source_outcome_counts"] = source_outcome_counts
    run_summary["capture_ledger_status"] = capture_ledger_status
    run_summary["inactive_source_count"] = source_outcome_counts["inactive"]
    run_summary["skipped_source_count"] = source_outcome_counts["skipped"]
    run_summary["failed_source_count"] = source_outcome_counts["failed"]
    run_summary["ignored_source_count"] = source_outcome_counts["ignored"]
    run_summary["precollected_source_count"] = source_outcome_counts["precollected"]
    run_summary["codex_captured_source_count"] = source_outcome_counts["codex-captured"]
    run_summary["unknown_source_count"] = source_outcome_counts["unknown"]
    run_summary["reused_artifact_count"] = reused_source_count
    run_summary["freshly_collected_count"] = freshly_collected_count
    run_summary["excluded_candidate_count"] = len(internal_exclusions)
    run_summary["editor_facing_count"] = editor_facing_count
    run_summary["update_delta_counts"] = update_delta_counts(candidates)
    run_summary["source_contributions"] = source_contributions
    run_summary["description_coverage"] = description_coverage(visible_candidates)
    run_summary["source_volume_status"] = source_volume_status(
        editor_facing_count,
        editor_row_target,
        source_outcome_counts["skipped"],
        source_outcome_counts["failed"],
    )
    run_summary["shortfall_reason"] = shortfall_reason(
        editor_facing_count,
        editor_row_target,
        source_outcome_counts["skipped"],
        source_outcome_counts["failed"],
        source_outcome_counts["ignored"],
        source_contributions,
    )
    run_summary["major_warnings"] = (
        major_warnings(
            source_outcome_counts["skipped"],
            source_outcome_counts["failed"],
            source_outcome_counts["ignored"],
            editor_facing_count,
            editor_row_target,
            int(run_summary.get("russian_check_skipped_count", 0) or 0),
            int(run_summary.get("russian_check_failed_count", 0) or 0),
            source_contributions,
            row_cap_dropped_count,
            bool(readiness.get("pilot_row_max_configured")),
            reused_source_count,
            source_run_summary.collecting_source_count,
            source_outcome_counts["unknown"],
            capture_ledger_status,
            source_statuses=source_statuses,
        )
        + recorded_departure_warnings(run_summary.get("row_count_departures"))
        + review_metadata_warnings(candidates)
    )

    from autoscout.description_diagnostics import refresh_description_diagnostics

    refresh_description_diagnostics(
        run_summary,
        visible_candidates,
        read_json(report_store.path("grounded-description-stage.json"), default={}),
        read_json(report_store.path("description-adjudication-stage.json"), default={}),
    )
    report_store.write_json("source-runs.json", updated_source_runs)
    report_store.write_json("source-metrics.json", source_metrics)
    report_store.write_text("source-review.md", render_source_review(source_metrics))
    # Search Optimization trial: the pipeline built the trial from the
    # pre-enrichment candidates. Rebuild it from the final candidates so the
    # trial artifacts never describe an excluded work as a current candidate
    # (Issue #391). When the trial is disabled, preserve the pipeline's
    # stale-artifact removal behavior.
    trial_diagnostic: dict[str, Any] | None = None
    if config.get("search_optimization_trial") is True:
        optimization_candidates = [
            candidate
            for candidate in candidates
            if str(candidate.get("identity_key", "")).strip() not in hidden_visibility_ids
        ]
        suppressed_candidates = read_json(report_store.path("suppressed-candidates.json"), default=[])
        if not isinstance(suppressed_candidates, list):
            suppressed_candidates = []
        # The Reject resurface diagnostic enriches from the pre-suppression
        # candidate set, which the final candidates no longer contain. The
        # pipeline persisted a bounded internal snapshot before suppression
        # (Issue #391); the refresh drops only the candidates that
        # post-enrichment authoritative eligibility excluded and keeps
        # everything else - including candidates suppressed for editor status,
        # with their full author and source evidence.
        pre_suppression_candidates = read_json(
            report_store.path("internal-pre-suppression-candidates.json"), default=[]
        )
        if not isinstance(pre_suppression_candidates, list):
            pre_suppression_candidates = []
        refresh_all_current_candidates = _refresh_all_current_candidates(
            pre_suppression_candidates,
            internal_exclusions,
            candidates,
        )
        try:
            optimization_inputs = SearchOptimizationInputs(
                candidates=optimization_candidates,
                candidate_history=candidate_history,
                run_date=run_date,
                suppressed_candidates=suppressed_candidates,
                source_mentions=source_mentions,
                internal_exclusions=internal_exclusions,
                all_current_candidates=refresh_all_current_candidates,
                source_metrics=source_metrics,
            )
            SearchOptimizationReview.construct(optimization_inputs).write(output_dir)
        except Exception as exc:
            remove_stale_optimization_artifacts(output_dir)
            trial_diagnostic = {"search_optimization_trial": {"status": "failed", "exception_type": type(exc).__name__}}
    else:
        remove_stale_optimization_artifacts(output_dir)
    report_store.write_json("run-summary.json", run_summary)
    if config.get("pilot_mode"):
        report_store.write_json(
            "pilot-status.json", pilot_status_artifact(candidates, editor_rows, run_summary, readiness)
        )
    result = {
        "status": "ok",
        "excluded_candidate_count": len(internal_exclusions),
        "editor_facing_count": editor_facing_count,
    }
    if trial_diagnostic is not None:
        result.update(trial_diagnostic)
    # Enrichment changes the snapshot and durable facts after the initial
    # pipeline marker was written. Seal the successfully refreshed report so
    # later feedback does not re-scout and overwrite its enriched evidence.
    _refresh_feedback_marker(output_dir, state_store)
    return result


def _canonical_or_run_copy_history(output_dir: Path, state_store: StateStore | None) -> Any:
    """Candidate History from the store, or the run's copy of it when the run has no state."""
    if state_store is not None:
        return state_store.read(Collection.CANDIDATE_HISTORY)
    return read_run_history(output_dir)


def _state_or_output_store(output_dir: Path, state_store: StateStore | None) -> StateStore:
    """The run's store, or, for a run without state, a store over the output directory standing in for it."""
    return state_store if state_store is not None else open_run_output_store(output_dir)


def _rescreen_visible_risk_after_enrichment(
    output_dir: Path, state_store: StateStore | None, candidates: list[dict[str, Any]], run_date: str = ""
) -> list[dict[str, Any]]:
    """Screen `partial` candidates again now that enrichment has run (issue #714).

    The pipeline screens before description enrichment, so a Content
    Description published later must reach the screen here. Changed
    Visible Risk fields are written to the current candidates, Candidate
    History, and the latest snapshot as one bundle, so they never disagree;
    the review rebuilds the CSV from the candidates.

    The pipeline could not conclude these candidates, so it could not label
    a risk change either, and Candidate History kept the previous run's
    verdict. A concluded status that differs from that verdict is labelled
    here as the Material Change it is. Candidates the pipeline suppressed
    never reach this point; they are not enriched, so their status comes
    from the same historical description as the stored verdict.
    """
    stored_statuses = _stored_visible_risk_statuses(output_dir, state_store)
    rescreened: dict[str, dict[str, Any]] = {}
    labelled: dict[str, dict[str, Any]] = {}
    material_changes: dict[str, dict[str, Any]] = {}
    updated: list[dict[str, Any]] = []
    for candidate in candidates:
        entity = ScoutingCandidate(deepcopy(dict(candidate)))
        if entity.rescreen_visible_risk():
            identity_key = entity.identity_key()
            rescreened[identity_key] = {name: entity.data.get(name) for name in VISIBLE_RISK_RESULT_FIELDS}
            stored = stored_statuses.get(identity_key, "")
            current = str(entity.data.get("visible_risk_status", ""))
            if stored and current and current != stored and entity.data.get("update_delta") != "new":
                reasons = [str(reason) for reason in entity.data.get("material_change_reasons") or []]
                if VISIBLE_RISK_CHANGE_REASON not in reasons:
                    reasons.append(VISIBLE_RISK_CHANGE_REASON)
                entity.data["material_change_reasons"] = reasons
                if entity.data.get("update_delta") != "upgraded":
                    entity.data["update_delta"] = "changed"
                labelled[identity_key] = {
                    "material_change_reasons": reasons,
                    "update_delta": entity.data["update_delta"],
                }
                if run_date:
                    material_changes[identity_key] = {"last_material_change_date": run_date}
        updated.append(entity.data)
    if not rescreened:
        return candidates
    _publish_candidate_field_updates(
        output_dir,
        state_store,
        updated,
        rescreened,
        candidate_fields=labelled,
        history_fields=material_changes,
    )
    return updated


def _stored_visible_risk_statuses(output_dir: Path, state_store: StateStore | None) -> dict[str, str]:
    """Return the Candidate History Visible Risk status by identity key."""
    records = _canonical_or_run_copy_history(output_dir, state_store)
    if not isinstance(records, list):
        return {}
    return {
        str(record.get("identity_key", "")): str(record.get("visible_risk_status", ""))
        for record in records
        if isinstance(record, dict)
    }


def _reapply_hard_rules_after_enrichment(
    output_dir: Path,
    state_store: StateStore | None,
    candidates: list[dict[str, Any]],
    profile: dict[str, Any],
    run_date: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Hide the final candidates an active Hard Rule now matches.

    The pipeline applies Hard Rules before description enrichment, so a topic
    term that appears only in a later Content Description, as Russian terms
    usually do, never reached the matcher and the candidate stayed visible.
    A candidate hidden here leaves the current candidates and the latest
    snapshot, gains the same Candidate History marks as a pipeline hide, and
    joins ``hidden-by-rule.json``, all as one bundle. Returns the remaining
    candidates and the hidden records this refresh added.
    """
    _visible, hidden = hide_candidates_by_rules(
        [ScoutingCandidate(deepcopy(dict(candidate))) for candidate in candidates], profile
    )
    if not hidden:
        return candidates, []
    report_store = WeeklyReportStore(output_dir)
    hidden_by_rule = read_json(report_store.path("hidden-by-rule.json"), default=[])
    if not isinstance(hidden_by_rule, list):
        hidden_by_rule = []
    hidden_keys = {str(record["identity_key"]) for record in hidden}
    remaining = [candidate for candidate in candidates if str(candidate.get("identity_key", "")) not in hidden_keys]
    history_marks = {
        str(record["identity_key"]): {
            "hidden_by_rule_id": str(record["hidden_by_rule_id"]),
            "hidden_by_rule_reason": str(record["reason"]),
            "last_hidden_by_rule_date": run_date,
        }
        for record in hidden
    }
    _publish_candidate_field_updates(
        output_dir,
        state_store,
        remaining,
        {},
        history_fields=history_marks,
        removed_from_latest=frozenset(hidden_keys),
        extra_contents={report_store.path("hidden-by-rule.json"): [*hidden_by_rule, *hidden]},
    )
    return remaining, hidden


def _reapply_soft_preferences_after_enrichment(
    output_dir: Path,
    state_store: StateStore | None,
    candidates: list[dict[str, Any]],
    profile: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, int]]]:
    """Judge the final candidates against the Soft Preferences (issue #747).

    The pipeline applies Soft Preferences before description enrichment, so
    a matching term that appears only in a later Content Description must
    reach the matcher here. Changed priority labels and profile notes are
    written to the current candidates, Candidate History, and the latest
    snapshot as one bundle; nothing is written when no candidate changed.
    Returns the candidates and the per-entry effects on them.
    """
    entities = [ScoutingCandidate(deepcopy(dict(candidate))) for candidate in candidates]
    effects = reapply_soft_preferences(entities, profile)
    changed: dict[str, dict[str, Any]] = {}
    updated: list[dict[str, Any]] = []
    for candidate, entity in zip(candidates, entities, strict=True):
        result = {name: entity.data.get(name) or "" for name in SOFT_PREFERENCE_RESULT_FIELDS}
        if result == {name: candidate.get(name) or "" for name in SOFT_PREFERENCE_RESULT_FIELDS}:
            updated.append(candidate)
            continue
        changed[entity.identity_key()] = result
        updated.append({**candidate, **result})
    if changed:
        _publish_candidate_field_updates(output_dir, state_store, updated, changed)
    return updated, effects


def _publish_candidate_field_updates(
    output_dir: Path,
    state_store: StateStore | None,
    candidates: list[dict[str, Any]],
    changed_fields: dict[str, dict[str, Any]],
    *,
    candidate_fields: dict[str, dict[str, Any]] | None = None,
    history_fields: dict[str, dict[str, Any]] | None = None,
    removed_from_latest: frozenset[str] = frozenset(),
    extra_contents: dict[Path, Any] | None = None,
) -> None:
    """Write changed candidate fields to every copy.

    *changed_fields* reach Candidate History and the latest snapshot alike.
    *candidate_fields* reach only the latest snapshot, and *history_fields*
    only Candidate History, for fields one of the two copies does not carry.
    Identity keys in *removed_from_latest* leave the latest snapshot, which
    holds the current candidates, and *extra_contents* joins the run's files.

    The run's files are published as one bundle inside the State Store
    transaction that writes canonical state, so the two land together: a
    failed publish writes no state, and a failed state commit puts the run's
    files back as they were, from versions kept before the publish. A retry then finds the candidates as they were
    and computes the same change again. A collection the store holds no
    records of is left as it is, and so is a missing or malformed run copy of
    Candidate History.
    """

    def updated(records: list[Any], copy_fields: dict[str, dict[str, Any]]) -> list[Any]:
        for record in records:
            if not isinstance(record, dict):
                continue
            identity_key = str(record.get("identity_key", ""))
            if identity_key in changed_fields:
                record.update(deepcopy(changed_fields[identity_key]))
            if identity_key in copy_fields:
                record.update(deepcopy(copy_fields[identity_key]))
        return records

    report_store = WeeklyReportStore(output_dir)
    run_history_path = report_store.path(RUN_HISTORY_COPY)
    # Under a store that keeps its state elsewhere, a history file in a report
    # directory that is its state directory is the export, not a run copy (#1074).
    is_export = exports_into(state_store, output_dir)
    run_history = read_json(run_history_path, default=None) if run_history_path.exists() and not is_export else None
    destinations = [report_store.path("scouting-candidates.json")]
    contents = [serialize_json(candidates)]
    if isinstance(run_history, list):
        destinations.append(run_history_path)
        contents.append(serialize_json(updated(run_history, history_fields or {})))
    for path, value in (extra_contents or {}).items():
        destinations.append(path)
        contents.append(serialize_json(value))
    if state_store is None and history_in_output_shards(output_dir, None):
        # A run without state whose output keeps Candidate History one file per book updates it there (#1073).
        state_store = open_run_output_store(output_dir)
    with bundle_write_lock(*destinations):
        if state_store is None:
            publish_bundle(destinations, contents)
            return
        store = state_store
        state_history = store.read(Collection.CANDIDATE_HISTORY)
        latest = store.read(Collection.LATEST_SCOUTING_CANDIDATES)

        def write_state() -> None:
            nonlocal latest
            if isinstance(state_history, list) and state_history:
                store.replace(Collection.CANDIDATE_HISTORY, updated(state_history, history_fields or {}))
            if isinstance(latest, list) and latest:
                if removed_from_latest:
                    latest = [
                        record
                        for record in latest
                        if not (isinstance(record, dict) and str(record.get("identity_key", "")) in removed_from_latest)
                    ]
                store.replace(Collection.LATEST_SCOUTING_CANDIDATES, updated(latest, candidate_fields or {}))

        publish_files_with_state(store, write_state, lambda: publish_bundle(destinations, contents), destinations)


# ── Identity merging helpers (issue #434; originally #275) ────────────────


def _group_mentions_by_identity(
    source_mentions: list[dict[str, Any]],
    run_date: str,
) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    """Group source mentions into identity groups using exact identity edges.

    Two-phase deterministic algorithm:

    1. **ISBN phase** - all pairs sharing a nonblank ISBN are unioned with
       full transitivity.

    2. **Shared-author phase** - every non-ISBN pair satisfying the exact
       shared-author rule is unioned.  Connected components therefore retain
       the rule's natural transitivity, including overlapping contributor
       sets and explicit combined-author mentions."""
    valid_pairs: list[tuple[int, dict[str, Any]]] = []
    internal_exclusions: list[dict[str, Any]] = []

    for idx, mention in enumerate(source_mentions):
        evidence = ScoutingCandidate.source_evidence_from_mention(mention)
        exclusion_reason = ScoutingCandidate.exclusion_reason_for_mapping(mention)
        if exclusion_reason:
            internal_exclusions.append(
                {
                    "identity_key": normalized_identity_key(mention),
                    "title": mention.get("title", ""),
                    "primary_authors": mention.get("primary_authors", []),
                    "exclusion_reason": exclusion_reason,
                    "exclusion_date": run_date,
                    "russian_edition_url": mention.get("russian_edition_url", ""),
                    "evidence": [evidence],
                }
            )
            continue
        valid_pairs.append((idx, mention))

    n = len(valid_pairs)
    if n == 0:
        return internal_exclusions, []

    parent = list(range(n))

    def _find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: int, b: int) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[ra] = rb

    # ── Collect all edges and classify them ──
    isbn_edges: list[tuple[int, int]] = []
    shared_author_edges: list[tuple[int, int]] = []
    for i in range(n):
        mention_i = valid_pairs[i][1]
        for j in range(i + 1, n):
            mention_j = valid_pairs[j][1]
            if _match_isbn_pair(mention_i, mention_j):
                isbn_edges.append((i, j))
            elif _match_shared_author_pair(mention_i, mention_j):
                shared_author_edges.append((i, j))

    # ── Phase 1: apply all ISBN edges (full transitivity) ──
    for i, j in isbn_edges:
        ri, rj = _find(i), _find(j)
        if ri != rj:
            _union(i, j)

    # ── Phase 2: union every exact non-ISBN shared-author edge ──
    for i, j in shared_author_edges:
        _union(i, j)

    # Collect groups: each is a list of (original_index, mention).
    groups_map: dict[int, list[tuple[int, dict[str, Any]]]] = {}
    for i in range(n):
        root = _find(i)
        groups_map.setdefault(root, []).append(valid_pairs[i])

    # Sort each group's members by original index; sort groups by earliest index.
    identity_groups: list[list[dict[str, Any]]] = []
    group_earliest: list[int] = []
    for _root, members in groups_map.items():
        members.sort(key=lambda p: p[0])
        earliest = members[0][0]
        identity_groups.append([m[1] for m in members])
        group_earliest.append(earliest)

    sorted_pairs = sorted(zip(group_earliest, identity_groups, strict=False), key=lambda p: p[0])
    identity_groups = [pair[1] for pair in sorted_pairs]

    return internal_exclusions, identity_groups


def _explicit_coauthor_bridge_mention(mentions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return a combined-author mention for an explicit split-credit group.

    This narrow seam recognizes one combined row plus one singleton row for
    each contributor.  It does not change pair matching or infer a title-only
    identity, and it leaves ordinary shared-author groups on the existing
    canonical rank path.
    """
    if len(mentions) < 3:
        return None
    contributor_sets = [normalized_contributor_set(mention) for mention in mentions]
    all_contributors = set().union(*contributor_sets)
    if len(all_contributors) < 2:
        return None
    singleton_contributors = {next(iter(contributors)) for contributors in contributor_sets if len(contributors) == 1}
    if singleton_contributors != all_contributors:
        return None
    bridge_mentions = [
        mention
        for mention, contributors in zip(mentions, contributor_sets, strict=False)
        if contributors == all_contributors
    ]
    if not bridge_mentions:
        return None
    return min(bridge_mentions, key=mention_canonical_rank)


def _build_candidate_from_mention_group(
    identity_key: str,
    mentions: list[dict[str, Any]],
) -> ScoutingCandidate:
    """Build a single ScoutingCandidate from sorted mentions.

    The canonical mention (first in the sorted list after deterministic rank)
    provides canonical identity fields.  All mentions contribute source
    evidence.  ISBN is backfilled from later mentions into a blank slot;
    a conflicting nonblank ISBN is never overwritten.
    """
    if not mentions:
        raise ValueError("Cannot build candidate from empty mention group")

    # Sort by canonical rank: nonempty contributors first, then narrowest, etc.
    ranked = sorted(mentions, key=mention_canonical_rank)
    canonical_mention = _explicit_coauthor_bridge_mention(mentions) or ranked[0]
    candidate = ScoutingCandidate.from_source_mention(canonical_mention, identity_key=identity_key)
    existing_isbn = normalized_isbn(canonical_mention)

    # Accumulate evidence from all mentions (canonical first, then others).
    ranked_after_canonical = [mention for mention in ranked if mention is not canonical_mention]
    for mention in ranked_after_canonical:
        candidate.merge_source_mention(mention)
        evidence = ScoutingCandidate.source_evidence_from_mention(mention)
        candidate.append_source_evidence(evidence)
        mention_isbn = CandidateIsbn.from_record(mention)
        if not existing_isbn and mention_isbn:
            mention_isbn.apply_to(candidate.data)
            existing_isbn = mention_isbn.normalized
        # Conflicting nonblank ISBN is not overwritten; the discovery-only
        # marker always travels with the value as one atomic pair.

    # Canonical mention evidence is already included via from_source_mention.
    return candidate


def _normalize_legacy_russian_history(candidate_history: list[dict[str, Any]], run_date: str) -> list[dict[str, Any]]:
    """Normalize legacy Russian statuses before history reconciliation.

    Reconciliation copies operational fields from history into the current
    candidate, so normalization must happen on its input rather than during
    the later persistence snapshot.
    """
    normalized = deepcopy(candidate_history)
    for record in normalized:
        if isinstance(record, dict):
            normalize_legacy_russian_status(record, run_date)
    return normalized


def _reconcile_with_history(
    candidates: list[ScoutingCandidate],
    candidate_history: list[dict[str, Any]],
    alias_table: dict[str, Any] | None = None,
) -> list[ScoutingCandidate]:
    """Reconcile current candidates to compatible historical identities.

    Uses the same conservative match rules.  Determine candidate↔history
    matches globally first.  Reconcile only one-to-one pairs: a candidate
    must have exactly one compatible history record AND that record must
    have exactly one current candidate match.  If several current candidates
    target one history identity, reconcile none — preserve each current
    identity and require guarded correction.
    """
    if not candidate_history:
        return candidates

    tracking_enabled = alias_table is not None
    alias_table = ensure_alias_table(alias_table)
    history_records = [
        record
        for record in candidate_history
        if isinstance(record, dict) and str(record.get("identity_key", "")).strip()
    ]
    if not history_records:
        return candidates

    # Build compatibility matrix through one resolver. This keeps aliases,
    # legacy records without IDs, and conservative identity matching aligned.
    # The matched record is one of these objects, so its position is looked
    # up by identity rather than by comparing it with every record (#719).
    keyed_history = KeyedHistory.of(history_records)
    position_by_record = {id(record): index for index, record in enumerate(history_records)}
    cand_matches: list[list[int]] = []
    candidate_ids: list[str | None] = []
    for candidate in candidates:
        current_as_record = ScoutingCandidate.from_mapping(candidate).data
        matched_record, current_id = resolve_history_match(current_as_record, keyed_history, alias_table)
        candidate_ids.append(current_id)
        position = position_by_record.get(id(matched_record)) if matched_record is not None else None
        cand_matches.append([] if position is None else [position])

    # Count how many current candidates match each history record.
    hist_match_count: list[int] = [0] * len(history_records)
    for matches in cand_matches:
        for hi in matches:
            hist_match_count[hi] += 1

    # Reconcile one-to-one pairs only.
    for _ci, (candidate, matches) in enumerate(zip(candidates, cand_matches, strict=False)):
        if len(matches) != 1:
            continue
        hi = matches[0]
        if hist_match_count[hi] != 1:
            continue
        hist = history_records[hi]
        current_key = candidate.identity_key()
        history_key = str(hist.get("identity_key", "")).strip()
        durable_id = str(hist.get("candidate_id") or candidate_ids[_ci] or candidate_id_for(history_key or current_key))
        if tracking_enabled:
            conflict = assign_aliases(alias_table, [current_key, history_key], durable_id)
            if conflict:
                # Leave the current candidate untouched. A conflicting durable
                # identity must never silently replace editor state.
                continue
        candidate.data["identity_key"] = history_key or current_key
        if tracking_enabled:
            candidate.data["candidate_id"] = durable_id
        candidate.data["title"] = str(hist.get("title", candidate.get("title", "")))
        candidate.data["primary_authors"] = list(hist.get("primary_authors", candidate.get("primary_authors", [])))
        title_metadata = merge_title_metadata(hist, candidate.data)
        if title_metadata:
            candidate.data.update(title_metadata)
        # Stored authoritative enrichment metadata (Issue #391) outlives the
        # retailer category from current evidence. Reapply the authoritative
        # work type and genre whenever either is present so eligibility
        # cannot change without new evidence; the folded authoritative
        # category keeps the canonical exclusion rule able to recognize a
        # grounded fiction work on later runs.
        if isinstance(hist.get("russian_edition_verification"), dict) or has_valid_found_in_russian_status(hist):
            for field_name in (
                "russian_edition_status",
                "russian_edition_url",
                "russian_edition_note",
                "russian_edition_evidence",
                "russian_edition_verification",
            ):
                if field_name in hist:
                    candidate.data[field_name] = deepcopy(hist[field_name])
        authoritative_work_type = str(hist.get("authoritative_work_type", "")).strip()
        authoritative_genre = str(hist.get("authoritative_genre", "")).strip()
        if authoritative_work_type or authoritative_genre:
            candidate.data["authoritative_work_type"] = authoritative_work_type
            candidate.data["authoritative_genre"] = authoritative_genre
            if authoritative_genre:
                candidate.data["candidate_category"] = str(hist.get("candidate_category", "")).strip()

    return list(candidates)


def build_scouting_candidates(
    source_mentions: list[dict[str, Any]],
    run_date: str,
    russian_edition_checks: list[dict[str, Any]] | None = None,
    excluded_candidate_history: list[dict[str, Any]] | None = None,
    config_path: Path | None = None,
    output_dir: Path | None = None,
    candidate_history: list[dict[str, Any]] | None = None,
    candidate_identity_aliases: dict[str, Any] | None = None,
) -> tuple[list[ScoutingCandidate], list[dict[str, Any]]]:
    alias_table = ensure_alias_table(candidate_identity_aliases)
    # ── Group mentions by identity (union-find with exact identity rules) ──
    internal_exclusions, identity_groups = _group_mentions_by_identity(source_mentions, run_date)

    # ── Build candidates: canonical mention from each group, plus evidence ──
    scouting_candidates: list[ScoutingCandidate] = []
    for group in identity_groups:
        canonical_mention = _explicit_coauthor_bridge_mention(group) or pick_canonical_mention(group)
        key = normalized_identity_key(canonical_mention)
        candidate = _build_candidate_from_mention_group(key, group)
        retailer_mentions = [mention for mention in group if mention.get("retailer_discovery") is True]
        classifications = {
            str(mention.get("narrative_classification", "")).strip().casefold() for mention in retailer_mentions
        }
        qualifying_non_retailer_families = {
            "award",
            "bestseller/ranking",
            "media/curated list",
            "reader-interest",
            "rights/deal",
        }
        has_independent_non_retailer = any(
            str(mention.get("source_family", "")).strip().casefold() in qualifying_non_retailer_families
            for mention in group
        )
        if (
            retailer_mentions
            and not classifications.isdisjoint(AUTOBIOGRAPHICAL_NARRATIVE_CLASSIFICATIONS)
            and not has_independent_non_retailer
        ):
            internal_exclusions.append(
                ScoutingCandidate.from_mapping(candidate).exclusion_record(
                    "retailer-only memoir/autobiography/personal-life narrative", run_date
                )
            )
            continue
        scouting_candidates.append(candidate)

    # ── Reconcile to compatible historical identities ──
    resolved_history = (
        _normalize_legacy_russian_history(candidate_history, run_date) if candidate_history is not None else []
    )
    if resolved_history:
        scouting_candidates = _reconcile_with_history(scouting_candidates, resolved_history, alias_table)
    for candidate in scouting_candidates:
        key = candidate.identity_key()
        candidate_id = resolve_candidate_id(candidate.data, alias_table)
        if candidate_id is None:
            candidate_id = str(candidate.data.get("candidate_id", "")).strip() or candidate_id_for(key)
        conflict = assign_alias(alias_table, key, candidate_id)
        if conflict:
            raise ValueError(f"candidate identity alias conflict for {key}")
        metadata = alias_table["candidates"].setdefault(candidate_id, {"candidate_id": candidate_id})
        metadata.setdefault("candidate_id", candidate_id)
        metadata.setdefault("canonical_identity_key", key)
        metadata.setdefault("title", candidate.get("title", ""))
        metadata.setdefault("primary_authors", list(candidate.get("primary_authors", [])))
        candidate.data["candidate_id"] = candidate_id

    restore_russian_edition_states(scouting_candidates, excluded_candidate_history or [])
    apply_russian_edition_checks(
        scouting_candidates,
        russian_edition_checks or [],
        run_date,
        config_path,
        output_dir,
    )
    apply_reentry_notes(scouting_candidates, excluded_candidate_history or [])
    apply_visible_risk_screening(scouting_candidates)

    kept_candidates: list[ScoutingCandidate] = []
    for candidate_record in scouting_candidates:
        candidate_entity = ScoutingCandidate.from_mapping(candidate_record)
        exclusion_reason = candidate_entity.exclusion_reason()
        if exclusion_reason:
            # One canonical exclusion-record builder keeps bounded
            # authoritative work-type/genre metadata on the record so
            # cumulative excluded state preserves the grounded evidence
            # across runs (Issue #391).
            internal_exclusions.append(authoritative_exclusion_record(candidate_entity, exclusion_reason, run_date))
            continue
        candidate_entity.apply_signal_aggregation()
        kept_candidates.append(candidate_entity)
    kept_candidates.sort(key=lambda candidate: candidate.sort_key())

    return kept_candidates, internal_exclusions


UPDATE_DELTA_ORDER = {"new": 0, "upgraded": 1, "changed": 2, "unchanged": 3}
PRIORITY_DELTA_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3}


def priority_label_improved(current_label: str, previous_label: str) -> bool:
    if current_label not in PRIORITY_DELTA_ORDER or previous_label not in PRIORITY_DELTA_ORDER:
        return False
    return PRIORITY_DELTA_ORDER[current_label] < PRIORITY_DELTA_ORDER[previous_label]


def _resolver_table(alias_table: dict[str, Any] | None) -> dict[str, Any]:
    """Return the table a stage resolves against: its validated table, or an empty one.

    A stage validates its alias table once before its loop (#719); without a
    table it resolves against an empty one and records no aliases.
    """
    return alias_table if alias_table is not None else empty_alias_table()


def _resolve_history_for_candidate(
    candidate: CandidateRecord,
    candidate_history: KeyedHistory,
    alias_table: dict[str, Any] | None,
    resolver_table: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve one candidate against a stage's keyed history.

    *alias_table* is the stage's validated table, or None when the stage
    records no aliases; *resolver_table* is ``_resolver_table(alias_table)``.
    """
    candidate_data = ScoutingCandidate.from_mapping(candidate).data
    if has_conflicting_aliases(candidate_data, resolver_table):
        raise ValueError("candidate identity alias conflict")
    history, candidate_id = resolve_history_match(candidate_data, candidate_history, resolver_table)
    if alias_table is None:
        return history, None
    if candidate_id is None:
        current_key = str(candidate_data.get("identity_key", "")).strip()
        if current_key and current_key != "|":
            candidate_id = str(candidate_data.get("candidate_id", "")).strip() or candidate_id_for(current_key)
    if candidate_id:
        aliases = [
            str(candidate_data.get("identity_key", "")),
            str(history.get("identity_key", "")) if history else "",
        ]
        if assign_aliases(alias_table, aliases, candidate_id):
            return None, None
        candidate_data["candidate_id"] = candidate_id
    return history, candidate_id


def apply_update_delta_labels(
    candidates: Sequence[CandidateRecord],
    candidate_history: list[dict[str, Any]],
    candidate_identity_aliases: dict[str, Any] | None = None,
) -> None:
    alias_table = ensure_alias_table(candidate_identity_aliases) if candidate_identity_aliases is not None else None
    resolver_table = _resolver_table(alias_table)
    keyed_history = KeyedHistory.of(candidate_history)
    for candidate_record in candidates:
        candidate = ScoutingCandidate.from_mapping(candidate_record)
        history, candidate_id = _resolve_history_for_candidate(candidate, keyed_history, alias_table, resolver_table)
        if history is None:
            candidate.data["update_delta"] = "new"
            continue
        reasons = candidate.material_change_reasons(history)
        upgraded_reasons = {"signal strength improved", "new independent signal family"}
        changed_reasons = {"Russian status changed", "Visible Risk status changed"}
        if priority_label_improved(
            str(candidate.data.get("priority_label", "")),
            str(history.get("priority_label", "")),
        ) or any(reason in upgraded_reasons for reason in reasons):
            candidate.data["update_delta"] = "upgraded"
        elif any(reason in changed_reasons for reason in reasons):
            candidate.data["update_delta"] = "changed"
        elif reasons:
            candidate.data["update_delta"] = "changed"
        else:
            candidate.data["update_delta"] = "unchanged"


def sort_candidates_for_update_digest(
    candidates: Sequence[CandidateRecord],
    candidate_history: list[dict[str, Any]],
    candidate_identity_aliases: dict[str, Any] | None = None,
) -> list[ScoutingCandidate]:
    alias_table = ensure_alias_table(candidate_identity_aliases) if candidate_identity_aliases is not None else None
    resolver_table = _resolver_table(alias_table)
    keyed_history = KeyedHistory.of(candidate_history)
    history_order_by_id: dict[str, int] = {}
    history_order_by_key: dict[str, int] = {}
    for index, record in enumerate(candidate_history):
        history_order_by_key[str(record.get("identity_key", ""))] = index
        durable_id = str(record.get("candidate_id", "")).strip() or resolve_candidate_id(record, resolver_table)
        if durable_id:
            history_order_by_id.setdefault(durable_id, index)
    original_order = {
        ScoutingCandidate.from_mapping(candidate).identity_key(): idx for idx, candidate in enumerate(candidates)
    }

    def key(candidate_record: CandidateRecord) -> tuple[int, tuple[int, int, int], int]:
        candidate = ScoutingCandidate.from_mapping(candidate_record)
        delta = str(candidate.data.get("update_delta", "unchanged"))
        history, durable_id = _resolve_history_for_candidate(candidate, keyed_history, alias_table, resolver_table)
        stable = history_order_by_id.get(
            durable_id or "",
            history_order_by_key.get(
                candidate.identity_key(), len(candidate_history) + original_order[candidate.identity_key()]
            ),
        )
        if delta == "unchanged":
            return (UPDATE_DELTA_ORDER[delta], (0, 0, 0), stable)
        return (UPDATE_DELTA_ORDER.get(delta, 3), candidate.sort_key(), original_order[candidate.identity_key()])

    return [ScoutingCandidate.from_mapping(candidate) for candidate in sorted(candidates, key=key)]


def update_delta_counts(candidates: list[dict[str, Any]]) -> dict[str, int]:
    counts = {label: 0 for label in UPDATE_DELTA_ORDER}
    for candidate in candidates:
        label = str(candidate.get("update_delta", "unchanged"))
        if label in counts:
            counts[label] += 1
    return counts


def candidate_dicts(candidates: Sequence[CandidateRecord]) -> list[dict[str, Any]]:
    return [ScoutingCandidate.from_mapping(candidate).to_dict() for candidate in candidates]


def restore_russian_edition_states(
    candidates: Sequence[CandidateRecord], excluded_candidate_history: list[dict[str, Any]]
) -> None:
    records_by_identity: dict[str, list[dict[str, Any]]] = {}
    for record in excluded_candidate_history:
        identity_key = str(record.get("identity_key", "")).strip()
        if identity_key:
            records_by_identity.setdefault(identity_key, []).append(record)
    for candidate in candidates:
        identity_key = ScoutingCandidate.from_mapping(candidate).identity_key()
        records = records_by_identity.get(identity_key)
        if records:
            restore_russian_edition_from_exclusion(
                ScoutingCandidate.from_mapping(candidate).data,
                merge_exclusion_records(records),
            )


def apply_reentry_notes(
    candidates: Sequence[CandidateRecord], excluded_candidate_history: list[dict[str, Any]]
) -> None:
    exclusion_records_by_identity: dict[str, list[dict[str, Any]]] = {}
    for record in excluded_candidate_history:
        identity_key = str(record.get("identity_key", "")).strip()
        if identity_key:
            exclusion_records_by_identity.setdefault(identity_key, []).append(record)
    found_in_russian_history: dict[str, dict[str, Any]] = {}
    for identity_key, records in exclusion_records_by_identity.items():
        merged = merge_exclusion_records(records)
        if is_found_in_russian(merged.get("exclusion_reason", "")):
            found_in_russian_history[identity_key] = merged
    for candidate in candidates:
        apply_reentry_note(ScoutingCandidate.from_mapping(candidate).data, found_in_russian_history)


def apply_russian_edition_checks(
    candidates: Sequence[CandidateRecord],
    checks: list[dict[str, Any]],
    run_date: str,
    config_path: Path | None = None,
    output_dir: Path | None = None,
) -> None:
    candidate_mappings = [ScoutingCandidate.from_mapping(candidate).data for candidate in candidates]
    configured_checks = configured_russian_checks(checks)
    if not configured_checks:
        for candidate in candidates:
            candidate_data = ScoutingCandidate.from_mapping(candidate).data
            replace_unrun_provider_verification(candidate_data, run_date)
            normalize_legacy_russian_status(candidate_data, run_date)
            if has_valid_found_in_russian_status(candidate_data):
                materialize_verified_russian_evidence(candidate_data)
                candidate_data["russian_edition_verification"] = russian_not_run_verification(run_date)
        return
    live_evidence = apply_live_rsl_checks(candidate_mappings, configured_checks, run_date, config_path, output_dir)
    verification_by_key = russian_edition_verification(
        configured_checks,
        candidate_mappings,
        run_date,
        live_evidence,
    )
    for candidate in candidates:
        candidate_data = ScoutingCandidate.from_mapping(candidate).data
        verification = verification_by_key.get(str(candidate_data.get("identity_key", "")))
        if verification is not None:
            apply_russian_edition_verification(candidate_data, verification)


def exclusion_from_candidate(candidate: CandidateRecord, exclusion_reason: str, run_date: str) -> dict[str, Any]:
    return ScoutingCandidate.from_mapping(candidate).exclusion_record(exclusion_reason, run_date)


GENERATED_FACT_COLUMNS = {
    "Title": "title",
    "Author": "primary_authors",
    "Russian Edition Status": "russian_edition_status",
    "Visible Risk Status": "visible_risk_status",
    "Priority Label": "priority_label",
}


def apply_editor_feedback(
    candidates: Sequence[CandidateRecord],
    existing_history: list[dict[str, Any]],
    returned_rows: list[dict[str, Any]],
    run_date: str,
    candidate_identity_aliases: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    alias_table = ensure_alias_table(candidate_identity_aliases) if candidate_identity_aliases is not None else None
    resolver_table = _resolver_table(alias_table)
    history_by_identity = {str(record.get("identity_key", "")): dict(record) for record in existing_history}
    candidates_by_identity = {
        ScoutingCandidate.from_mapping(candidate).identity_key(): candidate for candidate in candidates
    }
    proposed_corrections: list[dict[str, Any]] = []
    memo_queue: list[dict[str, Any]] = []
    decision_rows: dict[str, tuple[str, str, list[dict[str, Any]]]] = {}

    for row in returned_rows:
        identity_key = str(row.get("identity_key", row.get("Candidate ID", ""))).strip()
        candidate_record = candidates_by_identity.get(identity_key)
        if candidate_record is None:
            row_id = resolve_candidate_id({"identity_key": identity_key}, resolver_table)
            if row_id:
                matching = [
                    candidate
                    for candidate in candidates
                    if resolve_candidate_id(ScoutingCandidate.from_mapping(candidate).data, resolver_table) == row_id
                ]
                if len(matching) == 1:
                    candidate_record = matching[0]
        candidate = ScoutingCandidate.from_mapping(candidate_record) if candidate_record is not None else None
        # Each row may add a record to history, so every row keys the
        # history it resolves against. Returned rows are few.
        row_history = KeyedHistory.of(list(history_by_identity.values()))
        if candidate is not None:
            record, _candidate_id = _resolve_history_for_candidate(candidate, row_history, alias_table, resolver_table)
        else:
            row_record = {
                "identity_key": identity_key,
                "title": row.get("Title", row.get("title", "")),
                "primary_authors": [],
            }
            record, _candidate_id = resolve_history_match(row_record, row_history, resolver_table)
        if record is None:
            record = history_by_identity.get(identity_key)
        if record is None and candidate is not None:
            record = {
                "identity_key": candidate.identity_key(),
                "candidate_id": _candidate_id or str(candidate.get("candidate_id", "")),
            }
        if record is None:
            continue
        record_key = str(record.get("identity_key", identity_key))
        # The decisions are logged once per record, after every row (issue #1053).
        decision_rows.setdefault(
            record_key,
            (str(record.get("editor_status", "") or ""), str(record.get("editor_note", "") or ""), []),
        )[2].append(row)
        editor_status = str(row.get("Editor Status", "")).strip()
        if editor_status in EDITOR_STATUSES:
            record["editor_status"] = editor_status
            if candidate is not None:
                candidate.set_editor_status(editor_status)
        if "Editor Note" in row:
            record["editor_note"] = str(row.get("Editor Note", ""))
            if candidate is not None:
                candidate.set_editor_note(record["editor_note"])
        # Issue #678: structured dimensions are decision data and must
        # survive the full-pipeline consumption path, not just in-place
        # finalization. Both readers go through the shared row helper.
        dimensions = dimensions_from_row(row)
        if dimensions is not None:
            record["feedback_dimensions"] = dimensions
            if candidate is not None:
                candidate.set_feedback_dimensions(dimensions)
        if candidate is not None:
            record["last_seen_date"] = run_date
        record["memo_queue_marker"] = (
            "needs_deeper_analysis" if record.get("editor_status") == "needs_deeper_analysis" else ""
        )
        if record["memo_queue_marker"]:
            memo_queue.append(
                {
                    "identity_key": record_key,
                    "title": candidate.get("title", "") if candidate is not None else str(row.get("Title", "")),
                    "editor_note": record.get("editor_note", ""),
                    "status": "queued_for_future_memo",
                }
            )

        if candidate is not None:
            proposed_corrections.extend(proposed_corrections_from_row(row, candidate))
        history_by_identity[record_key] = record

    for record_key, (previous_status, previous_note, rows) in decision_rows.items():
        record_returned_decisions(history_by_identity[record_key], previous_status, previous_note, rows)
    return list(history_by_identity.values()), proposed_corrections, memo_queue


VISIBLE_RISK_CHANGE_REASON = "Visible Risk status changed"

# The Visible Risk verdict and the coverage that explains it travel together.
VISIBLE_RISK_VERDICT_FIELDS = frozenset(
    {"visible_risk_status", "visible_risk_category", "visible_risk_comment", "visible_risk_screen_coverage"}
)

HISTORY_SNAPSHOT_FIELDS = (
    "title",
    "primary_authors",
    "candidate_category",
    "short_description",
    "content_description",
    "description_provenance",
    "autobiographical_subject_context",
    "description",
    "english_title",
    "english_title_type",
    "english_title_provenance",
    "english_author_display",
    "english_annotation",
    "publisher",
    "publication_year",
    "isbn",
    "isbn_discovery_only",
    "source_evidence",
    "independent_signal_count",
    "independent_signal_families",
    "primary_signal",
    "primary_signal_strength",
    "signal_summary",
    "priority_label",
    "russian_edition_status",
    "russian_edition_url",
    "russian_edition_note",
    "russian_edition_evidence",
    "russian_edition_verification",
    "visible_risk_status",
    "visible_risk_category",
    "visible_risk_comment",
    "visible_risk_screen_coverage",
    "risk_signal_scope",
    "risk_evidence_url",
    "editorial_profile_notes",
    # Authoritative enrichment metadata (Issue #391): declared explicitly so
    # snapshot updates never rely on unknown-field preservation. The generic
    # branch writes a field only when the current candidate carries it, so an
    # absent current field never overwrites a stored authoritative value.
    "authoritative_genre",
    "authoritative_work_type",
    "content_description_rejection_reason",
)


def update_candidate_history_snapshots(
    candidate_history: list[dict[str, Any]],
    candidates: Sequence[CandidateRecord],
    run_date: str,
    hidden_by_rule: list[dict[str, Any]] | None = None,
    candidate_identity_aliases: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    # Keep direct helper callers on the durable-ID path even when they do not
    # supply a table. The local table is not persisted, but it prevents a new
    # record from silently omitting its candidate_id.
    alias_table = ensure_alias_table(candidate_identity_aliases)
    resolver_table = _resolver_table(alias_table)
    keyed_history = KeyedHistory.of(candidate_history)
    history_by_identity = {str(record.get("identity_key", "")): dict(record) for record in candidate_history}
    for record in history_by_identity.values():
        normalize_legacy_russian_status(record, run_date)
    hidden_by_identity = {str(record.get("identity_key", "")): record for record in (hidden_by_rule or [])}
    for candidate in candidates:
        candidate_entity = ScoutingCandidate.from_mapping(candidate)
        identity_key = str(candidate_entity.get("identity_key", "")).strip()
        if not identity_key:
            continue
        resolved_history, candidate_id = _resolve_history_for_candidate(
            candidate_entity, keyed_history, alias_table, resolver_table
        )
        if resolved_history is not None:
            resolved_key = str(resolved_history.get("identity_key", "")).strip()
            record = history_by_identity.get(resolved_key, dict(resolved_history))
            record_key = resolved_key or identity_key
        else:
            record_key = identity_key
            record = history_by_identity.get(record_key, {"identity_key": record_key})
        if candidate_id:
            record["candidate_id"] = candidate_id
        # A missing first-seen date in legacy state means the oldest known date
        # is the safest reconstruction. It must never move forward on a later
        # appearance.
        record.setdefault("first_seen_date", str(record.get("last_seen_date", "")) or run_date)
        record["last_seen_date"] = run_date
        # Material Change date: only set/advance when the candidate *already
        # existed* in history and the current appearance qualifies as upgraded
        # or changed. A brand-new record mislabeled 'changed'/'upgraded' must
        # not receive a fake Updated date.
        was_existing = record_key in history_by_identity
        update_delta = str(candidate_entity.get("update_delta", ""))
        if was_existing and update_delta in {"upgraded", "changed"}:
            record["last_material_change_date"] = run_date
        # An anomalous "new" label on an existing record must not erase a
        # prior material-change date; unchanged or unrecognised labels
        # preserve whatever durable state was already recorded.
        if not was_existing:
            record.pop("last_material_change_date", None)
        candidate_data = candidate_entity.data
        # The snapshot below replaces the evidence with this week's. When only
        # a PM deal cites the book this week but another source cited it
        # before, that citation is remembered first, so the book is never
        # held back as PM-only (issue #947). Other records need no flag.
        if cited_beyond_pm_deals(record.get("source_evidence")) and not cited_beyond_pm_deals(
            candidate_data.get("source_evidence")
        ):
            record[CITED_BEYOND_PM_DEALS_FIELD] = True
        # The names of the sources that drop out of the evidence are kept as
        # well: the review must not take a book an active source reported
        # earlier for one that only a retired source ever reported (#1025).
        earlier_names = remembered_source_names(record, candidate_data.get("source_evidence"))
        if earlier_names:
            record[EARLIER_SOURCE_NAMES_FIELD] = earlier_names
        else:
            record.pop(EARLIER_SOURCE_NAMES_FIELD, None)
        # The earliest re-check date a PM deal gave survives the snapshot,
        # so a later deal for the same book never moves it later.
        held_before = pm_publication_recheck_date(record)
        if held_before is not None:
            record[PM_RECHECK_DATE_FIELD] = held_before.isoformat()
        for field_name in HISTORY_SNAPSHOT_FIELDS:
            if field_name == "content_description":
                # Apply content_description with provenance-aware preservation:
                # - editor-corrected descriptions are authoritative and never overwritten
                # - valid existing descriptions are not replaced by different wording
                # - blank/fallback descriptions may be replaced by newly validated ones
                prov_record = record.get("description_provenance")
                if isinstance(prov_record, dict) and prov_record.get("provenance") == "editor-corrected":
                    continue
                existing_prov_str = (
                    str(prov_record.get("provenance", "")).strip()
                    if isinstance(prov_record, dict)
                    else str(prov_record or "").strip()
                )
                if existing_prov_str == "editor-corrected":
                    continue
                # A book found not yet published keeps its placeholder: a
                # description extracted from the title this week is the
                # guess the verdict already set aside, and the stages that
                # would check it are waiting for the re-check date (issue
                # #791).
                if not_yet_published_date(record) is not None:
                    continue
                new_desc = str(candidate_data.get("content_description", "")).strip()
                if new_desc:
                    validated, _reason = validate_content_description(new_desc)
                    if validated is not None:
                        existing_desc = str(record.get("content_description", "")).strip()
                        if not has_nonfallback_description(existing_desc):
                            # Blank or fallback — replace with the new validated description
                            identity_key = str(candidate_data.get("identity_key", ""))
                            # A PM deal page is never a description's evidence (issue #947).
                            evidence_urls = [
                                str(ev.get("evidence_url", ""))
                                for ev in candidate_data.get("source_evidence", [])
                                if ev.get("evidence_url") and not is_pm_url(ev.get("evidence_url"))
                            ]
                            record["content_description"] = new_desc
                            record["description_provenance"] = description_provenance_record(
                                identity_key,
                                new_desc,
                                str(candidate_data.get("description_provenance", "codex-generated")),
                                evidence_basis=evidence_urls,
                                applied_date=run_date,
                            )
                        # Else: valid existing description — preserve, don't overwrite
            elif field_name in {"publisher", "publication_year"}:
                # Bibliographic enrichment is cumulative: a later extraction
                # that omits these fields must not erase researched metadata.
                if candidate_data.get(field_name) or field_name not in record:
                    record[field_name] = deepcopy(candidate_data.get(field_name, ""))
            elif field_name == "isbn":
                # Fill blank historical ISBN together with its identity-use
                # state, applied atomically as one pair (issue #531); never
                # overwrite a conflicting nonblank history ISBN.
                candidate_isbn = CandidateIsbn.from_record(candidate_data)
                history_isbn = CandidateIsbn.from_record(record)
                if candidate_isbn and not history_isbn:
                    candidate_isbn.apply_to(record)
            elif field_name == "isbn_discovery_only":
                # Consumed atomically in the isbn branch above; a marker
                # must never reach history without its ISBN value.
                continue
            elif field_name == "english_title":
                # Typed title metadata is one protected bundle. A valid
                # sourced title is never replaced by a rendering or legacy
                # untyped helper.
                title_metadata = merge_title_metadata(record, candidate_data)
                if title_metadata:
                    record.update(title_metadata)
            elif field_name in {"english_title_type", "english_title_provenance"}:
                continue
            elif field_name == "candidate_category":
                # The stored authoritative genre category (Issue #391) is
                # preserved over a retailer category from current evidence.
                if record.get("authoritative_genre"):
                    continue
                if field_name in candidate_data:
                    record[field_name] = deepcopy(candidate_data[field_name])
            elif field_name in {
                "russian_edition_status",
                "russian_edition_url",
                "russian_edition_note",
                "russian_edition_evidence",
                "russian_edition_verification",
            }:
                # Source-mention projection supplies empty defaults for these
                # fields. Do not let those defaults erase a normalized legacy
                # state when this run has no operational Russian verification.
                if field_name not in candidate_data:
                    continue
                current_verification = candidate_data.get("russian_edition_verification")
                has_current_verification = isinstance(current_verification, dict) and bool(current_verification)
                if candidate_data.get(field_name) or field_name not in record or has_current_verification:
                    record[field_name] = deepcopy(candidate_data.get(field_name, ""))
            elif field_name in VISIBLE_RISK_VERDICT_FIELDS:
                # A screen that concluded nothing this run (no Content
                # Description yet) keeps the stored verdict as one bundle, so
                # the post-enrichment rescreen compares its conclusion with
                # the previous run's rather than with this run's blank.
                if not str(candidate_data.get("visible_risk_status", "")) and str(
                    record.get("visible_risk_status", "")
                ):
                    continue
                if field_name in candidate_data:
                    record[field_name] = deepcopy(candidate_data[field_name])
            elif field_name != "description_provenance" and field_name in candidate_data:
                record[field_name] = deepcopy(candidate_data[field_name])
        signal_snapshot = candidate_data.get("primary_signal") or {}
        if signal_snapshot:
            record["primary_signal_strength"] = str(signal_snapshot.get("signal_strength", ""))
        # Feedback is authoritative.  The candidate snapshot takes precedence
        # when it carries editor fields (e.g. in-place finalization then
        # full-pipeline fallthrough).  When the snapshot omits them, the
        # history record already retains any previously-stored values.
        if candidate_data.get("editor_status"):
            record["editor_status"] = str(candidate_data["editor_status"])
        if candidate_data.get("editor_note"):
            record["editor_note"] = str(candidate_data["editor_note"])
        candidate_dimensions = dimensions_from_record(candidate_data)
        if candidate_dimensions is not None:
            record["feedback_dimensions"] = candidate_dimensions
        hidden_record = hidden_by_identity.get(identity_key) or hidden_by_identity.get(record_key)
        if hidden_record:
            record["hidden_by_rule_id"] = str(hidden_record.get("hidden_by_rule_id", ""))
            record["hidden_by_rule_reason"] = str(hidden_record.get("reason", ""))
            record["last_hidden_by_rule_date"] = run_date
        else:
            record.pop("hidden_by_rule_id", None)
            record.pop("hidden_by_rule_reason", None)
        history_by_identity[record_key] = record

    # Migrate the date for legacy records without treating migration as a new
    # sighting. A record with no known date remains safely date-less.
    for record in history_by_identity.values():
        if not record.get("first_seen_date") and record.get("last_seen_date"):
            record["first_seen_date"] = str(record["last_seen_date"])
    return list(history_by_identity.values())


def merge_excluded_candidate_history(
    existing_history: list[dict[str, Any]], internal_exclusions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    records_by_identity: dict[str, list[dict[str, Any]]] = {}
    for records in (existing_history, internal_exclusions):
        for record in records:
            identity_key = str(record.get("identity_key", "")).strip()
            if identity_key:
                records_by_identity.setdefault(identity_key, []).append(record)
    return [merge_exclusion_records(records) for records in records_by_identity.values()]


# The editor statuses that hide a work with no Material Change.
SUPPRESSING_EDITOR_STATUSES = frozenset({"reject", "already_known"})


def suppress_candidates_without_material_change(
    candidates: Sequence[CandidateRecord],
    candidate_history: list[dict[str, Any]],
    candidate_identity_aliases: dict[str, Any] | None = None,
) -> tuple[list[ScoutingCandidate], list[dict[str, str]]]:
    alias_table = ensure_alias_table(candidate_identity_aliases) if candidate_identity_aliases is not None else None
    resolver_table = _resolver_table(alias_table)
    keyed_history = KeyedHistory.of(candidate_history)
    kept: list[ScoutingCandidate] = []
    suppressed = []
    for candidate_record in candidates:
        candidate = ScoutingCandidate.from_mapping(candidate_record)
        history, _candidate_id = _resolve_history_for_candidate(candidate, keyed_history, alias_table, resolver_table)
        editor_status = str((history or {}).get("editor_status", ""))
        reasons = material_change_reasons(candidate, history)
        if editor_status in SUPPRESSING_EDITOR_STATUSES and not reasons:
            suppressed.append(
                {
                    "identity_key": candidate.get("identity_key", ""),
                    "title": candidate.get("title", ""),
                    "editor_status": editor_status,
                    "suppression_reason": "suppressed by editor status without Material Change",
                }
            )
            continue
        if reasons:
            candidate.set_material_change_reasons(reasons)
        kept.append(candidate)
    return kept, suppressed


def material_change_reasons(candidate: CandidateRecord, history: dict[str, Any] | None) -> list[str]:
    return ScoutingCandidate.from_mapping(candidate).material_change_reasons(history)


def proposed_corrections_from_row(row: dict[str, Any], candidate: CandidateRecord) -> list[dict[str, str]]:
    corrections = []
    for column, field in GENERATED_FACT_COLUMNS.items():
        if column not in row:
            continue
        proposed_value = str(row.get(column, ""))
        current_value = generated_fact_value(candidate, field)
        if proposed_value and proposed_value != current_value:
            corrections.append(
                {
                    "identity_key": candidate.get("identity_key", ""),
                    "field": "title" if field == "title" else field,
                    "current_value": current_value,
                    "proposed_value": proposed_value,
                    "status": "proposed",
                }
            )
    return corrections


def generated_fact_value(candidate: CandidateRecord, field: str) -> str:
    return ScoutingCandidate.from_mapping(candidate).generated_fact_value(field)


def _refresh_all_current_candidates(
    pre_suppression_candidates: list[dict[str, Any]],
    internal_exclusions: list[dict[str, Any]],
    fallback_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return the pre-suppression candidate set for the post-enrichment refresh.

    Candidates whose identity keys appear in the post-enrichment exclusion
    records were removed by authoritative eligibility and are dropped;
    candidates suppressed for editor status stay, with their full author and
    source evidence. When no pre-suppression snapshot exists (legacy
    artifacts), the final candidates serve as the fallback (Issue #391).
    """
    if not pre_suppression_candidates:
        return list(fallback_candidates)
    excluded_identity_keys = {
        str(record.get("identity_key", "")).strip()
        for record in internal_exclusions
        if isinstance(record, dict) and str(record.get("identity_key", "")).strip()
    }
    return [
        candidate
        for candidate in pre_suppression_candidates
        if isinstance(candidate, dict) and str(candidate.get("identity_key", "")).strip() not in excluded_identity_keys
    ]


def _refresh_feedback_marker(output_dir: Path, state_store: StateStore | None) -> None:
    """Write a fresh feedback-finalization marker after a full pipeline run.

    Keeps the marker in sync with current state so a subsequent no-pending
    run correctly short-circuits instead of falling through again.
    """
    if state_store is None:
        return
    from autoscout.feedback_finalization import _snapshot_digest
    from autoscout.feedback_finalization import _state_guard_digest
    from autoscout.feedback_finalization import _write_marker

    digest = _snapshot_digest(output_dir)
    guard = _state_guard_digest(state_store)
    if digest and guard:
        _write_marker(output_dir, digest, guard)
