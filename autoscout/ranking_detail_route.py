"""Bounded declarative detail routes for ranking-table sources.

This module owns the detail-route declaration and the bounded parser used by
all detail evidence consumers.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from html.parser import HTMLParser
from re import Pattern
from typing import Any
from urllib.parse import urljoin
from urllib.parse import urlsplit

from autoscout.source_metadata import RANKING_DETAIL_CONFIG_FIELDS
from autoscout.source_metadata import RANKING_DETAIL_REQUESTED_FIELDS
from autoscout.source_proposals import normalize_source_identity

RANKING_DETAIL_MAX_PAGES = 200
_VOID_TAGS = frozenset(
    {
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
)
_NON_RENDERED_TAGS = frozenset({"script", "style", "template", "noscript"})
_SELECTOR_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_ISBN_VALUE_RE = re.compile(r"^(?:97[89]\d{10}|\d{9}[\dXx])$")
_ISBN_CANDIDATE_RE = re.compile(
    r"(?<![\dXx])(?:97[89](?:[-\s.]?\d){10}|\d(?:[-\s.]?\d){8}[-\s.]?[\dXx])(?![\dXx])",
    flags=re.IGNORECASE,
)


def _normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_detail_text(value: str) -> str:
    return _normalize_whitespace(value).casefold()


def _selector_parts(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or not value.strip():
        return ()
    return tuple(part.strip()[1:] for part in value.split(">"))


def validate_detail_selector(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return f"{field} must be nonblank"
    parts = [part.strip() for part in value.split(">")]
    if any(not part.startswith(".") for part in parts):
        return f"{field} must contain only class-token selectors"
    if any(_SELECTOR_TOKEN_RE.fullmatch(part[1:]) is None for part in parts):
        return f"{field} contains an invalid class token"
    return ""


def ranking_detail_route_declared(source: Mapping[str, Any]) -> bool:
    return any(field in source and source.get(field) not in (None, "", []) for field in RANKING_DETAIL_CONFIG_FIELDS)


def ranking_detail_requested_fields(source: Mapping[str, Any]) -> tuple[str, ...]:
    value = source.get("ranking_detail_fields", [])
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip().casefold() for item in value if str(item).strip())


@dataclass(frozen=True)
class RankingDetailRoute:
    """Immutable parsed form of one bounded ranking detail declaration."""

    link_selector: str
    url_pattern_text: str
    url_pattern: Pattern[str]
    scope_selector: str
    requested_fields: tuple[str, ...]
    identifier_selector: str
    identifier_attribute: str
    identifier_pattern: str
    category_selector: str
    category_pattern: str
    category_allowlist: tuple[str, ...]
    max_pages: int
    # Issue #1163: detail pages one capture may lose without failing the source.
    max_failed_pages: int = 0

    @classmethod
    def from_source(cls, source: Mapping[str, Any]) -> RankingDetailRoute | None:
        if not ranking_detail_route_declared(source):
            return None
        pattern_text = str(source.get("ranking_detail_url_pattern", "")).strip()
        if not pattern_text:
            raise ValueError("ranking_detail_url_pattern must be a nonblank string")
        try:
            url_pattern = re.compile(pattern_text, flags=re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"ranking_detail_url_pattern is malformed: {exc}") from exc
        return cls(
            link_selector=str(source.get("ranking_detail_link_selector", "")).strip(),
            url_pattern_text=pattern_text,
            url_pattern=url_pattern,
            scope_selector=str(source.get("ranking_detail_scope_selector", "")).strip(),
            requested_fields=ranking_detail_requested_fields(source),
            identifier_selector=str(source.get("ranking_detail_identifier_selector", "")).strip(),
            identifier_attribute=str(source.get("ranking_detail_identifier_attribute", "")).strip(),
            identifier_pattern=str(source.get("ranking_detail_identifier_pattern", "")).strip(),
            category_selector=str(source.get("ranking_detail_category_selector", "")).strip(),
            category_pattern=str(source.get("ranking_detail_category_pattern", "")).strip(),
            category_allowlist=tuple(
                str(value).strip()
                for value in source.get("ranking_detail_category_allowlist", [])
                if isinstance(value, str) and value.strip()
            ),
            max_pages=source.get("ranking_detail_max_pages", 0)
            if isinstance(source.get("ranking_detail_max_pages", 0), int)
            and not isinstance(source.get("ranking_detail_max_pages", 0), bool)
            else 0,
            max_failed_pages=_declared_max_failed_pages(source, source.get("ranking_detail_max_pages", 0)),
        )

    @property
    def has_isbn(self) -> bool:
        return "isbn" in self.requested_fields

    @property
    def has_category(self) -> bool:
        return "category" in self.requested_fields

    def validate_page_count(self, count: int) -> str:
        if count > self.max_pages:
            return f"ranking detail route requires {count} pages but ranking_detail_max_pages is {self.max_pages}"
        return ""

    def resolve_detail_url(self, source_url: str, href: str) -> tuple[str, str]:
        """Resolve one row link under the route's same-domain URL policy."""
        href = href.strip()
        resolved = urljoin(source_url, href)
        parsed = urlsplit(resolved)
        if (
            not href
            or any(character.isspace() for character in href)
            or parsed.scheme.casefold() not in {"http", "https"}
            or not parsed.netloc
            or not parsed.hostname
        ):
            return "", "ranking row detail link is not a valid HTTP(S) URL"
        source_host = (urlsplit(source_url).hostname or "").casefold().removeprefix("www.")
        detail_host = (parsed.hostname or "").casefold().removeprefix("www.")
        if not source_host or source_host != detail_host:
            return "", "ranking row detail link is outside the configured source domain"
        if self.url_pattern.fullmatch(resolved) is None:
            return "", "ranking row detail link does not match the configured URL pattern"
        return resolved, ""

    @staticmethod
    def same_url(left: str, right: str) -> bool:
        return normalize_source_identity(left) == normalize_source_identity(right)

    @staticmethod
    def same_url_safely(left: str, right: str) -> bool:
        """``same_url``, reading a URL that cannot be parsed as matching nothing."""
        try:
            return RankingDetailRoute.same_url(left, right)
        except ValueError:
            return False


