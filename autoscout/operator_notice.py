"""The morning report of the scheduled nightly run (#1057).

The server's timer starts the run every night with nobody watching. Every
scheduled run, whatever its outcome, sends exactly one short report when it
ends, so the operator reads one mail each morning: successful, needs
attention, stopped, or skipped (the operator's decision of 2026-11-14). The
report also states the Kirkus pass of the night, read from its attempt
record; the pass itself sends nothing.

The text is built by a pure function from the run result: the verdict, the
counts the result carries, the named sources with their problem category,
the failed stage and reason token. It never carries a book title, an author,
a path, a credential or raw error output; a reason that is not a plain token
is sent as ``unrecognized_reason``, and every number is an integer read from
the result.

The text reaches the operator through a transport, a function taking the
subject and the body. Email over SMTP is the one transport today, configured
only by the ``AUTOSCOUT_NOTIFY_*`` environment variables. Without them the
report is printed instead, so the service journal still holds it. A delivery
error is reported in one line and never changes the exit code.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import smtplib
import ssl
import sys
import tempfile
import time
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from autoscout.toolchain_preflight import STAGE_ZERO_NAME

ENV_HOST = "AUTOSCOUT_NOTIFY_SMTP_HOST"
ENV_PORT = "AUTOSCOUT_NOTIFY_SMTP_PORT"
ENV_USER = "AUTOSCOUT_NOTIFY_SMTP_USER"
ENV_PASSWORD = "AUTOSCOUT_NOTIFY_SMTP_PASSWORD"
ENV_FROM = "AUTOSCOUT_NOTIFY_FROM"
ENV_TO = "AUTOSCOUT_NOTIFY_TO"
NOTIFY_ENV = (ENV_HOST, ENV_PORT, ENV_USER, ENV_PASSWORD, ENV_FROM, ENV_TO)

DEFAULT_SMTP_PORT = 587
SMTPS_PORT = 465
SMTP_TIMEOUT_SECONDS = 15

# The weekly workflow's stage of the gate on new code (#1124).
CODE_GATE_STAGE = "code-gate"

GENERIC_REASON = "unrecognized_reason"
UNKNOWN_STAGE = "unknown"
RUN_JOURNAL = "journalctl --user -u autoscout-run.service"
# The night's second attempt has its own unit (#1160).
SECOND_ATTEMPT_JOURNAL = "journalctl --user -u autoscout-run-retry.service"
SECOND_ATTEMPT_SUBJECT = "повторная попытка"

# A reason token is a code, never prose: a title, a path, a message or an
# error output has a capital, a space, a slash or a dot, and fails this.
_REASON_TOKEN = re.compile(r"[a-z][a-z0-9_]{1,63}")
_STAGE_TOKEN = re.compile(r"[a-z][a-z0-9-]{1,63}")
_ATTENTION_REASONS = ("readiness", "sources", "descriptions")
_ATTENTION_REASONS_RU = {
    "readiness": "готовность обзора",
    "sources": "источники",
    "descriptions": "описания",
}
# The short category of a source problem, as the completion text names them.
_SOURCE_PROBLEM_RU = {
    "failed": "не удалось проверить",
    "skipped": "пропущено",
    "incomplete": "неполный охват",
    "unusual_count": "необычное число книг",
}
_SOURCE_NAME_LIMIT = 80

VERDICT_SUCCESS = "успешно"
VERDICT_ATTENTION = "требует внимания"
VERDICT_STOPPED = "остановлен"
VERDICT_SKIPPED = "пропущен"
_VERDICT_BY_OUTCOME = {
    "completed/no-attention": VERDICT_SUCCESS,
    "completed/attention-required": VERDICT_ATTENTION,
    "skipped/run-in-progress": VERDICT_SKIPPED,
}

Transport = Callable[[str, str], None]

# The file in which a scheduled job records that it delivered its report (#1175). The wrapper names one
# per start of a unit, after systemd's invocation id; the unit that reports a job systemd stopped reads
# it, so a job that reported for itself never gets a second report.
REPORT_RECORD_ENV = "AUTOSCOUT_REPORT_RECORD"
# The lines deliver() prints for a report that reached the operator, the same in every version since
# #1057; the journal of a unit start that holds one shows its report was delivered (#1175).
REPORT_SENT_LINE = "Отчёт отправлен."
REPORT_NOT_CONFIGURED_PREFIX = "Отправка отчёта не настроена"
# Records older than this are removed when a new one is written.
REPORT_RECORD_KEEP_SECONDS = 60 * 24 * 3600


@dataclass(frozen=True)
class Report:
    subject: str
    body: str


def safe_reason(value: Any) -> str:
    """*value* when it is a plain reason token, otherwise the generic reason."""
    return value if isinstance(value, str) and _REASON_TOKEN.fullmatch(value) else GENERIC_REASON


def _safe_stage(value: Any) -> str:
    return value if isinstance(value, str) and _STAGE_TOKEN.fullmatch(value) else UNKNOWN_STAGE


def _outcome_value(result: Any) -> str:
    outcome = getattr(result, "outcome", "")
    return str(getattr(outcome, "value", outcome) or "")


def failed_run_reason(result: Any) -> str:
    """The reason token of a run that ended intervention-required.

    Stage 0 logs the Codex probe's message, which is prose, so its reason is
    the probe's own reason code. Every other stage logs its reason on the
    failed entry of that stage.
    """
    stage = getattr(result, "failed_stage", None)
    if stage == STAGE_ZERO_NAME:
        summary = getattr(result, "run_summary", {}) or {}
        preflight = summary.get("toolchain_preflight", {}) if isinstance(summary, dict) else {}
        codex = preflight.get("codex", {}) if isinstance(preflight, dict) else {}
        if isinstance(codex, dict):
            return safe_reason(codex.get("reason_code"))
    for entry in reversed(list(getattr(result, "log", []) or [])):
        if isinstance(entry, dict) and entry.get("stage") == stage and entry.get("status") == "failed":
            return safe_reason(entry.get("reason"))
    return GENERIC_REASON


def failure_line(stage: str, reason: str) -> str:
    """The one line naming where and why a scheduled job stopped; the journal keeps it."""
    return f"Остановлено на этапе {_safe_stage(stage)}, причина: {safe_reason(reason)}."


def run_failure_line(result: Any) -> str:
    return failure_line(getattr(result, "failed_stage", "") or "", failed_run_reason(result))


def _count(mapping: Any, key: str) -> int:
    """An integer count from a result mapping; anything else is 0, never text."""
    if not isinstance(mapping, Mapping):
        return 0
    try:
        return max(0, int(mapping.get(key, 0) or 0))
    except (TypeError, ValueError):
        return 0


def _source_name(value: Any) -> str:
    return " ".join(str(value).split())[:_SOURCE_NAME_LIMIT]


def _streak_view(result: Any) -> dict[str, Any] | None:
    """The run's failure-streak view with every source name cut as the report cuts names, or ``None`` (#1125).

    A source a Kirkus, PM or PW pass reported a problem for is left out: that
    problem asks for attention whatever the streak, so the report names it
    with the other source problems.
    """
    from autoscout.source_failure_streaks import run_view

    view = run_view(getattr(result, "run_summary", None))
    if view is None:
        return None
    passes = {_source_name(name) for name in getattr(result, "pass_problem_sources", ()) or ()}
    failing = [
        {**entry, "source_name": _source_name(entry.get("source_name", ""))}
        for entry in view.get("failing", [])
        if isinstance(entry, dict) and _source_name(entry.get("source_name", "")) not in passes
    ]
    recovered = [
        {**entry, "source_name": _source_name(entry.get("source_name", ""))}
        for entry in view.get("recovered", []) or []
        if isinstance(entry, dict)
    ]
    # The report names a problem the pass reported with the other source problems, so the streak lines leave
    # it out; a stale pass collection found at capture stays, under the pass line.
    still_named = [name for name in view.get("pass_problem_sources", []) if _source_name(name) not in passes]
    return {**view, "failing": failing, "recovered": recovered, "pass_problem_sources": still_named}


def _source_problem_line(result: Any, streak_names: frozenset[str] = frozenset()) -> str:
    """Each named source with its problem category, once; no detail text, which can name a book.

    A source in *streak_names* is left out: the streak lines name it.
    """
    named: list[str] = []
    for problem in getattr(result, "source_problems", []) or []:
        if not isinstance(problem, dict):
            continue
        name = _source_name(problem.get("source_name", ""))
        if not name or name in streak_names:
            continue
        entry = f"{name} ({_SOURCE_PROBLEM_RU.get(str(problem.get('status', '')), 'проблема')})"
        if entry not in named:
            named.append(entry)
    return f"Источники с проблемами: {', '.join(named)}." if named else ""


def _completed_lines(result: Any) -> list[str]:
    from autoscout.content_description import coverage_denominator
    from autoscout.content_description import verified_description_count

    delta = getattr(result, "delta_counts", {}) or {}
    lines = [
        f"Новых книг: {_count(delta, 'new')}. Обновлено книг: {_count(delta, 'upgraded') + _count(delta, 'changed')}."
    ]
    coverage = getattr(result, "description_coverage", {}) or {}
    if isinstance(coverage, Mapping):
        try:
            denominator, verified = int(coverage_denominator(coverage)), int(verified_description_count(coverage))
        except (TypeError, ValueError):
            denominator = verified = 0
        if denominator > 0:
            lines.append(f"Описаний на русском: {verified} из {denominator}.")
    from autoscout.source_failure_streaks import attention_lines_ru
    from autoscout.source_failure_streaks import other_lines_ru

    view = _streak_view(result)
    # A widespread failure and the broken sources come first; the other source problems follow, then the
    # failures below the threshold and the recoveries.
    lines.extend(attention_lines_ru(view, with_code=True))
    streak_names = frozenset(entry["source_name"] for entry in view["failing"]) if view else frozenset()
    sources = _source_problem_line(result, streak_names)
    if sources:
        lines.append(sources)
    lines.extend(other_lines_ru(view, with_code=True))
    description_problems = len(list(getattr(result, "description_stage_problems", []) or []))
    if description_problems:
        # Description problems name books, so only their count is sent.
        lines.append(f"Описания с проблемами: {description_problems}.")
    return lines


def _verdict_line(result: Any, verdict: str, run_date: str, *, ran_on_earlier_code: bool = False) -> str:
    if verdict == VERDICT_SUCCESS:
        return f"Ночной поиск {run_date} завершён успешно."
    if verdict == VERDICT_ATTENTION:
        reasons = [reason for reason in _ATTENTION_REASONS if reason in tuple(getattr(result, "attention_reasons", ()))]
        names = [_ATTENTION_REASONS_RU[reason] for reason in reasons]
        if ran_on_earlier_code:
            names.insert(0, _EARLIER_CODE_ATTENTION_RU)
        named = ", ".join(names) or "причина не названа"
        return f"Ночной поиск {run_date} завершён, но требует внимания: {named}."
    if verdict == VERDICT_SKIPPED:
        return f"Ночной поиск {run_date} пропущен: в это время уже шёл другой поиск."
    line = f"Ночной поиск {run_date} не завершён. {run_failure_line(result)}"
    if getattr(result, "failed_stage", None) == CODE_GATE_STAGE:
        line += f"\n{_code_gate_line(result)}"
    return line


def _code_gate_line(result: Any) -> str:
    """The commit that failed the gate on new code (#1124), and what the next night does."""
    from autoscout.code_gate import REASON_ENVIRONMENT_OUT_OF_DATE
    from autoscout.code_gate import REASON_ENVIRONMENT_UPDATE_FAILED
    from autoscout.code_gate import REASON_ENVIRONMENT_UPDATE_TIMED_OUT
    from autoscout.code_gate import short_commit

    commit = reason = refused = ""
    for entry in reversed(list(getattr(result, "log", []) or [])):
        if isinstance(entry, dict) and entry.get("stage") == CODE_GATE_STAGE and entry.get("status") == "failed":
            detail = entry.get("detail")
            commit = short_commit(detail.get("commit")) if isinstance(detail, dict) else ""
            refused = safe_reason(detail.get("fallback_refused")) if isinstance(detail, dict) else ""
            refused = "" if refused == GENERIC_REASON else refused
            reason = str(entry.get("reason", ""))
            break
    if reason in (REASON_ENVIRONMENT_UPDATE_FAILED, REASON_ENVIRONMENT_UPDATE_TIMED_OUT):
        how = "не уложилась в отведённое время" if reason == REASON_ENVIRONMENT_UPDATE_TIMED_OUT else "не удалась"
        return (
            f"Новый код (коммит {commit or 'неизвестен'}) не проверен: окружение .venv на сервере отстало "
            f"от версий в pyproject.toml, а установка пакетов {how}. Сам код не виноват. "
            "Поиск не запускался, данные не изменены. Следующий ночной запуск повторит установку; "
            "повторить сейчас на сервере: ./scripts/code-gate --run."
        ) + _refused_fallback_text(refused)
    if reason == REASON_ENVIRONMENT_OUT_OF_DATE:
        line = (
            f"Новый код (коммит {commit or 'неизвестен'}) не проверен: окружение .venv на сервере "
            "не совпадает с версиями в pyproject.toml. Поиск не запускался, данные не изменены. "
            'Нужно на сервере: .venv/bin/python -m pip install -e ".[dev]", затем ./scripts/code-gate --run.'
        )
    else:
        line = (
            f"Новый код (коммит {commit or 'неизвестен'}) не прошёл ./scripts/check на сервере. "
            "Поиск не запускался, данные не изменены. Следующий ночной запуск проверит код снова."
        )
    return line + _refused_fallback_text(refused)


# Why a failed gate did not run the last code that passed here (#1159).
_FALLBACK_REFUSED_RU = {
    "last_pass_predates_fallback": "она проверена до того, как появился запуск предыдущей версии",
    "last_pass_unavailable": "её код не удалось подготовить",
    "new_commit_changes_files_the_run_reads": "новая версия меняет настройки или данные, которые читает и предыдущая",
    "new_commit_changes_state_store": "новая версия меняет код хранилища состояния, и совместимость данных не доказана",
    "venv_does_not_meet_last_pass_pins": "окружение .venv уже не совпадает с версиями пакетов предыдущей версии",
    "fallback_not_confirmed": "перезапуск на ней не подтвердил, что всё осталось как при решении",
    "checkout_moved_during_check": "пока шла проверка, на сервер пришёл ещё более новый код",
}


def _refused_fallback_text(refused: str) -> str:
    if not refused:
        return ""
    why = _FALLBACK_REFUSED_RU.get(refused, "причина не названа")
    return f" Предыдущая версия кода, прошедшая проверку, тоже не запускалась: {why}."


_EARLIER_CODE_ATTENTION_RU = "новый код не прошёл проверку"
# What happened to the new code of a night that ran on the last code that passed here (#1159).
_NEW_CODE_FAILED_RU = {
    "new_code_failed_check": "не прошла проверку ./scripts/check",
    "new_code_check_timed_out": "не уложилась во время, отведённое на проверку ./scripts/check",
    "server_venv_update_failed": "не проверена: не удалось обновить окружение .venv под её версии пакетов",
    "server_venv_update_timed_out": "не проверена: обновление окружения .venv под её версии пакетов не уложилось во время",
    "server_venv_out_of_date": "не проверена: окружение .venv не совпадает с её версиями пакетов",
}


def ran_on_earlier_code_text(reason: Any) -> str:
    """The plain Russian statement that a night ran on the last code that passed here, and why (#1159)."""
    why = _NEW_CODE_FAILED_RU.get(str(reason), "не прошла проверку на сервере")
    return (
        f"Поиск шёл на предыдущей версии кода, прошедшей проверку на этом сервере: новая версия {why}. "
        "Следующий ночной запуск проверит новый код снова."
    )


def ran_on_earlier_code_line(result: Any) -> str:
    """The line of a night that ran on the last code that passed here (#1159); empty for any other night."""
    from autoscout.code_gate_fallback import STATUS_RAN_LAST_PASSED

    for entry in reversed(list(getattr(result, "log", []) or [])):
        if isinstance(entry, dict) and entry.get("stage") == CODE_GATE_STAGE:
            detail = entry.get("detail")
            if entry.get("status") == STATUS_RAN_LAST_PASSED and isinstance(detail, dict):
                return ran_on_earlier_code_text(detail.get("reason"))
            return ""
    return ""


_PACKAGE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+!-]{0,63}")
_PACKAGES_NAMED = 8


