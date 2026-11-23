"""Record a whole returned file of editor decisions in one confirmed batch (#1153).

The editor returns a review as one file of 200 to 300 decisions. The batch
applies the same two-phase protocol as ``record-feedback``, once for the
whole file:

- The preview resolves every row the way ``resolve-review-rows`` does and
  prints, per row, the lines the single preview prints, then the counts per
  status and a digest of the resolved payload. It writes nothing (code 3).
- The confirmed write takes the preview's digest, resolves the file again in
  one store transaction, refuses when the digest differs, and stages every
  row with one write of the pending rows. Each staged row is the row
  ``record-feedback --confirmed-by-editor`` stages for the same decision, in
  file order, so the batch leaves the same state as the rows recorded one by
  one.

A row that does not resolve to exactly one candidate fails the whole batch,
and so does a row whose status, note or dimensions the single command would
refuse; every failing row is named and nothing is staged. A refused call
creates nothing: everything is checked outside any store transaction first.

A confirmed batch whose rows already end the pending rows is refused, so a
retry after a failed export (the rows were staged, the export was not
written) never stages them twice.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autoscout.editor_decisions import DECISION_DATE_COLUMN
from autoscout.editor_decisions import EDITOR_STATUSES
from autoscout.editor_feedback import KnownCandidates
from autoscout.editor_feedback import canonical_editor_status
from autoscout.editor_feedback import print_feedback_preview
from autoscout.editor_feedback import review_statuses
from autoscout.editor_feedback import staged_feedback_row
from autoscout.feedback_dimensions import DIMENSION_ATTRS
from autoscout.feedback_dimensions import FeedbackDimensionError
from autoscout.feedback_dimensions import canonical_dimensions_from_raw
from autoscout.feedback_dimensions import validate_note_dimensions
from autoscout.review_row_resolution import RESOLVED
from autoscout.review_row_resolution import ReturnedRow
from autoscout.review_row_resolution import ReviewRowResolver
from autoscout.state_store import Collection
from autoscout.state_store import StateStore
from autoscout.state_store import open_state_store

IDENTIFIER_FIELDS = ("identity_key", "candidate_id")
# The fields a decision may carry: an identifier, the status, the verbatim
# note, and the structured dimensions under their record-feedback flag names.
DECISION_FIELDS = frozenset({*IDENTIFIER_FIELDS, "status", "note", *DIMENSION_ATTRS.values()})
# The exit codes: an unreadable decisions file is an input error, as in
# resolve-review-rows; a refused batch is 1 and its preview 3.
REFUSED_EXIT = 1
INPUT_ERROR_EXIT = 2
PREVIEW_EXIT = 3


class FeedbackBatchInputError(ValueError):
    """The decisions file cannot be read as a list of decisions."""


@dataclass(frozen=True)
class Decision:
    """One validated row of the decisions file."""

    row: int
    identity_key: str
    candidate_id: str
    status: str
    note: str
    dimensions: dict[str, str]


@dataclass(frozen=True)
class ResolvedDecision:
    decision: Decision
    identity_key: str
    matched_by: str
    warnings: tuple[str, ...]


def read_decisions(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise FeedbackBatchInputError(f"cannot read decisions file: {path.name}") from exc
    except UnicodeDecodeError as exc:
        raise FeedbackBatchInputError(f"the decisions file is not UTF-8: {path.name}") from exc
    except (json.JSONDecodeError, RecursionError) as exc:
        # A deeply nested document exhausts the parser's recursion; it is no decisions file either.
        raise FeedbackBatchInputError(f"invalid JSON: {path.name}") from exc
    try:
        # A JSON escape can still name a lone surrogate, which no note can hold.
        json.dumps(value, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise FeedbackBatchInputError(f"the decisions file holds text that is not valid Unicode: {path.name}") from exc
    except RecursionError as exc:
        raise FeedbackBatchInputError(f"invalid JSON: {path.name}") from exc
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise FeedbackBatchInputError("the decisions file must be a JSON list of objects")
    if not value:
        raise FeedbackBatchInputError("the decisions file holds no decisions")
    return value


def _validated(number: int, raw: dict[str, Any]) -> tuple[Decision | None, list[str]]:
    """One row as a decision, or the reasons the single command would refuse it."""
    problems: list[str] = []
    unknown = sorted(set(raw) - DECISION_FIELDS)
    if unknown:
        problems.append(f"unknown_fields={','.join(unknown)}")
    texts: dict[str, str] = {}
    for field in sorted(DECISION_FIELDS):
        value = raw.get(field, "")
        if not isinstance(value, str):
            problems.append(f"not_text={field}")
            value = ""
        texts[field] = value
    if not any(texts[field].strip() for field in IDENTIFIER_FIELDS):
        problems.append("missing_identifier")
    status = canonical_editor_status(texts["status"])
    if not status:
        problems.append(f"unsupported_status={texts['status']}")
    try:
        dimensions = canonical_dimensions_from_raw({name: texts[attr] for name, attr in DIMENSION_ATTRS.items()})
        validate_note_dimensions(texts["note"], dimensions)
    except FeedbackDimensionError as exc:
        problems.append(str(exc))
        dimensions = {}
    if problems:
        return None, problems
    return (
        Decision(
            row=number,
            identity_key=texts["identity_key"],
            candidate_id=texts["candidate_id"],
            status=status,
            note=texts["note"],
            dimensions=dimensions,
        ),
        [],
    )


# A refused row: its number and the lines that name it.
RowErrors = list[tuple[int, str]]


def validate_decisions(raw_rows: list[dict[str, Any]]) -> tuple[list[Decision], RowErrors]:
    decisions: list[Decision] = []
    errors: RowErrors = []
    for number, raw in enumerate(raw_rows, start=1):
        decision, problems = _validated(number, raw)
        if decision is None:
            errors.extend((number, f"invalid_row row={number} {problem}") for problem in problems)
        else:
            decisions.append(decision)
    return decisions, errors


def _resolve(
    decisions: list[Decision], store: StateStore, known: KnownCandidates
) -> tuple[list[ResolvedDecision], RowErrors]:
    """Resolve every decision the way ``resolve-review-rows`` does; every unresolved row is named.

    A book decided in more than one row keeps every row, as recorded one by
    one, and each of them names the others in its warnings.
    """
    resolver = ReviewRowResolver(known.history, store.read(Collection.CANDIDATE_IDENTITY_ALIASES))
    matches: list[tuple[Decision, str, dict[str, Any]]] = []
    errors: RowErrors = []
    for decision in decisions:
        result = resolver.resolve(
            ReturnedRow.from_mapping({"identity_key": decision.identity_key, "candidate_id": decision.candidate_id}),
            decision.row,
        )
        candidate = result.get("candidate", {})
        identity_key = str(candidate.get("identity_key", "")).strip()
        if result["status"] == RESOLVED and not identity_key:
            errors.append(
                (
                    decision.row,
                    f"unstageable row={decision.row} reason=candidate_without_identity_key "
                    f"candidate_id={candidate.get('candidate_id', '')}",
                )
            )
            continue
        if result["status"] != RESOLVED:
            errors.append(
                (
                    decision.row,
                    f"unresolved row={decision.row} status={result['status']} "
                    f"identity_key={decision.identity_key} candidate_id={decision.candidate_id}",
                )
            )
            for alternative in result.get("alternatives", []):
                errors.append(
                    (
                        decision.row,
                        f"alternative row={decision.row} title={alternative['title']} "
                        f"author={alternative['author_display']} identity_key={alternative['identity_key']} "
                        f"editor_status={alternative['editor_status']}",
                    )
                )
            continue
        matches.append((decision, identity_key, result))
    rows_by_key: dict[str, list[int]] = defaultdict(list)
    for decision, identity_key, _result in matches:
        rows_by_key[identity_key].append(decision.row)
    resolved: list[ResolvedDecision] = []
    for decision, identity_key, result in matches:
        warnings = list(result["warnings"])
        others = [str(row) for row in rows_by_key[identity_key] if row != decision.row]
        if others:
            warnings.append(f"same_book_in_rows={'+'.join(others)}")
        resolved.append(ResolvedDecision(decision, identity_key, result["matched_by"], tuple(warnings)))
    return resolved, errors


def payload_digest(resolved: list[ResolvedDecision]) -> str:
    """The SHA-256 of what the batch stages: per row, in order, the resolved identity key, status, note and dimensions."""
    payload = [
        {
            "identity_key": item.identity_key,
            "status": item.decision.status,
            "note": item.decision.note,
            "dimensions": item.decision.dimensions,
        }
        for item in resolved
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _refuse(errors: RowErrors) -> int:
    """Name every refused row; nothing is staged."""
    print("batch_refused=true")
    print(f"refused_rows={len({row for row, _line in errors})}")
    for _row, line in errors:
        print(line)
    if any("unsupported_status=" in line for _row, line in errors):
        print(f"Supported statuses: {', '.join(sorted(EDITOR_STATUSES))}")
    print("staged_rows=0")
    return REFUSED_EXIT


def _refuse_batch(error: str) -> int:
    print(f"error={error}")
    print("batch_refused=true")
    print("staged_rows=0")
    return REFUSED_EXIT


def _without_decision_date(row: dict[str, Any]) -> dict[str, Any]:
    return {column: value for column, value in row.items() if column != DECISION_DATE_COLUMN}


def _already_staged(pending: list[Any], staged: list[dict[str, Any]]) -> bool:
    """Whether the pending rows already end with exactly these rows, whatever day they were confirmed."""
    tail = pending[-len(staged) :] if len(pending) >= len(staged) else []
    return all(isinstance(row, dict) for row in tail) and [_without_decision_date(row) for row in tail] == [
        _without_decision_date(row) for row in staged
    ]


def _print_preview(resolved: list[ResolvedDecision], known: KnownCandidates, store: StateStore, digest: str) -> None:
    statuses = review_statuses(store)
    print("confirmation_required=true")
    print(f"batch_rows={len(resolved)}")
    for status, count in sorted(Counter(item.decision.status for item in resolved).items()):
        print(f"status_count_{status}={count}")
    print(f"batch_digest={digest}")
    for item in resolved:
        details = known.details(item.identity_key)
        assert details is not None
        print(f"row={item.decision.row}")
        print(f"matched_by={item.matched_by}")
        if item.warnings:
            print(f"warnings={','.join(item.warnings)}")
        print_feedback_preview(
            details,
            item.decision.status,
            item.decision.note,
            item.decision.dimensions,
            statuses.get(item.identity_key, ""),
        )
    print("action=stage_pending_feedback")
    print(f"hint=re-run with --confirmed-by-editor --digest {digest} to apply")


def record_feedback_batch(
    decisions_path: Path, state_dir: Path, confirmed_by_editor: bool = False, digest: str = ""
) -> int:
    """Preview (code 3, zero mutation) or stage every decision of a returned file.

    Code 1 refuses the whole batch and stages nothing, 2 is an unreadable
    decisions file. A confirmed write that succeeded but whose export failed
    afterwards also ends with 1 (``state_export=failed``, from the CLI); its
    rows are staged, and repeating it is refused as ``batch_already_staged``.
    """
    try:
        raw_rows = read_decisions(decisions_path)
    except FeedbackBatchInputError as exc:
        print(f"error={exc}")
        return INPUT_ERROR_EXIT
    decisions, errors = validate_decisions(raw_rows)
    if errors:
        return _refuse(errors)
    if confirmed_by_editor and not digest.strip():
        return _refuse_batch("batch_digest_required: pass --digest with the batch_digest the preview printed")
    store = open_state_store(state_dir)
    # Checked first outside any transaction, so a refused call creates
    # nothing: under SQLite a transaction creates the database at its first read.
    known = KnownCandidates(store)
    resolved, errors = _resolve(decisions, store, known)
    if errors:
        return _refuse(errors)
    batch_digest = payload_digest(resolved)
    if not confirmed_by_editor:
        _print_preview(resolved, known, store, batch_digest)
        return PREVIEW_EXIT
    if batch_digest != digest.strip():
        return _refuse_batch(f"batch_digest_mismatch previewed={digest.strip()} current={batch_digest}")

    state_dir.mkdir(parents=True, exist_ok=True)
    # The rows are resolved again, checked against the preview and staged in
    # one store transaction, with one write of the pending rows: all or nothing.
    with store.transaction():
        known = KnownCandidates(store)
        resolved, errors = _resolve(decisions, store, known)
        if errors:
            return _refuse(errors)
        batch_digest = payload_digest(resolved)
        if batch_digest != digest.strip():
            return _refuse_batch(f"batch_digest_mismatch previewed={digest.strip()} current={batch_digest}")
        pending = store.read(Collection.PENDING_EDITOR_ROWS)
        staged = [
            staged_feedback_row(
                item.identity_key, item.decision.status, item.decision.note, item.decision.dimensions, known
            )
            for item in resolved
        ]
        if _already_staged(pending, staged):
            return _refuse_batch("batch_already_staged: the pending rows already end with these decisions")
        pending.extend(staged)
        store.replace(Collection.PENDING_EDITOR_ROWS, pending)
    for item in resolved:
        print(f"recorded_feedback identity_key={item.identity_key} status={item.decision.status}")
    print(f"staged_rows={len(resolved)}")
    return 0
