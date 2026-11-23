"""Offline regression coverage for issue #554 prominence evidence spans."""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from autoscout.grounded_description import _SUBJECT_CONTEXT_FIELD_MAX_CHARS
from autoscout.grounded_description import OUTCOME_SINGLE_SOURCE
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import DurableTarget
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import SynthesisResult
from autoscout.grounded_description import _span_supports_work_claim
from autoscout.grounded_description import committed_source_classifier

FIXTURES = Path(__file__).parent / "fixtures" / "issue-502-agreeing-subset"
CHECKED_DATE = "2026-08-17"
TARGET_TITLE = "Exhumed: Unearthing the History of the American Vampire"
TARGET_AUTHOR = "Aaron Mahnke"
BOOKSHOP_URL = (
    "https://bookshop.org/p/books/exhumed-unearthing-the-history-of-the-american-vampire-aaron-mahnke/c7e72bebb737534a"
)
AMAZON_URL = "https://www.amazon.com/Exhumed-Unearthing-History-American-Vampire/dp/B0G4P2VQ4P"
FIXTURE_URLS = (BOOKSHOP_URL, AMAZON_URL)
WORK_LEVEL_URL = "https://bookshop.org/p/books/exhumed-work-level-claims/0000000000000000"
BODY_FILES = {
    BOOKSHOP_URL: FIXTURES / "bookshop-exhumed.html",
    AMAZON_URL: FIXTURES / "amazon-exhumed.html",
    # Constructed page: see tests/fixtures/issue-554/README.md. Neither real
    # fixture contains a sentence that names the work, asserts a claim, and
    # omits the author, so a page that does is needed to prove the rule admits
    # a genuine claim.
    WORK_LEVEL_URL: Path(__file__).parent / "fixtures" / "issue-554" / "work-level-claim-page.html",
}
CLAIM_TYPES = ("bestseller", "debut", "prize", "mentor_or_backing")
DESCRIPTION = (
    "Книга исследует связь между историей, правом и ответственностью. "
    "Второй фрагмент раскрывает документальную основу, называет проверенные "
    "страницы и уточняет редакционный ракурс книги. "
    "Третье предложение закрепляет вывод и показывает, почему книга попала в обзор."
)
AUTHOR_LEVEL_SPAN = "bestselling author Aaron Mahnke"
# One verbatim sentence per claim type, each naming the work, asserting the
# claim, and not naming the author. The previous single span was
# "With Exhumed, Mahnke presents a unique history..." which names the author and
# asserts no claim at all, so it demonstrated the plumbing rather than the rule.
WORK_LEVEL_SPANS = {
    "bestseller": "Exhumed is a New York Times bestseller.",
    "debut": "Exhumed is a debut work of narrative nonfiction.",
    "prize": "Winner of the 2026 Baillie Gifford Prize, Exhumed reframes a nineteenth century panic.",
    "mentor_or_backing": "Exhumed is published with the backing of the Lore Foundation.",
}


class FixtureTransport:
    def request(self, url: str, **_kwargs: object) -> HTTPResponse:
        return HTTPResponse(
            url=url,
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=BODY_FILES[url].read_bytes(),
        )


class FixtureDiscovery:
    def __init__(self, url: str) -> None:
        self.url = url

    def discover(self, _candidate: Mapping[str, Any], **_kwargs: object) -> Sequence[DiscoveryResult]:
        classification = committed_source_classifier().classify(self.url)
        assert classification["classification"] == "classified"
        return (
            DiscoveryResult(
                url=self.url,
                source_kind=str(classification["source_kind"]),
                source_family=str(classification["source_family"]),
                owner_id=str(classification["owner_id"]),
                source_language="en",
                classification="classified",
            ),
        )


class StaticSynthesizer:
    def __init__(self, claims: Sequence[Mapping[str, Any]] = ()) -> None:
        self.claims = tuple(claims)

    def synthesize(self, _candidate: Mapping[str, Any], sources: Sequence[object]) -> SynthesisResult:
        return SynthesisResult(DESCRIPTION, self.claims)


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


