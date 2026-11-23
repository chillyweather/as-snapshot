"""Mark the Candidate History records an earlier archive backfill added.

`./scripts/backfill-pw-history` writes the archive marker on the records it
adds. A batch applied before the marker existed carries none, and nothing on
its records tells them from the weekly PW pass. Git does: the batch is the set
of identity keys Candidate History holds after the commit that applied it and
did not hold before. This command reads the two versions from Git, previews
which current records it would mark, and with ``--apply`` writes the marker
and renders the review.

A record that was already in history and only gained evidence from the
archive is not part of the batch: it entered through a weekly run, on its own
date, and many such records already carry an editor decision. The preview
counts them so the choice stays visible.

Only the marker is written. Dates, evidence, descriptions, Candidate IDs,
visibility and editor decisions are left as they are.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from autoscout.archive_backfill import ARCHIVE_BACKFILL_FIELD
from autoscout.archive_backfill import archive_backfill
from autoscout.archive_backfill import archive_backfill_marker
from autoscout.git_envelope import resolve_repo_root
from autoscout.git_envelope import weekly_run_in_progress
from autoscout.review_rendering import render_review
from autoscout.state_store import Collection
from autoscout.state_store import StateStoreError
from autoscout.state_store import open_configured_state_store
from autoscout.state_store.layout import committed_candidate_history

# How many identity keys the report names for each kind of problem.
_NAMED_KEYS = 20


def _identity_key(record: Any) -> str:
    return str(record.get("identity_key", "")).strip() if isinstance(record, dict) else ""


def history_at(repo_root: Path, ref: str, state_dir: Path) -> list[dict[str, Any]]:
    """Candidate History as *ref* holds it, in either layout. Raises ``ValueError`` when Git cannot give a valid one."""
    relative = state_dir.resolve().relative_to(repo_root.resolve()).as_posix()
    try:
        history = committed_candidate_history(repo_root, ref, "" if relative == "." else relative)
    except StateStoreError as exc:
        raise ValueError("archive_marker_ref_history_invalid") from exc
    if history is None:
        raise ValueError("archive_marker_ref_unreadable")
    if not isinstance(history, list) or not all(isinstance(record, dict) for record in history):
        raise ValueError("archive_marker_ref_history_invalid")
    return history


def mark_archive_backfill(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    history: list[dict[str, Any]],
    *,
    batch: str,
    applied_date: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return *history* with the marker on the batch's records, and the report.

    The batch is every record of *after* whose identity key *before* does not
    hold. A current record is matched by identity key, or by durable Candidate
    ID when an identity correction has since renamed the key.
    """
    marker = archive_backfill_marker(batch, applied_date)
    before_by_key = {_identity_key(record): record for record in before}
    added = [record for record in after if _identity_key(record) and _identity_key(record) not in before_by_key]
    gained_evidence = sum(
        1
        for record in after
        if _identity_key(record) in before_by_key
        and record.get("source_evidence") != before_by_key[_identity_key(record)].get("source_evidence")
    )
    by_key = {_identity_key(record): index for index, record in enumerate(history)}
    by_candidate_id: dict[str, list[int]] = {}
    for index, record in enumerate(history):
        candidate_id = str(record.get("candidate_id", "")).strip()
        if candidate_id:
            by_candidate_id.setdefault(candidate_id, []).append(index)
    updated = [dict(record) for record in history]
    to_mark = already_marked = matched_by_candidate_id = 0
    missing: list[str] = []
    other_batch: list[str] = []
    for record in added:
        key = _identity_key(record)
        if key in by_key:
            index = by_key[key]
        else:
            same_id = by_candidate_id.get(str(record.get("candidate_id", "")).strip(), [])
            if len(same_id) != 1:
                missing.append(key)
                continue
            index = same_id[0]
            matched_by_candidate_id += 1
        existing = archive_backfill(updated[index])
        if existing == marker:
            already_marked += 1
        elif existing is not None:
            other_batch.append(key)
        else:
            updated[index][ARCHIVE_BACKFILL_FIELD] = dict(marker)
            to_mark += 1
    report = {
        "batch": marker["batch"],
        "applied_date": marker["applied_date"],
        "added_between_refs": len(added),
        "to_mark": to_mark,
        "already_marked": already_marked,
        "matched_by_candidate_id": matched_by_candidate_id,
        "missing_from_history": len(missing),
        "missing_identity_keys": sorted(missing)[:_NAMED_KEYS],
        "marked_for_another_batch": len(other_batch),
        "other_batch_identity_keys": sorted(other_batch)[:_NAMED_KEYS],
        "existing_records_with_changed_evidence_not_marked": gained_evidence,
    }
    return updated, report


def run(
    state_dir: Path,
    output_dir: Path,
    config_path: Path,
    *,
    before_ref: str,
    after_ref: str,
    batch: str,
    applied_date: str,
    apply: bool = False,
) -> dict[str, Any]:
    store = open_configured_state_store(state_dir, config_path)
    history = None if store is None else store.read(Collection.CANDIDATE_HISTORY)
    if store is None or not history:
        raise ValueError("archive_marker_history_missing")
    repo_root = resolve_repo_root(state_dir)
    if repo_root is None:
        raise ValueError("archive_marker_not_in_repository")
    if not isinstance(history, list) or not all(isinstance(record, dict) for record in history):
        raise ValueError("archive_marker_history_invalid")
    # Earlier versions are read from Git, where the State Store's JSON export
    # is committed, one file or one file per book; the current one is read
    # through the store above.
    updated, report = mark_archive_backfill(
        history_at(repo_root, before_ref, state_dir),
        history_at(repo_root, after_ref, state_dir),
        history,
        batch=batch,
        applied_date=applied_date,
    )
    if not apply:
        return {"status": "preview", **report}
    if weekly_run_in_progress(state_dir):
        return {"status": "blocked", "reason_code": "weekly_run_in_progress", **report}
    if report["to_mark"]:
        store.replace(Collection.CANDIDATE_HISTORY, updated)
    # Positional: the review applies this configuration's retired sources (#1025).
    rendered = render_review(output_dir, state_dir, None, config_path, state_store=store)
    return {"status": "applied" if rendered == 0 else "review_failed", "marked": report["to_mark"], **report}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Preview or apply the archive marker on records an earlier backfill added"
    )
    parser.add_argument("--before-ref", required=True, help="Git revision just before the backfill was applied")
    parser.add_argument("--after-ref", required=True, help="Git revision that holds the applied backfill")
    parser.add_argument("--batch", required=True, help="batch name, for example pw-archive-2026")
    parser.add_argument("--applied-date", required=True, help="the day the backfill was applied, YYYY-MM-DD")
    parser.add_argument("--config", type=Path, default=Path("configs/current-weekly-pilot-live.json"))
    parser.add_argument("--state-dir", type=Path, default=Path("runs/current-weekly-pilot-russian"))
    parser.add_argument("--output-dir", type=Path, default=Path("runs/current-weekly-pilot-russian"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        report = run(
            args.state_dir,
            args.output_dir,
            args.config,
            before_ref=args.before_ref,
            after_ref=args.after_ref,
            batch=args.batch,
            applied_date=args.applied_date,
            apply=args.apply,
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "failed", "reason_code": str(exc).split(":", 1)[0]}))
        return 2
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if report["status"] == "blocked":
        return 6
    return 0 if report["status"] in {"preview", "applied"} else 3


if __name__ == "__main__":
    raise SystemExit(main())
