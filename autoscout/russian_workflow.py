"""Russian editor-facing natural-language contract for the weekly workflow.

This module defines the vendor-neutral agent-behavior contract for the
Russian-first weekly workflow (#247). The coding agent maps the editor's
Russian intent to the single weekly-workflow entry point; this file does
NOT implement a phrase parser, keyword list, or NLP engine.

Agent contract (summarised — the authoritative contract is ``AGENTS.md``,
with the full three-level policy in
``docs/agents/contracts/learning-and-escalation.md``):
- Clear Russian intent (e.g. «Запусти еженедельный поиск книг») maps to
  ``run_weekly_workflow``.
- Ambiguous intent produces exactly one short Russian clarification asking
  whether the user wants to run the weekly book search; no workflow side
  effect (Git, network, model, state mutation) occurs before clarification.
- Equivalent grammatical phrasing is understood semantically.

The three-level learning / technical-request / escalation policy (#248)
applies on top of the long-standing confirmation protocol:
- Level 1 — Learning: every candidate/profile mutation still requires
  preview + explicit confirmation; confirmed learning is acknowledged under
  "Что AutoScout узнал" with verbatim editor wording and recorded effects;
  routine safe follow-up then continues without another confirmation.
- Level 2 — Technical request: explicit editor request for code, config,
  source, workflow, report, field, command, or integration changes
  authorises filing a focused editor-request issue without redundant
  approval; ambiguous or suggestive technical remarks require one
  clarification first; editorial preferences remain Level 1.
- Level 3 — Escalation: the agent stops before the unsafe/consequential
  mutation (or before any further mutation once a conflict/rejected push
  is discovered) when facing corruption risk, Git conflict, security/
  privacy concerns, repeated source failures indicating systemic
  breakage, broad ambiguity, destructive/irreversible action, or
  identity merges; preserves sanitized diagnostics when applicable;
  explains in plain Russian; gives exactly one recommended next action
  and asks one minimum-confirmation question. Identity merges preserve
  the existing dry-run boundary.
"""

from __future__ import annotations

from collections.abc import Mapping as _Mapping
from collections.abc import Sequence as _Sequence
from typing import Any

from autoscout.content_description import DESCRIPTION_ADJUDICATED_PROVENANCE as _DESCRIPTION_ADJUDICATED_PROVENANCE
from autoscout.content_description import DESCRIPTION_HOST_GROUNDED_PROVENANCE as _DESCRIPTION_HOST_GROUNDED_PROVENANCE
from autoscout.content_description import coverage_denominator as _coverage_denominator
from autoscout.content_description import has_adjudicated_descriptions as _has_adjudicated_descriptions
from autoscout.content_description import pm_logline_count as _pm_logline_count
from autoscout.content_description import verified_description_count as _verified_description_count
from autoscout.grounded_description import REASON_NOT_YET_PUBLISHED as _REASON_NOT_YET_PUBLISHED
from autoscout.pipeline_path import PipelinePath as _PipelinePath
from autoscout.progress_snapshot import presentation_warning_threshold_seconds as _presentation_warning_threshold
from autoscout.progress_snapshot import summarize_snapshot_rows as _summarize_snapshot_rows
from autoscout.source_run_status import summarize_source_runs as _summarize_source_runs


def workflow_started_message() -> str:
    return "Запускаю еженедельный поиск книг..."


def resume_started_message() -> str:
    return "Продолжаю прерванный поиск книг..."


def recovery_started_message(action: str) -> str:
    if action == "resume-weekly":
        return resume_started_message()
    if action == "complete-locally":
        return "Обновляю обзор из сохранённых результатов..."
    return ""


def stage_message(stage: str) -> str:
    messages: dict[str, str] = {
        "kirkus-precollection": "Собираю данные Kirkus, это занимает до получаса...",
        "pm-precollection": "Читаю письма Publishers Marketplace о сделках...",
        "pw-precollection": "Читаю новый выпуск Publishers Weekly...",
        "capture": "Проверяю источники...",
        "codex": "Обрабатываю полученные данные...",
        "finalize": "Подготавливаю результаты поиска...",
        "pipeline": "Формирую список кандидатов для обзора...",
        "isbn-completion": "Ищу ISBN новых книг...",
        "enrichment": "Составляю описания новых книг...",
        "description-adjudication": "Проверяю описания новых книг по источникам...",
        "pm-logline-descriptions": "Пишу короткие описания по логлайнам сделок PM...",
        "original-edition-completion": "Ищу оригинальные издания переведённых книг...",
        "work-age-completion": "Узнаю год первой публикации книг...",
        "russian-edition-completion": "Ищу русские издания книг...",
        "review": "Готовлю итоговый обзор...",
        "health": "Проверяю качество результатов...",
    }
    return messages.get(stage, f"Выполняю этап: {stage}...")


