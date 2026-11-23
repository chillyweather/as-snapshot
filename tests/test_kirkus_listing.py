"""The Kirkus coming-soon listing contract (#816): a pre-collected listing, one row per reviewed book."""

import hashlib
import json
import threading
from datetime import date
from pathlib import Path

import pytest

from autoscout import kirkus_listing
from autoscout.kirkus_listing import EVIDENCE_FIELD
from autoscout.kirkus_listing import FAMILY
from autoscout.kirkus_listing import ORIGIN
from autoscout.kirkus_listing import SOURCE_URL
from autoscout.kirkus_listing import KirkusError
from autoscout.kirkus_listing import capture
from autoscout.kirkus_listing import collect
from autoscout.kirkus_listing import feasibility
from autoscout.kirkus_listing import listing_page_url
from autoscout.kirkus_listing import mention_violation
from autoscout.kirkus_listing import page_data
from autoscout.kirkus_listing import parse_listing_page
from autoscout.kirkus_listing import parse_printed_date
from autoscout.kirkus_listing import parse_review_page
from autoscout.kirkus_listing import qualifying_rows
from autoscout.kirkus_listing import read_seen_items
from autoscout.kirkus_listing import validate_bundle
from autoscout.kirkus_listing import validate_collection
from autoscout.kirkus_listing import validate_declaration

FIXTURES = Path(__file__).parent / "fixtures/kirkus"
LISTING = {number: (FIXTURES / f"listing-{number}.html").read_text(encoding="utf-8") for number in (1, 2, 3)}
SOURCE = {
    "name": "Kirkus Reviews Coming Soon Nonfiction",
    "url": SOURCE_URL,
    "family": "media/curated list",
    "contract_family": FAMILY,
    "kirkus_max_listing_pages": 5,
    "kirkus_max_detail_pages": 20,
    "kirkus_request_delay_seconds": 20,
    "kirkus_max_collection_age_days": 8,
    "kirkus_max_review_age_days": 240,
    "kirkus_genre_labels": ["NONFICTION", "BIOGRAPHY & MEMOIR", "HISTORY", "BUSINESS"],
    "language": "en",
    "eligible_candidate_category": "adult nonfiction",
    "expected_signal_strength": "minor mention",
}
REFERENCE = date(2026, 10, 13)


def review_url(path):
    return f"{ORIGIN}/book-reviews/{path}/"


BULLWHIP_URL = review_url("kamau-bobb/the-bullwhips-algorithm")
CAPITAL_URL = review_url("rene-sonneveld/invisible-capital")
# The two review pages kept as served, without their prose.
SERVED = {
    BULLWHIP_URL: (FIXTURES / "review-the-bullwhips-algorithm.html").read_text(encoding="utf-8"),
    CAPITAL_URL: (FIXTURES / "review-invisible-capital.html").read_text(encoding="utf-8"),
}
# The facts the other ten review pages printed on 13 October 2026: ISBN, publisher, dates, review program.
FACTS = {
    review_url("lawrence-david-rose/the-determinists-harris-sapolsky-hossenfelder-and-the-free-will-debate"): (
        "",
        "None",
        "Oct. 15, 2026",
        "Sept. 09, 2026",
        "",
    ),
    review_url("melissa-h-cronin/the-peach"): (
        "9781955194563",
        "Mnemosyne Books",
        "Oct. 15, 2026",
        "Sept. 21, 2026",
        "Kirkus Indie",
    ),
    review_url("m-faraday/love-and-lies-along-the-language-barrier"): (
        "9781835566909",
        "None",
        "Oct. 18, 2026",
        "Sept. 04, 2026",
        "",
    ),
    review_url("laura-cunningham/a-state-of-change"): (
        "9781597147323",
        "Heyday",
        "Oct. 20, 2026",
        "Sept. 11, 2026",
        "",
    ),
    review_url(
        "paula-caligiuri/dating-again-field-tested-science-backed-secrets-for-finding-love-and-sanity-in-your-second-act"
    ): ("9798997243104", "Porta Terra Press", "Oct. 20, 2026", "Sept. 10, 2026", "Kirkus Indie"),
    review_url("ethan-mollick/co-existence"): ("9798217181391", "Portfolio", "Oct. 20, 2026", "Aug. 31, 2026", ""),
    review_url("masha-gessen/the-best-american-essays-2026"): (
        "9780063454446",
        "Mariner Books",
        "Oct. 20, 2026",
        "Sept. 14, 2026",
        "",
    ),
    review_url("jay-parini/paradise-now-2"): (
        "9798898950071",
        "Morehouse Publishing",
        "Oct. 20, 2026",
        "Aug. 17, 2026",
        "",
    ),
    review_url("paris-marx/hyperscale"): ("9798217048526", "Riverhead", "Oct. 20, 2026", "Aug. 17, 2026", ""),
    review_url("kit-chapman/the-age-of-alchemy"): (
        "978-1778404177",
        "Greystone Books",
        "Oct. 20, 2026",
        "July 06, 2026",
        "",
    ),
}
LISTED = [row for number in (1, 2) for row in parse_listing_page(LISTING[number])]
TITLES = {row["review_url"]: row["title"] for row in LISTED}
ACCEPTED_ISBNS = [
    "9780807023433",
    "9781597147323",
    "9798217181391",
    "9780063454446",
    "9798898950071",
    "9798217048526",
    "9781778404177",
]
BULLWHIP = {
    "title": "THE BULLWHIP'S ALGORITHM",
    "primary_authors": ["Kamau Bobb"],
    "publisher": "Beacon Press",
    "publication_year": 2026,
    "isbn": "9780807023433",
    "source_week": "2026-09-25",
    "source_url": SOURCE_URL,
    "evidence_url": BULLWHIP_URL,
    "signal_type": "media/curated list",
    "signal_strength": "minor mention",
    "candidate_category": "adult nonfiction",
}
NOTE = {"evidence_note": "Reviewed book row on the captured coming-soon listing."}


def review_page(url, facts=None):
    """A review page in the served shape: the title heading, then labelled paragraphs, then a second review."""
    if facts is None and url in SERVED:
        return SERVED[url]
    isbn, publisher, published, reviewed, program = facts or FACTS[url]
    program_block = (
        f'<p class="article-category"><span class="cat-genre"><b>Review Program:</b></span>'
        f'<a href="/indie-reviews/">{program}</a></p>'
        if program
        else ""
    )
    return (
        '<html><body><div id="review-body" class="container book-description book-description-first">'
        f'<h1 class="article-title">{TITLES[url].replace("&", "&amp;")}</h1>'
        '<p class="description-title kirkus-title">Verdict sentence of the review.</p>'
        '<div class="first-alphabet book-content text-left"><p>Review text.</p></div>'
        f'<div class="row content-row mt-5"><p><b>Pub Date:</b> {published}</p><p><b>ISBN:</b> {isbn}</p>'
        f"<p><b>Page Count:</b> 240</p><p><b>Publisher:</b> {publisher}</p>"
        f"<p><b>Review Posted Online:</b> {reviewed}</p><p><b>Kirkus Reviews Issue:</b> Nov. 1, 2026</p></div>"
        f"{program_block}</div>"
        '<div id="review-body" class="container book-description book-description-pagination">'
        '<h1 class="article-title">ANOTHER BOOK</h1><p><b>Pub Date:</b> Jan. 05, 2021</p>'
        "<p><b>ISBN:</b> 9780000000002</p><p><b>Publisher:</b> Another House</p>"
        "<p><b>Review Posted Online:</b> Dec. 01, 2020</p></div></body></html>"
    )


def site(listing=None, pages=None, refuse=()):
    """A fetch function over the fixtures, the requests it saw, and the pauses kept between them."""
    requests, pauses = [], []
    listing = LISTING if listing is None else listing

    def fetch(url):
        requests.append(url)
        if url in refuse:
            return {"body": "", "status": 429, "resolved_url": url}
        for number, body in listing.items():
            if url == listing_page_url(number):
                return {"body": body, "status": 200, "resolved_url": url}
        return {"body": (pages or {}).get(url) or review_page(url), "status": 200, "resolved_url": url}

    return fetch, requests, pauses


def collected(source=None, reference=REFERENCE, previous=None, **kwargs):
    fetch, requests, pauses = site(**kwargs)
    return collect(source or SOURCE, reference, fetch, pauses.append, previous), requests, pauses


def captured(source=None, reference=REFERENCE, seen=None, **kwargs):
    collection, _, _ = collected(source, **kwargs)
    return capture(source or SOURCE, reference, collection, seen)


def by_isbn(rows):
    return {row["isbn"]: row for row in rows}


def by_url(collection):
    return {row["review_url"]: row for row in collection["rows"]}


def test_a_pass_reads_the_listing_to_its_empty_page_then_each_unopened_review_page():
    collection, requests, pauses = collected()
    assert requests[:3] == [listing_page_url(1), listing_page_url(2), listing_page_url(3)]
    assert requests[0] == SOURCE_URL + "?order=1" and requests[1] == SOURCE_URL + "?order=1&page=2"
    assert requests[3:] == [row["review_url"] for row in LISTED]
    # The declared delay is kept before every request after the first.
    assert pauses == [20.0] * (len(requests) - 1)
    assert [page["row_count"] for page in collection["listing_pages"]] == [6, 6, 0]
    assert len(collection["rows"]) == 12 and collection["stop_reason"] == ""


