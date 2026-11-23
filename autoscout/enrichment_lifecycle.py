"""Lifecycle bands for enrichment eligibility ordering (issue #699).

Only a **new** Scouting Candidate reaches the editor as something to act
on. A candidate seen in an earlier run is never reviewed again: its value
is statistical, or signal for taste and preference learning. Enrichment
knew nothing about that, so a June candidate competed for the stage budget
on equal terms with one discovered this week — and because the budget runs
out mid-queue, the rows the editor actually reads were often the ones never
attempted.

This module supplies the one ordering fact the enrichment stages were
missing: which lifecycle band a candidate belongs to.

* ``new`` — first appearance in this run (``update_delta`` "new").
* ``updated`` — re-seen with a material change ("upgraded"/"changed"):
  the editor sees these too, behind the new rows.
* ``previously_seen`` — everything else: re-seen unchanged, or not in this
  run's candidates at all. Still worked, but only with budget left over.

The band is a **reordering** key, never a filter. Nothing is dropped:
previously-seen candidates keep their place in the queue and still run
when the budget reaches them. Within a band the stages keep whatever
ordering they already had — in particular the repair fairness order
(fewest attempts first, then oldest attempt), so a repeatedly failing new
candidate still cannot starve its peers.

Band derivation is a pure function of one mapping's ``update_delta``
field, so it is testable without fixtures, and an unknown or missing label
degrades to ``previously_seen`` rather than jumping the queue: a candidate
must be positively labelled new to be treated as new.
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from enum import StrEnum
from typing import Any
from typing import TypeVar

BAND_NEW = "new"
BAND_UPDATED = "updated"
BAND_PREVIOUSLY_SEEN = "previously_seen"

# Admission order. New rows first, updated next, previously seen last.
BAND_ORDER: tuple[str, ...] = (BAND_NEW, BAND_UPDATED, BAND_PREVIOUSLY_SEEN)
_BAND_RANK = {band: rank for rank, band in enumerate(BAND_ORDER)}

# The pipeline's ``update_delta`` vocabulary, mapped onto the three bands.
# "unchanged" is a re-seen candidate with nothing new to act on, so it
# belongs with the previously-seen rows rather than with the updated ones.
_DELTA_BANDS = {
    "new": BAND_NEW,
    "upgraded": BAND_UPDATED,
    "changed": BAND_UPDATED,
    "unchanged": BAND_PREVIOUSLY_SEEN,
}

_M = TypeVar("_M", bound=Mapping[str, Any])


def lifecycle_band(record: Mapping[str, Any]) -> str:
    """Return the lifecycle band of one candidate or history mapping.

    Pure function of the mapping's ``update_delta`` label. A blank,
    missing, or unrecognised label is ``previously_seen``: a history-only
    record carries no delta, and an unknown label must not be promoted
    ahead of the rows the editor reads.
    """
    delta = str(record.get("update_delta", "") or "").strip().casefold()
    return _DELTA_BANDS.get(delta, BAND_PREVIOUSLY_SEEN)


def lifecycle_band_rank(record: Mapping[str, Any]) -> int:
    """Return the admission rank of one mapping's lifecycle band."""
    return _BAND_RANK[lifecycle_band(record)]


def band_rank(band: str) -> int:
    """Return the admission rank of a band name (unknown names sort last)."""
    return _BAND_RANK.get(str(band).strip().casefold(), len(BAND_ORDER))


def order_by_lifecycle_band(items: Sequence[_M]) -> list[_M]:
    """Stable-sort candidate mappings into band order.

    The sort is stable on purpose: each band keeps the relative ordering
    the caller already established — fresh work before repairs, repairs
    fewest-attempts-first — so this adds a band key without touching any
    existing fairness rule.
    """
    return sorted(items, key=lifecycle_band_rank)


_YIELD_FIELDS = ("eligible", "attempted", "described", "not_attempted_budget")
# Fields derived from what the stage actually did. A whole-stage failure
# knows none of them, so they are reported as unknown rather than guessed.
_ATTEMPT_FIELDS = ("attempted", "described", "not_attempted_budget")


class AttemptCoverage(StrEnum):
    """Whether the attempt figures describe observed stage behaviour.

    A closed vocabulary rather than a free string: ``measured`` is an
    affirmative claim that the stage really attempted what the counts say,
    and the #699 acceptance criteria are read off it. A typo must not be
    able to make that claim, so :func:`resolve_attempt_coverage` fails
    closed to ``UNKNOWN`` for anything it does not recognise exactly.
    """

    MEASURED = "measured"
    UNKNOWN = "unknown"