def _claim(url: str, claim_type: str, evidence_span: str) -> dict[str, Any]:
    return {
        "claim_type": claim_type,
        "evidence_urls": [url],
        "source_kind": "model-invented-kind",
        "checked_date": "1999-01-01",
        "display_text": "Краткое подтвержденное утверждение о книге.",
        "evidence_span": evidence_span,
        "title": TARGET_TITLE,
        "primary_authors": [TARGET_AUTHOR],
    }


def _run_stage(url: str, claims: Sequence[Mapping[str, Any]]) -> Any:
    stage = GroundedDescriptionStage(
        discovery=FixtureDiscovery(url),
        fetch=BoundedHTTPFetcher(transport=FixtureTransport()),
        synthesizer=StaticSynthesizer(claims),
    )
    return stage.run(
        [_candidate()],
        operation_id="issue-554",
        operation_date=CHECKED_DATE,
        force_candidates=True,
    )


def _accepted_source(url: str) -> Any:
    artifact = _run_stage(url, ())
    outcome = artifact.outcomes[0]
    assert len(outcome.research_sources) == 1
    return outcome.research_sources[0]


@pytest.mark.parametrize("url", FIXTURE_URLS)
@pytest.mark.parametrize("claim_type", CLAIM_TYPES)
def test_issue_554_author_level_span_is_refused_for_every_claim_type(url: str, claim_type: str) -> None:
    claim = _claim(url, claim_type, AUTHOR_LEVEL_SPAN)

    outcome = _run_stage(url, (claim,)).outcomes[0]

    assert outcome.outcome == OUTCOME_SINGLE_SOURCE
    assert outcome.content_description == DESCRIPTION
    assert outcome.prominence_claims == ()
    assert outcome.reason_detail == f"prominence_claims_refused={claim_type}:evidence_span_not_work_level"


@pytest.mark.parametrize("claim_type", CLAIM_TYPES)
def test_issue_554_work_level_span_is_accepted_for_every_claim_type(claim_type: str) -> None:
    span = WORK_LEVEL_SPANS[claim_type]
    claim = _claim(WORK_LEVEL_URL, claim_type, span)

    outcome = _run_stage(WORK_LEVEL_URL, (claim,)).outcomes[0]

    assert outcome.outcome == OUTCOME_SINGLE_SOURCE
    assert outcome.content_description == DESCRIPTION
    assert len(outcome.prominence_claims) == 1
    assert span in outcome.research_sources[0].display_text
    assert outcome.prominence_claims[0].claim_type == claim_type
    assert outcome.prominence_claims[0].evidence_span == span


def test_issue_554_both_exhumed_fixtures_support_no_claims() -> None:
    """The measured case from issue #554: these two pages support zero claims.

    Both state their prominence language about the author. Neither contains a
    single sentence that names the work, asserts a claim, and omits the author,
    so the correct claim count for both is zero.
    """
    import re

    for url in FIXTURE_URLS:
        text = _accepted_source(url).display_text
        qualifying = [
            sentence
            for sentence in re.split(r"(?<=[.!?])\s+", text)
            if _span_supports_work_claim(sentence, TARGET_TITLE, (TARGET_AUTHOR,))
        ]
        assert qualifying == [], url


def test_issue_554_span_missing_from_captured_page_is_refused() -> None:
    claim = _claim(BOOKSHOP_URL, "bestseller", "Exhumed is a bestseller according to an uncaptured page.")

    outcome = _run_stage(BOOKSHOP_URL, (claim,)).outcomes[0]

    assert outcome.content_description == DESCRIPTION
    assert outcome.prominence_claims == ()
    assert outcome.reason_detail == "prominence_claims_refused=bestseller:evidence_span_not_in_source"


def test_issue_554_missing_span_is_refused() -> None:
    claim = _claim(BOOKSHOP_URL, "bestseller", "")

    outcome = _run_stage(BOOKSHOP_URL, (claim,)).outcomes[0]

    assert outcome.content_description == DESCRIPTION
    assert outcome.prominence_claims == ()
    assert outcome.reason_detail == "prominence_claims_refused=bestseller:evidence_span_missing"


def test_issue_554_span_longer_than_bound_is_refused() -> None:
    claim = _claim(BOOKSHOP_URL, "bestseller", WORK_LEVEL_SPANS["bestseller"] + (" extra" * 100))

    outcome = _run_stage(BOOKSHOP_URL, (claim,)).outcomes[0]

    assert outcome.content_description == DESCRIPTION
    assert outcome.prominence_claims == ()
    assert outcome.reason_detail == "prominence_claims_refused=bestseller:evidence_span_too_long"


