"""Proving which ranking a form route just returned.

A page a reader opens carries its own identity: the heading names the list
and the week, and the rows below it are that list.  A form route carries no
such thing.  ``admin-ajax.php`` answers the same way whatever list is asked
for, so the rows alone cannot say whether they are adult non-fiction or
fiction, this week's or a replayed archive.  Trusting the request parameter
would make the declaration its own evidence.

A source may now declare a second, read-only identity request.  The rows
response reports the period it covers, that period names one dated page, and
that page's visible heading is the independent statement of what the rows
are.  The declaration is narrow by construction:

* The identity page is fetched with a plain ``GET`` and is never parsed for
  rows.  It supplies one thing: the visible heading.
* The URL is built from a declared template and declared envelope fields.
  Every placeholder must be declared, every declared field must be present in
  the envelope, and a resolved value that is not a plain alphanumeric token is
  refused.  Nothing is interpolated from the page or from a response body
  anywhere else.
* The same heading must contain the source's declared
  ``ranking_category_marker`` and every safe placeholder token resolved from
  the first response envelope. A heading that does not is a refusal, not a
  warning.
* It fails closed at every step, with a distinct reason for each.

The identity page proves the list's name and period.  It cannot prove that an
opaque numeric list id maps to that name; the operator declares that pairing
once, and the weekly check confirms the named list still exists and is
current.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from string import Formatter
from typing import Any
from urllib.parse import SplitResult
from urllib.parse import urlsplit

RANKING_IDENTITY_URL_TEMPLATE_FIELD = "ranking_identity_url_template"
RANKING_IDENTITY_FIELDS_FIELD = "ranking_identity_fields"

REASON_IDENTITY_UNRESOLVED = "identity_url_unresolved"
REASON_IDENTITY_UNREACHABLE = "identity_page_unreachable"
REASON_IDENTITY_MISMATCH = "identity_category_mismatch"

#: A resolved placeholder is part of a URL path, so it is held to a plain
#: token.  This is what keeps a hostile envelope from steering the request.
_SAFE_VALUE_RE = re.compile(r"[A-Za-z0-9]{1,32}")

_FIELD_SEPARATOR = "="


def _effective_port(parts: SplitResult) -> int | None:
    """Return the explicit or scheme-default port for an HTTP URL."""
    if parts.port is not None:
        return parts.port
    return {"http": 80, "https": 443}.get(parts.scheme.casefold())


def same_origin_url(left: str, right: str) -> bool:
    """Return whether two HTTP URLs share scheme, host, and effective port."""
    try:
        left_parts = urlsplit(left)
        right_parts = urlsplit(right)
        return (
            left_parts.scheme.casefold() in {"http", "https"}
            and right_parts.scheme.casefold() in {"http", "https"}
            and left_parts.hostname is not None
            and right_parts.hostname is not None
            and left_parts.hostname.casefold() == right_parts.hostname.casefold()
            and _effective_port(left_parts) == _effective_port(right_parts)
        )
    except ValueError:
        return False


@dataclass(frozen=True)
class RankingIdentityRoute:
    """One declared identity request."""

    url_template: str
    fields: tuple[tuple[str, str], ...]

    def resolve(self, envelope: Mapping[str, Any], configured_url: str = "") -> tuple[str, str]:
        """Return the identity URL for *envelope*, or the reason there is none."""
        values: dict[str, str] = {}
        for placeholder, source_field in self.fields:
            if source_field not in envelope:
                return "", f"response JSON has no {source_field!r} field for identity placeholder {placeholder!r}"
            raw = envelope[source_field]
            if isinstance(raw, bool) or not isinstance(raw, str | int):
                return "", f"identity field {source_field!r} is not a token"
            value = str(raw).strip()
            if not _SAFE_VALUE_RE.fullmatch(value):
                return "", f"identity field {source_field!r} is not a plain token"
            values[placeholder] = value
        try:
            url = self.url_template.format(**values)
        except (KeyError, IndexError, ValueError) as exc:
            return "", f"identity URL template cannot be resolved: {exc}"
        if configured_url:
            if not same_origin_url(configured_url, url):
                return "", "resolved identity URL is not on the configured source host"
        return url, ""

    def resolved_tokens(self, envelope: Mapping[str, Any]) -> tuple[str, ...]:
        """Return the safe declared placeholder values from *envelope*."""
        tokens: list[str] = []
        for _placeholder, source_field in self.fields:
            raw = envelope.get(source_field)
            if isinstance(raw, bool) or not isinstance(raw, str | int):
                return ()
            token = str(raw).strip()
            if not _SAFE_VALUE_RE.fullmatch(token):
                return ()
            tokens.append(token)
        return tuple(tokens)


def ranking_identity_declared(source: Mapping[str, Any]) -> bool:
    """Return whether this source declares an identity request."""
    return bool(str(source.get(RANKING_IDENTITY_URL_TEMPLATE_FIELD, "") or "").strip())


def _template_placeholders(template: str) -> tuple[str, ...]:
    placeholders: list[str] = []
    for _text, name, spec, conversion in Formatter().parse(template):
        if name and (spec or conversion):
            raise ValueError("placeholders must not use format specifications or conversions")
        if name:
            placeholders.append(name)
    return tuple(placeholders)


def _parse_identity_fields(value: Any) -> tuple[tuple[tuple[str, str], ...], str]:
    if value is None:
        return (), ""
    if isinstance(value, str) or not isinstance(value, list | tuple):
        return (), f"{RANKING_IDENTITY_FIELDS_FIELD} must be a list of 'placeholder=field' entries"
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            return (), f"{RANKING_IDENTITY_FIELDS_FIELD} entries must be strings"
        placeholder, separator, source_field = item.partition(_FIELD_SEPARATOR)
        placeholder, source_field = placeholder.strip(), source_field.strip()
        if not separator or not placeholder or not source_field:
            return (), f"{RANKING_IDENTITY_FIELDS_FIELD} entry '{item}' must use the form 'placeholder=field'"
        if not placeholder.isidentifier():
            return (), f"{RANKING_IDENTITY_FIELDS_FIELD} placeholder '{placeholder}' is not a valid name"
        if placeholder in seen:
            return (), f"{RANKING_IDENTITY_FIELDS_FIELD} declares '{placeholder}' more than once"
        seen.add(placeholder)
        entries.append((placeholder, source_field))
    return tuple(entries), ""


def validate_ranking_identity_declaration(source: Mapping[str, Any]) -> str:
    """Return the declaration error for the identity route, if any."""
    template = source.get(RANKING_IDENTITY_URL_TEMPLATE_FIELD)
    fields, error = _parse_identity_fields(source.get(RANKING_IDENTITY_FIELDS_FIELD))
    if error:
        return error
    if template is None:
        if fields:
            return f"{RANKING_IDENTITY_FIELDS_FIELD} requires {RANKING_IDENTITY_URL_TEMPLATE_FIELD}"
        return ""
    if not isinstance(template, str) or not template.strip():
        return f"{RANKING_IDENTITY_URL_TEMPLATE_FIELD} must be a nonempty URL template"
    parts = urlsplit(template)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc:
        return f"{RANKING_IDENTITY_URL_TEMPLATE_FIELD} must be an absolute HTTP or HTTPS URL"
    if parts.fragment:
        return f"{RANKING_IDENTITY_URL_TEMPLATE_FIELD} must not contain a fragment"
    if parts.username is not None or parts.password is not None:
        return f"{RANKING_IDENTITY_URL_TEMPLATE_FIELD} must not contain credentials"
    try:
        _ = parts.port
    except ValueError:
        return f"{RANKING_IDENTITY_URL_TEMPLATE_FIELD} has an invalid port"
    if any("{" in component or "}" in component for component in (parts.scheme, parts.netloc, parts.query)):
        return f"{RANKING_IDENTITY_URL_TEMPLATE_FIELD} placeholders may only occur in the path"
    try:
        placeholders = _template_placeholders(template)
    except ValueError as exc:
        return f"{RANKING_IDENTITY_URL_TEMPLATE_FIELD} is malformed: {exc}"
    if any(not str(name).isidentifier() for name in placeholders):
        return f"{RANKING_IDENTITY_URL_TEMPLATE_FIELD} placeholders must be plain names"
    configured_url = str(source.get("url", "") or "").strip()
    if configured_url:
        try:
            _ = urlsplit(configured_url).port
        except ValueError:
            return f"{RANKING_IDENTITY_URL_TEMPLATE_FIELD} has an invalid configured or template port"
        if not same_origin_url(configured_url, template):
            return f"{RANKING_IDENTITY_URL_TEMPLATE_FIELD} must stay on the configured source host"
    declared = {placeholder for placeholder, _field in fields}
    missing = sorted(set(placeholders) - declared)
    if missing:
        return f"{RANKING_IDENTITY_URL_TEMPLATE_FIELD} uses undeclared placeholders: {', '.join(missing)}"
    unused = sorted(declared - set(placeholders))
    if unused:
        return f"{RANKING_IDENTITY_FIELDS_FIELD} declares unused placeholders: {', '.join(unused)}"
    # The identity check is a comparison against the declared category, so
    # without that marker there is nothing to compare the heading to.
    if not str(source.get("ranking_category_marker", "")).strip():
        return f"{RANKING_IDENTITY_URL_TEMPLATE_FIELD} requires ranking_category_marker"
    return ""


def ranking_identity_route(source: Mapping[str, Any]) -> RankingIdentityRoute | None:
    """Return the declared identity route, or ``None`` when none is declared.

    The caller must validate first.  An invalid declaration returns ``None``
    rather than a partially built route.
    """
    if validate_ranking_identity_declaration(source) or not ranking_identity_declared(source):
        return None
    fields, _error = _parse_identity_fields(source.get(RANKING_IDENTITY_FIELDS_FIELD))
    return RankingIdentityRoute(str(source[RANKING_IDENTITY_URL_TEMPLATE_FIELD]).strip(), fields)


def identity_heading_matches(
    headings: tuple[str, ...] | list[str], category_marker: str, required_tokens: tuple[str, ...] = ()
) -> bool:
    """Return whether one visible heading states the category and all tokens."""
    marker = " ".join(category_marker.split()).casefold()
    if not marker:
        return False
    for heading in headings:
        normalized = " ".join(str(heading).split()).casefold()
        if marker not in normalized:
            continue
        if all(
            re.search(rf"(?<![A-Za-z0-9]){re.escape(token.casefold())}(?![A-Za-z0-9])", normalized)
            for token in required_tokens
        ):
            return True
    return False
