"""Allowlisted idempotent LOCAL-ONLY repair actions for the doctor command.

Issue #318: Each repair first proves its preconditions then records a structured
result. No model, network, or Git side effects. Evidence is stdout-only.

Repairs run only when explicitly selected via ``--repair <name>``. The default
``autoscout doctor`` invocation remains read-only.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

from autoscout.doctor import DoctorSeams
from autoscout.doctor import diagnose
from autoscout.git_envelope import GUARD_FILENAME
from autoscout.git_envelope import GitRepositoryAdapter
from autoscout.git_envelope import resolve_repo_root
from autoscout.weekly_report import WeeklyReportStore

# ── Canonical repair order ────────────────────────────────────────────────

CANONICAL_REPAIR_ORDER: tuple[str, ...] = (
    "remove-stale-lock",
    "rerun-health",
    "rerender-review",
    "open-review",
)

# Valid repair names for CLI.
VALID_REPAIR_NAMES: frozenset[str] = frozenset(CANONICAL_REPAIR_ORDER)


# ── Repair plan entry ─────────────────────────────────────────────────────


@dataclass
class RepairPlanEntry:
    name: str
    order: int


@dataclass
class RepairResult:
    name: str
    precondition: str  # "passed" | "blocked" | "not-executed"
    precondition_reason: str = ""
    outcome: str = ""  # "changed" | "already-complete" | "blocked" | "failed"
    changed_artifacts: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "precondition": self.precondition,
            "precondition_reason": self.precondition_reason,
            "outcome": self.outcome,
            "changed_artifacts": self.changed_artifacts,
            "detail": self.detail,
        }


# ── Report type ───────────────────────────────────────────────────────────


@dataclass
class RepairReport:
    plan: list[dict[str, Any]]
    repairs: list[dict[str, Any]]
    post_diagnosis: dict[str, Any]
    # Report-level recommendation/question when any repair is blocked/failed.
    # Derived from the FIRST blocked/failed repair.
    recommended_action: str = ""
    editor_question: str = ""


# ── Injection seams ───────────────────────────────────────────────────────


@dataclass
class RepairSeams:
    """Injectable callables for repair actions. Tests inject fakes."""

    # Stale lock removal.
    now: Callable[[], float] | None = None
    pid_probe: Callable[[int, int], None] | None = None
    # Health runner: returns (returncode, captured_stdout). Default calls the real module.
    health_runner: Callable[[Path, str], tuple[int, str]] | None = None
    # Review renderer: returns (returncode, captured_stdout). Default calls the real module.
    review_renderer: Callable[[Path, Path | None, str], tuple[int, str]] | None = None
    # Review opener: takes html path, returns dict. Default uses webbrowser.open.
    review_opener: Callable[[Path], dict[str, Any]] | None = None
    # Doctor seams for post-diagnosis.
    doctor_seams: DoctorSeams | None = None


# ── Default implementations ───────────────────────────────────────────────


def _default_health_runner(output_dir: Path, effective_date: str) -> tuple[int, str]:
    from autoscout.run_health import run_health

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run_health(output_dir, effective_date)
    return rc, buf.getvalue()


def _default_review_renderer(
    output_dir: Path, state_dir: Path | None, effective_date: str, config_path: Path | None = None
) -> tuple[int, str]:
    from autoscout.review_rendering import render_review

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = render_review(output_dir, state_dir, effective_date, config_path)
    return rc, buf.getvalue()


def _default_review_opener(review_path: Path) -> dict[str, Any]:
    import webbrowser

    try:
        opened = webbrowser.open(str(review_path))
        status = "ok" if opened else "failed"
    except Exception:
        opened = False
        status = "failed"
    return {"status": status, "opened": opened, "path": str(review_path)}


# ── Helpers ───────────────────────────────────────────────────────────────


def _hash_file(path: Path) -> str:
    """SHA-256 hex digest of a file's bytes, or '' if the file is missing."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, FileNotFoundError):
        return ""


def _derive_effective_date(output_dir: Path) -> str | None:
    """Derive effective_date from one coherent report snapshot."""
    snapshot = WeeklyReportStore(output_dir).snapshot("health")
    if snapshot.transaction_in_progress or snapshot.read_inconsistent:
        return None
    return snapshot.effective_date


