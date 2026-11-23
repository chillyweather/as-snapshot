"""Tests for issue #457 description provenance warnings."""

from __future__ import annotations

import csv
from typing import Any

from autoscout.html_review import build_html_review
from autoscout.review_rendering import assemble_cumulative_review
from autoscout.weekly_report_row import write_editor_csv

WARNING = "⚠ Описание не подтверждено источниками"
SUMMARY_WARNING = "⚠ Описание без источников"
DESCRIPTION = "Описание книги с подтверждённым содержанием."


def _candidate(*, description: str, provenance: str, identity_key: str = "book one|alex example") -> dict[str, Any]:
    return {
        "identity_key": identity_key,
        "title": "Book One",
        "primary_authors": ["Alex Example"],
        "content_description": description,
        "description_provenance": {"provenance": provenance},
        "editor_status": "interested",
        "editor_note": "Keep this note.",
        "update_delta": "new",
        "priority_label": "A",
        "independent_signal_count": 2,
        "russian_edition_status": "not found in Russian",
        "source_evidence": [],
    }


def _render_row(candidate: dict[str, Any]) -> dict[str, Any]:
    rows, _summary = assemble_cumulative_review([candidate], [], [])
    return rows[0]


def _csv_row(tmp_path, row: dict[str, Any]) -> dict[str, str]:
    path = tmp_path / "editor-facing.csv"
    write_editor_csv(path, [row["editor_row"]])
    with path.open(encoding="utf-8", newline="") as handle:
        return next(csv.DictReader(handle))


def test_codex_generated_description_is_marked_in_csv(tmp_path):
    row = _render_row(_candidate(description=DESCRIPTION, provenance="codex-generated"))

    rendered = _csv_row(tmp_path, row)

    assert rendered["Description status"] == WARNING
    assert rendered["Description"] == DESCRIPTION


def test_codex_generated_description_is_marked_in_html():
    row = _render_row(_candidate(description=DESCRIPTION, provenance="codex-generated"))

    html = build_html_review(
        [row],
        {
            "delta_counts": {"new": 1, "upgraded": 0, "changed": 0, "unchanged": 0},
            "hidden_by_rule_count": 0,
            "total_count": 1,
        },
    )

    assert html.count(WARNING) == 1
    assert f"<dt>Статус описания</dt><dd>{WARNING}</dd>" in html
    assert SUMMARY_WARNING not in html
    assert "description-warning-badge" not in html
    assert "description-detail" not in html
    assert ".title-cell" in html
    assert "flex-direction: column" in html
    assert '<dt>Описание</dt><dd><span class="note">' in html
    assert "Book One" in html
    assert DESCRIPTION in html


def test_backfilled_description_is_not_marked(tmp_path):
    row = _render_row(_candidate(description=DESCRIPTION, provenance="backfilled"))

    rendered = _csv_row(tmp_path, row)

    assert rendered["Description status"] == ""
    assert WARNING not in build_html_review([row], {"total_count": 1})


def test_empty_description_is_not_marked_even_with_codex_provenance(tmp_path):
    row = _render_row(_candidate(description="", provenance="codex-generated"))

    rendered = _csv_row(tmp_path, row)

    assert rendered["Description status"] == ""
    assert WARNING not in build_html_review([row], {"total_count": 1})


def test_marking_preserves_editor_facing_fields(tmp_path):
    row = _render_row(
        _candidate(
            description=DESCRIPTION,
            provenance="codex-generated",
            identity_key="book one|alex example",
        )
    )

    rendered = _csv_row(tmp_path, row)

    assert rendered["Description"] == DESCRIPTION
    assert rendered["Candidate ID"] == "book one|alex example"
    assert rendered["Title"] == "Book One"
    assert rendered["Author"] == "Alex Example"
    assert rendered["Editor status"] == "interested"
    assert rendered["Editor note"] == "Keep this note."
    assert rendered["Description status"] == WARNING
