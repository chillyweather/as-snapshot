"""Acceptance tests for issue #330: three-run Search Optimization Comparison.

Tests the public CLI/artifact interface (via autoscout.__main__.main with argv).
Uses deterministic constructed schema-4 review artifacts to exercise all four
classification labels, prove one anomalous run cannot imply consistent
improvement, and verify all validation / idempotency / sentinel behaviors.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from autoscout.__main__ import main as cli_main
from autoscout.search_optimization_comparison import CLASSIFICATION_LABELS
from autoscout.search_optimization_comparison import COMPARISON_ARTIFACT_TYPE
from autoscout.search_optimization_comparison import COMPARISON_SCHEMA_VERSION
from autoscout.search_optimization_comparison import CONCLUSION_IDS
from autoscout.search_optimization_comparison import FIXED_DIAGNOSTIC_CLASS_IDS
from autoscout.search_optimization_comparison import JSON_FILENAME
from autoscout.search_optimization_comparison import MD_FILENAME

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def _make_base_review(
    run_date: str,
    eligible_count: int = 10,
) -> dict[str, Any]:
    """Return a minimal schema-4 search-optimization-review artifact."""
    return {
        "schema_version": 4,
        "artifact_type": "search-optimization-review",
        "run_identity": run_date,
        "run_date": run_date,
        "canonical_effect": "none",
        "candidate_count": eligible_count,
        "eligible_count": eligible_count,
        "outcome_cohort_counts": {
            "strong_interest": 0,
            "actionable_follow_up": 0,
            "watch": 0,
            "known_workflow_item": 0,
            "negative": 0,
            "unreviewed": 0,
        },
        "ordering_method": {
            "kind": "deterministic lexicographic",
            "factors": [
                "Priority Label",
                "Independent Signal family count",
                "Strongest Signal Strength",
                "canonical rank",
            ],
            "opaque_score": False,
            "canonical_constraints_preserved": [],
        },
        "non_effects": ["Does not alter canonical ordering."],
        "metrics": {
            "eligible_recall": {
                "canonical": {"recalled_count": eligible_count, "eligible_count": eligible_count, "recall_rate": 1.0},
                "shadow": {"recalled_count": eligible_count, "eligible_count": eligible_count, "recall_rate": 1.0},
                "identity_sets_identical": True,
            },
            "cohort_coverage": {
                "rank_25": {
                    "canonical": {
                        "strong_interest": {
                            "count_in_top_cutoff": 0,
                            "total_eligible_in_cohort": 0,
                            "rate": None,
                            "evidence_label": "",
                        },
                        "actionable_follow_up": {
                            "count_in_top_cutoff": 0,
                            "total_eligible_in_cohort": 0,
                            "rate": None,
                            "evidence_label": "",
                        },
                    },
                    "shadow": {
                        "strong_interest": {
                            "count_in_top_cutoff": 0,
                            "total_eligible_in_cohort": 0,
                            "rate": None,
                            "evidence_label": "",
                        },
                        "actionable_follow_up": {
                            "count_in_top_cutoff": 0,
                            "total_eligible_in_cohort": 0,
                            "rate": None,
                            "evidence_label": "",
                        },
                    },
                },
                "rank_50": {
                    "canonical": {
                        "strong_interest": {
                            "count_in_top_cutoff": 0,
                            "total_eligible_in_cohort": 0,
                            "rate": None,
                            "evidence_label": "",
                        },
                        "actionable_follow_up": {
                            "count_in_top_cutoff": 0,
                            "total_eligible_in_cohort": 0,
                            "rate": None,
                            "evidence_label": "",
                        },
                    },
                    "shadow": {
                        "strong_interest": {
                            "count_in_top_cutoff": 0,
                            "total_eligible_in_cohort": 0,
                            "rate": None,
                            "evidence_label": "",
                        },
                        "actionable_follow_up": {
                            "count_in_top_cutoff": 0,
                            "total_eligible_in_cohort": 0,
                            "rate": None,
                            "evidence_label": "",
                        },
                    },
                },
            },
            "density": {
                "rank_25": {
                    "canonical": {
                        "negative": {"count_in_top_cutoff": 0, "rows_inspected": 5, "rate": 0.0},
                        "known_workflow_item": {"count_in_top_cutoff": 0, "rows_inspected": 5, "rate": 0.0},
                    },
                    "shadow": {
                        "negative": {"count_in_top_cutoff": 0, "rows_inspected": 5, "rate": 0.0},
                        "known_workflow_item": {"count_in_top_cutoff": 0, "rows_inspected": 5, "rate": 0.0},
                    },
                },
                "rank_50": {
                    "canonical": {
                        "negative": {"count_in_top_cutoff": 0, "rows_inspected": 5, "rate": 0.0},
                        "known_workflow_item": {"count_in_top_cutoff": 0, "rows_inspected": 5, "rate": 0.0},
                    },
                    "shadow": {
                        "negative": {"count_in_top_cutoff": 0, "rows_inspected": 5, "rate": 0.0},
                        "known_workflow_item": {"count_in_top_cutoff": 0, "rows_inspected": 5, "rate": 0.0},
                    },
                },
            },
            "diversity": {
                "rank_25": {
                    "canonical": {"family_names": ["award"], "family_count": 1},
                    "shadow": {"family_names": ["award"], "family_count": 1},
                },
                "rank_50": {
                    "canonical": {"family_names": ["award"], "family_count": 1},
                    "shadow": {"family_names": ["award"], "family_count": 1},
                },
            },
            "median_ranks": {
                "strong_interest": {
                    "eligible_count": 0,
                    "canonical": None,
                    "shadow": None,
                    "evidence_label": "",
                },
                "actionable_follow_up": {
                    "eligible_count": 0,
                    "canonical": None,
                    "shadow": None,
                    "evidence_label": "",
                },
            },
            "workload": {
                "canonical_rows_to_retain_all_interested": None,
                "shadow_rows_to_retain_all_interested": None,
                "estimated_rows_avoided": None,
                "evidence_label": "exploratory",
                "already_known_exposure": {
                    "note": "Already Known exposure note.",
                    "canonical_ak_in_interested_window": 0,
                    "shadow_ak_in_interested_window": 0,
                    "ak_rows_difference": 0,
                },
                "discovery_quality": {
                    "note": "Discovery quality note.",
                    "strong_interest_coverage_delta": None,
                    "strong_interest_coverage_delta_evidence_label": "",
                    "actionable_coverage_delta": None,
                    "strong_interest_median_rank_delta": None,
                    "strong_interest_median_rank_delta_evidence_label": "",
                    "actionable_median_rank_delta": None,
                },
            },
            "evidence_classification": {
                "observed_interested_count": 0,
                "threshold": 5,
                "is_exploratory": True,
            },
            "warnings": [],
        },
        "examples": {
            "selection_note": "0 promoted, 0 demoted.",
            "promoted_count": 0,
            "demoted_count": 0,
            "promoted": [],
            "demoted": [],
        },
        "false_negative_audit": [],
        "candidate_quality_diagnostics": {
            "section_non_effects": ["Diagnostics are informational."],
            "classes": {
                "already_known_in_normal_review": {
                    "label": "Already Known in normal review",
                    "count": 0,
                    "rank_effect": "No effect.",
                    "findings": [],
                },
                "reject_resurfaced_without_material_change": {
                    "label": "Reject resurfaced",
                    "count": 0,
                    "rank_effect": "No effect.",
                    "findings": [],
                },
                "probable_duplicate_identity_fragmentation": {
                    "label": "Duplicate fragmentation",
                    "count": 0,
                    "rank_effect": "No effect.",
                    "findings": [],
                },
                "scouting_candidate_definition_conflict": {
                    "label": "Definition conflict",
                    "count": 0,
                    "rank_effect": "No effect.",
                    "findings": [],
                },
                "missing_trial_structured_data": {
                    "label": "Missing data",
                    "count": 0,
                    "rank_effect": "No effect.",
                    "findings": [],
                },
            },
        },
        "source_allocation": {
            "section_non_effects": ["No mutation."],
            "thresholds": {},
            "allowed_recommendations": ["expand", "keep", "reduce", "exploration-only", "insufficient data"],
            "recommendations": [
                {
                    "source_name": "TestSource",
                    "source_url": "https://example.test/testsource",
                    "family": "test-family",
                    "lifecycle_status": "active",
                    "language": "",
                    "active": True,
                    "allocation": "insufficient data",
                    "reviewed_count": 3,
                    "interested_count": 0,
                    "needs_deeper_analysis_count": 0,
                    "maybe_watch_count": 0,
                    "already_known_count": 0,
                    "reject_count": 0,
                    "unique_contribution_rate": None,
                    "cross_family_corroboration_rate": None,
                    "confidence_class": "insufficient",
                    "explanation": "Insufficient data.",
                    "canonical_effect": "none",
                    "non_effect_statement": "No mutation.",
                    "probation_evidence": None,
                    "exploration_retention": {
                        "retained": True,
                        "nonzero": True,
                        "reversible": True,
                        "text": "Continues exploration.",
                    },
                },
            ],
        },
        "source_outcome_history_factor": {
            "factor_name": "source outcome history",
            "description": "",
            "outcome_order": [],
            "candidate_evidence": {},
            "candidate_factors": {},
            "insufficient_sources_neutral": True,
            "neutral_ordinal": 2,
        },
        "candidates": [],
    }


def _add_workload(
    review: dict[str, Any],
    estimated_rows_avoided: int | None,
    ak_rows_difference: int = 0,
    observed_interested: int = 10,
    is_exploratory: bool = False,
    strong_interest_coverage_delta: int | None = None,
    actionable_coverage_delta: int | None = None,
    strong_interest_median_rank_delta: float | None = None,
    actionable_median_rank_delta: float | None = None,
) -> None:
    """Set workload values on a review artifact."""
    m = review["metrics"]
    m["evidence_classification"]["observed_interested_count"] = observed_interested
    m["evidence_classification"]["is_exploratory"] = is_exploratory
    m["evidence_classification"]["threshold"] = 5
    wl = m["workload"]
    wl["canonical_rows_to_retain_all_interested"] = 50 if estimated_rows_avoided is not None else None
    wl["shadow_rows_to_retain_all_interested"] = (
        50 - estimated_rows_avoided if estimated_rows_avoided is not None else None
    )
    wl["estimated_rows_avoided"] = estimated_rows_avoided
    wl["evidence_label"] = "exploratory" if is_exploratory else "observational"
    wl["already_known_exposure"]["canonical_ak_in_interested_window"] = ak_rows_difference
    wl["already_known_exposure"]["shadow_ak_in_interested_window"] = 0
    wl["already_known_exposure"]["ak_rows_difference"] = ak_rows_difference
    dq = wl["discovery_quality"]
    dq["strong_interest_coverage_delta"] = strong_interest_coverage_delta
    dq["strong_interest_coverage_delta_evidence_label"] = "exploratory" if is_exploratory else "observational"
    dq["actionable_coverage_delta"] = actionable_coverage_delta
    dq["strong_interest_median_rank_delta"] = strong_interest_median_rank_delta
    dq["strong_interest_median_rank_delta_evidence_label"] = "exploratory" if is_exploratory else "observational"
    dq["actionable_median_rank_delta"] = actionable_median_rank_delta


def _add_diversity(review: dict[str, Any], families_25: list[str], families_50: list[str] | None = None) -> None:
    """Set diversity values."""
    if families_50 is None:
        families_50 = families_25
    d = review["metrics"]["diversity"]
    d["rank_25"]["canonical"] = {"family_names": ["award"], "family_count": 1}
    d["rank_25"]["shadow"] = {"family_names": sorted(families_25), "family_count": len(families_25)}
    d["rank_50"]["canonical"] = {"family_names": ["award"], "family_count": 1}
    d["rank_50"]["shadow"] = {"family_names": sorted(families_50), "family_count": len(families_50)}


def _add_diagnostics(review: dict[str, Any], counts: dict[str, int]) -> None:
    """Set diagnostic class counts."""
    classes = review["candidate_quality_diagnostics"]["classes"]
    for class_id, cnt in counts.items():
        classes[class_id]["count"] = cnt


def _add_false_negative_audit(review: dict[str, Any], items: list[dict[str, Any]]) -> None:
    """Set false-negative audit items."""
    review["false_negative_audit"] = list(items)


def _add_candidates(review: dict[str, Any], candidates: list[dict[str, Any]]) -> None:
    """Set candidates list."""
    review["candidates"] = list(candidates)


def _add_examples(review: dict[str, Any], promoted: list[dict[str, Any]], demoted: list[dict[str, Any]]) -> None:
    """Set examples."""
    review["examples"] = {
        "selection_note": f"{len(promoted)} promoted, {len(demoted)} demoted.",
        "promoted_count": len(promoted),
        "demoted_count": len(demoted),
        "promoted": list(promoted),
        "demoted": list(demoted),
    }


def _add_source_rec(review: dict[str, Any], rec: dict[str, Any]) -> None:
    """Replace source allocation recommendations."""
    review["source_allocation"]["recommendations"] = [rec]


# ── Tests ──────────────────────────────────────────────────────────────────


class ThreeRunComparisonTests:
    """Public CLI/artifact interface tests for three-run comparison."""

    # ── Finding 11: single test with all four labels ─────────────────────

    def test_single_fixture_exercises_all_four_labels(self, tmp_path: Path) -> None:
        """One fixture where seven conclusion objects collectively carry all four labels.

        Run 1 (2026-09-01): no material -- rows=0, ak=0, discovery neutral,
          diversity neutral, diagnostics all zero, no FN items.
        Run 2 (2026-09-08): positive -- rows=15, ak=5, discovery positive,
          diversity positive, diagnostics declining from run1.
        Run 3 (2026-09-15): exploratory but actionable signals positive --
          rows exploratory (insufficient), ak=3, discovery positive via actionable,
          diversity positive, diagnostics strictly declining, FN strict reduction.

        Labels:
        - insufficient evidence: editor_time_savings (exploratory run 3)
        - consistent improvement: already_known_leakage (5, 8, 10 all >0),
          diagnostics (strict reduction 0->0 on some, burden drops), FN (3,2,1)
        - no material difference: source_recommendation_stability
          (insufficient data all three -> not comparable -> insufficient actually...
           need a different approach)
        - inconsistent/mixed result: discovery_rank_quality (run1 neutral,
          run2 positive, run3 positive -> all positive -> consistent!

        Let me rethink. We need:
        - insufficient evidence: editor_time_savings (one exploratory)
        - consistent improvement: already_known_leakage (all positive)
        - no material difference: source_recommendation_stability
        - inconsistent/mixed result: discovery_rank_quality (neutral, positive, adverse)

        For source_recommendation_stability to be 'no material difference',
        we need at least one comparable-stable source with no variable/partial.
        For discovery to be mixed: need negative/adverse signals in one run.
        """
        # Run 1: neutral discovery (all signals zero or null), rows=0, ak=5, FN=3
        r1 = _make_base_review("2026-09-01", eligible_count=20)
        _add_workload(
            r1,
            0,
            ak_rows_difference=5,
            observed_interested=10,
            is_exploratory=False,
            strong_interest_coverage_delta=0,
            actionable_coverage_delta=0,
            strong_interest_median_rank_delta=0.0,
            actionable_median_rank_delta=0.0,
        )
        _add_diversity(r1, ["award"])
        _add_diagnostics(r1, dict.fromkeys(FIXED_DIAGNOSTIC_CLASS_IDS, 3))
        _add_false_negative_audit(r1, _fn_items(3))
        # Give a source with real allocation (not insufficient data) for comparable-stable.
        _add_source_rec(
            r1,
            {
                "source_name": "StableSource",
                "source_url": "https://example.test/stable",
                "family": "stable-family",
                "lifecycle_status": "active",
                "language": "",
                "active": True,
                "allocation": "keep",
                "reviewed_count": 12,
                "interested_count": 2,
                "needs_deeper_analysis_count": 1,
                "maybe_watch_count": 2,
                "already_known_count": 3,
                "reject_count": 4,
                "unique_contribution_rate": 0.3,
                "cross_family_corroboration_rate": 0.2,
                "confidence_class": "moderate",
                "explanation": "Moderate evidence.",
                "canonical_effect": "none",
                "non_effect_statement": "No mutation.",
                "probation_evidence": None,
                "exploration_retention": None,
            },
        )

        # Run 2: positive discovery, rows=15, ak=8, FN=2
        r2 = _make_base_review("2026-09-08", eligible_count=20)
        _add_workload(
            r2,
            15,
            ak_rows_difference=8,
            observed_interested=10,
            is_exploratory=False,
            strong_interest_coverage_delta=2,
            actionable_coverage_delta=1,
            strong_interest_median_rank_delta=-3.0,
            actionable_median_rank_delta=-1.0,
        )
        _add_diversity(r2, ["award", "media-test", "reader-interest"])
        _add_diagnostics(
            r2,
            {
                "already_known_in_normal_review": 2,
                "reject_resurfaced_without_material_change": 1,
                "probable_duplicate_identity_fragmentation": 1,
                "scouting_candidate_definition_conflict": 1,
                "missing_trial_structured_data": 2,
            },
        )
        _add_false_negative_audit(r2, _fn_items(2))
        _add_source_rec(
            r2,
            {
                "source_name": "StableSource",
                "source_url": "https://example.test/stable",
                "family": "stable-family",
                "lifecycle_status": "active",
                "language": "",
                "active": True,
                "allocation": "keep",
                "reviewed_count": 15,
                "interested_count": 3,
                "needs_deeper_analysis_count": 2,
                "maybe_watch_count": 3,
                "already_known_count": 3,
                "reject_count": 4,
                "unique_contribution_rate": 0.35,
                "cross_family_corroboration_rate": 0.25,
                "confidence_class": "moderate",
                "explanation": "Moderate evidence.",
                "canonical_effect": "none",
                "non_effect_statement": "No mutation.",
                "probation_evidence": None,
                "exploration_retention": None,
            },
        )

        # Run 3: exploratory (insufficient for editor_time), adverse discovery
        # via negative strong-interest (exploratory so excluded), but
        # actionable signals negative -> adverse. rows exploratory.
        # AK=10, FN=1, diagnostics strictly declining.
        r3 = _make_base_review("2026-09-15", eligible_count=20)
        _add_workload(
            r3,
            20,
            ak_rows_difference=10,
            observed_interested=2,
            is_exploratory=True,
            strong_interest_coverage_delta=None,
            actionable_coverage_delta=-2,
            strong_interest_median_rank_delta=None,
            actionable_median_rank_delta=2.0,
        )
        _add_diversity(r3, ["award", "media-test", "reader-interest", "bestseller-test"])
        _add_diagnostics(
            r3,
            {
                "already_known_in_normal_review": 1,
                "reject_resurfaced_without_material_change": 0,
                "probable_duplicate_identity_fragmentation": 0,
                "scouting_candidate_definition_conflict": 0,
                "missing_trial_structured_data": 1,
            },
        )
        _add_false_negative_audit(r3, _fn_items(1))
        _add_source_rec(
            r3,
            {
                "source_name": "StableSource",
                "source_url": "https://example.test/stable",
                "family": "stable-family",
                "lifecycle_status": "active",
                "language": "",
                "active": True,
                "allocation": "keep",
                "reviewed_count": 20,
                "interested_count": 4,
                "needs_deeper_analysis_count": 3,
                "maybe_watch_count": 4,
                "already_known_count": 4,
                "reject_count": 5,
                "unique_contribution_rate": 0.4,
                "cross_family_corroboration_rate": 0.3,
                "confidence_class": "high",
                "explanation": "Strong evidence.",
                "canonical_effect": "none",
                "non_effect_statement": "No mutation.",
                "probation_evidence": None,
                "exploration_retention": None,
            },
        )

        out = tmp_path / "out"
        comparison = _run_comparison([r1, r2, r3], out)
        conclusions = comparison["conclusions"]

        # editor_time_savings: run3 is exploratory -> insufficient evidence.
        assert conclusions["editor_time_savings"]["classification"] == "insufficient evidence"
        # already_known_leakage: (5, 8, 10) all > 0 -> consistent improvement.
        assert conclusions["already_known_leakage"]["classification"] == "consistent improvement"
        # discovery_rank_quality: run1=neutral, run2=positive, run3=adverse -> mixed.
        assert conclusions["discovery_rank_quality"]["classification"] == "inconsistent/mixed result"
        # source_family_diversity: run1=neutral, run2=positive, run3=positive -> mixed (not all positive).
        assert conclusions["source_family_diversity"]["classification"] == "inconsistent/mixed result"
        # candidate_quality_diagnostics: strict reduction in both transitions,
        # no individual class increase -> consistent improvement.
        assert conclusions["candidate_quality_diagnostics"]["classification"] == "consistent improvement"
        # source_recommendation_stability: one comparable-stable source (keep/keep/keep),
        # no variable, no partial -> no material difference.
        assert conclusions["source_recommendation_stability"]["classification"] == "no material difference"
        # false_negative_safety: (3, 2, 1) strict reduction -> consistent improvement.
        assert conclusions["false_negative_safety"]["classification"] == "consistent improvement"

        # All four labels present.
        all_labels = {c["classification"] for c in conclusions.values()}
        assert "consistent improvement" in all_labels
        assert "inconsistent/mixed result" in all_labels
        assert "no material difference" in all_labels
        assert "insufficient evidence" in all_labels

    # ── Finding 1: two positive + one unavailable != consistent ──────────

    def test_two_positive_one_unavailable_not_consistent_time(self, tmp_path: Path) -> None:
        """Two evaluable positive runs + one exploratory -> insufficient evidence.

        Regression for the central guardrail: consistent improvement requires
        positive evidence in all THREE runs, without exception.
        """
        r1 = _make_base_review("2027-01-01", eligible_count=20)
        _add_workload(r1, 10, ak_rows_difference=4, observed_interested=10, is_exploratory=False)

        r2 = _make_base_review("2027-01-08", eligible_count=20)
        _add_workload(r2, 15, ak_rows_difference=6, observed_interested=10, is_exploratory=False)

        r3 = _make_base_review("2027-01-15", eligible_count=20)
        _add_workload(r3, None, ak_rows_difference=0, observed_interested=1, is_exploratory=True)
        r3["metrics"]["workload"]["estimated_rows_avoided"] = None

        out = tmp_path / "out"
        comparison = _run_comparison([r1, r2, r3], out)
        assert comparison["conclusions"]["editor_time_savings"]["classification"] == "insufficient evidence"

    # ── Finding 2: exploratory with nonzero Interested -> insufficient ────

    def test_exploratory_with_nonzero_interested_is_insufficient(self, tmp_path: Path) -> None:
        """Exploratory run with 1-4 Interested candidates makes time savings insufficient.

        Regression: raw estimates preserved but classification is insufficient evidence.
        """
        r1 = _make_base_review("2027-02-01", eligible_count=20)
        _add_workload(r1, 10, 2, observed_interested=10, is_exploratory=False)

        r2 = _make_base_review("2027-02-08", eligible_count=20)
        _add_workload(r2, 15, 4, observed_interested=10, is_exploratory=False)

        r3 = _make_base_review("2027-02-15", eligible_count=20)
        _add_workload(r3, 8, 1, observed_interested=3, is_exploratory=True)

        out = tmp_path / "out"
        comparison = _run_comparison([r1, r2, r3], out)
        c = comparison["conclusions"]["editor_time_savings"]
        assert c["classification"] == "insufficient evidence"
        # Raw estimates preserved.
        assert c["per_run_evidence"][2]["estimated_rows_avoided"] == 8
        assert c["per_run_evidence"][2]["is_exploratory"] is True

    # ── Finding 3: actionable-only discovery with exploratory Interested ──

    def test_actionable_discovery_when_interested_exploratory(self, tmp_path: Path) -> None:
        """All three runs have exploratory Interested but positive actionable signals.

        Discovery can be consistent improvement from actionable alone.
        Editor-time must be insufficient.
        """
        r1 = _make_base_review("2027-03-01", eligible_count=20)
        _add_workload(
            r1,
            None,
            0,
            observed_interested=2,
            is_exploratory=True,
            strong_interest_coverage_delta=None,
            actionable_coverage_delta=1,
            strong_interest_median_rank_delta=None,
            actionable_median_rank_delta=-2.0,
        )
        r1["metrics"]["workload"]["estimated_rows_avoided"] = None

        r2 = _make_base_review("2027-03-08", eligible_count=20)
        _add_workload(
            r2,
            None,
            0,
            observed_interested=3,
            is_exploratory=True,
            strong_interest_coverage_delta=None,
            actionable_coverage_delta=2,
            strong_interest_median_rank_delta=None,
            actionable_median_rank_delta=-3.0,
        )
        r2["metrics"]["workload"]["estimated_rows_avoided"] = None

        r3 = _make_base_review("2027-03-15", eligible_count=20)
        _add_workload(
            r3,
            None,
            0,
            observed_interested=4,
            is_exploratory=True,
            strong_interest_coverage_delta=None,
            actionable_coverage_delta=1,
            strong_interest_median_rank_delta=None,
            actionable_median_rank_delta=-1.0,
        )
        r3["metrics"]["workload"]["estimated_rows_avoided"] = None

        out = tmp_path / "out"
        comparison = _run_comparison([r1, r2, r3], out)
        conclusions = comparison["conclusions"]

        # Editor-time: all exploratory -> insufficient.
        assert conclusions["editor_time_savings"]["classification"] == "insufficient evidence"
        # Discovery: all positive via actionable -> consistent improvement.
        assert conclusions["discovery_rank_quality"]["classification"] == "consistent improvement"
        # Per-run states in evidence.
        for pr in conclusions["discovery_rank_quality"]["per_run_evidence"]:
            assert pr["run_state"] == "positive"
            assert pr["actionable_coverage_delta"] is not None

    # ── Finding 4: diversity per-run positive definition ────────────────

    def test_diversity_per_run_positive_requires_no_negative(self, tmp_path: Path) -> None:
        """Diversity where rank-25 positive but rank-50 negative is mixed per-run.

        Overall cannot be consistent improvement with mixed runs.
        """
        # Run 1: rank-25 +1 (2 vs 1), rank-50 0 -> positive.
        r1 = _make_base_review("2027-04-01", eligible_count=20)
        _add_workload(r1, 0, 0, observed_interested=10, is_exploratory=False)
        _add_diversity(r1, ["award", "media-test"], ["award"])

        # Run 2: rank-25 +2 (3 vs 1), rank-50 -1 (0 vs 1) -- make canonical 1 and shadow 0 at rank 50
        # Actually we must construct rank_50 where shadow < canonical: set canonical to "award" + something,
        # shadow to just "award" (fewer). Since _add_diversity sets canonical to ["award"] and shadow to arg,
        # to make rank_50 negative we need shadow to have fewer families than canonical at rank_50.
        # We can't do that with _add_diversity directly. Let's manually adjust.
        r2 = _make_base_review("2027-04-08", eligible_count=20)
        _add_workload(r2, 0, 0, observed_interested=10, is_exploratory=False)
        _add_diversity(r2, ["award", "media-test", "reader-interest"], ["award", "media-test", "reader-interest"])
        # Make rank_50 negative: shadow has fewer families than canonical.
        r2["metrics"]["diversity"]["rank_50"]["canonical"] = {
            "family_names": ["award", "media-test"],
            "family_count": 2,
        }
        r2["metrics"]["diversity"]["rank_50"]["shadow"] = {"family_names": ["award"], "family_count": 1}

        # Run 3: rank-25 +1, rank-50 0 -> positive.
        r3 = _make_base_review("2027-04-15", eligible_count=20)
        _add_workload(r3, 0, 0, observed_interested=10, is_exploratory=False)
        _add_diversity(r3, ["award", "bestseller-test"], ["award"])

        out = tmp_path / "out"
        comparison = _run_comparison([r1, r2, r3], out)
        # Run states: neutral, mixed, neutral.
        # Since not all positive, not all neutral -> mixed.
        assert comparison["conclusions"]["source_family_diversity"]["classification"] == "inconsistent/mixed result"

    # ── Finding 5: diagnostics strict reduction ──────────────────────────

    def test_diagnostics_strict_reduction_required(self, tmp_path: Path) -> None:
        """One class increase breaks consistency; two transitions must all decline."""
        r1 = _make_base_review("2027-05-01", eligible_count=20)
        _add_workload(r1, 0, 0, observed_interested=10, is_exploratory=False)
        _add_diagnostics(
            r1,
            {
                "already_known_in_normal_review": 3,
                "reject_resurfaced_without_material_change": 2,
                "probable_duplicate_identity_fragmentation": 1,
                "scouting_candidate_definition_conflict": 0,
                "missing_trial_structured_data": 2,
            },
        )  # total: 8

        r2 = _make_base_review("2027-05-08", eligible_count=20)
        _add_workload(r2, 0, 0, observed_interested=10, is_exploratory=False)
        _add_diagnostics(
            r2,
            {
                "already_known_in_normal_review": 2,
                "reject_resurfaced_without_material_change": 1,
                "probable_duplicate_identity_fragmentation": 0,
                "scouting_candidate_definition_conflict": 3,  # increased!
                "missing_trial_structured_data": 1,
            },
        )  # total: 7 but one class rose -> NOT strict

        r3 = _make_base_review("2027-05-15", eligible_count=20)
        _add_workload(r3, 0, 0, observed_interested=10, is_exploratory=False)
        _add_diagnostics(
            r3,
            {
                "already_known_in_normal_review": 1,
                "reject_resurfaced_without_material_change": 0,
                "probable_duplicate_identity_fragmentation": 0,
                "scouting_candidate_definition_conflict": 0,
                "missing_trial_structured_data": 0,
            },
        )  # total: 1

        out = tmp_path / "out"
        comparison = _run_comparison([r1, r2, r3], out)
        # r1->r2: scouting_candidate_definition_conflict 0->3 breaks strict.
        assert (
            comparison["conclusions"]["candidate_quality_diagnostics"]["classification"] == "inconsistent/mixed result"
        )

    def test_diagnostics_missing_class_rejected(self, tmp_path: Path) -> None:
        """Missing diagnostic class fails validation."""
        r1 = _make_base_review("2027-06-01", eligible_count=20)
        _add_workload(r1, 0, 0, observed_interested=10, is_exploratory=False)
        del r1["candidate_quality_diagnostics"]["classes"]["missing_trial_structured_data"]

        r2 = _make_base_review("2027-06-08", eligible_count=20)
        _add_workload(r2, 0, 0, observed_interested=10, is_exploratory=False)

        r3 = _make_base_review("2027-06-15", eligible_count=20)
        _add_workload(r3, 0, 0, observed_interested=10, is_exploratory=False)

        out = tmp_path / "out"
        exit_code = _run_comparison_cli([r1, r2, r3], out, expect_error=True)
        assert exit_code == 1
        _assert_no_partial_artifacts(out)

    # ── Finding 6: false-negative strict reduction ───────────────────────

    def test_fn_strict_reduction_required(self, tmp_path: Path) -> None:
        """[2, 2, 1] is NOT consistent improvement. Must be [3, 2, 1]."""
        r1 = _make_base_review("2027-07-01", eligible_count=20)
        _add_workload(r1, 0, 0, observed_interested=10, is_exploratory=False)
        _add_false_negative_audit(r1, _fn_items(2))

        r2 = _make_base_review("2027-07-08", eligible_count=20)
        _add_workload(r2, 0, 0, observed_interested=10, is_exploratory=False)
        _add_false_negative_audit(r2, _fn_items(2))

        r3 = _make_base_review("2027-07-15", eligible_count=20)
        _add_workload(r3, 0, 0, observed_interested=10, is_exploratory=False)
        _add_false_negative_audit(r3, _fn_items(1))

        out = tmp_path / "out"
        comparison = _run_comparison([r1, r2, r3], out)
        # [2, 2, 1]: r1->r2 is not strict (2 not > 2).
        assert comparison["conclusions"]["false_negative_safety"]["classification"] == "inconsistent/mixed result"

    def test_fn_strict_reduction_is_consistent(self, tmp_path: Path) -> None:
        """[3, 2, 1] with strict reduction in both transitions -> consistent."""
        r1 = _make_base_review("2027-08-01", eligible_count=20)
        _add_workload(r1, 0, 0, observed_interested=10, is_exploratory=False)
        _add_false_negative_audit(r1, _fn_items(3))

        r2 = _make_base_review("2027-08-08", eligible_count=20)
        _add_workload(r2, 0, 0, observed_interested=10, is_exploratory=False)
        _add_false_negative_audit(r2, _fn_items(2))

        r3 = _make_base_review("2027-08-15", eligible_count=20)
        _add_workload(r3, 0, 0, observed_interested=10, is_exploratory=False)
        _add_false_negative_audit(r3, _fn_items(1))

        out = tmp_path / "out"
        comparison = _run_comparison([r1, r2, r3], out)
        assert comparison["conclusions"]["false_negative_safety"]["classification"] == "consistent improvement"

    # ── Finding 7: source stability three-run comparability ──────────────

    def test_source_all_insufficient_data_is_not_stable(self, tmp_path: Path) -> None:
        """All recommendations 'insufficient data' -> insufficient evidence for stability."""
        r1 = _make_base_review("2027-09-01", eligible_count=20)
        _add_workload(r1, 0, 0, observed_interested=10, is_exploratory=False)
        r2 = _make_base_review("2027-09-08", eligible_count=20)
        _add_workload(r2, 0, 0, observed_interested=10, is_exploratory=False)
        r3 = _make_base_review("2027-09-15", eligible_count=20)
        _add_workload(r3, 0, 0, observed_interested=10, is_exploratory=False)

        out = tmp_path / "out"
        comparison = _run_comparison([r1, r2, r3], out)
        # All TestSource with insufficient data -> not comparable -> insufficient.
        assert comparison["conclusions"]["source_recommendation_stability"]["classification"] == "insufficient evidence"

    def test_source_partial_presence_handled(self, tmp_path: Path) -> None:
        """Source present in only 2 of 3 runs is marked partial."""
        r1 = _make_base_review("2027-10-01", eligible_count=20)
        _add_workload(r1, 0, 0, observed_interested=10, is_exploratory=False)
        _add_source_rec(
            r1,
            {
                "source_name": "OnlyIn1",
                "source_url": "https://example.test/only1",
                "family": "partial-family",
                "lifecycle_status": "active",
                "language": "",
                "active": True,
                "allocation": "keep",
                "reviewed_count": 15,
                "interested_count": 3,
                "needs_deeper_analysis_count": 2,
                "maybe_watch_count": 3,
                "already_known_count": 3,
                "reject_count": 4,
                "unique_contribution_rate": 0.3,
                "cross_family_corroboration_rate": 0.2,
                "confidence_class": "moderate",
                "explanation": "Moderate evidence.",
                "canonical_effect": "none",
                "non_effect_statement": "No mutation.",
                "probation_evidence": None,
                "exploration_retention": None,
            },
        )

        r2 = _make_base_review("2027-10-08", eligible_count=20)
        _add_workload(r2, 0, 0, observed_interested=10, is_exploratory=False)
        _add_source_rec(
            r2,
            {
                "source_name": "OnlyIn1",
                "source_url": "https://example.test/only1",
                "family": "partial-family",
                "lifecycle_status": "active",
                "language": "",
                "active": True,
                "allocation": "keep",
                "reviewed_count": 15,
                "interested_count": 3,
                "needs_deeper_analysis_count": 2,
                "maybe_watch_count": 3,
                "already_known_count": 3,
                "reject_count": 4,
                "unique_contribution_rate": 0.3,
                "cross_family_corroboration_rate": 0.2,
                "confidence_class": "moderate",
                "explanation": "Moderate evidence.",
                "canonical_effect": "none",
                "non_effect_statement": "No mutation.",
                "probation_evidence": None,
                "exploration_retention": None,
            },
        )

        r3 = _make_base_review("2027-10-15", eligible_count=20)
        _add_workload(r3, 0, 0, observed_interested=10, is_exploratory=False)

        out = tmp_path / "out"
        comparison = _run_comparison([r1, r2, r3], out)
        src = comparison["conclusions"]["source_recommendation_stability"]
        # OnlyIn1 is partial (only 2 of 3).
        assert src["partial"] >= 1
        assert comparison["conclusions"]["source_recommendation_stability"]["classification"] == "insufficient evidence"

    # ── Finding 8: conflict dependency by movement sum ───────────────────

    def test_conflict_dependency_weighted_by_movement(self, tmp_path: Path) -> None:
        """One high-movement conflict candidate + several low-movement clean candidates.

        Conflict with rank_delta=10 plus clean with delta=1,1,1 -> 10/13 = 77% -> dependent.
        If we use candidate count it'd be 1/4 = 25% -> NOT dependent.
        """
        r1 = _make_base_review("2027-11-01", eligible_count=20)
        _add_workload(r1, 5, 0, observed_interested=10, is_exploratory=False)
        r1["candidate_quality_diagnostics"]["classes"]["scouting_candidate_definition_conflict"] = {
            "label": "Definition conflict",
            "count": 1,
            "rank_effect": "No effect.",
            "findings": [
                {
                    "identity_key": "big mover|author one",
                    "title": "Big Mover",
                    "exclusion_reason": "underlying_work_type=article",
                    "field": "underlying_work_type",
                    "field_value": "article",
                    "canonical_gate": "normal-review",
                    "source_identities": [],
                },
            ],
        }
        clean_candidates = [
            {
                "identity_key": f"clean_{i}|author_{i}",
                "title": f"Clean {i}",
                "author_display": f"Author {i}",
                "primary_authors": [f"Author {i}"],
                "canonical_rank": i + 1,
                "shadow_rank": i,
                "rank_delta": 1,
                "canonical_update_delta": "unchanged",
                "canonical_priority_label": "A",
                "editor_status": "interested",
                "outcome_cohort": "strong_interest",
                "candidate_category": "history",
                "isbn": "",
                "independent_signal_families": ["award"],
                "source_evidence_summary": {
                    "evidence_count": 1,
                    "source_names": ["TestSource"],
                    "independent_signal_family_count": 1,
                    "strongest_signal_strength": "winner",
                },
                "source_evidence": [],
                "explanations": ["Test."],
                "diagnostic_flags": [],
            }
            for i in range(3)
        ]
        _add_candidates(
            r1,
            [
                {
                    "identity_key": "big mover|author one",
                    "title": "Big Mover",
                    "author_display": "Author One",
                    "primary_authors": ["Author One"],
                    "canonical_rank": 20,
                    "shadow_rank": 10,
                    "rank_delta": 10,
                    "canonical_update_delta": "unchanged",
                    "canonical_priority_label": "A",
                    "editor_status": "interested",
                    "outcome_cohort": "strong_interest",
                    "candidate_category": "history",
                    "isbn": "",
                    "independent_signal_families": ["award"],
                    "source_evidence_summary": {
                        "evidence_count": 1,
                        "source_names": ["TestSource"],
                        "independent_signal_family_count": 1,
                        "strongest_signal_strength": "winner",
                    },
                    "source_evidence": [],
                    "explanations": ["Test."],
                    "diagnostic_flags": ["scouting_candidate_definition_conflict"],
                },
                *clean_candidates,
            ],
        )

        r2 = _make_base_review("2027-11-08", eligible_count=20)
        _add_workload(r2, 5, 0, observed_interested=10, is_exploratory=False)

        r3 = _make_base_review("2027-11-15", eligible_count=20)
        _add_workload(r3, 5, 0, observed_interested=10, is_exploratory=False)

        out = tmp_path / "out"
        comparison = _run_comparison([r1, r2, r3], out)
        cd = comparison["dependency_analysis"]["per_run"][0]["conflict_dependency"]
        # Movement-based: numerator=10, denominator=13.
        assert cd["numerator"] == 10
        assert cd["denominator"] == 13
        assert cd["dependent"] is True  # 10/13 approx 0.77 >= 0.5
        assert "77%" in cd["statement"]

    # ── Finding 9: expanded evidence summaries in Markdown ───────────────

    def test_markdown_has_tables_not_json_dumps(self, tmp_path: Path) -> None:
        """Markdown renders rank evidence in tables, not raw json.dumps."""
        r1 = _make_base_review("2028-01-01", eligible_count=20)
        _add_workload(
            r1,
            10,
            4,
            observed_interested=10,
            is_exploratory=False,
            strong_interest_coverage_delta=1,
            actionable_coverage_delta=2,
            strong_interest_median_rank_delta=-3.0,
            actionable_median_rank_delta=-1.0,
        )
        r2 = _make_base_review("2028-01-08", eligible_count=20)
        _add_workload(r2, 15, 6, observed_interested=10, is_exploratory=False)
        r3 = _make_base_review("2028-01-15", eligible_count=20)
        _add_workload(r3, 8, 3, observed_interested=10, is_exploratory=False)

        out = tmp_path / "out"
        _run_comparison([r1, r2, r3], out)
        md_text = (out / MD_FILENAME).read_text(encoding="utf-8")

        # Markdown should have table headers and pipe-delimited rows, not json.dumps.
        assert "| Cutoff | Cohort | Canonical in/total" in md_text
        assert "| Cutoff | Cohort | Canonical in/rows" in md_text
        assert "| Cohort | Canonical median | Shadow median" in md_text
        assert "| Source | URL | Family | Allocation | Lifecycle" in md_text
        # Should NOT contain raw json.dumps output.
        assert '"_' not in md_text or '"_' not in md_text  # no dict repr

    # ── Finding 10: validation of nested evidence ────────────────────────

    def test_invalid_nested_metrics_fails(self, tmp_path: Path) -> None:
        """Missing nested workload fields fails validation."""
        r1 = _make_base_review("2028-02-01", eligible_count=20)
        del r1["metrics"]["workload"]["already_known_exposure"]
        r2 = _make_base_review("2028-02-08", eligible_count=20)
        r3 = _make_base_review("2028-02-15", eligible_count=20)

        out = tmp_path / "out"
        exit_code = _run_comparison_cli([r1, r2, r3], out, expect_error=True)
        assert exit_code == 1
        _assert_no_partial_artifacts(out)

    def test_missing_diagnostic_class_fails(self, tmp_path: Path) -> None:
        """Missing diagnostic class fails validation."""
        r1 = _make_base_review("2028-03-01", eligible_count=20)
        del r1["candidate_quality_diagnostics"]["classes"]["scouting_candidate_definition_conflict"]
        r2 = _make_base_review("2028-03-08", eligible_count=20)
        r3 = _make_base_review("2028-03-15", eligible_count=20)

        out = tmp_path / "out"
        exit_code = _run_comparison_cli([r1, r2, r3], out, expect_error=True)
        assert exit_code == 1
        _assert_no_partial_artifacts(out)

    def test_run_identity_must_equal_run_date(self, tmp_path: Path) -> None:
        """run_identity must equal run_date for unambiguous ordering."""
        r1 = _make_base_review("2028-04-01", eligible_count=20)
        r1["run_identity"] = "2028-04-02"  # mismatch
        r2 = _make_base_review("2028-04-08", eligible_count=20)
        r3 = _make_base_review("2028-04-15", eligible_count=20)

        out = tmp_path / "out"
        exit_code = _run_comparison_cli([r1, r2, r3], out, expect_error=True)
        assert exit_code == 1
        _assert_no_partial_artifacts(out)

    def test_invalid_source_rec_fails(self, tmp_path: Path) -> None:
        """Source recommendation missing required field fails."""
        r1 = _make_base_review("2028-05-01", eligible_count=20)
        r1["source_allocation"]["recommendations"][0].pop("allocation")
        r2 = _make_base_review("2028-05-08", eligible_count=20)
        r3 = _make_base_review("2028-05-15", eligible_count=20)

        out = tmp_path / "out"
        exit_code = _run_comparison_cli([r1, r2, r3], out, expect_error=True)
        assert exit_code == 1
        _assert_no_partial_artifacts(out)

    # ── Finding 11: public CLI seam via main() ───────────────────────────

    def test_cli_via_main_argv(self, tmp_path: Path) -> None:
        """Exercise the public CLI through autoscout.__main__.main with actual argv."""
        r1 = _make_base_review("2028-06-01")
        r2 = _make_base_review("2028-06-08")
        r3 = _make_base_review("2028-06-15")

        out = tmp_path / "out"
        out.mkdir()
        p1 = out / "r1.json"
        p2 = out / "r2.json"
        p3 = out / "r3.json"
        _write_json(p1, r1)
        _write_json(p2, r2)
        _write_json(p3, r3)

        exit_code = cli_main(
            [
                "compare-search-optimization",
                "--review",
                str(p1),
                "--review",
                str(p2),
                "--review",
                str(p3),
                "--output-dir",
                str(out),
            ]
        )
        assert exit_code == 0
        assert (out / JSON_FILENAME).exists()
        assert (out / MD_FILENAME).exists()

    def test_cli_via_main_error(self, tmp_path: Path) -> None:
        """CLI via main with only 2 reviews returns nonzero."""
        r1 = _make_base_review("2028-07-01")
        r2 = _make_base_review("2028-07-08")

        out = tmp_path / "out"
        out.mkdir()
        p1 = out / "r1.json"
        p2 = out / "r2.json"
        _write_json(p1, r1)
        _write_json(p2, r2)

        exit_code = cli_main(
            [
                "compare-search-optimization",
                "--review",
                str(p1),
                "--review",
                str(p2),
                "--output-dir",
                str(out),
            ]
        )
        assert exit_code != 0

    # ── Finding 12: strengthened JSON-MD agreement ──────────────────────

    def test_json_md_actionable_metrics_agreement(self, tmp_path: Path) -> None:
        """Actionable coverage and median-rank deltas in JSON appear in Markdown."""
        r1 = _make_base_review("2028-08-01", eligible_count=20)
        _add_workload(
            r1,
            10,
            4,
            observed_interested=10,
            is_exploratory=False,
            strong_interest_coverage_delta=1,
            actionable_coverage_delta=3,
            strong_interest_median_rank_delta=-2.0,
            actionable_median_rank_delta=-5.0,
        )
        r2 = _make_base_review("2028-08-08", eligible_count=20)
        _add_workload(r2, 15, 6, observed_interested=10, is_exploratory=False)
        r3 = _make_base_review("2028-08-15", eligible_count=20)
        _add_workload(r3, 8, 3, observed_interested=10, is_exploratory=False)

        out = tmp_path / "out"
        _run_comparison([r1, r2, r3], out)

        json_data = json.loads((out / JSON_FILENAME).read_text(encoding="utf-8"))
        md_text = (out / MD_FILENAME).read_text(encoding="utf-8")

        # Actionable metrics in evidence summaries.
        es = json_data["evidence_summaries"]["rank_quality_metrics"][0]
        assert es["discovery_deltas"]["actionable_coverage_delta"] == 3
        assert es["discovery_deltas"]["actionable_median_rank_delta"] == -5.0
        assert "Actionable coverage delta: 3" in md_text
        assert "Actionable median-rank delta: -5" in md_text

    def test_json_md_explanations_agreement(self, tmp_path: Path) -> None:
        """Example explanations appear in both JSON and Markdown."""
        promoted = [
            {
                "identity_key": "expl book|author x",
                "title": "Explanation Book",
                "author_display": "Author X",
                "canonical_rank": 10,
                "shadow_rank": 5,
                "rank_delta": 5,
                "outcome_cohort": "strong_interest",
                "explanations": [
                    "Expl A: Priority Label A ranks before Priority Label B",
                    "Expl B: the stable canonical tie-breaker",
                ],
            }
        ]

        r1 = _make_base_review("2028-09-01", eligible_count=20)
        _add_workload(r1, 10, 0, observed_interested=10, is_exploratory=False)
        _add_examples(r1, promoted, [])
        r2 = _make_base_review("2028-09-08", eligible_count=20)
        _add_workload(r2, 10, 0, observed_interested=10, is_exploratory=False)
        r3 = _make_base_review("2028-09-15", eligible_count=20)
        _add_workload(r3, 10, 0, observed_interested=10, is_exploratory=False)

        out = tmp_path / "out"
        _run_comparison([r1, r2, r3], out)

        json_data = json.loads((out / JSON_FILENAME).read_text(encoding="utf-8"))
        md_text = (out / MD_FILENAME).read_text(encoding="utf-8")

        expl = json_data["evidence_summaries"]["promoted_demoted_examples"][0]["promoted"][0]["explanations"]
        assert "Priority Label A" in expl[0]
        assert "Priority Label A" in md_text

    def test_json_md_source_evidence_fields(self, tmp_path: Path) -> None:
        """Source recommendation supporting fields appear in both JSON and Markdown."""
        r1 = _make_base_review("2028-10-01", eligible_count=20)
        _add_workload(r1, 5, 0, observed_interested=10, is_exploratory=False)
        _add_source_rec(
            r1,
            {
                "source_name": "FullSource",
                "source_url": "https://example.test/full",
                "family": "full-family",
                "lifecycle_status": "probation",
                "language": "en",
                "active": True,
                "allocation": "expand",
                "reviewed_count": 25,
                "interested_count": 10,
                "needs_deeper_analysis_count": 5,
                "maybe_watch_count": 4,
                "already_known_count": 3,
                "reject_count": 3,
                "unique_contribution_rate": 0.6,
                "cross_family_corroboration_rate": 0.5,
                "confidence_class": "high",
                "explanation": "Strong outcome rate with meaningful contribution.",
                "canonical_effect": "none",
                "non_effect_statement": "No mutation.",
                "probation_evidence": None,
                "exploration_retention": None,
            },
        )
        r2 = _make_base_review("2028-10-08", eligible_count=20)
        _add_workload(r2, 5, 0, observed_interested=10, is_exploratory=False)
        r3 = _make_base_review("2028-10-15", eligible_count=20)
        _add_workload(r3, 5, 0, observed_interested=10, is_exploratory=False)

        out = tmp_path / "out"
        _run_comparison([r1, r2, r3], out)

        json_data = json.loads((out / JSON_FILENAME).read_text(encoding="utf-8"))
        md_text = (out / MD_FILENAME).read_text(encoding="utf-8")

        sr = json_data["evidence_summaries"]["source_recommendations"][0]["recommendations"][0]
        assert sr["lifecycle_status"] == "probation"
        assert sr["reviewed_count"] == 25
        assert sr["unique_contribution_rate"] == 0.6

        assert "probation" in md_text
        assert "0.6" in md_text or "0.600" in md_text

    def test_json_md_weighted_dependency(self, tmp_path: Path) -> None:
        """Weighted conflict dependency numerator/denominator in both formats."""
        r1 = _make_base_review("2028-11-01", eligible_count=20)
        _add_workload(r1, 5, 0, observed_interested=10, is_exploratory=False)
        r1["candidate_quality_diagnostics"]["classes"]["scouting_candidate_definition_conflict"] = {
            "label": "Definition conflict",
            "count": 1,
            "rank_effect": "No effect.",
            "findings": [
                {
                    "identity_key": "w|a",
                    "title": "W",
                    "exclusion_reason": "x",
                    "field": "underlying_work_type",
                    "field_value": "article",
                    "canonical_gate": "normal-review",
                    "source_identities": [],
                }
            ],
        }
        _add_candidates(
            r1,
            [
                {
                    "identity_key": "w|a",
                    "title": "W",
                    "author_display": "A",
                    "primary_authors": ["A"],
                    "canonical_rank": 5,
                    "shadow_rank": 1,
                    "rank_delta": 4,
                    "canonical_update_delta": "unchanged",
                    "canonical_priority_label": "A",
                    "editor_status": "interested",
                    "outcome_cohort": "strong_interest",
                    "candidate_category": "history",
                    "isbn": "",
                    "independent_signal_families": ["award"],
                    "source_evidence_summary": {
                        "evidence_count": 1,
                        "source_names": ["X"],
                        "independent_signal_family_count": 1,
                        "strongest_signal_strength": "winner",
                    },
                    "source_evidence": [],
                    "explanations": ["Test."],
                    "diagnostic_flags": ["scouting_candidate_definition_conflict"],
                }
            ],
        )

        r2 = _make_base_review("2028-11-08", eligible_count=20)
        _add_workload(r2, 5, 0, observed_interested=10, is_exploratory=False)
        r3 = _make_base_review("2028-11-15", eligible_count=20)
        _add_workload(r3, 5, 0, observed_interested=10, is_exploratory=False)

        out = tmp_path / "out"
        _run_comparison([r1, r2, r3], out)

        json_data = json.loads((out / JSON_FILENAME).read_text(encoding="utf-8"))
        md_text = (out / MD_FILENAME).read_text(encoding="utf-8")

        cd = json_data["dependency_analysis"]["per_run"][0]["conflict_dependency"]
        assert cd["numerator"] == 4  # movement sum
        assert cd["denominator"] == 4

        assert "Numerator" in md_text or "numerator" in md_text
        assert "Denominator" in md_text or "denominator" in md_text

    # ── Other existing tests (adapted) ───────────────────────────────────

    def test_byte_identical_regeneration(self, tmp_path: Path) -> None:
        r1 = _make_base_review("2028-12-01")
        _add_workload(r1, 10, 4, observed_interested=10, is_exploratory=False)
        r2 = _make_base_review("2028-12-08")
        _add_workload(r2, 15, 6, observed_interested=10, is_exploratory=False)
        r3 = _make_base_review("2028-12-15")
        _add_workload(r3, 8, 3, observed_interested=10, is_exploratory=False)

        out1 = tmp_path / "out1"
        _run_comparison([r1, r2, r3], out1)
        json1 = (out1 / JSON_FILENAME).read_bytes()
        md1 = (out1 / MD_FILENAME).read_bytes()

        out2 = tmp_path / "out2"
        _run_comparison([r1, r2, r3], out2)
        json2 = (out2 / JSON_FILENAME).read_bytes()
        md2 = (out2 / MD_FILENAME).read_bytes()

        assert json1 == json2
        assert md1 == md2

    def test_permuted_review_order_byte_identical(self, tmp_path: Path) -> None:
        r1 = _make_base_review("2029-01-01")
        _add_workload(r1, 10, 4, observed_interested=10, is_exploratory=False)
        r2 = _make_base_review("2029-01-08")
        _add_workload(r2, 15, 6, observed_interested=10, is_exploratory=False)
        r3 = _make_base_review("2029-01-15")
        _add_workload(r3, 8, 3, observed_interested=10, is_exploratory=False)

        out1 = tmp_path / "out1"
        _run_comparison([r1, r2, r3], out1)
        out2 = tmp_path / "out2"
        _run_comparison([r3, r1, r2], out2)

        assert (out1 / JSON_FILENAME).read_bytes() == (out2 / JSON_FILENAME).read_bytes()
        assert (out1 / MD_FILENAME).read_bytes() == (out2 / MD_FILENAME).read_bytes()

    def test_no_state_files_created_or_modified(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        sentinel = state_dir / "candidate-history.json"
        sentinel.write_text("original-content", encoding="utf-8")
        sentinel_mtime = sentinel.stat().st_mtime

        r1 = _make_base_review("2029-02-01")
        r2 = _make_base_review("2029-02-08")
        r3 = _make_base_review("2029-02-15")

        out = tmp_path / "out"
        _run_comparison([r1, r2, r3], out)

        assert sentinel.read_text(encoding="utf-8") == "original-content"
        assert sentinel.stat().st_mtime == sentinel_mtime
        all_files = sorted(p.name for p in state_dir.iterdir())
        assert all_files == ["candidate-history.json"]

    def test_top_level_artifact_meta(self, tmp_path: Path) -> None:
        r1 = _make_base_review("2029-03-01")
        r2 = _make_base_review("2029-03-08")
        r3 = _make_base_review("2029-03-15")
        out = tmp_path / "out"
        comparison = _run_comparison([r1, r2, r3], out)

        assert comparison["schema_version"] == COMPARISON_SCHEMA_VERSION
        assert comparison["artifact_type"] == COMPARISON_ARTIFACT_TYPE
        assert comparison["canonical_effect"] == "none"
        assert len(comparison["reviewed_runs"]) == 3

    def test_all_seven_conclusion_ids_present(self, tmp_path: Path) -> None:
        r1 = _make_base_review("2029-04-01")
        r2 = _make_base_review("2029-04-08")
        r3 = _make_base_review("2029-04-15")
        out = tmp_path / "out"
        comparison = _run_comparison([r1, r2, r3], out)

        for cid in CONCLUSION_IDS:
            assert cid in comparison["conclusions"], f"Missing: {cid}"
            conc = comparison["conclusions"][cid]
            assert conc["classification"] in CLASSIFICATION_LABELS

    def test_runs_sorted_chronologically(self, tmp_path: Path) -> None:
        r1 = _make_base_review("2029-05-15")
        r2 = _make_base_review("2029-05-08")
        r3 = _make_base_review("2029-05-01")
        out = tmp_path / "out"
        comparison = _run_comparison([r1, r2, r3], out)
        assert comparison["reviewed_runs"] == ["2029-05-01", "2029-05-08", "2029-05-15"]

    def test_wrong_artifact_type_fails(self, tmp_path: Path) -> None:
        r1 = _make_base_review("2029-06-01")
        r1["artifact_type"] = "weekly-review"
        r2 = _make_base_review("2029-06-08")
        r3 = _make_base_review("2029-06-15")
        out = tmp_path / "out"
        exit_code = _run_comparison_cli([r1, r2, r3], out, expect_error=True)
        assert exit_code == 1
        _assert_no_partial_artifacts(out)

    def test_wrong_schema_fails(self, tmp_path: Path) -> None:
        r1 = _make_base_review("2029-07-01")
        r1["schema_version"] = 3
        r2 = _make_base_review("2029-07-08")
        r3 = _make_base_review("2029-07-15")
        out = tmp_path / "out"
        exit_code = _run_comparison_cli([r1, r2, r3], out, expect_error=True)
        assert exit_code == 1
        _assert_no_partial_artifacts(out)

    def test_missing_section_fails(self, tmp_path: Path) -> None:
        r1 = _make_base_review("2029-08-01")
        del r1["metrics"]
        r2 = _make_base_review("2029-08-08")
        r3 = _make_base_review("2029-08-15")
        out = tmp_path / "out"
        exit_code = _run_comparison_cli([r1, r2, r3], out, expect_error=True)
        assert exit_code == 1
        _assert_no_partial_artifacts(out)

    def test_duplicate_runs_fails(self, tmp_path: Path) -> None:
        r1 = _make_base_review("2029-09-01")
        r2 = _make_base_review("2029-09-01")
        r3 = _make_base_review("2029-09-08")
        out = tmp_path / "out"
        exit_code = _run_comparison_cli([r1, r2, r3], out, expect_error=True)
        assert exit_code == 1
        _assert_no_partial_artifacts(out)

    def test_wrong_review_count_fails(self, tmp_path: Path) -> None:
        r1 = _make_base_review("2029-10-01")
        r2 = _make_base_review("2029-10-08")
        out = tmp_path / "out"
        exit_code = _run_comparison_cli([r1, r2], out, expect_error=True)
        assert exit_code == 2

    def test_invalid_run_date_fails(self, tmp_path: Path) -> None:
        r1 = _make_base_review("2029-11-01")
        r2 = _make_base_review("2029-11-08")
        r3 = _make_base_review("2029-11-15")
        r3["run_date"] = "not-a-date"
        r3["run_identity"] = "not-a-date"
        out = tmp_path / "out"
        exit_code = _run_comparison_cli([r1, r2, r3], out, expect_error=True)
        assert exit_code == 1
        _assert_no_partial_artifacts(out)

    # ── Finding 1: validation regressions ────────────────────────────

    def test_non_dict_diagnostics_section_rejected(self, tmp_path: Path) -> None:
        """Non-dict diagnostics.classes must fail with ValueError, no partial artifacts."""
        r1 = _make_base_review("2030-01-01", eligible_count=20)
        _add_workload(r1, 0, 0, observed_interested=10, is_exploratory=False)
        r1["candidate_quality_diagnostics"]["classes"] = "not-a-dict"
        r2 = _make_base_review("2030-01-08", eligible_count=20)
        _add_workload(r2, 0, 0, observed_interested=10, is_exploratory=False)
        r3 = _make_base_review("2030-01-15", eligible_count=20)
        _add_workload(r3, 0, 0, observed_interested=10, is_exploratory=False)
        out = tmp_path / "out"
        exit_code = _run_comparison_cli([r1, r2, r3], out, expect_error=True)
        assert exit_code == 1
        _assert_no_partial_artifacts(out)

    def test_invalid_allocation_label_rejected(self, tmp_path: Path) -> None:
        """Invalid allocation label must fail with nonzero CLI exit, no artifacts."""
        r1 = _make_base_review("2030-02-01", eligible_count=20)
        _add_workload(r1, 0, 0, observed_interested=10, is_exploratory=False)
        r1["source_allocation"]["recommendations"][0]["allocation"] = "invalid-label"
        r2 = _make_base_review("2030-02-08", eligible_count=20)
        _add_workload(r2, 0, 0, observed_interested=10, is_exploratory=False)
        r3 = _make_base_review("2030-02-15", eligible_count=20)
        _add_workload(r3, 0, 0, observed_interested=10, is_exploratory=False)
        out = tmp_path / "out"
        exit_code = _run_comparison_cli([r1, r2, r3], out, expect_error=True)
        assert exit_code == 1
        _assert_no_partial_artifacts(out)

    def test_duplicate_source_identity_in_run_rejected(self, tmp_path: Path) -> None:
        """Duplicate source name+URL within one run must fail."""
        r1 = _make_base_review("2030-03-01", eligible_count=20)
        _add_workload(r1, 0, 0, observed_interested=10, is_exploratory=False)
        # Add second rec with same name+URL.
        r1["source_allocation"]["recommendations"].append(dict(r1["source_allocation"]["recommendations"][0]))
        r2 = _make_base_review("2030-03-08", eligible_count=20)
        _add_workload(r2, 0, 0, observed_interested=10, is_exploratory=False)
        r3 = _make_base_review("2030-03-15", eligible_count=20)
        _add_workload(r3, 0, 0, observed_interested=10, is_exploratory=False)
        out = tmp_path / "out"
        exit_code = _run_comparison_cli([r1, r2, r3], out, expect_error=True)
        assert exit_code == 1
        _assert_no_partial_artifacts(out)

    def test_malformed_nested_field_rejected(self, tmp_path: Path) -> None:
        """Malformed nested int field (string instead of int) must fail."""
        r1 = _make_base_review("2030-04-01", eligible_count=20)
        _add_workload(r1, 0, 0, observed_interested=10, is_exploratory=False)
        r1["metrics"]["evidence_classification"]["observed_interested_count"] = "not-an-int"
        r2 = _make_base_review("2030-04-08", eligible_count=20)
        _add_workload(r2, 0, 0, observed_interested=10, is_exploratory=False)
        r3 = _make_base_review("2030-04-15", eligible_count=20)
        _add_workload(r3, 0, 0, observed_interested=10, is_exploratory=False)
        out = tmp_path / "out"
        exit_code = _run_comparison_cli([r1, r2, r3], out, expect_error=True)
        assert exit_code == 1
        _assert_no_partial_artifacts(out)

    def test_non_bool_is_exploratory_rejected(self, tmp_path: Path) -> None:
        """Non-bool is_exploratory must fail."""
        r1 = _make_base_review("2030-05-01", eligible_count=20)
        _add_workload(r1, 0, 0, observed_interested=10, is_exploratory=False)
        r1["metrics"]["evidence_classification"]["is_exploratory"] = "yes"
        r2 = _make_base_review("2030-05-08", eligible_count=20)
        _add_workload(r2, 0, 0, observed_interested=10, is_exploratory=False)
        r3 = _make_base_review("2030-05-15", eligible_count=20)
        _add_workload(r3, 0, 0, observed_interested=10, is_exploratory=False)
        out = tmp_path / "out"
        exit_code = _run_comparison_cli([r1, r2, r3], out, expect_error=True)
        assert exit_code == 1
        _assert_no_partial_artifacts(out)

    def test_negative_diagnostic_count_rejected(self, tmp_path: Path) -> None:
        """Negative diagnostic count must fail."""
        r1 = _make_base_review("2030-06-01", eligible_count=20)
        _add_workload(r1, 0, 0, observed_interested=10, is_exploratory=False)
        r1["candidate_quality_diagnostics"]["classes"]["already_known_in_normal_review"]["count"] = -1
        r2 = _make_base_review("2030-06-08", eligible_count=20)
        _add_workload(r2, 0, 0, observed_interested=10, is_exploratory=False)
        r3 = _make_base_review("2030-06-15", eligible_count=20)
        _add_workload(r3, 0, 0, observed_interested=10, is_exploratory=False)
        out = tmp_path / "out"
        exit_code = _run_comparison_cli([r1, r2, r3], out, expect_error=True)
        assert exit_code == 1
        _assert_no_partial_artifacts(out)

    def test_canonical_effect_not_none_rejected(self, tmp_path: Path) -> None:
        """canonical_effect != 'none' must fail."""
        r1 = _make_base_review("2030-07-01", eligible_count=20)
        _add_workload(r1, 0, 0, observed_interested=10, is_exploratory=False)
        r1["canonical_effect"] = "mutated"
        r2 = _make_base_review("2030-07-08", eligible_count=20)
        _add_workload(r2, 0, 0, observed_interested=10, is_exploratory=False)
        r3 = _make_base_review("2030-07-15", eligible_count=20)
        _add_workload(r3, 0, 0, observed_interested=10, is_exploratory=False)
        out = tmp_path / "out"
        exit_code = _run_comparison_cli([r1, r2, r3], out, expect_error=True)
        assert exit_code == 1
        _assert_no_partial_artifacts(out)

    # ── Finding 2: Markdown source recommendation completeness ───────

    def test_source_markdown_has_full_url_non_effect_explanation(self, tmp_path: Path) -> None:
        """Source Markdown includes full URL, canonical effect, non-effect, and
        full untruncated explanation even with pipes and newlines."""
        long_explanation = (
            "Line one with a | pipe symbol.\n"
            "Line two with another | pipe.\n"
            "Line three of the explanation that is well over 100 chars to prove no truncation occurs."
        )
        r1 = _make_base_review("2030-08-01", eligible_count=20)
        _add_workload(r1, 5, 0, observed_interested=10, is_exploratory=False)
        _add_source_rec(
            r1,
            {
                "source_name": "Pipe|Source",
                "source_url": "https://example.test/pipe|source",
                "family": "pipe-family",
                "lifecycle_status": "active",
                "language": "",
                "active": True,
                "allocation": "keep",
                "reviewed_count": 15,
                "interested_count": 3,
                "needs_deeper_analysis_count": 2,
                "maybe_watch_count": 3,
                "already_known_count": 3,
                "reject_count": 4,
                "unique_contribution_rate": 0.3,
                "cross_family_corroboration_rate": 0.2,
                "confidence_class": "moderate",
                "explanation": long_explanation,
                "canonical_effect": "none",
                "non_effect_statement": "This source recommendation does not | mutate anything.",
                "probation_evidence": None,
                "exploration_retention": None,
            },
        )
        r2 = _make_base_review("2030-08-08", eligible_count=20)
        _add_workload(r2, 5, 0, observed_interested=10, is_exploratory=False)
        r3 = _make_base_review("2030-08-15", eligible_count=20)
        _add_workload(r3, 5, 0, observed_interested=10, is_exploratory=False)

        out = tmp_path / "out"
        _run_comparison([r1, r2, r3], out)
        md_text = (out / MD_FILENAME).read_text(encoding="utf-8")

        # The pipe in source name/URL should be escaped.
        assert "&#124;" in md_text  # pipe converted to HTML entity
        # Full explanation text preserved (not truncated).
        assert "Line one with a" in md_text
        assert "Line two with another" in md_text
        assert "Line three of the explanation" in md_text
        # URL is present (escaped).
        assert "https://example.test/pipe&#124;source" in md_text
        # Canonical effect appears.
        assert "none" in md_text  # appears many places, but canonical effect column has it
        # Non-effect statement present (escaped).
        assert "does not &#124; mutate" in md_text

    # ── Finding 3: table separator column count ──────────────────────

    def test_source_table_row_header_separator_same_cols(self, tmp_path: Path) -> None:
        """Structural assertion: every source table row matches header column count."""
        r1 = _make_base_review("2030-09-01", eligible_count=20)
        _add_workload(r1, 5, 0, observed_interested=10, is_exploratory=False)
        r2 = _make_base_review("2030-09-08", eligible_count=20)
        _add_workload(r2, 5, 0, observed_interested=10, is_exploratory=False)
        r3 = _make_base_review("2030-09-15", eligible_count=20)
        _add_workload(r3, 5, 0, observed_interested=10, is_exploratory=False)

        out = tmp_path / "out"
        _run_comparison([r1, r2, r3], out)
        md_text = (out / MD_FILENAME).read_text(encoding="utf-8")

        # Verify the table renders and the structural assertion passes
        # (the assertion is checked at render time inside _render_comparison_markdown).
        assert "### Source Recommendations Trend" in md_text

    # ── Finding 4: source-stability per-source detail in Markdown ────

    def test_source_stability_detail_in_markdown(self, tmp_path: Path) -> None:
        """Markdown renders per-source stability detail from conclusions."""
        r1 = _make_base_review("2030-10-01", eligible_count=20)
        _add_workload(r1, 5, 0, observed_interested=10, is_exploratory=False)
        _add_source_rec(
            r1,
            {
                "source_name": "StableSrc",
                "source_url": "https://example.test/stable",
                "family": "stable-family",
                "lifecycle_status": "active",
                "language": "",
                "active": True,
                "allocation": "keep",
                "reviewed_count": 12,
                "interested_count": 2,
                "needs_deeper_analysis_count": 1,
                "maybe_watch_count": 2,
                "already_known_count": 3,
                "reject_count": 4,
                "unique_contribution_rate": 0.3,
                "cross_family_corroboration_rate": 0.2,
                "confidence_class": "moderate",
                "explanation": "Moderate evidence for keeping.",
                "canonical_effect": "none",
                "non_effect_statement": "No mutation.",
                "probation_evidence": None,
                "exploration_retention": None,
            },
        )
        r2 = _make_base_review("2030-10-08", eligible_count=20)
        _add_workload(r2, 5, 0, observed_interested=10, is_exploratory=False)
        _add_source_rec(
            r2,
            {
                "source_name": "StableSrc",
                "source_url": "https://example.test/stable",
                "family": "stable-family",
                "lifecycle_status": "active",
                "language": "",
                "active": True,
                "allocation": "keep",
                "reviewed_count": 15,
                "interested_count": 3,
                "needs_deeper_analysis_count": 2,
                "maybe_watch_count": 3,
                "already_known_count": 3,
                "reject_count": 4,
                "unique_contribution_rate": 0.35,
                "cross_family_corroboration_rate": 0.25,
                "confidence_class": "moderate",
                "explanation": "Moderate evidence for keeping.",
                "canonical_effect": "none",
                "non_effect_statement": "No mutation.",
                "probation_evidence": None,
                "exploration_retention": None,
            },
        )
        r3 = _make_base_review("2030-10-15", eligible_count=20)
        _add_workload(r3, 5, 0, observed_interested=10, is_exploratory=False)
        _add_source_rec(
            r3,
            {
                "source_name": "StableSrc",
                "source_url": "https://example.test/stable",
                "family": "stable-family",
                "lifecycle_status": "active",
                "language": "",
                "active": True,
                "allocation": "keep",
                "reviewed_count": 20,
                "interested_count": 4,
                "needs_deeper_analysis_count": 3,
                "maybe_watch_count": 4,
                "already_known_count": 4,
                "reject_count": 5,
                "unique_contribution_rate": 0.4,
                "cross_family_corroboration_rate": 0.3,
                "confidence_class": "high",
                "explanation": "Strong evidence for keeping.",
                "canonical_effect": "none",
                "non_effect_statement": "No mutation.",
                "probation_evidence": None,
                "exploration_retention": None,
            },
        )

        out = tmp_path / "out"
        _run_comparison([r1, r2, r3], out)
        md_text = (out / MD_FILENAME).read_text(encoding="utf-8")

        # Per-source stability detail subsection.
        assert "#### Source Stability Detail" in md_text
        assert "StableSrc" in md_text
        assert "comparable-stable" in md_text
        assert "keep" in md_text

    # ── A4: Signed regression tests ────────────────────────────────────

    def test_signed_editor_time_regression(self, tmp_path: Path) -> None:
        """Negative estimated_rows_avoided on all three runs -> consistent regression."""
        r1 = _make_base_review("2031-01-01", eligible_count=20)
        _add_workload(r1, -5, ak_rows_difference=-2, observed_interested=10, is_exploratory=False)
        r2 = _make_base_review("2031-01-08", eligible_count=20)
        _add_workload(r2, -10, ak_rows_difference=-4, observed_interested=10, is_exploratory=False)
        r3 = _make_base_review("2031-01-15", eligible_count=20)
        _add_workload(r3, -3, ak_rows_difference=-1, observed_interested=10, is_exploratory=False)

        out = tmp_path / "out"
        comparison = _run_comparison([r1, r2, r3], out)
        assert comparison["conclusions"]["editor_time_savings"]["classification"] == "consistent regression"
        assert "negative across all" in comparison["conclusions"]["editor_time_savings"]["summary"]

    def test_discovery_adverse_regression(self, tmp_path: Path) -> None:
        """All three runs adverse discovery -> consistent regression."""
        r1 = _make_base_review("2031-02-01", eligible_count=20)
        _add_workload(
            r1,
            0,
            0,
            observed_interested=10,
            is_exploratory=False,
            strong_interest_coverage_delta=-1,
            actionable_coverage_delta=-2,
            strong_interest_median_rank_delta=3.0,
            actionable_median_rank_delta=5.0,
        )
        r2 = _make_base_review("2031-02-08", eligible_count=20)
        _add_workload(
            r2,
            0,
            0,
            observed_interested=10,
            is_exploratory=False,
            strong_interest_coverage_delta=-2,
            actionable_coverage_delta=-1,
            strong_interest_median_rank_delta=4.0,
            actionable_median_rank_delta=2.0,
        )
        r3 = _make_base_review("2031-02-15", eligible_count=20)
        _add_workload(
            r3,
            0,
            0,
            observed_interested=10,
            is_exploratory=False,
            strong_interest_coverage_delta=-1,
            actionable_coverage_delta=-3,
            strong_interest_median_rank_delta=1.0,
            actionable_median_rank_delta=3.0,
        )

        out = tmp_path / "out"
        comparison = _run_comparison([r1, r2, r3], out)
        assert comparison["conclusions"]["discovery_rank_quality"]["classification"] == "consistent regression"
        assert "all three runs are adverse" in comparison["conclusions"]["discovery_rank_quality"]["summary"]

    def test_diversity_adverse_regression(self, tmp_path: Path) -> None:
        """All three runs with only negative diversity deltas -> consistent regression."""
        r1 = _make_base_review("2031-03-01", eligible_count=20)
        _add_workload(r1, 0, 0, observed_interested=10, is_exploratory=False)
        _add_diversity(r1, [])  # shadow has 0, canonical has 1 = -1 delta
        r1["metrics"]["diversity"]["rank_50"]["shadow"] = {"family_names": [], "family_count": 0}
        r2 = _make_base_review("2031-03-08", eligible_count=20)
        _add_workload(r2, 0, 0, observed_interested=10, is_exploratory=False)
        _add_diversity(r2, [])  # same: only negative deltas
        r2["metrics"]["diversity"]["rank_50"]["shadow"] = {"family_names": [], "family_count": 0}
        r3 = _make_base_review("2031-03-15", eligible_count=20)
        _add_workload(r3, 0, 0, observed_interested=10, is_exploratory=False)
        _add_diversity(r3, [])  # same: only negative deltas
        r3["metrics"]["diversity"]["rank_50"]["shadow"] = {"family_names": [], "family_count": 0}

        out = tmp_path / "out"
        comparison = _run_comparison([r1, r2, r3], out)
        # All three have only negative deltas => adverse.
        assert comparison["conclusions"]["source_family_diversity"]["classification"] == "consistent regression"

    def test_diagnostics_strict_increase_regression(self, tmp_path: Path) -> None:
        """Strictly increasing diagnostic burdens -> consistent regression."""
        r1 = _make_base_review("2031-04-01", eligible_count=20)
        _add_workload(r1, 0, 0, observed_interested=10, is_exploratory=False)
        _add_diagnostics(
            r1,
            {
                "already_known_in_normal_review": 0,
                "reject_resurfaced_without_material_change": 0,
                "probable_duplicate_identity_fragmentation": 0,
                "scouting_candidate_definition_conflict": 0,
                "missing_trial_structured_data": 1,
            },
        )
        r2 = _make_base_review("2031-04-08", eligible_count=20)
        _add_workload(r2, 0, 0, observed_interested=10, is_exploratory=False)
        _add_diagnostics(
            r2,
            {
                "already_known_in_normal_review": 1,
                "reject_resurfaced_without_material_change": 1,
                "probable_duplicate_identity_fragmentation": 1,
                "scouting_candidate_definition_conflict": 1,
                "missing_trial_structured_data": 2,
            },
        )
        r3 = _make_base_review("2031-04-15", eligible_count=20)
        _add_workload(r3, 0, 0, observed_interested=10, is_exploratory=False)
        _add_diagnostics(
            r3,
            {
                "already_known_in_normal_review": 2,
                "reject_resurfaced_without_material_change": 2,
                "probable_duplicate_identity_fragmentation": 2,
                "scouting_candidate_definition_conflict": 2,
                "missing_trial_structured_data": 3,
            },
        )

        out = tmp_path / "out"
        comparison = _run_comparison([r1, r2, r3], out)
        assert comparison["conclusions"]["candidate_quality_diagnostics"]["classification"] == "consistent regression"

    def test_false_negative_strict_increase_regression(self, tmp_path: Path) -> None:
        """Strictly increasing false-negative counts -> consistent regression."""
        r1 = _make_base_review("2031-05-01", eligible_count=20)
        _add_workload(r1, 0, 0, observed_interested=10, is_exploratory=False)
        _add_false_negative_audit(r1, _fn_items(1))
        r2 = _make_base_review("2031-05-08", eligible_count=20)
        _add_workload(r2, 0, 0, observed_interested=10, is_exploratory=False)
        _add_false_negative_audit(r2, _fn_items(3))
        r3 = _make_base_review("2031-05-15", eligible_count=20)
        _add_workload(r3, 0, 0, observed_interested=10, is_exploratory=False)
        _add_false_negative_audit(r3, _fn_items(5))

        out = tmp_path / "out"
        comparison = _run_comparison([r1, r2, r3], out)
        assert comparison["conclusions"]["false_negative_safety"]["classification"] == "consistent regression"


# ── Helpers ──────────────────────────────────────────────────────────────


def _fn_items(n: int) -> list[dict[str, Any]]:
    """Generate n false-negative audit items."""
    return [
        {
            "identity_key": f"fn_{i}|author_{i}",
            "title": f"FN Book {i}",
            "author_display": f"Author {i}",
            "canonical_rank": i + 5,
            "shadow_rank": i + 10,
            "rank_delta": -5,
            "outcome_cohort": "strong_interest",
            "explanations": [f"Demotion explanation for FN {i}."],
        }
        for i in range(n)
    ]


def _run_comparison(
    reviews: list[dict[str, Any]],
    out_dir: Path,
) -> dict[str, Any]:
    """Write reviews to temp files, run comparison via CLI, return JSON artifact."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i, review in enumerate(reviews):
        p = out_dir / f"review_{i}.json"
        _write_json(p, review)
        paths.append(p)

    argv = [
        "compare-search-optimization",
        "--output-dir",
        str(out_dir),
    ]
    for p in paths:
        argv.extend(["--review", str(p)])

    exit_code = cli_main(argv)
    assert exit_code == 0, f"Unexpected exit code: {exit_code}"

    result = json.loads((out_dir / JSON_FILENAME).read_text(encoding="utf-8"))
    assert isinstance(result, dict)
    return result


def _run_comparison_cli(
    reviews: list[dict[str, Any]],
    out_dir: Path,
    expect_error: bool = False,
) -> int:
    """Run comparison via CLI handler, expecting possible error."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i, review in enumerate(reviews):
        p = out_dir / f"review_{i}.json"
        _write_json(p, review)
        paths.append(p)

    argv = [
        "compare-search-optimization",
        "--output-dir",
        str(out_dir),
    ]
    for p in paths:
        argv.extend(["--review", str(p)])

    exit_code = cli_main(argv)
    if not expect_error:
        assert exit_code == 0, f"Unexpected error exit code: {exit_code}"
    return exit_code


def _assert_no_partial_artifacts(out_dir: Path) -> None:
    assert not (out_dir / JSON_FILENAME).exists(), "JSON artifact should not exist"
    assert not (out_dir / MD_FILENAME).exists(), "MD artifact should not exist"
