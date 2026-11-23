"""Regression coverage for issue #485 authorless source identity evidence."""

from __future__ import annotations

from collections.abc import Sequence

from autoscout.grounded_description import DurableTarget
from autoscout.grounded_description import FetchedSource
from autoscout.grounded_description import evaluate_grounding_gate

CHECKED_DATE = "2026-08-16"


def _source(
    slug: str,
    *,
    title: str,
    authors: Sequence[str],
    subject: str,
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


def test_issue_485_salt_accepts_only_the_author_page() -> None:
    target = DurableTarget("salt", "Salt", ("Mark Kurlansky",), {})
    result = evaluate_grounding_gate(
        target,
        [
            _source(
                "mineral",
                title="Salt",
                authors=(),
                subject=(
                    "Salt is a mineral composed primarily of sodium chloride used in food seasoning and preservation."
                ),
            ),
            _source(
                "kurlansky",
                title="Salt",
                authors=("Mark Kurlansky",),
                subject=(
                    "Kurlansky traces the world history of salt from ancient China through the Erie Canal and the Gandhi salt march."
                ),
            ),
        ],
    )

    # #866 excludes mineral text from synthesis instead of vetoing the book.
    assert result.accepted
    assert result.reason_code == ""
    assert tuple(source.canonical_url for source in result.accepted_sources) == ("https://kurlansky.example.test/book",)
    assert "https://mineral.example.test/book" not in {source.canonical_url for source in result.accepted_sources}


def test_issue_485_authorless_subject_naming_target_author_is_accepted() -> None:
    target = DurableTarget("salt", "Salt", ("Mark Kurlansky",), {})
    result = evaluate_grounding_gate(
        target,
        [
            _source(
                "kurlansky-subject",
                title="Salt",
                authors=(),
                subject="Kurlansky traces the world history of salt.",
            ),
            _source(
                "kurlansky-metadata",
                title="Salt",
                authors=("Mark Kurlansky",),
                subject="Mark Kurlansky traces the world history of salt.",
            ),
        ],
    )

    assert result.accepted


def test_issue_485_authorless_subject_naming_different_author_is_not_used() -> None:
    target = DurableTarget("salt", "Salt", ("Mark Kurlansky",), {})
    result = evaluate_grounding_gate(
        target,
        [
            _source(
                "wrong-subject-author",
                title="Salt",
                authors=(),
                subject="Robert Harris writes about the history of salt.",
            ),
            _source(
                "kurlansky",
                title="Salt",
                authors=("Mark Kurlansky",),
                subject="Kurlansky traces the world history of salt.",
            ),
        ],
    )

    # #866: a name in prose is not an authorship claim; the page stays unused.
    assert result.accepted
    assert tuple(source.canonical_url for source in result.accepted_sources) == ("https://kurlansky.example.test/book",)


def test_issue_485_surname_boundary_does_not_match_inside_sandstone_page() -> None:
    target = DurableTarget("londres", "38 Londres Street", ("Philippe Sands",), {})
    result = evaluate_grounding_gate(
        target,
        [
            _source(
                "sandstone",
                title="38 Londres Street",
                authors=(),
                subject="The book is by Philippe Sandstone.",
            ),
            _source(
                "sands",
                title="38 Londres Street",
                authors=("Philippe Sands",),
                subject="Philippe Sands examines international law.",
            ),
        ],
    )

    # Sandstone is not Sands evidence, so that page never joins the accepted set (#866).
    assert result.accepted
    assert tuple(source.canonical_url for source in result.accepted_sources) == ("https://sands.example.test/book",)
