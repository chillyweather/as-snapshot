"""Weekly run commands: run-mvp, render-review, run-health, prepare-codex-extraction, finalize-codex-run, run-weekly."""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from autoscout.cli.common import CliCommand
from autoscout.cli.common import SubParsers
from autoscout.cli.common import _review_config
from autoscout.cli.common import _validate_iso_date
from autoscout.codex_workflow import finalize_codex_run
from autoscout.codex_workflow import prepare_codex_extraction
from autoscout.config import load_validated_config
from autoscout.editorial_taste_profile import TasteProfileError
from autoscout.operator_notice import Transport
from autoscout.pipeline import run_pipeline
from autoscout.review_rendering import render_review
from autoscout.run_health import run_health
from autoscout.russian_workflow import intervention_message
from autoscout.russian_workflow import workflow_started_message
from autoscout.weekly_workflow import WorkflowOutcome
from autoscout.weekly_workflow import run_weekly_workflow


def _register_run_mvp(subparsers: SubParsers) -> None:
    run_parser = subparsers.add_parser("run-mvp", help="Run the local MVP Pipeline skeleton.")
    run_parser.add_argument("--config", required=True, type=Path)
    run_parser.add_argument("--output-dir", required=True, type=Path)
    run_parser.add_argument("--state-dir", type=Path)


def _handle_run_mvp(args: argparse.Namespace) -> int:
    try:
        run_mvp(args.config, args.output_dir, args.state_dir)
    except TasteProfileError as exc:
        print(str(exc))
        return 1
    return 0


def _register_render_review(subparsers: SubParsers) -> None:
    render_parser = subparsers.add_parser(
        "render-review", help="Render a feedback-ready local weekly review from run artifacts."
    )
    render_parser.add_argument("--output-dir", required=True, type=Path)
    render_parser.add_argument("--state-dir", type=Path)
    render_parser.add_argument(
        "--config",
        type=Path,
        help=(
            "Weekly configuration whose retired sources withdraw their cards from the review "
            "(default: configs/current-weekly-pilot-live.json of this checkout when it exists, "
            "otherwise no source is withdrawn)."
        ),
    )


def _handle_render_review(args: argparse.Namespace) -> int:
    return render_review(args.output_dir, args.state_dir, config_path=_review_config(args.config))


def _register_run_health(subparsers: SubParsers) -> None:
    run_health_parser = subparsers.add_parser("run-health", help="Summarize weekly run readiness and source health.")
    run_health_parser.add_argument("--output-dir", required=True, type=Path)
    run_health_parser.add_argument(
        "--state-dir",
        type=Path,
        help="Also check that the State Store's database and its JSON export in this directory agree (read-only).",
    )
    run_health_parser.add_argument(
        "--config", type=Path, help="The weekly config whose state_backend selects the State Store backend."
    )


def _handle_run_health(args: argparse.Namespace) -> int:
    return run_health(args.output_dir, state_dir=args.state_dir, config_path=args.config)


def _register_prepare_codex_extraction(subparsers: SubParsers) -> None:
    prepare_codex_parser = subparsers.add_parser(
        "prepare-codex-extraction",
        help="Fetch configured source pages and produce a Codex extraction worklist.",
    )
    prepare_codex_parser.add_argument("--config", required=True, type=Path)
    prepare_codex_parser.add_argument("--output-dir", required=True, type=Path)
    prepare_codex_parser.add_argument(
        "--live",
        action="store_true",
        help="Fetch source pages live even when fixture files are configured.",
    )


def _handle_prepare_codex_extraction(args: argparse.Namespace) -> int:
    load_validated_config(args.config)  # exits non-zero on validation failure
    return prepare_codex_extraction(args.config, args.output_dir, live=args.live)


def _register_finalize_codex_run(subparsers: SubParsers) -> None:
    finalize_codex_parser = subparsers.add_parser(
        "finalize-codex-run",
        help="Generate a replay config from a completed Codex extraction run.",
    )
    finalize_codex_parser.add_argument("--output-dir", required=True, type=Path)
    finalize_codex_parser.add_argument(
        "--live-config",
        type=Path,
        help="Original live-capture config to copy pilot settings from.",
    )


