"""Bounded repair of legacy thin Content Descriptions (#674).

Fresh pipeline work (missing or fallback descriptions) says "no usable
text". Repair work says "usable text below the depth floor". The two are
scheduled separately because their priority differs — a missing
description beats a short one — and because repair must never destroy
the short text it failed to improve (issue #673 guarantees that; this
module decides who gets attempted).

Eligibility (all required):

* The record holds a usable description (safety-valid, non-fallback).
* The description is below the shared depth floor.
* Its provenance is one of the three approved legacy provenances:
  ``grounded``, ``codex-generated``, or ``backfilled``. No other
  provenance is silently admitted, and ``editor-corrected`` records are
  never repair candidates.
* Hidden candidates are excluded by the caller (visibility lives with
  the caller, not here).

Scheduling (per record, derived from the repair log only):

* ``REPAIR_POLICY_VERSION`` versions the policy. A record logged under
  an older version restarts fresh under the current one. Version 2
  (issue #788) restarts every log: under version 1 the deterministic
  stage usually spent two of the three attempts, so the agentic route
  had one try before a record was parked.
* An unsourced stub (``codex-generated``) is not scheduled by this
  policy on the agentic route: adjudication selects it every run, with
  no cooldown and no exhaustion. It logs each attempt here, for a stub
  of any length, and orders stubs by ``repair_sort_key`` so a recurring
  failure cannot hold a slot of the candidate cap every week. The
  deterministic route keeps scheduling a thin stub as a repair.
* A failed repair attempt cools down for ``REPAIR_COOLDOWN_DAYS`` before
  the record is due again.
* After ``REPAIR_MAX_ATTEMPTS`` failed repair attempts the record is
  exhausted: honestly deferred, still counted in the backlog, retried
  only under a newer policy version or explicit selection.
* Ordering is fewest attempts first, then oldest attempt, then
  candidate ID: recurring failures sink behind untouched records, so a
  finite backlog always drains instead of starving (fairness).

State:

* Repair attempts live in the bounded ``depth_repair_attempts`` list on
  the history record (capped at ``REPAIR_LOG_LIMIT`` entries, oldest
  dropped): ``{date, operation_id, outcome, reason_code}``. Content
  provenance is never stored here and attempt state never touches
  content fields — the two stay separate.
* ``depth_repair_policy_version`` records the policy the log belongs to.

A repair admission forces synthesis even when the accepted evidence URLs
are unchanged (the weekly and standalone routes set the existing
``_force_synthesis`` flag on the attempt copy, never on the record), so
a below-floor grounded record with identical URLs still reaches the
model. A successful repair writes fresh evidence basis and digests, and
the record regains normal evidence reuse by construction: it is no
longer below the floor, so it is no longer a repair candidate.
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from datetime import date
from typing import Any

from autoscout.content_description import DESCRIPTION_HOST_GROUNDED_PROVENANCE
from autoscout.content_description import record_has_usable_description
from autoscout.description_depth import meets_description_depth
from autoscout.source_eligibility import historical_record_source_eligibility_exclusion_reason

# The three legacy provenances approved for automatic depth repair. Every
# other provenance — including agent-adjudicated repairs, which already
# passed a depth gate to exist — is never silently admitted (issue #674).
REPAIR_ELIGIBLE_PROVENANCES = frozenset(
    {
        DESCRIPTION_HOST_GROUNDED_PROVENANCE,
        "codex-generated",
        "backfilled",
    }
)

REPAIR_POLICY_VERSION = 2
REPAIR_COOLDOWN_DAYS = 7
REPAIR_MAX_ATTEMPTS = 3
REPAIR_LOG_LIMIT = 10
# A handoff stays pending for one weekly cycle: the residual stage that
# follows grounded in the same workflow gets exactly one chance to take
# it. After the window the record returns to normal cooldown scheduling,
# so a missed or absent adjudication run strands nothing.
HANDOFF_WINDOW_DAYS = 7

REPAIR_ATTEMPTS_FIELD = "depth_repair_attempts"
REPAIR_POLICY_FIELD = "depth_repair_policy_version"

# Attempt outcomes that count as a failed repair: the record stays thin.
# Success removes repair eligibility, so it never needs counting; budget
# and unchanged-evidence outcomes never attempted synthesis.
#
# Every repair attempt records which route ran it (``stage``:
# "grounded" or "adjudication"). The cross-stage handoff (issue #675,
# review finding) keys off that field: a grounded evidence-shortfall
# failure yields to the residual route instead of burning every attempt
# in grounded first. Entries written before the stage field existed
# carry "": they cool down and exhaust exactly like before but never
# trigger a handoff.

# Repair outcomes whose evidence could not support a description (issue
# #674). Model-side failures (synthesis_failure, invalid_description) and
# scheduling states stay in the backlog via cooldown instead: the evidence
# was never the problem there. The codes are owned by
# ``grounded_description`` and repeated here because that module cannot
# import this one; they are pinned equal by test.
REPAIR_EVIDENCE_SHORTFALL_REASONS = frozenset(
    {
        "no_results",
        "search_or_fetch_failure",
        "source_denylisted",
        "fewer_than_two_agreeing_sources",
        "title_mismatch",
        "author_mismatch",
        "author_unverifiable",
        "source_identity_disagreement",
        "subject_content_disagreement",
        "insufficient_subject_text",
        "truncated_page_no_evidence",
        "page_challenge_no_evidence",
        "search_snippet",
        "invalid_source",
        "insufficient_description_depth",
    }
)


def repair_provenance(record: Mapping[str, Any]) -> str:
    """Return the provenance string of one history record, or ""."""
    raw = record.get("description_provenance")
    if isinstance(raw, Mapping):
        return str(raw.get("provenance", "")).strip()
    return str(raw or "").strip()


def is_repair_candidate(record: Mapping[str, Any]) -> bool:
    """True when a record holds a thin usable description of an approved provenance.

    Display usability (safety-valid, non-fallback) and repair need
    (below the depth floor) are evaluated separately on purpose: the
    review keeps showing the usable text while repair is pending, and a
    record that is merely missing text is fresh work, not repair.
    Editor-corrected records are never candidates.
    """
    if repair_provenance(record) == "editor-corrected":
        return False
    if repair_provenance(record) not in REPAIR_ELIGIBLE_PROVENANCES:
        return False
    if not record_has_usable_description(record):
        return False
    return not meets_description_depth(str(record.get("content_description", "")))


def repair_log(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the bounded repair-attempt log entries of one record."""
    raw = record.get(REPAIR_ATTEMPTS_FIELD, [])
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _log_policy_version(record: Mapping[str, Any]) -> int | None:
    raw = record.get(REPAIR_POLICY_FIELD)
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def repair_attempt_count(record: Mapping[str, Any]) -> int:
    """Count failed repair attempts under the current policy version."""
    if _log_policy_version(record) != REPAIR_POLICY_VERSION:
        return 0
    return sum(1 for item in repair_log(record) if str(item.get("outcome", "")).strip() == "attempted_no_description")


