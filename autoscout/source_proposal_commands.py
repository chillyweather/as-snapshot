"""Source Proposal operator commands: add, accept, reject, reconsider."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from autoscout.json_io import read_json_or_default
from autoscout.json_io import write_json
from autoscout.source_proposals import ProposalReconsiderationError
from autoscout.source_proposals import confirm_proposal_reconsideration
from autoscout.source_proposals import merge_proposal_attempt
from autoscout.source_proposals import normalize_source_identity
from autoscout.source_proposals import prepare_proposal_reconsideration
from autoscout.source_proposals import proposal_from_attempt
from autoscout.source_rules import is_skipped_source
from autoscout.state_store import JSON_BACKEND
from autoscout.state_store import BackendSelection
from autoscout.state_store import Collection
from autoscout.state_store import StateStore
from autoscout.state_store import open_state_store


def write_source_proposals_state(state_dir: Path, proposals: list[dict[str, Any]]) -> None:
    """Replace the Source Proposals of *state_dir* (the onboarding flow's staged copies).

    The staged copies are JSON files the scaffold reads, so they are written
    with the JSON backend whichever backend keeps canonical state (#1074).
    """
    staged_proposals_store(state_dir).replace(Collection.SOURCE_PROPOSALS, proposals)


def staged_proposals_store(state_dir: Path) -> StateStore:
    """The store of the onboarding flow's staged proposal copies: JSON files, whatever backend is active."""
    return open_state_store(state_dir, BackendSelection(JSON_BACKEND, "staging"))


def _proposal_store(state_dir: Path, state_store: StateStore | None = None) -> StateStore:
    return state_store if state_store is not None else open_state_store(state_dir)


def prepare_source_proposal(
    existing: list[dict[str, Any]],
    *,
    source_url: str,
    proposed_by: str,
    reason: str,
    proposal_date: str | None = None,
    supporting_evidence: list[dict[str, Any]] | None = None,
    suggested_signal_family: str = "",
    editor_endorsement: dict[str, Any] | None = None,
    status: str = "proposed",
    decision_reason: str = "",
    source_name: str = "",
    access_status: str = "",
    caveats: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
    """Prepare a proposal merge without reading or writing repository state."""
    attempt = proposal_from_attempt(
        source_url=source_url,
        proposed_by=proposed_by,
        reason=reason,
        proposal_date=proposal_date,
        supporting_evidence=supporting_evidence,
        suggested_signal_family=suggested_signal_family,
        editor_endorsement=editor_endorsement,
        status=status,
        decision_reason=decision_reason,
        source_name=source_name,
        access_status=access_status,
        caveats=caveats,
    )
    return merge_proposal_attempt(existing, attempt)


def record_source_proposal(
    state_dir: Path,
    *,
    source_url: str,
    proposed_by: str,
    reason: str,
    proposal_date: str | None = None,
    supporting_evidence: list[dict[str, Any]] | None = None,
    suggested_signal_family: str = "",
    editor_endorsement: dict[str, Any] | None = None,
    status: str = "proposed",
    decision_reason: str = "",
    source_name: str = "",
    access_status: str = "",
    caveats: str = "",
    state_store: StateStore | None = None,
) -> tuple[dict[str, Any], bool]:
    """Merge one proposal into the Source Proposals of *state_store*, or of a store over *state_dir*."""
    state_dir.mkdir(parents=True, exist_ok=True)
    store = _proposal_store(state_dir, state_store)
    existing = store.read(Collection.SOURCE_PROPOSALS)
    if not isinstance(existing, list):
        raise ValueError("source proposal state must contain a list")
    proposals, proposal, created = prepare_source_proposal(
        existing,
        source_url=source_url,
        proposed_by=proposed_by,
        reason=reason,
        proposal_date=proposal_date,
        supporting_evidence=supporting_evidence,
        suggested_signal_family=suggested_signal_family,
        editor_endorsement=editor_endorsement,
        status=status,
        decision_reason=decision_reason,
        source_name=source_name,
        access_status=access_status,
        caveats=caveats,
    )
    store.replace(Collection.SOURCE_PROPOSALS, proposals)
    return proposal, created


def find_source_proposal(proposals: list[dict[str, Any]], proposal_id: str) -> dict[str, Any] | None:
    for proposal in proposals:
        if proposal.get("id") == proposal_id:
            return proposal
    return None


def has_configured_source_url(sources: list[dict[str, Any]], source_url: str) -> bool:
    normalized = normalize_source_identity(source_url)
    return any(
        normalize_source_identity(str(source.get("url", ""))) == normalized for source in sources if source.get("url")
    )


def configured_source_from_proposal(
    proposal: dict[str, Any],
    source_name: str | None,
    access_status: str,
    access_reason: str,
    language: str = "",
) -> dict[str, Any]:
    source = {
        "name": (
            source_name or proposal.get("source_name") or source_name_from_url(str(proposal.get("source_url", "")))
        ),
        "family": proposal.get("suggested_signal_family") or "media/curated list",
        "url": proposal.get("source_url", ""),
        "access_status": access_status,
        "extraction_method": "agent-extraction",
        "check_frequency": "weekly",
        "language": language.strip(),
        "lifecycle_status": "probation",
        "lifecycle_note": access_reason
        or "Newly accepted source proposal; review source metrics before promote or retire.",
    }
    if access_reason:
        source["notes"] = access_reason
    return source


def source_name_from_url(source_url: str) -> str:
    normalized = source_url.replace("https://", "").replace("http://", "").strip("/")
    return normalized or "Accepted Source Proposal"


def accept_source_proposal_record(
    proposals: list[dict[str, Any]],
    proposal_id: str,
    *,
    source_name: str | None,
    access_status: str,
    access_reason: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Accept one proposal without writing configuration.

    This is the lower-level acceptance operation used by URL onboarding.
    The legacy ``source-proposal accept`` command still adds the returned
    configured-source entry to its config file.
    """
    proposal = find_source_proposal(proposals, proposal_id)
    if proposal is None:
        raise ValueError(f"source proposal not found: {proposal_id}")
    source_entry = configured_source_from_proposal(
        proposal,
        source_name,
        access_status,
        access_reason,
    )
    skipped = is_skipped_source(source_entry)
    proposal["status"] = "infeasible" if skipped else "accepted"
    proposal["decision_reason"] = access_reason if skipped else "accepted by Dima"
    return proposal, source_entry


def accept_source_proposal_state(
    state_dir: Path,
    proposal_id: str,
    *,
    source_name: str | None = None,
    access_status: str = "open",
    access_reason: str = "",
    store: StateStore | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Accept one proposal in the Source Proposals of *state_dir* (or *store*) without touching configuration."""
    store = _proposal_store(state_dir, store)
    with store.transaction():
        proposals = store.read(Collection.SOURCE_PROPOSALS)
        if not isinstance(proposals, list):
            raise ValueError("source proposal state must contain a list")
        proposal, source_entry = accept_source_proposal_record(
            proposals,
            proposal_id,
            source_name=source_name,
            access_status=access_status,
            access_reason=access_reason,
        )
        store.replace(Collection.SOURCE_PROPOSALS, proposals)
    return proposal, source_entry


def add_source_proposal(
    state_dir: Path,
    source_url: str,
    source_name: str,
    proposed_by: str,
    reason: str,
    suggested_signal_family: str,
    access_status: str,
    caveats: str,
) -> int:
    if not source_url.strip():
        print("source_proposal_missing_url")
        return 1
    if not reason.strip():
        print("source_proposal_reason_required")
        return 1
    proposal, created = record_source_proposal(
        state_dir,
        source_url=source_url,
        proposed_by=proposed_by.strip() or "agent",
        reason=reason.strip(),
        suggested_signal_family=suggested_signal_family.strip(),
        source_name=source_name.strip(),
        access_status=access_status.strip(),
        caveats=caveats.strip(),
    )
    print(f"source_proposal_status={proposal['status']}")
    print(f"source_proposal_id={proposal['id']}")
    print(f"source_proposal_created={str(created).lower()}")
    print("configured_source_added=false")
    return 0


def accept_source_proposal(
    state_dir: Path,
    config_path: Path,
    proposal_id: str,
    source_name: str | None,
    access_status: str,
    access_reason: str,
    language: str,
) -> int:
    if not language.strip():
        print("source_proposal_language_required: pass --language with the source's declared language")
        return 1
    store = _proposal_store(state_dir)
    proposals = store.read(Collection.SOURCE_PROPOSALS)
    proposal = find_source_proposal(proposals, proposal_id)
    if proposal is None:
        print(f"source_proposal_not_found id={proposal_id}")
        return 1
    config = read_json_or_default(config_path, {})
    sources = list(config.get("sources", []))
    source_entry = configured_source_from_proposal(
        proposal,
        source_name,
        access_status,
        access_reason,
        language,
    )
    if not has_configured_source_url(sources, str(source_entry.get("url", ""))):
        sources.append(source_entry)
        config["sources"] = sources
        write_json(config_path, config)
    skipped = is_skipped_source(source_entry)
    # Infeasible accepted sources stay in config so future runs report them as Skipped Sources.
    proposal["status"] = "infeasible" if skipped else "accepted"
    proposal["decision_reason"] = access_reason if skipped else "accepted by Dima"
    store.replace(Collection.SOURCE_PROPOSALS, proposals)
    print(f"source_proposal_status={proposal['status']}")
    print(f"configured_source_url={source_entry['url']}")
    return 0


def reject_source_proposal(state_dir: Path, proposal_id: str, reason: str) -> int:
    if not reason.strip():
        print("reject_reason_required")
        return 1
    store = _proposal_store(state_dir)
    proposals = store.read(Collection.SOURCE_PROPOSALS)
    proposal = find_source_proposal(proposals, proposal_id)
    if proposal is None:
        print(f"source_proposal_not_found id={proposal_id}")
        return 1
    proposal["status"] = "rejected"
    proposal["decision_reason"] = reason.strip()
    store.replace(Collection.SOURCE_PROPOSALS, proposals)
    print("source_proposal_status=rejected")
    return 0


def reconsider_source_proposal(
    state_dir: Path,
    proposal_id: str,
    ledger_path: Path,
    reason: str,
    reconsidered_by: str,
    confirmation_token: str = "",
    confirmed_by_editor: bool = False,
) -> int:
    """Reopen one rejected Source Proposal on fresh accepted probe evidence (Issue #578).

    Without ``confirmed_by_editor`` this prints the preview and its token and
    returns 3 with nothing written. The confirmed call recomputes the preview
    from the current proposal and ledger, so a token issued for other evidence
    or another reason is refused. The fresh evidence is the proposal URL's
    current entry in the Source Candidate Ledger; the old rejection is kept in
    ``prior_decisions``. The reopened proposal is ``proposed``, not accepted.
    """
    from autoscout.source_probe import normalized_url as ledger_url_identity
    from autoscout.source_probe import read_source_candidate_ledger

    store = _proposal_store(state_dir)
    proposals = store.read(Collection.SOURCE_PROPOSALS)
    if not isinstance(proposals, list):
        raise ValueError("source proposal state must contain a list")
    proposal = find_source_proposal(proposals, proposal_id)
    if proposal is None:
        print(f"source_proposal_not_found id={proposal_id}")
        return 1
    try:
        entries = read_source_candidate_ledger(ledger_path)
        ledger_sha256 = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    except (OSError, ValueError) as exc:
        print(f"source_proposal_reconsideration_refused reason_code=ledger_unreadable message={exc}")
        return 1
    # The ledger keeps the query in its identity: ``?year=2025`` and
    # ``?year=2026`` are two entries, and only the proposal's own URL is its evidence.
    entry = entries.get(ledger_url_identity(str(proposal.get("source_url", ""))))
    if entry is None:
        print("source_proposal_reconsideration_refused reason_code=evidence_not_accepted message=no ledger entry")
        return 1
    fresh_evidence = {
        "source_url": entry.get("url", ""),
        "outcome": entry.get("outcome", ""),
        "reason_code": entry.get("reason_code", ""),
        "probe_date": entry.get("probe_date", ""),
        "bounded_entry_count": entry.get("bounded_entry_count"),
        "detected_heading": entry.get("detected_heading", ""),
        "contract_family": entry.get("contract_family", ""),
        "evidence_path": ledger_path.as_posix(),
        "evidence_sha256": ledger_sha256,
    }
    try:
        preview, _token = prepare_proposal_reconsideration(
            proposal,
            fresh_evidence=fresh_evidence,
            reconsideration_reason=reason,
            reconsidered_by=reconsidered_by,
            reconsideration_date=str(entry.get("probe_date", "")),
        )
        if not confirmed_by_editor:
            print(json.dumps(preview, ensure_ascii=False, indent=2, sort_keys=True))
            return 3
        updated = confirm_proposal_reconsideration(
            proposal,
            preview_payload=preview,
            confirmation_token=confirmation_token,
            confirmed_by_editor=True,
            fresh_evidence=fresh_evidence,
        )
    except ProposalReconsiderationError as exc:
        print(f"source_proposal_reconsideration_refused reason_code={exc.reason_code} message={exc.message}")
        return 1
    proposals[proposals.index(proposal)] = updated
    store.replace(Collection.SOURCE_PROPOSALS, proposals)
    print(f"source_proposal_status={updated['status']}")
    print(f"source_proposal_id={updated['id']}")
    print("configured_source_added=false")
    return 0
