"""Offline PW import is bounded, previewable, and metadata only."""

import json
from pathlib import Path

import pytest

from autoscout.pw_local_import import import_staged_issues

ROW = {
    "title": "An Invented Coast",
    "primary_authors": ["Ada Example"],
    "publisher": "Imaginary Press",
    "price": "$28",
    "format": "hardcover",
    "page_count": 240,
    "starred": False,
    "isbn13": "9780306406157",
    "subsection": "History",
    "agent": "",
    "agency": "",
    "publication_month": "Nov.",
    "issue_date": "2026-01-05",
}


def _write(path: Path, payload: dict):
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_preview_does_not_write_and_apply_adds_only_selected_issue(tmp_path: Path):
    staged = tmp_path / "staged.json"
    current = tmp_path / "current.json"
    second = {**ROW, "isbn13": "9781402894626", "issue_date": "2026-01-12"}
    _write(
        staged,
        {
            "schema_version": 1,
            "year": 2026,
            "rows": [ROW, second],
            "issues": [{"issue_date": "2026-01-05"}, {"issue_date": "2026-01-12"}],
        },
    )
    preview = import_staged_issues(staged, current, max_new_issues=1)
    assert not current.exists()
    assert preview["issue_dates"] == ["2026-01-05"]
    assert preview["remaining_unimported_issues"] == 1

    applied = import_staged_issues(staged, current, max_new_issues=1, apply=True)
    assert applied["status"] == "applied"
    data = json.loads(current.read_text())
    assert [issue["issue_date"] for issue in data["issues"]] == ["2026-01-05"]
    assert [row["isbn13"] for row in data["rows"]] == [ROW["isbn13"]]
    assert import_staged_issues(staged, current, max_new_issues=1)["issue_dates"] == ["2026-01-12"]


def test_import_refuses_any_review_text_field(tmp_path: Path):
    staged = tmp_path / "staged.json"
    _write(
        staged,
        {
            "schema_version": 1,
            "year": 2026,
            "rows": [{**ROW, "review": "Invented text"}],
            "issues": [{"issue_date": "2026-01-05"}],
        },
    )
    with pytest.raises(ValueError, match="unexpected_fields"):
        import_staged_issues(staged, tmp_path / "current.json")
