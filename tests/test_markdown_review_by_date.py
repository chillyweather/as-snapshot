"""The Markdown review reads in the same date order as the page (#1057).

Product rule changed by #1057 (authorized by the operator in the issue on
2026-11-13): the rows follow the date each book was added, newest first, and
the «Статус обзора» column with its «Новые» / «Обновлённые» / «Ранее
просмотренные» values is replaced by «Добавлено» and «Обновлено». The editor
CSV keeps its order and its lifecycle columns.
"""

from __future__ import annotations

import csv
import io
import json
import re
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from state_store_support import sync_state

from autoscout.candidate_identity import candidate_id_for
from autoscout.review_rendering import current_review_order
from autoscout.review_rendering import render_review
from autoscout.review_row_resolution import RESOLVED
from autoscout.review_row_resolution import read_returned_rows
from autoscout.review_row_resolution import resolve_review_rows

RUN_DATE = "2026-11-13"


def _record(title: str, first_seen: str, **fields: Any) -> dict[str, Any]:
    identity_key = f"{title.lower()}|ann author"
    return {
        "identity_key": identity_key,
        "candidate_id": candidate_id_for(identity_key),
        "title": title,
        "primary_authors": ["Ann Author"],
        "priority_label": "C",
        "independent_signal_count": 1,
        "russian_edition_status": "not found in Russian",
        "visible_risk_status": "no visible risk",
        "first_seen_date": first_seen,
        "last_seen_date": first_seen,
        "content_description": "Книга о том, как устроена повседневная жизнь небольшого города.",
        "source_evidence": [
            {
                "source_name": "Active Prize List",
                "source_family": "media/curated list",
                "source_url": "https://example.test/active-prize-list",
                "evidence_note": "Listed by the source.",
            }
        ],
        **fields,
    }


