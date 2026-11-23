"""Deterministic orchestration of consequential recovery from doctor findings.

Issue #319: Diagnosis (``autoscout doctor``, read-only) and consequential
repair remain distinct phases — nothing in this module executes unless
explicitly confirmed.

The module maps a read-only ``DoctorReport`` to a ``RecoveryPlan`` and,
when authorized, executes the plan through the same canonical seams used
by the weekly workflow (#315) and local-only repairs (#318).

All editor-facing strings are Russian and follow the clean editor-output
contract (no secrets, no absolute local paths, no raw Git directives, no
stack traces, no tool-control markup).
"""

from __future__ import annotations

import contextlib
import enum
import io
import json
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from pathlib import Path
from typing import Any

from autoscout.doctor import DiagnosisClass
from autoscout.doctor import DoctorReport
from autoscout.doctor import DoctorSeams
from autoscout.doctor import diagnose
from autoscout.doctor import exit_code_for
from autoscout.doctor_repairs import _render_russian_report as _repairs_russian
from autoscout.doctor_repairs import _russian_repair_name
from autoscout.doctor_repairs import run_repairs
from autoscout.git_envelope import resolve_repo_root
from autoscout.russian_workflow import intervention_message
from autoscout.russian_workflow import render_workflow_result

# ── RecoveryAction enum ───────────────────────────────────────────────────


class RecoveryAction(str, enum.Enum):
    RESUME_WEEKLY = "resume-weekly"
    COMPLETE_LOCALLY = "complete-locally"
    NONE = "none"
    BLOCKED = "blocked"


# ── RecoveryPlan ──────────────────────────────────────────────────────────


@dataclass
class RecoveryPlan:
    action: RecoveryAction
    authorization_required: bool
    side_effects: list[str] = field(default_factory=list)
    local_repairs: list[str] = field(default_factory=list)
    summary: str = ""
    confirmation_question: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "authorization_required": self.authorization_required,
            "side_effects": self.side_effects,
            "local_repairs": self.local_repairs,
            "summary": self.summary,
            "confirmation_question": self.confirmation_question,
        }


# ── Deterministic plan mapping ────────────────────────────────────────────


