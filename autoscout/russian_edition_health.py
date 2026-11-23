"""What the operator is told about the weekly run's Russian-edition stage (#1181).

Run health and the doctor read the stage's entry in ``workflow-log.json``,
from the latest attempt only: a retry of the same date appends to the log,
so the scan stops at the first entry of a later attempt (see
:data:`ATTEMPT_BOUNDARY_STAGES` and :func:`_starts_an_attempt`). They name a
stage that failed or was skipped for lack of time, and a source that
blocked the run or failed often enough to be given up. The stage is advisory: nothing here changes a verdict, the run's
readiness or an exit code, and none of it reaches the editor's review.
Nothing in the log can make the reader raise: an entry it cannot read is
skipped, and a log it cannot read gives no lines.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

from autoscout.russian_edition_sources import SOURCES
from autoscout.unexpected_error import UNEXPECTED_ERROR_STAGE

STAGE_NAME = "russian-edition-completion"
# The entry every attempt of a weekly run that gets past its repository steps writes.
ATTEMPT_START_STAGE = "operation-start"
# The stages an attempt logs only before its ``operation-start``: the repository
# preflight, the update and the code gate. A retry that fails at one of them
# logs that failure and its terminal entry but no ``operation-start``, and an
# entry of any of them after the stage's entry belongs to a later attempt.
ATTEMPT_BOUNDARY_STAGES = frozenset({ATTEMPT_START_STAGE, "repository-preflight", "repository-update", "code-gate"})
# The entries an attempt ends with. A retry that dies on an exception before any
# of its stages (a repository preflight that raises) logs only its
# ``unexpected-error`` entry, right after the earlier attempt's last one.
ATTEMPT_END_STAGES = frozenset({"terminal", UNEXPECTED_ERROR_STAGE})
# Why the weekly run skipped the stage: too little time was left before the run's limit.
SKIPPED_RUN_TIME_LIMIT = "run_time_limit"
# A source given up for the rest of the pass failed or blocked this many times in a row
# (``russian_edition_completion.SOURCE_FAILURE_LIMIT``; a test keeps them equal).
SOURCE_FAILURE_LIMIT = 3

SOURCE_NAMES_RU = {
    "livelib": "LiveLib",
    "wikidata": "Wikidata",
    "rsl": "РГБ",
    "litres": "ЛитРес",
}

_SAFE = re.compile(r"[A-Za-z0-9_.\-]{1,120}")


@dataclass(frozen=True)
class SourceProblem:
    source: str
    blocked: int
    failed: int
    not_asked: int


@dataclass(frozen=True)
class StageOutcome:
    """The stage's last logged outcome: its status, why it failed, and the sources with problems."""

    status: str
    failure: str = ""
    confirmed: int = 0
    sources: tuple[SourceProblem, ...] = field(default_factory=tuple)

    @property
    def has_problem(self) -> bool:
        return self.status != "ok" or bool(self.sources)

    @property
    def skipped(self) -> bool:
        return self.status == "skipped"


def _count(values: Mapping[str, Any], name: str) -> int:
    value = values.get(name)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def outcome_of(entry: Mapping[str, Any]) -> StageOutcome:
    """The outcome one workflow-log entry of the stage records."""
    raw_detail = entry.get("detail")
    detail: Mapping[str, Any] = raw_detail if isinstance(raw_detail, Mapping) else {}
    failure = next(
        (
            str(detail[key])
            for key in ("exception_type", "reason")
            if isinstance(detail.get(key), str) and _SAFE.fullmatch(detail[key])
        ),
        "",
    )
    problems: list[SourceProblem] = []
    raw_sources = detail.get("sources")
    sources: Mapping[str, Any] = raw_sources if isinstance(raw_sources, Mapping) else {}
    for source in SOURCES:
        counts = sources.get(source)
        if not isinstance(counts, Mapping):
            continue
        blocked, failed = _count(counts, "blocked"), _count(counts, "failed")
        # A block is always named; failures once there are enough of them to give the source up.
        if blocked or failed >= SOURCE_FAILURE_LIMIT:
            problems.append(SourceProblem(source, blocked, failed, _count(counts, "not_asked")))
    status = entry.get("status")
    return StageOutcome(
        status=status if isinstance(status, str) and _SAFE.fullmatch(status) else "unknown",
        failure=failure,
        confirmed=_count(detail, "confirmed"),
        sources=tuple(problems),
    )


