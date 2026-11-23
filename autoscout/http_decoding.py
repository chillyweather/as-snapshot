"""Bounded decoding for HTTP response bodies."""

from __future__ import annotations

import codecs
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# The shared hard bound for one HTTP response body in UTF-8 bytes.
MAX_RESPONSE_BYTES = 1_000_000
# One bounded body is read in slices of at most this size when the caller
# carries a deadline, so a server that keeps sending has to meet that deadline
# rather than only the per-receive socket timeout.
READ_SLICE_BYTES = 65_536
# A socket timeout of zero means non-blocking rather than "give up at once",
# so a socket narrowed to the very end of a deadline keeps this much.
MIN_SOCKET_TIMEOUT_SECONDS = 0.001


@dataclass(frozen=True)
class BoundedHttpBody:
    """One bounded response body and its decoding outcome."""

    text: str = ""
    byte_count: int = 0
    truncated: bool = False
    decode_failure: str = ""
    compressed: bool = False
    # The caller's deadline passed while the body was being read, or had passed
    # by the time it arrived.  Nothing was decoded: an overdue body is not
    # evidence, and the caller fails its source closed instead of publishing it.
    timed_out: bool = False


def _charset(response: Any) -> str:
    headers = getattr(response, "headers", None)
    if headers is None:
        return "utf-8"
    get_charset = getattr(headers, "get_content_charset", None)
    if callable(get_charset):
        try:
            return str(get_charset() or "utf-8")
        except Exception:
            return "utf-8"
    getter = getattr(headers, "get", None)
    content_type = str(getter("Content-Type", "") or "") if callable(getter) else ""
    for part in content_type.split(";")[1:]:
        name, separator, value = part.partition("=")
        if separator and name.strip().casefold() == "charset":
            return value.strip().strip('"') or "utf-8"
    return "utf-8"


def _content_encoding(response: Any) -> str:
    headers = getattr(response, "headers", None)
    getter = getattr(headers, "get", None) if headers is not None else None
    if not callable(getter):
        return ""
    return str(getter("Content-Encoding", "") or "").split(",", 1)[0].strip().casefold()


def _bounded_text(value: bytes, limit: int, charset: str) -> tuple[str, int, bool]:
    text = value.decode(charset, errors="replace")
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text, len(encoded), False
    retained: list[str] = []
    byte_count = 0
    for character in text:
        character_bytes = len(character.encode("utf-8", errors="replace"))
        if byte_count + character_bytes > limit:
            break
        retained.append(character)
        byte_count += character_bytes
    return "".join(retained), byte_count, True


def _decode_compressed(encoded: bytes, encoding: str, limit: int) -> tuple[bytes, bool, str]:
    window_bits = 31 if encoding in {"gzip", "x-gzip"} else -15
    decompressor = zlib.decompressobj(window_bits)
    try:
        decoded = decompressor.decompress(encoded, limit + 1)
    except zlib.error as exc:
        if encoding == "deflate":
            try:
                decompressor = zlib.decompressobj(15)
                decoded = decompressor.decompress(encoded, limit + 1)
            except zlib.error as fallback_exc:
                return b"", False, f"malformed {encoding} content: {fallback_exc}"
        else:
            return b"", False, f"malformed {encoding} content: {exc}"
    if len(decoded) > limit or decompressor.unconsumed_tail:
        return decoded[:limit], True, ""
    if not decompressor.eof:
        return decoded, True, f"incomplete {encoding} content"
    try:
        decoded += decompressor.flush(limit + 1 - len(decoded))
    except zlib.error as exc:
        return b"", False, f"malformed {encoding} content: {exc}"
    if len(decoded) > limit:
        return decoded[:limit], True, ""
    return decoded, False, ""


