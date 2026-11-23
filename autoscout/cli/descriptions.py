"""Description and English-title commands: prepare-description-backfill, apply-description-backfill, prepare-english-title-backfill, apply-english-title-backfill, generate-descriptions, adjudicate-descriptions."""

from __future__ import annotations

import argparse
from pathlib import Path

from autoscout.cli.common import CliCommand
from autoscout.cli.common import SubParsers
from autoscout.cli.common import _review_config
from autoscout.cli.common import _validate_iso_date
from autoscout.content_description import apply_description_artifacts
from autoscout.content_description import prepare_backfill_work_items
from autoscout.description_adjudication import ADJUDICATION_MAX_WORKERS
from autoscout.description_adjudication import ADJUDICATION_STAGE_BUDGET_SECONDS
from autoscout.english_title_backfill import apply_english_title_backfill
from autoscout.english_title_backfill import prepare_english_title_backfill
from autoscout.json_io import write_json


def _register_prepare_description_backfill(subparsers: SubParsers) -> None:
    backfill_prepare_parser = subparsers.add_parser(
        "prepare-description-backfill",
        help="Prepare metadata-only work items for blank and fallback Russian Content Descriptions.",
    )
    backfill_prepare_parser.add_argument("--state-dir", required=True, type=Path)
    backfill_prepare_parser.add_argument("--output-file", required=True, type=Path)


def _handle_prepare_description_backfill(args: argparse.Namespace) -> int:
    return _prepare_description_backfill(args.state_dir, args.output_file)


def _register_apply_description_backfill(subparsers: SubParsers) -> None:
    backfill_apply_parser = subparsers.add_parser(
        "apply-description-backfill",
        help="Apply validated backfill artifacts to Candidate History.",
    )
    backfill_apply_parser.add_argument("--state-dir", required=True, type=Path)
    backfill_apply_parser.add_argument("--artifacts-file", required=True, type=Path)
    backfill_apply_parser.add_argument(
        "--allow-overwrite-valid",
        action="store_true",
        help="Allow overwriting existing valid descriptions (default: skip).",
    )


def _handle_apply_description_backfill(args: argparse.Namespace) -> int:
    return _apply_description_backfill(args.state_dir, args.artifacts_file, args.allow_overwrite_valid)


def _register_prepare_english_title_backfill(subparsers: SubParsers) -> None:
    title_backfill_prepare_parser = subparsers.add_parser(
        "prepare-english-title-backfill",
        help="Prepare metadata-only work items for history rows missing an English title.",
    )
    title_backfill_prepare_parser.add_argument("--state-dir", required=True, type=Path)
    title_backfill_prepare_parser.add_argument("--output-file", required=True, type=Path)
    title_backfill_prepare_parser.add_argument(
        "--config",
        required=True,
        type=Path,
        help="Weekly portfolio config used to map evidence sources to declared languages.",
    )
    title_backfill_prepare_parser.add_argument(
        "--editor-csv",
        type=Path,
        default=None,
        help="Editor-facing CSV used to mark editor-visible rows (optional).",
    )


def _handle_prepare_english_title_backfill(args: argparse.Namespace) -> int:
    return _prepare_english_title_backfill(args.state_dir, args.output_file, args.editor_csv, args.config)


def _register_apply_english_title_backfill(subparsers: SubParsers) -> None:
    title_backfill_apply_parser = subparsers.add_parser(
        "apply-english-title-backfill",
        help="Apply curated English-title artifacts to Candidate History.",
    )
    title_backfill_apply_parser.add_argument("--state-dir", required=True, type=Path)
    title_backfill_apply_parser.add_argument("--artifacts-file", required=True, type=Path)
    title_backfill_apply_parser.add_argument(
        "--allow-overwrite-valid",
        action="store_true",
        help="Allow overwriting existing valid (non-evidence) helpers (default: skip).",
    )
    title_backfill_apply_parser.add_argument(
        "--confirm",
        action="store_true",
        help="Explicit confirmation: refuse to write anything without this flag.",
    )


def _handle_apply_english_title_backfill(args: argparse.Namespace) -> int:
    return _apply_english_title_backfill(args.state_dir, args.artifacts_file, args.allow_overwrite_valid, args.confirm)


