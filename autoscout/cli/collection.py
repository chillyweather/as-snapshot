"""Source collection and enrichment passes: check-sources, collect-kirkus, collect-pm-deals, collect-pw-issue, complete-isbns, complete-originals, complete-russian-editions, complete-work-age, portal-first-load, refresh-russian-editions."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Any

from autoscout.cli.common import WEEKLY_RUN_IN_PROGRESS_EXIT_CODE
from autoscout.cli.common import CliCommand
from autoscout.cli.common import SubParsers
from autoscout.cli.common import _validate_iso_date
from autoscout.config import load_validated_config
from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.russian_refresh import apply_refresh_preview
from autoscout.russian_refresh import build_refresh_preview
from autoscout.source_check import check_sources
from autoscout.state_store import Collection
from autoscout.state_store import open_state_store


def _register_check_sources(subparsers: SubParsers) -> None:
    check_sources_parser = subparsers.add_parser(
        "check-sources",
        help="Probe configured sources live (capture-only) and print per-source health verdicts.",
    )
    check_sources_parser.add_argument("--config", required=True, type=Path)
    check_sources_parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Keep captures and source-runs.json here; omitted captures are discarded.",
    )
    check_sources_parser.add_argument(
        "--replay",
        action="store_true",
        help="Probe configured fixture files instead of fetching live.",
    )


def _handle_check_sources(args: argparse.Namespace) -> int:
    load_validated_config(args.config)  # exits non-zero on validation failure
    return check_sources(args.config, args.output_dir, live=not args.replay)


def _register_collect_kirkus(subparsers: SubParsers) -> None:
    collect_kirkus_parser = subparsers.add_parser(
        "collect-kirkus",
        help="Run one Kirkus pre-collection pass and write the result the weekly run reads.",
    )
    collect_kirkus_parser.add_argument("--config", required=True, type=Path)
    collect_kirkus_parser.add_argument(
        "--scheduled",
        action="store_true",
        help=(
            "A start by the server's timer: skip quietly when a pass is running or already ran today. "
            "It sends nothing; the run's morning report states the pass."
        ),
    )


def _handle_collect_kirkus(args: argparse.Namespace) -> int:
    from autoscout.code_gate import is_passed
    from autoscout.git_envelope import resolve_repo_root
    from autoscout.kirkus_precollection import collect_kirkus_command

    repo_root = resolve_repo_root(args.config.resolve()) or Path.cwd()
    return collect_kirkus_command(args.config, scheduled=args.scheduled, code_passed=lambda: is_passed(repo_root))


def _register_collect_pm_deals(subparsers: SubParsers) -> None:
    collect_pm_deals_parser = subparsers.add_parser(
        "collect-pm-deals",
        help="Run one read-only PM deals mailbox pass and write the deal rows it parses.",
    )
    collect_pm_deals_parser.add_argument("--config", required=True, type=Path)


def _handle_collect_pm_deals(args: argparse.Namespace) -> int:
    from autoscout.pm_precollection import collect_pm_deals_command

    return collect_pm_deals_command(args.config)


def _register_collect_pw_issue(subparsers: SubParsers) -> None:
    collect_pw_parser = subparsers.add_parser(
        "collect-pw-issue", help="Collect every uncollected PW issue into bibliographic rows."
    )
    collect_pw_parser.add_argument("--config", required=True, type=Path)
    collect_pw_parser.add_argument(
        "--backfill",
        action="store_true",
        help="Read the whole PW label once, ignoring the age window.",
    )


def _handle_collect_pw_issue(args: argparse.Namespace) -> int:
    from autoscout.pw_precollection import collect_pw_issue_command

    return collect_pw_issue_command(args.config, backfill=args.backfill)


def _register_complete_isbns(subparsers: SubParsers) -> None:
    isbn_parser = subparsers.add_parser(
        "complete-isbns",
        help="Fill missing candidate ISBNs from source links, list pages, book pages and Open Library (#979).",
    )
    isbn_parser.add_argument("--state-dir", required=True, type=Path)
    isbn_parser.add_argument("--output-dir", type=Path, help="Run artifacts directory; defaults to --state-dir.")
    isbn_parser.add_argument(
        "--scope",
        choices=("undecided", "current"),
        default="undecided",
        help="undecided: every book without an editor decision; current: this run's candidates.",
    )
    isbn_parser.add_argument("--run-date", help="ISO date recorded with each ISBN; defaults to today.")
    isbn_parser.add_argument("--offline", action="store_true", help="Use only the source links and list pages.")
    isbn_parser.add_argument("--apply", action="store_true", help="Write the ISBNs; without it nothing is written.")


def _handle_complete_isbns(args: argparse.Namespace) -> int:
    return _complete_isbns_command(args)


def _register_complete_originals(subparsers: SubParsers) -> None:
    originals_parser = subparsers.add_parser(
        "complete-originals",
        help="Record confirmed original editions of translated books (#968).",
    )
    originals_parser.add_argument("--state-dir", required=True, type=Path)
    originals_parser.add_argument("--output-dir", type=Path, help="Run artifacts directory; defaults to --state-dir.")
    originals_parser.add_argument(
        "--scope",
        choices=("undecided", "current"),
        default="undecided",
        help="undecided: every book without an editor decision; current: this run's candidates.",
    )
    originals_parser.add_argument("--run-date", help="ISO date recorded with each lookup; defaults to today.")
    originals_parser.add_argument(
        "--config",
        type=Path,
        help="Weekly config whose todostuslibros_lookups switch allows the booksellers' portal (#1140); "
        "without it the portal is not asked.",
    )
    originals_parser.add_argument(
        "--apply",
        action="store_true",
        help="Write what was learned; without it nothing is written and the portal answers only from its ledger.",
    )


def _register_portal_first_load(subparsers: SubParsers) -> None:
    parser = subparsers.add_parser(
        "portal-first-load",
        help="Ask the booksellers' portal once about books already collected, within the first-load ceiling (#1140).",
    )
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument(
        "--config", required=True, type=Path, help="Weekly config with the todostuslibros_lookups switch."
    )
    parser.add_argument("--limit", type=int, help="Ask at most this many ISBNs in this call.")
    parser.add_argument("--apply", action="store_true", help="Ask the portal; without it nothing is asked or written.")


def _handle_portal_first_load(args: argparse.Namespace) -> int:
    return _portal_first_load_command(args)


def _handle_complete_originals(args: argparse.Namespace) -> int:
    return _complete_originals_command(args)


def _register_complete_russian_editions(subparsers: SubParsers) -> None:
    from autoscout.russian_edition_completion import MAX_BOOKS
    from autoscout.russian_edition_completion import MAX_LIVELIB_SEARCHES
    from autoscout.russian_edition_completion import SOURCES

    parser = subparsers.add_parser(
        "complete-russian-editions",
        help="Record whether a Russian edition of each undecided book exists (#1181).",
    )
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path, help="Run artifacts directory; defaults to --state-dir.")
    parser.add_argument(
        "--scope",
        choices=("undecided", "current"),
        default="undecided",
        help="undecided: every book without an editor decision; current: this run's candidates.",
    )
    parser.add_argument("--run-date", help="ISO date recorded with each lookup; defaults to today.")
    parser.add_argument(
        "--max-books", type=int, default=MAX_BOOKS, help=f"Look up at most this many books (default {MAX_BOOKS})."
    )
    parser.add_argument(
        "--max-livelib-searches",
        type=int,
        default=MAX_LIVELIB_SEARCHES,
        help=f"Make at most this many LiveLib searches; it refuses a client after about 30 (default {MAX_LIVELIB_SEARCHES}).",
    )
    parser.add_argument(
        "--skip-source",
        action="append",
        choices=SOURCES,
        default=[],
        help="Do not ask this source; repeat for several.",
    )
    parser.add_argument("--apply", action="store_true", help="Write what was learned; without it nothing is written.")


def _handle_complete_russian_editions(args: argparse.Namespace) -> int:
    return _complete_russian_editions_command(args)


def _register_complete_work_age(subparsers: SubParsers) -> None:
    work_age_parser = subparsers.add_parser(
        "complete-work-age",
        help="Record each book's first publication year and edition count from Open Library (#1138).",
    )
    work_age_parser.add_argument("--state-dir", required=True, type=Path)
    work_age_parser.add_argument("--output-dir", type=Path, help="Run artifacts directory; defaults to --state-dir.")
    work_age_parser.add_argument(
        "--scope",
        choices=("all", "undecided", "current"),
        default="all",
        help=(
            "all: every book in Candidate History, decided or not; undecided: every book without an "
            "editor decision; current: this run's candidates."
        ),
    )
    work_age_parser.add_argument("--run-date", help="ISO date recorded with each lookup; defaults to today.")
    work_age_parser.add_argument(
        "--apply", action="store_true", help="Write what was learned; without it nothing is written."
    )


def _handle_complete_work_age(args: argparse.Namespace) -> int:
    return _complete_work_age_command(args)


def _register_refresh_russian_editions(subparsers: SubParsers) -> None:
    refresh_parser = subparsers.add_parser(
        "refresh-russian-editions",
        help="Create or apply a bounded one-time RSL edition refresh preview.",
    )
    refresh_parser.add_argument("--state-dir", required=True, type=Path)
    refresh_parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Preview JSON path; defaults to the state directory with the run date.",
    )
    refresh_parser.add_argument(
        "--preview-file",
        type=Path,
        default=None,
        help="Apply an existing preview without making network requests.",
    )
    refresh_parser.add_argument("--run-date", default=None, type=_validate_iso_date)
    refresh_parser.add_argument("--limit", type=int, default=None)
    refresh_parser.add_argument("--delay-seconds", type=float, default=0.2)
    refresh_parser.add_argument("--timeout-seconds", type=float, default=12.0)
    refresh_parser.add_argument(
        "--only-with-russian-search-notes",
        action="store_true",
        default=False,
        help="Check only candidates carrying a verified Russian search translation.",
    )
    refresh_parser.add_argument("--apply", action="store_true", default=False)


def _handle_refresh_russian_editions(args: argparse.Namespace) -> int:
    run_date = args.run_date or date.today().isoformat()
    candidates = open_state_store(args.state_dir).read(Collection.LATEST_SCOUTING_CANDIDATES)
    if not isinstance(candidates, list):
        print(
            f"refresh-russian-editions: expected a list of latest Scouting Candidates in {args.state_dir}",
            file=sys.stderr,
        )
        return 1
    if args.limit is not None and args.limit < 1:
        print("refresh-russian-editions: --limit must be positive", file=sys.stderr)
        return 1
    if args.only_with_russian_search_notes:
        candidates = [
            candidate
            for candidate in candidates
            if isinstance(candidate.get("russian_search_notes"), dict)
            and str(candidate["russian_search_notes"].get("search_translation", "")).strip()
        ]
    if args.delay_seconds < 0 or args.timeout_seconds <= 0:
        print("refresh-russian-editions: delay must be non-negative and timeout positive", file=sys.stderr)
        return 1
    if args.preview_file is not None:
        preview = read_json(args.preview_file, default={})
        if not isinstance(preview, dict) or not isinstance(preview.get("rows"), list):
            print("refresh-russian-editions: invalid preview file", file=sys.stderr)
            return 1
    else:
        preview = build_refresh_preview(
            candidates,
            run_date=run_date,
            delay_seconds=args.delay_seconds,
            limit=args.limit,
            timeout_seconds=args.timeout_seconds,
        )
    output_path = (
        args.output or args.preview_file or args.state_dir / f"russian-edition-refresh-preview-{run_date}.json"
    )
    if args.preview_file is None or output_path != args.preview_file:
        write_json(output_path, preview)
    if args.apply:
        applied = apply_refresh_preview(args.state_dir, preview)
        print(f"russian_edition_refresh_applied={applied['matched_rows']}")
        print(f"russian_edition_refresh_changed_records={applied['changed_records']}")
    print(f"russian_edition_refresh_preview={output_path}")
    for outcome, count in sorted(preview.get("counts", {}).items()):
        print(f"russian_edition_refresh_{outcome}={count}")
    return 0


def _complete_isbns_command(args: argparse.Namespace) -> int:
    """Run ISBN completion for Candidate History and print counts, never titles."""
    from datetime import date as _date

    from autoscout.isbn_completion import BoundedPageGetter
    from autoscout.isbn_completion import run_isbn_completion

    run_date = args.run_date or _date.today().isoformat()
    try:
        _date.fromisoformat(run_date)
    except ValueError:
        print("reason_code=invalid_run_date")
        return 2
    from autoscout.weekly_report import WeeklyReportStore
    from autoscout.weekly_report import WeeklyReportTransactionInProgress

    output_dir = args.output_dir or args.state_dir
    try:
        result = run_isbn_completion(
            output_dir,
            args.state_dir,
            effective_date=run_date,
            scope=args.scope,
            getter=None if args.offline else BoundedPageGetter(),
            dry_run=not args.apply,
            publish_lock=lambda: WeeklyReportStore(output_dir).transaction("isbn-completion"),
        )
    except WeeklyReportTransactionInProgress:
        print("reason_code=report_transaction_in_progress")
        print("Другая операция сейчас записывает обзор. ISBN не сохранены — повторите позже.")
        return WEEKLY_RUN_IN_PROGRESS_EXIT_CODE
    if result.get("status") != "ok":
        print(f"reason_code={result.get('reason', 'isbn_completion_failed')}")
        return 2
    print(f"status=ok scope={result['scope']} applied={str(bool(args.apply)).lower()}")
    print(f"considered={result['considered']} completed={result['completed_total']} not_found={result['not_found']}")
    for method, count in result["completed"].items():
        print(f"completed_{method.replace('-', '_')}={count}")
    print(
        f"skipped_recently_tried={result['skipped_recently_tried']} budget_exhausted={result['budget_exhausted']} "
        f"lookup_unavailable={result['lookup_unavailable']}"
    )
    return 0 if args.apply else 3


def _library_unavailable(result: dict[str, Any]) -> str:
    """Per-library unavailable counts as ``dnb:2,bnf:0``, or ``0`` when every library answered."""
    counts = result.get("library_unavailable")
    if not isinstance(counts, dict) or not counts:
        return "0"
    return ",".join(f"{key}:{int(value)}" for key, value in sorted(counts.items()))


def _portal_counts(portal: Any) -> str:
    """The booksellers' portal counts as one line, or ``portal=off`` when it was not used."""
    if not isinstance(portal, dict) or not portal:
        return "portal=off"
    return "portal " + " ".join(f"{key}={str(value).lower()}" for key, value in portal.items())


