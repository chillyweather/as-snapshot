"""Live probing for unconfigured Source Candidates.

This module is deliberately separate from the weekly workflow.
It performs one bounded HEAD and plain GET sequence for an undeclared
route, or each bounded declared POST page without HEAD, retry, or fallback.
Captured evidence is evaluated by the pure Issue #334 policy seam.
The only durable files it may write are the explicit capture directory, the
Source Candidate Ledger, and the explicit feasibility draft directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.parse import urlunsplit
from urllib.request import Request
from urllib.request import urlopen

from autoscout.http_decoding import MAX_RESPONSE_BYTES
from autoscout.http_decoding import decode_bounded_http_body
from autoscout.http_decoding import stream_http_text
from autoscout.http_evidence import HttpStatusEvidence
from autoscout.http_evidence import declared_head_404_fallback
from autoscout.http_evidence import validate_head_404_fallback_declaration
from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.ranking_evidence import RankingPageEvidence
from autoscout.ranking_identity import identity_heading_matches
from autoscout.ranking_identity import ranking_identity_route
from autoscout.ranking_identity import same_origin_url
from autoscout.ranking_identity import validate_ranking_identity_declaration
from autoscout.ranking_paging import PAGING_EMPTY
from autoscout.ranking_paging import PAGING_MISSING_FINAL
from autoscout.ranking_paging import PAGING_REPEATED
from autoscout.ranking_paging import PAGING_TOO_LARGE
from autoscout.ranking_paging import RankingPagingState
from autoscout.ranking_request import DEFAULT_RANKING_REQUEST
from autoscout.ranking_request import RankingRequest
from autoscout.ranking_request import ranking_request_from_source
from autoscout.ranking_request import validate_ranking_request_declaration
from autoscout.ranking_response import UnwrappedResponse
from autoscout.ranking_response import unwrap_ranking_response
from autoscout.ranking_response import validate_ranking_response_declaration
from autoscout.safe_artifacts import SafeArtifactError
from autoscout.safe_artifacts import StorageDestination
from autoscout.safe_artifacts import StorageDestinationKind
from autoscout.safe_artifacts import exclusive_temp
from autoscout.safe_artifacts import publish_pair
from autoscout.safe_artifacts import publish_single
from autoscout.safe_artifacts import resolve_storage_destination
from autoscout.safe_artifacts import staged_replace
from autoscout.source_collectors.base import CONTRACT_FAMILIES
from autoscout.source_feasibility import OUTCOME_ACCEPT
from autoscout.source_feasibility import OUTCOME_DEFER
from autoscout.source_feasibility import OUTCOME_REJECT
from autoscout.source_feasibility import REASON_UNRELATED_REDIRECT
from autoscout.source_feasibility import CapturedSourceEvidence
from autoscout.source_feasibility import FeasibilityVerdict
from autoscout.source_feasibility import evaluate_source_feasibility
from autoscout.source_feasibility import head_fallback_allowed
from autoscout.source_feasibility import validate_http_status_evidence
from autoscout.source_page_identity import parse_visible_page_identity
from autoscout.source_page_identity import ranking_declaration_diagnostics
from autoscout.source_page_identity import ranking_declaration_location_is_safe
from autoscout.source_page_identity import repeated_visible_sibling_diagnostics
from autoscout.source_page_identity import validate_entry_scope_selector
from autoscout.source_page_identity import validate_ranking_contract_metadata
from autoscout.source_page_identity import validate_visible_title_selector

DEFAULT_LEDGER_PATH = Path("data/source-candidate-ledger.json")
DEFAULT_FEASIBILITY_DIR = Path("data/source-feasibility-drafts")
REQUEST_TIMEOUT_SECONDS = 15.0
# The whole Independent Press Top 40 page, which is streamed rather than read once.
IPC_TOP40_PAGE_SECONDS = 120.0
_LEDGER_VERSION_1 = 1
_LEDGER_VERSION_2 = 2
_LEDGER_VERSION_3 = 3
# Deterministic nonblank proposing context for a caller that supplied none.
DEFAULT_PROPOSING_CONTEXT = "unspecified"
# Explicit legacy proposing context for version-1 entries whose context was
# not recorded at probe time (Issue #336).
LEGACY_PROPOSING_CONTEXT = "legacy-v1-unrecorded"
_LEDGER_ENTRY_FIELDS_VERSION_1 = frozenset(
    {
        "normalized_url",
        "probe_date",
        "url",
        "name",
        "signal_family",
        "outcome",
        "reason_code",
        "reason",
        "http_evidence",
        "head_status",
        "get_status",
        "resolved_url",
        "detected_heading",
        "bounded_entry_count",
        "raw_row_count",
        "samples",
    }
)
_LEDGER_ENTRY_FIELDS_VERSION_2 = _LEDGER_ENTRY_FIELDS_VERSION_1 | {
    "proposing_context",
    "contract_family",
    "contract_metadata",
    "ranking_diagnostics",
}
_LEDGER_ENTRY_FIELDS_VERSION_3 = _LEDGER_ENTRY_FIELDS_VERSION_2 | {
    "structural_diagnostics",
    "ranking_declaration_diagnostics",
}
_LEDGER_ENTRY_FIELDS_VERSION_3_CURRENT = _LEDGER_ENTRY_FIELDS_VERSION_3 | {"prior_verdicts"}
_LEDGER_REQUIRED_ENTRY_FIELDS_VERSION_1 = _LEDGER_ENTRY_FIELDS_VERSION_1 - {"raw_row_count"}
_LEDGER_REQUIRED_ENTRY_FIELDS_VERSION_2 = (_LEDGER_ENTRY_FIELDS_VERSION_1 - {"raw_row_count"}) | {"proposing_context"}
_LEDGER_REQUIRED_ENTRY_FIELDS_VERSION_3 = _LEDGER_REQUIRED_ENTRY_FIELDS_VERSION_2
_LEDGER_OUTCOMES = frozenset({OUTCOME_ACCEPT, OUTCOME_DEFER, OUTCOME_REJECT})
REASON_ALREADY_EVALUATED = "already_evaluated"
REASON_INVALID_URL = "invalid_url"
REASON_REQUEST_FAILED = "request_failed"
REASON_HEAD_REQUEST_FAILED = "head_request_failed"
REASON_GET_REQUEST_FAILED = "get_request_failed"
REASON_POST_REQUEST_FAILED = "post_request_failed"
REASON_RESPONSE_ENVELOPE = "response_envelope"
REASON_RANKING_PAGING = "ranking_paging"
REASON_IDENTITY_ROUTE = "identity_route"
REASON_RESPONSE_TOO_LARGE = "response_too_large"
REASON_LEDGER_ERROR = "ledger_error"


@dataclass(frozen=True)
class SourceCandidate:
    """One operator-provided Source Candidate, aligned into one object.

    The URL, signal family, proposing context, and feasibility contract travel
    together so probing never depends on position-coupled list arguments.
    ``metadata`` carries only declarative family fields and is retained with
    accepted evidence for offline revalidation.
    """

    url: str
    name: str = ""
    signal_family: str = ""
    proposing_context: str = DEFAULT_PROPOSING_CONTEXT
    visible_title_selector: str = ""
    entry_scope_selector: str = ""
    contract_family: str = ""
    reference_date: date | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _HTTPObservation:
    status: int | None = None
    resolved_url: str = ""
    body: str = ""
    body_bytes: int = 0
    truncated: bool = False
    decode_failure: str = ""
    error: str = ""


@dataclass(frozen=True)
class _ProbeCapture:
    """Accepted row HTML and its separate ranking identity capture."""

    evidence: RankingPageEvidence
    structured: Mapping[str, Any] | None = None


class SourceCandidateLedgerError(ValueError):
    """The durable Source Candidate Ledger cannot be read or written safely."""


# Compatibility aliases: the destination validation policy lives in
# ``autoscout.safe_artifacts``; these names keep internal call sites stable.
_StorageDestinationKind = StorageDestinationKind
_StorageDestination = StorageDestination


def _verdict(
    outcome: str,
    reason_code: str,
    reason: str,
    *,
    detected_heading: str = "",
    bounded_entry_count: int = 0,
) -> FeasibilityVerdict:
    return FeasibilityVerdict(
        accepted=outcome == OUTCOME_ACCEPT,
        reason=reason,
        outcome=outcome,
        reason_code=reason_code,
        detected_heading=detected_heading,
        bounded_entry_count=bounded_entry_count,
    )


def _error_text(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or type(exc).__name__


def _status(response: Any) -> int | None:
    """Read an HTTP status from urllib responses and small test doubles."""
    value = getattr(response, "status", None)
    if callable(value):
        value = value()
    if value is None:
        getter = getattr(response, "getcode", None)
        if callable(getter):
            value = getter()
    if value is None:
        value = getattr(response, "code", None)
    if value is None:
        value = getattr(response, "status_code", None)
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _resolved_url(response: Any, fallback: str) -> str:
    getter = getattr(response, "geturl", None)
    if callable(getter):
        try:
            value = getter()
        except Exception:
            value = ""
        if value:
            return str(value)
    value = getattr(response, "url", "")
    return str(value or fallback)


def _close(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def _bounded_utf8(text: str, limit: int = MAX_RESPONSE_BYTES) -> str:
    """Keep the longest prefix whose UTF-8 representation fits *limit* bytes."""
    encoded = text.encode("utf-8", errors="replace")
    normalized = encoded.decode("utf-8")
    if len(encoded) <= limit:
        return normalized
    retained: list[str] = []
    retained_bytes = 0
    for character in normalized:
        character_bytes = len(character.encode("utf-8", errors="replace"))
        if retained_bytes + character_bytes > limit:
            break
        retained.append(character)
        retained_bytes += character_bytes
    return "".join(retained)


def _read_bounded(response: Any) -> tuple[str, int, bool]:
    """Read at most MAX_RESPONSE_BYTES plus one marker byte."""
    decoded = decode_bounded_http_body(response, MAX_RESPONSE_BYTES)
    return decoded.text, decoded.byte_count, decoded.truncated


def _open_response(opener: Callable[..., Any], request: Request) -> Any:
    return opener(request, timeout=REQUEST_TIMEOUT_SECONDS)


def _request_observation(
    url: str,
    method: str,
    opener: Callable[..., Any],
    consume: Callable[[Any, str], _HTTPObservation],
    ranking_request: RankingRequest = DEFAULT_RANKING_REQUEST,
) -> _HTTPObservation:
    headers = {
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Encoding": "identity",
        "User-Agent": "AutoScout Source Candidate probe",
    }
    body: bytes | None = None
    if method == "POST":
        headers.update(ranking_request.headers)
        body = ranking_request.body
    request = Request(url, data=body, method=method, headers=headers)
    try:
        response = _open_response(opener, request)
    except HTTPError as exc:
        http_status = int(exc.code) if isinstance(exc.code, int) else None
        _close(exc)
        return _HTTPObservation(
            status=http_status,
            resolved_url=_resolved_url(exc, url),
            error=f"HTTP {http_status}",
        )
    except (OSError, URLError, TimeoutError, ValueError) as exc:
        return _HTTPObservation(error=_error_text(exc))
    except Exception as exc:
        return _HTTPObservation(error=_error_text(exc))
    try:
        return consume(response, url)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        return _HTTPObservation(
            status=_status(response), resolved_url=_resolved_url(response, url), error=_error_text(exc)
        )
    except Exception as exc:
        return _HTTPObservation(
            status=_status(response), resolved_url=_resolved_url(response, url), error=_error_text(exc)
        )
    finally:
        _close(response)


def _consume_head(response: Any, url: str) -> _HTTPObservation:
    return _HTTPObservation(status=_status(response), resolved_url=_resolved_url(response, url))


def _consume_get(response: Any, url: str) -> _HTTPObservation:
    status = _status(response)
    resolved_url = _resolved_url(response, url)
    if status != 200:
        return _HTTPObservation(status=status, resolved_url=resolved_url)
    decoded = decode_bounded_http_body(response, MAX_RESPONSE_BYTES)
    return _HTTPObservation(
        status=status,
        resolved_url=resolved_url,
        body=decoded.text,
        body_bytes=decoded.byte_count,
        truncated=decoded.truncated,
        decode_failure=decoded.decode_failure,
    )


def _fetch_head(url: str, opener: Callable[..., Any]) -> _HTTPObservation:
    return _request_observation(url, "HEAD", opener, _consume_head)


def _fetch_get(url: str, opener: Callable[..., Any]) -> _HTTPObservation:
    return _request_observation(url, "GET", opener, _consume_get)


def _fetch_post(url: str, opener: Callable[..., Any], ranking_request: RankingRequest) -> _HTTPObservation:
    """Issue one bounded declared form POST page for a ranking route.

    Each page is requested once. There is no HEAD request, retry, or method
    fallback.
    """
    return _request_observation(url, "POST", opener, _consume_get, ranking_request)


def valid_http_url(value: Any) -> bool:
    """Return whether *value* is a valid HTTP(S) URL for source identity use."""
    if not isinstance(value, str) or not value.strip() or any(character.isspace() for character in value):
        return False
    try:
        parts = urlsplit(value)
        if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc or not parts.hostname:
            return False
        _ = parts.port
    except ValueError:
        return False
    return True


def normalized_url(url: str) -> str:
    """Return the deterministic URL identity used by the ledger."""
    if not valid_http_url(url):
        return "raw:" + url.strip()
    parts = urlsplit(url)
    hostname = (parts.hostname or "").casefold()
    default_port = {"http": 80, "https": 443}[parts.scheme.casefold()]
    netloc = (
        hostname
        if (parts.port if parts.port is not None else default_port) == default_port
        else f"{hostname}:{parts.port}"
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.casefold(), netloc, path, parts.query, ""))


def _http_evidence(head: _HTTPObservation, get: _HTTPObservation | None = None, method: str = "GET") -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "head": {
            "status": head.status,
            "resolved_url": head.resolved_url,
        },
        "get": {
            "status": get.status if get is not None else None,
            "resolved_url": get.resolved_url if get is not None else "",
        },
    }
    if head.error:
        evidence["head"]["error"] = head.error
    if get is not None:
        evidence["get"]["body_bytes"] = get.body_bytes
        evidence["get"]["truncated"] = get.truncated
        if get.error:
            evidence["get"]["error"] = get.error
        elif get.decode_failure:
            evidence["get"]["error"] = get.decode_failure
    if method != "GET":
        evidence["get"]["method"] = method
    return evidence


_DIAGNOSTIC_SENSITIVE_RE = re.compile(
    r"(?:isbn|ean|identifier|barcode|sku|sales?|units?|price|pre[cç]o|vendas?|r\$|€)", flags=re.IGNORECASE
)
_DIAGNOSTIC_ISBN_RE = re.compile(r"(?<![\dXx])(?:97[89](?:[-\s.]?\d){10}|\d(?:[-\s.]?\d){8}[-\s.]?[\dXx])(?![\dXx])")


def _safe_diagnostic_text(value: object, *, allow_rank: bool = False) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    text = _DIAGNOSTIC_ISBN_RE.sub("", text)
    if _DIAGNOSTIC_SENSITIVE_RE.search(text):
        return ""
    words = text.split()
    if len(words) > 10:
        return ""
    if any(re.search(r"\d", word) for word in words):
        if not allow_rank:
            return ""
        words = [word for index, word in enumerate(words) if not re.search(r"\d", word) or index == 0]
        text = " ".join(words)
    return text[:72]


def _sanitize_ranking_diagnostics(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    row_index = value.get("row_index")
    if isinstance(row_index, bool) or not isinstance(row_index, int) or not 1 <= row_index <= 1000:
        return None
    raw_tokens = value.get("class_tokens", [])
    tokens = (
        [
            token[:48]
            for token in raw_tokens
            if isinstance(token, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", token)
        ]
        if isinstance(raw_tokens, list)
        else []
    )
    tokens = [token for token in tokens if _DIAGNOSTIC_SENSITIVE_RE.search(token) is None][:24]
    raw_elements = value.get("visible_elements", [])
    elements: list[dict[str, object]] = []
    element_token_count = 0
    if isinstance(raw_elements, list):
        for raw_element in raw_elements[:12]:
            if not isinstance(raw_element, Mapping):
                continue
            tag = raw_element.get("tag")
            element_tokens = raw_element.get("class_tokens", [])
            if not isinstance(tag, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", tag) is None:
                continue
            if not isinstance(element_tokens, list):
                continue
            remaining_tokens = 24 - element_token_count
            if remaining_tokens <= 0:
                break
            safe_tokens = [
                token[:48]
                for token in element_tokens
                if isinstance(token, str)
                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", token)
                and _DIAGNOSTIC_SENSITIVE_RE.search(token) is None
            ][: min(8, remaining_tokens)]
            element_token_count += len(safe_tokens)
            elements.append(
                {
                    "tag": tag,
                    "class_tokens": safe_tokens,
                    "visible_text": _safe_diagnostic_text(raw_element.get("visible_text"), allow_rank=not elements),
                }
            )
    return {
        "row_index": row_index,
        "visible_text": _safe_diagnostic_text(value.get("visible_text"), allow_rank=True)[:160],
        "class_tokens": tokens,
        "visible_elements": elements,
    }


_STRUCTURAL_DIAGNOSTIC_MAX_STRUCTURES = 12
_STRUCTURAL_DIAGNOSTIC_MAX_CLASS_TOKENS = 24
_DIAGNOSTIC_TAG_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]*")
_DIAGNOSTIC_CLASS_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


_RANKING_DECLARATION_STATUSES = frozenset({"calendar_date_range", "week_ordinal", "not_found"})
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
_RANKING_DECLARATION_TAG_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]*")


def _sanitize_ranking_declaration_diagnostics(value: object) -> dict[str, Any] | None:
    """Return the strict canonical Issue #587 diagnostic shape."""
    if not isinstance(value, Mapping) or set(value) != {"reporting_period", "identifier_candidates"}:
        return None
    reporting = value.get("reporting_period")
    identifiers = value.get("identifier_candidates")
    if not isinstance(reporting, Mapping) or set(reporting) != {"status", "candidates"}:
        return None
    status = reporting.get("status")
    candidates = reporting.get("candidates")
    if not isinstance(status, str) or status not in _RANKING_DECLARATION_STATUSES:
        return None
    if not isinstance(candidates, list) or len(candidates) > 4:
        return None
    canonical_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or set(candidate) != {"text", "pattern", "date_formats"}:
            return None
        text = candidate.get("text")
        pattern = candidate.get("pattern")
        formats = candidate.get("date_formats")
        if (
            not isinstance(text, str)
            or not isinstance(pattern, str)
            or not isinstance(formats, list)
            or not 1 <= len(text) <= 96
            or not 1 <= len(pattern) <= 96
            or len(formats) > 2
            or any(not isinstance(item, str) for item in formats)
        ):
            return None
        expected = ranking_declaration_diagnostics(f"<h1>{text}</h1>")["reporting_period"]
        if not isinstance(expected, Mapping) or expected.get("status") != "calendar_date_range":
            return None
        expected_candidates = expected.get("candidates")
        if not isinstance(expected_candidates, list) or len(expected_candidates) != 1:
            return None
        if dict(candidate) != expected_candidates[0]:
            return None
        canonical_candidates.append({"text": text, "pattern": pattern, "date_formats": list(formats)})
    if status != "calendar_date_range" and canonical_candidates:
        return None
    if status == "calendar_date_range" and not canonical_candidates:
        return None
    period_keys = [_canonical_diagnostic_mapping(item) for item in canonical_candidates]
    if len(set(period_keys)) != len(period_keys):
        return None
    if not isinstance(identifiers, list) or len(identifiers) > 8:
        return None
    canonical_identifiers: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for candidate in identifiers:
        if not isinstance(candidate, Mapping) or set(candidate) != {
            "kind",
            "source",
            "tag",
            "class_tokens",
            "attribute",
            "value_shape",
        }:
            return None
        kind = candidate.get("kind")
        source = candidate.get("source")
        tag = candidate.get("tag")
        class_tokens = candidate.get("class_tokens")
        attribute = candidate.get("attribute")
        value_shape = candidate.get("value_shape")
        if (
            kind not in {"isbn", "internal_item_id"}
            or source not in {"text", "attribute"}
            or not isinstance(tag, str)
            or len(tag) > 32
            or tag != tag.casefold()
            or _RANKING_DECLARATION_TAG_RE.fullmatch(tag) is None
            or not isinstance(class_tokens, list)
            or len(class_tokens) > 4
            or any(
                not isinstance(token, str)
                or len(token) > 48
                or _RANKING_DECLARATION_CLASS_RE.fullmatch(token) is None
                or re.search(r"(?:title|author|description|review|prose|article|body)", token, re.IGNORECASE)
                for token in class_tokens
            )
            or len(set(class_tokens)) != len(class_tokens)
            or not isinstance(attribute, str)
            or len(attribute) > 32
            or attribute != attribute.casefold()
            or "_" in attribute
            or not isinstance(value_shape, str)
        ):
            return None
        if not ranking_declaration_location_is_safe(tag, class_tokens):
            return None
        if kind == "isbn":
            if value_shape not in {"isbn-10", "isbn-13"}:
                return None
            # An ISBN reaches the ledger from the row text, where it carries no
            # attribute name, or from an allowlisted attribute. Both are the
            # same identifier and neither exposes the value itself.
            if source == "text":
                if attribute:
                    return None
            elif attribute not in _RANKING_DECLARATION_IDENTIFIER_ATTRIBUTES:
                return None
        if kind == "internal_item_id":
            if source != "attribute" or attribute not in _RANKING_DECLARATION_IDENTIFIER_ATTRIBUTES:
                return None
            shape_match = re.fullmatch(r"(?:digits|alphanumeric):([1-9][0-9]{0,2})", value_shape)
            if shape_match is None or int(shape_match.group(1)) > 64:
                return None
        normalized = {
            "kind": kind,
            "source": source,
            "tag": tag,
            "class_tokens": list(class_tokens),
            "attribute": attribute,
            "value_shape": value_shape,
        }
        identity = (
            normalized["kind"],
            normalized["source"],
            normalized["tag"],
            tuple(normalized["class_tokens"]),
            normalized["attribute"],
            normalized["value_shape"],
        )
        if identity in seen:
            return None
        seen.add(identity)
        canonical_identifiers.append(normalized)
    return {
        "reporting_period": {
            "status": status,
            "candidates": canonical_candidates,
        },
        "identifier_candidates": canonical_identifiers,
    }


