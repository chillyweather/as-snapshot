"""Kirkus loses no listed book silently (#873).

The bylines below are the ones Kirkus served on 27 October 2026 for the three
books the 26 October pass dropped as `author_line`.
"""

import pytest

from autoscout.kirkus_listing import coverage_debug
from autoscout.kirkus_listing import page_data
from autoscout.kirkus_listing import parse_listing_page
from autoscout.source_page_identity import incomplete_coverage_from_run
from tests.test_kirkus_listing import BULLWHIP_URL
from tests.test_kirkus_listing import LISTING
from tests.test_kirkus_listing import REFERENCE
from tests.test_kirkus_listing import SOURCE
from tests.test_kirkus_listing import captured

BYLINE = """by
                        <a href="/author/kamau-bobb/">
                            Kamau Bobb
                        </a>"""


def _with_byline(byline):
    assert LISTING[1].count(BYLINE) == 1
    return {**LISTING, 1: LISTING[1].replace(BYLINE, byline)}


def _bullwhip(listing):
    return next(row for row in parse_listing_page(listing[1]) if row["review_url"] == BULLWHIP_URL)


@pytest.mark.parametrize(
    ("byline", "authors"),
    [
        # IS IT TOO LATE TO MAKE A RUN FOR IT?
        (
            'by <a href="/author/judi-dench/"> Judi Dench </a> with <a href="/author/brendan-ohea/"> Brendan O\'Hea </a>',
            ["Judi Dench"],
        ),
        # BROADWAY IN BLACK AND WHITE
        (
            'by <a href="/author/john-mauceri/"> John Mauceri </a> &amp; '
            '<a href="/author/john-h-mcwhorter/"> John H. McWhorter </a> with '
            '<a href="/author/michael-gildin/"> Michael Gildin </a>',
            ["John Mauceri", "John H. McWhorter"],
        ),
        # WE'VE GOT A BIGGER PROBLEM NOW
        (
            'by <a href="/author/glen-e-friedman/"> Glen E. Friedman </a> illustrated by '
            '<a href="/author/glen-e-friedman/"> Glen E. Friedman </a>',
            ["Glen E. Friedman"],
        ),
        (
            'by <a href="/author/a/"> Ann Author </a> translated by <a href="/author/t/"> Tom Translator </a>',
            ["Ann Author"],
        ),
    ],
)
def test_a_collaborator_or_a_role_after_the_author_keeps_the_primary_author(byline, authors):
    row = _bullwhip(_with_byline(byline))
    assert row["rejection"] == "" and row["authors"] == authors


@pytest.mark.parametrize(
    "byline",
    [
        'with <a href="/author/a/"> Ann Author </a>',
        'by <a href="/author/a/"> Ann Author </a> and friends',
        'by with <a href="/author/a/"> Ann Author </a>',
    ],
)
def test_a_byline_without_a_grounded_primary_author_is_still_rejected(byline):
    assert _bullwhip(_with_byline(byline))["rejection"] == "author_line"


def test_every_listed_book_left_out_is_counted_and_any_but_a_paid_review_is_named():
    bundle = captured(listing=_with_byline('with <a href="/author/a/"> Ann Author </a>'))
    debug = coverage_debug(page_data(bundle, SOURCE), SOURCE)
    assert debug["kirkus_dropped_rows"] == {"author_line": 1, "missing_isbn": 1, "paid_review": 3, "self_published": 1}
    assert debug["incomplete_ranking_warning"] == "Kirkus left out 2 listed books (author_line 1, missing_isbn 1)"
    run = {"source_name": SOURCE["name"], "status": "collected", "reason": "", "debug_metadata": debug}
    assert incomplete_coverage_from_run(run) == {
        "source_name": SOURCE["name"],
        "observed_rows": 10,
        "expected_rows": 12,
        # The books left out are named, not only counted (#913).
        "left_out_titles": ["THE DETERMINISTS", "THE BULLWHIP'S ALGORITHM"],
    }


def test_paid_and_self_published_reviews_alone_are_counted_but_not_named():
    bundle = captured()
    bundle["collection"]["rows"] = [row for row in bundle["collection"]["rows"] if row["rejection"] != "missing_isbn"]
    debug = coverage_debug(page_data(bundle, SOURCE), SOURCE)
    assert debug == {"kirkus_dropped_rows": {"paid_review": 3, "self_published": 1}}


def test_a_book_left_out_for_its_genre_label_is_named():
    source = {**SOURCE, "kirkus_genre_labels": ["NONFICTION"]}
    bundle = captured(source=source)
    counts = coverage_debug(page_data(bundle, source), source)["kirkus_dropped_rows"]
    assert counts.get("undeclared_genre_label", 0) > 0


def test_a_book_reported_by_an_earlier_run_is_not_counted_as_left_out():
    source = {**SOURCE, "kirkus_genre_labels": ["NONFICTION"]}
    first = captured(source=source)
    seen = {row["detail"]["isbn"]: "2026-10-01" for row in first["collection"]["rows"] if row["detail"]}
    bundle = captured(source=source, seen=seen, reference=REFERENCE)
    assert "undeclared_genre_label" not in coverage_debug(page_data(bundle, source), source)["kirkus_dropped_rows"]


def test_the_live_run_keeps_the_failed_pass_caveat_beside_the_dropped_book(tmp_path, monkeypatch):
    import json
    from datetime import date

    from autoscout.kirkus_precollection import weekly_stage
    from autoscout.source_collectors import live_agent_extraction_inputs
    from tests.test_kirkus_listing import _pass_config
    from tests.test_kirkus_listing import listing_page_url
    from tests.test_kirkus_listing import site

    config = _pass_config(tmp_path, seen_guids_file="../state/kirkus-seen-items.json")
    source = {**json.loads(config.read_text())["sources"][0], "enable_live": True}
    fetch, _, _ = site()
    weekly_stage(config, tmp_path / "run", date(2026, 10, 6), fetch=fetch, pause=lambda seconds: None)
    fetch, _, _ = site(refuse=(listing_page_url(1),))
    weekly_stage(config, tmp_path / "run", REFERENCE, fetch=fetch, pause=lambda seconds: None)
    monkeypatch.setattr(
        "autoscout.source_collectors.live_agent_extraction_response", lambda *args: ({"entries": []}, {})
    )
    _, _, debug = live_agent_extraction_inputs(config, tmp_path, source, run_date=REFERENCE.isoformat())
    assert debug["incomplete_ranking_warning"] == (
        "incomplete collection: the Kirkus pre-collection pass of 2026-10-13 failed (kirkus_http); "
        "the result of 2026-10-06 is used; Kirkus left out 1 listed book (missing_isbn 1)"
    )