def _handle_finalize_codex_run(args: argparse.Namespace) -> int:
    return finalize_codex_run(args.output_dir, args.live_config)


def _register_run_weekly(subparsers: SubParsers) -> None:
    weekly_parser = subparsers.add_parser(
        "run-weekly",
        help="Run the full weekly workflow with Russian editor messaging.",
    )
    weekly_parser.add_argument("--config", required=True, type=Path)
    weekly_parser.add_argument("--output-dir", required=True, type=Path)
    weekly_parser.add_argument("--state-dir", type=Path)
    weekly_parser.add_argument(
        "--run-date",
        default=None,
        type=_validate_iso_date,
        help="Effective ISO run date (YYYY-MM-DD). The blessed launcher captures the clock date.",
    )
    weekly_parser.add_argument(
        "--russian-intent",
        default="Запусти еженедельный поиск книг",
        help="Russian intent phrase that triggered the workflow.",
    )
    weekly_parser.add_argument(
        "--scheduled",
        action="store_true",
        help=(
            "A start by the server's timer: skip when a search is in progress, do not open the review, "
            "and send one morning report whatever the outcome."
        ),
    )


def _handle_run_weekly(args: argparse.Namespace) -> int:
    import os

    from autoscout.second_attempt import second_attempt_requested

    # The night's later timer marks its start in the environment, not with an option, so older code
    # simply runs an ordinary scheduled start (#1160). A manual run ignores the mark.
    return _run_weekly_command(
        args.config,
        args.output_dir,
        args.state_dir,
        args.run_date,
        scheduled=args.scheduled,
        second_attempt=args.scheduled and second_attempt_requested(os.environ),
    )


def run_mvp(config_path: Path, output_dir: Path, state_dir: Path | None = None) -> None:
    """Thin CLI delegate: validates config, then passes through to
    ``autoscout.pipeline.run_pipeline``."""
    load_validated_config(config_path)  # exits non-zero on validation failure
    run_pipeline(config_path, output_dir, state_dir)


def _find_repo_root(start: Path) -> Path:
    """Repo root containing ``start``, falling back to ``start`` itself.

    Thin wrapper over the shared walker; the only difference from it is this
    caller's non-optional fallback.
    """
    from autoscout.git_envelope import resolve_repo_root

    return resolve_repo_root(start) or start.resolve()


class _StartLine:
    """Prints the run's start line once, before any other line.

    A manual run prints it before the workflow starts. A scheduled run holds
    it back until a first progress line or the result, so a skipped start
    prints its one line and nothing else (#1057).
    """

    def __init__(self, output: Any) -> None:
        self._output = output
        self._pending = True
        self._lock = threading.Lock()

    def ensure(self) -> None:
        with self._lock:
            if self._pending:
                self._pending = False
                print(workflow_started_message(), file=self._output, flush=True)

    def printer(self, render: Callable[..., str]) -> Callable[..., None]:
        def emit(*values: Any) -> None:
            self.ensure()
            print(render(*values), file=self._output, flush=True)

        return emit


def _report_date(run_date: str | None) -> str:
    from datetime import date as _date

    return run_date or _date.today().isoformat()


def _report_scheduled_result(
    result: Any, config_path: Path, run_date: str | None, transport: Transport | None, second_attempt: str = ""
) -> None:
    """A scheduled run names a failed stage and reason on stdout, then sends its one morning report."""
    from autoscout.kirkus_precollection import last_attempt
    from autoscout.operator_notice import deliver
    from autoscout.operator_notice import run_failure_line
    from autoscout.operator_notice import run_report

    if result.outcome == WorkflowOutcome.INTERVENTION_REQUIRED:
        print(run_failure_line(result))
    report = run_report(
        result,
        run_date=_report_date(run_date),
        kirkus_attempt=last_attempt(config_path),
        second_attempt=second_attempt,
    )
    deliver(report, transport=transport)


