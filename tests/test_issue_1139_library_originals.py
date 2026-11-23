"""National-library lookups by ISBN before the original-edition model (issue #1139).

Every library answer here is a saved response under
``tests/fixtures/issue-1139-library-records``; nothing goes to the network.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_snapshot

from autoscout.__main__ import main
from autoscout.library_catalogue import ALL_LIBRARY_CATALOGUES
from autoscout.library_catalogue import BNF
from autoscout.library_catalogue import DNB
from autoscout.library_catalogue import LIBRARY_CATALOGUES
from autoscout.library_catalogue import bnf_search_url
from autoscout.library_catalogue import dnb_search_url
from autoscout.library_catalogue import parse_bnf
from autoscout.library_catalogue import parse_dnb
from autoscout.original_edition import ORIGINAL_EDITION_KEYS
from autoscout.original_edition import STATUS_CONFIRMED
from autoscout.original_edition import STATUS_LIBRARY_STATED
from autoscout.original_edition import build_original_edition
from autoscout.original_edition import library_statement
from autoscout.original_edition import validate_original_edition
from autoscout.original_edition_completion import LIBRARY_FAILURE_LIMIT
from autoscout.original_edition_completion import complete_originals
from autoscout.original_edition_completion import run_original_edition_completion
from autoscout.original_edition_presentation import format_original_edition

FIXTURES = Path(__file__).parent / "fixtures" / "issue-1139-library-records"
TODAY = date(2026, 11, 18)
OL_PREFIX = "https://openlibrary.org/search.json"

HAIG_ISBN = "9783426282564"
KEHLMANN_ISBN = "9783498035280"
SAPIENS_ISBN = "9782226257017"
ANOMALIE_ISBN = "9782072895098"

DNB_HAIG_URL = "https://d-nb.info/1213640713"
BNF_SAPIENS_URL = "http://catalogue.bnf.fr/ark:/12148/cb44403557s"
OL_HAIG_DOCS = {
    "docs": [
        {
            "key": "/works/OL2W",
            "title": "The Midnight Library",
            "author_name": ["Matt Haig"],
            "first_publish_year": 2020,
            "language": ["eng", "ger"],
        }
    ]
}


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _record(
    *,
    key: str = "die mitternachtsbibliothek|matt haig",
    candidate_id: str = "candidate-001",
    title: str = "Die Mitternachtsbibliothek",
    author: str = "Matt Haig",
    isbn: str = HAIG_ISBN,
    language: str = "de",
    **extra: Any,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "identity_key": key,
        "candidate_id": candidate_id,
        "title": title,
        "primary_authors": [author],
        "publisher": "Droemer",
        "publication_year": 2021,
        "isbn": isbn,
        "source_evidence": [{"source_name": "List", "language": language}],
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


def _complete(records: list[dict[str, Any]], getter: FakeGetter, proposer: FakeProposer | None = None) -> Any:
    # Every library, the paused DNB included, so its lookup stays tested while paused (#1145).
    return complete_originals(
        records, getter=getter, proposer=proposer or FakeProposer(), today=TODAY, libraries=ALL_LIBRARY_CATALOGUES
    )


# ── The library records ──────────────────────────────────────────────────


def test_search_urls_ask_by_normalized_isbn():
    assert dnb_search_url("978-3-426-28256-4") == (
        "https://services.dnb.de/sru/dnb?version=1.1&operation=searchRetrieve"
        "&query=num%3D9783426282564&recordSchema=MARC21-xml"
    )
    assert bnf_search_url("978-2-226-25701-7") == (
        "https://catalogue.bnf.fr/api/SRU?version=1.2&operation=searchRetrieve"
        "&query=bib.isbn%20adj%20%229782226257017%22"
    )


def test_each_library_serves_only_its_list_languages():
    # The Spanish National Library joined in #1147.
    assert [library.key for library in ALL_LIBRARY_CATALOGUES] == ["dnb", "bnf", "bne"]
    assert DNB.serves(["de"]) and not DNB.serves(["fr", "es"])
    assert BNF.serves(["es", "fr"]) and not BNF.serves(["de"])


def test_dnb_translation_record_states_language_title_and_pages():
    [record] = parse_dnb(_fixture("dnb-9783426282564-translation.xml"))

    assert record.states_translation is True
    assert record.original_language == "en"
    assert record.original_title == "The midnight library"
    assert record.page_count == 320
    assert record.record_url == DNB_HAIG_URL


def test_bnf_translation_through_an_intermediate_language_keeps_the_original_language_only():
    # Sapiens: French from the English version of a Hebrew original. Field
    # 454 names the English title, which is not the original's.
    [record] = parse_bnf(_fixture("bnf-9782226257017-translation.xml"))

    assert record.states_translation is True
    assert record.original_language == "he"
    assert record.original_title == ""
    assert record.page_count == 501
    assert record.record_url == BNF_SAPIENS_URL


def test_bnf_translation_without_intermediate_language_takes_the_original_title():
    body = _fixture("bnf-9782226257017-translation.xml").replace('<mxc:subfield code="b">eng</mxc:subfield>\n', "")

    [record] = parse_bnf(body)

    assert record.original_title == "Sapiens : a brief history of humankind"


def test_records_of_originals_state_no_translation_but_keep_pages():
    [german] = parse_dnb(_fixture("dnb-9783498035280-original.xml"))
    [french] = parse_bnf(_fixture("bnf-9782072895098-original.xml"))

    assert (german.states_translation, german.original_language, german.page_count) == (False, "", 302)
    assert (french.states_translation, french.original_language, french.page_count) == (False, "", 332)


def test_no_record_is_an_empty_answer_and_garbage_is_no_answer():
    assert parse_bnf(_fixture("bnf-9783421045953-no-record.xml")) == []
    assert parse_dnb(_fixture("dnb-no-record.xml")) == []
    diagnostics = (
        '<searchRetrieveResponse xmlns="http://www.loc.gov/zing/srw/"><numberOfRecords>0</numberOfRecords>'
        "<diagnostics><diagnostic><message>bad query</message></diagnostic></diagnostics>"
        "</searchRetrieveResponse>"
    )
    for body in ("<html><body>Service unavailable</body></html>", "not xml", diagnostics):
        assert parse_dnb(body) is None
        assert parse_bnf(body) is None


# ── The stored block ─────────────────────────────────────────────────────


def test_library_stated_block_validates_and_keeps_its_evidence():
    block = build_original_edition(
        credit_url=DNB_HAIG_URL,
        confirmation=library_statement(original_title="The midnight library", original_language="en"),
    )

    assert set(block) == set(ORIGINAL_EDITION_KEYS)
    assert validate_original_edition(block)
    assert block["status"] == STATUS_LIBRARY_STATED
    assert block["is_translation"] == "yes"
    assert block["translation_evidence_url"] == DNB_HAIG_URL
    assert not validate_original_edition({**block, "is_translation": "unknown", "translation_evidence_url": ""})
    assert not validate_original_edition({**block, "original_language": ""})
    assert not validate_original_edition({**block, "original_first_published_year": 2020})


def test_library_stated_original_is_shown_with_the_library_record_link():
    titled = build_original_edition(
        credit_url=DNB_HAIG_URL,
        confirmation=library_statement(original_title="The midnight library", original_language="en"),
    )
    untitled = build_original_edition(
        credit_url=BNF_SAPIENS_URL, confirmation=library_statement(original_title="", original_language="he")
    )

    assert format_original_edition({"original_edition": titled}) == (
        "«The midnight library» — английский",
        DNB_HAIG_URL,
        "Каталог национальной библиотеки",
    )
    assert format_original_edition({"original_edition": untitled})[:2] == (
        "Перевод, язык оригинала — иврит",
        BNF_SAPIENS_URL,
    )


# ── The pass ─────────────────────────────────────────────────────────────


def test_library_title_confirmed_by_open_library_saves_the_model_call():
    record = _record()
    getter = FakeGetter(
        {
            dnb_search_url(HAIG_ISBN): _fixture("dnb-9783426282564-translation.xml"),
            OL_PREFIX: json.dumps(OL_HAIG_DOCS),
        }
    )
    proposer = FakeProposer()

    report = _complete([record], getter, proposer)

    assert proposer.calls == []
    assert (report.answered_by_library, report.answered_by_model, report.confirmed) == (1, 0, 1)
    edition = record["original_edition"]
    assert validate_original_edition(edition)
    assert edition["status"] == STATUS_CONFIRMED
    assert edition["original_title"] == "The Midnight Library"
    assert edition["original_language"] == "en"
    assert edition["translation_evidence_url"] == DNB_HAIG_URL
    assert edition["confirmation_url"] == "https://openlibrary.org/works/OL2W"
    assert (record["page_count"], record["page_count_source_url"]) == (320, DNB_HAIG_URL)
    assert record["english_title"] == "The Midnight Library"


def test_library_original_open_library_does_not_confirm_is_stored_as_library_stated():
    record = _record()
    getter = FakeGetter(
        {
            dnb_search_url(HAIG_ISBN): _fixture("dnb-9783426282564-translation.xml"),
            OL_PREFIX: json.dumps({"docs": []}),
        }
    )
    proposer = FakeProposer()

    report = _complete([record], getter, proposer)

    assert proposer.calls == []
    assert (report.library_stated, report.not_found, report.confirmed) == (1, 0, 0)
    edition = record["original_edition"]
    assert validate_original_edition(edition)
    assert edition["status"] == STATUS_LIBRARY_STATED
    assert (edition["original_title"], edition["original_language"]) == ("The midnight library", "en")
    assert edition["translation_evidence_url"] == DNB_HAIG_URL
    assert "english_title" not in record


def test_library_language_without_title_is_stored_without_open_library_or_model():
    record = _record(title="Sapiens", author="Yuval Noah Harari", isbn=SAPIENS_ISBN, language="fr")
    getter = FakeGetter({bnf_search_url(SAPIENS_ISBN): _fixture("bnf-9782226257017-translation.xml")})
    proposer = FakeProposer()

    report = _complete([record], getter, proposer)

    assert proposer.calls == []
    assert getter.requests == [bnf_search_url(SAPIENS_ISBN)]
    assert report.library_stated == 1
    edition = record["original_edition"]
    assert (edition["status"], edition["original_language"], edition["original_title"]) == (
        STATUS_LIBRARY_STATED,
        "he",
        "",
    )
    assert record["page_count"] == 501


def test_record_silent_about_translation_falls_back_to_the_model_and_never_says_no():
    record = _record(
        title="L'anomalie", author="Hervé Le Tellier", isbn=ANOMALIE_ISBN, language="fr", candidate_id="c-2"
    )
    getter = FakeGetter({bnf_search_url(ANOMALIE_ISBN): _fixture("bnf-9782072895098-original.xml")})
    proposer = FakeProposer({})

    report = _complete([record], getter, proposer)

    assert [item["candidate_id"] for item in proposer.calls[0]] == ["c-2"]
    assert (report.answered_by_library, report.answered_by_model, report.no_proposal) == (0, 0, 1)
    edition = record["original_edition"]
    assert edition["is_translation"] == "unknown"
    assert edition["status"] != STATUS_LIBRARY_STATED
    assert record["page_count"] == 332


def test_model_answer_is_counted_when_no_library_answers():
    record = _record(isbn=KEHLMANN_ISBN, candidate_id="c-3")
    getter = FakeGetter(
        {
            dnb_search_url(KEHLMANN_ISBN): _fixture("dnb-no-record.xml"),
            OL_PREFIX: json.dumps(OL_HAIG_DOCS),
        }
    )
    proposer = FakeProposer({"c-3": {"original_title": "The Midnight Library", "original_language": "en"}})

    report = _complete([record], getter, proposer)

    assert (report.answered_by_library, report.answered_by_model, report.confirmed) == (0, 1, 1)
    assert "page_count" not in record


def test_unreachable_library_is_reported_alone_and_stops_being_asked():
    records = [
        _record(key=f"book {index}|matt haig", candidate_id=f"c-{index}", isbn=HAIG_ISBN)
        for index in range(LIBRARY_FAILURE_LIMIT + 2)
    ]
    getter = FakeGetter()  # Every request fails.
    proposer = FakeProposer({})

    report = _complete(records, getter, proposer)

    assert report.library_unavailable == {"dnb": LIBRARY_FAILURE_LIMIT + 2}
    assert report.as_dict()["library_unavailable"] == {"dnb": LIBRARY_FAILURE_LIMIT + 2}
    # The same ISBN is fetched once (cached); a different ISBN per book shows the limit.
    distinct = [
        _record(key=f"other {index}|a b", candidate_id=f"d-{index}", isbn=isbn)
        for index, isbn in enumerate(("9780306406157", "9781861972712", "9780140449136", "9780262033848", HAIG_ISBN))
    ]
    getter = FakeGetter()
    report = _complete(distinct, getter, FakeProposer({}))
    library_requests = [url for url in getter.requests if url.startswith("https://services.dnb.de/")]
    assert len(library_requests) == LIBRARY_FAILURE_LIMIT
    assert report.library_unavailable == {"dnb": len(distinct)}
    assert report.no_proposal == len(distinct)


def test_paused_dnb_is_not_asked_by_the_nightly_pass():
    # The DNB refuses every request since 2026-11-18 (#1145): the default pass leaves it out.
    assert [library.key for library in LIBRARY_CATALOGUES] == ["bnf", "bne"]
    getter = FakeGetter({dnb_search_url(HAIG_ISBN): _fixture("dnb-9783426282564-translation.xml")})

    report = complete_originals([_record()], getter=getter, proposer=FakeProposer({}), today=TODAY)

    assert not any("dnb.de" in url for url in getter.requests)
    assert report.answered_by_library == 0 and report.library_unavailable == {}


def test_book_from_a_list_no_library_serves_asks_no_library():
    record = _record(language="it")
    getter = FakeGetter()

    _complete([record], getter, FakeProposer({}))

    assert not any("dnb.de" in url or "bnf.fr" in url or "bne.es" in url for url in getter.requests)


def test_book_without_isbn_asks_no_library():
    record = _record(isbn="")
    getter = FakeGetter()

    _complete([record], getter, FakeProposer({}))

    assert not any("dnb.de" in url for url in getter.requests)


def test_existing_page_count_is_never_replaced():
    record = _record(page_count=300)
    getter = FakeGetter({dnb_search_url(HAIG_ISBN): _fixture("dnb-9783426282564-translation.xml")})

    report = _complete([record], getter)

    assert record["page_count"] == 300
    assert "page_count_source_url" not in record
    assert report.page_count_applied == 0


# ── State and the command ────────────────────────────────────────────────


def _workspace(tmp_path: Path, history: list[dict[str, Any]]) -> tuple[Path, Path]:
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "run"
    state_dir.mkdir()
    output_dir.mkdir()
    keys = [record["identity_key"] for record in history]
    seed_state(state_dir, candidate_history=history)
    seed_state(state_dir, latest_scouting_candidates=[{"identity_key": key} for key in keys])
    (output_dir / "scouting-candidates.json").write_text(
        json.dumps([{"identity_key": key} for key in keys]), encoding="utf-8"
    )
    return state_dir, output_dir


def _sapiens() -> dict[str, Any]:
    # A French-list book: the default pass leaves out the paused DNB (#1145).
    return _record(
        key="sapiens|yuval noah harari", title="Sapiens", author="Yuval Noah Harari", isbn=SAPIENS_ISBN, language="fr"
    )


def test_library_answer_and_page_count_are_persisted_to_history_and_snapshots(tmp_path):
    state_dir, output_dir = _workspace(tmp_path, [_sapiens()])
    getter = FakeGetter(
        {
            bnf_search_url(SAPIENS_ISBN): _fixture("bnf-9782226257017-translation.xml"),
            OL_PREFIX: json.dumps({"docs": []}),
        }
    )

    result = run_original_edition_completion(
        output_dir,
        state_dir,
        effective_date=TODAY.isoformat(),
        scope="undecided",
        getter=getter,
        proposer=FakeProposer(),
    )

    assert result["answered_by_library"] == 1 and result["library_stated"] == 1
    [record] = read_state(state_dir, "candidate_history")
    assert record["original_edition"]["status"] == STATUS_LIBRARY_STATED
    assert record["page_count"] == 501
    for rows in (
        json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8")),
        read_state(state_dir, "latest_scouting_candidates"),
    ):
        assert rows[0]["original_edition"]["status"] == STATUS_LIBRARY_STATED
        assert rows[0]["page_count"] == 501


def test_command_preview_says_how_many_books_a_library_and_the_model_answered(tmp_path, capsys, monkeypatch):
    model_book = _record(key="other|matt haig", candidate_id="c-9", isbn="", language="de")
    state_dir, output_dir = _workspace(tmp_path, [_sapiens(), model_book])
    getter = FakeGetter(
        {
            bnf_search_url(SAPIENS_ISBN): _fixture("bnf-9782226257017-translation.xml"),
            OL_PREFIX: json.dumps(OL_HAIG_DOCS),
        }
    )
    proposer = FakeProposer({"c-9": {"original_title": "The Midnight Library", "original_language": "en"}})
    monkeypatch.setattr("autoscout.cli.common.weekly_run_in_progress", lambda state_dir: False)
    monkeypatch.setattr("autoscout.original_edition_completion.BoundedPageGetter", lambda: getter)
    monkeypatch.setattr("autoscout.original_edition_completion.CodexOriginalTitleProposer", lambda: proposer)
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
    assert "answered_by_model=1" in output
    assert "library_unavailable=0" in output
