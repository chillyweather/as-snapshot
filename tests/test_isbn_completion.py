"""ISBN completion from links, list pages, book pages and Open Library (issue #979)."""

from __future__ import annotations

import contextlib
import json
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_databases
from state_store_support import state_snapshot
from test_issue_640_description_adjudication_stage import _run_fixture_weekly

from autoscout.__main__ import main
from autoscout.candidate_schema import CandidateIsbn
from autoscout.grounded_description import BoundedHTTPError
from autoscout.grounded_description import HTTPResponse
from autoscout.identity import identity_isbn
from autoscout.isbn_completion import METHOD_BOOK_PAGE
from autoscout.isbn_completion import METHOD_OPEN_LIBRARY
from autoscout.isbn_completion import METHOD_SOURCE_LINK
from autoscout.isbn_completion import METHOD_SOURCE_PAGE
from autoscout.isbn_completion import RETRY_AFTER_DAYS
from autoscout.isbn_completion import STAGE_NAME
from autoscout.isbn_completion import BoundedPageGetter
from autoscout.isbn_completion import ProductionIsbnCompletionStage
from autoscout.isbn_completion import SourcePage
from autoscout.isbn_completion import complete_isbns
from autoscout.isbn_completion import isbn13
from autoscout.isbn_completion import isbn_from_book_page
from autoscout.isbn_completion import isbn_from_link
from autoscout.isbn_completion import isbn_from_open_library
from autoscout.isbn_completion import isbns_from_capture
from autoscout.isbn_completion import open_library_query_url
from autoscout.isbn_completion import run_isbn_completion
from autoscout.russian_workflow import stage_message
from autoscout.state_store import open_state_store
from autoscout.weekly_workflow import STAGE_ORDER
from autoscout.weekly_workflow import deterministic_adapters

MOONWALK = "9788411783118"
OROZCO = "9788408331773"
ASESINO = "9788401041587"
TODAY = date(2026, 11, 5)


# ── ISBN values ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (MOONWALK, MOONWALK),
        ("978-84-1178-311-8", MOONWALK),
        ("0306406152", "9780306406157"),
        ("080442957x", "9780804429573"),
        ("9788411783119", ""),
        ("0306406153", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_isbn13_validates_and_converts_isbn10(raw, expected):
    assert isbn13(raw) == expected


# ── 1. Source link ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.amazon.com/dp/0857197681/ref=chrt_bk_rd_nf_13", "9780857197689"),
        ("https://www.amazon.de/gp/product/3442178584", "9783442178582"),
        ("https://bookshop.org/a/7775/9780316603782", "9780316603782"),
        ("https://www.todostuslibros.com/libros/sin-respeto_978-84-493-4584-5", "9788449345845"),
        ("https://yalebooks.yale.edu/book/9780300264531/vanilla/", "9780300264531"),
        ("https://www.audible.com/pd/Book/B00WYDJ2YQ", ""),
        ("https://libro.fm/audiobooks/9798217405244", ""),
        ("https://www.amazon.com/dp/B0DWJ1SP94", ""),
        ("https://example.test/9780316603782/9780300264531", ""),
        ("https://example.test/book/moonwalk", ""),
    ],
)
def test_isbn_from_link(url, expected):
    assert isbn_from_link(url) == expected


# ── 2. Captured list page ────────────────────────────────────────────────


def _json_ld_list(books: list[tuple[str, str]]) -> str:
    items = [
        {
            "@type": "ListItem",
            "item": {
                "@type": "Book",
                "name": title,
                "sameAs": f"https://shop.test/libro-{isbn}" if isbn else "https://shop.test/x",
            },
        }
        for title, isbn in books
    ]
    return '<script type="application/ld+json">' + json.dumps({"itemListElement": items}) + "</script>"


def test_a_json_ld_record_gives_its_own_isbn_to_its_own_title():
    markup = _json_ld_list([("Moonwalk", MOONWALK), ("Inevitablemente yo", OROZCO), ("Real Madrid", ASESINO)])

    assert isbns_from_capture(markup, ["Moonwalk", "Inevitablemente yo", "Real Madrid"]) == {
        "moonwalk": MOONWALK,
        "inevitablemente yo": OROZCO,
        "real madrid": ASESINO,
    }


def test_the_previous_records_isbn_never_reaches_the_next_title():
    # The real NuevosLibros case: the previous book's ISBN closes its record,
    # right before "Inevitablemente yo", whose own record has no ISBN.
    markup = _json_ld_list([("En la mente de un asesino", ASESINO), ("Inevitablemente yo", "")])

    assert isbns_from_capture(markup, ["Inevitablemente yo"]) == {}


@pytest.mark.parametrize("cover_first", [True, False])
def test_a_card_gives_its_isbn_to_its_title_whichever_comes_first(cover_first):
    def card(title: str, isbn: str) -> str:
        cover, heading = f'<img src="/covers/{isbn}.jpg">', f"<h3>{title}</h3>"
        return f"<div>{cover + heading if cover_first else heading + cover}</div>"

    page = "".join(card(title, isbn) for title, isbn in [("Moonwalk", MOONWALK), ("Real Madrid", ASESINO)])

    assert isbns_from_capture(page, ["Moonwalk", "Real Madrid"]) == {"moonwalk": MOONWALK, "real madrid": ASESINO}


def test_isbns_that_only_sit_near_titles_belong_to_no_one():
    # Review finding on #981: "X ISBN_X, A ISBN_A, B ISBN_B, C" in one flat
    # block, X not extracted. No card holds one title alone.
    page = f"<p>X {ASESINO} Moonwalk {MOONWALK} Real Madrid {OROZCO} Bandidos</p>"

    assert isbns_from_capture(page, ["Moonwalk", "Real Madrid", "Bandidos"]) == {}


def test_a_card_without_its_own_isbn_never_borrows_a_neighbours():
    # A store page whose own covers carry no ISBN; an unextracted card does.
    page = (
        '<div class="row"><div><img src="/c/mediamodifier.webp"><h3>Moonwalk</h3></div>'
        f'<div><img src="/c/{ASESINO}.jpg" alt="BORGES"><h3>Borges</h3></div></div>'
    )

    assert isbns_from_capture(page, ["Moonwalk"]) == {}


def test_a_buy_link_around_the_title_is_its_card():
    page = f'<p>• <strong>Zara Anishanslin</strong><br><a href="https://www.hup.harvard.edu/books/{MOONWALK}"><i>The Painter’s Fire</i></a></p>'

    assert isbns_from_capture(page, ["The Painter’s Fire: A Forgotten History"]) == {"the painter s fire": MOONWALK}


def test_a_title_whose_records_disagree_gets_nothing():
    page = _json_ld_list([("Moonwalk", MOONWALK)]) + f'<div><img src="/c/{OROZCO}.jpg"><h3>Moonwalk</h3></div>'

    assert isbns_from_capture(page, ["Moonwalk"]) == {}


def test_a_short_link_in_a_sibling_card_is_another_books_not_this_ones():
    # Review finding on #981 round 3.
    page = f'<div class="row"><div>Moonwalk</div><div><a href="/{OROZCO}">Borges</a></div></div>'

    assert isbns_from_capture(page, ["Moonwalk"]) == {}


def test_a_buy_button_and_a_labelled_isbn_are_owned_by_their_card():
    page = (
        f'<div><h3>Moonwalk</h3><a href="https://bookshop.org/a/1/{MOONWALK}">Comprar</a></div>'
        f"<div><h3>Real Madrid</h3><p>ISBN: {ASESINO}</p></div>"
    )

    assert isbns_from_capture(page, ["Moonwalk", "Real Madrid"]) == {"moonwalk": MOONWALK, "real madrid": ASESINO}


def test_a_neighbouring_card_with_a_labelled_isbn_is_not_this_books():
    # Review finding on #981 round 6.
    page = f'<div class="row"><div><h3>Moonwalk</h3></div><div><p>Borges</p><p>ISBN: {OROZCO}</p></div></div>'

    assert isbns_from_capture(page, ["Moonwalk"]) == {}


def test_a_loose_isbn_in_a_container_with_another_heading_is_no_ones():
    # Review finding on #981 round 7.
    page = f"<main><h3>Moonwalk</h3><h3>Borges</h3>ISBN: {OROZCO}</main>"

    assert isbns_from_capture(page, ["Moonwalk"]) == {}
    assert isbn_from_book_page(page, "Moonwalk") == ""