def last_repair_attempt_date(record: Mapping[str, Any]) -> date | None:
    """Return the date of the newest repair attempt under the current policy."""
    if _log_policy_version(record) != REPAIR_POLICY_VERSION:
        return None
    latest: date | None = None
    for item in repair_log(record):
        try:
            parsed = date.fromisoformat(str(item.get("date", "")).strip())
        except ValueError:
            continue
        if latest is None or parsed > latest:
            latest = parsed
    return latest


def repair_due(record: Mapping[str, Any], today: date) -> bool:
    """True when a repair candidate may be attempted on *today*."""
    if not is_repair_candidate(record):
        return False
    if repair_attempt_count(record) >= REPAIR_MAX_ATTEMPTS:
        return False
    latest = last_repair_attempt_date(record)
    if latest is None:
        return True
    return (today - latest).days >= REPAIR_COOLDOWN_DAYS


def repair_exhausted(record: Mapping[str, Any]) -> bool:
    """True when a repair candidate used all attempts under this policy."""
    return is_repair_candidate(record) and repair_attempt_count(record) >= REPAIR_MAX_ATTEMPTS


def repair_sort_key(record: Mapping[str, Any]) -> tuple[int, str, str]:
    """Order repairs fewest-attempts first, then oldest attempt, then ID.

    Failures sink behind untouched records, so recurring failures cannot
    indefinitely starve a finite backlog (fairness, issue #674).
    """
    latest = last_repair_attempt_date(record)
    identity = str(record.get("identity_key", "")).strip()
    candidate_id = str(record.get("candidate_id", "")).strip()
    return (
        repair_attempt_count(record),
        latest.isoformat() if latest is not None else "",
        candidate_id or identity,
    )