def _line(first_attempt: Any) -> str:
    """The second attempt's report line; empty for a first attempt."""
    return first_attempt.line() if first_attempt is not None else ""


def _restart_on_updated_code() -> None:
    """Replace this process with the same command, marked so it restarts at most once.

    A start from the last passing code (#1169) restarts on the checked-out code, which has now passed.
    """
    import os

    from autoscout.code_gate_fallback import leave_start_snapshot
    from autoscout.weekly_workflow import RESTARTED_ENV

    sys.stdout.flush()
    sys.stderr.flush()
    leave_start_snapshot(os.environ)
    os.environ[RESTARTED_ENV] = "1"
    os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])


def _started_again(
    result: Any,
    first_attempt: Any,
    repo_root: Path,
    restart: Callable[[], object] | None,
    run_last_passed: Callable[[dict[str, Any], Path], object] | None,
) -> bool:
    """Start the same command again when the workflow asked for it; ``False`` for any other outcome.

    Either way nothing was written and the lock is released, and the started-again command sends the
    one morning report itself. A second attempt's line about the first one is handed over (#1160).
    """
    import os

    from autoscout.weekly_workflow import WorkflowOutcome

    if result.outcome == WorkflowOutcome.RESTART_ON_UPDATED_CODE:
        # The update pulled new code (#1117); a .venv update is named in the report (#1130).
        from autoscout.weekly_workflow import ENVIRONMENT_UPDATED_ENV
        from autoscout.weekly_workflow import RESTART_AFTER_ENVIRONMENT_UPDATE

        if (getattr(result, "run_summary", None) or {}).get(RESTART_AFTER_ENVIRONMENT_UPDATE):
            os.environ[ENVIRONMENT_UPDATED_ENV] = "1"
        if first_attempt is not None:
            first_attempt.hand_over(os.environ)
        (restart or _restart_on_updated_code)()
        return True
    if result.outcome == WorkflowOutcome.RUN_ON_LAST_PASSED_CODE:
        # New code failed the gate: start again on a snapshot of the last code that passed here (#1159).
        from autoscout.code_gate_fallback import start_again
        from autoscout.weekly_workflow import LAST_PASSED_CODE_PLAN

        plan = dict((getattr(result, "run_summary", None) or {}).get(LAST_PASSED_CODE_PLAN) or {})
        if first_attempt is not None:
            first_attempt.hand_over(os.environ)
        (run_last_passed or start_again)(plan, repo_root)
        return True
    return False


