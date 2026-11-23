from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from autoscout.grounded_description import REASON_CANDIDATE_BUDGET_EXHAUSTED
from autoscout.grounded_description import REASON_CODES
from autoscout.grounded_description import REASON_INVALID_DESCRIPTION
from autoscout.grounded_description import REASON_LABELS_RU
from autoscout.grounded_description import REASON_SEARCH_OR_FETCH_FAILURE
from autoscout.grounded_description import REASON_SYNTHESIS_FAILURE
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import QueueEntry
from autoscout.grounded_description import StageArtifact
from autoscout.grounded_description import StageOutcome
from autoscout.grounded_description import SynthesisResult
from autoscout.grounded_description import committed_source_classifier
from autoscout.grounded_description import validate_stage_artifact


def test_issue_517_reason_constants_are_public_and_labelled() -> None:
    assert REASON_SYNTHESIS_FAILURE in REASON_CODES
    assert REASON_INVALID_DESCRIPTION in REASON_CODES
    assert REASON_LABELS_RU[REASON_SYNTHESIS_FAILURE]
    assert REASON_LABELS_RU[REASON_INVALID_DESCRIPTION]


def test_stage_artifact_round_trips_reason_detail() -> None:
    outcome = StageOutcome(
        candidate_id="candidate-1",
        outcome="attempted_no_description",
        reason_code=REASON_INVALID_DESCRIPTION,
        reason_detail="blank description",
        scheduling=QueueEntry(
            candidate_id="candidate-1",
            queue_entry_operation="op",
            queue_entry_date="2025-01-01",
            first_seen_date="2025-01-01",
            priority_label="D",
            attempt_status="attempted_no_description",
            reason_code=REASON_INVALID_DESCRIPTION,
        ),
    )
    restored = StageArtifact.from_mapping(
        {
            "operation_id": "op",
            "operation_date": "2025-01-01",
            "outcomes": [outcome.to_dict()],
            "queue": [],
        }
    )
    assert restored.outcomes[0].reason_detail == "blank description"
    assert "reason_detail" not in StageOutcome(candidate_id="candidate-2", outcome="grounded").to_dict()


def test_refusal_rejects_unknown_reason() -> None:
    with pytest.raises(ValueError):
        GroundedDescriptionStage._refusal(
            {"identity_key": "identity"},
            QueueEntry("candidate-1", "op", "2025-01-01", "2025-01-01", "D"),
            "unknown-reason",
        )


def test_issue_517_public_exports_include_new_reasons() -> None:
    import autoscout.grounded_description as module

    assert "REASON_SOURCE_DENYLISTED" in module.__all__
    assert "REASON_SYNTHESIS_FAILURE" in module.__all__
    assert "REASON_INVALID_DESCRIPTION" in module.__all__


def test_not_attempted_serialization_keeps_scheduling_and_unknown_timing_shape() -> None:
    value = StageOutcome(candidate_id="c", outcome="not_attempted", reason_detail="must not appear").to_dict()
    # Timing is now recorded for every outcome, while unavailable phases stay null.
    assert set(value) == {"candidate_id", "outcome", "reason_code", "candidate_timing"}
    assert value["candidate_timing"] == {
        "discovery_seconds": None,
        "fetch_seconds": None,
        "synthesis_seconds": None,
        "total_seconds": None,
    }


FIXTURES = Path(__file__).parent / "fixtures" / "issue-502-agreeing-subset"
URL = (
    "https://bookshop.org/p/books/exhumed-unearthing-the-history-of-the-american-vampire-aaron-mahnke/c7e72bebb737534a"
)


class _Transport:
    def request(self, url: str, **_: object) -> HTTPResponse:
        return HTTPResponse(
            url, 200, {"content-type": "text/html; charset=utf-8"}, (FIXTURES / "bookshop-exhumed.html").read_bytes()
        )


class _Discovery:
    def discover(self, _candidate: Mapping[str, Any], **_: object) -> Sequence[DiscoveryResult]:
        classifier = committed_source_classifier()
        metadata = classifier.classify(URL)
        return [DiscoveryResult(URL, metadata["source_kind"], metadata["source_family"], metadata["owner_id"], "en")]


CANDIDATE = {
    "identity_key": "exhumed|aaron mahnke",
    "candidate_id": "exhumed|aaron mahnke",
    "title": "Exhumed",
    "primary_authors": ["Aaron Mahnke"],
    "first_seen_date": "2026-08-17",
}


