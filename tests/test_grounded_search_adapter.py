"""Tests for the loopback grounded search adapter (#457, #468).

The adapter exists because ``HTTPDiscoveryProvider`` performs one bounded GET
with no custom header and reads ``{"results": [{"url": ...}]}``.  These tests
pin both halves of that contract and the credential handling.

Nothing here reaches the network.  The vendor call is always injected.
"""

from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from http.server import HTTPServer
from typing import Any

import pytest

from autoscout.grounded_description import HTTPDiscoveryProvider
from autoscout.grounded_description import committed_source_classifier
from autoscout.grounded_search_adapter import API_KEY_ENV
from autoscout.grounded_search_adapter import MAX_RESULTS
from autoscout.grounded_search_adapter import SearchAdapterError
from autoscout.grounded_search_adapter import _Handler
from autoscout.grounded_search_adapter import brave_search
from autoscout.grounded_search_adapter import serve
from autoscout.grounded_search_adapter import to_autoscout_shape
from autoscout.vendor_rate_limit import VendorRateLimiter

SECRET = "brave-key-must-never-appear"


def _brave_payload(urls: list[str]) -> dict[str, Any]:
    """A Brave-shaped body: results nested under ``web``."""
    return {"web": {"results": [{"url": url, "title": "T", "description": "D"} for url in urls]}}


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, size: int) -> bytes:
        return self._body[:size]

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def test_vendor_shape_is_translated_into_the_shape_the_provider_reads(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.headers)
        return _Response(json.dumps(_brave_payload(["https://a.test/1"])).encode("utf-8"))

    monkeypatch.setattr("autoscout.grounded_search_adapter.urlopen", fake_urlopen)

    shaped = to_autoscout_shape(brave_search("All That's Unseen", api_key=SECRET))

    assert list(shaped) == ["results"]
    assert shaped["results"][0]["url"] == "https://a.test/1"
    # The credential travels in the header the vendor requires, never the URL.
    assert SECRET not in captured["url"]
    assert captured["headers"]["X-subscription-token"] == SECRET


def test_a_non_http_url_and_an_empty_url_are_dropped() -> None:
    shaped = to_autoscout_shape(
        [
            {"url": "https://good.test/1"},
            {"url": "javascript:alert(1)"},
            {"url": "file:///etc/passwd"},
            {"url": ""},
            {"url": "   "},
        ]
    )

    assert [result["url"] for result in shaped["results"]] == ["https://good.test/1"]


def test_result_count_is_bounded() -> None:
    shaped = to_autoscout_shape([{"url": f"https://a.test/{index}"} for index in range(MAX_RESULTS + 5)])

    assert len(shaped["results"]) == MAX_RESULTS


def test_a_body_over_the_ceiling_is_refused_without_naming_the_key(monkeypatch) -> None:
    oversized = b"x" * (1_000_000 + 10)
    monkeypatch.setattr(
        "autoscout.grounded_search_adapter.urlopen",
        lambda request, timeout: _Response(oversized),
    )

    with pytest.raises(SearchAdapterError) as excinfo:
        brave_search("query", api_key=SECRET)

    assert "ceiling" in str(excinfo.value)
    assert SECRET not in str(excinfo.value)


def test_a_non_json_body_is_refused_without_naming_the_key(monkeypatch) -> None:
    monkeypatch.setattr(
        "autoscout.grounded_search_adapter.urlopen",
        lambda request, timeout: _Response(b"<html>not json</html>"),
    )

    with pytest.raises(SearchAdapterError) as excinfo:
        brave_search("query", api_key=SECRET)

    assert SECRET not in str(excinfo.value)


def test_an_unexpected_vendor_shape_yields_no_results_rather_than_raising(monkeypatch) -> None:
    monkeypatch.setattr(
        "autoscout.grounded_search_adapter.urlopen",
        lambda request, timeout: _Response(json.dumps({"unexpected": True}).encode("utf-8")),
    )

    assert brave_search("query", api_key=SECRET) == []


def test_the_adapter_refuses_to_bind_beyond_loopback() -> None:
    with pytest.raises(SearchAdapterError) as excinfo:
        serve("0.0.0.0", 1)

    assert "loopback" in str(excinfo.value)


