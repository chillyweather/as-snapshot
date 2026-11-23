"""Thin compatibility delegates for Scouting Candidate Visible Risk behaviour."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from autoscout.scouting_candidates import RISK_KEYWORDS  # noqa: F401
from autoscout.scouting_candidates import ScoutingCandidate


def apply_visible_risk_screening(candidates: Sequence[dict[str, Any] | ScoutingCandidate]) -> None:
    for candidate in candidates:
        ScoutingCandidate.from_mapping(candidate).apply_visible_risk_screening()


def visible_risk_category_for(candidate: dict[str, Any]) -> str:
    return ScoutingCandidate.from_mapping(candidate).visible_risk_category()


def is_weak_author_context_signal(candidate: dict[str, Any]) -> bool:
    return ScoutingCandidate.from_mapping(candidate).is_weak_author_context_signal()


def is_credible_book_content_signal(candidate: dict[str, Any]) -> bool:
    return ScoutingCandidate.from_mapping(candidate).is_credible_book_content_signal()
