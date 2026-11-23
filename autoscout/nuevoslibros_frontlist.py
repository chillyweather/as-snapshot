"""Opt-in grounding for NuevosLibros' dated nonfiction cards (#901, #902).

The page interleaves publisher navigation and genre links with its book
cards. This adapter identifies the declared cards; the ordinary agent still
performs extraction and eligibility decisions. No network or model calls.
"""

from __future__ import annotations

import html
import re
from calendar import monthrange
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from datetime import timedelta
from functools import lru_cache
from html.parser import HTMLParser
from urllib.parse import urljoin
from urllib.parse import urlsplit

from autoscout.author_admission import admitted_author_people
from autoscout.source_page_identity import _VOID_TAGS
from autoscout.source_page_identity import _pop_visibility
from autoscout.source_page_identity import _push_visibility
from autoscout.source_page_identity import _VisibilityFrame
from autoscout.source_page_identity import normalize_visible_text
from autoscout.source_page_identity import parse_heading_texts

FAMILY = "nuevoslibros-frontlist"
GENRES = {
    "ensayo": "ensayo",
    "historia": "historia",
    "politica-y-sociedad": "política y sociedad",
    "biografia-y-memorias": "biografía y memorias",
    "divulgacion": "divulgación",
}
MAX_CARDS = 500
# A weekly extraction covers the cards dated this many days before the
# capture's reference date, or later (#993).
RECENT_WINDOW_DAYS = 14
# The empty_reason both extraction routes state for a capture with no recent card (#993).
# Such a capture is reported by name as contributing nothing; it is not a successful empty week.
NO_RECENT_CARD_REASON = "The capture holds no card dated within the last 14 days before its reference date or later."


@dataclass(frozen=True)
class BookCard:
    title: str
    authors: tuple[str, ...]
    url: str
    release_date: str


@dataclass
class _Card:
    release_date: str
    titles: list[tuple[str, str]] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)


