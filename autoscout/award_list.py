"""Bounded annual award lists with unlinked rows (Issue #577, ADR 0019).

An award shortlist page states a year and a category heading, then lists a
handful of books as plain text. ``exact-book-list`` cannot ground it, because
no row links to a book. ``ranking-table`` cannot describe it, because it has
no reporting period, no rank, and usually no identifier.

``award-list`` describes what such a page is. The source declares the award
year, the exact category heading, how one row is written, and how many rows
the category holds. Everything else follows from the page:

- the rows are the declared elements between the category heading and the
  next heading of the same or a higher level;
- a page that lists several categories of one award is one source, and each
  declared category holds the declared number of rows (Issue #578);
- a page that labels its categories with bold paragraphs instead of headings
  declares that element, and such a paragraph then bounds a category as a
  heading does (Issue #795);
- every row must state a title and a primary author in the declared format;
- the row count must equal the declared count;
- the page must state the declared year, and no newer one.

A page that breaks any of these is refused whole. Nothing here fetches a
page, invents a field, or reads a row from outside the declared category.
"""

from __future__ import annotations

import html as _html
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

from autoscout.annual_source_audit import is_annual_source
from autoscout.helper_metadata import HELPER_METADATA_FIELDS
from autoscout.helper_metadata import validate_helper_metadata
from autoscout.source_language import requires_english_helper_fields
from autoscout.source_page_identity import _HEADING_TAGS
from autoscout.source_page_identity import _VOID_TAGS
from autoscout.source_page_identity import _normalize_whitespace
from autoscout.source_page_identity import _pop_visibility
from autoscout.source_page_identity import _push_visibility
from autoscout.source_page_identity import _VisibilityFrame
from autoscout.source_page_identity import normalize_visible_text
from autoscout.source_page_identity import validate_visible_title_selector
from autoscout.source_proposals import normalize_source_identity

FAMILY = "award-list"

YEAR_FIELD = "award_year"
YEAR_SELECTOR_FIELD = "award_year_selector"
CATEGORY_HEADING_FIELD = "award_category_heading"
ROW_SELECTOR_FIELD = "award_row_selector"
ROW_FORMAT_FIELD = "award_row_format"
ROW_COUNT_FIELD = "award_expected_row_count"
AUTHOR_CONJUNCTION_FIELD = "award_author_conjunction"
SPLIT_FIRST_FIELD = "award_row_split_first"
# Issue #578. One page is one source: a second category of the same award is
# declared here, not as a second source on the same URL.
ADDITIONAL_CATEGORY_HEADINGS_FIELD = "award_additional_category_headings"
TITLE_ELEMENT_FIELD = "award_row_title_element"
# Issue #795. A news article labels its lists with a bold paragraph, not a
# heading element.
CATEGORY_LABEL_ELEMENT_FIELD = "award_category_label_element"

REQUIRED_FIELDS: tuple[str, ...] = (
    YEAR_FIELD,
    CATEGORY_HEADING_FIELD,
    ROW_SELECTOR_FIELD,
    ROW_FORMAT_FIELD,
    ROW_COUNT_FIELD,
)
AWARD_LIST_FIELDS: tuple[str, ...] = (
    *REQUIRED_FIELDS,
    YEAR_SELECTOR_FIELD,
    AUTHOR_CONJUNCTION_FIELD,
    SPLIT_FIRST_FIELD,
    ADDITIONAL_CATEGORY_HEADINGS_FIELD,
    TITLE_ELEMENT_FIELD,
    CATEGORY_LABEL_ELEMENT_FIELD,
)

# A row is one of these elements, or an element carrying a declared class.
ROW_TAGS: frozenset[str] = frozenset({"li", "p", "tr", "dd", "div"})
# The emphasis markup a page may set a row's title in.
TITLE_ELEMENT_TAGS: frozenset[str] = frozenset({"em", "i", "cite", "b", "strong"})
# The bold markup a page may set a whole category label paragraph in.
CATEGORY_LABEL_TAGS: frozenset[str] = frozenset({"b", "strong"})
# A label paragraph ranks below every heading element, so any heading ends
# the category it opened.
_LABEL_LEVEL = 7
# HTML lets a page omit some end tags. Each start tag here closes the nearest
# open element of the first set, unless an element of the second set is
# nearer: a list or table nested inside a row has its own items.
_TABLE_SCOPE: frozenset[str] = frozenset({"table"})
_PARAGRAPH_SCOPE: frozenset[str] = frozenset({"table", "td", "th", "caption", "button", "object"})
_CLOSES_PARAGRAPH: tuple[frozenset[str], frozenset[str]] = (frozenset({"p"}), _PARAGRAPH_SCOPE)
_IMPLICIT_CLOSES: dict[str, tuple[tuple[frozenset[str], frozenset[str]], ...]] = {
    "li": ((frozenset({"li"}), frozenset({"ul", "ol"})), _CLOSES_PARAGRAPH),
    "dd": ((frozenset({"dd", "dt"}), frozenset({"dl"})), _CLOSES_PARAGRAPH),
    "dt": ((frozenset({"dd", "dt"}), frozenset({"dl"})), _CLOSES_PARAGRAPH),
    "tr": ((frozenset({"tr"}), _TABLE_SCOPE),),
    "td": ((frozenset({"td", "th"}), _TABLE_SCOPE),),
    "th": ((frozenset({"td", "th"}), _TABLE_SCOPE),),
    **{
        tag: (_CLOSES_PARAGRAPH,)
        for tag in (
            "address",
            "article",
            "aside",
            "blockquote",
            "details",
            "div",
            "dl",
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
            "hgroup",
            "hr",
            "main",
            "menu",
            "nav",
            "ol",
            "p",
            "pre",
            "section",
            "table",
            "ul",
        )  # fmt: skip
    },
}

# The latest edition of an annual award is this year's or last year's. An
# older page was not refreshed, and a list further ahead than next year's
# edition is not a list anything has published.
MAX_YEAR_AGE = 1

REASON_DECLARATION = "award_declaration"
REASON_YEAR_MISSING = "award_year_missing"
REASON_YEAR_SUPERSEDED = "award_year_superseded"
REASON_YEAR_STALE = "award_year_stale"
REASON_REFERENCE_DATE = "award_reference_date"
REASON_CATEGORY_HEADING = "award_category_heading"
REASON_ROW_SHAPE = "award_row_shape"
REASON_ROW_COUNT = "award_row_count"

