"""Explicit Perlentaucher daily review digest evidence, shared by probe, live capture and replay.

The stable index page names the current dated daily pages. Each daily page
lists the books reviewed that day, one row per review. Title, authors,
publisher, year and ISBN are read from the row by code. The row ends with the
opening of the publisher blurb; it is read once, only to see whether it opens
with a translator note, and is never retained. Book pages are not requested.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from collections.abc import Mapping
from datetime import date
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import NoReturn

from autoscout.http_decoding import MAX_RESPONSE_BYTES
from autoscout.json_io import read_json
from autoscout.json_io import write_json

if TYPE_CHECKING:
    from autoscout.source_feasibility import FeasibilityVerdict

FAMILY = "perlentaucher-daily-digest"
SOURCE_URL = "https://www.perlentaucher.de/buecherschau"
ORIGIN = "https://www.perlentaucher.de"
EVIDENCE_FIELD = "perlentaucher_evidence"
PAGE_HEADING = "Bücherschau des Tages"
TRANSLATION_POLICY = "exclude-translator-note"
MAX_DAILY_PAGES = 8
MAX_AGE_DAYS = 14
SIGNAL_TYPE = "media/curated list"
DECLARATION_FIELDS = (
    "perlentaucher_max_daily_pages",
    "perlentaucher_max_age_days",
    "perlentaucher_translation_policy",
    "adult_nonfiction_exclusions",
)

_DAILY_PATH_RE = re.compile(r"/buecherschau/(?P<day>[0-9]{4}-[0-9]{2}-[0-9]{2})\.html")
_BOOK_PATH_RE = re.compile(r"/buch/[a-z0-9-]+/[a-z0-9-]+\.html")
# An edited volume names no author, so its book link has no author segment.
_AUTHORLESS_BOOK_PATH_RE = re.compile(r"/buch/[a-z0-9-]+\.html")
_PRINTED_DATE_RE = re.compile(r"(?P<day>[0-9]{2}\.[0-9]{2}\.[0-9]{4})\b")
_PUBLISHER_LINE_RE = re.compile(r"(?P<publisher>.+), (?P<place>[^,]+) (?P<year>[0-9]{4})")
_ISBN_RE = re.compile(r"ISBN\s+(?P<isbn>[0-9]{13})\b")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
# A blurb that opens "Aus dem Französischen von ...", "Aus dem amerikanischen Englisch von ...",
# "Aus dem Englischen übersetzt von ..." or "Aus dem Russischen und mit einem Nachwort von ...".
# A language is named by an adjective in -isch or -ischen, or by one of the names below that take no
# such ending. "Aus dem Leben von ..." names no language and is not a translator note.
_UNINFLECTED_LANGUAGES = (
    "Afrikaans|Bengali|Dari|Esperanto|Farsi|Gujarati|Hindi|Igbo|Ivrit|Kannada|Khmer|Kurmandschi|Latein|"
    "Malayalam|Mandarin|Marathi|Nepali|Paschtu|Punjabi|Quechua|Sanskrit|Sorani|Suaheli|Swahili|Tagalog|"
    "Tamil|Telugu|Thai|Tigrinya|Urdu|Xhosa|Yoruba|Zulu"
)
_TRANSLATOR_NOTE_RE = re.compile(
    rf"Aus dem (?:(?:[\w-]+ )?[\w-]+isch(?:en)?(?: [\w-]+)?|{_UNINFLECTED_LANGUAGES})"
    r"(?: (?:übersetzt|übertragen|und)[^.]{0,80}?)? von "
)
_ROW_REJECTIONS = frozenset(
    {"missing_author", "invalid_isbn", "publisher_line", "missing_paper", "missing_book_link", "missing_title"}
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


class PerlentaucherError(ValueError):
    """A named refusal that callers can report without repairing evidence."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


def _fail(code: str, message: str) -> NoReturn:
    raise PerlentaucherError(code, message)


def _bounded_int(source: Mapping[str, Any], field: str, ceiling: int) -> int:
    value = source.get(field)
    if type(value) is not int or not 1 <= value <= ceiling:
        _fail("perlentaucher_declaration", f"Perlentaucher digest requires {field} between 1 and {ceiling}")
    return value


