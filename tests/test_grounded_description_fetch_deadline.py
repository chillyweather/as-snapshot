"""Candidate deadlines bound the real transport without live HTTP requests."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from email.message import Message
from io import BytesIO
from types import SimpleNamespace
from typing import Any
from urllib.request import HTTPHandler
from urllib.request import Request
from urllib.response import addinfourl

import pytest

import autoscout.grounded_description as grounded

URL = "https://example.test/book"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        clock: FakeClock,
        *,
        read_seconds: float = 0.0,
        slice_bytes: int = 1,
        content_length: str | None = None,
    ) -> None:
        self.body = BytesIO(body)
        self.clock = clock
        self.read_seconds = read_seconds
        self.slice_bytes = slice_bytes
        self.headers = {"Content-Type": "text/html", "X-Private": "discard"}
        if content_length is not None:
            self.headers["Content-Length"] = content_length
        self.timeout = 10.0
        self.timeouts: list[float] = []
        self.reads: list[int] = []
        self.slices: list[int] = []
        self.closed = False
        self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=self))

    def gettimeout(self) -> float:
        return self.timeout

    def settimeout(self, seconds: float) -> None:
        self.timeout = seconds
        self.timeouts.append(seconds)

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True

    def read(self, size: int) -> bytes:
        self.reads.append(size)
        return self.body.read(size)

    def read1(self, size: int) -> bytes:
        self.slices.append(size)
        self.clock.now += min(self.read_seconds, self.timeout)
        if self.read_seconds > self.timeout:
            raise TimeoutError("receive exceeded the narrowed timeout")
        return self.body.read(min(size, self.slice_bytes))

    def geturl(self) -> str:
        return URL

    def getcode(self) -> int:
        return 200


def install_opener(monkeypatch: pytest.MonkeyPatch, response: FakeResponse) -> None:
    monkeypatch.setattr(grounded, "build_opener", lambda *_args: SimpleNamespace(open=lambda *_a, **_kw: response))


def request(**kwargs: Any) -> grounded.HTTPResponse:
    return grounded.StandardLibraryHTTPTransport().request(URL, timeout_seconds=10, max_response_bytes=4, **kwargs)


def test_trickling_body_is_stopped_at_candidate_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    response = FakeResponse(b"abcdefgh", clock, read_seconds=1)
    install_opener(monkeypatch, response)
    with pytest.raises(grounded.BoundedHTTPError) as error:
        request(remaining_seconds=lambda: 3 - clock())
    assert error.value.reason_code == grounded.REASON_CANDIDATE_BUDGET_EXHAUSTED
    assert clock() == 3
    assert response.timeouts == [3, 2, 1]
    assert response.reads == []
    assert response.closed


def test_stalled_read_uses_only_time_left(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    response = FakeResponse(b"abcdefgh", clock, read_seconds=5)
    install_opener(monkeypatch, response)
    with pytest.raises(grounded.BoundedHTTPError) as error:
        request(remaining_seconds=lambda: 2 - clock())
    assert error.value.reason_code == grounded.REASON_CANDIDATE_BUDGET_EXHAUSTED
    assert clock() == 2
    assert response.closed


@pytest.mark.parametrize("blocked_phase", ["headers", "body"])
def test_blocking_request_returns_at_deadline(monkeypatch: pytest.MonkeyPatch, blocked_phase: str) -> None:
    response = FakeResponse(b"body", FakeClock())
    release = threading.Event()
    finished = threading.Event()
    opened_timeouts: list[float] = []

    def blocking(*_args: Any, **_kwargs: Any) -> Any:
        release.wait(0.5)
        return response if blocked_phase == "headers" else b"body"

    def open_response(_request: Request, *, timeout: float) -> FakeResponse:
        opened_timeouts.append(timeout)
        if blocked_phase == "headers":
            return blocking()
        return response

    original_close = response.close

    def close() -> None:
        original_close()
        finished.set()

    monkeypatch.setattr(response, "close", close)
    if blocked_phase == "body":
        monkeypatch.setattr(response, "read1", blocking)
    monkeypatch.setattr(grounded, "build_opener", lambda *_args: SimpleNamespace(open=open_response))
    started = time.monotonic()
    deadline = started + 0.05
    try:
        with pytest.raises(grounded.BoundedHTTPError) as error:
            request(remaining_seconds=lambda: deadline - time.monotonic())
        elapsed = time.monotonic() - started
        assert error.value.reason_code == grounded.REASON_CANDIDATE_BUDGET_EXHAUSTED
        assert 0.03 <= elapsed < 0.3
        assert 0 < opened_timeouts[0] <= 0.05
    finally:
        release.set()
        assert finished.wait(0.5)
    assert response.closed


@pytest.mark.parametrize(
    "hop_seconds, expected_timeouts, succeeds", [([4, 4, 1], [10, 6, 2], True), ([4, 6], [10, 6], False)]
)
def test_redirects_share_candidate_deadline(
    monkeypatch: pytest.MonkeyPatch, hop_seconds: list[int], expected_timeouts: list[int], succeeds: bool
) -> None:
    clock = FakeClock()
    timeouts: list[float] = []
    build_opener = grounded.build_opener

    class Redirects(HTTPHandler):
        def http_open(self, req: Any) -> Any:
            timeouts.append(req.timeout)
            clock.now += hop_seconds[len(timeouts) - 1]
            headers = Message()
            if len(timeouts) < 3:
                headers["Location"] = f"http://example.test/hop{len(timeouts)}"
                response = addinfourl(BytesIO(b""), headers, req.full_url, 302)
                response.msg = "Found"
            else:
                headers["Content-Length"] = "4"
                response = addinfourl(BytesIO(b"body"), headers, req.full_url, 200)
                response.msg = "OK"
            return response

    monkeypatch.setattr(grounded, "build_opener", lambda *handlers: build_opener(*handlers, Redirects()))

    def fetch() -> grounded.HTTPResponse:
        return grounded.StandardLibraryHTTPTransport().request(
            "http://example.test/book", timeout_seconds=10, max_response_bytes=4, remaining_seconds=lambda: 10 - clock()
        )

    if succeeds:
        assert fetch().body == b"body"
    else:
        with pytest.raises(grounded.BoundedHTTPError) as error:
            fetch()
        assert error.value.reason_code == grounded.REASON_CANDIDATE_BUDGET_EXHAUSTED
    assert timeouts == expected_timeouts


@pytest.mark.parametrize(
    "body, content_length, truncated, reads",
    [
        (b"body", None, False, [4, 1]),
        (b"longer", None, True, [4, 1]),
        (b"longer", "6", True, [4]),
        (b"body", "4", False, [4]),
    ],
)
@pytest.mark.parametrize("has_deadline", [False, True])
def test_success_preserves_body_headers_and_truncation(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
    content_length: str | None,
    truncated: bool,
    reads: list[int],
    has_deadline: bool,
) -> None:
    clock = FakeClock()
    response = FakeResponse(body, clock, slice_bytes=4, read_seconds=0.25, content_length=content_length)
    install_opener(monkeypatch, response)
    deadline = (0.5 if content_length is None else 0.25) + 0.001
    kwargs = {"remaining_seconds": lambda: deadline - clock()} if has_deadline else {}
    result = request(**kwargs)
    assert result.body == body[:4]
    assert result.truncated is truncated
    assert result.url == URL
    assert result.status == 200
    assert result.headers == {key.casefold(): value for key, value in response.headers.items() if key != "X-Private"}
    assert response.slices == (reads if has_deadline else [])
    assert response.reads == ([] if has_deadline else reads)
    assert response.closed


@pytest.mark.parametrize("content_length", [None, "4"])
def test_final_read_at_deadline_is_rejected(monkeypatch: pytest.MonkeyPatch, content_length: str | None) -> None:
    clock = FakeClock()
    response = FakeResponse(b"body", clock, slice_bytes=4, read_seconds=0.5, content_length=content_length)
    install_opener(monkeypatch, response)
    deadline = 0.5 if content_length else 1.0
    with pytest.raises(grounded.BoundedHTTPError) as error:
        request(remaining_seconds=lambda: deadline - clock())
    assert error.value.reason_code == grounded.REASON_CANDIDATE_BUDGET_EXHAUSTED
    assert clock() == deadline
    assert response.closed


@pytest.mark.parametrize("provider", ["page", "discovery"])
def test_providers_pass_candidate_deadline_to_transport(provider: str) -> None:
    clock = FakeClock()
    budget = grounded.CandidateRequestBudget(deadline=8, max_fetches=1, monotonic=clock)
    observed: list[Callable[[], float]] = []

    class Transport:
        def request(self, url: str, **kwargs: Any) -> grounded.HTTPResponse:
            observed.append(kwargs["remaining_seconds"])
            assert kwargs["timeout_seconds"] == 8
            return grounded.HTTPResponse(
                url, 200, {}, b"<html><title>Example Book</title></html>" if provider == "page" else b'{"results": []}'
            )

    if provider == "page":
        grounded.BoundedHTTPFetcher(transport=Transport(), min_host_interval_seconds=0).fetch(
            {"url": URL, "classification": "classified"}, request_budget=budget
        )
    else:
        grounded.HTTPDiscoveryProvider("https://search.test/?q={query}", transport=Transport()).discover(
            {"title": "Example Book", "primary_authors": ["Example Author"]}, request_budget=budget
        )
    assert len(observed) == 1
    assert observed[0].__self__ is budget
    clock.now = 3
    assert observed[0]() == 5