def _register_generate_descriptions(subparsers: SubParsers) -> None:
    generate_parser = subparsers.add_parser(
        "generate-descriptions",
        help="Generate source-grounded descriptions from Candidate History.",
    )
    generate_parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path("runs/current-weekly-pilot-russian"),
    )
    generate_parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("runs/current-weekly-pilot-russian"),
    )
    generate_parser.add_argument(
        "--limit", type=int, default=None, help="Positive number of eligible candidates to attempt."
    )
    generate_parser.add_argument(
        "--only",
        action="append",
        default=None,
        help=(
            "Explicit named selection. Attempt named candidates even when they already have a grounded "
            "description or a recent failure. Repeat for multiple candidates."
        ),
    )
    generate_parser.add_argument(
        "--only-file",
        type=Path,
        default=None,
        help=(
            "Explicit named selection, one candidate identity key per line. Named candidates are attempted "
            "even when they already have a grounded description or a recent failure."
        ),
    )
    generate_parser.add_argument("--retry-failed", action="store_true", default=False)
    generate_parser.add_argument("--run-date", default=None, type=_validate_iso_date)


def _handle_generate_descriptions(args: argparse.Namespace) -> int:
    from autoscout.generate_descriptions import DescriptionGenerationPreflightError
    from autoscout.generate_descriptions import generate_descriptions

    try:
        generate_descriptions(
            args.state_dir,
            args.output_dir,
            limit=args.limit,
            retry_failed=args.retry_failed,
            only=args.only,
            only_file=args.only_file,
            effective_date=args.run_date,
            review_config=_review_config(),
        )
    except DescriptionGenerationPreflightError as exc:
        if exc.reason_code == "discovery_endpoint_not_configured":
            print("generate-descriptions: discovery endpoint is not configured")
        elif exc.reason_code == "discovery_adapter_unavailable":
            print("generate-descriptions: discovery adapter is unavailable")
        elif exc.reason_code == "discovery_adapter_not_running":
            print("generate-descriptions: discovery adapter is not running")
        elif exc.reason_code == "discovery_adapter_key_missing":
            print(f"generate-descriptions: {exc}")
        elif exc.reason_code == "discovery_adapter_vendor_error":
            print(f"generate-descriptions: discovery adapter vendor error: {exc}")
        else:
            print("generate-descriptions: discovery adapter did not answer preflight")
        return 2
    except KeyboardInterrupt:
        print("generate-descriptions: interrupted")
        return 130
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        print(f"generate-descriptions: {exc}")
        return 1
    print("generate-descriptions: completed")
    return 0


def _register_adjudicate_descriptions(subparsers: SubParsers) -> None:
    adjudicate_parser = subparsers.add_parser(
        "adjudicate-descriptions",
        help=(
            "Preview description adjudication from Candidate History, and with --apply write the "
            "accepted descriptions through the existing description apply gate."
        ),
    )
    adjudicate_parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path("runs/current-weekly-pilot-russian"),
    )
    adjudicate_parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("runs/current-weekly-pilot-russian"),
    )
    adjudicate_parser.add_argument(
        "--limit", type=int, default=None, help="Positive number of eligible candidates to preview."
    )
    adjudicate_parser.add_argument(
        "--only",
        action="append",
        default=None,
        help=(
            "Explicit named selection. Only named candidates that need description work are previewed. "
            "Repeat for multiple candidates."
        ),
    )
    adjudicate_parser.add_argument(
        "--only-file",
        type=Path,
        default=None,
        help=(
            "Explicit named selection, one candidate identity key per line. Only named candidates that need "
            "description work are previewed."
        ),
    )
    adjudicate_parser.add_argument("--run-date", default=None, type=_validate_iso_date)
    adjudicate_parser.add_argument(
        "--adapter",
        choices=("replay", "codex"),
        default="replay",
        help=(
            "Adjudication adapter. 'replay' is the deterministic offline adapter used by the "
            "fixture run; 'codex' runs the live Codex CLI with web search, one candidate per call."
        ),
    )
    adjudicate_parser.add_argument(
        "--deep-pass",
        action="store_true",
        help=(
            "Give a candidate the first call leaves at the placeholder one more, deeper call: ISBN and "
            "original-language title, publisher and bookshop pages first. The weekly run always does this."
        ),
    )
    adjudicate_parser.add_argument(
        "--preview-file",
        type=Path,
        default=None,
        help="Write the preview artifact to this path instead of the output directory default.",
    )
    adjudicate_parser.add_argument(
        "--max-workers",
        type=int,
        default=ADJUDICATION_MAX_WORKERS,
        help=(
            "Bounded parallel workers for the per-candidate loop. This stage's own default is 3; "
            "it is not the grounded stage worker count."
        ),
    )
    adjudicate_parser.add_argument(
        "--stage-budget-seconds",
        type=float,
        default=ADJUDICATION_STAGE_BUDGET_SECONDS,
        help=(
            "Stage clock: stop admitting new candidates when the remaining time cannot fit one "
            "more call. Unattempted candidates are reported with not_attempted_stage_budget."
        ),
    )
    adjudicate_parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help=(
            "Write accepted descriptions into Candidate History through the existing apply gate. "
            "Without this option the command writes a complete preview and changes no state."
        ),
    )


