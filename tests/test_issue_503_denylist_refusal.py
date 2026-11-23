"""Offline contract tests for issue #503: how classifier refusals are reported.

Since issue #689 the classifier refuses only denylisted pages, so every
refusal carries the denylist reason code (issue #691).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from state_store_support import read_state
from state_store_support import seed_state

from autoscout.generate_descriptions import ATTEMPT_FIELD
from autoscout.generate_descriptions import generate_descriptions
from autoscout.grounded_description import OUTCOME_ATTEMPTED_NO_DESCRIPTION
from autoscout.grounded_description import OUTCOME_GROUNDED
from autoscout.grounded_description import REASON_CODES
from autoscout.grounded_description import REASON_SEARCH_OR_FETCH_FAILURE
from autoscout.grounded_description import REASON_SEARCH_SNIPPET
from autoscout.grounded_description import REASON_SOURCE_DENYLISTED
from autoscout.grounded_description import DurableTarget
from autoscout.grounded_description import FetchedSource
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import GroundedDescriptionStageService
from autoscout.grounded_description import RejectedSource
from autoscout.grounded_description import StageArtifact
from autoscout.grounded_description import SynthesisResult
from autoscout.grounded_description import evaluate_grounding_gate
from autoscout.grounded_description import reason_label
from autoscout.grounded_description import validate_stage_artifact

CHECKED_DATE = "2026-09-01"


def _target() -> DurableTarget:
    return DurableTarget("candidate-001", "Work 1", ("Alex Author",), {"title": "Work 1"})


def _candidate() -> dict[str, Any]:
    return {
        "candidate_id": "candidate-001",
        "identity_key": "Work 1|Alex Author",
        "title": "Work 1",
        "primary_authors": ["Alex Author"],
        "update_delta": "new",
        "priority_label": "D",
        "first_seen_date": CHECKED_DATE,
        "content_description": "",
        "short_description": "Editor annotation",
    }


def _source(
    url: str,
    *,
    owner: str,
    classification: str = "classified",
    is_search_snippet: bool = False,
) -> FetchedSource:
    return FetchedSource(
        url=url,
        source_kind="review",
        source_family="review",
        owner_id=owner,
        source_language="en",
        title="Work 1",
        primary_authors=("Alex Author",),
        subject_text="Memory migration and social life.",
        checked_date=CHECKED_DATE,
        classification=classification,
        is_search_snippet=is_search_snippet,
    )


def _discovery(url: str, *, owner: str, classification: str = "classified") -> dict[str, Any]:
    return {
        "url": url,
        "source_kind": "review",
        "source_family": "review",
        "owner_id": owner,
        "classification": classification,
    }


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class FakeDiscovery:
    def __init__(self, results: list[dict[str, Any]]) -> None:
        self.results = results

    def discover(self, _candidate: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return list(self.results)


class FakeFetch:
    def fetch(self, result: Any, **kwargs: Any) -> FetchedSource:
        return FetchedSource.from_mapping(
            {
                "url": result.url,
                "source_kind": result.source_kind,
                "source_family": result.source_family,
                "owner_id": result.owner_id,
                "source_language": "en",
                "title": "Work 1",
                "primary_authors": ["Alex Author"],
                "subject_text": "Memory migration and social life.",
                "checked_date": kwargs.get("checked_date", CHECKED_DATE),
                "classification": "classified",
            }
        )


class FakeSynthesizer:
    def synthesize(self, _candidate: Any, sources: Any) -> SynthesisResult:
        del sources
        return SynthesisResult(
            "Русское описание книги о памяти и миграции. "
            "Второй фрагмент раскрывает документальную основу, называет проверенные "
            "страницы и уточняет редакционный ракурс книги. "
            "Третье предложение закрепляет вывод и показывает, почему книга попала в обзор."
        )


class RaisingFetch:
    def fetch(self, _result: Any, **_kwargs: Any) -> FetchedSource:
        raise RuntimeError("network failure")


def test_gate_reports_unclassified_source_as_denylist_refusal() -> None:
    source = _source("https://outside.example.test/work", owner="outside", classification="unknown")

    result = evaluate_grounding_gate(_target(), [source])

    assert result.accepted is False
    assert result.reason_code == REASON_SOURCE_DENYLISTED
    assert result.rejected_sources == (RejectedSource("https://outside.example.test/work", REASON_SOURCE_DENYLISTED),)


def test_gate_reports_search_snippet_as_its_own_reason() -> None:
    source = _source("https://search.example.test/work", owner="search", is_search_snippet=True)

    result = evaluate_grounding_gate(_target(), [source])

    assert result.accepted is False
    # Issue #513 separates snippet from fetch failure: a snippet is unusable
    # source evidence, not a network fetch problem.
    assert result.reason_code == REASON_SEARCH_SNIPPET
    assert result.rejected_sources == (RejectedSource("https://search.example.test/work", REASON_SEARCH_SNIPPET),)


def test_stage_refuses_only_denylisted_results_and_validates_artifact() -> None:
    excluded_url = "https://outside.example.test/work"
    stage = GroundedDescriptionStage(
        discovery=FakeDiscovery([_discovery(excluded_url, owner="outside", classification="unknown")]),
        fetch=FakeFetch(),
        monotonic=FakeClock(),
    )

    artifact = stage.run([_candidate()], operation_id="op", operation_date=CHECKED_DATE)
    outcome = artifact.outcomes[0]

    assert outcome.outcome == OUTCOME_ATTEMPTED_NO_DESCRIPTION
    assert outcome.reason_code == REASON_SOURCE_DENYLISTED
    assert outcome.rejected_source_reasons == (RejectedSource(excluded_url, REASON_SOURCE_DENYLISTED),)
    assert artifact.reason_counts == {REASON_SOURCE_DENYLISTED: 1}
    assert validate_stage_artifact(artifact.to_dict())


def test_stage_preserves_denylist_refusals_when_classified_pair_grounds() -> None:
    excluded_urls = (
        "https://outside.example.test/work",
        "https://second-outside.example.test/work",
    )
    results = [
        _discovery(excluded_urls[0], owner="outside", classification="unknown"),
        _discovery(excluded_urls[0] + "#duplicate", owner="outside-duplicate", classification="unclassifiable"),
        _discovery(excluded_urls[1], owner="second-outside", classification=""),
        _discovery("https://review.example.test/work", owner="review"),
        _discovery("https://publisher.example.test/work", owner="publisher"),
    ]
    stage = GroundedDescriptionStage(
        discovery=FakeDiscovery(results),
        fetch=FakeFetch(),
        synthesizer=FakeSynthesizer(),
        monotonic=FakeClock(),
    )

    artifact = stage.run([_candidate()], operation_id="op", operation_date=CHECKED_DATE)
    outcome = artifact.outcomes[0]

    assert outcome.outcome == OUTCOME_GROUNDED
    assert tuple(item.canonical_url for item in outcome.rejected_source_reasons) == excluded_urls
    assert all(item.reason_code == REASON_SOURCE_DENYLISTED for item in outcome.rejected_source_reasons)
    serialized = artifact.to_dict()
    round_tripped = StageArtifact.from_mapping(serialized)
    assert round_tripped.to_dict() == serialized
    assert validate_stage_artifact(serialized)


def test_validate_stage_artifact_rejects_unknown_rejected_reason_code() -> None:
    stage = GroundedDescriptionStage(
        discovery=FakeDiscovery(
            [
                _discovery("https://outside.example.test/work", owner="outside", classification="unknown"),
                _discovery("https://review.example.test/work", owner="review"),
                _discovery("https://publisher.example.test/work", owner="publisher"),
            ]
        ),
        fetch=FakeFetch(),
        synthesizer=FakeSynthesizer(),
        monotonic=FakeClock(),
    )

    artifact = stage.run([_candidate()], operation_id="op", operation_date=CHECKED_DATE)
    serialized = artifact.to_dict()
    assert validate_stage_artifact(serialized)

    serialized["outcomes"][0]["rejected_source_reasons"][0]["reason_code"] = "not_a_bounded_reason"
    assert not validate_stage_artifact(serialized)

    invalid_top_level_reason = artifact.to_dict()
    invalid_top_level_reason["outcomes"][0]["reason_code"] = "not_a_bounded_reason"
    assert not validate_stage_artifact(invalid_top_level_reason)


def test_stage_zero_results_and_fetch_failure_keep_existing_reasons() -> None:
    no_results = GroundedDescriptionStage(discovery=FakeDiscovery([]), monotonic=FakeClock()).run(
        [_candidate()], operation_id="op", operation_date=CHECKED_DATE
    )
    assert no_results.outcomes[0].reason_code == "no_results"

    fetch_failure = GroundedDescriptionStage(
        discovery=FakeDiscovery([_discovery("https://review.example.test/work", owner="review")]),
        fetch=RaisingFetch(),
        monotonic=FakeClock(),
    ).run([_candidate()], operation_id="op", operation_date=CHECKED_DATE)
    assert fetch_failure.outcomes[0].reason_code == REASON_SEARCH_OR_FETCH_FAILURE


def test_generate_descriptions_attempt_history_keeps_denylist_refusal_reason(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    state_dir.mkdir()
    output_dir.mkdir()
    seed_state(state_dir, candidate_history=[_candidate()])
    service = GroundedDescriptionStageService(
        GroundedDescriptionStage(
            discovery=FakeDiscovery(
                [_discovery("https://outside.example.test/work", owner="outside", classification="unknown")]
            ),
            fetch=FakeFetch(),
            monotonic=FakeClock(),
        )
    )

    generate_descriptions(
        state_dir,
        output_dir,
        service=service,
        preflight=lambda: True,
        effective_date=CHECKED_DATE,
        max_workers=1,
    )

    saved = read_state(state_dir, "candidate_history")
    attempt = saved[0][ATTEMPT_FIELD][0]
    assert attempt["reason_code"] == REASON_SOURCE_DENYLISTED
    assert attempt["rejected_reason_counts"] == {REASON_SOURCE_DENYLISTED: 1}


def test_generate_descriptions_attempt_history_keeps_three_key_shape_without_rejected_sources(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    state_dir.mkdir()
    output_dir.mkdir()
    seed_state(state_dir, candidate_history=[_candidate()])
    service = GroundedDescriptionStageService(
        GroundedDescriptionStage(discovery=FakeDiscovery([]), monotonic=FakeClock())
    )

    generate_descriptions(
        state_dir,
        output_dir,
        service=service,
        preflight=lambda: True,
        effective_date=CHECKED_DATE,
        max_workers=1,
    )

    attempt = read_state(state_dir, "candidate_history")[0][ATTEMPT_FIELD][0]
    assert attempt == {
        "date": CHECKED_DATE,
        "outcome": OUTCOME_ATTEMPTED_NO_DESCRIPTION,
        "reason_code": "no_results",
    }


def test_generate_descriptions_attempt_history_keeps_denylist_refusal_counts_for_grounded_outcome(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "output"
    state_dir.mkdir()
    output_dir.mkdir()
    seed_state(state_dir, candidate_history=[_candidate()])
    service = GroundedDescriptionStageService(
        GroundedDescriptionStage(
            discovery=FakeDiscovery(
                [
                    _discovery("https://outside.example.test/work", owner="outside", classification="unknown"),
                    _discovery(
                        "https://second-outside.example.test/work",
                        owner="second-outside",
                        classification="unclassifiable",
                    ),
                    _discovery("https://review.example.test/work", owner="review"),
                    _discovery("https://publisher.example.test/work", owner="publisher"),
                ]
            ),
            fetch=FakeFetch(),
            synthesizer=FakeSynthesizer(),
            monotonic=FakeClock(),
        )
    )

    generate_descriptions(
        state_dir,
        output_dir,
        service=service,
        preflight=lambda: True,
        effective_date=CHECKED_DATE,
        max_workers=1,
    )

    attempt = read_state(state_dir, "candidate_history")[0][ATTEMPT_FIELD][0]
    assert attempt == {
        "date": CHECKED_DATE,
        "outcome": OUTCOME_GROUNDED,
        "reason_code": "",
        "rejected_reason_counts": {REASON_SOURCE_DENYLISTED: 2},
    }


def test_denylist_refusal_reason_is_bounded_and_labeled() -> None:
    assert REASON_SOURCE_DENYLISTED in REASON_CODES
    assert reason_label(REASON_SOURCE_DENYLISTED) == "Источник в стоп-списке источников"