def validate_declaration(source: Mapping[str, Any]) -> None:
    if source.get("url") != SOURCE_URL:
        _fail("perlentaucher_declaration", "Perlentaucher digest requires its canonical index URL")
    _bounded_int(source, "perlentaucher_max_daily_pages", MAX_DAILY_PAGES)
    _bounded_int(source, "perlentaucher_max_age_days", MAX_AGE_DAYS)
    if source.get("perlentaucher_translation_policy") != TRANSLATION_POLICY:
        _fail("perlentaucher_declaration", f"Perlentaucher digest requires explicit {TRANSLATION_POLICY} policy")
    exclusions = source.get("adult_nonfiction_exclusions", [])
    if not isinstance(exclusions, list) or any(not isinstance(x, str) or not x.strip() for x in exclusions):
        _fail("perlentaucher_declaration", "Perlentaucher digest exclusions must be a list of nonblank genre labels")


def signal_strength(source: Mapping[str, Any]) -> str:
    """One review notice is a minor mention unless the source declares otherwise."""
    return str(source.get("expected_signal_strength", "")).strip() or "minor mention"


def _text(parts: list[str]) -> str:
    return " ".join("".join(parts).split())


def _valid_isbn13(isbn: str) -> bool:
    return (
        re.fullmatch(r"97[89][0-9]{10}", isbn) is not None
        and sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(isbn)) % 10 == 0
    )


def has_translator_note(blurb_opening: str) -> bool:
    """True when a row's blurb opens with a translator note.

    The note says a translator is credited. Its absence does not show that the
    book was written in German.
    """
    return _TRANSLATOR_NOTE_RE.match(blurb_opening) is not None


_VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"})
_TEXT_ROLES = frozenset({"heading", "date", "paper", "author", "span", "small", "blurb"})


class _DailyPage(HTMLParser):
    """Read the dated heading, the paper headings and the book rows of one daily page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headings: list[str] = []
        self.printed_dates: list[str] = []
        self.rows: list[dict[str, Any]] = []
        self._paper = ""
        self._row: dict[str, Any] | None = None
        self._stack: list[tuple[str, str, list[str]]] = []

    def _inside(self, role: str) -> bool:
        return any(name == role for _, name, _ in self._stack)

    def _role(self, tag: str, attrs: dict[str, str | None]) -> str:
        classes = set((attrs.get("class") or "").split())
        if not self._inside("main"):
            return "main" if tag == "main" else ""
        row = self._row
        if row is None:
            if tag == "article" and "book" in classes:
                self._row = {"paper": self._paper, "href": None, "author": "", "spans": [], "small": "", "blurb": ""}
                return "row"
            if tag == "h4":
                return "heading"
            if tag == "span" and "date" in classes:
                return "date"
            if tag == "h2" and "newspaper" in classes:
                return "paper"
            return ""
        if tag == "h2" and row["href"] is None and not self._inside("row-heading"):
            return "row-heading"
        if tag == "a" and self._inside("row-heading") and row["href"] is None:
            row["href"] = attrs.get("href") or ""
            return "author"
        if tag == "span" and self._inside("author"):
            return "span"
        if tag == "div" and "subline" in classes:
            return "subline"
        if tag == "small" and self._inside("subline") and not row["small"]:
            return "small"
        if tag == "div" and "paragraph" in classes and not row["blurb"]:
            return "blurb"
        return ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID_TAGS:
            if tag == "br":
                self.handle_data("\n")
            return
        self._stack.append((tag, self._role(tag, dict(attrs)), []))

    def handle_endtag(self, tag: str) -> None:
        if not any(open_tag == tag for open_tag, _, _ in self._stack):
            return
        while self._stack:
            open_tag, role, parts = self._stack.pop()
            self._closed(role, "".join(parts))
            if open_tag == tag:
                break

    def _closed(self, role: str, text: str) -> None:
        row = self._row
        if role == "heading":
            self.headings.append(_text([text]))
        elif role == "date":
            self.printed_dates.append(_text([text]))
        elif role == "paper":
            self._paper = _text([text])
        elif row is None:
            return
        elif role == "author":
            row["author"] = _text([text])
        elif role == "span":
            row["spans"].append(_text([text]))
        elif role == "small":
            row["small"] = text
        elif role == "blurb":
            row["blurb"] = _text([text])
        elif role == "row":
            self.rows.append(row)
            self._row = None

    def handle_data(self, data: str) -> None:
        for _, role, parts in reversed(self._stack):
            if role in _TEXT_ROLES:
                parts.append(data)
                return


def _row(raw: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Turn one parsed row into retained metadata, or name why the row is rejected."""
    href = raw["href"]
    spans = [span for span in raw["spans"] if span]
    authorless = isinstance(href, str) and _AUTHORLESS_BOOK_PATH_RE.fullmatch(href) is not None
    # Check link, title, then paper so each malformed row has a deterministic reason.
    if not isinstance(href, str) or not (authorless or _BOOK_PATH_RE.fullmatch(href)):
        return None, "missing_book_link"
    if not spans:
        return None, "missing_title"
    title = spans[0].removesuffix(".").strip()
    if not title:
        return None, "missing_title"
    if not raw["paper"]:
        return None, "missing_paper"
    # A book without an author is one rejected row, not a changed page (2026-10-24, an edited volume).
    if authorless:
        return None, "missing_author"
    authors = [name.strip() for name in raw["author"].removesuffix(":").split("/") if name.strip()]
    if not authors:
        return None, "missing_author"
    # Only the publisher line before the first break and the ISBN are read; binding and price are not.
    publisher_text, _, rest = raw["small"].partition("\n")
    publisher_line = _PUBLISHER_LINE_RE.fullmatch(_text([publisher_text]))
    if publisher_line is None:
        return None, "publisher_line"
    isbn = _ISBN_RE.search(_text([rest]))
    if isbn is None or not _valid_isbn13(isbn["isbn"]):
        return None, "invalid_isbn"
    return {
        "paper": raw["paper"],
        "title": title,
        "subtitle": " ".join(spans[1:]).strip(),
        "authors": authors,
        "publisher": publisher_line["publisher"].strip(),
        "publication_year": int(publisher_line["year"]),
        "isbn": isbn["isbn"],
        "book_url": ORIGIN + href,
        "has_translator_note": has_translator_note(raw["blurb"]),
    }, ""


