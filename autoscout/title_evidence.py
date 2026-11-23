"""Typed English-title helpers and the publication-evidence guard.

The authoritative work title remains ``title``.
``english_title`` is editor-facing helper metadata and never becomes an
identity signal unless the complete sourced-publication contract passes.
"""

from __future__ import annotations

from copy import deepcopy
from html import unescape
from typing import Any
from typing import cast
from urllib.parse import urlsplit

from autoscout.identity import normalize_author_display
from autoscout.identity import normalize_author_identity
from autoscout.identity import normalize_work_title
from autoscout.source_language import is_english_language

TITLE_TYPE_ORIGINAL = "original"
TITLE_TYPE_SOURCED_PUBLISHED = "sourced_published"
TITLE_TYPE_GENERATED_RENDERING = "generated_rendering"
ENGLISH_TITLE_TYPES = (
    TITLE_TYPE_ORIGINAL,
    TITLE_TYPE_SOURCED_PUBLISHED,
    TITLE_TYPE_GENERATED_RENDERING,
)

_MAX_RESEARCH_SOURCES = 10
_MAX_SOURCE_URL_LENGTH = 500


def clean_title(value: Any) -> str:
    """Unescape HTML entities and collapse whitespace without changing words."""
    return " ".join(unescape(str(value or "")).split())


