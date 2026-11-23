"""The Russian-edition completion pass, through its run function and its command (#1181)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import unquote_plus

import pytest
from russian_edition_support import BLOCKED
from russian_edition_support import LITRES_EMPTY
from russian_edition_support import LIVELIB_EMPTY
from russian_edition_support import RSL_EMPTY
from russian_edition_support import TODAY
from russian_edition_support import WIKIDATA_EMPTY
from russian_edition_support import FakeClock
from russian_edition_support import FakeFetcher
from russian_edition_support import FakeMatcher
from russian_edition_support import answered
from russian_edition_support import book
from russian_edition_support import by_id
from russian_edition_support import fixture
from russian_edition_support import history
from russian_edition_support import litres_answer
from russian_edition_support import livelib_page
from russian_edition_support import recorded
from russian_edition_support import routed
from russian_edition_support import run
from russian_edition_support import workspace
from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_snapshot

from autoscout.__main__ import main
from autoscout.russian_edition import CONFIRMED_KEYS
from autoscout.russian_edition import MATCH_RESPONSE_SCHEMA
from autoscout.russian_edition import validate_russian_edition
from autoscout.russian_edition_completion import MATCH_ENTRY_FIELDS
from autoscout.russian_edition_completion import MATCH_ITEM_FIELDS
from autoscout.russian_edition_completion import REPORT_FILENAME
from autoscout.russian_edition_completion import CodexSameWorkMatcher
from autoscout.russian_edition_completion import run_russian_edition_completion
from autoscout.russian_edition_sources import SOURCE_LITRES
from autoscout.russian_edition_sources import SOURCE_LIVELIB
from autoscout.russian_edition_sources import SOURCE_RSL
from autoscout.russian_edition_sources import SOURCE_WIKIDATA

CLEAR = "c-clear"
ATTIA = "c-attia"


def _clear(**extra: Any) -> dict[str, Any]:
    return book(CLEAR, "Atomic Habits: An Easy & Proven Way", "James Clear", first_year=2018, **extra)


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ── Confirmed, unconfirmed, no find ──────────────────────────────────────


def test_a_livelib_find_that_both_catalogues_show_is_confirmed_with_high_confidence(tmp_path):
    state_dir, output_dir = workspace(tmp_path, [_clear()])
    fetcher = FakeFetcher(recorded("atomic-habits"))

    result = run(state_dir, output_dir, fetcher=fetcher, matcher=FakeMatcher({CLEAR: "Атомные привычки"}))

    record = history(state_dir)[0]
    edition = record["russian_edition"]
    assert validate_russian_edition(edition) and set(edition) == set(CONFIRMED_KEYS)
    assert edition["status"] == "confirmed"
    assert edition["russian_title"] == "Атомные привычки: как приобрести хорошие привычки и избавиться от плохих"
    assert edition["russian_author"] == "Джеймс Клир"
    assert edition["sources"] == [SOURCE_LITRES, SOURCE_LIVELIB, SOURCE_RSL]
    assert edition["confidence"] == "high"
    assert edition["evidence_url"].startswith("https://search.rsl.ru/ru/record/")
    assert (edition["publisher"], edition["checked_on"]) == ("Питер", TODAY)
    assert record["russian_edition_lookup_date"] == TODAY
    assert record["russian_author_name"] == "Джеймс Клир"
    # The LiveLib name made Wikidata unnecessary, and the subtitle stayed out of the search.
    assert fetcher.asked(SOURCE_WIKIDATA) == []
    assert fetcher.asked(SOURCE_LIVELIB) == ["https://www.livelib.ru/find/books/Atomic%20Habits"]
    # The State Library is asked by author and the located Russian title, without its subtitle, and by author alone.
    assert fetcher.asked(SOURCE_RSL) == ["author:(Клир Джеймс) AND title:(Атомные привычки)", "author:(Клир Джеймс)"]
    assert (result["confirmed"], result["unconfirmed"], result["no_find"]) == (1, 0, 0)
    assert result["projected"] == {"current": 1, "latest": 1}
    for rows in (_read(output_dir / "scouting-candidates.json"), read_state(state_dir, "latest_scouting_candidates")):
        assert rows[0]["russian_edition"] == edition
        assert rows[0]["russian_author_name"] == "Джеймс Клир"
    report = _read(output_dir / REPORT_FILENAME)
    assert report["date"] == TODAY and report["confirmed"] == 1
    assert report["sources"][SOURCE_LIVELIB] == {"answered": 1, "blocked": 0, "failed": 0, "not_asked": 0}


def test_the_author_route_confirms_when_livelib_is_empty_and_wikidata_gives_the_name(tmp_path):
    attia = book(ATTIA, "Outlive", "Peter Attia", first_year=2023)
    state_dir, output_dir = workspace(tmp_path, [attia])
    litres = litres_answer(
        {
            "title": "Жить дольше. Наука и искусство долголетия",
            "authors": ["Питер Аттиа"],
            "date_written_at": "2023-01-01",
        }
    )
    fetcher = FakeFetcher(recorded("outlive", livelib=LIVELIB_EMPTY, litres=litres))

    result = run(state_dir, output_dir, fetcher=fetcher, matcher=FakeMatcher({ATTIA: "Жить дольше"}))

    record = history(state_dir)[0]
    assert record["russian_edition"]["status"] == "confirmed"
    assert record["russian_edition"]["confidence"] == "medium"
    assert record["russian_edition"]["sources"] == [SOURCE_LITRES]
    assert record["russian_edition"]["evidence_url"].startswith("https://www.litres.ru/book/")
    assert record["russian_author_name"] == "Питер Аттиа"
    # One Wikidata request per author; without a located title the State Library is asked by author alone.
    assert len(fetcher.asked(SOURCE_WIKIDATA)) == 1
    assert fetcher.asked(SOURCE_RSL) == ["author:(Аттиа Питер)"]
    assert result["author_name_learned"] == 1


def test_a_state_library_record_stating_a_translation_alone_gives_high_confidence(tmp_path):
    walker = book("c-walker", "Why We Sleep", "Matthew Walker", first_year=2017)
    state_dir, output_dir = workspace(tmp_path, [walker])

    run(
        state_dir,
        output_dir,
        fetcher=FakeFetcher(recorded("why-we-sleep")),
        matcher=FakeMatcher({"c-walker": "Зачем мы спим: новая наука о сне"}),
        enabled_sources=[SOURCE_LIVELIB, SOURCE_WIKIDATA, SOURCE_RSL],
    )

    edition = history(state_dir)[0]["russian_edition"]
    assert edition["sources"] == [SOURCE_RSL]
    assert edition["confidence"] == "high"
    assert (edition["year"], edition["publisher"], edition["isbn"]) == (2018, "Колибри", "978-5-389-13666-3")


def test_one_catalogue_without_a_stated_translation_gives_medium_confidence(tmp_path):
    burkeman = book("c-burkeman", "Four Thousand Weeks", "Oliver Burkeman", first_year=2021)
    state_dir, output_dir = workspace(tmp_path, [burkeman])

    run(
        state_dir,
        output_dir,
        fetcher=FakeFetcher(recorded("four-thousand-weeks")),
        matcher=FakeMatcher({"c-burkeman": "Четыре тысячи недель"}),
    )

    edition = history(state_dir)[0]["russian_edition"]
    assert (edition["status"], edition["confidence"], edition["sources"]) == ("confirmed", "medium", [SOURCE_LITRES])


def test_a_livelib_find_no_catalogue_confirms_is_unconfirmed(tmp_path):
    harari = book("c-harari", "Nexus", "Yuval Noah Harari", first_year=2024)
    state_dir, output_dir = workspace(tmp_path, [harari])
    # The recorded LiveLib results for Nexus are all summaries; this one is the edition itself.
    located = livelib_page(("Nexus. Краткая история информационных сетей", "Юваль Ной Харари", "/book/1-nexus"))
    fetcher = FakeFetcher(recorded("nexus", livelib=located))

    result = run(state_dir, output_dir, fetcher=fetcher, matcher=FakeMatcher({"c-harari": "Nexus"}))

    record = history(state_dir)[0]
    edition = record["russian_edition"]
    assert edition["status"] == "unconfirmed"
    assert edition["russian_author"] == "Юваль Ной Харари"
    # The LiveLib spelling is kept because the book ended unconfirmed with it.
    assert record["russian_author_name"] == "Юваль Ной Харари"
    assert edition["evidence_url"].startswith("https://www.livelib.ru/book/")
    assert validate_russian_edition(edition)
    assert result["unconfirmed"] == 1


def test_the_model_answering_none_records_no_find(tmp_path):
    state_dir, output_dir = workspace(tmp_path, [_clear()])

    result = run(state_dir, output_dir, fetcher=FakeFetcher(recorded("atomic-habits")), matcher=FakeMatcher({}))

    record = history(state_dir)[0]
    assert record["russian_edition"] == {"status": "no_find", "checked_on": TODAY}
    assert record["russian_edition_lookup_date"] == TODAY
    assert result["no_find"] == 1


def test_a_confirmed_litres_edition_gets_isbn_and_publisher_from_its_detail(tmp_path):
    state_dir, output_dir = workspace(tmp_path, [_clear()])
    fetcher = FakeFetcher(recorded("atomic-habits", rsl=RSL_EMPTY))

    result = run(state_dir, output_dir, fetcher=fetcher, matcher=FakeMatcher({CLEAR: "Атомные привычки"}))

    edition = history(state_dir)[0]["russian_edition"]
    assert edition["sources"] == [SOURCE_LITRES, SOURCE_LIVELIB]
    assert edition["evidence_url"].endswith("-48514275/")
    # LitRes dates the original work, so the record carries no year from it.
    assert (edition["isbn"], edition["publisher"], edition["year"]) == ("978-5-4461-1216-6", "Питер (Айлиб)", None)
    assert fetcher.asked(SOURCE_LITRES)[-1] == "https://api.litres.ru/foundation/api/arts/48514275"
    assert result["sources"][SOURCE_LITRES]["answered"] == 2


def test_a_failed_litres_detail_leaves_the_edition_confirmed_without_isbn_and_publisher(tmp_path):
    state_dir, output_dir = workspace(tmp_path, [_clear()])

    def answer(source: str, target: str) -> Any:
        if source == SOURCE_LITRES and "/arts/" in target:
            return BLOCKED
        return recorded("atomic-habits", rsl=RSL_EMPTY)(source, target)

    result = run(state_dir, output_dir, fetcher=FakeFetcher(answer), matcher=FakeMatcher({CLEAR: "Атомные привычки"}))

    edition = history(state_dir)[0]["russian_edition"]
    assert edition["status"] == "confirmed"
    assert (edition["isbn"], edition["publisher"]) == ("", "")
    assert result["sources"][SOURCE_LITRES] == {"answered": 1, "blocked": 1, "failed": 0, "not_asked": 0}


# ── The Cyrillic spelling ────────────────────────────────────────────────


def test_the_published_spelling_comes_first_and_a_known_second_one_is_tried(tmp_path):
    housel = book(
        "c-housel",
        "The Psychology of Money",
        "Morgan Housel",
        first_year=2020,
        russian_author_name="Морган Хаусел",
    )
    state_dir, output_dir = workspace(tmp_path, [housel])
    litres = litres_answer({"title": "Психология денег", "authors": ["Морган Хаусел"]})

    def answer(source: str, target: str) -> Any:
        if source == SOURCE_LITRES:
            return litres if "%D0%A5%D0%B0%D1%83%D1%81" in target else LITRES_EMPTY
        return recorded("psychology-of-money", rsl=RSL_EMPTY)(source, target)

    fetcher = FakeFetcher(answer)
    run(state_dir, output_dir, fetcher=fetcher, matcher=FakeMatcher({"c-housel": "Психология денег"}))

    # LiveLib's «Морган Хаузел» first, then the stored «Морган Хаусел».
    assert fetcher.asked(SOURCE_RSL) == [
        "author:(Хаузел Морган) AND title:(Психология денег)",
        "author:(Хаузел Морган)",
        "author:(Хаусел Морган) AND title:(Психология денег)",
        "author:(Хаусел Морган)",
    ]
    assert len([target for target in fetcher.asked(SOURCE_LITRES) if "/search?" in target]) == 2
    assert fetcher.asked(SOURCE_WIKIDATA) == []
    record = history(state_dir)[0]
    assert record["russian_edition"]["status"] == "confirmed"
    assert record["russian_edition"]["russian_author"] == "Морган Хаусел"
    assert record["russian_author_name"] == "Морган Хаусел"


def test_wikidata_is_asked_only_when_neither_livelib_nor_the_stored_name_gives_one(tmp_path):
    stored = book("c-stored", "Outlive", "Peter Attia", russian_author_name="Питер Аттиа")
    state_dir, output_dir = workspace(tmp_path, [stored])
    fetcher = FakeFetcher(recorded("outlive", livelib=LIVELIB_EMPTY))

    run(state_dir, output_dir, fetcher=fetcher, matcher=FakeMatcher({}))

    assert fetcher.asked(SOURCE_WIKIDATA) == []
    assert fetcher.asked(SOURCE_RSL) == ["author:(Аттиа Питер)"]


def test_another_author_with_the_same_surname_never_reaches_the_model(tmp_path):
    greene = book("c-greene", "The 48 Laws of Power", "Robert Greene", first_year=1998)
    state_dir, output_dir = workspace(tmp_path, [greene])
    litres = litres_answer(
        {"title": "48 законов власти", "authors": ["Роберт Грин"]},
        {"title": "Виноваты звёзды", "authors": ["Джон Грин"]},
    )
    matcher = FakeMatcher({"c-greene": "Виноваты звёзды"})

    run(state_dir, output_dir, fetcher=FakeFetcher(recorded("48-laws", litres=litres)), matcher=matcher)

    titles = [entry["title"] for entry in matcher.calls[0][0]["russian_titles"]]
    assert "Виноваты звёзды" not in titles
    assert history(state_dir)[0]["russian_edition"]["status"] == "no_find"


def test_a_livelib_book_by_another_author_with_the_same_surname_is_not_kept(tmp_path):
    greene = book("c-greene", "The 48 Laws of Power", "Robert Greene", first_year=1998)
    state_dir, output_dir = workspace(tmp_path, [greene])
    green = livelib_page(("Виноваты звёзды", "Джон Грин", "/book/1-vinovaty-zvezdy"))
    fetcher = FakeFetcher(recorded("48-laws", livelib=green, wikidata=WIKIDATA_EMPTY))

    result = run(state_dir, output_dir, fetcher=fetcher, matcher=FakeMatcher({"c-greene": "Виноваты звёзды"}))

    # Wikidata is still asked, and no catalogue is searched for «Джон Грин».
    assert len(fetcher.asked(SOURCE_WIKIDATA)) == 1
    searched = [unquote_plus(target) for target in fetcher.asked(SOURCE_RSL) + fetcher.asked(SOURCE_LITRES)]
    assert not any("Джон" in target for target in searched)
    record = history(state_dir)[0]
    assert "russian_author_name" not in record
    assert record["russian_edition"]["status"] == "no_find"
    assert result["author_name_learned"] == 0


def test_a_wrong_stored_name_does_not_veto_the_right_livelib_result(tmp_path):
    greene = book("c-greene", "The 48 Laws of Power", "Robert Greene", first_year=1998, russian_author_name="Джон Грин")
    state_dir, output_dir = workspace(tmp_path, [greene])
    fetcher = FakeFetcher(recorded("48-laws"))

    run(state_dir, output_dir, fetcher=fetcher, matcher=FakeMatcher({"c-greene": "48 законов власти"}))

    assert fetcher.asked(SOURCE_RSL)[0] == "author:(Грин Роберт) AND title:(48 законов власти)"
    assert history(state_dir)[0]["russian_edition"]["status"] == "confirmed"


def test_a_litres_edition_in_russian_with_a_latin_title_is_confirmed(tmp_path):
    harari = book("c-harari", "Nexus", "Yuval Noah Harari", first_year=2024)
    state_dir, output_dir = workspace(tmp_path, [harari])
    litres = litres_answer({"title": "Nexus", "authors": ["Юваль Ной Харари"]})
    fetcher = FakeFetcher(recorded("nexus", livelib=LIVELIB_EMPTY, rsl=RSL_EMPTY, litres=litres))

    run(state_dir, output_dir, fetcher=fetcher, matcher=FakeMatcher({"c-harari": "Nexus"}))

    edition = history(state_dir)[0]["russian_edition"]
    assert (edition["status"], edition["russian_title"], edition["sources"]) == ("confirmed", "Nexus", [SOURCE_LITRES])
    assert validate_russian_edition(edition)


def test_a_litres_entry_with_a_latin_title_in_another_language_is_not_confirmed(tmp_path):
    harari = book("c-harari", "Nexus", "Yuval Noah Harari", first_year=2024)
    state_dir, output_dir = workspace(tmp_path, [harari])
    litres = litres_answer({"title": "Nexus", "authors": ["Юваль Ной Харари"], "language": "en"})
    fetcher = FakeFetcher(recorded("nexus", livelib=LIVELIB_EMPTY, rsl=RSL_EMPTY, litres=litres))

    run(state_dir, output_dir, fetcher=fetcher, matcher=FakeMatcher({"c-harari": "Nexus"}))

    assert history(state_dir)[0]["russian_edition"]["status"] == "no_find"


# ── Guards ───────────────────────────────────────────────────────────────


def _one_rsl_record(slug: str, title: str, replacement: str) -> str:
    """The first record of a recorded State Library answer, with its title rewritten."""
    data = json.loads(fixture(f"{slug}.rsl-author.json"))
    head, first, *_ = data["content"].split('<div class="row search-container')
    data["content"] = head + '<div class="row search-container' + first.replace(title, replacement) + "</div>"
    data["TotalHits"] = 1
    return json.dumps(data, ensure_ascii=False)


def test_an_abridged_state_library_edition_is_not_confirmed(tmp_path):
    walker = book("c-walker", "Why We Sleep", "Matthew Walker", first_year=2017)
    state_dir, output_dir = workspace(tmp_path, [walker])
    abridged = _one_rsl_record("why-we-sleep", "[18+]", "[сокращённое издание] : [18+]")
    fetcher = FakeFetcher(recorded("why-we-sleep", rsl=abridged, litres=LITRES_EMPTY, livelib=LIVELIB_EMPTY))
    matcher = FakeMatcher({"c-walker": "Зачем мы спим"})

    run(state_dir, output_dir, fetcher=fetcher, matcher=matcher)

    # The note is gone from the cleaned title, but the eligibility check read it:
    # the abridgement is never listed, so nothing is left to ask the model.
    assert matcher.calls == []
    assert history(state_dir)[0]["russian_edition"]["status"] == "no_find"


def test_a_summary_named_by_the_model_is_not_confirmed(tmp_path):
    state_dir, output_dir = workspace(tmp_path, [_clear()])
    litres = litres_answer(
        {"title": "Атомные привычки. Саммари", "authors": ["Джеймс Клир"], "date_written_at": "2020"}
    )
    fetcher = FakeFetcher(recorded("atomic-habits", livelib=LIVELIB_EMPTY, rsl=RSL_EMPTY, litres=litres))

    result = run(
        state_dir,
        output_dir,
        fetcher=fetcher,
        matcher=FakeMatcher({CLEAR: "Атомные привычки. Саммари"}),
    )

    record = history(state_dir)[0]
    assert record["russian_edition"]["status"] == "no_find"
    assert record["russian_author_name"] == "Джеймс Клир"
    assert result["confirmed"] == 0


def test_an_edition_dated_before_the_original_is_not_confirmed_even_with_an_undated_litres_entry(tmp_path):
    # «Стакан всегда наполовину полон» is in the State Library from 2020 and on LitRes without a year.
    haidt = book("c-haidt", "The Anxious Generation", "Jonathan Haidt", first_year=2024)
    state_dir, output_dir = workspace(tmp_path, [haidt])

    run(
        state_dir,
        output_dir,
        fetcher=FakeFetcher(recorded("anxious-generation")),
        matcher=FakeMatcher({"c-haidt": "Стакан всегда наполовину полон"}),
    )

    assert history(state_dir)[0]["russian_edition"]["status"] == "no_find"


def test_another_book_by_the_same_author_is_not_confirmed_when_the_model_answers_none(tmp_path):
    # No first year: the earlier books stay listed, and only the model tells them apart.
    haidt = book("c-haidt", "A New Book", "Jonathan Haidt")
    state_dir, output_dir = workspace(tmp_path, [haidt])
    matcher = FakeMatcher({"c-haidt": None})

    run(
        state_dir,
        output_dir,
        fetcher=FakeFetcher(recorded("anxious-generation", livelib=LIVELIB_EMPTY)),
        matcher=matcher,
    )

    titles = [entry["title"] for entry in matcher.calls[0][0]["russian_titles"]]
    assert any(title.startswith("Стакан всегда наполовину полон") for title in titles)
    assert history(state_dir)[0]["russian_edition"]["status"] == "no_find"


def test_non_russian_entries_and_other_authors_never_reach_the_model(tmp_path):
    greene = book("c-greene", "The 48 Laws of Power", "Robert Greene", first_year=1998)
    state_dir, output_dir = workspace(tmp_path, [greene])
    matcher = FakeMatcher({"c-greene": "48 законов власти"})

    run(state_dir, output_dir, fetcher=FakeFetcher(recorded("48-laws")), matcher=matcher)

    titles = [entry["title"] for entry in matcher.calls[0][0]["russian_titles"]]
    assert not any("Hakimiyyətin" in title or "USTALIQ" in title for title in titles)
    assert not any(title.startswith("Ключевые идеи книги") for title in titles)
    assert not any(title.startswith("Ривермен") for title in titles)
    edition = history(state_dir)[0]["russian_edition"]
    assert (edition["status"], edition["confidence"]) == ("confirmed", "high")
    assert edition["sources"] == [SOURCE_LITRES, SOURCE_RSL]


def test_a_livelib_result_whose_author_does_not_correspond_is_dropped(tmp_path):
    walker = book("c-walker", "Why We Sleep", "Matthew Walker")
    state_dir, output_dir = workspace(tmp_path, [walker])
    other = livelib_page(("О чем молчат женщины", "Эйда Кэлхун", "/book/1-o-chem"))
    fetcher = FakeFetcher(
        recorded("why-we-sleep", livelib=other, wikidata=WIKIDATA_EMPTY, rsl=RSL_EMPTY, litres=LITRES_EMPTY)
    )
    matcher = FakeMatcher({"c-walker": "О чем молчат женщины"})

    result = run(state_dir, output_dir, fetcher=fetcher, matcher=matcher)

    assert matcher.calls == []
    record = history(state_dir)[0]
    assert record["russian_edition"]["status"] == "no_find"
    assert "russian_author_name" not in record
    assert fetcher.asked(SOURCE_RSL) == fetcher.asked(SOURCE_LITRES) == []
    assert result["no_find"] == 1


# ── Failures ─────────────────────────────────────────────────────────────


def test_no_find_needs_an_edition_search_that_answered(tmp_path):
    neus = book("c-neus", "24 Hours at the Capitol", "Nora Neus", first_year=2023)
    state_dir, output_dir = workspace(tmp_path, [neus])
    fetcher = FakeFetcher(recorded("24-hours-capitol"))

    # LiveLib is off and Wikidata gives no name: nothing searched for an edition.
    result = run(
        state_dir,
        output_dir,
        fetcher=fetcher,
        matcher=FakeMatcher({}),
        enabled_sources=[SOURCE_WIKIDATA, SOURCE_RSL, SOURCE_LITRES],
    )

    record = history(state_dir)[0]
    assert "russian_edition" not in record and "russian_edition_lookup_date" not in record
    assert record["russian_edition_attempt_date"] == TODAY
    assert (result["no_find"], result["not_completed"]) == (0, 1)


@pytest.mark.parametrize("enabled", [[], [SOURCE_WIKIDATA], [SOURCE_LIVELIB, SOURCE_WIKIDATA]])
def test_the_run_refuses_a_selection_without_a_catalogue(tmp_path, enabled):
    state_dir, output_dir = workspace(tmp_path, [_clear()])
    before = state_snapshot(state_dir)

    with pytest.raises(ValueError, match="catalogue"):
        run_russian_edition_completion(
            output_dir, state_dir, effective_date=TODAY, fetcher=FakeFetcher({}), enabled_sources=enabled
        )

    assert state_snapshot(state_dir) == before


def test_a_state_library_answer_with_hits_but_no_readable_record_leaves_the_book_open(tmp_path):
    state_dir, output_dir = workspace(tmp_path, [_clear()])
    unreadable = json.dumps({"TotalHits": 3, "content": '<div class="row rsl-search-results"></div>'})
    fetcher = FakeFetcher(recorded("atomic-habits", rsl=unreadable, litres=LITRES_EMPTY))

    result = run(state_dir, output_dir, fetcher=fetcher, matcher=FakeMatcher({}))

    assert "russian_edition" not in history(state_dir)[0]
    # Both State Library queries (by author and title, by author alone) came back unreadable.
    assert result["sources"][SOURCE_RSL]["failed"] == 2
    assert result["not_completed"] == 1


def _four_books() -> list[dict[str, Any]]:
    return [book(f"c-{index}", f"Book {index}", f"Author Number{index}") for index in range(4)]


def test_a_source_that_blocks_three_times_is_not_asked_again_and_its_books_stay_open(tmp_path):
    state_dir, output_dir = workspace(tmp_path, _four_books())
    ddos = answered(fixture("livelib-ddos-guard.html"))
    fetcher = FakeFetcher(
        {SOURCE_LIVELIB: ddos, SOURCE_WIKIDATA: WIKIDATA_EMPTY, SOURCE_RSL: RSL_EMPTY, SOURCE_LITRES: LITRES_EMPTY}
    )

    result = run(state_dir, output_dir, fetcher=fetcher, matcher=FakeMatcher({}))

    assert len(fetcher.asked(SOURCE_LIVELIB)) == 3
    assert result["sources"][SOURCE_LIVELIB] == {"answered": 0, "blocked": 3, "failed": 0, "not_asked": 1}
    assert result["not_completed"] == 4 and result["no_find"] == 0
    for record in history(state_dir):
        assert "russian_edition" not in record
        assert "russian_edition_lookup_date" not in record


# ── LiveLib's rate limit ─────────────────────────────────────────────────

LIVELIB_CEILING_ROUTES = {
    "Atomic Habits": "atomic-habits",
    "James Clear": "atomic-habits",
    "Клир": "atomic-habits",
    "The 48 Laws of Power": "48-laws",
    "Robert Greene": "48-laws",
    "Грин": "48-laws",
    "Why We Sleep": "why-we-sleep",
    "Matthew Walker": "why-we-sleep",
    "Уолкер": "why-we-sleep",
    "24 Hours at the Capitol": "24-hours-capitol",
    "Nora Neus": "24-hours-capitol",
}


def test_past_the_livelib_ceiling_books_are_answered_through_wikidata_and_the_catalogues(tmp_path):
    books = [
        _clear(),
        book("c-greene", "The 48 Laws of Power", "Robert Greene", first_year=1998),
        book("c-walker", "Why We Sleep", "Matthew Walker", first_year=2017),
        book("c-neus", "24 Hours at the Capitol", "Nora Neus", first_year=2023),
    ]
    # The books of the current snapshot come first and spend the ceiling.
    state_dir, output_dir = workspace(tmp_path, books, [str(record["identity_key"]) for record in books[:2]])
    fetcher = FakeFetcher(routed(LIVELIB_CEILING_ROUTES))
    matcher = FakeMatcher({CLEAR: "Атомные привычки", "c-greene": "48 законов власти", "c-walker": "Зачем мы спим"})

    result = run(state_dir, output_dir, fetcher=fetcher, matcher=matcher, max_livelib_searches=2)

    assert fetcher.asked(SOURCE_LIVELIB) == [
        "https://www.livelib.ru/find/books/Atomic%20Habits",
        "https://www.livelib.ru/find/books/The%2048%20Laws%20of%20Power",
    ]
    assert result["sources"][SOURCE_LIVELIB] == {"answered": 2, "blocked": 0, "failed": 0, "not_asked": 2}
    records = by_id(state_dir)
    assert records[CLEAR]["russian_edition"]["status"] == "confirmed"
    assert records["c-greene"]["russian_edition"]["status"] == "confirmed"
    # Wikidata gave Walker's name; without a located title the State Library is asked by author alone.
    assert records["c-walker"]["russian_edition"]["status"] == "confirmed"
    assert records["c-walker"]["russian_author_name"] == "Мэттью Уолкер"
    assert "author:(Уолкер Мэттью)" in fetcher.asked(SOURCE_RSL)
    # No name from Wikidata and no LiveLib search: asked again on a later night.
    neus = records["c-neus"]
    assert "russian_edition" not in neus and "russian_edition_lookup_date" not in neus
    assert neus["russian_edition_attempt_date"] == TODAY
    assert (result["confirmed"], result["not_completed"]) == (3, 1)


def test_a_repeated_livelib_search_is_answered_from_the_cache_and_not_counted(tmp_path):
    # The same lookup title: the subtitle stays out of the search.
    twins = [_clear(), book("c-twin", "Atomic Habits: Tiny Changes", "James Clear", first_year=2018)]
    state_dir, output_dir = workspace(tmp_path, [*twins, book("c-walker", "Why We Sleep", "Matthew Walker")])
    fetcher = FakeFetcher(routed(LIVELIB_CEILING_ROUTES))

    result = run(state_dir, output_dir, fetcher=fetcher, matcher=FakeMatcher({}), max_livelib_searches=2)

    assert len(fetcher.asked(SOURCE_LIVELIB)) == 2
    assert result["sources"][SOURCE_LIVELIB]["not_asked"] == 0


def test_after_livelib_blocks_three_times_books_with_a_known_name_still_complete(tmp_path):
    unnamed = [book(f"c-{index}", f"Book {index}", f"Author Number{index}") for index in range(3)]
    stored = book("c-attia", "Outlive", "Peter Attia", russian_author_name="Питер Аттиа")
    walker = book("c-walker", "Why We Sleep", "Matthew Walker", first_year=2017)
    state_dir, output_dir = workspace(
        tmp_path, [*unnamed, stored, walker], [str(record["identity_key"]) for record in unnamed]
    )
    routes = {"Аттиа": "outlive", **LIVELIB_CEILING_ROUTES}
    fetcher = FakeFetcher(routed(routes, livelib=answered(fixture("livelib-ratelimit.html"))))

    result = run(state_dir, output_dir, fetcher=fetcher, matcher=FakeMatcher({"c-walker": "Зачем мы спим"}))

    assert result["sources"][SOURCE_LIVELIB] == {"answered": 0, "blocked": 3, "failed": 0, "not_asked": 2}
    records = by_id(state_dir)
    assert records["c-attia"]["russian_edition"]["status"] == "no_find"
    assert records["c-attia"]["russian_edition_lookup_date"] == TODAY
    assert records["c-walker"]["russian_edition"]["status"] == "confirmed"
    for index in range(3):
        assert "russian_edition" not in records[f"c-{index}"]
    assert (result["confirmed"], result["no_find"], result["not_completed"]) == (1, 1, 3)


def test_no_name_from_anywhere_and_an_empty_livelib_answer_is_no_find(tmp_path):
    neus = book("c-neus", "24 Hours at the Capitol", "Nora Neus", first_year=2023)
    state_dir, output_dir = workspace(tmp_path, [neus])
    fetcher = FakeFetcher(recorded("24-hours-capitol"))

    result = run(state_dir, output_dir, fetcher=fetcher, matcher=FakeMatcher({}))

    assert history(state_dir)[0]["russian_edition"] == {"status": "no_find", "checked_on": TODAY}
    assert fetcher.asked(SOURCE_RSL) == fetcher.asked(SOURCE_LITRES) == []
    assert result["no_find"] == 1


def test_a_failed_wikidata_leaves_a_book_without_a_name_open_even_when_livelib_answered(tmp_path):
    neus = book("c-neus", "24 Hours at the Capitol", "Nora Neus", first_year=2023)
    state_dir, output_dir = workspace(tmp_path, [neus])

    result = run(
        state_dir,
        output_dir,
        fetcher=FakeFetcher(recorded("24-hours-capitol", wikidata=BLOCKED)),
        matcher=FakeMatcher({}),
    )

    assert "russian_edition" not in history(state_dir)[0]
    assert result["not_completed"] == 1


def test_a_failed_catalogue_still_lets_another_catalogue_confirm(tmp_path):
    state_dir, output_dir = workspace(tmp_path, [_clear()])

    result = run(
        state_dir,
        output_dir,
        fetcher=FakeFetcher(recorded("atomic-habits", litres=BLOCKED)),
        matcher=FakeMatcher({CLEAR: "Атомные привычки"}),
    )

    assert history(state_dir)[0]["russian_edition"]["sources"] == [SOURCE_LIVELIB, SOURCE_RSL]
    assert result["sources"][SOURCE_LITRES]["blocked"] == 1


def test_an_existing_confirmed_record_survives_a_pass_in_which_every_source_fails(tmp_path):
    state_dir, output_dir = workspace(tmp_path, [_clear()])
    run(
        state_dir,
        output_dir,
        fetcher=FakeFetcher(recorded("atomic-habits")),
        matcher=FakeMatcher({CLEAR: "Атомные привычки"}),
    )
    before = history(state_dir)[0]

    result = run(
        state_dir,
        output_dir,
        effective_date="2027-06-01",
        fetcher=FakeFetcher({}),
        matcher=FakeMatcher({}),
    )

    assert history(state_dir)[0] == before
    assert result["skipped_settled"] == 1


def test_a_failed_model_call_leaves_the_book_open(tmp_path):
    state_dir, output_dir = workspace(tmp_path, [_clear()])

    result = run(
        state_dir, output_dir, fetcher=FakeFetcher(recorded("atomic-habits")), matcher=FakeMatcher(result=None)
    )

    record = history(state_dir)[0]
    assert "russian_edition" not in record and "russian_edition_lookup_date" not in record
    # LiveLib's spelling is kept only with a confirmed or unconfirmed edition.
    assert "russian_author_name" not in record
    assert (result["match_unavailable"], result["not_completed"]) == (1, 1)


# ── Selection, ceiling, budget, dry run, recovery ────────────────────────


def test_decided_and_recently_looked_up_books_are_skipped_and_an_old_lookup_is_asked_again(tmp_path):
    decided = book("c-decided", "Decided", "Ann Decided", editor_status="reject")
    recent = book("c-recent", "Recent", "Ann Recent", russian_edition_lookup_date="2026-11-01")
    old = book("c-old", "Old", "Ann Old", russian_edition_lookup_date="2026-10-01")
    state_dir, output_dir = workspace(tmp_path, [decided, recent, old])
    empty = {SOURCE_LIVELIB: LIVELIB_EMPTY, SOURCE_WIKIDATA: WIKIDATA_EMPTY}

    result = run(state_dir, output_dir, scope="current", fetcher=FakeFetcher(empty), matcher=FakeMatcher({}))

    records = by_id(state_dir)
    assert (result["skipped_decided"], result["skipped_settled"], result["no_find"]) == (1, 1, 1)
    assert "russian_edition" not in records["c-decided"] and "russian_edition" not in records["c-recent"]
    assert records["c-old"]["russian_edition_lookup_date"] == TODAY


def test_the_book_ceiling_takes_current_candidates_first_then_never_looked_up_books(tmp_path):
    older = book("c-older", "Older", "Ann Older", russian_edition_lookup_date="2026-09-01")
    fresh = book("c-fresh", "Fresh", "Ann Fresh")
    current = book("c-current", "Current", "Ann Current", russian_edition_lookup_date="2026-08-01")
    state_dir, output_dir = workspace(tmp_path, [older, fresh, current], [str(current["identity_key"])])
    empty = {SOURCE_LIVELIB: LIVELIB_EMPTY, SOURCE_WIKIDATA: WIKIDATA_EMPTY}

    result = run(state_dir, output_dir, fetcher=FakeFetcher(empty), matcher=FakeMatcher({}), max_books=2)

    records = by_id(state_dir)
    assert result["over_ceiling"] == 1
    assert records["c-current"]["russian_edition_lookup_date"] == TODAY
    assert records["c-fresh"]["russian_edition_lookup_date"] == TODAY
    assert records["c-older"]["russian_edition_lookup_date"] == "2026-09-01"


def test_a_book_left_open_tonight_waits_behind_a_never_attempted_book_tomorrow(tmp_path):
    first, second = book("c-first", "First", "Ann First"), book("c-second", "Second", "Ann Second")
    state_dir, output_dir = workspace(tmp_path, [first, second])
    empty = {SOURCE_LIVELIB: LIVELIB_EMPTY, SOURCE_WIKIDATA: WIKIDATA_EMPTY}

    tonight = run(state_dir, output_dir, fetcher=FakeFetcher({}), matcher=FakeMatcher({}), max_books=1)
    tomorrow = run(
        state_dir,
        output_dir,
        effective_date="2026-11-22",
        fetcher=FakeFetcher(empty),
        matcher=FakeMatcher({}),
        max_books=1,
    )

    records = by_id(state_dir)
    assert tonight["not_completed"] == 1 and tomorrow["no_find"] == 1
    assert records["c-first"]["russian_edition_attempt_date"] == TODAY
    assert "russian_edition" not in records["c-first"] and "russian_edition_lookup_date" not in records["c-first"]
    assert records["c-second"]["russian_edition_attempt_date"] == "2026-11-22"
    assert records["c-second"]["russian_edition_lookup_date"] == "2026-11-22"
    # An attempt never settles a book: the next night asks it again.
    third = run(
        state_dir,
        output_dir,
        effective_date="2026-11-23",
        fetcher=FakeFetcher(empty),
        matcher=FakeMatcher({}),
        max_books=1,
    )
    assert (third["skipped_settled"], third["no_find"]) == (1, 1)
    assert by_id(state_dir)["c-first"]["russian_edition_lookup_date"] == "2026-11-23"


def test_the_default_budget_covers_a_full_ceiling_at_the_wikidata_spacing(tmp_path):
    books = [book(f"c-{index}", f"Book {index}", f"Author Number{index}") for index in range(140)]
    state_dir, output_dir = workspace(tmp_path, books)
    empty = {SOURCE_LIVELIB: LIVELIB_EMPTY, SOURCE_WIKIDATA: WIKIDATA_EMPTY}
    clock = FakeClock()

    result = run(state_dir, output_dir, fetcher=FakeFetcher(empty), matcher=FakeMatcher({}), clock=clock)

    # 140 Wikidata requests 7 seconds apart take longer than 15 minutes.
    assert clock.now > 15 * 60
    assert result["budget_exhausted"] == 0
    # Past the LiveLib ceiling a book with no name anywhere stays open.
    assert (result["no_find"], result["not_completed"]) == (25, 115)


def test_the_time_budget_stops_the_pass_and_a_second_run_completes_it(tmp_path):
    state_dir, output_dir = workspace(tmp_path, _four_books())
    empty = {SOURCE_LIVELIB: LIVELIB_EMPTY, SOURCE_WIKIDATA: WIKIDATA_EMPTY}
    clock = FakeClock()

    first = run(
        state_dir,
        output_dir,
        fetcher=FakeFetcher(empty, clock=clock, step=10.0),
        matcher=FakeMatcher({}),
        clock=clock,
        budget_seconds=45.0,
    )
    second = run(state_dir, output_dir, fetcher=FakeFetcher(empty), matcher=FakeMatcher({}))

    assert first["no_find"] == 2 and first["budget_exhausted"] == 2
    assert (second["skipped_settled"], second["no_find"]) == (2, 2)
    assert all(record["russian_edition"]["status"] == "no_find" for record in history(state_dir))


def test_requests_to_one_source_are_spaced_without_real_sleeping(tmp_path):
    state_dir, output_dir = workspace(tmp_path, _four_books()[:3])
    clock = FakeClock()
    times: dict[str, list[float]] = {}

    def answer(source: str, target: str) -> str:
        times.setdefault(source, []).append(clock.now)
        return LIVELIB_EMPTY if source == SOURCE_LIVELIB else WIKIDATA_EMPTY

    run(state_dir, output_dir, fetcher=FakeFetcher(answer), matcher=FakeMatcher({}), clock=clock)

    for source, interval in ((SOURCE_LIVELIB, 6.0), (SOURCE_WIKIDATA, 7.0)):
        gaps = [later - earlier for earlier, later in zip(times[source], times[source][1:], strict=False)]
        assert len(gaps) == 2 and all(gap >= interval for gap in gaps)
    assert clock.sleeps and all(seconds <= 7.0 for seconds in clock.sleeps)


def test_a_newer_record_written_meanwhile_survives_a_pass_that_only_dated_an_attempt(tmp_path):
    old = {"status": "no_find", "checked_on": "2026-09-01"}
    state_dir, output_dir = workspace(tmp_path, [_clear(russian_edition=old, russian_edition_lookup_date="2026-09-01")])
    newer = {
        "status": "unconfirmed",
        "russian_title": "Атомные привычки",
        "russian_author": "Джеймс Клир",
        "evidence_url": "https://www.livelib.ru/book/1003608943-atomnye-privychki",
        "checked_on": TODAY,
    }
    written: list[bool] = []

    def answer(source: str, target: str) -> Any:
        if not written:
            records = history(state_dir)
            records[0].update(russian_edition=newer, russian_edition_lookup_date=TODAY)
            seed_state(state_dir, candidate_history=records)
            written.append(True)
        # Every catalogue fails: the pass learns nothing but its attempt.
        return BLOCKED if source in (SOURCE_RSL, SOURCE_LITRES) else recorded("atomic-habits")(source, target)

    result = run(state_dir, output_dir, fetcher=FakeFetcher(answer), matcher=FakeMatcher({}))

    record = history(state_dir)[0]
    assert result["not_completed"] == 1
    assert record["russian_edition"] == newer
    assert record["russian_edition_lookup_date"] == TODAY
    assert record["russian_edition_attempt_date"] == TODAY


def test_an_older_attempt_date_never_replaces_a_newer_one(tmp_path):
    state_dir, output_dir = workspace(tmp_path, [_clear()])

    def answer(source: str, target: str) -> Any:
        records = history(state_dir)
        if records[0].get("russian_edition_attempt_date") != "2026-12-01":
            records[0]["russian_edition_attempt_date"] = "2026-12-01"
            seed_state(state_dir, candidate_history=records)
        return None

    run(state_dir, output_dir, fetcher=FakeFetcher(answer), matcher=FakeMatcher({}))

    assert history(state_dir)[0]["russian_edition_attempt_date"] == "2026-12-01"


def test_a_dry_run_writes_nothing(tmp_path):
    state_dir, output_dir = workspace(tmp_path, [_clear()])
    before = state_snapshot(state_dir)

    result = run(
        state_dir,
        output_dir,
        fetcher=FakeFetcher(recorded("atomic-habits")),
        matcher=FakeMatcher({CLEAR: "Атомные привычки"}),
        dry_run=True,
    )

    assert result["confirmed"] == 1 and result["dry_run"] is True
    assert state_snapshot(state_dir) == before
    assert not (output_dir / REPORT_FILENAME).exists()
    assert "russian_edition" not in _read(output_dir / "scouting-candidates.json")[0]


# ── What the model sees, what is never touched ───────────────────────────


def test_the_model_input_holds_only_the_allowed_fields(tmp_path):
    editorial = {
        "editor_status": "",
        "editor_note": "EDITOR-NOTE-SECRET",
        "priority_label": "PRIORITY-SECRET",
        "visible_risk_comment": "RISK-SECRET",
        "editorial_profile_notes": ["PROFILE-SECRET"],
        "hidden_by_rule_reason": "VISIBILITY-SECRET",
    }
    state_dir, output_dir = workspace(tmp_path, [_clear(**editorial)])
    matcher = FakeMatcher({CLEAR: "Атомные привычки"})

    run(state_dir, output_dir, fetcher=FakeFetcher(recorded("atomic-habits")), matcher=matcher)

    item = matcher.calls[0][0]
    assert tuple(item) == MATCH_ITEM_FIELDS
    assert all(tuple(entry) == MATCH_ENTRY_FIELDS for entry in item["russian_titles"])
    assert (item["title"], item["subtitle"], item["author"]) == ("Atomic Habits", "An Easy & Proven Way", "James Clear")
    assert item["first_published_year"] == 2018
    assert "SECRET" not in json.dumps(matcher.calls, ensure_ascii=False)


def test_the_editorial_russian_edition_status_and_verification_are_untouched(tmp_path):
    verification = {"check_coverage": "incomplete", "provider_outcome": None}
    state_dir, output_dir = workspace(
        tmp_path, [_clear(russian_edition_status="unclear", russian_edition_verification=verification)]
    )

    run(
        state_dir,
        output_dir,
        fetcher=FakeFetcher(recorded("atomic-habits")),
        matcher=FakeMatcher({CLEAR: "Атомные привычки"}),
    )

    record = history(state_dir)[0]
    assert record["russian_edition"]["status"] == "confirmed"
    assert record["russian_edition_status"] == "unclear"
    assert record["russian_edition_verification"] == verification


# ── The command ──────────────────────────────────────────────────────────


def test_command_previews_without_writing_and_applies_with_apply(tmp_path, capsys, monkeypatch):
    state_dir, output_dir = workspace(tmp_path, [_clear()])
    fetcher = FakeFetcher(recorded("atomic-habits"))
    monkeypatch.setattr("autoscout.cli.common.weekly_run_in_progress", lambda state_dir: False)
    monkeypatch.setattr("autoscout.russian_edition_completion.LiveSourceFetcher", lambda: fetcher)
    monkeypatch.setattr(
        "autoscout.russian_edition_completion.CodexSameWorkMatcher", lambda: FakeMatcher({CLEAR: "Атомные привычки"})
    )
    monkeypatch.setattr("autoscout.russian_edition_completion.time.sleep", lambda seconds: None)
    before = state_snapshot(state_dir)
    arguments = ["complete-russian-editions", "--state-dir", str(state_dir), "--output-dir", str(output_dir)]
    arguments += ["--run-date", TODAY, "--skip-source", "wikidata"]

    preview = main(arguments)
    preview_output = capsys.readouterr().out
    unchanged = state_snapshot(state_dir) == before
    applied = main([*arguments, "--apply"])
    apply_output = capsys.readouterr().out

    assert preview == 3 and unchanged
    assert applied == 0
    assert "status=ok scope=undecided applied=true" in apply_output
    assert "confirmed=1" in apply_output
    assert "source=wikidata enabled=false" in apply_output
    assert "source=livelib enabled=true answered=1 blocked=0 failed=0 not_asked=0" in apply_output
    assert history(state_dir)[0]["russian_edition"]["status"] == "confirmed"
    for text in ("Atomic", "Clear", "Атомные", "Клир"):
        assert text not in preview_output + apply_output


def test_command_takes_a_livelib_search_ceiling(tmp_path, capsys, monkeypatch):
    state_dir, output_dir = workspace(tmp_path, [_clear()])
    fetcher = FakeFetcher(recorded("atomic-habits"))
    monkeypatch.setattr("autoscout.cli.common.weekly_run_in_progress", lambda state_dir: False)
    monkeypatch.setattr("autoscout.russian_edition_completion.LiveSourceFetcher", lambda: fetcher)
    monkeypatch.setattr(
        "autoscout.russian_edition_completion.CodexSameWorkMatcher", lambda: FakeMatcher({CLEAR: "Атомные привычки"})
    )
    monkeypatch.setattr("autoscout.russian_edition_completion.time.sleep", lambda seconds: None)
    arguments = ["complete-russian-editions", "--state-dir", str(state_dir), "--output-dir", str(output_dir)]
    arguments += ["--run-date", TODAY, "--apply"]

    refused = main([*arguments, "--max-livelib-searches", "-1"])
    applied = main([*arguments, "--max-livelib-searches", "0"])
    output = capsys.readouterr().out

    assert refused == 2 and "reason_code=invalid_max_livelib_searches" in output
    assert applied == 0 and fetcher.asked(SOURCE_LIVELIB) == []
    assert "source=livelib enabled=true answered=0 blocked=0 failed=0 not_asked=1" in output
    assert history(state_dir)[0]["russian_edition"]["status"] == "confirmed"


def test_command_refuses_to_run_without_a_catalogue(tmp_path, capsys, monkeypatch):
    state_dir, output_dir = workspace(tmp_path, [_clear()])
    monkeypatch.setattr("autoscout.cli.common.weekly_run_in_progress", lambda state_dir: False)
    before = state_snapshot(state_dir)
    arguments = ["complete-russian-editions", "--state-dir", str(state_dir), "--output-dir", str(output_dir)]

    code = main([*arguments, "--apply", "--skip-source", "rsl", "--skip-source", "litres"])

    assert code == 2 and "reason_code=no_catalogue_source" in capsys.readouterr().out
    assert state_snapshot(state_dir) == before


def test_command_refuses_during_a_weekly_run(tmp_path, monkeypatch):
    state_dir, output_dir = workspace(tmp_path, [_clear()])
    monkeypatch.setattr("autoscout.cli.common.weekly_run_in_progress", lambda state_dir: True)
    before = state_snapshot(state_dir)

    code = main(["complete-russian-editions", "--state-dir", str(state_dir), "--apply"])

    assert code == 6 and state_snapshot(state_dir) == before


# ── The Codex matcher ────────────────────────────────────────────────────


class _Runner:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.items: list[dict[str, Any]] = []
        self.schema: dict[str, Any] = {}

    def __call__(self, command: list[str], **kwargs: Any) -> Any:
        isolated = Path(command[command.index("-C") + 1])
        self.items = json.loads((isolated / "items.json").read_text(encoding="utf-8"))
        self.schema = json.loads((isolated / "response-schema.json").read_text(encoding="utf-8"))
        response_path = Path(command[command.index("--output-last-message") + 1])
        response_path.write_text(json.dumps(self.outcome), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


ITEM = {
    "candidate_id": CLEAR,
    "title": "Atomic Habits",
    "subtitle": "",
    "author": "James Clear",
    "first_published_year": 2018,
    "description": "",
    "russian_titles": [{"number": 1, "title": "Атомные привычки", "publisher": "", "year": 2021, "sources": ["rsl"]}],
}


def test_the_codex_matcher_parses_a_schema_valid_answer():
    runner = _Runner({"matches": [{"candidate_id": CLEAR, "number": 1}]})
    matcher = CodexSameWorkMatcher(runner=runner, model="", reasoning_effort="")

    assert matcher.match([ITEM], timeout=60.0) == {CLEAR: 1}
    assert runner.items == [ITEM]
    assert runner.schema == MATCH_RESPONSE_SCHEMA


def test_the_codex_matcher_answers_none_on_a_malformed_answer():
    runner = _Runner({"matches": [{"candidate_id": CLEAR, "number": "1"}]})
    matcher = CodexSameWorkMatcher(runner=runner, model="", reasoning_effort="")

    assert matcher.match([ITEM], timeout=60.0) is None
