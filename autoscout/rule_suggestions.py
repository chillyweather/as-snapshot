from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from autoscout.json_io import write_json
from autoscout.review_rendering import markdown_table_cell

REJECTION_STATUSES = {"reject", "already_known"}
MIN_PATTERN_OBSERVATIONS = 2
MIN_REJECTION_RATE = 0.6


def derive_rejection_pattern_suggestions(
    *,
    candidates: list[dict[str, Any]],
    candidate_history: list[dict[str, Any]],
    run_date: str,
) -> dict[str, Any]:
    history_by_identity = {str(record.get("identity_key", "")): record for record in candidate_history}
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        identity = str(candidate.get("identity_key", ""))
        history = history_by_identity.get(identity, {})
        status = str(candidate.get("editor_status") or history.get("editor_status", ""))
        if not status:
            continue
        item = {
            "identity_key": identity,
            "title": str(candidate.get("title", "")),
            "editor_status": status,
        }
        topic = str(candidate.get("candidate_category", "")).strip()
        if topic:
            groups[("topic", topic)].append(item)
        for family in sorted(source_families(candidate)):
            groups[("source_family", family)].append(item)
        for source in sorted(source_names(candidate)):
            groups[("source", source)].append(item)

    suggestions = []
    for (scope, subject), observations in sorted(groups.items()):
        rejected = [item for item in observations if item["editor_status"] in REJECTION_STATUSES]
        observed_count = len(observations)
        rejected_count = len(rejected)
        rejection_rate = round(rejected_count / observed_count, 3) if observed_count else 0.0
        if observed_count < MIN_PATTERN_OBSERVATIONS or rejection_rate < MIN_REJECTION_RATE:
            continue
        suggestions.append(
            {
                "id": f"suggestion-{scope}-{slug(subject)}",
                "kind": "hard_rule_suggestion",
                "scope": scope,
                "subject": subject,
                "observed_count": observed_count,
                "rejected_count": rejected_count,
                "rejection_rate": rejection_rate,
                "status": "suggestion_only",
                "inert_until_promoted": True,
                "plain_language_evidence": (
                    f"{rejected_count} of {observed_count} reviewed candidates for {scope} “{subject}” "
                    "were rejected or already known."
                ),
                "promotion_command_hint": promotion_command_hint(scope, subject),
                "example_candidates": rejected[:5],
            }
        )
    return {
        "run_date": run_date,
        "suggestions": suggestions,
        "notes": [
            "Suggestions are derived from history and are inert: they do not create, revoke, change, hide, or rank anything.",
            "Promote a suggestion only by recording an explicit Editorial Taste Profile entry with the editorial-profile command.",
        ],
    }


def source_families(candidate: dict[str, Any]) -> set[str]:
    return {
        str(evidence.get("source_family", "")).strip()
        for evidence in candidate.get("source_evidence", [])
        if str(evidence.get("source_family", "")).strip()
    }


def source_names(candidate: dict[str, Any]) -> set[str]:
    return {
        str(evidence.get("source_name", "")).strip()
        for evidence in candidate.get("source_evidence", [])
        if str(evidence.get("source_name", "")).strip()
    }


def slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or "pattern"


def promotion_command_hint(scope: str, subject: str) -> str:
    profile_scope = scope if scope in {"topic", "source", "source_family"} else "topic"
    return (
        "autoscout editorial-profile add --kind hard-rule "
        f'--scope {profile_scope} --subject "{subject}" --quote "<editor wording>" --provenance "source review"'
    )


def render_rejection_suggestions(artifact: dict[str, Any]) -> str:
    lines = [
        "# Rejection Pattern Suggestions",
        "",
        "These suggestions are inert until the editor explicitly promotes one through the Editorial Taste Profile command surface.",
        "",
    ]
    suggestions = artifact.get("suggestions", [])
    if not suggestions:
        lines.extend(["No deterministic rejection-pattern suggestions met the evidence threshold.", ""])
        return "\n".join(lines)
    lines.extend(
        [
            "| Suggestion | Evidence | Promotion command hint |",
            "|---|---|---|",
        ]
    )
    for suggestion in suggestions:
        lines.append(
            "| "
            + " | ".join(
                markdown_table_cell(value)
                for value in (
                    f"{suggestion['scope']}: {suggestion['subject']}",
                    suggestion["plain_language_evidence"],
                    suggestion["promotion_command_hint"],
                )
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def write_rejection_suggestion_artifacts(output_dir: Path, artifact: dict[str, Any]) -> None:
    write_json(output_dir / "derived-rule-suggestions.json", artifact)
    (output_dir / "derived-rule-suggestions.md").write_text(render_rejection_suggestions(artifact), encoding="utf-8")
