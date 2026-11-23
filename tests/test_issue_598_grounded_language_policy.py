"""Deterministic contract tests for issue #598 grounded language policy."""

from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from http.server import HTTPServer
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import pytest

from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import FetchedSource
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import HTTPDiscoveryProvider
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import candidate_source_language
from autoscout.grounded_description import committed_source_classifier
from autoscout.grounded_description import evaluate_grounding_gate
from autoscout.grounded_description import validate_stage_artifact
from autoscout.grounded_search_adapter import API_KEY_ENV
from autoscout.grounded_search_adapter import _Handler
from autoscout.grounded_search_adapter import brave_search
from autoscout.grounded_search_adapter import to_autoscout_shape
from autoscout.vendor_rate_limit import VendorRateLimiter


class RecordingTransport:
    def __init__(self) -> None:
        self.requested_urls: list[str] = []

    def request(self, url: str, **_: Any) -> HTTPResponse:
        self.requested_urls.append(url)
        return HTTPResponse(url, 200, {}, json.dumps({"results": []}).encode("utf-8"))


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, size: int) -> bytes:
        return self._body[:size]

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def test_todostuslibros_retail_listing_source_uses_conservative_fetch_preference() -> None:
    url = "https://www.todostuslibros.com/libros/atlas-mitologico-de-grecia_979-13-87933-06-7"
    classification = committed_source_classifier().classify(url)

    assert classification["classification"] == "classified"
    assert classification["source_kind"] == "retail_listing"
    assert classification["source_family"] == "retail_listing"

    class FetchTransport:
        def request(self, requested_url: str, **_: Any) -> HTTPResponse:
            return HTTPResponse(
                requested_url,
                200,
                {"content-type": "text/html; charset=utf-8"},
                b"<html><head><title>Atlas Mitologico de Grecia</title>"
                b"<meta name='author' content='Example Author'>"
                b"<meta name='description' content='A book about Greek mythology.'>"
                b"</head></html>",
            )

    discovery = DiscoveryResult(
        url=url,
        source_kind=str(classification["source_kind"]),
        source_family=str(classification["source_family"]),
        owner_id=str(classification["owner_id"]),
        source_language="es",
        classification=str(classification["classification"]),
        classification_evidence=classification,
    )
    fetched = BoundedHTTPFetcher(transport=FetchTransport()).fetch(discovery, checked_date="2026-09-01")

    assert fetched.source_preference == 2


def test_candidate_language_resolution_uses_explicit_then_primary_then_unambiguous_evidence() -> None:
    assert candidate_source_language({"language": "PT", "primary_signal": {"language": "it"}}) == "pt"
    assert candidate_source_language({"primary_signal": {"language": "IT"}}) == "it"
    assert candidate_source_language({"source_evidence": [{"language": "cs"}, {"source_language": "CS"}]}) == "cs"
    assert candidate_source_language({"source_evidence": [{"language": "cs"}, {"language": "it"}]}) == ""
    assert candidate_source_language({"language": "not-a-language", "primary_signal": {"language": "en"}}) == "en"


def test_conflicting_explicit_languages_fail_closed_without_using_primary_signal() -> None:
    class Candidate(dict[str, Any]):
        def get(self, key: str, default: Any = None) -> Any:
            if key == "primary_signal":
                raise AssertionError("ambiguous explicit language must not consult primary_signal")
            return super().get(key, default)

    candidate = Candidate(language="es", content_language="pt", primary_signal={"language": "en"})

    assert candidate_source_language(candidate) == ""