def _portal_first_load_command(args: argparse.Namespace) -> int:
    """Preview or make the portal's first load; counts only, never titles or page text."""
    from autoscout.bookseller_portal import FIRST_LOAD_CEILING
    from autoscout.bookseller_portal import LivePortalFetch
    from autoscout.bookseller_portal import config_enabled
    from autoscout.portal_first_load import first_load_lookup
    from autoscout.portal_first_load import run_portal_first_load

    if not config_enabled(args.config):
        print("reason_code=portal_disabled")
        return 2
    if args.limit is not None and args.limit < 0:
        print("reason_code=invalid_limit")
        return 2
    lookup = first_load_lookup(args.state_dir, LivePortalFetch() if args.apply else None)
    result = run_portal_first_load(
        args.state_dir,
        lookup=lookup,
        apply=args.apply,
        limit=args.limit,
        progress=lambda done, planned: print(f"progress requests={done} planned={planned}", flush=True),
    )
    if result.get("status") != "ok":
        print(f"reason_code={result.get('reason', 'portal_first_load_failed')}")
        return 2
    print(f"status=ok applied={str(bool(args.apply)).lower()} first_load_ceiling={FIRST_LOAD_CEILING}")
    print(
        f"candidates={result['candidates']} already_kept={result['already_kept']} "
        f"planned={result['planned']} first_load_remaining={result['first_load_remaining']}"
    )
    if args.apply:
        print(_portal_counts(result.get("portal")))
    return 0 if args.apply else 3


