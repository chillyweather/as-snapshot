"""Measure on decided books whether the age of the work would improve the review order (issue #1141)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from state_store_support import seed_state
from state_store_support import state_databases
from state_store_support import state_snapshot

from autoscout import work_age_report as report
from autoscout.__main__ import main as cli_main
from autoscout.fit_scores import QUESTION_SET_VERSION
from autoscout.fit_scores import SCORES_ARTIFACT_TYPE
from autoscout.work_age_report import DEVELOPMENT
from autoscout.work_age_report import HELD_OUT
from autoscout.work_age_report import Book

EDITOR_NOTE = "Слишком известная книга, у нас уже выходила."


def _book(
    decision: str,
    score: float | None,
    *,
    age: int | None = 0,
    editions: int | None = 1,
    half: str = DEVELOPMENT,
    title: str = "Book",
    language: str = "en",
) -> Book:
    listing_year = 2026
    return Book(
        title=title,
        authors="Author",
        language=language,
        decision=decision,
        listing_year=listing_year,
        first_publish_year=None if age is None else listing_year - age,
        edition_count=editions,
        age_status=report.AGE_KNOWN if age is not None and editions is not None else report.AGE_NOT_LOOKED_UP,
        score=score,
        half=half,
    )


class TopSharesTests:
    def test_the_highest_scored_two_fifths_are_the_top(self) -> None:
        assert report.top_shares([4.0, 3.0, 2.0, 1.0, 0.0]) == [1.0, 1.0, 0.0, 0.0, 0.0]

    def test_a_tie_on_the_edge_shares_the_places_left(self) -> None:
        # Two places: the 3.0 takes one, the two 2.0 books share the other.
        assert report.top_shares([3.0, 2.0, 2.0, 1.0, 0.0]) == [1.0, 0.5, 0.5, 0.0, 0.0]


class SimulationTests:
    def test_moving_an_old_rejected_book_down_lets_a_wanted_book_into_the_top(self) -> None:
        books = [
            _book("rejected", 4.0, age=30, editions=80),
            _book("wanted", 3.0, age=0, editions=1),
            _book("wanted", 2.0, age=1, editions=2),
            _book("rejected", 1.0, age=0, editions=1),
            _book("rejected", 0.0, age=0, editions=1),
        ]

        result = report.simulate(books, 10, 10)

        assert result["top_size"] == 2
        assert (result["rejected_moved_down"], result["rejected_left_top"]) == (1, 1.0)
        assert (result["wanted_in_top_before"], result["wanted_in_top_after"]) == (1.0, 2.0)
        assert (result["wanted_lost_from_top"], result["wanted_gained_in_top"]) == (0.0, 1.0)
        assert result["net_wanted_change"] == 1.0

    def test_a_book_moves_down_only_at_or_above_both_thresholds(self) -> None:
        assert report.moved_down(_book("rejected", 1.0, age=10, editions=10), 10, 10)
        assert not report.moved_down(_book("rejected", 1.0, age=9, editions=100), 10, 10)
        assert not report.moved_down(_book("rejected", 1.0, age=100, editions=9), 10, 10)

    def test_a_moved_wanted_book_counts_as_lost_from_the_top(self) -> None:
        books = [
            _book("wanted", 4.0, age=40, editions=100),
            _book("rejected", 3.0),
            _book("rejected", 2.0),
            _book("rejected", 1.0),
            _book("rejected", 0.0),
        ]

        result = report.simulate(books, 10, 10)

        assert (result["wanted_moved_down"], result["wanted_lost_from_top"]) == (1, 1.0)
        assert result["net_wanted_change"] == -1.0


def _halves_that_disagree() -> list[Book]:
    """Development: only the old-and-many-editions rule helps. Held-out: only the plain old-work rule would."""
    development = [
        _book("rejected", 4.0, age=30, editions=80, half=DEVELOPMENT),
        _book("wanted", 3.5, age=30, editions=2, half=DEVELOPMENT),
        _book("wanted", 3.0, half=DEVELOPMENT),
        _book("rejected", 1.0, half=DEVELOPMENT),
        _book("rejected", 0.5, half=DEVELOPMENT),
    ]
    held_out = [
        _book("rejected", 4.0, age=30, editions=2, half=HELD_OUT),
        _book("rejected", 3.5, age=30, editions=2, half=HELD_OUT),
        _book("wanted", 3.0, half=HELD_OUT),
        _book("wanted", 2.0, half=HELD_OUT),
        _book("rejected", 0.5, half=HELD_OUT),
    ]
    return development + held_out


class ThresholdChoiceTests:
    def test_the_threshold_is_chosen_on_the_development_half_only(self) -> None:
        built = report.build_report(_halves_that_disagree())

        chosen = built["chosen"]
        assert chosen[DEVELOPMENT]["net_wanted_change"] == 1.0
        assert chosen[DEVELOPMENT]["wanted_moved_down"] == 0
        assert chosen[DEVELOPMENT]["min_editions"] > 2
        # A rule with a lower edition threshold would have done better on the held-out half.
        assert max(row[HELD_OUT]["net_wanted_change"] for row in built["thresholds"]) == 2.0
        assert chosen[HELD_OUT]["net_wanted_change"] == 0.0

    def test_the_wrong_books_are_listed_by_title(self) -> None:
        books = [
            _book("wanted", 4.0, age=40, editions=100, title="Old Wanted"),
            _book("rejected", 3.5, age=3, editions=2, title="Old Rejected Kept"),
            _book("rejected", 3.0, age=0, editions=1, title="New Rejected"),
            _book("rejected", 2.0),
            _book("rejected", 1.0),
            # Any rule that would move the kept rejected book down moves this wanted book down too.
            _book("wanted", 0.2, age=3, editions=2, title="Young Wanted"),
        ]

        mistakes = report.build_report(books)["mistakes"]

        assert [row["title"] for row in mistakes["wanted_moved_down"]] == ["Old Wanted"]
        assert [row["title"] for row in mistakes["rejected_old_works_left_in_top"]] == ["Old Rejected Kept"]


class NoThresholdHelpsTests:
    def test_a_threshold_that_adds_no_wanted_book_is_named_least_harmful_not_chosen(self) -> None:
        # Every work is new, so no threshold moves a book and none adds a wanted book to the top.
        books = [
            _book("rejected", 4.0, half=DEVELOPMENT),
            _book("wanted", 3.0, half=DEVELOPMENT),
            _book("rejected", 2.0, half=DEVELOPMENT),
            _book("wanted", 4.0, half=HELD_OUT),
            _book("rejected", 1.0, half=HELD_OUT),
        ]

        built = report.build_report(books)
        text = report.render_report(built)

        assert built["chosen_helps"] is False
        assert "No threshold added wanted books to the top on the development half" in text
        assert "no threshold is recommended" in text
        assert "(least harmful)" in text
        assert "(chosen)" not in text
        assert "The chosen threshold" not in text
        assert "## 4. The books the least harmful threshold gets wrong" in text
        assert "these lists are not the mistakes of a recommended rule" in text

    def test_a_threshold_that_adds_wanted_books_is_named_chosen(self) -> None:
        built = report.build_report(_halves_that_disagree())
        text = report.render_report(built)

        assert built["chosen_helps"] is True
        assert "The chosen threshold moves a book down" in text
        assert "## 4. The books the chosen threshold gets wrong" in text
        assert "(least harmful)" not in text


class HalfSizeTests:
    def test_small_halves_carry_a_warning_with_their_counts(self) -> None:
        text = report.render_report(report.build_report(_halves_that_disagree()))

        assert "The development half holds 2 wanted books and the held-out half 2." in text
        assert f"With fewer than {report.SMALL_HALF_WANTED} wanted books in a half" in text

    def test_large_halves_carry_no_warning(self) -> None:
        wanted = report.SMALL_HALF_WANTED
        sentence = report._half_size_sentence({DEVELOPMENT: wanted, HELD_OUT: wanted + 3})

        assert sentence == f"The development half holds {wanted} wanted books and the held-out half {wanted + 3}."


class SplitTests:
    def test_the_half_depends_only_on_the_book(self) -> None:
        ids = [f"candidate-{index:024x}" for index in range(200)]
        halves = [report.split_half(book_id) for book_id in ids]

        assert halves == [report.split_half(book_id) for book_id in ids]
        assert 60 < halves.count(DEVELOPMENT) < 140


# ── From state ────────────────────────────────────────────────────────────


def _work_age(year: int | None, editions: int | None, status: str = "found", basis: str = "isbn") -> dict[str, Any]:
    found = status == "found"
    return {
        "status": status,
        "first_publish_year": year,
        "edition_count": editions,
        "basis": basis,
        "source": "open_library",
        "source_url": "https://openlibrary.org/works/OL1W" if found else "",
        "lookup_date": "2026-11-17",
    }


def _record(index: int, status: str, language: str, work_age: dict[str, Any] | None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "identity_key": f"book {index}|author {index}",
        "candidate_id": f"candidate-{index:04d}",
        "title": f"Book {index}",
        "primary_authors": [f"Author {index}"],
        "first_seen_date": "2024-03-01",
        "editor_status": status,
        "editor_note": EDITOR_NOTE,
        "source_evidence": [{"source_name": f"List {language}", "language": language}],
    }
    if work_age is not None:
        record["work_age"] = work_age
    return record


# (status, language, work age, fit level); None for no block or no score.
BOOKS = [
    ("reject", "en", _work_age(1950, 120, basis="original_edition"), 4),
    ("interested", "en", _work_age(2024, 1), 3),
    ("maybe_watch", "en", _work_age(2023, 2), 2),
    ("reject", "en", _work_age(2024, 1), 1),
    ("reject", "en", _work_age(2022, 3), 0),
    ("needs_deeper_analysis", "de", _work_age(2010, 4, basis="title_author"), 3),
    ("reject", "de", _work_age(None, None, "not_found"), 2),
    ("already_known", "en", _work_age(1990, 60), 4),
    ("reject", "es", None, 3),
    ("reject", "es", None, None),
    ("interested", "en", _work_age(2024, 1), None),
]


def _decided_state(tmp_path: Path) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    records = [
        _record(index, status, language, work_age)
        for index, (status, language, work_age, _level) in enumerate(BOOKS, start=1)
    ]
    seed_state(run_dir, candidate_history=records)
    shadow = run_dir / "editorial-fit-shadow"
    shadow.mkdir()
    artifact = {
        "artifact_type": SCORES_ARTIFACT_TYPE,
        "question_set_version": QUESTION_SET_VERSION,
        "run_date": "2026-11-03",
        "records": [
            {
                "identity_key": record["identity_key"],
                "candidate_id": record["candidate_id"],
                "status": "scored",
                "components": {"fit": {"expected_level": level}},
            }
            for record, (*_rest, level) in zip(records, BOOKS, strict=True)
            if level is not None
        ],
    }
    (shadow / "scores-2026-11-03.json").write_text(json.dumps(artifact), encoding="utf-8")
    return run_dir


def _files(run_dir: Path) -> dict[str, str]:
    files = {
        str(path.relative_to(run_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(run_dir.rglob("*"))
        if path.is_file()
    }
    return {**files, **{name: checksum.hex() for name, checksum in state_databases().items()}}


def _argv(run_dir: Path, report_file: Path) -> list[str]:
    return [
        "work-age-report",
        "--output-dir",
        str(run_dir),
        "--state-dir",
        str(run_dir),
        "--report-file",
        str(report_file),
    ]


class FromStateTests:
    def test_every_decided_book_is_read_once_with_its_score_and_age(self, tmp_path: Path) -> None:
        run_dir = _decided_state(tmp_path)

        books = {book.title: book for book in report.read_books(run_dir, run_dir)}

        assert len(books) == len(BOOKS)
        first = books["Book 1"]
        assert (first.decision, first.language, first.score) == ("rejected", "en", 4.0)
        # The age is counted from the year AutoScout first listed the book, not from today.
        assert (first.listing_year, first.age, first.edition_count) == (2024, 74, 120)
        assert books["Book 6"].decision == "wanted"
        assert books["Book 7"].age_status == report.AGE_NOT_FOUND
        assert books["Book 8"].decision == "already_known"
        assert books["Book 9"].age_status == report.AGE_NOT_LOOKED_UP
        assert books["Book 10"].score is None

    def test_the_report_counts_coverage_and_books_outside_the_simulation(self, tmp_path: Path) -> None:
        run_dir = _decided_state(tmp_path)

        built = report.build_report(report.read_books(run_dir, run_dir))

        totals = built["totals"]
        assert (totals["decided"], totals["wanted"], totals["rejected"], totals["already_known"]) == (11, 4, 6, 1)
        assert (totals[report.AGE_KNOWN], totals[report.AGE_NOT_FOUND], totals[report.AGE_NOT_LOOKED_UP]) == (8, 1, 2)
        assert built["outside"] == {
            "wanted_or_rejected": 10,
            "no_work_age": 3,
            "no_listing_year": 0,
            "work_age_but_no_fit_score": 1,
            "simulated": 6,
            "already_known": 1,
        }
        assert totals["by_basis"] == {"original_edition": 1, "isbn": 6, "title_author": 1}
        by_language = {row["language"]: row for row in built["coverage"]}
        assert (by_language["es"]["decided"], by_language["es"][report.AGE_KNOWN]) == (2, 0)

    def test_the_command_writes_one_report_file_and_changes_no_state(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run_dir = _decided_state(tmp_path)
        state_before = state_snapshot(run_dir)
        files_before = _files(run_dir)

        written = tmp_path / "reports" / "work-age-report.md"

        code = cli_main(_argv(run_dir, written))

        output = capsys.readouterr().out
        assert code == 0
        assert state_snapshot(run_dir) == state_before
        assert _files(run_dir) == files_before
        assert sorted(path.name for path in written.parent.iterdir()) == ["work-age-report.md"]
        text = written.read_text(encoding="utf-8")
        assert text in output
        assert EDITOR_NOTE not in text
        assert "## 4. The books the" in text
        assert "Of the 10 wanted and rejected books, 3 have no usable work age" in text
        assert "Spanish: 0 of 2 (0%)" in text
        assert (
            "Of the 8 work ages found, 1 of 8 (12%) were looked up by the book's confirmed original edition, "
            "6 of 8 (75%) by the book's own ISBN, and 1 of 8 (12%) by its title and author."
        ) in text
        assert "the work may then be older than shown, never younger" in text

    def test_the_report_is_the_same_on_every_run(self, tmp_path: Path) -> None:
        run_dir = _decided_state(tmp_path)

        first = report.write_report(run_dir, run_dir, tmp_path / "first.md")
        second = report.write_report(run_dir, run_dir, tmp_path / "second.md")

        assert first == second

    def test_an_unreadable_score_artifact_is_an_input_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run_dir = _decided_state(tmp_path)
        (run_dir / "editorial-fit-shadow" / "scores-2026-11-04.json").write_text("{", encoding="utf-8")

        written = tmp_path / "work-age-report.md"

        code = cli_main(_argv(run_dir, written))

        assert code == 2
        assert capsys.readouterr().out.startswith("error=")
        assert not written.exists()

    def test_a_report_file_inside_the_state_directory_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run_dir = _decided_state(tmp_path)
        files_before = _files(run_dir)

        code = cli_main(_argv(run_dir, run_dir / "editorial-fit-shadow" / "work-age-report.md"))

        assert code == 2
        assert capsys.readouterr().out.startswith("error=")
        assert _files(run_dir) == files_before
