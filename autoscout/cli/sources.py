"""Source portfolio commands: source-proposal, source-lifecycle, audit-annual-sources, discover-source-gaps, monthly-source-discovery, source-novelty, source-yield-report."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from autoscout.annual_source_audit import audit_annual_sources_command
from autoscout.cli.common import CliCommand
from autoscout.cli.common import SubParsers
from autoscout.config import resolve_weekly_portfolio_or_raise
from autoscout.monthly_source_discovery import MonthlySourceDiscoveryRequest
from autoscout.monthly_source_discovery import run_monthly_source_discovery
from autoscout.monthly_source_discovery_artifacts import DEFAULT_MONTHLY_OUTPUT_DIR
from autoscout.search_optimization import DEFAULT_SEARCH_OPTIMIZATION_REVIEW_PATH
from autoscout.source_gap_discovery import SourceGapProbeError
from autoscout.source_gap_discovery import discover_source_gaps
from autoscout.source_gap_discovery import source_gap_error_report
from autoscout.source_lifecycle import keep_source
from autoscout.source_lifecycle import promote_source
from autoscout.source_lifecycle import retire_source
from autoscout.source_probe import DEFAULT_LEDGER_PATH
from autoscout.source_probe import SourceCandidateLedgerError
from autoscout.source_probe import read_source_candidate_ledger
from autoscout.source_proposal_commands import accept_source_proposal
from autoscout.source_proposal_commands import add_source_proposal
from autoscout.source_proposal_commands import reconsider_source_proposal
from autoscout.source_proposal_commands import reject_source_proposal


def _register_source_proposal(subparsers: SubParsers) -> None:
    proposal_parser = subparsers.add_parser(
        "source-proposal", help="Add, accept, reject, or reconsider a Source Proposal."
    )
    proposal_subparsers = proposal_parser.add_subparsers(dest="proposal_command", required=True)
    add_parser = proposal_subparsers.add_parser("add", help="Record a manual Source Proposal.")
    add_parser.add_argument("--source-url", required=True)
    add_parser.add_argument("--name", default="")
    add_parser.add_argument("--proposed-by", default="agent")
    add_parser.add_argument("--reason", required=True)
    add_parser.add_argument("--signal-family", default="")
    add_parser.add_argument("--access-status", default="")
    add_parser.add_argument("--caveats", default="")
    add_parser.add_argument("--state-dir", required=True, type=Path)
    accept_parser = proposal_subparsers.add_parser("accept", help="Accept a Source Proposal.")
    accept_parser.add_argument("--id", required=True)
    accept_parser.add_argument("--state-dir", required=True, type=Path)
    accept_parser.add_argument("--config", required=True, type=Path)
    accept_parser.add_argument("--name")
    accept_parser.add_argument("--access-status", default="open")
    accept_parser.add_argument("--access-reason", default="")
    accept_parser.add_argument(
        "--language",
        required=True,
        help="Declared source language (e.g. en, de, fr). Every configured source must declare one.",
    )
    reject_parser = proposal_subparsers.add_parser("reject", help="Reject a Source Proposal.")
    reject_parser.add_argument("--id", required=True)
    reject_parser.add_argument("--state-dir", required=True, type=Path)
    reject_parser.add_argument("--reason", required=True)
    reconsider_parser = proposal_subparsers.add_parser(
        "reconsider", help="Reopen a rejected Source Proposal on fresh accepted probe evidence."
    )
    reconsider_parser.add_argument("--id", required=True)
    reconsider_parser.add_argument("--state-dir", required=True, type=Path)
    reconsider_parser.add_argument(
        "--ledger", required=True, type=Path, help="Source Candidate Ledger holding the fresh accepted verdict."
    )
    reconsider_parser.add_argument("--reason", required=True)
    reconsider_parser.add_argument("--reconsidered-by", required=True)
    reconsider_parser.add_argument("--confirmation-token", default="")
    reconsider_parser.add_argument("--confirmed-by-editor", action="store_true")


def _handle_source_proposal(args: argparse.Namespace) -> int:
    if args.proposal_command == "add":
        return add_source_proposal(
            args.state_dir,
            args.source_url,
            args.name,
            args.proposed_by,
            args.reason,
            args.signal_family,
            args.access_status,
            args.caveats,
        )
    if args.proposal_command == "accept":
        return accept_source_proposal(
            args.state_dir,
            args.config,
            args.id,
            args.name,
            args.access_status,
            args.access_reason,
            args.language,
        )
    if args.proposal_command == "reject":
        return reject_source_proposal(args.state_dir, args.id, args.reason)
    if args.proposal_command == "reconsider":
        return reconsider_source_proposal(
            args.state_dir,
            args.id,
            args.ledger,
            args.reason,
            args.reconsidered_by,
            args.confirmation_token,
            args.confirmed_by_editor,
        )
    return 1


def _register_source_lifecycle(subparsers: SubParsers) -> None:
    lifecycle_parser = subparsers.add_parser(
        "source-lifecycle", help="Keep, promote, or retire configured sources after source review."
    )
    lifecycle_subparsers = lifecycle_parser.add_subparsers(dest="lifecycle_command", required=True)
    for lifecycle_command in ("retire", "promote", "keep"):
        lifecycle_action = lifecycle_subparsers.add_parser(lifecycle_command)
        lifecycle_action.add_argument("--config", required=True, type=Path)
        lifecycle_action.add_argument("--source-url", default="")
        lifecycle_action.add_argument("--name", default="")
        lifecycle_action.add_argument("--reason", required=True)
        lifecycle_action.add_argument("--actor", default="agent")


def _handle_source_lifecycle(args: argparse.Namespace) -> int:
    if not args.source_url and not args.name:
        print("source_identifier_required")
        return 2
    if args.lifecycle_command == "retire":
        return retire_source(args.config, args.source_url, args.name, args.reason, args.actor)
    if args.lifecycle_command == "promote":
        return promote_source(args.config, args.source_url, args.name, args.reason, args.actor)
    if args.lifecycle_command == "keep":
        return keep_source(args.config, args.source_url, args.name, args.reason, args.actor)
    return 1


def _register_audit_annual_sources(subparsers: SubParsers) -> None:
    annual_audit_parser = subparsers.add_parser(
        "audit-annual-sources",
        help=(
            "Report, for each active annual source, whether a newer edition or award stage has been "
            "published: from the declared release windows (default, no network), a bounded check of the "
            "declared official pages (--live), or replayed captures (--evidence-dir). Never changes configuration."
        ),
    )
    annual_audit_parser.add_argument("--config", required=True, type=Path)
    annual_audit_parser.add_argument(
        "--today",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help="Date to audit against. Defaults to today; set explicitly for a deterministic check.",
    )
    annual_audit_parser.add_argument(
        "--live",
        action="store_true",
        help="Fetch the declared official pages: at most 2 plain GETs per source, no retry, read-only.",
    )
    annual_audit_parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="Replay page captures from DIR instead of fetching (no network).",
    )
    annual_audit_parser.add_argument(
        "--keep-evidence",
        type=Path,
        default=None,
        metavar="DIR",
        help="With --live, also write the fetched pages as captures to DIR (never a config, weekly or state dir).",
    )
    annual_audit_parser.add_argument(
        "--source", default=None, metavar="NAME", help="Audit only the active annual source with this exact name."
    )
    annual_audit_parser.add_argument(
        "--propose",
        action="store_true",
        help="Print a zero-mutation rollover or stage-change proposal for each source whose evidence calls for one.",
    )


def _handle_audit_annual_sources(args: argparse.Namespace) -> int:
    return audit_annual_sources_command(
        args.config,
        today=args.today,
        live=args.live,
        evidence_dir=args.evidence_dir,
        keep_evidence=args.keep_evidence,
        source_name=args.source,
        propose=args.propose,
    )


def _register_discover_source_gaps(subparsers: SubParsers) -> None:
    gap_parser = subparsers.add_parser(
        "discover-source-gaps",
        help="Generate a deterministic, read-only report of thin source signal families.",
    )
    gap_parser.add_argument(
        "--portfolio",
        type=Path,
        default=Path("configs/current-weekly-portfolio.json"),
        help="Configured weekly portfolio JSON (default: configs/current-weekly-portfolio.json).",
    )
    gap_parser.add_argument(
        "--review",
        type=Path,
        default=None,
        help="Optional Search Optimization Review JSON artifact override; the default run artifact is used when present.",
    )
    gap_parser.add_argument(
        "--ledger",
        type=Path,
        default=DEFAULT_LEDGER_PATH,
        help="Source Candidate Ledger JSON; a missing file means an empty ledger.",
    )
    gap_parser.add_argument(
        "--signal-family",
        default=None,
        help="Limit the report and candidates to one canonical signal family.",
    )
    gap_parser.add_argument(
        "--probe",
        action="store_true",
        help="Explicitly probe the remaining candidates through ./scripts/probe-source.",
    )


def _handle_discover_source_gaps(args: argparse.Namespace) -> int:
    return _discover_source_gaps_command(
        args.portfolio,
        args.review,
        args.ledger,
        args.signal_family,
        args.probe,
    )


def _register_monthly_source_discovery(subparsers: SubParsers) -> None:
    monthly_parser = subparsers.add_parser(
        "monthly-source-discovery",
        help="Publish one deterministic offline monthly source discovery report.",
    )
    monthly_parser.add_argument(
        "--operation-month",
        default=None,
        help="Explicit operation month in YYYY-MM format; this option is required.",
    )
    monthly_parser.add_argument(
        "--portfolio",
        type=Path,
        default=Path("configs/current-weekly-portfolio.json"),
        help="Offline configured weekly portfolio JSON input override, including supported overlays.",
    )
    monthly_parser.add_argument(
        "--review",
        type=Path,
        default=None,
        help="Optional offline Search Optimization Review JSON input override.",
    )
    monthly_parser.add_argument(
        "--ledger",
        type=Path,
        default=DEFAULT_LEDGER_PATH,
        help="Offline canonical Source Candidate Ledger JSON input override.",
    )
    monthly_parser.add_argument(
        "--source-proposals",
        type=Path,
        default=None,
        help="Offline override: another state directory's Source Proposals file (default: the weekly state directory's).",
    )
    monthly_parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_MONTHLY_OUTPUT_DIR,
        help="Offline derived output directory override (default: data/monthly-source-discovery; history stays inside it).",
    )


def _handle_monthly_source_discovery(args: argparse.Namespace) -> int:
    report, code = run_monthly_source_discovery(
        MonthlySourceDiscoveryRequest(
            operation_month=args.operation_month,
            portfolio_path=args.portfolio,
            review_path=args.review,
            ledger_path=args.ledger,
            proposals_path=args.source_proposals,
            output_dir=args.output_dir,
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return code


def _register_source_novelty(subparsers: SubParsers) -> None:
    novelty_parser = subparsers.add_parser(
        "source-novelty",
        help="Read-only: match a candidate source's sampled entries against Candidate History.",
    )
    novelty_parser.add_argument("--sample", required=True, type=Path)
    novelty_parser.add_argument("--state-dir", required=True, type=Path)
    novelty_parser.add_argument("--run-date", help="ISO date whose year decides staleness (default: today).")


def _handle_source_novelty(args: argparse.Namespace) -> int:
    from autoscout.source_novelty import source_novelty_command

    return source_novelty_command(args.sample, args.state_dir, args.run_date)


def _register_source_yield_report(subparsers: SubParsers) -> None:
    yield_parser = subparsers.add_parser(
        "source-yield-report",
        help="Read-only: per source, how many of the books it reported the editor found useful (#1055).",
    )
    yield_parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path("runs/current-weekly-pilot-russian"),
        help="State directory whose Candidate History is read (default: runs/current-weekly-pilot-russian).",
    )
    yield_parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/current-weekly-portfolio.json"),
        help="Weekly portfolio whose sources, retired ones included, the report lists "
        "(default: configs/current-weekly-portfolio.json).",
    )


def _handle_source_yield_report(args: argparse.Namespace) -> int:
    from autoscout.source_yield_report import source_yield_report_command

    return source_yield_report_command(args.state_dir, args.config)


def _discover_source_gaps_command(
    portfolio_path: Path,
    review_path: Path | None,
    ledger_path: Path,
    signal_family: str | None,
    probe: bool,
) -> int:
    """Load typed discovery inputs and print one structured report."""
    default_review_path = DEFAULT_SEARCH_OPTIMIZATION_REVIEW_PATH
    try:
        portfolio = json.loads(portfolio_path.read_text(encoding="utf-8"))
        if not isinstance(portfolio, dict):
            raise ValueError("portfolio must be a JSON object")
        portfolio = resolve_weekly_portfolio_or_raise(portfolio, portfolio_path)
        selected_review_path = review_path if review_path is not None else default_review_path
        review = None
        if selected_review_path.exists():
            review = json.loads(selected_review_path.read_text(encoding="utf-8"))
        elif review_path is not None:
            raise OSError(f"review artifact does not exist: {selected_review_path}")
        ledger_identities = set(read_source_candidate_ledger(ledger_path))
        report = discover_source_gaps(
            portfolio,
            review,
            ledger_identities,
            signal_family,
            ledger_path=ledger_path,
            probe=probe,
        )
    except SourceGapProbeError as exc:
        error_report = source_gap_error_report(
            str(exc),
            signal_family,
            probe={"status": "error", "command": list(exc.command), "exit_code": 2},
            error_kind="probe_error",
        )
        print(json.dumps(error_report, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    except (OSError, UnicodeError, json.JSONDecodeError, SourceCandidateLedgerError, TypeError, ValueError) as exc:
        error_report = source_gap_error_report(str(exc), signal_family)
        print(json.dumps(error_report, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if not probe:
        return 0
    return int(report["probe"].get("exit_code", 0))


COMMANDS: dict[str, CliCommand] = {
    "source-proposal": CliCommand(_register_source_proposal, _handle_source_proposal),
    "source-lifecycle": CliCommand(_register_source_lifecycle, _handle_source_lifecycle),
    "audit-annual-sources": CliCommand(_register_audit_annual_sources, _handle_audit_annual_sources),
    "discover-source-gaps": CliCommand(_register_discover_source_gaps, _handle_discover_source_gaps),
    "monthly-source-discovery": CliCommand(_register_monthly_source_discovery, _handle_monthly_source_discovery),
    "source-novelty": CliCommand(_register_source_novelty, _handle_source_novelty),
    "source-yield-report": CliCommand(_register_source_yield_report, _handle_source_yield_report),
}
