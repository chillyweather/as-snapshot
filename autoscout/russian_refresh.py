"""Bounded one-time Russian-edition refresh for the current candidate list.

This command is intentionally separate from the weekly pipeline.  It creates
an evidence preview first and applies only positive, metadata-backed RSL
transitions when explicitly requested with ``--apply``.
"""

from __future__ import annotations

import csv
import re
import time
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

from autoscout.identity import normalize_author_display
from autoscout.identity import normalize_identity_text
from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.russian_editions import is_ambiguous_russian_match
from autoscout.russian_editions import is_same_work_russian_edition
from autoscout.russian_editions import parse_rsl_live_response
from autoscout.russian_editions import rsl_live_search_response
from autoscout.russian_editions import russian_edition_evidence
from autoscout.russian_status import FOUND_IN_RUSSIAN
from autoscout.state_store import Collection
from autoscout.state_store import open_state_store

_RSL_METADATA_WORDS = {
    "текст",
    "портр",
    "табл",
    "прил",
    "сост",
    "пер",
    "обл",
    "карт",
    "илл",
    "экз",
}


def _identity_key(record: dict[str, Any]) -> str:
    existing = str(record.get("identity_key", "")).strip()
    if existing:
        return existing
    title = normalize_identity_text(str(record.get("title", "")))
    authors = " & ".join(
        normalize_identity_text(normalize_author_display(str(author)))
        for author in record.get("primary_authors", [])
        if str(author).strip()
    )
    return f"{title}|{authors}"


def _query_for(candidate: dict[str, Any]) -> str:
    isbn = str(candidate.get("isbn", "")).strip()
    if isbn:
        return isbn
    notes = candidate.get("russian_search_notes")
    if isinstance(notes, dict) and str(notes.get("search_translation", "")).strip():
        return str(notes["search_translation"]).strip()
    if str(candidate.get("search_translation", "")).strip():
        return str(candidate["search_translation"]).strip()
    title = str(candidate.get("title", "")).strip()
    # RSL bibliographic records commonly store translated/transliterated
    # author names (e.g. "Грин" rather than "Robert Greene").  An AND query
    # with the source-language author therefore hides genuine records.  Search
    # the title once, then validate the returned author in the adapter's
    # bounded match classification.
    return title


def _has_russian_bibliographic_text(result: dict[str, Any]) -> bool:
    result_title = str(result.get("result_title", ""))
    title_words = re.findall(r"[\u0400-\u04ff]{3,}", result_title.casefold())
    title_words = [word for word in title_words if word not in _RSL_METADATA_WORDS]
    # RSL uses Russian page-count and portrait abbreviations even on
    # foreign-language records.  Look for substantive Russian words in the
    # bibliographic title, not merely anywhere in page-count metadata.
    if title_words:
        return True
    return False


def _same_author_tokens(candidate: dict[str, Any], result: dict[str, Any]) -> bool:
    notes = candidate.get("russian_search_notes")
    note_author = notes.get("author_transliteration_note", "") if isinstance(notes, dict) else ""
    result_tokens = {
        token
        for token in normalize_identity_text(str(result.get("result_author", ""))).split()
        if token and token.isalpha()
    }
    if not result_tokens:
        return False
    for author in [*candidate.get("primary_authors", []), note_author]:
        candidate_tokens = {
            token for token in normalize_identity_text(str(author)).split() if token and token.isalpha()
        }
        if candidate_tokens and candidate_tokens == result_tokens:
            return True
    return False


def _has_cyrillic_author(result: dict[str, Any]) -> bool:
    return bool(re.search(r"[\u0400-\u04ff]{3,}", str(result.get("result_author", ""))))


def _title_is_bibliographic_segment(candidate: dict[str, Any], result: dict[str, Any]) -> bool:
    result_title = " ".join(str(result.get("result_title", "")).casefold().split())
    variants = [str(candidate.get("title", ""))]
    notes = candidate.get("russian_search_notes")
    if isinstance(notes, dict):
        variants.append(str(notes.get("search_translation", "")))
    variants.append(str(candidate.get("search_translation", "")))
    variants = [" ".join(value.casefold().split()) for value in variants if value.strip()]
    if not variants or not result_title:
        return False
    for candidate_title in variants:
        escaped = re.escape(candidate_title)
        match = re.search(rf"(?:^|[=:])\s*{escaped}(?:\s|$|[.:])", result_title)
        if match is None:
            continue
        if match.start() == 0:
            return True
        # When the original-language title follows an equals sign/colon,
        # require a Russian title before it.  This rejects map/catalog records
        # such as ``Te Ika a Maui: The Land and its people``.
        prefix = result_title[: match.start()]
        if _has_russian_bibliographic_text({"result_title": prefix}):
            return True
    return False


