"""Reusable, non-publishing Codex extraction observation seam.

This module measures one extraction attempt without writing canonical weekly
artifacts or resume evidence.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import Protocol

from autoscout.bookmarks_contract import is_bookmarks_source_identity
from autoscout.codex_recovery import ExtractionAgent
from autoscout.codex_recovery import ExtractionAttempt
from autoscout.codex_recovery import ExtractionStatus
from autoscout.codex_recovery import build_codex_extraction_prompt
from autoscout.extraction_completeness import capture_has_listing_rows
from autoscout.partial_list import GATE_DROPPED_ENTRIES_FIELD
from autoscout.partial_list import GATE_EXTRACTED_COUNT_FIELD
from autoscout.partial_list import GATE_RESERVED_FIELDS
from autoscout.partial_list import keeps_partial_list
from autoscout.ranking_author_policy import work_item_author_exempt

if TYPE_CHECKING:
    from autoscout.source_collectors.codex_extraction import MissingRequiredFieldsAggregate


class CancellationLike(Protocol):
    @property
    def cancelled(self) -> bool: ...


@dataclass(frozen=True)
class ValidationObservation:
    """Structured validation result for one staged extraction artifact."""

    valid: bool
    diagnostic_class: str | None
    raw_extracted_count: int | None
    validated_count: int | None
    output_bytes: int | None
    missing_required_fields: MissingRequiredFieldsAggregate | None = None
    identity_mismatches: tuple[dict[str, str], ...] = ()
    # A valid artifact whose failing rows are dropped rather than sinking the
    # list (Issue #839): their positions in the artifact and their reasons.
    dropped_indexes: tuple[int, ...] = ()
    dropped_rows: tuple[dict[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "valid": self.valid,
            "diagnostic_class": self.diagnostic_class,
            "raw_extracted_count": self.raw_extracted_count,
            "validated_count": self.validated_count,
            "output_bytes": self.output_bytes,
        }
        if self.missing_required_fields is not None:
            result["validation_missing_required_fields"] = self.missing_required_fields.to_dict()
        if self.identity_mismatches:
            result["identity_mismatches"] = [dict(mismatch) for mismatch in self.identity_mismatches]
        if self.dropped_rows:
            result["dropped_rows"] = [dict(row) for row in self.dropped_rows]
        return result


@dataclass(frozen=True)
class ObservedExtraction:
    """Result of one observed extraction attempt.

    The result contains no canonical publication or resume mutation.  A
    caller owns the supplied staging path and decides whether to remove it.
    """

    source_name: str
    source_url: str
    operation_id: str
    capture_sha256: str
    final_status: str
    diagnostic_class: str
    capture_bytes: int | None
    capture_unicode_chars: int | None
    generated_prompt_chars: int | None
    output_bytes: int | None
    raw_extracted_count: int | None
    validated_count: int | None
    queue_wait_seconds: float | None
    subprocess_duration_seconds: float | None
    validation_duration_seconds: float | None
    collection_duration_seconds: float | None
    total_duration_seconds: float | None
    applied_timeout_ceiling_seconds: float | None
    timeout_stage: str | None
    applied_timeout_threshold_seconds: float | None
    boundary_supplier: str | None
    worker_slot: int | None
    submission_sequence: int | None
    exit_code: int | None
    validation: ValidationObservation | None
    started: bool
    scheduled_before_budget_exhaustion: bool
    extraction_attempt: ExtractionAttempt | None = None
    diagnostic: str | None = None
    diagnostic_artifact: str | None = None
    subprocess_started_at: float | None = None
    subprocess_finished_at: float | None = None
    progress_event_counts: dict[str, int] | None = None
    no_progress_quiet_seconds: float | None = None


@dataclass(frozen=True)
class _CaptureRead:
    bytes_count: int | None
    unicode_chars: int | None
    valid: bool
    diagnostic_class: str | None


def _is_cancelled(cancellation: CancellationLike | Callable[[], bool] | None) -> bool:
    if cancellation is None:
        return False
    if callable(cancellation):
        return bool(cancellation())
    return bool(cancellation.cancelled)


def _round_seconds(value: float) -> float:
    return round(max(0.0, value), 6)


def _read_capture(capture_path: Path, item: Mapping[str, Any]) -> _CaptureRead:
    try:
        raw = capture_path.read_bytes()
    except OSError:
        return _CaptureRead(None, None, False, "capture_unavailable")

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _CaptureRead(len(raw), None, False, "capture_invalid_encoding")

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return _CaptureRead(len(raw), len(text), False, "capture_malformed_json")
    if not isinstance(data, dict):
        return _CaptureRead(len(raw), len(text), False, "capture_not_object")

    expected_url = str(item.get("source_url", ""))
    actual_url = str(data.get("source_url") or data.get("source_page") or "")
    if expected_url and actual_url != expected_url:
        return _CaptureRead(len(raw), len(text), False, "capture_source_mismatch")
    expected_name = str(item.get("source_name", ""))
    actual_name = str(data.get("source_name", ""))
    if actual_name and expected_name and actual_name != expected_name:
        return _CaptureRead(len(raw), len(text), False, "capture_source_mismatch")
    expected_digest = str(item.get("capture_sha256", ""))
    if expected_digest and hashlib.sha256(raw).hexdigest() != expected_digest:
        return _CaptureRead(len(raw), len(text), False, "capture_digest_mismatch")
    return _CaptureRead(len(raw), len(text), True, None)


def _mismatch_kind(field: str, expected: str, actual: Any) -> str:
    if actual is None or actual == "":
        return "missing"
    if not isinstance(actual, str):
        return "not_a_string"
    if actual.strip() == expected:
        return "whitespace_changed"
    if actual.lower() == expected.lower():
        return "case_changed"
    if field == "source_url" and actual.rstrip("/").split("://")[-1] == expected.rstrip("/").split("://")[-1]:
        return "url_variant"
    if len(actual) < len(expected) and (expected.startswith(actual) or expected.endswith(actual)):
        return "truncated"
    if len(actual) == len(expected):
        return "different_value_same_length"
    return "different_value_shorter" if len(actual) < len(expected) else "different_value_longer"


def identity_envelope_mismatches(identities: Mapping[str, str], data: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    """Name each envelope field that does not match the work item, and how.

    The four fields used to be compared as a group, so a refusal never said
    which one the extractor miscopied (Issue #823). Each record holds only the
    field name and a bounded kind, never the values themselves.
    """
    mismatches: list[dict[str, str]] = []
    for field, expected in identities.items():
        if not expected:
            mismatches.append({"field": field, "kind": "missing_in_work_item"})
            continue
        actual = data.get(field)
        if actual != expected:
            mismatches.append({"field": field, "kind": _mismatch_kind(field, expected, actual)})
    return tuple(mismatches)


def _validation_diagnostic(
    path: Path,
    item: Mapping[str, Any],
    capture_path: Path,
) -> ValidationObservation:
    try:
        output_bytes = path.stat().st_size
    except OSError:
        return ValidationObservation(False, "missing_output", None, None, None)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return ValidationObservation(False, "invalid_output_encoding", None, None, output_bytes)
    except json.JSONDecodeError:
        return ValidationObservation(False, "malformed_json", None, None, output_bytes)
    if not isinstance(data, dict):
        return ValidationObservation(False, "non_object_json", None, None, output_bytes)

    if "entries" in data:
        entries = data["entries"]
    elif "mentions" in data:
        entries = data["mentions"]
    else:
        entries = None
    if not isinstance(entries, list):
        return ValidationObservation(False, "missing_entries", None, None, output_bytes)
    raw_count = len(entries)

    identities = {
        "source_name": str(item.get("source_name", "")),
        "source_url": str(item.get("source_url", "")),
        "operation_id": str(item.get("operation_id", "")),
        "capture_sha256": str(item.get("capture_sha256", "")),
    }
    mismatches = identity_envelope_mismatches(identities, data)
    if mismatches:
        return ValidationObservation(
            False, "identity_mismatch", raw_count, None, output_bytes, identity_mismatches=mismatches
        )

    try:
        capture_data = json.loads(capture_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ValidationObservation(False, "invalid_capture", raw_count, None, output_bytes)
    if not isinstance(capture_data, dict):
        return ValidationObservation(False, "invalid_capture", raw_count, None, output_bytes)

    from autoscout.source_collectors import FORBIDDEN_COLLECTOR_RESPONSE_FIELDS
    from autoscout.source_collectors import _validate_codex_raw_required_fields
    from autoscout.source_collectors import codex_missing_field_aggregate

    if any(isinstance(entry, dict) and FORBIDDEN_COLLECTOR_RESPONSE_FIELDS & set(entry) for entry in entries):
        return ValidationObservation(False, "forbidden_copied_text", raw_count, 0, output_bytes)

    # A ranking source that declares author-less rows valid keeps them here
    # too, or a partial artifact would publish without them (Issue #839).
    author_optional = work_item_author_exempt(item)
    valid_entries, dropped = _validate_codex_raw_required_fields(entries, author_optional=author_optional)
    bookmarks = is_bookmarks_source_identity(item)
    if dropped and not valid_entries:
        return ValidationObservation(
            False,
            "missing_required_fields",
            raw_count,
            0,
            output_bytes,
            missing_required_fields=codex_missing_field_aggregate(entries, author_optional=author_optional),
        )

    capture_url = str(capture_data.get("source_url") or capture_data.get("source_page") or "")
    if capture_url != identities["source_url"]:
        return ValidationObservation(False, "capture_source_mismatch", raw_count, len(valid_entries), output_bytes)
    try:
        capture_digest = hashlib.sha256(capture_path.read_bytes()).hexdigest()
    except OSError:
        return ValidationObservation(False, "capture_unavailable", raw_count, len(valid_entries), output_bytes)
    if capture_digest != identities["capture_sha256"]:
        return ValidationObservation(False, "capture_digest_mismatch", raw_count, len(valid_entries), output_bytes)
    if not entries and capture_has_listing_rows(item, capture_data):
        return ValidationObservation(False, "unexpected_empty_extraction", 0, 0, output_bytes)

    # Each row is checked on its own (Issue #839). A row that fails is
    # dropped with its reason; the artifact is refused only when fewer than
    # 80% of its rows pass, and then with the class it reported before.
    row_failures = _artifact_row_failures(
        entries, item, capture_data, bookmarks=bookmarks, author_optional=author_optional
    )
    if not row_failures:
        return ValidationObservation(True, None, raw_count, len(valid_entries), output_bytes)
    # A row the collector would drop for a missing field also counts once.
    field_failures = [] if bookmarks else _field_failures(entries, author_optional=author_optional)
    failed_indexes = {index for index, _class, _row in row_failures}
    all_failures = sorted(
        [*row_failures, *(failure for failure in field_failures if failure[0] not in failed_indexes)],
        key=lambda failure: failure[0],
    )
    kept = raw_count - len(all_failures)
    if not keeps_partial_list(kept, raw_count):
        diagnostic = _refusal_class(row_failures)
        return ValidationObservation(
            False,
            diagnostic,
            raw_count,
            0 if diagnostic == "missing_required_fields" else len(valid_entries),
            output_bytes,
            missing_required_fields=(
                codex_missing_field_aggregate(entries, author_optional=author_optional)
                if diagnostic == "missing_required_fields"
                else None
            ),
        )
    return ValidationObservation(
        True,
        None,
        raw_count,
        kept,
        output_bytes,
        dropped_indexes=tuple(index for index, _class, _row in all_failures),
        dropped_rows=tuple(row for _index, _class, row in all_failures),
    )


_EXACT_LIST_METADATA_CLASSES = frozenset(
    {"invalid_signal_metadata", "invalid_candidate_category", "invalid_helper_metadata"}
)

RowFailure = tuple[int, str, dict[str, str]]


def _refusal_class(failures: list[RowFailure]) -> str:
    """The class a refused artifact reports, in the order the checks used to run.

    Missing fields came first, then the first row with wrong exact-list
    metadata, then the first row with unverifiable provenance.
    """
    classes = [diagnostic for _index, diagnostic, _row in failures]
    if "missing_required_fields" in classes:
        return "missing_required_fields"
    metadata = [diagnostic for diagnostic in classes if diagnostic in _EXACT_LIST_METADATA_CLASSES]
    return metadata[0] if metadata else classes[0]


def _failed_row(entry: Any, index: int, diagnostic: str) -> dict[str, str]:
    record = entry if isinstance(entry, dict) else {}
    row = {
        "title": str(record.get("title") or f"mention {index + 1}"),
        "evidence_url": str(record.get("evidence_url", "")),
        "reason": diagnostic,
    }
    # A chart may miss only the ranks its dropped rows claimed (Issue #839).
    if str(record.get("current_rank", "")).strip():
        row["current_rank"] = str(record["current_rank"]).strip()
    return row


def _field_failures(entries: list[Any], *, author_optional: bool) -> list[RowFailure]:
    """Rows missing a required field: the collector drops these on its own."""
    from autoscout.source_collectors import _validate_codex_raw_required_fields

    failures: list[RowFailure] = []
    for index, entry in enumerate(entries):
        _valid, dropped = _validate_codex_raw_required_fields([entry], author_optional=author_optional)
        if dropped:
            failures.append((index, "missing_required_fields", _failed_row(entry, index, dropped[0]["reason"])))
    return failures


def _artifact_row_failures(
    entries: list[Any],
    item: Mapping[str, Any],
    capture_data: dict[str, Any],
    *,
    bookmarks: bool,
    author_optional: bool,
) -> list[RowFailure]:
    """Every row the gate refuses on its own, with its position and class.

    Rows missing a required field count here only for BookMarks, whose
    contract refuses them at the gate; elsewhere the collector drops them,
    but an exact list still refuses their wrong list metadata here, as it
    did before Issue #839.
    """
    from autoscout.codex_recovery import _exact_list_metadata_failure_status
    from autoscout.source_collectors import codex_provenance_links

    field_failures = _field_failures(entries, author_optional=author_optional)
    failed_fields = {index for index, _class, _row in field_failures}
    failures: list[RowFailure] = list(field_failures) if bookmarks else []
    verified_links = codex_provenance_links(capture_data, item)
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or (bookmarks and index in failed_fields):
            continue
        diagnostic = _exact_list_metadata_failure_status(entry, dict(item))
        if not diagnostic and index not in failed_fields:
            diagnostic = _provenance_failure(entry, item, capture_data, verified_links)
        if diagnostic:
            failures.append((index, diagnostic, _failed_row(entry, index, diagnostic)))
    return failures


def _provenance_failure(
    entry: dict[str, Any],
    item: Mapping[str, Any],
    capture_data: dict[str, Any],
    verified_links: Any,
) -> str:
    from autoscout.source_collectors import evidence_url_verified
    from autoscout.source_collectors import normalize_codex_provenance_url

    evidence_url = str(entry.get("evidence_url", "")).strip()
    source_url = str(entry.get("source_url", "")).strip()
    if not evidence_url or not source_url:
        return "missing_provenance"
    resolved_evidence_url = normalize_codex_provenance_url(evidence_url, item)
    resolved_source_url = normalize_codex_provenance_url(source_url, item)
    if resolved_evidence_url is None or resolved_source_url is None:
        return "invalid_evidence_url"
    if not (
        resolved_evidence_url.startswith(("http://", "https://"))
        and resolved_source_url.startswith(("http://", "https://"))
    ):
        return "invalid_evidence_url"
    evidence_ok, _ = evidence_url_verified(capture_data, resolved_evidence_url, links=verified_links)
    source_ok, _ = evidence_url_verified(capture_data, resolved_source_url, links=verified_links)
    if not evidence_ok or not source_ok:
        return "unverifiable_provenance"
    return ""


def partial_artifact_payload(data: dict[str, Any], validation: ValidationObservation) -> dict[str, Any]:
    """The artifact as the gate publishes it: only the rows that passed.

    The removed rows travel in the artifact with their reasons, so the
    collector counts them and the source run names them (Issue #839).
    """
    entries_key = "entries" if "entries" in data else "mentions"
    entries = data.get(entries_key)
    entries = entries if isinstance(entries, list) else []
    dropped = set(validation.dropped_indexes)
    # Only the list the gate validated is published: an alias list would
    # carry the refused rows past it.
    payload = {key: value for key, value in data.items() if key not in GATE_RESERVED_FIELDS | {"mentions"}}
    payload[entries_key] = [entry for index, entry in enumerate(entries) if index not in dropped]
    if dropped:
        payload[GATE_DROPPED_ENTRIES_FIELD] = [dict(row) for row in validation.dropped_rows]
        payload[GATE_EXTRACTED_COUNT_FIELD] = len(entries)
    return payload


def publish_partial_artifact(path: Path, validation: ValidationObservation) -> None:
    """Rewrite a validated staged artifact without its failing rows.

    A fully valid artifact is left byte for byte, unless it carries a reserved
    gate field it must not (Codex cannot state what the gate dropped) or a
    second list alias the gate did not validate.
    """
    if not validation.valid:
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return
    both_lists = "entries" in data and "mentions" in data
    if not validation.dropped_indexes and not both_lists and not (GATE_RESERVED_FIELDS & set(data)):
        return
    temporary = path.with_name(path.name + ".partial")
    temporary.write_text(
        json.dumps(partial_artifact_payload(data, validation), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def validate_extraction_artifact(path: Path, item: Mapping[str, Any], capture_path: Path) -> ValidationObservation:
    """Validate a staged artifact with specific production-aligned diagnostics."""
    return _validation_diagnostic(path, item, capture_path)


def _failure_class(attempt: ExtractionAttempt) -> str:
    diagnostic = attempt.diagnostic.casefold()
    if "codex_no_progress_timeout" in diagnostic:
        return "codex_no_progress_timeout"
    if "codex_absolute_ceiling" in diagnostic:
        return "codex_absolute_ceiling"
    if "worker_collection_timeout" in diagnostic:
        return "worker_collection_timeout"
    if "codex_timeout" in diagnostic or attempt.status is ExtractionStatus.TIMEOUT:
        return "codex_timeout"
    if attempt.status is ExtractionStatus.UNAVAILABLE:
        return "command_unavailable"
    if attempt.status is ExtractionStatus.COMMAND_FAILED:
        if any(term in diagnostic for term in ("auth", "login")):
            return "command_failed_authentication"
        if "permission" in diagnostic:
            return "command_failed_permission"
        if "configuration" in diagnostic or "config" in diagnostic:
            return "command_failed_configuration"
        return "command_failed"
    return str(attempt.status.value)


def _observation(
    item: Mapping[str, Any],
    *,
    final_status: str,
    diagnostic_class: str,
    capture: _CaptureRead,
    prompt_chars: int | None,
    output_bytes: int | None = None,
    raw_count: int | None = None,
    validated_count: int | None = None,
    queue_wait: float | None = None,
    subprocess_duration: float | None = None,
    validation_duration: float | None = None,
    collection_duration: float | None = None,
    total_duration: float | None = None,
    applied_timeout_ceiling: float | None = None,
    exit_code: int | None = None,
    validation: ValidationObservation | None = None,
    timeout_stage: str | None = None,
    applied_timeout_threshold: float | None = None,
    boundary_supplier: str | None = None,
    worker_slot: int | None = None,
    submission_sequence: int | None = None,
    started: bool = False,
    scheduled_before_budget_exhaustion: bool = True,
    extraction_attempt: ExtractionAttempt | None = None,
    subprocess_started_at: float | None = None,
    subprocess_finished_at: float | None = None,
    progress_event_counts: dict[str, int] | None = None,
    no_progress_quiet_seconds: float | None = None,
) -> ObservedExtraction:
    return ObservedExtraction(
        source_name=str(item.get("source_name", "")),
        source_url=str(item.get("source_url", "")),
        operation_id=str(item.get("operation_id", "")),
        capture_sha256=str(item.get("capture_sha256", "")),
        final_status=final_status,
        diagnostic_class=diagnostic_class,
        capture_bytes=capture.bytes_count,
        capture_unicode_chars=capture.unicode_chars,
        generated_prompt_chars=prompt_chars,
        output_bytes=output_bytes,
        raw_extracted_count=raw_count,
        validated_count=validated_count,
        queue_wait_seconds=queue_wait,
        subprocess_duration_seconds=subprocess_duration,
        validation_duration_seconds=validation_duration,
        collection_duration_seconds=collection_duration,
        total_duration_seconds=total_duration,
        applied_timeout_ceiling_seconds=applied_timeout_ceiling,
        timeout_stage=timeout_stage,
        applied_timeout_threshold_seconds=applied_timeout_threshold,
        boundary_supplier=boundary_supplier,
        worker_slot=worker_slot,
        submission_sequence=submission_sequence,
        exit_code=exit_code,
        validation=validation,
        started=started,
        scheduled_before_budget_exhaustion=scheduled_before_budget_exhaustion,
        extraction_attempt=extraction_attempt,
        subprocess_started_at=subprocess_started_at,
        subprocess_finished_at=subprocess_finished_at,
        progress_event_counts=progress_event_counts,
        no_progress_quiet_seconds=no_progress_quiet_seconds,
    )


def observe_extraction(
    item: Mapping[str, Any],
    capture_path: Path,
    staging_path: Path,
    *,
    timeout_seconds: float | None = None,
    deadline: float | None = None,
    cancellation: CancellationLike | Callable[[], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
    agent: ExtractionAgent | None = None,
    submitted_at: float | None = None,
    boundary_supplier_hint: str | None = None,
    phase_telemetry: Any | None = None,
    raise_agent_exceptions: bool = False,
) -> ObservedExtraction:
    """Observe one exact extraction attempt without canonical publication.

    ``staging_path`` must be unique to the attempt.
    ``timeout_seconds`` is the per-observation ceiling.
    ``deadline`` is an optional total-budget deadline.
    ``cancellation`` is checked before invocation and before returning.
    ``clock`` is injected so tests can use a bounded fake clock.
    """
    started_at = submitted_at if submitted_at is not None else clock()
    capture = _read_capture(capture_path, item)
    queue_wait = None
    if not capture.valid:
        return _observation(
            item,
            final_status="input_invalid",
            diagnostic_class=capture.diagnostic_class or "input_invalid",
            capture=capture,
            prompt_chars=None,
            queue_wait=queue_wait,
            total_duration=_round_seconds(clock() - started_at),
            scheduled_before_budget_exhaustion=True,
        )

    prompt_chars = len(build_codex_extraction_prompt(dict(item)))
    if phase_telemetry is not None:
        phase_telemetry.mark_attempt_started(clock())
    if _is_cancelled(cancellation):
        return _observation(
            item,
            final_status="budget_exhausted",
            diagnostic_class="cancelled_before_execution",
            capture=capture,
            prompt_chars=prompt_chars,
            queue_wait=(None if submitted_at is None else _round_seconds(clock() - submitted_at)),
            total_duration=_round_seconds(clock() - started_at),
            timeout_stage="queue",
            applied_timeout_threshold=(None if submitted_at is None else _round_seconds(clock() - submitted_at)),
            boundary_supplier="portfolio_deadline",
            scheduled_before_budget_exhaustion=False,
        )

    remaining = None if deadline is None else deadline - clock()
    deadline_threshold = (
        None if submitted_at is None or deadline is None else _round_seconds(max(0.0, deadline - submitted_at))
    )
    scheduled_before_deadline = submitted_at is None or deadline is None or submitted_at < deadline
    if remaining is not None and remaining <= 0 and phase_telemetry is None:
        return _observation(
            item,
            final_status="budget_exhausted",
            diagnostic_class="portfolio_budget_exhausted",
            capture=capture,
            prompt_chars=prompt_chars,
            queue_wait=(None if submitted_at is None else _round_seconds(max(0.0, clock() - submitted_at))),
            total_duration=_round_seconds(clock() - started_at),
            timeout_stage="queue",
            applied_timeout_threshold=deadline_threshold,
            boundary_supplier="portfolio_deadline",
            scheduled_before_budget_exhaustion=scheduled_before_deadline,
        )
    if timeout_seconds is None and remaining is None:
        raise ValueError("timeout_seconds or deadline is required")
    remaining_seconds = None if phase_telemetry is not None else (float(remaining) if remaining is not None else None)
    bounded_timeout = float(timeout_seconds) if timeout_seconds is not None else remaining_seconds
    if bounded_timeout is None:
        raise ValueError("timeout_seconds or deadline is required")
    if remaining_seconds is not None:
        bounded_timeout = min(bounded_timeout, remaining_seconds)
    if bounded_timeout <= 0:
        return _observation(
            item,
            final_status="budget_exhausted",
            diagnostic_class="portfolio_budget_exhausted",
            capture=capture,
            prompt_chars=prompt_chars,
            queue_wait=(None if submitted_at is None else _round_seconds(max(0.0, clock() - submitted_at))),
            total_duration=_round_seconds(clock() - started_at),
            timeout_stage="queue",
            applied_timeout_threshold=deadline_threshold,
            boundary_supplier="portfolio_deadline",
            scheduled_before_budget_exhaustion=scheduled_before_deadline,
        )

    extraction_agent = agent or __import__(
        "autoscout.codex_recovery", fromlist=["CodexCliExtractionAgent"]
    ).CodexCliExtractionAgent(timeout_seconds=bounded_timeout, clock=clock)
    extraction_call_started = clock()
    if phase_telemetry is not None:
        phase_telemetry.mark_subprocess_started(extraction_call_started)
    try:
        if phase_telemetry is not None:
            phase_telemetry.mark_agent_invocation_started(extraction_call_started)
        deadline_aware = getattr(extraction_agent, "extract_with_timeout", None)
        if callable(deadline_aware):
            attempt = deadline_aware(item, capture_path, staging_path, timeout_seconds=bounded_timeout)
        else:
            attempt = extraction_agent.extract(dict(item), capture_path, staging_path)
    except Exception as exc:
        if raise_agent_exceptions:
            raise
        if phase_telemetry is not None:
            phase_telemetry.mark_subprocess_finished(clock())
        phase_queue_wait = None if submitted_at is None else _round_seconds(extraction_call_started - submitted_at)
        return _observation(
            item,
            final_status="command_failed",
            diagnostic_class=f"worker_exception_{type(exc).__name__}",
            capture=capture,
            prompt_chars=prompt_chars,
            queue_wait=phase_queue_wait,
            subprocess_duration=_round_seconds(clock() - extraction_call_started),
            total_duration=_round_seconds(clock() - started_at),
            applied_timeout_ceiling=bounded_timeout,
            started=True,
        )

    if attempt.status is ExtractionStatus.UNAVAILABLE and attempt.subprocess_started_at is None:
        if phase_telemetry is not None:
            phase_telemetry.mark_subprocess_unavailable()
        subprocess_started = extraction_call_started
        subprocess_finished = extraction_call_started
    else:
        subprocess_started = (
            extraction_call_started if attempt.subprocess_started_at is None else attempt.subprocess_started_at
        )
        subprocess_finished = clock() if attempt.subprocess_finished_at is None else attempt.subprocess_finished_at
        if phase_telemetry is not None:
            phase_telemetry.mark_subprocess_started(subprocess_started)
            phase_telemetry.mark_subprocess_finished(subprocess_finished)
    subprocess_duration = attempt.subprocess_duration_seconds
    if subprocess_duration is None:
        subprocess_duration = _round_seconds(subprocess_finished - subprocess_started)
    else:
        subprocess_duration = _round_seconds(subprocess_duration)
    output_bytes = attempt.output_bytes
    if output_bytes is None:
        try:
            output_bytes = staging_path.stat().st_size
        except OSError:
            output_bytes = None

    if _is_cancelled(cancellation):
        phase_queue_wait = None if submitted_at is None else _round_seconds(subprocess_started - submitted_at)
        return _observation(
            item,
            final_status="timeout",
            diagnostic_class="worker_collection_timeout",
            capture=capture,
            prompt_chars=prompt_chars,
            output_bytes=output_bytes,
            queue_wait=phase_queue_wait,
            subprocess_duration=subprocess_duration,
            validation_duration=None,
            total_duration=_round_seconds(clock() - started_at),
            applied_timeout_ceiling=bounded_timeout,
            exit_code=attempt.exit_code,
            started=True,
            timeout_stage="collection",
            applied_timeout_threshold=bounded_timeout,
            boundary_supplier=boundary_supplier_hint
            or ("portfolio_deadline" if deadline is not None else "source_execution_ceiling"),
            extraction_attempt=attempt,
            subprocess_started_at=subprocess_started,
            subprocess_finished_at=subprocess_finished,
            progress_event_counts=attempt.diagnosis.progress_event_counts,
            no_progress_quiet_seconds=attempt.diagnosis.no_progress_quiet_seconds,
        )

    validation: ValidationObservation | None = None
    validation_duration: float | None = None
    final_status = {
        ExtractionStatus.TIMEOUT: "timeout",
        ExtractionStatus.UNAVAILABLE: "unavailable",
        ExtractionStatus.COMMAND_FAILED: "command_failed",
        ExtractionStatus.INVALID_OUTPUT: "invalid_output",
        ExtractionStatus.WORKER_EXCEPTION: "command_failed",
        ExtractionStatus.PRODUCED: "validated",
    }.get(attempt.status, "command_failed")
    diagnostic_class = _failure_class(attempt)
    raw_count = None
    validated_count = None
    if attempt.status is ExtractionStatus.PRODUCED:
        validation_started = clock()
        if phase_telemetry is not None:
            phase_telemetry.mark_validation_started(validation_started)
        validation = _validation_diagnostic(staging_path, item, capture_path)
        validation_finished = clock()
        if phase_telemetry is not None:
            phase_telemetry.mark_validation_finished(validation_finished)
        validation_duration = _round_seconds(validation_finished - validation_started)
        raw_count = validation.raw_extracted_count
        validated_count = validation.validated_count
        if validation.valid:
            final_status = "validated"
            diagnostic_class = "validated"
        else:
            final_status = "invalid_output"
            diagnostic_class = f"validation_{validation.diagnostic_class or 'failed'}"
        output_bytes = validation.output_bytes

    timeout_stage = None
    applied_threshold = None
    boundary_supplier = None
    if deadline is not None and clock() > deadline and attempt.status is ExtractionStatus.PRODUCED:
        final_status = "timeout"
        diagnostic_class = "validation_timeout"
        timeout_stage = "validation"
        applied_threshold = max(0.0, deadline - extraction_call_started)
        boundary_supplier = "portfolio_deadline"

    phase_queue_wait = (
        None
        if submitted_at is None
        or (attempt.status is ExtractionStatus.UNAVAILABLE and attempt.subprocess_started_at is None)
        else _round_seconds(subprocess_started - submitted_at)
    )
    if attempt.status is ExtractionStatus.TIMEOUT:
        timeout_stage = "subprocess"
        applied_threshold = bounded_timeout
        boundary_supplier = boundary_supplier_hint or "source_execution_ceiling"
    return _observation(
        item,
        final_status=final_status,
        diagnostic_class=diagnostic_class,
        capture=capture,
        prompt_chars=prompt_chars,
        output_bytes=output_bytes,
        raw_count=raw_count,
        validated_count=validated_count,
        queue_wait=phase_queue_wait,
        subprocess_duration=subprocess_duration if attempt.status is not ExtractionStatus.UNAVAILABLE else None,
        validation_duration=validation_duration,
        total_duration=_round_seconds(clock() - started_at),
        applied_timeout_ceiling=bounded_timeout,
        exit_code=attempt.exit_code,
        validation=validation,
        timeout_stage=timeout_stage,
        applied_timeout_threshold=applied_threshold,
        boundary_supplier=boundary_supplier,
        started=True,
        extraction_attempt=attempt,
        subprocess_started_at=subprocess_started,
        subprocess_finished_at=subprocess_finished,
        progress_event_counts=attempt.diagnosis.progress_event_counts,
        no_progress_quiet_seconds=attempt.diagnosis.no_progress_quiet_seconds,
    )
