"""Explicit Kirkus coming-soon nonfiction listing evidence, shared by pre-collection, probe and replay.

Kirkus lists the nonfiction books it has reviewed that are not yet published,
six to a page, in order of publication date. A listing card names the title,
the authors, the site's genre label and the publication date. The book's
review page adds the ISBN, the publisher, the date the review was posted, and
the review program of a review its author paid for. The review itself is on
the same page; the page is read once for those facts and never retained.

The site asks for twenty seconds between requests, so a pass takes many
minutes and runs as its own stage, in front of the capture stage. Capture reads
the result of the last pass and requests nothing.

Kirkus prints every title in capitals. The capitalised title is this source's
title by operator decision (#816); it is never re-cased.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from collections.abc import Mapping
from datetime import date
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import NoReturn
from urllib.request import HTTPRedirectHandler
from urllib.request import Request
from urllib.request import build_opener

from autoscout.http_decoding import MAX_RESPONSE_BYTES
from autoscout.json_io import read_json
from autoscout.json_io import write_json

if TYPE_CHECKING:
    from autoscout.source_feasibility import FeasibilityVerdict

FAMILY = "kirkus-coming-soon"
ORIGIN = "https://www.kirkusreviews.com"
SOURCE_URL = ORIGIN + "/discover-books/nonfiction"
EVIDENCE_FIELD = "kirkus_evidence"
PAGE_HEADING = "Discover Books"
SELECTED_ORDER = "Release Date (Coming Soon)"
MAX_LISTING_PAGES = 40
MAX_DETAIL_PAGES = 60
MIN_REQUEST_DELAY_SECONDS = 20
MAX_REQUEST_DELAY_SECONDS = 120
MAX_COLLECTION_AGE_DAYS = 14
MAX_REVIEW_AGE_DAYS = 366
SIGNAL_TYPE = "media/curated list"
DECLARATION_FIELDS = (
    "kirkus_max_listing_pages",
    "kirkus_max_detail_pages",
    "kirkus_request_delay_seconds",
    "kirkus_max_collection_age_days",
    "kirkus_max_review_age_days",
    "kirkus_genre_labels",
)

_REVIEW_PATH_RE = re.compile(r"/book-reviews/[a-z0-9-]+/[A-Za-z0-9-]+/")
_DATE_RE = re.compile(r"(?P<month>[A-Za-z]+)\.? (?P<day>[0-9]{1,2}), (?P<year>[0-9]{4})")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_REVIEW_PAGE_LABELS = ("Pub Date:", "ISBN:", "Publisher:", "Review Posted Online:")
_REVIEW_PROGRAM_LABEL = "Review Program:"
# `missing_publication_date` is no longer given to a card (#913): a card that prints no date is opened, and its
# review page decides. The value stays valid so a result retained by an earlier pass still reads.
_CARD_REJECTIONS = frozenset({"author_line", "missing_publication_date"})
_PAGE_REJECTIONS = frozenset(
    {
        "paid_review",
        "missing_isbn",
        "invalid_isbn",
        "self_published",
        "publication_date_mismatch",
        "missing_review_date",
        "missing_page_publication_date",
    }
)
# A review page whose rejection a later pass may undo, so the page is opened again (#886).
# Kirkus can print the posting date of a review as a word, or not yet at all, on the day it posts it.
# Kirkus can also list a fresh review whose card and page print no publication date yet (#913).
_REOPENED_REJECTIONS = frozenset({"missing_review_date", "missing_page_publication_date"})
# The words Kirkus prints for a review posted on the day a page is opened or on the day before (#886).
_RELATIVE_REVIEW_DAYS = {"today": 0, "yesterday": 1}
# Why a pass stopped opening review pages: nothing, or the transport refusal that ended it.
# A pass ends early when the site refuses a review page or when the pass runs out of its time allowance.
_STOP_REASONS = frozenset({"", "kirkus_http", "kirkus_response_bounds", "kirkus_wrong_target", "kirkus_time_budget"})
_DETAIL_FIELDS = frozenset({"isbn", "publisher", "publication_date", "review_date"})
_ROW_FIELDS = frozenset(
    {
        "review_url",
        "title",
        "authors",
        "genre_label",
        "starred",
        "card_publication_date",
        "rejection",
        "page_fetched_date",
        "page_sha256",
        "detail",
    }
)
# Everything a mention of this source may carry: the row's identity, the configured signal fields, a
# metadata-only note, and the non-authoritative search helpers.
_MENTION_FIELDS = frozenset(
    {
        "title",
        "primary_authors",
        "publisher",
        "publication_year",
        "isbn",
        "source_week",
        "source_url",
        "evidence_url",
        "source_check_date",
        "signal_type",
        "signal_strength",
        "evidence_note",
        "candidate_category",
        "english_title",
        "english_title_type",
        "english_title_provenance",
        "search_translation",
        "author_transliteration_note",
    }
)


class KirkusError(ValueError):
    """A named refusal that callers can report without repairing evidence."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


def _fail(code: str, message: str) -> NoReturn:
    raise KirkusError(code, message)


def _bounded_int(source: Mapping[str, Any], field: str, floor: int, ceiling: int) -> int:
    value = source.get(field)
    if type(value) is not int or not floor <= value <= ceiling:
        _fail("kirkus_declaration", f"Kirkus listing requires {field} between {floor} and {ceiling}")
    return value


def genre_labels(source: Mapping[str, Any]) -> set[str]:
    return {label.strip().casefold() for label in source.get("kirkus_genre_labels", [])}


