"""BookFinder and IberLibro shop links in the editor-facing review (issues #975, #983)."""

from __future__ import annotations

from urllib.parse import parse_qs
from urllib.parse import urlsplit

import pytest

from autoscout.html_review import build_html_review
from autoscout.review_rendering import assemble_cumulative_review
from autoscout.shop_links import bookfinder_url
from autoscout.shop_links import iberlibro_link
from autoscout.shop_links import normalized_isbn


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("9788411783118", "9788411783118"),
        ("978-84-1178-311-8", "9788411783118"),
        (" 978 84 1178 311 8 ", "9788411783118"),
        ("0306406152", "0306406152"),
        ("080442957x", "080442957X"),
        ("9788411783119", ""),  # bad checksum
        ("0306406153", ""),  # bad checksum
        ("12345", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_normalized_isbn(raw, expected):
    assert normalized_isbn(raw) == expected


def test_isbn_link_opens_the_book_page_priced_for_spain():
    url = bookfinder_url("978-84-1178-311-8", "Moonwalk", "Michael Jackson")

    assert url == "https://www.bookfinder.com/isbn/9788411783118/?destination=ES&currency=EUR"


def test_without_isbn_the_link_searches_title_and_author_in_any_language():
    url = bookfinder_url("", "  Inevitablemente   yo ", "Antonio Orozco")

    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "www.bookfinder.com", "/search/")
    assert parse_qs(parts.query) == {
        "title": ["Inevitablemente yo"],
        "author": ["Antonio Orozco"],
        "lang": ["ANY"],
        "destination": ["ES"],
        "currency": ["EUR"],
    }


def test_an_invalid_isbn_falls_back_to_the_search():
    assert bookfinder_url("9788411783119", "Moonwalk", "").startswith(
        "https://www.bookfinder.com/search/?title=Moonwalk&"
    )


def test_no_isbn_and_no_title_gives_no_link():
    assert bookfinder_url(None, " ", "Antonio Orozco") == ""


def test_iberlibro_isbn_link_lists_that_edition():
    link = iberlibro_link("978-84-1178-311-8", "Moonwalk", "María Pildain")

    assert link.url == "https://www.iberlibro.com/servlet/SearchResults?isbn=9788411783118"
    assert link.isbn == "9788411783118"


def test_iberlibro_without_isbn_searches_title_and_author():
    link = iberlibro_link("9788411783119", "  Inevitablemente   yo ", "Antonio Orozco")

    parts = urlsplit(link.url)
    assert (parts.netloc, parts.path) == ("www.iberlibro.com", "/servlet/SearchResults")
    assert parse_qs(parts.query) == {"tn": ["Inevitablemente yo"], "an": ["Antonio Orozco"]}
    assert link.isbn == ""


def test_iberlibro_without_isbn_or_title_gives_no_link():
    assert iberlibro_link("", " ", "Antonio Orozco") is None


def _review_row(history_record):
    rows, _summary = assemble_cumulative_review(
        current_candidates=[], candidate_history=[history_record], current_editor_rows=[]
    )
    return rows[0]


def test_a_historical_interested_row_carries_the_shop_link():
    row = _review_row(
        {
            "identity_key": "moonwalk|michael jackson",
            "title": "Moonwalk",
            "primary_authors": ["Michael Jackson", "Someone Else"],
            "isbn": "9788411783118",
            "editor_status": "interested",
            "last_seen_date": "2026-11-01",
        }
    )

    assert row["shop_url"] == "https://www.bookfinder.com/isbn/9788411783118/?destination=ES&currency=EUR"
    assert row["iberlibro_url"] == "https://www.iberlibro.com/servlet/SearchResults?isbn=9788411783118"


def test_a_row_without_isbn_searches_by_its_first_author():
    row = _review_row(
        {
            "identity_key": "inevitablemente yo|antonio orozco",
            "title": "Inevitablemente yo",
            "primary_authors": ["Antonio Orozco", "Someone Else"],
            "editor_status": "interested",
            "last_seen_date": "2026-11-01",
        }
    )

    assert parse_qs(urlsplit(row["shop_url"]).query)["author"] == ["Antonio Orozco"]
    assert parse_qs(urlsplit(row["iberlibro_url"]).query)["an"] == ["Antonio Orozco"]


def _card(shop_url, shop_isbn="", iberlibro_url=""):
    return {
        "idx": 1,
        "identity_key": "moonwalk|michael jackson",
        "title": "Moonwalk",
        "authors": "Michael Jackson",
        "update_delta": "new",
        "editor_status": "interested",
        "shop_url": shop_url,
        "shop_isbn": shop_isbn,
        "iberlibro_url": iberlibro_url,
    }


