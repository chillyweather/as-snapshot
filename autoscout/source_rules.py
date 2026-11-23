"""Centralized source eligibility rules — single authoritative implementation.

Constants and predicate functions shared across the codebase for
determining whether a configured source is active, skipped, eligible
for discovery, or whether a candidate can produce a Source Mention.
"""

from __future__ import annotations

from typing import Any

SKIPPED_ACCESS_STATUSES = {
    "paid",
    "subscription required",
    "paid/subscription required",
    "blocked",
    "fragile",
    "unavailable",
    "manual import",
    "unsuitable",
}

DISCOVERY_SOURCE_FAMILIES = {
    "award",
    "bestseller/ranking",
    "media/curated list",
    "reader-interest",
    "audiobook ranking",
    "rights/deal",
}

# A configured source can end up inactive for two unrelated reasons: the
# operator disabled it, or the workflow withheld it because an artifact it
# depends on never arrived. Only the second is a source problem, so the
# withholding reason is recorded as a machine key rather than free text.
EXTRACTION_ARTIFACT_MISSING = "extraction-artifact-missing"
# An artifact that arrived and was refused is a different operational event
# from one that never arrived: the source is reachable, the extraction ran,
# and grounded work was discarded. Reporting the second as the first sends the
# operator to the capture stage or the source website instead of to the
# artifact (Issue #386).
EXTRACTION_ARTIFACT_REJECTED = "extraction-artifact-rejected"
# An absolute execution safety stop is operational capacity evidence, not a
# source-health failure, so it is intentionally not a withheld failure reason.
EXTRACTION_ARTIFACT_SAFETY_STOP = "extraction-artifact-safety-stop"
PRECOLLECTED_ARTIFACT_UNAVAILABLE = "precollected-artifact-unavailable"

WITHHELD_REASON_MESSAGES = {
    EXTRACTION_ARTIFACT_MISSING: "Codex extraction produced no artifact for this configured source",
    EXTRACTION_ARTIFACT_REJECTED: "Codex extraction produced an artifact that failed validation for this configured source",
    PRECOLLECTED_ARTIFACT_UNAVAILABLE: "precollected metadata artifact is unavailable for this configured source",
}


# A paid source whose contract reads the member's own copy of the paid content (Issue #945): the
# Publishers Marketplace deals reach AutoScout as the membership's emails, never through the paid site,
# and the Publishers Weekly issues as the subscriber's digital edition (#955).
MEMBER_ACCESS_CONTRACT_FAMILIES = frozenset({"pm-deal-email", "pw-issue-pdf"})


def is_skipped_source(source: dict[str, Any]) -> bool:
    """Return True when the source's access status means the pipeline
    should skip it entirely."""
    status = str(source.get("access_status", "")).strip().lower()
    if (
        status == "paid"
        and str(source.get("contract_family", "")).strip().casefold() in MEMBER_ACCESS_CONTRACT_FAMILIES
    ):
        return False
    return status in SKIPPED_ACCESS_STATUSES


def is_active_source(source: dict[str, Any]) -> bool:
    """Return True unless the source has been explicitly disabled."""
    return source.get("active", True) is not False


def withheld_reason(source: dict[str, Any]) -> str | None:
    """Return the machine reason when the workflow withheld this source.

    Returns None for a source the operator disabled deliberately, so callers
    can report a withheld source as a source problem while an operator-disabled
    source stays a quiet, expected exclusion."""
    reason = str(source.get("inactive_reason", "")).strip()
    if reason in WITHHELD_REASON_MESSAGES:
        return reason
    return None


def withheld_reason_message(source: dict[str, Any]) -> str:
    """Return the editor-facing reason for a withheld source, or an empty string.

    A withholding reason states the category. ``withheld_detail`` carries the
    specific evidence for it when the workflow knows more, such as the
    validation class that refused an artifact and how many extracted mentions
    that refusal discarded (Issue #386)."""
    reason = withheld_reason(source)
    if reason is None:
        return ""
    message = WITHHELD_REASON_MESSAGES[reason]
    detail = str(source.get("withheld_detail", "")).strip()
    if detail:
        return f"{message} ({detail})"
    return message


def is_discovery_source(source: dict[str, Any]) -> bool:
    """Return True when the source belongs to a family that the pipeline
    treats as a discovery source (eligible to generate mentions)."""
    return str(source.get("family", "")).strip().lower() in DISCOVERY_SOURCE_FAMILIES


def can_create_source_mention(source: dict[str, Any], candidate: dict[str, Any]) -> bool:
    """Decide whether *candidate* qualifies for a Source Mention record.

    Audiobook-ranking sources are only allowed to create mentions for
    underlying works that are actually books / written works."""
    family = str(source.get("family", "")).strip().lower()
    if family != "audiobook ranking":
        return True
    return str(candidate.get("underlying_work_type", "")).strip().lower() in {"book", "work"}


def skipped_reason(source: dict[str, Any]) -> str:
    """Human-readable reason why a source was skipped."""
    status = source.get("access_status", "unsuitable")
    notes = source.get("notes", "")
    if notes:
        return f"{status}: {notes}"
    return str(status)
