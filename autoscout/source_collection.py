from __future__ import annotations

import hashlib
import json
import math
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any
from typing import assert_never

from autoscout import pm_deal_capture
from autoscout import pw_issue_capture
from autoscout.artifact_freshness import ArtifactFreshness
from autoscout.artifact_freshness import is_stale_artifact_freshness
from autoscout.artifact_freshness import normalized_artifact_freshness
from autoscout.capture_bounds import DEFAULT_CAPTURE_MAX_RESPONSE_BYTES
from autoscout.capture_bounds import CaptureKind
from autoscout.capture_bounds import CaptureWriteError
from autoscout.capture_bounds import retain_ranking_detail_pages
from autoscout.capture_bounds import write_bounded_capture
from autoscout.capture_deadline import CAPTURE_DEADLINE_KEY
from autoscout.capture_deadline import CaptureDeadline
from autoscout.collection_schedule import CARRIED_FROM_FIELD
from autoscout.collection_schedule import CARRIED_FROM_NOTHING
from autoscout.collection_schedule import CARRIED_FROM_PREVIOUS_OUTPUT
from autoscout.collection_schedule import CARRIED_FROM_RECORDED_LIST
from autoscout.collection_schedule import CARRIED_LIST_DATE_FIELD
from autoscout.collection_schedule import CARRIED_MENTION_COUNT_FIELD
from autoscout.collection_schedule import NEXT_DUE_DATE_FIELD
from autoscout.collection_schedule import WORK_ITEM_POSITION_FIELD
from autoscout.collection_schedule import carried_forward_mentions
from autoscout.collection_schedule import collection_weekday
from autoscout.collection_schedule import declared_check_frequency
from autoscout.collection_schedule import is_not_due_entry
from autoscout.collection_schedule import is_source_due
from autoscout.collection_schedule import not_due_source_entry
from autoscout.collection_schedule import source_check_frequency
from autoscout.config import resolve_weekly_portfolio
from autoscout.helper_metadata import load_helper_metadata
from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.progress_snapshot import PROGRESS_HEARTBEAT_INTERVAL_SECONDS
from autoscout.progress_snapshot import ProgressSnapshotFailureRecorder
from autoscout.progress_snapshot import ProgressSnapshotPresentation
from autoscout.progress_snapshot import ProgressSnapshotWriter
from autoscout.ranking_detail_route import RankingDetailRoute
from autoscout.ranking_evidence import RankingCaptureError
from autoscout.ranking_evidence import RankingPageEvidence
from autoscout.ranking_freshness import FINGERPRINT_DEBUG_FIELD
from autoscout.ranking_freshness import RankingFreshnessLedger
from autoscout.ranking_freshness import freshness_status_is_skip
from autoscout.ranking_freshness import ranking_undated_declared
from autoscout.ranking_freshness import undated_freshness_failure
from autoscout.runtime_budget import CAPTURE_SOURCE_RETRY_TIMEOUT_SECONDS
from autoscout.runtime_budget import CAPTURE_SOURCE_TIMEOUT_SECONDS
from autoscout.runtime_budget import capture_runtime_budget_for
from autoscout.runtime_budget import capture_source_allowance_for
from autoscout.russian_status import is_found_in_russian
from autoscout.safe_artifacts import publish_single
from autoscout.source_collectors import COLLECTOR_REGISTRY
from autoscout.source_collectors import CollectorResult
from autoscout.source_collectors import CollectorRunError
from autoscout.source_collectors import configured_source_page
from autoscout.source_collectors import extraction_contract
from autoscout.source_collectors import fetch_agent_source_page
from autoscout.source_collectors import registered_extraction_methods
from autoscout.source_collectors.base import SEEN_ITEM_INDEX_FAMILIES
from autoscout.source_collectors.base import contract_family
from autoscout.source_contribution import zero_contribution_outcome
from autoscout.source_mentions import SourceMention
from autoscout.source_metadata import declared_source_metadata
from autoscout.source_metadata import portable_source_metadata
from autoscout.source_page_identity import BETWEEN_EDITIONS_SAFE_STATUS
from autoscout.source_page_identity import BETWEEN_EDITIONS_SKIP_PREFIX
from autoscout.source_page_identity import RANKING_DETAIL_SKIPPED_FIELD
from autoscout.source_page_identity import amazon_charts_dated_url_matches
from autoscout.source_page_identity import attach_ranking_row_diagnostics
from autoscout.source_page_identity import between_editions_reason
from autoscout.source_page_identity import incomplete_ranking_debug_for_page
from autoscout.source_page_identity import ranking_freshness_declared
from autoscout.source_page_identity import validate_page_shape
from autoscout.source_page_identity import validate_ranking_detail_pages
from autoscout.source_proposals import normalize_source_identity
from autoscout.source_rules import EXTRACTION_ARTIFACT_SAFETY_STOP
from autoscout.source_rules import can_create_source_mention
from autoscout.source_rules import is_active_source
from autoscout.source_rules import is_discovery_source
from autoscout.source_rules import is_skipped_source
from autoscout.source_rules import skipped_reason
from autoscout.source_rules import withheld_reason_message
from autoscout.source_run_status import KIRKUS_DEFERRED_SKIP_REASON
from autoscout.source_run_status import SOURCE_STATUS_COLLECTED
from autoscout.source_run_status import SOURCE_STATUS_NOT_DUE
from autoscout.source_run_status import SOURCE_STATUS_REUSED
from autoscout.source_run_status import SourceRunStatus
from autoscout.source_run_status import normalized_source_run_status
from autoscout.source_run_status import retains_ranking_freshness
from autoscout.source_week_freshness import lenient_reference_date
from autoscout.source_week_freshness import source_week_freshness_declared

# Sources collected by code from the result of their own pass (#945, #955), by contract family.
# Each never fetches its website, and records what a run reports only when the pipeline consumes it.
PRECOLLECTED_CAPTURES: dict[str, Any] = {
    pm_deal_capture.FAMILY: pm_deal_capture,
    pw_issue_capture.FAMILY: pw_issue_capture,
}
PRECOLLECTED_METHODS = frozenset(method for module in PRECOLLECTED_CAPTURES.values() for method in module.METHODS)
PRECOLLECTED_REPLAY_METHODS = frozenset(module.REPLAY_METHOD for module in PRECOLLECTED_CAPTURES.values())


def _source_name_slug(source_name: str) -> str:
    """Derive a filesystem-safe slug from a source name.

    Replaces special characters with hyphens, collapses whitespace, and
    lowercases."""
    slug = "".join(c if c.isalnum() or c in " -_" else "-" for c in source_name)
    return "-".join(slug.split()).strip("-").lower() or "unnamed-source"


def _source_url_hash(source_url: str) -> str:
    """Derive the 8-char URL disambiguator shared by capture and extraction
    filenames, or "" when no URL is given.

    URL whitespace is stripped before hashing so the hash is deterministic
    regardless of trailing spaces in configuration."""
    stripped = source_url.strip() if source_url else ""
    if not stripped:
        return ""
    return hashlib.sha256(stripped.encode("utf-8")).hexdigest()[:8]


def codex_extraction_slug(source_name: str, source_url: str = "") -> str:
    """Derive a filesystem-safe extraction-artifact name from a source name and URL.

    Includes an 8-char URL hash when source_url is provided, matching the
    disambiguator that codex_capture_filename adds."""
    slug = _source_name_slug(source_name)
    url_hash = _source_url_hash(source_url)
    if url_hash:
        return f"{slug}-{url_hash}-codex-extraction.json"
    return f"{slug}-codex-extraction.json"


@dataclass
class SourceCollectionResult:
    """Outcome of one collection pass.

    The three counter fields are stage-local: they count what this pass saw,
    and nothing more.  They are not the run's source-outcome counts.  Two
    dataclasses in this module carry ``skipped_source_count`` and
    ``failed_source_count`` with the same names, and issue #473 was caused by
    reading one of them as if it were the whole run.  The run summary derives
    its counts from the capture ledger through
    ``autoscout.reporting.capture_source_outcome_counts``, which is the single
    authority.  Do not wire these fields into the summary again.
    """

    source_mentions: list[dict[str, Any]] = field(default_factory=list)
    source_runs: list[dict[str, Any]] = field(default_factory=list)
    skipped_source_count: int = 0
    failed_source_count: int = 0
    ignored_source_count: int = 0
    raw_candidate_count: int = 0
    source_level_excluded_count: int = 0
    # The retained undated-ranking ledger updated with what this pass observed.
    # Collection computes it and never writes state itself: persisting it is the
    # caller's decision, and a caller with no state simply drops it.
    ranking_freshness: RankingFreshnessLedger | None = None