_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_CLASS_SELECTOR_PART_RE = re.compile(r"\.[A-Za-z_][A-Za-z0-9_-]*")
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]*)\}")
_PLACEHOLDER_PATTERNS: dict[str, str] = {
    "title": r".+?",
    "author": r".+?",
    "ignored": r".+?",
    "rank": r"\d{1,3}",
    "identifier": r"[0-9][0-9Xx -]{8,16}",
}
# A year element is a label. A container that happens to hold a year would
# make the year check pass on anything inside it.
# What follows a separator inside one person's name rather than naming a
# second person: ``Martin Luther King, Jr.``, ``Jane Doe, PhD``.
_NAME_SUFFIXES: frozenset[str] = frozenset(
    {"jr", "sr", "jnr", "snr", "ii", "iii", "iv", "esq", "phd", "md", "obe", "mbe", "cbe", "am", "ao"}
)
MAX_YEAR_LABEL_CHARS = 200
MAX_ROW_CHARS = 500
_ISBN_VALUE_RE = re.compile(r"(?:97[89]\d{10}|\d{9}[\dXx])")
# Inline markup sits inside a word run, so it adds no space of its own:
# ``<em>Memorial Days</em>, Geraldine Brooks`` reads as one comma-separated
# line. Every other element boundary inside a row separates two fields.
_INLINE_TAGS: frozenset[str] = frozenset(
    {"a", "abbr", "b", "cite", "em", "i", "mark", "q", "small", "span", "strong", "sub", "sup", "u"}
)


_MENTION_FIELDS: frozenset[str] = frozenset(
    {
        "title",
        "primary_authors",
        "authors",
        "isbn",
        "current_rank",
        "source_week",
        "source_url",
        "evidence_url",
        "source_check_date",
        "signal_type",
        "signal_strength",
        "evidence_note",
        "candidate_category",
        "english_title_type",
        "english_title_provenance",
        *HELPER_METADATA_FIELDS,
    }
)
_NON_ENGLISH_MENTION_FIELDS: frozenset[str] = _MENTION_FIELDS | {"english_author_display", "english_annotation"}


class AwardListError(ValueError):
    """A refused award page, with the safe status that names why."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class AwardRow:
    """One award row as the page states it."""

    title: str
    author: str
    rank: str
    identifier: str
    text: str
    # The declared category heading the row stands under.
    category: str = ""
    # Where the declared title element sits in ``text``, when one is declared.
    title_span: tuple[int, int] | None = None


@dataclass(frozen=True)
class AwardPage:
    """The declared categories of one award page: its year and bounded rows."""

    year: int
    category_heading: str
    rows: tuple[AwardRow, ...]
    category_headings: tuple[str, ...] = ()


def _normalized_isbn(value: str) -> str:
    return re.sub(r"[^0-9Xx]", "", value).upper()


def _row_segments(value: object) -> list[tuple[str, str]]:
    """Parse a declared row format into literal and placeholder segments."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{ROW_FORMAT_FIELD} must be a nonblank string")
    text = _normalize_whitespace(value)
    segments: list[tuple[str, str]] = []
    seen: list[str] = []
    position = 0
    for match in _PLACEHOLDER_RE.finditer(text):
        name = match.group(1)
        literal = text[position : match.start()]
        if name not in _PLACEHOLDER_PATTERNS:
            raise ValueError(
                f"{ROW_FORMAT_FIELD} placeholder {{{name}}} is not one of: "
                + ", ".join(f"{{{known}}}" for known in _PLACEHOLDER_PATTERNS)
            )
        if seen and not literal:
            raise ValueError(f"{ROW_FORMAT_FIELD} placeholders must be separated by literal text")
        if "{" in literal or "}" in literal:
            raise ValueError(f"{ROW_FORMAT_FIELD} contains an unbalanced brace")
        if name != "ignored" and name in seen:
            raise ValueError(f"{ROW_FORMAT_FIELD} must not repeat {{{name}}}")
        seen.append(name)
        if literal:
            segments.append(("literal", literal))
        segments.append(("field", name))
        position = match.end()
    trailing = text[position:]
    if "{" in trailing or "}" in trailing:
        raise ValueError(f"{ROW_FORMAT_FIELD} contains an unbalanced brace")
    if trailing:
        segments.append(("literal", trailing))
    missing = [name for name in ("title", "author") if name not in seen]
    if missing:
        raise ValueError(f"{ROW_FORMAT_FIELD} must contain " + " and ".join(f"{{{name}}}" for name in missing))
    return segments


def compile_row_format(value: object) -> re.Pattern[str]:
    """Compile a declared row format into the pattern one whole row must match.

    A format is the row written out with placeholders, for example
    ``{title}, {author} ({ignored})``. ``{title}`` and ``{author}`` appear
    exactly once. ``{rank}`` and ``{identifier}`` are optional. ``{ignored}``
    stands for text the row carries but the mention does not, such as a
    publisher, and may repeat. Two placeholders must be separated by literal
    text, because nothing else says where one field ends.

    Every field ends at the first place the format allows.
    """
    parts: list[str] = []
    for kind, text in _row_segments(value):
        if kind == "literal":
            parts.append(re.escape(text))
        else:
            pattern = _PLACEHOLDER_PATTERNS[text]
            parts.append(f"(?:{pattern})" if text == "ignored" else f"(?P<{text}>{pattern})")
    return re.compile("".join(parts))


def _title_fits_span(
    text: str, position: int, end: int, index: int, segments: list[tuple[str, str]], title_span: tuple[int, int] | None
) -> bool:
    """Say whether a title read as ``[position, end)`` is the marked title.

    The marked element begins where the title begins. It ends where the title
    ends, or inside the literal text that follows the title: a page writes
    ``<em>Guts</em>, `` and ``<em>Guts, </em>`` for the same row. Space
    between the element's text and the separator, as in ``<em>Guts </em>,``,
    belongs to neither.
    """
    if title_span is None:
        return True
    if position != title_span[0]:
        return False
    if end >= title_span[1]:
        return not text[title_span[1] : end].strip()
    following = len(segments[index + 1][1]) if index + 1 < len(segments) else 0
    return title_span[1] <= end + following