def test_a_longer_title_containing_the_title_is_another_book():
    # Review finding on #981 round 7: "Becoming" is not "Becoming Human".
    card = f'<div><img src="/c/{OROZCO}.jpg"><h3>Becoming Human</h3></div>'
    page = (
        f'<html><head><title>Becoming Human</title><meta property="books:isbn" content="{OROZCO}"></head>'
        "<body><h1>Becoming Human</h1></body></html>"
    )

    assert isbns_from_capture(card, ["Becoming"]) == {}
    assert isbn_from_book_page(page, "Becoming") == ""


@pytest.mark.parametrize(
    "subject",
    [
        "Moonwalk, de Michael Jackson · Alba · nuevoslibros",
        "Moonwalk by Michael Jackson | Goodreads",
        "Moonwalk: Memorias",
    ],
)
def test_a_page_subject_with_presentation_separators_is_the_book(subject):
    page = f'<html><head><title>{subject}</title><meta property="books:isbn" content="{MOONWALK}"></head></html>'

    assert isbn_from_book_page(page, "Moonwalk") == MOONWALK


def test_one_title_by_two_authors_with_one_surname_gets_nothing():
    # Review finding on #981 round 8: Jane Smith and John Smith.
    page = _json_ld_list([("Elon Musk", MOONWALK)])

    assert isbns_from_capture(page, ["Elon Musk", "Elon Musk"], ["Jane Smith", "John Smith"]) == {}
    assert isbns_from_capture(page, ["Elon Musk", "Elon Musk"], ["Smith, Jane", "Jane Smith"]) == {
        "elon musk": MOONWALK
    }


def test_a_list_card_is_no_evidence_for_a_title_the_json_ld_gives_another_author():
    # Review finding on #981 round 12.
    record = {"@type": "Book", "name": "Elon Musk", "author": {"name": "Ashlee Vance"}}
    page = (
        f'<script type="application/ld+json">{json.dumps(record)}</script>'
        f'<div><img src="/c/{OROZCO}.jpg"><h3>Elon Musk</h3></div>'
    )

    assert isbns_from_capture(page, ["Elon Musk"], ["Walter Isaacson"]) == {}
    assert isbns_from_capture(page, ["Elon Musk"], ["Ashlee Vance"]) == {"elon musk": OROZCO}


def test_a_page_about_another_authors_book_gives_no_weaker_evidence_either():
    # Review finding on #981 round 11.
    record = {"@type": "Book", "name": "Elon Musk", "author": {"name": "Ashlee Vance"}}
    page = (
        f'<html><head><title>Elon Musk</title><meta property="books:isbn" content="{OROZCO}"></head>'
        f'<body><div><h1>Elon Musk</h1><img src="/c/{OROZCO}.jpg"></div>'
        f'<script type="application/ld+json">{json.dumps(record)}</script></body></html>'
    )

    assert isbn_from_book_page(page, "Elon Musk", "Walter Isaacson") == ""
    assert isbn_from_book_page(page, "Elon Musk", "Ashlee Vance") == OROZCO


def test_a_structured_record_by_another_author_is_another_book():
    # Review finding on #981 round 10: Vance's "Elon Musk" for an Isaacson candidate.
    record = {
        "@type": "Book",
        "name": "Elon Musk",
        "author": {"@type": "Person", "name": "Ashlee Vance"},
        "isbn": OROZCO,
    }
    page = '<script type="application/ld+json">' + json.dumps(record) + "</script>"
    book_page = "<h1>Elon Musk</h1>" + page

    assert isbns_from_capture(page, ["Elon Musk"], ["Walter Isaacson"]) == {}
    assert isbns_from_capture(page, ["Elon Musk"], ["Isaacson, Ashlee Vance"]) == {}
    assert isbns_from_capture(page, ["Elon Musk"], ["Vance, Ashlee"]) == {"elon musk": OROZCO}
    assert isbn_from_book_page(book_page, "Elon Musk", "Walter Isaacson") == ""
    assert isbn_from_book_page(book_page, "Elon Musk", "Ashlee Vance") == OROZCO


def test_an_image_labelled_with_another_book_is_that_books():
    # Review finding on #981 round 13.
    page = f'<div><h2>Moonwalk</h2><img alt="Inevitablemente yo by Antonio Orozco" src="/{OROZCO}.jpg"></div>'

    assert isbns_from_capture(page, ["Moonwalk"]) == {}
    assert isbn_from_book_page("<title>Moonwalk</title>" + page, "Moonwalk") == ""


def test_an_image_labelled_with_the_cards_own_title_is_its_cover():
    page = f'<div><img alt="Portada de «Moonwalk», de Michael Jackson" src="/{MOONWALK}.jpg"><h2>Moonwalk</h2></div>'

    assert isbns_from_capture(page, ["Moonwalk"]) == {"moonwalk": MOONWALK}


def test_a_cover_labelled_with_a_longer_title_is_another_books():
    # Review finding on #981 round 15.
    page = f'<div><h3>Becoming</h3><img alt="Becoming Human" src="/{MOONWALK}.jpg"></div>'

    assert isbns_from_capture(page, ["Becoming"]) == {}


def test_an_isbn_label_inside_another_books_link_is_that_books():
    # Review finding on #981 round 16.
    page = f'<div><h3>Moonwalk</h3><a href="/books/{OROZCO}">Borges (ISBN: {OROZCO})</a></div>'

    assert isbns_from_capture(page, ["Moonwalk"]) == {}


def test_an_apostrophe_continues_a_title():
    # Review finding on #981 round 16: "Man's Search for Meaning" is not "Man".
    for apostrophe in ("'", "’"):
        page = (
            f"<html><head><title>Man{apostrophe}s Search for Meaning</title>"
            f'<meta property="books:isbn" content="{MOONWALK}"></head></html>'
        )
        assert isbn_from_book_page(page, "Man") == ""


def test_loose_card_text_naming_another_book_is_not_ownership():
    # Review finding on #981 round 19.
    page = f"<div><h3>Moonwalk</h3>Related: Borges ISBN: {ASESINO}</div>"

    assert isbns_from_capture(page, ["Moonwalk"]) == {}


def test_a_loose_bare_isbn_line_in_the_card_is_owned():
    page = f"<div><h3>Moonwalk</h3>ISBN: {MOONWALK}</div>"

    assert isbns_from_capture(page, ["Moonwalk"]) == {"moonwalk": MOONWALK}


def test_a_card_naming_another_author_is_another_books():
    # Review finding on #981 round 21.
    card = f'<div><img src="/c/{OROZCO}.jpg"><h3>Elon Musk</h3><span itemprop="author">Ashlee Vance</span></div>'

    assert isbns_from_capture(card, ["Elon Musk"], ["Walter Isaacson"]) == {}
    assert isbns_from_capture(card, ["Elon Musk"], ["Ashlee Vance"]) == {"elon musk": OROZCO}
    assert isbn_from_book_page("<title>Elon Musk</title>" + card, "Elon Musk", "Walter Isaacson") == ""


def test_two_books_sharing_a_main_title_get_nothing():
    # Review finding on #981 round 3: "Hope: A Memoir" and "Hope: A New
    # History" both match "hope".
    page = _json_ld_list([("Hope: A Memoir", MOONWALK), ("Other", OROZCO)])

    assert isbns_from_capture(page, ["Hope: A Memoir", "Hope: A New History", "Other"]) == {"other": OROZCO}


def test_a_records_related_book_reference_is_not_its_isbn():
    # Review finding on #981 round 5.
    page = (
        '<script type="application/ld+json">'
        + json.dumps(
            {
                "@type": "Book",
                "name": "Moonwalk",
                "isSimilarTo": {"@id": f"https://shop.test/books/{OROZCO}"},
                "description": f"Del autor de {ASESINO}.",
            }
        )
        + "</script>"
    )

    assert isbns_from_capture(page, ["Moonwalk"]) == {}


def test_a_records_own_identifiers_editions_and_offers_count():
    record = {
        "@type": "Book",
        "name": "Moonwalk",
        "identifier": {"@type": "PropertyValue", "propertyID": "ISBN", "value": MOONWALK},
        "workExample": [{"@type": "Book", "isbn": MOONWALK}],
        "offers": {"url": f"https://shop.test/libro-{MOONWALK}"},
    }
    page = '<script type="application/ld+json">' + json.dumps(record) + "</script>"

    assert isbns_from_capture(page, ["Moonwalk"]) == {"moonwalk": MOONWALK}


