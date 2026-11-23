"""The Perlentaucher daily digest contract (#809): dated pages from one index, one row per book."""

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from autoscout.perlentaucher_digest import EVIDENCE_FIELD
from autoscout.perlentaucher_digest import FAMILY
from autoscout.perlentaucher_digest import ORIGIN
from autoscout.perlentaucher_digest import SOURCE_URL
from autoscout.perlentaucher_digest import PerlentaucherError
from autoscout.perlentaucher_digest import capture
from autoscout.perlentaucher_digest import daily_page_days
from autoscout.perlentaucher_digest import daily_page_url
from autoscout.perlentaucher_digest import feasibility
from autoscout.perlentaucher_digest import has_translator_note
from autoscout.perlentaucher_digest import mention_violation
from autoscout.perlentaucher_digest import page_data
from autoscout.perlentaucher_digest import parse_daily_page
from autoscout.perlentaucher_digest import qualifying_rows
from autoscout.perlentaucher_digest import read_seen_items
from autoscout.perlentaucher_digest import record_seen_items
from autoscout.perlentaucher_digest import validate_bundle
from autoscout.perlentaucher_digest import validate_declaration

FIXTURES = Path(__file__).parent / "fixtures/perlentaucher"
INDEX = (FIXTURES / "index.html").read_text(encoding="utf-8")
DAYS = ["2026-10-10", "2026-10-09", "2026-10-08"]
PAGES = {day: (FIXTURES / f"{day}.html").read_text(encoding="utf-8") for day in DAYS}
SOURCE = {
    "name": "Perlentaucher Bücherschau des Tages",
    "url": SOURCE_URL,
    "family": "media/curated list",
    "contract_family": FAMILY,
    "perlentaucher_max_daily_pages": 3,
    "perlentaucher_max_age_days": 14,
    "perlentaucher_translation_policy": "exclude-translator-note",
    "language": "de",
    "eligible_candidate_category": "adult nonfiction",
    "expected_signal_strength": "minor mention",
}
REFERENCE = date(2026, 10, 12)
AUGSTEIN_URL = ORIGIN + "/buch/martin-doerry-hauke-janssen/augstein.html"
AUGSTEIN = {
    "title": "Augstein",
    "primary_authors": ["Martin Doerry", "Hauke Janssen"],
    "publisher": "C.H. Beck Verlag",
    "publication_year": 2026,
    "isbn": "9783406853098",
    "source_week": "2026-10-09",
    "source_url": SOURCE_URL,
    "evidence_url": AUGSTEIN_URL,
    "signal_type": "media/curated list",
    "signal_strength": "minor mention",
    "candidate_category": "adult nonfiction",
}


def captured(source=None, reference=REFERENCE, index=INDEX, pages=None, seen=None):
    requests = []
    bodies = PAGES if pages is None else pages

    def fetch(url):
        requests.append(url)
        if url == SOURCE_URL:
            return {"body": index, "status": 200, "resolved_url": url}
        return {"body": bodies[url.rsplit("/", 1)[1].removesuffix(".html")], "status": 200, "resolved_url": url}

    return capture(source or SOURCE, reference, fetch, seen), requests


def by_isbn(rows):
    return {row["isbn"]: row for row in rows}


def test_capture_reads_the_index_then_the_newest_daily_pages_up_to_the_cap():
    bundle, requests = captured()
    assert requests == [SOURCE_URL, *(daily_page_url(day) for day in DAYS)]
    assert bundle["index"]["daily_pages"] == DAYS
    assert [len(page["rows"]) for page in bundle["daily_pages"]] == [14, 10, 6]
    assert [page["rejected_rows"] for page in bundle["daily_pages"]] == [[], [], []]


def test_index_resolution_keeps_only_current_dated_pages():
    eight = {**SOURCE, "perlentaucher_max_daily_pages": 8}
    # Monthly archive links such as 2026-10.html are not daily pages, and 29 and 30 September fall past the cap.
    assert daily_page_days(INDEX, REFERENCE, eight) == [
        "2026-10-10",
        "2026-10-09",
        "2026-10-08",
        "2026-10-07",
        "2026-10-06",
        "2026-10-05",
        "2026-10-02",
        "2026-10-01",
    ]
    assert daily_page_days(INDEX, date(2026, 10, 20), eight)[-1] == "2026-10-06"
    with pytest.raises(PerlentaucherError) as stale:
        daily_page_days(INDEX, date(2026, 11, 1), eight)
    assert stale.value.reason_code == "perlentaucher_stale"
    with pytest.raises(PerlentaucherError) as empty:
        daily_page_days("<a href='/buecherschau/2026-10.html'>Oktober</a>", REFERENCE, eight)
    assert empty.value.reason_code == "perlentaucher_index"


def test_a_page_dated_after_the_run_date_is_not_read():
    assert daily_page_days(INDEX, date(2026, 10, 9), SOURCE) == ["2026-10-10", "2026-10-09", "2026-10-08"]
    assert daily_page_days(INDEX, date(2026, 10, 8), SOURCE) == ["2026-10-09", "2026-10-08", "2026-10-07"]


def test_identity_fields_are_grounded_from_the_row():
    bundle, _ = captured()
    rows = by_isbn(validate_bundle(bundle, SOURCE))
    assert rows["9783406853098"] == {
        "title": "Augstein",
        "subtitle": "Ein deutsches Leben",
        "primary_authors": ["Martin Doerry", "Hauke Janssen"],
        "publisher": "C.H. Beck Verlag",
        "publication_year": 2026,
        "isbn": "9783406853098",
        "evidence_url": AUGSTEIN_URL,
        "has_translator_note": False,
        "seen": False,
        "review_dates": ["2026-10-09"],
        "reviewed_in": ["Frankfurter Allgemeine Zeitung"],
        "source_week": "2026-10-09",
    }
    # A colon inside the title is not the author separator, and a title keeps its own punctuation.
    assert rows["9783446284791"]["title"] == "Komm du mal in mein Alter!"
    assert rows["9783446284876"]["primary_authors"] == ["Natalie Amiri", "Minu Barati", "Shila Behjat", "Düzen Tekkal"]
    assert rows["9783462055900"]["publisher"] == "Kiepenheuer und Witsch Verlag"


