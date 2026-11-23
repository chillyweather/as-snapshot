"""Production Codex-operated collection adapter for the weekly-workflow seam.

Implements bounded retries, validated resume markers, and existing
provenance/metadata-only validation through the collector registry.

The coding agent performs semantic extraction from captured configured-source
pages using its subscription. No Platform API credentials or separately billed
model transport are introduced by this module.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from contextlib import nullcontext
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from enum import Enum
from pathlib import Path
from typing import Any
from typing import Protocol

from autoscout.bookmarks_contract import BOOKMARKS_SOURCE_NAME
from autoscout.bookmarks_contract import BOOKMARKS_SOURCE_URL
from autoscout.bookmarks_contract import is_bookmarks_source_identity
from autoscout.bounded_workers import CancellationToken
from autoscout.bounded_workers import DaemonFuture
from autoscout.bounded_workers import DaemonThreadPool
from autoscout.codex_features import EXTRACTION_DISABLED_FEATURES
from autoscout.codex_model_pin import CodexModelPin
from autoscout.codex_model_pin import load_extraction_model_pin
from autoscout.codex_workflow import applied_capture_runtime_budget
from autoscout.codex_workflow import prepare_codex_extraction
from autoscout.extraction_completeness import capture_has_listing_rows
from autoscout.extraction_completeness import extractor_empty_reason
from autoscout.extraction_round import ExtractionRoundCompletion
from autoscout.extraction_round import ExtractionRoundContext
from autoscout.extraction_round import execute_bounded_extraction_round
from autoscout.helper_metadata import HELPER_METADATA_FIELDS
from autoscout.helper_metadata import validate_helper_metadata
from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.model_client import ClaudeApiClient
from autoscout.model_client import CodexCliClient
from autoscout.model_client import ModelCallResult
from autoscout.model_client import ModelCallStatus
from autoscout.model_client import ModelFailure
from autoscout.model_client import ModelRequest
from autoscout.model_client import PriorExchange
from autoscout.model_client import model_client_for
from autoscout.nuevoslibros_frontlist import NO_RECENT_CARD_REASON
from autoscout.progress_snapshot import ProgressSnapshotFailureRecorder
from autoscout.progress_snapshot import ProgressSnapshotPresentation
from autoscout.progress_snapshot import ProgressSnapshotWriter
from autoscout.ranking_author_policy import work_item_author_exempt
from autoscout.runtime_budget import CAPTURE_RUNTIME_BUDGET_SECONDS
from autoscout.runtime_budget import CAPTURE_SOURCE_RETRY_TIMEOUT_SECONDS
from autoscout.runtime_budget import CAPTURE_SOURCE_TIMEOUT_SECONDS
from autoscout.runtime_budget import CODEX_ABSOLUTE_CEILING_SECONDS
from autoscout.runtime_budget import CODEX_NO_PROGRESS_TIMEOUT_SECONDS
from autoscout.runtime_budget import PORTFOLIO_RUNTIME_BUDGET_SECONDS
from autoscout.runtime_budget import RETRY_HEADROOM_SECONDS
from autoscout.runtime_budget import SEGMENT_BUDGET_SECONDS
from autoscout.secret_redaction import redact_secrets
from autoscout.source_collectors import NON_ENGLISH_STRICT_CHART_SEARCH_TRANSLATION_RULE
from autoscout.source_collectors import contract_family
from autoscout.source_collectors import normalized_candidate_category
from autoscout.source_contribution import should_retry_empty_extraction
from autoscout.source_language import requires_english_helper_fields

# Capture deliberately runs once.  Portfolio sizing is derived from the
# current worklist by the weekly extraction coordinator.
MAX_CAPTURE_RETRIES = 1
MAX_EXTRACTION_RETRIES = 2
DEFAULT_EXTRACTION_WORKERS = 3
RETRY_BACKOFF_SECONDS = 0.25
TOTAL_SOURCE_PROCESSING_BUDGET_SECONDS = CAPTURE_RUNTIME_BUDGET_SECONDS + PORTFOLIO_RUNTIME_BUDGET_SECONDS

# Grace period for subprocess termination after SIGTERM before SIGKILL.
_SUBTERMINATE_GRACE_SECONDS = 5.0

# An extractor regularly answers with an empty entries array and no
# empty_reason after its own tool calls listed the rows (Issue #751). Asked in
# the same session, it returns the rows it had already parsed. The follow-up
# runs inside the attempt's own time bound, and only when at least this much
# of that bound is left.
EMPTY_ANSWER_FOLLOWUP_MIN_SECONDS = 60.0
EMPTY_ANSWER_FOLLOWUP_RECOVERED = "recovered"
EMPTY_ANSWER_FOLLOWUP_EXPLAINED = "explained"
EMPTY_ANSWER_FOLLOWUP_UNCHANGED = "unchanged"
EMPTY_ANSWER_FOLLOWUP_NOT_RUN = "not_run"
EMPTY_ANSWER_FOLLOWUP_PROMPT = (
    "Your answer has an empty entries array and no empty_reason. The work item allows an empty result only "
    "with a top-level empty_reason naming what capture.json lacks. Do not start over. If the rows you already "
    "found in capture.json include any that meet the required fields, return the complete JSON object with "
    "every such row in entries. If none does, return the same object with empty_reason. "
    "Return one top-level JSON object only."
)

RESUME_MARKER_FILE = "codex-workflow-resume.json"

# The answer cap of one extraction call on the Claude API, thinking included
# (Issue #1149).  A long list is about a hundred rows of some 150 tokens each;
# this leaves room for that and for the thinking before it, and at Haiku's
# speed the call still ends well inside the attempt's bound.
EXTRACTION_MAX_OUTPUT_TOKENS = 32_000
# Claude API failures a retry of the same source cannot cure.
_PERMANENT_API_FAILURES = frozenset(
    {ModelFailure.KEY_REJECTED, ModelFailure.MODEL_UNAVAILABLE, ModelFailure.USAGE_LIMIT, ModelFailure.BAD_REQUEST}
)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


@dataclass
class SourceExtractionItem:
    source_name: str
    source_url: str
    source_family: str
    capture_file: str
    expected_extraction_file: str
    source_metadata: dict[str, Any] = field(default_factory=dict)
    retries: int = 0
    status: str = "pending"


@dataclass
class CodexExtractionResult:
    work_items: list[SourceExtractionItem] = field(default_factory=list)
    captured_source_count: int = 0
    ready_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    source_runs: list[dict[str, Any]] = field(default_factory=list)
    resume_marker: dict[str, Any] = field(default_factory=dict)


def _read_resume_marker(output_dir: Path) -> dict[str, Any]:
    path = output_dir / RESUME_MARKER_FILE
    if path.exists():
        data = read_json(path, default={})
        return data if isinstance(data, dict) else {}
    return {}


def _write_resume_marker(output_dir: Path, marker: dict[str, Any]) -> None:
    write_json(output_dir / RESUME_MARKER_FILE, marker)


class ExtractionStatus(str, Enum):
    PRODUCED = "produced"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    COMMAND_FAILED = "command_failed"
    INVALID_OUTPUT = "invalid_output"
    WORKER_EXCEPTION = "worker_exception"


@dataclass(frozen=True)
class AttemptDiagnosis:
    """What one extraction attempt did, retained so a failure can be traced.

    Issue #449 could not be answered from the retained artifacts, because the
    event census existed only as a stage aggregate and no record said which
    Codex rollout belonged to which attempt.  These six values answer both
    questions and always travel together: the lease observes them, the
    attempt carries them, and the run artifact states them.

    ``codex_session_id`` and ``rollout_path`` identify the rollout.
    ``progress_event_counts`` is the recognized-event census and
    ``unrecognized_event_counts`` the census of what the lease ignored, which
    is what tells a stalled item from an item working unrecognizably.
    ``progress_tail`` is the bounded descriptor tail of the observed stream.
    """

    progress_event_counts: dict[str, int] = field(default_factory=dict)
    no_progress_quiet_seconds: float | None = None
    unrecognized_event_counts: dict[str, int] = field(default_factory=dict)
    progress_tail: tuple[dict[str, Any], ...] = ()
    codex_session_id: str = ""
    rollout_path: str = ""
    # Issue #751: what the same-session follow-up to an empty answer without
    # an empty_reason produced. Blank when the answer needed no follow-up.
    empty_answer_followup: str = ""

    def evidence(self, *, retain_tail: bool) -> dict[str, Any]:
        """Return this diagnosis as the fields an attempt diagnostic carries.

        The caller decides whether the tail is worth retaining, because that
        is a policy about which attempts need diagnosing rather than a fact
        about the attempt.
        """
        stated: dict[str, Any] = {
            "progress_event_counts": dict(self.progress_event_counts),
            "no_progress_quiet_seconds": self.no_progress_quiet_seconds,
            "unrecognized_event_counts": dict(self.unrecognized_event_counts),
            "codex_session_id": self.codex_session_id,
            "rollout_path": self.rollout_path,
        }
        if retain_tail and self.progress_tail:
            stated["progress_tail"] = [dict(record) for record in self.progress_tail]
        if self.empty_answer_followup:
            stated["empty_answer_followup"] = self.empty_answer_followup
        return stated


@dataclass(frozen=True)
class ExtractionAttempt:
    status: ExtractionStatus
    retryable: bool
    exit_code: int | None = None
    diagnostic: str = ""
    subprocess_duration_seconds: float | None = None
    output_bytes: int | None = None
    prompt_characters: int | None = None
    subprocess_started_at: float | None = None
    subprocess_finished_at: float | None = None
    diagnosis: AttemptDiagnosis = field(default_factory=AttemptDiagnosis)


class ExtractionAgent(Protocol):
    def extract(self, item: dict[str, Any], capture_path: Path, artifact_path: Path) -> ExtractionAttempt: ...


def attempt_diagnosis_evidence(attempt: ExtractionAttempt | None, status: str) -> dict[str, Any]:
    """Return the diagnosis fields for one attempt diagnostic (Issue #449).

    This is the retention policy, not the shape: the descriptor tail is kept
    only for an attempt that did not validate.  A healthy attempt has
    nothing to diagnose, and a tail on all 29 sources would grow the run log
    for no reading.  An attempt that never existed still states every field,
    so a reader never has to tell an absent key from an absent value.
    """
    diagnosis = attempt.diagnosis if attempt is not None else AttemptDiagnosis()
    return diagnosis.evidence(retain_tail=status != "validated")


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    """Terminate *process* and its descendants, with bounded fallback.

    The caller must have started *process* with ``start_new_session=True`` so
    that the process owns its own session / process group and signalling it
    cannot affect AutoScout's own process tree.

    On Unix: sends SIGTERM to the process group, waits up to
    ``_SUBTERMINATE_GRACE_SECONDS``, then escalates to SIGKILL.
    On Windows: uses ``terminate()`` / ``kill()`` on the single process.
    Never raises.
    """
    if sys.platform == "win32":
        try:
            process.terminate()
            process.wait(timeout=_SUBTERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=_SUBTERMINATE_GRACE_SECONDS)
            except Exception:
                pass
        except Exception:
            pass
        return

    # Unix: signal the process group owned by the child's new session.
    try:
        pgid = os.getpgid(process.pid)
    except (ProcessLookupError, OSError):
        pgid = None

    def _kill_pg() -> None:
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                pass
        try:
            process.kill()
        except Exception:
            pass

    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=_SUBTERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        _kill_pg()
        try:
            process.wait(timeout=_SUBTERMINATE_GRACE_SECONDS)
        except Exception:
            pass
    except Exception:
        _kill_pg()
        try:
            process.wait(timeout=_SUBTERMINATE_GRACE_SECONDS)
        except Exception:
            pass


def _exact_list_expected_metadata(item: dict[str, Any]) -> dict[str, str]:
    """Return configured values that every exact-list entry must preserve."""
    source_metadata = item.get("source_metadata")
    if not isinstance(source_metadata, dict) or source_metadata.get("metadata_exact_book_list") is not True:
        return {}
    return {
        "signal_type": str(
            source_metadata.get("signal_family") or source_metadata.get("family") or item.get("source_family") or ""
        ).strip(),
        "signal_strength": str(source_metadata.get("expected_signal_strength") or "").strip(),
        "candidate_category": str(source_metadata.get("eligible_candidate_category") or "").strip(),
    }


def _exact_list_helper_failure_status(entry: dict[str, Any], item: dict[str, Any]) -> str:
    """Return a recovery diagnostic for exact-list helper metadata."""
    source_metadata = item.get("source_metadata")
    if not isinstance(source_metadata, dict) or source_metadata.get("metadata_exact_book_list") is not True:
        return ""
    strict_chart = (
        source_metadata.get("audible_explicit_chart") is True or source_metadata.get("amazon_charts_weekly") is True
    )
    if strict_chart and not requires_english_helper_fields(source_metadata):
        return "invalid_helper_metadata" if any(field in entry for field in HELPER_METADATA_FIELDS) else ""
    title_relation = None if strict_chart else requires_english_helper_fields(source_metadata)
    if validate_helper_metadata(entry, title_relation=title_relation):
        return "invalid_helper_metadata"
    return ""


def _exact_list_metadata_failure_status(entry: dict[str, Any], item: dict[str, Any]) -> str:
    expected = _exact_list_expected_metadata(item)
    for field_name in ("signal_type", "signal_strength"):
        expected_value = expected.get(field_name, "")
        if expected_value and str(entry.get(field_name) or "").strip().casefold() != expected_value.casefold():
            return "invalid_signal_metadata"
    expected_category = expected.get("candidate_category", "")
    # Fold separators exactly as the collector's contract check does, or this
    # validator quarantines an artifact the collector would have accepted and
    # forces a needless re-extraction (Issue #375).
    if expected_category and normalized_candidate_category(
        entry.get("candidate_category") or ""
    ) != normalized_candidate_category(expected_category):
        return "invalid_candidate_category"
    return _exact_list_helper_failure_status(entry, item)


def _artifact_exact_list_metadata_failure_status(path: Path, item: dict[str, Any]) -> str:
    if not _exact_list_expected_metadata(item):
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    if not isinstance(data, dict):
        return ""
    entries = data.get("entries", data.get("mentions"))
    if not isinstance(entries, list):
        return ""
    for entry in entries:
        if isinstance(entry, dict):
            status = _exact_list_metadata_failure_status(entry, item)
            if status:
                return status
    return ""


def build_codex_extraction_prompt(item: dict[str, Any]) -> str:
    """Build the metadata-only prompt used by the subscription-backed agent."""
    exact_list_instructions = ""
    expected_metadata = _exact_list_expected_metadata(item)
    if expected_metadata:
        exact_list_instructions = (
            "\nFor every exact-book-list entry, preserve these separate configured values. "
            f"Set signal_type exactly to {json.dumps(expected_metadata['signal_type'], ensure_ascii=False)}. "
            "Set signal_strength exactly to "
            f"{json.dumps(expected_metadata['signal_strength'], ensure_ascii=False)}. "
            "Set candidate_category exactly to "
            f"{json.dumps(expected_metadata['candidate_category'], ensure_ascii=False)}. "
            "Do not combine signal_type and signal_strength into one label."
        )
        entry_section_heading = str((item.get("source_metadata") or {}).get("entry_section_heading") or "").strip()
        if entry_section_heading:
            exact_list_instructions += (
                "\nExtract only the linked entries inside the page section headed "
                f"{json.dumps(entry_section_heading, ensure_ascii=False)}. "
                "Books linked in any other section of the page are not entries of this source."
            )
    source_metadata = item.get("source_metadata")
    if isinstance(source_metadata, dict):
        strict_chart = (
            source_metadata.get("audible_explicit_chart") is True or source_metadata.get("amazon_charts_weekly") is True
        )
        # A non-English ranking table carries the same helper fields under the
        # same contract, so it needs the same rule (Issue #562 round 13).
        ranking_table = contract_family(source_metadata) == "ranking-table"
        # An absent language means a misconfigured source, matching the
        # predicate the extraction contract applies. A ranking table can
        # no longer reach this undeclared: validate_config requires a nonblank
        # language on every configured source.
        if (strict_chart or ranking_table) and requires_english_helper_fields(source_metadata):
            exact_list_instructions += (
                f"\n{NON_ENGLISH_STRICT_CHART_SEARCH_TRANSLATION_RULE} "
                "It is a Russian search helper only, not a replacement title. "
                "Never duplicate the authoritative title into search_translation. "
                "Omit the field you cannot render instead of repeating the title."
            )
    frontlist_instructions = ""
    if isinstance(source_metadata, dict) and contract_family(source_metadata) == "nuevoslibros-frontlist":
        frontlist_instructions = NUEVOSLIBROS_FRONTLIST_PROMPT
    bookmarks_instructions = ""
    if item.get("source_name") == BOOKMARKS_SOURCE_NAME and item.get("source_url") == BOOKMARKS_SOURCE_URL:
        bookmarks_instructions = (
            "\nBookMarks (LitHub) – New Nonfiction has a source-specific binding contract. "
            "Use the BookMarks latest_book listing rows as the only extraction units. "
            "For each row, take title and author only from the metadata shown in that same listing row, "
            "specifically latest_book_title and latest_book_author. "
            "Do not expand, complete, translate, or invent missing title or author data. "
            "For every returned entry, source_url and evidence_url must both equal exactly "
            '"https://bookmarks.reviews/category/non-fiction/". '
            'Use the already bound constants: set signal_type exactly to "media/curated list", '
            'signal_strength exactly to "major media list", and candidate_category exactly to "adult nonfiction". '
            "Make evidence_note a short metadata-only evidence note from the shown listing metadata, not copied prose. "
            "Omit a row only when its title or author is truly absent. "
            "If at least one complete listing row exists, return it and never return an empty entries array. "
            "Do not invent or salvage a row when title or author data is missing. "
            "Every required field must be non-empty. If title or author is truly absent, omit the deficient row "
            "rather than returning a partial row; do not omit a row with both values present. "
            "Return one top-level JSON object only. Do not return Markdown or prose. "
            "Before returning, perform a final field, identity, and provenance checklist. "
            "Use only BookMarks listing metadata from the fixed capture. Do not infer or invent data."
        )
    return (
        "Read capture.json and return only metadata-only JSON. Include top-level source_name, source_url, "
        "operation_id, capture_sha256, and entries. Copy identity values exactly from this work item:\n"
        + json.dumps(item, ensure_ascii=False)
        + "\nThe work item's source_metadata fields are binding extraction constraints. "
        "Every entry requires title, primary_authors, source_url, evidence_url, evidence_note, "
        "candidate_category, signal_type, and signal_strength. Both URLs must occur in the capture. "
        "Do not include copied reviews, blurbs, article bodies, excerpts, or samples."
        + exact_list_instructions
        + bookmarks_instructions
        + frontlist_instructions
        + "\nBefore answering, check your answer against the rows you found in capture.json: every row that "
        "meets the required fields must be in entries, and a row is left out only for a rule it breaks. "
        "If you return no entries, add a top-level empty_reason naming what the capture lacks (Issue #740)."
        + "\nThe retained capture.json is the complete and only input for this task. "
        "Do not read a memory folder, a prior-run note, or any other workspace history."
    )


def stamp_host_operation_id(payload: dict[str, Any], item: Mapping[str, Any]) -> None:
    """Set the answer's operation_id to the one this host process dispatched.

    The extractor was asked to retype a 64-character hex operation ID, and it
    dropped or inserted characters often enough to discard whole lists: every
    rejected National Book Awards and Carnegie artifact of the 2026-10-15 run
    differed from its work item in that field alone (Issue #823). The host
    writes this artifact from this very response, so the operation is known
    without the extractor's copy. The other envelope fields stay copied and
    checked.
    """
    operation_id = item.get("operation_id")
    if isinstance(operation_id, str) and operation_id:
        payload["operation_id"] = operation_id


def _answer_entries(payload: Mapping[str, Any]) -> Any:
    return payload["entries"] if "entries" in payload else payload.get("mentions")


def is_unreasoned_empty_answer(payload: Any) -> bool:
    """Say whether an answer holds no entries and states no reason for it.

    The contract requires an ``empty_reason`` beside an empty result. An
    answer that has neither rows nor a reason is an unfinished answer, not a
    statement about the page (Issue #751).
    """
    if not isinstance(payload, Mapping):
        return False
    entries = _answer_entries(payload)
    return isinstance(entries, list) and not entries and not extractor_empty_reason(payload)


class CodexProgressTransport(Protocol):
    """Narrow host transport used to expose structured execution activity."""

    def drain(self, lease: CodexProgressLease, now: float) -> None: ...


CodexProgressTransportFactory = Callable[[], CodexProgressTransport]


class CodexProgressLease:
    """Classify only recognized structured Codex execution activity."""

    # These are lifecycle/activity events, not content-bearing messages or
    # tool results.  The lease deliberately ignores prose, stdout, stderr,
    # source HTML, and custom tool output.
    RECOGNIZED_EVENT_TYPES = frozenset(
        {
            "reasoning",
            "custom_tool_call",
            "token_count",
            "task_started",
            "task_complete",
        }
    )

    _MAX_PARTIAL_LINE_BYTES = 64 * 1024
    # Diagnosis bounds (Issue #449). The census tells a stalled item apart
    # from an item working in a way the lease does not recognize, so it must
    # exist; it must also stay bounded, because the event stream is not.
    _MAX_CENSUS_KEYS = 64
    _TAIL_RECORD_LIMIT = 24
    _TOOL_INPUT_EXCERPT_CHARS = 200
    _MAX_EVENT_KIND_CHARS = 120
    _MAX_TOOL_NAME_CHARS = 64
    _MAX_TIMESTAMP_CHARS = 40
    # Every kind past the census limit is counted here, so a runaway event
    # stream loses the names of the rare kinds and not the total.
    _CENSUS_OVERFLOW_KEY = "overflow"
    # The kinds a line can have when it is not an event at all.
    _UNPARSED_KIND = "unparsed"
    _NON_OBJECT_KIND = "not_an_object"

    def __init__(self) -> None:
        self.last_progress_at: float | None = None
        self.event_counts: dict[str, int] = {}
        self.unrecognized_event_counts: dict[str, int] = {}
        self._tail: deque[dict[str, Any]] = deque(maxlen=self._TAIL_RECORD_LIMIT)
        self._partial_line = b""
        self._lock = threading.Lock()

    def feed(self, raw_bytes: bytes, now: float) -> None:
        """Classify JSONL events, including events split across reads.

        Only the incomplete final line is retained, and it is bounded. Lines
        that have been classified are discarded immediately.
        """
        if not raw_bytes:
            return
        with self._lock:
            data = self._partial_line + raw_bytes
            lines = data.split(b"\n")
            self._partial_line = lines.pop() or b""
            if len(self._partial_line) > self._MAX_PARTIAL_LINE_BYTES:
                self._partial_line = b""
            for line in lines:
                self._classify_line(line, now)

    def _classify_line(self, line: bytes, now: float) -> None:
        if not line.strip():
            return
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._count_ignored_event(self._UNPARSED_KIND, line, None)
            return
        if not isinstance(value, dict):
            self._count_ignored_event(self._NON_OBJECT_KIND, line, None)
            return
        event_type = value.get("type")
        payload = value.get("payload") if isinstance(value.get("payload"), dict) else None
        payload_type = payload.get("type") if payload is not None else None
        kind = f"{event_type}/{payload_type}" if isinstance(payload_type, str) else str(event_type)
        if isinstance(event_type, str) and event_type in self.RECOGNIZED_EVENT_TYPES:
            recognized = event_type
        elif event_type in {"response_item", "event_msg"}:
            recognized = payload_type if isinstance(payload_type, str) else ""
            if recognized not in self.RECOGNIZED_EVENT_TYPES:
                self._count_ignored_event(kind, line, value)
                return
        else:
            self._count_ignored_event(kind, line, value)
            return
        self._retain_descriptor(kind, line, value, recognized=True)
        self.last_progress_at = now
        self.event_counts[recognized] = self.event_counts.get(recognized, 0) + 1

    def _count_ignored_event(self, kind: str, line: bytes, value: dict[str, Any] | None) -> None:
        """Count one event the lease ignored, and retain its descriptor.

        Counting an ignored event does not renew the lease.  A recognized
        event is the only progress, and that rule is unchanged.  The census
        exists so the claim "the item was working, the lease could not see
        it" can be checked against data.
        """
        key = kind[: self._MAX_EVENT_KIND_CHARS]
        if key not in self.unrecognized_event_counts and len(self.unrecognized_event_counts) >= self._MAX_CENSUS_KEYS:
            key = self._CENSUS_OVERFLOW_KEY
        self.unrecognized_event_counts[key] = self.unrecognized_event_counts.get(key, 0) + 1
        self._retain_descriptor(kind, line, value, recognized=False)

    def _retain_descriptor(self, kind: str, line: bytes, value: dict[str, Any] | None, *, recognized: bool) -> None:
        """Retain one bounded, content-free descriptor of an observed event.

        A descriptor says what kind of event arrived, when, and how large it
        was.  ``kind`` is the event type, qualified by its payload type when
        it has one, as in ``response_item/reasoning``.

        Prose, reasoning summaries, and tool output are page-derived or
        model-derived content and are never retained; only their size is.
        Model-authored tool input is retained, bounded and redacted, because
        what the item was doing before it went quiet is the finding.
        """
        record: dict[str, Any] = {
            "kind": kind[: self._MAX_EVENT_KIND_CHARS],
            "recognized": recognized,
            "bytes": len(line),
        }
        if isinstance(value, dict):
            timestamp = value.get("timestamp")
            if isinstance(timestamp, str):
                record["at"] = timestamp[: self._MAX_TIMESTAMP_CHARS]
            payload = value.get("payload") if isinstance(value.get("payload"), dict) else None
            body = payload if payload is not None else value
            if body.get("type") == "custom_tool_call":
                name = body.get("name")
                if isinstance(name, str):
                    record["tool_name"] = name[: self._MAX_TOOL_NAME_CHARS]
                tool_input = body.get("input")
                if isinstance(tool_input, str):
                    record["tool_input_excerpt"] = redact_secrets(tool_input)[: self._TOOL_INPUT_EXCERPT_CHARS]
        self._tail.append(record)

    def begin(self, now: float) -> None:
        """Start the fail-safe lease after transport startup is observed."""
        with self._lock:
            if self.last_progress_at is None:
                self.last_progress_at = now

    def idle_seconds(self, now: float) -> float | None:
        with self._lock:
            if self.last_progress_at is None:
                return None
            return max(0.0, now - self.last_progress_at)

    def snapshot_counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self.event_counts)

    def snapshot_unrecognized_counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self.unrecognized_event_counts)

    def snapshot_tail(self) -> tuple[dict[str, Any], ...]:
        """Return the bounded descriptor tail, oldest retained record first."""
        with self._lock:
            return tuple(dict(record) for record in self._tail)


class _RolloutTail:
    """Fail-safe incremental tail of one discovered Codex rollout file."""

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._path: Path | None = None
        self._handle: Any | None = None
        self._offset = 0

    @property
    def path(self) -> Path | None:
        """The resolved rollout file, once discovery has found it."""
        return self._path

    def _discover(self) -> None:
        if self._path is not None:
            return
        home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
        sessions = home / "sessions"
        newest: tuple[float, Path] | None = None
        visited = 0
        try:
            for root, _dirs, files in os.walk(sessions):
                for name in files:
                    visited += 1
                    if visited > 10000:
                        return
                    if not name.endswith(f"-{self._session_id}.jsonl"):
                        continue
                    path = Path(root) / name
                    try:
                        mtime = path.stat().st_mtime
                    except OSError:
                        continue
                    if newest is None or mtime > newest[0]:
                        newest = (mtime, path)
        except OSError:
            return
        if newest is None:
            return
        try:
            self._handle = newest[1].open("rb")
            self._path = newest[1]
        except OSError:
            self._handle = None

    def drain(self, lease: CodexProgressLease, now: float) -> None:
        self._discover()
        if self._handle is None:
            return
        try:
            self._handle.seek(self._offset)
            data = self._handle.read()
            self._offset = self._handle.tell()
        except OSError:
            return
        lease.feed(data, now)

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None


# Issue #993: one NuevosLibros Codex call holds at most this many dated cards.
NUEVOSLIBROS_CODEX_BATCH_CARDS = 30
NUEVOSLIBROS_EMPTY_BATCH_DIAGNOSTIC = "unexpected_empty_extraction"
NUEVOSLIBROS_FRONTLIST_PROMPT = (
    "\nNuevosLibros frontlist contract: capture.json holds one batch of the dated book cards of the declared genre "
    "page, and code has already validated each card's title link, visible author links and release date. A card's "
    "title, authors, book link and date are the complete grounding an entry needs: the card's title link is "
    "evidence_url, the configured page is source_url, the card's date gives publication_year, and the declared "
    "nonfiction genre page establishes candidate_category. english_title, search_translation and "
    "author_transliteration_note are your own renderings of the shown title and author, not page facts. Omit "
    "content_description and publisher; a card does not show them. Leave a card out only when its shown title or "
    "author establishes an extraction_notes exclusion. A capture that holds cards is never empty."
)


@dataclass
class _AttemptSpan:
    """What one extraction attempt spent across its Codex calls (#993).

    A single-call attempt fills this exactly as the one call did. A
    NuevosLibros attempt sums its batches: the span runs from the first
    call's start to the last call's finish, and the census is the total.
    """

    started: float | None = None
    finished: float | None = None
    prompt_characters: int = 0
    returncode: int | None = None
    progress_event_counts: dict[str, int] = field(default_factory=dict)
    empty_answer_followup: str = ""


def _nuevoslibros_batch_captures(capture_bytes: bytes, source: dict[str, Any]) -> list[bytes]:
    """Split a NuevosLibros capture into one retained capture per Codex call (#993).

    Each batch keeps the capture's own fields and holds at most
    NUEVOSLIBROS_CODEX_BATCH_CARDS of the cards dated in the recent window.
    An empty list means no card is in the window. Raises ValueError or
    CollectorRunError when the capture cannot be read or exceeds the input budget.
    """
    from autoscout.nuevoslibros_frontlist import capture_reference_date
    from autoscout.nuevoslibros_frontlist import recent_cards
    from autoscout.nuevoslibros_frontlist import replay_html
    from autoscout.source_collectors.agent_model_input import model_bounded_page_data

    page = json.loads(capture_bytes)
    if not isinstance(page, dict):
        raise ValueError("NuevosLibros capture is not a JSON object")
    limit = int(source.get("max_model_input_chars") or 120_000)
    # Refuses an over-budget capture exactly as the model input does (#901).
    model_bounded_page_data(page, limit, source=source)
    markup = str(page.get("html", ""))
    url = str(source.get("url", ""))
    cards = recent_cards(markup, url, capture_reference_date(page))
    size = NUEVOSLIBROS_CODEX_BATCH_CARDS
    return [
        json.dumps(
            {**page, "html": replay_html(markup, url, [card.url for card in cards[start : start + size]]), "text": ""},
            ensure_ascii=False,
        ).encode("utf-8")
        for start in range(0, len(cards), size)
    ]


ENVELOPE_FIELDS = ("source_name", "source_url", "operation_id", "capture_sha256")


def _merge_batch_answers(answers: list[dict[str, Any]], item: Mapping[str, Any]) -> dict[str, Any]:
    """Merge the answers of one NuevosLibros attempt, keeping the first of each evidence URL (#993).

    The envelope is the work item's, so no batch's copy of it can disagree.
    """
    merged = dict(answers[0])
    merged.pop("empty_reason", None)
    merged.update({field_name: str(item.get(field_name, "")) for field_name in ENVELOPE_FIELDS})
    key = "entries" if "entries" in merged else "mentions"
    seen: set[str] = set()
    entries: list[Any] = []
    for answer in answers:
        for entry in _answer_entries(answer):
            evidence_url = entry.get("evidence_url") if isinstance(entry, dict) else None
            if isinstance(evidence_url, str):
                if evidence_url in seen:
                    continue
                seen.add(evidence_url)
            entries.append(entry)
    merged[key] = entries
    return merged


class CodexCliExtractionAgent:
    """Obtain structured output from Codex in an isolated, read-only sandbox.

    Captured web content never receives repository write access. Codex writes
    its final response to a temporary directory; Python validates the JSON and
    is the only process that writes the declared artifact.

    Uses ``subprocess.Popen`` with ``start_new_session=True`` (process-group
    isolation), ``stdin=DEVNULL`` (the prompt is the final CLI argument), and
    temporary-file-backed stderr to avoid PIPE deadlock from descendants.
    Timeout and cleanup are independently bounded: a monotonic poll loop is
    followed by ``_terminate_process_tree`` with a hard grace period, and no
    ``communicate`` re-entry drains pipes.
    """

    def __init__(
        self,
        timeout_seconds: float = CODEX_ABSOLUTE_CEILING_SECONDS,
        no_progress_timeout_seconds: float = CODEX_NO_PROGRESS_TIMEOUT_SECONDS,
        clock: Any = time.monotonic,
        sleeper: Any = time.sleep,
        progress_transport_factory: CodexProgressTransportFactory | None = None,
        model_pin: CodexModelPin | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._no_progress_timeout_seconds = no_progress_timeout_seconds
        # Extraction runs on the repository's pinned model, never on the
        # operator's global Codex default (Issue #731). A missing pin fails
        # here, before any capture work is spent.
        self.model_pin = model_pin if model_pin is not None else load_extraction_model_pin()
        self._clock = clock
        self._sleeper = sleeper
        self._progress_transport_factory = progress_transport_factory
        self._telemetry = threading.local()
        self._profile_lock = threading.Lock()
        self.effective_model_profile: dict[str, str] | None = None

    def _record_effective_profile(self, key: str, value: str) -> None:
        # Stderr is an untrusted vendor diagnostic channel. Redact before the
        # value enters the shared developer profile, then keep the fact short.
        safe_value = redact_secrets(str(value).strip())[:128]
        with self._profile_lock:
            profile = dict(self.effective_model_profile or {})
            profile[key] = safe_value
            self.effective_model_profile = profile

    def effective_model_profile_snapshot(self) -> dict[str, str] | None:
        with self._profile_lock:
            return dict(self.effective_model_profile) if self.effective_model_profile is not None else None

    @contextmanager
    def progress_observer(self, observer: Callable[[float | None, dict[str, int], float], None]):
        """Install a per-thread observer for read-only lease snapshots."""
        sentinel = object()
        previous = getattr(self._telemetry, "progress_observer", sentinel)
        self._telemetry.progress_observer = observer
        try:
            yield
        finally:
            if previous is sentinel:
                try:
                    del self._telemetry.progress_observer
                except AttributeError:
                    pass
            else:
                self._telemetry.progress_observer = previous

    def _observe_progress(self, lease: CodexProgressLease, now: float) -> None:
        observer = getattr(self._telemetry, "progress_observer", None)
        if not callable(observer):
            return
        try:
            observer(lease.idle_seconds(now), lease.snapshot_counts(), now)
        except Exception:
            # Observation is advisory and must never affect extraction.
            pass

    def _attempt_diagnosis(self) -> AttemptDiagnosis:
        """Return the diagnosis this thread's telemetry observed (Issue #449).

        One accessor keeps every ``ExtractionAttempt`` construction site
        carrying the same evidence, so a failed attempt and the healthy
        attempt beside it can be compared field by field.
        """
        return AttemptDiagnosis(
            progress_event_counts=dict(getattr(self._telemetry, "progress_event_counts", {})),
            no_progress_quiet_seconds=getattr(self._telemetry, "no_progress_quiet_seconds", None),
            unrecognized_event_counts=dict(getattr(self._telemetry, "unrecognized_event_counts", {})),
            progress_tail=tuple(getattr(self._telemetry, "progress_tail", ())),
            codex_session_id=str(getattr(self._telemetry, "codex_session_id", "")),
            rollout_path=str(getattr(self._telemetry, "rollout_path", "")),
            empty_answer_followup=str(getattr(self._telemetry, "empty_answer_followup", "")),
        )

    def _follow_up_empty_answer(
        self, payload: dict[str, Any], isolated: Path, response_path: Path, remaining_seconds: float
    ) -> tuple[dict[str, Any], bytes | None]:
        """Ask the same session once to finish an empty answer it gave no reason for.

        Returns the answer to keep and its bytes, or the original *payload*
        and ``None`` when the follow-up did not run or did not improve on it,
        so a failed follow-up leaves the attempt exactly as it was.
        """
        session_id = str(getattr(self._telemetry, "codex_session_id", ""))
        rollout_path = str(getattr(self._telemetry, "rollout_path", ""))
        first_started = getattr(self._telemetry, "subprocess_started_at", None)
        first_counts = dict(getattr(self._telemetry, "progress_event_counts", {}))
        client = model_client_for(self.model_pin)
        # The Claude API keeps no session: its follow-up resends the first
        # request and the answer it got (Issue #1149).
        first_request = getattr(self._telemetry, "api_request", None)
        resumable = bool(session_id) if isinstance(client, CodexCliClient) else first_request is not None
        if not resumable or remaining_seconds < EMPTY_ANSWER_FOLLOWUP_MIN_SECONDS:
            self._telemetry.empty_answer_followup = EMPTY_ANSWER_FOLLOWUP_NOT_RUN
            return payload, None
        followup_path = isolated / "followup-response.json"
        request = ModelRequest(
            prompt=EMPTY_ANSWER_FOLLOWUP_PROMPT,
            response_path=followup_path,
            model=self.model_pin.model,
            reasoning_effort=self.model_pin.reasoning_effort,
            disabled_features=EXTRACTION_DISABLED_FEATURES,
            resume_session_id=session_id,
            max_output_tokens=EXTRACTION_MAX_OUTPUT_TOKENS,
            follows=None if first_request is None else PriorExchange(first_request, _read_text(response_path)),
            purpose="extraction_followup",
        )
        answer: dict[str, Any] | None = None
        answer_bytes: bytes | None = None
        try:
            if isinstance(client, CodexCliClient):
                returncode, _stderr = self._run_codex(
                    client.command(request), isolated, followup_path, remaining_seconds
                )
            else:
                answered = self._call_api(client, request, remaining_seconds).status is ModelCallStatus.ANSWERED
                returncode = 0 if answered else 1
            if returncode == 0:
                answer_bytes = followup_path.read_bytes()
                parsed = json.loads(answer_bytes.decode("utf-8"))
                answer = parsed if isinstance(parsed, dict) else None
        except (subprocess.TimeoutExpired, OSError, UnicodeDecodeError, json.JSONDecodeError):
            answer = None
        finally:
            # The attempt is one span from the first start to the last finish,
            # and it stays bound to the session that produced the answer.
            self._telemetry.subprocess_started_at = first_started
            self._telemetry.boundary_reason = None
            self._telemetry.codex_session_id = session_id
            self._telemetry.rollout_path = rollout_path
            # A resumed session appends to the rollout it already had, and
            # the follow-up tails that file from its start, so its census
            # already holds the first invocation's events. Adding the two
            # would count those twice; the larger count per kind is the
            # whole attempt, and the first census stands when the follow-up
            # never read the rollout.
            combined = dict(first_counts)
            for kind, count in dict(getattr(self._telemetry, "progress_event_counts", {})).items():
                combined[kind] = max(combined.get(kind, 0), count)
            self._telemetry.progress_event_counts = combined
        # The follow-up replaces the answer only when it is a better one: an
        # entries list holding rows, or an empty list beside a stated reason.
        # An object without a readable entries list would be refused as
        # missing_entries and turn a valid empty artifact into a failed one.
        entries = _answer_entries(answer) if answer is not None else None
        if answer is None or answer_bytes is None or not isinstance(entries, list):
            outcome = EMPTY_ANSWER_FOLLOWUP_UNCHANGED
        elif entries:
            outcome = EMPTY_ANSWER_FOLLOWUP_RECOVERED
        elif extractor_empty_reason(answer):
            outcome = EMPTY_ANSWER_FOLLOWUP_EXPLAINED
        else:
            outcome = EMPTY_ANSWER_FOLLOWUP_UNCHANGED
        self._telemetry.empty_answer_followup = outcome
        if answer is None or answer_bytes is None or outcome == EMPTY_ANSWER_FOLLOWUP_UNCHANGED:
            return payload, None
        return answer, answer_bytes

    def extract(self, item: dict[str, Any], capture_path: Path, artifact_path: Path) -> ExtractionAttempt:
        return self.extract_with_timeout(item, capture_path, artifact_path, timeout_seconds=self._timeout_seconds)

    def _run_codex(
        self, command: list[str], isolated: Path, response_path: Path, bounded_timeout: float
    ) -> tuple[int, str]:
        """Run Codex while polling absolute and structured-progress bounds."""
        stderr_fd, stderr_path = tempfile.mkstemp(prefix="autoscout-codex-stderr-")
        stdout_fd, stdout_path = tempfile.mkstemp(prefix="autoscout-codex-stdout-")
        stderr_pending = b""
        lease = CodexProgressLease()
        rollout_tail: _RolloutTail | None = None
        progress_transport = (
            self._progress_transport_factory() if self._progress_transport_factory is not None else None
        )
        session_id: str | None = None
        started = self._clock()
        self._telemetry.subprocess_started_at = started
        self._telemetry.subprocess_finished_at = None
        self._telemetry.boundary_reason = None
        self._telemetry.progress_event_counts = {}
        self._telemetry.no_progress_quiet_seconds = None
        self._telemetry.unrecognized_event_counts = {}
        self._telemetry.progress_tail = ()
        self._telemetry.codex_session_id = ""
        self._telemetry.rollout_path = ""

        def _record_diagnosis(lease_now: CodexProgressLease) -> None:
            """Retain what an attempt did, so a failure can be traced later.

            Without the session id, an attempt cannot be matched to its
            rollout file, and a no-progress timeout can only be guessed at
            (Issue #449).  This runs on every attempt, because a healthy
            attempt beside a failed one is the comparison that resolves it.
            """
            self._telemetry.progress_event_counts = lease_now.snapshot_counts()
            self._telemetry.unrecognized_event_counts = lease_now.snapshot_unrecognized_counts()
            self._telemetry.progress_tail = lease_now.snapshot_tail()
            self._telemetry.codex_session_id = session_id or ""
            rollout_file = rollout_tail.path if rollout_tail is not None else None
            self._telemetry.rollout_path = str(rollout_file) if rollout_file is not None else ""

        def _consume_stderr(data: bytes, now: float) -> None:
            nonlocal stderr_pending, session_id, rollout_tail
            if not data:
                return
            # stderr is diagnostic text only.  It must never renew the
            # progress lease, even when it contains JSON-looking text.
            lines = (stderr_pending + data).split(b"\n")
            stderr_pending = lines.pop() or b""
            for line in lines:
                try:
                    text = line.decode("utf-8", errors="replace")
                except Exception:
                    continue
                session_match = re.search(r"session id:\s*([0-9a-fA-F-]+)", text)
                if session_match and progress_transport is None and rollout_tail is None:
                    session_id = session_match.group(1)
                    rollout_tail = _RolloutTail(session_id)
                model_match = re.search(r"^model:\s*(.+?)\s*$", text)
                if model_match:
                    self._record_effective_profile("model", model_match.group(1))
                effort_match = re.search(r"^reasoning effort:\s*(.+?)\s*$", text)
                if effort_match:
                    self._record_effective_profile("reasoning_effort", effort_match.group(1))

        def _drain(stderr_reader: Any, now: float) -> None:
            try:
                data = stderr_reader.read()
            except (OSError, ValueError):
                data = b""
            _consume_stderr(data, now)
            if progress_transport is not None:
                progress_transport.drain(lease, now)
            elif rollout_tail is not None:
                rollout_tail.drain(lease, now)

        try:
            process: subprocess.Popen[str] = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout_fd,
                stderr=stderr_fd,
                text=True,
                cwd=str(isolated),
                start_new_session=True,
            )
            # Transport startup is the baseline, not progress. Only the
            # structured transport may renew the lease after this point.
            lease.begin(self._clock())
            with open(stderr_path, "rb") as stderr_reader:
                while True:
                    now = self._clock()
                    _drain(stderr_reader, now)
                    self._observe_progress(lease, now)
                    returncode = process.poll()
                    if returncode is not None:
                        # The child may have written its final event just before
                        # exit, so drain both transports once more.
                        now = self._clock()
                        _drain(stderr_reader, now)
                        self._observe_progress(lease, now)
                        break
                    idle = lease.idle_seconds(now)
                    reason: str | None = None
                    if now - started >= bounded_timeout:
                        reason = "absolute_execution_ceiling"
                    elif (idle is not None and idle >= self._no_progress_timeout_seconds) or (
                        idle is None and now - started >= self._no_progress_timeout_seconds
                    ):
                        reason = "no_progress_timeout"
                    if reason is not None:
                        self._telemetry.boundary_reason = reason
                        _record_diagnosis(lease)
                        self._telemetry.no_progress_quiet_seconds = idle
                        _terminate_process_tree(process)
                        self._telemetry.subprocess_finished_at = self._clock()
                        raise subprocess.TimeoutExpired(command, bounded_timeout)
                    self._sleeper(0.005)

            _record_diagnosis(lease)
            self._telemetry.no_progress_quiet_seconds = lease.idle_seconds(self._clock())
            self._telemetry.subprocess_finished_at = self._clock()
            # Read diagnostics only after process exit. The caller redacts and
            # truncates them before they can enter a result or log.
            try:
                stderr_diagnostic = Path(stderr_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                stderr_diagnostic = ""
            return (returncode or 0, stderr_diagnostic)
        except OSError:
            raise
        except Exception:
            if getattr(self._telemetry, "subprocess_finished_at", None) is None:
                _terminate_process_tree(process)
                self._telemetry.subprocess_finished_at = self._clock()
            raise
        finally:
            if progress_transport is not None:
                close_transport = getattr(progress_transport, "close", None)
                if callable(close_transport):
                    try:
                        close_transport()
                    except Exception:
                        pass
            elif rollout_tail is not None:
                rollout_tail.close()
            for fd in (stdout_fd, stderr_fd):
                try:
                    os.close(fd)
                except OSError:
                    pass
            for path in (stdout_path, stderr_path):
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def extract_with_timeout(
        self,
        item: dict[str, Any],
        capture_path: Path,
        artifact_path: Path,
        *,
        timeout_seconds: float,
    ) -> ExtractionAttempt:
        bounded_timeout = min(float(self._timeout_seconds), timeout_seconds)
        if bounded_timeout <= 0:
            return ExtractionAttempt(ExtractionStatus.TIMEOUT, False, diagnostic="portfolio_budget_exhausted")
        with tempfile.TemporaryDirectory(prefix="autoscout-codex-") as temporary:
            isolated = Path(temporary)
            capture_bytes = capture_path.read_bytes()
            source_metadata = item.get("source_metadata")
            if isinstance(source_metadata, dict) and contract_family(source_metadata) == "publisher-frontlist":
                from autoscout.source_collectors.agent_model_input import model_bounded_page_data
                from autoscout.source_collectors.base import CollectorRunError

                try:
                    source = {**source_metadata, "url": item.get("source_url") or source_metadata.get("url")}
                    limit = int(source.get("max_model_input_chars") or 120_000)
                    page, _ = model_bounded_page_data(json.loads(capture_bytes), limit, source=source)
                    capture_bytes = json.dumps(page, ensure_ascii=False).encode("utf-8")
                except (ValueError, TypeError, CollectorRunError):
                    return ExtractionAttempt(
                        ExtractionStatus.UNAVAILABLE, False, diagnostic="frontlist_model_input_invalid_or_over_budget"
                    )
            nuevoslibros = isinstance(source_metadata, dict) and (
                contract_family(source_metadata) == "nuevoslibros-frontlist"
            )
            if isinstance(source_metadata, dict) and nuevoslibros:
                from autoscout.source_collectors.base import CollectorRunError

                try:
                    source = {**source_metadata, "url": item.get("source_url") or source_metadata.get("url")}
                    captures = _nuevoslibros_batch_captures(capture_bytes, source)
                except (ValueError, TypeError, CollectorRunError):
                    return ExtractionAttempt(
                        ExtractionStatus.UNAVAILABLE, False, diagnostic="frontlist_model_input_invalid_or_over_budget"
                    )
                if not captures:
                    # A capture with no recent card makes no model call; the source is reported
                    # by name as contributing nothing (#993).
                    return self._write_no_recent_card_artifact(item, artifact_path)
            else:
                captures = [capture_bytes]
            span = _AttemptSpan()
            attempt_started = self._clock()
            answers: list[tuple[dict[str, Any], bytes]] = []
            for index, batch_capture in enumerate(captures):
                if nuevoslibros:
                    batch_dir = isolated / f"batch-{index + 1:03d}"
                    batch_dir.mkdir()
                else:
                    batch_dir = isolated
                if index == 0:
                    timeout = bounded_timeout
                else:
                    # Every batch shares the attempt's single bound (#993).
                    timeout = bounded_timeout - (self._clock() - attempt_started)
                    if timeout <= 0:
                        return self._span_attempt(
                            span, ExtractionStatus.TIMEOUT, False, diagnostic="codex_absolute_ceiling"
                        )
                failure, payload, response_bytes = self._run_batch_call(
                    item, batch_dir, batch_capture, timeout, span, expect_entries=nuevoslibros
                )
                if failure is not None:
                    return failure
                assert payload is not None and response_bytes is not None
                answers.append((payload, response_bytes))
            if nuevoslibros:
                answer = _merge_batch_answers([payload for payload, _ in answers], item)
            else:
                answer = answers[0][0]
            stamp_host_operation_id(answer, item)
            write_json(artifact_path, answer)
            return self._span_attempt(
                span,
                ExtractionStatus.PRODUCED,
                True,
                exit_code=span.returncode,
                output_bytes=sum(len(response_bytes) for _, response_bytes in answers),
            )

    def _write_no_recent_card_artifact(self, item: dict[str, Any], artifact_path: Path) -> ExtractionAttempt:
        """Write the empty artifact of a NuevosLibros capture with no card in the window (#993)."""
        payload: dict[str, Any] = {
            "source_name": str(item.get("source_name", "")),
            "source_url": str(item.get("source_url", "")),
            "operation_id": str(item.get("operation_id", "")),
            "capture_sha256": str(item.get("capture_sha256", "")),
            "entries": [],
            "empty_reason": NO_RECENT_CARD_REASON,
        }
        stamp_host_operation_id(payload, item)
        write_json(artifact_path, payload)
        return ExtractionAttempt(
            ExtractionStatus.PRODUCED,
            True,
            output_bytes=artifact_path.stat().st_size,
            prompt_characters=0,
        )

    def _call_timing(self, fallback_started: float) -> tuple[float, float]:
        started = getattr(self._telemetry, "subprocess_started_at", None)
        finished = getattr(self._telemetry, "subprocess_finished_at", None)
        effective_started = fallback_started if started is None else float(started)
        effective_finished = self._clock() if finished is None else float(finished)
        return effective_started, effective_finished

    def _close_call(self, span: _AttemptSpan, fallback_started: float) -> None:
        """Fold the telemetry of one finished Codex call into its attempt span."""
        started, finished = self._call_timing(fallback_started)
        if span.started is None:
            span.started = started
        span.finished = finished
        for kind, count in dict(getattr(self._telemetry, "progress_event_counts", {})).items():
            span.progress_event_counts[kind] = span.progress_event_counts.get(kind, 0) + count
        followup = getattr(self._telemetry, "empty_answer_followup", "")
        if followup:
            span.empty_answer_followup = followup

    def _span_attempt(
        self,
        span: _AttemptSpan,
        status: ExtractionStatus,
        retryable: bool,
        *,
        exit_code: int | None = None,
        diagnostic: str = "",
        output_bytes: int | None = None,
    ) -> ExtractionAttempt:
        """Build an attempt whose timing and census cover every call it made."""
        duration = None
        if span.started is not None and span.finished is not None:
            duration = max(0.0, span.finished - span.started)
        diagnosis = replace(
            self._attempt_diagnosis(),
            progress_event_counts=dict(span.progress_event_counts),
            empty_answer_followup=span.empty_answer_followup,
        )
        return ExtractionAttempt(
            status,
            retryable,
            exit_code,
            diagnostic,
            subprocess_duration_seconds=duration,
            output_bytes=output_bytes,
            prompt_characters=span.prompt_characters,
            subprocess_started_at=span.started,
            subprocess_finished_at=span.finished,
            diagnosis=diagnosis,
        )

    def _codex_batch_call(
        self, command: list[str], batch_dir: Path, response_path: Path, timeout: float, span: _AttemptSpan
    ) -> tuple[ExtractionAttempt | None, int]:
        """Run one Codex call; return the attempt it failed as, or ``None`` and its exit code.

        A returned failure other than ``UNAVAILABLE`` is not yet bound to the
        attempt span; the caller closes the call and binds it.
        """
        try:
            returncode, stderr_str = self._run_codex(command, batch_dir, response_path, timeout)
        except subprocess.TimeoutExpired:
            raw_boundary_reason = getattr(self._telemetry, "boundary_reason", None)
            boundary_reason = str(raw_boundary_reason) if raw_boundary_reason is not None else None
            diagnostic = {
                "no_progress_timeout": "codex_no_progress_timeout",
                "absolute_execution_ceiling": "codex_absolute_ceiling",
            }.get(boundary_reason or "", "codex_timeout")
            return ExtractionAttempt(ExtractionStatus.TIMEOUT, False, diagnostic=diagnostic), 0
        except OSError as exc:
            return (
                ExtractionAttempt(
                    ExtractionStatus.UNAVAILABLE,
                    False,
                    diagnostic=type(exc).__name__,
                    prompt_characters=span.prompt_characters,
                ),
                0,
            )
        # Redact recognizable credential shapes BEFORE truncating: slicing
        # first could split a token into a fragment that no longer matches
        # any pattern while still leaking a recoverable substring of it.
        diagnostic = redact_secrets(stderr_str.strip())[-500:]
        if returncode != 0:
            permanent = any(term in diagnostic.lower() for term in ("auth", "login", "configuration", "permission"))
            return (
                ExtractionAttempt(ExtractionStatus.COMMAND_FAILED, not permanent, returncode, diagnostic),
                returncode,
            )
        return None, returncode

    def _call_api(self, client: ClaudeApiClient, request: ModelRequest, timeout: float) -> ModelCallResult:
        """Run one Claude API call with the telemetry a Codex call keeps (Issue #1149).

        A non-streaming request reports no progress while it runs, so the
        no-progress lease has nothing to renew it: the call's own deadline is
        the attempt's bound, and a timeout is the absolute ceiling.  The
        progress observer still sees the call start and end.
        """
        started = self._clock()
        self._telemetry.subprocess_started_at = started
        self._telemetry.subprocess_finished_at = None
        self._telemetry.boundary_reason = None
        self._telemetry.progress_event_counts = {}
        self._telemetry.no_progress_quiet_seconds = None
        self._telemetry.unrecognized_event_counts = {}
        self._telemetry.progress_tail = ()
        self._telemetry.codex_session_id = ""
        self._telemetry.rollout_path = ""
        lease = CodexProgressLease()
        lease.begin(started)
        self._observe_progress(lease, started)
        result = client.run(request, timeout=timeout)
        finished = self._clock()
        self._telemetry.subprocess_finished_at = finished
        self._observe_progress(lease, finished)
        if result.status is ModelCallStatus.TIMEOUT:
            self._telemetry.boundary_reason = "absolute_execution_ceiling"
        if result.served_model:
            self._record_effective_profile("model", result.served_model)
            self._record_effective_profile("reasoning_effort", request.reasoning_effort or "default")
        # The request a follow-up resends, with the answer it got.
        self._telemetry.api_request = request
        return result

    def _api_batch_call(
        self, client: ClaudeApiClient, request: ModelRequest, timeout: float, span: _AttemptSpan
    ) -> tuple[ExtractionAttempt | None, int]:
        """Run one Claude API call; return the attempt it failed as, or ``None`` when it answered.

        A refused or unsent call is ``UNAVAILABLE`` and names its failure,
        so a source the backend may not receive (``pw_text_not_permitted``)
        is reported by name and never retried.
        """
        result = self._call_api(client, request, timeout)
        failure = result.failure.value
        if result.status is ModelCallStatus.TIMEOUT:
            return ExtractionAttempt(ExtractionStatus.TIMEOUT, False, diagnostic="codex_absolute_ceiling"), 0
        if result.status in (ModelCallStatus.NOT_STARTED, ModelCallStatus.REFUSED):
            return (
                ExtractionAttempt(
                    ExtractionStatus.UNAVAILABLE,
                    False,
                    diagnostic=failure or result.status.value,
                    prompt_characters=span.prompt_characters,
                ),
                0,
            )
        if result.status is ModelCallStatus.EXIT_NONZERO:
            permanent = result.failure in _PERMANENT_API_FAILURES
            diagnostic = redact_secrets(result.stderr.strip())[-500:]
            return ExtractionAttempt(ExtractionStatus.COMMAND_FAILED, not permanent, result.returncode, diagnostic), 0
        if result.status is ModelCallStatus.UNREADABLE_RESPONSE and failure:
            return ExtractionAttempt(ExtractionStatus.INVALID_OUTPUT, True, 0, failure), 0
        # An answer, or an unreadable one: the shared path reads the response file.
        return None, 0

    def _run_batch_call(
        self,
        item: dict[str, Any],
        batch_dir: Path,
        capture_bytes: bytes,
        timeout: float,
        span: _AttemptSpan,
        *,
        expect_entries: bool,
    ) -> tuple[ExtractionAttempt | None, dict[str, Any] | None, bytes | None]:
        """Run one Codex call over one capture; return its answer, or the attempt that failed.

        ``expect_entries`` is set for NuevosLibros batches: each holds cards, so
        an answer without entries is a failed extraction, never a silent empty one.
        """
        (batch_dir / "capture.json").write_bytes(capture_bytes)
        response_path = batch_dir / "response.json"
        prompt = build_codex_extraction_prompt(item)
        span.prompt_characters += len(prompt)
        call_started = self._clock()
        self._telemetry.subprocess_started_at = None
        self._telemetry.subprocess_finished_at = None
        self._telemetry.boundary_reason = None
        self._telemetry.progress_event_counts = {}
        self._telemetry.no_progress_quiet_seconds = None
        self._telemetry.empty_answer_followup = ""

        client = model_client_for(self.model_pin)
        request = ModelRequest(
            prompt=prompt,
            response_path=response_path,
            # The pinned model and effort (Issue #731). Stage 0 proves the
            # CLI honoured them before capture.
            model=self.model_pin.model,
            reasoning_effort=self.model_pin.reasoning_effort,
            working_dir=batch_dir,
            # An extraction item reads the retained capture and nothing
            # else. The prompt asks for that; these flags enforce it
            # (Issue #414). Stage 0 proves the flags took effect before
            # any capture work is spent.
            disabled_features=EXTRACTION_DISABLED_FEATURES,
            # The Claude API backend sends the capture in the request.
            input_files=("capture.json",),
            max_output_tokens=EXTRACTION_MAX_OUTPUT_TOKENS,
            purpose="extraction",
        )
        if isinstance(client, CodexCliClient):
            failed, returncode = self._codex_batch_call(
                client.command(request), batch_dir, response_path, timeout, span
            )
        else:
            failed, returncode = self._api_batch_call(client, request, timeout, span)
        if failed is not None:
            if failed.status is not ExtractionStatus.UNAVAILABLE:
                self._close_call(span, call_started)
                failed = self._span_attempt(
                    span, failed.status, failed.retryable, exit_code=failed.exit_code, diagnostic=failed.diagnostic
                )
            return failed, None, None
        span.returncode = returncode
        try:
            response_bytes = response_path.read_bytes()
            payload = json.loads(response_bytes.decode("utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            self._close_call(span, call_started)
            output_bytes = response_path.stat().st_size if response_path.exists() else None
            return (
                self._span_attempt(
                    span,
                    ExtractionStatus.INVALID_OUTPUT,
                    True,
                    exit_code=returncode,
                    diagnostic="invalid_json",
                    output_bytes=output_bytes,
                ),
                None,
                None,
            )
        if not isinstance(payload, dict):
            self._close_call(span, call_started)
            return (
                self._span_attempt(
                    span,
                    ExtractionStatus.INVALID_OUTPUT,
                    True,
                    exit_code=returncode,
                    diagnostic="non_object_json",
                    output_bytes=len(response_bytes),
                ),
                None,
                None,
            )
        if is_unreasoned_empty_answer(payload):
            started, finished = self._call_timing(call_started)
            payload, followup_bytes = self._follow_up_empty_answer(
                payload, batch_dir, response_path, timeout - max(0.0, finished - started)
            )
            if followup_bytes is not None:
                response_bytes = followup_bytes
        self._close_call(span, call_started)
        if expect_entries:
            entries = _answer_entries(payload)
            if not isinstance(entries, list):
                return (
                    self._span_attempt(
                        span,
                        ExtractionStatus.INVALID_OUTPUT,
                        True,
                        exit_code=returncode,
                        diagnostic="missing_entries",
                        output_bytes=len(response_bytes),
                    ),
                    None,
                    None,
                )
            if not entries:
                return (
                    self._span_attempt(
                        span,
                        ExtractionStatus.INVALID_OUTPUT,
                        True,
                        exit_code=returncode,
                        diagnostic=NUEVOSLIBROS_EMPTY_BATCH_DIAGNOSTIC,
                        output_bytes=len(response_bytes),
                    ),
                    None,
                    None,
                )
        return None, payload, response_bytes


class QuarantineStatus(str, Enum):
    PRESERVED = "preserved"
    PRESERVATION_FAILED = "preservation_failed"


@dataclass(frozen=True)
class QuarantineResult:
    status: QuarantineStatus
    path: str = ""
    diagnostic: str = ""


def _quarantine_artifact(path: Path, destination_root: Path | None = None) -> QuarantineResult:
    """Move rejected content outside the staged tree, including across filesystems.

    Production callers keep the historical system-temporary destination when
    ``destination_root`` is omitted.  Offline calibration can provide its
    output root so preserved bytes remain beside the calibration artifact.
    """
    digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]
    quarantine_root = Path(tempfile.gettempdir()) if destination_root is None else Path(destination_root)
    quarantine_dir = quarantine_root / "autoscout-rejected-artifacts" / digest
    try:
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        index = 1
        while True:
            quarantined = quarantine_dir / f"{path.name}.rejected-{index}"
            if not quarantined.exists():
                shutil.move(str(path), str(quarantined))
                return QuarantineResult(QuarantineStatus.PRESERVED, str(quarantined))
            index += 1
    except (OSError, shutil.Error) as exc:
        return QuarantineResult(QuarantineStatus.PRESERVATION_FAILED, diagnostic=type(exc).__name__)


EXTRACTION_OUTCOMES_FILE = "codex-extraction-outcomes.json"

_EXTRACTION_SNAPSHOT_STATUSES = frozenset(
    {
        "pending",
        "retry_pending",
        "running",
        "validated",
        "unavailable",
        "failed",
        "invalid_output",
        "command_failed",
        "no_progress_timeout",
        "execution_safety_stop",
        "timeout",
        "worker_exception",
        "budget_exhausted",
        "unfinished",
        "portfolio_capacity_stop",
    }
)
_EXTRACTION_SNAPSHOT_TOP_STATUSES = frozenset({"running", "ok", "failed", "unfinished"})


def _controlled_extraction_snapshot_status(value: Any, *, fallback: str = "failed") -> str:
    candidate = str(value or "")
    return candidate if candidate in _EXTRACTION_SNAPSHOT_STATUSES else fallback


def _snapshot_status_from_result(result: Any) -> str:
    if isinstance(result, dict) and result.get("staged_valid") is True:
        return "validated"
    if isinstance(result, dict):
        for diagnostic in result.get("diagnostics", []):
            if not isinstance(diagnostic, dict):
                continue
            status = _controlled_extraction_snapshot_status(diagnostic.get("status"), fallback="")
            if status:
                return status
        if result.get("_boundary_supplier") == "segment_deadline":
            return "unfinished"
        if result.get("_boundary_supplier") == "portfolio_deadline":
            return "budget_exhausted"
    return "failed"


def _controlled_progress_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        str(event_type): int(count)
        for event_type, count in value.items()
        if (
            str(event_type) in CodexProgressLease.RECOGNIZED_EVENT_TYPES
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count >= 0
        )
    }


def _snapshot_status_from_state(state: dict[str, Any], *, final: bool) -> str:
    if state.get("valid"):
        return "validated"
    if not state.get("eligible"):
        return "unavailable"
    terminal = str(state.get("terminal", "pending"))
    if terminal in {"pending", "unfinished"}:
        return "unfinished" if final else "pending"
    return _controlled_extraction_snapshot_status(terminal)


class _ExtractionProgressSnapshot:
    """Publish extraction rows from the collector thread only."""

    def __init__(
        self,
        output_dir: Path,
        items: list[dict[str, Any]],
        started: float,
        monotonic: Any,
        failure_recorder: ProgressSnapshotFailureRecorder | None = None,
        presentation: ProgressSnapshotPresentation | None = None,
    ) -> None:
        self._writer = ProgressSnapshotWriter(
            output_dir,
            monotonic=monotonic,
            failure_recorder=failure_recorder,
            presentation=presentation,
        )
        self._started = started
        self._index_by_item_id = {id(item): index for index, item in enumerate(items)}
        self._rows: list[dict[str, Any]] = [
            {
                "source_name": str(item.get("source_name", "")),
                "source_url": str(item.get("source_url", "")),
                "state": "not_started",
                "status": "pending",
                "idle_seconds": None,
                "event_counts": {},
            }
            for item in items
        ]

    def _publish(self, now: float, *, status: str = "running", force: bool = True) -> None:
        top_status = status if status in _EXTRACTION_SNAPSHOT_TOP_STATUSES else "failed"
        self._writer.publish(
            stage="extraction",
            status=top_status,
            elapsed_seconds=now - self._started,
            rows=[dict(row) for row in self._rows],
            now=now,
            force=force,
        )

    def initial(self, now: float) -> None:
        self._publish(now)

    def sync_states(self, states: list[dict[str, Any]], now: float, *, final: bool = False) -> None:
        meaningful = False
        for state in states:
            index = self._index_by_item_id.get(id(state["item"]))
            if index is None:
                continue
            row = self._rows[index]
            next_state = "finished" if final or state.get("valid") or not state.get("eligible") else "not_started"
            next_status = _snapshot_status_from_state(state, final=final)
            next_counts = _controlled_progress_counts(state.get("progress_event_counts", {}))
            if (row["state"], row["status"], row["event_counts"]) != (next_state, next_status, next_counts):
                meaningful = True
            row.update(
                {
                    "state": next_state,
                    "status": next_status,
                    "idle_seconds": None,
                    "event_counts": next_counts,
                }
            )
        self._publish(now, force=meaningful or final)

    def poll(self, contexts: list[ExtractionRoundContext], now: float) -> None:
        meaningful = False
        for context in contexts:
            index = self._index_by_item_id.get(id(context.item))
            if index is None:
                continue
            row = self._rows[index]
            idle_seconds, event_counts = context.progress_snapshot(now)
            next_counts = _controlled_progress_counts(event_counts)
            if context.started:
                next_state = "running"
                next_status = "running"
            else:
                next_state = "not_started"
                next_status = "pending"
            if (row["state"], row["status"], row["event_counts"]) != (next_state, next_status, next_counts):
                meaningful = True
            row.update(
                {
                    "state": next_state,
                    "status": next_status,
                    "idle_seconds": _safe_snapshot_seconds(idle_seconds),
                    "event_counts": next_counts,
                }
            )
        self._publish(now, force=meaningful)

    def completion(self, completion: ExtractionRoundCompletion, now: float) -> None:
        index = self._index_by_item_id.get(id(completion.context.item))
        if index is None:
            return
        idle_seconds, event_counts = completion.context.progress_snapshot(now)
        self._rows[index].update(
            {
                "state": "finished",
                "status": _snapshot_status_from_result(completion.result),
                "idle_seconds": None,
                "event_counts": _controlled_progress_counts(event_counts),
            }
        )
        self._publish(now, force=True)

    def retry_pending(self, states: list[dict[str, Any]], now: float) -> None:
        for state in states:
            index = self._index_by_item_id.get(id(state["item"]))
            if index is None:
                continue
            self._rows[index].update(
                {
                    "state": "not_started",
                    "status": "retry_pending",
                    "idle_seconds": None,
                }
            )
        self._publish(now, force=True)

    def final(self, result: dict[str, Any], states: list[dict[str, Any]], now: float) -> None:
        self.sync_states(states, now, final=True)
        status = str(result.get("status", "failed"))
        self._publish(now, status=status, force=True)


def _safe_snapshot_seconds(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return round(max(0.0, number), 3) if math.isfinite(number) else 0.0


def validate_extraction_accounting(result: dict[str, Any], worklist_size: int) -> None:
    """Raise when one extraction result accounts for every worklist item.

    ``execution_safety_stop_count`` is a diagnostic subset of
    ``unfinished_items``. It is deliberately not part of the arithmetic.
    """
    if worklist_size < 0:
        raise ValueError(f"extraction worklist size must be non-negative, got {worklist_size}")
    fields = ("ready_items", "failed_items", "skipped_items", "unfinished_items", "total_items")
    values: dict[str, int] = {}
    for field_name in fields:
        value = result.get(field_name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"extraction accounting field {field_name!r} must be a non-negative integer")
        values[field_name] = value
    accounted = sum(values[field_name] for field_name in fields[:4])
    if values["total_items"] != worklist_size or accounted != values["total_items"]:
        raise ValueError(
            "extraction accounting invariant violated: "
            f"ready_items + failed_items + skipped_items + unfinished_items = {accounted}, "
            f"total_items = {values['total_items']}, worklist_size = {worklist_size}"
        )


def _assemble_extraction_result(result: dict[str, Any], worklist_size: int) -> dict[str, Any]:
    """Validate and return the one assembled production extraction result."""
    validate_extraction_accounting(result, worklist_size)
    return result


def _write_extraction_outcomes(
    output_dir: Path,
    states: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
) -> None:
    """Record each source's terminal extraction outcome for finalization.

    Writing this must never fail the round. The record improves how a failure
    is described; losing it degrades finalization to the older, coarser reason
    rather than invalidating the extraction that already happened.
    """
    rejections: dict[str, dict[str, Any]] = {}
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, dict):
            continue
        if not str(diagnostic.get("status", "")).startswith("rejected_artifact_"):
            continue
        # Later attempts describe the source's final refusal, so they win.
        rejections[str(diagnostic.get("source_name", ""))] = diagnostic

    outcomes: list[dict[str, Any]] = []
    for state in states:
        item = state["item"]
        source_name = str(item.get("source_name", ""))
        artifact_present = bool(state["expected"]) and state["artifact_path"].exists()
        rejection = rejections.get(source_name)
        if artifact_present:
            outcome = "artifact_present"
        elif rejection is not None:
            outcome = "rejected"
        else:
            outcome = "absent"
        record: dict[str, Any] = {
            "source_name": source_name,
            "source_url": str(item.get("source_url", "")),
            "expected_extraction_file": str(state["expected"]),
            "operation_id": str(item.get("operation_id", "")),
            "submission_sequence": state.get("submission_sequence"),
            "outcome": outcome,
            "terminal_class": str(state.get("terminal", "pending")),
        }
        # Issue #700: state the empty-extraction retry so the flakiness rate
        # is visible in the run artifact instead of inferred. Recorded
        # whatever the retry produced, including a retry that stayed empty or
        # failed, so an attempted recovery is never silent.
        if state.get("empty_retry_attempted"):
            record["empty_extraction_retry"] = True
            # Recovery means a validated, explicitly non-empty retry result.
            # `not empty_extraction` was wrong: an invalid second attempt
            # also clears that flag, so a failed retry read as a recovery.
            record["empty_extraction_recovered"] = bool(state.get("empty_retry_recovered"))
        elif state.get("empty_retry_used"):
            # The allowance was spent but admission or the execution deadline
            # prevented invocation.
            # Saying nothing would hide budget pressure; saying "retried"
            # would overstate the effort actually made.
            record["empty_extraction_retry_refused"] = True
        # Issue #824: state the retry that followed a no-progress timeout, so
        # whether a stall recurs on a fresh session is measured, not inferred.
        if state.get("no_progress_retry_attempted"):
            record["no_progress_retry"] = True
            record["no_progress_recovered"] = bool(state.get("valid"))
        elif state.get("no_progress_retry_used"):
            record["no_progress_retry_refused"] = True
        if rejection is not None:
            record["diagnostic_class"] = str(rejection.get("diagnostic_class", "") or rejection.get("diagnostic", ""))
            raw_count = rejection.get("raw_extracted_count")
            if raw_count is not None:
                record["raw_extracted_count"] = int(raw_count)
            record["diagnostic_artifact"] = str(rejection.get("diagnostic_artifact", ""))
        outcomes.append(record)

    try:
        write_json(output_dir / EXTRACTION_OUTCOMES_FILE, {"outcomes": outcomes})
    except OSError:
        return


def _observed_rejection(
    artifact_path: Path, item: dict[str, Any], capture_path: Path | None
) -> tuple[str, int | None, tuple[dict[str, str], ...]]:
    """Name why an on-disk artifact fails validation, without publishing it.

    Used on the pre-round path, where the boolean validator has already said
    "not valid" but the reason was thrown away. Returns an empty class rather
    than raising: naming a rejection must never be able to stop the quarantine
    that preserves the evidence.
    """
    if capture_path is None:
        return "", None, ()
    try:
        from autoscout.observed_extraction import validate_extraction_artifact

        observation = validate_extraction_artifact(artifact_path, item, capture_path)
    except Exception:
        return "", None, ()
    return (observation.diagnostic_class or ""), observation.raw_extracted_count, observation.identity_mismatches


def _record_quarantine_diagnostic(
    item: dict[str, Any],
    quarantine: QuarantineResult,
    attempt_number: int | None = None,
    *,
    diagnostic_class: str = "",
    raw_extracted_count: int | None = None,
    identity_mismatches: Sequence[Mapping[str, str]] = (),
) -> dict[str, Any]:
    """Build a quarantine diagnostic entry for a rejected artifact.

    The validator has already decided *why* it refused the artifact, so that
    class travels with the quarantine record. Without it the operator sees a
    rejection with an empty ``diagnostic`` and has to re-run the validator by
    hand to learn whether the artifact was malformed, ungrounded, or merely
    missing its identity envelope (Issue #386).

    ``raw_extracted_count`` states how many entries the refused artifact
    held. A rejection that discarded twenty grounded mentions is a different
    operational event from one that discarded nothing, and only the count
    tells them apart.

    ``identity_mismatches`` names which envelope field an ``identity_mismatch``
    refusal found wrong, and how (Issue #823).
    """
    diag: dict[str, Any] = {
        "source_name": str(item.get("source_name", "")),
        "status": "rejected_artifact_" + quarantine.status.value,
        "diagnostic_artifact": quarantine.path,
        # A preservation error describes the quarantine itself and outranks the
        # validation class: it is the reason the bytes may not be readable.
        "diagnostic": quarantine.diagnostic or diagnostic_class,
    }
    if diagnostic_class:
        diag["diagnostic_class"] = diagnostic_class
    if raw_extracted_count is not None:
        diag["raw_extracted_count"] = int(raw_extracted_count)
    if identity_mismatches:
        diag["identity_mismatches"] = [dict(mismatch) for mismatch in identity_mismatches]
    if attempt_number is not None:
        diag["attempt"] = attempt_number
    return diag


def _sanitized_exception_diagnostic(
    item: dict[str, Any], attempt_number: int, duration: float, exc_type: type[BaseException]
) -> dict[str, Any]:
    """Build a source-local diagnostic for an unexpected worker exception.

    Only includes source name, attempt, duration, and exception class name.
    Never exposes exception messages, tracebacks, or potentially sensitive
    filesystem paths.
    """
    return {
        "source_name": str(item.get("source_name", "")),
        "attempt": attempt_number,
        "status": ExtractionStatus.WORKER_EXCEPTION.value,
        "diagnostic": exc_type.__name__,
        "duration_seconds": round(duration, 3),
    }


def _matches_exact_list_metadata(entry: dict[str, Any], item: dict[str, Any]) -> bool:
    """Return whether an extracted entry preserves binding exact-list metadata."""
    return not _exact_list_metadata_failure_status(entry, item)


# Compatibility names remain private for existing production tests.
_DaemonFuture = DaemonFuture
_DaemonThreadPool = DaemonThreadPool
_CancellationToken = CancellationToken


# ── Outcome collection ────────────────────────────────────────────────────

# run_attempt no longer mutates the shared *state* dict directly from the
# worker thread.  Instead it returns a local result dictionary (the same
# shape as before).  The collector thread inspects the result and, only
# when the cancellation token is still live and the outcome is valid,
# publishes staging → canonical and updates *state*.
#
# This eliminates the race where a late worker could call
# staging_path.replace(canonical) or set state['valid'] = True after
# the collection already returned a timeout diagnostic.


def _run_attempt_local(
    adapter: ProductionCodexAdapter,
    state: dict[str, Any],
    attempt_number: int,
    deadline: float,
    cancel_token: _CancellationToken,
    context: ExtractionRoundContext | None = None,
) -> dict[str, Any]:
    """Compute a single extraction attempt locally (safe for worker thread).

    Reads from *state* but does NOT mutate it.  All side-effects (staging
    writes, validation) are local.  The returned dict is the same shape that
    the collector thread expects so existing diagnostic / preservation
    handling works unchanged.
    """
    item = state["item"]
    attempt_started = adapter._monotonic()
    remaining = deadline - attempt_started
    if context is not None:
        # Do not charge a worker while it is between pool dispatch and the
        # real agent invocation. This is important with deterministic clocks
        # shared by concurrent fake workers.
        if attempt_started >= context.portfolio_deadline:
            remaining = 0.0
        else:
            remaining = adapter._per_attempt_timeout_seconds
    if remaining <= 0:
        return {
            "started": False,
            "cancel_token": cancel_token,
            "preservation_failed": False,
            "failed_path": "",
            "diagnostics": [
                {
                    "source_name": str(item.get("source_name", "")),
                    "attempt": attempt_number,
                    "status": "budget_exhausted",
                    "diagnostic": "portfolio_budget_exhausted",
                    "duration_seconds": round(state.get("duration", 0.0), 3),
                }
            ],
            "staged_valid": False,
            "elapsed": 0.0,
            "extraction": None,
            "staging_path": None,
            "progress_event_counts": {},
            "no_progress_quiet_seconds": None,
        }

    staging_path = state["artifact_path"].parent / ("." + state["artifact_path"].name + "." + uuid.uuid4().hex)
    if context is not None:
        # The actual source ceiling starts at this boundary, immediately
        # before the observable extraction call.
        attempt_started = adapter._monotonic()
        context.mark_attempt_started(attempt_started)
        context.source_deadline = attempt_started + adapter._per_attempt_timeout_seconds
        attempt_deadline = context.effective_deadline
        deadline = context.portfolio_deadline
        remaining = max(0.0, attempt_deadline - attempt_started)
    timeout_for_attempt = min(adapter._per_attempt_timeout_seconds, remaining)
    try:
        from autoscout.observed_extraction import observe_extraction

        observer_factory = getattr(adapter._agent, "progress_observer", None)
        observer_scope = (
            observer_factory(context.update_progress_snapshot)
            if context is not None and callable(observer_factory)
            else nullcontext()
        )
        with observer_scope:
            observed = observe_extraction(
                item,
                state["capture_path"],
                staging_path,
                timeout_seconds=timeout_for_attempt,
                deadline=deadline,
                cancellation=cancel_token,
                clock=adapter._monotonic,
                agent=adapter._agent,
                submitted_at=attempt_started,
                boundary_supplier_hint=(context.limiting_supplier if context is not None else None),
                phase_telemetry=(context.phase_telemetry if context is not None else None),
                raise_agent_exceptions=True,
            )
    except Exception as exc:
        # Convert unexpected worker exceptions into the existing
        # sanitized-exception path.  The staging path (if written) will be
        # quarantined by the collector.
        if context is not None:
            context.phase_telemetry.mark_collection_started(adapter._monotonic())
            context.phase_telemetry.mark_collection_finished(adapter._monotonic())
        elapsed = max(0.0, adapter._monotonic() - attempt_started)
        staging_exists = staging_path.exists()
        return {
            "started": True,
            "cancel_token": cancel_token,
            "preservation_failed": False,
            "failed_path": "",
            "diagnostics": [],
            "staged_valid": False,
            "elapsed": elapsed,
            "extraction": None,
            "staging_path": staging_path if staging_exists else None,
            "_worker_exception": type(exc),
        }

    if context is not None:
        context.phase_telemetry.mark_collection_started(adapter._monotonic())
        context.phase_telemetry.mark_collection_finished(adapter._monotonic())
        collection_duration = context.phase_telemetry.known_durations()["collection_duration_seconds"]
        if collection_duration is not None:
            observed = replace(observed, collection_duration_seconds=collection_duration)

    extraction = observed.extraction_attempt
    elapsed = observed.total_duration_seconds or 0.0
    staged_valid = observed.final_status == "validated"
    publish_failed = False
    if staged_valid and observed.validation is not None:
        # A list kept without its failing rows is published without them, so
        # the collector never reads a row the gate refused (Issue #839).
        from autoscout.observed_extraction import publish_partial_artifact

        try:
            publish_partial_artifact(staging_path, observed.validation)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            staged_valid = False
            publish_failed = True
    validation_status = "partial_publish_failed" if publish_failed else ""
    if observed.validation is not None and not observed.validation.valid:
        validation_status = observed.validation.diagnostic_class or "validation_failed"
    elif observed.final_status == "input_invalid":
        validation_status = observed.diagnostic_class
    return {
        "started": observed.started,
        "cancel_token": cancel_token,
        "preservation_failed": False,
        "failed_path": "",
        "diagnostics": [],
        "staged_valid": staged_valid,
        "validation_status": validation_status,
        # Carried so a quarantine record can state how much extracted work the
        # rejection discarded (Issue #386).
        "raw_extracted_count": (observed.validation.raw_extracted_count if observed.validation is not None else None),
        "identity_mismatches": (observed.validation.identity_mismatches if observed.validation is not None else ()),
        "elapsed": elapsed,
        "extraction": extraction,
        "staging_path": staging_path,
        "progress_event_counts": extraction.diagnosis.progress_event_counts if extraction is not None else {},
        "no_progress_quiet_seconds": (
            extraction.diagnosis.no_progress_quiet_seconds if extraction is not None else None
        ),
    }


BOUNDED_COMPLETION_STATUSES = {"timeout", "budget_exhausted"}


def _completion_was_bounded(result: Any) -> bool:
    """Return True when a round result was cut by a timeout or the budget."""
    if not isinstance(result, dict):
        return False
    diagnostics = result.get("diagnostics")
    if not isinstance(diagnostics, list):
        return False
    return any(
        isinstance(diagnostic, dict) and diagnostic.get("status") in BOUNDED_COMPLETION_STATUSES
        for diagnostic in diagnostics
    )


def _round_boundary_local_result(
    context: ExtractionRoundContext,
    *,
    stage: str,
    threshold: float,
    supplier: str,
    attempt_number: int,
    clock: Any,
    segment_mode: bool = False,
) -> dict[str, Any]:
    """Represent a shared-round boundary without publishing or retrying it."""
    started = context.started_at is not None
    started_at = context.started_at if context.started_at is not None else context.submitted_at
    boundary_at = (
        context.effective_deadline
        if started
        else min(
            context.portfolio_deadline,
            context.admission_deadline if context.admission_deadline is not None else context.portfolio_deadline,
        )
    )
    portfolio_boundary = supplier == "portfolio_deadline"
    segment_boundary = supplier == "segment_deadline"
    budget_boundary = portfolio_boundary and stage in {"queue", "portfolio"}
    known_durations = context.phase_telemetry.known_durations(boundary_at=boundary_at if started else None)
    if segment_boundary:
        status = "unfinished"
        diagnostic = "segment_boundary_unfinished"
    elif segment_mode and portfolio_boundary and started:
        status = "portfolio_capacity_stop"
        diagnostic = "portfolio_capacity_stop"
    elif segment_mode and supplier == "source_execution_ceiling" and started:
        status = "execution_safety_stop"
        diagnostic = "execution_safety_stop"
    else:
        status = "budget_exhausted" if budget_boundary else "timeout"
        diagnostic = (
            "portfolio_budget_exhausted"
            if budget_boundary
            else {
                "queue": "worker_collection_timeout",
                "subprocess": "worker_collection_timeout",
                "validation": "validation_timeout",
                "collection": "worker_collection_timeout",
                "portfolio": "portfolio_budget_exhausted",
            }[stage]
        )
    diagnostics = [
        {
            "source_name": str(context.item.get("source_name", "")),
            "attempt": attempt_number,
            "status": status,
            "diagnostic": diagnostic,
            "duration_seconds": round(
                max(0.0, boundary_at - started_at) if started else 0.0,
                3,
            ),
            "queue_wait_seconds": round(
                max(0.0, started_at - context.submitted_at) if started else threshold,
                6,
            ),
            "subprocess_duration_seconds": known_durations["subprocess_duration_seconds"],
            "validation_duration_seconds": known_durations["validation_duration_seconds"],
            "collection_duration_seconds": known_durations["collection_duration_seconds"],
            "timeout_stage": stage,
            "applied_timeout_threshold": threshold,
            "boundary_supplier": supplier,
            "worker_slot": context.worker_slot if context.started else None,
            "submission_sequence": context.submission_sequence,
            "started": context.started,
        }
    ]
    return {
        "started": started,
        "cancel_token": context.token,
        "preservation_failed": False,
        "failed_path": "",
        "diagnostics": diagnostics,
        "staged_valid": False,
        "elapsed": max(0.0, boundary_at - started_at) if started else 0.0,
        "extraction": None,
        "staging_path": None,
        "_boundary_stage": stage,
        "_boundary_supplier": supplier,
        "_boundary_at": boundary_at,
        "_boundary_durations": known_durations,
        "_applied_threshold": threshold,
    }


def _shared_late_result_cleanup(context: ExtractionRoundContext, result: Any) -> None:
    """Clean a late worker's private staging bytes without publication."""
    if not isinstance(result, dict):
        return
    staging_path = result.get("staging_path")
    if not isinstance(staging_path, Path) or not staging_path.exists():
        return
    quarantine = _quarantine_artifact(staging_path)
    if quarantine.status is QuarantineStatus.PRESERVATION_FAILED:
        try:
            staging_path.unlink(missing_ok=True)
        except OSError:
            pass


def _item_source_declaration(item: Any) -> dict[str, Any]:
    """Return the source declaration carried by one extraction work item.

    The work item carries the configured URL top-level as ``source_url``
    while ``source_metadata`` holds only portable artifact fields, so the
    URL is folded in under the ``url`` key the retry predicate reads.
    Without it every work item would read as URL-less and the
    empty-extraction retry would never fire (issue #700 part A)."""
    metadata = getattr(item, "source_metadata", None)
    if metadata is None and hasattr(item, "get"):
        metadata = item.get("source_metadata")
    declaration = dict(metadata) if isinstance(metadata, dict) else {}
    url = getattr(item, "source_url", None)
    if url is None and hasattr(item, "get"):
        url = item.get("source_url")
    if isinstance(url, str) and url.strip() and not str(declaration.get("url", "")).strip():
        declaration["url"] = url.strip()
    return declaration


def _should_reattempt(state: dict[str, Any]) -> bool:
    """Say whether one extraction state deserves another attempt.

    The historical rule is a failed, eligible, transiently-retryable attempt
    that did not time out.

    Issue #700 adds one case: an attempt that produced a *valid* artifact
    holding no entries, for a source declared to publish a complete list on
    every check. Extraction is non-deterministic -- replaying the preserved
    captures from run 4090c59a returned a full list where the run returned
    none, from byte-identical input with an exit code of 0 both times. Such
    an artifact is valid, so the historical rule never retried it and three
    healthy sources were recorded as failed every week.

    The empty case is allowed exactly one extra attempt (``empty_retry_used``),
    so a source that is genuinely empty costs one extraction more and is then
    reported as before. ``state["valid"]`` is deliberately left true, so a
    persistently empty artifact keeps its existing outcome rather than being
    reported as invalid.
    """
    if state["valid"]:
        return bool(
            state.get("empty_extraction")
            and not state.get("empty_retry_used")
            and state["eligible"]
            and not state["timed_out"]
        )
    if state.get("terminal") == "no_progress_timeout":
        # Issue #824: a session that started and then went quiet for the
        # whole lease is not a broken source -- the same sources extract
        # normally in neighbouring runs. One fresh session is admitted through
        # the ordinary retry waves, so the deadline guarantee is unchanged.
        # Absolute execution-safety stops keep their own terminal and stay
        # non-retryable.
        return bool(state["eligible"] and not state.get("no_progress_retry_used"))
    return bool(state["eligible"] and state["retryable"] and not state["timed_out"])


def _plan_retry_wave(
    pending: list[dict[str, Any]],
    *,
    retry_capacity: Callable[[], float],
    per_attempt_timeout: float,
    projected_makespan: Callable[[list[float]], float],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split pending retries into the wave that fits now and the deferred rest.

    Every admitted retry is still costed at the full per-attempt timeout, so a
    wave can never run past the deadline minus the retry headroom.  A retry
    that does not fit is deferred rather than refused (Issue #732): it is
    costed again against the time actually left once the current wave has
    finished.  Real retries take a small fraction of the timeout, so a
    deferred retry usually fits the next wave.
    """
    admitted: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    projected: list[float] = []
    for state in pending:
        if projected_makespan([*projected, per_attempt_timeout]) <= retry_capacity() + 1e-9:
            admitted.append(state)
            projected.append(per_attempt_timeout)
        else:
            deferred.append(state)
    return admitted, deferred


def _record_retry_admission(state: dict[str, Any], retry_admission: dict[str, Any]) -> None:
    retry_admission["admitted_count"] += 1
    source_name = str(state["item"].get("source_name", ""))
    retry_admission["admitted_sources"].append(source_name)
    if state.get("terminal") == "no_progress_timeout":
        retry_admission.setdefault("admitted_after_no_progress", []).append(source_name)


def _record_retry_refusal(
    state: dict[str, Any],
    attempt_number: int,
    retry_admission: dict[str, Any],
    diagnostics: list[dict[str, Any]],
) -> None:
    """Refuse one retry for good, once no further wave can admit it."""
    retry_admission["refused_count"] += 1
    source_name = str(state["item"].get("source_name", ""))
    retry_admission["refused_sources"].append(source_name)
    state["retryable"] = False
    diagnostics.append(
        {
            "source_name": source_name,
            "attempt": attempt_number,
            "submission_sequence": state["submission_sequence"],
            "status": "retry_not_admitted_budget",
            "diagnostic": "retry_not_admitted_budget",
            "started": False,
            "duration_seconds": 0.0,
        }
    )


def _is_transient_retryable(
    extraction: ExtractionAttempt | None,
    validation_status: str = "",
) -> bool:
    """Allow only the existing transient retry outcomes."""
    if extraction is None or not extraction.retryable:
        return False
    if extraction.status not in {
        ExtractionStatus.INVALID_OUTPUT,
        ExtractionStatus.COMMAND_FAILED,
        ExtractionStatus.PRODUCED,
    }:
        return False
    value = f"{extraction.diagnostic} {validation_status}".casefold()
    return not any(
        term in value
        for term in ("timeout", "structural", "authentication", "permission", "configuration", "unavailable")
    )


def _apply_local_result(
    result: dict[str, Any],
    state: dict[str, Any],
    attempt_number: int,
) -> dict[str, Any]:
    """Apply the local worker outcome to *state* from the collector thread.

    This is the ONLY place where canonical state mutation or artifact
    replacement occurs.  The cancellation token gates publication: if the
    token was cancelled (collection timeout), staging bytes are quarantined
    and no state mutation occurs.
    """
    item = state["item"]
    cancel_token = result.get("cancel_token")
    cancelled = isinstance(cancel_token, _CancellationToken) and cancel_token.cancelled
    extraction = result.get("extraction")
    staging_path: Path | None = result.get("staging_path")
    elapsed: float = result.get("elapsed", 0.0)
    staged_valid: bool = result.get("staged_valid", False)
    validation_status = str(result.get("validation_status", ""))
    raw_extracted_count = result.get("raw_extracted_count")
    identity_mismatches = result.get("identity_mismatches") or ()
    diagnostics: list[dict[str, Any]] = list(result.get("diagnostics", []))
    preservation_failed = result.get("preservation_failed", False)
    failed_path = result.get("failed_path", "")

    progress_counts = result.get("progress_event_counts", {})
    if isinstance(progress_counts, dict):
        state_counts = state.setdefault("progress_event_counts", {})
        for event_type, count in progress_counts.items():
            if isinstance(count, int):
                state_counts[str(event_type)] = state_counts.get(str(event_type), 0) + count
    if result.get("no_progress_quiet_seconds") is not None:
        state["no_progress_quiet_seconds"] = result.get("no_progress_quiet_seconds")

    # Admission and dispatch do not prove invocation: the worker can still
    # stop at its deadline. Record effort from execution evidence, including
    # failed or cancelled attempts, before their early-return paths below.
    if state.get("empty_retry_used") and result.get("started"):
        state["empty_retry_attempted"] = True
    if state.get("no_progress_retry_used") and result.get("started"):
        state["no_progress_retry_attempted"] = True

    # Update cumulative duration regardless of cancellation.
    state["duration"] = state.get("duration", 0.0) + elapsed

    # Handle unexpected worker exception (not an ExtractionAttempt).
    worker_exc: type[BaseException] | None = result.get("_worker_exception")
    if worker_exc is not None:
        state["valid"] = False
        state["retryable"] = False
        state["timed_out"] = False
        state["terminal"] = "worker_exception"
        if staging_path is not None and staging_path.exists():
            quarantine = _quarantine_artifact(staging_path)
            diagnostics.append(
                _record_quarantine_diagnostic(
                    item,
                    quarantine,
                    attempt_number,
                    diagnostic_class=validation_status,
                    raw_extracted_count=raw_extracted_count,
                    identity_mismatches=identity_mismatches,
                )
            )
            if quarantine.status is QuarantineStatus.PRESERVATION_FAILED:
                preservation_failed = True
                failed_path = str(staging_path)
        diagnostics.append(_sanitized_exception_diagnostic(item, attempt_number, elapsed, worker_exc))
        return {
            "started": result.get("started", True),
            "preservation_failed": preservation_failed,
            "failed_path": failed_path,
            "diagnostics": diagnostics,
        }

    if not result.get("started"):
        # Budget or segment admission was exhausted before agent invocation.
        state["retryable"] = False
        state["terminal"] = (
            "unfinished" if result.get("_boundary_supplier") == "segment_deadline" else "budget_exhausted"
        )
        return {
            "started": False,
            "preservation_failed": False,
            "failed_path": "",
            "diagnostics": diagnostics,
        }

    if cancelled:
        # A shared-round boundary cancelled this worker.  Quarantine staging
        # bytes; do NOT mutate canonical state.  Source execution boundaries
        # remain timeouts and are never retryable.
        boundary_stage = str(result.get("_boundary_stage", "collection"))
        boundary_supplier = str(result.get("_boundary_supplier", ""))
        boundary_status = next(
            (str(diagnostic.get("status", "")) for diagnostic in diagnostics if isinstance(diagnostic, dict)),
            "",
        )
        state["valid"] = False
        state["retryable"] = False
        state["timed_out"] = boundary_stage in {"subprocess", "validation", "collection"}
        if boundary_status in {"unfinished", "portfolio_capacity_stop", "execution_safety_stop", "worker_exception"}:
            state["terminal"] = boundary_status
        elif boundary_supplier == "segment_deadline":
            state["terminal"] = "unfinished"
        else:
            state["terminal"] = "timeout" if state["timed_out"] else "budget_exhausted"
        if staging_path is not None and staging_path.exists():
            quarantine = _quarantine_artifact(staging_path)
            diagnostics.append(
                _record_quarantine_diagnostic(
                    item,
                    quarantine,
                    attempt_number,
                    diagnostic_class=validation_status,
                    raw_extracted_count=raw_extracted_count,
                    identity_mismatches=identity_mismatches,
                )
            )
            if quarantine.status is QuarantineStatus.PRESERVATION_FAILED:
                preservation_failed = True
                failed_path = str(staging_path)
        return {
            "started": bool(result.get("started", True)),
            "preservation_failed": preservation_failed,
            "failed_path": failed_path,
            "diagnostics": diagnostics,
        }

    # Normal path — publish if valid.
    if staged_valid:
        try:
            staging_path.replace(state["artifact_path"])  # type: ignore[union-attr]
        except OSError:
            staged_valid = False

    if not staged_valid and staging_path is not None and staging_path.exists():
        quarantine = _quarantine_artifact(staging_path)
        diagnostics.append(
            _record_quarantine_diagnostic(
                item,
                quarantine,
                attempt_number,
                diagnostic_class=validation_status,
                raw_extracted_count=raw_extracted_count,
                identity_mismatches=identity_mismatches,
            )
        )
        preservation_failed = quarantine.status is QuarantineStatus.PRESERVATION_FAILED
        if preservation_failed:
            failed_path = str(staging_path)
    if staging_path is not None and staging_path.exists() and not preservation_failed:
        try:
            staging_path.unlink(missing_ok=True)
        except OSError:
            pass

    state["valid"] = staged_valid
    state["timed_out"] = extraction.status is ExtractionStatus.TIMEOUT if extraction is not None else False
    state["retryable"] = _is_transient_retryable(extraction, validation_status)
    # Issue #700: a valid artifact holding no entries, from a source declared
    # to publish a complete list, is the non-deterministic case and earns one
    # more attempt. Recorded on the state so the run artifact states it.
    # Explicitly zero, never merely unknown: `not raw_extracted_count` also
    # matches None, which would treat every attempt that reported no count as
    # empty and retry it.
    state["empty_extraction"] = bool(
        staged_valid
        and raw_extracted_count == 0
        and should_retry_empty_extraction(_item_source_declaration(item), extracted_count=0)
    )
    if state.get("empty_retry_attempted") and staged_valid and (raw_extracted_count or 0) > 0:
        # Recovery is a validated result that actually holds entries. An
        # invalid retry clears empty_extraction too, so absence of the flag
        # is not evidence of recovery.
        state["empty_retry_recovered"] = True

    status = validation_status or (extraction.status.value if extraction is not None else "worker_exception")
    if extraction is not None and staged_valid:
        status = "validated"
        state["terminal"] = "validated"
    elif extraction is None:
        state["terminal"] = "worker_exception"
    elif extraction.diagnostic == "codex_no_progress_timeout":
        status = "no_progress_timeout"
        state["terminal"] = "no_progress_timeout"
    elif extraction.diagnostic == "codex_absolute_ceiling":
        status = "execution_safety_stop"
        state["terminal"] = "execution_safety_stop"
    elif extraction.status is ExtractionStatus.INVALID_OUTPUT or validation_status:
        state["terminal"] = "invalid_output"
    elif extraction.status is ExtractionStatus.COMMAND_FAILED:
        state["terminal"] = "command_failed"
    elif extraction.status is ExtractionStatus.UNAVAILABLE:
        state["terminal"] = "unavailable"
    elif extraction.status is ExtractionStatus.WORKER_EXCEPTION:
        state["terminal"] = "worker_exception"
    elif extraction.status is ExtractionStatus.TIMEOUT:
        state["terminal"] = "timeout"
    else:
        state["terminal"] = status
    diagnostics.append(
        {
            "source_name": str(item.get("source_name", "")),
            "attempt": attempt_number,
            "status": status,
            "exit_code": extraction.exit_code if extraction is not None else None,
            "diagnostic": validation_status or (extraction.diagnostic if extraction is not None else ""),
            "duration_seconds": round(elapsed, 3),
            **attempt_diagnosis_evidence(extraction, status),
        }
    )
    return {
        "started": True,
        "preservation_failed": preservation_failed,
        "failed_path": failed_path,
        "diagnostics": diagnostics,
    }


class ProductionCodexCaptureAdapter:
    """Run the capture stage once under the bounds the portfolio declares.

    The stage budget defaults to ``None``, which means "derive it from the
    configured portfolio".  A flat constant default here is what made the
    issue #595 derivation inert in production: the capture worklist derives a
    budget only when it is handed none, and both production links used to hand
    it the base constant (issue #717).
    """

    def __init__(
        self,
        monotonic: Any = time.monotonic,
        *,
        runtime_budget_seconds: float | None = None,
        per_source_timeout_seconds: float = CAPTURE_SOURCE_TIMEOUT_SECONDS,
        retry_source_timeout_seconds: float = CAPTURE_SOURCE_RETRY_TIMEOUT_SECONDS,
        sleep: Any = time.sleep,
        progress_snapshot_failure_recorder: ProgressSnapshotFailureRecorder | None = None,
    ) -> None:
        if (runtime_budget_seconds is not None and runtime_budget_seconds <= 0) or per_source_timeout_seconds <= 0:
            raise ValueError("capture runtime budget and per-source timeout must be positive")
        if not math.isfinite(retry_source_timeout_seconds) or retry_source_timeout_seconds < per_source_timeout_seconds:
            raise ValueError("capture retry timeout must be finite and at least the per-source timeout")
        self._monotonic = monotonic
        self._runtime_budget_seconds = runtime_budget_seconds
        self._per_source_timeout_seconds = per_source_timeout_seconds
        self._retry_source_timeout_seconds = retry_source_timeout_seconds
        self._sleep = sleep
        self._progress_snapshot_failure_recorder = progress_snapshot_failure_recorder
        self._snapshot_progress_presentation: ProgressSnapshotPresentation | None = None

    def set_progress_snapshot_failure_recorder(self, recorder: ProgressSnapshotFailureRecorder | None) -> None:
        self._progress_snapshot_failure_recorder = recorder

    def set_snapshot_progress_presentation(self, presentation: ProgressSnapshotPresentation | None) -> None:
        """Bind the optional per-item snapshot heartbeat (issue #452)."""
        self._snapshot_progress_presentation = presentation

    def prepare(
        self,
        config_path: Path,
        output_dir: Path,
        *,
        live: bool = True,
        effective_date: str | None = None,
        kirkus_mode: str = "all",
        runtime_budget_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Capture once: portfolio-wide retries used to repeat successful work."""
        started = self._monotonic()
        rc = prepare_codex_extraction(
            config_path,
            output_dir,
            live=live,
            effective_date=effective_date,
            monotonic=self._monotonic,
            runtime_budget_seconds=(
                runtime_budget_seconds if runtime_budget_seconds is not None else self._runtime_budget_seconds
            ),
            per_source_timeout_seconds=self._per_source_timeout_seconds,
            retry_source_timeout_seconds=self._retry_source_timeout_seconds,
            sleep=self._sleep,
            progress_snapshot_failure_recorder=self._progress_snapshot_failure_recorder,
            snapshot_progress_presentation=self._snapshot_progress_presentation,
            kirkus_mode=kirkus_mode,
            apply_collection_schedule=True,
        )
        source_runs = read_json(output_dir / "source-runs.json", default=[])
        return {
            "returncode": rc,
            "output_dir": str(output_dir),
            "live": live,
            "attempts": 1,
            "duration_seconds": round(max(0.0, self._monotonic() - started), 3),
            # The budget the stage applied, read back from the worklist it
            # produced, so the logged bound is the one that was enforced.
            "runtime_budget_seconds": applied_capture_runtime_budget(
                output_dir,
                default=(
                    self._runtime_budget_seconds
                    if self._runtime_budget_seconds is not None
                    else CAPTURE_RUNTIME_BUDGET_SECONDS
                ),
            ),
            "per_source_timeout_seconds": self._per_source_timeout_seconds,
            "source_runs": source_runs,
        }


class ProductionCodexAdapter:
    """Fulfil work items through Codex and validate their extraction artifacts.

    The default agent invokes the installed Codex subscription CLI in an
    isolated read-only directory. Tests and alternate coding-agent hosts may
    inject the same narrow ``ExtractionAgent`` boundary.

    Extraction parallelism uses a bounded daemon-thread pool so that an
    abandoned hung worker never blocks process exit.  Worker outcomes are
    computed locally and applied in the collector/main thread under a
    per-attempt cancellation token that gates canonical-state publication
    (issue #306).
    """

    def __init__(
        self,
        agent: ExtractionAgent | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Any = time.sleep,
        runtime_budget_seconds: float = PORTFOLIO_RUNTIME_BUDGET_SECONDS,
        max_attempts: int = MAX_EXTRACTION_RETRIES,
        per_attempt_timeout_seconds: float = CODEX_ABSOLUTE_CEILING_SECONDS,
        max_workers: int = DEFAULT_EXTRACTION_WORKERS,
        retry_headroom_seconds: float = RETRY_HEADROOM_SECONDS,
        on_progress: Callable[[int, int, int, int], None] | None = None,
        progress_snapshot_failure_recorder: ProgressSnapshotFailureRecorder | None = None,
    ) -> None:
        if runtime_budget_seconds <= 0:
            raise ValueError("extraction runtime budget must be positive")
        if max_attempts <= 0:
            raise ValueError("maximum extraction attempts must be positive")
        if per_attempt_timeout_seconds <= 0:
            raise ValueError("per-attempt extraction timeout must be positive")
        if max_workers <= 0:
            raise ValueError("maximum extraction workers must be positive")
        if not math.isfinite(float(retry_headroom_seconds)) or retry_headroom_seconds < 0:
            raise ValueError("retry headroom must be finite and non-negative")
        self._agent = agent or CodexCliExtractionAgent()
        self._monotonic = monotonic
        self._sleep = sleeper
        self._runtime_budget_seconds = runtime_budget_seconds
        self._max_attempts = max_attempts
        self._per_attempt_timeout_seconds = per_attempt_timeout_seconds
        self._max_workers = max_workers
        self._retry_headroom_seconds = float(retry_headroom_seconds)
        self._on_progress = on_progress
        self._progress_snapshot_failure_recorder = progress_snapshot_failure_recorder
        self._snapshot_progress_presentation: ProgressSnapshotPresentation | None = None

    def set_progress_snapshot_failure_recorder(self, recorder: ProgressSnapshotFailureRecorder | None) -> None:
        self._progress_snapshot_failure_recorder = recorder

    def set_snapshot_progress_presentation(self, presentation: ProgressSnapshotPresentation | None) -> None:
        """Bind the optional per-item snapshot heartbeat (issue #452)."""
        self._snapshot_progress_presentation = presentation

    def _pinned_model_profile(self) -> dict[str, str] | None:
        # The pin is what extraction asked for; the effective profile is what
        # the CLI reported. Both are kept so a mismatch is visible (#731).
        pin = getattr(self._agent, "model_pin", None)
        return pin.as_profile() if isinstance(pin, CodexModelPin) else None

    def _effective_model_profile(self) -> dict[str, str] | None:
        snapshot = getattr(self._agent, "effective_model_profile_snapshot", None)
        if callable(snapshot):
            value = snapshot()
            return dict(value) if isinstance(value, dict) else None
        profile = getattr(self._agent, "effective_model_profile", None)
        return dict(profile) if isinstance(profile, dict) else None

    def _record_quarantine_diagnostic(
        self,
        item: dict[str, Any],
        quarantine: QuarantineResult,
        attempt_number: int | None = None,
        *,
        diagnostic_class: str = "",
        raw_extracted_count: int | None = None,
        identity_mismatches: Sequence[Mapping[str, str]] = (),
    ) -> dict[str, Any]:
        return _record_quarantine_diagnostic(
            item,
            quarantine,
            attempt_number,
            diagnostic_class=diagnostic_class,
            raw_extracted_count=raw_extracted_count,
            identity_mismatches=identity_mismatches,
        )

    def _record_preservation_failure(
        self, artifact_path: Path, quarantine: QuarantineResult, failed_paths: list[str]
    ) -> bool:
        """Record that quarantine preservation failed for the rejected artifact.

        Never unlink or delete the rejected artifact. The bytes must stay
        untouched so the operator can inspect them. Returns True to signal
        that the weekly workflow must not reach repository finalization.
        """
        if quarantine.status is not QuarantineStatus.PRESERVATION_FAILED:
            return False
        failed_paths.append(str(artifact_path))
        return True

    def _is_previously_validated(
        self, output_dir: Path, source_url: str, expected_file: str, capture_file: str
    ) -> bool:
        """Check that the canonical bytes exactly match prior validation evidence.

        Identity fields alone are insufficient once digest evidence exists: a
        file may have been modified after the marker was written. Older markers
        without a digest are migrated in place on their first matching use.
        """
        artifact_path = output_dir / expected_file
        try:
            artifact_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        except OSError:
            return False
        marker = _read_resume_marker(output_dir)
        validated_artifacts = marker.get("validated_artifacts", [])
        identity = (source_url, expected_file, capture_file)
        matching = [
            va
            for va in validated_artifacts
            if isinstance(va, dict)
            and (
                va.get("source_url"),
                va.get("expected_extraction_file"),
                va.get("capture_file"),
            )
            == identity
        ]
        digest_evidence = [va for va in matching if va.get("artifact_sha256")]
        if digest_evidence:
            # Once digest evidence exists, a leftover legacy entry must not
            # weaken it and permit changed canonical bytes.
            return any(va.get("artifact_sha256") == artifact_sha256 for va in digest_evidence)
        if not matching:
            return False

        # Legacy markers predate byte digests. Their matching validation entry
        # is still authoritative for migration: preserve the canonical bytes
        # and atomically upgrade to one digest-bearing entry. If persistence of
        # that upgrade fails, preservation remains safer than quarantining a
        # previously validated canonical artifact.
        upgraded = dict(matching[-1])
        upgraded["artifact_sha256"] = artifact_sha256
        retained = [
            va
            for va in validated_artifacts
            if not (
                isinstance(va, dict)
                and (
                    va.get("source_url"),
                    va.get("expected_extraction_file"),
                    va.get("capture_file"),
                )
                == identity
            )
        ]
        marker["validated_artifacts"] = [*retained, upgraded]
        try:
            _write_resume_marker(output_dir, marker)
        except OSError:
            pass
        return True

    def _fulfill_segmented(
        self,
        output_dir: Path,
        items: list[dict[str, Any]],
        total_budget: float,
        started: float,
        snapshot: _ExtractionProgressSnapshot,
    ) -> dict[str, Any]:
        """Run first attempts in bounded admission segments, then retries."""
        deadline = started + total_budget
        diagnostics: list[dict[str, Any]] = []
        quarantine_preservation_failed = False
        failed_quarantine_paths: list[str] = []
        states: list[dict[str, Any]] = []

        for portfolio_sequence, item in enumerate(items, start=1):
            expected = str(item.get("expected_extraction_file", ""))
            capture_file = str(item.get("capture_file", ""))
            capture_path = output_dir / capture_file if capture_file else None
            artifact_path = output_dir / expected if expected else output_dir / ".missing"
            source_url = str(item.get("source_url", ""))
            present = bool(expected) and artifact_path.exists()
            previously_validated = (
                self._is_previously_validated(output_dir, source_url, expected, capture_file)
                if expected and capture_file
                else False
            )
            valid_now = present and self._validate_extraction_artifact(artifact_path, item, capture_path)
            if present and not valid_now:
                if previously_validated:
                    diagnostics.append(
                        {
                            "source_name": str(item.get("source_name", "")),
                            "status": "preserved_validated_artifact",
                            "artifact": expected,
                            "duration_seconds": 0.0,
                        }
                    )
                else:
                    observed_class, observed_count, observed_mismatches = _observed_rejection(
                        artifact_path, item, capture_path
                    )
                    quarantine = _quarantine_artifact(artifact_path)
                    diagnostics.append(
                        self._record_quarantine_diagnostic(
                            item,
                            quarantine,
                            diagnostic_class=observed_class,
                            raw_extracted_count=observed_count,
                            identity_mismatches=observed_mismatches,
                        )
                    )
                    quarantine_preservation_failed |= self._record_preservation_failure(
                        artifact_path, quarantine, failed_quarantine_paths
                    )
            elif valid_now:
                diagnostics.append(
                    {
                        "source_name": str(item.get("source_name", "")),
                        "status": "reused_validated_artifact",
                        "artifact": expected,
                        "duration_seconds": 0.0,
                    }
                )
            states.append(
                {
                    "item": item,
                    "expected": expected,
                    "capture_file": capture_file,
                    "capture_path": capture_path,
                    "artifact_path": artifact_path,
                    "artifact_was_present": present,
                    "eligible": bool(expected and capture_path is not None and capture_path.is_file()),
                    "valid": valid_now,
                    "retryable": True,
                    "timed_out": False,
                    "terminal": "validated"
                    if valid_now
                    else ("pending" if expected and capture_path else "unavailable"),
                    "duration": 0.0,
                    "submission_sequence": portfolio_sequence,
                    "worker_slot": None,
                    "progress_event_counts": {},
                    "no_progress_quiet_seconds": None,
                }
            )

        snapshot.sync_states(states, self._monotonic())
        deadline_aware = getattr(self._agent, "extract_with_timeout", None)
        shared_worker_count = self._max_workers if callable(deadline_aware) else 1
        required_retry_headroom = self._retry_headroom_seconds
        retry_admission: dict[str, Any] = {
            "required_headroom_seconds": required_retry_headroom,
            "candidate_count": 0,
            "admitted_count": 0,
            "refused_count": 0,
            "admitted_sources": [],
            "refused_sources": [],
            "admission_waves": 0,
        }
        portfolio_total = len(states)
        settled_timeouts = 0
        settled_items: set[int] = {id(state["item"]) for state in states if state["valid"] or not state["eligible"]}
        progress_sink_failed = False
        retries_used = 0

        def _emit_progress(retrying: int = 0) -> None:
            nonlocal progress_sink_failed
            if self._on_progress is None:
                return
            try:
                self._on_progress(len(settled_items), portfolio_total, settled_timeouts, retrying)
            except Exception as exc:
                if not progress_sink_failed:
                    progress_sink_failed = True
                    diagnostics.append(
                        {
                            "status": "progress_reporting_failed",
                            "diagnostic": type(exc).__name__,
                            "duration_seconds": 0.0,
                        }
                    )

        def _report_progress(completion: ExtractionRoundCompletion) -> None:
            nonlocal settled_timeouts
            snapshot.completion(completion, self._monotonic())
            key = id(completion.context.item)
            if key in settled_items:
                return
            settled_items.add(key)
            if _completion_was_bounded(completion.result):
                settled_timeouts += 1
            _emit_progress()

        def _projected_makespan(durations: list[float]) -> float:
            available = [0.0] * shared_worker_count
            for duration in durations:
                slot = min(range(shared_worker_count), key=lambda index: (available[index], index))
                available[slot] += max(0.0, duration)
            return max(available, default=0.0)

        def _run_round(
            round_states: list[dict[str, Any]],
            attempt_number: int,
            admission_deadline: float | None,
        ) -> None:
            nonlocal quarantine_preservation_failed, retries_used
            begin_round = getattr(self._agent, "begin_round", None)
            if callable(begin_round):
                begin_round([str(state["item"].get("source_name", "")) for state in round_states])
            state_by_identity = {id(state["item"]): state for state in round_states}

            def _run_shared(context: ExtractionRoundContext) -> dict[str, Any]:
                state = state_by_identity[id(context.item)]
                if context.token is None:
                    raise RuntimeError("shared extraction round token missing")
                return _run_attempt_local(
                    self,
                    state,
                    attempt_number,
                    context.effective_deadline,
                    context.token,
                    context=context,
                )

            def _boundary_shared(
                context: ExtractionRoundContext,
                stage: str,
                threshold: float,
                supplier: str,
            ) -> dict[str, Any]:
                return _round_boundary_local_result(
                    context,
                    stage=stage,
                    threshold=threshold,
                    supplier=supplier,
                    attempt_number=attempt_number,
                    clock=self._monotonic,
                    segment_mode=True,
                )

            def _exception_shared(context: ExtractionRoundContext, exc: BaseException) -> dict[str, Any]:
                return {
                    "started": context.started,
                    "cancel_token": context.token,
                    "preservation_failed": False,
                    "failed_path": "",
                    "diagnostics": [],
                    "staged_valid": False,
                    "elapsed": max(
                        0.0,
                        self._monotonic()
                        - (context.started_at if context.started_at is not None else context.submitted_at),
                    ),
                    "extraction": None,
                    "staging_path": None,
                    "_worker_exception": type(exc),
                }

            completions = execute_bounded_extraction_round(
                [state["item"] for state in round_states],
                worker_count=shared_worker_count,
                per_attempt_ceiling=self._per_attempt_timeout_seconds,
                portfolio_deadline=deadline,
                admission_deadline=admission_deadline,
                clock=self._monotonic,
                run=_run_shared,
                boundary=_boundary_shared,
                on_exception=_exception_shared,
                on_late_result=_shared_late_result_cleanup,
                on_completion=_report_progress,
                on_poll=snapshot.poll,
                thread_name_prefix="autoscout-codex-",
            )
            for completion in completions:
                state = state_by_identity[id(completion.context.item)]
                if completion.context.started:
                    state["worker_slot"] = completion.context.worker_slot
                if isinstance(completion.result, dict):
                    for diagnostic in completion.result.get("diagnostics", []):
                        if isinstance(diagnostic, dict):
                            diagnostic.setdefault("worker_slot", completion.context.worker_slot)
                            diagnostic["attempt_submission_sequence"] = completion.context.submission_sequence
                            diagnostic["submission_sequence"] = state["submission_sequence"]
                            diagnostic.setdefault("started", completion.context.started)
                outcome = _apply_local_result(completion.result, state, attempt_number)
                # Only an attempt after a source's first is a retry (Issue #1176).
                retries_used += int(bool(outcome["started"]) and attempt_number > 1)
                for diagnostic in outcome["diagnostics"]:
                    if isinstance(diagnostic, dict):
                        diagnostic["attempt_submission_sequence"] = completion.context.submission_sequence
                        diagnostic["submission_sequence"] = state["submission_sequence"]
                        diagnostic.setdefault(
                            "worker_slot", completion.context.worker_slot if completion.context.started else None
                        )
                        diagnostic.setdefault("started", completion.context.started)
                diagnostics.extend(outcome["diagnostics"])
                if outcome["preservation_failed"]:
                    quarantine_preservation_failed = True
                    failed_quarantine_paths.append(outcome["failed_path"])

        if settled_items:
            _emit_progress()

        segments: list[dict[str, Any]] = []
        segment_number = 0
        while not quarantine_preservation_failed:
            unfinished_first = [
                state for state in states if state["eligible"] and state["terminal"] in {"pending", "unfinished"}
            ]
            if not unfinished_first:
                break
            remaining = deadline - self._monotonic()
            if remaining < SEGMENT_BUDGET_SECONDS + required_retry_headroom - 1e-9:
                break
            segment_number += 1
            segment_started = self._monotonic()
            before_terminal = {id(state["item"]): state["terminal"] for state in unfinished_first}
            _run_round(unfinished_first, 1, segment_started + SEGMENT_BUDGET_SECONDS)
            terminal_count = sum(
                1
                for state in unfinished_first
                if before_terminal[id(state["item"])] in {"pending", "unfinished"}
                and state["terminal"] not in {"pending", "unfinished"}
            )
            unfinished_count = sum(
                1 for state in states if state["eligible"] and state["terminal"] in {"pending", "unfinished"}
            )
            segments.append(
                {
                    "segment": segment_number,
                    "duration_seconds": round(max(0.0, self._monotonic() - segment_started), 3),
                    "admitted_count": len(unfinished_first),
                    "terminal_count": terminal_count,
                    "unfinished_count": unfinished_count,
                }
            )
            for state in states:
                if state["terminal"] == "unfinished":
                    state["terminal"] = "pending"
            if terminal_count == 0:
                break

        unfinished_first = [state for state in states if state["eligible"] and state["terminal"] == "pending"]
        capacity_stop_count = len(unfinished_first)
        profile_snapshot = getattr(self._agent, "effective_model_profile_snapshot", None)
        if callable(profile_snapshot):
            effective_profile = profile_snapshot()
        else:
            profile = getattr(self._agent, "effective_model_profile", None)
            effective_profile = dict(profile) if isinstance(profile, dict) else None
        for state in unfinished_first:
            state["terminal"] = "portfolio_capacity_stop"
            diagnostics.append(
                {
                    "source_name": str(state["item"].get("source_name", "")),
                    "attempt": 1,
                    "submission_sequence": state["submission_sequence"],
                    "status": "portfolio_capacity_stop",
                    "diagnostic": "portfolio_capacity_stop",
                    "boundary_supplier": "segment_deadline",
                    "capacity_stop_count": capacity_stop_count,
                    "effective_model_profile": effective_profile,
                    "started": False,
                    "duration_seconds": 0.0,
                }
            )
            if id(state["item"]) not in settled_items:
                settled_items.add(id(state["item"]))
        if capacity_stop_count:
            _emit_progress()

        # Retry admission keeps the existing make-span rule, and runs only
        # after every first attempt has reached a terminal state. Retries are
        # admitted in waves: one that does not fit is costed again once the
        # admitted wave has finished, and refused only when nothing more fits
        # (Issue #732).
        def _retry_capacity() -> float:
            return max(0.0, deadline - self._monotonic()) - required_retry_headroom

        for attempt_number in range(2, self._max_attempts + 1):
            if quarantine_preservation_failed:
                break
            eligible = [state for state in states if _should_reattempt(state)]
            for state in eligible:
                # The allowance is spent here so a source that stays empty
                # cannot re-admit itself forever. Whether the retry actually
                # ran is a separate fact, recorded when execution starts.
                if state["valid"] and state.get("empty_extraction"):
                    state["empty_retry_used"] = True
                if state.get("terminal") == "no_progress_timeout":
                    state["no_progress_retry_used"] = True
            retry_admission["candidate_count"] += len(eligible)
            pending = eligible
            waves_run = 0
            while pending and not quarantine_preservation_failed:
                admitted, pending = _plan_retry_wave(
                    pending,
                    retry_capacity=_retry_capacity,
                    per_attempt_timeout=self._per_attempt_timeout_seconds,
                    projected_makespan=_projected_makespan,
                )
                if not admitted:
                    break
                for state in admitted:
                    _record_retry_admission(state, retry_admission)
                retry_admission["admission_waves"] += 1
                waves_run += 1
                snapshot.retry_pending(admitted, self._monotonic())
                _emit_progress(retrying=len(admitted))
                _run_round(admitted, attempt_number, None)
            if not quarantine_preservation_failed:
                for state in pending:
                    _record_retry_refusal(state, attempt_number, retry_admission, diagnostics)
            if not waves_run:
                break
            if attempt_number < self._max_attempts and any(_should_reattempt(state) for state in states):
                remaining = max(0.0, deadline - self._monotonic())
                if remaining > 0:
                    self._sleep(min(RETRY_BACKOFF_SECONDS, remaining))

        retry_admission["actual_headroom_seconds"] = deadline - self._monotonic()
        retry_admission["preserved_headroom"] = retry_admission["actual_headroom_seconds"] >= required_retry_headroom

        ready = 0
        failed = 0
        skipped = 0
        unfinished_items = 0
        execution_safety_stop_count = 0
        for state in states:
            if state["valid"]:
                ready += 1
                # The canonical fresh artifact is the durable completion fact.
                # Resume evidence is updated after each validated publication.
                resume = CodexResumeAdapter(output_dir)
                resume.mark_stage_complete(output_dir, "capture", str(state["item"].get("operation_id", "")))
                resume.mark_artifact_validated(
                    output_dir,
                    str(state["item"].get("source_url", "")),
                    state["expected"],
                    state["capture_file"],
                    str(state["item"].get("operation_id", "")),
                )
            elif state["terminal"] in {"portfolio_capacity_stop", "unfinished", "execution_safety_stop"}:
                unfinished_items += 1
                if state["terminal"] == "execution_safety_stop":
                    execution_safety_stop_count += 1
            elif (
                state["terminal"] == "invalid_output"
                or state["artifact_was_present"]
                or state["artifact_path"].exists()
            ):
                failed += 1
            else:
                skipped += 1

        progress_event_counts: dict[str, int] = {}
        quiet_values: list[float] = []
        for state in states:
            for event_type, count in state.get("progress_event_counts", {}).items():
                progress_event_counts[event_type] = progress_event_counts.get(event_type, 0) + int(count)
            quiet = state.get("no_progress_quiet_seconds")
            if isinstance(quiet, int | float):
                quiet_values.append(float(quiet))
        total_duration = round(max(0.0, self._monotonic() - started), 3)
        source_timings = [
            {
                "source_name": str(state["item"].get("source_name", "")),
                "duration_seconds": round(state["duration"], 3),
                "status": "ready" if state["valid"] else "not_ready",
                "terminal": state["terminal"],
                "submission_sequence": state["submission_sequence"],
                "worker_slot": state["worker_slot"],
            }
            for state in states
        ]
        _write_extraction_outcomes(output_dir, states, diagnostics)
        common = {
            "ready_items": ready,
            "failed_items": failed,
            "skipped_items": skipped,
            "unfinished_items": unfinished_items,
            "capacity_stop_count": capacity_stop_count,
            "execution_safety_stop_count": execution_safety_stop_count,
            "segment_count": len(segments),
            "segments": segments,
            "total_items": len(items),
            "retries_used": retries_used,
            "diagnostics": diagnostics,
            "duration_seconds": total_duration,
            "source_timings": source_timings,
            "runtime_budget_seconds": total_budget,
            "extraction_budget_seconds": total_budget,
            "per_attempt_timeout_seconds": self._per_attempt_timeout_seconds,
            "retry_admission": retry_admission,
            "progress_event_counts": progress_event_counts,
            "no_progress_quiet_seconds": max(quiet_values, default=None),
            "effective_model_profile": effective_profile,
            "pinned_model_profile": self._pinned_model_profile(),
        }
        if quarantine_preservation_failed:
            result = {
                "status": "failed",
                "reason": "quarantine_preservation_failed",
                **common,
                "failed_quarantine_paths": failed_quarantine_paths,
            }
        elif unfinished_items:
            result = {"status": "unfinished", "reason": "portfolio_capacity_stop", **common}
        elif ready == 0:
            result = {"status": "failed", "reason": "no_extraction_produced", **common}
        else:
            result = {"status": "ok", **common}
        snapshot.final(result, states, self._monotonic())
        return _assemble_extraction_result(result, len(items))

    def fulfill_additional(
        self, output_dir: Path, *, source_urls: set[str], extraction_budget_seconds: float
    ) -> dict[str, Any]:
        """Extract only the late batch, retaining the main batch's outcomes.

        Kirkus cannot stop the weekly run. A late capacity/safety stop is a
        named source failure, not a portfolio-wide finalization refusal.
        """
        path = output_dir / EXTRACTION_OUTCOMES_FILE
        previous = read_json(path, default={}).get("outcomes", [])
        try:
            result = self.fulfill(
                output_dir, extraction_budget_seconds=extraction_budget_seconds, source_urls=source_urls
            )
        finally:
            current = read_json(path, default={}).get("outcomes", [])
            late = [record for record in current if record.get("source_url") in source_urls]
            for record in late:
                if record.get("terminal_class") in {"unfinished", "portfolio_capacity_stop", "execution_safety_stop"}:
                    record["terminal_class"] = "unavailable"
            write_json(
                path,
                {"outcomes": [record for record in previous if record.get("source_url") not in source_urls] + late},
            )
        for timing in result.get("source_timings", []):
            if timing.get("status") == "not_ready":
                timing["terminal"] = "unavailable"
        return result

    def fulfill(
        self,
        output_dir: Path,
        *,
        extraction_budget_seconds: float | None = None,
        source_urls: set[str] | None = None,
    ) -> dict[str, Any]:
        worklist_path = output_dir / "codex-extraction-worklist.json"
        if not worklist_path.exists():
            return _assemble_extraction_result(
                {
                    "status": "failed",
                    "reason": "missing_worklist",
                    "ready_items": 0,
                    "failed_items": 0,
                    "skipped_items": 0,
                    "unfinished_items": 0,
                    "total_items": 0,
                },
                0,
            )

        worklist = read_json(worklist_path, default={})
        items = worklist.get("work_items", []) if isinstance(worklist, dict) else []
        if source_urls is not None and isinstance(items, list):
            items = [item for item in items if item.get("source_url") in source_urls]
        if not isinstance(items, list) or not items:
            return _assemble_extraction_result(
                {
                    "status": "failed",
                    "reason": "empty_worklist",
                    "ready_items": 0,
                    "failed_items": 0,
                    "skipped_items": 0,
                    "unfinished_items": 0,
                    "total_items": 0,
                },
                0,
            )

        total_budget = (
            float(extraction_budget_seconds)
            if extraction_budget_seconds is not None
            else float(self._runtime_budget_seconds)
        )
        started = self._monotonic()
        snapshot = _ExtractionProgressSnapshot(
            output_dir,
            items,
            started,
            self._monotonic,
            self._progress_snapshot_failure_recorder,
            presentation=self._snapshot_progress_presentation,
        )
        snapshot.initial(started)
        if total_budget >= SEGMENT_BUDGET_SECONDS + self._retry_headroom_seconds + 1e-9:
            return self._fulfill_segmented(output_dir, items, total_budget, started, snapshot)

        ready = 0
        failed = 0
        skipped = 0
        retries_used = 0
        diagnostics: list[dict[str, Any]] = []
        quarantine_preservation_failed = False
        failed_quarantine_paths: list[str] = []
        started = self._monotonic()
        deadline = started + total_budget
        states: list[dict[str, Any]] = []

        # Phase 0 — Validate/reuse existing results and detect previously
        # validated artifacts.  An artifact that validates against the
        # current work item / capture is reused regardless of the resume
        # marker.  Marker evidence only matters when an artifact is present
        # but *does not* validate against the current work item: the stale
        # previously-validated artifact is preserved and re-extraction
        # is attempted.
        for portfolio_sequence, item in enumerate(items, start=1):
            expected = str(item.get("expected_extraction_file", ""))
            capture_file = str(item.get("capture_file", ""))
            capture_path = output_dir / capture_file if capture_file else None
            artifact_path = output_dir / expected if expected else output_dir / ".missing"
            source_url = str(item.get("source_url", ""))
            present = bool(expected) and artifact_path.exists()
            previously_validated = (
                self._is_previously_validated(output_dir, source_url, expected, capture_file)
                if expected and capture_file
                else False
            )
            valid_now = present and self._validate_extraction_artifact(artifact_path, item, capture_path)
            if present and not valid_now:
                if previously_validated:
                    # Stale artifact from a prior run — preserve, will attempt
                    # fresh extraction.
                    diagnostics.append(
                        {
                            "source_name": str(item.get("source_name", "")),
                            "status": "preserved_validated_artifact",
                            "artifact": expected,
                            "duration_seconds": 0.0,
                        }
                    )
                else:
                    # Untrusted artifact — quarantine. Observe it first, so the
                    # record names the class that refused it rather than leaving
                    # the operator to re-derive it (Issue #386). Observation is
                    # read-only and must never block the quarantine, so a
                    # validator failure degrades to an unnamed class.
                    observed_class, observed_count, observed_mismatches = _observed_rejection(
                        artifact_path, item, capture_path
                    )
                    quarantine = _quarantine_artifact(artifact_path)
                    diagnostics.append(
                        self._record_quarantine_diagnostic(
                            item,
                            quarantine,
                            diagnostic_class=observed_class,
                            raw_extracted_count=observed_count,
                            identity_mismatches=observed_mismatches,
                        )
                    )
                    quarantine_preservation_failed |= self._record_preservation_failure(
                        artifact_path, quarantine, failed_quarantine_paths
                    )
            elif valid_now:
                diagnostics.append(
                    {
                        "source_name": str(item.get("source_name", "")),
                        "status": "reused_validated_artifact",
                        "artifact": expected,
                        "duration_seconds": 0.0,
                    }
                )
            states.append(
                {
                    "item": item,
                    "expected": expected,
                    "capture_file": capture_file,
                    "capture_path": capture_path,
                    "artifact_path": artifact_path,
                    "artifact_was_present": present,
                    "valid": valid_now,
                    "eligible": bool(expected and capture_path is not None and capture_path.is_file()),
                    "retryable": True,
                    "timed_out": False,
                    "terminal": "validated"
                    if valid_now
                    else ("pending" if expected and capture_path else "unavailable"),
                    "duration": 0.0,
                    "progress_event_counts": {},
                    "no_progress_quiet_seconds": None,
                    "submission_sequence": portfolio_sequence,
                    "worker_slot": None,
                }
            )

        snapshot.sync_states(states, self._monotonic())

        # Phase 1 — fair extraction rounds through the shared scheduler.  The
        # first round always completes before retry admission is evaluated.
        deadline_aware = getattr(self._agent, "extract_with_timeout", None)
        shared_worker_count = self._max_workers if callable(deadline_aware) else 1
        required_retry_headroom = self._retry_headroom_seconds
        retry_admission: dict[str, Any] = {
            "required_headroom_seconds": required_retry_headroom,
            "candidate_count": 0,
            "admitted_count": 0,
            "refused_count": 0,
            "admitted_sources": [],
            "refused_sources": [],
            "admission_waves": 0,
        }

        # Portfolio-level progress reporting. The extraction stage is the
        # longest stage in the weekly workflow, so it reports as each source
        # settles instead of staying silent until the whole stage returns
        # (issue #383). Counts are portfolio-wide and count each source once,
        # so a retry round never rewinds or inflates the reported progress.
        # Only counts cross this seam; the editor-facing wording belongs to
        # the presentation layer.
        portfolio_total = len(states)
        settled_timeouts = 0
        progress_sink_failed = False
        # A source that already holds a valid artifact, or that has no usable
        # capture, never enters a round and so never produces a completion.
        # Seed those here, otherwise the reported count can never reach the
        # portfolio total and the stage goes silent for the rest of its
        # runtime — the exact failure this reporting exists to remove.
        settled_items: set[int] = {id(state["item"]) for state in states if state["valid"] or not state["eligible"]}

        def _emit_progress(retrying: int = 0) -> None:
            # The swallow lives with the emitter, not with one of its callers.
            # Not every emit goes through the round's observer guard: the
            # baseline and the retry announce are emitted directly. The
            # production sink prints Russian text to a pipe, so a raise is not
            # hypothetical — a BrokenPipeError or a UnicodeEncodeError must
            # never end a 27-minute extraction stage because reporting failed.
            nonlocal progress_sink_failed
            if self._on_progress is None:
                return
            try:
                self._on_progress(len(settled_items), portfolio_total, settled_timeouts, retrying)
            except Exception as exc:
                # Record the first failure once. Swallowing silently would make a
                # wiring bug — a sink that cannot accept the counts it is given —
                # look exactly like a working stage that reports nothing.
                if not progress_sink_failed:
                    progress_sink_failed = True
                    diagnostics.append(
                        {
                            "status": "progress_reporting_failed",
                            "diagnostic": type(exc).__name__,
                            "duration_seconds": 0.0,
                        }
                    )

        if settled_items:
            _emit_progress()

        def _report_progress(completion: ExtractionRoundCompletion) -> None:
            nonlocal settled_timeouts
            snapshot.completion(completion, self._monotonic())
            if self._on_progress is None:
                return
            key = id(completion.context.item)
            if key in settled_items:
                return
            settled_items.add(key)
            if _completion_was_bounded(completion.result):
                settled_timeouts += 1
            _emit_progress()

        def _projected_makespan(durations: list[float]) -> float:
            available = [0.0] * shared_worker_count
            for duration in durations:
                slot = min(range(shared_worker_count), key=lambda index: (available[index], index))
                available[slot] += max(0.0, duration)
            return max(available, default=0.0)

        def _execute_round(eligible_this_round: list[dict[str, Any]], attempt_number: int) -> None:
            nonlocal retries_used, quarantine_preservation_failed
            if attempt_number > 1:
                snapshot.retry_pending(eligible_this_round, self._monotonic())
                # The portfolio count only moves on a source's first settled
                # result, so without this the stage would fall silent for the
                # whole retry round after the count had already filled.
                _emit_progress(retrying=len(eligible_this_round))

            begin_round = getattr(self._agent, "begin_round", None)
            if callable(begin_round):
                begin_round([str(state["item"].get("source_name", "")) for state in eligible_this_round])
            state_by_identity = {id(state["item"]): state for state in eligible_this_round}
            current_attempt = attempt_number
            current_states = state_by_identity

            def _run_shared(
                context: ExtractionRoundContext,
                states_by_identity: dict[int, dict[str, Any]] = current_states,
                attempt: int = current_attempt,
            ) -> dict[str, Any]:
                state = states_by_identity[id(context.item)]
                if context.token is None:
                    raise RuntimeError("shared extraction round token missing")
                return _run_attempt_local(
                    self,
                    state,
                    attempt,
                    context.effective_deadline,
                    context.token,
                    context=context,
                )

            def _boundary_shared(
                context: ExtractionRoundContext,
                stage: str,
                threshold: float,
                _supplier: str,
                attempt: int = current_attempt,
            ) -> dict[str, Any]:
                return _round_boundary_local_result(
                    context,
                    stage=stage,
                    threshold=threshold,
                    supplier=_supplier,
                    attempt_number=attempt,
                    clock=self._monotonic,
                )

            def _exception_shared(context: ExtractionRoundContext, exc: BaseException) -> dict[str, Any]:
                return {
                    "started": context.started,
                    "cancel_token": context.token,
                    "preservation_failed": False,
                    "failed_path": "",
                    "diagnostics": [],
                    "staged_valid": False,
                    "elapsed": max(
                        0.0,
                        self._monotonic()
                        - (context.started_at if context.started_at is not None else context.submitted_at),
                    ),
                    "extraction": None,
                    "staging_path": None,
                    "_worker_exception": type(exc),
                }

            round_completions = execute_bounded_extraction_round(
                [state["item"] for state in eligible_this_round],
                worker_count=shared_worker_count,
                per_attempt_ceiling=self._per_attempt_timeout_seconds,
                portfolio_deadline=deadline,
                clock=self._monotonic,
                run=_run_shared,
                boundary=_boundary_shared,
                on_exception=_exception_shared,
                on_late_result=_shared_late_result_cleanup,
                on_completion=_report_progress,
                on_poll=snapshot.poll,
                thread_name_prefix="autoscout-codex-",
            )
            round_outcomes: list[dict[str, Any]] = []
            for completion in round_completions:
                state = state_by_identity[id(completion.context.item)]
                if completion.context.started:
                    state["worker_slot"] = completion.context.worker_slot
                if isinstance(completion.result, dict):
                    for diagnostic in completion.result.get("diagnostics", []):
                        if isinstance(diagnostic, dict):
                            diagnostic.setdefault("worker_slot", completion.context.worker_slot)
                            diagnostic["attempt_submission_sequence"] = completion.context.submission_sequence
                            diagnostic["submission_sequence"] = state["submission_sequence"]
                            diagnostic.setdefault("started", completion.context.started)
                outcome = _apply_local_result(completion.result, state, current_attempt)
                round_outcomes.append(outcome)
                # Only an attempt after a source's first is a retry (Issue #1176).
                retries_used += int(bool(outcome["started"]) and current_attempt > 1)
                for diagnostic in outcome["diagnostics"]:
                    if isinstance(diagnostic, dict):
                        diagnostic["attempt_submission_sequence"] = completion.context.submission_sequence
                        diagnostic["submission_sequence"] = state["submission_sequence"]
                        diagnostic.setdefault(
                            "worker_slot", completion.context.worker_slot if completion.context.started else None
                        )
                        diagnostic.setdefault("started", completion.context.started)
                diagnostics.extend(outcome["diagnostics"])
                if outcome["preservation_failed"]:
                    quarantine_preservation_failed = True
                    failed_quarantine_paths.append(outcome["failed_path"])

        def _retry_capacity() -> float:
            return max(0.0, deadline - self._monotonic()) - required_retry_headroom

        for attempt_number in range(1, self._max_attempts + 1):
            if quarantine_preservation_failed:
                break
            eligible_this_round = [state for state in states if _should_reattempt(state)]
            for state in eligible_this_round:
                # The allowance is spent here so a source that stays empty
                # cannot re-admit itself forever. Whether the retry actually
                # ran is a separate fact, recorded when execution starts.
                if state["valid"] and state.get("empty_extraction"):
                    state["empty_retry_used"] = True
                if state.get("terminal") == "no_progress_timeout":
                    state["no_progress_retry_used"] = True
            if attempt_number == 1:
                if not eligible_this_round:
                    break
                _execute_round(eligible_this_round, attempt_number)
            else:
                # Retries are admitted in waves. One that does not fit is
                # costed again once the admitted wave has finished, and is
                # refused only when nothing more fits (Issue #732).
                retry_admission["candidate_count"] += len(eligible_this_round)
                pending = eligible_this_round
                waves_run = 0
                while pending and not quarantine_preservation_failed:
                    admitted, pending = _plan_retry_wave(
                        pending,
                        retry_capacity=_retry_capacity,
                        per_attempt_timeout=self._per_attempt_timeout_seconds,
                        projected_makespan=_projected_makespan,
                    )
                    if not admitted:
                        break
                    for state in admitted:
                        _record_retry_admission(state, retry_admission)
                    retry_admission["admission_waves"] += 1
                    waves_run += 1
                    _execute_round(admitted, attempt_number)
                if not quarantine_preservation_failed:
                    for state in pending:
                        _record_retry_refusal(state, attempt_number, retry_admission, diagnostics)
                if not waves_run:
                    break
            if attempt_number < self._max_attempts and any(_should_reattempt(state) for state in states):
                remaining = max(0.0, deadline - self._monotonic())
                if remaining > 0:
                    self._sleep(min(RETRY_BACKOFF_SECONDS, remaining))

        retry_admission["actual_headroom_seconds"] = deadline - self._monotonic()
        retry_admission["preserved_headroom"] = retry_admission["actual_headroom_seconds"] >= required_retry_headroom

        # Finalization sees only the output directory, and a refused artifact has
        # been moved out of it. Without this record the two very different
        # endings — "an artifact was refused" and "no artifact ever arrived" —
        # are indistinguishable on disk, which is how a rejection came to be
        # reported as an absence (Issue #386).
        _write_extraction_outcomes(output_dir, states, diagnostics)

        unfinished_items = 0
        execution_safety_stop_count = 0
        for state in states:
            item = state["item"]
            if state["valid"]:
                ready += 1
                resume = CodexResumeAdapter(output_dir)
                # Idempotent for the current operation and rotates stale marker
                # identity if fulfilment follows a newly bound worklist.
                resume.mark_stage_complete(output_dir, "capture", str(item.get("operation_id", "")))
                resume.mark_artifact_validated(
                    output_dir,
                    str(item.get("source_url", "")),
                    state["expected"],
                    state["capture_file"],
                    str(item.get("operation_id", "")),
                )
            elif state.get("terminal") in {"portfolio_capacity_stop", "unfinished", "execution_safety_stop"}:
                unfinished_items += 1
                if state.get("terminal") == "execution_safety_stop":
                    execution_safety_stop_count += 1
            elif (
                state.get("terminal") == "invalid_output"
                or state["artifact_was_present"]
                or state["artifact_path"].exists()
            ):
                failed += 1
            else:
                skipped += 1

        total_duration = round(max(0.0, self._monotonic() - started), 3)
        source_timings = [
            {
                "source_name": str(state["item"].get("source_name", "")),
                "duration_seconds": round(state["duration"], 3),
                "status": "ready" if state["valid"] else "not_ready",
                "terminal": state.get("terminal", "pending"),
                "submission_sequence": state["submission_sequence"],
                "worker_slot": state["worker_slot"],
            }
            for index, state in enumerate(states)
        ]
        progress_event_counts: dict[str, int] = {}
        quiet_values: list[float] = []
        for state in states:
            for event_type, count in state.get("progress_event_counts", {}).items():
                progress_event_counts[event_type] = progress_event_counts.get(event_type, 0) + int(count)
            quiet = state.get("no_progress_quiet_seconds")
            if isinstance(quiet, int | float):
                quiet_values.append(float(quiet))

        common_compat = {
            "ready_items": ready,
            "failed_items": failed,
            "skipped_items": skipped,
            "unfinished_items": unfinished_items,
            "total_items": len(items),
            "retries_used": retries_used,
            "diagnostics": diagnostics,
            "duration_seconds": total_duration,
            "source_timings": source_timings,
            "runtime_budget_seconds": total_budget,
            "extraction_budget_seconds": total_budget,
            "per_attempt_timeout_seconds": self._per_attempt_timeout_seconds,
            "retry_admission": retry_admission,
            "capacity_stop_count": 0,
            "execution_safety_stop_count": execution_safety_stop_count,
            "segment_count": 0,
            "segments": [],
            "progress_event_counts": progress_event_counts,
            "no_progress_quiet_seconds": max(quiet_values, default=None),
            "effective_model_profile": self._effective_model_profile(),
            "pinned_model_profile": self._pinned_model_profile(),
        }
        if quarantine_preservation_failed:
            result = {
                "status": "failed",
                "reason": "quarantine_preservation_failed",
                **common_compat,
                "failed_quarantine_paths": failed_quarantine_paths,
            }
        elif unfinished_items:
            result = {
                "status": "unfinished",
                "reason": "execution_safety_stop" if execution_safety_stop_count else "portfolio_capacity_stop",
                **common_compat,
            }
        elif ready == 0:
            result = {"status": "failed", "reason": "no_extraction_produced", **common_compat}
        else:
            result = {"status": "ok", **common_compat}
        snapshot.final(result, states, self._monotonic())
        return _assemble_extraction_result(result, len(items))

    def validate_extraction_artifact(self, path: Path, item: dict[str, Any], capture_path: Path | None = None) -> bool:
        """Validate a staged artifact without publishing or changing resume state."""
        return self._validate_extraction_artifact(path, item, capture_path)

    def _validate_extraction_artifact(self, path: Path, item: dict[str, Any], capture_path: Path | None = None) -> bool:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        if not isinstance(data, dict):
            return False
        source_name = str(item.get("source_name", ""))
        source_url = str(item.get("source_url", ""))
        operation_id = str(item.get("operation_id", ""))
        capture_sha256 = str(item.get("capture_sha256", ""))
        if not all((source_name, source_url, operation_id, capture_sha256)):
            return False
        identities = {
            "source_name": source_name,
            "source_url": source_url,
            "operation_id": operation_id,
            "capture_sha256": capture_sha256,
        }
        if any(data.get(key) != value for key, value in identities.items()):
            return False
        if capture_path is None or not capture_path.is_file():
            return False

        if "entries" in data:
            entries = data["entries"]
        elif "mentions" in data:
            entries = data["mentions"]
        else:
            return False
        if not isinstance(entries, list):
            return False

        from autoscout.source_collectors import FORBIDDEN_COLLECTOR_RESPONSE_FIELDS
        from autoscout.source_collectors import _validate_codex_raw_required_fields
        from autoscout.source_collectors import codex_provenance_links
        from autoscout.source_collectors import evidence_url_verified
        from autoscout.source_collectors import normalize_codex_provenance_url

        if any(isinstance(entry, dict) and FORBIDDEN_COLLECTOR_RESPONSE_FIELDS & set(entry) for entry in entries):
            return False

        valid_entries, dropped = _validate_codex_raw_required_fields(
            entries, author_optional=work_item_author_exempt(item)
        )
        if dropped and (is_bookmarks_source_identity(item) or not valid_entries):
            return False

        try:
            loaded_capture = json.loads(capture_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        if not isinstance(loaded_capture, dict):
            return False
        captured_url = str(loaded_capture.get("source_url") or loaded_capture.get("source_page") or "")
        if captured_url != source_url:
            return False
        if hashlib.sha256(capture_path.read_bytes()).hexdigest() != capture_sha256:
            return False
        page_data: dict[str, Any] = loaded_capture

        # An empty artifact from a capture whose rows code can count is an
        # extraction that abstained, not an empty page (Issues #401, #740).
        if not entries and capture_has_listing_rows(item, page_data):
            return False

        verified_links = codex_provenance_links(page_data, item)
        for entry in valid_entries:
            if not isinstance(entry, dict):
                continue
            if not _matches_exact_list_metadata(entry, item):
                return False
            evidence_url = str(entry.get("evidence_url", "")).strip()
            entry_source_url = str(entry.get("source_url", "")).strip()
            if not evidence_url or not entry_source_url:
                return False
            resolved_evidence_url = normalize_codex_provenance_url(evidence_url, item)
            resolved_source_url = normalize_codex_provenance_url(entry_source_url, item)
            if resolved_evidence_url is None or resolved_source_url is None:
                return False
            if not (
                resolved_evidence_url.startswith(("http://", "https://"))
                and resolved_source_url.startswith(("http://", "https://"))
            ):
                return False
            evidence_verified, _reason = evidence_url_verified(page_data, resolved_evidence_url, links=verified_links)
            source_verified, _reason = evidence_url_verified(page_data, resolved_source_url, links=verified_links)
            if not evidence_verified or not source_verified:
                return False

        return True


class CodexResumeAdapter:
    def __init__(self, output_dir: Path) -> None:
        self._output_dir = output_dir

    def can_resume(self, output_dir: Path) -> bool:
        marker = _read_resume_marker(output_dir)
        if not marker:
            return False

        worklist_path = output_dir / "codex-extraction-worklist.json"
        if not worklist_path.exists():
            return False

        completed_stages = marker.get("completed_stages", [])
        return "capture" in completed_stages

    def resume_worklist(self, output_dir: Path, run_id: str) -> list[SourceExtractionItem]:
        marker = _read_resume_marker(output_dir)
        marker_run_id = marker.get("run_id", "")
        if marker_run_id and marker_run_id != run_id:
            return []

        worklist_path = output_dir / "codex-extraction-worklist.json"
        if not worklist_path.exists():
            return []

        worklist = read_json(worklist_path, default={})
        items = worklist.get("work_items", [])

        work_items = []
        for item in items:
            source_name = str(item.get("source_name", ""))
            source_url = str(item.get("source_url", ""))
            capture_file = str(item.get("capture_file", ""))
            expected_file = str(item.get("expected_extraction_file", ""))

            validated_artifacts = marker.get("validated_artifacts", [])
            already_validated = any(
                isinstance(va, dict)
                and va.get("source_url") == source_url
                and va.get("expected_extraction_file") == expected_file
                and va.get("capture_file") == capture_file
                and va.get("run_id") == marker_run_id
                for va in validated_artifacts
            )

            if already_validated and expected_file and (output_dir / expected_file).exists():
                from autoscout.codex_recovery import ProductionCodexAdapter as _P

                adapter = _P()
                capture_path = output_dir / capture_file if capture_file else None
                if adapter._validate_extraction_artifact(output_dir / expected_file, item, capture_path):
                    status = "completed"
                else:
                    status = "pending"
            else:
                status = "pending"

            work_items.append(
                SourceExtractionItem(
                    source_name=source_name,
                    source_url=source_url,
                    source_family=str(item.get("source_family", "")),
                    capture_file=capture_file,
                    expected_extraction_file=expected_file,
                    source_metadata=item.get("source_metadata", {}),
                    status=status,
                )
            )

        return work_items

    def mark_stage_complete(self, output_dir: Path, stage: str, run_id: str) -> None:
        marker = _read_resume_marker(output_dir)
        if not marker:
            marker = {"run_id": run_id, "completed_stages": [], "validated_artifacts": []}
        elif marker.get("run_id") != run_id:
            # A fresh capture starts a new resumable operation. Keep prior
            # artifact evidence so its exact canonical bytes can be protected
            # while replacements are produced, but never carry stage completion
            # across operation identities.
            marker = {
                "run_id": run_id,
                "completed_stages": [],
                "validated_artifacts": list(marker.get("validated_artifacts", [])),
            }

        completed = list(marker.get("completed_stages", []))
        if stage not in completed:
            completed.append(stage)
        marker["completed_stages"] = completed
        marker["run_id"] = run_id

        _write_resume_marker(output_dir, marker)

    def mark_artifact_validated(
        self,
        output_dir: Path,
        source_url: str,
        expected_extraction_file: str,
        capture_file: str,
        run_id: str,
    ) -> None:
        marker = _read_resume_marker(output_dir)
        if not marker:
            return
        # Refuse to attach evidence for one operation to another operation's
        # resume identity. The capture path normally rotates the marker first.
        if marker.get("run_id") != run_id:
            return
        artifact_path = output_dir / expected_extraction_file
        try:
            artifact_sha256 = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        except OSError:
            return
        validated = list(marker.get("validated_artifacts", []))
        entry = {
            "source_url": source_url,
            "expected_extraction_file": expected_extraction_file,
            "capture_file": capture_file,
            "run_id": run_id,
            "artifact_sha256": artifact_sha256,
        }
        # Revalidation replaces evidence for this canonical artifact across
        # all operations instead of growing the marker every week.
        identity = (source_url, expected_extraction_file, capture_file)
        validated = [
            va
            for va in validated
            if not (
                isinstance(va, dict)
                and (
                    va.get("source_url"),
                    va.get("expected_extraction_file"),
                    va.get("capture_file"),
                )
                == identity
            )
        ]
        validated.append(entry)
        marker["validated_artifacts"] = validated
        _write_resume_marker(output_dir, marker)
