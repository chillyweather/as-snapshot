"""Fetch a configured source page live: bounded requests, ranking paging, identity and detail pages."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Mapping
from datetime import date
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request

from autoscout.capture_deadline import CaptureDeadline
from autoscout.capture_deadline import capture_deadline_for
from autoscout.http_decoding import decode_bounded_http_body
from autoscout.http_decoding import stream_http_text
from autoscout.ranking_detail_route import RankingDetailRoute
from autoscout.ranking_detail_route import ranking_detail_scope
from autoscout.ranking_identity import identity_heading_matches
from autoscout.ranking_identity import ranking_identity_declared
from autoscout.ranking_identity import ranking_identity_route
from autoscout.ranking_identity import same_origin_url
from autoscout.ranking_identity import validate_ranking_identity_declaration
from autoscout.ranking_paging import PAGING_EMPTY
from autoscout.ranking_paging import PAGING_MISSING_FINAL
from autoscout.ranking_paging import PAGING_REPEATED
from autoscout.ranking_paging import PAGING_TOO_LARGE
from autoscout.ranking_paging import RankingPagingState
from autoscout.ranking_request import DEFAULT_RANKING_REQUEST
from autoscout.ranking_request import RankingRequest
from autoscout.ranking_request import ranking_request_declared
from autoscout.ranking_request import ranking_request_from_source
from autoscout.ranking_request import validate_ranking_request_declaration
from autoscout.ranking_response import UnwrappedResponse
from autoscout.ranking_response import ranking_response_field
from autoscout.ranking_response import unwrap_ranking_response
from autoscout.ranking_response import validate_ranking_response_declaration
from autoscout.source_collectors.base import CollectorRunError
from autoscout.source_collectors.base import _collector_error
from autoscout.source_collectors.base import collector_request_headers
from autoscout.source_collectors.base import collectors_pkg
from autoscout.source_collectors.base import likely_blocked_response
from autoscout.source_collectors.row_families import RowCapture
from autoscout.source_collectors.row_families import row_family
from autoscout.source_page_identity import BETWEEN_EDITIONS_SAFE_STATUS
from autoscout.source_page_identity import BETWEEN_EDITIONS_SKIP_PREFIX
from autoscout.source_page_identity import RANKING_DETAIL_SKIPPED_FIELD
from autoscout.source_page_identity import amazon_charts_week_url
from autoscout.source_page_identity import attach_ranking_row_diagnostics
from autoscout.source_page_identity import between_editions_reason
from autoscout.source_page_identity import incomplete_ranking_debug_for_page
from autoscout.source_page_identity import parse_visible_page_identity
from autoscout.source_page_identity import ranking_detail_fetch_plan
from autoscout.source_page_identity import ranking_detail_skipped_entry
from autoscout.source_page_identity import validate_page_shape
from autoscout.source_page_identity import validate_ranking_detail_pages

_MAX_AGENT_PAGE_BYTES = 750_000


def _capture_deadline_error(deadline: CaptureDeadline) -> CollectorRunError:
    """Return the failure of a source that ran out of its own allowance."""
    return _collector_error(
        deadline.exhausted_reason,
        debug_metadata={"collector_kind": "agent-extraction", "safe_failure_status": "capture_deadline"},
    )


def _request_timeout_seconds(source: dict[str, Any], deadline: CaptureDeadline | None) -> float:
    """Return the timeout for one request, bounded by the source deadline.

    The configured socket timeout still applies to every single request, so a
    hung connection is cut at the socket level as before.  A source that
    carries a capture deadline additionally cannot spend more than the time it
    has left, and stops issuing requests once that time is gone (issue #717).
    """
    socket_timeout = float(source.get("timeout_seconds", 10))
    if deadline is None:
        return socket_timeout
    remaining = deadline.remaining_seconds()
    if remaining <= 0:
        raise _capture_deadline_error(deadline)
    return min(socket_timeout, remaining)


def _streamed_response_text(
    response: Any,
    ceiling: int,
    consume: Callable[[str], None],
    *,
    deadline: CaptureDeadline | None = None,
) -> bool:
    """Hand one response to *consume* slice by slice, keeping none of it; return whether it was compressed.

    The failures are the ones ``_bounded_response_text`` reports for a whole page.
    """
    streamed = stream_http_text(
        response, ceiling, consume, remaining_seconds=(deadline.remaining_seconds if deadline is not None else None)
    )
    if streamed.timed_out and deadline is not None:
        raise _capture_deadline_error(deadline)
    if streamed.decode_failure:
        raise _collector_error(f"Agent extraction live page has {streamed.decode_failure}")
    if streamed.truncated:
        raise _collector_error(
            "Agent extraction decompressed page exceeds the response-size limit"
            if streamed.compressed
            else "Agent extraction live page exceeds the response-size limit"
        )
    return streamed.compressed


def _bounded_response_text(
    response: Any,
    max_bytes: int = _MAX_AGENT_PAGE_BYTES,
    *,
    deadline: CaptureDeadline | None = None,
) -> tuple[str, bool]:
    """Decode one bounded response through the shared HTTP decoder.

    A source carrying a capture deadline reads its body under that deadline,
    so a body that keeps arriving cannot outlive the source's allowance and a
    body that arrives after it is refused instead of published (issue #717).
    """
    decoded = decode_bounded_http_body(
        response, max_bytes, remaining_seconds=(deadline.remaining_seconds if deadline is not None else None)
    )
    if decoded.timed_out and deadline is not None:
        raise _capture_deadline_error(deadline)
    if decoded.decode_failure:
        raise _collector_error(f"Agent extraction live page has {decoded.decode_failure}")
    if decoded.truncated:
        message = (
            "Agent extraction decompressed page exceeds the response-size limit"
            if decoded.compressed
            else "Agent extraction live page exceeds the response-size limit"
        )
        raise _collector_error(message)
    return decoded.text, decoded.compressed


def parse_retry_after(value: str | None, date_header: str | None) -> float | None:
    """Parse a server-provided retry delay without consulting the local clock."""
    if value is None:
        return None
    value = value.strip()
    if value.isascii() and value.isdecimal():
        return float(int(value))
    if not date_header:
        return None
    try:
        retry_at = parsedate_to_datetime(value)
        response_date = parsedate_to_datetime(date_header)
        if retry_at.tzinfo is None or response_date.tzinfo is None:
            return None
        delay = (retry_at - response_date).total_seconds()
        return delay if delay >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _blocked_http_error(status: int, headers: Any) -> CollectorRunError:
    debug: dict[str, Any] = {
        "http_status": str(status),
        "collector_kind": "agent-extraction",
        "safe_failure_status": "source_blocked",
    }
    reason = f"Agent extraction live collection blocked by HTTP {status}"
    if status == 429:
        reason = "Agent extraction live collection rate-limited by HTTP 429"
        debug["rate_limited"] = True
        delay = parse_retry_after(headers.get("Retry-After"), headers.get("Date")) if headers else None
        if delay is not None:
            debug["retry_after_seconds"] = delay
    return _collector_error(reason, status="skipped", debug_metadata=debug)


# Issue #1163: the failure kinds a ranking detail page may lose its row to.
# The kind is set where the error is raised, only for a detail-page fetch;
# any failure without one of these kinds fails the source as before.
DETAIL_FAILURE_KIND_FIELD = "detail_failure_kind"


SKIPPABLE_DETAIL_FAILURE_KINDS = frozenset({"http_status", "timeout", "dropped_connection", "redirect", "scope"})


_DROPPED_CONNECTION_ERRORS = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, IncompleteRead)


# A challenge page names itself early; an error body is read only this far to look for it.
_CHALLENGE_BODY_PEEK_BYTES = 65_536


def _error_body_text(response: Any) -> str:
    """Return the start of an error response's body for challenge detection, or ""."""
    try:
        raw = response.read(_CHALLENGE_BODY_PEEK_BYTES)
    except Exception:
        return ""
    return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else ""


def _bot_challenge_debug(headers: Any) -> dict[str, bool]:
    """Mark a response the site's bot protection answered with a challenge (Issue #1163).

    The failure keeps its status and reason; the marker only stops a ranking
    detail route from skipping the row as if one page had failed.
    """
    try:
        value = headers.get("cf-mitigated", "") if headers is not None else ""
    except Exception:
        return {}
    return {"bot_challenge": True} if str(value or "").strip().casefold() == "challenge" else {}


def _fetch_bounded_page(
    source: dict[str, Any],
    url_to_fetch: str,
    *,
    ranking_request: RankingRequest | None = None,
    unwrap_response: bool = True,
    strict_status: bool = False,
    consume: Callable[[str], None] | None = None,
    stream_ceiling: int = 0,
    classify_detail_failure: bool = False,
) -> tuple[str, bool, str]:
    """Fetch one bounded page with an explicit request and unwrap policy.

    The configured ranking request applies only to the main ranking URL. Detail
    pages, identity pages, and Amazon auxiliary pages pass a plain GET request
    and keep their response body unchanged.

    With *consume*, the body is handed to it slice by slice under
    *stream_ceiling* instead of being returned, and the returned text is empty.

    With *classify_detail_failure* (a ranking detail page, Issue #1163), each
    failure names its kind under ``detail_failure_kind``, a bot-protection
    challenge is recognised by its header on any status and by its body on an
    error status, and a dropped connection fails as a classified error.
    Without it every outcome is unchanged.
    """

    def failure_kind(kind: str) -> dict[str, str]:
        return {DETAIL_FAILURE_KIND_FIELD: kind} if classify_detail_failure else {}

    def challenge_debug(headers: Any, error_response: Any = None) -> dict[str, bool]:
        marker = _bot_challenge_debug(headers)
        if not marker and classify_detail_failure and error_response is not None:
            if likely_blocked_response(_error_body_text(error_response)):
                marker = {"bot_challenge": True}
        return marker

    if consume is not None and (unwrap_response or stream_ceiling < 1):
        raise ValueError("a streamed page is neither unwrapped nor unbounded")
    request_spec = ranking_request if ranking_request is not None else ranking_request_from_source(source)
    headers = collector_request_headers()
    body: bytes | None = None
    method = "GET"
    if request_spec.is_post:
        headers.update(request_spec.headers)
        body = request_spec.body
        method = "POST"
    request = Request(url_to_fetch, data=body, method=method, headers=headers)
    deadline = capture_deadline_for(source)
    timeout_seconds = _request_timeout_seconds(source, deadline)
    try:
        with collectors_pkg().urlopen(request, timeout=timeout_seconds) as response:
            status = getattr(response, "status", None)
            if callable(status):
                status = status()
            if status is None:
                getcode = getattr(response, "getcode", None)
                status = getcode() if callable(getcode) else None
            rows_family = row_family(source)
            if status is None and rows_family is not None and rows_family.missing_status_error:
                raise _collector_error(rows_family.missing_status_error)
            response_headers = getattr(response, "headers", None)
            if strict_status and status is not None and int(status) != 200:
                if int(status) in {401, 403, 429}:
                    raise _blocked_http_error(int(status), response_headers)
                challenge = challenge_debug(response_headers, response)
                raise _collector_error(
                    f"Agent extraction live collection failed: HTTP {int(status)}",
                    debug_metadata={
                        "http_status": str(int(status)),
                        "collector_kind": "agent-extraction",
                        "safe_failure_status": "ranking_paging",
                        **(challenge or failure_kind("http_status")),
                    },
                )
            if classify_detail_failure and _bot_challenge_debug(response_headers):
                raise _collector_error(
                    "Agent extraction live collection blocked by a bot-protection challenge",
                    status="skipped",
                    debug_metadata={
                        "safe_debug_status": "blocked",
                        "safe_failure_status": "source_blocked",
                        "collector_kind": "agent-extraction",
                        "bot_challenge": True,
                    },
                )
            resolved_url = response.geturl()
            if consume is not None:
                text = ""
                content_decoded = _streamed_response_text(response, stream_ceiling, consume, deadline=deadline)
            else:
                text, content_decoded = _bounded_response_text(
                    response, int(source.get("max_response_bytes", _MAX_AGENT_PAGE_BYTES)), deadline=deadline
                )
    except TimeoutError:
        # A request cut short because the source ran out of its allowance is a
        # deadline failure, not a hung socket, so the reason names the bound.
        if deadline is not None and deadline.exhausted():
            raise _capture_deadline_error(deadline)
        raise _collector_error(
            "Agent extraction live collection failed: request timed out",
            debug_metadata={
                "collector_kind": "agent-extraction",
                "safe_failure_status": "request_timeout",
                **failure_kind("timeout"),
            },
        )
    except HTTPError as exc:
        if exc.code in {401, 403, 429}:
            raise _blocked_http_error(exc.code, exc.headers)
        challenge = challenge_debug(exc.headers, exc)
        raise _collector_error(
            f"Agent extraction live collection failed: HTTP {exc.code}",
            debug_metadata={
                "http_status": str(exc.code),
                "collector_kind": "agent-extraction",
                **(challenge or failure_kind("http_status")),
            },
        )
    except URLError as exc:
        if isinstance(exc.reason, TimeoutError) and deadline is not None and deadline.exhausted():
            raise _capture_deadline_error(deadline)
        if isinstance(exc.reason, TimeoutError):
            raise _collector_error(
                "Agent extraction live collection failed: request timed out",
                debug_metadata={
                    "collector_kind": "agent-extraction",
                    "safe_failure_status": "request_timeout",
                    **failure_kind("timeout"),
                },
            )
        # Only a dropped connection is page-local; a refused connection, a
        # failed name lookup or a TLS verification failure keeps no kind.
        dropped = failure_kind("dropped_connection") if isinstance(exc.reason, _DROPPED_CONNECTION_ERRORS) else {}
        raise _collector_error(f"Agent extraction live collection failed: {exc.reason}", debug_metadata=dropped or None)
    except _DROPPED_CONNECTION_ERRORS as exc:
        # A connection the server dropped after the request was sent is
        # raised unwrapped by urllib; outside a detail fetch it propagates as before.
        if not classify_detail_failure:
            raise
        if deadline is not None and deadline.exhausted():
            raise _capture_deadline_error(deadline) from exc
        raise _collector_error(
            f"Agent extraction live collection failed: connection dropped ({type(exc).__name__})",
            debug_metadata={"collector_kind": "agent-extraction", **failure_kind("dropped_connection")},
        ) from exc
    if consume is not None:
        # The caller read the page itself; a block page fails its own check.
        return text, content_decoded, resolved_url
    if likely_blocked_response(text):
        raise _collector_error(
            "Agent extraction live collection blocked by CAPTCHA-like response",
            status="skipped",
            debug_metadata={
                "safe_debug_status": "blocked",
                "safe_failure_status": "source_blocked",
                "collector_kind": "agent-extraction",
            },
        )
    if not unwrap_response:
        return text, content_decoded, resolved_url
    unwrapped = unwrap_ranking_response(text, source)
    if not unwrapped.ok:
        raise _collector_error(
            f"Agent extraction live collection failed: {unwrapped.reason}",
            debug_metadata={
                "collector_kind": "agent-extraction",
                "safe_failure_status": unwrapped.reason_code,
            },
        )
    return unwrapped.html, content_decoded, resolved_url


def _same_requested_url(requested_url: str, resolved_url: str) -> bool:
    """Return whether a response stayed on the requested URL target."""
    requested = urlsplit(requested_url)
    resolved = urlsplit(resolved_url)
    return (
        same_origin_url(requested_url, resolved_url)
        and requested.path.rstrip("/") == resolved.path.rstrip("/")
        and requested.query == resolved.query
    )


def _ranking_declaration_error(source: dict[str, Any]) -> str:
    """Return a declared ranking route error before the first network request."""
    for error in (
        validate_ranking_request_declaration(source),
        validate_ranking_response_declaration(source),
        validate_ranking_identity_declaration(source),
    ):
        if error:
            return error
    return ""


def _agent_paging_failure(state: RankingPagingState, max_pages: int, max_bytes: int) -> CollectorRunError:
    """Map shared paging state to the agent collector failure contract."""
    failure = state.failure
    assert failure is not None
    if failure.kind == PAGING_EMPTY:
        message = f"Agent extraction ranking page {failure.page_number} returned empty HTML"
    elif failure.kind == PAGING_REPEATED:
        message = f"Agent extraction ranking page {failure.page_number} repeated an earlier page"
    elif failure.kind == PAGING_MISSING_FINAL:
        message = f"Agent extraction ranking page {failure.page_number} did not provide a boolean final-page value"
    elif failure.kind == PAGING_TOO_LARGE:
        message = f"Agent extraction joined ranking HTML exceeded the {max_bytes}-byte bound"
    else:
        message = f"Agent extraction ranking paging reached max_pages={max_pages} before the final page"
    safe_status = "ranking_paging_truncated" if failure.kind == PAGING_TOO_LARGE else "ranking_paging"
    return _collector_error(
        message,
        debug_metadata={
            "collector_kind": "agent-extraction",
            "safe_failure_status": safe_status,
            **({"truncated": True} if failure.kind == PAGING_TOO_LARGE else {}),
        },
    )


def _fetch_ranking_page(
    source: dict[str, Any],
    url: str,
    ranking_request: RankingRequest,
    *,
    strict_status: bool,
    page_label: str,
) -> tuple[UnwrappedResponse, bool, str]:
    """Fetch one page of the ranking and unwrap its response envelope."""
    raw, content_decoded, resolved_url = _fetch_bounded_page(
        source, url, ranking_request=ranking_request, unwrap_response=False, strict_status=strict_status
    )
    if not _same_requested_url(url, resolved_url):
        raise _collector_error(
            f"Agent extraction {page_label} resolved to a different source target",
            debug_metadata={"collector_kind": "agent-extraction", "safe_failure_status": "wrong_target"},
        )
    response = unwrap_ranking_response(raw, source)
    if not response.ok:
        raise _collector_error(
            f"Agent extraction {page_label} response envelope failed: {response.reason}",
            debug_metadata={"collector_kind": "agent-extraction", "safe_failure_status": response.reason_code},
        )
    return response, content_decoded, resolved_url


def _fetch_declared_ranking_rows(
    source: dict[str, Any],
    url: str,
    ranking_request: RankingRequest,
    *,
    strict_status: bool,
) -> tuple[str, bool, str, UnwrappedResponse]:
    """Fetch every declared ranking page through the shared pure paging state."""
    max_bytes = int(source.get("max_response_bytes", _MAX_AGENT_PAGE_BYTES))
    first, content_decoded, resolved_url = _fetch_ranking_page(
        source, url, ranking_request, strict_status=strict_status, page_label="ranking page"
    )
    if not ranking_request.paging_declared:
        return first.html, content_decoded, resolved_url, first
    state = RankingPagingState.start(
        max_pages=ranking_request.max_pages,
        max_bytes=max_bytes,
        html=first.html,
        final_page=first.last_page_value(source),
    )
    while True:
        if state.failure is not None:
            raise _agent_paging_failure(state, ranking_request.max_pages, max_bytes)
        if state.complete:
            return state.joined_html, content_decoded, resolved_url, first
        if not state.needs_next_page:
            break
        page_number = state.page_count + 1
        page_response, _page_decoded, _page_resolved = _fetch_ranking_page(
            source,
            url,
            ranking_request.for_page(page_number),
            strict_status=strict_status,
            page_label=f"ranking page {page_number}",
        )
        state = state.append(page_response.html, page_response.last_page_value(source))
    raise _collector_error(
        f"Agent extraction ranking paging reached max_pages={ranking_request.max_pages} before the final page",
        debug_metadata={"collector_kind": "agent-extraction", "safe_failure_status": "ranking_paging"},
    )


def _fetch_declared_identity_page(
    source: dict[str, Any],
    envelope: Mapping[str, Any],
) -> str:
    """Fetch the identity route with a bounded plain GET and retain its HTML."""
    route = ranking_identity_route(source)
    if route is None:
        return ""
    identity_url, resolve_error = route.resolve(envelope, str(source.get("url", "")).strip())
    if resolve_error:
        raise _collector_error(
            f"Agent extraction ranking identity route failed: {resolve_error}",
            debug_metadata={"collector_kind": "agent-extraction", "safe_failure_status": "identity_route"},
        )
    identity_html, _decoded, resolved_url = _fetch_bounded_page(
        source,
        identity_url,
        ranking_request=DEFAULT_RANKING_REQUEST,
        unwrap_response=False,
        strict_status=True,
    )
    if not _same_requested_url(identity_url, resolved_url):
        raise _collector_error(
            "Agent extraction ranking identity page resolved to a different target",
            debug_metadata={"collector_kind": "agent-extraction", "safe_failure_status": "identity_route"},
        )
    identity = parse_visible_page_identity(identity_html, str(source.get("visible_title_selector", "")))
    marker = str(source.get("ranking_category_marker", "")).strip()
    if not identity_heading_matches(identity.identity_texts, marker, route.resolved_tokens(envelope)):
        raise _collector_error(
            "Agent extraction ranking identity page does not state the declared category and period",
            debug_metadata={"collector_kind": "agent-extraction", "safe_failure_status": "identity_route"},
        )
    return identity_html


def _fetch_ranking_detail_page(
    source: dict[str, Any], detail_url: str, detail_route: RankingDetailRoute
) -> tuple[dict[str, Any], bool]:
    """Fetch one row's detail page, refusing a redirect or a page without its declared scope."""
    detail_html, detail_decoded, detail_resolved = _fetch_bounded_page(
        source,
        detail_url,
        ranking_request=DEFAULT_RANKING_REQUEST,
        unwrap_response=False,
        strict_status=True,
        # A route that declares no failed-page budget fetches exactly as before.
        classify_detail_failure=detail_route.max_failed_pages > 0,
    )
    if detail_resolved != detail_url:
        raise _collector_error(
            "Ranking detail page resolved to a different linked URL",
            debug_metadata={
                "collector_kind": "agent-extraction",
                "safe_failure_status": "detail_redirect",
                "resolved_url": detail_resolved,
                **({DETAIL_FAILURE_KIND_FIELD: "redirect"} if detail_route.max_failed_pages > 0 else {}),
            },
        )
    scope = ranking_detail_scope(detail_html, detail_route.scope_selector)
    if scope.failure_reason:
        raise _collector_error(
            f"Ranking detail page scope validation failed: {scope.failure_reason}",
            debug_metadata={
                "collector_kind": "agent-extraction",
                "safe_failure_status": "detail_scope",
                **({DETAIL_FAILURE_KIND_FIELD: "scope"} if detail_route.max_failed_pages > 0 else {}),
            },
        )
    return {
        "source_page": detail_url,
        "detail_url": detail_url,
        "resolved_url": detail_resolved,
        "html": detail_html,
    }, detail_decoded


def _detail_failure_tolerated(exc: CollectorRunError, source: dict[str, Any]) -> bool:
    """Whether one detail-page failure may skip its row instead of failing the source (Issue #1163).

    Only the failure kinds the fetch classified where it raised them qualify:
    an HTTP error status that is no block or challenge, a timeout, a dropped
    connection, a redirect to another URL, or a page without its declared
    scope. Every other failure - a block, a rate limit, a bot-protection
    challenge, a TLS or name-lookup failure, an unexpected error, a source out
    of its capture allowance - stops the source as before.
    """
    deadline = capture_deadline_for(source)
    if deadline is not None and deadline.exhausted():
        return False
    return (
        exc.status == "failed"
        and exc.debug_metadata.get(DETAIL_FAILURE_KIND_FIELD) in SKIPPABLE_DETAIL_FAILURE_KINDS
        and exc.debug_metadata.get("bot_challenge") is not True
        and exc.debug_metadata.get("safe_failure_status") != "capture_deadline"
    )


def _row_capture(config_path: Path, source: dict[str, Any], reference_date: date) -> RowCapture:
    """Give a row family the bounded plain GETs its capture is allowed to make."""

    def fetch(target: str) -> dict[str, Any]:
        raw, _decoded, resolved = _fetch_bounded_page(
            source, target, ranking_request=DEFAULT_RANKING_REQUEST, unwrap_response=False, strict_status=True
        )
        return {"body": raw, "status": 200, "resolved_url": resolved}

    def scan(target: str, consume: Callable[[str], None], ceiling: int) -> dict[str, Any]:
        _empty, _decoded, resolved = _fetch_bounded_page(
            source,
            target,
            ranking_request=DEFAULT_RANKING_REQUEST,
            unwrap_response=False,
            strict_status=True,
            consume=consume,
            stream_ceiling=ceiling,
        )
        return {"status": 200, "resolved_url": resolved, "truncated": False}

    return RowCapture(config_path=config_path, source=source, reference_date=reference_date, fetch=fetch, scan=scan)


def fetch_agent_source_page(
    config_path: Path,
    output_dir: Path,
    source: dict[str, Any],
    *,
    reference_date: date | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch one configured source page live.

    A live fetch returns the page as it stands now, so week-freshness checks
    judge it against the clock date. A configured ``run_date`` is a pinned
    replay label and must not decide whether a live page is current;
    *reference_date* exists so tests can pin the clock.
    """
    week_reference_date = reference_date or date.today()
    url = str(source.get("url", "")).strip()
    if not url:
        raise _collector_error("Agent extraction live collection requires a configured source URL")
    rows_family = row_family(source)
    if rows_family is not None:
        try:
            return rows_family.capture(_row_capture(config_path, source, week_reference_date))
        except rows_family.error as exc:
            reason_code = getattr(exc, "reason_code", "")
            raise _collector_error(str(exc), debug_metadata={"safe_failure_status": reason_code}) from exc
    if source.get("amazon_charts_weekly") is True:
        # Live collection makes two bounded requests per run: the stable
        # landing page for week resolution, then the dated chart page.
        landing_html, _landing_decoded, landing_resolved = _fetch_bounded_page(
            source, url, ranking_request=DEFAULT_RANKING_REQUEST, unwrap_response=False, strict_status=True
        )
        landing = urlsplit(landing_resolved)
        configured_landing = urlsplit(url)
        if (
            not configured_landing.scheme
            or landing.scheme.casefold() != configured_landing.scheme.casefold()
            or landing.netloc.casefold() != configured_landing.netloc.casefold()
            or landing.path != configured_landing.path
            or landing.query
            or landing.fragment
        ):
            raise _collector_error(
                "Amazon Charts landing page resolved to a different source target",
                debug_metadata={
                    "collector_kind": "agent-extraction",
                    "safe_failure_status": "wrong_target",
                    "resolved_url": landing_resolved,
                },
            )
        try:
            dated_url = amazon_charts_week_url(
                landing_html,
                url,
                chart_path_segment=str(source.get("chart_path_segment", "")),
                expected_page_heading=str(source.get("expected_page_heading", "")),
                reference_date=week_reference_date,
            )
        except ValueError as exc:
            raise _collector_error(
                f"Amazon Charts landing page could not resolve the dated nonfiction chart: {exc}",
                debug_metadata={
                    "collector_kind": "agent-extraction",
                    "safe_failure_status": "page_shape",
                },
            ) from exc
        dated_html, content_decoded, dated_resolved = _fetch_bounded_page(
            source, dated_url, ranking_request=DEFAULT_RANKING_REQUEST, unwrap_response=False, strict_status=True
        )
        dated_requested = urlsplit(dated_url)
        dated_actual = urlsplit(dated_resolved)
        if (
            dated_requested.scheme.casefold() != dated_actual.scheme.casefold()
            or dated_requested.netloc.casefold() != dated_actual.netloc.casefold()
            or dated_requested.path != dated_actual.path
            or dated_requested.query != dated_actual.query
            or dated_requested.fragment != dated_actual.fragment
        ):
            raise _collector_error(
                "Amazon Charts dated chart resolved to a different URL",
                debug_metadata={
                    "collector_kind": "agent-extraction",
                    "safe_failure_status": "wrong_target",
                    "resolved_url": dated_resolved,
                },
            )
        return {
            "source_page": dated_url,
            "resolved_url": dated_resolved,
            "reference_date": week_reference_date.isoformat(),
            "html": dated_html,
        }, {
            "source_url": dated_url,
            "resolved_url": dated_resolved,
            "landing_resolved_url": landing_resolved,
            "content_decoded": content_decoded,
        }
    declaration_error = _ranking_declaration_error(source)
    if declaration_error:
        raise _collector_error(
            f"Agent extraction ranking declaration is invalid: {declaration_error}",
            debug_metadata={"collector_kind": "agent-extraction", "safe_failure_status": "ranking_declaration"},
        )
    ranking_request = ranking_request_from_source(source)
    strict_status = (
        ranking_request_declared(source) or bool(ranking_response_field(source)) or ranking_identity_declared(source)
    )
    text, content_decoded, resolved_url, first_response = _fetch_declared_ranking_rows(
        source, url, ranking_request, strict_status=strict_status
    )
    identity_html = _fetch_declared_identity_page(source, first_response.envelope)
    page_data: dict[str, Any] = {
        "source_page": url,
        "resolved_url": resolved_url,
        "reference_date": week_reference_date.isoformat(),
        "html": text,
    }
    if identity_html:
        page_data["identity_html"] = identity_html
    detail_debug: dict[str, Any] = {}
    detail_route = RankingDetailRoute.from_source(source)
    off_season = between_editions_reason(source, page_data)
    if off_season:
        # Not a page-shape failure: the page shows the declared state between
        # two editions, so there is nothing to extract this week.
        raise _collector_error(
            BETWEEN_EDITIONS_SKIP_PREFIX + off_season,
            status="skipped",
            debug_metadata={"collector_kind": "agent-extraction", "safe_failure_status": BETWEEN_EDITIONS_SAFE_STATUS},
        )
    shape_error = validate_page_shape(source, page_data, reference_date=week_reference_date, detail_route=detail_route)
    if shape_error:
        debug_metadata = attach_ranking_row_diagnostics(
            {
                "collector_kind": "agent-extraction",
                "safe_failure_status": "page_shape",
            },
            text,
            source,
            shape_error,
            detail_route=detail_route,
        )
        raise _collector_error(
            shape_error,
            debug_metadata=debug_metadata,
        )
    if detail_route is not None:
        rows, plan_error = ranking_detail_fetch_plan(source, text, route=detail_route)
        if plan_error:
            raise _collector_error(
                plan_error,
                debug_metadata={"collector_kind": "agent-extraction", "safe_failure_status": "detail_route"},
            )
        detail_pages: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        for row in rows:
            try:
                detail_page, detail_decoded = _fetch_ranking_detail_page(source, row.detail_url, detail_route)
            except CollectorRunError as exc:
                # Issue #1163: a declared, bounded number of rows may lose their
                # detail page; the next failure fails the source as before.
                if not _detail_failure_tolerated(exc, source) or len(skipped) >= detail_route.max_failed_pages:
                    if skipped:
                        exc.debug_metadata = {**exc.debug_metadata, "detail_pages_skipped_count": len(skipped)}
                    raise
                skipped.append(ranking_detail_skipped_entry(row, str(exc)))
                continue
            detail_pages.append(detail_page)
            detail_debug["detail_content_decoded"] = detail_decoded
        page_data["detail_pages"] = detail_pages
        if skipped:
            page_data[RANKING_DETAIL_SKIPPED_FIELD] = skipped
        detail_error = validate_ranking_detail_pages(source, page_data, route=detail_route)
        if detail_error:
            raise _collector_error(
                detail_error,
                debug_metadata={"collector_kind": "agent-extraction", "safe_failure_status": "detail_evidence"},
            )
    return page_data, {
        "source_url": url,
        "resolved_url": resolved_url,
        "content_decoded": content_decoded,
        "detail_page_count": len(page_data.get("detail_pages", [])),
        **detail_debug,
        **incomplete_ranking_debug_for_page(source, page_data, detail_route=detail_route),
    }
