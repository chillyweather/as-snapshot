"""Original editions of translated books (issue #968).

A book found on a Spanish, Portuguese, German or other non-English list is
often a translation. This module holds the pure, offline core of the answer:

1. :func:`translator_credit` reads a bookshop or publisher page already
   fetched for the description and reports whether it carries a translator
   credit (and, sometimes, an explicitly stated original title).
2. :func:`open_library_search_url` and :func:`confirm_original` check a
   model-proposed original title and language against Open Library search
   documents.
3. :func:`build_original_edition` and :func:`validate_original_edition`
   assemble and check the optional ``original_edition`` candidate record;
   :func:`library_statement` is the block for an original a national
   library's record states but Open Library does not confirm (#1139).
4. :data:`PROPOSAL_RESPONSE_SCHEMA` and
   :func:`validate_proposal_response` describe and check a batched model
   proposal response.

Nothing here fetches a page, calls a model, touches weekly or editorial
state, or decides that a book is *not* a translation: a missing translator
credit only means ``unknown``. Only a proposal confirmed against Open
Library is ever stored as the original; an original a national library's
record states is stored as ``library_stated`` with that record as evidence.
"""

from __future__ import annotations

import html as html_lib
import re
from collections.abc import Collection
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode
from urllib.parse import urlsplit

from autoscout.identity import normalize_author_display
from autoscout.identity import normalize_author_identity
from autoscout.identity import normalize_work_title
from autoscout.isbn_completion import main_title

OPEN_LIBRARY_SEARCH_URL = "https://openlibrary.org/search.json"
_OPEN_LIBRARY_SEARCH_FIELDS = "key,title,author_name,first_publish_year,language"
_OPEN_LIBRARY_SEARCH_LIMIT = 10

# ISO 639-1 → the three-letter language code Open Library stores.
OPEN_LIBRARY_LANGUAGE: dict[str, str] = {
    "en": "eng",
    "es": "spa",
    "pt": "por",
    "it": "ita",
    "de": "ger",
    "fr": "fre",
    "nl": "dut",
    "ca": "cat",
    "no": "nor",
    "pl": "pol",
    "cs": "cze",
    "sv": "swe",
    "da": "dan",
    "fi": "fin",
    "ru": "rus",
    "ja": "jpn",
    "zh": "chi",
    "ko": "kor",
    "he": "heb",
    "ar": "ara",
    "hu": "hun",
}

STATUS_CONFIRMED = "confirmed"
STATUS_NOT_FOUND = "not_found"
STATUS_NOT_CHECKED = "not_checked"
# A national library's record states the translation and its original
# language (and, sometimes, the original title), but Open Library did not
# confirm that original (#1139). The library record is the evidence.
STATUS_LIBRARY_STATED = "library_stated"
STATUSES = (STATUS_CONFIRMED, STATUS_NOT_FOUND, STATUS_NOT_CHECKED, STATUS_LIBRARY_STATED)

IS_TRANSLATION_YES = "yes"
IS_TRANSLATION_UNKNOWN = "unknown"
# There is deliberately no "no": a page without a translator credit does not
# prove that a book was written in the language of the list.
IS_TRANSLATION_VALUES = (IS_TRANSLATION_YES, IS_TRANSLATION_UNKNOWN)

ORIGINAL_EDITION_KEYS = (
    "is_translation",
    "translation_evidence_url",
    "status",
    "original_title",
    "original_language",
    "language_confirmed",
    "original_first_published_year",
    "confirmation_url",
)

# ── Translator credit ────────────────────────────────────────────────────

