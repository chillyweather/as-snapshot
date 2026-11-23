"""The Spanish National Library in the original-edition library step (issue #1147).

Every BNE answer here is a saved live response under
``tests/fixtures/issue-1147-bne-records``; nothing goes to the network.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from state_store_support import seed_state
from state_store_support import state_snapshot

from autoscout.__main__ import main
from autoscout.library_catalogue import BNE
from autoscout.library_catalogue import BNE_REQUEST_INTERVAL_SECONDS
from autoscout.library_catalogue import best_record
from autoscout.library_catalogue import bne_search_url
from autoscout.library_catalogue import parse_bne
from autoscout.original_edition import STATUS_CONFIRMED
from autoscout.original_edition import STATUS_LIBRARY_STATED
from autoscout.original_edition import validate_original_edition
from autoscout.original_edition_completion import complete_originals

FIXTURES = Path(__file__).parent / "fixtures" / "issue-1147-bne-records"
TODAY = date(2026, 11, 19)
OL_PREFIX = "https://openlibrary.org/search.json"

LEYES_ISBN = "9788419558916"  # Las leyes de la competición: e-book record without 041, print record with it.
SAPIENS_ISBN = "9788499926223"  # Sapiens: Hebrew original, romanised 240 title.
SAPIENS_THIN_ISBN = "9788466347518"  # A provisional record with neither 041 nor 240.
CIEN_ISBN = "9788497592208"  # Cien años de soledad: a 240 uniform title without $l.
EBOOK_ISBN = "9788410313064"  # Las leyes de la competición, e-book only: 240 $l, no 041.

PRINT_LEYES_URL = "https://catalogo.bne.es/discovery/fulldisplay/alma991061032209108606/34BNE_INST:CATALOGO"
EBOOK_LEYES_URL = "https://catalogo.bne.es/discovery/fulldisplay/alma991061410339108606/34BNE_INST:CATALOGO"
OL_LEYES_DOCS = {
    "docs": [
        {
            "key": "/works/OL9W",
            "title": "Competition Demystified",
            "author_name": ["Bruce Greenwald", "Judd Kahn"],
            "first_publish_year": 2005,
            "language": ["eng"],
        }
    ]
}


def _fixture(isbn: str) -> str:
    [path] = FIXTURES.glob(f"bne-{isbn}-*.xml")
    return path.read_text(encoding="utf-8")


def _record(
    *,
    isbn: str = LEYES_ISBN,
    candidate_id: str = "c-1",
    title: str = "Las leyes de la competición",
    author: str = "Bruce Greenwald",
    language: str = "es",
    **extra: Any,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "identity_key": f"{title.casefold()}|{author.casefold()}",
        "candidate_id": candidate_id,
        "title": title,
        "primary_authors": [author],
        "publisher": "Deusto",
        "publication_year": 2024,
        "isbn": isbn,
        "source_evidence": [{"source_name": "Lista", "language": language}],
    }
    record.update(extra)
    return record


class FakeGetter:
    def __init__(self, pages: dict[str, str] | None = None) -> None:
        self.pages = pages or {}
        self.requests: list[str] = []

    def get(self, url: str, *, timeout: float | None = None) -> str | None:
        del timeout
        self.requests.append(url)
        for prefix, body in self.pages.items():
            if url.startswith(prefix):
                return body
        return None


class FakeProposer:
    def __init__(self, proposals: dict[str, dict[str, Any]] | None = None) -> None:
        self.proposals = proposals or {}
        self.calls: list[list[dict[str, Any]]] = []

    def propose(self, items: Any, *, timeout: float) -> Any:
        del timeout
        self.calls.append([dict(item) for item in items])
        return self.proposals


class FakeClock:
    """A monotonic clock that a sleep advances, so spacing is proven without waiting."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _bne_requests(getter: FakeGetter) -> list[str]:
    return [url for url in getter.requests if url.startswith("https://catalogo.bne.es/")]


# ── The records ──────────────────────────────────────────────────────────


def test_search_url_asks_the_sru_interface_by_normalized_isbn():
    assert bne_search_url("978-84-19558-91-6") == (
        "https://catalogo.bne.es/view/sru/34BNE_INST?version=1.2&operation=searchRetrieve"
        "&recordSchema=marcxml&query=alma.isbn%3D9788419558916"
    )


def test_bne_serves_spanish_lists_and_only_spanish_isbns():
    assert BNE.serves(["es"]) and not BNE.serves(["de", "fr", "en"])
    assert BNE.holds("9788419558916") and BNE.holds("9791387688769")
    # Argentina and Mexico: not the Spanish legal-deposit range.
    assert not BNE.holds("9789500773690") and not BNE.holds("9786313013401")
    assert BNE.min_request_interval_seconds == BNE_REQUEST_INTERVAL_SECONDS == 10.0


