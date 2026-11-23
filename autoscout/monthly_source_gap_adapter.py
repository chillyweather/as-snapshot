"""Adapt the canonical Issue #340 source-gap report for monthly discovery."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from autoscout.monthly_source_discovery_artifacts import MONTHLY_SOURCE_GAP_INSUFFICIENT_METADATA
from autoscout.monthly_source_discovery_artifacts import MONTHLY_SOURCE_GAP_STATUSES
from autoscout.monthly_source_discovery_artifacts import MonthlyInputError
from autoscout.monthly_source_discovery_artifacts import canonical_url


@dataclass(frozen=True)
class SourceGapSnapshot:
    """Validated, typed monthly view of the Issue #340 report."""

    status: str
    coverage_gaps: tuple[dict[str, Any], ...]
    family_coverage: dict[str, dict[str, Any]]
    sources_without_signal_family: tuple[dict[str, Any], ...]
    current_candidate_urls: tuple[str, ...]


def adapt_source_gap_report(
    gap_report: Mapping[str, Any], ledger: Mapping[str, Mapping[str, Any]]
) -> SourceGapSnapshot:
    """Validate and adapt one canonical Issue #340 report."""
    if not isinstance(gap_report, Mapping):
        raise MonthlyInputError("Issue #340 report must be a JSON object")
    required_fields = {"status", "family_coverage", "candidates", "ledger_exclusions", "sources_without_signal_family"}
    missing_fields = required_fields - set(gap_report)
    if missing_fields:
        raise MonthlyInputError("Issue #340 report is missing required fields")
    status = gap_report.get("status")
    if status not in MONTHLY_SOURCE_GAP_STATUSES:
        raise MonthlyInputError(f"Issue #340 discovery returned unsupported status: {status}")

    raw_coverage = gap_report.get("family_coverage", {})
    if not isinstance(raw_coverage, Mapping):
        raise MonthlyInputError("Issue #340 report has invalid family coverage")
    coverage_gaps: list[dict[str, Any]] = []
    family_coverage: dict[str, dict[str, Any]] = {}
    for family, raw_value in raw_coverage.items():
        if not isinstance(family, str) or not isinstance(raw_value, Mapping):
            raise MonthlyInputError("Issue #340 report has invalid family coverage entry")
        configured_count = raw_value.get("configured_source_count")
        active_count = raw_value.get("active_source_count")
        if (
            isinstance(configured_count, bool)
            or not isinstance(configured_count, int)
            or configured_count < 0
            or isinstance(active_count, bool)
            or not isinstance(active_count, int)
            or active_count < 0
        ):
            raise MonthlyInputError("Issue #340 report has invalid family source counts")
        weak = raw_value.get("weak")
        if weak is not True and weak is not False and weak is not None:
            raise MonthlyInputError("Issue #340 report has invalid family weakness")
        raw_reasons = raw_value.get("weakness_reasons", [])
        if not isinstance(raw_reasons, list) or any(not isinstance(reason, str) for reason in raw_reasons):
            raise MonthlyInputError("Issue #340 report has invalid family weakness reasons")
        if status != MONTHLY_SOURCE_GAP_INSUFFICIENT_METADATA and weak is True:
            coverage_gaps.append(
                {
                    "signal_family": family,
                    "configured_source_count": configured_count,
                    "active_source_count": active_count,
                    "weakness_reasons": list(raw_reasons),
                }
            )
        family_coverage[family] = dict(raw_value)

    raw_candidates = gap_report.get("candidates", [])
    raw_exclusions = gap_report.get("ledger_exclusions", [])
    if not isinstance(raw_candidates, list) or not isinstance(raw_exclusions, list):
        raise MonthlyInputError("Issue #340 report has invalid candidate lists")
    candidate_urls: set[str] = set()
    for candidate in raw_candidates:
        if not isinstance(candidate, Mapping):
            raise MonthlyInputError("Issue #340 candidate is not an object")
        candidate_urls.add(canonical_url(candidate.get("url"), MonthlyInputError, "Issue #340 candidate"))
    for exclusion in raw_exclusions:
        if not isinstance(exclusion, Mapping):
            raise MonthlyInputError("Issue #340 ledger exclusion is not an object")
        url = canonical_url(exclusion.get("url"), MonthlyInputError, "Issue #340 ledger exclusion")
        if url not in ledger:
            raise MonthlyInputError("Issue #340 ledger exclusion is absent from the canonical ledger")

    raw_unclassified = gap_report.get("sources_without_signal_family", [])
    if not isinstance(raw_unclassified, list) or any(not isinstance(source, Mapping) for source in raw_unclassified):
        raise MonthlyInputError("Issue #340 report has invalid unclassified-source fields")
    unclassified = tuple(dict(source) for source in raw_unclassified)
    coverage_gaps.sort(key=lambda gap: str(gap["signal_family"]))
    return SourceGapSnapshot(
        status=str(status),
        coverage_gaps=tuple(coverage_gaps),
        family_coverage=family_coverage,
        sources_without_signal_family=unclassified,
        current_candidate_urls=tuple(sorted(candidate_urls)),
    )
