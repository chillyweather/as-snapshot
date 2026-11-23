"""A ranking fragment carried inside a JSON response envelope.

The Boklista form route answers ``{"html": "...", "post_count": 26}``. The
escaped fragment inside ``html`` is not parseable as markup until it is
unwrapped, so a source may declare which field holds it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from autoscout.ranking_response import REASON_RESPONSE_FIELD_MISSING
from autoscout.ranking_response import REASON_RESPONSE_FIELD_NOT_TEXT
from autoscout.ranking_response import REASON_RESPONSE_NOT_JSON
from autoscout.ranking_response import ranking_response_field
from autoscout.ranking_response import unwrap_ranking_response
from autoscout.ranking_response import validate_ranking_response_declaration
from autoscout.source_probe import run_source_candidate_probe

SOURCE_URL = "https://example.test/admin-ajax.php"
DECLARED: dict[str, Any] = {"ranking_response_html_field": "html"}

PAGE = (
    "<html><body><h1>Current list</h1><ul>"
    f'<li><a href="{SOURCE_URL}/book-1">Book 1</a><span class="author">By: Jane Author</span></li>'
    f'<li><a href="{SOURCE_URL}/book-2">Book 2</a><span class="author">By: Jane Author</span></li>'
    "</ul></body></html>"
)


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


class _Transport:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = list(responses)

    def __call__(self, request: Any, timeout: float) -> _FakeResponse:
        if not self.responses:
            raise AssertionError("unexpected extra network call")
        return self.responses.pop(0)


# -- Declaration ------------------------------------------------------------


def test_no_declaration_returns_the_body_unchanged() -> None:
    result = unwrap_ranking_response("<html>rows</html>", {})
    assert result.ok is True
    assert result.html == "<html>rows</html>"
    assert ranking_response_field({}) == ""


def test_a_declared_field_is_unwrapped() -> None:
    body = json.dumps({"html": PAGE, "post_count": 26, "last": False})
    result = unwrap_ranking_response(body, DECLARED)
    assert result.ok is True
    assert result.html == PAGE


def test_escaped_markup_survives_the_unwrap_intact() -> None:
    fragment = '<div class="book-row"><h3 class="book-title">Middag under 50-lappen</h3></div>'
    result = unwrap_ranking_response(json.dumps({"html": fragment}), DECLARED)
    assert result.html == fragment
    assert "\\" not in result.html


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (7, "ranking_response_html_field must be a string"),
        ("  ", "ranking_response_html_field must be a nonempty field name"),
        (" html", "ranking_response_html_field must not have leading or trailing whitespace"),
    ],
)
def test_a_malformed_declaration_is_rejected(value: Any, expected: str) -> None:
    assert validate_ranking_response_declaration({"ranking_response_html_field": value}) == expected


def test_an_absent_declaration_is_valid() -> None:
    assert validate_ranking_response_declaration({}) == ""


# -- Failing closed ---------------------------------------------------------


def test_a_non_json_body_refuses_rather_than_falling_back_to_the_raw_body() -> None:
    result = unwrap_ranking_response(PAGE, DECLARED)
    assert result.ok is False
    assert result.reason_code == REASON_RESPONSE_NOT_JSON
    assert result.html == ""


def test_a_json_array_body_is_refused() -> None:
    result = unwrap_ranking_response("[1, 2, 3]", DECLARED)
    assert result.reason_code == REASON_RESPONSE_NOT_JSON


def test_an_envelope_without_the_declared_field_is_refused() -> None:
    result = unwrap_ranking_response(json.dumps({"markup": PAGE}), DECLARED)
    assert result.reason_code == REASON_RESPONSE_FIELD_MISSING
    assert "'html'" in result.reason


def test_a_declared_field_that_is_not_text_is_refused() -> None:
    result = unwrap_ranking_response(json.dumps({"html": {"nested": PAGE}}), DECLARED)
    assert result.reason_code == REASON_RESPONSE_FIELD_NOT_TEXT


def test_no_other_field_is_searched_for_markup() -> None:
    # Only the declared field is read; a lookalike neighbour is never used.
    result = unwrap_ranking_response(json.dumps({"body": PAGE, "html": None}), DECLARED)
    assert result.reason_code == REASON_RESPONSE_FIELD_NOT_TEXT


# -- Probe behaviour --------------------------------------------------------


def test_a_declared_envelope_is_unwrapped_before_the_probe_judges_the_rows(tmp_path: Path) -> None:
    body = json.dumps({"html": PAGE, "post_count": 2}).encode()
    report, code = run_source_candidate_probe(
        [SOURCE_URL],
        contract_metadata=[DECLARED],
        ledger_path=tmp_path / "ledger.json",
        opener=_Transport([_FakeResponse(200), _FakeResponse(200, body)]),
    )

    result = report["candidates"][0]
    assert code == 0
    assert result["outcome"] == "accept"
    assert result["bounded_entry_count"] == 2


def test_a_probe_envelope_failure_is_its_own_reason_code(tmp_path: Path) -> None:
    report, code = run_source_candidate_probe(
        [SOURCE_URL],
        contract_metadata=[DECLARED],
        ledger_path=tmp_path / "ledger.json",
        opener=_Transport([_FakeResponse(200), _FakeResponse(200, PAGE.encode())]),
    )

    result = report["candidates"][0]
    assert code == 1
    assert result["outcome"] == "reject"
    assert result["reason_code"] == "response_envelope"


def test_a_source_without_the_declaration_is_judged_on_the_whole_body(tmp_path: Path) -> None:
    report, code = run_source_candidate_probe(
        [SOURCE_URL],
        ledger_path=tmp_path / "ledger.json",
        opener=_Transport([_FakeResponse(200), _FakeResponse(200, PAGE.encode())]),
    )

    assert code == 0
    assert report["candidates"][0]["outcome"] == "accept"
