"""Offline regression coverage for issue #499 prominence claims."""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from autoscout.grounded_description import GROUNDED_SYNTHESIS_PROMPT
from autoscout.grounded_description import OUTCOME_SINGLE_SOURCE
from autoscout.grounded_description import REASON_SYNTHESIS_FAILURE
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import NoOpDescriptionSynthesizer
from autoscout.grounded_description import SynthesisResult
from autoscout.grounded_description import committed_source_classifier
from autoscout.review_rendering import assemble_cumulative_review

FIXTURES = Path(__file__).parent / "fixtures" / "issue-502-agreeing-subset"
CHECKED_DATE = "2026-08-17"
TARGET_TITLE = "Exhumed: Unearthing the History of the American Vampire"
TARGET_AUTHOR = "Aaron Mahnke"

AMAZON_URL = "https://www.amazon.com/Exhumed-Unearthing-History-American-Vampire/dp/B0G4P2VQ4P"
US_AMAZON_URL = "https://us.amazon.com/Exhumed-Unearthing-History-American-Vampire-ebook/dp/B0GFP7VDW9"
BOOKSHOP_URL = (
    "https://bookshop.org/p/books/exhumed-unearthing-the-history-of-the-american-vampire-aaron-mahnke/c7e72bebb737534a"
)

WORK_LEVEL_URL = "https://bookshop.org/p/books/exhumed-work-level-claims/0000000000000000"
BODY_FILES: dict[str, Path] = {
    AMAZON_URL: FIXTURES / "amazon-exhumed.html",
    US_AMAZON_URL: FIXTURES / "us-amazon-exhumed-ebook.html",
    BOOKSHOP_URL: FIXTURES / "bookshop-exhumed.html",
    WORK_LEVEL_URL: Path(__file__).parent / "fixtures" / "issue-554" / "work-level-claim-page.html",
}

DESCRIPTION = (
    "Книга исследует связь между историей, правом и ответственностью. "
    "Второй фрагмент раскрывает документальную основу, называет проверенные "
    "страницы и уточняет редакционный ракурс книги. "
    "Третье предложение закрепляет вывод и показывает, почему книга попала в обзор."
)
CLAIM_DISPLAY_TEXT = "Вошёл в список бестселлеров Bookshop.org на неделе публикации."
# Issue #554 tightened the span rule: a supporting span must be one sentence,
# must name the work, and must not name the author. The previous span here,
# "With Exhumed, Mahnke presents a unique history...", names the author and
# asserts no claim at all, so it can no longer support a claim. Neither
# issue-502 page contains a sentence that qualifies, so these tests use the
# constructed work-level page from tests/fixtures/issue-554.
CLAIM_EVIDENCE_SPAN = "Exhumed is a New York Times bestseller."


class FixtureTransport:
    def request(self, url: str, **_kwargs: object) -> HTTPResponse:
        body = BODY_FILES[url].read_bytes()
        return HTTPResponse(
            url=url,
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=body,
        )


class FixtureDiscovery:
    def __init__(self, results: Sequence[DiscoveryResult]) -> None:
        self.results = tuple(results)

    def discover(self, _candidate: Mapping[str, Any], **_kwargs: object) -> Sequence[DiscoveryResult]:
        return self.results


def _classify(url: str) -> dict[str, str]:
    classifier = committed_source_classifier()
    classification = classifier.classify(url)
    assert classification["classification"] == "classified"
    return {
        "source_kind": str(classification["source_kind"]),
        "source_family": str(classification["source_family"]),
        "owner_id": str(classification["owner_id"]),
    }


def _discoveries(urls: Sequence[str]) -> tuple[DiscoveryResult, ...]:
    return tuple(
        DiscoveryResult(
            url=url,
            source_kind=_classify(url)["source_kind"],
            source_family=_classify(url)["source_family"],
            owner_id=_classify(url)["owner_id"],
            source_language="en",
            classification="classified",
        )
        for url in urls
    )


def _candidate() -> dict[str, Any]:
    return {
        "candidate_id": "exhumed|aaron mahnke",
        "identity_key": "exhumed|aaron mahnke",
        "title": TARGET_TITLE,
        "primary_authors": [TARGET_AUTHOR],
        "update_delta": "new",
        "first_seen_date": CHECKED_DATE,
        "priority_label": "A",
    }


def _claim(**overrides: Any) -> dict[str, Any]:
    claim = {
        "claim_type": "bestseller",
        "evidence_urls": [WORK_LEVEL_URL],
        "source_kind": _classify(WORK_LEVEL_URL)["source_kind"],
        "checked_date": CHECKED_DATE,
        "display_text": CLAIM_DISPLAY_TEXT,
        "evidence_span": CLAIM_EVIDENCE_SPAN,
        "title": TARGET_TITLE,
        "primary_authors": [TARGET_AUTHOR],
    }
    claim.update(overrides)
    return claim


