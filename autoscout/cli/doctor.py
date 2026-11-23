"""The doctor command: read-only diagnosis, allowlisted repairs and confirmed recovery."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from autoscout.cli.common import CliCommand
from autoscout.cli.common import SubParsers


def _register_doctor(subparsers: SubParsers) -> None:
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Deterministic read-only diagnostics for the current weekly state.",
    )
    doctor_parser.add_argument("--config", required=True, type=Path)
    doctor_parser.add_argument("--output-dir", required=True, type=Path)
    doctor_parser.add_argument("--state-dir", type=Path)
    doctor_parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output only the machine-readable JSON report (no Russian text).",
    )
    doctor_parser.add_argument(
        "--repair",
        action="append",
        default=None,
        choices=["remove-stale-lock", "rerun-health", "rerender-review", "open-review"],
        dest="repairs",
        help="Allowlisted local repair action (repeatable).",
    )
    doctor_parser.add_argument(
        "--recover",
        action="store_true",
        default=False,
        help="Compute and optionally execute a recovery plan from the diagnosis.",
    )
    doctor_parser.add_argument(
        "--confirmed-by-editor",
        action="store_true",
        default=False,
        help="Apply the planned recovery after editor confirmation (requires --recover).",
    )


def _handle_doctor(args: argparse.Namespace) -> int:
    return _doctor_command(
        args.config,
        args.output_dir,
        args.state_dir,
        args.json,
        getattr(args, "repairs", None),
        recover=getattr(args, "recover", False),
        confirmed_by_editor=getattr(args, "confirmed_by_editor", False),
    )


def _doctor_command(
    config_path: Path,
    output_dir: Path,
    state_dir: Path | None,
    json_only: bool = False,
    repairs: list[str] | None = None,
    recover: bool = False,
    confirmed_by_editor: bool = False,
) -> int:
    """Run the doctor diagnostics (read-only), optionally allowlisted repairs, or recovery."""
    from autoscout.doctor import diagnose
    from autoscout.doctor import exit_code_for
    from autoscout.doctor import render_json_report
    from autoscout.doctor import render_russian

    # ── Error: --confirmed-by-editor without --recover ────────────────────
    if confirmed_by_editor and not recover:
        print("error=confirmed-by-editor-requires-recover")
        return 2

    # ── Error: --recover combined with --repair ───────────────────────────
    if recover and repairs:
        print("error=recover-and-repair-are-distinct-phases")
        return 2

    # ── Repairs path (unchanged #318 behavior) ────────────────────────────
    if repairs:
        from autoscout.doctor_repairs import _render_json_report
        from autoscout.doctor_repairs import _render_russian_report
        from autoscout.doctor_repairs import run_repairs

        repair_report, exit_code = run_repairs(config_path, output_dir, state_dir, repairs)

        if json_only:
            print(_render_json_report(repair_report))
        else:
            russian = _render_russian_report(repair_report)
            if russian:
                print(russian)
            print("")
            print(_render_json_report(repair_report))

        return exit_code

    # ── Recovery path (#319) ──────────────────────────────────────────────
    if recover:
        from autoscout.doctor_recovery import RecoverySeams
        from autoscout.doctor_recovery import render_recovery_json
        from autoscout.doctor_recovery import render_recovery_russian
        from autoscout.doctor_recovery import run_recovery
        from autoscout.russian_workflow import recovery_started_message
        from autoscout.russian_workflow import stage_message

        seams = RecoverySeams()
        if confirmed_by_editor and not json_only:
            # Bind the real stdout *before* the workflow runner installs its
            # redirect, exactly as _run_weekly_command does. Otherwise every
            # progress line lands in the runner's throwaway buffer and the
            # editor sees the started message then silence for the whole
            # multi-minute resume.
            progress_out = sys.stdout

            def _announce(action: str) -> None:
                message = recovery_started_message(action)
                if message:
                    print(message, file=progress_out, flush=True)

            seams.announce = _announce
            seams.progress = lambda stage: print(stage_message(stage), file=progress_out, flush=True)
            # Issue #452: bound only when not json_only, so JSON-only recovery
            # stays pure JSON with no progress heartbeat line.
            seams.snapshot_progress = lambda line: print(line, file=progress_out, flush=True)

            from autoscout.russian_workflow import extraction_progress_message

            seams.extraction_progress = lambda *counts: print(
                extraction_progress_message(*counts), file=progress_out, flush=True
            )

        recovery_report, exit_code = run_recovery(
            config_path,
            output_dir,
            state_dir,
            confirmed_by_editor=confirmed_by_editor,
            seams=seams,
        )

        if json_only:
            print(render_recovery_json(recovery_report))
        else:
            russian = render_recovery_russian(recovery_report)
            if russian:
                print(russian)
            print("")
            print(render_recovery_json(recovery_report))

        return exit_code

    # ── Read-only diagnostics (unchanged #317 behavior) ───────────────────
    report = diagnose(config_path, output_dir, state_dir)
    exit_code = exit_code_for(report)

    if json_only:
        print(render_json_report(report))
    else:
        russian = render_russian(report)
        if russian:
            print(russian)
        print("")
        print(render_json_report(report))

    return exit_code


COMMANDS: dict[str, CliCommand] = {
    "doctor": CliCommand(_register_doctor, _handle_doctor),
}
