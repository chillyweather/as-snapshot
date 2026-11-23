"""How many attempts in a row each source has failed, across runs (#1125).

One failed night is noise; a source that fails attempt after attempt is
broken. The weekly run keeps, per source, the number of attempts in a row that
ended in failure, the date of the last success and the last reason code, in
the State Store collection ``SOURCE_FAILURE_STREAKS``. Only a source at or over
the threshold makes the night need attention; a source below it is listed and
nothing more.

The counting rule:

* An **attempt** is a source the run tried to collect, whether it got as far
  as replay (``source-runs.json``) or stopped at capture (the capture ledger).
  ``collected``, ``precollected`` and ``codex-captured`` are successes;
  ``failed`` is a failure, and so is every ``skipped`` run but a deliberate
  one (a blocked page, a captcha, an HTTP 403 and a missing credential are
  failures, with or without a problem code).
* A source the run did not try leaves its streak alone: ``not-due`` (an annual
  or monthly list outside its collection weekday), ``reused`` (served from an
  earlier operation's artifact), ``inactive``, ``ignored``, and a deliberate
  skip: a source whose ``access_status`` says to skip it, Kirkus left to its
  background pass, a ranking list whose next edition is not out yet
  (``ranking_list_not_yet_refreshed``) and a prize page between two editions
  (``between_editions``). So a weekly or annual source never reaches the
  threshold by being idle.
* **One failure per source per run date.** A second failed attempt on a date
  that already counted a failure (a manual run after the nightly one, a
  resumed operation) updates the reason but does not lengthen the streak, so
  three bad runs in one morning are one bad day, not a broken source.
* A success resets the streak to zero. A run in which a source succeeds after
  a streak at or over the threshold reports the recovery; so does another run
  on the same date (a resumed operation, a manual run), and no later date.
* **A widespread failure is never noise.** When at least two sources failed
  and they are more than half of the sources the run attempted, the run asks
  for attention whatever the streaks: that is a broken run (a network outage,
  an expired login, a model that is down), not one bad source. On 2026-11-10
  nearly every source failed in one run.

Kirkus, Publishers Marketplace and Publishers Weekly are collected by their
own passes. Their source runs are counted like any other, but a source whose
pass reported a problem in the run asks for attention whatever its streak, as
before: a missed PW issue or PM email batch is content that a later night does
not bring back (#1117), and those passes keep their own attempt records. The
view names those sources (``pass_problem_sources``), together with any of
them whose collection is older than its age limit (``*_stale_collection``),
so the doctor and run-health judge them as the run does.

Every value the module writes is a source name, a date, an integer or a
reason token; never a book title and never prose from a page.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from autoscout.source_rules import is_skipped_source
from autoscout.source_run_status import KIRKUS_DEFERRED_SKIP_REASON

THRESHOLD_CONFIG_KEY = "source_failure_streak_threshold"
DEFAULT_THRESHOLD = 3
# The key of the run-summary block that carries this run's view of the streaks.
RUN_SUMMARY_KEY = "source_failure_streaks"

SUCCESS = "success"
FAILURE = "failure"
NOT_ATTEMPTED = "not_attempted"

_SUCCESS_STATUSES = frozenset({"collected", "precollected", "codex-captured"})
# A skip with one of these codes is no attempt: ``ranking_freshness.NOT_YET_REFRESHED_SKIP_STATUS``
# and ``source_page_identity.BETWEEN_EDITIONS_SAFE_STATUS``, spelled out to keep this module light
# enough for the doctor and the morning report (a test keeps them equal).
DELIBERATE_SKIP_CODES = frozenset({"ranking_list_not_yet_refreshed", "between_editions"})
# A Kirkus, PM or PW collection older than its age limit: issues or emails are being missed now (#1117), so the
# source asks for attention like a problem its pass reported, whatever its streak.
STALE_PASS_COLLECTION_CODES = frozenset({"kirkus_stale_collection", "pm_stale_collection", "pw_stale_collection"})

_REASON_TOKEN = re.compile(r"[a-z][a-z0-9_]{1,63}")
# The review's fixed Russian reasons (``source_problem_notes``); a stored phrase is shown only when it is one of these.
_REASON_PHRASES = frozenset(
    {
        "между сезонами: новый список ещё не опубликован",
        "список не обновился с прошлого запуска",
        "новый выпуск списка ещё не вышел",
        "свежий выпуск списка ещё не вышел",
        "страница не найдена",
        "сайт не пускает сборщик",
        "страница слишком большая для сбора",
        "сайт не ответил вовремя",
        "найденные книги не прошли проверку",
        "страница изменила вид",
        "не удалось собрать список",
        "пропущен при сборе",
    }
)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def threshold_error(value: object) -> str:
    """Why *value* is not a streak threshold, or "" when it is one."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return f"'{THRESHOLD_CONFIG_KEY}' must be a whole number of attempts, 1 or more"
    return ""