def test_no_disallowed_url_is_requested():
    _, requests, _ = collected()
    for url in requests:
        path = url[len(ORIGIN) :]
        assert path.startswith("/discover-books/nonfiction") or path.startswith("/book-reviews/")
        assert not any(
            path.startswith(prefix)
            for prefix in (
                "/book-reviews/non-fiction/",
                "/book-reviews/indie/",
                "/book-reviews/stars/",
                "/book-reviews/fiction-books-literature/",
                "/book-reviews/childrens-books/",
                "/search/",
            )
        )


def test_identity_fields_are_grounded_from_the_card_and_the_review_page():
    rows = by_isbn(validate_bundle(captured(), SOURCE))
    assert list(rows) == ACCEPTED_ISBNS
    bullwhip = rows["9780807023433"]
    assert {field: bullwhip[field] for field in BULLWHIP if field in bullwhip} == {
        "title": "THE BULLWHIP'S ALGORITHM",
        "primary_authors": ["Kamau Bobb"],
        "publisher": "Beacon Press",
        "publication_year": 2026,
        "isbn": "9780807023433",
        "source_week": "2026-09-25",
        "evidence_url": BULLWHIP_URL,
    }
    assert bullwhip["publication_date"] == "2026-10-20" and bullwhip["genre_label"] == "NONFICTION"
    # The source prints every title in capitals, and the title is kept as printed.
    assert all(row["title"] == row["title"].upper() for row in rows.values())
    assert rows["9781597147323"]["starred"] is True and rows["9781597147323"]["genre_label"] == "HISTORY"
    # A hyphenated ISBN is the same ISBN; an editor named in the place of an author is the primary author.
    assert rows["9781778404177"]["title"] == "THE AGE OF ALCHEMY"
    assert rows["9780063454446"]["primary_authors"] == ["M. Gessen"]


def test_self_published_and_paid_reviews_are_rejected_by_page_evidence():
    collection, _, _ = collected()
    rejections = {row["title"]: row["rejection"] for row in collection["rows"] if row["rejection"]}
    assert rejections == {
        "THE DETERMINISTS": "missing_isbn",
        "THE PEACH": "paid_review",
        "LOVE AND LIES ALONG THE LANGUAGE BARRIER": "self_published",
        "INVISIBLE CAPITAL": "paid_review",
        "DATING (AGAIN)": "paid_review",
    }
    assert all(row["detail"] is None and row["page_fetched_date"] for row in collection["rows"] if row["rejection"])


@pytest.mark.parametrize(
    ("facts", "reason"),
    [
        (("9780807023434", "Beacon Press", "Oct. 20, 2026", "Sept. 25, 2026", ""), "invalid_isbn"),
        (("9780807023433", "", "Oct. 20, 2026", "Sept. 25, 2026", ""), "self_published"),
        (("9780807023433", "Beacon Press", "Oct. 21, 2026", "Sept. 25, 2026", ""), "publication_date_mismatch"),
        (("9780807023433", "Beacon Press", "today", "Sept. 25, 2026", ""), "publication_date_mismatch"),
        (("9780807023433", "Beacon Press", "Oct. 20, 2026", "last week", ""), "missing_review_date"),
        (("9780807023433", "Beacon Press", "Oct. 20, 2026", "Sept. 25, 2026", "Kirkus Indie"), "paid_review"),
    ],
)
def test_a_review_page_without_grounded_facts_rejects_the_row_and_guesses_nothing(facts, reason):
    row = next(row for row in LISTED if row["review_url"] == BULLWHIP_URL)
    assert parse_review_page(review_page(BULLWHIP_URL, facts), row, REFERENCE) == (None, reason)


@pytest.mark.parametrize(("printed", "expected"), [("today", "2026-10-13"), ("Yesterday", "2026-10-12")])
def test_a_review_posted_today_or_yesterday_is_dated_from_the_day_its_page_was_opened(printed, expected):
    # Kirkus prints `Review Posted Online: today` on the day it posts a review (#886).
    row = next(row for row in LISTED if row["review_url"] == BULLWHIP_URL)
    facts = ("9780807023433", "Beacon Press", "Oct. 20, 2026", printed, "")
    detail, reason = parse_review_page(review_page(BULLWHIP_URL, facts), row, REFERENCE)
    assert reason == "" and detail["review_date"] == expected


def test_only_the_reviewed_book_of_a_review_page_is_read():
    row = next(row for row in LISTED if row["review_url"] == BULLWHIP_URL)
    detail, reason = parse_review_page(SERVED[BULLWHIP_URL], row, REFERENCE)
    assert reason == "" and detail == {
        "isbn": "9780807023433",
        "publisher": "Beacon Press",
        "publication_date": "2026-10-20",
        "review_date": "2026-09-25",
    }
    # The served page carries a second review with its own facts; they are not this book's.
    assert SERVED[BULLWHIP_URL].count('<h1 class="article-title">') == 2
    with pytest.raises(KirkusError) as error:
        parse_review_page(SERVED[CAPITAL_URL], row, REFERENCE)
    assert error.value.reason_code == "kirkus_review_page_shape"
    with pytest.raises(KirkusError):
        parse_review_page(SERVED[BULLWHIP_URL].replace("<b>ISBN:</b>", "<b>EAN:</b>", 1), row, REFERENCE)


def _bullwhip_card_changed(old, new):
    page = LISTING[1]
    start = page.index("/book-reviews/kamau-bobb/the-bullwhips-algorithm/")
    card_start = page.rindex('<div class="card feature-review-card', 0, start)
    card_end = page.index('<div class="card feature-review-card', start)
    changed = page[:card_start] + page[card_start:card_end].replace(old, new, 1) + page[card_end:]
    assert changed != page
    return {**LISTING, 1: changed}


BULLWHIP_CARD_DATE = '<span class="card-title-no-hover db-pub-date">October 20, 2026</span>'


@pytest.mark.parametrize(("old", "new"), [("by\n", "translated by\n"), ("Kamau Bobb", "")])
def test_a_card_without_a_grounded_author_is_rejected_and_never_opened(old, new):
    collection, requests, _ = collected(listing=_bullwhip_card_changed(old, new))
    row = by_url(collection)[BULLWHIP_URL]
    assert row["rejection"] == "author_line"
    assert row["detail"] is None and row["page_fetched_date"] == "" and BULLWHIP_URL not in requests


def test_a_card_without_a_date_is_opened_and_takes_the_date_its_review_page_prints():
    # Kirkus lists a freshly posted review before its card prints the date (#913).
    collection, requests, _ = collected(listing=_bullwhip_card_changed(BULLWHIP_CARD_DATE, ""))
    row = by_url(collection)[BULLWHIP_URL]
    assert BULLWHIP_URL in requests
    assert row["card_publication_date"] == "" and row["rejection"] == ""
    assert row["detail"]["publication_date"] == "2026-10-20"
    assert row["detail"]["isbn"] == "9780807023433"


def test_a_book_without_a_date_on_card_or_page_is_rejected_and_opened_again_next_pass():
    listing = _bullwhip_card_changed(BULLWHIP_CARD_DATE, "")
    dateless_page = SERVED[BULLWHIP_URL].replace("<b>Pub Date:</b> Oct. 20, 2026", "<b>Pub Date:</b> ", 1)
    assert dateless_page != SERVED[BULLWHIP_URL]
    first, _, _ = collected(listing=listing, pages={BULLWHIP_URL: dateless_page})
    row = by_url(first)[BULLWHIP_URL]
    assert row["rejection"] == "missing_page_publication_date" and row["detail"] is None
    assert row["page_fetched_date"] == REFERENCE.isoformat()

    second, requests, _ = collected(listing=listing, previous=first)
    assert BULLWHIP_URL in requests
    assert by_url(second)[BULLWHIP_URL]["detail"]["publication_date"] == "2026-10-20"


def test_a_dated_card_still_needs_the_same_date_on_its_review_page():
    changed = SERVED[BULLWHIP_URL].replace("<b>Pub Date:</b> Oct. 20, 2026", "<b>Pub Date:</b> Oct. 27, 2026", 1)
    collection, _, _ = collected(pages={BULLWHIP_URL: changed})
    assert by_url(collection)[BULLWHIP_URL]["rejection"] == "publication_date_mismatch"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("October 15, 2026", date(2026, 10, 15)),
        ("Oct. 20, 2026", date(2026, 10, 20)),
        ("Sept. 04, 2026", date(2026, 9, 4)),
        ("July 06, 2026", date(2026, 7, 6)),
        ("today", None),
        ("Ju. 06, 2026", None),
        ("Feb. 30, 2026", None),
        ("Oct. 20, 2026 and later", None),
    ],
)
def test_a_printed_date_is_read_only_in_its_full_form(text, expected):
    assert parse_printed_date(text) == expected


def test_no_review_prose_is_retained():
    bundle = captured()
    retained = json.dumps(page_data(bundle, SOURCE))
    assert "Verdict sentence" not in retained and "Review text" not in retained
    assert "ANOTHER BOOK" not in retained and "GOOD ECONOMICS" not in retained
    assert set(bundle["collection"]["rows"][0]) == {
        "review_url",
        "title",
        "authors",
        "genre_label",
        "starred",
        "card_publication_date",
        "rejection",
        "page_fetched_date",
        "page_sha256",
        "detail",
    }


