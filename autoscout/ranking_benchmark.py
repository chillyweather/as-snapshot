"""Ranking benchmark: is an ordering better than the current review order? (issue #1104)

The editorial-fit shadow report (#725) gives each ordering its own AUC and
interval. Overlapping intervals cannot say whether one ordering beats another
on the same books, so this benchmark compares each scorer with the baseline,
the order the review showed, book for book.

The observations are the shadow report's: a book counts once, on its first
shadow record, only when that record was made before the editor decided.
The split by run date and the pass rule below were fixed in #1104 before any
paired result existed; changing either starts a new evaluation.

Everything here is read-only and offline. Nothing is written.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from autoscout.editorial_fit_shadow import SCORER_COMPOSITE
from autoscout.editorial_fit_shadow import SCORER_FIT
from autoscout.editorial_fit_shadow import SCORER_ORDER
from autoscout.editorial_fit_shadow import SCORER_PRIORITY
from autoscout.editorial_fit_shadow import auc
from autoscout.editorial_fit_shadow import auc_interval
from autoscout.editorial_fit_shadow import labeled_observations
from autoscout.editorial_fit_shadow import top_fraction_hits
from autoscout.state_store import StateStore

ARTIFACT_TYPE = "ranking-benchmark"
SCHEMA_VERSION = 1

BASELINE = SCORER_ORDER
# Compared with the baseline, in report order. A new ordering (the
# reason-based order of #895) is added here and scored by ``labeled_observations``.
CANDIDATE_SCORERS = (SCORER_PRIORITY, SCORER_FIT, SCORER_COMPOSITE)

# Fixed in #1104. Runs before this date are development runs, the others are held out.
HELD_OUT_FROM = "2026-11-01"
# The practical clause of the pass rule: the gain in Recall@40% it asks for.
MINIMUM_RECALL_AT_40_GAIN = 0.10

# A gain of exactly the minimum must pass: 0.6 - 0.5 is 0.0999… in floating point.
_FLOAT_TOLERANCE = 1e-9

BOOTSTRAP_SAMPLES = 1000
BOOTSTRAP_SEED = 7

SLICE_ALL = "all"
SLICE_DEVELOPMENT = "development"
SLICE_HELD_OUT = "held_out"
SLICES = (SLICE_ALL, SLICE_DEVELOPMENT, SLICE_HELD_OUT)

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_NO_DATA = "no_data"

type Observation = Mapping[str, Any]
type Run = Sequence[Observation]


def _key(score: float | None) -> float:
    """A failed call ranks below every scored book, as in the shadow report."""
    return float("-inf") if score is None else score


def _split(run: Run, scorer: str) -> tuple[list[float | None], list[float | None]]:
    positives = [row["scores"][scorer] for row in run if row["positive"]]
    negatives = [row["scores"][scorer] for row in run if not row["positive"]]
    return positives, negatives


def within_run_auc(runs: Sequence[Run], scorer: str) -> float | None:
    """The chance a positive book outranks a rejected book of the same run.

    Only pairs from one run are compared, so this measures the order inside a
    run and not which run a book came from. Undefined when no run has both a
    positive and a rejected book.
    """
    wins = 0.0
    pairs = 0
    for run in runs:
        positives, negatives = _split(run, scorer)
        area = auc(positives, negatives)
        if area is not None:
            count = len(positives) * len(negatives)
            wins += area * count
            pairs += count
    return wins / pairs if pairs else None


def pooled_auc(runs: Sequence[Run], scorer: str) -> float | None:
    rows = [row for run in runs for row in run]
    return auc(*_split(rows, scorer))


def _recall_at_40(runs: Sequence[Run], scorer: str) -> float | None:
    rows = [(row["scores"][scorer], bool(row["positive"])) for run in runs for row in run]
    positives = sum(1 for _score, positive in rows if positive)
    if not positives:
        return None
    return top_fraction_hits(rows, 0.4)[0] / positives


def _percentile_interval(values: list[float]) -> list[float] | None:
    if not values:
        return None
    values.sort()
    count = len(values)
    return [values[int(0.025 * count)], values[min(count - 1, int(0.975 * count))]]


def paired_differences(runs: Sequence[Run], scorer: str, baseline: str = BASELINE) -> dict[str, Any]:
    """Scorer minus baseline on the same books, with seeded bootstrap intervals.

    Each sample draws books with replacement inside each run and scores both
    orderings on that one sample, so the interval is of the difference and not
    of two separate estimates. A sample where a difference is undefined is
    left out of that difference's interval.
    """
    rng = random.Random(BOOTSTRAP_SEED)
    within: list[float] = []
    pooled: list[float] = []
    for _ in range(BOOTSTRAP_SAMPLES):
        sample = [[rng.choice(run) for _ in run] for run in runs if run]
        for values, measure in ((within, within_run_auc), (pooled, pooled_auc)):
            ours, theirs = measure(sample, scorer), measure(sample, baseline)
            if ours is not None and theirs is not None:
                values.append(ours - theirs)

    def difference(measure: Any) -> float | None:
        ours, theirs = measure(runs, scorer), measure(runs, baseline)
        return None if ours is None or theirs is None else ours - theirs

    return {
        "within_run_auc_difference": difference(within_run_auc),
        "within_run_auc_difference_ci95": _percentile_interval(within),
        "pooled_auc_difference": difference(pooled_auc),
        "pooled_auc_difference_ci95": _percentile_interval(pooled),
        "recall_at_40_difference": difference(_recall_at_40),
    }


def _place(run: Run, scorer: str, row: Observation) -> float:
    """How many books of the run stand above *row*; tied books count half."""
    key = _key(row["scores"][scorer])
    keys = [_key(other["scores"][scorer]) for other in run]
    return sum(1 for other in keys if other > key) + (sum(1 for other in keys if other == key) - 1) / 2


def positives_moved_down(runs: Sequence[Run], scorer: str, baseline: str = BASELINE) -> int:
    """Positive books that stand lower inside their run under *scorer* than under *baseline*."""
    return sum(
        1 for run in runs for row in run if row["positive"] and _place(run, scorer, row) > _place(run, baseline, row)
    )


def _scorer_metrics(runs: Sequence[Run], scorer: str) -> dict[str, Any]:
    rows = [row for run in runs for row in run]
    positives, negatives = _split(rows, scorer)
    pooled = auc(positives, negatives)
    interval = auc_interval(positives, negatives)
    within = within_run_auc(runs, scorer)
    recall = _recall_at_40(runs, scorer)
    return {
        "within_run_auc": within,
        "pooled_auc": pooled,
        "pooled_auc_ci95": list(interval) if interval else None,
        "recall_at_40": recall,
    }


def verdict(
    metrics: Mapping[str, Any],
    paired: Mapping[str, Any],
    moved_down_share: float | None,
    max_moved_down_share: float | None,
) -> dict[str, str]:
    """The pass rule of #1104, and which clause decided.

    It is judged on the unrounded values; the report rounds them afterwards.
    """
    interval = paired["within_run_auc_difference_ci95"]
    difference = paired["within_run_auc_difference"]
    if difference is None or interval is None:
        return {"verdict": VERDICT_NO_DATA, "decided_by": "no run has both a positive and a rejected book"}
    if max_moved_down_share is not None and (moved_down_share or 0.0) > max_moved_down_share:
        return {"verdict": VERDICT_FAIL, "decided_by": "positives_moved_down_share_above_limit"}
    if interval[0] > 0:
        return {"verdict": VERDICT_PASS, "decided_by": "paired_lower_bound_above_zero"}
    own_interval = metrics["pooled_auc_ci95"]
    recall_gain = paired["recall_at_40_difference"]
    if (
        difference > 0
        and own_interval is not None
        and own_interval[0] > 0.5
        and recall_gain is not None
        and recall_gain >= MINIMUM_RECALL_AT_40_GAIN - _FLOAT_TOLERANCE
    ):
        return {"verdict": VERDICT_PASS, "decided_by": "practical_rule_recall_at_40"}
    return {"verdict": VERDICT_FAIL, "decided_by": "neither_clause_met"}


def _rounded(value: Any) -> Any:
    """*value* with every number rounded for the report, after the verdict was given."""
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, dict):
        return {key: _rounded(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_rounded(item) for item in value]
    return value


def _slice(runs: Sequence[Run], max_moved_down_share: float | None) -> dict[str, Any]:
    rows = [row for run in runs for row in run]
    positives = sum(1 for row in rows if row["positive"])
    scorers: dict[str, Any] = {BASELINE: _scorer_metrics(runs, BASELINE)}
    for scorer in CANDIDATE_SCORERS:
        metrics = _scorer_metrics(runs, scorer)
        paired = paired_differences(runs, scorer)
        moved = positives_moved_down(runs, scorer)
        share = moved / positives if positives else None
        scorers[scorer] = {
            **metrics,
            **paired,
            "positives_moved_down": moved,
            "positives_moved_down_share": share,
            **verdict(metrics, paired, share, max_moved_down_share),
        }
    return {
        "runs": len(runs),
        # Runs whose review was ordered by the fit score (#1105): the editor saw the score's order there.
        "runs_after_activation": sum(1 for run in runs if any(row.get("review_ordered_by_fit") for row in run)),
        "labeled_books": len(rows),
        "positives": positives,
        "rejects": len(rows) - positives,
        "scorers": _rounded(scorers),
    }


def build_benchmark(
    observations: Sequence[Observation], *, max_moved_down_share: float | None = None
) -> dict[str, Any]:
    """The benchmark report for *observations*, as ``labeled_observations`` returns them."""
    by_date: dict[str, list[Observation]] = {}
    for row in observations:
        by_date.setdefault(str(row["run_date"]), []).append(row)
    dates = sorted(by_date)
    members = {
        SLICE_ALL: dates,
        SLICE_DEVELOPMENT: [day for day in dates if day < HELD_OUT_FROM],
        SLICE_HELD_OUT: [day for day in dates if day >= HELD_OUT_FROM],
    }
    return {
        "artifact_type": ARTIFACT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "baseline": BASELINE,
        "held_out_from": HELD_OUT_FROM,
        "verdict_slice": SLICE_HELD_OUT,
        "minimum_recall_at_40_gain": MINIMUM_RECALL_AT_40_GAIN,
        "max_positives_moved_down_share": max_moved_down_share,
        "bootstrap": {"samples": BOOTSTRAP_SAMPLES, "seed": BOOTSTRAP_SEED},
        "slices": {name: _slice([by_date[day] for day in members[name]], max_moved_down_share) for name in SLICES},
    }


def run_benchmark(
    output_dir: Path,
    state_dir: Path,
    *,
    max_moved_down_share: float | None = None,
    state_store: StateStore | None = None,
) -> dict[str, Any]:
    """Build the benchmark from the recorded shadow scores and the editor's decisions."""
    observations = labeled_observations(output_dir, state_dir, state_store=state_store)
    return build_benchmark(observations, max_moved_down_share=max_moved_down_share)


