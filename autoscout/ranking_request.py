"""How a bounded ranking page is requested.

Every source fetch used one plain ``GET`` on the configured URL.  That is
right for a page that prints its own rows, and wrong for a recurring
industry ranking whose rows are served by a form endpoint.  The Norwegian
Bokhandlerforeningen Boklista is the recorded case: its dated
``/book-list/<year>-<week>-<list>/`` page returns HTTP 200 with a heading, a
publication date, and no rows at all, because the weekly Top 25 is served by
a form ``POST`` to ``admin-ajax.php``.  A ``GET``-only probe can only reject
that source, and the rejection says nothing true about the ranking.

A source may now declare how its ranking page is requested.  The declaration
is narrow by construction:

* It is a declaration on the source, never a discovery.  The method and every
  form field are fixed strings written by the operator; nothing is read from
  the page, guessed from a script, or templated at request time.
* Only ``GET`` and ``POST`` exist.  ``POST`` carries exactly one body kind,
  ``application/x-www-form-urlencoded``, built from the declared fields.
* A declared body is bounded before the request, so a source cannot grow an
  unbounded upload out of configuration.
* Each bounded declared ``POST`` page is requested once.  There is no
  ``HEAD`` request, retry, or method fallback for the route.
* A source that declares nothing keeps the unchanged single ``GET`` contract.

The declaration says how to ask for the page.  It says nothing about what a
valid page contains: the row, title, author, identifier, category, and
reporting-period declarations are unchanged, and a ``POST`` route is held to
exactly the same bounded evidence as a ``GET`` one.
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import replace
from typing import Any
from urllib.parse import urlencode

from autoscout.ranking_response import RANKING_RESPONSE_HTML_FIELD
from autoscout.ranking_response import RANKING_RESPONSE_LAST_PAGE_FIELD

RANKING_REQUEST_METHOD_FIELD = "ranking_request_method"
RANKING_REQUEST_BODY_FIELD = "ranking_request_body"
RANKING_REQUEST_PAGE_FIELD = "ranking_request_page_field"
RANKING_REQUEST_MAX_PAGES_FIELD = "ranking_request_max_pages"

#: The only methods a source may declare.
RANKING_REQUEST_METHODS: tuple[str, ...] = ("GET", "POST")

#: The method used by every source that declares none.
DEFAULT_RANKING_REQUEST_METHOD = "GET"

#: The single body kind a declared ``POST`` may carry.
RANKING_REQUEST_CONTENT_TYPE = "application/x-www-form-urlencoded"

#: Upper bound on the encoded body, in bytes.  A ranking form endpoint needs
#: a handful of short fields; anything larger is a configuration mistake, and
#: bounding it here keeps the bound in front of the network rather than
#: behind it.
MAX_RANKING_REQUEST_BODY_BYTES = 4096

#: Upper bound on the number of declared form fields.
MAX_RANKING_REQUEST_FIELDS = 32

#: Hard ceiling on declared pages, whatever a source asks for. A ranking is
#: a bounded list; a source that needs more pages than this is not a ranking.
MAX_RANKING_REQUEST_PAGES = 10

_FIELD_SEPARATOR = "="


@dataclass(frozen=True)
class RankingRequest:
    """One fully declared, ready-to-issue ranking page request."""

    method: str = DEFAULT_RANKING_REQUEST_METHOD
    fields: tuple[tuple[str, str], ...] = ()
    page_field: str = ""
    max_pages: int = 1

    @property
    def paging_declared(self) -> bool:
        """Return whether the source declares a bounded page sequence."""
        return bool(self.page_field)

    @property
    def is_paged(self) -> bool:
        """Return whether this request walks more than one declared page."""
        return self.paging_declared and self.max_pages > 1

    def for_page(self, page: int) -> RankingRequest:
        """Return this request with its declared page field set to *page*.

        The page field is replaced rather than appended, so a declaration
        that already names the field cannot send it twice.
        """
        if not self.page_field:
            return self
        fields = tuple((name, str(page) if name == self.page_field else value) for name, value in self.fields)
        return replace(self, fields=fields)

    @property
    def is_post(self) -> bool:
        """Return whether this request is a declared form ``POST``."""
        return self.method == "POST"

    @property
    def body(self) -> bytes:
        """Return the encoded form body, empty for a ``GET``."""
        if not self.is_post:
            return b""
        return urlencode(self.fields).encode("utf-8")

    @property
    def headers(self) -> dict[str, str]:
        """Return the request headers this declaration adds, if any."""
        if not self.is_post:
            return {}
        return {"Content-Type": RANKING_REQUEST_CONTENT_TYPE}

    def describe(self) -> str:
        """Return a deterministic, evidence-safe summary of the request."""
        if not self.is_post:
            return "GET"
        return "POST " + " ".join(name for name, _value in self.fields)


#: The request every source that declares nothing makes.
DEFAULT_RANKING_REQUEST = RankingRequest()


def _declared_method(source: Mapping[str, Any]) -> str:
    value = source.get(RANKING_REQUEST_METHOD_FIELD)
    if value is None:
        return DEFAULT_RANKING_REQUEST_METHOD
    return str(value).strip().upper()


def ranking_request_declared(source: Mapping[str, Any]) -> bool:
    """Return whether this source declares a non-default request."""
    return source.get(RANKING_REQUEST_METHOD_FIELD) is not None or source.get(RANKING_REQUEST_BODY_FIELD) is not None


def _parse_declared_fields(value: Any) -> tuple[tuple[tuple[str, str], ...], str]:
    """Split declared ``key=value`` entries, or return the declaration error."""
    if value is None:
        return (), ""
    if isinstance(value, str) or not isinstance(value, Sequence):
        return (), f"{RANKING_REQUEST_BODY_FIELD} must be a list of 'name=value' entries"
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            return (), f"{RANKING_REQUEST_BODY_FIELD} entries must be strings"
        name, separator, field_value = item.partition(_FIELD_SEPARATOR)
        if not separator:
            return (), f"{RANKING_REQUEST_BODY_FIELD} entry '{item}' must use the form 'name=value'"
        name = name.strip()
        if not name:
            return (), f"{RANKING_REQUEST_BODY_FIELD} entry '{item}' has an empty field name"
        # A control character in a declared field is never intentional, and
        # letting one through would put an unreviewable byte on the wire.
        if any(character < " " or character == "\x7f" for character in item):
            return (), f"{RANKING_REQUEST_BODY_FIELD} entry '{name}' contains a control character"
        if name in seen:
            return (), f"{RANKING_REQUEST_BODY_FIELD} declares '{name}' more than once"
        seen.add(name)
        entries.append((name, field_value))
    return tuple(entries), ""


def validate_ranking_request_declaration(source: Mapping[str, Any]) -> str:
    """Return the declaration error for the request fields, if any."""
    raw_method = source.get(RANKING_REQUEST_METHOD_FIELD)
    if raw_method is not None and not isinstance(raw_method, str):
        return f"{RANKING_REQUEST_METHOD_FIELD} must be a string"
    method = _declared_method(source)
    if method not in RANKING_REQUEST_METHODS:
        return f"{RANKING_REQUEST_METHOD_FIELD} must be one of: " + ", ".join(RANKING_REQUEST_METHODS)
    fields, error = _parse_declared_fields(source.get(RANKING_REQUEST_BODY_FIELD))
    if error:
        return error
    paging_error = _validate_paging(source, fields, method=method)
    if paging_error:
        return paging_error
    if method == "GET":
        if fields:
            return f"{RANKING_REQUEST_BODY_FIELD} requires {RANKING_REQUEST_METHOD_FIELD} POST"
        return ""
    if not fields:
        # A POST with no declared body would be an unexplained write-shaped
        # request. The point of the declaration is that the operator states
        # exactly which form the ranking endpoint expects.
        return f"{RANKING_REQUEST_METHOD_FIELD} POST requires a nonempty {RANKING_REQUEST_BODY_FIELD}"
    if len(fields) > MAX_RANKING_REQUEST_FIELDS:
        return f"{RANKING_REQUEST_BODY_FIELD} declares more than {MAX_RANKING_REQUEST_FIELDS} fields"
    encoded_bytes = len(urlencode(fields).encode("utf-8"))
    if encoded_bytes > MAX_RANKING_REQUEST_BODY_BYTES:
        return f"{RANKING_REQUEST_BODY_FIELD} encodes to more than {MAX_RANKING_REQUEST_BODY_BYTES} bytes"
    return ""


def _validate_paging(source: Mapping[str, Any], fields: tuple[tuple[str, str], ...], *, method: str = "POST") -> str:
    """Return the declaration error for the paging fields, if any."""
    page_field = source.get(RANKING_REQUEST_PAGE_FIELD)
    max_pages = source.get(RANKING_REQUEST_MAX_PAGES_FIELD)
    if page_field is None and max_pages is None:
        return ""
    if method != "POST":
        return f"{RANKING_REQUEST_PAGE_FIELD} requires {RANKING_REQUEST_METHOD_FIELD} POST"
    if page_field is None:
        return f"{RANKING_REQUEST_MAX_PAGES_FIELD} requires {RANKING_REQUEST_PAGE_FIELD}"
    if not isinstance(page_field, str) or not page_field.strip():
        return f"{RANKING_REQUEST_PAGE_FIELD} must be a nonempty field name"
    if page_field != page_field.strip():
        return f"{RANKING_REQUEST_PAGE_FIELD} must not have leading or trailing whitespace"
    # The page field must be one the source already declares, so the request
    # shape stays exactly what the operator wrote down.
    if page_field not in {name for name, _value in fields}:
        return f"{RANKING_REQUEST_PAGE_FIELD} '{page_field}' is not declared in {RANKING_REQUEST_BODY_FIELD}"
    if max_pages is None:
        return f"{RANKING_REQUEST_PAGE_FIELD} requires {RANKING_REQUEST_MAX_PAGES_FIELD}"
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
        return f"{RANKING_REQUEST_MAX_PAGES_FIELD} must be a positive integer"
    if max_pages > MAX_RANKING_REQUEST_PAGES:
        return f"{RANKING_REQUEST_MAX_PAGES_FIELD} must not exceed {MAX_RANKING_REQUEST_PAGES}"
    largest_page_fields = tuple((name, str(max_pages) if name == page_field else value) for name, value in fields)
    largest_page_bytes = len(urlencode(largest_page_fields).encode("utf-8"))
    if largest_page_bytes > MAX_RANKING_REQUEST_BODY_BYTES:
        return (
            f"{RANKING_REQUEST_BODY_FIELD} for page {max_pages} encodes to more than "
            f"{MAX_RANKING_REQUEST_BODY_BYTES} bytes"
        )
    if (
        not isinstance(source.get(RANKING_RESPONSE_HTML_FIELD), str)
        or not str(source.get(RANKING_RESPONSE_HTML_FIELD, "")).strip()
    ):
        return f"{RANKING_REQUEST_PAGE_FIELD} requires {RANKING_RESPONSE_HTML_FIELD}"
    if (
        not isinstance(source.get(RANKING_RESPONSE_LAST_PAGE_FIELD), str)
        or not str(source.get(RANKING_RESPONSE_LAST_PAGE_FIELD, "")).strip()
    ):
        return f"{RANKING_REQUEST_PAGE_FIELD} requires {RANKING_RESPONSE_LAST_PAGE_FIELD}"
    return ""


def ranking_request_from_source(source: Mapping[str, Any]) -> RankingRequest:
    """Return the declared request for one source.

    The caller must validate first.  An invalid declaration returns the
    default single ``GET`` rather than a partially built request, so a
    validation gap can never widen into an unintended method on the wire.
    """
    if validate_ranking_request_declaration(source):
        return DEFAULT_RANKING_REQUEST
    method = _declared_method(source)
    if method != "POST":
        return DEFAULT_RANKING_REQUEST
    fields, _error = _parse_declared_fields(source.get(RANKING_REQUEST_BODY_FIELD))
    page_field = str(source.get(RANKING_REQUEST_PAGE_FIELD, "") or "").strip()
    max_pages = source.get(RANKING_REQUEST_MAX_PAGES_FIELD)
    return RankingRequest(
        method="POST",
        fields=fields,
        page_field=page_field,
        max_pages=int(max_pages) if page_field and isinstance(max_pages, int) else 1,
    )