def plan_recovery(report: DoctorReport) -> RecoveryPlan:
    """Deterministic mapping from diagnosis class to recovery plan.

    Every diagnosis class maps to exactly one RecoveryAction with stable
    Russian summary and confirmation question text.
    """
    if report.diagnosis == DiagnosisClass.RESUMABLE_OPERATION:
        return RecoveryPlan(
            action=RecoveryAction.RESUME_WEEKLY,
            authorization_required=True,
            side_effects=["network", "model", "git-publication"],
            summary=(
                "Продолжение прерванного поиска без повторного сбора источников; "
                "результаты будут проверены, обзор обновлён и сохранён."
            ),
            confirmation_question="Продолжить прерванный поиск?",
        )
    if report.diagnosis == DiagnosisClass.PERSISTENCE_FAILURE:
        # Resuming through the weekly workflow only avoids a recapture while
        # the operation identity is still current. For a stale operation the
        # workflow would start a fresh capture, so the summary's promise
        # ("no re-collection") would be false and #319 forbids the resume.
        if not report.facts.get("is_current_operation"):
            return RecoveryPlan(
                action=RecoveryAction.BLOCKED,
                authorization_required=False,
                side_effects=[],
                summary="",
                confirmation_question="",
            )
        return RecoveryPlan(
            action=RecoveryAction.RESUME_WEEKLY,
            authorization_required=True,
            side_effects=["network", "model", "git-publication"],
            summary=(
                "Повторное сохранение результатов в репозиторий через обычный рабочий процесс; "
                "повторный сбор источников не потребуется."
            ),
            confirmation_question="Повторить сохранение результатов?",
        )
    if report.diagnosis == DiagnosisClass.COMPLETED_REVIEW_REFRESH:
        repairs = ["rerun-health", "rerender-review", "open-review"]
        return RecoveryPlan(
            action=RecoveryAction.COMPLETE_LOCALLY,
            authorization_required=True,
            side_effects=["local-files", "browser"],
            local_repairs=repairs,
            summary=(
                "Обновление обзора из сохранённых результатов "
                "(проверка качества, пересборка обзора, открытие); "
                "без сети и без изменений в репозитории."
            ),
            confirmation_question="Обновить обзор?",
        )
    if report.diagnosis == DiagnosisClass.SOURCE_FAILURE:
        return RecoveryPlan(
            action=RecoveryAction.NONE,
            authorization_required=False,
            side_effects=[],
            summary="",
            confirmation_question="",
        )
    if report.diagnosis == DiagnosisClass.HEALTHY:
        return RecoveryPlan(
            action=RecoveryAction.NONE,
            authorization_required=False,
            side_effects=[],
            summary="",
            confirmation_question="",
        )
    if report.diagnosis == DiagnosisClass.RUN_IN_PROGRESS:
        # A running operation owns the workflow: there is nothing to
        # recover, only to wait for (issue #706).
        return RecoveryPlan(
            action=RecoveryAction.NONE,
            authorization_required=False,
            side_effects=[],
            summary="",
            confirmation_question="",
        )
    if report.diagnosis == DiagnosisClass.TOOL_INCOMPATIBLE:
        return RecoveryPlan(
            action=RecoveryAction.BLOCKED,
            authorization_required=False,
            side_effects=[],
            summary="",
            confirmation_question="",
        )
    if report.diagnosis == DiagnosisClass.REPOSITORY_BLOCKER:
        return RecoveryPlan(
            action=RecoveryAction.BLOCKED,
            authorization_required=False,
            side_effects=[],
            summary="",
            confirmation_question="",
        )
    if report.diagnosis in (DiagnosisClass.ARTIFACT_FAILURE, DiagnosisClass.STATE_STORE_FINDING):
        # A database that disagrees with its export is a data decision (which side wins), never a recovery.
        return RecoveryPlan(
            action=RecoveryAction.BLOCKED,
            authorization_required=False,
            side_effects=[],
            summary="",
            confirmation_question="",
        )
    return RecoveryPlan(
        action=RecoveryAction.BLOCKED,
        authorization_required=False,
        side_effects=[],
        summary="",
        confirmation_question="",
    )


# ── RecoverySeams ─────────────────────────────────────────────────────────


def _default_workflow_runner(
    config_path: Path,
    output_dir: Path,
    state_dir: Path | None,
    *,
    progress: Callable[[str], None] | None = None,
    extraction_progress: Callable[[int, int, int, int], None] | None = None,
    snapshot_progress: Callable[[str], None] | None = None,
) -> Any:
    """Default workflow runner: resolve repo root, build adapters, run.

    Follows ``_run_weekly_command`` in ``cli/weekly.py``: resolve the repo root
    from config, build production-shaped adapters, attach the progress
    printer, redirect the workflow's own stdout/stderr into a buffer, return
    the result.

    Note that the ``progress`` callable must already be bound to the real
    stdout by its caller (``file=`` captured before this redirect installs).
    Anything it prints to the *current* stdout lands in the throwaway buffer
    below and never reaches the editor.
    """
    from autoscout.weekly_workflow import bind_editorial_fit_shadow
    from autoscout.weekly_workflow import bind_isbn_completion
    from autoscout.weekly_workflow import bind_kirkus_precollection
    from autoscout.weekly_workflow import bind_original_edition_completion
    from autoscout.weekly_workflow import bind_pm_logline
    from autoscout.weekly_workflow import bind_pm_precollection
    from autoscout.weekly_workflow import bind_pw_precollection
    from autoscout.weekly_workflow import bind_russian_edition_completion
    from autoscout.weekly_workflow import bind_snapshot_progress_printer
    from autoscout.weekly_workflow import bind_work_age_completion
    from autoscout.weekly_workflow import run_weekly_workflow
    from autoscout.weekly_workflow import weekly_adapters

    repo_root = resolve_repo_root(config_path)
    if repo_root is None:
        repo_root = config_path.resolve().parent

    # Issue #452: construct adapters with the pre-#452 factory arguments, then
    # bind the live snapshot printer through the optional presentation setter
    # (``getattr`` defensive). Existing patched ``weekly_adapters`` factories
    # with the old exact signature keep working unchanged.
    adapters = weekly_adapters(
        repo_root,
        progress=progress,
        extraction_progress=extraction_progress,
    )
    bind_snapshot_progress_printer(adapters, snapshot_progress)
    bind_editorial_fit_shadow(adapters)
    bind_isbn_completion(adapters)
    bind_original_edition_completion(adapters)
    bind_work_age_completion(adapters)
    bind_russian_edition_completion(adapters)
    bind_kirkus_precollection(adapters)
    bind_pm_precollection(adapters)
    bind_pm_logline(adapters)
    bind_pw_precollection(adapters)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        result = run_weekly_workflow(config_path, output_dir, state_dir, adapters=adapters)
    return result


