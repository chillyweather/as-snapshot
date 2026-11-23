"""Offline regression coverage for issue #527 Barnes and Noble description extraction.

The captured product page carries the publisher description in a
``product-description`` class container.
The truncated meta description does not name Aaron Mahnke.
"""

from __future__ import annotations

from pathlib import Path

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
BARNES_NOBLE_URL = "https://www.barnesandnoble.com/w/exhumed-aaron-mahnke/1148854448"
DESCRIPTION_OPENING = "An engrossing and macabre exploration of the folklore and early medical practices"


class FixtureTransport:
    def request(self, url: str, **_kwargs: object) -> HTTPResponse:
        return HTTPResponse(
            url=url,
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=(FIXTURES / "barnesandnoble-exhumed.html").read_bytes(),
        )


def _discovery() -> DiscoveryResult:
    classification = committed_source_classifier().classify(BARNES_NOBLE_URL)
    assert classification["classification"] == "classified"
    return DiscoveryResult(
        url=BARNES_NOBLE_URL,
        source_kind=str(classification["source_kind"]),
        source_family=str(classification["source_family"]),
        owner_id=str(classification["owner_id"]),
        source_language="en",
        classification="classified",
    )


def _fetched() -> FetchedSource:
    fetcher = BoundedHTTPFetcher(transport=FixtureTransport())
    return fetcher.fetch(_discovery(), checked_date=CHECKED_DATE)


def _detail_subject_text() -> str:
    parser = _PageEvidenceParser()
    parser.feed((FIXTURES / "barnesandnoble-exhumed.html").read_bytes().decode("utf-8", "replace"))
    parser.finish()
    return " ".join(parser.detail_subject_parts)


def test_issue_527_barnes_and_noble_subject_uses_product_description_container() -> None:
    source = _fetched()
    detail_subject = _detail_subject_text()

    assert source.subject_text.startswith(DESCRIPTION_OPENING)
    assert "Aaron Mahnke" in source.subject_text
    assert "Premium Members" not in source.subject_text
    assert len(source.subject_text) <= MAX_EVIDENCE_METADATA_TEXT
    assert len(source.display_text) <= MAX_SOURCE_SUBJECT_TEXT
    assert detail_subject.startswith(DESCRIPTION_OPENING)
    assert "Aaron Mahnke" in detail_subject
    assert "Premium Members" not in detail_subject
