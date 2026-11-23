"""Offline coverage for issue #634: drain the oldest waiting candidates first.

The grounded description queue holds waiting entries across runs, so the
work list must order by priority label, then by oldest first-seen date,
then by candidate id. The candidate id tie-break keeps the ordering total,
so two runs sort the same way and artifacts stay byte-identical.
"""

from __future__ import annotations

from autoscout.grounded_description import GroundedDescriptionQueue
from autoscout.grounded_description import QueueEntry


def _entry(
    candidate_id: str,
    *,
    operation: str = "operation-2026-09-14",
    operation_date: str = "2026-09-14",
    first_seen_date: str = "2026-09-14",
    priority_label: str = "D",
) -> QueueEntry:
    return QueueEntry(
        candidate_id=candidate_id,
        queue_entry_operation=operation,
        queue_entry_date=operation_date,
        first_seen_date=first_seen_date,
        priority_label=priority_label,
    )


def _candidate(candidate_id: str, *, first_seen_date: str, priority_label: str) -> dict[str, str]:
    return {
        "candidate_id": candidate_id,
        "first_seen_date": first_seen_date,
        "priority_label": priority_label,
    }


def _ordered_ids(queue: GroundedDescriptionQueue, candidates: list[dict[str, str]]) -> list[str]:
    ordered = queue.worklist(
        candidates,
        operation_id="operation-current",
        operation_date="2026-09-14",
        eligible_candidate_ids={candidate["candidate_id"] for candidate in candidates},
    )
    return [candidate["candidate_id"] for candidate, _entry in ordered]


def test_old_waiting_candidate_runs_before_newcomer_of_same_label() -> None:
    queue = GroundedDescriptionQueue(
        {
            "candidate-old": _entry("candidate-old", first_seen_date="2026-06-10"),
            "candidate-new": _entry(
                "candidate-new",
                operation="operation-current",
                operation_date="2026-09-03",
                first_seen_date="2026-09-03",
            ),
        }
    )
    candidates = [
        _candidate("candidate-new", first_seen_date="2026-09-03", priority_label="D"),
        _candidate("candidate-old", first_seen_date="2026-06-10", priority_label="D"),
    ]

    assert _ordered_ids(queue, candidates) == ["candidate-old", "candidate-new"]


def test_stronger_priority_label_wins_over_older_first_seen_date() -> None:
    queue = GroundedDescriptionQueue(
        {
            "candidate-old": _entry("candidate-old", first_seen_date="2026-06-10"),
            "candidate-new": _entry(
                "candidate-new",
                operation="operation-current",
                operation_date="2026-09-03",
                first_seen_date="2026-09-03",
                priority_label="A",
            ),
        }
    )
    candidates = [
        _candidate("candidate-old", first_seen_date="2026-06-10", priority_label="D"),
        _candidate("candidate-new", first_seen_date="2026-09-03", priority_label="A"),
    ]

    assert _ordered_ids(queue, candidates) == ["candidate-new", "candidate-old"]


def test_ordering_is_total_with_candidate_id_tie_break() -> None:
    queue = GroundedDescriptionQueue({})
    candidates = [
        _candidate("candidate-c", first_seen_date="2026-09-03", priority_label="D"),
        _candidate("candidate-a", first_seen_date="2026-09-03", priority_label="D"),
        _candidate("candidate-b", first_seen_date="2026-09-03", priority_label="D"),
    ]

    ordered = queue.worklist(
        candidates,
        operation_id="operation-current",
        operation_date="2026-09-03",
        eligible_candidate_ids={"candidate-a", "candidate-b", "candidate-c"},
    )

    assert [candidate["candidate_id"] for candidate, _entry in ordered] == [
        "candidate-a",
        "candidate-b",
        "candidate-c",
    ]
    keys = [(entry.priority_label, entry.first_seen_date, entry.candidate_id) for _candidate, entry in ordered]
    assert len(set(keys)) == len(keys)
