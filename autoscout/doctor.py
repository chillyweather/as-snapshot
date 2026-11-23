"""Deterministic read-only doctor diagnostics for the AutoScout weekly state.

Provides a stable, typed diagnostic report that tells the editor *why*
AutoScout is blocked or what safe next action is available — without
writing any files, removing locks, fetching/pulling/committing/pushing,
accessing the network, or invoking any model.

All editor-facing strings are Russian and follow the clean editor-output
contract (no secrets, no absolute local paths, no raw Git directives, no
stack traces, no tool-control markup).
"""

from __future__ import annotations

import enum
import json
import math
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from pathlib import Path
from typing import Any

from autoscout.doctor_state_store import finding_for
from autoscout.doctor_state_store import inspection_facts
from autoscout.doctor_state_store import look_at_state_store
from autoscout.doctor_state_store import state_store_line
from autoscout.git_envelope import CANONICAL_BRANCH
from autoscout.git_envelope import CANONICAL_REMOTE_URL
from autoscout.git_envelope import GUARD_STALE_SECONDS
from autoscout.git_envelope import resolve_repo_root
from autoscout.late_failure_leftovers import LATE_FAILURE_LEFTOVERS_STAGE
from autoscout.late_failure_leftovers import LeftoverScope
from autoscout.late_failure_leftovers import leftover_scope
from autoscout.model_client import exec_help_command
from autoscout.progress_snapshot import PROGRESS_SNAPSHOT_FILE
from autoscout.progress_snapshot import snapshot_stale_after_seconds
from autoscout.progress_snapshot import summarize_snapshot_rows
from autoscout.russian_workflow import snapshot_progress_line
from autoscout.source_failure_streaks import broken_entries
from autoscout.source_failure_streaks import needs_attention
from autoscout.source_failure_streaks import run_view
from autoscout.source_failure_streaks import view_lines_ru
from autoscout.weekly_progress import KIRKUS_PROGRESS_SNAPSHOT_FILE
from autoscout.weekly_report import WeeklyReportStore

# ── Stable diagnosis classes ──────────────────────────────────────────────


class DiagnosisClass(str, enum.Enum):
    TOOL_INCOMPATIBLE = "tool-incompatible"
    RESUMABLE_OPERATION = "resumable-operation"
    SOURCE_FAILURE = "source-failure"
    ARTIFACT_FAILURE = "artifact-failure"
    REPOSITORY_BLOCKER = "repository-blocker"
    PERSISTENCE_FAILURE = "persistence-failure"
    COMPLETED_REVIEW_REFRESH = "completed-review-refresh"
    RUN_IN_PROGRESS = "run-in-progress"
    STATE_STORE_FINDING = "state-store-finding"
    HEALTHY = "healthy"


# Public exit-code constants.
EXIT_HEALTHY = 0
EXIT_RECOVERABLE = 2
EXIT_BLOCKED = 1


# ── DoctorReport ──────────────────────────────────────────────────────────


@dataclass
class DoctorReport:
    """A single deterministic diagnostic report.

    Editor-facing strings (``verdict``, ``recommended_action``,
    ``editor_question``) are Russian and non-technical.

    ``review_available`` is True when the review HTML artifact exists and a
    completed outcome suggests it should be current.

    ``results_saved`` is True **only** when structured workflow-log evidence
    shows the terminal outcome was successfully published to the canonical
    repository.
    """

    diagnosis: DiagnosisClass
    verdict: str
    recommended_action: str = ""
    editor_question: str = ""
    facts: dict[str, Any] = field(default_factory=dict)
    review_available: bool = False
    results_saved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "diagnosis": self.diagnosis.value,
            "verdict": self.verdict,
            "recommended_action": self.recommended_action,
            "editor_question": self.editor_question,
            "facts": self.facts,
            "review_available": self.review_available,
            "results_saved": self.results_saved,
        }


# ── Read-only fact probes ─────────────────────────────────────────────────


def _secret_scan_flagged(output_dir: Path) -> list[str]:
    """Return filenames with credential patterns — read-only defense in depth."""
    from autoscout.secret_redaction import contains_secret
    from autoscout.weekly_workflow import canonical_artifact_names

    flagged: list[str] = []
    seen: set[str] = set()
    for name in canonical_artifact_names(output_dir):
        if name in seen:
            continue
        seen.add(name)
        path = output_dir / name
        if not path.is_file():
            continue
        try:
            if contains_secret(path.read_text(encoding="utf-8")):
                flagged.append(name)
        except OSError:
            pass
    return flagged


def _codex_probe(timeout_seconds: float = 5.0) -> dict[str, Any]:
    """Probe the codex CLI deterministically without invoking any model.

    Returns a sanitized fact dict — never exposes absolute paths or raw OS
    error text.  On success: ``{"codex_available": True, "codex_version": "…"}``.
    On failure: ``{"codex_available": False, "error": "<stable_token>"}``.

    Never calls ``codex exec`` or any network-requiring subcommand.
    """
    executable = shutil.which("codex")
    if executable is None:
        return {"codex_available": False, "error": "codex_not_on_path"}

    try:
        proc = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return {"codex_available": False, "error": "codex_version_timeout"}
    except OSError:
        return {"codex_available": False, "error": "codex_os_error"}

    if proc.returncode != 0:
        return {"codex_available": False, "error": "codex_version_nonzero_exit"}

    version = proc.stdout.strip() or proc.stderr.strip()
    if not version:
        return {"codex_available": False, "error": "codex_version_unparseable"}

    # Compatibility is decided separately, by the flags the extraction
    # actually needs (see ``_codex_compat_probe``); this function's contract
    # is deliberately narrow — availability only, ``--version`` only.
    return {"codex_available": True, "codex_version": version}


# Flags ``ProductionCodexAdapter`` passes to ``codex exec``. An installation
# that does not advertise them cannot run the extraction, whatever version it
# reports, so these are the real compatibility contract.
CODEX_REQUIRED_EXEC_FLAGS = ("--output-last-message", "--sandbox", "--skip-git-repo-check")


