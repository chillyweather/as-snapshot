"""Russian-edition check artifacts and review-queue helpers.

Extracted from ``autoscout.pipeline`` as part of issue #216.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from autoscout.candidate_verification import positive_russian_edition_url
from autoscout.candidate_verification import verified_positive_russian_evidence
from autoscout.russian_editions import configured_russian_checks
from autoscout.russian_editions import is_valid_russian_edition_verification
from autoscout.russian_editions import russian_edition_check_runs
from autoscout.russian_editions import russian_query_attempts
from autoscout.russian_status import FOUND_IN_RUSSIAN
from autoscout.russian_status import UNCLEAR
from autoscout.russian_status import is_found_in_russian
from autoscout.russian_status import is_unclear
from autoscout.scoring import source_evidence
from autoscout.scoring import work_identity_key
from autoscout.scouting_candidates import ScoutingCandidate

RUSSIAN_REVIEW_QUEUE_FIELDNAMES = [
    "Check date",
    "Identity key",
    "Title",
    "Author",
    "Attempted queries",
    "Possible match URL",
    "Review reason",
]


def has_russian_verification(checks: list[dict[str, Any]], records: Sequence[dict[str, Any]]) -> bool:
    """Return whether Russian verification data should be published."""
    return bool(configured_russian_checks(checks)) or any(
        isinstance(record, dict) and is_valid_russian_edition_verification(record.get("russian_edition_verification"))
        for record in records
    )


def russian_edition_check_artifact(
    checks: list[dict[str, Any]],
    source_mentions: list[dict[str, Any]],
    scouting_candidates: list[dict[str, Any]],
    internal_exclusions: list[dict[str, Any]],
    run_date: str,
) -> dict[str, Any]:
    candidate_checks = russian_candidate_check_artifacts(
        checks,
        source_mentions,
        scouting_candidates,
        internal_exclusions,
        run_date,
    )
    artifact: dict[str, Any] = {
        "retailer_runs": russian_edition_check_runs(checks),
        "candidate_checks": candidate_checks,
    }
    if configured_russian_checks(checks) or candidate_checks:
        artifact["verification_breakdown"] = russian_verification_counts(candidate_checks)
    return artifact


def russian_candidate_check_artifacts(
    checks: list[dict[str, Any]],
    source_mentions: list[dict[str, Any]],
    scouting_candidates: list[dict[str, Any]],
    internal_exclusions: list[dict[str, Any]],
    run_date: str,
) -> list[dict[str, Any]]:
    resolved = {str(candidate.get("identity_key", "")): candidate for candidate in scouting_candidates}
    resolved.update(
        {
            str(exclusion.get("identity_key", "")): exclusion
            for exclusion in internal_exclusions
            if isinstance(exclusion, dict)
            and (
                is_found_in_russian(exclusion.get("exclusion_reason", ""))
                or bool(exclusion.get("russian_edition_verification"))
            )
        }
    )
    if not configured_russian_checks(checks) and not any(
        isinstance(record, dict) and bool(record.get("russian_edition_verification")) for record in resolved.values()
    ):
        return []

    artifacts = []
    seen = set()
    for mention in source_mentions:
        identity_key = work_identity_key(mention)
        if identity_key in seen:
            continue
        seen.add(identity_key)
        record = resolved.get(identity_key)
        if record is None:
            continue
        status = str(
            record.get("russian_edition_status")
            or (FOUND_IN_RUSSIAN if is_found_in_russian(record.get("exclusion_reason", "")) else "")
        )
        verification = record.get("russian_edition_verification")
        evidence = list(record.get("russian_edition_evidence") or record.get("evidence") or [])
        evidence_url = str(record.get("russian_edition_url", ""))
        if is_found_in_russian(status):
            positive_evidence = verified_positive_russian_evidence(verification)
            if positive_evidence:
                evidence = positive_evidence
                evidence_url = positive_russian_edition_url(verification)
        if not evidence_url and evidence and not is_found_in_russian(status):
            evidence_url = str(evidence[0].get("url", ""))
        decision_reason = russian_decision_reason(status, record)
        artifact = {
            "identity_key": identity_key,
            "title": mention.get("title", ""),
            "primary_authors": mention.get("primary_authors", []),
            "check_date": run_date,
            "resolved_status": status or UNCLEAR,
            "evidence_url": evidence_url,
            "decision_reason": decision_reason,
            "query_attempts": russian_query_attempts(mention),
            "evidence": evidence,
            "source_evidence": [source_evidence(mention)],
        }
        if is_valid_russian_edition_verification(verification):
            artifact["russian_edition_verification"] = verification
        artifacts.append(artifact)
    return artifacts


def russian_decision_reason(status: str, record: dict[str, Any]) -> str:
    evidence = list(record.get("russian_edition_evidence") or record.get("evidence") or [])
    source_type = str(evidence[0].get("source_type", "")) if evidence else ""
    if is_found_in_russian(status):
        if source_type == "russian-library-catalog":
            return "same-work Russian-language bibliographic record found in a configured library catalog"
        return "same-work Russian-language edition found on a configured retailer"
    verification = record.get("russian_edition_verification")
    if isinstance(verification, dict):
        coverage = str(verification.get("check_coverage", "")).strip()
        outcome = str(verification.get("provider_outcome", "")).strip()
        if coverage == "complete" and outcome == "no_match":
            return "configured Russian providers completed successfully with no credible match"
        if outcome in {"blocked", "failed", "unimplemented", "not_run"} or coverage != "complete":
            return "Russian-edition verification is incomplete"
        if outcome == "ambiguous":
            return "Russian-edition verification found ambiguous identity evidence"
    if is_unclear(status):
        return "Russian-edition verification is incomplete or ambiguous"
    return "configured Russian providers found no credible match"


def write_russian_review_queue(path: Path, candidates: Sequence[dict[str, Any] | ScoutingCandidate]) -> None:
    rows = [
        russian_review_queue_row(candidate)
        for candidate in candidates
        if is_unclear(candidate.get("russian_edition_status", ""))
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RUSSIAN_REVIEW_QUEUE_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def russian_review_queue_row(candidate: dict[str, Any] | ScoutingCandidate) -> dict[str, str]:
    candidate_data = ScoutingCandidate.from_mapping(candidate).data
    evidence = candidate_data.get("russian_edition_evidence") or []
    possible_url = ""
    if evidence:
        possible_url = str(evidence[0].get("url", ""))
    return {
        "Check date": str((candidate_data.get("primary_signal") or {}).get("source_check_date", "")),
        "Identity key": str(candidate_data.get("identity_key", "")),
        "Title": str(candidate_data.get("title", "")),
        "Author": ", ".join(candidate_data.get("primary_authors", [])),
        "Attempted queries": " | ".join(attempt["query"] for attempt in russian_query_attempts(candidate_data)),
        "Possible match URL": possible_url,
        "Review reason": str(candidate_data.get("russian_edition_note", "")),
    }


def russian_check_unclear_count(candidates: list[dict[str, Any]]) -> int:
    return sum(1 for candidate in candidates if is_unclear(candidate.get("russian_edition_status", "")))


def russian_verification_counts(candidates: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "verified_russian_edition": 0,
        "verified_complete_no_match": 0,
        "ambiguous": 0,
        "partial": 0,
        "not_run": 0,
        "blocked": 0,
        "failed": 0,
        "unimplemented": 0,
    }
    for candidate in candidates:
        verification = candidate.get("russian_edition_verification")
        if not isinstance(verification, dict) or not is_valid_russian_edition_verification(verification):
            continue
        coverage = str(verification.get("check_coverage", "")).strip()
        outcome = str(verification.get("provider_outcome", "")).strip()
        status = (
            candidate.get("russian_edition_status")
            or candidate.get("resolved_status")
            or (FOUND_IN_RUSSIAN if is_found_in_russian(candidate.get("exclusion_reason", "")) else "")
        )
        if outcome == "matched" and is_found_in_russian(status):
            counts["verified_russian_edition"] += 1
        if coverage == "complete" and outcome == "no_match":
            counts["verified_complete_no_match"] += 1
        if outcome == "ambiguous":
            counts["ambiguous"] += 1
        if coverage == "partial":
            counts["partial"] += 1
        if coverage == "not_run" or outcome == "not_run":
            counts["not_run"] += 1
        if outcome == "blocked":
            counts["blocked"] += 1
        if outcome == "failed":
            counts["failed"] += 1
        if outcome == "unimplemented":
            counts["unimplemented"] += 1
    return counts