def test_issue_554_span_on_different_accepted_source_than_cited_is_refused() -> None:
    cited_source = replace(_accepted_source(BOOKSHOP_URL), display_text=AUTHOR_LEVEL_SPAN)
    other_source = _accepted_source(AMAZON_URL)
    claim = _claim(BOOKSHOP_URL, "bestseller", WORK_LEVEL_SPANS["bestseller"])
    target = DurableTarget("exhumed|aaron mahnke", TARGET_TITLE, (TARGET_AUTHOR,), _candidate())

    claims, detail = GroundedDescriptionStage._validated_prominence_claims(
        SynthesisResult(DESCRIPTION, (claim,)), target, (cited_source, other_source)
    )

    assert claims == ()
    assert detail == "prominence_claims_refused=bestseller:evidence_span_not_in_source"


# ── Span widening (review follow-up) ────────────────────────────────────────
# The first version of this fix required only that the title token appear
# somewhere in the span. The model chooses where a span begins and ends, so it
# could widen the span until a title appeared and the check became meaningless.


def test_issue_554_a_span_widened_across_sentences_is_refused() -> None:
    """The measured exploit: a verbatim span that borrows a distant title.

    This span is a real 449-character extract of bookshop-exhumed.html. Its
    only prominence language is "bestselling author Aaron Mahnke", and the
    work title appears four sentences later in an unrelated sentence. Before
    this fix the stage accepted it as a bestseller claim for Exhumed.
    """
    source_text = _accepted_source(BOOKSHOP_URL).display_text
    start = source_text.index("bestselling author")
    end = source_text.index("Exhumed", start) + len("Exhumed")
    widened = source_text[start:end]

    assert widened in source_text
    assert len(widened) <= _SUBJECT_CONTEXT_FIELD_MAX_CHARS
    assert "bestselling author" in widened
    assert "Exhumed" in widened

    outcome = _run_stage(BOOKSHOP_URL, (_claim(BOOKSHOP_URL, "bestseller", widened),)).outcomes[0]

    assert outcome.content_description == DESCRIPTION
    assert outcome.prominence_claims == ()
    assert "bestseller:evidence_span_not_work_level" in outcome.reason_detail


def test_issue_554_one_sentence_naming_work_and_author_is_refused() -> None:
    """A sentence naming both leaves the claim ambiguous, so it is refused.

    "Aaron Mahnke, bestselling author of The World of Lore, returns with
    Exhumed" names the work and says a different book is the bestseller. A
    token rule cannot tell that from "Exhumed is a bestseller", so the
    ambiguous case is refused rather than resolved.
    """
    span = "Aaron Mahnke, bestselling author of The World of Lore, returns with Exhumed"

    assert _span_supports_work_claim(span, TARGET_TITLE, (TARGET_AUTHOR,)) is False


def test_issue_554_a_work_level_sentence_without_the_author_is_accepted() -> None:
    """The rule must still admit a genuine book-level claim."""
    assert _span_supports_work_claim("Exhumed is a New York Times bestseller.", TARGET_TITLE, (TARGET_AUTHOR,))
    assert _span_supports_work_claim(
        "Winner of the 2026 Baillie Gifford Prize, Exhumed reframes a panic.",
        TARGET_TITLE,
        (TARGET_AUTHOR,),
    )


def test_issue_554_the_three_span_conditions_are_each_required() -> None:
    """One sentence, the title present, and no author named: all three bind."""
    # Multi-sentence, title present, no author named.
    assert _span_supports_work_claim("A panic gripped New England. Exhumed is a bestseller.", TARGET_TITLE, ()) is False
    # One sentence, no title.
    assert _span_supports_work_claim("A gripping account of nineteenth century folklore.", TARGET_TITLE, ()) is False
    # One sentence, title present, author named.
    assert (
        _span_supports_work_claim("Exhumed by Aaron Mahnke is a bestseller.", TARGET_TITLE, (TARGET_AUTHOR,)) is False
    )
    # All three satisfied.
    assert _span_supports_work_claim("Exhumed is a bestseller.", TARGET_TITLE, (TARGET_AUTHOR,)) is True