def _narrow_socket_timeout(response: Any, seconds: float) -> None:
    """Hold the next blocking read to the time the caller has left.

    A socket keeps the timeout it was given when the request was made, so a
    slice that arrives just before a deadline can be followed by a read that
    blocks for that whole original timeout - the deadline check between slices
    cannot see a read that has already begun.  Narrowing the socket first is
    what makes the deadline the bound instead of the deadline plus one
    timeout.

    This only ever narrows.  The timeout the request was made with is the
    bound on one read, and the deadline is the bound on the whole body, so a
    caller whose deadline is the wider of the two - a detail route with
    minutes of allowance left, reading a page fetched under a ten-second
    clamp - must not have its socket widened to that allowance: one stalled
    read would then spend the source's entire remaining clock.  Taking the
    smaller of the two keeps each read bounded by the clamp and the body
    bounded by the deadline.

    ``http.client.HTTPResponse`` keeps its socket private, so this walks the
    one construction it documents (``sock.makefile("rb")``) and does nothing
    when it finds no socket: the between-slice check is then still the bound,
    one read wider.
    """
    raw = getattr(getattr(response, "fp", None), "raw", None)
    sock = getattr(raw, "_sock", None)
    settimeout = getattr(sock, "settimeout", None)
    if not callable(settimeout):
        return
    gettimeout = getattr(sock, "gettimeout", None)
    current = gettimeout() if callable(gettimeout) else None
    if isinstance(current, int | float) and current > 0:
        seconds = min(seconds, float(current))
    try:
        settimeout(max(seconds, MIN_SOCKET_TIMEOUT_SECONDS))
    except OSError:
        # A socket already closed by the peer cannot be narrowed, and does not
        # need to be: its next read returns rather than blocks.
        return


def read_bounded_http_bytes(
    response: Any, limit: int, remaining_seconds: Callable[[], float] | None
) -> tuple[Any, bool]:
    """Read at most *limit* bytes, giving up once the caller's deadline passes.

    A socket timeout bounds one receive, not a whole body: a server that keeps
    sending data can hold a single ``read`` open indefinitely.  When the caller
    carries a deadline and the response can be read incrementally - which
    ``http.client.HTTPResponse`` can - the body is read in bounded slices, with
    the time left checked before each one and the socket narrowed to it, so
    neither the body nor a stalled read outlives the deadline.

    A response that offers only a whole-body ``read`` keeps exactly the single
    bounded read it always had, under the timeout the request was made with;
    the deadline is then applied to what arrived, which is reported through the
    returned flag.
    """
    if remaining_seconds is None:
        return response.read(limit), False
    read_slice = getattr(response, "read1", None)
    if not callable(read_slice):
        return response.read(limit), remaining_seconds() <= 0
    slices: list[bytes] = []
    read_bytes = 0
    while read_bytes < limit:
        remaining = remaining_seconds()
        if remaining <= 0:
            return b"", True
        _narrow_socket_timeout(response, remaining)
        body_slice = read_slice(min(READ_SLICE_BYTES, limit - read_bytes))
        if not body_slice:
            break
        slices.append(bytes(body_slice))
        read_bytes += len(body_slice)
    return b"".join(slices), remaining_seconds() <= 0


def decode_bounded_http_body(
    response: Any, max_bytes: int, *, remaining_seconds: Callable[[], float] | None = None
) -> BoundedHttpBody:
    """Read, decode, and bound one HTTP response without accepting partial HTML."""
    if max_bytes < 1:
        raise ValueError("HTTP body bound must be positive")
    raw, deadline_passed = read_bounded_http_bytes(response, max_bytes + 1, remaining_seconds)
    if deadline_passed:
        return BoundedHttpBody(timed_out=True)
    if isinstance(raw, str):
        encoded = raw.encode("utf-8", errors="replace")
        charset = "utf-8"
        compressed = False
        raw_truncated = len(encoded) > max_bytes
        decoded = encoded[:max_bytes]
        failure = ""
    else:
        encoded = bytes(raw)
        charset = _charset(response)
        encoding = _content_encoding(response)
        if not encoding and encoded.startswith(b"\x1f\x8b"):
            encoding = "gzip"
        compressed = encoding in {"gzip", "x-gzip", "deflate"}
        raw_truncated = len(encoded) > max_bytes
        bounded_encoded = encoded[:max_bytes]
        if compressed:
            decoded, decompressed_truncated, failure = _decode_compressed(bounded_encoded, encoding, max_bytes)
            raw_truncated = raw_truncated or decompressed_truncated
        else:
            decoded = bounded_encoded
            failure = ""
    if failure:
        return BoundedHttpBody(
            truncated=True if raw_truncated or compressed else False,
            decode_failure=failure,
            compressed=compressed,
        )
    text, byte_count, text_truncated = _bounded_text(decoded, max_bytes, charset)
    return BoundedHttpBody(
        text=text,
        byte_count=byte_count,
        truncated=raw_truncated or (text_truncated and compressed),
        compressed=compressed,
    )


