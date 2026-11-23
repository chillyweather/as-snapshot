"""Config-driven page-identity and ranking-table validation for captured HTML."""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from typing import ClassVar
from typing import Literal
from typing import TypeVar
from urllib.parse import parse_qs
from urllib.parse import unquote
from urllib.parse import urljoin
from urllib.parse import urlsplit

from autoscout.http_evidence import validate_head_404_fallback_declaration
from autoscout.isbn_author_verification import verified_author
from autoscout.ranking_author_policy import ranking_row_author_required
from autoscout.ranking_author_policy import validate_ranking_author_optional_declaration
from autoscout.ranking_detail_route import RankingDetailRoute
from autoscout.ranking_detail_route import ranking_detail_fields_from_html
from autoscout.ranking_detail_route import ranking_detail_requested_fields
from autoscout.ranking_detail_route import ranking_detail_route_declared
from autoscout.ranking_detail_route import ranking_detail_scope
from autoscout.ranking_detail_route import validate_ranking_detail_route
from autoscout.ranking_freshness import ranking_undated_declared
from autoscout.ranking_freshness import validate_undated_freshness_declaration
from autoscout.ranking_identity import ranking_identity_declared
from autoscout.ranking_identity import validate_ranking_identity_declaration
from autoscout.ranking_request import validate_ranking_request_declaration
from autoscout.ranking_response import validate_ranking_response_declaration
from autoscout.source_metadata import RANKING_RANK_SOURCES
from autoscout.source_proposals import normalize_source_identity
from autoscout.source_week_freshness import source_week_freshness_error

_VOID_TAGS: set[str] = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}

_AMAZON_CHARTS_WEEK_RECENCY_DAYS = 14
_NON_RENDERED_TAGS = frozenset({"script", "style", "template", "noscript"})


@dataclass(frozen=True)
class _VisibilityFrame:
    tag: str
    hidden: bool
    retain_hidden_text: bool


def _element_attributes_are_hidden(attrs: list[tuple[str, str | None]]) -> bool:
    """Return whether inline or accessibility attributes hide an element."""
    for name, value in attrs:
        name = name.casefold()
        if name == "hidden":
            return True
        if name == "aria-hidden" and (value or "").strip().casefold() == "true":
            return True
        if name != "style":
            continue
        for declaration in (value or "").split(";"):
            property_name, separator, property_value = declaration.partition(":")
            if not separator:
                continue
            normalized_value = re.sub(r"!important\s*$", "", property_value.strip(), flags=re.IGNORECASE)
            values = normalized_value.casefold().split()
            if (
                property_name.strip().casefold() in {"display", "visibility"}
                and values
                and values[0] in {"none", "hidden"}
            ):
                return True
    return False


_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_ANCHOR_BLOCK_BOUNDARY_TAGS = (
    frozenset(
        {
            "p",
            "div",
            "li",
            "ul",
            "ol",
            "dl",
            "dt",
            "dd",
            "blockquote",
            "figure",
            "figcaption",
            "table",
            "tr",
            "td",
            "th",
            "article",
            "section",
            "header",
            "footer",
            "address",
            "pre",
            "hr",
            "br",
        }
    )
    | _HEADING_TAGS
)


def _element_is_hidden(tag: str, attrs: list[tuple[str, str | None]], *, parent_hidden: bool = False) -> bool:
    """Return the one shared visibility decision for a parsed element."""
    return parent_hidden or tag in _NON_RENDERED_TAGS or _element_attributes_are_hidden(attrs)


def _retain_hidden_text(
    tag: str,
    attrs: list[tuple[str, str | None]],
    *,
    parent_hidden: bool,
    parent_retains_hidden_text: bool,
) -> bool:
    """Return whether hidden data may serve the explicit hidden-text path."""
    if tag in _NON_RENDERED_TAGS:
        return False
    if parent_hidden:
        return parent_retains_hidden_text
    return _element_attributes_are_hidden(attrs)


def _push_visibility(frames: list[_VisibilityFrame], tag: str, attrs: list[tuple[str, str | None]]) -> bool:
    parent_hidden = frames[-1].hidden if frames else False
    parent_retains_hidden_text = frames[-1].retain_hidden_text if frames else False
    hidden = _element_is_hidden(tag, attrs, parent_hidden=parent_hidden)
    retain_hidden_text = _retain_hidden_text(
        tag,
        attrs,
        parent_hidden=parent_hidden,
        parent_retains_hidden_text=parent_retains_hidden_text,
    )
    if tag not in _VOID_TAGS:
        frames.append(_VisibilityFrame(tag, hidden, retain_hidden_text))
    return hidden


def _pop_visibility(frames: list[_VisibilityFrame], tag: str) -> bool | None:
    if tag in _VOID_TAGS:
        return None
    for index in range(len(frames) - 1, -1, -1):
        if frames[index].tag == tag:
            hidden = frames[index].hidden
            del frames[index:]
            return hidden
    return None


def _visibility_is_hidden(frames: list[_VisibilityFrame]) -> bool:
    return bool(frames and frames[-1].hidden)


