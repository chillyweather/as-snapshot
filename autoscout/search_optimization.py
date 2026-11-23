"""Read-only, explainable shadow ordering for Search Optimization Reviews."""

from __future__ import annotations

import json
import statistics
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from datetime import timedelta
from pathlib import Path
from typing import Any

from autoscout.identity import normalize_author_display
from autoscout.identity import normalize_author_identity
from autoscout.identity import normalize_isbn
from autoscout.identity import normalize_work_title
from autoscout.identity import normalized_identity_key
from autoscout.repository_root import repository_root
from autoscout.review_rendering import markdown_table_cell
from autoscout.scouting_candidates import INELIGIBLE_CANDIDATE_CATEGORIES
from autoscout.scouting_candidates import PRIORITY_ORDER
from autoscout.scouting_candidates import ScoutingCandidate
from autoscout.source_lifecycle import parse_iso_date
from autoscout.source_lifecycle import rate
from autoscout.source_lifecycle import rate_label
from autoscout.weekly_report import WeeklyReportStore

SEARCH_OPTIMIZATION_SCHEMA_VERSION = 4

DIAGNOSTIC_CLASS_IDS = (
    "already_known_in_normal_review",
    "reject_resurfaced_without_material_change",
    "probable_duplicate_identity_fragmentation",
    "scouting_candidate_definition_conflict",
    "missing_trial_structured_data",
)

DIAGNOSTIC_LABELS = {
    "already_known_in_normal_review": "Already Known candidate in normal review",
    "reject_resurfaced_without_material_change": "Reject candidate resurfaced without Material Change",
    "probable_duplicate_identity_fragmentation": "Probable duplicate identity fragmentation",
    "scouting_candidate_definition_conflict": "Scouting Candidate definition conflict",
    "missing_trial_structured_data": "Missing structured data required to evaluate the trial",
}

DIAGNOSTIC_RANK_EFFECTS = {
    "already_known_in_normal_review": (
        "No shadow-rank effect: the candidate is in the normal review population; ranking applies normally."
    ),
    "reject_resurfaced_without_material_change": (
        "No shadow-rank effect: the candidate was already suppressed by Candidate History before the shadow review, "
        "so it never reached the eligible set. No shadow rank is assigned."
    ),
    "probable_duplicate_identity_fragmentation": (
        "No shadow-rank effect: these are distinct canonical identities that the deterministic conservative "
        "matcher deliberately does not merge. No identity change is applied."
    ),
    "scouting_candidate_definition_conflict": (
        "No shadow-rank effect: diagnostics themselves never affect shadow rank. "
        "Category conflicts already excluded by the canonical category gate remain excluded and have no shadow rank. "
        "Work-type conflicts that canonical rules did not gate remain normally ranked."
    ),
    "missing_trial_structured_data": (
        "No shadow-rank effect: missing data is reported for awareness; no values are invented or imputed."
    ),
}

SECTION_NON_EFFECTS = [
    (
        "Candidate-quality diagnostics never change shadow rank or eligibility; they merely report whether "
        "an independent canonical gate (suppression, category exclusion) already applied."
    ),
    (
        "Diagnostics do not normalize categories, merge identities, infer rejection reasons, auto-fix defects, "
        "or alter any canonical editorial artifact."
    ),
    ("The shadow review remains a read-only comparison; diagnostics are informational only."),
]
VALUABLE_COHORTS = ("strong_interest", "actionable_follow_up")
EVIDENCE_THRESHOLD = 5
MAX_EXAMPLE_CAP = 6
EXPLANATION_COLLAPSE_THRESHOLD = 6

SEARCH_OPTIMIZATION_JSON_FILENAME = "search-optimization-review.json"
SEARCH_OPTIMIZATION_MD_FILENAME = "search-optimization-review.md"
DEFAULT_SEARCH_OPTIMIZATION_REVIEW_PATH = (
    repository_root() / "runs/current-weekly-pilot-russian" / SEARCH_OPTIMIZATION_JSON_FILENAME
)

OUTCOME_COHORTS = {
    "interested": "strong_interest",
    "needs_deeper_analysis": "actionable_follow_up",
    "maybe_watch": "watch",
    "already_known": "known_workflow_item",
    "reject": "negative",
}

OUTCOME_LABELS = {
    "strong_interest": "Interested - strong interest",
    "actionable_follow_up": "Needs Deeper Analysis - actionable follow-up",
    "watch": "Maybe / Watch - watch",
    "known_workflow_item": "Already Known - known workflow item",
    "negative": "Reject - negative",
    "unreviewed": "Unreviewed",
}

CANONICAL_CONSTRAINTS = [
    "Editor-Facing Exclusions",
    "Hard Rules",
    "Candidate History suppression",
    "Material Change behavior",
    "Visible Risk floors",
    "accepted Soft Preference effects",
    "explicit editor visibility",
]


@dataclass(frozen=True)
class SearchOptimizationInputs:
    """Canonical inputs for one read-only Search Optimization Review."""

    candidates: list[dict[str, Any]]
    candidate_history: list[dict[str, Any]]
    run_date: str
    suppressed_candidates: list[dict[str, Any]] | None = None
    source_mentions: list[dict[str, Any]] | None = None
    internal_exclusions: list[dict[str, Any]] | None = None
    all_current_candidates: list[dict[str, Any]] | None = None
    source_metrics: dict[str, Any] | None = None


ComparisonNumber = int | float | None


@dataclass(frozen=True)
class ComparisonEvidenceClassification:
    observed_interested_count: int
    threshold: int
    is_exploratory: bool


@dataclass(frozen=True)
class ComparisonCoverage:
    count_in_top_cutoff: int
    total_eligible: int
    rate: ComparisonNumber
    evidence_label: str


@dataclass(frozen=True)
class ComparisonCohortCoverage:
    canonical: ComparisonCoverage
    shadow: ComparisonCoverage


@dataclass(frozen=True)
class ComparisonCohortWindow:
    strong_interest: ComparisonCohortCoverage
    actionable_follow_up: ComparisonCohortCoverage


@dataclass(frozen=True)
class ComparisonDensityValue:
    count_in_top_cutoff: int
    rows_inspected: int
    rate: ComparisonNumber


@dataclass(frozen=True)
class ComparisonDensity:
    canonical_reject: ComparisonDensityValue
    shadow_reject: ComparisonDensityValue
    canonical_already_known: ComparisonDensityValue
    shadow_already_known: ComparisonDensityValue


@dataclass(frozen=True)
class ComparisonDiversity:
    canonical_family_count: int
    shadow_family_count: int
    canonical_families: tuple[str, ...]
    shadow_families: tuple[str, ...]


@dataclass(frozen=True)
class ComparisonMedianRanks:
    canonical: ComparisonNumber
    shadow: ComparisonNumber
    evidence_label: str


@dataclass(frozen=True)
class ComparisonDiscoveryQuality:
    strong_interest_coverage_delta: ComparisonNumber
    strong_interest_coverage_delta_evidence_label: str
    actionable_coverage_delta: ComparisonNumber
    strong_interest_median_rank_delta: ComparisonNumber
    strong_interest_median_rank_delta_evidence_label: str
    actionable_median_rank_delta: ComparisonNumber


@dataclass(frozen=True)
class ComparisonWorkload:
    estimated_rows_avoided: ComparisonNumber
    evidence_label: str
    ak_rows_difference: int
    discovery_quality: ComparisonDiscoveryQuality


@dataclass(frozen=True)
class ComparisonEligibleRecall:
    canonical_recalled: int
    shadow_recalled: int
    eligible_count: int
    identity_sets_identical: bool


@dataclass(frozen=True)
class ComparisonMetrics:
    evidence_classification: ComparisonEvidenceClassification
    eligible_recall: ComparisonEligibleRecall
    rank_25_coverage: ComparisonCohortWindow
    rank_50_coverage: ComparisonCohortWindow
    rank_25_density: ComparisonDensity
    rank_50_density: ComparisonDensity
    rank_25_diversity: ComparisonDiversity
    rank_50_diversity: ComparisonDiversity
    strong_interest_median_ranks: ComparisonMedianRanks
    actionable_median_ranks: ComparisonMedianRanks
    workload: ComparisonWorkload
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ComparisonSourceRecommendation:
    source_name: str
    source_url: str
    family: str
    allocation: str
    lifecycle_status: str
    reviewed_count: int
    interested_count: int
    needs_deeper_analysis_count: int
    maybe_watch_count: int
    already_known_count: int
    reject_count: int
    unique_contribution_rate: ComparisonNumber
    cross_family_corroboration_rate: ComparisonNumber
    confidence_class: str
    explanation: str
    canonical_effect: str
    non_effect_statement: str


@dataclass(frozen=True)
class ComparisonCandidate:
    identity_key: str
    title: str
    author_display: str
    canonical_rank: int
    shadow_rank: int
    rank_delta: int
    outcome_cohort: str
    explanations: tuple[str, ...]


@dataclass(frozen=True)
class ComparisonExamples:
    promoted_count: int
    demoted_count: int
    promoted: tuple[ComparisonCandidate, ...]
    demoted: tuple[ComparisonCandidate, ...]


@dataclass(frozen=True)
class ComparisonDiagnostics:
    class_counts: tuple[tuple[str, int], ...]
    normal_review_conflict_identity_keys: frozenset[str]

    def count(self, class_id: str) -> int:
        return dict(self.class_counts).get(class_id, 0)


@dataclass(frozen=True)
class ComparisonRunOutcome:
    run_date: str
    run_identity: str
    candidate_count: int
    eligible_count: int
    metrics: ComparisonMetrics
    source_recommendations: tuple[ComparisonSourceRecommendation, ...]
    candidates: tuple[ComparisonCandidate, ...]
    diagnostics: ComparisonDiagnostics
    examples: ComparisonExamples
    false_negative_audit: tuple[ComparisonCandidate, ...]


def _check_dict(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"'{path}' is not a dict.")
    return value


def _check_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"'{path}' is not a list.")
    return value


def _check_int(value: Any, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"'{path}' is not an integer (got {type(value).__name__}).")
    return value


def _check_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"'{path}' is not a boolean (got {type(value).__name__}).")
    return value


def validate_search_optimization_review(mapping: Mapping[str, Any]) -> None:
    """Validate schema-v4 compatibility at the owning domain seam."""
    if not isinstance(mapping, Mapping):
        raise ValueError("Review is not a JSON object.")
    if mapping.get("artifact_type") != "search-optimization-review":
        raise ValueError(
            f"Review has artifact_type '{mapping.get('artifact_type', '')}', expected 'search-optimization-review'."
        )
    if mapping.get("schema_version") != SEARCH_OPTIMIZATION_SCHEMA_VERSION:
        raise ValueError(
            f"Review has schema version {mapping.get('schema_version')}, expected {SEARCH_OPTIMIZATION_SCHEMA_VERSION}."
        )
    if mapping.get("canonical_effect") != "none":
        raise ValueError(f"Review has canonical_effect '{mapping.get('canonical_effect')}', expected 'none'.")
    run_identity = str(mapping.get("run_identity", ""))
    run_date = str(mapping.get("run_date", ""))
    if not run_identity.strip() or not run_date.strip():
        raise ValueError("Review has empty run identity or run date.")
    if run_identity != run_date:
        raise ValueError(f"Review run_identity '{run_identity}' does not match run_date '{run_date}'.")
    try:
        date.fromisoformat(run_date)
    except ValueError:
        raise ValueError(f"Review has invalid run_date '{run_date}'. Expected YYYY-MM-DD.")

    required_sections = (
        "metrics",
        "examples",
        "false_negative_audit",
        "candidate_quality_diagnostics",
        "source_allocation",
        "candidates",
    )
    for section in required_sections:
        if section not in mapping:
            raise ValueError(f"Review (run {run_date}) is missing required section '{section}'.")

    metrics = _check_dict(mapping.get("metrics"), "metrics")
    evidence = _check_dict(metrics.get("evidence_classification"), "metrics.evidence_classification")
    _check_int(evidence.get("observed_interested_count"), "metrics.evidence_classification.observed_interested_count")
    _check_int(evidence.get("threshold"), "metrics.evidence_classification.threshold")
    _check_bool(evidence.get("is_exploratory"), "metrics.evidence_classification.is_exploratory")
    workload = _check_dict(metrics.get("workload"), "metrics.workload")
    ak = _check_dict(workload.get("already_known_exposure"), "metrics.workload.already_known_exposure")
    _check_int(ak.get("ak_rows_difference"), "metrics.workload.already_known_exposure.ak_rows_difference")
    discovery = _check_dict(workload.get("discovery_quality"), "metrics.workload.discovery_quality")
    for field_name in (
        "strong_interest_coverage_delta",
        "strong_interest_coverage_delta_evidence_label",
        "actionable_coverage_delta",
        "strong_interest_median_rank_delta",
        "strong_interest_median_rank_delta_evidence_label",
        "actionable_median_rank_delta",
    ):
        if field_name not in discovery:
            raise ValueError(f"metrics.workload.discovery_quality missing '{field_name}'.")
    for field_name in ("estimated_rows_avoided", "evidence_label"):
        if field_name not in workload:
            raise ValueError(f"metrics.workload missing '{field_name}'.")
    diversity = _check_dict(metrics.get("diversity"), "metrics.diversity")
    for cutoff in ("rank_25", "rank_50"):
        cutoff_data = _check_dict(diversity.get(cutoff), f"metrics.diversity.{cutoff}")
        for order in ("canonical", "shadow"):
            order_data = _check_dict(cutoff_data.get(order), f"metrics.diversity.{cutoff}.{order}")
            _check_int(order_data.get("family_count"), f"metrics.diversity.{cutoff}.{order}.family_count")
            if order_data.get("family_names") is not None:
                _check_list(order_data["family_names"], f"metrics.diversity.{cutoff}.{order}.family_names")
    for section_name in ("median_ranks", "cohort_coverage", "density", "eligible_recall"):
        _check_dict(metrics.get(section_name), f"metrics.{section_name}")
    _check_list(metrics.get("warnings"), "metrics.warnings")

    diagnostics = _check_dict(mapping.get("candidate_quality_diagnostics"), "candidate_quality_diagnostics")
    classes = _check_dict(diagnostics.get("classes"), "candidate_quality_diagnostics.classes")
    for class_id in DIAGNOSTIC_CLASS_IDS:
        class_data = _check_dict(classes.get(class_id), f"candidate_quality_diagnostics.classes.{class_id}")
        count = _check_int(class_data.get("count"), f"candidate_quality_diagnostics.classes.{class_id}.count")
        if count < 0:
            raise ValueError(f"Diagnostic class '{class_id}' has negative count {count}.")
        _check_list(class_data.get("findings"), f"candidate_quality_diagnostics.classes.{class_id}.findings")

    allocation = _check_dict(mapping.get("source_allocation"), "source_allocation")
    recommendations = _check_list(allocation.get("recommendations"), "source_allocation.recommendations")
    seen: set[str] = set()
    for index, recommendation in enumerate(recommendations):
        item = _check_dict(recommendation, f"source_allocation.recommendations[{index}]")
        for field_name in ("source_name", "source_url", "allocation"):
            if field_name not in item:
                raise ValueError(f"Source recommendation {index} missing '{field_name}'.")
        if str(item.get("allocation", "")) not in ALLOWED_ALLOCATION_LABELS:
            raise ValueError(f"Invalid allocation label '{item.get('allocation', '')}'.")
        identity = f"{item.get('source_name', '')}|{item.get('source_url', '')}"
        if identity in seen:
            raise ValueError(f"Duplicate source identity '{identity}' in source allocation recommendations.")
        seen.add(identity)

    examples = _check_dict(mapping.get("examples"), "examples")
    _check_list(examples.get("promoted"), "examples.promoted")
    _check_list(examples.get("demoted"), "examples.demoted")
    _check_list(mapping.get("false_negative_audit"), "false_negative_audit")
    _check_list(mapping.get("candidates"), "candidates")


