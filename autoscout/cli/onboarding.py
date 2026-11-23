"""Source probe and onboarding commands: probe-source-candidate, add-source, capture-source-fixtures, scaffold-source."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from autoscout.cli.common import WEEKLY_RUN_IN_PROGRESS_EXIT_CODE
from autoscout.cli.common import CliCommand
from autoscout.cli.common import SubParsers
from autoscout.cli.common import _editor_mutation_blocked
from autoscout.cli.common import _validate_iso_date
from autoscout.replay_fixtures import DEFAULT_OUTPUT_DIR
from autoscout.replay_fixtures import capture_source_fixtures
from autoscout.source_collectors.base import CONTRACT_FAMILIES
from autoscout.source_metadata import RANKING_FIELD_SPECS_BY_NAME
from autoscout.source_metadata import RANKING_RANK_SOURCES
from autoscout.source_metadata import RANKING_TABLE_FIELD_SPECS
from autoscout.source_metadata import RANKING_TABLE_FIELDS
from autoscout.source_metadata import SOURCE_CONTRACT_FIELDS
from autoscout.source_metadata import SOURCE_LAYOUT_FIELDS
from autoscout.source_metadata import SOURCE_SCAFFOLD_METADATA_FIELDS
from autoscout.source_metadata import SOURCE_SCAFFOLD_OPTIONAL_METADATA_FIELDS
from autoscout.source_onboarding import add_source_command
from autoscout.source_onboarding_models import ConfigPaths
from autoscout.source_onboarding_policy import DEFAULT_STATE_DIR
from autoscout.source_onboarding_policy import proposal_file_for
from autoscout.source_onboarding_policy import repo_root_from_paths
from autoscout.source_probe import DEFAULT_FEASIBILITY_DIR
from autoscout.source_probe import probe_source_candidates
from autoscout.source_scaffold import scaffold_probation_source


def _register_probe_source_candidate(subparsers: SubParsers) -> None:
    source_probe_parser = subparsers.add_parser(
        "probe-source-candidate",
        help="Probe unconfigured Source Candidates in one bounded live batch.",
    )
    source_probe_parser.add_argument(
        "--url",
        action="append",
        required=True,
        dest="urls",
        help="Candidate HTTP(S) URL. Repeat for each candidate.",
    )
    source_probe_parser.add_argument(
        "--name",
        action="append",
        default=[],
        dest="names",
        help="Optional candidate name, aligned by URL order. Repeatable.",
    )
    source_probe_parser.add_argument(
        "--signal-family",
        action="append",
        default=[],
        dest="signal_families",
        help="Optional signal family, aligned by URL order. Repeatable.",
    )
    source_probe_parser.add_argument(
        "--proposing-context",
        action="append",
        default=[],
        dest="proposing_contexts",
        help="Optional proposing context, aligned by URL order. Repeatable.",
    )
    source_probe_parser.add_argument(
        "--visible-title-selector",
        action="append",
        default=[],
        dest="visible_title_selectors",
        help="Optional visible page-title selector (.class-token > .class-token), aligned by URL order. Repeatable.",
    )
    source_probe_parser.add_argument(
        "--entry-scope-selector",
        action="append",
        default=[],
        dest="entry_scope_selectors",
        help="Optional entry scope selector (.scope > .entry), aligned by URL order. Repeatable.",
    )
    source_probe_parser.add_argument(
        "--candidate-category",
        "--eligible-candidate-category",
        action="append",
        default=[],
        dest="eligible_candidate_categories",
        help="Optional configured candidate category, aligned by URL order. Repeatable.",
    )
    source_probe_parser.add_argument(
        "--contract-family",
        action="append",
        default=[],
        choices=CONTRACT_FAMILIES,
        dest="contract_families",
        help="Optional feasibility/collection contract family, aligned by URL order. Repeatable.",
    )
    # Ranking declarations are optional CLI conveniences for the pure
    # ranking-table seam. They are aligned by URL order like the selectors.
    for spec in RANKING_TABLE_FIELD_SPECS:
        if not spec.probe_option:
            continue
        source_probe_parser.add_argument(
            spec.probe_option,
            action="append",
            default=[],
            dest=spec.probe_dest,
            type=(_parse_cli_bool if spec.value_kind == "bool" else int if spec.value_kind == "int" else str),
            choices=spec.probe_choices or None,
            help=spec.probe_help or None,
        )
    source_probe_parser.add_argument(
        "--keep-captures",
        type=Path,
        default=None,
        help="Keep bounded evidence for accepted candidates in this directory.",
    )
    source_probe_parser.add_argument(
        "--re-probe",
        action="store_true",
        help="Issue one fresh bounded probe for URLs already in the durable ledger.",
    )
    source_probe_parser.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help="Source Candidate Ledger JSON path (default: data/source-candidate-ledger.json).",
    )
    source_probe_parser.add_argument(
        "--feasibility-dir",
        type=Path,
        default=DEFAULT_FEASIBILITY_DIR,
        help="Write one deterministic feasibility draft per accepted candidate here "
        "(default: data/source-feasibility-drafts).",
    )


def _handle_probe_source_candidate(args: argparse.Namespace) -> int:
    ranking_metadata: list[dict[str, Any]] = []
    probe_specs = tuple(spec for spec in RANKING_TABLE_FIELD_SPECS if spec.probe_option)
    ranking_options = tuple((spec, getattr(args, spec.probe_dest)) for spec in probe_specs)
    candidate_categories = args.eligible_candidate_categories
    if any(option_values for _spec, option_values in ranking_options) or candidate_categories:
        for index in range(len(args.urls)):
            values = {
                spec.name: option_values[index]
                for spec, option_values in ranking_options
                if spec.probe_scope == "aligned" and index < len(option_values) and str(option_values[index]).strip()
            }
            if index < len(candidate_categories) and candidate_categories[index].strip():
                values["eligible_candidate_category"] = candidate_categories[index].strip()
            if index == 0:
                for spec, option_values in ranking_options:
                    if spec.probe_scope == "first" and option_values:
                        values[spec.name] = list(option_values)
            ranking_metadata.append(values)
    return probe_source_candidates(
        args.urls,
        args.names,
        args.signal_families,
        proposing_contexts=args.proposing_contexts,
        visible_title_selectors=args.visible_title_selectors,
        entry_scope_selectors=args.entry_scope_selectors,
        contract_families=args.contract_families,
        contract_metadata=ranking_metadata,
        keep_captures=args.keep_captures,
        ledger_path=args.ledger,
        feasibility_dir=args.feasibility_dir,
        re_probe=args.re_probe,
    )


def _register_add_source(subparsers: SubParsers) -> None:
    add_source_parser = subparsers.add_parser(
        "add-source",
        help="Onboard exactly one source from retained accepted probe evidence.",
    )
    add_source_parser.add_argument(
        "--url",
        "--source-url",
        dest="url",
        action="store",
        default=None,
        help="Exactly one HTTP(S) source URL; repeating this option is rejected.",
    )
    add_source_parser.add_argument("--probe-evidence", "--captured-evidence", dest="probe_evidence", type=Path)
    add_source_parser.add_argument("--helper-metadata", type=Path, dest="helper_metadata_file")
    add_source_parser.add_argument(
        "--helper-metadata-destination", "--canonical-helper-metadata", type=Path, default=None
    )
    add_source_parser.add_argument("--source-name", "--name", dest="source_name", default=None)
    add_source_parser.add_argument("--slug", default=None)
    add_source_parser.add_argument("--reason", default="")
    add_source_parser.add_argument("--proposed-by", default="agent")
    add_source_parser.add_argument("--access-reason", default="")
    add_source_parser.add_argument("--state-dir", "--proposal-state-dir", dest="state_dir", type=Path)
    add_source_parser.add_argument(
        "--proposals-file",
        "--proposal-file",
        dest="proposal_file",
        type=Path,
        help="The Source Proposals file of a state directory, as the State Store keeps it.",
    )
    add_source_parser.add_argument("--repo-root", type=Path)
    add_source_parser.add_argument("--config-dir", type=Path)
    add_source_parser.add_argument("--portfolio-config", type=Path)
    add_source_parser.add_argument("--pilot-config", type=Path)
    add_source_parser.add_argument("--pilot-live-config", type=Path)
    add_source_parser.add_argument("--live-config", type=Path)
    add_source_parser.add_argument(
        "--feasibility-evidence", "--feasibility-input", dest="feasibility_evidence", type=Path
    )
    add_source_parser.add_argument("--feasibility-doc", "--feasibility-destination", dest="feasibility_doc", type=Path)
    add_source_parser.add_argument("--source-specific-test", "--source-test", dest="source_specific_test", type=Path)
    add_source_parser.add_argument(
        "--onboarding-artifact",
        dest="onboarding_artifacts",
        action="append",
        default=None,
        type=Path,
        help=(
            "Dirty file under tests/ that this onboarding owns, such as a portfolio-composition pin; "
            "repeatable. It is validated and committed with the bundle."
        ),
    )
    add_source_parser.add_argument("--run-date", default=None, type=_validate_iso_date)
    add_source_parser.add_argument(
        "--confirmation-token",
        default=None,
        help="Exact token returned by the matching unconfirmed preview.",
    )
    add_source_parser.add_argument("--onboarding-issue", "--issue", dest="onboarding_issue", default="")
    add_source_parser.add_argument(
        "--confirmed-by-editor", "--confirmed", action="store_true", default=False, dest="confirmed_by_editor"
    )
    add_source_parser.add_argument("--contract-family", choices=CONTRACT_FAMILIES, default=None)
    add_ranking_table_arguments(add_source_parser)
    for field in SOURCE_LAYOUT_FIELDS:
        add_source_parser.add_argument(f"--{field.replace('_', '-')}", default=None, dest=field)
    for field in SOURCE_SCAFFOLD_METADATA_FIELDS:
        if field in {"source_name", "url"}:
            continue
        aliases = [f"--{field.replace('_', '-')}"]
        if field == "expected_page_heading":
            aliases.append("--expected-heading")
        elif field == "expected_signal_strength":
            aliases.append("--signal-strength")
        elif field == "eligible_candidate_category":
            aliases.append("--candidate-category")
        add_source_parser.add_argument(*aliases, default=None, dest=field)


def _handle_add_source(args: argparse.Namespace) -> int:
    config_dir = args.config_dir
    metadata = {
        field: getattr(args, field)
        for field in (*SOURCE_SCAFFOLD_METADATA_FIELDS, *SOURCE_LAYOUT_FIELDS)
        if field not in {"source_name", "url"} and getattr(args, field, None) is not None
    }
    for field in (*SOURCE_CONTRACT_FIELDS, *RANKING_TABLE_FIELDS):
        value = getattr(args, field, None)
        if value is not None:
            metadata[field] = value
    if args.source_name is not None:
        metadata["source_name"] = args.source_name
    resolved_root = repo_root_from_paths(
        args.repo_root,
        [
            args.proposal_file,
            args.state_dir,
            args.config_dir,
            args.probe_evidence,
            args.helper_metadata_file,
            args.feasibility_evidence,
            args.feasibility_doc,
            args.source_specific_test,
            *(args.onboarding_artifacts or ()),
            args.portfolio_config,
            args.pilot_config,
            args.pilot_live_config,
            args.live_config,
        ],
    )
    proposal_state_dir = (
        args.proposal_file.parent
        if args.proposal_file is not None
        else args.state_dir or resolved_root / DEFAULT_STATE_DIR
    )
    if args.confirmed_by_editor and _editor_mutation_blocked(proposal_state_dir):
        return WEEKLY_RUN_IN_PROGRESS_EXIT_CODE
    config_paths = ConfigPaths.from_root(
        resolved_root,
        config_dir=config_dir,
        portfolio=args.portfolio_config,
        pilot=args.pilot_config,
        pilot_live=args.pilot_live_config,
        live=args.live_config,
    )
    return add_source_command(
        url=args.url,
        probe_evidence=args.probe_evidence,
        helper_metadata_file=args.helper_metadata_file,
        helper_metadata_destination=args.helper_metadata_destination,
        feasibility_evidence=args.feasibility_evidence,
        proposal_file=args.proposal_file,
        state_dir=args.state_dir,
        config_paths=config_paths,
        feasibility_doc=args.feasibility_doc,
        source_specific_test=args.source_specific_test,
        onboarding_artifacts=tuple(args.onboarding_artifacts or ()),
        slug=args.slug,
        repo_root=resolved_root,
        metadata=metadata,
        reason=args.reason,
        proposed_by=args.proposed_by,
        access_reason=args.access_reason,
        onboarding_issue=args.onboarding_issue,
        run_date=args.run_date,
        confirmation_token=args.confirmation_token,
        confirmed_by_editor=args.confirmed_by_editor,
    )


def _register_capture_source_fixtures(subparsers: SubParsers) -> None:
    fixtures_parser = subparsers.add_parser(
        "capture-source-fixtures",
        help="Generate mutually grounded replay fixtures from retained probe evidence.",
    )
    fixtures_parser.add_argument(
        "--url",
        required=True,
        help="Source Candidate URL the retained probe evidence was captured for.",
    )
    fixtures_parser.add_argument(
        "--slug",
        required=True,
        help="Filesystem-safe slug driving the deterministic output names.",
    )
    fixtures_parser.add_argument(
        "--probe-evidence",
        required=True,
        type=Path,
        help="Retained probe capture JSON (probe-source-candidate --keep-captures output).",
    )
    fixtures_parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for the deterministic fixture pair names (default: data/replay-fixtures).",
    )
    fixtures_parser.add_argument(
        "--page-output",
        type=Path,
        default=None,
        help="Explicit codex-capture page fixture path (overrides --output-dir).",
    )
    fixtures_parser.add_argument(
        "--extraction-output",
        type=Path,
        default=None,
        help="Explicit codex-extraction envelope path (overrides --output-dir).",
    )
    fixtures_parser.add_argument(
        "--source-name",
        default="",
        help="Source name; defaults to the name retained in the probe evidence.",
    )
    fixtures_parser.add_argument(
        "--signal-family",
        default="",
        help="Signal family written as signal_type; defaults to the probe evidence signal family.",
    )
    fixtures_parser.add_argument(
        "--contract-family",
        default="",
        choices=CONTRACT_FAMILIES,
        help="Feasibility/collection contract family; defaults to the retained probe declaration.",
    )
    fixtures_parser.add_argument(
        "--signal-strength",
        required=True,
        help="Configured expected signal strength (not recorded in probe evidence).",
    )
    fixtures_parser.add_argument(
        "--candidate-category",
        required=True,
        help="Configured eligible candidate category written on every mention.",
    )
    fixtures_parser.add_argument(
        "--language",
        default="en",
        help="Source language used to validate retained helper metadata (default: en).",
    )
    fixtures_parser.add_argument(
        "--helper-metadata",
        required=True,
        type=Path,
        dest="helper_metadata_file",
        help="Retained helper metadata sidecar JSON matching every representative title, author, and book URL.",
    )
    fixtures_parser.add_argument(
        "--expected-sort-label",
        default="",
        help="Optional configured sort label rendered in the page fixture.",
    )


def _handle_capture_source_fixtures(args: argparse.Namespace) -> int:
    return capture_source_fixtures(
        args.url,
        args.slug,
        args.probe_evidence,
        output_dir=args.output_dir,
        page_output=args.page_output,
        extraction_output=args.extraction_output,
        source_name=args.source_name,
        signal_family=args.signal_family,
        contract_family=args.contract_family,
        signal_strength=args.signal_strength,
        candidate_category=args.candidate_category,
        language=args.language,
        expected_sort_label=args.expected_sort_label,
        helper_metadata_file=args.helper_metadata_file,
    )


def _register_scaffold_source(subparsers: SubParsers) -> None:
    scaffold_parser = subparsers.add_parser(
        "scaffold-source",
        help="Build one offline probation-source configuration bundle from retained evidence and replay fixtures.",
    )
    scaffold_parser.add_argument("--probe-evidence", required=True, type=Path)
    scaffold_parser.add_argument(
        "--helper-metadata",
        required=True,
        type=Path,
        dest="helper_metadata_file",
        help="Retained helper metadata sidecar JSON used to verify the exact fixture pair.",
    )
    scaffold_parser.add_argument("--page-fixture", required=True, dest="page_fixture", type=Path)
    scaffold_parser.add_argument("--extraction-fixture", required=True, dest="extraction_fixture", type=Path)
    proposal_input = scaffold_parser.add_mutually_exclusive_group(required=True)
    proposal_input.add_argument("--proposals-file", dest="proposals_file", type=Path)
    proposal_input.add_argument(
        "--state-dir",
        dest="proposal_state_dir",
        type=Path,
        help="Temporary state directory whose Source Proposals are read; it is read-only.",
    )
    scaffold_parser.add_argument("--proposal-id", required=True)
    scaffold_parser.add_argument("--slug", required=True, help="Filesystem-safe slug used by capture-source-fixtures.")
    scaffold_parser.add_argument("--portfolio-config", required=True, dest="portfolio_config", type=Path)
    scaffold_parser.add_argument("--pilot-config", required=True, dest="pilot_config", type=Path)
    scaffold_parser.add_argument("--pilot-live-config", required=True, dest="pilot_live_config", type=Path)
    scaffold_parser.add_argument("--live-config", required=True, dest="live_config", type=Path)
    for field in SOURCE_SCAFFOLD_METADATA_FIELDS:
        help_text = f"Explicit {field.replace('_', ' ')}."
        scaffold_parser.add_argument(
            f"--{field.replace('_', '-')}",
            required=field not in SOURCE_SCAFFOLD_OPTIONAL_METADATA_FIELDS,
            default="" if field in SOURCE_SCAFFOLD_OPTIONAL_METADATA_FIELDS else None,
            dest=field,
            help=help_text,
        )
    scaffold_parser.add_argument("--contract-family", choices=CONTRACT_FAMILIES, default=None)
    add_ranking_table_arguments(scaffold_parser)


def _handle_scaffold_source(args: argparse.Namespace) -> int:
    metadata = {field: getattr(args, field) for field in SOURCE_SCAFFOLD_METADATA_FIELDS}
    for field in (*SOURCE_CONTRACT_FIELDS, *RANKING_TABLE_FIELDS):
        value = getattr(args, field, None)
        if value is not None:
            metadata[field] = value
    return scaffold_probation_source(
        probe_evidence=args.probe_evidence,
        page_fixture=args.page_fixture,
        extraction_fixture=args.extraction_fixture,
        helper_metadata_file=args.helper_metadata_file,
        proposal_file=(
            args.proposals_file if args.proposals_file is not None else proposal_file_for(args.proposal_state_dir)
        ),
        proposal_id=args.proposal_id,
        slug=args.slug,
        portfolio_config=args.portfolio_config,
        pilot_config=args.pilot_config,
        pilot_live_config=args.pilot_live_config,
        live_config=args.live_config,
        metadata=metadata,
    )


def _validate_add_source_url_occurrences(arguments: list[str]) -> bool:
    """Validate the offline scalar URL option before argparse delegates."""
    if not arguments or arguments[0] != "add-source":
        return True
    count = 0
    index = 1
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"--url", "--source-url"}:
            count += 1
            if index + 1 >= len(arguments) or arguments[index + 1].startswith("-"):
                print("add-source: --url requires one value", file=sys.stderr)
                return False
            index += 2
            continue
        if argument.startswith("--url=") or argument.startswith("--source-url="):
            count += 1
        index += 1
    if count > 1:
        print("add-source: --url may only be used once", file=sys.stderr)
        return False
    return True


def _parse_cli_bool(value: str) -> bool:
    """Parse the only boolean spelling accepted by public CLI options."""
    normalized = value.casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def add_ranking_table_arguments(parser: argparse.ArgumentParser) -> None:
    """Declare one CLI option per ranking-table field, typed by that field.

    The tuple mixes types: a boolean family flag, a term list, integer bounds,
    and string selectors. A field typed wrongly here is declarable but unusable,
    so each shape gets its own branch.
    """
    for field in RANKING_TABLE_FIELDS:
        spec = RANKING_FIELD_SPECS_BY_NAME[field]
        if field == "adult_nonfiction_exclusions":
            parser.add_argument("--ranking-exclusion-term", action="append", default=None, dest=field)
        elif spec.value_kind == "list":
            option = f"--{field.replace('_', '-')}"
            parser.add_argument(option, action="append", default=None, dest=field)
        elif spec.value_kind == "bool":
            # A bare flag. The default stays None so an omitted flag never
            # injects a false declaration into the scaffolded source.
            parser.add_argument(f"--{field.replace('_', '-')}", action="store_true", default=None, dest=field)
        else:
            parser.add_argument(
                f"--{field.replace('_', '-')}",
                default=None,
                dest=field,
                type=int if spec.value_kind == "int" else str,
                choices=RANKING_RANK_SOURCES if field == "ranking_rank_source" else None,
            )


COMMANDS: dict[str, CliCommand] = {
    "probe-source-candidate": CliCommand(_register_probe_source_candidate, _handle_probe_source_candidate),
    "add-source": CliCommand(_register_add_source, _handle_add_source),
    "capture-source-fixtures": CliCommand(_register_capture_source_fixtures, _handle_capture_source_fixtures),
    "scaffold-source": CliCommand(_register_scaffold_source, _handle_scaffold_source),
}
