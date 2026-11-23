"""Pure candidate policy for Russian-edition verification.

This module owns the candidate-level policy that turns operational Russian
provider evidence into the editorial Russian-edition fields.
It does not collect data and does not depend on :class:`ScoutingCandidate`.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from autoscout.russian_editions import is_forthcoming
from autoscout.russian_editions import is_russian_language_clue
from autoscout.russian_editions import is_valid_russian_edition_verification
from autoscout.russian_editions import validate_russian_edition_verification
from autoscout.russian_status import FOUND_IN_RUSSIAN
from autoscout.russian_status import NOT_FOUND_IN_RUSSIAN
from autoscout.russian_status import UNCLEAR
from autoscout.russian_status import is_found_in_russian
from autoscout.russian_status import is_not_found_in_russian
from autoscout.russian_status import is_unclear

_VERIFIED_MATCH_TYPE = "same-work russian-language edition"


def _direct_evidence_url(evidence: dict[str, Any]) -> str:
    return str(evidence.get("url") or evidence.get("edition_url", "")).strip()


def _is_verified_positive_evidence(evidence: Any) -> bool:
    if not isinstance(evidence, dict):
        return False
    match_type = str(evidence.get("match_type", "")).strip().lower()
    language = str(evidence.get("language") or evidence.get("language_clue", "")).strip()
    return (
        match_type == _VERIFIED_MATCH_TYPE
        and is_russian_language_clue(language)
        and bool(_direct_evidence_url(evidence))
    )


def has_valid_russian_edition_evidence(candidate: dict[str, Any]) -> bool:
    evidence = candidate.get("russian_edition_evidence", [])
    return isinstance(evidence, list) and any(_is_verified_positive_evidence(item) for item in evidence)


def _provider_is_matched(provider: dict[str, Any]) -> bool:
    return (
        str(provider.get("status", "")).strip().lower() == "checked"
        and str(provider.get("outcome", "")).strip().lower() == "matched"
    )


def verified_positive_russian_evidence(verification: Any) -> list[dict[str, Any]]:
    """Return only verified direct evidence from matched providers.

    Aggregate ``evidence_urls`` is deliberately not used here.
    """
    if not isinstance(verification, dict):
        return []
    providers = verification.get("providers", [])
    if not isinstance(providers, list):
        return []
    evidence: list[dict[str, Any]] = []
    for provider in providers:
        if not isinstance(provider, dict) or not _provider_is_matched(provider):
            continue
        provider_evidence = provider.get("evidence", [])
        if not isinstance(provider_evidence, list):
            continue
        evidence.extend(deepcopy(item) for item in provider_evidence if _is_verified_positive_evidence(item))
    return evidence


def positive_russian_edition_url(verification: Any) -> str:
    """Return the first direct URL from a matched provider's verified evidence."""
    for evidence in verified_positive_russian_evidence(verification):
        url = _direct_evidence_url(evidence)
        if url:
            return url
    return ""


def has_verified_positive_russian_state(candidate: dict[str, Any]) -> bool:
    verification = candidate.get("russian_edition_verification")
    if not isinstance(verification, dict) or not is_valid_russian_edition_verification(verification):
        return False
    return (
        is_found_in_russian(candidate.get("russian_edition_status", ""))
        and verification.get("provider_outcome") == "matched"
        and bool(verified_positive_russian_evidence(verification))
    )


def has_valid_found_in_russian_status(candidate: dict[str, Any]) -> bool:
    return is_found_in_russian(candidate.get("russian_edition_status", "")) and (
        has_valid_russian_edition_evidence(candidate) or has_verified_positive_russian_state(candidate)
    )


def materialize_verified_russian_evidence(candidate: dict[str, Any]) -> None:
    """Copy matched-provider evidence into the candidate before a replacement."""
    verification = candidate.get("russian_edition_verification")
    if not has_verified_positive_russian_state(candidate) or has_valid_russian_edition_evidence(candidate):
        return
    evidence = verified_positive_russian_evidence(verification)
    if evidence:
        candidate["russian_edition_evidence"] = evidence
        candidate["russian_edition_url"] = positive_russian_edition_url(verification)