class StaticSynthesizer:
    """Returns one fixed synthesis result, whatever the stage supplies."""

    def __init__(self, result: SynthesisResult) -> None:
        self.result = result

    def synthesize(self, _candidate: Mapping[str, Any], sources: Sequence[object]) -> SynthesisResult:
        return self.result


def _run_stage(synthesizer: Any) -> Any:
    stage = GroundedDescriptionStage(
        discovery=FixtureDiscovery(_discoveries((WORK_LEVEL_URL,))),
        fetch=BoundedHTTPFetcher(transport=FixtureTransport()),
        synthesizer=synthesizer,
    )
    return stage.run(
        [_candidate()],
        operation_id="issue-499",
        operation_date=CHECKED_DATE,
        force_candidates=True,
    )


def test_issue_499_accepted_evidence_without_claims_keeps_description_and_zero_claims() -> None:
    outcome = _run_stage(StaticSynthesizer(SynthesisResult(DESCRIPTION))).outcomes[0]

    assert outcome.outcome == OUTCOME_SINGLE_SOURCE
    assert outcome.content_description == DESCRIPTION
    assert outcome.prominence_claims == ()
    assert outcome.reason_detail == ""


def test_issue_499_supported_claim_reaches_outcome_and_rendered_review() -> None:
    artifact = _run_stage(StaticSynthesizer(SynthesisResult(DESCRIPTION, (_claim(),))))
    outcome = artifact.outcomes[0]

    assert outcome.outcome == OUTCOME_SINGLE_SOURCE
    assert outcome.content_description == DESCRIPTION
    assert len(outcome.prominence_claims) == 1
    assert CLAIM_EVIDENCE_SPAN in outcome.research_sources[0].display_text
    claim = outcome.prominence_claims[0]
    assert claim.claim_type == "bestseller"
    assert claim.evidence_urls == (WORK_LEVEL_URL,)
    assert claim.display_text == CLAIM_DISPLAY_TEXT
    assert claim.evidence_span == CLAIM_EVIDENCE_SPAN
    assert claim.claim_type not in outcome.content_description

    rows, _summary = assemble_cumulative_review([_candidate()], [], [], grounded_description_stage=artifact)
    row = rows[0]
    assert row["prominence"] == f"bestseller: {CLAIM_DISPLAY_TEXT} ({WORK_LEVEL_URL})"
    assert row["prominence_claim_type"] == "bestseller"
    assert row["prominence_evidence_url"] == WORK_LEVEL_URL


def test_issue_499_claim_with_fabricated_evidence_url_is_refused_and_recorded() -> None:
    fabricated = _claim(evidence_urls=["https://invented.example.test/bestseller"])
    outcome = _run_stage(StaticSynthesizer(SynthesisResult(DESCRIPTION, (fabricated,)))).outcomes[0]

    assert outcome.outcome == OUTCOME_SINGLE_SOURCE
    assert outcome.content_description == DESCRIPTION
    assert outcome.prominence_claims == ()
    assert outcome.reason_detail == "prominence_claims_refused=bestseller:evidence_url_not_accepted"


def test_issue_499_unknown_claim_type_is_refused_and_recorded() -> None:
    outcome = _run_stage(StaticSynthesizer(SynthesisResult(DESCRIPTION, (_claim(claim_type="award"),)))).outcomes[0]

    assert outcome.content_description == DESCRIPTION
    assert outcome.prominence_claims == ()
    # The detail now names the claim types in the refused batch. "award" is not
    # one of the four allowed types, and the validator reports that as
    # search_or_fetch_failure, which does not describe what happened. Naming the
    # type is what makes the record legible.
    assert outcome.reason_detail == "prominence_claims_refused=batch[award]:search_or_fetch_failure"


def test_issue_499_claim_for_a_different_work_is_refused() -> None:
    other_book = _claim(title="Salt: A World History", primary_authors=["Mark Kurlansky"])
    outcome = _run_stage(StaticSynthesizer(SynthesisResult(DESCRIPTION, (other_book,)))).outcomes[0]

    assert outcome.content_description == DESCRIPTION
    assert outcome.prominence_claims == ()
    assert "title_mismatch" in outcome.reason_detail


def test_issue_499_claim_without_evidence_url_is_refused() -> None:
    outcome = _run_stage(StaticSynthesizer(SynthesisResult(DESCRIPTION, (_claim(evidence_urls=[]),)))).outcomes[0]

    assert outcome.content_description == DESCRIPTION
    assert outcome.prominence_claims == ()
    assert outcome.reason_detail == "prominence_claims_refused=bestseller:evidence_url_not_accepted"