def _canonical_diagnostic_mapping(value: Mapping[str, Any]) -> tuple[Any, ...]:
    return (value.get("text"), value.get("pattern"), tuple(value.get("date_formats", [])))


def _sanitize_structural_signature(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or set(value) != {"tag", "class_tokens"}:
        return None
    tag = value.get("tag")
    raw_tokens = value.get("class_tokens")
    if not isinstance(tag, str) or _DIAGNOSTIC_TAG_RE.fullmatch(tag) is None:
        return None
    if not isinstance(raw_tokens, list):
        return None
    safe_tokens = [
        token[:48]
        for token in raw_tokens
        if isinstance(token, str)
        and _DIAGNOSTIC_CLASS_RE.fullmatch(token)
        and _DIAGNOSTIC_SENSITIVE_RE.search(token) is None
    ][:_STRUCTURAL_DIAGNOSTIC_MAX_CLASS_TOKENS]
    return {"tag": tag, "class_tokens": safe_tokens}


def _structural_sort_key(value: Mapping[str, Any]) -> tuple[object, ...]:
    parent = value["parent"]
    child = value["child"]
    return (
        parent["tag"],
        tuple(parent["class_tokens"]),
        child["tag"],
        tuple(child["class_tokens"]),
    )


def _sanitize_structural_diagnostics(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or set(value) != {"repeated_siblings"}:
        return None
    raw_structures = value.get("repeated_siblings")
    if not isinstance(raw_structures, list):
        return None
    candidates: list[dict[str, Any]] = []
    for raw_structure in raw_structures:
        if not isinstance(raw_structure, Mapping) or set(raw_structure) != {"parent", "child", "count"}:
            return None
        count = raw_structure.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 1000:
            return None
        parent = _sanitize_structural_signature(raw_structure.get("parent"))
        child = _sanitize_structural_signature(raw_structure.get("child"))
        if parent is None or child is None or not child["class_tokens"]:
            continue
        candidates.append({"parent": parent, "child": child, "count": count})
    candidates.sort(key=lambda item: (-int(item["count"]), _structural_sort_key(item)))
    selected = candidates[:_STRUCTURAL_DIAGNOSTIC_MAX_STRUCTURES]
    if not selected:
        return {"repeated_siblings": []}

    # Give every selected child one class token before filling the remaining
    # bounded budget, so a useful selector cannot be displaced by parent classes.
    output: list[dict[str, Any]] = []
    remaining_tokens = _STRUCTURAL_DIAGNOSTIC_MAX_CLASS_TOKENS
    for item in selected:
        child_tokens = item["child"]["class_tokens"]
        if remaining_tokens <= 0 or not child_tokens:
            break
        child = {"tag": item["child"]["tag"], "class_tokens": child_tokens[:1]}
        remaining_tokens -= 1
        output.append(
            {"parent": {"tag": item["parent"]["tag"], "class_tokens": []}, "child": child, "count": item["count"]}
        )

    for item, bounded in zip(selected, output, strict=False):
        child_tokens = item["child"]["class_tokens"]
        parent_tokens = item["parent"]["class_tokens"]
        child = bounded["child"]["class_tokens"]
        extra_child = child_tokens[len(child) : remaining_tokens]
        child.extend(extra_child)
        remaining_tokens -= len(extra_child)
        parent = bounded["parent"]["class_tokens"]
        parent.extend(parent_tokens[:remaining_tokens])
        remaining_tokens -= len(parent)

    return {"repeated_siblings": output}


def _attach_structural_diagnostics(verdict: FeasibilityVerdict, html: str) -> FeasibilityVerdict:
    if verdict.outcome == OUTCOME_ACCEPT or not html:
        return verdict
    return replace(verdict, structural_diagnostics=repeated_visible_sibling_diagnostics(html))


def _attach_ranking_declaration_diagnostics(
    verdict: FeasibilityVerdict, candidate: SourceCandidate, html: str
) -> FeasibilityVerdict:
    if candidate.contract_family != "ranking-table" or verdict.outcome == OUTCOME_ACCEPT or not html:
        return verdict
    return replace(verdict, ranking_declaration_diagnostics=ranking_declaration_diagnostics(html))


def _samples(verdict: FeasibilityVerdict) -> list[dict[str, str]]:
    values: list[dict[str, str]] = []
    for sample in verdict.samples:
        value = {"title": sample.title, "author": sample.author, "url": sample.url}
        for field_name in ("rank", "identifier", "category", "reporting_period"):
            field_value = str(getattr(sample, field_name, ""))
            if field_value:
                value[field_name] = field_value
        values.append(value)
    return values


def _result(
    candidate: SourceCandidate,
    verdict: FeasibilityVerdict,
    *,
    head: _HTTPObservation,
    get: _HTTPObservation | None = None,
    prior: Mapping[str, Any] | None = None,
    method: str = "GET",
) -> dict[str, Any]:
    evidence = _http_evidence(head, get, method)
    result: dict[str, Any] = {
        "url": candidate.url,
        "name": candidate.name,
        "signal_family": candidate.signal_family,
        "outcome": verdict.outcome,
        "reason_code": verdict.reason_code,
        "reason": verdict.reason,
        "http_evidence": evidence,
        "detected_heading": verdict.detected_heading,
        "bounded_entry_count": verdict.bounded_entry_count,
        "raw_row_count": verdict.raw_row_count,
        "samples": _samples(verdict),
        "head_status": head.status,
        "get_status": get.status if get is not None else None,
        "resolved_url": (get.resolved_url if get is not None else head.resolved_url) or candidate.url,
    }
    ranking_diagnostics = _sanitize_ranking_diagnostics(verdict.ranking_diagnostics)
    if ranking_diagnostics is not None:
        result["ranking_diagnostics"] = ranking_diagnostics
    declaration_diagnostics = _sanitize_ranking_declaration_diagnostics(verdict.ranking_declaration_diagnostics)
    if declaration_diagnostics is not None:
        result["ranking_declaration_diagnostics"] = declaration_diagnostics
    structural_diagnostics = _sanitize_structural_diagnostics(verdict.structural_diagnostics)
    if structural_diagnostics is not None:
        result["structural_diagnostics"] = structural_diagnostics
    if candidate.contract_family:
        result["contract_family"] = candidate.contract_family
    if candidate.metadata:
        result["contract_metadata"] = dict(candidate.metadata)
    if verdict.contract_family:
        result["contract_family"] = verdict.contract_family
    if verdict.ranking_category:
        result["ranking_category"] = verdict.ranking_category
    if verdict.reporting_period:
        result["reporting_period"] = verdict.reporting_period
    if prior is not None:
        result["prior_outcome"] = prior.get("outcome")
        result["prior_reason_code"] = prior.get("reason_code", "")
        result["prior_probe_date"] = prior.get("probe_date", "")
    if candidate.visible_title_selector:
        result["visible_title_selector"] = candidate.visible_title_selector
    if candidate.entry_scope_selector:
        result["entry_scope_selector"] = candidate.entry_scope_selector
    return result


def _probe_one(
    candidate: SourceCandidate,
    opener: Callable[..., Any],
) -> tuple[dict[str, Any], _ProbeCapture | None]:
    """Probe one candidate and return its result and bounded accepted evidence."""
    empty_head = _HTTPObservation()
    if not valid_http_url(candidate.url):
        verdict = _verdict(OUTCOME_REJECT, REASON_INVALID_URL, "URL is not a valid HTTP or HTTPS URL")
        return _result(candidate, verdict, head=empty_head), None

    if candidate.contract_family == "bookinfo-weekly-json":
        from autoscout.bookinfo_ranking import BookInfoError
        from autoscout.bookinfo_ranking import capture
        from autoscout.bookinfo_ranking import feasibility
        from autoscout.bookinfo_ranking import page_data

        source = {**candidate.metadata, "url": candidate.url, "contract_family": candidate.contract_family}

        def fetch_json(target: str) -> dict[str, Any]:
            observed = _request_observation(target, "GET", opener, _consume_get)
            return {
                "status": observed.status,
                "resolved_url": observed.resolved_url,
                "body": observed.body,
                "truncated": observed.truncated,
            }

        try:
            if candidate.reference_date is None:
                raise BookInfoError("bookinfo_evidence_identity", "BookInfo probe needs a reference date")
            bundle = capture(source, candidate.reference_date, fetch_json)
            page = page_data(bundle, source)
            verdict = feasibility(page, source)
            ranking = bundle["ranking"]
            result = _result(
                candidate,
                verdict,
                head=empty_head,
                get=_HTTPObservation(
                    status=200, resolved_url=ranking["resolved_url"], body_bytes=len(ranking["body"].encode("utf-8"))
                ),
            )
            return result, (_ProbeCapture(RankingPageEvidence(), page) if verdict.accepted else None)
        except BookInfoError as exc:
            return _result(candidate, _verdict(OUTCOME_REJECT, exc.reason_code, str(exc)), head=empty_head), None

    if candidate.contract_family == "ipc-top40-csv":
        from autoscout.ipc_top40_ranking import PAGE_CEILING_BYTES as IPC_TOP40_PAGE_CEILING_BYTES
        from autoscout.ipc_top40_ranking import IpcTop40Error
        from autoscout.ipc_top40_ranking import capture as ipc_top40_capture
        from autoscout.ipc_top40_ranking import feasibility as ipc_top40_feasibility
        from autoscout.ipc_top40_ranking import page_data as ipc_top40_page_data

        source = {**candidate.metadata, "url": candidate.url, "contract_family": candidate.contract_family}

        def fetch_text(target: str) -> dict[str, Any]:
            observed = _request_observation(target, "GET", opener, _consume_get)
            return {
                "status": observed.status,
                "resolved_url": observed.resolved_url,
                "body": observed.body,
                "truncated": observed.truncated,
            }

        def scan_page(target: str, consume: Callable[[str], None]) -> dict[str, Any]:
            def consume_streamed(response: Any, url: str) -> _HTTPObservation:
                status = _status(response)
                resolved_url = _resolved_url(response, url)
                if status != 200:
                    return _HTTPObservation(status=status, resolved_url=resolved_url)
                # The socket timeout bounds one read; this bounds a body that keeps trickling in.
                deadline = time.monotonic() + IPC_TOP40_PAGE_SECONDS
                streamed = stream_http_text(
                    response,
                    IPC_TOP40_PAGE_CEILING_BYTES,
                    consume,
                    remaining_seconds=lambda: deadline - time.monotonic(),
                )
                if streamed.timed_out:
                    return _HTTPObservation(status=status, resolved_url=resolved_url, error="page read timed out")
                return _HTTPObservation(
                    status=status,
                    resolved_url=resolved_url,
                    body_bytes=streamed.byte_count,
                    truncated=streamed.truncated or bool(streamed.decode_failure),
                    decode_failure=streamed.decode_failure,
                )

            observed = _request_observation(target, "GET", opener, consume_streamed)
            # A request that failed part-way fed the scan only part of the page.
            return {
                "status": observed.status,
                "resolved_url": observed.resolved_url,
                "truncated": observed.truncated or bool(observed.error),
            }

        try:
            if candidate.reference_date is None:
                raise IpcTop40Error("ipc_top40_evidence_identity", "Independent Press Top 40 probe needs a date")
            bundle = ipc_top40_capture(source, candidate.reference_date, fetch_text, scan_page)
            page = ipc_top40_page_data(bundle, source)
            verdict = ipc_top40_feasibility(page, source)
            observed_file = bundle["file"]
            result = _result(
                candidate,
                verdict,
                head=empty_head,
                get=_HTTPObservation(
                    status=200,
                    resolved_url=observed_file["url"],
                    body_bytes=len(observed_file["body"].encode("utf-8")),
                ),
            )
            return result, (_ProbeCapture(RankingPageEvidence(), page) if verdict.accepted else None)
        except IpcTop40Error as exc:
            return _result(candidate, _verdict(OUTCOME_REJECT, exc.reason_code, str(exc)), head=empty_head), None

    if candidate.contract_family == "perlentaucher-daily-digest":
        from autoscout.perlentaucher_digest import PerlentaucherError
        from autoscout.perlentaucher_digest import capture as perlentaucher_capture
        from autoscout.perlentaucher_digest import feasibility as perlentaucher_feasibility
        from autoscout.perlentaucher_digest import page_data as perlentaucher_page_data

        source = {**candidate.metadata, "url": candidate.url, "contract_family": candidate.contract_family}

        def fetch_page(target: str) -> dict[str, Any]:
            observed = _request_observation(target, "GET", opener, _consume_get)
            return {
                "status": observed.status,
                "resolved_url": observed.resolved_url,
                "body": observed.body,
                "truncated": observed.truncated,
            }

        try:
            if candidate.reference_date is None:
                raise PerlentaucherError("perlentaucher_evidence_identity", "Perlentaucher probe needs a date")
            # A probe has no weekly state, so it reads no seen-item index.
            bundle = perlentaucher_capture(source, candidate.reference_date, fetch_page)
            page = perlentaucher_page_data(bundle, source)
            verdict = perlentaucher_feasibility(page, source)
            result = _result(
                candidate,
                verdict,
                head=empty_head,
                get=_HTTPObservation(
                    status=200,
                    resolved_url=bundle["index"]["url"],
                    body_bytes=sum(observed["body_bytes"] for observed in bundle["daily_pages"]),
                ),
            )
            return result, (_ProbeCapture(RankingPageEvidence(), page) if verdict.accepted else None)
        except PerlentaucherError as exc:
            return _result(candidate, _verdict(OUTCOME_REJECT, exc.reason_code, str(exc)), head=empty_head), None

    if candidate.contract_family == "kirkus-coming-soon":
        from autoscout import kirkus_listing

        source = {**candidate.metadata, "url": candidate.url, "contract_family": candidate.contract_family}

        # The default transport follows redirects, which would send a second request with no delay
        # before it, to a URL this contract never chose. An injected transport is a test's.
        listing_opener = kirkus_listing.open_without_redirect if opener is urlopen else opener

        def fetch_listing(target: str) -> dict[str, Any]:
            observed = _request_observation(target, "GET", listing_opener, _consume_get)
            return {
                "status": observed.status,
                "resolved_url": observed.resolved_url,
                "body": observed.body,
                "truncated": observed.truncated,
            }

        try:
            if candidate.reference_date is None:
                raise kirkus_listing.KirkusError("kirkus_evidence_identity", "Kirkus probe needs a date")
            # A probe is one pre-collection pass with the declared delay and caps. It has no weekly
            # state, so it reads no earlier pass and no seen-item index.
            collection = kirkus_listing.collect(
                source, candidate.reference_date, fetch_listing, lambda seconds: kirkus_listing.default_pause(seconds)
            )
            bundle = kirkus_listing.capture(source, candidate.reference_date, collection)
            page = kirkus_listing.page_data(bundle, source)
            verdict = kirkus_listing.feasibility(page, source)
            result = _result(
                candidate,
                verdict,
                head=empty_head,
                get=_HTTPObservation(
                    status=200,
                    resolved_url=collection["listing_pages"][0]["url"],
                    body_bytes=sum(observed["body_bytes"] for observed in collection["listing_pages"]),
                ),
            )
            return result, (_ProbeCapture(RankingPageEvidence(), page) if verdict.accepted else None)
        except kirkus_listing.KirkusError as exc:
            return _result(candidate, _verdict(OUTCOME_REJECT, exc.reason_code, str(exc)), head=empty_head), None

    http_source = {
        **candidate.metadata,
        "metadata_ranking_table": candidate.contract_family == "ranking-table",
        "contract_family": candidate.contract_family,
    }
    for error in (
        validate_head_404_fallback_declaration(http_source),
        validate_ranking_request_declaration(candidate.metadata),
        validate_ranking_response_declaration(candidate.metadata),
        validate_ranking_identity_declaration(candidate.metadata),
    ):
        if error:
            verdict = _verdict(OUTCOME_REJECT, REASON_REQUEST_FAILED, f"ranking declaration is invalid: {error}")
            return _result(candidate, verdict, head=empty_head), None

    ranking_request = ranking_request_from_source(candidate.metadata)
    if ranking_request.is_post:
        # A declared form route answers only its own POST. A HEAD preflight
        # would describe a different response, so the probe skips it and
        # records each bounded POST page as a body-bearing observation.
        return _probe_declared_request(candidate, opener, ranking_request)

    allow_head_404 = declared_head_404_fallback(http_source)
    head = _fetch_head(candidate.url, opener)
    if head.error and head.status is None:
        verdict = _verdict(OUTCOME_REJECT, REASON_HEAD_REQUEST_FAILED, f"HEAD request failed: {head.error}")
        return _result(candidate, verdict, head=head), None
    if (head.status == 200 or (head.status == 404 and allow_head_404)) and normalized_url(
        head.resolved_url or candidate.url
    ) != normalized_url(candidate.url):
        verdict = _verdict(OUTCOME_REJECT, REASON_UNRELATED_REDIRECT, "HEAD resolved to an unrelated target")
        return _result(candidate, verdict, head=head), None

    head_allows_get = head.status == 200 or (
        head.status is not None and head_fallback_allowed(head.status, allow_head_404=allow_head_404)
    )
    if not head_allows_get:
        head_verdict, _fallback = validate_http_status_evidence(
            HttpStatusEvidence.get_with_head(head_status=head.status, body_status=None, allow_head_404=allow_head_404)
        )
        if head_verdict is None:
            raise ValueError("HEAD status policy returned no verdict for a denied response")
        return _result(candidate, head_verdict, head=head), None

    get = _fetch_get(candidate.url, opener)
    if get.decode_failure:
        verdict = _verdict(
            OUTCOME_REJECT,
            REASON_GET_REQUEST_FAILED,
            f"GET response decoding failed: {get.decode_failure}",
        )
        return _result(candidate, verdict, head=head, get=get), None
    if get.error and get.status is None:
        verdict = _verdict(OUTCOME_REJECT, REASON_GET_REQUEST_FAILED, f"GET request failed: {get.error}")
        return _result(candidate, verdict, head=head, get=get), None
    if get.status != 200:
        return _evaluate_response(
            candidate,
            head,
            get,
            http_evidence=HttpStatusEvidence.get_with_head(
                head_status=head.status, body_status=get.status, allow_head_404=allow_head_404
            ),
        )
    unwrapped = unwrap_ranking_response(get.body, candidate.metadata)
    if not unwrapped.ok:
        # A declared envelope that does not hold the HTML is a refusal with
        # its own reason, never a silent fallback to the raw body.
        verdict = _verdict(OUTCOME_REJECT, REASON_RESPONSE_ENVELOPE, unwrapped.reason)
        return _result(candidate, verdict, head=head, get=get), None
    identity_html, identity_error = _verify_declared_identity(candidate, opener, unwrapped.envelope)
    if identity_error:
        verdict = _verdict(OUTCOME_REJECT, REASON_IDENTITY_ROUTE, identity_error)
        return _result(candidate, verdict, head=head, get=get), None
    return _evaluate_response(
        candidate,
        replace(head),
        replace(get, body=unwrapped.html),
        identity_html=identity_html,
        http_evidence=HttpStatusEvidence.get_with_head(
            head_status=head.status, body_status=get.status, allow_head_404=allow_head_404
        ),
    )


def _evaluate_response(
    candidate: SourceCandidate,
    head: _HTTPObservation,
    get: _HTTPObservation,
    http_evidence: HttpStatusEvidence,
    method: str = "GET",
    identity_html: str = "",
) -> tuple[dict[str, Any], _ProbeCapture | None]:
    """Judge one bounded body against the unchanged feasibility contract.

    *get* carries page HTML: any declared JSON envelope has already been
    unwrapped, and any declared pages already joined, by the caller. A
    declared POST route therefore reaches this seam with exactly the evidence
    a GET route reaches it with, so a form-served ranking is held to the same
    bounded rows, links, and reporting period as any other page.
    """
    if get.decode_failure:
        verdict = _verdict(
            OUTCOME_REJECT,
            REASON_POST_REQUEST_FAILED if method == "POST" else REASON_GET_REQUEST_FAILED,
            f"{method} response decoding failed: {get.decode_failure}",
        )
        return (
            _result(candidate, _attach_structural_diagnostics(verdict, get.body), head=head, get=get, method=method),
            None,
        )
    if get.truncated:
        verdict = _verdict(
            OUTCOME_DEFER,
            REASON_RESPONSE_TOO_LARGE,
            f"{method} response exceeded the {MAX_RESPONSE_BYTES}-byte capture limit",
        )
        return (
            _result(candidate, _attach_structural_diagnostics(verdict, get.body), head=head, get=get, method=method),
            None,
        )

    status_verdict, _fallback = validate_http_status_evidence(http_evidence)
    if status_verdict is not None:
        status_verdict = _attach_structural_diagnostics(status_verdict, get.body)
        status_verdict = _attach_ranking_declaration_diagnostics(status_verdict, candidate, get.body)
        return _result(candidate, status_verdict, head=head, get=get, method=method), None

    source_metadata: dict[str, Any] = {
        **dict(candidate.metadata),
        "url": candidate.url,
        "contract_family": candidate.contract_family,
        "visible_title_selector": candidate.visible_title_selector
        or str(candidate.metadata.get("visible_title_selector", "")),
        "entry_scope_selector": candidate.entry_scope_selector
        or str(candidate.metadata.get("entry_scope_selector", "")),
        "reference_date": (candidate.reference_date.isoformat() if candidate.reference_date is not None else ""),
    }
    verdict = evaluate_source_feasibility(
        source_metadata,
        CapturedSourceEvidence(
            source_url=candidate.url,
            resolved_url=get.resolved_url or candidate.url,
            head_status=head.status,
            get_status=get.status,
            html=get.body,
            visible_title_selector=candidate.visible_title_selector,
            entry_scope_selector=candidate.entry_scope_selector,
            source_page=candidate.url,
            reference_date=candidate.reference_date,
            identity_html=identity_html,
        ),
        http_evidence=http_evidence,
    )
    verdict = _attach_structural_diagnostics(verdict, get.body)
    verdict = _attach_ranking_declaration_diagnostics(verdict, candidate, get.body)
    return (
        _result(candidate, verdict, head=head, get=get, method=method),
        (
            _ProbeCapture(RankingPageEvidence(rows_html=get.body, identity_html=identity_html))
            if verdict.outcome == OUTCOME_ACCEPT
            else None
        ),
    )


def _probe_declared_request(
    candidate: SourceCandidate,
    opener: Callable[..., Any],
    ranking_request: RankingRequest,
) -> tuple[dict[str, Any], _ProbeCapture | None]:
    """Probe one candidate through its declared form POST route.

    Each bounded declared page is requested once and supplies its own status
    evidence and bounded body. The route makes no HEAD request, retry, or
    method fallback: a 405 or 501 from a form endpoint means the declared
    route is wrong.
    """
    post = _fetch_post(candidate.url, opener, ranking_request.for_page(1))
    if post.decode_failure:
        verdict = _verdict(
            OUTCOME_REJECT,
            REASON_POST_REQUEST_FAILED,
            f"POST response decoding failed: {post.decode_failure}",
        )
        return _result(candidate, verdict, head=_HTTPObservation(), get=post, method="POST"), None
    if post.error and post.status is None:
        verdict = _verdict(OUTCOME_REJECT, REASON_POST_REQUEST_FAILED, f"POST request failed: {post.error}")
        return _result(candidate, verdict, head=_HTTPObservation(), get=post, method="POST"), None
    if post.status != 200:
        status = post.status if post.status is not None else post.error
        verdict = _verdict(OUTCOME_REJECT, REASON_POST_REQUEST_FAILED, f"POST request answered {status}")
        return _result(candidate, verdict, head=_HTTPObservation(), get=post, method="POST"), None
    if normalized_url(post.resolved_url or candidate.url) != normalized_url(candidate.url):
        verdict = _verdict(OUTCOME_REJECT, REASON_UNRELATED_REDIRECT, "POST resolved to an unrelated target")
        return _result(candidate, verdict, head=_HTTPObservation(), get=post, method="POST"), None

    if post.truncated:
        return _evaluate_response(
            candidate,
            _HTTPObservation(),
            post,
            method="POST",
            http_evidence=HttpStatusEvidence.declared_post(body_status=post.status),
        )

    first = unwrap_ranking_response(post.body, candidate.metadata)
    if not first.ok:
        verdict = _verdict(OUTCOME_REJECT, REASON_RESPONSE_ENVELOPE, first.reason)
        return _result(candidate, verdict, head=_HTTPObservation(), get=post, method="POST"), None

    post, paging_reason_code, paging_error = _collect_declared_pages(candidate, opener, ranking_request, post, first)
    if paging_error:
        outcome = OUTCOME_DEFER if paging_reason_code == REASON_RESPONSE_TOO_LARGE else OUTCOME_REJECT
        verdict = _verdict(outcome, paging_reason_code, paging_error)
        return _result(candidate, verdict, head=_HTTPObservation(), get=post, method="POST"), None

    identity_html, identity_error = _verify_declared_identity(candidate, opener, first.envelope)
    if identity_error:
        verdict = _verdict(OUTCOME_REJECT, REASON_IDENTITY_ROUTE, identity_error)
        return _result(candidate, verdict, head=_HTTPObservation(), get=post, method="POST"), None

    return _evaluate_response(
        candidate,
        _HTTPObservation(),
        post,
        HttpStatusEvidence.declared_post(body_status=post.status),
        method="POST",
        identity_html=identity_html,
    )


def _paging_failure_message(failure_kind: str, page_number: int, max_pages: int, max_bytes: int) -> tuple[str, str]:
    """Map pure paging outcomes to the probe's established reason contract."""
    if failure_kind == PAGING_EMPTY:
        return REASON_RANKING_PAGING, f"ranking page {page_number} returned empty HTML"
    if failure_kind == PAGING_REPEATED:
        return REASON_RANKING_PAGING, f"ranking page {page_number} repeated an earlier page"
    if failure_kind == PAGING_MISSING_FINAL:
        return REASON_RANKING_PAGING, f"ranking page {page_number} did not provide a boolean final-page value"
    if failure_kind == PAGING_TOO_LARGE:
        return REASON_RESPONSE_TOO_LARGE, f"joined ranking HTML exceeded the {max_bytes}-byte capture limit"
    return REASON_RANKING_PAGING, f"ranking paging reached max_pages={max_pages} before the final page"


def _collect_declared_pages(
    candidate: SourceCandidate,
    opener: Callable[..., Any],
    ranking_request: RankingRequest,
    first_page: _HTTPObservation,
    first: UnwrappedResponse,
) -> tuple[_HTTPObservation, str, str]:
    """Walk declared pages through the shared pure paging state."""
    if not ranking_request.paging_declared:
        return replace(first_page, body=first.html), "", ""

    state = RankingPagingState.start(
        max_pages=ranking_request.max_pages,
        max_bytes=MAX_RESPONSE_BYTES,
        html=first.html,
        final_page=first.last_page_value(candidate.metadata),
    )
    if state.failure is not None:
        reason_code, reason = _paging_failure_message(
            state.failure.kind, state.failure.page_number, ranking_request.max_pages, MAX_RESPONSE_BYTES
        )
        return first_page, reason_code, reason
    if state.complete:
        return replace(first_page, body=state.joined_html, body_bytes=len(state.joined_html.encode("utf-8"))), "", ""

    while state.needs_next_page:
        page = state.page_count + 1
        observation = _fetch_post(candidate.url, opener, ranking_request.for_page(page))
        if observation.decode_failure:
            return (
                observation,
                REASON_RANKING_PAGING,
                f"ranking page {page} response decoding failed: {observation.decode_failure}",
            )
        if observation.error:
            return observation, REASON_RANKING_PAGING, f"ranking page {page} request failed: {observation.error}"
        if observation.status != 200:
            return observation, REASON_RANKING_PAGING, f"ranking page {page} returned HTTP {observation.status}"
        if observation.truncated:
            return (
                observation,
                REASON_RESPONSE_TOO_LARGE,
                f"ranking page {page} exceeded the {MAX_RESPONSE_BYTES}-byte capture limit",
            )
        if normalized_url(observation.resolved_url or candidate.url) != normalized_url(candidate.url):
            return observation, REASON_RANKING_PAGING, f"ranking page {page} resolved to an unrelated source target"
        unwrapped = unwrap_ranking_response(observation.body, candidate.metadata)
        if not unwrapped.ok:
            return observation, REASON_RESPONSE_ENVELOPE, f"ranking page {page}: {unwrapped.reason}"
        state = state.append(unwrapped.html, unwrapped.last_page_value(candidate.metadata))
        if state.failure is not None:
            reason_code, reason = _paging_failure_message(
                state.failure.kind, state.failure.page_number, ranking_request.max_pages, MAX_RESPONSE_BYTES
            )
            if state.failure.kind == PAGING_TOO_LARGE:
                observation = replace(
                    observation,
                    body=_bounded_utf8(state.joined_html),
                    body_bytes=len(_bounded_utf8(state.joined_html).encode("utf-8")),
                    truncated=True,
                )
            return observation, reason_code, reason
        if state.complete:
            return (
                replace(observation, body=state.joined_html, body_bytes=len(state.joined_html.encode("utf-8"))),
                "",
                "",
            )
    return (
        first_page,
        REASON_RANKING_PAGING,
        f"ranking paging reached max_pages={ranking_request.max_pages} before the final page",
    )


def _verify_declared_identity(
    candidate: SourceCandidate,
    opener: Callable[..., Any],
    envelope: Mapping[str, Any],
) -> tuple[str, str]:
    """Return the declared identity page capture and the reason it failed.

    Exactly one of the two is nonempty for a source that declares a route. A
    source that declares none returns two empty strings, which leaves every
    existing source judged on its own body.
    """
    route = ranking_identity_route(candidate.metadata)
    if route is None:
        return "", ""
    url, resolve_error = route.resolve(envelope, candidate.url)
    if resolve_error:
        return "", resolve_error
    if not valid_http_url(url):
        return "", f"resolved identity URL is not a valid HTTP or HTTPS URL: {url}"
    observation = _fetch_get(url, opener)
    if observation.decode_failure:
        return "", f"identity page {url} response decoding failed: {observation.decode_failure}"
    if observation.error:
        return "", f"identity page {url} request failed: {observation.error}"
    if observation.status != 200:
        status = observation.status if observation.status is not None else observation.error
        return "", f"identity page {url} answered {status}"
    if observation.truncated:
        return "", f"identity page {url} exceeded the {MAX_RESPONSE_BYTES}-byte capture limit"
    resolved_url = observation.resolved_url or url
    resolved_parts = urlsplit(resolved_url)
    requested_parts = urlsplit(url)
    if (
        not same_origin_url(url, resolved_url)
        or resolved_parts.path.rstrip("/") != requested_parts.path.rstrip("/")
        or resolved_parts.query != requested_parts.query
    ):
        return "", f"identity page {url} resolved to a different target"
    visible_title_selector = candidate.visible_title_selector or str(
        candidate.metadata.get("visible_title_selector", "")
    )
    identity = parse_visible_page_identity(observation.body, visible_title_selector)
    marker = str(candidate.metadata.get("ranking_category_marker", ""))
    if not identity_heading_matches(identity.identity_texts, marker, route.resolved_tokens(envelope)):
        return "", f"identity page {url} does not state the declared category and period {marker!r}"
    return observation.body, ""


def _ledger_markdown_path(path: Path) -> Path:
    return path.with_suffix(".md")


def _resolve_storage_destination(
    path: Path,
    kind: _StorageDestinationKind,
    label: str,
) -> _StorageDestination:
    """Resolve and validate one ledger file or capture directory destination.

    Thin wrapper over the shared public policy in
    ``autoscout.safe_artifacts``; the operator-facing error type stays the
    ledger error so every caller and CLI contract is unchanged."""
    try:
        return resolve_storage_destination(path, kind, label)
    except SafeArtifactError as exc:
        raise SourceCandidateLedgerError(_error_text(exc)) from exc


def _validate_ledger_targets(path: Path) -> tuple[_StorageDestination, _StorageDestination]:
    if path.suffix.casefold() != ".json":
        raise SourceCandidateLedgerError("ledger path must be an explicit JSON artifact")
    json_destination = _resolve_storage_destination(path, "file", "ledger")
    markdown_destination = _resolve_storage_destination(_ledger_markdown_path(path), "file", "ledger Markdown sidecar")
    return json_destination, markdown_destination


def _validate_ledger_string(entry: Mapping[str, Any], field: str) -> None:
    if field in entry and not isinstance(entry[field], str):
        raise SourceCandidateLedgerError(f"ledger entry field {field!r} must be a string")


def _validate_ledger_status(entry: Mapping[str, Any], field: str) -> None:
    if field not in entry:
        return
    value = entry[field]
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
        raise SourceCandidateLedgerError(f"ledger entry field {field!r} must be an integer or null")


def _validate_ledger_http_evidence(value: Any) -> None:
    if not isinstance(value, Mapping):
        raise SourceCandidateLedgerError("ledger entry http_evidence must be an object")
    if set(value) != {"head", "get"}:
        raise SourceCandidateLedgerError("ledger entry http_evidence must contain head and get sections")
    for section_name, section in value.items():
        if section_name not in {"head", "get"} or not isinstance(section, Mapping):
            raise SourceCandidateLedgerError("ledger entry http_evidence has an invalid section")
        allowed_fields = {"status", "resolved_url", "error"}
        if section_name == "get":
            allowed_fields.update({"body_bytes", "truncated", "method"})
        unknown_fields = set(section) - allowed_fields
        if unknown_fields:
            names = ", ".join(sorted(str(field) for field in unknown_fields))
            raise SourceCandidateLedgerError(f"ledger http_evidence has unknown fields: {names}")
        _validate_ledger_status(section, "status")
        for field in ("resolved_url", "error"):
            _validate_ledger_string(section, field)
        if "body_bytes" in section:
            body_bytes = section["body_bytes"]
            if isinstance(body_bytes, bool) or not isinstance(body_bytes, int) or body_bytes < 0:
                raise SourceCandidateLedgerError("ledger get body_bytes must be a non-negative integer")
        if "truncated" in section and not isinstance(section["truncated"], bool):
            raise SourceCandidateLedgerError("ledger get truncated must be a boolean")


def _validate_ledger_entry(item: Any, version: int, *, allow_history: bool = True) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise SourceCandidateLedgerError("ledger entries must be objects")
    entry = dict(item)
    if version == _LEDGER_VERSION_3:
        allowed_fields = _LEDGER_ENTRY_FIELDS_VERSION_3_CURRENT if allow_history else _LEDGER_ENTRY_FIELDS_VERSION_3
        required_fields = _LEDGER_REQUIRED_ENTRY_FIELDS_VERSION_3
    elif version == _LEDGER_VERSION_2:
        allowed_fields = _LEDGER_ENTRY_FIELDS_VERSION_2
        required_fields = _LEDGER_REQUIRED_ENTRY_FIELDS_VERSION_2
    else:
        allowed_fields = _LEDGER_ENTRY_FIELDS_VERSION_1
        required_fields = _LEDGER_REQUIRED_ENTRY_FIELDS_VERSION_1
    unknown_fields = set(entry) - allowed_fields
    if unknown_fields:
        names = ", ".join(sorted(str(field) for field in unknown_fields))
        raise SourceCandidateLedgerError(f"ledger entry has unknown fields: {names}")
    missing_fields = required_fields - set(entry)
    if missing_fields:
        names = ", ".join(sorted(missing_fields))
        raise SourceCandidateLedgerError(f"ledger entry is missing required fields: {names}")
    for field in (
        "normalized_url",
        "probe_date",
        "url",
        "name",
        "signal_family",
        "reason_code",
        "reason",
        "resolved_url",
        "detected_heading",
        "proposing_context",
        "contract_family",
    ):
        _validate_ledger_string(entry, field)
    if not entry["url"]:
        raise SourceCandidateLedgerError("ledger entry url must not be empty")
    probe_date = entry["probe_date"]
    if not probe_date.strip():
        raise SourceCandidateLedgerError("ledger entry probe_date must not be empty")
    try:
        date.fromisoformat(probe_date)
    except ValueError as exc:
        raise SourceCandidateLedgerError("ledger entry probe_date must be an ISO date") from exc
    outcome = entry["outcome"]
    if not isinstance(outcome, str) or outcome not in _LEDGER_OUTCOMES:
        raise SourceCandidateLedgerError(f"ledger entry outcome is not a supported version-{version} outcome")
    if version == _LEDGER_VERSION_2 and not entry["proposing_context"].strip():
        raise SourceCandidateLedgerError("ledger entry proposing_context must be nonblank")
    if "contract_family" in entry:
        contract = entry["contract_family"].strip().casefold()
        if contract not in CONTRACT_FAMILIES:
            raise SourceCandidateLedgerError(f"ledger entry contract_family is unsupported: {contract}")
    if "normalized_url" in entry and entry["normalized_url"] != normalized_url(entry["url"]):
        raise SourceCandidateLedgerError("ledger entry normalized_url does not match url")
    if "ranking_diagnostics" in entry:
        diagnostics = _sanitize_ranking_diagnostics(entry["ranking_diagnostics"])
        if diagnostics is None or diagnostics != entry["ranking_diagnostics"]:
            raise SourceCandidateLedgerError("ledger ranking_diagnostics is not safely bounded")
    if "ranking_declaration_diagnostics" in entry:
        diagnostics = _sanitize_ranking_declaration_diagnostics(entry["ranking_declaration_diagnostics"])
        if (
            diagnostics is None
            or diagnostics != entry["ranking_declaration_diagnostics"]
            or entry.get("outcome") == OUTCOME_ACCEPT
            or str(entry.get("contract_family", "")).strip().casefold() != "ranking-table"
        ):
            raise SourceCandidateLedgerError("ledger ranking_declaration_diagnostics is not safely bounded")
    if "structural_diagnostics" in entry:
        diagnostics = _sanitize_structural_diagnostics(entry["structural_diagnostics"])
        if diagnostics is None or diagnostics != entry["structural_diagnostics"]:
            raise SourceCandidateLedgerError("ledger structural_diagnostics is not safely bounded")
    if "contract_metadata" in entry:
        metadata = entry["contract_metadata"]
        if not isinstance(metadata, Mapping):
            raise SourceCandidateLedgerError("ledger entry contract_metadata must be an object")
        if any(not isinstance(key, str) for key in metadata):
            raise SourceCandidateLedgerError("ledger entry contract_metadata keys must be strings")
        for _key, value in metadata.items():
            if not (isinstance(value, bool) or isinstance(value, str | int | list)):
                raise SourceCandidateLedgerError(
                    "ledger entry contract_metadata values must be strings, integers, booleans, or lists"
                )
            if isinstance(value, list) and any(not isinstance(item, str) for item in value):
                raise SourceCandidateLedgerError("ledger entry contract_metadata lists must contain strings")
    if "http_evidence" in entry:
        _validate_ledger_http_evidence(entry["http_evidence"])
    _validate_ledger_status(entry, "head_status")
    _validate_ledger_status(entry, "get_status")
    if "bounded_entry_count" in entry:
        count = entry["bounded_entry_count"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise SourceCandidateLedgerError("ledger entry bounded_entry_count must be a non-negative integer")
    if "raw_row_count" in entry:
        count = entry["raw_row_count"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise SourceCandidateLedgerError("ledger entry raw_row_count must be a non-negative integer")
    if "samples" in entry:
        samples = entry["samples"]
        if not isinstance(samples, list):
            raise SourceCandidateLedgerError("ledger entry samples must be a list")
        for sample in samples:
            if not isinstance(sample, Mapping) or not {"title", "author", "url"}.issubset(sample):
                raise SourceCandidateLedgerError("ledger entry samples must contain title, author, and url")
            unknown_sample_fields = set(sample) - {
                "title",
                "author",
                "url",
                "rank",
                "identifier",
                "category",
                "reporting_period",
            }
            if unknown_sample_fields:
                raise SourceCandidateLedgerError("ledger entry samples contain unknown fields")
            if any(not isinstance(sample[field], str) for field in sample):
                raise SourceCandidateLedgerError("ledger entry sample fields must be strings")
    if "prior_verdicts" in entry:
        if version != _LEDGER_VERSION_3 or not allow_history:
            raise SourceCandidateLedgerError("ledger prior_verdicts are allowed only on current version-3 entries")
        history = entry["prior_verdicts"]
        if not isinstance(history, list):
            raise SourceCandidateLedgerError("ledger prior_verdicts must be a list")
        current_date = date.fromisoformat(entry["probe_date"])
        previous_date: date | None = None
        for snapshot in history:
            validated_snapshot = _validate_ledger_entry(snapshot, _LEDGER_VERSION_3, allow_history=False)
            if validated_snapshot["normalized_url"] != entry["normalized_url"]:
                raise SourceCandidateLedgerError("ledger prior verdict normalized_url does not match current entry")
            snapshot_date = date.fromisoformat(validated_snapshot["probe_date"])
            if snapshot_date > current_date:
                raise SourceCandidateLedgerError("ledger prior verdict probe_date is later than current verdict")
            if previous_date is not None and snapshot_date < previous_date:
                raise SourceCandidateLedgerError("ledger prior verdicts must be chronological")
            previous_date = snapshot_date
    return entry


def _counts(entries: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Derive the deterministic outcome counts for canonical entries.

    This is the single source of outcome counts for version-2 and version-3 ledger
    validation, JSON writes, Markdown rendering, and probe report counts.
    The accept/defer/reject sum always equals the total entry count."""
    materialized = list(entries)
    return {
        outcome: sum(1 for entry in materialized if entry.get("outcome") == outcome)
        for outcome in (OUTCOME_ACCEPT, OUTCOME_DEFER, OUTCOME_REJECT)
    }


def _validate_ledger_counts(data: Mapping[str, Any], entries: Mapping[str, Mapping[str, Any]]) -> None:
    """Validate stored counts against the canonical current entries."""
    actual = _counts(entries.values())
    for field, outcome in (
        ("accept_count", OUTCOME_ACCEPT),
        ("defer_count", OUTCOME_DEFER),
        ("reject_count", OUTCOME_REJECT),
    ):
        stored = data.get(field)
        if isinstance(stored, bool) or not isinstance(stored, int):
            raise SourceCandidateLedgerError(f"ledger {field} must be an integer")
        if stored != actual[outcome]:
            raise SourceCandidateLedgerError(f"ledger {field} does not match its entries")
    total = data.get("total_entry_count")
    if isinstance(total, bool) or not isinstance(total, int):
        raise SourceCandidateLedgerError("ledger total_entry_count must be an integer")
    if total != len(entries):
        raise SourceCandidateLedgerError("ledger total_entry_count does not match its entries")


def _load_ledger(destination: _StorageDestination) -> dict[str, dict[str, Any]]:
    json_path = destination.path
    if not json_path.exists():
        return {}
    try:
        data = read_json(json_path, default=None)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise SourceCandidateLedgerError(f"cannot read ledger: {_error_text(exc)}")
    if not isinstance(data, Mapping):
        raise SourceCandidateLedgerError("ledger must be a JSON object")
    version = data.get("version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version
        not in {
            _LEDGER_VERSION_1,
            _LEDGER_VERSION_2,
            _LEDGER_VERSION_3,
        }
    ):
        raise SourceCandidateLedgerError("ledger version must be 1, 2, or 3")
    if version == _LEDGER_VERSION_1:
        if set(data) != {"version", "entries"}:
            raise SourceCandidateLedgerError("ledger must be a version-1 object with entries")
    elif set(data) != {
        "version",
        "accept_count",
        "defer_count",
        "reject_count",
        "total_entry_count",
        "entries",
    }:
        expected_version = "version-3" if version == _LEDGER_VERSION_3 else "version-2"
        raise SourceCandidateLedgerError(f"ledger must be a {expected_version} object with counts and entries")
    entries = data["entries"]
    if not isinstance(entries, list):
        raise SourceCandidateLedgerError("ledger entries must be a list")
    loaded: dict[str, dict[str, Any]] = {}
    for item in entries:
        entry = _validate_ledger_entry(item, version)
        if version == _LEDGER_VERSION_1:
            # Version-1 entries never recorded a proposing context. The
            # explicit legacy value is added in memory only; the file is
            # upgraded only when a new verdict is written (Issue #336).
            entry.setdefault("proposing_context", LEGACY_PROPOSING_CONTEXT)
        identity = entry["normalized_url"]
        if identity in loaded:
            raise SourceCandidateLedgerError(f"ledger contains duplicate normalized_url: {identity}")
        loaded[identity] = entry
    if version in {_LEDGER_VERSION_2, _LEDGER_VERSION_3}:
        _validate_ledger_counts(data, loaded)
    return loaded


def read_source_candidate_ledger(path: Path) -> dict[str, dict[str, Any]]:
    """Read only the canonical JSON ledger without inspecting its sidecar."""
    if path.suffix.casefold() != ".json":
        raise SourceCandidateLedgerError("ledger path must be an explicit JSON artifact")
    destination = _resolve_storage_destination(path, "file", "ledger")
    return _load_ledger(destination)


def _ledger_record(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "normalized_url": normalized_url(str(result.get("url", ""))),
        "probe_date": str(result.get("probe_date", "")),
        "url": str(result.get("url", "")),
        "name": str(result.get("name", "")),
        "signal_family": str(result.get("signal_family", "")),
        "outcome": str(result.get("outcome", "")),
        "reason_code": str(result.get("reason_code", "")),
        "reason": str(result.get("reason", "")),
        "http_evidence": result.get("http_evidence", {}),
        "head_status": result.get("head_status"),
        "get_status": result.get("get_status"),
        "resolved_url": str(result.get("resolved_url", "")),
        "detected_heading": str(result.get("detected_heading", "")),
        "bounded_entry_count": int(result.get("bounded_entry_count", 0) or 0),
        "raw_row_count": int(result.get("raw_row_count", 0) or 0),
        "samples": result.get("samples", []),
        "proposing_context": str(result.get("proposing_context", DEFAULT_PROPOSING_CONTEXT)),
        **(
            {"ranking_diagnostics": _sanitize_ranking_diagnostics(result["ranking_diagnostics"])}
            if _sanitize_ranking_diagnostics(result.get("ranking_diagnostics")) is not None
            else {}
        ),
        **(
            {
                "ranking_declaration_diagnostics": _sanitize_ranking_declaration_diagnostics(
                    result["ranking_declaration_diagnostics"]
                )
            }
            if _sanitize_ranking_declaration_diagnostics(result.get("ranking_declaration_diagnostics")) is not None
            else {}
        ),
        **(
            {"structural_diagnostics": _sanitize_structural_diagnostics(result["structural_diagnostics"])}
            if _sanitize_structural_diagnostics(result.get("structural_diagnostics")) is not None
            else {}
        ),
        **(
            {"contract_family": str(result["contract_family"]).strip().casefold()}
            if str(result.get("contract_family", "")).strip()
            else {}
        ),
        **(
            {"contract_metadata": dict(result["contract_metadata"])}
            if isinstance(result.get("contract_metadata"), Mapping) and result.get("contract_metadata")
            else {}
        ),
    }


def _history_snapshot(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Copy one current verdict without carrying nested re-probe history."""
    snapshot = deepcopy(dict(entry))
    snapshot.pop("prior_verdicts", None)
    return snapshot


def _markdown_cell(value: Any) -> str:
    """Render one Markdown cell losslessly from a canonical value.

    No whitespace is stripped and no character is normalized. Backslashes,
    pipes, newlines, and carriage returns are escaped in that order, so the
    displayed cell decodes back to the exact canonical value: a literal
    backslash becomes two backslashes, a literal pipe becomes backslash-pipe,
    a newline becomes the two characters backslash-n, and a carriage return
    becomes the two characters backslash-r. No raw carriage return or raw
    newline from a canonical cell may enter the table row, so row splitting
    and newline-normalized sidecar rewrites cannot happen."""
    text = str(value if value is not None else "")
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "\\n").replace("\r", "\\r")


def _render_ledger_markdown(entries: Sequence[Mapping[str, Any]]) -> str:
    """Render the Markdown ledger only from validated canonical entries."""
    counts = _counts(entries)
    lines = [
        "# Source Candidate Ledger",
        "",
        "This ledger records deterministic probe results for unconfigured Source Candidates.",
        "",
        f"Total entry count: {len(entries)}",
        f"Accept count: {counts[OUTCOME_ACCEPT]}",
        f"Defer count: {counts[OUTCOME_DEFER]}",
        f"Reject count: {counts[OUTCOME_REJECT]}",
        "",
        "| URL | Probe date | Outcome | Reason code | Reason | Proposing context | Prior verdicts |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for entry in entries:
        history = entry.get("prior_verdicts", [])
        history_text = (
            "; ".join(
                f"{item.get('probe_date', '')}: {item.get('outcome', '')}/{item.get('reason_code', '')}"
                for item in history
                if isinstance(item, Mapping)
            )
            if isinstance(history, list)
            else ""
        )
        cells = [
            _markdown_cell(entry.get("url", "")),
            _markdown_cell(entry.get("probe_date", "")),
            _markdown_cell(entry.get("outcome", "")),
            _markdown_cell(entry.get("reason_code", "")),
            _markdown_cell(entry.get("reason", "")),
            _markdown_cell(entry.get("proposing_context", "")),
            _markdown_cell(history_text),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


# Module-level seam: tests inject replace failures through this name. The
# generic implementation lives in ``autoscout.safe_artifacts``; the pair and
# single-file publishers below resolve this name at call time so the seam
# keeps working.
_staged_replace = staged_replace


def _publish_ledger_pair(
    json_path: Path,
    markdown_path: Path,
    json_text: str,
    markdown_text: str,
) -> None:
    """Publish the JSON and Markdown pair with rollback on failure (Issue #336).

    Thin wrapper over the shared atomic pair publication; the ``_staged_replace``
    seam is passed through so tests can inject replace failures."""
    publish_pair(
        json_path,
        markdown_path,
        json_text,
        markdown_text,
        replace=_staged_replace,
    )


def _ordered_entries(entries: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Order canonical entries by normalized URL for durable rendering.

    Version-1 entries read from disk get the explicit legacy proposing
    context in memory only; the JSON file is upgraded only when a new
    verdict is written (Issue #336)."""
    ordered: list[dict[str, Any]] = []
    for key in sorted(entries):
        entry = dict(entries[key])
        entry.setdefault("proposing_context", LEGACY_PROPOSING_CONTEXT)
        ordered.append(entry)
    return ordered


def _write_ledger(
    destinations: tuple[_StorageDestination, _StorageDestination],
    entries: Mapping[str, Mapping[str, Any]],
) -> None:
    json_destination, markdown_destination = destinations
    json_path = json_destination.path
    markdown_path = markdown_destination.path
    ordered = _ordered_entries(entries)
    counts = _counts(ordered)
    accept_count = counts[OUTCOME_ACCEPT]
    defer_count = counts[OUTCOME_DEFER]
    reject_count = counts[OUTCOME_REJECT]
    total_entry_count = len(ordered)
    if accept_count + defer_count + reject_count != total_entry_count:
        raise SourceCandidateLedgerError("ledger outcome counts do not match its entries")
    json_text = (
        json.dumps(
            {
                "version": _LEDGER_VERSION_3,
                "accept_count": accept_count,
                "defer_count": defer_count,
                "reject_count": reject_count,
                "total_entry_count": total_entry_count,
                "entries": ordered,
            },
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n"
    )
    markdown_text = _render_ledger_markdown(ordered)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _publish_ledger_pair(json_path, markdown_path, json_text, markdown_text)
    except SourceCandidateLedgerError:
        raise
    except OSError as exc:
        raise SourceCandidateLedgerError(f"cannot publish ledger: {_error_text(exc)}") from exc


def _publish_markdown_sidecar(markdown_path: Path, markdown_text: str) -> None:
    """Publish one derived Markdown sidecar with an exclusive staged file.

    Thin wrapper over the shared single-file publication; the
    ``_staged_replace`` seam is passed through so tests can inject replace
    failures. A failed sidecar write leaves the previous sidecar intact and
    never leaves a partial sidecar at the final path."""
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    publish_single(markdown_path, markdown_text, replace=_staged_replace)


def _repair_ledger_markdown(
    destinations: tuple[_StorageDestination, _StorageDestination],
    ledger: Mapping[str, Mapping[str, Any]],
) -> None:
    """Regenerate the derived Markdown sidecar when it is missing or stale.

    The JSON ledger is canonical: every probe run validates the JSON
    entries first and then verifies the sidecar against the deterministic
    rendering. A missing, stale, malformed, or count-mismatched sidecar is
    regenerated from the validated JSON entries without network access.

    This repair never writes the JSON, so a duplicate-only run keeps the
    JSON bytes unchanged and never upgrades a version-1 ledger merely
    because it was read. The sidecar is published through an exclusive
    staged file and atomic replace, so a failed sidecar write leaves the
    previous sidecar intact."""
    json_destination, markdown_destination = destinations
    if not ledger or not json_destination.path.exists():
        return
    expected = _render_ledger_markdown(_ordered_entries(ledger))
    markdown_path = markdown_destination.path
    try:
        current = markdown_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        current = None
    if current == expected:
        return
    try:
        _publish_markdown_sidecar(markdown_path, expected)
    except OSError as exc:
        raise SourceCandidateLedgerError(f"cannot publish ledger Markdown sidecar: {_error_text(exc)}") from exc


def _contract_family_for_comparison(value: object) -> str:
    return str(value or "").strip().casefold() or "generic"


def _validate_prior_compatibility(candidate: SourceCandidate, prior: Mapping[str, Any]) -> None:
    prior_contract = _contract_family_for_comparison(prior.get("contract_family"))
    requested_contract = _contract_family_for_comparison(candidate.contract_family)
    if prior_contract != requested_contract:
        raise SourceCandidateLedgerError(
            "URL was already evaluated under contract family "
            f"{prior_contract!r}, but this probe requests {requested_contract!r}; "
            "do not reuse the verdict; preferred fresh evaluation: pass --re-probe, "
            "or use a new ledger path when separate history is intended."
        )
    prior_metadata = prior.get("contract_metadata")
    requested_metadata = dict(candidate.metadata)
    if requested_metadata and isinstance(prior_metadata, Mapping) and dict(prior_metadata) != requested_metadata:
        raise SourceCandidateLedgerError(
            "URL was already evaluated with different contract metadata; do not reuse the verdict; "
            "preferred fresh evaluation: pass --re-probe, or use a new ledger path "
            "when separate history is intended."
        )


def _already_evaluated_result(candidate: SourceCandidate, prior: Mapping[str, Any]) -> dict[str, Any]:
    _validate_prior_compatibility(candidate, prior)
    result = dict(prior)
    prior_outcome = str(prior["outcome"])
    result.update(
        {
            "url": candidate.url,
            "name": candidate.name,
            "signal_family": candidate.signal_family,
            "prior_outcome": prior_outcome,
            "prior_reason_code": prior.get("reason_code", ""),
            "prior_probe_date": prior.get("probe_date", ""),
            "reason_code": REASON_ALREADY_EVALUATED,
            "reason": "URL was already evaluated in the Source Candidate Ledger",
            "outcome": prior_outcome,
        }
    )
    result.setdefault("http_evidence", {})
    result.setdefault("detected_heading", "")
    result.setdefault("bounded_entry_count", 0)
    result.setdefault("samples", [])
    result.setdefault("probe_date", "")
    result.setdefault("head_status", None)
    result.setdefault("get_status", None)
    result.setdefault("resolved_url", candidate.url)
    result.setdefault("proposing_context", LEGACY_PROPOSING_CONTEXT)
    if candidate.visible_title_selector:
        result["visible_title_selector"] = candidate.visible_title_selector
    if candidate.entry_scope_selector:
        result["entry_scope_selector"] = candidate.entry_scope_selector
    return result


def _capture_path(directory: Path, url: str) -> Path:
    digest = hashlib.sha256(normalized_url(url).encode("utf-8")).hexdigest()[:20]
    return directory / f"source-candidate-{digest}.json"


def _write_capture(
    destination: _StorageDestination,
    candidate: SourceCandidate,
    result: Mapping[str, Any],
    body: _ProbeCapture,
) -> Path:
    directory = destination.path
    path = _capture_path(directory, candidate.url)
    reported_path = _capture_path(destination.requested, candidate.url)
    retained = body.evidence.sanitized(
        rows_max_bytes=MAX_RESPONSE_BYTES,
        identity_max_bytes=MAX_RESPONSE_BYTES,
    )
    directory.mkdir(parents=True, exist_ok=True)
    capture: dict[str, Any] = {
        "collection_mode": "source-candidate-probe",
        "source_url": candidate.url,
        "source_name": candidate.name,
        "source_page": candidate.url,
        "name": candidate.name,
        "reference_date": (candidate.reference_date.isoformat() if candidate.reference_date is not None else ""),
        "signal_family": candidate.signal_family,
        "resolved_url": result.get("resolved_url", candidate.url),
        "head_status": result.get("head_status"),
        "get_status": result.get("get_status"),
        "html": retained.rows_html,
        "detected_heading": result.get("detected_heading", ""),
        "bounded_entry_count": result.get("bounded_entry_count", 0),
        "raw_row_count": result.get("raw_row_count", 0),
        "samples": result.get("samples", []),
    }
    if body.structured is not None:
        capture.update(body.structured)
    if result.get("contract_family"):
        capture["contract_family"] = result["contract_family"]
    if isinstance(result.get("contract_metadata"), Mapping) and result.get("contract_metadata"):
        capture["contract_metadata"] = dict(result["contract_metadata"])
    for field_name in ("ranking_category", "reporting_period"):
        if result.get(field_name):
            capture[field_name] = result[field_name]
    if candidate.visible_title_selector:
        capture["visible_title_selector"] = candidate.visible_title_selector
    if candidate.entry_scope_selector:
        capture["entry_scope_selector"] = candidate.entry_scope_selector
    if retained.identity_html:
        capture["identity_html"] = retained.identity_html
    write_json(path, capture)
    return reported_path


def _candidate_values(values: list[str], index: int) -> str:
    return values[index].strip() if index < len(values) else ""


def _candidate_context(values: list[str], index: int) -> str:
    """Pick the proposing context for one candidate, preserving it exactly.

    Every supplied value was already validated nonblank, so an index inside
    the list keeps its exact value. An index beyond the list means the
    caller supplied no context for that URL, so the deterministic nonblank
    default applies."""
    if index < len(values):
        return values[index]
    return DEFAULT_PROPOSING_CONTEXT


def _draft_path(directory: Path, url: str) -> Path:
    """Deterministic feasibility draft filename from the normalized URL identity."""
    digest = hashlib.sha256(normalized_url(url).encode("utf-8")).hexdigest()[:20]
    return directory / f"source-feasibility-{digest}.md"


def _render_feasibility_draft(entry: Mapping[str, Any]) -> str:
    """Render one deterministic feasibility draft from bounded metadata only.

    The draft never contains raw HTML, reviews, descriptions, article bodies,
    promotional prose, or copied source text beyond the bounded metadata
    fields that the ledger already records. The editorial rationale section
    is intentionally incomplete and never invented."""
    lines = [
        "# Source Candidate Feasibility Draft",
        "",
        "Draft status: draft - incomplete; onboarding decision needs editor or agent judgment",
        "",
        "## Source Candidate",
        "",
        f"- Name: {_markdown_cell(entry.get('name', ''))}",
        f"- Candidate URL: {_markdown_cell(entry.get('url', ''))}",
        f"- Resolved URL: {_markdown_cell(entry.get('resolved_url', ''))}",
        f"- Probe date: {_markdown_cell(entry.get('probe_date', ''))}",
        f"- Signal family: {_markdown_cell(entry.get('signal_family', ''))}",
        f"- Contract family: {_markdown_cell(entry.get('contract_family', 'generic'))}",
        *(
            [
                "- Ranking rank source: "
                + _markdown_cell(
                    (entry.get("contract_metadata") or {}).get("ranking_rank_source", "selector")
                    if isinstance(entry.get("contract_metadata"), Mapping)
                    else "selector"
                )
            ]
            if str(entry.get("contract_family", "")).strip().casefold() == "ranking-table"
            else []
        ),
        f"- Proposing context: {_markdown_cell(entry.get('proposing_context', ''))}",
        "",
        "## Probe verdict",
        "",
        f"- Verdict: {_markdown_cell(entry.get('outcome', ''))}",
        f"- Reason code: {_markdown_cell(entry.get('reason_code', ''))}",
        f"- Plain-language reason: {_markdown_cell(entry.get('reason', ''))}",
        "",
        "## Detected page evidence",
        "",
        f"- Detected heading: {_markdown_cell(entry.get('detected_heading', ''))}",
        f"- Bounded entry count: {_markdown_cell(entry.get('bounded_entry_count', ''))}",
        f"- Raw row count: {_markdown_cell(entry.get('raw_row_count', ''))}",
        "",
        "## Captured evidence samples",
        "",
        "| Title | Primary author | Evidence URL | Rank | Identifier | Category | Reporting period |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for sample in entry.get("samples", []):
        lines.append(
            "| "
            + " | ".join(
                _markdown_cell(sample.get(field, ""))
                for field in ("title", "author", "url", "rank", "identifier", "category", "reporting_period")
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Editorial rationale",
            "",
            "This section is intentionally incomplete. It requires editor or agent judgment.",
            "No editorial rationale is invented in this draft, and the draft is not an onboarding decision.",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def _validate_draft_targets(destination: _StorageDestination, urls: Sequence[str]) -> None:
    """Validate existing feasibility draft files before any network access."""
    for url in urls:
        path = _draft_path(destination.path, url)
        if path.is_symlink():
            raise SourceCandidateLedgerError("feasibility draft must not be a symlink")
        if path.exists() and not path.is_file():
            raise SourceCandidateLedgerError("feasibility draft must be a regular file")
        if path.exists() and path.stat().st_nlink != 1:
            raise SourceCandidateLedgerError("feasibility draft must not be a hard link")


def _ensure_draft(
    destination: _StorageDestination,
    url: str,
    entry: Mapping[str, Any],
) -> str:
    """Create one feasibility draft, never overwriting an existing file.

    The draft is staged with an exclusive temporary name and then linked
    into place, so a failed write never leaves a partial draft at the
    final draft path."""
    path = _draft_path(destination.path, url)
    if path.exists():
        return "already_present"
    content = _render_feasibility_draft(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = exclusive_temp(path.parent, path.name, ".tmp")
    try:
        staged.write_text(content, encoding="utf-8")
        try:
            os.link(staged, path)
        except FileExistsError:
            return "already_present"
    finally:
        staged.unlink(missing_ok=True)
    return "created"


def _exit_code(results: Sequence[Mapping[str, Any]]) -> int:
    if any(result.get("outcome") == OUTCOME_ACCEPT for result in results):
        return 0
    if any(result.get("outcome") == OUTCOME_REJECT for result in results):
        return 1
    return 3


def run_source_candidate_probe(
    urls: list[str],
    names: list[str] | None = None,
    signal_families: list[str] | None = None,
    *,
    proposing_contexts: list[str] | None = None,
    visible_title_selectors: list[str] | None = None,
    entry_scope_selectors: list[str] | None = None,
    contract_families: list[str] | None = None,
    contract_metadata: Sequence[Mapping[str, Any]] | None = None,
    keep_captures: Path | None = None,
    ledger_path: Path | None = None,
    feasibility_dir: Path | None = None,
    opener: Callable[..., Any] | None = None,
    re_probe: bool = False,
) -> tuple[dict[str, Any], int]:
    """Probe every URL and return the structured report and exact exit code.

    *feasibility_dir* defaults to ``data/source-feasibility-drafts`` when
    omitted, so every newly accepted Source Candidate gets one
    deterministic feasibility draft unless an explicit directory is given."""
    if not isinstance(re_probe, bool):
        raise ValueError("re_probe must be a boolean")
    if not urls:
        raise ValueError("at least one --url is required")
    names = names or []
    signal_families = signal_families or []
    proposing_contexts = proposing_contexts or []
    visible_title_selectors = visible_title_selectors or []
    entry_scope_selectors = entry_scope_selectors or []
    contract_families = contract_families or []
    contract_metadata = contract_metadata or []
    if (
        len(names) > len(urls)
        or len(signal_families) > len(urls)
        or len(proposing_contexts) > len(urls)
        or len(visible_title_selectors) > len(urls)
        or len(entry_scope_selectors) > len(urls)
        or len(contract_families) > len(urls)
        or len(contract_metadata) > len(urls)
    ):
        raise ValueError(
            "--name, --signal-family, --proposing-context, --visible-title-selector, "
            "--entry-scope-selector, --contract-family, and ranking declarations "
            "cannot be supplied more times than --url"
        )
    # A supplied but blank proposing context is an operator error: only a
    # missing value receives the deterministic nonblank default. Reject
    # before any network access, ledger repair, or write.
    for index, context in enumerate(proposing_contexts):
        if not context.strip():
            raise ValueError(f"proposing context {index + 1} must be nonblank when supplied")
    for index, selector in enumerate(visible_title_selectors):
        if not isinstance(selector, str) or not selector.strip():
            raise ValueError(f"visible title selector {index + 1} must be nonblank when supplied")
        validate_visible_title_selector(selector)
    for index, selector in enumerate(entry_scope_selectors):
        if not isinstance(selector, str) or not selector.strip():
            raise ValueError(f"entry scope selector {index + 1} must be nonblank when supplied")
        validate_entry_scope_selector(selector)
    for index, family in enumerate(contract_families):
        normalized_family = str(family).strip().casefold()
        if normalized_family not in CONTRACT_FAMILIES:
            raise ValueError(f"contract family {index + 1} must be one of: {', '.join(CONTRACT_FAMILIES)}")
    for index, metadata in enumerate(contract_metadata):
        if not isinstance(metadata, Mapping):
            raise ValueError(f"contract metadata {index + 1} must be an object")
        for key, value in metadata.items():
            if not isinstance(key, str) or not (isinstance(value, bool) or isinstance(value, str | int | list)):
                raise ValueError(f"contract metadata {index + 1} values must be strings, integers, booleans, or lists")
            if isinstance(value, list) and any(not isinstance(item, str) for item in value):
                raise ValueError(f"contract metadata {index + 1} lists must contain strings")

    probe_date = date.today()
    candidates = [
        SourceCandidate(
            url=str(raw_url),
            name=_candidate_values(names, index),
            signal_family=_candidate_values(signal_families, index),
            proposing_context=_candidate_context(proposing_contexts, index),
            visible_title_selector=_candidate_values(visible_title_selectors, index),
            entry_scope_selector=_candidate_values(entry_scope_selectors, index),
            contract_family=_candidate_values(contract_families, index).casefold(),
            reference_date=probe_date,
            metadata=dict(contract_metadata[index]) if index < len(contract_metadata) else {},
        )
        for index, raw_url in enumerate(urls)
    ]
    for index, candidate in enumerate(candidates):
        if candidate.contract_family == "ranking-table":
            metadata = {
                **dict(candidate.metadata),
                "contract_family": candidate.contract_family,
                "metadata_ranking_table": True,
                "url": candidate.url,
                "visible_title_selector": candidate.visible_title_selector,
                "entry_scope_selector": candidate.entry_scope_selector,
            }
            ranking_error = validate_ranking_contract_metadata(metadata)
            if ranking_error:
                raise ValueError(f"contract metadata {index + 1}: {ranking_error}")
        if candidate.contract_family == "award-list":
            from autoscout.award_list import page_declaration_error

            award_list_error = page_declaration_error(candidate.metadata)
            if award_list_error:
                raise ValueError(f"contract metadata {index + 1}: {award_list_error}")

    ledger_file = ledger_path or DEFAULT_LEDGER_PATH
    ledger_destinations = _validate_ledger_targets(ledger_file)
    capture_destination = (
        _resolve_storage_destination(keep_captures, "directory", "capture directory")
        if keep_captures is not None
        else None
    )
    draft_dir = feasibility_dir if feasibility_dir is not None else DEFAULT_FEASIBILITY_DIR
    draft_destination = _resolve_storage_destination(draft_dir, "directory", "feasibility directory")
    _validate_draft_targets(draft_destination, [str(url) for url in urls])
    ledger = _load_ledger(ledger_destinations[0])
    _repair_ledger_markdown(ledger_destinations, ledger)
    probe_date_text = probe_date.isoformat()
    network = opener or urlopen
    results: list[dict[str, Any]] = []
    new_records: dict[str, dict[str, Any]] = {}
    bodies: list[tuple[SourceCandidate, dict[str, Any], _ProbeCapture]] = []

    for candidate in candidates:
        identity = normalized_url(candidate.url)
        durable_prior = ledger.get(identity)
        batch_prior = new_records.get(identity)
        if batch_prior is not None:
            result = _already_evaluated_result(candidate, batch_prior)
        elif durable_prior is not None and re_probe:
            if date.fromisoformat(str(durable_prior["probe_date"])) > probe_date:
                raise SourceCandidateLedgerError("re-probe date is earlier than the current ledger verdict")
            try:
                result, body = _probe_one(candidate, network)
            except Exception as exc:
                result = _result(
                    candidate,
                    _verdict(OUTCOME_REJECT, REASON_REQUEST_FAILED, f"candidate probe failed: {_error_text(exc)}"),
                    head=_HTTPObservation(),
                )
                body = None
            result.update(
                {
                    "prior_outcome": durable_prior.get("outcome"),
                    "prior_reason_code": durable_prior.get("reason_code", ""),
                    "prior_probe_date": durable_prior.get("probe_date", ""),
                }
            )
            result["probe_date"] = probe_date_text
            result["proposing_context"] = candidate.proposing_context
            if body is not None:
                bodies.append((candidate, result, body))
            current_record = _ledger_record(result)
            current_record["prior_verdicts"] = [
                *[deepcopy(snapshot) for snapshot in durable_prior.get("prior_verdicts", [])],
                _history_snapshot(durable_prior),
            ]
            new_records[identity] = current_record
        elif durable_prior is not None:
            result = _already_evaluated_result(candidate, durable_prior)
        else:
            try:
                result, body = _probe_one(candidate, network)
            except Exception as exc:
                result = _result(
                    candidate,
                    _verdict(OUTCOME_REJECT, REASON_REQUEST_FAILED, f"candidate probe failed: {_error_text(exc)}"),
                    head=_HTTPObservation(),
                )
                body = None
            result["probe_date"] = probe_date_text
            result["proposing_context"] = candidate.proposing_context
            if body is not None:
                bodies.append((candidate, result, body))
            new_records[identity] = _ledger_record(result)
        results.append(result)

    if capture_destination is not None:
        for candidate, result, body in bodies:
            if result.get("outcome") == OUTCOME_ACCEPT:
                capture = _write_capture(capture_destination, candidate, result, body)
                result["capture_file"] = str(capture)

    if new_records:
        for record in new_records.values():
            _validate_ledger_entry(record, _LEDGER_VERSION_3)
        combined = dict(ledger)
        combined.update(new_records)
        _write_ledger(ledger_destinations, combined)

    for result in results:
        if result.get("outcome") != OUTCOME_ACCEPT:
            continue
        url = str(result.get("url", ""))
        identity = normalized_url(url)
        prior = ledger.get(identity)
        entry = dict(new_records.get(identity) or prior or {})
        entry.setdefault("proposing_context", LEGACY_PROPOSING_CONTEXT)
        status = _ensure_draft(draft_destination, url, entry)
        if status == "created" and prior is not None and identity not in new_records:
            status = "regenerated"
        result["draft_status"] = status
        result["draft_file"] = str(_draft_path(draft_destination.requested, url))

    code = _exit_code(results)
    report = {
        "version": 1,
        "ledger": str(ledger_destinations[0].requested),
        "ledger_markdown": str(ledger_destinations[1].requested),
        "candidates": results,
        "counts": _counts(results),
        "exit_code": code,
    }
    return report, code


def probe_source_candidates(
    urls: list[str],
    names: list[str] | None = None,
    signal_families: list[str] | None = None,
    *,
    proposing_contexts: list[str] | None = None,
    visible_title_selectors: list[str] | None = None,
    entry_scope_selectors: list[str] | None = None,
    contract_families: list[str] | None = None,
    contract_metadata: Sequence[Mapping[str, Any]] | None = None,
    keep_captures: Path | None = None,
    ledger_path: Path | None = None,
    feasibility_dir: Path | None = None,
    opener: Callable[..., Any] | None = None,
    re_probe: bool = False,
) -> int:
    """Print one JSON report for a batch and return its contract exit code."""
    try:
        report, code = run_source_candidate_probe(
            urls,
            names,
            signal_families,
            proposing_contexts=proposing_contexts,
            visible_title_selectors=visible_title_selectors,
            entry_scope_selectors=entry_scope_selectors,
            contract_families=contract_families,
            contract_metadata=contract_metadata,
            keep_captures=keep_captures,
            ledger_path=ledger_path,
            feasibility_dir=feasibility_dir,
            opener=opener,
            re_probe=re_probe,
        )
    except (OSError, SourceCandidateLedgerError, ValueError) as exc:
        print(json.dumps({"version": 1, "error": _error_text(exc)}, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return code