def validate_declaration(source: Mapping[str, Any]) -> None:
    if source.get("url") != SOURCE_URL:
        _fail("kirkus_declaration", "Kirkus listing requires its canonical nonfiction listing URL")
    _bounded_int(source, "kirkus_max_listing_pages", 1, MAX_LISTING_PAGES)
    _bounded_int(source, "kirkus_max_detail_pages", 1, MAX_DETAIL_PAGES)
    _bounded_int(source, "kirkus_request_delay_seconds", MIN_REQUEST_DELAY_SECONDS, MAX_REQUEST_DELAY_SECONDS)
    _bounded_int(source, "kirkus_max_collection_age_days", 1, MAX_COLLECTION_AGE_DAYS)
    _bounded_int(source, "kirkus_max_review_age_days", 1, MAX_REVIEW_AGE_DAYS)
    labels = source.get("kirkus_genre_labels")
    if not isinstance(labels, list) or not labels or any(not isinstance(x, str) or not x.strip() for x in labels):
        _fail("kirkus_declaration", "Kirkus listing requires a nonempty list of accepted genre labels")


def signal_strength(source: Mapping[str, Any]) -> str:
    """One review is a minor mention unless the source declares otherwise."""
    return str(source.get("expected_signal_strength", "")).strip() or "minor mention"


def _text(value: str) -> str:
    return " ".join(value.split())


def _valid_isbn13(isbn: str) -> bool:
    return (
        re.fullmatch(r"97[89][0-9]{10}", isbn) is not None
        and sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(isbn)) % 10 == 0
    )


def parse_printed_date(text: str) -> date | None:
    """Read `October 15, 2026`, `Oct. 20, 2026` or `Sept. 04, 2026`; anything else is no date."""
    match = _DATE_RE.fullmatch(_text(text))
    if match is None:
        return None
    word = match["month"].casefold()
    months = [index for index, name in enumerate(_MONTHS, start=1) if len(word) >= 3 and name.startswith(word)]
    if len(months) != 1:
        return None
    try:
        return date(int(match["year"]), months[0], int(match["day"]))
    except ValueError:
        return None


def parse_review_posted_date(text: str, opened: date) -> date | None:
    """Read the posting date of a review: a printed date, or `today` or `yesterday` as of the day the page was opened."""
    days = _RELATIVE_REVIEW_DAYS.get(_text(text).casefold())
    return opened - timedelta(days=days) if days is not None else parse_printed_date(text)


def listing_page_url(page: int) -> str:
    return f"{SOURCE_URL}?order=1" if page == 1 else f"{SOURCE_URL}?order=1&page={page}"


_VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"})


