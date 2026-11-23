"""The Russian-edition completion pass: the third review round (#1181).

A late weekly-run refusal leaves no report marker, a partial State Library
answer never settles a book on its own, ineligible titles never reach the
model, transport errors are failures, unreadable LitRes hits are a failure,
and Russian is matched by an explicit list of words.
"""

from __future__ import annotations

import http.client
import json
from typing import Any

import pytest
from russian_edition_support import LITRES_EMPTY
from russian_edition_support import LIVELIB_EMPTY
from russian_edition_support import RSL_EMPTY
from russian_edition_support import TODAY
from russian_edition_support import FakeFetcher
from russian_edition_support import FakeMatcher
from russian_edition_support import book
from russian_edition_support import fixture
from russian_edition_support import history
from russian_edition_support import litres_answer
from russian_edition_support import recorded
from russian_edition_support import run
from russian_edition_support import workspace
from state_store_support import state_snapshot

from autoscout.__main__ import main
from autoscout.russian_edition import eligible_titles
from autoscout.russian_edition_completion import OUTCOME_FAILED
from autoscout.russian_edition_completion import LiveSourceFetcher
from autoscout.russian_edition_completion import _partial_author_answer
from autoscout.russian_edition_completion import order_records
from autoscout.russian_edition_sources import SOURCE_LITRES
from autoscout.russian_edition_sources import SOURCE_LIVELIB
from autoscout.russian_edition_sources import SOURCE_RSL
from autoscout.russian_edition_sources import EditionEntry
from autoscout.russian_edition_sources import RslEntries
from autoscout.russian_edition_sources import parse_litres_search
from autoscout.russian_edition_sources import parse_rsl_author_answer
from autoscout.russian_edition_sources import rsl_entries
from autoscout.russian_edition_sources import states_russian
from autoscout.weekly_report import WeeklyReportStore

CLEAR = "c-clear"
GREENE = "c-greene"


def _clear(**extra: Any) -> dict[str, Any]:
    return book(CLEAR, "Atomic Habits: An Easy & Proven Way", "James Clear", first_year=2018, **extra)


def _greene(**extra: Any) -> dict[str, Any]:
    return book(GREENE, "The 48 Laws of Power", "Robert Greene", first_year=1998, **extra)


# ── 1. A late refusal leaves no report marker ────────────────────────────


def test_a_late_refusal_leaves_the_report_transaction_cleanly(tmp_path):
    state_dir, output_dir = workspace(tmp_path, [_clear()])
    store = WeeklyReportStore(output_dir)
    before = state_snapshot(state_dir)

    result = run(
        state_dir,
        output_dir,
        fetcher=FakeFetcher(recorded("atomic-habits")),
        matcher=FakeMatcher({CLEAR: "Атомные привычки"}),
        publish_lock=lambda: store.transaction("russian-edition-completion"),
        weekly_run_check=lambda: True,
    )

    assert result == {"status": "refused", "reason": "weekly_run_in_progress"}
    assert state_snapshot(state_dir) == before
    assert not store.transaction_marker_path().exists()
    assert not store.transaction_in_progress()
    with store.transaction("feedback"):
        pass
    assert not store.transaction_marker_path().exists()


def test_the_command_refused_late_leaves_the_review_readable(tmp_path, capsys, monkeypatch):
    state_dir, output_dir = workspace(tmp_path, [_clear()])
    answers: list[bool] = []

    def weekly_run_in_progress(state_dir: Any) -> bool:
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
    arguments = ["complete-russian-editions", "--state-dir", str(state_dir), "--output-dir", str(output_dir)]

    code = main([*arguments, "--run-date", TODAY, "--skip-source", "wikidata", "--apply"])

    assert code == 6
    assert "reason_code=weekly_run_in_progress" in capsys.readouterr().out
    assert not WeeklyReportStore(output_dir).transaction_in_progress()


# ── 2. A partial State Library answer ────────────────────────────────────


def test_an_author_only_answer_with_more_hits_than_records_is_partial():
    greene = parse_rsl_author_answer(fixture("48-laws.rsl-author.json"), "author:(Грин Роберт)", TODAY)
    walker = parse_rsl_author_answer(fixture("why-we-sleep.rsl-author.json"), "author:(Уолкер Мэттью)", TODAY)

    assert greene is not None and greene.partial and len(greene) == 10
    assert walker is not None and not walker.partial


def test_a_title_scoped_answer_is_never_partial():
    partial = RslEntries([], partial=True)

    assert _partial_author_answer(SOURCE_RSL, "author:(Грин Роберт)", partial)
    assert not _partial_author_answer(SOURCE_RSL, "author:(Грин Роберт) AND title:(48 законов власти)", partial)
    assert not _partial_author_answer(SOURCE_LITRES, "x", partial)


def _greene_without_livelib(**overrides: Any) -> Any:
    answers = {"rsl": fixture("48-laws.rsl-author.json"), "litres": LITRES_EMPTY, **overrides}
    return recorded("48-laws", **answers)