@pytest.mark.parametrize("language", ["de", "fr"])
def test_provider_classifies_cross_language_result_and_sends_candidate_search_language(language: str) -> None:
    class ResultTransport:
        def __init__(self) -> None:
            self.requested_urls: list[str] = []

        def request(self, url: str, **_: Any) -> HTTPResponse:
            self.requested_urls.append(url)
            return HTTPResponse(
                url,
                200,
                {},
                json.dumps({"results": [{"url": "https://en.wikipedia.org/wiki/Test_Book"}]}).encode("utf-8"),
            )

    transport = ResultTransport()
    classifier = committed_source_classifier()
    provider = HTTPDiscoveryProvider(
        "http://127.0.0.1:8899/search?q={query}",
        transport=transport,
        classifier=classifier,
    )

    results = provider.discover({"title": "Test Book", "primary_authors": ["Test Author"], "source_language": language})

    assert len(results) == 1
    assert results[0].classification == "classified"
    assert results[0].source_language == language
    assert "source_language" not in results[0].classification_evidence
    assert "languages" not in results[0].classification_evidence
    assert results[0].classification_evidence["source_metadata"]["source_language"] == language
    params = parse_qs(urlsplit(transport.requested_urls[0]).query)
    assert set(params) == {"q", "search_lang"}
    assert params["search_lang"] == [language]


def test_provider_propagates_known_language_without_duplicate_search_lang() -> None:
    transport = RecordingTransport()
    provider = HTTPDiscoveryProvider(
        "http://127.0.0.1:8899/search?fixed=1&q={query}&country=DE&search_lang=old&search_lang=duplicate",
        transport=transport,
        classifier=committed_source_classifier(),
    )

    provider.discover(
        {
            "title": "A Book",
            "primary_authors": ["An Author"],
            "source_language": "ES",
        }
    )

    params = parse_qs(urlsplit(transport.requested_urls[0]).query)
    assert params["fixed"] == ["1"]
    assert params["country"] == ["DE"]
    assert params["search_lang"] == ["es"]
    assert len(params["search_lang"]) == 1


def test_brave_adapter_validates_locale_and_preserves_it_in_translated_results(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def fake_urlopen(request: Any, timeout: float) -> _Response:
        del timeout
        captured["url"] = request.full_url
        return _Response(
            json.dumps({"web": {"results": [{"url": "https://publisher.example.test/books/book"}]}}).encode()
        )

    monkeypatch.setattr("autoscout.grounded_search_adapter.urlopen", fake_urlopen)

    vendor_results = brave_search("a book", api_key="offline-key", search_lang="ES")
    translated = to_autoscout_shape(vendor_results, source_language="ES")

    assert parse_qs(urlsplit(captured["url"]).query)["search_lang"] == ["es"]
    assert translated["results"][0]["source_language"] == "es"

    brave_search("a book", api_key="offline-key", search_lang="english")
    assert "search_lang" not in parse_qs(urlsplit(captured["url"]).query)


def test_loopback_adapter_propagates_validated_search_language_to_brave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def fake_brave(query: str, *, api_key: str, **kwargs: Any) -> list[dict[str, Any]]:
        captured.update(query=query, api_key=api_key, **kwargs)
        return []

    monkeypatch.setattr("autoscout.grounded_search_adapter.brave_search", fake_brave)
    monkeypatch.setattr(_Handler, "rate_limiter", VendorRateLimiter(0.0))
    monkeypatch.setenv(API_KEY_ENV, "offline-key")
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        try:
            connection.request("GET", "/search?q=libro&search_lang=ES")
            response = connection.getresponse()
            response.read()
            assert response.status == 200
        finally:
            connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert captured == {"query": "libro", "api_key": "offline-key", "search_lang": "es"}


def test_loopback_adapter_omits_invalid_language_from_vendor_and_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_brave(query: str, *, api_key: str, **kwargs: Any) -> list[dict[str, Any]]:
        captured.update(query=query, api_key=api_key, **kwargs)
        return [{"url": "https://publisher.example.test/books/book"}]

    monkeypatch.setattr("autoscout.grounded_search_adapter.brave_search", fake_brave)
    monkeypatch.setattr(_Handler, "rate_limiter", VendorRateLimiter(0.0))
    monkeypatch.setenv(API_KEY_ENV, "offline-key")
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port)
        try:
            connection.request("GET", "/search?q=libro&search_lang=english")
            response = connection.getresponse()
            body = json.loads(response.read().decode("utf-8"))
            assert response.status == 200
        finally:
            connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert captured == {"query": "libro", "api_key": "offline-key"}
    assert body["results"] == [{"url": "https://publisher.example.test/books/book", "title": "", "snippet": ""}]