def _codex_compat_probe(timeout_seconds: float = 5.0) -> dict[str, Any]:
    """Check the installed Codex accepts the flags the extraction requires.

    Reads ``codex exec --help`` — help text only. Never passes a prompt,
    never runs an extraction, never invokes a model, never touches the
    network. This is why compatibility lives here rather than in
    :func:`_codex_probe`, whose contract is ``--version`` only.

    Prefers a capability check over a version floor because the repository
    documents no minimum Codex version: an installation that accepts the
    required flags works regardless of what it calls itself.

    Deliberately conservative — an inconclusive probe (missing executable,
    timeout, non-zero exit, empty help text) reports compatible, so a working
    installation is never blocked merely because its help output could not be
    parsed. Only positive evidence that a required flag is absent marks it
    incompatible.
    """
    executable = shutil.which("codex")
    if executable is None:
        return {"codex_compatible": True}

    try:
        proc = subprocess.run(
            exec_help_command(executable),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (subprocess.TimeoutExpired, OSError):
        return {"codex_compatible": True}

    if proc.returncode != 0:
        return {"codex_compatible": True}

    help_text = f"{proc.stdout}\n{proc.stderr}"
    if not help_text.strip():
        return {"codex_compatible": True}

    missing = [flag for flag in CODEX_REQUIRED_EXEC_FLAGS if flag not in help_text]
    if missing:
        return {"codex_compatible": False, "codex_missing_flags": missing}
    return {"codex_compatible": True}


def _is_weekly_review_present(output_dir: Path) -> bool:
    store = WeeklyReportStore(output_dir)
    return not store.transaction_in_progress() and store.path("weekly-review.html").is_file()


# ── Injection seam types ──────────────────────────────────────────────────


@dataclass
class DoctorSeams:
    """Deterministic injection points for ``diagnose``."""

    today: Callable[[], date] = field(default_factory=lambda: date.today)
    # Repository inspection: callable that takes validated_artifact_allowlist
    # and returns the standard inspection dict (the same shape
    # GitRepositoryAdapter._inspect produces). Tests inject a fake.
    repo_inspect: Callable[[set[Path] | None], dict[str, Any]] | None = None
    # Codex CLI probe: callable that returns the same dict shape
    # _codex_probe does. Tests inject a fake.
    codex_probe: Callable[[], dict[str, Any]] | None = None
    # Codex compatibility probe: returns the dict shape _codex_compat_probe
    # does. Separate from codex_probe because availability and compatibility
    # are distinct diagnoses (#317).
    codex_compat_probe: Callable[[], dict[str, Any]] | None = None
    guard_status: Callable[[], dict[str, Any]] | None = None
    # Expected repository envelope for the default inspector. Defaults to the
    # canonical branch/remote so a checkout pointing elsewhere is not
    # diagnosed as safe; tests that build a temporary remote override it.
    expected_branch: str = CANONICAL_BRANCH
    expected_remote_url: str | None = CANONICAL_REMOTE_URL


def _default_repo_inspect(
    repo_path: Path,
    expected_branch: str | None,
    expected_remote_url: str | None,
    output_dir: Path | None = None,
    leftover_scope: LeftoverScope | None = None,
) -> Callable[[set[Path] | None], dict[str, Any]]:
    """Build a genuinely local read-only inspection seam.

    Uses the refactored ``inspect_local`` of ``GitRepositoryAdapter`` so
    no fetch or guard acquisition is performed. A lone unstaged
    ``workflow-log.json`` of *output_dir* is not a blocker: the next run
    sets it aside before its own preflight judges the tree (#1057). Nor are
    the proven leftovers of a run that failed late, judged for the operation
    *leftover_scope* names (#1161).
    """
    from autoscout.git_envelope import GitRepositoryAdapter

    adapter = GitRepositoryAdapter(
        repo_path,
        expected_branch=expected_branch,
        expected_remote_url=expected_remote_url,
        run_log_path=None if output_dir is None else output_dir / "workflow-log.json",
    )
    adapter.set_leftover_scope(leftover_scope)

    def _inspect(validated_artifact_allowlist: set[Path] | None = None) -> dict[str, Any]:
        return adapter.inspect_local(validated_artifact_allowlist=validated_artifact_allowlist)

    return _inspect


# ── Russian output helpers ────────────────────────────────────────────────


def render_russian(report: DoctorReport) -> str:
    """Produce concise Russian editor output from a DoctorReport.

    The output contains: the one-line verdict; a review-availability line
    when relevant («Обзор доступен» / «Обзор недоступен»); a results-saved
    line ONLY when ``report.results_saved`` is evidenced; when the report
    has a ``recommended_action``, exactly one line with that action; and
    when blocked (has ``editor_question``), exactly one concise question
    line.  Never exposes secrets, absolute paths, raw Git directives, stack
    traces, or tool-control markup.
    """
    lines: list[str] = [report.verdict]

    code_gate = report.facts.get("code_gate")
    if isinstance(code_gate, dict):
        from autoscout.operator_notice import ran_on_earlier_code_text

        lines.append(ran_on_earlier_code_text(code_gate.get("reason")))

    state_store = report.facts.get("state_store")
    if isinstance(state_store, dict):
        lines.append(
            state_store_line(state_store, with_export=report.diagnosis is not DiagnosisClass.STATE_STORE_FINDING)
        )

    leftovers = report.facts.get("late_failure_leftovers")
    if isinstance(leftovers, dict) and not report.facts.get("late_failure_leftovers_in_verdict"):
        lines.append(late_failure_leftovers_line(leftovers))

    pm_status = report.facts.get("pm_deals")
    if isinstance(pm_status, dict):
        lines.append(pm_status_line(pm_status))
    pw_status = report.facts.get("pw_issue")
    if isinstance(pw_status, dict):
        lines.append(pw_status_line(pw_status))

    russian_edition = report.facts.get("russian_edition")
    if isinstance(russian_edition, dict) and russian_edition.get("line"):
        lines.append(str(russian_edition["line"]))

    unexpected = report.facts.get("unexpected_error")
    if isinstance(unexpected, dict) and unexpected.get("exception_type"):
        lines.append(unexpected_error_line(unexpected))

    if report.review_available:
        lines.append("Обзор доступен.")
    elif report.diagnosis not in (
        DiagnosisClass.HEALTHY,
        DiagnosisClass.REPOSITORY_BLOCKER,
        DiagnosisClass.TOOL_INCOMPATIBLE,
        DiagnosisClass.RUN_IN_PROGRESS,
        DiagnosisClass.STATE_STORE_FINDING,
    ):
        # Only mention review unavailability for recovery/completed states.
        # A running operation owns the output directory, so its review says
        # nothing about the run in flight.
        lines.append("Обзор недоступен.")

    if report.results_saved:
        lines.append("Результаты сохранены.")

    if report.recommended_action:
        lines.append(report.recommended_action)

    if report.editor_question:
        lines.append(report.editor_question)

    return "\n".join(lines)


def render_json_report(report: DoctorReport) -> str:
    """Serialize the report to JSON with the same verdict and recommended action."""
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2)


# ── Diagnosis engine ──────────────────────────────────────────────────────


def diagnose(
    config_path: Path,
    output_dir: Path,
    state_dir: Path | None = None,
    *,
    seams: DoctorSeams | None = None,
) -> DoctorReport:
    """Run the deterministic read-only diagnosis.

    Resolves paths as given; makes no repo-root assumption beyond what the
    config and output paths imply.

    Returns a ``DoctorReport``. Never writes files, removes locks, fetches,
    pulls, commits, pushes, or invokes any model.

    Whatever the diagnosis, the report also carries the last PM deals pass
    (issue #946) when PM is configured beside *config_path*: its attempt and
    result, read from their files without taking the pass lock.
    """
    notes: dict[str, Any] = {}
    report = _diagnose(config_path, output_dir, state_dir, seams=seams, notes=notes)
    report = _with_state_store(report, config_path, state_dir)
    _add_late_failure_leftovers(report, output_dir, notes.get("late_failure_leftovers"))
    from autoscout.pm_precollection import last_pass_status

    pm_status = last_pass_status(config_path)
    if pm_status is not None:
        report.facts["pm_deals"] = pm_status
    from autoscout.pw_precollection import last_pass_status as last_pw_pass_status

    pw_status = last_pw_pass_status(config_path)
    if pw_status is not None:
        report.facts["pw_issue"] = pw_status
    if report.diagnosis is not DiagnosisClass.RUN_IN_PROGRESS:
        earlier_code = _ran_on_earlier_code(output_dir)
        if earlier_code is not None:
            report.facts["code_gate"] = earlier_code
        from autoscout.russian_edition_health import doctor_line as russian_edition_line
        from autoscout.russian_edition_health import last_outcome as russian_edition_outcome

        russian_edition = russian_edition_outcome(output_dir)
        if russian_edition is not None and russian_edition.has_problem:
            # Advisory (#1181): named for the operator, never a change of the verdict.
            report.facts["russian_edition"] = {
                "status": russian_edition.status,
                "failure": russian_edition.failure,
                "sources": [asdict(problem) for problem in russian_edition.sources],
                "line": russian_edition_line(russian_edition),
            }
        unexpected = last_unexpected_error(output_dir)
        if unexpected is not None:
            report.facts["unexpected_error"] = unexpected
            if report.diagnosis is DiagnosisClass.HEALTHY:
                # A run that died before it left any work reads as "never started"; the crash is the finding.
                report = DoctorReport(
                    diagnosis=DiagnosisClass.ARTIFACT_FAILURE,
                    verdict="Последний поиск остановился на непредвиденной ошибке.",
                    recommended_action="Свяжитесь с Димой для диагностики: подробности в журнале запуска.",
                    editor_question="Связаться с Димой?",
                    facts=report.facts,
                    review_available=report.review_available,
                    results_saved=report.results_saved,
                )
    return report