def extraction_progress_message(completed: int, total: int, timed_out: int = 0, retrying: int = 0) -> str:
    """Report extraction progress from inside the longest workflow stage.

    The count, not a bare liveness ping, is what tells the operator whether the
    round will finish inside its budget. ``completed`` counts each source once,
    at its first settled result, so the number never decreases. ``retrying``
    reports sources being attempted again, which is what keeps the stage
    audible after the count has already filled."""
    message = f"Обработано {completed} из {total} источников"
    if timed_out > 0:
        message += f" (не успели: {timed_out})"
    if retrying > 0:
        message += f" (повторная попытка: {retrying})"
    return message + "..."


_SNAPSHOT_PROGRESS_STAGE_LABELS = {
    "capture": "Проверка источников",
    "extraction": "Обработка источников",
    "isbn-completion": "Поиск ISBN",
    "enrichment": "Составление описаний",
    "description-adjudication": "Проверка описаний",
    "pm-logline-descriptions": "Описания по логлайнам PM",
    "original-edition-completion": "Поиск оригинальных изданий",
    "work-age-completion": "Год первой публикации",
}


def snapshot_progress_line(snapshot: _Mapping[str, Any] | None) -> str:
    """Combine the main stage and an active parallel Kirkus pass in one line."""
    line = _stage_snapshot_progress_line(snapshot)
    kirkus = snapshot.get("kirkus") if isinstance(snapshot, _Mapping) else None
    if isinstance(kirkus, _Mapping) and kirkus.get("status") == "running":
        line += " " + _stage_snapshot_progress_line(kirkus)
    return line


def _stage_snapshot_progress_line(snapshot: _Mapping[str, Any] | None) -> str:
    """Build one operator heartbeat line from a redacted progress snapshot.

    Issue #452: the line reports aggregate finished/running/not-started
    counts and names at most three running items with their elapsed time.
    Wording is stage-specific and grounded in the row state: rows running
    under in-flight semantics — capture rows and the per-candidate rows the
    description stages publish (issue #706) — are ``in_flight`` and carry
    elapsed in-flight time, so they use the Russian "in work" wording,
    while extraction rows (``running``) carry event-idle time and keep the
    "no events" wording. An extraction item whose event-idle value reached
    the derived warning threshold (one heartbeat before the extraction
    no-progress lease) is labelled as a possible stall. The threshold
    applies only to extraction event-idle time and never to ``in_flight``
    rows, which report in-flight time rather than staleness, so those rows
    are never labelled as stalled. All Russian text is constructed here;
    the caller only prints the returned line.
    """
    rows: Any = snapshot.get("rows") if isinstance(snapshot, _Mapping) else None
    if not isinstance(rows, list):
        rows = []
    overview = _summarize_snapshot_rows(rows)
    stage = str(snapshot.get("stage", "")) if isinstance(snapshot, _Mapping) else ""
    if stage == "kirkus-wait":
        return "Ожидание завершения сбора Kirkus."
    if stage == "kirkus-precollection":
        # The pass learns how many pages it will open only as it reads the list, so it reports requests made.
        return f"Сбор Kirkus: запрошено страниц {overview['finished'] + overview['running']}."
    if stage == "pm-precollection":
        # One row per fetched email: the line counts emails, never their subjects or deals.
        return f"Сбор PM: прочитано писем {overview['finished']}."
    if stage == "pw-precollection":
        # One row per issue the pass finished, collected or failed: never a title or an issue's contents.
        return f"Сбор PW: обработано выпусков {overview['finished']}."
    if stage == "russian-edition-completion":
        # One row per book tried, marked when an edition was confirmed: never a title or a catalogue's text.
        found = sum(1 for row in rows if isinstance(row, _Mapping) and row.get("found") is True)
        return f"Поиск русских изданий: проверено {overview['finished']}, найдено {found}."
    label = _SNAPSHOT_PROGRESS_STAGE_LABELS.get(stage) or (f"Этап {stage}" if stage else "Прогресс")
    head = (
        f"{label}: готово {overview['finished']}, выполняется {overview['running']}, ожидают {overview['not_started']}."
    )
    if overview["running"] <= 0:
        return head
    threshold = _presentation_warning_threshold()
    descriptions: list[str] = []
    for item in overview["running_items"]:
        name = str(item.get("name") or "").strip() or "источник без имени"
        idle = item.get("idle_seconds")
        if idle is None:
            descriptions.append(name)
            continue
        seconds = max(0, round(float(idle)))
        # In-flight rows carry elapsed in-flight time, not event-idle time,
        # so their wording must not claim "без событий". Extraction rows are
        # the only "running" rows, and they carry event-idle time.
        elapsed_wording = "в работе" if str(item.get("state") or "") == "in_flight" else "без событий"
        description = f"{name} ({elapsed_wording} {seconds} с"
        # The possible-stall warning is gated on both the snapshot stage and
        # the row state: the derived threshold bounds extraction event-idle
        # time on running rows only, so a capture snapshot is never labelled
        # even for a malformed running-state row, and a malformed extraction
        # row carrying capture-style in-flight elapsed time is never labelled
        # by the event-idle threshold.
        if stage == "extraction" and str(item.get("state") or "") == "running" and float(idle) >= threshold:
            description += ", возможна остановка"
        description += ")"
        descriptions.append(description)
    tail = "Выполняется: " + ", ".join(descriptions)
    if overview["omitted_running"] > 0:
        return head + " " + tail + f" и ещё {overview['omitted_running']}."
    return head + " " + tail + "."


