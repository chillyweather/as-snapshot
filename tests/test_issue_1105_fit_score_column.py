"""The fit score as a review column, and the order of the books of one date by it (issue #1105)."""

from __future__ import annotations

import csv
import io
import json
import re
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from state_store_support import seed_state
from state_store_support import state_snapshot

from autoscout import editorial_fit_shadow as fit_shadow
from autoscout import fit_scores
from autoscout.config import validate_config
from autoscout.jev_adapter import ReplayJevAdapter
from autoscout.review_rendering import render_review

NEWER = "2026-11-12"
OLDER = "2026-11-10"


def _record(index: int, added: str, **fields: Any) -> dict[str, Any]:
    return {
        "identity_key": f"book {index}|author {index}",
        "candidate_id": f"candidate-{index:024x}",
        "title": f"Book {index}",
        "primary_authors": [f"Author {index}"],
        "priority_label": "C",
        "independent_signal_count": 1,
        "russian_edition_status": "not found in Russian",
        "visible_risk_status": "no visible risk",
        "first_seen_date": added,
        "last_seen_date": added,
        "content_description": "Книга о том, как устроена повседневная жизнь небольшого города.",
        "source_evidence": [
            {
                "source_name": "Prize List",
                "source_family": "media/curated list",
                "source_url": "https://example.test/prize-list",
                "evidence_note": "Listed by the source.",
            }
        ],
        **fields,
    }


def _history() -> list[dict[str, Any]]:
    """Books 1 to 3 were added on the newer date, books 4 and 5 on the older one."""
    return [_record(index, NEWER) for index in (1, 2, 3)] + [_record(index, OLDER) for index in (4, 5)]


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _state(tmp_path: Path, history: list[dict[str, Any]] | None = None) -> Path:
    state = tmp_path / "state"
    _write(state / "scouting-candidates.json", [])
    seed_state(state, candidate_history=_history() if history is None else history)
    _write(state / "run-summary.json", {"run_date": NEWER, "editor_facing_count": 0})
    return state


def _score_record(index: int, fit: float | None, **fields: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "identity_key": f"book {index}|author {index}",
        "candidate_id": f"candidate-{index:024x}",
        "status": "scored" if fit is not None else "unavailable",
        **fields,
    }
    if fit is not None:
        record["components"] = {"fit": {"expected": fit / 4, "expected_level": fit}}
    return record


def _write_scores(state: Path, run_date: str, records: list[dict[str, Any]], **fields: Any) -> None:
    _write(
        fit_scores.shadow_dir(state) / f"scores-{run_date}.json",
        {
            "artifact_type": fit_scores.SCORES_ARTIFACT_TYPE,
            "question_set_version": fit_scores.QUESTION_SET_VERSION,
            "run_date": run_date,
            "records": records,
            **fields,
        },
    )


def _config(tmp_path: Path, **fields: Any) -> Path:
    path = tmp_path / "config.json"
    _write(path, {"sources": [], **fields})
    return path


def _render(state: Path, config: Path | None = None) -> None:
    with redirect_stdout(io.StringIO()):
        assert render_review(state, state, config_path=config) == 0


def _markdown(state: Path) -> str:
    return (state / "weekly-review.md").read_text(encoding="utf-8")


def _html(state: Path) -> str:
    return (state / "weekly-review.html").read_text(encoding="utf-8")


def _markdown_rows(state: Path) -> list[dict[str, str]]:
    """The candidate table of the Markdown review, one dict per row, in the order it lists them."""
    lines = [line for line in _markdown(state).splitlines() if line.startswith("| ")]
    header = [cell.strip() for cell in lines[0].strip("|").split("|")]
    return [dict(zip(header, (cell.strip() for cell in line.strip("|").split("|")), strict=True)) for line in lines[1:]]


def _html_titles_by_date(state: Path) -> list[tuple[str, list[str]]]:
    """The date sections of the first tab with the titles each lists, in page order."""
    sections = re.findall(
        r'<section class="group" data-group="date" data-date="([^"]*)"(.*?)</section>', _html(state), re.S
    )
    return [(day, re.findall(r'<span class="title">([^<]*)</span>', body)) for day, body in sections]