def test_one_title_by_two_authors_gets_nothing():
    # Review finding on #981 round 5: two "Elon Musk" biographies.
    page = _json_ld_list([("Elon Musk", MOONWALK)])

    assert isbns_from_capture(page, ["Elon Musk", "Elon Musk"], ["Walter Isaacson", "Ashlee Vance"]) == {}
    assert isbns_from_capture(page, ["Elon Musk", "Elon Musk"], ["Walter Isaacson", "Walter Isaacson"]) == {
        "elon musk": MOONWALK
    }


# ── 3. Book page ─────────────────────────────────────────────────────────


def _ld(record: dict[str, Any]) -> str:
    return f'<script type="application/ld+json">{json.dumps(record)}</script>'


def test_book_page_structured_data_wins():
    page = (
        f"<title>Moonwalk</title>{_ld({'@type': 'Book', 'name': 'Moonwalk', 'isbn': MOONWALK})}"
        f'<a href="https://bookshop.org/a/1/{OROZCO}">related</a>'
    )

    assert isbn_from_book_page(page, "Moonwalk") == MOONWALK


def test_book_page_isbn10_and_isbn13_of_one_book_agree():
    page = "<h1>Abundance</h1>" + _ld({"name": "Abundance", "isbn": "1668023482", "isbn13": "9781668023488"})

    assert isbn_from_book_page(page, "Abundance") == "9781668023488"


def test_book_page_two_structured_isbns_decide_unknown_and_never_fall_through():
    page = (
        "<h1>Moonwalk</h1>"
        + _ld({"name": "Moonwalk", "isbn": MOONWALK})
        + _ld({"name": "Moonwalk", "isbn": OROZCO})
        + f"<p>ISBN {MOONWALK}</p>"
    )

    assert isbn_from_book_page(page, "Moonwalk") == ""


def test_book_page_label_line_with_other_words_is_not_ownership():
    # A label line that names more ("ISBN … Hogarth") may describe another book.
    page = f"<div><h1>Huna: A Memoir</h1><figcaption>ISBN {MOONWALK}<br> Hogarth</figcaption></div>"

    assert isbn_from_book_page(page, "Huna: A Memoir of Revolution") == ""


def test_book_page_label():
    page = f"<div><h1>Huna: A Memoir</h1><p>ISBN-13: {MOONWALK}</p></div>"

    assert isbn_from_book_page(page, "Huna: A Memoir of Revolution") == MOONWALK


def test_book_page_labelled_isbn10():
    # Review finding on #981 round 17.
    page = "<div><h1>A Brief History of Time</h1><p>ISBN-10: 0553380168</p></div>"

    assert isbn_from_book_page(page, "A Brief History of Time") == "9780553380163"


def test_a_bare_ten_digit_number_is_no_isbn():
    page = "<div><h1>A Brief History of Time</h1><p>0553380168</p></div>"

    assert isbn_from_book_page(page, "A Brief History of Time") == ""


def test_book_page_store_links():
    page = (
        f'<main><div><h1>Art Cure</h1><a href="https://uk.bookshop.org/a/4911/{MOONWALK}">Buy</a></div>'
        f'<section class="related-books"><img src="/uploads/{OROZCO}_Other.jpg"><h3>Other</h3></section></main>'
    )

    assert isbn_from_book_page(page, "Art Cure") == MOONWALK


def test_book_page_cover_beside_the_title():
    page = f'<div><img class="book-jacket" src="/uploads/{MOONWALK}.webp"><h1>Alexander</h1></div>'

    assert isbn_from_book_page(page, "Alexander") == MOONWALK


def test_book_page_never_takes_a_related_books_isbn():
    # Review finding on #981 round 3.
    page = (
        "<main><h1>Moonwalk</h1><p>Memorias.</p>"
        f'<section class="related"><h2>También te puede gustar</h2><p>Borges</p><p>ISBN: {OROZCO}</p></section></main>'
    )

    assert isbn_from_book_page(page, "Moonwalk") == ""


def test_book_page_ignores_related_book_microdata():
    # Review finding on #981 round 4.
    page = (
        "<main><h1>Moonwalk</h1><p>Memorias.</p>"
        f'<section class="related"><h2>Borges</h2><meta itemprop="isbn" content="{OROZCO}"></section></main>'
    )

    assert isbn_from_book_page(page, "Moonwalk") == ""


def test_book_page_head_meta_isbn_describes_the_page():
    page = f'<html><head><meta property="books:isbn" content="{MOONWALK}"></head><body><h1>Moonwalk</h1></body></html>'

    assert isbn_from_book_page(page, "Moonwalk") == MOONWALK


def test_head_metadata_of_another_book_is_not_the_related_titles():
    # Review finding on #981 round 6: Moonwalk appears only in a related block.
    page = (
        f'<html><head><title>Borges</title><meta property="books:isbn" content="{OROZCO}"></head>'
        '<body><h1>Borges</h1><section class="related"><p>Moonwalk</p></section></body></html>'
    )

    assert isbn_from_book_page(page, "Moonwalk") == ""


def test_book_page_ignores_the_structured_record_of_another_book():
    # Review finding on #981: a related book's JSON-LD must not win.
    page = "<div><h1>Moonwalk</h1>" + _ld({"name": "Thriller", "isbn": OROZCO}) + f"<p>ISBN: {MOONWALK}</p></div>"

    assert isbn_from_book_page(page, "Moonwalk") == MOONWALK


def test_book_page_without_the_title_is_not_this_book():
    page = "<h1>Something else</h1>" + _ld({"name": "Something else", "isbn": MOONWALK})

    assert isbn_from_book_page(page, "Moonwalk") == ""


def test_book_page_title_match_ignores_accents_and_subtitle():
    page = "<h1>Por un punado de fosiles</h1>" + _ld({"name": "Por un puñado de fósiles", "isbn": MOONWALK})

    assert isbn_from_book_page(page, "Por un puñado de fósiles: Historia") == MOONWALK


# ── 4. Open Library ──────────────────────────────────────────────────────


def _open_library(title="Moonwalk", author="Michael Jackson", language="spa", year="2026", isbns=(MOONWALK,)):
    return {
        "docs": [
            {
                "title": title,
                "author_name": [author],
                "editions": {
                    "docs": [
                        {
                            "key": "/books/OL1M",
                            "language": [language],
                            "publish_date": [year],
                            "isbn": list(isbns),
                        }
                    ]
                },
            }
        ]
    }


def test_open_library_agreeing_edition():
    assert isbn_from_open_library(_open_library(), "Moonwalk", "Michael Jackson", "es", 2025) == (
        MOONWALK,
        "https://openlibrary.org/books/OL1M",
    )


@pytest.mark.parametrize(
    "response",
    [
        _open_library(title="Moonwalker"),
        _open_library(author="Someone Else"),
        _open_library(language="eng"),
        _open_library(year="1988"),
        _open_library(isbns=()),
        _open_library(isbns=(MOONWALK, OROZCO)),
    ],
)
def test_open_library_disagreeing_editions_give_nothing(response):
    assert isbn_from_open_library(response, "Moonwalk", "Michael Jackson", "es", 2025) == ("", "")


@pytest.mark.parametrize(
    "edition_format", [{"physical_format": "Audio CD"}, {"format": ["MP3 CD"]}, {"physical_format": "Hörbuch"}]
)
def test_open_library_never_takes_an_audio_edition(edition_format):
    # Review finding on #981 round 4.
    response = _open_library()
    response["docs"][0]["editions"]["docs"][0].update(edition_format)

    assert isbn_from_open_library(response, "Moonwalk", "Michael Jackson", "es", 2025) == ("", "")


def test_open_library_takes_a_paperback():
    response = _open_library()
    response["docs"][0]["editions"]["docs"][0]["physical_format"] = "Paperback"

    assert isbn_from_open_library(response, "Moonwalk", "Michael Jackson", "es", 2025)[0] == MOONWALK


def test_open_library_matches_a_translation_by_its_edition_title():
    # Review finding on #981 round 8.
    response = _open_library(title="The Let Them Theory", author="Mel Robbins", language="ger")
    response["docs"][0]["editions"]["docs"][0]["title"] = "Die LET THEM Theorie"

    assert isbn_from_open_library(response, "Die LET THEM Theorie", "Mel Robbins", "de", 2025)[0] == MOONWALK