def test_a_review_page_is_opened_once_across_passes():
    first, _, _ = collected()
    second, requests, _ = collected(reference=date(2026, 10, 20), previous=first)
    assert requests == [listing_page_url(number) for number in (1, 2, 3)]
    assert second["collected_date"] == "2026-10-20"
    assert by_url(second)[BULLWHIP_URL]["page_fetched_date"] == "2026-10-13"
    assert by_url(second)[CAPITAL_URL]["rejection"] == "paid_review"
    assert [row["detail"] for row in second["rows"]] == [row["detail"] for row in first["rows"]]


def test_a_review_page_without_a_posting_date_is_opened_again_by_the_next_pass():
    # A review page that printed no posting date yet may print one later, so its rejection is not kept (#886).
    undated = {BULLWHIP_URL: review_page(BULLWHIP_URL, ("9780807023433", "Beacon Press", "Oct. 20, 2026", "", ""))}
    first, _, _ = collected(pages=undated)
    assert by_url(first)[BULLWHIP_URL]["rejection"] == "missing_review_date"
    second, requests, _ = collected(reference=date(2026, 10, 20), previous=first)
    assert requests[3:] == [BULLWHIP_URL]
    assert by_url(second)[BULLWHIP_URL]["page_fetched_date"] == "2026-10-20"
    assert by_url(second)[BULLWHIP_URL]["detail"]["review_date"] == "2026-09-25"
    # Every other opened row keeps the result of the first pass, a rejected one included.
    assert by_url(second)[CAPITAL_URL] == by_url(first)[CAPITAL_URL]


def test_the_declared_cap_leaves_the_remaining_review_pages_for_the_next_pass():
    capped = {**SOURCE, "kirkus_max_detail_pages": 4}
    first, requests, _ = collected(capped)
    assert requests[3:] == [row["review_url"] for row in LISTED[:4]]
    assert sum(not row["page_fetched_date"] for row in first["rows"]) == 8
    # An unopened row carries no facts, so the extractor is not given it.
    assert [row["isbn"] for row in validate_bundle(capture(capped, REFERENCE, first), capped)] == ACCEPTED_ISBNS[:1]
    second, requests, _ = collected(capped, previous=first)
    assert requests[3:] == [row["review_url"] for row in LISTED[4:8]]
    assert sum(not row["page_fetched_date"] for row in second["rows"]) == 4


def test_a_refused_review_page_ends_the_pass_and_keeps_what_was_opened():
    refused = LISTED[4]["review_url"]
    collection, requests, _ = collected(refuse={refused})
    assert requests[-1] == refused and collection["stop_reason"] == "kirkus_http"
    assert [bool(row["page_fetched_date"]) for row in collection["rows"]] == [True] * 4 + [False] * 8
    # Nothing is retried, and the next pass opens the refused page and the ones after it.
    again, requests, _ = collected(previous=collection)
    assert requests[3:] == [row["review_url"] for row in LISTED[4:]] and again["stop_reason"] == ""


def test_a_pass_cut_short_with_nothing_new_is_a_named_refusal_not_a_quiet_week(tmp_path):
    import autoscout.source_collectors.agent_fetch as agent
    from autoscout.source_collectors.base import CollectorRunError

    # The site refuses the first review page: the pass is valid, and it holds no row to report.
    empty, _, _ = collected(refuse={LISTED[0]["review_url"]})
    assert empty["stop_reason"] == "kirkus_http" and kirkus_listing.waiting_row_count(empty) == 12
    with pytest.raises(KirkusError) as error:
        capture(SOURCE, REFERENCE, empty)
    assert error.value.reason_code == "kirkus_partial_collection"
    config = tmp_path / "configs" / "live.json"
    with pytest.raises(CollectorRunError) as refused:
        agent.fetch_agent_source_page(config, tmp_path, _live_source(tmp_path, empty), reference_date=REFERENCE)
    assert refused.value.debug_metadata["safe_failure_status"] == "kirkus_partial_collection"

    # A pass cut short after it reached rows reports them, and the caveat stays in the diagnostics.
    partial, _, _ = collected(refuse={LISTED[4]["review_url"]})
    page, debug = agent.fetch_agent_source_page(
        config, tmp_path, _live_source(tmp_path, partial), reference_date=REFERENCE
    )
    assert len(json.loads(page["text"])) == 1
    assert debug["kirkus_stop_reason"] == "kirkus_http" and debug["kirkus_rows_waiting"] == 8
    # The shortfall is recorded as every incomplete source records it, so the weekly summary names it.
    warning = "after 4 of 12 listed books"
    assert debug["incomplete_ranking"] is True and warning in debug["incomplete_ranking_warning"]
    assert (debug["raw_row_count"], debug["expected_raw_row_count"]) == (4, 12)
    from autoscout.source_collectors.base import validate_extraction_page_identity
    from autoscout.source_page_identity import incomplete_coverage_from_run
    from autoscout.weekly_workflow import _source_problems_from_capture

    replayed = validate_extraction_page_identity(SOURCE, page, collector_kind="codex-capture", run_date="2026-10-13")
    assert warning in replayed["incomplete_ranking_warning"]
    run = {"source_name": SOURCE["name"], "status": "codex-captured", "reason": "", "debug_metadata": debug}
    assert incomplete_coverage_from_run(run) == {
        "source_name": SOURCE["name"],
        "observed_rows": 4,
        "expected_rows": 12,
        # The book the opened pages left out is named, not only counted (#913).
        "left_out_titles": ["THE DETERMINISTS"],
    }
    problems = _source_problems_from_capture({"source_runs": [run]})
    assert [problem["status"] for problem in problems] == ["incomplete"]
    # A pass that only reached its declared cap is not a shortfall.
    capped, _, _ = collected({**SOURCE, "kirkus_max_detail_pages": 4})
    assert kirkus_listing.partial_collection_debug(page_data(capture(SOURCE, REFERENCE, capped), SOURCE)) == {}
    assert kirkus_listing.partial_collection_debug({}) == {}
    # The same partial pass with its one row already reported is a refusal again.
    with pytest.raises(KirkusError):
        capture(SOURCE, REFERENCE, partial, {"9780807023433": "2026-10-06"})


def test_a_refused_listing_page_fails_the_pass():
    with pytest.raises(KirkusError) as error:
        collected(refuse={listing_page_url(2)})
    assert error.value.reason_code == "kirkus_http"


@pytest.mark.parametrize(
    ("status", "truncated", "resolved", "reason"),
    [
        (503, False, None, "kirkus_http"),
        (200, True, None, "kirkus_response_bounds"),
        (200, False, ORIGIN + "/discover-books/", "kirkus_wrong_target"),
    ],
)
def test_transport_refusal_stops_requests(status, truncated, resolved, reason):
    requests = []

    def fetch(url):
        requests.append(url)
        return {"body": LISTING[1], "status": status, "resolved_url": resolved or url, "truncated": truncated}

    with pytest.raises(KirkusError) as error:
        collect(SOURCE, REFERENCE, fetch, lambda seconds: None)
    assert error.value.reason_code == reason and requests == [listing_page_url(1)]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda page: page.replace("Discover Books</h1>", "Find Books</h1>"), "kirkus_page_shape"),
        (
            lambda page: page.replace('<option value="1"  selected >', '<option value="1" >').replace(
                '<option value="2" >', '<option value="2" selected >'
            ),
            "kirkus_page_shape",
        ),
        (lambda page: page.replace('class="card-text"', 'class="card-link"', 1), "kirkus_page_shape"),
        (lambda page: LISTING[3], "kirkus_page_shape"),
    ],
)
def test_listing_page_shape_drift_fails_closed(change, reason):
    with pytest.raises(KirkusError) as error:
        collected(listing={**LISTING, 1: change(LISTING[1])})
    assert error.value.reason_code == reason


def test_a_listing_longer_than_the_declared_cap_fails():
    with pytest.raises(KirkusError) as error:
        collected({**SOURCE, "kirkus_max_listing_pages": 2})
    assert error.value.reason_code == "kirkus_listing_cap"


def test_one_isbn_for_two_reviews_fails():
    twin = review_url("paris-marx/hyperscale")
    same = ("9780807023433", "Riverhead", "Oct. 20, 2026", "Aug. 17, 2026", "")
    with pytest.raises(KirkusError) as error:
        collected(pages={twin: review_page(twin, same)})
    assert error.value.reason_code == "kirkus_row_identity"


def test_a_run_reads_the_last_pass_and_refuses_a_missing_or_old_one():
    collection, _, _ = collected()
    with pytest.raises(KirkusError) as error:
        capture(SOURCE, REFERENCE, None)
    assert error.value.reason_code == "kirkus_collection_missing"
    assert len(validate_bundle(capture(SOURCE, date(2026, 10, 21), collection), SOURCE)) == 7
    # A pass made on a machine whose date is one day ahead is not a pass from the future.
    capture(SOURCE, date(2026, 10, 12), collection)
    for reference in (date(2026, 10, 22), date(2026, 10, 11)):
        with pytest.raises(KirkusError) as error:
            capture(SOURCE, reference, collection)
        assert error.value.reason_code == "kirkus_stale_collection"


def test_declared_genre_labels_and_the_review_age_limit_bound_the_rows():
    rows = validate_bundle(captured(), SOURCE)
    assert [row["isbn"] for row in qualifying_rows(rows, SOURCE)] == ACCEPTED_ISBNS
    without_history = {**SOURCE, "kirkus_genre_labels": ["Nonfiction", "Business"]}
    assert "9781597147323" not in by_isbn(qualifying_rows(rows, without_history))
    assert len(qualifying_rows(rows, without_history)) == 6
    # The oldest review of the sample was posted 99 days before the pass.
    assert by_isbn(rows)["9781778404177"]["review_age_days"] == 99
    recent = {**SOURCE, "kirkus_max_review_age_days": 98}
    assert [row["isbn"] for row in qualifying_rows(rows, recent)] == ACCEPTED_ISBNS[:-1]