def last_outcome(output_dir: Path) -> StageOutcome | None:
    """The stage's outcome in the latest attempt's log; ``None`` when the stage did not run in it."""
    from autoscout.mvp_run_evidence import read_sanitized_log

    log, _legacy = read_sanitized_log(output_dir)
    for index in range(len(log) - 1, -1, -1):
        stage = _stage_of(log[index])
        if stage is None:
            continue
        if stage == STAGE_NAME:
            return outcome_of(log[index])
        if _starts_an_attempt(stage, log[:index]):
            # A later attempt began after any earlier stage entry: that outcome is not this one's.
            return None
    return None


def _stage_of(entry: Any) -> str | None:
    """The entry's stage name; ``None`` for an entry that is not a mapping or names no stage."""
    if not isinstance(entry, Mapping):
        return None
    stage = entry.get("stage")
    return stage if isinstance(stage, str) else None


def _starts_an_attempt(stage: str, earlier: list[Any]) -> bool:
    """Whether an entry of *stage*, after the *earlier* entries, belongs to a later attempt.

    An ``unexpected-error`` entry ends the attempt it crashed, which may well
    have run the stage before it; it is a later attempt's only when the
    entry before it ended an earlier attempt.
    """
    if stage in ATTEMPT_BOUNDARY_STAGES:
        return True
    if stage != UNEXPECTED_ERROR_STAGE:
        return False
    previous = next((name for name in map(_stage_of, reversed(earlier)) if name is not None), None)
    return previous in ATTEMPT_END_STAGES


def health_lines(outcome: StageOutcome) -> list[str]:
    """The ``key=value`` lines run health prints for the stage."""
    lines = [f"russian_edition_completion={outcome.status}"]
    if outcome.skipped:
        lines.append(f"russian_edition_completion_skipped={outcome.failure or 'unknown'}")
    elif outcome.status != "ok":
        lines.append(f"russian_edition_completion_problem={outcome.failure or 'unknown'}")
    for problem in outcome.sources:
        lines.append(
            f"russian_edition_source_problem={problem.source}|blocked={problem.blocked}"
            f"|failed={problem.failed}|not_asked={problem.not_asked}"
        )
    return lines


def doctor_line(outcome: StageOutcome) -> str:
    """One Russian line for the doctor on a stage with a problem; empty when there is none."""
    if not outcome.has_problem:
        return ""
    if outcome.skipped:
        why = (
            ": до предела времени ночного запуска оставалось мало" if outcome.failure == SKIPPED_RUN_TIME_LIMIT else ""
        )
        return f"Поиск русских изданий в последнем поиске пропущен{why}."
    if outcome.status != "ok":
        cause = f" ({outcome.failure})" if outcome.failure else ""
        return f"Поиск русских изданий в последнем поиске не удался{cause}."
    named = []
    for problem in outcome.sources:
        name = SOURCE_NAMES_RU.get(problem.source, problem.source)
        parts = []
        if problem.blocked:
            parts.append(f"отказов {problem.blocked}")
        if problem.failed:
            parts.append(f"ошибок {problem.failed}")
        if problem.not_asked:
            parts.append(f"не спрошено {problem.not_asked}")
        named.append(f"{name} — {', '.join(parts)}")
    return "Поиск русских изданий: источники отвечали с проблемами: " + "; ".join(named) + "."


__all__ = [
    "ATTEMPT_BOUNDARY_STAGES",
    "ATTEMPT_END_STAGES",
    "ATTEMPT_START_STAGE",
    "SKIPPED_RUN_TIME_LIMIT",
    "SOURCE_FAILURE_LIMIT",
    "STAGE_NAME",
    "SourceProblem",
    "StageOutcome",
    "doctor_line",
    "health_lines",
    "last_outcome",
    "outcome_of",
]
