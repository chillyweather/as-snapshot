from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import date
from typing import Any
from urllib.parse import urlparse
from urllib.parse import urlunparse

SUPPRESSED_STATUSES = {"rejected", "infeasible"}
VALID_STATUSES = {"proposed", "accepted", "rejected", "infeasible"}


class ProposalReconsiderationError(ValueError):
    """An audited rejected-proposal reconsideration cannot proceed.

    *reason_code* names the exact refusal so callers can report one concrete
    next action without mutating proposal state (Issue #580).
    """

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


def normalize_source_identity(source_url: str) -> str:
    parsed = urlparse(source_url.strip())
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    if not netloc and parsed.path:
        reparsed = urlparse(f"https://{source_url.strip()}")
        scheme = reparsed.scheme.lower()
        netloc = reparsed.netloc.lower()
        path = reparsed.path.rstrip("/")
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return urlunparse((scheme, netloc, path, "", "", ""))


def proposal_id_for(source_url: str) -> str:
    identity = normalize_source_identity(source_url)
    return "source-" + "".join(
        character if character.isalnum() else "-"
        for character in identity.replace("https://", "").replace("http://", "")
    ).strip("-")


def proposal_from_attempt(
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
) -> dict[str, Any]:
    if status not in VALID_STATUSES:
        raise ValueError(f"Unsupported Source Proposal status: {status}")
    normalized_url = normalize_source_identity(source_url)
    return {
        "id": proposal_id_for(source_url),
        "source_url": source_url.strip(),
        "normalized_source": normalized_url,
        "proposed_by": proposed_by,
        "proposal_date": proposal_date or date.today().isoformat(),
        "reason": reason,
        "supporting_evidence": supporting_evidence or [],
        "suggested_signal_family": suggested_signal_family,
        "source_name": source_name,
        "access_status": access_status,
        "caveats": caveats,
        "editor_endorsement": editor_endorsement,
        "status": status,
        "decision_reason": decision_reason,
    }