def test_a_partial_answer_without_livelib_leaves_the_book_open(tmp_path):
    state_dir, output_dir = workspace(tmp_path, [_greene(russian_author_name="Роберт Грин")])

    result = run(
        state_dir,
        output_dir,
        fetcher=FakeFetcher(_greene_without_livelib()),
        matcher=FakeMatcher({GREENE: None}),
        max_livelib_searches=0,
    )

    record = history(state_dir)[0]
    assert "russian_edition" not in record and "russian_edition_lookup_date" not in record
    assert "russian_edition_livelib_search_date" not in record
    assert (result["no_find"], result["not_completed"]) == (0, 1)


def test_a_book_left_open_by_a_partial_answer_comes_forward_for_livelib(tmp_path):
    open_book = _greene(russian_edition_attempt_date="2026-11-20")
    searched = book(
        "c-searched",
        "Searched",
        "Someone Else",
        russian_edition_attempt_date="2026-11-01",
        russian_edition_livelib_search_date="2026-11-01",
    )

    ordered = order_records([searched, open_book], [], [searched, open_book], {})

    assert [record["candidate_id"] for record in ordered] == [GREENE, "c-searched"]


def test_a_partial_answer_counts_for_a_miss_when_livelib_answered(tmp_path):
    state_dir, output_dir = workspace(tmp_path, [_greene(russian_author_name="Роберт Грин")])

    result = run(
        state_dir,
        output_dir,
        fetcher=FakeFetcher(_greene_without_livelib(livelib=LIVELIB_EMPTY)),
        matcher=FakeMatcher({GREENE: None}),
    )

    assert history(state_dir)[0]["russian_edition"]["status"] == "no_find"
    assert result["no_find"] == 1


def test_a_partial_answer_still_confirms_an_edition(tmp_path):
    state_dir, output_dir = workspace(tmp_path, [_greene(russian_author_name="Роберт Грин")])

    run(
        state_dir,
        output_dir,
        fetcher=FakeFetcher(_greene_without_livelib()),
        matcher=FakeMatcher({GREENE: "48 законов власти"}),
        max_livelib_searches=0,
    )

    edition = history(state_dir)[0]["russian_edition"]
    assert edition["status"] == "confirmed"
    assert edition["russian_title"].startswith("48 законов власти")


# ── 3. Ineligible titles never reach the model ───────────────────────────


def _rsl(title: str, *, year: int | None, catalogue_title: str = "", author: str = "Уолкер, Мэттью") -> EditionEntry:
    return EditionEntry(
        source=SOURCE_RSL,
        title=title,
        authors=(author,),
        language="ru",
        url=f"https://search.rsl.ru/ru/record/{abs(hash((title, year)))}",
        year=year,
        catalogue_title=catalogue_title or title,
    )


def _eligible(entries: list[EditionEntry], first_year: int | None = 2017) -> list[str]:
    groups = eligible_titles(entries, author_name="Мэттью Уолкер", latin_author="Matthew Walker", first_year=first_year)
    return [group.title for group in groups]


def test_an_abridgement_marked_only_in_a_bracketed_note_is_not_listed():
    abridged = _rsl("Почему мы спим", year=2019, catalogue_title="Почему мы спим : [сокращённое издание]")
    full = _rsl("Зачем мы спим", year=2018)

    assert _eligible([abridged, full]) == ["Зачем мы спим"]


def test_entries_that_cannot_pass_the_guards_are_left_out():
    entries = [
        _rsl("Зачем мы спим", year=2018),
        _rsl("Why we sleep", year=2019),
        _rsl("Сон", year=2019, author="Уокер, Элис"),
        EditionEntry(SOURCE_RSL, "Зачем мы спим. Саммари", ("Уолкер, Мэттью",), "ru", "https://search.rsl.ru/1"),
        EditionEntry(SOURCE_RSL, "Сон по-английски", ("Уолкер, Мэттью",), "en", "https://search.rsl.ru/2"),
    ]

    assert _eligible(entries) == ["Зачем мы спим"]


def test_a_title_published_before_the_original_is_left_out_whole():
    dated = _rsl("Сон и память", year=2010)
    undated = EditionEntry(SOURCE_LITRES, "Сон и память", ("Мэттью Уолкер",), "ru", "https://www.litres.ru/book/1/")
    later = _rsl("Зачем мы спим", year=2018)

    assert _eligible([dated, undated, later]) == ["Зачем мы спим"]


def test_the_model_never_sees_a_retelling_listed_before_the_translation(tmp_path):
    state_dir, output_dir = workspace(tmp_path, [_clear()])
    litres = litres_answer(
        {"title": "Ключевые идеи книги «Атомные привычки»", "authors": ["Джеймс Клир"]},
        {"title": "Атомные привычки", "authors": ["Джеймс Клир"]},
    )
    fetcher = FakeFetcher(recorded("atomic-habits", livelib=LIVELIB_EMPTY, rsl=RSL_EMPTY, litres=litres))
    # The model names the first title, whatever it is.
    matcher = FakeMatcher(result={CLEAR: 1})

    run(state_dir, output_dir, fetcher=fetcher, matcher=matcher)

    assert [entry["title"] for entry in matcher.calls[0][0]["russian_titles"]] == ["Атомные привычки"]
    edition = history(state_dir)[0]["russian_edition"]
    assert edition["status"] == "confirmed"
    assert edition["russian_title"] == "Атомные привычки"