def _ran_on_earlier_code(output_dir: Path) -> dict[str, Any] | None:
    """The last run's code-gate entry when it ran on the last code that passed here (#1159); else ``None``."""
    from autoscout.code_gate_fallback import STATUS_RAN_LAST_PASSED
    from autoscout.mvp_run_evidence import read_sanitized_log
    from autoscout.operator_notice import safe_reason

    log, _ = read_sanitized_log(output_dir)
    for entry in reversed(log):
        if entry.get("stage") != "code-gate":
            continue
        detail = entry.get("detail")
        if entry.get("status") != STATUS_RAN_LAST_PASSED or not isinstance(detail, dict):
            return None
        return {"status": STATUS_RAN_LAST_PASSED, "reason": safe_reason(detail.get("reason"))}
    return None


def _add_late_failure_leftovers(report: DoctorReport, output_dir: Path, pending: Any) -> None:
    """Say what happens to the leftovers of a run that failed late (#1161), as a fact and one line.

    *pending* is what the repository inspection found still unpublished: the
    next run carries and publishes it, so it is not a blocker. Otherwise the
    latest run's log says whether that run carried an earlier run's leftovers.
    """
    if isinstance(pending, dict):
        report.facts["late_failure_leftovers"] = {"status": "pending", **pending}
        return
    from autoscout.mvp_run_evidence import read_sanitized_log

    log, _legacy = read_sanitized_log(output_dir)
    operation = log[-1].get("operation_id") if log else None
    entry = next(
        (
            entry
            for entry in reversed(log)
            if operation
            and entry.get("operation_id") == operation
            and entry.get("stage") == LATE_FAILURE_LEFTOVERS_STAGE
        ),
        None,
    )
    if entry is not None and entry.get("status") == "carried":
        found = entry.get("detail")
        detail: dict[str, Any] = found if isinstance(found, dict) else {}
        published = any(
            item.get("operation_id") == operation
            and item.get("stage") == "terminal"
            and str(item.get("status", "")).startswith("completed/")
            for item in log
        )
        report.facts["late_failure_leftovers"] = {
            "status": "published" if published else "carried",
            "earlier_operation_date": str(detail.get("earlier_operation_date", "")),
        }


_SAFE_FACT = re.compile(r"[\w.:/ <>-]{1,200}")


def last_unexpected_error(output_dir: Path) -> dict[str, str] | None:
    """The exception that ended the last run, when nothing was logged after it (#1158).

    A later entry in the same log means the run went on after that, so the exception no longer
    describes where it stands; the next run replaces the log. Only the kind, the location, and
    where it was caught are kept; the traceback is in the journal and the per-run log.
    """
    from autoscout.mvp_run_evidence import read_sanitized_log
    from autoscout.unexpected_error import UNEXPECTED_ERROR_STAGE

    log, _legacy = read_sanitized_log(output_dir)
    if not log or log[-1].get("stage") != UNEXPECTED_ERROR_STAGE:
        return None
    detail = log[-1].get("detail")
    if not isinstance(detail, dict):
        return None
    facts = {
        key: str(detail[key])
        for key in ("exception_type", "exception_location", "where")
        if isinstance(detail.get(key), str) and _SAFE_FACT.fullmatch(detail[key])
    }
    if "exception_type" not in facts:
        return None
    facts["at"] = str(log[-1].get("at", ""))
    return facts


def unexpected_error_line(facts: dict[str, Any]) -> str:
    """One Russian line naming the exception type that stopped the last run; never a path or a traceback."""
    return f"Последняя остановка: непредвиденная ошибка {facts['exception_type']}. Подробности в журнале запуска."


def _with_state_store(report: DoctorReport, config_path: Path, state_dir: Path | None) -> DoctorReport:
    """Add the State Store to *report*: the backend always, and a database that disagrees with its export (#1076).

    The look is read-only on both backends. It is skipped while a run is
    writing, because the export is written late in the run. A database and
    export that disagree become the diagnosis only when nothing else needs the
    editor first, that is, when the diagnosis would otherwise be healthy; any
    other diagnosis keeps its single action and carries the finding as a fact.
    """
    running = report.diagnosis is DiagnosisClass.RUN_IN_PROGRESS
    inspection = look_at_state_store(config_path, state_dir, compare=not running)
    facts = {**report.facts, "state_store": inspection_facts(inspection)}
    finding = finding_for(inspection, state_dir)
    if finding is None or report.diagnosis is not DiagnosisClass.HEALTHY:
        report.facts = facts
        return report
    if finding.command:
        facts["state_store"]["recommended_command"] = finding.command
    if finding.files_against_git:
        facts["state_store"]["files_against_git"] = finding.files_against_git
    return DoctorReport(
        diagnosis=DiagnosisClass.STATE_STORE_FINDING,
        verdict=finding.verdict,
        recommended_action=finding.recommended_action,
        editor_question=finding.editor_question,
        facts=facts,
        review_available=report.review_available,
        results_saved=report.results_saved,
    )


_PM_REASONS_RU = {
    "pm_credentials_missing": "нет данных для входа в почту",
    "pm_login_failed": "почта не приняла вход",
    "pm_connection_failed": "нет соединения с почтовым сервером",
    "pm_timeout": "почтовый сервер не ответил вовремя",
    "pm_folder_unavailable": "нет папки с письмами PM",
    "pm_imap_error": "ошибка почтового сервера",
    "pm_declaration": "ошибка в описании источника PM",
    "pm_no_deals_parsed": "письма прочитаны, но ни одна сделка не разобрана",
    "pm_pass_in_progress": "уже идёт другой сбор PM",
    "pm_time_budget": "истекло время сбора",
    "unexpected_error": "непредвиденная ошибка",
}


def late_failure_leftovers_line(facts: dict[str, Any]) -> str:
    """One Russian line on the unsaved results of a run that failed late (#1161), with its date."""
    from autoscout.operator_notice import late_failure_leftovers_line as carried_line

    return carried_line(facts.get("earlier_operation_date"), published=facts.get("status") == "published")


def pm_status_line(status: dict[str, Any]) -> str:
    """One Russian line on the last PM deals pass: status, date and counts, never a title."""
    if not status.get("configured"):
        return "Сбор PM: описание источника не читается."
    attempt = status.get("attempt") if isinstance(status.get("attempt"), dict) else None
    result = status.get("result") if isinstance(status.get("result"), dict) else None
    if attempt is None:
        line = "Сбор PM ещё не проводился."
    else:
        day = str(attempt.get("attempted_at", ""))[:10] or "дата неизвестна"
        counts = attempt.get("counts") if isinstance(attempt.get("counts"), dict) else {}
        state = attempt.get("status")
        if state in {"collected", "partial"}:
            done = "собран" if state == "collected" else "собран частично"
            line = (
                f"Сбор PM {day}: {done}, писем прочитано {counts.get('messages_read', 0)}, "
                f"сделок разобрано {counts.get('deals_parsed', 0)}, новых {counts.get('deals_new', 0)}."
            )
        elif state == "started":
            line = f"Сбор PM начат {day} и не завершён: идёт сейчас или был прерван."
        else:
            code = str(attempt.get("reason_code", ""))
            line = f"Сбор PM {day}: не удался — {_PM_REASONS_RU.get(code, code or 'причина неизвестна')}."
    if result is not None:
        line += (
            f" В последнем результате сделок: {result.get('deals_in_result', 0)}"
            f" (от {str(result.get('collected_at', ''))[:10]})."
        )
    elif attempt is not None:
        line += " Результата сбора PM нет."
    return line


