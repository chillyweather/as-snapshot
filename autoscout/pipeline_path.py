"""Typed outcomes for the pipeline stage."""

from enum import StrEnum


class PipelinePath(StrEnum):
    """The distinguishable paths through ``run_pipeline``."""

    SCOUTED = "scouted"
    SCOUTED_WITH_FEEDBACK = "scouted-with-feedback"
    FEEDBACK_APPLIED = "feedback-applied"
    FEEDBACK_NOOP = "feedback-noop"