def test_the_provider_parses_the_adapter_output_and_owner_grouping_holds(monkeypatch) -> None:
    """End to end: vendor body in, classified discovery results out.

    Goodreads and Bookshop share one owner on purpose, so they cannot
    corroborate each other (#468, now through the owner policy of #687).
    A denylisted host is never classified, so it can never ground a
    description.
    """
    monkeypatch.setattr(
        "autoscout.grounded_search_adapter.urlopen",
        lambda request, timeout: _Response(
            json.dumps(
                _brave_payload(
                    [
                        "https://www.publishersweekly.com/9780374611798",
                        "https://www.goodreads.com/book/show/1-a",
                        "https://bookshop.org/p/books/a-book/1",
                        "https://www.reddit.com/r/books/whatever",
                    ]
                )
            ).encode("utf-8")
        ),
    )
    shaped = to_autoscout_shape(brave_search("query", api_key=SECRET))

    class _Transport:
        def request(self, url: str, **_kwargs: Any) -> Any:
            class _Body:
                status = 200
                body = json.dumps(shaped).encode("utf-8")

            return _Body()

    provider = HTTPDiscoveryProvider(
        "http://127.0.0.1:8899/search?q={query}",
        transport=_Transport(),
        classifier=committed_source_classifier(),
    )

    results = provider.discover({"title": "A Book", "primary_authors": ["Alex Author"]})

    classified = [result for result in results if result.classification == "classified"]
    assert len(classified) == 3
    # Three classified pages, but only two owners: the retail listings collapse.
    assert {result.owner_id for result in classified} == {"publishersweekly.com", "publisher-supplied-copy"}
    assert [result.classification for result in results if "reddit" in result.url] == ["unknown"]


def test_the_key_environment_variable_name_is_stable() -> None:
    """The launcher and the documentation both name this variable."""
    assert API_KEY_ENV == "AUTOSCOUT_SEARCH_API_KEY"


def test_handler_applies_shared_vendor_floor_before_brave_and_not_to_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeClock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = FakeClock()

    def sleeper(seconds: float) -> None:
        events.append(f"sleep:{seconds}")
        clock.value += seconds

    monkeypatch.setattr(
        _Handler,
        "rate_limiter",
        VendorRateLimiter(1.0, monotonic=clock, sleeper=sleeper),
    )

    def fake_brave(query: str, *, api_key: str, count: int = MAX_RESULTS) -> list[dict[str, Any]]:
        del query, api_key, count
        events.append("brave")
        return []

    monkeypatch.setattr("autoscout.grounded_search_adapter.brave_search", fake_brave)
    monkeypatch.setenv(API_KEY_ENV, "offline-test-key")
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(path: str) -> None:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            response.read()
            assert response.status == 200
        finally:
            connection.close()

    try:
        request("/health")
        assert events == []
        request("/search?q=first")
        request("/search?q=second")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert events == ["brave", "sleep:1.0", "brave"]


def test_the_vendor_rate_limiter_spaces_calls_without_waiting_in_the_test() -> None:
    """Brave's free and credit tiers allow one query per second.

    The clock and the sleeper are injected, so this proves the spacing with no
    real waiting and cannot become a flaky wall-clock assertion.
    """

    class FakeClock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = FakeClock()
    slept: list[float] = []

    def sleeper(seconds: float) -> None:
        slept.append(seconds)
        clock.value += seconds

    limiter = VendorRateLimiter(1.0, monotonic=clock, sleeper=sleeper)

    assert limiter.wait() == 0.0  # the first call is never delayed
    assert slept == []

    assert limiter.wait() == 1.0  # immediately after, wait the full interval
    clock.value += 0.25
    assert limiter.wait() == pytest.approx(0.75)  # a partial gap waits the remainder

    clock.value += 10.0
    assert limiter.wait() == 0.0  # a long gap is never delayed

    assert slept == [1.0, pytest.approx(0.75)]


def test_the_rate_limiter_holds_across_concurrent_callers() -> None:
    """The stage runs three workers, so the limiter must be shared and locked."""

    class FakeClock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    clock = FakeClock()
    limiter = VendorRateLimiter(1.0, monotonic=clock, sleeper=lambda seconds: None)
    waits: list[float] = []
    barrier = threading.Barrier(3)

    def call() -> None:
        barrier.wait()
        waits.append(limiter.wait())

    threads = [threading.Thread(target=call) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # One caller proceeds at once; each of the others is pushed a further second
    # out, so three concurrent workers cannot exceed one query per second.
    assert sorted(waits) == [0.0, 1.0, 2.0]