_PW_REASONS_RU = {
    "pw_credentials_missing": "нет данных для входа в почту",
    "pw_login_failed": "почта не приняла вход",
    "pw_connection_failed": "нет соединения с сервером",
    "pw_timeout": "сервер не ответил вовремя",
    "pw_imap_error": "ошибка почтового сервера",
    "pw_declaration_invalid": "ошибка в описании источника PW",
    "pw_subscriber_link_rejected": "подписка не открыла выпуск (подтвердите аккаунт в цифровой библиотеке PW)",
    "pw_download_failed": "выпуск не скачался",
    "pw_pdf_too_large": "файл выпуска больше допустимого",
    "pw_pdf_parse_failed": "файл выпуска не читается",
    "pw_page_unreadable": "страница с рецензиями не читается",
    "pw_zero_nonfiction_rows": "в выпуске не найдено ни одной рецензии на нон-фикшн",
    "pw_pass_in_progress": "уже идёт другой сбор PW",
    "pw_time_budget": "истекло время сбора",
    "unexpected_error": "непредвиденная ошибка",
}


def pw_status_line(status: dict[str, Any]) -> str:
    """One Russian line on the last PW issue pass: status, dates and counts, never a title."""
    if not status.get("configured"):
        return "Сбор PW: описание источника не читается."
    attempt = status.get("attempt") if isinstance(status.get("attempt"), dict) else None
    result = status.get("result") if isinstance(status.get("result"), dict) else None
    if attempt is None:
        line = "Сбор PW ещё не проводился."
    else:
        day = str(attempt.get("attempted_at", ""))[:10] or "дата неизвестна"
        state = attempt.get("status")
        collected = attempt.get("issues_collected") if isinstance(attempt.get("issues_collected"), list) else []
        failed = attempt.get("issues_failed") if isinstance(attempt.get("issues_failed"), list) else []
        if state == "no_new_issue":
            line = f"Сбор PW {day}: нового выпуска нет."
        elif state in {"collected", "partial"}:
            line = f"Сбор PW {day}: выпусков собрано {len(collected)}, рецензий {attempt.get('nonfiction_rows', 0)}"
            if failed or attempt.get("stop_reason"):
                code = str(attempt.get("stop_reason") or (failed[0].get("reason_code", "") if failed else ""))
                line += f"; не собрано: {len(failed)} — {_PW_REASONS_RU.get(code, code or 'причина неизвестна')}"
            line += "."
        elif state == "started":
            line = f"Сбор PW начат {day} и не завершён: идёт сейчас или был прерван."
        else:
            code = str(attempt.get("reason_code", ""))
            line = f"Сбор PW {day}: не удался — {_PW_REASONS_RU.get(code, code or 'причина неизвестна')}."
    if result is not None:
        line += (
            f" В результате выпусков: {result.get('issues_in_result', 0)}, рецензий: {result.get('rows_in_result', 0)}"
            f" (последний выпуск {result.get('newest_issue_date') or '—'})."
        )
        if result.get("retry_issues"):
            line += f" Ждут повтора: {result['retry_issues']}."
    elif attempt is not None:
        line += " Результата сбора PW нет."
    if status.get("unreadable"):
        line += " Файлы сбора PW повреждены: " + ", ".join(str(name) for name in status["unreadable"]) + "."
    return line


