"""The review's «Обновлённые» tab and the 14-day updated mark (#1063).

Product rule changed by #1063 (authorized by the operator on 2026-11-14): a
book carries «⬆ обновлено» while its last material change is inside a 14-day
window counted back from the review date, in every tab, and «Обновлённые»
lists exactly those books under the date of the change.
"""

from __future__ import annotations

import re

from autoscout.html_review import build_html_review
from autoscout.review_dates import UPDATED_WINDOW_DAYS
from autoscout.review_dates import russian_days
from tests.test_html_review import _row
from tests.test_html_review import _summary
from tests.test_review_tabs import _panel

REVIEW = "2026-11-14"


def _rows():
    return [
        _row(idx=1, title="Inside Edge", first_seen_date="2026-06-01", last_material_change_date="2026-11-01"),
        _row(idx=2, title="Outside Edge", first_seen_date="2026-06-02", last_material_change_date="2026-10-31"),
        _row(idx=3, title="Fifteen Days", first_seen_date="2026-06-03", last_material_change_date="2026-10-30"),
        _row(
            idx=4,
            title="Today Decided",
            first_seen_date="2026-07-01",
            last_material_change_date=REVIEW,
            editor_status="interested",
        ),
        _row(idx=5, title="No Date", first_seen_date="2026-08-01", update_delta="upgraded"),
        _row(idx=6, title="Also Today", first_seen_date="2026-05-01", last_material_change_date=REVIEW),
        _row(idx=7, title="Plain", first_seen_date=REVIEW, update_delta="new"),
        _row(
            idx=8,
            title="Lead Changed",
            first_seen_date="2026-06-01",
            last_material_change_date=REVIEW,
            rights_lead_only=True,
        ),
    ]