def test_a_book_seen_by_an_earlier_run_is_not_reported_again():
    bundle = captured(seen={"9780807023433": "2026-10-06", "9780000000002": "2026-10-06"})
    assert bundle["seen_items"] == [{"isbn": "9780807023433", "first_seen_date": "2026-10-06"}]
    rows = validate_bundle(bundle, SOURCE)
    assert by_isbn(rows)["9780807023433"]["seen"] is True
    assert [row["isbn"] for row in qualifying_rows(rows, SOURCE)] == ACCEPTED_ISBNS[1:]
    # A book first reported today is shown again, so a second run on the same date loses nothing.
    same_day = captured(seen={"9780807023433": "2026-10-13"})
    assert same_day["seen_items"] == []


@pytest.mark.parametrize("state", ["{", "[]", '{"seen_guids": [{"guid": "123", "first_seen_date": "2026-10-06"}]}'])
def test_malformed_seen_state_fails_closed(tmp_path, state):
    path = tmp_path / "seen.json"
    path.write_text(state)
    with pytest.raises(KirkusError) as error:
        read_seen_items(path)
    assert error.value.reason_code == "kirkus_seen_state"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda bundle: bundle.update(version=2),
        lambda bundle: bundle.update(source_url="https://example.test/"),
        lambda bundle: bundle["collection"].update(collected_date="2026-13-40"),
        lambda bundle: bundle["collection"]["listing_pages"].pop(),
        lambda bundle: bundle["collection"]["rows"][3]["detail"].update(isbn="9780807023434"),
        lambda bundle: bundle["collection"]["rows"][3].update(title=""),
        lambda bundle: bundle["collection"]["rows"][3].update(review_text="copied prose"),
        lambda bundle: bundle["collection"]["rows"][0].update(detail={"isbn": "9780807023433"}),
        lambda bundle: bundle["collection"].update(stop_reason="tired"),
        lambda bundle: bundle["seen_items"].append({"isbn": "9780807023433", "first_seen_date": "2026-10-13"}),
    ],
)
def test_replay_identity_fails(mutation):
    bundle = captured()
    mutation(bundle)
    with pytest.raises(KirkusError) as error:
        validate_bundle(bundle, SOURCE)
    assert error.value.reason_code == "kirkus_evidence_identity"


@pytest.mark.parametrize(
    "change",
    [
        {"url": "https://www.kirkusreviews.com/book-reviews/"},
        {"kirkus_max_listing_pages": 41},
        {"kirkus_max_listing_pages": True},
        {"kirkus_max_detail_pages": 0},
        {"kirkus_request_delay_seconds": 19},
        {"kirkus_max_collection_age_days": 15},
        {"kirkus_max_review_age_days": 0},
        {"kirkus_genre_labels": []},
        {"kirkus_genre_labels": ["NONFICTION", " "]},
        {"kirkus_genre_labels": "NONFICTION"},
    ],
)
def test_declaration_is_explicit(change):
    from autoscout.config import validate_config

    with pytest.raises(KirkusError) as error:
        validate_declaration({**SOURCE, **change})
    assert error.value.reason_code == "kirkus_declaration"
    assert any("Kirkus listing" in message for message in validate_config({"sources": [{**SOURCE, **change}]}))


def test_mention_must_copy_one_qualifying_row_exactly():
    page = page_data(captured(), SOURCE)
    assert mention_violation(BULLWHIP, page, SOURCE) == ""
    assert mention_violation({**BULLWHIP, **NOTE, "english_title": BULLWHIP["title"]}, page, SOURCE) == ""
    for change in (
        {"title": "The Bullwhip's Algorithm"},
        {"primary_authors": ["Kamau Bobb", "Someone Else"]},
        {"publisher": "Beacon"},
        {"publication_year": "2026"},
        {"isbn": "9781597147323"},
        {"source_week": "2026-10-13"},
        {"source_url": BULLWHIP_URL},
        {"evidence_url": CAPITAL_URL},
        {"signal_strength": "strong"},
        {"candidate_category": "fiction"},
    ):
        assert "does not agree" in mention_violation({**BULLWHIP, **change}, page, SOURCE), change
    assert "content_description" in mention_violation({**BULLWHIP, "content_description": "x"}, page, SOURCE)
    # A rejected row and a seen row are not rows a mention may stand for.
    seen_page = page_data(captured(seen={"9780807023433": "2026-10-06"}), SOURCE)
    assert "does not agree" in mention_violation(BULLWHIP, seen_page, SOURCE)


def test_feasibility_reports_the_review_window_and_grounded_samples():
    page = page_data(captured(), SOURCE)
    verdict = feasibility(page, SOURCE)
    assert verdict.accepted and verdict.contract_family == FAMILY
    assert verdict.raw_row_count == 12 and verdict.bounded_entry_count == 7
    assert verdict.reporting_period == "2026-07-06/2026-09-25"
    assert [sample.identifier for sample in verdict.samples] == ACCEPTED_ISBNS[:3]
    assert verdict.samples[0].title == "THE BULLWHIP'S ALGORITHM" and verdict.samples[0].url == BULLWHIP_URL
    from autoscout.source_feasibility import evaluate_source_feasibility

    assert evaluate_source_feasibility(SOURCE, page) == verdict
    assert not feasibility({**page, "source_page": "https://example.test/"}, SOURCE).accepted


def test_extraction_contract_asks_for_every_row_and_never_for_a_title_correction():
    from autoscout.source_collectors.agent_model_input import model_bounded_page_data
    from autoscout.source_collectors.contract import agent_extraction_prompt
    from autoscout.source_collectors.contract import extraction_contract

    page = page_data(captured(), SOURCE)
    contract = extraction_contract(SOURCE, source_page=SOURCE_URL, capture_file="capture.json")
    rules = " ".join(contract["rules"])
    assert "return every row" in rules and "never change its capitalisation" in rules
    assert "never correct, complete, translate, reorder or merge them" in rules
    assert "its text field is the complete list of rows to return, one row per book" in rules
    assert "html field is the authoritative" not in rules
    # The shared completeness and empty-result rules stay verbatim for every configured contract (#740).
    assert "Return every row the page shows that meets the required fields" in rules
    shape = contract["required_output_shape"]["entries"][0]
    assert shape["signal_strength"] == "minor mention" and "content_description" not in shape
    assert {"isbn", "source_week", "publisher", "publication_year"} <= set(contract["required_fields"])

    model, _ = model_bounded_page_data(page, 120000, source=SOURCE)
    model_rows = json.loads(model["text"])
    assert len(model_rows) == 7 and EVIDENCE_FIELD not in model and model["html"] == ""
    assert set(model_rows[0]) == {
        "title",
        "primary_authors",
        "publisher",
        "publication_year",
        "isbn",
        "source_week",
        "evidence_url",
    }
    prompt = agent_extraction_prompt(SOURCE, page, 120000)
    assert BULLWHIP_URL in prompt and "INVISIBLE CAPITAL" not in prompt and "list-page HTML" not in prompt


def test_extraction_keeps_exact_rows_and_refuses_a_recased_title():
    from autoscout.source_collectors.agent_mentions import agent_extraction_mentions_from_response

    page = page_data(captured(), SOURCE)
    recased = {**BULLWHIP, **NOTE, "title": "The Bullwhip's Algorithm"}
    response = {"mentions": [{**BULLWHIP, **NOTE}, recased]}
    mentions, dropped = agent_extraction_mentions_from_response(page, response, SOURCE, "2026-10-13")
    assert [mention["title"] for mention in mentions] == ["THE BULLWHIP'S ALGORITHM"]
    assert len(dropped) == 1


def test_artifact_validators_verify_a_captured_review_link():
    from autoscout.source_collectors import evidence_url_verified

    page = page_data(captured(), SOURCE)
    assert evidence_url_verified(page, BULLWHIP_URL) == (True, "")
    assert not evidence_url_verified(page, review_url("someone/else"))[0]
    assert not evidence_url_verified({**page, "contract_family": "generic"}, BULLWHIP_URL)[0]


def test_empty_extraction_from_a_capture_with_rows_is_refused(tmp_path):
    from autoscout.extraction_completeness import capture_listing_row_count
    from autoscout.observed_extraction import validate_extraction_artifact
    from autoscout.source_collectors.contract import extraction_contract

    page = page_data(captured(), SOURCE)
    contract = extraction_contract(SOURCE, source_page=SOURCE_URL, capture_file="capture.json")
    assert capture_listing_row_count(contract, page) == 7
    assert capture_listing_row_count(contract, {**page, EVIDENCE_FIELD: {}}) is None
    capture_path = tmp_path / "capture.json"
    capture_path.write_text(json.dumps(page))
    item = {
        **contract,
        "operation_id": "operation-1",
        "capture_sha256": hashlib.sha256(capture_path.read_bytes()).hexdigest(),
    }
    artifact = tmp_path / "extraction.json"
    artifact.write_text(
        json.dumps(
            {k: item[k] for k in ("source_name", "source_url", "operation_id", "capture_sha256")} | {"entries": []}
        )
    )
    observation = validate_extraction_artifact(artifact, item, capture_path)
    assert not observation.valid and observation.diagnostic_class == "unexpected_empty_extraction"


