"""A form-served ranking that proves its identity and walks its pages.

A form route returns rows that state nothing about which list they are, and
serves them a page at a time. These tests pin the two declarations that close
that gap, and pin that neither one relaxes the evidence contract.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from autoscout.http_decoding import MAX_RESPONSE_BYTES
from autoscout.ranking_evidence import RankingCaptureError
from autoscout.ranking_identity import identity_heading_matches
from autoscout.ranking_identity import ranking_identity_declared
from autoscout.ranking_identity import ranking_identity_route
from autoscout.ranking_identity import validate_ranking_identity_declaration
from autoscout.ranking_request import MAX_RANKING_REQUEST_PAGES
from autoscout.ranking_request import ranking_request_from_source
from autoscout.ranking_response import unwrap_ranking_response
from autoscout.source_collection import prepare_codex_extraction_worklist
from autoscout.source_collectors import CollectorRunError
from autoscout.source_collectors import configured_source_page
from autoscout.source_collectors import fetch_agent_source_page
from autoscout.source_collectors import model_bounded_page_data
from autoscout.source_collectors.base import capture_live_response
from autoscout.source_page_identity import validate_page_shape
from autoscout.source_probe import run_source_candidate_probe

ROWS_URL = "https://example.test/admin-ajax.php"
IDENTITY_URL = "https://example.test/book-list/2026-33-adult-nonfiction/"
CATEGORY = "Adult nonfiction"

BASE: dict[str, Any] = {
    "ranking_request_method": "POST",
    "ranking_request_body": ["action=rows", "paged=1"],
    "ranking_request_page_field": "paged",
    "ranking_request_max_pages": 4,
    "ranking_response_html_field": "html",
    "ranking_response_last_page_field": "last",
    "ranking_identity_url_template": "https://example.test/book-list/{year}-{week}-adult-nonfiction/",
    "ranking_identity_fields": ["year=sel_year", "week=sel_week"],
    "ranking_category_marker": CATEGORY,
    # The unchanged ranking-table row contract, exactly as a real source
    # declares it: neither new declaration relaxes any of this.
    "metadata_ranking_table": True,
    "ranking_row_selector": ".book-row-cover > .book-row",
    "ranking_rank_source": "document_order",
    "ranking_title_selector": ".col1 > .book-title",
    "ranking_author_selector": ".col1 > .author-name",
    "ranking_identifier_selector": ".col2 > .isbn",
    "ranking_reporting_period_undated": True,
    "ranking_min_raw_row_count": 1,
    "ranking_max_raw_row_count": 100,
    "ranking_evidence_url": "source_page",
    "adult_nonfiction_exclusions": ["children"],
}


def _isbn(index: int) -> str:
    """Return a valid ISBN-13 for row *index*; the contract checks the digit."""
    stem = f"978020{index:06d}"
    total = sum(int(digit) * (1 if position % 2 == 0 else 3) for position, digit in enumerate(stem))
    return stem + str((10 - total % 10) % 10)


def _rows(start: int, count: int) -> str:
    return "".join(
        '<div class="book-row-cover"><div class="book-row">'
        f'<div class="col1"><h3 class="book-title">Book {index}</h3>'
        '<h3 class="author-name">Author, Jane</h3></div>'
        f'<div class="col2"><div class="isbn">{_isbn(index)}</div></div>'
        "</div></div>"
        for index in range(start, start + count)
    )


def _envelope(start: int, count: int, last: bool) -> bytes:
    payload = {
        "html": _rows(start, count),
        "last": last,
        "sel_year": 2026,
        "sel_week": 33,
    }
    return json.dumps(payload).encode()


def _identity_page(heading: str = f"2026-33-{CATEGORY}") -> bytes:
    # The real page puts a search-overlay label above the real heading.
    return f"<html><body><h1>What are you looking for?</h1><h1>{heading}</h1></body></html>".encode()


class _Response:
    def __init__(self, status: int, body: bytes = b"", url: str = ROWS_URL) -> None:
        self.status = status
        self.body = body
        self.url = url

    def geturl(self) -> str:
        return self.url

    def read(self, size: int) -> bytes:
        return self.body[:size]

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def close(self) -> None:
        return None


class _PlainGetTransport:
    """Answers one old-style undeclared GET without an identity queue."""

    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: Any, timeout: float) -> _Response:
        self.calls.append((request.get_method(), request.full_url))
        if request.get_method() != "GET":
            raise AssertionError(f"unexpected {request.get_method()} {request.full_url}")
        return self.response


class _Transport:
    """Answers POSTs from a page queue and GETs from an identity queue."""

    def __init__(self, pages: list[_Response], identity: list[_Response] | None = None) -> None:
        self.pages = list(pages)
        self.identity = list(identity or [])
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: Any, timeout: float) -> _Response:
        method = request.get_method()
        self.calls.append((method, request.full_url))
        queue = self.pages if method == "POST" else self.identity
        if not queue:
            raise AssertionError(f"unexpected {method} {request.full_url}")
        return queue.pop(0)


WEEKLY_SOURCE = {**BASE, "url": ROWS_URL, "contract_family": "ranking-table"}


def _probe(tmp_path: Path, transport: _Transport, metadata: dict[str, Any] | None = None):
    return run_source_candidate_probe(
        [ROWS_URL],
        contract_families=["ranking-table"],
        contract_metadata=[dict(metadata if metadata is not None else BASE)],
        ledger_path=tmp_path / "ledger.json",
        opener=transport,
    )


# -- Identity declaration ---------------------------------------------------


def test_no_identity_declaration_is_valid_and_absent() -> None:
    assert validate_ranking_identity_declaration({}) == ""
    assert ranking_identity_declared({}) is False
    assert ranking_identity_route({}) is None


def test_a_declared_route_resolves_its_url_from_the_envelope() -> None:
    route = ranking_identity_route(BASE)
    assert route is not None
    assert route.resolve({"sel_year": 2026, "sel_week": 33}) == (IDENTITY_URL, "")


@pytest.mark.parametrize(
    ("configured_url", "identity_url", "accepted"),
    [
        ("https://example.test", "https://example.test:443/list/33", True),
        ("http://example.test:80", "http://example.test/list/33", True),
        ("https://example.test", "http://example.test/list/33", False),
        ("https://example.test", "https://example.test:444/list/33", False),
    ],
)
def test_identity_route_uses_same_origin_with_effective_ports(
    configured_url: str, identity_url: str, accepted: bool
) -> None:
    source = {
        **BASE,
        "url": configured_url,
        "ranking_identity_url_template": identity_url,
        "ranking_identity_fields": [],
    }

    error = validate_ranking_identity_declaration(source)

    assert (error == "") is accepted
    if accepted:
        route = ranking_identity_route(source)
        assert route is not None
        assert route.resolve({}) == (identity_url, "")


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        (
            {"ranking_identity_fields": ["year=sel_year"]},
            "ranking_identity_url_template uses undeclared placeholders: week",
        ),
        (
            {"ranking_identity_fields": ["year=sel_year", "week=sel_week", "spare=x"]},
            "ranking_identity_fields declares unused placeholders: spare",
        ),
        (
            {"ranking_identity_url_template": "/book-list/{year}-{week}/"},
            "ranking_identity_url_template must be an absolute HTTP or HTTPS URL",
        ),
        (
            {"ranking_category_marker": ""},
            "ranking_identity_url_template requires ranking_category_marker",
        ),
        (
            {"ranking_identity_fields": ["year"]},
            "ranking_identity_fields entry 'year' must use the form 'placeholder=field'",
        ),
        (
            {"ranking_identity_fields": ["year=sel_year", "year=other"]},
            "ranking_identity_fields declares 'year' more than once",
        ),
    ],
)
def test_a_malformed_identity_declaration_is_rejected(override: dict[str, Any], expected: str) -> None:
    assert validate_ranking_identity_declaration({**BASE, **override}) == expected


@pytest.mark.parametrize(
    "template",
    [
        "https://{host}.example.test/list/{year}",
        "https://example.test:{port}/list/{year}",
        "https://example.test/list/{year}?week={week}",
        "https://example.test/list/{year}#fragment",
    ],
)
def test_identity_placeholders_cannot_control_url_authority_or_fragment(template: str) -> None:
    source = {
        **BASE,
        "ranking_identity_url_template": template,
        "ranking_identity_fields": ["year=sel_year", "week=sel_week", "host=host", "port=port"],
    }
    assert validate_ranking_identity_declaration(source) != ""


def test_identity_fields_without_a_template_are_rejected() -> None:
    assert validate_ranking_identity_declaration({"ranking_identity_fields": ["year=sel_year"]}) == (
        "ranking_identity_fields requires ranking_identity_url_template"
    )


@pytest.mark.parametrize(
    "value",
    ["../../etc/passwd", "33/../../evil", "a b", "", "x" * 33, "évil"],
)
def test_an_unsafe_envelope_value_never_reaches_the_identity_url(value: str) -> None:
    route = ranking_identity_route(BASE)
    assert route is not None
    url, error = route.resolve({"sel_year": 2026, "sel_week": value})
    assert url == ""
    assert "plain token" in error


def test_a_non_scalar_envelope_value_is_refused() -> None:
    route = ranking_identity_route(BASE)
    assert route is not None
    url, error = route.resolve({"sel_year": 2026, "sel_week": ["33"]})
    assert url == ""
    assert "not a token" in error


def test_a_missing_envelope_field_is_refused() -> None:
    route = ranking_identity_route(BASE)
    assert route is not None
    url, error = route.resolve({"sel_year": 2026})
    assert url == ""
    assert "sel_week" in error


def test_the_heading_match_ignores_whitespace_and_case() -> None:
    assert identity_heading_matches(("2026-33-ADULT   NONFICTION",), CATEGORY) is True
    assert identity_heading_matches(("2026-33-ADULT NONFICTION",), CATEGORY, ("2026", "33")) is True
    assert identity_heading_matches(("2026-34-ADULT NONFICTION",), CATEGORY, ("2026", "33")) is False
    assert identity_heading_matches(("anything",), "") is False


# -- Paging declaration -----------------------------------------------------


def test_the_page_field_is_replaced_not_appended() -> None:
    request = ranking_request_from_source(BASE)
    assert [name for name, _value in request.for_page(3).fields] == ["action", "paged"]
    assert dict(request.for_page(3).fields)["paged"] == "3"


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        (
            {"ranking_request_page_field": "missing"},
            "ranking_request_page_field 'missing' is not declared in ranking_request_body",
        ),
        ({"ranking_request_max_pages": 0}, "ranking_request_max_pages must be a positive integer"),
        (
            {"ranking_request_max_pages": MAX_RANKING_REQUEST_PAGES + 1},
            f"ranking_request_max_pages must not exceed {MAX_RANKING_REQUEST_PAGES}",
        ),
    ],
)
def test_a_malformed_paging_declaration_is_rejected(override: dict[str, Any], expected: str) -> None:
    from autoscout.ranking_request import validate_ranking_request_declaration

    assert validate_ranking_request_declaration({**BASE, **override}) == expected


def test_a_last_page_flag_requires_the_html_field() -> None:
    from autoscout.ranking_response import validate_ranking_response_declaration

    source = {"ranking_response_last_page_field": "last"}
    assert validate_ranking_response_declaration(source) == (
        "ranking_response_last_page_field requires ranking_response_html_field"
    )


def test_an_unreadable_last_page_flag_stops_paging_rather_than_continuing() -> None:
    unwrapped = unwrap_ranking_response(json.dumps({"html": ""}), BASE)
    assert unwrapped.is_last_page(BASE) is True


# -- End to end -------------------------------------------------------------


def test_declared_pages_are_walked_and_joined_into_one_bounded_body(tmp_path: Path) -> None:
    transport = _Transport(
        pages=[_Response(200, _envelope(1, 15, False)), _Response(200, _envelope(16, 11, True))],
        identity=[_Response(200, _identity_page(), url=IDENTITY_URL)],
    )
    report, code = _probe(tmp_path, transport)

    result = report["candidates"][0]
    assert code == 0
    assert result["outcome"] == "accept"
    assert result["bounded_entry_count"] == 26
    assert [method for method, _url in transport.calls] == ["POST", "POST", "GET"]


def test_paging_stops_on_the_declared_last_page_flag(tmp_path: Path) -> None:
    transport = _Transport(
        pages=[_Response(200, _envelope(1, 5, True))],
        identity=[_Response(200, _identity_page(), url=IDENTITY_URL)],
    )
    report, code = _probe(tmp_path, transport)

    assert code == 0
    assert report["candidates"][0]["bounded_entry_count"] == 5
    assert [method for method, _url in transport.calls].count("POST") == 1


def test_paging_never_accepts_partial_rows_at_the_declared_cap(tmp_path: Path) -> None:
    # Every page claims there is another; reaching the cap is incomplete.
    transport = _Transport(
        pages=[_Response(200, _envelope(index * 2 + 1, 2, False)) for index in range(8)],
        identity=[_Response(200, _identity_page(), url=IDENTITY_URL)],
    )
    report, code = _probe(tmp_path, transport)

    assert code == 1
    result = report["candidates"][0]
    assert result["outcome"] == "reject"
    assert result["reason_code"] == "ranking_paging"
    assert result["bounded_entry_count"] == 0
    assert [method for method, _url in transport.calls].count("POST") == BASE["ranking_request_max_pages"]
    assert [method for method, _url in transport.calls].count("GET") == 0


def test_paging_status_failure_refuses_partial_rows(tmp_path: Path) -> None:
    transport = _Transport(
        pages=[_Response(200, _envelope(1, 2, False)), _Response(503)],
        identity=[_Response(200, _identity_page(), url=IDENTITY_URL)],
    )
    report, code = _probe(tmp_path, transport)

    result = report["candidates"][0]
    assert code == 1
    assert result["reason_code"] == "ranking_paging"
    assert "HTTP 503" in result["reason"]
    assert [method for method, _url in transport.calls] == ["POST", "POST"]


def test_paging_redirect_failure_refuses_partial_rows(tmp_path: Path) -> None:
    transport = _Transport(
        pages=[
            _Response(200, _envelope(1, 2, False)),
            _Response(200, _envelope(3, 2, True), url="https://elsewhere.test/rows"),
        ],
        identity=[_Response(200, _identity_page(), url=IDENTITY_URL)],
    )
    report, code = _probe(tmp_path, transport)

    result = report["candidates"][0]
    assert code == 1
    assert result["reason_code"] == "ranking_paging"
    assert "unrelated source target" in result["reason"]


@pytest.mark.parametrize("bad_envelope", [{"html": _rows(3, 2)}, {"html": ""}])
def test_paging_missing_or_empty_page_signal_refuses_partial_rows(tmp_path: Path, bad_envelope: dict[str, Any]) -> None:
    transport = _Transport(
        pages=[_Response(200, _envelope(1, 2, False)), _Response(200, json.dumps(bad_envelope).encode())],
        identity=[_Response(200, _identity_page(), url=IDENTITY_URL)],
    )
    report, code = _probe(tmp_path, transport)

    result = report["candidates"][0]
    assert code == 1
    assert result["reason_code"] == "ranking_paging"
    assert result["bounded_entry_count"] == 0


def test_paging_duplicate_page_refuses_partial_rows(tmp_path: Path) -> None:
    first = _envelope(1, 2, False)
    transport = _Transport(
        pages=[_Response(200, first), _Response(200, first)],
        identity=[_Response(200, _identity_page(), url=IDENTITY_URL)],
    )
    report, code = _probe(tmp_path, transport)

    result = report["candidates"][0]
    assert code == 1
    assert result["reason_code"] == "ranking_paging"
    assert "repeated" in result["reason"]


def test_paging_duplicate_detection_ignores_nonsemantic_markup_changes(tmp_path: Path) -> None:
    first_html = '<div class="row" data-cache="first"><a href="/book-1">Book 1</a></div>'
    second_html = '<div data-cache="second" class="row"><a href="/book-1"> Book 1 </a></div>'
    first = json.dumps({"html": first_html, "last": False, "sel_year": 2026, "sel_week": 33}).encode()
    second = json.dumps({"html": second_html, "last": True}).encode()
    transport = _Transport(pages=[_Response(200, first), _Response(200, second)])

    report, code = _probe(tmp_path, transport)

    result = report["candidates"][0]
    assert code == 1
    assert result["reason_code"] == "ranking_paging"
    assert "repeated" in result["reason"]


def test_first_post_response_overflow_defers_without_head_evidence(tmp_path: Path) -> None:
    oversized = json.dumps(
        {
            "html": _rows(1, 2) + ("x" * (MAX_RESPONSE_BYTES + 10)),
            "last": True,
            "sel_year": 2026,
            "sel_week": 33,
        }
    ).encode()
    transport = _Transport(pages=[_Response(200, oversized)])
    report, code = _probe(tmp_path, transport)

    result = report["candidates"][0]
    assert code == 3
    assert result["outcome"] == "defer"
    assert result["reason_code"] == "response_too_large"
    assert result["http_evidence"]["head"]["status"] is None


def test_joined_paging_html_overflow_refuses_partial_rows(tmp_path: Path) -> None:
    large_rows = _rows(1, 2) + ("x" * 600_000)
    pages = [
        _Response(200, json.dumps({"html": large_rows, "last": False, "sel_year": 2026, "sel_week": 33}).encode()),
        _Response(200, json.dumps({"html": large_rows.replace("Book 1", "Book 3"), "last": True}).encode()),
    ]
    transport = _Transport(pages=pages, identity=[_Response(200, _identity_page(), url=IDENTITY_URL)])
    report, code = _probe(tmp_path, transport)

    result = report["candidates"][0]
    assert code == 3
    assert result["outcome"] == "defer"
    assert result["reason_code"] == "response_too_large"
    assert result["bounded_entry_count"] == 0
    assert result["http_evidence"]["get"]["truncated"] is True


def test_probe_refuses_wrong_raw_row_count_without_a_detail_route(tmp_path: Path) -> None:
    source = {
        key: value
        for key, value in BASE.items()
        if key not in {"ranking_min_raw_row_count", "ranking_max_raw_row_count"}
    }
    source["ranking_expected_raw_row_count"] = 26
    transport = _Transport(
        pages=[_Response(200, _envelope(1, 15, True))],
        identity=[_Response(200, _identity_page(), url=IDENTITY_URL)],
    )

    report, code = _probe(tmp_path, transport, source)

    result = report["candidates"][0]
    assert code == 1
    assert result["reason_code"] == "ranking_row_shape"
    assert "expected exactly 26" in result["reason"]


def test_weekly_fetch_rejects_a_non_resolving_declared_identity_selector_at_the_identity_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = {**WEEKLY_SOURCE, "visible_title_selector": ".page-title"}
    transport = _Transport(
        pages=[_Response(200, _envelope(1, 15, True))],
        identity=[_Response(200, _identity_page(), url=IDENTITY_URL)],
    )
    monkeypatch.setattr("autoscout.source_collectors.urlopen", transport)

    with pytest.raises(CollectorRunError) as exc_info:
        fetch_agent_source_page(tmp_path / "config.json", tmp_path, source, reference_date=date(2026, 8, 24))

    assert exc_info.value.debug_metadata["safe_failure_status"] == "identity_route"
    assert [method for method, _url in transport.calls] == ["POST", "GET"]


def test_weekly_fetch_accepts_a_declared_identity_selector_that_resolves_on_the_identity_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The declared selector skips the search-overlay heading on its own, so the
    # accepted identity does not depend on the category-matching tolerance.
    source = {**WEEKLY_SOURCE, "visible_title_selector": ".page-title"}
    identity_html = (
        f'<html><body><h1>What are you looking for?</h1><h1 class="page-title">2026-33-{CATEGORY}</h1></body></html>'
    ).encode()
    transport = _Transport(
        pages=[_Response(200, _envelope(1, 15, True))],
        identity=[_Response(200, identity_html, url=IDENTITY_URL)],
    )
    monkeypatch.setattr("autoscout.source_collectors.urlopen", transport)

    fetch_agent_source_page(tmp_path / "config.json", tmp_path, source, reference_date=date(2026, 8, 24))

    assert [method for method, _url in transport.calls] == ["POST", "GET"]


def test_weekly_fetch_rejects_wrong_identity_period(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _Transport(
        pages=[_Response(200, _envelope(1, 15, True))],
        identity=[_Response(200, _identity_page(f"2026-34-{CATEGORY}"), url=IDENTITY_URL)],
    )
    monkeypatch.setattr("autoscout.source_collectors.urlopen", transport)

    with pytest.raises(CollectorRunError, match="category and period"):
        fetch_agent_source_page(tmp_path / "config.json", tmp_path, WEEKLY_SOURCE, reference_date=date(2026, 8, 24))


def test_weekly_fetch_rejects_wrong_raw_row_count_without_detail_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = {
        key: value
        for key, value in WEEKLY_SOURCE.items()
        if key not in {"ranking_min_raw_row_count", "ranking_max_raw_row_count"}
    }
    source["ranking_expected_raw_row_count"] = 26
    transport = _Transport(
        pages=[_Response(200, _envelope(1, 15, True))],
        identity=[_Response(200, _identity_page(), url=IDENTITY_URL)],
    )
    monkeypatch.setattr("autoscout.source_collectors.urlopen", transport)

    with pytest.raises(CollectorRunError, match="expected exactly 26"):
        fetch_agent_source_page(tmp_path / "config.json", tmp_path, source, reference_date=date(2026, 8, 24))


def test_weekly_fetch_rejects_semantically_repeated_pages_before_identity_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_html = '<div class="row" data-cache="first"><a href="/book-1">Book 1</a></div>'
    second_html = '<div data-cache="second" class="row"><a href="/book-1"> Book 1 </a></div>'
    transport = _Transport(
        pages=[
            _Response(200, json.dumps({"html": first_html, "last": False}).encode()),
            _Response(200, json.dumps({"html": second_html, "last": True}).encode()),
        ]
    )
    monkeypatch.setattr("autoscout.source_collectors.urlopen", transport)

    with pytest.raises(CollectorRunError, match="repeated"):
        fetch_agent_source_page(tmp_path / "config.json", tmp_path, WEEKLY_SOURCE)
    assert [method for method, _url in transport.calls] == ["POST", "POST"]


def test_weekly_fetch_joins_rows_and_keeps_identity_html_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = _Transport(
        pages=[_Response(200, _envelope(1, 15, False)), _Response(200, _envelope(16, 11, True))],
        identity=[_Response(200, _identity_page(), url=IDENTITY_URL)],
    )
    monkeypatch.setattr("autoscout.source_collectors.urlopen", transport)

    page, _debug = fetch_agent_source_page(
        tmp_path / "config.json", tmp_path, WEEKLY_SOURCE, reference_date=date(2026, 8, 24)
    )

    assert page["html"] == _rows(1, 15) + _rows(16, 11)
    assert page["identity_html"] == _identity_page().decode()
    assert [method for method, _url in transport.calls] == ["POST", "POST", "GET"]
    assert "Book 1" not in page["identity_html"]


def test_identity_html_is_required_for_weekly_page_validation() -> None:
    page = {"source_page": ROWS_URL, "resolved_url": ROWS_URL, "html": _rows(1, 2)}
    assert "requires identity_html" in validate_page_shape(WEEKLY_SOURCE, page)
    page["identity_html"] = _identity_page().decode()
    assert validate_page_shape(WEEKLY_SOURCE, page) == ""


def test_identity_html_is_retained_in_replay_page_data(tmp_path: Path) -> None:
    page = {
        "source_page": ROWS_URL,
        "resolved_url": ROWS_URL,
        "html": _rows(1, 2),
        "identity_html": _identity_page().decode(),
        "reference_date": "2026-08-24",
    }
    source = {**WEEKLY_SOURCE, "capture_live_response_file": "ranking-capture.json"}
    capture_live_response(tmp_path / "config.json", tmp_path, source, page)
    replay_source = {**source, "live_response_file": "ranking-capture.json"}
    replay_page, _debug = configured_source_page(tmp_path / "config.json", replay_source)

    assert replay_page["html"] == page["html"]
    assert replay_page["identity_html"] == page["identity_html"]
    assert "Book 90" not in replay_page["html"]


def test_capture_writer_refuses_an_oversized_identity_before_writing(tmp_path: Path) -> None:
    page = {
        "source_page": ROWS_URL,
        "resolved_url": ROWS_URL,
        "html": "x",
        "identity_html": _identity_page().decode(),
    }
    capture_path = tmp_path / "ranking-capture.json"
    source = {
        **WEEKLY_SOURCE,
        "capture_live_response_file": str(capture_path),
        "max_response_bytes": 32,
    }

    with pytest.raises(RankingCaptureError, match="identity capture exceeds"):
        capture_live_response(tmp_path / "config.json", tmp_path, source, page)

    assert not capture_path.exists()


def test_weekly_fetch_keeps_an_undeclared_source_as_one_plain_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://example.test/old-list"
    raw_html = "<html><body><h1>Old list</h1><p>Raw unchanged</p></body></html>"
    transport = _PlainGetTransport(_Response(200, raw_html.encode(), url=url))
    monkeypatch.setattr("autoscout.source_collectors.urlopen", transport)

    page, _debug = fetch_agent_source_page(tmp_path / "config.json", tmp_path, {"url": url})

    assert transport.calls == [("GET", url)]
    assert page["html"] == raw_html
    assert "identity_html" not in page


def test_weekly_fetch_preserves_undeclared_get_body_for_a_non_http_error_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://example.test/old-list"
    raw_html = "<html><body><h1>Old list</h1><p>503 body</p></body></html>"
    transport = _PlainGetTransport(_Response(503, raw_html.encode(), url=url))
    monkeypatch.setattr("autoscout.source_collectors.urlopen", transport)

    page, _debug = fetch_agent_source_page(tmp_path / "config.json", tmp_path, {"url": url})

    assert page["html"] == raw_html


def test_weekly_fetch_refuses_a_declared_ranking_response_status_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = _Transport(pages=[_Response(503)], identity=[])
    monkeypatch.setattr("autoscout.source_collectors.urlopen", transport)

    with pytest.raises(CollectorRunError, match="HTTP 503"):
        fetch_agent_source_page(tmp_path / "config.json", tmp_path, WEEKLY_SOURCE)


def test_model_bounded_ranking_data_keeps_identity_html_separate() -> None:
    page = {
        "source_page": ROWS_URL,
        "html": _rows(1, 2),
        "identity_html": _identity_page().decode(),
    }
    bounded, _debug = model_bounded_page_data(page, 10_000, source=WEEKLY_SOURCE)

    assert "Book 1" in bounded["html"]
    assert "What are you looking for?" in bounded["identity_html"]
    assert "Book 90" not in bounded["identity_html"]

    long_page = {**page, "html": _rows(1, 100)}
    bounded_limited, _debug = model_bounded_page_data(long_page, 1_000, source=WEEKLY_SOURCE)
    assert len(bounded_limited["html"]) + len(bounded_limited["identity_html"]) <= 1_000


def test_weekly_fetch_refuses_incomplete_paging_before_identity_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = _Transport(pages=[_Response(200, _envelope(index * 2 + 1, 2, False)) for index in range(4)])
    monkeypatch.setattr("autoscout.source_collectors.urlopen", transport)

    with pytest.raises(CollectorRunError, match="max_pages=4"):
        fetch_agent_source_page(tmp_path / "config.json", tmp_path, WEEKLY_SOURCE, reference_date=date(2026, 8, 24))
    assert [method for method, _url in transport.calls] == ["POST", "POST"] + ["POST"] * 2


def test_an_identity_route_accepts_an_implicit_and_explicit_default_https_port_pair(tmp_path: Path) -> None:
    transport = _Transport(
        pages=[_Response(200, _envelope(1, 15, True))],
        identity=[_Response(200, _identity_page(), url="https://example.test:443/book-list/2026-33-adult-nonfiction/")],
    )

    report, code = _probe(tmp_path, transport)

    assert code == 0
    assert report["candidates"][0]["outcome"] == "accept"


def test_an_identity_redirect_to_another_target_is_rejected(tmp_path: Path) -> None:
    transport = _Transport(
        pages=[_Response(200, _envelope(1, 15, True))],
        identity=[_Response(200, _identity_page(), url="https://elsewhere.test/identity")],
    )
    report, code = _probe(tmp_path, transport)

    result = report["candidates"][0]
    assert code == 1
    assert result["reason_code"] == "identity_route"
    assert "different target" in result["reason"]


def test_probe_rejects_a_non_resolving_declared_identity_selector_at_the_identity_route(
    tmp_path: Path,
) -> None:
    transport = _Transport(
        pages=[_Response(200, _envelope(1, 15, True))],
        identity=[_Response(200, _identity_page(), url=IDENTITY_URL)],
    )
    report, code = run_source_candidate_probe(
        [ROWS_URL],
        visible_title_selectors=[".page-title"],
        contract_families=["ranking-table"],
        contract_metadata=[{**BASE, "visible_title_selector": ".metadata-title"}],
        ledger_path=tmp_path / "ledger.json",
        opener=transport,
    )

    result = report["candidates"][0]
    assert code == 1
    assert result["outcome"] == "reject"
    assert result["reason_code"] == "identity_route"
    assert "does not state the declared category and period" in result["reason"]
    assert [method for method, _url in transport.calls] == ["POST", "GET"]


def test_probe_accepts_a_declared_identity_selector_that_resolves_on_the_identity_page(
    tmp_path: Path,
) -> None:
    identity_html = f'<html><body><h1 class="page-title">2026-33-{CATEGORY}</h1></body></html>'.encode()
    transport = _Transport(
        pages=[_Response(200, _envelope(1, 15, True))],
        identity=[_Response(200, identity_html, url=IDENTITY_URL)],
    )
    report, code = run_source_candidate_probe(
        [ROWS_URL],
        visible_title_selectors=[".page-title"],
        contract_families=["ranking-table"],
        contract_metadata=[{**BASE, "visible_title_selector": ".metadata-title"}],
        ledger_path=tmp_path / "ledger.json",
        opener=transport,
    )

    result = report["candidates"][0]
    assert code == 0
    assert result["outcome"] == "accept"
    assert [method for method, _url in transport.calls] == ["POST", "GET"]


def test_an_identity_page_naming_another_list_is_rejected(tmp_path: Path) -> None:
    transport = _Transport(
        pages=[_Response(200, _envelope(1, 15, True))],
        identity=[_Response(200, _identity_page(f"2026-34-{CATEGORY}"), url=IDENTITY_URL)],
    )
    report, code = _probe(tmp_path, transport)

    result = report["candidates"][0]
    assert code == 1
    assert result["outcome"] == "reject"
    assert result["reason_code"] == "identity_route"
    assert "does not state the declared category and period" in result["reason"]


def test_an_unreachable_identity_page_is_rejected(tmp_path: Path) -> None:
    transport = _Transport(
        pages=[_Response(200, _envelope(1, 15, True))],
        identity=[_Response(404, b"", url=IDENTITY_URL)],
    )
    report, code = _probe(tmp_path, transport)

    result = report["candidates"][0]
    assert code == 1
    assert result["reason_code"] == "identity_route"
    assert "answered 404" in result["reason"]


def test_the_identity_heading_is_what_the_verdict_reports(tmp_path: Path) -> None:
    # Not the search-overlay label that sits above it on the real page.
    transport = _Transport(
        pages=[_Response(200, _envelope(1, 15, True))],
        identity=[_Response(200, _identity_page(), url=IDENTITY_URL)],
    )
    report, _code = _probe(tmp_path, transport)

    assert report["candidates"][0]["detected_heading"] == f"2026-33-{CATEGORY}"


def test_the_identity_page_is_never_read_for_rows(tmp_path: Path) -> None:
    identity_with_rows = f"<html><body><h1>2026-33-{CATEGORY}</h1>{_rows(90, 40)}</body></html>".encode()
    transport = _Transport(
        pages=[_Response(200, _envelope(1, 15, True))],
        identity=[_Response(200, identity_with_rows, url=IDENTITY_URL)],
    )
    report, code = _probe(tmp_path, transport)

    assert code == 0
    assert report["candidates"][0]["bounded_entry_count"] == 15


def test_a_declared_route_with_no_identity_capture_yields_no_identity() -> None:
    from autoscout.source_page_identity import ranking_page_identity

    assert ranking_page_identity(f"<html><h1>2026-33-{CATEGORY}</h1></html>", dict(BASE)) is None


def test_a_source_without_an_identity_route_still_reads_its_own_heading() -> None:
    from autoscout.source_page_identity import ranking_page_identity

    source = {"ranking_category_marker": CATEGORY, "ranking_reporting_period_undated": True}
    identity = ranking_page_identity(f"<html><h1>2026-33-{CATEGORY}</h1></html>", source)
    assert identity is not None
    assert identity.heading == f"2026-33-{CATEGORY}"


def test_prepare_worklist_records_an_oversized_identity_as_one_failed_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = {
        **WEEKLY_SOURCE,
        "name": "Bounded ranking",
        "family": "bestseller/ranking",
        "active": True,
        "access_status": "open",
        "extraction_method": "codex",
        "max_response_bytes": 256,
    }
    config_path = tmp_path / "sources.json"
    config_path.write_text(json.dumps({"sources": [source]}), encoding="utf-8")
    page = {
        "source_page": ROWS_URL,
        "resolved_url": ROWS_URL,
        "html": _rows(1, 1),
        "identity_html": _identity_page().decode() + ("identity evidence " * 40),
    }
    monkeypatch.setattr("autoscout.source_collection.codex_source_page", lambda *_args, **_kwargs: (page, {}))

    worklist = prepare_codex_extraction_worklist(config_path, tmp_path / "out", "2026-08-24")

    assert worklist.failed_source_count == 1
    assert worklist.work_items == []
    run = worklist.source_runs[0]
    assert run["status"] == "failed"
    assert run["debug_metadata"]["safe_failure_status"] == "ranking_capture_bound"
    assert "identity capture exceeds" in run["reason"]
    assert not list((tmp_path / "out").glob("*codex-capture*.json"))


def test_codex_replay_rejects_a_short_ranking_page_even_with_a_retained_reference_date(
    tmp_path: Path,
) -> None:
    url = "https://example.test/replayed-ranking"
    page_path = tmp_path / "page.json"
    page_path.write_text(
        json.dumps(
            {
                "source_page": url,
                "resolved_url": url,
                "reference_date": "2026-08-24",
                "html": (
                    f"<h1>{CATEGORY}</h1>"
                    '<div class="book-row-cover"><div class="book-row">'
                    '<span class="col1"><span class="book-title">Book 1</span></span>'
                    '<span class="col1"><span class="author-name">Author, Jane</span></span>'
                    '<span class="col2"><span class="isbn">9780200000017</span></span>'
                    "</div></div>"
                ),
            }
        ),
        encoding="utf-8",
    )
    source = {
        "name": "Replayed ranking",
        "family": "bestseller/ranking",
        "url": url,
        "active": True,
        "access_status": "open",
        "extraction_method": "codex-extraction-replay",
        "source_page_fixture_file": str(page_path),
        "contract_family": "ranking-table",
        "metadata_ranking_table": True,
        "ranking_row_selector": ".book-row-cover > .book-row",
        "ranking_rank_source": "document_order",
        "ranking_title_selector": ".col1 > .book-title",
        "ranking_author_selector": ".col1 > .author-name",
        "ranking_identifier_selector": ".col2 > .isbn",
        "ranking_category_marker": CATEGORY,
        "ranking_reporting_period_undated": True,
        "ranking_expected_raw_row_count": 2,
        "adult_nonfiction_exclusions": ["children"],
    }
    config_path = tmp_path / "sources.json"
    config_path.write_text(json.dumps({"sources": [source]}), encoding="utf-8")

    worklist = prepare_codex_extraction_worklist(config_path, tmp_path / "out", "2026-08-24")

    assert worklist.failed_source_count == 1
    assert worklist.source_runs[0]["status"] == "failed"
    assert "expected exactly 2" in worklist.source_runs[0]["reason"]


def test_semantic_page_fingerprint_ignores_attribute_order_whitespace_and_hidden_values() -> None:
    from autoscout.ranking_paging import semantic_page_fingerprint

    first = (
        '<div class="row" data-cache="first"><a href="/book-1">'
        "Book 1</a><span>Author 1</span></div>"
        '<div hidden data-secret="one">Book 90</div><script>nonce=one</script>'
    )
    second = (
        '<div data-cache="second" class="row"><a href="/book-1"> Book 1 </a>'
        "<span> Author 1 </span></div>"
        '<div data-secret="two" hidden>Book 91</div><script>nonce=two</script>'
    )

    assert semantic_page_fingerprint(first) == semantic_page_fingerprint(second)


def test_semantic_page_fingerprint_distinguishes_visible_metadata_and_links() -> None:
    from autoscout.ranking_paging import semantic_page_fingerprint

    base = '<div class="row"><a href="/book-1">Book 1</a><span>Author 1</span></div>'

    assert semantic_page_fingerprint(base) != semantic_page_fingerprint(base.replace("Author 1", "Author 2"))
    assert semantic_page_fingerprint(base) != semantic_page_fingerprint(base.replace("/book-1", "/book-2"))
