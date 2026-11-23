"""Run health operator command: run-health."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from autoscout.doctor_state_store import finding_for
from autoscout.doctor_state_store import look_at_state_store
from autoscout.source_failure_streaks import run_view
from autoscout.source_failure_streaks import tolerated_source_names
from autoscout.source_run_status import is_stale_source_run
from autoscout.source_run_status import normalized_source_run_status
from autoscout.weekly_report import WeeklyReportStore


def _int_or(val: Any, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def recurring_readiness(
    output_dir: Path,
    ready: bool,
    editor_count: Any,
    skipped_count: int,
    failed_count: int,
    update_delta_counts: dict[str, int] | None = None,
    source_runs: list[dict[str, Any]] | None = None,
    source_failure_streaks: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The recurring run's readiness; a failed or skipped source below the streak threshold asks for no attention (#1125).

    With *source_failure_streaks* (the run's streak view) and *source_runs*,
    only the failed and skipped sources the view does not tolerate count
    towards attention; ``skipped_source_count`` and ``failed_source_count``
    still report every one. Without the view every failed or skipped source
    counts, as before.
    """
    purpose = "recurring-weekly"

    delta = update_delta_counts or {}
    new_count = _int_or(delta.get("new"))
    updated_count = _int_or(delta.get("upgraded", 0)) + _int_or(delta.get("changed", 0))
    previously_seen_count = _int_or(delta.get("unchanged"))

    problem_sources = []
    if source_runs:
        problem_sources = [
            {
                "source_name": str(source.get("source_name", "")),
                "status": str(source.get("status", "")),
                "reason": str(source.get("reason", "")),
            }
            for source in source_runs
            if str(source.get("status", "")) in {"skipped", "failed"}
        ]

    attention_failed, attention_skipped = failed_count, skipped_count
    pass_problem = False
    if source_failure_streaks is not None:
        # The view holds every attempt of the run, the capture ledger's too, so it decides alone.
        tolerated = tolerated_source_names(source_failure_streaks)
        asking = [
            str(entry.get("status", ""))
            for entry in source_failure_streaks.get("failing", [])
            if isinstance(entry, Mapping) and str(entry.get("source_name", "")) not in tolerated
        ]
        attention_failed, attention_skipped = asking.count("failed"), asking.count("skipped")
        # A Kirkus, PM or PW pass problem asks for attention even when its source was served from its previous result.
        pass_problem = bool(source_failure_streaks.get("pass_problem_sources"))

    reasons: list[str] = []
    attention_required = False
    if ready:
        health_classification = "healthy"
    else:
        attention_required = True
        health_classification = "pipeline or readiness problem"
        reasons.append("pipeline or readiness validation did not pass")

    if attention_failed:
        attention_required = True
        health_classification = "degraded"
        if not reasons:
            reasons.append(f"{attention_failed} source failure(s) blocked collection")

    if attention_skipped:
        attention_required = True
        if health_classification == "healthy":
            health_classification = "degraded"
        if not any("skipped" in r for r in reasons):
            reasons.append(f"{attention_skipped} source(s) skipped")

    if pass_problem:
        attention_required = True
        if health_classification == "healthy":
            health_classification = "degraded"
        reasons.append("a Kirkus, PM or PW collection pass reported a problem")

    if ready and not attention_required:
        health_classification = "healthy"

    return {
        "readiness_purpose": purpose,
        "health_classification": health_classification,
        "attention_required": attention_required,
        "reasons": reasons,
        "editor_facing_count": _int_or(editor_count),
        "new_count": new_count,
        "updated_count": updated_count,
        "previously_seen_count": previously_seen_count,
        "skipped_source_count": skipped_count,
        "failed_source_count": failed_count,
        "problem_sources": problem_sources,
    }


