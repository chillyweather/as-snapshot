"""Structured feedback dimensions for Issue #678.

The editorial audit (#667) showed that the single feedback status mixes
several different signals: editorial taste, rights/acquisition availability,
product or data-quality problems, information still needed, and the final
editorial disposition. Treating those signals as one label risks teaching
the wrong preference.

This module defines the bounded vocabularies for the four recordable
dimensions. The fifth dimension — the final editorial disposition — is the
existing editor status and is presented as a dimension in the preview
without changing how it is stored.

Safety rules (Issue #678):
- A short bare refusal (a note that is nothing but a refusal word, such as
  «Нет.») stays explicitly unspecified. No topic, treatment, or author
  preference may be derived from it, so a bare refusal combined with any
  specified dimension is refused.
- Positive-but-unavailable is not negative taste: the appeal and rights
  dimensions are recorded separately so learning never has to infer one
  from the other.
- A technical complaint is not an acquisition lead, and a purchase-status
  check is not a taste signal: quality problems and information needs are
  recorded on their own dimensions.
- Recording a dimension is presentation of editor-stated facts only; it
  never creates an Editorial Taste Profile entry by itself.

Values are accepted exactly as declared in ``DIMENSION_CHOICES`` — no
normalization, translation, or inference. Blank means "not discussed" and
stays ``unspecified``.
"""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Mapping
from typing import Any

from autoscout.editorial_audit import BARE_REFUSAL

UNSPECIFIED = "unspecified"

DIMENSION_CHOICES: dict[str, tuple[str, ...]] = {
    "editorial_appeal": ("appealing", "not_appealing", UNSPECIFIED),
    "rights_availability": ("available", "unavailable", "unknown", UNSPECIFIED),
    "quality_problem": (
        "none",
        "description_missing",
        "wrong_category",
        "bad_links",
        "unusable_title",
        "other",
        UNSPECIFIED,
    ),
    "information_needed": (
        "none",
        "purchase_status",
        "rights_status",
        "russian_edition",
        "description",
        "other",
        UNSPECIFIED,
    ),
}

# record-feedback argparse destinations, one per dimension name.
DIMENSION_ATTRS: dict[str, str] = {
    "editorial_appeal": "appeal",
    "rights_availability": "rights",
    "quality_problem": "quality_problem",
    "information_needed": "info_needed",
}

# Bounded set of short bare refusals: a note that is nothing but a refusal
# word. The set is exact (whitespace-stripped) and deliberately small —
# anything beyond these forms is a substantive note, not a bare refusal.
# The canonical form is the single truth in ``editorial_audit.BARE_REFUSAL``
# (the #667 audit observed 55 exact notes); the remaining forms are the
# explicitly bounded #678 extension.
BARE_REFUSALS: frozenset[str] = frozenset({BARE_REFUSAL, "Нет", "нет.", "нет", "No.", "No", "no.", "no"})


class FeedbackDimensionError(ValueError):
    """A structured-dimension validation problem."""


def canonical_dimensions_from_raw(raw: Mapping[str, str]) -> dict[str, str]:
    """Validate raw per-dimension values into the canonical dimension dict.

    Every declared dimension must be present in ``raw`` (missing keys count
    as blank). Blank values default to ``unspecified`` (the dimension was
    not discussed). Values are compared exactly against the declared
    choices; unknown values raise :class:`FeedbackDimensionError`.
    """
    dims: dict[str, str] = {}
    for name, allowed in DIMENSION_CHOICES.items():
        value = str(raw.get(name, "")).strip()
        if not value:
            dims[name] = UNSPECIFIED
            continue
        if value not in allowed:
            raise FeedbackDimensionError(f"Unsupported {name} value: {value}. Allowed values: {', '.join(allowed)}")
        dims[name] = value
    return dims


def unspecified_dimensions() -> dict[str, str]:
    """Return the all-unspecified dimension dict (nothing was discussed)."""
    return {name: UNSPECIFIED for name in DIMENSION_CHOICES}


def has_specified_dimensions(dims: Mapping[str, str]) -> bool:
    """Return True when at least one dimension carries a non-default value."""
    return any(value != UNSPECIFIED for value in dims.values())


def is_bare_refusal(note: str) -> bool:
    """Return True when the note is a short bare refusal (whitespace-tolerant)."""
    return str(note).strip() in BARE_REFUSALS


def validate_note_dimensions(note: str, dims: Mapping[str, str]) -> None:
    """Refuse a bare refusal combined with any specified dimension.

    A bare refusal stays explicitly unspecified: no topic, treatment, or
    author preference may be derived from it, so recording a dimension
    beside it would smuggle in an inference the editor never made.
    """
    if is_bare_refusal(note) and has_specified_dimensions(dims):
        raise FeedbackDimensionError(
            "bare_refusal_must_stay_unspecified: a short bare refusal such as «Нет.» "
            "stays explicitly unspecified; no dimension may be recorded from it"
        )


def dimensions_from_row(row: Mapping[str, Any]) -> dict[str, str] | None:
    """Extract the confirmed dimension dict from one pending/returned row.

    Returns a defensive copy when the row carries a ``Feedback Dimensions``
    mapping, and ``None`` otherwise. Every consumer of stored pending-row
    dimensions (in-place finalization, full-pipeline consumption) goes
    through this one helper so the stored shape is read identically
    everywhere.
    """
    return _dimensions_copy(row, "Feedback Dimensions")


def dimensions_from_record(record: Mapping[str, Any]) -> dict[str, str] | None:
    """Extract the dimension dict from a candidate/history record.

    Records store dimensions under the lowercase ``feedback_dimensions``
    key (the projected field name), while pending rows use the CSV-style
    ``Feedback Dimensions`` column name.
    """
    return _dimensions_copy(record, "feedback_dimensions")


def _dimensions_copy(record: Mapping[str, Any], key: str) -> dict[str, str] | None:
    value = record.get(key) if isinstance(record, Mapping) else None
    return dict(value) if isinstance(value, dict) else None


def dimension_preview_lines(dims: Mapping[str, str], final_disposition: str) -> Iterable[str]:
    """Yield the exact preview lines for the structured dimensions."""
    for name in DIMENSION_CHOICES:
        yield f"dimension_{name}={dims.get(name, UNSPECIFIED)}"
    yield f"final_disposition={final_disposition}"
