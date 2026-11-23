"""Validation for non-authoritative exact-list helper metadata."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autoscout.ranking_author_policy import mention_author_exempt
from autoscout.source_language import is_english_language
from autoscout.source_proposals import normalize_source_identity

HELPER_METADATA_FIELDS: tuple[str, ...] = (
    "english_title",
    "search_translation",
    "author_transliteration_note",
)

HELPER_METADATA_TOP_LEVEL_FIELDS: frozenset[str] = frozenset({"version", "source_url", "language", "entries"})
HELPER_METADATA_ENTRY_FIELDS: frozenset[str] = frozenset({"title", "author", "evidence_url", *HELPER_METADATA_FIELDS})


class HelperMetadataError(ValueError):
    """A configured helper sidecar cannot be used safely."""

    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class HelperMetadataIndex:
    """Validated helper values indexed by exact authoritative identity."""

    source_url: str
    language: str
    entries: dict[tuple[str, str, str], dict[str, str]]
    sha256: str
    raw_bytes: bytes


HELPER_PLACEHOLDER_RE = re.compile(
    r"""
    ^\s*(?:
        (?:(?:helper\s+)?(?:unavailable|not\s+available)|n\s*/\s*a|todo|placeholder)
        (?:\s*[:=-]\s*.*)?
        |unknown
    )\s*$
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)


def _helper_error(message: str, reason_code: str) -> HelperMetadataError:
    return HelperMetadataError(message, reason_code=reason_code)


def _safe_sidecar_bytes(config_path: Path, raw_path: Any) -> bytes:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise _helper_error("helper metadata file is not configured safely", "helper_metadata_unsafe")
    path = Path(raw_path.strip())
    if not path.is_absolute():
        path = config_path.parent / path
    try:
        metadata = os.lstat(path)
    except FileNotFoundError as exc:
        raise _helper_error("helper metadata file is missing", "helper_metadata_missing") from exc
    except OSError as exc:
        raise _helper_error("helper metadata file is unreadable", "helper_metadata_unreadable") from exc
    if os.path.islink(path) or not os.path.isfile(path) or metadata.st_nlink != 1:
        raise _helper_error("helper metadata file is unsafe", "helper_metadata_unsafe")
    try:
        return path.read_bytes()
    except (OSError, UnicodeError) as exc:
        raise _helper_error("helper metadata file is unreadable", "helper_metadata_unreadable") from exc


