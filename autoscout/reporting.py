"""Readiness, pilot status, run summary, and reporting helpers.

Extracted from ``autoscout.pipeline`` as part of issue #216.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from autoscout.json_io import read_json
from autoscout.russian_status import is_unclear
from autoscout.source_proposals import normalize_source_identity
from autoscout.source_run_status import SOURCE_STATUS_COLLECTED
from autoscout.source_run_status import SOURCE_STATUS_NOT_DUE
from autoscout.source_run_status import SOURCE_STATUS_REUSED
from autoscout.source_run_status import normalized_source_run_status
from autoscout.source_run_status import summarize_source_runs
from autoscout.visible_risk_coverage import FAILED
from autoscout.visible_risk_coverage import SCREENED
from autoscout.visible_risk_coverage import VISIBLE_RISK_SCREEN_COVERAGE_FIELD
from autoscout.visible_risk_coverage import recorded_coverage
from autoscout.visible_risk_coverage import visible_risk_screen_coverage_counts
from autoscout.weekly_report import WEEKLY_REPORT_ARTIFACTS

VALID_READINESS_PURPOSES = ("recurring-weekly", "seed-calibration")

# These are source-run accounting statuses, not artifact-freshness policy.
# A stale artifact still produced mentions that must receive the same pipeline
# counts as a freshly collected source (Issue #384).
COLLECTING_STATUSES = frozenset({SOURCE_STATUS_COLLECTED, SOURCE_STATUS_REUSED})
CAPTURE_SOURCE_OUTCOMES = (
    "inactive",
    "skipped",
    "ignored",
    "failed",
    "precollected",
    "codex-captured",
    # Issue #1057: an annual or monthly source outside its collection weekday.
    SOURCE_STATUS_NOT_DUE,
)

_DEFAULT_READINESS_PURPOSE = "seed-calibration"
_REPORT_ARTIFACT_NAMES = frozenset(artifact.name for artifact in WEEKLY_REPORT_ARTIFACTS)


def _report_artifact_name(name: str) -> str:
    if name not in _REPORT_ARTIFACT_NAMES:
        raise ValueError(f"Unknown Weekly Report artifact: {name}")
    return name


def readiness_config(config: dict[str, Any]) -> dict[str, Any]:
    target = int(config.get("editor_row_target") or 10)
    pilot_min = int(config.get("pilot_row_min") or config.get("pilot_row_target_min") or 8)
    pilot_max = int(config.get("pilot_row_max") or config.get("pilot_row_target_max") or 12)
    purpose = str(config.get("readiness_purpose") or _DEFAULT_READINESS_PURPOSE)
    if purpose not in VALID_READINESS_PURPOSES:
        purpose = _DEFAULT_READINESS_PURPOSE
    return {
        "editor_row_target": target,
        "pilot_row_min": pilot_min,
        "pilot_row_max": pilot_max,
        "readiness_purpose": purpose,
        # pilot_row_max carries two meanings. A config that sets it declares a
        # safety ceiling that should not bind, so a truncation there is a finding.
        # A config that omits it inherits the small seed-calibration band, where
        # truncation is the intended sampling and not worth a warning.
        "pilot_row_max_configured": bool(config.get("pilot_row_max") or config.get("pilot_row_target_max")),
    }


def source_editor_facing_contributions(
    source_mentions: list[dict[str, Any]],
    scouting_candidates: list[dict[str, Any]],
    editor_rows: list[dict[str, str]],
) -> list[dict[str, Any]]:
    mention_source_counts: dict[str, int] = {}
    for mention in source_mentions:
        source_name = str(mention.get("source_name", ""))
        mention_source_counts[source_name] = mention_source_counts.get(source_name, 0) + 1

    candidate_contributing_sources: dict[str, set[str]] = {}
    for candidate in scouting_candidates:
        identity = str(candidate.get("identity_key", ""))
        evidence = candidate.get("source_evidence", [])
        for ev in evidence:
            source_name = str(ev.get("source_name", ""))
            candidate_contributing_sources.setdefault(identity, set()).add(source_name)

    source_row_count: dict[str, int] = {}
    for row in editor_rows:
        identity = row.get("Candidate ID", "")
        sources = candidate_contributing_sources.get(identity, set())
        for source_name in sources:
            source_row_count[source_name] = source_row_count.get(source_name, 0) + 1

    all_source_names = set(mention_source_counts) | set(source_row_count)
    return [
        {
            "source_name": name,
            "mention_count": mention_source_counts.get(name, 0),
            "editor_facing_contribution": source_row_count.get(name, 0),
        }
        for name in sorted(all_source_names)
    ]


def capture_ledger_from_workflow_log(
    output_dir: Path, operation_id: str = ""
) -> tuple[list[dict[str, Any]] | None, str]:
    """Return the operation's capture ledger and how confident we are in it.

    The replay pipeline necessarily writes a smaller ``source-runs.json``: it
    only contains sources that reached replay. The workflow capture entry is
    the complete source partition, including sources that were inactive,
    skipped, ignored, or failed before replay.

    The status distinguishes three states, because a bare ``None`` cannot tell
    a degraded run from one that never captured:

    - ``present``: the ledger was read and is authoritative.
    - ``absent``: this run has no capture stage at all, so the pipeline ledger
      is already the complete picture and nothing is missing.
    - ``incomplete``: a capture stage ran but its ledger could not be read, so
      the counts fall back to the pipeline ledger and under-report.

    Only ``incomplete`` is a problem, and only it earns a warning.
    """
    log_path = output_dir / "workflow-log.json"
    if not log_path.exists():
        return None, "absent"
    try:
        raw_log = read_json(log_path, default=None)
    except (OSError, TypeError, ValueError):
        return None, "incomplete"
    if not isinstance(raw_log, list):
        return None, "incomplete"
    entries = [
        entry
        for entry in raw_log
        if isinstance(entry, dict)
        and entry.get("stage") == "capture"
        and (not operation_id or str(entry.get("operation_id", "")) == operation_id)
    ]
    if not entries:
        return None, "absent"
    for entry in reversed(entries):
        detail = entry.get("detail")
        source_runs = detail.get("source_runs") if isinstance(detail, dict) else None
        if isinstance(source_runs, list) and all(isinstance(run, dict) for run in source_runs):
            return list(source_runs), "present"
    return None, "incomplete"


def source_run_identity(run: dict[str, Any]) -> tuple[str, str]:
    """Return the key that matches one source across the capture and replay ledgers."""
    url = str(run.get("url") or run.get("source_url") or "").strip()
    if url:
        return "url", normalize_source_identity(url)
    name = str(run.get("source_name") or run.get("name") or "").strip().casefold()
    return "name", name


def _capture_outcome(status: str, *, allow_pipeline_statuses: bool) -> str | None:
    if status in CAPTURE_SOURCE_OUTCOMES:
        return status
    if allow_pipeline_statuses and status in COLLECTING_STATUSES:
        return None
    return "unknown"


def capture_source_outcome_counts(
    capture_source_runs: list[dict[str, Any]],
    pipeline_source_runs: list[dict[str, Any]] | None = None,
    *,
    allow_pipeline_statuses: bool = False,
) -> dict[str, int]:
    """Count the deduplicated source outcomes for the complete run.

    The capture ledger contains sources that never reach replay. The pipeline
    ledger is authoritative for a source that does reach replay and later
    fails. A collecting status does not replace a capture outcome, so the
    reused path keeps its existing warning and capture outcome. Unknown
    statuses are explicit and never become success.
    """
    outcomes: dict[tuple[str, str], str] = {}
    for run in capture_source_runs:
        outcome = _capture_outcome(
            str(run.get("status", "")).strip(),
            allow_pipeline_statuses=allow_pipeline_statuses,
        )
        if outcome is not None:
            outcomes[source_run_identity(run)] = outcome

    for run in pipeline_source_runs or []:
        status = str(run.get("status", "")).strip()
        if status in COLLECTING_STATUSES:
            continue
        # ``allow_pipeline_statuses=False`` never returns ``None``, and the
        # collecting statuses are already skipped above, so every remaining
        # status maps to a named outcome or to ``unknown``.
        outcomes[source_run_identity(run)] = _capture_outcome(status, allow_pipeline_statuses=False) or "unknown"

    counts = {outcome: 0 for outcome in CAPTURE_SOURCE_OUTCOMES}
    counts["unknown"] = 0
    for outcome in outcomes.values():
        counts[outcome] += 1
    return counts


def source_runs_with_pipeline_counts(
    source_runs: list[dict[str, Any]],
    scouting_candidates: list[dict[str, Any]],
    internal_exclusions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    editor_counts = source_counts_from_candidate_evidence(scouting_candidates)
    exclusion_counts = source_counts_from_exclusion_evidence(internal_exclusions)
    updated_runs = []
    for run in source_runs:
        # A reused source contributed mentions exactly like a freshly collected
        # one, so it needs the same pipeline counts. Only its freshness differs
        # (Issue #384). A not-due source contributes its carried list (#1057).
        if normalized_source_run_status(run.get("status", "")) not in {*COLLECTING_STATUSES, SOURCE_STATUS_NOT_DUE}:
            updated_runs.append(run)
            continue
        source_name = str(run.get("source_name", ""))
        updated = dict(run)
        updated["editor_facing_count"] = editor_counts.get(source_name, 0)
        updated["excluded_candidate_count"] = exclusion_counts.get(source_name, 0)
        updated_runs.append(updated)
    return updated_runs


def source_counts_from_candidate_evidence(candidates: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for candidate in candidates:
        source_names = {
            str(evidence.get("source_name", ""))
            for evidence in candidate.get("source_evidence", [])
            if evidence.get("source_name")
        }
        for source_name in source_names:
            counts[source_name] = counts.get(source_name, 0) + 1
    return counts


def source_counts_from_exclusion_evidence(exclusions: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for exclusion in exclusions:
        source_names = {
            str(evidence.get("source_name", ""))
            for evidence in exclusion.get("evidence", [])
            if evidence.get("source_name")
        }
        for source_name in source_names:
            counts[source_name] = counts.get(source_name, 0) + 1
    return counts


def shortfall_reason(
    editor_facing_count: int,
    editor_row_target: int = 10,
    skipped_source_count: int = 0,
    failed_source_count: int = 0,
    ignored_source_count: int = 0,
    source_contributions: list[dict[str, Any]] | None = None,
) -> str:
    if editor_facing_count >= editor_row_target:
        return ""
    if failed_source_count:
        return (
            f"fewer than {editor_row_target} editor-facing rows produced because "
            f"{failed_source_count} configured source failed"
        )
    if skipped_source_count or ignored_source_count:
        reasons = []
        if skipped_source_count:
            reasons.append(f"{skipped_source_count} configured source skipped")
        if ignored_source_count:
            reasons.append(f"{ignored_source_count} non-discovery source ignored")
        return (
            f"fewer than {editor_row_target} editor-facing rows produced; "
            f"source scarcity or source-boundary limits: {', '.join(reasons)}"
        )
    return (
        f"fewer than {editor_row_target} editor-facing rows produced by configured "
        "source evidence; no pipeline failure was recorded"
    )


def major_warnings(
    skipped_source_count: int,
    failed_source_count: int,
    ignored_source_count: int,
    editor_facing_count: int,
    editor_row_target: int = 10,
    russian_check_skipped_count: int = 0,
    russian_check_failed_count: int = 0,
    source_contributions: list[dict[str, Any]] | None = None,
    row_cap_dropped_count: int = 0,
    pilot_row_max_configured: bool = False,
    reused_source_count: int = 0,
    collecting_source_count: int = 0,
    unknown_source_count: int = 0,
    capture_ledger_status: str = "present",
    source_statuses: Sequence[object] | None = None,
) -> list[str]:
    warnings = []
    # Stated first, and as a share rather than a bare count: reuse is the one
    # warning that changes how every other number in the summary should be
    # read, because those numbers may describe an earlier run (Issue #384).
    # Older callers only have the persisted aggregate count. Current callers
    # pass source-run statuses, which are authoritative at this boundary.
    if source_statuses is not None:
        source_summary = summarize_source_runs(source_statuses)
        effective_reused_source_count = source_summary.stale_source_count
        effective_collecting_source_count = source_summary.collecting_source_count
    else:
        effective_reused_source_count = reused_source_count
        effective_collecting_source_count = collecting_source_count
    if effective_reused_source_count:
        if effective_collecting_source_count:
            warnings.append(
                f"{effective_reused_source_count} of {effective_collecting_source_count} sources served from an earlier run's "
                "artifact rather than fresh extraction; candidate and description counts are not all from this run"
            )
        else:
            warnings.append(f"{effective_reused_source_count} sources served from an earlier run's artifact")
    # Only a configured ceiling is worth a warning. Truncation against the built-in
    # seed-calibration band is the intended sampling, not a loss (Issue #379).
    if row_cap_dropped_count and pilot_row_max_configured:
        warnings.append(
            f"{row_cap_dropped_count} eligible editor-facing rows discarded by the pilot_row_max cap; "
            "raise pilot_row_max to recover them"
        )
    if skipped_source_count:
        warnings.append(f"{skipped_source_count} configured source skipped")
    if failed_source_count:
        warnings.append(f"{failed_source_count} configured source failed")
    if ignored_source_count:
        warnings.append(f"{ignored_source_count} configured discovery source ignored")
    if unknown_source_count:
        warnings.append(f"{unknown_source_count} source outcome(s) unknown; capture reporting is incomplete")
    if capture_ledger_status == "incomplete":
        # A capture stage ran but its ledger could not be read, so the counts
        # silently fall back to the pre-#473 pipeline-only view and
        # under-report failures and skips.  A degraded run must say so rather
        # than look like a clean one.  A run with no capture stage is not
        # degraded and stays quiet.
        warnings.append("capture ledger unreadable; source outcome counts are incomplete")
    if russian_check_skipped_count:
        warnings.append(f"{russian_check_skipped_count} Russian retailer check skipped")
    if russian_check_failed_count:
        warnings.append(f"{russian_check_failed_count} Russian retailer check failed")
    if editor_facing_count < editor_row_target:
        warnings.append(f"{editor_facing_count} editor-facing rows produced; fewer than target {editor_row_target}")
    return warnings


def is_screened_negative(candidate: dict[str, Any]) -> bool:
    """Return whether the screen itself concluded `no visible risk` for a candidate.

    A screened negative asks nothing of a person: its coverage record says
    what was checked, so it is not an unreviewed status (issue #714). A
    `text check required` or `clear visible risk` status still is.
    """
    return (
        str(candidate.get("visible_risk_status", "")).strip() == "no visible risk"
        and recorded_coverage(candidate.get(VISIBLE_RISK_SCREEN_COVERAGE_FIELD)) == SCREENED
    )


def review_metadata_warnings(candidates: list[dict[str, Any]]) -> list[str]:
    warnings = []
    unclear_russian_count = sum(
        1 for candidate in candidates if is_unclear(candidate.get("russian_edition_status", ""))
    )
    if unclear_russian_count:
        warnings.append(f"{unclear_russian_count} editor-facing rows have unclear Russian-edition status")

    unreviewed_risk_count = sum(
        1
        for candidate in candidates
        if str(candidate.get("visible_risk_status", "")).strip()
        and str(candidate.get("visible_risk_status_source", "")).strip().lower() not in {"manual", "checked"}
        and not is_screened_negative(candidate)
    )
    if unreviewed_risk_count:
        warnings.append(f"{unreviewed_risk_count} editor-facing rows have unreviewed Visible Risk status")

    # An inert screen must fail loudly: counting only rows that carry a status
    # stays silent when the screen concluded nothing at all (issue #714).
    coverage_counts = visible_risk_screen_coverage_counts(candidates)
    if candidates and not coverage_counts[SCREENED]:
        warnings.append(f"Visible Risk screen concluded for none of {len(candidates)} editor-facing rows")
    if coverage_counts[FAILED]:
        warnings.append(f"Visible Risk screen failed for {coverage_counts[FAILED]} editor-facing rows")
    return warnings


def pilot_status_artifact(
    candidates: list[dict[str, Any]],
    editor_rows: list[dict[str, str]],
    run_summary: dict[str, Any],
    readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Issue #280: carry the effective run_date from run_summary into pilot-status.
    run_date = str(run_summary.get("run_date", ""))
    readiness = readiness or {
        "editor_row_target": 10,
        "pilot_row_min": 8,
        "pilot_row_max": 12,
        "readiness_purpose": "seed-calibration",
    }
    readiness_purpose = str(readiness.get("readiness_purpose", "seed-calibration"))
    is_recurring = readiness_purpose == "recurring-weekly"

    caveats = list(run_summary.get("major_warnings", []))
    locator_missing_count = sum(1 for candidate in candidates if not has_evidence_locator(candidate))
    if locator_missing_count:
        caveats.append(f"{locator_missing_count} editor-facing rows do not have title-level evidence locators")

    row_count = len(editor_rows)
    pilot_min = readiness["pilot_row_min"]
    pilot_max = readiness["pilot_row_max"]
    if not pilot_min <= row_count <= pilot_max:
        if is_recurring:
            caveats.append(
                f"{row_count} editor-facing rows produced; below the {pilot_min}-{pilot_max} pilot reference"
            )
        else:
            caveats.append(f"{row_count} editor-facing rows produced; outside the {pilot_min}-{pilot_max} pilot target")

    if is_recurring:
        ready_for_editor = all(
            w not in c or not c.startswith(w)
            for c in caveats
            for w in [
                "editor-facing rows produced; outside the",
                "editor-facing rows produced; below the",
            ]
        )
    else:
        ready_for_editor = not caveats
    # Description caveats explain missing descriptions to the editor; they
    # are not a reason to withhold the review, so they never gate readiness
    # (issue #865).
    caveats.extend(run_summary.get("description_caveats", []))
    return {
        "run_date": run_date,
        "ready_for_editor": ready_for_editor,
        "editor_facing_count": row_count,
        "csv_path": "editor-facing.csv",
        "editor_review_guide_path": "editor-review-guide.md",
        "shareable_artifacts": [
            {
                "path": _report_artifact_name("editor-facing.csv"),
                "purpose": "editor-facing Weekly Report Rows for pilot review",
            },
            {
                "path": _report_artifact_name("editor-review-guide.md"),
                "purpose": "plain-language review prompts and pilot caveats for the editor",
            },
        ],
        "internal_artifacts": [
            {
                "path": _report_artifact_name("pilot-status.json"),
                "purpose": "pilot readiness, share guidance, and caveats for Dima",
            },
            {
                "path": _report_artifact_name("run-summary.json"),
                "purpose": "source counts, warning counts, shortfall reason, and operational health",
            },
            {
                "path": _report_artifact_name("source-runs.json"),
                "purpose": "per-source collection status, counts, skipped or failed reasons, and safe debug metadata",
            },
            {
                "path": _report_artifact_name("source-proposals.json"),
                "purpose": "pending Source Proposals with provenance and supporting evidence for Dima review",
            },
            {
                "path": _report_artifact_name("source-metrics.json"),
                "purpose": "per-source lifecycle metrics for source review conversations",
            },
            {
                "path": _report_artifact_name("source-review.md"),
                "purpose": "review-ready source lifecycle table with probation/retirement context",
            },
            {
                "path": _report_artifact_name("derived-rule-suggestions.json"),
                "purpose": "inert rejection-pattern suggestions; does not change taste profile rules",
            },
            {
                "path": _report_artifact_name("derived-rule-suggestions.md"),
                "purpose": "plain-language rejection-pattern suggestions for editor review",
            },
            {
                "path": _report_artifact_name("source-mentions.json"),
                "purpose": "metadata-only configured-source discovery output",
            },
            {
                "path": _report_artifact_name("scouting-candidates.json"),
                "purpose": "work-level Scouting Candidates after merging, screening, and ranking",
            },
            {
                "path": _report_artifact_name("internal-exclusions.json"),
                "purpose": "Found in Russian and other hard exclusions kept outside the editor CSV",
            },
            {
                "path": _report_artifact_name("russian-edition-checks.json"),
                "purpose": "auditable Russian-edition check runs and candidate-level decisions",
            },
            {
                "path": _report_artifact_name("russian-review-queue.csv"),
                "purpose": "manual review queue for unclear Russian-edition statuses",
            },
            {
                "path": _report_artifact_name("report-qa.json"),
                "purpose": "Dima report-level QA, separate from candidate editor status",
            },
            {
                "path": _report_artifact_name("candidate-history.json"),
                "purpose": "persisted candidate history and editor status markers",
            },
            {
                "path": _report_artifact_name("proposed-corrections.json"),
                "purpose": "returned edits to generated facts awaiting explicit acceptance",
            },
            {
                "path": _report_artifact_name("memo-queue.json"),
                "purpose": "future memo/research markers from needs-deeper-analysis feedback",
            },
            {
                "path": _report_artifact_name("suppressed-candidates.json"),
                "purpose": "rejected or already-known candidates hidden without Material Change",
            },
            {
                "path": _report_artifact_name("hidden-by-rule.json"),
                "purpose": "candidates hidden from the editor-facing review by active Editorial Taste Profile hard rules",
            },
        ],
        "readiness_basis": {
            "readiness_purpose": readiness_purpose,
            "editor_facing_count": row_count,
            "editor_row_target": readiness["editor_row_target"],
            "pilot_row_min": pilot_min,
            "pilot_row_max": pilot_max,
            "row_cap_dropped_count": int(run_summary.get("row_cap_dropped_count", 0) or 0),
            "codex_workflow_health": str(run_summary.get("codex_workflow_health", "")),
            "source_volume_status": str(run_summary.get("source_volume_status", "")),
            "shortfall_reason": str(run_summary.get("shortfall_reason", "")),
            "major_warning_count": len(run_summary.get("major_warnings", [])),
            "found_in_russian_exclusions_stay_internal": True,
            "clear_visible_risk_rows_stay_visible": any(
                row.get("Visible-risk status") == "clear visible risk" for row in editor_rows
            ),
            "source_contributions": run_summary.get("source_contributions", []),
        },
        "feedback_intake_contract": {
            "supported_editor_statuses": [
                "interested",
                "maybe_watch",
                "reject",
                "already_known",
                "needs_deeper_analysis",
            ],
            "authoritative_feedback_fields": ["Editor Status", "Editor Note"],
            "generated_fact_edits": "stored in proposed-corrections.json until explicitly accepted",
            "report_qa_artifact": "report-qa.json",
            "feedback_artifacts": [
                "candidate-history.json",
                "proposed-corrections.json",
                "memo-queue.json",
            ],
            "next_step_note": (
                "Use pilot feedback to open follow-up implementation issues; do not change "
                "source discovery, Russian-edition, Visible Risk, or CSV semantics silently."
            ),
        },
        "caveats": caveats,
        "share_note": (
            "Ready to share with the editor friend" if ready_for_editor else "Not ready to share with the editor friend"
        ),
    }


def write_pilot_editor_review_guide(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "# Recorded Media Trial Editor Review Guide",
                "",
                "This is a recorded scouting trial from open-source metadata. It is not a production weekly report, not legal clearance, not rights clearance, and not an acquisition recommendation.",
                "",
                "Visible Risk is public-data scouting-stage screening, not legal clearance. Russian-edition status is based on configured checks and recorded fixtures; rights availability is not determined.",
                "",
                "Please review the editor-facing CSV with these questions:",
                "",
                "- Are these plausible Scouting Candidates?",
                "- Are Evidence Notes concise and useful?",
                "- Are Source, Source URL, Signal Summary, and Links enough to verify provenance?",
                "- Do Priority Labels match editorial intuition?",
                "- Are Russian-edition statuses understandable?",
                "- Are Visible Risk statuses and comments understandable?",
                "- Are English helper fields useful where present?",
                "- Does one Weekly Report Row per Scouting Candidate work for review?",
                "",
                "Use editor feedback fields for candidate workflow decisions. Edits to generated facts should be treated as proposed corrections until explicitly accepted.",
                "Do not edit the Candidate ID column; AutoScout uses it to match returned feedback to candidates.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def has_evidence_locator(candidate: dict[str, Any]) -> bool:
    for evidence in candidate.get("source_evidence", []):
        if str(evidence.get("evidence_url", "")).strip():
            return True
        if str(evidence.get("evidence_locator", "")).strip():
            return True
    return False