def _run_weekly_command(
    config_path: Path,
    output_dir: Path,
    state_dir: Path | None = None,
    run_date: str | None = None,
    *,
    scheduled: bool = False,
    second_attempt: bool = False,
    report_transport: Transport | None = None,
    restart: Callable[[], object] | None = None,
    run_last_passed: Callable[[dict[str, Any], Path], object] | None = None,
) -> int:
    """Run weekly while keeping unexpected failures behind the editor boundary.

    *scheduled* is a start by the server's timer (#1057): whatever its
    outcome, it sends one morning report. *second_attempt* is the night's
    later scheduled start (#1160): it does nothing, and sends nothing, when
    the date already has a completed run or a run is in progress; a report it
    sends says that it is the second attempt. *report_transport* replaces the
    email transport of that report, for tests. *restart* replaces the
    re-execution of a scheduled run whose update pulled new code, for tests;
    *run_last_passed* the re-execution on the last passing code after a failed
    gate (#1159).
    """
    import os

    from autoscout.second_attempt import FirstAttempt

    # How the date's earlier run ended, named in the second attempt's report; until the workflow reads it
    # under the lock, the report still says that it is the second attempt.
    first_attempt = FirstAttempt.from_environ(os.environ) if second_attempt else None
    # What the workflow prints to stderr while it is silenced, kept for the operator if the run dies (#1158).
    workflow_stderr = io.StringIO()
    try:
        from datetime import date as _date

        from autoscout.weekly_workflow import bind_code_gate
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
        from autoscout.weekly_workflow import weekly_adapters

        repo_root = _find_repo_root(config_path)
        from autoscout.russian_workflow import extraction_progress_message
        from autoscout.russian_workflow import stage_message

        output = sys.stdout
        start_line = _StartLine(output)
        adapters = weekly_adapters(
            repo_root,
            progress=start_line.printer(stage_message),
            extraction_progress=start_line.printer(extraction_progress_message),
        )
        # Issue #452: bind the new heartbeat printer to the real stdout before
        # the runner installs its redirect, exactly like the other progress
        # seams. Binding happens after the factory call, so existing custom
        # factories with the pre-#452 signature keep working unchanged.
        bind_snapshot_progress_printer(adapters, start_line.printer(str))
        bind_code_gate(adapters, repo_root, start_line.printer(str))
        bind_editorial_fit_shadow(adapters)
        bind_isbn_completion(adapters)
        bind_original_edition_completion(adapters)
        bind_work_age_completion(adapters)
        bind_russian_edition_completion(adapters)
        bind_kirkus_precollection(adapters)
        bind_pm_precollection(adapters)
        bind_pm_logline(adapters)
        bind_pw_precollection(adapters)
        if run_date is not None:
            frozen = _date.fromisoformat(run_date)

            class _FrozenClock:
                def today(self) -> _date:
                    return frozen

            adapters.clock = _FrozenClock()
        clock = getattr(adapters, "clock", None)
        operation_date = clock.today() if clock is not None else _date.today()

        from autoscout.russian_workflow import render_workflow_result

        if not scheduled:
            start_line.ensure()
        scheduled_options: dict[str, Any] = {"scheduled": True} if scheduled else {}
        if first_attempt is not None:
            scheduled_options.update(second_attempt=True, on_prior_run=first_attempt.seen)
        from autoscout.unexpected_error import tracebacks_to

        # Tracebacks of the passes go to the stderr this command started with, not to the silenced one.
        with (
            tracebacks_to(sys.stderr),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(workflow_stderr),
        ):
            result = run_weekly_workflow(config_path, output_dir, state_dir, adapters=adapters, **scheduled_options)

        if _started_again(result, first_attempt, repo_root, restart, run_last_passed):
            return 0
        if result.outcome == WorkflowOutcome.SKIPPED_ALREADY_COMPLETED:
            # The night's second attempt has nothing to do: the first attempt's report stands (#1160).
            from autoscout.second_attempt import already_completed_message

            print(already_completed_message(operation_date.isoformat()))
            return 0
        if result.outcome == WorkflowOutcome.SKIPPED_RUN_IN_PROGRESS:
            if second_attempt:
                # The run in progress sends the night's report itself (#1160).
                from autoscout.second_attempt import skipped_live_run_message

                print(skipped_live_run_message())
                return 0
            # The skip line and the report: the run in progress owns every file.
            print(render_workflow_result(result, include_progress=False))
            if scheduled:
                _report_scheduled_result(result, config_path, run_date, report_transport)
            return 0
        start_line.ensure()
        print(render_workflow_result(result, include_progress=False))
        if scheduled:
            _report_scheduled_result(result, config_path, run_date, report_transport, _line(first_attempt))
        return 1 if result.outcome == WorkflowOutcome.INTERVENTION_REQUIRED else 0
    except SystemExit as exc:
        # Config validation exits with SystemExit(1), which ``except
        # Exception`` does not catch. A manual run lets it through as before;
        # a scheduled one reports it, or the night would end in silence.
        from autoscout.operator_notice import exit_code_of

        code = exit_code_of(exc)
        if code != 0:
            _keep_failure(exc, config_path, output_dir, run_date, workflow_stderr, exit_code=code)
        if not scheduled or code == 0:
            raise
        print(intervention_message("unknown"))
        _report_scheduled_exception("config_invalid", config_path, run_date, report_transport, _line(first_attempt))
        return code
    except Exception as exc:
        _keep_failure(exc, config_path, output_dir, run_date, workflow_stderr)
        print(intervention_message("unknown"))
        if scheduled:
            _report_scheduled_exception(
                "unexpected_error", config_path, run_date, report_transport, _line(first_attempt)
            )
        return 1


