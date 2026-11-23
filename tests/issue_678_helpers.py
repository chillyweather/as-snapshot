"""Shared helpers for the Issue #678 test suites.

Both the decision-support and feedback-dimension suites build canonical
dimension dicts from named overrides; one helper keeps the construction
identical everywhere instead of duplicating the three-line shape.
"""

from __future__ import annotations

from autoscout.feedback_dimensions import DIMENSION_CHOICES
from autoscout.feedback_dimensions import canonical_dimensions_from_raw


def make_dims(**overrides: str) -> dict[str, str]:
    """Return canonical dimensions with named overrides (blank = unspecified)."""
    raw = {name: "" for name in DIMENSION_CHOICES}
    raw.update(overrides)
    return canonical_dimensions_from_raw(raw)