def _diagnose(
    config_path: Path,
    output_dir: Path,
    state_dir: Path | None = None,
    *,
    seams: DoctorSeams | None = None,
    notes: dict[str, Any] | None = None,
) -> DoctorReport:
    s = seams or DoctorSeams()
    _output_dir = output_dir
    _today = s.today()

    # Resolve the repo root from the config path (parent of .git).
    repo_root = resolve_repo_root(config_path)

    # Ensure output dir exists for artifact reads (read-only — we never mkdir).
    if not _output_dir.is_dir():
        return _no_run_report(False)

    # ── Precedence (a): Repository blocker ─────────────────────────────────
    repo_inspect = s.repo_inspect
    if repo_inspect is None and repo_root is not None:
        # Validate against the same canonical branch *and* remote as the
        # weekly adapter — a clean ``main`` pointing at the wrong remote is
        # not a safe repository (#316 expected-repository check).
        repo_inspect = _default_repo_inspect(
            repo_root,
            s.expected_branch,
            s.expected_remote_url,
            _output_dir,
            leftover_scope(config_path, _output_dir, state_dir),
        )

    from autoscout.mvp_run_evidence import inspect as inspect_run_evidence

    evidence = inspect_run_evidence(_output_dir, config_path, _today)
    is_current = evidence.capture_current
    validated_allowlist: set[Path] | None = set(evidence.allowlist) if is_current else None

    # Guard status probe (read-only) — delegate to git_envelope as single source of truth.
    guard_facts: dict[str, Any] = {}
    if s.guard_status is not None:
        guard_facts = s.guard_status()
    elif repo_root is not None:
        from autoscout.git_envelope import GitRepositoryAdapter

        guard_facts = GitRepositoryAdapter(repo_root).guard_status()

    # A live lock means another AutoScout process owns the run right now.
    # That is a running operation, not a blocker (issue #706): report the
    # live stage from the progress snapshot instead of escalating. Lock
    # ownership is still inspected before the clean-repository shortcut
    # below, so an unreadable or dead lock keeps its #317 behavior.
    if guard_facts.get("guard_pid_alive"):
        return _run_in_progress_report(_output_dir, guard_facts)

    if repo_inspect is not None:
        repo_status = repo_inspect(validated_allowlist)
        # Pending leftovers are named only where the next preflight will set them aside (#1161).
        if notes is not None and repo_status.get("status") == "ok" and repo_status.get("late_failure_leftovers"):
            notes["late_failure_leftovers"] = repo_status["late_failure_leftovers"]
        repo_blocker_report = _classify_repo_blocker(repo_status, guard_facts)
        if repo_blocker_report is not None:
            return repo_blocker_report

    # The evidence module has already filtered legacy and stale entries,
    # selected the latest terminal, and selected the latest repository
    # finalization as persistence truth.
    log = list(evidence.log)
    persistence_ok = evidence.persistence_ok
    persistence_failed = evidence.persistence_failed
    has_completed_outcome = evidence.has_completed_outcome
    terminal_status = evidence.terminal_status
    has_any_work_done = evidence.has_work
    is_resumable = evidence.resumable

    # ── Precedence (b): Tool incompatibility ──────────────────────────────
    needs_resume = evidence.needs_resume

    if needs_resume:
        codex = (s.codex_probe or _codex_probe)()
        if codex.get("codex_available"):
            # Available but possibly too old to accept the required flags.
            compat = (s.codex_compat_probe or _codex_compat_probe)()
            if not compat.get("codex_compatible", True):
                codex = {
                    **codex,
                    "codex_available": False,
                    "error": "codex_incompatible_version",
                    **{k: v for k, v in compat.items() if k != "codex_compatible"},
                }
        if not codex.get("codex_available"):
            # An installed-but-incompatible Codex reads differently to the
            # editor than a missing one, and needs a different single action.
            incompatible = codex.get("error") == "codex_incompatible_version"
            if incompatible:
                verdict = (
                    "AutoScout не может продолжить — установленная версия Codex устарела. "
                    "Обновите Codex и повторите попытку."
                )
                recommended_action = "Обновите Codex до последней версии, затем повторите запуск."
                editor_question = "Версия Codex устарела. Проверить ещё раз после обновления?"
            else:
                verdict = (
                    "AutoScout не может продолжить — утилита Codex недоступна. "
                    "Обновите или установите Codex и повторите попытку."
                )
                recommended_action = "Установите или обновите Codex, затем повторите запуск."
                editor_question = "Codex недоступен. Проверить ещё раз после обновления?"
            return DoctorReport(
                diagnosis=DiagnosisClass.TOOL_INCOMPATIBLE,
                verdict=verdict,
                recommended_action=recommended_action,
                editor_question=editor_question,
                facts=_merge_guard_facts(dict(codex), guard_facts),
                review_available=False,
                results_saved=False,
            )

    # ── Precedence (c): Resumable operation ───────────────────────────────
    # Only an operation that is both current *and* unfinished may resume; a
    # completed one falls through to persistence/review/healthy below.
    if is_resumable:
        review_available = _is_weekly_review_present(_output_dir)
        return DoctorReport(
            diagnosis=DiagnosisClass.RESUMABLE_OPERATION,
            verdict=(
                "Обнаружен прерванный поиск, который можно безопасно продолжить. "
                "Предыдущие результаты сохранены, повторный сбор источников не требуется."
            ),
            recommended_action="Продолжите прерванный поиск (запустите восстановление).",
            editor_question="Продолжить прерванный поиск?",
            facts={
                "is_current_operation": True,
                "operation_date": evidence.operation_date,
                "guard": guard_facts if guard_facts else {},
                "log_stages": [e.get("stage") for e in log],
            },
            review_available=review_available,
            results_saved=False,
        )

    # ── Precedence (d): Persistence failure ───────────────────────────────
    if persistence_failed and has_completed_outcome:
        review_available = _is_weekly_review_present(_output_dir)
        # A retry is only offerable while the operation is still current;
        # for a stale operation the single safe action is escalation, so the
        # verdict must not invite a repeat that recovery will refuse.
        if is_current:
            persistence_action = "Проверьте состояние репозитория и повторите сохранение."
            persistence_question = "Попробовать сохранить результаты ещё раз?"
        else:
            persistence_action = "Свяжитесь с Димой, чтобы сохранить результаты вручную."
            persistence_question = "Связаться с Димой для сохранения результатов?"
        return DoctorReport(
            diagnosis=DiagnosisClass.PERSISTENCE_FAILURE,
            verdict=(
                "Результаты поиска готовы, но не удалось сохранить их в репозиторий. "
                "Данные не потеряны — диагностика сохранена."
            ),
            recommended_action=persistence_action,
            editor_question=persistence_question,
            facts={
                # Recovery must know whether the operation identity is still
                # valid: resuming a stale operation would recapture sources
                # (#319 forbids resuming a stale or mismatched operation).
                "is_current_operation": is_current,
                "operation_date": evidence.operation_date,
                "log_stages": [e.get("stage") for e in log],
                "finalize_status": list(evidence.repository_finalize_statuses),
                "guard": guard_facts if guard_facts else {},
            },
            review_available=review_available,
            results_saved=False,
        )

    # ── Precedence (e): Completed run needing review refresh ──────────────
    # Completed outcome: treat explicit persistence_ok *or* lack of any
    # finalize entries (older run without repo adapter) as "results may be
    # saved".  We never claim saved without evidence.
    if has_completed_outcome:
        review_available = _is_weekly_review_present(_output_dir)
        artifacts_ok = _validate_required_artifacts(_output_dir)
        # Missing or malformed required artifacts are a corruption risk, not
        # something a review refresh can fix — that stays Level 3 (#316).
        if not artifacts_ok:
            return DoctorReport(
                diagnosis=DiagnosisClass.ARTIFACT_FAILURE,
                verdict="Поиск завершён, но результаты повреждены или неполны — требуется диагностика.",
                recommended_action="Свяжитесь с Димой для диагностики артефактов.",
                editor_question="Связаться с Димой для диагностики?",
                facts={
                    "review_present": review_available,
                    "artifacts_valid": False,
                    "guard": guard_facts if guard_facts else {},
                    "terminal_status": terminal_status,
                },
                review_available=review_available,
                results_saved=persistence_ok,
            )
        if not review_available:
            # Only claim persistence when finalize evidence proves it.
            verdict = (
                "Результаты поиска сохранены, но обзор требует обновления."
                if persistence_ok
                else "Поиск завершён, но обзор требует обновления."
            )
            return DoctorReport(
                diagnosis=DiagnosisClass.COMPLETED_REVIEW_REFRESH,
                verdict=verdict,
                recommended_action="Обновите обзор (запустите пересборку).",
                editor_question="Обновить обзор?",
                facts={
                    "review_present": review_available,
                    "artifacts_valid": artifacts_ok,
                    "guard": guard_facts if guard_facts else {},
                    "terminal_status": terminal_status,
                },
                review_available=review_available,
                results_saved=persistence_ok,
            )

    # ── 4a: Incomplete run without completed outcome ──────────────────────
    if has_any_work_done and not has_completed_outcome:
        # Secret scan first — credential patterns block everything.
        flagged = _secret_scan_flagged(_output_dir)
        if flagged:
            return DoctorReport(
                diagnosis=DiagnosisClass.ARTIFACT_FAILURE,
                verdict=(
                    "Обнаружена проблема в артефактах поиска — возможна утечка учётных данных. "
                    "Дальнейшая работа заблокирована."
                ),
                recommended_action="Свяжитесь с Димой для безопасной очистки артефактов.",
                editor_question="Связаться с Димой для диагностики?",
                facts={"flagged_artifacts": flagged, "log_stages": [e.get("stage") for e in log]},
            )

        failed_stage = evidence.failed_stage
        # Capture/codex failures → source-failure (recoverable, re-run).
        if failed_stage in ("capture", "codex"):
            return DoctorReport(
                diagnosis=DiagnosisClass.SOURCE_FAILURE,
                verdict=("Поиск не завершён — произошёл сбой на этапе сбора данных. Можно запустить поиск заново."),
                recommended_action="Запустите еженедельный поиск повторно.",
                editor_question="Запустить поиск заново?",
                facts={
                    "failed_stage": failed_stage,
                    "log_stages": [e.get("stage") for e in log],
                    "guard": guard_facts if guard_facts else {},
                },
                review_available=False,
                results_saved=False,
            )
        # Missing/malformed required artifacts → artifact-failure.
        artifact_failure = _check_artifact_failure_missing(_output_dir, log)
        if artifact_failure is not None:
            return artifact_failure
        # Other failed stages (finalize, pipeline, review, health, etc.) → artifact-failure.
        return DoctorReport(
            diagnosis=DiagnosisClass.ARTIFACT_FAILURE,
            verdict="Поиск не завершён — требуется диагностика артефактов.",
            recommended_action="Свяжитесь с Димой для диагностики артефактов.",
            editor_question="Связаться с Димой?",
            facts={
                "failed_stage": failed_stage,
                "log_stages": [e.get("stage") for e in log],
                "guard": guard_facts if guard_facts else {},
            },
            review_available=False,
            results_saved=False,
        )

    # ── Precedence (g): Source failure/degradation ────────────────────────
    if has_completed_outcome:
        source_failure = _classify_source_failure(_output_dir, persistence_ok)
        if source_failure is not None:
            return source_failure

    # ── Completed run that is fully healthy ───────────────────────────────
    if has_completed_outcome:
        review_available = _is_weekly_review_present(_output_dir)
        readiness_facts = _readiness_facts(_output_dir)
        source_notes, source_facts = source_streak_notes(_output_dir)
        verdict = "AutoScout в порядке — последний поиск завершён успешно, обзор доступен."
        return DoctorReport(
            diagnosis=DiagnosisClass.HEALTHY,
            verdict=f"{verdict} {source_notes}" if source_notes else verdict,
            recommended_action="Обзор готов. Откройте или обновите weekly-review.html.",
            facts={
                "guard": guard_facts if guard_facts else {},
                "readiness": readiness_facts,
                "review_available": review_available,
                **source_facts,
            },
            review_available=review_available,
            results_saved=persistence_ok,
        )

    # ── No log of today's operation, a late failure's results unsaved (#1161) ─
    pending = notes.get("late_failure_leftovers") if notes is not None else None
    if isinstance(pending, dict):
        return _unsaved_results_report(pending)

    # ── No prior evidence — healthy, nothing to do ────────────────────────
    return _no_run_report(_output_dir.is_dir())


# ── Internal helpers ──────────────────────────────────────────────────────


def _unsaved_results_report(pending: dict[str, Any]) -> DoctorReport:
    """The verdict when a run that failed late left its results unsaved and nothing ran since (#1161)."""
    day = pending.get("earlier_operation_date")
    run = f"поиск {day}" if isinstance(day, str) and day else "прошлый поиск"
    return DoctorReport(
        diagnosis=DiagnosisClass.HEALTHY,
        verdict=(
            f"AutoScout в порядке — {run} не успел сохранить результаты; следующий поиск сохранит их, ничего не удаляя."
        ),
        recommended_action="Дождитесь ночного поиска или скажите «Запусти поиск».",
        facts={"has_run_evidence": False, "late_failure_leftovers_in_verdict": True},
    )