def row_readings(
    text: str, segments: list[tuple[str, str]], *, limit: int = 2, title_span: tuple[int, int] | None = None
) -> set[tuple[str, str]]:
    """Return the distinct title and author pairs one row can be read as.

    Every way of dividing the row among the format's fields is tried, not
    only the first, so a second reading is found wherever it comes from: a
    separator repeated in a title, in an author list, or in ignored text.
    The search stops once *limit* readings are known. With a *title_span*,
    only a reading whose title is the marked element is a reading.
    """
    readings: set[tuple[str, str]] = set()
    # Only the title and author read so far distinguish two partial readings;
    # how the ignored text was divided does not. Visiting each such state
    # once keeps the search polynomial in the row length.
    visited: set[tuple[int, int, str, str]] = set()

    def fits(name: str, value: str) -> bool:
        if not value.strip():
            return False
        return name in {"title", "author", "ignored"} or re.fullmatch(_PLACEHOLDER_PATTERNS[name], value) is not None

    def read(position: int, index: int, title: str, author: str) -> None:
        state = (position, index, title, author)
        if len(readings) >= limit or state in visited:
            return
        visited.add(state)
        if index == len(segments):
            if position == len(text):
                readings.add((title.strip(), author.strip()))
            return
        kind, value = segments[index]
        if kind == "literal":
            if text.startswith(value, position):
                read(position + len(value), index + 1, title, author)
            return
        ends = [len(text)] if index + 1 == len(segments) else []
        if not ends:
            literal = segments[index + 1][1]
            end = text.find(literal, position + 1)
            while end != -1:
                ends.append(end)
                end = text.find(literal, end + 1)
        for end in ends:
            field = text[position:end]
            if value == "title" and not _title_fits_span(text, position, end, index, segments, title_span):
                continue
            if fits(value, field):
                read(end, index + 1, field if value == "title" else title, field if value == "author" else author)

    read(0, 0, "", "")
    return readings


def first_reading(
    text: str, segments: list[tuple[str, str]], *, title_span: tuple[int, int] | None = None
) -> dict[str, str] | None:
    """Return the fields of the first way *text* fits the format, or ``None``.

    Every field ends at the first place the format allows, which is the
    reading ``compile_row_format`` describes. The pattern itself is not run
    against a page: on a row that almost fits, it retries every division of
    the repeated ``{ignored}`` fields. A position that already failed for a
    segment is not tried again, so a broken row is refused in bounded time.
    With a *title_span*, the title is the marked element and nothing else.
    """
    failed: set[tuple[int, int]] = set()

    def read(position: int, index: int) -> dict[str, str] | None:
        if index == len(segments):
            return {} if position == len(text) else None
        if (position, index) in failed:
            return None
        kind, value = segments[index]
        if kind == "literal":
            rest = read(position + len(value), index + 1) if text.startswith(value, position) else None
        else:
            rest = None
            last = index + 1 == len(segments)
            end = len(text) if last else text.find(segments[index + 1][1], position + 1)
            while rest is None and position < end:
                field = text[position:end]
                marked = value != "title" or _title_fits_span(text, position, end, index, segments, title_span)
                if marked and re.fullmatch(_PLACEHOLDER_PATTERNS[value], field) is not None:
                    rest = read(end, index + 1)
                    if rest is not None and value != "ignored":
                        rest = {**rest, value: field}
                end = -1 if last else text.find(segments[index + 1][1], end + 1)
        if rest is None:
            failed.add((position, index))
        return rest

    return read(0, 0)


def row_format_fields(source: Mapping[str, Any]) -> frozenset[str]:
    """Return the optional row fields the declared format states."""
    try:
        pattern = compile_row_format(source.get(ROW_FORMAT_FIELD))
    except ValueError:
        return frozenset()
    return frozenset(pattern.groupindex) & {"rank", "identifier"}