def load_helper_metadata(config_path: Path, source: Mapping[str, Any]) -> HelperMetadataIndex | None:
    """Load and validate one configured version-1 helper sidecar.

    Relative paths are resolved from the configuration file. The sidecar is
    treated as retained input only. This function does not infer, translate,
    or repair any helper value.
    """
    raw_path = source.get("helper_metadata_file")
    if not raw_path:
        return None
    raw_bytes = _safe_sidecar_bytes(config_path, raw_path)
    try:
        value = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _helper_error("helper metadata file is malformed JSON", "helper_metadata_unreadable") from exc
    if not isinstance(value, Mapping):
        raise _helper_error("helper metadata must be a JSON object", "helper_metadata_schema_invalid")
    unknown_top_level = set(value) - HELPER_METADATA_TOP_LEVEL_FIELDS
    if unknown_top_level:
        raise _helper_error("helper metadata has unknown fields", "helper_metadata_schema_invalid")
    if isinstance(value.get("version"), bool) or value.get("version") != 1:
        raise _helper_error("helper metadata version must be 1", "helper_metadata_schema_invalid")
    configured_url = str(source.get("url", "")).strip()
    sidecar_url = value.get("source_url")
    if (
        not isinstance(sidecar_url, str)
        or not sidecar_url.strip()
        or normalize_source_identity(sidecar_url) != normalize_source_identity(configured_url)
    ):
        raise _helper_error("helper metadata source URL does not match the configured source", "helper_metadata_stale")
    language = str(source.get("language", "")).strip()
    sidecar_language = value.get("language")
    if (
        not isinstance(sidecar_language, str)
        or not sidecar_language.strip()
        or sidecar_language.casefold() != language.casefold()
    ):
        raise _helper_error("helper metadata language does not match the configured source", "helper_metadata_stale")
    entries = value.get("entries")
    if not isinstance(entries, list) or not entries:
        raise _helper_error("helper metadata entries must be a non-empty list", "helper_metadata_schema_invalid")

    by_identity: dict[tuple[str, str, str], dict[str, str]] = {}
    # The sidecar-match check above refuses a blank source language before
    # this line, so `language` is provably nonblank here.
    is_english = is_english_language(language)
    author_note_optional = mention_author_exempt(source)
    for index, raw_entry in enumerate(entries):
        if not isinstance(raw_entry, Mapping):
            raise _helper_error(f"helper metadata entry {index + 1} is not an object", "helper_metadata_entry_invalid")
        if set(raw_entry) - HELPER_METADATA_ENTRY_FIELDS:
            raise _helper_error(
                f"helper metadata entry {index + 1} has unknown fields", "helper_metadata_entry_invalid"
            )
        identity_values: list[str] = []
        for field in ("title", "author", "evidence_url"):
            candidate = raw_entry.get(field)
            if field == "author" and author_note_optional and isinstance(candidate, str) and not candidate.strip():
                identity_values.append("")
                continue
            if not isinstance(candidate, str) or not candidate.strip():
                raise _helper_error(
                    f"helper metadata entry {index + 1} has a blank identity field", "helper_metadata_entry_invalid"
                )
            identity_values.append(candidate.strip())
        identity = (identity_values[0], identity_values[1], identity_values[2])
        if identity in by_identity:
            raise _helper_error("helper metadata contains duplicate identity", "helper_metadata_duplicate_entry")
        if author_note_optional and not identity[1] and "author_transliteration_note" in raw_entry:
            raise _helper_error(
                "author_transliteration_note is not allowed for an authorless entry", "helper_metadata_entry_invalid"
            )
        helper_error = validate_helper_metadata(
            raw_entry,
            title_relation=not is_english,
            author_note_required=not (author_note_optional and not identity[1]),
        )
        if helper_error:
            raise _helper_error(helper_error, "helper_metadata_entry_invalid")
        by_identity[identity] = {
            field: str(raw_entry[field]).strip()
            for field in HELPER_METADATA_FIELDS
            if isinstance(raw_entry.get(field), str) and raw_entry[field].strip()
        }

    digest = hashlib.sha256(raw_bytes).hexdigest()
    expected_digest = str(source.get("helper_metadata_sha256", "")).strip()
    if expected_digest and expected_digest != digest:
        raise _helper_error(
            "helper metadata file digest does not match the configured artifact", "helper_metadata_stale"
        )
    return HelperMetadataIndex(
        source_url=normalize_source_identity(configured_url),
        language=language,
        entries=by_identity,
        sha256=digest,
        raw_bytes=raw_bytes,
    )


def _author_display_candidates(entry: Mapping[str, Any]) -> tuple[str, ...]:
    candidates: list[str] = []
    for field in ("author_display", "author"):
        value = entry.get(field)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())
    for field in ("primary_authors", "authors"):
        values = entry.get(field)
        if not isinstance(values, list):
            continue
        authors = [str(value).strip() for value in values if str(value).strip()]
        if not values:
            candidates.append("")
        elif len(authors) == 1:
            candidates.append(authors[0])
        elif authors:
            candidates.append(" / ".join(authors))
    return tuple(dict.fromkeys(candidates))