def _no_run_report(output_dir_exists: bool) -> DoctorReport:
    return DoctorReport(
        diagnosis=DiagnosisClass.HEALTHY,
        verdict=("AutoScout в порядке — еженедельный поиск ещё не запускался. Чтобы начать, скажите «Запусти поиск»."),
        recommended_action="Запустите поиск командой «Запусти поиск».",
        facts={"has_run_evidence": False, "output_dir_exists": output_dir_exists},
    )


def _merge_guard_facts(base: dict[str, Any], guard: dict[str, Any]) -> dict[str, Any]:
    if guard:
        base["guard"] = guard
    return base


# Filesystem mtime granularity plus the gap between the guard probe and the
# snapshot read: a snapshot written during the live run has an mtime at or
# after the lock's, so this tolerance only absorbs measurement jitter.
_GUARD_AGE_TOLERANCE_SECONDS = 2.0


def _guard_age_seconds(guard_facts: dict[str, Any]) -> float | None:
    """Read the guard lock's age from the guard facts, if reported."""
    value = guard_facts.get("guard_age_seconds")
    if value is None:
        return None
    try:
        age = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(age) or age < 0:
        return None
    return age


def _snapshot_describes_live_run(
    payload: dict[str, Any] | None,
    age: float | None,
    guard_age: float | None,
) -> bool:
    """Decide whether the on-disk snapshot can belong to the live run.

    The running process acquires the guard before any stage publishes, so a
    snapshot written at or after the lock belongs to this run and one
    written before it belongs to a previous run (the file is never deleted
    between runs). ``guard_age`` missing means the guard seam provided no
    age at all — no cross-check is possible, so the payload is attributed
    to the live run unless it is both finished and stale: a stage the run
    already left behind cannot be the running stage.
    """
    if payload is None or age is None:
        return False
    if guard_age is None:
        return not (str(payload.get("status", "")) == "finished" and age > snapshot_stale_after_seconds())
    return age <= guard_age + _GUARD_AGE_TOLERANCE_SECONDS


def _leftover_lock_report(
    base_facts: dict[str, Any],
    payload: dict[str, Any] | None,
    age: float | None,
    guard_age: float | None,
    *,
    hang: bool = False,
) -> DoctorReport:
    """Escalate the live-lock corner where liveness proves nothing.

    A guard PID reported alive but with no current-run progress and a lock
    past the stale-guard threshold is indistinguishable from a recycled or
    foreign PID left behind by a crash. The editor-facing contract is the
    old #317 Level 3 corner: exactly one action and one question, so the
    remove-stale-lock repair (which rightly refuses a live PID) is not the
    only answer the editor ever gets.
    """
    if hang:
        verdict = (
            "Поиск числится выполняющимся, но давно не сообщает о прогрессе — "
            "возможно, процесс завис или это остаток прерванного запуска."
        )
    else:
        verdict = "Поиск числится выполняющимся, но данных о прогрессе нет — возможно, это остаток прерванного запуска."
    facts: dict[str, Any] = {
        **base_facts,
        "snapshot_present": payload is not None,
        "snapshot_age_seconds": age,
        "snapshot_stale": age is not None and age > snapshot_stale_after_seconds(),
        "guard_age_seconds": guard_age,
        "guard_stale": True,
    }
    if payload is not None:
        facts["stage"] = str(payload.get("stage", "") or "")
        facts["status"] = str(payload.get("status", "") or "")
    return DoctorReport(
        diagnosis=DiagnosisClass.REPOSITORY_BLOCKER,
        verdict=verdict,
        recommended_action="Свяжитесь с Димой для диагностики блокировки.",
        editor_question="Связаться с Димой для диагностики блокировки?",
        facts=facts,
        review_available=False,
        results_saved=False,
    )


def _read_progress_snapshot(
    output_dir: Path, filename: str = PROGRESS_SNAPSHOT_FILE
) -> tuple[dict[str, Any] | None, float | None]:
    """Read the live progress-snapshot payload and its age in seconds.

    Read-only: a missing, unreadable, or malformed snapshot is not an error
    here — the run is still running, only its stage is unknown. Returns
    ``(payload, age)`` with ``None`` placeholders when unreadable.
    """
    path = output_dir / filename
    try:
        mtime = path.stat().st_mtime
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    try:
        age = max(0.0, time.time() - float(mtime))
    except (TypeError, ValueError, OverflowError):
        age = None
    if age is not None and not math.isfinite(age):
        age = None
    return payload, age


def _format_duration_ru(seconds: float) -> str:
    """Format a stage elapsed/budget time as short Russian text."""
    try:
        total = float(seconds)
    except (TypeError, ValueError, OverflowError):
        total = 0.0
    if not math.isfinite(total):
        total = 0.0
    total = max(0, round(total))
    if total < 90:
        return f"{total} с"
    minutes = round(total / 60)
    if minutes < 90:
        return f"{minutes} мин"
    hours, rest = divmod(minutes, 60)
    if rest:
        return f"{hours} ч {rest} мин"
    return f"{hours} ч"


def _format_snapshot_age_ru(age: float) -> str:
    """Format a snapshot age as short Russian text."""
    if age < 15:
        return "только что"
    if age < 90:
        return f"{max(0, round(age))} с назад"
    minutes = max(0, round(age / 60))
    if minutes < 90:
        return f"{minutes} мин назад"
    hours, rest = divmod(minutes, 60)
    if rest:
        return f"{hours} ч {rest} мин назад"
    return f"{hours} ч назад"


