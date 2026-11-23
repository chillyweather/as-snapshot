"""Regression coverage for grounded stage timing measurement."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from state_store_support import seed_state

from autoscout.generate_descriptions import generate_descriptions
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import CandidateTiming
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import GroundedDescriptionStageService
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import StageArtifact
from autoscout.grounded_description import SynthesisResult
from autoscout.grounded_description import committed_source_classifier
from autoscout.grounded_description import validate_stage_artifact


class FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class MeasurementFailureClock(FakeClock):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        if self.calls == 5:
            raise RuntimeError("clock unavailable for phase measurement")
        return super().__call__()


class NoResultsDiscovery:
    def discover(self, candidate: dict[str, Any], **_: Any) -> list[DiscoveryResult]:
        del candidate
        return []


class WaitingClock(FakeClock):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def __call__(self) -> float:
        current = super().__call__()
        self.calls += 1
        if self.calls == 1:
            self.advance(1.0)
        return current


class FixtureTransport:
    def request(self, url: str, **_: Any) -> HTTPResponse:
        fixture = Path(__file__).parent / "fixtures" / "issue-502-agreeing-subset" / "bookshop-exhumed.html"
        return HTTPResponse(
            url,
            200,
            {"content-type": "text/html; charset=utf-8"},
            fixture.read_bytes(),
        )


class FixtureDiscovery:
    url = (
        "https://bookshop.org/p/books/exhumed-unearthing-the-history-of-the-american-vampire-aaron-mahnke/"
        "c7e72bebb737534a"
    )

    def discover(self, _candidate: dict[str, Any], **_: Any) -> list[DiscoveryResult]:
        classifier = committed_source_classifier()
        metadata = classifier.classify(self.url)
        return [
            DiscoveryResult(
                self.url,
                metadata["source_kind"],
                metadata["source_family"],
                metadata["owner_id"],
                "en",
            )
        ]


class FixtureSynthesizer:
    def synthesize(self, *_: Any, **__: Any) -> SynthesisResult:
        return SynthesisResult(
            "Описание книги о страхе, истории и вампирах. "
            "Второй фрагмент раскрывает документальную основу, называет проверенные "
            "страницы и уточняет редакционный ракурс книги. "
            "Третье предложение закрепляет вывод и показывает, почему книга попала в обзор."
        )


class TimedDiscovery:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock

    def discover(self, candidate: dict[str, Any], **_: Any) -> list[DiscoveryResult]:
        self.clock.advance(2.0)
        return [
            DiscoveryResult(
                url="https://source.example.test/timed-work",
                source_kind="review",
                source_family="review",
                owner_id="review-owner",
                source_language="en",
                title=str(candidate["title"]),
                primary_authors=tuple(candidate["primary_authors"]),
            )
        ]


class TimedFetch:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock

    def fetch(self, result: DiscoveryResult, **kwargs: Any) -> dict[str, Any]:
        self.clock.advance(3.0)
        return {
            "url": result.url,
            "source_kind": result.source_kind,
            "source_family": result.source_family,
            "owner_id": result.owner_id,
            "source_language": result.source_language,
            "title": result.title,
            "primary_authors": list(result.primary_authors),
            "subject_text": "A grounded source about the timed work by Alex Author.",
            "display_text": "Timed Work by Alex Author. Evidence about memory and migration.",
            "checked_date": kwargs["checked_date"],
            "classification": "classified",
        }


class TimedSynthesizer:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock

    def synthesize(self, candidate: dict[str, Any], **_: Any) -> SynthesisResult:
        self.clock.advance(5.0)
        return SynthesisResult(f"Описание книги {candidate['title']} о памяти и миграции.")


def _candidate() -> dict[str, Any]:
    return {
        "candidate_id": "timed-work|alex-author",
        "identity_key": "Timed Work|Alex Author",
        "title": "Timed Work",
        "primary_authors": ["Alex Author"],
        "first_seen_date": "2026-08-17",
        "update_delta": "new",
        "content_description": "",
    }


def _timed_stage(clock: FakeClock) -> GroundedDescriptionStage:
    return GroundedDescriptionStage(
        discovery=TimedDiscovery(clock),
        fetch=TimedFetch(clock),
        synthesizer=TimedSynthesizer(clock),
        monotonic=clock,
        max_workers=1,
    )


def test_generate_descriptions_reports_injected_stage_and_candidate_elapsed(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    state_dir.mkdir()
    output_dir.mkdir()
    candidate = _candidate()
    seed_state(state_dir, candidate_history=[candidate])
    clock = FakeClock()

    generate_descriptions(
        state_dir,
        output_dir,
        discovery=TimedDiscovery(clock),
        fetch=TimedFetch(clock),
        synthesizer=TimedSynthesizer(clock),
        monotonic=clock,
        preflight=lambda: True,
        effective_date="2026-08-17",
        max_workers=1,
    )

    artifact = json.loads((output_dir / "grounded-description-stage.json").read_text(encoding="utf-8"))
    assert artifact["timing"]["elapsed_seconds"] == 10.0
    assert artifact["outcomes"][0]["candidate_timing"] == {
        "discovery_seconds": 2.0,
        "fetch_seconds": 3.0,
        "synthesis_seconds": 5.0,
        "total_seconds": 10.0,
    }


def test_weekly_stage_reports_injected_stage_and_candidate_elapsed() -> None:
    clock = FakeClock()
    artifact = _timed_stage(clock).run(
        [_candidate()],
        operation_id="weekly-operation",
        operation_date="2026-08-17",
    )

    assert artifact.elapsed_seconds == 10.0
    assert artifact.outcomes[0].candidate_timing.to_dict() == {
        "discovery_seconds": 2.0,
        "fetch_seconds": 3.0,
        "synthesis_seconds": 5.0,
        "total_seconds": 10.0,
    }


def test_weekly_waiting_outcome_has_unknown_candidate_timing() -> None:
    clock = WaitingClock()
    # Issue #633 admits on the measured per-candidate cost, not the fixed
    # envelope, so a zero budget is what genuinely cannot hold one candidate.
    artifact = GroundedDescriptionStage(
        discovery=NoResultsDiscovery(),
        monotonic=clock,
        stage_budget_seconds=0.0,
        candidate_envelope_seconds=45.0,
    ).run(
        [_candidate()],
        operation_id="waiting-operation",
        operation_date="2026-08-17",
        force_candidates=True,
    )

    outcome = artifact.outcomes[0]
    assert outcome.outcome == "not_attempted"
    assert outcome.reason_code == "not_attempted_stage_budget"
    assert artifact.counts == {
        "grounded": 0,
        "single_source": 0,
        "attempted_no_description": 0,
        "not_attempted": 1,
        "evidence_unchanged": 0,
    }
    assert outcome.candidate_timing.to_dict() == {
        "discovery_seconds": None,
        "fetch_seconds": None,
        "synthesis_seconds": None,
        "total_seconds": None,
    }


def test_timing_does_not_change_committed_fixture_decisions() -> None:
    candidate = {
        "candidate_id": "exhumed|aaron mahnke",
        "identity_key": "exhumed|aaron mahnke",
        "title": "Exhumed",
        "primary_authors": ["Aaron Mahnke"],
        "first_seen_date": "2026-08-17",
    }
    artifact = GroundedDescriptionStage(
        discovery=FixtureDiscovery(),
        fetch=BoundedHTTPFetcher(transport=FixtureTransport()),
        synthesizer=FixtureSynthesizer(),
        monotonic=FakeClock(0.0),
    ).run(
        [candidate],
        operation_id="fixture-decision-operation",
        operation_date="2026-08-17",
        force_candidates=True,
    )

    decisions_without_timing = {
        "outcomes": [(item.outcome, item.reason_code) for item in artifact.outcomes],
        "counts": artifact.counts,
        "reason_counts": artifact.reason_counts,
    }
    assert decisions_without_timing == {
        "outcomes": [("single_source", "")],
        "counts": {
            "grounded": 0,
            "single_source": 1,
            "attempted_no_description": 0,
            "not_attempted": 0,
            "evidence_unchanged": 0,
        },
        "reason_counts": {},
    }


def test_evidence_unchanged_timing_has_total_without_synthesis() -> None:
    candidate = _candidate()
    candidate["description_provenance"] = {
        "provenance": "grounded",
        "evidence_basis": ["https://source.example.test/timed-work"],
    }
    candidate["grounded_outcome"] = "single_source"
    clock = FakeClock()

    artifact = _timed_stage(clock).run(
        [candidate],
        operation_id="unchanged-operation",
        operation_date="2026-08-17",
        force_candidates=True,
    )

    outcome = artifact.outcomes[0]
    assert outcome.outcome == "evidence_unchanged"
    assert outcome.candidate_timing.to_dict() == {
        "discovery_seconds": 2.0,
        "fetch_seconds": 3.0,
        "synthesis_seconds": None,
        "total_seconds": 5.0,
    }


def test_clock_failure_reports_unknown_phase_timing() -> None:
    clock = MeasurementFailureClock()
    artifact = GroundedDescriptionStage(discovery=NoResultsDiscovery(), monotonic=clock).run(
        [_candidate()],
        operation_id="clock-failure-operation",
        operation_date="2026-08-17",
        force_candidates=True,
    )

    timing = artifact.outcomes[0].candidate_timing.to_dict()
    assert timing["discovery_seconds"] is None
    assert timing["discovery_seconds"] != 0.0


def test_candidate_timing_round_trips_and_rejects_invalid_values() -> None:
    clock = FakeClock()
    artifact = _timed_stage(clock).run(
        [_candidate()],
        operation_id="round-trip-operation",
        operation_date="2026-08-17",
    )
    serialized = artifact.to_dict()
    restored = StageArtifact.from_mapping(serialized)

    assert restored.to_dict() == serialized
    assert validate_stage_artifact(serialized)
    serialized["outcomes"][0]["candidate_timing"]["fetch_seconds"] = -1.0
    assert not validate_stage_artifact(serialized)


def test_missing_candidate_timing_is_unknown_not_zero() -> None:
    legacy = {
        "operation_id": "legacy-operation",
        "operation_date": "2026-08-17",
        "outcomes": [
            {
                "candidate_id": "legacy-candidate",
                "identity_key": "Legacy Work|Alex Author",
                "outcome": "attempted_no_description",
                "reason_code": "no_results",
            }
        ],
        "queue": [],
    }
    artifact = StageArtifact.from_mapping(legacy)

    assert validate_stage_artifact(legacy)
    assert StageArtifact.from_mapping(artifact.to_dict()).to_dict() == artifact.to_dict()
    timing = artifact.outcomes[0].to_dict()["candidate_timing"]
    assert timing == {
        "discovery_seconds": None,
        "fetch_seconds": None,
        "synthesis_seconds": None,
        "total_seconds": None,
    }
    assert timing["discovery_seconds"] != 0.0


class EnvelopeBurningClock(FakeClock):
    """A clock that burns the candidate envelope inside discovery.

    Every reading advances by ``step``, so the candidate deadline passes while
    discovery is still running. This reproduces the shape issue #542 is about:
    the candidate did no expensive work of its own and still ran out of time.
    """

    def __init__(self, step: float = 20.0) -> None:
        super().__init__()
        self.step = step

    def __call__(self) -> float:
        self.advance(self.step)
        return self.value


def test_generate_descriptions_does_not_charge_executor_queue_wait_to_candidate_timing(tmp_path: Path) -> None:
    class QueueWaitingDiscovery:
        def discover(self, candidate: dict[str, Any], **_: Any) -> list[DiscoveryResult]:
            return [
                DiscoveryResult(
                    url=f"https://source.example.test/{candidate['candidate_id']}",
                    source_kind="review",
                    source_family="review",
                    owner_id="review-owner",
                    source_language="en",
                    title=str(candidate["title"]),
                    primary_authors=tuple(candidate["primary_authors"]),
                )
            ]

    class QueueWaitingFetch:
        def __init__(self, clock: FakeClock) -> None:
            self.clock = clock

        def fetch(self, result: DiscoveryResult, **kwargs: Any) -> dict[str, Any]:
            self.clock.advance(21.0 if result.url.endswith("timed-work|alex-author") else 2.0)
            return {
                "url": result.url,
                "source_kind": result.source_kind,
                "source_family": result.source_family,
                "owner_id": result.owner_id,
                "source_language": result.source_language,
                "title": result.title,
                "primary_authors": list(result.primary_authors),
                "subject_text": f"A grounded source about {result.title} by Alex Author.",
                "display_text": f"{result.title} by Alex Author. Evidence about memory and migration.",
                "checked_date": kwargs["checked_date"],
                "classification": "classified",
            }

    class QueueWaitingSynthesizer:
        def __init__(self, clock: FakeClock) -> None:
            self.clock = clock

        def synthesize(self, candidate: dict[str, Any], **_: Any) -> SynthesisResult:
            self.clock.advance(21.0 if candidate["candidate_id"] == "timed-work|alex-author" else 2.0)
            return SynthesisResult(
                f"Описание книги {candidate['title']} о памяти и миграции. "
                "Второй фрагмент раскрывает документальную основу, называет проверенные "
                "страницы и уточняет редакционный ракурс книги. "
                "Третье предложение закрепляет вывод и показывает, почему книга попала в обзор."
            )

    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    state_dir.mkdir()
    output_dir.mkdir()
    candidates = [
        _candidate(),
        {
            **_candidate(),
            "candidate_id": "second|alex-author",
            "identity_key": "Second|Alex Author",
            "title": "Second",
        },
    ]
    seed_state(state_dir, candidate_history=candidates)
    clock = FakeClock()
    service = GroundedDescriptionStageService(
        GroundedDescriptionStage(
            discovery=QueueWaitingDiscovery(),
            fetch=QueueWaitingFetch(clock),
            synthesizer=QueueWaitingSynthesizer(clock),
            monotonic=clock,
            max_workers=1,
        )
    )

    # The real generator uses ThreadPoolExecutor with one worker. The second
    # candidate starts after the first candidate's 42 seconds of own work.
    generate_descriptions(
        state_dir,
        output_dir,
        service=service,
        max_workers=1,
        monotonic=clock,
        preflight=lambda: True,
        effective_date="2026-08-17",
    )

    artifact = json.loads((output_dir / "grounded-description-stage.json").read_text(encoding="utf-8"))
    outcomes = {outcome["candidate_id"]: outcome for outcome in artifact["outcomes"]}
    assert outcomes["timed-work|alex-author"]["outcome"] == "single_source"
    assert outcomes["timed-work|alex-author"]["candidate_timing"] == {
        "discovery_seconds": 0.0,
        "fetch_seconds": 21.0,
        "synthesis_seconds": 21.0,
        "total_seconds": 42.0,
    }
    assert outcomes["second|alex-author"]["outcome"] == "single_source"
    assert outcomes["second|alex-author"]["candidate_timing"] == {
        "discovery_seconds": 0.0,
        "fetch_seconds": 2.0,
        "synthesis_seconds": 2.0,
        "total_seconds": 4.0,
    }


def test_candidate_timing_describes_all_phases() -> None:
    timing = CandidateTiming(discovery_seconds=2.0, fetch_seconds=None, synthesis_seconds=5.0, total_seconds=7.0)

    assert timing.describe() == "discovery=2.0; fetch=unknown; synthesis=5.0; total=7.0"


def test_budget_exhausted_outcome_reports_where_the_time_went() -> None:
    """The refusal issue #542 is about must say what consumed the envelope.

    ``candidate_budget_exhausted`` reads as "this candidate was slow". The
    measured case in #542 is a candidate whose only in-policy page grounds it
    alone in 1.4 seconds, which then exhausted a 45 second envelope. Only a
    per-phase breakdown separates that from a genuinely slow candidate, so this
    outcome carrying its timing is the whole point of the instrumentation.

    Stage-budget exhaustion is attributed to discovery. Fetch and synthesis are
    unknown rather than zero, because they were never reached, and zero would
    read as "took no time".
    """
    stage = GroundedDescriptionStage(
        discovery=NoResultsDiscovery(),
        monotonic=EnvelopeBurningClock(),
        stage_budget_seconds=100.0,
        candidate_envelope_seconds=45.0,
    )

    artifact = stage.run(
        [_candidate()],
        operation_id="budget-exhausted-operation",
        operation_date="2026-08-21",
        force_candidates=True,
    )
    outcome = artifact.outcomes[0]
    timing = outcome.candidate_timing.to_dict()

    assert outcome.outcome == "attempted_no_description"
    assert outcome.reason_code == "candidate_budget_exhausted"
    assert timing["discovery_seconds"] == 20.0
    assert timing["fetch_seconds"] is None
    assert timing["synthesis_seconds"] is None
    assert timing["total_seconds"] == 100.0
    assert "timing: discovery=20.0; fetch=unknown; synthesis=unknown; total=100.0" in outcome.reason_detail
    # The stage total is measured independently of the candidate total.
    assert artifact.elapsed_seconds == 160.0
    # A budget refusal must still be serialisable and readable back.
    assert validate_stage_artifact(artifact.to_dict())