def configured_threshold(config: Mapping[str, Any] | None) -> int:
    """The weekly config's threshold, else the default of 3 attempts in a row."""
    if not isinstance(config, Mapping) or THRESHOLD_CONFIG_KEY not in config:
        return DEFAULT_THRESHOLD
    value = config[THRESHOLD_CONFIG_KEY]
    error = threshold_error(value)
    if error:
        raise ValueError(error)
    return int(value)


def reason_code(source_run: Mapping[str, Any]) -> str:
    """The run's problem code (``safe_failure_status``), or a generic code from its status."""
    debug = source_run.get("debug_metadata")
    code = debug.get("safe_failure_status") if isinstance(debug, Mapping) else None
    if isinstance(code, str) and _REASON_TOKEN.fullmatch(code):
        return code
    return "source_skipped" if source_run.get("status") == "skipped" else "source_failed"


def attempt_kind(source_run: Mapping[str, Any]) -> str:
    """Whether a source run was a successful attempt, a failed one, or no attempt at all."""
    status = str(source_run.get("status", "")).strip()
    if status in _SUCCESS_STATUSES:
        return SUCCESS
    if status == "failed":
        return FAILURE
    if status == "skipped":
        debug = source_run.get("debug_metadata")
        code = debug.get("safe_failure_status") if isinstance(debug, Mapping) else None
        if isinstance(code, str) and code in DELIBERATE_SKIP_CODES:
            return NOT_ATTEMPTED
        if source_run.get("reason") == KIRKUS_DEFERRED_SKIP_REASON or is_skipped_source(dict(source_run)):
            return NOT_ATTEMPTED
        return FAILURE
    return NOT_ATTEMPTED


@dataclass(frozen=True)
class Streak:
    """One source's record in the State Store."""

    source_name: str
    consecutive_failures: int = 0
    streak_started: str | None = None
    last_failure_date: str | None = None
    last_success_date: str | None = None
    last_reason_code: str = ""
    # The date a streak at or over the threshold ended, and its length, so a second run that date repeats it.
    recovered_on: str | None = None
    recovered_after: int = 0

    def to_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "source_name": self.source_name,
            "consecutive_failures": self.consecutive_failures,
            "streak_started": self.streak_started,
            "last_failure_date": self.last_failure_date,
            "last_success_date": self.last_success_date,
            "last_reason_code": self.last_reason_code,
        }
        if self.recovered_on is not None:
            record["recovered_on"] = self.recovered_on
            record["recovered_after"] = self.recovered_after
        return record

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> Streak:
        def date_or_none(value: Any) -> str | None:
            return value if isinstance(value, str) and _DATE.fullmatch(value) else None

        count = record.get("consecutive_failures", 0)
        code = record.get("last_reason_code", "")
        recovered_after = record.get("recovered_after", 0)
        recovered_on = date_or_none(record.get("recovered_on"))
        if not isinstance(recovered_after, int) or isinstance(recovered_after, bool) or recovered_after < 1:
            recovered_on, recovered_after = None, 0
        return cls(
            source_name=str(record.get("source_name", "")),
            consecutive_failures=count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 0,
            streak_started=date_or_none(record.get("streak_started")),
            last_failure_date=date_or_none(record.get("last_failure_date")),
            last_success_date=date_or_none(record.get("last_success_date")),
            last_reason_code=code if isinstance(code, str) and _REASON_TOKEN.fullmatch(code) else "",
            recovered_on=recovered_on,
            recovered_after=recovered_after if recovered_on is not None else 0,
        )


@dataclass(frozen=True)
class StreakUpdate:
    """The records to store and this run's view of them."""

    records: list[dict[str, Any]]
    view: dict[str, Any]