def environment_update_line(result: Any) -> str:
    """The .venv update the night's gate made before checking new code (#1130); empty when there was none.

    Only package names and versions are sent, each a plain token; anything
    else is left out.
    """
    update: Any = None
    for entry in reversed(list(getattr(result, "log", []) or [])):
        if isinstance(entry, dict) and entry.get("stage") == CODE_GATE_STAGE:
            detail = entry.get("detail")
            update = detail.get("environment_update") if isinstance(detail, dict) else None
            break
    if not isinstance(update, dict):
        return ""
    named: list[str] = []
    packages = update.get("packages")
    for change in packages if isinstance(packages, list) else []:
        if not isinstance(change, dict):
            continue
        name, before, after = (str(change.get(key, "")) for key in ("package", "before", "after"))
        if not _PACKAGE_TOKEN.fullmatch(name) or any(v and not _PACKAGE_TOKEN.fullmatch(v) for v in (before, after)):
            continue
        named.append(f"{name} {before or '—'} → {after or '—'}")
    text = "; ".join(named[:_PACKAGES_NAMED])
    if len(named) > _PACKAGES_NAMED:
        text += f" и ещё {len(named) - _PACKAGES_NAMED}"
    return "Окружение .venv на сервере обновлено ночью под новый код" + (f": {text}." if text else ".")