def test_a_book_reviewed_in_several_papers_or_on_several_days_is_one_row():
    bundle, _ = captured()
    rows = validate_bundle(bundle, SOURCE)
    assert sum(len(page["rows"]) for page in bundle["daily_pages"]) == 30
    assert len(rows) == 28 == len({row["isbn"] for row in rows})
    lepore = by_isbn(rows)["9783406849329"]
    assert lepore["review_dates"] == ["2026-10-09", "2026-10-10"]
    assert lepore["source_week"] == "2026-10-09"
    seiler = by_isbn(rows)["9783518433157"]
    assert seiler["review_dates"] == ["2026-10-08"]
    assert seiler["reviewed_in"] == ["Süddeutsche Zeitung", "Deutschlandfunk Kultur"]


def test_a_row_with_a_translator_note_is_excluded():
    bundle, _ = captured()
    rows = validate_bundle(bundle, SOURCE)
    noted = [row for row in rows if row["has_translator_note"]]
    assert len(noted) == 13
    assert by_isbn(rows)["9783518588574"]["has_translator_note"] is True
    kept = qualifying_rows(rows, SOURCE)
    assert len(kept) == 15
    assert not any(row["has_translator_note"] for row in kept)


@pytest.mark.parametrize(
    ("opening", "noted"),
    [
        ("Aus dem Französischen von Andrea Hemminger. Mit einem Vorwort", True),
        ("Aus dem Englischen übersetzt von Werner Roller.", True),
        ("Aus dem amerikanischen Englisch von Eva Bonné.", True),
        ("Aus dem Russischen und mit einem Nachwort von Ganna-Maria Braungardt.", True),
        ("Aus dem Hindi von Almuth Degener.", True),
        ("Aus dem Urdu übersetzt von Christina Oesterheld.", True),
        ("Aus dem Sanskrit und mit einem Nachwort von Peter Schreiner.", True),
        ("Aus dem Hindukusch von Kabul nach Berlin.", False),
        ("Aus dem Leben von Rudolf Augstein erzählt dieses Buch.", False),
        ("Mit Abbildungen. Aus dem Französischen von Andrea Hemminger.", False),
        ("…", False),
        ("", False),
    ],
)
def test_translator_note_is_read_only_at_the_opening_of_the_blurb(opening, noted):
    assert has_translator_note(opening) is noted


def test_no_blurb_or_summary_text_is_retained():
    marker = "Ein einzigartiger Klappentext, der nirgends gespeichert werden darf"
    summary = "Die Zeitung staunt über ein unvergleichliches Buch"
    page = (
        PAGES["2026-10-08"]
        .replace('<div class="paragraph">… </div>', f'<div class="paragraph">{marker}… </div>', 1)
        .replace("<b>08.10.2026. </b>", f"<b>08.10.2026. </b>{summary}")
    )
    assert marker in page and summary in page
    bundle, _ = captured(pages={**PAGES, "2026-10-08": page})
    retained = json.dumps(page_data(bundle, SOURCE), ensure_ascii=False)
    assert marker not in retained and summary not in retained
    assert "Aus dem" not in retained and "Seiten" not in retained and "EUR" not in retained
    assert set(bundle["daily_pages"][0]) == {"date", "url", "status", "sha256", "body_bytes", "rows", "rejected_rows"}
    assert bundle["daily_pages"][2]["sha256"] == hashlib.sha256(page.encode()).hexdigest()


def test_declared_genre_labels_exclude_rows_before_the_extractor():
    labelled = {**SOURCE, "adult_nonfiction_exclusions": ["Roman", "Thriller"]}
    bundle, _ = captured(labelled)
    rows = validate_bundle(bundle, labelled)
    kept = by_isbn(qualifying_rows(rows, labelled))
    # "Das tickende Herz" carries the subtitle "Roman"; "Wiedergutmachung für Sinti und Roma" does not.
    assert "9783518433157" not in kept
    assert len(kept) == 10
    assert not any(row["subtitle"].casefold().endswith("roman") for row in kept.values())
    assert by_isbn(qualifying_rows(rows, SOURCE))["9783518433157"]["subtitle"] == "Roman"


def test_a_book_seen_by_an_earlier_run_is_not_reported_again():
    seen = {"9783406853098": "2026-10-05", "9783462055900": "2026-10-12", "9780000000002": "2026-10-01"}
    bundle, _ = captured(seen=seen)
    # A book first recorded today is shown again, and an ISBN not on these pages is not carried along.
    assert bundle["seen_items"] == [{"isbn": "9783406853098", "first_seen_date": "2026-10-05"}]
    rows = by_isbn(validate_bundle(bundle, SOURCE))
    assert rows["9783406853098"]["seen"] is True
    assert rows["9783462055900"]["seen"] is False
    kept = by_isbn(qualifying_rows(list(rows.values()), SOURCE))
    assert "9783406853098" not in kept and "9783462055900" in kept
    assert mention_violation(AUGSTEIN, page_data(bundle, SOURCE), SOURCE)