def _complete_originals_command(args: argparse.Namespace) -> int:
    """Learn original editions of translated books and print counts, never titles."""
    from datetime import date as _date

    from autoscout.bookseller_portal import LivePortalFetch
    from autoscout.original_edition_completion import BoundedPageGetter
    from autoscout.original_edition_completion import CodexOriginalTitleProposer
    from autoscout.original_edition_completion import run_original_edition_completion
    from autoscout.original_edition_completion import weekly_portal_lookup

    run_date = args.run_date or _date.today().isoformat()
    try:
        _date.fromisoformat(run_date)
    except ValueError:
        print("reason_code=invalid_run_date")
        return 2
    from autoscout.weekly_report import WeeklyReportStore
    from autoscout.weekly_report import WeeklyReportTransactionInProgress

    output_dir = args.output_dir or args.state_dir
    # A preview asks the portal nothing: every portal request is written to
    # its ledger, and a preview writes nothing (#1140).
    portal = weekly_portal_lookup(
        args.config, args.state_dir, fetch_factory=LivePortalFetch if args.apply else (lambda: None)
    )
    try:
        result = run_original_edition_completion(
            output_dir,
            args.state_dir,
            effective_date=run_date,
            scope=args.scope,
            getter=BoundedPageGetter(),
            proposer=CodexOriginalTitleProposer(),
            dry_run=not args.apply,
            publish_lock=lambda: WeeklyReportStore(output_dir).transaction("original-edition-completion"),
            portal=portal,
        )
    except WeeklyReportTransactionInProgress:
        print("reason_code=report_transaction_in_progress")
        print("Другая операция сейчас записывает обзор. Оригинальные издания не сохранены — повторите позже.")
        return WEEKLY_RUN_IN_PROGRESS_EXIT_CODE
    if result.get("status") != "ok":
        print(f"reason_code={result.get('reason', 'original_edition_completion_failed')}")
        return 2
    print(f"status=ok scope={result['scope']} applied={str(bool(args.apply)).lower()}")
    print(
        f"considered={result['considered']} answered_by_library={result['answered_by_library']} "
        f"answered_by_model={result['answered_by_model']} library_stated={result['library_stated']} "
        f"answered_by_portal={result['answered_by_portal']} portal_stated={result['portal_stated']} "
        f"page_count_applied={result['page_count_applied']} "
        f"library_unavailable={_library_unavailable(result)} "
        f"no_credit={result['no_credit']} credited={result['credited']} "
        f"no_proposal={result['no_proposal']} proposal_unavailable={result['proposal_unavailable']} "
        f"confirmed={result['confirmed']} not_found={result['not_found']} "
        f"lookup_unavailable={result['lookup_unavailable']} "
        f"english_title_applied={result['english_title_applied']} "
        f"english_title_not_applied={result['english_title_not_applied']} "
        f"skipped_recent={result['skipped_recent']} budget_exhausted={result['budget_exhausted']}"
    )
    print(_portal_counts(result.get("portal")))
    return 0 if args.apply else 3