def test_page_validation_binds_the_capture_to_its_reference_date():
    from autoscout.source_collection import _validate_page_shape

    source = {**SOURCE, "contract_family": " Kirkus-Coming-Soon "}
    page = page_data(captured(source), source)
    assert _validate_page_shape(source, page, "2026-10-13") == ""
    assert _validate_page_shape(source, page, "2026-06-10", freshness_reference="page_reference_date") == ""
    assert _validate_page_shape(source, page, "2026-10-14")
    page["source_page"] = "https://example.test/wrong"
    assert _validate_page_shape(source, page, "2026-10-13")


def test_retained_live_capture_carries_the_bundle_and_the_rows(tmp_path):
    from autoscout.source_collectors.base import capture_content_evidence
    from autoscout.source_collectors.base import capture_live_response

    source = {**SOURCE, "capture_live_response_file": "kirkus.json"}
    bundle = captured()
    page = page_data(bundle, source)
    capture_live_response(tmp_path / "config.json", tmp_path, source, page)
    retained = json.loads((tmp_path / "kirkus.json").read_text())
    assert retained[EVIDENCE_FIELD] == bundle and retained["contract_family"] == FAMILY
    assert len(json.loads(retained["text"])) == 7
    assert capture_content_evidence(page) == {
        "capture_html_bytes": 0,
        "capture_content_bytes": len(page["text"].encode()),
    }


def _live_source(tmp_path, collection=None, seen=None):
    """A live declaration whose result file and seen-item index stand beside the config, as weekly state."""
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    if collection is not None:
        (state / "kirkus-collection.json").write_text(json.dumps(collection))
    if seen is not None:
        (state / "kirkus-seen-items.json").write_text(json.dumps(seen))
    return {
        **SOURCE,
        "kirkus_collection_file": "../state/kirkus-collection.json",
        "seen_guids_file": "../state/kirkus-seen-items.json",
    }


def test_the_weekly_run_requests_nothing_and_reads_the_pre_collected_result(tmp_path, monkeypatch):
    import autoscout.source_collectors.agent_fetch as agent
    from autoscout.source_collectors.base import CollectorRunError

    def no_request(*args, **kwargs):
        raise AssertionError("the weekly run must not request Kirkus")

    monkeypatch.setattr(agent, "_fetch_bounded_page", no_request)
    monkeypatch.setattr("autoscout.source_collectors.urlopen", no_request)
    collection, _, _ = collected()
    seen = {"seen_guids": [{"guid": "9780807023433", "first_seen_date": "2026-10-06"}]}
    source = _live_source(tmp_path, collection, seen)
    config = tmp_path / "configs" / "live.json"
    state_before = {path.name: path.read_bytes() for path in (tmp_path / "state").iterdir()}
    page, debug = agent.fetch_agent_source_page(config, tmp_path, source, reference_date=REFERENCE)
    assert debug["kirkus_collected_date"] == "2026-10-13"
    assert page[EVIDENCE_FIELD]["seen_items"] == [{"isbn": "9780807023433", "first_seen_date": "2026-10-06"}]
    assert len(json.loads(page["text"])) == 6
    # Reading the result moves neither file, so a source health check marks nothing as seen.
    assert {path.name: path.read_bytes() for path in (tmp_path / "state").iterdir()} == state_before

    # A stale result and a missing one are named refusals of this source alone.
    with pytest.raises(CollectorRunError) as stale:
        agent.fetch_agent_source_page(config, tmp_path, source, reference_date=date(2026, 10, 30))
    assert stale.value.debug_metadata["safe_failure_status"] == "kirkus_stale_collection"
    (tmp_path / "state" / "kirkus-collection.json").unlink()
    with pytest.raises(CollectorRunError) as missing:
        agent.fetch_agent_source_page(config, tmp_path, source, reference_date=REFERENCE)
    assert missing.value.debug_metadata["safe_failure_status"] == "kirkus_collection_missing"
    assert "run the pre-collection step" in str(missing.value)


def _codex_replay(output_dir, entries, **extra):
    page = {**page_data(captured(), SOURCE), "source_name": SOURCE["name"]}
    (output_dir / "capture.json").write_text(json.dumps(page))
    (output_dir / "extraction.json").write_text(json.dumps({"entries": entries}))
    return {
        **SOURCE,
        "extraction_method": "codex-extraction-replay",
        "source_page_fixture_file": "capture.json",
        "extraction_response_file": "extraction.json",
        "codex_artifact_dir": ".",
        **extra,
    }


def _mention(isbn):
    row = by_isbn(validate_bundle(captured(), SOURCE))[isbn]
    return {
        **BULLWHIP,
        **NOTE,
        **{field: row[field] for field in ("title", "primary_authors", "publisher", "isbn", "source_week")},
        "evidence_url": row["evidence_url"],
    }


def test_collected_extraction_records_the_reported_books_beside_the_replay_config(tmp_path):
    from autoscout.source_collection import _portable_seen_items_file
    from autoscout.source_collectors.codex_extraction import CodexExtractionCollector

    output_dir = tmp_path / "runs" / "weekly"
    output_dir.mkdir(parents=True)
    live = {**SOURCE, "seen_guids_file": "../runs/weekly/kirkus-seen-items.json"}
    portable = _portable_seen_items_file(tmp_path / "configs" / "live.json", output_dir, live)
    assert portable == {"seen_guids_file": "kirkus-seen-items.json"}
    assert _portable_seen_items_file(tmp_path / "configs" / "live.json", output_dir, SOURCE) == {}

    entries = [_mention(isbn) for isbn in ACCEPTED_ISBNS]
    source = _codex_replay(output_dir, entries, **portable)
    result = CodexExtractionCollector().collect(
        output_dir / "codex-replay-config.json", output_dir, source, "2026-10-13"
    )
    assert [mention["isbn"] for mention in result.mentions] == ACCEPTED_ISBNS
    assert result.debug_metadata["kirkus_new_seen_item_count"] == 7
    recorded = read_seen_items(output_dir / "kirkus-seen-items.json")
    assert list(recorded) == ACCEPTED_ISBNS and set(recorded.values()) == {"2026-10-13"}


def test_a_row_the_extractor_left_out_or_changed_is_shown_again(tmp_path):
    from autoscout.source_collectors.codex_extraction import CodexExtractionCollector

    recased = {**_mention("9781597147323"), "title": "A State of Change"}
    source = _codex_replay(tmp_path, [_mention("9780807023433"), recased], seen_guids_file="seen-items.json")
    result = CodexExtractionCollector().collect(tmp_path / "codex-replay-config.json", tmp_path, source, "2026-10-13")
    assert [mention["title"] for mention in result.mentions] == ["THE BULLWHIP'S ALGORITHM"]
    assert result.skipped_item_count == 1
    # Every row is nonfiction by the site's label, so a row that was not returned was lost, not decided.
    assert read_seen_items(tmp_path / "seen-items.json") == {"9780807023433": "2026-10-13"}


def test_a_capture_with_nothing_new_is_a_healthy_empty_result(tmp_path):
    from autoscout.extraction_completeness import capture_listing_row_count
    from autoscout.source_collectors.agent_extraction import AgentExtractionCollector
    from autoscout.source_collectors.base import CollectorRunError
    from autoscout.source_contribution import source_reports_new_items_only

    all_seen = page_data(captured(seen=dict.fromkeys(ACCEPTED_ISBNS, "2026-10-06")), SOURCE)
    assert json.loads(all_seen["text"]) == [] and capture_listing_row_count(SOURCE, all_seen) == 0
    assert not feasibility(all_seen, SOURCE).accepted
    assert source_reports_new_items_only({**SOURCE, "seen_guids_file": "seen.json"})

    def replay(page):
        (tmp_path / "page.json").write_text(json.dumps(page))
        (tmp_path / "extraction.json").write_text(json.dumps({"mentions": []}))
        return {
            **SOURCE,
            "extraction_method": "agent-extraction-replay",
            "source_page_fixture_file": "page.json",
            "extraction_response_file": "extraction.json",
        }

    collector = AgentExtractionCollector("configured")
    result = collector.collect(tmp_path / "config.json", tmp_path, replay(all_seen), "2026-10-13")
    assert result.mentions == [] and result.debug_metadata["safe_failure_status"] == "healthy_empty"
    # A capture that holds rows still refuses an empty extraction.
    with pytest.raises(CollectorRunError, match="returned no mentions"):
        collector.collect(tmp_path / "config.json", tmp_path, replay(page_data(captured(), SOURCE)), "2026-10-13")


class _Response:
    status = 200

    def __init__(self, url, body):
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self.url = url
        self.body = body.encode()

    def read(self, count):
        return self.body[:count]

    def geturl(self):
        return self.url

    def close(self):
        pass


def _clock(day):
    class Clock(date):
        @classmethod
        def today(cls):
            return day

    return Clock