def test_issue_499_contradictory_same_type_claims_are_all_omitted() -> None:
    conflicting = (
        _claim(display_text="Первое противоречивое утверждение о списке бестселлеров."),
        _claim(display_text="Второе противоположное утверждение о списке бестселлеров."),
    )
    outcome = _run_stage(StaticSynthesizer(SynthesisResult(DESCRIPTION, conflicting))).outcomes[0]

    assert outcome.content_description == DESCRIPTION
    assert outcome.prominence_claims == ()
    assert "source_identity_disagreement" in outcome.reason_detail


def test_issue_499_valid_claim_survives_alongside_a_refused_sibling() -> None:
    mixed = (_claim(), _claim(evidence_urls=["https://invented.example.test/bestseller"]))
    outcome = _run_stage(StaticSynthesizer(SynthesisResult(DESCRIPTION, mixed))).outcomes[0]

    assert outcome.content_description == DESCRIPTION
    assert len(outcome.prominence_claims) == 1
    assert outcome.prominence_claims[0].evidence_urls == (WORK_LEVEL_URL,)
    assert outcome.reason_detail == "prominence_claims_refused=bestseller:evidence_url_not_accepted"


def test_issue_499_synthesizer_returning_a_bare_string_fails_loudly() -> None:
    class LegacyStringSynthesizer:
        def synthesize(self, _candidate: Mapping[str, Any], sources: Sequence[object]) -> str:
            return DESCRIPTION

    outcome = _run_stage(LegacyStringSynthesizer()).outcomes[0]

    assert outcome.outcome != OUTCOME_SINGLE_SOURCE
    assert outcome.reason_code == REASON_SYNTHESIS_FAILURE
    assert outcome.reason_detail == "synthesizer raised TypeError"


def test_issue_499_noop_synthesizer_declares_empty_capability_explicitly() -> None:
    result = NoOpDescriptionSynthesizer().synthesize(_candidate(), [])

    assert result == SynthesisResult("")
    assert result.prominence_claims == ()


def test_issue_499_prompt_requests_claims_only_from_accepted_bounded_evidence() -> None:
    assert '"prominence_claims"' in GROUNDED_SYNTHESIS_PROMPT
    assert "bestseller, debut, prize, mentor_or_backing" in GROUNDED_SYNTHESIS_PROMPT
    assert "only URLs copied verbatim from accepted_bounded_evidence" in GROUNDED_SYNTHESIS_PROMPT
    assert "evidence_span" in GROUNDED_SYNTHESIS_PROMPT
    assert "concise original Russian prose" in GROUNDED_SYNTHESIS_PROMPT
    assert "missing, unclear, or contradictory" in GROUNDED_SYNTHESIS_PROMPT
    assert (
        "Never infer a claim from the title, the authors, metadata, or outside knowledge" in GROUNDED_SYNTHESIS_PROMPT
    )
    assert (
        "must not search, browse, follow links, use outside knowledge, or invent URLs/evidence"
        in GROUNDED_SYNTHESIS_PROMPT
    )


def test_issue_499_source_kind_and_checked_date_come_from_the_evidence_not_the_model() -> None:
    """A claim may carry no invented fact, including its own provenance.

    validate_prominence_claim only checks source_kind and checked_date for
    non-emptiness, and it reuses source_kind as source_family and owner_id, so
    a model-supplied string would become an owner identity. The stage already
    holds both facts for the accepted URL, so it stamps them.
    """
    lying = _claim(source_kind="totally-made-up-kind", checked_date="1999-01-01")
    outcome = _run_stage(StaticSynthesizer(SynthesisResult(DESCRIPTION, (lying,)))).outcomes[0]

    assert len(outcome.prominence_claims) == 1
    claim = outcome.prominence_claims[0]
    assert claim.source_kind == _classify(WORK_LEVEL_URL)["source_kind"]
    assert claim.checked_date == CHECKED_DATE
    assert claim.source_kind != "totally-made-up-kind"
    assert claim.checked_date != "1999-01-01"


def test_issue_499_an_unknown_type_spoils_the_batch_and_the_detail_names_it() -> None:
    """The batch is all-or-nothing after URL filtering, and it says which claim.

    A valid claim beside a URL-refused sibling survives, because that sibling is
    filtered per claim. A valid claim beside a sibling the validator rejects does
    not survive, because a model that returned one malformed claim has cast doubt
    on the rest. That asymmetry is deliberate and it must be legible: the detail
    names the claim types in the batch, since the validator reports an unknown
    claim type as search_or_fetch_failure, which does not describe what happened.
    """
    mixed = (_claim(), _claim(claim_type="viral_on_tiktok"))
    outcome = _run_stage(StaticSynthesizer(SynthesisResult(DESCRIPTION, mixed))).outcomes[0]

    assert outcome.content_description == DESCRIPTION
    assert outcome.prominence_claims == ()
    assert "batch[bestseller,viral_on_tiktok]" in outcome.reason_detail
    assert outcome.reason_detail.endswith("search_or_fetch_failure")