def _is_verified_rsl_match(candidate: dict[str, Any], result: dict[str, Any]) -> bool:
    if not is_same_work_russian_edition(result) and not is_ambiguous_russian_match(result):
        return False
    if not _title_is_bibliographic_segment(candidate, result):
        return False
    if not _has_russian_bibliographic_text(result):
        return False
    if _same_author_tokens(candidate, result):
        return True
    # Translated catalog records often contain the original title and a
    # Cyrillic author display (e.g. ``Грин, Роберт``), so exact source-language
    # author tokens are unavailable.  Accept that narrow case only when the
    # record explicitly identifies a translation and the title is a complete
    # bibliographic segment, never from a bare search snippet.
    metadata = " ".join(
        str(result.get(field, "")) for field in ("result_title", "publication_data", "evidence_note")
    ).casefold()
    original_title_in_record = _title_is_bibliographic_segment({"title": candidate.get("title", "")}, result)
    return bool(
        re.search(r"перевод|пер\.\s*с", metadata, re.I) or (original_title_in_record and _has_cyrillic_author(result))
    )


def _candidate_result(
    candidate: dict[str, Any],
    check: dict[str, Any],
    run_date: str,
) -> dict[str, Any]:
    query = _query_for(candidate)
    if not query:
        return {
            "identity_key": _identity_key(candidate),
            "title": str(candidate.get("title", "")),
            "authors": list(candidate.get("primary_authors", [])),
            "old_status": str(candidate.get("russian_edition_status", "")),
            "outcome": "not_attempted",
            "query": "",
            "proposed_status": "",
            "evidence": [],
        }

    body, error = rsl_live_search_response(check, query)
    if error:
        return {
            "identity_key": _identity_key(candidate),
            "title": str(candidate.get("title", "")),
            "authors": list(candidate.get("primary_authors", [])),
            "old_status": str(candidate.get("russian_edition_status", "")),
            "outcome": "failed",
            "query": query,
            "error": error,
            "proposed_status": "",
            "evidence": [],
        }

    results, parse_error = parse_rsl_live_response(body, query, candidate, check, run_date)
    if parse_error:
        return {
            "identity_key": _identity_key(candidate),
            "title": str(candidate.get("title", "")),
            "authors": list(candidate.get("primary_authors", [])),
            "old_status": str(candidate.get("russian_edition_status", "")),
            "outcome": "failed",
            "query": query,
            "error": parse_error,
            "proposed_status": "",
            "evidence": [],
        }

    positive = next(
        (result for result in results if _is_verified_rsl_match(candidate, result)),
        None,
    )
    if positive is not None:
        return {
            "identity_key": _identity_key(candidate),
            "title": str(candidate.get("title", "")),
            "authors": list(candidate.get("primary_authors", [])),
            "old_status": str(candidate.get("russian_edition_status", "")),
            "outcome": "matched",
            "query": query,
            "proposed_status": FOUND_IN_RUSSIAN,
            "evidence": [russian_edition_evidence(positive, run_date)],
        }

    ambiguous = next((result for result in results if is_ambiguous_russian_match(result)), None)
    return {
        "identity_key": _identity_key(candidate),
        "title": str(candidate.get("title", "")),
        "authors": list(candidate.get("primary_authors", [])),
        "old_status": str(candidate.get("russian_edition_status", "")),
        "outcome": "ambiguous" if ambiguous is not None else "no_match",
        "query": query,
        "proposed_status": "",
        "evidence": [russian_edition_evidence(ambiguous, run_date)] if ambiguous else [],
    }