def test_seen_items_are_recorded_only_through_a_declared_index(tmp_path):
    config = tmp_path / "configs" / "live.json"
    bundle, _ = captured()
    page = page_data(bundle, SOURCE)
    assert record_seen_items(config, SOURCE, page, set(), 0) == 0
    assert not list(tmp_path.rglob("*.json"))

    declared = {**SOURCE, "seen_guids_file": "../state/seen-items.json"}
    assert record_seen_items(config, declared, page, {"9783406853098"}, 0) == 15
    state = json.loads((tmp_path / "state" / "seen-items.json").read_text())
    assert set(state) == {"source", "updated_at", "seen_guids"}
    assert state["updated_at"] == "2026-10-12"
    assert {entry["first_seen_date"] for entry in state["seen_guids"]} == {"2026-10-12"}
    recorded = {entry["guid"] for entry in state["seen_guids"]}
    assert recorded == {row["isbn"] for row in qualifying_rows(validate_bundle(bundle, SOURCE), SOURCE)}
    # A translated row was never given to the extractor, so it is not recorded as decided.
    assert "9783518588574" not in recorded
    assert record_seen_items(config, declared, page, {"9783406853098"}, 0) == 0

    seen = read_seen_items(tmp_path / "state" / "seen-items.json")
    later, _ = captured(reference=date(2026, 10, 13), seen=seen)
    assert qualifying_rows(validate_bundle(later, SOURCE), SOURCE) == []


@pytest.mark.parametrize(
    "state",
    ["{", "[]", '{"seen_guids": [{"guid": "123", "first_seen_date": "2026-10-01"}]}', '{"seen_guids": [{}]}'],
)
def test_malformed_seen_state_fails_closed(tmp_path, state):
    path = tmp_path / "seen.json"
    path.write_text(state)
    with pytest.raises(PerlentaucherError) as error:
        read_seen_items(path)
    assert error.value.reason_code == "perlentaucher_seen_state"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda page: page.replace("Bücherschau des Tages</h4>", "Neue Bücher</h4>"), "perlentaucher_page_shape"),
        (lambda page: page.replace("<b>08.10.2026. </b>", "<b>07.10.2026. </b>"), "perlentaucher_page_date"),
        (lambda page: page.replace("<b>08.10.2026. </b>", ""), "perlentaucher_page_shape"),
        (lambda page: page.replace('class="book teaser-block"', 'class="teaser-block"'), "perlentaucher_page_shape"),
    ],
)
def test_page_shape_drift_fails_closed(change, reason):
    with pytest.raises(PerlentaucherError) as error:
        parse_daily_page(change(PAGES["2026-10-08"]), date(2026, 10, 8))
    assert error.value.reason_code == reason


@pytest.mark.parametrize(
    ("old", "new", "reason"),
    [
        ("Franz Walter: <span>", "<span>", "missing_author"),
        ("9783423285650", "9783423285651", "invalid_isbn"),
        ("dtv, München 2026<br>", "dtv<br>", "publisher_line"),
    ],
)
def test_a_row_without_a_grounded_author_isbn_or_publisher_line_is_rejected_not_guessed(old, new, reason):
    rows, rejected = parse_daily_page(PAGES["2026-10-08"].replace(old, new, 1), date(2026, 10, 8))
    assert rejected == [reason]
    assert len(rows) == 5
    assert "Joachim Fest" not in {row["title"] for row in rows}


def test_a_book_without_an_author_is_one_rejected_row_not_a_changed_page():
    page = (FIXTURES / "2026-10-24.html").read_text(encoding="utf-8")
    rows, rejected = parse_daily_page(page, date(2026, 10, 24))
    assert rejected == ["missing_author"]
    assert len(rows) == 2
    assert "Weltgeschichte der Schweiz" not in {row["title"] for row in rows}


@pytest.mark.parametrize(
    ("old", "new", "reason"),
    [
        ('class="newspaper"', 'class="paper"', "missing_paper"),
        ('href="/buch/franz-walter/joachim-fest.html"', 'href="/autor/fest"', "missing_book_link"),
        ("<span>Joachim Fest.</span> <span>Ein Leben im 20. Jahrhundert </span>", "<span> </span>", "missing_title"),
    ],
)
def test_one_malformed_row_is_rejected(old, new, reason):
    rows, rejected = parse_daily_page(PAGES["2026-10-08"].replace(old, new, 1), date(2026, 10, 8))
    assert len(rows) == 5
    assert rejected == [reason]


@pytest.mark.parametrize(
    ("old", "new", "reason"),
    [
        ('href="/buch/', 'href="/autor/', "missing_book_link"),
        ('class="newspaper"', 'class="paper"', "missing_paper"),
        ("ISBN", "EAN", "invalid_isbn"),
    ],
)
def test_all_rows_rejected(old, new, reason):
    rows, rejected = parse_daily_page(PAGES["2026-10-08"].replace(old, new), date(2026, 10, 8))
    assert not rows
    assert rejected == [reason] * 6
    with pytest.raises(PerlentaucherError) as error:
        captured(pages={day: page.replace(old, new) for day, page in PAGES.items()})
    assert error.value.reason_code == "perlentaucher_all_rows_rejected"


