"""Add locally staged PW books to Candidate History without a weekly run.

The preview is read-only. Apply imports candidates with original descriptions
and leaves undecided identities, weekly snapshots and the PW seen index
untouched. Subscriber review prose never enters this module.
"""

from __future__ import annotations

import argparse
import json
import re
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

from autoscout.archive_backfill import ARCHIVE_BACKFILL_FIELD
from autoscout.archive_backfill import archive_backfill_marker
from autoscout.archive_backfill import pw_archive_batch
from autoscout.candidate_identity import assign_alias
from autoscout.candidate_identity import ensure_alias_table
from autoscout.config import resolve_weekly_portfolio
from autoscout.content_description import PW_LOCAL_ENGLISH_SOURCE
from autoscout.content_description import description_provenance_record
from autoscout.content_description import not_yet_published_date
from autoscout.content_description import record_has_usable_description
from autoscout.content_description import validate_english_description
from autoscout.description_depth import meets_description_depth
from autoscout.json_io import read_json
from autoscout.pipeline import build_scouting_candidates
from autoscout.pipeline import update_candidate_history_snapshots
from autoscout.pw_issue_capture import EVIDENCE_URL_PREFIX
from autoscout.pw_issue_capture import mention
from autoscout.pw_issue_capture import read_descriptions
from autoscout.pw_issue_capture import validate_collection
from autoscout.pw_local_import import _load_collection
from autoscout.review_rendering import render_review
from autoscout.scouting_candidates import ScoutingCandidate
from autoscout.source_mentions import SourceMention
from autoscout.state import read_run_state
from autoscout.state_store import Collection
from autoscout.state_store import open_configured_state_store


def _pw_source(config_path: Path) -> dict[str, Any]:
    raw = read_json(config_path)
    if not isinstance(raw, dict):
        raise ValueError("pw_history_config_invalid")
    config = resolve_weekly_portfolio(raw, config_path)
    matches = [
        source
        for source in config.get("sources", [])
        if isinstance(source, dict) and source.get("contract_family") == "pw-issue-pdf"
    ]
    if len(matches) != 1:
        raise ValueError("pw_history_source_missing")
    return matches[0]


def _local_descriptions(path: Path, staged: dict[str, Any]) -> tuple[str, dict[str, dict[str, str]]]:
    payload = read_json(path, None)
    if not isinstance(payload, dict) or payload.get("language", "ru") == "ru":
        return "ru", read_descriptions(path, staged)
    if (
        payload.get("language") != "en"
        or payload.get("schema_version") != 1
        or payload.get("year") != staged["year"]
        or not isinstance(payload.get("descriptions"), dict)
    ):
        raise ValueError("pw_english_description_state_invalid")
    rows = {row["isbn13"]: row for row in validate_collection(staged)}
    accepted: dict[str, dict[str, str]] = {}
    for isbn, item in payload["descriptions"].items():
        if isbn not in rows:
            continue
        row = rows[isbn]
        if (
            not isinstance(item, dict)
            or item.get("isbn13") != isbn
            or any(item.get(key) != row[key] for key in ("title", "primary_authors", "issue_date"))
            or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("review_sha256", "")))
        ):
            raise ValueError("pw_english_description_state_invalid")
        description, _reason = validate_english_description(item.get("english_description"))
        if description is None or not meets_description_depth(description):
            raise ValueError("pw_english_description_state_invalid")
        accepted[isbn] = {"english_description": description}
    return "en", accepted


def _description_provenance(
    key: str, description: str, isbn: str, applied_date: str, candidate_id: str, language: str
) -> dict[str, Any]:
    evidence_url = EVIDENCE_URL_PREFIX + isbn
    provenance = description_provenance_record(
        key,
        description,
        "grounded",
        evidence_basis=[evidence_url],
        grounding_evidence_url=evidence_url,
        applied_date=applied_date,
        candidate_id=candidate_id or None,
    )
    if language == "en":
        provenance.update({"language": "en", "source": PW_LOCAL_ENGLISH_SOURCE})
    return provenance