def test_print_record_041_states_language_title_and_pages():
    records = parse_bne(_fixture(LEYES_ISBN))
    best = best_record(records)

    assert len(records) == 2
    assert best is not None
    assert (best.states_translation, best.original_language, best.original_title, best.page_count) == (
        True,
        "en",
        "Competition demystified",
        519,
    )
    assert best.record_url == PRINT_LEYES_URL


def test_hebrew_original_is_stated_with_its_romanised_title():
    best = best_record(parse_bne(_fixture(SAPIENS_ISBN)))

    assert best is not None
    assert (best.states_translation, best.original_language, best.page_count) == (True, "he", 492)
    assert best.original_title == "Ḳitsur toldot ha-enoshut"


def test_uniform_title_with_language_subfield_states_a_translation_without_a_language():
    [record] = parse_bne(_fixture(EBOOK_ISBN))

    assert record.states_translation
    assert record.original_language == ""
    assert record.original_title == "Competition demystified"
    assert record.record_url == EBOOK_LEYES_URL


def test_uniform_title_of_a_spanish_work_states_nothing():
    records = parse_bne(_fixture(CIEN_ISBN))

    assert records and not any(record.states_translation for record in records)
    assert all(record.original_title == "" for record in records)
    assert best_record(records).page_count == 494


def test_greek_text_editions_with_041_indicator_0_state_nothing():
    records = parse_bne((FIXTURES / "bne-iliada-greek-original-text.xml").read_text(encoding="utf-8"))

    assert len(records) == 4
    assert not any(record.states_translation for record in records)


def test_catalan_original_of_a_spanish_translation_is_stated():
    records = parse_bne((FIXTURES / "bne-yo-confieso-catalan.xml").read_text(encoding="utf-8"))
    best = best_record(records)

    assert all(record.states_translation for record in records)
    assert best is not None
    assert (best.original_language, best.original_title) == ("ca", "Jo confesso")


def test_isbn_the_bne_does_not_hold_is_an_empty_answer():
    assert parse_bne(_fixture("9788401039690")) == []


def test_thin_record_states_nothing_and_garbage_is_no_answer():
    [record] = parse_bne(_fixture(SAPIENS_THIN_ISBN))

    assert not record.states_translation and record.original_title == ""
    assert parse_bne("<html>Verificación de seguridad</html>") is None


# ── The pass ─────────────────────────────────────────────────────────────


def test_bne_title_confirmed_by_open_library_saves_the_model_call():
    record = _record()
    getter = FakeGetter({bne_search_url(LEYES_ISBN): _fixture(LEYES_ISBN), OL_PREFIX: json.dumps(OL_LEYES_DOCS)})
    proposer = FakeProposer()

    report = complete_originals([record], getter=getter, proposer=proposer, today=TODAY, sleeper=_no_sleep)

    assert proposer.calls == []
    assert (report.answered_by_library, report.answered_by_model, report.confirmed) == (1, 0, 1)
    edition = record["original_edition"]
    assert validate_original_edition(edition)
    assert edition["status"] == STATUS_CONFIRMED
    assert (edition["original_title"], edition["original_language"]) == ("Competition Demystified", "en")
    assert edition["translation_evidence_url"] == PRINT_LEYES_URL
    assert (record["page_count"], record["page_count_source_url"]) == (519, PRINT_LEYES_URL)


def test_bne_original_open_library_does_not_confirm_is_stored_as_library_stated():
    record = _record(isbn=SAPIENS_ISBN, title="Sapiens", author="Yuval Noah Harari")
    getter = FakeGetter({bne_search_url(SAPIENS_ISBN): _fixture(SAPIENS_ISBN), OL_PREFIX: json.dumps({"docs": []})})
    proposer = FakeProposer()

    report = complete_originals([record], getter=getter, proposer=proposer, today=TODAY, sleeper=_no_sleep)

    assert proposer.calls == []
    assert report.library_stated == 1
    edition = record["original_edition"]
    assert validate_original_edition(edition)
    assert edition["status"] == STATUS_LIBRARY_STATED
    assert edition["is_translation"] == "yes"
    assert (edition["original_language"], edition["original_title"]) == ("he", "Ḳitsur toldot ha-enoshut")


def test_translation_without_a_stated_language_goes_to_the_model_with_the_record_as_credit():
    record = _record(isbn=EBOOK_ISBN)
    getter = FakeGetter({bne_search_url(EBOOK_ISBN): _fixture(EBOOK_ISBN), OL_PREFIX: json.dumps({"docs": []})})
    proposer = FakeProposer({})

    report = complete_originals([record], getter=getter, proposer=proposer, today=TODAY, sleeper=_no_sleep)

    [[item]] = proposer.calls
    assert item["stated_original_title"] == "Competition demystified"
    assert item["translator_credited"] is True
    assert (report.answered_by_library, report.credited) == (0, 1)
    edition = record["original_edition"]
    assert edition["original_language"] in ("", None)
    assert edition["translation_evidence_url"] == EBOOK_LEYES_URL