def _comparison_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _comparison_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _comparison_number(value: Any) -> ComparisonNumber:
    if value is None or isinstance(value, int | float):
        return value
    return None


def _comparison_coverage(value: Any) -> ComparisonCoverage:
    mapping = _comparison_mapping(value)
    return ComparisonCoverage(
        count_in_top_cutoff=int(mapping.get("count_in_top_cutoff", 0)),
        total_eligible=int(mapping.get("total_eligible_in_cohort", 0)),
        rate=_comparison_number(mapping.get("rate")),
        evidence_label=str(mapping.get("evidence_label", "")),
    )


def _comparison_cohort_coverage_pair(
    canonical: Mapping[str, Any],
    shadow: Mapping[str, Any],
    cohort: str,
) -> ComparisonCohortCoverage:
    return ComparisonCohortCoverage(
        canonical=_comparison_coverage(canonical.get(cohort)),
        shadow=_comparison_coverage(shadow.get(cohort)),
    )


def _comparison_cohort_window(metrics: Mapping[str, Any], cutoff: str) -> ComparisonCohortWindow:
    data = _comparison_mapping(_comparison_mapping(metrics.get("cohort_coverage")).get(cutoff))
    canonical = _comparison_mapping(data.get("canonical"))
    shadow = _comparison_mapping(data.get("shadow"))
    return ComparisonCohortWindow(
        strong_interest=_comparison_cohort_coverage_pair(canonical, shadow, "strong_interest"),
        actionable_follow_up=_comparison_cohort_coverage_pair(canonical, shadow, "actionable_follow_up"),
    )


def _comparison_density_value(value: Any) -> ComparisonDensityValue:
    mapping = _comparison_mapping(value)
    return ComparisonDensityValue(
        count_in_top_cutoff=int(mapping.get("count_in_top_cutoff", 0)),
        rows_inspected=int(mapping.get("rows_inspected", 0)),
        rate=_comparison_number(mapping.get("rate")),
    )


def _comparison_density(metrics: Mapping[str, Any], cutoff: str) -> ComparisonDensity:
    data = _comparison_mapping(_comparison_mapping(metrics.get("density")).get(cutoff))
    canonical = _comparison_mapping(data.get("canonical"))
    shadow = _comparison_mapping(data.get("shadow"))
    return ComparisonDensity(
        canonical_reject=_comparison_density_value(canonical.get("negative")),
        shadow_reject=_comparison_density_value(shadow.get("negative")),
        canonical_already_known=_comparison_density_value(canonical.get("known_workflow_item")),
        shadow_already_known=_comparison_density_value(shadow.get("known_workflow_item")),
    )


def _comparison_diversity(metrics: Mapping[str, Any], cutoff: str) -> ComparisonDiversity:
    data = _comparison_mapping(_comparison_mapping(metrics.get("diversity")).get(cutoff))
    canonical = _comparison_mapping(data.get("canonical"))
    shadow = _comparison_mapping(data.get("shadow"))
    return ComparisonDiversity(
        canonical_family_count=int(canonical.get("family_count", 0)),
        shadow_family_count=int(shadow.get("family_count", 0)),
        canonical_families=tuple(str(value) for value in _comparison_list(canonical.get("family_names"))),
        shadow_families=tuple(str(value) for value in _comparison_list(shadow.get("family_names"))),
    )


def _comparison_median_ranks(metrics: Mapping[str, Any], cohort: str) -> ComparisonMedianRanks:
    value = _comparison_mapping(_comparison_mapping(metrics.get("median_ranks")).get(cohort))
    return ComparisonMedianRanks(
        canonical=_comparison_number(value.get("canonical")),
        shadow=_comparison_number(value.get("shadow")),
        evidence_label=str(value.get("evidence_label", "")),
    )


def _comparison_metrics(mapping: Mapping[str, Any]) -> ComparisonMetrics:
    metrics = _comparison_mapping(mapping.get("metrics"))
    evidence = _comparison_mapping(metrics.get("evidence_classification"))
    workload = _comparison_mapping(metrics.get("workload"))
    already_known = _comparison_mapping(workload.get("already_known_exposure"))
    discovery = _comparison_mapping(workload.get("discovery_quality"))
    return ComparisonMetrics(
        evidence_classification=ComparisonEvidenceClassification(
            observed_interested_count=int(evidence.get("observed_interested_count", 0)),
            threshold=int(evidence.get("threshold", EVIDENCE_THRESHOLD)),
            is_exploratory=bool(evidence.get("is_exploratory", True)),
        ),
        eligible_recall=_comparison_eligible_recall(metrics),
        rank_25_coverage=_comparison_cohort_window(metrics, "rank_25"),
        rank_50_coverage=_comparison_cohort_window(metrics, "rank_50"),
        rank_25_density=_comparison_density(metrics, "rank_25"),
        rank_50_density=_comparison_density(metrics, "rank_50"),
        rank_25_diversity=_comparison_diversity(metrics, "rank_25"),
        rank_50_diversity=_comparison_diversity(metrics, "rank_50"),
        strong_interest_median_ranks=_comparison_median_ranks(metrics, "strong_interest"),
        actionable_median_ranks=_comparison_median_ranks(metrics, "actionable_follow_up"),
        workload=ComparisonWorkload(
            estimated_rows_avoided=_comparison_number(workload.get("estimated_rows_avoided")),
            evidence_label=str(workload.get("evidence_label", "")),
            ak_rows_difference=int(already_known.get("ak_rows_difference", 0)),
            discovery_quality=ComparisonDiscoveryQuality(
                strong_interest_coverage_delta=_comparison_number(discovery.get("strong_interest_coverage_delta")),
                strong_interest_coverage_delta_evidence_label=str(
                    discovery.get("strong_interest_coverage_delta_evidence_label", "")
                ),
                actionable_coverage_delta=_comparison_number(discovery.get("actionable_coverage_delta")),
                strong_interest_median_rank_delta=_comparison_number(
                    discovery.get("strong_interest_median_rank_delta")
                ),
                strong_interest_median_rank_delta_evidence_label=str(
                    discovery.get("strong_interest_median_rank_delta_evidence_label", "")
                ),
                actionable_median_rank_delta=_comparison_number(discovery.get("actionable_median_rank_delta")),
            ),
        ),
        warnings=tuple(str(value) for value in _comparison_list(metrics.get("warnings"))),
    )


def _comparison_eligible_recall(metrics: Mapping[str, Any]) -> ComparisonEligibleRecall:
    recall = _comparison_mapping(metrics.get("eligible_recall"))
    canonical = _comparison_mapping(recall.get("canonical"))
    shadow = _comparison_mapping(recall.get("shadow"))
    return ComparisonEligibleRecall(
        canonical_recalled=int(canonical.get("recalled_count", 0)),
        shadow_recalled=int(shadow.get("recalled_count", 0)),
        eligible_count=int(canonical.get("eligible_count", 0)),
        identity_sets_identical=bool(recall.get("identity_sets_identical", False)),
    )


def _comparison_source_recommendation(mapping: Mapping[str, Any]) -> ComparisonSourceRecommendation:
    return ComparisonSourceRecommendation(
        source_name=str(mapping.get("source_name", "")),
        source_url=str(mapping.get("source_url", "")),
        family=str(mapping.get("family", "")),
        allocation=str(mapping.get("allocation", "")),
        lifecycle_status=str(mapping.get("lifecycle_status", "")),
        reviewed_count=int(mapping.get("reviewed_count", 0)),
        interested_count=int(mapping.get("interested_count", 0)),
        needs_deeper_analysis_count=int(mapping.get("needs_deeper_analysis_count", 0)),
        maybe_watch_count=int(mapping.get("maybe_watch_count", 0)),
        already_known_count=int(mapping.get("already_known_count", 0)),
        reject_count=int(mapping.get("reject_count", 0)),
        unique_contribution_rate=_comparison_number(mapping.get("unique_contribution_rate")),
        cross_family_corroboration_rate=_comparison_number(mapping.get("cross_family_corroboration_rate")),
        confidence_class=str(mapping.get("confidence_class", "")),
        explanation=str(mapping.get("explanation", "")),
        canonical_effect=str(mapping.get("canonical_effect", "none")),
        non_effect_statement=str(mapping.get("non_effect_statement", "")),
    )


def _comparison_candidate(mapping: Mapping[str, Any]) -> ComparisonCandidate:
    return ComparisonCandidate(
        identity_key=str(mapping.get("identity_key", "")),
        title=str(mapping.get("title", "")),
        author_display=str(mapping.get("author_display", "")),
        canonical_rank=int(mapping.get("canonical_rank", 0)),
        shadow_rank=int(mapping.get("shadow_rank", 0)),
        rank_delta=int(mapping.get("rank_delta", 0)),
        outcome_cohort=str(mapping.get("outcome_cohort", "")),
        explanations=tuple(str(value) for value in _comparison_list(mapping.get("explanations"))),
    )


def _comparison_diagnostics(mapping: Mapping[str, Any]) -> ComparisonDiagnostics:
    diagnostics = _comparison_mapping(mapping.get("candidate_quality_diagnostics"))
    classes = _comparison_mapping(diagnostics.get("classes"))
    class_counts = tuple(
        (
            class_id,
            int(_comparison_mapping(classes.get(class_id)).get("count", 0)),
        )
        for class_id in DIAGNOSTIC_CLASS_IDS
    )
    conflict_findings = _comparison_list(
        _comparison_mapping(classes.get("scouting_candidate_definition_conflict")).get("findings")
    )
    conflict_ids = frozenset(
        str(_comparison_mapping(finding).get("identity_key", ""))
        for finding in conflict_findings
        if str(_comparison_mapping(finding).get("canonical_gate", "")) == "normal-review"
    )
    return ComparisonDiagnostics(class_counts=class_counts, normal_review_conflict_identity_keys=conflict_ids)


def _comparison_examples(mapping: Mapping[str, Any]) -> ComparisonExamples:
    examples = _comparison_mapping(mapping.get("examples"))
    promoted = tuple(
        _comparison_candidate(_comparison_mapping(value)) for value in _comparison_list(examples.get("promoted"))
    )
    demoted = tuple(
        _comparison_candidate(_comparison_mapping(value)) for value in _comparison_list(examples.get("demoted"))
    )
    return ComparisonExamples(
        promoted_count=int(examples.get("promoted_count", 0)),
        demoted_count=int(examples.get("demoted_count", 0)),
        promoted=promoted,
        demoted=demoted,
    )


def _comparison_audit(mapping: Mapping[str, Any]) -> tuple[ComparisonCandidate, ...]:
    return tuple(
        _comparison_candidate(_comparison_mapping(value))
        for value in _comparison_list(mapping.get("false_negative_audit"))
    )


def _comparison_outcome_from_mapping(mapping: Mapping[str, Any]) -> ComparisonRunOutcome:
    candidates = tuple(
        _comparison_candidate(_comparison_mapping(value)) for value in _comparison_list(mapping.get("candidates"))
    )
    source_allocation = _comparison_mapping(mapping.get("source_allocation"))
    recommendations = tuple(
        _comparison_source_recommendation(_comparison_mapping(value))
        for value in _comparison_list(source_allocation.get("recommendations"))
    )
    return ComparisonRunOutcome(
        run_date=str(mapping["run_date"]),
        run_identity=str(mapping["run_identity"]),
        candidate_count=int(mapping.get("candidate_count", 0)),
        eligible_count=int(mapping.get("eligible_count", 0)),
        metrics=_comparison_metrics(mapping),
        source_recommendations=recommendations,
        candidates=candidates,
        diagnostics=_comparison_diagnostics(mapping),
        examples=_comparison_examples(mapping),
        false_negative_audit=_comparison_audit(mapping),
    )


class SearchOptimizationReview(Mapping[str, Any]):
    """Domain facade for construction, compatibility, and representation.

    The raw validated mapping remains private so JSON and Markdown retain the
    existing schema and ordering.  Comparison callers receive typed domain
    outcomes instead of inspecting implementation sections.
    """

    def __init__(self, mapping: Mapping[str, Any]) -> None:
        validate_search_optimization_review(mapping)
        self._mapping = deepcopy(dict(mapping))

    @classmethod
    def construct(
        cls,
        inputs: SearchOptimizationInputs | None = None,
        *,
        candidates: list[dict[str, Any]] | None = None,
        candidate_history: list[dict[str, Any]] | None = None,
        run_date: str | None = None,
        suppressed_candidates: list[dict[str, Any]] | None = None,
        source_mentions: list[dict[str, Any]] | None = None,
        internal_exclusions: list[dict[str, Any]] | None = None,
        all_current_candidates: list[dict[str, Any]] | None = None,
        source_metrics: dict[str, Any] | None = None,
    ) -> SearchOptimizationReview:
        if inputs is None:
            if candidates is None or candidate_history is None or not run_date:
                raise TypeError("SearchOptimizationInputs or candidates, candidate_history, and run_date are required")
            inputs = SearchOptimizationInputs(
                candidates=candidates,
                candidate_history=candidate_history,
                run_date=run_date,
                suppressed_candidates=suppressed_candidates,
                source_mentions=source_mentions,
                internal_exclusions=internal_exclusions,
                all_current_candidates=all_current_candidates,
                source_metrics=source_metrics,
            )
        mapping = _build_search_optimization_review_mapping(
            inputs.candidates,
            inputs.candidate_history,
            inputs.run_date,
            suppressed_candidates=inputs.suppressed_candidates,
            source_mentions=inputs.source_mentions,
            internal_exclusions=inputs.internal_exclusions,
            all_current_candidates=inputs.all_current_candidates,
            source_metrics=inputs.source_metrics,
        )
        return cls(mapping)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> SearchOptimizationReview:
        return cls(mapping)

    def __getitem__(self, key: str) -> Any:
        return deepcopy(self._mapping[key])

    def __iter__(self):
        return iter(self._mapping)

    def __len__(self) -> int:
        return len(self._mapping)

    def to_mapping(self) -> dict[str, Any]:
        return deepcopy(self._mapping)

    @property
    def run_date(self) -> str:
        return str(self._mapping["run_date"])

    @property
    def run_identity(self) -> str:
        return str(self._mapping["run_identity"])

    def comparison_outcome(self) -> ComparisonRunOutcome:
        """Return an immutable comparison outcome owned by this domain module."""
        return _comparison_outcome_from_mapping(self._mapping)

    @classmethod
    def compare_three(
        cls,
        reviews: Sequence[SearchOptimizationReview],
        output_dir: Path,
    ) -> dict[str, Any]:
        """Compare exactly three validated review domain objects."""
        if len(reviews) != 3:
            raise ValueError("Exactly three review artifacts are required.")
        if not all(isinstance(review, cls) for review in reviews):
            raise TypeError("compare_three requires SearchOptimizationReview objects.")
        identities = [review.run_identity for review in reviews]
        if len(set(identities)) < 3:
            raise ValueError(f"Duplicate run identities detected: {identities}. All three runs must be distinct.")
        outcomes = [review.comparison_outcome() for review in reviews]
        from autoscout.search_optimization_comparison import _compare_validated_reviews

        return _compare_validated_reviews(outcomes, output_dir)

    def to_json(self) -> str:
        return json.dumps(self._mapping, indent=2, ensure_ascii=False, sort_keys=True) + "\n"

    def to_markdown(self) -> str:
        return render_search_optimization_review(self)

    def write(self, output_dir: Path) -> None:
        store = WeeklyReportStore(output_dir)
        store.write_json(SEARCH_OPTIMIZATION_JSON_FILENAME, self._mapping)
        store.write_text(SEARCH_OPTIMIZATION_MD_FILENAME, self.to_markdown())