def _html(card):
    return build_html_review([card], {"delta_counts": {"new": 1}, "total_count": 1})


def test_an_isbn_link_shows_the_isbn_as_an_exact_edition():
    url = "https://www.bookfinder.com/isbn/9788411783118/?destination=ES&currency=EUR"
    html = _html(_card(url, "9788411783118"))

    assert (
        '<dt>Где купить</dt><dd><a class="shop shop--isbn" href="https://www.bookfinder.com/isbn/9788411783118/'
        '?destination=ES&amp;currency=EUR" target="_blank" rel="noopener noreferrer">'
        'BookFinder · ISBN <span class="shop__isbn">9788411783118</span></a>'
        '<span class="shop__hint">это издание в магазинах</span></dd>'
    ) in html
    assert "shop--search" not in html.split("</style>", 1)[1]


def test_a_title_search_link_looks_different_and_says_no_isbn_was_found():
    url = "https://www.bookfinder.com/search/?title=Moonwalk&lang=ANY&destination=ES&currency=EUR"
    html = _html(_card(url))

    body = html.split("</style>", 1)[1]
    assert '<a class="shop shop--search" href="https://www.bookfinder.com/search/?title=Moonwalk&amp;' in body
    assert "Искать на BookFinder по названию и автору</a>" in body
    assert '<span class="shop__hint">ISBN не найден</span>' in body
    assert "shop--isbn" not in body


def test_iberlibro_follows_bookfinder_as_a_second_exact_edition_link():
    html = _html(
        _card(
            "https://www.bookfinder.com/isbn/9788411783118/",
            "9788411783118",
            "https://www.iberlibro.com/servlet/SearchResults?isbn=9788411783118",
        )
    )

    assert (
        '<span class="shop__isbn">9788411783118</span></a>'
        '<a class="shop shop--isbn" href="https://www.iberlibro.com/servlet/SearchResults?isbn=9788411783118" '
        'target="_blank" rel="noopener noreferrer">IberLibro</a>'
        '<span class="shop__hint">это издание в магазинах</span>'
    ) in html


def test_iberlibro_search_link_is_in_the_search_style():
    html = _html(
        _card(
            "https://www.bookfinder.com/search/?title=Moonwalk",
            iberlibro_url="https://www.iberlibro.com/servlet/SearchResults?tn=Moonwalk&an=Mar%C3%ADa",
        )
    )

    body = html.split("</style>", 1)[1]
    assert (
        "Искать на BookFinder по названию и автору</a>"
        '<a class="shop shop--search" href="https://www.iberlibro.com/servlet/SearchResults?tn=Moonwalk&amp;an=Mar%C3%ADa" '
        'target="_blank" rel="noopener noreferrer">Искать на IberLibro</a>'
        '<span class="shop__hint">ISBN не найден</span>'
    ) in body
    assert "shop--isbn" not in body


def test_the_two_link_kinds_have_distinct_styles():
    html = _html(_card(""))

    assert ".detail a.shop--isbn {" in html
    assert ".detail a.shop--search {" in html


def test_the_card_omits_where_to_buy_without_a_link():
    assert "Где купить" not in _html(_card(""))


def test_a_blank_snapshot_isbn_does_not_hide_the_history_isbn():
    history = {
        "identity_key": "moonwalk|michael jackson",
        "title": "Moonwalk",
        "primary_authors": ["Michael Jackson"],
        "isbn": "9788411783118",
        "isbn_discovery_only": True,
        "last_seen_date": "2026-11-01",
    }
    current = {**history, "isbn": ""}
    current.pop("isbn_discovery_only")
    rows, _summary = assemble_cumulative_review(
        current_candidates=[current], candidate_history=[history], current_editor_rows=[]
    )

    assert rows[0]["shop_isbn"] == "9788411783118"
    assert rows[0]["shop_url"].startswith("https://www.bookfinder.com/isbn/9788411783118/")
    assert rows[0]["iberlibro_url"].endswith("?isbn=9788411783118")


def test_a_row_without_any_isbn_gets_a_search_link_and_no_isbn():
    row = _review_row(
        {
            "identity_key": "moonwalk|michael jackson",
            "title": "Moonwalk",
            "primary_authors": ["Michael Jackson"],
            "last_seen_date": "2026-11-01",
        }
    )

    assert row["shop_isbn"] == ""
    assert row["shop_url"].startswith("https://www.bookfinder.com/search/?")
