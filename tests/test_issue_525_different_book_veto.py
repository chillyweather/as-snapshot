"""Reproduction for issue #525 - different-book source veto."""

from __future__ import annotations

from autoscout.grounded_description import REASON_TITLE_MISMATCH
from autoscout.grounded_description import DurableTarget
from autoscout.grounded_description import FetchedSource
from autoscout.grounded_description import evaluate_grounding_gate


def _source(slug: str, *, title: str, subject: str, authors: tuple[str, ...] = ()) -> FetchedSource:
    return FetchedSource(
        url=f"https://{slug}.example.test/book",
        source_kind="review",
        source_family="book-review",
        owner_id=f"owner-{slug}",
        source_language="en",
        title=title,
        primary_authors=authors,
        subject_text=subject,
        subject_key=subject,
        checked_date="2026-08-17",
    )


def _target_you_do_you() -> DurableTarget:
    return DurableTarget(
        "you do you|serena wolf",
        "You Do You: 100+ Endlessly Adaptable Recipes to Boost Your Culinary Confidence",
        ("Serena Wolf",),
        {},
    )


def test_issue_525_different_book_source_does_not_veto_agreeing_subset() -> None:
    """A different-book source must not veto an otherwise agreeing single source."""
    target = _target_you_do_you()
    good = _source(
        "good",
        title="You Do You: 100+ Endlessly Adaptable Recipes to Boost Your Culinary Confidence",
        subject="Serena Wolf shares 100 adaptable recipes for culinary confidence in You Do You with publisher-supplied copy.",
        authors=("Serena Wolf",),
    )
    dude = _source(
        "dude",
        title="The Dude Diet",
        subject="The Dude Diet shares clean eating recipes for culinary confidence with adaptable tips.",
    )
    result = evaluate_grounding_gate(target, [good, dude])

    assert result.accepted is True
    assert result.reason_code == ""
    assert len(result.accepted_sources) == 1
    assert result.accepted_sources[0].canonical_url == "https://good.example.test/book"
    rejected = {item.canonical_url: item.reason_code for item in result.rejected_sources}
    assert rejected["https://dude.example.test/book"] == REASON_TITLE_MISMATCH


def test_issue_525_only_different_book_sources_still_refuse() -> None:
    """A candidate whose only sources are a different book must still be refused."""
    target = _target_you_do_you()
    s1 = _source(
        "other1",
        title="The Dude Diet",
        subject="The Dude Diet is about clean eating.",
    )
    s2 = _source(
        "other2",
        title="Another Book",
        subject="Another book is about something else.",
    )
    result = evaluate_grounding_gate(target, [s1, s2])

    assert not result.accepted
    assert result.reason_code == REASON_TITLE_MISMATCH