def _complete_russian_editions_command(args: argparse.Namespace) -> int:
    """Learn whether Russian editions exist and print counts, never titles."""
    from datetime import date as _date

    from autoscout.cli import common
    from autoscout.russian_edition_completion import CATALOGUE_SOURCES
    from autoscout.russian_edition_completion import SOURCES
    from autoscout.russian_edition_completion import WEEKLY_RUN_IN_PROGRESS
    from autoscout.russian_edition_completion import CodexSameWorkMatcher
    from autoscout.russian_edition_completion import LiveSourceFetcher
    from autoscout.russian_edition_completion import run_russian_edition_completion
    from autoscout.weekly_report import WeeklyReportStore
    from autoscout.weekly_report import WeeklyReportTransactionInProgress

    run_date = args.run_date or _date.today().isoformat()
    try:
        _date.fromisoformat(run_date)
    except ValueError:
        print("reason_code=invalid_run_date")
        return 2
    if args.max_books < 0:
        print("reason_code=invalid_max_books")
        return 2
    if args.max_livelib_searches < 0:
        print("reason_code=invalid_max_livelib_searches")
        return 2
    enabled_sources = [source for source in SOURCES if source not in set(args.skip_source)]
    if not set(enabled_sources) & set(CATALOGUE_SOURCES):
        # Without the State Library and LitRes nothing can be confirmed.
        print("reason_code=no_catalogue_source")
        return 2
    output_dir = args.output_dir or args.state_dir
    try:
        result = run_russian_edition_completion(
            output_dir,
            args.state_dir,
            effective_date=run_date,
            scope=args.scope,
            fetcher=LiveSourceFetcher(),
            matcher=CodexSameWorkMatcher(),
            dry_run=not args.apply,
            publish_lock=lambda: WeeklyReportStore(output_dir).transaction("russian-edition-completion"),
            # The lookups can take half an hour: a weekly run that started
            # meanwhile is checked again immediately before anything is written.
            weekly_run_check=lambda: common.weekly_run_in_progress(args.state_dir),
            max_books=args.max_books,
            max_livelib_searches=args.max_livelib_searches,
            enabled_sources=enabled_sources,
        )
    except WeeklyReportTransactionInProgress:
        print("reason_code=report_transaction_in_progress")
        print("Другая операция сейчас записывает обзор. Русские издания не сохранены — повторите позже.")
        return WEEKLY_RUN_IN_PROGRESS_EXIT_CODE
    if result.get("reason") == WEEKLY_RUN_IN_PROGRESS:
        print(f"reason_code={WEEKLY_RUN_IN_PROGRESS}")
        print("Идёт еженедельный поиск. Русские издания не сохранены — повторите после его завершения.")
        return WEEKLY_RUN_IN_PROGRESS_EXIT_CODE
    if result.get("status") != "ok":
        print(f"reason_code={result.get('reason', 'russian_edition_completion_failed')}")
        return 2
    print(f"status=ok scope={result['scope']} applied={str(bool(args.apply)).lower()}")
    print(
        f"considered={result['considered']} skipped_decided={result['skipped_decided']} "
        f"skipped_settled={result['skipped_settled']} over_ceiling={result['over_ceiling']} "
        f"confirmed={result['confirmed']} unconfirmed={result['unconfirmed']} no_find={result['no_find']} "
        f"not_completed={result['not_completed']} match_unavailable={result['match_unavailable']} "
        f"budget_exhausted={result['budget_exhausted']} author_name_learned={result['author_name_learned']}"
    )
    for source, counts in result["sources"].items():
        enabled = source in result["enabled_sources"]
        print(
            f"source={source} enabled={str(enabled).lower()} answered={counts['answered']} "
            f"blocked={counts['blocked']} failed={counts['failed']} not_asked={counts['not_asked']}"
        )
    if result.get("report_file") == "not_written":
        # The editions are saved; only the diagnostic report file is missing.
        print("warning=report_file_not_written")
        print("Русские издания сохранены, но файл отчёта о поиске не записан.")
    return 0 if args.apply else 3