def test_bundle_accepts_rejected_page_but_not_all_rejected():
    bundle, _ = captured()
    page = bundle["daily_pages"][0]
    page["rejected_rows"] = ["missing_title"] * len(page["rows"])
    page["rows"] = []
    assert validate_bundle(bundle, SOURCE)
    for page in bundle["daily_pages"]:
        page["rejected_rows"] = ["missing_title"]
        page["rows"] = []
    with pytest.raises(PerlentaucherError) as error:
        validate_bundle(bundle, SOURCE)
    assert error.value.reason_code == "perlentaucher_all_rows_rejected"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda b: b.__setitem__("version", 2),
        lambda b: b.__setitem__("source_url", "https://example.test/"),
        lambda b: b.__setitem__("reference_date", "12.10.2026"),
        lambda b: b["index"].__setitem__("daily_pages", list(reversed(DAYS))),
        lambda b: b["index"].__setitem__("sha256", "0"),
        lambda b: b["daily_pages"].pop(),
        lambda b: b["daily_pages"][0].__setitem__("url", daily_page_url("2026-10-09")),
        lambda b: b["daily_pages"][0].__setitem__("rows", []),
        lambda b: b["daily_pages"][0].__setitem__("rejected_rows", ["blurb"]),
        lambda b: b["daily_pages"][0]["rows"][0].__setitem__("isbn", "9783518588575"),
        lambda b: b["daily_pages"][0]["rows"][0].__setitem__("authors", []),
        lambda b: b["daily_pages"][0]["rows"][0].__setitem__("publication_year", "2026"),
        lambda b: b["daily_pages"][0]["rows"][0].__setitem__("book_url", "https://example.test/buch/a/b.html"),
        lambda b: b["daily_pages"][0]["rows"][0].__setitem__("blurb", "copied text"),
        lambda b: b["daily_pages"][0]["rows"][0].__setitem__("has_translator_note", "no"),
        lambda b: b.__setitem__("seen_items", [{"isbn": "9783406853098", "first_seen_date": "2026-10-12"}]),
        lambda b: b.pop("seen_items"),
    ],
)
def test_replay_identity_fails(mutation):
    bundle, _ = captured()
    mutation(bundle)
    with pytest.raises(PerlentaucherError) as error:
        validate_bundle(bundle, SOURCE)
    assert error.value.reason_code == "perlentaucher_evidence_identity"


def test_one_isbn_for_two_books_fails():
    bundle, _ = captured()
    bundle["daily_pages"][1]["rows"][0]["isbn"] = bundle["daily_pages"][0]["rows"][1]["isbn"]
    with pytest.raises(PerlentaucherError) as error:
        validate_bundle(bundle, SOURCE)
    assert error.value.reason_code == "perlentaucher_row_identity"


@pytest.mark.parametrize(("reference", "accepted"), [("2026-10-22", True), ("2026-10-23", False)])
def test_daily_page_age_limit_holds_on_replay(reference, accepted):
    bundle, _ = captured()
    bundle["reference_date"] = reference
    if accepted:
        assert validate_bundle(bundle, SOURCE)
    else:
        with pytest.raises(PerlentaucherError) as error:
            validate_bundle(bundle, SOURCE)
        assert error.value.reason_code == "perlentaucher_stale"


@pytest.mark.parametrize(
    "change",
    [
        {"url": "https://www.perlentaucher.de/buecherschau/2026-10-10.html"},
        {"perlentaucher_max_daily_pages": 9},
        {"perlentaucher_max_daily_pages": 0},
        {"perlentaucher_max_daily_pages": True},
        {"perlentaucher_max_age_days": 15},
        {"perlentaucher_max_age_days": None},
        {"perlentaucher_translation_policy": "keep"},
        {"adult_nonfiction_exclusions": ["Roman", " "]},
    ],
)
def test_declaration_is_explicit(change):
    with pytest.raises(PerlentaucherError) as error:
        validate_declaration({**SOURCE, **change})
    assert error.value.reason_code == "perlentaucher_declaration"


@pytest.mark.parametrize(
    ("status", "truncated", "resolved", "reason"),
    [
        (404, False, None, "perlentaucher_http"),
        (200, True, None, "perlentaucher_response_bounds"),
        (200, False, "https://www.perlentaucher.de/", "perlentaucher_wrong_target"),
    ],
)
def test_transport_refusal_stops_requests(status, truncated, resolved, reason):
    requests = []

    def fetch(url):
        requests.append(url)
        if url == SOURCE_URL:
            return {"body": INDEX, "status": 200, "resolved_url": url}
        return {"body": PAGES[DAYS[0]], "status": status, "truncated": truncated, "resolved_url": resolved or url}

    with pytest.raises(PerlentaucherError) as error:
        capture(SOURCE, REFERENCE, fetch)
    assert error.value.reason_code == reason
    assert requests == [SOURCE_URL, daily_page_url(DAYS[0])]


def test_no_disallowed_or_book_page_url_is_requested():
    _, requests = captured()
    assert all("?" not in url for url in requests)
    assert not any(
        "/buch/" in url or "/woche/" in url or "/stichwort/" in url or "/buchKSL/" in url for url in requests
    )


def test_mention_must_copy_one_qualifying_row_exactly():
    bundle, _ = captured()
    page = page_data(bundle, SOURCE)
    assert mention_violation(AUGSTEIN, page, SOURCE) == ""
    for change in (
        {"title": "Augstein. Ein deutsches Leben"},
        {"primary_authors": ["Martin Doerry"]},
        {"primary_authors": ["Hauke Janssen", "Martin Doerry"]},
        {"publisher": "C.H. Beck"},
        {"publication_year": 2025},
        {"publication_year": "2026"},
        {"isbn": "9783406849329"},
        {"source_week": "2026-10-10"},
        {"source_url": AUGSTEIN_URL},
        {"evidence_url": SOURCE_URL},
        {"signal_strength": "major media list"},
        {"candidate_category": "fiction"},
    ):
        assert mention_violation({**AUGSTEIN, **change}, page, SOURCE), change
    # The model decides one thing. It may not add a description, a status or a fit judgement.
    for extra in (
        {"content_description": "Описание книги."},
        {"russian_edition_status": "found in Russian"},
        {"publisher_fit": "strong"},
        {"subtitle": "Ein deutsches Leben"},
    ):
        refusal = mention_violation({**AUGSTEIN, **extra}, page, SOURCE)
        assert next(iter(extra)) in refusal, extra
    helpers = {"english_title": "Augstein: A German Life", "search_translation": "Аугштайн", "evidence_note": "Row."}
    assert mention_violation({**AUGSTEIN, **helpers, "author_transliteration_note": "Дёрри"}, page, SOURCE) == ""
    # A translated row exists on the page and still cannot become a mention.
    foucault = {
        **AUGSTEIN,
        "title": "Die Hermaphroditen",
        "primary_authors": ["Michel Foucault"],
        "publisher": "Suhrkamp Verlag",
        "isbn": "9783518588574",
        "source_week": "2026-10-10",
        "evidence_url": ORIGIN + "/buch/michel-foucault/die-hermaphroditen.html",
    }
    assert mention_violation(foucault, page, SOURCE)