def test_probe_is_one_delayed_pass_that_retains_a_replayable_bundle(tmp_path, monkeypatch):
    import autoscout.source_probe as probe

    fetch, expected, _ = site()
    collection, _, _ = collected()
    bundle = capture(SOURCE, REFERENCE, collection)
    observed, pauses = [], []

    def opener(request, **kwargs):
        observed.append((request.get_method(), request.full_url))
        return _Response(request.full_url, fetch(request.full_url)["body"])

    monkeypatch.setattr(probe, "date", _clock(REFERENCE))
    monkeypatch.setattr(kirkus_listing, "default_pause", pauses.append)
    report, code = probe.run_source_candidate_probe(
        [SOURCE_URL],
        [SOURCE["name"]],
        ["media/curated list"],
        contract_families=[FAMILY],
        contract_metadata=[SOURCE],
        opener=opener,
        keep_captures=tmp_path / "captures",
        ledger_path=tmp_path / "ledger.json",
        feasibility_dir=tmp_path / "drafts",
    )
    assert code == 0, report
    assert observed == [("GET", url) for url in expected]
    assert pauses == [20.0] * (len(observed) - 1)
    retained = json.loads(next((tmp_path / "captures").glob("*.json")).read_text())
    assert retained[EVIDENCE_FIELD] == bundle
    assert "Verdict sentence" not in json.dumps(retained)

    from autoscout.replay_fixtures import build_replay_fixture_pair

    samples = feasibility(retained, SOURCE).samples
    helper_metadata = {
        "version": 1,
        "source_url": SOURCE_URL,
        "language": "en",
        "entries": [
            {
                "title": sample.title,
                "author": sample.author,
                "evidence_url": sample.url,
                "english_title": sample.title,
                "search_translation": f"Поиск книги {index}",
                "author_transliteration_note": f"Автор книги {index}",
            }
            for index, sample in enumerate(samples, 1)
        ],
    }
    pair = build_replay_fixture_pair(
        SOURCE_URL,
        "kirkus-reviews-coming-soon-nonfiction",
        retained,
        signal_strength="minor mention",
        candidate_category="adult nonfiction",
        language="en",
        helper_metadata=helper_metadata,
    )
    assert pair.entry_count == 3
    assert pair.page[EVIDENCE_FIELD] == bundle and pair.page["html"] == ""
    first = pair.extraction["mentions"][0]
    assert first["title"] == "THE BULLWHIP'S ALGORITHM" and first["primary_authors"] == ["Kamau Bobb"]
    assert first["publisher"] == "Beacon Press" and first["publication_year"] == 2026
    assert first["isbn"] == "9780807023433" and first["source_week"] == "2026-09-25"

    from autoscout.source_collectors.agent_mentions import agent_extraction_mentions_from_response

    mentions, dropped = agent_extraction_mentions_from_response(pair.page, pair.extraction, SOURCE, "2026-10-13")
    assert len(mentions) == 3 and not dropped

    from autoscout.observed_extraction import validate_extraction_artifact

    (tmp_path / "capture.json").write_bytes(pair.page_bytes)
    (tmp_path / "extraction.json").write_bytes(pair.extraction_bytes)
    item = {
        **SOURCE,
        "source_name": pair.extraction["source_name"],
        "source_url": SOURCE_URL,
        "operation_id": pair.operation_id,
        "capture_sha256": pair.capture_sha256,
    }
    observation = validate_extraction_artifact(tmp_path / "extraction.json", item, tmp_path / "capture.json")
    assert observation.valid, observation.diagnostic_class


def test_a_review_page_read_again_because_its_card_changed_is_counted_as_a_request(tmp_path):
    from autoscout.kirkus_precollection import run_pass

    config = _pass_config(tmp_path)
    fetch, _, _ = site()
    run_pass(config, reference=REFERENCE, fetch=fetch, pause=lambda seconds: None)
    renamed = LISTING[1].replace("Kamau Bobb", "Kamau J. Bobb")
    fetch, requests, _ = site(listing={**LISTING, 1: renamed})
    report = run_pass(config, reference=date(2026, 10, 20), fetch=fetch, pause=lambda seconds: None)
    assert requests[3:] == [BULLWHIP_URL]
    assert report["review_pages_opened_this_pass"] == 1 and report["review_pages_kept_from_earlier_passes"] == 11


def test_the_pre_collection_step_decodes_a_compressed_response(monkeypatch):
    import gzip

    import autoscout.kirkus_precollection as step

    class Compressed:
        status = 200

        def __init__(self, url):
            self.url = url
            self.headers = {"Content-Type": "text/html; charset=utf-8", "Content-Encoding": "gzip"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, count=-1):
            return gzip.compress(LISTING[1].encode())[: count if count > 0 else None]

        def geturl(self):
            return self.url

    monkeypatch.setattr(
        kirkus_listing, "open_without_redirect", lambda request, *, timeout: Compressed(request.full_url)
    )
    observed = step.fetch_page(listing_page_url(1))
    assert observed["status"] == 200 and observed["truncated"] is False
    assert len(parse_listing_page(observed["body"])) == 6


def test_the_live_probe_and_the_pre_collection_step_follow_no_redirect(tmp_path, monkeypatch):
    import urllib.error

    import autoscout.kirkus_precollection as step
    import autoscout.source_probe as probe

    opened = []

    def redirected(request, *, timeout):
        opened.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 301, "Moved Permanently", {}, None)

    monkeypatch.setattr(kirkus_listing, "open_without_redirect", redirected)
    monkeypatch.setattr(kirkus_listing, "default_pause", lambda seconds: None)
    monkeypatch.setattr(probe, "date", _clock(REFERENCE))
    # With no injected transport the probe uses the transport that follows no redirect.
    report, code = probe.run_source_candidate_probe(
        [SOURCE_URL],
        [SOURCE["name"]],
        ["media/curated list"],
        contract_families=[FAMILY],
        contract_metadata=[SOURCE],
        ledger_path=tmp_path / "ledger.json",
        feasibility_dir=tmp_path / "drafts",
    )
    assert code == 1 and report["candidates"][0]["reason_code"] == "kirkus_http"
    assert opened == [listing_page_url(1)]
    assert step.fetch_page(listing_page_url(1))["status"] == 301
    # A redirect is answered as an error: the handler hands urllib no second request.
    assert kirkus_listing._NoRedirect().redirect_request(None, None, 301, "", {}, SOURCE_URL) is None


def test_probe_reports_a_named_refusal(tmp_path, monkeypatch):
    import autoscout.source_probe as probe

    monkeypatch.setattr(probe, "date", _clock(REFERENCE))
    monkeypatch.setattr(kirkus_listing, "default_pause", lambda seconds: None)
    report, code = probe.run_source_candidate_probe(
        [SOURCE_URL],
        [SOURCE["name"]],
        ["media/curated list"],
        contract_families=[FAMILY],
        contract_metadata=[SOURCE],
        opener=lambda request, **kwargs: _Response(request.full_url, LISTING[3]),
        ledger_path=tmp_path / "ledger.json",
        feasibility_dir=tmp_path / "drafts",
    )
    assert code == 1
    assert report["candidates"][0]["outcome"] == "reject"
    assert report["candidates"][0]["reason_code"] == "kirkus_page_shape"


