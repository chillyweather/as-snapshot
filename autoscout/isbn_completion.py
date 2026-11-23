"""Complete missing candidate ISBNs (issue #979).

Most sources show a book without its ISBN on the list page AutoScout reads,
although the ISBN is one step away. This module fills a blank ISBN from, in
order:

1. ``source-link``: the book link the source itself gave (Amazon ``/dp/``,
   bookshop.org, publisher pages). No network.
2. ``source-page``: the captured list page, where the book's own record
   carries the ISBN: its JSON-LD record, or its card (cover image file names,
   button ids, buy links). No network.
3. ``book-page``: the book's own page behind the source link, read from its
   structured data, an ``ISBN`` label, bookshop links, or its single ISBN.
4. ``open-library``: an Open Library edition in the source language whose
   title, author and year agree with the candidate.

Every learned ISBN is fill-only and discovery-only (#531): it never replaces
an existing ISBN and never proves that two mentions are the same work. Each
one records ``isbn_provenance`` (method, URL, date). A record that found no
ISBN after a network attempt records ``isbn_lookup_date`` and is not asked
again for :data:`RETRY_AFTER_DAYS`.

Candidate History and the latest candidate snapshot are written in one State
Store transaction; the current candidates then receive the same ISBN pair,
fill-only. That copy is a pure
function of history, so an interruption recovers by running the step again.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import time
import unicodedata
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from typing import Protocol
from urllib.parse import unquote
from urllib.parse import urlencode
from urllib.parse import urljoin
from urllib.parse import urlsplit

from autoscout.candidate_schema import CandidateIsbn
from autoscout.identity import normalize_author_display
from autoscout.identity import normalize_author_identity
from autoscout.json_io import read_json_or_default
from autoscout.learned_field_completion import SCOPE_CURRENT
from autoscout.learned_field_completion import SCOPE_UNDECIDED
from autoscout.learned_field_completion import LearnedFieldSpec
from autoscout.learned_field_completion import load_aliases
from autoscout.learned_field_completion import persist_learned_fields
from autoscout.learned_field_completion import project_fields
from autoscout.learned_field_completion import read_history
from autoscout.learned_field_completion import seal_finalization
from autoscout.learned_field_completion import select_records
from autoscout.state_store import StateStore
from autoscout.state_store import open_state_store

STAGE_NAME = "isbn-completion"
REPORT_FILENAME = "isbn-completion.json"
_SEAL_PENDING_FILENAME = ".isbn-completion-seal-pending.json"

METHOD_SOURCE_LINK = "source-link"
METHOD_SOURCE_PAGE = "source-page"
METHOD_BOOK_PAGE = "book-page"
METHOD_OPEN_LIBRARY = "open-library"
METHODS = (METHOD_SOURCE_LINK, METHOD_SOURCE_PAGE, METHOD_BOOK_PAGE, METHOD_OPEN_LIBRARY)

# A book that found no ISBN is asked again after four weeks: new books gain
# ISBNs and catalogue records, so a permanent "no" would be wrong, and a
# weekly retry would spend requests on the same misses.
RETRY_AFTER_DAYS = 28
# The stage is advisory and runs before the description stages, so it gets a
# small wall-clock budget. Offline methods keep running after it expires.
STAGE_BUDGET_SECONDS = 600.0
MIN_HOST_INTERVAL_SECONDS = 2.0
REQUEST_TIMEOUT_SECONDS = 20.0
MAX_RESPONSE_BYTES = 3_000_000
OPEN_LIBRARY_SEARCH_URL = "https://openlibrary.org/search.json"
USER_AGENT = "AutoScout/1.0 (book scouting; ISBN completion)"

# Audiobook pages carry audio-edition ISBNs (or none); a print ISBN for those
# books comes from Open Library instead.
_AUDIO_HOST_MARKERS = ("audible.", "audiobooks.com", "libro.fm")
_AUDIO_SOURCE_MARKERS = ("audible", "audiobook", "hörbuch", "hoerbuch")
# Hosts never fetched here: bot checks (Amazon, Audible, BookFinder), and
# Kirkus, which asks for 20 seconds between requests and whose books carry
# their ISBN from the Kirkus pass already.
_NO_FETCH_HOST_MARKERS = (
    "amazon.",
    "audible.",
    "bookfinder.com",
    "kirkusreviews.com",
    # AbeBooks sites disallow ``/servlet/`` for crawlers (issue #983).
    "abebooks.",
    "iberlibro.com",
    "zvab.com",
)
_OPEN_LIBRARY_LANGUAGES = {
    "en": "eng",
    "es": "spa",
    "de": "ger",
    "fr": "fre",
    "it": "ita",
    "pt": "por",
    "nl": "dut",
    "no": "nor",
    "nb": "nor",
    "cs": "cze",
    "pl": "pol",
    "sv": "swe",
    "da": "dan",
}

_ISBN13_TEXT = re.compile(r"(?<![\dX])(97[89](?:[\s-]?\d){10})(?![\dX])", re.IGNORECASE)
_HEAD = re.compile(r"<head\b[^>]*>(.*?)</head>", re.IGNORECASE | re.DOTALL)
_META_ISBN = re.compile(
    r"""<meta[^>]+(?:property|name)=["'](?:books?:isbn|isbn)["'][^>]*content=["']([\dXx\s-]{10,17})["']""",
    re.IGNORECASE,
)
_AMAZON_ISBN10 = re.compile(r"/(?:dp|gp/product|exec/obidos/asin)/(\d{9}[\dX])(?:[/?]|$)", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_SCRIPT_OR_STYLE = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_JSON_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


# ── ISBN values ──────────────────────────────────────────────────────────


def _isbn13_valid(digits: str) -> bool:
    return (
        len(digits) == 13
        and digits.isdigit()
        and digits[:3] in {"978", "979"}
        and sum(int(digit) * (1 if index % 2 == 0 else 3) for index, digit in enumerate(digits)) % 10 == 0
    )


def _isbn10_valid(value: str) -> bool:
    if not re.fullmatch(r"\d{9}[\dX]", value):
        return False
    return sum((10 - index) * (10 if char == "X" else int(char)) for index, char in enumerate(value)) % 11 == 0


def isbn13(raw: Any) -> str:
    """Return a checksum-valid ISBN-13 for an ISBN-13 or ISBN-10 value, else ``""``."""
    compact = re.sub(r"[\s-]", "", str(raw or "")).upper()
    if _isbn13_valid(compact):
        return compact
    if _isbn10_valid(compact):
        body = "978" + compact[:9]
        check = (10 - sum(int(digit) * (1 if index % 2 == 0 else 3) for index, digit in enumerate(body)) % 10) % 10
        return body + str(check)
    return ""


def _unique(values: Iterable[str]) -> str:
    """The single distinct ISBN among ``values``, or ``""`` when none or several."""
    distinct = {value for value in values if value}
    return next(iter(distinct)) if len(distinct) == 1 else ""


def _isbn13s_in(text: str) -> list[str]:
    return [value for value in (isbn13(match) for match in _ISBN13_TEXT.findall(text)) if value]


# ── Text matching ────────────────────────────────────────────────────────


def _fold(value: str) -> str:
    """Lowercase and strip accents one character at a time, keeping positions."""
    folded = []
    for character in value:
        base = unicodedata.normalize("NFKD", character)
        kept = "".join(part for part in base if not unicodedata.combining(part))
        folded.append((kept[:1] or " ").lower())
    return "".join(folded)


def _flatten_markup(markup: str) -> str:
    """Decode entities and JSON escapes so titles and ISBNs can be found in place."""
    text = html_lib.unescape(markup)
    text = _JSON_UNICODE_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), text)
    return _fold(text)