def kirkus_line(attempt: Mapping[str, str] | None, run_date: str) -> str:
    """The Kirkus pass of the night in one line, from its attempt record; ``None`` means no Kirkus source."""
    if attempt is None:
        return "Сбор Kirkus: источник не настроен."
    if attempt.get("attempted_date") != run_date:
        return "Сбор Kirkus: сегодня не проводился."
    status = attempt.get("status")
    reason = safe_reason(attempt.get("reason_code"))
    if status == "collected":
        return "Сбор Kirkus: собран."
    if status == "partial":
        return f"Сбор Kirkus: прерван на полпути ({reason}), следующий сбор продолжит."
    if status == "not_collected":
        return f"Сбор Kirkus: не удался, причина: {reason}. Поиск взял последний готовый результат."
    if status == "started":
        return "Сбор Kirkus: начат и не завершён (ещё идёт или был прерван)."
    return "Сбор Kirkus: состояние неизвестно."


_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def late_failure_leftovers_line(day: Any, *, published: bool) -> str:
    """The line on the unsaved results of an earlier run that failed late, which this run carried (#1161)."""
    run = f"незавершённого поиска {day}" if isinstance(day, str) and _DATE.fullmatch(day) else "незавершённого поиска"
    if published:
        return f"Сохранены и результаты {run}; ничего не удалено."
    return f"Результаты {run} ещё не сохранены; ничего не удалено, их сохранит следующий поиск."