def _default_repairs_runner(
    config_path: Path,
    output_dir: Path,
    state_dir: Path | None,
    selected: list[str],
) -> tuple[Any, int]:
    """Default repairs runner: delegate to ``autoscout.doctor_repairs.run_repairs``."""
    return run_repairs(config_path, output_dir, state_dir, selected)


@dataclass
class RecoverySeams:
    """Injection points for recovery orchestration. Tests inject fakes."""

    doctor_seams: DoctorSeams | None = None
    workflow_runner: Callable[[Path, Path, Path | None], Any] | None = None
    repairs_runner: Callable[..., tuple[Any, int]] | None = None
    progress: Callable[[str], None] | None = None
    extraction_progress: Callable[[int, int, int, int], None] | None = None
    announce: Callable[[str], None] | None = None
    # Issue #452: receives one already built Russian heartbeat line per
    # progress snapshot; must be bound to the real stdout by the caller and
    # stays ``None`` (silent) in ``json_only`` mode.
    snapshot_progress: Callable[[str], None] | None = None


# ── RecoveryReport ────────────────────────────────────────────────────────


@dataclass
class RecoveryReport:
    phase: str  # "preview" | "apply" | "not-required"
    diagnosis: dict[str, Any] = field(default_factory=dict)
    plan: dict[str, Any] = field(default_factory=dict)
    confirmation_required: bool = False
    execution: dict[str, Any] = field(default_factory=dict)
    post_diagnosis: dict[str, Any] | None = None
    # Raw execution objects, carried so Russian rendering can reuse the
    # canonical renderers (``render_workflow_result`` / the repairs renderer)
    # instead of a degraded reconstruction. Never serialized — ``to_dict`` is
    # the machine-readable contract and must stay stable.
    workflow_result: Any = field(default=None, repr=False, compare=False)
    repairs_report: Any = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "phase": self.phase,
            "diagnosis": self.diagnosis,
            "plan": self.plan,
            "confirmation_required": self.confirmation_required,
            "execution": self.execution,
        }
        if self.post_diagnosis is not None:
            result["post_diagnosis"] = self.post_diagnosis
        return result


# ── Main entry point ──────────────────────────────────────────────────────


