"""Deterministic, offline monthly source-gap discovery orchestration.

The monthly operation reuses the canonical Issue #340 analysis and publishes
one derived report pair per operation month plus one derived history index.
It never probes, invokes a model, or changes onboarding or editorial state.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from autoscout.config import resolve_weekly_portfolio_or_raise
from autoscout.monthly_source_discovery_artifacts import MONTHLY_ARTIFACT_TYPE
from autoscout.monthly_source_discovery_artifacts import MONTHLY_HISTORY_ARTIFACT_TYPE
from autoscout.monthly_source_discovery_artifacts import MONTHLY_HISTORY_SCHEMA_VERSION
from autoscout.monthly_source_discovery_artifacts import MONTHLY_SCHEMA_VERSION
from autoscout.monthly_source_discovery_artifacts import MonthlyArtifactError
from autoscout.monthly_source_discovery_artifacts import MonthlyInputError
from autoscout.monthly_source_discovery_artifacts import MonthlyPublicationPaths
from autoscout.monthly_source_discovery_artifacts import canonical_url
from autoscout.monthly_source_discovery_artifacts import derive_monthly_status
from autoscout.monthly_source_discovery_artifacts import inspect_publication
from autoscout.monthly_source_discovery_artifacts import json_text
from autoscout.monthly_source_discovery_artifacts import month_end
from autoscout.monthly_source_discovery_artifacts import month_shift
from autoscout.monthly_source_discovery_artifacts import monthly_evidence_digest
from autoscout.monthly_source_discovery_artifacts import monthly_next_action
from autoscout.monthly_source_discovery_artifacts import monthly_operation_identity
from autoscout.monthly_source_discovery_artifacts import parse_operation_month
from autoscout.monthly_source_discovery_artifacts import publication_paths
from autoscout.monthly_source_discovery_artifacts import publish_monthly_bundle
from autoscout.monthly_source_discovery_artifacts import render_monthly_markdown
from autoscout.monthly_source_discovery_artifacts import sha256
from autoscout.monthly_source_gap_adapter import SourceGapSnapshot
from autoscout.monthly_source_gap_adapter import adapt_source_gap_report
from autoscout.safe_artifacts import bundle_write_lock
from autoscout.search_optimization import DEFAULT_SEARCH_OPTIMIZATION_REVIEW_PATH
from autoscout.source_gap_discovery import discover_source_gaps
from autoscout.source_onboarding_policy import DEFAULT_STATE_DIR
from autoscout.source_onboarding_policy import SourceOnboardingError
from autoscout.source_onboarding_policy import proposal_file_for
from autoscout.source_onboarding_policy import proposal_state_dir
from autoscout.source_probe import SourceCandidateLedgerError
from autoscout.source_probe import normalized_url
from autoscout.source_probe import read_source_candidate_ledger
from autoscout.source_probe import valid_http_url
from autoscout.state_store import Collection
from autoscout.state_store import open_state_store


@dataclass(frozen=True)
class MonthlySourceDiscoveryRequest:
    """All explicit paths and the operation month for one monthly invocation."""

    operation_month: str | None
    portfolio_path: Path
    review_path: Path | None
    ledger_path: Path
    # A state directory's Source Proposals file, read through its State Store;
    # ``None`` is the weekly state directory's.
    proposals_path: Path | None
    output_dir: Path


@dataclass(frozen=True)
class MonthlyInputs:
    """Validated canonical inputs and protected input paths for one run."""

    portfolio: dict[str, Any]
    review: Mapping[str, Any] | None
    ledger: dict[str, dict[str, Any]]
    proposals: list[dict[str, Any]]
    input_paths: tuple[Path, ...]


@dataclass(frozen=True)
class CandidateClassification:
    """Immutable classification of current and ledger-known source candidates."""

    new_urls: tuple[str, ...]
    banked_urls: tuple[str, ...]
    rejected_or_deferred_urls: tuple[str, ...]
    observed_urls: tuple[str, ...]
    current_urls: tuple[str, ...]


def _read_json_file(path: Path, label: str, *, required: bool, default: Any = None) -> Any:
    if path.is_symlink():
        raise MonthlyInputError(f"{label} must not be a symlink")
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        if required:
            raise MonthlyInputError(f"{label} does not exist: {path}") from None
        return default
    except (OSError, UnicodeError) as exc:
        raise MonthlyInputError(f"cannot read {label}: {exc}") from exc
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MonthlyInputError(f"{label} is not valid UTF-8 JSON: {exc}") from exc


def _read_proposals(path: Path) -> list[dict[str, Any]]:
    """The Source Proposals of the state directory *path* names, read through its State Store."""
    label = "Source Proposal state"
    try:
        state_dir = proposal_state_dir(path)
    except SourceOnboardingError as exc:
        raise MonthlyInputError(f"{label} must be a state directory's Source Proposals: {exc}") from exc
    if path.is_symlink():
        raise MonthlyInputError(f"{label} must not be a symlink")
    try:
        value = open_state_store(state_dir).read(Collection.SOURCE_PROPOSALS)
    except (UnicodeError, ValueError) as exc:
        raise MonthlyInputError(f"{label} is not valid UTF-8 JSON: {exc}") from exc
    except OSError as exc:
        raise MonthlyInputError(f"cannot read {label}: {exc}") from exc
    if not isinstance(value, list):
        raise MonthlyInputError("Source Proposal state must be a JSON list")
    proposals: list[dict[str, Any]] = []
    for index, proposal in enumerate(value):
        if not isinstance(proposal, Mapping):
            raise MonthlyInputError(f"Source Proposal entry {index} must be a JSON object")
        if not valid_http_url(proposal.get("source_url")):
            raise MonthlyInputError(f"Source Proposal entry {index} has an invalid source_url")
        status = str(proposal.get("status", "")).strip().casefold()
        if status not in {"proposed", "accepted", "rejected", "infeasible"}:
            raise MonthlyInputError(f"Source Proposal entry {index} has an invalid status")
        if status == "accepted" and (not isinstance(proposal.get("id"), str) or not proposal["id"].strip()):
            raise MonthlyInputError(f"Source Proposal entry {index} has a blank accepted proposal ID")
        proposals.append(dict(proposal))
    return proposals


def _paths_overlap(first: Path, second: Path) -> bool:
    first_resolved = first.resolve(strict=False)
    second_resolved = second.resolve(strict=False)
    return (
        first_resolved == second_resolved
        or first_resolved in second_resolved.parents
        or second_resolved in first_resolved.parents
    )


def _reject_output_input_overlap(
    publication_destinations: MonthlyPublicationPaths, input_paths: tuple[Path, ...]
) -> None:
    for destination in (
        publication_destinations.report,
        publication_destinations.markdown,
        publication_destinations.history,
    ):
        for input_path in input_paths:
            if _paths_overlap(destination, input_path):
                raise MonthlyInputError(
                    f"monthly publication destination overlaps an input artifact: {destination.name} and {input_path.name}"
                )


def _configured_source_urls(portfolio: Mapping[str, Any]) -> set[str]:
    raw_sources = portfolio.get("sources")
    if not isinstance(raw_sources, list):
        raise MonthlyInputError("resolved portfolio sources must be a list")
    return {
        normalized_url(str(source["url"]))
        for source in raw_sources
        if isinstance(source, Mapping) and valid_http_url(source.get("url"))
    }


def _classify_candidates(
    snapshot: SourceGapSnapshot,
    ledger: Mapping[str, Mapping[str, Any]],
    configured_urls: set[str],
) -> CandidateClassification:
    """Classify current candidates from portable canonical evidence only."""
    ledger_known: set[str] = set()
    accepted_banked: set[str] = set()
    rejected_or_deferred: set[str] = set()
    for ledger_url, entry in ledger.items():
        if not isinstance(entry, Mapping):
            raise MonthlyInputError("Source Candidate Ledger entry is not an object")
        url = canonical_url(entry.get("url"), MonthlyInputError, "Source Candidate Ledger entry")
        if url != ledger_url:
            raise MonthlyInputError("Source Candidate Ledger entry identity is not canonical")
        ledger_known.add(url)
        outcome = entry.get("outcome")
        if outcome == "accept":
            if url not in configured_urls:
                accepted_banked.add(url)
        elif outcome in {"reject", "defer"}:
            rejected_or_deferred.add(url)
        else:
            raise MonthlyInputError(f"unsupported Source Candidate Ledger outcome: {outcome}")

    current_urls = set(snapshot.current_candidate_urls)
    new_urls = current_urls - configured_urls - ledger_known
    banked_urls = accepted_banked - configured_urls
    observed_urls = (current_urls | accepted_banked) - configured_urls
    return CandidateClassification(
        new_urls=tuple(sorted(new_urls)),
        banked_urls=tuple(sorted(banked_urls)),
        rejected_or_deferred_urls=tuple(sorted(rejected_or_deferred)),
        observed_urls=tuple(sorted(observed_urls)),
        current_urls=tuple(sorted(current_urls)),
    )


def _parse_canonical_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _onboarding_evidence(matching: list[dict[str, Any]], configured_url: str) -> list[tuple[date, dict[str, Any]]]:
    evidence: list[tuple[date, dict[str, Any]]] = []
    for proposal in matching:
        request = proposal.get("onboarding_request")
        request_url = request.get("url") if isinstance(request, Mapping) else None
        request_date = request.get("run_date") if isinstance(request, Mapping) else None
        parsed_date = _parse_canonical_date(request_date)
        if (
            parsed_date is not None
            and valid_http_url(request_url)
            and normalized_url(str(request_url)) == configured_url
        ):
            evidence.append((parsed_date, proposal))
    return sorted(evidence, key=lambda item: (item[0], str(item[1].get("id", ""))))


def _onboarding_progress(
    proposals: list[dict[str, Any]], portfolio: Mapping[str, Any], operation_month: str, month_start: date
) -> dict[str, Any]:
    configured_urls = _configured_source_urls(portfolio)
    window_start = month_shift(month_start, -11)
    window_end = month_end(month_start)
    accepted_by_url: dict[str, list[dict[str, Any]]] = {}
    for proposal in proposals:
        if str(proposal.get("status", "")).casefold() == "accepted":
            accepted_by_url.setdefault(normalized_url(str(proposal["source_url"])), []).append(proposal)

    verified: dict[str, dict[str, str]] = {}
    missing_evidence: set[str] = set()
    in_window_sources: list[dict[str, str]] = []
    without_completion: set[str] = set()
    without_accepted_proposal: set[str] = set()
    for configured_url in sorted(configured_urls):
        matching = accepted_by_url.get(configured_url, [])
        evidence = _onboarding_evidence(matching, configured_url)
        if evidence:
            parsed_date, proposal = evidence[0]
            verified[configured_url] = {
                "url": configured_url,
                "proposal_id": str(proposal.get("id", "")),
                "onboarding_run_date": parsed_date.isoformat(),
            }
        else:
            missing_evidence.add(configured_url)
            if matching:
                without_completion.add(configured_url)
            else:
                without_accepted_proposal.add(configured_url)

        proposal_dates: list[tuple[date, dict[str, Any]]] = []
        for proposal in matching:
            parsed_proposal_date = _parse_canonical_date(proposal.get("proposal_date"))
            if parsed_proposal_date is not None and window_start <= parsed_proposal_date <= window_end:
                proposal_dates.append((parsed_proposal_date, proposal))
        if proposal_dates:
            parsed_date, proposal = sorted(proposal_dates, key=lambda item: (item[0], str(item[1].get("id", ""))))[0]
            in_window_sources.append(
                {
                    "url": configured_url,
                    "proposal_id": str(proposal.get("id", "")),
                    "proposal_date": parsed_date.isoformat(),
                }
            )

    verified_in_window = [
        value
        for value in verified.values()
        if window_start <= date.fromisoformat(value["onboarding_run_date"]) <= window_end
    ]
    verified_in_window.sort(key=lambda value: (value["onboarding_run_date"], value["url"]))
    in_window_sources.sort(key=lambda value: (value["proposal_date"], value["url"]))
    evidence_complete = not missing_evidence
    verified_count = len(verified_in_window)
    accepted_proposal_signal = {
        "semantics": "accepted_proposal_in_window_not_verified_onboarding_completion",
        "in_window_count": len(in_window_sources),
        "in_window_sources": in_window_sources,
        "without_completion_count": len(without_completion),
        "without_completion_urls": sorted(without_completion),
        "without_accepted_proposal_count": len(without_accepted_proposal),
        "without_accepted_proposal_urls": sorted(without_accepted_proposal),
    }
    return {
        "goal": 12,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "verified_completed_count": verified_count,
        "verified_completed_count_semantics": "exact" if evidence_complete else "auditable_lower_bound",
        "remaining_to_goal": max(0, 12 - verified_count) if evidence_complete else None,
        "evidence_status": "complete" if evidence_complete else "insufficient_evidence",
        "configured_source_count": len(configured_urls),
        "configured_without_canonical_onboarding_evidence_count": len(missing_evidence),
        "configured_without_canonical_onboarding_evidence_urls": sorted(missing_evidence),
        "verified_completed_sources": verified_in_window,
        "accepted_proposal_signal": accepted_proposal_signal,
        "operation_month": operation_month,
    }


def _error_report(
    status: str,
    error: str,
    next_action: str,
    *,
    operation_month: str | None = None,
) -> dict[str, Any]:
    return {
        "artifact_type": MONTHLY_ARTIFACT_TYPE,
        "version": MONTHLY_SCHEMA_VERSION,
        "status": status,
        "operation_month": operation_month,
        "operation_identity": None,
        "evidence_digest": None,
        "new_candidate_urls": [],
        "banked_candidate_urls": [],
        "ledger_rejected_or_deferred_urls": [],
        "coverage_gaps": [],
        "next_action": next_action,
        "error": error.strip() or status,
    }


def _load_monthly_inputs(request: MonthlySourceDiscoveryRequest) -> MonthlyInputs:
    portfolio_value = _read_json_file(request.portfolio_path, "portfolio", required=True)
    if not isinstance(portfolio_value, dict):
        raise MonthlyInputError("portfolio must be a JSON object")
    try:
        portfolio = resolve_weekly_portfolio_or_raise(portfolio_value, request.portfolio_path)
    except (OSError, UnicodeError, ValueError) as exc:
        raise MonthlyInputError(str(exc)) from exc
    input_paths: list[Path] = [request.portfolio_path.resolve(strict=False)]
    portfolio_ref = portfolio_value.get("weekly_portfolio")
    if isinstance(portfolio_ref, str) and portfolio_ref.strip():
        referenced_path = (request.portfolio_path.parent / portfolio_ref).resolve()
        input_paths.append(referenced_path)
        try:
            referenced_path.read_bytes()
        except (OSError, UnicodeError) as exc:
            raise MonthlyInputError(f"cannot read weekly portfolio file: {exc}") from exc

    selected_review_path = request.review_path or DEFAULT_SEARCH_OPTIMIZATION_REVIEW_PATH
    review_value = _read_json_file(
        selected_review_path,
        "Search Optimization Review",
        required=request.review_path is not None,
        default=None,
    )
    input_paths.append(selected_review_path.resolve(strict=False))
    if review_value is not None and not isinstance(review_value, Mapping):
        raise MonthlyInputError("Search Optimization Review must be a JSON object")

    try:
        ledger = read_source_candidate_ledger(request.ledger_path)
    except SourceCandidateLedgerError as exc:
        raise MonthlyInputError(str(exc)) from exc
    if request.ledger_path.is_symlink():
        raise MonthlyInputError("Source Candidate Ledger must not be a symlink")
    input_paths.extend(
        [request.ledger_path.resolve(strict=False), request.ledger_path.with_suffix(".md").resolve(strict=False)]
    )
    proposals_path = request.proposals_path or proposal_file_for(DEFAULT_STATE_DIR)
    proposals = _read_proposals(proposals_path)
    input_paths.append(proposals_path.resolve(strict=False))
    return MonthlyInputs(
        portfolio=portfolio,
        review=review_value,
        ledger=ledger,
        proposals=proposals,
        input_paths=tuple(input_paths),
    )


def run_monthly_source_discovery(request: MonthlySourceDiscoveryRequest) -> tuple[dict[str, Any], int]:
    """Run one offline monthly report and return its JSON model and exit code."""
    parsed_month = parse_operation_month(request.operation_month)
    if parsed_month is None:
        return (
            _error_report(
                "input_error",
                "--operation-month is required and must use YYYY-MM",
                "Pass an explicit operation month as YYYY-MM, then run the monthly report again.",
                operation_month=request.operation_month,
            ),
            2,
        )
    month_text, month_start = parsed_month
    try:
        inputs = _load_monthly_inputs(request)
        destinations = publication_paths(request.output_dir, month_text)
        _reject_output_input_overlap(destinations, inputs.input_paths)
        output_dir_resolved = request.output_dir.resolve(strict=False)
        with bundle_write_lock(destinations.history):
            existing_report, history_entries = inspect_publication(
                output_dir_resolved, destinations.history, month_text
            )
            gap_report = discover_source_gaps(
                inputs.portfolio,
                inputs.review,
                set(inputs.ledger),
                None,
                ledger_path=request.ledger_path,
                probe=False,
            )
            snapshot = adapt_source_gap_report(gap_report, inputs.ledger)
            configured_urls = _configured_source_urls(inputs.portfolio)
            classification = _classify_candidates(snapshot, inputs.ledger, configured_urls)
            progress = _onboarding_progress(inputs.proposals, inputs.portfolio, month_text, month_start)
            status = derive_monthly_status(
                snapshot.status,
                classification.new_urls,
                classification.banked_urls,
            )
            next_action = monthly_next_action(status)
            semantic_report: dict[str, Any] = {
                "artifact_type": MONTHLY_ARTIFACT_TYPE,
                "version": MONTHLY_SCHEMA_VERSION,
                "operation_month": month_text,
                "operation_identity": "",
                "evidence_digest": "",
                "status": status,
                "source_gap_status": snapshot.status,
                "current_candidate_urls": list(classification.current_urls),
                "observed_candidate_urls": list(classification.observed_urls),
                "new_candidate_urls": list(classification.new_urls),
                "banked_candidate_urls": list(classification.banked_urls),
                "ledger_rejected_or_deferred_urls": list(classification.rejected_or_deferred_urls),
                "coverage_gaps": list(snapshot.coverage_gaps),
                "family_coverage": snapshot.family_coverage,
                "sources_without_signal_family": list(snapshot.sources_without_signal_family),
                "onboarding_progress": progress,
                "next_action": next_action,
            }
            evidence_digest = monthly_evidence_digest(semantic_report)
            report = dict(semantic_report)
            report["evidence_digest"] = evidence_digest
            report["operation_identity"] = monthly_operation_identity(month_text, evidence_digest)
            if existing_report is not None and existing_report == report:
                return existing_report, 0

            report_text = json_text(report)
            markdown_text = render_monthly_markdown(report)
            new_history_entry = {
                "operation_month": month_text,
                "operation_identity": report["operation_identity"],
                "evidence_digest": evidence_digest,
                "report_sha256": sha256(report_text.encode()),
                "markdown_sha256": sha256(markdown_text.encode()),
                "status": status,
                "observed_candidate_urls": list(classification.observed_urls),
                "new_candidate_urls": list(classification.new_urls),
                "banked_candidate_urls": list(classification.banked_urls),
                "ledger_rejected_or_deferred_urls": list(classification.rejected_or_deferred_urls),
            }
            replacement_history = [entry for entry in history_entries if entry["operation_month"] != month_text]
            replacement_history.append(new_history_entry)
            replacement_history.sort(key=lambda entry: (entry["operation_month"], entry["operation_identity"]))
            history_value = {
                "artifact_type": MONTHLY_HISTORY_ARTIFACT_TYPE,
                "version": MONTHLY_HISTORY_SCHEMA_VERSION,
                "entries": replacement_history,
            }
            publish_monthly_bundle(
                destinations,
                report_text=report_text,
                markdown_text=markdown_text,
                history_text=json_text(history_value),
            )
            return report, 0
    except MonthlyArtifactError as exc:
        return (
            _error_report(
                "artifact_error",
                str(exc),
                "Repair or remove the invalid derived monthly publication, then run the report again.",
                operation_month=month_text,
            ),
            2,
        )
    except (MonthlyInputError, OSError, TypeError, ValueError) as exc:
        return (
            _error_report(
                "input_error",
                str(exc),
                "Correct the monthly input artifacts, then run the offline monthly report again.",
                operation_month=month_text,
            ),
            2,
        )
