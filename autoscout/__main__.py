"""AutoScout CLI entry point.

Builds the top-level parser, asks each command group in ``autoscout.cli`` to
register its subcommands, and dispatches to the chosen command's handler.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from autoscout.cli import collection
from autoscout.cli import descriptions
from autoscout.cli import doctor
from autoscout.cli import feedback
from autoscout.cli import identity
from autoscout.cli import onboarding
from autoscout.cli import profile
from autoscout.cli import qualification
from autoscout.cli import sources
from autoscout.cli import state
from autoscout.cli import weekly
from autoscout.cli.common import WEEKLY_RUN_IN_PROGRESS_EXIT_CODE
from autoscout.cli.common import CliCommand
from autoscout.cli.common import _editor_mutation_blocked
from autoscout.cli.onboarding import _validate_add_source_url_occurrences
from autoscout.state_store import StateStoreError

# The exit code of a State Store refusal, the one ``state-database`` returns.
STATE_STORE_REFUSED_EXIT_CODE = 2

_GROUPS = (
    weekly.COMMANDS,
    feedback.COMMANDS,
    profile.COMMANDS,
    identity.COMMANDS,
    sources.COMMANDS,
    state.COMMANDS,
    onboarding.COMMANDS,
    collection.COMMANDS,
    descriptions.COMMANDS,
    doctor.COMMANDS,
    qualification.COMMANDS,
)

# argparse lists subcommands in the order they are registered, so this order
# is part of the ``--help`` output. A new command is added to its group's
# COMMANDS and here.
_COMMAND_ORDER = (
    "run-mvp",
    "ingest-feedback",
    "source-proposal",
    "record-feedback",
    "record-feedback-batch",
    "record-visibility",
    "export-decision-corpus",
    "editorial-profile",
    "source-lifecycle",
    "render-review",
    "complete-isbns",
    "complete-originals",
    "complete-russian-editions",
    "complete-work-age",
    "portal-first-load",
    "run-health",
    "audit-annual-sources",
    "check-sources",
    "collect-kirkus",
    "collect-pm-deals",
    "collect-pw-issue",
    "probe-source-candidate",
    "discover-source-gaps",
    "monthly-source-discovery",
    "audit-editorial-notes",
    "add-source",
    "capture-source-fixtures",
    "scaffold-source",
    "compare-search-optimization",
    "editorial-fit-shadow",
    "ranking-benchmark",
    "work-age-report",
    "preserve-search-optimization-run",
    "qualify-model",
    "qualify-description-model",
    "prepare-codex-extraction",
    "finalize-codex-run",
    "prepare-description-backfill",
    "apply-description-backfill",
    "prepare-english-title-backfill",
    "apply-english-title-backfill",
    "doctor",
    "merge-candidate-identity",
    "probable-duplicates",
    "resolve-review-rows",
    "source-novelty",
    "source-yield-report",
    "migrate-candidate-history",
    "shard-candidate-history",
    "state-database",
    "generate-descriptions",
    "adjudicate-descriptions",
    "run-weekly",
    "code-gate",
    "refresh-russian-editions",
)


def _editor_state_mutation_requested(args: argparse.Namespace) -> bool:
    """Classify CLI apply paths only; library callers inside a run are exempt."""
    if args.command in {
        "run-mvp",
        "ingest-feedback",
        "record-visibility",
        "generate-descriptions",
        "apply-description-backfill",
    }:
        return True
    if args.command in {"record-feedback", "record-feedback-batch"}:
        return bool(args.confirmed_by_editor)
    if args.command == "editorial-profile":
        return args.profile_command != "list" and bool(args.confirmed_by_editor)
    if args.command == "source-proposal":
        return args.proposal_command != "reconsider" or bool(args.confirmed_by_editor)
    if args.command in {"merge-candidate-identity", "shard-candidate-history"}:
        return not args.dry_run
    if args.command == "state-database":
        return True
    if args.command == "probable-duplicates":
        return bool(args.dismiss) and args.confirmed_by_editor
    if args.command == "apply-english-title-backfill":
        return bool(args.confirm)
    if args.command in {
        "refresh-russian-editions",
        "adjudicate-descriptions",
        "migrate-candidate-history",
        "complete-isbns",
        "complete-originals",
        "complete-russian-editions",
        "complete-work-age",
        "portal-first-load",
    }:
        return bool(args.apply)
    return False


def _commands() -> dict[str, CliCommand]:
    return {name: command for group in _GROUPS for name, command in group.items()}


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not _validate_add_source_url_occurrences(arguments):
        return 1
    parser = argparse.ArgumentParser(prog="autoscout")
    subparsers = parser.add_subparsers(dest="command", required=True)
    commands = _commands()
    for name in _COMMAND_ORDER:
        commands[name].register(subparsers)

    args = parser.parse_args(arguments)
    mutation = _editor_state_mutation_requested(args)
    if mutation and _editor_mutation_blocked(args.state_dir):
        return WEEKLY_RUN_IN_PROGRESS_EXIT_CODE
    try:
        returncode = commands[args.command].handle(args)
    except StateStoreError as exc:
        # A State Store refusal no command handled itself (a marked state
        # directory whose database is missing on this machine, a backend
        # conflict) is one operator line and exit 2, as ``state-database``
        # reports it (#1098). Nothing was written: the store refuses before
        # it opens a write, and the export below is skipped.
        print(f"error={exc}")
        return STATE_STORE_REFUSED_EXIT_CODE
    if mutation and args.command != "state-database":
        returncode = _export_state_after(args, returncode)
    return returncode


# The state mutation commands whose store is chosen by their --config.
_CONFIG_SELECTED_STORE_COMMANDS = frozenset({"run-mvp"})


def _export_state_after(args: argparse.Namespace, returncode: int) -> int:
    """Under the SQLite backend, write the state a command changed back into its state directory as JSON (#1074).

    Git keeps the JSON export, not the database, so a confirmed editor action
    is exported before anything commits it. Nothing happens on the JSON
    backend. A failed export is reported and fails a command that succeeded.
    """
    from autoscout.state_store import export_json
    from autoscout.state_store import select_state_backend_from_file

    state_dir = getattr(args, "state_dir", None)
    # The export uses the backend the command's own store used: the weekly
    # config's for the commands that open their store from it, else the
    # environment's or the default.
    config = getattr(args, "config", None) if args.command in _CONFIG_SELECTED_STORE_COMMANDS else None
    if not isinstance(state_dir, Path):
        return returncode
    try:
        selection = select_state_backend_from_file(config if isinstance(config, Path) else None)
    except StateStoreError:
        return returncode
    try:
        export_json(state_dir, selection)
    except (StateStoreError, ValueError, OSError) as exc:
        print("state_export=failed", file=sys.stderr)
        print(f"error={exc}", file=sys.stderr)
        return returncode or 1
    return returncode


if __name__ == "__main__":
    sys.exit(main())