def test_scaffolded_live_overlay_declares_the_result_file_and_the_seen_index(tmp_path):
    from autoscout.source_scaffold import _live_model_output_tokens
    from autoscout.source_scaffold import _live_overlay

    helper = tmp_path / "data" / "current-weekly-pilot" / "helper.json"
    metadata = {"name": SOURCE["name"], "contract_family": FAMILY, "helper_metadata_sha256": "0" * 64}
    state_dir = tmp_path / "runs" / "weekly"
    overlay = _live_overlay(metadata, "kirkus", helper, tmp_path / "configs" / "live.json", state_dir)
    # The result is written before the weekly run, whose preflight refuses a dirty tree, so it is kept
    # in a directory Git ignores and never beside the weekly state.
    assert overlay["kirkus_collection_file"] == "../data/kirkus-precollection/kirkus-collection.json"
    ignored = (Path(__file__).parent.parent / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/data/kirkus-precollection/" in ignored
    assert overlay["seen_guids_file"] == "../runs/weekly/kirkus-seen-items.json"
    other = _live_overlay(
        {**metadata, "contract_family": "ipc-top40-csv"}, "top40", helper, tmp_path / "configs" / "live.json", state_dir
    )
    assert "kirkus_collection_file" not in other and "seen_guids_file" not in other
    assert _live_model_output_tokens({"contract_family": FAMILY}) == 8000


def test_the_weekly_capture_allowance_pays_for_no_kirkus_request():
    from autoscout.runtime_budget import declared_extra_request_count

    assert declared_extra_request_count(SOURCE) == 0


def _pass_config(tmp_path, **source):
    config = tmp_path / "configs" / "live.json"
    config.parent.mkdir(exist_ok=True)
    declared = {
        **SOURCE,
        "active": True,
        "access_status": "open",
        "check_frequency": "weekly",
        "extraction_method": "agent-extraction-live",
        "kirkus_collection_file": "../state/kirkus-collection.json",
        **source,
    }
    config.write_text(json.dumps({"run_date": "2026-10-13", "sources": [declared]}))
    return config


def test_the_pre_collection_step_writes_the_result_the_run_reads(tmp_path):
    from autoscout.kirkus_precollection import run_pass

    config = _pass_config(tmp_path)
    fetch, requests, pauses = site()
    report = run_pass(config, reference=REFERENCE, fetch=fetch, pause=pauses.append)
    assert report == {
        "status": "collected",
        "collected_date": "2026-10-13",
        "listing_pages": 3,
        "listed_rows": 12,
        "review_pages_opened_this_pass": 12,
        "review_pages_kept_from_earlier_passes": 0,
        "rows_waiting_for_a_later_pass": 0,
        "rows_with_facts": 7,
        "rejected_rows": {"missing_isbn": 1, "paid_review": 3, "self_published": 1},
        "stop_reason": "",
    }
    # The printed counts carry no title, author or other page text.
    assert "BULLWHIP" not in json.dumps(report)
    written = json.loads((tmp_path / "state" / "kirkus-collection.json").read_text())
    assert len(validate_collection(written, SOURCE)) == 12 and pauses == [20.0] * 14

    # The next pass reads the file it wrote and opens no review page again.
    fetch, requests, _ = site()
    report = run_pass(config, reference=date(2026, 10, 20), fetch=fetch, pause=lambda seconds: None)
    assert requests == [listing_page_url(number) for number in (1, 2, 3)]
    assert report["review_pages_opened_this_pass"] == 0 and report["review_pages_kept_from_earlier_passes"] == 12


def test_a_second_pass_on_the_same_day_reports_the_requests_it_made(tmp_path):
    from autoscout.kirkus_precollection import run_pass

    config = _pass_config(tmp_path, kirkus_max_detail_pages=5)
    fetch, _, _ = site()
    first = run_pass(config, reference=REFERENCE, fetch=fetch, pause=lambda seconds: None)
    assert first["review_pages_opened_this_pass"] == 5 and first["rows_waiting_for_a_later_pass"] == 7
    fetch, requests, _ = site()
    second = run_pass(config, reference=REFERENCE, fetch=fetch, pause=lambda seconds: None)
    # Pages are counted by the pass that requested them, not by their date.
    assert len(requests) == 3 + 5
    assert second["review_pages_opened_this_pass"] == 5 and second["review_pages_kept_from_earlier_passes"] == 5


def test_a_failed_pass_writes_nothing_and_keeps_the_earlier_result(tmp_path, monkeypatch, capsys):
    import autoscout.kirkus_precollection as step

    config = _pass_config(tmp_path)
    fetch, _, _ = site()
    step.run_pass(config, reference=REFERENCE, fetch=fetch, pause=lambda seconds: None)
    result = tmp_path / "state" / "kirkus-collection.json"
    before = result.read_bytes()

    monkeypatch.setattr(step, "fetch_page", lambda url: {"body": "", "status": 403, "resolved_url": url})
    monkeypatch.setattr(kirkus_listing, "default_pause", lambda seconds: None)
    assert step.collect_kirkus_command(config) == 2
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "failed" and printed["reason_code"] == "kirkus_http"
    assert result.read_bytes() == before


def test_the_pre_collection_step_needs_one_declared_kirkus_source(tmp_path):
    from autoscout.kirkus_precollection import configured_source

    with pytest.raises(KirkusError, match="exactly one active Kirkus listing source"):
        configured_source({"sources": [{**SOURCE, "active": False}]})
    with pytest.raises(KirkusError, match="declares no kirkus_collection_file"):
        configured_source({"sources": [SOURCE]})
    declared = {**SOURCE, "kirkus_collection_file": "kirkus.json"}
    assert configured_source({"sources": [declared, {"name": "Other", "contract_family": "generic"}]}) == declared


def _heartbeat_lines():
    from autoscout.russian_workflow import snapshot_progress_line

    lines = []
    return lines, lambda snapshot: lines.append(snapshot_progress_line(snapshot))


def test_the_weekly_stage_makes_the_pass_of_the_day_and_reports_requests_without_page_text(tmp_path):
    from autoscout.kirkus_precollection import weekly_stage

    config = _pass_config(tmp_path)
    fetch, requests, pauses = site()
    lines, presentation = _heartbeat_lines()
    report = weekly_stage(
        config,
        tmp_path / "run",
        REFERENCE,
        presentation=presentation,
        fetch=fetch,
        pause=pauses.append,
        monotonic=lambda: sum(pauses),
    )
    assert report["status"] == "collected" and report["review_pages_opened_this_pass"] == 12
    assert len(requests) == 15 and pauses == [20.0] * 14
    written = json.loads((tmp_path / "state" / "kirkus-collection.json").read_text())
    assert written["collected_date"] == "2026-10-13"
    # At most one line a minute, counting requests made; the snapshot the doctor reads ends finished.
    assert lines[0] == "Сбор Kirkus: запрошено страниц 1."
    assert lines == [f"Сбор Kirkus: запрошено страниц {number}." for number in (1, 4, 7, 10, 13)]
    snapshot = (tmp_path / "run" / "progress-snapshot.json").read_text()
    assert json.loads(snapshot)["status"] == "finished" and len(json.loads(snapshot)["rows"]) == 15
    assert "BULLWHIP" not in snapshot + json.dumps(report)


def test_the_weekly_stage_makes_no_second_pass_on_the_day_of_a_result(tmp_path):
    from autoscout.kirkus_precollection import kirkus_is_due
    from autoscout.kirkus_precollection import weekly_stage

    refused = "https://www.kirkusreviews.com/book-reviews/kamau-bobb/the-bullwhips-algorithm/"
    config = _pass_config(tmp_path)
    fetch, requests, _ = site(refuse=(refused,))
    first = weekly_stage(config, tmp_path / "run", REFERENCE, fetch=fetch, pause=lambda seconds: None)
    assert first["status"] == "partial" and first["stop_reason"] == "kirkus_http"
    assert json.loads((tmp_path / "state" / "kirkus-collection-attempt.json").read_text())["http_status"] == "429"
    # The site refused a request of a pass cut short, so the same day asks nothing more.
    fetch, requests, _ = site()
    assert not kirkus_is_due(config, REFERENCE)
    assert weekly_stage(config, tmp_path / "run", REFERENCE, fetch=fetch) == {"status": "already_attempted_today"}
    assert requests == []
    assert kirkus_is_due(config, date(2026, 10, 14))


def test_the_weekly_stage_is_skipped_without_a_configured_kirkus_source(tmp_path):
    from autoscout.kirkus_precollection import weekly_stage

    config = _pass_config(tmp_path, active=False)
    fetch, requests, _ = site()
    assert weekly_stage(config, tmp_path / "run", REFERENCE, fetch=fetch) == {"status": "not_configured"}
    assert requests == [] and not (tmp_path / "run").exists()


def test_the_weekly_stage_reports_a_refused_listing_without_raising(tmp_path):
    from autoscout.kirkus_precollection import weekly_stage

    config = _pass_config(tmp_path)
    fetch, _, _ = site(refuse=(listing_page_url(1),))
    report = weekly_stage(config, tmp_path / "run", REFERENCE, fetch=fetch, pause=lambda seconds: None)
    assert report["status"] == "not_collected" and report["reason_code"] == "kirkus_http"
    assert not (tmp_path / "state" / "kirkus-collection.json").exists()
    # The attempt says which refusal it was; a pass that got no answer at all says "none".
    attempt = json.loads((tmp_path / "state" / "kirkus-collection-attempt.json").read_text())
    assert attempt["http_status"] == "429"

    def broken(url):
        raise RuntimeError("socket closed")

    report = weekly_stage(config, tmp_path / "run", date(2026, 10, 14), fetch=broken, pause=lambda seconds: None)
    assert report["status"] == "not_collected" and report["exception_type"] == "RuntimeError"


def test_a_listing_that_got_no_answer_records_none_as_its_status(tmp_path):
    from autoscout.kirkus_precollection import weekly_stage

    config = _pass_config(tmp_path)

    def silent(url):
        return {"status": None, "resolved_url": url, "body": "", "truncated": False}

    report = weekly_stage(config, tmp_path / "run", REFERENCE, fetch=silent, pause=lambda seconds: None)
    assert report["reason_code"] == "kirkus_http"
    attempt = json.loads((tmp_path / "state" / "kirkus-collection-attempt.json").read_text())
    assert attempt["http_status"] == "none"


class _OrderedStages:
    """A Kirkus stage and a capture stage that record the order the weekly run calls them in."""

    def __init__(self, order, failing=False):
        self.order, self.failing = order, failing
        self.started = threading.Event()

    def collect(self, config_path, output_dir, reference, *, presentation=None, failure_recorder=None, **options):
        self.order.append(("kirkus", reference.isoformat(), callable(presentation)))
        self.started.set()
        if self.failing:
            raise RuntimeError("socket closed")
        return {"status": "collected", "listed_rows": 12}

    def prepare(self, config_path, output_dir, *, live, effective_date, kirkus_mode="all"):
        if kirkus_mode == "defer":
            assert self.started.wait(5)
        self.order.append(("capture", effective_date))
        return {"returncode": 1, "reason": "stopped by the test"}


@pytest.mark.parametrize("failing", [False, True])
def test_the_weekly_run_starts_the_kirkus_pass_before_the_source_check(tmp_path, failing):
    from autoscout.weekly_workflow import deterministic_adapters
    from autoscout.weekly_workflow import run_weekly_workflow

    config = _pass_config(tmp_path)
    order, announced = [], []
    stages = _OrderedStages(order, failing)
    adapters = deterministic_adapters(progress=announced.append)
    adapters.clock = _clock(REFERENCE)
    adapters.capture = stages
    adapters.kirkus_precollection = stages
    run_weekly_workflow(config, tmp_path / "run", adapters=adapters)
    # A Kirkus pass that fails is left to the capture stage to name; it never stops the run.
    assert order == [("kirkus", "2026-10-13", True), ("capture", "2026-10-13")]
    assert announced[:2] == ["kirkus-precollection", "capture"]
    log = json.loads((tmp_path / "run" / "workflow-log.json").read_text())
    entry = next(item for item in log if item["stage"] == "kirkus-precollection")
    # #880 starts the pass in the background. This capture deliberately
    # stops the run before the normal join; only the launch is logged here.
    assert entry["status"] == "started"


def test_a_weekly_run_with_no_bound_kirkus_stage_requests_nothing(tmp_path):
    from autoscout.weekly_workflow import deterministic_adapters
    from autoscout.weekly_workflow import run_weekly_workflow

    order, announced = [], []
    adapters = deterministic_adapters(progress=announced.append)
    adapters.clock = _clock(REFERENCE)
    adapters.capture = _OrderedStages(order)
    run_weekly_workflow(_pass_config(tmp_path), tmp_path / "run", adapters=adapters)
    assert order == [("capture", "2026-10-13")] and announced[0] == "capture"


def test_the_weekly_command_and_the_recovery_runner_bind_the_live_kirkus_stage():
    import inspect

    import autoscout.cli.weekly as cli
    import autoscout.doctor_recovery as recovery
    from autoscout.kirkus_precollection import ProductionKirkusPrecollectionStage
    from autoscout.weekly_workflow import bind_kirkus_precollection
    from autoscout.weekly_workflow import deterministic_adapters

    adapters = deterministic_adapters()
    assert adapters.kirkus_precollection is None
    bind_kirkus_precollection(adapters)
    assert isinstance(adapters.kirkus_precollection, ProductionKirkusPrecollectionStage)
    assert "bind_kirkus_precollection(adapters)" in inspect.getsource(cli._run_weekly_command)
    assert "bind_kirkus_precollection(adapters)" in inspect.getsource(recovery)


def test_a_failed_pass_is_still_the_pass_of_its_day_and_the_run_names_the_earlier_result_it_reads(tmp_path):
    import autoscout.source_collectors.agent_fetch as agent
    from autoscout.kirkus_precollection import kirkus_is_due
    from autoscout.kirkus_precollection import weekly_stage

    config = _pass_config(tmp_path, seen_guids_file="../state/kirkus-seen-items.json")
    source = json.loads(config.read_text())["sources"][0]
    fetch, _, _ = site()
    weekly_stage(config, tmp_path / "run", date(2026, 10, 6), fetch=fetch, pause=lambda seconds: None)
    page, debug = agent.fetch_agent_source_page(config, tmp_path, source, reference_date=date(2026, 10, 6))
    # The pass of the day is read, so no failed pass is named; the one listed book without an ISBN is (#873).
    assert debug["incomplete_ranking_warning"] == "Kirkus left out 1 listed book (missing_isbn 1)"

    # The listing is refused a week later: no result is written, and the day still has had its pass.
    fetch, requests, _ = site(refuse=(listing_page_url(1),))
    report = weekly_stage(config, tmp_path / "run", REFERENCE, fetch=fetch, pause=lambda seconds: None)
    assert report["status"] == "not_collected" and len(requests) == 1
    assert not kirkus_is_due(config, REFERENCE)
    fetch, requests, _ = site()
    assert weekly_stage(config, tmp_path / "run", REFERENCE, fetch=fetch) == {"status": "already_attempted_today"}
    assert requests == []

    # The run reads the result of the earlier pass and says so, in the form every incomplete source uses.
    page, debug = agent.fetch_agent_source_page(config, tmp_path, source, reference_date=REFERENCE)
    assert debug["kirkus_collected_date"] == "2026-10-06" and debug["incomplete_ranking"] is True
    assert debug["incomplete_ranking_warning"] == (
        "incomplete collection: the Kirkus pre-collection pass of 2026-10-13 failed (kirkus_http); "
        "the result of 2026-10-06 is used; Kirkus left out 1 listed book (missing_isbn 1)"
    )
    assert (debug["raw_row_count"], debug["expected_raw_row_count"]) == (12, 12)
    # The next day asks again.
    assert kirkus_is_due(config, date(2026, 10, 14))


def test_the_weekly_stage_stops_at_its_time_allowance_and_keeps_the_pages_it_opened(tmp_path):
    from autoscout.kirkus_precollection import kirkus_is_due
    from autoscout.kirkus_precollection import weekly_stage

    config = _pass_config(tmp_path)
    fetch, requests, _ = site()
    elapsed = [0.0]

    def pause(seconds):
        elapsed[0] += seconds

    def stage(budget):
        return weekly_stage(
            config,
            tmp_path / "run",
            REFERENCE,
            fetch=fetch,
            pause=pause,
            monotonic=lambda: elapsed[0],
            budget_seconds=budget,
        )

    # Three listing pages and three review pages are requested before 120 seconds of delays have passed.
    report = stage(120.0)
    assert report["status"] == "partial" and report["stop_reason"] == "kirkus_time_budget"
    assert len(requests) == 6 and report["review_pages_opened_this_pass"] == 3
    assert report["rows_waiting_for_a_later_pass"] == 9
    assert json.loads((tmp_path / "run" / "progress-snapshot.json").read_text())["stage_budget_seconds"] == 120.0
    assert not kirkus_is_due(config, REFERENCE)

    # An allowance that ends inside the listing leaves no result at all.
    (tmp_path / "state" / "kirkus-collection.json").unlink()
    (tmp_path / "state" / "kirkus-collection-attempt.json").unlink()
    elapsed[0] = 0.0
    report = stage(30.0)
    assert report == {
        "status": "not_collected",
        "reason_code": "kirkus_time_budget",
        "duration_seconds": report["duration_seconds"],
    }
    assert not (tmp_path / "state" / "kirkus-collection.json").exists()


def test_the_declared_caps_let_no_kirkus_pass_outlast_the_weekly_run():
    from autoscout.runtime_budget import CAPTURE_RUNTIME_BUDGET_SECONDS
    from autoscout.runtime_budget import FINALIZATION_RESERVE_SECONDS
    from autoscout.runtime_budget import KIRKUS_PASS_RUNTIME_BUDGET_SECONDS
    from autoscout.runtime_budget import PORTFOLIO_RUNTIME_BUDGET_SECONDS
    from autoscout.runtime_budget import WEEKLY_OPERATION_CEILING_SECONDS

    # The pass leaves the capture and extraction stages their whole budgets inside the weekly ceiling.
    assert KIRKUS_PASS_RUNTIME_BUDGET_SECONDS == 2400
    assert (
        KIRKUS_PASS_RUNTIME_BUDGET_SECONDS
        + CAPTURE_RUNTIME_BUDGET_SECONDS
        + PORTFOLIO_RUNTIME_BUDGET_SECONDS
        + FINALIZATION_RESERVE_SECONDS
        <= WEEKLY_OPERATION_CEILING_SECONDS
    )


def test_a_kirkus_source_the_run_skips_for_its_access_status_is_never_requested(tmp_path):
    from autoscout.kirkus_precollection import kirkus_is_due
    from autoscout.kirkus_precollection import weekly_stage

    config = _pass_config(tmp_path, access_status="blocked")
    fetch, requests, _ = site()
    assert not kirkus_is_due(config, REFERENCE)
    assert weekly_stage(config, tmp_path / "run", REFERENCE, fetch=fetch) == {"status": "not_configured"}
    assert requests == []


class _TricklingResponse:
    """A response whose body never ends: every read returns a few bytes after a second."""

    status, headers = 200, {}

    def __init__(self, clock):
        self.clock = clock

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def geturl(self):
        return listing_page_url(1)

    def read1(self, size=-1):
        self.clock[0] += 1.0
        return b"<p>slow</p>"


def test_a_response_that_trickles_in_cannot_hold_a_pass_past_its_allowance(tmp_path, monkeypatch):
    import autoscout.kirkus_precollection as step

    clock = [0.0]
    monkeypatch.setattr(step.time, "monotonic", lambda: clock[0])
    timeouts = []

    def open_slow(request, timeout):
        timeouts.append(timeout)
        return _TricklingResponse(clock)

    monkeypatch.setattr(kirkus_listing, "open_without_redirect", open_slow)
    # Alone, a request is given up after its own thirty seconds.
    page = step.fetch_page(listing_page_url(1))
    assert page["truncated"] is True and page["body"] == "" and 30.0 <= clock[0] <= 32.0

    # Inside a weekly pass it is given up when the pass has no time left, and the pass ends there.
    clock[0] = 0.0
    report = step.weekly_stage(
        _pass_config(tmp_path),
        tmp_path / "run",
        REFERENCE,
        pause=lambda seconds: None,
        monotonic=lambda: clock[0],
        budget_seconds=12.0,
    )
    assert report["status"] == "not_collected" and report["reason_code"] == "kirkus_response_bounds"
    assert timeouts[-1] == 12.0 and 12.0 <= clock[0] <= 14.0


def test_a_response_whose_headers_never_arrive_is_given_up_at_the_request_deadline(monkeypatch):
    import threading

    import autoscout.kirkus_precollection as step

    release = threading.Event()

    def open_stalled(request, timeout):
        release.wait(30)
        raise OSError("closed")

    monkeypatch.setattr(kirkus_listing, "open_without_redirect", open_stalled)
    monkeypatch.setattr(step, "REQUEST_TIMEOUT_SECONDS", 0.05)
    try:
        # The socket timeout bounds one receive; the deadline bounds the request, headers included.
        assert step.fetch_page(listing_page_url(1))["status"] is None
        # A pass with less time left than one request gives the request only that.
        assert step.fetch_page(listing_page_url(1), lambda: 0.0)["status"] is None
    finally:
        release.set()