# Markers are matched inside one visible line and must stand on their own:
# a marker never matches inside a longer word ("Übersetzungen" is not
# "Übersetzung" plus "en"). The longest marker wins, so "Traductores" is not
# read as "Traductor" plus a stray "es", and a "translated from <language>"
# phrase is preferred over its shorter prefix.
TRANSLATOR_CREDIT_MARKERS: dict[str, tuple[str, ...]] = {
    "es": (
        "Traducción del \\w+ de",
        "Traducido del \\w+ por",
        "Traducción de",
        "Traducido por",
        "Traductores",
        "Traductora",
        "Traductor",
    ),
    "ca": ("Traducció de l['’]\\w+ de", "Traducció de"),
    "pt": (
        "Tradução do \\w+ de",
        "Traduzido do \\w+ por",
        "Tradução de",
        "Traduzido por",
        "Tradutora",
        "Tradutor",
    ),
    "it": (
        "Traduzione dall['’]\\w+ di",
        "Tradotto dall['’]\\w+ da",
        "Traduzione di",
        "Tradotto da",
        "Traduttrice",
        "Traduttore",
    ),
    "fr": ("Traduit de l['’]\\w+ par", "Traduction de", "Traduit par", "Traductrice", "Traducteur"),
    "de": (
        "Aus dem \\w+ übersetzt von",
        "Aus dem \\w+ von",
        "Übersetzung von",
        "Übersetzt von",
        "Übersetzerin",
        "Übersetzer",
        "Übersetzung",
    ),
    "nl": ("Vertaald uit het \\w+ door", "Vertaald door", "Vertaling", "Vertaler"),
    "no": ("Oversatt fra \\w+ av", "Oversatt av", "Oversetter"),
    "pl": ("Tłumaczenie", "Przekład", "Tłumacz"),
    "cs": ("Přeložila", "Přeložil", "Překlad"),
}

STATED_ORIGINAL_TITLE_MARKERS: dict[str, tuple[str, ...]] = {
    "es": ("Título original",),
    "pt": ("Título original",),
    "it": ("Titolo originale",),
    "fr": ("Titre original",),
    "de": ("Originaltitel",),
    "nl": ("Oorspronkelijke titel",),
    "ca": ("Títol original",),
    "no": ("Originaltittel",),
    "pl": ("Tytuł oryginału",),
    "cs": ("Originální název",),
    "en": ("Original title",),
}


def _compiled(sources: Iterable[str]) -> tuple[re.Pattern[str], ...]:
    """Compile markers longest first, each bounded by a non-word character.

    Lookarounds rather than ``\\b`` because a marker may end with a quote or
    another non-word pattern, where ``\\b`` would not fire.
    """
    return tuple(
        re.compile(rf"(?<!\w)(?:{source})(?!\w)", re.IGNORECASE)
        for source in sorted(set(sources), key=lambda value: (-len(value), value))
    )


TRANSLATOR_CREDIT_PATTERNS: tuple[re.Pattern[str], ...] = _compiled(
    marker for markers in TRANSLATOR_CREDIT_MARKERS.values() for marker in markers
)
STATED_ORIGINAL_TITLE_PATTERNS: tuple[re.Pattern[str], ...] = _compiled(
    marker for markers in STATED_ORIGINAL_TITLE_MARKERS.values() for marker in markers
)

_SKIP_TAGS = frozenset({"script", "style", "noscript", "template"})
_LINE_BREAK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "body",
        "br",
        "caption",
        "dd",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "html",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)

# After a translator name the line often continues with the imprint. These
# words begin an imprint, not a person's surname.
_IMPRINT_WORDS = frozenset(
    {
        "books",
        "casa",
        "ediciones",
        "editora",
        "editorial",
        "editions",
        "edizione",
        "fassung",
        "forlag",
        "grupo",
        "groupe",
        "libros",
        "nakladatelství",
        "press",
        "publishing",
        "publishers",
        "uitgeverij",
        "verlag",
        "wydawnictwo",
        "éditions",
        "édition",
    }
)
_NAME_STOP = re.compile(r"[·|;(]")
_NAME_WORD = re.compile(r"[^\s·|;(]+")
_NAME_MAX_LENGTH = 80
_STATED_TITLE_MAX_LENGTH = 200
_SEPARATORS = " \t:：-–—"


_CELL_TAGS = frozenset({"dd", "dt", "td", "th"})
_VALUE_CELL_TAGS = frozenset({"td", "th"})