@pytest.mark.parametrize(
    ("language", "urls"),
    [
        (
            "es",
            (
                "https://www.todostuslibros.com/libros/atlas-mitologico-de-grecia_979-13-87933-06-7",
                "https://www.casadellibro.com/libro-atlas-mitologico-de-grecia/9791387933067/1",
            ),
        ),
        (
            "pt",
            (
                "https://www.amazon.com.br/morte-que-vale-pena-viver/dp/6555646000",
                "https://books.google.com/books?id=a-morte-que-vale-a-pena-viver",
            ),
        ),
    ],
)
def test_two_storefronts_with_one_owner_cannot_corroborate_independently(
    language: str,
    urls: tuple[str, str],
) -> None:
    classifier = committed_source_classifier()
    sources: list[FetchedSource] = []
    for url in urls:
        classification = classifier.classify(url)
        assert classification["classification"] == "classified"
        assert classification["owner_id"] == "publisher-supplied-copy"
        sources.append(
            FetchedSource(
                url=url,
                source_kind=str(classification["source_kind"]),
                source_family=str(classification["source_family"]),
                owner_id=str(classification["owner_id"]),
                source_language=language,
                title="Shared publisher copy",
                primary_authors=("Example Author",),
                subject_text="Shared publisher copy subject.",
                checked_date="2026-09-01",
                classification="classified",
            )
        )

    assert all(source.classification_evidence["source_metadata"]["source_language"] == language for source in sources)
    result = evaluate_grounding_gate(
        {
            "candidate_id": "shared-publisher-copy|example-author",
            "identity_key": "shared publisher copy|example author",
            "title": "Shared publisher copy",
            "primary_authors": ["Example Author"],
        },
        sources,
    )

    assert len(result.accepted_sources) == 1
    assert result.accepted_sources[0].owner_id == "publisher-supplied-copy"
    assert result.accepted_sources[0].classification_evidence["source_metadata"]["source_language"] == language
    assert result.source_independence_evidence is not None
    assert result.source_independence_evidence.owner_ids == ("publisher-supplied-copy",)


def test_provider_discovers_for_a_language_no_source_declares() -> None:
    transport = RecordingTransport()
    provider = HTTPDiscoveryProvider(
        "http://127.0.0.1:8899/search?q={query}",
        transport=transport,
        classifier=committed_source_classifier(),
    )
    candidate = {"title": "A Book", "primary_authors": ["An Author"], "source_language": "ko"}

    assert provider.discover(candidate) == []
    assert len(transport.requested_urls) == 1


def test_stage_reaches_discovery_for_a_language_no_source_declares() -> None:
    """Issue #691: no candidate is refused before discovery for its language.

    Policy languages used to refuse a Korean candidate before any request
    was made; owner derivation declares no language at all.
    """
    transport = RecordingTransport()
    classifier = committed_source_classifier()
    provider = HTTPDiscoveryProvider(
        "http://127.0.0.1:8899/search?q={query}",
        transport=transport,
        classifier=classifier,
    )
    candidate = {
        "candidate_id": "book|author",
        "identity_key": "book|author",
        "title": "A Book",
        "primary_authors": ["An Author"],
        "source_language": "ko",
    }
    artifact = GroundedDescriptionStage(discovery=provider).run(
        [candidate],
        operation_id="issue-598",
        operation_date="2026-09-01",
        force_candidates=True,
    )

    assert artifact.outcomes[0].reason_code == "no_results"
    assert validate_stage_artifact(artifact.to_dict())
    assert len(transport.requested_urls) == 1
