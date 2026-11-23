from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from autoscout.scouting_candidates import ScoutingCandidate
from autoscout.scouting_candidates import truncate_annotation  # noqa: F401

EDITOR_FIELDNAMES = [
    "Title",
    "Author",
    "Annotation",
    "Description",
    "Description status",
    "Signal count",
    "Visible-risk status",
    "Risk comment",
    "English title",
    "English author display",
    "English annotation",
    "Check date",
    "Source",
    "Source URL",
    "Publisher / year",
    "Category / topic",
    "Why it entered the list",
    "Signal summary",
    "Priority label",
    "Russian-edition status",
    "Russian-edition URL",
    "Links",
    "Update delta",
    "Lifecycle",
    "Review context",
    "Taste profile notes",
    "Editor status",
    "Editor note",
    "Candidate ID",
]


OPTIONAL_EDITOR_FIELDNAMES = (
    # The durable ``candidate-…`` ID beside the identity key in "Candidate ID"
    # (Issue #851). It is optional so CSVs written before it stay readable.
    "Durable Candidate ID",
    "Description reason",
    "Description provenance",
    "Description depth",
    "Prominence",
    "Prominence claim type",
    "Prominence evidence URL",
    # Issue #945: the publisher, agent, agency and territory of a rights deal.
    "Rights deal",
    "Original edition",
    # Issue #1181: the learned Russian-edition finding, a mark that never excludes the book.
    "Russian-edition finding",
    "Earlier edition decision",
    # The archive backfill block a row waits in, until the editor decides on it.
    "Archive batch",
)


def editor_csv_fieldnames(rows: list[dict[str, str]]) -> list[str]:
    """Keep the legacy CSV header stable and add bounded optional lines only when used."""
    fields = list(EDITOR_FIELDNAMES)
    fields.extend(field for field in OPTIONAL_EDITOR_FIELDNAMES if any(field in row for row in rows))
    return fields


EDITOR_LIFECYCLE_LABELS = {
    "new": "New",
    "upgraded": "Updated",
    "changed": "Updated",
    "unchanged": "Previously seen",
    "previously_seen": "Previously seen",
}


def editor_lifecycle_label(update_delta: Any) -> str:
    return EDITOR_LIFECYCLE_LABELS.get(str(update_delta or "unchanged").strip(), "Previously seen")


def editor_row_from_candidate(candidate: dict[str, Any] | ScoutingCandidate) -> dict[str, str]:
    row = ScoutingCandidate.from_mapping(candidate).editor_row()
    row["Lifecycle"] = editor_lifecycle_label(row.get("Update delta", ""))
    row["Review context"] = ""
    return row


def publisher_year(candidate: dict[str, Any] | ScoutingCandidate) -> str:
    return ScoutingCandidate.from_mapping(candidate).publisher_year()


def editor_annotation(candidate: dict[str, Any] | ScoutingCandidate, evidence: dict[str, Any]) -> str:
    return ScoutingCandidate.from_mapping(candidate).editor_annotation(evidence)


def editor_description(candidate: dict[str, Any] | ScoutingCandidate) -> str:
    return ScoutingCandidate.from_mapping(candidate).editor_description()


def evidence_links(candidate: dict[str, Any] | ScoutingCandidate) -> str:
    return ScoutingCandidate.from_mapping(candidate).evidence_links()


def write_editor_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=editor_csv_fieldnames(rows), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