def _handle_adjudicate_descriptions(args: argparse.Namespace) -> int:
    from autoscout.description_adjudication import adjudicate_descriptions

    try:
        result = adjudicate_descriptions(
            args.state_dir,
            args.output_dir,
            limit=args.limit,
            only=args.only,
            only_file=args.only_file,
            effective_date=args.run_date,
            adapter_kind=args.adapter,
            preview_file=args.preview_file,
            apply=args.apply,
            max_workers=args.max_workers,
            stage_budget_seconds=args.stage_budget_seconds,
            deep_pass=args.deep_pass,
        )
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        print(f"adjudicate-descriptions: {exc}")
        return 1
    print(f"adjudicate-descriptions: previewed={result['previewed']}")
    print(f"adjudicate-descriptions: considered={result['considered']}")
    print(f"adjudicate-descriptions: valid={result['valid']}")
    print(f"adjudicate-descriptions: rejected={result['rejected']}")
    # A dry run must never report work it did not do. It reports what an
    # apply run would do instead, under a name that says so.
    if args.apply:
        print(f"adjudicate-descriptions: applied={result['applied']}")
    else:
        print(f"adjudicate-descriptions: would_apply={result['would_apply']}")
    print(f"adjudicate-descriptions: skipped={result['skipped']}")
    print(f"adjudicate-descriptions: fallback={result['fallback']}")
    print(f"adjudicate-descriptions: evidence_unchanged={result['evidence_unchanged']}")
    print(f"adjudicate-descriptions: not_attempted={result['not_attempted']}")
    print(f"adjudicate-descriptions: preview={result['preview_file']}")
    print("adjudicate-descriptions: completed")
    return 0


def _prepare_description_backfill(state_dir: Path, output_file: Path) -> int:
    """Prepare metadata-only backfill work items for blank, fallback, and stub descriptions."""
    from autoscout.state_store import Collection
    from autoscout.state_store import open_state_store

    store = open_state_store(state_dir)
    candidate_history = store.read(Collection.CANDIDATE_HISTORY)
    if not isinstance(candidate_history, list):
        print("error=invalid_candidate_history")
        return 1
    visibility_records = store.read(Collection.CANDIDATE_VISIBILITY)
    if not isinstance(visibility_records, list):
        visibility_records = []

    # A codex-generated stub is a guess from the title, and the apply
    # command may replace it without --allow-overwrite-valid (#791), so the
    # work list names it too (issue #912).
    items = prepare_backfill_work_items(
        candidate_history,
        visibility_records=visibility_records,
        include_unsourced_stubs=True,
    )
    write_json(output_file, {"work_items": items})

    print(f"backfill_work_items={len(items)}")
    print(f"output_file={output_file}")
    return 0


def _apply_description_backfill(state_dir: Path, artifacts_file: Path, allow_overwrite_valid: bool = False) -> int:
    """Apply validated backfill artifacts to Candidate History and its snapshots."""
    from autoscout.description_projection import project_history_onto_snapshots
    from autoscout.json_io import read_json_or_default
    from autoscout.state_store import Collection
    from autoscout.state_store import open_state_store

    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        artifacts_data = read_json_or_default(artifacts_file, {})
    except Exception as exc:
        artifacts_data = exc
    store = open_state_store(state_dir)
    # Candidate History is read, decided on and written in one store
    # transaction, so a value written in between is never replaced.
    with store.transaction():
        candidate_history = store.read(Collection.CANDIDATE_HISTORY)
        if not isinstance(candidate_history, list):
            print("error=invalid_candidate_history")
            return 1
        if isinstance(artifacts_data, Exception):
            print(f"error=malformed_artifacts_file: {artifacts_data}")
            return 1

        artifacts = artifacts_data.get("backfill_items", artifacts_data.get("work_items", []))
        if not isinstance(artifacts, list):
            print("error=artifacts_must_be_a_list")
            return 1

        # A researched description replaces a description no source confirmed
        # without the general overwrite flag: the stub has nothing to protect
        # (issue #791).
        updated_history, results = apply_description_artifacts(
            candidate_history,
            artifacts,
            allow_overwrite_valid=allow_overwrite_valid,
            allow_unsourced_replacement=True,
        )
        store.replace(Collection.CANDIDATE_HISTORY, updated_history)
    # History is durable first; the snapshots then show the same text, so
    # the resolver and previews never read a replaced stub (issue #912).
    projection = project_history_onto_snapshots(updated_history, state_dir, store)

    print(f"backfill_applied={results['applied']}")
    print(f"backfill_skipped={results['skipped']}")
    print(f"backfill_invalid={results['invalid']}")
    print(f"backfill_fallback={results['fallback']}")
    print(f"backfill_unknown_identity={results['unknown_identity']}")
    for snapshot in ("current", "latest"):
        snapshot_summary = projection.get(snapshot)
        if snapshot_summary is not None:
            print(f"backfill_projected_{snapshot}={snapshot_summary['projected']}")
    return 0