def print_source_failure_streaks(view: Mapping[str, Any] | None) -> None:
    """The run's streak view as ``source_streak*`` lines; nothing for a run without one (#1125)."""
    if view is None:
        return
    print(f"source_streak_threshold={view.get('threshold', '')}")
    for entry in view.get("failing", []):
        if not isinstance(entry, Mapping):
            continue
        level = "broken" if entry.get("broken") else ("listed" if entry.get("counted") else "not_counted")
        print(
            f"source_streak={entry.get('source_name', '')}|{level}|{entry.get('consecutive_failures', 0)}"
            f"|{entry.get('last_success_date') or 'none'}|{entry.get('reason_code', '')}"
        )
    for entry in view.get("recovered", []):
        if isinstance(entry, Mapping):
            print(f"source_recovered={entry.get('source_name', '')}|{entry.get('failed_attempts', 0)}")


def annual_rollover_findings(output_dir: Path) -> dict[str, Any]:
    """Return the weekly run's annual edition-year advisory, if it recorded one.

    Read from workflow-log.json rather than run-summary.json because this is
    an operator maintenance advisory, not a property of the editor's result.
    Absent for any run predating the check, which reports nothing rather than
    claiming a clean audit it never performed.
    """
    from autoscout.annual_source_audit import ANNUAL_AUDIT_STAGE_NAME
    from autoscout.config import read_json

    log = read_json(output_dir / "workflow-log.json", default=None)
    if not isinstance(log, list):
        return {}
    for entry in reversed(log):
        if isinstance(entry, dict) and entry.get("stage") == ANNUAL_AUDIT_STAGE_NAME:
            detail = entry.get("detail")
            return detail if isinstance(detail, dict) else {}
    return {}


def run_health(
    output_dir: Path,
    effective_date: str | None = None,
    *,
    state_dir: Path | None = None,
    config_path: Path | None = None,
) -> int:
    """Summarize the run's readiness and source health; with *state_dir*, also whether the State Store agrees (#1076).

    The State Store check is independent of the artifacts' and runs whatever
    they report: under the SQLite backend the database and the JSON export in
    *state_dir* must agree once a run has finalized. The backend is the one
    *config_path* (or the environment) selects. Either check failing fails
    the command.
    """
    returncode = _report_artifact_health(output_dir, effective_date)
    if state_dir is None:
        return returncode
    return max(returncode, _report_state_store_health(state_dir, config_path))


def _report_state_store_health(state_dir: Path, config_path: Path | None) -> int:
    """Print the State Store lines and return 1 when the database and its export disagree.

    Read-only on both backends. Nothing to compare on the JSON backend.
    """
    inspection = look_at_state_store(config_path, state_dir)
    print(f"state_backend={inspection.backend}")
    if inspection.selected_by:
        print(f"state_backend_selected_by={inspection.selected_by}")
    if inspection.database_path_class:
        print(f"state_database_path_class={inspection.database_path_class}")
    if inspection.schema_version is not None:
        print(f"state_schema_version={inspection.schema_version}")
    print(f"state_export={inspection.export_status}")
    if inspection.problem:
        print(f"state_export_problem={inspection.problem}")
    if inspection.differing_count:
        print(f"state_export_differing={inspection.differing_count}")
    for name in inspection.differing:
        print(f"state_export_file={name}")
    finding = finding_for(inspection, state_dir)
    if finding is None:
        return 0
    if finding.files_against_git:
        print(f"state_files_against_git={finding.files_against_git}")
    if finding.command:
        print(f"state_export_command={finding.command}")
    return 1


