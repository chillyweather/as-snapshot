"""Fixture-backed and synthetic regression coverage for issue #505.

The fixture-backed tests cover the pair agreement, subset monotonicity, and
the stage-level live shape against the faithful issue-502 retailer fixtures.
The synthetic tests at the bottom cover the distinct-owner independence seam,
which the five faithful fixtures cannot reach because they all classify to one
shared owner id.  The disagreement matrix stays in
tests/test_issue_502_agreeing_subset.py.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from autoscout.grounded_description import OUTCOME_SINGLE_SOURCE
from autoscout.grounded_description import REASON_FEWER_THAN_TWO
from autoscout.grounded_description import REASON_SOURCE_IDENTITY_DISAGREEMENT
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import DurableTarget
from autoscout.grounded_description import FetchedSource
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import SynthesisResult
from autoscout.grounded_description import committed_source_classifier
from autoscout.grounded_description import evaluate_grounding_gate

FIXTURES = Path(__file__).parent / "fixtures" / "issue-502-agreeing-subset"
CHECKED_DATE = "2026-08-17"
TARGET_TITLE = "Exhumed: Unearthing the History of the American Vampire"
TARGET_AUTHOR = "Aaron Mahnke"

AMAZON_URL = "https://www.amazon.com/Exhumed-Unearthing-History-American-Vampire/dp/B0G4P2VQ4P"
US_AMAZON_URL = "https://us.amazon.com/Exhumed-Unearthing-History-American-Vampire-ebook/dp/B0GFP7VDW9"
BOOKSHOP_URL = (
    "https://bookshop.org/p/books/exhumed-unearthing-the-history-of-the-american-vampire-aaron-mahnke/c7e72bebb737534a"
)
HACHETTE_URL = "https://www.hachettebookgroup.com/titles/aaron-mahnke/exhumed/9798894141817/"
BARNES_NOBLE_URL = "https://www.barnesandnoble.com/w/exhumed-aaron-mahnke/1148854448"

ALL_URLS = (AMAZON_URL, US_AMAZON_URL, BOOKSHOP_URL, HACHETTE_URL, BARNES_NOBLE_URL)

BODY_FILES: dict[str, str] = {
    AMAZON_URL: "amazon-exhumed.html",
    US_AMAZON_URL: "us-amazon-exhumed-ebook.html",
    BOOKSHOP_URL: "bookshop-exhumed.html",
    HACHETTE_URL: "hachette-exhumed.html",
    BARNES_NOBLE_URL: "barnesandnoble-exhumed.html",
}


class FixtureTransport:
    def __init__(self) -> None:
        self.requested_urls: list[str] = []

    def request(self, url: str, **_kwargs: object) -> HTTPResponse:
        self.requested_urls.append(url)
        body = (FIXTURES / BODY_FILES[url]).read_bytes()
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


class RussianSynthesizer:
    def synthesize(self, _candidate: Mapping[str, Any], sources: Sequence[object]) -> SynthesisResult:
        return SynthesisResult(
            "Книга исследует связь между историей, правом и ответственностью. "
            "Второй фрагмент раскрывает документальную основу, называет проверенные "
            "страницы и уточняет редакционный ракурс книги. "
            "Третье предложение закрепляет вывод и показывает, почему книга попала в обзор."
        )


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


def _fetched_sources(urls: Sequence[str]) -> list[FetchedSource]:
    fetcher = BoundedHTTPFetcher(transport=FixtureTransport())
    return [fetcher.fetch(result, checked_date=CHECKED_DATE) for result in _discoveries(urls)]


def _target() -> DurableTarget:
    return DurableTarget(
        candidate_id="exhumed|aaron mahnke",
        title=TARGET_TITLE,
        primary_authors=(TARGET_AUTHOR,),
        record={
            "candidate_id": "exhumed|aaron mahnke",
            "title": TARGET_TITLE,
            "primary_authors": [TARGET_AUTHOR],
        },
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


def _source(slug: str, *, title: str, subject: str, authors: Sequence[str] = ()) -> FetchedSource:
    return FetchedSource(
        url=f"https://{slug}.example.test/book",
        source_kind="review",
        source_family="book-review",
        owner_id=f"owner-{slug}",
        source_language="en",
        title=title,
        primary_authors=tuple(authors),
        subject_text=subject,
        checked_date=CHECKED_DATE,
    )


def _salt_target() -> DurableTarget:
    """Salt/Kurlansky target for the synthetic seam tests below.

    The faithful issue-502 fixtures all classify to one shared owner id, so
    the distinct-owner and single-source seams need hand-written doubles.
    """
    return DurableTarget("salt", "Salt", ("Mark Kurlansky",), {})


def test_issue_505_two_authorless_sources_with_author_evidence_agree() -> None:
    """Two authorless sources whose fetched subjects both name the target author agree.

    Issue #505 lets the authorless pair form identity evidence when both fetched
    subjects name Aaron Mahnke.  The fixture pair shares one owner id, so the
    gate accepts the deterministic single source and records the other as
    fewer-than-two.
    """
    result = evaluate_grounding_gate(_target(), _fetched_sources((US_AMAZON_URL, BARNES_NOBLE_URL)))

    assert result.accepted is True
    assert result.reason_code == ""
    assert tuple(source.canonical_url for source in result.accepted_sources) == (US_AMAZON_URL,)
    rejected = {item.canonical_url: item.reason_code for item in result.rejected_sources}
    assert rejected == {BARNES_NOBLE_URL: REASON_FEWER_THAN_TWO}


def test_issue_505_every_retailer_fixture_subset_grounds() -> None:
    """Every non-empty subset of the five retailer fixtures grounds the gate.

    This pins "adding further copies of agreeing evidence never turns an
    accepted candidate into a refusal" on real markup; before #505 the three
    authorless pairs and the authorless triple were refused, every superset
    and singleton accepted.
    """
    for size in range(1, 6):
        for subset in itertools.combinations(ALL_URLS, size):
            result = evaluate_grounding_gate(_target(), _fetched_sources(subset))
            assert result.accepted is True
            assert result.reason_code == ""


def test_issue_505_real_stage_poisoning_pair_grounds() -> None:
    """The real stage grounds the exhumed|aaron mahnke poisoning pair.

    This is the offline end-to-end proof for the exhumed|aaron mahnke
    poisoning pair: live Amazon and Bookshop fetching is CAPTCHA/Cloudflare-
    gated, which is why the captured fixtures are the evidence channel.
    """
    stage = GroundedDescriptionStage(
        discovery=FixtureDiscovery(_discoveries((BARNES_NOBLE_URL, US_AMAZON_URL))),
        fetch=BoundedHTTPFetcher(transport=FixtureTransport()),
        synthesizer=RussianSynthesizer(),
    )

    artifact = stage.run(
        [_candidate()],
        operation_id="issue-505",
        operation_date=CHECKED_DATE,
        force_candidates=True,
    )
    outcome = artifact.outcomes[0]

    assert outcome.outcome == OUTCOME_SINGLE_SOURCE
    assert tuple(source.canonical_url for source in outcome.research_sources) == (US_AMAZON_URL,)
    assert len(outcome.rejected_source_reasons) == 1
    rejected = outcome.rejected_source_reasons[0]
    assert rejected.canonical_url == BARNES_NOBLE_URL
    assert rejected.reason_code == REASON_FEWER_THAN_TWO


def test_issue_505_two_authorless_sources_without_author_evidence_stay_disagreement() -> None:
    """Two authorless sources stay a disagreement when neither subject names the target author.

    Issue #505 lets two authorless sources agree only when BOTH fetched
    subjects name a target author.  Two mineral-only subjects name no author,
    so the gate must keep refusing with the identity-disagreement reason.
    """
    result = evaluate_grounding_gate(
        _salt_target(),
        [
            _source(
                "mineral-a",
                title="Salt",
                subject="Salt is a mineral composed primarily of sodium chloride used in food seasoning.",
            ),
            _source(
                "mineral-b",
                title="Salt",
                subject="Salt is essential to food preservation and food seasoning.",
            ),
        ],
    )

    assert not result.accepted
    assert result.reason_code == REASON_SOURCE_IDENTITY_DISAGREEMENT


def test_issue_505_two_authorless_sources_with_author_evidence_form_independent_set() -> None:
    """Two authorless sources whose subjects both name Kurlansky form an independent set.

    Issue #505 lets the authorless pair agree when both fetched subjects name
    the target author.  Distinct URLs and distinct owner ids make the pair an
    independent set, so the gate accepts both sources.  The five faithful
    issue-502 fixtures all classify to one shared owner id, so this
    distinct-owner independence seam needs hand-written doubles.
    """
    result = evaluate_grounding_gate(
        _salt_target(),
        [
            _source("salt-a", title="Salt", subject="Kurlansky traces the world history of salt."),
            _source("salt-b", title="Salt", subject="Kurlansky follows salt from ancient China to the Erie Canal."),
        ],
    )

    assert result.accepted is True
    assert result.reason_code == ""
    assert tuple(source.canonical_url for source in result.accepted_sources) == (
        "https://salt-a.example.test/book",
        "https://salt-b.example.test/book",
    )


def test_issue_505_adding_agreeing_authorless_copy_never_refuses() -> None:
    """Three agreeing authorless sources must all be accepted.

    Adding an agreeing authorless copy must never flip the gate into a
    refusal: every pair agrees on identity, so the whole set grounds.  The
    faithful fixtures cannot supply three distinct owner ids, so this
    copy-adding seam needs hand-written doubles.
    """
    result = evaluate_grounding_gate(
        _salt_target(),
        [
            _source("salt-a", title="Salt", subject="Kurlansky traces the world history of salt."),
            _source("salt-b", title="Salt", subject="Kurlansky follows salt from ancient China to the Erie Canal."),
            _source("salt-c", title="Salt", subject="Kurlansky describes the Gandhi salt march."),
        ],
    )

    assert result.accepted is True
    assert result.reason_code == ""
    assert tuple(source.canonical_url for source in result.accepted_sources) == (
        "https://salt-a.example.test/book",
        "https://salt-b.example.test/book",
        "https://salt-c.example.test/book",
    )


def test_issue_505_single_authorless_source_with_author_evidence_stays_accepted() -> None:
    """A single authorless source naming the target author in its subject stays accepted.

    This pins the single-source evidence standard that the two-source pair
    branch must match: authorless subject evidence is enough for one source,
    so it must also be enough for two agreeing sources.  The hand-written
    double isolates this single-source seam from the fixture pair shapes.
    """
    result = evaluate_grounding_gate(
        _salt_target(),
        [_source("salt-a", title="Salt", subject="Kurlansky traces the world history of salt.")],
    )

    assert result.accepted is True
    assert result.reason_code == ""
    assert tuple(source.canonical_url for source in result.accepted_sources) == ("https://salt-a.example.test/book",)


def test_issue_505_named_sources_naming_different_target_coauthors_agree() -> None:
    """Named sources naming different target co-authors agree under the single standard.

    Each source individually passes the single-source evidence standard
    (its extracted author is one of the target's primary authors), so the
    pair must agree.  Before #505 the pair rule declared them a pairwise
    disagreement, so adding a correct second co-author source made the
    candidate fail - the named-source analogue of the inversion #505 stops.
    """
    target = DurableTarget("multi", "Shared Work", ("Author One", "Author Two"), {})
    result = evaluate_grounding_gate(
        target,
        [
            _source("coauthor-a", title="Shared Work", authors=("Author One",), subject="shared topic one"),
            _source("coauthor-b", title="Shared Work", authors=("Author Two",), subject="shared topic two"),
        ],
    )

    assert result.accepted is True
    assert result.reason_code == ""
    assert tuple(source.canonical_url for source in result.accepted_sources) == (
        "https://coauthor-a.example.test/book",
        "https://coauthor-b.example.test/book",
    )