def _carried_leftovers_line(result: Any, verdict: str) -> str:
    from autoscout.late_failure_leftovers import LATE_FAILURE_LEFTOVERS_STAGE

    for entry in list(getattr(result, "log", []) or []):
        if isinstance(entry, dict) and entry.get("stage") == LATE_FAILURE_LEFTOVERS_STAGE:
            detail = entry.get("detail")
            day = detail.get("earlier_operation_date") if isinstance(detail, dict) else ""
            return late_failure_leftovers_line(day, published=verdict in (VERDICT_SUCCESS, VERDICT_ATTENTION))
    return ""


def _report(verdict: str, run_date: str, lines: list[str], second_attempt: str) -> Report:
    """The report with its subject and journal line; a second attempt (#1160) says so in both."""
    subject = f"AutoScout: ночной поиск {run_date} — {verdict}"
    if second_attempt:
        subject += f" ({SECOND_ATTEMPT_SUBJECT})"
        lines = [lines[0], second_attempt, *lines[1:]]
    lines = [*lines, f"Журнал: {SECOND_ATTEMPT_JOURNAL if second_attempt else RUN_JOURNAL}"]
    return Report(subject=subject, body="\n".join(lines))


def run_report(
    result: Any, *, run_date: str, kirkus_attempt: Mapping[str, str] | None, second_attempt: str = ""
) -> Report:
    """The morning report of a scheduled run that ended with a workflow result, whatever its outcome.

    *second_attempt* is the line of the night's second attempt naming how the
    first one ended (#1160); empty for the first attempt.
    """
    verdict = _VERDICT_BY_OUTCOME.get(_outcome_value(result), VERDICT_STOPPED)
    # A night on the last code that passed here asks for attention: the new code needs a fix (#1159).
    earlier_code = ran_on_earlier_code_line(result)
    if earlier_code and verdict == VERDICT_SUCCESS:
        verdict = VERDICT_ATTENTION
    lines = [_verdict_line(result, verdict, run_date, ran_on_earlier_code=bool(earlier_code))]
    if earlier_code:
        lines.append(earlier_code)
    leftovers = _carried_leftovers_line(result, verdict)
    if leftovers:
        lines.append(leftovers)
    update = environment_update_line(result)
    if update:
        lines.append(update)
    if verdict in (VERDICT_SUCCESS, VERDICT_ATTENTION):
        lines.extend(_completed_lines(result))
    lines.append(kirkus_line(kirkus_attempt, run_date))
    return _report(verdict, run_date, lines, second_attempt)