def test_feasibility_reports_the_page_window_and_grounded_samples():
    bundle, _ = captured()
    page = page_data(bundle, SOURCE)
    verdict = feasibility(page, SOURCE)
    assert verdict.accepted and verdict.contract_family == FAMILY
    assert verdict.raw_row_count == 28 and verdict.bounded_entry_count == 15
    assert verdict.reporting_period == "2026-10-08/2026-10-10"
    assert [sample.identifier for sample in verdict.samples] == ["9783446284876", "9783462055900", "9783630878393"]
    assert verdict.samples[0].author == "Natalie Amiri / Minu Barati / Shila Behjat / Düzen Tekkal"
    from autoscout.source_feasibility import evaluate_source_feasibility

    assert evaluate_source_feasibility(SOURCE, page) == verdict
    assert not feasibility({**page, "source_page": "https://example.test/"}, SOURCE).accepted


def test_extraction_contract_asks_only_for_the_nonfiction_decision():
    from autoscout.source_collectors.agent_model_input import model_bounded_page_data
    from autoscout.source_collectors.contract import agent_extraction_prompt
    from autoscout.source_collectors.contract import extraction_contract

    bundle, _ = captured()
    page = page_data(bundle, SOURCE)
    contract = extraction_contract(SOURCE, source_page=SOURCE_URL, capture_file="capture.json")
    rules = " ".join(contract["rules"])
    assert "Decide for each row only whether the book is adult nonfiction" in rules
    assert "never correct, complete, translate, reorder or merge them" in rules
    assert "its text field is the complete list of rows to decide on, one row per book" in rules
    assert "Return at most one mention per isbn" in rules
    assert "html field is the authoritative" not in rules
    assert "A row that is not adult nonfiction breaks a rule" in rules
    # The shared completeness and empty-result rules stay verbatim for every configured contract (#740).
    assert "Return every row the page shows that meets the required fields" in rules
    shape = contract["required_output_shape"]["entries"][0]
    assert shape["signal_strength"] == "minor mention"
    assert "content_description" not in shape and "subtitle" not in shape
    assert {"isbn", "source_week", "publisher", "publication_year"} <= set(contract["required_fields"])

    model, _ = model_bounded_page_data(page, 120000, source=SOURCE)
    model_rows = json.loads(model["text"])
    assert len(model_rows) == 15 and EVIDENCE_FIELD not in model and model["html"] == ""
    assert set(model_rows[0]) == {
        "title",
        "subtitle",
        "primary_authors",
        "publisher",
        "publication_year",
        "isbn",
        "source_week",
        "evidence_url",
    }
    prompt = agent_extraction_prompt(SOURCE, page, 120000)
    assert AUGSTEIN_URL in prompt and "Die Hermaphroditen" not in prompt and "list-page HTML" not in prompt


def test_extraction_keeps_the_rows_the_model_returns_and_refuses_changed_identity():
    from autoscout.source_collectors.agent_mentions import agent_extraction_mentions_from_response

    bundle, _ = captured()
    page = page_data(bundle, SOURCE)
    helpers = {
        "english_title": "Augstein: A German Life",
        "search_translation": "Аугштайн. Немецкая жизнь",
        "author_transliteration_note": "Мартин Дёрри, Хауке Янссен",
        "evidence_note": "Reviewed book row on a captured daily page.",
    }
    corrected = {**AUGSTEIN, **helpers, "title": "Rudolf Augstein", "isbn": "9783406853098"}
    response = {"mentions": [{**AUGSTEIN, **helpers}, corrected]}
    mentions, dropped = agent_extraction_mentions_from_response(page, response, SOURCE, "2026-10-12")
    assert [mention["title"] for mention in mentions] == ["Augstein"]
    assert mentions[0]["primary_authors"] == ["Martin Doerry", "Hauke Janssen"]
    assert len(dropped) == 1


def test_artifact_validators_verify_a_captured_book_link():
    from autoscout.source_collectors import evidence_url_verified

    bundle, _ = captured()
    page = page_data(bundle, SOURCE)
    assert evidence_url_verified(page, AUGSTEIN_URL) == (True, "")
    assert not evidence_url_verified(page, ORIGIN + "/buch/someone/else.html")[0]
    assert not evidence_url_verified({**page, "contract_family": "generic"}, AUGSTEIN_URL)[0]