def _run_in_progress_report(output_dir: Path, guard_facts: dict[str, Any]) -> DoctorReport:
    """Report a live AutoScout run as running, never as blocked (issue #706).

    The verdict comes from the live snapshot: the stage heartbeat line
    (stage label plus finished/running/not-started counts), the stage
    elapsed time against the stage budget when the snapshot carries one,
    and the last snapshot time. A snapshot older than the derived staleness
    threshold flags a possible hang in the verdict, but the diagnosis stays
    ``run-in-progress`` with no escalation question while the run shows
    live progress.

    The snapshot file is advisory, gitignored, and never deleted between
    runs, and its payload carries no absolute timestamp — only elapsed
    time — so it cannot self-identify as belonging to the live run. The
    attribution is therefore done against the guard: the running process
    acquires the lock before any stage publishes, so a snapshot written
    after the lock belongs to this run and one written before it belongs
    to a previous run. Two corners stay honest (code-review findings on
    #706):

    * a prior-run (or missing) snapshot with a fresh guard reads as a run
      that just started — no fresh progress data yet, not the previous
      run's stage;
    * no live sign of progress together with an *old* lock (past the
      stale-guard threshold) escalates with a question again: the PID
      liveness verdict cannot distinguish a hung process from a recycled
      or foreign PID after a crash, so the editor gets the Level 3 way
      out instead of a permanent wait.
    """
    base_facts: dict[str, Any] = {"guard": guard_facts if guard_facts else {}}
    payload, age = _read_progress_snapshot(output_dir)
    guard_age = _guard_age_seconds(guard_facts)
    guard_stale = guard_age is not None and guard_age > float(GUARD_STALE_SECONDS)

    if payload is None or not _snapshot_describes_live_run(payload, age, guard_age):
        if guard_stale:
            return _leftover_lock_report(base_facts, payload, age, guard_age)
        return DoctorReport(
            diagnosis=DiagnosisClass.RUN_IN_PROGRESS,
            verdict="Поиск выполняется, но свежих данных о прогрессе пока нет.",
            recommended_action="Дождитесь завершения текущего этапа.",
            editor_question="",
            facts={
                **base_facts,
                "snapshot_present": False,
                # True when the file on disk predates the live guard: it
                # describes a previous run (never deleted between runs).
                "prior_run_snapshot": payload is not None,
                "guard_age_seconds": guard_age,
            },
        )

    # From here the snapshot describes the live run.
    assert payload is not None
    kirkus, kirkus_age = _read_progress_snapshot(output_dir, KIRKUS_PROGRESS_SNAPSHOT_FILE)
    if (
        kirkus is not None
        and kirkus.get("stage") == "kirkus-precollection"
        and kirkus.get("status") == "running"
        and _snapshot_describes_live_run(kirkus, kirkus_age, guard_age)
    ):
        payload = {**payload, "kirkus": kirkus}
    else:
        kirkus = None
    rows = payload.get("rows")
    overview = summarize_snapshot_rows(rows if isinstance(rows, list) else [])
    stage = str(payload.get("stage", "") or "")
    try:
        elapsed = float(payload.get("elapsed_seconds", 0.0) or 0.0)
    except (TypeError, ValueError, OverflowError):
        elapsed = 0.0
    if not math.isfinite(elapsed):
        elapsed = 0.0
    budget = payload.get("stage_budget_seconds")
    try:
        budget_seconds = float(budget) if budget is not None else None
    except (TypeError, ValueError, OverflowError):
        budget_seconds = None
    if budget_seconds is not None and (not math.isfinite(budget_seconds) or budget_seconds <= 0):
        budget_seconds = None
    stale = age is not None and age > snapshot_stale_after_seconds()
    if stale and guard_stale:
        # A run older than the stale-guard threshold that stopped
        # publishing: the only live-sign liveness gives is a PID that may
        # be recycled or foreign. Restore the Level 3 escape hatch instead
        # of a permanent wait (code-review finding on #706).
        return _leftover_lock_report(base_facts, payload, age, guard_age, hang=True)

    parts = [f"Поиск выполняется. {snapshot_progress_line(payload)}"]
    time_part = f"Этап идёт {_format_duration_ru(elapsed)}"
    if budget_seconds is not None:
        time_part += f" из {_format_duration_ru(budget_seconds)}"
    parts.append(time_part + ".")
    if kirkus is not None:
        kirkus_time = f"Сбор Kirkus идёт {_format_duration_ru(kirkus.get('elapsed_seconds', 0))}"
        if kirkus.get("stage_budget_seconds") is not None:
            kirkus_time += f" из {_format_duration_ru(kirkus['stage_budget_seconds'])}"
        parts.append(kirkus_time + ".")
        if kirkus_age is not None:
            parts.append(f"Kirkus: обновлено {_format_snapshot_age_ru(kirkus_age)}.")
    if age is None:
        parts.append("Время последнего обновления неизвестно.")
    elif stale:
        parts.append(f"Нет обновлений {_format_snapshot_age_ru(age)} — возможно, этап завис.")
    else:
        parts.append(f"Обновлено {_format_snapshot_age_ru(age)}.")

    facts: dict[str, Any] = {
        **base_facts,
        "snapshot_present": True,
        "stage": stage,
        "status": str(payload.get("status", "") or ""),
        "finished": overview["finished"],
        "running": overview["running"],
        "not_started": overview["not_started"],
        "elapsed_seconds": max(0.0, elapsed),
        "snapshot_age_seconds": age,
        "snapshot_stale": stale,
    }
    if budget_seconds is not None:
        facts["stage_budget_seconds"] = budget_seconds
    if kirkus is not None:
        kirkus_rows = kirkus.get("rows")
        kirkus_overview = summarize_snapshot_rows(kirkus_rows if isinstance(kirkus_rows, list) else [])
        facts["kirkus"] = {
            **kirkus,
            **{key: kirkus_overview[key] for key in ("finished", "running", "not_started")},
            "snapshot_age_seconds": kirkus_age,
            "snapshot_stale": kirkus_age is not None and kirkus_age > snapshot_stale_after_seconds(),
        }
    return DoctorReport(
        diagnosis=DiagnosisClass.RUN_IN_PROGRESS,
        verdict=" ".join(parts),
        recommended_action="Дождитесь завершения текущего этапа.",
        editor_question="",
        facts=facts,
        review_available=False,
        results_saved=False,
    )


def _classify_repo_blocker(
    repo_status: dict[str, Any],
    guard_facts: dict[str, Any],
) -> DoctorReport | None:
    """Classify repository inspection result. Returns a DoctorReport if blocked.

    Technical reason tokens live in ``facts`` only — the editor-facing
    Russian verdict is always plain non-technical Russian.
    """
    status = repo_status.get("status", "unsafe")

    reasons = repo_status.get("reasons", [])
    if not isinstance(reasons, list):
        reasons = [str(reasons)] if reasons else []

    # Lock ownership is inspected *before* the clean-repository shortcut.
    # ``inspect_local`` never looks at the lockfile, so a live or unreadable
    # lock must not be reported healthy merely because Git is clean — #317
    # includes lock ownership in the repository diagnosis. A *live* lock
    # never reaches this function: ``diagnose`` reports it as
    # ``run-in-progress`` with the live snapshot (issue #706).
    if guard_facts.get("guard_exists"):
        # A lock whose ownership cannot be established (unreadable, or no
        # liveness verdict at all) is ambiguous, and unsafe ambiguity is a
        # blocker rather than something to clear silently.
        if guard_facts.get("guard_error") or guard_facts.get("guard_pid_alive") is None:
            return DoctorReport(
                diagnosis=DiagnosisClass.REPOSITORY_BLOCKER,
                verdict="Запуск поиска заблокирован — не удалось определить состояние блокировки.",
                recommended_action="Свяжитесь с Димой для диагностики блокировки.",
                editor_question="Связаться с Димой для диагностики блокировки?",
                facts={"repo_status": status, "guard": guard_facts, "reasons": reasons},
            )

    if status == "ok":
        return None

    return DoctorReport(
        diagnosis=DiagnosisClass.REPOSITORY_BLOCKER,
        verdict="Запуск поиска заблокирован — репозиторий не в безопасном состоянии.",
        recommended_action="Устраните проблему в репозитории и повторите запуск.",
        editor_question="Связаться с Димой для диагностики репозитория?",
        facts={"repo_status": status, "guard": guard_facts, "reasons": reasons},
    )


def _validate_required_artifacts(output_dir: Path) -> bool:
    """Validate the health artifact set through the report store."""
    snapshot = WeeklyReportStore(output_dir).snapshot("health")
    if not snapshot.coherent:
        return False
    pilot = snapshot.json("pilot-status.json", {})
    run_summary = snapshot.json("run-summary.json", {})
    return isinstance(pilot, dict) and isinstance(run_summary, dict) and snapshot.coherent


def _check_artifact_failure_missing(output_dir: Path, log: list[dict[str, Any]]) -> DoctorReport | None:
    """Check required report artifacts through the shared store."""
    snapshot = WeeklyReportStore(output_dir).snapshot("health")
    missing = list(snapshot.missing)
    malformed = list(snapshot.malformed) + list(snapshot.date_inconsistent)

    if missing or malformed or snapshot.transaction_in_progress or snapshot.read_inconsistent:
        return DoctorReport(
            diagnosis=DiagnosisClass.ARTIFACT_FAILURE,
            verdict="Артефакты поиска повреждены или отсутствуют. Требуется диагностика.",
            recommended_action="Свяжитесь с Димой для восстановления артефактов.",
            editor_question="Связаться с Димой?",
            facts={
                "missing_artifacts": missing,
                "malformed_artifacts": malformed,
                "read_inconsistent": snapshot.read_inconsistent,
                "log_stages": [e.get("stage") for e in log],
            },
        )

    return None


