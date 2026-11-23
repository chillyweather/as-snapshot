"""State directory commands: shard-candidate-history (#1073) and state-database (#1074, #1093)."""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from autoscout.cli.common import CliCommand
from autoscout.cli.common import SubParsers


def _register_shard_candidate_history(subparsers: SubParsers) -> None:
    parser = subparsers.add_parser(
        "shard-candidate-history",
        help="Convert a state directory's Candidate History from one file to one file per book.",
    )
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument(
        "--dry-run", action="store_true", help="Validate every record and print the counts; write nothing."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace shards the state directory already has with the records of the single file.",
    )


def _handle_shard_candidate_history(args: argparse.Namespace) -> int:
    from autoscout.state_store.conversion import ConversionError
    from autoscout.state_store.conversion import convert_to_sharded

    started = time.monotonic()
    try:
        report = convert_to_sharded(args.state_dir, dry_run=args.dry_run, force=args.force)
    except (ConversionError, OSError) as exc:
        print("candidate_history_conversion=refused")
        print(f"error={exc}")
        return 2
    print(f"candidate_history_conversion={report.status}")
    for key, value in report.to_dict().items():
        if key != "status":
            print(f"{key}={str(value).lower() if isinstance(value, bool) else value}")
    print(f"seconds={time.monotonic() - started:.2f}")
    return 0


def _register_state_database(subparsers: SubParsers) -> None:
    parser = subparsers.add_parser(
        "state-database",
        help=(
            "Build the SQLite state database from the JSON state, export it back to JSON, or move a state "
            "directory to SQLite (cutover) and back (rollback)."
        ),
    )
    parser.add_argument("action", choices=("rebuild", "export", "cutover", "rollback"))
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument(
        "--database",
        type=Path,
        help="The database file; default: state_database in --config, AUTOSCOUT_STATE_DATABASE, else data/state-store/.",
    )
    parser.add_argument("--config", type=Path, help="A weekly config whose state_database names the database file.")
    parser.add_argument("--overwrite", action="store_true", help="rebuild: replace an existing database.")
    parser.add_argument(
        "--allow-non-canonical",
        action="store_true",
        help="rebuild: accept state files not in the form the State Store writes; the first export rewrites them.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=("export, rollback: write the database over a state directory changed outside it since the last export."),
    )


def _print_report(prefix: str, status: str, report: dict[str, Any], started: float) -> None:
    print(f"{prefix}={status}")
    for key, value in report.items():
        if key == "status":
            continue
        if isinstance(value, dict):
            for name, count in value.items():
                print(f"{key}.{name}={count}")
        elif isinstance(value, list):
            print(f"{key}={','.join(str(item) for item in value)}")
        else:
            print(f"{key}={str(value).lower() if isinstance(value, bool) else value}")
    print(f"seconds={time.monotonic() - started:.2f}")


def _handle_state_database(args: argparse.Namespace) -> int:
    from autoscout.state_store import SQLITE_BACKEND
    from autoscout.state_store import BackendSelection
    from autoscout.state_store import StateStoreError
    from autoscout.state_store import cut_over_to_sqlite
    from autoscout.state_store import export_json
    from autoscout.state_store import rebuild_from_json
    from autoscout.state_store import roll_back_to_json
    from autoscout.state_store import select_state_backend_from_file

    prefix = f"state_database_{args.action}"
    started = time.monotonic()
    try:
        configured = select_state_backend_from_file(args.config)
        database = args.database.absolute() if args.database is not None else configured.database
        # The database is the SQLite backend's whichever backend the config makes active.
        selection = BackendSelection(SQLITE_BACKEND, configured.selected_by, database)
        if args.action == "rebuild":
            report = rebuild_from_json(
                args.state_dir,
                overwrite=args.overwrite,
                selection=selection,
                require_canonical=not args.allow_non_canonical,
            )
            _print_report(prefix, "built", report.to_dict(), started)
            return 0
        if args.action == "cutover":
            exported = cut_over_to_sqlite(args.state_dir, selection)
            _print_report(prefix, "cut_over", {"export": exported.status, **exported.to_dict()}, started)
            return 0
        if args.action == "rollback":
            exported = roll_back_to_json(args.state_dir, selection, force=args.force)
            if exported is None:
                _print_report(prefix, "not_cut_over", {}, started)
            else:
                _print_report(prefix, "rolled_back", {"export": exported.status, **exported.to_dict()}, started)
            return 0
        exported = export_json(args.state_dir, selection, force=args.force)
    except (StateStoreError, ValueError, OSError) as exc:
        print(f"{prefix}=refused")
        print(f"error={exc}")
        return 2
    _print_report(prefix, exported.status, exported.to_dict(), started)
    return 0


COMMANDS: dict[str, CliCommand] = {
    "shard-candidate-history": CliCommand(_register_shard_candidate_history, _handle_shard_candidate_history),
    "state-database": CliCommand(_register_state_database, _handle_state_database),
}