def parse_daily_page(page_html: str, day: date) -> tuple[list[dict[str, Any]], list[str]]:
    """Return the valid rows of one daily page and the reasons of its rejected rows."""
    parser = _DailyPage()
    parser.feed(page_html)
    parser.close()
    if PAGE_HEADING not in parser.headings:
        _fail("perlentaucher_page_shape", f"Perlentaucher daily page lacks the heading {PAGE_HEADING}")
    # The dated element continues with the day's editorial summary; only its leading date is read.
    printed = [_PRINTED_DATE_RE.match(text) for text in parser.printed_dates]
    if len(printed) != 1 or printed[0] is None:
        _fail("perlentaucher_page_shape", "Perlentaucher daily page must print exactly one date")
    if datetime.strptime(printed[0]["day"], "%d.%m.%Y").date() != day:
        _fail("perlentaucher_page_date", "Perlentaucher daily page prints a date other than the one in its URL")
    if not parser.rows:
        _fail("perlentaucher_page_shape", "Perlentaucher daily page lists no book row")
    rows: list[dict[str, Any]] = []
    rejected: list[str] = []
    for raw in parser.rows:
        row, reason = _row(raw)
        if row is None:
            rejected.append(reason)
        else:
            rows.append(row)
    return rows, rejected


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.hrefs.append(dict(attrs).get("href") or "")


def daily_page_days(index_html: str, reference: date, source: Mapping[str, Any]) -> list[str]:
    """Return the current daily page dates the index names, newest first, within the declared bounds."""
    parser = _Links()
    parser.feed(index_html)
    parser.close()
    days: set[date] = set()
    for href in parser.hrefs:
        match = _DAILY_PATH_RE.fullmatch(href[len(ORIGIN) :] if href.startswith(ORIGIN + "/") else href)
        if match is None:
            continue
        try:
            days.add(date.fromisoformat(match["day"]))
        except ValueError:
            _fail("perlentaucher_index", "Perlentaucher index links a daily page with an invalid date")
    if not days:
        _fail("perlentaucher_index", "Perlentaucher index links no dated daily page")
    max_age = _bounded_int(source, "perlentaucher_max_age_days", MAX_AGE_DAYS)
    # The page date is German local time and the run date is the operator's, so one day ahead is not a future page.
    current = sorted((day for day in days if -1 <= (reference - day).days <= max_age), reverse=True)
    if not current:
        _fail("perlentaucher_stale", f"Perlentaucher index links no daily page from the last {max_age} days")
    return [
        day.isoformat() for day in current[: _bounded_int(source, "perlentaucher_max_daily_pages", MAX_DAILY_PAGES)]
    ]


def daily_page_url(day: str) -> str:
    return f"{SOURCE_URL}/{day}.html"


