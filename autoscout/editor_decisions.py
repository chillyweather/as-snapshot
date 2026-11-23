"""The dated log of the editor's confirmed decisions on one book (issue #1053).

Candidate History shows only the latest decision in ``editor_status`` and
``editor_note``. Each history record also keeps every confirmed decision in
``editor_decisions``, oldest first, so a changed decision never erases the
earlier one. One entry is flat on purpose: a later State Store (#1011) keeps
the log as its own table, and the Git-history backfill (#929) writes older
decisions in the same shape.

An entry holds exactly these fields:

- ``status``: the canonical editor status the record shows after the decision;
- ``note``: the editor note it shows, verbatim;
- ``decision_date``: the ISO date the editor confirmed it, or ``None`` when
  unknown;
- ``description_digest``: a short digest of the Russian description she saw
  (``""`` when the book had none, ``None`` when unknown).

The date and the digest are stamped on the returned row when the decision is
confirmed (``record-feedback --confirmed-by-editor`` and ``ingest-feedback``),
so a row consumed later still carries the moment she decided. A decision taken
before the log existed is never given a date: when a new decision replaces it,
it is kept as an undated entry and nothing else.
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

from autoscout.content_description import has_nonfallback_description
from autoscout.content_description import validate_record_description

# The canonical editor statuses; ``editor_feedback`` re-exports them.
EDITOR_STATUSES = {
    "interested",
    "maybe_watch",
    "reject",
    "already_known",
    "needs_deeper_analysis",
}

DECISION_LOG_FIELD = "editor_decisions"
# Returned-row columns stamped when the editor confirms a decision.
DECISION_DATE_COLUMN = "Decision Date"
DESCRIPTION_DIGEST_COLUMN = "Description Digest"
DECISION_ROW_COLUMNS = (DECISION_DATE_COLUMN, DESCRIPTION_DIGEST_COLUMN)

# Sixteen hex characters tell two descriptions apart; the text itself stays on the record.
_DIGEST_LENGTH = 16


def description_digest(record: dict[str, Any]) -> str:
    """Return the short digest of the description the review shows for one record, or "" when it shows none."""
    return shown_description_digest(record, None)


def shown_description_digest(history_record: dict[str, Any] | None, latest_record: dict[str, Any] | None) -> str:
    """Return the short digest of the description the review shows for a book, or "" when it shows none.

    The review takes the latest candidate's fields over Candidate History's,
    except a nonblank Candidate History description (with its provenance),
    which is canonical. It shows nothing for the fallback placeholder or a
    description that fails validation, so neither gets a digest.
    """
    history = history_record or {}
    shown = {**history, **(latest_record or {})}
    if str(history.get("content_description", "") or "").strip():
        shown["content_description"] = history["content_description"]
        if "description_provenance" in history:
            shown["description_provenance"] = history["description_provenance"]
    if not has_nonfallback_description(shown.get("content_description")):
        return ""
    validated, _reason = validate_record_description(shown)
    if validated is None:
        return ""
    return hashlib.sha256(validated.strip().encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]


def stamp_confirmed_row(row: dict[str, Any], digest: str | None, today: date | None = None) -> dict[str, Any]:
    """Add the decision date and the seen description's digest to a confirmed returned row.

    ``digest`` is ``None`` when the row names no known book.
    """
    row[DECISION_DATE_COLUMN] = (today or date.today()).isoformat()
    row[DESCRIPTION_DIGEST_COLUMN] = digest
    return row


def _row_decision_date(row: dict[str, Any]) -> str | None:
    value = row.get(DECISION_DATE_COLUMN)
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None


def _row_description_digest(row: dict[str, Any]) -> str | None:
    value = row.get(DESCRIPTION_DIGEST_COLUMN)
    return value.strip() if isinstance(value, str) else None


def decision_entry(status: str, note: str, decision_date: str | None, digest: str | None) -> dict[str, str | None]:
    return {
        "status": status,
        "note": note,
        "decision_date": decision_date,
        "description_digest": digest,
    }


def decision_log(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the record's decision entries, oldest first."""
    log = record.get(DECISION_LOG_FIELD)
    if not isinstance(log, list):
        return []
    return [entry for entry in log if isinstance(entry, dict)]