@dataclass(frozen=True)
class StreamedHttpBody:
    """What streaming one response body found; the text itself went to the caller's sink."""

    byte_count: int = 0
    truncated: bool = False
    decode_failure: str = ""
    compressed: bool = False
    timed_out: bool = False


def _is_zlib_wrapped(head: bytes) -> bool:
    """A deflate body carries the zlib header (RFC 1950) or none at all (raw RFC 1951)."""
    return len(head) >= 2 and head[0] & 0x0F == 8 and (head[0] << 8 | head[1]) % 31 == 0


class _TextSink:
    """Decoded text handed on to the caller, held to the ceiling in UTF-8 bytes."""

    def __init__(self, charset: str, ceiling: int, consume: Callable[[str], None]) -> None:
        try:
            self.decoder = codecs.getincrementaldecoder(charset)(errors="replace")
        except LookupError:
            self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.ceiling = ceiling
        self.consume = consume
        self.byte_count = 0

    def take(self, data: bytes, *, final: bool = False) -> bool:
        """Decode *data* and hand it on; False once the text crossed the ceiling (that slice is withheld)."""
        text = self.decoder.decode(data, final=final)
        if not text:
            return True
        self.byte_count += len(text.encode("utf-8", errors="replace"))
        if self.byte_count > self.ceiling:
            return False
        self.consume(text)
        return True


class _Inflater:
    """A compressed body inflated one bounded step at a time.

    A deflate body's framing is chosen from two bytes that both framings can
    begin with, so until the first output the input is kept and the other
    framing is tried on it once, as the whole-body decoder does.
    """

    def __init__(self, encoding: str, head: bytes) -> None:
        self.encoding = encoding
        if encoding == "deflate":
            framing = 15 if _is_zlib_wrapped(head) else -15
            self.other_framing: int | None = -framing
        else:
            framing = 31
            self.other_framing = None
        self.inflater = zlib.decompressobj(framing)
        self.kept_input = b""
        self.inflated = False

    @property
    def eof(self) -> bool:
        return bool(self.inflater.eof)

    def take(self, data: bytes, sink: _TextSink) -> bool:
        """Inflate *data* into *sink*; False once the text crossed the ceiling. Raises zlib.error."""
        if self.other_framing is not None and not self.inflated:
            self.kept_input += data
        try:
            complete = self._inflate(data, sink)
        except zlib.error:
            if self.other_framing is None or self.inflated:
                raise
            self.inflater = zlib.decompressobj(self.other_framing)
            self.other_framing = None
            complete = self._inflate(self.kept_input, sink)
        if self.inflated:
            self.other_framing, self.kept_input = None, b""
        return complete

    def _inflate(self, data: bytes, sink: _TextSink) -> bool:
        pending = data
        while True:
            chunk = self.inflater.decompress(pending, READ_SLICE_BYTES)
            pending = self.inflater.unconsumed_tail
            self.inflated = self.inflated or bool(chunk)
            if not sink.take(chunk):
                return False
            # A step that filled its limit may leave output inside the inflater with no input left.
            if not pending and len(chunk) < READ_SLICE_BYTES:
                return True


def _slice_reader(response: Any, ceiling: int) -> Callable[[], Any]:
    read_slice = getattr(response, "read1", None)
    if callable(read_slice):
        return lambda: read_slice(READ_SLICE_BYTES)
    # As in read_bounded_http_bytes: a response that offers only a whole-body
    # read gets one bounded read, then is handed on like any other.
    whole = [response.read(ceiling + 1)]
    return lambda: whole.pop() if whole else b""


