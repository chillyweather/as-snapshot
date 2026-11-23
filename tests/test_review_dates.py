"""The order the review is read in: by the date each book was added, newest first (#1057)."""

from __future__ import annotations

from autoscout.review_dates import UPDATED_WINDOW_DAYS
from autoscout.review_dates import added_on_run_date_count
from autoscout.review_dates import date_sections
from autoscout.review_dates import is_marked_updated
from autoscout.review_dates import long_russian_date
from autoscout.review_dates import marked_updated_count
from autoscout.review_dates import reading_order
from autoscout.review_dates import review_date
from autoscout.review_dates import short_russian_date
from autoscout.review_dates import updated_cell_text
from autoscout.review_dates import updated_mark_text
from autoscout.review_dates import updated_sections


def _row(title: str, added: str = "2026-11-12", **fields):
    return {"title": title, "first_seen_date": added, "update_delta": "unchanged", **fields}


def _archive(title: str, applied: str, batch: str = "pw-archive-2026"):
    return _row(title, "2026-01-05", archive_pending=True, archive_batch=batch, archive_applied_date=applied)


def _layout(rows):
    return [
        (section.archive_batch or section.date, [row["title"] for row in section.rows])
        for section in date_sections(rows)
    ]


def test_dates_read_newest_first_with_page_order_inside_a_date():
    rows = [
        _row("Old A", "2026-11-10"),
        _row("New A", "2026-11-12"),
        _row("Old B", "2026-11-10"),
        _row("Middle", "2026-11-11"),
        _row("New B", "2026-11-12"),
    ]

    assert _layout(rows) == [
        ("2026-11-12", ["New A", "New B"]),
        ("2026-11-11", ["Middle"]),
        ("2026-11-10", ["Old A", "Old B"]),
    ]


def test_cards_with_no_date_come_last():
    rows = [_row("Undated", ""), _row("Old", "2026-01-01"), _row("New", "2026-11-12")]

    assert _layout(rows) == [("2026-11-12", ["New"]), ("2026-01-01", ["Old"]), ("", ["Undated"])]


def test_an_archive_block_is_one_section_after_the_books_of_its_day():
    rows = [
        _archive("Coast", "2026-11-11"),
        _row("Run book", "2026-11-11"),
        _row("Newer", "2026-11-12"),
        _archive("Harbour", "2026-11-11"),
        _row("Older", "2026-11-10"),
    ]

    assert _layout(rows) == [
        ("2026-11-12", ["Newer"]),
        ("2026-11-11", ["Run book"]),
        ("pw-archive-2026", ["Coast", "Harbour"]),
        ("2026-11-10", ["Older"]),
    ]


def test_a_decided_archive_book_joins_the_date_the_batch_came_in():
    decided = _row("Decided", "2026-01-05", archive_batch="pw-archive-2026", archive_applied_date="2026-11-11")

    assert _layout([decided]) == [("2026-11-11", ["Decided"])]


def test_an_updated_book_stays_under_the_date_it_was_added():
    updated = _row("Changed", "2026-10-01", update_delta="upgraded", last_material_change_date="2026-11-12")

    assert review_date(updated) == "2026-10-01"
    assert _layout([_row("Fresh", "2026-11-12"), updated]) == [
        ("2026-11-12", ["Fresh"]),
        ("2026-10-01", ["Changed"]),
    ]


def test_reading_order_flattens_the_sections():
    rows = [_row("Old", "2026-11-10"), _row("New", "2026-11-12"), _archive("Coast", "2026-11-11")]

    assert [row["title"] for row in reading_order(rows)] == ["New", "Coast", "Old"]


def test_the_mark_window_is_fourteen_days_including_the_review_date():
    """Product rule changed by #1063 (authorized by the operator on 2026-11-14): the mark follows a
    14-day window counted back from the review date, not the run that changed the book."""
    assert UPDATED_WINDOW_DAYS == 14
    review = "2026-11-14"
    # review - 13 days is the oldest day inside; review - 14 days is outside.
    assert is_marked_updated(_row("A", last_material_change_date="2026-11-01"), review)
    assert not is_marked_updated(_row("A", last_material_change_date="2026-10-31"), review)
    assert is_marked_updated(_row("A", last_material_change_date=review), review)
    # A book changed 15 days ago is not marked, whatever its lifecycle says.
    assert not is_marked_updated(_row("A", update_delta="upgraded", last_material_change_date="2026-10-30"), review)
    # A book of an earlier run is marked while its change is inside the window.
    assert is_marked_updated(
        _row("A", lifecycle_label="Previously seen", last_material_change_date="2026-11-05"), review
    )
    # Without a readable review date no change date is inside.
    assert not is_marked_updated(_row("A", last_material_change_date="2026-11-05"), "")