def merge_proposal_attempt(
    proposals: list[dict[str, Any]], attempt: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
    normalized = str(attempt.get("normalized_source") or normalize_source_identity(attempt["source_url"]))
    for index, proposal in enumerate(proposals):
        existing_normalized = str(
            proposal.get("normalized_source") or normalize_source_identity(str(proposal.get("source_url", "")))
        )
        if existing_normalized != normalized:
            continue
        if proposal.get("status") in SUPPRESSED_STATUSES:
            return proposals, proposal, False
        merged = merge_proposal(proposal, attempt)
        updated = list(proposals)
        updated[index] = merged
        return updated, merged, False
    return [*proposals, attempt], attempt, True


def merge_proposal(existing: dict[str, Any], attempt: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    merged["normalized_source"] = str(
        existing.get("normalized_source")
        or attempt.get("normalized_source")
        or normalize_source_identity(str(existing.get("source_url", attempt.get("source_url", ""))))
    )
    merged["proposed_by"] = merge_unique_text(existing.get("proposed_by"), attempt.get("proposed_by"))
    merged["reason"] = merge_unique_text(existing.get("reason"), attempt.get("reason"))
    merged["supporting_evidence"] = merge_evidence(
        existing.get("supporting_evidence", []), attempt.get("supporting_evidence", [])
    )
    if attempt.get("suggested_signal_family") and not merged.get("suggested_signal_family"):
        merged["suggested_signal_family"] = attempt["suggested_signal_family"]
    if attempt.get("source_name") and not merged.get("source_name"):
        merged["source_name"] = attempt["source_name"]
    if attempt.get("access_status") and not merged.get("access_status"):
        merged["access_status"] = attempt["access_status"]
    if attempt.get("caveats"):
        merged["caveats"] = merge_unique_text(existing.get("caveats"), attempt.get("caveats"))
    if attempt.get("editor_endorsement"):
        merged["editor_endorsement"] = attempt["editor_endorsement"]
    return merged


def merge_unique_text(existing: Any, incoming: Any) -> str:
    parts: list[str] = []
    for value in (existing, incoming):
        text = str(value or "").strip()
        if text and text not in parts:
            parts.append(text)
    return "; ".join(parts)


def merge_evidence(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*existing, *incoming]:
        key = repr(sorted(item.items()))
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def agent_proposals_from_collected_evidence(
    source_mentions: list[dict[str, Any]],
    configured_sources: list[dict[str, Any]],
    existing_proposals: list[dict[str, Any]],
    run_date: str,
    *,
    minimum_references: int = 2,
) -> tuple[list[dict[str, Any]], int]:
    configured_identities = {
        normalize_source_identity(str(source.get("url", ""))) for source in configured_sources if source.get("url")
    }
    attempts_by_source: dict[str, list[dict[str, Any]]] = {}
    for mention in source_mentions:
        for referenced_url in referenced_source_urls(mention):
            normalized = normalize_source_identity(referenced_url)
            if not normalized or normalized in configured_identities:
                continue
            attempts_by_source.setdefault(normalized, []).append(
                {
                    "title": mention.get("title", ""),
                    "source_url": mention.get("source_url", ""),
                    "evidence_url": mention.get("evidence_url", ""),
                    "source_name": mention.get("source_name", ""),
                }
            )

    proposals = list(existing_proposals)
    created_count = 0
    for normalized, evidence in sorted(attempts_by_source.items()):
        if len(evidence) < minimum_references:
            continue
        attempt = proposal_from_attempt(
            source_url=normalized,
            proposed_by="agent",
            proposal_date=run_date,
            reason=(f"Referenced by {len(evidence)} already-collected evidence records from configured sources."),
            supporting_evidence=evidence,
            suggested_signal_family="media/curated list",
        )
        proposals, _, created = merge_proposal_attempt(proposals, attempt)
        if created:
            created_count += 1
    return proposals, created_count


def referenced_source_urls(mention: dict[str, Any]) -> list[str]:
    raw = mention.get("referenced_source_urls", [])
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if not isinstance(raw, list):
        return []
    return [str(value).strip() for value in raw if str(value).strip()]


def reconsideration_token_for(payload: Mapping[str, Any]) -> str:
    """Return the deterministic token for one reconsideration preview payload."""
    encoded = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_iso_date(value: object, *, label: str, reason_code: str) -> date:
    text = value.strip() if isinstance(value, str) else ""
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ProposalReconsiderationError(reason_code, f"{label} must be ISO formatted as YYYY-MM-DD") from exc


_EVIDENCE_SHA_RE = re.compile(r"[0-9a-f]{64}")


def _require_evidence_identity(
    evidence: Mapping[str, Any], *, path_key: str = "evidence_path", digest_key: str = "evidence_sha256"
) -> tuple[str, str]:
    """Return the retained-evidence identity bound into the preview token.

    The path names the retained evidence artifact (ledger or capture file) and
    the digest fingerprints its bytes, so two captures with identical summary
    fields still produce distinct tokens. Both are re-verified at
    confirmation time against the current evidence.
    """
    path = evidence.get(path_key, "")
    digest = evidence.get(digest_key, "")
    path_text = path.strip() if isinstance(path, str) else ""
    digest_text = digest.strip().casefold() if isinstance(digest, str) else ""
    if not path_text or _EVIDENCE_SHA_RE.fullmatch(digest_text) is None:
        raise ProposalReconsiderationError(
            "evidence_identity_missing",
            "Reconsideration requires the retained evidence path and SHA-256 digest",
        )
    return path_text, digest_text


def _validate_reconsideration_payload(
    payload: Mapping[str, Any], *, proposal_url: str, proposal_day: date
) -> tuple[date, str, str]:
    """Validate one preview payload's admission fields and metadata.

    Shared by preview and confirmation so confirmation re-runs the complete
    preview validation instead of trusting hash consistency alone: acceptance,
    URL, date, evidence identity, reason, author, and reconsideration date are
    all re-checked. Returns the probe day, reason, and reconsidered-by text.
    """
    evidence_url = str(payload.get("fresh_source_url", "")).strip()
    outcome = str(payload.get("fresh_outcome", "")).strip()
    probe_text = str(payload.get("fresh_probe_date", "")).strip()
    probe_day = _parse_iso_date(probe_text, label="probe_date", reason_code="evidence_stale")
    _check_evidence_admission(evidence_url, outcome, probe_day, proposal_day, proposal_url=proposal_url)
    _require_evidence_identity(payload, path_key="fresh_evidence_path", digest_key="fresh_evidence_sha256")
    raw_reason = payload.get("reconsideration_reason", "")
    reason = raw_reason.strip() if isinstance(raw_reason, str) else ""
    if not reason:
        raise ProposalReconsiderationError("reason_required", "Reconsideration reason is required")
    raw_by = payload.get("reconsidered_by", "")
    by_text = raw_by.strip() if isinstance(raw_by, str) else ""
    if not by_text:
        raise ProposalReconsiderationError("reconsidered_by_required", "Reconsidered-by identity is required")
    recon_day = _parse_iso_date(
        str(payload.get("reconsideration_date", "")).strip(),
        label="reconsideration_date",
        reason_code="reconsideration_date_mismatch",
    )
    if recon_day != probe_day:
        raise ProposalReconsiderationError(
            "reconsideration_date_mismatch", "Reconsideration date must equal the fresh evidence probe date"
        )
    return probe_day, reason, by_text


def _check_evidence_admission(
    evidence_url: str,
    outcome: str,
    probe_day: date,
    proposal_day: date,
    *,
    proposal_url: str,
) -> None:
    """Repeat the acceptance, URL, and date admission checks.

    Shared by preview and confirmation so a hash-consistent payload alone can
    never reopen a proposal: confirmation re-runs validity, not just
    consistency.
    """
    if not evidence_url or outcome != "accept":
        raise ProposalReconsiderationError(
            "evidence_not_accepted", "Reconsideration requires fresh accepted probe evidence"
        )
    if normalize_source_identity(evidence_url) != normalize_source_identity(proposal_url):
        raise ProposalReconsiderationError(
            "evidence_url_mismatch", "Fresh evidence URL does not match the rejected proposal"
        )
    if probe_day <= proposal_day:
        raise ProposalReconsiderationError(
            "evidence_stale", "Fresh evidence probe date must be after the rejection date"
        )


def prepare_proposal_reconsideration(
    existing: Mapping[str, Any],
    *,
    fresh_evidence: Mapping[str, Any],
    reconsideration_reason: str,
    reconsidered_by: str,
    reconsideration_date: str,
) -> tuple[dict[str, Any], str]:
    """Preview an audited reconsideration of one rejected proposal (zero mutation).

    The preview preserves the old rejection identity, date, and reason, binds
    the request to fresh accepted evidence, and returns a deterministic
    confirmation token. Call :func:`confirm_proposal_reconsideration` with the
    exact preview payload, token, and explicit editor confirmation to apply it.
    """
    status = str(existing.get("status", "")).strip()
    if status != "rejected":
        raise ProposalReconsiderationError(
            "proposal_not_rejected", f"Source Proposal status is {status or 'unknown'}, not rejected"
        )
    proposal_id = str(existing.get("id", "")).strip()
    source_url = str(existing.get("source_url", "")).strip()
    normalized = str(existing.get("normalized_source", "")).strip()
    proposal_date_text = str(existing.get("proposal_date", "")).strip()
    decision_reason = str(existing.get("decision_reason", "")).strip()
    if not proposal_id or not source_url or not normalized:
        raise ProposalReconsiderationError(
            "proposal_identity_mismatch", "Source Proposal identity is missing id, source_url, or normalized_source"
        )
    if normalized != normalize_source_identity(source_url):
        raise ProposalReconsiderationError(
            "proposal_identity_mismatch", "Source Proposal normalized URL is inconsistent"
        )
    if not proposal_date_text or not decision_reason:
        raise ProposalReconsiderationError(
            "proposal_identity_mismatch", "Source Proposal rejection date or reason is missing"
        )
    proposal_day = _parse_iso_date(proposal_date_text, label="proposal_date", reason_code="proposal_identity_mismatch")

    evidence_url = str(fresh_evidence.get("source_url", "")).strip()
    outcome = str(fresh_evidence.get("outcome", "")).strip()
    probe_date_text = str(fresh_evidence.get("probe_date", "")).strip()
    probe_day = _parse_iso_date(probe_date_text, label="probe_date", reason_code="evidence_stale")
    _check_evidence_admission(evidence_url, outcome, probe_day, proposal_day, proposal_url=source_url)
    evidence_path, evidence_digest = _require_evidence_identity(fresh_evidence)

    # Metadata is normalized here but validated once, by the shared payload
    # validator below, so preview and confirmation can never differ.
    reason = reconsideration_reason.strip() if isinstance(reconsideration_reason, str) else ""
    reconsidered_by_text = reconsidered_by.strip() if isinstance(reconsidered_by, str) else ""
    reconsideration_day = _parse_iso_date(
        reconsideration_date, label="reconsideration_date", reason_code="reconsideration_date_mismatch"
    )

    payload: dict[str, Any] = {
        "proposal_id": proposal_id,
        "source_url": source_url,
        "normalized_source": normalized,
        "existing_status": status,
        "existing_proposal_date": proposal_date_text,
        "existing_decision_reason": decision_reason,
        "fresh_source_url": evidence_url,
        "fresh_outcome": outcome,
        "fresh_reason_code": str(fresh_evidence.get("reason_code", "")).strip(),
        "fresh_probe_date": probe_date_text,
        "fresh_bounded_entry_count": fresh_evidence.get("bounded_entry_count"),
        "fresh_detected_heading": str(fresh_evidence.get("detected_heading", "")).strip(),
        "fresh_contract_family": str(fresh_evidence.get("contract_family", "")).strip(),
        "fresh_evidence_path": evidence_path,
        "fresh_evidence_sha256": evidence_digest,
        "reconsideration_reason": reason,
        "reconsidered_by": reconsidered_by_text,
        "reconsideration_date": reconsideration_day.isoformat(),
    }
    _validate_reconsideration_payload(payload, proposal_url=source_url, proposal_day=proposal_day)
    token = reconsideration_token_for(payload)
    preview = {**payload, "confirmation_required": True, "confirmation_token": token}
    return preview, token


def confirm_proposal_reconsideration(
    existing: Mapping[str, Any],
    *,
    preview_payload: Mapping[str, Any],
    confirmation_token: str,
    confirmed_by_editor: bool,
    fresh_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply a previewed reconsideration after explicit editor confirmation.

    The input mapping is never mutated; a new proposal mapping with status
    ``proposed`` and a ``prior_decisions`` entry preserving the old rejection
    is returned. The token proves the payload is unchanged, but validity is
    re-established from scratch: the payload's admission fields and the
    current retained evidence are both re-checked, and the evidence digest
    bound at preview time must still match. Stale or incompatible inputs fail
    before any state change.
    """
    if not confirmed_by_editor:
        raise ProposalReconsiderationError(
            "confirmation_required", "Reconsideration requires explicit editor confirmation"
        )
    embedded = str(preview_payload.get("confirmation_token", "")).strip()
    payload = {
        key: value
        for key, value in dict(preview_payload).items()
        if key not in {"confirmation_token", "confirmation_required"}
    }
    expected = reconsideration_token_for(payload)
    if embedded != expected:
        raise ProposalReconsiderationError(
            "confirmation_payload_mismatch",
            "Preview payload does not match its confirmation token",
        )
    if not confirmation_token.strip() or confirmation_token.strip() != expected:
        raise ProposalReconsiderationError(
            "confirmation_token_mismatch",
            "Confirmation token does not match the previewed reconsideration payload",
        )
    if preview_payload.get("confirmation_required") is not True:
        raise ProposalReconsiderationError(
            "confirmation_payload_mismatch", "Preview payload does not require confirmation"
        )
    status = str(existing.get("status", "")).strip()
    if status != "rejected":
        raise ProposalReconsiderationError(
            "proposal_not_rejected", f"Source Proposal status is {status or 'unknown'}, not rejected"
        )
    for field in ("proposal_id", "source_url", "normalized_source"):
        payload_identity = str(payload.get(field, "")).strip()
        existing_identity = str(existing.get(field if field != "proposal_id" else "id", "")).strip()
        if not payload_identity or payload_identity != existing_identity:
            raise ProposalReconsiderationError(
                "confirmation_payload_mismatch", f"Preview {field} does not match the proposal state"
            )
    if str(existing.get("proposal_date", "")).strip() != str(payload.get("existing_proposal_date", "")).strip():
        raise ProposalReconsiderationError("confirmation_payload_mismatch", "Preview rejection date is stale")
    if str(existing.get("decision_reason", "")).strip() != str(payload.get("existing_decision_reason", "")).strip():
        raise ProposalReconsiderationError("confirmation_payload_mismatch", "Preview rejection reason is stale")

    # Re-run the complete preview validation on the payload: a matching hash
    # proves the payload is unchanged, not that it is valid.
    proposal_url = str(existing.get("source_url", "")).strip()
    proposal_day = _parse_iso_date(
        str(existing.get("proposal_date", "")).strip(),
        label="proposal_date",
        reason_code="proposal_identity_mismatch",
    )
    _validate_reconsideration_payload(payload, proposal_url=proposal_url, proposal_day=proposal_day)
    payload_evidence_url = str(payload.get("fresh_source_url", "")).strip()
    payload_probe_text = str(payload.get("fresh_probe_date", "")).strip()

    # Re-validate the current retained evidence and bind it to the preview.
    current_url = str(fresh_evidence.get("source_url", "")).strip()
    current_outcome = str(fresh_evidence.get("outcome", "")).strip()
    current_probe_text = str(fresh_evidence.get("probe_date", "")).strip()
    current_probe_day = _parse_iso_date(current_probe_text, label="probe_date", reason_code="evidence_stale")
    _check_evidence_admission(current_url, current_outcome, current_probe_day, proposal_day, proposal_url=proposal_url)
    current_path, current_digest = _require_evidence_identity(fresh_evidence)
    if (
        current_url != payload_evidence_url
        or current_probe_text.strip() != payload_probe_text.strip()
        or current_path != str(payload.get("fresh_evidence_path", "")).strip()
        or current_digest != str(payload.get("fresh_evidence_sha256", "")).strip().casefold()
        or str(fresh_evidence.get("reason_code", "")).strip() != str(payload.get("fresh_reason_code", "")).strip()
        or fresh_evidence.get("bounded_entry_count") != payload.get("fresh_bounded_entry_count")
        or str(fresh_evidence.get("detected_heading", "")).strip()
        != str(payload.get("fresh_detected_heading", "")).strip()
        or str(fresh_evidence.get("contract_family", "")).strip()
        != str(payload.get("fresh_contract_family", "")).strip()
    ):
        raise ProposalReconsiderationError(
            "evidence_changed", "Current retained evidence does not match the previewed evidence"
        )

    updated = dict(existing)
    prior_entry = {
        "status": status,
        "proposal_date": str(existing.get("proposal_date", "")).strip(),
        "decision_reason": str(existing.get("decision_reason", "")).strip(),
        "reason": str(existing.get("reason", "")).strip(),
    }
    prior = list(existing.get("prior_decisions", [])) if isinstance(existing.get("prior_decisions"), list) else []
    updated["prior_decisions"] = [*prior, prior_entry]
    updated["status"] = "proposed"
    updated["proposal_date"] = str(payload["reconsideration_date"])
    updated["reason"] = str(payload["reconsideration_reason"])
    updated["decision_reason"] = ""
    evidence_entry = {
        "reconsidered_by": str(payload["reconsidered_by"]),
        "reconsideration_date": str(payload["reconsideration_date"]),
        "fresh_outcome": str(payload["fresh_outcome"]),
        "probe_date": str(payload["fresh_probe_date"]),
        "bounded_entry_count": payload.get("fresh_bounded_entry_count"),
        "detected_heading": str(payload.get("fresh_detected_heading", "")),
        "contract_family": str(payload.get("fresh_contract_family", "")),
        "evidence_path": str(payload["fresh_evidence_path"]),
        "evidence_sha256": str(payload["fresh_evidence_sha256"]),
    }
    supporting = (
        list(existing.get("supporting_evidence", [])) if isinstance(existing.get("supporting_evidence"), list) else []
    )
    updated["supporting_evidence"] = [*supporting, evidence_entry]
    return updated
