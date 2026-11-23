"""Editor feedback operator commands: record-feedback, ingest-feedback."""

from __future__ import annotations

import csv
import re
from datetime import date
from pathlib import Path
from typing import Any

from autoscout.editor_decisions import EDITOR_STATUSES
from autoscout.editor_decisions import shown_description_digest
from autoscout.editor_decisions import stamp_confirmed_row
from autoscout.feedback_dimensions import FeedbackDimensionError
from autoscout.feedback_dimensions import dimension_preview_lines
from autoscout.feedback_dimensions import has_specified_dimensions
from autoscout.feedback_dimensions import unspecified_dimensions
from autoscout.feedback_dimensions import validate_note_dimensions
from autoscout.source_proposal_commands import record_source_proposal
from autoscout.state_store import Collection
from autoscout.state_store import StateStore
from autoscout.state_store import open_state_store

FEEDBACK_ID_COLUMNS = ("Candidate ID", "identity_key", "Identity key")
FEEDBACK_STATUS_COLUMNS = ("Editor Status", "Editor status")
FEEDBACK_NOTE_COLUMNS = ("Editor Note", "Editor note")
SOURCE_IDEA_URL_COLUMNS = ("Source URL", "Source url", "URL", "url")
SOURCE_IDEA_REASON_COLUMNS = ("Reason", "reason", "Why", "why")
SOURCE_IDEA_SIGNAL_COLUMNS = (
    "Suggested signal family",
    "Signal family",
    "signal_family",
)
SOURCE_IDEA_STANCE_COLUMNS = ("Editor endorsement", "Endorsement", "Status", "Decision")
SOURCE_IDEA_NOTE_COLUMNS = ("Editor note", "Editor Note", "Note", "note")

# The collections a known Candidate is looked up in, in order.
KNOWN_CANDIDATE_COLLECTIONS = (Collection.CANDIDATE_HISTORY, Collection.LATEST_SCOUTING_CANDIDATES)
VISIBILITY_ACTIONS = {
    "hide": "hide",
    "remove": "hide",
    "restore": "restore",
    "show": "restore",
    "show_again": "restore",
    "show-again": "restore",
}


def first_present(row: dict[str, Any], columns: tuple[str, ...]) -> str:
    for column in columns:
        if column in row:
            return str(row.get(column, ""))
    return ""


def canonical_editor_status(value: str) -> str:
    normalized = re.sub(r"[\s-]+", "_", value.strip().lower())
    return normalized if normalized in EDITOR_STATUSES else ""


def returned_row_from_csv(row: dict[str, Any], identity_key: str, canonical_status: str, note: str) -> dict[str, Any]:
    returned = {"identity_key": identity_key}
    if canonical_status:
        returned["Editor Status"] = canonical_status
    elif first_present(row, FEEDBACK_STATUS_COLUMNS).strip():
        returned["Editor Status"] = ""
    returned["Editor Note"] = note
    for csv_column, feedback_column in (
        ("Title", "Title"),
        ("Author", "Author"),
        ("Russian-edition status", "Russian Edition Status"),
        ("Visible-risk status", "Visible Risk Status"),
        ("Priority label", "Priority Label"),
    ):
        if csv_column in row:
            returned[feedback_column] = row[csv_column]
    return returned


def state_known_identity_keys(store: StateStore) -> set[str]:
    keys: set[str] = set()
    for collection in KNOWN_CANDIDATE_COLLECTIONS:
        for record in store.read(collection):
            key = str(record.get("identity_key", "")).strip()
            if key:
                keys.add(key)
    return keys


