from __future__ import annotations

from datetime import date
from datetime import timedelta
from pathlib import Path
from typing import Any

from autoscout.json_io import read_json_or_default
from autoscout.json_io import write_json
from autoscout.json_io import write_json_preserving_key_order
from autoscout.review_rendering import markdown_table_cell
from autoscout.source_metadata import declared_source_metadata
from autoscout.source_proposals import normalize_source_identity

POSITIVE_EDITOR_STATUSES = {"interested", "maybe_watch", "needs_deeper_analysis"}
REVIEWED_EDITOR_STATUSES = POSITIVE_EDITOR_STATUSES | {"reject", "already_known"}
DEFAULT_SOURCE_METRICS_WINDOW_WEEKS = 8


def source_lifecycle_status(source: dict[str, Any]) -> str:
    if source.get("active", True) is False and not source.get("lifecycle_status"):
        return "inactive"
    return str(source.get("lifecycle_status") or source.get("source_status") or "active").strip() or "active"


def source_probation_note(source: dict[str, Any]) -> str:
    return str(source.get("lifecycle_note") or source.get("probation_note") or source.get("notes", "")).strip()


def source_key(source_name: str, source_url: str = "") -> str:
    normalized_url = normalize_source_identity(source_url)
    if normalized_url:
        return normalized_url
    return source_name.strip().lower()


def source_matches_evidence(source: dict[str, Any], evidence: dict[str, Any]) -> bool:
    source_name = str(source.get("name", ""))
    source_url = str(source.get("url", ""))
    evidence_name = str(evidence.get("source_name", ""))
    evidence_url = str(evidence.get("source_url", ""))
    name_matches = bool(source_name and evidence_name and source_name == evidence_name)
    url_matches = bool(
        source_url and evidence_url and normalize_source_identity(source_url) == normalize_source_identity(evidence_url)
    )
    return name_matches or url_matches


def parse_iso_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def trailing_window_start(run_date: str, window_weeks: int) -> str:
    parsed = parse_iso_date(run_date)
    if parsed is None:
        return ""
    return (parsed - timedelta(weeks=window_weeks)).isoformat()


def record_in_window(record: dict[str, Any], window_start: str) -> bool:
    if not window_start:
        return True
    last_seen = str(record.get("last_seen_date", ""))
    if not last_seen:
        return True
    return last_seen >= window_start


def rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 3)


def rate_label(value: float | None) -> str:
    if value is None:
        return "unknown"
    return f"{value:.0%}"


