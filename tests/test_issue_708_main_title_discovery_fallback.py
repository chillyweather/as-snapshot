"""A changed subtitle must not make a findable book unfindable (issue #708).

Discovery quotes the whole title as one exact phrase. In the 2026-09-16 weekly
run the chart source carried the pre-publication subtitle of Kier Gaines's
*It All Starts When You Do*; the publisher had since renamed it, so the quoted
phrase matched nothing and the candidate ended with ``no_results``. The same
query with only the main title quoted returned the publisher page first.

The fallback is one extra query, only after an empty response, inside the same
discovery phase deadline and behind the same vendor floor.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import unquote_plus
from urllib.parse import urlsplit

import pytest

from autoscout.grounded_description import BoundedHTTPError
from autoscout.grounded_description import CandidateRequestBudget
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import HTTPDiscoveryProvider
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import _build_discovery_queries
from autoscout.grounded_description import committed_source_classifier
from autoscout.vendor_rate_limit import VendorRateLimiter

GAINES = {
    "candidate_id": "it all starts when you do|kier gaines",
    "identity_key": "it all starts when you do|kier gaines",
    "title": "It All Starts When You Do: Self-Healing for the Therapy Averse",
    "primary_authors": ["Kier Gaines"],
    "isbn": "978-0-316-58742-6",
}
FULL_QUERY = '"It All Starts When You Do: Self-Healing for the Therapy Averse" Kier Gaines book 9780316587426'
MAIN_QUERY = '"It All Starts When You Do" Kier Gaines book 9780316587426'
PUBLISHER_URL = "https://www.hachettebookgroup.com/titles/kier-gaines-lpc/it-all-starts-when-you-do/9780316587426/"


class QueryAwareTransport:
    def __init__(self, mapping: Mapping[str, Sequence[dict[str, Any]]], *, fail: set[str] | None = None) -> None:
        self.mapping = dict(mapping)
        self.fail = fail or set()
        self.requested_queries: list[str] = []

    def request(self, url: str, **_: object) -> HTTPResponse:
        query = unquote_plus(parse_qs(urlsplit(url).query).get("q", [""])[0])
        self.requested_queries.append(query)
        if query in self.fail:
            return HTTPResponse(url, 502, {}, b'{"error": "search vendor returned HTTP 429"}')
        body = json.dumps({"results": list(self.mapping.get(query, []))}).encode("utf-8")
        return HTTPResponse(url, 200, {}, body)


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


def _provider(transport: Any, rate_limiter: VendorRateLimiter | None = None) -> HTTPDiscoveryProvider:
    return HTTPDiscoveryProvider(
        "http://127.0.0.1:8899/search?q={query}",
        transport=transport,
        classifier=committed_source_classifier(),
        rate_limiter=rate_limiter,
    )


def _budget(clock: FakeClock, *, seconds: float = 10.0, max_discovery_requests: int = 2) -> CandidateRequestBudget:
    return CandidateRequestBudget(
        deadline=clock.value + seconds,
        max_fetches=0,
        monotonic=clock,
        max_discovery_requests=max_discovery_requests,
    )


def test_a_colon_subtitle_yields_the_full_query_then_the_main_title_query() -> None:
    assert _build_discovery_queries(GAINES) == [FULL_QUERY, MAIN_QUERY]


@pytest.mark.parametrize("separator", [":", "：", " - ", " – ", " — "])
def test_every_subtitle_separator_yields_the_main_title_query(separator: str) -> None:
    """Issue #708 names colon, hyphen, and em dash separators; review of #710 found only the colon handled."""
    title = f"It All Starts When You Do{separator}Self-Healing for the Therapy Averse"
    queries = _build_discovery_queries({**GAINES, "title": title})

    assert queries == [f'"{title}" Kier Gaines book 9780316587426', MAIN_QUERY]


def test_a_dash_subtitle_search_falls_back_to_the_main_title() -> None:
    title = "It All Starts When You Do — Self-Healing for the Therapy Averse"
    transport = QueryAwareTransport({MAIN_QUERY: [{"url": PUBLISHER_URL}]})
    clock = FakeClock()

    results = _provider(transport).discover({**GAINES, "title": title}, request_budget=_budget(clock))

    assert transport.requested_queries == [f'"{title}" Kier Gaines book 9780316587426', MAIN_QUERY]
    assert [result.url for result in results] == [PUBLISHER_URL]