def _first_record_by_key(records: Any) -> dict[str, dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for record in records if isinstance(records, list) else []:
        key = str(record.get("identity_key", "")).strip() if isinstance(record, dict) else ""
        if key:
            by_key.setdefault(key, record)
    return by_key


class KnownCandidates:
    """The Candidate History and latest scouting records a feedback command looks candidates up in, read once.

    A candidate is known when either collection holds its identity key; its
    title and authors come from the first record that holds it, Candidate
    History first.
    """

    def __init__(self, store: StateStore) -> None:
        self.history = store.read(Collection.CANDIDATE_HISTORY)
        self._history = _first_record_by_key(self.history)
        self._latest = _first_record_by_key(store.read(Collection.LATEST_SCOUTING_CANDIDATES))

    def __contains__(self, identity_key: object) -> bool:
        return identity_key in self._history or identity_key in self._latest

    def details(self, identity_key: str) -> dict[str, str] | None:
        """Return title, author display, identity key and English title for a known candidate."""
        record = self._history.get(identity_key) or self._latest.get(identity_key)
        if record is None:
            return None
        return {
            "title": str(record.get("title", "")),
            "author_display": _safe_author_display(record.get("primary_authors")),
            "identity_key": identity_key,
            "english_title": str(record.get("english_title", "")),
        }

    def description_digest(self, identity_key: str) -> str | None:
        """The digest of the description the review shows for a known candidate; ``None`` for an unknown one."""
        if identity_key not in self:
            return None
        return shown_description_digest(self._history.get(identity_key), self._latest.get(identity_key))


def source_idea_endorsement(row: dict[str, Any]) -> dict[str, Any] | None:
    raw_stance = first_present(row, SOURCE_IDEA_STANCE_COLUMNS).strip()
    note = first_present(row, SOURCE_IDEA_NOTE_COLUMNS).strip()
    if not raw_stance and not note:
        return None
    normalized = re.sub(r"[\s-]+", "_", raw_stance.lower())
    if normalized in {"endorse", "endorsed", "yes", "support", "supported"}:
        value = "endorsed"
    elif normalized in {"dismiss", "dismissed", "no", "oppose", "opposed"}:
        value = "dismissed"
    else:
        value = normalized or "note"
    return {"value": value, "note": note}


def ingest_source_ideas(csv_path: Path | None, state_dir: Path, state_store: StateStore | None = None) -> int:
    if csv_path is None:
        return 0
    staged = 0
    errors = 0
    skipped_empty = 0
    with csv_path.open(encoding="utf-8", newline="") as handle:
        for index, row in enumerate(csv.DictReader(handle), start=2):
            source_url = first_present(row, SOURCE_IDEA_URL_COLUMNS).strip()
            reason = first_present(row, SOURCE_IDEA_REASON_COLUMNS).strip()
            suggested_signal_family = first_present(row, SOURCE_IDEA_SIGNAL_COLUMNS).strip()
            endorsement = source_idea_endorsement(row)
            if not source_url and not reason and not endorsement:
                skipped_empty += 1
                continue
            if not source_url:
                print(f"source_idea_missing_url row={index}")
                errors += 1
                continue
            record_source_proposal(
                state_dir,
                source_url=source_url,
                proposed_by="editor",
                reason=reason,
                suggested_signal_family=suggested_signal_family,
                editor_endorsement=endorsement,
                state_store=state_store,
            )
            staged += 1
    print(f"source_idea_rows={staged}")
    print(f"source_idea_skipped_empty_rows={skipped_empty}")
    print(f"source_idea_error_rows={errors}")
    return errors


def ingest_feedback(
    csv_path: Path,
    state_dir: Path,
    source_ideas_csv: Path | None = None,
    state_store: StateStore | None = None,
) -> int:
    state_dir.mkdir(parents=True, exist_ok=True)
    store = open_state_store(state_dir) if state_store is None else state_store
    known = KnownCandidates(store)
    staged: list[dict[str, Any]] = []
    invalid_statuses: list[dict[str, str]] = []
    unmatched: list[str] = []
    skipped_empty = 0

    with csv_path.open(encoding="utf-8", newline="") as handle:
        for index, row in enumerate(csv.DictReader(handle), start=2):
            identity_key = first_present(row, FEEDBACK_ID_COLUMNS).strip()
            raw_status = first_present(row, FEEDBACK_STATUS_COLUMNS)
            note = first_present(row, FEEDBACK_NOTE_COLUMNS)
            canonical_status = canonical_editor_status(raw_status)
            has_status = bool(raw_status.strip())
            has_note = bool(note)
            if not has_status and not has_note:
                skipped_empty += 1
                continue

            staged_row = stamp_confirmed_row(
                returned_row_from_csv(row, identity_key, canonical_status, note), known.description_digest(identity_key)
            )
            if has_status and canonical_status == "":
                invalid_statuses.append({"row": str(index), "status": raw_status})
            if identity_key not in known:
                unmatched.append(identity_key or f"row {index}")
            staged.append(staged_row)

    # The returned rows and the source ideas are staged in one store
    # transaction: an import that fails part way stages nothing.
    with store.transaction():
        existing = store.read(Collection.PENDING_EDITOR_ROWS)
        existing.extend(staged)
        store.replace(Collection.PENDING_EDITOR_ROWS, existing)

        print(f"staged_rows={len(staged)}")
        print(f"skipped_empty_rows={skipped_empty}")
        print(f"invalid_status_rows={len(invalid_statuses)}")
        print(f"unmatched_rows={len(unmatched)}")
        for item in invalid_statuses:
            print(f"invalid_status row={item['row']} status={item['status']}")
        for identity_key in unmatched:
            print(f"unmatched identity_key={identity_key}")
        source_idea_errors = ingest_source_ideas(source_ideas_csv, state_dir, store)
    return 1 if invalid_statuses or unmatched or source_idea_errors else 0


def canonical_visibility_action(value: str) -> str:
    return VISIBILITY_ACTIONS.get(value.strip().lower(), "")


def record_visibility(
    identity_key: str,
    raw_action: str,
    state_dir: Path,
    reason: str = "",
    audit_provenance: str = "agent-confirmed editor action",
    state_store: StateStore | None = None,
) -> int:
    """Record a confirmed, reversible visibility decision for one candidate."""
    action = canonical_visibility_action(raw_action)
    if not action:
        print(f"Unsupported visibility action: {raw_action}")
        print("Supported actions: hide, remove, restore, show, show-again")
        return 1

    state_dir.mkdir(parents=True, exist_ok=True)
    store = open_state_store(state_dir) if state_store is None else state_store
    # The candidate is looked up, its visibility read, decided on and written
    # in one store transaction.
    with store.transaction():
        known_keys = state_known_identity_keys(store)
        if identity_key not in known_keys:
            print(f"Unknown candidate identity: {identity_key}")
            return 1

        desired_visibility = "hidden" if action == "hide" else "visible"
        records = store.read(Collection.CANDIDATE_VISIBILITY)
        if not isinstance(records, list):
            records = []
        by_identity = {
            str(record.get("identity_key", "")): dict(record)
            for record in records
            if isinstance(record, dict) and str(record.get("identity_key", "")).strip()
        }
        record = by_identity.get(identity_key)
        if record and record.get("visibility") == desired_visibility:
            print(f"visibility_unchanged identity_key={identity_key} visibility={desired_visibility}")
            return 0

        decision = {
            "action": action,
            "decision_date": date.today().isoformat(),
            "reason": reason,
            "audit_provenance": audit_provenance or "agent-confirmed editor action",
        }
        if record is None:
            record = {"identity_key": identity_key, "audit_log": []}
        audit_log = record.get("audit_log", [])
        if not isinstance(audit_log, list):
            audit_log = []
        audit_log.append(decision)
        record.update(
            {
                "identity_key": identity_key,
                "visibility": desired_visibility,
                **decision,
                "audit_log": audit_log,
            }
        )
        by_identity[identity_key] = record
        store.replace(
            Collection.CANDIDATE_VISIBILITY,
            [by_identity[key] for key in sorted(by_identity)],
        )
    print(f"recorded_visibility identity_key={identity_key} action={action}")
    return 0


def _safe_author_display(authors: Any) -> str:
    """Return a comma-separated author display, handling both list and legacy string forms."""
    if isinstance(authors, list):
        return ", ".join(str(a) for a in authors)
    if isinstance(authors, str) and authors.strip():
        return authors.strip()
    return ""


def record_feedback(
    identity_key: str,
    raw_status: str,
    note: str,
    state_dir: Path,
    confirmed_by_editor: bool = False,
    dimensions: dict[str, str] | None = None,
    state_store: StateStore | None = None,
) -> int:
    """Preview (code 3, zero mutation) or stage one editor feedback record.

    ``dimensions`` carries the optional structured Issue #678 dimensions
    (editorial appeal, rights availability, quality problem, information
    needed) as a canonical dict. They are validated before preview, refused
    outright beside a bare refusal, and stored atomically with the verbatim
    note on confirmation.
    """
    canonical = canonical_editor_status(raw_status)
    if not canonical:
        print(f"Unsupported editor status: {raw_status}")
        print(f"Supported statuses: {', '.join(sorted(EDITOR_STATUSES))}")
        return 1
    store = open_state_store(state_dir) if state_store is None else state_store
    if dimensions is None:
        dimensions = unspecified_dimensions()

    # A refused call creates nothing: the lookup reads outside any transaction,
    # which under SQLite would create the database at its first read.
    known = KnownCandidates(store)
    details = known.details(identity_key)
    if details is None:
        print(f"Unknown candidate identity: {identity_key}")
        return 1
    try:
        validate_note_dimensions(note, dimensions)
    except FeedbackDimensionError as exc:
        print(str(exc))
        return 1

    if not confirmed_by_editor:
        print("confirmation_required=true")
        print_feedback_preview(details, canonical, note, dimensions, _current_review_status(identity_key, store))
        print("action=stage_pending_feedback")
        print("hint=re-run with --confirmed-by-editor to apply")
        return 3

    state_dir.mkdir(parents=True, exist_ok=True)
    # The description digest is read and the row staged in one store transaction.
    with store.transaction():
        known = KnownCandidates(store)
        if identity_key not in known:
            print(f"Unknown candidate identity: {identity_key}")
            return 1
        existing = store.read(Collection.PENDING_EDITOR_ROWS)
        existing.append(staged_feedback_row(identity_key, canonical, note, dimensions, known))
        store.replace(Collection.PENDING_EDITOR_ROWS, existing)
    print(f"recorded_feedback identity_key={identity_key} status={canonical}")
    return 0


def print_feedback_preview(
    details: dict[str, str], canonical: str, note: str, dimensions: dict[str, str], review_status: str
) -> None:
    """Print the preview lines of one candidate's feedback, the same for a single book and a batch row."""
    print(f"candidate_title={details['title']}")
    print(f"candidate_author={details['author_display']}")
    if details["english_title"]:
        print(f"candidate_english_title={details['english_title']}")
    print(f"candidate_identity_key={details['identity_key']}")
    print(f"intended_status={canonical}")
    print(f"editor_note={note}")
    for line in dimension_preview_lines(dimensions, canonical):
        print(line)
    if review_status:
        print(f"current_review_status={review_status}")


def staged_feedback_row(
    identity_key: str, canonical: str, note: str, dimensions: dict[str, str], known: KnownCandidates
) -> dict[str, Any]:
    """The pending returned row a confirmed decision stages, stamped with today's date and the shown description's digest."""
    staged_row = returned_row_from_csv({FEEDBACK_ID_COLUMNS[0]: identity_key}, identity_key, canonical, note)
    if has_specified_dimensions(dimensions):
        staged_row["Feedback Dimensions"] = dict(dimensions)
    return stamp_confirmed_row(staged_row, known.description_digest(identity_key))


def _current_review_status(identity_key: str, store: StateStore) -> str:
    """Return current review status from visibility records, if any."""
    return review_statuses(store).get(identity_key, "")


def review_statuses(store: StateStore) -> dict[str, str]:
    """Return each candidate's current review status from the visibility records: its first nonblank visibility."""
    records = store.read(Collection.CANDIDATE_VISIBILITY)
    statuses: dict[str, str] = {}
    for record in records if isinstance(records, list) else []:
        if isinstance(record, dict):
            visibility = str(record.get("visibility", ""))
            if visibility:
                statuses.setdefault(str(record.get("identity_key", "")), visibility)
    return statuses