def test_empty_extraction_from_a_capture_with_rows_is_refused(tmp_path):
    from autoscout.extraction_completeness import capture_listing_row_count
    from autoscout.observed_extraction import validate_extraction_artifact
    from autoscout.source_collectors.contract import extraction_contract

    bundle, _ = captured()
    page = page_data(bundle, SOURCE)
    contract = extraction_contract(SOURCE, source_page=SOURCE_URL, capture_file="capture.json")
    assert capture_listing_row_count(contract, page) == 15
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

    source = {**SOURCE, "contract_family": " Perlentaucher-Daily-Digest "}
    bundle, _ = captured(source)
    page = page_data(bundle, source)
    assert _validate_page_shape(source, page, "2026-10-12") == ""
    assert _validate_page_shape(source, page, "2026-06-10", freshness_reference="page_reference_date") == ""
    assert _validate_page_shape(source, page, "2026-10-13")
    page["source_page"] = "https://example.test/wrong"
    assert _validate_page_shape(source, page, "2026-10-12")


def test_retained_live_capture_carries_the_bundle_and_the_rows(tmp_path):
    from autoscout.source_collectors.base import capture_content_evidence
    from autoscout.source_collectors.base import capture_live_response

    source = {**SOURCE, "capture_live_response_file": "perlentaucher.json"}
    bundle, _ = captured()
    page = page_data(bundle, source)
    capture_live_response(tmp_path / "config.json", tmp_path, source, page)
    retained = json.loads((tmp_path / "perlentaucher.json").read_text())
    assert retained[EVIDENCE_FIELD] == bundle and retained["contract_family"] == FAMILY
    assert len(json.loads(retained["text"])) == 15
    sizes = capture_content_evidence(page)
    assert sizes == {"capture_html_bytes": 0, "capture_content_bytes": len(page["text"].encode())}


def test_live_capture_reads_the_seen_index_and_never_writes_it(tmp_path, monkeypatch):
    import autoscout.source_collectors.agent_fetch as agent

    state = tmp_path / "state" / "seen-items.json"
    state.parent.mkdir()
    state.write_text(json.dumps({"seen_guids": [{"guid": "9783406853098", "first_seen_date": "2026-10-05"}]}))
    before = state.read_bytes()
    source = {**SOURCE, "seen_guids_file": "../state/seen-items.json"}

    def fetch(_source, url, **kwargs):
        assert kwargs["unwrap_response"] is False and kwargs["strict_status"] is True
        body = INDEX if url == SOURCE_URL else PAGES[url.rsplit("/", 1)[1].removesuffix(".html")]
        return body, True, url

    monkeypatch.setattr(agent, "_fetch_bounded_page", fetch)
    page, debug = agent.fetch_agent_source_page(
        tmp_path / "configs" / "live.json", tmp_path, source, reference_date=REFERENCE
    )
    assert debug["perlentaucher_daily_pages"] == DAYS
    assert page[EVIDENCE_FIELD]["seen_items"] == [{"isbn": "9783406853098", "first_seen_date": "2026-10-05"}]
    assert len(json.loads(page["text"])) == 14
    assert state.read_bytes() == before


def test_collected_extraction_records_seen_items_beside_the_replay_config(tmp_path):
    from autoscout.source_collection import _portable_seen_items_file
    from autoscout.source_collectors.codex_extraction import CodexExtractionCollector

    output_dir = tmp_path / "runs" / "weekly"
    output_dir.mkdir(parents=True)
    live = {**SOURCE, "seen_guids_file": "../runs/weekly/perlentaucher-seen-items.json"}
    portable = _portable_seen_items_file(tmp_path / "configs" / "live.json", output_dir, live)
    assert portable == {"seen_guids_file": "perlentaucher-seen-items.json"}
    assert _portable_seen_items_file(tmp_path / "configs" / "live.json", output_dir, SOURCE) == {}

    bundle, _ = captured()
    page = {**page_data(bundle, SOURCE), "source_name": SOURCE["name"]}
    (output_dir / "capture.json").write_text(json.dumps(page))
    helpers = {
        "english_title": "Augstein: A German Life",
        "search_translation": "Аугштайн. Немецкая жизнь",
        "author_transliteration_note": "Мартин Дёрри, Хауке Янссен",
        "evidence_note": "Reviewed book row on a captured daily page.",
    }
    (output_dir / "extraction.json").write_text(json.dumps({"entries": [{**AUGSTEIN, **helpers}]}))
    replay_source = {
        **SOURCE,
        **portable,
        "extraction_method": "codex-extraction-replay",
        "source_page_fixture_file": "capture.json",
        "extraction_response_file": "extraction.json",
        "codex_artifact_dir": ".",
    }
    result = CodexExtractionCollector().collect(
        output_dir / "codex-replay-config.json", output_dir, replay_source, "2026-10-12"
    )
    assert [mention["title"] for mention in result.mentions] == ["Augstein"]
    assert result.debug_metadata["perlentaucher_new_seen_item_count"] == 15
    recorded = read_seen_items(output_dir / "perlentaucher-seen-items.json")
    # The fourteen rows the extractor left out were decided too, and are not asked about again.
    assert len(recorded) == 15 and recorded["9783406853098"] == "2026-10-12"