class _ListingPage(HTMLParser):
    """Read the heading, the selected order and the book cards of one listing page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headings: list[str] = []
        self.selected_orders: list[str] = []
        self.cards: list[dict[str, Any]] = []
        self._card: dict[str, Any] | None = None
        self._stack: list[tuple[str, str, list[str]]] = []

    def _inside(self, role: str) -> bool:
        return any(name == role for _, name, _ in self._stack)

    def _role(self, tag: str, attrs: dict[str, str | None]) -> str:
        classes = set((attrs.get("class") or "").split())
        card = self._card
        if card is None:
            if tag == "div" and {"card", "feature-review-card"} <= classes:
                self._card = {
                    "starred": "kirkus-star" in classes,
                    "date": "",
                    "genre": "",
                    "href": None,
                    "title": "",
                    "byline": [],
                }
                return "card"
            if tag == "h1":
                return "heading"
            if tag == "option" and "selected" in attrs:
                return "order"
            return ""
        if tag == "span" and "db-pub-date" in classes:
            return "date"
        if tag == "a" and "card-title" in classes:
            return "genre"
        if tag == "a" and "card-text" in classes and card["href"] is None:
            card["href"] = attrs.get("href") or ""
            return "title"
        if tag == "span" and "by-author" in classes and not card["byline"]:
            return "byline"
        if tag == "a" and self._inside("byline"):
            return "name"
        return ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID_TAGS:
            return
        self._stack.append((tag, self._role(tag, dict(attrs)), []))

    def handle_endtag(self, tag: str) -> None:
        if not any(open_tag == tag for open_tag, _, _ in self._stack):
            return
        while self._stack:
            open_tag, role, parts = self._stack.pop()
            self._closed(role, _text("".join(parts)))
            if open_tag == tag:
                break

    def _closed(self, role: str, text: str) -> None:
        card = self._card
        if role == "heading":
            self.headings.append(text)
        elif role == "order":
            self.selected_orders.append(text)
        elif card is None:
            return
        elif role == "date":
            card["date"] = text
        elif role == "genre":
            card["genre"] = text
        elif role == "title":
            card["title"] = text
        elif role == "name":
            card["byline"].append(("name", text))
        elif role == "byline":
            if text:
                card["byline"].append(("text", text))
        elif role == "card":
            self.cards.append(card)
            self._card = None

    def handle_data(self, data: str) -> None:
        for _, role, parts in reversed(self._stack):
            if role == "byline":
                # Text between author links is kept in order, so the line can be read as written.
                if _text(data):
                    self._card["byline"].append(("text", _text(data)))  # type: ignore[index]
                return
            if role in {"heading", "order", "date", "genre", "title", "name"}:
                parts.append(data)
                return


def _ends_primary_authors(value: str) -> bool:
    """A semicolon, `with`, or a role such as `illustrated by` after a name: what follows is no primary author."""
    return value.startswith(";") or value == "with" or re.fullmatch(r"[a-z]+(?: [a-z]+)* by", value) is not None


def _primary_authors(byline: list[tuple[str, str]]) -> list[str]:
    """Names after `by` or `edited by`, up to a collaborator or a role; any other line names no primary author.

    Kirkus prints `by A with B` for a book written with a collaborator and
    `by A illustrated by B` for an illustrator (#873). Both keep A alone.
    """
    if not byline or byline[0] not in {("text", "by"), ("text", "edited by")}:
        return []
    authors: list[str] = []
    expect_name = True
    for kind, value in byline[1:]:
        if kind == "name":
            if not expect_name or not value:
                return []
            authors.append(value)
            expect_name = False
        elif not expect_name and _ends_primary_authors(value):
            break
        elif value.startswith(";"):
            break
        elif value in {"&", ","} and not expect_name:
            expect_name = True
        else:
            return []
    return authors if authors and not expect_name else []


def _card_row(card: Mapping[str, Any]) -> dict[str, Any]:
    """Turn one parsed card into a retained row, naming why a card without a grounded author is rejected.

    A card that prints no date is kept: Kirkus lists a freshly posted review before its card carries the
    date, and the review page prints it (#913).
    """
    href = card["href"]
    if not isinstance(href, str) or _REVIEW_PATH_RE.fullmatch(href) is None or not card["title"] or not card["genre"]:
        _fail("kirkus_page_shape", "Kirkus listing card lacks its review link, title or genre label")
    authors = _primary_authors(card["byline"])
    published = parse_printed_date(card["date"])
    rejection = "" if authors else "author_line"
    return {
        "review_url": ORIGIN + href,
        "title": card["title"],
        "authors": authors,
        "genre_label": card["genre"],
        "starred": card["starred"],
        "card_publication_date": published.isoformat() if published else "",
        "rejection": rejection,
        "page_fetched_date": "",
        "page_sha256": "",
        "detail": None,
    }


def parse_listing_page(page_html: str) -> list[dict[str, Any]]:
    """Return the rows of one listing page; an empty list is the page after the last one."""
    parser = _ListingPage()
    parser.feed(page_html)
    parser.close()
    if PAGE_HEADING not in parser.headings:
        _fail("kirkus_page_shape", f"Kirkus listing page lacks the heading {PAGE_HEADING}")
    if SELECTED_ORDER not in parser.selected_orders:
        _fail("kirkus_page_shape", f"Kirkus listing page is not in the order {SELECTED_ORDER}")
    return [_card_row(card) for card in parser.cards]


class _ReviewPage(HTMLParser):
    """Read the title and the labelled facts of the reviewed book on one review page.

    A review page also carries the reviews of two further books, each under
    its own title heading. Only what stands between the first title heading
    and the second is read. A fact is a paragraph that opens with its label in
    bold; the review text stands in paragraphs without one and is not read.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.titles: list[str] = []
        self.facts: list[tuple[str, str]] = []
        self._title: list[str] | None = None
        self._paragraph: dict[str, list[str]] | None = None
        self._in_label = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "h1" and "article-title" in (dict(attrs).get("class") or "").split():
            self._title = []
        elif len(self.titles) != 1:
            return
        elif tag == "p":
            self._paragraph = {"label": [], "value": []}
            self._in_label = False
        elif tag == "b" and self._paragraph is not None and not self._paragraph["label"]:
            self._in_label = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1" and self._title is not None:
            self.titles.append(_text("".join(self._title)))
            self._title = None
        elif tag == "b":
            self._in_label = False
        elif tag == "p" and self._paragraph is not None:
            label = _text("".join(self._paragraph["label"]))
            if label:
                self.facts.append((label, _text("".join(self._paragraph["value"]))))
            self._paragraph = None

    def handle_data(self, data: str) -> None:
        if self._title is not None:
            self._title.append(data)
        elif self._paragraph is not None:
            self._paragraph["label" if self._in_label else "value"].append(data)


def parse_review_page(page_html: str, row: Mapping[str, Any], opened: date) -> tuple[dict[str, Any] | None, str]:
    """Return the facts of one review page, or name why the row is rejected.

    Only the labelled values of the reviewed book are read. The review text
    and the two further reviews on the page are not read.
    """
    parser = _ReviewPage()
    parser.feed(page_html)
    parser.close()
    if not parser.titles or parser.titles[0] != row["title"]:
        _fail("kirkus_review_page_shape", "Kirkus review page does not open with the title of its listing card")
    values: dict[str, str] = {}
    for label in _REVIEW_PAGE_LABELS:
        found = [value for printed, value in parser.facts if printed == label]
        if len(found) != 1:
            _fail("kirkus_review_page_shape", f"Kirkus review page must print the label {label} exactly once")
        values[label] = found[0]
    if any(printed == _REVIEW_PROGRAM_LABEL for printed, _value in parser.facts):
        # Kirkus names a review program only on a review its author paid for.
        return None, "paid_review"
    isbn = re.sub(r"[-\s]", "", values["ISBN:"])
    if not isbn:
        return None, "missing_isbn"
    if not _valid_isbn13(isbn):
        return None, "invalid_isbn"
    publisher = values["Publisher:"]
    if not publisher or publisher.casefold() == "none":
        # Kirkus prints no publisher for a book its author published.
        return None, "self_published"
    published = parse_printed_date(values["Pub Date:"])
    if not row["card_publication_date"]:
        # A card without a date takes the date its review page prints (#913).
        if published is None:
            return None, "missing_page_publication_date"
    elif published is None or published.isoformat() != row["card_publication_date"]:
        return None, "publication_date_mismatch"
    reviewed = parse_review_posted_date(values["Review Posted Online:"], opened)
    if reviewed is None:
        return None, "missing_review_date"
    return {
        "isbn": isbn,
        "publisher": publisher,
        "publication_date": published.isoformat(),
        "review_date": reviewed.isoformat(),
    }, ""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def open_without_redirect(request: Request, *, timeout: float) -> Any:
    """Open one request and follow no redirect.

    A followed redirect is a second request with no delay before it, to a URL
    this contract never chose. A redirect is answered as the HTTP error it is.
    """
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


def default_pause(seconds: float) -> None:
    """Wait out the declared delay between two requests."""
    time.sleep(seconds)


def _body(response: Mapping[str, Any], url: str) -> str:
    body = response.get("body")
    if response.get("status") != 200:
        _fail("kirkus_http", "Kirkus request did not return HTTP 200")
    if response.get("truncated") or not isinstance(body, str) or len(body.encode("utf-8")) > MAX_RESPONSE_BYTES:
        _fail("kirkus_response_bounds", "Kirkus response is missing, truncated or oversized")
    if response.get("resolved_url") != url:
        _fail("kirkus_wrong_target", "Kirkus request resolved to a different URL")
    return body


def _digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def collect(
    source: Mapping[str, Any],
    reference: date,
    fetch: Callable[[str], Mapping[str, Any]],
    pause: Callable[[float], None],
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One pre-collection pass: the listing pages, then the review pages of rows not opened before.

    The declared delay is kept before every request after the first. A row
    whose review page an earlier pass opened keeps that result and is not
    requested again, unless that pass found no posting date on it. A review page is requested for at most the declared number
    of rows; the rest wait for the next pass. Nothing is retried.

    A listing request the site refuses fails the pass. A review page request
    it refuses ends the pass there: the pages already opened are kept, the
    reason is recorded, and the rest wait for the next pass.
    """
    validate_declaration(source)
    delay = _bounded_int(source, "kirkus_request_delay_seconds", MIN_REQUEST_DELAY_SECONDS, MAX_REQUEST_DELAY_SECONDS)
    max_pages = _bounded_int(source, "kirkus_max_listing_pages", 1, MAX_LISTING_PAGES)
    requests = 0

    def get(url: str) -> str:
        nonlocal requests
        if requests:
            pause(float(delay))
        requests += 1
        return _body(fetch(url), url)

    opened: dict[str, Mapping[str, Any]] = {}
    if previous is not None:
        for row in validate_collection(previous, source):
            if row["page_fetched_date"]:
                opened[row["review_url"]] = row
    pages: list[dict[str, Any]] = []
    rows: dict[str, dict[str, Any]] = {}
    for number in range(1, max_pages + 1):
        url = listing_page_url(number)
        body = get(url)
        page_rows = parse_listing_page(body)
        pages.append(
            {
                "page": number,
                "url": url,
                "status": 200,
                "sha256": _digest(body),
                "body_bytes": len(body.encode("utf-8")),
                "row_count": len(page_rows),
            }
        )
        if not page_rows:
            break
        for row in page_rows:
            # A book that moves between two page requests is listed twice; the first card is kept.
            rows.setdefault(row["review_url"], row)
    if not rows:
        _fail("kirkus_page_shape", "Kirkus listing lists no book")
    if pages[-1]["row_count"]:
        _fail("kirkus_listing_cap", f"Kirkus listing is longer than the declared {max_pages} pages")
    budget = _bounded_int(source, "kirkus_max_detail_pages", 1, MAX_DETAIL_PAGES)
    stop_reason = ""
    for row in rows.values():
        if row["rejection"]:
            continue
        earlier = opened.get(row["review_url"])
        if (
            earlier is not None
            and earlier["rejection"] not in _REOPENED_REJECTIONS
            and all(earlier[field] == row[field] for field in ("title", "authors", "card_publication_date"))
        ):
            for field in ("rejection", "page_fetched_date", "page_sha256", "detail"):
                row[field] = earlier[field]
            continue
        if not budget or stop_reason:
            continue
        budget -= 1
        try:
            body = get(row["review_url"])
        except KirkusError as exc:
            stop_reason = exc.reason_code
            continue
        row["detail"], row["rejection"] = parse_review_page(body, row, reference)
        row["page_fetched_date"] = reference.isoformat()
        row["page_sha256"] = _digest(body)
    collection = {
        "version": 1,
        "source_url": SOURCE_URL,
        "collected_date": reference.isoformat(),
        "listing_pages": pages,
        "rows": list(rows.values()),
        "stop_reason": stop_reason,
    }
    validate_collection(collection, source)
    return collection


def _checked_row(row: Any) -> dict[str, Any]:
    if not isinstance(row, Mapping) or set(row) != _ROW_FIELDS:
        raise ValueError("row is not valid retained metadata")
    detail, rejection, fetched = row["detail"], row["rejection"], row["page_fetched_date"]
    if (
        any(not isinstance(row[field], str) for field in ("review_url", "title", "genre_label"))
        or not row["review_url"].startswith(ORIGIN)
        or _REVIEW_PATH_RE.fullmatch(row["review_url"][len(ORIGIN) :]) is None
        or not row["title"].strip()
        or not row["genre_label"].strip()
        or not isinstance(row["authors"], list)
        or any(not isinstance(name, str) or not name.strip() for name in row["authors"])
        or type(row["starred"]) is not bool
        or not isinstance(row["card_publication_date"], str)
        or not isinstance(rejection, str)
        or not isinstance(fetched, str)
        or not isinstance(row["page_sha256"], str)
    ):
        raise ValueError("row is not valid retained metadata")
    if row["card_publication_date"]:
        date.fromisoformat(row["card_publication_date"])
    if rejection in _CARD_REJECTIONS:
        # A card rejected as listed is never opened.
        if fetched or row["page_sha256"] or detail is not None:
            raise ValueError("a rejected card carries review page evidence")
        return dict(row)
    if not row["authors"]:
        raise ValueError("an accepted card lacks its author")
    if not fetched:
        if rejection or row["page_sha256"] or detail is not None:
            raise ValueError("an unopened row carries review page evidence")
        return dict(row)
    date.fromisoformat(fetched)
    if _SHA256_RE.fullmatch(row["page_sha256"]) is None:
        raise ValueError("an opened row lacks its review page digest")
    if rejection:
        if rejection not in _PAGE_REJECTIONS or detail is not None:
            raise ValueError("a rejected review page carries facts")
        return dict(row)
    if (
        not isinstance(detail, Mapping)
        or set(detail) != _DETAIL_FIELDS
        or any(not isinstance(detail[field], str) for field in _DETAIL_FIELDS)
        or not _valid_isbn13(detail["isbn"])
        or not detail["publisher"].strip()
        or detail["publisher"].casefold() == "none"
        or detail["publication_date"] != (row["card_publication_date"] or detail["publication_date"])
    ):
        raise ValueError("review page facts are not valid retained metadata")
    date.fromisoformat(detail["publication_date"])
    date.fromisoformat(detail["review_date"])
    return {**row, "detail": dict(detail)}


def validate_collection(collection: Mapping[str, Any], source: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Revalidate the result of a pre-collection pass and return its rows in listing order.

    Page bodies are not retained: a review page carries the review, so a pass
    keeps each response's digest and the parsed rows.
    """
    validate_declaration(source)
    if not isinstance(collection, Mapping):
        _fail("kirkus_evidence_identity", "Kirkus collection must be an object")
    max_pages = _bounded_int(source, "kirkus_max_listing_pages", 1, MAX_LISTING_PAGES)
    try:
        collected = date.fromisoformat(collection["collected_date"])
        pages = collection["listing_pages"]
        if (
            set(collection) != {"version", "source_url", "collected_date", "listing_pages", "rows", "stop_reason"}
            or collection["stop_reason"] not in _STOP_REASONS
            or type(collection["version"]) is not int
            or collection["version"] != 1
            or collection["source_url"] != SOURCE_URL
            or collected.isoformat() != collection["collected_date"]
            or not isinstance(pages, list)
            or not 2 <= len(pages) <= max_pages
            or not isinstance(collection["rows"], list)
            or not collection["rows"]
        ):
            raise ValueError("wrong collection identity")
        for number, page in enumerate(pages, start=1):
            if (
                set(page) != {"page", "url", "status", "sha256", "body_bytes", "row_count"}
                or page["page"] != number
                or page["url"] != listing_page_url(number)
                or page["status"] != 200
                or _SHA256_RE.fullmatch(page["sha256"]) is None
                or type(page["body_bytes"]) is not int
                or page["body_bytes"] <= 0
                or type(page["row_count"]) is not int
                # Every page but the last lists books; the last one is the empty page that ends the list.
                or (page["row_count"] > 0) != (number < len(pages))
            ):
                raise ValueError("wrong listing page observation")
        rows = [_checked_row(row) for row in collection["rows"]]
        if len({row["review_url"] for row in rows}) != len(rows) or len(rows) > sum(p["row_count"] for p in pages):
            raise ValueError("wrong row set")
        books: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row["detail"] is None:
                continue
            if date.fromisoformat(row["page_fetched_date"]) > collected:
                raise ValueError("a review page was opened after the pass")
            other = books.setdefault(row["detail"]["isbn"], row)
            if other is not row:
                _fail("kirkus_row_identity", "Kirkus rows give one ISBN to two reviews")
    except KirkusError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        _fail("kirkus_evidence_identity", "Kirkus collection identity, page or row evidence is invalid")
    return rows


def collection_path(config_path: Path, source: Mapping[str, Any]) -> Path | None:
    """The declared result file of the pre-collection step, resolved beside the config that names it."""
    raw_path = str(source.get("kirkus_collection_file", "")).strip()
    return (config_path.parent / raw_path).resolve() if raw_path else None


def read_collection(path: Path | None) -> dict[str, Any] | None:
    """Read the last pass, or None when no pass has written the declared file."""
    if path is None or not path.exists():
        return None
    try:
        data = read_json(path, default=None)
    except json.JSONDecodeError:
        _fail("kirkus_collection_state", "Kirkus pre-collected result is malformed JSON")
    if not isinstance(data, dict):
        _fail("kirkus_collection_state", "Kirkus pre-collected result has an invalid shape")
    return data


def attempt_path(config_path: Path, source: Mapping[str, Any]) -> Path | None:
    """The record of the last pass attempted, kept beside the result file."""
    path = collection_path(config_path, source)
    return None if path is None else path.with_name(path.stem + "-attempt.json")


def read_attempt(path: Path | None) -> dict[str, str]:
    """The last pass attempted: its date, status and reason code. {} when none is recorded or readable."""
    if path is None or not path.exists():
        return {}
    try:
        data = read_json(path, default=None)
        if not isinstance(data, dict) or any(not isinstance(value, str) for value in data.values()):
            return {}
        date.fromisoformat(data["attempted_date"])
        return {field: data.get(field, "") for field in ("attempted_date", "status", "reason_code")}
    except (json.JSONDecodeError, KeyError, ValueError, OSError):
        return {}


def failed_refresh_debug(attempt: Mapping[str, str], bundle: Mapping[str, Any]) -> dict[str, Any]:
    """The incomplete-coverage record of a run that reads an earlier result because its own pass failed, or {}.

    The pass of the run date wrote no result, so the run reports the books of
    an earlier pass and none listed since. That is a shortfall the weekly
    summary, the source health check and the review must name.
    """
    collection = bundle["collection"]
    if (
        attempt.get("attempted_date") != bundle["reference_date"]
        or attempt.get("status") not in {"started", "not_collected"}
        or collection["collected_date"] == bundle["reference_date"]
    ):
        return {}
    read = sum(bool(row["page_fetched_date"]) for row in collection["rows"])
    return {
        "incomplete_ranking": True,
        "incomplete_ranking_warning": (
            f"incomplete collection: the Kirkus pre-collection pass of {bundle['reference_date']} failed "
            f"({attempt.get('reason_code') or 'interrupted'}); the result of {collection['collected_date']} is used"
        ),
        "raw_row_count": read,
        "expected_raw_row_count": read + waiting_row_count(collection),
    }


def seen_items_path(config_path: Path, source: Mapping[str, Any]) -> Path | None:
    """The declared seen-item index, resolved beside the config that names it."""
    raw_path = str(source.get("seen_guids_file", "")).strip()
    return (config_path.parent / raw_path).resolve() if raw_path else None


def read_seen_items(path: Path | None) -> dict[str, str]:
    """Read the seen-item index: ISBN to the run date that first reported it."""
    if path is None:
        return {}
    try:
        data = read_json(path, default={})
    except json.JSONDecodeError:
        _fail("kirkus_seen_state", "Kirkus seen-item state is malformed JSON")
    if not isinstance(data, dict) or not isinstance(data.get("seen_guids", []), list):
        _fail("kirkus_seen_state", "Kirkus seen-item state has an invalid shape")
    seen: dict[str, str] = {}
    for entry in data.get("seen_guids", []):
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("guid"), str)
            or not _valid_isbn13(entry["guid"])
            or not isinstance(entry.get("first_seen_date"), str)
        ):
            _fail("kirkus_seen_state", "Kirkus seen-item state has an invalid entry")
        try:
            date.fromisoformat(entry["first_seen_date"])
        except ValueError:
            _fail("kirkus_seen_state", "Kirkus seen-item state has an invalid entry")
        seen[entry["guid"]] = entry["first_seen_date"]
    return seen


def record_seen_items(
    config_path: Path,
    source: Mapping[str, Any],
    page: Mapping[str, Any],
    accepted_isbns: set[str],
) -> int:
    """Record the rows an extraction of this capture reported, keeping an earlier first-seen date.

    Called once the extraction of a capture has produced accepted mentions.
    Every row given to the extractor is nonfiction by the site's own label, so
    a row it did not return was lost, not decided: only the accepted rows are
    recorded and every other row is shown again. Returns how many ISBNs were
    newly recorded.
    """
    path = seen_items_path(config_path, source)
    if path is None:
        return 0
    bundle = page.get(EVIDENCE_FIELD, {})
    rows = qualifying_rows(validate_bundle(bundle, source), source)
    seen = read_seen_items(path)
    new = [row["isbn"] for row in rows if row["isbn"] in accepted_isbns and row["isbn"] not in seen]
    if not new:
        return 0
    for isbn in new:
        seen[isbn] = bundle["reference_date"]
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(
        path,
        {
            "source": str(source.get("name", "")),
            "updated_at": bundle["reference_date"],
            "seen_guids": [{"guid": isbn, "first_seen_date": first} for isbn, first in seen.items()],
        },
    )
    return len(new)


def capture(
    source: Mapping[str, Any],
    reference: date,
    collection: Mapping[str, Any] | None,
    seen: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Bind the last pre-collection pass to one run. This requests nothing.

    A missing pass and a pass older than the declared age are named refusals,
    so the weekly run reports the source and carries on without it. So is a
    pass the site cut short that left no row to report: nothing new because a
    request was refused is a collection failure, not a quiet week.
    """
    validate_declaration(source)
    if collection is None:
        _fail("kirkus_collection_missing", "Kirkus pre-collected result is missing; run the pre-collection step")
    rows = validate_collection(collection, source)
    reported = {row["detail"]["isbn"] for row in rows if row["detail"] is not None}
    bundle = {
        "version": 1,
        "source_url": SOURCE_URL,
        "reference_date": reference.isoformat(),
        "collection": dict(collection),
        # A book reported by an earlier run is seen. One first reported today is shown again, so a
        # second run on the same date does not lose what the first one captured.
        "seen_items": [
            {"isbn": isbn, "first_seen_date": first}
            for isbn, first in sorted((seen or {}).items())
            if isbn in reported and first < reference.isoformat()
        ],
    }
    rows = validate_bundle(bundle, source)
    if collection["stop_reason"] and not qualifying_rows(rows, source):
        _fail(
            "kirkus_partial_collection",
            "Kirkus pre-collection stopped before its end and holds no new row; run the step again later",
        )
    return bundle


def waiting_row_count(collection: Mapping[str, Any]) -> int:
    """How many listed books still wait for a pass to open their review page."""
    return sum(not row["rejection"] and not row["page_fetched_date"] for row in collection["rows"])


def partial_collection_debug(page: Mapping[str, Any]) -> dict[str, Any]:
    """The incomplete-coverage record of a capture whose pass the site cut short, or {}.

    A pass that stopped before its end still reports the rows it reached.
    The shortfall is recorded in the form every incomplete source uses, so the
    weekly summary, the source health check and the review name it. A pass that
    only ran into its declared cap is not a shortfall: the next pass continues.
    Never raises.
    """
    bundle = page.get(EVIDENCE_FIELD)
    collection = bundle.get("collection") if isinstance(bundle, Mapping) else None
    if not isinstance(collection, Mapping) or not collection.get("stop_reason"):
        return {}
    try:
        waiting = waiting_row_count(collection)
        read = sum(bool(row["page_fetched_date"]) for row in collection["rows"])
    except (KeyError, TypeError):
        return {}
    return {
        "incomplete_ranking": True,
        "incomplete_ranking_warning": (
            f"incomplete collection: the Kirkus pre-collection pass stopped before its end "
            f"({collection['stop_reason']}) after {read} of {read + waiting} listed books"
        ),
        "raw_row_count": read,
        "expected_raw_row_count": read + waiting,
    }


# Rows a pass leaves out by design: a review its author paid for, and a self-published book.
EXCLUDED_BY_DESIGN = frozenset({"paid_review", "self_published"})


def _dropped_rows(bundle: Mapping[str, Any], source: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Every listed book the run does not report, as (reason, title), in listing order. Raises on bad evidence."""
    dropped: list[tuple[str, str]] = []
    for row in bundle["collection"]["rows"]:
        if row["rejection"]:
            dropped.append((row["rejection"], str(row["title"])))
    labels = genre_labels(source)
    max_age = _bounded_int(source, "kirkus_max_review_age_days", 1, MAX_REVIEW_AGE_DAYS)
    for row in validate_bundle(bundle, source):
        if row["seen"]:
            continue
        if row["genre_label"].casefold() not in labels:
            reason = "undeclared_genre_label"
        elif not -1 <= row["review_age_days"] <= max_age:
            reason = "review_outside_age_limit"
        else:
            continue
        dropped.append((reason, str(row["title"])))
    return dropped


def dropped_row_counts(bundle: Mapping[str, Any], source: Mapping[str, Any]) -> dict[str, int]:
    """How many listed books the run does not report, by reason, apart from books already reported.

    A card or review page the pass rejected carries its rejection. An opened,
    accepted book that is not yet reported is left out for an undeclared genre
    label or a review outside the age limit. Never raises.
    """
    counts: dict[str, int] = {}
    try:
        for reason, _title in _dropped_rows(bundle, source):
            counts[reason] = counts.get(reason, 0) + 1
    except (KirkusError, KeyError, TypeError, ValueError):
        return {}
    return dict(sorted(counts.items()))


# The review names at most this many left-out books; the count still covers every one.
MAX_LEFT_OUT_TITLES = 10


def left_out_titles(bundle: Mapping[str, Any], source: Mapping[str, Any]) -> list[str]:
    """The titles of listed books left out for a reason other than a paid or self-published review (#913).

    Bounded to `MAX_LEFT_OUT_TITLES`, in listing order. Never raises.
    """
    try:
        dropped = _dropped_rows(bundle, source)
    except (KirkusError, KeyError, TypeError, ValueError):
        return []
    return [title for reason, title in dropped if reason not in EXCLUDED_BY_DESIGN][:MAX_LEFT_OUT_TITLES]


def coverage_debug(page: Mapping[str, Any], source: Mapping[str, Any], *records: Mapping[str, Any]) -> dict[str, Any]:
    """Every coverage record of one Kirkus capture: a cut-short pass, the books left out, and `records`.

    Kirkus is a source the editor must not lose a book from (#873). Every
    listed book the run does not report is counted by reason. A book left out
    for any reason other than a paid or self-published review is named in the
    caveat, so the weekly summary and the review show it. Never raises.
    """
    merged: dict[str, Any] = {}
    warnings: list[str] = []
    for record in (partial_collection_debug(page), *records):
        merged.update(record)
        if record.get("incomplete_ranking_warning"):
            warnings.append(record["incomplete_ranking_warning"])
    bundle = page.get(EVIDENCE_FIELD)
    counts = dropped_row_counts(bundle, source) if isinstance(bundle, Mapping) else {}
    merged["kirkus_dropped_rows"] = counts
    unexpected = {reason: count for reason, count in counts.items() if reason not in EXCLUDED_BY_DESIGN}
    if unexpected:
        # The editor sees which books are missing, not only how many (#913).
        merged["left_out_titles"] = left_out_titles(bundle, source)  # type: ignore[arg-type]
        total = sum(unexpected.values())
        reasons = ", ".join(f"{reason} {count}" for reason, count in unexpected.items())
        warnings.append(f"Kirkus left out {total} listed book{'s' if total != 1 else ''} ({reasons})")
        if not merged.get("incomplete_ranking"):
            # The shortfall is reported in the form every incomplete source uses, so the review names it.
            # A pass cut short already reports its own counts, and the caveat names both shortfalls.
            listed = len(bundle["collection"]["rows"])  # type: ignore[index]
            merged.update(incomplete_ranking=True, raw_row_count=listed - total, expected_raw_row_count=listed)
    if warnings:
        merged["incomplete_ranking_warning"] = "; ".join(warnings)
    return merged


def validate_bundle(bundle: Mapping[str, Any], source: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Revalidate a retained capture and return one row per opened, accepted book, in listing order."""
    validate_declaration(source)
    if not isinstance(bundle, Mapping):
        _fail("kirkus_evidence_identity", "Kirkus bundle must be an object")
    max_age = _bounded_int(source, "kirkus_max_collection_age_days", 1, MAX_COLLECTION_AGE_DAYS)
    try:
        reference = date.fromisoformat(bundle["reference_date"])
        collection = bundle["collection"]
        seen_items = bundle["seen_items"]
        if (
            set(bundle) != {"version", "source_url", "reference_date", "collection", "seen_items"}
            or type(bundle["version"]) is not int
            or bundle["version"] != 1
            or bundle["source_url"] != SOURCE_URL
            or reference.isoformat() != bundle["reference_date"]
            or not isinstance(seen_items, list)
        ):
            raise ValueError("wrong bundle identity")
        seen: dict[str, str] = {}
        for item in seen_items:
            if not _valid_isbn13(item["isbn"]) or date.fromisoformat(item["first_seen_date"]) >= reference:
                raise ValueError("wrong seen item")
            seen[item["isbn"]] = item["first_seen_date"]
    except (KeyError, TypeError, ValueError, AttributeError):
        _fail("kirkus_evidence_identity", "Kirkus bundle identity or seen-item evidence is invalid")
    collected_rows = validate_collection(collection, source)
    collected = date.fromisoformat(collection["collected_date"])
    # The pass date is the operator's and the run date may be another machine's, so one day ahead is not the future.
    if not -1 <= (reference - collected).days <= max_age:
        _fail("kirkus_stale_collection", f"Kirkus pre-collected result is outside the {max_age}-day age limit")
    rows: list[dict[str, Any]] = []
    for row in collected_rows:
        detail = row["detail"]
        if detail is None:
            continue
        rows.append(
            {
                "title": row["title"],
                "primary_authors": list(row["authors"]),
                "publisher": detail["publisher"],
                "publication_year": int(detail["publication_date"][:4]),
                "publication_date": detail["publication_date"],
                "isbn": detail["isbn"],
                "evidence_url": row["review_url"],
                "genre_label": row["genre_label"],
                "starred": row["starred"],
                # A row is bound to the date its review was posted.
                "source_week": detail["review_date"],
                "review_age_days": (collected - date.fromisoformat(detail["review_date"])).days,
                "seen": detail["isbn"] in seen,
            }
        )
    return rows


def qualifying_rows(rows: list[dict[str, Any]], source: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Rows the extractor is given: an accepted genre label, a review inside the age limit, not seen before."""
    labels = genre_labels(source)
    max_age = _bounded_int(source, "kirkus_max_review_age_days", 1, MAX_REVIEW_AGE_DAYS)
    return [
        row
        for row in rows
        if row["genre_label"].casefold() in labels and -1 <= row["review_age_days"] <= max_age and not row["seen"]
    ]


def model_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The fields the extractor is given for each qualifying row."""
    fields = ("title", "primary_authors", "publisher", "publication_year", "isbn", "source_week", "evidence_url")
    return [{field: row[field] for field in fields} for row in rows]


def model_text(bundle: Mapping[str, Any], source: Mapping[str, Any]) -> str:
    """The validated qualifying rows as the extractor reads them."""
    return json.dumps(model_rows(qualifying_rows(validate_bundle(bundle, source), source)), ensure_ascii=False)


def page_data(bundle: dict[str, Any], source: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source_page": SOURCE_URL,
        "source_url": SOURCE_URL,
        "html": "",
        "reference_date": bundle["reference_date"],
        EVIDENCE_FIELD: bundle,
        "text": model_text(bundle, source),
        "contract_family": FAMILY,
    }


def retained_capture_fields(page: Mapping[str, Any], source: Mapping[str, Any]) -> dict[str, Any]:
    """The fields a saved capture keeps: the bundle, and the rows rebuilt from it."""
    bundle = page.get(EVIDENCE_FIELD, {})
    return {"contract_family": FAMILY, EVIDENCE_FIELD: bundle, "text": model_text(bundle, source)}


def validate_page_identity(page: Mapping[str, Any], source: Mapping[str, Any], active_run_date: str = "") -> None:
    """Refuse a capture whose identity or reference date disagrees with its bundle."""
    bundle = page.get(EVIDENCE_FIELD, {})
    validate_bundle(bundle, source)
    if page.get("source_page") != SOURCE_URL or page.get("reference_date") != bundle["reference_date"]:
        _fail("kirkus_evidence_identity", "Kirkus capture identity disagrees with its bundle")
    if active_run_date and bundle["reference_date"] != active_run_date:
        _fail("kirkus_evidence_identity", "Kirkus reference date disagrees with active operation")


def evidence_url_grounded(page: Mapping[str, Any], evidence_url: str) -> bool:
    """True when the URL is a review link a retained listing row carries."""
    bundle = page.get(EVIDENCE_FIELD)
    if page.get("contract_family") != FAMILY or not isinstance(bundle, Mapping):
        return False
    collection = bundle.get("collection")
    rows = collection.get("rows") if isinstance(collection, Mapping) else None
    if not isinstance(rows, list):
        return False
    return any(isinstance(row, Mapping) and row.get("review_url") == evidence_url for row in rows)


def mention_violation(entry: Mapping[str, Any], page: Mapping[str, Any], source: Mapping[str, Any]) -> str:
    """Refuse a mention whose identity is not exactly one qualifying captured row.

    Every identity field is read from the listing by code. The extractor may
    not supply or correct a title, author, publisher, year or ISBN.
    """
    try:
        rows = qualifying_rows(validate_bundle(page.get(EVIDENCE_FIELD, {}), source), source)
    except KirkusError as exc:
        return str(exc)
    # The rows carry no description, status or fit evidence, so a mention may not carry any either.
    prohibited = sorted(set(entry) - _MENTION_FIELDS)
    if prohibited:
        return f"Kirkus mention contains fields the rows do not support: {', '.join(prohibited)}"
    for row in rows:
        if (
            entry.get("title") == row["title"]
            and entry.get("primary_authors") == row["primary_authors"]
            and entry.get("publisher") == row["publisher"]
            and type(entry.get("publication_year")) is int
            and entry.get("publication_year") == row["publication_year"]
            and entry.get("isbn") == row["isbn"]
            and entry.get("source_week") == row["source_week"]
            and entry.get("source_url") == SOURCE_URL
            and entry.get("evidence_url") == row["evidence_url"]
            and entry.get("signal_type") == SIGNAL_TYPE
            and entry.get("signal_strength") == signal_strength(source)
            and entry.get("candidate_category") == source.get("eligible_candidate_category", "adult nonfiction")
        ):
            return ""
    return "Kirkus mention does not agree with a qualifying captured listing row"


def feasibility(evidence: Mapping[str, Any], source: Mapping[str, Any]) -> FeasibilityVerdict:
    from autoscout.source_feasibility import EntrySample
    from autoscout.source_feasibility import FeasibilityVerdict

    try:
        validate_page_identity(evidence, source)
        bundle = evidence[EVIDENCE_FIELD]
        all_rows = validate_bundle(bundle, source)
        rows = qualifying_rows(all_rows, source)
        if not rows:
            _fail("kirkus_no_qualifying_rows", "Kirkus listing has no qualifying rows")
    except KirkusError as exc:
        return FeasibilityVerdict(False, str(exc), reason_code=exc.reason_code, contract_family=FAMILY)
    dates = sorted(row["source_week"] for row in rows)
    return FeasibilityVerdict(
        True,
        "bounded coming-soon review listing accepted",
        reason_code="ok",
        contract_family=FAMILY,
        detected_heading=PAGE_HEADING,
        bounded_entry_count=len(rows),
        raw_row_count=len(bundle["collection"]["rows"]),
        reporting_period=f"{dates[0]}/{dates[-1]}",
        samples=tuple(
            EntrySample(
                row["title"],
                " / ".join(row["primary_authors"]),
                row["evidence_url"],
                identifier=row["isbn"],
                category="adult nonfiction",
                reporting_period=row["source_week"],
            )
            for row in rows[:3]
        ),
    )
