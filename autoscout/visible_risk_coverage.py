"""Visible Risk Screen Coverage: the operational record beside Visible Risk.

The editorial Visible Risk status answers "does public data show a
configured risk category?". This record answers the separate operational
question "was this candidate screened, and how completely?" (issue #714,
ADR 0018), so an empty status is never the only signal. It mirrors the
Russian-edition Verification record of ADR 0015: a closed vocabulary,
bounded English reasons, and no Russian wording inside JSON artifacts.

Coverage is computed by the screening layer from the evidence it read. It
is never inherited from a collector field and never inferred from an
absent one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from collections.abc import Mapping
from typing import Any

VISIBLE_RISK_SCREEN_COVERAGE_FIELD = "visible_risk_screen_coverage"

SCREENED = "screened"
PARTIAL = "partial"
NOT_RUN = "not_run"
FAILED = "failed"
VISIBLE_RISK_SCREEN_COVERAGES = (SCREENED, PARTIAL, NOT_RUN, FAILED)

# The public metadata the screen reads, in canonical order. The title names
# the book and a description says what it is about; only the two together
# let the screen conclude that no configured category is present. Evidence
# Notes are read for signals, but they say why a book was listed (an award,
# a ranking) rather than what it contains, so they never conclude alone.
TITLE_EVIDENCE = "title"
DESCRIPTION_EVIDENCE_FIELDS = ("short_description", "content_description")
EVIDENCE_NOTES_EVIDENCE = "evidence_notes"
EVIDENCE_FIELDS = (TITLE_EVIDENCE, *DESCRIPTION_EVIDENCE_FIELDS, EVIDENCE_NOTES_EVIDENCE)

SCREENED_REASON = "Every configured category was checked against the title and a description."
PARTIAL_NOTES_ONLY_REASON = (
    "No description was available; configured categories were checked against the title and Evidence Notes only."
)
PARTIAL_TITLE_ONLY_REASON = (
    "No Content Description or Evidence Note was available; configured categories were checked against the title only."
)
PARTIAL_NO_TITLE_REASON = (
    "No title was available; configured categories were checked against the other listed fields only."
)
PARTIAL_NO_METADATA_REASON = "No public metadata was available; configured categories could not be checked."
NOT_RUN_REASON = "A Visible Risk status was supplied with the Source Mention; the screen did not run."
_FAILED_REASON_PREFIX = "The screen raised "
_FAILED_REASON_PATTERN = re.compile(r"^The screen raised [A-Za-z_][A-Za-z0-9_]{0,79}\.$")


def coverage_for_evidence(evidence_fields: Iterable[str]) -> dict[str, Any]:
    """Return the screened or partial record for the fields the screen read."""
    present = set(evidence_fields)
    fields = [name for name in EVIDENCE_FIELDS if name in present]
    has_description = any(name in present for name in DESCRIPTION_EVIDENCE_FIELDS)
    if TITLE_EVIDENCE in present and has_description:
        return {"coverage": SCREENED, "evidence_fields": fields, "reason": SCREENED_REASON}
    if not fields:
        reason = PARTIAL_NO_METADATA_REASON
    elif TITLE_EVIDENCE not in present:
        reason = PARTIAL_NO_TITLE_REASON
    elif EVIDENCE_NOTES_EVIDENCE in present:
        reason = PARTIAL_NOTES_ONLY_REASON
    else:
        reason = PARTIAL_TITLE_ONLY_REASON
    return {"coverage": PARTIAL, "evidence_fields": fields, "reason": reason}


def not_run_coverage() -> dict[str, Any]:
    return {"coverage": NOT_RUN, "evidence_fields": [], "reason": NOT_RUN_REASON}


def failed_coverage(exception_type: str) -> dict[str, Any]:
    name = exception_type if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", exception_type) else "Exception"
    return {"coverage": FAILED, "evidence_fields": [], "reason": f"{_FAILED_REASON_PREFIX}{name}."}


def validate_visible_risk_screen_coverage(value: Any) -> None:
    """Reject any record outside the closed coverage contract."""
    if not isinstance(value, Mapping):
        raise ValueError("Visible Risk Screen Coverage must be an object")
    if set(value) != {"coverage", "evidence_fields", "reason"}:
        raise ValueError("Visible Risk Screen Coverage has unsupported or missing keys")
    coverage = value["coverage"]
    if coverage not in VISIBLE_RISK_SCREEN_COVERAGES:
        raise ValueError("Visible Risk Screen Coverage has an unsupported coverage value")
    fields = value["evidence_fields"]
    if not isinstance(fields, list) or any(name not in EVIDENCE_FIELDS for name in fields):
        raise ValueError("Visible Risk Screen Coverage evidence_fields has an unsupported value")
    if fields != [name for name in EVIDENCE_FIELDS if name in fields]:
        raise ValueError("Visible Risk Screen Coverage evidence_fields must be unique and in canonical order")
    reason = value["reason"]
    if coverage in {SCREENED, PARTIAL}:
        if dict(value) != coverage_for_evidence(fields):
            raise ValueError(f"Visible Risk Screen Coverage {coverage} does not match its evidence fields")
    elif fields:
        raise ValueError(f"Visible Risk Screen Coverage {coverage} cannot list evidence fields")
    elif coverage == NOT_RUN and reason != NOT_RUN_REASON:
        raise ValueError("Visible Risk Screen Coverage not_run has an unsupported reason")
    elif coverage == FAILED and not (isinstance(reason, str) and _FAILED_REASON_PATTERN.fullmatch(reason)):
        raise ValueError("Visible Risk Screen Coverage failed has an unsupported reason")


def is_valid_visible_risk_screen_coverage(value: Any) -> bool:
    try:
        validate_visible_risk_screen_coverage(value)
    except ValueError:
        return False
    return True


def recorded_coverage(record: Any) -> str:
    """Return the coverage value of a valid record, or blank for anything else."""
    if record is None or not is_valid_visible_risk_screen_coverage(record):
        return ""
    return str(record["coverage"])


def visible_risk_screen_coverage_counts(candidates: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Count candidates per coverage value; records outside the contract are not counted."""
    counts = dict.fromkeys(VISIBLE_RISK_SCREEN_COVERAGES, 0)
    for candidate in candidates:
        coverage = recorded_coverage(candidate.get(VISIBLE_RISK_SCREEN_COVERAGE_FIELD))
        if coverage:
            counts[coverage] += 1
    return counts