def _files(state: Path) -> dict[str, bytes]:
    return {
        name: (state / name).read_bytes() for name in ("weekly-review.md", "weekly-review.html", "editor-facing.csv")
    }


class ColumnTests:
    def test_a_review_with_no_recorded_score_is_unchanged(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        _render(state)
        before = _files(state)

        _render(state, _config(tmp_path, editorial_fit_ordering=True))

        assert _files(state) == before
        assert "Оценка" not in _markdown(state)
        assert "c-fit" not in _html(state)

    def test_the_column_shows_each_books_score_with_one_decimal(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        _write_scores(state, NEWER, [_score_record(1, 2.66), _score_record(2, 3.0)])

        _render(state)

        scores = {row["Название"]: row["Оценка"] for row in _markdown_rows(state)}
        assert scores == {"Book 1": "2.7", "Book 2": "3.0", "Book 3": "", "Book 4": "", "Book 5": ""}
        html = _html(state)
        assert '<span class="sortable" data-key="fit" data-type="num">Оценка' in html
        assert '<span class="num c-fit">2.7</span>' in html
        assert '<span class="num c-fit">—</span>' in html

    def test_the_html_column_sorts_the_highest_score_first_and_unscored_last(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        _write_scores(state, NEWER, [_score_record(1, 1.5), _score_record(2, 3.25)])

        _render(state)

        html = _html(state)
        values = {
            title: float(value)
            for value, title in re.findall(r'data-fit="([^"]*)"[^>]*>.*?<span class="title">([^<]*)</span>', html, re.S)
        }
        # The sorter lists ascending values first.
        assert values["Book 2"] < values["Book 1"] < values["Book 3"]

    def test_the_newest_scored_record_of_a_book_is_shown(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        _write_scores(state, OLDER, [_score_record(1, 1.0), _score_record(2, 2.0)])
        # Book 2's later call failed: its earlier score stands.
        _write_scores(state, NEWER, [_score_record(1, 3.0), _score_record(2, None)])

        _render(state)

        scores = {row["Название"]: row["Оценка"] for row in _markdown_rows(state)}
        assert (scores["Book 1"], scores["Book 2"]) == ("3.0", "2.0")

    def test_a_score_follows_the_book_through_a_renamed_identity_key(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        _write_scores(state, NEWER, [_score_record(1, 3.5, identity_key="book one|author 1")])

        _render(state)

        assert {row["Название"]: row["Оценка"] for row in _markdown_rows(state)}["Book 1"] == "3.5"

    def test_after_a_merge_the_newer_of_the_two_records_is_the_books_score(self, tmp_path: Path) -> None:
        # The surviving record keeps Book 1's identity key and durable ID; the
        # merged-away record was scored later, under a Candidate ID that now redirects.
        retired = "candidate-" + "9" * 24
        state = _state(tmp_path)
        seed_state(
            state,
            candidate_history=_history(),
            candidate_identity_aliases={
                "version": 2,
                "aliases": {},
                "candidates": {},
                "redirects": {retired: f"candidate-{1:024x}"},
                "durable_merges": [],
            },
        )
        _write_scores(state, OLDER, [_score_record(1, 1.0)])
        _write_scores(
            state, NEWER, [_score_record(9, 3.0, identity_key="book 1 (reissue)|author 1", candidate_id=retired)]
        )

        _render(state)

        assert {row["Название"]: row["Оценка"] for row in _markdown_rows(state)}["Book 1"] == "3.0"

    def test_an_unreadable_score_file_leaves_the_review_as_it_is_without_scores(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        _render(state)
        before = _files(state)
        directory = fit_scores.shadow_dir(state)
        directory.mkdir()
        (directory / "scores-2026-11-12.json").write_text("{not json", encoding="utf-8")

        _render(state, _config(tmp_path, editorial_fit_ordering=True))

        assert _files(state) == before

    def test_the_column_changes_no_state_and_no_priority(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        _write_scores(state, NEWER, [_score_record(1, 4.0)])
        before = state_snapshot(state)

        _render(state, _config(tmp_path, editorial_fit_ordering=True))

        assert state_snapshot(state) == before
        assert {row["Приоритет"] for row in _markdown_rows(state)} == {"C"}
        with (state / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
            assert "Оценка" not in (csv.DictReader(handle).fieldnames or [])


# A clear content risk for the Russian market: the hard floor that makes a book priority D.
RISKY = {"priority_label": "D", "visible_risk_status": "clear book-content visible risk"}


class OrderTests:
    def _scored_state(self, tmp_path: Path) -> Path:
        state = _state(tmp_path)
        # Book 3 has no score. Book 5 outscores every book of the newer date.
        _write_scores(
            state,
            NEWER,
            [_score_record(1, 1.0), _score_record(2, 3.0), _score_record(4, 2.0), _score_record(5, 4.0)],
        )
        return state

    def test_without_the_config_value_the_order_is_the_same_as_with_no_scores(self, tmp_path: Path) -> None:
        plain = _state(tmp_path / "plain")
        _render(plain)
        state = self._scored_state(tmp_path / "scored")

        _render(state, _config(tmp_path, editorial_fit_ordering=False))

        assert _html_titles_by_date(state) == _html_titles_by_date(plain)
        assert [row["Название"] for row in _markdown_rows(state)] == [row["Название"] for row in _markdown_rows(plain)]
        assert "Порядок книг" not in _markdown(state) + _html(state)

    def test_the_books_of_one_date_are_ordered_by_score_and_the_unscored_follow(self, tmp_path: Path) -> None:
        state = self._scored_state(tmp_path)

        _render(state, _config(tmp_path, editorial_fit_ordering=True))

        expected = [(NEWER, ["Book 2", "Book 1", "Book 3"]), (OLDER, ["Book 5", "Book 4"])]
        assert _html_titles_by_date(state) == expected
        assert [row["Название"] for row in _markdown_rows(state)] == [
            title for _day, titles in expected for title in titles
        ]

    def test_priority_d_books_follow_every_other_book_of_their_date_whatever_their_score(self, tmp_path: Path) -> None:
        history = [
            _record(1, NEWER),
            _record(2, NEWER, **RISKY),
            _record(3, NEWER),
            _record(6, NEWER, **RISKY),
            _record(4, OLDER, **RISKY),
            _record(5, OLDER),
        ]
        state = _state(tmp_path, history)
        # Book 2 has the top score and book 6 the next, but both are priority D;
        # book 3 has no score and is not D; book 4 is D with no score.
        _write_scores(state, NEWER, [_score_record(1, 1.0), _score_record(2, 4.0), _score_record(6, 2.0)])

        _render(state, _config(tmp_path, editorial_fit_ordering=True))

        expected = [(NEWER, ["Book 1", "Book 3", "Book 2", "Book 6"]), (OLDER, ["Book 5", "Book 4"])]
        assert _html_titles_by_date(state) == expected
        assert [row["Название"] for row in _markdown_rows(state)] == [
            title for _day, titles in expected for title in titles
        ]

    def test_without_the_config_value_priority_d_books_keep_their_place(self, tmp_path: Path) -> None:
        history = [_record(1, NEWER, **RISKY), _record(2, NEWER), _record(3, NEWER)]
        plain = _state(tmp_path / "plain", history)
        _render(plain)
        state = _state(tmp_path / "scored", history)
        _write_scores(state, NEWER, [_score_record(1, 4.0), _score_record(2, 1.0)])

        _render(state, _config(tmp_path, editorial_fit_ordering=False))

        assert _html_titles_by_date(state) == _html_titles_by_date(plain)

    def test_with_no_recorded_score_priority_d_books_keep_their_place(self, tmp_path: Path) -> None:
        # Seen again more recently, the D book leads its date in the order without scores.
        history = [_record(1, OLDER, **RISKY, last_seen_date=NEWER), _record(2, OLDER), _record(3, OLDER)]
        plain = _state(tmp_path / "plain", history)
        _render(plain)
        assert _html_titles_by_date(plain)[-1][1][0] == "Book 1"
        state = _state(tmp_path / "ordered", history)

        _render(state, _config(tmp_path, editorial_fit_ordering=True))

        assert _html_titles_by_date(state) == _html_titles_by_date(plain)

    def test_the_row_numbers_follow_the_order_inside_a_date(self, tmp_path: Path) -> None:
        # The browser returns a tab to page order by the row number.
        state = self._scored_state(tmp_path)

        _render(state, _config(tmp_path, editorial_fit_ordering=True))

        numbers = {row["Название"]: int(row["#"]) for row in _markdown_rows(state)}
        assert numbers["Book 2"] < numbers["Book 1"] < numbers["Book 3"]
        assert numbers["Book 5"] < numbers["Book 4"]

    def test_the_review_says_that_the_order_uses_the_score(self, tmp_path: Path) -> None:
        state = self._scored_state(tmp_path)

        _render(state, _config(tmp_path, editorial_fit_ordering=True))

        assert f"**Порядок книг**: {fit_scores.FIT_ORDER_NOTE}" in _markdown(state)
        assert f"Порядок книг: {fit_scores.FIT_ORDER_NOTE}" in _html(state)

    def test_no_book_leaves_the_review(self, tmp_path: Path) -> None:
        state = self._scored_state(tmp_path)

        _render(state, _config(tmp_path, editorial_fit_ordering=True))

        assert sorted(row["Название"] for row in _markdown_rows(state)) == [f"Book {index}" for index in range(1, 6)]

    def test_turning_the_value_off_restores_the_order(self, tmp_path: Path) -> None:
        state = self._scored_state(tmp_path)
        _render(state)
        before = _files(state)
        _render(state, _config(tmp_path, editorial_fit_ordering=True))
        assert _files(state) != before

        _render(state, _config(tmp_path, editorial_fit_ordering=False))

        assert _files(state) == before

    def test_the_config_value_is_a_known_boolean(self) -> None:
        assert not validate_config({"sources": [], "editorial_fit_ordering": True})
        assert validate_config({"sources": [], "editorial_fit_ordering": "yes"})


class ActivationMarkTests:
    def test_a_rescore_of_the_same_run_keeps_the_mark(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run"
        _write(run_dir / "scouting-candidates.json", [])
        seed_state(run_dir, candidate_history=[])
        adapter = ReplayJevAdapter({})
        fit_shadow.score_weekly_run(run_dir, run_dir, NEWER, adapter, review_ordered_by_fit=True)

        fit_shadow.score_weekly_run(run_dir, run_dir, NEWER, adapter)

        artifact = json.loads(fit_shadow.scores_path(run_dir, NEWER).read_text(encoding="utf-8"))
        assert artifact["review_ordered_by_fit"] is True

    def test_a_run_is_marked_only_when_its_review_was_ordered_by_the_score(self, tmp_path: Path) -> None:
        state = _state(tmp_path, [])
        _write_scores(state, OLDER, [_score_record(1, 1.0)])
        _write_scores(state, NEWER, [_score_record(2, 2.0)], review_ordered_by_fit=True)

        report = fit_shadow.build_shadow_report(state, state)

        assert [(week["run_date"], week["review_ordered_by_fit"]) for week in report["weeks"]] == [
            (OLDER, False),
            (NEWER, True),
        ]
        text = fit_shadow.render_shadow_report(report)
        assert text.count("After activation: the review of this run was ordered by the fit score.") == 1


def _minimum_row_width(template: str) -> float:
    """The least width, in px at 16px per rem, the tracks of a row grid and the gaps between them need."""
    tracks = re.findall(r"minmax\(([\d.]+)(px|rem),[^)]*\)|([\d.]+)(px|rem)", template)
    widths = [float(a or c) * (16 if (b or d) == "rem" else 1) for a, b, c, d in tracks]
    return sum(widths) + 0.75 * 16 * (len(widths) - 1)


def test_the_row_with_the_score_column_needs_no_more_width_than_without_it() -> None:
    # Above 1000px the row is a grid inside a clipped table: a wider minimum would cut the decision off.
    from autoscout import html_review

    def template(style: str) -> str:
        match = re.search(r"\.thead, \.cand > summary \{[^}]*?grid-template-columns: ([^;]+);", style)
        assert match is not None
        return match.group(1)

    assert _minimum_row_width(template(html_review._FIT_STYLE)) <= _minimum_row_width(template(html_review._STYLE))