def backfill_pw_history(
    staged_file: Path,
    descriptions_file: Path,
    config_path: Path,
    state_dir: Path,
    output_dir: Path,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    # The State Store cannot tell a missing Candidate History from an empty
    # one, so a missing state directory is what refuses the backfill.
    store = open_configured_state_store(state_dir, config_path)
    if store is None or not state_dir.is_dir():
        raise ValueError("pw_history_missing")
    staged = _load_collection(staged_file, staged=True)
    rows = validate_collection(staged)
    source = _pw_source(config_path)
    exclusions = set(source.get("pw_excluded_subsections", []))
    language, descriptions = _local_descriptions(descriptions_file, staged)
    state = read_run_state(store)
    history = state["candidate_history"]
    excluded_history = state["excluded_candidate_history"]
    if not isinstance(history, list) or not all(isinstance(item, dict) for item in history):
        raise ValueError("pw_history_invalid")
    if not isinstance(excluded_history, list):
        raise ValueError("pw_excluded_history_invalid")

    def candidate_description(data: dict[str, Any]) -> str:
        if language == "ru":
            return str(data.get("content_description", "")).strip()
        return str(descriptions.get(str(data.get("isbn", "")), {}).get("english_description", "")).strip()

    eligible_rows = [row for row in rows if row["subsection"] not in exclusions]
    source_mentions = [
        SourceMention.from_collector(
            source,
            mention(row, source, row["issue_date"], descriptions.get(row["isbn13"]) if language == "ru" else None),
            row["issue_date"],
        ).to_dict()
        for row in eligible_rows
    ]
    aliases = ensure_alias_table(deepcopy(state["candidate_identity_aliases"]))
    candidates, excluded = build_scouting_candidates(
        source_mentions,
        date.today().isoformat(),
        excluded_candidate_history=excluded_history,
        candidate_history=history,
        candidate_identity_aliases=aliases,
    )
    history_by_key = {str(record.get("identity_key", "")): record for record in history}
    history_by_isbn = {str(record["isbn"]): record for record in history if record.get("isbn")}
    new_candidates = []
    existing_description_updates: dict[str, tuple[str, str]] = {}
    existing_evidence_updates: dict[str, list[dict[str, Any]]] = {}
    already_present = 0
    isbn_conflicts: list[dict[str, str]] = []
    for candidate in candidates:
        data = ScoutingCandidate.from_mapping(candidate).data
        if data["identity_key"] in history_by_key:
            already_present += 1
            existing = history_by_key[str(data["identity_key"])]
            old_evidence = existing.get("source_evidence", [])
            if not isinstance(old_evidence, list):
                raise ValueError("pw_history_evidence_invalid")
            known_evidence = {
                (str(item.get("evidence_url", "")), str(item.get("source_week", "")))
                for item in old_evidence
                if isinstance(item, dict)
            }
            fresh_evidence = [
                item
                for item in data.get("source_evidence", [])
                if isinstance(item, dict)
                and (str(item.get("evidence_url", "")), str(item.get("source_week", ""))) not in known_evidence
            ]
            if fresh_evidence:
                existing_evidence_updates[str(data["identity_key"])] = fresh_evidence
            provenance = existing.get("description_provenance")
            provenance_name = (
                str(provenance.get("provenance", "")) if isinstance(provenance, dict) else str(provenance or "")
            )
            description = candidate_description(data)
            if (
                description
                and not record_has_usable_description(existing)
                and provenance_name != "editor-corrected"
                and not_yet_published_date(existing) is None
            ):
                existing_description_updates[str(data["identity_key"])] = (description, str(data.get("isbn", "")))
        elif data.get("isbn") in history_by_isbn:
            existing = history_by_isbn[str(data["isbn"])]
            isbn_conflicts.append(
                {
                    "isbn": str(data["isbn"]),
                    "staged_title": str(data.get("title", "")),
                    "staged_identity_key": str(data["identity_key"]),
                    "history_title": str(existing.get("title", "")),
                    "history_identity_key": str(existing.get("identity_key", "")),
                    "history_isbn_discovery_only": str(bool(existing.get("isbn_discovery_only", False))).lower(),
                }
            )
        else:
            new_candidates.append(candidate)
    ready_candidates = [
        candidate
        for candidate in new_candidates
        if candidate_description(ScoutingCandidate.from_mapping(candidate).data)
    ]
    without_descriptions = len(new_candidates) - len(ready_candidates)
    report: dict[str, Any] = {
        "status": "preview",
        "staged_issues": len(staged["issues"]),
        "staged_rows": len(rows),
        "excluded_subsection_rows": len(rows) - len(eligible_rows),
        "pipeline_exclusions": len(excluded),
        "already_in_history": already_present,
        "deferred_isbn_identity_conflicts": len(isbn_conflicts),
        "isbn_identity_conflict_details": isbn_conflicts,
        "new_candidates": len(new_candidates),
        "new_candidates_ready": len(ready_candidates),
        "new_candidates_without_descriptions": without_descriptions,
        "existing_descriptions_ready": len(existing_description_updates),
        "existing_evidence_ready": len(existing_evidence_updates),
    }
    if not apply:
        return report
    if not ready_candidates and not existing_description_updates:
        return {**report, "status": "blocked", "reason_code": "pw_nothing_ready"}
    applied_date = date.today().isoformat()
    snapshots = update_candidate_history_snapshots([], ready_candidates, applied_date)
    # The dates below are the PW issue's, so the batch and the day it was
    # applied are kept apart: the review shows the batch in one place.
    marker = archive_backfill_marker(pw_archive_batch(staged["year"]), applied_date)
    for snapshot in snapshots:
        snapshot[ARCHIVE_BACKFILL_FIELD] = dict(marker)
        weeks = [
            str(evidence.get("source_week", ""))
            for evidence in snapshot.get("source_evidence", [])
            if isinstance(evidence, dict) and evidence.get("source_week")
        ]
        if not weeks:
            raise ValueError("pw_history_issue_date_missing")
        snapshot["first_seen_date"] = min(weeks)
        snapshot["last_seen_date"] = max(weeks)
        if language == "en":
            isbn = str(snapshot.get("isbn", ""))
            description = descriptions[isbn]["english_description"]
            snapshot["content_description"] = description
            snapshot["description_provenance"] = _description_provenance(
                str(snapshot["identity_key"]),
                description,
                isbn,
                applied_date,
                str(snapshot.get("candidate_id", "")),
                language,
            )
    persisted_aliases = ensure_alias_table(deepcopy(state["candidate_identity_aliases"]))
    for snapshot in snapshots:
        key, candidate_id = str(snapshot["identity_key"]), str(snapshot["candidate_id"])
        if assign_alias(persisted_aliases, key, candidate_id):
            raise ValueError("pw_history_alias_conflict")
        metadata = persisted_aliases["candidates"].setdefault(candidate_id, {"candidate_id": candidate_id})
        metadata.setdefault("canonical_identity_key", key)
        metadata.setdefault("title", snapshot["title"])
        metadata.setdefault("primary_authors", snapshot["primary_authors"])
    updated_history = deepcopy(history)
    for record in updated_history:
        key = str(record.get("identity_key", ""))
        if key in existing_evidence_updates:
            record["source_evidence"] = [
                *record.get("source_evidence", []),
                *deepcopy(existing_evidence_updates[key]),
            ]
        if key not in existing_description_updates:
            continue
        description, isbn = existing_description_updates[key]
        record["content_description"] = description
        record["description_provenance"] = _description_provenance(
            key,
            description,
            isbn,
            applied_date,
            str(record.get("candidate_id", "")),
            language,
        )
    state_dir.mkdir(parents=True, exist_ok=True)
    with store.transaction():
        store.replace(
            Collection.CANDIDATE_HISTORY,
            sorted([*updated_history, *snapshots], key=lambda x: x["identity_key"]),
        )
        if snapshots:
            store.replace(Collection.CANDIDATE_IDENTITY_ALIASES, persisted_aliases)
    # Positional: the review applies this configuration's retired sources (#1025).
    rendered = render_review(output_dir, state_dir, None, config_path, state_store=store)
    return {
        **report,
        "status": "applied" if rendered == 0 else "review_failed",
        "added": len(snapshots),
        "existing_descriptions_updated": len(existing_description_updates),
        "existing_evidence_updated": len(existing_evidence_updates),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview or apply an offline PW Candidate History backfill")
    parser.add_argument("--staged", type=Path, required=True)
    parser.add_argument("--descriptions", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("configs/current-weekly-pilot-live.json"))
    parser.add_argument("--state-dir", type=Path, default=Path("runs/current-weekly-pilot-russian"))
    parser.add_argument("--output-dir", type=Path, default=Path("runs/current-weekly-pilot-russian"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        report = backfill_pw_history(
            args.staged, args.descriptions, args.config, args.state_dir, args.output_dir, apply=args.apply
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "failed", "reason_code": str(exc).split(":", 1)[0]}))
        return 2
    print(json.dumps(report, indent=2))
    return 0 if report["status"] in {"preview", "applied"} else 3


if __name__ == "__main__":
    raise SystemExit(main())
