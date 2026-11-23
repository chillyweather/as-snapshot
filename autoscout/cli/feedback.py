"""Editor feedback commands: ingest-feedback, record-feedback, record-feedback-batch, record-visibility, export-decision-corpus."""

from __future__ import annotations

import argparse
from pathlib import Path

from autoscout.cli.common import CliCommand
from autoscout.cli.common import SubParsers
from autoscout.editor_feedback import ingest_feedback
from autoscout.editor_feedback import record_feedback
from autoscout.editor_feedback import record_visibility
from autoscout.feedback_batch import record_feedback_batch
from autoscout.feedback_dimensions import DIMENSION_ATTRS
from autoscout.feedback_dimensions import FeedbackDimensionError
from autoscout.feedback_dimensions import canonical_dimensions_from_raw


def _register_ingest_feedback(subparsers: SubParsers) -> None:
    ingest_parser = subparsers.add_parser(
        "ingest-feedback", help="Stage returned editor feedback from a Sheet CSV export."
    )
    ingest_parser.add_argument("--csv", required=True, type=Path)
    ingest_parser.add_argument("--state-dir", required=True, type=Path)
    ingest_parser.add_argument("--source-ideas-csv", type=Path)


def _handle_ingest_feedback(args: argparse.Namespace) -> int:
    return ingest_feedback(args.csv, args.state_dir, args.source_ideas_csv)


def _register_record_feedback(subparsers: SubParsers) -> None:
    record_parser = subparsers.add_parser(
        "record-feedback", help="Record conversational agent-session feedback for one candidate."
    )
    record_parser.add_argument("--identity-key", required=True)
    record_parser.add_argument("--status", required=True)
    record_parser.add_argument("--note", default="")
    record_parser.add_argument(
        "--appeal",
        default="",
        help="Structured dimension: editorial appeal (appealing, not_appealing). Omit when not discussed.",
    )
    record_parser.add_argument(
        "--rights",
        default="",
        help="Structured dimension: editor-reported rights/acquisition availability (available, unavailable, unknown).",
    )
    record_parser.add_argument(
        "--quality-problem",
        default="",
        help=(
            "Structured dimension: product/data-quality problem "
            "(none, description_missing, wrong_category, bad_links, unusable_title, other)."
        ),
    )
    record_parser.add_argument(
        "--info-needed",
        default="",
        help=(
            "Structured dimension: information still needed "
            "(none, purchase_status, rights_status, russian_edition, description, other)."
        ),
    )
    record_parser.add_argument("--state-dir", required=True, type=Path)
    record_parser.add_argument(
        "--confirmed-by-editor",
        action="store_true",
        default=False,
        help="Apply the feedback after editor confirmation. Without this flag, prints preview and returns code 3.",
    )


def _handle_record_feedback(args: argparse.Namespace) -> int:
    try:
        dimensions = canonical_dimensions_from_raw(
            {name: getattr(args, attr) for name, attr in DIMENSION_ATTRS.items()}
        )
    except FeedbackDimensionError as exc:
        print(str(exc))
        return 1
    return record_feedback(
        args.identity_key,
        args.status,
        args.note,
        args.state_dir,
        args.confirmed_by_editor,
        dimensions,
    )


def _register_record_feedback_batch(subparsers: SubParsers) -> None:
    batch_parser = subparsers.add_parser(
        "record-feedback-batch",
        help="Record a returned file of editor decisions in one confirmed batch (two-phase, like record-feedback).",
    )
    batch_parser.add_argument(
        "--decisions",
        required=True,
        type=Path,
        help=(
            "JSON list of decisions: identity_key or candidate_id, status, note (verbatim), "
            "and optional appeal, rights, quality_problem, info_needed."
        ),
    )
    batch_parser.add_argument("--state-dir", required=True, type=Path)
    batch_parser.add_argument(
        "--confirmed-by-editor",
        action="store_true",
        default=False,
        help="Stage every decision after editor confirmation. Without this flag, prints the preview and returns code 3.",
    )
    batch_parser.add_argument(
        "--digest", default="", help="The batch_digest the preview printed; required with --confirmed-by-editor."
    )


def _handle_record_feedback_batch(args: argparse.Namespace) -> int:
    return record_feedback_batch(args.decisions, args.state_dir, args.confirmed_by_editor, args.digest)


def _register_record_visibility(subparsers: SubParsers) -> None:
    visibility_parser = subparsers.add_parser(
        "record-visibility", help="Record a confirmed hide/remove or restore/show action for one candidate."
    )
    visibility_parser.add_argument("--identity-key", required=True)
    visibility_parser.add_argument("--action", required=True, help="hide/remove or restore/show/show-again")
    visibility_parser.add_argument("--reason", default="")
    visibility_parser.add_argument("--provenance", default="agent-confirmed editor action")
    visibility_parser.add_argument("--state-dir", required=True, type=Path)


def _handle_record_visibility(args: argparse.Namespace) -> int:
    return record_visibility(
        args.identity_key,
        args.action,
        args.state_dir,
        args.reason,
        args.provenance,
    )


def _register_export_decision_corpus(subparsers: SubParsers) -> None:
    export_parser = subparsers.add_parser(
        "export-decision-corpus",
        help="Read-only: write the dated editor decisions, rebuilt from Git history, to a local Git-ignored directory.",
    )
    export_parser.add_argument("--state-dir", required=True, type=Path)
    export_parser.add_argument(
        "--output-dir", type=Path, default=None, help="Default: data/decision-corpus/ in the repository."
    )
    export_parser.add_argument("--ref", default="HEAD", help="The commit whose history is read (default HEAD).")


def _handle_export_decision_corpus(args: argparse.Namespace) -> int:
    from autoscout.decision_corpus import export_decision_corpus_command

    return export_decision_corpus_command(args.state_dir, args.output_dir, args.ref)


COMMANDS: dict[str, CliCommand] = {
    "ingest-feedback": CliCommand(_register_ingest_feedback, _handle_ingest_feedback),
    "record-feedback": CliCommand(_register_record_feedback, _handle_record_feedback),
    "record-feedback-batch": CliCommand(_register_record_feedback_batch, _handle_record_feedback_batch),
    "record-visibility": CliCommand(_register_record_visibility, _handle_record_visibility),
    "export-decision-corpus": CliCommand(_register_export_decision_corpus, _handle_export_decision_corpus),
}