def _capture_health_verdict(stdout: str) -> dict[str, Any]:
    """Extract key health lines from captured stdout (sanitized — no paths)."""
    lines = stdout.splitlines()
    detail: dict[str, Any] = {}
    for line in lines:
        line = line.strip()
        if line.startswith("run_health_status="):
            detail["run_health_status"] = line.split("=", 1)[1]
        elif line.startswith("classification="):
            detail["classification"] = line.split("=", 1)[1]
        elif line.startswith("ready_for_editor="):
            detail["ready_for_editor"] = line.split("=", 1)[1]
    return detail


# ── Stdout sanitization (Finding 2: no absolute paths in JSON) ───────────


# Prefixes that carry absolute filesystem paths — never keep these lines.
_PATH_BEARING_PREFIXES: tuple[str, ...] = (
    "artifact=",
    "inspect_dir=",
    "shareable_artifact=",
)


def _sanitize_stdout_lines(stdout: str, output_dir: Path | None = None) -> list[str]:
    """Strip path-bearing lines and any line containing the output_dir string.

    Returns a list of sanitized non-empty lines (truncated to 20).
    """
    # Check both resolved and unresolved forms — on macOS /var → /private/var
    # under resolve(), so a line with the unresolved path won't match resolved.
    paths_to_check: list[str] = []
    if output_dir is not None:
        paths_to_check.append(str(output_dir))
        resolved = str(output_dir.resolve())
        if resolved != paths_to_check[0]:
            paths_to_check.append(resolved)
    clean: list[str] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(_PATH_BEARING_PREFIXES):
            continue
        if any(p and p in stripped for p in paths_to_check):
            continue
        clean.append(stripped)
        if len(clean) >= 20:
            break
    return clean


# ── Individual repair handlers ────────────────────────────────────────────


def _repair_remove_stale_lock(repo_root: Path | None, seams: RepairSeams) -> RepairResult:
    """Remove a provably dead stale workflow lock."""
    if repo_root is None:
        return RepairResult(
            name="remove-stale-lock",
            precondition="blocked",
            precondition_reason="no_repo_root",
            outcome="blocked",
            detail={"reason": "no_repo_root"},
        )

    adapter = GitRepositoryAdapter(repo_root)
    result = adapter.remove_stale_guard(now=seams.now, pid_probe=seams.pid_probe)

    if result["result"] == "removed":
        return RepairResult(
            name="remove-stale-lock",
            precondition="passed",
            outcome="changed",
            changed_artifacts=[GUARD_FILENAME],
            detail={"reason": result["reason"]},
        )
    elif result["result"] == "absent":
        return RepairResult(
            name="remove-stale-lock",
            precondition="passed",
            outcome="already-complete",
            detail={"reason": result["reason"]},
        )
    else:
        return RepairResult(
            name="remove-stale-lock",
            precondition="blocked",
            precondition_reason=result["reason"],
            outcome="blocked",
            detail={"reason": result["reason"]},
        )


def _repair_rerun_health(output_dir: Path, seams: RepairSeams) -> RepairResult:
    """Re-run health check from validated canonical inputs."""
    effective_date = _derive_effective_date(output_dir)
    if effective_date is None:
        return RepairResult(
            name="rerun-health",
            precondition="blocked",
            precondition_reason="no_run_date",
            outcome="blocked",
            detail={"reason": "no_run_date"},
        )

    runner = seams.health_runner or _default_health_runner
    rc, stdout = runner(output_dir, effective_date)

    if rc != 0:
        # Sanitize reason lines — no absolute paths, no path-bearing prefixes.
        reason_lines = _sanitize_stdout_lines(stdout, output_dir)
        return RepairResult(
            name="rerun-health",
            precondition="blocked",
            precondition_reason="health_check_failed",
            outcome="blocked",
            detail={
                "reason": "health_check_failed",
                "rc": rc,
                "sanitized_reasons": reason_lines,
            },
        )

    detail = _capture_health_verdict(stdout)
    detail["rc"] = rc

    # ``run_health`` is pure verification — it reads the canonical artifacts,
    # prints a verdict, and writes nothing. So a passing rerun mutated no
    # artifact, and the truthful outcome is ``already-complete`` with no
    # changed artifacts. This makes the repair idempotent by construction
    # (#318) rather than by caching a verdict: persisting one would drop an
    # extra untracked file into the run directory, dirtying the working tree
    # and risking a repository blocker on the next weekly run.
    return RepairResult(
        name="rerun-health",
        precondition="passed",
        outcome="already-complete",
        detail=detail,
    )


