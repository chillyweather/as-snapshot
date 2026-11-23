"""Check PEOPLE headline and round-up cards and PW children's bylines (Issue #1004)."""

import re
from html.parser import HTMLParser
from urllib.parse import urljoin

from autoscout.identity import normalize_identity_text
from autoscout.source_proposals import normalize_source_identity

ROUNDUP = "round-up article names no single book"
HEADLINE = "article headline is not a book title"
CHILDREN = "children's/YA byline"
_MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
_ROUNDUP = re.compile(r"\bBest Books of " + _MONTH + r"\b", re.I)


class _Entries(HTMLParser):
    """Collect card titles and bylines from captured markup."""

    def __init__(self, tag: str) -> None:
        super().__init__(convert_charrefs=True)
        self.tag = tag
        self.depth = 0
        self.entries: list[dict[str, str]] = []
        self.current: dict[str, str] | None = None
        self.field = ""
        self.field_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if self.current is None and tag == self.tag:
            self.current = {"href": attributes.get("href", ""), "title": "", "alt": "", "byline": ""}
            self.depth = 1
        elif self.current is not None:
            self.depth += 1
        if self.current is None:
            return
        if tag == "img":
            self.current["alt"] += " " + attributes.get("alt", "")
        if tag == "h3" or (tag == "span" and "card__title-text" in attributes.get("class", "").split()):
            self.field, self.field_depth = "title", self.depth
        elif tag == "h4":
            self.field, self.field_depth = "byline", self.depth
        if tag in {
            "img",
            "br",
            "input",
            "hr",
            "source",
            "wbr",
            "meta",
            "link",
            "area",
            "embed",
            "param",
            "track",
            "base",
            "col",
        }:
            self.depth -= 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self.current is None:
            return
        if self.field and self.depth == self.field_depth:
            self.field = ""
        self.depth -= 1
        if self.depth == 0:
            self.entries.append(self.current)
            self.current = None

    def handle_data(self, data: str) -> None:
        if self.current is not None and self.field:
            self.current[self.field] += data


def violation(raw: dict, page: dict, source: dict, mention: dict | None = None) -> str:
    """Check only configured PEOPLE cards and PW rows; unmatched rows remain grounded elsewhere."""
    title = normalize_identity_text(str(raw.get("title", "")))
    if source.get("headline_card_titles"):
        parser = _Entries("a")
        parser.feed(str(page.get("html", "")))
        source_url = str(source.get("url", ""))
        evidence_url = str(
            (mention or {}).get("evidence_url")
            or raw.get("evidence_url")
            or raw.get("source_url")
            or raw.get("item_url")
            or ""
        ).strip()
        for card in parser.entries:
            if (
                card["title"]
                and card["href"]
                and evidence_url
                and normalize_source_identity(urljoin(source_url, card["href"]))
                == normalize_source_identity(urljoin(source_url, evidence_url))
            ):
                headline = normalize_identity_text(card["title"])
                if _ROUNDUP.search(card["title"]) or _ROUNDUP.search(str(raw.get("title", ""))):
                    return ROUNDUP
                if title == headline:
                    return HEADLINE
    markers = source.get("children_byline_markers", [])
    if markers:
        parser = _Entries("li")
        parser.feed(str(page.get("html", "")))
        for row in parser.entries:
            if (
                title
                and title == normalize_identity_text(row["title"])
                and any(str(marker).casefold() in row["byline"].casefold() for marker in markers)
            ):
                return CHILDREN
    return ""