@dataclass(frozen=True)
class SourceCollection:
    config_path: Path
    output_dir: Path
    run_date: str
    # Retained undated-ranking fingerprints from previous runs.  ``None`` means
    # no state is available, which fails an undated ranking closed.
    ranking_freshness: RankingFreshnessLedger | None = None

    def collect(self, sources: list[dict[str, Any]]) -> SourceCollectionResult:
        result = SourceCollectionResult(ranking_freshness=self.ranking_freshness)
        # Issue #700 part A: the zero-contribution seam tells "extraction
        # returned nothing, retry included" from a single attempt only when
        # retry evidence reaches it. The extraction phase records per-source
        # retry flags in the outcomes artifact; read them once here (absent
        # in offline and replay paths, which then honestly report no retry).
        from autoscout.codex_workflow import _extraction_outcomes

        try:
            retry_outcomes = _extraction_outcomes(self.output_dir)
        except (OSError, ValueError):
            retry_outcomes = {}
        for source in sources:
            if not is_active_source(source):
                # A source the workflow withheld is a source problem, not a
                # quiet operator decision, so it must reach failed_source_count.
                withheld = withheld_reason_message(source)
                if withheld:
                    result.failed_source_count += 1
                    result.source_runs.append(
                        source_run_record(source, "failed", withheld, 0, 0, 0, source_check_date=self.run_date)
                    )
                    continue
                # An absolute execution safety stop is a terminal extraction
                # outcome but never a source-health failure, so it stays an
                # inactive record with its own named reason (issue #411).
                if source.get("inactive_reason") == EXTRACTION_ARTIFACT_SAFETY_STOP:
                    result.source_runs.append(
                        source_run_record(
                            source,
                            "inactive",
                            "extraction stopped at the absolute execution safety ceiling",
                            0,
                            0,
                            0,
                            source_check_date=self.run_date,
                        )
                    )
                    continue
                result.source_runs.append(
                    source_run_record(
                        source, "inactive", "source is disabled", 0, 0, 0, source_check_date=self.run_date
                    )
                )
                continue
            if is_skipped_source(source):
                result.skipped_source_count += 1
                result.source_runs.append(
                    source_run_record(
                        source, "skipped", skipped_reason(source), 0, 0, 0, source_check_date=self.run_date
                    )
                )
                continue
            if not is_discovery_source(source):
                result.ignored_source_count += 1
                result.source_runs.append(
                    source_run_record(
                        source,
                        "ignored",
                        "source family is not allowed to create Scouting Candidate mentions",
                        0,
                        0,
                        0,
                        source_check_date=self.run_date,
                    )
                )
                continue

            try:
                collector_result = _with_extraction_retry_evidence(
                    source_candidates(self.config_path, self.output_dir, source, self.run_date),
                    retry_outcomes.get(str(source.get("name", ""))),
                )
                frozen_reason, frozen_status = undated_freshness_failure(
                    source,
                    str(collector_result.debug_metadata.get(FINGERPRINT_DEBUG_FIELD, "")),
                    self.ranking_freshness,
                    self.run_date,
                )
                if frozen_reason:
                    frozen_run_status = _count_freshness_verdict(result, frozen_status)
                    result.source_runs.append(
                        source_run_record(
                            source,
                            frozen_run_status,
                            frozen_reason,
                            collector_result.raw_mention_count,
                            0,
                            0,
                            collector_result.valid_mention_count,
                            collector_result.skipped_item_count,
                            {**collector_result.debug_metadata, "safe_failure_status": frozen_status},
                            source_check_date=self.run_date,
                        )
                    )
                    continue
                candidates = collector_result.mentions
                result.raw_candidate_count += collector_result.raw_mention_count
                excluded_count = 0
                valid_count = 0
                for candidate in candidates:
                    if not can_create_source_mention(source, candidate):
                        continue
                    valid_count += 1
                    # Fixture/collector payloads are untrusted source data;
                    # classify their helpers while preserving the candidate
                    # projection shape.
                    mention = SourceMention.from_candidate(
                        source,
                        candidate,
                        self.run_date,
                        classify_titles=True,
                        allow_sourced=True,
                    ).to_dict()
                    result.source_mentions.append(mention)
                    if is_found_in_russian(mention.get("russian_edition_status")):
                        excluded_count += 1
                result.source_level_excluded_count += excluded_count
                collected_status, collected_reason = _collected_status(
                    source,
                    extracted_count=int(
                        collector_result.debug_metadata.get(
                            "extracted_mention_count", collector_result.raw_mention_count
                        )
                    ),
                    valid_mention_count=collector_result.valid_mention_count,
                    admissible_mention_count=valid_count,
                    debug_metadata=collector_result.debug_metadata,
                )
                incomplete_warning = str(collector_result.debug_metadata.get("incomplete_ranking_warning", "")).strip()
                if incomplete_warning and collected_status == "collected":
                    collected_reason = incomplete_warning
                # Issue #716: the observed fingerprint is retained only once the
                # Source Run Status says the run was accepted.  Retaining it for
                # a failed run recorded "this list was current on date N" for a
                # run that reported nothing, and the next run refused the same
                # unchanged page as frozen for a reason it could never clear.
                # This is the one retention point: every accepted outcome of the
                # collect loop passes through it.
                retained_status = normalized_source_run_status(collected_status)
                if (
                    ranking_undated_declared(source)
                    and result.ranking_freshness is not None
                    and retained_status is not None
                    and retains_ranking_freshness(retained_status)
                ):
                    result.ranking_freshness = result.ranking_freshness.observe(
                        source,
                        str(collector_result.debug_metadata.get(FINGERPRINT_DEBUG_FIELD, "")),
                        self.run_date,
                    )
                if collected_status == "failed":
                    result.failed_source_count += 1
                result.source_runs.append(
                    source_run_record(
                        source,
                        collected_status,
                        collected_reason,
                        collector_result.raw_mention_count,
                        excluded_count,
                        valid_count - excluded_count,
                        collector_result.valid_mention_count,
                        collector_result.skipped_item_count,
                        collector_result.debug_metadata,
                        source_check_date=self.run_date,
                    )
                )
            except CollectorRunError as exc:
                # A frozen undated list is the more honest reason: whatever the
                # collector reported, the run cannot be published as current.
                fingerprint = str(exc.debug_metadata.get(FINGERPRINT_DEBUG_FIELD, "")).strip()
                if fingerprint:
                    frozen_reason, frozen_status = undated_freshness_failure(
                        source,
                        fingerprint,
                        self.ranking_freshness,
                        self.run_date,
                    )
                    if frozen_reason:
                        frozen_run_status = _count_freshness_verdict(result, frozen_status)
                        result.source_runs.append(
                            source_run_record(
                                source,
                                frozen_run_status,
                                frozen_reason,
                                exc.raw_mention_count,
                                0,
                                0,
                                exc.valid_mention_count,
                                exc.skipped_item_count,
                                {**exc.debug_metadata, "safe_failure_status": frozen_status},
                                source_check_date=self.run_date,
                            )
                        )
                        continue
                if exc.status == "skipped":
                    result.skipped_source_count += 1
                else:
                    result.failed_source_count += 1
                result.source_runs.append(
                    source_run_record(
                        source,
                        exc.status,
                        str(exc),
                        exc.raw_mention_count,
                        0,
                        0,
                        exc.valid_mention_count,
                        exc.skipped_item_count,
                        exc.debug_metadata,
                        source_check_date=self.run_date,
                    )
                )
            except ValueError as exc:
                # Isolate failures from live adapters while preserving the
                # established fail-fast contract for malformed replay and
                # recorded artifacts.
                method = str(source.get("extraction_method", "")).strip().casefold()
                if not method.endswith("-live"):
                    raise
                result.failed_source_count += 1
                result.source_runs.append(
                    source_run_record(source, "failed", str(exc), 0, 0, 0, source_check_date=self.run_date)
                )
            except Exception as exc:
                result.failed_source_count += 1
                result.source_runs.append(
                    source_run_record(source, "failed", str(exc), 0, 0, 0, source_check_date=self.run_date)
                )
        return result

    def collect_on_schedule(
        self, sources: list[dict[str, Any]], recorded_lists: Mapping[str, Mapping[str, Any]] | None = None
    ) -> SourceCollectionResult:
        """Collect the due sources and carry each not-due source's list forward (#1057).

        A not-due source was neither captured nor extracted tonight. Its last
        collected list, from *recorded_lists* (``scheduled-source-lists.json``
        in the state directory), is added unchanged except for its date, in
        the source's own place among the sources, so its books keep their
        signals and do not read as changed. It gets a ``not-due`` run.
        """
        if not any(is_not_due_entry(source) for source in sources):
            return self.collect(sources)
        recorded = recorded_lists or {}
        previous_output: list[Any] | None = None
        result = SourceCollectionResult(ranking_freshness=self.ranking_freshness)
        for source in sources:
            if not is_not_due_entry(source):
                part = replace(self, ranking_freshness=result.ranking_freshness).collect([source])
                result.source_mentions.extend(part.source_mentions)
                result.source_runs.extend(part.source_runs)
                result.skipped_source_count += part.skipped_source_count
                result.failed_source_count += part.failed_source_count
                result.ignored_source_count += part.ignored_source_count
                result.raw_candidate_count += part.raw_candidate_count
                result.source_level_excluded_count += part.source_level_excluded_count
                result.ranking_freshness = part.ranking_freshness
                continue
            name = str(source.get("name", "")).strip()
            entry = recorded.get(name)
            if isinstance(entry, Mapping) and isinstance(entry.get("mentions"), list):
                original, carried_from = list(entry["mentions"]), CARRIED_FROM_RECORDED_LIST
                list_date = str(entry.get("collected_on", ""))
            else:
                # Transition only: a source collected before its list was kept in
                # state has its last list in the previous run's output. It is
                # read once; this run records it in state for the nights after.
                if previous_output is None:
                    from autoscout.weekly_report import WeeklyReportStore

                    raw = read_json(WeeklyReportStore(self.output_dir).path("source-mentions.json"), default=[])
                    previous_output = raw if isinstance(raw, list) else []
                original = [
                    mention
                    for mention in previous_output
                    if isinstance(mention, Mapping) and str(mention.get("source_name", "")).strip() == name
                ]
                carried_from = CARRIED_FROM_PREVIOUS_OUTPUT if original else CARRIED_FROM_NOTHING
                list_date = str(original[0].get("source_check_date", "")) if original else ""
            carried = carried_forward_mentions(original, name, self.run_date)
            result.source_mentions.extend(carried)
            run = source_run_record(
                source,
                SOURCE_STATUS_NOT_DUE,
                str(source.get("notes", "")),
                0,
                0,
                0,
                collection_mode="codex-capture",
                source_check_date=self.run_date,
            )
            run[NEXT_DUE_DATE_FIELD] = str(source.get(NEXT_DUE_DATE_FIELD, ""))
            run[CARRIED_MENTION_COUNT_FIELD] = len(carried)
            run[CARRIED_FROM_FIELD] = carried_from
            run[CARRIED_LIST_DATE_FIELD] = list_date
            result.source_runs.append(run)
        return result