def exit_code_of(exc: SystemExit) -> int:
    """The process exit code a ``SystemExit`` carries; a non-integer code exits 1, as Python does."""
    if exc.code is None:
        return 0
    return exc.code if isinstance(exc.code, int) else 1


def stopped_run_report(
    reason: str, *, run_date: str, kirkus_attempt: Mapping[str, str] | None, second_attempt: str = ""
) -> Report:
    """The morning report of a scheduled run stopped outside its stages, by an exception or a config exit."""
    lines = [
        f"Ночной поиск {run_date} не завершён. {failure_line(UNKNOWN_STAGE, reason)}",
        kirkus_line(kirkus_attempt, run_date),
    ]
    return _report(VERDICT_STOPPED, run_date, lines, second_attempt)


def smtp_transport(environ: Mapping[str, str]) -> Transport | None:
    """An SMTP transport configured by the ``AUTOSCOUT_NOTIFY_*`` variables; ``None`` when they are absent.

    Port 465 speaks TLS from the start; any other port upgrades with
    STARTTLS. A user and a password are optional, for a relay on the host.
    """
    host = environ.get(ENV_HOST, "").strip()
    sender = environ.get(ENV_FROM, "").strip()
    recipients = [address.strip() for address in environ.get(ENV_TO, "").split(",") if address.strip()]
    if not host or not sender or not recipients:
        return None
    port_text = environ.get(ENV_PORT, "").strip()
    user = environ.get(ENV_USER, "")
    password = environ.get(ENV_PASSWORD, "")

    def send(subject: str, body: str) -> None:
        port = int(port_text) if port_text else DEFAULT_SMTP_PORT
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = sender
        message["To"] = ", ".join(recipients)
        message.set_content(body)
        context = ssl.create_default_context()
        client: smtplib.SMTP
        if port == SMTPS_PORT:
            client = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_SECONDS, context=context)
        else:
            client = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_SECONDS)
        with client:
            if port != SMTPS_PORT:
                client.starttls(context=context)
            if user:
                client.login(user, password)
            client.send_message(message)

    return send