def build_refresh_preview(
    candidates: list[dict[str, Any]],
    *,
    run_date: str,
    delay_seconds: float = 0.2,
    limit: int | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    check = {
        "name": "RSL",
        "family": "russian-library-catalog",
        "url": "https://search.rsl.ru/",
        "timeout_seconds": timeout_seconds,
    }
    selected = candidates[:limit] if limit is not None else candidates
    rows = []
    for index, candidate in enumerate(selected):
        if index and delay_seconds > 0:
            time.sleep(delay_seconds)
        rows.append(_candidate_result(candidate, check, run_date))
    counts: dict[str, int] = {}
    for row in rows:
        outcome = str(row.get("outcome", ""))
        counts[outcome] = counts.get(outcome, 0) + 1
    return {
        "schema_version": 1,
        "run_date": run_date,
        "source": {
            "name": "RSL",
            "source_type": "russian-library-catalog",
            "url": "https://search.rsl.ru/",
            "query_policy": "one ISBN or title query per candidate; author is validated from the record",
        },
        "candidate_count": len(selected),
        "counts": counts,
        "rows": rows,
    }


def _apply_row(record: dict[str, Any], row: dict[str, Any], run_date: str) -> bool:
    if row.get("outcome") != "matched" or row.get("proposed_status") != FOUND_IN_RUSSIAN:
        return False
    evidence = list(row.get("evidence") or [])
    if not evidence:
        return False
    # Revalidate the persisted preview at apply time.  This protects the
    # canonical state if a preview was produced by an older, more permissive
    # parser version and is later applied after the matcher was tightened.
    validation_record = dict(record)
    if row.get("russian_search_notes"):
        validation_record["russian_search_notes"] = row["russian_search_notes"]
    if row.get("search_translation"):
        validation_record["search_translation"] = row["search_translation"]
    if not _is_verified_rsl_match(validation_record, evidence[0]):
        return False
    record["russian_edition_status"] = FOUND_IN_RUSSIAN
    record["russian_edition_url"] = str(evidence[0].get("url", ""))
    record["russian_edition_evidence"] = deepcopy(evidence)
    record["russian_edition_note"] = f"RSL catalog metadata confirmed a Russian-language edition on {run_date}."
    record["russian_edition_status_source"] = "RSL live metadata refresh"
    return True


def apply_refresh_preview(state_dir: Path, preview: dict[str, Any]) -> dict[str, int]:
    """Apply a refresh preview to canonical state and to the run files kept beside it.

    Candidate History and the latest Scouting Candidates are read, updated and
    written in one State Store transaction; the current candidates, source
    mentions and editor CSV in *state_dir* are run files and stay files.
    """
    rows = {str(row.get("identity_key", "")): row for row in preview.get("rows", [])}
    run_date = str(preview.get("run_date", date.today().isoformat()))
    store = open_state_store(state_dir)
    context_by_identity: dict[str, dict[str, Any]] = {}

    def apply_rows(data: list[Any]) -> int:
        applied = 0
        for record in data:
            if not isinstance(record, dict):
                continue
            row = rows.get(_identity_key(record))
            if row is not None:
                row = dict(row)
                context = context_by_identity.get(_identity_key(record), {})
                for field in ("russian_search_notes", "search_translation"):
                    if context.get(field):
                        row.setdefault(field, context[field])
            if row and _apply_row(record, row, run_date):
                applied += 1
        return applied

    changed = 0
    with store.transaction():
        latest_candidates = store.read(Collection.LATEST_SCOUTING_CANDIDATES)
        context_by_identity.update(
            {_identity_key(record): record for record in latest_candidates if isinstance(record, dict)}
        )
        for collection in (Collection.CANDIDATE_HISTORY, Collection.LATEST_SCOUTING_CANDIDATES):
            records = store.read(collection)
            if not isinstance(records, list):
                continue
            collection_changed = apply_rows(records)
            if collection_changed:
                store.replace(collection, records)
                changed += collection_changed
    for filename in ("scouting-candidates.json", "source-mentions.json"):
        path = state_dir / filename
        data = read_json(path, default=[])
        if not isinstance(data, list):
            continue
        file_changed = apply_rows(data)
        if file_changed:
            write_json(path, data)
            changed += file_changed

    csv_path = state_dir / "editor-facing.csv"
    if csv_path.exists():
        history = store.read(Collection.CANDIDATE_HISTORY)
        history_by_identity = {_identity_key(record): record for record in history if isinstance(record, dict)}
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = list(reader.fieldnames or [])
            csv_rows = list(reader)
        csv_changed = 0
        for record in csv_rows:
            canonical = history_by_identity.get(str(record.get("Candidate ID", "")).strip())
            if canonical and str(canonical.get("russian_edition_status", "")).strip() in {
                FOUND_IN_RUSSIAN,
                "not found in Russian",
            }:
                record["Russian-edition status"] = str(canonical.get("russian_edition_status", ""))
                record["Russian-edition URL"] = str(canonical.get("russian_edition_url", ""))
            row = rows.get(str(record.get("Candidate ID", "")).strip())
            if row and row.get("outcome") == "matched" and row.get("evidence"):
                validation_record = {
                    "title": row.get("title", record.get("Title", "")),
                    "primary_authors": row.get("authors", []),
                }
                context = context_by_identity.get(str(record.get("Candidate ID", "")).strip(), {})
                if context.get("russian_search_notes"):
                    validation_record["russian_search_notes"] = context["russian_search_notes"]
                if context.get("search_translation"):
                    validation_record["search_translation"] = context["search_translation"]
                if not _is_verified_rsl_match(validation_record, row["evidence"][0]):
                    continue
                record["Russian-edition status"] = FOUND_IN_RUSSIAN
                record["Russian-edition URL"] = str(row["evidence"][0].get("url", ""))
                csv_changed += 1
        if csv_changed:
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(csv_rows)

    applied_identities = set()
    for identity_key, row in rows.items():
        if row.get("outcome") != "matched" or not row.get("evidence"):
            continue
        context = context_by_identity.get(identity_key, {})
        candidate = {"title": row.get("title", ""), "primary_authors": row.get("authors", [])}
        for field in ("russian_search_notes", "search_translation"):
            if context.get(field):
                candidate[field] = context[field]
        if _is_verified_rsl_match(candidate, row["evidence"][0]):
            applied_identities.add(identity_key)
    return {"changed_records": changed, "matched_rows": len(applied_identities)}
