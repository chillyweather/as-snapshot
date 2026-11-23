"""Closed artifact-freshness domain values and stale-artifact policy."""

from __future__ import annotations

from typing import Final
from typing import Literal
from typing import TypeGuard
from typing import assert_never

ArtifactFreshness = Literal["fresh", "reused", "unbound"]

__all__ = [
    "ALL_FRESHNESS_VALUES",
    "FRESH",
    "REUSED",
    "UNBOUND",
    "ArtifactFreshness",
    "is_artifact_freshness",
    "is_fresh",
    "is_stale_artifact_freshness",
    "is_unbound",
    "normalized_artifact_freshness",
]

FRESH: Final[ArtifactFreshness] = "fresh"
REUSED: Final[ArtifactFreshness] = "reused"
UNBOUND: Final[ArtifactFreshness] = "unbound"
ALL_FRESHNESS_VALUES: Final[tuple[ArtifactFreshness, ...]] = (FRESH, REUSED, UNBOUND)


def is_artifact_freshness(value: object) -> TypeGuard[ArtifactFreshness]:
    """Return whether a raw value is one of the closed freshness states."""
    return value in ALL_FRESHNESS_VALUES


def normalized_artifact_freshness(value: object) -> ArtifactFreshness | None:
    """Return a normalized closed freshness state for a raw field value."""
    normalized = value.strip() if isinstance(value, str) else value
    return normalized if is_artifact_freshness(normalized) else None


def is_stale_artifact_freshness(value: ArtifactFreshness) -> bool:
    """Return whether freshness represents the Issue #384 stale-artifact fault.

    A future validated cache-hit state must be added to this decision as its
    own explicit non-fault branch.
    """
    match value:
        case "reused":
            return True
        case "fresh" | "unbound":
            return False
        case _:
            return assert_never(value)


def is_fresh(value: ArtifactFreshness) -> bool:
    """Return whether an artifact was bound to the current operation."""
    return value == FRESH


def is_unbound(value: ArtifactFreshness) -> bool:
    """Return whether no operation identity was available for the artifact."""
    return value == UNBOUND
