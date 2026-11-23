"""Ranking benchmark: a paired test of an ordering against the current review order (issue #1104)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from state_store_support import seed_state
from state_store_support import state_databases

from autoscout import editorial_fit_shadow as fit_shadow
from autoscout import jev_adapter
from autoscout import ranking_benchmark as benchmark
from autoscout.__main__ import main as cli_main
from autoscout.jev_adapter import ReplayJevAdapter

FIT = fit_shadow.SCORER_FIT
ORDER = fit_shadow.SCORER_ORDER
PRIORITY = fit_shadow.SCORER_PRIORITY
COMPOSITE = fit_shadow.SCORER_COMPOSITE


def _row(run_date: str, positive: bool, order: float, fit: float | None, priority: float = 1.0) -> dict[str, Any]:
    return {
        "run_date": run_date,
        "positive": positive,
        "scores": {ORDER: order, FIT: fit, COMPOSITE: fit, PRIORITY: priority},
    }


def _run(run_date: str, books: list[tuple[bool, float | None]]) -> list[dict[str, Any]]:
    """A run whose review listed *books* in this order; each is (positive, fit score)."""
    last = max(1, len(books) - 1)
    return [_row(run_date, positive, 1.0 - index / last, fit) for index, (positive, fit) in enumerate(books)]


class WithinRunAucTests:
    def test_only_pairs_of_one_run_are_compared(self) -> None:
        # Inside each run the positive scores above the reject. Across runs the
        # second run's reject scores above the first run's positive.
        runs = [
            [_row("2026-11-01", True, 0.0, 1.0), _row("2026-11-01", False, 1.0, 0.0)],
            [_row("2026-11-02", True, 0.0, 4.0), _row("2026-11-02", False, 1.0, 3.0)],
        ]

        assert benchmark.within_run_auc(runs, FIT) == 1.0
        assert benchmark.pooled_auc(runs, FIT) == 0.75

    def test_a_run_with_one_class_adds_no_pairs(self) -> None:
        runs = [
            [_row("2026-11-01", True, 1.0, 4.0), _row("2026-11-01", False, 0.0, 0.0)],
            [_row("2026-11-02", False, 1.0, 4.0), _row("2026-11-02", False, 0.0, 3.0)],
        ]

        assert benchmark.within_run_auc(runs, FIT) == 1.0

    def test_undefined_when_no_run_has_both_classes(self) -> None:
        assert benchmark.within_run_auc([[_row("2026-11-01", True, 1.0, 4.0)]], FIT) is None

    def test_a_failed_call_ranks_below_every_scored_book(self) -> None:
        runs = [[_row("2026-11-01", True, 1.0, None), _row("2026-11-01", False, 0.0, 0.0)]]

        assert benchmark.within_run_auc(runs, FIT) == 0.0


class PositivesMovedDownTests:
    def test_counts_positives_that_stand_lower_than_in_the_review(self) -> None:
        # The review listed the positives first and third; the fit score puts
        # the first one last and the third one first.
        run = _run("2026-11-01", [(True, 0.0), (False, 2.0), (True, 4.0)])

        assert benchmark.positives_moved_down([run], FIT) == 1

    def test_a_tie_counts_half_a_place(self) -> None:
        # Tied with one reject, the positive stands at place 0.5: lower than
        # first in the review, not lower than second.
        first = _run("2026-11-01", [(True, 2.0), (False, 2.0)])
        second = _run("2026-11-02", [(False, 2.0), (True, 2.0)])

        assert benchmark.positives_moved_down([first], FIT) == 1
        assert benchmark.positives_moved_down([second], FIT) == 0


class VerdictTests:
    def _verdict(self, **overrides: Any) -> dict[str, str]:
        values: dict[str, Any] = {
            "difference": 0.1,
            "interval": [-0.05, 0.25],
            "own_interval": [0.6, 0.8],
            "recall_gain": 0.05,
            "share": 0.2,
            "limit": None,
        }
        values.update(overrides)
        return benchmark.verdict(
            {"pooled_auc_ci95": values["own_interval"]},
            {
                "within_run_auc_difference": values["difference"],
                "within_run_auc_difference_ci95": values["interval"],
                "recall_at_40_difference": values["recall_gain"],
            },
            values["share"],
            values["limit"],
        )

    def test_passes_when_the_paired_lower_bound_is_above_zero(self) -> None:
        assert self._verdict(interval=[0.01, 0.2]) == {
            "verdict": "pass",
            "decided_by": "paired_lower_bound_above_zero",
        }

    def test_a_lower_bound_of_exactly_zero_does_not_pass_the_first_clause(self) -> None:
        assert self._verdict(interval=[0.0, 0.2])["verdict"] == "fail"

    def test_passes_on_the_practical_rule(self) -> None:
        assert self._verdict(recall_gain=0.10) == {"verdict": "pass", "decided_by": "practical_rule_recall_at_40"}

    def test_a_recall_gain_of_exactly_the_minimum_passes_despite_float_subtraction(self) -> None:
        assert 0.6 - 0.5 < 0.10
        assert self._verdict(recall_gain=0.6 - 0.5)["decided_by"] == "practical_rule_recall_at_40"

    @pytest.mark.parametrize(
        "overrides",
        [
            {"recall_gain": 0.09},
            {"recall_gain": 0.3, "own_interval": [0.5, 0.8]},
            {"recall_gain": 0.3, "difference": 0.0},
            {"recall_gain": 0.3, "own_interval": None},
        ],
    )
    def test_fails_when_one_part_of_the_practical_rule_is_missing(self, overrides: dict[str, Any]) -> None:
        assert self._verdict(**overrides) == {"verdict": "fail", "decided_by": "neither_clause_met"}

    def test_the_limit_on_positives_moved_down_fails_an_ordering_that_would_pass(self) -> None:
        assert self._verdict(interval=[0.01, 0.2], share=0.31, limit=0.3) == {
            "verdict": "fail",
            "decided_by": "positives_moved_down_share_above_limit",
        }
        assert self._verdict(interval=[0.01, 0.2], share=0.3, limit=0.3)["verdict"] == "pass"

    def test_the_limit_is_judged_on_the_unrounded_share(self) -> None:
        # 1 of 3 is 0.3333…: above a limit of 0.33332, though it rounds to 0.3333.
        rows = _run("2026-11-01", [(True, 4.0), (True, 3.0), (True, 1.0), (False, 2.0)])

        fit = benchmark.build_benchmark(rows, max_moved_down_share=0.33332)["slices"]["held_out"]["scorers"][FIT]

        assert (fit["positives_moved_down"], fit["positives_moved_down_share"]) == (1, 0.3333)
        assert fit["decided_by"] == "positives_moved_down_share_above_limit"

    def test_no_data_is_not_a_fail(self) -> None:
        assert self._verdict(difference=None, interval=None)["verdict"] == "no_data"


def _observations() -> list[dict[str, Any]]:
    """Two development runs and two held-out runs.

    The review listed every run's positives last; the fit score puts them
    first in the held-out runs and last in the development runs.
    """
    rows: list[dict[str, Any]] = []
    for run_date, fit_is_right in (
        ("2026-10-10", False),
        ("2026-10-20", False),
        ("2026-11-01", True),
        ("2026-11-05", True),
    ):
        positive_fit, reject_fit = (4.0, 1.0) if fit_is_right else (1.0, 4.0)
        books: list[tuple[bool, float | None]] = [(False, reject_fit)] * 6 + [(True, positive_fit)] * 2
        rows.extend(_run(run_date, books))
    return rows


class BenchmarkTests:
    def test_runs_are_split_at_the_fixed_date(self) -> None:
        slices = benchmark.build_benchmark(_observations())["slices"]

        assert benchmark.HELD_OUT_FROM == "2026-11-01"
        assert (slices["all"]["runs"], slices["development"]["runs"], slices["held_out"]["runs"]) == (4, 2, 2)
        assert slices["held_out"]["positives"] == 4 and slices["held_out"]["rejects"] == 12

    def test_the_verdict_is_judged_on_the_held_out_runs(self) -> None:
        report = benchmark.build_benchmark(_observations())
        held_out = report["slices"]["held_out"]["scorers"]
        development = report["slices"]["development"]["scorers"]

        assert report["verdict_slice"] == "held_out"
        assert held_out[ORDER]["within_run_auc"] == 0.0
        assert held_out[FIT]["within_run_auc"] == 1.0
        assert held_out[FIT]["within_run_auc_difference"] == 1.0
        assert held_out[FIT]["within_run_auc_difference_ci95"] == [1.0, 1.0]
        assert held_out[FIT]["positives_moved_down"] == 0
        assert held_out[FIT]["verdict"] == "pass"
        assert held_out[FIT]["decided_by"] == "paired_lower_bound_above_zero"
        # The same scorer loses on the development runs; that does not decide.
        assert development[FIT]["within_run_auc_difference"] == 0.0
        assert development[FIT]["verdict"] == "fail"
        # One priority label for every book cannot beat the review order by the first clause.
        assert held_out[PRIORITY]["within_run_auc"] == 0.5

    def test_the_same_observations_give_the_same_report(self) -> None:
        first = benchmark.build_benchmark(_observations())
        second = benchmark.build_benchmark(_observations())

        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
        assert benchmark.render_benchmark(first) == benchmark.render_benchmark(second)

    def test_the_limit_is_part_of_the_report_and_of_the_verdict(self) -> None:
        # The review listed the two positives first; the fit score lists them last.
        rows = _run("2026-11-01", [(True, 0.0), (True, 0.0), (False, 4.0), (False, 4.0)])

        unlimited = benchmark.build_benchmark(rows)["slices"]["held_out"]["scorers"][FIT]
        report = benchmark.build_benchmark(rows, max_moved_down_share=0.5)
        limited = report["slices"]["held_out"]["scorers"][FIT]

        assert report["max_positives_moved_down_share"] == 0.5
        assert (limited["positives_moved_down"], limited["positives_moved_down_share"]) == (2, 1.0)
        assert unlimited["decided_by"] == "neither_clause_met"
        assert limited["decided_by"] == "positives_moved_down_share_above_limit"

    def test_runs_after_activation_are_counted_and_named(self) -> None:
        rows = [{**row, "review_ordered_by_fit": row["run_date"] == "2026-11-05"} for row in _observations()]

        report = benchmark.build_benchmark(rows)

        slices = report["slices"]
        assert [slices[name]["runs_after_activation"] for name in ("all", "development", "held_out")] == [1, 0, 1]
        assert benchmark.render_benchmark(report).count("are after activation") == 2
        assert "after activation" not in benchmark.render_benchmark(benchmark.build_benchmark(_observations()))

    def test_no_observations_give_an_empty_report_with_no_verdict(self) -> None:
        report = benchmark.build_benchmark([])

        held_out = report["slices"]["held_out"]
        assert held_out["labeled_books"] == 0
        assert held_out["scorers"][FIT]["verdict"] == "no_data"
        assert "fit-v2 fit only: no_data" in benchmark.render_benchmark(report)

    def test_the_rendered_report_names_the_verdict_and_every_slice(self) -> None:
        text = benchmark.render_benchmark(benchmark.build_benchmark(_observations()))

        assert "- fit-v2 fit only: pass (paired_lower_bound_above_zero)" in text
        assert "## Held-out runs (2026-11-01 and later)" in text
        assert "## Development runs (before 2026-11-01)" in text
        assert "| fit-v2 fit only | +1.00 | +1.00 to +1.00 |" in text


# ── From recorded scores ──────────────────────────────────────────────────

EDITOR_NOTE = "Мне ужасно нравится, надо срочно запросить."


def _candidate(index: int) -> dict[str, Any]:
    return {
        "identity_key": f"book {index}|author {index}",
        "candidate_id": f"candidate-{index:04d}",
        "title": f"Book {index}",
        "primary_authors": [f"Author {index}"],
        "candidate_category": "adult nonfiction",
        "content_description": f"Описание книги {index}.",
        "priority_label": "C",
    }


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
                "probabilities": one_hot([label for label, _ in fit_shadow.AUDIENCE_OPTIONS], "unclear"),
                "confidence": 0.8,
            },
            "author_reach": {
                "probabilities": one_hot([label for label, _ in fit_shadow.AUTHOR_REACH_OPTIONS], "unclear"),
                "confidence": 0.7,
            },
        },
    }


def _scored_and_decided_run(tmp_path: Path) -> Path:
    """One held-out run of four books: the editor wanted the one the fit score ranked highest."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    candidates = [_candidate(index) for index in range(1, 5)]
    (run_dir / "scouting-candidates.json").write_text(json.dumps(candidates, ensure_ascii=False), encoding="utf-8")
    seed_state(run_dir, candidate_history=[])
    fits = [0, 1, 2, 4]
    adapter = ReplayJevAdapter(
        {
            jev_adapter.request_digest(fit_shadow.fit_model_state(candidate), fit_shadow.FIT_QUESTIONS): _response(fit)
            for candidate, fit in zip(candidates, fits, strict=True)
        }
    )
    fit_shadow.score_weekly_run(run_dir, run_dir, "2026-11-03", adapter)
    statuses = ["reject", "reject", "reject", "interested"]
    seed_state(
        run_dir,
        candidate_history=[
            {"identity_key": candidate["identity_key"], "editor_status": status, "editor_note": EDITOR_NOTE}
            for candidate, status in zip(candidates, statuses, strict=True)
        ],
    )
    return run_dir


