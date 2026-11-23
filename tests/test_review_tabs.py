"""The review page splits its cards into tabs, so the list to read first stays short."""

from __future__ import annotations

import re

from autoscout.html_review import build_html_review
from autoscout.pm_publication import CITED_BEYOND_PM_DEALS_FIELD
from autoscout.pm_publication import is_rights_lead_only
from tests.test_html_review import _row
from tests.test_html_review import _summary

PM_URL = "https://www.publishersmarketplace.com/deals/"


def _deal(**overrides):
    entry = {
        "source_name": "Publishers Marketplace Deals",
        "signal_type": "rights/deal",
        "source_url": PM_URL,
        "rights_deal": {"publisher": "Example Press"},
    }
    entry.update(overrides)
    return entry


def _panel(html: str, tab: str) -> str:
    match = re.search(
        rf'<section class="tab-panel" id="tab-{tab}".*?(?=<section class="tab-panel"|<footer>)', html, re.S
    )
    return match.group(0) if match else ""


def _mixed_rows():
    """Product rule changed by #1063 (authorized by the operator on 2026-11-14): the tabs are all,
    undecided, updated and leads; the editor's decision alone splits the books.

    Product rule changed on 2026-11-19 (authorized by the operator): the "decided" tab is gone;
    nobody opens it, and a book with a decision stays in the "all" tab."""
    return [
        _row(idx=1, title="Fresh Book", update_delta="new", first_seen_date="2026-11-14"),
        _row(idx=2, title="Waiting Book", update_delta="unchanged", lifecycle_label="Previously seen"),
        _row(idx=3, title="Decided Book", lifecycle_label="Previously seen", editor_status="reject"),
        _row(idx=4, title="Deal Book", lifecycle_label="Previously seen", rights_lead_only=True),
        _row(
            idx=5,
            title="Decided Deal",
            lifecycle_label="Previously seen",
            rights_lead_only=True,
            editor_status="reject",
        ),
        _row(
            idx=6,
            title="Archive Book",
            lifecycle_label="Previously seen",
            archive_pending=True,
            archive_batch="pw-archive-2026",
            archive_batch_label="Архив PW 2026",
            archive_applied_date="2026-11-10",
        ),
        _row(idx=7, title="Decided New Book", update_delta="new", editor_status="interested"),
        _row(
            idx=8,
            title="Decided Updated Book",
            update_delta="upgraded",
            last_material_change_date="2026-11-12",
            editor_status="maybe_watch",
            first_seen_date="2026-09-01",
        ),
    ]


def _tabs(html: str) -> list[tuple[str, str, int]]:
    return [
        (key, label, int(count))
        for key, label, count in re.findall(
            r'<a class="tab[^"]*" href="#tab-[a-z]+" data-tab="([a-z]+)">([^<]+) <span class="n">(\d+)</span></a>', html
        )
    ]


def _cards(fragment: str) -> list[tuple[str, str]]:
    """(data-decided, title) of every card in *fragment*, in order."""
    return re.findall(
        r'<details class="cand[^"]*"[^>]*\bdata-decided="(yes|no)"[^>]*>.*?<span class="title">(.*?)</span>',
        fragment,
        re.S,
    )


def _titles(fragment: str) -> list[str]:
    return [title for _decided, title in _cards(fragment)]


def test_the_tabs_in_order_with_their_counts():
    html = build_html_review(_mixed_rows(), _summary(total_count=8, run_date="2026-11-14"))

    assert _tabs(html) == [
        ("all", "Все", 6),
        ("undecided", "Без решения", 3),
        ("updated", "Обновлённые", 1),
        ("leads", "Сделки и права", 2),
    ]
    assert "С решением" not in html
    assert 'data-tab="decided"' not in html


def test_books_are_written_once_in_the_timeline_with_their_decision():
    html = build_html_review(_mixed_rows(), _summary(total_count=8, run_date="2026-11-14"))

    timeline = _panel(html, "all")
    assert sorted(_cards(timeline), key=lambda card: card[1]) == [
        ("no", "Archive Book"),
        ("yes", "Decided Book"),
        ("yes", "Decided New Book"),
        ("yes", "Decided Updated Book"),
        ("no", "Fresh Book"),
        ("no", "Waiting Book"),
    ]
    # A new or updated book with a decision is a decided book like any other.
    assert dict((title, decided) for decided, title in _cards(timeline))["Decided New Book"] == "yes"
    # The filter tab has no panel and no copy of the cards.
    assert 'id="tab-undecided"' not in html
    # Leads are only in their own tab.
    assert _titles(_panel(html, "leads")) == ["Deal Book", "Decided Deal"]
    assert "Deal Book" not in timeline
    # Every card appears once on the page, plus one copy in «Обновлённые».
    assert html.count('<details class="cand') == 8 + 1
    assert _titles(_panel(html, "updated")) == ["Decided Updated Book"]


def test_a_review_with_one_kind_of_card_has_no_tab_bar():
    html = build_html_review([_row()], _summary())

    assert '<nav class="tabbar"' not in html
    assert 'class="tab-title"' not in html
    assert "Book One" in _panel(html, "all")


def test_the_filter_tabs_appear_only_when_both_kinds_of_book_exist():
    rows = [_row(idx=1, title="Undecided"), _row(idx=2, title="Lead", rights_lead_only=True)]

    html = build_html_review(rows, _summary(total_count=2))

    assert [key for key, _label, _count in _tabs(html)] == ["all", "leads"]
    assert "count--undecided" not in html.split("</style>", 1)[1]


def test_a_lead_names_its_buyer_under_the_title():
    html = build_html_review(
        [_row(rights_lead_only=True, publisher="Example Press"), _row(idx=2, title="Other")], _summary()
    )

    assert html.count('<span class="sub">Права купило: Example Press</span>') == 1


def test_long_source_cautions_fold_behind_their_count():
    lines = [f"Не удалось собрать: Источник {number} — не удалось собрать список." * 3 for number in range(4)]

    html = build_html_review([_row()], _summary(source_problem_lines=lines))

    assert '<details class="more-alerts"><summary>Предупреждений об источниках: 4</summary>' in html
    assert "Источник 3" in html


def test_a_short_source_caution_stays_in_view():
    html = build_html_review([_row()], _summary(source_problem_lines=["Пропущены: Источник — между сезонами."]))

    assert 'class="more-alerts"' not in html.split("</style>", 1)[1]
    assert "Пропущены: Источник — между сезонами." in html


def test_rights_lead_only_reads_deals_and_offerings():
    offering = {"source_name": "Publishers Marketplace Rights Offerings", "signal_type": "rights_offering"}
    review = {"source_name": "Kirkus Reviews", "signal_type": "media_list", "source_url": "https://example.test/"}

    assert is_rights_lead_only({"source_evidence": [_deal()]}) is True
    assert is_rights_lead_only({"source_evidence": [offering]}) is True
    assert is_rights_lead_only({"source_evidence": [_deal(), offering]}) is True
    assert is_rights_lead_only({"source_evidence": [_deal(), review]}) is False
    assert is_rights_lead_only({"source_evidence": []}) is False
    # Another source cited the book in an earlier week: it is a book to assess.
    assert is_rights_lead_only({"source_evidence": [_deal()], CITED_BEYOND_PM_DEALS_FIELD: True}) is False
