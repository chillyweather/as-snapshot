"""Weekly description sizing; adjudication's separate time ceiling remains 5400 seconds."""

from dataclasses import dataclass

# Previous fixed values: ordinary weeks must not get a smaller allowance.
STAGE_BUDGET_SECONDS = 900.0
# The candidate cap bounds vendor calls separately from the stage clock (#640).
# 120 exceeded the 92 candidates on 2026-09-03; stubs join the backlog,
# but new books are admitted first (#788).
ADJUDICATION_CANDIDATE_LIMIT = 120
# Approximately the observed 3.5 seconds per candidate, rounded up.
SECONDS_PER_CANDIDATE = 4.0
# Keep the weekly run bounded.
GROUNDED_BUDGET_CEILING_SECONDS = 2700.0
# About what fits in 5400 seconds with three workers and 43-second calls.
ADJUDICATION_LIMIT_CEILING = 360


@dataclass(frozen=True)
class DescriptionStageLimits:
    grounded_stage_budget_seconds: float
    adjudication_candidate_limit: int


def description_stage_limits(count: int | None) -> DescriptionStageLimits:
    """Size weekly allowances from eligible new and updated candidates."""
    count = max(0, count or 0)
    return DescriptionStageLimits(
        grounded_stage_budget_seconds=min(
            GROUNDED_BUDGET_CEILING_SECONDS, max(STAGE_BUDGET_SECONDS, count * SECONDS_PER_CANDIDATE)
        ),
        adjudication_candidate_limit=min(ADJUDICATION_LIMIT_CEILING, max(ADJUDICATION_CANDIDATE_LIMIT, count)),
    )
