"""The second scheduled attempt of a night (#1160).

The timer of 02:30 fires once. A later timer starts the same scheduled run
with ``AUTOSCOUT_SECOND_ATTEMPT=1`` in its environment; it runs only when the
date's operation has a completed run that is committed. The decision reads
the run's own evidence: the entries of ``workflow-log.json`` stamped with the
operation id of the date, the same evidence a same-date retry resumes from,
and HEAD's copy of that log, because a ``completed/*`` terminal only in the
working tree means the run died while publishing it.

The second attempt is an environment variable, not a command-line option, so
code that does not know it (a checkout not yet updated, or the last passing
code of #1159) runs an ordinary scheduled start instead of refusing an
unknown option; a second run on one date is supported.

Every outcome other than a committed completed run is attempted again: a run
that never started, one that was interrupted (no terminal entry, or a
completed terminal never committed), and one that
stopped with intervention required, whatever its stage. A second start is
the same operation, so it resumes what the first one left as any same-date
retry does, and it refuses everything the next night's run would refuse, so
it is never less safe than waiting a day. A stop that needs a person (a
repository blocker, a failed code gate with no fallback) repeats within
seconds, or within one check of the code, and its report says that it is the
second attempt and names how the first one ended.

A live run is not this module's question: the scheduled run's lock claim
skips a start that meets one, and a second attempt that skips sends nothing.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from collections.abc import MutableMapping
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path

from autoscout.mvp_run_evidence import MvpRunEvidence
from autoscout.mvp_run_evidence import evidence_from_log
from autoscout.operator_notice import failure_line
from autoscout.operator_notice import safe_reason
from autoscout.toolchain_preflight import STAGE_ZERO_NAME

# How the earlier run of the date ended, as its run log shows it.
PRIOR_NONE = "none"
PRIOR_INTERRUPTED = "interrupted"
PRIOR_STOPPED = "stopped"
PRIOR_COMPLETED = "completed"
# Completed in the working tree, but the terminal never reached a commit.
PRIOR_UNPUBLISHED = "unpublished"
_PRIOR_STATES = frozenset({PRIOR_NONE, PRIOR_INTERRUPTED, PRIOR_STOPPED, PRIOR_COMPLETED, PRIOR_UNPUBLISHED})

# Set to "1" by the wrapper for the night's later timer; read by the scheduled command.
SECOND_ATTEMPT_ENV = "AUTOSCOUT_SECOND_ATTEMPT"
# Carries the earlier run across a restart on updated code: the first start's
# preflight may set the failed log aside, so the restarted start cannot read it.
PRIOR_RUN_ENV = "AUTOSCOUT_SECOND_ATTEMPT_PRIOR_RUN"
# The value handed over when the earlier run could not be read.
UNKNOWN_PRIOR_RUN = "unknown"


def second_attempt_requested(environ: Mapping[str, str]) -> bool:
    """Whether the environment marks this scheduled start as the night's second attempt."""
    return environ.get(SECOND_ATTEMPT_ENV) == "1"


@dataclass(frozen=True)
class PriorRun:
    """The earlier run of the operation date, read from the run log."""

    state: str
    stage: str = ""
    reason: str = ""

    @property
    def completed(self) -> bool:
        return self.state == PRIOR_COMPLETED


def prior_run(evidence: MvpRunEvidence) -> PriorRun:
    """How the earlier run of the evidence's operation ended.

    Only entries stamped with the operation id count; a legacy unstamped log
    or another date's log is no run of this date.
    """
    if not evidence.stamped_current:
        return PriorRun(PRIOR_NONE)
    if evidence.has_completed_outcome:
        return PriorRun(PRIOR_COMPLETED)
    if not evidence.has_terminal_intervention:
        return PriorRun(PRIOR_INTERRUPTED)
    # The stop is the last failed entry, which the terminal follows; a failed
    # repository-finalize counts, though the evidence's failed_stage skips it.
    stop = next(
        (entry for entry in reversed(evidence.log) if entry.get("status") == "failed" and entry.get("stage")),
        None,
    )
    if stop is None:
        return PriorRun(PRIOR_STOPPED, stage=evidence.failed_stage)
    stage = str(stop["stage"])
    if stage == STAGE_ZERO_NAME:
        # The toolchain stop's reason is the probe's prose; its code is in the stage's own entry.
        return PriorRun(PRIOR_STOPPED, stage=stage, reason=_toolchain_reason_code(evidence))
    return PriorRun(PRIOR_STOPPED, stage=stage, reason=str(stop.get("reason", "")))


