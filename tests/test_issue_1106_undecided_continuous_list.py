"""The undecided tab as one continuous list ordered by the fit score (issue #1106).

The timeline is written once, under its dates. With the order by fit score
on, the page also carries one empty table and a script that moves the
undecided cards into it when the undecided tab opens. The script's behaviour
is checked in a headless browser when one is installed and can start.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from autoscout.html_review import FLAT_UNDECIDED_HEADING
from autoscout.html_review import build_html_review

RUN_DATE = "2026-11-12"
OLDER = "2026-11-10"
OLDEST = "2026-11-01"


def _row(idx: int, title: str, added: str, fit: float | None, status: str = "", **fields: Any) -> dict[str, Any]:
    return {
        "idx": idx,
        "title": title,
        "authors": f"Author {idx}",
        "priority": "C",
        "signal_count": 1,
        "first_seen_date": added,
        "update_delta": "unchanged",
        "editor_status": status,
        "fit_score": fit,
        **fields,
    }


def _rows() -> list[dict[str, Any]]:
    """Six books over three dates, numbered in the order the timeline lists them inside a date."""
    return [
        _row(1, "Newer Mid", RUN_DATE, 2.0),
        _row(2, "Newer Decided", RUN_DATE, 1.5, "reject"),
        _row(3, "Newer Unscored", RUN_DATE, None),
        _row(4, "Older Top", OLDER, 3.5),
        _row(5, "Oldest Unscored", OLDEST, None),
        _row(6, "Oldest Low", OLDEST, 0.5),
    ]


def _summary(**overrides: Any) -> dict[str, Any]:
    summary = {"delta_counts": {}, "total_count": 6, "run_date": RUN_DATE, "ordered_by_fit": True}
    summary.update(overrides)
    return summary


def _flat_section(html: str) -> str | None:
    match = re.search(r'<section class="group" data-group="flat">.*?</section>', html, re.S)
    return match.group(0) if match else None


def _panel(html: str, key: str) -> str:
    """One tab panel, from its opening tag; a browser adds classes and attributes to that tag."""
    start = html.rindex("<section", 0, html.index(f'id="tab-{key}"'))
    following = html.find('<section class="tab-panel', start + 1)
    return html[start : following if following != -1 else html.index("<footer>")]


class MarkupTests:
    def test_the_timeline_starts_with_one_empty_table_for_the_undecided_books(self) -> None:
        html = build_html_review(_rows(), _summary())

        section = _flat_section(html)
        assert section is not None
        assert f'<h2>{FLAT_UNDECIDED_HEADING} <span class="count count--undecided">(5)</span></h2>' in section
        assert "<details" not in section
        assert 'data-key="fit"' in section
        timeline = _panel(html, "all")
        assert timeline.index('data-group="flat"') < timeline.index('data-group="date"')
        assert html.count('data-group="flat"]') > 0 and html.count("<script>") == 2

    def test_every_card_is_still_written_once_under_its_date(self) -> None:
        html = build_html_review(_rows(), _summary())

        timeline = _panel(html, "all")
        assert timeline.count("<details") == 6
        assert [day for day in re.findall(r'data-group="date" data-date="([^"]*)"', timeline)] == [
            RUN_DATE,
            OLDER,
            OLDEST,
        ]

    @pytest.mark.parametrize(
        ("rows", "summary"),
        [
            (_rows(), _summary(ordered_by_fit=False)),
            ([{**row, "fit_score": None} for row in _rows()], _summary()),
            # With no decided book the page has no undecided tab: "all" is that list.
            ([{**row, "editor_status": ""} for row in _rows()], _summary()),
        ],
        ids=["order off", "no scores", "no undecided tab"],
    )
    def test_without_the_order_a_score_or_the_tab_the_page_has_no_such_table(
        self, rows: list[dict[str, Any]], summary: dict[str, Any]
    ) -> None:
        html = build_html_review(rows, summary)

        assert _flat_section(html) is None
        assert 'data-group="flat"' not in html
        assert html.count("<script>") == 1

    def test_the_other_tabs_are_the_same_with_and_without_it(self) -> None:
        rows = [*_rows(), _row(7, "Rights Lead", RUN_DATE, None, rights_lead_only=True)]
        rows[0] = {**rows[0], "update_delta": "upgraded", "last_material_change_date": RUN_DATE}
        with_list = build_html_review(rows, _summary(total_count=7))
        without = build_html_review(rows, _summary(total_count=7, ordered_by_fit=False))

        for key in ("updated", "leads"):
            assert _panel(with_list, key) == _panel(without, key), key
        flat = _flat_section(with_list)
        assert flat is not None
        assert _panel(with_list, "all").replace(flat, "") == _panel(without, "all")


# ── In a browser ──────────────────────────────────────────────────────────

_BROWSER = next(
    (path for name in ("chromium", "chromium-browser", "google-chrome") if (path := shutil.which(name))), None
)
needs_browser = pytest.mark.skipif(_BROWSER is None, reason="no headless Chromium on this machine")


def _dom(tmp_path: Path, html: str, tab: str, then_click: str = "") -> str:
    """The page's DOM after it opened on *tab* and, optionally, the editor clicked the tab *then_click*."""
    if then_click:
        click = (
            "<script>setTimeout(function () {"
            f"document.querySelector('.tabbar .tab[data-tab=\"{then_click}\"]').click();"
            "}, 200);</script></body>"
        )
        html = html.replace("</body>", click)
    tmp_path.mkdir(parents=True, exist_ok=True)
    page = tmp_path / "review.html"
    page.write_text(html, encoding="utf-8")
    assert _BROWSER is not None
    try:
        result = subprocess.run(
            [
                _BROWSER,
                "--headless",
                "--no-sandbox",
                "--disable-gpu",
                f"--user-data-dir={tmp_path / 'profile'}",
                "--virtual-time-budget=3000",
                "--dump-dom",
                f"{page.as_uri()}#tab-{tab}",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        # A browser that cannot start here (a sandbox, no display server) says nothing about the page.
        pytest.skip("headless Chromium did not start on this machine")
    return result.stdout


def _titles(markup: str) -> list[str]:
    return re.findall(r'<span class="title">(.*?)</span>', markup)


def _date_sections(dom: str) -> list[tuple[str, list[str]]]:
    sections = re.findall(r'<section class="group" data-group="date" data-date="([^"]*)"(.*?)</section>', dom, re.S)
    return [(day, _titles(body)) for day, body in sections]


@needs_browser
class BrowserTests:
    def test_the_undecided_tab_is_one_list_by_score_then_the_unscored_newest_first(self, tmp_path: Path) -> None:
        dom = _dom(tmp_path, build_html_review(_rows(), _summary()), "undecided")

        timeline = _panel(dom, "all")
        flat = _flat_section(timeline)
        assert flat is not None
        assert _titles(flat) == ["Older Top", "Newer Mid", "Oldest Low", "Newer Unscored", "Oldest Unscored"]
        assert 'data-flat="1"' in timeline[: timeline.index(">")]
        # The decided book stays under its date, where the filter hides it.
        assert _date_sections(timeline) == [(RUN_DATE, ["Newer Decided"]), (OLDER, []), (OLDEST, [])]

    def test_priority_d_books_close_the_undecided_list_whatever_their_score(self, tmp_path: Path) -> None:
        rows = [
            *_rows(),
            _row(7, "Newer Risky Top", RUN_DATE, 4.0, priority="D"),
            _row(8, "Older Risky Unscored", OLDER, None, priority="D"),
            _row(9, "Oldest Risky Low", OLDEST, 1.0, priority="D"),
        ]

        flat = _flat_section(_panel(_dom(tmp_path, build_html_review(rows, _summary()), "undecided"), "all"))

        assert flat is not None
        assert _titles(flat) == [
            "Older Top",
            "Newer Mid",
            "Oldest Low",
            "Newer Unscored",
            "Oldest Unscored",
            "Newer Risky Top",
            "Oldest Risky Low",
            "Older Risky Unscored",
        ]

    def test_opening_the_timeline_again_puts_every_book_back_under_its_date(self, tmp_path: Path) -> None:
        html = build_html_review(_rows(), _summary())

        timeline = _panel(_dom(tmp_path, html, "undecided", then_click="all"), "all")

        assert "data-flat" not in timeline[: timeline.index(">")]
        flat = _flat_section(timeline)
        assert flat is not None and _titles(flat) == []
        assert _date_sections(timeline) == [
            (RUN_DATE, ["Newer Mid", "Newer Decided", "Newer Unscored"]),
            (OLDER, ["Older Top"]),
            (OLDEST, ["Oldest Unscored", "Oldest Low"]),
        ]

    def test_the_timeline_and_an_old_decided_tab_link_never_use_the_list(self, tmp_path: Path) -> None:
        html = build_html_review(_rows(), _summary())

        # The "decided" tab is gone (2026-11-19); a saved link to it opens the full timeline.
        for tab in ("all", "decided"):
            timeline = _panel(_dom(tmp_path / tab, html, tab), "all")
            flat = _flat_section(timeline)
            assert flat is not None and _titles(flat) == [], tab
            assert sum(len(titles) for _day, titles in _date_sections(timeline)) == 6, tab