def compute_source_metrics(
    *,
    sources: list[dict[str, Any]],
    source_runs: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    candidate_history: list[dict[str, Any]],
    run_date: str,
    window_weeks: int = DEFAULT_SOURCE_METRICS_WINDOW_WEEKS,
) -> dict[str, Any]:
    window_start = trailing_window_start(run_date, window_weeks)
    history_by_identity = {
        str(record.get("identity_key", "")): record
        for record in candidate_history
        if record_in_window(record, window_start)
    }
    metric_records_by_identity = {
        identity: record for identity, record in history_by_identity.items() if record.get("source_evidence")
    }
    for candidate in candidates:
        identity = str(candidate.get("identity_key", ""))
        if identity and identity not in metric_records_by_identity:
            metric_records_by_identity[identity] = candidate
    metric_records = list(metric_records_by_identity.values())
    run_by_key = {source_key(str(run.get("source_name", "")), str(run.get("url", ""))): run for run in source_runs}
    metrics = []
    for source in sources:
        name = str(source.get("name", ""))
        url = str(source.get("url", ""))
        key = source_key(name, url)
        run = run_by_key.get(key, {})
        associated_candidates = [candidate for candidate in metric_records if candidate_has_source(candidate, source)]
        candidate_count = len(associated_candidates)
        unique_count = sum(1 for candidate in associated_candidates if candidate_is_unique_to_source(candidate, source))
        corroborated_count = sum(
            1 for candidate in associated_candidates if candidate_is_corroborated(candidate, source)
        )
        # Per-status counts -- count each distinct identity once per source.
        seen_identities: set[str] = set()
        status_counts: dict[str, int] = {
            "interested": 0,
            "needs_deeper_analysis": 0,
            "maybe_watch": 0,
            "already_known": 0,
            "reject": 0,
        }
        reviewed_count = 0
        yield_count = 0
        for candidate in associated_candidates:
            identity = str(candidate.get("identity_key", "")).strip()
            if identity and identity in seen_identities:
                continue
            if identity:
                seen_identities.add(identity)
            history = history_by_identity.get(str(candidate.get("identity_key", "")), {})
            status = str(candidate.get("editor_status") or history.get("editor_status", "")).strip().lower()
            if status in REVIEWED_EDITOR_STATUSES:
                reviewed_count += 1
            if status in POSITIVE_EDITOR_STATUSES:
                yield_count += 1
            if status in status_counts:
                status_counts[status] += 1

        # Cross-family corroboration: candidates corroborated by at least one
        # source in a DIFFERENT family.
        own_family = str(source.get("family", "")).strip()
        own_name = str(source.get("name", "")).strip()
        cross_family_corroborated_count = 0
        if own_family:
            for candidate in associated_candidates:
                evidence = candidate.get("source_evidence", [])
                has_other_family = any(
                    str(ev.get("source_family", "")).strip()
                    and str(ev.get("source_family", "")).strip() != own_family
                    and str(ev.get("source_name", "")).strip() != own_name
                    for ev in evidence
                )
                if has_other_family:
                    cross_family_corroborated_count += 1

        data_status = "ok" if candidate_count else "insufficient-data"
        if not any(record.get("source_evidence") for record in history_by_identity.values()) and candidate_count:
            data_status = "current-run-only"
        metrics.append(
            {
                "source_name": name,
                "source_url": url,
                "family": str(source.get("family") or run.get("family", "")),
                **declared_source_metadata(source),
                "run_status": str(run.get("status", "unknown")),
                "lifecycle_status": source_lifecycle_status(source),
                "lifecycle_note": source_probation_note(source),
                "probation_started_at": str(source.get("probation_started_at", "")),
                "probation_evidence_window_weeks": source.get("probation_evidence_window_weeks"),
                "active": source.get("active", True) is not False,
                "window_weeks": window_weeks,
                "window_start": window_start,
                "data_status": data_status,
                "candidate_count": candidate_count,
                "unique_contribution_count": unique_count,
                "unique_contribution_rate": rate(unique_count, candidate_count),
                "corroborated_count": corroborated_count,
                "corroboration_rate": rate(corroborated_count, candidate_count),
                "cross_family_corroborated_count": cross_family_corroborated_count,
                "cross_family_corroboration_rate": rate(cross_family_corroborated_count, candidate_count),
                "reviewed_candidate_count": reviewed_count,
                "editor_yield_count": yield_count,
                "editor_yield_rate": rate(yield_count, reviewed_count),
                "interested_count": status_counts["interested"],
                "needs_deeper_analysis_count": status_counts["needs_deeper_analysis"],
                "maybe_watch_count": status_counts["maybe_watch"],
                "already_known_count": status_counts["already_known"],
                "reject_count": status_counts["reject"],
                "raw_mention_count": int(run.get("raw_mention_count", run.get("raw_candidate_count", 0)) or 0),
                "editor_facing_count": int(run.get("editor_facing_count", 0) or 0),
            }
        )
    return {
        "run_date": run_date,
        "window_weeks": window_weeks,
        "window_start": window_start,
        "metrics": sorted(metrics, key=lambda metric: (str(metric["source_name"]), str(metric["source_url"]))),
        "notes": [
            "Metrics are descriptive only; no source is added, retired, or promoted automatically.",
            "unknown/insufficient-data values mean the configured source lacks enough candidate history in the trailing window.",
        ],
    }


def candidate_has_source(candidate: dict[str, Any], source: dict[str, Any]) -> bool:
    return any(source_matches_evidence(source, evidence) for evidence in candidate.get("source_evidence", []))


def candidate_is_unique_to_source(candidate: dict[str, Any], source: dict[str, Any]) -> bool:
    source_names = {
        str(evidence.get("source_name", ""))
        for evidence in candidate.get("source_evidence", [])
        if str(evidence.get("source_name", "")).strip()
    }
    if source_names:
        return source_names == {str(source.get("name", ""))}
    return candidate_has_source(candidate, source)


def candidate_is_corroborated(candidate: dict[str, Any], source: dict[str, Any]) -> bool:
    if not candidate_has_source(candidate, source):
        return False
    source_names = {
        str(evidence.get("source_name", ""))
        for evidence in candidate.get("source_evidence", [])
        if str(evidence.get("source_name", "")).strip()
    }
    return len(source_names) > 1


