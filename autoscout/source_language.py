"""One owner for the source-language question (Issue #645).

Every extraction contract branch and every contract validator must agree on
whether a source needs the English helper fields (``english_title``,
``search_translation``, ``author_transliteration_note``). Two branches used
to read an empty language as non-English while a third read it as English,
so German sources lost the helpers silently. An empty language is an error
here, not a default: configuration validation requires every source to
declare one, so a blank value reaching this module is a programming error
and fails loudly instead of silently picking a meaning.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

ENGLISH_LANGUAGE_NAMES = frozenset({"en", "english"})

#: Required English helper fields shared by every contract family.
ENGLISH_HELPER_REQUIRED_FIELDS: tuple[str, ...] = (
    "english_title",
    "search_translation",
    "author_transliteration_note",
)

#: Canonical mention-shape descriptions for the helper fields. One copy owns
#: the wording so the four family branches cannot drift apart again.
ENGLISH_HELPER_MENTION_FIELDS: dict[str, str] = {
    "english_title": "Non-authoritative English title helper",
    "search_translation": "Non-authoritative Russian search-translation helper",
    "author_transliteration_note": "Non-authoritative Russian author-transliteration helper note",
}

#: Chart-only author helpers. Charts identify audio narrators and series
#: context the list families never carry, so only the chart branches add
#: these two on top of the shared three.
ENGLISH_CHART_EXTRA_HELPER_MENTION_FIELDS: dict[str, str] = {
    "english_author_display": "Non-authoritative English author display helper",
    "english_annotation": "Non-authoritative English annotation helper",
}


#: Display labels for the authority rule. Codes outside this map read as
#: the generic "non-English".
LANGUAGE_DISPLAY_NAMES = {
    "pt": "Portuguese",
    "de": "German",
    "fr": "French",
    "it": "Italian",
}


def source_language(source: Mapping[str, Any]) -> str:
    """Return the normalized declared language of one configured source."""
    return str(source.get("language", "")).strip().casefold()


def is_english_language(language: object) -> bool:
    """Return whether one raw language value names English.

    This is the only place that owns the English-name set. Blank values
    read as not English here; sources must still declare a language, and
    :func:`requires_english_helper_fields` enforces that.
    """
    return str(language).strip().casefold() in ENGLISH_LANGUAGE_NAMES


def requires_english_helper_fields(source: Mapping[str, Any]) -> bool:
    """Return whether a source needs the English helper fields.

    Raises ``ValueError`` when the source declares no language. The empty
    case has no default reading: two former branches treated it as
    non-English and one as English, and configuration validation rejects
    undeclared languages before any contract is built.
    """
    language = source_language(source)
    if not language:
        name = str(source.get("name", "unnamed source")).strip() or "unnamed source"
        raise ValueError(f"source {name!r} declares no language: cannot decide the helper contract")
    return not is_english_language(language)


def apply_english_helper_contract(
    mention_shape: dict[str, Any],
    required_fields: list[str],
    *,
    chart_extras: bool = False,
) -> None:
    """Add the shared helper fragment to one family's contract in place."""
    if chart_extras:
        mention_shape.update(ENGLISH_CHART_EXTRA_HELPER_MENTION_FIELDS)
    mention_shape.update(ENGLISH_HELPER_MENTION_FIELDS)
    required_fields += list(ENGLISH_HELPER_REQUIRED_FIELDS)


def english_helper_authority_rules(source: Mapping[str, Any]) -> list[str]:
    """Return the shared non-English authority rules for one source.

    The original-language title and author stay authoritative; the helper
    fields are search and reading aids only. Chart families carry their own
    stricter search-translation rule on top; exact-book-list sources carry
    the English-equality rule instead. Everything else shares this text.
    """
    language_label = LANGUAGE_DISPLAY_NAMES.get(source_language(source), "non-English")
    return [
        f"The original {language_label} title and primary author are authoritative; english_title, search_translation, and author_transliteration_note are non-authoritative helpers only.",
        "Do not invent canonical translations or transliterations; helper values are only search aids and never replace the original metadata.",
        "search_translation must be nonblank and different from the authoritative title: give a plain Russian rendering of the title for search.",
        "Never duplicate the authoritative title into search_translation. Omit the field you cannot render instead of repeating the title.",
    ]