# ── 4. Transport errors are failures ─────────────────────────────────────


@pytest.mark.parametrize(
    "error", [ConnectionResetError("reset"), TimeoutError("timed out"), http.client.IncompleteRead(b"")]
)
def test_a_state_library_transport_error_is_a_failed_answer(monkeypatch, error):
    def broken(config: Any, query: str) -> Any:
        raise error

    monkeypatch.setattr("autoscout.russian_editions.rsl_live_search_response", broken)

    answer = LiveSourceFetcher().fetch(SOURCE_RSL, "author:(Грин Роберт)", timeout=1.0)

    assert answer.outcome == OUTCOME_FAILED


@pytest.mark.parametrize("error", [ConnectionResetError("reset"), http.client.IncompleteRead(b"")])
def test_a_transport_error_while_reading_a_page_is_a_failed_answer(monkeypatch, error):
    class Response:
        headers = http.client.HTTPMessage()

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def read(self, limit: int) -> bytes:
            raise error

    monkeypatch.setattr("autoscout.russian_edition_completion.urlopen", lambda request, timeout: Response())

    answer = LiveSourceFetcher().fetch(SOURCE_LIVELIB, "https://www.livelib.ru/find/books/x", timeout=1.0)

    assert answer.outcome == OUTCOME_FAILED


def test_an_interruption_is_not_swallowed(monkeypatch):
    def interrupted(request: Any, timeout: float) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr("autoscout.russian_edition_completion.urlopen", interrupted)

    with pytest.raises(KeyboardInterrupt):
        LiveSourceFetcher().fetch(SOURCE_LIVELIB, "https://www.livelib.ru/find/books/x", timeout=1.0)


def test_a_reset_mid_pass_fails_the_source_and_the_pass_goes_on(tmp_path, monkeypatch):
    state_dir, output_dir = workspace(tmp_path, [_greene()])

    class Resetting(FakeFetcher):
        def fetch(self, source: str, target: str, *, timeout: float) -> Any:
            if source == SOURCE_RSL:
                self.requests.append((source, target))
                return LiveSourceFetcher().fetch(source, target, timeout=timeout)
            return super().fetch(source, target, timeout=timeout)

    def reset(config: Any, query: str) -> Any:
        raise ConnectionResetError("reset")

    monkeypatch.setattr("autoscout.russian_editions.rsl_live_search_response", reset)

    result = run(state_dir, output_dir, fetcher=Resetting(recorded("48-laws")), matcher=FakeMatcher({GREENE: None}))

    assert result["status"] == "ok"
    assert result["sources"][SOURCE_RSL]["failed"] >= 1


# ── 5. LitRes hits that cannot be read ───────────────────────────────────


def test_litres_text_book_hits_that_cannot_be_read_are_a_failure():
    changed = json.dumps(
        {"payload": {"data": [{"type": "text_book", "instance": {"name": "Атомные привычки", "link": "/x/"}}]}}
    )

    assert parse_litres_search(changed) is None


def test_a_litres_answer_without_text_book_hits_is_empty():
    audio_only = json.dumps({"payload": {"data": [{"type": "audiobook", "instance": {"title": "x"}}]}})

    assert parse_litres_search(audio_only) == []
    assert parse_litres_search(LITRES_EMPTY) == []


def test_unreadable_litres_hits_count_as_a_failure(tmp_path):
    state_dir, output_dir = workspace(tmp_path, [_clear()])
    changed = json.dumps({"payload": {"data": [{"type": "text_book", "instance": {"name": "Атомные привычки"}}]}})

    result = run(
        state_dir,
        output_dir,
        fetcher=FakeFetcher(recorded("atomic-habits", litres=changed)),
        matcher=FakeMatcher({CLEAR: None}),
    )

    assert result["sources"][SOURCE_LITRES]["failed"] == 1
    assert "russian_edition" not in history(state_dir)[0]


# ── 6. Russian by an explicit list ───────────────────────────────────────


@pytest.mark.parametrize("clue", ["русинский", "белорусский", "русинский язык", "Rusyn", "russkiy-ish"])
def test_rusyn_and_belarusian_are_not_russian(clue):
    assert not states_russian(clue)


@pytest.mark.parametrize("clue", ["русский", "Русский", "рус.", "рус", "rus", "ru", "Russian", "русский; английский"])
def test_the_listed_words_are_russian(clue):
    assert states_russian(clue)


def test_a_rusyn_state_library_record_is_not_a_russian_edition():
    result = {
        "result_title": "Зачем мы спим",
        "record_url": "https://search.rsl.ru/ru/record/1",
        "result_author": "Уолкер, Мэттью",
        "publication_data": "",
        "language_clue": "русинский",
    }

    assert rsl_entries([result])[0].language != "ru"