def main_title(title: str) -> str:
    """The title before any subtitle, accent-free and lowercase, words single-spaced."""
    head = re.split(r"[:：]|\s[-–—]\s", str(title or ""), maxsplit=1)[0]
    return normalize_author_identity(head)


def _title_pattern(title: str) -> re.Pattern[str] | None:
    words = main_title(title).split()
    if not words:
        return None
    return re.compile(r"(?<!\w)" + r"[\W_]+".join(re.escape(word) for word in words) + r"(?!\w)")


_TITLE_END = r"(?:[\W\d_]*$|\s*(?:[|:·•–—,;(\[»”\"]|\s-\s)|\s+(?:by|de|von|par|di|van|por|from)\b)"


def _is_whole_title(text: str, title: str) -> bool:
    """True when ``text`` is ``title`` itself, up to a presentation separator.

    "Moonwalk, de Michael Jackson · Alba", "Abundance by Ezra Klein |
    Goodreads" and "Huna: A Memoir" are those books; "Becoming Human" is not
    "Becoming". Leading ISBNs written in from attributes are ignored.
    """
    words = main_title(title).split()
    if not words:
        return False
    pattern = r"^[\W\d_]*" + r"[\W_]+".join(re.escape(word) for word in words) + _TITLE_END
    return bool(re.match(pattern, _fold(html_lib.unescape(text)).strip()))


_PAGE_SUBJECT = re.compile(
    r"<title[^>]*>(.*?)</title>|<h1\b[^>]*>(.*?)</h1>|<meta[^>]+property=[\"']og:title[\"'][^>]*content=[\"']([^\"']*)",
    re.IGNORECASE | re.DOTALL,
)


def _page_is_about(markup: str, title: str) -> bool:
    """True when ``title`` is the page's own subject: its title, an h1 or og:title.

    The subject must be the whole title (:func:`_is_whole_title`), so a page
    about "Becoming Human" is never a page about "Becoming".

    A title that appears only elsewhere (a related-books block, a sidebar)
    names another book than the one the page's metadata describes.
    """
    for match in _PAGE_SUBJECT.finditer(markup or ""):
        subject = next((group for group in match.groups() if group), "")
        if _is_whole_title(_TAG.sub(" ", subject), title):
            return True
    return False


def _surname(author: str) -> str:
    words = normalize_author_identity(normalize_author_display(str(author or ""))).split()
    return words[-1] if words else ""


# ── 1. The source's own book link ────────────────────────────────────────


def _host(url: str) -> str:
    return (urlsplit(str(url or "")).hostname or "").casefold()


def _is_audio_url(url: str) -> bool:
    return any(marker in _host(url) for marker in _AUDIO_HOST_MARKERS)


def isbn_from_link(url: str) -> str:
    """The single ISBN a book link names, else ``""``.

    An ISBN-13 anywhere in the path or query counts, and so does the ISBN-10
    an Amazon product path uses for a printed book. Audiobook links never
    count: their ISBNs belong to the audio edition.
    """
    if not url or _is_audio_url(url):
        return ""
    parts = urlsplit(url)
    text = unquote(f"{parts.path}?{parts.query}")
    values = _isbn13s_in(text)
    if "amazon." in (parts.hostname or "").casefold():
        values += [isbn13(match) for match in _AMAZON_ISBN10.findall(parts.path)]
    return _unique(values)


# ── Records on a page ────────────────────────────────────────────────────

_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)
_LD_JSON = re.compile(
    r"""<script[^>]*type=["']application/ld\+json["'][^>]*>(.*?)</script>""", re.IGNORECASE | re.DOTALL
)


def _json_ld_objects(markup: str) -> list[Any]:
    objects: list[Any] = []
    for block in _LD_JSON.findall(markup or ""):
        try:
            objects.append(json.loads(html_lib.unescape(block.strip())))
        except ValueError:
            continue
    return objects