def _report_artifact_health(output_dir: Path, effective_date: str | None = None) -> int:
    report_store = WeeklyReportStore(output_dir)
    if report_store.transaction_in_progress():
        print("run_health_status=incomplete")
        print("weekly_report_transaction=in_progress")
        return 1
    required_artifacts = {
        "pilot-status.json": report_store.path("pilot-status.json"),
        "run-summary.json": report_store.path("run-summary.json"),
        "source-runs.json": report_store.path("source-runs.json"),
    }
    required_snapshot = report_store.snapshot("health", effective_date)
    if required_snapshot.transaction_in_progress or required_snapshot.read_inconsistent:
        print("run_health_status=incomplete")
        if required_snapshot.transaction_in_progress:
            print("weekly_report_transaction=in_progress")
        if required_snapshot.read_inconsistent:
            print("weekly_report_snapshot=read_inconsistent")
        return 1

    missing = [name for name in required_snapshot.missing if name in required_artifacts]
    if missing:
        print("run_health_status=incomplete")
        for name in missing:
            print(f"missing_artifact={name}")
        print(f"inspect_dir={output_dir}")
        return 1

    date_errors = report_store.date_errors(effective_date, required_snapshot)
    if required_snapshot.date_inconsistent or date_errors:
        print("run_health_status=date_inconsistent")
        for error in date_errors:
            print(f"date_mismatch={error}")
        for name in required_snapshot.date_inconsistent:
            if not any(name in error for error in date_errors):
                print(f"date_mismatch={name} report dates disagree")
        return 1

    malformed = list(required_snapshot.malformed)
    if malformed or not required_snapshot.coherent:
        print("run_health_status=incomplete")
        for name in malformed:
            print(f"invalid_artifact_shape={name} is malformed")
        if not malformed:
            print("weekly_report_snapshot=incoherent")
        return 1

    pilot_status = required_snapshot.json("pilot-status.json", {})
    run_summary = required_snapshot.json("run-summary.json", {})
    source_runs = required_snapshot.json("source-runs.json", [])
    if not isinstance(pilot_status, dict) or not isinstance(run_summary, dict):
        print("run_health_status=incomplete")
        print("invalid_artifact_shape=weekly metadata must be objects")
        return 1
    if not isinstance(source_runs, list) or any(not isinstance(source, dict) for source in source_runs):
        print("run_health_status=incomplete")
        print("invalid_artifact_shape=source-runs.json must be a list of objects")
        return 1

    ready = bool(pilot_status.get("ready_for_editor"))
    editor_count = run_summary.get("editor_facing_count", pilot_status.get("editor_facing_count", 0))
    target = run_summary.get(
        "editor_row_target",
        pilot_status.get("readiness_basis", {}).get("editor_row_target", ""),
    )
    skipped_count = int(run_summary.get("skipped_source_count", 0) or 0)
    failed_count = int(run_summary.get("failed_source_count", 0) or 0)
    shortfall_reason = str(run_summary.get("shortfall_reason", "") or "")
    readiness_purpose = str(pilot_status.get("readiness_basis", {}).get("readiness_purpose", "seed-calibration"))
    update_deltas = run_summary.get("update_delta_counts") or pilot_status.get("readiness_basis", {}).get(
        "update_delta_counts", {}
    )
    classification = run_health_classification(ready, editor_count, target, skipped_count, failed_count)
    collection_mode = str(run_summary.get("collection_mode") or run_collection_mode(source_runs))
    collection_modes = run_summary.get("collection_modes") or sorted(
        {str(source.get("collection_mode", "configured")) for source in source_runs}
    )

    workflow_health = codex_workflow_health(source_runs, run_summary)
    volume_status = source_volume_status(editor_count, target, skipped_count, failed_count)
    blocker = readiness_blocker(
        ready, classification, editor_count, target, skipped_count, failed_count, shortfall_reason, workflow_health
    )

    streak_view = run_view(run_summary)
    print(f"readiness_purpose={readiness_purpose}")
    print(f"run_health_status={'ready' if ready else 'not_ready'}")
    print(f"classification={classification}")
    print(f"codex_workflow_health={workflow_health}")
    print(f"source_volume_status={volume_status}")
    if blocker:
        print(f"readiness_blocker={blocker}")

    if readiness_purpose == "recurring-weekly":
        rec = recurring_readiness(
            output_dir,
            ready,
            editor_count,
            skipped_count,
            failed_count,
            update_delta_counts=update_deltas,
            source_runs=source_runs,
            source_failure_streaks=streak_view,
        )
        print(f"recurring_health_classification={rec['health_classification']}")
        print(f"recurring_attention_required={str(rec['attention_required']).lower()}")
        print(f"recurring_new_count={rec['new_count']}")
        print(f"recurring_updated_count={rec['updated_count']}")
        print(f"recurring_previously_seen_count={rec['previously_seen_count']}")
        for reason in rec["reasons"]:
            print(f"recurring_reason={reason}")
        for problem in rec["problem_sources"]:
            print(f"recurring_source_problem={problem['source_name']}|{problem['status']}|{problem['reason']}")
    print_source_failure_streaks(streak_view)
    annual_audit = annual_rollover_findings(output_dir)
    if annual_audit:
        status = str(annual_audit.get("status", "unknown"))
        print(f"annual_source_audit={status}")
        if status == "unknown":
            print(f"annual_source_audit_reason={annual_audit.get('reason', '')}")
        else:
            print(f"annual_source_rollover_checks={annual_audit.get('rollover_checks_required', 0)}")
            for source in annual_audit.get("sources", []) or []:
                if isinstance(source, dict):
                    print(
                        f"annual_source_rollover_check={source.get('source_name', '')}"
                        f"|{source.get('edition_year', '')}|{source.get('url', '')}"
                    )
    # The Russian-edition stage (#1181): advisory, named only when it ran.
    from autoscout.russian_edition_health import health_lines as russian_edition_health_lines
    from autoscout.russian_edition_health import last_outcome as russian_edition_outcome

    russian_edition = russian_edition_outcome(output_dir)
    if russian_edition is not None:
        for line in russian_edition_health_lines(russian_edition):
            print(line)
    print(f"collection_mode={collection_mode}")
    print(f"collection_modes={','.join(str(mode) for mode in collection_modes)}")
    print(f"ready_for_editor={str(ready).lower()}")
    print(f"editor_facing_count={editor_count}")
    print(f"editor_row_target={target}")
    print(f"skipped_source_count={skipped_count}")
    print(f"failed_source_count={failed_count}")
    if shortfall_reason:
        print(f"shortfall_reason={shortfall_reason}")
    desc_coverage = (
        required_snapshot.json("description-coverage.json", {}) or run_summary.get("description_coverage", {})
    ) or {}
    if desc_coverage:
        print(f"description_coverage={desc_coverage.get('coverage', 'unknown')}")
        print(f"description_validated_count={desc_coverage.get('validated_count', 0)}")
        print(f"description_fallback_count={desc_coverage.get('fallback_count', 0)}")
        # Depth keys are absent on legacy artifacts; their absence must not
        # read as complete depth coverage, so nothing depth-related prints
        # without them (issue #672).
        if "depth_coverage" in desc_coverage:
            print(f"description_depth_coverage={desc_coverage.get('depth_coverage', 'unknown')}")
        if "meets_depth_count" in desc_coverage or "below_depth_count" in desc_coverage:
            print(f"description_meets_depth_count={desc_coverage.get('meets_depth_count', 0)}")
            print(f"description_below_depth_count={desc_coverage.get('below_depth_count', 0)}")
        if "depth_policy_version" in desc_coverage:
            print(f"description_depth_policy_version={desc_coverage.get('depth_policy_version', 0)}")
        invalid_reasons = desc_coverage.get("invalid_reason_counts", {})
        if invalid_reasons:
            print(f"description_invalid_reasons={{{', '.join(f'{k}: {v}' for k, v in invalid_reasons.items())}}}")
        fallback_ids = desc_coverage.get("fallback_ids", [])
        for identity_key in fallback_ids:
            print(f"description_fallback_candidate={identity_key}")
        for source in desc_coverage.get("fallback_by_source", []):
            if not isinstance(source, dict):
                continue
            print(
                f"description_fallback_source={source.get('source_name', '')}: "
                f"{source.get('fallback_count', 0)} of {source.get('candidate_count', 0)}"
            )
    for caveat in pilot_status.get("caveats", []):
        print(f"caveat={caveat}")

    for source in source_runs:
        status = str(source.get("status", ""))
        if status in {"skipped", "failed"}:
            print(f"source_problem={source.get('source_name', '')}|{status}|{source.get('reason', '')}")
        mode = source.get("collection_mode")
        if mode:
            print(f"source_mode={source.get('source_name', '')}|{mode}")
        debug_metadata = source.get("debug_metadata", {})
        if isinstance(debug_metadata, dict) and debug_metadata.get("backend_name"):
            print(
                "source_backend="
                f"{source.get('source_name', '')}|"
                f"{debug_metadata.get('backend_name', '')}|"
                f"{debug_metadata.get('backend_mode', mode or '')}|"
                f"{debug_metadata.get('safe_failure_status', '')}|"
                f"{debug_metadata.get('capture_live_response_file', source.get('capture_live_response_file', ''))}|"
                f"{debug_metadata.get('capture_extraction_response_file', source.get('capture_extraction_response_file', ''))}"
            )
        if isinstance(debug_metadata, dict) and debug_metadata.get("collector_kind") == "codex-extraction":
            print(
                "source_codex="
                f"{source.get('source_name', '')}|"
                f"{source.get('status', '')}|"
                f"{debug_metadata.get('safe_failure_status', '')}|"
                f"extracted={debug_metadata.get('extracted_mention_count', 0)}|"
                f"validated={debug_metadata.get('validated_mention_count', 0)}|"
                f"dropped={debug_metadata.get('dropped_mention_count', 0)}|"
                f"field_dropped={debug_metadata.get('field_dropped_count', 0)}|"
                f"provenance_dropped={debug_metadata.get('provenance_dropped_count', 0)}"
            )
    for contribution in run_summary.get("source_contributions", [])[:5]:
        print(
            "source_contribution="
            f"{contribution.get('source_name', '')}|"
            f"{contribution.get('editor_facing_contribution', 0)}"
        )

    print(f"artifact=pilot-status.json:{required_artifacts['pilot-status.json']}")
    print(f"artifact=run-summary.json:{required_artifacts['run-summary.json']}")
    print(f"artifact=source-runs.json:{required_artifacts['source-runs.json']}")
    for artifact in pilot_status.get("shareable_artifacts", []):
        path = artifact.get("path")
        if path:
            print(f"shareable_artifact={path}:{output_dir / str(path)}")
    review_path = report_store.path("weekly-review.md")
    if review_path.exists():
        print(f"shareable_artifact=weekly-review.md:{review_path}")
    return 0