_SCORER_TITLES = {
    SCORER_PRIORITY: "Current priority label",
    SCORER_ORDER: "Current review order (baseline)",
    SCORER_FIT: "fit-v2 fit only",
    SCORER_COMPOSITE: "fit-v2 composite",
}
_SLICE_TITLES = {
    SLICE_ALL: "All runs",
    SLICE_DEVELOPMENT: f"Development runs (before {HELD_OUT_FROM})",
    SLICE_HELD_OUT: f"Held-out runs ({HELD_OUT_FROM} and later)",
}


def _cell(value: Any, *, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, list):
        return f"{_cell(value[0], signed=signed)} to {_cell(value[1], signed=signed)}"
    if isinstance(value, float):
        return f"{value:+.2f}" if signed else f"{value:.2f}"
    return str(value)


def render_benchmark(report: Mapping[str, Any]) -> str:
    limit = report["max_positives_moved_down_share"]
    lines = [
        "# Ranking benchmark",
        "",
        "Each ordering is compared with the current review order on the same books. Offline and read-only.",
        "",
        f"Pass rule, fixed in #1104 and judged on the held-out runs ({report['held_out_from']} and later): "
        "the lower bound of the paired within-run AUC difference is above 0; or the within-run AUC is above "
        "the baseline's, the lower bound of the ordering's own pooled AUC interval is above 0.5, and its "
        f"Recall@40% is at least {report['minimum_recall_at_40_gain']:.2f} above the baseline's.",
        "Limit on the share of positive books moved down: " + ("none." if limit is None else f"{limit:.2f}."),
        "",
        "## Verdict",
        "",
    ]
    held_out = report["slices"][report["verdict_slice"]]["scorers"]
    for scorer in CANDIDATE_SCORERS:
        lines.append(f"- {_SCORER_TITLES[scorer]}: {held_out[scorer]['verdict']} ({held_out[scorer]['decided_by']})")
    lines.append("")
    for name in SLICES:
        data = report["slices"][name]
        lines.extend(
            [
                f"## {_SLICE_TITLES[name]}",
                "",
                f"{data['runs']} runs, {data['positives']} positive / {data['rejects']} reject."
                + (
                    f" {data['runs_after_activation']} of these runs are after activation: their review was "
                    "ordered by the fit score, so their decisions are not blind to it."
                    if data["runs_after_activation"]
                    else ""
                ),
                "",
                "| Ordering | Within-run AUC | Pooled AUC | 95% CI | Recall@40% |",
                "| --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for scorer in (BASELINE, *CANDIDATE_SCORERS):
            metrics = data["scorers"][scorer]
            lines.append(
                f"| {_SCORER_TITLES[scorer]} | {_cell(metrics['within_run_auc'])} | {_cell(metrics['pooled_auc'])} | "
                f"{_cell(metrics['pooled_auc_ci95'])} | {_cell(metrics['recall_at_40'])} |"
            )
        lines.extend(
            [
                "",
                "Against the baseline, on the same books:",
                "",
                "| Ordering | Within-run AUC diff | 95% CI | Pooled AUC diff | 95% CI | Recall@40% diff | Positives moved down |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for scorer in CANDIDATE_SCORERS:
            metrics = data["scorers"][scorer]
            lines.append(
                f"| {_SCORER_TITLES[scorer]} | {_cell(metrics['within_run_auc_difference'], signed=True)} | "
                f"{_cell(metrics['within_run_auc_difference_ci95'], signed=True)} | "
                f"{_cell(metrics['pooled_auc_difference'], signed=True)} | "
                f"{_cell(metrics['pooled_auc_difference_ci95'], signed=True)} | "
                f"{_cell(metrics['recall_at_40_difference'], signed=True)} | "
                f"{metrics['positives_moved_down']} of {data['positives']} |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
