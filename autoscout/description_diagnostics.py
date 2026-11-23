"""Derived description-stage latency and post-enrichment caveats."""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from statistics import median
from typing import Any

from autoscout.content_description import description_coverage

# One slow call is ordinary noise; losing more than one in five attempts
# warrants an explanation even if a later retry recovers the description.
SLOW_CODEX_TIMEOUT_SHARE = 0.20
# Name sources that lose a majority of their new books, rather than
# treating an occasional missing description as a source-wide problem.
SOURCE_PLACEHOLDER_SHARE = 0.50


def call_latency_summary(seconds: Sequence[float], timeout_count: int) -> dict[str, Any]:
    return {
        "median_seconds": round(median(seconds), 3) if seconds else None,
        "max_seconds": round(max(seconds), 3) if seconds else None,
        "call_count": len(seconds),
        "timeout_count": timeout_count,
    }


def refresh_description_diagnostics(
    run_summary: dict[str, Any],
    candidates: list[dict[str, Any]],
    grounded: Mapping[str, Any],
    adjudication: Mapping[str, Any],
) -> None:
    """Replace derived facts after both stages, without retaining stale caveats."""
    run_date = str(run_summary.get("run_date", ""))
    if run_date:
        grounded = grounded if grounded.get("operation_date") == run_date else {}
        adjudication = adjudication if adjudication.get("operation_date") == run_date else {}
    operation_id = str(run_summary.get("operation_id", ""))
    if operation_id:
        grounded = grounded if grounded.get("operation_id") == operation_id else {}
        adjudication = adjudication if adjudication.get("operation_id") == operation_id else {}
    latency = {
        name: dict(stage["call_latency"])
        for name, stage in (("synthesis", grounded), ("adjudication", adjudication))
        if isinstance(stage.get("call_latency"), Mapping)
    }
    if latency:
        run_summary["description_call_latency"] = latency
    else:
        run_summary.pop("description_call_latency", None)
    caveats = []
    grounded_counts = grounded.get("counts", {})
    grounded_attempts = sum(
        int(grounded_counts.get(outcome, 0) or 0)
        for outcome in ("grounded", "single_source", "attempted_no_description")
    )
    attempts = grounded_attempts + int(latency.get("adjudication", {}).get("call_count", 0))
    timeouts = sum(int(item.get("timeout_count", 0)) for item in latency.values())
    if attempts and timeouts / attempts > SLOW_CODEX_TIMEOUT_SHARE:
        caveats.append(
            f"Внимание: проверка описаний шла слишком медленно и не завершилась вовремя "
            f"в {timeouts} из {attempts} попыток."
        )
    new_coverage = description_coverage([item for item in candidates if item.get("update_delta") == "new"])
    for source in new_coverage["fallback_by_source"]:
        missing, total = source["fallback_count"], source["candidate_count"]
        if total and missing / total > SOURCE_PLACEHOLDER_SHARE:
            caveats.append(f"Внимание: {source['source_name']} — без описания {missing} из {total} новых книг.")
    if caveats:
        run_summary["description_caveats"] = caveats
    else:
        run_summary.pop("description_caveats", None)
