"""Schema, validation, rendering, and publication support for monthly reports.

This module owns the derived monthly artifact contract.
The discovery orchestration module supplies the report model and semantic evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from datetime import timedelta
from pathlib import Path
from typing import Any

from autoscout.safe_artifacts import SafeArtifactError
from autoscout.safe_artifacts import publish_bundle
from autoscout.safe_artifacts import resolve_storage_destination
from autoscout.source_probe import normalized_url
from autoscout.source_probe import valid_http_url

MONTHLY_ARTIFACT_TYPE = "monthly-source-discovery"
MONTHLY_HISTORY_ARTIFACT_TYPE = "monthly-source-discovery-history"
MONTHLY_SCHEMA_VERSION = 1
MONTHLY_HISTORY_SCHEMA_VERSION = 1
DEFAULT_MONTHLY_OUTPUT_DIR = Path("data/monthly-source-discovery")
DEFAULT_MONTHLY_REPORT_FILENAME = "monthly-source-discovery.json"
DEFAULT_MONTHLY_MARKDOWN_FILENAME = "monthly-source-discovery.md"
DEFAULT_MONTHLY_HISTORY_FILENAME = "monthly-source-discovery-history.json"
_MONTH_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
_HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
MONTHLY_SOURCE_GAP_SUCCESS = "success"
MONTHLY_SOURCE_GAP_INSUFFICIENT_METADATA = "insufficient_metadata"
MONTHLY_SOURCE_GAP_STATUSES = frozenset({MONTHLY_SOURCE_GAP_SUCCESS, MONTHLY_SOURCE_GAP_INSUFFICIENT_METADATA})

MONTHLY_STATUS_NEW_CANDIDATES = "new_candidates_available"
MONTHLY_STATUS_BANKED_ONLY = "only_banked_candidates_available"
MONTHLY_STATUS_NO_CANDIDATES = "no_candidates_this_month"
MONTHLY_STATUS_INSUFFICIENT_METADATA = "insufficient_portfolio_metadata"
_MONTHLY_NEXT_ACTIONS = {
    MONTHLY_STATUS_NEW_CANDIDATES: "Review the new source candidates and choose any source for a separate onboarding decision.",
    MONTHLY_STATUS_BANKED_ONLY: "Review the banked source candidates; no new source candidate was found this month.",
    MONTHLY_STATUS_NO_CANDIDATES: "No source candidate needs action this month; keep the rolling onboarding goal under review.",
    MONTHLY_STATUS_INSUFFICIENT_METADATA: "Supply canonical signal-family metadata for the portfolio, then run the next monthly report.",
}
MONTHLY_STATUSES = frozenset(_MONTHLY_NEXT_ACTIONS)
_REPORT_REQUIRED_FIELDS = frozenset(
    {
        "artifact_type",
        "version",
        "operation_month",
        "operation_identity",
        "evidence_digest",
        "status",
        "source_gap_status",
        "current_candidate_urls",
        "observed_candidate_urls",
        "new_candidate_urls",
        "banked_candidate_urls",
        "ledger_rejected_or_deferred_urls",
        "coverage_gaps",
        "family_coverage",
        "sources_without_signal_family",
        "onboarding_progress",
        "next_action",
    }
)
_HISTORY_ENTRY_FIELDS = frozenset(
    {
        "operation_month",
        "operation_identity",
        "evidence_digest",
        "report_sha256",
        "markdown_sha256",
        "status",
        "observed_candidate_urls",
        "new_candidate_urls",
        "banked_candidate_urls",
        "ledger_rejected_or_deferred_urls",
    }
)
_PROGRESS_FIELDS = frozenset(
    {
        "goal",
        "window_start",
        "window_end",
        "verified_completed_count",
        "verified_completed_count_semantics",
        "remaining_to_goal",
        "evidence_status",
        "configured_source_count",
        "configured_without_canonical_onboarding_evidence_count",
        "configured_without_canonical_onboarding_evidence_urls",
        "verified_completed_sources",
        "accepted_proposal_signal",
        "operation_month",
    }
)


class MonthlyInputError(ValueError):
    """A monthly input is missing, malformed, or inconsistent."""


class MonthlyArtifactError(ValueError):
    """A monthly derived publication is missing, malformed, or inconsistent."""


@dataclass(frozen=True)
class MonthlyPublicationPaths:
    """Resolved paths for one monthly report, Markdown file, and history index."""

    report: Path
    markdown: Path
    history: Path


def json_text(value: Mapping[str, Any] | dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_parts(parts: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for label, content in parts:
        label_bytes = label.encode("utf-8")
        digest.update(len(label_bytes).to_bytes(8, "big"))
        digest.update(label_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def parse_operation_month(value: Any) -> tuple[str, date] | None:
    if not isinstance(value, str) or _MONTH_RE.fullmatch(value) is None:
        return None
    year, month = (int(part) for part in value.split("-"))
    return value, date(year, month, 1)


def month_shift(value: date, months: int) -> date:
    index = value.year * 12 + value.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def month_end(value: date) -> date:
    return month_shift(value, 1) - timedelta(days=1)


def derive_monthly_status(
    source_gap_status: str, new_candidate_urls: Collection[str], banked_candidate_urls: Collection[str]
) -> str:
    """Derive the canonical monthly status from upstream status and candidates."""
    if source_gap_status == MONTHLY_SOURCE_GAP_INSUFFICIENT_METADATA:
        if new_candidate_urls:
            raise MonthlyArtifactError("monthly report has candidates despite insufficient source-gap metadata")
        return MONTHLY_STATUS_INSUFFICIENT_METADATA
    if source_gap_status != MONTHLY_SOURCE_GAP_SUCCESS:
        raise MonthlyArtifactError(f"monthly report has an unsupported source-gap status: {source_gap_status}")
    if new_candidate_urls:
        return MONTHLY_STATUS_NEW_CANDIDATES
    if banked_candidate_urls:
        return MONTHLY_STATUS_BANKED_ONLY
    return MONTHLY_STATUS_NO_CANDIDATES


def monthly_next_action(status: str) -> str:
    """Return the canonical editor action for one monthly status."""
    try:
        return _MONTHLY_NEXT_ACTIONS[status]
    except KeyError as exc:
        raise MonthlyArtifactError(f"monthly report has an unsupported status: {status}") from exc


def canonical_url(value: Any, error_type: type[ValueError], label: str) -> str:
    if not valid_http_url(value):
        raise error_type(f"{label} must be a valid HTTP or HTTPS URL")
    return normalized_url(str(value))


def _validate_url_list(raw: Any, label: str, error_type: type[ValueError]) -> list[str]:
    if not isinstance(raw, list):
        raise error_type(f"{label} must be a list")
    urls = [canonical_url(value, error_type, f"{label} URL") for value in raw]
    if any(value != canonical for value, canonical in zip(raw, urls, strict=True)):
        raise error_type(f"{label} must contain canonical URLs")
    if urls != sorted(set(urls)):
        raise error_type(f"{label} must contain sorted, unique normalized URLs")
    return urls


def monthly_evidence_digest(report: Mapping[str, Any]) -> str:
    semantic_report = {
        key: value for key, value in report.items() if key not in {"operation_identity", "evidence_digest"}
    }
    return digest_parts(
        [
            ("operation-month", str(report["operation_month"]).encode("utf-8")),
            ("semantic-report-evidence", json_text(semantic_report).encode("utf-8")),
        ]
    )


def monthly_operation_identity(operation_month: str, evidence_digest: str) -> str:
    return "monthly-source-discovery-" + sha256(f"{operation_month}\x00{evidence_digest}".encode())[:32]


def validate_history(path: Path) -> list[dict[str, Any]]:
    """Read and validate history, returning only entries used by orchestration."""
    if path.is_symlink():
        raise MonthlyArtifactError("monthly history must not be a symlink")
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MonthlyArtifactError(f"monthly history is malformed: {exc}") from exc
    if not isinstance(value, Mapping):
        raise MonthlyArtifactError("monthly history must be a JSON object")
    if set(value) != {"artifact_type", "version", "entries"}:
        raise MonthlyArtifactError("monthly history has an invalid field shape")
    if (
        value.get("artifact_type") != MONTHLY_HISTORY_ARTIFACT_TYPE
        or value.get("version") != MONTHLY_HISTORY_SCHEMA_VERSION
    ):
        raise MonthlyArtifactError("monthly history has an unsupported artifact type or version")
    raw_entries = value.get("entries")
    if not isinstance(raw_entries, list):
        raise MonthlyArtifactError("monthly history entries must be a list")
    entries: list[dict[str, Any]] = []
    months: set[str] = set()
    identities: set[str] = set()
    for index, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, Mapping) or set(raw_entry) != _HISTORY_ENTRY_FIELDS:
            raise MonthlyArtifactError(f"monthly history entry {index} has an invalid field shape")
        entry = dict(raw_entry)
        operation_month = entry.get("operation_month")
        if not isinstance(operation_month, str) or parse_operation_month(operation_month) is None:
            raise MonthlyArtifactError(f"monthly history entry {index} has an invalid operation month")
        operation_identity = entry.get("operation_identity")
        evidence_digest = entry.get("evidence_digest")
        if not isinstance(operation_identity, str) or not operation_identity.strip():
            raise MonthlyArtifactError(f"monthly history entry {index} has no operation identity")
        if not isinstance(evidence_digest, str) or _HEX_DIGEST_RE.fullmatch(evidence_digest) is None:
            raise MonthlyArtifactError(f"monthly history entry {index} has an invalid evidence digest")
        if operation_identity != monthly_operation_identity(operation_month, evidence_digest):
            raise MonthlyArtifactError(f"monthly history entry {index} has an invalid operation identity")
        if operation_month in months:
            raise MonthlyArtifactError(f"monthly history has duplicate operation month: {operation_month}")
        if operation_identity in identities:
            raise MonthlyArtifactError(f"monthly history has duplicate operation identity: {operation_identity}")
        for field in (
            "new_candidate_urls",
            "banked_candidate_urls",
            "ledger_rejected_or_deferred_urls",
            "observed_candidate_urls",
        ):
            entry[field] = _validate_url_list(entry[field], f"history entry {index} {field}", MonthlyArtifactError)
        if entry["status"] not in MONTHLY_STATUSES:
            raise MonthlyArtifactError(f"monthly history entry {index} has an invalid status")
        for field in ("report_sha256", "markdown_sha256"):
            if not isinstance(entry[field], str) or _HEX_DIGEST_RE.fullmatch(entry[field]) is None:
                raise MonthlyArtifactError(f"monthly history entry {index} has an invalid {field}")
        months.add(operation_month)
        identities.add(operation_identity)
        entries.append(entry)
    entries.sort(key=lambda entry: (entry["operation_month"], entry["operation_identity"]))
    return entries


def _canonical_date(value: Any, label: str) -> date:
    if not isinstance(value, str):
        raise MonthlyArtifactError(f"{label} must be a date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise MonthlyArtifactError(f"{label} must be a date") from exc
    if parsed.isoformat() != value:
        raise MonthlyArtifactError(f"{label} must be canonical")
    return parsed


def _validate_counted_urls(progress: Mapping[str, Any], prefix: str, configured_count: int) -> list[str]:
    urls = _validate_url_list(progress[f"{prefix}_urls"], f"monthly progress {prefix}", MonthlyArtifactError)
    count = progress[f"{prefix}_count"]
    if isinstance(count, bool) or not isinstance(count, int) or count != len(urls) or count > configured_count:
        raise MonthlyArtifactError(f"monthly progress {prefix} count disagrees with its URLs")
    return urls


def _validate_progress_sources(
    sources: Any,
    fields: set[str],
    date_field: str,
    label: str,
    date_window: tuple[date, date] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(sources, list):
        raise MonthlyArtifactError(f"{label} must be a list")
    validated: list[dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, Mapping) or set(source) != fields:
            raise MonthlyArtifactError(f"{label} has an invalid field shape")
        if not isinstance(source["proposal_id"], str) or not source["proposal_id"].strip():
            raise MonthlyArtifactError(f"{label} has an invalid proposal ID")
        source_date = _canonical_date(source[date_field], f"{label} date")
        if date_window is not None and not date_window[0] <= source_date <= date_window[1]:
            raise MonthlyArtifactError(f"{label} date is outside the rolling window")
        canonical_source_url = canonical_url(source["url"], MonthlyArtifactError, label)
        if source["url"] != canonical_source_url:
            raise MonthlyArtifactError(f"{label} has a noncanonical URL")
        item = dict(source)
        item["url"] = canonical_source_url
        item[date_field] = source_date.isoformat()
        validated.append(item)
    return validated


def _require_count(value: Any, label: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or (maximum is not None and value > maximum):
        raise MonthlyArtifactError(f"monthly onboarding progress has an invalid {label}")
    return int(value)


def validate_progress(value: Any, operation_month: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _PROGRESS_FIELDS:
        raise MonthlyArtifactError("monthly onboarding progress has an invalid field shape")
    progress = dict(value)
    configured_count = _require_count(progress["configured_source_count"], "configured source count")
    if progress["goal"] != 12 or progress["operation_month"] != operation_month:
        raise MonthlyArtifactError("monthly onboarding progress has an invalid goal or month")
    evidence_status = progress["evidence_status"]
    expected_semantics = "exact" if evidence_status == "complete" else "auditable_lower_bound"
    if (
        evidence_status not in {"complete", "insufficient_evidence"}
        or progress["verified_completed_count_semantics"] != expected_semantics
    ):
        raise MonthlyArtifactError("monthly onboarding progress evidence semantics are invalid")
    parsed_month = parse_operation_month(operation_month)
    if parsed_month is None:
        raise MonthlyArtifactError("monthly onboarding progress has an invalid operation month")
    window_start = _canonical_date(progress["window_start"], "monthly onboarding window start")
    window_end = _canonical_date(progress["window_end"], "monthly onboarding window end")
    _, operation_start = parsed_month
    if window_start != month_shift(operation_start, -11) or window_end != month_end(operation_start):
        raise MonthlyArtifactError("monthly onboarding progress has an invalid rolling window")

    verified_count = _require_count(progress["verified_completed_count"], "verified count", configured_count)
    missing_count = _require_count(
        progress["configured_without_canonical_onboarding_evidence_count"], "missing-evidence count", configured_count
    )
    missing_urls = _validate_url_list(
        progress["configured_without_canonical_onboarding_evidence_urls"],
        "monthly missing-evidence URLs",
        MonthlyArtifactError,
    )
    if missing_count != len(missing_urls):
        raise MonthlyArtifactError("monthly onboarding missing-evidence count disagrees with its URLs")
    remaining = progress["remaining_to_goal"]
    if evidence_status == "complete":
        if not isinstance(remaining, int) or isinstance(remaining, bool) or remaining != max(0, 12 - verified_count):
            raise MonthlyArtifactError("complete monthly onboarding progress has an invalid remaining goal")
    elif remaining is not None:
        raise MonthlyArtifactError("incomplete monthly onboarding progress must have an unknown remaining goal")

    verified_sources = _validate_progress_sources(
        progress["verified_completed_sources"],
        {"url", "proposal_id", "onboarding_run_date"},
        "onboarding_run_date",
        "monthly verified onboarding sources",
        (window_start, window_end),
    )
    verified_urls = [source["url"] for source in verified_sources]
    if len(verified_sources) != verified_count or len(verified_urls) != len(set(verified_urls)):
        raise MonthlyArtifactError("monthly onboarding verified count disagrees with its sources")
    if set(verified_urls) & set(missing_urls) or len(set(verified_urls) | set(missing_urls)) != configured_count:
        raise MonthlyArtifactError("monthly onboarding evidence does not partition configured sources")
    if verified_sources != sorted(verified_sources, key=lambda item: (item["onboarding_run_date"], item["url"])):
        raise MonthlyArtifactError("monthly verified onboarding sources must be sorted")

    signal = progress["accepted_proposal_signal"]
    signal_fields = {
        "semantics",
        "in_window_count",
        "in_window_sources",
        "without_completion_count",
        "without_completion_urls",
        "without_accepted_proposal_count",
        "without_accepted_proposal_urls",
    }
    if not isinstance(signal, Mapping) or set(signal) != signal_fields:
        raise MonthlyArtifactError("monthly accepted-proposal signal has an invalid field shape")
    if signal["semantics"] != "accepted_proposal_in_window_not_verified_onboarding_completion":
        raise MonthlyArtifactError("monthly accepted-proposal signal has invalid semantics")
    in_window_sources = _validate_progress_sources(
        signal["in_window_sources"],
        {"url", "proposal_id", "proposal_date"},
        "proposal_date",
        "monthly accepted-proposal signal sources",
        (window_start, window_end),
    )
    in_window_count = _require_count(signal["in_window_count"], "accepted-proposal signal count", configured_count)
    in_window_urls = [source["url"] for source in in_window_sources]
    if (
        in_window_count != len(in_window_sources)
        or len(in_window_urls) != len(set(in_window_urls))
        or in_window_sources != sorted(in_window_sources, key=lambda item: (item["proposal_date"], item["url"]))
    ):
        raise MonthlyArtifactError("monthly accepted-proposal signal sources are invalid or unsorted")

    without_completion = _validate_counted_urls(signal, "without_completion", configured_count)
    without_accepted = _validate_counted_urls(signal, "without_accepted_proposal", configured_count)
    if set(without_completion) | set(without_accepted) != set(missing_urls):
        raise MonthlyArtifactError("monthly accepted-proposal signal does not partition missing evidence")
    if set(without_completion) & set(without_accepted):
        raise MonthlyArtifactError("monthly accepted-proposal signal partitions overlap")
    configured_universe = set(verified_urls) | set(missing_urls)
    if set(in_window_urls) - configured_universe:
        raise MonthlyArtifactError("monthly accepted-proposal signal is outside configured sources")
    if set(in_window_urls) & set(without_accepted):
        raise MonthlyArtifactError("monthly signal overlaps sources without accepted proposals")
    return progress


def validate_report(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _REPORT_REQUIRED_FIELDS:
        raise MonthlyArtifactError("monthly JSON report has an invalid field shape")
    report = dict(value)
    if report["artifact_type"] != MONTHLY_ARTIFACT_TYPE or report["version"] != MONTHLY_SCHEMA_VERSION:
        raise MonthlyArtifactError("monthly JSON report has an unsupported artifact type or version")
    if parse_operation_month(report["operation_month"]) is None:
        raise MonthlyArtifactError("monthly JSON report has an invalid operation month")
    if not isinstance(report["operation_identity"], str) or not report["operation_identity"].strip():
        raise MonthlyArtifactError("monthly JSON report has no operation identity")
    if not isinstance(report["evidence_digest"], str) or _HEX_DIGEST_RE.fullmatch(report["evidence_digest"]) is None:
        raise MonthlyArtifactError("monthly JSON report has an invalid evidence digest")
    if report["operation_identity"] != monthly_operation_identity(report["operation_month"], report["evidence_digest"]):
        raise MonthlyArtifactError("monthly report operation identity is not derived from semantic evidence")
    if report["status"] not in MONTHLY_STATUSES:
        raise MonthlyArtifactError("monthly JSON report has an unsupported status")
    if report["source_gap_status"] not in MONTHLY_SOURCE_GAP_STATUSES:
        raise MonthlyArtifactError("monthly JSON report has an invalid source-gap status")
    for field in (
        "current_candidate_urls",
        "observed_candidate_urls",
        "new_candidate_urls",
        "banked_candidate_urls",
        "ledger_rejected_or_deferred_urls",
    ):
        report[field] = _validate_url_list(report[field], f"monthly report {field}", MonthlyArtifactError)
    if not isinstance(report["coverage_gaps"], list) or not isinstance(report["family_coverage"], Mapping):
        raise MonthlyArtifactError("monthly JSON report has invalid coverage fields")
    if not isinstance(report["sources_without_signal_family"], list):
        raise MonthlyArtifactError("monthly JSON report has invalid unclassified-source fields")
    validate_progress(report["onboarding_progress"], report["operation_month"])
    if report["evidence_digest"] != monthly_evidence_digest(report):
        raise MonthlyArtifactError("monthly JSON report has an invalid evidence digest")
    expected_status = derive_monthly_status(
        report["source_gap_status"], report["new_candidate_urls"], report["banked_candidate_urls"]
    )
    if report["status"] != expected_status:
        raise MonthlyArtifactError("monthly JSON report has inconsistent status and candidate lists")
    if not isinstance(report["next_action"], str) or report["next_action"] != monthly_next_action(report["status"]):
        raise MonthlyArtifactError("monthly JSON report has a noncanonical next_action")
    return report


def render_monthly_markdown(report: Mapping[str, Any]) -> str:
    """Render the concise editor report from the canonical JSON report model."""

    def bullets(title: str, values: list[str]) -> list[str]:
        lines = [f"## {title}", ""]
        lines.extend(f"- {value}" for value in values)
        if not values:
            lines.append("- None")
        lines.append("")
        return lines

    lines = [
        f"# Monthly source discovery: {report.get('operation_month', '')}",
        "",
        f"- Status: `{report.get('status', '')}`",
        f"- Operation identity: `{report.get('operation_identity', '')}`",
        f"- Evidence digest: `{report.get('evidence_digest', '')}`",
        "",
    ]
    lines.extend(bullets("New candidate URLs", list(report.get("new_candidate_urls", []))))
    lines.extend(bullets("Previously banked candidate URLs", list(report.get("banked_candidate_urls", []))))
    lines.extend(
        bullets(
            "Ledger-known rejected or deferred URLs",
            list(report.get("ledger_rejected_or_deferred_urls", [])),
        )
    )
    lines.append("## Current signal-family coverage gaps")
    lines.append("")
    coverage_gaps = report.get("coverage_gaps", [])
    if isinstance(coverage_gaps, list) and coverage_gaps:
        for gap in coverage_gaps:
            if isinstance(gap, Mapping):
                reasons = ", ".join(str(reason) for reason in gap.get("weakness_reasons", []))
                lines.append(f"- {gap.get('signal_family', '')}: {reasons}")
    else:
        lines.append("- None")
    lines.append("")
    progress = report.get("onboarding_progress", {})
    if isinstance(progress, Mapping):
        evidence_status = progress.get("evidence_status")
        lines.extend(
            [
                "## Progress toward 12 onboarded sources in 12 months",
                "",
                (
                    f"- Verified completed: at least {progress.get('verified_completed_count', 0)} verified"
                    if evidence_status == "insufficient_evidence"
                    else f"- Verified completed: {progress.get('verified_completed_count', 0)} verified"
                ),
                f"- Goal: {progress.get('goal', 12)}",
                f"- Rolling window: {progress.get('window_start', '')} to {progress.get('window_end', '')}",
            ]
        )
        if evidence_status == "insufficient_evidence":
            lines.append("- Remaining: unknown because canonical evidence is incomplete")
        else:
            lines.append(f"- Remaining to goal: {progress.get('remaining_to_goal', 0)}")
        signal = progress.get("accepted_proposal_signal", {})
        if isinstance(signal, Mapping):
            lines.extend(
                [
                    "- Accepted-proposal signal: in-window accepted proposals, not verified onboarding completion.",
                    f"- Accepted-proposal signal count: {signal.get('in_window_count', 0)}",
                    f"- Accepted proposals without structured completion: {signal.get('without_completion_count', 0)}",
                    f"- Configured sources without an accepted proposal: {signal.get('without_accepted_proposal_count', 0)}",
                    f"- Evidence: `{evidence_status}`",
                    "",
                ]
            )
    lines.append(f"Next action: {report.get('next_action', '')}")
    return "\n".join(lines) + "\n"


def publication_paths(output_dir: Path, operation_month: str) -> MonthlyPublicationPaths:
    report_path = output_dir / "reports" / operation_month / DEFAULT_MONTHLY_REPORT_FILENAME
    markdown_path = output_dir / "reports" / operation_month / DEFAULT_MONTHLY_MARKDOWN_FILENAME
    history_path = output_dir / DEFAULT_MONTHLY_HISTORY_FILENAME
    if (
        len(
            {report_path.resolve(strict=False), markdown_path.resolve(strict=False), history_path.resolve(strict=False)}
        )
        != 3
    ):
        raise MonthlyArtifactError("monthly publication destinations must be different files")
    try:
        report_destination = resolve_storage_destination(report_path, "file", "monthly JSON report")
        markdown_destination = resolve_storage_destination(markdown_path, "file", "monthly Markdown report")
        history_destination = resolve_storage_destination(history_path, "file", "monthly history")
    except SafeArtifactError as exc:
        raise MonthlyArtifactError(str(exc)) from exc
    return MonthlyPublicationPaths(
        report=report_destination.path,
        markdown=markdown_destination.path,
        history=history_destination.path,
    )


def _scan_report_pairs(output_dir: Path) -> dict[str, tuple[Path, Path]]:
    reports_root = output_dir / "reports"
    if not reports_root.exists():
        return {}
    if reports_root.is_symlink() or not reports_root.is_dir():
        raise MonthlyArtifactError("monthly reports directory is not a regular directory")
    pairs: dict[str, tuple[Path, Path]] = {}
    try:
        children = sorted(reports_root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise MonthlyArtifactError(f"monthly reports directory is unreadable: {exc}") from exc
    for month_dir in children:
        if month_dir.is_symlink() or not month_dir.is_dir() or parse_operation_month(month_dir.name) is None:
            raise MonthlyArtifactError("monthly reports directory contains an invalid month publication")
        expected = {DEFAULT_MONTHLY_REPORT_FILENAME, DEFAULT_MONTHLY_MARKDOWN_FILENAME}
        try:
            actual = {path.name for path in month_dir.iterdir()}
        except OSError as exc:
            raise MonthlyArtifactError(f"monthly report directory is unreadable: {exc}") from exc
        if actual != expected:
            raise MonthlyArtifactError(f"monthly publication for {month_dir.name} is partial or has extra files")
        report_path = month_dir / DEFAULT_MONTHLY_REPORT_FILENAME
        markdown_path = month_dir / DEFAULT_MONTHLY_MARKDOWN_FILENAME
        if (
            report_path.is_symlink()
            or markdown_path.is_symlink()
            or not report_path.is_file()
            or not markdown_path.is_file()
        ):
            raise MonthlyArtifactError(f"monthly publication for {month_dir.name} is not regular files")
        pairs[month_dir.name] = (report_path, markdown_path)
    return pairs


def inspect_publication(
    output_dir: Path, history_path: Path, target_month: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Check publication shape and fully validate only the requested month.

    Older complete pairs remain indexed but are not classification or identity
    input and are not reparsed or rerendered. This keeps a later month
    independent from old derived display edits while preserving fail-closed
    retries for the target month.
    """
    entries = validate_history(history_path)
    pairs = _scan_report_pairs(output_dir)
    entry_by_month = {entry["operation_month"]: entry for entry in entries}
    if set(pairs) != set(entry_by_month):
        raise MonthlyArtifactError("every monthly report pair must have exactly one history entry")
    if target_month not in pairs:
        return None, entries

    report_path, markdown_path = pairs[target_month]
    try:
        report_bytes = report_path.read_bytes()
        report_value = json.loads(report_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MonthlyArtifactError(f"monthly JSON report is malformed: {exc}") from exc
    report = validate_report(report_value)
    if report_bytes != json_text(report).encode("utf-8") or report["operation_month"] != target_month:
        raise MonthlyArtifactError(f"monthly JSON report for {target_month} is not canonical")
    try:
        markdown_bytes = markdown_path.read_bytes()
    except OSError as exc:
        raise MonthlyArtifactError(f"monthly Markdown report is unreadable: {exc}") from exc
    if markdown_bytes != render_monthly_markdown(report).encode("utf-8"):
        raise MonthlyArtifactError(f"monthly Markdown report for {target_month} disagrees with its JSON report")

    entry = entry_by_month[target_month]
    if (
        entry["operation_identity"] != report["operation_identity"]
        or entry["evidence_digest"] != report["evidence_digest"]
    ):
        raise MonthlyArtifactError(f"monthly report and history identity differ for {target_month}")
    if entry["status"] != report["status"]:
        raise MonthlyArtifactError(f"monthly report and history status differ for {target_month}")
    for field in (
        "observed_candidate_urls",
        "new_candidate_urls",
        "banked_candidate_urls",
        "ledger_rejected_or_deferred_urls",
    ):
        if entry[field] != report[field]:
            raise MonthlyArtifactError(f"monthly report and history {field} differ for {target_month}")
    if entry["report_sha256"] != sha256(report_bytes) or entry["markdown_sha256"] != sha256(markdown_bytes):
        raise MonthlyArtifactError(f"monthly history hashes do not match the publication for {target_month}")
    return report, entries


def publish_monthly_bundle(
    paths: MonthlyPublicationPaths,
    *,
    report_text: str,
    markdown_text: str,
    history_text: str,
) -> None:
    publication_bundle = (
        (paths.report, report_text),
        (paths.markdown, markdown_text),
        (paths.history, history_text),
    )
    for path, _ in publication_bundle:
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        publish_bundle(
            [path for path, _ in publication_bundle],
            [content for _, content in publication_bundle],
        )
    except (OSError, SafeArtifactError, RuntimeError) as exc:
        raise MonthlyArtifactError(f"monthly publication failed: {exc}") from exc