def test_spanish_original_falls_back_to_the_model_and_never_says_no():
    record = _record(isbn=CIEN_ISBN, title="Cien años de soledad", author="Gabriel García Márquez")
    getter = FakeGetter({bne_search_url(CIEN_ISBN): _fixture(CIEN_ISBN)})
    proposer = FakeProposer({})

    report = complete_originals([record], getter=getter, proposer=proposer, today=TODAY, sleeper=_no_sleep)

    assert len(proposer.calls) == 1
    assert (report.answered_by_library, report.no_proposal) == (0, 1)
    assert record["original_edition"]["is_translation"] == "unknown"
    assert record["page_count"] == 494


def test_latin_american_isbn_on_a_spanish_list_does_not_ask_the_bne():
    record = _record(isbn="9789500773690", title="El arte de envejecer")
    getter = FakeGetter()

    complete_originals([record], getter=getter, proposer=FakeProposer({}), today=TODAY, sleeper=_no_sleep)

    assert _bne_requests(getter) == []


def test_bne_requests_are_spaced_by_the_robots_crawl_delay():
    records = [
        _record(isbn=LEYES_ISBN, candidate_id="c-1"),
        _record(isbn=CIEN_ISBN, candidate_id="c-2", title="Cien años de soledad", author="García Márquez"),
        # The same ISBN again is answered from the pass's cache: no request, no wait.
        _record(isbn=LEYES_ISBN, candidate_id="c-3", title="Las leyes", author="Judd Kahn"),
    ]
    getter = FakeGetter({"https://catalogo.bne.es/": _fixture(CIEN_ISBN)})
    clock = FakeClock()

    complete_originals(
        records, getter=getter, proposer=FakeProposer({}), today=TODAY, monotonic=clock, sleeper=clock.sleep
    )

    assert len(_bne_requests(getter)) == 2
    assert clock.sleeps == [BNE_REQUEST_INTERVAL_SECONDS]


def test_a_crawl_delay_past_the_budget_stops_the_pass_instead_of_waiting():
    records = [_record(isbn=LEYES_ISBN, candidate_id="c-1"), _record(isbn=CIEN_ISBN, candidate_id="c-2")]
    getter = FakeGetter({"https://catalogo.bne.es/": _fixture(CIEN_ISBN)})
    clock = FakeClock()

    report = complete_originals(
        records,
        getter=getter,
        proposer=FakeProposer({}),
        today=TODAY,
        budget_seconds=5.0,
        monotonic=clock,
        sleeper=clock.sleep,
    )

    assert len(_bne_requests(getter)) == 1
    assert clock.sleeps == []
    # Both books stop short: the first before its proposal, the second before its library request.
    assert report.budget_exhausted == 2
    assert "original_edition" not in records[1]


def test_unreachable_bne_is_reported_for_the_bne_alone():
    record = _record()
    getter = FakeGetter()  # Every request fails.

    report = complete_originals([record], getter=getter, proposer=FakeProposer({}), today=TODAY, sleeper=_no_sleep)

    assert report.library_unavailable == {"bne": 1}
    assert report.no_proposal == 1


def test_command_preview_counts_bne_answers_with_the_other_libraries(tmp_path, capsys, monkeypatch):
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "run"
    state_dir.mkdir()
    output_dir.mkdir()
    record = _record()
    seed_state(state_dir, candidate_history=[record])
    seed_state(state_dir, latest_scouting_candidates=[{"identity_key": record["identity_key"]}])
    (output_dir / "scouting-candidates.json").write_text(
        json.dumps([{"identity_key": record["identity_key"]}]), encoding="utf-8"
    )
    getter = FakeGetter({bne_search_url(LEYES_ISBN): _fixture(LEYES_ISBN), OL_PREFIX: json.dumps(OL_LEYES_DOCS)})
    monkeypatch.setattr("autoscout.cli.common.weekly_run_in_progress", lambda state_dir: False)
    monkeypatch.setattr("autoscout.original_edition_completion.BoundedPageGetter", lambda: getter)
    monkeypatch.setattr("autoscout.original_edition_completion.CodexOriginalTitleProposer", lambda: FakeProposer())
    before = state_snapshot(state_dir)

    code = main(
        [
            "complete-originals",
            "--state-dir",
            str(state_dir),
            "--output-dir",
            str(output_dir),
            "--run-date",
            TODAY.isoformat(),
        ]
    )
    output = capsys.readouterr().out

    assert code == 3
    assert state_snapshot(state_dir) == before
    assert "answered_by_library=1" in output
    assert "answered_by_model=0" in output


def _no_sleep(seconds: float) -> None:
    del seconds
