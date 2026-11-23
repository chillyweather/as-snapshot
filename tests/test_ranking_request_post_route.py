"""A ranking page served by a declared form POST route.

The Norwegian Boklista dated page answers a plain GET with a heading, a
publication date, and no rows: its weekly Top 25 comes from a form POST.
These tests pin the declaration that lets such a source be probed and
collected, and pin that it buys no relaxation of the evidence contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import pytest

from autoscout.ranking_request import DEFAULT_RANKING_REQUEST
from autoscout.ranking_request import MAX_RANKING_REQUEST_BODY_BYTES
from autoscout.ranking_request import MAX_RANKING_REQUEST_FIELDS
from autoscout.ranking_request import RankingRequest
from autoscout.ranking_request import ranking_request_from_source
from autoscout.ranking_request import validate_ranking_request_declaration
from autoscout.source_probe import run_source_candidate_probe

SOURCE_URL = "https://example.test/wp-admin/admin-ajax.php"

POST_DECLARATION: dict[str, Any] = {
    "ranking_request_method": "POST",
    "ranking_request_body": ["action=load_book_archive_list", "bookcat=177", "paged=1"],
}


def _page(entries: int = 2) -> bytes:
    items = "".join(
        f'<li><a href="{SOURCE_URL}/book-{index}">Book {index}</a><span class="author">By: Jane Author</span></li>'
        for index in range(1, entries + 1)
    )
    return f"<html><body><h1>Current list</h1><ul>{items}</ul></body></html>".encode()


class _FakeResponse:
    def __init__(self, status: int, body: bytes = b"", url: str = SOURCE_URL) -> None:
        self.status = status
        self.body = body
        self.url = url

    def geturl(self) -> str:
        return self.url

    def read(self, size: int) -> bytes:
        return self.body[:size]

    def close(self) -> None:
        return None


class _RecordingTransport:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, bytes]] = []

    def __call__(self, request: Any, timeout: float) -> _FakeResponse:
        self.calls.append((request.get_method(), request.full_url, request.data or b""))
        if not self.responses:
            raise AssertionError("unexpected extra network call")
        return self.responses.pop(0)


# -- Declaration validation -------------------------------------------------


def test_a_source_that_declares_nothing_keeps_the_plain_get() -> None:
    assert validate_ranking_request_declaration({}) == ""
    assert ranking_request_from_source({}) == DEFAULT_RANKING_REQUEST
    assert ranking_request_from_source({}).is_post is False


def test_an_explicit_get_declaration_is_valid() -> None:
    assert validate_ranking_request_declaration({"ranking_request_method": "get"}) == ""
    assert ranking_request_from_source({"ranking_request_method": "get"}).is_post is False


def test_a_declared_post_builds_a_form_encoded_body() -> None:
    request = ranking_request_from_source(POST_DECLARATION)
    assert request.is_post is True
    assert request.headers == {"Content-Type": "application/x-www-form-urlencoded"}
    assert dict(parse_qsl(request.body.decode())) == {
        "action": "load_book_archive_list",
        "bookcat": "177",
        "paged": "1",
    }


def test_a_declared_post_preserves_the_declared_field_order() -> None:
    request = ranking_request_from_source(POST_DECLARATION)
    assert [name for name, _value in request.fields] == ["action", "bookcat", "paged"]
    assert request.body == ranking_request_from_source(POST_DECLARATION).body


def test_for_page_replaces_a_middle_page_field_in_place() -> None:
    source = {
        **POST_DECLARATION,
        "ranking_request_body": ["paged=1", "action=load_book_archive_list", "bookcat=177"],
        "ranking_request_page_field": "paged",
        "ranking_request_max_pages": 3,
        "ranking_response_html_field": "html",
        "ranking_response_last_page_field": "last",
    }

    request = ranking_request_from_source(source).for_page(3)

    assert request.fields == (("paged", "3"), ("action", "load_book_archive_list"), ("bookcat", "177"))
    assert request.body == b"paged=3&action=load_book_archive_list&bookcat=177"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ({"ranking_request_method": "PUT"}, "ranking_request_method must be one of: GET, POST"),
        ({"ranking_request_method": "DELETE"}, "ranking_request_method must be one of: GET, POST"),
        ({"ranking_request_method": 7}, "ranking_request_method must be a string"),
        (
            {"ranking_request_method": "POST"},
            "ranking_request_method POST requires a nonempty ranking_request_body",
        ),
        (
            {"ranking_request_method": "POST", "ranking_request_body": []},
            "ranking_request_method POST requires a nonempty ranking_request_body",
        ),
        (
            {"ranking_request_body": ["a=1"]},
            "ranking_request_body requires ranking_request_method POST",
        ),
        (
            {"ranking_request_method": "POST", "ranking_request_body": "action=x"},
            "ranking_request_body must be a list of 'name=value' entries",
        ),
        (
            {"ranking_request_method": "POST", "ranking_request_body": [5]},
            "ranking_request_body entries must be strings",
        ),
        (
            {"ranking_request_method": "POST", "ranking_request_body": ["novalue"]},
            "ranking_request_body entry 'novalue' must use the form 'name=value'",
        ),
        (
            {"ranking_request_method": "POST", "ranking_request_body": ["=1"]},
            "ranking_request_body entry '=1' has an empty field name",
        ),
        (
            {"ranking_request_method": "POST", "ranking_request_body": ["a=1", "a=2"]},
            "ranking_request_body declares 'a' more than once",
        ),
    ],
)
def test_a_malformed_declaration_is_rejected_with_its_reason(source: dict[str, Any], expected: str) -> None:
    assert validate_ranking_request_declaration(source) == expected


def test_a_control_character_in_a_declared_field_is_rejected() -> None:
    source = {"ranking_request_method": "POST", "ranking_request_body": ["action=load\r\nX-Evil: 1"]}
    assert validate_ranking_request_declaration(source) == (
        "ranking_request_body entry 'action' contains a control character"
    )


def test_too_many_declared_fields_are_rejected() -> None:
    body = [f"field{index}=1" for index in range(MAX_RANKING_REQUEST_FIELDS + 1)]
    source = {"ranking_request_method": "POST", "ranking_request_body": body}
    assert "declares more than" in validate_ranking_request_declaration(source)


def test_an_oversized_declared_body_is_rejected_before_any_request() -> None:
    source = {
        "ranking_request_method": "POST",
        "ranking_request_body": ["action=" + "x" * (MAX_RANKING_REQUEST_BODY_BYTES + 1)],
    }
    assert "encodes to more than" in validate_ranking_request_declaration(source)


def test_the_largest_declared_page_body_is_bounded_before_network_use() -> None:
    action_length = MAX_RANKING_REQUEST_BODY_BYTES - len("action=") - len("&paged=1")
    source = {
        "ranking_request_method": "POST",
        "ranking_request_body": ["action=" + "x" * action_length, "paged=1"],
        "ranking_request_page_field": "paged",
        "ranking_request_max_pages": 10,
        "ranking_response_html_field": "html",
        "ranking_response_last_page_field": "last",
    }
    error = validate_ranking_request_declaration(source)

    assert "for page 10 encodes to more than 4096 bytes" in error


def test_an_invalid_declaration_never_yields_a_post_request() -> None:
    # Fail closed: a validation gap must not widen into an unintended method.
    assert ranking_request_from_source({"ranking_request_method": "POST"}) == DEFAULT_RANKING_REQUEST
    assert ranking_request_from_source({"ranking_request_method": "PATCH"}) == DEFAULT_RANKING_REQUEST


def test_describe_never_leaks_declared_field_values() -> None:
    described = RankingRequest("POST", (("action", "secret_value"),)).describe()
    assert described == "POST action"
    assert "secret_value" not in described


# -- Probe behaviour --------------------------------------------------------


def test_a_declared_post_route_is_probed_with_one_post_and_no_head(tmp_path: Path) -> None:
    transport = _RecordingTransport([_FakeResponse(200, _page())])
    report, code = run_source_candidate_probe(
        [SOURCE_URL],
        contract_metadata=[POST_DECLARATION],
        ledger_path=tmp_path / "ledger.json",
        opener=transport,
    )

    result = report["candidates"][0]
    assert code == 0
    assert result["outcome"] == "accept"
    assert result["bounded_entry_count"] == 2
    assert [method for method, _url, _body in transport.calls] == ["POST"]
    assert result["http_evidence"]["head"]["status"] is None
    assert result["head_status"] is None
    ledger = json.loads((tmp_path / "ledger.json").read_text(encoding="utf-8"))
    assert ledger["entries"][0]["http_evidence"]["head"]["status"] is None
    assert dict(parse_qsl(transport.calls[0][2].decode()))["action"] == "load_book_archive_list"


def test_the_post_method_is_recorded_in_the_probe_http_evidence(tmp_path: Path) -> None:
    report, _code = run_source_candidate_probe(
        [SOURCE_URL],
        contract_metadata=[POST_DECLARATION],
        ledger_path=tmp_path / "ledger.json",
        opener=_RecordingTransport([_FakeResponse(200, _page())]),
    )

    assert report["candidates"][0]["http_evidence"]["get"]["method"] == "POST"
    assert report["candidates"][0]["http_evidence"]["head"]["status"] is None
    assert report["candidates"][0]["head_status"] is None


def test_a_get_route_records_no_method_so_existing_evidence_is_unchanged(tmp_path: Path) -> None:
    report, _code = run_source_candidate_probe(
        [SOURCE_URL],
        ledger_path=tmp_path / "ledger.json",
        opener=_RecordingTransport([_FakeResponse(200), _FakeResponse(200, _page())]),
    )

    assert "method" not in report["candidates"][0]["http_evidence"]["get"]


def test_a_declared_post_route_is_still_held_to_the_bounded_evidence_contract(tmp_path: Path) -> None:
    rowless = b"<html><body><h1>Current list</h1><p>Published 19.08.2026</p></body></html>"
    report, code = run_source_candidate_probe(
        [SOURCE_URL],
        contract_metadata=[POST_DECLARATION],
        ledger_path=tmp_path / "ledger.json",
        opener=_RecordingTransport([_FakeResponse(200, rowless)]),
    )

    result = report["candidates"][0]
    assert code == 1
    assert result["outcome"] == "reject"
    assert result["bounded_entry_count"] == 0


def test_a_blocked_post_route_is_rejected_on_its_own_status(tmp_path: Path) -> None:
    report, code = run_source_candidate_probe(
        [SOURCE_URL],
        contract_metadata=[POST_DECLARATION],
        ledger_path=tmp_path / "ledger.json",
        opener=_RecordingTransport([_FakeResponse(403)]),
    )

    assert code == 1
    assert report["candidates"][0]["outcome"] == "reject"


def test_a_post_route_that_redirects_to_another_target_is_rejected(tmp_path: Path) -> None:
    elsewhere = _FakeResponse(200, _page(), url="https://elsewhere.test/other")
    report, code = run_source_candidate_probe(
        [SOURCE_URL],
        contract_metadata=[POST_DECLARATION],
        ledger_path=tmp_path / "ledger.json",
        opener=_RecordingTransport([elsewhere]),
    )

    result = report["candidates"][0]
    assert code == 1
    assert result["outcome"] == "reject"
    assert result["reason_code"] == "unrelated_redirect"
