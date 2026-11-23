"""Closed source-run status values and stale-evidence policy."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final
from typing import Literal
from typing import TypeGuard
from typing import assert_never

SourceRunStatus = Literal[
    "inactive",
    "skipped",
    "ignored",
    "failed",
    "collected",
    "reused",
    "precollected",
    "codex-captured",
    "not-due",
]

SOURCE_STATUS_INACTIVE: Final[SourceRunStatus] = "inactive"
SOURCE_STATUS_SKIPPED: Final[SourceRunStatus] = "skipped"
SOURCE_STATUS_IGNORED: Final[SourceRunStatus] = "ignored"
SOURCE_STATUS_FAILED: Final[SourceRunStatus] = "failed"
SOURCE_STATUS_COLLECTED: Final[SourceRunStatus] = "collected"
SOURCE_STATUS_REUSED: Final[SourceRunStatus] = "reused"
SOURCE_STATUS_PRECOLLECTED: Final[SourceRunStatus] = "precollected"
SOURCE_STATUS_CODEX_CAPTURED: Final[SourceRunStatus] = "codex-captured"
# Deliberately not collected tonight: an annual or monthly source outside its
# collection weekday (#1057). Neither a failure, a skip, nor stale evidence.
SOURCE_STATUS_NOT_DUE: Final[SourceRunStatus] = "not-due"
# The reason of a Kirkus source the main capture leaves to the background pre-collection pass (#880).
KIRKUS_DEFERRED_SKIP_REASON: Final[str] = "Kirkus capture awaits the background pre-collection pass"

ALL_SOURCE_RUN_STATUSES: Final[tuple[SourceRunStatus, ...]] = (
    SOURCE_STATUS_INACTIVE,
    SOURCE_STATUS_SKIPPED,
    SOURCE_STATUS_IGNORED,
    SOURCE_STATUS_FAILED,
    SOURCE_STATUS_COLLECTED,
    SOURCE_STATUS_REUSED,
    SOURCE_STATUS_PRECOLLECTED,
    SOURCE_STATUS_CODEX_CAPTURED,
    SOURCE_STATUS_NOT_DUE,
)

COLLECTING_SOURCE_STATUSES: Final[frozenset[SourceRunStatus]] = frozenset(
    {SOURCE_STATUS_COLLECTED, SOURCE_STATUS_REUSED}
)


@dataclass(frozen=True)
class SourceRunSummary:
    """Normalized source-run accounting used by pipeline and review seams."""

    statuses: tuple[SourceRunStatus, ...]
    stale_source_count: int
    collecting_source_count: int
    freshly_collected_count: int


__all__ = [
    "ALL_SOURCE_RUN_STATUSES",
    "COLLECTING_SOURCE_STATUSES",
    "SOURCE_STATUS_CODEX_CAPTURED",
    "SOURCE_STATUS_COLLECTED",
    "SOURCE_STATUS_FAILED",
    "SOURCE_STATUS_IGNORED",
    "SOURCE_STATUS_INACTIVE",
    "SOURCE_STATUS_NOT_DUE",
    "SOURCE_STATUS_PRECOLLECTED",
    "SOURCE_STATUS_REUSED",
    "SOURCE_STATUS_SKIPPED",
    "SourceRunStatus",
    "SourceRunSummary",
    "is_source_run_status",
    "is_stale_source_run",
    "normalized_source_run_status",
    "retains_ranking_freshness",
    "summarize_source_runs",
]


def is_source_run_status(value: object) -> TypeGuard[SourceRunStatus]:
    """Return whether a raw value belongs to the closed source-run domain."""
    return value in ALL_SOURCE_RUN_STATUSES


def normalized_source_run_status(value: object) -> SourceRunStatus | None:
    """Normalize a raw source-run status, rejecting values outside the domain."""
    normalized = value.strip() if isinstance(value, str) else value
    return normalized if is_source_run_status(normalized) else None


def is_stale_source_run(status: SourceRunStatus) -> bool:
    """Return whether a source run used evidence from an earlier operation."""
    match status:
        case "reused":
            return True
        case (
            "inactive" | "skipped" | "ignored" | "failed" | "collected" | "precollected" | "codex-captured" | "not-due"
        ):
            return False
        case _:
            return assert_never(status)


def retains_ranking_freshness(status: SourceRunStatus) -> bool:
    """Return whether a source run may retain an undated-ranking fingerprint.

    An undated recurring ranking proves currency by row movement, so the
    retained fingerprint means "this list was the current list on that date".
    Only a run the stage accepted may make that claim.  A failed run that
    recorded it locked the source out of every later run: the next run saw the
    same unchanged page and refused it as frozen, for a reason the source could
    not clear until the remote rows happened to move (issue #716).
    """
    match status:
        case "collected" | "precollected":
            return True
        case "inactive" | "skipped" | "ignored" | "failed" | "reused" | "codex-captured" | "not-due":
            return False
        case _:
            return assert_never(status)


def summarize_source_runs(statuses: Iterable[object]) -> SourceRunSummary:
    """Normalize source-run statuses and return all shared accounting counts."""
    normalized_statuses = tuple(
        status for raw_status in statuses if (status := normalized_source_run_status(raw_status)) is not None
    )
    return SourceRunSummary(
        statuses=normalized_statuses,
        stale_source_count=sum(1 for status in normalized_statuses if is_stale_source_run(status)),
        collecting_source_count=sum(1 for status in normalized_statuses if status in COLLECTING_SOURCE_STATUSES),
        freshly_collected_count=sum(1 for status in normalized_statuses if status == SOURCE_STATUS_COLLECTED),
    )
