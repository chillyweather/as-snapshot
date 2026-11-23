"""Offline regression coverage for issue #484 truncated HTML pages."""

from pathlib import Path
from typing import Any

import pytest

import autoscout.grounded_description as grounded_description
from autoscout.grounded_description import MAX_RESPONSE_BYTES
from autoscout.grounded_description import REASON_TRUNCATED_PAGE_NO_EVIDENCE
from autoscout.grounded_description import BoundedHTTPError
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import DiscoveryResult
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import HTTPResponse
from autoscout.grounded_description import StandardLibraryHTTPTransport
from autoscout.grounded_description import reason_label

FIXTURES = Path(__file__).parent / "fixtures" / "issue-484-truncated-pages"
CHECKED_DATE = "2026-08-16"
LIMIT = 512
URLS = {
    "oversized-og-description.html": "https://fixtures.example.test/books/truncated-og-description",
    "truncated-middle-tag.html": "https://fixtures.example.test/books/truncated-middle-tag",
    "truncated-multibyte.html": "https://fixtures.example.test/books/truncated-multibyte",
    "truncated-jsonld.html": "https://fixtures.example.test/books/truncated-jsonld",
    "oversized-no-evidence.html": "https://fixtures.example.test/books/truncated-no-evidence",
    "below-limit.html": "https://fixtures.example.test/books/below-limit",
}


class FixtureTransport:
    def request(self, url: str, **kwargs: object) -> HTTPResponse:
        filename = next(filename for filename, candidate_url in URLS.items() if candidate_url == url)
        body = (FIXTURES / filename).read_bytes()
        limit = int(kwargs["max_response_bytes"])
        return HTTPResponse(
            url=url,
            status=200,
            headers={"content-type": "text/html; charset=utf-8", "content-length": str(len(body))},
            body=body[:limit],
            truncated=len(body) > limit,
        )


def _discovery(url: str) -> DiscoveryResult:
    return DiscoveryResult(
        url=url,
        source_kind="publisher",
        source_family="publisher",
        owner_id="fixture-owner",
        source_language="en",
    )


def _fetch(filename: str, *, limit: int = LIMIT):
    return BoundedHTTPFetcher(transport=FixtureTransport(), max_response_bytes=limit).fetch(
        _discovery(URLS[filename]),
        checked_date=CHECKED_DATE,
    )


def test_issue_484_truncated_page_keeps_title_and_og_description() -> None:
    source = _fetch("oversized-og-description.html")

    assert source.title == "The Book Beyond the Limit"
    assert source.primary_authors == ("Alex Reader",)
    assert source.subject_text.startswith("A grounded book about how communities preserve memory")


def test_issue_484_unfinished_tag_is_fail_safe() -> None:
    source = _fetch("truncated-middle-tag.html")

    assert source.title == "Book With an Unfinished Tag"


def test_issue_484_partial_utf8_character_is_fail_safe() -> None:
    body = (FIXTURES / "truncated-multibyte.html").read_bytes()
    marker = "é".encode()
    limit = body.index(marker) + 1

    source = _fetch("truncated-multibyte.html", limit=limit)

    assert source.title == "Book With a Partial Character"


@pytest.mark.parametrize("closing_prefix", [b"<", b"</", b"</scr"])
def test_issue_484_unfinished_jsonld_script_keeps_complete_evidence(closing_prefix: bytes) -> None:
    body = (FIXTURES / "truncated-jsonld.html").read_bytes()
    limit = body.index(b"</script>") + len(closing_prefix)

    source = _fetch("truncated-jsonld.html", limit=limit)

    assert source.title == "Book From Partial JSON-LD"
    assert source.primary_authors == ("JSON Author",)
    assert source.subject_text == "A subject recovered from a complete JSON-LD object with a literal < marker."


def test_issue_484_truncated_page_without_evidence_has_distinct_reason() -> None:
    try:
        _fetch("oversized-no-evidence.html")
    except BoundedHTTPError as exc:
        assert exc.reason_code == REASON_TRUNCATED_PAGE_NO_EVIDENCE
    else:
        raise AssertionError("expected a truncated-page evidence refusal")

    assert reason_label(REASON_TRUNCATED_PAGE_NO_EVIDENCE) == "Обрезанная страница не содержит подтверждения"