def _classify_source_failure(output_dir: Path, persistence_ok: bool = False) -> DoctorReport | None:
    """Check for source failures in a completed run. Returns report if degraded.

    ``persistence_ok`` must come from structured repository-finalize evidence;
    it is never inferred from the run merely having completed.
    """
    snapshot = WeeklyReportStore(output_dir).snapshot("health")
    source_runs = snapshot.json("source-runs.json", [])
    if not snapshot.coherent or not isinstance(source_runs, list):
        return None

    failed = []
    skipped = []
    for src in source_runs:
        if not isinstance(src, dict):
            continue
        status = str(src.get("status", ""))
        name = str(src.get("source_name", ""))
        if status == "failed" and name:
            failed.append(name)
        elif status == "skipped" and name:
            skipped.append(name)

    view = run_view(snapshot.json("run-summary.json", {}))
    if view is not None:
        return _classify_source_streaks(output_dir, view, failed, skipped, persistence_ok)

    if not failed and not skipped:
        return None

    parts = []
    if failed:
        parts.append(f"недоступны: {', '.join(failed)}")
    if skipped:
        parts.append(f"пропущены: {', '.join(skipped)}")

    review_available = _is_weekly_review_present(output_dir)

    return DoctorReport(
        diagnosis=DiagnosisClass.SOURCE_FAILURE,
        verdict=f"Поиск завершён, но есть проблемы с источниками — {'; '.join(parts)}. Остальные результаты доступны.",
        recommended_action="Проверьте доступность источников или отключите проблемные.",
        editor_question="Проверить источники или продолжить с доступными результатами?",
        facts={
            "failed_sources": failed,
            "skipped_sources": skipped,
            "failed_count": len(failed),
            "skipped_count": len(skipped),
        },
        review_available=review_available,
        results_saved=persistence_ok,
    )


def _classify_source_streaks(
    output_dir: Path, view: dict[str, Any], failed: list[str], skipped: list[str], persistence_ok: bool
) -> DoctorReport | None:
    """The source verdict of a run that counted failure streaks (#1125).

    A source at or over the threshold, or a failure of more than half of the
    sources the run attempted, makes the run a source failure (exit 2) and is
    named first. A source below the threshold, and a recovery, are only
    listed: the run stays healthy (exit 0) and the healthy verdict carries
    them (``source_streak_notes``).
    """
    if not needs_attention(view):
        return None
    # The view also counts the sources that stopped at capture, which source-runs.json does not hold.
    failed = [str(entry.get("source_name", "")) for entry in view["failing"] if entry.get("status") == "failed"]
    skipped = [str(entry.get("source_name", "")) for entry in view["failing"] if entry.get("status") == "skipped"]
    lines = view_lines_ru(view)
    if view.get("widespread"):
        verdict = (
            f"Поиск завершён, но сбор источников массово не удался. {' '.join(lines)} Остальные результаты доступны."
        )
        action = "Проверьте источники: сбой затронул большинство из них сразу."
        question = "Проверить источники или продолжить с доступными результатами?"
    elif broken_entries(view):
        verdict = f"Поиск завершён, но есть сломанные источники. {' '.join(lines)} Остальные результаты доступны."
        action = "Проверьте сломанные источники: почините их или отключите."
        question = "Проверить сломанные источники или продолжить с доступными результатами?"
    else:
        verdict = (
            f"Поиск завершён, но отдельный проход сбора дал сбой. {' '.join(lines)} Остальные результаты доступны."
        )
        action = "Проверьте проход сбора Kirkus, PM или PW, названный выше."
        question = "Проверить проход сбора или продолжить с доступными результатами?"
    return DoctorReport(
        diagnosis=DiagnosisClass.SOURCE_FAILURE,
        verdict=verdict,
        recommended_action=action,
        editor_question=question,
        facts={
            "failed_sources": failed,
            "skipped_sources": skipped,
            "failed_count": len(failed),
            "skipped_count": len(skipped),
            **_streak_facts(view),
        },
        review_available=_is_weekly_review_present(output_dir),
        results_saved=persistence_ok,
    )


def _streak_facts(view: dict[str, Any]) -> dict[str, Any]:
    return {
        "streak_threshold": view.get("threshold"),
        "widespread_failure": bool(view.get("widespread")),
        "broken_sources": [str(entry.get("source_name", "")) for entry in broken_entries(view)],
        "source_streaks": list(view.get("failing") or []),
        "recovered_sources": list(view.get("recovered") or []),
        "pass_problem_sources": list(view.get("pass_problem_sources") or []),
    }


def source_streak_notes(output_dir: Path) -> tuple[str, dict[str, Any]]:
    """The listed-only source facts of a healthy completed run: a Russian sentence and the facts, or nothing."""
    snapshot = WeeklyReportStore(output_dir).snapshot("health")
    if not snapshot.coherent:
        return "", {}
    view = run_view(snapshot.json("run-summary.json", {}))
    if view is None or not (view.get("failing") or view.get("recovered")):
        return "", {}
    failing = [entry for entry in view.get("failing", []) if isinstance(entry, dict)]
    return " ".join(view_lines_ru(view)), {
        "failed_sources": [str(entry.get("source_name", "")) for entry in failing if entry.get("status") == "failed"],
        "skipped_sources": [str(entry.get("source_name", "")) for entry in failing if entry.get("status") == "skipped"],
        **_streak_facts(view),
    }


def _readiness_facts(output_dir: Path) -> dict[str, Any]:
    """Extract editor-facing readiness facts from artifacts (read-only)."""
    snapshot = WeeklyReportStore(output_dir).snapshot("health")
    pilot = snapshot.json("pilot-status.json", {})
    run_summary = snapshot.json("run-summary.json", {})

    if not isinstance(pilot, dict):
        pilot = {}
    if not isinstance(run_summary, dict):
        run_summary = {}

    return {
        "ready_for_editor": pilot.get("ready_for_editor"),
        "editor_facing_count": run_summary.get("editor_facing_count", pilot.get("editor_facing_count", 0)),
        "new_count": (run_summary.get("update_delta_counts", {}) or {}).get("new", 0),
        "updated_count": (
            (run_summary.get("update_delta_counts", {}) or {}).get("upgraded", 0)
            + (run_summary.get("update_delta_counts", {}) or {}).get("changed", 0)
        ),
    }


# ── Exit-code helpers ─────────────────────────────────────────────────────

_HEALTHY_CLASSES: frozenset[DiagnosisClass] = frozenset(
    {
        DiagnosisClass.HEALTHY,
        # A normally running operation needs no action: the doctor answers a
        # status question, it does not gate a launch (issue #706).
        DiagnosisClass.RUN_IN_PROGRESS,
    }
)
_RECOVERABLE_CLASSES: frozenset[DiagnosisClass] = frozenset(
    {
        DiagnosisClass.RESUMABLE_OPERATION,
        DiagnosisClass.COMPLETED_REVIEW_REFRESH,
        DiagnosisClass.PERSISTENCE_FAILURE,
        DiagnosisClass.SOURCE_FAILURE,
    }
)
_BLOCKED_CLASSES: frozenset[DiagnosisClass] = frozenset(
    {
        DiagnosisClass.REPOSITORY_BLOCKER,
        DiagnosisClass.TOOL_INCOMPATIBLE,
        DiagnosisClass.ARTIFACT_FAILURE,
        DiagnosisClass.STATE_STORE_FINDING,
    }
)


def exit_code_for(report: DoctorReport) -> int:
    """Map a DoctorReport to a stable exit code.

    * 0 = healthy or running (completed with no blocking issues, or an
      operation currently running with nothing to do); a completed run whose
      failed or skipped sources are all below the failure-streak threshold
      is healthy and lists them (#1125)
    * 2 = safely recoverable (resumable-operation, completed-review-refresh,
      persistence-failure, source-failure); source-failure after a completed
      run means a source at or over the streak threshold, or any failed or
      skipped source of a run that recorded no streaks
    * 1 = blocked (repository-blocker, tool-incompatible, artifact-failure,
      state-store-finding)
    """
    if report.diagnosis in _HEALTHY_CLASSES:
        return EXIT_HEALTHY
    if report.diagnosis in _RECOVERABLE_CLASSES:
        return EXIT_RECOVERABLE
    if report.diagnosis in _BLOCKED_CLASSES:
        return EXIT_BLOCKED
    return EXIT_BLOCKED