class _Parser(HTMLParser):
    def __init__(self, source_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source_url = source_url
        self.stack: list[tuple[str, bool]] = []
        self.visibility: list[_VisibilityFrame] = []
        self.main_depth: int | None = None
        self.card_depth: int | None = None
        self.anchor_depth: int | None = None
        self.anchor: tuple[str, str] | None = None
        self.parts: list[str] = []
        self.card: _Card | None = None
        self.cards: list[BookCard] = []
        self.main_count = 0
        self.body_genres: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        hidden = _push_visibility(self.visibility, tag, attrs)
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        chrome = tag in {"nav", "aside", "footer"} or bool(self.stack and self.stack[-1][1])
        if tag == "body" and not hidden:
            self.body_genres.append(a.get("data-genero") or "")
        if tag == "main" and "main" in classes and not hidden and not chrome:
            self.main_count += 1
            if self.main_depth is not None:
                raise ValueError("nested frontlist main")
            self.main_depth = len(self.stack)
        if self.main_depth is not None and not hidden and not chrome:
            if tag == "article" and "book" in classes:
                if self.card is not None:
                    raise ValueError("nested frontlist card")
                self.card = _Card(a.get("data-fecha") or "")
                self.card_depth = len(self.stack)
            if tag == "a" and self.card is not None:
                if self.anchor_depth is not None:
                    raise ValueError("nested frontlist anchor")
                href = a.get("href") or ""
                if "t" in classes:
                    self.anchor = ("title", href)
                elif urlsplit(href).path.startswith("/autor/"):
                    self.anchor = ("author", href)
                else:
                    self.anchor = None
                self.anchor_depth = len(self.stack)
                self.parts = []
        if tag not in _VOID_TAGS:
            self.stack.append((tag, chrome))

    def handle_data(self, data: str) -> None:
        if self.anchor is not None and self.visibility and not self.visibility[-1].hidden:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        # The source closes card and field tags explicitly. Refuse malformed
        # card markup instead of lending another card its author or link.
        _pop_visibility(self.visibility, tag)
        if tag in _VOID_TAGS:
            return
        if not self.stack or self.stack[-1][0] != tag:
            if self.card is not None:
                raise ValueError("unbalanced frontlist card markup")
            return
        depth = len(self.stack) - 1
        if self.anchor_depth == depth and tag == "a":
            if self.anchor is not None and self.card is not None:
                kind, href = self.anchor
                text = " ".join(" ".join(self.parts).split())
                if kind == "title":
                    self.card.titles.append((text, urljoin(self.source_url, href)))
                elif text:
                    self.card.authors.append(text)
            self.anchor = None
            self.anchor_depth = None
        if self.card_depth == depth and tag == "article":
            self._finish_card()
        if self.main_depth == depth and tag == "main":
            self.main_depth = None
        self.stack.pop()

    def _finish_card(self) -> None:
        assert self.card is not None
        card = self.card
        if len(card.titles) != 1 or not card.titles[0][0] or not card.authors:
            raise ValueError("frontlist card needs one title link and visible author links")
        if not all(admitted_author_people(author) for author in card.authors):
            raise ValueError("frontlist author link does not name a person")
        title, url = card.titles[0]
        target = urlsplit(url)
        if target.scheme != "https" or target.netloc != "nuevoslibros.com" or not target.path.startswith("/libro/"):
            raise ValueError("frontlist title is not a same-site book link")
        if target.query or target.fragment:
            raise ValueError("frontlist book link has a query or fragment")
        if not re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", card.release_date):
            raise ValueError("frontlist card has no readable release date")
        date.fromisoformat(card.release_date if len(card.release_date) == 10 else card.release_date + "-01")
        self.cards.append(BookCard(title, tuple(card.authors), url, card.release_date))
        if len(self.cards) > MAX_CARDS:
            raise ValueError("frontlist exceeds the 500-card bound")
        self.card = None
        self.card_depth = None


def frontlist_cards(markup: str, source_url: str) -> list[BookCard]:
    return list(_frontlist_cards(markup, source_url))


# A collection pass checks every mention against the same capture; parse it
# once, not once per book (#901). Results are pure functions of the arguments.
@lru_cache(maxsize=8)
def _frontlist_cards(markup: str, source_url: str) -> tuple[BookCard, ...]:
    url = urlsplit(source_url)
    genre = url.path.strip("/").removeprefix("genero/")
    if (
        url.scheme != "https"
        or url.netloc != "nuevoslibros.com"
        or url.path != f"/genero/{genre}/"
        or genre not in GENRES
        or url.query
        or url.fragment
    ):
        raise ValueError("frontlist requires one declared NuevosLibros nonfiction genre URL")
    parser = _Parser(source_url)
    parser.feed(markup)
    parser.close()
    if parser.main_count != 1 or parser.body_genres != [genre] or parser.card is not None or not parser.cards:
        raise ValueError("frontlist requires one matching genre body and populated main")
    if normalize_visible_text(heading(source_url)) not in {
        normalize_visible_text(text) for text in parse_heading_texts(markup)
    }:
        raise ValueError("frontlist has no visible genre heading")
    if len({card.url for card in parser.cards}) != len(parser.cards):
        raise ValueError("frontlist repeats a book identity")
    return tuple(parser.cards)


def capture_reference_date(capture: Mapping[str, object]) -> date:
    """Return the capture's reference date; refuse a missing or unreadable one (#993)."""
    raw = capture.get("reference_date")
    if not isinstance(raw, str):
        raise ValueError("frontlist capture has no reference_date")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("frontlist capture has an unreadable reference_date") from exc


def release_day(release_date: str) -> date:
    """Return the day a card's date counts by: a month counts by its last day (#993)."""
    if len(release_date) == 10:
        return date.fromisoformat(release_date)
    year, month = (int(part) for part in release_date.split("-"))
    return date(year, month, monthrange(year, month)[1])


def recent_cards(markup: str, source_url: str, reference_date: date) -> list[BookCard]:
    """Return the validated cards dated within the window before the reference date, or later (#993).

    Validation still grounds against the full capture; only the cards a weekly
    extraction asks about are narrowed here. Future dates are kept.
    """
    earliest = reference_date - timedelta(days=RECENT_WINDOW_DAYS)
    return [card for card in _frontlist_cards(markup, source_url) if release_day(card.release_date) >= earliest]


def heading(source_url: str) -> str:
    return "Novedades de " + GENRES[urlsplit(source_url).path.strip("/").split("/")[-1]]


@lru_cache(maxsize=8)
def linked_html(markup: str, source_url: str) -> str:
    """Metadata-only normal form for the existing exact-list validator/model."""
    cards = _frontlist_cards(markup, source_url)
    rows = [f"<h1>{html.escape(heading(source_url))}</h1><ul>"]
    for card in cards:
        rows.append(
            f'<li><a href="{html.escape(card.url, quote=True)}">{html.escape(card.title)}</a>'
            f'<span class="author">{html.escape(" / ".join(card.authors))}</span>'
            f"<span>{card.release_date}</span></li>"
        )
    return "".join(rows) + "</ul>"


def replay_html(markup: str, source_url: str, selected_urls: list[str]) -> str:
    """Retain representative cards and their actual dates, never invented ones."""
    cards = {card.url: card for card in _frontlist_cards(markup, source_url)}
    genre = urlsplit(source_url).path.strip("/").split("/")[-1]
    rows = [f'<body data-genero="{genre}"><main class="main"><h1>{html.escape(heading(source_url))}</h1>']
    for url in selected_urls:
        card = cards[url]
        rows.append(f'<article class="book" data-fecha="{card.release_date}"><p class="caption">')
        rows.append(f'<a class="t" href="{html.escape(card.url, quote=True)}">{html.escape(card.title)}</a>')
        for index, author in enumerate(card.authors):
            rows.append(f'<a href="/autor/replay-{index}/">{html.escape(author)}</a>')
        rows.append("</p></article>")
    return "".join(rows) + "</main></body>"


def matching_card(markup: str, source_url: str, title: str, authors: list[str], evidence_url: str) -> BookCard | None:
    for card in _frontlist_cards(markup, source_url):
        if (
            card.url == evidence_url
            and normalize_visible_text(card.title) == normalize_visible_text(title)
            and {normalize_visible_text(author) for author in authors}
            == {normalize_visible_text(author) for author in card.authors}
        ):
            return card
    return None
