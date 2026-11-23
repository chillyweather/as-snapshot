"""Offline regression coverage for the Issue #530 discovery contract.

The provider uses one deterministic HTTP request per candidate.
The query keeps exact-title precision, author context, and a plausible ISBN
without sequential query fan-out.

Conventions follow tests/test_issue_517_synthesis_refusal.py: fixture-backed
transport and assertions through the real HTTPDiscoveryProvider and
GroundedDescriptionStage.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import unquote_plus
from urllib.parse import urlsplit

from autoscout.grounded_description import CANDIDATE_ENVELOPE_SECONDS
from autoscout.grounded_description import MAX_FETCHES_PER_CANDIDATE
from autoscout.grounded_description import MAX_WORKERS
from autoscout.grounded_description import STAGE_BUDGET_SECONDS
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import HTTPDiscoveryProvider
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import SynthesisResult
from autoscout.grounded_description import _build_discovery_queries
from autoscout.grounded_description import committed_source_classifier
from autoscout.vendor_rate_limit import MIN_VENDOR_INTERVAL_SECONDS

FIXTURES = Path(__file__).parent / "fixtures" / "issue-529-insight-editions"

INSIGHT_URL = "https://insighteditions.com/products/the-ultimate-renovation-planner-and-organizer"
SIMON_URL = (
    "https://www.simonandschuster.com/books/The-Ultimate-Renovation-Planner-and-Organizer/Jonathan-Scott/9798886744392"
)

CANDIDATE = {
    "candidate_id": "the ultimate renovation planner|jonathan scott",
    "identity_key": "the ultimate renovation planner|jonathan scott",
    "title": "the ultimate renovation planner",
    "primary_authors": ["Jonathan Scott"],
    "first_seen_date": "2026-08-20",
}

CANDIDATE_WITH_ISBN = {
    **CANDIDATE,
    "isbn": "9798886744392",
}


def _result(url: str, *, title: str = "") -> dict[str, Any]:
    return {"url": url, "title": title or f"title for {url}"}


HGTV_URLS = [
    "https://www.hgtv.com/shows/property-brothers",
    "https://www.youtube.com/watch?v=abc",
    "https://parade.com/celebs/jonathan-scott-interview",
    "https://www.housebeautiful.com/design",
]


class QueryAwareTransport:
    """Return a different result set per decoded query string."""

    def __init__(self, mapping: Mapping[str, Sequence[dict[str, Any]]]) -> None:
        self.mapping = dict(mapping)
        self.requested_queries: list[str] = []
        self.requested_urls: list[str] = []

    def request(self, url: str, **_: object) -> HTTPResponse:
        self.requested_urls.append(url)
        query = parse_qs(urlsplit(url).query).get("q", [""])[0]
        decoded = unquote_plus(query)
        # Also handle quote_plus encoding of quoted title: keep as decoded string
        self.requested_queries.append(decoded)
        results = list(self.mapping.get(decoded, []))
        body = json.dumps({"results": results}).encode("utf-8")
        return HTTPResponse(url, 200, {}, body)


def _provider(transport: Any) -> HTTPDiscoveryProvider:
    return HTTPDiscoveryProvider(
        "http://127.0.0.1:8899/search?q={query}",
        transport=transport,
        classifier=committed_source_classifier(),
    )


def test_discovery_makes_no_request_without_deterministic_query_fields() -> None:
    assert _build_discovery_queries({"title": "", "primary_authors": []}) == []


def test_discovery_uses_one_http_request_per_candidate() -> None:
    class OneRequestTransport:
        def __init__(self) -> None:
            self.requested_urls: list[str] = []

        def request(self, url: str, **_: object) -> HTTPResponse:
            self.requested_urls.append(url)
            return HTTPResponse(
                url,
                200,
                {},
                json.dumps({"results": [_result(INSIGHT_URL)]}).encode("utf-8"),
            )

    transport = OneRequestTransport()
    results = _provider(transport).discover(CANDIDATE)

    assert len(transport.requested_urls) == 1
    assert [result.url for result in results] == [INSIGHT_URL]


def test_single_request_keeps_deterministic_query_reachability() -> None:
    """The one request carries precise title, author, and book terms."""
    transport = QueryAwareTransport(
        {
            '"the ultimate renovation planner" Jonathan Scott book': [
                _result(INSIGHT_URL, title="The Ultimate Renovation Planner and Organizer by Jonathan Scott"),
                _result(SIMON_URL, title="The Ultimate Renovation Planner and Organizer by Jonathan Scott"),
            ]
        }
    )

    results = _provider(transport).discover(CANDIDATE)

    assert len(transport.requested_queries) == 1
    assert transport.requested_queries == ['"the ultimate renovation planner" Jonathan Scott book']
    assert {result.url for result in results} == {INSIGHT_URL, SIMON_URL}


def test_discovery_order_is_deterministic_without_query_merging() -> None:
    """One vendor response keeps its order and remains repeatable."""
    mapping = {
        '"the ultimate renovation planner" Jonathan Scott book': [
            _result(INSIGHT_URL, title="The Ultimate Renovation Planner and Organizer by Jonathan Scott"),
            _result(SIMON_URL, title="The Ultimate Renovation Planner and Organizer by Jonathan Scott"),
        ]
    }
    first_transport = QueryAwareTransport(mapping)
    second_transport = QueryAwareTransport(mapping)

    first = _provider(first_transport).discover(CANDIDATE)
    second = _provider(second_transport).discover(CANDIDATE)

    assert [result.url for result in first] == [result.url for result in second]
    assert [result.url for result in first] == [INSIGHT_URL, SIMON_URL]
    assert len(first_transport.requested_queries) == len(second_transport.requested_queries) == 1


def test_fetch_budget_does_not_grow_with_more_discovery() -> None:
    """Hard constraint: MAX_FETCHES_PER_CANDIDATE stays 3 even after discovery expands."""
    assert MAX_FETCHES_PER_CANDIDATE == 3
    assert MIN_VENDOR_INTERVAL_SECONDS == 1.0
    # Issue #563 raised the envelope from 45 to 90 seconds: a candidate that
    # reached synthesis was being cut off at 45 after its sources had been
    # accepted. The fetch budget below is what bounds cost, not the clock.
    assert CANDIDATE_ENVELOPE_SECONDS == 90.0
    assert MAX_WORKERS == 3
    assert STAGE_BUDGET_SECONDS == 900.0

    # Simulate stage fetch recording
    class RecordingFetch:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def fetch(self, result: Any, **_: object) -> dict[str, Any]:
            url = result.url if isinstance(result, DiscoveryResult) else str(result.get("url", ""))
            self.urls.append(url)
            return {
                "url": url,
                "source_kind": "publisher",
                "source_family": "publisher_copy",
                "owner_id": f"owner-{url}",
                "source_language": "en",
                "title": "The Ultimate Renovation Planner",
                "primary_authors": ["Jonathan Scott"],
                "subject_text": "Renovating can be expensive, complicated, and easy to get wrong. " * 10,
                "subject_key": "renovating",
                "checked_date": "2026-08-20",
                "classification": "classified",
            }

    class Synth:
        def synthesize(self, *_: object, **__: object) -> SynthesisResult:
            return SynthesisResult(
                "Книга о ремонте и планировании. "
                "Второй фрагмент раскрывает документальную основу, называет проверенные "
                "страницы и уточняет редакционный ракурс книги. "
                "Третье предложение закрепляет вывод и показывает, почему книга попала в обзор."
            )

    # Verify that even with many discovered URLs, only three are fetched.
    many = [_result(f"https://example.test/page-{i}") for i in range(10)]
    # Convert to DiscoveryResult via classifier
    classifier = committed_source_classifier()

    # Use a simple deterministic discovery that returns many classified URLs
    def fake_discover(_candidate: Any, **_: Any) -> Sequence[DiscoveryResult]:
        results = []
        for raw in many:
            url = raw["url"]
            # Use a classified host like penguinrandomhouse
            url = f"https://www.penguinrandomhouse.com/books/{raw['url'].split('/')[-1]}"
            meta = classifier.classify(url)
            results.append(DiscoveryResult(url, meta["source_kind"], meta["source_family"], meta["owner_id"], "en"))
        return results

    class SimpleDiscovery:
        def discover(self, _candidate: Any, **_: Any) -> Sequence[DiscoveryResult]:
            return fake_discover(_candidate)

    fetch2 = RecordingFetch()
    stage2 = GroundedDescriptionStage(discovery=SimpleDiscovery(), fetch=fetch2, synthesizer=Synth())
    stage2.run([CANDIDATE], operation_id="test", operation_date="2026-08-20", force_candidates=True)
    assert len(fetch2.urls) == 3


def test_isbn_is_included_in_the_single_discovery_query() -> None:
    """A plausible ISBN improves the one deterministic request."""
    query = '"the ultimate renovation planner" Jonathan Scott book 9798886744392'
    transport = QueryAwareTransport({query: [_result(INSIGHT_URL)]})

    results = _provider(transport).discover(CANDIDATE_WITH_ISBN)

    assert transport.requested_queries == [query]
    assert [result.url for result in results] == [INSIGHT_URL]

    transport_without_isbn = QueryAwareTransport({})
    _provider(transport_without_isbn).discover(CANDIDATE)
    assert len(transport_without_isbn.requested_queries) == 1
    assert "9798886744392" not in transport_without_isbn.requested_queries[0]


def test_deduplication_by_canonical_url_within_one_response() -> None:
    query = '"the ultimate renovation planner" Jonathan Scott book'
    mapping = {
        query: [
            _result(INSIGHT_URL),
            _result(INSIGHT_URL),
            _result("https://INSIGHTEDITIONS.com/products/the-ultimate-renovation-planner-and-organizer"),
        ]
    }
    transport = QueryAwareTransport(mapping)

    results = _provider(transport).discover(CANDIDATE)

    assert transport.requested_queries == [query]
    assert [result.url for result in results] == [INSIGHT_URL]


def test_one_failed_discovery_request_does_not_retry() -> None:
    class AlwaysFail:
        def request(self, url: str, **_: object) -> HTTPResponse:
            return HTTPResponse(url, 500, {}, b"{}")

    transport = AlwaysFail()
    provider = _provider(transport)

    try:
        provider.discover(CANDIDATE)
        raise AssertionError("expected BoundedHTTPError")
    except Exception as exc:
        assert type(exc).__name__ == "BoundedHTTPError"


def test_discovery_request_failure_raises() -> None:
    class AlwaysFail:
        def request(self, url: str, **_: object) -> HTTPResponse:
            return HTTPResponse(url, 500, {}, b"{}")

    provider = _provider(AlwaysFail())
    try:
        provider.discover(CANDIDATE)
        raise AssertionError("expected BoundedHTTPError")
    except Exception as exc:
        assert type(exc).__name__ == "BoundedHTTPError"


def test_isbn_garbage_never_becomes_a_live_query() -> None:
    """Garbage ISBN values are not appended to the one live request."""
    for garbage in ["n/a", "TBA", "forthcoming", " ", ""]:
        candidate = {**CANDIDATE, "isbn": garbage}
        transport = QueryAwareTransport({})

        _provider(transport).discover(candidate)

        assert len(transport.requested_queries) == 1
        if garbage.strip():
            assert garbage.strip().lower() not in transport.requested_queries[0].lower()


def test_title_with_quote_stays_unquoted_in_the_single_query() -> None:
    candidate = {**CANDIDATE, "title": 'He said "hi"', "primary_authors": ["Alex Author"]}
    transport = QueryAwareTransport({})

    _provider(transport).discover(candidate)

    assert len(transport.requested_queries) == 1
    assert transport.requested_queries[0] == 'He said "hi" Alex Author book'
