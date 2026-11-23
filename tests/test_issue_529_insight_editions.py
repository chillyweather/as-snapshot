"""Offline regression coverage for issue #529 Insight Editions.

The policy entry and its ``/products/`` route were retired with the policy
file in #1123; the owner policy makes the host its own owner.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from autoscout.grounded_description import REASON_FEWER_THAN_TWO
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import DurableTarget
from autoscout.grounded_description import FetchedSource
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import committed_source_classifier
from autoscout.grounded_description import evaluate_grounding_gate

FIXTURES = Path(__file__).parent / "fixtures" / "issue-529-insight-editions"
AGREEING_SUBSET_FIXTURES = Path(__file__).parent / "fixtures" / "issue-502-agreeing-subset"
CHECKED_DATE = "2026-08-20"

INSIGHT_EDITIONS_URL = "https://insighteditions.com/products/the-ultimate-renovation-planner-and-organizer"
AMAZON_URL = "https://www.amazon.com/Exhumed-Unearthing-History-American-Vampire/dp/B0G4P2VQ4P"
US_AMAZON_URL = "https://us.amazon.com/Exhumed-Unearthing-History-American-Vampire-ebook/dp/B0GFP7VDW9"

BODY_FILES: dict[str, Path] = {
    INSIGHT_EDITIONS_URL: FIXTURES / "insight-editions.html",
    AMAZON_URL: AGREEING_SUBSET_FIXTURES / "amazon-exhumed.html",
    US_AMAZON_URL: AGREEING_SUBSET_FIXTURES / "us-amazon-exhumed-ebook.html",
}


class FixtureTransport:
    def request(self, url: str, **_kwargs: object) -> HTTPResponse:
        return HTTPResponse(
            url=url,
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=BODY_FILES[url].read_bytes(),
        )


def _classification(url: str) -> dict[str, Any]:
    return committed_source_classifier().classify(url)


def _fetch(url: str) -> FetchedSource:
    classification = _classification(url)
    discovery = DiscoveryResult(
        url=url,
        source_kind=str(classification["source_kind"]),
        source_family=str(classification["source_family"]),
        owner_id=str(classification["owner_id"]),
        source_language="en",
        classification=str(classification["classification"]),
    )
    return BoundedHTTPFetcher(transport=FixtureTransport()).fetch(discovery, checked_date=CHECKED_DATE)


def test_issue_529_insight_editions_url_is_its_own_owner() -> None:
    classification = _classification(INSIGHT_EDITIONS_URL)

    assert classification["classification"] == "classified"
    assert classification["owner_id"] == "insighteditions.com"


def test_issue_529_insight_editions_capture_provides_usable_subject() -> None:
    source = _fetch(INSIGHT_EDITIONS_URL)

    assert len(source.subject_text) >= 100
    assert "Renovating can be expensive, complicated, and easy to get wrong." in source.subject_text


def test_issue_529_insight_editions_capture_passes_grounding_gate() -> None:
    source = _fetch(INSIGHT_EDITIONS_URL)
    target = DurableTarget(
        candidate_id="issue-529-ultimate-renovation-planner",
        title="The Ultimate Renovation Planner",
        primary_authors=("Jonathan Scott",),
        record={},
    )

    result = evaluate_grounding_gate(target, [source])

    assert result.accepted is True
    assert result.reason_code == ""
    assert len(result.accepted_sources) == 1
    assert len(result.rejected_sources) == 0


def test_issue_529_same_owner_pair_collapses_to_one_accepted_source() -> None:
    amazon = _fetch(AMAZON_URL)
    us_amazon = _fetch(US_AMAZON_URL)

    assert amazon.owner_id == us_amazon.owner_id == "publisher-supplied-copy"

    # The Amazon fixture names the author in its captured subject text, even
    # though its structured author extraction is empty.
    assert amazon.primary_authors == ()
    target_authors = ("Aaron Mahnke",)
    assert all(author in amazon.subject_text for author in target_authors)
    target = DurableTarget(
        candidate_id="issue-502-exhumed",
        title=amazon.title,
        primary_authors=target_authors,
        record={},
    )
    result = evaluate_grounding_gate(target, [amazon, us_amazon])

    assert result.accepted is True
    assert len(result.accepted_sources) == 1
    assert len(result.rejected_sources) == 1
    assert result.rejected_sources[0].reason_code == REASON_FEWER_THAN_TWO
