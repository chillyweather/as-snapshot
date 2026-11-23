"""Weekly workflow orchestration seam.

Coordinates capture, Codex-artifact handoff, finalization, MVP Pipeline,
health validation, and cumulative review rendering through narrow adapters
so the complete behavior can be tested deterministically.
"""

from __future__ import annotations

import inspect
import os
import queue
import threading
import time
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import date
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any
from typing import Protocol

from autoscout.annual_source_audit import ANNUAL_AUDIT_STAGE_NAME
from autoscout.annual_source_audit import annual_audit_stage_result
from autoscout.artifact_freshness import is_fresh
from autoscout.artifact_freshness import normalized_artifact_freshness
from autoscout.bounded_workers import DaemonFuture
from autoscout.bounded_workers import DaemonThreadPool
from autoscout.codex_workflow import applied_capture_runtime_budget
from autoscout.codex_workflow import finalize_codex_run
from autoscout.codex_workflow import prepare_codex_extraction
from autoscout.content_description import description_provenance_counts as count_description_provenance
from autoscout.content_description import has_adjudicated_descriptions
from autoscout.description_stage_limits import ADJUDICATION_CANDIDATE_LIMIT as ADJUDICATION_CANDIDATE_LIMIT
from autoscout.fit_scores import fit_ordering_enabled
from autoscout.grounded_description import OUTCOME_ORDER
from autoscout.grounded_description import STAGE_BUDGET_SECONDS
from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.late_failure_leftovers import LATE_FAILURE_LEFTOVERS_STAGE
from autoscout.late_failure_leftovers import leftover_scope
from autoscout.model_client import ModelUsageLedger
from autoscout.model_client import recording_model_usage
from autoscout.mvp_run_evidence import MvpRunEvidence
from autoscout.mvp_run_evidence import bind_worklist_to_operation
from autoscout.mvp_run_evidence import inspect as inspect_run_evidence
from autoscout.mvp_run_evidence import operation_id as evidence_operation_id
from autoscout.mvp_run_evidence import safe_artifact_path
from autoscout.mvp_run_evidence import stamp_entry
from autoscout.mvp_run_evidence import validated_artifact_allowlist
from autoscout.pipeline import run_pipeline
from autoscout.pipeline_path import PipelinePath
from autoscout.progress_snapshot import ProgressSnapshotWriter
from autoscout.review_rendering import render_review
from autoscout.run_health import run_health
from autoscout.runtime_budget import FINALIZATION_RESERVE_SECONDS
from autoscout.runtime_budget import KIRKUS_PASS_RUNTIME_BUDGET_SECONDS
from autoscout.runtime_budget import PM_PASS_RUNTIME_BUDGET_SECONDS
from autoscout.runtime_budget import PW_PASS_RUNTIME_BUDGET_SECONDS
from autoscout.runtime_budget import WEEKLY_OPERATION_CEILING_SECONDS
from autoscout.russian_workflow import snapshot_progress_line
from autoscout.second_attempt import PriorRun
from autoscout.second_attempt import confirm_published
from autoscout.second_attempt import log_unreadable
from autoscout.second_attempt import prior_run
from autoscout.secret_redaction import contains_secret
from autoscout.secret_redaction import redact_structure
from autoscout.source_page_identity import incomplete_coverage_from_run
from autoscout.source_row_history import recorded_departures
from autoscout.state_store import Collection
from autoscout.state_store import StateStore
from autoscout.state_store import StateStoreError
from autoscout.state_store import export_store
from autoscout.state_store import inspect_store
from autoscout.state_store import keeps_json_in_state_dir
from autoscout.state_store import open_run_output_store
from autoscout.state_store import open_state_store
from autoscout.state_store import resolve_state_backend
from autoscout.state_store import select_state_backend_from_file
from autoscout.state_store.layout import shard_files
from autoscout.state_store.marker import BACKEND_MARKER_FILE
from autoscout.toolchain_preflight import STAGE_ZERO_NAME
from autoscout.toolchain_preflight import build_stage_zero_result
from autoscout.toolchain_preflight import probe_codex_toolchain
from autoscout.toolchain_preflight import probe_discovery_adapter
from autoscout.unexpected_error import UNEXPECTED_ERROR_STAGE
from autoscout.unexpected_error import exception_facts
from autoscout.unexpected_error import keep_exception
from autoscout.weekly_progress import KIRKUS_WAIT_STAGE
from autoscout.weekly_progress import WeeklyProgressPresentation
from autoscout.weekly_report import WEEKLY_REPORT_ARTIFACTS
from autoscout.weekly_report import WeeklyReportSnapshot
from autoscout.weekly_report import WeeklyReportStore
from autoscout.weekly_report import read_run_history


class WorkflowOutcome(str, Enum):
    COMPLETED_NO_ATTENTION = "completed/no-attention"
    COMPLETED_ATTENTION_REQUIRED = "completed/attention-required"
    INTERVENTION_REQUIRED = "intervention-required"
    # A scheduled start that met a live run (#1057). It wrote nothing: the
    # run in progress owns the log and the artifacts.
    SKIPPED_RUN_IN_PROGRESS = "skipped/run-in-progress"
    # A scheduled run whose own update pulled new commits (#1117). It wrote
    # nothing and released the lock; the command starts it again on the new
    # code, because the process keeps running the modules it already loaded.
    RESTART_ON_UPDATED_CODE = "restart/updated-code"
    # A second attempt of the night (#1160) whose date already has a completed
    # run. It wrote nothing and sends no report.
    SKIPPED_ALREADY_COMPLETED = "skipped/already-completed"
    # New code failed the gate and the last code that passed here can run on the
    # current configs and state (#1159). It wrote nothing and released the lock;
    # the command starts again on a snapshot of that code.
    RUN_ON_LAST_PASSED_CODE = "restart/last-passed-code"


# Set in the environment of a run started again on updated code, so it restarts at most once.
RESTARTED_ENV = "AUTOSCOUT_RESTARTED_ON_UPDATED_CODE"
# Also set when the first start's gate updated the .venv (#1130), so the
# restarted run reports that update; a pass recorded on an earlier night that
# carries an update is not this night's.
ENVIRONMENT_UPDATED_ENV = "AUTOSCOUT_RESTARTED_AFTER_ENVIRONMENT_UPDATE"
# The key of a restart result's run summary that asks the command to set it.
RESTART_AFTER_ENVIRONMENT_UPDATE = "environment_updated"
# The key of a RUN_ON_LAST_PASSED_CODE result's run summary that holds the plan to start (#1159).
LAST_PASSED_CODE_PLAN = "last_passed_code_plan"

# The stage of the gate on new code (#1124); its failed entry names the commit.
CODE_GATE_STAGE = "code-gate"

# The preflight reasons that mean another run holds the run lock. A scheduled
# run skips on them only when the lock's owner is verifiably alive.
RUN_IN_PROGRESS_GUARD_REASONS = frozenset({"guard_held", "stale_guard_with_live_process"})


_DESCRIPTION_INPUT_REASON_CODES = frozenset(
    {
        "invalid_scouting_candidates",
        "invalid_candidate_identity_aliases",
    }
)


def _description_input_failure_reason(result: dict[str, Any]) -> str:
    reason = result.get("reason")
    if isinstance(reason, str) and reason in _DESCRIPTION_INPUT_REASON_CODES:
        return reason
    return "description_input_failure"


def weekly_run_attention_reasons(
    *,
    readiness_purpose: str,
    recurring_attention_required: bool | None,
    ready_for_editor: bool | None,
    source_problem_count: int,
    description_stage_problem_count: int,
) -> tuple[str, ...]:
    """Name which this-run facts require editor attention (issue #557, option 1).

    Returns any of ``"readiness"``, ``"sources"``, ``"descriptions"`` so the
    editor-facing narrative can report the actual unresolved fact instead of
    a generic symptom. The cumulative description backlog is a standing
    corpus property; it never enters this decision and stays reported in
    ``description_coverage`` and the review.
    """
    reasons: list[str] = []
    if readiness_purpose == "recurring-weekly":
        if bool(recurring_attention_required):
            reasons.append("readiness")
    elif ready_for_editor is not True:
        reasons.append("readiness")
    if source_problem_count > 0:
        reasons.append("sources")
    if description_stage_problem_count > 0:
        reasons.append("descriptions")
    return tuple(reasons)


def weekly_run_attention_rule(
    *,
    readiness_purpose: str,
    recurring_attention_required: bool | None,
    ready_for_editor: bool | None,
    source_problem_count: int,
    description_stage_problem_count: int,
) -> bool:
    """Decide whether THIS RUN needs editor attention (issue #557, option 1).

    Attention means this run introduced or failed to resolve something: a
    readiness verdict against it, a failed or skipped source, or a
    description-stage candidate that could not be grounded.
    """
    return bool(
        weekly_run_attention_reasons(
            readiness_purpose=readiness_purpose,
            recurring_attention_required=recurring_attention_required,
            ready_for_editor=ready_for_editor,
            source_problem_count=source_problem_count,
            description_stage_problem_count=description_stage_problem_count,
        )
    )


@dataclass
class WeeklyWorkflowResult:
    outcome: WorkflowOutcome
    readiness_facts: dict[str, Any] = field(default_factory=dict)
    delta_counts: dict[str, int] = field(default_factory=dict)
    source_problems: list[dict[str, str]] = field(default_factory=list)
    failed_stage: str | None = None
    review_available: bool = False
    review_path: str = ""
    log: list[dict[str, Any]] = field(default_factory=list)
    run_summary: dict[str, Any] = field(default_factory=dict)
    description_coverage: dict[str, Any] = field(default_factory=dict)
    # How much of the portfolio was served from an earlier run's artifact, and
    # out of how many contributing sources. Carried on the result so the editor
    # narrative can qualify the counts it reports (Issue #384).
    reused_source_count: int = 0
    collecting_source_count: int = 0
    # Source-run statuses are carried separately from the aggregate counts so
    # presentation can apply the stale-evidence predicate to actual values.
    source_statuses: tuple[str, ...] | None = None
    # Which path the pipeline took, including a scouting pass with feedback.
    # The editor narrative must not report an earlier run's counts as this
    # run's finding (Issue #388).
    pipeline_path: PipelinePath | None = None
    unfinished_items: int = 0
    capacity_stop_count: int = 0
    grounded_description_counts: dict[str, int] = field(default_factory=dict)
    grounded_description_reason_counts: dict[str, int] = field(default_factory=dict)
    # Usable descriptions split by verified-description provenance, counted
    # from durable Candidate History. Empty until a week leaves an
    # adjudicated description behind, so older summaries read unchanged
    # (issue #644).
    description_provenance_counts: dict[str, int] = field(default_factory=dict)
    description_stage_report: dict[str, Any] = field(default_factory=dict)
    description_adjudication_stage_report: dict[str, Any] = field(default_factory=dict)
    description_stage_problems: list[dict[str, str]] = field(default_factory=list)
    # Which this-run facts forced attention: "readiness", "sources",
    # "descriptions" (issue #557). Empty when the run is clean.
    attention_reasons: tuple[str, ...] = ()
    # The sources a Kirkus, PM or PW pass reported a problem for; they ask for
    # attention whatever their failure streak (#1125).
    pass_problem_sources: tuple[str, ...] = ()


class CaptureAdapter(Protocol):
    def prepare(
        self,
        config_path: Path,
        output_dir: Path,
        *,
        live: bool = False,
        effective_date: str | None = None,
        kirkus_mode: str = "all",
        runtime_budget_seconds: float | None = None,
    ) -> dict[str, Any]: ...


class CodexAdapter(Protocol):
    def fulfill(
        self,
        output_dir: Path,
        *,
        extraction_budget_seconds: float | None = None,
        source_urls: set[str] | None = None,
    ) -> dict[str, Any]: ...

    def fulfill_additional(
        self, output_dir: Path, *, source_urls: set[str], extraction_budget_seconds: float
    ) -> dict[str, Any]: ...


class RepositoryAdapter(Protocol):
    def preflight(self, validated_artifact_allowlist: set[Path] | None = None) -> dict[str, Any]: ...

    def update(self, validated_artifact_allowlist: set[Path] | None = None) -> dict[str, Any]: ...

    def finalize(self, output_dir: Path) -> dict[str, Any]: ...

    def release_guard(self) -> None: ...


class ClockAdapter(Protocol):
    def today(self) -> date: ...


class PresentationAdapter(Protocol):
    """Presentation variability: progress and opening the local review.

    Issue #452: ``announce_progress_snapshot`` receives one redacted
    progress-snapshot payload per operator heartbeat. Runtime binding stays
    defensive (``getattr``), so legacy adapters that implement only
    ``announce`` and ``open_review`` keep working unchanged and stay silent.
    """

    def announce(self, stage: str) -> None: ...

    def announce_progress_snapshot(self, snapshot: dict[str, Any]) -> None: ...

    def open_review(self, review_path: Path) -> dict[str, Any]: ...


class DescriptionEnrichmentAdapter(Protocol):
    def enrich(
        self,
        output_dir: Path,
        state_dir: Path | None = None,
        effective_date: str | None = None,
        *,
        operation_id: str | None = None,
        state_store: StateStore | None = None,
    ) -> dict[str, Any]: ...


class DescriptionAdjudicationAdapter(Protocol):
    """Decide whether a public web page really describes one book.

    The adapter takes one candidate work item and returns one adjudication
    response matching ``ADJUDICATION_RESPONSE_SCHEMA``. The deterministic
    replay adapter serves tests and the offline fixture run; the production
    Codex CLI adapter (issue #638) must be wired explicitly and is never
    reachable from the fixture run.
    """

    def adjudicate(self, work_item: Mapping[str, Any]) -> Mapping[str, Any]: ...


class DescriptionAdjudicationStageAdapter(Protocol):
    def adjudicate_stage(
        self,
        output_dir: Path,
        state_dir: Path | None = None,
        effective_date: str | None = None,
        *,
        operation_id: str | None = None,
        state_store: StateStore | None = None,
    ) -> dict[str, Any]: ...


class EditorialFitShadowAdapter(Protocol):
    """Advisory shadow scoring of the final candidates (issue #725)."""

    def score(
        self,
        output_dir: Path,
        state_dir: Path,
        run_date: str,
        *,
        state_store: StateStore | None = None,
        review_ordered_by_fit: bool = False,
    ) -> dict[str, Any]:
        """Score with *state_store*, the run's store, or without one a store over *state_dir*.

        *review_ordered_by_fit* says this run's review lists books by the fit score (#1105).
        """
        ...


class KirkusPrecollectionAdapter(Protocol):
    """The Kirkus pass of the day (issue #816); it reports counts and never raises a refusal of the site."""

    def collect(
        self,
        config_path: Path,
        output_dir: Path,
        reference: date,
        *,
        presentation: Callable[[dict[str, Any]], None] | None = None,
        failure_recorder: Callable[[str, str], None] | None = None,
        budget_seconds: float = KIRKUS_PASS_RUNTIME_BUDGET_SECONDS,
        write_snapshot: bool = True,
        stop_requested: threading.Event | None = None,
    ) -> dict[str, Any]: ...


class PwPrecollectionAdapter(Protocol):
    """The PW issue pass of the run (issue #956); it reports counts and never raises."""

    def collect(
        self,
        config_path: Path,
        output_dir: Path,
        *,
        presentation: Callable[[dict[str, Any]], None] | None = None,
        failure_recorder: Callable[[str, str], None] | None = None,
        budget_seconds: float = PW_PASS_RUNTIME_BUDGET_SECONDS,
        run_date: date | None = None,
    ) -> dict[str, Any]: ...


class PmPrecollectionAdapter(Protocol):
    """The PM deals pass of the run (issue #946); it reports counts and never raises."""

    def collect(
        self,
        config_path: Path,
        output_dir: Path,
        *,
        presentation: Callable[[dict[str, Any]], None] | None = None,
        failure_recorder: Callable[[str, str], None] | None = None,
        budget_seconds: float = PM_PASS_RUNTIME_BUDGET_SECONDS,
        run_date: date | None = None,
    ) -> dict[str, Any]: ...


@dataclass
class WeeklyWorkflowAdapters:
    """The seven external variability points of the weekly workflow."""

    capture: CaptureAdapter
    codex: CodexAdapter
    repository: RepositoryAdapter | None = None
    presentation: PresentationAdapter | None = None
    enrichment: DescriptionEnrichmentAdapter | None = None
    adjudication: DescriptionAdjudicationStageAdapter | None = None
    clock: ClockAdapter | None = None
    # Advisory and optional. ``None`` means the shadow stage never runs. The
    # weekly command wires it explicitly, and it also needs
    # ``editorial_fit_shadow: true`` in the weekly config, so neither the
    # fixture run nor a production-shaped test can reach the vendor.
    editorial_fit: EditorialFitShadowAdapter | None = None
    # Live and slow. ``None`` means the weekly run never starts the Kirkus
    # pass; the weekly command binds it, so no test reaches the site.
    kirkus_precollection: KirkusPrecollectionAdapter | None = None
    # Live mailbox reads. ``None`` means the weekly run never starts the PM
    # pass; the weekly command binds it, so no test reaches the mail server.
    pm_precollection: PmPrecollectionAdapter | None = None
    # Live mailbox reads and subscriber downloads. ``None`` means the weekly run
    # never starts the PW pass; the weekly command binds it, so no test reaches PW.
    pw_precollection: PwPrecollectionAdapter | None = None
    # Live page and catalogue reads (issue #979). ``None`` means the weekly
    # run never completes ISBNs; the weekly command binds it, so no test
    # reaches the network.
    isbn_completion: Any | None = None
    # Live page reads, Codex, and Open Library lookups (issue #968). ``None``
    # means the weekly run never records original editions; the weekly command
    # binds it, so no test reaches the network or the model.
    original_edition_completion: Any | None = None
    # Live Open Library lookups (issue #1138). ``None`` means the weekly run
    # never records the work age; the weekly command binds it, so no test
    # reaches the network.
    work_age_completion: Any | None = None
    # Live LiveLib, Wikidata, State Library and LitRes reads and Codex
    # (issue #1181). ``None`` means the weekly run never looks for Russian
    # editions; the weekly command binds it, and it also needs
    # ``russian_edition_completion: true`` in the weekly config, so no test
    # reaches the network or the model.
    russian_edition_completion: Any | None = None
    # A model call per PM-only book with a usable deal logline (issue #950).
    # ``None`` means the weekly run never writes logline descriptions; the
    # weekly command binds it, and it also needs ``pm_logline_descriptions:
    # true`` in the weekly config, so no test reaches the model.
    pm_logline: Any | None = None
    # Stage 0 probes must be wired explicitly.  ``None`` means "not checked"
    # and never falls back to a real subprocess: a default that shelled out to
    # the Codex CLI made three production-shaped tests spend vendor quota and
    # depend on the developer's installed CLI version.  ``weekly_adapters``
    # wires the real probes for production; tests wire fakes (issue #541).
    stage_zero_codex_probe: Callable[[], dict[str, Any]] | None = None
    stage_zero_discovery_probe: Callable[[], dict[str, Any]] | None = None
    # The run server's gate on new code (issue #1124). ``None`` means no gate;
    # the weekly command binds it with ``bind_code_gate``, so no test runs the
    # real check.
    code_gate: Any | None = None