def update_streaks(
    stored: Iterable[Mapping[str, Any]],
    source_runs: Iterable[Any],
    run_date: str,
    threshold: int = DEFAULT_THRESHOLD,
    pass_problem_sources: Iterable[str] = (),
) -> StreakUpdate:
    """Apply one run's source runs to the stored streaks; see the module docstring for the rule.

    The view lists every source the run reports as failed or skipped (with
    ``counted`` false for a skip that was no attempt), broken ones first, and
    every source that recovered in this run. Its reason is a code and one of
    the review's fixed Russian phrases, never the recorded reason text.
    """
    from autoscout.source_problem_notes import editor_source_reason

    streaks = {
        streak.source_name: streak
        for streak in (Streak.from_record(record) for record in stored if isinstance(record, Mapping))
        if streak.source_name
    }
    failing: list[dict[str, Any]] = []
    recovered: list[dict[str, Any]] = []
    attempts = {SUCCESS: 0, FAILURE: 0}
    passes = {str(name) for name in pass_problem_sources if str(name).strip()}
    # One record per source name, the last one: the ledgers come capture first and replay last.
    by_name: dict[str, Mapping[str, Any]] = {}
    for source_run in source_runs:
        if isinstance(source_run, Mapping) and str(source_run.get("source_name", "")).strip():
            by_name.pop(str(source_run.get("source_name", "")), None)
            by_name[str(source_run.get("source_name", ""))] = source_run
    for name, source_run in by_name.items():
        status = str(source_run.get("status", "")).strip()
        kind = attempt_kind(source_run)
        if kind in attempts:
            attempts[kind] += 1
        previous = streaks.get(name, Streak(name))
        if kind == SUCCESS:
            recovered_on, recovered_after = None, 0
            if previous.consecutive_failures >= threshold:
                recovered_on, recovered_after = run_date, previous.consecutive_failures
            elif previous.consecutive_failures == 0 and previous.recovered_on == run_date:
                recovered_on, recovered_after = run_date, previous.recovered_after
            if recovered_on is not None:
                recovered.append(
                    {
                        "source_name": name,
                        "failed_attempts": recovered_after,
                        "last_failure_date": previous.last_failure_date,
                    }
                )
            streaks[name] = Streak(
                name,
                last_failure_date=previous.last_failure_date,
                last_success_date=run_date,
                recovered_on=recovered_on,
                recovered_after=recovered_after,
            )
            continue
        current = previous
        if kind == FAILURE:
            same_day = previous.consecutive_failures > 0 and previous.last_failure_date == run_date
            count = previous.consecutive_failures if same_day else previous.consecutive_failures + 1
            current = Streak(
                name,
                consecutive_failures=count,
                streak_started=previous.streak_started if previous.consecutive_failures > 0 else run_date,
                last_failure_date=run_date,
                last_success_date=previous.last_success_date,
                last_reason_code=reason_code(source_run),
            )
            streaks[name] = current
        if status in {"failed", "skipped"}:
            if reason_code(source_run) in STALE_PASS_COLLECTION_CODES:
                passes.add(name)
            failing.append(
                {
                    "source_name": name,
                    "status": status,
                    "reason_code": reason_code(source_run),
                    "reason_ru": editor_source_reason(source_run, "failed" if status == "failed" else "skipped"),
                    "counted": kind == FAILURE,
                    "consecutive_failures": current.consecutive_failures,
                    "last_success_date": current.last_success_date,
                    "broken": kind == FAILURE and current.consecutive_failures >= threshold,
                }
            )
    failing.sort(key=lambda entry: (not entry["broken"], -entry["consecutive_failures"], entry["source_name"]))
    records = [streaks[name].to_record() for name in sorted(streaks)]
    attempted = attempts[SUCCESS] + attempts[FAILURE]
    return StreakUpdate(
        records=records,
        view={
            "threshold": threshold,
            "run_date": run_date,
            "attempted": attempted,
            "failed_attempts": attempts[FAILURE],
            "widespread": attempts[FAILURE] >= 2 and attempts[FAILURE] * 2 > attempted,
            "pass_problem_sources": sorted(passes),
            "failing": failing,
            "recovered": recovered,
        },
    )