def record_repair_attempt(
    record: dict[str, Any],
    *,
    operation_date: str,
    operation_id: str,
    outcome: str,
    reason_code: str,
    stage: str = "",
) -> None:
    """Append one bounded repair-attempt entry to a history record.

    Content fields are never touched here; only the attempt log and the
    policy version marker move. Entries past ``REPAIR_LOG_LIMIT`` drop
    oldest-first, and a stale policy version restarts the log under the
    current one. ``stage`` names the route that ran the attempt
    ("grounded" or "adjudication") for cross-stage handoff accounting.
    """
    if _log_policy_version(record) != REPAIR_POLICY_VERSION:
        record[REPAIR_ATTEMPTS_FIELD] = []
        record[REPAIR_POLICY_FIELD] = REPAIR_POLICY_VERSION
    entries = repair_log(record)
    entries.append(
        {
            "date": str(operation_date).strip(),
            "operation_id": str(operation_id).strip(),
            "outcome": str(outcome).strip(),
            "reason_code": str(reason_code).strip(),
            "stage": str(stage).strip(),
        }
    )
    record[REPAIR_ATTEMPTS_FIELD] = entries[-REPAIR_LOG_LIMIT:]
    record[REPAIR_POLICY_FIELD] = REPAIR_POLICY_VERSION


def pending_repair_handoff(record: Mapping[str, Any], today: date | None = None) -> bool:
    """True when a record's next repair belongs to the residual route.

    A grounded evidence-shortfall failure yields to adjudication: the
    grounded route defers the record and the residual route admits it
    without waiting out the cooldown. The handoff is consumed by the
    next adjudication attempt (success or failure ends it) and expires
    after ``HANDOFF_WINDOW_DAYS`` so a missed adjudication run strands
    nothing — the record then returns to normal cooldown scheduling.
    Exhaustion still blocks it, and model-side or unknown-stage failures
    never start one. Without the handoff, normal weekly sequencing would
    let grounded consume every attempt before adjudication ever sees the
    record (review finding on #675).
    """
    if not is_repair_candidate(record):
        return False
    if repair_exhausted(record):
        return False
    if _log_policy_version(record) != REPAIR_POLICY_VERSION:
        return False
    entries = [
        item for item in repair_log(record) if str(item.get("outcome", "")).strip() == "attempted_no_description"
    ]
    if not entries:
        return False
    latest = entries[-1]
    if str(latest.get("stage", "")).strip() != "grounded":
        return False
    if str(latest.get("reason_code", "")).strip() not in REPAIR_EVIDENCE_SHORTFALL_REASONS:
        return False
    try:
        failed_on = date.fromisoformat(str(latest.get("date", "")).strip())
    except ValueError:
        return False
    day = today if today is not None else date.today()
    return 0 <= (day - failed_on).days <= HANDOFF_WINDOW_DAYS


def select_due_repairs(
    records: Sequence[Mapping[str, Any]],
    today: date,
) -> dict[str, list[dict[str, Any]]]:
    """Partition repair candidates into due, cooling, and exhausted lists.

    Returns ``{"due", "cooling_down", "exhausted"}`` with ``due`` sorted
    by :func:`repair_sort_key`. Non-candidates are ignored entirely: they
    are fresh work, protected records, or already sufficient — never
    repair backlog.
    """
    due: list[dict[str, Any]] = []
    cooling_down: list[dict[str, Any]] = []
    exhausted: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        if not is_repair_candidate(record):
            continue
        # Never shown in the review, so never repaired either (#915).
        if historical_record_source_eligibility_exclusion_reason(record):
            continue
        item = dict(record)
        if repair_exhausted(record):
            exhausted.append(item)
        elif repair_due(record, today):
            due.append(item)
        else:
            cooling_down.append(item)
    due.sort(key=lambda item: (repair_sort_key(item), str(item.get("candidate_id", ""))))
    return {"due": due, "cooling_down": cooling_down, "exhausted": exhausted}


__all__ = [
    "HANDOFF_WINDOW_DAYS",
    "REPAIR_ATTEMPTS_FIELD",
    "REPAIR_COOLDOWN_DAYS",
    "REPAIR_ELIGIBLE_PROVENANCES",
    "REPAIR_EVIDENCE_SHORTFALL_REASONS",
    "REPAIR_LOG_LIMIT",
    "REPAIR_MAX_ATTEMPTS",
    "REPAIR_POLICY_FIELD",
    "REPAIR_POLICY_VERSION",
    "is_repair_candidate",
    "last_repair_attempt_date",
    "pending_repair_handoff",
    "record_repair_attempt",
    "repair_attempt_count",
    "repair_due",
    "repair_exhausted",
    "repair_log",
    "repair_provenance",
    "repair_sort_key",
    "select_due_repairs",
]