def _editorial_fit_shadow_enabled(config_path: Path) -> bool:
    """The shadow stage is an explicit opt-in of the weekly config."""
    try:
        config = read_json(config_path, default={})
    except (OSError, ValueError):
        return False
    return isinstance(config, dict) and config.get("editorial_fit_shadow") is True


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _operation_id(config_path: Path, operation_date: date | None = None) -> str:
    """Compatibility delegate for the operation-evidence module."""
    return evidence_operation_id(config_path, operation_date)


def _safe_artifact_path(output_dir: Path, value: Any) -> Path | None:
    """Compatibility delegate for safe report-artifact resolution."""
    return safe_artifact_path(output_dir, value)


def _capture_is_current(output_dir: Path, config_path: Path, operation_date: date | None = None) -> bool:
    """Compatibility delegate for the operation-evidence module."""
    return inspect_run_evidence(output_dir, config_path, operation_date).capture_current


def _bind_worklist_to_operation(output_dir: Path, operation_id: str) -> None:
    """Compatibility delegate for operation-stamped capture binding."""
    bind_worklist_to_operation(output_dir, operation_id)


# How many changed paths a refused fallback names in the run log.
_FALLBACK_PATHS_LOGGED = 10


def _leaves_start_snapshot(gate_verdict: Mapping[str, Any] | None) -> bool:
    """Whether a start from the last passing code (#1169) now restarts on the checked-out code: it passed the gate."""
    from autoscout.code_gate import STATUS_PASSED
    from autoscout.code_gate_fallback import started_from_last_passed

    return bool(gate_verdict and gate_verdict.get("status") == STATUS_PASSED and started_from_last_passed(os.environ))


def _fallback_refusal_detail(fallback: Mapping[str, Any] | None) -> dict[str, Any]:
    """Why a failed gate did not run the last passing code (#1159); nothing when no pass is recorded, as before."""
    from autoscout.code_gate_fallback import REFUSED_NO_PASS

    if fallback is None or fallback.get("reason") in (None, REFUSED_NO_PASS):
        return {}
    detail: dict[str, Any] = {"fallback_refused": str(fallback.get("reason"))}
    paths = fallback.get("paths")
    if isinstance(paths, list) and paths:
        detail["fallback_refused_paths"] = [str(path) for path in paths[:_FALLBACK_PATHS_LOGGED]]
    return detail


def _bad_outcome(
    outcome: WorkflowOutcome,
    failed_stage: str,
    log: list[dict[str, Any]],
    reason: str = "",
    log_path: Path | None = None,
    operation_id: str = "",
    pipeline_path: PipelinePath | None = None,
    run_summary: dict[str, Any] | None = None,
    unfinished_items: int = 0,
    capacity_stop_count: int = 0,
    detail: Mapping[str, Any] | None = None,
) -> WeeklyWorkflowResult:
    # Prior diagnostic preservation is now handled proactively by the caller
    # (loading persisted entries into *log* before preflight).  Here we only
    # strip old terminal/outcome-candidate, append the new failure, and write
    # exactly one final terminal.
    if not operation_id:
        operation_id = next(
            (str(entry.get("operation_id", "")) for entry in reversed(log) if entry.get("operation_id")),
            "",
        )
    log[:] = [entry for entry in log if entry.get("stage") not in {"terminal", "outcome-candidate"}]
    if reason:
        entry: dict[str, Any] = {"stage": failed_stage, "status": "failed", "reason": reason, "at": _now_iso()}
        if detail:
            entry["detail"] = dict(detail)
        log.append(stamp_entry(entry, operation_id) if operation_id else entry)
    terminal = {"stage": "terminal", "status": outcome.value, "at": _now_iso()}
    log.append(stamp_entry(terminal, operation_id) if operation_id else terminal)
    if log_path is not None:
        _persist_final_log(log_path, log)
    return WeeklyWorkflowResult(
        outcome=outcome,
        failed_stage=failed_stage,
        log=list(log),
        run_summary=run_summary or {},
        pipeline_path=pipeline_path,
        unfinished_items=unfinished_items,
        capacity_stop_count=capacity_stop_count,
    )


# ── Deterministic in-process adapters ─────────────────────────────────────


class _DeterministicCaptureAdapter:
    def prepare(
        self,
        config_path: Path,
        output_dir: Path,
        *,
        live: bool = False,
        effective_date: str | None = None,
        kirkus_mode: str = "all",
        runtime_budget_seconds: float | None = None,
    ) -> dict[str, Any]:
        rc = prepare_codex_extraction(
            config_path,
            output_dir,
            live=False,
            effective_date=effective_date,
            kirkus_mode=kirkus_mode,
            runtime_budget_seconds=runtime_budget_seconds,
            apply_collection_schedule=True,
        )
        if rc == 0:
            from autoscout.codex_recovery import CodexResumeAdapter

            # Issue #280: when an explicit effective_date is supplied, use it
            # for operation-id and resume binding so the test clock is
            # authoritative. Default remains the deterministic 2000-01-01 date.
            resolved_date = date.fromisoformat(effective_date) if effective_date else date(2000, 1, 1)
            run_id = _operation_id(config_path, resolved_date)
            _bind_worklist_to_operation(output_dir, run_id)
            CodexResumeAdapter(output_dir).mark_stage_complete(output_dir, "capture", run_id)
        source_runs = WeeklyReportStore(output_dir).read("source-runs.json", default=[])
        duration = (
            sum(float(run.get("duration_seconds", 0.0) or 0.0) for run in source_runs if isinstance(run, dict))
            if isinstance(source_runs, list)
            else 0.0
        )
        return {
            "returncode": rc,
            "output_dir": str(output_dir),
            "duration_seconds": duration,
            # The budget the stage applied, which follows what the portfolio
            # declares rather than the base constant (issue #717).
            "runtime_budget_seconds": applied_capture_runtime_budget(output_dir),
            "source_runs": source_runs,
        }


class _DeterministicCodexAdapter:
    def fulfill_additional(
        self, output_dir: Path, *, source_urls: set[str], extraction_budget_seconds: float
    ) -> dict[str, Any]:
        return self.fulfill(output_dir, extraction_budget_seconds=extraction_budget_seconds, source_urls=source_urls)

    def fulfill(
        self,
        output_dir: Path,
        *,
        extraction_budget_seconds: float | None = None,
        source_urls: set[str] | None = None,
    ) -> dict[str, Any]:
        worklist_path = output_dir / "codex-extraction-worklist.json"
        if not worklist_path.exists():
            return {"status": "failed", "reason": "missing_worklist"}
        worklist = read_json(worklist_path, default={})
        items = worklist.get("work_items", [])
        if source_urls is not None:
            items = [item for item in items if item.get("source_url") in source_urls]
        if not items:
            return {"status": "failed", "reason": "empty_worklist"}
        ready = 0
        for item in items:
            expected = str(item.get("expected_extraction_file", ""))
            if expected and (output_dir / expected).exists():
                ready += 1
        return {"status": "ok", "ready_items": ready, "total_items": len(items)}


def _run_finalize_stage(output_dir: Path, live_config: Path, effective_date: str) -> dict[str, Any]:
    rc = finalize_codex_run(output_dir, live_config, effective_date=effective_date)
    replay_path = output_dir / "codex-replay-config.json"
    return {
        "returncode": rc,
        "replay_config_exists": replay_path.exists(),
        "replay_config": str(replay_path) if replay_path.exists() else "",
        "fresh_evidence": rc == 0 and replay_path.exists() and _replay_config_has_fresh_evidence(replay_path),
    }


def _run_pipeline_stage(
    config_path: Path,
    output_dir: Path,
    state_dir: Path | None,
    effective_date: str,
    *,
    state_store: StateStore | None = None,
) -> dict[str, Any]:
    try:
        result = run_pipeline(
            config_path, output_dir, state_dir, effective_date=effective_date, state_store=state_store
        )
    except SystemExit as exc:
        return {"returncode": exc.code if exc.code is not None else 1, "exception": str(exc)}
    if isinstance(result, dict):
        return {"returncode": 0, **result}
    return {"returncode": 1, "exception": "pipeline returned no result"}


def _replay_config_has_fresh_evidence(replay_config: Path) -> bool:
    config = read_json(replay_config, default={})
    if not isinstance(config, dict):
        return False
    sources = config.get("sources", [])
    if not isinstance(sources, list):
        return False
    for source in sources:
        if not isinstance(source, dict) or source.get("active", True) is False:
            continue
        freshness = normalized_artifact_freshness(source.get("artifact_freshness"))
        if freshness is not None and is_fresh(freshness):
            return True
        if (
            source.get("recorded_mentions_file")
            and source.get("operation_id")
            and source.get("recorded_mentions_sha256")
        ):
            return True
    return False


def pipeline_result_is_publishable(pipeline_path: PipelinePath | None, *, fresh_evidence_produced: bool) -> bool:
    """Decide whether a known pipeline outcome may be published.

    Every pipeline result must identify its path. Missing or invalid values
    fail closed because the workflow cannot prove what produced the artifacts.
    A feedback no-op is publishable only when this run did not produce fresh
    evidence. Other known paths are publishable regardless of evidence.
    """
    if not isinstance(pipeline_path, PipelinePath):
        return False
    return pipeline_path != PipelinePath.FEEDBACK_NOOP or not fresh_evidence_produced


def _run_review_stage(
    output_dir: Path,
    state_dir: Path | None,
    effective_date: str,
    config_path: Path | None = None,
    *,
    state_store: StateStore | None = None,
) -> dict[str, Any]:
    rc = render_review(
        output_dir, state_dir, effective_date=effective_date, config_path=config_path, state_store=state_store
    )
    html_path = WeeklyReportStore(output_dir).path("weekly-review.html")
    return {"returncode": rc, "html_exists": html_path.exists(), "html_path": str(html_path)}


def _run_health_stage(output_dir: Path, effective_date: str) -> dict[str, Any]:
    return {"returncode": run_health(output_dir, effective_date=effective_date)}


def _run_enrichment_stage(
    adapter: DescriptionEnrichmentAdapter,
    output_dir: Path,
    state_dir: Path | None,
    *,
    effective_date: str,
    operation_id: str,
    state_store: StateStore | None = None,
) -> dict[str, Any]:
    """Call the typed enrichment seam with the current operation identity."""
    return adapter.enrich(
        output_dir,
        state_dir,
        effective_date=effective_date,
        operation_id=operation_id,
        state_store=state_store,
    )


def _grounded_attention_survives_adjudication(
    grounded_report: dict[str, Any],
    adjudication_report: dict[str, Any],
) -> bool:
    """True when the grounded stage's attention is still an unresolved fact.

    The grounded stage decides ``attention_required`` before adjudication
    runs, and adjudication exists to repair exactly what the grounded stage
    left at fallback. A run where every grounded failure was then applied
    resolved everything it attempted, so carrying the grounded verdict
    forward would put the run into attention-required for work that
    finished (issue #641).

    An adjudication stage that produced no records repaired nothing, so the
    grounded verdict stands unchanged.
    """
    from autoscout.description_adjudication import unresolved_description_candidates

    records = adjudication_report.get("records") if isinstance(adjudication_report, dict) else None
    deferred = adjudication_report.get("deferred_limit", {}) if isinstance(adjudication_report, dict) else {}
    has_deferred = isinstance(deferred, dict) and bool(deferred.get("candidate_ids"))
    if (not isinstance(records, list) or not records) and not has_deferred:
        return True
    return bool(unresolved_description_candidates(grounded_report, adjudication_report))


def _run_adjudication_stage(
    adapter: DescriptionAdjudicationStageAdapter,
    output_dir: Path,
    state_dir: Path | None,
    *,
    effective_date: str,
    operation_id: str,
    state_store: StateStore | None = None,
) -> dict[str, Any]:
    """Call the typed adjudication stage seam with the current operation identity."""
    return adapter.adjudicate_stage(
        output_dir,
        state_dir,
        effective_date=effective_date,
        operation_id=operation_id,
        state_store=state_store,
    )


class _NoOpRepositoryAdapter:
    def preflight(self, validated_artifact_allowlist: set[Path] | None = None) -> dict[str, Any]:
        return {"status": "ok"}

    def update(self, validated_artifact_allowlist: set[Path] | None = None) -> dict[str, Any]:
        return {"status": "ok", "pulled": False}

    def finalize(self, output_dir: Path) -> dict[str, Any]:
        return {"status": "ok", "committed": False, "pushed": False}

    def release_guard(self) -> None:
        pass


class _FixedClockAdapter:
    def today(self) -> date:
        return date(2000, 1, 1)


class _SystemClockAdapter:
    def today(self) -> date:
        return date.today()


class _NoOpPresentation:
    def __init__(self, progress: Callable[[str], None] | None = None) -> None:
        self._progress = progress

    def announce(self, stage: str) -> None:
        if self._progress is not None:
            self._progress(stage)

    def announce_progress_snapshot(self, snapshot: dict[str, Any]) -> None:
        return None

    def open_review(self, review_path: Path) -> dict[str, Any]:
        return {"status": "ok", "opened": False, "path": str(review_path)}


def _deterministic_stage_zero_codex_probe() -> dict[str, Any]:
    return {
        "status": "pass",
        "reason_code": "codex_healthy",
        "message": "Codex toolchain is healthy (model 'deterministic-test-model', CLI deterministic-test-cli 0.0.0)",
        "model": "deterministic-test-model",
        "cli_version": "deterministic-test-cli 0.0.0",
        "detail": {},
    }


def _deterministic_stage_zero_discovery_probe() -> dict[str, Any]:
    return {
        "status": "pass",
        "reason_code": "discovery_adapter_healthy",
        "message": "discovery adapter is healthy",
        "endpoint": "",
    }


def deterministic_adapters(progress: Callable[[str], None] | None = None) -> WeeklyWorkflowAdapters:
    return WeeklyWorkflowAdapters(
        capture=_DeterministicCaptureAdapter(),
        codex=_DeterministicCodexAdapter(),
        repository=_NoOpRepositoryAdapter(),
        presentation=_NoOpPresentation(progress),
        enrichment=_make_deterministic_enrichment(),
        adjudication=_make_deterministic_adjudication(),
        clock=_FixedClockAdapter(),
        stage_zero_codex_probe=_deterministic_stage_zero_codex_probe,
        stage_zero_discovery_probe=_deterministic_stage_zero_discovery_probe,
    )


# ── Orchestration ─────────────────────────────────────────────────────────

STAGE_ORDER = [
    STAGE_ZERO_NAME,
    "capture",
    "codex",
    "finalize",
    "pipeline",
    "isbn-completion",
    "enrichment",
    "description-adjudication",
    "pm-logline-descriptions",
    "original-edition-completion",
    "work-age-completion",
    "russian-edition-completion",
    "review",
    "health",
    "open-review",
]


@dataclass(repr=False, eq=False)
class _WeeklyRunContext:
    """What the stages of one weekly run share.

    The fields in the first group are known when the run opens. Every later
    field is set by exactly one stage, the one that computes it, and read by
    the stages after it; reading one before its stage ran raises
    ``AttributeError``. The generated ``repr`` and ``==`` would read every
    field, so they are off.
    """

    config_path: Path
    output_dir: Path
    state_dir: Path | None
    adapters: WeeklyWorkflowAdapters
    monotonic: Callable[[], float]
    operation_started_monotonic: float
    log: list[dict[str, Any]]
    live_config: Path
    log_path: Path
    effective_date_obj: date
    effective_date: str
    run_id: str

    # Bound while the run opens.
    # The token counts of the run's Claude API calls (Issue #1149); each log
    # entry carries the calls finished since the entry before it.
    model_usage: ModelUsageLedger | None = field(init=False, default=None)
    kirkus_failures: queue.SimpleQueue[tuple[str, str]] = field(init=False)
    progress: WeeklyProgressPresentation = field(init=False)
    # _start_operation: the run's canonical state, ``None`` without a state directory
    state_store: StateStore | None = field(init=False)
    # _check_retry_evidence
    evidence: MvpRunEvidence = field(init=False)
    is_same_op_retry: bool = field(init=False)
    # _check_toolchain
    stage_zero_for_summary: dict[str, Any] = field(init=False)
    # _start_kirkus_pass
    operational_source_problems: list[dict[str, str]] = field(init=False)
    # The sources a Kirkus, PM or PW pass named a problem for: those always ask for attention (#1125).
    pass_problem_names: set[str] = field(init=False)
    capture_timing: dict[str, Any] = field(init=False)
    kirkus_future: DaemonFuture | None = field(init=False)
    kirkus_source_name: str = field(init=False)
    # _capture
    capture_was_refreshed: bool = field(init=False)
    # _extract_main_batch
    elapsed_before_extraction: float = field(init=False)
    extraction_budget_seconds: float = field(init=False)
    codex_result: dict[str, Any] = field(init=False)
    worklist: Any = field(init=False)
    deferred_kirkus: bool = field(init=False)
    # _finalize
    pipeline_config: Path = field(init=False)
    fresh_evidence_produced: bool = field(init=False)
    # _run_pipeline
    pipeline_path: PipelinePath | None = field(init=False)
    # _enrich_descriptions
    enrichment_result: dict[str, Any] = field(init=False)
    # _adjudicate_descriptions
    adjudication_result: dict[str, Any] = field(init=False)
    # _write_pm_logline_descriptions
    logline_result: dict[str, Any] = field(init=False)
    # _complete_original_editions
    original_result: dict[str, Any] = field(init=False)
    # _render_review
    review_available: bool = field(init=False)
    review_path: str = field(init=False)
    # _record_source_failure_streaks: this run's view of the streaks, ``None`` when none was recorded
    source_failure_streaks: dict[str, Any] | None = field(init=False)
    # _check_health
    report_snapshot: WeeklyReportSnapshot = field(init=False)
    readiness_facts: dict[str, Any] = field(init=False)
    ready_for_editor: bool = field(init=False)
    run_summary: dict[str, Any] = field(init=False)
    description_stage_report: dict[str, Any] = field(init=False)
    adjudication_stage_report: dict[str, Any] = field(init=False)
    description_stage_problems: list[dict[str, str]] = field(init=False)
    delta_counts: dict[str, Any] = field(init=False)
    source_problems: list[dict[str, str]] = field(init=False)
    source_runs: Any = field(init=False)
    # _decide_outcome
    attention_reasons: tuple[str, ...] = field(init=False)
    outcome: WorkflowOutcome = field(init=False)

    def stage_store(self) -> StateStore:
        """The store a stage given ``state_dir or output_dir`` reads and writes.

        The run's canonical state, or for a run without a state directory the
        JSON files of its output directory standing in for it, whatever
        backend the environment selects for state directories (#1075).
        """
        return self.state_store if self.state_store is not None else open_run_output_store(self.output_dir)

    def add_pass_problem(self, problem: dict[str, str]) -> None:
        """Name a problem the Kirkus, PM or PW pass reported; it asks for attention whatever the source's streak."""
        self.operational_source_problems.append(problem)
        self.pass_problem_names.add(problem.get("source_name", ""))

    def announce(self, stage: str) -> None:
        if self.adapters.presentation is not None:
            self.adapters.presentation.announce(stage)

    def log_stage(self, stage: str, status: str, **extra: Any) -> None:
        entry: dict[str, Any] = {"stage": stage, "status": status, "at": _now_iso()}
        entry.update(extra)
        model_calls = self.model_usage.drain() if self.model_usage is not None else []
        if model_calls:
            entry["model_calls"] = model_calls
        entry = stamp_entry(entry, self.run_id)
        self.log.append(entry)
        sanitized = _sanitize_log_entry(entry)
        _persist_log(self.log_path, sanitized)

    def record_progress_snapshot_failure(self, stage: str, reason: str) -> None:
        workflow_stage = {"capture": "capture", "extraction": "codex"}.get(stage, stage)
        self.log_stage(
            workflow_stage,
            "progress_snapshot_failed",
            detail={"reason": reason},
        )

    def remaining_source_seconds(self) -> float:
        return max(
            0.0,
            WEEKLY_OPERATION_CEILING_SECONDS
            - (self.monotonic() - self.operation_started_monotonic)
            - FINALIZATION_RESERVE_SECONDS,
        )