def _sections(fragment: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """(heading text, [(card number, title)]) of each section in *fragment*."""
    result = []
    for section in re.findall(r'<section class="group".*?</section>', fragment, re.S):
        heading = re.sub(r"<[^>]+>", "", re.search(r"<h2>(.*?)</h2>", section).group(1))  # type: ignore[union-attr]
        cards = re.findall(
            r'<span class="num c-num">(\d+)</span><span class="title-cell"><span class="title">(.*?)</span>', section
        )
        result.append((heading, cards))
    return result


def _marked_titles(fragment: str) -> set[str]:
    return set(
        re.findall(r'<span class="title">([^<]*)</span>(?:<span class="sub">[^<]*</span>)?<span class="upd">', fragment)
    )


def test_the_updated_tab_lists_the_window_by_change_date_newest_first():
    html = build_html_review(_rows(), _summary(total_count=8, run_date=REVIEW))

    assert _sections(_panel(html, "updated")) == [
        ("14 ноября 2026 (2)", [("4", "Today Decided"), ("6", "Also Today")]),
        ("1 ноября 2026 (1)", [("1", "Inside Edge")]),
        ("Дата изменения неизвестна (1)", [("5", "No Date")]),
    ]
    assert '<a class="tab" href="#tab-updated" data-tab="updated">Обновлённые <span class="n">4</span></a>' in html


def test_every_card_of_the_updated_tab_carries_the_mark_in_the_timeline_with_the_same_number():
    html = build_html_review(_rows(), _summary(total_count=8, run_date=REVIEW))

    timeline = _panel(html, "all")
    updated = _panel(html, "updated")
    numbers = {title: number for _heading, cards in _sections(timeline) for number, title in cards}
    for _heading, cards in _sections(updated):
        for number, title in cards:
            assert numbers[title] == number, title
    assert _marked_titles(timeline) == {title for _h, cards in _sections(updated) for _n, title in cards}
    assert _marked_titles(updated) == _marked_titles(timeline)
    # A rights lead changed today carries the mark in its own tab but is no book of «Обновлённые».
    assert _marked_titles(_panel(html, "leads")) == {"Lead Changed"}


def test_a_book_changed_outside_the_window_has_no_mark_anywhere():
    html = build_html_review(
        _rows(), _summary(total_count=8, run_date=REVIEW, cumulative_counts={"visible_total_count": 8})
    )

    for title in ("Outside Edge", "Fifteen Days"):
        assert title not in _panel(html, "updated")
        assert title not in _marked_titles(html)
    assert "⬆ обновлено 31 октября" not in html
    assert "⬆ обновлено 30 октября" not in html
    # The totals count the same marks: four books and the lead.
    assert "с отметкой «обновлено» — 5," in html


def test_an_empty_updated_tab_is_not_shown():
    rows = [
        _row(idx=1, title="Old Change", last_material_change_date="2026-01-01"),
        _row(idx=2, title="Lead", rights_lead_only=True),
    ]

    html = build_html_review(rows, _summary(total_count=2, run_date=REVIEW))

    assert 'data-tab="updated"' not in html
    assert 'class="upd"' not in html


def test_the_archive_block_is_in_the_timeline_and_has_no_decided_cards():
    rows = [
        _row(
            idx=1,
            title="Waiting Archive",
            archive_pending=True,
            archive_batch="pw-archive-2026",
            archive_batch_label="Архив PW 2026",
            archive_applied_date="2026-11-11",
        ),
        _row(
            idx=2,
            title="Decided Archive",
            archive_batch="pw-archive-2026",
            archive_batch_label="Архив PW 2026",
            archive_applied_date="2026-11-11",
            editor_status="reject",
        ),
    ]

    html = build_html_review(rows, _summary(total_count=2, run_date=REVIEW))

    timeline = _panel(html, "all")
    assert _sections(timeline) == [
        ("11 ноября 2026 (1)(0)", [("2", "Decided Archive")]),
        ("Архив PW 2026 (1)(1)", [("1", "Waiting Archive")]),
    ]
    assert 'data-group="archive" data-batch="pw-archive-2026" data-n-undecided="1"' in timeline
    assert (
        "Книги добавлены из архива одним пакетом 2026-11-11. Без решения — 1 из 2 видимых книг архива. "
        "Книга с решением выходит из блока и стоит в списке под датой добавления пакета."
    ) in timeline


def test_the_filter_tabs_are_css_over_one_timeline():
    """The browser filter's contract: the script sets data-filter on the timeline panel, the CSS does the rest."""
    html = build_html_review(_rows(), _summary(total_count=8, run_date=REVIEW))
    style = html[html.index("<style>") : html.index("</style>")]
    script = html[html.index("<script>") :]

    for rule in (
        '.tab-panel[data-filter="undecided"] .cand[data-decided="yes"]',
        '.tab-panel[data-filter="undecided"] .group[data-n-undecided="0"]',
        '.tab-panel[data-filter="undecided"] .group h2 .count:not(.count--undecided) { display: none; }',
        '.tab-panel[data-filter="undecided"] .count--undecided { display: inline; }',
        # Without the script the filter tab is hidden and the full timeline shows.
        ".tab--filter, .count--undecided { display: none; }",
        ".js .tab--filter { display: inline-flex; }",
    ):
        assert rule in style, rule
    assert "var FILTERS = { undecided: 1 };" in script
    assert 'var key = FILTERS[name] ? "all" : name' in script
    assert 'if (panel.dataset.tab === "all") panel.dataset.filter = name;' in script
    # The filter tabs are links of the bar with no panel of their own; the timeline is not filtered in the markup.
    assert '<a class="tab tab--filter" href="#tab-undecided" data-tab="undecided">' in html
    assert "data-filter=" not in html[html.index("<body>") : html.index("<script>")]
    # The "decided" tab is gone (operator, 2026-11-19): no link, no note, no CSS for it.
    assert 'data-tab="decided"' not in html
    assert 'data-filter="decided"' not in style and "count--decided" not in html


def test_the_updated_tab_note_names_the_window():
    html = build_html_review(_rows(), _summary(total_count=8, run_date=REVIEW))

    note = (
        "Книги, изменившиеся за последние 14 дней, под датой изменения. "
        "Каждая из них остаётся и в общем списке под датой добавления."
    )
    # The literal fails when the window changes and the note does not follow.
    assert f"за последние {russian_days(UPDATED_WINDOW_DAYS)}," in note
    assert _panel(html, "updated").startswith(
        '<section class="tab-panel" id="tab-updated" data-tab="updated">'
        f'<h2 class="tab-title">Обновлённые <span class="count">(4)</span></h2><p class="tab-note">{note}</p>'
    )


def test_russian_days_takes_the_plural_form():
    assert [russian_days(n) for n in (1, 2, 5, 11, 14, 21, 22, 112)] == [
        "1 день",
        "2 дня",
        "5 дней",
        "11 дней",
        "14 дней",
        "21 день",
        "22 дня",
        "112 дней",
    ]
