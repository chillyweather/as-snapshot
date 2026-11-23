"""Offline coverage for issue #633: attempt the whole eligible queue in budget.

Two defects kept the grounded description stage at about 92 attempts while
225 candidates waited with ``not_attempted_stage_budget``: the candidate loop
was serial despite a ``max_workers`` value of 3, and admission reserved a
fixed 90 s envelope per candidate while real cost averaged under 9 s. The
stage now runs admitted candidates through the bounded daemon worker pool
and admits on the measured per-candidate cost, while a hard per-candidate
deadline still bounds one slow candidate. Outcomes are collected first and
sorted by candidate id, so concurrency never changes artifact bytes.
"""

from __future__ import annotations

import threading
from typing import Any

from autoscout.grounded_description import ADMISSION_SAMPLE_FLOOR
from autoscout.grounded_description import MEASURED_CANDIDATE_COST_SECONDS
from autoscout.grounded_description import OUTCOME_NOT_ATTEMPTED
from autoscout.grounded_description import REASON_NOT_ATTEMPTED_STAGE_BUDGET
from autoscout.grounded_description import GroundedDescriptionQueue
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import SynthesisResult


class FrozenClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class LockedClock:
    """A fake clock that stays correct when worker threads advance it."""

    def __init__(self, value: float = 0.0) -> None:
        self.value = value
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.value

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.value += seconds


class AdvancingNoResultsDiscovery:
    """Charge a bounded cost per attempt without grounding anything."""

    def __init__(self, clock: LockedClock, cost: float = 5.0) -> None:
        self.clock = clock
        self.cost = cost
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def discover(self, candidate: dict[str, Any], **_: Any) -> list[Any]:
        with self._lock:
            self.calls.append(str(candidate["candidate_id"]))
        self.clock.advance(self.cost)
        return []