def run_recovery(
    config_path: Path,
    output_dir: Path,
    state_dir: Path | None,
    *,
    confirmed_by_editor: bool = False,
    seams: RecoverySeams | None = None,
) -> tuple[RecoveryReport, int]:
    """Orchestrate consequential recovery from doctor findings.

    1. Run read-only diagnosis.
    2. Compute the recovery plan.
    3. If blocked or nothing to do → phase "not-required", no execution ever.
    4. If not confirmed → phase "preview", return code 3, zero mutation.
    5. If confirmed → phase "apply", execute the planned recovery.
    """
    s = seams or RecoverySeams()

    # ── Step 1: Read-only diagnosis ───────────────────────────────────────
    doctor_s = s.doctor_seams or DoctorSeams()
    report = diagnose(config_path, output_dir, state_dir, seams=doctor_s)

    # ── Step 2: Compute recovery plan ─────────────────────────────────────
    plan = plan_recovery(report)

    # ── Step 3: Blocked / nothing ─────────────────────────────────────────
    if plan.action in (RecoveryAction.BLOCKED, RecoveryAction.NONE):
        return RecoveryReport(
            phase="not-required",
            diagnosis=report.to_dict(),
            plan=plan.to_dict(),
            confirmation_required=False,
        ), (1 if plan.action == RecoveryAction.BLOCKED else exit_code_for(report))

    # ── Step 4: Preview (not confirmed) ───────────────────────────────────
    if not confirmed_by_editor:
        return RecoveryReport(
            phase="preview",
            diagnosis=report.to_dict(),
            plan=plan.to_dict(),
            confirmation_required=True,
        ), 3

    # ── Step 5: Apply (confirmed) ─────────────────────────────────────────
    execution: dict[str, Any] = {}

    if plan.action == RecoveryAction.RESUME_WEEKLY:
        if s.announce is not None:
            s.announce(plan.action.value)
        runner = s.workflow_runner or (
            lambda c, o, st: _default_workflow_runner(
                c,
                o,
                st,
                progress=s.progress,
                extraction_progress=s.extraction_progress,
                snapshot_progress=s.snapshot_progress,
            )
        )
        result = runner(config_path, output_dir, state_dir)

        outcome_value = getattr(result, "outcome", "")
        if hasattr(outcome_value, "value"):
            outcome_value = outcome_value.value

        execution = {
            "action": "resume-weekly",
            "outcome": outcome_value,
            "failed_stage": getattr(result, "failed_stage", None),
            "review_available": getattr(result, "review_available", False),
        }

        # Evidence-backed results_saved comes from the shared typed snapshot.
        # Prefer persisted stamped evidence. A fake or compatibility runner
        # may return a trusted in-memory legacy log, which the evidence module
        # interprets without granting capture ownership.
        from autoscout.mvp_run_evidence import evidence_from_log
        from autoscout.mvp_run_evidence import inspect as inspect_run_evidence
        from autoscout.mvp_run_evidence import operation_id

        operation_date = report.facts.get("operation_date")
        effective_date = date.fromisoformat(operation_date) if isinstance(operation_date, str) else date.today()
        persisted_evidence = inspect_run_evidence(output_dir, config_path, effective_date)
        result_log = getattr(result, "log", [])
        if not isinstance(result_log, list):
            result_log = []
        result_evidence = evidence_from_log(
            result_log,
            operation_id(config_path, effective_date),
            effective_date.isoformat(),
            allow_legacy=True,
        )
        evidence = persisted_evidence if persisted_evidence.stamped_current else result_evidence
        execution["results_saved"] = evidence.persistence_ok

        # Run post-recovery diagnosis.
        post_report = diagnose(config_path, output_dir, state_dir, seams=doctor_s)

        exit_code: int
        if outcome_value == "intervention-required":
            exit_code = 1
        else:
            exit_code = exit_code_for(post_report)

        return RecoveryReport(
            phase="apply",
            diagnosis=report.to_dict(),
            plan=plan.to_dict(),
            confirmation_required=False,
            execution=execution,
            post_diagnosis=post_report.to_dict(),
            workflow_result=result,
        ), exit_code

    if plan.action == RecoveryAction.COMPLETE_LOCALLY:
        if s.announce is not None:
            s.announce(plan.action.value)
        repairs_runner = s.repairs_runner or _default_repairs_runner
        repairs_report, repairs_rc = repairs_runner(
            config_path,
            output_dir,
            state_dir,
            plan.local_repairs,
        )

        repairs_dicts: list[dict[str, Any]]
        if hasattr(repairs_report, "repairs"):
            repairs_dicts = list(repairs_report.repairs)
        elif isinstance(repairs_report, dict):
            repairs_dicts = repairs_report.get("repairs", [])
        else:
            repairs_dicts = list(getattr(repairs_report, "repairs", []))

        post_diag: dict[str, Any] | None = None
        if hasattr(repairs_report, "post_diagnosis"):
            post_diag = dict(repairs_report.post_diagnosis)
        elif isinstance(repairs_report, dict):
            post_diag = dict(repairs_report.get("post_diagnosis", {}))

        execution = {
            "action": "complete-locally",
            "repairs": repairs_dicts,
            "repairs_exit_code": repairs_rc,
        }

        return RecoveryReport(
            phase="apply",
            diagnosis=report.to_dict(),
            plan=plan.to_dict(),
            confirmation_required=False,
            execution=execution,
            post_diagnosis=post_diag,
            repairs_report=repairs_report,
        ), repairs_rc

    # Should not be reachable, but fail safe.
    return RecoveryReport(
        phase="not-required",
        diagnosis=report.to_dict(),
        plan=plan.to_dict(),
        confirmation_required=False,
    ), 1