def _repair_rerender_review(output_dir: Path, state_dir: Path | None, seams: RepairSeams) -> RepairResult:
    """Re-render the weekly review from validated canonical inputs."""
    # Gate 1: same canonical-inputs validation as rerun-health.
    effective_date = _derive_effective_date(output_dir)
    if effective_date is None:
        return RepairResult(
            name="rerender-review",
            precondition="blocked",
            precondition_reason="no_run_date",
            outcome="blocked",
            detail={"reason": "no_run_date"},
        )

    health_runner = seams.health_runner or _default_health_runner
    rc_health, health_stdout = health_runner(output_dir, effective_date)
    if rc_health != 0:
        return RepairResult(
            name="rerender-review",
            precondition="blocked",
            precondition_reason="health_check_failed",
            outcome="blocked",
            detail={"reason": "health_check_failed", "rc": rc_health},
        )

    # Gate 2: scouting-candidates.json exists.
    report_store = WeeklyReportStore(output_dir)
    if not report_store.path("scouting-candidates.json").is_file():
        return RepairResult(
            name="rerender-review",
            precondition="blocked",
            precondition_reason="missing_scouting_candidates",
            outcome="blocked",
            detail={"reason": "missing_scouting_candidates"},
        )

    # Capture before hashes.
    md_path = report_store.path("weekly-review.md")
    html_path = report_store.path("weekly-review.html")
    before_md = _hash_file(md_path) if md_path.is_file() else ""
    before_html = _hash_file(html_path) if html_path.is_file() else ""

    review_renderer = seams.review_renderer or _default_review_renderer
    rc_render, render_stdout = review_renderer(output_dir, state_dir, effective_date)

    if rc_render != 0:
        return RepairResult(
            name="rerender-review",
            precondition="passed",
            outcome="failed",
            detail={
                "reason": "render_failed",
                "rc": rc_render,
                "sanitized_output": _sanitize_stdout_lines(render_stdout, output_dir),
            },
        )

    after_md = _hash_file(md_path) if md_path.is_file() else ""
    after_html = _hash_file(html_path) if html_path.is_file() else ""

    changed: list[str] = []
    if after_md != before_md or (after_md and not before_md):
        changed.append("weekly-review.md")
    if after_html != before_html or (after_html and not before_html):
        changed.append("weekly-review.html")

    if changed:
        return RepairResult(
            name="rerender-review",
            precondition="passed",
            outcome="changed",
            changed_artifacts=changed,
            detail={"reason": "review_regenerated"},
        )
    else:
        return RepairResult(
            name="rerender-review",
            precondition="passed",
            outcome="already-complete",
            detail={"reason": "review_unchanged"},
        )


def _repair_open_review(output_dir: Path, seams: RepairSeams) -> RepairResult:
    """Open the weekly review HTML in a browser."""
    html_path = WeeklyReportStore(output_dir).path("weekly-review.html")

    # Gate 1: HTML exists.
    if not html_path.is_file():
        return RepairResult(
            name="open-review",
            precondition="blocked",
            precondition_reason="review_html_missing",
            outcome="blocked",
            detail={"reason": "review_html_missing"},
        )

    # Gate 2: canonical inputs validate.
    effective_date = _derive_effective_date(output_dir)
    if effective_date is None:
        return RepairResult(
            name="open-review",
            precondition="blocked",
            precondition_reason="no_run_date",
            outcome="blocked",
            detail={"reason": "no_run_date"},
        )

    health_runner = seams.health_runner or _default_health_runner
    rc_health, _ = health_runner(output_dir, effective_date)
    if rc_health != 0:
        return RepairResult(
            name="open-review",
            precondition="blocked",
            precondition_reason="health_check_failed",
            outcome="blocked",
            detail={"reason": "health_check_failed", "rc": rc_health},
        )

    opener = seams.review_opener or _default_review_opener
    result = opener(html_path)

    if result.get("status") == "ok":
        return RepairResult(
            name="open-review",
            precondition="passed",
            outcome="changed",
            detail={
                "reason": "review_opened",
                "opener_status": result.get("status"),
                "opener_opened": result.get("opened"),
            },
        )
    else:
        return RepairResult(
            name="open-review",
            precondition="passed",
            outcome="failed",
            detail={
                "reason": "open_failed",
                "opener_status": result.get("status"),
                "opener_opened": result.get("opened"),
            },
        )


