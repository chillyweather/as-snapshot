"""Preview or apply a bounded import of locally staged PW issues.

No network, model, weekly state, or Git operation occurs here. Existing issue
dates are never rewritten; the known October 2026 discrepancy stays visible.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.pw_issue_capture import validate_collection

ROW_FIELDS = frozenset(
    {
        "title",
        "primary_authors",
        "publisher",
        "price",
        "format",
        "page_count",
        "starred",
        "isbn13",
        "subsection",
        "agent",
        "agency",
        "publication_month",
        "issue_date",
    }
)


def _load_collection(path: Path, *, staged: bool) -> dict[str, Any]:
    data = read_json(path, None)
    if data is None and not staged:
        return {
            "schema_version": 1,
            "rows": [],
            "issues": [],
            "skipped_issues": [],
            "retry_issues": [],
            "untried_issues": [],
        }
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("pw_import_collection_invalid")
    validate_collection(data)
    if staged:
        if not isinstance(data.get("year"), int):
            raise ValueError("pw_import_stage_invalid")
        for row in data["rows"]:
            if set(row) != ROW_FIELDS:
                raise ValueError("pw_import_stage_contains_unexpected_fields")
    return data


def import_staged_issues(
    staged_file: Path, collection_file: Path, *, max_new_issues: int = 4, apply: bool = False
) -> dict[str, Any]:
    if not 1 <= max_new_issues <= 10 or staged_file.resolve() == collection_file.resolve():
        raise ValueError("pw_import_input_invalid")
    staged = _load_collection(staged_file, staged=True)
    current = _load_collection(collection_file, staged=False)
    stage_dates = [str(issue["issue_date"]) for issue in staged["issues"]]
    if len(stage_dates) != len(set(stage_dates)):
        raise ValueError("pw_import_duplicate_issue_date")
    for day in stage_dates:
        if date.fromisoformat(day).year != staged["year"]:
            raise ValueError("pw_import_stage_date_invalid")
    if any(row["issue_date"] not in stage_dates for row in staged["rows"]):
        raise ValueError("pw_import_stage_row_orphaned")
    current_dates = {str(issue["issue_date"]) for issue in current["issues"]}
    conflicts = []
    for day in sorted(set(stage_dates) & current_dates):
        stage_isbns = {row["isbn13"] for row in staged["rows"] if row["issue_date"] == day}
        current_isbns = {row["isbn13"] for row in current["rows"] if row["issue_date"] == day}
        if stage_isbns != current_isbns:
            conflicts.append(
                {
                    "issue_date": day,
                    "staged_only": len(stage_isbns - current_isbns),
                    "existing_only": len(current_isbns - stage_isbns),
                }
            )
    selected = sorted(set(stage_dates) - current_dates)[:max_new_issues]
    new_rows = [row for row in staged["rows"] if row["issue_date"] in selected]
    report = {
        "status": "applied" if apply else "preview",
        "issue_dates": selected,
        "issues_to_add": len(selected),
        "rows_in_selected_issues": len(new_rows),
        "existing_issue_conflicts": conflicts,
        "remaining_unimported_issues": len(set(stage_dates) - current_dates) - len(selected),
    }
    if not apply or not selected:
        return report
    rows = {row["isbn13"]: row for row in current["rows"]}
    for row in new_rows:
        existing = rows.get(row["isbn13"])
        if existing is None or existing["issue_date"] <= row["issue_date"]:
            rows[row["isbn13"]] = row
    current["rows"] = sorted(rows.values(), key=lambda row: row["isbn13"])
    current["issues"] = sorted(
        [*current["issues"], *({"issue_date": day, "issue_id": "local-pdf"} for day in selected)],
        key=lambda issue: issue["issue_date"],
    )
    for key in ("retry_issues", "untried_issues"):
        current[key] = [day for day in current.get(key, []) if day not in selected]
    current["skipped_issues"] = [
        entry for entry in current.get("skipped_issues", []) if entry.get("issue_date") not in selected
    ]
    collection_file.parent.mkdir(parents=True, exist_ok=True)
    write_json(collection_file, current)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview or apply a bounded local PW issue import")
    parser.add_argument("--staged", type=Path, required=True)
    parser.add_argument("--collection", type=Path, required=True)
    parser.add_argument("--max-new-issues", type=int, default=4)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        report = import_staged_issues(
            args.staged, args.collection, max_new_issues=args.max_new_issues, apply=args.apply
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        print(json.dumps({"status": "failed", "reason_code": "pw_import_invalid"}))
        return 2
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