# Retry flags the extraction phase records, copied verbatim so the
# zero-contribution seam reads the same evidence production wrote.
_RETRY_EVIDENCE_FIELDS = (
    "empty_extraction_retry",
    "empty_extraction_recovered",
    "empty_extraction_retry_refused",
)


def _count_freshness_verdict(result: SourceCollectionResult, status: str) -> str:
    """Count one refused undated-ranking run and return its Source Run Status."""
    if freshness_status_is_skip(status):
        result.skipped_source_count += 1
        return "skipped"
    result.failed_source_count += 1
    return "failed"


def _with_extraction_retry_evidence(
    collector_result: CollectorResult, outcome_record: Mapping[str, Any] | None
) -> CollectorResult:
    """Merge extraction-phase retry evidence into collector debug metadata.

    Issue #700 part A: without this, the seam cannot tell a single attempt
    from a retry that stayed empty. Absent or malformed records merge
    nothing, so offline and replay paths keep byte-identical debug metadata.
    """
    if not isinstance(outcome_record, Mapping):
        return collector_result
    retry = {field: outcome_record[field] for field in _RETRY_EVIDENCE_FIELDS if field in outcome_record}
    if not retry:
        return collector_result
    return replace(collector_result, debug_metadata={**collector_result.debug_metadata, **retry})


def source_candidates(config_path: Path, output_dir: Path, source: dict[str, Any], run_date: str) -> CollectorResult:
    candidates = list(source.get("fixture_candidates", []))
    raw_mention_count = len(candidates)
    valid_mention_count = len(candidates)
    skipped_item_count = 0
    debug_metadata: dict[str, Any] = {}
    for index, mention in enumerate(source.get("collector_mentions", [])):
        if not isinstance(mention, dict):
            raise ValueError(
                f"{source.get('name', 'collector source')}: collector mention {index + 1} must be an object"
            )
        candidates.append(SourceMention.from_collector(source, mention, run_date).to_dict())
        raw_mention_count += 1
        valid_mention_count += 1
    # Dispatch: extraction_method is normalized (stripped, lowercased) and
    # matched against the registry exactly once, here. A source whose method
    # matches no registered collector gets no collector contribution -- the
    # pre-existing fixture/collector-mention candidates above and the
    # recorded-mentions inputs below still compose normally.
    raw_extraction_method = source.get("extraction_method", "")
    normalized_extraction_method = str(raw_extraction_method).strip().lower()
    registered_collector = COLLECTOR_REGISTRY.get(normalized_extraction_method)
    recorded_paths = recorded_mentions_paths(config_path, source)
    if registered_collector is None and not candidates and not recorded_paths:
        # No registered collector, and no fixture/collector-mention/recorded
        # inputs to fall back on: this source would otherwise silently
        # collect nothing while reporting success. Surface it explicitly
        # (issue #237) instead -- a configuration typo becomes a visible
        # failed source-run record naming the unrecognized value.
        raise CollectorRunError(
            f'unrecognized extraction_method "{raw_extraction_method}"; '
            f"registered methods: {', '.join(registered_extraction_methods())}",
            status="failed",
        )
    if registered_collector is not None:
        dispatched_result = registered_collector.collect(config_path, output_dir, source, run_date)
        for mention in dispatched_result.mentions:
            candidates.append(SourceMention.from_collector(source, mention, run_date).to_dict())
        raw_mention_count += dispatched_result.raw_mention_count
        valid_mention_count += dispatched_result.valid_mention_count
        skipped_item_count += dispatched_result.skipped_item_count
        debug_metadata.update(dispatched_result.debug_metadata)
        if contract_family(source) in PRECOLLECTED_CAPTURES:
            # A pipeline that reads the pass result directly reports its rows here.
            _record_reported(config_path, source, dispatched_result.mentions, run_date, debug_metadata)
    for recorded_path in recorded_paths:
        if not recorded_path.exists():
            raise FileNotFoundError(recorded_path)
        recorded_data = read_json(recorded_path)
        mentions = recorded_data.get("mentions", recorded_data) if isinstance(recorded_data, dict) else recorded_data
        if not isinstance(mentions, list):
            raise ValueError(f"{recorded_path}: recorded source mentions must be a list")
        for index, mention in enumerate(mentions):
            if not isinstance(mention, dict):
                raise ValueError(f"{recorded_path}: mention {index + 1} must be an object")
            candidates.append(SourceMention.from_recorded(source, mention).to_dict())
            raw_mention_count += 1
            valid_mention_count += 1
        capture_module = PRECOLLECTED_CAPTURES.get(contract_family(source))
        if capture_module is not None:
            coverage = recorded_data.get(capture_module.COVERAGE_FIELD) if isinstance(recorded_data, dict) else None
            if isinstance(coverage, dict):
                lost = coverage.get("skipped_item_count", 0)
                if isinstance(lost, int) and lost > 0:
                    raw_mention_count += lost
                    skipped_item_count += lost
                if isinstance(coverage.get("debug_metadata"), dict):
                    debug_metadata.update(coverage["debug_metadata"])
            # The weekly run consumes the rows here, as Kirkus records its rows when its
            # extraction is collected: only now are they reported, so only now are they seen (#945).
            _record_reported(config_path, source, mentions, run_date, debug_metadata)
    return CollectorResult(
        mentions=candidates,
        raw_mention_count=raw_mention_count,
        valid_mention_count=valid_mention_count,
        skipped_item_count=skipped_item_count,
        debug_metadata=debug_metadata,
    )


def _record_reported(
    config_path: Path,
    source: dict[str, Any],
    mentions: list[Any],
    run_date: str,
    debug_metadata: dict[str, Any],
) -> None:
    """Mark the rows a pipeline reports as seen; capture and the source check never get here."""
    module = PRECOLLECTED_CAPTURES[contract_family(source)]
    try:
        debug_metadata[module.NEW_SEEN_COUNT_KEY] = module.record_reported(config_path, source, mentions, run_date)
    except module.CAPTURE_ERROR as exc:
        raise CollectorRunError(str(exc), status="failed") from exc


