"""Pure bounded paging state for declared ranking pages."""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser

PAGING_MISSING_FINAL = "missing_final_page"
PAGING_EMPTY = "empty_page"
PAGING_REPEATED = "repeated_page"
PAGING_TOO_LARGE = "response_too_large"
PAGING_MAX_EXHAUSTED = "max_pages_exhausted"


class _SemanticMarkupParser(HTMLParser):
    """Keep visible markup, links, and visible attributes in canonical order."""

    _NON_RENDERED = frozenset({"script", "style", "template", "noscript"})
    _HIDDEN_ATTRIBUTES = frozenset({"hidden", "aria-hidden", "style"})
    _VOID_TAGS = frozenset(
        {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._frames: list[tuple[str, bool, bool]] = []
        self.parts: list[tuple[str, object]] = []

    @staticmethod
    def _own_hidden(attrs: list[tuple[str, str | None]]) -> bool:
        for name, value in attrs:
            folded = name.casefold()
            if folded == "hidden":
                return True
            if folded == "aria-hidden" and str(value or "").strip().casefold() == "true":
                return True
            if folded == "style":
                declarations = str(value or "").split(";")
                for declaration in declarations:
                    property_name, separator, property_value = declaration.partition(":")
                    if not separator:
                        continue
                    if property_name.strip().casefold() in {"display", "visibility"}:
                        if re.sub(r"!important\s*$", "", property_value.strip(), flags=re.IGNORECASE).casefold() in {
                            "none",
                            "hidden",
                        }:
                            return True
        return False

    def _start(self, tag: str, attrs: list[tuple[str, str | None]], *, self_closing: bool) -> None:
        parent_hidden = self._frames[-1][1] if self._frames else False
        hidden = parent_hidden or tag in self._NON_RENDERED or self._own_hidden(attrs)
        included = not hidden
        if included:
            normalized_attrs: list[tuple[str, str]] = []
            for name, value in attrs:
                folded = name.casefold()
                if folded in self._HIDDEN_ATTRIBUTES:
                    continue
                is_semantic_attribute = folded in {"href", "title", "alt", "aria-label"} or (
                    folded.startswith("data-")
                    and any(token in folded for token in ("id", "isbn", "rank", "title", "author", "book", "url"))
                )
                if folded != "class" and not is_semantic_attribute:
                    continue
                if folded == "class":
                    value = " ".join(sorted(str(value or "").split()))
                normalized_attrs.append((folded, " ".join(str(value or "").split())))
            self.parts.append(("tag", (tag, tuple(sorted(normalized_attrs)))))
        if not self_closing:
            self._frames.append((tag, hidden, included))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.casefold()
        self._start(normalized_tag, attrs, self_closing=normalized_tag in self._VOID_TAGS)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag.casefold(), attrs, self_closing=True)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        for index in range(len(self._frames) - 1, -1, -1):
            frame_tag, _hidden, included = self._frames[index]
            if frame_tag == tag:
                del self._frames[index:]
                if included:
                    self.parts.append(("end", tag))
                return

    def handle_data(self, data: str) -> None:
        if self._frames and self._frames[-1][1]:
            return
        normalized = " ".join(html.unescape(data).split())
        if normalized:
            self.parts.append(("text", normalized))


def semantic_page_fingerprint(markup: str) -> str:
    """Return a deterministic fingerprint of the visible ranking markup."""
    parser = _SemanticMarkupParser()
    parser.feed(markup)
    parser.close()
    canonical = json.dumps(parser.parts, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RankingPagingFailure:
    """A pure paging failure without adapter-specific wording."""

    kind: str
    page_number: int


@dataclass(frozen=True)
class RankingPagingState:
    """The bounded page sequence accumulated so far."""

    max_pages: int
    max_bytes: int
    pages: tuple[str, ...] = ()
    fingerprints: frozenset[str] = frozenset()
    complete: bool = False
    failure: RankingPagingFailure | None = None

    @classmethod
    def start(cls, *, max_pages: int, max_bytes: int, html: str, final_page: bool | None) -> RankingPagingState:
        """Validate and retain the first page of a declared sequence."""
        state = cls(max_pages=max_pages, max_bytes=max_bytes)
        return state._append(html, final_page)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def joined_html(self) -> str:
        return "".join(self.pages)

    @property
    def needs_next_page(self) -> bool:
        return not self.complete and self.failure is None

    def append(self, html: str, final_page: bool | None) -> RankingPagingState:
        """Validate and retain one later page."""
        if self.complete or self.failure is not None:
            return self
        return self._append(html, final_page)

    def _append(self, html: str, final_page: bool | None) -> RankingPagingState:
        page_number = len(self.pages) + 1
        if final_page is None:
            return self._failed(PAGING_MISSING_FINAL, page_number)
        if not html.strip():
            return self._failed(PAGING_EMPTY, page_number)
        fingerprint = semantic_page_fingerprint(html)
        if fingerprint in self.fingerprints:
            return self._failed(PAGING_REPEATED, page_number)
        pages = (*self.pages, html)
        joined = "".join(pages)
        if len(joined.encode("utf-8")) > self.max_bytes:
            return self._failed(PAGING_TOO_LARGE, page_number)
        if final_page:
            return RankingPagingState(
                max_pages=self.max_pages,
                max_bytes=self.max_bytes,
                pages=pages,
                fingerprints=self.fingerprints | {fingerprint},
                complete=True,
            )
        if page_number >= self.max_pages:
            return self._failed(PAGING_MAX_EXHAUSTED, page_number)
        return RankingPagingState(
            max_pages=self.max_pages,
            max_bytes=self.max_bytes,
            pages=pages,
            fingerprints=self.fingerprints | {fingerprint},
        )

    def _failed(self, kind: str, page_number: int) -> RankingPagingState:
        return RankingPagingState(
            max_pages=self.max_pages,
            max_bytes=self.max_bytes,
            pages=self.pages,
            fingerprints=self.fingerprints,
            failure=RankingPagingFailure(kind, page_number),
        )