def _row_selector(value: object) -> tuple[str, tuple[str, ...]]:
    """Return the row tag, or the row class chain, of a declared selector."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{ROW_SELECTOR_FIELD} must be a nonblank string")
    text = value.strip()
    if text.casefold() in ROW_TAGS:
        return text.casefold(), ()
    parts = [part.strip() for part in text.split(">")]
    if any(_CLASS_SELECTOR_PART_RE.fullmatch(part) is None for part in parts):
        raise ValueError(
            f"{ROW_SELECTOR_FIELD} must be one of {', '.join(sorted(ROW_TAGS))} "
            "or .class tokens joined by direct-child '>' combinators"
        )
    return "", tuple(part[1:] for part in parts)


def _declared_year(source: Mapping[str, Any]) -> int:
    year = source.get(YEAR_FIELD)
    if isinstance(year, bool) or not isinstance(year, int) or not 2000 <= year <= 2099:
        raise ValueError(f"{YEAR_FIELD} must be a four-digit year")
    return year


def _declared_row_count(source: Mapping[str, Any]) -> int:
    count = source.get(ROW_COUNT_FIELD)
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError(f"{ROW_COUNT_FIELD} must be a positive integer")
    return count


def category_headings(source: Mapping[str, Any]) -> tuple[str, ...]:
    """Return every declared category heading, the first one first."""
    first = str(source.get(CATEGORY_HEADING_FIELD) or "").strip()
    additional = source.get(ADDITIONAL_CATEGORY_HEADINGS_FIELD)
    if additional in (None, "", []):
        return (first,)
    if not isinstance(additional, list) or any(not isinstance(item, str) or not item.strip() for item in additional):
        raise ValueError(f"{ADDITIONAL_CATEGORY_HEADINGS_FIELD} must be a list of nonblank headings")
    headings = (first, *(item.strip() for item in additional))
    if len({normalize_visible_text(heading) for heading in headings}) != len(headings):
        raise ValueError(f"{ADDITIONAL_CATEGORY_HEADINGS_FIELD} must not repeat a declared category heading")
    return headings


def _title_element(source: Mapping[str, Any]) -> str:
    """Return the declared title element tag, or an empty string."""
    value = source.get(TITLE_ELEMENT_FIELD)
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or value.strip().casefold() not in TITLE_ELEMENT_TAGS:
        raise ValueError(f"{TITLE_ELEMENT_FIELD} must be one of: {', '.join(sorted(TITLE_ELEMENT_TAGS))}")
    if source.get(SPLIT_FIRST_FIELD) is True:
        # Two rules for where the title ends cannot both hold.
        raise ValueError(f"{TITLE_ELEMENT_FIELD} and {SPLIT_FIRST_FIELD} must not both be declared")
    return value.strip().casefold()


def _category_label_element(source: Mapping[str, Any]) -> str:
    """Return the declared category label tag, or an empty string."""
    value = source.get(CATEGORY_LABEL_ELEMENT_FIELD)
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or value.strip().casefold() not in CATEGORY_LABEL_TAGS:
        raise ValueError(f"{CATEGORY_LABEL_ELEMENT_FIELD} must be one of: {', '.join(sorted(CATEGORY_LABEL_TAGS))}")
    if str(source.get(ROW_SELECTOR_FIELD) or "").strip().casefold() == "p":
        # A paragraph cannot be both a row and the label that bounds the rows.
        raise ValueError(f"{CATEGORY_LABEL_ELEMENT_FIELD} must not be declared with {ROW_SELECTOR_FIELD} p")
    return value.strip().casefold()


def page_declaration_error(source: Mapping[str, Any]) -> str:
    """Return why the declared page shape is unusable, or an empty string.

    These are the declarations a page is read with. A probe has only these;
    a configured source also answers :func:`validate_declaration`.
    """
    missing = [field for field in REQUIRED_FIELDS if source.get(field) in (None, "")]
    if missing:
        return f"{FAMILY} requires " + ", ".join(missing)
    # An award list has no reporting period, no freshness window, and no
    # title exclusions. A declaration of one would be read by nothing.
    foreign = sorted(
        field
        for field in source
        if (field.startswith("ranking_") or field == "adult_nonfiction_exclusions")
        and source[field] not in (None, "", [], False)
    )
    if foreign:
        return f"{FAMILY} does not use ranking-table declarations: " + ", ".join(foreign)
    # An award category is a set, not a sorted list. A sort label would be
    # one more thing the page must state, and the replay page keeps only the
    # headings, the year, and the rows.
    if str(source.get("expected_sort_label") or "").strip():
        return f"{FAMILY} does not use expected_sort_label: an award category declares no sort order"
    try:
        _declared_year(source)
        _declared_row_count(source)
        compile_row_format(source.get(ROW_FORMAT_FIELD))
        _author_conjunction(source)
        if source.get(SPLIT_FIRST_FIELD) not in (None, "", True, False):
            raise ValueError(f"{SPLIT_FIRST_FIELD} must be true or false")
        _title_element(source)
        _category_label_element(source)
        _row_tag, row_classes = _row_selector(source.get(ROW_SELECTOR_FIELD))
        if source.get(YEAR_SELECTOR_FIELD) not in (None, ""):
            validate_visible_title_selector(source[YEAR_SELECTOR_FIELD])
            year_classes = tuple(_class_chain(str(source[YEAR_SELECTOR_FIELD])))
            # A year element that contains the rows is a container, not a label.
            if row_classes[: len(year_classes)] == year_classes:
                raise ValueError(
                    f"{YEAR_SELECTOR_FIELD} must name the element that states the year, not one that contains the rows"
                )
    except ValueError as exc:
        return str(exc)
    heading = source.get(CATEGORY_HEADING_FIELD)
    if not isinstance(heading, str) or not heading.strip():
        return f"{CATEGORY_HEADING_FIELD} must be a nonblank string"
    try:
        category_headings(source)
    except ValueError as exc:
        return str(exc)
    return ""


def validate_declaration(source: Mapping[str, Any]) -> str:
    """Return why an award-list declaration is unusable, or an empty string.

    The scaffold and the configuration loader both call this, so a
    declaration one of them accepts is one the other accepts.
    """
    if str(source.get("contract_family", "")).strip().casefold() != FAMILY:
        return f"{FAMILY} requires contract_family {FAMILY}"
    for flag in ("metadata_ranking_table", "metadata_exact_book_list"):
        if source.get(flag) is True:
            return f"{FAMILY} must not set {flag}"
    page_error = page_declaration_error(source)
    if page_error:
        return page_error
    # Every mention must carry the declared strength exactly, so a source
    # that declares none has no mention that could satisfy the contract.
    if not str(source.get("expected_signal_strength") or "").strip():
        return f"{FAMILY} requires expected_signal_strength"
    # The annual audit is what reports a list that is one edition behind, and
    # it reads only sources that declare an annual cadence.
    if not is_annual_source(source):
        return f"{FAMILY} requires an annual check_frequency or refresh_cadence"
    return ""


class _CategoryRowCollector(HTMLParser):
    """Collect the visible rows under each exact category heading.

    A category runs from its heading to the next visible heading of the same
    or a higher level. A hidden heading neither opens nor closes it, and a
    hidden row is not a row.

    With a declared label element, a category heading is instead a paragraph
    whose whole visible text is set in one such element. The category runs
    from that label to the next label paragraph or the next heading.
    """

    def __init__(
        self,
        headings: tuple[str, ...],
        row_tag: str,
        row_classes: tuple[str, ...],
        year_classes: tuple[str, ...] = (),
        title_tag: str = "",
        label_tag: str = "",
    ) -> None:
        super().__init__(convert_charrefs=True)
        self._label_tag = label_tag
        # Depth of the open paragraph that may be a category label, and of
        # the label element inside it.
        self._label_depth = 0
        self._label_element_depth = 0
        self._label_elements = 0
        self._label_parts: list[str] = []
        self._label_other_text = False
        self._year_classes = year_classes
        self._year_depth = 0
        self._year_parts: list[str] = []
        # The visible text of every element the year selector matches.
        self.year_labels: list[str] = []
        self.year_contains_rows = False
        self._headings = {normalize_visible_text(heading): heading for heading in headings}
        self._category = ""
        self._title_tag = title_tag
        self._title_depth = 0
        self._title_start = 0
        self._title_marks: list[tuple[int, int]] = []
        self._row_tag = row_tag
        self._row_classes = row_classes
        self._visibility: list[_VisibilityFrame] = []
        self._elements: list[tuple[str, frozenset[str]]] = []
        self._heading_tag = ""
        self._heading_parts: list[str] = []
        self._category_level = 0
        self._row_depth = 0
        self._row_parts: list[str] = []
        self.heading_matches: dict[str, int] = dict.fromkeys(headings, 0)
        # Visible headings outside the rows. A heading inside a row is part
        # of that row, usually its title, and states nothing about the page.
        self.headings: list[str] = []
        # Depth of an open row-shaped element outside the declared category,
        # such as a book row of another category on the same page.
        self._other_row_depth = 0
        self._heading_in_other_row = False
        self.rows: list[str] = []
        # For each row, the declared category it stands under and where each
        # declared title element sits in its text.
        self.row_categories: list[str] = []
        self.row_title_spans: list[tuple[tuple[int, int] | None, ...]] = []

    def _is_row(self, tag: str, classes: frozenset[str]) -> bool:
        if self._row_tag:
            return tag == self._row_tag
        return self._matches(classes, self._row_classes)

    def _matches(self, classes: frozenset[str], chain: tuple[str, ...]) -> bool:
        if chain[-1] not in classes:
            return False
        ancestors = [element[1] for element in self._elements[:-1]]
        if len(ancestors) < len(chain) - 1:
            return False
        return all(wanted in actual for wanted, actual in zip(chain[:-1], ancestors[-(len(chain) - 1) :], strict=False))

    def _close_year(self) -> None:
        if not self._year_depth:
            return
        self.year_labels.append(_normalize_whitespace("".join(self._year_parts)))
        self._year_depth = 0
        self._year_parts = []

    def _close_title(self) -> None:
        if not self._title_depth:
            return
        self._title_marks.append((self._title_start, len(self._row_parts)))
        self._title_depth = 0

    def _title_span(self, text: str, start_part: int, end_part: int) -> tuple[int, int] | None:
        """Locate one title element in the row's normalized *text*."""
        before = "".join(self._row_parts[:start_part])
        inside = "".join(self._row_parts[start_part:end_part])
        title = _normalize_whitespace(inside)
        leading = _normalize_whitespace(before)
        spaced = bool(leading) and (before[-1:].isspace() or inside[:1].isspace())
        start = len(leading) + (1 if spaced else 0)
        # An empty element marks nothing, and a span that does not read back
        # as the element's own text is not a position in the row.
        if not title or text[start : start + len(title)] != title:
            return None
        return start, start + len(title)

    def _close_label(self) -> None:
        if not self._label_depth:
            return
        text = _normalize_whitespace("".join(self._label_parts))
        # A paragraph that only emphasizes a phrase, or holds two bold runs,
        # is prose and bounds nothing.
        is_label = bool(text) and self._label_elements == 1 and not self._label_other_text
        self._label_depth = 0
        self._label_element_depth = 0
        if not is_label:
            return
        self._category_level = 0
        category = self._headings.get(normalize_visible_text(text), "")
        if category:
            self.heading_matches[category] += 1
            self._category = category
            self._category_level = _LABEL_LEVEL

    def _close_row(self) -> None:
        if not self._row_depth:
            return
        self._close_title()
        # An empty row is kept: it is a row that states no title or author.
        text = _normalize_whitespace("".join(self._row_parts))
        self.rows.append(text)
        self.row_categories.append(self._category)
        self.row_title_spans.append(tuple(self._title_span(text, start, end) for start, end in self._title_marks))
        self._row_depth = 0
        self._row_parts = []
        self._title_marks = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        # An element whose end tag the page omits is closed where a browser
        # closes it, whether it is a row, a hidden row, or neither. Left open,
        # it would take in the next row's text or pass its hidden state on.
        for targets, barriers in _IMPLICIT_CLOSES.get(tag, ()):
            for element, _classes in reversed(self._elements):
                if element in targets:
                    self.handle_endtag(element)
                    break
                if element in barriers:
                    break
        hidden = _push_visibility(self._visibility, tag, attrs)
        classes = frozenset(
            token for name, value in attrs if name.casefold() == "class" for token in (value or "").split()
        )
        if tag not in _VOID_TAGS:
            self._elements.append((tag, classes))
            # A hidden duplicate of the year label, such as an alternate
            # layout, is not a second label.
            if (
                self._year_classes
                and not hidden
                and not self._year_depth
                and self._matches(classes, self._year_classes)
            ):
                self._year_depth = len(self._elements)
                if self._row_depth or self._other_row_depth:
                    # The year element sits inside a row, of this category
                    # or of another one on the same page.
                    self.year_contains_rows = True
        if hidden:
            return
        if self._year_depth and tag not in _INLINE_TAGS:
            self._year_parts.append(" ")
        if self._row_depth:
            if tag not in _INLINE_TAGS:
                self._row_parts.append(" ")
            if tag == self._title_tag and not self._title_depth:
                self._title_depth = len(self._elements)
                self._title_start = len(self._row_parts)
            return
        if self._heading_tag and tag not in _INLINE_TAGS:
            # A line break or a block inside a heading separates two words.
            self._heading_parts.append(" ")
        if tag in _HEADING_TAGS:
            if not self._heading_tag:
                self._heading_tag = tag
                self._heading_in_other_row = bool(self._other_row_depth)
                self._heading_parts = []
                if self._category_level and int(tag[1]) <= self._category_level:
                    self._category_level = 0
            return
        if self._label_depth:
            if tag == self._label_tag:
                # A nested element counts too: a label is one element, not two.
                self._label_elements += 1
                if not self._label_element_depth:
                    self._label_element_depth = len(self._elements)
            elif tag not in _INLINE_TAGS:
                self._label_parts.append(" ")
        elif (
            self._label_tag
            and tag == "p"
            and not self._heading_tag
            and not self._other_row_depth
            and not self._is_row(tag, classes)
        ):
            self._label_depth = len(self._elements)
            self._label_elements = 0
            self._label_parts = []
            self._label_other_text = False
            return
        if tag in _VOID_TAGS or not self._is_row(tag, classes):
            return
        # A paragraph that contains a row is a container, not a label.
        self._label_other_text = True
        if not self._category_level:
            # A bare ``div`` selector matches most of a page outside the
            # category, so only a class-selected ``div`` means a row there.
            if self._row_tag != "div" and not self._other_row_depth:
                self._other_row_depth = len(self._elements)
                # A year label that is itself a paragraph outside the
                # category is a label; one that contains a row is not.
                if self._year_depth and self._year_depth < len(self._elements):
                    self.year_contains_rows = True
            return
        self._row_depth = len(self._elements)
        self._row_parts = []
        if self._year_depth:
            self.year_contains_rows = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        hidden = _pop_visibility(self._visibility, tag)
        if self._row_depth and tag not in _INLINE_TAGS and not hidden:
            self._row_parts.append(" ")
        if self._year_depth and tag not in _INLINE_TAGS and not hidden:
            self._year_parts.append(" ")
        if tag == self._heading_tag and not hidden:
            text = _normalize_whitespace("".join(self._heading_parts))
            if text and not self._heading_in_other_row:
                self.headings.append(text)
            # A heading inside another category's book row is part of that
            # row, so it neither states the year nor opens the category.
            # A page that declares a label element names its categories in
            # label paragraphs, and a heading with the same text is not one.
            category = "" if self._label_tag else self._headings.get(normalize_visible_text(text), "")
            if category and not self._heading_in_other_row:
                self.heading_matches[category] += 1
                self._category = category
                self._category_level = int(tag[1])
            self._heading_tag = ""
        elif self._heading_tag and tag not in _INLINE_TAGS and not hidden:
            self._heading_parts.append(" ")
        for index in range(len(self._elements) - 1, -1, -1):
            if self._elements[index][0] == tag:
                del self._elements[index:]
                break
        if self._title_depth and len(self._elements) < self._title_depth:
            self._close_title()
        if self._row_depth and len(self._elements) < self._row_depth:
            self._close_row()
        if self._label_element_depth and len(self._elements) < self._label_element_depth:
            self._label_element_depth = 0
        if self._label_depth and len(self._elements) < self._label_depth:
            self._close_label()
        if self._year_depth and len(self._elements) < self._year_depth:
            self._close_year()
        if self._other_row_depth and len(self._elements) < self._other_row_depth:
            self._other_row_depth = 0

    def handle_data(self, data: str) -> None:
        if self._visibility and self._visibility[-1].hidden:
            return
        if self._year_depth:
            self._year_parts.append(data)
        if self._heading_tag:
            self._heading_parts.append(data)
        elif self._row_depth:
            self._row_parts.append(data)
        elif self._label_element_depth:
            self._label_parts.append(data)
        elif self._label_depth and data.strip():
            self._label_other_text = True

    def close(self) -> None:
        self._close_row()
        self._close_label()
        self._close_year()
        super().close()


