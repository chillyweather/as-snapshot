"""Deterministic canonical extraction-content document (issue #467 phase 1).

This module turns one sanitized capture JSON object into versioned canonical
extraction-content bytes.  The bytes are the single artifact that a later
model/cache path must consume and hash: the model reads exactly these bytes
and the cache hashes exactly these bytes, so both see the same extraction
input.

Design rules (issue #467, phase 1):

- Deterministic local code only: stdlib HTML parsing and sorted-key JSON.
- Every extraction-relevant HTML/content surface in the capture is
  canonicalized recursively: any string under an ``html`` or ``*_html`` key,
  at any nesting depth, is replaced by its canonical content projection.
  The known top-level ``html`` and ``identity_html`` surfaces and the nested
  ``detail_pages`` list are therefore all covered by one uniform rule, and
  executable raw HTML (scripts, styles, comments) can never survive into the
  canonical bytes through an unknown nested field.
- Visible text is preserved in document order.
- Insignificant whitespace is collapsed: every whitespace run inside text and
  retained attribute values becomes one ASCII space and the result is
  stripped.  See ``collapse_whitespace``.
- Comments, ``<style>`` content, and executable ``<script>`` content are
  excluded.  Non-executable ``<script type="application/ld+json">`` structured
  data is retained deterministically.
- Link targets (``a[href]``, ``area[href]``, ``img[src]``) are retained in a
  decoded form with the exact character case preserved.  URL and path case is
  never lowered, and query strings are never removed.
- ``alt`` and ``aria-label`` values are retained on any element because they
  can carry book information.
- Model-visible capture fields that carry source identity or list context are
  never silently discarded.  Unknown future capture fields are included
  automatically rather than dropped by a fixed allowlist.  Exactly two fields
  are excluded, and both exclusions are documented and deliberate:
  ``collection_mode`` is transport/provenance metadata about how the capture
  was fetched, not extraction content, and ``reference_date`` is a wall-clock
  capture stamp - the date the list page was captured - not page content.
  Including ``reference_date`` would make two otherwise identical captures
  taken on different dates produce different canonical bytes, so it is
  excluded from the bytes a later model/cache path consumes and hashes.
- The serialization is versioned: the schema version is a key inside the
  canonical bytes themselves.

This module deliberately does not perform source-family distillation or token
reduction; that stays a separate optimization (issue #467 non-goals).

URL normalization is deliberate and documented:

- ``canonical_source_identity`` reuses the existing
  ``normalize_source_identity`` for the canonical source identity of a
  capture.  That helper lowercases scheme and host, strips a leading
  ``www.``, removes trailing slashes from the path, and retains the character
  case of the path itself.
- No third URL normalizer is introduced here.  Analysis code that must
  compare two URLs for equality reuses the existing ``canonicalize_url``
  helper for absolute HTTP(S) URLs (scheme and host casefolded, path case and
  query verbatim, fragment dropped) and keeps relative values in raw form
  until they are resolved against a source base with the existing provenance
  resolver (``resolve_provenance_url``).  Raw link values inside the canonical
  content are never routed through any normalizer; they keep their original
  bytes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from autoscout.source_proposals import normalize_source_identity

#: Version 4 removes the ``reference_date`` wall-clock capture stamp from the
#: canonical bytes, so captures differing only by that stamp now produce
#: identical bytes and digests.
CANONICAL_EXTRACTION_CONTENT_SCHEMA_VERSION = 4

#: Capture fields that become the structured source-identity block.
_SOURCE_FIELDS: frozenset[str] = frozenset({"source_url", "source_name", "source_page", "resolved_url"})

#: Capture metadata deliberately excluded from the canonical bytes, documented
#: here.  ``collection_mode`` is transport/provenance metadata about how the
#: capture was fetched.  ``reference_date`` is a wall-clock capture stamp -
#: when the list page was captured - not extraction-relevant page content, so
#: it must never make two otherwise identical captures hash differently.
#: Everything else in the capture envelope survives.
_VOLATILE_CAPTURE_FIELDS: frozenset[str] = frozenset({"collection_mode", "reference_date"})

#: Tags whose link/target attribute values are retained as decoded link targets.
_LINK_TARGET_ATTRIBUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("a", "href"),
        ("area", "href"),
        ("img", "src"),
    }
)

#: Attributes retained on any element because they can carry book information.
_RETAINED_BOOK_ATTRIBUTES: frozenset[str] = frozenset({"alt", "aria-label"})


def collapse_whitespace(value: str) -> str:
    """Collapse every whitespace run to one ASCII space and strip the ends.

    This is the documented deterministic whitespace rule for canonical
    extraction content.  It applies to visible text runs and to retained
    attribute values alike, so two raw documents that differ only in
    insignificant whitespace produce identical canonical bytes.
    """
    return " ".join(value.split())


def canonical_json_bytes(document: dict[str, Any]) -> bytes:
    """Serialize a document to deterministic canonical bytes.

    Keys are sorted, separators are compact, non-ASCII text stays literal
    UTF-8, and the result is UTF-8 encoded without a trailing newline.
    """
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class _CanonicalContentHTMLParser(HTMLParser):
    """Collect canonical content features from sanitized capture HTML.

    Skip handling is stack-based and tag-matched: a ``<script>`` or
    ``<style>`` start tag pushes onto the skip stack and only its own matching
    end tag pops it.  Malformed input such as a stray or repeated end tag can
    never pop another tag's skip frame, so content after malformed markup
    stays deterministic.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.visible_text_runs: list[str] = []
        self.link_targets: list[dict[str, str]] = []
        self.retained_attributes: list[dict[str, str]] = []
        self.json_ld_documents: list[Any] = []
        self._skip_stack: list[str] = []
        self._json_ld_chunks: list[str] | None = None

    # -- skip bookkeeping -------------------------------------------------

    @staticmethod
    def _is_json_ld_script(attrs: list[tuple[str, str | None]]) -> bool:
        for name, value in attrs:
            if name == "type" and value and "application/ld+json" in value.strip().casefold():
                return True
        return False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.casefold()
        if lowered == "script":
            if self._is_json_ld_script(attrs):
                # JSON-LD scripts cannot nest (their content is CDATA data),
                # so a single collection slot is sufficient and robust.
                if not self._skip_stack and self._json_ld_chunks is None:
                    self._json_ld_chunks = []
                return
            self._skip_stack.append("script")
            return
        if lowered == "style":
            self._skip_stack.append("style")
            return
        if self._skip_stack:
            return
        attr_map = {name: (value or "") for name, value in attrs}
        for attribute_name in ("href", "src"):
            if (lowered, attribute_name) in _LINK_TARGET_ATTRIBUTES and attribute_name in attr_map:
                value = collapse_whitespace(attr_map[attribute_name])
                if value:
                    self.link_targets.append({"tag": lowered, "attribute": attribute_name, "value": value})
        for attribute_name in sorted(_RETAINED_BOOK_ATTRIBUTES):
            if attribute_name in attr_map:
                value = collapse_whitespace(attr_map[attribute_name])
                if value:
                    self.retained_attributes.append({"tag": lowered, "attribute": attribute_name, "value": value})

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered == "script" and self._json_ld_chunks is not None:
            self._store_json_ld("".join(self._json_ld_chunks))
            self._json_ld_chunks = None
            return
        if lowered in ("script", "style") and self._skip_stack and self._skip_stack[-1] == lowered:
            self._skip_stack.pop()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in ("script", "style"):
            # A self-closing script/style frame carries no content.
            return
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self._json_ld_chunks is not None:
            self._json_ld_chunks.append(data)
            return
        if self._skip_stack:
            return
        collapsed = collapse_whitespace(data)
        if collapsed:
            self.visible_text_runs.append(collapsed)

    def handle_comment(self, data: str) -> None:
        return

    def handle_decl(self, decl: str) -> None:
        return

    def handle_pi(self, data: str) -> None:
        return

    def unknown_decl(self, data: str) -> None:
        return

    # -- structured data --------------------------------------------------

    def _store_json_ld(self, raw_text: str) -> None:
        collapsed = collapse_whitespace(raw_text)
        if not collapsed:
            return
        try:
            self.json_ld_documents.append(json.loads(collapsed))
        except json.JSONDecodeError:
            self.json_ld_documents.append({"unparseable": True, "text": collapsed})