def test_issue_484_standard_transport_returns_only_the_bounded_prefix(monkeypatch: Any) -> None:
    body = (FIXTURES / "oversized-og-description.html").read_bytes()

    class OfflineResponse:
        def __init__(self) -> None:
            self.headers = {"Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(body))}
            self.read_sizes: list[int] = []
            self.position = 0

        def __enter__(self) -> "OfflineResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            self.read_sizes.append(size)
            if size < 0:
                size = len(body) - self.position
            chunk = body[self.position : self.position + size]
            self.position += len(chunk)
            return chunk

        def geturl(self) -> str:
            return "https://fixtures.example.test/books/truncated-og-description"

        def getcode(self) -> int:
            return 200

    response = OfflineResponse()
    monkeypatch.setattr(
        grounded_description,
        "build_opener",
        lambda _handler: type("Opener", (), {"open": lambda self, request, timeout: response})(),
    )

    bounded = StandardLibraryHTTPTransport().request(
        "https://fixtures.example.test/books/truncated-og-description",
        timeout_seconds=1,
        max_response_bytes=LIMIT,
        max_redirects=3,
    )

    assert len(bounded.body) == LIMIT
    assert len(bounded.body) <= LIMIT
    assert bounded.truncated is True
    assert response.read_sizes == [LIMIT]
    assert MAX_RESPONSE_BYTES == 1_000_000


def test_issue_484_standard_transport_exact_limit_without_length_is_not_truncated(monkeypatch: Any) -> None:
    body = b"x" * LIMIT

    class OfflineResponse:
        def __init__(self) -> None:
            self.headers = {"Content-Type": "text/html; charset=utf-8"}
            self.read_sizes: list[int] = []
            self.position = 0

        def __enter__(self) -> "OfflineResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            self.read_sizes.append(size)
            if size < 0:
                size = len(body) - self.position
            chunk = body[self.position : self.position + size]
            self.position += len(chunk)
            return chunk

        def geturl(self) -> str:
            return "https://fixtures.example.test/books/exact-limit"

        def getcode(self) -> int:
            return 200

    response = OfflineResponse()
    monkeypatch.setattr(
        grounded_description,
        "build_opener",
        lambda _handler: type("Opener", (), {"open": lambda self, request, timeout: response})(),
    )

    bounded = StandardLibraryHTTPTransport().request(
        "https://fixtures.example.test/books/exact-limit",
        timeout_seconds=1,
        max_response_bytes=LIMIT,
        max_redirects=3,
    )

    assert len(bounded.body) == LIMIT
    assert len(bounded.body) <= LIMIT
    assert bounded.truncated is False
    assert response.read_sizes == [LIMIT, 1]


def test_issue_484_fetcher_clamps_an_injected_overbound_response() -> None:
    body = (FIXTURES / "oversized-no-evidence.html").read_bytes()

    class OverboundTransport:
        def request(self, url: str, **_kwargs: object) -> HTTPResponse:
            return HTTPResponse(
                url=url,
                status=200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=body,
            )

    try:
        BoundedHTTPFetcher(transport=OverboundTransport(), max_response_bytes=LIMIT).fetch(
            _discovery(URLS["oversized-no-evidence.html"]),
            checked_date=CHECKED_DATE,
        )
    except BoundedHTTPError as exc:
        assert exc.reason_code == REASON_TRUNCATED_PAGE_NO_EVIDENCE
    else:
        raise AssertionError("expected the fetcher to ignore bytes beyond its configured bound")


def test_issue_484_stage_propagates_truncated_no_evidence_reason() -> None:
    class Discovery:
        def discover(self, _candidate: dict[str, object], **_kwargs: object) -> list[DiscoveryResult]:
            return [_discovery(URLS["oversized-no-evidence.html"])]

    class Fetch:
        def fetch(self, _result: DiscoveryResult, **_kwargs: object) -> object:
            raise BoundedHTTPError(
                "truncated page contained no usable book evidence",
                reason_code=REASON_TRUNCATED_PAGE_NO_EVIDENCE,
            )

    candidate = {
        "candidate_id": "candidate-truncated",
        "identity_key": "truncated work|fixture author",
        "title": "Truncated Work",
        "primary_authors": ["Fixture Author"],
        "priority_label": "D",
        "first_seen_date": CHECKED_DATE,
    }
    artifact = GroundedDescriptionStage(discovery=Discovery(), fetch=Fetch()).run(
        [candidate],
        operation_id="issue-484",
        operation_date=CHECKED_DATE,
        force_candidates=True,
    )

    assert artifact.outcomes[0].reason_code == REASON_TRUNCATED_PAGE_NO_EVIDENCE


def test_issue_484_page_below_bound_keeps_existing_behavior() -> None:
    source = _fetch("below-limit.html", limit=LIMIT)

    assert source.title == "Small Book"
    assert source.primary_authors == ("Small Author",)
    assert source.subject_text == "A small page with stable book evidence."
    assert source.checked_date == CHECKED_DATE


def test_issue_484_http_response_truncation_field_is_backward_compatible() -> None:
    response = HTTPResponse("https://example.test", 200, {}, b"body")

    assert response.truncated is False
