"""Regression coverage for Issue #617 truncated Candidate History titles.

The provider recovers bounded stored titles only for discovery and grounding
comparison at read time.
Historical backfill is out of scope because the original full title cannot be
reconstructed safely from bounded stored metadata.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import unquote_plus
from urllib.parse import urlsplit

import pytest

from autoscout.grounded_description import DurableTarget
from autoscout.grounded_description import FetchedSource
from autoscout.grounded_description import HTTPDiscoveryProvider
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import committed_source_classifier
from autoscout.grounded_description import evaluate_grounding_gate

AUTHOR = "Tom Rosenstiel"
RESULT_URL = "https://publisher.example.test/books/the-next-journalism"
COMPLETE_TITLE = "The Next Journalism: How the Press Must Change"


class QuerySensitiveTransport:
    """Return an in-policy page only for the expected recovered query."""

    def __init__(self, expected_query: str) -> None:
        self.expected_query = expected_query
        self.requested_queries: list[str] = []

    def request(self, url: str, **_: Any) -> HTTPResponse:
        query = unquote_plus(parse_qs(urlsplit(url).query).get("q", [""])[0])
        self.requested_queries.append(query)
        results = [{"url": RESULT_URL}] if query == self.expected_query else []
        return HTTPResponse(url, 200, {}, json.dumps({"results": results}).encode("utf-8"))


def _provider(transport: QuerySensitiveTransport) -> HTTPDiscoveryProvider:
    return HTTPDiscoveryProvider(
        "http://127.0.0.1:8899/search?q={query}",
        transport=transport,
        classifier=committed_source_classifier(),
    )


@pytest.mark.parametrize(
    "title",
    [
        "The Next Journalism: How the Press Must Change t…",
        "The Next Journalism: How the Press Must Change t...",
    ],
)
def test_truncated_colon_title_uses_one_recovered_intact_prefix_query(title: str) -> None:
    expected_query = '"The Next Journalism: How the Press Must Change" Tom Rosenstiel book'
    transport = QuerySensitiveTransport(expected_query)
    candidate = {"title": title, "primary_authors": [AUTHOR]}

    results = _provider(transport).discover(candidate)

    assert transport.requested_queries == [expected_query]
    assert [result.url for result in results] == [RESULT_URL]
    assert results[0].classification == "classified"
    assert candidate["title"] == title


@pytest.mark.parametrize(
    "title",
    [
        "The Future of Journalism t…",
        "The Future of Journalism t...",
    ],
)
def test_truncated_title_without_subtitle_delimiter_drops_attached_cut_word(title: str) -> None:
    expected_query = '"The Future of Journalism" A B book'
    transport = QuerySensitiveTransport(expected_query)

    results = _provider(transport).discover({"title": title, "primary_authors": ["A B"]})

    assert transport.requested_queries == [expected_query]
    assert [result.url for result in results] == [RESULT_URL]


@pytest.mark.parametrize(
    ("title", "expected_title"),
    [
        ("Nexus: A Brief History of Information Networks…", "Nexus: A Brief History of Information Networks"),
        ("Nexus: A Brief History of Information Networks...", "Nexus: A Brief History of Information Networks"),
        ("The Wager…", "The Wager"),
    ],
)
def test_truncated_title_preserves_useful_intact_text(title: str, expected_title: str) -> None:
    expected_query = f'"{expected_title}" {AUTHOR} book'
    transport = QuerySensitiveTransport(expected_query)

    results = _provider(transport).discover({"title": title, "primary_authors": [AUTHOR]})

    assert transport.requested_queries == [expected_query]
    assert [result.url for result in results] == [RESULT_URL]


def test_complete_colon_title_keeps_exact_quoted_phrase() -> None:
    expected_query = f'"{COMPLETE_TITLE}" {AUTHOR} book'
    transport = QuerySensitiveTransport(expected_query)

    results = _provider(transport).discover({"title": COMPLETE_TITLE, "primary_authors": [AUTHOR]})

    assert transport.requested_queries == [expected_query]
    assert [result.url for result in results] == [RESULT_URL]


@pytest.mark.parametrize("title", ["AI", "It", "Salt"])
def test_complete_short_titles_keep_exact_quoted_terms(title: str) -> None:
    expected_query = f'"{title}" {AUTHOR} book'
    transport = QuerySensitiveTransport(expected_query)

    results = _provider(transport).discover({"title": title, "primary_authors": [AUTHOR]})

    assert transport.requested_queries == [expected_query]
    assert [result.url for result in results] == [RESULT_URL]


@pytest.mark.parametrize("title", ["AI", "It", "Salt"])
def test_complete_short_titles_exact_match_at_grounding_gate(title: str) -> None:
    target = DurableTarget("short-title", title, (AUTHOR,), {"title": title})
    result = evaluate_grounding_gate(
        target,
        [
            _fetched_source(
                "https://one.example.test/short-title",
                title,
                owner="one",
                family="one",
                subject="journalism history",
            )
        ],
    )

    assert result.accepted
    assert all(item.reason_code != "title_mismatch" for item in result.rejected_sources)


def test_complete_fullwidth_colon_title_stays_unchanged_in_query() -> None:
    title = "情報の未来：石器時代からAIまでの歴史"
    expected_query = f'"{title}" A B book'
    transport = QuerySensitiveTransport(expected_query)

    results = _provider(transport).discover({"title": title, "primary_authors": ["A B"]})

    assert transport.requested_queries == [expected_query]
    assert [result.url for result in results] == [RESULT_URL]


def test_complete_fullwidth_colon_title_keeps_grounding_behavior() -> None:
    title = "情報の未来：石器時代からAIまでの歴史"
    target = DurableTarget("information-future", title, (AUTHOR,), {"title": title})
    result = evaluate_grounding_gate(
        target,
        [
            _fetched_source(
                "https://one.example.test/information-future",
                title,
                owner="one",
                family="one",
                subject="information technology history",
            )
        ],
    )

    assert result.accepted


@pytest.mark.parametrize("title", ["Wor…", "…", "of…"])
def test_weak_recovered_title_is_omitted_and_identifiers_remain(title: str) -> None:
    expected_query = f"{AUTHOR} book 9780306406157"
    transport = QuerySensitiveTransport(expected_query)

    results = _provider(transport).discover({"title": title, "primary_authors": [AUTHOR], "isbn": "978-0-306-40615-7"})

    assert transport.requested_queries == [expected_query]
    assert [result.url for result in results] == [RESULT_URL]


def test_truncated_fullwidth_colon_title_preserves_recovered_query_delimiter() -> None:
    expected_query = '"情報の未来：石器時代からAIまでの歴史" A B book'
    transport = QuerySensitiveTransport(expected_query)

    results = _provider(transport).discover(
        {"title": "情報の未来：石器時代からAIまでの歴史…", "primary_authors": ["A B"]}
    )

    assert transport.requested_queries == [expected_query]
    assert [result.url for result in results] == [RESULT_URL]


def test_single_token_non_ascii_truncated_title_is_not_dropped() -> None:
    expected_query = '"猫" A B book'
    transport = QuerySensitiveTransport(expected_query)

    results = _provider(transport).discover({"title": "猫…", "primary_authors": ["A B"]})

    assert transport.requested_queries == [expected_query]
    assert [result.url for result in results] == [RESULT_URL]


def _fetched_source(url: str, title: str, *, owner: str, family: str, subject: str) -> FetchedSource:
    return FetchedSource(
        url=url,
        source_kind="publisher",
        source_family=family,
        owner_id=owner,
        source_language="en",
        title=title,
        primary_authors=(AUTHOR,),
        subject_text=subject,
        subject_key=subject,
        checked_date="2026-09-01",
    )


def test_grounding_gate_recovers_no_colon_title_for_two_fetched_sources() -> None:
    title = "The Future of Journalism t…"
    target = DurableTarget("future", title, (AUTHOR,), {"title": title})
    sources = [
        _fetched_source(
            "https://one.example.test/future",
            "The Future of Journalism in the Age of AI",
            owner="one",
            family="one",
            subject="journalism history",
        ),
        _fetched_source(
            "https://two.example.test/future",
            "The Future of Journalism in the Age of AI",
            owner="two",
            family="two",
            subject="journalism history",
        ),
    ]

    result = evaluate_grounding_gate(target, sources)

    assert result.accepted
    assert all(item.reason_code != "title_mismatch" for item in result.rejected_sources)


def test_grounding_gate_normalizes_fullwidth_colon_before_title_comparison() -> None:
    title = "情報の未来：石器時代からAIまでの歴史…"
    target = DurableTarget("information-future", title, (AUTHOR,), {"title": title})
    sources = [
        _fetched_source(
            "https://one.example.test/information-future",
            "情報の未来",
            owner="one",
            family="one",
            subject="information technology history",
        ),
        _fetched_source(
            "https://two.example.test/information-future",
            "情報の未来",
            owner="two",
            family="two",
            subject="information technology history",
        ),
    ]

    result = evaluate_grounding_gate(target, sources)

    assert result.accepted
    assert all(item.reason_code != "title_mismatch" for item in result.rejected_sources)