def _deduplicated_description_failures(
    grounded_report: _Mapping[str, Any] | dict[str, Any] | None,
    adjudication_report: _Mapping[str, Any] | dict[str, Any] | None,
    fallback_count: int,
) -> int:
    """Count candidates this run left without a description.

    The sentence counts candidates, not stage attempts. A candidate both
    stages failed counts once, and a candidate the grounded stage could not
    ground and adjudication then applied counts zero, because it ended the
    run with a description. Otherwise the sentence contradicts the coverage
    line printed above it (issue #641).

    When neither report carries records (direct callers, skipped stages),
    fall back to the caller's count so the sentence stays stable and never
    reports a missing report as zero.
    """
    from autoscout.description_adjudication import unresolved_description_candidates

    unresolved = unresolved_description_candidates(grounded_report, adjudication_report)
    if unresolved:
        return len(unresolved)
    if _has_stage_records(grounded_report) or _has_stage_records(adjudication_report):
        return 0
    return int(fallback_count or 0)


def _agent_call_failures(report: _Mapping[str, Any] | dict[str, Any] | None) -> int:
    """Count failed agent calls in the adjudication stage report (issue #788).

    ``counts.failed`` holds the rows whose own verdict is a failed call;
    ``deep_pass.failed`` holds the calls that failed behind another call's
    verdict. The two never count the same row.
    """
    if not isinstance(report, _Mapping):
        return 0
    total = 0
    for section, key in (("counts", "failed"), ("deep_pass", "failed")):
        values = report.get(section)
        if not isinstance(values, _Mapping):
            continue
        try:
            total += int(values.get(key, 0) or 0)
        except (TypeError, ValueError):
            continue
    return total


def _has_stage_records(report: _Mapping[str, Any] | dict[str, Any] | None) -> bool:
    """True when a description stage report carries per-candidate records."""
    return isinstance(report, _Mapping) and isinstance(report.get("records"), list) and bool(report["records"])


