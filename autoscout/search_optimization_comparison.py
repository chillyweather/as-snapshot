"""Read-only three-run comparison for Search Optimization Reviews.

Deterministic plain-language comparison of exactly three preserved per-run
Search Optimization Review artifacts.  Never mutates canonical state or
invokes consequential workflows.
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from autoscout.json_io import write_json
from autoscout.review_rendering import markdown_table_cell
from autoscout.search_optimization import DIAGNOSTIC_CLASS_IDS as FIXED_DIAGNOSTIC_CLASS_IDS
from autoscout.search_optimization import EVIDENCE_THRESHOLD
from autoscout.search_optimization import SEARCH_OPTIMIZATION_SCHEMA_VERSION as SUPPORTED_REVIEW_SCHEMA
from autoscout.search_optimization import ComparisonCandidate
from autoscout.search_optimization import ComparisonCohortCoverage
from autoscout.search_optimization import ComparisonCohortWindow
from autoscout.search_optimization import ComparisonCoverage
from autoscout.search_optimization import ComparisonDensityValue
from autoscout.search_optimization import ComparisonDiversity
from autoscout.search_optimization import ComparisonRunOutcome
from autoscout.search_optimization import ComparisonSourceRecommendation
from autoscout.search_optimization import compare_search_optimization_reviews

COMPARISON_SCHEMA_VERSION = 1
COMPARISON_ARTIFACT_TYPE = "search-optimization-comparison"

CLASSIFICATION_LABELS = (
    "consistent improvement",
    "consistent regression",
    "inconsistent/mixed result",
    "no material difference",
    "insufficient evidence",
)

CONCLUSION_IDS = (
    "editor_time_savings",
    "discovery_rank_quality",
    "already_known_leakage",
    "source_family_diversity",
    "candidate_quality_diagnostics",
    "source_recommendation_stability",
    "false_negative_safety",
)

AK_DEPENDENCY_THRESHOLD = 0.5
CONFLICT_DEPENDENCY_THRESHOLD = 0.5

JSON_FILENAME = "search-optimization-comparison.json"
MD_FILENAME = "search-optimization-comparison.md"

NON_EFFECTS_STATEMENTS = [
    "No source configuration, quota, or lifecycle changes.",
    "No Editorial Taste Profile or record-feedback actions.",
    "No candidate identity or category normalization.",
    "No canonical review ordering, priority, suppression, or visibility changes.",
    "No new canonical state created or modified.",
]


def compare_search_optimization(
    reviews: Sequence[Mapping[str, Any]],
    output_dir: Path,
) -> dict[str, Any]:
    """Validate compatibility inputs through the owning domain facade."""
    return compare_search_optimization_reviews(reviews, output_dir)


def _compare_validated_reviews(
    reviews: Sequence[ComparisonRunOutcome],
    output_dir: Path,
) -> dict[str, Any]:
    """Compare immutable domain outcomes without reading review artifacts."""
    reviews_sorted = sorted(reviews, key=lambda review: review.run_date)

    run_summaries = _build_run_summaries(reviews_sorted)
    conclusions = _build_all_conclusions(reviews_sorted)
    dependency_analysis = _build_dependency_analysis(reviews_sorted)
    evidence_summaries = _build_evidence_summaries(reviews_sorted)

    comparison: dict[str, Any] = {
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "artifact_type": COMPARISON_ARTIFACT_TYPE,
        "source_artifact_type": "search-optimization-review",
        "source_schema_version": SUPPORTED_REVIEW_SCHEMA,
        "reviewed_runs": [review.run_date for review in reviews_sorted],
        "canonical_effect": "none",
        "non_effects": list(NON_EFFECTS_STATEMENTS),
        "run_summaries": run_summaries,
        "conclusions": conclusions,
        "dependency_analysis": dependency_analysis,
        "evidence_summaries": evidence_summaries,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / JSON_FILENAME, comparison)
    (output_dir / MD_FILENAME).write_text(
        _render_comparison_markdown(comparison),
        encoding="utf-8",
    )

    return comparison


# ── Run summaries ─────────────────────────────────────────────────────────


def _build_run_summaries(reviews: Sequence[ComparisonRunOutcome]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for review in reviews:
        metrics = review.metrics
        evidence = metrics.evidence_classification
        workload = metrics.workload
        discovery = workload.discovery_quality

        diag_counts = {class_id: review.diagnostics.count(class_id) for class_id in FIXED_DIAGNOSTIC_CLASS_IDS}
        allocation_counts: dict[str, int] = {}
        for recommendation in review.source_recommendations:
            allocation_counts[recommendation.allocation] = allocation_counts.get(recommendation.allocation, 0) + 1

        summaries.append(
            {
                "run_date": review.run_date,
                "run_identity": review.run_identity,
                "eligible_count": review.eligible_count,
                "candidate_count": review.candidate_count,
                "observed_interested": evidence.observed_interested_count,
                "is_exploratory": evidence.is_exploratory,
                "estimated_rows_avoided": workload.estimated_rows_avoided,
                "ak_rows_difference": workload.ak_rows_difference,
                "strong_interest_coverage_delta": discovery.strong_interest_coverage_delta,
                "actionable_coverage_delta": discovery.actionable_coverage_delta,
                "strong_interest_median_rank_delta": discovery.strong_interest_median_rank_delta,
                "actionable_median_rank_delta": discovery.actionable_median_rank_delta,
                "diagnostic_class_counts": diag_counts,
                "allocation_counts": allocation_counts,
            }
        )
    return summaries


# ── Conclusions ───────────────────────────────────────────────────────────


def _build_all_conclusions(reviews: Sequence[ComparisonRunOutcome]) -> dict[str, dict[str, Any]]:
    return {
        "editor_time_savings": _conclude_editor_time_savings(reviews),
        "discovery_rank_quality": _conclude_discovery_rank_quality(reviews),
        "already_known_leakage": _conclude_already_known_leakage(reviews),
        "source_family_diversity": _conclude_source_family_diversity(reviews),
        "candidate_quality_diagnostics": _conclude_candidate_quality_diagnostics(reviews),
        "source_recommendation_stability": _conclude_source_recommendation_stability(reviews),
        "false_negative_safety": _conclude_false_negative_safety(reviews),
    }


# ── Editor-time savings ────────────────────────────────────────────────────


def _conclude_editor_time_savings(reviews: Sequence[ComparisonRunOutcome]) -> dict[str, Any]:
    per_run: list[dict[str, Any]] = []
    warnings: list[str] = []
    any_exploratory = False
    any_unavailable = False
    all_values: list[int | None] = []

    for review in reviews:
        evidence = review.metrics.evidence_classification
        workload = review.metrics.workload
        rows_avoided = workload.estimated_rows_avoided
        is_exploratory = evidence.is_exploratory
        observed = evidence.observed_interested_count

        if is_exploratory:
            any_exploratory = True
            msg = (
                f"Run {review.run_date}: Interested evidence below threshold "
                f"({observed} observed, threshold {EVIDENCE_THRESHOLD}); "
                f"workload estimate is exploratory."
            )
            warnings.append(msg)

        value: int | None = None
        if observed == 0:
            any_unavailable = True
            value = None
        elif is_exploratory:
            any_unavailable = True
            value = None
        elif isinstance(rows_avoided, int):
            value = rows_avoided
        else:
            any_unavailable = True
            value = None

        all_values.append(value)
        per_run.append(
            {
                "run_date": review.run_date,
                "estimated_rows_avoided": rows_avoided,
                "observed_interested": observed,
                "is_exploratory": is_exploratory,
                "evaluable": value is not None,
            }
        )

    if any_unavailable or any_exploratory:
        classification = "insufficient evidence"
    else:
        classification = _classify_all_three_evaluable_int(all_values)

    return {
        "id": "editor_time_savings",
        "classification": classification,
        "summary": _time_savings_summary(classification, all_values, per_run),
        "warnings": warnings,
        "per_run_evidence": per_run,
    }


def _classify_all_three_evaluable_int(values: list[int | None]) -> str:
    vals = [v for v in values if v is not None]
    if len(vals) < 3:
        return "insufficient evidence"
    if all(v > 0 for v in vals):
        return "consistent improvement"
    if all(v == 0 for v in vals):
        return "no material difference"
    if all(v < 0 for v in vals):
        return "consistent regression"
    return "inconsistent/mixed result"


def _time_savings_summary(
    classification: str,
    values: list[int | None],
    per_run: list[dict[str, Any]],
) -> str:
    avoided = [str(p["estimated_rows_avoided"]) for p in per_run]
    if classification == "insufficient evidence":
        return (
            f"Insufficient evidence for editor-time savings: at least one run "
            f"has exploratory or unavailable Interested evidence. "
            f"Raw estimates: {', '.join(avoided)}."
        )
    if classification == "consistent improvement":
        return (
            f"Consistent improvement: estimated rows avoided across all three runs: "
            f"{', '.join(avoided)}. The shadow order consistently reduces the "
            f"number of rows the editor must inspect to retain all Interested candidates."
        )
    if classification == "no material difference":
        return (
            "No material difference: estimated rows avoided is zero across all "
            "three evaluable runs. The shadow order does not change the workload."
        )
    if classification == "consistent regression":
        return (
            f"Consistent regression: estimated rows avoided is negative across all "
            f"three evaluable runs ({', '.join(avoided)}). "
            f"The shadow order consistently requires the editor to inspect more rows."
        )
    return (
        f"Inconsistent/mixed result: estimated rows avoided varies across runs "
        f"({', '.join(avoided)}). Some runs show savings while others do not."
    )


# ── Discovery / rank quality ────────────────────────────────────────────────


def _conclude_discovery_rank_quality(reviews: Sequence[ComparisonRunOutcome]) -> dict[str, Any]:
    per_run: list[dict[str, Any]] = []
    run_states: list[str] = []

    for review in reviews:
        evidence = review.metrics.evidence_classification
        discovery = review.metrics.workload.discovery_quality
        is_exploratory = evidence.is_exploratory

        si_cov = discovery.strong_interest_coverage_delta if not is_exploratory else None
        si_mr = discovery.strong_interest_median_rank_delta if not is_exploratory else None
        ac_cov = discovery.actionable_coverage_delta
        ac_mr = discovery.actionable_median_rank_delta

        signals = {
            "strong_interest_coverage_delta": si_cov,
            "strong_interest_median_rank_delta": si_mr,
            "actionable_coverage_delta": ac_cov,
            "actionable_median_rank_delta": ac_mr,
        }

        run_state = _classify_per_run_signals(signals)
        run_states.append(run_state)

        per_run.append(
            {
                "run_date": review.run_date,
                "strong_interest_coverage_delta": si_cov,
                "strong_interest_median_rank_delta": si_mr,
                "actionable_coverage_delta": ac_cov,
                "actionable_median_rank_delta": ac_mr,
                "is_exploratory": is_exploratory,
                "run_state": run_state,
            }
        )

    if all(s == "positive" for s in run_states):
        classification = "consistent improvement"
    elif all(s == "adverse" for s in run_states):
        classification = "consistent regression"
    elif all(s == "neutral" for s in run_states):
        classification = "no material difference"
    elif sum(1 for s in run_states if s != "insufficient") < 2:
        classification = "insufficient evidence"
    else:
        classification = "inconsistent/mixed result"

    return {
        "id": "discovery_rank_quality",
        "classification": classification,
        "summary": _discovery_summary(classification, run_states, per_run),
        "per_run_evidence": per_run,
    }


def _classify_per_run_signals(signals: dict[str, float | int | None]) -> str:
    pos_cov_sigs = ("strong_interest_coverage_delta", "actionable_coverage_delta")
    neg_mr_sigs = ("strong_interest_median_rank_delta", "actionable_median_rank_delta")

    evaluable: list[tuple[str, float | int]] = []
    for name, value in signals.items():
        if value is not None:
            evaluable.append((name, value))

    if not evaluable:
        return "insufficient"

    has_positive = False
    has_adverse = False
    has_nonzero = False

    for name, value in evaluable:
        if name in pos_cov_sigs:
            if value > 0:
                has_positive = True
                has_nonzero = True
            elif value < 0:
                has_adverse = True
                has_nonzero = True
        elif name in neg_mr_sigs:
            if value < 0:
                has_positive = True
                has_nonzero = True
            elif value > 0:
                has_adverse = True
                has_nonzero = True

    if has_positive and not has_adverse:
        return "positive"
    if has_adverse and not has_positive:
        return "adverse"
    if has_positive and has_adverse:
        return "mixed"
    if not has_nonzero:
        return "neutral"
    return "mixed"


def _discovery_summary(
    classification: str,
    run_states: list[str],
    per_run: list[dict[str, Any]],
) -> str:
    states_str = ", ".join(run_states)
    if classification == "insufficient evidence":
        return f"Insufficient evidence: fewer than two evaluable runs. Per-run states: [{states_str}]."
    if classification == "consistent improvement":
        return f"Consistent improvement: all three runs are positive. Per-run states: [{states_str}]."
    if classification == "consistent regression":
        return f"Consistent regression: all three runs are adverse. Per-run states: [{states_str}]."
    if classification == "no material difference":
        return f"No material difference: all three runs are neutral. Per-run states: [{states_str}]."
    return f"Inconsistent/mixed result: per-run states [{states_str}]. Not all runs show consistent improvement."


# ── Already Known leakage ────────────────────────────────────────────────────


def _conclude_already_known_leakage(reviews: Sequence[ComparisonRunOutcome]) -> dict[str, Any]:
    values: list[int] = []
    per_run: list[dict[str, Any]] = []

    for review in reviews:
        diff = review.metrics.workload.ak_rows_difference
        values.append(diff)
        per_run.append(
            {
                "run_date": review.run_date,
                "ak_rows_difference": diff,
            }
        )

    if all(v > 0 for v in values):
        classification = "consistent improvement"
    elif all(v == 0 for v in values):
        classification = "no material difference"
    elif all(v < 0 for v in values):
        classification = "consistent regression"
    else:
        classification = "inconsistent/mixed result"

    # Build direction-aware summary.
    direction = (
        "fewer"
        if all(v >= 0 for v in values) and any(v > 0 for v in values)
        else "more"
        if all(v <= 0 for v in values) and any(v < 0 for v in values)
        else "variable"
    )
    return {
        "id": "already_known_leakage",
        "classification": classification,
        "summary": (
            f"Already Known rows interleaved in the Interested retention window: "
            f"{', '.join(str(v) for v in values)} ({direction} in the shadow order). "
            f"{classification}."
        ),
        "per_run_evidence": per_run,
    }


# ── Source-family diversity ──────────────────────────────────────────────────


def _conclude_source_family_diversity(reviews: Sequence[ComparisonRunOutcome]) -> dict[str, Any]:
    per_run: list[dict[str, Any]] = []
    run_states: list[str] = []

    for review in reviews:
        d25 = review.metrics.rank_25_diversity
        d50 = review.metrics.rank_50_diversity

        c25 = d25.canonical_family_count
        s25 = d25.shadow_family_count
        c50 = d50.canonical_family_count
        s50 = d50.shadow_family_count

        delta_25 = s25 - c25
        delta_50 = s50 - c50

        has_pos = (delta_25 > 0) or (delta_50 > 0)
        has_neg = (delta_25 < 0) or (delta_50 < 0)
        if has_pos and not has_neg:
            run_state = "positive"
        elif has_neg and not has_pos:
            run_state = "adverse"
        elif not has_pos and not has_neg:
            run_state = "neutral"
        else:
            run_state = "mixed"

        run_states.append(run_state)

        per_run.append(
            {
                "run_date": review.run_date,
                "canonical_families_rank_25": c25,
                "shadow_families_rank_25": s25,
                "delta_rank_25": delta_25,
                "canonical_families_rank_50": c50,
                "shadow_families_rank_50": s50,
                "delta_rank_50": delta_50,
                "run_state": run_state,
            }
        )

    if all(s == "positive" for s in run_states):
        classification = "consistent improvement"
    elif all(s == "adverse" for s in run_states):
        classification = "consistent regression"
    elif all(s == "neutral" for s in run_states):
        classification = "no material difference"
    else:
        classification = "inconsistent/mixed result"

    return {
        "id": "source_family_diversity",
        "classification": classification,
        "summary": (
            f"Source-family diversity delta (shadow minus canonical): "
            f"run states: {run_states}. "
            f"Positive values mean more families in the shadow top window. "
            f"{classification}."
        ),
        "per_run_evidence": per_run,
    }


# ── Candidate-quality diagnostics ───────────────────────────────────────────


def _conclude_candidate_quality_diagnostics(reviews: Sequence[ComparisonRunOutcome]) -> dict[str, Any]:
    per_run: list[dict[str, Any]] = []
    class_vectors: list[dict[str, int]] = []

    for review in reviews:
        counts = {class_id: review.diagnostics.count(class_id) for class_id in FIXED_DIAGNOSTIC_CLASS_IDS}
        class_vectors.append(counts)
        per_run.append(
            {
                "run_date": review.run_date,
                "class_counts": dict(counts),
            }
        )

    burdens = [sum(v.values()) for v in class_vectors]

    strict_reduction = True
    for idx in (0, 1):
        if burdens[idx + 1] >= burdens[idx]:
            strict_reduction = False
            break
        for class_id in FIXED_DIAGNOSTIC_CLASS_IDS:
            if class_vectors[idx + 1][class_id] > class_vectors[idx][class_id]:
                strict_reduction = False
                break

    strict_increase = True
    for idx in (0, 1):
        if burdens[idx + 1] <= burdens[idx]:
            strict_increase = False
            break
        for class_id in FIXED_DIAGNOSTIC_CLASS_IDS:
            if class_vectors[idx + 1][class_id] < class_vectors[idx][class_id]:
                strict_increase = False
                break

    if strict_reduction:
        classification = "consistent improvement"
    elif strict_increase:
        classification = "consistent regression"
    elif all(v == class_vectors[0] for v in class_vectors):
        classification = "no material difference"
    else:
        classification = "inconsistent/mixed result"

    summary_parts: list[str] = []
    for class_id in FIXED_DIAGNOSTIC_CLASS_IDS:
        trend = [v[class_id] for v in class_vectors]
        summary_parts.append(f"{class_id}: {trend}")

    return {
        "id": "candidate_quality_diagnostics",
        "classification": classification,
        "summary": (
            f"Candidate-quality diagnostic counts across runs: "
            f"total burdens {burdens}; {'; '.join(summary_parts)}. "
            f"Diagnostics are observations and never change ranking. "
            f"{classification}."
        ),
        "per_run_evidence": per_run,
        "class_trends": {k: [v[k] for v in class_vectors] for k in FIXED_DIAGNOSTIC_CLASS_IDS},
        "total_burdens": burdens,
    }


# ── Source recommendation stability ─────────────────────────────────────────


def _conclude_source_recommendation_stability(reviews: Sequence[ComparisonRunOutcome]) -> dict[str, Any]:
    all_sources: dict[str, dict[str, Any]] = {}
    run_dates = [review.run_date for review in reviews]

    for review in reviews:
        for recommendation in review.source_recommendations:
            name = recommendation.source_name
            url = recommendation.source_url
            key = f"{name}|{url}"
            if key not in all_sources:
                all_sources[key] = {
                    "source_name": name,
                    "source_url": url,
                    "family": recommendation.family,
                    "runs_present": set(),
                    "allocations_by_run": {},
                    "supporting_fields_by_run": {},
                }
            all_sources[key]["runs_present"].add(review.run_date)
            all_sources[key]["allocations_by_run"][review.run_date] = recommendation.allocation
            all_sources[key]["supporting_fields_by_run"][review.run_date] = {
                "lifecycle_status": recommendation.lifecycle_status,
                "reviewed_count": recommendation.reviewed_count,
                "interested_count": recommendation.interested_count,
                "needs_deeper_analysis_count": recommendation.needs_deeper_analysis_count,
                "maybe_watch_count": recommendation.maybe_watch_count,
                "already_known_count": recommendation.already_known_count,
                "reject_count": recommendation.reject_count,
                "unique_contribution_rate": recommendation.unique_contribution_rate,
                "cross_family_corroboration_rate": recommendation.cross_family_corroboration_rate,
                "confidence_class": recommendation.confidence_class,
                "explanation": recommendation.explanation,
                "canonical_effect": recommendation.canonical_effect,
                "non_effect_statement": recommendation.non_effect_statement,
            }

    per_source: list[dict[str, Any]] = []
    comparable_stable = 0
    comparable_variable = 0
    partial_count = 0

    for _key, info in sorted(all_sources.items(), key=lambda x: (x[1]["source_name"], x[0])):
        in_all_three = info["runs_present"] == set(run_dates)
        allocs = info["allocations_by_run"]
        unique_allocs = set(allocs.values())
        has_insufficient = any(a == "insufficient data" for a in allocs.values())
        comparable = in_all_three and not has_insufficient

        if not in_all_three:
            kind = "partial"
            partial_count += 1
        elif comparable:
            if len(unique_allocs) <= 1:
                kind = "comparable-stable"
                comparable_stable += 1
            else:
                kind = "comparable-variable"
                comparable_variable += 1
        else:
            kind = "partial"
            partial_count += 1

        per_source.append(
            {
                "source_name": info["source_name"],
                "source_url": info["source_url"],
                "family": info["family"],
                "in_all_three_runs": in_all_three,
                "allocations_by_run": dict(sorted(allocs.items())),
                "kind": kind,
                "supporting_fields_by_run": {
                    k: dict(sorted(v.items())) for k, v in sorted(info["supporting_fields_by_run"].items())
                },
            }
        )

    total = len(per_source)
    if comparable_stable + comparable_variable == 0:
        classification = "insufficient evidence"
    elif comparable_variable == 0 and partial_count == 0:
        classification = "no material difference"
    elif comparable_variable > 0:
        classification = "inconsistent/mixed result"
    else:
        classification = "inconsistent/mixed result"

    return {
        "id": "source_recommendation_stability",
        "classification": classification,
        "summary": (
            f"Source recommendation stability: {comparable_stable} comparable-stable, "
            f"{comparable_variable} comparable-variable, "
            f"{partial_count} partial out of {total} sources. "
            f"Sources with 'insufficient data' allocation are not comparable. "
            f"Recommendations are read-only diagnostics; none have been applied. "
            f"{classification}."
        ),
        "comparable_stable": comparable_stable,
        "comparable_variable": comparable_variable,
        "partial": partial_count,
        "total_sources": total,
        "per_source": per_source,
    }


# ── False-negative safety ────────────────────────────────────────────────────


def _candidate_to_mapping(candidate: ComparisonCandidate) -> dict[str, Any]:
    return {
        "identity_key": candidate.identity_key,
        "title": candidate.title,
        "author_display": candidate.author_display,
        "canonical_rank": candidate.canonical_rank,
        "shadow_rank": candidate.shadow_rank,
        "rank_delta": candidate.rank_delta,
        "outcome_cohort": candidate.outcome_cohort,
        "explanations": list(candidate.explanations),
    }


def _conclude_false_negative_safety(reviews: Sequence[ComparisonRunOutcome]) -> dict[str, Any]:
    per_run: list[dict[str, Any]] = []
    counts: list[int] = []

    for review in reviews:
        audit = review.false_negative_audit
        cnt = len(audit)
        counts.append(cnt)
        items = [_candidate_to_mapping(item) for item in audit]
        per_run.append(
            {
                "run_date": review.run_date,
                "false_negative_count": cnt,
                "items": items,
            }
        )

    if len(counts) == 3 and counts[0] > counts[1] and counts[1] > counts[2]:
        classification = "consistent improvement"
    elif len(counts) == 3 and counts[0] < counts[1] and counts[1] < counts[2]:
        classification = "consistent regression"
    elif all(c == counts[0] for c in counts):
        classification = "no material difference"
    else:
        classification = "inconsistent/mixed result"

    return {
        "id": "false_negative_safety",
        "classification": classification,
        "summary": (f"False-negative audit: valuable candidates demoted per run: {counts}. {classification}."),
        "per_run_evidence": per_run,
    }


# ── Dependency analysis ──────────────────────────────────────────────────────


def _build_dependency_analysis(reviews: Sequence[ComparisonRunOutcome]) -> dict[str, Any]:
    per_run: list[dict[str, Any]] = []
    ak_dependencies: list[bool] = []
    conflict_dependencies: list[bool] = []

    for review in reviews:
        workload = review.metrics.workload
        rows_avoided = workload.estimated_rows_avoided
        ak_diff = workload.ak_rows_difference

        if isinstance(rows_avoided, int | float) and rows_avoided is not None and rows_avoided > 0 and ak_diff > 0:
            ak_dependent = ak_diff >= rows_avoided * AK_DEPENDENCY_THRESHOLD
        else:
            ak_dependent = False

        ak_dependencies.append(ak_dependent)
        conflict_dep = _compute_conflict_dependency(review)
        conflict_dependencies.append(conflict_dep["dependent"])

        per_run.append(
            {
                "run_date": review.run_date,
                "ak_dependency": {
                    "dependent": ak_dependent,
                    "ak_rows_difference": ak_diff,
                    "estimated_rows_avoided": rows_avoided,
                    "threshold": AK_DEPENDENCY_THRESHOLD,
                    "statement": (
                        f"At least {AK_DEPENDENCY_THRESHOLD:.0%} of rows avoided attributable to "
                        f"Already Known leakage ({ak_diff} of {rows_avoided})."
                        if ak_dependent
                        else f"Already Known leakage ({ak_diff}) does not dominate rows avoided ({rows_avoided})."
                    ),
                },
                "conflict_dependency": conflict_dep,
            }
        )

    overall_ak_dependent = any(ak_dependencies)
    overall_conflict_dependent = any(conflict_dependencies)

    return {
        "per_run": per_run,
        "overall_ak_dependency": overall_ak_dependent,
        "overall_conflict_dependency": overall_conflict_dependent,
        "overall_ak_statement": (
            "At least one run has editor-time savings dominated by Already Known leakage."
            if overall_ak_dependent
            else "No run has editor-time savings dominated by Already Known leakage."
        ),
        "overall_conflict_statement": (
            "At least one run has rank improvement dominated by candidate-category inconsistencies."
            if overall_conflict_dependent
            else "No run has rank improvement dominated by candidate-category inconsistencies."
        ),
    }


def _compute_conflict_dependency(review: ComparisonRunOutcome) -> dict[str, Any]:
    conflict_ids = review.diagnostics.normal_review_conflict_identity_keys

    promoted = [candidate for candidate in review.candidates if candidate.rank_delta > 0]
    total_movement = sum(candidate.rank_delta for candidate in promoted)
    conflict_movement = 0
    conflict_promoted: list[dict[str, Any]] = []

    for candidate in promoted:
        if candidate.identity_key in conflict_ids:
            conflict_movement += candidate.rank_delta
            conflict_promoted.append(
                {
                    "identity_key": candidate.identity_key,
                    "title": candidate.title,
                    "author_display": candidate.author_display,
                    "canonical_rank": candidate.canonical_rank,
                    "shadow_rank": candidate.shadow_rank,
                    "rank_delta": candidate.rank_delta,
                }
            )

    numerator = conflict_movement
    denominator = total_movement
    dependent = denominator > 0 and (numerator / denominator) >= CONFLICT_DEPENDENCY_THRESHOLD

    if denominator > 0:
        statement = (
            f"Conflict-flagged promoted movement sum {numerator} of "
            f"total promoted movement sum {denominator} "
            f"({numerator / denominator:.0%} of promoted movement, "
            f"threshold {CONFLICT_DEPENDENCY_THRESHOLD:.0%})."
        )
    else:
        statement = "No promoted candidates; conflict dependency not applicable."

    return {
        "dependent": dependent,
        "numerator": numerator,
        "denominator": denominator,
        "threshold": CONFLICT_DEPENDENCY_THRESHOLD,
        "statement": statement,
        "conflict_candidates": conflict_promoted,
    }


# ── Evidence summaries ───────────────────────────────────────────────────────


def _build_evidence_summaries(reviews: Sequence[ComparisonRunOutcome]) -> dict[str, Any]:
    return {
        "rank_quality_metrics": _summarize_rank_metrics(reviews),
        "source_recommendations": _summarize_source_recs(reviews),
        "source_family_diversity": _summarize_diversity(reviews),
        "candidate_quality_diagnostic_counts": _summarize_diagnostic_counts(reviews),
        "promoted_demoted_examples": _summarize_examples(reviews),
        "false_negative_audit": _summarize_fn_audit(reviews),
    }


def _coverage_to_mapping(value: ComparisonCohortCoverage) -> dict[str, Any]:
    def _one(order: ComparisonCoverage) -> dict[str, Any]:
        return {
            "count_in_top_cutoff": order.count_in_top_cutoff,
            "total_eligible": order.total_eligible,
            "rate": order.rate,
            "evidence_label": order.evidence_label,
        }

    return {"canonical": _one(value.canonical), "shadow": _one(value.shadow)}


def _density_to_mapping(
    canonical: ComparisonDensityValue,
    shadow: ComparisonDensityValue,
) -> dict[str, Any]:
    def _one(order: ComparisonDensityValue) -> dict[str, Any]:
        return {
            "count_in_top_cutoff": order.count_in_top_cutoff,
            "rows_inspected": order.rows_inspected,
            "rate": order.rate,
        }

    return {"canonical": _one(canonical), "shadow": _one(shadow)}


def _window_coverage_to_mapping(window: ComparisonCohortWindow) -> dict[str, Any]:
    return {
        "strong_interest": _coverage_to_mapping(window.strong_interest),
        "actionable_follow_up": _coverage_to_mapping(window.actionable_follow_up),
    }


def _summarize_rank_metrics(reviews: Sequence[ComparisonRunOutcome]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for review in reviews:
        metrics = review.metrics
        evidence = metrics.evidence_classification
        workload = metrics.workload
        discovery = workload.discovery_quality

        result.append(
            {
                "run_date": review.run_date,
                "evidence_classification": {
                    "observed_interested_count": evidence.observed_interested_count,
                    "threshold": evidence.threshold,
                    "is_exploratory": evidence.is_exploratory,
                },
                "eligible_recall": {
                    "canonical_recalled": metrics.eligible_recall.canonical_recalled,
                    "shadow_recalled": metrics.eligible_recall.shadow_recalled,
                    "eligible_count": metrics.eligible_recall.eligible_count,
                    "identity_sets_identical": metrics.eligible_recall.identity_sets_identical,
                },
                "cohort_coverage": {
                    "rank_25": _window_coverage_to_mapping(metrics.rank_25_coverage),
                    "rank_50": _window_coverage_to_mapping(metrics.rank_50_coverage),
                },
                "density": {
                    "rank_25": {
                        "reject": _density_to_mapping(
                            metrics.rank_25_density.canonical_reject,
                            metrics.rank_25_density.shadow_reject,
                        ),
                        "already_known": _density_to_mapping(
                            metrics.rank_25_density.canonical_already_known,
                            metrics.rank_25_density.shadow_already_known,
                        ),
                    },
                    "rank_50": {
                        "reject": _density_to_mapping(
                            metrics.rank_50_density.canonical_reject,
                            metrics.rank_50_density.shadow_reject,
                        ),
                        "already_known": _density_to_mapping(
                            metrics.rank_50_density.canonical_already_known,
                            metrics.rank_50_density.shadow_already_known,
                        ),
                    },
                },
                "median_ranks": {
                    "strong_interest": {
                        "canonical": metrics.strong_interest_median_ranks.canonical,
                        "shadow": metrics.strong_interest_median_ranks.shadow,
                        "evidence_label": metrics.strong_interest_median_ranks.evidence_label,
                    },
                    "actionable_follow_up": {
                        "canonical": metrics.actionable_median_ranks.canonical,
                        "shadow": metrics.actionable_median_ranks.shadow,
                        "evidence_label": metrics.actionable_median_ranks.evidence_label,
                    },
                },
                "workload": {
                    "estimated_rows_avoided": workload.estimated_rows_avoided,
                    "evidence_label": workload.evidence_label,
                    "ak_rows_difference": workload.ak_rows_difference,
                },
                "discovery_deltas": {
                    "strong_interest_coverage_delta": discovery.strong_interest_coverage_delta,
                    "strong_interest_coverage_delta_evidence_label": (
                        discovery.strong_interest_coverage_delta_evidence_label
                    ),
                    "actionable_coverage_delta": discovery.actionable_coverage_delta,
                    "strong_interest_median_rank_delta": discovery.strong_interest_median_rank_delta,
                    "strong_interest_median_rank_delta_evidence_label": (
                        discovery.strong_interest_median_rank_delta_evidence_label
                    ),
                    "actionable_median_rank_delta": discovery.actionable_median_rank_delta,
                },
                "warnings": list(metrics.warnings),
            }
        )
    return result


def _source_recommendation_to_mapping(recommendation: ComparisonSourceRecommendation) -> dict[str, Any]:
    return {
        "source_name": recommendation.source_name,
        "source_url": recommendation.source_url,
        "family": recommendation.family,
        "allocation": recommendation.allocation,
        "lifecycle_status": recommendation.lifecycle_status,
        "reviewed_count": recommendation.reviewed_count,
        "interested_count": recommendation.interested_count,
        "needs_deeper_analysis_count": recommendation.needs_deeper_analysis_count,
        "maybe_watch_count": recommendation.maybe_watch_count,
        "already_known_count": recommendation.already_known_count,
        "reject_count": recommendation.reject_count,
        "unique_contribution_rate": recommendation.unique_contribution_rate,
        "cross_family_corroboration_rate": recommendation.cross_family_corroboration_rate,
        "confidence_class": recommendation.confidence_class,
        "explanation": recommendation.explanation,
        "canonical_effect": recommendation.canonical_effect,
        "non_effect_statement": recommendation.non_effect_statement,
    }


def _summarize_source_recs(reviews: Sequence[ComparisonRunOutcome]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for review in reviews:
        recommendations = sorted(
            review.source_recommendations,
            key=lambda recommendation: (recommendation.source_name, recommendation.source_url),
        )
        result.append(
            {
                "run_date": review.run_date,
                "recommendations": [
                    _source_recommendation_to_mapping(recommendation) for recommendation in recommendations
                ],
            }
        )
    return result


def _diversity_to_mapping(value: ComparisonDiversity) -> dict[str, Any]:
    return {
        "canonical_family_count": value.canonical_family_count,
        "shadow_family_count": value.shadow_family_count,
        "canonical_families": list(value.canonical_families),
        "shadow_families": list(value.shadow_families),
    }


def _summarize_diversity(reviews: Sequence[ComparisonRunOutcome]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for review in reviews:
        result.append(
            {
                "run_date": review.run_date,
                "rank_25": _diversity_to_mapping(review.metrics.rank_25_diversity),
                "rank_50": _diversity_to_mapping(review.metrics.rank_50_diversity),
            }
        )
    return result


def _summarize_diagnostic_counts(reviews: Sequence[ComparisonRunOutcome]) -> list[dict[str, Any]]:
    return [
        {
            "run_date": review.run_date,
            "class_counts": {class_id: review.diagnostics.count(class_id) for class_id in FIXED_DIAGNOSTIC_CLASS_IDS},
        }
        for review in reviews
    ]


def _summarize_examples(reviews: Sequence[ComparisonRunOutcome]) -> list[dict[str, Any]]:
    return [
        {
            "run_date": review.run_date,
            "promoted_count": review.examples.promoted_count,
            "demoted_count": review.examples.demoted_count,
            "promoted": [_candidate_to_mapping(candidate) for candidate in review.examples.promoted],
            "demoted": [_candidate_to_mapping(candidate) for candidate in review.examples.demoted],
        }
        for review in reviews
    ]


def _summarize_fn_audit(reviews: Sequence[ComparisonRunOutcome]) -> list[dict[str, Any]]:
    return [
        {
            "run_date": review.run_date,
            "count": len(review.false_negative_audit),
            "items": [_candidate_to_mapping(item) for item in review.false_negative_audit],
        }
        for review in reviews
    ]


# ── Markdown rendering ────────────────────────────────────────────────────


def _md_value(val: Any) -> str:
    if val is None:
        return "n/a"
    if isinstance(val, float):
        if val == int(val):
            return str(int(val))
        return f"{val:.3f}"
    return str(val)


def _render_comparison_markdown(comparison: dict[str, Any]) -> str:
    lines: list[str] = [
        "# Search Optimization Comparison",
        "",
        f"Schema version: {comparison['schema_version']}",
        f"Artifact type: {comparison['artifact_type']}",
        f"Source artifact type: {comparison['source_artifact_type']}",
        f"Source schema version: {comparison['source_schema_version']}",
        "",
        f"Reviewed runs: {', '.join(comparison['reviewed_runs'])}",
        "",
        "## Canonical Effect",
        "",
        f"**{comparison['canonical_effect']}** -- this comparison is read-only.",
        "",
    ]

    lines.append("## Non-Effects")
    lines.append("")
    for ne in comparison.get("non_effects", []):
        lines.append(f"- {ne}")

    # Run summaries.
    lines.append("")
    lines.append("## Run Summaries")
    lines.append("")
    for rs in comparison.get("run_summaries", []):
        lines.append(f"### {rs['run_date']}")
        lines.append("")
        lines.append(f"- Eligible candidates: {rs['eligible_count']}")
        lines.append(f"- Total candidates: {rs['candidate_count']}")
        lines.append(f"- Observed Interested: {rs['observed_interested']}")
        lines.append(f"- Exploratory: {rs['is_exploratory']}")
        lines.append(f"- Estimated rows avoided: {_md_value(rs['estimated_rows_avoided'])}")
        lines.append(f"- AK rows difference: {rs['ak_rows_difference']}")
        lines.append(f"- Strong-interest coverage delta: {_md_value(rs['strong_interest_coverage_delta'])}")
        lines.append(f"- Actionable coverage delta: {_md_value(rs['actionable_coverage_delta'])}")
        lines.append(f"- Strong-interest median-rank delta: {_md_value(rs['strong_interest_median_rank_delta'])}")
        lines.append(f"- Actionable median-rank delta: {_md_value(rs['actionable_median_rank_delta'])}")
        dc = rs.get("diagnostic_class_counts", {})
        if dc:
            dc_parts = [f"{k}: {v}" for k, v in sorted(dc.items())]
            lines.append(f"- Diagnostic counts: {'; '.join(dc_parts)}")
        ac = rs.get("allocation_counts", {})
        if ac:
            ac_parts = [f"{k}: {v}" for k, v in sorted(ac.items())]
            lines.append(f"- Allocation counts: {'; '.join(ac_parts)}")
        lines.append("")

    # Conclusions.
    lines.append("## Conclusions")
    lines.append("")
    for cid in CONCLUSION_IDS:
        conclusion = comparison.get("conclusions", {}).get(cid, {})
        lines.append(f"### {conclusion.get('id', cid)}")
        lines.append("")
        lines.append(f"**Classification: {conclusion.get('classification', 'unknown')}**")
        lines.append("")
        lines.append(conclusion.get("summary", ""))
        lines.append("")

        per_run = conclusion.get("per_run_evidence", [])
        if per_run:
            lines.append("#### Per-Run Evidence")
            lines.append("")
            for pr in per_run:
                lines.append(f"**{pr.get('run_date', '')}**:")
                for k, v in pr.items():
                    if k == "run_date":
                        continue
                    if k == "items":
                        continue
                    if isinstance(v, dict):
                        lines.append(f"  - {k}:")
                        for dk, dv in v.items():
                            lines.append(f"    - {dk}: {_md_value(dv)}")
                    elif isinstance(v, list):
                        lines.append(f"  - {k}: {v}")
                    else:
                        lines.append(f"  - {k}: {_md_value(v)}")
                lines.append("")

        # Source stability per-source rendering in conclusion.
        if cid == "source_recommendation_stability":
            per_source = conclusion.get("per_source", [])
            if per_source:
                lines.append("#### Source Stability Detail")
                lines.append("")
                for src in per_source:
                    alloc_str = ", ".join(
                        f"{rd}: {src['allocations_by_run'].get(rd, 'n/a')}" for rd in comparison["reviewed_runs"]
                    )
                    lines.append(
                        f"- **{markdown_table_cell(src['source_name'])}** "
                        f"({markdown_table_cell(src['source_url'])}) "
                        f"[{src.get('family', '')}] -- kind: {src['kind']}"
                    )
                    lines.append(f"  Allocations: {alloc_str}")
                lines.append("")

        warnings = conclusion.get("warnings", [])
        if warnings:
            lines.append("#### Warnings")
            lines.append("")
            for w in warnings:
                lines.append(f"- {w}")
            lines.append("")

    # Dependency analysis.
    da = comparison.get("dependency_analysis", {})
    lines.append("## Dependency Analysis")
    lines.append("")
    lines.append(f"**Overall AK dependency**: {da.get('overall_ak_statement', '')}")
    lines.append("")
    lines.append(f"**Overall conflict dependency**: {da.get('overall_conflict_statement', '')}")
    lines.append("")

    for pr in da.get("per_run", []):
        lines.append(f"### {pr['run_date']}")
        lines.append("")
        akd = pr.get("ak_dependency", {})
        lines.append(f"- AK dependency: {akd.get('statement', '')}")
        cd = pr.get("conflict_dependency", {})
        lines.append(f"- Conflict dependency: {cd.get('statement', '')}")
        lines.append(f"  - Numerator (conflict movement): {cd.get('numerator', 0)}")
        lines.append(f"  - Denominator (total movement): {cd.get('denominator', 0)}")
        lines.append(f"  - Threshold: {cd.get('threshold', 0)}")
        if cd.get("conflict_candidates"):
            lines.append("  Conflict-flagged promoted candidates:")
            for cc in cd["conflict_candidates"]:
                lines.append(
                    f"  - {cc['title']} ({cc['author_display']}) "
                    f"[{cc['identity_key']}] rank delta {cc['rank_delta']:+d}"
                )
        lines.append("")

    # Evidence summaries.
    es = comparison.get("evidence_summaries", {})
    lines.append("## Evidence Summaries")
    lines.append("")

    # Rank quality metrics.
    lines.append("### Rank Quality Metrics")
    lines.append("")
    for rm in es.get("rank_quality_metrics", []):
        lines.append(f"#### {rm['run_date']}")
        lines.append("")

        ec = rm.get("evidence_classification", {})
        lines.append(
            f"Evidence: observed Interested {ec.get('observed_interested_count', 0)} "
            f"(threshold {ec.get('threshold', EVIDENCE_THRESHOLD)}), "
            f"exploratory: {ec.get('is_exploratory', True)}"
        )
        lines.append("")

        er = rm.get("eligible_recall", {})
        lines.append(
            f"Eligible recall: canonical {er.get('canonical_recalled', 0)}, "
            f"shadow {er.get('shadow_recalled', 0)} "
            f"of {er.get('eligible_count', 0)} "
            f"(sets identical: {er.get('identity_sets_identical', False)})"
        )
        lines.append("")

        wl = rm.get("workload", {})
        dq = rm.get("discovery_deltas", {})
        lines.append(
            f"Estimated rows avoided: {_md_value(wl.get('estimated_rows_avoided'))} [{wl.get('evidence_label', '')}]"
        )
        lines.append(f"AK rows difference: {wl.get('ak_rows_difference', 0)}")
        lines.append("")

        lines.append("Discovery deltas:")
        lines.append(
            f"- Strong-interest coverage delta: {_md_value(dq.get('strong_interest_coverage_delta'))} "
            f"[{dq.get('strong_interest_coverage_delta_evidence_label', '')}]"
        )
        lines.append(f"- Actionable coverage delta: {_md_value(dq.get('actionable_coverage_delta'))}")
        lines.append(
            f"- Strong-interest median-rank delta: {_md_value(dq.get('strong_interest_median_rank_delta'))} "
            f"[{dq.get('strong_interest_median_rank_delta_evidence_label', '')}]"
        )
        lines.append(f"- Actionable median-rank delta: {_md_value(dq.get('actionable_median_rank_delta'))}")
        lines.append("")

        # Cohort coverage table.
        cc = rm.get("cohort_coverage", {})
        lines.append("Cohort coverage:")
        lines.append("")
        lines.append("| Cutoff | Cohort | Canonical in/total | Canonical rate | Shadow in/total | Shadow rate |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for cutoff in ("rank_25", "rank_50"):
            for cohort in ("strong_interest", "actionable_follow_up"):
                cov = cc.get(cutoff, {}).get(cohort, {})
                c = cov.get("canonical", {})
                s = cov.get("shadow", {})
                lines.append(
                    f"| {cutoff} | {cohort} "
                    f"| {c.get('count_in_top_cutoff', 0)} / {c.get('total_eligible', 0)} "
                    f"| {_md_value(c.get('rate'))} "
                    f"| {s.get('count_in_top_cutoff', 0)} / {s.get('total_eligible', 0)} "
                    f"| {_md_value(s.get('rate'))} |"
                )
        lines.append("")

        # Density table.
        dens = rm.get("density", {})
        lines.append("Density:")
        lines.append("")
        lines.append("| Cutoff | Cohort | Canonical in/rows | Canonical rate | Shadow in/rows | Shadow rate |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for cutoff in ("rank_25", "rank_50"):
            for cohort_label, cohort_key in (("reject", "reject"), ("already_known", "already_known")):
                d = dens.get(cutoff, {}).get(cohort_key, {})
                c = d.get("canonical", {})
                s = d.get("shadow", {})
                lines.append(
                    f"| {cutoff} | {cohort_label} "
                    f"| {c.get('count_in_top_cutoff', 0)} / {c.get('rows_inspected', 0)} "
                    f"| {_md_value(c.get('rate'))} "
                    f"| {s.get('count_in_top_cutoff', 0)} / {s.get('rows_inspected', 0)} "
                    f"| {_md_value(s.get('rate'))} |"
                )
        lines.append("")

        # Median ranks.
        mr = rm.get("median_ranks", {})
        lines.append("Median ranks:")
        lines.append("")
        lines.append("| Cohort | Canonical median | Shadow median | Evidence label |")
        lines.append("|---|---|---|---|")
        for cohort in ("strong_interest", "actionable_follow_up"):
            data = mr.get(cohort, {})
            lines.append(
                f"| {cohort} "
                f"| {_md_value(data.get('canonical'))} "
                f"| {_md_value(data.get('shadow'))} "
                f"| {data.get('evidence_label', '')} |"
            )
        lines.append("")

        wlist = rm.get("warnings", [])
        if wlist:
            lines.append("Warnings:")
            for w in wlist:
                lines.append(f"- {w}")
            lines.append("")

    # Source recommendations trend.
    lines.append("### Source Recommendations Trend")
    lines.append("")
    for st in es.get("source_recommendations", []):
        lines.append(f"**{st['run_date']}**")
        lines.append("")
        # 14 columns: Source, URL, Family, Allocation, Lifecycle, Reviewed, Int, NDA, MW, AK, Rej,
        #   Unique, Cross-Corr, Confidence, Explanation, Canonical Effect, Non-Effect
        # = 18 columns.
        header_cols = [
            "Source",
            "URL",
            "Family",
            "Allocation",
            "Lifecycle",
            "Reviewed",
            "Int",
            "NDA",
            "MW",
            "AK",
            "Rej",
            "Unique",
            "Cross-Corr",
            "Confidence",
            "Explanation",
            "Canonical Effect",
            "Non-Effect",
        ]
        header = "| " + " | ".join(header_cols) + " |"
        sep = "|" + "|".join("---" for _ in header_cols) + "|"
        lines.append(header)
        lines.append(sep)
        for rec in st.get("recommendations", []):
            row_cols = [
                markdown_table_cell(rec["source_name"]),
                markdown_table_cell(rec["source_url"]),
                markdown_table_cell(rec["family"]),
                markdown_table_cell(rec["allocation"]),
                markdown_table_cell(rec["lifecycle_status"]),
                str(rec["reviewed_count"]),
                str(rec["interested_count"]),
                str(rec["needs_deeper_analysis_count"]),
                str(rec["maybe_watch_count"]),
                str(rec["already_known_count"]),
                str(rec["reject_count"]),
                _md_value(rec.get("unique_contribution_rate")),
                _md_value(rec.get("cross_family_corroboration_rate")),
                markdown_table_cell(rec["confidence_class"]),
                markdown_table_cell(rec["explanation"]),
                markdown_table_cell(rec.get("canonical_effect", "none")),
                markdown_table_cell(rec.get("non_effect_statement", "")),
            ]
            lines.append("| " + " | ".join(row_cols) + " |")
            # Structural assertion: row has same col count as header.
            assert len(row_cols) == len(header_cols), (
                f"Column count mismatch: header={len(header_cols)}, row={len(row_cols)}"
            )
        lines.append("")

    # Source-family diversity.
    lines.append("### Source-Family Diversity")
    lines.append("")
    for dt in es.get("source_family_diversity", []):
        lines.append(f"**{dt['run_date']}**")
        r25 = dt["rank_25"]
        r50 = dt["rank_50"]
        lines.append(
            f"- Rank 25: canonical {r25['canonical_family_count']} "
            f"({', '.join(r25['canonical_families']) or 'none'}), "
            f"shadow {r25['shadow_family_count']} "
            f"({', '.join(r25['shadow_families']) or 'none'})"
        )
        lines.append(
            f"- Rank 50: canonical {r50['canonical_family_count']} "
            f"({', '.join(r50['canonical_families']) or 'none'}), "
            f"shadow {r50['shadow_family_count']} "
            f"({', '.join(r50['shadow_families']) or 'none'})"
        )
        lines.append("")

    # Diagnostic counts.
    lines.append("### Candidate-Quality Diagnostic Counts")
    lines.append("")
    for dct in es.get("candidate_quality_diagnostic_counts", []):
        parts = [f"{k}: {v}" for k, v in sorted(dct["class_counts"].items())]
        lines.append(f"- **{dct['run_date']}**: {'; '.join(parts)}")
    lines.append("")

    # Promoted/demoted examples.
    lines.append("### Promoted/Demoted Examples")
    lines.append("")
    for et in es.get("promoted_demoted_examples", []):
        lines.append(f"**{et['run_date']}** -- promoted: {et['promoted_count']}, demoted: {et['demoted_count']}")
        for p in et.get("promoted", []):
            explanations = " | ".join(str(e) for e in p.get("explanations", []))
            lines.append(
                f"- Promoted: {p['title']} ({p['author_display']}) "
                f"[{p['identity_key']}] delta {p['rank_delta']:+d} "
                f"({explanations})"
            )
        for d in et.get("demoted", []):
            explanations = " | ".join(str(e) for e in d.get("explanations", []))
            lines.append(
                f"- Demoted: {d['title']} ({d['author_display']}) "
                f"[{d['identity_key']}] delta {d['rank_delta']:+d} "
                f"({explanations})"
            )
        lines.append("")

    # False-negative audit.
    lines.append("### False-Negative Audit Evidence")
    lines.append("")
    for ft in es.get("false_negative_audit", []):
        lines.append(f"**{ft['run_date']}** -- count: {ft['count']}")
        for item in ft.get("items", []):
            explanations = " | ".join(str(e) for e in item.get("explanations", []))
            lines.append(
                f"- {item['title']} ({item['author_display']}) "
                f"[{item['identity_key']}] delta {item['rank_delta']:+d} "
                f"({explanations})"
            )
        lines.append("")

    return "\n".join(lines) + "\n"
