"""Regression coverage for issue #493 evidence budget separation."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import ClassVar

import pytest

import autoscout.grounded_description as grounded_description
from autoscout.description_enrichment import _encode_work_items
from autoscout.grounded_description import REASON_SEARCH_OR_FETCH_FAILURE
from autoscout.grounded_description import REASON_SUBJECT_DISAGREEMENT
from autoscout.grounded_description import AcceptedSource
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import CodexGroundedDescriptionSynthesizer
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import DurableTarget
from autoscout.grounded_description import FetchedSource
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import evaluate_grounding_gate
from autoscout.grounded_description import validate_prominence_claim

FIXTURES = Path(__file__).parent / "fixtures" / "issue-493-evidence-budget"
LONG_SUBJECT_FIXTURE = FIXTURES / "long-subject.html"
LONG_SUBJECT_URL = "https://example.test/long-subject"
DEVIL_URL = "https://en.wikipedia.org/wiki/The_Devil_in_the_White_City"
DEVIL_FIXTURE = FIXTURES / "devil-in-white-city-wikipedia.html"
CHECKED_DATE = "2026-08-17"
EXPECTED_METADATA_TEXT = 500
EXPECTED_SUBJECT_TEXT = 1_500
EXPECTED_PROMINENCE_TEXT = 300
MAX_METADATA_TEXT = int(
    getattr(grounded_description, "MAX_EVIDENCE_METADATA_TEXT", getattr(grounded_description, "MAX_EVIDENCE_TEXT", 500))
)
MAX_SUBJECT_TEXT = int(getattr(grounded_description, "MAX_SOURCE_SUBJECT_TEXT", EXPECTED_SUBJECT_TEXT))
MAX_DISPLAY_TEXT = int(getattr(grounded_description, "MAX_DISPLAY_TEXT", EXPECTED_PROMINENCE_TEXT))
SUBJECT_SEPARATOR = str(getattr(grounded_description, "SOURCE_SUBJECT_SEPARATOR", "; subject="))
MAX_SYNTHESIS_TEXT = int(
    getattr(
        grounded_description,
        "MAX_SYNTHESIS_EVIDENCE_TEXT",
        EXPECTED_METADATA_TEXT + EXPECTED_SUBJECT_TEXT + len(SUBJECT_SEPARATOR),
    )
)


class RecordingAgent:
    def __init__(self) -> None:
        self.work_items: list[Mapping[str, Any]] = []

    def generate(self, work_items: list[Mapping[str, Any]], **_kwargs: object) -> Mapping[str, Any]:
        self.work_items = work_items
        identity_key = str(work_items[0]["identity_key"])
        return {identity_key: {"content_description": "Описание на основе источника."}}


class FixtureTransport:
    bodies: ClassVar[dict[str, Path]] = {
        LONG_SUBJECT_URL: LONG_SUBJECT_FIXTURE,
        DEVIL_URL: DEVIL_FIXTURE,
    }

    def request(self, url: str, **_kwargs: object) -> HTTPResponse:
        return HTTPResponse(
            url=url,
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=self.bodies[url].read_bytes(),
        )


class AdversarialTransport:
    bodies: ClassVar[dict[str, bytes]]

    def __init__(self, bodies: dict[str, str]) -> None:
        self.bodies = {url: html.encode("utf-8") for url, html in bodies.items()}

    def request(self, url: str, **_kwargs: object) -> HTTPResponse:
        return HTTPResponse(
            url=url,
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=self.bodies[url],
        )


def _candidate() -> dict[str, Any]:
    return {
        "identity_key": "long-subject-work|alex-author",
        "title": "Long Subject Work",
        "primary_authors": ["Alex Author"],
    }


def _target() -> DurableTarget:
    return DurableTarget("work", "The Work", ("Alex Author",), {})


def _accepted_source(
    *, subject: str, url: str = "https://source.example.test/book", owner_id: str = "source-owner"
) -> AcceptedSource:
    return AcceptedSource(
        canonical_url=url,
        source_kind="review",
        source_family="review",
        owner_id=owner_id,
        source_language="en",
        checked_date=CHECKED_DATE,
        raw_title="A title that consumes metadata space. " * 30,
        raw_authors=("Alex Author",),
        display_text=subject,
    )


def _synthesis_evidence(subject: str) -> tuple[str, RecordingAgent]:
    agent = RecordingAgent()
    synthesizer = CodexGroundedDescriptionSynthesizer(agent=agent)
    synthesizer.synthesize(_candidate(), [_accepted_source(subject=subject)])
    return str(agent.work_items[0]["metadata"]["signal_summary"][0]), agent


def test_fetch_preserves_old_gate_subject_and_separate_long_display_text() -> None:
    discovery = DiscoveryResult(
        url=LONG_SUBJECT_URL,
        source_kind="review",
        source_family="review",
        owner_id="example",
        source_language="en",
    )

    fetched = BoundedHTTPFetcher(transport=FixtureTransport()).fetch(discovery, checked_date=CHECKED_DATE)

    assert len(fetched.subject_text) == MAX_METADATA_TEXT == 500
    assert fetched.subject_key == fetched.subject_text.replace(":", " ")
    assert len(fetched.display_text) > EXPECTED_PROMINENCE_TEXT
    assert len(fetched.display_text) <= EXPECTED_SUBJECT_TEXT
    assert fetched.display_text.endswith(".")
    assert fetched.display_text != fetched.subject_text


def test_two_fetched_sources_disagree_before_late_shared_subject_term() -> None:
    first_url = "https://one.example.test/work"
    second_url = "https://two.example.test/work"
    first_subject = ("archival memory " * 40) + " shared late evidence."
    second_subject = ("quantum telescopes " * 40) + " shared late evidence."
    transport = AdversarialTransport(
        {
            first_url: (
                '<html><title>The Work</title><meta name="author" content="Alex Author">'
                f"<article><p>{first_subject}</p></article></html>"
            ),
            second_url: (
                '<html><title>The Work</title><meta name="author" content="Alex Author">'
                f"<article><p>{second_subject}</p></article></html>"
            ),
        }
    )
    discoveries = (
        DiscoveryResult(first_url, "review", "review", "owner-one", "en"),
        DiscoveryResult(second_url, "review", "review", "owner-two", "en"),
    )
    fetched = [BoundedHTTPFetcher(transport=transport).fetch(item, checked_date=CHECKED_DATE) for item in discoveries]
    result = evaluate_grounding_gate(_target(), fetched)

    assert result.accepted is False
    assert result.reason_code == REASON_SUBJECT_DISAGREEMENT
    assert tuple(source.canonical_url for source in result.rejected_sources) == (first_url, second_url)
    assert all(source.subject_text.find("shared") == -1 for source in fetched)
    assert all(source.display_text.find("shared") > EXPECTED_METADATA_TEXT for source in fetched)


def test_late_editorial_hook_reaches_model_boundary_on_base_compatible_path() -> None:
    source = AcceptedSource(
        canonical_url="https://source.example.test/late-hook",
        source_kind="review",
        source_family="review",
        owner_id="source-owner",
        source_language="en",
        checked_date=CHECKED_DATE,
        raw_title="Long metadata that must not consume the subject budget. " * 30,
        raw_authors=("Alex Author",),
        display_text=("A source overview establishes the historical context. " * 20)
        + "Holmes is the late editorial hook.",
    )
    agent = RecordingAgent()
    synthesizer = CodexGroundedDescriptionSynthesizer(agent=agent)

    synthesizer.synthesize(_candidate(), [source])
    evidence = str(agent.work_items[0]["metadata"]["signal_summary"][0])

    assert "Holmes" in evidence


def test_encoder_keeps_late_holmes_hook_in_model_payload() -> None:
    late_hook = ("context " * 100) + "Holmes is the late editorial hook."
    item = {
        "identity_key": "work|author",
        "title": "The Work",
        "accepted_bounded_evidence": [{"display_text": late_hook}],
    }

    encoded = _encode_work_items([item])[0]
    encoded_display_text = encoded["accepted_bounded_evidence"][0]["display_text"]

    assert encoded_display_text.index("Holmes") > EXPECTED_PROMINENCE_TEXT
    assert "Holmes" in encoded_display_text
    assert len(encoded_display_text) == len(late_hook)


def test_fetch_gate_synthesize_and_encode_keep_late_holmes_hook() -> None:
    discovery = DiscoveryResult(
        url=DEVIL_URL,
        source_kind="encyclopedia",
        source_family="encyclopedia",
        owner_id="wikipedia",
        source_language="en",
    )
    fetched = BoundedHTTPFetcher(transport=FixtureTransport()).fetch(discovery, checked_date=CHECKED_DATE)
    target = DurableTarget(
        candidate_id="devil",
        title="The Devil in the White City",
        primary_authors=("Erik Larson",),
        record={},
    )
    gate = evaluate_grounding_gate(target, [fetched])

    assert gate.accepted
    accepted = gate.accepted_sources[0]
    assert accepted.display_text.index("Holmes") > EXPECTED_PROMINENCE_TEXT

    agent = RecordingAgent()
    CodexGroundedDescriptionSynthesizer(agent=agent).synthesize(
        {
            "identity_key": target.candidate_id,
            "title": target.title,
            "primary_authors": list(target.primary_authors),
        },
        gate.accepted_sources,
    )
    encoded = _encode_work_items([dict(agent.work_items[0])])[0]
    encoded_display_text = encoded["accepted_bounded_evidence"][0]["display_text"]

    assert "Holmes" in encoded_display_text
    assert len(encoded_display_text) > MAX_DISPLAY_TEXT


def test_long_metadata_does_not_shorten_full_subject_budget() -> None:
    sentence = "Holmes investigates the Chicago World's Fair and its history."
    subject = " ".join([sentence] * 20)
    evidence, agent = _synthesis_evidence(subject)

    metadata, handed_subject = evidence.split(SUBJECT_SEPARATOR, maxsplit=1)
    assert len(metadata) <= MAX_METADATA_TEXT
    assert handed_subject == subject
    assert len(handed_subject) <= EXPECTED_SUBJECT_TEXT
    assert len(evidence) <= MAX_SYNTHESIS_TEXT
    assert agent.work_items[0]["accepted_bounded_evidence"][0]["display_text"] == subject


def test_long_subject_ends_at_a_complete_sentence() -> None:
    first = "The first sentence establishes the subject."
    second = " ".join(["Holmes"] * 180) + "."
    third = " ".join(["This trailing sentence must not be included"] * 100) + "."

    evidence, _agent = _synthesis_evidence(f"{first} {second} {third}")

    handed_subject = evidence.split(SUBJECT_SEPARATOR, maxsplit=1)[1]
    assert handed_subject == f"{first} {second}"
    assert handed_subject.endswith(".")
    assert not handed_subject.endswith("Holmes")


def test_short_subject_is_unchanged() -> None:
    subject = "A short subject without terminal punctuation"

    evidence, _agent = _synthesis_evidence(subject)

    assert evidence.split(SUBJECT_SEPARATOR, maxsplit=1)[1] == subject


def test_subject_without_a_complete_sentence_fails_safe() -> None:
    subject = "word " * 400

    evidence, _agent = _synthesis_evidence(subject)

    assert evidence.endswith(SUBJECT_SEPARATOR)


def test_accepted_source_storage_uses_subject_budget() -> None:
    subject = ("A source subject. " * 100).strip()
    stored = _accepted_source(subject=subject).to_dict()

    assert len(subject) > EXPECTED_SUBJECT_TEXT
    assert stored["display_text"].endswith(".")
    assert len(stored["display_text"]) <= EXPECTED_SUBJECT_TEXT
    assert len(stored["display_text"]) > EXPECTED_PROMINENCE_TEXT


@pytest.mark.parametrize(
    ("display_text", "accepted"),
    [("x" * EXPECTED_PROMINENCE_TEXT, True), ("x" * (EXPECTED_PROMINENCE_TEXT + 1), False)],
)
def test_prominence_display_bound_is_unchanged(display_text: str, accepted: bool) -> None:
    claim = {
        "claim_type": "prize",
        "evidence_urls": ["https://award.example.test/work"],
        "source_kind": "award",
        "checked_date": CHECKED_DATE,
        "display_text": display_text,
        "evidence_span": "The Work is shortlisted for the example prize.",
        "title": "The Work",
        "primary_authors": ["Alex Author"],
    }

    result = validate_prominence_claim(_target(), claim)

    assert result.accepted is accepted
    if not accepted:
        assert result.reason_code == REASON_SEARCH_OR_FETCH_FAILURE


def test_representative_gate_decision_and_urls_are_unchanged() -> None:
    first_url = "https://matrix.example/one"
    second_url = "https://matrix.example/two"
    sources = [
        FetchedSource(
            url=first_url,
            source_kind="review",
            source_family="review",
            owner_id="owner-one",
            source_language="en",
            title="The Work",
            primary_authors=("Alex Author",),
            subject_text="memory migration",
            checked_date=CHECKED_DATE,
        ),
        FetchedSource(
            url=second_url,
            source_kind="review",
            source_family="review",
            owner_id="owner-two",
            source_language="en",
            title="The Work",
            primary_authors=("Alex Author",),
            subject_text="memory migration",
            checked_date=CHECKED_DATE,
        ),
    ]

    result = evaluate_grounding_gate(_target(), sources)

    assert result.accepted
    assert result.research_sources == (first_url, second_url)