def test_open_library_edition_title_must_agree_when_present():
    response = _open_library()
    response["docs"][0]["editions"]["docs"][0]["title"] = "Moonwalker"

    assert isbn_from_open_library(response, "Moonwalk", "Michael Jackson", "es", 2025) == ("", "")


def test_open_library_best_edition_of_one_work_is_accepted_although_more_exist():
    # Deliberate (review round 18 on #981): Open Library returns one edition
    # per work and counts the rest; editions of one work in one language are
    # the same book for the shop link.
    response = _open_library()
    response["docs"][0]["editions"]["numFound"] = 4

    assert isbn_from_open_library(response, "Moonwalk", "Michael Jackson", "es", 2025)[0] == MOONWALK


def test_open_library_two_works_with_different_isbns_give_nothing():
    response = {"docs": _open_library()["docs"] + _open_library(isbns=(OROZCO,))["docs"]}

    assert isbn_from_open_library(response, "Moonwalk", "Michael Jackson", "es", 2025) == ("", "")


def test_open_library_query_asks_for_the_source_language():
    url = open_library_query_url("Moonwalk: Memorias", "Michael Jackson", "es")

    assert "language%3Aspa" in url
    assert "title%3A%22moonwalk%22" in url
    assert "author%3A%22jackson%22" in url


# ── Completion ───────────────────────────────────────────────────────────


class FakeGetter:
    def __init__(self, pages: dict[str, str] | None = None, on_get: Any = None, catalogue_down: bool = False) -> None:
        self.pages = pages or {}
        self.catalogue_down = catalogue_down
        self.requests: list[str] = []
        self.timeouts: list[float | None] = []
        self.on_get = on_get

    def get(self, url: str, *, timeout: float | None = None) -> str | None:
        self.requests.append(url)
        self.timeouts.append(timeout)
        if self.on_get is not None:
            self.on_get()
        for prefix, body in self.pages.items():
            if url.startswith(prefix):
                return body
        if url.startswith("https://openlibrary.org/") and not self.catalogue_down:
            return json.dumps({"docs": []})  # the catalogue answers: no such edition
        return None


def _record(title="Moonwalk", evidence_url="https://nuevoslibros.com/libro/alba-moonwalk/", **extra):
    record = {
        "identity_key": f"{title.lower()}|michael jackson",
        "title": title,
        "primary_authors": ["Michael Jackson"],
        "first_seen_date": "2026-11-01",
        "source_evidence": [
            {
                "source_name": "NuevosLibros — Biografía y memorias",
                "source_url": "https://nuevoslibros.com/genero/biografia-y-memorias/",
                "evidence_url": evidence_url,
                "language": "es",
            }
        ],
    }
    record.update(extra)
    return record


def _book_page(title, isbn):
    return f"<h1>{title}</h1>" + _ld({"@type": "Book", "name": title, "isbn": isbn})


def test_a_learned_isbn_is_discovery_only_with_provenance():
    record = _record()
    getter = FakeGetter({"https://nuevoslibros.com/libro/alba-moonwalk/": _book_page("Moonwalk", MOONWALK)})

    report = complete_isbns([record], pages={}, getter=getter, today=TODAY)

    assert CandidateIsbn.from_record(record) == CandidateIsbn(MOONWALK, discovery_only=True)
    assert identity_isbn(record) == ""  # never proof that two mentions are one work
    assert record["isbn_provenance"] == {
        "method": METHOD_BOOK_PAGE,
        "url": "https://nuevoslibros.com/libro/alba-moonwalk/",
        "date": "2026-11-05",
    }
    assert report.completed[METHOD_BOOK_PAGE] == 1


def test_clearing_or_replacing_an_isbn_drops_its_provenance():
    # Review finding on #981 round 15: enrichment clears or learns through CandidateIsbn.
    record = {"isbn": MOONWALK, "isbn_discovery_only": True, "isbn_provenance": {"method": METHOD_BOOK_PAGE}}
    CandidateIsbn.from_record(record).clear_from(record)
    assert record == {}

    record = {"isbn_provenance": {"method": METHOD_BOOK_PAGE, "url": "https://old.test/"}}
    CandidateIsbn(OROZCO, discovery_only=True).apply_to(record)
    assert record == {"isbn": OROZCO, "isbn_discovery_only": True}


def test_an_existing_isbn_is_never_replaced_or_fetched():
    record = _record(isbn=OROZCO)
    getter = FakeGetter({"https://": _book_page("Moonwalk", MOONWALK)})

    report = complete_isbns([record], pages={}, getter=getter, today=TODAY)

    assert record["isbn"] == OROZCO
    assert "isbn_discovery_only" not in record
    assert getter.requests == []
    assert report.considered == 0


def test_the_source_link_is_used_without_any_request():
    record = _record(evidence_url="https://www.amazon.com/dp/0857197681/ref=x")
    getter = FakeGetter()

    complete_isbns([record], pages={}, getter=getter, today=TODAY)

    assert record["isbn"] == "9780857197689"
    assert record["isbn_provenance"]["method"] == METHOD_SOURCE_LINK
    assert getter.requests == []


def test_the_source_page_is_used_without_any_request():
    record = _record(evidence_url="https://nuevoslibros.com/genero/biografia-y-memorias/")
    page = SourcePage(
        source_name="NuevosLibros — Biografía y memorias",
        markup=_json_ld_list([("Moonwalk", MOONWALK), ("Bandidos", OROZCO)]),
        titles=("Moonwalk", "Bandidos"),
    )
    getter = FakeGetter()

    complete_isbns([record], pages={page.source_name: page}, getter=getter, today=TODAY)

    assert record["isbn"] == MOONWALK
    assert record["isbn_provenance"]["method"] == METHOD_SOURCE_PAGE
    assert getter.requests == []


def test_audiobook_sources_skip_their_pages_and_use_open_library():
    record = _record(evidence_url="https://www.audiobooks.com/audiobook/moonwalk/1")
    record["source_evidence"][0]["source_name"] = "Audiobooks.com Nonfiction Audiobook Bestsellers"
    getter = FakeGetter(
        {
            "https://www.audiobooks.com/": _book_page("Moonwalk", OROZCO),
            "https://openlibrary.org/": json.dumps(_open_library()),
        }
    )

    complete_isbns([record], pages={}, getter=getter, today=TODAY)

    assert record["isbn"] == MOONWALK
    assert record["isbn_provenance"]["method"] == METHOD_OPEN_LIBRARY
    assert all("audiobooks.com" not in url for url in getter.requests)


def test_an_audiobook_sources_link_is_never_its_isbn():
    # Review finding on #981 round 14: the store link names the audio edition.
    record = _record(evidence_url=f"https://shop.test/audiolibros/{OROZCO}")
    record["source_evidence"][0]["source_name"] = "Audible Bestselling Science & Engineering Audiobooks"
    getter = FakeGetter({"https://openlibrary.org/": json.dumps(_open_library())})

    complete_isbns([record], pages={}, getter=getter, today=TODAY)

    assert record["isbn"] == MOONWALK
    assert record["isbn_provenance"]["method"] == METHOD_OPEN_LIBRARY


def test_a_miss_records_the_lookup_date_and_is_not_asked_again_soon():
    record = _record()
    getter = FakeGetter()

    first = complete_isbns([record], pages={}, getter=getter, today=TODAY)
    asked = len(getter.requests)
    second = complete_isbns([record], pages={}, getter=getter, today=TODAY.replace(day=20))
    later = complete_isbns(
        [record], pages={}, getter=getter, today=date.fromordinal(TODAY.toordinal() + RETRY_AFTER_DAYS)
    )

    assert first.not_found == 1 and record.get("isbn_lookup_date")
    assert second.skipped_recently_tried == 1
    assert len(getter.requests) > asked  # the later pass asked again
    assert later.not_found == 1


def test_an_unreachable_catalogue_is_no_miss_and_is_asked_again_next_run():
    # Review finding on #981 round 20: a timeout or HTTP error is not a "no".
    record = _record()

    report = complete_isbns([record], pages={}, getter=FakeGetter(catalogue_down=True), today=TODAY)

    assert report.lookup_unavailable == 1 and report.not_found == 0
    assert "isbn_lookup_date" not in record