def test_probe_retains_a_replayable_bundle_and_builds_grounded_fixtures(tmp_path, monkeypatch):
    import autoscout.source_probe as probe

    class Clock(date):
        @classmethod
        def today(cls):
            return REFERENCE

    class Response:
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

    bundle, requests = captured()
    observed = []

    def opener(request, **kwargs):
        observed.append((request.get_method(), request.full_url))
        url = request.full_url
        return Response(url, INDEX if url == SOURCE_URL else PAGES[url.rsplit("/", 1)[1].removesuffix(".html")])

    monkeypatch.setattr(probe, "date", Clock)
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
    assert code == 0
    assert observed == [("GET", url) for url in requests]
    retained = json.loads(next((tmp_path / "captures").glob("*.json")).read_text())
    assert retained[EVIDENCE_FIELD] == bundle
    assert "Klappentext" not in json.dumps(retained)

    from autoscout.replay_fixtures import build_replay_fixture_pair

    samples = feasibility(retained, SOURCE).samples
    helper_metadata = {
        "version": 1,
        "source_url": SOURCE_URL,
        "language": "de",
        "entries": [
            {
                "title": sample.title,
                "author": sample.author,
                "evidence_url": sample.url,
                "english_title": f"English title {index}",
                "search_translation": f"Поиск книги {index}",
                "author_transliteration_note": f"Автор книги {index}",
            }
            for index, sample in enumerate(samples, 1)
        ],
    }
    pair = build_replay_fixture_pair(
        SOURCE_URL,
        "perlentaucher-buecherschau-des-tages",
        retained,
        signal_strength="minor mention",
        candidate_category="adult nonfiction",
        language="de",
        helper_metadata=helper_metadata,
    )
    assert pair.entry_count == 3
    assert pair.page[EVIDENCE_FIELD] == bundle and pair.page["html"] == ""
    first = pair.extraction["mentions"][0]
    assert first["primary_authors"] == ["Natalie Amiri", "Minu Barati", "Shila Behjat", "Düzen Tekkal"]
    assert first["publisher"] == "Carl Hanser Verlag" and first["publication_year"] == 2026
    assert first["isbn"] == "9783446284876" and first["source_week"] == "2026-10-10"

    from autoscout.source_collectors.agent_mentions import agent_extraction_mentions_from_response

    mentions, dropped = agent_extraction_mentions_from_response(pair.page, pair.extraction, SOURCE, "2026-10-12")
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


def test_probe_reports_a_named_refusal(tmp_path, monkeypatch):
    import autoscout.source_probe as probe

    class Clock(date):
        @classmethod
        def today(cls):
            return date(2026, 11, 20)

    class Response:
        status = 200

        def __init__(self):
            self.headers = {"Content-Type": "text/html; charset=utf-8"}

        def read(self, count):
            return INDEX.encode()[:count]

        def geturl(self):
            return SOURCE_URL

        def close(self):
            pass

    monkeypatch.setattr(probe, "date", Clock)
    report, code = probe.run_source_candidate_probe(
        [SOURCE_URL],
        [SOURCE["name"]],
        ["media/curated list"],
        contract_families=[FAMILY],
        contract_metadata=[SOURCE],
        opener=lambda request, **kwargs: Response(),
        ledger_path=tmp_path / "ledger.json",
        feasibility_dir=tmp_path / "drafts",
    )
    assert code == 1
    assert report["candidates"][0]["outcome"] == "reject"
    assert report["candidates"][0]["reason_code"] == "perlentaucher_stale"


def test_scaffolded_live_overlay_declares_the_seen_index_and_a_full_output_budget(tmp_path):
    from autoscout.source_scaffold import _live_model_output_tokens
    from autoscout.source_scaffold import _live_overlay

    helper = tmp_path / "data" / "helper.json"
    metadata = {"name": SOURCE["name"], "contract_family": FAMILY, "helper_metadata_sha256": "0" * 64}
    state_dir = tmp_path / "runs" / "weekly"
    overlay = _live_overlay(metadata, "perlentaucher", helper, tmp_path / "configs" / "live.json", state_dir)
    assert overlay["seen_guids_file"] == "../runs/weekly/perlentaucher-seen-items.json"
    # A config kept one level deeper still names the same weekly state directory.
    nested = _live_overlay(metadata, "perlentaucher", helper, tmp_path / "configs" / "custom" / "live.json", state_dir)
    assert nested["seen_guids_file"] == "../../runs/weekly/perlentaucher-seen-items.json"
    other = _live_overlay(
        {**metadata, "contract_family": "ipc-top40-csv"}, "top40", helper, tmp_path / "configs" / "live.json", state_dir
    )
    assert "seen_guids_file" not in other
    assert _live_model_output_tokens({"contract_family": FAMILY}) == 8000


def test_capture_allowance_pays_for_every_declared_daily_page():
    from autoscout.runtime_budget import capture_source_allowance_for
    from autoscout.runtime_budget import declared_extra_request_count

    assert declared_extra_request_count(SOURCE) == 3
    assert declared_extra_request_count({**SOURCE, "perlentaucher_max_daily_pages": 8}) == 8
    assert declared_extra_request_count({**SOURCE, "contract_family": "generic"}) == 0
    assert capture_source_allowance_for({**SOURCE, "perlentaucher_max_daily_pages": 8}, 10.0) == 90.0


def test_a_capture_with_nothing_new_counts_no_rows_so_an_empty_extraction_is_correct():
    from autoscout.extraction_completeness import capture_listing_row_count
    from autoscout.source_contribution import source_reports_new_items_only

    first, _ = captured()
    seen = {row["isbn"]: "2026-10-05" for row in validate_bundle(first, SOURCE)}
    bundle, _ = captured(seen=seen)
    page = page_data(bundle, SOURCE)
    assert json.loads(page["text"]) == []
    assert capture_listing_row_count(SOURCE, page) == 0
    assert not feasibility(page, SOURCE).accepted
    assert source_reports_new_items_only({**SOURCE, "seen_guids_file": "seen.json"})


