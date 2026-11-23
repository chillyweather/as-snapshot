"""Issue #1055: the source yield report counts the editor's decisions per source and changes no state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from state_store_support import seed_state
from state_store_support import state_snapshot

from autoscout import source_yield_report as report_module
from autoscout.__main__ import main
from autoscout.editor_decisions import EDITOR_STATUSES
from autoscout.source_yield_report import MAX_USEFUL_PERCENT
from autoscout.source_yield_report import MIN_DECIDED_BOOKS
from autoscout.source_yield_report import NEAR_MAX_USEFUL_PERCENT
from autoscout.source_yield_report import NEAR_MIN_DECIDED_BOOKS
from autoscout.source_yield_report import SECTION_ACTIVE
from autoscout.source_yield_report import SECTION_ANNUAL
from autoscout.source_yield_report import SECTION_RETIRED
from autoscout.source_yield_report import SECTION_UNLISTED
from autoscout.source_yield_report import USEFUL_STATUSES
from autoscout.source_yield_report import VERDICT_CLOSE
from autoscout.source_yield_report import VERDICT_MEETS_RULE
from autoscout.source_yield_report import SourceYield
from autoscout.source_yield_report import book_source_names
from autoscout.source_yield_report import build_source_yield_report
from autoscout.source_yield_report import verdict_for

CONFIGURED = [
    {"name": "Alpha", "active": True},
    {"name": "Beta", "active": True},
    {"name": "Gamma", "active": False, "lifecycle_status": "retired"},
    {"name": "Prize 2026", "active": True, "check_frequency": "annual"},
]


def book(
    sources: list[str],
    status: str = "",
    first_seen: str = "2026-11-01",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "first_seen_date": first_seen,
        "editor_status": status,
        "source_evidence": [{"source_name": name} for name in sources],
        **extra,
    }


def source(report: report_module.SourceYieldReport, name: str) -> SourceYield:
    return next(entry for entry in report.sources if entry.name == name)


def test_a_book_counts_for_every_source_that_reported_it() -> None:
    report = build_source_yield_report([book(["Alpha", "Beta"], "interested")], CONFIGURED)

    assert (source(report, "Alpha").decided, source(report, "Alpha").useful) == (1, 1)
    assert (source(report, "Beta").decided, source(report, "Beta").useful) == (1, 1)
    assert (report.decided, report.useful) == (1, 1)


def test_a_source_of_an_earlier_run_still_counts_as_a_source_of_the_book() -> None:
    record = book(["Alpha"], "reject", earlier_source_names=["Beta"])

    assert book_source_names(record) == {"Alpha", "Beta"}
    report = build_source_yield_report([record], CONFIGURED)
    assert source(report, "Beta").rejected == 1
    assert source(report, "Beta").only_here == 0


def test_only_here_counts_books_no_other_source_reported_decided_or_not() -> None:
    history = [
        book(["Alpha"], "reject"),
        book(["Alpha"]),
        book(["Alpha", "Beta"], "reject"),
    ]

    report = build_source_yield_report(history, CONFIGURED)

    assert source(report, "Alpha").only_here == 2
    assert source(report, "Beta").only_here == 0


def test_already_known_is_counted_but_not_useful() -> None:
    history = [book(["Alpha"], "already_known"), book(["Alpha"], "reject"), book(["Alpha"], "maybe_watch")]

    alpha = source(build_source_yield_report(history, CONFIGURED), "Alpha")

    assert (alpha.decided, alpha.already_known, alpha.rejected, alpha.useful) == (3, 1, 1, 1)


@pytest.mark.parametrize("status", sorted(USEFUL_STATUSES))
def test_each_useful_status_counts_as_useful(status: str) -> None:
    alpha = source(build_source_yield_report([book(["Alpha"], status)], CONFIGURED), "Alpha")

    assert alpha.useful == 1


def test_useful_statuses_are_editor_statuses_that_exclude_reject_and_already_known() -> None:
    assert USEFUL_STATUSES <= EDITOR_STATUSES
    assert not USEFUL_STATUSES & {"reject", "already_known"}


def test_undecided_books_are_left_out_of_every_yield_count() -> None:
    history = [book(["Alpha"], ""), book(["Alpha"], "   "), {**book(["Alpha"]), "editor_status": None}]

    report = build_source_yield_report(history, CONFIGURED)
    alpha = source(report, "Alpha")

    assert (alpha.decided, alpha.useful, report.decided, report.useful) == (0, 0, 0, 0)
    assert alpha.books == 3
    assert alpha.useful_percent is None


def test_first_and_last_book_are_the_earliest_and_latest_first_seen_dates() -> None:
    history = [
        book(["Alpha"], "reject", first_seen="2026-09-02"),
        book(["Alpha"], "", first_seen="2026-07-01"),
        book(["Alpha"], "reject", first_seen="2026-10-05"),
    ]

    alpha = source(build_source_yield_report(history, CONFIGURED), "Alpha")

    assert (alpha.first_book, alpha.last_book) == ("2026-07-01", "2026-10-05")


def test_a_book_with_no_source_is_counted_apart_and_in_no_row() -> None:
    history = [{"editor_status": "reject", "first_seen_date": "2026-11-01"}]

    report = build_source_yield_report(history, CONFIGURED)

    assert report.books_without_source == 1
    assert report.decided == 1
    assert all(entry.decided == 0 for entry in report.sources)


def test_sources_are_sorted_into_sections_by_the_portfolio() -> None:
    history = [book(["Alpha", "Gamma", "Prize 2026", "Elsewhere"], "reject")]

    report = build_source_yield_report(history, CONFIGURED)

    assert source(report, "Alpha").section == SECTION_ACTIVE
    assert source(report, "Gamma").section == SECTION_RETIRED
    assert source(report, "Prize 2026").section == SECTION_ANNUAL
    assert source(report, "Elsewhere").section == SECTION_UNLISTED


def test_a_retired_source_is_reported_with_its_numbers_and_no_verdict() -> None:
    history = [book(["Gamma"], "reject") for _ in range(MIN_DECIDED_BOOKS)]

    gamma = source(build_source_yield_report(history, CONFIGURED), "Gamma")

    assert (gamma.decided, gamma.useful) == (MIN_DECIDED_BOOKS, 0)
    assert gamma.verdict == ""


def test_a_yearly_prize_list_gets_no_verdict_whatever_its_numbers() -> None:
    history = [book(["Prize 2026"], "reject") for _ in range(MIN_DECIDED_BOOKS)]

    prize = source(build_source_yield_report(history, CONFIGURED), "Prize 2026")

    assert prize.verdict == ""


def test_the_rule_needs_enough_decided_books_and_a_low_enough_useful_share() -> None:
    most_useful_allowed = MIN_DECIDED_BOOKS * MAX_USEFUL_PERCENT // 100

    assert verdict_for(MIN_DECIDED_BOOKS, 0) == VERDICT_MEETS_RULE
    assert verdict_for(MIN_DECIDED_BOOKS, most_useful_allowed) == VERDICT_MEETS_RULE
    # One book more useful than the share allows is no longer a match, only close.
    assert verdict_for(MIN_DECIDED_BOOKS, most_useful_allowed + 1) != VERDICT_MEETS_RULE
    # One book short of the decided minimum with no useful book is close, not a match.
    assert verdict_for(MIN_DECIDED_BOOKS - 1, 0) == VERDICT_CLOSE


def test_close_needs_some_decided_books_and_a_small_useful_share() -> None:
    near_limit = NEAR_MIN_DECIDED_BOOKS * NEAR_MAX_USEFUL_PERCENT // 100

    assert verdict_for(NEAR_MIN_DECIDED_BOOKS, near_limit) == VERDICT_CLOSE
    assert verdict_for(NEAR_MIN_DECIDED_BOOKS - 1, 0) == ""
    assert verdict_for(NEAR_MIN_DECIDED_BOOKS, near_limit + 1) == ""
    assert verdict_for(MIN_DECIDED_BOOKS * 10, MIN_DECIDED_BOOKS * 10 // 3) == ""


def test_the_report_marks_active_sources_by_the_rule() -> None:
    history = [book(["Alpha"], "reject") for _ in range(MIN_DECIDED_BOOKS)]
    history += [book(["Beta"], "reject") for _ in range(NEAR_MIN_DECIDED_BOOKS)]

    report = build_source_yield_report(history, CONFIGURED)

    assert source(report, "Alpha").verdict == VERDICT_MEETS_RULE
    assert source(report, "Beta").verdict == VERDICT_CLOSE


def test_the_rule_ignores_undecided_books() -> None:
    # Many books, almost none decided: nothing is known about this source.
    history = [book(["Alpha"], "") for _ in range(MIN_DECIDED_BOOKS * 2)]

    assert source(build_source_yield_report(history, CONFIGURED), "Alpha").verdict == ""


def test_the_rule_does_not_count_already_known_as_useful() -> None:
    history = [book(["Alpha"], "already_known") for _ in range(MIN_DECIDED_BOOKS)]

    alpha = source(build_source_yield_report(history, CONFIGURED), "Alpha")

    assert alpha.useful == 0
    assert alpha.verdict == VERDICT_MEETS_RULE


def _write_config(path: Path) -> Path:
    path.write_text(json.dumps({"sources": CONFIGURED}), encoding="utf-8")
    return path


def test_the_command_prints_the_report_and_changes_no_state(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    history = [book(["Alpha"], "reject") for _ in range(MIN_DECIDED_BOOKS)] + [book(["Alpha", "Gamma"], "interested")]
    seed_state(state_dir, candidate_history=history)
    config = _write_config(tmp_path / "config.json")
    before = state_snapshot(state_dir)
    config_before = config.read_bytes()
    files_before = sorted(path.name for path in tmp_path.rglob("*"))

    code = main(["source-yield-report", "--state-dir", str(state_dir), "--config", str(config)])

    out = capsys.readouterr().out
    assert code == 0
    assert "Alpha" in out
    assert "Gamma" in out
    assert state_snapshot(state_dir) == before
    assert config.read_bytes() == config_before
    assert sorted(path.name for path in tmp_path.rglob("*")) == files_before


@pytest.mark.parametrize("missing", ["state", "config"])
def test_the_command_reports_missing_input_as_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], missing: str
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config = _write_config(tmp_path / "config.json")
    if missing == "state":
        state_dir = tmp_path / "absent"
    else:
        config = tmp_path / "absent.json"

    code = main(["source-yield-report", "--state-dir", str(state_dir), "--config", str(config)])

    assert code == 2
    assert capsys.readouterr().out == ""
