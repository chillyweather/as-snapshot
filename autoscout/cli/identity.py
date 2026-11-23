"""Candidate identity commands: merge-candidate-identity, probable-duplicates, resolve-review-rows, migrate-candidate-history."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from autoscout.cli.common import CliCommand
from autoscout.cli.common import SubParsers
from autoscout.identity_corrections import apply_identity_correction


def _register_merge_candidate_identity(subparsers: SubParsers) -> None:
    identity_parser = subparsers.add_parser(
        "merge-candidate-identity",
        help="Apply a guarded, replayable Candidate identity correction.",
    )
    identity_parser.add_argument("--state-dir", required=True, type=Path)
    identity_parser.add_argument("--correction-file", required=True, type=Path)
    identity_parser.add_argument("--dry-run", action="store_true")


def _handle_merge_candidate_identity(args: argparse.Namespace) -> int:
    return _merge_candidate_identity(args.state_dir, args.correction_file, args.dry_run)


def _register_probable_duplicates(subparsers: SubParsers) -> None:
    duplicates_parser = subparsers.add_parser(
        "probable-duplicates",
        help="Read-only: list probable duplicate Candidate identities; never merges.",
    )
    duplicates_parser.add_argument("--state-dir", required=True, type=Path)
    duplicates_parser.add_argument(
        "--dismiss",
        action="append",
        default=None,
        metavar="IDENTITY_KEY",
        help="Dismiss one reported pair: give both identity keys (preview unless --confirmed-by-editor).",
    )
    duplicates_parser.add_argument("--reason", default="")
    duplicates_parser.add_argument("--confirmed-by-editor", action="store_true")


def _handle_probable_duplicates(args: argparse.Namespace) -> int:
    from autoscout.probable_duplicates import probable_duplicates_command

    return probable_duplicates_command(
        args.state_dir,
        dismiss_keys=args.dismiss,
        reason=args.reason,
        confirmed_by_editor=args.confirmed_by_editor,
    )


def _register_resolve_review_rows(subparsers: SubParsers) -> None:
    resolve_rows_parser = subparsers.add_parser(
        "resolve-review-rows",
        help="Read-only: match returned review rows to durable Candidate identities.",
    )
    resolve_rows_parser.add_argument("--state-dir", required=True, type=Path)
    resolve_rows_parser.add_argument(
        "--rows", required=True, type=Path, help="Returned rows as a .json list, an editor .csv, or a .md review."
    )


def _handle_resolve_review_rows(args: argparse.Namespace) -> int:
    from autoscout.review_row_resolution import resolve_review_rows_command

    return resolve_review_rows_command(args.rows, args.state_dir)


def _register_migrate_candidate_history(subparsers: SubParsers) -> None:
    migration_parser = subparsers.add_parser(
        "migrate-candidate-history",
        help="Plan or apply a deterministic offline Candidate History identity migration.",
    )
    migration_parser.add_argument("--state-dir", required=True, type=Path)
    migration_parser.add_argument("--report", type=Path, help="Write the Markdown report to this path.")
    migration_parser.add_argument("--editor-csv", type=Path, help="Include editor-facing CSV before/after rows.")
    migration_parser.add_argument("--apply", action="store_true", help="Apply the plan to Candidate History.")
    migration_parser.add_argument(
        "--durable-merge-approval",
        type=Path,
        action="append",
        default=None,
        dest="durable_merge_approvals",
        help=(
            "Approve one pairwise durable-ID merge from a JSON file with "
            "canonical_identity_key, survivor_candidate_id, retired_candidate_ids, "
            "and expected_members. "
            "Repeat for multiple merges. Without an approval, a multiple_durable_ids "
            "conflict still blocks apply."
        ),
    )
    migration_parser.add_argument(
        "--emit-merge-approval",
        type=Path,
        default=None,
        dest="emit_merge_approval",
        help=(
            "Write an approval template for the single pending durable-ID merge. "
            "Dry-run only; review the file before passing it back with --durable-merge-approval."
        ),
    )


def _handle_migrate_candidate_history(args: argparse.Namespace) -> int:
    return _migrate_candidate_history(
        args.state_dir,
        args.report,
        args.editor_csv,
        args.apply,
        args.durable_merge_approvals,
        args.emit_merge_approval,
    )


def _migrate_candidate_history(
    state_dir: Path,
    report_path: Path | None,
    editor_csv_path: Path | None,
    apply: bool,
    durable_merge_approval_paths: list[Path] | None = None,
    emit_merge_approval_path: Path | None = None,
) -> int:
    from autoscout.candidate_history_migration import emit_durable_merge_approval
    from autoscout.candidate_history_migration import load_durable_merge_approvals
    from autoscout.candidate_history_migration import migrate_candidate_history
    from autoscout.candidate_history_migration import render_candidate_history_migration_report
    from autoscout.state_store import Collection
    from autoscout.state_store import open_state_store

    if emit_merge_approval_path is not None:
        if apply:
            print("error=candidate_history_migration: cannot emit an approval with --apply")
            return 2
        try:
            store = open_state_store(state_dir)
            history = store.read(Collection.CANDIDATE_HISTORY)
            # The store reads a missing history as an empty one; both are refused.
            if not isinstance(history, list) or not history:
                raise ValueError("candidate-history must be a JSON list")
            approval = emit_durable_merge_approval(history, store.read(Collection.CANDIDATE_IDENTITY_ALIASES))
            emit_merge_approval_path.parent.mkdir(parents=True, exist_ok=True)
            emit_merge_approval_path.write_text(
                json.dumps(approval, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f"error=candidate_history_migration: {exc}")
            return 1
        print(f"merge_approval_emitted={emit_merge_approval_path}")
        print(f"survivor={approval['survivor_candidate_id']}")
        return 0

    try:
        approvals = load_durable_merge_approvals(durable_merge_approval_paths)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"error=candidate_history_migration: {exc}")
        return 2 if apply else 1

    try:
        report, changed = migrate_candidate_history(
            state_dir,
            report_path=report_path,
            editor_csv_path=editor_csv_path,
            apply=apply,
            durable_merge_approvals=approvals or None,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"error=candidate_history_migration: {exc}")
        return 2 if apply else 1

    mode = "applied" if apply else "dry-run"
    print(f"candidate_history_migration={mode}")
    print(f"history_entries_before={report['history_entries_before']}")
    print(f"projected_history_count={report['projected_history_count']}")
    print(f"merge_group_count={report['merge_group_count']}")
    print(f"singleton_changed_count={report.get('singleton_changed_count', 0)}")
    print(f"unrekeyable_count={report['unrekeyable_count']}")
    print(f"apply_blocked={str(report['apply_blocked']).lower()}")
    for merge in report.get("durable_merges", []):
        print(
            f"durable_merge={merge.get('status', '')} "
            f"canonical={merge.get('canonical_identity_key', '')} "
            f"survivor={merge.get('survivor_candidate_id', '')} "
            f"retired={','.join(merge.get('retired_candidate_ids', []))}"
        )
    if apply:
        print(f"candidate_history_changed={str(changed).lower()}")
    if report_path is None:
        print(render_candidate_history_migration_report(report), end="")
    return 0


def _merge_candidate_identity(state_dir: Path, correction_file: Path, dry_run: bool = False) -> int:
    from autoscout.json_io import read_json_or_default

    correction = read_json_or_default(correction_file, {})
    if not isinstance(correction, dict):
        print("error=identity_correction_must_be_an_object")
        return 1
    try:
        report = apply_identity_correction(state_dir, correction, dry_run=dry_run)
    except (ValueError, OSError) as exc:
        # A refused correction and a failed bundle write are both operator
        # findings, not tracebacks. The bundle restores every state file
        # before the error reaches here, so the same correction can be run
        # again once the cause is fixed.
        print(f"error={exc}")
        return 1
    print(f"identity_correction={'dry-run' if dry_run else 'applied'}")
    print(f"canonical_identity_key={report['canonical_identity_key']}")
    print(f"canonical_title={report['canonical_title']}")
    print(f"canonical_authors={'; '.join(report['canonical_authors'])}")
    # Every merged identity key is printed on its own line: the editor
    # confirmation must name them all, never a truncated list.
    for identity_key in report["source_identity_keys"]:
        print(f"source_identity_key={identity_key}")
    print(f"merged_history_records={report['merged_history_records']}")
    print(f"rewritten_references={report['rewritten_references']}")
    if report["survivor_candidate_id"]:
        print(f"survivor_candidate_id={report['survivor_candidate_id']}")
    for candidate_id in report["retired_candidate_ids"]:
        print(f"retired_candidate_id={candidate_id}")
    print(f"preserved_editor_status={report['preserved_editor_status']}")
    # Free text is JSON-quoted so a note or description stays verbatim and on
    # one line, whatever punctuation or line breaks the editor wrote.
    print(f"preserved_editor_note={json.dumps(report['preserved_editor_note'], ensure_ascii=False)}")
    resolution = report["description_resolution"]
    print(f"description_resolution={'explicit' if resolution else 'none'}")
    if resolution:
        print(f"description_source_identity_key={resolution['chosen_source_identity_key']}")
        print(f"description_provenance={resolution['provenance']}")
        for evidence_url in resolution["evidence_basis"]:
            print(f"description_evidence_basis={evidence_url}")
        for identity_key in resolution["discarded_source_identity_keys"]:
            print(f"description_discarded_identity_key={identity_key}")
        # A description attempt the chosen record's attempt replaced under one
        # operation_id moves to the audit record; the preview names each (#1023).
        for attempt in resolution.get("discarded_description_attempts", []):
            print(f"description_discarded_attempt={json.dumps(attempt, ensure_ascii=False, sort_keys=True)}")
    decision = report.get("editor_decision_resolution")
    if decision:
        # Printed only when the artifact resolves a decision, so every other
        # dry run reads exactly as before (issue #917). Each decision the
        # merge does not keep is shown verbatim: it moves to the audit record.
        print("editor_decision_resolution=explicit")
        print(f"editor_decision_source_identity_key={decision['chosen_source_identity_key']}")
        for entry in decision["discarded_decisions"]:
            print(f"editor_decision_discarded={json.dumps(entry, ensure_ascii=False, sort_keys=True)}")
    print(f"content_description={json.dumps(report['content_description'], ensure_ascii=False)}")
    return 0


COMMANDS: dict[str, CliCommand] = {
    "merge-candidate-identity": CliCommand(_register_merge_candidate_identity, _handle_merge_candidate_identity),
    "probable-duplicates": CliCommand(_register_probable_duplicates, _handle_probable_duplicates),
    "resolve-review-rows": CliCommand(_register_resolve_review_rows, _handle_resolve_review_rows),
    "migrate-candidate-history": CliCommand(_register_migrate_candidate_history, _handle_migrate_candidate_history),
}
