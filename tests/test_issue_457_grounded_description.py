"""Offline contract tests for the issue #457 grounded description stage."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest
from state_store_support import seed_state

from autoscout.content_description import apply_description_artifacts
from autoscout.content_description import description_provenance_record
from autoscout.description_enrichment import _encode_work_items
from autoscout.grounded_description import CANDIDATE_ENVELOPE_SECONDS
from autoscout.grounded_description import DISCOVERY_BUDGET_SECONDS
from autoscout.grounded_description import GROUNDED_SYNTHESIS_PROMPT
from autoscout.grounded_description import OUTCOME_ATTEMPTED_NO_DESCRIPTION
from autoscout.grounded_description import OUTCOME_GROUNDED
from autoscout.grounded_description import OUTCOME_NOT_ATTEMPTED
from autoscout.grounded_description import OUTCOME_SINGLE_SOURCE
from autoscout.grounded_description import REASON_AUTHOR_MISMATCH
from autoscout.grounded_description import REASON_CANDIDATE_BUDGET_EXHAUSTED
from autoscout.grounded_description import REASON_FEWER_THAN_TWO
from autoscout.grounded_description import REASON_NOT_ATTEMPTED_STAGE_BUDGET
from autoscout.grounded_description import REASON_SEARCH_OR_FETCH_FAILURE
from autoscout.grounded_description import REASON_SEARCH_SNIPPET
from autoscout.grounded_description import REASON_SOURCE_DENYLISTED
from autoscout.grounded_description import REASON_SOURCE_IDENTITY_DISAGREEMENT
from autoscout.grounded_description import REASON_SUBJECT_DISAGREEMENT
from autoscout.grounded_description import REASON_TITLE_MISMATCH
from autoscout.grounded_description import SOURCE_INDEPENDENCE_REASON
from autoscout.grounded_description import AcceptedSource
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import CodexGroundedDescriptionSynthesizer
from autoscout.grounded_description import DurableTarget
from autoscout.grounded_description import GroundedDescriptionQueue
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import GroundedDescriptionStageService
from autoscout.grounded_description import HTTPDiscoveryProvider
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import StageArtifact
from autoscout.grounded_description import SynthesisResult
from autoscout.grounded_description import committed_source_classifier
from autoscout.grounded_description import evaluate_grounding_gate
from autoscout.grounded_description import validate_prominence_claim
from autoscout.grounded_description import validate_stage_artifact
from autoscout.html_review import build_html_review
from autoscout.review_rendering import assemble_cumulative_review
from autoscout.weekly_report_row import write_editor_csv

FIXTURES = Path(__file__).parent / "fixtures" / "issue-457"


def _fixture() -> dict[str, Any]:
    return json.loads((FIXTURES / "grounded-sources.json").read_text(encoding="utf-8"))


def _target() -> DurableTarget:
    target = _fixture()["target"]
    return DurableTarget(target["candidate_id"], target["title"], tuple(target["primary_authors"]), target)


def _source(
    url: str,
    *,
    owner: str,
    family: str = "default",
    title: str = "The Work",
    author: str = "Alex Author",
    subject: str = "memory migration",
    **kwargs: Any,
) -> dict[str, Any]:
    return {
        "url": url,
        "source_kind": "review",
        "source_family": family,
        "owner_id": owner,
        "source_language": "de",
        "title": title,
        "primary_authors": [author],
        "subject_text": subject,
        "subject_key": subject,
        "checked_date": "2026-09-01",
        **kwargs,
    }


def test_two_distinct_subject_bearing_sources_ground_subtitle_insensitively() -> None:
    fixture = _fixture()
    result = evaluate_grounding_gate(_target(), fixture["agreeing_sources"])

    assert result.accepted
    assert len(result.accepted_sources) == 2
    assert result.research_sources == (
        "https://review.example.test/books/the-work",
        "https://publisher.example.test/catalog/the-work",
    )
    assert result.accepted_sources[0].raw_title == "The Work"
    assert all(not source.display_text for source in result.accepted_sources)


def test_issue_457_part_2_grounding_decision_battery() -> None:
    target = _target()
    cases = [
        (
            "two owners different families",
            [
                _source("https://one.example.test/work", owner="one", family="review"),
                _source("https://two.example.test/work", owner="two", family="publisher"),
            ],
            True,
            OUTCOME_GROUNDED,
            "",
        ),
        (
            "two owners same family",
            [
                _source("https://one.example.test/work", owner="one", family="shared"),
                _source("https://two.example.test/work", owner="two", family="shared"),
            ],
            True,
            OUTCOME_GROUNDED,
            "",
        ),
        (
            "one source",
            [_source("https://one.example.test/work", owner="one", family="review")],
            True,
            OUTCOME_SINGLE_SOURCE,
            "",
        ),
        (
            "same owner two URLs",
            [
                _source("https://one.example.test/work", owner="one", family="shared"),
                _source("https://two.example.test/work", owner="one", family="shared"),
            ],
            True,
            OUTCOME_SINGLE_SOURCE,
            "",
        ),
        (
            "same canonical URL twice",
            [
                _source("https://one.example.test/work", owner="one", family="one"),
                _source("https://one.example.test/work", owner="two", family="two"),
            ],
            True,
            OUTCOME_SINGLE_SOURCE,
            "",
        ),
        (
            "same title different authors",
            [
                _source("https://one.example.test/work", owner="one", author="Author One"),
                _source("https://two.example.test/work", owner="two", author="Author Two"),
            ],
            False,
            "",
            # Both sources are rejected individually with the author mismatch
            # and no source joins the parsed set, so the pair-level code must
            # not hide which source mismatched the target (review round 2).
            REASON_AUTHOR_MISMATCH,
        ),
        (
            "contradictory subjects",
            [
                _source("https://one.example.test/work", owner="one", subject="memory migration"),
                _source("https://two.example.test/work", owner="two", subject="quantum physics"),
            ],
            False,
            "",
            REASON_SUBJECT_DISAGREEMENT,
        ),
        (
            "only source title mismatch",
            [_source("https://one.example.test/work", owner="one", title="Other Work")],
            False,
            "",
            REASON_TITLE_MISMATCH,
        ),
        (
            "only source author mismatch",
            [_source("https://one.example.test/work", owner="one", author="Other Author")],
            False,
            "",
            REASON_AUTHOR_MISMATCH,
        ),
        (
            "only source snippet",
            [_source("https://one.example.test/work", owner="one", is_search_snippet=True)],
            False,
            "",
            # Issue #513: snippet is unusable, not a fetch failure.
            REASON_SEARCH_SNIPPET,
        ),
        (
            "only source unclassified",
            [_source("https://one.example.test/work", owner="one", classification="unknown")],
            False,
            "",
            REASON_SOURCE_DENYLISTED,
        ),
        (
            "three sources two agreeing one disagreeing",
            [
                _source("https://one.example.test/work", owner="one", family="review"),
                _source("https://two.example.test/work", owner="two", family="publisher"),
                _source("https://three.example.test/work", owner="three", family="author", subject="quantum physics"),
            ],
            True,
            OUTCOME_GROUNDED,
            "",
        ),
    ]
    for name, sources, accepted, expected_outcome, expected_reason in cases:
        result = evaluate_grounding_gate(target, sources)
        assert result.accepted is accepted, name
        assert len(result.accepted_sources) == (
            {OUTCOME_GROUNDED: 2, OUTCOME_SINGLE_SOURCE: 1}.get(expected_outcome, 0)
        ), name
        assert expected_reason == (result.reason_code if not accepted else ""), name
        if name == "three sources two agreeing one disagreeing":
            assert result.research_sources == (
                "https://one.example.test/work",
                "https://two.example.test/work",
            )
        if name in {"same owner two URLs", "same canonical URL twice"}:
            assert any(item.reason_code == REASON_FEWER_THAN_TWO for item in result.rejected_sources)


def test_source_independence_reason_ignores_source_family() -> None:
    sources = [
        _source("https://one.example.test/work", owner="one", family="same-family"),
        _source("https://two.example.test/work", owner="two", family="same-family"),
    ]
    result = evaluate_grounding_gate(_target(), sources)

    assert result.accepted
    assert result.source_independence_evidence is not None
    assert result.source_independence_evidence.reason == SOURCE_INDEPENDENCE_REASON
    assert result.source_independence_evidence.source_families == ("same-family", "same-family")


def test_subject_key_can_supply_bounded_subject_evidence_without_subject_text() -> None:
    sources = [
        _source("https://one.example.test/work", owner="one", family="one"),
        _source("https://two.example.test/work", owner="two", family="two"),
    ]
    for source in sources:
        source["subject_text"] = ""
    result = evaluate_grounding_gate(_target(), sources)
    assert result.accepted


def test_subject_intersection_selects_a_consistent_pair_when_three_have_no_common_term() -> None:
    sources = [
        _source("https://one.example.test/work", owner="one", subject="memory migration"),
        _source("https://two.example.test/work", owner="two", subject="memory archive"),
        _source("https://three.example.test/work", owner="three", subject="migration archive"),
    ]

    result = evaluate_grounding_gate(_target(), sources)

    assert result.accepted
    assert result.research_sources == (
        "https://one.example.test/work",
        "https://three.example.test/work",
    )


def test_title_only_and_snippet_fail_closed_but_same_family_different_owners_ground() -> None:
    fixture = _fixture()
    assert evaluate_grounding_gate(_target(), [fixture["title_only"]]).reason_code == "insufficient_subject_text"
    assert evaluate_grounding_gate(_target(), [fixture["search_snippet"]]).reason_code != OUTCOME_GROUNDED
    one_family = [
        _source("https://one.example.test/a", owner="one", family="same", subject="memory migration"),
        _source("https://two.example.test/b", owner="two", family="same", subject="memory migration"),
    ]
    assert evaluate_grounding_gate(_target(), one_family).accepted


def test_mismatches_and_isbn_never_bypass_title_author_gate() -> None:
    assert (
        evaluate_grounding_gate(
            _target(),
            [
                _source("https://one.example.test/a", owner="one", family="one", title="Other Work"),
                _source("https://two.example.test/b", owner="two", family="two", title="Other Work"),
            ],
        ).reason_code
        == REASON_TITLE_MISMATCH
    )
    assert (
        evaluate_grounding_gate(
            _target(),
            [
                _source("https://one.example.test/a", owner="one", family="one", author="Walter Isaacson"),
                _source("https://two.example.test/b", owner="two", family="two", author="Walter Isaacson"),
            ],
        ).reason_code
        == REASON_AUTHOR_MISMATCH
    )
    isbn_sources = [
        {
            **_source("https://one.example.test/a", owner="one", family="one", title="Other Work", author="Other"),
            "isbn": "978-1",
        },
        {
            **_source("https://two.example.test/b", owner="two", family="two", title="Other Work", author="Other"),
            "isbn": "978-1",
        },
    ]
    assert evaluate_grounding_gate(_target(), isbn_sources).reason_code == REASON_TITLE_MISMATCH


def test_elon_musk_authors_remain_separate_and_title_mismatch_does_not_veto() -> None:
    elon = DurableTarget("elon", "Elon Musk", ("Ashlee Vance",), {"title": "Elon Musk"})
    sources = [
        _source(
            "https://vance.example.test/elon", owner="vance", family="review", title="Elon Musk", author="Ashlee Vance"
        ),
        _source(
            "https://isaacson.example.test/elon",
            owner="isaacson",
            family="publisher",
            title="Elon Musk",
            author="Walter Isaacson",
        ),
    ]
    result = evaluate_grounding_gate(elon, sources)
    assert not result.accepted
    assert result.reason_code == REASON_SOURCE_IDENTITY_DISAGREEMENT

    agree = _source(
        "https://agree.example.test/elon", owner="agree", family="author", title="Elon Musk", author="Ashlee Vance"
    )
    disagree = _source(
        "https://wrong.example.test/elon", owner="wrong", family="wrong", title="Other Book", author="Ashlee Vance"
    )
    result = evaluate_grounding_gate(elon, [agree, disagree])
    # After #525, a different-book title mismatch no longer vetoes an
    # otherwise agreeing subset, so the correct Elon Musk source is accepted
    # as single_source and the different-book source is recorded as
    # title_mismatch. The old expectation was refused with
    # source_identity_disagreement.
    assert result.accepted is True
    assert result.reason_code == ""
    assert tuple(source.canonical_url for source in result.accepted_sources) == ("https://agree.example.test/elon",)
    assert any(
        item.canonical_url == "https://wrong.example.test/elon" and item.reason_code == REASON_TITLE_MISMATCH
        for item in result.rejected_sources
    )

    # Two sources that each name a different co-author of the same multi-author
    # target agree. Issue #505 made the pair rule apply the single-source
    # evidence standard, so a source naming one of the target's own authors is
    # corroboration, not a pairwise disagreement. This assertion pinned the
    # opposite outcome before #505.
    multi_author_target = DurableTarget("multi", "Shared Work", ("Author One", "Author Two"), {})
    pairwise = [
        _source("https://one.example.test/shared", owner="one", family="one", title="Shared Work", author="Author One"),
        _source("https://two.example.test/shared", owner="two", family="two", title="Shared Work", author="Author Two"),
    ]
    multi_author_result = evaluate_grounding_gate(multi_author_target, pairwise)
    assert multi_author_result.accepted
    assert multi_author_result.reason_code == ""


def test_generated_rendering_literal_translation_and_transliteration_cannot_bridge_identity() -> None:
    evidence = {
        "title": "The Work",
        "primary_authors": ["Alex Author"],
        "english_title": "Generated Work",
        "english_title_type": "generated_rendering",
        "english_title_provenance": {
            "kind": "generated_rendering",
            "research_sources": [],
            "mapping": {"original_title": "The Work", "original_authors": ["Alex Author"]},
        },
    }
    source = _source(
        "https://translated.example.test/work",
        owner="translated",
        family="translated",
        title="Generated Work",
    )
    source["title_evidence_route"] = "verified_published_title"
    source["title_evidence"] = evidence
    assert evaluate_grounding_gate(_target(), [source]).reason_code == REASON_TITLE_MISMATCH


def test_verified_published_title_route_requires_title_evidence() -> None:
    mapping = {
        "title": "The Work",
        "primary_authors": ["Alex Author"],
        "english_title": "The Work: A Complete Study",
        "english_title_type": "sourced_published",
        "english_title_provenance": {
            "kind": "sourced_published",
            "research_sources": ["https://mapping.example.test/work"],
            "mapping": {
                "original_title": "The Work",
                "original_authors": ["Alex Author"],
                "published_title": "The Work: A Complete Study",
                "published_authors": ["Alex Author"],
            },
        },
    }
    source = _source(
        "https://published.example.test/work",
        owner="published",
        family="published",
        title="The Work: A Complete Study",
    )
    source.update(title_evidence_route="verified_published_title", title_evidence=mapping)
    assert evaluate_grounding_gate(_target(), [source]).accepted
    assert len(evaluate_grounding_gate(_target(), [source]).accepted_sources) == 1
    second = _source("https://second.example.test/work", owner="second", family="second", title="The Work")
    assert evaluate_grounding_gate(_target(), [source, second]).accepted


def test_verified_published_title_uses_fetched_author_for_identity_gate() -> None:
    evidence = {
        "title": "The Work",
        "primary_authors": ["Alex Author"],
        "english_title": "The Work: A Complete Study",
        "english_title_type": "sourced_published",
        "english_title_provenance": {
            "kind": "sourced_published",
            "research_sources": ["https://mapping.example.test/work"],
            "mapping": {
                "original_title": "The Work",
                "original_authors": ["Alex Author"],
                "published_title": "The Work: A Complete Study",
                "published_authors": ["Alex Author"],
            },
        },
    }
    wrong_author = _source(
        "https://wrong-one.example.test/work",
        owner="wrong-one",
        family="wrong-one",
        title="The Work: A Complete Study",
        author="Wrong Author",
    )
    wrong_author.update(
        title_type="sourced_published",
        title_evidence_route="verified_published_title",
        title_evidence=evidence,
    )
    second_wrong_author = {
        **wrong_author,
        "url": "https://wrong-two.example.test/work",
        "owner_id": "wrong-two",
        "source_family": "wrong-two",
    }
    result = evaluate_grounding_gate(_target(), [wrong_author, second_wrong_author])
    assert not result.accepted
    assert result.reason_code == REASON_AUTHOR_MISMATCH

    missing_author = {**wrong_author, "url": "https://missing-author.example.test/work", "primary_authors": []}
    missing_author_result = evaluate_grounding_gate(_target(), [missing_author, second_wrong_author])
    assert not missing_author_result.accepted
    assert missing_author_result.reason_code == REASON_SOURCE_IDENTITY_DISAGREEMENT


def test_prominence_is_typed_separate_and_identity_gated() -> None:
    valid = validate_prominence_claim(
        _target(),
        {
            "claim_type": "prize",
            "evidence_urls": ["https://award.example.test/work"],
            "source_kind": "award",
            "checked_date": "2026-09-01",
            "display_text": "Shortlisted for the example prize",
            "evidence_span": "The Work is shortlisted for the example prize.",
            "title": "The Work",
            "primary_authors": ["Alex Author"],
        },
    )
    assert valid.accepted
    assert valid.claim is not None
    assert valid.claim.evidence_urls == ("https://award.example.test/work",)
    invalid = validate_prominence_claim(
        _target(),
        {
            "claim_type": "prize",
            "evidence_urls": ["https://award.example.test/work"],
            "source_kind": "award",
            "checked_date": "2026-09-01",
            "display_text": "Wrong book",
            "evidence_span": "The Work is shortlisted for the example prize.",
            "title": "Other Work",
            "primary_authors": ["Alex Author"],
        },
    )
    assert not invalid.accepted
    assert invalid.reason_code == REASON_TITLE_MISMATCH


def test_accepted_evidence_persists_classification_title_and_independence_metadata() -> None:
    published_title_evidence = {
        "title": "The Work",
        "primary_authors": ["Alex Author"],
        "english_title": "The Work: A Complete Study",
        "english_title_type": "sourced_published",
        "english_title_provenance": {
            "kind": "sourced_published",
            "research_sources": ["https://mapping.example.test/work"],
            "mapping": {
                "original_title": "The Work",
                "original_authors": ["Alex Author"],
                "published_title": "The Work: A Complete Study",
                "published_authors": ["Alex Author"],
            },
        },
    }
    first = _source(
        "https://review.example.test/work",
        owner="review-owner",
        family="review-family",
        title="The Work",
    )
    first.update(
        classification_evidence={
            "host": "review.example.test",
            "path_rule": "/books/",
            "policy_metadata_version": "2026-09",
            "classification": "classified",
            "source_metadata": {"source_name": "Review Desk", "source_kind": "review"},
        }
    )
    second = _source(
        "https://publisher.example.test/work",
        owner="publisher-owner",
        family="publisher-family",
        title="The Work: A Complete Study",
    )
    second.update(
        title_evidence_route="verified_published_title",
        title_evidence=published_title_evidence,
        title_type="sourced_published",
        classification_evidence={
            "host": "publisher.example.test",
            "path_rule": "/catalog/",
            "policy_metadata_version": "2026-09",
            "classification": "classified",
            "source_metadata": {"source_name": "Publisher Catalog", "source_kind": "publisher"},
        },
    )
    gate = evaluate_grounding_gate(_target(), [first, second])
    assert gate.accepted
    assert gate.source_independence_evidence is not None
    records = [source.to_dict() for source in gate.accepted_sources]
    published_record = next(record for record in records if record["owner_id"] == "publisher-owner")
    assert published_record["classification_evidence"] == {
        "host": "publisher.example.test",
        "path_rule": "/catalog/",
        "policy_metadata_version": "2026-09",
        "classification": "classified",
        "source_metadata": {
            "owner_id": "publisher-owner",
            "source_family": "publisher-family",
            "source_kind": "publisher",
            "source_language": "de",
            "source_name": "Publisher Catalog",
        },
    }
    assert published_record["title_evidence"] == published_title_evidence
    raw_artifact = {
        "operation_id": "op",
        "operation_date": "2026-09-01",
        "outcomes": [
            {
                "candidate_id": "candidate-work-001",
                "identity_key": "the work|alex author",
                "outcome": "grounded",
                "content_description": "Подтвержденное подробное описание книги. Второй фрагмент раскрывает документальную основу и уточняет редакционный ракурс. Третье предложение закрепляет вывод и показывает, почему книга попала в обзор.",
                "research_sources": [source["url"] for source in records],
                "research_source_records": records,
                "description_provenance": {
                    "provenance": "grounded",
                    "applied_date": "2026-09-01",
                    "evidence_basis": [source["url"] for source in records],
                },
                "source_independence_evidence": gate.source_independence_evidence.to_dict(),
            }
        ],
        "queue": [],
    }
    assert validate_stage_artifact(raw_artifact)
    round_tripped = StageArtifact.from_mapping(raw_artifact).to_dict()
    assert round_tripped["outcomes"][0]["research_source_records"] == records
    assert round_tripped["outcomes"][0]["source_independence_evidence"] == gate.source_independence_evidence.to_dict()


def test_artifact_rejects_tampered_independence_or_classification_evidence() -> None:
    source_records = [
        source.to_dict()
        for source in evaluate_grounding_gate(_target(), _fixture()["agreeing_sources"]).accepted_sources
    ]
    artifact = {
        "operation_id": "op",
        "operation_date": "2026-09-01",
        "outcomes": [
            {
                "candidate_id": "candidate-work-001",
                "identity_key": "the work|alex author",
                "outcome": "grounded",
                "content_description": "Подтвержденное подробное описание книги. Второй фрагмент раскрывает документальную основу и уточняет редакционный ракурс. Третье предложение закрепляет вывод и показывает, почему книга попала в обзор.",
                "research_sources": [source["url"] for source in source_records],
                "research_source_records": source_records,
                "source_independence_evidence": {
                    "canonical_urls": [source["url"] for source in source_records],
                    "owner_ids": [source["owner_id"] for source in source_records],
                    "source_families": [source["source_family"] for source in source_records],
                    "reason": "distinct_canonical_urls_owner_ids",
                },
            }
        ],
        "queue": [],
    }
    assert validate_stage_artifact(artifact)
    tampered_independence = json.loads(json.dumps(artifact))
    tampered_independence["outcomes"][0]["source_independence_evidence"]["owner_ids"][0] = "other-owner"
    assert not validate_stage_artifact(tampered_independence)
    tampered_classification = json.loads(json.dumps(artifact))
    tampered_classification["outcomes"][0]["research_source_records"][0]["classification_evidence"]["host"] = (
        "other.example.test"
    )
    assert not validate_stage_artifact(tampered_classification)


def _candidate(number: int, *, priority: str = "D") -> dict[str, Any]:
    return {
        "candidate_id": f"candidate-{number:03d}",
        "identity_key": f"work {number}|alex author",
        "title": f"Work {number}",
        "primary_authors": ["Alex Author"],
        "update_delta": "new",
        "priority_label": priority,
        "first_seen_date": "2026-09-01",
    }


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class NoResults:
    def discover(self, candidate: Any, **_kwargs: Any) -> list[Any]:
        return []


class AgreeingDiscovery:
    calls = 0

    def discover(self, candidate: Any, **_kwargs: Any) -> list[Any]:
        self.calls += 1
        return [
            {
                "url": "https://review.example.test/work",
                "source_kind": "review",
                "source_family": "review",
                "owner_id": "review",
                "classification": "classified",
            },
            {
                "url": "https://publisher.example.test/work",
                "source_kind": "publisher",
                "source_family": "publisher",
                "owner_id": "publisher",
                "classification": "classified",
            },
        ]


class AgreeingFetch:
    calls = 0

    def fetch(self, result: Any, **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return _source(
            result.url,
            owner=result.owner_id,
            family=result.source_family,
            title="Work 1",
            subject="memory migration",
        )


class RussianSynthesizer:
    def synthesize(self, candidate: Any, sources: Any) -> SynthesisResult:
        return SynthesisResult(
            "Русское описание, подтвержденное независимыми источниками. "
            "Второй фрагмент раскрывает документальную основу, называет проверенные "
            "страницы и уточняет редакционный ракурс книги. "
            "Третье предложение закрепляет вывод и показывает, почему книга попала в обзор."
        )


def test_stage_synthesizes_from_exact_single_source_tuple_and_records_single_outcome() -> None:
    observed: list[tuple[AcceptedSource, ...]] = []

    class SingleDiscovery:
        def discover(self, candidate: Any, **_kwargs: Any) -> list[Any]:
            return [
                {
                    "url": "https://review.example.test/single",
                    "source_kind": "review",
                    "source_family": "review",
                    "owner_id": "review-owner",
                    "classification": "classified",
                }
            ]

    class SingleFetch:
        def fetch(self, result: Any, **_kwargs: Any) -> dict[str, Any]:
            return _source(
                result.url,
                owner=result.owner_id,
                family=result.source_family,
                title="Work 1",
                subject="memory migration",
            )

    class RecordingSynthesizer:
        def synthesize(self, candidate: Any, sources: tuple[AcceptedSource, ...]) -> SynthesisResult:
            observed.append(sources)
            return SynthesisResult(
                "Русское описание по одному источнику. "
                "Второй фрагмент раскрывает документальную основу, называет проверенную "
                "страницу и уточняет редакционный ракурс книги. "
                "Третье предложение закрепляет вывод и показывает, почему книга попала в обзор."
            )

    artifact = GroundedDescriptionStage(
        discovery=SingleDiscovery(),
        fetch=SingleFetch(),
        synthesizer=RecordingSynthesizer(),
        monotonic=FakeClock(),
    ).run([_candidate(1)], operation_id="op", operation_date="2026-09-01")
    outcome = artifact.outcomes[0]
    assert outcome.outcome == OUTCOME_SINGLE_SOURCE
    assert len(outcome.research_sources) == 1
    assert observed == [outcome.research_sources]
    assert artifact.counts == {
        "grounded": 0,
        "single_source": 1,
        "attempted_no_description": 0,
        "not_attempted": 0,
        "evidence_unchanged": 0,
    }
    assert artifact.reason_counts == {}
    serialized = artifact.to_dict()
    assert serialized["counts"] == artifact.counts
    assert serialized["reason_counts"] == {}
    assert serialized["outcomes"][0]["research_sources"] == ["https://review.example.test/single"]
    assert validate_stage_artifact(serialized)


def test_stage_acceptance_is_separate_from_discovery_and_fetch() -> None:
    discovery = AgreeingDiscovery()
    fetch = AgreeingFetch()
    artifact = GroundedDescriptionStage(
        discovery=discovery,
        fetch=fetch,
        synthesizer=RussianSynthesizer(),
        monotonic=FakeClock(),
    ).run([_candidate(1)], operation_id="op", operation_date="2026-09-01")
    assert artifact.outcomes[0].outcome == OUTCOME_GROUNDED
    assert artifact.outcomes[0].content_description == (
        "Русское описание, подтвержденное независимыми источниками. "
        "Второй фрагмент раскрывает документальную основу, называет проверенные "
        "страницы и уточняет редакционный ракурс книги. "
        "Третье предложение закрепляет вывод и показывает, почему книга попала в обзор."
    )
    assert discovery.calls == 1
    assert fetch.calls == 2
    assert artifact.outcomes[0].to_dict()["research_sources"]


def test_scheduler_has_no_fixed_15_or_155_cap_and_preserves_order() -> None:
    for count in (16, 37, 155):
        artifact = GroundedDescriptionStage(discovery=NoResults(), monotonic=FakeClock()).run(
            [_candidate(number) for number in range(count)], operation_id="op", operation_date="2026-09-01"
        )
        assert len(artifact.outcomes) == count
        assert all(item.outcome == OUTCOME_ATTEMPTED_NO_DESCRIPTION for item in artifact.outcomes)


def test_not_attempted_is_scheduling_only_and_is_retried() -> None:
    clock = FakeClock()
    # Issue #633 admits on the measured per-candidate cost, not the fixed
    # envelope: a 2 s budget at three workers holds no 9 s candidate.
    first = GroundedDescriptionStage(discovery=NoResults(), monotonic=clock, stage_budget_seconds=2.0).run(
        [_candidate(1)], operation_id="op-1", operation_date="2026-09-01"
    )
    outcome = first.outcomes[0]
    assert outcome.outcome == OUTCOME_NOT_ATTEMPTED
    assert outcome.reason_code == REASON_NOT_ATTEMPTED_STAGE_BUDGET
    assert "identity_key" not in outcome.to_dict()
    assert "research_sources" not in outcome.to_dict()

    queue = GroundedDescriptionQueue({entry.candidate_id: entry for entry in first.queue})
    second = GroundedDescriptionStage(discovery=NoResults(), monotonic=clock).run(
        [_candidate(1)], operation_id="op-2", operation_date="2026-09-02", queue=queue
    )
    assert second.outcomes[0].outcome == OUTCOME_ATTEMPTED_NO_DESCRIPTION
    assert second.outcomes[0].reason_code == "no_results"


def test_budget_exhaustion_does_not_replace_a_concrete_refusal() -> None:
    clock = FakeClock()
    stage = GroundedDescriptionStage(
        discovery=NoResults(),
        monotonic=clock,
        stage_budget_seconds=DISCOVERY_BUDGET_SECONDS + CANDIDATE_ENVELOPE_SECONDS,
    )
    artifact = stage.run([_candidate(1)], operation_id="op", operation_date="2026-09-01")
    assert artifact.outcomes[0].reason_code == "no_results"
    assert artifact.outcomes[0].reason_code != REASON_NOT_ATTEMPTED_STAGE_BUDGET


class SlowDiscovery:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock

    def discover(self, candidate: Any, **_kwargs: Any) -> list[Any]:
        # Discovery exceeds its separate budget but remains within the stage deadline.
        self.clock.value = DISCOVERY_BUDGET_SECONDS + 1
        return []


class RaisingSlowDiscovery:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock

    def discover(self, candidate: Any, **_kwargs: Any) -> list[Any]:
        # Discovery exceeds its separate budget but remains within the stage deadline.
        self.clock.value = DISCOVERY_BUDGET_SECONDS + 1
        raise RuntimeError("discovery failed after the deadline")


def test_discovery_failure_after_discovery_budget_is_candidate_budget_exhaustion() -> None:
    clock = FakeClock()
    artifact = GroundedDescriptionStage(discovery=RaisingSlowDiscovery(clock), monotonic=clock).run(
        [_candidate(1)], operation_id="op", operation_date="2026-09-01"
    )
    assert artifact.outcomes[0].outcome == OUTCOME_ATTEMPTED_NO_DESCRIPTION
    assert artifact.outcomes[0].reason_code == REASON_CANDIDATE_BUDGET_EXHAUSTED


def test_discovery_return_after_discovery_budget_is_candidate_budget_exhaustion() -> None:
    clock = FakeClock()
    artifact = GroundedDescriptionStage(discovery=SlowDiscovery(clock), monotonic=clock).run(
        [_candidate(1)], operation_id="op", operation_date="2026-09-01"
    )
    assert artifact.outcomes[0].outcome == OUTCOME_ATTEMPTED_NO_DESCRIPTION
    assert artifact.outcomes[0].reason_code == REASON_CANDIDATE_BUDGET_EXHAUSTED


def test_discovery_failure_within_discovery_budget_is_search_failure() -> None:
    class FastFailure:
        def discover(self, candidate: Any, **_kwargs: Any) -> list[Any]:
            del candidate
            raise RuntimeError("bounded discovery failure")

    artifact = GroundedDescriptionStage(discovery=FastFailure(), monotonic=FakeClock()).run(
        [_candidate(1)], operation_id="op", operation_date="2026-09-01"
    )
    assert artifact.outcomes[0].reason_code == REASON_SEARCH_OR_FETCH_FAILURE


def test_queue_order_is_age_then_priority_then_first_seen_then_durable_id() -> None:
    queue = GroundedDescriptionQueue()
    candidates = [
        {**_candidate(1, priority="D"), "first_seen_date": "2026-09-02"},
        {**_candidate(2, priority="A"), "first_seen_date": "2026-09-02"},
        {**_candidate(3, priority="A"), "first_seen_date": "2026-09-01"},
    ]
    ordered = queue.worklist(candidates, operation_id="op", operation_date="2026-09-01")
    assert [candidate["candidate_id"] for candidate, _entry in ordered] == [
        "candidate-003",
        "candidate-002",
        "candidate-001",
    ]
    assert [candidate["priority_label"] for candidate, _entry in ordered] == ["A", "A", "D"]


def test_accepted_source_urls_are_deterministic_and_bounded() -> None:
    source = AcceptedSource(
        "https://example.test/work",
        "review",
        "review",
        "owner",
        "en",
        "direct_original",
        "2026-09-01",
        "Raw title",
        ("Raw author",),
        "subject",
    )
    assert source.to_dict()["url"] == "https://example.test/work"
    assert "rejected prose" not in json.dumps(source.to_dict())


class FakeTransport:
    def __init__(self, responses: list[HTTPResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, url: str, **kwargs: Any) -> HTTPResponse:
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


def test_review_renders_three_confidence_levels_and_stage_outcomes_without_state_mutation(tmp_path: Path) -> None:
    candidates = [
        {**_candidate(1), "update_delta": "new"},
        {**_candidate(2), "update_delta": "new"},
        {**_candidate(3), "update_delta": "new"},
        {**_candidate(4), "update_delta": "new"},
    ]
    stage = {
        "outcomes": [
            {
                "candidate_id": "candidate-001",
                "identity_key": candidates[0]["identity_key"],
                "outcome": "grounded",
                "content_description": "Подтвержденное описание книги.",
                "research_sources": [
                    {
                        "url": "https://review.example.test/work",
                        "source_kind": "review",
                        "source_family": "review",
                        "owner_id": "review",
                        "checked_date": "2026-09-01",
                    },
                    {
                        "url": "https://publisher.example.test/work",
                        "source_kind": "publisher",
                        "source_family": "publisher",
                        "owner_id": "publisher",
                        "checked_date": "2026-09-01",
                    },
                ],
                "prominence_claims": [
                    {
                        "claim_type": "prize",
                        "evidence_urls": ["https://award.example.test/work"],
                        "source_kind": "award",
                        "checked_date": "2026-09-01",
                        "display_text": "Shortlisted",
                        "evidence_span": "The Work is shortlisted for the example prize.",
                    }
                ],
            },
            {
                "candidate_id": "candidate-002",
                "identity_key": candidates[1]["identity_key"],
                "outcome": "single_source",
                "content_description": "Описание по одному проверенному источнику.",
                "research_sources": [
                    {
                        "url": "https://review.example.test/single",
                        "source_kind": "review",
                        "source_family": "review",
                        "owner_id": "review-single",
                        "checked_date": "2026-09-01",
                    }
                ],
            },
            {
                "candidate_id": "candidate-003",
                "identity_key": candidates[2]["identity_key"],
                "outcome": "attempted_no_description",
                "reason_code": "title_mismatch",
            },
            {
                "candidate_id": "candidate-004",
                "outcome": "not_attempted",
                "reason_code": "not_attempted_stage_budget",
                "scheduling": {
                    "candidate_id": "candidate-004",
                    "queue_entry_operation": "op",
                    "queue_entry_date": "2026-09-01",
                    "first_seen_date": "2026-09-01",
                    "priority_label": "D",
                    "attempt_status": "waiting",
                    "reason_code": "not_attempted_stage_budget",
                },
            },
        ]
    }
    rows, summary = assemble_cumulative_review(candidates, [], [], grounded_description_stage=stage)
    by_id = {row["identity_key"]: row for row in rows}
    assert by_id[candidates[0]["identity_key"]]["description"] == "Подтвержденное описание книги."
    assert by_id[candidates[0]["identity_key"]]["prominence"] == "prize: Shortlisted (https://award.example.test/work)"
    assert by_id[candidates[0]["identity_key"]]["prominence_claim_type"] == "prize"
    assert by_id[candidates[0]["identity_key"]]["prominence_evidence_url"] == "https://award.example.test/work"
    assert by_id[candidates[1]["identity_key"]]["description_status"] == "⚠ Описание по одному источнику"
    assert (
        by_id[candidates[2]["identity_key"]]["description_status"] == "Источники проверены - описание не подтверждено"
    )
    assert by_id[candidates[2]["identity_key"]]["description_stage_reason"] == "Название источника не совпадает"
    assert by_id[candidates[3]["identity_key"]]["description_status"] == "Поиск источников еще не запускался"
    assert summary["description_stage_counts"] == {
        "grounded": 1,
        "single_source": 1,
        "attempted_no_description": 1,
        "not_attempted": 1,
    }

    csv_path = tmp_path / "review.csv"
    write_editor_csv(csv_path, [row["editor_row"] for row in rows])
    with csv_path.open(encoding="utf-8", newline="") as handle:
        csv_rows = list(csv.DictReader(handle))
    assert csv_rows[0]["Prominence"] == "prize: Shortlisted (https://award.example.test/work)"
    assert csv_rows[0]["Prominence claim type"] == "prize"
    assert csv_rows[0]["Prominence evidence URL"] == "https://award.example.test/work"
    assert csv_rows[2]["Description reason"] == "Название источника не совпадает"
    assert {row["Description status"] for row in csv_rows} == {
        "Описание подтверждено двумя источниками",
        "⚠ Описание по одному источнику",
        "Источники проверены - описание не подтверждено",
        "Поиск источников еще не запускался",
    }
    html = build_html_review(
        rows,
        {
            "total_count": 4,
            "cumulative_counts": summary,
            "description_stage_counts": summary["description_stage_counts"],
        },
    )
    assert "Описание подтверждено двумя источниками" in html
    assert html.count("⚠ Описание по одному источнику") == 1
    assert "<dt>Статус описания</dt><dd>⚠ Описание по одному источнику</dd>" in html
    assert "description-warning-badge" not in html
    assert "Источники проверены - описание не подтверждено" in html
    confirmed_html = build_html_review([by_id[candidates[0]["identity_key"]]], {"total_count": 1})
    assert "⚠ Описание не подтверждено источниками" not in confirmed_html
    assert "⚠ Описание по одному источнику" not in confirmed_html
    assert "Название источника не совпадает" in html
    assert "Поиск источников еще не запускался" in html
    assert "Проминентность" in html
    assert "prize: Shortlisted" in html
    assert "Тип проминентности" in html
    assert "URL доказательства проминентности" in html
    assert 'href="https://award.example.test/work"' in html
    assert "Проверка источников: подтверждено несколькими источниками - 1" in html


def test_verified_title_evidence_flows_from_discovery_to_fetch() -> None:
    evidence = {
        "title": "The Work",
        "primary_authors": ["Alex Author"],
        "english_title": "The Work: A Complete Study",
        "english_title_type": "sourced_published",
        "english_title_provenance": {
            "kind": "sourced_published",
            "research_sources": ["https://mapping.example.test/work"],
            "mapping": {
                "original_title": "The Work",
                "original_authors": ["Alex Author"],
                "published_title": "The Work: A Complete Study",
                "published_authors": ["Alex Author"],
            },
        },
    }
    discovery_body = json.dumps(
        {
            "results": [
                {
                    "url": "https://review.example.test/work",
                    "title_evidence_route": "verified_published_title",
                    "title_evidence": evidence,
                }
            ]
        }
    ).encode()
    # HTTPDiscoveryProvider issues one deterministic discovery request per
    # candidate, and the same transport then serves the fetched page.
    transport = FakeTransport(
        [
            HTTPResponse(
                "https://search.example.test/api",
                200,
                {"content-type": "application/json"},
                discovery_body,
            ),
            HTTPResponse(
                "https://review.example.test/work",
                200,
                {"content-type": "text/html"},
                b"<html><title>The Work: A Complete Study</title><meta name='author' content='Wrong Author'><meta name='description' content='Memory migration.'></html>",
            ),
        ]
    )
    provider = HTTPDiscoveryProvider(
        "https://search.example.test/api?q={query}",
        transport=transport,
        classifier=committed_source_classifier(),
    )
    discovered = provider.discover(_target().record)
    assert discovered[0].title_evidence == evidence
    fetched = BoundedHTTPFetcher(transport=transport).fetch(discovered[0], checked_date="2026-09-01")
    assert fetched.title_evidence == evidence


def test_phase_b_discovery_is_classification_only_and_fetch_is_bounded() -> None:
    # HTTPDiscoveryProvider issues one deterministic request per candidate.
    # Its single response still carries the snippet, classified, and unknown
    # result shapes needed by the classification and bounded-fetch contract.
    discovery_body = json.dumps(
        {
            "results": [
                {
                    "url": "https://review.example.test/snippet",
                    "result_kind": "search_snippet",
                    "snippet": "do not use",
                },
                {"url": "https://review.example.test/work"},
                # A denylisted host stays unknown.
                {"url": "https://www.reddit.com/r/books/work"},
            ]
        }
    ).encode()
    discovery_transport = FakeTransport(
        [
            HTTPResponse(
                "https://search.example.test/api",
                200,
                {"content-type": "application/json"},
                discovery_body,
            ),
        ]
    )
    classifier = committed_source_classifier()
    provider = HTTPDiscoveryProvider(
        "https://search.example.test/api?q={query}", transport=discovery_transport, classifier=classifier
    )
    results = provider.discover(_target().record)
    assert len(results) == 3
    assert results[0].is_search_snippet
    assert results[1].classification == "classified"
    assert results[1].classification_evidence == {
        "host": "review.example.test",
        "path_rule": "",
        "policy_metadata_version": "owner-derived-1",
        "classification": "classified",
        "source_metadata": {
            "source_kind": "independent",
            "source_family": "independent",
            "owner_id": "example.test",
            "tier": "derived",
        },
    }
    assert results[2].classification == "unknown"
    assert all("snippet" not in result.metadata for result in results)
    assert discovery_transport.calls[0]["max_response_bytes"] == 1_000_000

    fetch_transport = FakeTransport(
        [
            HTTPResponse(
                "https://review.example.test/work",
                200,
                {"content-type": "text/html; charset=utf-8"},
                b"<html><head><title>The Work</title><meta name='author' content='Alex Author'><meta name='description' content='Memory and migration.'></head></html>",
            )
        ]
    )
    fetched = BoundedHTTPFetcher(transport=fetch_transport, max_response_bytes=1234, max_redirects=2).fetch(results[1])
    assert fetched.title == "The Work"
    assert fetched.primary_authors == ("Alex Author",)
    assert fetched.subject_text == "Memory and migration."
    assert fetched.classification_evidence["host"] == "review.example.test"
    assert fetched.classification_evidence["path_rule"] == ""
    assert fetched.classification_evidence["policy_metadata_version"] == "owner-derived-1"
    assert fetch_transport.calls[0]["max_response_bytes"] == 1234
    assert fetch_transport.calls[0]["max_redirects"] == 2
    assert fetch_transport.calls[0]["allowed_hosts"] == {"review.example.test"}


def test_production_enrichment_uses_safe_grounded_stage_and_explicit_http_config(
    monkeypatch: Any, tmp_path: Path
) -> None:
    from autoscout.grounded_description import NoNetworkDiscoveryProvider
    from autoscout.weekly_workflow import GROUNDED_DISCOVERY_ENDPOINT_ENV
    from autoscout.weekly_workflow import _make_production_enrichment

    monkeypatch.delenv(GROUNDED_DISCOVERY_ENDPOINT_ENV, raising=False)
    offline = _make_production_enrichment()
    assert isinstance(offline, GroundedDescriptionStageService)
    assert isinstance(offline.stage, GroundedDescriptionStage)
    assert isinstance(offline.stage.discovery, NoNetworkDiscoveryProvider)
    assert offline.stage.fetch is None
    assert offline.enabled is False
    output_dir = tmp_path / "output"
    state_dir = tmp_path / "state"
    output_dir.mkdir()
    state_dir.mkdir()
    (output_dir / "scouting-candidates.json").write_text(json.dumps([_candidate(1)]), encoding="utf-8")
    seed_state(state_dir, candidate_history=[])
    offline_result = offline.enrich(output_dir, state_dir, effective_date="2026-09-01")
    assert offline_result["status"] == "skipped"
    assert not (output_dir / "grounded-description-stage.json").exists()

    monkeypatch.setenv(GROUNDED_DISCOVERY_ENDPOINT_ENV, "https://search.example.test/api?q={query}")
    configured = _make_production_enrichment()
    assert isinstance(configured, GroundedDescriptionStageService)
    assert isinstance(configured.stage.discovery, HTTPDiscoveryProvider)
    assert isinstance(configured.stage.fetch, BoundedHTTPFetcher)
    assert isinstance(configured.stage.synthesizer, CodexGroundedDescriptionSynthesizer)


def test_stage_service_and_artifact_record_full_outcome_and_reason_counts(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "scouting-candidates.json").write_text(json.dumps([_candidate(1)]), encoding="utf-8")
    service = GroundedDescriptionStageService(stage=GroundedDescriptionStage(discovery=NoResults()))

    result = service.enrich(output_dir, effective_date="2026-09-01")
    expected_counts = {
        "grounded": 0,
        "single_source": 0,
        "attempted_no_description": 1,
        "not_attempted": 0,
        "evidence_unchanged": 0,
    }
    assert result["grounded_description_counts"] == expected_counts
    assert result["grounded_description_reason_counts"] == {"no_results": 1}
    assert result["attempted_no_description"] == 1
    artifact = json.loads((output_dir / "grounded-description-stage.json").read_text(encoding="utf-8"))
    assert artifact["counts"] == expected_counts
    assert artifact["reason_counts"] == {"no_results": 1}
    assert validate_stage_artifact(artifact)


@pytest.mark.json_backend_only("the state directory's file listing and bytes")
def test_grounded_stage_service_persists_bounded_fallback_attempts(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    state_dir = tmp_path / "state"
    output_dir.mkdir()
    state_dir.mkdir()
    (output_dir / "scouting-candidates.json").write_text(json.dumps([_candidate(1)]), encoding="utf-8")
    history_path = state_dir / "candidate-history.json"
    history_path.write_text(json.dumps([_candidate(1)], indent=2) + "\n", encoding="utf-8")
    alias_path = state_dir / "candidate-identity-aliases.json"
    alias_path.write_text(
        json.dumps({"version": 1, "aliases": {}, "candidates": {}}, indent=2) + "\n", encoding="utf-8"
    )
    aliases_before = alias_path.read_bytes()

    service = GroundedDescriptionStageService(stage=GroundedDescriptionStage(discovery=NoResults()))
    result = service.enrich(output_dir, state_dir, effective_date="2026-09-01")

    assert result["status"] == "ok"
    assert result["attempted_no_description"] == 1
    persisted_history = json.loads(history_path.read_text(encoding="utf-8"))
    assert persisted_history[0]["content_description"] == "Описание требует дополнительной проверки"
    assert persisted_history[0]["grounded_description_attempts"][0]["reason_code"] == "no_results"
    assert alias_path.read_bytes() == aliases_before
    assert (output_dir / "grounded-description-stage.json").exists()
    assert (state_dir / "grounded-description-queue.json").exists()
    assert sorted(path.name for path in output_dir.iterdir()) == [
        "candidate-history.json",
        "grounded-description-stage.json",
        "scouting-candidates.json",
    ]
    assert sorted(path.name for path in state_dir.iterdir()) == [
        "candidate-history.json",
        "candidate-identity-aliases.json",
        "grounded-description-queue.json",
    ]


def test_model_encoding_bounds_nested_accepted_evidence() -> None:
    oversized = {
        "url": "https://example.test/" + "u" * 1000,
        "source_kind": "review",
        "source_family": "family",
        "owner_id": "owner",
        "source_language": "en",
        "title_evidence": {
            "published_title": "t" * 1000,
            "mapping": {
                "original_title": "o" * 1000,
                "deep": {"deeper": {"value": "d" * 1000}},
            },
            "many": ["x" * 1000 for _ in range(30)],
        },
        "classification_evidence": {
            "classification": "classified",
            "source_metadata": {
                "source_name": "s" * 1000,
                "nested": {"value": "n" * 1000},
            },
        },
        "raw_authors": ["a" * 1000 for _ in range(30)],
    }
    encoded = _encode_work_items(
        [{"identity_key": "work|author", "title": "Work", "accepted_bounded_evidence": [oversized]}]
    )[0]["accepted_bounded_evidence"][0]

    assert len(encoded["url"]) == 500
    assert len(encoded["raw_authors"]) == 10
    assert all(len(author) == 200 for author in encoded["raw_authors"])
    assert len(encoded["title_evidence"]["published_title"]) == 300
    assert len(encoded["classification_evidence"]["source_metadata"]["source_name"]) == 300
    assert len(encoded["title_evidence"]["many"]) == 10
    assert len(encoded["title_evidence"]["mapping"]["deep"]["deeper"]["value"]) == 300


def test_production_synthesizer_receives_only_accepted_bounded_evidence() -> None:
    class RecordingAgent:
        def __init__(self) -> None:
            self.work_items: list[dict[str, Any]] = []

        def generate(self, work_items: list[dict[str, Any]]) -> dict[str, Any]:
            self.work_items = work_items
            return {work_items[0]["identity_key"]: {"content_description": "Русское описание книги."}}

    agent = RecordingAgent()
    synthesizer = CodexGroundedDescriptionSynthesizer(agent=agent)
    candidate = _candidate(1)
    sources = (
        AcceptedSource(
            "https://review.example.test/work",
            "review",
            "review-family",
            "review-owner",
            "en",
            "direct_original",
            "2026-09-01",
            "Work 1",
            ("Alex Author",),
            "memory migration",
            display_text="A bounded subject note.",
        ),
    )

    description = synthesizer.synthesize(candidate, sources)

    assert description == SynthesisResult("Русское описание книги.")
    item = agent.work_items[0]
    assert item["identity_key"] == candidate["identity_key"]
    assert item["metadata"]["signal_summary"]
    summary = item["metadata"]["signal_summary"][0]
    assert "https://review.example.test/work" in summary
    assert "A bounded subject note." in summary
    assert "source_evidence" not in item["metadata"]


def test_grounded_synthesis_uses_dedicated_prompt_and_only_accepted_evidence() -> None:
    class PromptRecordingAgent:
        def __init__(self) -> None:
            self.work_items: list[dict[str, Any]] = []
            self.prompt = ""

        def generate(self, work_items: list[dict[str, Any]], *, _prompt_override: str | None = None) -> dict[str, Any]:
            self.work_items = work_items
            self.prompt = _prompt_override or ""
            return {work_items[0]["identity_key"]: {"content_description": "Русское описание книги."}}

    agent = PromptRecordingAgent()
    synthesizer = CodexGroundedDescriptionSynthesizer(agent=agent)
    source = AcceptedSource(
        "https://review.example.test/work",
        "review",
        "review-family",
        "review-owner",
        "en",
        "direct_original",
        "2026-09-01",
        "The Work",
        ("Alex Author",),
        "memory migration",
        display_text="Accepted subject evidence.",
    )
    candidate = {**_candidate(1), "source_evidence": [{"evidence_url": "https://outside.example.test"}]}

    assert synthesizer.synthesize(candidate, (source,)) == SynthesisResult("Русское описание книги.")
    assert agent.prompt == GROUNDED_SYNTHESIS_PROMPT
    assert "must not search" in agent.prompt
    assert "browse" in agent.prompt
    assert "follow links" in agent.prompt
    assert "use outside knowledge" in agent.prompt
    assert "invent URLs/evidence" in agent.prompt
    assert agent.work_items[0]["accepted_bounded_evidence"] == [source.to_dict()]
    assert "outside.example.test" not in json.dumps(agent.work_items[0], ensure_ascii=False)


def test_existing_codex_prompt_output_remains_unchanged() -> None:
    import hashlib

    from autoscout.description_enrichment import CodexDescriptionEnrichmentAgent

    prompt = CodexDescriptionEnrichmentAgent._build_prompt()
    # Deliberate tripwire. Last changed by issue #727, which asks this still
    # active legacy path for the machine-facing English Description.
    assert (
        hashlib.sha256(prompt.encode()).hexdigest()
        == "96ffcd3ff775f6ab18cc74c28901736a2629fdf86d19a5501947fb62d3648208"
    )


def test_stage_uses_history_and_alias_seams_for_ambiguous_resolution() -> None:
    candidate = _candidate(1)
    history = [
        {**candidate, "title": "Work 1"},
        {**candidate, "title": "Work 1"},
    ]
    artifact = GroundedDescriptionStage(
        discovery=AgreeingDiscovery(),
        fetch=AgreeingFetch(),
        monotonic=FakeClock(),
    ).run(
        [candidate],
        operation_id="op",
        operation_date="2026-09-01",
        history=history,
        aliases={"version": 1, "aliases": {}, "candidates": {}},
    )

    assert artifact.outcomes[0].outcome == OUTCOME_ATTEMPTED_NO_DESCRIPTION
    assert artifact.outcomes[0].reason_code == "ambiguity"


def test_grounded_provenance_is_accepted_by_shared_persistence_helper() -> None:
    record = description_provenance_record(
        "work 1|alex author",
        "Русское описание книги.",
        "grounded",
        evidence_basis=["https://review.example.test/work"],
        applied_date="2026-09-01",
    )
    assert record["provenance"] == "grounded"

    updated, result = apply_description_artifacts(
        [
            {
                "identity_key": "work 1|alex author",
                "content_description": "Описание требует дополнительной проверки",
            }
        ],
        [
            {
                "identity_key": "work 1|alex author",
                "content_description": "Русское описание книги.",
                "research_sources": ["https://review.example.test/work"],
            }
        ],
        provenance="grounded",
        effective_date="2026-09-01",
    )
    assert result["applied"] == 1
    assert updated[0]["description_provenance"]["provenance"] == "grounded"
