"""Deterministic merging for editor-internal exclusion records."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from typing import Any

from autoscout.candidate_verification import verified_positive_russian_evidence
from autoscout.russian_editions import is_valid_russian_edition_verification
from autoscout.russian_status import is_found_in_russian

_RUSSIAN_VERIFICATION_HISTORY_FIELD = "russian_edition_verification_history"


def _canonical_record(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)


def _exclusion_record_key(record: dict[str, Any]) -> tuple[int, int, int, int, str]:
    raw_date = str(record.get("exclusion_date", "")).strip()
    try:
        date_value = date.fromisoformat(raw_date).toordinal()
    except ValueError:
        date_value = 0
    evidence = record.get("evidence", [])
    evidence_count = len(evidence) if isinstance(evidence, list) else int(bool(evidence))
    return (
        int(is_found_in_russian(record.get("exclusion_reason", ""))),
        date_value,
        int(bool(str(record.get("russian_edition_url", "")).strip())),
        evidence_count,
        _canonical_record(record),
    )


def _is_verified_positive(verification: Any) -> bool:
    return (
        isinstance(verification, dict)
        and is_valid_russian_edition_verification(verification)
        and verification.get("provider_outcome") == "matched"
        and bool(verified_positive_russian_evidence(verification))
    )


def _verification_record_key(verification: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(verification.get("checked_at", "")).strip(),
        str(verification.get("query_timestamp", "")).strip(),
        _canonical_record(verification),
    )


def merge_exclusion_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge records for one identity without allowing input order to decide facts."""
    valid_records = [record for record in records if isinstance(record, dict)]
    if not valid_records:
        return {}
    found_records = [record for record in valid_records if is_found_in_russian(record.get("exclusion_reason", ""))]
    strongest_records = found_records or valid_records
    strongest = max(strongest_records, key=_exclusion_record_key)
    merged = deepcopy(strongest)

    # Found in Russian is the strongest acquisition transition. Preserve the
    # best matching URL and all distinct evidence from that transition even if
    # a later, weaker exclusion record was supplied first.
    url_records = [record for record in strongest_records if str(record.get("russian_edition_url", "")).strip()]
    if url_records:
        merged["russian_edition_url"] = str(max(url_records, key=_exclusion_record_key)["russian_edition_url"])
    evidence_records: dict[str, dict[str, Any]] = {}
    for record in strongest_records:
        evidence = record.get("evidence", [])
        if isinstance(evidence, dict):
            evidence = [evidence]
        if not isinstance(evidence, list):
            continue
        for item in evidence:
            if isinstance(item, dict):
                evidence_records[_canonical_record(item)] = deepcopy(item)
    if evidence_records:
        merged["evidence"] = [evidence_records[key] for key in sorted(evidence_records)]

    # An incomplete later check is stored as the current verification, but a
    # prior verified positive must remain available for the next restoration.
    current_verification = merged.get("russian_edition_verification")
    if _is_verified_positive(current_verification):
        merged.pop(_RUSSIAN_VERIFICATION_HISTORY_FIELD, None)
        return merged

    verification_history: dict[str, dict[str, Any]] = {}
    for record in strongest_records:
        candidates = []
        verification = record.get("russian_edition_verification")
        if isinstance(verification, dict):
            candidates.append(verification)
        history = record.get(_RUSSIAN_VERIFICATION_HISTORY_FIELD, [])
        if isinstance(history, dict):
            history = [history]
        if isinstance(history, list):
            candidates.extend(item for item in history if isinstance(item, dict))
        for verification in candidates:
            if _is_verified_positive(verification):
                verification_history[_canonical_record(verification)] = deepcopy(verification)
    if verification_history:
        retained = max(verification_history.values(), key=_verification_record_key)
        merged[_RUSSIAN_VERIFICATION_HISTORY_FIELD] = [retained]
    else:
        merged.pop(_RUSSIAN_VERIFICATION_HISTORY_FIELD, None)

    return merged
