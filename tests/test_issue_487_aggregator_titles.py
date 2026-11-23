"""Regression coverage for issue #487 aggregator title identity matching."""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import ClassVar

import pytest

from autoscout.grounded_description import OUTCOME_GROUNDED
from autoscout.grounded_description import REASON_SOURCE_IDENTITY_DISAGREEMENT
from autoscout.grounded_description import REASON_TITLE_MISMATCH
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import DurableTarget
from autoscout.grounded_description import FetchedSource
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import SynthesisResult
from autoscout.grounded_description import evaluate_grounding_gate
from autoscout.grounded_description import grounding_titles_match

FIXTURES = Path(__file__).parent / "fixtures" / "issue-487-aggregator-titles"
CHECKED_DATE = "2026-08-16"
BOOK_MARKS_URL = "https://bookmarks.reviews/reviews/all/the-devil-in-the-white-city-murder-magic-and-madness-at-the-fair-that-changed-america/"
WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/The_Devil_in_the_White_City"
TARGET_TITLE = "The Devil in the White City"
BOOK_MARKS_SHORT_TITLE = "Book Marks reviews of The Devil in the White City by Erik Larson"
BOOK_MARKS_FULL_TITLE = (
    "Book Marks reviews of The Devil in the White City: Murder, Magic, and Madness at the Fair That Changed America "
    "by Erik Larson"
)
BOOK_MARKS_PAGE_TITLE = "All Book Marks reviews for The Devil in the White City: Murder, Magic, and Madness at the Fair That Changed America"
AUTHOR = "Erik Larson"
BOOK_MARKS_SUBJECT = (
    "The Devil in the White City by Erik Larson has an overall rating of Rave based on 25 book reviews."
)
WIKIPEDIA_SUBJECT = "The Devil in the White City is a nonfiction book by Erik Larson about the 1893 Chicago World's Fair and H. H. Holmes."


class FixtureTransport:
    bodies: ClassVar[dict[str, str]] = {
        BOOK_MARKS_URL: "book-marks-reviews-all.html",
        WIKIPEDIA_URL: "wikipedia-devil-in-the-white-city.html",
    }

    def __init__(self) -> None:
        self.requested_urls: list[str] = []

    def request(self, url: str, **_kwargs: object) -> HTTPResponse:
        self.requested_urls.append(url)
        body = (FIXTURES / self.bodies[url]).read_bytes()
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
            "Книга исследует историю Чикаго, Всемирной выставки и преступлений. "
            "Второй фрагмент раскрывает документальную основу, называет проверенные "
            "страницы и уточняет редакционный ракурс книги. "
            "Третье предложение закрепляет вывод и показывает, почему книга попала в обзор."
        )


def _target(title: str = TARGET_TITLE, author: str = AUTHOR) -> DurableTarget:
    return DurableTarget(
        candidate_id="issue-487-devil-in-the-white-city",
        title=title,
        primary_authors=(author,),
        record={"candidate_id": "issue-487-devil-in-the-white-city", "title": title, "primary_authors": [author]},
    )


def _source(
    slug: str,
    *,
    title: str,
    authors: Sequence[str] = (AUTHOR,),
    subject: str = BOOK_MARKS_SUBJECT,
) -> FetchedSource:
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


def _fixture_results() -> tuple[DiscoveryResult, ...]:
    return (
        DiscoveryResult(
            url=BOOK_MARKS_URL,
            source_kind="review",
            source_family="review",
            owner_id="book-marks",
            source_language="en",
        ),
        DiscoveryResult(
            url=WIKIPEDIA_URL,
            source_kind="encyclopedia",
            source_family="encyclopedia",
            owner_id="wikipedia",
            source_language="en",
        ),
    )


def _candidate() -> dict[str, Any]:
    return {
        "candidate_id": "issue-487-devil-in-the-white-city",
        "identity_key": "the devil in the white city|erik larson",
        "title": TARGET_TITLE,
        "primary_authors": [AUTHOR],
        "update_delta": "new",
        "first_seen_date": CHECKED_DATE,
        "priority_label": "A",
    }


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (TARGET_TITLE, BOOK_MARKS_PAGE_TITLE, True),
        (BOOK_MARKS_PAGE_TITLE, TARGET_TITLE, True),
        ("The Devil in the White City", "The Devil in the Blue City", False),
        ("Salt", "Saltburn", False),
        ("Salt", "The Salt Path", False),
        ("Salt", "Salt", True),
    ],
)
def test_issue_487_grounding_title_comparison_is_bounded(left: str, right: str, expected: bool) -> None:
    assert grounding_titles_match(left, right) is expected


