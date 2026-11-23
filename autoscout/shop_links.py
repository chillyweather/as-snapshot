"""Shop links for the editor-facing review (issues #975, #983).

AutoScout never fetches these shops. BookFinder's ``robots.txt`` disallows
``/isbn/`` and ``/search/`` and it sits behind a bot challenge; IberLibro's
disallows ``/servlet/``, where its search lives. The review only renders
links the editor opens in her own browser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

_BOOKFINDER = "https://www.bookfinder.com"
# The Spanish AbeBooks site: Spanish sellers and Spanish defaults (issue #983).
_IBERLIBRO = "https://www.iberlibro.com/servlet/SearchResults"
# The editor lives in Spain: prices include shipping there, in euros.
_REGION = {"destination": "ES", "currency": "EUR"}


def normalized_isbn(value: Any) -> str:
    """Return a checksum-valid ISBN-13 or ISBN-10 without separators, else ``""``."""
    compact = re.sub(r"[\s-]", "", str(value or "")).upper()
    if re.fullmatch(r"97[89]\d{10}", compact):
        total = sum(int(digit) * (1 if index % 2 == 0 else 3) for index, digit in enumerate(compact))
        return compact if total % 10 == 0 else ""
    if re.fullmatch(r"\d{9}[\dX]", compact):
        total = sum((10 - index) * (10 if char == "X" else int(char)) for index, char in enumerate(compact))
        return compact if total % 11 == 0 else ""
    return ""


@dataclass(frozen=True)
class ShopLink:
    """One shop link and what it points at (issue #978).

    ``isbn`` is set only for an exact-edition link; a title-and-author search
    leaves it blank, so the review can show the two differently.
    """

    url: str
    isbn: str = ""

    @property
    def is_exact_edition(self) -> bool:
        return bool(self.isbn)


def bookfinder_link(isbn: Any, title: str, author: str) -> ShopLink | None:
    """The BookFinder page for the ISBN, else a title-and-author search, else ``None``.

    The search asks for any language; BookFinder otherwise searches English only.
    """
    clean_isbn = normalized_isbn(isbn)
    if clean_isbn:
        return ShopLink(url=f"{_BOOKFINDER}/isbn/{clean_isbn}/?{urlencode(_REGION)}", isbn=clean_isbn)
    title = " ".join(str(title or "").split())
    if not title:
        return None
    query = {"title": title}
    author = " ".join(str(author or "").split())
    if author:
        query["author"] = author
    query.update(lang="ANY", **_REGION)
    return ShopLink(url=f"{_BOOKFINDER}/search/?{urlencode(query)}")


def iberlibro_link(isbn: Any, title: str, author: str) -> ShopLink | None:
    """IberLibro's offers for the ISBN, else a title-and-author search, else ``None``."""
    clean_isbn = normalized_isbn(isbn)
    if clean_isbn:
        return ShopLink(url=f"{_IBERLIBRO}?{urlencode({'isbn': clean_isbn})}", isbn=clean_isbn)
    title = " ".join(str(title or "").split())
    if not title:
        return None
    query = {"tn": title}
    author = " ".join(str(author or "").split())
    if author:
        query["an"] = author
    return ShopLink(url=f"{_IBERLIBRO}?{urlencode(query)}")


def bookfinder_url(isbn: Any, title: str, author: str) -> str:
    """The URL of :func:`bookfinder_link`, or ``""`` when there is no link."""
    link = bookfinder_link(isbn, title, author)
    return link.url if link else ""
