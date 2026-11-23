"""Single source of truth for the Russian-publication status vocabulary.

Mirrors the ``EDITOR_STATUSES`` precedent in ``autoscout.editor_feedback``:
the three status values a Russian-edition check can produce are defined
exactly once, here, along with the predicates every other module must use
to compare against them.

Canonical casing is preserved as written into artifacts today (see ADR
0003 and the mention-schema allowed values in ``autoscout.source_mentions``)
so stored artifacts, state files, and CSVs remain byte-identical. The
predicates are tolerant on read -- they strip surrounding whitespace and
compare case-insensitively -- and canonical on write: callers that need to
emit a status or exclusion-reason string should source it from the
constants below rather than retyping a literal.
"""

from __future__ import annotations

FOUND_IN_RUSSIAN = "found in Russian"
NOT_FOUND_IN_RUSSIAN = "not found in Russian"
UNCLEAR = "unclear"

RUSSIAN_STATUSES = {
    FOUND_IN_RUSSIAN,
    NOT_FOUND_IN_RUSSIAN,
    UNCLEAR,
}


def _normalize(value: object) -> str:
    return str(value or "").strip().lower()


def is_found_in_russian(value: object) -> bool:
    return _normalize(value) == FOUND_IN_RUSSIAN.lower()


def is_not_found_in_russian(value: object) -> bool:
    return _normalize(value) == NOT_FOUND_IN_RUSSIAN.lower()


def is_unclear(value: object) -> bool:
    return _normalize(value) == UNCLEAR.lower()