@pytest.mark.parametrize("source_title", [BOOK_MARKS_SHORT_TITLE, BOOK_MARKS_FULL_TITLE])
def test_issue_487_book_marks_title_forms_pass_the_grounding_gate(source_title: str) -> None:
    result = evaluate_grounding_gate(_target(), [_source("book-marks", title=source_title)])

    assert result.accepted


def test_issue_487_book_marks_and_wikipedia_devil_pair_passes_the_grounding_gate() -> None:
    result = evaluate_grounding_gate(
        _target(),
        [
            _source("book-marks", title=BOOK_MARKS_FULL_TITLE),
            _source("wikipedia", title=TARGET_TITLE, authors=(), subject=WIKIPEDIA_SUBJECT),
        ],
    )

    assert result.accepted
    assert len(result.accepted_sources) == 2


def test_issue_487_same_author_different_books_now_single_source() -> None:
    result = evaluate_grounding_gate(
        _target(),
        [
            _source("matching", title=TARGET_TITLE),
            _source("different", title="The Devil in the Blue City"),
        ],
    )

    # After #525, a different-book title mismatch no longer vetoes an
    # otherwise agreeing subset, so the matching source is now accepted as
    # single_source and the different-book source is recorded as
    # title_mismatch. The old expectation was refused with
    # source_identity_disagreement.
    assert result.accepted is True
    assert result.reason_code == ""
    assert tuple(source.canonical_url for source in result.accepted_sources) == ("https://matching.example.test/book",)
    assert any(
        item.canonical_url == "https://different.example.test/book" and item.reason_code == REASON_TITLE_MISMATCH
        for item in result.rejected_sources
    )


def test_issue_487_same_title_with_different_authors_is_refused() -> None:
    result = evaluate_grounding_gate(
        _target(),
        [
            _source("matching-author", title=TARGET_TITLE),
            _source("different-author", title=TARGET_TITLE, authors=("Robert Harris",)),
        ],
    )

    assert not result.accepted
    assert result.reason_code == REASON_SOURCE_IDENTITY_DISAGREEMENT


def test_issue_487_generic_one_word_title_does_not_match_longer_title() -> None:
    target = _target(title="Salt", author="Mark Kurlansky")
    result = evaluate_grounding_gate(
        target,
        [
            _source(
                "salt-path",
                title="The Salt Path",
                authors=("Mark Kurlansky",),
                subject="A walking memoir about a couple's journey along the South West Coast Path.",
            )
        ],
    )

    assert not result.accepted
    assert result.reason_code == REASON_TITLE_MISMATCH


def test_issue_487_authorless_source_still_needs_subject_author_evidence() -> None:
    result = evaluate_grounding_gate(
        _target(),
        [
            _source("authorless", title=TARGET_TITLE, authors=(), subject="A story about a historic fair."),
            _source("named", title=TARGET_TITLE, authors=(AUTHOR,)),
        ],
    )

    assert not result.accepted
    assert result.reason_code == REASON_SOURCE_IDENTITY_DISAGREEMENT


def _fetch(url: str, source_kind: str, owner_id: str) -> FetchedSource:
    result = DiscoveryResult(
        url=url,
        source_kind=source_kind,
        source_family=source_kind,
        owner_id=owner_id,
        source_language="en",
    )
    return BoundedHTTPFetcher(transport=FixtureTransport()).fetch(result, checked_date=CHECKED_DATE)


def test_issue_487_fetcher_parses_both_saved_page_shapes() -> None:
    book_marks = _fetch(BOOK_MARKS_URL, "review", "book-marks")
    wikipedia = _fetch(WIKIPEDIA_URL, "encyclopedia", "wikipedia")

    assert book_marks.title == BOOK_MARKS_PAGE_TITLE
    assert book_marks.primary_authors == (AUTHOR,)
    assert BOOK_MARKS_SUBJECT in book_marks.subject_text
    assert wikipedia.title == TARGET_TITLE
    assert wikipedia.primary_authors == ()
    assert "Erik Larson" in wikipedia.subject_text


def test_issue_487_real_two_source_fixture_pair_reaches_grounded_outcome() -> None:
    transport = FixtureTransport()
    stage = GroundedDescriptionStage(
        discovery=FixtureDiscovery(_fixture_results()),
        fetch=BoundedHTTPFetcher(transport=transport),
        synthesizer=RussianSynthesizer(),
    )

    artifact = stage.run(
        [_candidate()],
        operation_id="issue-487",
        operation_date=CHECKED_DATE,
        force_candidates=True,
    )
    outcome = artifact.outcomes[0]

    assert set(transport.requested_urls) == {BOOK_MARKS_URL, WIKIPEDIA_URL}
    assert outcome.outcome == OUTCOME_GROUNDED
    assert len(outcome.research_sources) == 2