def _record_name(node: Mapping[str, Any]) -> str:
    for key in ("name", "title", "headline"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


# The fields of a JSON-LD record that identify the record itself. Any other
# field (a description, "isSimilarTo", a review) may name another book.
_OWN_ID_KEYS = frozenset(
    {"isbn", "isbn10", "isbn13", "isbn_10", "isbn_13", "gtin", "gtin13", "gtin_13", "productid", "sameas", "url", "@id"}
)
# Nested parts that still describe the record itself: its editions, its own
# offers, and identifier objects ({"propertyID": "ISBN", "value": ...}).
_OWN_PART_KEYS = frozenset({"workexample", "offers", "identifier"})


def _id_values(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(_isbn13s_in(value)) | ({isbn13(value)} - {""})
    if isinstance(value, list):
        return set().union(*(_id_values(item) for item in value)) if value else set()
    if isinstance(value, Mapping) and "value" in value:
        return _id_values(value.get("value"))
    return set()


def _record_isbns(node: Mapping[str, Any]) -> set[str]:
    """ISBNs in the fields of one record that identify the record itself."""
    found: set[str] = set()
    for key, value in node.items():
        name = str(key).casefold()
        if name in _OWN_ID_KEYS:
            found |= _id_values(value)
        elif name in _OWN_PART_KEYS:
            parts = value if isinstance(value, list) else [value]
            for part in parts:
                if isinstance(part, Mapping):
                    found |= _record_isbns(part) | _id_values(part)
                else:
                    found |= _id_values(part)
    return found


def _record_authors(node: Mapping[str, Any]) -> set[str]:
    """The full normalized names a JSON-LD record gives as its author or creator."""
    names: set[str] = set()
    for key in ("author", "creator"):
        values = node.get(key)
        for value in values if isinstance(values, list) else [values]:
            name = _record_name(value) if isinstance(value, Mapping) else value
            if isinstance(name, str) and _author_identity(name):
                names.add(_author_identity(name))
    return names


def _names_another_author(markup: str, title_key: str, authors: set[str]) -> bool:
    """True when a JSON-LD record of this title names authors, none of them ``authors``."""
    conflict = False

    def walk(node: Any) -> None:
        nonlocal conflict
        if isinstance(node, Mapping):
            if main_title(_record_name(node)) == title_key:
                own = _record_authors(node)
                if own and not own & authors:
                    conflict = True
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for value in _json_ld_objects(markup):
        walk(value)
    return conflict


def structured_isbns(markup: str, authors: Mapping[str, set[str]] | None = None) -> dict[str, set[str]]:
    """Map each named JSON-LD record (by :func:`main_title`) to the ISBNs it holds itself.

    ``authors`` maps a title to the full normalized names of its listed
    authors. A record of that title that names authors of its own counts
    only when one of them is listed: the same title by another author is
    another book.
    """
    found: dict[str, set[str]] = {}
    wanted = authors or {}

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            name = main_title(_record_name(node))
            own_authors = _record_authors(node) if name else set()
            if name and wanted.get(name) and own_authors and not own_authors & wanted[name]:
                name = ""
            if name:
                isbns = _record_isbns(node)
                if isbns:
                    found.setdefault(name, set()).update(isbns)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for value in _json_ld_objects(markup):
        walk(value)
    return found


_RELATED_MARKER = re.compile(
    r"related|recommend|similar|also|more-?books|carousel|relacionad|recomend|tambien|weitere|aehnlich|ahnlich",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _Span:
    start: int
    end: int
    tag: str
    related: bool

    def holds(self, start: int, end: int) -> bool:
        return self.start <= start and end <= self.end

    def __len__(self) -> int:
        return self.end - self.start


_LABEL_ATTRIBUTES = frozenset({"alt", "title", "aria-label"})
_LABELLED_ISBN10 = re.compile(r"\bISBN(?:-?10)?\s*[:#.]?\s*((?:\d[\s-]?){9}[\dXx])(?![\dXx])", re.IGNORECASE)


class _CardCollector(HTMLParser):
    """Flatten a page into visible text, recording each element's text span.

    An ISBN in an element's attributes (a cover file name, a buy link, a
    button id) is written into the text at the element's start, so it falls
    inside that element's span and every span around it. An element whose
    class or id names a related-books block is marked, so no ISBN inside it
    is ever owned by the page's book.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.length = 0
        self.isbns: list[tuple[int, str]] = []
        # The text an ISBN was read from, by position; attribute ISBNs have none.
        self.text_at: dict[int, str] = {}
        self.spans: list[_Span] = []
        self._stack: list[tuple[str, int, bool]] = []
        # Start offsets of elements that name an author (itemprop, class or rel).
        self.author_starts: set[int] = set()
        self._skip = 0

    def _write(self, text: str) -> None:
        self.parts.append(text)
        self.length += len(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip += 1
            return
        start = self.length
        related = any(name in {"class", "id"} and bool(_RELATED_MARKER.search(value or "")) for name, value in attrs)
        if any(name in {"itemprop", "class", "rel"} and "author" in (value or "").casefold() for name, value in attrs):
            self.author_starts.add(start)
        for _name, value in attrs:
            for isbn in _isbn13s_in(unquote(value or "")):
                self.isbns.append((self.length, isbn))
                self._write(f" {isbn} ")
        # A label in an attribute names the element as visible text would:
        # an image's alt text can be the only trace of a neighbouring book.
        for name, value in attrs:
            if name in _LABEL_ATTRIBUTES and value and value.strip():
                self._write(f" {value.strip()} ")
        if tag in _VOID_TAGS:
            self.spans.append(_Span(start, self.length, tag, related))
        else:
            self._stack.append((tag, start, related))
        self._write(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS and self._stack and self._stack[-1][0] == tag:
            open_tag, start, related = self._stack.pop()
            self.spans.append(_Span(start, self.length, open_tag, related))

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self._skip = max(0, self._skip - 1)
            return
        if not any(open_tag == tag for open_tag, _start, _related in self._stack):
            return
        while self._stack:
            open_tag, start, related = self._stack.pop()
            self.spans.append(_Span(start, self.length, open_tag, related))
            if open_tag == tag:
                break
        self._write(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            found = _isbn13s_in(data)
            # An ISBN-10 counts only where a label names it: a bare ten-digit
            # number is too often something else.
            found += [value for raw in _LABELLED_ISBN10.findall(data) if (value := isbn13(raw))]
            for isbn in found:
                self.isbns.append((self.length, isbn))
            if found:
                self.text_at[self.length] = data
            self._write(data)

    def finish(self) -> str:
        self.close()
        while self._stack:
            open_tag, start, related = self._stack.pop()
            self.spans.append(_Span(start, self.length, open_tag, related))
        return _fold("".join(self.parts))


_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
# A book card's text beside its title: author, publisher, price, a blurb line.
CARD_MAX_LETTERS = 300
_ISBN_LINE = re.compile(r"(?:(?:isbn(?:-?1[03])?|ean)\s*[:#.]?\s*(?:[\dx][\s-]?){9,16}[\dx]\s*)+", re.IGNORECASE)
_OWN_ACTION_LABELS = frozenset(
    {
        "comprar",
        "comprar ahora",
        "anadir",
        "anadir a la cesta",
        "anadir al carrito",
        "agregar al carrito",
        "en stock",
        "kaufen",
        "in den warenkorb",
        "acheter",
        "ajouter au panier",
        "acquista",
        "kopen",
        "kjop",
    }
)


_COVER_PREFIX = re.compile(
    r"^\s*(?:(?:book\s+)?cover(?:\s+image)?|portada|cubierta|couverture|copertina|capa|umschlag|omslag)"
    r"(?:\s+(?:of|de|del|di|du|da|von|van))?[\s:«»\"'“”‘’]*",
)


def _owned_part(text: str, title: str = "") -> bool:
    """True when a card part's text claims nothing but its ISBN.

    Affirmative ownership: the part shows no words (a cover, an icon
    button), only a buy or cover label, or an explicit ISBN label with
    little else. Any other text, even one short word, may be another book's
    title, so the ISBN is not this card's.
    """
    words = normalize_author_identity(re.sub(r"\d", " ", text))
    if not words:
        return True
    if title and _is_whole_title(_COVER_PREFIX.sub("", _fold(text).strip(" \t0123456789")), title):
        return True  # a cover labelled with this card's own title, never "Becoming Human" for "Becoming"
    from autoscout.source_page_identity import is_action_label
    from autoscout.source_page_identity import is_cover_label

    if words in _OWN_ACTION_LABELS or is_action_label(words) or is_cover_label(words):
        return True
    # A labelled ISBN is owned only as a bare metadata line, "ISBN: 978…";
    # "Borges (ISBN: 978…)" names its own book.
    return bool(_ISBN_LINE.fullmatch(_fold(text).strip()))


def card_isbns(
    markup: str, titles: Sequence[str], authors: Mapping[str, set[str]] | None = None
) -> dict[str, set[str]]:
    """Map each listed title to the ISBNs its own card holds.

    A title's card is the smallest element around it that holds an ISBN.
    The title must be the whole text of its own element, and the card must
    look like one book's card: its only heading holds the title and it
    carries at most :data:`CARD_MAX_LETTERS` letters beside it. The card
    counts only when it holds no other listed title, and when every
    ISBN in it is owned: it sits in the card's own attributes or in the
    title's own element, or the part of the card holding it but not the
    title is no heading, no related-books block, and passes
    :func:`_owned_part`. Otherwise the ISBN may belong to a neighbouring
    book that was not extracted.
    """
    collector = _CardCollector()
    try:
        collector.feed(markup or "")
        text = collector.finish()
    except (AssertionError, ValueError):
        return {}
    if not collector.isbns:
        return {}
    patterns = {title: pattern for title in set(titles) if (pattern := _title_pattern(title))}
    occurrences = {title: [(m.start(), m.end()) for m in pattern.finditer(text)] for title, pattern in patterns.items()}
    spans = sorted(collector.spans, key=len)

    def owned(card: _Span, start: int, end: int, isbn_at: int, title: str) -> bool:
        parts = [
            span
            for span in spans
            if card.holds(span.start, span.end)
            and span != card
            and span.start <= isbn_at < span.end
            and not span.holds(start, end)
        ]
        if not parts:
            # In the card's own attributes, or loose text in the card itself:
            # that text must be nothing but the ISBN line ("Related: Borges
            # ISBN: …" names another book).
            loose = collector.text_at.get(isbn_at)
            return loose is None or _owned_part(loose, title)
        part = max(parts, key=len)
        if any(part.holds(span.start, span.end) and (span.tag in _HEADING_TAGS or span.related) for span in spans):
            return False
        # Any other element with words inside the part ("<p>Borges</p>" next
        # to "<p>ISBN: …</p>") makes it a neighbouring book's card: an owned
        # part is a single cover, button, link or labelled ISBN line.
        if any(
            part.holds(span.start, span.end)
            and span != part
            and not span.start <= isbn_at < span.end
            and any(character.isalpha() for character in text[span.start : span.end])
            for span in spans
        ):
            return False
        return _owned_part(text[part.start : part.end], title)

    wanted_surnames = {
        title: {name.split()[-1] for name in names if name.split()} for title, names in (authors or {}).items()
    }

    def names_another_author(card: _Span, title: str) -> bool:
        # An author element in the card that names none of this title's
        # listed authors makes it another book's card (Vance's "Elon Musk").
        surnames = wanted_surnames.get(title)
        if not surnames:
            return False
        for span in spans:
            if span.start in collector.author_starts and card.holds(span.start, span.end):
                words = set(normalize_author_identity(text[span.start : span.end]).split())
                if words and not words & surnames:
                    return True
        return False

    def is_card(card: _Span, start: int, end: int) -> bool:
        # A card shows one book: its only heading holds the title, and it
        # carries little text beside it. A page-sized container never does.
        if any(
            card.holds(span.start, span.end) and span.tag in _HEADING_TAGS and not span.holds(start, end)
            for span in spans
        ):
            return False
        letters = sum(character.isalpha() for character in text[card.start : card.end])
        return letters - sum(character.isalpha() for character in text[start:end]) <= CARD_MAX_LETTERS

    def whole_title_at(start: int, end: int, title: str) -> bool:
        element = next((span for span in spans if span.holds(start, end)), None)
        return element is not None and _is_whole_title(text[element.start : element.end], title)

    found: dict[str, set[str]] = {}
    for title, places in occurrences.items():
        for start, end in places:
            if any(span.related and span.holds(start, end) for span in spans):
                continue  # a related-books block names the title of another book
            if not whole_title_at(start, end, title):
                continue  # "Becoming" inside "Becoming Human" is another book
            for card in spans:
                if not card.holds(start, end):
                    continue
                inside = [(position, isbn) for position, isbn in collector.isbns if card.start <= position < card.end]
                if not inside:
                    continue
                crowded = any(
                    other != title and any(card.holds(a, b) for a, b in other_places)
                    for other, other_places in occurrences.items()
                )
                if (
                    not crowded
                    and is_card(card, start, end)
                    and not names_another_author(card, title)
                    and all(owned(card, start, end, at, title) for at, _isbn in inside)
                ):
                    found.setdefault(title, set()).update(isbn for _at, isbn in inside)
                break
    return found


# ── 2. The captured list page ────────────────────────────────────────────


def _author_identity(author: str) -> str:
    """A full normalized author name: "Isaacson, Walter" and "Walter Isaacson" agree; two Smiths do not."""
    return normalize_author_identity(normalize_author_display(str(author or "")))


def _match_keys(titles: Sequence[str], authors: Sequence[str] | None = None) -> tuple[set[str], set[str]]:
    """The main titles of ``titles``, and those two different books share.

    Two listed books differ when their full titles differ ("Hope: A Memoir"
    and "Hope: A New History") or their authors do (two "Elon Musk"
    biographies). Matching reads titles only, so nothing tells such books
    apart, and their shared main title is ambiguous.
    """
    names = list(authors or [])
    books_by_key: dict[str, set[tuple[str, str]]] = {}
    for index, title in enumerate(titles):
        key = main_title(title)
        if key:
            author = _author_identity(names[index]) if index < len(names) else ""
            books_by_key.setdefault(key, set()).add((normalize_author_identity(title), author))
    shared = set()
    for key, books in books_by_key.items():
        full_titles = {full for full, _author in books}
        known_authors = {author for _full, author in books if author}
        if len(full_titles) > 1 or len(known_authors) > 1:
            shared.add(key)
    return set(books_by_key), shared


def isbns_from_capture(markup: str, titles: Sequence[str], authors: Sequence[str] | None = None) -> dict[str, str]:
    """Map each listed title (by :func:`main_title`) to its ISBN on a captured list page.

    An ISBN belongs to a book only through that book's own record: the
    JSON-LD record named with its title, or its own card (see
    :func:`card_isbns`). Position alone never decides. A title whose records
    disagree gets nothing, so does an ISBN two titles claim, and so do two
    different books whose titles share a main title, or one title by two
    authors (``authors`` runs parallel to ``titles``), because nothing here
    tells them apart.
    """
    if not markup:
        return {}
    listed, shared = _match_keys(titles, authors)
    listed_authors: dict[str, set[str]] = {}
    for index, title in enumerate(titles):
        name = _author_identity((authors or [])[index]) if index < len(authors or []) else ""
        if name and main_title(title):
            listed_authors.setdefault(main_title(title), set()).add(name)
    evidence: dict[str, set[str]] = {}
    for title, isbns in structured_isbns(markup, listed_authors).items():
        if title in listed:
            evidence.setdefault(title, set()).update(isbns)
    # A title the page's JSON-LD gives to another author is another book on
    # this page: its card is no evidence either.
    conflicted = {key for key, names in listed_authors.items() if _names_another_author(markup, key, names)}
    for title, isbns in card_isbns(markup, list(listed - conflicted), listed_authors).items():
        evidence.setdefault(title, set()).update(isbns)
    chosen = {title: next(iter(isbns)) for title, isbns in evidence.items() if len(isbns) == 1 and title not in shared}
    claims: dict[str, int] = {}
    for value in chosen.values():
        claims[value] = claims.get(value, 0) + 1
    return {title: value for title, value in chosen.items() if claims[value] == 1}


def isbn_from_capture(
    markup: str, title: str, other_titles: Sequence[str], author: str = "", other_authors: Sequence[str] = ()
) -> str:
    """The ISBN :func:`isbns_from_capture` gives ``title`` on its list page, else ``""``."""
    others = list(other_authors) + [""] * (len(other_titles) - len(other_authors))
    return isbns_from_capture(markup, [title, *other_titles], [author, *others]).get(main_title(title), "")


# ── 3. The book's own page ───────────────────────────────────────────────


def isbn_from_book_page(markup: str, title: str, author: str = "") -> str:
    """The ISBN of the book a page is about, else ``""``.

    The book must be the page's own subject (:func:`_page_is_about`), and
    the ISBN must be the book's own. The evidence is read in order of strength, and the first kind that
    finds anything decides: the JSON-LD record named with this title
    (records of other books are ignored), the ISBN meta tags of the page's
    own head, then
    the book's own card around its title (see :func:`card_isbns`), which
    excludes related-books blocks. A kind that finds two different ISBNs
    decides "unknown"; a weaker kind is never asked to break that tie.
    """
    if not markup or not _page_is_about(markup, title):
        return ""
    flat = html_lib.unescape(markup)
    wanted = {main_title(title): {_author_identity(author)}} if _author_identity(author) else None
    if wanted and _names_another_author(markup, main_title(title), wanted[main_title(title)]):
        # The page describes this title by someone else: none of its
        # evidence is this book's, however weak or strong.
        return ""
    structured = sorted(structured_isbns(markup, wanted).get(main_title(title), set()))
    # Only the page's own head describes the page's book; microdata in the
    # body may describe a related book and counts only through its card.
    head = _HEAD.search(flat)
    tagged = [isbn13(value) for value in _META_ISBN.findall(head.group(1) if head else "")]
    own_card = sorted(card_isbns(markup, [main_title(title)], wanted).get(main_title(title), set()))
    for evidence in (structured, tagged, own_card):
        found = {value for value in evidence if value}
        if found:
            return next(iter(found)) if len(found) == 1 else ""
    return ""


# ── 4. Open Library ──────────────────────────────────────────────────────


def open_library_query_url(title: str, author: str, language: str) -> str:
    """The Open Library search for one edition of the book in its language."""
    query = f'title:"{main_title(title)}" author:"{_surname(author)}"'
    language_code = _OPEN_LIBRARY_LANGUAGES.get(language.casefold(), "")
    if language_code:
        query += f" language:{language_code}"
    fields = (
        "key,title,author_name,editions,editions.key,editions.title,editions.isbn,"
        "editions.language,editions.publish_date,editions.physical_format,editions.format"
    )
    return f"{OPEN_LIBRARY_SEARCH_URL}?{urlencode({'q': query, 'fields': fields, 'limit': 3})}"


def _publish_year(value: Any) -> int | None:
    texts = value if isinstance(value, list) else [value]
    years = [int(year) for text in texts for year in re.findall(r"\b(1[5-9]\d\d|20\d\d)\b", str(text or ""))]
    return max(years) if years else None


_AUDIO_FORMAT = re.compile(r"audio|\bcd\b|mp3|cassette|h[öo]rbuch|audiolibro|livre audio", re.IGNORECASE)


def _is_audio_format(edition: Mapping[str, Any]) -> bool:
    """True for an audio edition: its ISBN would open audiobook listings, not the book."""
    formats = [edition.get("physical_format"), *_list(edition.get("format")), edition.get("format")]
    return any(isinstance(value, str) and _AUDIO_FORMAT.search(value) for value in formats)


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def isbn_from_open_library(
    response: Mapping[str, Any], title: str, author: str, language: str, min_year: int | None
) -> tuple[str, str]:
    """The ``(isbn, edition_url)`` of one agreeing Open Library edition, else ``("", "")``.

    A work agrees when an author shares the candidate author's surname. Its
    matched edition agrees when its own title, or else the work's title,
    has the candidate's main title (a translation keeps the original work
    title), and when it carries an ISBN, is not an audio edition, is in the
    source language when one is known, and is not older than ``min_year``.
    Two agreeing editions with different ISBNs decide "unknown".

    Open Library returns one edition per work, its best match, and counts
    the others in ``editions.numFound``. That edition is accepted on
    purpose: editions of one work in one language (hardcover, paperback)
    are the same book for the editor's shop link, so a truncated list is no
    ambiguity. The "unknown" rule guards against two different works.
    """
    want_title = main_title(title)
    want_surname = _surname(author)
    language_code = _OPEN_LIBRARY_LANGUAGES.get(language.casefold(), "")
    found: dict[str, str] = {}
    docs = response.get("docs") if isinstance(response, Mapping) else None
    for doc in docs if isinstance(docs, list) else []:
        if not isinstance(doc, Mapping):
            continue
        work_title_agrees = main_title(str(doc.get("title", ""))) == want_title
        authors = _list(doc.get("author_name"))
        if not want_surname or not any(_surname(name) == want_surname for name in authors):
            continue
        editions = doc.get("editions")
        edition_docs = editions.get("docs") if isinstance(editions, Mapping) else None
        for edition in edition_docs if isinstance(edition_docs, list) else []:
            if not isinstance(edition, Mapping):
                continue
            edition_title = str(edition.get("title", "") or "")
            if not (main_title(edition_title) == want_title if edition_title else work_title_agrees):
                continue
            languages = _list(edition.get("language"))
            if language_code and language_code not in languages:
                continue
            if _is_audio_format(edition):
                continue
            year = _publish_year(edition.get("publish_date"))
            if min_year is not None and (year is None or year < min_year):
                continue
            isbns = _list(edition.get("isbn"))
            value = _unique(isbn13(item) for item in isbns)
            if value:
                key = str(edition.get("key", "") or "")
                found[value] = f"https://openlibrary.org{key}" if key.startswith("/books/") else OPEN_LIBRARY_SEARCH_URL
    if len(found) != 1:
        return "", ""
    value, url = next(iter(found.items()))
    return value, url


# ── Network ──────────────────────────────────────────────────────────────


class PageGetter(Protocol):
    def get(self, url: str, *, timeout: float | None = None) -> str | None: ...


class BoundedPageGetter:
    """GET one page under a whole-request deadline, a size cap and per-host spacing.

    The deadline covers connecting, redirects, headers and the body: the
    shared grounded-description transport reads in chunks against it and
    abandons a request that trickles past it. Any failure (status, timeout,
    oversize, an unknown charset) answers ``None``: a missing ISBN is never
    a reason to stop.
    """

    def __init__(
        self,
        *,
        transport: Any = None,
        min_host_interval_seconds: float = MIN_HOST_INTERVAL_SECONDS,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        max_bytes: int = MAX_RESPONSE_BYTES,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        from autoscout.grounded_description import HostRateLimiter
        from autoscout.grounded_description import StandardLibraryHTTPTransport

        self._transport = transport or StandardLibraryHTTPTransport(max_redirects=3)
        self._timeout = timeout_seconds
        self._max_bytes = max_bytes
        self._monotonic = monotonic
        self._limiter = HostRateLimiter(min_host_interval_seconds, monotonic=monotonic, sleeper=sleeper)

    def get(self, url: str, *, timeout: float | None = None) -> str | None:
        """GET ``url`` within ``timeout`` seconds in all, never more than the getter's own timeout."""
        if urlsplit(url).scheme not in {"http", "https"}:
            return None
        self._limiter.wait(_host(url))
        allowance = self._timeout if timeout is None else max(0.0, min(self._timeout, timeout))
        deadline = self._monotonic() + allowance
        try:
            response = self._transport.request(
                url,
                timeout_seconds=allowance,
                max_response_bytes=self._max_bytes,
                remaining_seconds=lambda: deadline - self._monotonic(),
            )
        except Exception:
            return None
        if int(getattr(response, "status", 0)) != 200 or getattr(response, "truncated", False):
            return None
        return _decode(bytes(response.body), dict(getattr(response, "headers", {}) or {}))


def _decode(body: bytes, headers: Mapping[str, str]) -> str:
    match = re.search(r"charset=([\w.:-]+)", str(headers.get("content-type", "")), re.IGNORECASE)
    try:
        return body.decode(match.group(1) if match else "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


# ── Completion ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SourcePage:
    """One captured list page and every title extracted from it."""

    source_name: str
    markup: str
    titles: tuple[str, ...]
    # The first author of each title, in the same order; blank when unknown.
    authors: tuple[str, ...] = ()
    # The URLs this list page was captured from: never a book's own page.
    urls: frozenset[str] = frozenset()


@dataclass
class CompletionReport:
    considered: int = 0
    completed: dict[str, int] = field(default_factory=lambda: dict.fromkeys(METHODS, 0))
    not_found: int = 0
    skipped_recently_tried: int = 0
    budget_exhausted: int = 0
    lookup_unavailable: int = 0
    records: list[dict[str, str]] = field(default_factory=list)
    # Identity keys of the records this pass changed: an ISBN filled or a
    # miss dated. Only these are merged back into Candidate History.
    touched: set[str] = field(default_factory=set)

    def as_dict(self) -> dict[str, Any]:
        return {
            "considered": self.considered,
            "completed": dict(self.completed),
            "completed_total": sum(self.completed.values()),
            "not_found": self.not_found,
            "skipped_recently_tried": self.skipped_recently_tried,
            "budget_exhausted": self.budget_exhausted,
            "lookup_unavailable": self.lookup_unavailable,
            "records": list(self.records),
        }


def _evidence(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    evidence = record.get("source_evidence")
    return [item for item in evidence if isinstance(item, Mapping)] if isinstance(evidence, list) else []


def _is_audio_source(evidence: Mapping[str, Any]) -> bool:
    name = str(evidence.get("source_name", "")).casefold()
    return any(marker in name for marker in _AUDIO_SOURCE_MARKERS) or _is_audio_url(
        str(evidence.get("evidence_url", ""))
    )


def _first_author(record: Mapping[str, Any]) -> str:
    authors = record.get("primary_authors")
    if isinstance(authors, list):
        return str(next((author for author in authors if str(author).strip()), "") or "")
    return str(authors or "")


def _source_language(record: Mapping[str, Any]) -> str:
    return next((str(item.get("language", "")) for item in _evidence(record) if item.get("language")), "")


def _min_edition_year(record: Mapping[str, Any]) -> int | None:
    """The oldest acceptable edition year: only from a known publication year.

    The date a book was first seen says nothing about when it was published
    (a backlist bestseller), so it never restricts the edition.
    """
    year = _publish_year(record.get("publication_year"))
    return year - 1 if year is not None else None


def _recently_tried(record: Mapping[str, Any], today: date) -> bool:
    try:
        tried = date.fromisoformat(str(record.get("isbn_lookup_date", "")))
    except ValueError:
        return False
    return today - tried < timedelta(days=RETRY_AFTER_DAYS)


def _url_key(url: str) -> str:
    return url.strip().rstrip("/")


def _book_page_url(evidence: Mapping[str, Any], listing_urls: frozenset[str]) -> str:
    """The book's own page behind a source mention, or ``""``.

    A captured list page is never a book page. A stored evidence URL equal
    to its source URL is still a book page when the source is a book page
    itself (a single review), so only the URLs this run captured as lists
    are excluded.
    """
    url = str(evidence.get("evidence_url", "") or "").strip()
    source_url = str(evidence.get("source_url", "") or "").strip()
    if url and source_url and not urlsplit(url).scheme:
        url = urljoin(source_url, url)
    if not url or _url_key(url) in listing_urls:
        return ""
    if any(marker in _host(url) for marker in _NO_FETCH_HOST_MARKERS) or _is_audio_url(url):
        return ""
    return url if urlsplit(url).scheme in {"http", "https"} else ""


def _offline_isbn(record: Mapping[str, Any], pages: Mapping[str, SourcePage]) -> tuple[str, str, str]:
    title = str(record.get("title", "") or "")
    for evidence in _evidence(record):
        if _is_audio_source(evidence):
            continue  # an audiobook source links the audio edition
        url = str(evidence.get("evidence_url", "") or "")
        value = isbn_from_link(urljoin(str(evidence.get("source_url", "") or ""), url))
        if value:
            return value, METHOD_SOURCE_LINK, url
    for evidence in _evidence(record):
        if _is_audio_source(evidence):
            continue
        page = pages.get(str(evidence.get("source_name", "")))
        if page is None:
            continue
        value = isbn_from_capture(page.markup, title, page.titles, _first_author(record), page.authors)
        if value:
            return value, METHOD_SOURCE_PAGE, str(evidence.get("source_url", "") or "")
    return "", "", ""


class _BudgetExhausted(Exception):
    """The stage deadline passed before the next request."""


class _LookupUnavailable(Exception):
    """The catalogue gave no usable answer (timeout, HTTP error, malformed JSON)."""


def _network_isbn(
    record: Mapping[str, Any],
    getter: PageGetter,
    deadline: float,
    monotonic: Callable[[], float],
    *,
    listing_urls: frozenset[str] = frozenset(),
    fetched: dict[str, str | None] | None = None,
) -> tuple[str, str, str]:
    cache = fetched if fetched is not None else {}

    def fetch(url: str) -> str | None:
        if url in cache:
            return cache[url]
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise _BudgetExhausted
        body = getter.get(url, timeout=remaining)
        if body is None and monotonic() >= deadline:
            # The request ran into the deadline: cut short, not a miss.
            raise _BudgetExhausted
        cache[url] = body
        return body

    title = str(record.get("title", "") or "")
    for evidence in _evidence(record):
        if _is_audio_source(evidence):
            continue
        url = _book_page_url(evidence, listing_urls)
        if not url:
            continue
        value = isbn_from_book_page(fetch(url) or "", title, _first_author(record))
        if value:
            return value, METHOD_BOOK_PAGE, url
    author = _first_author(record)
    if not main_title(title) or not _surname(author):
        return "", "", ""
    language = _source_language(record)
    body = fetch(open_library_query_url(title, author, language))
    try:
        response = json.loads(body) if body is not None else None
    except ValueError:
        response = None
    if not isinstance(response, Mapping) or not isinstance(response.get("docs"), list):
        # No answer is not a "no": the catalogue was unreachable or answered
        # garbage, so the record stays eligible for the next run.
        raise _LookupUnavailable
    value, edition_url = isbn_from_open_library(response, title, author, language, _min_edition_year(record))
    if value:
        return value, METHOD_OPEN_LIBRARY, edition_url
    return "", "", ""


class _Progress(Protocol):
    def initial(self, items: list[tuple[str, str]]) -> None: ...
    def begin(self, key: str) -> None: ...
    def finish(self, key: str) -> None: ...
    def final(self, *, status: str = "finished") -> None: ...


def complete_isbns(
    records: Sequence[dict[str, Any]],
    *,
    pages: Mapping[str, SourcePage],
    getter: PageGetter | None,
    today: date,
    budget_seconds: float = STAGE_BUDGET_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    progress: _Progress | None = None,
) -> CompletionReport:
    """Fill a blank ISBN on each record in place and report what happened.

    ``getter=None`` runs the offline methods only. A record whose network
    methods ran and found nothing records today's ``isbn_lookup_date``.
    """
    report = CompletionReport()
    deadline = monotonic() + budget_seconds
    listing_urls = frozenset().union(*(page.urls for page in pages.values())) if pages else frozenset()
    fetched: dict[str, str | None] = {}
    todo = [record for record in records if not CandidateIsbn.from_record(record) and str(record.get("title", ""))]
    keys = [str(record.get("candidate_id") or record.get("identity_key", "")) for record in todo]
    if progress is not None:
        progress.initial([(key, str(record.get("title", ""))) for key, record in zip(keys, todo, strict=True)])
    for key, record in zip(keys, todo, strict=True):
        report.considered += 1
        if progress is not None:
            progress.begin(key)
        value, method, url = _offline_isbn(record, pages)
        tried_network = False
        if not value and getter is not None:
            if _recently_tried(record, today):
                report.skipped_recently_tried += 1
            elif monotonic() >= deadline:
                report.budget_exhausted += 1
            else:
                try:
                    value, method, url = _network_isbn(
                        record, getter, deadline, monotonic, listing_urls=listing_urls, fetched=fetched
                    )
                    tried_network = True
                except _BudgetExhausted:
                    # Cut short, not a miss: the record is asked again next run.
                    report.budget_exhausted += 1
                except _LookupUnavailable:
                    report.lookup_unavailable += 1
        if value:
            CandidateIsbn(value=value, discovery_only=True).apply_to(record)
            record["isbn_provenance"] = {"method": method, "url": url, "date": today.isoformat()}
            record.pop("isbn_lookup_date", None)
            report.touched.add(str(record.get("identity_key", "")))
            report.completed[method] += 1
            report.records.append(
                {
                    "identity_key": str(record.get("identity_key", "")),
                    "title": str(record.get("title", "")),
                    "isbn": value,
                    "method": method,
                    "url": url,
                }
            )
        elif tried_network:
            record["isbn_lookup_date"] = today.isoformat()
            report.touched.add(str(record.get("identity_key", "")))
            report.not_found += 1
        if progress is not None:
            progress.finish(key)
    if progress is not None:
        progress.final()
    return report


# ── State files ──────────────────────────────────────────────────────────


def load_source_pages(output_dir: Path) -> dict[str, SourcePage]:
    """Pair each captured list page with the titles extracted from it."""
    titles: dict[str, list[str]] = {}
    authors: dict[str, list[str]] = {}
    for path in sorted(output_dir.glob("*-codex-extraction.json")):
        data = read_json_or_default(path, {})
        if not isinstance(data, Mapping):
            continue
        entries = data.get("entries")
        name = str(data.get("source_name", "") or "")
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, Mapping) and str(entry.get("title", "")).strip():
                titles.setdefault(name, []).append(str(entry["title"]))
                authors.setdefault(name, []).append(_first_author(entry))
    markup: dict[str, list[str]] = {}
    urls: dict[str, set[str]] = {}
    for path in sorted(output_dir.glob("codex-capture-*.json")):
        data = read_json_or_default(path, {})
        if isinstance(data, Mapping) and isinstance(data.get("html"), str):
            name = str(data.get("source_name", "") or "")
            markup.setdefault(name, []).append(data["html"])
            urls.setdefault(name, set()).update(
                _url_key(str(data.get(key, "") or "")) for key in ("source_url", "source_page", "resolved_url")
            )
    return {
        name: SourcePage(
            source_name=name,
            markup="\n".join(pages),
            titles=tuple(titles.get(name, ())),
            authors=tuple(authors.get(name, ())),
            urls=frozenset(url for url in urls.get(name, set()) if url),
        )
        for name, pages in markup.items()
        if name
    }


def _copy_isbn(history_record: Mapping[str, Any], row: dict[str, Any]) -> bool:
    """Fill-only copy of the learned ISBN pair and its provenance onto a snapshot row."""
    learned = CandidateIsbn.from_record(history_record)
    if not learned or CandidateIsbn.from_record(row):
        return False
    learned.apply_to(row)
    if isinstance(history_record.get("isbn_provenance"), Mapping):
        row["isbn_provenance"] = dict(history_record["isbn_provenance"])
    return True


_LEARNED_FIELDS = ("isbn", "isbn_discovery_only", "isbn_provenance", "isbn_lookup_date")

# The envelope's seal step, kept as this module's own name: a patched
# ``_seal_finalization`` still intercepts it.
_seal_finalization = seal_finalization


def _has_isbn(record: Mapping[str, Any]) -> bool:
    """True when the record already holds an ISBN."""
    return bool(CandidateIsbn.from_record(record))


_LEARNED_FIELD_SPEC = LearnedFieldSpec(
    fields=_LEARNED_FIELDS,
    already_filled=_has_isbn,
    copy_to_row=_copy_isbn,
    report_filename=REPORT_FILENAME,
    seal_pending_filename=_SEAL_PENDING_FILENAME,
    label="ISBN completion",
    # The envelope's two interruptible steps are named here through this
    # module's own globals, so a patched ``project_isbns`` or
    # ``_seal_finalization`` still intercepts them.
    project=lambda history, rows, aliases: project_isbns(history, rows, aliases),
    seal=lambda output_dir, store: _seal_finalization(output_dir, store),
)


def project_isbns(
    history: Sequence[dict[str, Any]], rows: Sequence[Mapping[str, Any]], aliases: Mapping[str, Any] | None = None
) -> tuple[list[dict[str, Any]], int]:
    """Copy learned ISBNs from history onto snapshot rows; return the rows and how many changed."""
    return project_fields(history, rows, aliases, _LEARNED_FIELD_SPEC)


def run_isbn_completion(
    output_dir: Path,
    state_dir: Path,
    *,
    effective_date: str,
    scope: str = SCOPE_CURRENT,
    getter: PageGetter | None = None,
    dry_run: bool = False,
    publish_lock: Callable[[], Any] | None = None,
    budget_seconds: float = STAGE_BUDGET_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    progress: _Progress | None = None,
    state_store: StateStore | None = None,
) -> dict[str, Any]:
    """Complete ISBNs for the selected Candidate History records and persist them.

    ``scope="current"`` takes this run's candidates; ``scope="undecided"``
    takes every record without an editor decision. History and the latest
    snapshot are written first, through *state_store* (the run's store) or a
    store opened over *state_dir*, then the current snapshot, then the
    report. ``dry_run`` writes nothing.
    """
    if scope not in {SCOPE_CURRENT, SCOPE_UNDECIDED}:
        raise ValueError(f"unknown scope: {scope}")
    today = date.fromisoformat(effective_date)
    store = state_store if state_store is not None else open_state_store(state_dir)
    history = read_history(store)
    if history is None:
        return {"status": "input_failed", "reason": "invalid_candidate_history"}
    current_path = output_dir / "scouting-candidates.json"
    current = read_json_or_default(current_path, [])
    aliases = load_aliases(store)
    selected = select_records(history, scope, current if isinstance(current, list) else [], aliases)
    report = complete_isbns(
        selected,
        pages=load_source_pages(output_dir),
        getter=getter,
        today=today,
        budget_seconds=budget_seconds,
        monotonic=monotonic,
        progress=progress,
    )
    result: dict[str, Any] = {"status": "ok", "scope": scope, "dry_run": dry_run, **report.as_dict()}
    if dry_run:
        return result
    persist_learned_fields(
        output_dir,
        store,
        spec=_LEARNED_FIELD_SPEC,
        selected=selected,
        touched=report.touched,
        changed=bool(report.records or report.not_found),
        result=result,
        effective_date=effective_date,
        publish_lock=publish_lock,
    )
    return result


# ── Weekly stage ─────────────────────────────────────────────────────────


class ProductionIsbnCompletionStage:
    """The live weekly stage: this run's candidates, the bounded page getter, the heartbeat."""

    def __init__(self, getter_factory: Callable[[], PageGetter | None] = BoundedPageGetter) -> None:
        self._getter_factory = getter_factory
        self._presentation: Callable[[dict[str, Any]], None] | None = None
        self._failure_recorder: Callable[[str, str], None] | None = None

    def set_snapshot_progress_presentation(self, presentation: Callable[[dict[str, Any]], None] | None) -> None:
        self._presentation = presentation

    def set_progress_snapshot_failure_recorder(self, recorder: Callable[[str, str], None] | None) -> None:
        self._failure_recorder = recorder

    def complete(
        self, output_dir: Path, state_dir: Path, *, effective_date: str, state_store: StateStore | None = None
    ) -> dict[str, Any]:
        from autoscout.progress_snapshot import CandidateProgressSnapshot

        tracker = CandidateProgressSnapshot(
            output_dir,
            stage=STAGE_NAME,
            stage_budget_seconds=STAGE_BUDGET_SECONDS,
            failure_recorder=self._failure_recorder,
            presentation=self._presentation,
        )
        return run_isbn_completion(
            output_dir,
            state_dir,
            effective_date=effective_date,
            scope=SCOPE_CURRENT,
            getter=self._getter_factory(),
            progress=tracker,
            state_store=state_store,
        )
