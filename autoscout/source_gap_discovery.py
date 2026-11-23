"""Deterministic discovery of thin source signal families.

The default discovery operation is offline and read-only.
An explicit probe operation delegates the remaining batch to the blessed
``./scripts/probe-source`` launcher and records that launcher's result.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from collections.abc import Collection
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Literal

from autoscout.config import validate_config
from autoscout.repository_root import repository_root as _repository_root
from autoscout.search_optimization import ALLOWED_ALLOCATION_LABELS
from autoscout.search_optimization import SearchOptimizationReview
from autoscout.source_probe import DEFAULT_FEASIBILITY_DIR
from autoscout.source_probe import DEFAULT_LEDGER_PATH
from autoscout.source_probe import normalized_url
from autoscout.source_probe import valid_http_url
from autoscout.source_rules import DISCOVERY_SOURCE_FAMILIES
from autoscout.source_rules import is_active_source

SOURCE_GAP_ARTIFACT_TYPE = "source-gap-discovery"
SOURCE_GAP_SCHEMA_VERSION = 1
NOT_REVIEWED_ALLOCATION = "not-reviewed"
CANONICAL_SIGNAL_FAMILIES = tuple(sorted(DISCOVERY_SOURCE_FAMILIES))
DEFAULT_CAPTURE_DIR = Path("tmp/source-candidate-captures")


@dataclass(frozen=True)
class SourceGapSeed:
    """One repository-grounded, not-yet-configured source candidate."""

    url: str
    name: str
    signal_family: str
    reason: str


# These seeds are limited to exact URLs named by repository requirements or
# research.
# Later feasibility documents explicitly reject or defer the older broad seeds,
# so those URLs are intentionally absent.
SOURCE_GAP_SEED_CATALOG: tuple[SourceGapSeed, ...] = (
    SourceGapSeed(
        "https://www.pulitzer.org/prize-winners-by-category/223",
        "Pulitzer Prize General Nonfiction",
        "award",
        "Repository requirements list the Pulitzer General Nonfiction category.",
    ),
    SourceGapSeed(
        "https://royalsociety.org/medals-and-prizes/science-book-prize/",
        "Royal Society Trivedi Science Book Prize",
        "award",
        "Repository requirements list the Royal Society science book prize.",
    ),
    SourceGapSeed(
        "https://www.casadellibro.com/libros-mas-vendidos",
        "Casa del Libro Más vendidos",
        "bestseller/ranking",
        "Repository requirements list the Casa del Libro bestseller ranking.",
    ),
    SourceGapSeed(
        "https://www.books.com.tw/web/sys_saletopb/books/",
        "Books.com.tw Bestseller",
        "bestseller/ranking",
        "Repository requirements list the Books.com.tw bestseller page.",
    ),
    SourceGapSeed(
        "https://www.mckinsey.com/featured-insights/mckinsey-on-books/author-talks",
        "McKinsey Author Talks",
        "media/curated list",
        "Repository requirements list McKinsey Author Talks as a book signal.",
    ),
    SourceGapSeed(
        "https://www.scientificamerican.com/books/",
        "Scientific American Books",
        "media/curated list",
        "Repository requirements list the Scientific American Books page.",
    ),
    SourceGapSeed(
        "https://app.thestorygraph.com/",
        "StoryGraph",
        "reader-interest",
        "Repository requirements list StoryGraph reader signals.",
    ),
    SourceGapSeed(
        "https://www.librarything.com/zeitgeist",
        "LibraryThing Zeitgeist",
        "reader-interest",
        "Repository requirements list LibraryThing Zeitgeist reader signals.",
    ),
    SourceGapSeed(
        "https://www.audible.com/charts/best/money-finance-audiobooks/18574547011",
        "Audible Bestselling Money & Finance Audiobooks",
        "audiobook ranking",
        "Repository research records this exact Audible Money & Finance chart.",
    ),
    SourceGapSeed(
        "https://www.audible.com/charts/best/health-wellness-audiobooks/18573370011",
        "Audible Bestselling Health & Wellness Audiobooks",
        "audiobook ranking",
        "Repository research records this exact Audible Health & Wellness chart.",
    ),
    SourceGapSeed(
        "https://www.publishersweekly.com/pw/by-topic/industry-news/book-deals/index.html",
        "Publishers Weekly Book Deals",
        "rights/deal",
        "Repository requirements list Publishers Weekly Book Deals.",
    ),
)

ProbeRunner = Callable[[Sequence[str]], tuple[int, str]]


class SourceGapProbeError(ValueError):
    """The blessed probe returned an unusable process result."""

    def __init__(self, message: str, command: Sequence[str]) -> None:
        super().__init__(message)
        self.command = tuple(command)


def run_probe_launcher(command: Sequence[str]) -> tuple[int, str]:
    """Run the repository probe launcher from the module's repository root.

    This is the injectable public runner seam for live discovery tests.
    The launcher, rather than Python probe internals, owns all network and
    probe-output behavior.
    """
    repository_root = _repository_root()
    completed = subprocess.run(
        list(command),
        cwd=repository_root,
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    return completed.returncode, completed.stdout


def source_gap_error_report(
    error: str,
    requested_signal_family: str | None = None,
    *,
    probe: Mapping[str, Any] | None = None,
    error_kind: Literal["input_error", "probe_error"] = "input_error",
) -> dict[str, Any]:
    """Build the structured discovery error envelope."""
    probe_mapping = dict(probe) if probe is not None else {"status": "not-run", "command": []}
    next_action = (
        "Inspect the probe failure and run the explicit discovery probe again."
        if error_kind == "probe_error"
        else "Correct the portfolio, review, ledger, or command input and run discovery again."
    )
    return {
        "artifact_type": SOURCE_GAP_ARTIFACT_TYPE,
        "version": SOURCE_GAP_SCHEMA_VERSION,
        "status": error_kind,
        "requested_signal_family": requested_signal_family,
        "family_coverage": {},
        "sources_without_signal_family": [],
        "candidates": [],
        "ledger_exclusions": [],
        "probe": probe_mapping,
        "next_action": next_action,
        "error": error.strip() or error_kind.replace("_", " "),
    }


def _normalise_requested_family(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    family = value.strip().lower()
    if family not in DISCOVERY_SOURCE_FAMILIES:
        supported = ", ".join(CANONICAL_SIGNAL_FAMILIES)
        raise ValueError(f"unsupported signal family '{value}'; expected one of: {supported}")
    return family


def _unclassified_source_view(source: Mapping[str, Any], index: int) -> dict[str, Any]:
    """Build one bounded, deterministic view for a source without a canonical family."""
    name_value = source.get("name")
    name = name_value.strip() if isinstance(name_value, str) and name_value.strip() else f"source[{index}]"
    family_value = source.get("family")
    entry: dict[str, Any] = {
        "name": name,
        "active": source.get("active", True) if isinstance(source.get("active", True), bool) else True,
    }
    url_value = source.get("url")
    if isinstance(url_value, str) and url_value.strip():
        entry["url"] = url_value
    if isinstance(family_value, str) and family_value.strip():
        declared_family = family_value.strip()
        entry["declared_family"] = declared_family
        entry["reason"] = f"unsupported signal family '{declared_family}'"
    elif "family" not in source or family_value is None or (isinstance(family_value, str) and not family_value.strip()):
        entry["reason"] = "family metadata absent"
    else:
        entry["reason"] = f"unsupported signal family metadata type '{type(family_value).__name__}'"
    return entry


def _validated_portfolio(portfolio: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(portfolio, Mapping):
        raise ValueError("portfolio must be a JSON object")
    sources = portfolio.get("sources")
    if not isinstance(sources, list):
        raise ValueError("portfolio sources must be a list")
    validation_errors = validate_config(dict(portfolio))
    if validation_errors:
        raise ValueError(f"portfolio validation failed: {'; '.join(validation_errors)}")
    if any(not isinstance(source, Mapping) for source in sources):
        for index, source in enumerate(sources):
            if not isinstance(source, Mapping):
                raise ValueError(f"portfolio source[{index}] must be a JSON object")

    validated: list[dict[str, Any]] = []
    unclassified: list[dict[str, Any]] = []
    names: set[str] = set()
    identities: set[str] = set()
    for index, raw_source in enumerate(sources):
        assert isinstance(raw_source, Mapping)
        family = raw_source.get("family")
        if not isinstance(family, str) or family.strip().lower() not in DISCOVERY_SOURCE_FAMILIES:
            unclassified.append(_unclassified_source_view(raw_source, index))
            continue
        name = raw_source.get("name")
        url = raw_source.get("url")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"portfolio source[{index}] must have a nonblank name")
        if not valid_http_url(url):
            raise ValueError(f"portfolio source[{index}] must have a valid HTTP or HTTPS URL")
        url_text = str(url)
        active = raw_source.get("active", True)
        if not isinstance(active, bool):
            raise ValueError(f"portfolio source[{index}] active must be a boolean")
        clean_name = name.strip()
        clean_family = family.strip().lower()
        identity = normalized_url(url_text)
        if clean_name in names:
            raise ValueError(f"portfolio has duplicate source name '{clean_name}'")
        if identity in identities:
            raise ValueError(f"portfolio has duplicate source URL '{url}'")
        names.add(clean_name)
        identities.add(identity)
        validated.append(
            {
                "name": clean_name,
                "family": clean_family,
                "url": url_text,
                "active": active,
            }
        )
    unclassified.sort(
        key=lambda item: (
            str(item.get("name", "")).casefold(),
            str(item.get("url", "")),
            str(item.get("declared_family", "")).casefold(),
            str(item.get("active", "")),
            str(item["reason"]),
        )
    )
    return validated, unclassified


def _validated_review(review: Mapping[str, Any] | SearchOptimizationReview) -> SearchOptimizationReview:
    if isinstance(review, SearchOptimizationReview):
        return review
    if not isinstance(review, Mapping):
        raise ValueError("Search Optimization Review must be a JSON object")
    return SearchOptimizationReview.from_mapping(review)


def _validated_ledger_identities(value: Collection[str] | None) -> set[str]:
    """Accept only the normalized identity collection returned by the ledger reader."""
    if value is None:
        return set()
    if isinstance(value, str | bytes | Mapping):
        raise ValueError("ledger identities must be a collection of strings")
    try:
        identities = set(value)
    except TypeError as exc:
        raise ValueError("ledger identities must be a collection of strings") from exc
    if any(not isinstance(identity, str) or not identity.strip() for identity in identities):
        raise ValueError("ledger identities must contain only nonblank strings")
    return identities


def _allocation_by_source(review: SearchOptimizationReview) -> dict[tuple[str, str, str], str]:
    outcome = review.comparison_outcome()
    allocations: dict[tuple[str, str, str], str] = {}
    for recommendation in outcome.source_recommendations:
        name = recommendation.source_name.strip()
        url = recommendation.source_url
        family = recommendation.family.strip().lower()
        allocation = recommendation.allocation
        if not name or not valid_http_url(url) or family not in DISCOVERY_SOURCE_FAMILIES:
            raise ValueError("Search Optimization Review has an invalid source allocation identity")
        if allocation not in ALLOWED_ALLOCATION_LABELS:
            raise ValueError(f"Search Optimization Review has unsupported allocation '{allocation}'")
        identity = (name, normalized_url(url), family)
        if identity in allocations:
            raise ValueError("Search Optimization Review has duplicate source allocation identity")
        allocations[identity] = allocation
    return allocations


def _allocation_for_source(
    source: Mapping[str, Any],
    allocations: Mapping[tuple[str, str, str], str],
) -> str:
    identity = (str(source["name"]), normalized_url(str(source["url"])), str(source["family"]))
    return allocations.get(identity, NOT_REVIEWED_ALLOCATION)


def _source_view(source: Mapping[str, Any], allocation: str) -> dict[str, Any]:
    return {
        "name": source["name"],
        "url": source["url"],
        "active": source["active"],
        "allocation": allocation,
    }


def _weakness_reasons(active_count: int, allocation_labels: Sequence[str]) -> list[str]:
    reasons: list[str] = []
    if active_count == 0:
        reasons.append("no active source")
    elif active_count == 1:
        reasons.append("one active source")
    reduce_count = allocation_labels.count("reduce")
    if allocation_labels and reduce_count * 2 > len(allocation_labels):
        reasons.append(f"{reduce_count} of {len(allocation_labels)} active reviewed sources have reduce allocation")
    return reasons


def _family_coverage(
    family: str,
    sources: Sequence[Mapping[str, Any]],
    allocations: Mapping[tuple[str, str, str], str],
) -> dict[str, Any]:
    family_sources = [source for source in sources if source["family"] == family]
    views = [
        _source_view(source, _allocation_for_source(source, allocations))
        for source in sorted(family_sources, key=lambda item: (str(item["name"]).casefold(), str(item["url"])))
    ]
    allocation_counts = {label: 0 for label in (*sorted(ALLOWED_ALLOCATION_LABELS), NOT_REVIEWED_ALLOCATION)}
    for view in views:
        allocation_counts[str(view["allocation"])] += 1
    active_sources = [source for source in family_sources if is_active_source(dict(source))]
    active_count = len(active_sources)
    available_labels: list[str] = []
    for active_source in active_sources:
        allocation = _allocation_for_source(active_source, allocations)
        if allocation in ALLOWED_ALLOCATION_LABELS:
            available_labels.append(allocation)
    reasons = _weakness_reasons(active_count, available_labels)
    return {
        "signal_family": family,
        "configured_source_count": len(family_sources),
        "active_source_count": active_count,
        "sources": views,
        "allocation_counts": allocation_counts,
        "weak": bool(reasons),
        "weakness_reasons": reasons,
    }


def _metadata_insufficiency_reason(
    sources: Sequence[Mapping[str, Any]], unclassified: Sequence[Mapping[str, Any]]
) -> str | None:
    if not unclassified:
        return None
    absent_count = sum(1 for source in unclassified if source.get("reason") == "family metadata absent")
    if absent_count:
        label = "source" if absent_count == 1 else "sources"
        return f"family metadata absent for {absent_count} configured {label}"
    if not sources:
        return "no configured source has a canonical signal family"
    return None


def _seed_mapping(seed: SourceGapSeed, reason: str) -> dict[str, str]:
    return {
        "url": seed.url,
        "name": seed.name,
        "signal_family": seed.signal_family,
        "reason": reason,
    }


def _probe_command(candidates: Sequence[Mapping[str, str]], ledger_path: Path) -> list[str]:
    command = ["./scripts/probe-source"]
    for candidate in candidates:
        command.extend(
            [
                "--url",
                candidate["url"],
                "--name",
                candidate["name"],
                "--signal-family",
                candidate["signal_family"],
                "--proposing-context",
                candidate["reason"],
            ]
        )
    command.extend(
        [
            "--keep-captures",
            str(DEFAULT_CAPTURE_DIR),
            "--ledger",
            str(ledger_path),
            "--feasibility-dir",
            str(DEFAULT_FEASIBILITY_DIR),
        ]
    )
    return command


def _planned_probe(candidates: Sequence[Mapping[str, str]], ledger_path: Path) -> dict[str, Any]:
    if not candidates:
        return {"status": "no-candidates", "command": []}
    return {"status": "planned", "command": _probe_command(candidates, ledger_path)}


def _execute_probe(command: Sequence[str], runner: ProbeRunner) -> dict[str, Any]:
    try:
        execution = runner(command)
        if not isinstance(execution, tuple) or len(execution) != 2:
            raise TypeError("probe runner must return exactly (exit_code, stdout)")
        exit_code, stdout = execution
        if isinstance(exit_code, bool) or not isinstance(exit_code, int) or not isinstance(stdout, str):
            raise TypeError("probe runner must return exactly (int exit_code, str stdout)")
    except (OSError, RuntimeError, subprocess.SubprocessError, TypeError, ValueError) as exc:
        raise SourceGapProbeError(f"probe launcher failed: {exc}", command) from exc
    if exit_code not in {0, 1, 2, 3}:
        raise SourceGapProbeError(f"probe launcher returned unsupported exit code {exit_code}", command)
    try:
        probe_result = json.loads(stdout)
    except (json.JSONDecodeError, TypeError, UnicodeError) as exc:
        raise SourceGapProbeError(f"probe launcher returned malformed JSON: {exc}", command) from exc
    if not isinstance(probe_result, Mapping):
        raise SourceGapProbeError("probe launcher returned a JSON value that is not an object", command)
    structured_exit_code = probe_result.get("exit_code")
    if isinstance(structured_exit_code, bool) or not isinstance(structured_exit_code, int):
        raise SourceGapProbeError("probe launcher report has no valid exit_code", command)
    if structured_exit_code != exit_code:
        raise SourceGapProbeError(
            f"probe launcher report exit_code {structured_exit_code} disagrees with process exit code {exit_code}",
            command,
        )
    return dict(probe_result)


def discover_source_gaps(
    portfolio: Mapping[str, Any],
    review: Mapping[str, Any] | SearchOptimizationReview | None = None,
    ledger_identities: Collection[str] | None = None,
    signal_family: str | None = None,
    *,
    ledger_path: Path | None = None,
    probe: bool = False,
) -> dict[str, Any]:
    """Build a source-gap report and optionally run its explicit probe batch.

    Without *probe*, this function is deterministic, offline, and read-only.
    With *probe*, it delegates to the public blessed launcher adapter.
    """
    requested_family = _normalise_requested_family(signal_family)
    sources, sources_without_signal_family = _validated_portfolio(portfolio)
    allocations = {} if review is None else _allocation_by_source(_validated_review(review))
    known_ledger_identities = _validated_ledger_identities(ledger_identities)
    families = [requested_family] if requested_family is not None else list(CANONICAL_SIGNAL_FAMILIES)

    metadata_reason = _metadata_insufficiency_reason(sources, sources_without_signal_family)
    coverage = {family: _family_coverage(family, sources, allocations) for family in families}
    if metadata_reason is not None:
        for family_coverage in coverage.values():
            family_coverage["weak"] = None
            family_coverage["weakness_reasons"] = [metadata_reason]
    weak_families = {family for family, value in coverage.items() if value["weak"] is True}
    raw_sources = portfolio.get("sources", [])
    configured_identities = {
        normalized_url(str(raw_source["url"]))
        for raw_source in raw_sources
        if isinstance(raw_source, Mapping) and valid_http_url(raw_source.get("url"))
    }
    candidates: list[dict[str, str]] = []
    ledger_exclusions: list[dict[str, str]] = []
    seen_seed_identities: set[str] = set()
    if metadata_reason is None:
        for seed in SOURCE_GAP_SEED_CATALOG:
            if seed.signal_family not in weak_families:
                continue
            identity = normalized_url(seed.url)
            if identity in seen_seed_identities:
                continue
            seen_seed_identities.add(identity)
            if identity in configured_identities:
                continue
            coverage_for_seed = coverage[seed.signal_family]
            reason = f"Family gap: {coverage_for_seed['weakness_reasons'][0]}. Research basis: {seed.reason}"
            if identity in known_ledger_identities:
                ledger_exclusions.append(_seed_mapping(seed, "already recorded in Source Candidate Ledger"))
            else:
                candidates.append(_seed_mapping(seed, reason))

    effective_ledger_path = ledger_path or DEFAULT_LEDGER_PATH
    report_status = "insufficient_metadata" if metadata_reason is not None else "success"
    if metadata_reason is not None:
        next_action = "Supply canonical signal-family metadata for the unclassified configured sources or provide a resolvable weekly portfolio reference, then run discovery again."
    elif not probe:
        next_action = "Present the offline report. Probe only after a separate explicit editor request with ./scripts/add-source discover --probe. Then wait for the editor to choose exactly one accepted candidate before onboarding."
    elif not candidates:
        next_action = "No candidates remain after filtering, so no probe was run. Wait for the editor to choose exactly one accepted candidate before onboarding."
    else:
        next_action = "The explicit probe is complete. Wait for the editor to choose exactly one accepted candidate before onboarding."
    report: dict[str, Any] = {
        "artifact_type": SOURCE_GAP_ARTIFACT_TYPE,
        "version": SOURCE_GAP_SCHEMA_VERSION,
        "status": report_status,
        "requested_signal_family": requested_family,
        "family_coverage": coverage,
        "sources_without_signal_family": sources_without_signal_family,
        "candidates": candidates,
        "ledger_exclusions": ledger_exclusions,
        "probe": _planned_probe(candidates, effective_ledger_path),
        "next_action": next_action,
    }
    if not probe or not candidates:
        return report

    probe_result = _execute_probe(report["probe"]["command"], run_probe_launcher)
    probe_exit_code = int(probe_result["exit_code"])
    report["probe"] = {
        "status": "completed" if probe_exit_code in {0, 1, 3} else "error",
        "command": list(report["probe"]["command"]),
        "result": probe_result,
        "exit_code": probe_exit_code,
    }
    if probe_exit_code == 2:
        report["status"] = "probe_error"
        report["next_action"] = "Inspect the probe failure and run the explicit discovery probe again."
    return report


__all__ = [
    "CANONICAL_SIGNAL_FAMILIES",
    "DEFAULT_CAPTURE_DIR",
    "DEFAULT_FEASIBILITY_DIR",
    "DEFAULT_LEDGER_PATH",
    "NOT_REVIEWED_ALLOCATION",
    "SOURCE_GAP_ARTIFACT_TYPE",
    "SOURCE_GAP_SCHEMA_VERSION",
    "SOURCE_GAP_SEED_CATALOG",
    "SourceGapProbeError",
    "discover_source_gaps",
    "run_probe_launcher",
    "source_gap_error_report",
]
