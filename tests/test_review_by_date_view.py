"""The review page lists the books under the date they were added, newest first (#1057).

Product rule changed by #1057 (authorized by the operator in the issue on
2026-11-13): with a run every night, the «Новые», «Обновлённые» and «Ранее
просмотренные» groups, the switch to a dates view and the flat table by
added date are gone. The dates layout is the only one, rendered on the server.
"""

from __future__ import annotations

import re

from autoscout.html_review import build_html_review
from tests.test_review_tabs import _panel


def _row(**overrides):
    row = {
        "idx": 1,
        "title": "Alpha",
        "authors": "Ann Author",
        "priority": "B",
        "signal_count": 1,
        "first_seen_date": "2026-10-05",
        "update_delta": "new",
    }
    row.update(overrides)
    return row


def _summary(**overrides):
    summary = {"delta_counts": {}, "total_count": 1, "run_date": "2026-10-05"}
    summary.update(overrides)
    return summary


def _sections(html: str) -> list[tuple[str, str, list[str]]]:
    """Each section of the page, or of one tab panel, as (heading text, count, titles in order)."""
    body = html[: html.index("<script>")] if "<script>" in html else html
    sections = []
    for section in re.findall(r'<section class="group".*?</section>', body, re.DOTALL):
        heading = re.search(r"<h2>(.*?)</h2>", section)
        assert heading is not None
        text = re.sub(r"<[^>]+>", "", heading.group(1))
        # A filtered timeline adds the counts without and with a decision after the first one.
        name, count = re.match(r"(.*?) \((\d+)\)", text).groups()  # type: ignore[union-attr]
        sections.append((name, count, re.findall(r'<span class="title">(.*?)</span>', section)))
    return sections


def _card(html: str, title: str) -> str:
    match = re.search(rf'<details class="cand[^"]*"[^>]*>(?:(?!</details>).)*?>{title}<.*?</details>', html, re.S)
    assert match is not None, title
    return match.group(0)


def test_run_date_is_machine_readable_for_the_last_run_badge():
    html = build_html_review([_row()], _summary(run_date="2026-10-05"))
    assert "<div class='run-date' data-run-date='2026-10-05'>Дата обзора: 2026-10-05</div>" in html


def test_sections_are_dates_newest_first_with_counts_and_page_order():
    rows = [
        _row(idx=1, title="Fresh One", first_seen_date="2026-10-05"),
        _row(idx=2, title="Older One", first_seen_date="2026-09-28", update_delta="unchanged"),
        _row(idx=3, title="Fresh Two", first_seen_date="2026-10-05"),
        _row(idx=4, title="Undated", first_seen_date="", update_delta="unchanged"),
        _row(idx=5, title="Middle", first_seen_date="2026-10-01", update_delta="unchanged"),
    ]

    sections = _sections(build_html_review(rows, _summary(total_count=5)))

    assert sections == [
        ("5 октября 2026 последний запуск", "2", ["Fresh One", "Fresh Two"]),
        ("1 октября 2026", "1", ["Middle"]),
        ("28 сентября 2026", "1", ["Older One"]),
        ("Дата неизвестна", "1", ["Undated"]),
    ]


def test_only_the_run_dates_section_carries_the_last_run_badge():
    rows = [_row(idx=1, first_seen_date="2026-10-05"), _row(idx=2, title="Beta", first_seen_date="2026-10-04")]

    html = build_html_review(rows, _summary(total_count=2))

    assert html.count('<span class="latest">последний запуск</span>') == 1
    assert '<h2>5 октября 2026 <span class="latest">последний запуск</span> <span class="count">(1)</span></h2>' in html
    assert '<section class="group" data-group="date" data-date="2026-10-05">' in html
    no_run = build_html_review(rows, _summary(total_count=2, run_date="2026-10-06"))
    assert "последний запуск" not in no_run


def test_card_numbers_stay_page_wide():
    """The editor cites row numbers in chat; a section does not renumber its cards."""
    rows = [
        _row(idx=1, title="Older", first_seen_date="2026-10-01", update_delta="unchanged"),
        _row(idx=2, title="Newer", first_seen_date="2026-10-05"),
    ]

    html = build_html_review(rows, _summary(total_count=2))

    assert '<span class="num c-num">2</span>' in _card(html, "Newer")
    assert '<span class="num c-num">1</span>' in _card(html, "Older")
    assert html.index("Newer") < html.index("Older")


def test_the_page_has_no_lifecycle_groups_no_switch_and_no_global_view():
    rows = [
        _row(idx=1, title="New Book"),
        _row(idx=2, title="Updated Book", update_delta="upgraded", last_material_change_date="2026-10-05"),
        _row(idx=3, title="Seen Book", update_delta="unchanged", lifecycle_label="Previously seen"),
    ]

    html = build_html_review(rows, _summary(total_count=3))

    for text in (
        "Новые",
        "новая",
        "🆕",
        "Ранее просмотренные",
        "Статус обзора",
        "По статусу",
        "По датам",
        "view-toggle",
        "global-added-view",
        "Все книги по дате добавления",
        "Вернуться к группам",
        "restore-groups-btn",
        "dates-view",
        "data-bucket",
    ):
        assert text not in html, text
    # «Обновлённые» is a tab since #1063, never a group of the timeline.
    assert [name for name, _count, _titles in _sections(_panel(html, "all"))] == ["5 октября 2026 последний запуск"]