def _body(response: Mapping[str, Any], url: str) -> str:
    body = response.get("body")
    if response.get("status") != 200:
        _fail("perlentaucher_http", "Perlentaucher request did not return HTTP 200")
    if response.get("truncated") or not isinstance(body, str) or len(body.encode("utf-8")) > MAX_RESPONSE_BYTES:
        _fail("perlentaucher_response_bounds", "Perlentaucher response is missing, truncated or oversized")
    if response.get("resolved_url") != url:
        _fail("perlentaucher_wrong_target", "Perlentaucher request resolved to a different URL")
    return body


def _digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


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
        _fail("perlentaucher_seen_state", "Perlentaucher seen-item state is malformed JSON")
    if not isinstance(data, dict) or not isinstance(data.get("seen_guids", []), list):
        _fail("perlentaucher_seen_state", "Perlentaucher seen-item state has an invalid shape")
    seen: dict[str, str] = {}
    for entry in data.get("seen_guids", []):
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("guid"), str)
            or not _valid_isbn13(entry["guid"])
            or not isinstance(entry.get("first_seen_date"), str)
        ):
            _fail("perlentaucher_seen_state", "Perlentaucher seen-item state has an invalid entry")
        try:
            date.fromisoformat(entry["first_seen_date"])
        except ValueError:
            _fail("perlentaucher_seen_state", "Perlentaucher seen-item state has an invalid entry")
        seen[entry["guid"]] = entry["first_seen_date"]
    return seen


def record_seen_items(
    config_path: Path,
    source: Mapping[str, Any],
    page: Mapping[str, Any],
    accepted_isbns: set[str],
    refused_entry_count: int,
) -> int:
    """Record the rows an extraction of this capture decided, keeping an earlier first-seen date.

    Called once the extraction of a capture has produced accepted mentions. A row
    the extractor left out as fiction is recorded too: it was decided, and is not
    decided again. When the contract refused any returned entry, the row that
    entry stood for cannot be told from a row left out, so only the accepted
    rows are recorded and every other row is shown again. Returns how many ISBNs
    were newly recorded.
    """
    path = seen_items_path(config_path, source)
    if path is None:
        return 0
    bundle = page.get(EVIDENCE_FIELD, {})
    rows = qualifying_rows(validate_bundle(bundle, source), source)
    seen = read_seen_items(path)
    decided = [row["isbn"] for row in rows if refused_entry_count == 0 or row["isbn"] in accepted_isbns]
    new = [isbn for isbn in decided if isbn not in seen]
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
    fetch: Callable[[str], Mapping[str, Any]],
    seen: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """One index GET plus one GET per current daily page, up to the declared cap; never retry."""
    validate_declaration(source)
    index_body = _body(fetch(SOURCE_URL), SOURCE_URL)
    days = daily_page_days(index_body, reference, source)
    pages: list[dict[str, Any]] = []
    for day in days:
        url = daily_page_url(day)
        body = _body(fetch(url), url)
        rows, rejected = parse_daily_page(body, date.fromisoformat(day))
        pages.append(
            {
                "date": day,
                "url": url,
                "status": 200,
                "sha256": _digest(body),
                "body_bytes": len(body.encode("utf-8")),
                "rows": rows,
                "rejected_rows": rejected,
            }
        )
    rejected_count = sum(len(page["rejected_rows"]) for page in pages)
    if rejected_count and not any(page["rows"] for page in pages):
        _fail(
            "perlentaucher_all_rows_rejected",
            f"Perlentaucher daily pages ground no row: {rejected_count} rows rejected",
        )
    reported = {row["isbn"] for page in pages for row in page["rows"]}
    bundle = {
        "version": 1,
        "source_url": SOURCE_URL,
        "reference_date": reference.isoformat(),
        "index": {"url": SOURCE_URL, "status": 200, "sha256": _digest(index_body), "daily_pages": days},
        "daily_pages": pages,
        # A book reported by an earlier run is seen. One first reported today is shown again, so a
        # second run on the same date does not lose what the first one captured.
        "seen_items": [
            {"isbn": isbn, "first_seen_date": first}
            for isbn, first in sorted((seen or {}).items())
            if isbn in reported and first < reference.isoformat()
        ],
    }
    validate_bundle(bundle, source)
    return bundle


