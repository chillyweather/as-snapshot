"""Thin compatibility delegates for Scouting Candidate behaviour."""

from __future__ import annotations

from typing import Any

from autoscout.scouting_candidates import INELIGIBLE_CANDIDATE_CATEGORIES  # noqa: F401
from autoscout.scouting_candidates import SIGNAL_STRENGTH_RANKS  # noqa: F401
from autoscout.scouting_candidates import ScoutingCandidate


def exclusion_reason_for(mention: dict[str, Any]) -> str:
    return ScoutingCandidate.exclusion_reason_for_mapping(mention)


def work_identity_key(mention: dict[str, Any]) -> str:
    return ScoutingCandidate.identity_key_for_mention(mention)


def source_evidence(mention: dict[str, Any]) -> dict[str, Any]:
    return ScoutingCandidate.source_evidence_from_mention(mention)


def merge_candidate_status(candidate: dict[str, Any], mention: dict[str, Any]) -> None:
    ScoutingCandidate.from_mapping(candidate).merge_source_mention(mention)


def russian_search_notes(mention: dict[str, Any]) -> dict[str, str]:
    return ScoutingCandidate.russian_search_notes_from_mention(mention)


def add_signal_aggregation(candidate: dict[str, Any]) -> None:
    ScoutingCandidate.from_mapping(candidate).apply_signal_aggregation()


def primary_signal(candidate: dict[str, Any]) -> dict[str, Any]:
    return ScoutingCandidate.from_mapping(candidate).primary_signal()


def signal_strength_rank(evidence: dict[str, Any]) -> int:
    return ScoutingCandidate.signal_strength_rank(evidence)


def priority_label(candidate: dict[str, Any]) -> str:
    return ScoutingCandidate.from_mapping(candidate).priority_label()


def candidate_sort_key(candidate: dict[str, Any]) -> tuple[int, int, int]:
    return ScoutingCandidate.from_mapping(candidate).sort_key()


def signal_locator_text(candidate: dict[str, Any]) -> str:
    return ScoutingCandidate.from_mapping(candidate).signal_locator_text()
