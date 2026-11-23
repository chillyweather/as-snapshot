"""Work identity normalization — single authoritative implementation.

One place to decide what constitutes the «same» work when comparing
titles and author names across sources.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

# One author value can name several people. A source that prints its whole
# author cell joins them with a spaced slash: Bokhandlerforeningen prints
# ``Hevrøy, Hans Ola / Ngan, John`` (issue #756). Only a slash with whitespace
# on both sides separates people, so ``AC/DC`` and ``Hans/Ola`` stay one name.
AUTHOR_LIST_SLASH = re.compile(r"\s/\s")
# Identity matching also separates people at a literal ``&``, the join used by
# merged Candidate History displays.
AUTHOR_LIST_SEPARATOR = re.compile(r"&|\s/\s")


def split_author_list(value: object) -> list[str]:
    """Return the people a spaced-slash author value names, each as printed.

    A value without a spaced slash is returned as one entry, unchanged apart
    from surrounding whitespace. Name order and spelling are not rewritten.
    """
    text = str(value or "")
    if not AUTHOR_LIST_SLASH.search(text):
        return [text.strip()] if text.strip() else []
    return [part.strip() for part in AUTHOR_LIST_SLASH.split(text) if part.strip()]


def normalize_identity_text(value: str) -> str:
    """Collapse all non-word characters to spaces, lowercase, and strip.

    Used to compare titles and author names for identity matching,
    e.g. in Russian-edition checks and in work-identity deduplication.
    """
    return re.sub(r"[^\w]+", " ", value.lower(), flags=re.UNICODE).strip()


def normalize_author_identity(value: str) -> str:
    """Normalize an author name for identity matching, ignoring accents."""
    accent_free = "".join(
        character for character in unicodedata.normalize("NFKD", value) if not unicodedata.combining(character)
    )
    return normalize_identity_text(accent_free)


def normalize_work_title(value: str) -> str:
    """Normalize a title at work level, ignoring a colon-delimited subtitle.

    ASCII and fullwidth colons are equivalent subtitle delimiters.
    Sources commonly publish either the short title or ``Title: Subtitle``.
    The subtitle is presentation metadata and must not split mentions of the
    same work when the primary authors also match.
    """
    title = re.split(r"[:：]", value, maxsplit=1)[0]
    return normalize_identity_text(title)


_HONORIFIC_PREFIX = re.compile(
    r"^(?:(?:prof|mme|mlle|dr|pr)\.?(?:\s+|,\s*)|m(?:\.|,\s+))+",
    flags=re.IGNORECASE,
)
_CREDENTIAL_SUFFIX = re.compile(
    r"(?:[\s,;]+(?:m\.?\s*d|p\.?\s*h\.?\s*d|jr|sr)\.?\s*)+$",
    flags=re.IGNORECASE,
)

# This is an explicit allowlist, not a structural period/token stripper.
# Exact supported wrappers are ``Dr. med.``, ``Dr med.``, ``Dr. med``,
# ``Dr med``, ``Dr.med.``, and ``Dr.med``.
_GERMAN_MEDICAL_CREDENTIAL_PREFIXES = (
    "dr. med. ",
    "dr med. ",
    "dr. med ",
    "dr med ",
    "dr.med. ",
    "dr.med ",
)


def _strip_german_medical_credential_prefix(value: str) -> str:
    """Strip only allowlisted ``Dr. med.`` punctuation wrappers.

    Unknown short leading tokens such as ``X. Y.`` are intentionally left
    intact; adding a general period or token stripper would create false
    identity merges.
    """
    lowered = value.casefold()
    for prefix in _GERMAN_MEDICAL_CREDENTIAL_PREFIXES:
        if lowered.startswith(prefix):
            return value[len(prefix) :].lstrip()
    return value


def normalize_author_display(value: str) -> str:
    """Normalize an author name for identity comparison without rewriting facts.

    Maps the conservative ``Last, First`` display form to ``First Last``.
    It removes only allowlisted presentation wrappers: leading ``M.``, ``Mme``,
    ``Mlle``, ``Dr.``, ``Pr.``, ``Prof.``, and the German ``Dr. med.``
    punctuation variants (``Dr. med.``, ``Dr med.``, ``Dr. med``, ``Dr med``,
    ``Dr.med.``, and ``Dr.med``), plus trailing ``MD``, ``PhD``, ``Jr``, and
    ``Sr`` with their supported punctuation and comma variants.  Arbitrary
    short tokens such as ``X. Y.`` are not stripped.  Raw author values remain
    unchanged in candidate and editor-facing data, with one narrow exception:
    a Candidate History record merged from more than one source record (issue
    #444) deliberately stores this normalized display in its union
    ``primary_authors``, so ordinary candidate displays keep their raw shape
    while merged union displays are normalized.
    """
    value = str(value or "").strip().rstrip(",; ").strip()
    if not value:
        return ""

    value = _strip_german_medical_credential_prefix(value)
    value = _HONORIFIC_PREFIX.sub("", value).strip()
    while value:
        stripped = _CREDENTIAL_SUFFIX.sub("", value).strip().rstrip(",; ").strip()
        if stripped == value:
            break
        value = stripped

    if "," in value:
        parts = [part.strip() for part in value.split(",", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            value = f"{parts[1]} {parts[0]}"

    # A wrapper can occur in the second part of a surname-first display.
    value = _strip_german_medical_credential_prefix(value)
    value = _HONORIFIC_PREFIX.sub("", value).strip()
    while value:
        stripped = _CREDENTIAL_SUFFIX.sub("", value).strip().rstrip(",; ").strip()
        if stripped == value:
            break
        value = stripped
    return " ".join(value.split())


def normalize_isbn(raw: str) -> str:
    """Strip hyphens and spaces from an ISBN for identity comparison.

    Returns an empty string for blank input so callers can treat
    missing and whitespace-only ISBNs the same way.
    """
    value = str(raw or "").strip()
    if not value:
        return ""
    return value.replace("-", "").replace(" ", "")


# ── Identity comparison helpers (issue #275 review item A6) ─────────────


def normalized_identity_key(mention: dict[str, Any]) -> str:
    """Compute the exact normalized work-identity key for one mention."""
    title = normalize_work_title(str(mention.get("title", "")))
    authors = " & ".join(
        normalize_identity_text(normalize_author_display(str(author))) for author in mention.get("primary_authors", [])
    )
    return f"{title}|{authors}"


def normalized_contributor_set(mention: dict[str, Any]) -> frozenset[str]:
    """Return normalized contributor identities, splitting at ``&`` and a spaced slash.

    The spaced slash matters for records stored before admission split such
    values (issue #756): a stored ``A / B`` must still share an author with a
    newly admitted ``A``, ``B``, or the same book becomes a second candidate.
    """
    contributors: set[str] = set()
    for author in mention.get("primary_authors", []):
        for component in AUTHOR_LIST_SEPARATOR.split(str(author)):
            if not component.strip():
                continue
            normalized = normalize_author_identity(normalize_author_display(component))
            if normalized:
                contributors.add(normalized)
    return frozenset(contributors)


def normalized_title(mention: dict[str, Any]) -> str:
    """Normalized title (subtitle-insensitive)."""
    return normalize_work_title(str(mention.get("title", "")))


def normalized_isbn(mention: dict[str, Any]) -> str:
    """Normalized nonblank ISBN, or ''."""
    return normalize_isbn(str(mention.get("isbn", "")))


def identity_isbn(mention: dict[str, Any]) -> str:
    """Return the mention ISBN that may serve as identity proof, or ``''``.

    A grounded learned ISBN is discovery-only metadata: it helps discovery
    find a page but must never prove that two mentions are the same work
    (issue #531).  A mention marked ``isbn_discovery_only`` therefore
    contributes no ISBN to identity matching.  Unmarked ISBNs keep the
    historical strong-rule behavior.
    """
    if bool(mention.get("isbn_discovery_only", False)):
        return ""
    return normalized_isbn(mention)


def _match_isbn_pair(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """True when the pair shares a normalized nonblank ISBN (Rule 2).

    An ISBN marked discovery-only is ignored here: equal learned ISBNs are
    not proof that two mentions are the same work.
    """
    isbn_a = identity_isbn(a)
    isbn_b = identity_isbn(b)
    return bool(isbn_a and isbn_b and isbn_a == isbn_b)


def _match_shared_author_pair(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """True for the non-ISBN rule: same work title and one shared author."""
    title_a = normalized_title(a)
    title_b = normalized_title(b)
    if not title_a or title_a != title_b:
        return False
    contributors_a = normalized_contributor_set(a)
    contributors_b = normalized_contributor_set(b)
    return bool(contributors_a and contributors_b and contributors_a & contributors_b)


def mentions_are_identity_match(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Conservative identity match using the strong ISBN or work-author rule.

    A non-ISBN pair matches only when both mentions have the same nonblank
    normalized work title and share at least one normalized primary author.
    ISBN equality remains a strong work identity signal and can override
    different titles or authors.
    """
    return _match_isbn_pair(a, b) or _match_shared_author_pair(a, b)


# ── Deterministic canonical mention rank ─────────────────────────────────


def mention_canonical_rank(mention: dict[str, Any]) -> tuple[int, int, int, int, str, str, str, str]:
    """Deterministic rank for picking a canonical mention from a merged group.

    Lower values preferred.  Ties broken by increasingly specific fields
    until reaching a stable full-record tie-breaker.
    """
    contribs = normalized_contributor_set(mention)
    has_contributors = int(not contribs)  # 0 = has contributors (preferred), 1 = empty
    # Narrower nonempty contributor list preferred.
    contrib_size = len(contribs) if contribs else 0
    # Natural non-comma author display preferred.
    uses_comma = int(any("," in str(a) for a in mention.get("primary_authors", []) if str(a).strip()))
    # Subtitle-free title preferred.
    raw_title = str(mention.get("title", ""))
    has_subtitle = int(":" in raw_title)
    # Normalized identity key tie-breaker.
    ident_key = normalized_identity_key(mention)
    # Stable tie-breakers: raw title, raw authors, ISBN, then full-record.
    raw_authors = "|".join(str(a) for a in mention.get("primary_authors", []))
    isbn = normalized_isbn(mention)
    full_key = (
        raw_title
        + "|"
        + raw_authors
        + "|"
        + isbn
        + "|"
        + str(mention.get("publisher", ""))
        + "|"
        + str(mention.get("candidate_category", ""))
    )
    return (
        has_contributors,
        contrib_size,
        uses_comma,
        has_subtitle,
        ident_key,
        raw_title,
        raw_authors,
        full_key,
    )


def pick_canonical_mention(mentions: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the canonical mention from a merged group by deterministic rank."""
    return min(mentions, key=mention_canonical_rank)