def run_view(run_summary: Any) -> dict[str, Any] | None:
    """This run's streak view from its run summary, or ``None`` for a run made before #1125 or without state."""
    if not isinstance(run_summary, Mapping):
        return None
    view = run_summary.get(RUN_SUMMARY_KEY)
    if not isinstance(view, Mapping) or not isinstance(view.get("failing"), list):
        return None

    def objects(value: Any) -> list[dict[str, Any]]:
        return [dict(item) for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []

    passes = view.get("pass_problem_sources")
    return {
        **view,
        "failing": objects(view.get("failing")),
        "recovered": objects(view.get("recovered")),
        "pass_problem_sources": [str(name) for name in passes] if isinstance(passes, list) else [],
    }


def tolerated_source_names(view: Mapping[str, Any] | None) -> frozenset[str]:
    """The sources whose failure or skip this run lists without asking for attention: those below the threshold.

    None in a run whose failure was widespread, and never a source whose Kirkus, PM or PW pass reported a problem.
    """
    if view is None or view.get("widespread"):
        return frozenset()
    return frozenset(
        str(entry.get("source_name", ""))
        for entry in view.get("failing", [])
        if isinstance(entry, Mapping) and not entry.get("broken")
    ) - set(view.get("pass_problem_sources") or ())


def _asks_attention(view: Mapping[str, Any], entry: Mapping[str, Any]) -> bool:
    return bool(entry.get("broken")) or str(entry.get("source_name", "")) in set(view.get("pass_problem_sources") or ())


def needs_attention(view: Mapping[str, Any] | None) -> bool:
    """Whether the run's sources ask for attention: a widespread failure, a source at or over the threshold,
    or a failed source whose pass reported a problem."""
    if view is None:
        return False
    return bool(view.get("widespread") or view.get("pass_problem_sources")) or any(
        _asks_attention(view, entry) for entry in view.get("failing", []) if isinstance(entry, Mapping)
    )


def broken_entries(view: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The view's sources at or over the threshold, longest streak first."""
    if view is None:
        return []
    return [dict(entry) for entry in view.get("failing", []) if isinstance(entry, Mapping) and entry.get("broken")]


# ── Russian wording, shared by the doctor and the morning report ──────────────


def _success_ru(last_success_date: Any) -> str:
    if isinstance(last_success_date, str) and _DATE.fullmatch(last_success_date):
        return f"последний успех {last_success_date}"
    return "успешных попыток ещё не записано"


def _reason_ru(entry: Mapping[str, Any], with_code: bool) -> str:
    phrase = entry.get("reason_ru")
    phrase = phrase if isinstance(phrase, str) and phrase in _REASON_PHRASES else "проблема при сборе"
    code = str(entry.get("reason_code", ""))
    return f"{phrase}, {code}" if with_code and _REASON_TOKEN.fullmatch(code) else phrase


def entry_ru(entry: Mapping[str, Any], *, with_code: bool = False) -> str:
    """One failing source in a line: name, reason and streak; *with_code* adds the reason code for the operator."""
    name = str(entry.get("source_name", ""))
    reason = _reason_ru(entry, with_code)
    if not entry.get("counted"):
        return f"{name} ({reason}; не считается неудачной попыткой)"
    count = entry.get("consecutive_failures")
    count = count if isinstance(count, int) and count > 0 else 1
    return f"{name} ({reason}; неудачных попыток подряд: {count}, {_success_ru(entry.get('last_success_date'))})"


def recovered_ru(entry: Mapping[str, Any]) -> str:
    name = str(entry.get("source_name", ""))
    count = entry.get("failed_attempts")
    count = count if isinstance(count, int) and count > 0 else 0
    return f"{name} (до этого неудачных попыток подряд: {count})"


def _count(view: Mapping[str, Any], key: str) -> int:
    value = view.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def view_lines_ru(view: Mapping[str, Any] | None, *, with_code: bool = False) -> list[str]:
    """The streak facts of a run as Russian lines: the lines that ask for attention, then the others."""
    return attention_lines_ru(view, with_code=with_code) + other_lines_ru(view, with_code=with_code)


def attention_lines_ru(view: Mapping[str, Any] | None, *, with_code: bool = False) -> list[str]:
    """The lines that ask for attention: a widespread failure, then the broken sources."""
    if view is None:
        return []
    lines: list[str] = []
    if view.get("widespread"):
        lines.append(
            f"Не удалось собрать {_count(view, 'failed_attempts')} из {_count(view, 'attempted')} источников "
            "этого запуска: похоже на общий сбой, а не на отдельные источники."
        )
    threshold = view.get("threshold", DEFAULT_THRESHOLD)
    threshold = threshold if isinstance(threshold, int) and threshold > 0 else DEFAULT_THRESHOLD
    broken = [
        entry_ru(entry, with_code=with_code)
        for entry in view.get("failing", [])
        if isinstance(entry, Mapping) and entry.get("broken")
    ]
    if broken:
        lines.append(f"Не работают {threshold} и более попыток подряд: {'; '.join(broken)}.")
    passes = [
        entry_ru(entry, with_code=with_code)
        for entry in view.get("failing", [])
        if isinstance(entry, Mapping) and not entry.get("broken") and _asks_attention(view, entry)
    ]
    listed = {str(entry.get("source_name", "")) for entry in view.get("failing", []) if isinstance(entry, Mapping)}
    passes += [str(name) for name in view.get("pass_problem_sources") or () if str(name) not in listed]
    if passes:
        lines.append(f"Сбои отдельного прохода сбора (Kirkus, PM, PW): {'; '.join(passes)}.")
    return lines


def other_lines_ru(view: Mapping[str, Any] | None, *, with_code: bool = False) -> list[str]:
    """The listed-only lines: failures below the threshold, then recoveries."""
    if view is None:
        return []
    minor = [
        entry_ru(entry, with_code=with_code)
        for entry in view.get("failing", [])
        if isinstance(entry, Mapping) and not _asks_attention(view, entry)
    ]
    recovered = [recovered_ru(entry) for entry in view.get("recovered", []) if isinstance(entry, Mapping)]
    lines: list[str] = []
    if minor:
        label = "Другие сбои этого запуска" if view.get("widespread") else "Сбои, пока не требующие внимания"
        lines.append(f"{label}: {'; '.join(minor)}.")
    if recovered:
        lines.append(f"Снова работают: {'; '.join(recovered)}.")
    return lines