def test_without_a_change_date_the_lifecycle_decides_the_mark():
    assert is_marked_updated(_row("A", update_delta="upgraded"), "2026-11-14")
    assert is_marked_updated(_row("A", update_delta="changed"), "2026-11-14")
    assert is_marked_updated(_row("A", lifecycle_label="Updated"), "2026-11-14")
    assert not is_marked_updated(_row("A", update_delta="new"), "2026-11-14")
    assert not is_marked_updated(_row("A", update_delta="unchanged"), "2026-11-14")
    assert not is_marked_updated(_row("A", update_delta="upgraded", lifecycle_label="Previously seen"), "2026-11-14")


def test_the_mark_names_the_change_date_and_its_year_only_when_it_differs():
    row = _row("A", update_delta="changed", last_material_change_date="2026-11-12")

    assert updated_mark_text(row, "2026-11-13") == "⬆ обновлено 12 ноября"
    assert updated_mark_text(_row("A", last_material_change_date="2025-12-30"), "2026-01-02") == (
        "⬆ обновлено 30 декабря 2025"
    )
    assert updated_mark_text(_row("A", update_delta="changed"), "2026-11-13") == "⬆ обновлено"
    assert updated_mark_text(_row("A", last_material_change_date="2026-10-01"), "2026-11-13") == ""


def test_updated_sections_follow_the_change_date_newest_first():
    rows = [
        _row("Older change", "2026-01-01", last_material_change_date="2026-11-03"),
        _row("Undated", "2026-02-01", update_delta="upgraded"),
        _row("Outside", "2026-03-01", last_material_change_date="2026-10-01"),
        _row("Newest A", "2026-04-01", last_material_change_date="2026-11-13"),
        _row("Unmarked", "2026-05-01"),
        _row("Newest B", "2026-06-01", last_material_change_date="2026-11-13"),
    ]

    sections = updated_sections(rows, "2026-11-14")

    assert [(section.date, [row["title"] for row in section.rows]) for section in sections] == [
        ("2026-11-13", ["Newest A", "Newest B"]),
        ("2026-11-03", ["Older change"]),
        ("", ["Undated"]),
    ]


def test_russian_dates_use_the_genitive_month():
    assert long_russian_date("2026-10-05") == "5 октября 2026"
    assert long_russian_date("2026-03-31") == "31 марта 2026"
    assert short_russian_date("2026-05-01", "2026-11-13") == "1 мая"
    # Anything that is not an ISO date is shown as it is.
    assert long_russian_date("2026-13-01") == "2026-13-01"
    assert long_russian_date("soon") == "soon"


def test_the_counts_follow_the_run_date_and_the_marks():
    rows = [
        _row("Today", "2026-11-13"),
        _row("Today updated", "2026-11-13", update_delta="upgraded"),
        _row("Earlier updated", "2026-11-01", last_material_change_date="2026-11-02"),
        _row("Long ago updated", "2026-10-01", last_material_change_date="2026-10-02"),
        _archive("Coast", "2026-11-13"),
    ]

    assert added_on_run_date_count(rows, "2026-11-13") == 2
    assert added_on_run_date_count(rows, "") == 0
    assert marked_updated_count(rows, "2026-11-13") == 2


def test_the_markdown_cell_follows_the_same_mark():
    review = "2026-11-14"
    assert updated_cell_text(_row("A", last_material_change_date="2026-11-12"), review) == "2026-11-12"
    assert updated_cell_text(_row("A", update_delta="changed"), review) == "обновлено"
    assert updated_cell_text(_row("A", last_material_change_date="2026-10-30"), review) == ""


def test_a_batch_applied_over_two_days_keeps_its_first_day_whichever_books_still_wait():
    decided = _row("Decided", "2026-01-05", archive_batch="pw-archive-2026", archive_applied_date="2026-11-10")
    rows = [_row("Between", "2026-11-11"), _archive("Later", "2026-11-12"), decided]

    assert _layout(rows) == [
        ("2026-11-11", ["Between"]),
        ("2026-11-10", ["Decided"]),
        ("pw-archive-2026", ["Later"]),
    ]
    assert next(section.date for section in date_sections(rows) if section.archive_batch) == "2026-11-10"