# ── Repair dispatcher ─────────────────────────────────────────────────────

_REPAIR_HANDLERS: dict[
    str,
    Callable[[Path | None, Path, Path | None, RepairSeams], RepairResult],
] = {
    "remove-stale-lock": lambda repo, out, state, seams: _repair_remove_stale_lock(repo, seams),
    "rerun-health": lambda repo, out, state, seams: _repair_rerun_health(out, seams),
    "rerender-review": lambda repo, out, state, seams: _repair_rerender_review(out, state, seams),
    "open-review": lambda repo, out, state, seams: _repair_open_review(out, seams),
}


def _build_plan(selected: list[str]) -> list[RepairPlanEntry]:
    """Sort selected repairs into canonical execution order; dedupe repeats."""
    seen: set[str] = set()
    plan: list[RepairPlanEntry] = []
    for name in CANONICAL_REPAIR_ORDER:
        if name in selected and name not in seen:
            seen.add(name)
            plan.append(RepairPlanEntry(name=name, order=len(plan)))
    return plan


# ── Russian output helpers ────────────────────────────────────────────────


def _russian_repair_name(name: str) -> str:
    names: dict[str, str] = {
        "remove-stale-lock": "удаление устаревшей блокировки",
        "rerun-health": "перезапуск проверки здоровья",
        "rerender-review": "пересборка обзора",
        "open-review": "открытие обзора",
    }
    return names.get(name, name)


# ── Russian reason mapping (Finding 1: no English tokens in editor output) ─


_RUSSIAN_BLOCKED_REASON: dict[str, str] = {
    "no_repo_root": "корневая папка репозитория не найдена",
    "guard_held_young": "блокировка активна (процесс запущен недавно)",
    "guard_pid_alive": "блокировка активна (процесс выполняется)",
    "guard_malformed": "файл блокировки повреждён",
    "guard_unreadable": "файл блокировки недоступен для чтения",
    "guard_pid_unknown": "не удалось определить состояние процесса блокировки",
    "guard_remove_failed": "не удалось удалить файл блокировки",
    "no_run_date": "отсутствует дата запуска в артефактах",
    "health_check_failed": "проверка здоровья артефактов не пройдена",
    "missing_scouting_candidates": "отсутствуют данные кандидатов для обзора",
    "review_html_missing": "файл обзора отсутствует",
}

_RUSSIAN_FAILED_REASON: dict[str, str] = {
    "render_failed": "не удалось выполнить пересборку обзора",
    "open_failed": "не удалось открыть обзор в браузере",
}

_GENERIC_RUSSIAN_BLOCKED = "действие заблокировано"
_GENERIC_RUSSIAN_FAILED = "неизвестная ошибка"


def _russian_reason_for(repair: RepairResult) -> str:
    """Map any precondition_reason or detail.reason token to plain Russian.

    Unknown/future tokens fall back to a generic phrase — never leak raw tokens.
    """
    token = repair.precondition_reason or repair.detail.get("reason", "")

    if repair.outcome == "blocked":
        return _RUSSIAN_BLOCKED_REASON.get(token, _GENERIC_RUSSIAN_BLOCKED)
    if repair.outcome == "failed":
        return _RUSSIAN_FAILED_REASON.get(token, _GENERIC_RUSSIAN_FAILED)
    # For "not-executed" we show a short phrase.
    if repair.outcome == "not-executed":
        return "предыдущее действие заблокировано или не выполнено"
    return ""


