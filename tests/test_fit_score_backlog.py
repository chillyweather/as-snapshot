"""Offline tests for scoring the undecided backlog with the ``fit-v2`` question set.

A run scores only its own candidates. These tests cover the books of earlier
runs that the editor has not decided on: which of them are scored, what the
review reads back, and that the shadow evaluation never sees these scores.
No network and no model are used.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from state_store_support import seed_state
from state_store_support import state_snapshot

from autoscout import editorial_fit_shadow as fit_shadow
from autoscout import jev_adapter
from autoscout.fit_scores import read_fit_scores
from autoscout.jev_adapter import ReplayJevAdapter

RUN_DATE = "2026-11-16"


def _book(index: int, **extra: Any) -> dict[str, Any]:
    book = {
        "identity_key": f"book {index}|author {index}",
        "candidate_id": f"candidate-{index:04d}",
        "title": f"Book {index}",
        "primary_authors": [f"Author {index}"],
        "candidate_category": "adult nonfiction",
        "content_description": f"Описание книги {index}.",
        "first_seen_date": f"2026-10-{index:02d}",
        "priority_label": "C",
    }
    book.update(extra)
    return book


def _response(fit_level: int) -> dict[str, Any]:
    def one_hot(labels: list[str], chosen: str) -> dict[str, float]:
        return {label: 1.0 if label == chosen else 0.0 for label in labels}

    return {
        "model": jev_adapter.JEV_PINNED_MODEL,
        "usage": {"input_tokens": 900},
        "latency_ms": 700,
        "answers": {
            "fit": {"probabilities": one_hot(["0", "1", "2", "3", "4"], str(fit_level)), "confidence": 0.9},
            "audience": {
                "probabilities": one_hot([label for label, _ in fit_shadow.AUDIENCE_OPTIONS], "mass_market"),
                "confidence": 0.8,
            },
            "author_reach": {
                "probabilities": one_hot([label for label, _ in fit_shadow.AUTHOR_REACH_OPTIONS], "unclear"),
                "confidence": 0.7,
            },
        },
    }


def _replay(pairs: list[tuple[dict[str, Any], int]]) -> ReplayJevAdapter:
    return ReplayJevAdapter(
        {
            jev_adapter.request_digest(fit_shadow.fit_model_state(book), fit_shadow.FIT_QUESTIONS): _response(level)
            for book, level in pairs
        }
    )


def _run_dir(tmp_path: Path, history: list[dict[str, Any]], candidates: list[dict[str, Any]] | None = None) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    (run_dir / "scouting-candidates.json").write_text(json.dumps(candidates or []), encoding="utf-8")
    seed_state(run_dir, candidate_history=history)
    return run_dir


def _backlog(run_dir: Path, run_date: str = RUN_DATE) -> dict[str, Any]:
    return json.loads(fit_shadow.backlog_scores_path(run_dir, run_date).read_text(encoding="utf-8"))


def _fit(run_dir: Path, book: dict[str, Any]) -> float | None:
    return read_fit_scores(run_dir, {}).score_for(book)


def test_undecided_books_without_a_score_are_scored_and_the_review_reads_them(tmp_path: Path) -> None:
    books = [_book(1), _book(2)]
    run_dir = _run_dir(tmp_path, books)
    before = state_snapshot(run_dir)

    result = fit_shadow.score_undecided_backlog(run_dir, run_dir, RUN_DATE, _replay([(books[0], 4), (books[1], 1)]))

    assert (result["undecided_without_score"], result["scored"], result["not_attempted"]) == (2, 2, 0)
    assert (_fit(run_dir, books[0]), _fit(run_dir, books[1])) == (4.0, 1.0)
    assert state_snapshot(run_dir) == before


def test_decided_hidden_and_already_scored_books_are_not_called(tmp_path: Path) -> None:
    decided = _book(1, editor_status="reject", editor_note="Не наше.")
    hidden = _book(2)
    scored_by_a_run = _book(3)
    waiting = _book(4)
    run_dir = _run_dir(tmp_path, [decided, hidden, scored_by_a_run, waiting], candidates=[scored_by_a_run])
    seed_state(run_dir, candidate_visibility=[{"identity_key": hidden["identity_key"], "visibility": "hidden"}])
    fit_shadow.score_weekly_run(run_dir, run_dir, "2026-11-10", _replay([(scored_by_a_run, 3)]))

    fit_shadow.score_undecided_backlog(
        run_dir, run_dir, RUN_DATE, _replay([(book, 2) for book in (decided, hidden, scored_by_a_run, waiting)])
    )

    assert [record["identity_key"] for record in _backlog(run_dir)["records"]] == [waiting["identity_key"]]


def test_backlog_scores_never_enter_the_shadow_evaluation(tmp_path: Path) -> None:
    book = _book(1)
    run_dir = _run_dir(tmp_path, [book])
    fit_shadow.score_undecided_backlog(run_dir, run_dir, RUN_DATE, _replay([(book, 4)]))
    seed_state(run_dir, candidate_history=[{**book, "editor_status": "interested"}])

    report = fit_shadow.build_shadow_report(run_dir, run_dir)

    assert fit_shadow.labeled_observations(run_dir, run_dir) == []
    assert report["shadow_weeks_scored"] == 0
    assert report["operations"]["calls"] == 0


def test_a_later_run_score_replaces_the_backlog_score(tmp_path: Path) -> None:
    book = _book(1)
    run_dir = _run_dir(tmp_path, [book], candidates=[book])
    fit_shadow.score_undecided_backlog(run_dir, run_dir, RUN_DATE, _replay([(book, 1)]))

    fit_shadow.score_weekly_run(run_dir, run_dir, RUN_DATE, _replay([(book, 4)]))

    assert _fit(run_dir, book) == 4.0


def test_a_spent_budget_leaves_the_rest_for_the_next_run_newest_first(tmp_path: Path) -> None:
    older, newer = _book(1), _book(2)
    run_dir = _run_dir(tmp_path, [older, newer])
    adapter = _replay([(older, 3), (newer, 3)])
    clock = iter([0.0, 0.0, 100.0])

    first = fit_shadow.score_undecided_backlog(
        run_dir, run_dir, RUN_DATE, adapter, workers=1, budget_seconds=10.0, monotonic=lambda: next(clock)
    )

    assert (first["scored"], first["not_attempted"]) == (1, 1)
    assert [record["identity_key"] for record in _backlog(run_dir)["records"]] == [newer["identity_key"]]

    second = fit_shadow.score_undecided_backlog(run_dir, run_dir, "2026-11-17", adapter)

    assert (second["undecided_without_score"], second["scored"]) == (1, 1)
    assert _fit(run_dir, older) == 3.0


def test_no_call_writes_no_artifact(tmp_path: Path) -> None:
    run_dir = _run_dir(tmp_path, [_book(1)])

    result = fit_shadow.score_undecided_backlog(
        run_dir, run_dir, RUN_DATE, ReplayJevAdapter({}), budget_seconds=0.0, monotonic=lambda: 0.0
    )

    assert result["not_attempted"] == 1
    assert not fit_shadow.backlog_scores_path(run_dir, RUN_DATE).exists()


def test_a_failed_call_is_retried_the_same_day_and_replaced(tmp_path: Path) -> None:
    books = [_book(1), _book(2)]
    run_dir = _run_dir(tmp_path, books)
    fit_shadow.score_undecided_backlog(run_dir, run_dir, RUN_DATE, _replay([(books[0], 3)]))
    assert [record["status"] for record in _backlog(run_dir)["records"]] == ["unavailable", "scored"]

    result = fit_shadow.score_undecided_backlog(run_dir, run_dir, RUN_DATE, _replay([(books[1], 2)]))

    assert result["undecided_without_score"] == 1
    assert sorted((record["title"], record["status"]) for record in _backlog(run_dir)["records"]) == [
        ("Book 1", "scored"),
        ("Book 2", "scored"),
    ]


def test_the_weekly_stage_scores_the_backlog_after_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    current, earlier = _book(1), _book(2)
    run_dir = _run_dir(tmp_path, [current, earlier], candidates=[current])
    monkeypatch.setattr(jev_adapter, "load_api_key", lambda: "k")
    monkeypatch.setattr(jev_adapter, "LiveJevAdapter", lambda key: _replay([(current, 4), (earlier, 2)]))

    result = fit_shadow.ProductionEditorialFitShadowStage().score(run_dir, run_dir, RUN_DATE)

    assert result["scored"] == 1
    assert result["backlog"]["scored"] == 1
    assert (_fit(run_dir, current), _fit(run_dir, earlier)) == (4.0, 2.0)


def test_a_backlog_failure_leaves_the_scores_of_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    current = _book(1)
    run_dir = _run_dir(tmp_path, [current], candidates=[current])
    monkeypatch.setattr(jev_adapter, "load_api_key", lambda: "k")
    monkeypatch.setattr(jev_adapter, "LiveJevAdapter", lambda key: _replay([(current, 4)]))

    def broken(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("backlog broke")

    monkeypatch.setattr(fit_shadow, "score_undecided_backlog", broken)

    result = fit_shadow.ProductionEditorialFitShadowStage().score(run_dir, run_dir, RUN_DATE)

    assert result["scored"] == 1
    assert result["backlog"] == {"status": "failed", "exception_type": "RuntimeError"}
    assert _fit(run_dir, current) == 4.0


class _Refusing:
    kind = "live"

    def __init__(self) -> None:
        self.calls = 0

    def assess(self, state: Any, questions: Any) -> jev_adapter.JevAssessment:
        self.calls += 1
        return jev_adapter.unavailable(self.kind, "auth_failed")


def test_a_refused_key_stops_the_backlog_after_the_first_call(tmp_path: Path) -> None:
    run_dir = _run_dir(tmp_path, [_book(index) for index in range(1, 6)])
    adapter = _Refusing()

    result = fit_shadow.score_undecided_backlog(run_dir, run_dir, RUN_DATE, adapter, workers=1)

    assert adapter.calls == 1
    assert (result["unavailable"], result["not_attempted"]) == (1, 4)


def test_a_key_refused_in_the_run_is_not_tried_on_the_backlog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    current, earlier = _book(1), _book(2)
    run_dir = _run_dir(tmp_path, [current, earlier], candidates=[current])
    adapter = _Refusing()
    monkeypatch.setattr(jev_adapter, "load_api_key", lambda: "k")
    monkeypatch.setattr(jev_adapter, "LiveJevAdapter", lambda key: adapter)

    result = fit_shadow.ProductionEditorialFitShadowStage().score(run_dir, run_dir, RUN_DATE)

    assert adapter.calls == 1
    assert result["backlog"] == {"status": "skipped", "reason": "auth_failed"}


def test_a_key_fixed_the_same_day_reaches_the_backlog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    current, earlier = _book(1), _book(2)
    run_dir = _run_dir(tmp_path, [current, earlier], candidates=[current])
    monkeypatch.setattr(jev_adapter, "load_api_key", lambda: "k")
    monkeypatch.setattr(jev_adapter, "LiveJevAdapter", lambda key: _Refusing())
    fit_shadow.ProductionEditorialFitShadowStage().score(run_dir, run_dir, RUN_DATE)
    # The editor decides, so the refused record of the run is kept as it is.
    seed_state(run_dir, candidate_history=[{**current, "editor_status": "reject"}, earlier])
    monkeypatch.setattr(jev_adapter, "LiveJevAdapter", lambda key: _replay([(earlier, 3)]))

    result = fit_shadow.ProductionEditorialFitShadowStage().score(run_dir, run_dir, RUN_DATE)

    assert result["unavailable_reason_counts"] == {"auth_failed": 1}
    assert result["backlog"]["scored"] == 1


def test_a_score_recorded_under_an_earlier_identity_key_counts_for_the_renamed_book(tmp_path: Path) -> None:
    old = _book(1)
    renamed = {**old, "identity_key": "book one|author 1"}
    run_dir = _run_dir(tmp_path, [old])
    fit_shadow.score_undecided_backlog(run_dir, run_dir, RUN_DATE, _replay([(old, 3)]))
    # The identity key is corrected; the durable Candidate ID stays.
    seed_state(run_dir, candidate_history=[renamed])

    result = fit_shadow.score_undecided_backlog(run_dir, run_dir, "2026-11-17", _replay([(renamed, 0)]))

    assert result["undecided_without_score"] == 0
