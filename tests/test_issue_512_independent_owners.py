"""Offline regression coverage for issue #512 independent-owner admission.

Issue #512 admitted ``bookreporter.com`` with an editorial owner distinct
from the shared ``publisher-supplied-copy`` owner. Owners now come from the
owner policy (#687, #1123), so these tests check that owner against captured
markup rather than hand-written strings, and protect the design decision that
surfaces sharing ``publisher-supplied-copy`` must never be treated as an
independent pair. The path rules that kept author pages, people.com and
Books-A-Million out were retired with the policy file in #1123.
"""

from __future__ import annotations

from pathlib import Path

from autoscout.grounded_description import REASON_FEWER_THAN_TWO
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import DurableTarget
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import committed_source_classifier
from autoscout.grounded_description import evaluate_grounding_gate

FIXTURES = Path(__file__).parent / "fixtures" / "issue-512-independent-owners"
# Reused verbatim: the two same-owner Amazon captures from issue #502 prove
# that two publisher-supplied-copy surfaces still cannot form an independent
# pair, without hand-writing new markup for that regression.
AGREEING_SUBSET_FIXTURES = Path(__file__).parent / "fixtures" / "issue-502-agreeing-subset"

CHECKED_DATE = "2026-08-19"

BOOKREPORTER_URL = "https://www.bookreporter.com/reviews/the-devil-in-the-white-city"
AMAZON_URL = "https://www.amazon.com/Exhumed-Unearthing-History-American-Vampire/dp/B0G4P2VQ4P"
US_AMAZON_URL = "https://us.amazon.com/Exhumed-Unearthing-History-American-Vampire-ebook/dp/B0GFP7VDW9"
BODY_FILES: dict[str, Path] = {
    BOOKREPORTER_URL: FIXTURES / "bookreporter-devil-in-the-white-city.html",
    AMAZON_URL: AGREEING_SUBSET_FIXTURES / "amazon-exhumed.html",
    US_AMAZON_URL: AGREEING_SUBSET_FIXTURES / "us-amazon-exhumed-ebook.html",
}


class FixtureTransport:
    def __init__(self) -> None:
        self.requested_urls: list[str] = []

    def request(self, url: str, **_kwargs: object) -> HTTPResponse:
        self.requested_urls.append(url)
        body = BODY_FILES[url].read_bytes()
        return HTTPResponse(
            url=url,
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=body,
        )


def _classify(url: str) -> dict[str, str]:
    classifier = committed_source_classifier()
    return classifier.classify(url)


def _fetch(url: str) -> object:
    classification = _classify(url)
    discovery = DiscoveryResult(
        url=url,
        source_kind=str(classification.get("source_kind", "")),
        source_family=str(classification.get("source_family", "")),
        owner_id=str(classification.get("owner_id", "")),
        source_language="en",
        classification=str(classification["classification"]),
    )
    fetcher = BoundedHTTPFetcher(transport=FixtureTransport())
    return fetcher.fetch(discovery, checked_date=CHECKED_DATE)


def test_issue_512_bookreporter_classifies_with_distinct_owner() -> None:
    classification = _classify(BOOKREPORTER_URL)

    assert classification["classification"] == "classified"
    assert classification["owner_id"] == "bookreporter.com"
    assert classification["owner_id"] != "publisher-supplied-copy"

    fetched = _fetch(BOOKREPORTER_URL)
    assert fetched.owner_id == "bookreporter.com"
    # The captured markup carries the real book title, not a hand-written string.
    assert "White City" in fetched.title


def test_issue_512_new_owner_and_publisher_supplied_copy_are_distinct() -> None:
    bookreporter = _fetch(BOOKREPORTER_URL)
    amazon = _fetch(AMAZON_URL)

    assert amazon.owner_id == "publisher-supplied-copy"
    assert bookreporter.owner_id != amazon.owner_id
    assert len({bookreporter.owner_id, amazon.owner_id}) == 2


def test_issue_512_two_publisher_supplied_copy_surfaces_still_cannot_pair() -> None:
    """Protects the existing design: shared-owner surfaces never count as independent.

    Both fixtures come from the issue #502 captures and share the
    ``publisher-supplied-copy`` owner.  Even though the gate can still ground
    a single named source from them, it must never treat the pair itself as
    two independent sources.
    """
    amazon = _fetch(AMAZON_URL)
    us_amazon = _fetch(US_AMAZON_URL)
    assert amazon.owner_id == us_amazon.owner_id == "publisher-supplied-copy"

    target = DurableTarget(
        candidate_id="exhumed|aaron mahnke",
        title="Exhumed: Unearthing the History of the American Vampire",
        primary_authors=("Aaron Mahnke",),
        record={},
    )

    result = evaluate_grounding_gate(target, [amazon, us_amazon])

    # A same-owner pair can, at best, ground a single named source; it must
    # never be accepted as two independent sources.
    assert len(result.accepted_sources) == 1
    rejected_reasons = {item.reason_code for item in result.rejected_sources}
    assert rejected_reasons <= {REASON_FEWER_THAN_TWO}
