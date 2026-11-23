"""Typed config model and explicit from-dict validation.

Provides dataclass-based config models for the top-level run config,
per-source config, and per-Russian-check config, together with a
validation function that operates on raw dicts read from JSON config
files.

Validation is strict about known keys and basic field types so that
misconfigured runs fail early with clear operator-facing messages.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from autoscout.annual_edition import ANNUAL_EDITION_FIELD
from autoscout.annual_edition import validate_annual_edition_declaration
from autoscout.artifact_freshness import ALL_FRESHNESS_VALUES
from autoscout.artifact_freshness import UNBOUND
from autoscout.artifact_freshness import ArtifactFreshness
from autoscout.collection_schedule import COLLECTION_WEEKDAY_FIELD
from autoscout.collection_schedule import NEXT_DUE_DATE_FIELD
from autoscout.collection_schedule import NOT_DUE_FIELD
from autoscout.collection_schedule import collection_weekday_error
from autoscout.json_io import read_json
from autoscout.ranking_detail_route import ranking_detail_requested_fields
from autoscout.russian_edition_settings import CONFIG_KEYS as RUSSIAN_EDITION_CONFIG_KEYS
from autoscout.russian_edition_settings import config_errors as russian_edition_config_errors
from autoscout.source_collectors.base import CONTRACT_FAMILIES
from autoscout.source_contribution import validate_no_entries_declaration
from autoscout.source_failure_streaks import THRESHOLD_CONFIG_KEY as STREAK_THRESHOLD_CONFIG_KEY
from autoscout.source_failure_streaks import threshold_error as streak_threshold_error
from autoscout.source_metadata import EXACT_BOOK_LIST_FIELDS
from autoscout.source_metadata import RANKING_FIELD_SPECS_BY_NAME
from autoscout.source_metadata import RANKING_RANK_SOURCES
from autoscout.source_metadata import RANKING_TABLE_FIELDS
from autoscout.source_metadata import SOURCE_CONTRACT_FIELDS
from autoscout.source_metadata import SOURCE_LAYOUT_FIELDS
from autoscout.source_metadata import SOURCE_METADATA_FIELDS
from autoscout.source_metadata import SOURCE_PAGE_IDENTITY_FIELDS
from autoscout.source_page_identity import validate_between_editions_declaration
from autoscout.source_page_identity import validate_entry_scope_selector
from autoscout.source_page_identity import validate_ranking_contract_metadata
from autoscout.source_page_identity import validate_visible_title_selector
from autoscout.source_week_freshness import validate_source_week_freshness_declaration
from autoscout.state_store import STATE_BACKEND_CONFIG_KEY
from autoscout.state_store import STATE_BACKENDS
from autoscout.state_store import STATE_DATABASE_CONFIG_KEY

# The Amazon Charts marketplace is a host-and-language pair: the German
# chart must declare language de on www.amazon.de, and the English charts
# must declare language en on www.amazon.com or www.amazon.co.uk
# (Issue #378).
AMAZON_CHARTS_MARKETPLACE_LANGUAGE: dict[str, str] = {
    "www.amazon.com": "en",
    "www.amazon.de": "de",
    "www.amazon.co.uk": "en",
}

# ── Top-level run-config keys ─────────────────────────────────────────────

_TOP_LEVEL_KNOWN_KEYS: set[str] = {
    "run_date",
    "sources",
    "pilot_mode",
    "editor_row_target",
    "pilot_row_min",
    "pilot_row_max",
    "pilot_row_target_min",  # alias in readiness_config
    "pilot_row_target_max",  # alias in readiness_config
    "readiness_purpose",
    "report_qa",
    "candidate_history",
    "excluded_candidate_history",
    "returned_editor_rows",
    "search_optimization_trial",
    "editorial_fit_shadow",
    # Issue #1105: order the review by the recorded fit score. Off unless true.
    "editorial_fit_ordering",
    # Issue #950: a Russian description from the PM deal logline. Off unless true.
    "pm_logline_descriptions",
    "pm_logline_deepseek_fallback",
    # Issue #1140: «Idioma original» and page counts from todostuslibros.com. Off unless true.
    "todostuslibros_lookups",
    "source_metrics_window_weeks",
    "russian_edition_checks",
    # Issue #1181: the weekly run's Russian-edition stage and its limits. Off unless true.
    *RUSSIAN_EDITION_CONFIG_KEYS,
    "weekly_portfolio",
    # Issue #1057: the weekday annual and monthly sources are collected on.
    COLLECTION_WEEKDAY_FIELD,
    # Issue #1068: the State Store backend; json until the SQLite cutover.
    STATE_BACKEND_CONFIG_KEY,
    # Issue #1125: failed attempts in a row before a source counts as broken; 3 by default.
    STREAK_THRESHOLD_CONFIG_KEY,
    # Issue #1074: the SQLite backend's database file, when not the default.
    STATE_DATABASE_CONFIG_KEY,
    # Legacy replay configs may contain these generated identity fields. The
    # pipeline recomputes evidence_digest from current inputs at run time.
    "operation_id",
    "evidence_digest",
}

_TOP_LEVEL_REQUIRED_KEYS: set[str] = {"sources"}

_TOP_LEVEL_KEY_TYPES: dict[str, type | tuple[type, ...]] = {
    "run_date": str,
    "sources": list,
    "pilot_mode": bool,
    "editor_row_target": (int, float),
    "pilot_row_min": (int, float),
    "pilot_row_max": (int, float),
    "pilot_row_target_min": (int, float),
    "pilot_row_target_max": (int, float),
    "readiness_purpose": str,
    "report_qa": dict,
    "candidate_history": list,
    "excluded_candidate_history": list,
    "returned_editor_rows": list,
    "search_optimization_trial": bool,
    "editorial_fit_shadow": bool,
    "editorial_fit_ordering": bool,
    "pm_logline_descriptions": bool,
    "pm_logline_deepseek_fallback": bool,
    "todostuslibros_lookups": bool,
    "source_metrics_window_weeks": int,
    "russian_edition_checks": list,
    "weekly_portfolio": str,
    COLLECTION_WEEKDAY_FIELD: str,
    STATE_BACKEND_CONFIG_KEY: str,
    STATE_DATABASE_CONFIG_KEY: str,
    STREAK_THRESHOLD_CONFIG_KEY: int,
    "operation_id": str,
    "evidence_digest": str,
}

# ── Source config keys ────────────────────────────────────────────────────

_SOURCE_KNOWN_KEYS: set[str] = {
    "access_status",
    "active",
    "metadata_only",
    "inactive_reason",
    # Set by finalize-codex-run, not by an operator: the specific evidence
    # behind a withholding reason (#386), and whether the artifact behind an
    # active source was produced by this operation or reused from an earlier
    # one (#384).
    "withheld_detail",
    "artifact_freshness",
    # Set by finalize-codex-run for a source not due tonight (#1057).
    NOT_DUE_FIELD,
    NEXT_DUE_DATE_FIELD,
    "amazon_charts_weekly",
    "chart_path_segment",
    "author_marker",
    "audible_explicit_chart",
    "api_key_env",
    "capture_extraction_response_file",
    "capture_live_response_file",
    "capture_normalized_mentions_file",
    "check_frequency",
    "may_report_no_entries",
    "may_report_no_entries_reason",
    "between_editions_page_heading",
    "between_editions_reason",
    ANNUAL_EDITION_FIELD,
    "source_week_max_age_days",
    "codex_artifact_dir",
    "collector_mentions",
    "collector_response_file",
    "credential_env",
    "credentials_ref",
    "enable_live",
    "enable_real_backend",
    "expected_page_heading",
    "expected_sort_label",
    "headline_card_titles",
    "children_byline_markers",
    "eligibility_outside_knowledge",
    "expected_range_marker",
    "expected_table_headers",
    "expected_visible_markers",
    "forbidden_visible_markers",
    "expected_reporting_period",
    "ranking_expected_path",
    "visible_title_selector",
    "ranking_row_selector",
    "ranking_rank_selector",
    "ranking_title_selector",
    "ranking_author_selector",
    "ranking_identifier_selector",
    "ranking_identifier_attribute",
    "ranking_identifier_pattern",
    "ranking_category_marker",
    "ranking_reporting_period_pattern",
    "ranking_reporting_period_date_format",
    "ranking_reporting_period_max_age_days",
    "ranking_expected_raw_row_count",
    "ranking_min_raw_row_count",
    "ranking_max_raw_row_count",
    "ranking_evidence_url",
    "adult_nonfiction_exclusions",
    "metadata_ranking_table",
    "extraction_backend",
    "extraction_method",
    "extraction_response_file",
    "fake_extraction_response",  # inline fixture response (Any)
    "fake_extraction_response_file",
    "fake_real_extraction_response_file",
    "family",
    "fixture_candidates",
    "live_extraction_response_file",
    "live_response_file",
    "max_model_input_chars",
    "model",
    "model_max_output_tokens",
    "model_name",
    "model_timeout_seconds",
    "lifecycle_note",
    "lifecycle_status",
    "name",
    "probation_started_at",
    "probation_evidence_window_weeks",
    "notes",
    "portfolio_name",
    "real_extraction_response",
    "real_extraction_response_file",
    "retired_at",
    "retired_by",
    "retired_cards_hidden",
    "retirement_reason",
    "promoted_at",
    "promoted_by",
    "promoted_reason",
    "kept_at",
    "kept_by",
    "kept_reason",
    "recorded_mentions_file",
    "recorded_mentions_files",
    # Capture-binding identity, written onto precollected sources by
    # mvp_run_evidence and copied verbatim into the generated replay config.
    "operation_id",
    "recorded_mentions_sha256",
    # How a precollected source was collected at capture time (issue #927).
    "precollected_collection_mode",
    "precollected_extraction_method",
    "review_pages_fixture_file",
    "rss_fixture_file",
    "russian_edition_status_note",
    "russian_edition_status_source",
    "source_page_fixture_file",
    "helper_metadata_file",
    "helper_metadata_sha256",
    "seen_guids_file",
    "seen_guids_fixture_file",
    "kirkus_collection_file",
    "pm_collection_file",
    "pw_collection_file",
    "pw_descriptions_file",
    "max_new_items_per_run",
    "max_response_bytes",
    "timeout_seconds",
    "url",
    "visible_risk_status_note",
    "visible_risk_status_source",
    *SOURCE_METADATA_FIELDS,
    *SOURCE_LAYOUT_FIELDS,
    *SOURCE_CONTRACT_FIELDS,
    *RANKING_TABLE_FIELDS,
    *EXACT_BOOK_LIST_FIELDS,
}

_SOURCE_REQUIRED_KEYS: set[str] = {"name"}

_SOURCE_KEY_TYPES: dict[str, type | tuple[type, ...]] = {
    "name": str,
    "family": str,
    "url": str,
    "active": bool,
    "metadata_only": bool,
    "inactive_reason": str,
    "withheld_detail": str,
    "artifact_freshness": str,
    NOT_DUE_FIELD: bool,
    NEXT_DUE_DATE_FIELD: str,
    "access_status": str,
    "extraction_method": str,
    "check_frequency": str,
    "notes": str,
    "fixture_candidates": list,
    "collector_mentions": list,
    "enable_live": bool,
    "expected_page_heading": str,
    "expected_sort_label": str,
    "headline_card_titles": bool,
    "children_byline_markers": list,
    "eligibility_outside_knowledge": bool,
    "expected_table_headers": list,
    "expected_visible_markers": list,
    "forbidden_visible_markers": list,
    "expected_reporting_period": str,
    "ranking_expected_path": str,
    "visible_title_selector": str,
    "adult_nonfiction_exclusions": list,
    "metadata_ranking_table": bool,
    "may_report_no_entries": bool,
    "may_report_no_entries_reason": str,
    "between_editions_page_heading": str,
    "between_editions_reason": str,
    ANNUAL_EDITION_FIELD: dict,
    "source_week_max_age_days": int,
    "extraction_backend": str,
    "enable_real_backend": bool,
    "credential_env": str,
    "api_key_env": str,
    "credentials_ref": str,
    "max_model_input_chars": (int, float),
    "model_timeout_seconds": (int, float),
    "model_max_output_tokens": (int, float),
    "model": str,
    "model_name": str,
    "lifecycle_status": str,
    "lifecycle_note": str,
    "probation_started_at": str,
    "probation_evidence_window_weeks": int,
    "portfolio_name": str,
    "source_page_fixture_file": str,
    "helper_metadata_file": str,
    "helper_metadata_sha256": str,
    "extraction_response_file": str,
    "live_response_file": str,
    "capture_live_response_file": str,
    "capture_extraction_response_file": str,
    "recorded_mentions_file": str,
    "recorded_mentions_files": list,
    "precollected_collection_mode": str,
    "precollected_extraction_method": str,
    "review_pages_fixture_file": str,
    "rss_fixture_file": str,
    "collector_response_file": str,
    "live_extraction_response_file": str,
    "real_extraction_response_file": str,
    "retired_at": str,
    "retired_by": str,
    "retired_cards_hidden": bool,
    "retirement_reason": str,
    "promoted_at": str,
    "promoted_by": str,
    "promoted_reason": str,
    "kept_at": str,
    "kept_by": str,
    "kept_reason": str,
    "fake_real_extraction_response_file": str,
    "fake_extraction_response_file": str,
    "capture_normalized_mentions_file": str,
    "codex_artifact_dir": str,
    "timeout_seconds": (int, float),
    "max_response_bytes": int,
    "amazon_charts_weekly": bool,
    "chart_path_segment": str,
    "author_marker": str,
    "audible_explicit_chart": bool,
    "expected_range_marker": str,
    "seen_guids_file": str,
    "seen_guids_fixture_file": str,
    "kirkus_collection_file": str,
    "pm_collection_file": str,
    "pw_collection_file": str,
    "pw_descriptions_file": str,
    "max_new_items_per_run": int,
    "russian_edition_status_source": str,
    "russian_edition_status_note": str,
    "visible_risk_status_source": str,
    "visible_risk_status_note": str,
    **{field: str for field in SOURCE_METADATA_FIELDS},
    **{field: str for field in SOURCE_PAGE_IDENTITY_FIELDS},
    **{field: str for field in SOURCE_LAYOUT_FIELDS},
    "contract_family": str,
    **{
        field: {"bool": bool, "list": list, "int": int, "string": str}[spec.value_kind]
        for field, spec in RANKING_FIELD_SPECS_BY_NAME.items()
    },
    **{
        field: bool
        if field in {"amazon_charts_weekly", "audible_explicit_chart", "metadata_exact_book_list"}
        else int
        if field == "max_response_bytes"
        else str
        for field in EXACT_BOOK_LIST_FIELDS
    },
}

# ── Russian-edition check config keys ─────────────────────────────────────

_RUSSIAN_CHECK_KNOWN_KEYS: set[str] = {
    "capture_recorded_results_file",
    "enable_live",
    "family",
    "fixture_error",
    "fixture_live_results",
    "fixture_results",
    "live_blocked_reason",
    "live_response_file",
    "live_response_files",
    "name",
    "query_param",
    "recorded_results",
    "recorded_results_file",
    "recorded_results_files",
    "retailer",
    "safe_debug_status",
    "search_path",
    "source_family",
    "timeout_seconds",
    "url",
    # source-rules keys also valid on checks (is_skipped_source, skipped_reason):
    "access_status",
    "active",
    "notes",
}

_RUSSIAN_CHECK_KEY_TYPES: dict[str, type | tuple[type, ...]] = {
    "retailer": str,
    "url": str,
    "name": str,
    "fixture_results": list,
    "fixture_live_results": list,
    "recorded_results": list,
    "recorded_results_file": str,
    "recorded_results_files": list,
    "capture_recorded_results_file": str,
    "enable_live": bool,
    "family": str,
    "source_family": str,
    "search_path": str,
    "query_param": str,
    "timeout_seconds": (int, float),
    "live_response_file": str,
    "live_response_files": list,
    "fixture_error": str,
    "live_blocked_reason": str,
    "safe_debug_status": str,
    "access_status": str,
    "active": bool,
    "notes": str,
}

# ── Dataclass models (documentation + typed scaffolding) ───────────────────


@dataclass
class SourceConfig:
    """Validated per-source configuration."""

    name: str
    url: str
    family: str = ""
    active: bool = True
    # The capture contains bibliographic/signal facts, never book prose.
    metadata_only: bool = False
    access_status: str = "open"
    extraction_method: str = ""
    check_frequency: str = "weekly"
    notes: str = ""
    artifact_freshness: ArtifactFreshness = UNBOUND
    language: str = ""
    country: str = ""
    market: str = ""
    expected_category: str = ""
    signal_family: str = ""
    refresh_cadence: str = ""
    url_stability: str = ""
    extraction_notes: str = ""
    expected_page_heading: str = ""
    headline_card_titles: bool = False
    children_byline_markers: list[str] = field(default_factory=list)
    eligibility_outside_knowledge: bool = False
    expected_sort_label: str = ""
    expected_range_marker: str = ""
    fixture_candidates: list[dict[str, Any]] = field(default_factory=list)
    collector_mentions: list[dict[str, Any]] = field(default_factory=list)
    enable_live: bool = False
    extraction_backend: str = ""
    enable_real_backend: bool = False
    credential_env: str = ""
    api_key_env: str = ""
    credentials_ref: str = ""
    max_model_input_chars: int = 120_000
    model_timeout_seconds: float = 30.0
    model_max_output_tokens: int = 1200
    model: str = ""
    model_name: str = ""
    source_page_fixture_file: str = ""
    extraction_response_file: str = ""
    live_response_file: str = ""
    capture_live_response_file: str = ""
    capture_extraction_response_file: str = ""
    recorded_mentions_file: str = ""
    recorded_mentions_files: list[str] = field(default_factory=list)
    review_pages_fixture_file: str = ""
    rss_fixture_file: str = ""
    seen_guids_file: str = ""
    seen_guids_fixture_file: str = ""
    kirkus_collection_file: str = ""
    pm_collection_file: str = ""
    pw_collection_file: str = ""
    pw_descriptions_file: str = ""
    max_new_items_per_run: int = 12
    collector_response_file: str = ""
    live_extraction_response_file: str = ""
    real_extraction_response_file: str = ""
    fake_real_extraction_response_file: str = ""
    fake_extraction_response_file: str = ""
    real_extraction_response: Any = None
    capture_normalized_mentions_file: str = ""
    codex_artifact_dir: str = ""
    timeout_seconds: float = 10.0
    max_response_bytes: int = 750_000
    amazon_charts_weekly: bool = False
    chart_path_segment: str = ""
    author_marker: str = ""
    audible_explicit_chart: bool = False
    russian_edition_status_source: str = ""
    russian_edition_status_note: str = ""
    visible_risk_status_source: str = ""
    visible_risk_status_note: str = ""
    metadata_exact_book_list: bool = False
    expected_signal_strength: str = ""
    eligible_candidate_category: str = ""
    helper_metadata_file: str = ""
    helper_metadata_sha256: str = ""
    ranking_detail_link_selector: str = ""
    ranking_detail_url_pattern: str = ""
    ranking_detail_scope_selector: str = ""
    ranking_detail_fields: list[str] = field(default_factory=list)
    ranking_detail_identifier_selector: str = ""
    ranking_detail_identifier_attribute: str = ""
    ranking_detail_identifier_pattern: str = ""
    ranking_detail_category_selector: str = ""
    ranking_detail_category_pattern: str = ""
    ranking_detail_category_allowlist: list[str] = field(default_factory=list)
    ranking_detail_max_pages: int = 0


@dataclass
class RussianCheckConfig:
    """Validated per-check Russian-edition configuration."""

    retailer: str = ""
    url: str = ""
    name: str = ""
    fixture_results: list[dict[str, Any]] = field(default_factory=list)
    fixture_live_results: list[dict[str, Any]] = field(default_factory=list)
    recorded_results: list[dict[str, Any]] = field(default_factory=list)
    recorded_results_file: str = ""
    recorded_results_files: list[str] = field(default_factory=list)
    capture_recorded_results_file: str = ""
    enable_live: bool = False
    family: str = ""
    source_family: str = ""
    search_path: str = ""
    query_param: str = ""
    timeout_seconds: float = 10.0
    live_response_file: str = ""
    live_response_files: list[str] = field(default_factory=list)
    fixture_error: str = ""
    live_blocked_reason: str = ""
    safe_debug_status: str = ""


@dataclass
class RunConfig:
    """Validated top-level run configuration."""

    run_date: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    pilot_mode: bool = False
    editor_row_target: int = 10
    pilot_row_min: int = 8
    pilot_row_max: int = 12
    report_qa: dict[str, Any] = field(default_factory=dict)
    candidate_history: list[dict[str, Any]] = field(default_factory=list)
    excluded_candidate_history: list[dict[str, Any]] = field(default_factory=list)
    returned_editor_rows: list[dict[str, Any]] = field(default_factory=list)
    search_optimization_trial: bool = False
    russian_edition_checks: list[dict[str, Any]] = field(default_factory=list)


# ── Validation ────────────────────────────────────────────────────────────


def validate_config(raw: Any, *, advisory_stage_keys: bool = True) -> list[str]:
    """Validate a complete run config value.

    Returns a (possibly empty) list of human-readable error messages.
    When the list is empty the config is valid.

    With *advisory_stage_keys* false, the keys of advisory weekly stages
    (the Russian-edition stage, #1181) are not checked: the runtime reader
    that exits on an error leaves them to the stage, which records a bad
    value as its own failure, so no such value can stop the run.
    """
    errors: list[str] = []

    # Top-level
    errors.extend(_validate_top_level(raw, advisory_stage_keys=advisory_stage_keys))
    if not isinstance(raw, dict):
        return errors

    # Sources
    sources: list[Any] = []
    if isinstance(raw.get("sources"), list):
        sources = raw["sources"]
    for index, source in enumerate(sources):
        source_label = _source_label(source, index)
        errors.extend(_validate_source(source, index, source_label))

    # Russian-edition checks
    russian_checks: list[Any] = []
    if isinstance(raw.get("russian_edition_checks"), list):
        russian_checks = raw["russian_edition_checks"]
    for index, check in enumerate(russian_checks):
        check_label = _check_label(check, index)
        errors.extend(_validate_russian_check(check, index, check_label))

    return errors


def _validate_top_level(raw: Any, *, advisory_stage_keys: bool = True) -> list[str]:
    errors: list[str] = []

    if not isinstance(raw, dict):
        return ["top-level config must be a JSON object"]

    for key in _TOP_LEVEL_REQUIRED_KEYS:
        if key not in raw:
            errors.append(f"top-level config: missing required key '{key}'")

    for key, value in raw.items():
        if key not in _TOP_LEVEL_KNOWN_KEYS:
            errors.append(f"top-level config: unknown key '{key}'")
            continue
        expected_type = _TOP_LEVEL_KEY_TYPES.get(key)
        if expected_type is not None and not isinstance(value, expected_type):
            actual = type(value).__name__
            expected = _type_name(expected_type)
            errors.append(f"top-level config: '{key}' must be {expected}, got {actual}")
    if COLLECTION_WEEKDAY_FIELD in raw and (weekday_error := collection_weekday_error(raw[COLLECTION_WEEKDAY_FIELD])):
        errors.append(f"top-level config: {weekday_error}")
    if STREAK_THRESHOLD_CONFIG_KEY in raw and (
        streak_error := streak_threshold_error(raw[STREAK_THRESHOLD_CONFIG_KEY])
    ):
        errors.append(f"top-level config: {streak_error}")
    if advisory_stage_keys:
        errors.extend(f"top-level config: {error}" for error in russian_edition_config_errors(raw))
    if isinstance(raw.get(STATE_BACKEND_CONFIG_KEY), str) and raw[STATE_BACKEND_CONFIG_KEY] not in STATE_BACKENDS:
        errors.append(
            f"top-level config: '{STATE_BACKEND_CONFIG_KEY}' must be one of {', '.join(STATE_BACKENDS)}, "
            f"got {raw[STATE_BACKEND_CONFIG_KEY]!r}"
        )

    return errors


def _validate_source(source: Any, index: int, label: str) -> list[str]:
    errors: list[str] = []

    if not isinstance(source, dict):
        errors.append(f"{label}: must be a JSON object")
        return errors

    for key in _SOURCE_REQUIRED_KEYS:
        if key not in source:
            errors.append(f"{label}: missing required key '{key}'")

    for key, value in source.items():
        if key not in _SOURCE_KNOWN_KEYS:
            errors.append(f"{label}: unknown key '{key}'")
            continue
        expected_type = _SOURCE_KEY_TYPES.get(key)
        if expected_type is not None and not isinstance(value, expected_type):
            actual = type(value).__name__
            expected = _type_name(expected_type)
            errors.append(f"{label}: '{key}' must be {expected}, got {actual}")

    if "artifact_freshness" in source and source["artifact_freshness"] not in ALL_FRESHNESS_VALUES:
        allowed = ", ".join(ALL_FRESHNESS_VALUES)
        errors.append(f"{label}: 'artifact_freshness' must be one of: {allowed}")

    helper_file_declared = "helper_metadata_file" in source
    helper_digest_declared = "helper_metadata_sha256" in source
    if helper_file_declared != helper_digest_declared:
        errors.append(f"{label}: 'helper_metadata_file' and 'helper_metadata_sha256' must be declared together")
    if helper_file_declared and helper_digest_declared:
        helper_file = source.get("helper_metadata_file")
        helper_digest = source.get("helper_metadata_sha256")
        if not isinstance(helper_file, str) or not helper_file.strip():
            errors.append(f"{label}: 'helper_metadata_file' must be non-empty when supplied")
        if not isinstance(helper_digest, str) or not helper_digest.strip():
            errors.append(f"{label}: 'helper_metadata_sha256' must be non-empty when supplied")
        elif re.fullmatch(r"[0-9a-f]{64}", helper_digest) is None:
            errors.append(f"{label}: 'helper_metadata_sha256' must be exactly 64 lowercase hexadecimal characters")

    if "entry_scope_selector" in source:
        selector = source["entry_scope_selector"]
        if isinstance(selector, str) and not selector.strip():
            errors.append(f"{label}: 'entry_scope_selector' must be nonblank when supplied")
        elif isinstance(selector, str):
            try:
                validate_entry_scope_selector(selector)
            except ValueError as exc:
                errors.append(f"{label}: {exc}")
    if "visible_title_selector" in source:
        selector = source["visible_title_selector"]
        if isinstance(selector, str) and not selector.strip():
            errors.append(f"{label}: 'visible_title_selector' must be nonblank when supplied")
        elif isinstance(selector, str):
            try:
                validate_visible_title_selector(selector)
            except ValueError as exc:
                errors.append(f"{label}: {exc}")
    if "ranking_source_statement" in source:
        statement = source["ranking_source_statement"]
        if isinstance(statement, str) and not statement.strip():
            errors.append(f"{label}: 'ranking_source_statement' must be nonblank when supplied")

    for field_name in ("expected_table_headers", "expected_visible_markers", "forbidden_visible_markers"):
        markers = source.get(field_name)
        if markers is not None and isinstance(markers, list):
            if not markers or any(not isinstance(value, str) or not value.strip() for value in markers):
                errors.append(f"{label}: '{field_name}' must contain non-empty strings")
    if "ranking_rank_source" in source:
        rank_source = str(source.get("ranking_rank_source", "")).strip().casefold()
        if rank_source not in RANKING_RANK_SOURCES:
            errors.append(f"{label}: ranking_rank_source must be one of: {', '.join(RANKING_RANK_SOURCES)}")
    if source.get("metadata_ranking_table") is True:
        ranking_row_contract = bool(str(source.get("ranking_row_selector", "")).strip())
        if ranking_row_contract:
            required_ranking = (
                "ranking_row_selector",
                "ranking_title_selector",
                "ranking_author_selector",
                "ranking_category_marker",
            )
            missing_ranking = [field for field in required_ranking if not str(source.get(field, "")).strip()]
            rank_source = str(source.get("ranking_rank_source", "selector")).strip().casefold()
            if rank_source == "selector" and not str(source.get("ranking_rank_selector", "")).strip():
                missing_ranking.append("ranking_rank_selector when ranking_rank_source is selector")
            detail_supplies_isbn = "isbn" in ranking_detail_requested_fields(source)
            if not detail_supplies_isbn and not any(
                str(source.get(field, "")).strip()
                for field in (
                    "ranking_identifier_selector",
                    "ranking_identifier_attribute",
                    "ranking_identifier_pattern",
                )
            ):
                missing_ranking.append(
                    "one of ranking_identifier_selector, ranking_identifier_attribute, or ranking_identifier_pattern"
                )
            if missing_ranking:
                errors.append(f"{label}: metadata_ranking_table requires " + ", ".join(missing_ranking))
        elif not source.get("expected_table_headers") or not source.get("expected_visible_markers"):
            errors.append(
                f"{label}: metadata_ranking_table requires expected_table_headers and expected_visible_markers"
            )
    contract = str(source.get("contract_family", "")).strip().casefold()
    if contract and contract not in CONTRACT_FAMILIES:
        errors.append(f"{label}: contract_family must be one of: {', '.join(CONTRACT_FAMILIES)}")
    if contract == "bookinfo-weekly-json":
        from autoscout.bookinfo_ranking import BookInfoError
        from autoscout.bookinfo_ranking import validate_declaration

        try:
            validate_declaration(source)
        except BookInfoError as exc:
            errors.append(f"{label}: {exc}")
    if contract == "ipc-top40-csv":
        from autoscout.ipc_top40_ranking import IpcTop40Error
        from autoscout.ipc_top40_ranking import validate_declaration as validate_ipc_top40_declaration

        try:
            validate_ipc_top40_declaration(source)
        except IpcTop40Error as exc:
            errors.append(f"{label}: {exc}")
    if contract == "perlentaucher-daily-digest":
        from autoscout.perlentaucher_digest import PerlentaucherError
        from autoscout.perlentaucher_digest import validate_declaration as validate_perlentaucher_declaration

        try:
            validate_perlentaucher_declaration(source)
        except PerlentaucherError as exc:
            errors.append(f"{label}: {exc}")
    if contract == "kirkus-coming-soon":
        from autoscout.kirkus_listing import KirkusError
        from autoscout.kirkus_listing import validate_declaration as validate_kirkus_declaration

        try:
            validate_kirkus_declaration(source)
        except KirkusError as exc:
            errors.append(f"{label}: {exc}")
    if contract == "pm-deal-email":
        from autoscout.pm_deal_capture import PmDealCaptureError
        from autoscout.pm_deal_capture import validate_declaration as validate_pm_deal_declaration

        try:
            validate_pm_deal_declaration(source)
        except PmDealCaptureError as exc:
            errors.append(f"{label}: {exc}")
    if contract == "pw-issue-pdf":
        from autoscout.pw_issue_capture import PwIssueCaptureError
        from autoscout.pw_issue_capture import validate_declaration as validate_pw_issue_declaration

        try:
            validate_pw_issue_declaration(source)
        except PwIssueCaptureError as exc:
            errors.append(f"{label}: {exc}")
    if contract == "award-list":
        from autoscout.award_list import validate_declaration as validate_award_list_declaration

        award_list_error = validate_award_list_declaration(source)
        if award_list_error:
            errors.append(f"{label}: {award_list_error}")
    if contract == "ranking-table" and source.get("metadata_exact_book_list") is True:
        errors.append(f"{label}: ranking-table must not set metadata_exact_book_list")
    ranking_contract = contract == "ranking-table" or (
        source.get("metadata_ranking_table") is True and bool(str(source.get("ranking_row_selector", "")).strip())
    )
    # An absent language used to be read as English wherever the translation
    # rules are applied. A non-English source onboarded without a declared
    # language silently lost its helper contract (Issue #645 did this to
    # three German sources in the generic family), and different families
    # even disagreed on what the absence meant. The declaration is therefore
    # required on every configured source, not only on ranking tables.
    if not str(source.get("language", "")).strip():
        errors.append(f"{label}: every configured source requires a nonblank language")
    if ranking_contract:
        try:
            ranking_error = validate_ranking_contract_metadata(source)
        except (TypeError, ValueError) as exc:
            ranking_error = str(exc)
        if ranking_error:
            errors.append(f"{label}: {ranking_error}")
    no_entries_error = validate_no_entries_declaration(source)
    if no_entries_error:
        errors.append(f"{label}: {no_entries_error}")
    between_editions_error = validate_between_editions_declaration(source)
    if between_editions_error:
        errors.append(f"{label}: {between_editions_error}")
    annual_edition_error = validate_annual_edition_declaration(source)
    if annual_edition_error:
        errors.append(f"{label}: {annual_edition_error}")
    source_week_error = validate_source_week_freshness_declaration(source)
    if source_week_error:
        errors.append(f"{label}: {source_week_error}")
    if "adult_nonfiction_exclusions" in source:
        exclusions = source["adult_nonfiction_exclusions"]
        if not isinstance(exclusions, list) or any(
            not isinstance(value, str) or not value.strip() for value in exclusions
        ):
            errors.append(f"{label}: adult_nonfiction_exclusions must contain non-empty strings")

    for alias, canonical in (("signal_family", "family"), ("refresh_cadence", "check_frequency")):
        if alias in source and canonical in source and str(source[alias]).strip() != str(source[canonical]).strip():
            errors.append(f"{label}: '{alias}' must equal '{canonical}' when both are supplied")

    if "max_response_bytes" in source:
        max_bytes = source.get("max_response_bytes")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= 2_000_000:
            errors.append(f"{label}: max_response_bytes must be an integer between 1 and 2000000")

    if source.get("audible_explicit_chart") is True:
        parsed = urlparse(str(source.get("url", "")))
        if parsed.query or parsed.fragment:
            errors.append(f"{label}: audible_explicit_chart URL must be the base chart URL without query or fragment")
        if source.get("family") != "audiobook ranking":
            errors.append(f"{label}: audible_explicit_chart requires family audiobook ranking")
        max_bytes = source.get("max_response_bytes", 0)
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 750_000 <= max_bytes <= 2_000_000:
            errors.append(f"{label}: audible_explicit_chart requires max_response_bytes between 750000 and 2000000")
        # The range marker is locale-exact: English charts require
        # "1 - 20 of 100 results" and the German chart requires
        # "1 - 20 von 100 Ergebnissen" (Issue #366).
        range_marker = str(source.get("expected_range_marker", "")).strip()
        expected_range_marker = (
            "1 - 20 von 100 Ergebnissen"
            if str(source.get("language", "")).strip().casefold() == "de"
            else "1 - 20 of 100 results"
        )
        if range_marker != expected_range_marker:
            errors.append(f"{label}: audible_explicit_chart requires exact expected_range_marker")
        if str(source.get("expected_signal_strength", "")).strip() != "top ranking":
            errors.append(f"{label}: audible_explicit_chart requires top ranking signal strength")

    if source.get("amazon_charts_weekly") is True:
        parsed = urlparse(str(source.get("url", "")))
        language = str(source.get("language", "")).strip().casefold()
        # The marketplace is a host-and-language pair enforced by the
        # module-level registry: a German chart must declare language de on
        # www.amazon.de, and the English charts must declare language en on
        # www.amazon.com or www.amazon.co.uk (Issue #378).
        if (
            parsed.scheme != "https"
            or parsed.netloc not in AMAZON_CHARTS_MARKETPLACE_LANGUAGE
            or language != AMAZON_CHARTS_MARKETPLACE_LANGUAGE[parsed.netloc]
        ):
            errors.append(
                f"{label}: amazon_charts_weekly requires an https Amazon charts landing URL on a supported "
                f"marketplace with a matching language (www.amazon.com/en, www.amazon.de/de, or "
                f"www.amazon.co.uk/en)"
            )
        elif parsed.path != "/charts":
            errors.append(f"{label}: amazon_charts_weekly requires the base /charts landing path")
        if parsed.query or parsed.fragment:
            errors.append(f"{label}: amazon_charts_weekly URL must be the landing URL without query or fragment")
        if source.get("family") != "bestseller/ranking":
            errors.append(f"{label}: amazon_charts_weekly requires family bestseller/ranking")
        if source.get("metadata_exact_book_list") is not True:
            errors.append(f"{label}: amazon_charts_weekly requires metadata_exact_book_list true")
        max_bytes = source.get("max_response_bytes", 0)
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 750_000 <= max_bytes <= 2_000_000:
            errors.append(f"{label}: amazon_charts_weekly requires max_response_bytes between 750000 and 2000000")
        if str(source.get("expected_signal_strength", "")).strip() != "top ranking":
            errors.append(f"{label}: amazon_charts_weekly requires top ranking signal strength")
        heading_words = [word for word in str(source.get("expected_page_heading", "")).strip().split() if word]
        if len(heading_words) < 2:
            errors.append(f"{label}: amazon_charts_weekly requires expected_page_heading with at least two words")
        # The dated URL path is an explicit English Amazon path even on
        # non-English marketplaces, so it cannot be derived from the display
        # heading (Issue #378).
        chart_segment = str(source.get("chart_path_segment", "")).strip().strip("/")
        if not chart_segment:
            errors.append(f"{label}: amazon_charts_weekly requires an explicit chart_path_segment")
        elif re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*(?:/[a-z0-9]+(?:-[a-z0-9]+)*)+", chart_segment) is None:
            errors.append(
                f"{label}: amazon_charts_weekly chart_path_segment requires at least two lowercase "
                f"slash-separated path segments"
            )
        author_marker = str(source.get("author_marker", "")).strip()
        if not author_marker:
            errors.append(f"{label}: amazon_charts_weekly requires a nonblank author_marker")
        elif re.search(r"\s", author_marker) is not None:
            errors.append(f"{label}: amazon_charts_weekly author_marker must be a single nonblank token")

    if source.get("metadata_exact_book_list") is True:
        missing: list[str] = []
        if not str(source.get("url", "")).strip():
            missing.append("url")
        if not str(source.get("expected_page_heading", "")).strip():
            missing.append("expected_page_heading")
        if not str(source.get("expected_signal_strength", "")).strip():
            missing.append("expected_signal_strength")
        if not str(source.get("eligible_candidate_category", "")).strip():
            missing.append("eligible_candidate_category")
        if not str(source.get("signal_family") or source.get("family", "")).strip():
            missing.append("signal_family")
        if missing:
            errors.append(f"{label}: metadata_exact_book_list requires nonblank " + ", ".join(missing))
    if "entry_section_heading" in source:
        section_heading = source["entry_section_heading"]
        if isinstance(section_heading, str) and not section_heading.strip():
            errors.append(f"{label}: 'entry_section_heading' must be nonblank when supplied")
        if source.get("metadata_exact_book_list") is not True:
            errors.append(f"{label}: entry_section_heading applies only to an exact-book-list source")

    # Probation field validation.
    if "probation_evidence_window_weeks" in source:
        pew = source["probation_evidence_window_weeks"]
        if isinstance(pew, bool) or not isinstance(pew, int) or pew < 1:
            errors.append(
                f"{label}: probation_evidence_window_weeks must be a positive integer, got {type(pew).__name__} {pew}"
            )

    if "probation_started_at" in source:
        psa = source["probation_started_at"]
        if not isinstance(psa, str) or not psa.strip():
            errors.append(f"{label}: probation_started_at must be a nonblank ISO date string")
        else:
            from datetime import date as _date

            try:
                _date.fromisoformat(psa.strip())
            except ValueError:
                errors.append(f"{label}: probation_started_at '{psa}' is not a valid ISO date (YYYY-MM-DD)")

    return errors


def _validate_russian_check(check: Any, index: int, label: str) -> list[str]:
    errors: list[str] = []

    if not isinstance(check, dict):
        errors.append(f"{label}: must be a JSON object")
        return errors

    for key, value in check.items():
        if key not in _RUSSIAN_CHECK_KNOWN_KEYS:
            errors.append(f"{label}: unknown key '{key}'")
            continue
        expected_type = _RUSSIAN_CHECK_KEY_TYPES.get(key)
        if expected_type is not None and not isinstance(value, expected_type):
            actual = type(value).__name__
            expected = _type_name(expected_type)
            errors.append(f"{label}: '{key}' must be {expected}, got {actual}")

    return errors


def _source_label(source: Any, index: int) -> str:
    if isinstance(source, dict):
        name = source.get("name")
        if name:
            return f"source[{index}] '{name}'"
    return f"source[{index}]"


def _check_label(check: Any, index: int) -> str:
    if isinstance(check, dict):
        name = check.get("retailer") or check.get("name")
        if name:
            return f"russian-check[{index}] '{name}'"
    return f"russian-check[{index}]"


def _type_name(expected: type | tuple[type, ...]) -> str:
    if isinstance(expected, tuple):
        return " or ".join(t.__name__ for t in expected)
    return expected.__name__


# ── Portfolio resolution ──────────────────────────────────────────────────


def resolve_weekly_portfolio_or_raise(raw: dict[str, Any], config_path: Path) -> dict[str, Any]:
    """Resolve a weekly portfolio reference and raise structured input errors."""
    portfolio_ref = raw.get("weekly_portfolio")
    if not portfolio_ref:
        return raw

    portfolio_path = (config_path.parent / portfolio_ref).resolve()
    if not portfolio_path.exists():
        raise ValueError(f"weekly portfolio file not found: {portfolio_path}")
    portfolio = read_json(portfolio_path)
    if not isinstance(portfolio, dict):
        raise ValueError(f"weekly portfolio file is not a JSON object: {portfolio_path}")

    portfolio_sources: list[dict[str, Any]] = portfolio.get("sources", [])
    if not isinstance(portfolio_sources, list):
        raise ValueError(f"weekly portfolio 'sources' must be a list: {portfolio_path}")

    overlay_sources: list[dict[str, Any]] = raw.get("sources", [])
    if not isinstance(overlay_sources, list):
        raise ValueError("config source overlays must be a list")

    portfolio_by_name: dict[str, dict[str, Any]] = {}
    for index, base_source in enumerate(portfolio_sources):
        if not isinstance(base_source, dict):
            raise ValueError(f"weekly portfolio source[{index}] is not a JSON object: {portfolio_path}")
        name = str(base_source.get("name", "")).strip()
        if not name:
            raise ValueError(f"weekly portfolio source[{index}] missing required key 'name': {portfolio_path}")
        if name in portfolio_by_name:
            raise ValueError(f"weekly portfolio has duplicate source name '{name}': {portfolio_path}")
        portfolio_by_name[name] = base_source

    overlays_by_name: dict[str, dict[str, Any]] = {}
    for index, overlay in enumerate(overlay_sources):
        if not isinstance(overlay, dict):
            raise ValueError(f"config source overlay[{index}] must be a JSON object")
        if not overlay:
            continue
        target_name = str(overlay.get("portfolio_name") or overlay.get("name", "")).strip()
        if not target_name:
            raise ValueError(
                f"config source overlay[{index}] must include 'name' or 'portfolio_name' to match weekly portfolio source"
            )
        if target_name not in portfolio_by_name:
            raise ValueError(f"config source overlay[{index}] names unknown weekly portfolio source '{target_name}'")
        if target_name in overlays_by_name:
            raise ValueError(f"config has duplicate source overlay for weekly portfolio source '{target_name}'")
        overlay_for_merge = dict(overlay)
        overlay_for_merge.pop("portfolio_name", None)
        overlays_by_name[target_name] = overlay_for_merge

    resolved_sources: list[dict[str, Any]] = []
    for base_source in portfolio_sources:
        name = str(base_source.get("name", "")).strip()
        overlay = overlays_by_name.get(name, {})
        resolved_sources.append({**base_source, **overlay})

    resolved = dict(raw)
    resolved["sources"] = resolved_sources
    resolved.pop("weekly_portfolio", None)
    return resolved


def resolve_weekly_portfolio(raw: dict[str, Any], config_path: Path) -> dict[str, Any]:
    """Resolve a weekly portfolio while preserving the CLI exit contract."""
    try:
        return resolve_weekly_portfolio_or_raise(raw, config_path)
    except json.JSONDecodeError:
        raise
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)


# ── Load + validate ───────────────────────────────────────────────────────


def load_validated_config(config_path: Path) -> dict[str, Any]:
    """Load a JSON config file, resolve any portfolio reference, validate
    it, and return the parsed (resolved) dict.

    If validation discovers errors this function prints them to stderr
    and calls ``sys.exit(1)`` — it is intended to be used at CLI
    entry points. Weekly runs read the config through it too, so the keys
    of advisory stages are left to those stages (#1181): a bad
    ``russian_edition_*`` value fails that stage, never the run.
    """
    raw = read_json(config_path, default={})
    if not isinstance(raw, dict):
        print("config must be a JSON object", file=sys.stderr)
        sys.exit(1)
    resolved = resolve_weekly_portfolio(raw, config_path)
    errors = validate_config(resolved, advisory_stage_keys=False)
    if errors:
        print(f"config validation failed ({len(errors)} error(s))", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        sys.exit(1)
    assert isinstance(resolved, dict)
    return resolved