def _prepare_english_title_backfill(
    state_dir: Path, output_file: Path, editor_csv: Path | None, config_path: Path
) -> int:
    """Prepare metadata-only work items for history rows missing an English title."""
    import csv

    from autoscout.config import load_validated_config
    from autoscout.state_store import Collection
    from autoscout.state_store import open_state_store

    candidate_history = open_state_store(state_dir).read(Collection.CANDIDATE_HISTORY)
    if not isinstance(candidate_history, list):
        print("error=invalid_candidate_history")
        return 1
    try:
        portfolio = load_validated_config(config_path)
    except Exception as exc:
        print(f"error=invalid_config: {exc}")
        return 1
    source_languages = {
        str(source.get("name", "")): str(source.get("language", ""))
        for source in portfolio.get("sources", [])
        if isinstance(source, dict)
    }
    editor_titles: set[str] = set()
    if editor_csv is not None:
        try:
            with editor_csv.open(encoding="utf-8", newline="") as handle:
                editor_titles = {row.get("Title", "") for row in csv.DictReader(handle)}
        except OSError as exc:
            print(f"error=unreadable_editor_csv: {exc}")
            return 1

    items = prepare_english_title_backfill(
        candidate_history, editor_titles=editor_titles, source_languages=source_languages
    )
    write_json(output_file, {"work_items": items})

    print(f"backfill_work_items={len(items)}")
    print(f"output_file={output_file}")
    return 0


def _apply_english_title_backfill(
    state_dir: Path, artifacts_file: Path, allow_overwrite_valid: bool = False, confirm: bool = False
) -> int:
    """Apply curated English-title artifacts to Candidate History."""
    from autoscout.json_io import read_json_or_default
    from autoscout.state_store import Collection
    from autoscout.state_store import open_state_store

    if not confirm:
        print("error=refusing_to_apply_without_confirm: re-run with --confirm after reviewing the preview")
        return 2
    try:
        artifacts_data = read_json_or_default(artifacts_file, {})
    except Exception as exc:
        artifacts_data = exc
    store = open_state_store(state_dir)
    # Candidate History is read, decided on and written in one store
    # transaction, so a title written in between is never replaced.
    with store.transaction():
        candidate_history = store.read(Collection.CANDIDATE_HISTORY)
        if not isinstance(candidate_history, list):
            print("error=invalid_candidate_history")
            return 1
        if isinstance(artifacts_data, Exception):
            print(f"error=malformed_artifacts_file: {artifacts_data}")
            return 1

        artifacts = artifacts_data.get("backfill_items", artifacts_data.get("work_items", []))
        if not isinstance(artifacts, list):
            print("error=artifacts_must_be_a_list")
            return 1

        updated_history, results = apply_english_title_backfill(
            candidate_history, artifacts, allow_overwrite_valid=allow_overwrite_valid
        )
        store.replace(Collection.CANDIDATE_HISTORY, updated_history)

    print(f"backfill_applied={results['applied']}")
    print(f"backfill_skipped_existing={results['skipped_existing']}")
    print(f"backfill_skipped_evidence={results['skipped_evidence']}")
    print(f"backfill_invalid={results['invalid']}")
    print(f"backfill_unknown_identity={results['unknown_identity']}")
    return 0


COMMANDS: dict[str, CliCommand] = {
    "prepare-description-backfill": CliCommand(
        _register_prepare_description_backfill, _handle_prepare_description_backfill
    ),
    "apply-description-backfill": CliCommand(_register_apply_description_backfill, _handle_apply_description_backfill),
    "prepare-english-title-backfill": CliCommand(
        _register_prepare_english_title_backfill, _handle_prepare_english_title_backfill
    ),
    "apply-english-title-backfill": CliCommand(
        _register_apply_english_title_backfill, _handle_apply_english_title_backfill
    ),
    "generate-descriptions": CliCommand(_register_generate_descriptions, _handle_generate_descriptions),
    "adjudicate-descriptions": CliCommand(_register_adjudicate_descriptions, _handle_adjudicate_descriptions),
}