def canonical_source_identity(source_url: str) -> str:
    """Return the canonical source identity for a capture source URL.

    This deliberately reuses ``normalize_source_identity`` (no third URL
    normalizer).  The known and accepted behavior is: scheme and host are
    lowercased, a leading ``www.`` is removed, trailing slashes are removed
    from the path, and the character case of the path itself is retained.
    Raw link values inside canonical content are never routed through this
    function, so link case and trailing slashes survive in the content.
    """
    return normalize_source_identity(source_url)


def _optional_collapsed(capture: dict[str, Any], key: str) -> str:
    value = capture.get(key)
    if not isinstance(value, str):
        return ""
    return collapse_whitespace(value)


def _surface_projection(html: str) -> dict[str, Any]:
    """Project one HTML surface onto its canonical content features."""
    parser = _CanonicalContentHTMLParser()
    parser.feed(html)
    parser.close()
    return {
        "visible_text_runs": parser.visible_text_runs,
        "link_targets": parser.link_targets,
        "retained_attributes": parser.retained_attributes,
        "json_ld": parser.json_ld_documents,
    }


def _is_html_surface_key(key: str) -> bool:
    """True when a capture field name names an HTML content surface."""
    lowered = key.casefold()
    return lowered == "html" or lowered.endswith("_html")


def _canonicalize_field(key: str, value: Any) -> Any:
    """Include a capture field generically so nothing model-visible is dropped.

    Strings under HTML-surface keys (``html`` or any ``*_html`` key, at any
    nesting depth) are replaced by their canonical content projection, so
    executable raw HTML can never survive through an unknown nested field.
    All other strings are whitespace-collapsed; containers keep their order;
    unknown future fields flow through the same rules automatically, so no
    fixed allowlist can omit them.
    """
    if isinstance(value, str):
        if _is_html_surface_key(key):
            return _surface_projection(value)
        return collapse_whitespace(value)
    if isinstance(value, dict):
        return {str(name): _canonicalize_field(str(name), item) for name, item in value.items()}
    if isinstance(value, list):
        return [_canonicalize_field(key, item) for item in value]
    if value is None or isinstance(value, bool | int | float):
        return value
    return collapse_whitespace(str(value))


