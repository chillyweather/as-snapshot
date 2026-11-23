"""English-title backfill worklist and apply (Issue #645).

A contract change reaches the next extraction only. The existing rows stay
empty without a backfill, which reuses the description-backfill shape: a
metadata-only worklist, a preview, then an apply behind an explicit
confirmation.

An English title is non-authoritative helper metadata. The apply step can
only fill a blank ``english_title`` with a typed helper bundle; it cannot
express an authoritative title or author change, it never overwrites a
nonblank helper (valid or editor-corrected), and it never overwrites valid
``sourced_published`` title evidence even behind the overwrite flag.
"""

from __future__ import annotations

from typing import Any

from autoscout.helper_metadata import HELPER_PLACEHOLDER_RE
from autoscout.source_language import is_english_language
from autoscout.title_evidence import classify_english_title
from autoscout.title_evidence import clean_title
from autoscout.title_evidence import is_valid_sourced_published_title


def prepare_english_title_backfill(
    history: list[dict[str, Any]],
    *,
    editor_titles: set[str] | None = None,
    source_languages: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return metadata-only work items for history rows missing an English title.

    Rows that already carry a helper (valid, editor-corrected, or sourced
    evidence) are not actionable and stay out of the worklist, as are rows
    whose every evidence source is English: an English book needs no English
    title. Items never carry helper values themselves; curation happens in
    the artifacts file the editor confirms.
    """
    visible = editor_titles or set()
    items: list[dict[str, Any]] = []
    for record in history:
        if not isinstance(record, dict):
            continue
        title = clean_title(record.get("title", ""))
        if not title:
            continue
        if clean_title(record.get("english_title", "")):
            continue
        if is_valid_sourced_published_title(record):
            continue
        evidence = record.get("source_evidence") or []
        source_names = sorted(
            {
                str(entry.get("source_name", "")).strip()
                for entry in evidence
                if isinstance(entry, dict) and str(entry.get("source_name", "")).strip()
            }
        )
        evidence_urls = sorted(
            {
                str(entry.get("evidence_url", "")).strip()
                for entry in evidence
                if isinstance(entry, dict) and str(entry.get("evidence_url", "")).strip()
            }
        )
        if source_languages is not None and source_names:
            codes = [source_languages.get(name, "") for name in source_names]
            if all(code.strip() for code in codes) and all(is_english_language(code) for code in codes):
                # Every evidence source declares English: an English book
                # needs no English title. A source without a declared
                # language keeps the row actionable for the editor to judge.
                continue
        items.append(
            {
                "identity_key": record.get("identity_key", ""),
                "title": title,
                "primary_authors": list(record.get("primary_authors", [])),
                "source_names": source_names,
                "evidence_urls": evidence_urls,
                "editor_visible": title in visible,
            }
        )
    return items


def apply_english_title_backfill(
    history: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    *,
    allow_overwrite_valid: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Fill blank English titles from curated artifacts without overwriting.

    Returns the updated history and a counts report. Every artifact is
    judged independently: unknown identities and invalid helpers are
    counted, never applied, and unrelated rows are returned untouched.
    """
    report = {
        "applied": 0,
        "skipped_existing": 0,
        "skipped_evidence": 0,
        "invalid": 0,
        "unknown_identity": 0,
    }
    updated = [dict(record) if isinstance(record, dict) else record for record in history]
    updated_by_identity = {record.get("identity_key"): record for record in updated if isinstance(record, dict)}
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            report["invalid"] += 1
            continue
        identity = artifact.get("identity_key", "")
        record = updated_by_identity.get(identity)
        if record is None:
            report["unknown_identity"] += 1
            continue
        if is_valid_sourced_published_title(record):
            # Sourced title evidence is never overwritten, not even behind
            # the overwrite flag: it is identity evidence, not a helper.
            report["skipped_evidence"] += 1
            continue
        existing_helper = clean_title(record.get("english_title", ""))
        editor_touched = bool(str(record.get("editor_status", "")).strip())
        if existing_helper and (editor_touched or not allow_overwrite_valid):
            report["skipped_existing"] += 1
            continue
        helper = clean_title(artifact.get("english_title", ""))
        if not helper or HELPER_PLACEHOLDER_RE.search(helper):
            report["invalid"] += 1
            continue
        typed = classify_english_title({**record, "english_title": helper})
        if not typed.get("english_title"):
            report["invalid"] += 1
            continue
        # The artifact shape cannot express an authoritative title or author
        # change: only the helper bundle is written, never title/authors.
        record["english_title"] = typed["english_title"]
        if typed.get("english_title_type"):
            record["english_title_type"] = typed["english_title_type"]
        if typed.get("english_title_provenance"):
            record["english_title_provenance"] = typed["english_title_provenance"]
        report["applied"] += 1
    return updated, report
