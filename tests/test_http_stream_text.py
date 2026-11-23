"""stream_http_text: a body handed on in slices, never held whole, under a ceiling and a deadline."""

import gzip
import zlib

import pytest

from autoscout.http_decoding import READ_SLICE_BYTES
from autoscout.http_decoding import stream_http_text


class Response:
    def __init__(self, body, *, encoding="", charset="utf-8", slice_bytes=None):
        self.headers = {"Content-Type": f"text/html; charset={charset}"}
        if encoding:
            self.headers["Content-Encoding"] = encoding
        self._body = body
        self._offset = 0
        self._slice_bytes = slice_bytes
        self.reads = []

    def read1(self, count):
        if self._slice_bytes is not None:
            count = min(count, self._slice_bytes)
        piece = self._body[self._offset : self._offset + count]
        self._offset += len(piece)
        self.reads.append(len(piece))
        return piece


TEXT = "<p>Привет, мир</p>" * 20_000  # multi-byte characters across slice boundaries


def streamed(response, ceiling=10_000_000, **kwargs):
    pieces = []
    result = stream_http_text(response, ceiling, pieces.append, **kwargs)
    return result, "".join(pieces), pieces


def test_plain_body_is_handed_on_in_slices():
    response = Response(TEXT.encode())
    result, text, pieces = streamed(response)
    assert text == TEXT
    assert result.byte_count == len(TEXT.encode())
    assert not result.truncated and not result.compressed
    assert len(pieces) > 1 and max(response.reads) <= READ_SLICE_BYTES


def test_multibyte_character_split_between_reads_is_decoded_whole():
    result, text, _ = streamed(Response(TEXT.encode(), slice_bytes=3))
    assert text == TEXT and not result.truncated


def test_declared_charset_is_used():
    result, text, _ = streamed(Response(TEXT.encode("cp1251"), charset="windows-1251"))
    assert text == TEXT and not result.truncated


@pytest.mark.parametrize(
    "body,encoding",
    [
        (gzip.compress(TEXT.encode(), mtime=0), "gzip"),
        (gzip.compress(TEXT.encode(), mtime=0), ""),  # gzip recognised by its magic bytes
        (zlib.compress(TEXT.encode()), "deflate"),
        (zlib.compress(TEXT.encode())[2:-4], "deflate"),  # raw deflate, no zlib header
    ],
)
def test_compressed_body_is_inflated_slice_by_slice(body, encoding):
    result, text, pieces = streamed(Response(body, encoding=encoding))
    assert text == TEXT and result.compressed and not result.truncated
    assert max(len(p.encode()) for p in pieces) <= READ_SLICE_BYTES * 4


def test_text_past_the_ceiling_is_truncated_and_the_crossing_slice_is_withheld():
    result, text, _ = streamed(Response(TEXT.encode()), ceiling=100_000)
    assert result.truncated
    assert len(text.encode()) <= 100_000


def test_small_compressed_body_cannot_expand_past_the_ceiling():
    bomb = gzip.compress(b"x" * 20_000_000, mtime=0)
    result, text, _ = streamed(Response(bomb, encoding="gzip"), ceiling=1_000_000)
    assert result.truncated and result.compressed
    assert len(text) <= 1_000_000


def test_malformed_and_incomplete_compressed_bodies_fail():
    malformed, _, _ = streamed(Response(b"\x1f\x8bnot gzip at all", encoding="gzip"))
    assert malformed.truncated and malformed.decode_failure.startswith("malformed gzip")
    incomplete, _, _ = streamed(Response(gzip.compress(TEXT.encode(), mtime=0)[:-20], encoding="gzip"))
    assert incomplete.truncated and incomplete.decode_failure == "incomplete gzip content"


def test_passed_deadline_stops_the_read():
    clock = iter([5.0, 0.0])
    result, _, _ = streamed(Response(TEXT.encode()), remaining_seconds=lambda: next(clock, 0.0))
    assert result.timed_out


def test_response_without_read1_gets_one_bounded_read():
    class WholeBody:
        def __init__(self):
            self.headers = {"Content-Type": "text/html; charset=utf-8"}
            self.calls = []

        def read(self, count):
            self.calls.append(count)
            return TEXT.encode()[:count]

    response = WholeBody()
    result, text, _ = streamed(response, ceiling=50_000)
    assert response.calls == [50_001]
    assert result.truncated and len(text.encode()) <= 50_000


@pytest.mark.parametrize(
    "body,encoding",
    [
        (zlib.compress(TEXT.encode()), "deflate"),  # the zlib header split by a one-byte first read
        (gzip.compress(TEXT.encode(), mtime=0), ""),  # the gzip magic split the same way
    ],
)
def test_compression_header_split_across_reads_is_recognised(body, encoding):
    class OneByteFirst(Response):
        def read1(self, count):
            return super().read1(1 if self._offset == 0 else count)

    result, text, _ = streamed(OneByteFirst(body, encoding=encoding))
    assert text == TEXT and result.compressed and not result.truncated


def test_one_byte_body_is_handed_on():
    result, text, _ = streamed(Response(b"x"))
    assert text == "x" and not result.truncated


@pytest.mark.parametrize("encoding", ["deflate", "gzip"])
@pytest.mark.parametrize("size", [READ_SLICE_BYTES, READ_SLICE_BYTES + 1, 4 * READ_SLICE_BYTES + 1])
def test_output_held_inside_the_inflater_is_drained(encoding, size):
    plain = b"x" * size
    body = zlib.compress(plain)[2:-4] if encoding == "deflate" else gzip.compress(plain, mtime=0)
    result, text, _ = streamed(Response(body, encoding=encoding))
    assert not result.truncated and not result.decode_failure
    assert text == plain.decode()


def test_compressed_body_that_adds_no_text_ends_at_the_ceiling():
    # Empty stored deflate blocks, sent without end: the text never grows.
    endless = b"\x00\x00\x00\xff\xff" * 400_000
    result, text, _ = streamed(Response(endless, encoding="deflate"), ceiling=1_000_000)
    assert result.truncated and text == ""


def test_raw_deflate_that_looks_zlib_wrapped_falls_back():
    body = bytes.fromhex("780100feff610300")
    assert zlib.decompress(body, -15) == b"a"
    result, text, _ = streamed(Response(body, encoding="deflate"))
    assert text == "a" and not result.truncated and not result.decode_failure


def test_deflate_framing_falls_back_across_a_split_first_read():
    class OneByteReads(Response):
        def read1(self, count):
            return super().read1(1)

    body = bytes.fromhex("780100feff610300")
    result, text, _ = streamed(OneByteReads(body, encoding="deflate"))
    assert text == "a" and not result.decode_failure


def test_bomless_utf16_body_is_a_decode_failure_not_an_exception():
    result, _, _ = streamed(Response("<p>a</p>".encode("utf-16-le"), charset="utf-16"))
    assert result.truncated and result.decode_failure.startswith("undecodable text")


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
def test_empty_body_that_declares_compression_is_incomplete(encoding):
    result, text, _ = streamed(Response(b"", encoding=encoding))
    assert text == "" and result.truncated and result.decode_failure == f"incomplete {encoding} content"
