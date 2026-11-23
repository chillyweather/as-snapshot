"""Source parsers, name correspondence, the record, and the regression list of 2026-11-21 (#1181).

The fixtures under ``tests/fixtures/issue-1181-russian-editions`` are the
recorded answers, trimmed to what the parsers read (see the provenance file).
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest
from russian_edition_support import TODAY
from russian_edition_support import FakeFetcher
from russian_edition_support import FakeMatcher
from russian_edition_support import book
from russian_edition_support import fixture
from russian_edition_support import history
from russian_edition_support import recorded
from russian_edition_support import run
from russian_edition_support import workspace

from autoscout.russian_edition import build_no_find
from autoscout.russian_edition import is_retelling
from autoscout.russian_edition import names_correspond
from autoscout.russian_edition import normalized_title
from autoscout.russian_edition import retelling_by_credit
from autoscout.russian_edition import russian_main_title
from autoscout.russian_edition import validate_match_response
from autoscout.russian_edition import validate_russian_edition
from autoscout.russian_edition import wikidata_russian_name
from autoscout.russian_edition_sources import SOURCE_LITRES
from autoscout.russian_edition_sources import SOURCE_RSL
from autoscout.russian_edition_sources import SOURCE_WIKIDATA
from autoscout.russian_edition_sources import EditionEntry
from autoscout.russian_edition_sources import WikidataHit
from autoscout.russian_edition_sources import is_block_page
from autoscout.russian_edition_sources import litres_art_url
from autoscout.russian_edition_sources import litres_search_url
from autoscout.russian_edition_sources import livelib_search_url
from autoscout.russian_edition_sources import parse_litres_art
from autoscout.russian_edition_sources import parse_litres_search
from autoscout.russian_edition_sources import parse_livelib_search
from autoscout.russian_edition_sources import parse_rsl_author_answer
from autoscout.russian_edition_sources import parse_wikidata_search
from autoscout.russian_edition_sources import rsl_author_query
from autoscout.russian_edition_sources import rsl_entries
from autoscout.russian_edition_sources import wikidata_search_url
from autoscout.russian_editions import parse_rsl_live_response

# ── LiveLib ──────────────────────────────────────────────────────────────


def test_livelib_results_give_title_cyrillic_author_and_book_path():
    results = parse_livelib_search(fixture("atomic-habits.livelib-search.html"))

    assert results is not None and len(results) == 1
    assert results[0].title == "Атомные привычки: как приобрести хорошие привычки и избавиться от плохих"
    assert results[0].author == "Джеймс Клир"
    assert results[0].url.startswith("https://www.livelib.ru/book/1003608943-")


def test_a_livelib_result_without_an_author_keeps_an_empty_author():
    results = parse_livelib_search(fixture("48-laws.livelib-search.html"))

    assert results is not None
    assert [(result.title[:17], result.author) for result in results] == [
        ("48 законов власти", "Роберт Грин"),
        ("Summary, Analysis", ""),
    ]


def test_an_empty_livelib_search_is_an_empty_list():
    assert parse_livelib_search(fixture("24-hours-capitol.livelib-search.html")) == []


def test_the_livelib_ddos_guard_page_and_changed_markup_are_unreadable():
    page = fixture("livelib-ddos-guard.html")

    assert parse_livelib_search(page) is None
    assert is_block_page(page)
    assert parse_livelib_search("<html><body><p>New layout</p></body></html>") is None


def test_the_livelib_rate_limit_page_is_a_block():
    page = fixture("livelib-ratelimit.html")

    assert parse_livelib_search(page) is None
    assert is_block_page(page)


def test_the_livelib_query_is_the_title_alone():
    assert (
        livelib_search_url("The 48 Laws of Power") == "https://www.livelib.ru/find/books/The%2048%20Laws%20of%20Power"
    )


# ── Wikidata ─────────────────────────────────────────────────────────────


def _hits(slug: str) -> Any:
    hits = parse_wikidata_search(fixture(f"{slug}.wikidata-search-ru.json"))
    assert hits is not None
    return hits


def test_the_wikidata_search_is_one_request_answered_in_russian():
    url = wikidata_search_url("James Clear")

    assert "action=wbsearchentities" in url and "uselang=ru" in url and "search=James+Clear" in url


def test_wikidata_hits_give_the_matched_name_and_the_russian_label():
    hits = _hits("atomic-habits")

    assert (hits[0].match_text, hits[0].label, hits[0].label_language) == ("James Clear", "Джеймс Клир", "ru")
    assert (hits[3].label, hits[3].label_language) == ("James Cleary", "en")
    assert wikidata_russian_name(hits, "James Clear") == "Джеймс Клир"


def test_wikidata_takes_the_first_hit_with_the_name_and_a_russian_label():
    # The first Matthew Walker (an alias of Matthew C. Walker) has no Russian label.
    assert wikidata_russian_name(_hits("why-we-sleep"), "Matthew Walker") == "Мэттью Уолкер"
    assert wikidata_russian_name(_hits("braiding-sweetgrass"), "Robin Wall Kimmerer") == "Робин Киммерер"


def test_a_talk_named_after_the_author_is_not_the_author():
    hits = _hits("anxious-generation")
    talks = [hit for hit in hits if hit.label.startswith("Джонатан Хайдт ")]

    assert talks and wikidata_russian_name(talks, "Jonathan Haidt") == ""
    assert wikidata_russian_name(hits, "Jonathan Haidt") == "Джонатан Хайдт"


def test_an_author_without_a_russian_label_gives_an_empty_name():
    assert wikidata_russian_name(_hits("augustine-african"), "Catherine Conybeare") == ""
    assert wikidata_russian_name(_hits("24-hours-capitol"), "Nora Neus") == ""
    assert wikidata_russian_name(_hits("why-we-sleep")[:1], "Matthew Walker") == ""


def test_a_wikidata_label_with_another_given_name_is_not_the_author():
    hits = [
        WikidataHit(match_text="Robert Greene", label="Джон Грин", label_language="ru"),
        WikidataHit(match_text="Robert Greene", label="Роберт Грин", label_language="ru"),
    ]

    assert wikidata_russian_name(hits, "Robert Greene") == "Роберт Грин"
    assert wikidata_russian_name(hits[:1], "Robert Greene") == ""


def test_the_wikidata_rate_limit_page_is_unreadable_and_a_block():
    page = fixture("wikidata-rate-limited.txt")

    assert parse_wikidata_search(page) is None
    assert is_block_page(page)


# ── Russian State Library ────────────────────────────────────────────────


def test_the_state_library_author_answer_gives_metadata_only_editions():
    entries = parse_rsl_author_answer(fixture("why-we-sleep.rsl-author.json"), "author:(Уолкер Мэттью)", TODAY)

    assert entries is not None and len(entries) == 4
    first = entries[0]
    assert first.title == "Зачем мы спим: новая наука о сне и сновидениях"
    # The whole author as the catalogue states it, given name included.
    assert first.authors == ("Уолкер, Мэттью",)
    assert (first.language, first.publisher, first.year) == ("ru", "Колибри", 2018)
    assert first.isbn == "978-5-389-13666-3"
    assert first.states_translation is True
    assert first.url == "https://search.rsl.ru/ru/record/01009742196"


def test_a_state_library_record_without_a_translation_statement_says_so():
    entries = parse_rsl_author_answer(fixture("48-laws.rsl-author.json"), "author:(Грин Роберт)", TODAY)

    assert entries is not None
    assert (entries[0].title, entries[0].year, entries[0].states_translation) == ("48 законов власти", 2003, False)


def test_a_long_state_library_title_keeps_publisher_year_and_isbn():
    entries = parse_rsl_author_answer(fixture("braiding-sweetgrass.rsl-author.json"), "author:(Киммерер Робин)", TODAY)

    assert entries is not None
    assert entries[0].title.startswith("Голос земли: легендарный бестселлер")
    assert (entries[0].publisher, entries[0].year, entries[0].isbn) == ("Эксмо, Бомбора", 2022, "978-5-04-102050-7")


def test_existing_callers_of_the_state_library_parser_still_get_240_characters():
    results, error = parse_rsl_live_response(
        fixture("braiding-sweetgrass.rsl-author.json"), "author:(Киммерер Робин)", {}, {"name": "RSL"}, TODAY
    )

    assert error == "" and results
    assert all(len(result["publication_data"]) <= 240 for result in results)
    assert results[0]["publication_data"].endswith("...")


def test_an_empty_state_library_answer_is_an_empty_list():
    assert parse_rsl_author_answer(fixture("outlive.rsl-author.json"), "author:(Аттиа Питер)", TODAY) == []


def test_an_unreadable_state_library_answer_is_none():
    assert parse_rsl_author_answer("{not json", "author:(Аттиа Питер)", TODAY) is None
    assert parse_rsl_author_answer("", "author:(Аттиа Питер)", TODAY) is None


def test_state_library_hits_without_a_readable_record_are_none_and_no_hits_are_an_empty_list():
    content = '<div class="row rsl-search-results"></div>'
    hits = json.dumps({"TotalHits": 3, "content": content})
    none = json.dumps({"TotalHits": 0, "content": content})

    assert parse_rsl_author_answer(hits, "author:(Клир Джеймс)", TODAY) is None
    assert parse_rsl_author_answer(none, "author:(Клир Джеймс)", TODAY) == []


def _rsl_result(language_clue: str) -> dict[str, Any]:
    return {
        "result_title": "Атомные привычки [Текст]",
        "record_url": "https://search.rsl.ru/ru/record/01010704039",
        "result_author": "Клир",
        "publication_data": "Москва : Питер, 2021.",
        "language_clue": language_clue,
    }


@pytest.mark.parametrize(
    ("clue", "language"),
    [("", "ru"), ("русский", "ru"), ("рус.", "ru"), ("Russian", "ru"), ("rus", "ru"), ("ru", "ru")],
)
def test_a_state_library_language_clue_naming_russian_is_russian(clue, language):
    assert rsl_entries([_rsl_result(clue)])[0].language == language


@pytest.mark.parametrize("clue", ["белорусский", "английский", "English", "украинский"])
def test_any_other_stated_language_is_not_russian(clue):
    assert rsl_entries([_rsl_result(clue)])[0].language != "ru"


def test_an_abridgement_note_in_the_state_library_title_is_seen_after_the_clean_up():
    result = {**_rsl_result(""), "result_title": "Зачем мы спим [Текст] : [сокращенное издание] : [18+]"}
    entry = rsl_entries([result])[0]

    assert entry.title == "Зачем мы спим"
    assert is_retelling(entry)


def test_the_state_library_query_is_surname_then_given_name():
    assert rsl_author_query("Робин Уолл Киммерер") == "author:(Киммерер Робин)"


def test_the_state_library_query_adds_the_located_title():
    assert rsl_author_query("Роберт Грин", "48 законов власти") == "author:(Грин Роберт) AND title:(48 законов власти)"
    assert rsl_author_query("Робин Уолл Киммерер", "Голос земли") == "author:(Киммерер Робин) AND title:(Голос земли)"


@pytest.mark.parametrize(
    ("title", "main"),
    [
        ("48 законов власти. Краткая версия", "48 законов власти"),
        ("Атомные привычки: как приобрести хорошие привычки и избавиться от плохих", "Атомные привычки"),
        ("Голос земли. Легендарный бестселлер десятилетия", "Голос земли"),
        ("Тревожное поколение", "Тревожное поколение"),
    ],
)
def test_the_main_russian_title_drops_the_subtitle(title, main):
    assert russian_main_title(title) == main


# ── LitRes ───────────────────────────────────────────────────────────────


def test_litres_answers_give_language_and_authors_but_never_a_year():
    entries = parse_litres_search(fixture("48-laws.litres-author.json"))

    assert entries is not None and len(entries) == 20
    first = entries[0]
    # LitRes dates the original work (1998 here), not the Russian edition.
    assert (first.title, first.language, first.authors, first.year) == (
        "48 законов власти",
        "ru",
        ("Роберт Грин",),
        None,
    )
    assert all(entry.year is None for entry in entries)
    assert first.url.startswith("https://www.litres.ru/book/")
    assert {entry.language for entry in entries} == {"ru", "az"}


def test_litres_summaries_are_retellings():
    entries = parse_litres_search(fixture("48-laws.litres-author.json"))
    assert entries is not None
    by_title = {entry.title: entry for entry in entries}

    assert is_retelling(by_title["Ключевые идеи книги: 48 законов власти. Роберт Грин"])
    assert is_retelling(by_title["48 законов власти. Роберт Грин. Кратко"])
    assert is_retelling(by_title["Мастер игры. Роберт Грин. Саммари"])
    assert not is_retelling(by_title["48 законов власти"])


@pytest.mark.parametrize(
    "title",
    [
        "Атомные привычки. Сокращённое издание",
        "Атомные привычки (сокращенное издание)",
        "Атомные привычки в сокращении",
        "Атомные привычки. Сокращённый перевод",
        "Атомные привычки. Краткий пересказ",
        "Пересказ книги «Атомные привычки»",
        "Атомные привычки. Адаптированное издание",
    ],
)
def test_abridged_and_retold_editions_are_retellings(title):
    entry = EditionEntry(source=SOURCE_LITRES, title=title, authors=("Джеймс Клир",), language="ru", url="")

    assert is_retelling(entry)


def test_a_summary_publisher_credited_as_author_is_a_retelling_by_credit():
    entries = parse_litres_search(fixture("48-laws.litres-author.json"))
    assert entries is not None
    golden = next(entry for entry in entries if entry.title == "Роберт Грин. Золотые правила")

    assert retelling_by_credit(golden, "Роберт Грин")
    assert not retelling_by_credit(entries[0], "Роберт Грин")


def test_an_unreadable_litres_answer_is_none():
    assert parse_litres_search("<html>captcha</html>") is None
    assert parse_litres_art("<html>captcha</html>") is None


def test_the_litres_detail_gives_isbn_and_the_first_rightholder():
    detail = parse_litres_art(fixture("atomic-habits.litres-art-detail.json"))

    assert detail is not None
    assert (detail.isbn, detail.publisher) == ("978-5-4461-1216-6", "Питер (Айлиб)")
    assert litres_art_url("48514275") == "https://api.litres.ru/foundation/api/arts/48514275"


def test_the_litres_query_names_the_author_and_text_books():
    assert litres_search_url("Джеймс Клир") == (
        "https://api.litres.ru/foundation/api/search?q=%D0%94%D0%B6%D0%B5%D0%B9%D0%BC%D1%81+"
        "%D0%9A%D0%BB%D0%B8%D1%80&limit=20&types=text_book"
    )


# ── Names, titles, the record, the model answer ──────────────────────────


@pytest.mark.parametrize(
    ("cyrillic", "latin"),
    [
        ("Хайдт", "Jonathan Haidt"),
        ("Джеймс Клир", "James Clear"),
        ("Роберт Грин", "Robert Greene"),
        ("Робин Уолл Киммерер", "Robin Wall Kimmerer"),
        ("Оливер Бёркман", "Oliver Burkeman"),
        ("Юваль Ной Харари", "Yuval Noah Harari"),
        ("Чарлз Дахигг", "Charles Duhigg"),
        ("Мэттью Уолкер", "Matthew Walker"),
        ("Морган Хаузел", "Morgan Housel"),
        ("Оливер Буркеман", "Оливер Бёркман"),
    ],
)
def test_names_correspond_across_scripts(cyrillic, latin):
    assert names_correspond(cyrillic, latin)


@pytest.mark.parametrize(
    ("left", "right"),
    [("Эйда Кэлхун", "Matthew Walker"), ("Дариан Лидер", "Matthew Walker"), ("Smart Reading", "Роберт Грин"), ("", "")],
)
def test_different_names_do_not_correspond(left, right):
    assert not names_correspond(left, right)


def test_two_cyrillic_names_with_given_names_must_share_the_given_initial():
    assert not names_correspond("Роберт Грин", "Джон Грин")
    assert names_correspond("Робин Уолл Киммерер", "Робин Киммерер")
    # Without a given name on one side, the surname decides.
    assert names_correspond("Грин", "Джон Грин")


@pytest.mark.parametrize(
    ("cyrillic", "latin"),
    [
        ("Джеймс", "James"),
        ("Джонатан", "Jonathan"),
        ("Джон", "John"),
        ("Роберт", "Robert"),
        ("Чарлз", "Charles"),
        ("Мэттью", "Matthew"),
        ("Уолтер", "Walter"),
        ("Юваль", "Yuval"),
        ("Оливер", "Oliver"),
        ("Робин", "Robin"),
        ("Мел", "Mel"),
        ("Брианна", "Brianna"),
        ("Морган", "Morgan"),
        ("Питер", "Peter"),
        ("Мириам", "Miriam"),
        ("Майкл", "Michael"),
        ("Кристофер", "Christopher"),
        ("Стивен", "Stephen"),
        ("Джоан", "J. K."),
    ],
)
def test_given_names_correspond_across_scripts(cyrillic, latin):
    assert names_correspond(f"{cyrillic} Грин", f"{latin} Greene")


@pytest.mark.parametrize(
    ("left", "right"),
    [("Джон Грин", "Robert Greene"), ("Роберт Грин", "John Green"), ("Мэттью Уолкер", "Alice Walker")],
)
def test_another_given_name_across_scripts_does_not_correspond(left, right):
    assert not names_correspond(left, right)


def test_without_a_given_name_on_one_side_the_surname_decides_across_scripts():
    assert names_correspond("Грин", "Robert Greene")
    assert names_correspond("Джон Грин", "Greene")
    assert names_correspond("Хайдт", "Jonathan Haidt")


def test_normalized_titles_ignore_punctuation_bracket_notes_and_age_marks():
    assert normalized_title("Зачем мы спим [Текст] : новая наука о сне : [18+]") == normalized_title(
        "Зачем мы спим. Новая наука о сне"
    )
    assert normalized_title("Психология денег. Вечные уроки богатства, жадности") == normalized_title(
        "Психология денег. Вечные уроки богатства жадности"
    )


def test_the_record_validator_rejects_an_incomplete_or_foreign_record():
    confirmed = {
        "status": "confirmed",
        "russian_title": "Атомные привычки",
        "russian_author": "Джеймс Клир",
        "publisher": "Питер",
        "year": 2021,
        "isbn": "",
        "evidence_url": "https://search.rsl.ru/ru/record/01010704039",
        "sources": ["rsl"],
        "confidence": "medium",
        "checked_on": TODAY,
    }
    assert validate_russian_edition(confirmed)
    assert validate_russian_edition(build_no_find(date(2026, 11, 21)))
    assert not validate_russian_edition({**confirmed, "evidence_url": "https://www.livelib.ru/book/1"})
    assert not validate_russian_edition({**confirmed, "description": "copied text"})
    assert not validate_russian_edition({**confirmed, "russian_title": "Atomic Habits"})
    # A LitRes edition states its language, so a Latin title is allowed there.
    litres = {**confirmed, "sources": ["litres"], "evidence_url": "https://www.litres.ru/book/x/nexus-1/"}
    assert validate_russian_edition({**litres, "russian_title": "Nexus"})
    assert not validate_russian_edition({**confirmed, "confidence": "low"})
    assert not validate_russian_edition({"status": "not_found", "checked_on": TODAY})


def test_the_model_answer_is_validated_per_book():
    counts = {"a": 3, "b": 2}

    assert validate_match_response({"matches": [{"candidate_id": "a", "number": 2}]}, counts) == {"a": 2}
    assert validate_match_response({"matches": [{"candidate_id": "a", "number": None}]}, counts) == {"a": None}
    assert validate_match_response({"matches": [{"candidate_id": "b", "number": 3}]}, counts) == {}
    assert validate_match_response({"matches": [{"candidate_id": "x", "number": 1}]}, counts) == {}
    duplicate = {"matches": [{"candidate_id": "a", "number": 1}, {"candidate_id": "a", "number": 2}]}
    assert validate_match_response(duplicate, counts) is None
    assert validate_match_response({"matches": [{"candidate_id": "a", "number": True}]}, counts) is None
    assert validate_match_response({"answers": []}, counts) is None


# ── The regression list of 2026-11-21 ────────────────────────────────────

# slug, title, author, first publication year, the Russian title a correct matcher names.
FOUND = [
    ("atomic-habits", "Atomic Habits", "James Clear", 2018, "Атомные привычки"),
    ("anxious-generation", "The Anxious Generation", "Jonathan Haidt", 2024, "Тревожное поколение"),
    ("48-laws", "The 48 Laws of Power", "Robert Greene", 1998, "48 законов власти"),
    ("why-we-sleep", "Why We Sleep", "Matthew Walker", 2017, "Зачем мы спим"),
    ("braiding-sweetgrass", "Braiding Sweetgrass", "Robin Wall Kimmerer", 2013, "Голос земли"),
    ("supercommunicators", "Supercommunicators", "Charles Duhigg", 2024, "Виртуозы общения"),
    ("four-thousand-weeks", "Four Thousand Weeks", "Oliver Burkeman", 2021, "Четыре тысячи недель"),
    # Hard case: the State Library had nothing under «Хаузел Морган»; LitRes confirms it.
    ("psychology-of-money", "The Psychology of Money", "Morgan Housel", 2020, "Психология денег"),
]


def _regression(tmp_path: Any, slug: str, title: str, author: str, year: int, pick: str | None) -> dict[str, Any]:
    state_dir, output_dir = workspace(tmp_path, [book("c-1", title, author, first_year=year)])
    result = run(state_dir, output_dir, fetcher=FakeFetcher(recorded(slug)), matcher=FakeMatcher({"c-1": pick}))
    return {"result": result, "record": history(state_dir)[0]}


@pytest.mark.parametrize(("slug", "title", "author", "year", "pick"), FOUND)
def test_recorded_books_with_a_russian_edition_are_confirmed(tmp_path, slug, title, author, year, pick):
    outcome = _regression(tmp_path, slug, title, author, year, pick)

    edition = outcome["record"]["russian_edition"]
    assert edition["status"] == "confirmed", slug
    assert edition["russian_title"].startswith(pick)
    assert {SOURCE_RSL, SOURCE_LITRES} & set(edition["sources"])
    assert validate_russian_edition(edition)


def test_nexus_is_not_confirmed_because_every_recorded_listing_is_a_summary(tmp_path):
    # LiveLib lists only «Саммари» books for Nexus, LitRes credits its Nexus
    # titles to a summary publisher and to Синдбад, and the first State
    # Library page of Харари holds no Nexus record.
    outcome = _regression(tmp_path, "nexus", "Nexus", "Yuval Noah Harari", 2024, "Nexus")

    assert outcome["record"]["russian_edition"]["status"] == "no_find"
    # The name came from LiveLib, and the book ended without an edition: it is not kept.
    assert "russian_author_name" not in outcome["record"]


def test_outlive_finds_nothing_because_every_listing_is_a_summary(tmp_path):
    outcome = _regression(tmp_path, "outlive", "Outlive", "Peter Attia", 2023, "Жить дольше")

    assert outcome["record"]["russian_edition"]["status"] == "no_find"
    assert outcome["record"]["russian_author_name"] == "Питер Аттиа"


@pytest.mark.parametrize(
    ("slug", "title", "author"),
    [
        ("24-hours-capitol", "24 Hours at the Capitol", "Nora Neus"),
        ("augustine-african", "Augustine the African", "Catherine Conybeare"),
    ],
)
def test_untranslated_books_find_nothing(tmp_path, slug, title, author):
    # Wikidata gives neither author a Russian label, so no catalogue is asked.
    outcome = _regression(tmp_path, slug, title, author, 2025, None)

    assert outcome["record"]["russian_edition"]["status"] == "no_find"
    assert "russian_author_name" not in outcome["record"]
    assert outcome["result"]["sources"][SOURCE_WIKIDATA]["answered"] == 1
    assert outcome["result"]["sources"][SOURCE_RSL]["answered"] == 0
    assert outcome["result"]["no_find"] == 1