class ReplayDiscovery:
    """Ground every candidate with two independent-owner sources."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def discover(self, candidate: dict[str, Any], **_: Any) -> list[dict[str, Any]]:
        with self._lock:
            self.calls.append(str(candidate["candidate_id"]))
        candidate_id = str(candidate["candidate_id"])
        return [
            {
                "url": f"https://publisher.example.test/{candidate_id}",
                "source_kind": "publisher",
                "source_family": "publisher_copy",
                "owner_id": f"publisher-{candidate_id}",
                "classification": "classified",
                "title": candidate["title"],
                "primary_authors": candidate["primary_authors"],
                "subject_text": "Память, миграция и общественная жизнь.",
            },
            {
                "url": f"https://review.example.test/{candidate_id}",
                "source_kind": "review",
                "source_family": "review",
                "owner_id": f"review-{candidate_id}",
                "classification": "classified",
                "title": candidate["title"],
                "primary_authors": candidate["primary_authors"],
                "subject_text": "Память, миграция и общественная жизнь.",
            },
        ]


class ReplayFetch:
    def fetch(self, result: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "url": result.url,
            "source_kind": result.source_kind,
            "source_family": result.source_family,
            "owner_id": result.owner_id,
            "source_language": "en",
            "title": result.title,
            "primary_authors": list(result.primary_authors),
            "subject_text": "Память, миграция и общественная жизнь.",
            "checked_date": kwargs.get("checked_date", "2026-09-14"),
            "classification": "classified",
        }


class ReplaySynthesizer:
    def synthesize(self, candidate: dict[str, Any], sources: Any) -> SynthesisResult:
        return SynthesisResult(f"Книга {candidate['title']} о памяти, обществе и общественной жизни.")


def _candidate(candidate_id: str) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "identity_key": f"{candidate_id}|alex author",
        "title": f"Work {candidate_id}",
        "primary_authors": ["Alex Author"],
        "first_seen_date": "2026-09-03",
        "update_delta": "new",
        "content_description": "",
    }


def test_measured_admission_attempts_candidates_the_old_envelope_would_defer() -> None:
    # Twelve candidates at 5 s each with a 150 s budget. The old rule stopped
    # admission when less than the 100 s discovery-plus-envelope reserve
    # remained, so it admitted 11 and left the twelfth waiting. The measured
    # reserve admits all 12 into 450 available worker-seconds.
    assert MEASURED_CANDIDATE_COST_SECONDS < 90.0
    clock = LockedClock()
    discovery = AdvancingNoResultsDiscovery(clock)
    stage = GroundedDescriptionStage(
        discovery=discovery,
        fetch=None,
        synthesizer=ReplaySynthesizer(),
        monotonic=clock,
        stage_budget_seconds=150.0,
        max_workers=3,
    )
    candidates = [_candidate(f"candidate-{index:02d}") for index in range(12)]

    artifact = stage.run(
        candidates,
        operation_id="admission-operation",
        operation_date="2026-09-14",
    )

    assert sorted(discovery.calls) == sorted(candidate["candidate_id"] for candidate in candidates)
    assert all(outcome.outcome != OUTCOME_NOT_ATTEMPTED for outcome in artifact.outcomes)
    assert all(outcome.reason_code != REASON_NOT_ATTEMPTED_STAGE_BUDGET for outcome in artifact.outcomes)


def test_genuinely_unaffordable_candidates_stay_queued_with_budget_reason() -> None:
    # A 20 s budget at one worker, with candidates that really cost 5 s each.
    # A worker admits only while the remaining time can still hold one, so
    # the rest stay queued with the scheduling reason. The clock has to
    # advance for this to mean anything: under a frozen clock no candidate is
    # ever unaffordable, because no time passes.
    clock = LockedClock()
    discovery = AdvancingNoResultsDiscovery(clock, cost=5.0)
    stage = GroundedDescriptionStage(
        discovery=discovery,
        fetch=None,
        synthesizer=ReplaySynthesizer(),
        monotonic=clock,
        stage_budget_seconds=20.0,
        max_workers=1,
    )
    queue = GroundedDescriptionQueue()
    candidates = [_candidate(f"candidate-{index:02d}") for index in range(12)]

    artifact = stage.run(
        candidates,
        operation_id="tight-budget-operation",
        operation_date="2026-09-14",
        queue=queue,
    )

    attempted = [outcome for outcome in artifact.outcomes if outcome.outcome != OUTCOME_NOT_ATTEMPTED]
    waiting = [outcome for outcome in artifact.outcomes if outcome.outcome == OUTCOME_NOT_ATTEMPTED]
    assert len(attempted) == 3
    assert len(waiting) == 9
    assert sorted(discovery.calls) == sorted(outcome.candidate_id for outcome in attempted)
    for outcome in waiting:
        assert outcome.reason_code == REASON_NOT_ATTEMPTED_STAGE_BUDGET
        assert outcome.scheduling is not None
        assert outcome.scheduling.attempt_status == "waiting"
        assert outcome.scheduling.reason_code == REASON_NOT_ATTEMPTED_STAGE_BUDGET
        assert queue.entries[outcome.candidate_id].attempt_status == "waiting"


def test_a_candidate_the_stage_never_started_is_never_reported_as_attempted() -> None:
    # Admission must not outrun the clock. Real cost here is 20 s, inside the
    # observed range whose maximum was 25.7 s, while the seed reserve is 9 s.
    # One up-front admission batch would admit eleven candidates, reach five,
    # and report the other six as attempted and failed with
    # ``candidate_budget_exhausted``. That tells the editor the sources were
    # checked for books nobody looked at, and it counts each one as a stage
    # failure that needs attention.
    clock = LockedClock()
    discovery = AdvancingNoResultsDiscovery(clock, cost=20.0)
    stage = GroundedDescriptionStage(
        discovery=discovery,
        fetch=None,
        synthesizer=ReplaySynthesizer(),
        monotonic=clock,
        stage_budget_seconds=100.0,
        max_workers=1,
    )
    queue = GroundedDescriptionQueue()
    candidates = [_candidate(f"candidate-{index:02d}") for index in range(20)]

    artifact = stage.run(
        candidates,
        operation_id="overrun-operation",
        operation_date="2026-09-14",
        queue=queue,
    )

    started = set(discovery.calls)
    assert len(started) == ADMISSION_SAMPLE_FLOOR
    for outcome in artifact.outcomes:
        if outcome.candidate_id in started:
            assert outcome.outcome != OUTCOME_NOT_ATTEMPTED
            continue
        # Never started, so it claims nothing about its sources, and it keeps
        # its queue age for the next run.
        assert outcome.outcome == OUTCOME_NOT_ATTEMPTED
        assert outcome.reason_code == REASON_NOT_ATTEMPTED_STAGE_BUDGET
        assert queue.entries[outcome.candidate_id].attempt_status == "waiting"


def test_admission_reserve_grows_to_the_observed_p95_of_the_run() -> None:
    # The seed reserve carries admission until the run has samples of its
    # own. After the sample floor, a run whose candidates cost more than the
    # seed reserves the observed p95 instead, so a stale constant cannot make
    # admission outrun the clock.
    cost = 20.0
    assert cost > MEASURED_CANDIDATE_COST_SECONDS
    clock = LockedClock()
    discovery = AdvancingNoResultsDiscovery(clock, cost=cost)
    stage = GroundedDescriptionStage(
        discovery=discovery,
        fetch=None,
        synthesizer=ReplaySynthesizer(),
        monotonic=clock,
        stage_budget_seconds=100.0,
        max_workers=1,
    )

    artifact = stage.run(
        [_candidate(f"candidate-{index:02d}") for index in range(20)],
        operation_id="p95-operation",
        operation_date="2026-09-14",
    )

    # Five candidates fill the sample floor and consume the whole budget. The
    # sixth is refused against the 20 s observed reserve, and the stage never
    # runs past its budget.
    assert len(discovery.calls) == ADMISSION_SAMPLE_FLOOR
    assert clock() <= 100.0
    waiting = [outcome for outcome in artifact.outcomes if outcome.outcome == OUTCOME_NOT_ATTEMPTED]
    assert len(waiting) == 20 - ADMISSION_SAMPLE_FLOOR


def test_concurrent_loop_matches_serial_artifacts_for_same_fixture() -> None:
    candidates = [_candidate(f"candidate-{index:02d}") for index in range(6)]

    def _run(max_workers: int) -> dict[str, Any]:
        stage = GroundedDescriptionStage(
            discovery=ReplayDiscovery(),
            fetch=ReplayFetch(),
            synthesizer=ReplaySynthesizer(),
            monotonic=FrozenClock(),
            stage_budget_seconds=900.0,
            max_workers=max_workers,
        )
        artifact = stage.run(
            candidates,
            operation_id="parity-operation",
            operation_date="2026-09-14",
            queue=GroundedDescriptionQueue(),
        )
        return artifact.to_dict()

    assert _run(1) == _run(3)


def test_candidate_work_runs_concurrently_at_configured_worker_count() -> None:
    barrier = threading.Barrier(3, timeout=10.0)

    class BarrierDiscovery:
        def discover(self, candidate: dict[str, Any], **_: Any) -> list[Any]:
            barrier.wait()
            return []

    stage = GroundedDescriptionStage(
        discovery=BarrierDiscovery(),
        fetch=None,
        synthesizer=ReplaySynthesizer(),
        monotonic=FrozenClock(),
        stage_budget_seconds=900.0,
        max_workers=3,
    )
    candidates = [_candidate(f"candidate-{index:02d}") for index in range(3)]

    artifact = stage.run(
        candidates,
        operation_id="concurrency-operation",
        operation_date="2026-09-14",
        queue=GroundedDescriptionQueue(),
    )

    # All three workers had to be inside discovery at once to pass the
    # barrier. A serial loop would time out waiting for parties that never
    # arrive and the stage would raise instead of returning outcomes.
    assert len(artifact.outcomes) == 3
    assert all(outcome.outcome != OUTCOME_NOT_ATTEMPTED for outcome in artifact.outcomes)
    assert [outcome.candidate_id for outcome in artifact.outcomes] == sorted(
        outcome.candidate_id for outcome in artifact.outcomes
    )