def drop_title_duplicate_helpers(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Remove a ``search_translation`` that merely repeats the authoritative title.

    ``search_translation`` is always a translation, so a value equal to the title
    carries none and counts as absent: the curated sidecar may still supply the
    real value, and an entry with no sidecar row fails as missing helper metadata
    instead of as a title duplicate. ``english_title`` is left alone because its
    relation to the title is contract specific.
    """
    cleaned = dict(entry)
    title = cleaned.get("title")
    if not isinstance(title, str) or not title.strip():
        return cleaned
    value = cleaned.get("search_translation")
    if isinstance(value, str) and value.strip() == title.strip():
        del cleaned["search_translation"]
    return cleaned


def enrich_with_helper_metadata(
    entry: Mapping[str, Any], index: HelperMetadataIndex
) -> tuple[dict[str, Any], tuple[str, str, str] | None]:
    """Apply one exact sidecar match without overwriting model values."""
    title = entry.get("title")
    evidence_url = entry.get("evidence_url") or entry.get("source_url")
    if not isinstance(title, str) or not title.strip() or not isinstance(evidence_url, str) or not evidence_url.strip():
        return dict(entry), None
    author_candidates = _author_display_candidates(entry)
    matches = [
        identity
        for identity in index.entries
        if identity[0] == title.strip() and identity[1] in author_candidates and identity[2] == evidence_url.strip()
    ]
    if len(matches) > 1:
        raise _helper_error("helper metadata match is ambiguous", "helper_metadata_ambiguous")
    if not matches:
        return dict(entry), None
    identity = matches[0]
    enriched = dict(entry)
    normalized_title = title.strip()
    # ``search_translation`` is always a translation. ``english_title`` is one
    # only for a non-English sidecar; an English list requires it to equal the
    # title, so a duplicate there is the correct value, not a missing one.
    sidecar_is_english = is_english_language(index.language)
    translated_fields = {"search_translation"} if sidecar_is_english else {"search_translation", "english_title"}
    for field, value in index.entries[identity].items():
        current = enriched.get(field)
        blank = not isinstance(current, str) or not current.strip()
        # A translated field that merely repeats the authoritative title carries
        # no translation. The curated sidecar row is the better value, so it
        # wins over that duplicate; any other model value stays untouched.
        title_duplicate = (
            field in translated_fields
            and isinstance(current, str)
            and current.strip() == normalized_title
            and value.strip() != normalized_title
        )
        if blank or title_duplicate:
            enriched[field] = value
    return enriched, identity


def validate_helper_metadata(
    entry: Mapping[str, Any], *, title_relation: bool | None, author_note_required: bool = True
) -> str:
    """Return a contract violation for invalid helper metadata.

    ``title_relation`` is true for normal non-English exact lists, false for
    normal English exact lists, and None for non-English strict charts.

    ``author_note_required`` is false only for a mention that legitimately
    carries no author, per ADR 0013.  There is no author to transliterate, and
    demanding a note would force an invented one.
    """
    values: dict[str, str] = {}
    required_fields = [
        field for field in HELPER_METADATA_FIELDS if author_note_required or field != "author_transliteration_note"
    ]
    missing = [field for field in required_fields if not isinstance(entry.get(field), str) or not entry[field].strip()]
    if missing:
        return "exact book list helper metadata is missing: " + ", ".join(missing)

    for field in required_fields:
        value = entry[field].strip()
        if HELPER_PLACEHOLDER_RE.search(value):
            return f"exact book list helper metadata {field} is a placeholder"
        values[field] = value

    title = entry.get("title")
    if not isinstance(title, str) or not title.strip():
        return "exact book list helper metadata requires a nonblank title"
    title = title.strip()
    english_title = values["english_title"]
    search_translation = values["search_translation"]
    # A non-English title may read the same in English: a name ("Stalin") or a
    # shared word ("Memorial", "Moonwalk").  The helper is non-authoritative, so
    # an equal English title is accepted rather than costing the book (2026-11-03).
    if title_relation is False and english_title != title:
        return "exact book list English english_title must equal title"
    if search_translation == title:
        return "exact book list search_translation must differ from title"
    return ""