def test_a_refused_entry_keeps_every_undecided_row_out_of_the_seen_index(tmp_path):
    from autoscout.source_collectors.codex_extraction import CodexExtractionCollector

    bundle, _ = captured()
    page = {**page_data(bundle, SOURCE), "source_name": SOURCE["name"]}
    (tmp_path / "capture.json").write_text(json.dumps(page))
    helpers = {
        "english_title": "English title",
        "search_translation": "Поиск книги",
        "author_transliteration_note": "Автор книги",
        "evidence_note": "Reviewed book row on a captured daily page.",
    }
    changed = {
        **AUGSTEIN,
        **helpers,
        "title": "Unsere Väter und wir",
        "primary_authors": ["Natalie Amiri", "Minu Barati", "Shila Behjat", "Düzen Tekkal"],
        "publisher": "Carl Hanser Verlag",
        "isbn": "9783446284876",
        "source_week": "2026-10-10",
        "evidence_url": ORIGIN + "/buch/natalie-amiri-minu-barati-shila-behjat-duezen-tekkal/unsere-vaeter.html",
    }
    (tmp_path / "extraction.json").write_text(json.dumps({"entries": [{**AUGSTEIN, **helpers}, changed]}))
    source = {
        **SOURCE,
        "seen_guids_file": "seen-items.json",
        "extraction_method": "codex-extraction-replay",
        "source_page_fixture_file": "capture.json",
        "extraction_response_file": "extraction.json",
        "codex_artifact_dir": ".",
    }
    result = CodexExtractionCollector().collect(tmp_path / "codex-replay-config.json", tmp_path, source, "2026-10-12")
    assert [mention["title"] for mention in result.mentions] == ["Augstein"]
    assert result.skipped_item_count == 1
    # The refused book, and every row that may stand behind a refusal, is shown again on the next run.
    assert read_seen_items(tmp_path / "seen-items.json") == {"9783406853098": "2026-10-12"}


def test_a_translator_note_on_any_captured_row_of_a_book_excludes_it():
    # Jill Lepore's book is reviewed on 9 and 10 October; strip the note from the newer row only.
    newer = PAGES["2026-10-10"]
    start = newer.index("/buch/jill-lepore/")
    note = newer.index("Aus dem Englischen", start)
    stripped = newer[:note] + newer[note:].replace("Aus dem Englischen", "Mit Abbildungen. Aus dem Englischen", 1)
    bundle, _ = captured(pages={**PAGES, "2026-10-10": stripped})
    flags = [
        row["has_translator_note"]
        for page in bundle["daily_pages"]
        for row in page["rows"]
        if row["isbn"] == "9783406849329"
    ]
    assert flags == [False, True]
    rows = validate_bundle(bundle, SOURCE)
    assert by_isbn(rows)["9783406849329"]["has_translator_note"] is True
    assert "9783406849329" not in by_isbn(qualifying_rows(rows, SOURCE))


def _agent_replay(tmp_path, page, entries, **extra):
    (tmp_path / "page.json").write_text(json.dumps(page))
    (tmp_path / "extraction.json").write_text(json.dumps({"mentions": entries}))
    return {
        **SOURCE,
        "extraction_method": "agent-extraction-replay",
        "source_page_fixture_file": "page.json",
        "extraction_response_file": "extraction.json",
        **extra,
    }


HELPERS = {
    "english_title": "Augstein: A German Life",
    "search_translation": "Аугштайн. Немецкая жизнь",
    "author_transliteration_note": "Мартин Дёрри, Хауке Янссен",
    "evidence_note": "Reviewed book row on a captured daily page.",
}


def test_a_mention_the_collection_stage_refuses_leaves_the_seen_index_untouched(tmp_path):
    from autoscout.source_collection import source_candidates

    bundle, _ = captured()
    page = page_data(bundle, SOURCE)
    without_note = {key: value for key, value in {**AUGSTEIN, **HELPERS}.items() if key != "evidence_note"}
    source = _agent_replay(tmp_path, page, [without_note], seen_guids_file="seen-items.json")
    with pytest.raises(ValueError, match="evidence_note"):
        source_candidates(tmp_path / "config.json", tmp_path, source, "2026-10-12")
    assert not (tmp_path / "seen-items.json").exists()

    complete = _agent_replay(tmp_path, page, [{**AUGSTEIN, **HELPERS}], seen_guids_file="seen-items.json")
    result = source_candidates(tmp_path / "config.json", tmp_path, complete, "2026-10-12")
    assert [mention["title"] for mention in result.mentions] == ["Augstein"]
    assert len(read_seen_items(tmp_path / "seen-items.json")) == 15


def test_agent_extraction_reports_nothing_new_as_a_healthy_empty_result(tmp_path):
    from autoscout.source_collectors.agent_extraction import AgentExtractionCollector
    from autoscout.source_collectors.base import CollectorRunError

    first, _ = captured()
    seen = {row["isbn"]: "2026-10-05" for row in validate_bundle(first, SOURCE)}
    all_seen, _ = captured(seen=seen)
    source = _agent_replay(tmp_path, page_data(all_seen, SOURCE), [])
    result = AgentExtractionCollector("configured").collect(tmp_path / "config.json", tmp_path, source, "2026-10-12")
    assert result.mentions == [] and result.debug_metadata["safe_failure_status"] == "healthy_empty"

    # A capture that holds rows still refuses an empty extraction.
    with_rows = _agent_replay(tmp_path, page_data(first, SOURCE), [])
    with pytest.raises(CollectorRunError, match="returned no mentions"):
        AgentExtractionCollector("configured").collect(tmp_path / "config.json", tmp_path, with_rows, "2026-10-12")


@pytest.mark.parametrize(
    "change",
    [
        {"english_title": ""},
        {"search_translation": "Augstein"},
        {"author_transliteration_note": ""},
    ],
)
def test_a_german_mention_without_valid_helpers_is_refused(change):
    from autoscout.source_collectors.agent_mentions import agent_extraction_mentions_from_response

    bundle, _ = captured()
    page = page_data(bundle, SOURCE)
    response = {"mentions": [{**AUGSTEIN, **HELPERS, **change}]}
    mentions, dropped = agent_extraction_mentions_from_response(page, response, SOURCE, "2026-10-12")
    assert mentions == [] and len(dropped) == 1
