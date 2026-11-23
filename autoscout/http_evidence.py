"""Structured HTTP status evidence for source feasibility."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


class HttpEvidencePolicy(str, Enum):
    """The request policy that produced the status evidence."""

    GET_WITH_HEAD = "get-with-head"
    DECLARED_POST_WITHOUT_HEAD = "declared-post-without-head"


@dataclass(frozen=True)
class HttpStatusEvidence:
    """HTTP statuses together with the request policy that produced them."""

    head_status: int | None
    body_status: int | None
    method: str
    policy: HttpEvidencePolicy
    allow_head_404: bool = False

    @classmethod
    def get_with_head(
        cls, *, head_status: int | None, body_status: int | None, allow_head_404: bool = False
    ) -> HttpStatusEvidence:
        return cls(head_status, body_status, "GET", HttpEvidencePolicy.GET_WITH_HEAD, allow_head_404)

    @classmethod
    def declared_post(cls, *, body_status: int | None) -> HttpStatusEvidence:
        return cls(None, body_status, "POST", HttpEvidencePolicy.DECLARED_POST_WITHOUT_HEAD)


def declared_head_404_fallback(source: Mapping[str, Any]) -> bool:
    """Only an explicit ranking declaration permits the observed HEAD-404 exception."""
    return (
        source.get("ranking_head_404_get_fallback") is True
        and (
            source.get("metadata_ranking_table") is True
            or (source.get("contract_family") == "ranking-table" and "metadata_ranking_table" not in source)
        )
        and source.get("contract_family", "ranking-table") == "ranking-table"
        and str(source.get("ranking_request_method", "GET")).upper() == "GET"
    )


def validate_head_404_fallback_declaration(source: Mapping[str, Any]) -> str:
    value = source.get("ranking_head_404_get_fallback", False)
    if not isinstance(value, bool):
        return "ranking_head_404_get_fallback must be a boolean"
    if value and not declared_head_404_fallback(source):
        return "HEAD 404 fallback requires a declared ranking-table GET source"
    return ""