def _complete_work_age_command(args: argparse.Namespace) -> int:
    """Record the age of each book's work and print counts, never titles."""
    from datetime import date as _date

    from autoscout.work_age import BACKFILL_BUDGET_SECONDS
    from autoscout.work_age import open_library_getter
    from autoscout.work_age import run_work_age_completion

    run_date = args.run_date or _date.today().isoformat()
    try:
        _date.fromisoformat(run_date)
    except ValueError:
        print("reason_code=invalid_run_date")
        return 2
    from autoscout.weekly_report import WeeklyReportStore
    from autoscout.weekly_report import WeeklyReportTransactionInProgress

    output_dir = args.output_dir or args.state_dir
    try:
        result = run_work_age_completion(
            output_dir,
            args.state_dir,
            effective_date=run_date,
            scope=args.scope,
            getter=open_library_getter(),
            dry_run=not args.apply,
            budget_seconds=BACKFILL_BUDGET_SECONDS,
            publish_lock=lambda: WeeklyReportStore(output_dir).transaction("work-age-completion"),
        )
    except WeeklyReportTransactionInProgress:
        print("reason_code=report_transaction_in_progress")
        print("Другая операция сейчас записывает обзор. Возраст книг не сохранён — повторите позже.")
        return WEEKLY_RUN_IN_PROGRESS_EXIT_CODE
    if result.get("status") != "ok":
        print(f"reason_code={result.get('reason', 'work_age_completion_failed')}")
        return 2
    print(f"status=ok scope={result['scope']} applied={str(bool(args.apply)).lower()}")
    print(
        f"considered={result['considered']} found={result['found']} not_found={result['not_found']} "
        f"not_askable={result['not_askable']} skipped_settled={result['skipped_settled']} "
        f"lookup_unavailable={result['lookup_unavailable']} "
        f"skipped_catalogue_unavailable={result['skipped_catalogue_unavailable']} "
        f"budget_exhausted={result['budget_exhausted']}"
    )
    for basis, count in result["found_by_basis"].items():
        print(f"found_by_{basis}={count}")
    return 0 if args.apply else 3


COMMANDS: dict[str, CliCommand] = {
    "check-sources": CliCommand(_register_check_sources, _handle_check_sources),
    "collect-kirkus": CliCommand(_register_collect_kirkus, _handle_collect_kirkus),
    "collect-pm-deals": CliCommand(_register_collect_pm_deals, _handle_collect_pm_deals),
    "collect-pw-issue": CliCommand(_register_collect_pw_issue, _handle_collect_pw_issue),
    "complete-isbns": CliCommand(_register_complete_isbns, _handle_complete_isbns),
    "complete-originals": CliCommand(_register_complete_originals, _handle_complete_originals),
    "complete-russian-editions": CliCommand(_register_complete_russian_editions, _handle_complete_russian_editions),
    "complete-work-age": CliCommand(_register_complete_work_age, _handle_complete_work_age),
    "portal-first-load": CliCommand(_register_portal_first_load, _handle_portal_first_load),
    "refresh-russian-editions": CliCommand(_register_refresh_russian_editions, _handle_refresh_russian_editions),
}