@dataclass(frozen=True)
class _Cell:
    """One structural cell of a page: a table cell, a ``<dt>`` or a ``<dd>``.

    ``row`` and ``column`` identify a table cell's place in its own ``<tr>``;
    they are ``-1`` for a ``<dt>``/``<dd>``.
    """

    kind: str
    row: int
    column: int


@dataclass(frozen=True)
class _Line:
    """One visible line of text and the structural cell it came from."""

    text: str
    cell: int | None


def _append_line(result: list[_Line], buffer: list[str], cell: int | None) -> None:
    if not buffer:
        return
    text = re.sub(r"\s+", " ", "".join(buffer)).strip()
    if text:
        result.append(_Line(text=text, cell=cell))


class _VisibleText(HTMLParser):
    """Visible text of a page, one line per block element, content of scripts dropped.

    Each visible line also remembers which structural cell it came from, so a
    label standing alone in a table cell or a ``<dt>`` reads the value from the
    cell that structurally belongs to it instead of from whatever line happens
    to follow. Empty cells are kept as cells with empty text, so a field
    boundary survives an empty value.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[tuple[str, int | None]] = []
        self.cells: list[_Cell] = []
        self._texts: list[list[str]] = []
        self._skip = 0
        self._row = 0
        self._column = 0
        self._row_stack: list[tuple[int, int]] = []
        self._cell_stack: list[int] = []

    def _current_cell(self) -> int | None:
        return self._cell_stack[-1] if self._cell_stack else None

    def _append(self, text: str) -> None:
        cell = self._current_cell()
        self.parts.append((text, cell))
        for index in self._cell_stack:
            self._texts[index].append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        name = tag.casefold()
        if name in _SKIP_TAGS:
            self._skip += 1
            return
        if name == "tr":
            self._row += 1
            self._row_stack.append((self._row, self._column))
            self._column = 0
        elif name in _CELL_TAGS:
            if name in _VALUE_CELL_TAGS:
                self.cells.append(_Cell(kind=name, row=self._row, column=self._column))
                self._column += 1
            else:
                self.cells.append(_Cell(kind=name, row=-1, column=-1))
            self._texts.append([])
            self._cell_stack.append(len(self.cells) - 1)
        if name in _LINE_BREAK_TAGS:
            self._append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.casefold() in _LINE_BREAK_TAGS:
            self._append("\n")

    def handle_endtag(self, tag: str) -> None:
        name = tag.casefold()
        if name in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if name in _CELL_TAGS and self._cell_stack:
            self._cell_stack.pop()
        if name == "tr" and self._row_stack:
            self._row, self._column = self._row_stack.pop()
        if name in _LINE_BREAK_TAGS:
            self._append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._append(data)

    def lines(self) -> list[_Line]:
        self.close()
        result: list[_Line] = []
        buffer: list[str] = []
        cell: int | None = None
        for chunk, chunk_cell in self.parts:
            for position, piece in enumerate(chunk.split("\n")):
                if position:
                    _append_line(result, buffer, cell)
                    buffer, cell = [], None
                if piece:
                    if cell is None:
                        cell = chunk_cell
                    buffer.append(piece)
        _append_line(result, buffer, cell)
        return result

    def cell_text(self, index: int) -> str:
        """The visible text of one structural cell, empty when the cell is empty."""
        return re.sub(r"\s+", " ", html_lib.unescape("".join(self._texts[index]))).strip()


def _visible_text(markup: str) -> _VisibleText:
    parser = _VisibleText()
    try:
        parser.feed(markup or "")
        parser.close()
    except (AssertionError, ValueError):
        return _VisibleText()
    return parser


@dataclass(frozen=True)
class TranslatorCredit:
    """A translator credit or stated original title found on a page."""

    marker: str = ""
    translator: str = ""
    stated_original_title: str = ""


def _clean_name(text: str) -> str | None:
    candidate = text.lstrip(_SEPARATORS)
    if not candidate or len(candidate[0]) != 1 or not candidate[0].isupper():
        return None
    cut = _NAME_MAX_LENGTH
    stop = _NAME_STOP.search(candidate)
    if stop is not None:
        cut = min(cut, stop.start())
    for word in _NAME_WORD.finditer(candidate):
        if word.start() >= cut:
            break
        if word.group(0).casefold().strip(".,;:") in _IMPRINT_WORDS:
            cut = min(cut, word.start())
            break
    name = candidate[:cut].rstrip(" \t,;:.")
    return name or None


def _next_cell_text(page: _VisibleText, index: int | None) -> str | None:
    """The text of the cell that structurally holds a label cell's value."""
    if index is None:
        return None
    label = page.cells[index]
    for other_index in range(index + 1, len(page.cells)):
        other = page.cells[other_index]
        if label.kind in _VALUE_CELL_TAGS:
            if other.kind not in _VALUE_CELL_TAGS:
                continue
            if other.row != label.row or other.column != label.column + 1:
                return None
            return page.cell_text(other_index) or None
        # A <dt> is followed by its own <dd> and nothing else.
        if other.kind == "dd":
            return page.cell_text(other_index) or None
        return None
    return None


