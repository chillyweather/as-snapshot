"""Offline regression coverage for issue #502 agreeing-subset grounding."""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from autoscout.grounded_description import OUTCOME_SINGLE_SOURCE
from autoscout.grounded_description import REASON_FEWER_THAN_TWO
from autoscout.grounded_description import REASON_SOURCE_IDENTITY_DISAGREEMENT
from autoscout.grounded_description import REASON_SUBJECT_DISAGREEMENT
from autoscout.grounded_description import REASON_TITLE_MISMATCH
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import DurableTarget
from autoscout.grounded_description import FetchedSource
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import GroundingGateResult
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import SynthesisResult
from autoscout.grounded_description import _identity_reason
from autoscout.grounded_description import _source_pair_identity_disagrees
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

EXPECTED_TITLES: dict[str, str] = {
    # Issue #504 strips the retailer chrome from the Amazon listing title.
    AMAZON_URL: "Exhumed: Unearthing the History of the American Vampire",
    US_AMAZON_URL: "Exhumed: Unearthing the History of the American Vampire",
    BOOKSHOP_URL: "Exhumed",
    HACHETTE_URL: "Exhumed",
    BARNES_NOBLE_URL: "Exhumed: Unearthing the History of the American Vampire",
}

EXPECTED_AUTHORS: dict[str, tuple[str, ...]] = {
    AMAZON_URL: (),
    US_AMAZON_URL: (),
    BOOKSHOP_URL: ("Aaron Mahnke",),
    HACHETTE_URL: ("Aaron Mahnke",),
    BARNES_NOBLE_URL: (),
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


def _assert_refused_with_identity_disagreement(result: GroundingGateResult) -> None:
    assert result.accepted is False
    assert tuple(source.canonical_url for source in result.accepted_sources) == ()
    assert result.reason_code == REASON_SOURCE_IDENTITY_DISAGREEMENT


def test_issue_502_fixture_fidelity_and_disagreement_matrix() -> None:
    target = _target()
    sources = {source.url: source for source in _fetched_sources(ALL_URLS)}

    assert set(sources) == set(ALL_URLS)
    for url in ALL_URLS:
        assert sources[url].title == EXPECTED_TITLES[url]
        assert sources[url].primary_authors == EXPECTED_AUTHORS[url]

    assert _identity_reason(target, sources[AMAZON_URL]) == ""
    for url in (US_AMAZON_URL, BOOKSHOP_URL, HACHETTE_URL, BARNES_NOBLE_URL):
        assert _identity_reason(target, sources[url]) == ""

    def disagrees(first: str, second: str) -> bool:
        return _source_pair_identity_disagrees(target, sources[first], sources[second])

    # The chrome-stripped Amazon title (issue #504) now agrees with the
    # author-carrying sources whose fetched text names the target author.
    # The authorless pairings agree too (issue #505): both fetched subjects
    # name Aaron Mahnke, so each authorless pair forms identity evidence.
    for url in (BOOKSHOP_URL, HACHETTE_URL):
        assert not disagrees(AMAZON_URL, url)
        assert not disagrees(url, AMAZON_URL)
    for first, second in (
        (AMAZON_URL, US_AMAZON_URL),
        (AMAZON_URL, BARNES_NOBLE_URL),
        (US_AMAZON_URL, BARNES_NOBLE_URL),
    ):
        assert not disagrees(first, second)
        assert not disagrees(second, first)
    for first, second in (
        (US_AMAZON_URL, BOOKSHOP_URL),
        (US_AMAZON_URL, HACHETTE_URL),
        (BOOKSHOP_URL, HACHETTE_URL),
        (BOOKSHOP_URL, BARNES_NOBLE_URL),
        (HACHETTE_URL, BARNES_NOBLE_URL),
    ):
        assert not disagrees(first, second)
        assert not disagrees(second, first)


def test_issue_502_full_five_source_set_grounds_from_agreeing_subset() -> None:
    result = evaluate_grounding_gate(_target(), _fetched_sources(ALL_URLS))

    assert result.accepted is True
    assert result.reason_code == ""
    # Under the owner policy (#687, #1123) the publisher is its own owner and
    # the storefronts share one, so Hachette and Bookshop form the pair and
    # the other storefronts add nothing.
    assert tuple(source.canonical_url for source in result.accepted_sources) == (HACHETTE_URL, BOOKSHOP_URL)
    assert tuple(source.canonical_url for source in result.rejected_sources) == (
        AMAZON_URL,
        US_AMAZON_URL,
        BARNES_NOBLE_URL,
    )
    assert tuple(item.reason_code for item in result.rejected_sources) == (
        REASON_FEWER_THAN_TWO,
        REASON_FEWER_THAN_TWO,
        REASON_FEWER_THAN_TWO,
    )
    assert result.reason_code != REASON_SOURCE_IDENTITY_DISAGREEMENT
    assert all(item.reason_code != REASON_SOURCE_IDENTITY_DISAGREEMENT for item in result.rejected_sources)


def test_issue_502_hachette_bookshop_and_chrome_amazon_grounds_hachette_and_bookshop() -> None:
    result = evaluate_grounding_gate(_target(), _fetched_sources((HACHETTE_URL, BOOKSHOP_URL, AMAZON_URL)))

    assert result.accepted is True
    assert result.reason_code == ""
    assert tuple(source.canonical_url for source in result.accepted_sources) == (HACHETTE_URL, BOOKSHOP_URL)
    rejected = {item.canonical_url: item.reason_code for item in result.rejected_sources}
    assert rejected == {AMAZON_URL: REASON_FEWER_THAN_TWO}


def test_issue_502_live_three_source_shape_grounds_bookshop() -> None:
    result = evaluate_grounding_gate(_target(), _fetched_sources((BOOKSHOP_URL, US_AMAZON_URL, AMAZON_URL)))

    assert result.accepted is True
    assert result.reason_code == ""
    assert tuple(source.canonical_url for source in result.accepted_sources) == (BOOKSHOP_URL,)
    rejected = {item.canonical_url: item.reason_code for item in result.rejected_sources}
    assert rejected == {
        US_AMAZON_URL: REASON_FEWER_THAN_TWO,
        AMAZON_URL: REASON_FEWER_THAN_TWO,
    }


def test_issue_502_real_stage_single_source_from_live_shape() -> None:
    transport = FixtureTransport()
    stage = GroundedDescriptionStage(
        discovery=FixtureDiscovery(_discoveries((BOOKSHOP_URL, US_AMAZON_URL, AMAZON_URL))),
        fetch=BoundedHTTPFetcher(transport=transport),
        synthesizer=RussianSynthesizer(),
    )

    artifact = stage.run(
        [_candidate()],
        operation_id="issue-502",
        operation_date=CHECKED_DATE,
        force_candidates=True,
    )
    outcome = artifact.outcomes[0]

    assert set(transport.requested_urls) == {BOOKSHOP_URL, US_AMAZON_URL, AMAZON_URL}
    assert len(transport.requested_urls) == 3
    assert outcome.outcome == OUTCOME_SINGLE_SOURCE
    assert outcome.content_description == (
        "Книга исследует связь между историей, правом и ответственностью. "
        "Второй фрагмент раскрывает документальную основу, называет проверенные "
        "страницы и уточняет редакционный ракурс книги. "
        "Третье предложение закрепляет вывод и показывает, почему книга попала в обзор."
    )
    assert tuple(source.canonical_url for source in outcome.research_sources) == (BOOKSHOP_URL,)
    assert all(item.reason_code != REASON_SOURCE_IDENTITY_DISAGREEMENT for item in outcome.rejected_source_reasons)


def test_issue_502_issue_485_salt_shape_accepts_only_the_author_page() -> None:
    target = DurableTarget("salt", "Salt", ("Mark Kurlansky",), {})
    result = evaluate_grounding_gate(
        target,
        [
            FetchedSource(
                url="https://mineral.example.test/book",
                source_kind="review",
                source_family="book-review",
                owner_id="owner-mineral",
                source_language="en",
                title="Salt",
                primary_authors=(),
                subject_text=(
                    "Salt is a mineral composed primarily of sodium chloride used in food seasoning and preservation."
                ),
                checked_date=CHECKED_DATE,
            ),
            FetchedSource(
                url="https://kurlansky.example.test/book",
                source_kind="review",
                source_family="book-review",
                owner_id="owner-kurlansky",
                source_language="en",
                title="Salt",
                primary_authors=("Mark Kurlansky",),
                subject_text=(
                    "Kurlansky traces the world history of salt from ancient China through the Erie Canal "
                    "and the Gandhi salt march."
                ),
                checked_date=CHECKED_DATE,
            ),
        ],
    )

    # #866 keeps #485's mineral-text hazard closed: only the author page
    # may feed synthesis; a silent page does not veto that evidenced source.
    assert result.accepted
    assert result.reason_code == ""
    assert tuple(source.canonical_url for source in result.accepted_sources) == ("https://kurlansky.example.test/book",)
    assert "https://mineral.example.test/book" not in {source.canonical_url for source in result.accepted_sources}


def test_issue_502_issue_491_atomic_habits_shape_now_single_source() -> None:
    target = DurableTarget("atomic-habits", "Atomic Habits", ("James Clear",), {})
    urls = (
        "https://matrix.example/atomic-a",
        "https://matrix.example/atomic-b",
        "https://matrix.example/deep-work",
    )
    result = evaluate_grounding_gate(
        target,
        [
            FetchedSource(
                url=urls[0],
                source_kind="review",
                source_family="book-review",
                owner_id="owner-ab",
                source_language="en",
                title="Atomic Habits",
                primary_authors=("James Clear",),
                subject_text="habits and behavior",
                checked_date=CHECKED_DATE,
            ),
            FetchedSource(
                url=urls[1],
                source_kind="review",
                source_family="book-review",
                owner_id="owner-ab",
                source_language="en",
                title="Atomic Habits",
                primary_authors=("James Clear",),
                subject_text="habits and behavior",
                checked_date=CHECKED_DATE,
            ),
            FetchedSource(
                url=urls[2],
                source_kind="review",
                source_family="book-review",
                owner_id="owner-dw",
                source_language="en",
                title="Deep Work",
                primary_authors=("Cal Newport",),
                subject_text="focus and productivity",
                checked_date=CHECKED_DATE,
            ),
        ],
    )

    # After #525, a different-book title mismatch no longer vetoes an
    # otherwise agreeing subset, so the two Atomic Habits with same owner
    # now correctly yield a single_source with the Deep Work page recorded
    # as title_mismatch. The old expectation was refused with all three.
    assert result.accepted is True
    assert tuple(source.canonical_url for source in result.accepted_sources) == (urls[0],)
    assert tuple(source.canonical_url for source in result.rejected_sources) == (urls[2], urls[1])
    assert result.reason_code == ""


def test_issue_502_bookshop_grounds_alongside_agreeing_amazon_listing() -> None:
    """The chrome-stripped Amazon listing must agree with Bookshop, not poison it.

    Issue #504 removes the retailer chrome from the Amazon page title, so the
    listing carries the book title itself.  The pair now forms an agreeing
    subset: Bookshop remains the accepted named source and Amazon is recorded
    as fewer-than-two with no title or identity conflict anywhere.
    """
    result = evaluate_grounding_gate(_target(), _fetched_sources((BOOKSHOP_URL, AMAZON_URL)))

    assert result.accepted is True
    assert result.reason_code == ""
    assert tuple(source.canonical_url for source in result.accepted_sources) == (BOOKSHOP_URL,)
    rejected = {item.canonical_url: item.reason_code for item in result.rejected_sources}
    assert rejected == {AMAZON_URL: REASON_FEWER_THAN_TWO}
    assert all(item.reason_code != REASON_TITLE_MISMATCH for item in result.rejected_sources)


def test_issue_502_singleton_subset_refuses_subject_conflict_with_authorless_peers() -> None:
    """A subject-disjoint authorless peer must keep the singleton refused.

    Source 0 carries the target author's name; sources 1 to 3 are authorless
    but mention the author in their fetched subject.  Since issue #505, every
    pair agrees on identity: the authorless peers name the target author, so
    identity no longer conflicts.  The surviving conflict is the subject
    disagreement between the named source and the authorless peers, and the
    gate must refuse with that subject-disagreement reason and accept nothing.
    """
    target = DurableTarget("shared-work", "Shared Work", ("Author One",), {})
    texts = ("history culture", "Author One biology", "Author One astronomy", "Author One economics")
    sources = [
        FetchedSource(
            url=f"https://example.test/{index}",
            source_kind="review",
            source_family="book-review",
            owner_id=f"owner-{index}",
            source_language="en",
            title="Shared Work",
            primary_authors=(("Author One",) if index == 0 else ()),
            subject_text=text,
            checked_date=CHECKED_DATE,
        )
        for index, text in enumerate(texts)
    ]

    result = evaluate_grounding_gate(target, sources)

    assert result.accepted is False
    assert tuple(source.canonical_url for source in result.accepted_sources) == ()
    assert result.reason_code == REASON_SUBJECT_DISAGREEMENT
    assert all(item.reason_code != REASON_TITLE_MISMATCH for item in result.rejected_sources), (
        "singleton refusal must surface the subject-disagreement code, not a title mismatch"
    )
