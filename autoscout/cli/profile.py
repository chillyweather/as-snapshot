"""Editorial Taste Profile commands: editorial-profile, audit-editorial-notes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from autoscout.cli.common import CliCommand
from autoscout.cli.common import SubParsers
from autoscout.cli.common import _validate_iso_date
from autoscout.editorial_audit import DEFAULT_EDITORIAL_AUDIT_OUTPUT_DIR
from autoscout.editorial_audit import DEFAULT_EDITORIAL_AUDIT_STATE_DIR
from autoscout.editorial_audit import EditorialAuditRequest
from autoscout.editorial_audit import run_editorial_audit
from autoscout.editorial_taste_profile import add_profile_entry
from autoscout.editorial_taste_profile import list_profile_entries
from autoscout.editorial_taste_profile import revoke_profile_entry
from autoscout.editorial_taste_profile import set_profile_entry_terms


def _register_editorial_profile(subparsers: SubParsers) -> None:
    profile_parser = subparsers.add_parser(
        "editorial-profile", help="Add, list, revoke, or supply terms for Editorial Taste Profile entries."
    )
    profile_subparsers = profile_parser.add_subparsers(dest="profile_command", required=True)
    profile_add = profile_subparsers.add_parser("add", help="Add a Hard Rule or Soft Preference.")
    profile_add.add_argument("--kind", required=True, help="hard-rule or soft-preference")
    profile_add.add_argument("--scope", required=True, help="author, topic, publisher, source, or source_family")
    profile_add.add_argument("--subject", required=True, help="Rule/preference subject, e.g. author name")
    profile_add.add_argument("--quote", required=True, help="Editor's verbatim words")
    profile_add.add_argument("--provenance", required=True, help="Where the conversational decision came from")
    profile_add.add_argument("--created-by", default="agent")
    profile_add.add_argument("--direction", default="", help="positive or negative (soft-preference only)")
    profile_add.add_argument("--description", default="", help="Plain-language description (soft-preference only)")
    profile_add.add_argument(
        "--example-books", default="", help="Comma-separated example book titles (soft-preference only)"
    )
    profile_add.add_argument("--state-dir", required=True, type=Path)
    profile_add.add_argument(
        "--match-term",
        action="append",
        default=None,
        dest="matching_terms",
        help=(
            "Explicit matching term for topic entries (repeatable). Values are trimmed and deduplicated. "
            "End a word with * to match its inflected forms, e.g. трансгендер*."
        ),
    )
    profile_add.add_argument(
        "--exception-term",
        action="append",
        default=None,
        dest="exception_terms",
        help=(
            "Topic term that keeps a matching candidate outside the entry (repeatable), "
            "e.g. a cookbook rule that keeps food history visible."
        ),
    )
    profile_add.add_argument(
        "--confirmed-by-editor",
        action="store_true",
        default=False,
        help="Apply the profile entry after editor confirmation. Without this flag, prints preview and returns code 3.",
    )
    profile_add.add_argument(
        "--overlap-resolution",
        default="",
        help="Resolve probable overlap: keep-existing, replace-existing, or add-distinct.",
    )
    profile_add.add_argument(
        "--replace-id",
        action="append",
        default=None,
        dest="replace_ids",
        help="Explicit ID to replace (repeatable; required for replace-existing).",
    )
    profile_add.add_argument(
        "--replacement-reason",
        default="",
        help="Reason for replacement (required for replace-existing).",
    )
    profile_list = profile_subparsers.add_parser("list", help="List the Editorial Taste Profile in plain language.")
    profile_list.add_argument("--state-dir", required=True, type=Path)
    profile_revoke = profile_subparsers.add_parser("revoke", help="Revoke an Editorial Taste Profile entry.")
    profile_revoke.add_argument("--id", required=True)
    profile_revoke.add_argument("--reason", required=True)
    profile_revoke.add_argument("--revoked-by", default="agent")
    profile_revoke.add_argument("--state-dir", required=True, type=Path)
    profile_revoke.add_argument(
        "--confirmed-by-editor",
        action="store_true",
        default=False,
        help="Apply the revocation after editor confirmation. Without this flag, prints preview and returns code 3.",
    )
    profile_set_terms = profile_subparsers.add_parser(
        "set-terms",
        help="Supply explicit matching terms for one existing active topic profile entry.",
    )
    profile_set_terms.add_argument("--id", required=True)
    profile_set_terms.add_argument(
        "--match-term",
        action="append",
        required=True,
        dest="matching_terms",
        help="Explicit matching term to store on the entry (repeatable). Values are trimmed and deduplicated.",
    )
    profile_set_terms.add_argument("--state-dir", required=True, type=Path)
    profile_set_terms.add_argument(
        "--confirmed-by-editor",
        action="store_true",
        default=False,
        help="Apply the terms after editor confirmation. Without this flag, prints preview and returns code 3.",
    )


def _handle_editorial_profile(args: argparse.Namespace) -> int:
    if args.profile_command == "add":
        matching_terms = args.matching_terms if hasattr(args, "matching_terms") else None
        replace_ids = args.replace_ids if hasattr(args, "replace_ids") and args.replace_ids else None
        overlap_resolution = args.overlap_resolution if hasattr(args, "overlap_resolution") else ""
        replacement_reason = args.replacement_reason if hasattr(args, "replacement_reason") else ""
        return add_profile_entry(
            args.state_dir,
            args.kind,
            args.scope,
            args.subject,
            args.quote,
            args.provenance,
            args.created_by,
            args.direction,
            args.description,
            args.example_books,
            confirmed_by_editor=args.confirmed_by_editor,
            matching_terms=matching_terms,
            overlap_resolution=overlap_resolution,
            replace_ids=replace_ids,
            replacement_reason=replacement_reason,
            exception_terms=args.exception_terms,
        )
    if args.profile_command == "list":
        return list_profile_entries(args.state_dir)
    if args.profile_command == "revoke":
        return revoke_profile_entry(
            args.state_dir,
            args.id,
            args.reason,
            args.revoked_by,
            confirmed_by_editor=args.confirmed_by_editor,
        )
    if args.profile_command == "set-terms":
        return set_profile_entry_terms(
            args.state_dir,
            args.id,
            args.matching_terms,
            confirmed_by_editor=args.confirmed_by_editor,
        )
    return 1


def _register_audit_editorial_notes(subparsers: SubParsers) -> None:
    audit_parser = subparsers.add_parser(
        "audit-editorial-notes",
        help="Build a deterministic offline editorial audit outside canonical weekly/editorial state.",
    )
    audit_parser.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_EDITORIAL_AUDIT_STATE_DIR,
        help="Weekly state directory holding Candidate History and the Editorial Taste Profile.",
    )
    audit_parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_EDITORIAL_AUDIT_OUTPUT_DIR,
        help="Derived output directory for editorial-audit.json/md (default: data/editorial-audit).",
    )
    audit_parser.add_argument(
        "--run-date",
        default=None,
        type=_validate_iso_date,
        help="Effective ISO audit date (YYYY-MM-DD). Defaults to today.",
    )


def _handle_audit_editorial_notes(args: argparse.Namespace) -> int:
    report, code = run_editorial_audit(
        EditorialAuditRequest(
            state_dir=args.state_dir,
            output_dir=args.output_dir,
            run_date=args.run_date,
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return code


COMMANDS: dict[str, CliCommand] = {
    "editorial-profile": CliCommand(_register_editorial_profile, _handle_editorial_profile),
    "audit-editorial-notes": CliCommand(_register_audit_editorial_notes, _handle_audit_editorial_notes),
}