def _starts_with_marker(text: str) -> bool:
    """True when a line begins with a translator or original-title marker."""
    candidate = text.lstrip(_SEPARATORS)
    return any(
        pattern.match(candidate) is not None
        for pattern in (*TRANSLATOR_CREDIT_PATTERNS, *STATED_ORIGINAL_TITLE_PATTERNS)
    )


def _label_value(page: _VisibleText, lines: Sequence[_Line], index: int) -> str | None:
    """The value belonging to a marker that stands alone on ``lines[index]``.

    Inside a table cell or a ``<dt>`` the value is only the cell that
    structurally follows the label: the next cell of the same ``<tr>`` or the
    ``<dd>`` right after the ``<dt>``. An empty or missing cell means the page
    states no value, so the next field's label is never read as this one's.
    Anywhere else the immediately following visible line is the value, unless
    that line is itself a marker.
    """
    line = lines[index]
    if line.cell is not None:
        cell = page.cells[line.cell]
        if cell.kind in _VALUE_CELL_TAGS or cell.kind == "dt":
            return _next_cell_text(page, line.cell)
        return None
    if index + 1 >= len(lines):
        return None
    following = lines[index + 1].text
    if _starts_with_marker(following):
        return None
    return following or None


def _name_after_marker(page: _VisibleText, lines: Sequence[_Line], index: int, match: re.Match[str]) -> str | None:
    line = lines[index].text
    remainder = line[match.end() :].lstrip(_SEPARATORS)
    if remainder:
        return _clean_name(remainder)
    # A table cell or metadata row can carry the label alone; the value is in
    # the cell that structurally belongs to the label, never in the next field.
    if line[: match.start()].strip(_SEPARATORS):
        return None
    value = _label_value(page, lines, index)
    if value is None:
        return None
    return _clean_name(value)


def _stated_original_title(page: _VisibleText, lines: Sequence[_Line]) -> tuple[str, str]:
    for index, entry in enumerate(lines):
        line = entry.text
        for pattern in STATED_ORIGINAL_TITLE_PATTERNS:
            match = pattern.search(line)
            if match is None:
                continue
            title = line[match.end() :].lstrip(_SEPARATORS)
            if not title:
                # A table cell or metadata row can carry the label alone; the
                # title is in the cell that structurally belongs to it. Unlike
                # a translator name, a title need not start with a capital.
                if line[: match.start()].strip(_SEPARATORS):
                    continue
                value = _label_value(page, lines, index)
                if value is None:
                    continue
                title = value
            title = title[:_STATED_TITLE_MAX_LENGTH].strip()
            if not title:
                continue
            return title, match.group(0)
    return "", ""


def translator_credit(markup: str) -> TranslatorCredit | None:
    """The first translator credit on a page, or ``None``.

    The page is reduced to its visible text first, so a hidden label
    dictionary inside a script never counts. A translator marker must be
    followed by a capitalised name; an original-title marker alone also
    counts as a translation credit.
    """
    page = _visible_text(markup)
    lines = page.lines()
    stated_title, stated_marker = _stated_original_title(page, lines)
    for index, entry in enumerate(lines):
        for pattern in TRANSLATOR_CREDIT_PATTERNS:
            match = pattern.search(entry.text)
            if match is None:
                continue
            translator = _name_after_marker(page, lines, index, match)
            if translator is None:
                continue
            return TranslatorCredit(
                marker=match.group(0),
                translator=translator,
                stated_original_title=stated_title,
            )
    if stated_marker:
        return TranslatorCredit(marker=stated_marker, translator="", stated_original_title=stated_title)
    return None