def render_source_review(metrics_artifact: dict[str, Any]) -> str:
    lines = [
        "# Source Review",
        "",
        f"Trailing window: {metrics_artifact.get('window_weeks', DEFAULT_SOURCE_METRICS_WINDOW_WEEKS)} weeks starting {metrics_artifact.get('window_start') or 'unknown'}.",
        "No source is added, retired, or promoted automatically from these metrics; discuss consequences before changing configuration.",
        "",
        "| Source | Lifecycle | Data | Unique contribution | Corroboration | Editor yield | Context |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    for metric in metrics_artifact.get("metrics", []):
        lifecycle = str(metric.get("lifecycle_status", "active"))
        if lifecycle == "probation":
            lifecycle = f"probation — {metric.get('lifecycle_note') or 'review before promote/retire'}"
        context = consequence_context(metric)
        lines.append(
            "| "
            + " | ".join(
                markdown_table_cell(value)
                for value in (
                    metric.get("source_name", ""),
                    lifecycle,
                    metric.get("data_status", ""),
                    f"{metric.get('unique_contribution_count', 0)}/{metric.get('candidate_count', 0)} ({rate_label(metric.get('unique_contribution_rate'))})",
                    f"{metric.get('corroborated_count', 0)}/{metric.get('candidate_count', 0)} ({rate_label(metric.get('corroboration_rate'))})",
                    f"{metric.get('editor_yield_count', 0)}/{metric.get('reviewed_candidate_count', 0)} ({rate_label(metric.get('editor_yield_rate'))})",
                    context,
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Review prompts",
            "",
            "- Keep: leave the source configured and active.",
            "- Promote: mark a probationary source as established; this does not broaden discovery.",
            "- Retire: deactivate the configured source with a recorded reason; this is reversible by editing configuration or using the promote command.",
        ]
    )
    return "\n".join(lines) + "\n"


def consequence_context(metric: dict[str, Any]) -> str:
    if metric.get("data_status") == "insufficient-data":
        return "insufficient data; do not infer source quality yet"
    if metric.get("lifecycle_status") == "probation":
        return "probationary source; review before keep/promote/retire"
    return "metric-backed review only; no automatic lifecycle change"


def write_source_review_artifacts(output_dir: Path, metrics_artifact: dict[str, Any]) -> None:
    write_json(output_dir / "source-metrics.json", metrics_artifact)
    (output_dir / "source-review.md").write_text(render_source_review(metrics_artifact), encoding="utf-8")


def retire_source(config_path: Path, source_url: str, source_name: str, reason: str, actor: str) -> int:
    if not reason.strip():
        print("source_retired=false")
        print("error=retirement_reason_required")
        return 2
    return update_source_lifecycle(
        config_path,
        source_url=source_url,
        source_name=source_name,
        lifecycle_status="retired",
        active=False,
        reason=reason,
        actor=actor,
        action="retired",
    )


def promote_source(config_path: Path, source_url: str, source_name: str, reason: str, actor: str) -> int:
    return update_source_lifecycle(
        config_path,
        source_url=source_url,
        source_name=source_name,
        lifecycle_status="established",
        active=True,
        reason=reason,
        actor=actor,
        action="promoted",
    )


def keep_source(config_path: Path, source_url: str, source_name: str, reason: str, actor: str) -> int:
    return update_source_lifecycle(
        config_path,
        source_url=source_url,
        source_name=source_name,
        lifecycle_status="",
        active=True,
        reason=reason,
        actor=actor,
        action="kept",
        preserve_probation=True,
    )


def update_source_lifecycle(
    config_path: Path,
    *,
    source_url: str,
    source_name: str,
    lifecycle_status: str,
    active: bool,
    reason: str,
    actor: str,
    action: str,
    preserve_probation: bool = False,
) -> int:
    config = read_json_or_default(config_path, {})
    sources = list(config.get("sources", []))
    source = find_config_source(sources, source_url, source_name)
    if source is None:
        print(f"source_{action}=false")
        print("error=source_not_found")
        return 1
    source["active"] = active
    if not preserve_probation and lifecycle_status:
        source["lifecycle_status"] = lifecycle_status
    source["lifecycle_note"] = reason.strip()
    reason_key = "retirement_reason" if action == "retired" else f"{action}_reason"
    source[reason_key] = reason.strip()
    source[f"{action}_at"] = date.today().isoformat()
    source[f"{action}_by"] = actor.strip() or "agent"
    config["sources"] = sources
    write_json_preserving_key_order(config_path, config)
    print(f"source_{action}=true")
    print(f"configured_source_active={str(active).lower()}")
    print("automatic_lifecycle_change=false")
    print(
        "consequence=configuration updated only after explicit operator command; future runs use configured source active status"
    )
    return 0


def find_config_source(
    sources: list[dict[str, Any]], source_url: str = "", source_name: str = ""
) -> dict[str, Any] | None:
    normalized_url = normalize_source_identity(source_url) if source_url else ""
    for source in sources:
        if normalized_url and normalize_source_identity(str(source.get("url", ""))) == normalized_url:
            return source
        if source_name and str(source.get("name", "")).strip() == source_name.strip():
            return source
    return None