def build_canonical_document(capture: dict[str, Any]) -> dict[str, Any]:
    """Build the versioned canonical document for one sanitized capture."""
    html = capture.get("html")
    if not isinstance(html, str):
        raise ValueError("sanitized capture must carry a string 'html' field")
    source_url = _optional_collapsed(capture, "source_url")
    content = {
        key: _canonicalize_field(key, value)
        for key, value in sorted(capture.items())
        if key not in _SOURCE_FIELDS and key not in _VOLATILE_CAPTURE_FIELDS
    }
    return {
        "kind": "canonical-extraction-content",
        "schema_version": CANONICAL_EXTRACTION_CONTENT_SCHEMA_VERSION,
        "source": {
            "source_name": _optional_collapsed(capture, "source_name"),
            "source_identity": canonical_source_identity(source_url) if source_url else "",
            "source_url": source_url,
            "source_page": _optional_collapsed(capture, "source_page"),
            "resolved_url": _optional_collapsed(capture, "resolved_url"),
        },
        "content": content,
    }


@dataclass(frozen=True)
class CanonicalExtractionContent:
    """The exact canonical extraction-content artifact for one capture.

    ``canonical_bytes`` is the artifact a later model/cache path must consume
    and hash; ``sha256`` is the digest over exactly those bytes.
    """

    schema_version: int
    document: dict[str, Any]
    canonical_bytes: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()

    @property
    def visible_text(self) -> str:
        """Join the main ``html`` surface's visible text runs into one blob.

        Runs are joined with a single space; matching helpers must apply the
        same whitespace normalization to the needle.  Use
        :func:`canonical_searchable_text` when alt/aria-label values and
        JSON-LD must also count as surviving context.
        """
        return " ".join(str(run) for run in self.document["content"]["html"]["visible_text_runs"])


def canonicalize_sanitized_capture(capture: dict[str, Any]) -> CanonicalExtractionContent:
    """Canonicalize one sanitized capture JSON object into versioned bytes.

    Deliberate exclusions from the capture envelope: ``collection_mode`` is
    transport/provenance metadata rather than extraction content, and
    ``reference_date`` is a wall-clock capture stamp rather than page content,
    so two captures differing only by it produce identical bytes.  Every raw
    HTML surface (top level or nested) is replaced by its canonical content
    projection.  Everything else the extraction contract needs - source
    identity, visible text, decoded link targets, book attributes, structured
    data, nested detail pages, and any unknown future capture field -
    survives into the bytes.
    """
    document = build_canonical_document(capture)
    return CanonicalExtractionContent(
        schema_version=CANONICAL_EXTRACTION_CONTENT_SCHEMA_VERSION,
        document=document,
        canonical_bytes=canonical_json_bytes(document),
    )


def canonical_content_digest(capture: dict[str, Any]) -> str:
    """Return the SHA-256 digest of the canonical extraction-content bytes."""
    return canonicalize_sanitized_capture(capture).sha256


def _iter_surfaces(value: Any):
    """Yield every canonical HTML content surface in deterministic key order.

    A surface is any projected HTML-surface dict, recognizable by its
    ``visible_text_runs`` key.  Walks containers depth-first with sorted dict
    keys, so the yield order is fully deterministic for any document.
    """
    if isinstance(value, dict):
        if "visible_text_runs" in value:
            yield value
        for key in sorted(value):
            yield from _iter_surfaces(value[key])
    elif isinstance(value, list):
        for item in value:
            yield from _iter_surfaces(item)


def canonical_searchable_text(content: CanonicalExtractionContent) -> str:
    """Join every model-visible text carrier across all surfaces.

    This covers visible text, retained ``alt``/``aria-label`` values, and
    JSON-LD values, in deterministic key order.  A title that survives only
    in an image alt text, an aria label, or structured data counts as
    surviving.
    """
    parts: list[str] = []
    for surface in _iter_surfaces(content.document["content"]):
        parts.extend(str(run) for run in surface.get("visible_text_runs", []))
        parts.extend(str(item.get("value", "")) for item in surface.get("retained_attributes", []))
        parts.extend(str(block) for block in surface.get("json_ld", []))
    return " ".join(parts)


def canonical_link_values(content: CanonicalExtractionContent) -> list[str]:
    """Return every decoded link target across all surfaces, in document order."""
    values: list[str] = []
    for surface in _iter_surfaces(content.document["content"]):
        values.extend(str(link.get("value", "")) for link in surface.get("link_targets", []))
    return values


__all__ = [
    "CANONICAL_EXTRACTION_CONTENT_SCHEMA_VERSION",
    "CanonicalExtractionContent",
    "canonical_content_digest",
    "canonical_json_bytes",
    "canonical_link_values",
    "canonical_searchable_text",
    "canonicalize_sanitized_capture",
    "collapse_whitespace",
]
