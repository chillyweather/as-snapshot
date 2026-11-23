"""The ``audit-annual-sources`` command (Issue #680).

Three ways to judge the active annual sources, one output shape:

- offline (default): the declared release windows alone, as the weekly run
  does. No page is read.
- ``--live``: a bounded check of the pages each source declares, at most two
  plain GETs per source, sequential, with no retry. ``--keep-evidence DIR``
  also writes what was fetched as JSON captures.
- ``--evidence-dir DIR``: the same judgement replayed from captures, with no
  network.

``--propose`` adds one zero-mutation proposal per source whose evidence shows
a newer edition or a newer stage. Nothing here changes configuration, weekly
state or any repository file; the only file this command can write is a
capture under ``--keep-evidence``, which must lie outside every config,
weekly and state directory.

Exit codes: ``0`` when no source needs the operator, ``1`` when at least one
does, ``2`` for a config or usage error.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from autoscout.annual_rollover_evidence import AnnualEditionEvidence
from autoscout.annual_rollover_evidence import EvidenceCaptureError
from autoscout.annual_rollover_evidence import PageFetcher
from autoscout.annual_rollover_evidence import gather_evidence
from autoscout.annual_rollover_evidence import live_page_fetcher
from autoscout.annual_rollover_evidence import read_evidence_dir
from autoscout.annual_rollover_evidence import validated_capture_dir
from autoscout.annual_rollover_evidence import write_evidence_capture
from autoscout.annual_rollover_proposal import RepositoryWiring
from autoscout.annual_rollover_proposal import build_proposal
from autoscout.annual_rollover_proposal import describe_evidence
from autoscout.annual_rollover_proposal import load_repository_wiring
from autoscout.annual_rollover_proposal import render_proposal
from autoscout.annual_source_audit import AnnualSourceVerdict
from autoscout.annual_source_audit import audit_annual_source
from autoscout.annual_source_audit import audited_sources
from autoscout.annual_source_audit import rollover_checks_required
from autoscout.repository_root import repository_root as _repository_root

REPOSITORY_ROOT = _repository_root()

FetcherFactory = Callable[[Mapping[str, Any]], PageFetcher]

# Whether a verdict calls for a proposal does not depend on the wiring, so it
# is decided against empty wiring; the real wiring is read only once some
# proposal is due.
_NO_WIRING = RepositoryWiring()


class _UsageError(ValueError):
    pass


def _command_sources(config_path: Any) -> list[Any] | str:
    """Load and validate one config for the command; a string is the error."""
    from autoscout.config import read_json
    from autoscout.config import resolve_weekly_portfolio_or_raise
    from autoscout.config import validate_config

    # Loaded through the raising variants throughout. Both load_validated_config
    # and resolve_weekly_portfolio preserve the shared CLI exit contract by
    # calling sys.exit(1) on a bad config, and SystemExit does not inherit from
    # Exception, so an `except ValueError` around either one never fires. This
    # command distinguishes "cannot read the config" (2) from "found something
    # to check" (1); borrowing an exit that collapses them into 1 would report
    # a missing portfolio file as a rollover finding.
    path = Path(config_path)
    try:
        raw = read_json(path, default=None)
        if not isinstance(raw, dict):
            return f"config is not a JSON object: {path}"
        config = resolve_weekly_portfolio_or_raise(raw, path)
    except (OSError, ValueError) as exc:
        return str(exc)
    errors = validate_config(config)
    if errors:
        return "\n".join([f"config validation failed ({len(errors)} error(s))", *errors])
    sources = config.get("sources")
    if not isinstance(sources, list):
        return "config 'sources' must be a list"
    return sources


def _print_verdict(verdict: AnnualSourceVerdict) -> None:
    year_text = verdict.edition_year if verdict.edition_year is not None else "none"
    print(
        f"source={verdict.source_name}; verdict={verdict.verdict}; reason={verdict.reason}; "
        f"edition_year={year_text}; declared_in={verdict.year_source or 'none'}; "
        f"tracked_stage={verdict.tracked_stage or 'none'}; release_window={verdict.release_window or 'none'}; "
        f"url={verdict.url}; landing_url={verdict.landing_url or 'none'}; detail={verdict.detail}"
    )
    for line in describe_evidence(verdict):
        print(f"evidence source={verdict.source_name}; {line}")


def _check_options(live: bool, evidence_dir: Path | None, keep_evidence: Path | None) -> Path | None:
    """Refuse contradictory flags and a protected capture directory before any request."""
    if live and evidence_dir is not None:
        raise _UsageError("--live and --evidence-dir are exclusive: fetch evidence or replay it, not both")
    if keep_evidence is None:
        return None
    if not live:
        raise _UsageError("--keep-evidence writes what --live fetched; it needs --live")
    try:
        return validated_capture_dir(keep_evidence)
    except EvidenceCaptureError as exc:
        raise _UsageError(f"--keep-evidence refused: {exc}") from exc


def _select(sources: list[Any], source_name: str | None) -> list[Mapping[str, Any]]:
    selected = audited_sources(sources)
    if source_name is None:
        return selected
    chosen = [source for source in selected if str(source.get("name", "")).strip() == source_name.strip()]
    if not chosen:
        raise _UsageError(f"no active annual source is named {source_name!r}")
    return chosen


def _live_evidence(
    selected: list[Mapping[str, Any]], today: date, fetcher_factory: FetcherFactory, keep_dir: Path | None
) -> tuple[dict[str, AnnualEditionEvidence], int]:
    evidence: dict[str, AnnualEditionEvidence] = {}
    requests = 0
    for source in selected:
        name = str(source.get("name", "")).strip()
        evidence[name], count = gather_evidence(source, today=today, fetch=fetcher_factory(source))
        requests += count
        if keep_dir is not None:
            write_evidence_capture(keep_dir, name, evidence[name])
    return evidence, requests


def _print_proposals(
    selected: list[Mapping[str, Any]], verdicts: list[AnnualSourceVerdict], repository_root: Path, mode: str
) -> None:
    sources_by_name = {str(source.get("name", "")).strip(): source for source in selected}
    wanting = [
        verdict for verdict in verdicts if build_proposal(sources_by_name[verdict.source_name], verdict, _NO_WIRING)
    ]
    print(f"annual_audit_proposals={len(wanting)}")
    if mode == "offline":
        print("annual_audit_proposals_note=proposals need official-page evidence: pass --live or --evidence-dir")
    if not wanting:
        return
    wiring = load_repository_wiring(repository_root, [verdict.source_name for verdict in wanting])
    for verdict in wanting:
        proposal = build_proposal(sources_by_name[verdict.source_name], verdict, wiring)
        assert proposal is not None
        for line in render_proposal(proposal):
            print(line)


def run_audit_command(
    config_path: Any,
    *,
    today: date | None = None,
    live: bool = False,
    evidence_dir: Path | None = None,
    keep_evidence: Path | None = None,
    source_name: str | None = None,
    propose: bool = False,
    fetcher_factory: FetcherFactory = live_page_fetcher,
    repository_root: Path = REPOSITORY_ROOT,
) -> int:
    """Audit the active annual sources of one config and print the result."""
    audit_date = today if today is not None else date.today()
    sources = _command_sources(config_path)
    if isinstance(sources, str):
        first, *rest = sources.split("\n")
        print(f"annual_audit_status=config_error; detail={first}")
        for error in rest:
            print(f"config_error={error}")
        return 2
    try:
        keep_dir = _check_options(live, evidence_dir, keep_evidence)
        selected = _select(sources, source_name)
        replayed = read_evidence_dir(evidence_dir) if evidence_dir is not None else None
    except (_UsageError, EvidenceCaptureError) as exc:
        print(f"annual_audit_status=usage_error; detail={exc}")
        return 2

    mode = "live" if live else ("replay" if replayed is not None else "offline")
    evidence: Mapping[str, AnnualEditionEvidence] | None = replayed
    requests = 0
    if live:
        try:
            evidence, requests = _live_evidence(selected, audit_date, fetcher_factory, keep_dir)
        except EvidenceCaptureError as exc:
            print(f"annual_audit_status=usage_error; detail=--keep-evidence refused: {exc}")
            return 2
    verdicts = [
        audit_annual_source(
            source,
            today=audit_date,
            evidence=None
            if evidence is None
            else evidence.get(str(source.get("name", "")).strip(), AnnualEditionEvidence()),
        )
        for source in selected
    ]
    findings = rollover_checks_required(verdicts)

    print(f"annual_audit_date={audit_date.isoformat()}")
    print(f"annual_audit_mode={mode}")
    if live:
        print(f"annual_audit_live_requests={requests}")
    print(f"annual_audit_annual_source_count={len(verdicts)}")
    print(f"annual_audit_rollover_checks_required={len(findings)}")
    for verdict in verdicts:
        _print_verdict(verdict)
    if propose:
        _print_proposals(selected, verdicts, repository_root, mode)
    print("annual_audit_mutation=none")

    if not findings:
        print("annual_audit_status=no_rollover_checks_required")
        return 0
    print("annual_audit_status=rollover_checks_required")
    return 1