def test_a_malformed_catalogue_answer_is_no_miss():
    record = _record()

    report = complete_isbns(
        [record], pages={}, getter=FakeGetter({"https://openlibrary.org/": "<html>busy</html>"}), today=TODAY
    )

    assert report.lookup_unavailable == 1
    assert "isbn_lookup_date" not in record


@pytest.mark.parametrize("body", ['{"error": "temporarily unavailable"}', "{}", '{"docs": null}', "[]"])
def test_a_catalogue_answer_without_a_docs_list_is_no_miss(body):
    # Review finding on #981 round 21.
    record = _record()

    report = complete_isbns([record], pages={}, getter=FakeGetter({"https://openlibrary.org/": body}), today=TODAY)

    assert report.lookup_unavailable == 1
    assert "isbn_lookup_date" not in record


def test_a_backlist_book_without_a_publication_year_accepts_an_older_edition():
    # Review finding on #981 round 20: the first-seen date is no publication date.
    record = _record(title="Moonwalk", first_seen_date="2026-11-01")
    getter = FakeGetter({"https://openlibrary.org/": json.dumps(_open_library(year="1988"))})

    complete_isbns([record], pages={}, getter=getter, today=TODAY)

    assert record["isbn"] == MOONWALK


def test_offline_completion_makes_no_request_and_records_no_lookup_date():
    record = _record()

    report = complete_isbns([record], pages={}, getter=None, today=TODAY)

    assert "isbn_lookup_date" not in record
    assert report.not_found == 0


def test_an_exhausted_budget_stops_the_network_methods_only():
    clock = iter([0.0, 1000.0, 1000.0, 1000.0])
    linked = _record(title="Linked", evidence_url="https://www.amazon.com/dp/0857197681")
    other = _record(title="Other")
    getter = FakeGetter()

    report = complete_isbns(
        [linked, other], pages={}, getter=getter, today=TODAY, budget_seconds=10, monotonic=lambda: next(clock)
    )

    assert linked["isbn"] == "9780857197689"
    assert report.budget_exhausted == 1
    assert getter.requests == []
    assert "isbn_lookup_date" not in other


def test_the_deadline_is_checked_before_every_request_of_one_book():
    # Review finding on #981: slow responses must not start requests after
    # the budget. Each request takes 20 s of a 10 s budget.
    now = [0.0]
    getter = FakeGetter(on_get=lambda: now.__setitem__(0, now[0] + 20.0))
    record = _record()
    record["source_evidence"].append(dict(record["source_evidence"][0], evidence_url="https://other.test/moonwalk"))

    report = complete_isbns([record], pages={}, getter=getter, today=TODAY, budget_seconds=10, monotonic=lambda: now[0])

    assert len(getter.requests) == 1
    assert getter.timeouts == [10.0]
    assert report.budget_exhausted == 1 and report.not_found == 0
    assert "isbn_lookup_date" not in record  # cut short, asked again next run


def test_a_request_that_runs_into_the_deadline_is_cut_short_not_a_miss():
    # Review finding on #981 round 3: the last request consumed the budget.
    now = [0.0]
    getter = FakeGetter(on_get=lambda: now.__setitem__(0, now[0] + 50.0))
    record = _record(evidence_url="https://nuevoslibros.com/genero/biografia-y-memorias/")  # Open Library only

    report = complete_isbns([record], pages={}, getter=getter, today=TODAY, budget_seconds=10, monotonic=lambda: now[0])

    assert len(getter.requests) == 1
    assert report.budget_exhausted == 1 and report.not_found == 0
    assert "isbn_lookup_date" not in record


def test_a_stored_book_page_equal_to_its_source_url_is_still_fetched():
    # Review finding on #981 round 7: a single-review source (BookPage).
    review = "https://www.bookpage.com/reviews/obstinate-daughters/"
    record = _record(title="Obstinate Daughters", evidence_url=review)
    record["source_evidence"][0]["source_url"] = review
    getter = FakeGetter({review: _book_page("Obstinate Daughters", MOONWALK)})

    complete_isbns([record], pages={}, getter=getter, today=TODAY)

    assert record["isbn"] == MOONWALK
    assert getter.requests[0] == review


def test_a_captured_list_page_is_never_fetched_as_a_book_page():
    listing = "https://nuevoslibros.com/genero/biografia-y-memorias"
    record = _record(evidence_url=listing + "/")
    page = SourcePage(source_name="other", markup="", titles=(), urls=frozenset({listing}))
    getter = FakeGetter()

    complete_isbns([record], pages={"other": page}, getter=getter, today=TODAY)

    assert all("nuevoslibros.com" not in url for url in getter.requests)


def test_one_url_is_fetched_once_per_run():
    shared = "https://bookmarks.reviews/category/non-fiction/"
    records = [_record(title=title, evidence_url=shared) for title in ("Moonwalk", "Bandidos")]
    getter = FakeGetter()

    complete_isbns(records, pages={}, getter=getter, today=TODAY)

    assert getter.requests.count(shared) == 1


def test_kirkus_pages_are_never_fetched():
    record = _record(evidence_url="https://www.kirkusreviews.com/book-reviews/michael-jackson/moonwalk/")
    getter = FakeGetter()

    complete_isbns([record], pages={}, getter=getter, today=TODAY)

    assert all("kirkusreviews.com" not in url for url in getter.requests)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.iberlibro.com/servlet/SearchResults?isbn=9788411783118",
        "https://www.abebooks.co.uk/servlet/BookDetailsPL?bi=1",
        "https://www.zvab.com/servlet/SearchResults?isbn=9788411783118",
    ],
)
def test_abebooks_sites_are_never_fetched(url):
    getter = FakeGetter()

    complete_isbns([_record(evidence_url=url)], pages={}, getter=getter, today=TODAY)

    assert url not in getter.requests


# ── State files ──────────────────────────────────────────────────────────


def _state(tmp: Path, history: list[dict[str, Any]], current_keys: list[str]) -> None:
    seed_state(tmp, candidate_history=history)
    rows = [{"identity_key": key, "isbn": ""} for key in current_keys]
    (tmp / "scouting-candidates.json").write_text(json.dumps(rows), encoding="utf-8")
    seed_state(tmp, latest_scouting_candidates=rows)


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def test_run_writes_history_then_projects_onto_both_snapshots(tmp_path):
    moonwalk = _record()
    _state(tmp_path, [moonwalk], [moonwalk["identity_key"]])
    getter = FakeGetter({"https://nuevoslibros.com/": _book_page("Moonwalk", MOONWALK)})

    result = run_isbn_completion(tmp_path, tmp_path, effective_date="2026-11-05", getter=getter)

    assert result["completed_total"] == 1
    assert read_state(tmp_path, "candidate_history")[0]["isbn"] == MOONWALK
    for rows in (_read(tmp_path / "scouting-candidates.json"), read_state(tmp_path, "latest_scouting_candidates")):
        row = rows[0]
        assert (row["isbn"], row["isbn_discovery_only"]) == (MOONWALK, True)
        assert row["isbn_provenance"]["method"] == METHOD_BOOK_PAGE
    assert _read(tmp_path / "isbn-completion.json")["completed_total"] == 1


def test_projection_never_replaces_a_snapshot_isbn(tmp_path):
    moonwalk = _record()
    _state(tmp_path, [moonwalk], [moonwalk["identity_key"]])
    seed_state(tmp_path, latest_scouting_candidates=[{"identity_key": moonwalk["identity_key"], "isbn": OROZCO}])
    getter = FakeGetter({"https://nuevoslibros.com/": _book_page("Moonwalk", MOONWALK)})

    run_isbn_completion(tmp_path, tmp_path, effective_date="2026-11-05", getter=getter)

    assert read_state(tmp_path, "latest_scouting_candidates")[0]["isbn"] == OROZCO


