"""Author values that are placeholder text, not people (Issue #852).

An extractor sometimes writes its own refusal into the author field: on
2026-09-20 BookPage Nonfiction produced the Russian for "not stated in HTML"
for *Valiant Women*, which became a second identity beside the real
``Lena Andrews`` record (#849). Such a value is never admitted as an author.

Matching is deliberately narrow. The list below is closed, and an author value
matches only when the whole value, after normalization, is one phrase,
optionally followed by one of the closed locator qualifiers (``in HTML``,
``on the page``, and their Russian forms). A real name that merely contains a
listed word, such as a surname ``Unknown`` beside a given name, never matches.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from itertools import pairwise

from autoscout.identity import normalize_author_identity
from autoscout.identity import split_author_list

PLACEHOLDER_AUTHOR_DROP_REASON = "placeholder_author"
# Issue #854: every author value is a credential, such as "MD", naming nobody.
NO_PERSON_AUTHOR_DROP_REASON = "no_person_author"

# Whole-value refusal phrases in the project's languages.
PLACEHOLDER_AUTHOR_PHRASES = (
    # Russian
    "не указан",
    "не указано",
    "не указана",
    "не указаны",
    "автор не указан",
    "автор не указана",
    "автор неизвестен",
    "неизвестный автор",
    "нет автора",
    # English
    "not stated",
    "not specified",
    "not provided",
    "not listed",
    "unknown",
    "unknown author",
    "author unknown",
    "no author",
    "n/a",
    "anonymous source",
    # German
    "nicht angegeben",
    "unbekannt",
    "autor unbekannt",
    # French
    "inconnu",
    "auteur inconnu",
    "non précisé",
    # Spanish
    "desconocido",
    "autor desconocido",
    "no especificado",
)

# Where the extractor says it looked; only these may follow a phrase.
PLACEHOLDER_AUTHOR_QUALIFIERS = (
    "",
    "в html",
    "на странице",
    "в источнике",
    "in html",
    "in the html",
    "on page",
    "on the page",
    "in source",
    "in the source",
)

_PLACEHOLDER_VALUES = frozenset(
    normalize_author_identity(f"{phrase} {qualifier}")
    for phrase in PLACEHOLDER_AUTHOR_PHRASES
    for qualifier in PLACEHOLDER_AUTHOR_QUALIFIERS
)


def is_placeholder_author(value: object) -> bool:
    """Return whether one whole author value is placeholder or refusal text."""
    normalized = normalize_author_identity(str(value or ""))
    return bool(normalized) and normalized in _PLACEHOLDER_VALUES


def without_placeholder_authors(authors: Iterable[object]) -> list[object]:
    """Return the author values that name people, in their given order."""
    return [author for author in authors if not is_placeholder_author(author)]


# Issue #854: one author value can list several people, end in a contributor
# tail, or carry credentials. Admission turns it into the people it names
# before any identity key exists; ``normalize_author_display`` and
# ``normalized_identity_key`` are not changed, so stored keys do not move.

# Contributor tails mean "and more people"; they are never a person.
_CONTRIBUTOR_TAIL = re.compile(
    r"(?:^|[\s,;]+)(?:and\s+others|et\s+al\.?|u\.\s?a\.|y\s+otros|e\s+altri|и\s+др\.?)\s*$",
    flags=re.IGNORECASE,
)
# Honorifics before a name and credentials after it, each as a whole token.
# The whole German medical prefix goes first, in every form the identity
# normalizer accepts (``Dr. med.``, ``Dr med``, ``Dr.med``, ...), so ``med`` is
# never left in front of the name.
_LEADING_HONORIFIC = re.compile(r"^(?:dr\.?\s*med(?:\.\s*|\s+)|(?:dr|prof)(?:\.\s*|\s+)|med\.\s*)", flags=re.IGNORECASE)
_TRAILING_CREDENTIAL = re.compile(r"(?:^|[\s,;]+)(?:m\.?\s?d|ph\.?\s?d)\.?\s*$", flags=re.IGNORECASE)
_CREDENTIAL_VALUES = frozenset({"md", "m d", "phd", "ph d", "dr", "dr med", "med", "prof"})
# A generational suffix belongs to the name before it: "Connick, Harry, Jr.".
_GENERATIONAL_VALUES = frozenset({"jr", "sr", "ii", "iii", "iv"})
# A conjunction that opens the last item of a comma list: "A, B, and C".
_LIST_CONJUNCTION = re.compile(r"^(?:and|&|und|y|e|et|и)\s+")


def _is_credential(value: str) -> bool:
    return normalize_author_identity(value) in _CREDENTIAL_VALUES


def _without_credentials(name: str) -> str:
    """Strip leading honorifics and trailing credentials from one person's name."""
    previous = None
    while previous != name:
        previous = name
        name = _LEADING_HONORIFIC.sub("", name).strip()
        name = _TRAILING_CREDENTIAL.sub("", name).strip().rstrip(",;").strip()
    return name


def _without_contributor_tail(value: str) -> tuple[str, bool]:
    had_tail = False
    while True:
        match = _CONTRIBUTOR_TAIL.search(value)
        if match is None:
            return value, had_tail
        had_tail = True
        value = value[: match.start()].strip().rstrip(",;").strip()


def _is_name_suffix(value: str) -> bool:
    return _is_credential(value) or normalize_author_identity(value) in _GENERATIONAL_VALUES


def _comma_list(value: str, had_tail: bool) -> list[str]:
    """Return the people a comma-joined value names, or the value as one name.

    A credential or generational suffix segment belongs to the name before it
    (``Connick, Harry, Jr.``, ``Smith, John, MD``): a generational suffix stays
    as printed, a credential is dropped. Exactly one remaining comma with no
    tail is the "Surname, Given" display and stays one name (``Bildner,
    Jim``). Two or more commas list people, and so does a suffix in the middle
    (``John Doe, Jr., Jane Roe``). One comma before a contributor tail lists
    people only when both sides are several words long (``Barack Obama,
    Higher Ground, et al``), so ``Smith, John et al`` still reads as one
    surname-first name.
    """
    parts = [part.strip() for part in value.split(",") if part.strip()]
    segments: list[str] = []
    for part in parts:
        if segments and _is_name_suffix(part):
            if not _is_credential(part):
                segments[-1] = f"{segments[-1]}, {part}"
            continue
        segments.append(part)
    # A suffix followed by another name ends a person, so the value is a list.
    suffix_inside = any(
        _is_name_suffix(part) and not _is_name_suffix(following) for part, following in pairwise(parts[1:])
    )
    if not segments:
        return []
    if len(segments) == 1 or (
        len(segments) == 2
        and not suffix_inside
        and not (had_tail and all(len(segment.split()) > 1 for segment in segments))
    ):
        return [", ".join(segments)]
    return [_LIST_CONJUNCTION.sub("", segment).strip() for segment in segments]


def admitted_author_people(value: object) -> list[str]:
    """Return the people one raw author value names, each as printed.

    A spaced slash separates people (#756). Contributor tails are dropped, a
    comma list is split as ``_comma_list`` describes, and honorifics and
    credentials are stripped from each name; a value that is only a credential
    names nobody. Name order and spelling are otherwise kept.
    """
    people: list[str] = []
    seen: set[str] = set()
    for part in split_author_list(value):
        without_tail, had_tail = _without_contributor_tail(part)
        for name in _comma_list(without_tail, had_tail):
            person = _without_credentials(name)
            normalized = normalize_author_identity(person)
            if not normalized or _is_credential(person) or normalized in seen:
                continue
            seen.add(normalized)
            people.append(person)
    return people