def compare_search_optimization_reviews(
    reviews: Sequence[Mapping[str, Any] | SearchOptimizationReview],
    output_dir: Path,
) -> dict[str, Any]:
    """Validate compatibility inputs before comparing domain outcomes."""
    validated_reviews: list[SearchOptimizationReview] = []
    for idx, review in enumerate(reviews):
        try:
            validated_reviews.append(
                review
                if isinstance(review, SearchOptimizationReview)
                else SearchOptimizationReview.from_mapping(review)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Review {idx + 1}: {exc}") from exc
    return SearchOptimizationReview.compare_three(validated_reviews, output_dir)


def build_search_optimization_review(
    candidates: list[dict[str, Any]],
    candidate_history: list[dict[str, Any]],
    run_date: str,
    *,
    suppressed_candidates: list[dict[str, Any]] | None = None,
    source_mentions: list[dict[str, Any]] | None = None,
    internal_exclusions: list[dict[str, Any]] | None = None,
    all_current_candidates: list[dict[str, Any]] | None = None,
    source_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compatibility mapping facade for existing callers."""
    return SearchOptimizationReview.construct(
        SearchOptimizationInputs(
            candidates=candidates,
            candidate_history=candidate_history,
            run_date=run_date,
            suppressed_candidates=suppressed_candidates,
            source_mentions=source_mentions,
            internal_exclusions=internal_exclusions,
            all_current_candidates=all_current_candidates,
            source_metrics=source_metrics,
        )
    ).to_mapping()


def _build_search_optimization_review_mapping(
    candidates: list[dict[str, Any]],
    candidate_history: list[dict[str, Any]],
    run_date: str,
    *,
    suppressed_candidates: list[dict[str, Any]] | None = None,
    source_mentions: list[dict[str, Any]] | None = None,
    internal_exclusions: list[dict[str, Any]] | None = None,
    all_current_candidates: list[dict[str, Any]] | None = None,
    source_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a derived comparison without mutating candidates or state."""
    history_by_identity = {
        str(record.get("identity_key", "")): record
        for record in candidate_history
        if str(record.get("identity_key", "")).strip()
    }
    canonical = [
        _candidate_comparison(candidate, canonical_rank, history_by_identity)
        for canonical_rank, candidate in enumerate(candidates, start=1)
    ]

    # Build source allocation recommendations from source metrics artifact.
    source_allocation = _build_source_allocation(
        source_metrics=source_metrics,
        run_date=run_date,
    )

    # Build source outcome history shadow factor from allocation recs.
    source_outcome_history = _build_source_outcome_history_factor(
        source_allocation=source_allocation,
        canonical=canonical,
    )

    # Build the candidate factor lookup for shadow sort.
    candidate_factors = source_outcome_history.get("candidate_factors", {})
    candidate_evidence = source_outcome_history.get("candidate_evidence", {})

    # Shadow sort: inject source outcome history as a named final lexicographic
    # shadow factor before the stable tie-breaker.
    def _source_outcome_factor(c: dict[str, Any]) -> tuple[int, str]:
        ik = str(c["identity_key"])
        evidence = candidate_evidence.get(ik, {})
        ordinal = candidate_factors.get(ik, NEUTRAL_OUTCOME_ORDINAL)
        outcome_class = evidence.get("outcome_class", "insufficient data")
        return (ordinal, f"Source Outcome History {outcome_class}")

    # Build extended shadow factor builders.
    extended_factors: tuple[tuple[str, ShadowFactorBuilder], ...] = (
        *SHADOW_FACTOR_BUILDERS,
        ("source outcome history", _source_outcome_factor),
    )

    # Shadow sort using extended factors.
    def _extended_sort_key(c: dict[str, Any]) -> tuple[int, ...]:
        result = tuple(builder(c)[0] for _name, builder in extended_factors)
        return (*result, int(c["canonical_rank"]))

    shadow_order = sorted(canonical, key=_extended_sort_key)
    shadow_rank_by_identity = {
        str(candidate["identity_key"]): shadow_rank for shadow_rank, candidate in enumerate(shadow_order, start=1)
    }

    # Extended shadow factors for explanations.
    def _extended_shadow_factors(c: dict[str, Any]) -> tuple[tuple[int, str], ...]:
        return tuple(builder(c) for _name, builder in extended_factors)

    for candidate in canonical:
        canonical_rank = int(candidate["canonical_rank"])
        shadow_rank = shadow_rank_by_identity[str(candidate["identity_key"])]
        candidate["shadow_rank"] = shadow_rank
        candidate["rank_delta"] = canonical_rank - shadow_rank
    for candidate in canonical:
        candidate["explanations"] = _movement_explanations_with_factors(candidate, canonical, _extended_shadow_factors)

    # Derive ordering factor names directly from the extended builders.
    ordering_factors = [name for name, _ in extended_factors] + [STABLE_TIE_BREAKER_FACTOR]

    # Add source outcome info to each candidate's source_evidence_summary.
    for candidate in canonical:
        ik = str(candidate["identity_key"])
        so_evidence = candidate_evidence.get(ik, {})
        candidate["source_evidence_summary"]["source_outcome"] = {
            "outcome_class": so_evidence.get("outcome_class", "insufficient data"),
            "ordinal": so_evidence.get("ordinal", NEUTRAL_OUTCOME_ORDINAL),
            "family_outcomes": so_evidence.get("family_outcomes", {}),
            "matched_sufficient_sources": so_evidence.get("matched_sufficient_sources", []),
            "explanation": so_evidence.get("explanation", "No sufficient evidence."),
        }

    # Build diagnostics first, then derive flags from actual findings.
    candidate_quality = _build_candidate_quality_diagnostics(
        canonical=canonical,
        candidate_history=candidate_history,
        suppressed_candidates=suppressed_candidates or [],
        source_mentions=source_mentions or [],
        internal_exclusions=internal_exclusions or [],
        all_current_candidates=all_current_candidates or [],
    )
    _attach_diagnostic_flags_from_findings(canonical, candidate_quality)

    cohort_counts = {cohort: 0 for cohort in OUTCOME_LABELS}
    for candidate in canonical:
        cohort_counts[str(candidate["outcome_cohort"])] += 1

    eligible_count = len(canonical)
    cutoffs = (25, 50)

    metrics = _build_metrics(canonical, shadow_order, eligible_count, cutoffs)
    examples = _build_examples(canonical)
    false_negative_audit = _build_false_negative_audit(canonical)

    return {
        "schema_version": SEARCH_OPTIMIZATION_SCHEMA_VERSION,
        "artifact_type": "search-optimization-review",
        "run_identity": run_date,
        "run_date": run_date,
        "canonical_effect": "none",
        "candidate_count": len(canonical),
        "eligible_count": eligible_count,
        "outcome_cohort_counts": cohort_counts,
        "ordering_method": {
            "kind": "deterministic lexicographic",
            "factors": ordering_factors,
            "opaque_score": False,
            "canonical_constraints_preserved": CANONICAL_CONSTRAINTS,
        },
        "non_effects": [
            "The shadow review does not alter canonical Weekly Report Rows or ordering.",
            "The shadow review does not alter Priority Labels, Candidate History, or Editorial Taste Profile.",
            "The shadow review does not alter configured sources, source quotas, visibility, or suppression behavior.",
        ],
        "metrics": metrics,
        "examples": examples,
        "false_negative_audit": false_negative_audit,
        "candidate_quality_diagnostics": candidate_quality,
        "source_allocation": source_allocation,
        "source_outcome_history_factor": source_outcome_history,
        "candidates": canonical,
    }


def _candidate_comparison(
    candidate: dict[str, Any],
    canonical_rank: int,
    history_by_identity: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    entity = ScoutingCandidate.from_mapping(candidate)
    identity_key = entity.identity_key()
    history = history_by_identity.get(identity_key, {})
    editor_status = str(candidate.get("editor_status") or history.get("editor_status", "")).strip()
    families = list(candidate.get("independent_signal_families") or entity.independent_signal_families())
    primary_signal = candidate.get("primary_signal") or entity.primary_signal()
    evidence = candidate.get("source_evidence", [])
    source_names = sorted(
        {str(item.get("source_name", "")).strip() for item in evidence if str(item.get("source_name", "")).strip()}
    )
    return {
        "identity_key": identity_key,
        "title": str(candidate.get("title", "")),
        "author_display": ", ".join(str(author) for author in candidate.get("primary_authors", [])),
        "primary_authors": list(candidate.get("primary_authors", [])),
        "canonical_rank": canonical_rank,
        "canonical_update_delta": str(candidate.get("update_delta", "unchanged")),
        "canonical_priority_label": str(candidate.get("priority_label", "D")),
        "editor_status": editor_status,
        "outcome_cohort": OUTCOME_COHORTS.get(editor_status, "unreviewed"),
        "candidate_category": str(candidate.get("candidate_category", "")),
        "isbn": str(candidate.get("isbn", "")),
        "independent_signal_families": families,
        "source_evidence_summary": {
            "evidence_count": len(evidence),
            "source_names": source_names,
            "independent_signal_family_count": len(families),
            "strongest_signal_strength": str(primary_signal.get("signal_strength", "")),
        },
        # Full source evidence for diagnostic source-identity extraction.
        "source_evidence": evidence,
    }


def _priority_factor(candidate: dict[str, Any]) -> tuple[int, str]:
    priority_label = str(candidate["canonical_priority_label"])
    return (
        PRIORITY_ORDER.get(priority_label, len(PRIORITY_ORDER)),
        f"Priority Label {priority_label}",
    )


def _family_factor(candidate: dict[str, Any]) -> tuple[int, str]:
    summary = candidate["source_evidence_summary"]
    family_count = int(summary["independent_signal_family_count"])
    return (
        -family_count,
        f"{family_count} Independent Signal {_family_word(family_count)}",
    )


def _strength_factor(candidate: dict[str, Any]) -> tuple[int, str]:
    summary = candidate["source_evidence_summary"]
    strength = str(summary["strongest_signal_strength"]) or "unknown"
    return (
        -ScoutingCandidate.signal_strength_rank({"signal_strength": strength}),
        f"Signal Strength {strength}",
    )


ShadowFactorBuilder = Callable[[dict[str, Any]], tuple[int, str]]
SHADOW_FACTOR_BUILDERS: tuple[tuple[str, ShadowFactorBuilder], ...] = (
    ("canonical Priority Label", _priority_factor),
    ("Independent Signal family count", _family_factor),
    ("strongest Signal Strength", _strength_factor),
)
STABLE_TIE_BREAKER_FACTOR = "canonical rank as the stable tie-breaker"
ORDERING_FACTORS = [
    *(name for name, _builder in SHADOW_FACTOR_BUILDERS),
    STABLE_TIE_BREAKER_FACTOR,
]


def _movement_explanations_with_factors(
    candidate: dict[str, Any],
    canonical: list[dict[str, Any]],
    factors_fn: Callable[[dict[str, Any]], tuple[tuple[int, str], ...]],
) -> list[str]:
    rank_delta = int(candidate["rank_delta"])
    if rank_delta == 0:
        return [_unchanged_explanation_with_factors(candidate, factors_fn)]
    crossed = _crossed_candidates(candidate, canonical)
    direction = "ahead of" if rank_delta > 0 else "behind"
    explanations = [
        (
            f"{'Promoted' if rank_delta > 0 else 'Demoted'} {direction} "
            f"{peer['title']} ({peer['identity_key']}): "
            f"{_canonical_position_explanation(candidate, peer, rank_delta)} "
            f"The shadow order instead compares {_decisive_factor_explanation_with_factors(candidate, peer, factors_fn)}."
        )
        for peer in crossed
    ]
    if len(explanations) > EXPLANATION_COLLAPSE_THRESHOLD:
        decisive_peer = explanations[0]
        remainder = len(explanations) - 1
        decisive_factor = _decisive_factor_explanation_with_factors(candidate, crossed[0], factors_fn)
        return [
            decisive_peer,
            f"Crossed {remainder} additional peer(s); decisive factor: {decisive_factor}.",
        ]
    return explanations


def _crossed_candidates(
    candidate: dict[str, Any],
    canonical: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    canonical_rank = int(candidate["canonical_rank"])
    shadow_rank = int(candidate["shadow_rank"])
    if int(candidate["rank_delta"]) > 0:
        crossed = [
            peer
            for peer in canonical
            if int(peer["canonical_rank"]) < canonical_rank and int(peer["shadow_rank"]) > shadow_rank
        ]
    else:
        crossed = [
            peer
            for peer in canonical
            if int(peer["canonical_rank"]) > canonical_rank and int(peer["shadow_rank"]) < shadow_rank
        ]
    return sorted(crossed, key=lambda peer: int(peer["shadow_rank"]))


def _canonical_position_explanation(
    candidate: dict[str, Any],
    peer: dict[str, Any],
    rank_delta: int,
) -> str:
    candidate_delta = str(candidate["canonical_update_delta"]).replace("_", " ")
    peer_delta = str(peer["canonical_update_delta"]).replace("_", " ")
    if rank_delta > 0:
        return f"The canonical update digest placed the {peer_delta} peer before this {candidate_delta} candidate."
    return f"The canonical update digest placed this {candidate_delta} candidate before the {peer_delta} peer."


def _decisive_factor_explanation_with_factors(
    candidate: dict[str, Any],
    peer: dict[str, Any],
    factors_fn: Callable[[dict[str, Any]], tuple[tuple[int, str], ...]],
) -> str:
    for candidate_factor, peer_factor in zip(
        factors_fn(candidate),
        factors_fn(peer),
        strict=True,
    ):
        candidate_order, candidate_label = candidate_factor
        peer_order, peer_label = peer_factor
        if candidate_order != peer_order:
            relation = "before" if candidate_order < peer_order else "after"
            return f"{candidate_label} ranks {relation} {peer_label}"
    return "the stable canonical tie-breaker"


def _family_word(count: int) -> str:
    return "family" if count == 1 else "families"


def _unchanged_explanation_with_factors(
    candidate: dict[str, Any],
    factors_fn: Callable[[dict[str, Any]], tuple[tuple[int, str], ...]],
) -> str:
    factor_summary = ", ".join(label for _sort_value, label in factors_fn(candidate))
    return f"Position unchanged after comparing {factor_summary}; {STABLE_TIE_BREAKER_FACTOR}."


# ---------------------------------------------------------------------------
# Issue 327 -- aggregate metrics, examples, and false-negative audit
# ---------------------------------------------------------------------------


def _rows_at_cutoff(order: list[dict[str, Any]], cutoff: int) -> list[dict[str, Any]]:
    return order[: min(cutoff, len(order))]


def _median_rank(ranks: list[int]) -> float | None:
    if not ranks:
        return None
    return statistics.median(sorted(ranks))


def _null_rate(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def _eligible_in_cohort(candidates_list: list[dict[str, Any]], cohort: str) -> list[dict[str, Any]]:
    return [c for c in candidates_list if c["outcome_cohort"] == cohort]


def _build_metrics(
    canonical: list[dict[str, Any]],
    shadow_order: list[dict[str, Any]],
    eligible_count: int,
    cutoffs: tuple[int, ...],
) -> dict[str, Any]:
    """Compute all structured metrics for the optimization review."""
    interested_eligible = _eligible_in_cohort(canonical, "strong_interest")
    observed_interested = len(interested_eligible)
    is_exploratory = observed_interested < EVIDENCE_THRESHOLD

    warnings: list[str] = []
    if is_exploratory:
        warnings.append(
            f"Fewer than {EVIDENCE_THRESHOLD} observed Interested candidates "
            f"({observed_interested}); strong-interest-dependent metrics and the "
            f"overall workload conclusion are marked exploratory."
        )

    # Cohort coverage at each cutoff.
    cohort_coverage: dict[str, dict[str, Any]] = {}
    for cutoff in cutoffs:
        cutoff_key = f"rank_{cutoff}"
        canonical_window = _rows_at_cutoff(canonical, cutoff)
        shadow_window = _rows_at_cutoff(shadow_order, cutoff)
        cohort_coverage[cutoff_key] = {
            "canonical": _coverage_for(window=canonical_window, eligible_population=canonical),
            "shadow": _coverage_for(window=shadow_window, eligible_population=canonical),
        }

    # Attach evidence labels to strong-interest coverage at each cutoff.
    for cutoff_key in cohort_coverage:
        for order_name in ("canonical", "shadow"):
            entry = cohort_coverage[cutoff_key][order_name]
            for cohort in VALUABLE_COHORTS:
                c = entry.get(cohort, {})
                if isinstance(c, dict):
                    c["evidence_label"] = (
                        "exploratory" if (is_exploratory and cohort == "strong_interest") else "observational"
                    )

    # Density at each cutoff.
    density: dict[str, dict[str, Any]] = {}
    for cutoff in cutoffs:
        cutoff_key = f"rank_{cutoff}"
        canonical_window = _rows_at_cutoff(canonical, cutoff)
        shadow_window = _rows_at_cutoff(shadow_order, cutoff)
        density[cutoff_key] = {
            "canonical": _density_for(window=canonical_window),
            "shadow": _density_for(window=shadow_window),
        }

    # Source-family diversity at each cutoff.
    diversity: dict[str, dict[str, Any]] = {}
    for cutoff in cutoffs:
        cutoff_key = f"rank_{cutoff}"
        canonical_window = _rows_at_cutoff(canonical, cutoff)
        shadow_window = _rows_at_cutoff(shadow_order, cutoff)
        diversity[cutoff_key] = {
            "canonical": _diversity_for(window=canonical_window),
            "shadow": _diversity_for(window=shadow_window),
        }

    # Median ranks for valuable cohorts.
    median_ranks: dict[str, dict[str, Any]] = {}
    for cohort in VALUABLE_COHORTS:
        eligible = _eligible_in_cohort(canonical, cohort)
        canonical_ranks = [int(c["canonical_rank"]) for c in eligible]
        shadow_ranks = [int(c["shadow_rank"]) for c in eligible]
        mr_entry: dict[str, Any] = {
            "eligible_count": len(eligible),
            "canonical": _median_rank(canonical_ranks),
            "shadow": _median_rank(shadow_ranks),
        }
        mr_entry["evidence_label"] = (
            "exploratory" if (is_exploratory and cohort == "strong_interest") else "observational"
        )
        median_ranks[cohort] = mr_entry

    # Eligible recall - auditable per ordering.
    canonical_identities = {c["identity_key"] for c in canonical}
    shadow_identities = {c["identity_key"] for c in shadow_order}
    sets_identical = canonical_identities == shadow_identities
    eligible_recall = {
        "canonical": {
            "recalled_count": len(canonical_identities),
            "eligible_count": eligible_count,
            "recall_rate": _null_rate(len(canonical_identities), eligible_count),
        },
        "shadow": {
            "recalled_count": len(shadow_identities),
            "eligible_count": eligible_count,
            "recall_rate": _null_rate(len(shadow_identities), eligible_count),
        },
        "identity_sets_identical": sets_identical,
    }

    # Workload.
    workload = _build_workload(canonical, shadow_order, interested_eligible, observed_interested, is_exploratory)

    # Evidence classification.
    evidence_classification: dict[str, Any] = {
        "observed_interested_count": observed_interested,
        "threshold": EVIDENCE_THRESHOLD,
        "is_exploratory": is_exploratory,
    }

    # Add no_interested warning if applicable.
    if observed_interested == 0:
        warnings.append("No Interested candidates observed; workload estimates unavailable.")

    return {
        "eligible_recall": eligible_recall,
        "cohort_coverage": cohort_coverage,
        "density": density,
        "diversity": diversity,
        "median_ranks": median_ranks,
        "workload": workload,
        "evidence_classification": evidence_classification,
        "warnings": warnings,
    }


def _coverage_for(
    *,
    window: list[dict[str, Any]],
    eligible_population: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for cohort in VALUABLE_COHORTS:
        eligible = _eligible_in_cohort(eligible_population, cohort)
        total = len(eligible)
        in_window = sum(1 for c in window if c["outcome_cohort"] == cohort)
        result[cohort] = {
            "count_in_top_cutoff": in_window,
            "total_eligible_in_cohort": total,
            "rate": _null_rate(in_window, total),
        }
    return result


def _density_for(*, window: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    rows_inspected = len(window)
    result: dict[str, dict[str, Any]] = {}
    for cohort in ("negative", "known_workflow_item"):
        count = sum(1 for c in window if c["outcome_cohort"] == cohort)
        result[cohort] = {
            "count_in_top_cutoff": count,
            "rows_inspected": rows_inspected,
            "rate": _null_rate(count, rows_inspected),
        }
    return result


def _diversity_for(*, window: list[dict[str, Any]]) -> dict[str, Any]:
    families: set[str] = set()
    for c in window:
        for family in c.get("independent_signal_families", []):
            if isinstance(family, str) and family.strip():
                families.add(family.strip())
    sorted_families = sorted(families)
    return {
        "family_names": sorted_families,
        "family_count": len(sorted_families),
    }


def _safe_median_delta(canonical_ranks: list[int], shadow_ranks: list[int]) -> float | None:
    """Compute median rank delta, returning None if either list is empty."""
    if not canonical_ranks or not shadow_ranks:
        return None
    c_median = _median_rank(canonical_ranks)
    s_median = _median_rank(shadow_ranks)
    if c_median is None or s_median is None:
        return None
    return s_median - c_median


def _build_workload(
    canonical: list[dict[str, Any]],
    shadow_order: list[dict[str, Any]],
    interested_eligible: list[dict[str, Any]],
    observed_interested: int,
    is_exploratory: bool,
) -> dict[str, Any]:
    """Compute workload estimates.

    Rows needed to retain all observed Interested candidates in each order.
    Estimated rows avoided = canonical - shadow (signed; negative = regression).

    Separate Already Known exposure from discovery quality.
    """
    evidence_label = "exploratory" if is_exploratory else "observational"

    if observed_interested == 0:
        return {
            "canonical_rows_to_retain_all_interested": None,
            "shadow_rows_to_retain_all_interested": None,
            "estimated_rows_avoided": None,
            "evidence_label": evidence_label,
            "already_known_exposure": {
                "note": (
                    "Already Known candidates encountered in the window needed to retain "
                    "all Interested candidates. Additional Already Known rows in the "
                    "canonical window represent rows that the editor must visually skip; "
                    "a lower count in the shadow window means fewer Already Known rows "
                    "are interleaved within the Interested retention window. "
                    "No additional suppression occurs - the shadow review does not alter "
                    "canonical suppression behavior. This is a time-saving effect "
                    "separate from discovery-quality improvements."
                ),
                "canonical_ak_in_interested_window": 0,
                "shadow_ak_in_interested_window": 0,
                "ak_rows_difference": 0,
            },
            "discovery_quality": {
                "note": (
                    "Discovery-quality deltas measure how many more (or fewer) valuable "
                    "candidates appear in the top 25 of each order, plus median-rank "
                    "changes. Median-rank delta is shadow minus canonical: a negative "
                    "value means earlier access (lower rank) in the shadow order; "
                    "a positive value means later access. Fixed-rank coverage delta "
                    "measures the count difference within the top 25."
                ),
                "strong_interest_coverage_delta": None,
                "strong_interest_coverage_delta_evidence_label": evidence_label,
                "actionable_coverage_delta": None,
                "strong_interest_median_rank_delta": None,
                "strong_interest_median_rank_delta_evidence_label": evidence_label,
                "actionable_median_rank_delta": None,
            },
        }

    max_canonical_rank = max(int(c["canonical_rank"]) for c in interested_eligible)
    max_shadow_rank = max(int(c["shadow_rank"]) for c in interested_eligible)
    estimated_rows_avoided = max_canonical_rank - max_shadow_rank

    # Already Known exposure attribution.
    canonical_window = canonical[:max_canonical_rank]
    shadow_window = shadow_order[:max_shadow_rank]
    canonical_ak = sum(1 for c in canonical_window if c["outcome_cohort"] == "known_workflow_item")
    shadow_ak = sum(1 for c in shadow_window if c["outcome_cohort"] == "known_workflow_item")
    ak_rows_difference = canonical_ak - shadow_ak

    # Discovery quality deltas.
    si_canonical_cov = len([c for c in _rows_at_cutoff(canonical, 25) if c["outcome_cohort"] == "strong_interest"])
    si_shadow_cov = len([c for c in _rows_at_cutoff(shadow_order, 25) if c["outcome_cohort"] == "strong_interest"])
    actionable_canonical_cov = len(
        [c for c in _rows_at_cutoff(canonical, 25) if c["outcome_cohort"] == "actionable_follow_up"]
    )
    actionable_shadow_cov = len(
        [c for c in _rows_at_cutoff(shadow_order, 25) if c["outcome_cohort"] == "actionable_follow_up"]
    )

    si_eligible_ranks_canonical = [int(c["canonical_rank"]) for c in _eligible_in_cohort(canonical, "strong_interest")]
    si_eligible_ranks_shadow = [int(c["shadow_rank"]) for c in _eligible_in_cohort(shadow_order, "strong_interest")]
    actionable_eligible_canonical = [
        int(c["canonical_rank"]) for c in _eligible_in_cohort(canonical, "actionable_follow_up")
    ]
    actionable_eligible_shadow = [
        int(c["shadow_rank"]) for c in _eligible_in_cohort(shadow_order, "actionable_follow_up")
    ]

    return {
        "canonical_rows_to_retain_all_interested": max_canonical_rank,
        "shadow_rows_to_retain_all_interested": max_shadow_rank,
        "estimated_rows_avoided": estimated_rows_avoided,
        "evidence_label": evidence_label,
        "already_known_exposure": {
            "note": (
                "Already Known candidates encountered in the window needed to retain "
                "all Interested candidates. Additional Already Known rows in the "
                "canonical window represent rows that the editor must visually skip; "
                "a lower count in the shadow window means fewer Already Known rows "
                "are interleaved within the Interested retention window. "
                "No additional suppression occurs - the shadow review does not alter "
                "canonical suppression behavior. This is a time-saving effect "
                "separate from discovery-quality improvements."
            ),
            "canonical_ak_in_interested_window": canonical_ak,
            "shadow_ak_in_interested_window": shadow_ak,
            "ak_rows_difference": ak_rows_difference,
        },
        "discovery_quality": {
            "note": (
                "Discovery-quality deltas measure how many more (or fewer) valuable "
                "candidates appear in the top 25 of each order, plus median-rank "
                "changes. Median-rank delta is shadow minus canonical: a negative "
                "value means earlier access (lower rank) in the shadow order; "
                "a positive value means later access. Fixed-rank coverage delta "
                "measures the count difference within the top 25."
            ),
            "strong_interest_coverage_delta": si_shadow_cov - si_canonical_cov,
            "strong_interest_coverage_delta_evidence_label": evidence_label,
            "actionable_coverage_delta": actionable_shadow_cov - actionable_canonical_cov,
            "strong_interest_median_rank_delta": _safe_median_delta(
                si_eligible_ranks_canonical, si_eligible_ranks_shadow
            ),
            "strong_interest_median_rank_delta_evidence_label": evidence_label,
            "actionable_median_rank_delta": _safe_median_delta(
                actionable_eligible_canonical, actionable_eligible_shadow
            ),
        },
    }


def _build_examples(canonical: list[dict[str, Any]]) -> dict[str, Any]:
    """Select deterministic representative promoted and demoted candidates.

    Selection policy: absolute rank delta descending, then canonical rank
    ascending, then identity key lexicographic.
    """
    promoted = [c for c in canonical if int(c["rank_delta"]) > 0]
    demoted = [c for c in canonical if int(c["rank_delta"]) < 0]

    # Sort by absolute rank delta descending, then canonical rank ascending,
    # then identity key for deterministic tie-breaking.
    def _example_sort_key(c: dict[str, Any]) -> tuple[int, int, str]:
        return (-abs(int(c["rank_delta"])), int(c["canonical_rank"]), str(c["identity_key"]))

    promoted_sorted = sorted(promoted, key=_example_sort_key)
    demoted_sorted = sorted(demoted, key=_example_sort_key)

    selected_promoted = promoted_sorted[:MAX_EXAMPLE_CAP]
    selected_demoted = demoted_sorted[:MAX_EXAMPLE_CAP]

    return {
        "selection_note": (
            f"Up to {MAX_EXAMPLE_CAP} representative examples per direction, "
            f"selected by absolute rank delta descending, then canonical rank "
            f"ascending, then identity key lexicographic. "
            f"Total promoted: {len(promoted)}, total demoted: {len(demoted)}."
        ),
        "promoted_count": len(promoted),
        "demoted_count": len(demoted),
        "promoted": [
            {
                "identity_key": c["identity_key"],
                "title": c["title"],
                "author_display": c["author_display"],
                "canonical_rank": c["canonical_rank"],
                "shadow_rank": c["shadow_rank"],
                "rank_delta": c["rank_delta"],
                "outcome_cohort": c["outcome_cohort"],
                "explanations": c["explanations"],
            }
            for c in selected_promoted
        ],
        "demoted": [
            {
                "identity_key": c["identity_key"],
                "title": c["title"],
                "author_display": c["author_display"],
                "canonical_rank": c["canonical_rank"],
                "shadow_rank": c["shadow_rank"],
                "rank_delta": c["rank_delta"],
                "outcome_cohort": c["outcome_cohort"],
                "explanations": c["explanations"],
            }
            for c in selected_demoted
        ],
    }


def _build_false_negative_audit(canonical: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Audit every valuable candidate moved downward."""
    return [
        {
            "identity_key": c["identity_key"],
            "title": c["title"],
            "author_display": c["author_display"],
            "canonical_rank": c["canonical_rank"],
            "shadow_rank": c["shadow_rank"],
            "rank_delta": c["rank_delta"],
            "outcome_cohort": c["outcome_cohort"],
            "explanations": c["explanations"],
        }
        for c in canonical
        if c["outcome_cohort"] in VALUABLE_COHORTS and int(c["rank_delta"]) < 0
    ]


# ---------------------------------------------------------------------------
# Issue 328 -- candidate-quality diagnostics
# ---------------------------------------------------------------------------


def _source_identity_from_mention(mention: dict[str, Any]) -> dict[str, str]:
    """Extract deterministic source identity from a source mention or evidence record."""
    return {
        "source_name": str(mention.get("source_name", "")).strip(),
        "source_family": str(mention.get("source_family", "")).strip(),
        "source_url": str(mention.get("source_url", "")).strip(),
        "evidence_url": str(mention.get("evidence_url", "")).strip(),
    }


def _source_identities_from_comparison(c: dict[str, Any]) -> list[dict[str, str]]:
    """Extract deterministic source identities from a candidate comparison record."""
    identities: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for ev in c.get("source_evidence", []):
        sid = _source_identity_from_mention(ev)
        key = (sid["source_name"], sid["source_url"])
        if key not in seen and sid["source_name"]:
            seen.add(key)
            identities.append(sid)
    return identities


def _source_identities_from_mention_list(mentions: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Extract source identities from raw source mentions for a given identity."""
    identities: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for m in mentions:
        sid = _source_identity_from_mention(m)
        key = (sid["source_name"], sid["source_url"])
        if key not in seen and sid["source_name"]:
            seen.add(key)
            identities.append(sid)
    return identities


def _build_candidate_quality_diagnostics(
    *,
    canonical: list[dict[str, Any]],
    candidate_history: list[dict[str, Any]],
    suppressed_candidates: list[dict[str, Any]],
    source_mentions: list[dict[str, Any]],
    internal_exclusions: list[dict[str, Any]],
    all_current_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the candidate-quality diagnostics section."""
    canonical_set = {str(c["identity_key"]): c for c in canonical}

    # Build a lookup: normalized identity key -> list of raw source mentions.
    mention_by_norm: dict[str, list[dict[str, Any]]] = {}
    for m in source_mentions:
        nk = normalized_identity_key(m)
        mention_by_norm.setdefault(nk, []).append(m)

    ak_findings = _detect_already_known_in_normal_review(canonical, candidate_history)
    resurface_findings = _detect_reject_resurface_without_material_change(suppressed_candidates, all_current_candidates)
    duplicate_findings = _detect_probable_duplicate_identity_fragmentation(canonical)
    conflict_findings = _detect_scouting_candidate_definition_conflict(
        source_mentions, internal_exclusions, canonical_set, mention_by_norm
    )
    missing_data_findings = _detect_missing_trial_structured_data(canonical)

    result: dict[str, Any] = {
        "section_non_effects": list(SECTION_NON_EFFECTS),
        "classes": {},
    }
    for class_id in DIAGNOSTIC_CLASS_IDS:
        if class_id == "already_known_in_normal_review":
            findings = ak_findings
        elif class_id == "reject_resurfaced_without_material_change":
            findings = resurface_findings
        elif class_id == "probable_duplicate_identity_fragmentation":
            findings = duplicate_findings
        elif class_id == "scouting_candidate_definition_conflict":
            findings = conflict_findings
        elif class_id == "missing_trial_structured_data":
            findings = missing_data_findings
        else:
            findings = []
        result["classes"][class_id] = {
            "label": DIAGNOSTIC_LABELS[class_id],
            "count": len(findings),
            "rank_effect": DIAGNOSTIC_RANK_EFFECTS[class_id],
            "findings": findings,
        }
    return result


def _history_map(candidate_history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(record.get("identity_key", "")): record
        for record in candidate_history
        if str(record.get("identity_key", "")).strip()
    }


def _detect_already_known_in_normal_review(
    canonical: list[dict[str, Any]],
    candidate_history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Detect Already Known candidates that reached normal editorial review."""
    hist_map = _history_map(candidate_history)
    findings: list[dict[str, Any]] = []
    for c in canonical:
        identity_key = str(c["identity_key"])
        hist = hist_map.get(identity_key, {})
        if str(hist.get("editor_status", "")).strip() == "already_known":
            findings.append(
                {
                    "identity_key": c["identity_key"],
                    "title": c["title"],
                    "author_display": c["author_display"],
                    "primary_authors": c.get("primary_authors", []),
                    "editor_status": "already_known",
                    "canonical_gate": "normal-review",
                    "source_identities": _source_identities_from_comparison(c),
                }
            )
    return findings


def _detect_reject_resurface_without_material_change(
    suppressed_candidates: list[dict[str, Any]],
    all_current_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Detect Reject candidates suppressed without Material Change.

    Enriches from pre-suppression all_current_candidates for exact
    author and source identity evidence.
    """
    suppressed = [sc for sc in suppressed_candidates if str(sc.get("editor_status", "")).strip() == "reject"]
    if not suppressed:
        return []

    # Index pre-suppression candidates by identity key.
    pre_by_id: dict[str, dict[str, Any]] = {}
    for cand in all_current_candidates:
        ik = str(cand.get("identity_key", "")).strip()
        if ik:
            pre_by_id[ik] = cand

    findings: list[dict[str, Any]] = []
    for sc in suppressed:
        identity_key = str(sc.get("identity_key", ""))
        title = str(sc.get("title", ""))
        pre = pre_by_id.get(identity_key, {})
        authors = pre.get("primary_authors") or sc.get("primary_authors", [])
        author_display = ", ".join(str(a) for a in authors) if authors else ""
        source_identities = _source_identities_from_comparison(pre) if pre else []
        findings.append(
            {
                "identity_key": identity_key,
                "title": title,
                "author_display": author_display,
                "primary_authors": list(authors) if isinstance(authors, list) else [],
                "editor_status": "reject",
                "suppression_reason": str(sc.get("suppression_reason", "")),
                "canonical_gate": "candidate-history-suppression",
                "source_identities": source_identities,
            }
        )
    return findings


def _detect_probable_duplicate_identity_fragmentation(
    canonical: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Detect distinct current normal-review identities that may refer to the same work.

    Signal: same normalized title with overlapping but divergent nonempty
    contributor sets -- neither is a subset of the other. Also: distinct
    records sharing a nonblank ISBN.
    """
    by_title: dict[str, list[dict[str, Any]]] = {}
    for c in canonical:
        norm_title = normalize_work_title(str(c["title"]))
        by_title.setdefault(norm_title, []).append(c)

    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen_pairs: set[tuple[str, str]] = set()

    for _title, group in by_title.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a = group[i]
                b = group[j]
                if str(a["identity_key"]) == str(b["identity_key"]):
                    continue
                sorted_pair = sorted([str(a["identity_key"]), str(b["identity_key"])])
                pair_key: tuple[str, str] = (sorted_pair[0], sorted_pair[1])
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)

                # Check ISBN match from comparison record's isbn field.
                isbn_a = normalize_isbn(str(a.get("isbn", "")))
                isbn_b = normalize_isbn(str(b.get("isbn", "")))
                if isbn_a and isbn_b and isbn_a == isbn_b:
                    pairs.append((a, b))
                    continue

                # Check contributor overlap from primary_authors.
                contrib_a = _contributor_set_from_comparison(a)
                contrib_b = _contributor_set_from_comparison(b)
                if contrib_a and contrib_b:
                    overlap = contrib_a & contrib_b
                    if overlap and not contrib_a.issubset(contrib_b) and not contrib_b.issubset(contrib_a):
                        pairs.append((a, b))

    findings: list[dict[str, Any]] = []
    for a, b in pairs:
        identities = [
            {
                "identity_key": a["identity_key"],
                "title": a["title"],
                "author_display": a["author_display"],
                "primary_authors": a.get("primary_authors", []),
                "source_identities": _source_identities_from_comparison(a),
            },
            {
                "identity_key": b["identity_key"],
                "title": b["title"],
                "author_display": b["author_display"],
                "primary_authors": b.get("primary_authors", []),
                "source_identities": _source_identities_from_comparison(b),
            },
        ]
        evidence: dict[str, object] = {
            "reason": "same normalized title with overlapping but divergent contributor sets"
        }
        isbn_a = normalize_isbn(str(a.get("isbn", "")))
        isbn_b = normalize_isbn(str(b.get("isbn", "")))
        if isbn_a and isbn_b and isbn_a == isbn_b:
            evidence["shared_isbn"] = isbn_a
        findings.append(
            {
                "identity_group": identities,
                "evidence": evidence,
                "canonical_gate": "normal-review",
            }
        )
    return findings


def _contributor_set_from_comparison(c: dict[str, Any]) -> set[str]:
    """Build a normalized contributor set from primary_authors."""
    authors = c.get("primary_authors", [])
    if not isinstance(authors, list):
        return set()
    return {normalize_author_identity(normalize_author_display(str(a).strip())) for a in authors if str(a).strip()}


def _detect_scouting_candidate_definition_conflict(
    source_mentions: list[dict[str, Any]],
    internal_exclusions: list[dict[str, Any]],
    canonical_set: dict[str, dict[str, Any]],
    mention_by_norm: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Detect structured category or work-type evidence conflicting with definition.

    Sources: INELIGIBLE_CANDIDATE_CATEGORIES exclusions, and non-book/work
    underlying_work_type from source_mentions.

    Deduplicates: if a candidate_category conflict also appears as an
    underlying_work_type finding for the same identity, keep both as
    separate structured findings (different evidence).
    """
    findings: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str]] = set()

    def _add_finding(key: tuple[str, str, str], finding: dict[str, Any]) -> None:
        if key not in seen_keys:
            seen_keys.add(key)
            findings.append(finding)

    # 1. INELIGIBLE_CANDIDATE_CATEGORIES from internal_exclusions.
    for exc in internal_exclusions:
        reason_raw = str(exc.get("exclusion_reason", "")).strip()
        reason_lower = reason_raw.lower()
        if reason_lower not in INELIGIBLE_CANDIDATE_CATEGORIES:
            continue
        identity_key = str(exc.get("identity_key", ""))
        # Try to get the original candidate_category from source mentions.
        field_value = reason_raw
        orig_mentions = mention_by_norm.get(identity_key, [])
        if orig_mentions:
            cat = str(orig_mentions[0].get("candidate_category", "")).strip()
            if cat:
                field_value = cat
        _add_finding(
            (identity_key, "candidate_category", field_value),
            {
                "identity_key": identity_key,
                "title": str(exc.get("title", "")),
                "exclusion_reason": str(exc.get("exclusion_reason", "")),
                "field": "candidate_category",
                "field_value": field_value,
                "canonical_gate": "category-exclusion",
                "source_identities": _source_identities_from_mention_list(_exclusion_source_mentions(exc)),
            },
        )

    # 2. Non-book/work underlying_work_type from source_mentions.
    for mention in source_mentions:
        work_type_raw = str(mention.get("underlying_work_type", "")).strip()
        if not work_type_raw:
            continue
        work_type_lower = work_type_raw.lower()
        if work_type_lower in {"book", "work"}:
            continue
        title = str(mention.get("title", ""))
        identity_key = normalized_identity_key(mention)
        # Determine canonical gate: if this candidate is excluded, it was gated;
        # otherwise it reached normal review.
        in_normal = identity_key in canonical_set
        gate = "normal-review" if in_normal else "category-exclusion"
        _add_finding(
            (identity_key, "underlying_work_type", work_type_raw),
            {
                "identity_key": identity_key,
                "title": title,
                "exclusion_reason": f"underlying_work_type={work_type_raw}",
                "field": "underlying_work_type",
                "field_value": work_type_raw,
                "canonical_gate": gate,
                "source_identities": [
                    {
                        "source_name": str(mention.get("source_name", "")).strip(),
                        "source_family": str(mention.get("source_family", "")).strip(),
                        "source_url": str(mention.get("source_url", "")).strip(),
                        "evidence_url": str(mention.get("evidence_url", "")).strip(),
                    }
                ],
            },
        )

    return findings


def _exclusion_source_mentions(exc: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert exclusion record evidence back into mention-like dicts for source identity extraction."""
    evidence = exc.get("evidence", [])
    if not isinstance(evidence, list):
        return []
    result: list[dict[str, Any]] = []
    for ev in evidence:
        if isinstance(ev, dict):
            result.append(ev)
    return result


def _detect_missing_trial_structured_data(
    canonical: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Detect missing structured data required to evaluate the trial.

    Flag: candidate_category missing on a normal-review candidate.
    Detected directly from the comparison record's candidate_category field.
    """
    findings: list[dict[str, Any]] = []
    for c in canonical:
        category = str(c.get("candidate_category", "")).strip()
        if not category:
            findings.append(
                {
                    "identity_key": c["identity_key"],
                    "title": c["title"],
                    "author_display": c["author_display"],
                    "primary_authors": c.get("primary_authors", []),
                    "missing_fields": ["candidate_category"],
                    "canonical_gate": "normal-review",
                    "source_identities": _source_identities_from_comparison(c),
                }
            )
    return findings


def _attach_diagnostic_flags_from_findings(
    canonical: list[dict[str, Any]],
    diagnostics: dict[str, Any],
) -> None:
    """Derive diagnostic flags from actual findings for all normal candidates."""
    classes = diagnostics.get("classes", {})

    # Build per-identity-key flag sets from all findings.
    flags_by_id: dict[str, set[str]] = {}

    def _add_flag(ik: str, flag: str) -> None:
        if ik:
            flags_by_id.setdefault(ik, set()).add(flag)

    # Already Known: flagged on each finding's identity.
    for f in classes.get("already_known_in_normal_review", {}).get("findings", []):
        _add_flag(str(f.get("identity_key", "")), "already_known_in_normal_review")

    # Missing data: flagged on each finding's identity.
    for f in classes.get("missing_trial_structured_data", {}).get("findings", []):
        _add_flag(str(f.get("identity_key", "")), "missing_trial_structured_data")

    # Duplicate fragmentation: flag all members of each group.
    for f in classes.get("probable_duplicate_identity_fragmentation", {}).get("findings", []):
        for identity in f.get("identity_group", []):
            _add_flag(str(identity.get("identity_key", "")), "probable_duplicate_identity_fragmentation")

    # Definition conflict: flag normal-review findings.
    for f in classes.get("scouting_candidate_definition_conflict", {}).get("findings", []):
        if str(f.get("canonical_gate", "")) == "normal-review":
            _add_flag(str(f.get("identity_key", "")), "scouting_candidate_definition_conflict")

    # Apply flags to every canonical comparison record.
    for c in canonical:
        ik = str(c.get("identity_key", ""))
        c["diagnostic_flags"] = sorted(flags_by_id.get(ik, set()))


# ---------------------------------------------------------------------------
# Issue 329 -- source allocation recommendations
# ---------------------------------------------------------------------------

# Allocation labels
ALLOCATION_LABELS = ("expand", "keep", "reduce", "exploration-only", "insufficient data")

# Recommendation thresholds
MINIMUM_REVIEWED_THRESHOLD = 10
MEANINGFUL_CONTRIBUTION_THRESHOLD = 0.20
STRONG_OUTCOME_THRESHOLD = 0.40
DEFAULT_PROBATION_WINDOW_WEEKS = 6

# Outcome classes for source shadow factor (family-level collapsed).
# Neutral order value for insufficient data / no sufficient evidence.
NEUTRAL_OUTCOME_ORDINAL = 2
FAMILY_OUTCOME_ORDER: dict[str, int] = {
    "expand": 0,
    "keep": 1,
    "insufficient data": NEUTRAL_OUTCOME_ORDINAL,
    "reduce": 3,
    "exploration-only": 4,
}

ALLOWED_ALLOCATION_LABELS = frozenset(ALLOCATION_LABELS)

ALLOCATION_NON_EFFECTS: list[str] = [
    (
        "Source allocation recommendations never mutate configured sources, "
        "lifecycle status, canonical rankings, editorial state, quotas, or "
        "any other configuration or state file."
    ),
    "Recommendations are read-only diagnostics; no source is added, retired, promoted, or reconfigured.",
    "The optimization review remains opt-in and cannot apply any mutation.",
]

THRESHOLDS_DOC = {
    "minimum_reviewed": MINIMUM_REVIEWED_THRESHOLD,
    "meaningful_contribution": MEANINGFUL_CONTRIBUTION_THRESHOLD,
    "strong_outcome_rate": STRONG_OUTCOME_THRESHOLD,
    "confidence_classes": {
        "insufficient": {"min": 0, "max": 9},
        "moderate": {"min": 10, "max": 24},
        "high": {"min": 25, "max": None},
    },
    "rule_descriptions": {
        "insufficient_data": (
            f"Fewer than {MINIMUM_REVIEWED_THRESHOLD} reviewed candidates -> insufficient data, "
            f"confidence: insufficient."
        ),
        "expand": (
            f"Outcome rate >= {STRONG_OUTCOME_THRESHOLD} AND "
            f"(unique contribution rate >= {MEANINGFUL_CONTRIBUTION_THRESHOLD} "
            f"OR cross-family corroboration rate >= {MEANINGFUL_CONTRIBUTION_THRESHOLD}) -> expand."
        ),
        "keep": (
            f"Any of: outcome rate >= {MEANINGFUL_CONTRIBUTION_THRESHOLD}, "
            f"unique >= {MEANINGFUL_CONTRIBUTION_THRESHOLD}, "
            f"or cross-family corroboration >= {MEANINGFUL_CONTRIBUTION_THRESHOLD} -> keep."
        ),
        "reduce": (
            "Established active source with >=10 reviewed but below keep thresholds, "
            "or completed-probation source with weak evidence -> reduce."
        ),
        "exploration_only_incomplete_probation": (
            "Probation source with incomplete evidence window -> exploration-only (cannot reduce)."
        ),
        "exploration_only_inactive": (
            "Inactive/retired source with sufficient data -> exploration-only (no automatic activation)."
        ),
    },
}


def _confidence_class(reviewed_count: int) -> str:
    if reviewed_count < MINIMUM_REVIEWED_THRESHOLD:
        return "insufficient"
    if reviewed_count >= 25:
        return "high"
    return "moderate"


def _parse_probation_window(
    source: dict[str, Any],
    run_date: str,
) -> dict[str, Any] | None:
    """Parse probation evidence window for a source.

    Returns a structured probation_evidence object or None if not probation.
    """
    lifecycle = source.get("lifecycle_status", "")
    if lifecycle != "probation":
        return None

    started_at_raw = source.get("probation_started_at", "")
    window_weeks = int(source.get("probation_evidence_window_weeks", 0) or DEFAULT_PROBATION_WINDOW_WEEKS)
    started_at = parse_iso_date(started_at_raw) if started_at_raw else None
    run_parsed = parse_iso_date(run_date)

    window_complete = False
    if started_at is not None and run_parsed is not None:
        elapsed = run_parsed - started_at
        window_complete = elapsed >= timedelta(weeks=window_weeks)

    return {
        "probation_started_at": str(started_at_raw or ""),
        "evidence_window_weeks": window_weeks,
        "window_complete": window_complete,
    }


PER_REC_NON_EFFECT = (
    "This recommendation does not alter quotas, configured-source status, "
    "lifecycle state, canonical ranking, or any editorial state."
)


def _build_source_allocation(
    *,
    source_metrics: dict[str, Any] | None,
    run_date: str,
) -> dict[str, Any]:
    """Build structured source allocation recommendations.

    Returns a dict with section_non_effects, thresholds, and recommendations.
    Every configured source in source_metrics maps to exactly one recommendation.
    """
    if source_metrics is None:
        return {
            "section_non_effects": list(ALLOCATION_NON_EFFECTS),
            "thresholds": dict(THRESHOLDS_DOC),
            "allowed_recommendations": list(ALLOCATION_LABELS),
            "recommendations": [],
        }

    source_records = source_metrics.get("metrics", [])

    recommendations: list[dict[str, Any]] = []
    for metric in source_records:
        rec = _build_single_recommendation(metric, run_date)
        recommendations.append(rec)

    # Sort deterministically by source name, then URL.
    recommendations.sort(key=lambda r: (r["source_name"], r["source_url"]))

    return {
        "section_non_effects": list(ALLOCATION_NON_EFFECTS),
        "thresholds": dict(THRESHOLDS_DOC),
        "allowed_recommendations": list(ALLOCATION_LABELS),
        "recommendations": recommendations,
    }


def _build_single_recommendation(
    metric: dict[str, Any],
    run_date: str,
) -> dict[str, Any]:
    """Build a single source allocation recommendation from a metrics record."""
    source_name = str(metric.get("source_name", ""))
    source_url = str(metric.get("source_url", ""))
    family = str(metric.get("family", ""))
    lifecycle_status = str(metric.get("lifecycle_status", "active"))
    language = str(metric.get("language", ""))
    is_active = metric.get("active", True) is not False

    reviewed_count = int(metric.get("reviewed_candidate_count", 0))

    interested_count = int(metric.get("interested_count", 0))
    nda_count = int(metric.get("needs_deeper_analysis_count", 0))
    maybe_watch_count = int(metric.get("maybe_watch_count", 0))
    already_known_count = int(metric.get("already_known_count", 0))
    reject_count = int(metric.get("reject_count", 0))

    unique_rate = metric.get("unique_contribution_rate")

    # Cross-family corroboration: strictly from cross_family fields only.
    cross_corroboration_rate = metric.get("cross_family_corroboration_rate")

    # Confidence class.
    confidence = _confidence_class(reviewed_count)

    # Determine allocation.
    allocation, explanation = _determine_allocation(
        metric=metric,
        reviewed_count=reviewed_count,
        interested_count=interested_count,
        nda_count=nda_count,
        maybe_watch_count=maybe_watch_count,
        unique_rate=unique_rate,
        cross_corroboration_rate=cross_corroboration_rate,
        lifecycle_status=lifecycle_status,
        is_active=is_active,
        confidence=confidence,
        run_date=run_date,
    )

    # Probation evidence.
    probation_evidence = _parse_probation_window(metric, run_date)

    # Exploration retention: every source with active=True, and every probation source.
    exploration_retention: dict[str, Any] | None = None
    if is_active or lifecycle_status == "probation":
        exploration_retention = {
            "retained": True,
            "nonzero": True,
            "reversible": True,
            "text": (
                f"Source '{source_name}' continues exploration. "
                f"This recommendation does not reconfigure or deactivate the source."
            ),
        }

    return {
        "source_name": source_name,
        "source_url": source_url,
        "family": family,
        "lifecycle_status": lifecycle_status,
        "language": language,
        "active": is_active,
        "allocation": allocation,
        "reviewed_count": reviewed_count,
        "interested_count": interested_count,
        "needs_deeper_analysis_count": nda_count,
        "maybe_watch_count": maybe_watch_count,
        "already_known_count": already_known_count,
        "reject_count": reject_count,
        "unique_contribution_rate": unique_rate,
        "cross_family_corroboration_rate": cross_corroboration_rate,
        "confidence_class": confidence,
        "explanation": explanation,
        "canonical_effect": "none",
        "non_effect_statement": PER_REC_NON_EFFECT,
        "probation_evidence": probation_evidence,
        "exploration_retention": exploration_retention,
    }


def _determine_allocation(
    *,
    metric: dict[str, Any],
    reviewed_count: int,
    interested_count: int,
    nda_count: int,
    maybe_watch_count: int,
    unique_rate: float | None,
    cross_corroboration_rate: float | None,
    lifecycle_status: str,
    is_active: bool,
    confidence: str,
    run_date: str,
) -> tuple[str, str]:
    """Determine allocation recommendation for a single source.

    Rules (applied in order):
    1. < MINIMUM_REVIEWED_THRESHOLD reviewed -> insufficient data.
    2. Inactive/retired with sufficient data -> exploration-only (before expand/keep checks).
    3. Incomplete probation with weak evidence -> exploration-only (cannot reduce).
       Incomplete probation with strong evidence -> expand/keep (safe to encourage).
    4. expand: outcome_rate >= STRONG_OUTCOME_THRESHOLD AND (unique >= MEANINGFUL OR cross-corroboration >= MEANINGFUL).
    5. keep: outcome_rate >= MEANINGFUL OR unique >= MEANINGFUL OR cross-corroboration >= MEANINGFUL.
    6. reduce: established active/completed-probation with enough evidence but below keep thresholds.
    7. exploration-only: fallback.
    """
    total_known = reviewed_count

    # Rule 1: Insufficient data.
    if reviewed_count < MINIMUM_REVIEWED_THRESHOLD:
        return (
            "insufficient data",
            f"Only {reviewed_count} reviewed candidates (<{MINIMUM_REVIEWED_THRESHOLD}); "
            f"insufficient data to make a confident allocation recommendation. "
            f"Confidence: insufficient. Keep the source configured and review manually after "
            f"more evidence accumulates.",
        )

    # Compute outcome rate.
    review_worthy = interested_count + nda_count + maybe_watch_count
    outcome_rate_val = rate(review_worthy, total_known) if total_known > 0 else None

    # Parse probation window.
    probation_window = _parse_probation_window(metric, run_date)
    is_probation = lifecycle_status == "probation"
    probation_incomplete = is_probation and probation_window is not None and not probation_window["window_complete"]
    probation_complete = is_probation and probation_window is not None and probation_window["window_complete"]
    is_inactive = lifecycle_status in {"retired", "inactive"} or (not is_active and lifecycle_status == "active")

    # Rule 2: Inactive/retired with sufficient data -> exploration-only.
    if is_inactive or (not is_active and not is_probation):
        return (
            "exploration-only",
            f"Inactive/retired source with sufficient data ({reviewed_count} reviewed). "
            f"No automatic activation or reconfiguration. "
            f"Confidence: {confidence}. Rule: exploration-only (inactive).",
        )

    # Rule 3: Incomplete probation handling.
    if probation_incomplete:
        # Strong evidence -> allow expand/keep (safe).
        if (
            outcome_rate_val is not None
            and outcome_rate_val >= STRONG_OUTCOME_THRESHOLD
            and (_meaningful(unique_rate) or _meaningful(cross_corroboration_rate))
        ):
            return (
                "expand",
                f"Strong outcome rate ({outcome_rate_val:.0%}) with meaningful contribution "
                f"(unique={rate_label(unique_rate)}, cross-family corroboration={rate_label(cross_corroboration_rate)}). "
                f"Probation window not yet complete; expand is safe. "
                f"Confidence: {confidence}. Rule: expand (probation-safe).",
            )
        if _meaningful(outcome_rate_val) or _meaningful(unique_rate) or _meaningful(cross_corroboration_rate):
            return (
                "keep",
                f"Moderate evidence (outcome={rate_label(outcome_rate_val)}, "
                f"unique={rate_label(unique_rate)}, cross-family-corroboration={rate_label(cross_corroboration_rate)}). "
                f"Probation window not yet complete; keep is safe. "
                f"Confidence: {confidence}. Rule: keep (probation-safe).",
            )
        # Weak evidence + incomplete probation -> exploration-only.
        return (
            "exploration-only",
            f"Weak evidence (outcome={rate_label(outcome_rate_val)}, "
            f"unique={rate_label(unique_rate)}, cross-family-corroboration={rate_label(cross_corroboration_rate)}). "
            f"Probation window not yet complete; cannot reduce. "
            f"Confidence: {confidence}. Rule: exploration-only (incomplete probation).",
        )

    # Rule 4: Expand.
    if (
        outcome_rate_val is not None
        and outcome_rate_val >= STRONG_OUTCOME_THRESHOLD
        and (_meaningful(unique_rate) or _meaningful(cross_corroboration_rate))
    ):
        return (
            "expand",
            f"Strong outcome rate ({outcome_rate_val:.0%}) with meaningful contribution "
            f"(unique={rate_label(unique_rate)}, cross-family corroboration={rate_label(cross_corroboration_rate)}). "
            f"Confidence: {confidence}. Rule: expand.",
        )

    # Rule 5: Keep.
    if _meaningful(outcome_rate_val) or _meaningful(unique_rate) or _meaningful(cross_corroboration_rate):
        reasons: list[str] = []
        if _meaningful(outcome_rate_val):
            reasons.append(f"outcome rate {rate_label(outcome_rate_val)}")
        if _meaningful(unique_rate):
            reasons.append(f"unique contribution {rate_label(unique_rate)}")
        if _meaningful(cross_corroboration_rate):
            reasons.append(f"cross-family corroboration {rate_label(cross_corroboration_rate)}")
        return (
            "keep",
            f"Moderate evidence: {'; '.join(reasons)}. Confidence: {confidence}. Rule: keep.",
        )

    # Rule 6: Reduce (established active or completed probation).
    if is_active or probation_complete:
        return (
            "reduce",
            f"Weak evidence (outcome={rate_label(outcome_rate_val)}, "
            f"unique={rate_label(unique_rate)}, cross-family-corroboration={rate_label(cross_corroboration_rate)}). "
            f"Consider reducing collection effort. "
            f"Confidence: {confidence}. Rule: reduce.",
        )

    # Rule 7: Fallback exploration-only.
    return (
        "exploration-only",
        f"Source with sufficient data ({reviewed_count} reviewed) but no positive signal. "
        f"Confidence: {confidence}. Rule: exploration-only.",
    )


def _meaningful(value: float | None) -> bool:
    if value is None:
        return False
    return value >= MEANINGFUL_CONTRIBUTION_THRESHOLD


def _build_source_outcome_history_factor(
    *,
    source_allocation: dict[str, Any],
    canonical: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the source outcome history shadow factor.

    Based on each candidate's actual matched source evidence, not globally
    best recommendation for an entire family. Multiple actual evidence sources
    within one family collapse to one family outcome class. Candidates backed
    only by <10-review sources receive the neutral outcome class.

    Returns structured candidate_evidence map with class/family wording.
    """
    recommendations = source_allocation.get("recommendations", [])

    # Build lookup: source name AND normalized URL -> recommendation + family.
    # Only sources with >= MINIMUM_REVIEWED_THRESHOLD participate.
    source_allocation_map: dict[str, dict[str, Any]] = {}
    for rec in recommendations:
        reviewed = int(rec.get("reviewed_count", 0))
        if reviewed < MINIMUM_REVIEWED_THRESHOLD:
            continue
        name = str(rec.get("source_name", "")).strip()
        url = str(rec.get("source_url", "")).strip()
        if name:
            source_allocation_map[name] = rec
        if url:
            source_allocation_map[url] = rec

    # Build lookup: source identity -> family from recommendataions.
    source_family_map: dict[str, str] = {}
    for rec in recommendations:
        name = str(rec.get("source_name", "")).strip()
        url = str(rec.get("source_url", "")).strip()
        family = str(rec.get("family", "")).strip()
        if name and family:
            source_family_map[name] = family
        if url and family:
            source_family_map[url] = family

    candidate_evidence: dict[str, dict[str, Any]] = {}
    candidate_factors: dict[str, int] = {}

    for c in canonical:
        identity_key = str(c["identity_key"])
        evidence_records = c.get("source_evidence", [])

        # Deduplicate: one outcome per unique evidence source identity.
        seen_source_ids: set[str] = set()
        family_best: dict[str, str] = {}
        matched_sufficient_sources: list[str] = []

        for ev in evidence_records:
            ev_name = str(ev.get("source_name", "")).strip()
            ev_url = str(ev.get("source_url", "")).strip()

            # Match by name, then by URL.
            rec = source_allocation_map.get(ev_name)
            if rec is None and ev_url:
                rec = source_allocation_map.get(ev_url)
            if rec is None:
                continue

            rec_name = str(rec.get("source_name", "")).strip()
            if rec_name in seen_source_ids:
                continue
            seen_source_ids.add(rec_name)
            matched_sufficient_sources.append(rec_name)

            alloc = str(rec.get("allocation", ""))
            family = source_family_map.get(ev_name) or source_family_map.get(ev_url, "")
            if not family:
                continue
            current_best = family_best.get(family)
            if current_best is None:
                family_best[family] = alloc
            else:
                current_ordinal = FAMILY_OUTCOME_ORDER.get(current_best, NEUTRAL_OUTCOME_ORDINAL)
                new_ordinal = FAMILY_OUTCOME_ORDER.get(alloc, NEUTRAL_OUTCOME_ORDINAL)
                if new_ordinal < current_ordinal:
                    family_best[family] = alloc

        # Determine overall outcome class and ordinal.
        if not family_best:
            outcome_class = "insufficient data"
            ordinal = NEUTRAL_OUTCOME_ORDINAL
            explanation = "No sufficient-evidence source backs this candidate; neutral factor."
        else:
            best_ordinal: int | None = None
            best_class = "insufficient data"
            for _family, alloc in family_best.items():
                ordinal = FAMILY_OUTCOME_ORDER.get(alloc, NEUTRAL_OUTCOME_ORDINAL)
                if best_ordinal is None or ordinal < best_ordinal:
                    best_ordinal = ordinal
                    best_class = alloc
            outcome_class = best_class
            ordinal = best_ordinal if best_ordinal is not None else NEUTRAL_OUTCOME_ORDINAL
            family_labels = [f"{f}:{a}" for f, a in sorted(family_best.items())]
            explanation = (
                f"Matched sufficient sources: {', '.join(matched_sufficient_sources)}. "
                f"Family outcomes: {'; '.join(family_labels)}. "
                f"Result: {outcome_class}."
            )

        candidate_factors[identity_key] = ordinal
        candidate_evidence[identity_key] = {
            "outcome_class": outcome_class,
            "ordinal": ordinal,
            "family_outcomes": dict(sorted(family_best.items())),
            "matched_sufficient_sources": matched_sufficient_sources,
            "explanation": explanation,
        }

    return {
        "factor_name": "source outcome history",
        "description": (
            "Per-candidate source outcome factor based on actual matched evidence "
            "sources. Same-family sources collapse to one family outcome. "
            "Only recommendations backed by "
            f">={MINIMUM_REVIEWED_THRESHOLD} reviews participate. "
            f"Candidates without sufficient evidence receive neutral class."
        ),
        "outcome_order": [
            "expand (best)",
            "keep",
            "insufficient data (neutral)",
            "reduce",
            "exploration-only (worst)",
        ],
        "candidate_evidence": candidate_evidence,
        "candidate_factors": candidate_factors,
        "insufficient_sources_neutral": True,
        "neutral_ordinal": NEUTRAL_OUTCOME_ORDINAL,
    }


# ---------------------------------------------------------------------------
# End issue 329
# ---------------------------------------------------------------------------


def render_search_optimization_review(review: Mapping[str, Any] | SearchOptimizationReview) -> str:
    lines = [
        "# Search Optimization Review",
        "",
        f"Run: {review['run_identity']}",
        f"Schema version: {review['schema_version']}",
        "",
        "This is a read-only shadow comparison. It does not change the canonical weekly review or editorial state.",
        "",
        "Shadow ordering is deterministic and lexicographic: "
        + ", then ".join(review.get("ordering_method", {}).get("factors", ORDERING_FACTORS))
        + ".",
        "",
        "---",
        "",
        "## Candidate-Level Comparison",
        "",
        "| Canonical | Shadow | Change | Candidate | Candidate ID | Priority | Outcome | Signals | Source evidence | Explanation |",
        "|---:|---:|---:|---|---|---|---|---|---|---|",
    ]
    for candidate in review["candidates"]:
        delta = int(candidate["rank_delta"])
        delta_label = f"{delta:+d}" if delta else "0"
        families = ", ".join(candidate["independent_signal_families"]) or "none"
        candidate_label = f"{candidate['title']} - {candidate['author_display']}"
        source_summary = candidate["source_evidence_summary"]
        evidence_count = int(source_summary["evidence_count"])
        source_names = ", ".join(source_summary["source_names"]) or "none"
        source_evidence = (
            f"{evidence_count} evidence {'record' if evidence_count == 1 else 'records'}; "
            f"{source_names}; strongest: {source_summary['strongest_signal_strength'] or 'unknown'}"
        )
        lines.append(
            "| "
            + " | ".join(
                markdown_table_cell(value)
                for value in (
                    candidate["canonical_rank"],
                    candidate["shadow_rank"],
                    delta_label,
                    candidate_label,
                    candidate["identity_key"],
                    candidate["canonical_priority_label"],
                    OUTCOME_LABELS[str(candidate["outcome_cohort"])],
                    families,
                    source_evidence,
                    " ".join(candidate["explanations"]),
                )
            )
            + " |"
        )

    # Render aggregated metrics sections.
    metrics = review.get("metrics", {})
    if metrics:
        lines.extend(_render_metrics_markdown(metrics))

    # Render examples.
    examples = review.get("examples", {})
    if examples:
        lines.extend(_render_examples_markdown(examples))

    # Render false-negative audit (always present, may be empty).
    fn_audit = review.get("false_negative_audit")
    if fn_audit is not None:
        lines.extend(_render_false_negative_markdown(fn_audit if isinstance(fn_audit, list) else []))

    # Render candidate-quality diagnostics.
    cq_diag = review.get("candidate_quality_diagnostics")
    if cq_diag is not None:
        lines.extend(_render_candidate_quality_diagnostics(cq_diag))

    # Render source allocation recommendations.
    source_alloc = review.get("source_allocation")
    if source_alloc is not None:
        lines.extend(_render_source_allocation(source_alloc))

    lines.extend(
        [
            "",
            "## What this review does not change",
            "",
            *[f"- {statement}" for statement in review["non_effects"]],
        ]
    )
    return "\n".join(lines) + "\n"


def _render_metrics_markdown(metrics: dict[str, Any]) -> list[str]:
    """Render all metric sections as Markdown lines."""
    lines: list[str] = []

    # Warnings section (first, prominently).
    warnings: list[str] = metrics.get("warnings", [])
    if warnings:
        lines.extend(["", "## Warnings", ""])
        for w in warnings:
            lines.append(f"- {w}")

    # Evidence classification.
    ec = metrics.get("evidence_classification", {})
    lines.extend(
        [
            "",
            "## Evidence Classification",
            "",
            f"Observed Interested candidates: {ec.get('observed_interested_count', 0)} "
            f"(threshold: {ec.get('threshold', EVIDENCE_THRESHOLD)}).",
        ]
    )
    if ec.get("is_exploratory"):
        evidence_label_text = "exploratory"
    else:
        evidence_label_text = "observational"
    lines.extend(
        [
            "",
            f"Evidence classification: **{evidence_label_text}**.",
        ]
    )

    # Eligible recall - per ordering.
    recall = metrics.get("eligible_recall", {})
    canonical_recall = recall.get("canonical", {})
    shadow_recall = recall.get("shadow", {})
    lines.extend(
        [
            "",
            "## Eligible Recall",
            "",
            f"Canonical: {canonical_recall.get('recalled_count', 0)} of "
            f"{canonical_recall.get('eligible_count', 0)} candidates "
            f"(recall rate: {_fmt_rate(canonical_recall.get('recall_rate'))}).",
            f"Shadow: {shadow_recall.get('recalled_count', 0)} of "
            f"{shadow_recall.get('eligible_count', 0)} candidates "
            f"(recall rate: {_fmt_rate(shadow_recall.get('recall_rate'))}).",
            f"Identity sets identical: {recall.get('identity_sets_identical', False)}.",
        ]
    )

    # Cohort coverage.
    coverage = metrics.get("cohort_coverage", {})
    lines.extend(["", "## Cohort Coverage"])
    for cutoff_key in sorted(coverage.keys()):
        data = coverage[cutoff_key]
        lines.extend(
            [
                "",
                f"### At rank cutoff: {cutoff_key.replace('rank_', '')}",
                "",
                "| Cohort | Canonical (in / eligible) | Canonical rate | Shadow (in / eligible) | Shadow rate |",
                "|---|---|---|---|---|",
            ]
        )
        for cohort in VALUABLE_COHORTS:
            c = data.get("canonical", {}).get(cohort, {})
            s = data.get("shadow", {}).get(cohort, {})
            c_label = c.get("evidence_label", "")
            s_label = s.get("evidence_label", "")
            lines.append(
                f"| {_cohort_display(cohort)} "
                f"| {c.get('count_in_top_cutoff', 0)} / {c.get('total_eligible_in_cohort', 0)}"
                f"{_evidence_tag(c_label)} "
                f"| {_fmt_rate(c.get('rate'))} "
                f"| {s.get('count_in_top_cutoff', 0)} / {s.get('total_eligible_in_cohort', 0)}"
                f"{_evidence_tag(s_label)} "
                f"| {_fmt_rate(s.get('rate'))} |"
            )

    # Density.
    density = metrics.get("density", {})
    lines.extend(["", "## Density"])
    for cutoff_key in sorted(density.keys()):
        data = density[cutoff_key]
        lines.extend(
            [
                "",
                f"### At rank cutoff: {cutoff_key.replace('rank_', '')}",
                "",
                "| Cohort | Canonical (in / rows) | Canonical rate | Shadow (in / rows) | Shadow rate |",
                "|---|---|---|---|---|",
            ]
        )
        for cohort in ("negative", "known_workflow_item"):
            c = data.get("canonical", {}).get(cohort, {})
            s = data.get("shadow", {}).get(cohort, {})
            lines.append(
                f"| {_cohort_display(cohort)} "
                f"| {c.get('count_in_top_cutoff', 0)} / {c.get('rows_inspected', 0)} "
                f"| {_fmt_rate(c.get('rate'))} "
                f"| {s.get('count_in_top_cutoff', 0)} / {s.get('rows_inspected', 0)} "
                f"| {_fmt_rate(s.get('rate'))} |"
            )

    # Source-family diversity.
    diversity = metrics.get("diversity", {})
    lines.extend(["", "## Source-Family Diversity"])
    for cutoff_key in sorted(diversity.keys()):
        data = diversity[cutoff_key]
        canonical_families = data.get("canonical", {}).get("family_names", [])
        shadow_families = data.get("shadow", {}).get("family_names", [])
        canonical_count = data.get("canonical", {}).get("family_count", 0)
        shadow_count = data.get("shadow", {}).get("family_count", 0)
        lines.extend(
            [
                "",
                f"### At rank cutoff: {cutoff_key.replace('rank_', '')}",
                "",
                f"Canonical: {canonical_count} families - {', '.join(canonical_families) or 'none'}",
                f"Shadow: {shadow_count} families - {', '.join(shadow_families) or 'none'}",
            ]
        )

    # Median ranks.
    mr = metrics.get("median_ranks", {})
    lines.extend(
        [
            "",
            "## Median Ranks",
            "",
            "| Cohort | Eligible | Canonical median rank | Shadow median rank |",
            "|---|---|---|---|",
        ]
    )
    for cohort in VALUABLE_COHORTS:
        data = mr.get(cohort, {})
        label = data.get("evidence_label", "")
        lines.append(
            f"| {_cohort_display(cohort)}{_evidence_tag(label)} "
            f"| {data.get('eligible_count', 0)} "
            f"| {_fmt_median(data.get('canonical'))} "
            f"| {_fmt_median(data.get('shadow'))} |"
        )

    # Workload.
    wl = metrics.get("workload", {})
    wl_label = wl.get("evidence_label", "")
    lines.extend(
        [
            "",
            "## Workload Estimate",
            "",
        ]
    )
    if wl.get("canonical_rows_to_retain_all_interested") is None:
        lines.append("Workload estimates unavailable (no Interested candidates).")
    else:
        lines.append(
            f"Rows to retain all Interested candidates - Canonical: "
            f"{_fmt_value(wl.get('canonical_rows_to_retain_all_interested'))}, "
            f"Shadow: {_fmt_value(wl.get('shadow_rows_to_retain_all_interested'))}."
        )
        lines.append(
            f"Estimated rows avoided while retaining all Interested: "
            f"{_fmt_signed(wl.get('estimated_rows_avoided'))}.{_evidence_tag(wl_label)}"
        )

    # Already Known exposure.
    ak = wl.get("already_known_exposure", {})
    lines.extend(
        [
            "",
            "### Already Known Exposure",
            "",
            ak.get(
                "note",
                "Already Known counts in the Interested retention window.",
            ),
            "",
            f"Already Known in canonical Interested window: {ak.get('canonical_ak_in_interested_window', 0)}.",
            f"Already Known in shadow Interested window: {ak.get('shadow_ak_in_interested_window', 0)}.",
            f"Already Known rows interleaved change (canonical - shadow): {_fmt_signed(ak.get('ak_rows_difference', 0))}. "
            f"{_ak_direction_word(ak.get('ak_rows_difference', 0))} AK rows in the shadow order.",
        ]
    )

    # Discovery quality.
    dq = wl.get("discovery_quality", {})
    si_cov_label = dq.get("strong_interest_coverage_delta_evidence_label", "")
    si_mr_label = dq.get("strong_interest_median_rank_delta_evidence_label", "")
    lines.extend(
        [
            "",
            "### Discovery Quality",
            "",
            dq.get("note", "Discovery-quality deltas."),
            "",
            f"Strong-interest top-25 coverage delta: "
            f"{_fmt_delta(dq.get('strong_interest_coverage_delta'))}"
            f"{_evidence_tag(si_cov_label)}.",
            f"Actionable top-25 coverage delta: {_fmt_delta(dq.get('actionable_coverage_delta'))}.",
            f"Strong-interest median-rank delta (shadow - canonical): "
            f"{_fmt_delta(dq.get('strong_interest_median_rank_delta'))}"
            f"{_evidence_tag(si_mr_label)}.",
            f"Actionable median-rank delta (shadow - canonical): {_fmt_delta(dq.get('actionable_median_rank_delta'))}.",
        ]
    )

    return lines


def _evidence_tag(label: str) -> str:
    """Format an evidence label for inline Markdown rendering."""
    if not label:
        return ""
    return f" [{label}]"


def _render_examples_markdown(examples: dict[str, Any]) -> list[str]:
    """Render representative promoted/demoted examples as Markdown."""
    lines: list[str] = ["", "## Representative Examples", ""]
    lines.append(examples.get("selection_note", ""))

    promoted = examples.get("promoted", [])
    if promoted:
        lines.extend(
            [
                "",
                "### Promoted Candidates",
                "",
                "| Candidate | ID | Canonical | Shadow | Delta | Cohort | Explanations |",
                "|---|---|---:|---:|---:|---|---|",
            ]
        )
        for ex in promoted:
            label = f"{ex['title']} - {ex['author_display']}"
            lines.append(
                f"| {markdown_table_cell(label)} "
                f"| {markdown_table_cell(ex['identity_key'])} "
                f"| {ex['canonical_rank']} "
                f"| {ex['shadow_rank']} "
                f"| {ex['rank_delta']:+d} "
                f"| {_cohort_display(ex['outcome_cohort'])} "
                f"| {markdown_table_cell(' '.join(ex['explanations']))} |"
            )

    demoted = examples.get("demoted", [])
    if demoted:
        lines.extend(
            [
                "",
                "### Demoted Candidates",
                "",
                "| Candidate | ID | Canonical | Shadow | Delta | Cohort | Explanations |",
                "|---|---|---:|---:|---:|---|---|",
            ]
        )
        for ex in demoted:
            label = f"{ex['title']} - {ex['author_display']}"
            lines.append(
                f"| {markdown_table_cell(label)} "
                f"| {markdown_table_cell(ex['identity_key'])} "
                f"| {ex['canonical_rank']} "
                f"| {ex['shadow_rank']} "
                f"| {ex['rank_delta']:+d} "
                f"| {_cohort_display(ex['outcome_cohort'])} "
                f"| {markdown_table_cell(' '.join(ex['explanations']))} |"
            )

    return lines


def _render_false_negative_markdown(audit: list[dict[str, Any]]) -> list[str]:
    """Render the false-negative audit of valuable candidates moved downward."""
    lines: list[str] = [
        "",
        "## False-Negative Audit",
        "",
        f"Every valuable candidate ({' or '.join(VALUABLE_COHORTS)}) that was moved downward in the shadow order.",
        "",
    ]
    if not audit:
        lines.append("No valuable candidates were moved downward.")
    else:
        lines.append("| Candidate | ID | Canonical | Shadow | Delta | Cohort | Explanations |")
        lines.append("|---|---|---:|---:|---:|---|---|")
        for item in audit:
            label = f"{item['title']} - {item['author_display']}"
            lines.append(
                f"| {markdown_table_cell(label)} "
                f"| {markdown_table_cell(item['identity_key'])} "
                f"| {item['canonical_rank']} "
                f"| {item['shadow_rank']} "
                f"| {item['rank_delta']:+d} "
                f"| {_cohort_display(item['outcome_cohort'])} "
                f"| {markdown_table_cell(' '.join(item['explanations']))} |"
            )
    return lines


def _render_candidate_quality_diagnostics(diagnostics: dict[str, Any]) -> list[str]:
    lines: list[str] = [
        "",
        "## Candidate-Quality Diagnostics",
        "",
    ]

    # Section-wide non-effects.
    section_effects = diagnostics.get("section_non_effects", [])
    if section_effects:
        for effect in section_effects:
            lines.append(f"- {effect}")
        lines.append("")

    classes = diagnostics.get("classes", {})
    for class_id in DIAGNOSTIC_CLASS_IDS:
        class_data = classes.get(class_id, {})
        label = class_data.get("label", class_id)
        count = class_data.get("count", 0)
        rank_effect = class_data.get("rank_effect", "")
        findings = class_data.get("findings", [])

        lines.append(f"### {label}")
        lines.append("")
        lines.append(f"Count: {count}")
        lines.append("")
        lines.append(f"Rank effect: {rank_effect}")
        lines.append("")

        if findings:
            if class_id == "probable_duplicate_identity_fragmentation":
                lines.append("| Identity Key | Title | Author | Reason | Gate | Source Identities |")
                lines.append("|---|---|---|---|---|---|")
                for finding in findings:
                    evidence = finding.get("evidence", {})
                    reason_parts: list[str] = [str(evidence.get("reason", ""))]
                    shared_isbn = str(evidence.get("shared_isbn", "")).strip()
                    if shared_isbn:
                        reason_parts.append(f"shared ISBN: {shared_isbn}")
                    reason_text = "; ".join(r for r in reason_parts if r)
                    gate = str(finding.get("canonical_gate", "normal-review"))
                    for identity in finding.get("identity_group", []):
                        lines.append(
                            f"| {markdown_table_cell(identity['identity_key'])} "
                            f"| {markdown_table_cell(identity['title'])} "
                            f"| {markdown_table_cell(identity['author_display'])} "
                            f"| {markdown_table_cell(reason_text)} "
                            f"| {markdown_table_cell(gate)} "
                            f"| {_format_source_identities_md(identity.get('source_identities', []))} |"
                        )
                lines.append("")
            elif class_id in ("already_known_in_normal_review", "reject_resurfaced_without_material_change"):
                lines.append("| Identity Key | Title | Author | Status | Gate | Source Identities |")
                lines.append("|---|---|---|---|---|---|")
                for finding in findings:
                    lines.append(
                        f"| {markdown_table_cell(finding['identity_key'])} "
                        f"| {markdown_table_cell(finding['title'])} "
                        f"| {markdown_table_cell(finding.get('author_display', ''))} "
                        f"| {markdown_table_cell(finding.get('editor_status', ''))} "
                        f"| {markdown_table_cell(finding.get('canonical_gate', ''))} "
                        f"| {_format_source_identities_md(finding.get('source_identities', []))} |"
                    )
                lines.append("")
            elif class_id == "scouting_candidate_definition_conflict":
                lines.append("| Identity Key | Title | Field | Value | Gate | Source Identities |")
                lines.append("|---|---|---|---|---|---|")
                for finding in findings:
                    lines.append(
                        f"| {markdown_table_cell(finding['identity_key'])} "
                        f"| {markdown_table_cell(finding['title'])} "
                        f"| {markdown_table_cell(finding.get('field', ''))} "
                        f"| {markdown_table_cell(finding.get('field_value', ''))} "
                        f"| {markdown_table_cell(finding.get('canonical_gate', ''))} "
                        f"| {_format_source_identities_md(finding.get('source_identities', []))} |"
                    )
                lines.append("")
            elif class_id == "missing_trial_structured_data":
                lines.append("| Identity Key | Title | Author | Missing Fields | Gate | Source Identities |")
                lines.append("|---|---|---|---|---|---|")
                for finding in findings:
                    lines.append(
                        f"| {markdown_table_cell(finding['identity_key'])} "
                        f"| {markdown_table_cell(finding['title'])} "
                        f"| {markdown_table_cell(finding.get('author_display', ''))} "
                        f"| {markdown_table_cell(', '.join(finding.get('missing_fields', [])))} "
                        f"| {markdown_table_cell(finding.get('canonical_gate', ''))} "
                        f"| {_format_source_identities_md(finding.get('source_identities', []))} |"
                    )
                lines.append("")

    return lines


def _format_source_identities_md(identities: list[dict[str, str]]) -> str:
    """Format source identities as an escaped Markdown string.

    Renders every nonblank structured field: source name, family, URL, evidence URL.
    """
    if not identities:
        return "none"
    parts: list[str] = []
    for sid in identities:
        fields: list[str] = []
        for key in ("source_name", "source_family", "source_url", "evidence_url"):
            value = str(sid.get(key, "")).strip()
            if value:
                fields.append(value)
        parts.append(markdown_table_cell(" | ".join(fields)))
    return "; ".join(parts)


def _render_source_allocation(source_allocation: dict[str, Any]) -> list[str]:
    """Render source allocation recommendations as Markdown."""
    lines: list[str] = [
        "",
        "## Source Allocation Recommendations",
        "",
    ]

    # Section non-effects.
    non_effects = source_allocation.get("section_non_effects", [])
    if non_effects:
        for effect in non_effects:
            lines.append(f"- {effect}")
        lines.append("")

    # Thresholds.
    thresholds = source_allocation.get("thresholds", {})
    if thresholds:
        lines.extend(
            [
                "### Thresholds",
                "",
                f"Minimum reviewed for recommendation: **{thresholds.get('minimum_reviewed', '?')}**",
                f"Meaningful contribution threshold: **{thresholds.get('meaningful_contribution', '?'):.0%}**",
                f"Strong outcome rate threshold: **{thresholds.get('strong_outcome_rate', '?'):.0%}**",
                "",
                "**Confidence classes:**",
            ]
        )
        cc = thresholds.get("confidence_classes", {})
        confidence_order = ("insufficient", "moderate", "high")
        ordered_classes = [(name, cc[name]) for name in confidence_order if name in cc]
        ordered_classes.extend((name, bounds) for name, bounds in cc.items() if name not in confidence_order)
        for name, bounds in ordered_classes:
            lo = bounds.get("min", "?")
            hi = bounds.get("max")
            if hi is not None:
                lines.append(f"- **{name}**: {lo}-{hi} reviewed candidates")
            else:
                lines.append(f"- **{name}**: {lo}+ reviewed candidates")
        lines.append("")

        # Rule descriptions.
        rules = thresholds.get("rule_descriptions", {})
        if rules:
            lines.extend(
                [
                    "**Allocation rules (applied in order):**",
                    "",
                ]
            )
            rule_order = (
                "insufficient_data",
                "expand",
                "keep",
                "reduce",
                "exploration_only_incomplete_probation",
                "exploration_only_inactive",
            )
            ordered_rules = [(name, rules[name]) for name in rule_order if name in rules]
            ordered_rules.extend((name, value) for name, value in rules.items() if name not in rule_order)
            for rule_name, rule_desc in ordered_rules:
                lines.append(f"- **{rule_name}**: {rule_desc}")
            lines.append("")

    # Recommendations table.
    recs = source_allocation.get("recommendations", [])
    if recs:
        lines.extend(
            [
                "### Recommendations",
                "",
                "| Source | Family | Allocation | Reviewed | Int. | NDA | MW | AK | Rej | Unique | Cross-Fam Corrob | Confidence | Lifecycle | Language |",
                "|---:|:---:|:---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for rec in recs:
            lines.append(
                "| "
                + " | ".join(
                    markdown_table_cell(str(value))
                    for value in (
                        rec.get("source_name", ""),
                        rec.get("family", ""),
                        rec.get("allocation", ""),
                        rec.get("reviewed_count", 0),
                        rec.get("interested_count", 0),
                        rec.get("needs_deeper_analysis_count", 0),
                        rec.get("maybe_watch_count", 0),
                        rec.get("already_known_count", 0),
                        rec.get("reject_count", 0),
                        rate_label(rec.get("unique_contribution_rate")),
                        rate_label(rec.get("cross_family_corroboration_rate")),
                        rec.get("confidence_class", ""),
                        rec.get("lifecycle_status", ""),
                        rec.get("language", ""),
                    )
                )
                + " |"
            )
        lines.append("")

        # Detailed explanations.
        lines.extend(
            [
                "### Detailed Explanations",
                "",
            ]
        )
        for rec in recs:
            lines.append(f"**{rec.get('source_name', '')}** ({rec.get('allocation', '')}):")
            lines.append(f"{rec.get('explanation', '')}")
            lines.append(f"Canonical effect: {rec.get('canonical_effect', 'none')}.")
            nse = rec.get("non_effect_statement", "")
            if nse:
                lines.append(f"Non-effect: {nse}")
            # Probation evidence if present.
            pe = rec.get("probation_evidence")
            if pe:
                window_status = "complete" if pe.get("window_complete") else "incomplete"
                lines.append(
                    f"  Probation: started {pe.get('probation_started_at', 'N/A')}, "
                    f"window {pe.get('evidence_window_weeks', '?')} weeks, "
                    f"status: {window_status}."
                )
            # Exploration retention if present.
            er = rec.get("exploration_retention")
            if er:
                lines.append(f"  Exploration: {er.get('text', '')}")
            lines.append("")

    return lines


def _cohort_display(cohort: str) -> str:
    return OUTCOME_LABELS.get(cohort, cohort.replace("_", " ").title())


def _fmt_rate(rate: float | None) -> str:
    if rate is None:
        return "n/a"
    return f"{rate:.3f}"


def _fmt_median(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value == int(value):
        return str(int(value))
    return str(value)


def _fmt_value(value: int | None) -> str:
    if value is None:
        return "n/a"
    return str(value)


def _fmt_signed(value: Any) -> str:
    """Format a signed integer for Markdown display, preserving negatives."""
    if value is None:
        return "N/A"
    if isinstance(value, float) and value == int(value):
        return f"{int(value):+d}"
    if isinstance(value, int):
        return f"{value:+d}"
    return str(value)


def _ak_direction_word(value: Any) -> str:
    """Return a human-readable direction word for an AK rows difference value."""
    if isinstance(value, int | float) and value > 0:
        return "Fewer"
    if isinstance(value, int | float) and value < 0:
        return "More"
    return "No change in"


def _fmt_delta(value: int | float | None) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float) and value == int(value):
        return f"{int(value):+d}"
    if isinstance(value, int):
        return f"{value:+d}"
    return f"{value:+.1f}"


def write_search_optimization_review(output_dir: Path, review: Mapping[str, Any] | SearchOptimizationReview) -> None:
    domain = review if isinstance(review, SearchOptimizationReview) else SearchOptimizationReview.from_mapping(review)
    domain.write(output_dir)


def remove_stale_optimization_artifacts(output_dir: Path) -> None:
    """Remove derived optimization artifacts from a reused output directory.

    Removes only the two Search Optimization Review files (JSON and
    Markdown).  Does not touch canonical artifacts or state.
    """
    store = WeeklyReportStore(output_dir)
    store.path(SEARCH_OPTIMIZATION_JSON_FILENAME).unlink(missing_ok=True)
    store.path(SEARCH_OPTIMIZATION_MD_FILENAME).unlink(missing_ok=True)