COVERAGE_MEASURED = AttemptCoverage.MEASURED
COVERAGE_UNKNOWN = AttemptCoverage.UNKNOWN


def resolve_attempt_coverage(value: Any) -> AttemptCoverage:
    """Resolve a coverage label, failing closed to ``UNKNOWN``.

    Only the exact ``measured`` label yields affirmative coverage. A typo,
    a ``None``, a differently-cased spelling, or any unrecognised value
    reports unknown rather than silently claiming the attempt figures were
    observed — the safe direction, because "unknown" only withholds
    evidence while a wrong "measured" manufactures it.
    """
    try:
        return AttemptCoverage(value)
    except ValueError:
        return AttemptCoverage.UNKNOWN


def _yield_block(values: Mapping[str, int], *, measured: bool) -> dict[str, Any]:
    if not measured:
        # Eligibility is planned before the stage runs, so it survives a
        # stage failure. Everything downstream of admission does not.
        return {
            "eligible": values["eligible"],
            **dict.fromkeys(_ATTEMPT_FIELDS, None),
            "yield": None,
        }
    attempted = values["attempted"]
    return {
        **{field: values[field] for field in _YIELD_FIELDS},
        # A stage that attempted nothing has no yield. Reporting 0.0 there
        # would read as "attempted and failed", which is a different fact.
        "yield": round(values["described"] / attempted, 4) if attempted else None,
    }


def lifecycle_yield(
    entries: Sequence[Mapping[str, Any]],
    *,
    attempt_coverage: AttemptCoverage | str = AttemptCoverage.MEASURED,
) -> dict[str, Any]:
    """Summarize first-pass description yield per band and in total (#699).

    Each entry is ``{"band", "attempted", "described",
    "not_attempted_budget"}`` for one eligible candidate: ``attempted``
    means the stage actually ran synthesis for it, ``described`` means the
    run produced a description, and ``not_attempted_budget`` means the
    stage clock never reached it. A nonzero ``not_attempted_budget`` in the
    ``new`` band is the direct signal that the budget ran out before the
    rows the editor reads were exhausted.

    The new-and-updated figure is reported beside the all-rows total
    rather than replacing it: the all-rows number stays comparable with
    earlier runs, while the banded one says what the editor actually got.
    A yield of ``None`` means nothing was attempted in that band.

    ``attempt_coverage`` guards the honesty of all of that. A whole-stage
    failure synthesizes one failed outcome per eligible candidate, which
    is indistinguishable here from a real attempt that found nothing — so
    counting it would report "every new candidate was attempted, yield
    0.0" for a stage that may never have started one. Pass
    ``COVERAGE_UNKNOWN`` for that case: eligibility is still reported (it
    is planned before the stage runs), and every attempt-derived figure
    becomes ``None``. An unknown-coverage summary must never be read as
    evidence for the #699 acceptance criteria. An unrecognised label fails
    closed to unknown rather than claiming coverage it cannot support.
    """
    coverage = resolve_attempt_coverage(attempt_coverage)
    measured = coverage is AttemptCoverage.MEASURED
    per_band = {band: dict.fromkeys(_YIELD_FIELDS, 0) for band in BAND_ORDER}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        band = str(entry.get("band", "")).strip().casefold()
        if band not in per_band:
            band = BAND_PREVIOUSLY_SEEN
        bucket = per_band[band]
        bucket["eligible"] += 1
        if not measured:
            continue
        if entry.get("attempted"):
            bucket["attempted"] += 1
            if entry.get("described"):
                bucket["described"] += 1
        elif entry.get("not_attempted_budget"):
            bucket["not_attempted_budget"] += 1

    def totals(bands: Sequence[str]) -> dict[str, int]:
        return {field: sum(per_band[band][field] for band in bands) for field in _YIELD_FIELDS}

    return {
        # A plain string in the artifact, never the enum member: the report
        # is serialized JSON and must stay byte-stable.
        "attempt_coverage": coverage.value,
        "bands": {band: _yield_block(values, measured=measured) for band, values in per_band.items()},
        "new_and_updated": _yield_block(totals((BAND_NEW, BAND_UPDATED)), measured=measured),
        "all_rows": _yield_block(totals(BAND_ORDER), measured=measured),
    }


__all__ = [
    "BAND_NEW",
    "BAND_ORDER",
    "BAND_PREVIOUSLY_SEEN",
    "BAND_UPDATED",
    "COVERAGE_MEASURED",
    "COVERAGE_UNKNOWN",
    "AttemptCoverage",
    "band_rank",
    "lifecycle_band",
    "lifecycle_band_rank",
    "lifecycle_yield",
    "order_by_lifecycle_band",
    "resolve_attempt_coverage",
]