def _clean_authors(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [clean_title(author) for author in value if clean_title(author)]


def _author_keys(authors: Any) -> frozenset[str]:
    keys: set[str] = set()
    for author in _clean_authors(authors):
        for component in author.split("&"):
            normalized = normalize_author_identity(normalize_author_display(component))
            if normalized:
                keys.add(normalized)
    return frozenset(keys)


def _http_source_urls(value: Any) -> list[str] | None:
    if not isinstance(value, list) or len(value) > _MAX_RESEARCH_SOURCES:
        return None
    urls: list[str] = []
    for raw_url in value:
        url = str(raw_url or "").strip()
        parsed = urlsplit(url)
        if (
            not url
            or len(url) > _MAX_SOURCE_URL_LENGTH
            or parsed.scheme.lower() not in {"http", "https"}
            or not parsed.netloc
            or url in urls
        ):
            return None
        urls.append(url)
    return urls


def _mapping_strings(mapping: Any, fields: tuple[str, ...]) -> bool:
    return isinstance(mapping, dict) and all(
        isinstance(mapping.get(field), str) and mapping[field].strip() for field in fields
    )


def _mapping_authors(mapping: Any, field: str) -> bool:
    return isinstance(mapping.get(field), list) and bool(_author_keys(mapping[field]))


def validate_title_provenance(
    provenance: Any,
    *,
    expected_kind: str | None = None,
    require_sources: bool = False,
) -> bool:
    """Validate the bounded JSON shape without treating it as identity proof."""
    if not isinstance(provenance, dict):
        return False
    kind = provenance.get("kind")
    if kind not in ENGLISH_TITLE_TYPES or (expected_kind and kind != expected_kind):
        return False
    sources = _http_source_urls(provenance.get("research_sources"))
    if sources is None or (require_sources and not sources):
        return False
    mapping = provenance.get("mapping")
    if kind == TITLE_TYPE_SOURCED_PUBLISHED:
        return (
            _mapping_strings(mapping, ("original_title", "published_title"))
            and _mapping_authors(mapping, "original_authors")
            and _mapping_authors(mapping, "published_authors")
            and bool(sources)
        )
    if kind == TITLE_TYPE_GENERATED_RENDERING:
        if sources:
            return False
        return mapping is None or (
            _mapping_strings(mapping, ("original_title",)) and _mapping_authors(mapping, "original_authors")
        )
    # An original title has no publication mapping or publication evidence.
    return not sources and mapping is None


def is_valid_sourced_published_title(candidate: dict[str, Any]) -> bool:
    """Return true only for a fully evidenced published English title."""
    if str(candidate.get("english_title_type", "")) != TITLE_TYPE_SOURCED_PUBLISHED:
        return False
    english_title = clean_title(candidate.get("english_title", ""))
    if not english_title:
        return False
    provenance = candidate.get("english_title_provenance")
    if not validate_title_provenance(provenance, expected_kind=TITLE_TYPE_SOURCED_PUBLISHED, require_sources=True):
        return False
    provenance_dict = cast(dict[str, Any], provenance)
    mapping = cast(dict[str, Any], provenance_dict["mapping"])
    if normalize_work_title(clean_title(mapping["original_title"])) != normalize_work_title(
        clean_title(candidate.get("title", ""))
    ):
        return False
    candidate_author_keys = _author_keys(candidate.get("primary_authors", []))
    if not candidate_author_keys:
        return False
    if _author_keys(mapping["original_authors"]) != candidate_author_keys:
        return False
    if _author_keys(mapping["published_authors"]) != candidate_author_keys:
        return False
    return normalize_work_title(clean_title(mapping["published_title"])) == normalize_work_title(english_title)


def effective_title_kind(record: dict[str, Any]) -> str:
    """Return the verified display kind, or empty for unverified data."""
    raw_kind = str(record.get("english_title_type", "")).strip()
    if raw_kind == TITLE_TYPE_SOURCED_PUBLISHED and is_valid_sourced_published_title(record):
        return raw_kind
    if raw_kind in {TITLE_TYPE_ORIGINAL, TITLE_TYPE_GENERATED_RENDERING} and validate_title_provenance(
        record.get("english_title_provenance"), expected_kind=raw_kind
    ):
        if raw_kind == TITLE_TYPE_ORIGINAL and clean_title(record.get("english_title")) != clean_title(
            record.get("title")
        ):
            return ""
        return raw_kind
    return ""


def title_is_identity_evidence(candidate: dict[str, Any]) -> bool:
    """Explicit identity guard used by callers that consider title evidence."""
    return is_valid_sourced_published_title(candidate)


def classify_english_title(mention: dict[str, Any], *, allow_sourced: bool = False) -> dict[str, Any]:
    """Return typed helper fields for one normalized collector mention.

    Invalid or absent claimed publication evidence is downgraded to a
    generated rendering, never silently accepted as a publication title.
    """
    title = clean_title(mention.get("title", ""))
    helper = clean_title(mention.get("english_title", ""))
    if not helper:
        return {}
    normalized: dict[str, Any] = {"english_title": helper}
    claimed = {
        **mention,
        "title": title,
        "english_title": helper,
        "primary_authors": _clean_authors(mention.get("primary_authors", [])),
    }
    if allow_sourced and is_valid_sourced_published_title(claimed):
        normalized["english_title_type"] = TITLE_TYPE_SOURCED_PUBLISHED
        normalized["english_title_provenance"] = deepcopy(mention["english_title_provenance"])
        return normalized
    if helper == title:
        normalized["english_title_type"] = TITLE_TYPE_ORIGINAL
        normalized["english_title_provenance"] = {
            "kind": TITLE_TYPE_ORIGINAL,
            "research_sources": [],
        }
        return normalized
    normalized["english_title_type"] = TITLE_TYPE_GENERATED_RENDERING
    normalized["english_title_provenance"] = {
        "kind": TITLE_TYPE_GENERATED_RENDERING,
        "research_sources": [],
        "mapping": {
            "original_title": title,
            "original_authors": _clean_authors(mention.get("primary_authors", [])),
        },
    }
    return normalized


def normalize_title_fields(
    mention: dict[str, Any], *, classify: bool = False, allow_sourced: bool = False
) -> dict[str, Any]:
    """Normalize title text in place and optionally classify a collector helper."""
    if "title" in mention:
        mention["title"] = clean_title(mention.get("title"))
    if "english_title" in mention and mention.get("english_title") is not None:
        mention["english_title"] = clean_title(mention.get("english_title"))
    if not mention.get("english_title"):
        mention.pop("english_title_type", None)
        mention.pop("english_title_provenance", None)
    elif classify:
        fields = classify_english_title(mention, allow_sourced=allow_sourced)
        for key, value in fields.items():
            mention[key] = value
    return mention


def _title_bundle(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(record[key])
        for key in ("english_title", "english_title_type", "english_title_provenance")
        if key in record
    }


def merge_title_metadata(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Choose helper metadata without allowing a rendering to replace evidence."""
    existing_sourced = is_valid_sourced_published_title(existing)
    incoming_sourced = is_valid_sourced_published_title(incoming)
    if existing_sourced:
        return _title_bundle(existing)
    if incoming_sourced:
        return _title_bundle(incoming)
    if str(existing.get("english_title", "")).strip():
        # A typed current helper can safely upgrade a legacy untyped helper.
        # A typed existing helper remains stable until sourced evidence wins.
        if (
            not str(existing.get("english_title_type", "")).strip()
            and str(incoming.get("english_title_type", "")).strip()
        ):
            return _title_bundle(incoming)
        return _title_bundle(existing)
    return _title_bundle(incoming)


def record_has_only_non_english_sources(record: dict[str, Any]) -> bool:
    """Return whether every declared evidence source is non-English.

    ``original`` only says the helper title equals the source title, so a
    book found only on non-English lists must not read as an English
    original (issue #969). Missing, empty, or malformed evidence keeps the
    previous behaviour and reports ``False``.
    """
    evidence = record.get("source_evidence")
    if not isinstance(evidence, list):
        return False
    languages = [
        str(entry.get("language", "")).strip()
        for entry in evidence
        if isinstance(entry, dict) and str(entry.get("language", "")).strip()
    ]
    return bool(languages) and not any(is_english_language(language) for language in languages)


def format_editor_english_title(record: dict[str, Any]) -> str:
    """Keep the CSV column stable while making verification status visible."""
    value = clean_title(record.get("english_title", ""))
    if not value:
        return ""
    kind = effective_title_kind(record)
    if kind == TITLE_TYPE_ORIGINAL and record_has_only_non_english_sources(record):
        # The helper repeats the source title the review already shows.
        return ""
    labels = {
        TITLE_TYPE_SOURCED_PUBLISHED: "published title",
        TITLE_TYPE_GENERATED_RENDERING: "machine translation",
        TITLE_TYPE_ORIGINAL: "original",
    }
    return f"{value} ({labels[kind]})" if kind in labels else f"{value} (unverified)"


def format_editor_english_author_display(record: dict[str, Any]) -> str:
    """Mark an English author helper without changing its source text."""
    value = clean_title(record.get("english_author_display", ""))
    if not value:
        return ""
    original_keys = _author_keys(record.get("primary_authors", []))
    helper_keys = _author_keys([value])
    kind = "original" if original_keys and helper_keys == original_keys else "transliteration"
    return f"{value} ({kind})"


def title_evidence_source_urls(record: dict[str, Any]) -> list[str]:
    """Expose source URLs only for structurally valid sourced titles."""
    if not is_valid_sourced_published_title(record):
        return []
    return list(record["english_title_provenance"]["research_sources"])