def restore_russian_edition_from_exclusion(candidate: dict[str, Any], exclusion: dict[str, Any]) -> None:
    """Restore a found-in-Russian exclusion from verified provider evidence."""
    if not is_found_in_russian(exclusion.get("exclusion_reason", "")):
        return
    verifications: list[Any] = [exclusion.get("russian_edition_verification")]
    verification_history = exclusion.get("russian_edition_verification_history", [])
    if isinstance(verification_history, dict):
        verification_history = [verification_history]
    if isinstance(verification_history, list):
        verifications.extend(verification_history)
    for verification in verifications:
        if not is_valid_russian_edition_verification(verification):
            continue
        evidence = verified_positive_russian_evidence(verification)
        if not evidence:
            continue
        candidate["russian_edition_status"] = FOUND_IN_RUSSIAN
        candidate["russian_edition_url"] = _direct_evidence_url(evidence[0])
        candidate["russian_edition_evidence"] = evidence
        candidate["russian_edition_verification"] = deepcopy(verification)
        break
    else:
        return
    if "russian_edition_note" in exclusion:
        candidate["russian_edition_note"] = deepcopy(exclusion["russian_edition_note"])


def _provider_evidence(verification: dict[str, Any]) -> list[dict[str, Any]]:
    providers = verification.get("providers", [])
    if not isinstance(providers, list):
        return []
    evidence: list[dict[str, Any]] = []
    for provider in providers:
        if not isinstance(provider, dict) or provider.get("outcome") not in {
            "matched",
            "ambiguous",
            "no_match",
        }:
            continue
        values = provider.get("evidence", [])
        if isinstance(values, list):
            evidence.extend(deepcopy(item) for item in values if isinstance(item, dict))
    return evidence


def apply_russian_edition_verification(candidate: dict[str, Any], verification: dict[str, Any]) -> None:
    """Apply operational verification while preserving valid prior positives."""
    validate_russian_edition_verification(verification)
    preserve_positive = has_valid_found_in_russian_status(candidate)
    if preserve_positive:
        materialize_verified_russian_evidence(candidate)

    candidate["russian_edition_verification"] = deepcopy(verification)
    outcome = verification.get("provider_outcome")
    coverage = str(verification.get("check_coverage", "")).strip()
    evidence = _provider_evidence(verification)
    matched_evidence = verified_positive_russian_evidence(verification)

    if outcome == "matched" and matched_evidence:
        candidate["russian_edition_status"] = FOUND_IN_RUSSIAN
        candidate["russian_edition_url"] = _direct_evidence_url(matched_evidence[0])
        candidate["russian_edition_evidence"] = matched_evidence
        candidate["russian_edition_note"] = ""
        return
    if outcome == "no_match" and coverage == "complete":
        candidate["russian_edition_status"] = NOT_FOUND_IN_RUSSIAN
        candidate["russian_edition_url"] = ""
        candidate["russian_edition_evidence"] = evidence
        if is_forthcoming(candidate):
            candidate["russian_edition_note"] = (
                "Configured retailer checks found no Russian edition or preorder; original publication is forthcoming."
            )
        elif verification.get("providers") and all(
            isinstance(provider, dict)
            and provider.get("outcome") == "no_match"
            and "imported foreign-language listing" in str(provider.get("reason", ""))
            for provider in verification["providers"]
        ):
            candidate["russian_edition_note"] = (
                "Configured retailer checks found only imported foreign-language listings, "
                "not Russian-language editions."
            )
        else:
            candidate["russian_edition_note"] = "Configured Russian retailer checks found no credible match."
        return
    if preserve_positive:
        return

    candidate["russian_edition_status"] = UNCLEAR
    candidate["russian_edition_url"] = ""
    candidate["russian_edition_evidence"] = evidence
    reason = str(verification.get("reason", "")).strip()
    if outcome == "ambiguous":
        candidate["russian_edition_note"] = (
            "Configured retailer checks found ambiguous Russian-language matches; same-work identity needs review."
        )
    else:
        candidate["russian_edition_note"] = (
            reason or "Configured Russian-edition verification is incomplete or ambiguous."
        )


def russian_not_run_verification(run_date: str) -> dict[str, Any]:
    verification: dict[str, Any] = {
        "check_coverage": "not_run",
        "provider_outcome": None,
        "confidence": "low",
        "checked_sources": [],
        "checked_at": run_date,
        "query_timestamp": run_date,
        "evidence_urls": [],
        "providers": [],
        "reason": "No recognized Russian-edition providers were configured",
    }
    validate_russian_edition_verification(verification)
    return verification


def is_unrun_provider_verification(verification: Any) -> bool:
    """True for a verification whose providers were all declared but none ran.

    Such a record says nothing about the book: no provider was checked, none
    returned an outcome and none left evidence. It is what an empty retailer
    fixture produced (#1181).
    """
    if not is_valid_russian_edition_verification(verification):
        return False
    providers = verification.get("providers")
    return (
        verification.get("provider_outcome") is None
        and not verification.get("checked_sources")
        and not verification.get("evidence_urls")
        and isinstance(providers, list)
        and bool(providers)
        and all(
            isinstance(provider, dict)
            and provider.get("status") == "not_run"
            and provider.get("outcome") is None
            and not provider.get("evidence")
            and not provider.get("evidence_urls")
            for provider in providers
        )
    )