def completion_message(
    outcome: str,
    new_count: int = 0,
    updated_count: int = 0,
    previously_seen: int = 0,
    attention_required: bool = False,
    review_available: bool = False,
    description_coverage: dict[str, Any] | None = None,
    reused_source_count: int = 0,
    collecting_source_count: int = 0,
    pipeline_path: _PipelinePath | None = None,
    grounded_description_counts: dict[str, int] | None = None,
    grounded_description_reason_counts: dict[str, int] | None = None,
    description_stage_failures: int = 0,
    description_stage_broken: bool = False,
    attention_reasons: tuple[str, ...] = (),
    source_statuses: _Sequence[object] | None = None,
    adjudication_stage_report: dict[str, Any] | None = None,
    description_stage_report: dict[str, Any] | None = None,
    description_provenance_counts: dict[str, int] | None = None,
    description_caveats: _Sequence[str] = (),
) -> str:
    lines = []

    feedback_only = pipeline_path in {_PipelinePath.FEEDBACK_NOOP, _PipelinePath.FEEDBACK_APPLIED}

    if outcome == "intervention-required":
        lines.append("Еженедельный поиск книг прерван - требуется вмешательство.")
        return "\n".join(lines)
    if pipeline_path == _PipelinePath.FEEDBACK_APPLIED:
        lines.append("Финализация ваших решений по книгам завершена.")
    elif pipeline_path == _PipelinePath.FEEDBACK_NOOP:
        lines.append("Новый поиск не выполнялся.")
    elif outcome == "completed/no-attention":
        lines.append("Еженедельный поиск книг завершён успешно.")
    elif outcome == "completed/attention-required":
        lines.append("Еженедельный поиск книг завершён. Требуется ваше внимание.")

    # A run that scouted nothing has no counts of its own. Whatever sits in the
    # report was produced by an earlier run, and answering "what did you find
    # this week?" with those numbers is the fault this issue exists to stop, so
    # the run says what it actually did instead (Issue #388).
    if pipeline_path == _PipelinePath.FEEDBACK_APPLIED:
        lines.append("Учтены ваши решения по книгам. Новый поиск не выполнялся.")
    elif pipeline_path == _PipelinePath.SCOUTED_WITH_FEEDBACK:
        lines.append("Учтены ваши решения по книгам в свежем поиске.")

    # Stated before the counts, because it changes what the counts mean. A run
    # served mostly from earlier artifacts reports last week's books as this
    # week's result, and the editor cannot tell from the numbers alone
    # (Issue #384). Current workflow results carry the source-run statuses so
    # this boundary evaluates the shared predicate against actual values.
    if source_statuses is not None:
        source_summary = _summarize_source_runs(source_statuses)
        effective_reused_source_count = source_summary.stale_source_count
        effective_collecting_source_count = source_summary.collecting_source_count
    else:
        effective_reused_source_count = reused_source_count
        effective_collecting_source_count = collecting_source_count
    if effective_reused_source_count:
        if effective_collecting_source_count:
            lines.append(
                f"Внимание: {effective_reused_source_count} из {effective_collecting_source_count} источников — "
                "данные предыдущего запуска, а не свежий сбор."
            )
        else:
            lines.append(
                f"Внимание: {effective_reused_source_count} источников использовали данные предыдущего запуска."
            )
        lines.append(
            "Показатели обзора относятся не только к этой неделе."
            if feedback_only
            else "Числа ниже относятся не только к этой неделе."
        )

    if not feedback_only:
        lines.append(f"Новых книг: {new_count}.")
        lines.append(f"Обновлено книг: {updated_count}.")
        if previously_seen:
            lines.append(f"Ранее встречавшихся книг: {previously_seen}.")
        lines.extend(description_caveats)

    if description_coverage and not feedback_only:
        validated = int(description_coverage.get("validated_count", 0))
        fallback = int(description_coverage.get("fallback_count", 0))
        total = int(description_coverage.get("total_candidates", validated + fallback))
        if total > 0:
            # A stub no source confirmed is not a description the editor
            # has, so it is reported on its own line (issue #788). Coverage
            # written before the split carries no key and reads as before.
            try:
                unsourced = int(description_coverage.get("unsourced_count", 0) or 0)
            except (TypeError, ValueError):
                unsourced = 0
            # Books awaiting publication have their own line below and are
            # no gap in this headline (issue #944).
            denominator = _coverage_denominator(description_coverage)
            if denominator > 0:
                lines.append(
                    f"Описаний на русском: {_verified_description_count(description_coverage)} из {denominator}."
                )
            if unsourced > 0:
                lines.append(f"Не подтверждены источниками: {unsourced}.")
            # A book that is not yet published needs no check from the
            # editor, so it has its own line (issue #791).
            try:
                unpublished = int(description_coverage.get("not_yet_published_count", 0) or 0)
            except (TypeError, ValueError):
                unpublished = 0
            if fallback - unpublished > 0:
                lines.append(f"Требуют проверки: {fallback - unpublished}.")
            if unpublished > 0:
                lines.append(f"Ещё не опубликованы: {unpublished}.")
            # A full description is not expected for a PM deal, so a
            # logline description is no gap; it has its own line (#950).
            logline = _pm_logline_count(description_coverage)
            if logline > 0:
                lines.append(f"Описания по логлайну PM: {logline}.")
            # Mechanical depth, shown only when below-floor descriptions
            # remain and the artifact actually assessed depth: legacy
            # coverage without depth keys claims nothing (issue #672).
            try:
                below_depth = int(description_coverage.get("below_depth_count", 0) or 0)
            except (TypeError, ValueError):
                below_depth = 0
            if below_depth > 0 and "below_depth_count" in description_coverage:
                lines.append(f"Кратких описаний (ниже порога глубины): {below_depth}.")

    if description_provenance_counts is not None and not feedback_only:
        # The counts beside the coverage counts: which sources the
        # confirmed descriptions rest on. Shown only once an adjudicated
        # description exists, so a week without one reads exactly as
        # before; a zero beside a nonzero is still reported, the way the
        # existing lines report their zeros (issue #644).
        if _has_adjudicated_descriptions(description_provenance_counts):
            host_grounded = int(description_provenance_counts.get(_DESCRIPTION_HOST_GROUNDED_PROVENANCE, 0) or 0)
            adjudicated = int(description_provenance_counts.get(_DESCRIPTION_ADJUDICATED_PROVENANCE, 0) or 0)
            lines.append(
                "Происхождение описаний: источники из проверенного списка - "
                f"{host_grounded}, источники проверены по тексту страниц - {adjudicated}."
            )

    if grounded_description_counts is not None and not feedback_only:
        lines.append(
            "Проверка источников: подтверждено несколькими источниками - "
            f"{int(grounded_description_counts.get('grounded', 0) or 0)}, "
            "по одному источнику - "
            f"{int(grounded_description_counts.get('single_source', 0) or 0)}, "
            "без описания - "
            f"{int(grounded_description_counts.get('attempted_no_description', 0) or 0)}, "
            "поиск не запускался - "
            f"{int(grounded_description_counts.get('not_attempted', 0) or 0)}."
        )
        if grounded_description_reason_counts:
            from autoscout.grounded_description import reason_label

            labeled_reason_counts: dict[str, int] = {}
            for reason, count in sorted(grounded_description_reason_counts.items()):
                count_value = int(count or 0)
                if count_value > 0:
                    label = reason_label(str(reason))
                    labeled_reason_counts[label] = labeled_reason_counts.get(label, 0) + count_value
            reasons = [f"{label} - {count}" for label, count in sorted(labeled_reason_counts.items())]
            if reasons:
                lines.append("Причины отсутствия описания: " + "; ".join(reasons) + ".")

    if adjudication_stage_report is not None and not feedback_only:
        adjudication_counts = adjudication_stage_report.get("counts", {})
        if isinstance(adjudication_counts, dict) and adjudication_stage_report.get("records") is not None:
            applied = int(adjudication_counts.get("applied", 0) or 0)
            ambiguous = int(adjudication_counts.get("ambiguous", 0) or 0)
            failed = int(adjudication_counts.get("failed", 0) or 0)
            skipped = int(adjudication_counts.get("skipped", 0) or 0)
            still_fallback = int(adjudication_counts.get("still_fallback", 0) or 0)
            # A book that is not yet published is classified with the
            # declines but needs no decision from anyone (issue #791).
            stage_records = adjudication_stage_report.get("records", [])
            awaiting_publication = sum(
                1
                for record in (stage_records if isinstance(stage_records, list) else [])
                if isinstance(record, dict) and str(record.get("reason_code", "")) == _REASON_NOT_YET_PUBLISHED
            )
            lines.append(
                "Проверка описаний агентом: применено - "
                f"{applied}, без описания - {still_fallback}, "
                f"требует решения - {max(0, ambiguous + failed - awaiting_publication)}, пропущено - {skipped}."
            )
            from autoscout.description_adjudication import adjudication_reason_label

            adjudication_reason_totals: dict[str, int] = {}
            records = adjudication_stage_report.get("records", [])
            if isinstance(records, list):
                for record in records:
                    if not isinstance(record, dict):
                        continue
                    if str(record.get("result", "")) not in {"failed", "ambiguous"}:
                        continue
                    code = str(record.get("reason_code", "") or "").strip()
                    if not code:
                        continue
                    label = adjudication_reason_label(code)
                    adjudication_reason_totals[label] = adjudication_reason_totals.get(label, 0) + 1
            adjudication_reasons = [f"{label} - {count}" for label, count in sorted(adjudication_reason_totals.items())]
            if adjudication_reasons:
                lines.append("Причины проверки агентом: " + "; ".join(adjudication_reasons) + ".")

    deferred_parts: list[str] = []
    if not feedback_only:
        from autoscout.description_adjudication import resolved_description_candidates
        from autoscout.grounded_description import REASON_NOT_ATTEMPTED_STAGE_BUDGET

        # Attribute a candidate deferred twice to the later adjudication stage.
        grounded_records = (
            description_stage_report.get("records", []) if isinstance(description_stage_report, _Mapping) else []
        )
        adjudication_records = (
            adjudication_stage_report.get("records", []) if isinstance(adjudication_stage_report, _Mapping) else []
        )

        def deferred_ids(records: Any) -> set[str]:
            if not isinstance(records, list):
                return set()
            return {
                str(row.get("candidate_id") or row.get("identity_key") or "")
                for row in records
                if isinstance(row, _Mapping) and row.get("reason_code") == REASON_NOT_ATTEMPTED_STAGE_BUDGET
            }

        grounded_deferred = deferred_ids(grounded_records)
        adjudication_deferred = deferred_ids(adjudication_records)
        limit_info = (
            adjudication_stage_report.get("deferred_limit", {})
            if isinstance(adjudication_stage_report, _Mapping)
            else {}
        )
        if isinstance(limit_info, _Mapping) and isinstance(limit_info.get("candidate_ids"), list):
            eligible = (
                {
                    str(row.get("candidate_id") or row.get("identity_key") or "")
                    for row in grounded_records
                    if isinstance(row, _Mapping)
                    and (
                        row.get("result") in {"failed", "ambiguous"}
                        or row.get("reason_code") == REASON_NOT_ATTEMPTED_STAGE_BUDGET
                    )
                }
                if isinstance(grounded_records, list)
                else set()
            )
            adjudication_deferred.update(str(value) for value in limit_info["candidate_ids"] if str(value) in eligible)
        resolved = resolved_description_candidates(adjudication_stage_report)
        grounded_deferred -= resolved
        adjudication_deferred -= resolved
        grounded_deferred -= adjudication_deferred
        if grounded_deferred:
            deferred_parts.append(f"составление описаний - {len(grounded_deferred)}")
        if adjudication_deferred:
            deferred_parts.append(f"проверка описаний - {len(adjudication_deferred)}")
        if deferred_parts:
            lines.append("Отложено из-за лимита времени: " + ", ".join(deferred_parts) + ".")

    if attention_required:
        # Issue #557: attention is a this-run fact. Name what this run left
        # unresolved and what happens next; the standing description backlog
        # is already reported above as plain numbers. Precedence: the most
        # concrete fact first. The sentence counts candidates, not stage
        # attempts, so a candidate both stages failed counts once (issue
        # #641).
        effective_failures = (
            _deduplicated_description_failures(
                description_stage_report, adjudication_stage_report, description_stage_failures
            )
            if (description_stage_report is not None or adjudication_stage_report is not None)
            else int(description_stage_failures or 0)
        )
        if effective_failures > 0:
            lines.append(
                f"Не удалось подготовить описания для {effective_failures} "
                f"{_books_genitive(effective_failures)} из этого запуска. "
                "Их список в обзоре; следующий поиск повторит попытку."
            )
        elif description_stage_broken:
            lines.append(
                "Этап подготовки описаний не завершился. Результаты поиска в обзоре; следующий поиск повторит попытку."
            )
        elif _agent_call_failures(adjudication_stage_report) > 0:
            # No book was left without text, but a call to the agent
            # failed: behind a deeper call that succeeded, or on a book
            # that keeps its unsourced stub or short description. That is
            # a stage-health fact, and it must not be reported as a source
            # that was not checked (issue #788).
            lines.append(
                "Часть обращений к агенту при проверке описаний завершилась сбоем. "
                "Результаты в обзоре; следующий поиск повторит попытку."
            )
        elif "sources" in attention_reasons:
            lines.append("Часть источников не была проверена. Результаты доступны для просмотра.")
        elif "readiness" in attention_reasons:
            lines.append(
                "Проверка качества результатов завершилась с замечаниями. Результаты в обзоре; следующий поиск повторит попытку."
            )
        elif not deferred_parts:
            # Direct callers that predate reason reporting keep the legacy text.
            lines.append("Часть источников не была проверена. Результаты доступны для просмотра.")

    if not feedback_only and outcome == "completed/no-attention" and not new_count and not updated_count:
        lines.append("На этой неделе новых подходящих книг не найдено. Это нормально.")

    if review_available:
        lines.append("Обзор готов. Открываю...")

    return "\n".join(lines)