def test_a_sealed_feedback_marker_stays_sealed_after_the_isbn_writes(tmp_path):
    # Review finding on #981 round 9.
    from autoscout.feedback_finalization import _marker_matches
    from autoscout.feedback_finalization import _snapshot_digest
    from autoscout.feedback_finalization import _state_guard_digest
    from autoscout.feedback_finalization import _write_marker

    moonwalk = _record()
    _state(tmp_path, [moonwalk], [moonwalk["identity_key"]])
    (tmp_path / "source-mentions.json").write_text("[]", encoding="utf-8")
    _write_marker(tmp_path, _snapshot_digest(tmp_path), _state_guard_digest(open_state_store(tmp_path)))
    getter = FakeGetter({"https://nuevoslibros.com/": _book_page("Moonwalk", MOONWALK)})

    run_isbn_completion(tmp_path, tmp_path, effective_date="2026-11-05", getter=getter)

    assert read_state(tmp_path, "candidate_history")[0]["isbn"] == MOONWALK
    assert _marker_matches(tmp_path, _snapshot_digest(tmp_path), _state_guard_digest(open_state_store(tmp_path)))


def test_a_projection_only_change_keeps_a_sealed_marker_sealed(tmp_path):
    # Review finding on #981 round 10: history already holds the ISBN.
    from autoscout.feedback_finalization import _marker_matches
    from autoscout.feedback_finalization import _snapshot_digest
    from autoscout.feedback_finalization import _state_guard_digest
    from autoscout.feedback_finalization import _write_marker

    moonwalk = _record(isbn=MOONWALK, isbn_discovery_only=True)
    _state(tmp_path, [moonwalk], [moonwalk["identity_key"]])
    (tmp_path / "source-mentions.json").write_text("[]", encoding="utf-8")
    _write_marker(tmp_path, _snapshot_digest(tmp_path), _state_guard_digest(open_state_store(tmp_path)))

    result = run_isbn_completion(tmp_path, tmp_path, effective_date="2026-11-05", getter=FakeGetter())

    assert result["projected"]["current"] == 1
    assert _read(tmp_path / "scouting-candidates.json")[0]["isbn"] == MOONWALK
    assert _marker_matches(tmp_path, _snapshot_digest(tmp_path), _state_guard_digest(open_state_store(tmp_path)))


def test_a_retry_finishes_the_seal_an_interrupted_apply_left_pending(tmp_path, monkeypatch):
    # Review finding on #981 round 13: history written, then the pass died.
    from autoscout import isbn_completion
    from autoscout.feedback_finalization import _marker_matches
    from autoscout.feedback_finalization import _snapshot_digest
    from autoscout.feedback_finalization import _state_guard_digest
    from autoscout.feedback_finalization import _write_marker

    moonwalk = _record()
    _state(tmp_path, [moonwalk], [moonwalk["identity_key"]])
    (tmp_path / "source-mentions.json").write_text("[]", encoding="utf-8")
    _write_marker(tmp_path, _snapshot_digest(tmp_path), _state_guard_digest(open_state_store(tmp_path)))
    getter = FakeGetter({"https://nuevoslibros.com/": _book_page("Moonwalk", MOONWALK)})

    def crash(*args: Any, **kwargs: Any) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(isbn_completion, "project_isbns", crash)
    with pytest.raises(OSError):
        run_isbn_completion(tmp_path, tmp_path, effective_date="2026-11-05", getter=getter)
    monkeypatch.undo()
    assert (tmp_path / ".isbn-completion-seal-pending.json").exists()

    run_isbn_completion(tmp_path, tmp_path, effective_date="2026-11-05", getter=FakeGetter())

    assert _marker_matches(tmp_path, _snapshot_digest(tmp_path), _state_guard_digest(open_state_store(tmp_path)))
    assert not (tmp_path / ".isbn-completion-seal-pending.json").exists()


def test_a_pending_seal_is_void_once_editorial_state_changed(tmp_path, monkeypatch):
    # Review finding on #981 round 14: a Hard Rule confirmed between the
    # interruption and the retry must not be sealed unseen.
    from autoscout import isbn_completion
    from autoscout.feedback_finalization import _marker_matches
    from autoscout.feedback_finalization import _snapshot_digest
    from autoscout.feedback_finalization import _state_guard_digest
    from autoscout.feedback_finalization import _write_marker

    moonwalk = _record()
    _state(tmp_path, [moonwalk], [moonwalk["identity_key"]])
    (tmp_path / "source-mentions.json").write_text("[]", encoding="utf-8")
    _write_marker(tmp_path, _snapshot_digest(tmp_path), _state_guard_digest(open_state_store(tmp_path)))
    getter = FakeGetter({"https://nuevoslibros.com/": _book_page("Moonwalk", MOONWALK)})

    def crash(*args: Any, **kwargs: Any) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(isbn_completion, "project_isbns", crash)
    with pytest.raises(OSError):
        run_isbn_completion(tmp_path, tmp_path, effective_date="2026-11-05", getter=getter)
    monkeypatch.undo()
    seed_state(
        tmp_path,
        editorial_profile={
            "entries": [
                {
                    "id": "taste-0001",
                    "kind": "hard_rule",
                    "scope": "author",
                    "subject": "Some Author",
                    "verbatim_quote": "Этого автора не показывай.",
                    "provenance": "conversation",
                    "created_at": "2026-11-14",
                    "created_by": "editor",
                    "status": "active",
                }
            ]
        },
    )

    run_isbn_completion(tmp_path, tmp_path, effective_date="2026-11-05", getter=FakeGetter())

    assert not _marker_matches(tmp_path, _snapshot_digest(tmp_path), _state_guard_digest(open_state_store(tmp_path)))


def test_a_retry_finishes_the_seal_after_an_interrupted_projection_only_pass(tmp_path, monkeypatch):
    # Review finding on #981 round 15: history already held the ISBN.
    from autoscout import isbn_completion
    from autoscout.feedback_finalization import _marker_matches
    from autoscout.feedback_finalization import _snapshot_digest
    from autoscout.feedback_finalization import _state_guard_digest
    from autoscout.feedback_finalization import _write_marker

    moonwalk = _record(isbn=MOONWALK, isbn_discovery_only=True)
    _state(tmp_path, [moonwalk], [moonwalk["identity_key"]])
    (tmp_path / "source-mentions.json").write_text("[]", encoding="utf-8")
    _write_marker(tmp_path, _snapshot_digest(tmp_path), _state_guard_digest(open_state_store(tmp_path)))
    real_seal = isbn_completion._seal_finalization

    def crash(*args: Any, **kwargs: Any) -> None:
        raise OSError("killed")

    monkeypatch.setattr(isbn_completion, "_seal_finalization", crash)
    with pytest.raises(OSError):
        run_isbn_completion(tmp_path, tmp_path, effective_date="2026-11-05", getter=FakeGetter())
    monkeypatch.setattr(isbn_completion, "_seal_finalization", real_seal)

    run_isbn_completion(tmp_path, tmp_path, effective_date="2026-11-05", getter=FakeGetter())

    assert _marker_matches(tmp_path, _snapshot_digest(tmp_path), _state_guard_digest(open_state_store(tmp_path)))
    assert not (tmp_path / ".isbn-completion-seal-pending.json").exists()


def test_a_pending_seal_is_void_once_the_marker_changed(tmp_path):
    from autoscout.feedback_finalization import _marker_matches
    from autoscout.feedback_finalization import _snapshot_digest
    from autoscout.feedback_finalization import _state_guard_digest
    from autoscout.feedback_finalization import _write_marker

    moonwalk = _record(isbn=MOONWALK, isbn_discovery_only=True)
    _state(tmp_path, [moonwalk], [moonwalk["identity_key"]])
    (tmp_path / "source-mentions.json").write_text("[]", encoding="utf-8")
    (tmp_path / ".isbn-completion-seal-pending.json").write_text(
        json.dumps({"marker": {"snapshot_digest": "old", "state_guard": "old", "finalization_version": 1}}),
        encoding="utf-8",
    )
    _write_marker(tmp_path, "someone-else", "someone-else")

    run_isbn_completion(tmp_path, tmp_path, effective_date="2026-11-05", getter=FakeGetter())

    assert not _marker_matches(tmp_path, _snapshot_digest(tmp_path), _state_guard_digest(open_state_store(tmp_path)))


def test_a_stale_feedback_marker_is_left_stale(tmp_path):
    from autoscout.feedback_finalization import _marker_matches
    from autoscout.feedback_finalization import _snapshot_digest
    from autoscout.feedback_finalization import _state_guard_digest
    from autoscout.feedback_finalization import _write_marker

    moonwalk = _record()
    _state(tmp_path, [moonwalk], [moonwalk["identity_key"]])
    (tmp_path / "source-mentions.json").write_text("[]", encoding="utf-8")
    _write_marker(tmp_path, "stale-digest", "stale-guard")
    getter = FakeGetter({"https://nuevoslibros.com/": _book_page("Moonwalk", MOONWALK)})

    run_isbn_completion(tmp_path, tmp_path, effective_date="2026-11-05", getter=getter)

    assert not _marker_matches(tmp_path, _snapshot_digest(tmp_path), _state_guard_digest(open_state_store(tmp_path)))


