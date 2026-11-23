from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from autoscout.search_optimization import ComparisonRunOutcome
from autoscout.search_optimization import SearchOptimizationInputs
from autoscout.search_optimization import SearchOptimizationReview
from autoscout.search_optimization_comparison import compare_search_optimization


def _inputs(run_date: str) -> SearchOptimizationInputs:
    return SearchOptimizationInputs(
        candidates=[
            {
                "title": "A Useful Book",
                "primary_authors": ["An Author"],
                "candidate_category": "history",
                "priority_label": "B",
                "signal_strength": "shortlist",
                "source_evidence": [
                    {
                        "source_name": "Awards",
                        "source_url": "https://example.test/awards",
                        "source_family": "award",
                        "signal_strength": "shortlist",
                    }
                ],
                "russian_edition_status": "not found in Russian",
                "visible_risk_status": "no visible risk",
            }
        ],
        candidate_history=[],
        run_date=run_date,
    )


def test_review_facade_constructs_writes_and_keeps_schema_bytes(tmp_path: Path) -> None:
    review = SearchOptimizationReview.construct(_inputs("2026-08-01"))
    review.write(tmp_path)

    stored = json.loads((tmp_path / "search-optimization-review.json").read_text(encoding="utf-8"))
    restored = SearchOptimizationReview.from_mapping(stored)
    assert restored.run_date == "2026-08-01"
    assert not hasattr(restored, "comparison_projection")
    assert restored.to_json() == (tmp_path / "search-optimization-review.json").read_text(encoding="utf-8")
    assert restored.to_markdown() == (tmp_path / "search-optimization-review.md").read_text(encoding="utf-8")


def test_review_facade_comparison_is_deterministic_and_private(tmp_path: Path) -> None:
    reviews = [SearchOptimizationReview.construct(_inputs(f"2026-08-0{index}")) for index in (1, 2, 3)]
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"

    first = SearchOptimizationReview.compare_three(reviews, first_dir)
    second = SearchOptimizationReview.compare_three(reviews, second_dir)

    assert first == second
    assert (first_dir / "search-optimization-comparison.json").read_bytes() == (
        second_dir / "search-optimization-comparison.json"
    ).read_bytes()
    assert not hasattr(reviews[0], "comparison_projection")
    assert not hasattr(reviews[0], "_comparison_projection")


def test_comparison_outcome_is_typed_immutable_domain_data() -> None:
    outcome = SearchOptimizationReview.construct(_inputs("2026-08-01")).comparison_outcome()

    assert isinstance(outcome, ComparisonRunOutcome)
    assert outcome.metrics.evidence_classification.observed_interested_count == 0
    assert isinstance(outcome.source_recommendations, tuple)
    assert isinstance(outcome.candidates, tuple)
    with pytest.raises(FrozenInstanceError):
        outcome.run_date = "2026-08-02"


def test_compatibility_wrapper_matches_facade_domain_comparison(tmp_path: Path) -> None:
    reviews = [SearchOptimizationReview.construct(_inputs(f"2026-08-0{index}")) for index in (1, 2, 3)]
    facade_dir = tmp_path / "facade"
    wrapper_dir = tmp_path / "wrapper"

    facade_result = SearchOptimizationReview.compare_three(reviews, facade_dir)
    wrapper_result = compare_search_optimization([review.to_mapping() for review in reviews], wrapper_dir)

    assert wrapper_result == facade_result
    assert (wrapper_dir / "search-optimization-comparison.json").read_bytes() == (
        facade_dir / "search-optimization-comparison.json"
    ).read_bytes()
    assert (wrapper_dir / "search-optimization-comparison.md").read_bytes() == (
        facade_dir / "search-optimization-comparison.md"
    ).read_bytes()


def test_review_facade_owns_three_run_compatibility_validation() -> None:
    review = SearchOptimizationReview.construct(_inputs("2026-08-02"))
    invalid = review.to_mapping().copy()
    invalid.pop("source_allocation")
    with pytest.raises(ValueError, match="source_allocation"):
        SearchOptimizationReview.from_mapping(invalid)
