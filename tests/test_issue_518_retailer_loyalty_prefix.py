"""Regression coverage for issue #518 retailer loyalty prefix stripping.

A Barnes and Noble product page puts a membership offer before the publisher
description.  Issue #518 strips that prefix and keeps the description, so the
source carries real subject evidence instead of shop furniture.

The rule is deliberately narrow.  It needs two loyalty markers and a "Learn
More" control, so a page whose text merely mentions rewards is left unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import DurableTarget
from autoscout.grounded_description import FetchedSource
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import _subject_reason
from autoscout.grounded_description import _usable_subject_text
from autoscout.grounded_description import committed_source_classifier
from autoscout.grounded_description import evaluate_grounding_gate

FIXTURES = Path(__file__).parent / "fixtures" / "issue-502-agreeing-subset"
CHECKED_DATE = "2026-08-17"
TARGET_TITLE = "Exhumed: Unearthing the History of the American Vampire"
TARGET_AUTHOR = "Aaron Mahnke"

BARNES_NOBLE_URL = "https://www.barnesandnoble.com/w/exhumed-aaron-mahnke/1148854448"
BOOKSHOP_URL = (
    "https://bookshop.org/p/books/exhumed-unearthing-the-history-of-the-american-vampire-aaron-mahnke/c7e72bebb737534a"
)
HACHETTE_URL = "https://www.hachettebookgroup.com/titles/aaron-mahnke/exhumed/9798894141817/"

BODY_FILES: dict[str, str] = {
    BARNES_NOBLE_URL: "barnesandnoble-exhumed.html",
    BOOKSHOP_URL: "bookshop-exhumed.html",
    HACHETTE_URL: "hachette-exhumed.html",
}

PRODUCT_DESCRIPTION_OPENING = "An engrossing and macabre exploration of the folklore and early medical practices"


class FixtureTransport:
    def request(self, url: str, **_kwargs: object) -> HTTPResponse:
        body = (FIXTURES / BODY_FILES[url]).read_bytes()
        return HTTPResponse(
            url=url,
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=body,
        )


def _classify(url: str) -> dict[str, str]:
    classification = committed_source_classifier().classify(url)
    assert classification["classification"] == "classified"
    return {
        "source_kind": str(classification["source_kind"]),
        "source_family": str(classification["source_family"]),
        "owner_id": str(classification["owner_id"]),
    }


def _fetched_sources(urls: Sequence[str]) -> list[FetchedSource]:
    fetcher = BoundedHTTPFetcher(transport=FixtureTransport())
    return [
        fetcher.fetch(
            DiscoveryResult(
                url=url,
                source_kind=_classify(url)["source_kind"],
                source_family=_classify(url)["source_family"],
                owner_id=_classify(url)["owner_id"],
                source_language="en",
                classification="classified",
            ),
            checked_date=CHECKED_DATE,
        )
        for url in urls
    ]


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


def test_issue_518_barnes_and_noble_container_supersedes_loyalty_prefix_path() -> None:
    """The product container supplies the publisher description before the fallback."""
    source = _fetched_sources([BARNES_NOBLE_URL])[0]

    assert "Premium Members" not in source.subject_text
    assert "Learn More" not in source.subject_text
    # Issue #527: the product container supersedes the loyalty path for this host.
    assert source.subject_text.startswith(PRODUCT_DESCRIPTION_OPENING)
    assert _subject_reason(source) == ""


def test_issue_518_barnes_and_noble_container_description_still_grounds() -> None:
    """The container description is real subject evidence at the gate seam."""
    result = evaluate_grounding_gate(_target(), _fetched_sources([BARNES_NOBLE_URL]))

    assert result.accepted is True
    assert result.reason_code == ""


def test_issue_518_publisher_descriptions_are_untouched() -> None:
    """Bookshop and Hachette carry no loyalty prefix, so their text must not change."""
    bookshop, hachette = _fetched_sources([BOOKSHOP_URL, HACHETTE_URL])

    assert bookshop.subject_text.startswith("An engrossing and macabre exploration")
    assert hachette.subject_text.startswith("An engrossing and macabre exploration")


def test_issue_518_a_single_loyalty_marker_does_not_strip() -> None:
    """One marker is not enough, so a description mentioning rewards survives."""
    text = "Learn More about the rewards of slow reading in this study of attention and memory."

    assert _usable_subject_text(text) == text


def test_issue_518_loyalty_blurb_with_nothing_after_it_is_rejected() -> None:
    """A page that carries only the membership offer yields no subject evidence."""
    text = "Premium Members save an extra 10% and all Members collect stamps to save with Rewards. Learn More"

    assert _usable_subject_text(text) == ""


def test_issue_518_loyalty_markers_without_the_control_do_not_strip() -> None:
    """Both signals are required, so loyalty markers alone leave the text unchanged.

    The stripping needs the "Learn More" control to know where the prefix ends.
    Without it there is no boundary, and guessing one could cut a real
    description. This is the no-op half of the narrow rule.
    """
    text = "Premium Members save an extra 10% on every order this month, and the offer applies to all titles."

    assert _usable_subject_text(text) == text