def scheduled_run_skipped_message() -> str:
    """The one line of a scheduled start that met a search in progress (#1057)."""
    return "Запуск по расписанию пропущен: поиск уже идёт."


def open_failure_fallback() -> str:
    return "Обзор готов, но не открылся автоматически. Откройте или обновите файл обзора weekly-review.html."


def intervention_message(failed_stage: str = "", reason: str = "") -> str:
    from autoscout.code_gate import ENVIRONMENT_UPDATE_REASONS

    if failed_stage == "code-gate" and reason in ENVIRONMENT_UPDATE_REASONS:
        # The environment, not the code, stopped the night (#1130).
        return (
            "Не удалось обновить окружение Python на сервере под новую версию кода, поэтому поиск не запускался "
            "и данные не изменены. Следующий ночной запуск попробует снова. "
            "Рекомендую связаться с Димой для исправления. Связаться с Димой?"
        )
    if failed_stage in ("repository-preflight", "repository-update"):
        return (
            "Запуск поиска заблокирован — репозиторий не в безопасном состоянии. "
            "Рекомендую связаться с Димой для диагностики. Связаться с Димой?"
        )
    if failed_stage == "code-gate":
        return (
            "Новая версия кода не прошла проверку на этом сервере, поэтому поиск не запускался и данные не изменены. "
            "Рекомендую связаться с Димой для исправления. Связаться с Димой?"
        )
    if failed_stage == "repository-finalize":
        return (
            "Не удалось завершить сохранение результатов. "
            "Рекомендую связаться с Димой для диагностики. Связаться с Димой?"
        )
    if failed_stage == "description-input":
        return (
            "Входные данные для описаний некорректны. Рекомендую связаться с Димой для диагностики. Связаться с Димой?"
        )
    if failed_stage == "description-persistence":
        return "Не удалось сохранить результаты описаний. Рекомендую повторить еженедельный поиск. Повторить поиск?"
    if failed_stage == "description-adjudication-persistence":
        return (
            "Не удалось сохранить результаты проверки описаний по источникам. "
            "Рекомендую повторить еженедельный поиск. Повторить поиск?"
        )
    if failed_stage == "description-recovery":
        return (
            "Не удалось безопасно восстановить результаты описаний. "
            "Рекомендую связаться с Димой для диагностики. Связаться с Димой?"
        )
    if failed_stage == "capture":
        problem = "Не удалось подготовить источники для поиска."
    elif failed_stage == "codex":
        return (
            "Не удалось обработать данные из источников. Рекомендую: проверьте доступность данных вместе с Димой. "
            "Связаться с Димой?"
        )
    elif failed_stage in ("finalize", "pipeline"):
        problem = "Не удалось завершить обработку результатов."
    elif failed_stage in ("enrichment", "review", "health"):
        problem = "Не удалось завершить подготовку и проверку обзора."
    else:
        problem = "Возникла непредвиденная ошибка."
    return f"{problem} Рекомендую связаться с Димой для диагностики. Связаться с Димой?"