class WeeklyWorkflow:
    def __init__(
        self,
        adapters: WeeklyWorkflowAdapters | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        scheduled: bool = False,
        second_attempt: bool = False,
        on_prior_run: Callable[[PriorRun | None], None] | None = None,
    ) -> None:
        if second_attempt and not scheduled:
            raise ValueError("a second attempt is a scheduled start")
        self._adapters = adapters or deterministic_adapters()
        self._monotonic = monotonic
        # A start by the server's timer (#1057): it skips itself when a run
        # is in progress and has no browser to open the review in.
        self._scheduled = scheduled
        # The night's later timer (#1160): it also skips itself when the date
        # already has a completed run. *on_prior_run* is told how the date's
        # earlier run ended, as read under the lock (``None`` for a log that
        # cannot be read), for the attempt's report.
        self._second_attempt = second_attempt
        self._on_prior_run = on_prior_run

    def _probe_stage_zero_codex(self) -> dict[str, Any]:
        probe = getattr(self._adapters, "stage_zero_codex_probe", None)
        if callable(probe):
            try:
                result = probe()
                if isinstance(result, dict):
                    return result
            except Exception as exc:
                return {
                    "status": "unknown",
                    "reason_code": "codex_check_unknown",
                    "message": f"Could not check Codex toolchain: {exc}. Verify Codex CLI with 'codex --version'.",
                    "model": "",
                    "cli_version": "",
                    "detail": {"error": str(exc)[:500]},
                }
        return {
            "status": "unknown",
            "reason_code": "codex_check_unknown",
            "message": (
                "Codex toolchain was not checked: no Stage 0 probe is wired. "
                "Build adapters with weekly_adapters(), which wires probe_codex_toolchain."
            ),
            "model": "",
            "cli_version": "",
            "detail": {"probe": "not wired"},
        }

    def _probe_stage_zero_discovery(self) -> dict[str, Any]:
        probe = getattr(self._adapters, "stage_zero_discovery_probe", None)
        if callable(probe):
            try:
                result = probe()
                if isinstance(result, dict):
                    return result
            except Exception as exc:
                return {
                    "status": "unknown",
                    "reason_code": "discovery_check_unknown",
                    "message": f"Could not check discovery adapter: {exc}. Verify AUTOSCOUT_GROUNDED_DISCOVERY_ENDPOINT.",
                    "endpoint": "",
                    "detail": {"error": str(exc)[:500]},
                }
        return {
            "status": "unknown",
            "reason_code": "discovery_check_unknown",
            "message": (
                "Discovery adapter was not checked: no Stage 0 probe is wired. "
                "Build adapters with weekly_adapters(), which wires probe_discovery_adapter."
            ),
            "endpoint": "",
            "detail": {"probe": "not wired"},
        }

    def run(
        self,
        config_path: Path,
        output_dir: Path,
        state_dir: Path | None = None,
    ) -> WeeklyWorkflowResult:
        # The guard is acquired as a side effect of a successful repository
        # preflight (Stage 1) inside this method. It must be released on
        # every exit path -- not only the happy path -- or an
        # intervention-required return leaks the lock and blocks every
        # subsequent run. release_guard() itself only unlinks a lock this
        # adapter instance actually acquired, so calling it here is always
        # safe even when preflight never got that far.
        self._kirkus_stop = threading.Event()
        self._kirkus_pool: DaemonThreadPool | None = None
        try:
            with recording_model_usage() as model_usage:
                self._model_usage = model_usage
                return self._run(config_path, output_dir, state_dir)
        except BaseException as exc:
            # Recorded while this run still owns the guard, so no other run's log is written to (#1158).
            self._record_escaping_exception(exc, config_path, output_dir)
            raise
        finally:
            self._kirkus_stop.set()
            if self._kirkus_pool is not None:
                self._kirkus_pool.shutdown(wait=False, cancel_futures=True)
            if self._adapters.repository is not None:
                try:
                    self._adapters.repository.release_guard()
                except Exception:
                    pass

    def _record_escaping_exception(self, exc: BaseException, config_path: Path, output_dir: Path) -> None:
        """Log the kind and location of an exception that is leaving the run, unless it is a normal exit."""
        extra: dict[str, Any] = {}
        if isinstance(exc, SystemExit):
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
            if code == 0:
                return
            extra["exit_code"] = code
        elif not isinstance(exc, Exception):
            return
        try:
            clock = self._adapters.clock
            day = clock.today() if clock is not None else date.today()
            record_unexpected_error(output_dir, config_path, day, exc, "weekly-run", **extra)
            exc.__dict__["autoscout_failure_recorded"] = True
        except BaseException:  # a second SystemExit from re-reading a broken config must not hide the first
            pass

    def _open_run(
        self,
        config_path: Path,
        output_dir: Path,
        state_dir: Path | None,
    ) -> _WeeklyRunContext:
        """Resolve the run's date and identity and bind the progress recorders."""
        operation_started_monotonic = self._monotonic()
        log: list[dict[str, Any]] = []
        output_dir.mkdir(parents=True, exist_ok=True)
        live_config = config_path
        log_path = output_dir / "workflow-log.json"

        # Issue #280: resolve the effective date once at the beginning of the
        # run from the injected clock, before any potentially lengthy repo
        # preflight. The effective date is the single source of truth for
        # operation identity, worklist stamps, pipeline/run metadata, source
        # check dates, candidate history, and rendered review.
        effective_date_obj = self._adapters.clock.today() if self._adapters.clock is not None else date.today()
        effective_date = effective_date_obj.isoformat()
        run_id = _operation_id(config_path, effective_date_obj)

        run = _WeeklyRunContext(
            config_path=config_path,
            output_dir=output_dir,
            state_dir=state_dir,
            adapters=self._adapters,
            monotonic=self._monotonic,
            operation_started_monotonic=operation_started_monotonic,
            log=log,
            live_config=live_config,
            log_path=log_path,
            effective_date_obj=effective_date_obj,
            effective_date=effective_date,
            run_id=run_id,
        )
        run.model_usage = getattr(self, "_model_usage", None)

        for adapter in (
            self._adapters.capture,
            self._adapters.codex,
            getattr(self._adapters, "isbn_completion", None),
            self._adapters.enrichment,
            self._adapters.adjudication,
            getattr(self._adapters, "pm_logline", None),
            getattr(self._adapters, "original_edition_completion", None),
            getattr(self._adapters, "work_age_completion", None),
            getattr(self._adapters, "russian_edition_completion", None),
        ):
            set_recorder = getattr(adapter, "set_progress_snapshot_failure_recorder", None)
            if callable(set_recorder):
                set_recorder(run.record_progress_snapshot_failure)
        # Issue #452: bind the optional per-item snapshot heartbeat to the
        # production adapters that opt in. Purely advisory; legacy adapters
        # without the setter (and legacy presentations without the optional
        # method) stay untouched and silent.
        kirkus_failures: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()
        run.kirkus_failures = kirkus_failures
        progress = WeeklyProgressPresentation(
            output_dir,
            getattr(self._adapters.presentation, "announce_progress_snapshot", None),
            monotonic=self._monotonic,
            failure_recorder=lambda stage, reason: kirkus_failures.put((stage, reason)),
        )
        run.progress = progress
        wire_snapshot_progress_presentation(self._adapters, progress)
        return run

    def _claim_run_lock(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """A scheduled run takes the run lock first, and skips itself when a live run holds it.

        The run in progress owns the log and the artifacts, so the skip writes
        nothing, not even the log. The lock is the one the preflight takes;
        the preflight then finds it held by this run and keeps it.
        """
        acquire = getattr(self._adapters.repository, "acquire_guard", None)
        if not callable(acquire):
            return None
        take_over = getattr(self._adapters.repository, "take_over_dead_guard", None)
        if self._second_attempt and callable(take_over):
            # The first attempt's unit has ended, so a dead owner's lock is that attempt's (#1175).
            take_over()
        guard = acquire()
        if (
            not getattr(guard, "acquired", False)
            and getattr(guard, "reason", "") in RUN_IN_PROGRESS_GUARD_REASONS
            and self._lock_held_by_live_run()
        ):
            return WeeklyWorkflowResult(outcome=WorkflowOutcome.SKIPPED_RUN_IN_PROGRESS)
        return None

    def _lock_held_by_live_run(self) -> bool:
        """Whether a verifiably live process holds the run lock (#1057).

        A lock younger than the stale limit is ``guard_held`` whether or not
        its process lives, so the reason alone would skip a night after a
        crash. An adapter that cannot tell is not trusted: the run goes on
        and the preflight refuses it with a reported failure.
        """
        check = getattr(self._adapters.repository, "held_by_live_run", None)
        return callable(check) and check() is True

    def _check_retry_evidence(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """Read this operation's earlier evidence; block an interrupted Kirkus capture."""
        # Read and retain only operation-stamped evidence before repository
        # preflight.  Legacy entries are never used for retry ownership.
        run.evidence = inspect_run_evidence(run.output_dir, run.config_path, run.effective_date_obj)
        if self._second_attempt:
            # Read under the run lock, so a run that ended a moment ago is seen; nothing is written.
            earlier_run = confirm_published(prior_run(run.evidence), self._committed_run_log(run), run.run_id)
            if self._on_prior_run is not None:
                self._on_prior_run(None if log_unreadable(run.output_dir) else earlier_run)
            if earlier_run.completed:
                return WeeklyWorkflowResult(outcome=WorkflowOutcome.SKIPPED_ALREADY_COMPLETED)
        run.is_same_op_retry = run.evidence.capture_current
        if (
            not run.is_same_op_retry
            and run.evidence.stamped_current
            # A start of this operation that saved its candidate state got past
            # the Kirkus capture: its leftovers are a late failure's, which the
            # preflight carries (#1161), not an interrupted capture.
            and not _latest_start_saved_state(run.evidence.log)
            and (
                any(entry.get("stage") == "codex-main" for entry in run.evidence.log)
                or (
                    any(entry.get("stage") == "capture" and entry.get("status") == "ok" for entry in run.evidence.log)
                    and any(entry.get("stage") == "kirkus-precollection" for entry in run.evidence.log)
                )
            )
            and not any(entry.get("stage") == "kirkus-codex" for entry in run.evidence.log)
        ):
            # Valid deferred and committed late captures resume below. An
            # inconsistent capture must never discard the main batch's work.
            run.log[:] = list(run.evidence.log)
            return _bad_outcome(
                WorkflowOutcome.INTERVENTION_REQUIRED,
                "capture",
                run.log,
                log_path=run.log_path,
                reason="interrupted_kirkus_capture_requires_recovery",
                operation_id=run.run_id,
            )
        if run.is_same_op_retry:
            prior = [entry for entry in run.evidence.log if entry.get("stage") not in {"terminal", "outcome-candidate"}]
            run.log[:] = prior
        return None

    def _committed_run_log(self, run: _WeeklyRunContext) -> str | None:
        """HEAD's copy of the run log (#1160); ``None`` when the repository adapter cannot read it."""
        read = getattr(self._adapters.repository, "committed_text", None)
        if not callable(read):
            return None
        text = read(run.log_path)
        return text if isinstance(text, str) else None

    def _update_repository(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """Stage 1: repository preflight and update, before the run writes its log."""
        # Stage 1: Repository preflight and update.
        # Run preflight then update BEFORE persisting any log entries
        # into the output directory so the real Git adapter does not see
        # the workflow's own log as a dirty tree.
        if self._adapters.repository is not None:
            # Issue #315: before preflight, read-only validate whether *output_dir*
            # is a valid current operation.  Only a validated same-operation retry
            # may exempt its own workflow-owned artifacts during preflight/update.
            # The allowlist uses exact resolved paths — unrelated files inside
            # output_dir still block.
            validated_allowlist = set(run.evidence.allowlist) if run.is_same_op_retry else None
            # The log an earlier failed run left modified is set aside by the
            # preflight instead of blocking this run (#1057).
            set_run_log_path = getattr(self._adapters.repository, "set_run_log_path", None)
            if callable(set_run_log_path):
                set_run_log_path(run.log_path)
            # So are the proven leftovers of an earlier run that failed late (#1161).
            set_leftover_scope = getattr(self._adapters.repository, "set_leftover_scope", None)
            if callable(set_leftover_scope):
                set_leftover_scope(leftover_scope(run.config_path, run.output_dir, run.state_dir))

            repo_result = self._adapters.repository.preflight(validated_artifact_allowlist=validated_allowlist)
            if (
                self._scheduled
                and repo_result.get("reason") in RUN_IN_PROGRESS_GUARD_REASONS
                and self._lock_held_by_live_run()
            ):
                return WeeklyWorkflowResult(outcome=WorkflowOutcome.SKIPPED_RUN_IN_PROGRESS)
            if repo_result.get("status") != "ok":
                return _bad_outcome(
                    WorkflowOutcome.INTERVENTION_REQUIRED,
                    "repository-preflight",
                    run.log,
                    log_path=run.log_path,
                    reason=_normalize_adapter_reason(repo_result),
                    operation_id=run.run_id,
                )
            # A run carrying a late failure's leftovers says so in its log, even
            # when the update or the code gate then stops it (#1161).
            _note_carried_leftovers(run, repo_result)
            repo_update = self._adapters.repository.update(validated_artifact_allowlist=validated_allowlist)
            if repo_update.get("status") not in ("ok", "unchanged"):
                return _bad_outcome(
                    WorkflowOutcome.INTERVENTION_REQUIRED,
                    "repository-update",
                    run.log,
                    log_path=run.log_path,
                    reason=_normalize_adapter_reason(repo_update),
                    operation_id=run.run_id,
                )
            # A fast-forward may change config or an included portfolio overlay.
            # Never recapture over evidence that ceased to identify this operation.
            if run.is_same_op_retry and not _capture_is_current(
                run.output_dir, run.config_path, run.effective_date_obj
            ):
                return _bad_outcome(
                    WorkflowOutcome.INTERVENTION_REQUIRED,
                    "repository-update",
                    run.log,
                    log_path=run.log_path,
                    reason="operation_changed_after_update",
                    operation_id=run.run_id,
                )
            gate_verdict = self._pass_code_gate(run)
            if isinstance(gate_verdict, WeeklyWorkflowResult):
                return gate_verdict
            # A .venv updated by this start's gate is restarted on like pulled code: modules already
            # imported may be the old versions (#1130).
            updated_now = bool(gate_verdict and gate_verdict.get("checked") and gate_verdict.get("environment_update"))
            # At most two restarts: the first for pulled code or an update, a second only for an update made
            # after the first (a start restarted on pulled code may still pull a pin change and install it).
            restart = (
                (repo_update.get("pulled") and not os.environ.get(RESTARTED_ENV))
                or (updated_now and not os.environ.get(ENVIRONMENT_UPDATED_ENV))
                or _leaves_start_snapshot(gate_verdict)
            )
            if self._scheduled and restart:
                return WeeklyWorkflowResult(
                    outcome=WorkflowOutcome.RESTART_ON_UPDATED_CODE,
                    run_summary={RESTART_AFTER_ENVIRONMENT_UPDATE: True} if updated_now else {},
                )
            run.log_stage(
                "repository-preflight",
                "ok",
                detail=_sanitize_repo_result(repo_result),
            )
            run.log_stage("repository-update", repo_update.get("status", "ok"), detail=repo_update)
            if gate_verdict is not None:
                run.log_stage(CODE_GATE_STAGE, str(gate_verdict.get("status", "")), detail=gate_verdict)
            _log_set_asides(run, repo_result)
        return None

    def _pass_code_gate(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | dict[str, Any] | None:
        """The gate on new code (#1124), after the update and before any stage that reads or writes state.

        A scheduled run checks a code version that has not passed on this
        machine and stops on a failure: nothing is written but the run log,
        the lock is released, and the morning report names the reason and
        the commit. A manual run is never blocked; it only says when the
        code has not passed here. Returns the verdict to log, or the failed
        result.
        """
        from autoscout.code_gate import STATUS_PASSED
        from autoscout.code_gate import UNCHECKED_MANUAL_RUN_LINE
        from autoscout.code_gate import short_commit

        gate = self._adapters.code_gate
        if gate is None:
            return None
        if not self._scheduled:
            current = gate.status()
            if current.get("status") != STATUS_PASSED:
                gate.announce(UNCHECKED_MANUAL_RUN_LINE)
            return {"status": str(current.get("status", "")), "commit": short_commit(current.get("commit"))}
        handed_over = self._confirm_last_passed_code(run, gate)
        if handed_over is not None:
            return handed_over
        verdict = gate.ensure_passed()
        detail: dict[str, Any] = {
            "commit": short_commit(verdict.get("commit")),
            "checked": bool(verdict.get("checked")),
            "passed_at": str(verdict.get("passed_at", "")),
        }
        # The update this start made, or the one the first start made before restarting (#1130).
        update = verdict.get("environment_update")
        if isinstance(update, dict) and (detail["checked"] or os.environ.get(ENVIRONMENT_UPDATED_ENV)):
            detail["environment_update"] = update
        if verdict.get("status") != STATUS_PASSED:
            failed_detail = {"commit": detail["commit"]}
            if "environment_update" in detail:
                failed_detail["environment_update"] = detail["environment_update"]
            fallback = self._last_passed_code_plan(gate, verdict)
            if fallback is not None and fallback.get("status") == "ready":
                return WeeklyWorkflowResult(
                    outcome=WorkflowOutcome.RUN_ON_LAST_PASSED_CODE, run_summary={LAST_PASSED_CODE_PLAN: fallback}
                )
            failed_detail.update(_fallback_refusal_detail(fallback))
            return _bad_outcome(
                WorkflowOutcome.INTERVENTION_REQUIRED,
                CODE_GATE_STAGE,
                run.log,
                log_path=run.log_path,
                reason=_normalize_adapter_reason(verdict),
                operation_id=run.run_id,
                detail=failed_detail,
            )
        return {"status": STATUS_PASSED, **detail}

    def _confirm_last_passed_code(
        self, run: _WeeklyRunContext, gate: Any
    ) -> WeeklyWorkflowResult | dict[str, Any] | None:
        """In a run started again on the last passing code (#1159): its code-gate verdict, or the stop.

        ``None`` in any other run, which goes through the gate as usual.
        """
        from autoscout.code_gate import short_commit
        from autoscout.code_gate_fallback import STATUS_RAN_LAST_PASSED

        confirm = getattr(gate, "confirm_fallback", None)
        verdict = confirm() if callable(confirm) else None
        if verdict is None:
            return None
        if verdict.get("status") == STATUS_RAN_LAST_PASSED:
            return dict(verdict)
        return _bad_outcome(
            WorkflowOutcome.INTERVENTION_REQUIRED,
            CODE_GATE_STAGE,
            run.log,
            log_path=run.log_path,
            reason=_normalize_adapter_reason(verdict),
            operation_id=run.run_id,
            detail={
                "commit": short_commit(verdict.get("commit")),
                "fallback_refused": str(verdict.get("fallback_refused", "")),
            },
        )

    @staticmethod
    def _last_passed_code_plan(gate: Any, verdict: dict[str, Any]) -> dict[str, Any] | None:
        """Whether the scheduled run can run the last passing code after this failed gate (#1159)."""
        plan = getattr(gate, "fallback_plan", None)
        return plan(verdict) if callable(plan) else None

    def _start_operation(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """Write the operation's identity and the annual edition audit to the log."""
        # This is the first safe point at which a new operation can write its
        # identity into workflow-log.json.
        try:
            state_backend = select_state_backend_from_file(run.config_path)
        except StateStoreError:
            return _bad_outcome(
                WorkflowOutcome.INTERVENTION_REQUIRED,
                "operation-start",
                run.log,
                log_path=run.log_path,
                reason="state_backend_unknown",
                operation_id=run.run_id,
            )
        # The state directory's own backend marker decides when it has one
        # (#1093); a config or environment selection that disagrees stops the
        # run before anything is written.
        if run.state_dir is not None:
            try:
                state_backend = resolve_state_backend(run.state_dir, state_backend)
            except StateStoreError:
                return _bad_outcome(
                    WorkflowOutcome.INTERVENTION_REQUIRED,
                    "operation-start",
                    run.log,
                    log_path=run.log_path,
                    reason="state_backend_conflict",
                    operation_id=run.run_id,
                )
        # Every later stage that moved to the State Store uses this one store,
        # so the whole run reads and writes the backend recorded here.
        run.state_store = None if run.state_dir is None else open_state_store(run.state_dir, state_backend)
        if run.state_store is not None and not keeps_json_in_state_dir(run.state_store):
            # A store kept outside the state directory may be missing on this
            # machine (a checkout that pulled a cut-over state directory, #1093);
            # one read finds that out before anything is collected.
            try:
                run.state_store.read(Collection.SOURCE_PROPOSALS)
            except (StateStoreError, OSError):
                return _bad_outcome(
                    WorkflowOutcome.INTERVENTION_REQUIRED,
                    "operation-start",
                    run.log,
                    log_path=run.log_path,
                    reason="state_store_unavailable",
                    operation_id=run.run_id,
                )
        run.log_stage(
            "operation-start",
            "ok",
            detail={"operation_date": run.effective_date, "state_backend": state_backend.describe()},
        )

        # An annual source carries its edition in its identity and is refreshed
        # by hand. Three were a year behind in run 4090c59a and two of them
        # reported `collected`, because a year-old award list is indistinguishable
        # from a fresh one once extraction succeeds (#680, #700 part B).
        #
        # Advisory only, and deliberately not a source problem: those feed the
        # editor's Russian summary and mean collection failed. A source needing
        # a rollover check has not failed and is not the editor's decision. It
        # reads configuration only, costs no network, and cannot fail the run -
        # a status of `unknown` is the worst it can report. It is judged on the
        # run's own date against each source's declared release window.
        run.log_stage(
            ANNUAL_AUDIT_STAGE_NAME,
            "ok",
            detail=annual_audit_stage_result(run.config_path, today=run.effective_date_obj),
        )
        return None

    def _check_toolchain(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """Stage 0: toolchain preflight; a failed Codex check stops the run."""
        # Stage 0: Toolchain preflight before capture.
        # Codex readiness is fail-closed. Discovery adapter is warn-only and uses
        # GET /health so it spends no Brave quota. Every check is three-state
        # (pass/fail or pass/degraded/unknown) so inconclusive is never
        # confused with healthy.
        stage_zero_codex = self._probe_stage_zero_codex()
        stage_zero_discovery = self._probe_stage_zero_discovery()
        stage_zero_result = build_stage_zero_result(stage_zero_codex, stage_zero_discovery)
        overall = str(stage_zero_result.get("overall_status", "unknown"))
        if overall == "pass":
            stage_zero_log_status = "ok"
        elif overall == "fail":
            stage_zero_log_status = "failed"
        elif overall == "degraded":
            stage_zero_log_status = "degraded"
        else:
            stage_zero_log_status = "unknown"
        run.log_stage(STAGE_ZERO_NAME, stage_zero_log_status, detail=stage_zero_result)
        if str(stage_zero_codex.get("status", "")) == "fail":
            # Fail closed before capture wastes work. Persist the durable
            # summary surfacing before returning, because workflow-log.json
            # is replaced at the start of each run.
            try:
                report_store = WeeklyReportStore(run.output_dir)
                existing_summary = report_store.read("run-summary.json", default={})
                if not isinstance(existing_summary, dict):
                    existing_summary = {}
                existing_summary["toolchain_preflight"] = stage_zero_result
                with report_store.transaction("toolchain-preflight-failed"):
                    report_store.write_json("run-summary.json", existing_summary)
            except Exception as exc:
                # The run is already failing, so this must not raise.  But the
                # whole point of writing run-summary.json is that
                # workflow-log.json is replaced each run, so a lost write must
                # leave a trace rather than vanish (issue #473).
                run.log.append(
                    stamp_entry(
                        {
                            "stage": STAGE_ZERO_NAME,
                            "status": "summary-write-failed",
                            "reason": str(exc)[:500],
                            "at": _now_iso(),
                        },
                        run.run_id,
                    )
                )
            return _bad_outcome(
                WorkflowOutcome.INTERVENTION_REQUIRED,
                STAGE_ZERO_NAME,
                run.log,
                log_path=run.log_path,
                reason=str(stage_zero_codex.get("message", "codex toolchain not ready")),
                operation_id=run.run_id,
                run_summary={"toolchain_preflight": stage_zero_result},
            )
        run.stage_zero_for_summary = stage_zero_result
        return None

    def _start_kirkus_pass(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """Open the source diagnostics and start the Kirkus pass beside capture when it is due."""
        # Capture/extraction diagnostics are retained independently because
        # replay pipeline source-runs may replace the capture source-runs.
        run.operational_source_problems = []
        run.pass_problem_names = set()
        run.capture_timing = {"duration_seconds": 0.0, "source_runs": []}

        # Kirkus requests overlap the other sources' capture and extraction.
        # Main stages own the main snapshot; the shared presenter persists
        # the pass separately and combines both under one heartbeat limit.
        run.kirkus_future = None
        run.kirkus_source_name = "Kirkus"
        kirkus_precollection = getattr(self._adapters, "kirkus_precollection", None)
        if kirkus_precollection is not None and not run.is_same_op_retry:
            from autoscout.config import load_validated_config
            from autoscout.kirkus_precollection import WEEKLY_STAGE_NAME as KIRKUS_STAGE_NAME
            from autoscout.kirkus_precollection import configured_source
            from autoscout.kirkus_precollection import kirkus_is_due

            try:
                if kirkus_is_due(run.config_path, run.effective_date_obj):
                    run.kirkus_source_name = str(configured_source(load_validated_config(run.config_path))["name"])
                    run.announce(KIRKUS_STAGE_NAME)
                    run.log_stage(KIRKUS_STAGE_NAME, "started")
                    kirkus_budget = min(KIRKUS_PASS_RUNTIME_BUDGET_SECONDS, run.remaining_source_seconds())
                    run.progress.start_kirkus(kirkus_budget)
                    self._kirkus_pool = DaemonThreadPool(1, thread_name_prefix="kirkus-pass-")
                    run.kirkus_future = self._kirkus_pool.submit(
                        kirkus_precollection.collect,
                        run.config_path,
                        run.output_dir,
                        run.effective_date_obj,
                        presentation=run.progress,
                        failure_recorder=lambda stage, reason: run.kirkus_failures.put((stage, reason)),
                        budget_seconds=kirkus_budget,
                        write_snapshot=False,
                        stop_requested=self._kirkus_stop,
                    )
            except Exception as exc:
                # No outcome of the pass may stop the run; capture then reads the
                # previous result and names it, as before (#816).
                run.kirkus_future = None
                run.progress.finish_kirkus()
                run.log_stage(
                    KIRKUS_STAGE_NAME,
                    "ok",
                    detail={"status": "not_collected", **keep_exception(exc, KIRKUS_STAGE_NAME)},
                )
        return None

    def _run_pm_pass(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """The PM deals pass, or on a resumed operation the pass it already made."""
        # The PM deals pass reads the mailbox before capture, on every run (#946).
        # It is short and sequential; no outcome of it stops the run.
        pm_precollection = getattr(self._adapters, "pm_precollection", None)
        if pm_precollection is not None and run.is_same_op_retry:
            # A resumed operation starts no second pass, but keeps naming the pass it already made.
            from autoscout.pm_precollection import configured_source_name as pm_source_name
            from autoscout.pm_precollection import weekly_source_problem as pm_source_problem

            retained_pm = next(
                (
                    entry.get("detail")
                    for entry in reversed(run.log)
                    if entry.get("stage") == "pm-precollection" and entry.get("status") == "ok"
                ),
                None,
            )
            if isinstance(retained_pm, dict):
                pm_problem = pm_source_problem(retained_pm, pm_source_name(run.config_path))
                if pm_problem is not None:
                    run.add_pass_problem(pm_problem)
        elif pm_precollection is not None:
            from autoscout.pm_precollection import WEEKLY_STAGE_NAME as PM_STAGE_NAME
            from autoscout.pm_precollection import configured_source_name as pm_source_name
            from autoscout.pm_precollection import pm_config_path
            from autoscout.pm_precollection import weekly_source_problem as pm_source_problem

            if pm_config_path(run.config_path).is_file():
                run.announce(PM_STAGE_NAME)
                try:
                    pm_result = pm_precollection.collect(
                        run.config_path,
                        run.output_dir,
                        presentation=run.progress,
                        failure_recorder=run.record_progress_snapshot_failure,
                        budget_seconds=min(PM_PASS_RUNTIME_BUDGET_SECONDS, run.remaining_source_seconds()),
                        run_date=run.effective_date_obj,
                    )
                except Exception as exc:
                    pm_result = {
                        "status": "not_collected",
                        "reason_code": "unexpected_error",
                        **keep_exception(exc, PM_STAGE_NAME),
                    }
                # Never log a pass as failed: that status names what stopped a run.
                run.log_stage(PM_STAGE_NAME, "ok", detail=pm_result)
                pm_problem = pm_source_problem(pm_result, pm_source_name(run.config_path))
                if pm_problem is not None:
                    run.add_pass_problem(pm_problem)
        return None

    def _run_pw_pass(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """The PW issue pass, or on a resumed operation the pass it already made."""
        # The PW issue pass reads the mailbox and downloads any new issue before capture,
        # on every run (#956). A week with no new issue email opens no subscriber link.
        pw_precollection = getattr(self._adapters, "pw_precollection", None)
        if pw_precollection is not None and run.is_same_op_retry:
            # A resumed operation starts no second pass, but keeps naming the pass it already made.
            from autoscout.pw_precollection import configured_source_name as pw_source_name
            from autoscout.pw_precollection import weekly_source_problem as pw_source_problem

            retained_pw = next(
                (
                    entry.get("detail")
                    for entry in reversed(run.log)
                    if entry.get("stage") == "pw-precollection" and entry.get("status") == "ok"
                ),
                None,
            )
            if isinstance(retained_pw, dict):
                pw_problem = pw_source_problem(retained_pw, pw_source_name(run.config_path))
                if pw_problem is not None:
                    run.add_pass_problem(pw_problem)
        elif pw_precollection is not None:
            from autoscout.pw_precollection import WEEKLY_STAGE_NAME as PW_STAGE_NAME
            from autoscout.pw_precollection import configured_source_name as pw_source_name
            from autoscout.pw_precollection import pw_config_path
            from autoscout.pw_precollection import weekly_source_problem as pw_source_problem

            if pw_config_path(run.config_path).is_file():
                run.announce(PW_STAGE_NAME)
                try:
                    pw_result = pw_precollection.collect(
                        run.config_path,
                        run.output_dir,
                        presentation=run.progress,
                        failure_recorder=run.record_progress_snapshot_failure,
                        budget_seconds=min(PW_PASS_RUNTIME_BUDGET_SECONDS, run.remaining_source_seconds()),
                        run_date=run.effective_date_obj,
                    )
                except Exception as exc:
                    pw_result = {
                        "status": "not_collected",
                        "reason_code": "unexpected_error",
                        **keep_exception(exc, PW_STAGE_NAME, omit_message=True),
                    }
                # Never log a pass as failed: that status names what stopped a run.
                run.log_stage(PW_STAGE_NAME, "ok", detail=pw_result)
                pw_problem = pw_source_problem(pw_result, pw_source_name(run.config_path))
                if pw_problem is not None:
                    run.add_pass_problem(pw_problem)
        return None

    def _capture(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """Stage 2: capture the source pages, unless this operation's capture is current."""
        # Stage 2: Capture
        run.announce("capture")
        # Whether this run fetched anything of its own. A pipeline that scouts
        # nothing means something different depending on the answer (#388).
        run.capture_was_refreshed = False
        if not _capture_is_current(run.output_dir, run.config_path, run.effective_date_obj):
            run.capture_was_refreshed = True
            capture_options: dict[str, Any] = {"live": True, "effective_date": run.effective_date}
            if run.kirkus_future is not None:
                capture_options["kirkus_mode"] = "defer"
            capture_result = self._adapters.capture.prepare(run.config_path, run.output_dir, **capture_options)
            if capture_result.get("returncode") != 0:
                return _bad_outcome(
                    WorkflowOutcome.INTERVENTION_REQUIRED,
                    "capture",
                    run.log,
                    reason=str(capture_result.get("reason", "capture failed")),
                    log_path=run.log_path,
                    operation_id=run.run_id,
                )
            from autoscout.codex_recovery import CodexResumeAdapter

            _bind_worklist_to_operation(run.output_dir, run.run_id)
            CodexResumeAdapter(run.output_dir).mark_stage_complete(run.output_dir, "capture", run.run_id)
            run.log_stage("capture", "ok", detail=capture_result)
            run.capture_timing = dict(capture_result)
        else:
            from autoscout.mvp_run_evidence import capture_source_runs

            # Complete a committed late append whose separate source-runs
            # write was interrupted. Evidence validation above is read-only.
            if read_json(run.output_dir / "codex-extraction-worklist.json", default={}).get("kirkus_source_runs"):
                captured_runs = capture_source_runs(run.output_dir)
                if captured_runs != read_json(run.output_dir / "source-runs.json", default=[]):
                    write_json(run.output_dir / "source-runs.json", captured_runs)
            run.log_stage("capture", "ok", detail={"status": "worklist_matches_current_run"})
        return None

    def _extract_main_batch(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """Stage 3: Codex extraction of the main batch within the derived operation budget."""
        # Stage 3: Codex handoff
        run.announce("codex")
        run.elapsed_before_extraction = max(0.0, self._monotonic() - run.operation_started_monotonic)
        run.extraction_budget_seconds = max(
            0.0,
            WEEKLY_OPERATION_CEILING_SECONDS - run.elapsed_before_extraction - FINALIZATION_RESERVE_SECONDS,
        )
        fulfill = self._adapters.codex.fulfill
        try:
            parameters = inspect.signature(fulfill).parameters.values()
            accepts_budget = any(
                parameter.name == "extraction_budget_seconds" or parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            accepts_budget = True
        run.worklist = read_json(run.output_dir / "codex-extraction-worklist.json", default={})
        run.deferred_kirkus = "kirkus_pending" in run.worklist
        from autoscout.source_collectors.base import contract_family

        main_urls = {
            item["source_url"]
            for item in run.worklist.get("work_items", [])
            if contract_family(item.get("source_metadata", {})) != "kirkus-coming-soon"
        }
        # A night whose only sources are not due (#1057) has nothing to extract.
        nothing_due = not run.worklist.get("work_items") and bool(run.worklist.get("not_due_sources"))
        if nothing_due:
            from autoscout.codex_recovery import _write_extraction_outcomes

            # This operation's outcome ledger is empty, as an extraction round
            # writes it; an earlier operation's unfinished outcome left in the
            # directory would otherwise stop finalization.
            _write_extraction_outcomes(run.output_dir, [], [])
        if (run.deferred_kirkus and not main_urls) or nothing_due:
            run.codex_result = {"status": "ok", "duration_seconds": 0.0}
        elif run.deferred_kirkus and not run.worklist["kirkus_pending"]:
            # Revalidate/reuse the main artifacts, but keep Kirkus failures
            # inside the nonblocking late batch, including on recovery.
            run.codex_result = fulfill(
                run.output_dir, source_urls=main_urls, extraction_budget_seconds=run.extraction_budget_seconds
            )
        elif accepts_budget:
            run.codex_result = fulfill(run.output_dir, extraction_budget_seconds=run.extraction_budget_seconds)
        else:
            # Keep old injected test adapters usable while all production
            # adapters receive the derived operation bound.
            run.codex_result = fulfill(run.output_dir)
        return None

    def _join_kirkus_pass(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """Wait for the Kirkus pass, then capture and extract its late batch; never stops the run."""
        from autoscout.source_collectors.base import contract_family

        if run.deferred_kirkus:
            run.log_stage("codex-main", str(run.codex_result.get("status", "failed")), detail=dict(run.codex_result))
            if run.kirkus_future is not None:
                try:
                    waiting_started = self._monotonic()
                    waiting_budget = run.remaining_source_seconds()
                    waiting = ProgressSnapshotWriter(
                        run.output_dir,
                        monotonic=self._monotonic,
                        presentation=run.progress,
                        failure_recorder=run.record_progress_snapshot_failure,
                    )
                    while not run.kirkus_future.done() and run.remaining_source_seconds() > 0:
                        waiting.publish(
                            stage=KIRKUS_WAIT_STAGE,
                            status="running",
                            elapsed_seconds=self._monotonic() - waiting_started,
                            rows=[],
                            stage_budget_seconds=waiting_budget,
                            force=False,
                        )
                        try:
                            run.kirkus_future.result(timeout=min(0.25, run.remaining_source_seconds()))
                        except TimeoutError:
                            if run.kirkus_future.done():
                                break
                    kirkus_result = run.kirkus_future.result(timeout=run.remaining_source_seconds())
                except TimeoutError:
                    self._kirkus_stop.set()
                    kirkus_result = {"status": "not_collected", "reason_code": "kirkus_time_budget"}
                except Exception as exc:
                    kirkus_result = {"status": "not_collected", **keep_exception(exc, "kirkus-precollection")}
                run.progress.finish_kirkus()
                # Never log a pass as failed: that status names what stopped a run.
                run.log_stage("kirkus-precollection", "ok", detail=kirkus_result)
            else:
                # The attempt counts even if the process died before the pass
                # returned. Capture reads its retained partial/older result.
                kirkus_result = next(
                    (
                        entry.get("detail", {})
                        for entry in reversed(run.log)
                        if entry.get("stage") == "kirkus-precollection" and entry.get("status") == "ok"
                    ),
                    {"status": "not_collected", "reason_code": "interrupted_kirkus_precollection"},
                )
            if kirkus_result.get("status") == "not_collected":
                run.add_pass_problem(
                    {
                        "source_name": run.kirkus_source_name,
                        "status": "failed",
                        "reason": "Kirkus pre-collection failed; capture uses the previous result when available",
                    }
                )
            while not run.kirkus_failures.empty():
                run.record_progress_snapshot_failure(*run.kirkus_failures.get_nowait())

            try:
                if run.worklist["kirkus_pending"]:
                    run.announce("capture")
                    late_capture = self._adapters.capture.prepare(
                        run.config_path,
                        run.output_dir,
                        live=True,
                        effective_date=run.effective_date,
                        kirkus_mode="only",
                        runtime_budget_seconds=run.remaining_source_seconds(),
                    )
                    _bind_worklist_to_operation(run.output_dir, run.run_id)
                else:
                    late_capture = {"returncode": 0, "status": "worklist_matches_current_run"}
                run.capture_timing["source_runs"] = late_capture.get("source_runs", [])
                run.capture_timing["duration_seconds"] = float(run.capture_timing.get("duration_seconds", 0)) + float(
                    late_capture.get("duration_seconds", 0)
                )
                run.log_stage("kirkus-capture", "ok", detail=late_capture)
                run.worklist = read_json(run.output_dir / "codex-extraction-worklist.json", default={})
                kirkus_urls = {
                    item["source_url"]
                    for item in run.worklist.get("work_items", [])
                    if contract_family(item.get("source_metadata", {})) == "kirkus-coming-soon"
                }
                if late_capture.get("returncode") != 0:
                    raise RuntimeError("Kirkus capture did not complete")
                if kirkus_urls:
                    run.announce("codex")
                    late_extraction = self._adapters.codex.fulfill_additional(
                        run.output_dir,
                        source_urls=kirkus_urls,
                        extraction_budget_seconds=run.remaining_source_seconds(),
                    )
                    run.log_stage("kirkus-codex", "ok", detail=late_extraction)
                    run.operational_source_problems.extend(_source_problems_from_extraction(late_extraction))
                    run.codex_result["duration_seconds"] = float(run.codex_result.get("duration_seconds", 0)) + float(
                        late_extraction.get("duration_seconds", 0)
                    )
                    run.codex_result["source_timings"] = run.codex_result.get(
                        "source_timings", []
                    ) + late_extraction.get("source_timings", [])
                    run.codex_result["diagnostics"] = run.codex_result.get("diagnostics", []) + late_extraction.get(
                        "diagnostics", []
                    )
                    for count in ("ready_items", "failed_items", "skipped_items", "total_items", "retries_used"):
                        run.codex_result[count] = int(run.codex_result.get(count, 0) or 0) + int(
                            late_extraction.get(count, 0) or 0
                        )
                    run.codex_result["failed_items"] += int(late_extraction.get("unfinished_items", 0) or 0)
                    if late_extraction.get("status") != "ok":
                        raise RuntimeError("Kirkus extraction did not complete")
            except Exception as exc:
                # The main batch can still finalize. Keep the source named
                # even when an adapter fails before it writes diagnostics.
                run.add_pass_problem(
                    {
                        "source_name": run.kirkus_source_name,
                        "status": "failed",
                        "reason": f"Kirkus late batch incomplete ({type(exc).__name__})",
                    }
                )
                run.log_stage(
                    "kirkus-codex", "ok", detail={"status": "not_collected", "exception_type": type(exc).__name__}
                )
        return None

    def _record_source_processing(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """Name capture and extraction problems and log source processing; a failed extraction stops the run."""
        if run.capture_was_refreshed:
            run.operational_source_problems.extend(_source_problems_from_capture(run.capture_timing))
        elif run.is_same_op_retry:
            run.operational_source_problems.extend(
                _source_problems_from_capture(
                    {"source_runs": read_json(run.output_dir / "source-runs.json", default=[])}
                )
            )

        from autoscout.codex_recovery import CAPTURE_RUNTIME_BUDGET_SECONDS
        from autoscout.codex_recovery import PORTFOLIO_RUNTIME_BUDGET_SECONDS

        capture_duration = float(run.capture_timing.get("duration_seconds", 0.0) or 0.0)
        extraction_duration = float(run.codex_result.get("duration_seconds", 0.0) or 0.0)
        capture_budget = float(
            run.capture_timing.get("runtime_budget_seconds", CAPTURE_RUNTIME_BUDGET_SECONDS)
            or CAPTURE_RUNTIME_BUDGET_SECONDS
        )
        extraction_budget = float(
            run.codex_result.get("runtime_budget_seconds", PORTFOLIO_RUNTIME_BUDGET_SECONDS)
            or PORTFOLIO_RUNTIME_BUDGET_SECONDS
        )
        source_processing_detail = {
            "duration_seconds": round(capture_duration + extraction_duration, 3),
            "runtime_budget_seconds": capture_budget + extraction_budget,
            "capture": {
                "duration_seconds": capture_duration,
                "runtime_budget_seconds": capture_budget,
                "source_timings": _capture_source_timings(run.capture_timing),
            },
            "extraction": {
                "duration_seconds": extraction_duration,
                "runtime_budget_seconds": extraction_budget,
                "source_timings": run.codex_result.get("source_timings", []),
            },
            "weekly_ceiling_seconds": WEEKLY_OPERATION_CEILING_SECONDS,
            "elapsed_before_extraction_seconds": round(run.elapsed_before_extraction, 3),
            "finalization_reserve_seconds": FINALIZATION_RESERVE_SECONDS,
            "extraction_budget_derived_seconds": round(run.extraction_budget_seconds, 3),
        }
        if run.codex_result.get("status") == "unfinished":
            unfinished_count = int(run.codex_result.get("unfinished_items", 0) or 0)
            run.log_stage("codex", "unfinished", detail=run.codex_result)
            run.log_stage("source-processing", "unfinished", detail=source_processing_detail)
            return _bad_outcome(
                WorkflowOutcome.INTERVENTION_REQUIRED,
                "codex",
                run.log,
                log_path=run.log_path,
                reason=f"portfolio capacity stop: {unfinished_count} extraction item(s) unfinished",
                operation_id=run.run_id,
                run_summary={"source_processing": source_processing_detail, "extraction": run.codex_result},
                unfinished_items=unfinished_count,
                capacity_stop_count=int(run.codex_result.get("capacity_stop_count", 0) or 0),
            )
        if run.codex_result.get("status") != "ok":
            run.log_stage("codex", "failed", detail=run.codex_result)
            run.log_stage("source-processing", "failed", detail=source_processing_detail)
            return _bad_outcome(
                WorkflowOutcome.INTERVENTION_REQUIRED,
                "codex",
                run.log,
                log_path=run.log_path,
            )
        run.log_stage("codex", "ok", detail=run.codex_result)
        run.operational_source_problems.extend(_source_problems_from_extraction(run.codex_result))
        run.log_stage("source-processing", "ok", detail=source_processing_detail)
        return None

    def _finalize(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """Stage 4: finalize the extraction into a replay config for the pipeline."""
        # Stage 4: Finalization
        run.announce("finalize")
        finalize_result = _run_finalize_stage(run.output_dir, run.live_config, run.effective_date)
        if finalize_result.get("returncode") != 0:
            return _bad_outcome(
                WorkflowOutcome.INTERVENTION_REQUIRED,
                "finalize",
                run.log,
                reason=str(finalize_result.get("reason", "finalization failed")),
                log_path=run.log_path,
            )
        run.log_stage("finalize", "ok", detail=finalize_result)

        replay_config = run.output_dir / "codex-replay-config.json"
        if not replay_config.exists():
            return _bad_outcome(
                WorkflowOutcome.INTERVENTION_REQUIRED,
                "finalize",
                run.log,
                log_path=run.log_path,
                reason="missing replay config",
            )
        run.pipeline_config = replay_config
        run.fresh_evidence_produced = run.capture_was_refreshed and bool(finalize_result.get("fresh_evidence"))
        return None

    def _run_pipeline(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """Stage 5: the MVP pipeline; an unpublishable result stops the run."""
        # Stage 5: Pipeline
        run.announce("pipeline")
        pipeline_result = _run_pipeline_stage(
            run.pipeline_config, run.output_dir, run.state_dir, run.effective_date, state_store=run.state_store
        )
        if pipeline_result.get("returncode") != 0:
            return _bad_outcome(
                WorkflowOutcome.INTERVENTION_REQUIRED,
                "pipeline",
                run.log,
                reason=str(pipeline_result.get("exception", "pipeline failed")),
                log_path=run.log_path,
            )

        # The pipeline kept the last collection's failure streaks for a rerun after feedback; this run counts
        # its own in a later stage, so a run that stops before it never shows the earlier run's (#1125).
        from autoscout.source_failure_streaks import RUN_SUMMARY_KEY as STREAKS_KEY

        _remove_run_summary_key(WeeklyReportStore(run.output_dir), STREAKS_KEY)
        pipeline_path_value = pipeline_result.get("pipeline_path")
        run.pipeline_path = pipeline_path_value if isinstance(pipeline_path_value, PipelinePath) else None
        if not pipeline_result_is_publishable(
            run.pipeline_path,
            fresh_evidence_produced=run.fresh_evidence_produced,
        ):
            run.log_stage("pipeline", "failed", detail=pipeline_result)
            reason = (
                "pipeline did not report a valid path"
                if run.pipeline_path is None
                else (
                    "pipeline scouted nothing after fresh capture and extraction; "
                    "the stored report belongs to an earlier run"
                )
            )
            return _bad_outcome(
                WorkflowOutcome.INTERVENTION_REQUIRED,
                "pipeline",
                run.log,
                reason=reason,
                log_path=run.log_path,
                operation_id=run.run_id,
                pipeline_path=run.pipeline_path,
            )

        trial_failure = pipeline_result.get("search_optimization_trial")
        if isinstance(trial_failure, dict) and trial_failure.get("status") == "failed":
            run.log_stage(
                "search-optimization-trial",
                "failed",
                detail={"exception_type": trial_failure.get("exception_type", "unknown")},
            )

        run.log_stage("pipeline", "ok", detail=pipeline_result)
        return None

    def _complete_isbns(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """ISBN completion; advisory."""
        # ISBN completion (issue #979): fill missing ISBNs of this run's
        # candidates before the description stages, which search by ISBN.
        # Advisory: no outcome of it stops the run. The failure is caught
        # inside the transaction so the context manager removes its own
        # marker, as the adjudication stage does.
        isbn_completion = getattr(self._adapters, "isbn_completion", None)
        if isbn_completion is not None:
            run.announce("isbn-completion")
            isbn_result: dict[str, Any]
            with WeeklyReportStore(run.output_dir).transaction("isbn-completion"):
                try:
                    isbn_result = isbn_completion.complete(
                        run.output_dir,
                        run.state_dir or run.output_dir,
                        effective_date=run.effective_date,
                        state_store=run.stage_store(),
                    )
                except Exception as exc:
                    isbn_result = {"status": "failed", "exception_type": type(exc).__name__}
            run.log_stage(
                "isbn-completion",
                "failed" if isbn_result.get("status") not in {"ok", "skipped"} else str(isbn_result["status"]),
                detail={key: value for key, value in isbn_result.items() if key != "records"},
            )
        return None

    def _enrich_descriptions(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """Stage 6: description enrichment; a failed input or persistence stops the run."""
        # Stage 6: Description Enrichment
        run.announce("enrichment")
        run.enrichment_result = {
            "status": "ok",
            "enriched": 0,
            "fallback": 0,
            "grounded_description_counts": {outcome: 0 for outcome in OUTCOME_ORDER},
            "grounded_description_reason_counts": {},
        }
        if self._adapters.enrichment is not None:
            with WeeklyReportStore(run.output_dir).transaction("enrichment"):
                run.enrichment_result = _run_enrichment_stage(
                    self._adapters.enrichment,
                    run.output_dir,
                    run.state_dir,
                    effective_date=run.effective_date,
                    operation_id=run.run_id,
                    state_store=run.state_store,
                )
            enrichment_status = str(run.enrichment_result.get("status", "ok"))
            if enrichment_status == "input_failed":
                reason = _description_input_failure_reason(run.enrichment_result)
                run.log_stage("enrichment", "input_failed", detail={"reason": reason})
                return _bad_outcome(
                    WorkflowOutcome.INTERVENTION_REQUIRED,
                    "description-input",
                    run.log,
                    log_path=run.log_path,
                    reason=reason,
                    operation_id=run.run_id,
                )
            if enrichment_status == "recovery_failed":
                return _bad_outcome(
                    WorkflowOutcome.INTERVENTION_REQUIRED,
                    "description-recovery",
                    run.log,
                    log_path=run.log_path,
                    reason="description_persistence_recovery_failure",
                    operation_id=run.run_id,
                )
            if enrichment_status == "persistence_failed":
                return _bad_outcome(
                    WorkflowOutcome.INTERVENTION_REQUIRED,
                    "description-persistence",
                    run.log,
                    log_path=run.log_path,
                    reason="description_persistence_failure",
                    operation_id=run.run_id,
                )
            if enrichment_status == "skipped":
                run.log_stage("enrichment", "skipped", detail=run.enrichment_result)
            elif enrichment_status == "failed":
                run.log_stage("enrichment", "failed", detail=run.enrichment_result)
            else:
                run.log_stage("enrichment", "ok", detail=run.enrichment_result)
        else:
            run.log_stage("enrichment", "skipped", detail={"reason": "no enrichment adapter"})
        return None

    def _adjudicate_descriptions(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """Description adjudication; a persistence failure stops the run."""
        # Stage: Description Adjudication (issue #640). Runs after enrichment
        # and before the post-enrichment refresh, because it reads
        # candidate-history.json that enrichment writes and changes the same
        # descriptions and exclusions. One refresh then covers both passes.
        run.announce("description-adjudication")
        run.adjudication_result = {"status": "skipped", "reason": "no adjudication adapter"}
        if self._adapters.adjudication is not None:
            # The failure is caught inside the transaction, so the context
            # manager exits normally and removes its own marker, holding its
            # own lock and checking its own owner token. Catching outside
            # would leave the marker behind, and taking the marker away by
            # hand afterwards would run after the lock is already released
            # and could remove a competing transaction's marker instead.
            stage_failure: dict[str, Any] | None = None
            with WeeklyReportStore(run.output_dir).transaction("description-adjudication"):
                try:
                    run.adjudication_result = _run_adjudication_stage(
                        self._adapters.adjudication,
                        run.output_dir,
                        run.state_dir,
                        effective_date=run.effective_date,
                        operation_id=run.run_id,
                        state_store=run.stage_store(),
                    )
                except Exception as exc:
                    stage_failure = {
                        "status": "failed",
                        "reason": "stage_execution_failure",
                        "exception_type": type(exc).__name__,
                    }
            if stage_failure is not None:
                run.adjudication_result = stage_failure
                run.log_stage("description-adjudication", "failed", detail=run.adjudication_result)
            else:
                adjudication_status = str(run.adjudication_result.get("status", "ok"))
                if adjudication_status == "skipped":
                    run.log_stage("description-adjudication", "skipped", detail=run.adjudication_result)
                elif adjudication_status in ("failed", "persistence_failed"):
                    run.log_stage("description-adjudication", "failed", detail=run.adjudication_result)
                else:
                    run.log_stage("description-adjudication", "ok", detail=run.adjudication_result)
        else:
            run.log_stage("description-adjudication", "skipped", detail={"reason": "no adjudication adapter"})
        # A persistence failure stops the run at its own named stage, the
        # way enrichment stops at ``description-persistence``. An ordinary
        # per-candidate failure and a stage execution failure stay warnings
        # and the run continues to refresh, review, and health (issue #641).
        if str(run.adjudication_result.get("status", "")) == "persistence_failed":
            run.log_stage("description-adjudication-persistence", "failed", detail=run.adjudication_result)
            return _bad_outcome(
                WorkflowOutcome.INTERVENTION_REQUIRED,
                "description-adjudication-persistence",
                run.log,
                log_path=run.log_path,
                reason=str(
                    run.adjudication_result.get("reason", "description_adjudication_persistence_failure")
                    or "description_adjudication_persistence_failure"
                ),
                operation_id=run.run_id,
            )
        return None

    def _write_pm_logline_descriptions(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """PM logline descriptions, when the weekly config switches them on; advisory."""
        # PM logline descriptions (issue #950): after both description
        # stages, so a book either of them described keeps its description,
        # and before the refresh, which then counts what this stage wrote.
        # Advisory: no outcome of it stops the run.
        run.logline_result = {}
        pm_logline = getattr(self._adapters, "pm_logline", None)
        if pm_logline is not None:
            from autoscout.pm_logline_description import WEEKLY_STAGE_NAME as LOGLINE_STAGE_NAME
            from autoscout.pm_logline_description import config_enabled as logline_enabled

            if logline_enabled(run.config_path):
                run.announce(LOGLINE_STAGE_NAME)
                with WeeklyReportStore(run.output_dir).transaction(LOGLINE_STAGE_NAME):
                    try:
                        run.logline_result = pm_logline.describe(
                            run.config_path,
                            run.output_dir,
                            run.state_dir or run.output_dir,
                            run.effective_date,
                            state_store=run.stage_store(),
                        )
                    except Exception as exc:
                        run.logline_result = {"status": "failed", "exception_type": type(exc).__name__}
                run.log_stage(
                    LOGLINE_STAGE_NAME,
                    "failed"
                    if run.logline_result.get("status") not in {"ok", "skipped"}
                    else str(run.logline_result["status"]),
                    detail=run.logline_result,
                )
        return None

    def _complete_original_editions(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """Original edition completion; advisory."""
        # Original edition completion (issue #968): after the PM logline stage and
        # before the refresh, so a confirmed English title exists when the refresh
        # re-applies the Hard Rules and Soft Preferences, which match English titles.
        # Advisory: no outcome of it stops the run.
        run.original_result = {}
        original_edition_completion = getattr(self._adapters, "original_edition_completion", None)
        if original_edition_completion is not None:
            run.announce("original-edition-completion")
            # The stage reads the booksellers' portal switch from the weekly config (#1140).
            set_config_path = getattr(original_edition_completion, "set_config_path", None)
            if callable(set_config_path):
                set_config_path(run.config_path)
            with WeeklyReportStore(run.output_dir).transaction("original-edition-completion"):
                try:
                    run.original_result = original_edition_completion.complete(
                        run.output_dir,
                        run.state_dir or run.output_dir,
                        effective_date=run.effective_date,
                        state_store=run.stage_store(),
                    )
                except Exception as exc:
                    run.original_result = {"status": "failed", "exception_type": type(exc).__name__}
            run.log_stage(
                "original-edition-completion",
                "failed"
                if run.original_result.get("status") not in {"ok", "skipped"}
                else str(run.original_result["status"]),
                detail={key: value for key, value in run.original_result.items() if key != "records"},
            )
        return None

    def _complete_work_age(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """Work age completion; advisory."""
        # Work age (issue #1138): after original edition completion, so a
        # translation with a confirmed original takes the original's age.
        # Advisory: no outcome of it stops the run, and it changes nothing the
        # refresh derives, so it never triggers one.
        work_age_completion = getattr(self._adapters, "work_age_completion", None)
        if work_age_completion is not None:
            run.announce("work-age-completion")
            work_age_result: dict[str, Any]
            with WeeklyReportStore(run.output_dir).transaction("work-age-completion"):
                try:
                    work_age_result = work_age_completion.complete(
                        run.output_dir,
                        run.state_dir or run.output_dir,
                        effective_date=run.effective_date,
                        state_store=run.stage_store(),
                    )
                except Exception as exc:
                    work_age_result = {"status": "failed", "exception_type": type(exc).__name__}
            run.log_stage(
                "work-age-completion",
                "failed" if work_age_result.get("status") not in {"ok", "skipped"} else str(work_age_result["status"]),
                detail=work_age_result,
            )
        return None

    def _complete_russian_editions(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """Russian-edition completion, when the weekly config switches it on; advisory."""
        # Russian editions (issue #1181): after original edition and work age
        # completion, so the pass has the original title and the first
        # publication year. Advisory: no outcome of it stops the run, and the
        # finding is a mark the review shows, nothing the refresh derives.
        russian_edition_completion = getattr(self._adapters, "russian_edition_completion", None)
        if russian_edition_completion is None:
            return None
        from autoscout.run_time_limit import russian_edition_seconds_left
        from autoscout.russian_edition_completion import INVALID_WEEKLY_CONFIG
        from autoscout.russian_edition_completion import MIN_STAGE_SECONDS as RUSSIAN_EDITION_MIN_SECONDS
        from autoscout.russian_edition_completion import STAGE_NAME as RUSSIAN_EDITION_STAGE_NAME
        from autoscout.russian_edition_health import SKIPPED_RUN_TIME_LIMIT
        from autoscout.russian_edition_settings import config_enabled as russian_edition_enabled
        from autoscout.russian_edition_settings import config_switch_malformed

        if not russian_edition_enabled(run.config_path):
            if config_switch_malformed(run.config_path):
                # Off, as for an absent switch, but named: the operator meant something by it.
                run.log_stage(
                    RUSSIAN_EDITION_STAGE_NAME,
                    "failed",
                    detail={"status": "failed", "reason": INVALID_WEEKLY_CONFIG},
                )
            return None
        # The stage is not in the run's worst case (autoscout.run_time_limit):
        # it takes only what is left of it after the stages that follow.
        seconds_left = russian_edition_seconds_left(run.monotonic() - run.operation_started_monotonic)
        if seconds_left < RUSSIAN_EDITION_MIN_SECONDS:
            run.log_stage(
                RUSSIAN_EDITION_STAGE_NAME,
                "skipped",
                detail={"status": "skipped", "reason": SKIPPED_RUN_TIME_LIMIT, "seconds_left": int(seconds_left)},
            )
            return None
        run.announce(RUSSIAN_EDITION_STAGE_NAME)
        # The stage reads its limits from the weekly config.
        set_config_path = getattr(russian_edition_completion, "set_config_path", None)
        if callable(set_config_path):
            set_config_path(run.config_path)
        report_store = WeeklyReportStore(run.output_dir)
        russian_result: dict[str, Any]
        try:
            # The report transaction is taken only around the write, as the
            # standalone command takes it: the lookups can take half an hour,
            # and an interruption during them must leave no marker.
            russian_result = russian_edition_completion.complete(
                run.output_dir,
                run.state_dir or run.output_dir,
                effective_date=run.effective_date,
                state_store=run.stage_store(),
                publish_lock=lambda: report_store.transaction(RUSSIAN_EDITION_STAGE_NAME),
                budget_seconds=seconds_left,
            )
        except Exception as exc:
            russian_result = {"status": "failed", "exception_type": type(exc).__name__}
        run.log_stage(
            RUSSIAN_EDITION_STAGE_NAME,
            "failed" if russian_result.get("status") not in {"ok", "skipped"} else str(russian_result["status"]),
            detail=russian_result,
        )
        return None

    def _refresh_after_descriptions(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """Recompute the derived artifacts when a description stage changed candidates."""
        # Post-enrichment refresh: enrichment can exclude current candidates
        # (authoritative pure fiction) and rewrite descriptions, so the
        # pipeline's pre-enrichment run-summary, pilot status, and source
        # metrics would otherwise go stale. Adjudication changes the same
        # things, so one refresh covers both passes. One narrow canonical
        # seam recomputes the derived artifacts from the final candidates
        # and exclusion records (Issue #391).
        if (
            run.enrichment_result.get("status") in {"ok", "failed"}
            or run.adjudication_result.get("status") in {"ok", "failed"}
            or int(run.logline_result.get("written", 0) or 0) > 0
            or int(run.logline_result.get("unchanged", 0) or 0) > 0
            or int(run.original_result.get("confirmed", 0) or 0) > 0
        ):
            from autoscout.pipeline import refresh_post_enrichment_artifacts

            with WeeklyReportStore(run.output_dir).transaction("post-enrichment-refresh"):
                refresh_result = refresh_post_enrichment_artifacts(
                    run.config_path, run.output_dir, run.state_dir, run.effective_date, state_store=run.state_store
                )
            trial_failure = (
                refresh_result.get("search_optimization_trial") if isinstance(refresh_result, dict) else None
            )
            if isinstance(trial_failure, dict) and trial_failure.get("status") == "failed":
                run.log_stage(
                    "search-optimization-trial",
                    "failed",
                    detail={"exception_type": trial_failure.get("exception_type", "unknown")},
                )
        return None

    def _score_editorial_fit(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """Editorial-fit shadow scores, when the weekly config switches them on; advisory."""
        # Editorial-fit shadow scoring (issue #725): advisory scores for the
        # final candidates, recorded before the editor reviews them. It writes
        # only its own directory, and no outcome of it can stop the run.
        editorial_fit = getattr(self._adapters, "editorial_fit", None)
        if editorial_fit is not None and _editorial_fit_shadow_enabled(run.config_path):
            try:
                fit_result = editorial_fit.score(
                    run.output_dir,
                    run.state_dir or run.output_dir,
                    run.effective_date,
                    state_store=run.stage_store(),
                    review_ordered_by_fit=fit_ordering_enabled(run.config_path),
                )
                run.log_stage("editorial-fit-shadow", str(fit_result.get("status", "ok")), detail=fit_result)
            except Exception as exc:
                run.log_stage("editorial-fit-shadow", "failed", detail={"exception_type": type(exc).__name__})
        return None

    def _render_review(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """Stage 8: render the review; a missing review stops the run."""
        # Stage 8: Review rendering (after enrichment and adjudication)
        run.announce("review")
        review_result = _run_review_stage(
            run.output_dir, run.state_dir, run.effective_date, run.config_path, state_store=run.state_store
        )
        if review_result.get("returncode") != 0:
            return _bad_outcome(
                WorkflowOutcome.INTERVENTION_REQUIRED,
                "review",
                run.log,
                log_path=run.log_path,
                reason="review rendering failed",
            )
        html_path = WeeklyReportStore(run.output_dir).path("weekly-review.html")
        run.review_available = review_result.get("html_exists", False)
        run.review_path = review_result.get("html_path", str(html_path))
        run.log_stage("review", "ok", detail=review_result)

        if not run.review_available:
            return _bad_outcome(
                WorkflowOutcome.INTERVENTION_REQUIRED,
                "review",
                run.log,
                log_path=run.log_path,
                reason="review artifact missing",
            )
        return None

    def _record_source_failure_streaks(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """Count this run's attempts per source into the State Store and put the run's view in run-summary.json (#1125).

        Before the health check, so run-health, the outcome, the doctor and
        the morning report all read the same view. The attempts are the
        replay ledger (``source-runs.json``), the capture ledger for sources
        that stopped before replay, and the sources the extraction left not
        ready. Without a state directory, or when the count cannot be
        recorded, the run has no view (an earlier run's is removed) and every
        failed or skipped source asks for attention, as before #1125. The
        stage never stops the run.
        """
        from autoscout.source_failure_streaks import RUN_SUMMARY_KEY
        from autoscout.source_failure_streaks import configured_threshold
        from autoscout.source_failure_streaks import update_streaks

        run.source_failure_streaks = None
        stage = "source-failure-streaks"
        report_store = WeeklyReportStore(run.output_dir)
        if run.state_store is None:
            _remove_run_summary_key(report_store, RUN_SUMMARY_KEY)
            return None
        try:
            threshold = configured_threshold(read_json(run.config_path, default={}))
            source_runs = report_store.read("source-runs.json", default=[])
            run_summary = report_store.read("run-summary.json", default=None)
            if not isinstance(source_runs, list) or not isinstance(run_summary, dict):
                _remove_run_summary_key(report_store, RUN_SUMMARY_KEY)
                run.log_stage(stage, "ok", detail={"status": "not_recorded", "reason": "run_artifacts_unreadable"})
                return None
            attempts = _attempted_source_runs(run, source_runs, str(run_summary.get("operation_id", "")).strip())
            with run.state_store.transaction():
                update = update_streaks(
                    run.state_store.read(Collection.SOURCE_FAILURE_STREAKS),
                    attempts,
                    run.effective_date,
                    threshold,
                    run.pass_problem_names,
                )
                run.state_store.replace(Collection.SOURCE_FAILURE_STREAKS, update.records)
            with report_store.transaction("source-failure-streaks"):
                report_store.write_json("run-summary.json", {**run_summary, RUN_SUMMARY_KEY: update.view})
        except Exception as exc:  # counting streaks must never stop the run
            _remove_run_summary_key(report_store, RUN_SUMMARY_KEY)
            # Never "failed": that status names what stopped a run.
            run.log_stage(
                stage,
                "ok",
                detail={"status": "not_recorded", "reason": "streaks_unavailable", "error": type(exc).__name__},
            )
            return None
        run.source_failure_streaks = update.view
        run.log_stage(
            stage,
            "ok",
            detail={
                "status": "recorded",
                "threshold": threshold,
                "attempted": update.view["attempted"],
                "failing": len(update.view["failing"]),
                "broken": sum(1 for entry in update.view["failing"] if entry["broken"]),
                "widespread": update.view["widespread"],
                "recovered": len(update.view["recovered"]),
            },
        )
        return None

    def _check_health(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """Stage 9: run health, then collect readiness, description-stage and source problems."""
        # Stage 9: Health (after review, for description-coverage.json consumption)
        run.announce("health")
        health_result = _run_health_stage(run.output_dir, run.effective_date)
        run.report_snapshot = WeeklyReportStore(run.output_dir).snapshot("health", run.effective_date)
        run.readiness_facts = {}
        ready_for_editor: bool | None = None
        pilot = run.report_snapshot.json("pilot-status.json", {})
        if isinstance(pilot, dict):
            run.readiness_facts = pilot.get("readiness_basis", {})
            raw_ready = pilot.get("ready_for_editor")
            ready_for_editor = bool(raw_ready) if raw_ready is not None else None

        run_summary_value = run.report_snapshot.json("run-summary.json", {})
        run.run_summary = run_summary_value if isinstance(run_summary_value, dict) else {}
        description_stage_report_value = run.enrichment_result.get("description_stage_report", {})
        run.description_stage_report = (
            description_stage_report_value if isinstance(description_stage_report_value, dict) else {}
        )
        adjudication_stage_report_value = run.adjudication_result.get("description_adjudication_stage_report", {})
        run.adjudication_stage_report = (
            adjudication_stage_report_value if isinstance(adjudication_stage_report_value, dict) else {}
        )
        run.description_stage_problems = []
        if run.enrichment_result.get("status") == "failed":
            run.description_stage_problems.append(
                {
                    "stage": "grounded",
                    "status": "failed",
                    "reason": str(
                        run.description_stage_report.get(
                            "failure_reason",
                            run.enrichment_result.get("reason", "stage_execution_failure"),
                        )
                    ),
                }
            )
        elif run.description_stage_report.get("attention_required") and _grounded_attention_survives_adjudication(
            run.description_stage_report, run.adjudication_stage_report
        ):
            run.description_stage_problems.append(
                {
                    "stage": "grounded",
                    "status": "attention_required",
                    "reason": "description_stage_attention_required",
                }
            )
        if run.adjudication_result.get("status") == "failed":
            run.description_stage_problems.append(
                {
                    "stage": "adjudication",
                    "status": "failed",
                    "reason": str(
                        run.adjudication_stage_report.get(
                            "failure_reason",
                            run.adjudication_result.get("reason", "stage_execution_failure"),
                        )
                    ),
                }
            )
        elif run.adjudication_stage_report.get("attention_required"):
            run.description_stage_problems.append(
                {
                    "stage": "adjudication",
                    "status": "attention_required",
                    "reason": "description_adjudication_stage_attention_required",
                }
            )
        if run.logline_result.get("attention_required") or run.logline_result.get("status") == "failed":
            # Calls that failed, not loglines the model declined: on 2026-11-10
            # fourteen of them failed and the run reported nothing.
            run.description_stage_problems.append(
                {
                    "stage": "pm-logline",
                    "status": "attention_required",
                    "reason": "pm_logline_calls_failed",
                }
            )
        delta_counts_value = run.run_summary.get("update_delta_counts", {})
        run.delta_counts = delta_counts_value if isinstance(delta_counts_value, dict) else {}
        run.source_problems = list(run.operational_source_problems)

        run.source_runs = run.report_snapshot.json("source-runs.json", None)
        if isinstance(run.source_runs, list):
            for source in run.source_runs:
                status = str(source.get("status", ""))
                if status in {"skipped", "failed"}:
                    run.source_problems.append(
                        {
                            "source_name": str(source.get("source_name", "")),
                            "status": status,
                            "reason": str(source.get("reason", "")),
                        }
                    )
                elif isinstance(source, dict):
                    incomplete = _incomplete_coverage_problem(source)
                    if incomplete is not None:
                        run.source_problems.append(incomplete)
        # Issue #765: a collected source whose row count left its usual range
        # is a named caveat too. Failed or skipped entries for the same source
        # come first and win the deduplication.
        for departure in recorded_departures(run.run_summary.get("row_count_departures")):
            run.source_problems.append(
                {
                    "source_name": departure.source_name,
                    "status": "unusual_count",
                    "reason": departure.warning(),
                    "editor_detail": departure.editor_detail(),
                }
            )
        run.source_problems = _deduplicate_source_problems(run.source_problems)
        if not run.source_problems:
            skipped = int(run.run_summary.get("skipped_source_count", 0) or 0)
            failed = int(run.run_summary.get("failed_source_count", 0) or 0)
            if skipped:
                run.source_problems.append({"type": "skipped", "count": str(skipped)})
            if failed:
                run.source_problems.append({"type": "failed", "count": str(failed)})

        run.log_stage("health", "ok", detail=health_result)

        if health_result.get("returncode") != 0 or not run.report_snapshot.coherent:
            return _bad_outcome(
                WorkflowOutcome.INTERVENTION_REQUIRED,
                "health",
                run.log,
                log_path=run.log_path,
                reason="health check failed",
            )
        if ready_for_editor is None:
            return _bad_outcome(
                WorkflowOutcome.INTERVENTION_REQUIRED,
                "health",
                run.log,
                log_path=run.log_path,
                reason="missing pilot-status with readiness",
            )
        run.ready_for_editor = ready_for_editor
        return None

    def _record_completion_facts(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """Write the final description coverage, description-stage counts and Stage 0 result to run-summary.json."""
        # Refresh description_coverage from the captured post-enrichment snapshot.
        desc_cov = run.report_snapshot.json("description-coverage.json", {})
        if desc_cov and isinstance(desc_cov, dict):
            run.run_summary["description_coverage"] = desc_cov

        # Persist updated run-summary so canonical completion facts include
        # final description coverage and the complete description-stage counts.
        grounded_counts_value = run.enrichment_result.get("grounded_description_counts")
        if not isinstance(grounded_counts_value, dict):
            grounded_counts_value = {outcome: run.enrichment_result.get(outcome, 0) for outcome in OUTCOME_ORDER}
        grounded_counts = {}
        for outcome in OUTCOME_ORDER:
            try:
                grounded_counts[outcome] = int(grounded_counts_value.get(outcome, 0) or 0)
            except (TypeError, ValueError):
                grounded_counts[outcome] = 0
        reason_counts_value = run.enrichment_result.get("grounded_description_reason_counts", {})
        grounded_reason_counts: dict[str, int] = {}
        if isinstance(reason_counts_value, dict):
            for raw_reason, raw_count in sorted(reason_counts_value.items(), key=lambda item: str(item[0]).strip()):
                reason = str(raw_reason).strip()
                if not reason:
                    continue
                try:
                    grounded_reason_counts[reason] = int(raw_count or 0)
                except (TypeError, ValueError):
                    grounded_reason_counts[reason] = 0
        run.run_summary["grounded_description_counts"] = grounded_counts
        run.run_summary["grounded_description_reason_counts"] = grounded_reason_counts
        # Provenance counts read durable state, not the run's artifacts: an
        # adjudicated description from weeks ago still counts as
        # adjudicated. The key is stored only once one exists, so weeks
        # without adjudicated descriptions keep their exact summary bytes
        # (issue #644).
        if run.state_store is not None:
            history_records = run.state_store.read(Collection.CANDIDATE_HISTORY)
        else:
            history_records = read_run_history(run.output_dir)
        provenance_counts = count_description_provenance(history_records) if isinstance(history_records, list) else {}
        if has_adjudicated_descriptions(provenance_counts):
            run.run_summary["description_provenance_counts"] = provenance_counts
        if run.description_stage_report:
            run.run_summary["description_stage_report"] = run.description_stage_report
        if run.adjudication_stage_report:
            run.run_summary["description_adjudication_stage_report"] = run.adjudication_stage_report
        # Stage 0 surfacing: the log is replaced each run, so the durable
        # run-summary carries every Stage 0 result. Use a three-state string,
        # not a bool, so "unknown" is distinguishable from "pass".
        if hasattr(run, "stage_zero_for_summary") and isinstance(run.stage_zero_for_summary, dict):
            run.run_summary["toolchain_preflight"] = run.stage_zero_for_summary
        report_store = WeeklyReportStore(run.output_dir)
        with report_store.transaction("workflow-completion"):
            report_store.write_json("run-summary.json", run.run_summary)
        return None

    def _decide_outcome(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """Decide whether the completed run needs the editor's attention."""
        readiness_purpose = str(run.readiness_facts.get("readiness_purpose", "seed-calibration"))
        recurring_attention_required: bool | None
        if readiness_purpose == "recurring-weekly":
            # Consume the same recurring interpretation used by run-health.
            from autoscout.run_health import recurring_readiness

            canonical_readiness = recurring_readiness(
                run.output_dir,
                ready=run.ready_for_editor,
                editor_count=run.run_summary.get("editor_facing_count", 0),
                skipped_count=int(run.run_summary.get("skipped_source_count", 0) or 0),
                failed_count=int(run.run_summary.get("failed_source_count", 0) or 0),
                update_delta_counts=run.delta_counts,
                source_runs=run.source_runs if isinstance(run.source_runs, list) else None,
                source_failure_streaks=run.source_failure_streaks,
            )
            run.readiness_facts = {**run.readiness_facts, **canonical_readiness}
            recurring_attention_required = bool(canonical_readiness["attention_required"])
        else:
            # Reporting remains authoritative for calibration/legacy row-target readiness.
            recurring_attention_required = None

        run.attention_reasons = weekly_run_attention_reasons(
            readiness_purpose=readiness_purpose,
            recurring_attention_required=recurring_attention_required,
            ready_for_editor=run.ready_for_editor,
            source_problem_count=len(
                attention_source_problems(run.source_problems, run.source_failure_streaks, run.pass_problem_names)
            ),
            description_stage_problem_count=len(run.description_stage_problems),
        )
        attention_required = bool(run.attention_reasons)
        run.readiness_facts = {**run.readiness_facts, "attention_required": attention_required}

        run.outcome = (
            WorkflowOutcome.COMPLETED_ATTENTION_REQUIRED
            if attention_required
            else WorkflowOutcome.COMPLETED_NO_ATTENTION
        )
        return None

    def _scan_artifacts_for_secrets(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """Block publication when a canonical artifact holds a recognizable credential."""
        # Fail-closed artifact scan (issue #287): before any publication step
        # (opening the review, committing/pushing), scan the run's canonical
        # artifacts for recognizable credential shapes. A hit blocks
        # publication entirely -- the workflow terminates as
        # intervention-required with a sanitized diagnostic that names only
        # the flagged file, never the matched value.
        # The leftovers a run carries are published with it, whatever they are (#1161).
        carried = getattr(run.adapters.repository, "carried_paths", None)
        flagged_artifacts = _scan_output_dir_for_secrets(run.output_dir)
        if callable(carried):
            flagged_artifacts += _scan_carried_for_secrets(run.output_dir, carried(run.output_dir), flagged_artifacts)
        if flagged_artifacts:
            return _bad_outcome(
                WorkflowOutcome.INTERVENTION_REQUIRED,
                "artifact-secret-scan",
                run.log,
                log_path=run.log_path,
                reason="recognized credential pattern detected in canonical artifact(s): "
                + ", ".join(sorted(flagged_artifacts)),
            )
        return None

    def _open_review(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """Open the review for the editor."""
        # Stage 9: Open review (after outcome is computed).
        if self._adapters.presentation is not None:
            open_result = self._adapters.presentation.open_review(Path(run.review_path))
            run.log_stage("open-review", open_result.get("status", "attempted"), detail=open_result)
        return None

    def _export_state(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """Write the SQLite database's JSON export into the state directory; nothing on the JSON backend.

        Git keeps the export, not the database (#1074), so it is written after
        the last state write and before the credential scan and the envelope,
        which then see the files they commit. The JSON backend already keeps
        the JSON, logs nothing here and writes nothing. After the export the
        database and the exported files are compared; a difference stops the
        run before anything is committed.
        """
        if run.state_store is None:
            return None
        try:
            report = export_store(run.state_store)
        except (StateStoreError, OSError) as exc:
            # The error names state files and the database; the log keeps only its kind.
            run.log_stage(
                "state-export", "failed", detail={"reason": "state_export_failed", "error": type(exc).__name__}
            )
            _persist_final_log(run.log_path, run.log)
            return _bad_outcome(
                WorkflowOutcome.INTERVENTION_REQUIRED,
                "state-export",
                run.log,
                reason="state_export_failed",
                log_path=run.log_path,
            )
        if report is not None:
            # The run's health check runs before the export, so the agreement of the database
            # and its export is checked here, once the export is written (#1076).
            inspection = inspect_store(run.state_store)
            if inspection is not None and not inspection.consistent:
                run.log_stage(
                    "state-export",
                    "failed",
                    detail={
                        "reason": "state_export_mismatch",
                        "export_status": inspection.export_status,
                        "problem": inspection.problem,
                        "differing_count": inspection.differing_count,
                    },
                )
                _persist_final_log(run.log_path, run.log)
                return _bad_outcome(
                    WorkflowOutcome.INTERVENTION_REQUIRED,
                    "state-export",
                    run.log,
                    reason="state_export_mismatch",
                    log_path=run.log_path,
                )
            detail = report.to_dict()
            detail.pop("database", None)
            run.log_stage("state-export", "ok", detail=detail)
        return None

    def _finalize_repository(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult | None:
        """Record the outcome candidate, then commit and push the run and publish the terminal log."""
        run.log.append(
            stamp_entry({"stage": "outcome-candidate", "status": run.outcome.value, "at": _now_iso()}, run.run_id)
        )
        _persist_final_log(run.log_path, run.log)

        # Stage 9: Repository finalization. Record the candidate before it runs,
        # then persist its real result. A second finalize publishes that log
        # update, so canonical state includes the first commit/push outcome.
        if self._adapters.repository is not None:
            run.log_stage("repository-finalize", "started")
            repo_finalize = self._adapters.repository.finalize(run.output_dir)
            run.log[-1].update(status=repo_finalize.get("status", "unknown"), detail=repo_finalize)
            _persist_final_log(run.log_path, run.log)
            if repo_finalize.get("status") not in ("ok", "noop"):
                return _bad_outcome(
                    WorkflowOutcome.INTERVENTION_REQUIRED,
                    "repository-finalize",
                    run.log,
                    reason=repo_finalize.get("reason", "repository finalization failed"),
                    log_path=run.log_path,
                )
            run.log[:] = [entry for entry in run.log if entry.get("stage") != "outcome-candidate"]
            run.log.append(
                stamp_entry({"stage": "terminal", "status": run.outcome.value, "at": _now_iso()}, run.run_id)
            )
            _persist_final_log(run.log_path, run.log)
            publish_log = self._adapters.repository.finalize(run.output_dir)
            if publish_log.get("status") not in ("ok", "noop"):
                return _bad_outcome(
                    WorkflowOutcome.INTERVENTION_REQUIRED,
                    "repository-finalize",
                    run.log,
                    reason=publish_log.get("reason", "workflow log publish failed"),
                    log_path=run.log_path,
                )
        else:
            run.log[:] = [entry for entry in run.log if entry.get("stage") != "outcome-candidate"]
            run.log.append(
                stamp_entry({"stage": "terminal", "status": run.outcome.value, "at": _now_iso()}, run.run_id)
            )
            _persist_final_log(run.log_path, run.log)
        return None

    def _completed_result(self, run: _WeeklyRunContext) -> WeeklyWorkflowResult:
        """The completed run's result."""
        description_coverage_facts = run.run_summary.get("description_coverage", {}) or {}

        return WeeklyWorkflowResult(
            outcome=run.outcome,
            readiness_facts=run.readiness_facts,
            delta_counts=run.delta_counts,
            source_problems=run.source_problems,
            review_available=run.review_available,
            review_path=run.review_path,
            log=list(run.log),
            run_summary=run.run_summary,
            description_coverage=description_coverage_facts,
            grounded_description_counts={
                outcome: int(run.run_summary.get("grounded_description_counts", {}).get(outcome, 0) or 0)
                for outcome in OUTCOME_ORDER
            }
            if isinstance(run.run_summary.get("grounded_description_counts"), dict)
            else {outcome: 0 for outcome in OUTCOME_ORDER},
            grounded_description_reason_counts={
                str(reason): int(count)
                for reason, count in run.run_summary.get("grounded_description_reason_counts", {}).items()
            }
            if isinstance(run.run_summary.get("grounded_description_reason_counts"), dict)
            else {},
            description_provenance_counts={
                str(name): int(count)
                for name, count in run.run_summary.get("description_provenance_counts", {}).items()
            }
            if isinstance(run.run_summary.get("description_provenance_counts"), dict)
            else {},
            description_stage_report=run.description_stage_report,
            description_adjudication_stage_report=run.adjudication_stage_report,
            description_stage_problems=run.description_stage_problems,
            attention_reasons=run.attention_reasons,
            pass_problem_sources=tuple(sorted(run.pass_problem_names)),
            reused_source_count=int(run.run_summary.get("reused_artifact_count", 0) or 0),
            collecting_source_count=(
                int(run.run_summary.get("reused_artifact_count", 0) or 0)
                + int(run.run_summary.get("freshly_collected_count", 0) or 0)
            ),
            source_statuses=(
                tuple(str(source.get("status", "")).strip() for source in run.source_runs if isinstance(source, dict))
                if isinstance(run.source_runs, list)
                else None
            ),
            pipeline_path=run.pipeline_path,
        )
        return None

    def _stages(self) -> tuple[Callable[[_WeeklyRunContext], WeeklyWorkflowResult | None], ...]:
        """The weekly run's stages, in the order they run.

        Each stage takes the run context and returns ``None`` to continue, or
        the result that ends the run (an intervention-required outcome). A new
        stage is a method added here at its place in the order.

        A scheduled run (#1057) takes the run lock before anything else, so
        it can skip itself without writing a byte, and has no open-review
        stage: nobody sits at the server's screen.
        """
        stages = (
            self._claim_run_lock,
            self._check_retry_evidence,
            self._update_repository,
            self._start_operation,
            self._check_toolchain,
            self._start_kirkus_pass,
            self._run_pm_pass,
            self._run_pw_pass,
            self._capture,
            self._extract_main_batch,
            self._join_kirkus_pass,
            self._record_source_processing,
            self._finalize,
            self._run_pipeline,
            self._complete_isbns,
            self._enrich_descriptions,
            self._adjudicate_descriptions,
            self._write_pm_logline_descriptions,
            self._complete_original_editions,
            self._complete_work_age,
            self._complete_russian_editions,
            self._refresh_after_descriptions,
            self._score_editorial_fit,
            self._render_review,
            self._record_source_failure_streaks,
            self._check_health,
            self._record_completion_facts,
            self._decide_outcome,
            self._export_state,
            self._scan_artifacts_for_secrets,
            self._open_review,
            self._finalize_repository,
        )
        if self._scheduled:
            return tuple(stage for stage in stages if stage != self._open_review)
        return tuple(stage for stage in stages if stage != self._claim_run_lock)

    def _run(
        self,
        config_path: Path,
        output_dir: Path,
        state_dir: Path | None = None,
    ) -> WeeklyWorkflowResult:
        run = self._open_run(config_path, output_dir, state_dir)
        for stage in self._stages():
            result = stage(run)
            if result is not None:
                return result
        return self._completed_result(run)


def run_weekly_workflow(
    config_path: Path,
    output_dir: Path,
    state_dir: Path | None = None,
    adapters: WeeklyWorkflowAdapters | None = None,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    scheduled: bool = False,
    second_attempt: bool = False,
    on_prior_run: Callable[[PriorRun | None], None] | None = None,
) -> WeeklyWorkflowResult:
    workflow = WeeklyWorkflow(
        adapters, monotonic=monotonic, scheduled=scheduled, second_attempt=second_attempt, on_prior_run=on_prior_run
    )
    return workflow.run(config_path, output_dir, state_dir)


def _make_deterministic_enrichment() -> DescriptionEnrichmentAdapter:
    from autoscout.description_enrichment import DescriptionEnrichmentService
    from autoscout.description_enrichment import NoOpDescriptionEnrichmentAgent

    return DescriptionEnrichmentService(agent=NoOpDescriptionEnrichmentAgent())


def _make_deterministic_adjudication() -> DescriptionAdjudicationStageAdapter:
    from autoscout.description_adjudication import ADJUDICATION_MAX_WORKERS
    from autoscout.description_adjudication import ADJUDICATION_STAGE_BUDGET_SECONDS
    from autoscout.description_adjudication import DescriptionAdjudicationStageService
    from autoscout.description_adjudication import ReplayDescriptionAdjudicationAdapter
    from autoscout.grounded_description import BoundedHTTPFetcher
    from autoscout.grounded_description import NoNetworkHTTPTransport

    return DescriptionAdjudicationStageService(
        adapter=ReplayDescriptionAdjudicationAdapter(),
        fetcher=BoundedHTTPFetcher(transport=NoNetworkHTTPTransport()),
        scale_weekly_limit=True,
        stage_budget_seconds=ADJUDICATION_STAGE_BUDGET_SECONDS,
        max_workers=ADJUDICATION_MAX_WORKERS,
    )


GROUNDED_DISCOVERY_ENDPOINT_ENV = "AUTOSCOUT_GROUNDED_DISCOVERY_ENDPOINT"


def _make_production_enrichment(
    *,
    discovery_endpoint: str | None = None,
    repo_path: Path | None = None,
    stage_budget_seconds: float = STAGE_BUDGET_SECONDS,
) -> DescriptionEnrichmentAdapter:
    """Build the safe production enrichment adapter.

    Without ``AUTOSCOUT_GROUNDED_DISCOVERY_ENDPOINT`` the stage is offline-safe
    and performs no requests.  An explicit endpoint enables Phase B discovery
    with the bounded standard-library fetcher.
    """
    from autoscout.grounded_description import BoundedHTTPFetcher
    from autoscout.grounded_description import CodexGroundedDescriptionSynthesizer
    from autoscout.grounded_description import GroundedDescriptionStage
    from autoscout.grounded_description import GroundedDescriptionStageService
    from autoscout.grounded_description import HTTPDiscoveryProvider
    from autoscout.grounded_description import NoNetworkDiscoveryProvider
    from autoscout.grounded_description import committed_source_classifier
    from autoscout.vendor_rate_limit import VendorRateLimiter

    endpoint = discovery_endpoint
    if endpoint is None:
        endpoint = os.environ.get(GROUNDED_DISCOVERY_ENDPOINT_ENV, "").strip()
    if endpoint:
        # Issue #689: owner derivation decides admission.
        classifier = committed_source_classifier(repo_path)
        vendor_rate_limiter = VendorRateLimiter()
        stage = GroundedDescriptionStage(
            discovery=HTTPDiscoveryProvider(
                endpoint,
                classifier=classifier,
                rate_limiter=vendor_rate_limiter,
            ),
            fetch=BoundedHTTPFetcher(),
            synthesizer=CodexGroundedDescriptionSynthesizer(),
            rate_limiter=vendor_rate_limiter,
            stage_budget_seconds=stage_budget_seconds,
            scale_weekly_budget=True,
        )
        return GroundedDescriptionStageService(stage=stage)
    return GroundedDescriptionStageService(
        stage=GroundedDescriptionStage(discovery=NoNetworkDiscoveryProvider()),
        enabled=False,
        disabled_reason="grounded discovery endpoint not configured",
    )


def _make_production_adjudication() -> DescriptionAdjudicationStageAdapter:
    from autoscout.description_adjudication import ADJUDICATION_MAX_WORKERS
    from autoscout.description_adjudication import ADJUDICATION_STAGE_BUDGET_SECONDS
    from autoscout.description_adjudication import CodexDescriptionAdjudicationAdapter
    from autoscout.description_adjudication import DescriptionAdjudicationStageService

    return DescriptionAdjudicationStageService(
        adapter=CodexDescriptionAdjudicationAdapter(),
        scale_weekly_limit=True,
        stage_budget_seconds=ADJUDICATION_STAGE_BUDGET_SECONDS,
        max_workers=ADJUDICATION_MAX_WORKERS,
        # A book must not leave a weekly run at the literal placeholder
        # before the deeper search has also failed (issue #788).
        deep_pass=True,
    )


def weekly_adapters(
    repo_path: Path,
    progress: Callable[[str], None] | None = None,
    extraction_progress: Callable[[int, int, int, int], None] | None = None,
    snapshot_progress: Callable[[str], None] | None = None,
    *,
    stage_zero_codex_probe: Callable[[], dict[str, Any]] | None = None,
    stage_zero_discovery_probe: Callable[[], dict[str, Any]] | None = None,
) -> WeeklyWorkflowAdapters:
    """Build the production adapters.

    The advisory editorial-fit shadow stage (issue #725) is not built here. It
    calls a separately billed vendor, so the weekly command binds it with
    ``bind_editorial_fit_shadow`` and a production-shaped test never reaches it.

    Both data progress seams carry data, not sentences, and the caller formats
    them. ``progress`` receives stage keys. ``extraction_progress`` receives
    ``(completed, total, timed_out, retrying)`` portfolio counts from inside
    the extraction stage (issue #383). The third progress route is different:
    ``snapshot_progress`` is a line printer, not a data seam - it receives one
    already built Russian heartbeat line per progress snapshot (issue #452)
    and must be bound to the real stdout before the runner installs its
    redirect.

    The two Stage 0 probes default to the real toolchain probes, which is what
    production wants.  A production-shaped test must override them, because the
    real Codex probe spends one vendor request and reads the machine's installed
    CLI version (issue #541)."""
    from autoscout.codex_recovery import ProductionCodexAdapter
    from autoscout.codex_recovery import ProductionCodexCaptureAdapter
    from autoscout.git_envelope import CANONICAL_BRANCH
    from autoscout.git_envelope import CANONICAL_REMOTE_URL
    from autoscout.git_envelope import GitRepositoryAdapter

    enrichment = _make_production_enrichment(repo_path=repo_path)
    adjudication = _make_production_adjudication()
    return WeeklyWorkflowAdapters(
        capture=ProductionCodexCaptureAdapter(),
        codex=ProductionCodexAdapter(on_progress=extraction_progress),
        repository=GitRepositoryAdapter(
            repo_path,
            expected_branch=CANONICAL_BRANCH,
            expected_remote_url=CANONICAL_REMOTE_URL,
        ),
        presentation=_RealOpenAdapter(progress, snapshot_progress),
        enrichment=enrichment,
        adjudication=adjudication,
        clock=_SystemClockAdapter(),
        stage_zero_codex_probe=stage_zero_codex_probe or probe_codex_toolchain,
        stage_zero_discovery_probe=(stage_zero_discovery_probe or (lambda: probe_discovery_adapter(enrichment))),
    )


def bind_editorial_fit_shadow(adapters: WeeklyWorkflowAdapters) -> None:
    """Bind the live editorial-fit shadow stage to production adapters.

    Binding happens after the factory call, like the snapshot printer, so a
    custom factory with an older signature keeps working unchanged.
    """
    from autoscout.editorial_fit_shadow import ProductionEditorialFitShadowStage

    try:
        adapters.editorial_fit = ProductionEditorialFitShadowStage()
    except AttributeError:
        pass


def bind_isbn_completion(adapters: WeeklyWorkflowAdapters) -> None:
    """Bind the live ISBN completion stage (issue #979), like the other late bindings."""
    from autoscout.isbn_completion import ProductionIsbnCompletionStage

    # An explicitly injected stage (an offline test double) is kept.
    if getattr(adapters, "isbn_completion", None) is not None:
        return
    try:
        adapters.isbn_completion = ProductionIsbnCompletionStage()
    except AttributeError:
        pass


def bind_original_edition_completion(adapters: WeeklyWorkflowAdapters) -> None:
    """Bind the live original-edition completion stage (issue #968), like the other late bindings."""
    from autoscout.original_edition_completion import ProductionOriginalEditionCompletionStage

    # An explicitly injected stage (an offline test double) is kept.
    if getattr(adapters, "original_edition_completion", None) is not None:
        return
    try:
        adapters.original_edition_completion = ProductionOriginalEditionCompletionStage()
    except AttributeError:
        pass


def bind_work_age_completion(adapters: WeeklyWorkflowAdapters) -> None:
    """Bind the live work-age completion stage (issue #1138), like the other late bindings."""
    from autoscout.work_age import ProductionWorkAgeCompletionStage

    # An explicitly injected stage (an offline test double) is kept.
    if getattr(adapters, "work_age_completion", None) is not None:
        return
    try:
        adapters.work_age_completion = ProductionWorkAgeCompletionStage()
    except AttributeError:
        pass


def bind_russian_edition_completion(adapters: WeeklyWorkflowAdapters) -> None:
    """Bind the live Russian-edition completion stage (issue #1181), like the other late bindings.

    Bound always; the weekly run starts it only when the weekly config's switch is on.
    """
    from autoscout.russian_edition_completion import ProductionRussianEditionCompletionStage

    # An explicitly injected stage (an offline test double) is kept.
    if getattr(adapters, "russian_edition_completion", None) is not None:
        return
    try:
        adapters.russian_edition_completion = ProductionRussianEditionCompletionStage()
    except AttributeError:
        pass


def bind_pm_logline(adapters: WeeklyWorkflowAdapters) -> None:
    """Bind the PM logline descriptions (issue #950) to the production Codex agent."""
    from autoscout.pm_logline_description import CodexPmLoglineDescriber

    # An explicitly injected stage (an offline test double) is kept.
    if getattr(adapters, "pm_logline", None) is not None:
        return
    try:
        adapters.pm_logline = CodexPmLoglineDescriber()
    except AttributeError:
        pass


def bind_pm_precollection(adapters: WeeklyWorkflowAdapters) -> None:
    """Bind the live PM deals pass to production adapters, like the Kirkus pass."""
    from autoscout.pm_precollection import ProductionPmPrecollectionStage

    # Older custom factories return bundles without the field; they run without the pass.
    try:
        adapters.pm_precollection = ProductionPmPrecollectionStage()
    except AttributeError:
        pass


def bind_pw_precollection(adapters: WeeklyWorkflowAdapters) -> None:
    """Bind the live PW issue pass to production adapters, like the PM pass."""
    from autoscout.pw_precollection import ProductionPwPrecollectionStage

    # Older custom factories return bundles without the field; they run without the pass.
    try:
        adapters.pw_precollection = ProductionPwPrecollectionStage()
    except AttributeError:
        pass


def bind_code_gate(adapters: WeeklyWorkflowAdapters, repo_root: Path, announce: Callable[[str], None]) -> None:
    """Bind the run server's gate on new code (#1124), after the factory call like the other late bindings.

    The gate guards the code a run takes from Git, so a run whose repository
    adapter is not the Git adapter (a test or fixture bundle) gets none.
    """
    from autoscout.code_gate import ProductionCodeGate
    from autoscout.git_envelope import GitRepositoryAdapter

    if not isinstance(getattr(adapters, "repository", None), GitRepositoryAdapter):
        return
    try:
        adapters.code_gate = ProductionCodeGate(repo_root, announce=announce)
    except AttributeError:
        pass


def bind_kirkus_precollection(adapters: WeeklyWorkflowAdapters) -> None:
    """Bind the live Kirkus pass to production adapters, after the factory call like the other late bindings."""
    from autoscout.kirkus_precollection import ProductionKirkusPrecollectionStage

    try:
        adapters.kirkus_precollection = ProductionKirkusPrecollectionStage()
    except AttributeError:
        pass


def wire_snapshot_progress_presentation(
    adapters: WeeklyWorkflowAdapters, observer: Callable[[dict[str, Any]], None] | None = None
) -> None:
    """Bind the optional snapshot-heartbeat presentation to adapters that opt in.

    Issue #452: the presentation adapter's optional ``announce_progress_snapshot``
    method is handed to the production capture and extraction adapters through
    their ``set_snapshot_progress_presentation`` setters. Issue #706 extends
    the same heartbeat to the description stages: the enrichment and
    adjudication services expose the same setter and publish one row per
    candidate. Everything here is optional and defensive: a ``None``
    presentation, a legacy presentation that only implements ``announce``,
    and an adapter without the setter all stay untouched, so existing call
    sites that do not opt in keep working.
    """
    method = observer or getattr(adapters.presentation, "announce_progress_snapshot", None)
    if not callable(method):
        return
    for adapter in (
        adapters.capture,
        adapters.codex,
        getattr(adapters, "isbn_completion", None),
        adapters.enrichment,
        adapters.adjudication,
        getattr(adapters, "pm_logline", None),
        getattr(adapters, "original_edition_completion", None),
        getattr(adapters, "work_age_completion", None),
        getattr(adapters, "russian_edition_completion", None),
    ):
        setter = getattr(adapter, "set_snapshot_progress_presentation", None)
        if callable(setter):
            setter(method)


def bind_snapshot_progress_printer(adapters: WeeklyWorkflowAdapters, printer: Callable[[str], None] | None) -> None:
    """Bind the live heartbeat line printer to the presentation adapter.

    Issue #452: the CLI and the recovery runner construct adapters with the
    pre-#452 factory arguments and then bind the live snapshot printer through
    the optional ``set_snapshot_progress_printer`` setter on the returned
    presentation adapter. Every step uses ``getattr`` defensively: a mock or
    legacy factory returning an adapter without the setter (or without a
    presentation at all) stays untouched, so existing patched factories with
    the old exact signature keep working unchanged.
    """
    presentation = getattr(adapters, "presentation", None)
    if presentation is None:
        return
    setter = getattr(presentation, "set_snapshot_progress_printer", None)
    if callable(setter):
        setter(printer)


class _RealOpenAdapter:
    def __init__(
        self,
        progress: Callable[[str], None] | None = None,
        snapshot_progress: Callable[[str], None] | None = None,
    ) -> None:
        self._progress = progress
        self._snapshot_progress = snapshot_progress

    def announce(self, stage: str) -> None:
        if self._progress is not None:
            self._progress(stage)

    def set_snapshot_progress_printer(self, printer: Callable[[str], None] | None) -> None:
        """Bind the live heartbeat line printer after factory construction (issue #452)."""
        self._snapshot_progress = printer

    def announce_progress_snapshot(self, snapshot: dict[str, Any]) -> None:
        if self._snapshot_progress is None:
            return
        self._snapshot_progress(snapshot_progress_line(snapshot))

    def open_review(self, review_path: Path) -> dict[str, Any]:
        import webbrowser

        try:
            opened = webbrowser.open(str(review_path))
            status = "ok" if opened else "failed"
        except Exception:
            opened = False
            status = "failed"
        return {"status": status, "opened": opened, "path": str(review_path)}


def _capture_source_timings(result: dict[str, Any]) -> list[dict[str, Any]]:
    runs = result.get("source_runs", [])
    if not isinstance(runs, list):
        return []
    return [
        {
            "source_name": str(run.get("source_name", "")),
            "status": str(run.get("status", "")),
            "duration_seconds": float(run.get("duration_seconds", 0.0) or 0.0),
        }
        for run in runs
        if isinstance(run, dict)
    ]


def _incomplete_coverage_problem(run: dict[str, Any]) -> dict[str, str] | None:
    """Return a success-with-warning entry for an incomplete-coverage run.

    Issue #668: a collected/codex-captured source whose ranking was short
    must reach weekly completion as a named caveat, not vanish into a plain
    success. Failed and skipped runs are excluded here — they already report
    through their own entries.
    """
    if not isinstance(run, dict) or run.get("status") in {"failed", "skipped"}:
        return None
    coverage = incomplete_coverage_from_run(run)
    if coverage is None:
        return None
    reason = str(run.get("reason", "")).strip() or (
        f"incomplete ranking: exposes {coverage['observed_rows']} of {coverage['expected_rows']} expected raw rows"
    )
    return {
        "source_name": str(coverage["source_name"]),
        "status": "incomplete",
        "reason": reason,
        "observed_rows": str(coverage["observed_rows"]),
        "expected_rows": str(coverage["expected_rows"]),
    }


def _source_problems_from_capture(result: dict[str, Any]) -> list[dict[str, str]]:
    runs = result.get("source_runs", [])
    if not isinstance(runs, list):
        return []
    problems = [
        {
            "source_name": str(run.get("source_name", "")),
            "status": str(run.get("status", "failed")),
            "reason": str(run.get("reason", "")),
        }
        for run in runs
        if isinstance(run, dict) and run.get("status") in {"failed", "skipped"}
    ]
    for run in runs:
        if not isinstance(run, dict):
            continue
        incomplete = _incomplete_coverage_problem(run)
        if incomplete is not None:
            problems.append(incomplete)
    return problems


def _source_problems_from_extraction(result: dict[str, Any]) -> list[dict[str, str]]:
    problem_statuses = {
        "timeout",
        "budget_exhausted",
        "unavailable",
        "command_failed",
        "invalid_output",
        "invalid_signal_metadata",
        "worker_exception",
        "no_progress_timeout",
    }
    diagnostics = result.get("diagnostics", [])
    if not isinstance(diagnostics, list):
        diagnostics = []
    last_reason = {
        str(diag.get("source_name", "")): str(diag.get("status", ""))
        for diag in diagnostics
        if isinstance(diag, dict) and diag.get("status") in problem_statuses
    }
    timings = result.get("source_timings")
    if isinstance(timings, list):
        return [
            {
                "source_name": str(timing.get("source_name", "")),
                "status": "failed",
                "reason": last_reason.get(str(timing.get("source_name", "")), "not_ready"),
            }
            for timing in timings
            if isinstance(timing, dict)
            and timing.get("status") == "not_ready"
            and timing.get("terminal") not in {"unfinished", "portfolio_capacity_stop", "execution_safety_stop"}
        ]
    # Compatibility with older/injected adapters that expose diagnostics only.
    return [{"source_name": name, "status": "failed", "reason": reason} for name, reason in last_reason.items()]


def _remove_run_summary_key(report_store: WeeklyReportStore, key: str) -> None:
    """Drop *key* from run-summary.json, best effort, so an earlier run's facts are never read as this run's."""
    try:
        summary = report_store.read("run-summary.json", default=None)
        if isinstance(summary, dict) and key in summary:
            with report_store.transaction("source-failure-streaks"):
                report_store.write_json(
                    "run-summary.json", {name: value for name, value in summary.items() if name != key}
                )
    except (OSError, ValueError, RuntimeError):
        pass


def _attempted_source_runs(run: Any, source_runs: list[Any], operation_id: str) -> list[dict[str, Any]]:
    """Every source run of this operation: the capture ledger merged with the replay ledger, replay winning (#1125).

    A failed problem the run itself found is a failed attempt of its source
    and replaces what the ledgers say: an extraction that left the source not
    ready while replay served an earlier artifact (``reused``), and a Kirkus,
    PM or PW pass that failed while the source was served from its previous
    result. Its reason token is the run's problem code.
    """
    from autoscout.reporting import capture_ledger_from_workflow_log
    from autoscout.source_problem_notes import complete_source_runs

    capture_ledger, _status = capture_ledger_from_workflow_log(run.output_dir, operation_id)
    if capture_ledger is None:
        timing_runs = run.capture_timing.get("source_runs") if isinstance(run.capture_timing, dict) else None
        capture_ledger = timing_runs if isinstance(timing_runs, list) else None
    merged = complete_source_runs(capture_ledger, source_runs)
    failed_here = {
        str(problem.get("source_name", "")): problem
        for problem in run.operational_source_problems
        if problem.get("status") == "failed" and str(problem.get("source_name", "")).strip()
    }
    attempts = [
        record
        for record in merged
        if str(record.get("source_name", "")) not in failed_here or record.get("status") == "failed"
    ]
    kept = {str(record.get("source_name", "")) for record in attempts}
    for name, problem in failed_here.items():
        if name not in kept:
            code = "source_pass_failed" if name in run.pass_problem_names else problem.get("reason", "")
            attempts.append({"source_name": name, "status": "failed", "debug_metadata": {"safe_failure_status": code}})
    return attempts


def attention_source_problems(
    problems: list[dict[str, str]],
    streaks: Mapping[str, Any] | None,
    pass_problem_names: Iterable[str] = (),
) -> list[dict[str, str]]:
    """The source problems that make the run need attention (#1125).

    A failed or skipped source below the streak threshold is listed but asks
    for nothing. Every other problem does, as before: a source at or over the
    threshold, a problem a Kirkus, PM or PW pass reported, incomplete
    coverage, an unusual count, and any problem of a run without a streak view.
    """
    from autoscout.source_failure_streaks import tolerated_source_names

    tolerated = tolerated_source_names(streaks) - set(pass_problem_names)
    return [
        problem
        for problem in problems
        if not (problem.get("status") in {"failed", "skipped"} and problem.get("source_name", "") in tolerated)
    ]


def _deduplicate_source_problems(problems: list[dict[str, str]]) -> list[dict[str, str]]:
    unique: dict[str, dict[str, str]] = {}
    unnamed: list[dict[str, str]] = []
    for problem in problems:
        name = problem.get("source_name", "")
        if name:
            unique.setdefault(name, problem)
        elif problem not in unnamed:
            unnamed.append(problem)
    return [*unique.values(), *unnamed]


# ── Log persistence and sanitization ──────────────────────────────────────

DELETE_FROM_LOG = (
    "credentials",
    "credential",
    "api_key",
    "token",
    "password",
    "secret",
)


def _sanitize_log_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _sanitize_log_entry(value)
    if isinstance(value, list):
        return [_sanitize_log_value(item) for item in value]
    return value


def _sanitize_log_entry(entry: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in entry.items():
        if any(forbidden in str(key).lower() for forbidden in DELETE_FROM_LOG):
            continue
        if isinstance(value, str) and any(forbidden in value.lower() for forbidden in DELETE_FROM_LOG):
            continue
        clean[key] = _sanitize_log_value(value)
    # Defense in depth (issue #287): the key-name/generic-word sanitizer above
    # does not recognize credential *formats* embedded in otherwise-benign
    # strings (e.g. a Figma PAT surfaced through subprocess stderr). Run the
    # pattern-based redactor over the already key-sanitized structure too.
    redacted = redact_structure(clean)
    assert isinstance(redacted, dict)
    return redacted


# Canonical artifacts persisted by the weekly workflow. Scanned fail-closed
# (issue #287) before any publication step so a recognized credential shape
# blocks commit/push instead of being carried into repository history.
CANONICAL_ARTIFACT_NAMES = (
    "workflow-log.json",
    "codex-extraction-worklist.json",
    "codex-workflow-resume.json",
    "codex-replay-config.json",
    *tuple(artifact.name for artifact in WEEKLY_REPORT_ARTIFACTS),
)


def canonical_artifact_names(output_dir: Path) -> list[str]:
    """The canonical artifacts of *output_dir* the secret scans read, by their relative path.

    Candidate History kept one file per book is committed from the output
    directory too, so every one of its files is scanned (#1073), and so is
    the state directory's backend marker (#1093).
    """
    return [
        *CANONICAL_ARTIFACT_NAMES,
        BACKEND_MARKER_FILE,
        *(path.relative_to(output_dir).as_posix() for path in shard_files(output_dir)),
    ]


def _scan_output_dir_for_secrets(output_dir: Path) -> list[str]:
    """Scan canonical run artifacts for recognizable credential shapes.

    Returns the relative filenames that were flagged -- never the matched
    value itself, so the caller can build a sanitized diagnostic. Also scans
    every per-source capture/extraction artifact referenced by the worklist,
    since those are canonical too.
    """
    candidates = canonical_artifact_names(output_dir)
    worklist_path = output_dir / "codex-extraction-worklist.json"
    if worklist_path.is_file():
        worklist = read_json(worklist_path, default={})
        if isinstance(worklist, dict):
            for item in worklist.get("work_items", []):
                if not isinstance(item, dict):
                    continue
                for key in ("capture_file", "expected_extraction_file"):
                    name = str(item.get(key, ""))
                    if name:
                        candidates.append(name)

    flagged: list[str] = []
    seen: set[str] = set()
    for name in candidates:
        if name in seen:
            continue
        seen.add(name)
        path = output_dir / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if contains_secret(text):
            flagged.append(name)
    return flagged


def _scan_carried_for_secrets(output_dir: Path, carried: Iterable[str], already: Iterable[str]) -> list[str]:
    """The carried leftovers of a late failure (#1161) that hold a credential, by their path in *output_dir*.

    A carried file may be anything a person dropped into the run directory,
    binary too, so undecodable bytes are replaced rather than refused.
    """
    flagged: list[str] = []
    skip = set(already)
    for name in carried:
        path = output_dir / name
        if name in skip or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if contains_secret(text):
            flagged.append(name)
    return flagged


def _latest_start_saved_state(log: Iterable[dict[str, Any]]) -> bool:
    """Whether the latest start in *log* (from its last successful repository preflight on) logged a successful pipeline."""
    entries = list(log)
    starts = [
        index
        for index, entry in enumerate(entries)
        if entry.get("stage") == "repository-preflight" and entry.get("status") == "ok"
    ]
    if not starts:
        return False
    return any(entry.get("stage") == "pipeline" and entry.get("status") == "ok" for entry in entries[starts[-1] :])


def _log_set_asides(run: _WeeklyRunContext, repo_result: dict[str, Any]) -> None:
    """Log the residue of an early failure the preflight set aside (#1057)."""
    if repo_result.get("log_residue_set_aside"):
        # The copies' location stays out of the log; the doctor contract names it.
        residue_detail: dict[str, Any] = {"reason": "earlier_workflow_log_set_aside"}
        if isinstance(repo_result.get("log_residue_files"), list):
            residue_detail["files"] = [str(name) for name in repo_result["log_residue_files"]]
        run.log_stage("log-residue", "set_aside", detail=residue_detail)


def _note_carried_leftovers(run: _WeeklyRunContext, repo_result: dict[str, Any]) -> None:
    """Record that this run carries a late failure's leftovers awaiting publication (#1161).

    Every start that finds the run directory awaiting publication records it
    once, a restarted one included, so the run that publishes the leftovers,
    and any start that stops before, says so in its log and its morning
    report. The entry joins the run's log at once and is written with it:
    the log file is not touched before the update.
    """
    summary = repo_result.get("late_failure_leftovers")
    if not isinstance(summary, dict) or not summary.get("carried"):
        return
    detail: dict[str, Any] = {"reason": "earlier_late_failure_awaiting_publication"}
    detail.update({key: summary[key] for key in ("earlier_operation_date", "carried") if key in summary})
    run.log.append(
        stamp_entry(
            {"stage": LATE_FAILURE_LEFTOVERS_STAGE, "status": "carried", "at": _now_iso(), "detail": detail}, run.run_id
        )
    )


def _sanitize_repo_result(result: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    safe_keys = ("status", "branch", "upstream", "behind_count", "ahead_count", "dirty", "reasons")
    for key in safe_keys:
        if key in result:
            safe[key] = result[key]
    return safe


def _normalize_adapter_reason(result: dict[str, Any]) -> str:
    """Extract a safe, actionable reason string from an adapter result.

    The GitRepositoryAdapter's _inspect returns a ``reasons`` list (e.g.
    ``["dirty_working_tree"]``).  For logging and editor messaging we
    want a single deterministic reason token — never raw paths or Git
    detail."""
    reasons = result.get("reasons")
    if isinstance(reasons, list) and reasons:
        return str(reasons[0])
    single = result.get("reason")
    if isinstance(single, str) and single:
        return single
    return "blocked"


def _build_validated_artifact_allowlist(
    output_dir: Path, config_path: Path, operation_date: date | None = None
) -> set[Path]:
    """Compatibility delegate for the operation-evidence module."""
    return validated_artifact_allowlist(output_dir, config_path, operation_date)


def _persist_log(log_path: Path, entry: dict[str, Any]) -> None:
    try:
        existing: list[dict[str, Any]] = []
        if log_path.exists():
            existing = read_json(log_path, default=[])
            if not isinstance(existing, list):
                existing = []
        existing.append(entry)
        write_json(log_path, existing)
    except OSError:
        pass


def record_unexpected_error(
    output_dir: Path, config_path: Path, run_date: date, exc: BaseException, where: str, **extra: Any
) -> None:
    """Add the kind and location of an exception that ended the run to its log, for the doctor (#1158).

    The entry carries the operation's identity, so it belongs to this run and the next run's log
    replaces it. A run that died before it had an output directory leaves no log to add to.
    """
    if not output_dir.is_dir():
        return
    entry: dict[str, Any] = {
        "stage": UNEXPECTED_ERROR_STAGE,
        "status": "failed",
        "at": _now_iso(),
        "detail": {"where": where, **exception_facts(exc), **extra},
    }
    _persist_log(
        output_dir / "workflow-log.json",
        _sanitize_log_entry(stamp_entry(entry, _operation_id(config_path, run_date))),
    )


def _persist_final_log(log_path: Path, log: list[dict[str, Any]]) -> None:
    try:
        write_json(log_path, [_sanitize_log_entry(e) for e in log])
    except OSError:
        pass
