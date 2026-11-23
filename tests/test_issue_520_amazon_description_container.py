"""Offline regression coverage for issue #520 Amazon description container.

Amazon serves the real book description inside a container marked with the id
``bookDescription_feature_div``.  Detail-page subject containers used to be
matched by CSS class only, so the description was never read and the listing
chrome was stored as the subject evidence instead.  These tests read the
captured issue #502 Amazon fixtures and assert the stored subject is the book
description, bounded by the existing evidence limits.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from autoscout.grounded_description import MAX_EVIDENCE_METADATA_TEXT
from autoscout.grounded_description import MAX_SOURCE_SUBJECT_TEXT
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import FetchedSource
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import _PageEvidenceParser
from autoscout.grounded_description import committed_source_classifier

FIXTURES = Path(__file__).parent / "fixtures" / "issue-502-agreeing-subset"
CHECKED_DATE = "2026-08-17"

AMAZON_URL = "https://www.amazon.com/Exhumed-Unearthing-History-American-Vampire/dp/B0G4P2VQ4P"
US_AMAZON_URL = "https://us.amazon.com/Exhumed-Unearthing-History-American-Vampire-ebook/dp/B0GFP7VDW9"
BOOKSHOP_URL = (
    "https://bookshop.org/p/books/exhumed-unearthing-the-history-of-the-american-vampire-aaron-mahnke/c7e72bebb737534a"
)

BODY_FILES: dict[str, str] = {
    AMAZON_URL: "amazon-exhumed.html",
    US_AMAZON_URL: "us-amazon-exhumed-ebook.html",
    BOOKSHOP_URL: "bookshop-exhumed.html",
}

# The opening of the publisher description Amazon serves for the fixture book.
DESCRIPTION_OPENING = "An engrossing and macabre exploration of the folklore and early medical practices"
# The listing chrome the parser stored before issue #520.
CHROME_OPENING = "Amazon.com: Exhumed"


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


def _classification(url: str) -> Mapping[str, Any]:
    classifier = committed_source_classifier()
    classification = classifier.classify(url)
    assert classification["classification"] == "classified"
    return classification


def _fetched(url: str) -> FetchedSource:
    classification = _classification(url)
    result = DiscoveryResult(
        url=url,
        source_kind=str(classification["source_kind"]),
        source_family=str(classification["source_family"]),
        owner_id=str(classification["owner_id"]),
        source_language="en",
        classification="classified",
    )
    fetcher = BoundedHTTPFetcher(transport=FixtureTransport())
    return fetcher.fetch(result, checked_date=CHECKED_DATE)


def _detail_subject_parts(url: str) -> list[str]:
    parser = _PageEvidenceParser()
    parser.feed((FIXTURES / BODY_FILES[url]).read_bytes().decode("utf-8", "replace"))
    parser.finish()
    return parser.detail_subject_parts


def test_issue_520_amazon_listing_subject_is_the_book_description() -> None:
    source = _fetched(AMAZON_URL)

    assert source.subject_text.startswith(DESCRIPTION_OPENING)
    assert not source.subject_text.startswith(CHROME_OPENING)
    assert "Amazon.com" not in source.subject_text


def test_issue_520_us_amazon_ebook_subject_is_the_book_description() -> None:
    source = _fetched(US_AMAZON_URL)

    assert source.subject_text.startswith(DESCRIPTION_OPENING)
    assert "Kindle edition" not in source.subject_text


def test_issue_520_amazon_description_reaches_detail_subject_parts_once() -> None:
    """The container id appears once per fixture, so the text is not doubled."""
    for url in (AMAZON_URL, US_AMAZON_URL):
        collected = " ".join(_detail_subject_parts(url))
        assert collected.count(DESCRIPTION_OPENING) == 1


def test_issue_520_amazon_subject_stays_bounded() -> None:
    for url in (AMAZON_URL, US_AMAZON_URL):
        source = _fetched(url)
        assert len(source.subject_text) <= MAX_EVIDENCE_METADATA_TEXT
        assert len(source.display_text) <= MAX_SOURCE_SUBJECT_TEXT


def test_issue_520_structured_schema_org_still_wins_over_detail_container() -> None:
    """Bookshop carries schema.org/Book JSON-LD, and it must still be preferred."""
    source = _fetched(BOOKSHOP_URL)

    assert source.subject_text.startswith(DESCRIPTION_OPENING)


def test_issue_520_detail_subject_classes_still_match() -> None:
    """Both pre-existing class selectors must keep collecting their containers."""
    for class_name in ("bookmarks_a_review_pullquote", "book_manual_description"):
        parser = _PageEvidenceParser()
        parser.feed(f'<html><body><div class="{class_name}">Detail container prose.</div></body></html>')
        parser.finish()
        assert parser.detail_subject_parts == ["Detail container prose."]
