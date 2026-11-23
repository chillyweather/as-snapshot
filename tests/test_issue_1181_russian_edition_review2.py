"""The Russian-edition completion pass: the second review round (#1181).

A weekly run that starts during the pass, stored and credited author names
checked against the book's author, the State Library's whole author, the
LiveLib search date in the queue, the State Library asked with and without
the located title, unreadable LiveLib result blocks, and books the model
left without an answer.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from russian_edition_support import BLOCKED
from russian_edition_support import FAILED
from russian_edition_support import LITRES_EMPTY
from russian_edition_support import LIVELIB_EMPTY
from russian_edition_support import RSL_EMPTY
from russian_edition_support import TODAY
from russian_edition_support import WIKIDATA_EMPTY
from russian_edition_support import FakeFetcher
from russian_edition_support import FakeMatcher
from russian_edition_support import book
from russian_edition_support import by_id
from russian_edition_support import fixture
from russian_edition_support import history
from russian_edition_support import litres_answer
from russian_edition_support import livelib_page
from russian_edition_support import recorded
from russian_edition_support import run
from russian_edition_support import workspace
from state_store_support import seed_state
from state_store_support import state_snapshot

from autoscout.__main__ import main
from autoscout.russian_edition import decide
from autoscout.russian_edition import numbered_titles
from autoscout.russian_edition_completion import REPORT_FILENAME
from autoscout.russian_edition_sources import SOURCE_LITRES
from autoscout.russian_edition_sources import SOURCE_LIVELIB
from autoscout.russian_edition_sources import SOURCE_RSL
from autoscout.russian_edition_sources import SOURCE_WIKIDATA
from autoscout.russian_edition_sources import EditionEntry
from autoscout.russian_edition_sources import parse_livelib_search

CLEAR = "c-clear"
GREENE = "c-greene"


def _clear(**extra: Any) -> dict[str, Any]:
    return book(CLEAR, "Atomic Habits: An Easy & Proven Way", "James Clear", first_year=2018, **extra)


def _greene(**extra: Any) -> dict[str, Any]:
    return book(GREENE, "The 48 Laws of Power", "Robert Greene", first_year=1998, **extra)


# ── 1. A weekly run that starts during the pass ──────────────────────────


def test_a_weekly_run_found_before_writing_leaves_everything_unwritten(tmp_path):
    state_dir, output_dir = workspace(tmp_path, [_clear()])
    before = state_snapshot(state_dir)
    checks: list[bool] = []

    def weekly_run_started() -> bool:
        checks.append(True)
        return True

    result = run(
        state_dir,
        output_dir,
        fetcher=FakeFetcher(recorded("atomic-habits")),
        matcher=FakeMatcher({CLEAR: "Атомные привычки"}),
        weekly_run_check=weekly_run_started,
    )

    assert result == {"status": "refused", "reason": "weekly_run_in_progress"}
    assert checks == [True]
    assert state_snapshot(state_dir) == before
    assert not (output_dir / REPORT_FILENAME).exists()


def test_without_a_weekly_run_the_check_lets_the_pass_write(tmp_path):
    state_dir, output_dir = workspace(tmp_path, [_clear()])

    result = run(
        state_dir,
        output_dir,
        fetcher=FakeFetcher(recorded("atomic-habits")),
        matcher=FakeMatcher({CLEAR: "Атомные привычки"}),
        weekly_run_check=lambda: False,
    )

    assert result["status"] == "ok"
    assert history(state_dir)[0]["russian_edition"]["status"] == "confirmed"


def test_command_refuses_when_a_weekly_run_starts_during_the_lookups(tmp_path, capsys, monkeypatch):
    state_dir, output_dir = workspace(tmp_path, [_clear()])
    answers: list[bool] = []

    def weekly_run_in_progress(state_dir: Any) -> bool:
        # Not running when the command starts; running by the time it would write.
        answers.append(bool(answers))
        return answers[-1]

    monkeypatch.setattr("autoscout.cli.common.weekly_run_in_progress", weekly_run_in_progress)
    monkeypatch.setattr(
        "autoscout.russian_edition_completion.LiveSourceFetcher", lambda: FakeFetcher(recorded("atomic-habits"))
    )
    monkeypatch.setattr(
        "autoscout.russian_edition_completion.CodexSameWorkMatcher", lambda: FakeMatcher({CLEAR: "Атомные привычки"})
    )
    monkeypatch.setattr("autoscout.russian_edition_completion.time.sleep", lambda seconds: None)
    before = state_snapshot(state_dir)
    arguments = ["complete-russian-editions", "--state-dir", str(state_dir), "--output-dir", str(output_dir)]

    code = main([*arguments, "--run-date", TODAY, "--skip-source", "wikidata", "--apply"])
    output = capsys.readouterr().out

    assert code == 6
    assert answers == [False, True]
    assert "reason_code=weekly_run_in_progress" in output
    assert "Русские издания не сохранены" in output
    assert "status=ok" not in output
    assert state_snapshot(state_dir) == before


# ── 2. A stored Cyrillic name is checked ─────────────────────────────────


def test_a_stored_name_of_another_author_is_ignored_and_replaced(tmp_path):
    state_dir, output_dir = workspace(tmp_path, [_greene(russian_author_name="Джон Грин")])
    fetcher = FakeFetcher(recorded("48-laws", livelib=LIVELIB_EMPTY))

    result = run(state_dir, output_dir, fetcher=fetcher, matcher=FakeMatcher({GREENE: "48 законов власти"}))

    # The stale «Джон Грин» did not stop Wikidata, and no catalogue was asked for it.
    assert len(fetcher.asked(SOURCE_WIKIDATA)) == 1
    assert fetcher.asked(SOURCE_RSL) == ["author:(Грин Роберт)"]
    record = history(state_dir)[0]
    assert record["russian_edition"]["status"] == "confirmed"
    assert record["russian_edition"]["russian_author"] == "Роберт Грин"
    assert record["russian_author_name"] == "Роберт Грин"
    assert result["author_name_learned"] == 1


def test_a_stored_name_of_the_author_is_kept_and_spares_wikidata(tmp_path):
    state_dir, output_dir = workspace(tmp_path, [_greene(russian_author_name="Роберт Грин")])
    fetcher = FakeFetcher(recorded("48-laws", livelib=LIVELIB_EMPTY))

    result = run(state_dir, output_dir, fetcher=fetcher, matcher=FakeMatcher({GREENE: "48 законов власти"}))

    assert fetcher.asked(SOURCE_WIKIDATA) == []
    assert history(state_dir)[0]["russian_author_name"] == "Роберт Грин"
    assert result["author_name_learned"] == 0


def test_a_catalogue_author_must_be_the_books_author_as_well_as_the_spelling(tmp_path):
    # «Ричард Грин» shares the surname and the Cyrillic initial of «Роберт Грин»,
    # but is not Robert Greene.
    state_dir, output_dir = workspace(tmp_path, [_greene()])
    litres = litres_answer(
        {"title": "48 законов власти", "authors": ["Роберт Грин"]},
        {"title": "Сады Англии", "authors": ["Ричард Грин"]},
    )
    matcher = FakeMatcher({GREENE: "Сады Англии"})
    fetcher = FakeFetcher(recorded("48-laws", livelib=LIVELIB_EMPTY, rsl=RSL_EMPTY, litres=litres))

    run(state_dir, output_dir, fetcher=fetcher, matcher=matcher)

    titles = [entry["title"] for entry in matcher.calls[0][0]["russian_titles"]]
    assert titles == ["48 законов власти"]
    assert history(state_dir)[0]["russian_edition"]["status"] == "no_find"


def test_the_guard_refuses_an_edition_whose_author_is_not_the_books_author():
    other = EditionEntry(
        source=SOURCE_RSL,
        title="Сады Англии",
        authors=("Грин, Ричард",),
        language="ru",
        url="https://search.rsl.ru/ru/record/1",
    )
    group = numbered_titles([other])[0]
    today = date.fromisoformat(TODAY)

    refused = decide(group, author_name="Роберт Грин", latin_author="Robert Greene", first_year=None, today=today)
    allowed = decide(group, author_name="Роберт Грин", latin_author="Richard Green", first_year=None, today=today)

    assert refused["status"] == "no_find"
    assert allowed["status"] == "confirmed"


# ── 3. The State Library's whole author ──────────────────────────────────


def _rsl_with_author(slug: str, given: str, replacement: str) -> str:
    data = json.loads(fixture(f"{slug}.rsl-author.json"))
    data["content"] = data["content"].replace(f"<b>{given}</b>.</b>", f"<b>{replacement}</b>.</b>")
    return json.dumps(data, ensure_ascii=False)


def test_a_state_library_edition_by_another_walker_is_told_apart(tmp_path):
    state_dir, output_dir = workspace(tmp_path, [book("c-walker", "Why We Sleep", "Matthew Walker", first_year=2017)])
    alice = _rsl_with_author("why-we-sleep", "Мэттью", "Элис")
    matcher = FakeMatcher({"c-walker": "Зачем мы спим"})
    fetcher = FakeFetcher(recorded("why-we-sleep", rsl=alice, litres=LITRES_EMPTY))

    run(state_dir, output_dir, fetcher=fetcher, matcher=matcher)

    sources = [entry["sources"] for entry in matcher.calls[0][0]["russian_titles"]]
    assert all(SOURCE_RSL not in listed for listed in sources)
    assert history(state_dir)[0]["russian_edition"]["status"] == "unconfirmed"


# ── 4. The LiveLib search date orders the queue ──────────────────────────


def test_the_livelib_search_date_is_written_only_for_a_search_actually_made(tmp_path):
    # Titles in the order the books come (selection breaks ties by identity key).
    books = [
        book("c-first", "A Same Title", "Ann First"),
        book("c-repeat", "A Same Title", "Ann Repeat"),
        book("c-blocked", "B Blocked Title", "Ann Blocked"),
        book("c-over", "C Over Title", "Ann Over"),
    ]
    state_dir, output_dir = workspace(tmp_path, books)

    def answer(source: str, target: str) -> Any:
        if source == SOURCE_LIVELIB:
            return BLOCKED if "Blocked" in target else LIVELIB_EMPTY
        return {SOURCE_WIKIDATA: WIKIDATA_EMPTY, SOURCE_RSL: RSL_EMPTY, SOURCE_LITRES: LITRES_EMPTY}[source]

    run(state_dir, output_dir, fetcher=FakeFetcher(answer), matcher=FakeMatcher({}), max_livelib_searches=2)

    records = by_id(state_dir)
    assert records["c-first"]["russian_edition_livelib_search_date"] == TODAY
    assert records["c-blocked"]["russian_edition_livelib_search_date"] == TODAY
    # Answered from the cache, and skipped at the ceiling: no search was made.
    assert "russian_edition_livelib_search_date" not in records["c-repeat"]
    assert "russian_edition_livelib_search_date" not in records["c-over"]
    # It never settles a book: the blocked one stays open.
    assert "russian_edition" not in records["c-blocked"]


def test_the_queue_puts_books_never_searched_on_livelib_before_the_oldest_search(tmp_path):
    attempted = "russian_edition_attempt_date"
    searched = "russian_edition_livelib_search_date"
    books = [
        book("c-recent-search", "Recent Search", "Ann Recent", **{attempted: "2026-11-01", searched: "2026-11-01"}),
        book("c-old-search", "Old Search", "Ann Old", **{attempted: "2026-11-15", searched: "2026-10-01"}),
        book("c-no-search", "No Search", "Ann None", **{attempted: "2026-11-20"}),
        book("c-same-search", "Same Search", "Ann Same", **{attempted: "2026-10-20", searched: "2026-11-01"}),
        book("c-never", "Never Tried", "Ann Never"),
    ]
    state_dir, output_dir = workspace(tmp_path, books)
    fetcher = FakeFetcher({SOURCE_LIVELIB: LIVELIB_EMPTY, SOURCE_WIKIDATA: WIKIDATA_EMPTY})

    run(state_dir, output_dir, fetcher=fetcher, matcher=FakeMatcher({}))

    order = [target.rsplit("/", 1)[1] for target in fetcher.asked(SOURCE_LIVELIB)]
    assert order == ["Never%20Tried", "No%20Search", "Old%20Search", "Same%20Search", "Recent%20Search"]


def test_an_older_livelib_search_date_never_replaces_a_newer_one(tmp_path):
    state_dir, output_dir = workspace(tmp_path, [_clear()])

    def answer(source: str, target: str) -> Any:
        records = history(state_dir)
        if records[0].get("russian_edition_livelib_search_date") != "2026-12-01":
            records[0]["russian_edition_livelib_search_date"] = "2026-12-01"
            seed_state(state_dir, candidate_history=records)
        return None

    run(state_dir, output_dir, fetcher=FakeFetcher(answer), matcher=FakeMatcher({}))

    assert history(state_dir)[0]["russian_edition_livelib_search_date"] == "2026-12-01"


# ── 5. The State Library by author and title, and by author alone ────────


def _rsl_by_query(*, combined: Any, author_only: Any) -> Any:
    """A State Library stand-in that tells the two query forms apart."""

    def answer(source: str, target: str) -> Any:
        if source == SOURCE_RSL:
            return combined if " AND title:(" in target else author_only
        return recorded("48-laws", livelib=_other_greene_book(), litres=LITRES_EMPTY)(source, target)

    return answer


def _other_greene_book() -> str:
    return livelib_page(("Искусство обольщения", "Роберт Грин", "/book/1-iskusstvo-obolscheniya"))


def test_the_author_only_query_finds_the_edition_the_located_title_misses(tmp_path):
    state_dir, output_dir = workspace(tmp_path, [_greene()])
    fetcher = FakeFetcher(_rsl_by_query(combined=RSL_EMPTY, author_only=fixture("48-laws.rsl-author.json")))

    result = run(state_dir, output_dir, fetcher=fetcher, matcher=FakeMatcher({GREENE: "48 законов власти"}))

    assert fetcher.asked(SOURCE_RSL) == [
        "author:(Грин Роберт) AND title:(Искусство обольщения)",
        "author:(Грин Роберт)",
    ]
    edition = history(state_dir)[0]["russian_edition"]
    assert edition["status"] == "confirmed"
    assert edition["russian_title"].startswith("48 законов власти")
    assert SOURCE_RSL in edition["sources"]
    assert result["sources"][SOURCE_RSL]["answered"] == 2


def test_the_state_library_counts_as_answered_only_when_both_queries_answer(tmp_path):
    state_dir, output_dir = workspace(tmp_path, [_greene()])
    fetcher = FakeFetcher(_rsl_by_query(combined=RSL_EMPTY, author_only=FAILED))

    result = run(state_dir, output_dir, fetcher=fetcher, matcher=FakeMatcher({GREENE: None}))

    record = history(state_dir)[0]
    assert "russian_edition" not in record and "russian_edition_lookup_date" not in record
    assert result["not_completed"] == 1
    assert (result["sources"][SOURCE_RSL]["answered"], result["sources"][SOURCE_RSL]["failed"]) == (1, 1)


# ── 6. LiveLib result blocks that cannot be read ─────────────────────────


def test_livelib_result_blocks_that_yield_no_result_are_unreadable():
    changed = (
        '<html><body><div class="object-wrapper object-edition ll-redirect" data-link="/book/1-x">'
        '<span class="new-title">Атомные привычки</span></div></body></html>'
    )

    assert parse_livelib_search(changed) is None


def test_unreadable_livelib_result_blocks_count_as_a_failure(tmp_path):
    state_dir, output_dir = workspace(tmp_path, [_clear()])
    changed = '<div class="object-wrapper object-edition" data-link="/book/1-x"><span>Атомные привычки</span></div>'

    result = run(
        state_dir,
        output_dir,
        fetcher=FakeFetcher(recorded("atomic-habits", livelib=changed)),
        matcher=FakeMatcher({CLEAR: "Атомные привычки"}),
    )

    assert result["sources"][SOURCE_LIVELIB]["failed"] == 1
    assert result["sources"][SOURCE_LIVELIB]["answered"] == 0


# ── 7. Books the model left without an answer ────────────────────────────


def test_every_asked_book_without_a_validated_answer_is_counted(tmp_path):
    books = [
        book("c-left-out", "Left Out", "James Clear"),
        book("c-out-of-range", "Out Of Range", "James Clear"),
        book("c-none", "None Of Them", "James Clear"),
    ]
    state_dir, output_dir = workspace(tmp_path, books)
    fetcher = FakeFetcher(recorded("atomic-habits", livelib=LIVELIB_EMPTY))
    matcher = FakeMatcher(result={"c-out-of-range": 99, "c-none": None})

    result = run(state_dir, output_dir, fetcher=fetcher, matcher=matcher)

    records = by_id(state_dir)
    assert result["match_unavailable"] == 2
    assert "russian_edition" not in records["c-left-out"]
    # A number outside the list is no answer, never a «none of them».
    assert "russian_edition" not in records["c-out-of-range"]
    assert records["c-none"]["russian_edition"]["status"] == "no_find"
    assert (result["no_find"], result["not_completed"]) == (1, 2)
