"""An extracted author must be in the capture it was extracted from (Issue #850).

An extractor can supply an author the source page never names: a paraphrase
of the page (``The Cartoonist Cooperative`` for a page whose URL says
``the-cartoonist-collective``) or a name it knows from elsewhere. Either
becomes a separate identity beside the real book, out of reach of the
editor's decision on it.

"In the capture" means anything the capture keeps: the raw HTML (visible
text, attribute values, embedded script data), every other retained field
such as a structured text body, and the entry's evidence URL slug. Measured
over every committed capture, a narrower rule of visible text, attributes,
and slug alone would refuse 198 correct authors from five sources whose
names live only in page data.

A person matches as a whole normalized phrase, accent-free and
case-insensitive, in the printed order, given name first, or surname first
with a surname of any length. Markup is searched as written, with its tags
removed (``Jane <em>Author</em>``), and with its tags read as spaces
(``Jon<br>Ronson``).

A ranking-table source may vouch for an author its page does not print with a
retained ISBN catalog record (``ranking_author_evidence_json``); the source
contract already grounds that author, and so does this check.
"""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import unquote
from urllib.parse import urlsplit

from autoscout.author_admission import admitted_author_people
from autoscout.identity import normalize_author_display
from autoscout.identity import normalize_author_identity

AUTHOR_NOT_IN_CAPTURE_DROP_REASON = "author_not_in_capture"

_JSON_UNICODE_ESCAPE = re.compile(r"\\\\?u([0-9a-fA-F]{4})")
_TAG = re.compile(r"<[^>]*>")


def _searchable(value: str) -> str:
    return f" {normalize_author_identity(value)} "


def _person_forms(person: str) -> list[str]:
    printed = normalize_author_identity(person)
    given_first = normalize_author_identity(normalize_author_display(person))
    tokens = given_first.split()
    # Every split point: "Gabriel García Márquez" is printed "García Márquez, Gabriel".
    surname_first = [" ".join([*tokens[split:], *tokens[:split]]) for split in range(1, len(tokens))]
    return [form for form in dict.fromkeys((printed, given_first, *surname_first)) if form]


def _strings(value: Any) -> list[str]:
    """Return every string inside a retained capture field, decoded as stored."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return [] if value is None else [str(value)]


class CaptureAuthorText:
    """The normalized text of one capture, built once per extraction pass."""

    def __init__(self, page_data: dict[str, Any]) -> None:
        pieces: list[str] = []
        for text in _strings(page_data):
            # Script data and JSON bodies escape non-ASCII letters as \uXXXX.
            text = html.unescape(_JSON_UNICODE_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), text))
            pieces.append(text)
            if "<" in text:
                pieces.append(_TAG.sub("", text))
                pieces.append(_TAG.sub(" ", text))
        # Each piece is searched on its own, so no phrase spans two of them.
        self._pieces = tuple(dict.fromkeys(_searchable(piece) for piece in pieces))

    def missing_people(
        self, authors: list[Any], evidence_url: str, *, vouched_authors: tuple[str, ...] = ()
    ) -> list[str]:
        """Return each admitted person in *authors* the capture does not name.

        *vouched_authors* are author values retained evidence names for this
        row, such as a verified ISBN catalog record.
        """
        slug = unquote(urlsplit(evidence_url).path).replace("-", " ").replace("_", " ").replace("/", " ")
        texts = (*self._pieces, _searchable(slug), *(_searchable(author) for author in vouched_authors if author))
        missing: list[str] = []
        for author in authors:
            for person in admitted_author_people(str(author)):
                forms = _person_forms(person)
                if forms and not any(f" {form} " in text for form in forms for text in texts):
                    missing.append(person)
        return missing
