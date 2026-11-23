"""Issue #765: name a source whose row count departs from its own recent runs.

The seven cases of the issue are the acceptance set. Each is a fresh
collection compared with the source's earlier fresh collections; the counts
are the ones the issue records.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoscout.source_row_history import HISTORY_LENGTH
from autoscout.source_row_history import STATE_SOURCE_ROW_HISTORY
from autoscout.source_row_history import load_row_history
from autoscout.source_row_history import recorded_departure_warnings
from autoscout.source_row_history import row_count_departures
from autoscout.source_row_history import updated_row_history
from autoscout.source_row_history import write_row_history

RUN_DATE = "2026-10-05"


def _run(name: str, valid: int, extracted: int | None = None, status: str = "collected") -> dict:
    return {
        "source_name": name,
        "status": status,
        "valid_mention_count": valid,
        "extracted_mention_count": valid if extracted is None else extracted,
    }


def _history(name: str, *counts: int) -> dict:
    return {
        name: [{"run_date": f"2026-09-{day:02d}", "valid_rows": count} for day, count in enumerate(counts, start=1)]
    }


@pytest.mark.parametrize(
    ("name", "earlier", "valid", "extracted", "rule"),
    [
        ("Die Zeit Sachbuch-Bestenliste", (10, 10, 10), 1, None, "fixed_list"),
        ("Cúspide 100 más vendidos", (12, 15, 13), 3, 16, "dropped_rows"),
        ("Cundill History Prize 2026 Longlist", (15, 15, 15), 13, None, "fixed_list"),
        ("National Book Critics Circle Awards", (20, 19, 21), 42, None, "varying_list"),
        ("Publishers Weekly Hardcover Frontlist Nonfiction", (25, 24, 25), 10, None, "varying_list"),
        ("Goodreads Choice Awards 2025 Nonfiction", (20, 20, 20), 1, None, "fixed_list"),
        ("Académie française Prix de l'essai", (55, 55, 55), 5, None, "fixed_list"),
    ],
)
def test_each_case_of_the_issue_is_flagged(name, earlier, valid, extracted, rule) -> None:
    departures = row_count_departures([_run(name, valid, extracted)], _history(name, *earlier), RUN_DATE)

    assert [(d.source_name, d.rule) for d in departures] == [(name, rule)]
    warning = departures[0].warning()
    assert name in warning and str(valid) in warning


def test_a_single_row_reads_as_one_row() -> None:
    departures = row_count_departures([_run("Goodreads", 1)], _history("Goodreads", 20, 20, 20), RUN_DATE)

    assert departures[0].warning().startswith("Goodreads: 1 row, usually exactly 20;")


def test_the_warnings_carry_both_numbers() -> None:
    fixed = row_count_departures([_run("Cundill", 13)], _history("Cundill", 15, 15, 15), RUN_DATE)
    varying = row_count_departures([_run("PW", 10)], _history("PW", 25, 24, 25), RUN_DATE)
    dropped = row_count_departures([_run("Cúspide", 3, 16)], {}, RUN_DATE)

    assert fixed[0].warning() == "Cundill: 13 rows, usually exactly 15; possibly a partial list or a page change"
    assert varying[0].warning() == (
        "PW: 10 rows, usually about 25 (recent 24–25); possibly a partial list or a page change"
    )
    assert dropped[0].warning() == "Cúspide: kept 3 of 16 extracted rows; most rows were dropped by validation"


@pytest.mark.parametrize("earlier", [(), (15,), (15, 15)])
def test_a_source_with_too_little_history_is_not_flagged(earlier) -> None:
    assert row_count_departures([_run("New Source", 3)], _history("New Source", *earlier), RUN_DATE) == []


def test_ordinary_variation_of_a_varying_list_is_not_flagged() -> None:
    history = _history("Weekly Chart", 12, 15, 13)

    assert row_count_departures([_run("Weekly Chart", 10)], history, RUN_DATE) == []
    assert row_count_departures([_run("Weekly Chart", 20)], history, RUN_DATE) == []


def test_a_fixed_list_at_its_usual_count_is_not_flagged() -> None:
    assert row_count_departures([_run("Cundill", 15)], _history("Cundill", 15, 15, 15), RUN_DATE) == []


def test_a_few_dropped_rows_are_not_flagged() -> None:
    assert row_count_departures([_run("Small", 2, 4)], {}, RUN_DATE) == []
    assert row_count_departures([_run("Chart", 12, 20)], {}, RUN_DATE) == []


@pytest.mark.parametrize("status", ["reused", "failed", "skipped", "inactive"])
def test_only_a_fresh_collection_is_compared_or_recorded(status) -> None:
    run = _run("Cundill", 13, status=status)
    history = _history("Cundill", 15, 15, 15)

    assert row_count_departures([run], history, RUN_DATE) == []
    assert updated_row_history(history, [run], RUN_DATE) == history


def test_a_second_run_on_the_same_day_is_compared_with_the_days_before() -> None:
    history = {"Cundill": [*_history("Cundill", 15, 15, 15)["Cundill"], {"run_date": RUN_DATE, "valid_rows": 13}]}

    departures = row_count_departures([_run("Cundill", 13)], history, RUN_DATE)
    updated = updated_row_history(history, [_run("Cundill", 14)], RUN_DATE)

    assert [d.rule for d in departures] == ["fixed_list"]
    assert [entry["valid_rows"] for entry in updated["Cundill"]] == [15, 15, 15, 14]


def test_the_history_keeps_the_most_recent_runs() -> None:
    history: dict = {}
    for day in range(1, HISTORY_LENGTH + 4):
        history = updated_row_history(history, [_run("Chart", day)], f"2026-09-{day:02d}")

    # The window of earlier days, plus the latest day itself.
    assert len(history["Chart"]) == HISTORY_LENGTH + 1
    assert history["Chart"][-1] == {"run_date": f"2026-09-{HISTORY_LENGTH + 3:02d}", "valid_rows": HISTORY_LENGTH + 3}


def test_a_same_day_rerun_keeps_the_warning_of_the_first_run() -> None:
    """Codex review of #765: with a full history, adding today's entry evicted
    an earlier day, so a rerun the same day compared with fewer earlier runs
    and lost the warning for identical evidence."""
    history = {
        "Chart": [
            {"run_date": f"2026-09-{day:02d}", "valid_rows": count}
            for day, count in enumerate([20, 20, 20, 20, 5, 5, 5, 5], start=1)
        ]
    }
    first = row_count_departures([_run("Chart", 6)], history, RUN_DATE)
    after_first = updated_row_history(history, [_run("Chart", 6)], RUN_DATE)
    second = row_count_departures([_run("Chart", 6)], after_first, RUN_DATE)

    assert [d.rule for d in first] == ["varying_list"]
    assert second == first


def test_the_history_round_trips_and_a_malformed_file_reads_as_empty(tmp_path: Path) -> None:
    history = _history("Cundill", 15, 15, 15)
    write_row_history(tmp_path, history)

    assert load_row_history(tmp_path) == history
    (tmp_path / STATE_SOURCE_ROW_HISTORY).write_text("[1, 2]", encoding="utf-8")
    assert load_row_history(tmp_path) == {}
    assert load_row_history(None) == {}


def test_recorded_departures_rebuild_their_warnings() -> None:
    departures = row_count_departures([_run("Cundill", 13)], _history("Cundill", 15, 15, 15), RUN_DATE)
    records = [departure.to_dict() for departure in departures]

    assert recorded_departure_warnings(records) == [departures[0].warning()]
    assert recorded_departure_warnings([{"source_name": "x", "rule": "unknown"}, "junk"]) == []
    assert recorded_departure_warnings(None) == []


def test_a_weekly_pipeline_run_names_the_source_and_records_its_count(tmp_path: Path) -> None:
    """End to end on the golden-run fixture config, with a seeded state.

    "Fixture Collector Recorded Golden Source" collects 3 rows; a history of
    exactly 8 makes it a fixed list that came back short.
    """
    from autoscout.cli.weekly import run_mvp
    from tests.test_golden_run import _build_config

    config_path = _build_config(tmp_path)
    state_dir = tmp_path / "state"
    name = "Fixture Collector Recorded Golden Source"
    write_row_history(state_dir, {name: [{"run_date": f"2026-05-{day:02d}", "valid_rows": 8} for day in (1, 8, 15)]})

    run_mvp(config_path, tmp_path / "out", state_dir)

    summary = json.loads((tmp_path / "out" / "run-summary.json").read_text(encoding="utf-8"))
    assert summary["row_count_departures"] == [
        {
            "source_name": name,
            "valid_rows": 3,
            "extracted_rows": 3,
            "rule": "fixed_list",
            "expected": "usually exactly 8",
            "usual": 8.0,
        }
    ]
    assert f"{name}: 3 rows, usually exactly 8; possibly a partial list or a page change" in summary["major_warnings"]
    retained = load_row_history(state_dir)
    assert retained[name][-1] == {"run_date": summary["run_date"], "valid_rows": 3}
    assert retained["BookPage Best Nonfiction"] == [{"run_date": summary["run_date"], "valid_rows": 1}]


def test_a_source_with_a_seen_item_index_is_neither_compared_nor_recorded() -> None:
    """Nonfiction.fr reports only reviews it has not shown before: 5, 3, 9, 4, 5, 0, 2, 0."""
    from autoscout.source_row_history import incremental_source_names

    sources = [
        {"name": "Nonfiction.fr Critiques", "seen_guids_file": "nonfiction-fr-seen-guids.json"},
        {"name": "Cundill History Prize 2026 Longlist"},
    ]
    incremental = incremental_source_names(sources)
    history = _history("Nonfiction.fr Critiques", 5, 3, 9, 4, 5)
    run = _run("Nonfiction.fr Critiques", 0)

    assert incremental == {"Nonfiction.fr Critiques"}
    assert row_count_departures([run], history, RUN_DATE, incremental_sources=incremental) == []
    assert updated_row_history(history, [run], RUN_DATE, incremental_sources=incremental) == history


def test_a_tiny_varying_list_is_not_flagged_for_halving() -> None:
    """PEOPLE Books carries one to five books: 5, 4, 2, 1, 1, 5, 3, 3."""
    history = _history("PEOPLE Books", 5, 4, 2, 1, 1, 5, 3, 3)

    assert row_count_departures([_run("PEOPLE Books", 1)], history, RUN_DATE) == []
    assert row_count_departures([_run("PEOPLE Books", 6)], history, RUN_DATE) == []


def test_the_editor_reads_a_short_russian_detail_for_each_rule() -> None:
    fixed = row_count_departures([_run("Cundill", 13)], _history("Cundill", 15, 15, 15), RUN_DATE)[0]
    varying = row_count_departures([_run("PW", 10)], _history("PW", 25, 24, 25), RUN_DATE)[0]
    dropped = row_count_departures([_run("Cúspide", 3, 16)], {}, RUN_DATE)[0]

    assert fixed.editor_detail() == "13 вместо обычных 15"
    assert varying.editor_detail() == "10, обычно около 25"
    assert dropped.editor_detail() == "оставлено 3 из 16 найденных"


def test_the_review_line_and_the_weekly_completion_name_the_departure() -> None:
    from autoscout.russian_workflow import source_problem_message
    from autoscout.source_row_history import departure_review_lines

    records = [
        departure.to_dict()
        for departure in row_count_departures(
            [_run("Cundill", 13), _run("Cúspide", 3, 16)], _history("Cundill", 15, 15, 15), RUN_DATE
        )
    ]

    assert departure_review_lines(records) == [
        "Необычное число книг: Cundill — 13 вместо обычных 15; Cúspide — оставлено 3 из 16 найденных."
    ]
    assert departure_review_lines([]) == []
    message = source_problem_message(
        [{"source_name": "Cundill", "status": "unusual_count", "editor_detail": "13 вместо обычных 15"}]
    )
    assert message == "⚠ необычное число книг: Cundill (13 вместо обычных 15)."


def test_a_departure_recorded_before_the_usual_field_still_reads() -> None:
    from autoscout.source_row_history import recorded_departures

    old = {"source_name": "Cundill", "valid_rows": 13, "extracted_rows": 13, "rule": "fixed_list", "expected": "x"}

    assert recorded_departures([old])[0].editor_detail() == "13"


def test_the_rendered_review_names_the_departure(tmp_path: Path) -> None:
    from autoscout.json_io import write_json
    from autoscout.review_rendering import render_review

    write_json(
        tmp_path / "scouting-candidates.json",
        [
            {
                "identity_key": "book one|alex example",
                "title": "Book One",
                "primary_authors": ["Alex Example"],
                "update_delta": "new",
                "priority_label": "A",
                "independent_signal_count": 1,
                "russian_edition_status": "not found in Russian",
                "content_description": "Описание книги.",
                "source_evidence": [{"source_name": "Cundill", "source_url": "https://example.test/c"}],
            }
        ],
    )
    departure = row_count_departures([_run("Cundill", 13)], _history("Cundill", 15, 15, 15), RUN_DATE)[0]
    write_json(
        tmp_path / "run-summary.json",
        {"run_date": RUN_DATE, "editor_facing_count": 1, "row_count_departures": [departure.to_dict()]},
    )

    assert render_review(tmp_path) == 0

    line = "Необычное число книг: Cundill — 13 вместо обычных 15."
    assert f"**⚠ Источники**: {line}" in (tmp_path / "weekly-review.md").read_text(encoding="utf-8")
    assert line in (tmp_path / "weekly-review.html").read_text(encoding="utf-8")