def deliver(
    report: Report,
    *,
    transport: Transport | None = None,
    environ: Mapping[str, str] | None = None,
    output: Callable[[str], None] = print,
) -> str:
    """Send *report* and say so in one line; returns ``sent``, ``not_configured`` or ``failed``.

    Never raises: a scheduled job's exit code is its own, whatever happens to
    its report.
    """
    environ = os.environ if environ is None else environ
    outcome = _send(report, transport, environ, output)
    # What the report printed reaches the journal before the record says it was delivered (#1175).
    with contextlib.suppress(Exception):
        sys.stdout.flush()
    # Recorded after the attempt, whatever its outcome, so a job killed while sending is reported
    # again rather than not at all (#1175).
    record = environ.get(REPORT_RECORD_ENV, "")
    if record:
        record_report(Path(record), outcome)
    return outcome


def _send(
    report: Report, transport: Transport | None, environ: Mapping[str, str], output: Callable[[str], None]
) -> str:
    try:
        send = transport or smtp_transport(environ)
        if send is None:
            output(f"{REPORT_NOT_CONFIGURED_PREFIX} (нет переменных {ENV_HOST}, {ENV_FROM}, {ENV_TO}); его текст:")
            output(report.subject)
            output(report.body)
            return "not_configured"
        send(report.subject, report.body)
    except Exception as exc:
        # The type alone: an SMTP error message can echo the login exchange.
        output(f"Отчёт не отправлен: {type(exc).__name__}.")
        return "failed"
    output(REPORT_SENT_LINE)
    return "sent"


def record_report(path: Path, outcome: str) -> None:
    """Record that the report of the unit start *path* names was delivered (#1175); never raises.

    The file appears whole or not at all: it is written beside its place and renamed into it.
    """
    tmp = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"outcome": outcome, "recorded_at": time.time()}) + "\n")
        os.replace(tmp, path)
        tmp = ""
    except Exception:  # a missing record means a second report at worst, never a changed exit
        pass
    finally:
        if tmp:
            with contextlib.suppress(OSError):
                Path(tmp).unlink(missing_ok=True)
    _prune_records(path.parent)


def _prune_records(folder: Path) -> None:
    try:
        cutoff = time.time() - REPORT_RECORD_KEEP_SECONDS
        for old in folder.iterdir():
            if old.is_file() and old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
    except OSError:
        pass