def _render(tmp_path: Path) -> tuple[str, list[dict[str, str]]]:
    fresh = _record("Fresh", RUN_DATE, update_delta="new")
    changed = _record(
        "Changed", "2026-11-01", update_delta="upgraded", last_material_change_date=RUN_DATE, last_seen_date=RUN_DATE
    )
    history = [
        fresh,
        changed,
        _record("Oldest", "2026-10-01"),
        _record("Middle", "2026-11-05"),
        _record("Recent", "2026-11-10"),
    ]
    for name, value in (
        ("scouting-candidates.json", [fresh, changed]),
        ("candidate-history.json", history),
        ("run-summary.json", {"run_date": RUN_DATE, "editor_facing_count": 2}),
    ):
        (tmp_path / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    sync_state(tmp_path)
    with redirect_stdout(io.StringIO()):
        assert render_review(tmp_path, tmp_path, effective_date=RUN_DATE) == 0
    with (tmp_path / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
        csv_rows = list(csv.DictReader(handle))
    return (tmp_path / "weekly-review.md").read_text(encoding="utf-8"), csv_rows


def _table(markdown: str) -> list[dict[str, str]]:
    lines = [line for line in markdown.splitlines() if line.startswith("| ")]
    header = [cell.strip() for cell in lines[0].strip("|").split("|")]
    return [dict(zip(header, (cell.strip() for cell in line.strip("|").split("|")), strict=True)) for line in lines[1:]]


def test_rows_read_by_date_added_newest_first_with_page_wide_numbers(tmp_path: Path) -> None:
    markdown, csv_rows = _render(tmp_path)

    table = _table(markdown)
    assert [(row["Название"], row["Добавлено"]) for row in table] == [
        ("Fresh", "2026-11-13"),
        ("Recent", "2026-11-10"),
        ("Middle", "2026-11-05"),
        ("Changed", "2026-11-01"),
        ("Oldest", "2026-10-01"),
    ]
    # The row numbers are the page-wide ones, so the CSV order and the numbers stay as they were.
    numbers = {row["Название"]: row["#"] for row in table}
    assert [row["Title"] for row in csv_rows] == sorted(numbers, key=lambda title: int(numbers[title]))
    assert [row["Title"] for row in csv_rows] == ["Fresh", "Changed", "Recent", "Middle", "Oldest"]


def test_the_updated_column_holds_the_change_date_of_an_updated_book_only(tmp_path: Path) -> None:
    markdown, csv_rows = _render(tmp_path)

    updated = {row["Название"]: row["Обновлено"] for row in _table(markdown)}
    assert updated == {"Fresh": "", "Recent": "", "Middle": "", "Changed": RUN_DATE, "Oldest": ""}
    # The CSV keeps its lifecycle data.
    assert {row["Title"]: (row["Update delta"], row["Lifecycle"]) for row in csv_rows}["Changed"] == (
        "upgraded",
        "Updated",
    )


def test_the_markdown_has_no_lifecycle_vocabulary(tmp_path: Path) -> None:
    markdown, _csv_rows = _render(tmp_path)

    for text in ("Статус обзора", "Новые", "Обновлённые", "Ранее просмотренные", "ранее просмотренных"):
        assert text not in markdown, text
    assert (
        "**Всего в обзоре**: добавлено в последний запуск — 1, с отметкой «обновлено» — 1, скрытых редактором — 0."
        in markdown
    )
    assert "**Видимых книг**: 5." in markdown


def test_returned_markdown_rows_still_resolve(tmp_path: Path) -> None:
    _render(tmp_path)

    rows = read_returned_rows(tmp_path / "weekly-review.md")
    report = resolve_review_rows(rows, tmp_path)

    assert report["counts"][RESOLVED] == report["row_count"] == 5
    assert [row.title for row in rows] == ["Fresh", "Recent", "Middle", "Changed", "Oldest"]
    for row, result in zip(rows, report["results"], strict=True):
        assert result["matched_by"] == "short_durable_id", row.title
        assert result["candidate"]["title"] == row.title


def test_current_review_order_is_the_order_of_the_cards_on_the_page(tmp_path: Path) -> None:
    """The editorial-fit shadow ranks against this order, so it must be the one the editor sees (#1057)."""
    current = [
        _record("Alpha", RUN_DATE, update_delta="new", priority_label="C"),
        _record("Bravo", RUN_DATE, update_delta="new", priority_label="A"),
        _record("Charlie", "2026-11-01", update_delta="upgraded", priority_label="A", last_seen_date=RUN_DATE),
        _record("Delta", "2026-11-10", update_delta="unchanged", priority_label="B", last_seen_date=RUN_DATE),
    ]
    for name, value in (
        ("scouting-candidates.json", current),
        ("candidate-history.json", current),
        ("run-summary.json", {"run_date": RUN_DATE, "editor_facing_count": 4}),
    ):
        (tmp_path / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    sync_state(tmp_path)
    with redirect_stdout(io.StringIO()):
        assert render_review(tmp_path, tmp_path, effective_date=RUN_DATE) == 0

    order = current_review_order(current, current)

    by_key = {record["identity_key"]: record["title"] for record in current}
    # Newest date first; inside a date the page order (lifecycle, then priority).
    assert [by_key[key] for key in order] == ["Bravo", "Alpha", "Delta", "Charlie"]
    html = (tmp_path / "weekly-review.html").read_text(encoding="utf-8")
    # The timeline (the "all" tab); «Обновлённые» holds second copies of the changed books (#1063).
    timeline = html[html.index('id="tab-all"') : html.index('id="tab-updated"')]
    assert [by_key[key] for key in order] == re.findall(r'<span class="title">(.*?)</span>', timeline)
    # The CSV keeps the page-wide numbering order, which differs here.
    with (tmp_path / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
        assert [row["Title"] for row in csv.DictReader(handle)] == ["Bravo", "Alpha", "Charlie", "Delta"]


def test_the_updated_cell_tells_a_marked_book_from_an_unchanged_one(tmp_path: Path) -> None:
    """A marked book without a change date still says «обновлено», as the page mark does (#1057)."""
    current = [
        _record("Dated", "2026-11-01", update_delta="upgraded", last_material_change_date=RUN_DATE),
        _record("Undated", "2026-11-01", update_delta="changed"),
        _record("Steady", "2026-11-01", update_delta="unchanged"),
    ]
    for name, value in (
        ("scouting-candidates.json", current),
        ("candidate-history.json", current),
        ("run-summary.json", {"run_date": RUN_DATE, "editor_facing_count": 3}),
    ):
        (tmp_path / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    sync_state(tmp_path)
    with redirect_stdout(io.StringIO()):
        assert render_review(tmp_path, tmp_path, effective_date=RUN_DATE) == 0
    markdown = (tmp_path / "weekly-review.md").read_text(encoding="utf-8")
    html = (tmp_path / "weekly-review.html").read_text(encoding="utf-8")

    cells = {row["Название"]: row["Обновлено"] for row in _table(markdown)}
    assert cells == {"Dated": RUN_DATE, "Undated": "обновлено", "Steady": ""}
    marked = sum(1 for cell in cells.values() if cell)
    assert f"с отметкой «обновлено» — {marked}," in markdown
    timeline = html[html.index('id="tab-all"') : html.index('id="tab-updated"')]
    assert timeline.count('<span class="upd">') == marked
    assert '<span class="upd">⬆ обновлено</span>' in html


def test_the_updated_cell_and_totals_follow_the_fourteen_day_window(tmp_path: Path) -> None:
    """Product rule changed by #1063 (authorized by the operator on 2026-11-14): the mark lasts 14 days."""
    history = [
        _record("Inside", "2026-10-01", last_material_change_date="2026-10-31"),
        _record("Outside", "2026-10-01", last_material_change_date="2026-10-30"),
        _record("Never", "2026-10-01"),
    ]
    for name, value in (
        ("scouting-candidates.json", []),
        ("candidate-history.json", history),
        ("run-summary.json", {"run_date": RUN_DATE, "editor_facing_count": 0}),
    ):
        (tmp_path / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    sync_state(tmp_path)
    with redirect_stdout(io.StringIO()):
        assert render_review(tmp_path, tmp_path, effective_date=RUN_DATE) == 0
    markdown = (tmp_path / "weekly-review.md").read_text(encoding="utf-8")

    # RUN_DATE is 2026-11-13: 2026-10-31 is the oldest day inside the window, 2026-10-30 is outside.
    assert {row["Название"]: row["Обновлено"] for row in _table(markdown)} == {
        "Inside": "2026-10-31",
        "Outside": "",
        "Never": "",
    }
    assert "с отметкой «обновлено» — 1," in markdown
