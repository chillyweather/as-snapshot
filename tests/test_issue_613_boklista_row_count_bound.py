"""The Boklista raw-row bound must accept a healthy week of a Top 25 list.

Onboarding under #605 pinned ``ranking_expected_raw_row_count: 26`` from one
observation, week 2026-33.  The list is a first-party Top 25, so a week that
carries no tie exposes 25 rows and the exact equality rejects it.  The weekly
run reported that as ``25 rows instead of the expected 26`` on a page that was
complete and correctly parsed, ranks 1 to 25 with a title, an author, and an
ISBN in every row.

An exact equality is the wrong shape of declaration for a count the source
varies by design.  These tests pin the two-sided bound instead: a collapsed
row set still fails closed, and a tie week no longer fails at all.

Repair: #613.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoscout.source_page_identity import ranking_raw_row_count_error
from autoscout.source_page_identity import ranking_row_bounds

ROOT = Path(__file__).resolve().parents[1]
PORTFOLIO = ROOT / "configs" / "current-weekly-portfolio.json"
SOURCE_NAME = "Bokhandlerforeningen Boklista - Generell litteratur voksne"


def _boklista_source() -> dict[str, object]:
    sources = json.loads(PORTFOLIO.read_text(encoding="utf-8"))["sources"]
    for source in sources:
        if source.get("name") == SOURCE_NAME:
            return source
    raise AssertionError(f"{SOURCE_NAME} is not configured")


def test_boklista_declares_a_two_sided_bound_not_an_exact_count():
    source = _boklista_source()
    assert "ranking_expected_raw_row_count" not in source
    exact, minimum, maximum = ranking_row_bounds(source)
    assert exact is None
    assert minimum == 25
    assert maximum == 30


@pytest.mark.parametrize("raw_row_count", [25, 26, 27, 30])
def test_boklista_accepts_a_top_25_list_with_or_without_ties(raw_row_count: int):
    assert ranking_raw_row_count_error(_boklista_source(), raw_row_count) == ""


@pytest.mark.parametrize("raw_row_count", [0, 1, 24])
def test_boklista_still_fails_closed_on_a_collapsed_row_set(raw_row_count: int):
    error = ranking_raw_row_count_error(_boklista_source(), raw_row_count)
    assert error == f"ranking page exposes {raw_row_count} raw rows; expected at least 25"


@pytest.mark.parametrize("raw_row_count", [31, 50, 100])
def test_boklista_still_fails_closed_on_archive_wide_over_collection(raw_row_count: int):
    error = ranking_raw_row_count_error(_boklista_source(), raw_row_count)
    assert error == f"ranking page exposes {raw_row_count} raw rows; expected at most 30"


def test_retained_capture_stays_inside_the_declared_bound():
    """The committed week-33 capture has 26 rows and must remain admissible."""
    from autoscout.source_page_identity import ranking_rows

    capture = ROOT / (
        "data/current-weekly-pilot/codex-capture-bokhandlerforeningen-boklista-generell-litteratur-voksne-4fc8ef89.json"
    )
    source = _boklista_source()
    rows = ranking_rows(json.loads(capture.read_text(encoding="utf-8"))["html"], dict(source))
    assert len(rows) == 26
    assert ranking_raw_row_count_error(source, len(rows)) == ""