def _collected(html_content: str, source: Mapping[str, Any]) -> _CategoryRowCollector:
    """Read *html_content* with the declared headings, row, year, and title elements."""
    row_tag, row_classes = _row_selector(source.get(ROW_SELECTOR_FIELD))
    year_classes = tuple(_class_chain(str(source.get(YEAR_SELECTOR_FIELD) or "")))
    collector = _CategoryRowCollector(
        category_headings(source),
        row_tag,
        row_classes,
        year_classes,
        _title_element(source),
        _category_label_element(source),
    )
    collector.feed(html_content)
    collector.close()
    return collector


def year_texts(html_content: str, source: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the visible texts that may state the award year.

    With a declared ``award_year_selector`` that is the one element it names.
    Without one it is every visible heading outside the award rows: a heading
    inside a row is a book title, and ``Life in 2026`` is not an edition.
    """
    year_classes = tuple(_class_chain(str(source.get(YEAR_SELECTOR_FIELD) or "")))
    collector = _collected(html_content, source)
    if not year_classes:
        return tuple(collector.headings)
    # The label is read by the parser that reads the rows, so an end tag the
    # page omits ends the label where it ends a row. Two matching elements
    # do not say which one states the year.
    labels = [label for label in collector.year_labels if label]
    return tuple(labels) if len(collector.year_labels) == 1 else ()


def stated_year_error(html_content: str, source: Mapping[str, Any]) -> AwardListError | None:
    """Return why the page does not state the declared award year, if it does not.

    The year is read from the visible headings, or from the one element a
    declared ``award_year_selector`` names. A page that also states a newer
    year has rolled over to the next edition, so its rows are no longer the
    declared year's rows.
    """
    year = _declared_year(source)
    year_selector = str(source.get(YEAR_SELECTOR_FIELD) or "").strip()
    texts = year_texts(html_content, source)
    if year_selector and any(len(text) > MAX_YEAR_LABEL_CHARS for text in texts):
        return AwardListError(
            REASON_YEAR_MISSING, f"{YEAR_SELECTOR_FIELD} matches a container, not the element that states the year"
        )
    stated = sorted({int(value) for text in texts for value in _YEAR_RE.findall(text)})
    if not stated:
        return AwardListError(REASON_YEAR_MISSING, "award page states no visible award year")
    if stated[-1] > year:
        return AwardListError(
            REASON_YEAR_SUPERSEDED,
            f"award page states the newer award year {stated[-1]}; the declared {year} list needs a rollover check",
        )
    if year not in stated:
        return AwardListError(REASON_YEAR_MISSING, f"award page does not state the declared award year {year}")
    return None


def award_year_error(
    html_content: str, source: Mapping[str, Any], reference_date: date | None
) -> AwardListError | None:
    """Return why the award year is not current on *reference_date*, if it is not."""
    stated_error = stated_year_error(html_content, source)
    if stated_error is not None:
        return stated_error
    year = _declared_year(source)
    if reference_date is None:
        return AwardListError(
            REASON_REFERENCE_DATE, "award year freshness requires a reference date and the capture carries none"
        )
    if reference_date.year - year > MAX_YEAR_AGE:
        return AwardListError(
            REASON_YEAR_STALE,
            f"award year {year} is stale on {reference_date.isoformat()}; the page was not refreshed for a newer edition",
        )
    if year - reference_date.year > MAX_YEAR_AGE:
        return AwardListError(
            REASON_YEAR_STALE, f"award year {year} is not a published edition on {reference_date.isoformat()}"
        )
    return None


def award_rows(html_content: str, source: Mapping[str, Any]) -> tuple[AwardRow, ...]:
    """Return the declared categories' rows, or raise when the page has drifted."""
    headings = category_headings(source)
    expected = _declared_row_count(source)
    title_tag = _title_element(source)
    collector = _collected(html_content, source)
    if collector.year_contains_rows:
        # The element contains a row or sits inside one. A row's own text, a
        # title such as "Life in 2026", would then state the award year.
        raise AwardListError(
            REASON_YEAR_MISSING, f"{YEAR_SELECTOR_FIELD} matches an element that overlaps the award rows"
        )
    split_first = source.get(SPLIT_FIRST_FIELD) is True
    segments = _row_segments(source.get(ROW_FORMAT_FIELD))
    for heading in headings:
        if collector.heading_matches[heading] != 1:
            raise AwardListError(
                REASON_CATEGORY_HEADING,
                f"award category heading '{heading}' appears {collector.heading_matches[heading]} times; "
                "expected exactly once",
            )
    rows: list[AwardRow] = []
    books: dict[tuple[str, tuple[str, ...]], str] = {}
    for index, text in enumerate(collector.rows, start=1):
        title_span = None
        if title_tag:
            spans = collector.row_title_spans[index - 1]
            if len(spans) != 1 or spans[0] is None:
                raise AwardListError(
                    REASON_ROW_SHAPE,
                    f"award row {index} does not set its title in exactly one <{title_tag}> element",
                )
            title_span = spans[0]
        # A row is one line of title and author. Longer text is prose, and
        # is refused before any reading of it is attempted.
        match = first_reading(text, segments, title_span=title_span) if len(text) <= MAX_ROW_CHARS else None
        fields = {name: value.strip() for name, value in match.items()} if match else {}
        identifier = _normalized_isbn(fields.get("identifier", ""))
        if (
            match is None
            or not fields["title"]
            or not author_names(fields["author"], source)
            or ("identifier" in fields and _ISBN_VALUE_RE.fullmatch(identifier) is None)
        ):
            raise AwardListError(
                REASON_ROW_SHAPE,
                f"award row {index} does not state a title and primary author in the declared row format",
            )
        suffix = next((name for name in author_names(fields["author"], source) if _is_name_suffix(name)), "")
        if suffix:
            # ``Martin Luther King, Jr.`` is one author. Splitting it would
            # publish a cut name and a coauthor who does not exist.
            raise AwardListError(
                REASON_ROW_SHAPE,
                f"award row {index} lists '{suffix}' as an author; it is the suffix of a name "
                "that contains a separator, which the declared author list cannot state",
            )
        if not split_first and len(row_readings(text, segments, title_span=title_span)) > 1:
            raise AwardListError(
                REASON_ROW_SHAPE,
                f"award row {index} can be divided into title and author in more than one way; "
                f"declare {SPLIT_FIRST_FIELD} only if the first separator always divides them",
            )
        category = collector.row_categories[index - 1]
        # One book is one mention. Listed under two declared categories it
        # would be two rows that a single mention cannot both answer.
        book = (
            normalize_visible_text(fields["title"]),
            tuple(sorted(normalize_visible_text(name) for name in author_names(fields["author"], source))),
        )
        if books.setdefault(book, category) != category:
            raise AwardListError(
                REASON_ROW_SHAPE,
                f"award row {index} repeats a book listed under the declared category '{books[book]}'",
            )
        rows.append(
            AwardRow(
                title=fields["title"],
                author=fields["author"],
                rank=str(int(fields["rank"])) if "rank" in fields else "",
                identifier=identifier,
                text=text,
                category=category,
                title_span=title_span,
            )
        )
    for heading in headings:
        count = sum(1 for row in rows if row.category == heading)
        if count != expected:
            named = f" '{heading}'" if len(headings) > 1 else ""
            raise AwardListError(
                REASON_ROW_COUNT, f"award category{named} exposes {count} rows; expected exactly {expected}"
            )
    return tuple(rows)


def award_page(html_content: str, source: Mapping[str, Any], *, reference_date: date | None) -> AwardPage:
    """Read one award page, refusing a stale year, a drifted category, or a broken row."""
    try:
        year_error = award_year_error(html_content, source, reference_date)
        if year_error is not None:
            raise year_error
        rows = award_rows(html_content, source)
    except AwardListError:
        raise
    except ValueError as exc:
        raise AwardListError(REASON_DECLARATION, str(exc)) from exc
    headings = category_headings(source)
    return AwardPage(year=_declared_year(source), category_heading=headings[0], rows=rows, category_headings=headings)


def mention_violation(
    entry: Mapping[str, Any], mention: Mapping[str, Any], page: Mapping[str, Any], source: Mapping[str, Any]
) -> str:
    """Return why one mention is not grounded in one row of the declared category."""
    html_content = str(page.get("html", ""))
    try:
        stated_error = stated_year_error(html_content, source)
        if stated_error is not None:
            raise stated_error
        rows = award_rows(html_content, source)
    except ValueError as exc:
        return f"award list page is not the declared list: {exc}"
    # The page states no reporting period, so a supplied week is not evidence.
    if str(entry.get("source_week") or "").strip():
        return "award list mention carries a source_week the page does not state"
    # A row states a title and its authors, and nothing else is grounded in
    # it. A publication year or a description would be an unsupported fact.
    allowed = _MENTION_FIELDS if not requires_english_helper_fields(dict(source)) else _NON_ENGLISH_MENTION_FIELDS
    prohibited = sorted(set(entry) - allowed)
    if prohibited:
        return f"award list mention contains prohibited fields: {', '.join(prohibited)}"
    configured_url = page_identity(str(source.get("url", "")))
    for field in ("source_url", "evidence_url"):
        value = str(mention.get(field) or entry.get(field) or "")
        if page_identity(value) != configured_url:
            return f"award list {field} is not the configured page"
    expected_signal_type = str(source.get("signal_family") or source.get("family", "")).strip()
    if str(mention.get("signal_type", "")).strip().casefold() != expected_signal_type.casefold():
        return f"award list signal_type must be {expected_signal_type}"
    expected_strength = str(source.get("expected_signal_strength", "")).strip()
    if expected_strength and str(entry.get("signal_strength", "")).strip().casefold() != expected_strength.casefold():
        return f"award list signal_strength must be {expected_strength}"
    eligible_category = str(source.get("eligible_candidate_category", "")).strip()
    if eligible_category and _folded(entry.get("candidate_category", "")) != _folded(eligible_category):
        return f"award list candidate_category must be {eligible_category}"
    title = normalize_visible_text(str(entry.get("title", "")))
    raw_authors = entry.get("primary_authors") or entry.get("authors") or []
    authors = [normalize_visible_text(str(author)) for author in raw_authors] if isinstance(raw_authors, list) else []
    authors = [author for author in authors if author]
    if not title or not authors:
        return "award list mention is missing a title or primary author"
    rank = str(entry.get("current_rank") if entry.get("current_rank") is not None else "").strip()
    rank = str(int(rank)) if rank.isdigit() else rank
    supplied_isbn = str(entry.get("isbn") if entry.get("isbn") is not None else "").strip()
    for row in rows:
        if normalize_visible_text(row.title) != title or not _authors_are_the_row_authors(authors, row.author, source):
            continue
        if rank and rank != row.rank:
            return "award list current_rank is not the rank its row states"
        # Any supplied value must be the row's own identifier. A value that
        # normalizes to nothing, such as ``N/A``, is not one.
        if supplied_isbn and (not row.identifier or _normalized_isbn(supplied_isbn) != row.identifier):
            return "award list isbn is not the identifier its row states"
        break
    else:
        return "award list title and primary author are not grounded in one row of the declared category"
    return validate_helper_metadata(entry, title_relation=requires_english_helper_fields(dict(source))).replace(
        "exact book list", "award list"
    )


def page_identity(url: str) -> tuple[str, str]:
    """Return the page a URL names, query included.

    ``/awards?year=2026`` and ``/awards?year=2025`` are two lists, so the
    query is part of the page; the fragment is not.
    """
    return normalize_source_identity(url.strip()), urlparse(url.strip()).query


def _author_conjunction(source: Mapping[str, Any]) -> str:
    value = source.get(AUTHOR_CONJUNCTION_FIELD)
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or re.fullmatch(r"\w+", value.strip()) is None:
        raise ValueError(f"{AUTHOR_CONJUNCTION_FIELD} must be one word, such as 'and'")
    return value.strip()


def author_names(author_text: str, source: Mapping[str, Any]) -> tuple[str, ...]:
    """Split a row's author text into the complete names it lists.

    Names are separated by ``,``, ``;``, ``/``, ``&``, or the declared
    ``award_author_conjunction``, the word this page puts between the last two
    names. The word is declared, never guessed from the language: ``y`` joins
    two authors on one page and is part of ``José Ortega y Gasset`` on another.
    """
    word = _author_conjunction(source)
    # ``Alice Doe, Bob Roe, and Carol Moe``: a comma and the word after it are
    # one separator, or the last name would begin with the word.
    separator = rf"\s*[,;/&]\s*(?:{re.escape(word)}\s+)?|\s+{re.escape(word)}\s+" if word else r"\s*[,;/&]\s*"
    parts = re.split(separator, _normalize_whitespace(author_text), flags=re.IGNORECASE)
    return tuple(part.strip() for part in parts if part.strip())


def _is_name_suffix(name: str) -> bool:
    return name.casefold().rstrip(".").replace(".", "") in _NAME_SUFFIXES


def author_display(author_text: str, source: Mapping[str, Any]) -> str:
    """Return the row's authors in the one display the helper sidecar is keyed by.

    Collection matches a sidecar entry on the mention's authors joined with
    `` / ``, so a row's author display is its names joined the same way.
    """
    return " / ".join(author_names(author_text, source))


def _authors_are_the_row_authors(authors: list[str], row_author: str, source: Mapping[str, Any]) -> bool:
    """Say whether *authors* are exactly the complete names the row lists.

    A truncated name, a name split in two, several names joined into one, a
    wrong name, and a partial list all fail. Order is not compared.
    """
    return sorted(authors) == sorted(normalize_visible_text(name) for name in author_names(row_author, source))


def _folded(value: object) -> str:
    """Fold a category the way ``normalized_candidate_category`` does."""
    return re.sub(r"[\s_\-]+", " ", str(value)).strip().casefold()


def _class_chain(selector: str) -> list[str]:
    return [part.strip()[1:] for part in selector.split(">") if part.strip()]


def _nested(chain: list[str], inner: str) -> str:
    """Wrap *inner* in one ``div`` per class of *chain*, outermost first."""
    for name in reversed(chain):
        inner = f'<div class="{_html.escape(name)}">{inner}</div>'
    return inner


def _replay_identity_markup(page_heading: str, year_text: str, source: Mapping[str, Any]) -> str:
    """Render the page heading and the stated year once each.

    The year may be stated by the page heading itself, by a separate heading,
    or by a declared element. A declared year element and a declared title
    element can share ancestors, and a shared ancestor is rendered once: a
    second copy would make either selector ambiguous.
    """
    title_chain = _class_chain(str(source.get("visible_title_selector") or ""))
    year_chain = _class_chain(str(source.get(YEAR_SELECTOR_FIELD) or ""))
    title_class = f' class="{_html.escape(title_chain[-1])}"' if title_chain else ""
    heading = f"<h1{title_class}>{_html.escape(page_heading)}</h1>"
    alone = _nested(title_chain[:-1], heading)
    if stated_year_error(alone, source) is None:
        return alone
    year = _html.escape(year_text)
    if title_chain and len(year_chain) > len(title_chain) and year_chain[: len(title_chain)] == title_chain:
        # The year label sits inside the title element, which is rendered once.
        before, found, after = page_heading.partition(year_text)
        label = year
        for name in reversed(year_chain[len(title_chain) :]):
            label = f'<span class="{_html.escape(name)}">{label}</span>'
        inner = (
            f"{_html.escape(before)}{label}{_html.escape(after)}" if found else f"{label} {_html.escape(page_heading)}"
        )
        return _nested(title_chain[:-1], f"<h1{title_class}>{inner}</h1>")
    if not year_chain:
        # The category headings are rendered next, and one may be what states the year.
        if normalize_visible_text(year_text) in {normalize_visible_text(item) for item in category_headings(source)}:
            return alone
        return f"{alone}\n<h2>{year}</h2>"
    if len(year_chain) < len(title_chain) and title_chain[: len(year_chain)] == year_chain:
        # The year label contains the title element, so its text already
        # includes the page heading, which is rendered once, in place.
        before, found, after = year_text.partition(_normalize_whitespace(page_heading))
        if not found:
            raise ValueError(f"{YEAR_SELECTOR_FIELD} contains the page title but its text does not state the heading")
        inner = _html.escape(before) + _nested(title_chain[len(year_chain) : -1], heading) + _html.escape(after)
        return _nested(year_chain, inner)
    shared = 0
    while shared < min(len(year_chain), len(title_chain) - 1) and year_chain[shared] == title_chain[shared]:
        shared += 1
    # The year comes first, so that it never sits inside the category when
    # the page heading is also the category heading.
    return _nested(
        title_chain[:shared],
        _nested(year_chain[shared:], year) + _nested(title_chain[shared:-1], heading),
    )


def _row_markup(row: AwardRow, title_tag: str) -> str:
    """Render one row line, with its title in the declared title element."""
    if not title_tag or row.title_span is None:
        return _html.escape(row.text)
    start, end = row.title_span
    return (
        f"{_html.escape(row.text[:start])}<{title_tag}>{_html.escape(row.text[start:end])}</{title_tag}>"
        f"{_html.escape(row.text[end:])}"
    )


def render_replay_html(html_content: str, source: Mapping[str, Any], page_heading: str) -> str:
    """Render the bounded replay page for an accepted award capture.

    Only what the contract reads is retained: the page heading, the text that
    states the year, each declared category heading, and the row lines
    themselves, with the declared title element where the page sets it. A
    declared category label element keeps its paragraph shape.
    """
    rows = award_rows(html_content, source)
    year = _declared_year(source)
    title_tag = _title_element(source)
    label_tag = _category_label_element(source)
    year_text = next((text for text in year_texts(html_content, source) if str(year) in _YEAR_RE.findall(text)), "")
    lines = [_replay_identity_markup(page_heading, year_text, source)]
    row_tag, row_classes = _row_selector(source.get(ROW_SELECTOR_FIELD))
    # A category named by the page heading has no heading of its own, so its
    # rows come first, directly under the page heading.
    page_title = normalize_visible_text(page_heading)
    ordered = sorted(category_headings(source), key=lambda heading: normalize_visible_text(heading) != page_title)
    for category_heading in ordered:
        if label_tag:
            lines.append(f"<p><{label_tag}>{_html.escape(category_heading)}</{label_tag}></p>")
        elif normalize_visible_text(category_heading) != page_title:
            lines.append(f"<h2>{_html.escape(category_heading)}</h2>")
        markup = [_row_markup(row, title_tag) for row in rows if row.category == category_heading]
        if row_tag == "tr":
            lines.append("<table>")
            lines.extend(f"<tr><td>{line}</td></tr>" for line in markup)
            lines.append("</table>")
        elif row_tag:
            # A ``div`` row takes no wrapper: a wrapping ``div`` would be a row.
            wrapper = {"li": "ul", "dd": "dl", "p": "div"}.get(row_tag, "")
            lines.append(f"<{wrapper}>" if wrapper else "")
            lines.extend(f"<{row_tag}>{line}</{row_tag}>" for line in markup)
            lines.append(f"</{wrapper}>" if wrapper else "")
        else:
            opening = "".join(f'<div class="{_html.escape(name)}">' for name in row_classes[:-1])
            lines.append(opening)
            lines.extend(f'<div class="{_html.escape(row_classes[-1])}">{line}</div>' for line in markup)
            lines.append("</div>" * (len(row_classes) - 1))
    return "<!doctype html>\n<html><body>\n" + "\n".join(line for line in lines if line) + "\n</body></html>\n"