class _VisibleTextCollector(HTMLParser):
    """Collect visible page text, headings, and table rows/cells."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._visibility_frames: list[_VisibilityFrame] = []
        self.heading_tag: str | None = None
        self.heading_parts: list[str] = []
        self.headings: list[str] = []
        self.hidden_heading_tag: str | None = None
        self.hidden_heading_parts: list[str] = []
        self.hidden_headings: list[str] = []
        self.visible_parts: list[str] = []
        self.hidden_parts: list[str] = []
        self.table_depth = 0
        self.row_depth = 0
        self.cell_tag: str | None = None
        self.cell_parts: list[str] = []
        self.current_row: list[str] = []
        self.table_rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if _push_visibility(self._visibility_frames, tag, attrs):
            # A heading inside attribute-hidden markup is tracked separately so
            # a hidden bot wall can still be recognized by its heading, exactly
            # as a visible one is. Ordinary hidden prose never becomes a heading.
            if (
                self.hidden_heading_tag is None
                and tag in _HEADING_TAGS
                and self._visibility_frames
                and self._visibility_frames[-1].retain_hidden_text
            ):
                self.hidden_heading_tag = tag
                self.hidden_heading_parts = []
            return
        if tag == "table":
            self.table_depth += 1
        elif tag == "tr" and self.table_depth:
            self.row_depth += 1
            if self.row_depth == 1:
                self.current_row = []
        elif tag in {"td", "th"} and self.table_depth and self.row_depth == 1 and self.cell_tag is None:
            self.cell_tag = tag
            self.cell_parts = []
        if self.heading_tag is None and tag in _HEADING_TAGS:
            self.heading_tag = tag
            self.heading_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        closing_hidden = _pop_visibility(self._visibility_frames, tag)
        if closing_hidden is None or closing_hidden:
            if self.hidden_heading_tag is not None and tag == self.hidden_heading_tag:
                hidden_text = _normalize_whitespace("".join(self.hidden_heading_parts))
                if hidden_text:
                    self.hidden_headings.append(hidden_text)
                self.hidden_heading_tag = None
                self.hidden_heading_parts = []
            return
        if self.cell_tag == tag:
            self.current_row.append(_normalize_whitespace(" ".join(self.cell_parts)))
            self.cell_tag = None
            self.cell_parts = []
        elif tag == "tr" and self.table_depth and self.row_depth:
            if self.row_depth == 1 and self.current_row:
                self.table_rows.append(self.current_row)
            self.row_depth -= 1
        elif tag == "table" and self.table_depth:
            self.table_depth -= 1
        if self.heading_tag is not None and tag == self.heading_tag:
            text = _normalize_whitespace("".join(self.heading_parts))
            if text:
                self.headings.append(text)
            self.heading_tag = None
            self.heading_parts = []

    def handle_data(self, data: str) -> None:
        if _visibility_is_hidden(self._visibility_frames):
            if self._visibility_frames[-1].retain_hidden_text and data.strip():
                self.hidden_parts.append(data)
                if self.hidden_heading_tag is not None:
                    self.hidden_heading_parts.append(data)
            return
        if data.strip():
            self.visible_parts.append(data)
            if self.heading_tag is not None:
                self.heading_parts.append(data)
            if self.cell_tag is not None:
                self.cell_parts.append(data)


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize_visible_text(text: str) -> str:
    """Normalize parsed/unescaped visible text for deterministic matching."""
    return _normalize_whitespace(text).casefold()


# Typed link kinds carried by SourceListEntry records so the generic policy
# can ignore non-book links when it qualifies the primary book-level link of
# an entry container (Issue #334).
LINK_KIND_BOOK = "book"
LINK_KIND_ACTION = "action"
LINK_KIND_AUTHOR = "author"
LINK_KIND_NAVIGATION = "navigation"


# Explicit primary-author marker prefixes, matched against visible entry
# context after the title/rank/action labels are stripped. The collector uses
# the stricter colon form below for anchor text so titles containing ``By`` do
# not become author links; the feasibility seam uses this broader locale-aware
# form for visible marker runs.
AUTHOR_MARKER_RE = re.compile(
    r"^\s*(?:(?:by|von|par)\s*:?\s+|(?:от|de|di|author)\s*:\s*|автор\s*:\s*)",
    flags=re.IGNORECASE,
)
# Anchor text has no surrounding structural evidence. Requiring the explicit
# colon form for marker links prevents a valid title such as ``By the River``
# from being classified as an author link.
AUTHOR_LINK_MARKER_RE = re.compile(
    r"^\s*(?:(?:by|von|par|от|de|di|author|автор)\s*:\s*)",
    flags=re.IGNORECASE,
)


# Deterministic commerce/UI action labels. An anchor whose normalized text
# equals one of these is an action link, never a book-level link: "Buy now",
# "Read more", and "Add to cart" buttons must not become second book entries
# (Issue #334). A multi-word label also matches when the text starts with the
# label followed by a non-letter separator ("Buy now - Kindle"), so composite
# button text stays an action. Single-word labels match only exactly: a longer
# text that merely starts with a common word such as "read" or "share" is a
# real title ("Read Write Own"), never an action (Issue #334 review).
# Multi-word labels are additionally stripped from the visible entry context
# before the fallback author parse, so a real author plus an action link
# still works.
ACTION_LABELS = frozenset(
    {
        # Commerce / call-to-action labels, multi-word first.
        "buy now",
        "buy the book",
        "get the book",
        "get it now",
        "order now",
        "preorder now",
        "pre-order now",
        "read more",
        "read a sample",
        "read an excerpt",
        "read the first chapter",
        "read the review",
        "continue reading",
        "start reading",
        "read on",
        "add to cart",
        "add to basket",
        "add to bag",
        "add to wishlist",
        "add to list",
        "save for later",
        "shop now",
        "learn more",
        "show more",
        "see more",
        "view more",
        "load more",
        "view all",
        "see all",
        "view details",
        "more details",
        "all details",
        "product details",
        "book details",
        "view cover",
        "view cover image",
        "view book cover",
        "view larger cover",
        "see cover",
        "open cover",
        "expand cover",
        "enlarge cover",
        "listen now",
        "listen free",
        "listen on audible",
        "listen to a sample",
        "try audible free",
        "members listen free",
        "find out more",
        "browse all",
        "view the list",
        "see the list",
        "back to list",
        "back to top",
        "skip to content",
        "open menu",
        "close menu",
        "sign up",
        "log in",
        "join now",
        "about the author",
        "visit author page",
        "view author",
        "about this book",
        "about the book",
        # Single-word chrome labels. These match exactly (or before a
        # non-letter separator), so "reading" never matches "read" and
        # "buyer" never matches "buy".
        "buy",
        "shop",
        "more",
        "read",
        "listen",
        "details",
        "preorder",
        "pre-order",
        "subscribe",
        "share",
        "save",
        "download",
        "email",
        "print",
        "next",
        "previous",
        "menu",
        "close",
        "explore",
        "expand",
    }
)


def is_action_label(text: str) -> bool:
    """True for a deterministic commerce/UI action label.

    The normalized text must equal a label exactly. A multi-word label also
    matches when the text starts with it followed by a non-letter separator,
    so "Buy now" and "Buy now - Kindle" are actions while "Buyer" and
    "Reading" are not. Single-word labels never classify a longer text: a
    title such as "Read Write Own" merely starting with the single-word
    label "read" is not an action (Issue #334 review)."""
    if text in ACTION_LABELS:
        return True
    for label in ACTION_LABELS:
        if len(label.split()) < 2:
            continue
        if text.startswith(label):
            rest = text[len(label) :]
            if rest and not rest[0].isalpha():
                return True
    return False


# Deterministic accessible cover-label phrases. An anchor whose normalized
# text equals one of these (or starts with a multi-word phrase followed by a
# non-letter separator, so "Cover of Quantum Worlds" and "Cover image of
# Quantum Worlds" classify) is an accessible cover link, never a book-level
# link and never the primary title: a long cover label such as "Cover of
# Quantum Worlds" must not beat the real title "Quantum Worlds" on word
# count alone (Issue #334 review). Single-word phrases match only exactly,
# so a real title such as "Cover Story" is never classified as a cover
# label.
COVER_LABELS = frozenset(
    {
        # Multi-word cover labels, most specific first. The prefix rule
        # applies to multi-word phrases only, exactly like ACTION_LABELS.
        "cover image",
        "cover image of",
        "cover of",
        "cover photo",
        "cover art",
        "cover thumbnail",
        "book cover",
        "book cover image",
        "book jacket",
        "jacket image",
        "jacket art",
        "front cover",
        "back cover",
        "view cover",
        "view cover image",
        "view book cover",
        "view larger cover",
        "see cover",
        "open cover",
        "expand cover",
        "enlarge cover",
        "show cover",
        "display cover",
        "thumbnail image",
        "album cover",
        "audiobook cover",
        "hardcover image",
        "paperback cover",
        # Single-word cover labels: exact match only.
        "cover",
        "jacket",
        "thumbnail",
        "artwork",
    }
)


def is_cover_label(text: str) -> bool:
    """True for a deterministic accessible cover label.

    The normalized text must equal a label exactly. A multi-word label also
    matches when the text starts with it followed by a non-letter separator,
    so "Cover of Quantum Worlds" is a cover label while "Cover Story" and
    "Covering" are not. Single-word labels never classify a longer text, so
    a title such as "Cover Story" merely starting with the single-word
    label "cover" is not a cover label (Issue #334 review)."""
    if text in COVER_LABELS:
        return True
    for label in COVER_LABELS:
        if len(label.split()) < 2:
            continue
        if text.startswith(label):
            rest = text[len(label) :]
            if rest and not rest[0].isalpha():
                return True
    return False


def _element_has_author_attr(attrs: list[tuple[str, str | None]]) -> bool:
    """True when the element carries an explicit author identity attribute.

    ``rel="author"``, ``itemprop="author"``, a populated ``data-author``
    attribute, or an author-bearing class token all mark an author element
    (a linked author for anchors, an author-marked element such as a
    ``span class="author"`` otherwise)."""
    for name, value in attrs:
        name = name.casefold()
        tokens = (value or "").casefold().split()
        if name == "rel" and "author" in tokens:
            return True
        if name == "itemprop" and "author" in tokens:
            return True
        if name == "data-author" and (value or "").strip():
            return True
        if name == "class" and any("author" in token for token in tokens):
            return True
    return False


def _landmark_kind(tag: str, attrs: list[tuple[str, str | None]]) -> str | None:
    """Map an element to a landmark kind, if any.

    ``nav`` elements and ``role="navigation"`` are navigation landmarks;
    ``header``/``footer`` elements and the ``banner``/``contentinfo`` roles
    are the header/footer landmarks. The returned kind drives the
    in_nav/in_header/in_footer flags on collected anchors (Issue #334). A
    header/footer landmark nested inside an actual per-entry container is
    that entry's own header/footer, not page-level site chrome, so the
    collector ignores it there; a navigation landmark always applies, even
    inside an entry container (Issue #334 review)."""
    if tag == "nav":
        return "nav"
    if tag == "header":
        return "header"
    if tag == "footer":
        return "footer"
    for name, value in attrs:
        if name.casefold() == "role":
            role = (value or "").casefold()
            if role == "navigation":
                return "nav"
            if role == "banner":
                return "header"
            if role == "contentinfo":
                return "footer"
    return None


def page_title_text(html_content: str) -> str:
    """Return the normalized text of the document title element."""
    match = re.search(r"<title\b[^>]*>(.*?)</title\s*>", html_content, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    title = re.sub(r"<[^>]+>", " ", match.group(1))
    return _normalize_whitespace(html.unescape(title))


def amazon_charts_path_segment(expected_page_heading: str) -> str:
    """Derive the dated-path segment from the configured page heading.

    "Most Read Nonfiction" becomes "mostread/nonfiction": every word except
    the last joins without spaces, then the final category word follows a
    slash. "Most Sold Nonfiction" becomes "mostsold/nonfiction".

    Production sources configure the segment explicitly via
    ``chart_path_segment`` because the dated URL path is an English Amazon
    path even on non-English marketplaces; this derivation remains the
    documented fallback for direct callers that predate the explicit key."""
    words = [word for word in re.split(r"\s+", expected_page_heading.strip()) if word]
    if len(words) < 2:
        raise ValueError("Amazon Charts expected_page_heading must carry at least two words")
    return "".join(words[:-1]).casefold() + "/" + words[-1].casefold()


def amazon_charts_effective_segment(chart_path_segment: str, expected_page_heading: str) -> str:
    """Return the explicit configured chart segment, or the heading-derived
    fallback for direct callers that do not configure one."""
    segment = str(chart_path_segment or "").strip().strip("/")
    if segment:
        return segment.casefold()
    return amazon_charts_path_segment(expected_page_heading)


_AMAZON_CHARTS_ASIN_PATTERN = re.compile(r"^/dp/([A-Z0-9]{10})(?:/|$)", flags=re.IGNORECASE)


def amazon_charts_asin_from_path(path: str) -> str | None:
    """Return a normalized Amazon Charts ASIN from a direct product path."""
    match = _AMAZON_CHARTS_ASIN_PATTERN.match(path)
    return match.group(1).upper() if match else None


def amazon_charts_dated_url_matches(
    configured_url: str,
    resolved_url: str,
    *,
    chart_path_segment: str = "",
    expected_page_heading: str = "",
) -> bool:
    """Return True when the resolved URL is the configured week-dated chart page.

    The scheme and netloc must equal the configured landing URL, the path must
    match the dated chart pattern for the configured chart path segment, and
    the URL must carry no query or fragment. The explicit segment is the
    production contract (Issue #378); the heading-derived segment remains the
    fallback for direct callers that predate the explicit key."""
    configured = urlsplit(configured_url)
    resolved = urlsplit(resolved_url)
    try:
        segment = amazon_charts_effective_segment(chart_path_segment, expected_page_heading)
    except ValueError:
        return False
    return (
        bool(configured.scheme)
        and configured.scheme.casefold() == resolved.scheme.casefold()
        and configured.netloc.casefold() == resolved.netloc.casefold()
        and re.fullmatch(rf"/charts/\d{{4}}-\d{{2}}-\d{{2}}/{re.escape(segment)}", resolved.path) is not None
        and not resolved.query
        and not resolved.fragment
    )


def amazon_charts_week_url(
    landing_html: str,
    base_url: str,
    *,
    chart_path_segment: str = "",
    expected_page_heading: str = "",
    reference_date: date,
) -> str:
    """Resolve the newest recent Amazon Charts dated link to an absolute URL.

    The link path must carry the configured chart path segment and a week date
    within the recency window of the reference date. Every candidate link is
    considered and the newest week wins, so a landing page that renders the
    previous week before the current one still resolves the current chart;
    equal dates keep document order. Links inside script, style, template,
    and noscript blocks are ignored. Absolute links are accepted only on the
    configured landing host (https), so a German landing page resolves on
    www.amazon.de. Pagination query parameters (page, pg, offset) fail
    closed; other query parameters and fragments are stripped. A landing page
    with no in-window link for the segment fails closed."""
    segment = amazon_charts_effective_segment(chart_path_segment, expected_page_heading)
    configured_host = urlsplit(base_url).netloc.casefold()
    pattern = re.compile(rf"/charts/(\d{{4}}-\d{{2}}-\d{{2}})/{re.escape(segment)}$")
    without_hidden = re.sub(r"(?s)<(script|style|template|noscript)\b.*?</\1>", " ", landing_html, flags=re.IGNORECASE)
    newest_week: date | None = None
    newest_path = ""
    malformed_date_seen = False
    for match in re.finditer(r"\bhref\s*=\s*([\"'])(.*?)\1", without_hidden, flags=re.IGNORECASE | re.DOTALL):
        href = html.unescape(match.group(2).strip())
        parsed = urlsplit(href)
        if parsed.netloc:
            if parsed.scheme.casefold() != "https" or parsed.netloc.casefold() != configured_host:
                continue
        path_match = pattern.fullmatch(parsed.path)
        if path_match is None:
            continue
        resolved_candidate = urljoin(base_url, parsed.path)
        if not amazon_charts_dated_url_matches(
            base_url,
            resolved_candidate,
            chart_path_segment=chart_path_segment,
            expected_page_heading=expected_page_heading,
        ):
            continue
        try:
            week_date = date.fromisoformat(path_match.group(1))
        except ValueError:
            malformed_date_seen = True
            continue
        query_params = parse_qs(parsed.query)
        if any(key.casefold() in {"page", "pg", "offset"} for key in query_params):
            raise ValueError("Amazon Charts nonfiction link carries a pagination query")
        if abs((week_date - reference_date).days) > _AMAZON_CHARTS_WEEK_RECENCY_DAYS:
            continue
        if newest_week is None or week_date > newest_week:
            newest_week = week_date
            newest_path = parsed.path
    if newest_week is None:
        if malformed_date_seen:
            raise ValueError("Amazon Charts landing page contains a malformed dated chart link")
        raise ValueError("Amazon Charts landing page has no recent dated nonfiction link")
    resolved_parts = urlsplit(urljoin(base_url, newest_path))
    if not resolved_parts.scheme or not resolved_parts.netloc:
        raise ValueError("Amazon Charts nonfiction link did not resolve to an absolute URL")
    return resolved_parts._replace(query="", fragment="").geturl()


@dataclass
class _AmazonChartsBlock:
    rank: int
    depth: int
    href: str = ""
    title_parts: list[str] = field(default_factory=list)
    visible_parts: list[str] = field(default_factory=list)
    title_depth: int | None = None


class _AmazonChartsEntryCollector(HTMLParser):
    """Collect bounded Amazon Charts rank blocks."""

    def __init__(self, source_page: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source_page = source_page
        self.depth = 0
        self._visibility_frames: list[_VisibilityFrame] = []
        self.block: _AmazonChartsBlock | None = None
        self.blocks: list[_AmazonChartsBlock] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        is_void = tag in _VOID_TAGS
        if not is_void:
            self.depth += 1
        if _push_visibility(self._visibility_frames, tag, attrs):
            return
        if tag == "div":
            attributes = {name.casefold(): value or "" for name, value in attrs}
            id_value = attributes.get("id", "").strip()
            rank_match = re.fullmatch(r"rank(\d+)", id_value, flags=re.IGNORECASE)
            if rank_match is None and not re.fullmatch(r"rank\d+-.*", id_value, flags=re.IGNORECASE):
                rank_match = re.fullmatch(r"(\d+)", attributes.get("data-rank", ""))
            if rank_match is not None:
                if self.block is not None:
                    self._finish_block()
                self.block = _AmazonChartsBlock(rank=int(rank_match.group(1)), depth=self.depth)
            if self.block is not None:
                classes = set(attributes.get("class", "").split())
                if "kc-rank-card-title" in classes:
                    self.block.title_depth = self.depth
        if self.block is not None:
            href = next(
                (value.strip() for name, value in attrs if name.casefold() == "href" and isinstance(value, str)),
                "",
            )
            if href and not self.block.href and amazon_charts_asin_from_path(urlsplit(href).path) is not None:
                self.block.href = urljoin(self.source_page, html.unescape(href))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        is_void = tag in _VOID_TAGS
        closing_hidden = _pop_visibility(self._visibility_frames, tag)
        if closing_hidden is None or closing_hidden:
            if not is_void:
                self.depth = max(0, self.depth - 1)
            return
        if self.block is not None and tag == "div":
            if self.block.title_depth == self.depth:
                self.block.title_depth = None
            if self.block.depth == self.depth:
                self._finish_block()
        if not is_void:
            self.depth = max(0, self.depth - 1)

    def handle_data(self, data: str) -> None:
        if self.block is None or _visibility_is_hidden(self._visibility_frames):
            return
        if data.strip():
            self.block.visible_parts.append(data)
            if self.block.title_depth is not None:
                self.block.title_parts.append(data)

    def close(self) -> None:
        if self.block is not None:
            self._finish_block()
        super().close()

    def _finish_block(self) -> None:
        if self.block is not None:
            self.blocks.append(self.block)
            self.block = None


def _amazon_charts_blocks(html_content: str, source_page: str) -> list[_AmazonChartsBlock]:
    collector = _AmazonChartsEntryCollector(source_page)
    collector.feed(html_content)
    collector.close()
    return collector.blocks


def amazon_charts_entries(
    html_content: str, source_page: str, *, parsed: ParsedCapture | None = None
) -> list[tuple[str, str, str]]:
    """Return Amazon Charts direct product links, titles, and entry context."""
    return [
        (
            block.href,
            _normalize_whitespace(" ".join(block.title_parts)),
            _normalize_whitespace(" ".join(block.visible_parts)),
        )
        for block in capture_for(html_content, source_page, parsed).amazon_charts_blocks()
        if block.href
    ]


def amazon_charts_ranks(html_content: str, source_page: str) -> list[int]:
    """Return Amazon Charts block ranks in document order."""
    return [block.rank for block in _amazon_charts_blocks(html_content, source_page)]


def amazon_charts_mention_is_grounded(
    html_content: str,
    source_page: str,
    *,
    rank: int,
    title: str,
    authors: list[str],
    evidence_url: str,
    parsed: ParsedCapture | None = None,
) -> bool:
    """Ground an Amazon Charts rank, title, authors, and ASIN in one block.

    *parsed*, when given, is the pass's capture of this page (Issue #718).
    """
    wanted_title = normalize_visible_text(title)
    wanted_authors = {normalize_visible_text(author) for author in authors if author.strip()}
    wanted_asin = amazon_charts_asin_from_path(urlsplit(evidence_url).path)
    if not wanted_title or not wanted_authors or wanted_asin is None:
        return False
    for block in capture_for(html_content, source_page, parsed).amazon_charts_blocks():
        if block.rank != rank or not block.href:
            continue
        entry_asin = amazon_charts_asin_from_path(urlsplit(block.href).path)
        if entry_asin is None or entry_asin != wanted_asin:
            continue
        title_text = _normalize_whitespace(" ".join(block.title_parts))
        context = normalize_visible_text(" ".join(block.visible_parts))
        if normalize_visible_text(title_text) == wanted_title and all(
            re.search(rf"(?<!\w){re.escape(author)}(?!\w)", context) is not None for author in wanted_authors
        ):
            return True
    return False


def _parse(markup: str) -> _VisibleTextCollector:
    collector = _VisibleTextCollector()
    collector.feed(markup)
    collector.close()
    return collector


_T = TypeVar("_T")


def _source_key(source: Mapping[str, Any]) -> str:
    return json.dumps(source, sort_keys=True, default=repr)


class ParsedCapture:
    """One captured page, each kind of parse run at most once (Issue #718).

    A collection pass builds one for the page it validates and hands it to
    every mention's grounding check, so a page with N entries is parsed once
    per kind rather than N times. Every result is a pure function of the
    markup, the source page URL, and the declaration it is keyed by, so reuse
    cannot change a grounding decision. It lives for one page in one pass and
    is never stored.
    """

    def __init__(self, html_content: str, source_page: str = "") -> None:
        self.html_content = html_content
        self.source_page = source_page
        self._results: dict[tuple[Any, ...], Any] = {}

    def _once(self, key: tuple[Any, ...], build: Callable[[], _T]) -> _T:
        if key not in self._results:
            self._results[key] = build()
        result: _T = self._results[key]
        return result

    def subpage(self, markup: str) -> ParsedCapture:
        """Return the capture of another page of the same pass, such as a detail page."""
        return self._once(("subpage", markup), lambda: ParsedCapture(markup, self.source_page))

    def visible_text(self) -> _VisibleTextCollector:
        return self._once(("visible_text",), lambda: _parse(self.html_content))

    def entry_blocks(self) -> _EntryBlockCollector:
        return self._once(("entry_blocks",), lambda: _entry_blocks(self.html_content, self.source_page))

    def amazon_charts_blocks(self) -> list[_AmazonChartsBlock]:
        return self._once(("amazon_charts_blocks",), lambda: _amazon_charts_blocks(self.html_content, self.source_page))

    def audible_product_containers(self) -> list[tuple[list[str], str]]:
        return self._once(
            ("audible_product_containers",),
            lambda: _audible_product_containers(self.html_content, self.source_page),
        )

    def audible_de_author_names(self) -> dict[str, list[str]]:
        return self._once(("audible_de_author_names",), lambda: audible_de_entry_author_names(self.html_content))

    def entry_scope(self, entry_scope_selector: str) -> EntryScopeResult:
        return self._once(
            ("entry_scope", entry_scope_selector), lambda: parse_entry_scope(self.html_content, entry_scope_selector)
        )

    def entry_rows(self, entry_scope_selector: str) -> list[str]:
        return self._once(
            ("entry_rows", entry_scope_selector),
            lambda: server_rendered_entry_rows(self.html_content, entry_scope_selector),
        )

    def section(self, section_heading: str) -> ParsedCapture | None:
        """Return the capture of the declared entry section, or None when it is not present exactly once."""

        def build() -> ParsedCapture | None:
            section_html = entry_section_html(self.html_content, section_heading)
            return None if section_html is None else ParsedCapture(section_html, self.source_page)

        return self._once(("section", section_heading), build)

    def ranking_rows(self, source: Mapping[str, Any], detail_route: RankingDetailRoute | None) -> list[RankingRow]:
        return self._once(
            ("ranking_rows", _source_key(source), detail_route),
            lambda: ranking_rows(self.html_content, dict(source), detail_route=detail_route),
        )

    def ranking_page_identity(self, source: Mapping[str, Any], identity_html: str) -> RankingPageIdentity | None:
        return self._once(
            ("ranking_page_identity", _source_key(source), identity_html),
            lambda: ranking_page_identity(self.html_content, dict(source), identity_html=identity_html),
        )

    def ranking_detail_fields(self, route: RankingDetailRoute) -> tuple[dict[str, str], str]:
        return self._once(
            ("ranking_detail_fields", route), lambda: ranking_detail_fields_from_html(self.html_content, route)
        )


def capture_for(html_content: str, source_page: str | None, parsed: ParsedCapture | None) -> ParsedCapture:
    """Return *parsed* when it is the capture of this page, or a fresh one for a single call.

    A None *source_page* is a caller that resolves no links, so any source page matches.
    """
    if parsed is None:
        return ParsedCapture(html_content, source_page or "")
    if parsed.html_content != html_content or source_page not in (None, parsed.source_page):
        raise ValueError("parsed capture is not the capture of this page")
    return parsed


def _joined_visible_text(parsed: _VisibleTextCollector) -> str:
    return normalize_visible_text(" ".join(parsed.visible_parts))


def visible_page_text(html_content: str) -> str:
    """Return the normalized visible text that ``expected_visible_markers`` is matched against.

    The ``<title>`` counts as visible; script, style, template, noscript and
    attribute-hidden markup do not.
    """
    return _joined_visible_text(_parse(html_content))


def parse_heading_texts(html_content: str) -> list[str]:
    """Return the aggregated visible text of each h1-h6 element."""
    return _parse(html_content).headings


def parse_hidden_text(html_content: str) -> str:
    """Return attribute-hidden text, excluding non-rendered tag contents."""
    return _normalize_whitespace(" ".join(_parse(html_content).hidden_parts))


def parse_hidden_heading_texts(html_content: str) -> list[str]:
    """Return the aggregated text of each h1-h6 inside attribute-hidden markup."""
    return _parse(html_content).hidden_headings


_VISIBLE_TITLE_CLASS_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def _validate_class_token_selector(value: object, *, label: str, minimum_parts: int = 1) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} selector must be nonblank")
    selector = value.strip()
    parts = selector.split(">")
    if len(parts) < minimum_parts:
        if minimum_parts == 2:
            raise ValueError(f"{label} selector must contain a scope and entry class token joined by >")
        raise ValueError(f"{label} selector must contain at least {minimum_parts} class-token parts")
    if any(
        not part.strip().startswith(".") or _VISIBLE_TITLE_CLASS_TOKEN_RE.fullmatch(part.strip()[1:]) is None
        for part in parts
    ):
        raise ValueError(f"{label} selector must contain only .class-token parts joined by >")
    return selector


def validate_visible_title_selector(value: object) -> str:
    """Validate ``.class-token`` selectors joined only by direct-child ``>``."""
    return _validate_class_token_selector(value, label="visible title")


def validate_entry_scope_selector(value: object) -> str:
    """Validate a scope path ending in a direct-child entry class token."""
    return _validate_class_token_selector(value, label="entry scope", minimum_parts=2)


def visible_title_selector_classes(selector: str) -> tuple[str, ...]:
    """Return class tokens from a validated visible-title selector."""
    validated = validate_visible_title_selector(selector)
    return tuple(part.strip()[1:] for part in validated.split(">"))


def _entry_scope_selector_classes(selector: str) -> tuple[str, ...]:
    validated = validate_entry_scope_selector(selector)
    return tuple(part.strip()[1:] for part in validated.split(">"))


def _visible_title_is_chrome(tag: str, attrs: list[tuple[str, str | None]], *, page_level: bool) -> bool:
    if tag == "nav":
        return True
    for name, value in attrs:
        if name.casefold() != "role":
            continue
        role = (value or "").strip().casefold()
        if role == "navigation" or (page_level and role in {"banner", "contentinfo"}):
            return True
    return page_level and tag in {"header", "footer"}


EntryScopeFailure = Literal[
    "entry_scope_missing",
    "entry_scope_ambiguous",
    "entry_scope_hidden",
    "entry_scope_chrome",
    "entry_scope_empty",
    "entry_scope_no_candidate_entries",
]


@dataclass(frozen=True)
class EntryScopeResult:
    """The unique visible declared scope or its typed parser failure."""

    failure_cause: EntryScopeFailure | None = None
    selector_classes: tuple[str, ...] = ()


@dataclass
class _EntryScopeFrame:
    tag: str
    hidden: bool
    chrome: bool
    class_tokens: frozenset[str]
    scope_match: bool
    candidate_scope: _EntryScopeFrame | None = None
    parts: list[str] = field(default_factory=list)
    candidate_parts: list[str] = field(default_factory=list)
    visible_candidate_count: int = 0


class _EntryScopeCollector(HTMLParser):
    """Find one declared scope and its visible direct-child entry containers."""

    def __init__(self, selector_classes: tuple[str, ...]) -> None:
        super().__init__(convert_charrefs=True)
        self.selector_classes = selector_classes
        self.frames: list[_EntryScopeFrame] = []
        self.scopes: list[_EntryScopeFrame] = []

    def _matches_prefix(self, class_tokens: frozenset[str]) -> bool:
        prefix = self.selector_classes[:-1]
        if not prefix or prefix[-1] not in class_tokens:
            return False
        ancestors = self.frames[-(len(prefix) - 1) :] if len(prefix) > 1 else []
        return all(expected in frame.class_tokens for expected, frame in zip(prefix[:-1], ancestors, strict=True))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        parent_hidden = any(frame.hidden for frame in self.frames)
        hidden = _element_is_hidden(tag, attrs, parent_hidden=parent_hidden)
        parent_chrome = any(frame.chrome for frame in self.frames)
        chrome = parent_chrome or _visible_title_is_chrome(tag, attrs, page_level=True)
        class_tokens = frozenset(
            token for name, value in attrs if name.casefold() == "class" for token in (value or "").split()
        )
        scope_match = self._matches_prefix(class_tokens)
        frame = _EntryScopeFrame(tag, hidden, chrome, class_tokens, scope_match)
        parent_scope = self.frames[-1] if self.frames and self.frames[-1].scope_match else None
        if parent_scope is not None and self.selector_classes[-1] in class_tokens:
            frame.candidate_scope = parent_scope
        if scope_match:
            self.scopes.append(frame)
        if tag in _VOID_TAGS:
            return
        self.frames.append(frame)

    def _finalize_candidate_frames(self, frames: list[_EntryScopeFrame]) -> None:
        for frame in frames:
            if (
                frame.candidate_scope is not None
                and not frame.hidden
                and not frame.chrome
                and _normalize_whitespace(" ".join(frame.candidate_parts))
            ):
                frame.candidate_scope.visible_candidate_count += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in _NON_RENDERED_TAGS and not any(frame.tag == tag for frame in self.frames):
            return
        for index in range(len(self.frames) - 1, -1, -1):
            if self.frames[index].tag != tag:
                continue
            closing = self.frames[index:]
            del self.frames[index:]
            self._finalize_candidate_frames(closing)
            return

    def close(self) -> None:
        self._finalize_candidate_frames(self.frames)
        self.frames.clear()
        super().close()

    def handle_data(self, data: str) -> None:
        if not data.strip() or any(frame.hidden for frame in self.frames):
            return
        for frame in self.frames:
            if frame.scope_match:
                frame.parts.append(data)
            if frame.candidate_scope is not None:
                frame.candidate_parts.append(data)


def parse_entry_scope(html_content: str, entry_scope_selector: str) -> EntryScopeResult:
    """Parse the unique declared scope using the visible-title selector grammar."""
    selector_classes = _entry_scope_selector_classes(entry_scope_selector)
    collector = _EntryScopeCollector(selector_classes)
    collector.feed(html_content)
    collector.close()
    if len(collector.scopes) > 1:
        return EntryScopeResult(failure_cause="entry_scope_ambiguous", selector_classes=selector_classes)
    if not collector.scopes:
        return EntryScopeResult(failure_cause="entry_scope_missing", selector_classes=selector_classes)
    scope = collector.scopes[0]
    if scope.hidden:
        return EntryScopeResult(failure_cause="entry_scope_hidden", selector_classes=selector_classes)
    if scope.chrome:
        return EntryScopeResult(failure_cause="entry_scope_chrome", selector_classes=selector_classes)
    if not _normalize_whitespace(" ".join(scope.parts)):
        return EntryScopeResult(failure_cause="entry_scope_empty", selector_classes=selector_classes)
    candidate_count = scope.visible_candidate_count
    if candidate_count == 0:
        return EntryScopeResult(failure_cause="entry_scope_no_candidate_entries", selector_classes=selector_classes)
    return EntryScopeResult(selector_classes=selector_classes)


VisiblePageIdentityFailure = Literal["no_match", "multiple_matches", "hidden", "chrome", "empty"]


@dataclass(frozen=True)
class VisiblePageIdentityResult:
    """Parsed page identity texts or the specific configured-selector failure."""

    identity_texts: tuple[str, ...] = ()
    failure_cause: VisiblePageIdentityFailure | None = None


@dataclass
class _VisiblePageIdentityFrame:
    tag: str
    hidden: bool
    chrome: bool
    entry_container: bool
    class_tokens: frozenset[str]
    matching: bool
    match_index: int | None = None
    parts: list[str] = field(default_factory=list)


@dataclass
class _VisiblePageIdentityMatch:
    text: str = ""
    hidden: bool = False
    chrome: bool = False
    void: bool = False


class _VisiblePageIdentityCollector(HTMLParser):
    """Collect one explicitly configured visible page-identity element."""

    def __init__(self, selector_classes: tuple[str, ...]) -> None:
        super().__init__(convert_charrefs=True)
        self.selector_classes = selector_classes
        self.frames: list[_VisiblePageIdentityFrame] = []
        self.matches: list[_VisiblePageIdentityMatch] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        parent_hidden = any(frame.hidden for frame in self.frames)
        parent_chrome = any(frame.chrome for frame in self.frames)
        parent_entry_container = any(frame.entry_container for frame in self.frames)
        hidden = _element_is_hidden(tag, attrs, parent_hidden=parent_hidden)
        entry_container = parent_entry_container or tag in {"li", "article", "tr"}
        chrome = parent_chrome or _visible_title_is_chrome(tag, attrs, page_level=not parent_entry_container)
        class_tokens = frozenset(
            token for name, value in attrs if name.casefold() == "class" for token in (value or "").split()
        )
        matching = self._selector_matches(class_tokens)
        frame = _VisiblePageIdentityFrame(tag, hidden, chrome, entry_container, class_tokens, matching)
        if matching:
            frame.match_index = len(self.matches)
            self.matches.append(_VisiblePageIdentityMatch(hidden=hidden, chrome=chrome))
            frame.parts = []
        if tag in _VOID_TAGS:
            if matching and frame.match_index is not None:
                self.matches[frame.match_index].void = True
            return
        self.frames.append(frame)

    def _selector_matches(self, class_tokens: frozenset[str]) -> bool:
        if self.selector_classes[-1] not in class_tokens:
            return False
        preceding = self.selector_classes[:-1]
        if len(self.frames) < len(preceding):
            return False
        ancestors = self.frames[-len(preceding) :] if preceding else []
        return all(expected in frame.class_tokens for expected, frame in zip(preceding, ancestors, strict=True))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in _NON_RENDERED_TAGS and not any(frame.tag == tag for frame in self.frames):
            return
        for index in range(len(self.frames) - 1, -1, -1):
            if self.frames[index].tag != tag:
                continue
            closing = self.frames[index:]
            del self.frames[index:]
            for frame in closing:
                if frame.matching and frame.match_index is not None:
                    match = self.matches[frame.match_index]
                    match.text = _normalize_whitespace(" ".join(frame.parts))
                    match.hidden = frame.hidden
                    match.chrome = frame.chrome
            return

    def handle_data(self, data: str) -> None:
        if not data.strip() or any(frame.hidden for frame in self.frames):
            return
        for frame in self.frames:
            if frame.matching:
                frame.parts.append(data)


def parse_visible_page_identity(html_content: str, visible_title_selector: str = "") -> VisiblePageIdentityResult:
    """Parse a declared visible page-title element or, without one, h1-h6.

    A supplied selector is authoritative and validated before parsing, so
    malformed operator input always raises ``ValueError``. Selector failures
    are returned as explicit causes in deterministic precedence order.
    """
    if not visible_title_selector:
        headings = parse_heading_texts(html_content)
        if headings:
            return VisiblePageIdentityResult(identity_texts=tuple(headings))
        return VisiblePageIdentityResult()
    selector_classes = visible_title_selector_classes(visible_title_selector)
    collector = _VisiblePageIdentityCollector(selector_classes)
    collector.feed(html_content)
    collector.close()
    # A void match counts toward ambiguity before it is discarded: a selector
    # that matches both a void element and a real one is ambiguous operator
    # input, not a match to be silently resolved in the real element's favour.
    if len(collector.matches) > 1:
        return VisiblePageIdentityResult(failure_cause="multiple_matches")
    non_void_matches = [match for match in collector.matches if not match.void]
    if not non_void_matches:
        return VisiblePageIdentityResult(failure_cause="no_match")
    match = non_void_matches[0]
    if match.hidden:
        return VisiblePageIdentityResult(failure_cause="hidden")
    if match.chrome:
        return VisiblePageIdentityResult(failure_cause="chrome")
    if not match.text:
        return VisiblePageIdentityResult(failure_cause="empty")
    return VisiblePageIdentityResult(identity_texts=(match.text,))


def ranking_table_rows(html_content: str) -> list[list[str]]:
    """Return visible cells for rows that occur inside actual HTML tables."""
    return _parse(html_content).table_rows


@dataclass(frozen=True)
class RankingRow:
    """One declaratively parsed ranking row.

    The generic page parser never reads hidden text.  A hidden identifier is
    available only through an explicit ranking declaration and is therefore a
    narrow contract-specific exception, not a change to generic parsing.
    Rank is read from the declared row field, or assigned from one-based row
    order when the ranking contract declares ``document_order``.
    """

    rank: str
    title: str
    author: str
    identifier: str
    text: str
    identifier_error: str = ""
    detail_url: str = ""
    detail_link_error: str = ""


@dataclass
class _RankingDiagnosticFrame:
    tag: str
    class_tokens: frozenset[str]
    hidden: bool
    direct_parts: list[str] = field(default_factory=list)


@dataclass
class _RankingRowBuilder:
    rank: str = ""
    title: str = ""
    author: str = ""
    identifier: str = ""
    text_parts: list[str] = field(default_factory=list)
    raw_parts: list[str] = field(default_factory=list)
    identifier_values: list[str] = field(default_factory=list)
    detail_link_matches: list[list[str]] = field(default_factory=list)


_ISBN_VALUE_RE = re.compile(r"^(?:97[89]\d{10}|\d{9}[\dXx])$")
_ISBN_CANDIDATE_RE = re.compile(
    r"(?<![\dXx])(?:97[89](?:[-\s.]?\d){10}|\d(?:[-\s.]?\d){8}[-\s.]?[\dXx])(?![\dXx])",
    flags=re.IGNORECASE,
)

_RANK_INTEGER_TOKEN_RE = re.compile(r"(?<!\w)\d+(?!\w)")


def _normalized_selector_rank(value: str) -> str:
    normalized = _normalize_whitespace(value)
    matches = list(_RANK_INTEGER_TOKEN_RE.finditer(normalized))
    if len(matches) != 1:
        return ""
    match = matches[0]
    prefix = normalized[: match.start()].rstrip()
    suffix = normalized[match.end() :].lstrip()
    if prefix.endswith(("-", "−")) or suffix.startswith(("-", "−")):
        return ""
    rank = int(match.group())
    return str(rank) if rank > 0 else ""


def _ranking_detail_url(
    source: Mapping[str, Any], values: list[str], route: RankingDetailRoute | None = None
) -> tuple[str, str]:
    """Compatibility wrapper around the detail-route URL policy."""
    if len(values) != 1:
        if not values:
            return "", "ranking row detail link is missing"
        return "", "ranking row detail link is ambiguous"
    try:
        parsed_route = route or RankingDetailRoute.from_source(source)
    except (TypeError, ValueError, re.error) as exc:
        return "", f"ranking detail URL pattern is malformed: {exc}"
    if parsed_route is None:
        return "", "ranking detail route is not declared"
    return parsed_route.resolve_detail_url(str(source.get("url", "")).strip(), values[0])


def _ranking_rank_source(source: Mapping[str, Any]) -> str:
    value = str(source.get("ranking_rank_source", "selector")).strip().casefold()
    return value if value in RANKING_RANK_SOURCES else ""


def _normalized_isbn(value: str) -> str:
    compact = re.sub(r"[-\s.]", "", _normalize_whitespace(value))
    if _ISBN_VALUE_RE.fullmatch(compact) is None:
        return ""
    if len(compact) == 10:
        total = sum(
            (10 if digit.casefold() == "x" else int(digit)) * (10 - index) for index, digit in enumerate(compact)
        )
        return compact.upper() if total % 11 == 0 else ""
    total = sum(int(digit) * (1 if index % 2 == 0 else 3) for index, digit in enumerate(compact))
    return compact if total % 10 == 0 else ""


def _isbn_values(value: str) -> list[str]:
    candidates = _ISBN_CANDIDATE_RE.findall(value)
    if not candidates:
        candidates = [value]
    return [_normalized_isbn(candidate) for candidate in candidates]


def ranking_entry_isbn(value: str) -> str:
    """Return the one valid ISBN inside an extracted ranking value, or "".

    The value is read the way page identifiers are, so a label such as
    ``ISBN 978…`` copied from the row does not lose the row (Issue #827).
    A value with no ISBN, several, or a bad checksum yields "".
    """
    values = _isbn_values(value)
    return values[0] if len(values) == 1 else ""


def _row_states_isbn(row_text: str, isbn: str) -> bool:
    """Return whether the row states *isbn*, copied or as one of its ISBNs.

    The contract asks for bare digits while a row may show the ISBN
    hyphenated, so a copied value is not the only form that grounds.
    """
    copied = normalize_visible_text(isbn)
    if copied and copied in row_text:
        return True
    wanted = ranking_entry_isbn(isbn)
    return bool(wanted) and wanted in {_normalized_isbn(value) for value in _ISBN_CANDIDATE_RE.findall(row_text)}


def _identifier_from_pattern(markup: str, pattern: str) -> tuple[str, str]:
    try:
        matches = list(re.finditer(pattern, markup, flags=re.IGNORECASE))
    except re.error as exc:
        return "", f"identifier pattern is malformed: {exc}"
    values: list[str] = []
    invalid = False
    for match in matches:
        if "identifier" in match.groupdict():
            candidate = match.group("identifier") or ""
        elif match.lastindex:
            candidate = match.group(1) or ""
        else:
            candidate = match.group(0)
        candidates = _isbn_values(candidate)
        if len(candidates) != 1 or not candidates[0]:
            invalid = True
        else:
            values.append(candidates[0])
    if invalid or len(values) != 1:
        return "", f"identifier pattern must match exactly one valid ISBN value in one row (matched {len(matches)})"
    return values[0], ""


def _identifier_from_values(values: list[str]) -> tuple[str, str]:
    normalized_values: list[str] = []
    invalid = False
    for value in values:
        candidates = _isbn_values(value)
        if len(candidates) != 1 or not candidates[0]:
            invalid = True
        else:
            normalized_values.append(candidates[0])
    valid = list(dict.fromkeys(normalized_values))
    if invalid or len(valid) != 1:
        return (
            "",
            f"identifier declaration must contain exactly one valid ISBN value in one row (matched {len(values)})",
        )
    return valid[0], ""


class _RankingRowCollector(HTMLParser):
    """Collect bounded rows and selected fields from visible HTML."""

    def __init__(self, source: dict[str, Any]) -> None:
        super().__init__(convert_charrefs=True)
        self.source = source
        self.row_classes = self._classes(source.get("ranking_row_selector", ""))
        self.field_classes = {
            field: self._classes(source.get(key, ""))
            for field, key in (
                ("rank", "ranking_rank_selector"),
                ("title", "ranking_title_selector"),
                ("author", "ranking_author_selector"),
                ("identifier", "ranking_identifier_selector"),
            )
        }
        self.identifier_attribute = str(source.get("ranking_identifier_attribute", "")).strip().casefold()
        self.identifier_pattern = str(source.get("ranking_identifier_pattern", "")).strip()
        self.detail_link_classes = self._classes(source.get("ranking_detail_link_selector", ""))
        self.visibility: list[_VisibilityFrame] = []
        self.elements: list[tuple[str, frozenset[str], bool]] = []
        self.rows: list[_RankingRowBuilder] = []
        self.active_row: _RankingRowBuilder | None = None
        self.active_field: tuple[str, _RankingRowBuilder, list[str], bool] | None = None
        self.detail_link_frames: list[tuple[int, list[str]]] = []
        self.row_depth = 0

    @staticmethod
    def _classes(value: object) -> tuple[str, ...]:
        if not isinstance(value, str) or not value.strip():
            return ()
        return tuple(part.strip()[1:] for part in value.split(">") if part.strip().startswith("."))

    def _matches(self, classes: frozenset[str], selector: tuple[str, ...]) -> bool:
        if not selector or selector[-1] not in classes:
            return False
        if len(selector) == 1:
            return True
        ancestors = [element[1] for element in self.elements[:-1]]
        if len(ancestors) < len(selector) - 1:
            return False
        return all(
            wanted in actual for wanted, actual in zip(selector[:-1], ancestors[-(len(selector) - 1) :], strict=True)
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        hidden = _push_visibility(self.visibility, tag, attrs)
        classes = frozenset(
            token for name, value in attrs if name.casefold() == "class" for token in (value or "").split()
        )
        parent_hidden = any(element[2] for element in self.elements)
        effective_hidden = hidden or parent_hidden
        self.elements.append((tag, classes, effective_hidden))
        if self.active_row is None and self._matches(classes, self.row_classes) and not effective_hidden:
            self.active_row = _RankingRowBuilder()
            self.rows.append(self.active_row)
            self.row_depth = len(self.elements)
            if self.identifier_attribute:
                for name, value in attrs:
                    if name.casefold() == self.identifier_attribute and value:
                        self.active_row.identifier_values.append(value)
        if self.active_row is None:
            return
        self.active_row.raw_parts.append(self.get_starttag_text() or f"<{tag}>")
        if self.identifier_attribute:
            for name, value in attrs:
                if name.casefold() == self.identifier_attribute and value:
                    self.active_row.identifier_values.append(value)
        if self.detail_link_classes and self._matches(classes, self.detail_link_classes) and not effective_hidden:
            values: list[str] = []
            self.active_row.detail_link_matches.append(values)
            self.detail_link_frames.append((len(self.elements), values))
            if tag == "a":
                href = next((value for name, value in attrs if name.casefold() == "href"), None)
                values.append(str(href or "").strip())
        elif self.detail_link_frames and tag == "a" and not effective_hidden:
            href = next((value for name, value in attrs if name.casefold() == "href"), None)
            for _depth, values in self.detail_link_frames:
                values.append(str(href or "").strip())
        for field_name, selector in self.field_classes.items():
            if self._matches(classes, selector):
                parts: list[str] = []
                if field_name == "identifier" and self.identifier_attribute:
                    value = next(
                        (value for name, value in attrs if name.casefold() == self.identifier_attribute),
                        None,
                    )
                    if value:
                        self.active_row.identifier_values.append(value)
                self.active_field = (field_name, self.active_row, parts, field_name == "identifier")
                break

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self.active_row is not None:
            self.active_row.raw_parts.append(f"</{tag}>")
        if self.active_field is not None and self.elements and self.elements[-1][0] == tag:
            field_name, row, parts, _allow_hidden = self.active_field
            value = _normalize_whitespace(" ".join(parts))
            if value:
                if field_name == "identifier":
                    row.identifier_values.append(value)
                else:
                    setattr(row, field_name, value)
            self.active_field = None
        if self.detail_link_frames:
            matching_depth = len(self.elements)
            if self.detail_link_frames[-1][0] == matching_depth and self.elements[-1][0] == tag:
                self.detail_link_frames.pop()
        closing_row = (
            self.active_row is not None and len(self.elements) == self.row_depth and self.elements[-1][0] == tag
        )
        if closing_row:
            self.active_row = None
            self.row_depth = 0
        _pop_visibility(self.visibility, tag)
        for index in range(len(self.elements) - 1, -1, -1):
            if self.elements[index][0] == tag:
                del self.elements[index:]
                break

    def handle_data(self, data: str) -> None:
        if self.active_row is None or not data.strip():
            return
        self.active_row.raw_parts.append(data)
        hidden = bool(self.visibility and self.visibility[-1].hidden)
        if not hidden:
            self.active_row.text_parts.append(data)
        if self.active_field is not None:
            field_name, row, parts, allow_hidden = self.active_field
            if not hidden or allow_hidden:
                parts.append(data)

    def close(self) -> None:
        if self.active_row is not None:
            self.active_row = None
        super().close()


def ranking_rows(
    html_content: str, source: dict[str, Any], *, detail_route: RankingDetailRoute | None = None
) -> list[RankingRow]:
    """Return rows using only explicitly declared ranking-row selectors."""
    collector = _RankingRowCollector(source)
    collector.feed(html_content)
    collector.close()
    route = detail_route
    if route is None and ranking_detail_route_declared(source):
        route = RankingDetailRoute.from_source(source)
    rows: list[RankingRow] = []
    rank_source = _ranking_rank_source(source)
    for row_index, row in enumerate(collector.rows, start=1):
        identifier = ""
        identifier_error = ""
        if row.identifier:
            identifier, identifier_error = _identifier_from_values([row.identifier])
        elif row.identifier_values:
            identifier, identifier_error = _identifier_from_values(row.identifier_values)
        elif collector.identifier_pattern:
            identifier, identifier_error = _identifier_from_pattern(
                "".join(row.raw_parts), collector.identifier_pattern
            )
        rows.append(
            RankingRow(
                rank=(str(row_index) if rank_source == "document_order" else _normalized_selector_rank(row.rank)),
                title=row.title,
                author=row.author or verified_author(source, identifier, row.title),
                identifier=identifier,
                text=_normalize_whitespace(" ".join(row.text_parts)),
                identifier_error=identifier_error,
                detail_url=(
                    _ranking_detail_url(
                        source,
                        row.detail_link_matches[0] if len(row.detail_link_matches) == 1 else [],
                        route,
                    )[0]
                    if route is not None and len(row.detail_link_matches) == 1
                    else ""
                ),
                detail_link_error=(
                    (
                        "ranking row detail link selector matched more than one element"
                        if len(row.detail_link_matches) > 1
                        else _ranking_detail_url(
                            source, row.detail_link_matches[0] if row.detail_link_matches else [], route
                        )[1]
                    )
                    if route is not None
                    else ""
                ),
            )
        )
    return rows


_RANKING_DIAGNOSTIC_MAX_CHARS = 160
_RANKING_DIAGNOSTIC_MAX_TOKENS = 24
_RANKING_DIAGNOSTIC_MAX_ELEMENTS = 12
_RANKING_DIAGNOSTIC_MAX_ELEMENT_TEXT_CHARS = 72
_RANKING_DIAGNOSTIC_SENSITIVE_CLASS_RE = re.compile(
    r"(?:isbn|ean|identifier|barcode|sku|sales?|units?|price|pre[cç]o|vendas?)", flags=re.IGNORECASE
)
_RANKING_DIAGNOSTIC_SENSITIVE_TEXT_RE = re.compile(
    r"(?:isbn|ean|barcode|sku|sales?|units?|price|pre[cç]o|vendas?|r\\$|€)", flags=re.IGNORECASE
)


def _diagnostic_class_tokens(value: frozenset[str]) -> list[str]:
    tokens = [
        token[:48]
        for token in sorted(value)
        if _VISIBLE_TITLE_CLASS_TOKEN_RE.fullmatch(token)
        and _RANKING_DIAGNOSTIC_SENSITIVE_CLASS_RE.search(token) is None
    ]
    return tokens[:8]


def _diagnostic_text(value: str, *, allow_rank: bool) -> str:
    text = _normalize_whitespace(value)
    if not text or _RANKING_DIAGNOSTIC_SENSITIVE_TEXT_RE.search(text):
        return ""
    words = text.split()
    if len(words) > 10:
        return ""
    if any(re.search(r"\d", word) for word in words):
        if not allow_rank or re.fullmatch(r"(?:#|№)?\d{1,3}[.)]?", text) is None:
            return ""
    if any("<" in word or ">" in word for word in words):
        return ""
    return text[:_RANKING_DIAGNOSTIC_MAX_ELEMENT_TEXT_CHARS]


class _RankingDiagnosticCollector(HTMLParser):
    """Collect only bounded visible structure from one declared row."""

    def __init__(self, source: dict[str, Any]) -> None:
        super().__init__(convert_charrefs=True)
        self.row_classes = _RankingRowCollector._classes(source.get("ranking_row_selector", ""))
        self.frames: list[_RankingDiagnosticFrame] = []
        self._visibility: list[_VisibilityFrame] = []
        self.row_start: int | None = None
        self.row_elements: list[_RankingDiagnosticFrame] = []
        self.rows: list[dict[str, object]] = []

    def _matches(self, class_tokens: frozenset[str]) -> bool:
        if not self.row_classes or self.row_classes[-1] not in class_tokens:
            return False
        preceding = self.row_classes[:-1]
        if not preceding:
            return True
        ancestors = self.frames[:-1]
        if len(ancestors) < len(preceding):
            return False
        return all(
            wanted in frame.class_tokens for wanted, frame in zip(preceding, ancestors[-len(preceding) :], strict=True)
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        hidden = _push_visibility(self._visibility, tag, attrs)
        classes = frozenset(
            token for name, value in attrs if name.casefold() == "class" for token in (value or "").split()
        )
        frame = _RankingDiagnosticFrame(tag=tag, class_tokens=classes, hidden=hidden)
        self.frames.append(frame)
        if self.row_start is None and not hidden and self._matches(classes):
            self.row_start = len(self.frames) - 1
            self.row_elements = [frame]
        elif self.row_start is not None:
            self.row_elements.append(frame)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        matching_index = next(
            (index for index in range(len(self.frames) - 1, -1, -1) if self.frames[index].tag == tag), None
        )
        if matching_index is None:
            return
        _pop_visibility(self._visibility, tag)
        if self.row_start == matching_index:
            row_frames = self.row_elements
            visible_elements: list[dict[str, object]] = []
            visible_class_tokens: list[str] = []
            text_values: list[str] = []
            text_budget = _RANKING_DIAGNOSTIC_MAX_CHARS
            for frame in row_frames:
                if frame.hidden:
                    continue
                classes = _diagnostic_class_tokens(frame.class_tokens)
                direct = _diagnostic_text(" ".join(frame.direct_parts), allow_rank=not text_values)
                if direct and text_budget:
                    direct = direct[:text_budget]
                    text_budget -= len(direct)
                    text_values.append(direct)
                else:
                    direct = ""
                remaining_tokens = _RANKING_DIAGNOSTIC_MAX_TOKENS - len(visible_class_tokens)
                if classes and remaining_tokens > 0 and len(visible_elements) < _RANKING_DIAGNOSTIC_MAX_ELEMENTS:
                    bounded_classes = classes[:remaining_tokens]
                    visible_class_tokens.extend(bounded_classes)
                    visible_elements.append({"tag": frame.tag, "class_tokens": bounded_classes, "visible_text": direct})
            row_text = " ".join(text_values)[:_RANKING_DIAGNOSTIC_MAX_CHARS]
            self.rows.append(
                {
                    "visible_text": row_text,
                    "class_tokens": visible_class_tokens[:_RANKING_DIAGNOSTIC_MAX_TOKENS],
                    "visible_elements": visible_elements,
                }
            )
            self.row_start = None
            self.row_elements = []
        del self.frames[matching_index:]

    def handle_data(self, data: str) -> None:
        if self.row_start is None or not data.strip() or _visibility_is_hidden(self._visibility):
            return
        if self.frames and not self.frames[-1].hidden:
            self.frames[-1].direct_parts.append(data)

    def close(self) -> None:
        # The diagnostic is intentionally best effort for malformed HTML.
        while self.frames:
            self.handle_endtag(self.frames[-1].tag)
        super().close()


def ranking_row_diagnostics(html_content: str, source: Mapping[str, Any], row_index: int) -> dict[str, object] | None:
    """Return safe bounded visible structure for one malformed ranking row."""
    collector = _RankingDiagnosticCollector(dict(source))
    collector.feed(html_content)
    collector.close()
    if row_index < 1 or row_index > len(collector.rows):
        return None
    diagnostic = dict(collector.rows[row_index - 1])
    diagnostic["row_index"] = row_index
    return diagnostic


@dataclass
class _StructuralDiagnosticFrame:
    tag: str
    class_tokens: tuple[str, ...]
    hidden: bool
    child_counts: dict[tuple[str, tuple[str, ...]], int] = field(default_factory=dict)


class _StructuralDiagnosticCollector(HTMLParser):
    """Count repeated direct visible child signatures without retaining text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.visibility: list[_VisibilityFrame] = []
        self.frames: list[_StructuralDiagnosticFrame] = []
        self.repeated: dict[tuple[tuple[str, tuple[str, ...]], tuple[str, tuple[str, ...]]], int] = {}

    @staticmethod
    def _class_tokens(attrs: list[tuple[str, str | None]]) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    token
                    for name, value in attrs
                    if name.casefold() == "class"
                    for token in (value or "").split()
                    if token
                }
            )
        )

    def _record(self, frame: _StructuralDiagnosticFrame) -> None:
        parent = (frame.tag, frame.class_tokens)
        for child, count in frame.child_counts.items():
            if count < 2:
                continue
            key = (parent, child)
            self.repeated[key] = min(1000, self.repeated.get(key, 0) + count)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        hidden = _push_visibility(self.visibility, tag, attrs)
        if self.frames and not hidden and not self.frames[-1].hidden:
            child = (tag, self._class_tokens(attrs))
            parent = self.frames[-1]
            parent.child_counts[child] = min(1000, parent.child_counts.get(child, 0) + 1)
        if tag not in _VOID_TAGS:
            self.frames.append(_StructuralDiagnosticFrame(tag, self._class_tokens(attrs), hidden))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        _pop_visibility(self.visibility, tag)
        if tag in _VOID_TAGS:
            return
        for index in range(len(self.frames) - 1, -1, -1):
            if self.frames[index].tag == tag:
                for frame in self.frames[index:]:
                    self._record(frame)
                del self.frames[index:]
                return

    def close(self) -> None:
        while self.frames:
            self._record(self.frames.pop())
        super().close()


def repeated_visible_sibling_diagnostics(html_content: str) -> dict[str, object]:
    """Return repeated direct-child structure from visible HTML only."""
    collector = _StructuralDiagnosticCollector()
    collector.feed(html_content)
    collector.close()
    return {
        "repeated_siblings": [
            {
                "parent": {"tag": parent[0], "class_tokens": list(parent[1])},
                "child": {"tag": child[0], "class_tokens": list(child[1])},
                "count": count,
            }
            for (parent, child), count in collector.repeated.items()
        ]
    }


_RANKING_DECLARATION_RANGE_RE = re.compile(
    r"(?<!\d)(?P<start>\d{4}(?P<year_sep>[./-])\d{1,2}(?P=year_sep)\d{1,2}"
    r"|\d{1,2}(?P<day_sep>[./-])\d{1,2}(?P=day_sep)\d{4})"
    r"(?P<separator>\s*(?:-|–|—|~|〜|至|to|a)\s*)"
    r"(?P<end>\d{4}(?P<end_year_sep>[./-])\d{1,2}(?P=end_year_sep)\d{1,2}"
    r"|\d{1,2}(?P<end_day_sep>[./-])\d{1,2}(?P=end_day_sep)\d{4})(?!\d)",
    flags=re.IGNORECASE,
)
_RANKING_DECLARATION_WEEK_RE = re.compile(
    r"(?<!\d)\d{4}\s*(?:년\s*\d{1,2}\s*월\s*(?:第\s*)?\d{1,2}\s*(?:주(?:차)?|週)|"
    r"年\s*\d{1,2}\s*月\s*(?:第\s*)?\d{1,2}\s*週)(?!\d)",
    flags=re.IGNORECASE,
)
_RANKING_DECLARATION_IDENTIFIER_ATTRIBUTES = frozenset(
    {
        "itemid",
        "item-id",
        "data-item-id",
        "data-itemid",
        "product-id",
        "productid",
        "data-product-id",
        "data-productid",
        "book-id",
        "bookid",
        "data-book-id",
        "data-bookid",
        "goodsno",
        "goods-no",
        "data-goods-no",
        "data-goodsno",
        "isbn",
        "data-isbn",
    }
)
_RANKING_DECLARATION_CLASS_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_RANKING_DECLARATION_UNSAFE_TAGS = frozenset(
    {"nav", "header", "footer", "aside", "article", "blockquote", "figure", "figcaption"}
)
_RANKING_DECLARATION_EXCLUDED_RE = re.compile(
    r"(?:title|author|description|blurb|review|prose|article|body|nav|header|footer|aside)", flags=re.IGNORECASE
)
_RANKING_DECLARATION_CONTEXT_CLASS_RE = re.compile(
    r"(?:^|[-_])(?:row|item|candidate|entry|card)(?:$|[-_])|^(?:book|product)$", flags=re.IGNORECASE
)
_RANKING_DECLARATION_PAGE_HEADING_CLASS_RE = re.compile(
    r"(?:^|[-_])(?:page[-_]?(?:title|heading)|heading(?:[-_]title)?)(?:$|[-_])", flags=re.IGNORECASE
)


def _ranking_declaration_page_heading_class_is_safe(class_tokens: Sequence[str]) -> bool:
    return any(_RANKING_DECLARATION_PAGE_HEADING_CLASS_RE.search(token) for token in class_tokens)


def ranking_declaration_location_is_safe(tag: str, class_tokens: Sequence[str]) -> bool:
    """Return the conservative direct-element privacy policy for diagnostics."""
    excluded_class = any(_RANKING_DECLARATION_EXCLUDED_RE.search(token) for token in class_tokens)
    page_heading_exception = tag in _HEADING_TAGS and _ranking_declaration_page_heading_class_is_safe(class_tokens)
    return (
        tag == tag.casefold()
        and tag not in _RANKING_DECLARATION_UNSAFE_TAGS
        and tag not in {"html", "body", "script", "style", "template", "noscript"}
        and (not excluded_class or page_heading_exception)
    )


def _ranking_declaration_intrinsic_unsafe(tag: str, class_tokens: Sequence[str]) -> bool:
    if tag in _RANKING_DECLARATION_UNSAFE_TAGS or tag in {"script", "style", "template", "noscript"}:
        return True
    if any(_RANKING_DECLARATION_CONTEXT_CLASS_RE.search(token) for token in class_tokens):
        return True
    return _ranking_declaration_intrinsic_identifier_unsafe(tag, class_tokens)


def _ranking_declaration_intrinsic_identifier_unsafe(tag: str, class_tokens: Sequence[str]) -> bool:
    if tag in _RANKING_DECLARATION_UNSAFE_TAGS or tag in {"script", "style", "template", "noscript"}:
        return True
    if not any(_RANKING_DECLARATION_EXCLUDED_RE.search(token) for token in class_tokens):
        return False
    return not (tag in _HEADING_TAGS and _ranking_declaration_page_heading_class_is_safe(class_tokens))


@dataclass
class _RankingDeclarationElement:
    tag: str
    class_tokens: list[str]
    attrs: list[tuple[str, str | None]]
    hidden: bool
    parent: int | None
    unsafe_context: bool
    identifier_unsafe_context: bool
    direct_parts: list[str] = field(default_factory=list)
    heading_parts: list[str] = field(default_factory=list)


class _RankingDeclarationCollector(HTMLParser):
    """Collect visible element text and attributes without retaining values."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.visibility: list[_VisibilityFrame] = []
        self.elements: list[_RankingDeclarationElement] = []
        self.open_elements: list[int] = []
        self.open_headings: list[int] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        hidden = _push_visibility(self.visibility, tag, attrs)
        parent = self.open_elements[-1] if self.open_elements else None
        class_tokens: list[str] = []
        for name, value in attrs:
            if name.casefold() != "class":
                continue
            for token in (value or "").split():
                if len(token) <= 48 and _RANKING_DECLARATION_CLASS_RE.fullmatch(token) and token not in class_tokens:
                    class_tokens.append(token)
        element = _RankingDeclarationElement(
            tag=tag,
            class_tokens=class_tokens,
            attrs=list(attrs),
            hidden=hidden,
            parent=parent,
            unsafe_context=(
                (self.elements[parent].unsafe_context if parent is not None else False)
                or _ranking_declaration_intrinsic_unsafe(tag, class_tokens)
            ),
            identifier_unsafe_context=(
                (self.elements[parent].identifier_unsafe_context if parent is not None else False)
                or _ranking_declaration_intrinsic_identifier_unsafe(tag, class_tokens)
            ),
        )
        index = len(self.elements)
        self.elements.append(element)
        if tag not in _VOID_TAGS:
            self.open_elements.append(index)
            if tag in _HEADING_TAGS and not hidden:
                self.open_headings.append(index)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        _pop_visibility(self.visibility, tag)
        for position in range(len(self.open_elements) - 1, -1, -1):
            if self.elements[self.open_elements[position]].tag == tag:
                del self.open_elements[position:]
                break
        for position in range(len(self.open_headings) - 1, -1, -1):
            if self.elements[self.open_headings[position]].tag == tag:
                del self.open_headings[position:]
                break

    def handle_data(self, data: str) -> None:
        if not data.strip() or _visibility_is_hidden(self.visibility):
            return
        current_element_is_unsafe = bool(self.open_elements and self.elements[self.open_elements[-1]].unsafe_context)
        if self.open_elements:
            self.elements[self.open_elements[-1]].direct_parts.append(data)
        if not current_element_is_unsafe:
            for index in self.open_headings:
                self.elements[index].heading_parts.append(data)

    def close(self) -> None:
        self.open_elements.clear()
        self.open_headings.clear()
        super().close()


def _ranking_declaration_text(element: _RankingDeclarationElement) -> str:
    parts = element.heading_parts if element.tag in _HEADING_TAGS else element.direct_parts
    return _normalize_whitespace(" ".join(parts))


def _ranking_declaration_direct_text(element: _RankingDeclarationElement) -> str:
    return _normalize_whitespace(" ".join(element.direct_parts))


def _ranking_declaration_location_is_safe(elements: list[_RankingDeclarationElement], index: int) -> bool:
    element = elements[index]
    return (
        not element.hidden
        and not element.unsafe_context
        and ranking_declaration_location_is_safe(element.tag, element.class_tokens)
    )


def _ranking_declaration_identifier_location_is_safe(elements: list[_RankingDeclarationElement], index: int) -> bool:
    element = elements[index]
    return (
        not element.hidden
        and not element.identifier_unsafe_context
        and ranking_declaration_location_is_safe(element.tag, element.class_tokens)
    )


def _ranking_declaration_date_formats(start: str, end: str) -> list[str] | None:
    def numeric_parts(value: str) -> tuple[str, str] | None:
        year_first = re.fullmatch(r"\d{4}(?P<separator>[./-])\d{1,2}(?P=separator)\d{1,2}", value)
        if year_first is not None:
            return "ymd", year_first.group("separator")
        day_first = re.fullmatch(r"\d{1,2}(?P<separator>[./-])\d{1,2}(?P=separator)\d{4}", value)
        if day_first is not None:
            return "dmy-or-mdy", day_first.group("separator")
        return None

    start_shape = numeric_parts(start)
    end_shape = numeric_parts(end)
    if start_shape is None or end_shape is None or start_shape != end_shape:
        return None
    layout, separator = start_shape
    if separator == "-":
        if layout == "ymd":
            formats = ["%Y-%m-%d"]
        else:
            formats = ["%d-%m-%Y", "%m-%d-%Y"]
    elif separator == "/":
        if layout == "ymd":
            formats = ["%Y/%m/%d"]
        else:
            formats = ["%d/%m/%Y", "%m/%d/%Y"]
    else:
        if layout == "ymd":
            formats = ["%Y.%m.%d"]
        else:
            formats = ["%d.%m.%Y", "%m.%d.%Y"]

    valid_formats: list[str] = []
    for candidate in formats:
        try:
            start_date = datetime.strptime(start, candidate).date()
            end_date = datetime.strptime(end, candidate).date()
        except ValueError:
            continue
        if start_date <= end_date:
            valid_formats.append(candidate)
    return valid_formats[:2] or None


def _ranking_declaration_pattern(start: str, end: str, range_separator: str) -> str:
    def date_pattern(value: str) -> str:
        year_first = re.fullmatch(r"\d{4}(?P<separator>[./-])\d{1,2}(?P=separator)\d{1,2}", value)
        if year_first is not None:
            escaped = {"-": "-", "/": "/", ".": r"\."}[year_first.group("separator")]
            return rf"\d{{4}}{escaped}\d{{1,2}}{escaped}\d{{1,2}}"
        day_first = re.fullmatch(r"\d{1,2}(?P<separator>[./-])\d{1,2}(?P=separator)\d{4}", value)
        if day_first is None:
            raise ValueError("ranking declaration period has an unsupported date shape")
        escaped = {"-": "-", "/": "/", ".": r"\."}[day_first.group("separator")]
        return rf"\d{{1,2}}{escaped}\d{{1,2}}{escaped}\d{{4}}"

    if range_separator == "-":
        range_pattern = r"\s*-\s*"
    elif range_separator == "to":
        range_pattern = r"\s*to\s*"
    else:
        range_pattern = rf"\s*{re.escape(range_separator)}\s*"
    return f"(?P<period>{date_pattern(start)}{range_pattern}{date_pattern(end)})"


def _ranking_declaration_period_candidate(text: str) -> dict[str, object] | None:
    match = _RANKING_DECLARATION_RANGE_RE.search(text)
    if match is None:
        return None
    start = match.group("start")
    end = match.group("end")
    formats_and_separators = _ranking_declaration_date_formats(start, end)
    if formats_and_separators is None:
        return None
    formats = formats_and_separators
    matched_text = _normalize_whitespace(match.group(0))
    if len(matched_text) > 96:
        return None
    range_separator = match.group("separator").strip() or "-"
    pattern = _ranking_declaration_pattern(start, end, range_separator)
    if len(pattern) > 96:
        return None
    return {"text": matched_text, "pattern": pattern, "date_formats": formats}


def _ranking_declaration_value_shape(value: str) -> str:
    normalized = re.sub(r"[-_\s]", "", value)
    if re.fullmatch(r"\d+", normalized):
        return f"digits:{len(normalized)}" if 1 <= len(normalized) <= 64 else ""
    if re.fullmatch(r"[A-Za-z0-9]+", normalized) and any(char.isdigit() for char in normalized):
        return f"alphanumeric:{len(normalized)}" if 1 <= len(normalized) <= 64 else ""
    return ""


def ranking_declaration_diagnostics(html_content: str) -> dict[str, object]:
    """Extract bounded, source-independent ranking declaration suggestions."""
    collector = _RankingDeclarationCollector()
    collector.feed(html_content)
    collector.close()
    elements = collector.elements
    heading_indices = [
        index
        for index, element in enumerate(elements)
        if element.tag in _HEADING_TAGS and _ranking_declaration_location_is_safe(elements, index)
    ]
    other_indices = [
        index
        for index, element in enumerate(elements)
        if index not in heading_indices and _ranking_declaration_location_is_safe(elements, index)
    ]
    period_candidates: list[dict[str, object]] = []
    for index in [*heading_indices, *other_indices]:
        period_candidate = _ranking_declaration_period_candidate(_ranking_declaration_text(elements[index]))
        if period_candidate is not None and period_candidate not in period_candidates:
            period_candidates.append(period_candidate)
        if len(period_candidates) >= 4:
            break
    if period_candidates:
        reporting_period: dict[str, object] = {
            "status": "calendar_date_range",
            "candidates": period_candidates[:4],
        }
    else:
        week_found = any(
            _RANKING_DECLARATION_WEEK_RE.search(_ranking_declaration_text(elements[index])) for index in heading_indices
        )
        if not week_found:
            week_found = any(
                _RANKING_DECLARATION_WEEK_RE.search(_ranking_declaration_text(elements[index]))
                for index in other_indices
            )
        reporting_period = {"status": "week_ordinal" if week_found else "not_found", "candidates": []}

    identifier_candidates: list[dict[str, object]] = []
    seen_identifiers: set[tuple[object, ...]] = set()
    for index, element in enumerate(elements):
        if not _ranking_declaration_identifier_location_is_safe(elements, index):
            continue
        text = _ranking_declaration_direct_text(element)
        for value in _ISBN_CANDIDATE_RE.findall(text):
            normalized = _normalized_isbn(value)
            if not normalized:
                continue
            class_tokens = element.class_tokens[:4]
            candidate: dict[str, object] = {
                "kind": "isbn",
                "source": "text",
                "tag": element.tag,
                "class_tokens": class_tokens,
                "attribute": "",
                "value_shape": f"isbn-{len(normalized)}",
            }
            key: tuple[object, ...] = (
                candidate["kind"],
                candidate["source"],
                candidate["tag"],
                tuple(class_tokens),
                candidate["attribute"],
                candidate["value_shape"],
            )
            if key not in seen_identifiers:
                seen_identifiers.add(key)
                identifier_candidates.append(candidate)
                if len(identifier_candidates) >= 8:
                    break
        if len(identifier_candidates) >= 8:
            break
        for name, value in element.attrs:
            attribute = name.casefold().replace("_", "-")
            if attribute not in _RANKING_DECLARATION_IDENTIFIER_ATTRIBUTES or not value:
                continue
            # An ISBN on an attribute is reported as an ISBN, not skipped. The
            # allowlist carries isbn and data-isbn precisely because a retailer
            # commonly puts the identifier there and nowhere in the row text,
            # and skipping it told the operator that the row had no identifier
            # at all, which is the wrong answer this issue exists to remove.
            normalized_isbn = _normalized_isbn(value)
            if normalized_isbn:
                kind = "isbn"
                shape = f"isbn-{len(normalized_isbn)}"
            else:
                kind = "internal_item_id"
                shape = _ranking_declaration_value_shape(value)
            if not shape:
                continue
            class_tokens = element.class_tokens[:4]
            candidate = {
                "kind": kind,
                "source": "attribute",
                "tag": element.tag,
                "class_tokens": class_tokens,
                "attribute": attribute,
                "value_shape": shape,
            }
            key = (
                candidate["kind"],
                candidate["source"],
                candidate["tag"],
                tuple(class_tokens),
                candidate["attribute"],
                candidate["value_shape"],
            )
            if key not in seen_identifiers:
                seen_identifiers.add(key)
                identifier_candidates.append(candidate)
                if len(identifier_candidates) >= 8:
                    break
        if len(identifier_candidates) >= 8:
            break
    return {"reporting_period": reporting_period, "identifier_candidates": identifier_candidates[:8]}


@dataclass
class _AnchorText:
    """Join rendered anchor text and retain whole block-delimited segments."""

    parts: list[str] = field(default_factory=list)
    segments: list[str] = field(default_factory=list)
    segment_tags: list[str] = field(default_factory=list)
    block_stack: list[str] = field(default_factory=list)

    def add(self, data: str) -> None:
        self.parts.append(data)

    def boundary(self) -> None:
        segment = _normalize_whitespace("".join(self.parts))
        if segment:
            self.segments.append(segment)
            self.segment_tags.append(self.block_stack[-1] if self.block_stack else "")
        self.parts.clear()

    def start_block(self, tag: str) -> None:
        self.boundary()
        if tag not in _VOID_TAGS:
            self.block_stack.append(tag)

    def end_block(self, tag: str) -> None:
        self.boundary()
        for index in range(len(self.block_stack) - 1, -1, -1):
            if self.block_stack[index] == tag:
                del self.block_stack[index:]
                break

    def finish(self) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
        self.boundary()
        return " ".join(self.segments), tuple(self.segments), tuple(self.segment_tags)


class _LinkAnchorCollector(HTMLParser):
    """Collect (href, visible anchor text) pairs from <a> elements."""

    def __init__(self, source_page: str) -> None:
        super().__init__(convert_charrefs=True)
        self._source_page = source_page
        self._visibility_frames: list[_VisibilityFrame] = []
        self._in_a = False
        self._a_href: str | None = None
        self._a_text = _AnchorText()
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if _push_visibility(self._visibility_frames, tag, attrs):
            return
        if tag == "a":
            self._in_a = True
            self._a_text = _AnchorText()
            href = ""
            for attr, value in attrs:
                if attr.casefold() == "href" and isinstance(value, str):
                    href = value.strip()
                    break
            if href:
                self._a_href = urljoin(self._source_page, href)
            else:
                self._a_href = None
        elif self._in_a and tag in _ANCHOR_BLOCK_BOUNDARY_TAGS:
            self._a_text.start_block(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        closing_hidden = _pop_visibility(self._visibility_frames, tag)
        if closing_hidden is None or closing_hidden:
            return
        if self._in_a and tag in _ANCHOR_BLOCK_BOUNDARY_TAGS:
            self._a_text.end_block(tag)
        if tag == "a" and self._in_a:
            self._in_a = False
            if self._a_href:
                text, _segments, _segment_tags = self._a_text.finish()
                if text:
                    self.links.append((self._a_href, text))
            self._a_href = None
            self._a_text = _AnchorText()

    def handle_data(self, data: str) -> None:
        if _visibility_is_hidden(self._visibility_frames):
            return
        if self._in_a and self._a_href:
            self._a_text.add(data)


def link_anchor_pairs(html_content: str, source_page: str = "") -> list[tuple[str, str]]:
    """Return (href, visible anchor text) pairs from all server-rendered <a> elements.

    Skips elements hidden by the HTML ``hidden`` attribute, inline
    ``display:none`` or ``visibility:hidden`` styles, ``aria-hidden="true"``
    ancestors, and non-rendered ``script``, ``style``, ``template``, and
    ``noscript`` blocks. Resolves relative hrefs against *source_page*."""
    collector = _LinkAnchorCollector(source_page)
    collector.feed(html_content)
    collector.close()
    return collector.links


@dataclass(frozen=True)
class SourceListEntry:
    """One server-rendered linked entry with its bounded context (Issue #334).

    *block_tag* is the nearest entry-block tag that encloses the anchor at
    finalization time (one of ``li``, ``article``, ``section``, ``tr``), or a
    declared entry-scope container, or an empty string for flat anchors that
    sit in no entry block at all. The
    policy layer decides which block tags count as actual per-entry
    containers; a page-level wrapper such as ``section`` stays visible as
    metadata so the probe can reject it as unbounded when it wraps several
    direct anchors instead of one entry each.

    *in_nav*, *in_header*, and *in_footer* are True when the anchor sits
    inside the corresponding landmark (a ``nav`` element or
    ``role="navigation"`` for *in_nav*, a ``header`` element or
    ``role="banner"`` for *in_header*, a ``footer`` element or
    ``role="contentinfo"`` for *in_footer*); the policy layer ignores such
    site-chrome anchors when it looks for qualifying book entries (Issue
    #334 review).

    *block_id* identifies the nearest entry-block element so anchors that
    belong to the same bounded entry container can be grouped (flat anchors
    carry -1). *link_kind* is the typed anchor classification (one of
    ``book``, ``action``, ``author``, ``navigation``): action/commerce
    labels, explicit author links, and author-page URL links are never
    primary book-level links, and a group that contains only them is not an
    accepted book entry.

    *author_elements* carries the visible texts of author-marked non-anchor
    elements (``rel="author"``/``itemprop="author"``/``data-author``/author
    class tokens) inside the same bounded container. *author_evidence* carries
    explicit author elements and explicit author links in document order.
    *marker_evidence* carries visible text runs outside anchors that begin
    with a known author marker. *inferred_author_evidence* carries only
    URL-inferred author labels that also have deterministic name overlap with
    their URL identity slug. The policy can therefore prefer explicit
    evidence without merging URL inference into the same trust level (Issue
    #334 hardening)."""

    url: str
    title: str
    context: str
    block_tag: str
    in_nav: bool = False
    in_header: bool = False
    in_footer: bool = False
    block_id: int = -1
    link_kind: str = LINK_KIND_BOOK
    author_elements: tuple[str, ...] = ()
    author_evidence: tuple[str, ...] = ()
    marker_evidence: tuple[str, ...] = ()
    inferred_author_evidence: tuple[str, ...] = ()


def server_rendered_list_entries(
    html_content: str, source_page: str, entry_scope_selector: str = ""
) -> list[SourceListEntry]:
    """Return every server-rendered anchor with visible text as a list entry.

    Source-independent parser seam for feasibility probing of unconfigured
    Source Candidates (Issue #334). Each entry carries the resolved URL, the
    normalized visible anchor text, the bounded visible-text context of its
    nearest ``li``/``article``/``section``/``tr`` container (for flat anchors
    a conservative visible-text neighbourhood), the tag of that nearest
    container as *block_tag* (empty for flat anchors), landmark flags
    (*in_nav*/*in_header*/*in_footer*), the entry-block identity
    (*block_id*), and the typed *link_kind* (``book``/``action``/
    ``author``/``navigation``, where ``author`` also covers unmarked
    author-page URLs). Anchors inside ``script``, ``style``,
    ``template``, and ``noscript`` blocks are ignored,
    so client-rendered content yields no entries.

    The block tag is recorded when the entry is finalized, so a nested anchor
    reports the innermost enclosing entry block (for example ``li`` inside a
    wrapping ``section``), or the declared row when scoped parsing is active.
    This keeps the metadata aligned with how the bounded entry context itself
    is cut out of enclosing blocks.

    The bounded-context rules are shared with the configured-source collectors
    (``audible_chart_entries`` and friends) so a probe verdict and the later
    fixture grounding contract parse the same markup the same way."""
    scope_result = parse_entry_scope(html_content, entry_scope_selector) if entry_scope_selector else None
    if scope_result is not None and scope_result.failure_cause is not None:
        return []
    collector = _EntryBlockCollector(source_page, entry_scope_selector=entry_scope_selector)
    collector.feed(html_content)
    collector.close()
    return [
        SourceListEntry(
            url=record.href,
            title=record.anchor_text,
            context=record.context,
            block_tag=record.block_tag,
            in_nav=record.in_nav,
            in_header=record.in_header,
            in_footer=record.in_footer,
            block_id=record.block_id,
            link_kind=record.link_kind,
            author_elements=record.author_elements,
            author_evidence=record.author_evidence,
            marker_evidence=record.marker_evidence,
            inferred_author_evidence=record.inferred_author_evidence,
        )
        for record in collector.records
    ]


def server_rendered_entry_rows(html_content: str, entry_scope_selector: str) -> list[str]:
    """Return visible text for each declared direct-child entry container."""
    scope_result = parse_entry_scope(html_content, entry_scope_selector)
    if scope_result.failure_cause is not None:
        return []
    collector = _EntryBlockCollector(source_page="", entry_scope_selector=entry_scope_selector)
    collector.feed(html_content)
    collector.close()
    return list(collector.declared_rows)


@dataclass
class _EntryBlockRecord:
    """One collected anchor with its bounded context and block metadata.

    The single internal record type of :class:`_EntryBlockCollector`: it
    carries the entry data (href, anchor text, resolved bounded context), the
    document-order start index, the nearest entry-block tag, the landmark
    flags (*in_nav*/*in_header*/*in_footer*), the entry-block identity
    (*block_id*), the typed *link_kind*, and the grounded author texts inside
    the same block (*author_elements*/*author_evidence*), plus marker and
    URL-inferred evidence kept at separate trust levels.
    Records replace the former parallel
    ``entries``/``_entry_order``/``_entry_tags`` bookkeeping; the legacy
    tuple list is derived from the sorted records at close time."""

    href: str
    anchor_text: str
    anchor_segments: tuple[str, ...]
    anchor_segment_tags: tuple[str, ...]
    start_index: int
    depth: int
    block_start: int | None
    block_tag: str
    in_nav: bool
    in_header: bool
    in_footer: bool
    block_id: int
    link_kind: str
    context: str = ""
    author_elements: tuple[str, ...] = ()
    author_evidence: tuple[str, ...] = ()
    marker_evidence: tuple[str, ...] = ()
    inferred_author_evidence: tuple[str, ...] = ()


class _EntryBlockCollector(HTMLParser):
    """Collect bounded entry-block text for each <a> element.

    For structured list markup (``<li>``, ``<article>``), or a configured
    declared entry row, the nearest containing block's visible text is the
    entry context. For flat
    article/prose markup without a container, a conservative visible-text
    neighbourhood (surrounding tokens around the anchor) is used instead.

    Each collected anchor becomes one :class:`_EntryBlockRecord` carrying the
    anchor data, its document-order index, its nearest entry-block tag and
    identity, the landmark flags (in_nav/in_header/in_footer), and the typed
    link kind. Records are sorted once in document order at close time and
    the legacy ``entries`` tuple list is derived from them for the
    configured-source consumers."""

    ENTRY_BLOCKS: ClassVar[set[str]] = {"li", "article", "section", "tr"}
    MAX_WINDOW: ClassVar[int] = 500

    def __init__(self, source_page: str, *, entry_scope_selector: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self._source_page = source_page
        self._entry_scope_classes = (
            _entry_scope_selector_classes(entry_scope_selector) if entry_scope_selector else None
        )
        self._scope_frames: list[_EntryScopeFrame] = []
        self._visibility_frames: list[_VisibilityFrame] = []
        self._in_a = False
        self._a_href: str | None = None
        self._a_text = _AnchorText()
        self._a_start_index: int | None = None
        # Stack of visible text for enclosing entry blocks.
        self._block_stack: list[str] = []
        # Start index (in _all_visible) of each open entry block.
        self._block_starts: list[int] = []
        # Per open entry block, the visible-token ranges of nested entry blocks
        # that hold a product anchor of their own. Those ranges belong to the
        # nested product, not to this one, so they are cut out of this block's
        # context.
        self._block_excludes: list[list[tuple[int, int]]] = []
        # All visible tokens (for flat-window fallback).
        self._all_visible: list[str] = []
        # Pending records in document order, finalized when their own entry
        # block closes (or EOF). The store is a list, never keyed by href, so
        # duplicate anchors that share one URL are all preserved: the record
        # carries the document-order start index, the entry-block depth, the
        # block start, the nearest entry-block tag, and the
        # navigation-landmark flag at anchor completion time (Issue #334
        # review).
        self._pending: list[_EntryBlockRecord] = []
        # Visible texts of author-marked elements per open entry block,
        # parallel to _block_stack: an element carrying rel/itemprop/
        # data-author/class author metadata inside the block is grounded
        # structural author evidence for the feasibility probe (Issue #334
        # review).
        self._block_author_elements: list[list[str]] = []
        # Explicit grounded author evidence in document order, including
        # explicitly marked author links and non-anchor author-marked
        # elements. URL-inferred evidence is kept in a separate stack so it
        # cannot outrank explicit evidence accidentally.
        self._block_author_evidence: list[list[str]] = []
        self._block_inferred_author_evidence: list[list[str]] = []
        # Visible text events retain whether each chunk came from an anchor
        # and whether it came from site-chrome landmark text. This gives
        # marker parsing exact structural boundaries instead of deleting every
        # occurrence of a book title from the whole context.
        self._block_text_events: list[list[tuple[int, bool, str, bool]]] = []
        # The open author-marked element as (tag, element depth at start,
        # owning entry-block depth), with its collected text parts, so a
        # multi-token author span remains attached to its own block across
        # nested entry blocks.
        self._author_element: tuple[str, int, int] | None = None
        self._author_parts: list[str] = []
        # All anchor start indices seen so far (for flat-window boundaries).
        self._anchor_indices: list[int] = []
        # Final records in document order. A nested block closes before the
        # block that encloses it, so records are finalized out of document
        # order; close() sorts them once, because audible_chart_ranks reads
        # the rank sequence straight off entry order.
        self.records: list[_EntryBlockRecord] = []
        # Declared entry containers are rows even when they contain no anchor.
        # Ranking grounding uses these bounded visible texts directly.
        self.declared_rows: list[str] = []
        # Legacy result list of (href, anchor_text, entry_context_text) in
        # document order, derived from records at close time for the
        # configured-source consumers.
        self.entries: list[tuple[str, str, str]] = []
        # Tag of each open entry block, parallel to _block_stack.
        self._block_tags: list[str] = []
        # Owning element depth of each open entry block. Scoped rows can
        # contain child elements with the same tag, so tag equality alone is
        # not enough to identify the row's own end tag.
        self._block_element_depths: list[int] = []
        # Identity of each open entry block, parallel to _block_stack, so
        # anchors that belong to the same bounded container can be grouped.
        self._block_ids: list[int] = []
        self._next_block_id = 0
        # Element depth of the current element (void tags do not count), so
        # an end tag can be matched to the landmark it opened: a nested div
        # inside role="navigation" must not prematurely end the landmark
        # (Issue #334 review).
        self._element_depth = 0
        # Open policy landmarks as (element tag, kind, element depth at
        # start), innermost last. Kinds are "nav", "header", and "footer"
        # (from the elements and their ARIA roles, see _landmark_kind).
        self._landmarks: list[tuple[str, str, int]] = []
        # All open landmarks are also tracked for marker exclusion. A
        # per-entry header/footer stays eligible for book anchors, but its
        # chrome marker text must not become author evidence.
        self._marker_landmarks: list[tuple[str, str, int]] = []
        # Whether the open anchor element carries an author identity
        # attribute (rel="author", itemprop="author", data-author, or an
        # author-bearing class).
        self._a_author_attr = False

    def _push_visible(self, data: str) -> None:
        index = len(self._all_visible)
        self._all_visible.append(data)
        for i in range(len(self._block_stack)):
            self._block_stack[i] += f" {data} "
            self._block_text_events[i].append((index, self._in_a, data, bool(self._marker_landmarks)))

    def _marker_runs(
        self,
        depth: int,
        block_start: int | None,
        window_end: int | None,
        excludes: list[tuple[int, int]],
    ) -> list[str]:
        """Return non-anchor text runs that begin with a known author marker.

        Anchor chunks are boundaries, not text to clean away. This preserves
        the exact marker payload when a title word also appears in the name,
        and it prevents an anchor titled ``By ...`` from becoming a marker.
        """
        if block_start is None or not 0 <= depth < len(self._block_text_events):
            return []
        end = window_end if window_end is not None else len(self._all_visible)
        runs: list[str] = []
        current: list[str] = []

        def flush() -> None:
            if not current:
                return
            candidate = _normalize_whitespace(" ".join(current))
            rank_stripped = re.sub(
                r"^(?:\d{1,3}(?:st|nd|rd|th)?[.)]?\s*|#\s*\d+\s*|№\s*\d+\s*|rank\s+\d+\s*)+",
                "",
                candidate,
                flags=re.IGNORECASE,
            )
            if AUTHOR_MARKER_RE.match(rank_stripped):
                runs.append(candidate)
            current.clear()

        for index, in_anchor, data, in_landmark in self._block_text_events[depth]:
            if (
                index < block_start
                or index >= end
                or in_landmark
                or any(start <= index < stop for start, stop in excludes)
            ):
                flush()
                continue
            if in_anchor:
                flush()
            else:
                current.append(data)
        flush()
        return runs

    def _finalize_entry(
        self,
        record: _EntryBlockRecord,
        window_end: int | None = None,
        excludes: list[tuple[int, int]] | None = None,
    ) -> None:
        """Resolve and record one entry with its bounded context."""
        record.context = self._entry_context_for_anchor(
            record.start_index, record.depth, record.block_start, window_end, excludes
        )
        self.records.append(record)

    def _flush_pending(self, *, depth: int | None = None, only_flat: bool = False) -> None:
        """Finalize pending records using current block context.

        With *depth*, finalize the records whose own entry block is closing:
        that block's text, including nested blocks that carry no product
        anchor of their own (an Audible flyout holds the author there), is the
        record's context. With *only_flat*, finalize just the anchors that
        have no enclosing entry block, which is what a following sibling
        block does to flat markup."""
        # Pre-compute all anchor indices from pending records so that
        # flat-markup context windows can see sibling anchor boundaries.
        all_indices = sorted(set([record.start_index for record in self._pending] + self._anchor_indices))
        saved, self._anchor_indices = self._anchor_indices, all_indices
        window_end = len(self._all_visible)
        remaining: list[_EntryBlockRecord] = []
        for record in sorted(self._pending, key=lambda record: record.start_index):
            if only_flat and record.depth >= 0:
                remaining.append(record)
                continue
            if depth is not None and record.depth != depth:
                remaining.append(record)
                continue
            excludes = self._block_excludes[record.depth] if 0 <= record.depth < len(self._block_excludes) else []
            if 0 <= record.depth < len(self._block_author_elements):
                record.author_elements = tuple(self._block_author_elements[record.depth])
                record.author_evidence = tuple(self._block_author_evidence[record.depth])
                record.marker_evidence = tuple(
                    self._marker_runs(record.depth, record.block_start, window_end, excludes)
                )
                record.inferred_author_evidence = tuple(self._block_inferred_author_evidence[record.depth])
            self._finalize_entry(record, window_end, excludes)
        self._pending = remaining
        # Restore and merge.
        self._anchor_indices = sorted(set(saved) | set(all_indices))

    def _scope_path_matches(self, class_tokens: frozenset[str]) -> bool:
        if self._entry_scope_classes is None:
            return False
        prefix = self._entry_scope_classes[:-1]
        if not prefix or prefix[-1] not in class_tokens:
            return False
        ancestors = self._scope_frames[-(len(prefix) - 1) :] if len(prefix) > 1 else []
        return all(expected in frame.class_tokens for expected, frame in zip(prefix[:-1], ancestors, strict=True))

    def _pop_scope_frame(self, tag: str) -> None:
        if self._entry_scope_classes is None:
            return
        for index in range(len(self._scope_frames) - 1, -1, -1):
            if self._scope_frames[index].tag == tag:
                del self._scope_frames[index:]
                return

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        is_void = tag in _VOID_TAGS
        if not is_void:
            self._element_depth += 1
        if self._entry_scope_classes is not None:
            hidden = _push_visibility(self._visibility_frames, tag, attrs)
            class_tokens = frozenset(
                token for name, value in attrs if name.casefold() == "class" for token in (value or "").split()
            )
            scope_match = self._scope_path_matches(class_tokens)
            parent_scope = self._scope_frames[-1].scope_match if self._scope_frames else False
            parent_chrome = any(frame.chrome for frame in self._scope_frames)
            chrome = parent_chrome or _visible_title_is_chrome(tag, attrs, page_level=True)
            if not is_void:
                self._scope_frames.append(_EntryScopeFrame(tag, hidden, chrome, class_tokens, scope_match))
            if hidden:
                return
            declared_entry = parent_scope and self._entry_scope_classes[-1] in class_tokens and not chrome
        else:
            declared_entry = False
            if _push_visibility(self._visibility_frames, tag, attrs):
                return
        if self._in_a and tag in _ANCHOR_BLOCK_BOUNDARY_TAGS:
            self._a_text.start_block(tag)
        # A header/footer landmark (element or banner/contentinfo role) is
        # page-level site chrome only when it opens outside any per-entry
        # container: <article><header>…</header></article> keeps its anchors
        # eligible, while a <header> that wraps the whole list (it opens
        # before any entry block) keeps filtering (Issue #334 review).
        # Navigation landmarks always apply: a <nav> or role="navigation"
        # inside an article is still site chrome, never a book link (Issue
        # #334 review).
        kind = _landmark_kind(tag, attrs)
        if kind is not None and (kind == "nav" or not self._block_stack):
            self._marker_landmarks.append((tag, kind, self._element_depth))
            self._landmarks.append((tag, kind, self._element_depth))
        if (self._entry_scope_classes is None and tag in self.ENTRY_BLOCKS) or declared_entry:
            # An entry inside a block waits for its own block to close, so a
            # nested block no longer cuts its context short. Only flat anchors,
            # which have no block to wait for, are bounded here by the arrival
            # of this block.
            if not self._block_stack:
                self._flush_pending(only_flat=True)
            self._block_stack.append("")
            self._block_starts.append(len(self._all_visible))
            self._block_excludes.append([])
            self._block_author_elements.append([])
            self._block_author_evidence.append([])
            self._block_inferred_author_evidence.append([])
            self._block_text_events.append([])
            self._block_tags.append(tag)
            self._block_element_depths.append(self._element_depth)
            self._block_ids.append(self._next_block_id)
            self._next_block_id += 1
        if tag == "a":
            self._in_a = True
            self._a_text = _AnchorText()
            self._a_author_attr = _element_has_author_attr(attrs)
            href = ""
            for attr, value in attrs:
                if attr.casefold() == "href" and isinstance(value, str):
                    href = value.strip()
                    break
            if href:
                self._a_href = urljoin(self._source_page, href)
                self._a_start_index = len(self._all_visible)
            else:
                self._a_href = None
                self._a_start_index = None
        elif (
            self._author_element is None
            and not self._in_a
            and self._block_stack
            and tag not in self.ENTRY_BLOCKS
            and tag not in _VOID_TAGS
            and not any(kind == "nav" for _tag, kind, _depth in self._landmarks)
            and _element_has_author_attr(attrs)
        ):
            # An author-marked non-anchor element (span/div carrying
            # rel/itemprop/data-author/class author metadata) inside an entry
            # block is grounded structural author evidence: its whole visible
            # text is captured for the feasibility probe (Issue #334 review).
            self._author_element = (tag, self._element_depth, len(self._block_stack) - 1)
            self._author_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        closing_hidden = _pop_visibility(self._visibility_frames, tag)
        self._pop_scope_frame(tag)
        if closing_hidden is None or closing_hidden:
            if tag not in _VOID_TAGS:
                self._element_depth = max(0, self._element_depth - 1)
            return
        if self._in_a and tag in _ANCHOR_BLOCK_BOUNDARY_TAGS:
            self._a_text.end_block(tag)
        # Leave the innermost landmark only when the closing tag is the
        # landmark's own element (same tag and element depth), so nested divs
        # inside role="navigation" do not prematurely end the landmark.
        if (
            self._marker_landmarks
            and self._marker_landmarks[-1][0] == tag
            and self._marker_landmarks[-1][2] == self._element_depth
        ):
            self._marker_landmarks.pop()
        if self._landmarks and self._landmarks[-1][0] == tag and self._landmarks[-1][2] == self._element_depth:
            self._landmarks.pop()
        if (
            self._author_element is not None
            and tag == self._author_element[0]
            and self._author_element[1] == self._element_depth
        ):
            self._flush_author_element()
        if tag == "a" and self._in_a:
            self._in_a = False
            if self._a_href and self._a_start_index is not None:
                anchor_text, anchor_segments, anchor_segment_tags = self._a_text.finish()
                if anchor_text:
                    depth = len(self._block_stack) - 1
                    kinds = {kind for _element_tag, kind, _element_depth in self._landmarks}
                    in_nav = "nav" in kinds
                    in_header = "header" in kinds
                    in_footer = "footer" in kinds
                    explicit_author = self._a_author_attr or (
                        AUTHOR_LINK_MARKER_RE.match(anchor_text) is not None
                        and self._a_href is not None
                        and _author_page_url(self._a_href)
                    )
                    link_kind = self._classify_link(anchor_text, in_nav or in_header or in_footer)
                    if self._block_stack and link_kind == LINK_KIND_AUTHOR and not (in_nav or in_header or in_footer):
                        if explicit_author:
                            self._block_author_evidence[-1].append(anchor_text)
                        else:
                            inferred_name = _url_only_author_name(anchor_text, self._a_href)
                            if inferred_name:
                                self._block_inferred_author_evidence[-1].append(inferred_name)
                    if self._entry_scope_classes is None or self._block_stack:
                        self._pending.append(
                            _EntryBlockRecord(
                                href=self._a_href,
                                anchor_text=anchor_text,
                                anchor_segments=anchor_segments,
                                anchor_segment_tags=anchor_segment_tags,
                                start_index=self._a_start_index,
                                depth=depth,
                                block_start=self._block_starts[-1] if self._block_stack else None,
                                block_tag=self._block_tags[depth] if 0 <= depth < len(self._block_tags) else "",
                                in_nav=in_nav,
                                in_header=in_header,
                                in_footer=in_footer,
                                block_id=self._block_ids[-1] if self._block_stack else -1,
                                link_kind=link_kind,
                            )
                        )
            self._a_href = None
            self._a_start_index = None
            self._a_text = _AnchorText()
        block_is_closing = (self._entry_scope_classes is None and tag in self.ENTRY_BLOCKS) or (
            self._entry_scope_classes is not None
            and bool(self._block_tags)
            and self._block_tags[-1] == tag
            and self._block_element_depths[-1] == self._element_depth
        )
        if self._block_stack and block_is_closing:
            self._close_block()
        if tag not in _VOID_TAGS:
            self._element_depth = max(0, self._element_depth - 1)

    def _classify_link(self, anchor_text: str, in_landmark: bool) -> str:
        """Typed link kind for the generic policy (Issue #334).

        An explicit author attribute, or an explicit marker on an
        author-page link, marks an ``author`` link; a deterministic
        commerce/UI label marks an ``action`` link; an author-page URL path
        marks an ``author`` link even without an attribute or marker, so an
        author route is excluded from primary
        book-title selection. URL-only evidence is retained as author
        evidence only when its visible label overlaps the URL identity slug;
        generic labels such as ``Profile`` remain excluded links but do not
        become authors (Issue #334 hardening). A link inside a
        nav/header/footer landmark is ``navigation``; everything else is a
        ``book`` link."""
        if self._a_author_attr:
            return LINK_KIND_AUTHOR
        if self._a_href is not None and _author_page_url(self._a_href):
            return LINK_KIND_AUTHOR
        if is_action_label(normalize_visible_text(anchor_text)):
            return LINK_KIND_ACTION
        # Accessible cover labels are chrome like action labels: they are
        # never book-level links, and their anchor text is stripped from the
        # author parse as known chrome text (Issue #334 review).
        if is_cover_label(normalize_visible_text(anchor_text)):
            return LINK_KIND_ACTION
        if in_landmark:
            return LINK_KIND_NAVIGATION
        return LINK_KIND_BOOK

    def _is_product_record(self, record: _EntryBlockRecord) -> bool:
        """Return whether a record can qualify its block as a product entry."""
        return record.link_kind == LINK_KIND_BOOK and bool(record.href) and bool(record.anchor_text.strip())

    def _reclassify_marker_links(self, depth: int) -> None:
        """Classify colon-marked links only when their block has a product.

        A link such as ``By: The River`` can be a real book title, so the
        marker is not author evidence by itself. When the same bounded block
        also contains a product link, the marker link is an author link even
        when its URL has no author-route segment. A marker-only nested block
        is also author evidence when an enclosing block already has a product
        link, which keeps nested author sections attached to that product.
        """
        records = [record for record in self._pending if record.depth == depth]
        marker_records = [
            record
            for record in records
            if record.link_kind == LINK_KIND_BOOK and AUTHOR_LINK_MARKER_RE.match(record.anchor_text) is not None
        ]
        if not marker_records:
            return
        has_local_product = any(self._is_product_record(record) for record in records if record not in marker_records)
        has_enclosing_product = depth >= 1 and any(
            record.depth < depth and self._is_product_record(record) for record in self._pending
        )
        if not has_local_product and not has_enclosing_product:
            return
        for record in marker_records:
            record.link_kind = LINK_KIND_AUTHOR
            self._block_author_evidence[depth].append(record.anchor_text)

    def _close_block(self) -> None:
        """Finalize the innermost open entry block.

        Its own entries are complete, so the whole block is their context. A
        nested block without a product belongs to the enclosing product and
        propagates its author evidence. A nested block with its own product
        stays separate and its visible range is excluded from the enclosing
        context."""
        depth = len(self._block_stack) - 1
        # An author-marked element the markup never closed belongs to this
        # block only when this is its owning block. An outer element can span
        # a nested entry and must remain open until its own end or owner close.
        if self._author_element is not None and self._author_element[2] == depth:
            self._flush_author_element()
        self._reclassify_marker_links(depth)
        yielded_product = any(record.depth == depth and self._is_product_record(record) for record in self._pending)
        if self._entry_scope_classes is not None:
            self.declared_rows.append(
                _normalize_whitespace(
                    self._block_context(self._block_starts[depth], len(self._all_visible), self._block_excludes[depth])
                )
            )
        child_author_elements = self._block_author_elements[depth]
        child_author_evidence = self._block_author_evidence[depth]
        child_inferred_evidence = self._block_inferred_author_evidence[depth]
        self._flush_pending(depth=depth)
        if depth >= 1:
            if yielded_product:
                self._block_excludes[depth - 1].append((self._block_starts[depth], len(self._all_visible)))
            else:
                self._block_author_elements[depth - 1].extend(child_author_elements)
                self._block_author_evidence[depth - 1].extend(child_author_evidence)
                self._block_inferred_author_evidence[depth - 1].extend(child_inferred_evidence)
        self._block_stack.pop()
        self._block_starts.pop()
        self._block_excludes.pop()
        self._block_author_elements.pop()
        self._block_author_evidence.pop()
        self._block_inferred_author_evidence.pop()
        self._block_text_events.pop()
        self._block_tags.pop()
        self._block_element_depths.pop()
        self._block_ids.pop()

    def _flush_author_element(self) -> None:
        """Append captured author text to the element's owning block."""
        if self._author_element is None:
            return
        owner_depth = self._author_element[2]
        text = _normalize_whitespace("".join(self._author_parts))
        if text and 0 <= owner_depth < len(self._block_stack):
            self._block_author_elements[owner_depth].append(text)
            self._block_author_evidence[owner_depth].append(text)
        self._author_element = None
        self._author_parts = []

    def close(self) -> None:
        # Unwind blocks the markup never closed, innermost first, so a page that
        # omits </li> bounds its entries the same way a well-formed one does.
        while self._block_stack:
            self._close_block()
        self._flush_pending()
        # Sort the single internal record list once in document order, then
        # expose the legacy entries list for the configured-source consumers.
        self.records.sort(key=lambda record: record.start_index)
        self.entries = [(record.href, record.anchor_text, record.context) for record in self.records]
        super().close()

    def _block_context(self, block_start: int, window_end: int, excludes: list[tuple[int, int]]) -> str:
        """Return one entry block's visible text, minus nested product entries.

        A nested entry block that carries no product anchor (an Audible flyout
        holding ``By: <author>``) is part of this product. A nested entry block
        that carries its own product anchor is a different product, and its
        text must not ground a claim about this one."""
        tokens: list[str] = []
        index = block_start
        for cut_start, cut_end in sorted(excludes):
            if cut_start >= window_end:
                break
            if cut_start > index:
                tokens.extend(self._all_visible[index : min(cut_start, window_end)])
            index = max(index, cut_end)
        if index < window_end:
            tokens.extend(self._all_visible[index:window_end])
        return " ".join(tokens)

    def _entry_context_for_anchor(
        self,
        start_index: int,
        depth: int,
        block_start: int | None,
        window_end: int | None = None,
        excludes: list[tuple[int, int]] | None = None,
    ) -> str:
        """Return the bounded visible-text context for an anchor.

        Prefer the visible text of the anchor's own entry block, which includes
        nested blocks that hold no product anchor of their own and excludes
        those that do. When no enclosing entry block exists (flat/article
        markup), use a conservative neighbourhood that clips at adjacent
        anchors."""
        # The anchor's own product entry text.
        if depth >= 0 and block_start is not None:
            end = window_end if window_end is not None else len(self._all_visible)
            window = self._block_context(block_start, end, excludes or [])
            if window.strip():
                return _normalize_whitespace(window)
        # Flat markup: use the anchor text plus immediately adjacent tokens
        # up to the next anchor index or a small bound, whichever is closer.
        sorted_indices = sorted(set(self._anchor_indices))
        if start_index in sorted_indices:
            pos = sorted_indices.index(start_index)
            prev_anchor_start = sorted_indices[pos - 1] if pos > 0 else 0
            next_anchor_start = sorted_indices[pos + 1] if pos + 1 < len(sorted_indices) else len(self._all_visible)
        else:
            pos = -1
            prev_anchor_start = 0
            next_anchor_start = len(self._all_visible)
        start = max(prev_anchor_start, start_index - 2)
        end = min(next_anchor_start, start_index + 6)
        window = " ".join(self._all_visible[start:end])
        return _normalize_whitespace(window)[: self.MAX_WINDOW]

    def handle_data(self, data: str) -> None:
        if _visibility_is_hidden(self._visibility_frames):
            return
        if self._in_a and self._a_href:
            self._a_text.add(data)
        if data.strip():
            self._push_visible(data)
            if self._author_element is not None:
                self._author_parts.append(data)


# URL-path segments that mark a link as an author page rather than a book
# page. Deterministic and fixed; matching is exact against one path segment,
# so ``/authors/jane-author`` is an author page while ``/books/the-author``
# and ``/books-and-authors/...`` are not. An unmarked linked author must
# never become the primary book-level link of its entry, so URL evidence
# classifies it before the policy layer picks the primary link (Issue #334
# review).
_AUTHOR_PAGE_SEGMENTS = frozenset(
    {
        "author",
        "authors",
        "writer",
        "writers",
        "contributor",
        "contributors",
        "creator",
        "creators",
        "person",
        "people",
        "profile",
        "profiles",
        "bio",
        "bios",
        "autor",
        "autoren",
        "autorin",
        "autorinnen",
        "verfasser",
        "verfasserin",
        "auteur",
        "auteurs",
        "autrice",
        "autrices",
        "autore",
        "autori",
        "autrici",
        "autora",
        "autoras",
        "autores",
        "autorzy",
        "autorka",
        "autorki",
        "pisarz",
        "pisarze",
        "pisarka",
        "spisovatel",
        "spisovatelka",
        "scriitor",
        "scriitori",
        "forfatter",
        "forfattere",
        "forfatteren",
        "författare",
        "författaren",
        "schrijver",
        "schrijvers",
        "kirjailija",
        "yazar",
        "автор",
        "авторы",
        "авторка",
        "авторки",
    }
)


def _author_page_url(url: str) -> bool:
    """True when the URL path carries an exact author-page segment.

    The match is exact per path segment (casefolded), so a book page whose
    path merely contains the word ``author`` (``/books/the-author``) is not
    an author page. Deterministic evidence only - no model, network, or
    loose-text guessing (Issue #334 review)."""
    path = urlsplit(url).path.casefold()
    return any(segment in _AUTHOR_PAGE_SEGMENTS for segment in path.split("/") if segment)


_GENERIC_URL_ONLY_LABELS = frozenset(
    {
        "about",
        "bio",
        "contributor",
        "contributors",
        "meet",
        "profile",
        "profiles",
        "see",
        "view",
        "visit",
    }
)


def _url_only_author_name(anchor_text: str, url: str | None) -> str:
    """Return a visible name grounded by the author URL identity.

    An author route is not enough to trust its label. The returned text must
    be exactly the contiguous visible token sequence grounded by the URL
    identity. A one-token identity therefore returns only that visible token,
    rather than absorbing adjacent CTA or role words.
    """
    if not url:
        return ""
    path_segments = [unquote(segment) for segment in urlsplit(url).path.split("/") if segment]
    route_index = next(
        (index for index, segment in enumerate(path_segments) if segment.casefold() in _AUTHOR_PAGE_SEGMENTS),
        None,
    )
    if route_index is None:
        return ""
    identity_tokens = [
        token.casefold() for segment in path_segments[route_index + 1 :] for token in re.findall(r"[^\W\d_]+", segment)
    ]
    if not identity_tokens:
        return ""
    visible_tokens = [
        (match.group(0).casefold(), match.start(), match.end()) for match in re.finditer(r"[^\W\d_]+", anchor_text)
    ]
    if not visible_tokens:
        return ""
    identity_length = len(identity_tokens)
    match_start: int | None = None
    match_end: int | None = None
    for index in range(len(visible_tokens) - identity_length + 1):
        if [token for token, _start, _end in visible_tokens[index : index + identity_length]] == identity_tokens:
            match_start = index
            match_end = index + identity_length
            break
    if match_start is None or match_end is None:
        return ""
    candidate = anchor_text[visible_tokens[match_start][1] : visible_tokens[match_end - 1][2]]
    if normalize_visible_text(candidate) in _GENERIC_URL_ONLY_LABELS:
        return ""
    return candidate


def _author_prefixed_anchor_matches(
    normalized_anchor: str,
    normalized_title: str,
    normalized_authors: set[str],
) -> bool:
    """Return True when *normalized_anchor* is ``Author(s): Title``
    where the suffix after the *first* colon matches *normalized_title*
    exactly and every normalized author appears in the prefix before
    the separator.

    Uses ``str.partition`` on the *first* colon so that titles
    containing colons (``Title: Subtitle``) can still ground when
    the requested title is ``Title: Subtitle``.

    This is a deterministic, source-independent fallback for
    longlist/unordered-list markup where the anchor text carries
    semantic ``Author: Title`` spans rather than a bare title."""
    separator = ":"
    if separator not in normalized_anchor:
        return False
    prefix, _sep, suffix = normalized_anchor.partition(separator)
    if suffix.strip() != normalized_title:
        return False
    return all(author in prefix for author in normalized_authors)


class _EntrySectionLocator(HTMLParser):
    """Find the ``<section>`` elements whose own heading states one exact text.

    A heading belongs to the innermost ``<section>`` open around it. A heading
    inside a link is a book card's title, not a section heading, so it never
    names a section.

    A section that is hidden, by its own attributes or by any ancestor, is
    never a match, and neither is a hidden heading. The returned markup starts
    at the section's own tag, so an ancestor's visibility would otherwise be
    lost and hidden cards would ground as visible ones.
    """

    def __init__(self, markup: str, heading: str) -> None:
        super().__init__(convert_charrefs=True)
        self._heading = normalize_visible_text(heading)
        self._line_starts = [0]
        for match in re.finditer(r"\n", markup):
            self._line_starts.append(match.end())
        # Open sections as [start offset, own heading matched, visible].
        self._sections: list[list[Any]] = []
        self._visibility_frames: list[_VisibilityFrame] = []
        self._anchor_depth = 0
        self._heading_tag = ""
        self._heading_parts: list[str] = []
        self.spans: list[tuple[int, int]] = []

    def _offset(self) -> int:
        line, column = self.getpos()
        return self._line_starts[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        hidden = _push_visibility(self._visibility_frames, tag, attrs)
        if tag == "section":
            self._sections.append([self._offset(), False, not hidden])
        elif tag == "a":
            self._anchor_depth += 1
        elif tag in _HEADING_TAGS and not self._heading_tag and not self._anchor_depth and not hidden:
            self._heading_tag = tag
            self._heading_parts = []

    def handle_endtag(self, tag: str) -> None:
        _pop_visibility(self._visibility_frames, tag)
        if tag == "section" and self._sections:
            start, matched, visible = self._sections.pop()
            if matched and visible:
                self.spans.append((start, self._offset()))
        elif tag == "a" and self._anchor_depth:
            self._anchor_depth -= 1
        elif tag == self._heading_tag:
            if self._sections and normalize_visible_text(" ".join(self._heading_parts)) == self._heading:
                self._sections[-1][1] = True
            self._heading_tag = ""

    def handle_data(self, data: str) -> None:
        if self._heading_tag and not _visibility_is_hidden(self._visibility_frames):
            self._heading_parts.append(data)


def entry_section_html(html_content: str, section_heading: str) -> str | None:
    """Return the markup of the one ``<section>`` headed by *section_heading*.

    An award page can keep an earlier stage beside the current one, for
    example the rest of a longlist under its own heading below the shortlist.
    Every linked book on such a page is grounded, so grounding alone cannot
    tell the stages apart. A source that declares ``entry_section_heading``
    is bounded to the section carrying that exact heading.

    The result is ``None`` unless exactly one closed section matches, so a
    page that drops, repeats, or restructures the section fails closed.
    """
    if not normalize_visible_text(section_heading):
        return None
    locator = _EntrySectionLocator(html_content, section_heading)
    locator.feed(html_content)
    locator.close()
    if len(locator.spans) != 1:
        return None
    start, end = locator.spans[0]
    return html_content[start:end]


def exact_book_list_mention_is_grounded(
    html_content: str,
    source_page: str,
    *,
    title: str,
    authors: list[str],
    evidence_url: str,
    parsed: ParsedCapture | None = None,
) -> bool:
    """Verify that a mention's title, every primary author, and evidence URL
    are grounded in a single server-rendered <a> element on the captured page.

    Grounding rule (Issue #715): the evidence URL matches a captured link
    href exactly; the normalized title equals the whole anchor, one whole
    block-delimited segment, or the title after the first colon in an
    ``Author(s): Title`` anchor/segment with every author in the prefix;
    the title differs from every primary author; and every normalized primary
    author is visibly present in the link's bounded entry context (nearest
    ``<li>``, ``<article>``, etc.; for flat markup a conservative visible-text
    neighbourhood). A leading non-heading year segment followed by more card
    text is metadata, not a title.

    Accepted anchor shapes (this docstring is the one place they are
    documented; ``docs/source-onboarding-runbook.md`` section 4 refers here):

    * bare title - ``<a>Stay Alive</a>``;
    * ``Author(s): Title`` - ``<a>Ian Buruma: Stay Alive</a>``;
    * whole book card - one ``<a>`` wrapping year, title and author with the
      title as one block element, as in
      ``<a><p>2026</p><h2>Stay Alive</h2><p>Ian Buruma</p></a>``.

    *parsed*, when given, is the pass's capture of *html_content* (Issue #718).
    """
    if not authors:
        return False
    normalized_title = normalize_visible_text(title)
    normalized_authors = {normalize_visible_text(a) for a in authors if a.strip()}
    if not normalized_title or not normalized_authors or normalized_title in normalized_authors:
        return False
    for record in capture_for(html_content, source_page, parsed).entry_blocks().records:
        if normalize_visible_text(record.href) != normalize_visible_text(evidence_url):
            continue
        # A leading year segment followed by more card text is listing
        # metadata, whatever element holds the title after it. A numeric
        # title in a heading remains eligible.
        segments = record.anchor_segments
        if (
            len(segments) > 1
            and record.anchor_segment_tags[0] not in _HEADING_TAGS
            and re.fullmatch(r"(?:1|2)\d{3}", normalize_visible_text(segments[0]))
        ):
            segments = segments[1:]
        anchor_candidates = (record.anchor_text, *segments)
        title_matches = False
        for candidate in anchor_candidates:
            normalized_candidate = normalize_visible_text(candidate)
            if normalized_candidate == normalized_title or _author_prefixed_anchor_matches(
                normalized_candidate, normalized_title, normalized_authors
            ):
                title_matches = True
                break
        if not title_matches:
            continue
        normalized_context = normalize_visible_text(record.context)
        if all(author in normalized_context for author in normalized_authors):
            return True
        # URL+title matched but not all authors in this link's context. Keep
        # looking: a later same-URL link may be the fully grounded entry.
    return False


def _entry_blocks(html_content: str, source_page: str) -> _EntryBlockCollector:
    collector = _EntryBlockCollector(source_page)
    collector.feed(html_content)
    collector.close()
    return collector


def audible_chart_entries(
    html_content: str, source_page: str, *, parsed: ParsedCapture | None = None
) -> list[tuple[str, str, str]]:
    """Return direct Audible product links with title and bounded entry context."""
    collector = capture_for(html_content, source_page, parsed).entry_blocks()
    return [entry for entry in collector.entries if "/pd/" in entry[0]]


class _AudibleProductContainerCollector(HTMLParser):
    """Collect bounded Audible product-container text and links."""

    def __init__(self, source_page: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source_page = source_page
        self.depth = 0
        self._visibility_frames: list[_VisibilityFrame] = []
        self.container_depth: int | None = None
        self.container_text: list[str] = []
        self.container_links: list[str] = []
        self.containers: list[tuple[list[str], str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        is_void = tag in _VOID_TAGS
        attributes = {name.casefold(): value or "" for name, value in attrs}
        if _push_visibility(self._visibility_frames, tag, attrs):
            if not is_void:
                self.depth += 1
            return
        if (
            tag == "li"
            and self.container_depth is None
            and "productlistitem" in attributes.get("class", "").casefold().split()
        ):
            self.container_depth = self.depth
            self.container_text = []
            self.container_links = []
        if self.container_depth is not None and tag == "a":
            href = attributes.get("href", "").strip()
            if href:
                self.container_links.append(urljoin(self.source_page, href))
        if not is_void:
            self.depth += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        is_void = tag in _VOID_TAGS
        closing_hidden = _pop_visibility(self._visibility_frames, tag)
        if closing_hidden is None or closing_hidden:
            if not is_void:
                self.depth = max(0, self.depth - 1)
            return
        if self.container_depth is not None and tag == "li" and self.depth == self.container_depth + 1:
            self.containers.append((self.container_links, _normalize_whitespace(" ".join(self.container_text))))
            self.container_depth = None
            self.container_text = []
            self.container_links = []
        if not is_void:
            self.depth = max(0, self.depth - 1)

    def handle_data(self, data: str) -> None:
        if self.container_depth is not None and not _visibility_is_hidden(self._visibility_frames) and data.strip():
            self.container_text.append(data)


def _audible_product_containers(html_content: str, source_page: str) -> list[tuple[list[str], str]]:
    collector = _AudibleProductContainerCollector(source_page)
    collector.feed(html_content)
    collector.close()
    return collector.containers


def _audible_chart_authors_in_product_window(page: ParsedCapture, evidence_url: str, authors: set[str]) -> bool:
    """Check author markers in one bounded Audible product container.

    Some Audible captures put the product, author, and narrator links in
    sibling list items inside one product container.
    The exact normalized product URL binds the fallback to that container.
    """
    wanted_url = normalize_visible_text(evidence_url)
    for links, text in page.audible_product_containers():
        if not any(normalize_visible_text(link) == wanted_url for link in links):
            continue
        # The marker is a whole word, so a name such as "Byrne", "Parker" or "Vonnegut" is never split.
        marker_sections = re.split(r"\b(?:by|von|par)\b\s*:?\s*", normalize_visible_text(text))
        marked_text = marker_sections[1:]
        if marked_text and all(any(author in section for section in marked_text) for author in authors):
            return True
    return False


def _audible_product_container_texts(page: ParsedCapture, evidence_url: str) -> list[str]:
    """Return the normalized text of every product container that links *evidence_url*."""
    wanted_url = normalize_visible_text(evidence_url)
    return [
        normalize_visible_text(text)
        for links, text in page.audible_product_containers()
        if any(normalize_visible_text(link) == wanted_url for link in links)
    ]


def audible_chart_entry_title(
    html_content: str, source_page: str, *, title: str, evidence_url: str, parsed: ParsedCapture | None = None
) -> str:
    """Return the chart's own title for an extracted Audible title, or *title* unchanged.

    The chart entry's link text is the title; the product container shows the
    subtitle in its own element. An extractor sometimes joins the two, and the
    grounding check, which requires the link text exactly, then drops the row
    (Issue #835). When *title* is exactly the link text of the entry at
    *evidence_url* followed by text that the same product container shows,
    the link text is returned, so every run stores one title and the book
    keeps its identity. Any other title is returned unchanged and is grounded
    or refused as before. *parsed*, when given, is the pass's capture of
    this page (Issue #718).
    """
    page = capture_for(html_content, source_page, parsed)
    wanted_url = normalize_visible_text(evidence_url)
    anchors = {
        _normalize_whitespace(anchor)
        for href, anchor, _context in audible_chart_entries(html_content, source_page, parsed=page)
        if normalize_visible_text(href) == wanted_url
    }
    if len(anchors) != 1:
        return title
    chart_title = anchors.pop()
    key = normalize_visible_text(chart_title)
    wanted = normalize_visible_text(title)
    if not key or wanted == key or not wanted.startswith(key) or wanted[len(key) : len(key) + 1] not in {" ", ":"}:
        return title
    rest = wanted[len(key) :].lstrip(" :").strip()
    if not rest:
        return title
    if any(rest in text for text in _audible_product_container_texts(page, evidence_url)):
        return chart_title
    return title


def audible_chart_mention_is_grounded(
    html_content: str,
    source_page: str,
    *,
    rank: int,
    title: str,
    authors: list[str],
    evidence_url: str,
    parsed: ParsedCapture | None = None,
) -> bool:
    """Ground Audible rank, title, authors, and /pd/ URL in one bounded entry.

    *parsed*, when given, is the pass's capture of this page (Issue #718).
    """
    page = capture_for(html_content, source_page, parsed)
    entries = audible_chart_entries(html_content, source_page, parsed=page)
    wanted_title = normalize_visible_text(title)
    wanted_url = normalize_visible_text(evidence_url)
    wanted_authors = {normalize_visible_text(author) for author in authors if author.strip()}
    rank_pattern = re.compile(rf"^{rank}(?:\.|\D)")
    for href, anchor, context in entries:
        normalized_context = normalize_visible_text(context)
        if normalize_visible_text(href) != wanted_url or normalize_visible_text(anchor) != wanted_title:
            continue
        if not wanted_authors or not rank_pattern.search(normalized_context):
            continue
        if all(author in normalized_context for author in wanted_authors):
            return True
        if _audible_chart_authors_in_product_window(page, evidence_url, wanted_authors):
            return True
    return False


def audible_chart_ranks(html_content: str, source_page: str, *, parsed: ParsedCapture | None = None) -> list[int]:
    """Return ranks from bounded Audible chart entries containing direct /pd/ links."""
    ranks: list[int] = []
    for href, _anchor, context in audible_chart_entries(html_content, source_page, parsed=parsed):
        if "/pd/" not in href:
            continue
        match = re.match(r"(\d{1,3})(?:\.|\D)", normalize_visible_text(context))
        if match:
            ranks.append(int(match.group(1)))
    return ranks


def _visible_audible_de_author_names(visible_parts: list[str]) -> list[str]:
    """Extract bounded visible ``Von:`` names from ordered data parts."""
    names: list[str] = []
    for index, part in enumerate(visible_parts):
        marker = re.search(r"\bVon\s*:", part, flags=re.IGNORECASE)
        if marker is None:
            continue
        name = _normalize_whitespace(part[marker.end() :])
        if not name:
            name = next(
                (_normalize_whitespace(next_part) for next_part in visible_parts[index + 1 :] if next_part.strip()),
                "",
            )
        if name and not re.fullmatch(r"\d{1,3}", name):
            names.append(name[:120])
    return names


def audible_de_entry_author_names(html_content: str) -> dict[str, list[str]]:
    """Return per-entry German ``Von:`` author names keyed by raw ``/pd/`` path.

    On the German Audible chart pages the primary-author marker ``Von:`` is
    rendered in a flyout block that follows the entry's product link rather
    than inside the bounded entry block itself.  This helper therefore pairs
    each direct ``/pd/`` link with the bounded HTML window up to the next
    ``/pd/`` link and collects visible ``Von:`` marker names inside that
    window, in document order."""
    paths: list[str] = []
    positions: list[int] = []
    for match in re.finditer(r'href="([^"]*?/pd/[^"]+?)(?:\?|\")', html_content):
        path = urlsplit(html.unescape(match.group(1))).path
        if path not in paths:
            paths.append(path)
            positions.append(match.start())
    result: dict[str, list[str]] = {}
    for index, path in enumerate(paths):
        start = positions[index]
        end = positions[index + 1] if index + 1 < len(positions) else len(html_content)
        window = html_content[start:end]
        # Keep data chunks ordered. A marker and name may be split across
        # adjacent spans, but later sibling text must not extend the name.
        result[path] = _visible_audible_de_author_names(_parse(window).visible_parts)
    return result


def audible_de_chart_mention_is_grounded(
    html_content: str,
    source_page: str,
    *,
    rank: int,
    title: str,
    authors: list[str],
    evidence_url: str,
    parsed: ParsedCapture | None = None,
) -> bool:
    """Ground a German Audible chart mention: rank/title/URL in one bounded
    entry plus every primary author visible in that entry's ``Von:`` marker.

    The rank, title anchor, and direct ``/pd/`` URL are grounded exactly like
    the English variant.  Because the German pages render the author in a
    ``Von:`` flyout outside the entry block, author grounding uses the
    per-entry marker windows from :func:`audible_de_entry_author_names`.
    *parsed*, when given, is the pass's capture of this page (Issue #718)."""
    page = capture_for(html_content, source_page, parsed)
    entries = audible_chart_entries(html_content, source_page, parsed=page)
    wanted_title = normalize_visible_text(title)
    wanted_url_path = normalize_visible_text(urlsplit(evidence_url).path)
    wanted_authors = {normalize_visible_text(author) for author in authors if author.strip()}
    rank_pattern = re.compile(rf"^{rank}(?:\.|\D)")
    author_windows = page.audible_de_author_names()
    for href, anchor, context in entries:
        normalized_context = normalize_visible_text(context)
        if (
            normalize_visible_text(urlsplit(href).path) != wanted_url_path
            or normalize_visible_text(anchor) != wanted_title
            or not wanted_authors
            or not rank_pattern.search(normalized_context)
        ):
            continue
        raw_path = urlsplit(href).path
        marker_names = [normalize_visible_text(name) for name in author_windows.get(raw_path, [])]
        if marker_names and all(any(author in name for name in marker_names) for author in wanted_authors):
            return True
    return False


@dataclass(frozen=True)
class RankingPageIdentity:
    """First-party category and reporting-period identity for a ranking page."""

    heading: str
    category: str
    reporting_period: str


def _ranking_row_bound(source: Mapping[str, Any], *fields: str) -> object:
    for field in fields:
        value = source.get(field)
        if value is not None and value != "":
            return value
    return None


def ranking_row_bounds(source: Mapping[str, Any]) -> tuple[int | None, int | None, int | None]:
    """Return the exact, minimum, and maximum raw-row bounds."""
    exact = _ranking_row_bound(source, "ranking_expected_raw_row_count")
    minimum = _ranking_row_bound(source, "ranking_min_raw_row_count")
    maximum = _ranking_row_bound(source, "ranking_max_raw_row_count")
    values: list[int | None] = []
    for value in (exact, minimum, maximum):
        if value is None or value == "":
            values.append(None)
        elif isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("ranking raw row bounds must be integers")
        else:
            values.append(value)
    exact_value, minimum_value, maximum_value = values
    if exact_value is not None and exact_value < 1:
        raise ValueError("ranking_expected_raw_row_count must be positive")
    if minimum_value is not None and minimum_value < 1:
        raise ValueError("ranking_min_raw_row_count must be positive")
    if maximum_value is not None and maximum_value < 1:
        raise ValueError("ranking_max_raw_row_count must be positive")
    if exact_value is not None and (minimum_value is not None or maximum_value is not None):
        raise ValueError("ranking_expected_raw_row_count cannot be combined with raw row bounds")
    if minimum_value is not None and maximum_value is not None and minimum_value > maximum_value:
        raise ValueError("ranking_min_raw_row_count must not exceed ranking_max_raw_row_count")
    return exact_value, minimum_value, maximum_value


def ranking_accepts_incomplete(source: Mapping[str, Any]) -> bool:
    """Return whether a short non-empty ranking is accepted as incomplete.

    Issue #668: only sources that explicitly declare
    ``ranking_accept_incomplete: true`` alongside an exact
    ``ranking_expected_raw_row_count`` opt in. Every other source keeps the
    fail-closed exact gate. The flag never excuses an empty capture or an
    over-count capture.
    """
    return source.get("ranking_accept_incomplete") is True


def ranking_incomplete_row_count(source: Mapping[str, Any], raw_row_count: int) -> bool:
    """Return whether *raw_row_count* is an accepted incomplete shortfall."""
    if not ranking_accepts_incomplete(source):
        return False
    try:
        exact, minimum, maximum = ranking_row_bounds(source)
    except ValueError:
        return False
    if exact is None or minimum is not None or maximum is not None:
        return False
    return 1 <= raw_row_count < exact


def ranking_incomplete_warning(source: Mapping[str, Any], raw_row_count: int) -> str:
    """Return the visible incomplete-coverage warning, or "" when not incomplete."""
    if not ranking_incomplete_row_count(source, raw_row_count):
        return ""
    try:
        exact, _, _ = ranking_row_bounds(source)
    except ValueError:
        return ""
    return f"incomplete ranking: exposes {raw_row_count} of {exact} expected raw rows; valid rows accepted with partial coverage"


def ranking_raw_row_count_error(source: Mapping[str, Any], raw_row_count: int) -> str:
    """Return the fail-closed error for declared raw-row bounds."""
    exact, minimum, maximum = ranking_row_bounds(source)
    if exact is not None and raw_row_count != exact:
        if ranking_incomplete_row_count(source, raw_row_count):
            return ""
        return f"ranking page exposes {raw_row_count} raw rows; expected exactly {exact}"
    if minimum is not None and raw_row_count < minimum:
        return f"ranking page exposes {raw_row_count} raw rows; expected at least {minimum}"
    if maximum is not None and raw_row_count > maximum:
        return f"ranking page exposes {raw_row_count} raw rows; expected at most {maximum}"
    return ""


def ranking_raw_row_diagnostics(
    html_content: str,
    source: Mapping[str, Any],
    *,
    detail_route: RankingDetailRoute | None = None,
    max_ranks: int = 200,
) -> dict[str, object]:
    """Return safe row-count diagnostics for a ranking page-shape failure.

    Only the bounded row count and normalized rank tokens are reported, so a
    short ranking page (Issue #665 observed 93 rows instead of 100; the
    historical cause is undetermined because the original response was not
    retained) stays diagnosable after the capture itself is discarded.
    Titles, authors, URLs, and identifiers are never included.

    Findings are computed from the complete parsed rank set; only the
    displayed lists are truncated, with explicit truncation flags.
    """
    try:
        rows = ranking_rows(html_content, dict(source), detail_route=detail_route)
    except (TypeError, ValueError):
        return {}
    observed_all: list[str] = [row.rank for row in rows if row.rank.isdigit()]
    try:
        exact, minimum, maximum = ranking_row_bounds(source)
    except ValueError:
        return {
            "raw_row_count": len(rows),
            "observed_ranks": sorted(observed_all, key=int)[:max_ranks] if observed_all else [],
            "observed_rank_count": len(observed_all),
            "observed_ranks_truncated": len(observed_all) > max_ranks,
        }
    expected = exact if exact is not None else maximum if maximum is not None else minimum
    diagnostics: dict[str, object] = {
        "raw_row_count": len(rows),
        "observed_ranks": sorted(observed_all, key=int)[:max_ranks] if observed_all else [],
        "observed_rank_count": len(observed_all),
        "observed_ranks_truncated": len(observed_all) > max_ranks,
    }
    if expected is not None and observed_all:
        seen = {int(rank) for rank in observed_all}
        missing_all = [str(rank) for rank in range(1, expected + 1) if rank not in seen]
        diagnostics["missing_ranks"] = missing_all[:max_ranks]
        diagnostics["missing_rank_count"] = len(missing_all)
        diagnostics["missing_ranks_truncated"] = len(missing_all) > max_ranks
        diagnostics["duplicate_ranks"] = len(seen) != len(observed_all)
    return diagnostics


def attach_ranking_row_diagnostics(
    debug_metadata: Mapping[str, Any],
    html_content: str,
    source: Mapping[str, Any],
    shape_error: str,
    *,
    detail_route: RankingDetailRoute | None = None,
) -> dict[str, Any]:
    """Merge safe rank diagnostics into page-shape debug metadata.

    Shared by the live collector and the replay worklist so both failure
    paths behave identically (Issue #665 review): only raw-row count
    failures are enriched, and enrichment never raises — diagnostics are
    best effort and must not mask the original failure.
    """
    merged = dict(debug_metadata)
    if "raw rows" not in shape_error:
        return merged
    try:
        merged.update(ranking_raw_row_diagnostics(html_content, source, detail_route=detail_route))
    except Exception:
        pass
    return merged


def incomplete_ranking_debug(
    html_content: str,
    source: Mapping[str, Any],
    raw_row_count: int,
    *,
    detail_route: RankingDetailRoute | None = None,
) -> dict[str, Any]:
    """Return visible incomplete-coverage metadata for an accepted shortfall.

    Issue #668: a short Cúspide capture that passes every other gate is
    collected, but it must never read as full coverage. The returned mapping
    carries the ``incomplete_ranking`` marker, the observed and expected row
    counts, the human-readable warning, and the bounded missing/duplicate-rank
    diagnostics. Best effort and never raises: call sites must not wrap it in
    their own exception handling.
    """
    try:
        warning = ranking_incomplete_warning(source, raw_row_count)
        if not warning:
            return {}
        try:
            exact, _, _ = ranking_row_bounds(source)
        except ValueError:
            return {}
        debug: dict[str, Any] = {
            "incomplete_ranking": True,
            "incomplete_ranking_warning": warning,
            "expected_raw_row_count": exact,
            "raw_row_count": raw_row_count,
        }
        try:
            debug.update(ranking_raw_row_diagnostics(html_content, source, detail_route=detail_route))
        except Exception:
            pass
        debug["raw_row_count"] = raw_row_count
        return debug
    except Exception:
        return {}


def incomplete_ranking_debug_for_page(
    source: Mapping[str, Any],
    page_data: Mapping[str, Any],
    *,
    detail_route: RankingDetailRoute | None = None,
) -> dict[str, Any]:
    """Return incomplete-coverage metadata for a validated page, or {}.

    Parses the page once with the same row selector the gates use, so the
    observed count cannot disagree with the validation decision. Returns {}
    when the page is not an accepted incomplete shortfall (full coverage,
    failed collection, unparsable rows, or a source that did not opt in).
    Never raises: call sites must not wrap it in their own exception handling.
    """
    try:
        rows = ranking_rows(str(page_data.get("html", "")), dict(source), detail_route=detail_route)
    except (TypeError, ValueError):
        return {}
    except Exception:
        return {}
    debug = incomplete_ranking_debug(str(page_data.get("html", "")), source, len(rows), detail_route=detail_route)
    return _with_skipped_detail_rows(debug, source, page_data, len(rows))


def _with_skipped_detail_rows(
    debug: dict[str, Any], source: Mapping[str, Any], page_data: Mapping[str, Any], list_row_count: int
) -> dict[str, Any]:
    """Add the rows skipped for an unreadable detail page to the coverage record (Issue #1163).

    A skipped row is partial coverage like a short list (Issue #668): the
    source reads as incomplete, its readable row count drops by the skipped
    rows, and each skipped book is named by title with the reason its page was
    lost. Never raises.
    """
    try:
        skipped = ranking_detail_skipped_entries(page_data)
        if not skipped:
            return debug
        try:
            exact, _, _ = ranking_row_bounds(source)
        except ValueError:
            exact = None
        expected = debug.get("expected_raw_row_count")
        if isinstance(expected, bool) or not isinstance(expected, int):
            expected = exact if exact is not None else list_row_count
        count = len(skipped)
        detail_warning = (
            f"incomplete detail evidence: {count} product page{'s' if count != 1 else ''} could not be read; "
            f"{'their rows were' if count != 1 else 'its row was'} skipped"
        )
        list_warning = str(debug.get("incomplete_ranking_warning", "")).strip()
        records = [
            {
                "rank": str(item.get("rank", "")),
                "title": str(item.get("title", "")),
                "detail_url": str(item.get("detail_url", "")),
                "reason": str(item.get("reason", "")),
            }
            for item in skipped
        ]
        return {
            **debug,
            "incomplete_ranking": True,
            "incomplete_ranking_warning": "; ".join(part for part in (list_warning, detail_warning) if part),
            "expected_raw_row_count": expected,
            "raw_row_count": max(list_row_count - count, 0),
            "ranking_list_row_count": list_row_count,
            "detail_pages_skipped": records,
            "detail_pages_skipped_count": count,
            "left_out_titles": [record["title"] for record in records if record["title"].strip()],
        }
    except Exception:
        return debug


def incomplete_coverage_from_run(run: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the reported coverage of a successful-but-incomplete source run.

    Shared by weekly completion, source-health, and review rendering so
    partial coverage reads identically everywhere (Issue #668): the source
    name plus the observed and expected raw-row counts, and the bounded
    ``left_out_titles`` a source names when it records them (Issue #913,
    Kirkus). Returns None for
    failed/skipped runs, runs without the incomplete marker, and malformed
    records. Never raises.
    """
    try:
        if not isinstance(run, Mapping):
            return None
        if str(run.get("status", "")) in {"failed", "skipped"}:
            return None
        debug = run.get("debug_metadata")
        if not isinstance(debug, Mapping) or debug.get("incomplete_ranking") is not True:
            return None
        observed = debug.get("raw_row_count")
        expected = debug.get("expected_raw_row_count")
        if isinstance(observed, bool) or not isinstance(observed, int):
            return None
        if isinstance(expected, bool) or not isinstance(expected, int):
            return None
        name = str(run.get("source_name", "")).strip()
        if not name:
            return None
        coverage: dict[str, Any] = {"source_name": name, "observed_rows": observed, "expected_rows": expected}
        titles = debug.get("left_out_titles")
        if isinstance(titles, list):
            named = [str(title).strip() for title in titles if isinstance(title, str) and title.strip()]
            if named:
                coverage["left_out_titles"] = named
        return coverage
    except Exception:
        return None


def left_out_titles_text(coverage: Mapping[str, Any]) -> str:
    """The Russian clause naming the books a partial source left out, or "" (Issue #913).

    Markdown and HTML reviews share it, so the same books read the same way in both.
    """
    titles = coverage.get("left_out_titles")
    if not isinstance(titles, list):
        return ""
    named = [str(title).strip() for title in titles if isinstance(title, str) and title.strip()]
    if not named:
        return ""
    return "не вошли: " + ", ".join(f"«{title}»" for title in named)


def _validate_date_format(value: str) -> str:
    if value == "iso-publication-week-wednesday":
        return value
    if not value.strip():
        raise ValueError("ranking_reporting_period_date_format must be nonblank")
    for sample in ("01/02/2026", "2026-02-01", "01.02.2026", "2026.02.01"):
        try:
            datetime.strptime(sample, value)
        except ValueError:
            continue
        return value
    raise ValueError("ranking_reporting_period_date_format is not a valid date format")


def validate_ranking_contract_metadata(source: Mapping[str, Any]) -> str:
    """Validate ranking declarations before a page is fetched or stored."""
    if source.get("metadata_ranking_table") is not True:
        return "ranking-table requires metadata_ranking_table true"
    http_error = validate_head_404_fallback_declaration(source)
    if http_error:
        return http_error
    if "ranking_author_evidence_json" in source:
        try:
            verified_author(source, "", "")
        except ValueError as exc:
            return str(exc)
    rank_source = str(source.get("ranking_rank_source", "selector")).strip().casefold()
    if rank_source not in RANKING_RANK_SOURCES:
        return "ranking_rank_source must be one of: " + ", ".join(RANKING_RANK_SOURCES)
    if rank_source == "selector" and not str(source.get("ranking_rank_selector", "")).strip():
        return "ranking-table requires ranking_rank_selector when ranking_rank_source is selector"
    detail_route_error = validate_ranking_detail_route(source)
    if detail_route_error:
        return detail_route_error
    detail_route = RankingDetailRoute.from_source(source) if ranking_detail_route_declared(source) else None
    expected_evidence_url = "detail_page" if detail_route is not None else "source_page"
    if str(source.get("ranking_evidence_url", "")).strip() != expected_evidence_url:
        return f"ranking-table requires ranking_evidence_url to be exactly '{expected_evidence_url}'"
    undated_error = validate_undated_freshness_declaration(source)
    if undated_error:
        return undated_error
    author_optional_error = validate_ranking_author_optional_declaration(source)
    if author_optional_error:
        return author_optional_error
    request_error = validate_ranking_request_declaration(source)
    if request_error:
        return request_error
    response_error = validate_ranking_response_declaration(source)
    if response_error:
        return response_error
    identity_error = validate_ranking_identity_declaration(source)
    if identity_error:
        return identity_error
    undated = ranking_undated_declared(source)
    if "ranking_reporting_date_selector" in source:
        try:
            _validate_class_token_selector(source["ranking_reporting_date_selector"], label="ranking reporting date")
        except ValueError as exc:
            return str(exc)
    pattern = str(source.get("ranking_reporting_period_pattern", "")).strip()
    if not pattern and not undated:
        return "ranking-table requires ranking_reporting_period_pattern"
    if pattern:
        try:
            re.compile(pattern)
        except re.error as exc:
            return f"ranking_reporting_period_pattern is malformed: {exc}"
    detail_fields = (
        detail_route.requested_fields if detail_route is not None else ranking_detail_requested_fields(source)
    )
    identifier_pattern = str(source.get("ranking_identifier_pattern", "")).strip()
    if identifier_pattern:
        try:
            compiled = re.compile(identifier_pattern)
        except re.error as exc:
            return f"ranking_identifier_pattern is malformed: {exc}"
        if compiled.groups == 0 and "identifier" not in compiled.groupindex:
            return "ranking_identifier_pattern must contain a capture group named identifier or one positional group"
    date_format = str(source.get("ranking_reporting_period_date_format", "")).strip()
    if (
        date_format == "iso-publication-week-wednesday"
        and not str(source.get("ranking_reporting_date_selector", "")).strip()
    ):
        return "ISO publication week requires ranking_reporting_date_selector"
    max_age = _ranking_row_bound(source, "ranking_reporting_period_max_age_days")
    if not undated:
        if not date_format:
            return "ranking-table requires ranking_reporting_period_date_format"
        try:
            _validate_date_format(date_format)
        except ValueError as exc:
            return str(exc)
        if isinstance(max_age, bool) or not isinstance(max_age, int) or max_age < 0:
            return "ranking_reporting_period_max_age_days must be a non-negative integer"
    try:
        exact, minimum, maximum = ranking_row_bounds(source)
    except ValueError as exc:
        return str(exc)
    if exact is None and minimum is None and maximum is None:
        return "ranking-table requires an expected or bounded raw row count"
    if source.get("ranking_accept_incomplete") is True:
        if exact is None or minimum is not None or maximum is not None:
            return "ranking_accept_incomplete requires ranking_expected_raw_row_count alone"
    elif "ranking_accept_incomplete" in source and source.get("ranking_accept_incomplete") not in (False, None, ""):
        return "ranking_accept_incomplete must be true or omitted"
    if "isbn" not in detail_fields and not any(
        str(source.get(field, "")).strip()
        for field in (
            "ranking_identifier_selector",
            "ranking_identifier_attribute",
            "ranking_identifier_pattern",
        )
    ):
        return "ranking-table requires one of ranking_identifier_selector, ranking_identifier_attribute, or ranking_identifier_pattern"
    if not str(source.get("ranking_category_marker", "")).strip():
        return "ranking-table requires ranking_category_marker"
    exclusions = source.get("adult_nonfiction_exclusions")
    if "category" in detail_fields:
        if exclusions is not None and (
            not isinstance(exclusions, list)
            or any(not isinstance(value, str) or not value.strip() for value in exclusions)
        ):
            return "ranking-table adult_nonfiction_exclusions must contain strings when supplied"
    elif (
        not isinstance(exclusions, list)
        or not exclusions
        or any(not isinstance(value, str) or not value.strip() for value in exclusions)
    ):
        return "ranking-table requires adult_nonfiction_exclusions with non-empty terms"
    expected_path = str(source.get("ranking_expected_path", "")).strip()
    if expected_path:
        parsed = urlsplit(str(source.get("url", "")))
        if (
            not expected_path.startswith("/")
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") != expected_path.rstrip("/")
        ):
            return "ranking_expected_path must match the configured source URL path"
    return ""


def _reporting_period_dates(period: str, date_format: str, *, single_date: bool = False) -> tuple[date, date] | None:
    if date_format == "iso-publication-week-wednesday":
        if not single_date:
            return None
        match = re.fullmatch(r"Week ([0-9]{1,2}) - ([0-9]{4})", period.strip())
        if match is None:
            return None
        try:
            publication_date = date.fromisocalendar(int(match.group(2)), int(match.group(1)), 3)
        except ValueError:
            return None
        return publication_date, publication_date
    tokens = re.findall(r"(?<!\d)\d{1,4}(?:[./-]\d{1,4}){2}(?!\d)", period)
    if single_date:
        if len(tokens) != 1:
            return None
        tokens = tokens * 2
    if len(tokens) != 2:
        return None
    try:
        return tuple(datetime.strptime(token, date_format).date() for token in tokens)  # type: ignore[return-value]
    except ValueError:
        return None


def ranking_freshness_declared(source: Mapping[str, Any]) -> bool:
    """Return whether a ranking source declares date-based freshness."""
    return bool(
        str(source.get("ranking_reporting_period_pattern", "") or source.get("reporting_period_pattern", "")).strip()
        and str(source.get("ranking_reporting_period_date_format", "")).strip()
    )


def _first_heading_stating(headings: tuple[str, ...], category: str) -> str:
    """Return the first heading that states *category*, or an empty string."""
    marker = normalize_visible_text(category)
    if not marker:
        return ""
    for heading in headings:
        if marker in normalize_visible_text(heading):
            return heading
    return ""


def ranking_page_identity(
    html_content: str,
    source: dict[str, Any],
    reference_date: date | None = None,
    identity_html: str = "",
) -> RankingPageIdentity | None:
    """Extract declared ranking identity from visible first-party page text.

    Category and reporting period are page-level facts.  They are not read
    from a row and cannot be supplied by an extraction response.  A declared
    regex must contain one named ``period`` group or one ordinary capture; the
    captured text is retained exactly after whitespace normalization.

    A declared visible title selector is authoritative for the selected
    capture: if it does not resolve, this function refuses the ranking rather
    than falling back to an h1-h6 heading. A source that declares an identity
    route reads its heading from that page's capture instead of the rows body,
    because a form route returns rows that state nothing about which list they are.
    The heading is held to the same category and reporting-period checks either
    way. A declared route with no captured page yields no identity and does not
    fall back to unidentified rows.
    """
    identity_route = ranking_identity_declared(source)
    heading_html = html_content
    if identity_route:
        if not isinstance(identity_html, str) or not identity_html.strip():
            return None
        heading_html = identity_html
    identity = parse_visible_page_identity(heading_html, str(source.get("visible_title_selector", "")))
    if identity.failure_cause is not None or not identity.identity_texts:
        return None
    category = str(source.get("ranking_category_marker") or source.get("expected_category") or "").strip()
    if identity_route:
        # An identity page is a whole page, not a bare fragment, so its first
        # heading may be chrome such as a search-overlay label. The route
        # exists to find the statement of the list, so the heading that names
        # the declared category is the one read, and a page with no such
        # heading yields no identity.
        heading = _first_heading_stating(identity.identity_texts, category)
        if not heading:
            return None
    else:
        heading = identity.identity_texts[0]
    normalized_heading = normalize_visible_text(heading)
    if category and normalize_visible_text(category) not in normalized_heading:
        return None
    pattern_value = str(
        source.get("ranking_reporting_period_pattern") or source.get("reporting_period_pattern") or ""
    ).strip()
    reporting_period = ""
    date_selector = str(source.get("ranking_reporting_date_selector", "")).strip()
    period_text = heading
    if date_selector:
        date_identity = parse_visible_page_identity(heading_html, date_selector)
        if date_identity.failure_cause is not None or not date_identity.identity_texts:
            return None
        period_text = date_identity.identity_texts[0]
    if pattern_value:
        try:
            match = re.search(pattern_value, period_text, flags=re.IGNORECASE)
        except re.error:
            return None
        if match is None:
            return None
        if "period" in match.groupdict():
            reporting_period = match.group("period") or ""
        elif match.lastindex:
            reporting_period = match.group(1) or ""
        else:
            reporting_period = match.group(0)
    if pattern_value and not reporting_period.strip():
        return None
    date_format = str(source.get("ranking_reporting_period_date_format", "")).strip()
    if date_format:
        try:
            _validate_date_format(date_format)
        except ValueError:
            return None
        parsed_dates = _reporting_period_dates(reporting_period, date_format, single_date=bool(date_selector))
        if parsed_dates is None:
            return None
        if date_selector and _reporting_period_dates(period_text, date_format, single_date=True) != parsed_dates:
            return None
        period_start, period_end = parsed_dates
        if period_start > period_end:
            return None
        if reference_date is not None:
            if period_end > reference_date:
                return None
            max_age = _ranking_row_bound(source, "ranking_reporting_period_max_age_days")
            if isinstance(max_age, int) and not isinstance(max_age, bool):
                if (reference_date - period_end).days > max_age:
                    return None
    return RankingPageIdentity(
        heading=heading,
        category=category,
        reporting_period=_normalize_whitespace(reporting_period),
    )


def _ranking_author_display_key(value: str) -> str:
    """Normalize only known author display separators for declarative rows."""
    normalized = normalize_visible_text(value)
    normalized = re.sub(r"\s*(?:/|&|;)\s*", "|", normalized)
    normalized = re.sub(r"\s+\b(?:and|e)\b\s+", "|", normalized)
    return normalized


def ranking_rows_after_title_exclusions(
    rows: list[RankingRow], source: Mapping[str, Any], *, route: RankingDetailRoute | None = None
) -> list[RankingRow]:
    """Apply the shared pre-detail title exclusion policy in row order."""
    detail_fields = route.requested_fields if route is not None else ranking_detail_requested_fields(source)
    if "category" in detail_fields:
        return list(rows)
    exclusions = source.get("adult_nonfiction_exclusions", [])
    normalized_exclusions = [normalize_visible_text(value) for value in exclusions if isinstance(value, str)]
    return [
        row
        for row in rows
        if not any(term and term in normalize_visible_text(row.title) for term in normalized_exclusions)
    ]


def validate_ranking_detail_row_urls(rows: list[RankingRow]) -> str:
    """Validate row detail-link errors and duplicate URL identities once."""
    seen_urls: set[str] = set()
    for row in rows:
        if row.detail_link_error:
            return row.detail_link_error
        if not row.detail_url:
            return "ranking row detail link is missing"
        identity = normalize_source_identity(row.detail_url)
        if identity in seen_urls:
            return "ranking rows contain duplicate detail URL"
        seen_urls.add(identity)
    return ""


def _ranking_detail_rows_for_fetch(
    source: Mapping[str, Any], html_content: str, route: RankingDetailRoute | None = None
) -> list[RankingRow]:
    rows = ranking_rows(html_content, dict(source), detail_route=route)
    return ranking_rows_after_title_exclusions(rows, source, route=route)


def ranking_detail_fetch_plan(
    source: Mapping[str, Any], html_content: str, *, route: RankingDetailRoute | None = None
) -> tuple[list[RankingRow], str]:
    """Validate cheap list fields and return surviving rows before detail I/O."""
    if route is None:
        try:
            route = RankingDetailRoute.from_source(source)
        except (TypeError, ValueError, re.error) as exc:
            return [], str(exc)
    rows = ranking_rows(html_content, dict(source), detail_route=route)
    detail_fields = route.requested_fields if route is not None else ranking_detail_requested_fields(source)
    try:
        row_count_error = ranking_raw_row_count_error(source, len(rows))
    except ValueError as exc:
        return [], str(exc)
    if row_count_error:
        return [], row_count_error
    for row in rows:
        required_row_fields: tuple[tuple[str, str], ...] = (("rank", row.rank), ("title", row.title))
        if ranking_row_author_required(source):
            required_row_fields += (("author", row.author),)
        missing = [field for field, value in required_row_fields if not value]
        if "isbn" not in detail_fields and not row.identifier:
            missing.append("identifier")
        if missing:
            return [], "ranking row missing " + ", ".join(missing)
    row_url_error = validate_ranking_detail_row_urls(rows)
    if row_url_error:
        return [], row_url_error
    surviving = _ranking_detail_rows_for_fetch(source, html_content, route)
    if route is not None:
        page_error = route.validate_page_count(len(surviving))
        if page_error:
            return [], page_error
    return surviving, ""


# Issue #1163: the rows whose detail page one capture could not read. Each item
# names the row (rank, title, detail URL) and the bounded reason; validation
# accepts at most the route's declared ``ranking_detail_max_failed_pages``.
RANKING_DETAIL_SKIPPED_FIELD = "detail_pages_skipped"
MAX_DETAIL_SKIP_REASON_CHARS = 300


def ranking_detail_skipped_entry(row: RankingRow, reason: str) -> dict[str, str]:
    """Record one row skipped because its detail page could not be read (Issue #1163)."""
    return {
        "rank": row.rank,
        "title": row.title,
        "detail_url": row.detail_url,
        "reason": " ".join(reason.split())[:MAX_DETAIL_SKIP_REASON_CHARS] or "detail page could not be read",
    }


def ranking_detail_skipped_entries(page_data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return the skipped-row records a capture carries, or [] when it carries none."""
    value = page_data.get(RANKING_DETAIL_SKIPPED_FIELD)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _ranking_detail_rows_with_pages(
    rows: list[RankingRow], raw_skipped: object, route: RankingDetailRoute
) -> tuple[list[RankingRow], str]:
    """Return the surviving rows that must carry a detail page, after the declared skips.

    A capture without the field skips nothing, as before. Each skip must name
    exactly one surviving row, once, with its own rank and title and a stated
    reason; more skips than the route declares, or a skip of every row, fail.
    """
    if raw_skipped is None:
        return rows, ""
    if not isinstance(raw_skipped, list):
        return [], "ranking detail skipped pages must be a list"
    if not raw_skipped:
        return rows, ""
    if len(raw_skipped) > route.max_failed_pages:
        return [], (
            f"ranking detail evidence skips {len(raw_skipped)} pages but "
            f"ranking_detail_max_failed_pages is {route.max_failed_pages}"
        )
    rows_by_identity = {normalize_source_identity(row.detail_url): row for row in rows}
    skipped_identities: set[str] = set()
    for item in raw_skipped:
        if not isinstance(item, Mapping):
            return [], "ranking detail skipped page must be an object"
        try:
            identity = normalize_source_identity(str(item.get("detail_url", "")).strip())
        except ValueError:
            return [], "ranking detail skipped page does not match a surviving ranking row"
        row = rows_by_identity.get(identity)
        if row is None:
            return [], "ranking detail skipped page does not match a surviving ranking row"
        if identity in skipped_identities:
            return [], "ranking detail skipped pages contain a duplicate detail URL"
        if item.get("rank") != row.rank or item.get("title") != row.title:
            return [], "ranking detail skipped page does not match its list row"
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_DETAIL_SKIP_REASON_CHARS:
            return [], "ranking detail skipped page must state a bounded reason"
        skipped_identities.add(identity)
    if len(skipped_identities) >= len(rows):
        return [], "ranking detail evidence skips every surviving ranking row"
    return [row for row in rows if normalize_source_identity(row.detail_url) not in skipped_identities], ""


def validate_ranking_detail_pages(
    source: Mapping[str, Any], page_data: Mapping[str, Any], *, route: RankingDetailRoute | None = None
) -> str:
    """Validate the deterministic detail-page evidence bound to one list page.

    Every surviving row carries its detail page, except the rows the capture
    names under ``detail_pages_skipped``, within the route's declared
    ``ranking_detail_max_failed_pages`` (Issue #1163).
    """
    route_declared = route is not None or ranking_detail_route_declared(source)
    raw_pages = page_data.get("detail_pages")
    if not route_declared:
        if raw_pages not in (None, [], ()):  # A detail capture without a declaration is unsafe.
            return "ranking detail pages are present without a declared detail route"
        if page_data.get(RANKING_DETAIL_SKIPPED_FIELD) not in (None, [], ()):
            return "ranking detail skipped pages are present without a declared detail route"
        return ""
    html_content = str(page_data.get("html", ""))
    if route is None:
        try:
            route = RankingDetailRoute.from_source(source)
        except (TypeError, ValueError, re.error) as exc:
            return str(exc)
    all_rows = ranking_rows(html_content, dict(source), detail_route=route)
    row_url_error = validate_ranking_detail_row_urls(all_rows)
    if row_url_error:
        return row_url_error
    rows = _ranking_detail_rows_for_fetch(source, html_content, route)
    try:
        row_count_error = ranking_raw_row_count_error(source, len(all_rows))
    except ValueError as exc:
        return str(exc)
    if row_count_error:
        return row_count_error
    if route is None:
        return "ranking detail route is not declared"
    page_error = route.validate_page_count(len(rows))
    if page_error:
        return page_error
    rows, skip_error = _ranking_detail_rows_with_pages(rows, page_data.get(RANKING_DETAIL_SKIPPED_FIELD), route)
    if skip_error:
        return skip_error
    if not isinstance(raw_pages, list):
        return "ranking detail evidence must contain a detail_pages list"
    if len(raw_pages) != len(rows):
        return f"ranking detail evidence count {len(raw_pages)} does not match surviving ranking rows {len(rows)}"
    seen_urls: set[str] = set()
    max_response_bytes = source.get("max_response_bytes", 750_000)
    for row, raw_page in zip(rows, raw_pages, strict=True):
        if not isinstance(raw_page, Mapping):
            return "ranking detail evidence item must be an object"
        detail_url = str(raw_page.get("detail_url", "")).strip()
        resolved_url = str(raw_page.get("resolved_url", "")).strip()
        source_page = str(raw_page.get("source_page", "")).strip()
        if not route or not route.same_url(source_page, detail_url):
            return "ranking detail evidence source_page must be the requested detail URL"
        if not route.same_url(detail_url, row.detail_url):
            return "ranking detail evidence detail_url does not match its list row"
        if not route.same_url(resolved_url, detail_url):
            return "ranking detail evidence was redirected or resolved to a different URL"
        detail_identity = normalize_source_identity(detail_url)
        if detail_identity in seen_urls:
            return "ranking detail evidence contains a duplicate detail URL"
        seen_urls.add(detail_identity)
        linked_url, link_error = _ranking_detail_url(source, [detail_url], route)
        if link_error or not route.same_url(linked_url, detail_url):
            return link_error or "ranking detail evidence URL has the wrong shape"
        detail_html = raw_page.get("html")
        if not isinstance(detail_html, str) or not detail_html.strip():
            return "ranking detail evidence HTML is missing or empty"
        if isinstance(max_response_bytes, int) and len(detail_html.encode("utf-8")) > max_response_bytes:
            return "ranking detail evidence exceeds the response-size limit"
        scope = ranking_detail_scope(detail_html, str(source.get("ranking_detail_scope_selector", "")))
        if scope.failure_reason:
            return f"ranking detail scope validation failed: {scope.failure_reason}"
    return ""


def ranking_mention_is_grounded(
    html_content: str,
    *,
    title: str,
    isbn: str,
    current_rank: str,
    source_week: str,
    primary_author: str | None = None,
    primary_authors: list[str] | None = None,
    entry_scope_selector: str = "",
    ranking_source_statement: str | None = None,
    ranking_source: dict[str, Any] | None = None,
    detail_pages: list[Mapping[str, Any]] | None = None,
    evidence_url: str = "",
    candidate_category: str = "",
    detail_route: RankingDetailRoute | None = None,
    identity_html: str = "",
    parsed: ParsedCapture | None = None,
) -> bool:
    """Ground ranking fields in one bounded row and week in its source statement.

    Declarative ranking rows compare the complete ordered author display.
    Generic ranking tables preserve the established first-author containment
    behavior. *parsed*, when given, is the pass's capture of *html_content*;
    its detail and identity pages are read through it too (Issue #718).
    """
    page = capture_for(html_content, None, parsed)
    authors = primary_authors if primary_authors is not None else ([primary_author] if primary_author else [])
    author_values = [str(author).strip() for author in authors if str(author).strip()]
    generic_author = author_values[0] if author_values else ""
    wanted = [normalize_visible_text(value) for value in (title, generic_author)]
    rank = normalize_visible_text(current_rank)
    declarative_author = _ranking_author_display_key(" / ".join(author_values)) if author_values else ""
    row_matches = False
    declarative_source = ranking_source or {}
    if declarative_source.get("ranking_row_selector"):
        if _ranking_rank_source(declarative_source) == "":
            return False
        detail_route = detail_route or RankingDetailRoute.from_source(declarative_source)
        wanted_identifier = ranking_entry_isbn(isbn)
        if not wanted_identifier:
            return False
        for row in page.ranking_rows(declarative_source, detail_route):
            list_fields_match = (
                normalize_visible_text(row.rank) == rank
                and normalize_visible_text(row.title) == wanted[0]
                and _ranking_author_display_key(row.author) == declarative_author
                and not row.detail_link_error
            )
            if not list_fields_match:
                continue
            if detail_route:
                if not row.detail_url or not normalize_source_identity(evidence_url) == normalize_source_identity(
                    row.detail_url
                ):
                    continue
                if not detail_route.has_isbn and row.identifier != wanted_identifier:
                    continue
            elif row.identifier != wanted_identifier:
                continue
            row_matches = True
            break
    elif entry_scope_selector:
        if not rank:
            return False
        scope = page.entry_scope(entry_scope_selector)
        if scope.failure_cause is not None:
            return False
        row_texts = [normalize_visible_text(row) for row in page.entry_rows(entry_scope_selector)]
        for row_text in row_texts:
            if not row_text or re.match(rf"^{re.escape(rank)}(?:\b|[.)])", row_text) is None:
                continue
            if all(value and value in row_text for value in wanted) and _row_states_isbn(row_text, isbn):
                row_matches = True
                break
    else:
        for cells in page.visible_text().table_rows:
            if not cells or normalize_visible_text(cells[0]) != rank:
                continue
            row_text = normalize_visible_text(" ".join(cells))
            if all(value and value in row_text for value in wanted) and _row_states_isbn(row_text, isbn):
                row_matches = True
                break
    if not row_matches:
        return False
    if detail_route is not None:
        if not detail_pages or not evidence_url.strip():
            return False
        matching_pages = [
            page
            for page in detail_pages
            if normalize_source_identity(str(page.get("source_page", "")).strip())
            == normalize_source_identity(evidence_url)
            and normalize_source_identity(str(page.get("detail_url", page.get("resolved_url", ""))).strip())
            == normalize_source_identity(evidence_url)
            and normalize_source_identity(str(page.get("resolved_url", "")).strip())
            == normalize_source_identity(evidence_url)
        ]
        if len(matching_pages) != 1:
            return False
        values, detail_error = page.subpage(str(matching_pages[0].get("html", ""))).ranking_detail_fields(detail_route)
        if detail_error:
            return False
        if detail_route.has_isbn and wanted_identifier != values.get("isbn", ""):
            return False
        if detail_route.has_category:
            expected_category = normalize_visible_text(str(declarative_source.get("eligible_candidate_category", "")))
            if not expected_category or normalize_visible_text(candidate_category) != expected_category:
                return False
    visible = normalize_visible_text(" ".join(page.visible_text().visible_parts))
    week = normalize_visible_text(source_week)
    if declarative_source.get("ranking_row_selector"):
        identity = page.ranking_page_identity(declarative_source, identity_html)
        if identity is None or not week:
            return False
        if identity.reporting_period and normalize_visible_text(identity.reporting_period) != week:
            return False
        statement_text = str(declarative_source.get("ranking_source_statement", "")).strip()
        if not statement_text:
            return True
    else:
        statement_text = "Source: Circana BookScan" if ranking_source_statement is None else ranking_source_statement
    statement = normalize_visible_text(statement_text)
    if not statement or not week:
        return False
    statement_start = visible.find(statement)
    bounded_statement = visible[statement_start : statement_start + 300] if statement_start >= 0 else ""
    return bool(bounded_statement and week in bounded_statement)


def _page_reference_date_result(page_data: Mapping[str, Any]) -> tuple[date | None, str]:
    """Return the page reference date, and the field name if one is unreadable.

    Absent and unreadable are different facts and must not collapse into one
    ``None`` (Issue #599).  Every staleness comparison in
    :func:`ranking_page_identity` sits behind ``reference_date is not None``, so
    a caller that cannot tell the two apart silently stops ageing the page:
    ``ranking_reporting_period_max_age_days`` reads like a guarantee and
    enforces nothing.  That is how the Audible.de chart in #597 rested on
    evidence frozen at 2026-08-04 and still looked current.

    A stated value the system cannot parse is the worse case of the two,
    because the page claims a date and the claim is unusable.  It is reported
    so the caller can refuse, never swallowed.
    """
    for field_name in ("reference_date", "run_date"):
        value = page_data.get(field_name)
        if isinstance(value, date) and not isinstance(value, datetime):
            return value, ""
        if isinstance(value, str) and value.strip():
            try:
                return date.fromisoformat(value.strip()), ""
            except ValueError:
                return None, field_name
    return None, ""


BETWEEN_EDITIONS_HEADING_FIELD = "between_editions_page_heading"
BETWEEN_EDITIONS_REASON_FIELD = "between_editions_reason"
BETWEEN_EDITIONS_SKIP_PREFIX = "between editions: "
BETWEEN_EDITIONS_SAFE_STATUS = "between_editions"


def validate_between_editions_declaration(source: Mapping[str, Any]) -> str:
    """Return an error naming an unusable between-editions declaration, or ``""``.

    The declaration replaces a page-shape failure with a named skip, so it is
    held to what makes that safe: a heading to recognise the state by, a
    recorded reason for the run record, an expected heading it stands in for,
    and a heading that differs from the expected one, since a state that
    cannot be told from the current edition is not a state.
    """
    declared = [field for field in (BETWEEN_EDITIONS_HEADING_FIELD, BETWEEN_EDITIONS_REASON_FIELD) if field in source]
    if not declared:
        return ""
    for field in (BETWEEN_EDITIONS_HEADING_FIELD, BETWEEN_EDITIONS_REASON_FIELD):
        value = source.get(field)
        if not isinstance(value, str) or not value.strip():
            return f"{BETWEEN_EDITIONS_HEADING_FIELD} and {BETWEEN_EDITIONS_REASON_FIELD} must both be nonblank text"
    expected_heading = str(source.get("expected_page_heading", "")).strip()
    if not expected_heading:
        return f"{BETWEEN_EDITIONS_HEADING_FIELD} requires expected_page_heading"
    if normalize_visible_text(expected_heading) == normalize_visible_text(str(source[BETWEEN_EDITIONS_HEADING_FIELD])):
        return f"{BETWEEN_EDITIONS_HEADING_FIELD} must differ from expected_page_heading"
    return ""


def between_editions_reason(source: Mapping[str, Any], page_data: Mapping[str, Any]) -> str:
    """Return the declared reason when the page shows the between-editions state.

    An annual page spends months between editions: the last edition has left
    the page and the next is not published. The expected heading is then
    legitimately absent, and reporting that as a failure every week says
    something is broken when nothing is (Issue #761).

    The state is recognised only by evidence on the page. The current-edition
    heading must be absent and the declared between-editions heading present,
    both matched exactly against the same visible headings the page-shape
    check reads. The current edition wins when both appear, and a page showing
    neither still fails page-shape validation, so the declaration cannot hide
    a page that changed in some other way. Nothing is dated: the source
    collects again on the first run that finds the expected heading.
    """
    if validate_between_editions_declaration(source) or BETWEEN_EDITIONS_HEADING_FIELD not in source:
        return ""
    identity = parse_visible_page_identity(
        str(page_data.get("html", "")), str(source.get("visible_title_selector", ""))
    )
    if identity.failure_cause is not None:
        return ""
    headings = {normalize_visible_text(heading) for heading in identity.identity_texts}
    if normalize_visible_text(str(source.get("expected_page_heading", ""))) in headings:
        return ""
    if normalize_visible_text(str(source[BETWEEN_EDITIONS_HEADING_FIELD])) not in headings:
        return ""
    return str(source[BETWEEN_EDITIONS_REASON_FIELD]).strip()


def validate_page_shape(
    source: dict[str, Any],
    page_data: dict[str, Any],
    *,
    reference_date: date | None = None,
    detail_route: RankingDetailRoute | None = None,
) -> str:
    """Return an error unless configured identity and table markers are present."""
    if str(source.get("contract_family", "")).strip().casefold() == "publisher-frontlist":
        from autoscout.publisher_frontlist import frontlist_cards as publisher_frontlist_cards

        try:
            publisher_frontlist_cards(str(page_data.get("html", "")), str(source.get("url", "")))
        except ValueError as exc:
            return f"page shape validation failed: {exc}"
    if str(source.get("contract_family", "")).strip().casefold() == "nuevoslibros-frontlist":
        from autoscout.nuevoslibros_frontlist import frontlist_cards

        try:
            frontlist_cards(str(page_data.get("html", "")), str(source.get("url", "")))
        except ValueError as exc:
            return f"page shape validation failed: {exc}"
    ranking_contract = bool(str(source.get("ranking_row_selector", "")).strip()) and (
        str(source.get("contract_family", "")).strip().casefold() == "ranking-table"
        or source.get("metadata_ranking_table") is True
    )
    if source.get("amazon_charts_weekly") is True:
        html_content = str(page_data.get("html", ""))
        expected_heading = str(source.get("expected_page_heading", "")).strip()
        if (
            not expected_heading
            or normalize_visible_text(expected_heading) not in page_title_text(html_content).casefold()
        ):
            return "page shape validation failed: Amazon Charts page title is not the configured heading"
        ranks = amazon_charts_ranks(html_content, str(page_data.get("source_page", "")))
        if sorted(ranks) != list(range(1, 21)):
            return "page shape validation failed: Amazon Charts chart must contain exact ranks 1 through 20 once each"
        return ""
    expected_heading = str(source.get("expected_page_heading", "")).strip()
    expected_sort = str(source.get("expected_sort_label", "")).strip()
    expected_range = str(source.get("expected_range_marker", "")).strip()
    raw_headers = source.get("expected_table_headers", [])
    expected_headers = [str(value).strip() for value in raw_headers] if isinstance(raw_headers, list) else []
    raw_markers = source.get("expected_visible_markers", [])
    expected_markers = [str(value).strip() for value in raw_markers] if isinstance(raw_markers, list) else []
    raw_forbidden = source.get("forbidden_visible_markers", [])
    forbidden_markers = [str(value).strip() for value in raw_forbidden] if isinstance(raw_forbidden, list) else []
    if (
        not ranking_contract
        and not expected_heading
        and not expected_sort
        and not expected_range
        and not expected_headers
        and not expected_markers
        and not forbidden_markers
        and not str(source.get("entry_section_heading", "")).strip()
        and str(source.get("contract_family", "")).strip().casefold() != "award-list"
    ):
        return ""

    parsed = _parse(str(page_data.get("html", "")))
    if expected_heading and not ranking_contract:
        normalized = normalize_visible_text(expected_heading)
        identity = parse_visible_page_identity(
            str(page_data.get("html", "")), str(source.get("visible_title_selector", ""))
        )
        if identity.failure_cause is not None or not any(
            normalized == normalize_visible_text(heading) for heading in identity.identity_texts
        ):
            return f"page shape validation failed: expected page heading '{expected_heading}' not found"
    if forbidden_markers:
        # Stage-exclusivity guard: a newer award stage coexisting on the page
        # (e.g. a shortlist section added beside the longlist) must fail
        # closed instead of collecting stale rows under the old signal
        # strength. Matching is exact per-heading, so non-heading prose that
        # names a future stage (key dates, standfirsts) never trips it.
        # Forbidden headings are parsed independently across all visible
        # h1-h6: reusing visible_title_selector here would fail open when the
        # selector matches only the page title and a separate forbidden
        # section heading coexists elsewhere on the page.
        headings = parsed.headings
        if not headings:
            # Fail closed: without readable headings the absence of a newer
            # stage cannot be verified — a heading-less page may not silently
            # pass a declared forbidden-marker contract
            # (docs/architecture.md).
            return "page shape validation failed: forbidden visible markers cannot be verified (no page headings)"
        forbidden_found = [
            marker
            for marker in forbidden_markers
            if any(normalize_visible_text(marker) == normalize_visible_text(heading) for heading in headings)
        ]
        if forbidden_found:
            return "page shape validation failed: forbidden visible markers found: " + ", ".join(forbidden_found)
    entry_section_heading = str(source.get("entry_section_heading", "")).strip()
    if entry_section_heading and entry_section_html(str(page_data.get("html", "")), entry_section_heading) is None:
        return f"page shape validation failed: entry section '{entry_section_heading}' not found exactly once"
    visible_text = _joined_visible_text(parsed)
    if expected_sort and normalize_visible_text(expected_sort) not in visible_text:
        return f"page shape validation failed: expected sort label '{expected_sort}' not found"
    if expected_range and normalize_visible_text(expected_range) not in visible_text:
        return f"page shape validation failed: expected range marker '{expected_range}' not found"
    if source.get("audible_explicit_chart") is True:
        ranks = audible_chart_ranks(str(page_data.get("html", "")), str(page_data.get("source_page", "")))
        if sorted(ranks) != list(range(1, 21)):
            return "page shape validation failed: Audible chart must contain exact ranks 1 through 20 once each"
    missing_markers = [marker for marker in expected_markers if normalize_visible_text(marker) not in visible_text]
    if missing_markers:
        return f"page shape validation failed: expected visible markers not found: {', '.join(missing_markers)}"
    if expected_headers:
        header_match = any(
            all(
                any(normalize_visible_text(header) in normalize_visible_text(cell) for cell in row)
                for header in expected_headers
            )
            for row in parsed.table_rows
        )
        if not header_match:
            return "page shape validation failed: expected headers not found in one table row"
    source_week_error = source_week_freshness_error(source, " ".join(parsed.visible_parts), reference_date)
    if source_week_error:
        return f"page shape validation failed: {source_week_error}"
    if ranking_contract:
        page_reference_date, unreadable_field = _page_reference_date_result(page_data)
        if unreadable_field:
            return f"page shape validation failed: the captured page states a {unreadable_field} the system cannot read"
        ranking_reference_date = reference_date or page_reference_date
        # A declared date format means the reporting period can be aged, so it
        # must be aged. Ageing is the only thing that stops a frozen page from
        # validating as current for ever, and it needs a reference date to
        # compare against (Issue #599). The probe path already refuses here, at
        # source_feasibility.py, and this path now agrees with it rather than
        # holding a second opinion about the same rule.
        if str(source.get("ranking_reporting_period_date_format", "")).strip() and ranking_reference_date is None:
            return (
                "page shape validation failed: "
                "ranking reporting-period freshness requires a reference date and the capture carries none"
            )
        identity_html = page_data.get("identity_html", "")
        if identity_html is None:
            identity_html = ""
        if not isinstance(identity_html, str):
            return "page shape validation failed: identity_html must be text"
        if ranking_identity_declared(source) and not identity_html.strip():
            return "page shape validation failed: ranking identity route requires identity_html"
        if (
            ranking_page_identity(
                str(page_data.get("html", "")),
                source,
                reference_date=ranking_reference_date,
                identity_html=identity_html,
            )
            is None
        ):
            return "page shape validation failed: ranking category or reporting period is not current"
        rows = ranking_rows(str(page_data.get("html", "")), source, detail_route=detail_route)
        if not rows:
            return "page shape validation failed: ranking row selector matched no visible rows"
        try:
            row_count_error = ranking_raw_row_count_error(source, len(rows))
        except ValueError as exc:
            return f"page shape validation failed: {exc}"
        if row_count_error:
            return f"page shape validation failed: {row_count_error}"
    if str(source.get("contract_family", "")).strip().casefold() == "award-list":
        from autoscout.award_list import AwardListError
        from autoscout.award_list import award_page
        from autoscout.award_list import page_identity

        page_reference_date, unreadable_field = _page_reference_date_result(page_data)
        if unreadable_field:
            return f"page shape validation failed: the captured page states a {unreadable_field} the system cannot read"
        # The query selects the list, so a capture of ``?category=fiction``
        # is not the configured ``?category=nonfiction`` page.
        configured_page = page_identity(str(source.get("url", "")))
        for field in ("source_page", "resolved_url"):
            captured_url = str(page_data.get(field) or "").strip()
            if captured_url and page_identity(captured_url) != configured_page:
                return f"page shape validation failed: captured {field} is not the configured award page"
        try:
            award_page(str(page_data.get("html", "")), source, reference_date=reference_date or page_reference_date)
        except AwardListError as exc:
            return f"page shape validation failed: {exc}"
    return ""