def _snapshot(run_dir: Path) -> dict[str, str]:
    files = {
        str(path.relative_to(run_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(run_dir.rglob("*"))
        if path.is_file()
    }
    return {**files, **{name: checksum.hex() for name, checksum in state_databases().items()}}


class RecordedScoresTests:
    def test_the_benchmark_reads_the_books_the_shadow_report_counts(self, tmp_path: Path) -> None:
        run_dir = _scored_and_decided_run(tmp_path)

        report = benchmark.run_benchmark(run_dir, run_dir)

        held_out = report["slices"]["held_out"]
        shadow = fit_shadow.build_shadow_report(run_dir, run_dir)["cumulative"]
        assert (held_out["positives"], held_out["rejects"]) == (shadow["positives"], shadow["rejects"]) == (1, 3)
        # The review listed the wanted book last and the fit score lists it first.
        assert held_out["scorers"][ORDER]["within_run_auc"] == 0.0
        assert held_out["scorers"][FIT]["within_run_auc"] == 1.0
        assert held_out["scorers"][FIT]["pooled_auc"] == shadow["scorers"][FIT]["auc"]
        assert report["slices"]["development"]["labeled_books"] == 0

    def test_the_command_prints_the_report_and_writes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run_dir = _scored_and_decided_run(tmp_path)
        before = _snapshot(run_dir)

        code = cli_main(["ranking-benchmark", "--output-dir", str(run_dir), "--state-dir", str(run_dir)])

        output = capsys.readouterr().out
        assert code == 0
        assert "- fit-v2 fit only: pass (paired_lower_bound_above_zero)" in output
        assert EDITOR_NOTE not in output
        assert _snapshot(run_dir) == before

    def test_the_command_prints_json_on_request(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        run_dir = _scored_and_decided_run(tmp_path)

        code = cli_main(["ranking-benchmark", "--output-dir", str(run_dir), "--state-dir", str(run_dir), "--json"])

        report = json.loads(capsys.readouterr().out)
        assert code == 0
        assert report["artifact_type"] == "ranking-benchmark"
        assert report["slices"]["held_out"]["scorers"][FIT]["verdict"] == "pass"

    def test_a_share_outside_zero_to_one_is_an_input_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run_dir = _scored_and_decided_run(tmp_path)

        code = cli_main(
            [
                "ranking-benchmark",
                "--output-dir",
                str(run_dir),
                "--state-dir",
                str(run_dir),
                "--max-positives-moved-down-share",
                "1.5",
            ]
        )

        assert code == 2
        assert capsys.readouterr().out.startswith("error=")