def stream_http_text(
    response: Any,
    ceiling: int,
    consume: Callable[[str], None],
    *,
    remaining_seconds: Callable[[], float] | None = None,
) -> StreamedHttpBody:
    """Decode one response in slices and hand each decoded slice to *consume*, keeping none of it.

    The memory a body costs is one slice, whatever its length, so a caller that
    needs only something the page says can read a page of any size.  *ceiling*
    bounds the decoded text in UTF-8 bytes against a response that never ends;
    the slice that crosses it is not handed on and the result is truncated.  The
    bytes received are held to the ceiling too, so a compressed body that keeps
    arriving without adding text still ends.  A caller's deadline is checked
    before each slice, as in ``read_bounded_http_bytes``; a response with no
    ``read1`` is read once, bounded by the ceiling, as there.  A compressed body
    is inflated slice by slice, so it cannot expand past the ceiling in one step.
    """
    if ceiling < 1:
        raise ValueError("HTTP body bound must be positive")
    sink = _TextSink(_charset(response), ceiling, consume)
    encoding = _content_encoding(response)
    read = _slice_reader(response, ceiling)
    inflater: _Inflater | None = None
    head: bytes | None = b""  # the first bytes, until the body's format is known
    text_body = False
    received = 0

    def cut_short(decode_failure: str = "") -> StreamedHttpBody:
        compressed = inflater is not None
        return StreamedHttpBody(
            byte_count=0 if decode_failure else sink.byte_count,
            truncated=True,
            decode_failure=decode_failure,
            compressed=compressed,
        )

    def begin(first: bytes) -> None:
        nonlocal encoding, inflater, sink
        if not encoding and first.startswith(b"\x1f\x8b") and not text_body:
            encoding = "gzip"
        if encoding in {"gzip", "x-gzip", "deflate"}:
            inflater = _Inflater(encoding, first)
        if text_body:
            sink = _TextSink("utf-8", ceiling, consume)

    def take(data: bytes, *, final: bool = False) -> StreamedHttpBody | None:
        try:
            if final:
                return None if sink.take(b"", final=True) else cut_short()
            if inflater is None:
                return None if sink.take(data) else cut_short()
            return None if inflater.take(data, sink) else cut_short()
        except zlib.error as exc:
            return cut_short(f"malformed {encoding} content: {exc}")
        except UnicodeError as exc:
            # A UTF-16 or UTF-32 body with no byte-order mark fails even under errors="replace".
            return cut_short(f"undecodable text: {exc}")

    while inflater is None or not inflater.eof:
        if remaining_seconds is not None:
            remaining = remaining_seconds()
            if remaining <= 0:
                return StreamedHttpBody(timed_out=True)
            _narrow_socket_timeout(response, remaining)
        body_slice = read()
        if not body_slice:
            break
        text_body = isinstance(body_slice, str)
        raw = body_slice.encode("utf-8", errors="replace") if text_body else bytes(body_slice)
        received += len(raw)
        if received > ceiling:
            return cut_short()
        if head is not None:
            head += raw
            # Both the gzip magic and the zlib header are two bytes long.
            if len(head) < 2:
                continue
            raw, head = head, None
            begin(raw)
        failed = take(raw)
        if failed is not None:
            return failed
    if head:
        begin(head)
        failed = take(head)
        if failed is not None:
            return failed
    elif head is not None and encoding in {"gzip", "x-gzip", "deflate"}:
        # An empty body that declares compression is an incomplete one, as for the whole-body decoder.
        return StreamedHttpBody(truncated=True, decode_failure=f"incomplete {encoding} content", compressed=True)
    if remaining_seconds is not None and remaining_seconds() <= 0:
        return StreamedHttpBody(timed_out=True)
    if inflater is not None and not inflater.eof:
        return cut_short(f"incomplete {encoding} content")
    failed = take(b"", final=True)
    if failed is not None:
        return failed
    return StreamedHttpBody(byte_count=sink.byte_count, compressed=inflater is not None)