# ── Open Library confirmation ────────────────────────────────────────────


def _surname(author: str) -> str:
    words = normalize_author_display(str(author or "")).split()
    return words[-1] if words else ""


def open_library_search_url(original_title: str, author: str, *, fields: str = _OPEN_LIBRARY_SEARCH_FIELDS) -> str:
    """The Open Library search for a proposed original title and its author."""
    parameters = {
        "title": main_title(original_title),
        "author": _surname(author),
        "fields": fields,
        "limit": _OPEN_LIBRARY_SEARCH_LIMIT,
    }
    return f"{OPEN_LIBRARY_SEARCH_URL}?{urlencode(parameters)}"


def _author_key(name: Any) -> str:
    return normalize_author_identity(normalize_author_display(str(name or "")))


def _authors_match(left: Any, right: Any) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    left_key = _author_key(left)
    right_key = _author_key(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    left_parts = left_key.split()
    right_parts = right_key.split()
    if len(left_parts) < 2 or len(right_parts) < 2:
        return False
    # Same surname and same first given name: middle initials do not matter,
    # but a different surname always does ("Robert Service" ≠ "Robert Greene").
    return left_parts[-1] == right_parts[-1] and left_parts[0] == right_parts[0]


def work_title_key(title: Any) -> str:
    """The work-level key two titles must share to name the same work."""
    return normalize_work_title(main_title(str(title or "")))


def title_and_author_agree(
    doc_titles: Iterable[Any], doc_authors: Any, wanted_title: str, candidate_authors: Sequence[str]
) -> bool:
    """True when one document title is the wanted work title and one author matches in full.

    This is the strict match of the original-edition confirmation: the whole
    main title must agree (a shared first word is not enough, so "Castles"
    is not "Castles made of sand"), and so must an author's surname and first
    given name, never a surname alone.
    """
    wanted = work_title_key(wanted_title)
    if not wanted:
        return False
    if not any(isinstance(title, str) and title.strip() and work_title_key(title) == wanted for title in doc_titles):
        return False
    if not isinstance(doc_authors, list):
        return False
    return any(_authors_match(author, doc_author) for author in candidate_authors for doc_author in doc_authors)


def _year(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _doc_languages(doc: Mapping[str, Any]) -> list[str]:
    languages = doc.get("language")
    if not isinstance(languages, list):
        return []
    return [str(language) for language in languages if language]


def _candidate_authors(candidate: Mapping[str, Any]) -> list[str]:
    authors = candidate.get("primary_authors")
    if isinstance(authors, list | tuple):
        return [str(author) for author in authors]
    if authors:
        return [str(authors)]
    return []


def _confirmation(
    *,
    status: str,
    original_title: str = "",
    original_language: str = "",
    language_confirmed: bool = False,
    first_published_year: int | None = None,
    confirmation_url: str = "",
) -> dict[str, Any]:
    return {
        "status": status,
        "original_title": original_title,
        "original_language": original_language,
        "language_confirmed": language_confirmed,
        "original_first_published_year": first_published_year,
        "confirmation_url": confirmation_url,
    }


def _not_found() -> dict[str, Any]:
    return _confirmation(status=STATUS_NOT_FOUND)


def library_statement(*, original_title: str, original_language: str) -> dict[str, Any]:
    """The confirmation block for an original a library record states and Open Library did not confirm.

    The language must be one Open Library knows; without it the library
    stated no usable original and the answer is not found.
    """
    language = str(original_language or "").strip().casefold()
    if language not in OPEN_LIBRARY_LANGUAGE:
        return _not_found()
    return _confirmation(
        status=STATUS_LIBRARY_STATED,
        original_title=str(original_title or "").strip(),
        original_language=language,
    )


def confirm_original(
    proposal: Mapping[str, Any], candidate: Mapping[str, Any], docs: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Confirm a proposed original against Open Library search documents.

    A document must have the proposed work title, an author matching one of
    the candidate's authors, a language list that includes the proposal's
    language (or no language list at all), and a first publication year no
    later than the candidate's edition year. Documents whose language list
    confirms the proposed language win over documents without a language
    list; among those the smallest known year wins, unknown years last, and
    input order breaks a tie.
    """
    language = str(proposal.get("original_language") or "").strip().casefold()
    language_code = OPEN_LIBRARY_LANGUAGE.get(language, "")
    proposed_title = str(proposal.get("original_title") or "")
    if not language_code or not main_title(proposed_title):
        return _not_found()
    candidate_year = _year(candidate.get("publication_year"))
    candidate_authors = _candidate_authors(candidate)

    matches: list[tuple[int, int, Mapping[str, Any], bool]] = []
    for index, doc in enumerate(docs):
        if not isinstance(doc, Mapping):
            continue
        key = doc.get("key")
        title = doc.get("title")
        if not isinstance(key, str) or not key.startswith("/"):
            continue
        if not title_and_author_agree([title], doc.get("author_name"), proposed_title, candidate_authors):
            continue
        languages = _doc_languages(doc)
        language_confirmed = bool(languages) and language_code in languages
        if languages and not language_confirmed:
            continue
        year = _year(doc.get("first_publish_year"))
        if year is not None and candidate_year is not None and year > candidate_year:
            continue
        # Confirmed-language documents first, then the smallest known year
        # (unknown years last), then input order.
        sort_year = year if year is not None else 1 << 30
        matches.append((sort_year, index, doc, language_confirmed))

    if not matches:
        return _not_found()
    _sort_year, _index, doc, language_confirmed = min(matches, key=lambda item: (not item[3], item[0], item[1]))
    return _confirmation(
        status=STATUS_CONFIRMED,
        original_title=str(doc.get("title") or ""),
        original_language=language,
        language_confirmed=language_confirmed,
        first_published_year=_year(doc.get("first_publish_year")),
        confirmation_url=f"https://openlibrary.org{doc.get('key', '')}",
    )


# ── The candidate record ─────────────────────────────────────────────────


def build_original_edition(*, credit_url: str, confirmation: Mapping[str, Any] | None) -> dict[str, Any]:
    """Assemble the optional ``original_edition`` block for one candidate."""
    url = str(credit_url or "").strip()
    record: dict[str, Any] = {
        "is_translation": IS_TRANSLATION_YES if url else IS_TRANSLATION_UNKNOWN,
        "translation_evidence_url": url,
    }
    if confirmation is None:
        record.update(_confirmation(status=STATUS_NOT_CHECKED))
        return record
    record.update(
        {
            "status": str(confirmation.get("status") or STATUS_NOT_CHECKED),
            "original_title": str(confirmation.get("original_title") or ""),
            "original_language": str(confirmation.get("original_language") or ""),
            "language_confirmed": bool(confirmation.get("language_confirmed")),
            "original_first_published_year": _year(confirmation.get("original_first_published_year")),
            "confirmation_url": str(confirmation.get("confirmation_url") or ""),
        }
    )
    return record


def _is_http_url(url: Any) -> bool:
    """True for a non-empty http(s) URL with a host; malformed input is not."""
    if not isinstance(url, str) or not url:
        return False
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    return parts.scheme in {"http", "https"} and bool(host)


def _is_open_library_url(url: Any) -> bool:
    if not isinstance(url, str) or not url:
        return False
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").casefold()
    except ValueError:
        return False
    return parts.scheme in {"http", "https"} and (host == "openlibrary.org" or host.endswith(".openlibrary.org"))


def validate_original_edition(value: Any) -> bool:
    """True when ``value`` is a complete, internally consistent record."""
    if not isinstance(value, dict) or set(value) != set(ORIGINAL_EDITION_KEYS):
        return False
    if value["is_translation"] not in IS_TRANSLATION_VALUES:
        return False
    if value["status"] not in STATUSES:
        return False
    if not isinstance(value["translation_evidence_url"], str):
        return False
    if not isinstance(value["original_title"], str) or not isinstance(value["original_language"], str):
        return False
    if not isinstance(value["language_confirmed"], bool):
        return False
    year = value["original_first_published_year"]
    if year is not None and (not isinstance(year, int) or isinstance(year, bool)):
        return False
    if not isinstance(value["confirmation_url"], str):
        return False
    # A translation credit and its evidence URL stand or fall together: only
    # a real http(s) page proves a credit, and no credit means no evidence.
    if value["is_translation"] == IS_TRANSLATION_YES:
        if not _is_http_url(value["translation_evidence_url"]):
            return False
    elif value["translation_evidence_url"]:
        return False
    if value["status"] == STATUS_CONFIRMED:
        return bool(
            value["is_translation"] == IS_TRANSLATION_YES
            and value["original_title"].strip()
            and value["original_language"] in OPEN_LIBRARY_LANGUAGE
            and _is_open_library_url(value["confirmation_url"])
        )
    if value["status"] == STATUS_LIBRARY_STATED:
        # A library statement names the language; the title is optional, and
        # nothing Open Library would have confirmed may leak into it.
        return bool(
            value["is_translation"] == IS_TRANSLATION_YES
            and value["original_language"] in OPEN_LIBRARY_LANGUAGE
            and value["original_title"] == value["original_title"].strip()
            and value["language_confirmed"] is False
            and year is None
            and value["confirmation_url"] == ""
        )
    # not_found and not_checked state no original at all: no title, language,
    # confirmed language, year or confirmation page may leak into them.
    return bool(
        value["original_title"] == ""
        and value["original_language"] == ""
        and value["language_confirmed"] is False
        and year is None
        and value["confirmation_url"] == ""
    )


# ── Model proposal contract ──────────────────────────────────────────────

PROPOSAL_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string"},
                    "original_title": {"type": ["string", "null"]},
                    "original_language": {"type": ["string", "null"]},
                },
                "required": ["candidate_id", "original_title", "original_language"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["proposals"],
    "additionalProperties": False,
}
_PROPOSAL_KEYS = frozenset({"candidate_id", "original_title", "original_language"})
_PROPOSAL_TITLE_MAX_LENGTH = 300


def validate_proposal_response(payload: Any, candidate_ids: Collection[str]) -> dict[str, dict[str, str]] | None:
    """The usable proposals keyed by candidate id, or ``None`` on a wrong shape.

    A proposal is usable when its id is requested, its title is a non-blank
    string of at most 300 characters, and its language is a known ISO 639-1
    code. Null or blank proposals mean the model does not know and are
    dropped. Unknown ids are ignored; a duplicate id is a malformed response.
    """
    if not isinstance(payload, Mapping) or set(payload) != {"proposals"}:
        return None
    proposals = payload.get("proposals")
    if not isinstance(proposals, list):
        return None
    wanted = {str(candidate_id) for candidate_id in candidate_ids}
    result: dict[str, dict[str, str]] = {}
    seen: set[str] = set()
    for item in proposals:
        if not isinstance(item, Mapping) or set(item) != _PROPOSAL_KEYS:
            return None
        candidate_id = item.get("candidate_id")
        title = item.get("original_title")
        language = item.get("original_language")
        if not isinstance(candidate_id, str):
            return None
        if title is not None and not isinstance(title, str):
            return None
        if language is not None and not isinstance(language, str):
            return None
        if candidate_id in seen:
            return None
        seen.add(candidate_id)
        if candidate_id not in wanted or not isinstance(title, str) or not isinstance(language, str):
            continue
        clean_title = title.strip()
        clean_language = language.strip().casefold()
        if not clean_title or len(clean_title) > _PROPOSAL_TITLE_MAX_LENGTH:
            continue
        if clean_language not in OPEN_LIBRARY_LANGUAGE:
            continue
        result[candidate_id] = {"original_title": clean_title, "original_language": clean_language}
    return result