def test_a_hyphen_inside_a_word_is_not_a_subtitle_separator() -> None:
    assert len(_build_discovery_queries({**GAINES, "title": "Self-Healing for the Therapy-Averse"})) == 1


@pytest.mark.parametrize(
    "title",
    [
        "The Ultimate Renovation Planner",  # no subtitle
        ": Leading Colon",  # nothing before the colon
        'He said "hi": a memoir',  # left unquoted, so there is no exact phrase to relax
        "Truncated Title: With a Subtitle…",  # a truncated title is already a recovered prefix
    ],
)
def test_no_fallback_query_without_a_usable_main_title(title: str) -> None:
    assert len(_build_discovery_queries({"title": title, "primary_authors": ["A Author"]})) == 1


def test_an_empty_full_title_search_falls_back_to_the_main_title() -> None:
    transport = QueryAwareTransport({MAIN_QUERY: [{"url": PUBLISHER_URL}]})
    clock = FakeClock()

    results = _provider(transport).discover(GAINES, request_budget=_budget(clock))

    assert transport.requested_queries == [FULL_QUERY, MAIN_QUERY]
    assert [result.url for result in results] == [PUBLISHER_URL]


def test_a_full_title_search_with_results_issues_no_second_query() -> None:
    transport = QueryAwareTransport({FULL_QUERY: [{"url": PUBLISHER_URL}], MAIN_QUERY: [{"url": "https://x.test/"}]})
    clock = FakeClock()

    results = _provider(transport).discover(GAINES, request_budget=_budget(clock))

    assert transport.requested_queries == [FULL_QUERY]
    assert [result.url for result in results] == [PUBLISHER_URL]


def test_both_queries_empty_is_still_no_results() -> None:
    transport = QueryAwareTransport({})
    clock = FakeClock()

    assert list(_provider(transport).discover(GAINES, request_budget=_budget(clock))) == []
    assert transport.requested_queries == [FULL_QUERY, MAIN_QUERY]


def test_a_single_request_budget_never_issues_the_fallback() -> None:
    transport = QueryAwareTransport({MAIN_QUERY: [{"url": PUBLISHER_URL}]})
    clock = FakeClock()

    results = _provider(transport).discover(GAINES, request_budget=_budget(clock, max_discovery_requests=1))

    assert list(results) == []
    assert transport.requested_queries == [FULL_QUERY]


def test_the_fallback_waits_for_the_vendor_floor_and_gives_up_when_it_cannot_fit() -> None:
    transport = QueryAwareTransport({MAIN_QUERY: [{"url": PUBLISHER_URL}]})
    clock = FakeClock()
    sleeps: list[float] = []

    def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        clock.value += seconds

    limiter = VendorRateLimiter(1.0, monotonic=clock, sleeper=sleeper)
    limiter.wait()  # the stage's own wait before the first query

    results = _provider(transport, limiter).discover(GAINES, request_budget=_budget(clock))
    assert sleeps == [1.0]
    assert [result.url for result in results] == [PUBLISHER_URL]

    # A discovery window shorter than the vendor floor cannot hold a second query.
    transport = QueryAwareTransport({MAIN_QUERY: [{"url": PUBLISHER_URL}]})
    limiter.wait()
    results = _provider(transport, limiter).discover(GAINES, request_budget=_budget(clock, seconds=0.5))
    assert list(results) == []
    assert transport.requested_queries == [FULL_QUERY]


def test_a_failed_fallback_request_raises_like_the_first_request() -> None:
    transport = QueryAwareTransport({}, fail={MAIN_QUERY})
    clock = FakeClock()

    with pytest.raises(BoundedHTTPError):
        _provider(transport).discover(GAINES, request_budget=_budget(clock))


def test_the_stage_gives_discovery_room_for_exactly_one_fallback() -> None:
    transport = QueryAwareTransport({MAIN_QUERY: [{"url": PUBLISHER_URL}]})
    stage = GroundedDescriptionStage(discovery=_provider(transport))

    stage.run(
        [{**GAINES, "first_seen_date": "2026-09-16"}],
        operation_id="op-708",
        operation_date="2026-09-16",
        force_candidates=True,
    )

    assert transport.requested_queries == [FULL_QUERY, MAIN_QUERY]