# ── Editor-known review artifact filenames (Finding 1: no .auto-scout-workflow.lock in Russian) ──

_EDITOR_KNOWN_ARTIFACTS: frozenset[str] = frozenset({"weekly-review.md", "weekly-review.html"})


def _render_repair_russian(repair: RepairResult) -> str:
    """One concise Russian line per repair result — plain, non-technical."""
    rname = _russian_repair_name(repair.name)
    if repair.outcome == "changed":
        editor_files = [a for a in repair.changed_artifacts if a in _EDITOR_KNOWN_ARTIFACTS]
        if editor_files:
            files = ", ".join(editor_files)
            return f"✓ {rname}: выполнено (изменено: {files})."
        return f"✓ {rname}: выполнено."
    elif repair.outcome == "already-complete":
        return f"✓ {rname}: уже выполнено ранее."
    elif repair.outcome == "blocked":
        reason_str = _russian_reason_for(repair)
        return f"✗ {rname}: заблокировано — {reason_str}."
    elif repair.outcome == "failed":
        reason_str = _russian_reason_for(repair)
        return f"✗ {rname}: не удалось — {reason_str}."
    elif repair.outcome == "not-executed":
        reason_str = _russian_reason_for(repair)
        return f"— {rname}: не выполнялось ({reason_str})."
    return f"? {rname}: {repair.outcome}."


def _report_level_recommendation(repairs: list[dict[str, Any]]) -> tuple[str, str]:
    """Build report-level recommended_action / editor_question from the FIRST blocked/failed repair.

    Returns (recommended_action, editor_question).
    """
    for r_dict in repairs:
        r = RepairResult(
            name=r_dict["name"],
            precondition=r_dict.get("precondition", ""),
            precondition_reason=r_dict.get("precondition_reason", ""),
            outcome=r_dict.get("outcome", ""),
            changed_artifacts=r_dict.get("changed_artifacts", []),
            detail=r_dict.get("detail", {}),
        )
        if r.outcome not in ("blocked", "failed"):
            continue

        token = r.precondition_reason or r.detail.get("reason", "")
        # Lock-related blocks → contact Dima.
        if token in (
            "no_repo_root",
            "guard_held_young",
            "guard_pid_alive",
            "guard_malformed",
            "guard_unreadable",
            "guard_pid_unknown",
            "guard_remove_failed",
        ):
            return (
                "Свяжитесь с Димой для диагностики проблемы с блокировкой процесса.",
                "Связаться с Димой для диагностики блокировки?",
            )
        # Invalid/missing input artifacts → contact Dima.
        if token in ("no_run_date", "health_check_failed", "missing_scouting_candidates"):
            return (
                "Свяжитесь с Димой для диагностики и восстановления артефактов.",
                "Связаться с Димой для диагностики артефактов?",
            )
        # Missing review HTML → regenerate first.
        if token == "review_html_missing":
            return (
                "Сначала пересоберите обзор, затем повторите открытие.",
                "Пересобрать обзор?",
            )
        # Render/open failure → one safe next step.
        if token == "render_failed":
            return (
                "Проверьте целостность артефактов и повторите пересборку обзора.",
                "Проверить артефакты и повторить пересборку?",
            )
        if token == "open_failed":
            return (
                "Попробуйте открыть обзор вручную или обновите страницу браузера.",
                "Открыть обзор вручную?",
            )
        # Unknown → generic contact Dima.
        return (
            "Свяжитесь с Димой для диагностики.",
            "Связаться с Димой?",
        )
    return ("", "")