def _declared_max_failed_pages(source: Mapping[str, Any], max_pages: object) -> int:
    """The declared failed-page bound, or 0 (strict) for anything but a valid count.

    A malformed value, or one not below the page cap, never loosens the route:
    it parses as 0 here, so a path that reads the declaration without
    ``validate_ranking_detail_route`` stays strict, and validation refuses it.
    """
    value: object = source.get("ranking_detail_max_failed_pages", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or value >= max_pages:
        return 0
    return value


def validate_ranking_detail_route(source: Mapping[str, Any]) -> str:
    """Validate a declared route and preserve the established error wording."""
    if not ranking_detail_route_declared(source):
        return ""
    required = (
        "ranking_detail_link_selector",
        "ranking_detail_url_pattern",
        "ranking_detail_scope_selector",
        "ranking_detail_fields",
        "ranking_detail_max_pages",
    )
    missing = [field for field in required if field not in source or source.get(field) in (None, "", [])]
    if missing:
        return "ranking detail route is missing: " + ", ".join(missing)
    for field in ("ranking_detail_link_selector", "ranking_detail_scope_selector"):
        error = validate_detail_selector(source.get(field), field)
        if error:
            return error
    url_pattern = source.get("ranking_detail_url_pattern")
    if not isinstance(url_pattern, str) or not url_pattern.strip():
        return "ranking_detail_url_pattern must be a nonblank string"
    try:
        re.compile(url_pattern.strip(), flags=re.IGNORECASE)
    except re.error as exc:
        return f"ranking_detail_url_pattern is malformed: {exc}"
    fields = source.get("ranking_detail_fields")
    if not isinstance(fields, list) or not fields:
        return "ranking_detail_fields must be a non-empty list"
    if any(not isinstance(value, str) or not value.strip() for value in fields):
        return "ranking_detail_fields must contain non-empty strings"
    normalized_fields = [value.strip().casefold() for value in fields]
    if len(set(normalized_fields)) != len(normalized_fields):
        return "ranking_detail_fields must not contain duplicates"
    unknown = sorted(set(normalized_fields) - set(RANKING_DETAIL_REQUESTED_FIELDS))
    if unknown:
        return "ranking_detail_fields contains unsupported fields: " + ", ".join(unknown)
    max_pages = source.get("ranking_detail_max_pages")
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
        return "ranking_detail_max_pages must be a positive integer"
    if max_pages > RANKING_DETAIL_MAX_PAGES:
        return f"ranking_detail_max_pages must not exceed {RANKING_DETAIL_MAX_PAGES}"
    if "ranking_detail_max_failed_pages" in source:
        max_failed = source.get("ranking_detail_max_failed_pages")
        if isinstance(max_failed, bool) or not isinstance(max_failed, int) or max_failed < 0:
            return "ranking_detail_max_failed_pages must be a non-negative integer"
        if max_failed >= max_pages:
            return "ranking_detail_max_failed_pages must be less than ranking_detail_max_pages"

    identifier_declarations = (
        "ranking_detail_identifier_selector",
        "ranking_detail_identifier_attribute",
        "ranking_detail_identifier_pattern",
    )
    has_isbn = "isbn" in normalized_fields
    has_identifier = any(str(source.get(field, "")).strip() for field in identifier_declarations)
    if has_isbn:
        if not has_identifier:
            return "ranking detail ISBN requires one identifier selector, attribute, or pattern"
        selector_error = (
            validate_detail_selector(source.get("ranking_detail_identifier_selector"), "ranking_detail_identifier")
            if source.get("ranking_detail_identifier_selector")
            else ""
        )
        if selector_error:
            return selector_error
        if source.get("ranking_detail_identifier_pattern"):
            error = _validate_capture_pattern(source, "ranking_detail_identifier_pattern")
            if error:
                return error
    elif has_identifier:
        return "ranking detail identifier declarations require isbn in ranking_detail_fields"

    category_declarations = (
        "ranking_detail_category_selector",
        "ranking_detail_category_pattern",
        "ranking_detail_category_allowlist",
    )
    has_category = "category" in normalized_fields
    has_category_declaration = any(
        str(source.get(field, "")).strip() if field != "ranking_detail_category_allowlist" else source.get(field)
        for field in category_declarations
    )
    if has_category:
        selector = source.get("ranking_detail_category_selector")
        if not isinstance(selector, str) or not selector.strip():
            return "ranking detail category requires ranking_detail_category_selector"
        selector_error = validate_detail_selector(selector, "ranking_detail_category")
        if selector_error:
            return selector_error
        if (
            not isinstance(source.get("ranking_detail_category_pattern"), str)
            or not str(source.get("ranking_detail_category_pattern", "")).strip()
        ):
            return "ranking detail category requires ranking_detail_category_pattern"
        error = _validate_capture_pattern(source, "ranking_detail_category_pattern")
        if error:
            return error
        allowlist = source.get("ranking_detail_category_allowlist")
        if (
            not isinstance(allowlist, list)
            or not allowlist
            or any(not isinstance(value, str) or not value.strip() for value in allowlist)
        ):
            return "ranking_detail_category_allowlist must contain non-empty strings"
        if not str(source.get("eligible_candidate_category", "")).strip():
            return "ranking detail category requires a nonblank eligible_candidate_category"
    elif has_category_declaration:
        return "ranking detail category declarations require category in ranking_detail_fields"
    return ""


def _validate_capture_pattern(source: Mapping[str, Any], field: str) -> str:
    pattern = str(source.get(field, "")).strip()
    try:
        compiled = re.compile(pattern, flags=re.IGNORECASE)
    except re.error as exc:
        return f"{field} is malformed: {exc}"
    if compiled.groups == 0:
        return f"{field} must contain a capture group named or positional"
    return ""


@dataclass(frozen=True)
class RankingDetailScopeResult:
    html: str = ""
    visible_text: str = ""
    failure_reason: str = ""


@dataclass
class _DetailElement:
    tag: str
    attrs: list[tuple[str, str | None]]
    classes: frozenset[str]
    hidden: bool
    scope_match: bool = False
    field_matches: dict[str, bool] = field(default_factory=dict)
    text_parts: list[str] = field(default_factory=list)
    raw_parts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DetailSelectedElement:
    attrs: tuple[tuple[str, str | None], ...]
    hidden: bool
    text: str


@dataclass(frozen=True)
class BoundedDetailParseResult:
    scope: RankingDetailScopeResult
    selected: dict[str, tuple[DetailSelectedElement, ...]]


class BoundedDetailParser(HTMLParser):
    """Collect one visible bounded scope and configurable field elements."""

    def __init__(
        self,
        scope_selector: str,
        field_selectors: Mapping[str, str] | None = None,
        field_attributes: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.scope_selector = _selector_parts(scope_selector)
        self.field_selectors = {name: _selector_parts(value) for name, value in (field_selectors or {}).items()}
        self.field_attributes = {name: value.casefold() for name, value in (field_attributes or {}).items() if value}
        self.frames: list[_DetailElement] = []
        self.scope_matches: list[_DetailElement] = []
        self.selected: dict[str, list[_DetailElement]] = {name: [] for name in self.field_selectors}

    @staticmethod
    def _matches(selector: tuple[str, ...], classes: frozenset[str], ancestors: list[frozenset[str]]) -> bool:
        if not selector or selector[-1] not in classes:
            return False
        if len(selector) == 1:
            return True
        if len(ancestors) < len(selector) - 1:
            return False
        return all(
            wanted in actual for wanted, actual in zip(selector[:-1], ancestors[-(len(selector) - 1) :], strict=True)
        )

    def _active_scopes(self) -> list[_DetailElement]:
        return [frame for frame in self.frames if frame.scope_match]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        parent_hidden = any(frame.hidden for frame in self.frames)
        hidden = parent_hidden or tag in _NON_RENDERED_TAGS or _attributes_hidden(attrs)
        classes = frozenset(
            value for name, value in attrs if name.casefold() == "class" for value in (value or "").split()
        )
        ancestors = [frame.classes for frame in self.frames]
        frame = _DetailElement(
            tag=tag,
            attrs=attrs,
            classes=classes,
            hidden=hidden,
            scope_match=self._matches(self.scope_selector, classes, ancestors),
        )
        for active in self._active_scopes():
            active.raw_parts.append(self.get_starttag_text() or f"<{tag}>")
        if frame.scope_match:
            frame.raw_parts.append(self.get_starttag_text() or f"<{tag}>")
            self.scope_matches.append(frame)
        active_scopes = self._active_scopes() + ([frame] if frame.scope_match else [])
        if active_scopes:
            for name, selector in self.field_selectors.items():
                attribute = self.field_attributes.get(name, "")
                selector_matches = self._matches(selector, classes, ancestors) if selector else bool(attribute)
                attribute_matches = not attribute or any(
                    attr_name.casefold() == attribute and attr_value for attr_name, attr_value in attrs
                )
                if (
                    selector_matches
                    and attribute_matches
                    and any(scope in self.frames or scope is frame for scope in active_scopes)
                ):
                    frame.field_matches[name] = True
                    self.selected[name].append(frame)
        if tag not in _VOID_TAGS:
            self.frames.append(frame)

    def handle_data(self, data: str) -> None:
        if not self.frames:
            return
        hidden = any(frame.hidden for frame in self.frames)
        for frame in self.frames:
            if frame.scope_match:
                frame.raw_parts.append(data)
                if not hidden and data.strip():
                    frame.text_parts.append(data)
        if not hidden and data.strip():
            for selected in self.frames:
                if selected.field_matches:
                    selected.text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in _VOID_TAGS:
            return
        for index in range(len(self.frames) - 1, -1, -1):
            if self.frames[index].tag != tag:
                continue
            for active in self.frames:
                if active.scope_match:
                    active.raw_parts.append(f"</{tag}>")
            del self.frames[index:]
            break

    def result(self) -> BoundedDetailParseResult:
        if len(self.scope_matches) != 1:
            scope = RankingDetailScopeResult(
                failure_reason=("detail scope is missing" if not self.scope_matches else "detail scope is ambiguous")
            )
            return BoundedDetailParseResult(scope=scope, selected={name: () for name in self.field_selectors})
        matched_scope = self.scope_matches[0]
        if matched_scope.hidden:
            return BoundedDetailParseResult(
                scope=RankingDetailScopeResult(failure_reason="detail scope is hidden"),
                selected={name: () for name in self.field_selectors},
            )
        visible_text = _normalize_whitespace(" ".join(matched_scope.text_parts))
        if not visible_text:
            return BoundedDetailParseResult(
                scope=RankingDetailScopeResult(failure_reason="detail scope is empty"),
                selected={name: () for name in self.field_selectors},
            )
        selected = {
            name: tuple(
                DetailSelectedElement(
                    tuple(element.attrs), element.hidden, _normalize_whitespace(" ".join(element.text_parts))
                )
                for element in elements
            )
            for name, elements in self.selected.items()
        }
        return BoundedDetailParseResult(
            scope=RankingDetailScopeResult(html="".join(matched_scope.raw_parts), visible_text=visible_text),
            selected=selected,
        )


def _attributes_hidden(attrs: list[tuple[str, str | None]]) -> bool:
    for name, value in attrs:
        name = name.casefold()
        if name == "hidden":
            return True
        if name == "aria-hidden" and (value or "").strip().casefold() == "true":
            return True
        if name == "style":
            for declaration in (value or "").split(";"):
                property_name, separator, property_value = declaration.partition(":")
                if separator and property_name.strip().casefold() in {"display", "visibility"}:
                    if property_value.strip().casefold().split(" ", 1)[0] in {"none", "hidden"}:
                        return True
    return False


def parse_bounded_detail(
    html_content: str,
    scope_selector: str,
    field_selectors: Mapping[str, str] | None = None,
    field_attributes: Mapping[str, str] | None = None,
) -> BoundedDetailParseResult:
    try:
        scope_error = validate_detail_selector(scope_selector, "ranking_detail_scope_selector")
        if scope_error:
            return BoundedDetailParseResult(scope=RankingDetailScopeResult(failure_reason=scope_error), selected={})
        for field, selector in (field_selectors or {}).items():
            if selector:
                error = validate_detail_selector(selector, field)
                if error:
                    return BoundedDetailParseResult(scope=RankingDetailScopeResult(failure_reason=error), selected={})
        parser = BoundedDetailParser(scope_selector, field_selectors, field_attributes)
        parser.feed(html_content)
        parser.close()
        return parser.result()
    except (ValueError, re.error) as exc:
        return BoundedDetailParseResult(scope=RankingDetailScopeResult(failure_reason=str(exc)), selected={})


def ranking_detail_scope(html_content: str, selector: str) -> RankingDetailScopeResult:
    return parse_bounded_detail(html_content, selector).scope


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
    candidates = _ISBN_CANDIDATE_RE.findall(value) or [value]
    return [_normalized_isbn(candidate) for candidate in candidates]


def _pattern_value(value: str, pattern: str, field: str) -> tuple[str, str]:
    try:
        matches = list(re.finditer(pattern, value, flags=re.IGNORECASE))
    except re.error as exc:
        return "", f"{field} pattern is malformed: {exc}"
    if len(matches) != 1:
        return "", f"{field} pattern must match exactly once (matched {len(matches)})"
    match = matches[0]
    captured = match.group(field) if field in match.groupdict() else match.group(1)
    if not captured or not captured.strip():
        return "", f"{field} pattern capture is empty"
    return _normalize_whitespace(captured), ""


def _selected_value(
    parsed: BoundedDetailParseResult,
    field: str,
    selector: str,
    attribute: str,
) -> tuple[str, str]:
    if not selector and not attribute:
        return parsed.scope.visible_text, ""
    matches = [element for element in parsed.selected.get(field, ()) if not element.hidden]
    if len(matches) != 1:
        return "", f"detail {field} selector must match exactly one visible element"
    element = matches[0]
    if attribute:
        value = next((value for name, value in element.attrs if name.casefold() == attribute.casefold()), None)
        if not value:
            return "", f"detail {field} attribute is missing"
        return _normalize_whitespace(value), ""
    return element.text, ""


def ranking_detail_fields_from_html(
    html_content: str,
    route: RankingDetailRoute | Mapping[str, Any],
    *,
    enforce_category_allowlist: bool = True,
) -> tuple[dict[str, str], str]:
    """Extract declared fields from visible text in one bounded scope."""
    parsed_route = route if isinstance(route, RankingDetailRoute) else RankingDetailRoute.from_source(route)
    if parsed_route is None:
        return {}, "ranking detail route is not declared"
    field_selectors = {
        field: selector
        for field, selector in (
            ("identifier", parsed_route.identifier_selector),
            ("category", parsed_route.category_selector),
        )
        if selector or (field == "identifier" and parsed_route.identifier_attribute)
    }
    parsed = parse_bounded_detail(
        html_content,
        parsed_route.scope_selector,
        field_selectors,
        {"identifier": parsed_route.identifier_attribute} if parsed_route.identifier_attribute else None,
    )
    if parsed.scope.failure_reason:
        return {}, parsed.scope.failure_reason
    values: dict[str, str] = {}
    if parsed_route.has_isbn:
        value, error = _selected_value(
            parsed,
            "identifier",
            parsed_route.identifier_selector,
            parsed_route.identifier_attribute,
        )
        if error:
            return {}, error
        if parsed_route.identifier_pattern:
            value, error = _pattern_value(value, parsed_route.identifier_pattern, "identifier")
            if error:
                return {}, error
        identifiers = _isbn_values(value)
        if len(identifiers) != 1 or not identifiers[0]:
            return {}, "detail identifier is not exactly one valid ISBN"
        values["isbn"] = identifiers[0]
    if parsed_route.has_category:
        value, error = _selected_value(parsed, "category", parsed_route.category_selector, "")
        if error:
            return {}, error
        value, error = _pattern_value(value, parsed_route.category_pattern, "category")
        if error:
            return {}, error
        allowlist = {normalize_detail_text(item) for item in parsed_route.category_allowlist}
        if enforce_category_allowlist and normalize_detail_text(value) not in allowlist:
            return {}, "detail category is outside the exact allowlist"
        values["category"] = _normalize_whitespace(value)
    return values, ""


__all__ = [
    "RANKING_DETAIL_MAX_PAGES",
    "BoundedDetailParser",
    "RankingDetailRoute",
    "RankingDetailScopeResult",
    "normalize_detail_text",
    "parse_bounded_detail",
    "ranking_detail_fields_from_html",
    "ranking_detail_requested_fields",
    "ranking_detail_route_declared",
    "ranking_detail_scope",
    "validate_detail_selector",
    "validate_ranking_detail_route",
]