def recorded_mentions_paths(config_path: Path, source: dict[str, Any]) -> list[Path]:
    raw_paths: list[Any] = []
    if source.get("recorded_mentions_file"):
        raw_paths.append(source["recorded_mentions_file"])
    raw_paths.extend(source.get("recorded_mentions_files", []))
    return [
        path if path.is_absolute() else config_path.parent / path
        for path in (Path(str(raw_path)) for raw_path in raw_paths)
    ]


def source_run_status_from_artifact_freshness(freshness: ArtifactFreshness) -> SourceRunStatus:
    """Convert artifact freshness to the source-run status at collection."""
    match freshness:
        case "fresh" | "unbound":
            return SOURCE_STATUS_COLLECTED
        case "reused":
            return SOURCE_STATUS_REUSED
        case _:
            return assert_never(freshness)


def _collected_status(
    source: dict[str, Any],
    *,
    extracted_count: int,
    valid_mention_count: int,
    admissible_mention_count: int,
    debug_metadata: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Return the run status and reason for a source that raised no error.

    A source whose artifact was reused from an earlier operation did collect
    mentions, so it is not a failure, but it did not collect them this week.
    Reporting both cases as "collected" is what let most of a portfolio go
    stale while the row counts kept looking healthy (Issue #384).

    A source that contributed nothing is named rather than reported as a plain
    success. An empty result is not evidence of an empty page, and a zero with
    no reason let a broken source hide behind a healthy summary (Issue #464).
    """
    freshness = normalized_artifact_freshness(source.get("artifact_freshness"))
    if freshness is not None and is_stale_artifact_freshness(freshness):
        # The artifact predicate decides fault policy. The adapter only
        # converts the two domain values at this serialization boundary.
        return (
            source_run_status_from_artifact_freshness(freshness),
            "served from an artifact bound to an earlier operation, not freshly collected",
        )
    if valid_mention_count == 0:
        return zero_contribution_outcome(
            source,
            extracted_count=extracted_count,
            valid_mention_count=valid_mention_count,
            admissible_mention_count=admissible_mention_count,
            debug_metadata=debug_metadata,
        )
    if freshness is not None:
        return source_run_status_from_artifact_freshness(freshness), ""
    return SOURCE_STATUS_COLLECTED, ""


# A source collected at capture time and replayed from its recorded artifact
# keeps its capture-time collection mode and method in its run row (issue #927).
PRECOLLECTED_COLLECTION_MODE_FIELD = "precollected_collection_mode"
PRECOLLECTED_EXTRACTION_METHOD_FIELD = "precollected_extraction_method"


def source_run_record(
    source: dict[str, Any],
    status: str,
    reason: str,
    raw_count: int,
    excluded_count: int,
    editor_facing_count: int,
    valid_mention_count: int | None = None,
    skipped_item_count: int = 0,
    debug_metadata: dict[str, Any] | None = None,
    collection_mode: str | None = None,
    source_check_date: str = "",
) -> dict[str, Any]:
    collector_debug_metadata = debug_metadata or {}
    effective_mode = (
        collection_mode if collection_mode is not None else source_collection_mode(source, collector_debug_metadata)
    )
    return {
        "source_name": source.get("name", ""),
        "family": source.get("family", ""),
        "source_check_date": source_check_date,
        "url": source.get("url", ""),
        "access_status": source.get("access_status", ""),
        "extraction_method": source.get(PRECOLLECTED_EXTRACTION_METHOD_FIELD) or source.get("extraction_method", ""),
        "collection_mode": effective_mode,
        "check_frequency": source.get("check_frequency", ""),
        **declared_source_metadata(source),
        "status": status,
        "reason": reason,
        "raw_candidate_count": raw_count,
        "raw_mention_count": raw_count,
        "valid_mention_count": raw_count if valid_mention_count is None else valid_mention_count,
        "skipped_item_count": skipped_item_count,
        "extracted_mention_count": int(collector_debug_metadata.get("extracted_mention_count", raw_count)),
        "dropped_mention_count": int(collector_debug_metadata.get("dropped_mention_count", skipped_item_count)),
        "drop_reasons": collector_debug_metadata.get("drop_reasons", []),
        "excluded_candidate_count": excluded_count,
        "editor_facing_count": editor_facing_count,
        "debug_metadata": {
            "fixture_candidate_count": len(source.get("fixture_candidates", [])),
            "collector_mention_count": len(source.get("collector_mentions", [])),
            "collector_response_file": str(source.get("collector_response_file", "")),
            **collector_debug_metadata,
        },
    }


def source_collection_mode(source: dict[str, Any], debug_metadata: dict[str, Any]) -> str:
    method = str(source.get("extraction_method", "")).strip().lower()
    backend_mode = str(debug_metadata.get("backend_mode", "")).strip().lower()
    if backend_mode:
        return backend_mode
    precollected_mode = str(source.get(PRECOLLECTED_COLLECTION_MODE_FIELD, "")).strip()
    if precollected_mode:
        return precollected_mode
    if debug_metadata.get("live_fixture_replay"):
        return "live-fixture-replay"
    if debug_metadata.get("live_fetch"):
        return "live"
    if method == "codex-extraction-replay":
        return "codex-capture"
    if method in {
        "agent-extraction-replay",
        "bookpage-recorded-response",
        "bookpage-source-page",
        "nonfiction-fr-rss-replay",
        "npr-source-page",
        *PRECOLLECTED_REPLAY_METHODS,
    }:
        return "replay"
    if (
        method in {"fixture", "collector-fixture", "recorded"}
        or source.get("fixture_candidates")
        or source.get("recorded_mentions_file")
    ):
        return "fixture-replay"
    if method.endswith("-live"):
        return "live"
    return "configured"


_CAPTURE_SNAPSHOT_OUTCOMES = frozenset(
    {"inactive", "skipped", "ignored", "failed", "precollected", "codex-captured", SOURCE_STATUS_NOT_DUE}
)


class _CaptureProgressSnapshot:
    """Keep capture progress rows in configuration order."""

    def __init__(
        self,
        output_dir: Path,
        sources: list[dict[str, Any]],
        *,
        started: float,
        deadline: float,
        per_source_timeout_seconds: float,
        monotonic: Any,
        failure_recorder: ProgressSnapshotFailureRecorder | None = None,
        presentation: ProgressSnapshotPresentation | None = None,
    ) -> None:
        self._writer = ProgressSnapshotWriter(
            output_dir,
            monotonic=monotonic,
            failure_recorder=failure_recorder,
            presentation=presentation,
        )
        self._started = started
        self._deadline = deadline
        self._per_source_timeout_seconds = per_source_timeout_seconds
        self._source_started: dict[int, float] = {}
        self._rows = [
            {
                "source_name": str(source.get("name", "")),
                "source_url": str(source.get("url", "")),
                "state": "pending",
                "status": "pending",
                "outcome": None,
                "elapsed_seconds": 0.0,
                "remaining_runtime_budget_seconds": self._remaining(started),
                "per_source_timeout_seconds": self._configured_timeout(source),
            }
            for source in sources
        ]

    def _remaining(self, now: float) -> float:
        return round(max(0.0, self._deadline - now), 3)

    def _configured_timeout(self, source: dict[str, Any]) -> float:
        """Return the wall-clock bound this source will be held to.

        The heartbeat reports the bound that is actually enforced, so a
        declared detail route shows the allowance it earns rather than the
        per-request socket ceiling nothing would hold it to (issue #717).
        """
        try:
            configured = float(source.get("timeout_seconds", self._per_source_timeout_seconds))
        except (TypeError, ValueError, OverflowError):
            configured = self._per_source_timeout_seconds
        if not math.isfinite(configured) or configured <= 0:
            configured = self._per_source_timeout_seconds
        socket_timeout = min(configured, self._per_source_timeout_seconds)
        try:
            allowance = capture_source_allowance_for(source, socket_timeout)
        except ValueError:
            # A malformed detail declaration fails this source closed once it
            # starts; the pending row keeps the bound it can state.
            allowance = socket_timeout
        return round(allowance, 3)

    def _write(self, now: float, *, force: bool = True) -> None:
        self._writer.publish(
            stage="capture",
            status="running",
            elapsed_seconds=now - self._started,
            rows=[dict(row) for row in self._rows],
            now=now,
            force=force,
        )

    def initial(self) -> None:
        self._write(self._started)

    def begin(self, index: int, now: float, timeout_seconds: float) -> None:
        row = self._rows[index]
        self._source_started[index] = now
        row.update(
            {
                "state": "in_flight",
                "status": "in_flight",
                "outcome": None,
                "elapsed_seconds": 0.0,
                "per_source_timeout_seconds": round(max(0.0, float(timeout_seconds)), 3),
            }
        )
        self._refresh_budget(now)
        self._write(now)

    def heartbeat(self, index: int, now: float) -> None:
        row = self._rows[index]
        row["elapsed_seconds"] = round(max(0.0, now - self._source_started[index]), 3)
        self._refresh_budget(now)
        self._write(now)

    def finish(self, index: int, outcome: str, now: float) -> None:
        safe_outcome = outcome if outcome in _CAPTURE_SNAPSHOT_OUTCOMES else "failed"
        row = self._rows[index]
        elapsed = now - self._source_started.get(index, now)
        row.update(
            {
                "state": "finished",
                "status": "finished",
                "outcome": safe_outcome,
                "elapsed_seconds": round(max(0.0, elapsed), 3),
            }
        )
        self._refresh_budget(now)
        self._write(now)

    def _refresh_budget(self, now: float) -> None:
        remaining = self._remaining(now)
        for row in self._rows:
            row["remaining_runtime_budget_seconds"] = remaining

    def final(self, now: float) -> None:
        for row in self._rows:
            if row["state"] != "finished":
                row.update(
                    {
                        "state": "finished",
                        "status": "finished",
                        "outcome": "failed",
                    }
                )
        self._refresh_budget(now)
        self._writer.publish(
            stage="capture",
            status="finished",
            elapsed_seconds=now - self._started,
            rows=[dict(row) for row in self._rows],
            now=now,
            force=True,
        )


@dataclass
class CodexExtractionWorklist:
    """Capture worklist for one operation.

    ``skipped_source_count`` and ``failed_source_count`` are stage-local, like
    the identically named fields on ``SourceCollectionResult`` above, and are
    reported to the operator by ``autoscout.codex_workflow``.  They are not
    the run's source-outcome counts; see that class for why (issue #473).
    """

    run_date: str = ""
    work_items: list[dict[str, Any]] = field(default_factory=list)
    precollected_sources: list[dict[str, Any]] = field(default_factory=list)
    source_runs: list[dict[str, Any]] = field(default_factory=list)
    skipped_source_count: int = 0
    failed_source_count: int = 0
    captured_source_count: int = 0
    # Sources not due tonight (#1057); finalization carries them to the pipeline.
    not_due_sources: list[dict[str, Any]] = field(default_factory=list)
    # The budget this stage actually applied, derived from the configured
    # portfolio unless the caller supplied one.  Reported so the production
    # adapter logs the bound it ran under instead of a constant (issue #717).
    runtime_budget_seconds: float = 0.0


def _validate_page_shape(
    source: dict[str, Any],
    page_data: dict[str, Any],
    run_date: str = "",
    *,
    freshness_reference: str = "active_run_date",
    detail_route: RankingDetailRoute | None = None,
) -> str:
    """Validate page shape against an explicit active or retained date."""
    if contract_family(source) in {
        "bookinfo-weekly-json",
        "ipc-top40-csv",
        "perlentaucher-daily-digest",
        "kirkus-coming-soon",
    }:
        from autoscout.source_collectors.base import validate_extraction_page_identity

        try:
            validate_extraction_page_identity(
                source,
                page_data,
                collector_kind="codex-capture",
                run_date=run_date,
                freshness_reference=(
                    "page_reference_date" if freshness_reference == "page_reference_date" else "active_run_date"
                ),
            )
        except CollectorRunError as exc:
            return f"page shape validation failed: {exc}"
        return ""
    reference_date = None
    if contract_family(source) == "award-list" or (
        source_week_freshness_declared(source) and not str(source.get("ranking_row_selector", "")).strip()
    ):
        # The page-shape check refuses a missing reference date itself, after
        # it has confirmed the page is the declared table.
        reference_date = lenient_reference_date(
            page_data.get("reference_date") if freshness_reference == "page_reference_date" else run_date
        )
    elif str(source.get("ranking_row_selector", "")).strip() and ranking_freshness_declared(source):
        if freshness_reference == "page_reference_date":
            raw_reference_date = page_data.get("reference_date")
            if not isinstance(raw_reference_date, str) or not raw_reference_date.strip():
                return "page shape validation failed: replay page reference_date is missing"
            try:
                reference_date = date.fromisoformat(raw_reference_date.strip())
            except ValueError:
                return "page shape validation failed: replay page reference_date is invalid"
            if reference_date.isoformat() != raw_reference_date.strip():
                return "page shape validation failed: replay page reference_date is invalid"
        elif run_date:
            reference_date = date.fromisoformat(run_date)
    return validate_page_shape(
        source,
        page_data,
        reference_date=reference_date,
        detail_route=detail_route,
    )


def codex_capture_filename(source: dict[str, Any]) -> str:
    slug = _source_name_slug(str(source.get("name", "unnamed-source")))
    url_hash = _source_url_hash(str(source.get("url", "")))
    return f"codex-capture-{slug}{'-' + url_hash if url_hash else ''}.json"


def codex_helper_metadata_filename(source: dict[str, Any]) -> str:
    """Return one bounded, source-identity-derived helper artifact name."""
    slug = _source_name_slug(str(source.get("name", "unnamed-source")))[:48].strip("-") or "unnamed-source"
    identity = f"{source.get('name', '')}\x1f{source.get('url', '')}"
    identity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"codex-helper-metadata-{slug}-{identity_hash}.json"


def publish_codex_helper_metadata(output_dir: Path, source: dict[str, Any], helper_metadata: Any) -> str:
    """Publish validated helper bytes once under the run output directory."""
    filename = codex_helper_metadata_filename(source)
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / filename
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise ValueError("helper metadata artifact destination is unsafe")
    if destination.exists() and destination.stat().st_nlink != 1:
        raise ValueError("helper metadata artifact destination is unsafe")
    raw_bytes = helper_metadata.raw_bytes
    if destination.exists() and destination.read_bytes() == raw_bytes:
        return filename
    publish_single(destination, raw_bytes)
    return filename


def _portable_seen_items_file(config_path: Path, output_dir: Path, source: dict[str, Any]) -> dict[str, str]:
    """Re-express a seen-item index path for the replay config in *output_dir*.

    The index is written when the extraction is collected, and that stage reads
    the generated replay config, not the config that declared the path.
    """
    if contract_family(source) not in SEEN_ITEM_INDEX_FAMILIES:
        return {}
    # Both seen-item contracts resolve the declared path beside the config that names it.
    from autoscout.perlentaucher_digest import seen_items_path

    path = seen_items_path(config_path, source)
    if path is None:
        return {}
    return {"seen_guids_file": Path(os.path.relpath(path, start=output_dir.resolve())).as_posix()}


def _portable_precollected_seen_items_file(
    config_path: Path, output_dir: Path, source: dict[str, Any]
) -> dict[str, str]:
    """Re-express a precollected source's seen-item index path for the replay config in *output_dir* (#945).

    The index moves when the pipeline consumes the recorded mentions, and
    that stage reads the generated replay config.
    """
    module = PRECOLLECTED_CAPTURES.get(contract_family(source))
    if module is None:
        return {}
    path = module.seen_items_path(config_path, source)
    if path is None:
        return {}
    return {"seen_guids_file": Path(os.path.relpath(path, start=output_dir.resolve())).as_posix()}


def codex_source_page(
    config_path: Path,
    output_dir: Path,
    source: dict[str, Any],
    *,
    live: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch or replay a source page for Codex extraction worklist.

    When live=True, always fetch the source URL live even if fixture files are
    configured.  When live=False (default), replay from fixture files when present.
    """
    if not live and (source.get("source_page_fixture_file") or source.get("live_response_file")):
        page_data, page_debug = configured_source_page(config_path, source)
        page_debug["live_fixture_replay"] = True
        return page_data, page_debug
    active_run_date = str(source.get("_active_run_date", "")).strip()
    reference_date = date.fromisoformat(active_run_date) if active_run_date else None
    return fetch_agent_source_page(config_path, output_dir, source, reference_date=reference_date)


# Capture-attempt failures that one retry may follow (issue #1005).  A socket or
# per-source deadline timeout is retried with the larger retry ceiling; a 429
# that names a Retry-After inside the stage budget is retried after that wait.
RETRY_CLASS_TIMEOUT = "timeout"
RETRY_CLASS_RATE_LIMITED = "rate_limited"
_RETRYABLE_TIMEOUT_STATUSES = frozenset({"capture_deadline", "request_timeout"})


@dataclass
class _SourceCaptureResult:
    """One source's capture outcome, held until the stage's order is known.

    A retried source has two attempts; ``run``, ``counter``, ``work_item`` and
    ``precollected`` always describe the final one.
    """

    run: dict[str, Any]
    counter: str | None = None
    snapshot_outcome: str = ""
    work_item: dict[str, Any] | None = None
    precollected: dict[str, Any] | None = None
    not_due: dict[str, Any] | None = None
    # The failure class of a live fetch a single retry may follow, if any.
    retry_class: str | None = None
    retry_after_seconds: float = 0.0
    # Whether the first attempt was retry-eligible, retried or not.
    retryable: bool = False
    socket_timeout_seconds: float = 0.0
    allowance_seconds: float = 0.0
    started: float = 0.0
    ended: float = 0.0
    attempts: list[dict[str, Any]] = field(default_factory=list)


def _retry_eligibility(run: dict[str, Any]) -> tuple[str | None, float]:
    """Return the retry class of a failed live fetch and its Retry-After, if any."""
    debug = run.get("debug_metadata") or {}
    if str(debug.get("safe_failure_status", "")) in _RETRYABLE_TIMEOUT_STATUSES:
        return RETRY_CLASS_TIMEOUT, 0.0
    if debug.get("rate_limited") is True and debug.get("retry_after_seconds") is not None:
        return RETRY_CLASS_RATE_LIMITED, float(debug["retry_after_seconds"])
    return None, 0.0


def _attempt_evidence(
    number: int, result: _SourceCaptureResult, *, waited_seconds: float | None = None
) -> dict[str, Any]:
    """Return the bounded evidence of one capture attempt for the source run."""
    debug = result.run.get("debug_metadata") or {}
    evidence: dict[str, Any] = {
        "attempt": number,
        "status": result.run["status"],
        "reason": result.run["reason"],
        "safe_failure_status": str(debug.get("safe_failure_status", "") or ""),
        "duration_seconds": round(max(0.0, result.ended - result.started), 3),
        "socket_timeout_seconds": round(result.socket_timeout_seconds, 3),
        "allowance_seconds": round(result.allowance_seconds, 3),
    }
    if waited_seconds is not None:
        evidence["waited_seconds"] = round(waited_seconds, 3)
    return evidence


def _capture_source_attempt(
    source: dict[str, Any],
    *,
    snapshot_index: int,
    capture_snapshot: _CaptureProgressSnapshot,
    config_path: Path,
    output_dir: Path,
    run_date: str,
    live: bool,
    monotonic: Any,
    started: float,
    deadline: float,
    socket_cap_seconds: float,
) -> _SourceCaptureResult:
    """Make one live or replay capture attempt of one source.

    The attempt is bounded by the socket ceiling it is given, its own
    allowance and the stage *deadline*.  The result is returned, not published:
    the caller decides whether a retry follows and records outcomes in
    configuration order.
    """
    remaining = deadline - started
    bounded_source = dict(source)
    helper_metadata_filename = ""
    helper_metadata_sha256 = ""
    socket_timeout_seconds = 0.0
    allowance_seconds = 0.0
    fetch_attempted = False

    def settle(
        run: dict[str, Any],
        counter: str | None,
        snapshot_outcome: str | None = None,
        *,
        work_item: dict[str, Any] | None = None,
        precollected: dict[str, Any] | None = None,
    ) -> _SourceCaptureResult:
        ended = monotonic()
        run["duration_seconds"] = round(max(0.0, ended - started), 3)
        return _SourceCaptureResult(
            run=run,
            counter=counter,
            snapshot_outcome=(counter or "") if snapshot_outcome is None else snapshot_outcome,
            work_item=work_item,
            precollected=precollected,
            started=started,
            ended=ended,
            socket_timeout_seconds=socket_timeout_seconds,
            allowance_seconds=allowance_seconds,
        )

    try:
        # A configured timeout only lowers the ceiling; an absent one leaves
        # the caller's ceiling in force, which is what a retry relies on.
        configured_timeout = math.inf
        if "timeout_seconds" in source:
            try:
                configured_timeout = float(source["timeout_seconds"])
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid source timeout_seconds") from exc
            if not math.isfinite(configured_timeout) or configured_timeout <= 0:
                raise ValueError("invalid source timeout_seconds")
        socket_timeout_seconds = min(configured_timeout, socket_cap_seconds, remaining)
        # The source's allowance is a wall-clock deadline across every
        # request it makes, not just the timeout of each one.  Without it a
        # declared detail route could spend (1 + max_pages) x timeout
        # seconds against a bound of timeout and take the overrun out of
        # the extraction stage (issue #717).  The allowance follows the
        # same declaration the stage budget follows, so a declared detail
        # route is paid for here too, and it never exceeds the stage budget
        # still left.
        allowance_seconds = min(capture_source_allowance_for(source, socket_timeout_seconds), remaining)
        bounded_source["timeout_seconds"] = socket_timeout_seconds
        bounded_source[CAPTURE_DEADLINE_KEY] = CaptureDeadline.starting_at(started, allowance_seconds, monotonic)
        bounded_source["_active_run_date"] = run_date
        helper_metadata = load_helper_metadata(config_path, bounded_source)
        if helper_metadata is not None:
            helper_metadata_filename = publish_codex_helper_metadata(output_dir, source, helper_metadata)
            helper_metadata_sha256 = helper_metadata.sha256
        capture_snapshot.begin(snapshot_index, monotonic(), allowance_seconds)
        normalized_method = str(source.get("extraction_method", "")).strip().lower()
        # A PM deals or PW issue source is always collected from its pass result: its website is never fetched.
        if normalized_method in {"nonfiction-fr-rss-live", *PRECOLLECTED_METHODS} or (
            normalized_method == "nonfiction-fr-rss-replay" and not live
        ):
            collector = COLLECTOR_REGISTRY[normalized_method]
            collector_result = collector.collect(config_path, output_dir, bounded_source, run_date)
            artifact_name = (
                f"precollected-{_source_name_slug(str(source.get('name', 'nonfiction-fr')))}"
                f"-{_source_url_hash(str(source.get('url', '')))}.json"
            )
            artifact: dict[str, Any] = {"mentions": collector_result.mentions}
            capture_module = PRECOLLECTED_CAPTURES.get(contract_family(source))
            if normalized_method in PRECOLLECTED_METHODS and capture_module is not None:
                # The extraction stage replays only this artifact: the rows the capture
                # could not report travel with it, so the review names them too (#945).
                artifact[capture_module.COVERAGE_FIELD] = {
                    "skipped_item_count": collector_result.skipped_item_count,
                    "debug_metadata": {
                        key: value
                        for key, value in collector_result.debug_metadata.items()
                        if key in capture_module.COVERAGE_DEBUG_KEYS
                    },
                }
            write_json(output_dir / artifact_name, artifact)
            capture_mode = (
                "live-fixture-replay"
                if collector_result.debug_metadata.get("live_fixture_replay")
                else "replay"
                if normalized_method in {"nonfiction-fr-rss-replay", *PRECOLLECTED_REPLAY_METHODS}
                else "live"
            )
            precollected = {
                "name": source.get("name", ""),
                "family": source.get("family", ""),
                "url": source.get("url", ""),
                "active": True,
                "access_status": source.get("access_status", "open"),
                "extraction_method": "recorded",
                "check_frequency": declared_check_frequency(source),
                "notes": source.get("notes", ""),
                "recorded_mentions_file": artifact_name,
                # The extraction stage replays the artifact as a recorded
                # source; its run row keeps how the source was collected
                # (issue #927).
                PRECOLLECTED_COLLECTION_MODE_FIELD: capture_mode,
                PRECOLLECTED_EXTRACTION_METHOD_FIELD: normalized_method,
                **portable_source_metadata(
                    source,
                    helper_metadata_file=helper_metadata_filename,
                    helper_metadata_sha256=helper_metadata_sha256,
                ),
                **_portable_precollected_seen_items_file(config_path, output_dir, source),
            }
            run = source_run_record(
                source,
                "precollected",
                "",
                collector_result.raw_mention_count,
                0,
                collector_result.valid_mention_count,
                collector_result.valid_mention_count,
                collector_result.skipped_item_count,
                collector_result.debug_metadata,
                collection_mode=capture_mode,
                source_check_date=run_date,
            )
            return settle(run, "captured", "precollected", precollected=precollected)
        fetch_attempted = True
        page_data, page_debug = codex_source_page(config_path, output_dir, bounded_source, live=live)
    except CollectorRunError as exc:
        counter = "skipped" if exc.status == "skipped" else "failed"
        run = source_run_record(
            source,
            exc.status,
            str(exc),
            0,
            0,
            0,
            debug_metadata=exc.debug_metadata,
            collection_mode="codex-capture",
            source_check_date=run_date,
        )
        result = settle(run, counter)
        if fetch_attempted and live:
            result.retry_class, result.retry_after_seconds = _retry_eligibility(run)
        return result
    except Exception as exc:
        run = source_run_record(
            source,
            "failed",
            str(exc),
            0,
            0,
            0,
            collection_mode="codex-capture",
            source_check_date=run_date,
        )
        return settle(run, "failed")

    resolved_url = str(page_data.get("resolved_url", "")).strip()
    configured_url = str(source.get("url", "")).strip()
    if resolved_url:
        if source.get("amazon_charts_weekly") is True:
            target_matches = amazon_charts_dated_url_matches(
                configured_url,
                resolved_url,
                chart_path_segment=str(source.get("chart_path_segment", "")),
                expected_page_heading=str(source.get("expected_page_heading", "")),
            )
        else:
            target_matches = normalize_source_identity(resolved_url) == normalize_source_identity(configured_url)
    else:
        target_matches = True
    if not target_matches:
        run = source_run_record(
            source,
            "failed",
            "capture resolved to a different source target",
            0,
            0,
            0,
            debug_metadata={**page_debug, "safe_failure_status": "wrong_target", "resolved_url": resolved_url},
            collection_mode="codex-capture",
            source_check_date=run_date,
        )
        return settle(run, "failed")

    off_season = between_editions_reason(source, page_data)
    if off_season:
        # The page shows the declared state between two editions: nothing
        # to capture or extract, and nothing has failed.
        run = source_run_record(
            source,
            "skipped",
            BETWEEN_EDITIONS_SKIP_PREFIX + off_season,
            0,
            0,
            0,
            debug_metadata={**page_debug, "safe_failure_status": BETWEEN_EDITIONS_SAFE_STATUS},
            collection_mode="codex-capture",
            source_check_date=run_date,
        )
        return settle(run, "skipped")

    detail_route = RankingDetailRoute.from_source(source)
    # Page-shape validation: when configured, verify expected HTML markers are present.
    page_shape_error = _validate_page_shape(
        source,
        page_data,
        run_date,
        freshness_reference=("page_reference_date" if page_debug.get("live_fixture_replay") else "active_run_date"),
        detail_route=detail_route,
    )
    if page_shape_error:
        page_shape_debug = attach_ranking_row_diagnostics(
            {**page_debug, "safe_failure_status": "page_shape"},
            str(page_data.get("html", "")),
            source,
            page_shape_error,
            detail_route=detail_route,
        )
        run = source_run_record(
            source,
            "failed",
            page_shape_error,
            0,
            0,
            0,
            debug_metadata=page_shape_debug,
            collection_mode="codex-capture",
            source_check_date=run_date,
        )
        return settle(run, "failed")

    incomplete_debug = incomplete_ranking_debug_for_page(source, page_data, detail_route=detail_route)
    if incomplete_debug:
        page_debug = {**page_debug, **incomplete_debug}
    # A contract that reads no ranking rows states its own shortfall in the capture's debug metadata.
    incomplete_reason = str(
        incomplete_debug.get("incomplete_ranking_warning") or page_debug.get("incomplete_ranking_warning", "")
    )

    detail_error = validate_ranking_detail_pages(source, page_data, route=detail_route)
    if detail_error:
        run = source_run_record(
            source,
            "failed",
            detail_error,
            0,
            0,
            0,
            debug_metadata={**page_debug, "safe_failure_status": "detail_evidence"},
            collection_mode="codex-capture",
            source_check_date=run_date,
        )
        return settle(run, "failed")

    capture_filename = codex_capture_filename(source)
    capture_limit = int(source.get("max_response_bytes", DEFAULT_CAPTURE_MAX_RESPONSE_BYTES))
    try:
        retained_evidence = RankingPageEvidence.from_mapping(page_data).sanitized(
            rows_max_bytes=capture_limit,
            identity_max_bytes=capture_limit,
        )
        capture_data = {
            "collection_mode": "codex-capture",
            "source_url": source.get("url", ""),
            "source_name": source.get("name", ""),
            "source_page": page_data.get("source_page", source.get("url", "")),
            "resolved_url": page_data.get("resolved_url", ""),
            "html": retained_evidence.rows_html,
        }
        if retained_evidence.identity_html:
            capture_data["identity_html"] = retained_evidence.identity_html
        if detail_route is not None and isinstance(page_data.get("detail_pages"), list):
            capture_data["detail_pages"] = retain_ranking_detail_pages(page_data["detail_pages"], detail_route)
        # Issue #1163: the validated skipped rows stay with their capture.
        if detail_route is not None and page_data.get(RANKING_DETAIL_SKIPPED_FIELD):
            capture_data[RANKING_DETAIL_SKIPPED_FIELD] = page_data[RANKING_DETAIL_SKIPPED_FIELD]
        # The extractor reads rows from the capture file, so it gets the validated rows, not the raw
        # response. They are rebuilt from the evidence: a capture written before #782 carries no text.
        if contract_family(source) == "bookinfo-weekly-json":
            from autoscout.bookinfo_ranking import validate_bundle as validate_bookinfo_bundle

            capture_data["contract_family"] = "bookinfo-weekly-json"
            capture_data["bookinfo_evidence"] = page_data["bookinfo_evidence"]
            capture_data["text"] = json.dumps(
                validate_bookinfo_bundle(page_data["bookinfo_evidence"], source), ensure_ascii=False
            )
        if contract_family(source) == "ipc-top40-csv":
            from autoscout.ipc_top40_ranking import validate_bundle as validate_ipc_top40_bundle

            capture_data["contract_family"] = "ipc-top40-csv"
            capture_data["ipc_top40_evidence"] = page_data["ipc_top40_evidence"]
            capture_data["text"] = json.dumps(
                validate_ipc_top40_bundle(page_data["ipc_top40_evidence"], source), ensure_ascii=False
            )
        if contract_family(source) == "perlentaucher-daily-digest":
            from autoscout.perlentaucher_digest import retained_capture_fields as perlentaucher_capture_fields

            capture_data.update(perlentaucher_capture_fields(page_data, source))
        if contract_family(source) == "kirkus-coming-soon":
            from autoscout.kirkus_listing import retained_capture_fields as kirkus_capture_fields

            capture_data.update(kirkus_capture_fields(page_data, source))
        if "reference_date" in page_data:
            capture_data["reference_date"] = page_data["reference_date"]
        write_bounded_capture(
            capture_data,
            max_response_bytes=capture_limit,
            kind=CaptureKind.RANKING,
            destination=output_dir / capture_filename,
        )
    except (RankingCaptureError, CaptureWriteError) as exc:
        run = source_run_record(
            source,
            "failed",
            str(exc),
            0,
            0,
            0,
            debug_metadata={**page_debug, "safe_failure_status": "ranking_capture_bound"},
            collection_mode="codex-capture",
            source_check_date=run_date,
        )
        return settle(run, "failed")

    run = source_run_record(
        source,
        "codex-captured",
        incomplete_reason,
        0,
        0,
        0,
        debug_metadata={
            "collection_mode": "codex-capture",
            "capture_file": capture_filename,
            **page_debug,
        },
        collection_mode="codex-capture",
        source_check_date=run_date,
    )
    expected_extraction = codex_extraction_slug(source.get("name", ""), source.get("url", ""))
    work_item = {
        "source_name": source.get("name", ""),
        "source_family": source.get("family", ""),
        "source_url": source.get("url", ""),
        "capture_file": capture_filename,
        "expected_extraction_file": expected_extraction,
        "extraction_method": "codex",
        # Without a stated contract the 2026-08-01 run produced entries
        # missing the eligibility fields and entries whose evidence URLs
        # never occurred in the capture, and every mention was dropped
        # by the collector's contract check (Issue #375).
        "extraction_contract": extraction_contract(source, capture_file=capture_filename),
        "source_metadata": {
            "access_status": source.get("access_status", ""),
            "notes": source.get("notes", ""),
            **portable_source_metadata(
                source,
                helper_metadata_file=helper_metadata_filename,
                helper_metadata_sha256=helper_metadata_sha256,
            ),
            **_portable_seen_items_file(config_path, output_dir, source),
        },
    }
    return settle(run, "captured", "codex-captured", work_item=work_item)


def _unfetched_result(source: dict[str, Any], status: str, reason: str, run_date: str) -> _SourceCaptureResult:
    run = source_run_record(
        source, status, reason, 0, 0, 0, collection_mode="codex-capture", source_check_date=run_date
    )
    return _SourceCaptureResult(run=run, counter="skipped" if status == "skipped" else None, snapshot_outcome=status)


def _capture_without_fetch(
    source: dict[str, Any], run_date: str, *, kirkus_mode: str, weekday: str | None
) -> _SourceCaptureResult | None:
    """Settle a source that is not fetched tonight, or return None for one that is.

    *weekday* is the collection weekday when the collection schedule applies
    (#1057), and None when every active source is fetched, as in a source
    health check.
    """
    if not is_active_source(source):
        return _unfetched_result(source, "inactive", "source is disabled", run_date)
    if is_skipped_source(source):
        return _unfetched_result(source, "skipped", skipped_reason(source), run_date)
    if kirkus_mode == "defer" and contract_family(source) == "kirkus-coming-soon":
        return _unfetched_result(source, "skipped", KIRKUS_DEFERRED_SKIP_REASON, run_date)
    if not is_discovery_source(source):
        return _unfetched_result(
            source, "ignored", "source family is not allowed to create Scouting Candidate mentions", run_date
        )
    if weekday is not None and not is_source_due(source_check_frequency(source), date.fromisoformat(run_date), weekday):
        entry = not_due_source_entry(source, date.fromisoformat(run_date), weekday)
        result = _unfetched_result(source, SOURCE_STATUS_NOT_DUE, entry["notes"], run_date)
        result.run[NEXT_DUE_DATE_FIELD] = entry[NEXT_DUE_DATE_FIELD]
        result.not_due = entry
        return result
    return None


def prepare_codex_extraction_worklist(
    config_path: Path,
    output_dir: Path,
    run_date: str,
    *,
    live: bool = False,
    monotonic: Any = time.monotonic,
    runtime_budget_seconds: float | None = None,
    per_source_timeout_seconds: float = CAPTURE_SOURCE_TIMEOUT_SECONDS,
    retry_source_timeout_seconds: float = CAPTURE_SOURCE_RETRY_TIMEOUT_SECONDS,
    sleep: Any = time.sleep,
    progress_snapshot_failure_recorder: ProgressSnapshotFailureRecorder | None = None,
    snapshot_progress_presentation: ProgressSnapshotPresentation | None = None,
    kirkus_mode: str = "all",
    apply_collection_schedule: bool = False,
) -> CodexExtractionWorklist:
    if kirkus_mode not in {"all", "defer", "only"}:
        raise ValueError("invalid Kirkus capture mode")
    if not math.isfinite(per_source_timeout_seconds) or per_source_timeout_seconds <= 0:
        raise ValueError("capture runtime budget and per-source timeout must be positive")
    if not math.isfinite(retry_source_timeout_seconds) or retry_source_timeout_seconds < per_source_timeout_seconds:
        raise ValueError("capture retry timeout must be finite and at least the per-source timeout")
    if runtime_budget_seconds is not None and (
        not math.isfinite(runtime_budget_seconds)
        or runtime_budget_seconds < 0
        or (runtime_budget_seconds == 0 and kirkus_mode != "only")
    ):
        raise ValueError("capture runtime budget and per-source timeout must be positive")
    raw_config = read_json(config_path, default={})
    config = resolve_weekly_portfolio(raw_config, config_path) if isinstance(raw_config, dict) else {}
    sources = config.get("sources", [])
    weekday = collection_weekday(config) if apply_collection_schedule else None
    if kirkus_mode == "only":
        sources = [source for source in sources if contract_family(source) == "kirkus-coming-soon"]
    # The stage budget follows what the portfolio declares.  A detail route
    # fetches one page per row, so its declared cap is part of the bound; an
    # explicit caller-supplied budget still wins.
    if runtime_budget_seconds is None:
        runtime_budget_seconds = capture_runtime_budget_for(source for source in sources if isinstance(source, Mapping))

    # One entry per configured source, in configuration order.  Each entry is
    # appended exactly once, so its index is the source's index.
    results: list[_SourceCaptureResult] = []
    started = monotonic()
    deadline = started + runtime_budget_seconds
    capture_snapshot = _CaptureProgressSnapshot(
        output_dir,
        sources,
        started=started,
        deadline=deadline,
        per_source_timeout_seconds=per_source_timeout_seconds,
        monotonic=monotonic,
        failure_recorder=progress_snapshot_failure_recorder,
        presentation=snapshot_progress_presentation,
    )
    capture_snapshot.initial()

    def attempt(source: dict[str, Any], index: int, started_at: float, socket_cap: float) -> _SourceCaptureResult:
        return _capture_source_attempt(
            source,
            snapshot_index=index,
            capture_snapshot=capture_snapshot,
            config_path=config_path,
            output_dir=output_dir,
            run_date=run_date,
            live=live,
            monotonic=monotonic,
            started=started_at,
            deadline=deadline,
            socket_cap_seconds=socket_cap,
        )

    for source_index, source in enumerate(sources):
        source_started = monotonic()
        unfetched = _capture_without_fetch(source, run_date, kirkus_mode=kirkus_mode, weekday=weekday)
        if unfetched is not None:
            results.append(unfetched)
            capture_snapshot.finish(source_index, unfetched.snapshot_outcome, monotonic())
            continue

        remaining = deadline - monotonic()
        if remaining <= 0:
            run = source_run_record(
                source,
                "failed",
                f"capture budget exhausted after {runtime_budget_seconds:.1f}s",
                0,
                0,
                0,
                collection_mode="codex-capture",
                source_check_date=run_date,
            )
            run["duration_seconds"] = round(max(0.0, monotonic() - source_started), 3)
            results.append(_SourceCaptureResult(run=run, counter="failed", snapshot_outcome="failed"))
            capture_snapshot.finish(source_index, "failed", monotonic())
            continue

        result = attempt(source, source_index, source_started, per_source_timeout_seconds)
        result.attempts = [_attempt_evidence(1, result)]
        result.retryable = result.retry_class is not None
        results.append(result)
        capture_snapshot.finish(source_index, result.snapshot_outcome, monotonic())

    # The one retry (issue #1005), in configuration order, inside the same stage
    # deadline.  A timeout is retried when a full first allowance is still left;
    # a rate-limited source is retried after its Retry-After when that wait ends
    # before the deadline.  A retry that fails again is final.
    for source_index, source in enumerate(sources):
        first = results[source_index]
        if first.retry_class is None:
            continue
        waited: float | None = None
        if first.retry_class == RETRY_CLASS_TIMEOUT:
            if deadline - monotonic() <= first.allowance_seconds:
                continue
            socket_cap = retry_source_timeout_seconds
        else:
            wait_until = first.ended + first.retry_after_seconds
            if wait_until >= deadline:
                continue
            before_wait = monotonic()
            capture_snapshot.begin(source_index, before_wait, max(0.0, wait_until - before_wait))
            while monotonic() < wait_until:
                sleep(min(PROGRESS_HEARTBEAT_INTERVAL_SECONDS, max(0.0, wait_until - monotonic())))
                capture_snapshot.heartbeat(source_index, monotonic())
            waited = max(0.0, monotonic() - before_wait)
            if deadline - monotonic() <= 0:
                continue
            socket_cap = per_source_timeout_seconds
        retry = attempt(source, source_index, monotonic(), socket_cap)
        capture_snapshot.finish(source_index, retry.snapshot_outcome, monotonic())
        retry.retryable = True
        retry.attempts = [*first.attempts, _attempt_evidence(2, retry, waited_seconds=waited)]
        results[source_index] = retry

    source_runs: list[dict[str, Any]] = []
    work_items: list[dict[str, Any]] = []
    precollected_sources: list[dict[str, Any]] = []
    not_due_sources: list[dict[str, Any]] = []
    skipped = 0
    failed = 0
    captured = 0
    for result in results:
        run = result.run
        if result.attempts and (len(result.attempts) > 1 or result.retryable):
            run["capture_attempts"] = result.attempts
        if len(result.attempts) > 1:
            # The total covers every attempt and the wait a 429 retry spent.
            run["duration_seconds"] = round(
                sum(
                    float(item["duration_seconds"]) + float(item.get("waited_seconds", 0.0)) for item in result.attempts
                ),
                3,
            )
        source_runs.append(run)
        if result.counter == "captured":
            captured += 1
        elif result.counter == "skipped":
            skipped += 1
        elif result.counter == "failed":
            failed += 1
        if result.work_item is not None:
            work_items.append(result.work_item)
        if result.precollected is not None:
            precollected_sources.append(result.precollected)
        if result.not_due is not None:
            # Where the source sits among the work items, so the pipeline sees
            # the sources in the order of a night that collected it.
            not_due_sources.append({**result.not_due, WORK_ITEM_POSITION_FIELD: len(work_items)})

    for run in source_runs:
        run.setdefault("duration_seconds", 0.0)
    capture_snapshot.final(monotonic())

    return CodexExtractionWorklist(
        run_date=run_date,
        work_items=work_items,
        precollected_sources=precollected_sources,
        source_runs=source_runs,
        skipped_source_count=skipped,
        failed_source_count=failed,
        captured_source_count=captured,
        not_due_sources=not_due_sources,
        runtime_budget_seconds=runtime_budget_seconds,
    )