def record_returned_decisions(
    record: dict[str, Any],
    previous_status: str,
    previous_note: str,
    rows: list[dict[str, Any]],
) -> None:
    """Log the decisions ``rows`` made on a record that now shows the last of them.

    ``previous_status`` and ``previous_note`` are what the record showed before
    the rows were applied. A record with no log yet keeps that earlier decision
    as an undated entry. Every row but the last is logged as it would apply in
    turn; the last entry is what the record now shows, so the latest entry and
    ``editor_status``/``editor_note`` always agree.

    Pass every row one consumption applies to the record in one call: when the
    log already ends with one entry per row that agrees with everything the
    row itself says, the rows were consumed before (a retried finalization)
    and nothing is added. The check reads only the rows, never the record's
    state before them, which a retry no longer has. Entries carry their date
    and description digest, so only a same-day repeat of the same batch on the
    same description is taken for a retry.
    """
    if not rows:
        return
    log = decision_log(record)
    if _already_logged(log, rows):
        return
    if not log and (previous_status or previous_note):
        log.append(decision_entry(previous_status, previous_note, None, None))
    entries = []
    status, note = previous_status, previous_note
    for row in rows[:-1]:
        row_status = str(row.get("Editor Status", "")).strip()
        if row_status in EDITOR_STATUSES:
            status = row_status
        if "Editor Note" in row:
            note = str(row.get("Editor Note", ""))
        entries.append(decision_entry(status, note, _row_decision_date(row), _row_description_digest(row)))
    last = rows[-1]
    entries.append(
        decision_entry(
            str(record.get("editor_status", "") or ""),
            str(record.get("editor_note", "") or ""),
            _row_decision_date(last),
            _row_description_digest(last),
        )
    )
    log.extend(entries)
    record[DECISION_LOG_FIELD] = log


def _already_logged(log: list[dict[str, Any]], rows: list[dict[str, Any]]) -> bool:
    if len(log) < len(rows):
        return False
    for entry, row in zip(log[-len(rows) :], rows, strict=True):
        if entry.get("decision_date") != _row_decision_date(row):
            return False
        if entry.get("description_digest") != _row_description_digest(row):
            return False
        status = str(row.get("Editor Status", "")).strip()
        if status in EDITOR_STATUSES and entry.get("status") != status:
            return False
        if "Editor Note" in row and entry.get("note") != str(row.get("Editor Note", "")):
            return False
    return True


def merge_decision_logs(records: list[dict[str, Any]], *, keep_unlogged: bool = False) -> list[dict[str, Any]]:
    """Combine the logs of records merged into one book, oldest first; undated entries lead.

    With ``keep_unlogged`` a record that shows a decision but has no log yet
    contributes that decision as an undated entry, because a merge that
    chooses another decision is about to replace it.
    """
    # Equal entries are separate decisions (on separate records, or a repeat
    # on one), so every entry is kept.
    combined: list[dict[str, Any]] = []
    for record in records:
        entries = decision_log(record)
        status = str(record.get("editor_status", "") or "")
        note = str(record.get("editor_note", "") or "")
        if not entries and keep_unlogged and (status or note):
            entries = [decision_entry(status, note, None, None)]
        combined.extend(dict(entry) for entry in entries)
    # A stable sort keeps each record's own order among entries of one date.
    return sorted(
        combined, key=lambda entry: (entry.get("decision_date") is not None, entry.get("decision_date") or "")
    )


def record_resolved_decision(record: dict[str, Any], today: date | None = None) -> None:
    """Log the decision the editor chose in a confirmed identity merge, dated today."""
    log = decision_log(record)
    log.append(
        decision_entry(
            str(record.get("editor_status", "") or ""),
            str(record.get("editor_note", "") or ""),
            (today or date.today()).isoformat(),
            description_digest(record),
        )
    )
    record[DECISION_LOG_FIELD] = log
