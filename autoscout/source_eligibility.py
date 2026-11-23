"""Canonical source-level eligibility predicates."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from autoscout.identity import normalize_identity_text
from autoscout.identity import normalize_work_title

AWARD_NO_SPECIFIC_WORK_REASON = "award citation names no specific work"
FICTION_REASON = "fiction"
NON_BOOK_MATERIAL_REASON = "non-book material"
INVALID_FIXTURE_RECORD_REASON = "invalid fixture record"

# Keep this list deliberately bounded.  These are citation wrappers, not a
# generic title parser: only the award family may reject these exact or close
# variants, while the same title from another source family remains eligible.
_AWARD_NO_SPECIFIC_WORK_TITLE = re.compile(
    r"^(?:(?:a|au|de|l|la|le|pour|prix|un|une)\s+)*"
    r"ensemble\s+de\s+(?:son|l)\s+oeuvre(?:\s+(?:complete|compl[eè]te|entier|enti[eè]re))?$",
    re.IGNORECASE,
)


# These are source-specific evidence corrections, not title-wide bans.  The
# same title remains eligible when an independent source identifies a real
# book.  The Audible entries are courses or audio documentary series even
# though the captured extraction incorrectly labelled them as books; the
# Nonfiction.fr entry is a deliberately synthetic fixture record.
_SOURCE_WORK_EXCLUSIONS: dict[tuple[str, str], str] = {
    # Issue #1004: retailer category misclassifies this crime novel.
    ("audiobooks com nonfiction audiobook bestsellers", "the return|lone theils"): FICTION_REASON,
    (
        "audible bestselling science engineering audiobooks",
        "chemistry and our universe|ron b davis",
    ): NON_BOOK_MATERIAL_REASON,
    (
        "audible bestselling science engineering audiobooks",
        "how the earth works|michael e wysession",
    ): NON_BOOK_MATERIAL_REASON,
    (
        "audible bestselling computers technology audiobooks",
        "understanding artificial intelligence|patrick grim",
    ): NON_BOOK_MATERIAL_REASON,
    (
        "audible de bestselling biografien erinnerungen hoerbuecher",
        "mafia zwischen romantik und gewalt|bosepark productions gmbh",
    ): NON_BOOK_MATERIAL_REASON,
    (
        "audible de bestselling biografien erinnerungen hoerbuecher",
        "family business|undone work gmbh",
    ): NON_BOOK_MATERIAL_REASON,
    (
        "nonfiction fr critiques",
        "les futurs en partage|élodie exemple",
    ): INVALID_FIXTURE_RECORD_REASON,
}


def award_title_has_no_specific_work(value: Any) -> bool:
    """Return whether a title is the bounded lifetime/prize citation pattern."""
    normalized = normalize_identity_text(str(value or "").replace("œ", "oe").replace("Œ", "OE"))
    return bool(normalized and _AWARD_NO_SPECIFIC_WORK_TITLE.fullmatch(normalized))


def source_eligibility_exclusion_reason(*, title: Any, source_family: Any) -> str:
    """Return the canonical exclusion reason for one source mention.

    The no-specific-work rule is restricted to the exact ``award`` source
    family and the bounded citation pattern above.  It does not reject a
    generic title from a non-award source.
    """
    if str(source_family or "").strip().casefold() == "award" and award_title_has_no_specific_work(title):
        return AWARD_NO_SPECIFIC_WORK_REASON
    return ""


def _source_work_key(title: Any, authors: Any) -> str:
    """Return the same bounded title/author identity used by candidates."""
    if isinstance(authors, list):
        author_values = [normalize_identity_text(str(author)) for author in authors if str(author).strip()]
    else:
        author_values = [normalize_identity_text(str(authors))] if str(authors).strip() else []
    normalized_title = normalize_work_title(str(title))
    return f"{normalized_title}|{' & '.join(author_values)}" if normalized_title and author_values else ""


def _source_work_exclusion_reason(*, title: Any, authors: Any, source_name: Any, source_family: Any) -> str:
    source_key = normalize_identity_text(str(source_name))
    work_key = _source_work_key(title, authors)
    if not source_key or not work_key:
        return ""
    return _SOURCE_WORK_EXCLUSIONS.get((source_key, work_key), "")


def source_mention_eligibility_exclusion_reason(mention: Mapping[str, Any]) -> str:
    """Return the canonical source eligibility reason for a normalized mention."""
    source_family = mention.get("source_family") or mention.get("signal_family") or mention.get("signal_type", "")
    reason = source_eligibility_exclusion_reason(title=mention.get("title", ""), source_family=source_family)
    if reason:
        return reason

    # Candidates carry source-specific metadata in source_evidence rather than
    # at the top level. Check every source signal so a bad current snapshot is
    # projected out immediately, while a title-wide ban is still avoided.
    evidence_records: list[Mapping[str, Any]] = [mention]
    nested_evidence = mention.get("source_evidence", [])
    if isinstance(nested_evidence, Mapping):
        evidence_records.append(nested_evidence)
    elif isinstance(nested_evidence, list):
        evidence_records.extend(item for item in nested_evidence if isinstance(item, Mapping))
    primary_signal = mention.get("primary_signal")
    if isinstance(primary_signal, Mapping):
        evidence_records.append(primary_signal)
    for evidence in evidence_records:
        reason = _source_work_exclusion_reason(
            title=mention.get("title", ""),
            authors=mention.get("primary_authors", []),
            source_name=evidence.get("source_name", ""),
            source_family=evidence.get("source_family")
            or evidence.get("signal_family")
            or evidence.get("signal_type", ""),
        )
        if reason:
            return reason
    return ""


def historical_record_source_eligibility_exclusion_reason(record: Mapping[str, Any]) -> str:
    """Return a projection-only exclusion reason for an old history record.

    Historical records may store source-family metadata in several nested
    snapshots.  A record is filtered only when all known source-family values
    are ``award``.  This avoids hiding a legitimate non-award record that uses
    the same generic title and never mutates the stored history.
    """
    families: set[str] = set()
    for field_name in ("source_family", "signal_family", "signal_type"):
        value = str(record.get(field_name, "")).strip().casefold()
        if value:
            families.add(value)
    for field_name in ("independent_signal_families",):
        values = record.get(field_name, [])
        if isinstance(values, list):
            families.update(str(value).strip().casefold() for value in values if str(value).strip())
    for field_name in ("primary_signal", "source_evidence"):
        values = record.get(field_name, [])
        if isinstance(values, Mapping):
            values = [values]
        if isinstance(values, list):
            for value in values:
                if not isinstance(value, Mapping):
                    continue
                for family_field in ("source_family", "signal_family", "signal_type"):
                    family = str(value.get(family_field, "")).strip().casefold()
                    if family:
                        families.add(family)
                reason = _source_work_exclusion_reason(
                    title=record.get("title", ""),
                    authors=record.get("primary_authors", []),
                    source_name=value.get("source_name", ""),
                    source_family=value.get("source_family")
                    or value.get("signal_family")
                    or value.get("signal_type", ""),
                )
                if reason:
                    return reason
    if families and families <= {"award"}:
        return source_eligibility_exclusion_reason(title=record.get("title", ""), source_family="award")
    return ""