def _keep_failure(
    exc: BaseException,
    config_path: Path,
    output_dir: Path,
    run_date: str | None,
    workflow_stderr: io.StringIO,
    **extra: Any,
) -> None:
    """Leave what the operator needs after a run died on *exc* (#1158), and nothing for the editor.

    The traceback and what the workflow wrote to the silenced stderr go to stderr (the journal and
    the per-run log); the exception's kind and location go to the run log for the doctor.
    """
    from datetime import date as _date

    from autoscout.unexpected_error import emit_traceback
    from autoscout.unexpected_error import write_stderr
    from autoscout.weekly_workflow import record_unexpected_error

    try:
        silenced = workflow_stderr.getvalue()
        if silenced:
            write_stderr(silenced if silenced.endswith("\n") else silenced + "\n")
        emit_traceback(exc, "weekly run")
        # The workflow records what escapes it itself, while it holds the run lock; this covers the rest.
        # A failure outside the workflow holds no lock, so it writes the log only when no other run does.
        if not getattr(exc, "autoscout_failure_recorded", False) and not _another_run_holds_the_lock(config_path):
            record_unexpected_error(
                output_dir, config_path, _date.fromisoformat(_report_date(run_date)), exc, "weekly-run", **extra
            )
    except BaseException:  # keeping the diagnosis must never change how the run ends, nor hide its exit
        pass


def _another_run_holds_the_lock(config_path: Path) -> bool:
    """Whether a live process holds the run lock: the log then belongs to that run (read-only check)."""
    from autoscout.git_envelope import GitRepositoryAdapter

    try:
        return bool(GitRepositoryAdapter(_find_repo_root(config_path)).guard_status().get("guard_pid_alive"))
    except Exception:
        return True


def _report_scheduled_exception(
    reason: str, config_path: Path, run_date: str | None, transport: Transport | None, second_attempt: str = ""
) -> None:
    """A scheduled run stopped outside its stages prints the failure line and sends its one morning report."""
    from autoscout.kirkus_precollection import last_attempt
    from autoscout.operator_notice import UNKNOWN_STAGE
    from autoscout.operator_notice import deliver
    from autoscout.operator_notice import failure_line
    from autoscout.operator_notice import stopped_run_report

    print(failure_line(UNKNOWN_STAGE, reason))
    report = stopped_run_report(
        reason, run_date=_report_date(run_date), kirkus_attempt=last_attempt(config_path), second_attempt=second_attempt
    )
    deliver(report, transport=transport)


def _register_code_gate(subparsers: SubParsers) -> None:
    parser = subparsers.add_parser(
        "code-gate",
        help=(
            "Say whether this checkout's code passed ./scripts/check on this machine, "
            "the gate a scheduled run applies to new code (#1124)."
        ),
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Run ./scripts/check now and record a pass, as a scheduled run does for new code.",
    )


def _handle_code_gate(args: argparse.Namespace) -> int:
    from autoscout.code_gate import code_gate_command
    from autoscout.git_envelope import resolve_repo_root

    return code_gate_command(resolve_repo_root(Path.cwd()) or Path.cwd(), run=args.run)


COMMANDS: dict[str, CliCommand] = {
    "run-mvp": CliCommand(_register_run_mvp, _handle_run_mvp),
    "render-review": CliCommand(_register_render_review, _handle_render_review),
    "run-health": CliCommand(_register_run_health, _handle_run_health),
    "prepare-codex-extraction": CliCommand(_register_prepare_codex_extraction, _handle_prepare_codex_extraction),
    "finalize-codex-run": CliCommand(_register_finalize_codex_run, _handle_finalize_codex_run),
    "run-weekly": CliCommand(_register_run_weekly, _handle_run_weekly),
    "code-gate": CliCommand(_register_code_gate, _handle_code_gate),
}
