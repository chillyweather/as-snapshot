"""Loopback search adapter for the grounded description stage (#457, #468).

`HTTPDiscoveryProvider` performs one bounded GET, sends no custom header, and
requires a JSON body shaped ``{"results": [{"url": ...}]}``.

No commercial search API satisfies both conditions.  Brave needs an
``X-Subscription-Token`` header and nests results under ``web.results``.  Exa
and Tavily need a POST.  Serper and Google Custom Search use a different
results key.

This adapter is the seam between the two.  It accepts the GET that AutoScout
can make, calls the vendor the way the vendor requires, and returns the shape
AutoScout can read.

The credential never enters AutoScout.  A URL reaches logs, run artifacts, and
the live progress snapshot, so a key placed in a discovery endpoint template
would depend on redaction to stay secret.  Here the key is read from the
environment by this process alone and is never echoed, logged, or returned.

The listener binds to the loopback interface only.

The adapter validates an optional ISO 639-1 ``search_lang`` parameter and translates it
into Brave's own language vocabulary before passing it on (#707).
The adapter applies a shared server-side vendor floor before every Brave call.
AutoScout also applies its client-side stage limiter so candidate timing stays
accurate and all discovery callers share the same client-side queue.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer
from typing import Any
from typing import ClassVar
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import parse_qs
from urllib.parse import quote_plus
from urllib.parse import urlsplit
from urllib.request import Request
from urllib.request import urlopen

from autoscout.language_codes import normalize_language_code
from autoscout.vendor_rate_limit import MIN_VENDOR_INTERVAL_SECONDS
from autoscout.vendor_rate_limit import VendorRateLimiter

API_KEY_ENV = "AUTOSCOUT_SEARCH_API_KEY"
BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8899
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

REQUEST_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 1_000_000

# Measured on 2026-08-15, the first live query, for `All That's Unseen` by
# Emilee Hackney.  Retail listings and the publisher page took positions 1, 2,
# 3 and 10, and every one of them carries the same publisher blurb, so they
# collapse to one owner.  The Library Journal review, the only source that says
# what the book is actually about, came 8th.
#
# At 10 the margin was two positions.  At 5 this book would have reached the
# single-source outcome carrying marketing copy, and the wrong conclusion would
# have been that trade reviews rank too low to use.
#
# A larger count costs nothing: the same single request and the same quota.
MAX_RESULTS = 20


# Brave's ``search_lang`` is its own vocabulary, not ISO 639-1.  This is the
# ``Language-Input`` enum of the Brave web search API reference, copied on
# 2026-09-16.  Brave answers any other value with HTTP 422, and the 2026-09-16
# weekly run lost every Portuguese candidate to a bare ``pt`` (issue #707).
BRAVE_SEARCH_LANGUAGES = frozenset(
    """
    ar eu bn bg ca zh-hans zh-hant hr cs da nl en en-gb et fi fr gl de el gu he hi hu is it ja jp kn ko
    lv lt ms ml mr nb pl pt-br pt-pt pa ro ru sr sk sl es sv ta te th tr uk vi
    """.split()
)

# ISO 639-1 codes Brave only accepts in a regional or differently named form.
# Portuguese maps to Brazil because the configured Portuguese sources are
# Brazilian; Chinese maps to Simplified; Norwegian ``no`` maps to Bokmål.
_BRAVE_SEARCH_LANGUAGE_ALIASES = {
    "pt": "pt-br",
    "zh": "zh-hans",
    "no": "nb",
}


def brave_search_language(value: object) -> str | None:
    """Translate an ISO 639-1 code into Brave's ``search_lang``, or ``None``.

    ``None`` means Brave has no value for this language, so the request must
    omit ``search_lang`` rather than send one Brave will reject.
    """
    normalized = normalize_language_code(value)
    if normalized is None:
        return None
    translated = _BRAVE_SEARCH_LANGUAGE_ALIASES.get(normalized, normalized)
    return translated if translated in BRAVE_SEARCH_LANGUAGES else None


class SearchAdapterError(RuntimeError):
    """A bounded failure that must never carry the credential."""


class SearchLanguageRejectedError(SearchAdapterError):
    """Brave refused a request that carried ``search_lang`` with HTTP 422."""


def brave_search(
    query: str,
    *,
    api_key: str,
    count: int = MAX_RESULTS,
    search_lang: str | None = None,
) -> list[dict[str, Any]]:
    """Call Brave and return its raw result list.

    Raises ``SearchAdapterError`` with a message that never includes the key.
    """
    url = f"{BRAVE_ENDPOINT}?q={quote_plus(query)}&count={int(count)}"
    vendor_language = brave_search_language(search_lang)
    if vendor_language:
        url += f"&search_lang={quote_plus(vendor_language)}"
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": api_key,
            "User-Agent": "AutoScout grounded-search-adapter/1",
        },
    )
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        message = f"search vendor returned HTTP {exc.code}"
        if exc.code == 422 and vendor_language:
            raise SearchLanguageRejectedError(message) from None
        raise SearchAdapterError(message) from None
    except (URLError, TimeoutError) as exc:  # pragma: no cover - network failure path
        raise SearchAdapterError(f"search vendor unreachable: {type(exc).__name__}") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise SearchAdapterError("search vendor response exceeded the byte ceiling")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SearchAdapterError("search vendor response was not JSON") from None
    web = payload.get("web") if isinstance(payload, dict) else None
    results = web.get("results") if isinstance(web, dict) else None
    return [item for item in results if isinstance(item, dict)] if isinstance(results, list) else []


def to_autoscout_shape(
    vendor_results: list[dict[str, Any]],
    *,
    limit: int = MAX_RESULTS,
    source_language: str | None = None,
) -> dict[str, Any]:
    """Translate vendor results into the shape HTTPDiscoveryProvider reads.

    Only the URL is authoritative.  A vendor title and description are passed
    through as discovery metadata, and the grounding gate treats them as a
    search snippet: never as description evidence, and never as identity
    evidence.  Classification and identity come from the fetched page.
    """
    results: list[dict[str, Any]] = []
    for item in vendor_results[:limit]:
        url = str(item.get("url", "") or "").strip()
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        result = {
            "url": url,
            "title": str(item.get("title", "") or "")[:300],
            "snippet": str(item.get("description", "") or "")[:500],
        }
        normalized_language = normalize_language_code(source_language)
        if normalized_language:
            result["source_language"] = normalized_language
        results.append(result)
    return {"results": results}


class _Handler(BaseHTTPRequestHandler):
    server_version = "AutoScoutSearchAdapter/1"
    rate_limiter: ClassVar[VendorRateLimiter] = VendorRateLimiter()

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            api_key = os.environ.get(API_KEY_ENV, "").strip()
            if not api_key:
                self._reply(503, {"error": f"{API_KEY_ENV} is not set"})
                return
            self._reply(200, {"status": "healthy"})
            return
        if parsed.path != "/search":
            self._reply(404, {"error": "unknown path"})
            return
        query = (parse_qs(parsed.query).get("q") or [""])[0].strip()
        if not query:
            self._reply(400, {"error": "missing q"})
            return
        api_key = os.environ.get(API_KEY_ENV, "").strip()
        if not api_key:
            self._reply(503, {"error": f"{API_KEY_ENV} is not set"})
            return
        try:
            self.rate_limiter.wait()
            raw_language = (parse_qs(parsed.query).get("search_lang") or [""])[0].strip()
            normalized_language = normalize_language_code(raw_language)
            brave_kwargs: dict[str, Any] = {"api_key": api_key}
            if normalized_language:
                brave_kwargs["search_lang"] = normalized_language
            try:
                vendor_results = brave_search(query, **brave_kwargs)
            except SearchLanguageRejectedError:
                # One unfiltered retry: a language Brave refuses must not cost
                # the candidate its whole search (issue #707).  The retry is a
                # second vendor call, so it waits for the shared floor again.
                self.rate_limiter.wait()
                vendor_results = brave_search(query, api_key=api_key)
            payload = to_autoscout_shape(
                vendor_results,
                source_language=normalized_language,
            )
        except SearchAdapterError as exc:
            self._reply(502, {"error": str(exc)})
            return
        self._reply(200, payload)

    def _reply(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Log the path only.

        The default handler logs the full request line.  A query string can
        carry a book title, and a future change could carry more, so nothing
        beyond the path is written.
        """
        del format, args
        print(f"grounded-search-adapter {self.command} {urlsplit(self.path).path}")


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Serve on the loopback interface only."""
    if host not in LOOPBACK_HOSTS:
        raise SearchAdapterError("the adapter binds to the loopback interface only")
    server = HTTPServer((host, port), _Handler)
    print(f"grounded-search-adapter listening on http://{host}:{port}/search")
    print(f"set AUTOSCOUT_GROUNDED_DISCOVERY_ENDPOINT=http://{host}:{port}/search?q={{query}}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    """Serve the loopback adapter. Bounded flags only."""
    import argparse

    parser = argparse.ArgumentParser(description="AutoScout grounded search adapter (loopback only).")
    parser.add_argument("--host", default=DEFAULT_HOST, choices=sorted(LOOPBACK_HOSTS))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    if not os.environ.get(API_KEY_ENV, "").strip():
        print(f"auto-scout: {API_KEY_ENV} is not set; the adapter will answer 503 until it is", flush=True)
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())


__all__ = [
    "API_KEY_ENV",
    "BRAVE_SEARCH_LANGUAGES",
    "MAX_RESULTS",
    "MIN_VENDOR_INTERVAL_SECONDS",
    "SearchAdapterError",
    "SearchLanguageRejectedError",
    "VendorRateLimiter",
    "brave_search",
    "brave_search_language",
    "main",
    "serve",
    "to_autoscout_shape",
]
