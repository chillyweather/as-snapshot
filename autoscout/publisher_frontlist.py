"""Bounded publisher cards for Verso and Beacon; extraction stays with the agent (#936, #937).

Both publishers put title/author metadata outside the generic linked-list
shape. This opt-in adapter binds metadata to one card without treating a
card's header/footer as site chrome. It performs no fetching or eligibility
inference and accepts only the two reviewed recurring URLs.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from dataclasses import field
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

FAMILY = "publisher-frontlist"
VERSO = "https://www.versobooks.com/en-gb/collections/forthcoming"
BEACON = "https://www.beacon.org/Forthcoming-C1461.aspx"
MAX_CARDS = 100


@dataclass(frozen=True)
class BookCard:
    title: str
    authors: tuple[str, ...]
    url: str
    release_date: str = ""


@dataclass
class _Card:
    depth: int
    titles: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    merchandise: bool = False
    role: str = "author:"


class _Parser(HTMLParser):
    def __init__(self, source_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source_url = source_url
        self.stack: list[tuple[str, dict[str, str | None]]] = []
        self.visibility: list[_VisibilityFrame] = []
        self.card: _Card | None = None
        self.cards: list[BookCard] = []
        self.field_kind = ""
        self.field_depth = -1
        self.parts: list[str] = []
        self.byline_prefix = ""
        self.byline_started = False
        self.byline_is_author = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        hidden = _push_visibility(self.visibility, tag, attrs)
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        in_main = any(t == "main" for t, _ in self.stack)
        is_card = (self.source_url == VERSO and tag == "article" and "c-product-card" in classes and in_main) or (
            self.source_url == BEACON and tag == "div" and "SearchItem" in classes
        )
        outside_chrome = not any(t in {"nav", "aside", "header", "footer"} for t, _ in self.stack)
        if is_card and not hidden and outside_chrome:
            if self.card is not None:
                raise ValueError("nested publisher card")
            self.card = _Card(len(self.stack))
        if self.card is not None and not hidden:
            if self.source_url == VERSO:
                if "c-product-card__image-wrapper--merchandise" in classes:
                    self.card.merchandise = True
                if tag == "a" and "c-product-card__product-link" in classes:
                    self.card.links.append(urljoin(self.source_url, a.get("href") or ""))
                if tag == "h3" and "c-product-card__title" in classes:
                    self._begin_field("title")
                if tag == "p" and "c-product-card__byline" in classes:
                    self.byline_prefix, self.byline_started, self.byline_is_author = "", False, False
                if tag == "a" and any(
                    "c-product-card__byline" in (v.get("class") or "").split() for _, v in self.stack
                ):
                    if not self.byline_started:
                        self.byline_is_author = self.byline_prefix.strip().casefold() == "by"
                        self.byline_started = True
                    if self.byline_is_author:
                        self._begin_field("author")
            else:
                if tag == "span" and "authorbold" in classes:
                    self._begin_field("role")
                if tag == "img" and "Thumbnail" in classes:
                    self.card.titles.append(a.get("alt") or "")
                    parent = next((v for t, v in reversed(self.stack) if t == "a"), {})
                    self.card.links.append(urljoin(self.source_url, parent.get("href") or ""))
                if (
                    tag == "a"
                    and self.stack
                    and "author" in (self.stack[-1][1].get("class") or "").split()
                    and self.card.role == "author:"
                ):
                    self._begin_field("author")
        if tag not in _VOID_TAGS:
            self.stack.append((tag, a))

    def _begin_field(self, kind: str) -> None:
        if self.field_kind:
            raise ValueError("nested publisher metadata field")
        self.field_kind, self.field_depth, self.parts = kind, len(self.stack), []

    def handle_data(self, data: str) -> None:
        if (
            self.card is not None
            and self.source_url == VERSO
            and not self.byline_started
            and any("c-product-card__byline" in (v.get("class") or "").split() for _, v in self.stack)
            and self.visibility
            and not self.visibility[-1].hidden
        ):
            self.byline_prefix += data
        if self.field_kind and self.visibility and not self.visibility[-1].hidden:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        # Beacon omits the outer SearchItem </div>; the table cell is its
        # explicit row boundary. Accept only that one documented omission.
        if (
            self.source_url == BEACON
            and tag == "td"
            and self.card is not None
            and len(self.stack) == self.card.depth + 1
            and not self.field_kind
            and len(self.stack) >= 2
            and self.stack[-2][0] == "td"
        ):
            self._finish_card()
            self.stack.pop()
        _pop_visibility(self.visibility, tag)
        if tag in _VOID_TAGS:
            return
        if not self.stack or self.stack[-1][0] != tag:
            if self.card is not None:
                raise ValueError("unbalanced publisher card")
            # HTML void/self-closing site widgets are outside the card contract.
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index][0] == tag:
                    del self.stack[index:]
                    break
            return
        depth = len(self.stack) - 1
        if self.field_kind and self.field_depth == depth:
            text = " ".join(" ".join(self.parts).split())
            assert self.card is not None
            if self.field_kind == "role":
                self.card.role = text.casefold()
            else:
                (self.card.titles if self.field_kind == "title" else self.card.authors).append(text)
            self.field_kind, self.field_depth, self.parts = "", -1, []
        if self.card is not None and self.card.depth == depth:
            self._finish_card()
        self.stack.pop()

    def _finish_card(self) -> None:
        assert self.card is not None
        card = self.card
        if not card.merchandise:
            if len(card.titles) != 1 or len(card.links) != 1 or not card.titles[0].strip() or not card.authors:
                raise ValueError("publisher card needs one title, book link and author display")
            if not all(admitted_author_people(a) for a in card.authors):
                raise ValueError("publisher author does not name a person")
            target = urlsplit(card.links[0])
            if (
                target.scheme != "https"
                or target.netloc != urlsplit(self.source_url).netloc
                or target.query
                or target.fragment
            ):
                raise ValueError("publisher card needs a same-site HTTPS book link")
            if self.source_url == VERSO and not target.path.startswith("/en-gb/products/"):
                raise ValueError("Verso card needs a product link")
            if self.source_url == BEACON and (not target.path.endswith(".aspx") or "-P" not in target.path):
                raise ValueError("Beacon card needs a product link")
            self.cards.append(BookCard(card.titles[0].strip(), tuple(card.authors), card.links[0]))
            if len(self.cards) > MAX_CARDS:
                raise ValueError("publisher listing exceeds 100 books")
        self.card = None


@lru_cache(maxsize=8)
def _cards(markup: str, source_url: str) -> tuple[BookCard, ...]:
    if source_url not in {VERSO, BEACON}:
        raise ValueError("publisher frontlist requires one reviewed recurring URL")
    if normalize_visible_text("Forthcoming") not in {normalize_visible_text(t) for t in parse_heading_texts(markup)}:
        raise ValueError("publisher frontlist needs its visible Forthcoming heading")
    parser = _Parser(source_url)
    parser.feed(markup)
    parser.close()
    if parser.card is not None or not parser.cards:
        raise ValueError("publisher frontlist needs complete populated cards")
    if len({card.url for card in parser.cards}) != len(parser.cards):
        raise ValueError("publisher frontlist repeats a book link")
    return tuple(parser.cards)


def frontlist_cards(markup: str, source_url: str) -> list[BookCard]:
    return list(_cards(markup, source_url))


def heading(source_url: str) -> str:
    if source_url not in {VERSO, BEACON}:
        raise ValueError("unreviewed publisher URL")
    return "Forthcoming"


@lru_cache(maxsize=8)
def linked_html(markup: str, source_url: str) -> str:
    rows = ["<h1>Forthcoming</h1><ul>"]
    for card in _cards(markup, source_url):
        rows.append(
            f'<li><a href="{html.escape(card.url, quote=True)}">{html.escape(card.title)}</a>'
            f'<span class="author">{html.escape(" / ".join(card.authors))}</span></li>'
        )
    return "".join(rows) + "</ul>"


def replay_html(markup: str, source_url: str, selected_urls: list[str]) -> str:
    cards = {c.url: c for c in _cards(markup, source_url)}
    rows = ["<main><h1>Forthcoming</h1>"]
    for url in selected_urls:
        card = cards[url]
        title, link = html.escape(card.title), html.escape(card.url, quote=True)
        if source_url == VERSO:
            rows.append(
                f'<article class="c-product-card"><a class="c-product-card__product-link" href="{link}">'
                f'<header><h3 class="c-product-card__title">{title}</h3></header></a>'
                '<footer><p class="c-product-card__byline">by '
            )
            for author in card.authors:
                rows.append(f'<a href="/en-gb/blogs/authors/replay">{html.escape(author)}</a>')
            rows.append("</p></footer></article>")
        else:
            rows.append(f'<div class="SearchItem"><a href="{link}"><img class="Thumbnail" alt="{title}"></a>')
            for author in card.authors:
                rows.append(f'<span class="author"><a href="/cw_contributorinfo.aspx">{html.escape(author)}</a></span>')
            rows.append("</div>")
    return "".join(rows) + "</main>"


def matching_card(markup: str, source_url: str, title: str, authors: list[str], evidence_url: str) -> BookCard | None:
    for card in _cards(markup, source_url):
        if (
            card.url == evidence_url
            and normalize_visible_text(card.title) == normalize_visible_text(title)
            and [normalize_visible_text(a) for a in authors] == [normalize_visible_text(a) for a in card.authors]
        ):
            return card
    return None