def replace_unrun_provider_verification(candidate: dict[str, Any], run_date: str) -> None:
    """Replace an inherited verification in which no provider ran, and its note (#1181).

    With no Russian-edition checks configured, such a verification copied from
    Candidate History would repeat a previous run's notes about checks that no
    longer exist. It gives way to this run's own record that no provider was
    configured, with an empty note, so Candidate History is updated too. A
    verified positive is never touched.
    """
    if not is_unrun_provider_verification(candidate.get("russian_edition_verification")):
        return
    if is_found_in_russian(candidate.get("russian_edition_status", "")):
        return
    candidate["russian_edition_verification"] = russian_not_run_verification(run_date)
    candidate["russian_edition_note"] = ""


def normalize_legacy_russian_status(candidate: dict[str, Any], run_date: str = "") -> None:
    """Remove unverified legacy status claims without touching editor state."""
    verification = candidate.get("russian_edition_verification")
    if verification is not None and not is_valid_russian_edition_verification(verification):
        candidate.pop("russian_edition_verification", None)
        verification = None

    evidence = candidate.get("russian_edition_evidence", [])
    evidence_records = evidence if isinstance(evidence, list) else []
    if is_found_in_russian(candidate.get("russian_edition_status", "")) and not has_valid_found_in_russian_status(
        candidate
    ):
        for field in (
            "russian_edition_status",
            "russian_edition_url",
            "russian_edition_evidence",
            "russian_edition_note",
        ):
            candidate.pop(field, None)
        return
    if not is_not_found_in_russian(candidate.get("russian_edition_status", "")):
        return

    has_complete_no_match = (
        isinstance(verification, dict)
        and is_valid_russian_edition_verification(verification)
        and verification.get("check_coverage") == "complete"
        and verification.get("provider_outcome") == "no_match"
        and bool(str(verification.get("checked_at") or verification.get("query_timestamp", "")).strip())
        and isinstance(verification.get("providers"), list)
        and bool(verification["providers"])
        and all(
            isinstance(provider, dict) and provider.get("status") == "checked" and provider.get("outcome") == "no_match"
            for provider in verification["providers"]
        )
    )
    if has_complete_no_match:
        return

    check_date = str(run_date).strip()
    if not check_date and isinstance(verification, dict):
        check_date = str(verification.get("checked_at") or verification.get("query_timestamp", "")).strip()
    if not check_date:
        for evidence_item in evidence_records:
            if isinstance(evidence_item, dict):
                check_date = str(evidence_item.get("check_date", "")).strip()
                if check_date:
                    break
    if not check_date:
        primary_signal = candidate.get("primary_signal")
        if isinstance(primary_signal, dict):
            check_date = str(primary_signal.get("source_check_date", "")).strip()
    if not check_date:
        for source_evidence in candidate.get("source_evidence", []):
            if isinstance(source_evidence, dict):
                check_date = str(source_evidence.get("source_check_date", "")).strip()
                if check_date:
                    break
    candidate["russian_edition_verification"] = russian_not_run_verification(check_date)
    candidate["russian_edition_status"] = UNCLEAR
    candidate["russian_edition_url"] = ""
    candidate["russian_edition_note"] = "Russian-edition verification was not run by a recognized provider."


def apply_reentry_note(candidate: dict[str, Any], found_in_russian_history: dict[str, dict[str, Any]]) -> None:
    if candidate.get("identity_key") not in found_in_russian_history:
        return
    status = str(candidate.get("russian_edition_status", "")).strip()
    if is_unclear(status) or is_not_found_in_russian(status):
        candidate["reentry_reason"] = f"Russian status changed from {FOUND_IN_RUSSIAN} to {status}"


def is_non_definitive_russian_normalization(candidate: dict[str, Any], history: dict[str, Any]) -> bool:
    verification = candidate.get("russian_edition_verification")
    if not (
        is_not_found_in_russian(history.get("russian_edition_status", ""))
        and is_unclear(candidate.get("russian_edition_status", ""))
        and isinstance(verification, dict)
        and verification
    ):
        return False
    return str(verification.get("check_coverage", "")).strip().lower() in {"not_run", "partial", "failed"} or str(
        verification.get("provider_outcome")
    ).strip().lower() in {"blocked", "unimplemented"}