def _checked_row(row: Any) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        raise ValueError("row must be an object")
    title, subtitle, authors, publisher = row["title"], row["subtitle"], row["authors"], row["publisher"]
    if (
        any(not isinstance(value, str) for value in (title, subtitle, publisher, row["paper"], row["book_url"]))
        or not title.strip()
        or not publisher.strip()
        or not row["paper"].strip()
        or not isinstance(authors, list)
        or not authors
        or any(not isinstance(name, str) or not name.strip() for name in authors)
        or type(row["publication_year"]) is not int
        or not 1000 <= row["publication_year"] <= 9999
        or not isinstance(row["isbn"], str)
        or not _valid_isbn13(row["isbn"])
        or not row["book_url"].startswith(ORIGIN)
        or _BOOK_PATH_RE.fullmatch(row["book_url"][len(ORIGIN) :]) is None
        or type(row["has_translator_note"]) is not bool
        or set(row)
        != {
            "paper",
            "title",
            "subtitle",
            "authors",
            "publisher",
            "publication_year",
            "isbn",
            "book_url",
            "has_translator_note",
        }
    ):
        raise ValueError("row is not valid retained metadata")
    return dict(row)


def validate_bundle(bundle: Mapping[str, Any], source: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Revalidate a retained capture and return one row per book, newest daily page first.

    The daily page bodies carry publisher blurbs, so the bundle retains each
    page's digest and its parsed rows, never the body.
    """
    validate_declaration(source)
    if not isinstance(bundle, Mapping):
        _fail("perlentaucher_evidence_identity", "Perlentaucher bundle must be an object")
    max_pages = _bounded_int(source, "perlentaucher_max_daily_pages", MAX_DAILY_PAGES)
    max_age = _bounded_int(source, "perlentaucher_max_age_days", MAX_AGE_DAYS)
    try:
        reference = date.fromisoformat(bundle["reference_date"])
        index = bundle["index"]
        pages = bundle["daily_pages"]
        seen_items = bundle["seen_items"]
        days = index["daily_pages"]
        if (
            type(bundle["version"]) is not int
            or bundle["version"] != 1
            or bundle["source_url"] != SOURCE_URL
            or reference.isoformat() != bundle["reference_date"]
            or index["url"] != SOURCE_URL
            or index["status"] != 200
            or _SHA256_RE.fullmatch(index["sha256"]) is None
            or not isinstance(days, list)
            or not 1 <= len(days) <= max_pages
            or days != sorted(set(days), reverse=True)
            or not isinstance(pages, list)
            or [page["date"] for page in pages] != days
            or not isinstance(seen_items, list)
        ):
            raise ValueError("wrong bundle identity")
        seen: dict[str, str] = {}
        for item in seen_items:
            if not _valid_isbn13(item["isbn"]) or date.fromisoformat(item["first_seen_date"]) >= reference:
                raise ValueError("wrong seen item")
            seen[item["isbn"]] = item["first_seen_date"]
        books: dict[str, dict[str, Any]] = {}
        for page in pages:
            day = date.fromisoformat(page["date"])
            if not -1 <= (reference - day).days <= max_age:
                _fail("perlentaucher_stale", f"Perlentaucher daily page is outside the {max_age}-day age limit")
            if (
                page["url"] != daily_page_url(page["date"])
                or page["status"] != 200
                or _SHA256_RE.fullmatch(page["sha256"]) is None
                or type(page["body_bytes"]) is not int
                or page["body_bytes"] <= 0
                or not isinstance(page["rows"], list)
                or not isinstance(page["rejected_rows"], list)
                or any(reason not in _ROW_REJECTIONS for reason in page["rejected_rows"])
                or not (page["rows"] or page["rejected_rows"])
            ):
                raise ValueError("wrong daily page observation")
            for raw in page["rows"]:
                row = _checked_row(raw)
                book = books.setdefault(
                    row["isbn"],
                    {
                        "title": row["title"],
                        "subtitle": row["subtitle"],
                        "primary_authors": list(row["authors"]),
                        "publisher": row["publisher"],
                        "publication_year": row["publication_year"],
                        "isbn": row["isbn"],
                        "evidence_url": row["book_url"],
                        "has_translator_note": row["has_translator_note"],
                        "seen": row["isbn"] in seen,
                        "review_dates": [],
                        "reviewed_in": [],
                    },
                )
                if book["evidence_url"] != row["book_url"] or book["title"] != row["title"]:
                    _fail("perlentaucher_row_identity", "Perlentaucher rows give one ISBN two different books")
                # A translator note on any captured row of the book excludes the book.
                book["has_translator_note"] = book["has_translator_note"] or row["has_translator_note"]
                if page["date"] not in book["review_dates"]:
                    book["review_dates"].append(page["date"])
                if row["paper"] not in book["reviewed_in"]:
                    book["reviewed_in"].append(row["paper"])
    except PerlentaucherError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        _fail("perlentaucher_evidence_identity", "Perlentaucher bundle identity, page or row evidence is invalid")
    rejected_count = sum(len(page["rejected_rows"]) for page in pages)
    if rejected_count and not books:
        _fail(
            "perlentaucher_all_rows_rejected",
            f"Perlentaucher daily pages ground no row: {rejected_count} rows rejected",
        )
    rows = list(books.values())
    for row in rows:
        # A book is bound to the earliest captured daily page that reviewed it.
        row["source_week"] = min(row["review_dates"])
        row["review_dates"].sort()
    return rows


def _genre_label(subtitle: str) -> str:
    """The publisher's genre label: the whole subtitle, or its last sentence."""
    return subtitle.rsplit(". ", 1)[-1].strip().casefold()


def qualifying_rows(rows: list[dict[str, Any]], source: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Rows the extractor decides on: no translator note, not seen before, no declared genre label."""
    labels = {term.strip().casefold() for term in source.get("adult_nonfiction_exclusions", [])}
    return [
        row
        for row in rows
        if not row["has_translator_note"] and not row["seen"] and _genre_label(row["subtitle"]) not in labels
    ]


def model_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The fields the extractor is given for each qualifying row."""
    fields = ("title", "subtitle", "primary_authors", "publisher", "publication_year", "isbn", "source_week")
    return [{**{field: row[field] for field in fields}, "evidence_url": row["evidence_url"]} for row in rows]


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
        _fail("perlentaucher_evidence_identity", "Perlentaucher capture identity disagrees with its bundle")
    if active_run_date and bundle["reference_date"] != active_run_date:
        _fail("perlentaucher_evidence_identity", "Perlentaucher reference date disagrees with active operation")


def evidence_url_grounded(page: Mapping[str, Any], evidence_url: str) -> bool:
    """True when the URL is a book link a retained daily page row carries."""
    bundle = page.get(EVIDENCE_FIELD)
    if page.get("contract_family") != FAMILY or not isinstance(bundle, Mapping):
        return False
    pages = bundle.get("daily_pages")
    if not isinstance(pages, list):
        return False
    return any(
        isinstance(row, Mapping) and row.get("book_url") == evidence_url
        for observed in pages
        if isinstance(observed, Mapping) and isinstance(observed.get("rows"), list)
        for row in observed["rows"]
    )


def mention_violation(entry: Mapping[str, Any], page: Mapping[str, Any], source: Mapping[str, Any]) -> str:
    """Refuse a mention whose identity is not exactly one qualifying captured row.

    The extractor decides only whether a row is adult nonfiction. It may leave a
    row out; it may not supply or correct a title, author, publisher, year or ISBN.
    """
    try:
        rows = qualifying_rows(validate_bundle(page.get(EVIDENCE_FIELD, {}), source), source)
    except PerlentaucherError as exc:
        return str(exc)
    # The rows carry no description, status or fit evidence, so a mention may not carry any either.
    prohibited = sorted(set(entry) - _MENTION_FIELDS)
    if prohibited:
        return f"Perlentaucher mention contains fields the rows do not support: {', '.join(prohibited)}"
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
    return "Perlentaucher mention does not agree with a qualifying captured daily page row"


def feasibility(evidence: Mapping[str, Any], source: Mapping[str, Any]) -> FeasibilityVerdict:
    from autoscout.source_feasibility import EntrySample
    from autoscout.source_feasibility import FeasibilityVerdict

    try:
        validate_page_identity(evidence, source)
        bundle = evidence[EVIDENCE_FIELD]
        all_rows = validate_bundle(bundle, source)
        rows = qualifying_rows(all_rows, source)
        if not rows:
            _fail("perlentaucher_no_qualifying_rows", "Perlentaucher digest has no qualifying rows")
    except PerlentaucherError as exc:
        return FeasibilityVerdict(False, str(exc), reason_code=exc.reason_code, contract_family=FAMILY)
    days = bundle["index"]["daily_pages"]
    return FeasibilityVerdict(
        True,
        "bounded dated review digest accepted",
        reason_code="ok",
        contract_family=FAMILY,
        detected_heading=PAGE_HEADING,
        bounded_entry_count=len(rows),
        raw_row_count=len(all_rows),
        reporting_period=f"{days[-1]}/{days[0]}",
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