def confirm_published(prior: PriorRun, committed_log: str | None, operation_id: str) -> PriorRun:
    """*prior*, unless it is a completion whose terminal is not in HEAD's copy of the run log.

    *committed_log* is that copy's text, ``None`` when it cannot be read; a
    completion that cannot be shown committed is an interrupted publication.
    """
    if not prior.completed:
        return prior
    try:
        entries = json.loads(committed_log) if committed_log is not None else None
    except ValueError:
        entries = None
    if isinstance(entries, list) and evidence_from_log(entries, operation_id).has_completed_outcome:
        return prior
    return PriorRun(PRIOR_UNPUBLISHED)


def _toolchain_reason_code(evidence: MvpRunEvidence) -> str:
    for entry in reversed(evidence.log):
        detail = entry.get("detail") if entry.get("stage") == STAGE_ZERO_NAME else None
        codex = detail.get("codex") if isinstance(detail, dict) else None
        if isinstance(codex, dict) and codex.get("reason_code"):
            return safe_reason(codex["reason_code"])
    return ""


def log_unreadable(output_dir: Path) -> bool:
    """Whether the run log exists but is not a readable JSON list, so no earlier run can be told."""
    path = output_dir / "workflow-log.json"
    if not path.exists():
        return False
    try:
        return not isinstance(json.loads(path.read_text(encoding="utf-8")), list)
    except (OSError, ValueError):
        return True


def encode(prior: PriorRun) -> str:
    """*prior* as the value of :data:`PRIOR_RUN_ENV`."""
    return json.dumps(asdict(prior), sort_keys=True)


def decode(value: str) -> PriorRun | None:
    """The earlier run a restarted start was handed; ``None`` for anything malformed."""
    try:
        data = json.loads(value)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("state") not in _PRIOR_STATES:
        return None
    stage, reason = data.get("stage", ""), data.get("reason", "")
    if not isinstance(stage, str) or not isinstance(reason, str):
        return None
    return PriorRun(data["state"], stage=stage, reason=reason)


def already_completed_message(run_date: str) -> str:
    """The one journal line of a second attempt that has nothing to do."""
    return f"Повторная попытка не нужна: поиск за {run_date} уже завершён."


def skipped_live_run_message() -> str:
    """The one journal line of a second attempt that met a run in progress."""
    return "Повторная попытка пропущена: поиск уже идёт."


# The report line when the earlier run of the date could not be read.
GENERIC_REPORT_LINE = "Это повторная попытка этой ночи."


def report_line(prior: PriorRun | None) -> str:
    """The morning report's line saying that this is the night's second attempt, and why it ran.

    ``None`` is an earlier run that could not be read.
    """
    if prior is None:
        return GENERIC_REPORT_LINE
    if prior.state == PRIOR_STOPPED:
        return f"Это повторная попытка: первый запуск этой даты не завершён. {failure_line(prior.stage, prior.reason)}"
    how = {
        PRIOR_INTERRUPTED: "был прерван и не завершился",
        PRIOR_UNPUBLISHED: "был прерван, когда сохранял итог",
    }.get(prior.state, "не состоялся или был прерван до первой записи в журнал")
    return f"Это повторная попытка: первый запуск этой даты {how}."


class FirstAttempt:
    """The earlier run of the date as the second attempt's report names it.

    The workflow tells it what it read under the run lock (:meth:`seen`). A
    start restarted on updated code was handed it instead, because the first
    start's preflight may have set the failed log aside; what the restarted
    start reads then is not the earlier run.
    """

    def __init__(self, prior: PriorRun | None, *, handed: bool) -> None:
        self.prior = prior
        self._handed = handed

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> FirstAttempt:
        """A handed value that is unknown or malformed stays unknown.

        The restarted start's own read is not the earlier run, so it never replaces a handed value.
        """
        value = environ.get(PRIOR_RUN_ENV)
        return cls(decode(value) if value is not None else None, handed=value is not None)

    def seen(self, prior: PriorRun | None) -> None:
        if not self._handed:
            self.prior = prior

    def hand_over(self, environ: MutableMapping[str, str]) -> None:
        environ[PRIOR_RUN_ENV] = encode(self.prior) if self.prior is not None else UNKNOWN_PRIOR_RUN

    def line(self) -> str:
        return report_line(self.prior)