def _books_genitive(n: int) -> str:
    """Genitive after «для N»: 1 книги, 2/5/11 книг, 21 книги."""
    return "книги" if n % 10 == 1 and n % 100 != 11 else "книг"


def source_problem_message(
    problems: list[dict[str, str]] | None = None,
    skipped: int = 0,
    failed: int = 0,
) -> str:
    if problems:
        # A failed source may carry one plain action for the editor (#956: PW refused the subscription link).
        failed_names = [
            f"{p['source_name']} ({p['editor_detail']})"
            if str(p.get("editor_detail", "")).strip()
            else str(p["source_name"])
            for p in problems
            if p.get("status") == "failed" and p.get("source_name")
        ]
        skipped_names = [
            str(p["source_name"]) for p in problems if p.get("status") == "skipped" and p.get("source_name")
        ]
        incomplete_names = []
        for problem in problems:
            if problem.get("status") != "incomplete" or not problem.get("source_name"):
                continue
            observed = str(problem.get("observed_rows", "")).strip()
            expected = str(problem.get("expected_rows", "")).strip()
            if observed.isdigit() and expected.isdigit():
                incomplete_names.append(f"{problem['source_name']} ({observed} из {expected})")
            elif str(problem.get("editor_detail", "")).strip():
                incomplete_names.append(f"{problem['source_name']} ({problem['editor_detail']})")
            else:
                incomplete_names.append(str(problem["source_name"]))
        # Issue #765: a source whose row count left its usual range.
        unusual_counts = [
            f"{problem['source_name']} ({problem['editor_detail']})"
            if str(problem.get("editor_detail", "")).strip()
            else str(problem["source_name"])
            for problem in problems
            if problem.get("status") == "unusual_count" and problem.get("source_name")
        ]
        named_parts = []
        if failed_names:
            named_parts.append(f"не удалось проверить: {', '.join(failed_names)}")
        if skipped_names:
            named_parts.append(f"пропущено: {', '.join(skipped_names)}")
        if incomplete_names:
            named_parts.append(f"неполный охват: {', '.join(incomplete_names)}")
        if unusual_counts:
            named_parts.append(f"необычное число книг: {', '.join(unusual_counts)}")
        if named_parts:
            return "\u26a0 " + "; ".join(named_parts) + "."

    parts = []
    if failed:
        parts.append(f"{failed} источников не удалось проверить")
    if skipped:
        parts.append(f"{skipped} источников пропущено")
    if not parts:
        return ""
    return "\u26a0 " + ", ".join(parts) + "."


