"""The age of the work behind every book: first publication year and edition count (issue #1138).

Every test is offline. Open Library answers come from responses saved under
``tests/fixtures/issue-1138-work-age/`` or from small documents built here.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import pytest
from state_store_support import read_state
from state_store_support import state_snapshot
from test_issue_640_description_adjudication_stage import _run_fixture_weekly
from test_issue_968_original_edition_completion import FakeClock
from test_issue_968_original_edition_completion import FakeGetter
from test_issue_968_original_edition_completion import _workspace

from autoscout.__main__ import main
from autoscout.editorial_fit_shadow import fit_model_state
from autoscout.html_review import _candidate_html
from autoscout.isbn_completion import OPEN_LIBRARY_SEARCH_URL
from autoscout.original_edition import build_original_edition
from autoscout.weekly_workflow import STAGE_ORDER
from autoscout.weekly_workflow import bind_work_age_completion
from autoscout.weekly_workflow import deterministic_adapters
from autoscout.work_age import BASIS_ISBN
from autoscout.work_age import BASIS_ORIGINAL_EDITION
from autoscout.work_age import BASIS_TITLE_AUTHOR
from autoscout.work_age import MIN_REQUEST_INTERVAL_SECONDS
from autoscout.work_age import RETRY_AFTER_DAYS
from autoscout.work_age import STAGE_NAME
from autoscout.work_age import STATUS_FOUND
from autoscout.work_age import STATUS_NOT_CHECKED
from autoscout.work_age import STATUS_NOT_FOUND
from autoscout.work_age import UNAVAILABLE_LIMIT
from autoscout.work_age import ProductionWorkAgeCompletionStage
from autoscout.work_age import build_work_age
from autoscout.work_age import complete_work_age
from autoscout.work_age import format_work_age
from autoscout.work_age import open_library_getter
from autoscout.work_age import run_work_age_completion
from autoscout.work_age import validate_work_age
from autoscout.work_age import work_age_of

FIXTURES = Path(__file__).parent / "fixtures" / "issue-1138-work-age"
TODAY = "2026-11-18"
ATOMIC_ISBN = "9780735211292"
SPANISH_SAPIENS = "De animales a dioses"
SAPIENS_ORIGINAL = "Sapiens: A Brief History of Humankind"
SAPIENS_DOCS = {
    "docs": [
        {
            "key": "/works/OL17075811W",
            "title": "Sapiens",
            "author_name": ["Yuval Noah Harari"],
            "first_publish_year": 2011,
            "edition_count": 120,
            "editions": {"docs": [{"key": "/books/OL1M", "title": "Sapiens"}]},
        }
    ]
}


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _record(
    title: str, authors: list[str], *, key: str | None = None, candidate_id: str = "candidate-001", **extra: Any
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "identity_key": key or f"{title.casefold()}|{authors[0].casefold()}",
        "candidate_id": candidate_id,
        "title": title,
        "primary_authors": authors,
        "publication_year": 2026,
    }
    record.update(extra)
    return record


class RoutedGetter(FakeGetter):
    """Answers an ISBN search and a title search with different bodies."""

    def __init__(self, *, isbn: str | None = None, title: str | None = None) -> None:
        super().__init__()
        self.isbn_body = isbn
        self.title_body = title

    def get(self, url: str, *, timeout: float | None = None) -> str | None:
        self.requests.append(url)
        self.timeouts.append(timeout)
        query = parse_qs(urlsplit(url).query)
        return self.isbn_body if "q" in query else self.title_body


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


def _complete(records: list[dict[str, Any]], getter: Any, **kwargs: Any) -> Any:
    return complete_work_age(records, getter=getter, today=date.fromisoformat(TODAY), **kwargs)


# ── The record ───────────────────────────────────────────────────────────


def test_a_record_holds_year_edition_count_source_and_one_of_three_statuses():
    found = build_work_age(
        status=STATUS_FOUND,
        lookup_date=TODAY,
        first_publish_year=2016,
        edition_count=42,
        basis=BASIS_ISBN,
        source_url="https://openlibrary.org/works/OL17930368W",
    )
    not_found = build_work_age(status=STATUS_NOT_FOUND, lookup_date=TODAY, basis=BASIS_TITLE_AUTHOR)
    not_checked = build_work_age(status=STATUS_NOT_CHECKED, lookup_date="")

    assert all(validate_work_age(block) for block in (found, not_found, not_checked))
    assert found["source"] == not_found["source"] == "open_library"
    assert (not_found["first_publish_year"], not_found["edition_count"], not_found["source_url"]) == (None, None, "")
    # A book never looked up is "not checked", which is not "not found".
    assert work_age_of(_record("Untouched", ["Some Author"])) is None


def test_a_not_found_block_cannot_carry_a_guessed_year():
    block = build_work_age(status=STATUS_NOT_FOUND, lookup_date=TODAY, basis=BASIS_TITLE_AUTHOR)
    block["first_publish_year"] = 2002

    assert not validate_work_age(block)


# ── Matching ─────────────────────────────────────────────────────────────


def test_an_isbn_lookup_takes_the_agreeing_work_and_skips_the_authorless_duplicate():
    record = _record("Atomic Habits", ["James Clear"], isbn=ATOMIC_ISBN)
    getter = RoutedGetter(isbn=_fixture("atomic-isbn.json"))

    report = _complete([record], getter)

    assert report.found == 1
    assert record["work_age"] == build_work_age(
        status=STATUS_FOUND,
        lookup_date=TODAY,
        first_publish_year=2016,
        edition_count=42,
        basis=BASIS_ISBN,
        source_url="https://openlibrary.org/works/OL17930368W",
    )
    assert getter.requests == [getter.requests[0]]
    assert _query(getter.requests[0])["q"] == [f"isbn:{ATOMIC_ISBN}"]


def test_a_book_without_isbn_is_looked_up_by_title_and_author():
    record = _record("Castles made of sand", ["Gwyneth Jones"])
    getter = RoutedGetter(title=_fixture("castles.json"))

    _complete([record], getter)

    assert record["work_age"]["status"] == STATUS_FOUND
    assert record["work_age"]["basis"] == BASIS_TITLE_AUTHOR
    assert (record["work_age"]["first_publish_year"], record["work_age"]["edition_count"]) == (2002, 4)
    assert _query(getter.requests[0])["title"] == ["castles made of sand"]


@pytest.mark.parametrize(
    ("title", "author"),
    [
        # The probe's mistake: a shorter title is not the same work.
        ("Castles", "Gwyneth Jones"),
        # The same title by another author with the same surname is not either.
        ("Castles made of sand", "Tom Jones"),
    ],
)
def test_a_doubtful_match_is_stored_as_unknown_never_as_a_guess(title, author):
    record = _record(title, [author])

    _complete([record], RoutedGetter(title=_fixture("castles.json")))

    assert record["work_age"]["status"] == STATUS_NOT_FOUND
    assert record["work_age"]["first_publish_year"] is None
    assert record["work_age"]["edition_count"] is None
    assert format_work_age(record) == ""


def test_an_isbn_whose_work_disagrees_falls_back_to_title_and_author():
    record = _record("Castles made of sand", ["Gwyneth Jones"], isbn=ATOMIC_ISBN)
    getter = RoutedGetter(isbn=_fixture("atomic-isbn.json"), title=_fixture("castles.json"))

    _complete([record], getter)

    assert len(getter.requests) == 2
    assert record["work_age"]["basis"] == BASIS_TITLE_AUTHOR
    assert record["work_age"]["first_publish_year"] == 2002


def test_a_translation_with_a_confirmed_original_takes_the_age_of_the_original():
    original = build_original_edition(
        credit_url="https://editorial.example.test/sapiens",
        confirmation={
            "status": "confirmed",
            "original_title": SAPIENS_ORIGINAL,
            "original_language": "en",
            "language_confirmed": True,
            "original_first_published_year": 2011,
            "confirmation_url": "https://openlibrary.org/works/OL17075811W",
        },
    )
    record = _record(SPANISH_SAPIENS, ["Yuval Noah Harari"], isbn="9788499926223", original_edition=original)
    # Open Library keeps the Spanish edition as its own work of 2018.
    getter = RoutedGetter(isbn=_fixture("sapiens-es-title.json"), title=json.dumps(SAPIENS_DOCS))

    _complete([record], getter)

    assert record["work_age"]["basis"] == BASIS_ORIGINAL_EDITION
    assert record["work_age"]["first_publish_year"] == 2011
    assert record["work_age"]["edition_count"] == 120
    assert [_query(url).get("title") for url in getter.requests] == [["sapiens"]]


def test_without_a_confirmed_original_the_translation_keeps_its_own_work():
    record = _record(SPANISH_SAPIENS, ["Yuval Noah Harari"])

    _complete([record], RoutedGetter(title=_fixture("sapiens-es-title.json")))

    # The edition title "De animales a dioses: Breve historia…" agrees too.
    assert record["work_age"]["first_publish_year"] == 2018


def test_a_found_listed_edition_is_looked_up_again_once_an_original_is_confirmed():
    record = _record(SPANISH_SAPIENS, ["Yuval Noah Harari"])
    _complete([record], RoutedGetter(title=_fixture("sapiens-es-title.json")))
    settled = _complete([record], RoutedGetter(title=_fixture("sapiens-es-title.json")))
    record["original_edition"] = build_original_edition(
        credit_url="https://editorial.example.test/sapiens",
        confirmation={
            "status": "confirmed",
            "original_title": SAPIENS_ORIGINAL,
            "original_language": "en",
            "language_confirmed": True,
            "original_first_published_year": 2011,
            "confirmation_url": "https://openlibrary.org/works/OL17075811W",
        },
    )

    again = _complete([record], RoutedGetter(title=json.dumps(SAPIENS_DOCS)))

    assert settled.skipped_settled == 1
    assert again.found == 1
    assert record["work_age"]["first_publish_year"] == 2011


# ── Not found, not checked, retries and the budget ──────────────────────


def test_a_book_the_catalogue_does_not_know_is_not_found_and_rests_for_a_while():
    record = _record("A Brand New Book", ["New Author"])
    _complete([record], RoutedGetter(title=json.dumps({"docs": []})))
    first = copy.deepcopy(record["work_age"])

    soon = complete_work_age(
        [record], getter=RoutedGetter(title="{}"), today=date.fromisoformat(TODAY) + timedelta(days=1)
    )
    later_getter = RoutedGetter(title=_fixture("castles.json"))
    later = complete_work_age(
        [record], getter=later_getter, today=date.fromisoformat(TODAY) + timedelta(days=RETRY_AFTER_DAYS)
    )

    assert first["status"] == STATUS_NOT_FOUND
    assert soon.skipped_settled == 1
    assert later.not_found == 1 and len(later_getter.requests) == 1


def test_an_unreachable_catalogue_leaves_the_book_not_checked():
    record = _record("Castles made of sand", ["Gwyneth Jones"])

    report = _complete([record], RoutedGetter(title=None))
    offline = _complete([record], None)

    assert report.lookup_unavailable == 1 and offline.lookup_unavailable == 1
    assert "work_age" not in record


def test_the_budget_stops_before_the_next_request_and_stores_nothing_for_it():
    clock = FakeClock()
    records = [_record("Castles made of sand", ["Gwyneth Jones"], key=f"k{index}") for index in range(2)]

    def on_get(_url: str) -> None:
        clock.now = 1_000.0

    getter = FakeGetter({OPEN_LIBRARY_SEARCH_URL: _fixture("castles.json")}, on_get=on_get)

    report = _complete(records, getter, budget_seconds=100.0, monotonic=clock)

    assert report.found == 1 and report.budget_exhausted == 1
    assert "work_age" not in records[1]


class ScriptedGetter(FakeGetter):
    """Answers each request with the next body of a script; ``None`` is no answer."""

    def __init__(self, bodies: list[str | None]) -> None:
        super().__init__()
        self.bodies = list(bodies)

    def get(self, url: str, *, timeout: float | None = None) -> str | None:
        self.requests.append(url)
        return self.bodies.pop(0) if self.bodies else None


def _castles_records(count: int) -> list[dict[str, Any]]:
    return [_record("Castles made of sand", ["Gwyneth Jones"], key=f"k{index}") for index in range(count)]


def test_a_catalogue_that_stops_answering_is_not_asked_for_the_remaining_books():
    records = _castles_records(UNAVAILABLE_LIMIT + 4)
    getter = ScriptedGetter([])

    report = _complete(records, getter)

    assert UNAVAILABLE_LIMIT == 3
    assert len(getter.requests) == UNAVAILABLE_LIMIT
    assert report.lookup_unavailable == UNAVAILABLE_LIMIT
    assert report.skipped_catalogue_unavailable == 4
    assert report.as_dict()["skipped_catalogue_unavailable"] == 4
    assert all("work_age" not in record for record in records)


def test_an_answer_resets_the_count_of_unavailable_answers():
    castles = _fixture("castles.json")
    records = _castles_records(6)
    getter = ScriptedGetter([None, None, castles, None, None, castles])

    report = _complete(records, getter)

    assert len(getter.requests) == 6
    assert report.found == 2 and report.lookup_unavailable == 4
    assert report.skipped_catalogue_unavailable == 0


def test_the_getter_keeps_to_open_library_rate_guidance():
    # Open Library asks clients without contact details for at most one request per second.
    assert MIN_REQUEST_INTERVAL_SECONDS >= 1.0
    assert open_library_getter()._limiter.min_interval_seconds == MIN_REQUEST_INTERVAL_SECONDS


# ── Persistence and the command ──────────────────────────────────────────


def _castles_workspace(tmp_path: Path) -> tuple[Path, Path]:
    record = _record("Castles made of sand", ["Gwyneth Jones"])
    return _workspace(tmp_path, [record], [record["identity_key"]])


def test_the_pass_writes_history_and_both_snapshots(tmp_path):
    state_dir, output_dir = _castles_workspace(tmp_path)

    result = run_work_age_completion(
        output_dir,
        state_dir,
        effective_date=TODAY,
        getter=RoutedGetter(title=_fixture("castles.json")),
    )

    assert result["found"] == 1
    (history,) = read_state(state_dir, "candidate_history")
    (latest,) = read_state(state_dir, "latest_scouting_candidates")
    (current,) = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
    assert history["work_age"]["first_publish_year"] == 2002
    assert latest["work_age"] == current["work_age"] == history["work_age"]
    assert json.loads((output_dir / "work-age-completion.json").read_text(encoding="utf-8"))["found"] == 1


def test_by_default_a_book_with_an_editor_decision_gets_its_work_age_too(tmp_path):
    decided = _record("Castles made of sand", ["Gwyneth Jones"], editor_status="reject")
    state_dir, output_dir = _workspace(tmp_path, [decided], [])

    result = run_work_age_completion(
        output_dir, state_dir, effective_date=TODAY, getter=RoutedGetter(title=_fixture("castles.json"))
    )

    assert result["scope"] == "all" and result["found"] == 1
    (history,) = read_state(state_dir, "candidate_history")
    assert history["editor_status"] == "reject"
    assert history["work_age"]["first_publish_year"] == 2002


def test_the_undecided_scope_leaves_decided_books_alone(tmp_path):
    decided = _record("Castles made of sand", ["Gwyneth Jones"], editor_status="reject")
    state_dir, output_dir = _workspace(tmp_path, [decided], [])

    result = run_work_age_completion(
        output_dir,
        state_dir,
        effective_date=TODAY,
        scope="undecided",
        getter=RoutedGetter(title=_fixture("castles.json")),
    )

    assert result["considered"] == 0
    assert "work_age" not in read_state(state_dir, "candidate_history")[0]


def test_a_dry_run_writes_nothing(tmp_path):
    state_dir, output_dir = _castles_workspace(tmp_path)
    before = state_snapshot(state_dir)

    result = run_work_age_completion(
        output_dir,
        state_dir,
        effective_date=TODAY,
        getter=RoutedGetter(title=_fixture("castles.json")),
        dry_run=True,
    )

    assert result["found"] == 1
    assert state_snapshot(state_dir) == before
    assert not (output_dir / "work-age-completion.json").exists()


def test_command_previews_without_writing_and_applies_with_apply(tmp_path, capsys, monkeypatch):
    # A book the editor already decided: the command's default scope takes every book.
    record = _record("Castles made of sand", ["Gwyneth Jones"], editor_status="interested")
    state_dir, output_dir = _workspace(tmp_path, [record], [record["identity_key"]])
    monkeypatch.setattr("autoscout.cli.common.weekly_run_in_progress", lambda state_dir: False)
    monkeypatch.setattr("autoscout.work_age.open_library_getter", lambda: RoutedGetter(title=_fixture("castles.json")))
    arguments = ["complete-work-age", "--state-dir", str(state_dir), "--output-dir", str(output_dir)]
    arguments += ["--run-date", TODAY]
    before = state_snapshot(state_dir)

    preview = main(arguments)
    preview_output = capsys.readouterr().out
    unchanged = state_snapshot(state_dir) == before
    applied = main([*arguments, "--apply"])
    apply_output = capsys.readouterr().out

    assert preview == 3 and unchanged
    assert "applied=false" in preview_output and "found=1" in preview_output
    assert applied == 0
    assert "status=ok scope=all applied=true" in apply_output
    assert "Castles" not in apply_output
    assert read_state(state_dir, "candidate_history")[0]["work_age"]["status"] == STATUS_FOUND


# ── The weekly run ───────────────────────────────────────────────────────


TEST_BOOK_DOCS = {
    "docs": [
        {
            "key": "/works/OL5W",
            "title": "Test Book",
            "author_name": ["Test Author"],
            "first_publish_year": 1999,
            "edition_count": 7,
        }
    ]
}


def test_the_weekly_run_fills_the_work_age_of_its_books_after_the_original_editions(tmp_path):
    adapters = deterministic_adapters()
    adapters.work_age_completion = ProductionWorkAgeCompletionStage(
        getter_factory=lambda: RoutedGetter(title=json.dumps(TEST_BOOK_DOCS))
    )

    output_dir, result = _run_fixture_weekly(tmp_path, adapters=adapters)

    entries = [entry for entry in result.log if entry.get("stage") == STAGE_NAME]
    assert [entry["status"] for entry in entries] == ["ok"]
    assert entries[0]["detail"]["found"] == 1
    assert STAGE_ORDER.index("original-edition-completion") < STAGE_ORDER.index(STAGE_NAME)
    assert STAGE_ORDER.index(STAGE_NAME) < STAGE_ORDER.index("review")
    (current,) = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
    assert current["work_age"]["first_publish_year"] == 1999
    assert "Впервые издана в 1999, 7 изданий в Open Library" in (output_dir / "weekly-review.html").read_text(
        encoding="utf-8"
    )


class _RaisingStage:
    def complete(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("catalogue down")


def test_a_failing_lookup_never_stops_the_weekly_run(tmp_path):
    adapters = deterministic_adapters()
    adapters.work_age_completion = _RaisingStage()

    output_dir, result = _run_fixture_weekly(tmp_path, adapters=adapters)

    entries = [entry for entry in result.log if entry.get("stage") == STAGE_NAME]
    assert [entry["status"] for entry in entries] == ["failed"]
    assert entries[0]["detail"] == {"status": "failed", "exception_type": "RuntimeError"}
    assert (output_dir / "weekly-review.html").exists()


def test_the_binding_keeps_an_injected_stage_and_binds_the_production_one_otherwise():
    injected = ProductionWorkAgeCompletionStage(getter_factory=lambda: None)
    adapters = deterministic_adapters()
    adapters.work_age_completion = injected
    bind_work_age_completion(adapters)
    fresh = deterministic_adapters()
    bind_work_age_completion(fresh)

    assert adapters.work_age_completion is injected
    assert isinstance(fresh.work_age_completion, ProductionWorkAgeCompletionStage)


# ── The review card and the fit score ────────────────────────────────────


def _found(year: int | None, count: int | None) -> dict[str, Any]:
    return build_work_age(
        status=STATUS_FOUND,
        lookup_date=TODAY,
        first_publish_year=year,
        edition_count=count,
        basis=BASIS_ISBN,
        source_url="https://openlibrary.org/works/OL1W",
    )


@pytest.mark.parametrize(
    ("year", "count", "text"),
    [
        (2016, 42, "Впервые издана в 2016, 42 издания в Open Library"),
        (2002, 1, "Впервые издана в 2002, 1 издание в Open Library"),
        (1946, 11, "Впервые издана в 1946, 11 изданий в Open Library"),
        (2020, None, "Впервые издана в 2020"),
    ],
)
def test_the_card_shows_the_first_year_and_the_edition_count_as_plain_text(year, count, text):
    html = _candidate_html({"title": "Book", "work_age": _found(year, count)})

    assert f"<dt>Возраст произведения</dt><dd>{text}</dd>" in html.replace("\n", "")


@pytest.mark.parametrize(
    "work_age",
    [None, build_work_age(status=STATUS_NOT_FOUND, lookup_date=TODAY, basis=BASIS_TITLE_AUTHOR)],
)
def test_the_card_shows_nothing_when_the_age_is_unknown(work_age):
    assert "Возраст произведения" not in _candidate_html({"title": "Book", "work_age": work_age})


def test_the_fit_score_input_does_not_include_the_work_age():
    record = _record("Atomic Habits", ["James Clear"], publisher="Avery")

    assert fit_model_state({**record, "work_age": _found(2016, 42)}) == fit_model_state(record)
