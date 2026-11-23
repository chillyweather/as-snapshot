"""Brave's ``search_lang`` vocabulary is not ISO 639-1 (issue #707).

The 2026-09-16 weekly run sent ``search_lang=pt`` for every Portuguese
candidate. Brave answers a bare ``pt`` with HTTP 422, so all five Portuguese
candidates ended with ``search_or_fetch_failure`` before a single fetch.
The adapter is the vendor seam, so the translation lives there: AutoScout keeps
speaking ISO 639-1, and only the Brave request uses Brave's codes.
"""

from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from http.server import HTTPServer
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import pytest

from autoscout.grounded_search_adapter import API_KEY_ENV
from autoscout.grounded_search_adapter import BRAVE_SEARCH_LANGUAGES
from autoscout.grounded_search_adapter import SearchAdapterError
from autoscout.grounded_search_adapter import SearchLanguageRejectedError
from autoscout.grounded_search_adapter import _Handler
from autoscout.grounded_search_adapter import brave_search
from autoscout.grounded_search_adapter import brave_search_language
from autoscout.language_codes import ISO_639_1_CODES
from autoscout.vendor_rate_limit import VendorRateLimiter

# Copied from the ``Language-Input`` enum of the Brave web search API reference
# on 2026-09-16. A change here is a vendor change and needs its own evidence.
BRAVE_DOCUMENTED_SEARCH_LANG = (
    "ar eu bn bg ca zh-hans zh-hant hr cs da nl en en-gb et fi fr gl de el gu he hi hu is it ja jp kn ko "
    "lv lt ms ml mr nb pl pt-br pt-pt pa ro ru sr sk sl es sv ta te th tr uk vi"
).split()


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, size: int) -> bytes:
        return self._body[:size]

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _sent_language(url: str) -> list[str] | None:
    return parse_qs(urlsplit(url).query).get("search_lang")


def test_the_adapter_vocabulary_is_exactly_the_documented_brave_enum() -> None:
    assert BRAVE_SEARCH_LANGUAGES == frozenset(BRAVE_DOCUMENTED_SEARCH_LANG)


@pytest.mark.parametrize(
    ("iso", "brave"),
    [
        ("pt", "pt-br"),
        ("zh", "zh-hans"),
        ("no", "nb"),
        ("nb", "nb"),
        ("ja", "ja"),
        ("en", "en"),
        ("es", "es"),
        ("PT", "pt-br"),
    ],
)
def test_iso_codes_translate_to_brave_codes(iso: str, brave: str) -> None:
    assert brave_search_language(iso) == brave


@pytest.mark.parametrize("value", ["id", "ga", "english", "", None, "pt-br"])
def test_codes_brave_does_not_accept_translate_to_nothing(value: object) -> None:
    assert brave_search_language(value) is None


def test_every_iso_code_translates_to_a_documented_brave_code_or_to_nothing() -> None:
    for code in ISO_639_1_CODES:
        translated = brave_search_language(code)
        assert translated is None or translated in BRAVE_SEARCH_LANGUAGES, code


def test_a_portuguese_request_reaches_brave_as_pt_br(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def fake_urlopen(request: Any, timeout: float) -> _Response:
        del timeout
        captured["url"] = request.full_url
        return _Response(json.dumps({"web": {"results": []}}).encode())

    monkeypatch.setattr("autoscout.grounded_search_adapter.urlopen", fake_urlopen)

    brave_search('"Coragem de transformar" Alcione Albanesi book 9786555447538', api_key="k", search_lang="pt")

    assert _sent_language(captured["url"]) == ["pt-br"]


def test_an_unsupported_language_is_omitted_from_the_brave_request(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def fake_urlopen(request: Any, timeout: float) -> _Response:
        del timeout
        captured["url"] = request.full_url
        return _Response(json.dumps({"web": {"results": []}}).encode())

    monkeypatch.setattr("autoscout.grounded_search_adapter.urlopen", fake_urlopen)

    brave_search("buku", api_key="k", search_lang="id")

    assert _sent_language(captured["url"]) is None


def test_a_422_for_a_request_with_a_language_raises_the_language_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: Any, timeout: float) -> _Response:
        raise HTTPError(request.full_url, 422, "Unprocessable Entity", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr("autoscout.grounded_search_adapter.urlopen", fake_urlopen)

    with pytest.raises(SearchLanguageRejectedError) as raised:
        brave_search("a book", api_key="brave-secret", search_lang="es")
    assert "brave-secret" not in str(raised.value)

    with pytest.raises(SearchAdapterError) as without_language:
        brave_search("a book", api_key="brave-secret")
    assert not isinstance(without_language.value, SearchLanguageRejectedError)
    assert str(without_language.value) == "search vendor returned HTTP 422"


def _serve_one(path: str) -> tuple[int, dict[str, Any]]:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_the_handler_retries_once_without_a_language_when_brave_rejects_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    waits: list[str] = []

    def fake_brave(query: str, *, api_key: str, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append(dict(kwargs))
        if kwargs.get("search_lang"):
            raise SearchLanguageRejectedError("search vendor returned HTTP 422")
        return [{"url": "https://publisher.example.test/books/book"}]

    class CountingLimiter(VendorRateLimiter):
        def wait(self) -> None:
            waits.append("wait")

    monkeypatch.setattr("autoscout.grounded_search_adapter.brave_search", fake_brave)
    monkeypatch.setattr(_Handler, "rate_limiter", CountingLimiter(0.0))
    monkeypatch.setenv(API_KEY_ENV, "offline-key")

    status, body = _serve_one("/search?q=livro&search_lang=pt")

    assert status == 200
    assert calls == [{"search_lang": "pt"}, {}]
    # The retry is a second vendor call, so it waits for the shared floor too.
    assert waits == ["wait", "wait"]
    # The candidate's language is still what the results are about.
    assert body["results"][0]["source_language"] == "pt"


def test_the_handler_does_not_retry_other_vendor_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_brave(query: str, *, api_key: str, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append(dict(kwargs))
        raise SearchAdapterError("search vendor returned HTTP 429")

    monkeypatch.setattr("autoscout.grounded_search_adapter.brave_search", fake_brave)
    monkeypatch.setattr(_Handler, "rate_limiter", VendorRateLimiter(0.0))
    monkeypatch.setenv(API_KEY_ENV, "offline-key")

    status, body = _serve_one("/search?q=livro&search_lang=pt")

    assert status == 502
    assert body == {"error": "search vendor returned HTTP 429"}
    assert calls == [{"search_lang": "pt"}]