# ── Russian rendering ─────────────────────────────────────────────────────


def render_recovery_russian(
    report: RecoveryReport,
    *,
    workflow_result: Any = None,
    repairs_report: Any = None,
) -> str:
    """Produce concise Russian editor output from a RecoveryReport.

    Preview: diagnosis verdict line, plan.summary, plan.confirmation_question.
    Apply RESUME_WEEKLY: reuse ``render_workflow_result`` (→ includes results-saved evidence
    and intervention on failure).
    Apply COMPLETE_LOCALLY: reuse ``_repairs_russian``.
    Not-required: diagnosis verdict + recommended_action + editor_question (exactly one each).

    The explicit ``workflow_result``/``repairs_report`` keywords override, but
    default to the objects ``run_recovery`` carried on the report, so callers
    (including the CLI) get the canonical renderers — with translated repair
    names, blocked reasons, and the Level 3 action/question — rather than the
    degraded reconstruction below.
    """
    diagnosis = report.diagnosis
    plan = report.plan
    action = plan.get("action", "")

    if workflow_result is None:
        workflow_result = report.workflow_result
    if repairs_report is None:
        repairs_report = report.repairs_report

    if report.phase == "preview":
        lines: list[str] = [str(diagnosis.get("verdict", ""))]
        summary = plan.get("summary", "")
        if summary:
            lines.append(summary)
        question = plan.get("confirmation_question", "")
        if question:
            lines.append(question)
        return "\n".join(lines)

    if report.phase == "apply":
        if action == "resume-weekly":
            if workflow_result is not None:
                outcome_value = getattr(workflow_result, "outcome", "")
                if hasattr(outcome_value, "value"):
                    outcome_value = outcome_value.value

                if outcome_value == "intervention-required":
                    failed_stage = getattr(workflow_result, "failed_stage", "") or ""
                    return intervention_message(failed_stage)

                return render_workflow_result(workflow_result, include_progress=False)

            # Fallback: build from execution dict when raw result not provided.
            execution = report.execution
            lines = []
            outcome = execution.get("outcome", "")
            if outcome == "intervention-required":
                failed_stage = execution.get("failed_stage", "") or ""
                lines.append(intervention_message(failed_stage))
            else:
                if execution.get("results_saved"):
                    lines.append("Результаты сохранены.")
                if execution.get("review_available"):
                    lines.append("Обзор готов.")
            return "\n".join(lines)

        if action == "complete-locally":
            if repairs_report is not None:
                return _repairs_russian(repairs_report)
            # Fallback for a report built without the raw repairs object.
            # Still translate repair names — a raw English identifier must
            # never reach the editor.
            repairs = report.execution.get("repairs", [])
            lines = []
            for r in repairs:
                name = _russian_repair_name(str(r.get("name", "")))
                outcome = r.get("outcome", "")
                if outcome == "changed":
                    lines.append(f"✓ {name}: выполнено.")
                elif outcome == "already-complete":
                    lines.append(f"✓ {name}: уже выполнено ранее.")
                elif outcome in ("blocked", "failed"):
                    lines.append(f"✗ {name}: заблокировано.")
                elif outcome == "not-executed":
                    lines.append(f"— {name}: не выполнялось.")
            return "\n".join(lines)

    # Not-required
    lines = [str(diagnosis.get("verdict", ""))]
    rec_action = diagnosis.get("recommended_action", "")
    if rec_action:
        lines.append(rec_action)
    question = diagnosis.get("editor_question", "")
    if question:
        lines.append(question)
    return "\n".join(lines)


def render_recovery_json(report: RecoveryReport) -> str:
    """Serialize the recovery report to JSON."""
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