def test_an_updated_book_stays_under_its_added_date_with_the_change_date_mark():
    rows = [
        _row(idx=1, title="Fresh", first_seen_date="2026-11-12"),
        _row(
            idx=2,
            title="Changed",
            first_seen_date="2026-10-01",
            update_delta="changed",
            last_material_change_date="2026-11-12",
        ),
        _row(idx=3, title="Steady", first_seen_date="2026-10-01", update_delta="unchanged"),
    ]

    html = build_html_review(rows, _summary(total_count=3, run_date="2026-11-12"))

    assert _sections(_panel(html, "all")) == [
        ("12 ноября 2026 последний запуск", "1", ["Fresh"]),
        ("1 октября 2026", "2", ["Changed", "Steady"]),
    ]
    changed = _card(html, "Changed")
    summary_row = changed[: changed.index("</summary>")]
    assert '<span class="upd">⬆ обновлено 12 ноября</span>' in summary_row
    # The detail panel keeps the change date.
    assert "<dt>Обновлено</dt><dd>2026-11-12</dd>" in changed
    for title in ("Fresh", "Steady"):
        assert 'class="upd"' not in _card(html, title), title


def test_the_mark_shows_the_year_of_an_earlier_year_and_stands_alone_without_a_date():
    rows = [
        _row(idx=1, title="Last Year", update_delta="upgraded", last_material_change_date="2025-12-30"),
        _row(idx=2, title="No Date", update_delta="upgraded"),
    ]

    html = build_html_review(rows, _summary(total_count=2, run_date="2026-01-02"))

    assert '<span class="upd">⬆ обновлено 30 декабря 2025</span>' in _card(html, "Last Year")
    assert '<span class="upd">⬆ обновлено</span>' in _card(html, "No Date")


def test_a_book_changed_outside_the_window_keeps_its_change_date_but_no_mark():
    row = _row(
        title="Seen", update_delta="upgraded", lifecycle_label="Previously seen", last_material_change_date="2026-09-01"
    )

    card = _card(build_html_review([row], _summary()), "Seen")

    assert 'class="upd"' not in card
    assert "<dt>Обновлено</dt><dd>2026-09-01</dd>" in card


def test_a_later_run_leaves_earlier_books_under_their_own_dates():
    """Successor of the PR #786 rule: a later run must not mark only its own books as new.

    The books of the run before stay together under their own date, and the
    only relative cue is the badge on the latest run's date.
    """
    first_run = [
        _row(idx=1, title="Monday A", first_seen_date="2026-11-09", update_delta="new"),
        _row(idx=2, title="Monday B", first_seen_date="2026-11-09", update_delta="new"),
    ]
    later_run = [
        _row(idx=1, title="Wednesday", first_seen_date="2026-11-11", update_delta="new"),
        _row(idx=2, title="Monday A", first_seen_date="2026-11-09", lifecycle_label="Previously seen"),
        _row(idx=3, title="Monday B", first_seen_date="2026-11-09", lifecycle_label="Previously seen"),
    ]

    before = _sections(build_html_review(first_run, _summary(total_count=2, run_date="2026-11-09")))
    after = _sections(build_html_review(later_run, _summary(total_count=3, run_date="2026-11-11")))

    assert before == [("9 ноября 2026 последний запуск", "2", ["Monday A", "Monday B"])]
    assert after == [
        ("11 ноября 2026 последний запуск", "1", ["Wednesday"]),
        ("9 ноября 2026", "2", ["Monday A", "Monday B"]),
    ]


def test_the_timeline_lists_decided_and_undecided_books_together_by_date():
    """Product rule changed by #1063 (authorized by the operator on 2026-11-14): one timeline of every
    book; the decision only filters it, so a decided book keeps its place among the dates."""
    seen = {"lifecycle_label": "Previously seen"}
    rows = [
        _row(idx=1, title="Older Waiting", first_seen_date="2026-08-01", **seen),
        _row(idx=2, title="Decided Old", first_seen_date="2026-09-01", editor_status="reject", **seen),
        _row(idx=3, title="Fresh", first_seen_date="2026-10-05"),
        _row(idx=4, title="Decided Older", first_seen_date="2026-08-01", editor_status="reject", **seen),
    ]

    html = build_html_review(rows, _summary(total_count=4))

    assert _sections(_panel(html, "all")) == [
        ("5 октября 2026 последний запуск", "1", ["Fresh"]),
        ("1 сентября 2026", "1", ["Decided Old"]),
        ("1 августа 2026", "2", ["Older Waiting", "Decided Older"]),
    ]
    # Each section carries its count without a decision for the undecided tab.
    assert '<section class="group" data-group="date" data-date="2026-08-01" data-n-undecided="1">' in html
    assert '<section class="group" data-group="date" data-date="2026-09-01" data-n-undecided="0">' in html
    assert (
        '<h2>1 августа 2026 <span class="count">(2)</span><span class="count count--undecided">(1)</span></h2>'
    ) in html


def test_the_added_column_is_a_plain_label_and_the_others_still_sort():
    html = build_html_review([_row()], _summary())

    assert '<span class="plain" data-key="added">Добавлено</span>' in html
    for key in ("num", "title", "author", "priority", "signal", "source", "editor"):
        assert f'<span class="sortable" data-key="{key}"' in html, key
    script = html[html.index("<script>") :]
    assert "sortPerGroup([{ key: key, type: type, dir: dir }]);" in script


def test_static_markup_needs_no_script_for_the_sections():
    html = build_html_review([_row()], _summary())
    body = html[html.index("<body>") : html.index("<script>")]

    assert '<section class="group" data-group="date" data-date="2026-10-05">' in body
