"""The longest a weekly run may take while each part stays within its own limit (#1162, #1181).

The scheduled units' ``TimeoutStartSec`` (``deploy/server/autoscout-run.service``)
is set above this figure, 11 hours 28 minutes: the stage budgets, and over up
to three starts (two restarts on new code or a new environment) the code
gate and the Git network commands before the stages, then the two
finalizations' fetch and push, each at its own limit.

The Russian-edition stage (#1181) is not in the figure. Inside the weekly
run it takes only what is left of it, after the time the stages after it
may need, so it never adds to the worst case.
"""

from __future__ import annotations

# A run starts at most three times: the first start and two restarts.
RUN_STARTS = 3
# Before the stages, each start makes the preflight's fetch and the update's fetch and pull.
GIT_NETWORK_COMMANDS_PER_START = 3
# The run finalizes twice, each time with a fetch and a push.
FINALIZATIONS = 2
GIT_NETWORK_COMMANDS_PER_FINALIZATION = 2


def stage_budgets_seconds() -> float:
    """Every stage budget of one run: sources, then descriptions, adjudication and the other stages."""
    from autoscout import description_adjudication
    from autoscout import description_stage_limits
    from autoscout import editorial_fit_shadow
    from autoscout import isbn_completion
    from autoscout import original_edition_completion
    from autoscout import pm_logline_description
    from autoscout import runtime_budget
    from autoscout import work_age

    return float(
        runtime_budget.WEEKLY_OPERATION_CEILING_SECONDS
        + isbn_completion.STAGE_BUDGET_SECONDS
        + description_stage_limits.GROUNDED_BUDGET_CEILING_SECONDS
        + description_adjudication.ADJUDICATION_STAGE_BUDGET_SECONDS
        + pm_logline_description.STAGE_BUDGET_SECONDS
        + original_edition_completion.STAGE_BUDGET_SECONDS
        + work_age.STAGE_BUDGET_SECONDS
        + editorial_fit_shadow.DEFAULT_STAGE_BUDGET_SECONDS
    )


def start_seconds() -> float:
    """What one start may spend before its stages: the code gate's install and check, and its Git commands."""
    from autoscout import code_gate
    from autoscout import git_envelope

    return float(
        code_gate.INSTALL_TIMEOUT_SECONDS
        + code_gate.CHECK_TIMEOUT_SECONDS
        + GIT_NETWORK_COMMANDS_PER_START * git_envelope.GIT_NETWORK_TIMEOUT_SECONDS
    )


def publication_seconds() -> float:
    """What the finalizations' Git network commands may spend."""
    from autoscout import git_envelope

    return float(FINALIZATIONS * GIT_NETWORK_COMMANDS_PER_FINALIZATION * git_envelope.GIT_NETWORK_TIMEOUT_SECONDS)


def worst_case_seconds() -> float:
    """The longest a run may take while every part stays within its limit: 11 hours 28 minutes."""
    return stage_budgets_seconds() + RUN_STARTS * start_seconds() + publication_seconds()


def russian_edition_reserve_seconds() -> float:
    """The time the stages after the Russian-edition stage may need.

    The editorial-fit stage's budget and publication. The refresh and the
    review have no budget of their own and are not in the worst case either.
    """
    from autoscout import editorial_fit_shadow

    return float(editorial_fit_shadow.DEFAULT_STAGE_BUDGET_SECONDS) + publication_seconds()


def russian_edition_seconds_left(elapsed_this_start: float) -> float:
    """What the Russian-edition stage may spend, *elapsed_this_start* seconds into the run's current start.

    The current start is the run's last: the earlier starts may have spent
    their share before the stages, counted at its limit. What is left of the
    worst case after this start's elapsed time and the reserve for the
    stages after it is the stage's; never below zero.
    """
    earlier_starts = (RUN_STARTS - 1) * start_seconds()
    left = worst_case_seconds() - earlier_starts - max(0.0, elapsed_this_start) - russian_edition_reserve_seconds()
    return max(0.0, left)


__all__ = [
    "FINALIZATIONS",
    "GIT_NETWORK_COMMANDS_PER_FINALIZATION",
    "GIT_NETWORK_COMMANDS_PER_START",
    "RUN_STARTS",
    "publication_seconds",
    "russian_edition_reserve_seconds",
    "russian_edition_seconds_left",
    "stage_budgets_seconds",
    "start_seconds",
    "worst_case_seconds",
]