@pytest.mark.parametrize("mode", ["blank", "exception"])
def test_issue_517_real_stage_rejects_synthesis_refusal(mode: str) -> None:
    class Synth:
        def synthesize(self, *_: object, **__: object) -> SynthesisResult:
            if mode == "exception":
                raise RuntimeError("secret details")
            return SynthesisResult("   ")

    artifact = GroundedDescriptionStage(_Discovery(), BoundedHTTPFetcher(transport=_Transport()), Synth()).run(
        [CANDIDATE], operation_id="op", operation_date="2026-08-17", force_candidates=True
    )
    outcome = artifact.outcomes[0]
    assert outcome.outcome == "attempted_no_description"
    assert outcome.reason_code == (REASON_SYNTHESIS_FAILURE if mode == "exception" else REASON_INVALID_DESCRIPTION)
    assert outcome.reason_detail == (
        "synthesizer raised RuntimeError" if mode == "exception" else "empty or whitespace-only value rejected"
    )
    assert REASON_SEARCH_OR_FETCH_FAILURE not in outcome.reason_code
    assert "secret details" not in str(artifact.to_dict())
    assert validate_stage_artifact(artifact)


@pytest.mark.parametrize("mode", ["blank", "exception"])
def test_issue_517_synthesis_envelope_overrun_refuses_without_synthesis_error(mode: str) -> None:
    class Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = Clock()

    class Synth:
        def synthesize(self, *_: object, **__: object) -> SynthesisResult:
            clock.value = 46.0
            if mode == "exception":
                raise RuntimeError("secret details")
            return SynthesisResult("   ")

    artifact = GroundedDescriptionStage(
        _Discovery(),
        BoundedHTTPFetcher(transport=_Transport()),
        Synth(),
        monotonic=clock,
        candidate_envelope_seconds=45,
    ).run([CANDIDATE], operation_id="op", operation_date="2026-08-17", force_candidates=True)
    outcome = artifact.outcomes[0]
    assert outcome.outcome == "attempted_no_description"
    assert outcome.reason_code == REASON_CANDIDATE_BUDGET_EXHAUSTED
    assert outcome.reason_code not in {
        REASON_SEARCH_OR_FETCH_FAILURE,
        REASON_INVALID_DESCRIPTION,
        REASON_SYNTHESIS_FAILURE,
    }
    expected_detail = "synthesizer raised RuntimeError" if mode == "exception" else ""
    assert outcome.reason_detail.startswith(expected_detail)
    assert "timing: discovery=" in outcome.reason_detail
    assert "fetch=0.0" in outcome.reason_detail
    assert "synthesis=46.0" in outcome.reason_detail
    assert "total=46.0" in outcome.reason_detail
    assert validate_stage_artifact(artifact)


def test_issue_517_fixture_same_owner_overrun_preserves_gate_rejection() -> None:
    amazon_url = "https://www.amazon.com/Exhumed-Unearthing-History-American-Vampire/dp/B0G4P2VQ4P"
    us_amazon_url = "https://us.amazon.com/Exhumed-Unearthing-History-American-Vampire-ebook/dp/B0GFP7VDW9"
    classifier = committed_source_classifier()
    results = [
        DiscoveryResult(
            url,
            (metadata := classifier.classify(url))["source_kind"],
            metadata["source_family"],
            metadata["owner_id"],
            "en",
        )
        for url in (amazon_url, us_amazon_url)
    ]

    class Discovery:
        def discover(self, _candidate: Mapping[str, Any], **_: object) -> Sequence[DiscoveryResult]:
            return results

    class Transport:
        def request(self, url: str, **_: object) -> HTTPResponse:
            filename = "amazon-exhumed.html" if url == amazon_url else "us-amazon-exhumed-ebook.html"
            return HTTPResponse(
                url, 200, {"content-type": "text/html; charset=utf-8"}, (FIXTURES / filename).read_bytes()
            )

    class Clock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = Clock()

    class Synth:
        def synthesize(self, *_: object, **__: object) -> SynthesisResult:
            clock.value = 46.0
            return SynthesisResult("Описание")

    artifact = GroundedDescriptionStage(
        Discovery(),
        BoundedHTTPFetcher(transport=Transport()),
        Synth(),
        monotonic=clock,
        candidate_envelope_seconds=45,
    ).run([CANDIDATE], operation_id="op", operation_date="2026-08-17", force_candidates=True)
    outcome = artifact.outcomes[0]
    assert outcome.reason_code == REASON_CANDIDATE_BUDGET_EXHAUSTED
    assert any(reason.reason_code == "fewer_than_two_agreeing_sources" for reason in outcome.rejected_source_reasons)
