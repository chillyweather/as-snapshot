"""Bounded subscriber-session fetching and temporary PDF text adapter (#954)."""

from __future__ import annotations

import base64
import html
import http.client
import http.cookiejar
import logging
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any
from typing import Protocol
from typing import cast

from autoscout.pw_issue_parser import UNREADABLE_PAGE
from autoscout.source_collectors.base import COLLECTOR_USER_AGENT


class PwError(Exception):
    """A public reason code; never include a URL or underlying exception text."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


HOSTS = {"sailthru.emagazines.com", "archives.publishersweekly.com", "reader.publishersweekly.com"}
READER = re.compile(r"/publishers_weekly/(\d{8})/index\.html$")
ARCHIVE = re.compile(r"/archive/([\w-]+)/(?P<id>\d+)$")


def checked_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port  # raises ValueError for a malformed port
    except ValueError:
        raise PwError("pw_subscriber_link_rejected") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname not in HOSTS
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or "/account/" in parsed.path.casefold()
    ):
        raise PwError("pw_subscriber_link_rejected")
    permitted = {
        "sailthru.emagazines.com": r"/click/[^/]+/[^/]+/[^/]+",
        "archives.publishersweekly.com": r"/(?:archive/[\w-]+/\d+|(?:[^/]+/)?StoryFinderViewIssue/\d+)",
        "reader.publishersweekly.com": r"/publishers_weekly/\d{8}/(?:index\.html|[^/]+\.pdf)",
    }
    if not re.fullmatch(permitted[parsed.hostname], parsed.path, re.I):
        raise PwError("pw_subscriber_link_rejected")
    return url


def issue_link(body: str, collected_ids: frozenset[str] = frozenset()) -> tuple[str, str]:
    """Decode tracker targets locally; never visit unsubscribe or other links.

    When the email links to more than one issue, links to an issue already collected
    are dropped: since November 2026 it carries a BookLife paragraph linking to an
    older issue beside the current one.
    The reader date check in ``fetch_issue`` still refuses a wrong issue.
    """
    candidates = []
    for found in re.findall(r'https://[^\s<>"\']+', body):
        url = html.unescape(found)
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError:
            continue  # not a usable link; another one may be the issue link
        target = url
        if parsed.hostname == "sailthru.emagazines.com":
            match = re.fullmatch(r"/click/[^/]+/([^/]+)/[^/]+", parsed.path)
            if not match:
                continue
            try:
                target = base64.urlsafe_b64decode(match[1] + "=" * (-len(match[1]) % 4)).decode("utf-8")
            except (ValueError, UnicodeError):
                continue
        try:
            destination = urllib.parse.urlsplit(target)
        except ValueError:
            continue
        archive = ARCHIVE.fullmatch(destination.path)
        if destination.scheme == "https" and destination.hostname == "archives.publishersweekly.com" and archive:
            try:
                checked_url(url)
            except PwError:
                # A malformed link in the email is the email's fault, never a refusal by PW.
                raise PwError("pw_issue_link_missing_or_ambiguous") from None
            candidates.append((url, archive["id"]))
    if len({item[1] for item in candidates}) > 1:
        candidates = [item for item in candidates if item[1] not in collected_ids]
    if not candidates or len({item[1] for item in candidates}) != 1:
        raise PwError("pw_issue_link_missing_or_ambiguous")
    return candidates[0]


class Response(Protocol):
    def geturl(self) -> str: ...
    def read(self, size: int = -1) -> bytes: ...
    def close(self) -> None: ...


class HttpClient(Protocol):
    def open(self, url: str) -> Response: ...


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        checked_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SubscriberClient:
    """Cookies exist for this pass only; every redirect is checked before sending."""

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        self.opener = urllib.request.build_opener(
            _Redirects(), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def open(self, url: str) -> Response:
        checked_url(url)
        try:
            return cast(
                Response,
                self.opener.open(
                    urllib.request.Request(url, headers={"User-Agent": COLLECTOR_USER_AGENT}), timeout=self.timeout
                ),
            )
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 410):
                raise PwError("pw_subscriber_link_rejected") from None
            raise PwError("pw_download_failed") from None
        except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError):
            raise PwError("pw_download_failed") from None


def pdf_page_texts(path: Path) -> list[str]:
    """The sole optional dependency import. No source text is logged by pypdf."""
    try:
        from pypdf import PdfReader
    except ImportError:
        raise PwError("pw_pdf_reader_missing") from None
    logger = logging.getLogger("pypdf")
    old_handlers, old_propagate = logger.handlers[:], logger.propagate
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    try:
        reader = PdfReader(path)
        texts = []
        for page in reader.pages:
            # One page past pypdf's limits (LimitReachedError) must not lose the issue.
            try:
                texts.append(page.extract_text() or "")
            except Exception:
                texts.append(UNREADABLE_PAGE)
        return texts
    except Exception:
        raise PwError("pw_pdf_parse_failed") from None
    finally:
        logger.handlers, logger.propagate = old_handlers, old_propagate


def _fetch_issue(
    client: HttpClient,
    link: str,
    expected_date: str,
    max_pdf_bytes: int,
    *,
    text_reader: Callable[[Path], list[str]] = pdf_page_texts,
) -> list[str]:
    """Authenticate using the subscriber link, then delete the PDF on every exit.

    A link the email itself carries malformed is the email's fault, not PW's, so
    ``pw_subscriber_link_rejected`` always means PW refused or redirected us.
    """
    try:
        checked_url(link)
    except PwError:
        raise PwError("pw_issue_link_missing_or_ambiguous") from None
    response = client.open(link)
    try:
        final = urllib.parse.urlsplit(checked_url(response.geturl()))
        match = READER.fullmatch(final.path)
        if not match:
            raise PwError("pw_subscriber_link_rejected")
        stamp = match[1]
        actual_date = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"
        if actual_date != expected_date:
            raise PwError("pw_issue_date_mismatch")
        page = response.read(2_000_001)
        if len(page) > 2_000_000:
            raise PwError("pw_reader_page_too_large")
        paths = set(
            re.findall(
                r'href=["\'](/publishers_weekly/' + stamp + r'/[^/"\'?#]+\.pdf)["\']', page.decode("utf-8", "replace")
            )
        )
        if len(paths) != 1:
            raise PwError("pw_pdf_link_missing_or_ambiguous")
        pdf_url = urllib.parse.urljoin(response.geturl(), paths.pop())
    finally:
        response.close()
    with tempfile.TemporaryDirectory(prefix="autoscout-pw-") as directory:
        path = Path(directory) / "issue.pdf"
        response = client.open(checked_url(pdf_url))
        try:
            if urllib.parse.urlsplit(checked_url(response.geturl())).path != urllib.parse.urlsplit(pdf_url).path:
                raise PwError("pw_subscriber_link_rejected")
            total = 0
            with path.open("wb") as output:
                while chunk := response.read(min(1024 * 1024, max_pdf_bytes - total + 1)):
                    total += len(chunk)
                    if total > max_pdf_bytes:
                        raise PwError("pw_pdf_too_large")
                    output.write(chunk)
        finally:
            response.close()
        return text_reader(path)


def fetch_issue(
    client: HttpClient,
    link: str,
    expected_date: str,
    max_pdf_bytes: int,
    *,
    text_reader: Callable[[Path], list[str]] = pdf_page_texts,
) -> list[str]:
    """Translate transport failures without exposing the subscriber request."""
    try:
        return _fetch_issue(client, link, expected_date, max_pdf_bytes, text_reader=text_reader)
    except urllib.error.HTTPError as exc:
        raise PwError("pw_subscriber_link_rejected" if exc.code in (401, 403, 410) else "pw_download_failed") from None
    # http.client.HTTPException covers truncated bodies (IncompleteRead) during read().
    except (OSError, urllib.error.URLError, http.client.HTTPException):
        raise PwError("pw_download_failed") from None