def run_collection_mode(source_runs: list[dict[str, Any]]) -> str:
    modes = {str(source.get("collection_mode", "configured")) for source in source_runs}
    if not modes:
        return "none"
    live_modes = {
        "live",
        "real-live",
        "real-live-replay",
        "real-live-unavailable",
        "fake-live",
        "fixture-live",
        "live-fixture-replay",
    }
    if modes == {"real-live"}:
        return "real-live"
    if modes == {"fake-live"}:
        return "fake-live"
    if modes <= {"fixture-live", "live-fixture-replay", "real-live-replay"}:
        return "fixture-live"
    if modes == {"live"}:
        return "live"
    if modes & live_modes:
        return "mixed"
    if modes <= {"replay", "fixture-replay"}:
        return "replay"
    if len(modes) == 1:
        return next(iter(modes))
    return "mixed"


def run_health_classification(
    ready: bool,
    editor_count: Any,
    target: Any,
    skipped_count: int,
    failed_count: int,
) -> str:
    if ready:
        return "ready"
    if failed_count:
        return "source health problem"
    if skipped_count:
        return "degraded source health"
    try:
        if int(editor_count) < int(target):
            return "source scarcity"
    except (TypeError, ValueError):
        pass
    return "pipeline or readiness problem"


def codex_workflow_health(
    source_runs: list[dict[str, Any]],
    run_summary: dict[str, Any],
) -> str:
    """Classify Codex workflow health independently of source volume.

    Returns 'healthy' when all Codex-operated sources collected freshly without
    failure, 'stale-evidence' when some sources were served from an earlier
    run's artifact, 'degraded' when some Codex sources were skipped,
    'failed' when any Codex source failed,
    'absent' when no Codex-operated sources are present.

    This is one scalar over conditions that are not ordered by nature, so a
    ladder has to pick, and any order it picks masks something. Reuse is placed
    last because the established failed/degraded contract predates it and other
    reporting depends on those two values.

    That placement is safe only because reuse is never reported through this
    value alone. ``reused_artifact_count`` in run-summary.json, the leading
    entry in ``major_warnings``, the per-source ``reused`` status, and the
    editor summary all state reuse unconditionally, whatever this value says
    (Issue #384). Read this value as "the worst named condition", not as a
    complete description of the run.
    """
    codex_runs = [
        run
        for run in source_runs
        if str(run.get("collection_mode", "")).startswith("codex")
        or (
            isinstance(run.get("debug_metadata"), dict)
            and run["debug_metadata"].get("collector_kind") == "codex-extraction"
        )
    ]
    if not codex_runs:
        return "absent"
    if any(run.get("status") == "failed" for run in codex_runs):
        return "failed"
    if any(run.get("status") == "skipped" for run in codex_runs):
        return "degraded"
    if any(
        (status := normalized_source_run_status(run.get("status", ""))) is not None and is_stale_source_run(status)
        for run in codex_runs
    ):
        return "stale-evidence"
    # Healthy: all Codex sources collected, even if row count is below target.
    return "healthy"