def test_history_written_meanwhile_is_kept_and_its_isbns_win(tmp_path):
    # Review finding on #981 round 11: lookups take minutes; feedback or
    # another completion may write history in between.
    moonwalk, bandidos = _record(title="Moonwalk"), _record(title="Bandidos")
    _state(tmp_path, [moonwalk, bandidos], [moonwalk["identity_key"], bandidos["identity_key"]])

    def write_meanwhile() -> None:
        history = {record["title"]: record for record in read_state(tmp_path, "candidate_history")}
        history["Moonwalk"]["editor_status"] = "interested"
        history["Bandidos"]["isbn"] = ASESINO
        seed_state(tmp_path, candidate_history=list(history.values()))

    getter = FakeGetter(
        {
            "https://nuevoslibros.com/libro/alba-moonwalk/": _book_page("Moonwalk", MOONWALK),
            "https://openlibrary.org/": json.dumps(_open_library(title="Bandidos", isbns=(OROZCO,))),
        },
        on_get=write_meanwhile,
    )

    run_isbn_completion(tmp_path, tmp_path, effective_date="2026-11-05", getter=getter)

    history = {record["title"]: record for record in read_state(tmp_path, "candidate_history")}
    assert history["Moonwalk"]["editor_status"] == "interested"
    assert history["Moonwalk"]["isbn"] == MOONWALK
    assert history["Bandidos"]["isbn"] == ASESINO
    assert "isbn_discovery_only" not in history["Bandidos"]


def test_an_isbn_cleared_meanwhile_on_an_untouched_record_stays_cleared(tmp_path):
    # Review finding on #981 round 12: a disagreement cleared a learned ISBN
    # during the lookups; this pass never touched that record.
    learned = _record(title="Bandidos", isbn=ASESINO, isbn_discovery_only=True)
    moonwalk = _record(title="Moonwalk")
    _state(tmp_path, [learned, moonwalk], [learned["identity_key"], moonwalk["identity_key"]])

    def clear_meanwhile() -> None:
        history = read_state(tmp_path, "candidate_history")
        history[0].pop("isbn")
        history[0].pop("isbn_discovery_only")
        seed_state(tmp_path, candidate_history=history)

    getter = FakeGetter({"https://nuevoslibros.com/": _book_page("Moonwalk", MOONWALK)}, on_get=clear_meanwhile)

    run_isbn_completion(tmp_path, tmp_path, effective_date="2026-11-05", getter=getter)

    history = read_state(tmp_path, "candidate_history")
    assert "isbn" not in history[0]
    assert history[1]["isbn"] == MOONWALK


def test_projection_uses_history_as_it_is_after_the_lookups(tmp_path):
    # Review finding on #981 round 16: nothing completed, yet another writer
    # cleared a learned ISBN meanwhile; the snapshots must not get it back.
    learned = _record(title="Bandidos", isbn=ASESINO, isbn_discovery_only=True)
    other = _record(title="Moonwalk")
    _state(tmp_path, [learned, other], [learned["identity_key"], other["identity_key"]])

    def clear_meanwhile() -> None:
        history = read_state(tmp_path, "candidate_history")
        history[0].pop("isbn", None)
        history[0].pop("isbn_discovery_only", None)
        seed_state(tmp_path, candidate_history=history)

    now = [0.0]

    def slow() -> None:
        clear_meanwhile()
        now[0] += 1000.0

    run_isbn_completion(
        tmp_path,
        tmp_path,
        effective_date="2026-11-05",
        getter=FakeGetter(on_get=slow),
        budget_seconds=10,
        monotonic=lambda: now[0],
    )

    assert not read_state(tmp_path, "latest_scouting_candidates")[0].get("isbn")
    assert not _read(tmp_path / "scouting-candidates.json")[0].get("isbn")


def test_dry_run_writes_nothing(tmp_path):
    moonwalk = _record()
    _state(tmp_path, [moonwalk], [moonwalk["identity_key"]])
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    databases = state_databases()
    getter = FakeGetter({"https://nuevoslibros.com/": _book_page("Moonwalk", MOONWALK)})

    result = run_isbn_completion(tmp_path, tmp_path, effective_date="2026-11-05", getter=getter, dry_run=True)

    assert result["completed_total"] == 1
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before
    assert state_databases() == databases


def test_undecided_scope_takes_only_books_without_an_editor_decision(tmp_path):
    undecided = _record(title="Moonwalk")
    decided = _record(title="Bandidos", editor_status="reject")
    _state(tmp_path, [undecided, decided], [])
    getter = FakeGetter({"https://nuevoslibros.com/": _book_page("Moonwalk", MOONWALK)})

    result = run_isbn_completion(tmp_path, tmp_path, effective_date="2026-11-05", scope="undecided", getter=getter)

    assert result["considered"] == 1
    history = {record["title"]: record for record in read_state(tmp_path, "candidate_history")}
    assert history["Moonwalk"]["isbn"] == MOONWALK
    assert "isbn" not in history["Bandidos"]


def test_current_scope_takes_only_this_runs_candidates(tmp_path):
    current = _record(title="Moonwalk")
    older = _record(title="Bandidos")
    _state(tmp_path, [current, older], [current["identity_key"]])

    result = run_isbn_completion(tmp_path, tmp_path, effective_date="2026-11-05", getter=FakeGetter())

    assert result["considered"] == 1


@pytest.mark.json_backend_only("a malformed state file on disk")
def test_invalid_history_fails_its_input_and_writes_nothing(tmp_path):
    # Malformed on disk: the State Store refuses to write it, so the file is written directly.
    (tmp_path / "candidate-history.json").write_text(json.dumps({"not": "a list"}), encoding="utf-8")

    result = run_isbn_completion(tmp_path, tmp_path, effective_date="2026-11-05", getter=None)

    assert result == {"status": "input_failed", "reason": "invalid_candidate_history"}
    assert not (tmp_path / "isbn-completion.json").exists()


# ── Network getter ───────────────────────────────────────────────────────


class _Transport:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def request(self, url: str, **kwargs: Any) -> Any:
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        return self.response


def _http(body: bytes, status: int = 200, content_type: str = "text/html; charset=utf-8", truncated: bool = False):
    return HTTPResponse(
        url="https://example.test/",
        status=status,
        headers={"content-type": content_type},
        body=body,
        truncated=truncated,
    )


def _getter(transport: _Transport, **kwargs: Any) -> BoundedPageGetter:
    return BoundedPageGetter(transport=transport, min_host_interval_seconds=0, **kwargs)


def test_getter_reads_a_page():
    assert _getter(_Transport(_http("Moonwalk ñ".encode()))).get("https://example.test/") == "Moonwalk ñ"


@pytest.mark.parametrize(
    "transport",
    [
        _Transport(_http(b"x", status=404)),
        _Transport(_http(b"x", truncated=True)),
        _Transport(error=BoundedHTTPError("candidate deadline expired")),
        _Transport(error=OSError("refused")),
    ],
)
def test_getter_answers_none_on_any_failure(transport):
    assert _getter(transport).get("https://example.test/") is None


def test_an_unknown_charset_falls_back_to_utf8_instead_of_stopping_the_pass():
    # Review finding on #981: LookupError must not escape the getter.
    transport = _Transport(_http(b"Moonwalk", content_type="text/html; charset=x-unknown-charset"))

    assert _getter(transport).get("https://example.test/") == "Moonwalk"


def test_the_whole_request_runs_under_the_remaining_budget():
    # Review finding on #981: the transport gets a deadline for the whole
    # request (redirects, headers and body), not only a socket timeout.
    now = [100.0]
    transport = _Transport(_http(b"x"))
    getter = _getter(transport, monotonic=lambda: now[0])

    getter.get("https://example.test/", timeout=7.0)

    call = transport.calls[0]
    assert call["timeout_seconds"] == 7.0
    assert call["remaining_seconds"]() == pytest.approx(7.0)
    now[0] += 5.0
    assert call["remaining_seconds"]() == pytest.approx(2.0)