def _render_russian_report(report: RepairReport) -> str:
    """Produce concise Russian editor output from a repair report.

    When any repair is blocked/failed: repair lines first, then exactly one
    report-level recommendation + one question (from the first blocked/failed
    repair). Post-diagnosis verdict/action/question are NOT appended — they
    would contradict.

    When all repairs succeed: standard behavior with post-diagnosis appended.
    """
    lines: list[str] = []

    for repair_dict in report.repairs:
        result = RepairResult(
            name=repair_dict["name"],
            precondition=repair_dict.get("precondition", ""),
            precondition_reason=repair_dict.get("precondition_reason", ""),
            outcome=repair_dict.get("outcome", ""),
            changed_artifacts=repair_dict.get("changed_artifacts", []),
            detail=repair_dict.get("detail", {}),
        )
        lines.append(_render_repair_russian(result))

    any_blocked_or_failed = any(r.get("outcome") in ("blocked", "failed") for r in report.repairs)

    if any_blocked_or_failed:
        # Use the report-level recommendation from the first blocked/failed repair.
        rec, question = _report_level_recommendation(report.repairs)
        if rec:
            lines.append("")
            lines.append(rec)
        if question:
            lines.append(question)
    else:
        # Post-diagnosis verdict for successful repairs.
        post = report.post_diagnosis
        verdict = post.get("verdict", "")
        if verdict:
            lines.append("")
            lines.append(verdict)

        recommended = post.get("recommended_action", "")
        if recommended:
            lines.append(recommended)

        question = post.get("editor_question", "")
        if question:
            lines.append(question)

    return "\n".join(lines)


def _render_json_report(report: RepairReport) -> str:
    return json.dumps(
        {
            "plan": report.plan,
            "repairs": report.repairs,
            "post_diagnosis": report.post_diagnosis,
            "recommended_action": report.recommended_action,
            "editor_question": report.editor_question,
        },
        ensure_ascii=False,
        indent=2,
    )


# ── Main entry point ──────────────────────────────────────────────────────


def run_repairs(
    config_path: Path,
    output_dir: Path,
    state_dir: Path | None,
    selected: list[str],
    *,
    seams: RepairSeams | None = None,
) -> tuple[RepairReport, int]:
    """Execute allowlisted repairs selected via ``--repair``.

    Returns (report, exit_code). Never writes diagnostic evidence to disk;
    structured evidence is stdout-only.
    """
    s = seams or RepairSeams()
    if s.review_renderer is None:
        # The default renderer applies this configuration's retired sources (#1025).
        s = replace(s, review_renderer=partial(_default_review_renderer, config_path=config_path))
    repo_root = resolve_repo_root(config_path)

    # Build plan.
    plan_entries = _build_plan(selected)
    plan_dicts = [{"name": e.name, "order": e.order} for e in plan_entries]

    # Execute repairs in order; stop when one is blocked or fails.
    repair_results: list[RepairResult] = []
    stop = False
    for entry in plan_entries:
        if stop:
            repair_results.append(
                RepairResult(
                    name=entry.name,
                    precondition="not-executed",
                    outcome="not-executed",
                    detail={"reason": "earlier_repair_blocked_or_failed"},
                )
            )
            continue

        handler = _REPAIR_HANDLERS.get(entry.name)
        if handler is None:
            repair_results.append(
                RepairResult(
                    name=entry.name,
                    precondition="blocked",
                    precondition_reason="unknown_repair",
                    outcome="blocked",
                    detail={"reason": "unknown_repair"},
                )
            )
            stop = True
            continue

        result = handler(repo_root, output_dir, state_dir, s)
        repair_results.append(result)

        if result.outcome in ("blocked", "failed"):
            stop = True

    # Run post-repair diagnosis.
    doctor_s = s.doctor_seams or DoctorSeams()
    post_report = diagnose(config_path, output_dir, state_dir, seams=doctor_s)
    post_dict = post_report.to_dict()

    # Compute report-level recommendation from the first blocked/failed repair.
    rec_action, rec_question = _report_level_recommendation([r.to_dict() for r in repair_results])

    report = RepairReport(
        plan=plan_dicts,
        repairs=[r.to_dict() for r in repair_results],
        post_diagnosis=post_dict,
        recommended_action=rec_action,
        editor_question=rec_question,
    )

    # Exit code: 1 if any repair was blocked or failed; otherwise post-diagnosis exit code.
    any_blocked_or_failed = any(r.outcome in ("blocked", "failed") for r in repair_results)
    if any_blocked_or_failed:
        exit_code = 1
    else:
        from autoscout.doctor import exit_code_for

        exit_code = exit_code_for(post_report)

    return report, exit_code