def source_volume_status(
    editor_count: Any,
    target: Any,
    skipped_count: int,
    failed_count: int,
) -> str:
    """Classify how source volume compares to the target, factoring in skips/fails.

    Returns 'below_target_no_failures' when row count is below target but no
    sources failed or were skipped (pure source scarcity),
    'below_target_with_skips' when skipped sources contribute to shortfall,
    'below_target_with_failures' when failed sources contribute to shortfall,
    'at_or_above_target' when row count meets or exceeds target.
    """
    try:
        count = int(editor_count)
        target_int = int(target)
    except (TypeError, ValueError):
        return "indeterminate"
    if count >= target_int:
        return "at_or_above_target"
    if failed_count:
        return "below_target_with_failures"
    if skipped_count:
        return "below_target_with_skips"
    return "below_target_no_failures"


def readiness_blocker(
    ready: bool,
    classification: str,
    editor_count: Any,
    target: Any,
    skipped_count: int,
    failed_count: int,
    shortfall_reason: str,
    workflow_health: str,
) -> str:
    """Return a human-readable summary of what's blocking readiness.

    Returns empty string when ready.
    """
    if ready:
        return ""
    if classification == "source scarcity":
        if workflow_health == "healthy":
            return (
                f"Codex workflow ran cleanly but configured source evidence "
                f"produced only {editor_count} editor-facing rows (target {target}); "
                f"no source or pipeline failures were recorded. "
                f"Add accepted sources or expand configured-source coverage "
                f"without using unconfigured discovery."
            )
        return (
            f"Insufficient source volume: {editor_count} editor-facing rows "
            f"produced (target {target}); "
            f"Codex workflow health is {workflow_health}."
        )
    if classification == "source health problem":
        return (
            f"{failed_count} source failure(s) blocked collection; "
            f"{editor_count} editor-facing rows produced (target {target}). "
            f"Inspect source-runs.json for per-source failure reasons."
        )
    if classification == "degraded source health":
        return (
            f"{skipped_count} source(s) skipped (paid, blocked, or unavailable); "
            f"{editor_count} editor-facing rows produced (target {target}). "
            f"Review skipped sources for alternative access or replacement."
        )
    if classification == "pipeline or readiness problem":
        return (
            f"Pipeline or readiness issue prevented reaching target {target}; "
            f"{editor_count} editor-facing rows produced. "
            f"Inspect pilot-status.json caveats and run-summary.json warnings."
        )
    return shortfall_reason