def test_getter_never_extends_its_own_timeout():
    transport = _Transport(_http(b"x"))

    _getter(transport, timeout_seconds=20.0).get("https://example.test/", timeout=900.0)

    assert transport.calls[0]["timeout_seconds"] == 20.0


def test_getter_refuses_non_http_urls():
    transport = _Transport(_http(b"x"))

    assert _getter(transport).get("file:///etc/passwd") is None
    assert transport.calls == []


def test_a_renamed_current_row_with_the_same_durable_id_is_completed_and_projected(tmp_path):
    # Review finding on #981: a translated row keeps its candidate_id while
    # its identity key differs from the history record's.
    record = _record(candidate_id="cand-0001")
    renamed = {"identity_key": "moonwalk (es)|michael jackson", "candidate_id": "cand-0001", "isbn": ""}
    seed_state(tmp_path, candidate_history=[record])
    (tmp_path / "scouting-candidates.json").write_text(json.dumps([renamed]), encoding="utf-8")
    seed_state(tmp_path, latest_scouting_candidates=[renamed])
    getter = FakeGetter({"https://nuevoslibros.com/": _book_page("Moonwalk", MOONWALK)})

    result = run_isbn_completion(tmp_path, tmp_path, effective_date="2026-11-05", getter=getter)

    assert result["considered"] == 1
    assert read_state(tmp_path, "candidate_history")[0]["isbn"] == MOONWALK
    assert read_state(tmp_path, "latest_scouting_candidates")[0]["isbn"] == MOONWALK


# ── Weekly stage and command ─────────────────────────────────────────────


def test_stage_runs_after_the_pipeline_and_before_enrichment():
    assert STAGE_ORDER.index("pipeline") < STAGE_ORDER.index(STAGE_NAME) < STAGE_ORDER.index("enrichment")
    assert stage_message(STAGE_NAME) == "Ищу ISBN новых книг..."


class _RecordingStage:
    def __init__(self, result: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.calls: list[tuple[Path, Path, str]] = []
        self.result = result or {"status": "ok", "completed_total": 0, "records": [{"title": "secret"}]}
        self.error = error

    def complete(
        self, output_dir: Path, state_dir: Path, *, effective_date: str, state_store: Any = None
    ) -> dict[str, Any]:
        self.calls.append((output_dir, state_dir, effective_date))
        if self.error is not None:
            raise self.error
        return self.result


def _stage_log(result: Any) -> list[dict[str, Any]]:
    return [entry for entry in result.log if entry.get("stage") == STAGE_NAME]


def test_weekly_run_calls_the_bound_stage_and_logs_counts_without_titles():
    with tempfile.TemporaryDirectory() as tmp:
        adapters = deterministic_adapters()
        stage = _RecordingStage()
        adapters.isbn_completion = stage
        _output_dir, result = _run_fixture_weekly(Path(tmp), adapters=adapters)

    assert len(stage.calls) == 1
    entries = _stage_log(result)
    assert [entry["status"] for entry in entries] == ["ok"]
    assert "records" not in entries[0]["detail"]
    stages = [entry["stage"] for entry in result.log if entry.get("stage") in STAGE_ORDER]
    assert stages.index("pipeline") < stages.index(STAGE_NAME) < stages.index("enrichment")


def test_a_failing_stage_never_stops_the_weekly_run():
    with tempfile.TemporaryDirectory() as tmp:
        adapters = deterministic_adapters()
        adapters.isbn_completion = _RecordingStage(error=RuntimeError("boom"))
        output_dir, result = _run_fixture_weekly(Path(tmp), adapters=adapters)
        review_exists = (output_dir / "weekly-review.html").exists()

    assert _stage_log(result)[0]["status"] == "failed"
    assert _stage_log(result)[0]["detail"] == {"status": "failed", "exception_type": "RuntimeError"}
    assert review_exists


def test_the_deterministic_weekly_run_has_no_isbn_stage():
    with tempfile.TemporaryDirectory() as tmp:
        _output_dir, result = _run_fixture_weekly(Path(tmp))

    assert _stage_log(result) == []


def test_the_cli_binder_keeps_an_injected_stage():
    # Review finding on #981 round 11: offline tests inject their own stage.
    from autoscout.weekly_workflow import bind_isbn_completion

    adapters = deterministic_adapters()
    injected = ProductionIsbnCompletionStage(getter_factory=lambda: None)
    adapters.isbn_completion = injected
    bind_isbn_completion(adapters)
    assert adapters.isbn_completion is injected

    fresh = deterministic_adapters()
    bind_isbn_completion(fresh)
    assert isinstance(fresh.isbn_completion, ProductionIsbnCompletionStage)


def test_production_stage_publishes_progress_and_uses_its_getter(tmp_path):
    moonwalk = _record()
    _state(tmp_path, [moonwalk], [moonwalk["identity_key"]])
    stage = ProductionIsbnCompletionStage(
        getter_factory=lambda: FakeGetter({"https://nuevoslibros.com/": _book_page("Moonwalk", MOONWALK)})
    )
    presented: list[dict[str, Any]] = []
    stage.set_snapshot_progress_presentation(presented.append)

    result = stage.complete(tmp_path, tmp_path, effective_date="2026-11-05")

    assert result["completed_total"] == 1
    snapshot = _read(tmp_path / "progress-snapshot.json")
    assert snapshot["stage"] == STAGE_NAME
    assert snapshot["status"] == "finished"


def test_command_previews_without_writing_and_applies_with_apply(tmp_path, capsys, monkeypatch):
    moonwalk = _record(evidence_url="https://www.amazon.com/dp/0857197681")
    _state(tmp_path, [moonwalk], [])
    monkeypatch.setattr("autoscout.cli.common.weekly_run_in_progress", lambda state_dir: False)
    before = state_snapshot(tmp_path)

    preview = main(["complete-isbns", "--state-dir", str(tmp_path), "--offline", "--run-date", "2026-11-05"])
    preview_output = capsys.readouterr().out
    unchanged = state_snapshot(tmp_path) == before
    applied = main(["complete-isbns", "--state-dir", str(tmp_path), "--offline", "--apply", "--run-date", "2026-11-05"])

    assert preview == 3 and unchanged
    assert "completed=1" in preview_output and "Moonwalk" not in preview_output
    assert applied == 0
    assert read_state(tmp_path, "candidate_history")[0]["isbn"] == "9780857197689"


def test_the_command_writes_under_the_report_lock(tmp_path, capsys, monkeypatch):
    # Review finding on #981 round 19: two standalone passes must not erase
    # each other's ISBNs.
    from autoscout.weekly_report import WeeklyReportStore

    _state(tmp_path, [_record(evidence_url="https://www.amazon.com/dp/0857197681")], [])
    monkeypatch.setattr("autoscout.cli.common.weekly_run_in_progress", lambda state_dir: False)

    with WeeklyReportStore(tmp_path).transaction("other-writer"):
        code = main(["complete-isbns", "--state-dir", str(tmp_path), "--offline", "--apply"])

    assert code == 6
    assert "reason_code=report_transaction_in_progress" in capsys.readouterr().out
    assert "isbn" not in read_state(tmp_path, "candidate_history")[0]


def test_publish_lock_wraps_the_writes(tmp_path):
    calls: list[str] = []

    @contextlib.contextmanager
    def lock():
        calls.append("enter")
        assert "isbn" not in read_state(tmp_path, "candidate_history")[0]
        yield
        calls.append("exit")
        assert read_state(tmp_path, "candidate_history")[0]["isbn"] == "9780857197689"

    _state(tmp_path, [_record(evidence_url="https://www.amazon.com/dp/0857197681")], [])
    run_isbn_completion(
        tmp_path, tmp_path, effective_date="2026-11-05", getter=None, scope="undecided", publish_lock=lock
    )

    assert calls == ["enter", "exit"]


def test_command_refuses_to_apply_during_a_weekly_run(tmp_path, capsys, monkeypatch):
    _state(tmp_path, [_record()], [])
    monkeypatch.setattr("autoscout.cli.common.weekly_run_in_progress", lambda state_dir: True)

    code = main(["complete-isbns", "--state-dir", str(tmp_path), "--offline", "--apply"])

    assert code == 6
    assert "reason_code=weekly_run_in_progress" in capsys.readouterr().out
