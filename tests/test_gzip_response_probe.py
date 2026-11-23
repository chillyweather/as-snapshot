"""Regression tests for a compressed probe response body.

The probe asks for ``Accept-Encoding: identity``, but a caching proxy can
still answer with a gzip body. Before this fix the compressed bytes decoded
into replacement characters, so a healthy page was rejected with the
misleading reason ``missing_heading``.
"""

from __future__ import annotations

import gzip
import zlib
from email.message import Message
from pathlib import Path
from typing import Any

from autoscout.http_decoding import MAX_RESPONSE_BYTES
from autoscout.http_decoding import decode_bounded_http_body
from autoscout.source_page_identity import parse_heading_texts
from autoscout.source_probe import _read_bounded
from autoscout.source_probe import run_source_candidate_probe

PAGE = b"<html><body><h1>Current list</h1><p>Body text</p></body></html>"


class _CompressedResponse:
    def __init__(self, body: bytes, content_encoding: str, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.url = "https://example.test/compressed"
        self.headers = Message()
        self.headers["Content-Type"] = "text/html; charset=UTF-8"
        if content_encoding:
            self.headers["Content-Encoding"] = content_encoding

    def geturl(self) -> str:
        return self.url

    def read(self, size: int) -> bytes:
        return self.body[:size]

    def close(self) -> None:
        return None


class _ProbeTransport:
    def __init__(self, responses: list[_CompressedResponse]) -> None:
        self.responses = list(responses)

    def __call__(self, _request: Any, timeout: float) -> _CompressedResponse:
        return self.responses.pop(0)


def _read(body: bytes, content_encoding: str) -> tuple[str, int, bool]:
    return _read_bounded(_CompressedResponse(body, content_encoding))


def test_gzip_body_is_decompressed_before_parsing() -> None:
    text, byte_count, truncated = _read(gzip.compress(PAGE), "gzip")
    assert text == PAGE.decode()
    assert byte_count == len(PAGE)
    assert truncated is False
    assert parse_heading_texts(text) == ["Current list"]


def test_x_gzip_alias_is_decompressed() -> None:
    text, _, _ = _read(gzip.compress(PAGE), "x-gzip")
    assert parse_heading_texts(text) == ["Current list"]


def test_content_encoding_is_matched_case_insensitively() -> None:
    text, _, _ = _read(gzip.compress(PAGE), "  GZIP  ")
    assert parse_heading_texts(text) == ["Current list"]


def test_raw_deflate_body_is_decompressed() -> None:
    compressor = zlib.compressobj(wbits=-15)
    body = compressor.compress(PAGE) + compressor.flush()
    text, _, _ = _read(body, "deflate")
    assert parse_heading_texts(text) == ["Current list"]


def test_zlib_wrapped_deflate_body_is_decompressed() -> None:
    text, _, _ = _read(zlib.compress(PAGE), "deflate")
    assert parse_heading_texts(text) == ["Current list"]


def test_identity_body_is_unchanged() -> None:
    text, byte_count, truncated = _read(PAGE, "")
    assert text == PAGE.decode()
    assert byte_count == len(PAGE)
    assert truncated is False


def test_unknown_content_encoding_keeps_the_raw_body() -> None:
    text, _, _ = _read(PAGE, "br")
    assert text == PAGE.decode()


def test_malformed_gzip_body_returns_an_explicit_decode_failure() -> None:
    decoded = decode_bounded_http_body(_CompressedResponse(b"not really gzip at all", "gzip"), MAX_RESPONSE_BYTES)

    assert decoded.text == ""
    assert decoded.decode_failure


def test_incomplete_gzip_body_returns_an_explicit_decode_failure() -> None:
    body = gzip.compress(b"<html><h1>Current list</h1>" + b"<p>filler</p>" * 4000)
    decoded = decode_bounded_http_body(_CompressedResponse(body[: len(body) // 2], "gzip"), MAX_RESPONSE_BYTES)

    assert decoded.text == ""
    assert decoded.decode_failure


def test_bounded_reader_never_exposes_an_incomplete_decoded_prefix() -> None:
    body = gzip.compress(b"<html><h1>Current list</h1>" + b"<p>filler</p>" * 4000)
    text, _, truncated = _read(body[: len(body) // 2], "gzip")

    assert text == ""
    assert truncated is True
    assert parse_heading_texts(text) == []


def test_decompressed_body_is_still_bounded() -> None:
    body = gzip.compress(b"<html><h1>Current list</h1>" + b"a" * (MAX_RESPONSE_BYTES * 3))
    text, byte_count, truncated = _read(body, "gzip")
    assert byte_count <= MAX_RESPONSE_BYTES
    assert truncated is True


def test_response_without_headers_is_treated_as_identity() -> None:
    class _NoHeaders:
        def read(self, size: int) -> bytes:
            return PAGE[:size]

    response: Any = _NoHeaders()
    text, _, _ = _read_bounded(response)
    assert text == PAGE.decode()


def test_probe_refuses_an_incomplete_gzip_response_before_page_parsing(tmp_path: Path) -> None:
    body = gzip.compress(PAGE + b"<p>filler</p>" * 1000)
    transport = _ProbeTransport([_CompressedResponse(b"", ""), _CompressedResponse(body[: len(body) // 2], "gzip")])

    report, code = run_source_candidate_probe(
        ["https://example.test/compressed"],
        ledger_path=tmp_path / "ledger.json",
        opener=transport,
    )

    result = report["candidates"][0]
    assert code == 1
    assert result["reason_code"] == "get_request_failed"
    assert "decoding failed" in result["reason"]