_PROGRESS_STAGES = (
    "capture",
    "codex",
    "finalize",
    "pipeline",
    "enrichment",
    "description-adjudication",
    "review",
    "health",
)


def _failed_reason(log: list[Any], failed_stage: str) -> str:
    for entry in reversed(log):
        if isinstance(entry, dict) and entry.get("stage") == failed_stage and entry.get("status") == "failed":
            return str(entry.get("reason", ""))
    return ""


def render_workflow_result(result, *, include_progress: bool = True) -> str:
    """Render a complete WeeklyWorkflowResult into Russian conversation text.

    Ties together the individually-testable message helpers above so the
    editor-facing narrative is actually connected to structured workflow
    events instead of being assembled ad hoc: routine stage progress, the
    outcome-appropriate completion or intervention message, named source
    problems, and the open-review fallback when automatic opening failed.
    """
    if getattr(getattr(result, "outcome", ""), "value", getattr(result, "outcome", "")) == "skipped/run-in-progress":
        return scheduled_run_skipped_message()
    log = list(getattr(result, "log", []) or [])
    lines = [workflow_started_message()] if include_progress else []

    if include_progress:
        for entry in log:
            stage = entry.get("stage")
            if stage in _PROGRESS_STAGES and entry.get("status") == "ok":
                lines.append(stage_message(stage))

    outcome = getattr(result, "outcome", "")
    outcome_value = getattr(outcome, "value", outcome)

    if outcome_value == "intervention-required":
        failed_stage = getattr(result, "failed_stage", "") or ""
        capacity_stop_count = int(getattr(result, "capacity_stop_count", 0) or 0)
        lines.append(intervention_message(failed_stage, _failed_reason(log, failed_stage)))
        if capacity_stop_count:
            lines.append(f"Портфельный лимит остановил {capacity_stop_count} элементов; поиск не завершён.")
        return "\n".join(lines)

    readiness_facts = getattr(result, "readiness_facts", {}) or {}
    delta_counts = getattr(result, "delta_counts", {}) or {}
    problems = getattr(result, "source_problems", []) or []
    description_stage_problems = getattr(result, "description_stage_problems", []) or []
    grounded_counts = getattr(result, "grounded_description_counts", None)
    grounded_reason_counts = getattr(result, "grounded_description_reason_counts", None)
    provenance_counts = getattr(result, "description_provenance_counts", None)
    source_statuses = getattr(result, "source_statuses", None)
    run_summary = getattr(result, "run_summary", {}) or {}
    if not grounded_counts and isinstance(run_summary, dict):
        grounded_counts = run_summary.get("grounded_description_counts")
    if not grounded_reason_counts and isinstance(run_summary, dict):
        grounded_reason_counts = run_summary.get("grounded_description_reason_counts")
    if not provenance_counts and isinstance(run_summary, dict):
        provenance_counts = run_summary.get("description_provenance_counts")

    description_stage_report = getattr(result, "description_stage_report", {}) or {}
    if not description_stage_report and isinstance(run_summary, dict):
        candidate_report = run_summary.get("description_stage_report", {})
        if isinstance(candidate_report, dict):
            description_stage_report = candidate_report
    adjudication_stage_report = getattr(result, "description_adjudication_stage_report", {}) or {}
    if not adjudication_stage_report and isinstance(run_summary, dict):
        candidate_adjudication = run_summary.get("description_adjudication_stage_report", {})
        if isinstance(candidate_adjudication, dict):
            adjudication_stage_report = candidate_adjudication
    stage_counts = description_stage_report.get("counts", {}) or {}
    grounded_failures = int(stage_counts.get("failed", 0) or 0) + int(stage_counts.get("ambiguous", 0) or 0)
    description_stage_failures = _deduplicated_description_failures(
        description_stage_report or None, adjudication_stage_report or None, grounded_failures
    )
    description_stage_broken = any(str(problem.get("status")) == "failed" for problem in description_stage_problems)
    problem_msg = source_problem_message(problems)
    if problem_msg:
        lines.append(problem_msg)

    open_entry = next((e for e in log if e.get("stage") == "open-review"), None)
    open_failed = False
    if open_entry is not None:
        detail = open_entry.get("detail", {}) or {}
        open_failed = not detail.get("opened", False)
        if open_failed:
            lines.append(open_failure_fallback())

    repository_entries = [e for e in log if e.get("stage") == "repository-finalize"]
    persistence_established = bool(repository_entries) and all(
        e.get("status") in ("ok", "noop") for e in repository_entries
    )
    if persistence_established:
        lines.append("Результаты сохранены.")

    lines.append(
        completion_message(
            outcome=outcome_value,
            new_count=int(delta_counts.get("new", 0) or 0),
            updated_count=int(delta_counts.get("upgraded", 0) or 0) + int(delta_counts.get("changed", 0) or 0),
            previously_seen=int(delta_counts.get("unchanged", 0) or 0),
            attention_required=bool(readiness_facts.get("attention_required", False)),
            review_available=bool(getattr(result, "review_available", False)) and not open_failed,
            description_coverage=getattr(result, "description_coverage", None),
            reused_source_count=int(getattr(result, "reused_source_count", 0) or 0),
            collecting_source_count=int(getattr(result, "collecting_source_count", 0) or 0),
            pipeline_path=getattr(result, "pipeline_path", None),
            grounded_description_counts=grounded_counts,
            grounded_description_reason_counts=grounded_reason_counts,
            description_provenance_counts=provenance_counts if isinstance(provenance_counts, dict) else None,
            description_stage_failures=description_stage_failures,
            description_stage_broken=description_stage_broken,
            attention_reasons=tuple(getattr(result, "attention_reasons", ()) or ()),
            source_statuses=source_statuses,
            adjudication_stage_report=adjudication_stage_report or None,
            description_stage_report=description_stage_report or None,
            description_caveats=run_summary.get("description_caveats", []),
        )
    )
    return "\n".join(lines)
