"""Offline description adjudication preview and apply.

Adjudication is one decision: does a public web page really describe this
book? This module holds the adapter seam, the exact response contract, a
deterministic replay implementation, the production Codex CLI adapter, citation
verification in code, and the standalone ``adjudicate-descriptions`` command.
The command selects
candidates with the existing backfill work-item helper and always writes a
complete preview artifact. Without ``apply`` it changes no Candidate History
record; with ``apply`` it writes accepted descriptions through the existing
description apply gate, and through no other path. Verification re-fetches
every cited page with the existing bounded HTTP fetcher; tests inject a
fake transport so the suite stays offline. The production adapter is never
reached by the default command path or the offline fixture run: the replay
adapter answers there, and the Codex adapter must be wired explicitly.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from urllib.parse import urlsplit

from autoscout.bounded_workers import DaemonThreadPool
from autoscout.codex_model_pin import ModelPinError
from autoscout.codex_model_pin import load_adjudication_model_pin
from autoscout.content_description import DESCRIPTION_AGENT_UNVERIFIED_PROVENANCE
from autoscout.content_description import DESCRIPTION_FALLBACK
from autoscout.content_description import DESCRIPTION_NOT_YET_PUBLISHED_DATE_FIELD
from autoscout.content_description import DESCRIPTION_SINGLE_SOURCE_STATUS
from autoscout.content_description import DESCRIPTION_UNCONFIRMED_STATUS
from autoscout.content_description import MAX_SEARCH_HINT_URL_LENGTH
from autoscout.content_description import MAX_SEARCH_HINTS
from autoscout.content_description import _bounded_search_hints
from autoscout.content_description import _prov_string
from autoscout.content_description import apply_description_artifacts
from autoscout.content_description import description_provenance_record
from autoscout.content_description import has_nonfallback_description
from autoscout.content_description import is_unsourced_stub
from autoscout.content_description import not_yet_published_date
from autoscout.content_description import prepare_backfill_work_items
from autoscout.content_description import record_has_usable_description
from autoscout.content_description import validate_content_description
from autoscout.description_depth import meets_description_depth
from autoscout.description_diagnostics import call_latency_summary
from autoscout.description_enrichment import _encode_work_items
from autoscout.description_projection import project_history_onto_snapshots
from autoscout.description_repair import REPAIR_EVIDENCE_SHORTFALL_REASONS
from autoscout.description_repair import is_repair_candidate
from autoscout.description_repair import record_repair_attempt
from autoscout.description_repair import repair_sort_key
from autoscout.description_repair import select_due_repairs
from autoscout.enrichment_lifecycle import BAND_PREVIOUSLY_SEEN
from autoscout.enrichment_lifecycle import band_rank
from autoscout.enrichment_lifecycle import lifecycle_band
from autoscout.grounded_description import GROUNDING_STAGE_ARTIFACT_NAME
from autoscout.grounded_description import MAX_FETCHES_PER_CANDIDATE
from autoscout.grounded_description import OUTCOME_ATTEMPTED_NO_DESCRIPTION
from autoscout.grounded_description import OUTCOME_EVIDENCE_UNCHANGED
from autoscout.grounded_description import OUTCOME_GROUNDED
from autoscout.grounded_description import OUTCOME_NOT_ATTEMPTED
from autoscout.grounded_description import OUTCOME_SINGLE_SOURCE
from autoscout.grounded_description import REASON_AMBIGUITY
from autoscout.grounded_description import REASON_AUTHOR_MISMATCH
from autoscout.grounded_description import REASON_AUTHOR_UNVERIFIABLE as _GROUNDED_REASON_AUTHOR_UNVERIFIABLE
from autoscout.grounded_description import REASON_EVIDENCE_UNCHANGED
from autoscout.grounded_description import REASON_FEWER_THAN_TWO
from autoscout.grounded_description import REASON_INSUFFICIENT_DEPTH
from autoscout.grounded_description import REASON_INVALID_DESCRIPTION
from autoscout.grounded_description import REASON_INVALID_SOURCE
from autoscout.grounded_description import REASON_NOT_ATTEMPTED_STAGE_BUDGET
from autoscout.grounded_description import REASON_NOT_YET_PUBLISHED
from autoscout.grounded_description import REASON_SEARCH_OR_FETCH_FAILURE
from autoscout.grounded_description import REASON_SOURCE_DENYLISTED
from autoscout.grounded_description import REASON_TITLE_MISMATCH
from autoscout.grounded_description import BoundedHTTPError
from autoscout.grounded_description import BoundedHTTPFetcher
from autoscout.grounded_description import SourceClassifier
from autoscout.grounded_description import _author_keys
from autoscout.grounded_description import _normalized_text
from autoscout.grounded_description import _normalized_token_text
from autoscout.grounded_description import canonicalize_hostname
from autoscout.grounded_description import canonicalize_url
from autoscout.grounded_description import committed_source_classifier
from autoscout.grounded_description import page_prints_author
from autoscout.grounded_description import page_prints_author_surname
from autoscout.grounded_description import page_prints_isbn
from autoscout.grounded_description import plausible_isbn
from autoscout.identity import identity_isbn
from autoscout.identity import normalize_work_title
from autoscout.json_io import read_json_or_default
from autoscout.json_io import write_json
from autoscout.model_client import CodexCliClient
from autoscout.model_client import ModelCallStatus
from autoscout.model_client import ModelRequest
from autoscout.pm_publication import is_pm_url
from autoscout.progress_snapshot import CandidateProgressSnapshot
from autoscout.progress_snapshot import ProgressSnapshotFailureRecorder
from autoscout.progress_snapshot import ProgressSnapshotPresentation
from autoscout.source_owner import TIER_DENYLIST
from autoscout.state_store import Collection
from autoscout.state_store import StateStore
from autoscout.state_store import open_state_store

if TYPE_CHECKING:
    from autoscout.weekly_workflow import DescriptionAdjudicationAdapter

ADJUDICATION_PREVIEW_ARTIFACT_NAME = "description-adjudication-preview.json"

ADJUDICATION_STAGE_ARTIFACT_NAME = "description-adjudication-stage.json"

ADJUDICATION_PROVENANCE = "agent-adjudicated"

# The agent wrote a finished description and was sure of it, but no cited
# page passed verification. The text still replaces a title-derived stub or
# the placeholder, under its own provenance and the review's unconfirmed
# warning, instead of being discarded (issue #791).
ADJUDICATION_UNVERIFIED_PROVENANCE = DESCRIPTION_AGENT_UNVERIFIED_PROVENANCE
ADJUDICATION_UNVERIFIED_CONFIDENCE = "high"

# Bounded rejection reasons travel into the preview artifact and stay small.
MAX_ADJUDICATION_REASON_LENGTH = 200

# Identity notes stay visible when a page is about a different work, but
# they stay bounded like every other retained string.
MAX_ADJUDICATION_IDENTITY_NOTE_LENGTH = 200

# The adapter response is untrusted text. Every field of it that reaches the
# preview artifact is bounded here, so no unbounded adapter string is ever
# retained.
MAX_ADJUDICATION_RESPONSE_TEXT_LENGTH = 2000
MAX_ADJUDICATION_CITATION_TEXT_LENGTH = 300
MAX_ADJUDICATION_RETAINED_CITATIONS = 20

# Grounded search hints (issue #684) are URL-only starting points the
# adjudication agent may begin from, never evidence. The count mirrors the
# grounded stage's per-candidate fetch budget; the length matches the shared
# URL bound. Hints never bypass verification: every cited page is still
# re-fetched and checked in code.
MAX_ADJUDICATION_SEARCH_HINTS = 3
MAX_ADJUDICATION_SEARCH_HINT_URL_LENGTH = 500
assert MAX_ADJUDICATION_SEARCH_HINTS == MAX_FETCHES_PER_CANDIDATE
assert MAX_ADJUDICATION_SEARCH_HINTS == MAX_SEARCH_HINTS
assert MAX_ADJUDICATION_SEARCH_HINT_URL_LENGTH == MAX_SEARCH_HINT_URL_LENGTH

CONFIDENCE_VALUES = ("high", "medium", "low")

# Planned per-candidate outcomes. ``INVALID`` means the response failed the
# schema and never reached verification. ``FALLBACK`` leaves the record at
# its fallback text with a bounded reason. ``SKIPPED`` means an explicitly
# named candidate needed no description work, so the adapter was never
# consulted and the record was never touched. ``EVIDENCE_UNCHANGED`` reuses
# the existing deterministic-stage outcome name: every retained cited page
# still fetches with the same digest, so the agent is not called and the
# text is not changed (issue #639). ``NOT_ATTEMPTED`` reuses the existing
# deterministic-stage outcome name for the stage clock: the remaining stage
# budget could not fit one more call, so the candidate was never attempted
# and is reported with ``not_attempted_stage_budget`` rather than dropped
# (issue #651). No new outcome name is added.
# A cited page that names no author at all cannot confirm or refute the
# printed author the adapter claimed, so the citation is dropped as
# unverifiable. ``author_mismatch`` stays what it says: the page named an
# author, and it was a different one (issue #649). The value is shared with
# ``grounded_description`` so the Russian label lives in one place.
REASON_AUTHOR_UNVERIFIABLE = _GROUNDED_REASON_AUTHOR_UNVERIFIABLE
assert REASON_AUTHOR_UNVERIFIABLE == "author_unverifiable"

# The adjudication worker count is its own constant. It is not imported
# from the grounded stage: that number was chosen for a stage whose unit of
# work is an HTTP fetch, and this stage's unit of work is a 90 to 135 second
# subprocess against a metered account. At 3 workers the 92-candidate pass
# measured on 2026-09-04 costs about 57 minutes; at 5 it costs about
# 34 minutes. Raising it is a separate decision that needs its own
# measurement, because every worker is one more concurrent ``codex exec``
# against the same account (issue #651).
ADJUDICATION_MAX_WORKERS = 3

# How many times verification fetches one cited page before dropping it as
# unfetchable (issue #791).
ADJUDICATION_VERIFICATION_FETCH_ATTEMPTS = 2

# The stage clock default. The 2026-09-04 dry run measured about 111 s per
# candidate; at 3 workers the 92-candidate worklist from the 2026-09-03
# weekly run finishes in about 3441 s. A one-hour budget does fit that
# mean, but only just: it leaves 159 s of slack, which is less than the
# 240 s a single call may take, so the budget was one slow candidate away
# from cutting the tail. It is also sharply sensitive to drift, because
# admission stops a whole call-timeout before the deadline:
#
#     mean 111 s -> 0 candidates unattempted
#     mean 120 s -> 5 candidates unattempted
#     mean 135 s -> 17 candidates unattempted
#
# 135 s is a real measurement, not a margin: it is what the hardest
# candidate in that run cost. Ninety minutes absorbs all three cases and
# still bounds the stage. A candidate that is admitted and then times out
# is already a bounded per-candidate failure and needs no new handling
# (issue #651).
ADJUDICATION_STAGE_BUDGET_SECONDS = 5400.0

DECISION_APPLY_TWO_SOURCE = "apply_two_source"
DECISION_APPLY_SINGLE_SOURCE = "apply_single_source"
DECISION_APPLY_UNVERIFIED = "apply_unverified"
DECISION_FALLBACK = "fallback"
DECISION_INVALID = "invalid"
DECISION_SKIPPED = "skipped"
DECISION_EVIDENCE_UNCHANGED = OUTCOME_EVIDENCE_UNCHANGED
DECISION_NOT_ATTEMPTED = OUTCOME_NOT_ATTEMPTED

ADJUDICATION_DECISIONS = (
    DECISION_APPLY_TWO_SOURCE,
    DECISION_APPLY_SINGLE_SOURCE,
    DECISION_APPLY_UNVERIFIED,
    DECISION_FALLBACK,
    DECISION_INVALID,
    DECISION_SKIPPED,
    DECISION_EVIDENCE_UNCHANGED,
    DECISION_NOT_ATTEMPTED,
)

# The decisions that carry an accepted description into the apply gate.
ADJUDICATION_APPLY_DECISIONS = (
    DECISION_APPLY_TWO_SOURCE,
    DECISION_APPLY_SINGLE_SOURCE,
    DECISION_APPLY_UNVERIFIED,
)

# Mapping from the eight adjudication decisions to the four exclusive
# classifications the grounded description report declares (issue #641).
# ``applied`` means the stage wrote a description through the gate.
# ``failed`` means the adapter answer could not be used at all.
# ``ambiguous`` means the stage settled the candidate at fallback with a
# reason the editor can act on. ``skipped`` means the agent was never
# consulted for this candidate in this run. ``still_fallback`` is not a
# fifth classification: it overlaps the four and counts every record whose
# final state is the standard fallback, whatever its classification.
ADJUDICATION_DECISION_CLASSIFICATION: dict[str, str] = {
    DECISION_APPLY_TWO_SOURCE: "applied",
    DECISION_APPLY_SINGLE_SOURCE: "applied",
    DECISION_APPLY_UNVERIFIED: "applied",
    DECISION_INVALID: "failed",
    DECISION_FALLBACK: "ambiguous",
    DECISION_SKIPPED: "skipped",
    DECISION_EVIDENCE_UNCHANGED: "skipped",
    DECISION_NOT_ATTEMPTED: "skipped",
}

ADJUDICATION_CLASSIFICATION_COUNTS = ("applied", "skipped", "failed", "ambiguous")

# The adjudication stage never started its agent check when the stage clock
# could not fit one more call. The shared ``not_attempted_stage_budget``
# label names the source search, which did run for this stage, so the
# adjudication narrative uses its own true label (issue #641).
ADJUDICATION_REASON_NOT_ATTEMPTED_LABEL = "Проверка описаний агентом еще не запускалась"

SURFACE_KINDS = (
    "publisher",
    "library_catalogue",
    "trade_review",
    "press",
    "retail_listing",
    "other",
)

# Exact contract from the working prototype call. Strict rule: every key in
# ``properties`` also appears in ``required``, so an optional field is
# present and empty, never absent.
ADJUDICATION_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "identity_key",
        "resolved",
        "unresolved_reason",
        "confidence",
        "content_description",
        "citations",
        "english_description",
        "unresolved_kind",
    ],
    "properties": {
        "identity_key": {"type": "string"},
        "resolved": {"type": "boolean"},
        "unresolved_reason": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "content_description": {"type": "string"},
        # Machine-facing English beside the Russian text (issue #727). The
        # CLI's structured output needs every property required, so an empty
        # string means "none"; the response validator treats it as optional.
        "english_description": {"type": "string"},
        # The one closed unresolved verdict code acts on (issue #791). Empty
        # for every other answer; the response validator treats the field
        # as optional, like ``english_description``.
        "unresolved_kind": {"type": "string", "enum": ["", "not_yet_published"]},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "url",
                    "host",
                    "surface_kind",
                    "printed_title",
                    "printed_authors",
                    "describes_this_work",
                    "identity_note",
                ],
                "properties": {
                    "url": {"type": "string"},
                    "host": {"type": "string"},
                    "surface_kind": {
                        "type": "string",
                        "enum": [
                            "publisher",
                            "library_catalogue",
                            "trade_review",
                            "press",
                            "retail_listing",
                            "other",
                        ],
                    },
                    "printed_title": {"type": "string"},
                    "printed_authors": {"type": "array", "items": {"type": "string"}},
                    "describes_this_work": {"type": "boolean"},
                    "identity_note": {"type": "string"},
                },
            },
        },
    },
}

_TOP_LEVEL_FIELDS = ("identity_key", "resolved", "unresolved_reason", "confidence", "content_description", "citations")

# Optional in the response contract, so replay fixtures and responses recorded
# before issue #727 stay valid.
_OPTIONAL_TOP_LEVEL_FIELDS = ("english_description", "unresolved_kind")

UNRESOLVED_KIND_NOT_YET_PUBLISHED = "not_yet_published"

_CITATION_FIELDS = (
    "url",
    "host",
    "surface_kind",
    "printed_title",
    "printed_authors",
    "describes_this_work",
    "identity_note",
)


def _invalid(reason: str) -> tuple[bool, str]:
    return False, reason[:MAX_ADJUDICATION_REASON_LENGTH]


def _check_object_keys(
    value: Any,
    required: Sequence[str],
    label: str,
    optional: Sequence[str] = (),
) -> tuple[bool, str]:
    """Require exactly the contract keys: no missing, no unexpected."""
    for key in value:
        if key not in required and key not in optional:
            return _invalid(f"{label} has unexpected key {str(key)[:50]!r}")
    for key in required:
        if key not in value:
            return _invalid(f"{label} is missing required key {key!r}")
    return True, ""


def _validate_citation(citation: Any, index: int) -> tuple[bool, str]:
    label = f"citations[{index}]"
    if not isinstance(citation, Mapping):
        return _invalid(f"{label} must be an object")
    valid, reason = _check_object_keys(citation, _CITATION_FIELDS, label)
    if not valid:
        return False, reason
    for field in ("url", "host", "printed_title", "identity_note"):
        if not isinstance(citation[field], str):
            return _invalid(f"{label}.{field} must be a string")
    if citation["surface_kind"] not in SURFACE_KINDS:
        return _invalid(f"{label}.surface_kind must be one of {', '.join(SURFACE_KINDS)}")
    printed_authors = citation["printed_authors"]
    if not isinstance(printed_authors, list) or any(not isinstance(item, str) for item in printed_authors):
        return _invalid(f"{label}.printed_authors must be an array of strings")
    if type(citation["describes_this_work"]) is not bool:
        return _invalid(f"{label}.describes_this_work must be a boolean")
    return True, ""


def validate_adjudication_response(response: Any) -> tuple[bool, str]:
    """Check one adapter response against the adjudication contract.

    Returns ``(True, "")`` for a well-formed response, else ``(False, reason)``
    with a bounded reason. A rejection never stops the caller.
    """
    if not isinstance(response, Mapping):
        return _invalid("adjudication response must be an object")
    valid, reason = _check_object_keys(
        response,
        _TOP_LEVEL_FIELDS,
        "adjudication response",
        optional=_OPTIONAL_TOP_LEVEL_FIELDS,
    )
    if not valid:
        return False, reason
    if not isinstance(response["identity_key"], str):
        return _invalid("identity_key must be a string")
    if type(response["resolved"]) is not bool:
        return _invalid("resolved must be a boolean")
    for field in ("unresolved_reason", "content_description"):
        if not isinstance(response[field], str):
            return _invalid(f"{field} must be a string")
    # A malformed english_description is not checked here: it is optional
    # helper text, so the bounding step drops it and the Russian stands
    # (issue #727).
    if response["confidence"] not in CONFIDENCE_VALUES:
        return _invalid(f"confidence must be one of {', '.join(CONFIDENCE_VALUES)}")
    citations = response["citations"]
    if not isinstance(citations, list):
        return _invalid("citations must be an array")
    for index, citation in enumerate(citations):
        valid, reason = _validate_citation(citation, index)
        if not valid:
            return False, reason
    return True, ""


class ReplayDescriptionAdjudicationAdapter:
    """Deterministic replay adapter for tests and the offline fixture run.

    Canned responses are keyed by identity key. A work item without a canned
    response gets an unresolved verdict, so the preview still covers every
    candidate. The adapter makes no network request.
    """

    def __init__(self, responses: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        self._responses = {str(key): copy.deepcopy(dict(value)) for key, value in (responses or {}).items()}
        self.calls: list[str] = []
        self._calls_lock = threading.Lock()

    def adjudicate(self, work_item: Mapping[str, Any]) -> dict[str, Any]:
        identity_key = str(work_item.get("identity_key", ""))
        with self._calls_lock:
            self.calls.append(identity_key)
        if identity_key in self._responses:
            response = copy.deepcopy(self._responses[identity_key])
            response["identity_key"] = identity_key
            return response
        return {
            "identity_key": identity_key,
            "resolved": False,
            "unresolved_reason": "replay has no adjudication verdict",
            "confidence": "low",
            "content_description": "",
            "citations": [],
        }


# Measured wall clock for one live call, on the editor's selected model at
# high reasoning effort (issue #638): 92 s for a candidate with abundant
# English coverage, 87 s for a little-known one, and 135 s for the hardest
# measured case, a generic title by a corporate author. A hard candidate
# costs most because the search for two independent owners runs longest.
# 240 s clears the worst measured call by well under a factor of two, so
# it is a real ceiling and not a generous one. It costs wall clock only
# when a call actually runs long, because the timeout is a ceiling and not
# a wait. Reasoning effort moves these numbers: the same abundant-coverage
# candidate cost 56 s at the model's default effort and 43 s on the CLI
# default model.
ADJUDICATION_CALL_TIMEOUT_SECONDS = 240


class CodexAdjudicationAdapterError(Exception):
    """Base class for bounded per-candidate Codex adjudication failures.

    An optional detail string is always fixed text or an integer count
    from this code, never process output: a missing-CLI ``OSError`` may
    carry the looked-up path, so its message is never forwarded.
    """


class CodexAdjudicationTimeoutError(CodexAdjudicationAdapterError):
    """One call exceeded its hard timeout."""


class CodexAdjudicationExitError(CodexAdjudicationAdapterError):
    """One call failed to spawn, or exited with a non-zero code."""


class CodexAdjudicationResponseError(CodexAdjudicationAdapterError):
    """One call produced no parsable, schema-valid response."""


def _bounded_search_hint_urls(value: Any) -> list[str]:
    """Bound one raw search-hint list through the shared helper (issue #684).

    The module keeps its own constants so the bound stays visible next to
    the prompt and verification it serves; the assert beside them ties the
    count to the grounded fetch budget instead of duplicating the logic.
    """
    return _bounded_search_hints(
        value,
        max_hints=MAX_ADJUDICATION_SEARCH_HINTS,
        max_url_length=MAX_ADJUDICATION_SEARCH_HINT_URL_LENGTH,
    )


def _grounded_outcome_hint_urls(outcome: Mapping[str, Any]) -> list[str]:
    """Collect hint URLs from one grounded stage outcome: verified only.

    Only accepted ``research_sources`` (and their records) qualify, per
    the issue's "accepted/verified URLs" constraint. Rejected URLs are
    never hints: a rejection is either evidence the page is about a
    different work (so suggesting it would spend agent budget on a page
    already proven wrong) or an unverified host the grounded gate never
    confirmed. A hint that is never cited has zero effect on the verdict
    either way.
    """
    candidates: list[Any] = []
    research_sources = outcome.get("research_sources", [])
    if isinstance(research_sources, list | tuple):
        candidates.extend(research_sources)
    records = outcome.get("research_source_records", [])
    if isinstance(records, list | tuple):
        for record in records:
            if isinstance(record, Mapping):
                url = record.get("url", record.get("canonical_url", ""))
                candidates.append(url)
            elif isinstance(record, str):
                candidates.append(record)
    return _bounded_search_hint_urls(candidates)


def load_grounded_search_hints(source: Mapping[str, Any] | Path | str | None) -> dict[str, list[str]]:
    """Load bounded grounded search hints keyed by identity key.

    ``source`` is a grounded stage artifact mapping, a path to one, a
    directory holding one, or ``None``. Missing, unreadable, or malformed
    input yields ``{}`` — never an error — so deterministic and offline
    runs stay byte-identical when no grounded artifact exists.
    """
    if source is None:
        return {}
    if isinstance(source, Path | str):
        path = Path(source)
        try:
            if path.is_dir():
                path = path / GROUNDING_STAGE_ARTIFACT_NAME
            artifact = read_json_or_default(path, None)
        except (OSError, TypeError, ValueError):
            return {}
        if not isinstance(artifact, Mapping):
            return {}
        source = artifact
    if not isinstance(source, Mapping):
        return {}
    outcomes = source.get("outcomes", [])
    if not isinstance(outcomes, list):
        return {}
    hints: dict[str, list[str]] = {}
    for outcome in outcomes:
        if not isinstance(outcome, Mapping):
            continue
        identity_key = str(outcome.get("identity_key", "")).strip()
        candidate_id = str(outcome.get("candidate_id", "")).strip()
        key = identity_key or candidate_id
        if not key or key in hints:
            continue
        urls = _grounded_outcome_hint_urls(outcome)
        if urls:
            hints[key] = urls
    return hints


def _attach_grounded_search_hints(
    work_items: list[dict[str, Any]],
    hints: Mapping[str, Any] | None,
) -> None:
    """Attach bounded grounded hints to work items in place.

    Lookup is by identity key, falling back to candidate ID. Items that
    already carry hints and identities without hints are left alone, so an
    explicit empty mapping disables hints without touching anything else.
    """
    if not hints:
        return
    for item in work_items:
        if not isinstance(item, dict) or item.get("search_hints"):
            continue
        identity_key = str(item.get("identity_key", "")).strip()
        candidate_id = str(item.get("candidate_id", "")).strip()
        raw: Any = None
        if identity_key and identity_key in hints:
            raw = hints[identity_key]
        elif candidate_id and candidate_id in hints:
            raw = hints[candidate_id]
        bounded = _bounded_search_hint_urls(raw) if raw is not None else []
        if bounded:
            item["search_hints"] = bounded


SEARCH_DEPTH_FIELD = "search_depth"
SEARCH_DEPTH_DEEP = "deep"
# The date of a record's last deeper search, kept on the history record.
# Without it a decline the first search retained evidence for would keep
# matching itself, and the deeper search would never run for that book.
DEEP_SEARCH_DATE_FIELD = "description_deep_search_date"

# The deeper search (issue #788) changes where the agent looks, never what
# counts: every numbered rule and the code verification stay the same. The
# 14 placeholder books of the 2026-10-10 run were found by hand this way,
# ISBN queries resolving the non-English titles straight to bookshop and
# publisher pages.
_DEEP_SEARCH_GUIDANCE = (
    "This is a second, deeper search. An earlier search for this book found no usable evidence, "
    "so search differently this time:\n"
    "- When metadata carries an isbn, search for that ISBN first.\n"
    "- Search for the title exactly as given, in its original language. Do not search for a "
    "translated title.\n"
    "- Look first for the publisher's own page for the book, and for bookshop product pages in "
    "the book's own country and language. Then try library and trade catalogues.\n"
    "- Try at least three different queries before you set resolved to false.\n"
    "Every rule below still applies unchanged.\n"
)


def build_adjudication_prompt(*, deep: bool = False) -> str:
    """Build the model-facing prompt for one adjudication call.

    Every rule from the working prototype call is stated here verbatim in
    meaning: printed titles and authors exactly as printed, the
    ``describes_this_work`` identity decision, the original Russian
    description contract, the no-copy and no-invention rules, the
    two-owner requirement with the publisher-retail collapse, and the
    unresolved verdict when the evidence is not enough. The work item's
    ``description_requirements`` are binding, the same way they are for
    the enrichment agent: an autobiographical candidate carries its
    subject-context requirement in that field. The work item's
    ``search_hints`` are grounded-stage URLs to start from (issue #684):
    optional starting points only, never evidence.

    ``deep`` adds the deeper-search guidance for a book the first call left
    at the placeholder (issue #788). The default prompt is unchanged.
    """
    return (
        "Read work_item.json. It holds a list with exactly one work item. Decide whether public "
        "web pages really describe that exact book, and answer with the adjudication response.\n"
        "Echo the exact identity_key from the work item in the identity_key field of your response.\n"
        "Obey the work item's description_requirements.\n"
        "The work item may carry search_hints: grounded-stage URLs that are starting points only. "
        "They are not evidence and do not count as citations. You may start from them, but you must "
        "still search independently and cite pages you verified yourself. A hint never counts toward "
        "the two-owner requirement in rule 5.\n" + (_DEEP_SEARCH_GUIDANCE if deep else "") + "Rules:\n"
        "1. For every cited page, record the title exactly as printed on that page, and the authors "
        "exactly as printed on that page.\n"
        "2. Set describes_this_work to false when the page is about a different work or a different "
        "author, and give the reason in identity_note.\n"
        "3. Write an original Russian Content Description of 2 to 5 sentences, with the subject, the "
        "angle, and the editorial hook. Also write english_description: the same subject and angle in 2 to 5 "
        "sentences of original English prose from the same evidence. It is never shown to the editor. "
        "Leave english_description empty when content_description is empty.\n"
        "4. Do not copy publisher or review prose. Do not invent facts or URLs.\n"
        "5. Two pages with different owners are needed. A publisher page and its own retail listings "
        "count as one owner.\n"
        "6. Set resolved to false with a concrete unresolved_reason when the evidence is not enough. "
        "Never guess.\n"
        "7. Set unresolved_kind to not_yet_published only when resolved is false because the book is "
        "announced, forthcoming, untitled, or offered for rights and is not yet published, so no page "
        "describes it. Leave unresolved_kind empty in every other case.\n"
    )


def _run_codex_subprocess(command: list[str], **kwargs: Any) -> Any:
    """Run the Codex CLI. Tests replace this seam entirely."""
    return subprocess.run(command, **kwargs)


# The reasoning effort is read from the editor's config and goes inside a
# quoted ``-c`` value, so it is shape-checked rather than trusted: a bare
# word cannot close the quoting or become a second setting. The set of
# valid efforts belongs to the CLI, not here, so any bare word passes the
# shape and an effort the CLI rejects fails as a bounded exit code.
_REASONING_EFFORT_SHAPE = re.compile(r"^[a-z]{1,16}$")


def _bounded_reasoning_effort(value: Any) -> str:
    """Return a shape-checked reasoning effort, or "" to omit the flag."""
    effort = str(value or "").strip().casefold()
    return effort if _REASONING_EFFORT_SHAPE.match(effort) else ""


def _pinned_model_and_effort() -> tuple[str, str]:
    """The repository's adjudication pin, or two empty strings when it cannot be read.

    That is the description pin while it is on the Codex CLI, and otherwise
    the pin file's ``adjudication`` entry (Issue #1149): adjudication searches
    the web, which only the Codex CLI offers.  Stage 0 stops the weekly run on
    an unreadable pin before any stage starts, so the empty answer here only
    keeps construction from raising.
    """
    try:
        pin = load_adjudication_model_pin()
    except ModelPinError:
        return "", ""
    return pin.model, pin.reasoning_effort


class CodexDescriptionAdjudicationAdapter:
    """Production adjudication adapter: one Codex CLI call per candidate.

    The command shape is the proven prototype shape from issue #638, with
    ``--ignore-user-config`` added: without it the prototype loaded
    unrelated user memory from the local Codex home, so the call was
    neither bounded nor repeatable. Web search is a model tool, so
    ``--sandbox read-only`` still permits it. The response schema is the
    exact ``ADJUDICATION_RESPONSE_SCHEMA`` contract.

    Isolation follows the existing Codex description agent: a temporary
    directory holds one encoded work-item file, the schema file, and one
    response file, and nothing else. The work item is encoded by the
    existing bounded encoder, so no raw page body and no oversized
    structure reaches the model. Every call has a hard timeout.

    A timeout, a spawn failure, a non-zero exit, and an unparsable or
    schema-invalid response each raise a closed exception subclass. The
    command turns each into a bounded per-candidate failure that never
    stops the run and never produces a description; the exception carries
    no message, so no path or process output can leak into an artifact.

    ``--ignore-user-config`` keeps the whole Codex home out: an unattended
    batch must not run the operator's ``notify`` hook once per candidate or
    inherit their approval and personality settings. The model and its
    reasoning effort are passed explicitly from the ``description`` entry
    of ``configs/codex-model-pin.json`` (or its ``adjudication`` entry once
    the description pin is on a backend without web search, Issue #1149),
    never from the operator's global default: on 2026-11-10 that default named a model the account could
    not use, and every adjudication call of the run failed. Both are read
    once per run, so every candidate in one run is adjudicated the same
    way. When the pin cannot be read the flags are omitted; Stage 0 stops
    the weekly run on that pin before this adapter is used.
    """

    def __init__(
        self,
        timeout_seconds: float = ADJUDICATION_CALL_TIMEOUT_SECONDS,
        *,
        runner: Callable[..., Any] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self._timeout_seconds = float(timeout_seconds)
        self._runner = runner or _run_codex_subprocess
        # Always the Codex CLI, whatever backend the description pin names (#1149).
        self._client = CodexCliClient()
        pinned_model, pinned_effort = _pinned_model_and_effort()
        self._model = str(pinned_model if model is None else model).strip()
        self._reasoning_effort = _bounded_reasoning_effort(
            pinned_effort if reasoning_effort is None else reasoning_effort
        )

    def adjudicate(self, work_item: Mapping[str, Any]) -> dict[str, Any]:
        encoded = _encode_work_items([dict(work_item)])
        with tempfile.TemporaryDirectory(prefix="autoscout-desc-adjudication-") as temporary:
            isolated = Path(temporary)
            work_path = isolated / "work_item.json"
            schema_path = isolated / "response-schema.json"
            response_path = isolated / "response.json"
            work_path.write_text(json.dumps(encoded, ensure_ascii=False), encoding="utf-8")
            schema_path.write_text(json.dumps(ADJUDICATION_RESPONSE_SCHEMA), encoding="utf-8")
            request = ModelRequest(
                # The work-item encoder forwards known keys only, so the
                # search depth reaches the model through the prompt.
                prompt=build_adjudication_prompt(deep=work_item.get(SEARCH_DEPTH_FIELD) == SEARCH_DEPTH_DEEP),
                response_path=response_path,
                # A bad model name fails as a bounded exit code.
                model=self._model,
                reasoning_effort=self._reasoning_effort,
                working_dir=isolated,
                output_schema_path=schema_path,
                web_search=True,
                isolated=True,
            )
            # The command decides identity with the raw work-item key, so
            # this side uses the same key and the same comparison. The
            # encoded key is bounded to 200 characters, so an over-long key
            # compared here against the encoded form would pass this check
            # and still fail the command's, as an unexplained ambiguity.
            identity_key = str(work_item.get("identity_key", "")).strip()
            result = self._client.run(request, timeout=self._timeout_seconds, runner=self._runner)
            if result.status is ModelCallStatus.TIMEOUT:
                # A builtin TimeoutError from an alternative runner seam is a
                # timeout too, never a spawn failure.
                raise CodexAdjudicationTimeoutError(f"timeout after {self._timeout_seconds:g}s")
            if result.status is ModelCallStatus.NOT_STARTED:
                # Fixed text only: ``str(exc)`` can carry the looked-up path.
                raise CodexAdjudicationExitError("spawn failed") from result.spawn_error
            if result.status is ModelCallStatus.EXIT_NONZERO:
                # The integer exit code is the one safe diagnostic: it tells
                # a model failure from a different CLI failure without
                # carrying any process output.
                raise CodexAdjudicationExitError(f"exit code {result.returncode}")
            if result.status is not ModelCallStatus.ANSWERED:
                # Fixed text only: an OSError message can carry the path.
                raise CodexAdjudicationResponseError("unparsable response")
            payload = result.payload
            # The response is validated against the schema before any other
            # code reads it. The command re-validates on receipt; that is
            # idempotent, and this side is the hard boundary.
            # The validator reason names the offending field, which is a
            # fixed string from this module, but it is not forwarded: the
            # operator needs the failure class, and the preview already
            # carries the validator reason for a response that reaches it.
            valid, _reason = validate_adjudication_response(payload)
            if not valid:
                raise CodexAdjudicationResponseError("schema invalid")
            # The prompt asks the model to echo the exact identity_key the
            # work item carries. Check that here, at the source: a
            # formatting slip must fail as its own bounded reason now, not
            # surface much later as an evidence-ambiguity fallback that
            # names the wrong cause and costs a full call. The comparison
            # is exact, the same one the command applies, so a padded echo
            # cannot pass here and fail there.
            if str(payload.get("identity_key", "")) != identity_key:
                raise CodexAdjudicationResponseError("identity echo mismatch")
            return dict(payload)


def _candidate_id(record: Mapping[str, Any]) -> str:
    value = record.get("candidate_id", "")
    return "" if value is None else str(value).strip()


def _candidate_identity_key(record: Mapping[str, Any]) -> str:
    value = record.get("identity_key", "")
    return "" if value is None else str(value).strip()


def _bounded_response(raw_response: Mapping[str, Any], *, identity_key: str) -> dict[str, Any]:
    """Copy one adapter response with every retained string bounded.

    The response is untrusted adapter output, and the preview artifact keeps
    it so a reader can see what the adapter claimed. Bounded retention holds
    here as everywhere else: each free-text field is truncated, and the
    citation list itself is capped. Nothing here is a page body; the adapter
    never returns one, and verification retains none.
    """
    citations = raw_response.get("citations", [])
    if not isinstance(citations, list):
        citations = []
    bounded_citations: list[dict[str, Any]] = []
    for citation in citations[:MAX_ADJUDICATION_RETAINED_CITATIONS]:
        if not isinstance(citation, Mapping):
            continue
        authors = citation.get("printed_authors", [])
        if not isinstance(authors, list):
            authors = []
        bounded_citations.append(
            {
                "url": str(citation.get("url", ""))[:MAX_ADJUDICATION_CITATION_TEXT_LENGTH],
                "host": str(citation.get("host", ""))[:MAX_ADJUDICATION_CITATION_TEXT_LENGTH],
                "surface_kind": str(citation.get("surface_kind", ""))[:MAX_ADJUDICATION_CITATION_TEXT_LENGTH],
                "printed_title": str(citation.get("printed_title", ""))[:MAX_ADJUDICATION_CITATION_TEXT_LENGTH],
                "printed_authors": [
                    str(author)[:MAX_ADJUDICATION_CITATION_TEXT_LENGTH]
                    for author in authors[:MAX_ADJUDICATION_RETAINED_CITATIONS]
                ],
                "describes_this_work": citation.get("describes_this_work") is True,
                "identity_note": str(citation.get("identity_note", ""))[:MAX_ADJUDICATION_IDENTITY_NOTE_LENGTH],
            }
        )
    return {
        "identity_key": identity_key,
        "resolved": raw_response.get("resolved") is True,
        "unresolved_reason": str(raw_response.get("unresolved_reason", ""))[:MAX_ADJUDICATION_REASON_LENGTH],
        "confidence": str(raw_response.get("confidence", ""))[:20],
        "content_description": str(raw_response.get("content_description", ""))[:MAX_ADJUDICATION_RESPONSE_TEXT_LENGTH],
        "citations": bounded_citations,
        **(
            {"english_description": str(raw_response["english_description"])[:MAX_ADJUDICATION_RESPONSE_TEXT_LENGTH]}
            if isinstance(raw_response.get("english_description"), str) and raw_response["english_description"]
            else {}
        ),
        # Kept only as the one closed value, so no adapter text travels here.
        **(
            {"unresolved_kind": UNRESOLVED_KIND_NOT_YET_PUBLISHED}
            if raw_response.get("unresolved_kind") == UNRESOLVED_KIND_NOT_YET_PUBLISHED
            else {}
        ),
    }


def _skip_reason_for_record(record: Mapping[str, Any]) -> str:
    """Explain why an explicitly named candidate needs no description work."""
    if _prov_string(dict(record)) == "editor-corrected":
        return "editor-corrected record preserved"
    if has_nonfallback_description(record.get("content_description", "")):
        return "valid description preserved"
    return "no description work needed"


def _applied_detail(row: Mapping[str, Any], operation_date: str) -> dict[str, Any]:
    """Restate the applied record in the preview, next to its evidence.

    The applied Candidate History record already carries the Candidate ID,
    the identity key, the provenance, the applied date, the ordered evidence
    URLs, the owner count, and the confidence. This repeats them in the
    preview so one artifact shows what the run did without a second read of
    state.
    """
    response = row.get("response", {})
    verification = row.get("verification", {})
    confidence = str(response.get("confidence", "")) if isinstance(response, Mapping) else ""
    evidence_urls = list(verification.get("counting_urls", [])) if isinstance(verification, Mapping) else []
    owner_count = verification.get("owner_count", 0) if isinstance(verification, Mapping) else 0
    unverified = str(row.get("decision", "")) == DECISION_APPLY_UNVERIFIED
    detail: dict[str, Any] = {
        "candidate_id": row.get("candidate_id", ""),
        "identity_key": row.get("identity_key", ""),
        "provenance": ADJUDICATION_UNVERIFIED_PROVENANCE if unverified else ADJUDICATION_PROVENANCE,
        "applied_date": operation_date,
        "evidence_urls": evidence_urls,
        "owner_count": owner_count,
        "confidence": confidence,
    }
    if str(row.get("decision", "")) == DECISION_APPLY_SINGLE_SOURCE:
        detail["status_text"] = DESCRIPTION_SINGLE_SOURCE_STATUS
    elif unverified:
        detail["status_text"] = DESCRIPTION_UNCONFIRMED_STATUS
    return detail


def adjudication_reason_label(reason_code: str) -> str:
    """Return the Russian editor label for an adjudication reason code.

    Every closed code the stage emits already has a shared label in
    ``grounded_description.reason_label``. The one exception is the stage
    clock: the shared ``not_attempted_stage_budget`` label names the source
    search, which did run before adjudication, so this stage uses its own
    true sentence (issue #641).
    """
    from autoscout.grounded_description import REASON_NOT_ATTEMPTED_STAGE_BUDGET
    from autoscout.grounded_description import reason_label

    if str(reason_code).strip() == REASON_NOT_ATTEMPTED_STAGE_BUDGET:
        return ADJUDICATION_REASON_NOT_ATTEMPTED_LABEL
    return reason_label(reason_code)


def build_adjudication_stage_report(
    *,
    operation_id: str,
    operation_date: str,
    status: str,
    rows: Sequence[Mapping[str, Any]],
    history: Sequence[Mapping[str, Any]],
    stage_budget_seconds: float = ADJUDICATION_STAGE_BUDGET_SECONDS,
    call_timeout_seconds: float = ADJUDICATION_CALL_TIMEOUT_SECONDS,
    max_workers: int = ADJUDICATION_MAX_WORKERS,
    candidate_limit: int | None = None,
    failure_reason: str = "",
    repair: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the editor-facing adjudication stage report.

    The shape mirrors ``_build_report`` in
    ``grounded_description_enrichment.py`` so an operator reads both
    description stages the same way: ``version``, ``operation_id``,
    ``operation_date``, ``status``, ``counts``, ``count_semantics``,
    ``records``, ``attention_required``, ``budget``.

    ``counts`` holds the four exclusive classifications plus the overlapping
    ``still_fallback`` final-state count. ``adjudication_counts`` keeps the stage's own eight
    decisions apart, so ``evidence_unchanged`` and ``not_attempted`` are not
    lost inside ``skipped``. ``count_semantics`` declares both sets.

    Per record only bounded values travel: candidate id, identity key,
    result, final state, reason code, surviving citation count, owner count,
    and confidence. Adapter free text (``response.unresolved_reason``) never
    leaves the preview.
    """
    history_by_identity: dict[str, Mapping[str, Any]] = {}
    for record in history:
        if isinstance(record, Mapping):
            key = str(record.get("identity_key", "")).strip()
            if key and key not in history_by_identity:
                history_by_identity[key] = record
    adjudication_counts: dict[str, int] = {decision: 0 for decision in ADJUDICATION_DECISIONS}
    for row in rows:
        decision = str(row.get("decision", "")).strip()
        if decision in adjudication_counts:
            adjudication_counts[decision] += 1
    classification_counts: dict[str, int] = {name: 0 for name in ADJUDICATION_CLASSIFICATION_COUNTS}
    records: list[dict[str, Any]] = []
    still_fallback = 0
    for row in rows:
        decision = str(row.get("decision", "")).strip()
        result = ADJUDICATION_DECISION_CLASSIFICATION.get(decision, "skipped")
        if result in classification_counts:
            classification_counts[result] += 1
        candidate_id = str(row.get("candidate_id", "") or "").strip()
        identity_key = str(row.get("identity_key", "") or "").strip()
        decision_reason = row.get("decision_reason", "")
        reason_code = str(decision_reason or "").strip()[:MAX_ADJUDICATION_REASON_LENGTH]
        verification = row.get("verification", {})
        response = row.get("response", {})
        counting_urls = verification.get("counting_urls", []) if isinstance(verification, Mapping) else []
        citation_count = len(counting_urls) if isinstance(counting_urls, list) else 0
        raw_owner = verification.get("owner_count", 0) if isinstance(verification, Mapping) else 0
        owner_count = int(raw_owner) if isinstance(raw_owner, int) and raw_owner >= 0 else 0
        raw_confidence = response.get("confidence", "") if isinstance(response, Mapping) else ""
        confidence = str(raw_confidence or "").strip()[:20]
        history_record = history_by_identity.get(identity_key)
        if history_record is None:
            final_state = "not_evaluated"
        elif record_has_usable_description(history_record):
            final_state = "description_present"
        else:
            final_state = "still_fallback"
            still_fallback += 1
        records.append(
            {
                "candidate_id": candidate_id,
                "identity_key": identity_key,
                "result": result,
                "final_state": final_state,
                "reason_code": reason_code,
                "citation_count": citation_count,
                "owner_count": owner_count,
                "confidence": confidence,
            }
        )
    records.sort(key=lambda item: (str(item.get("candidate_id", "")), str(item.get("identity_key", ""))))
    counts: dict[str, int] = {
        "applied": int(classification_counts.get("applied", 0)),
        "skipped": int(classification_counts.get("skipped", 0)),
        "failed": int(classification_counts.get("failed", 0)),
        "ambiguous": int(classification_counts.get("ambiguous", 0)),
        "still_fallback": int(still_fallback),
    }
    report: dict[str, Any] = {
        "version": 1,
        "operation_id": str(operation_id or ""),
        "operation_date": str(operation_date or ""),
        "status": str(status or "ok"),
        "call_latency": call_latency_summary((), 0),
        "counts": counts,
        "adjudication_counts": dict(adjudication_counts),
        "count_semantics": {
            "classification_counts": ["applied", "skipped", "failed", "ambiguous"],
            "overlapping_final_state_counts": ["still_fallback"],
            "adjudication_decision_counts": list(ADJUDICATION_DECISIONS),
        },
        "records": records,
        # Attention from this stage is a stage-health fact, not a backlog
        # fact. ``failed`` means the adapter returned something the schema
        # or the identity echo rejected, which is worth an editor's
        # attention because the stage itself is misbehaving.
        #
        # ``ambiguous`` is not. This stage is the backlog repair pass: it
        # reads every record that already lacks a description, so a decline
        # is the backlog staying the backlog. Issue #557 settled that the
        # cumulative backlog "is a standing corpus property; it never
        # enters this decision", and counting declines here would put every
        # weekly run into attention-required forever, because a decline
        # with no citations retains no evidence and is re-adjudicated every
        # week. The declines stay reported in the counts, in the reason
        # totals, and in the review (issue #641).
        "attention_required": bool(counts["failed"]),
        "budget": {
            "stage_budget_seconds": float(stage_budget_seconds),
            "call_timeout_seconds": float(call_timeout_seconds),
            "max_workers": int(max_workers),
            "candidate_limit": int(candidate_limit) if candidate_limit is not None else None,
        },
    }
    if repair is not None:
        report["repair"] = dict(repair)
    # The deeper second call (issue #788), reported only when one ran so a
    # run without placeholders reads exactly as before. ``applied`` counts
    # the books the deeper call described after the first call declined.
    #
    # ``failed`` counts the adapter failures the row's own decision does
    # not show. A candidate has one row but two calls, and the row carries
    # the better call's verdict, so a timed-out or invalid call can sit
    # behind a plain decline. That is the same stage-health fact ``failed``
    # reports for a single call, so it raises attention the same way; a
    # row whose own decision is invalid is already in ``counts``.
    deep_rows = [row for row in rows if isinstance(row.get("deep_pass"), Mapping)]
    if deep_rows:
        hidden_failures = sum(
            1
            for row in deep_rows
            if str(row.get("decision", "")).strip() != DECISION_INVALID
            and DECISION_INVALID
            in (
                str(row["deep_pass"].get("first_decision", "")).strip(),
                str(row["deep_pass"].get("decision", "")).strip(),
            )
        )
        report["deep_pass"] = {
            "attempted": len(deep_rows),
            "applied": sum(
                1
                for row in deep_rows
                if ADJUDICATION_DECISION_CLASSIFICATION.get(str(row.get("decision", "")).strip()) == "applied"
            ),
            "failed": hidden_failures,
        }
        if hidden_failures:
            report["attention_required"] = True
    if failure_reason:
        report["failure_reason"] = str(failure_reason)[:MAX_ADJUDICATION_REASON_LENGTH]
    return report


def _stage_report_record_id(record: Mapping[str, Any]) -> str:
    """Identify one stage-report record the way both stages write it."""
    candidate_id = str(record.get("candidate_id", "") or "").strip()
    identity_key = str(record.get("identity_key", "") or "").strip()
    return candidate_id or identity_key


def _stage_report_records(report: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    """Return the per-candidate records of a description stage report."""
    if not isinstance(report, Mapping):
        return []
    records = report.get("records")
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, Mapping)]


def resolved_description_candidates(adjudication_report: Mapping[str, Any] | None) -> set[str]:
    """Return candidates adjudication described or found not yet published."""
    return {
        _stage_report_record_id(record)
        for record in _stage_report_records(adjudication_report)
        if str(record.get("final_state", "")) == "description_present"
        or str(record.get("reason_code", "")) == REASON_NOT_YET_PUBLISHED
    }


def unresolved_description_candidates(
    grounded_report: Mapping[str, Any] | None,
    adjudication_report: Mapping[str, Any] | None,
) -> set[str]:
    """Return candidates a description stage flagged and no stage repaired.

    Both description stages write reports in one vocabulary, and both run
    in one weekly pass: the grounded stage first, then adjudication over
    what it left at fallback. So a per-stage ``failed`` or ``ambiguous``
    result is a stage verdict, not the run's verdict. A candidate the
    grounded stage could not ground and adjudication then applied ended the
    run with a description, and reporting it as a failure contradicts the
    coverage line printed beside it (issue #641).

    ``final_state`` in the adjudication report is computed from Candidate
    History after the apply gate ran, so it is the authority on how the run
    ended. A candidate that report shows as ``description_present`` is
    repaired, whichever stage flagged it.
    """
    flagged: set[str] = set()
    for report in (grounded_report, adjudication_report):
        for record in _stage_report_records(report):
            if (
                str(record.get("result", "")) in {"failed", "ambiguous"}
                and record.get("reason_code") != REASON_NOT_ATTEMPTED_STAGE_BUDGET
            ):
                identifier = _stage_report_record_id(record)
                if identifier:
                    flagged.add(identifier)
    repaired = resolved_description_candidates(adjudication_report)
    deferred: set[str] = set()
    if isinstance(adjudication_report, Mapping):
        limit_info = adjudication_report.get("deferred_limit", {})
        if isinstance(limit_info, Mapping) and isinstance(limit_info.get("candidate_ids"), list):
            deferred.update(str(value) for value in limit_info["candidate_ids"])
    deferred.update(
        _stage_report_record_id(record)
        for record in _stage_report_records(adjudication_report)
        if record.get("reason_code") == REASON_NOT_ATTEMPTED_STAGE_BUDGET
    )
    return {identifier for identifier in flagged - repaired - deferred if identifier}


def _fallback_plan(
    reason: str,
    *,
    identity_key: str = "",
    research_sources: Sequence[str] | None = None,
    evidence_digests: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Plan one fallback: keep the record's fallback text, name the reason.

    The gate reads ``rejection_reason`` and leaves the description at the
    standard fallback. Every reason here is a closed reason code, never
    adapter prose. Verified counting evidence travels along when the caller
    has it, so the next run can report ``evidence_unchanged`` instead of
    calling the agent again (issue #639). A fallback with no verified
    evidence carries no evidence keys, exactly as before.
    """
    artifact: dict[str, Any] = {
        "identity_key": identity_key,
        "content_description": "",
        "rejection_reason": reason,
    }
    if research_sources:
        artifact["research_sources"] = list(research_sources)
    if evidence_digests:
        artifact["evidence_digests"] = list(evidence_digests)
    return {
        "decision": DECISION_FALLBACK,
        "reason": reason,
        "artifact": artifact,
    }


def plan_adjudication_application(
    response: Mapping[str, Any],
    verification: Mapping[str, Any],
    *,
    identity_matched: bool,
    candidate_id: str = "",
    allow_unverified: bool = False,
    may_await_publication: bool = False,
) -> dict[str, Any]:
    """Decide the acceptance bar for one verified adjudication response.

    A two-source description applies only when the response resolved, at
    least two counting citations carry two different owners, the existing
    validator accepts the text, and the identity matches. Exactly one
    surviving owner applies a single-source description marked with the
    existing single-source status text. With ``allow_unverified``, a
    resolved high-confidence description none of whose citations counted is
    kept as unverified text rather than discarded, unless a cited page named
    a different author (issue #791). The caller sets it only for a record
    that holds no description or a title-derived stub. Every other case
    leaves the record at fallback with a bounded reason code. Nothing is
    written here: the
    returned artifact is the complete input the apply gate needs, for an
    accepted description and for a fallback alike.
    """
    identity_key = str(response.get("identity_key", ""))
    # Verification already published the counting evidence, URLs and digests
    # in one order. Reading it here keeps one owner of the counting rule.
    raw_urls = verification.get("counting_urls", [])
    raw_digests = verification.get("counting_digests", [])
    counting_urls = [str(url) for url in raw_urls] if isinstance(raw_urls, list) else []
    counting_digests = [str(digest) for digest in raw_digests] if isinstance(raw_digests, list) else []
    if len(counting_digests) != len(counting_urls):
        # A caller that publishes URLs without digests retains no digests,
        # rather than a mis-paired list (issue #639).
        counting_digests = []
    if not identity_matched:
        # Nothing about this response is anchored to a known record, so it
        # carries no evidence worth retaining.
        return _fallback_plan(REASON_AMBIGUITY, identity_key=identity_key)

    def fallback(reason: str) -> dict[str, Any]:
        """Plan a fallback that keeps the counting evidence this run verified."""
        return _fallback_plan(
            reason,
            identity_key=identity_key,
            research_sources=counting_urls,
            evidence_digests=counting_digests,
        )

    if verification.get("resolved") is not True:
        # The adapter explains itself in free text. That text is adapter
        # prose, so it stays in the preview and never becomes the editor's
        # reason: the editor reads a closed reason code that already has a
        # Russian label (issue #637).
        #
        # The one closed verdict the adapter may give is that the book is
        # not yet published. It is believed only where the caller found
        # nothing on the record that says the book is out (issue #791).
        if may_await_publication and response.get("unresolved_kind") == UNRESOLVED_KIND_NOT_YET_PUBLISHED:
            return fallback(REASON_NOT_YET_PUBLISHED)
        return fallback(REASON_AMBIGUITY)
    owner_count = verification.get("owner_count", 0)
    unverified = not isinstance(owner_count, int) or owner_count <= 0
    if unverified and not (allow_unverified and _unverified_description_qualifies(response, verification)):
        return fallback(REASON_FEWER_THAN_TWO)
    validated, _rejection = validate_content_description(response.get("content_description", ""))
    if unverified and (validated is None or not meets_description_depth(validated)):
        # Unusable text changes nothing about why the citations failed.
        return fallback(REASON_FEWER_THAN_TWO)
    if validated is None:
        return fallback(REASON_INVALID_DESCRIPTION)
    # Depth acceptance (issue #673): verified safety is not enough for a
    # newly applied description. Below-floor text falls back with the
    # shared depth reason instead of becoming editor copy.
    if not meets_description_depth(validated):
        return fallback(REASON_INSUFFICIENT_DEPTH)
    if unverified:
        # The cited URLs are kept as where the agent read, with no digests:
        # nothing was verified, so nothing here is comparable evidence.
        unverified_artifact: dict[str, Any] = {
            "identity_key": identity_key,
            "content_description": validated,
            "research_sources": _unverified_citation_urls(verification),
            "candidate_id": candidate_id,
            "owner_count": 0,
            "confidence": str(response.get("confidence", "")),
        }
        unverified_english = str(response.get("english_description", "") or "").strip()
        if unverified_english:
            unverified_artifact["english_description"] = unverified_english
        return {"decision": DECISION_APPLY_UNVERIFIED, "reason": "", "artifact": unverified_artifact}
    two_source = owner_count >= 2
    artifact = {
        "identity_key": identity_key,
        "content_description": validated,
        "research_sources": [str(url) for url in counting_urls if str(url).strip()],
        "evidence_digests": list(counting_digests),
        # The applied record names itself: the exact Candidate ID, how many
        # independent owners agreed, how sure the adapter was, and the stage
        # outcome the review turns into the editor's status mark (issue #637).
        "candidate_id": candidate_id,
        "owner_count": owner_count,
        "confidence": str(response.get("confidence", "")),
        "stage_outcome": OUTCOME_GROUNDED if two_source else OUTCOME_SINGLE_SOURCE,
    }
    # The apply gate validates it and drops it when invalid; the Russian
    # description never depends on it (issue #727).
    english_description = str(response.get("english_description", "") or "").strip()
    if english_description:
        artifact["english_description"] = english_description
    if _counting_identity_note_present(verification):
        # A note on a counting citation is evidence the description rests
        # on, so the record keeps that a note existed. The note itself is
        # adapter prose and stays in the preview: only the flag travels
        # (issues #637 and #644).
        artifact["identity_note_present"] = True
    decision = DECISION_APPLY_TWO_SOURCE if two_source else DECISION_APPLY_SINGLE_SOURCE
    return {"decision": decision, "reason": "", "artifact": artifact}


# A dropped citation is disproof only when the page named a different
# author. A page that could not be fetched, or that printed no author or
# title where the check could read it, confirms nothing and refutes nothing.
_UNVERIFIED_DROP_REASONS = (REASON_SEARCH_OR_FETCH_FAILURE, REASON_AUTHOR_UNVERIFIABLE, REASON_TITLE_MISMATCH)


def _unverified_citation_urls(verification: Mapping[str, Any]) -> list[str]:
    """Return the cited URLs verification could neither confirm nor refute."""
    dropped = verification.get("dropped", [])
    if not isinstance(dropped, list):
        return []
    urls: list[str] = []
    for entry in dropped:
        if not isinstance(entry, Mapping) or entry.get("describes_this_work") is not True:
            continue
        url = str(entry.get("url", "")).strip()
        if url and str(entry.get("reason", "")) in _UNVERIFIED_DROP_REASONS and url not in urls:
            urls.append(url)
    return urls


def _unverified_description_qualifies(response: Mapping[str, Any], verification: Mapping[str, Any]) -> bool:
    """Say whether an unconfirmed description is still worth keeping.

    The agent must be highly confident and must have cited at least one page
    it says describes this work, and no cited page may have named a
    different author: that is the one drop that says the agent described
    another book (issue #791).
    """
    if str(response.get("confidence", "")) != ADJUDICATION_UNVERIFIED_CONFIDENCE:
        return False
    dropped = verification.get("dropped", [])
    if not isinstance(dropped, list):
        return False
    if any(
        isinstance(entry, Mapping)
        # A page the agent itself excluded says nothing about this work.
        and entry.get("describes_this_work") is True
        and (entry.get("reason") == REASON_AUTHOR_MISMATCH or entry.get("author_contradicted") is True)
        for entry in dropped
    ):
        return False
    return bool(_unverified_citation_urls(verification))


def _record_may_await_publication(record: Mapping[str, Any], operation_date: str) -> bool:
    """Say whether nothing on the record contradicts a not-yet-published verdict.

    A record with an ISBN that may prove identity, or with a publication
    year before the run's year, names a published book, so the agent's
    verdict is not believed for it (issue #791).
    """
    if identity_isbn(dict(record)):
        return False
    year_match = re.search(r"\d{4}", str(record.get("publication_year", "") or ""))
    return year_match is None or year_match.group() >= str(operation_date)[:4]


def _counting_identity_note_present(verification: Mapping[str, Any]) -> bool:
    """Say whether a counting citation carried an adapter identity note.

    Only citations that took a counting slot and describe this work count:
    a note on a citation the verification dropped is not evidence the
    description rests on and must not reach the record. Dropped citations
    never take a counting slot, so both qualifiers are checked.
    """
    surviving = verification.get("surviving", [])
    if not isinstance(surviving, list):
        return False
    for entry in surviving:
        if not isinstance(entry, Mapping):
            continue
        if (
            entry.get("counts_toward_independence") is True
            and entry.get("describes_this_work") is True
            and str(entry.get("identity_note", "") or "").strip()
        ):
            return True
    return False


# Only adjudication-retained evidence is comparable across runs, because only
# it pairs each evidence URL with the digest this stage computes. A
# deterministic ``grounded`` record carries URLs but no digests, so it can
# never match here and stays eligible by construction.
#
# Both adjudication provenances belong here. An applied description is
# protected by selection already: ``prepare_backfill_work_items`` never
# returns a record that holds a valid description, and the apply gate runs
# with ``allow_overwrite_valid=False``, so approved text cannot be
# rewritten. A fallback record is the case this gate exists for: its cited
# pages are unchanged, so a fresh agent call would reach the same verdict
# and spend the run's budget to do it. That saving must never harden into
# silence, so the gate is bounded by the re-verification cadence and by the
# editor's named selection below (issue #639).
_ADJUDICATION_EVIDENCE_PROVENANCES = (ADJUDICATION_PROVENANCE, "fallback")


def _retained_adjudication_evidence(record: Mapping[str, Any]) -> tuple[list[str], list[str]] | None:
    """Return the retained (evidence URLs, evidence digests) for one record.

    Returns ``None`` when the record carries no comparable evidence: no
    provenance record, a provenance that never retained digests, or missing,
    empty, or mismatched URL and digest lists. An empty evidence set is not
    a match: there is nothing whose sameness could skip the agent.
    """
    provenance = record.get("description_provenance")
    if not isinstance(provenance, Mapping):
        return None
    if str(provenance.get("provenance", "")) not in _ADJUDICATION_EVIDENCE_PROVENANCES:
        return None
    urls = provenance.get("evidence_basis")
    digests = provenance.get("evidence_digests")
    if not isinstance(urls, list) or not isinstance(digests, list):
        return None
    clean_urls = [str(url) for url in urls if isinstance(url, str) and url.strip()]
    clean_digests = [str(digest) for digest in digests if isinstance(digest, str) and digest.strip()]
    if not clean_urls or len(clean_urls) != len(clean_digests):
        return None
    return clean_urls, clean_digests


def _adjudication_evidence_is_stale(record: Mapping[str, Any], checked_date: str | None) -> bool:
    """Say whether the retained evidence is due for a fresh look anyway.

    The deterministic stage re-verifies a grounded candidate about monthly
    instead of trusting a stored comparison for ever, and re-queues a record
    whose last attempt is undated (``GROUNDED_REVERIFY_AGE_DAYS``, issues
    #548 and #549). Adjudication follows the same cadence, and for the same
    reason: without it a candidate the agent once rejected could never be
    reconsidered, because its evidence would keep matching itself while the
    stage, the response contract, and the committed policy file all moved on.
    An undated or unreadable record is stale, so a missing date can only
    cost one agent call, never permanent silence.
    """
    from autoscout.generate_descriptions import GROUNDED_REVERIFY_AGE_DAYS

    provenance = record.get("description_provenance")
    applied = str(provenance.get("applied_date", "")).strip() if isinstance(provenance, Mapping) else ""
    if not applied or not checked_date:
        return True
    try:
        age_days = (date.fromisoformat(str(checked_date)) - date.fromisoformat(applied)).days
    except ValueError:
        return True
    return age_days >= GROUNDED_REVERIFY_AGE_DAYS


def _adjudication_evidence_unchanged(
    record: Mapping[str, Any],
    *,
    fetcher: BoundedHTTPFetcher,
    classifier: SourceClassifier,
    forced: bool = False,
    checked_date: str | None = None,
) -> tuple[bool, list[str]]:
    """Re-fetch the retained evidence URLs and compare each fresh digest.

    Returns ``(True, urls)`` only when every retained page still fetches and
    every fresh evidence digest matches the retained one. Any fetch failure
    or any changed page returns ``(False, [])``: the candidate is eligible
    again. Classification is recomputed and sent truthfully, with the same
    named unclassified-host opt-in verification uses. No page body is
    retained; only the fresh digests are compared.

    Four cases never reach the comparison at all:

    - ``forced``: the editor named this candidate on the command line, which
      forces re-synthesis exactly as ``_force_synthesis`` does in the
      deterministic stage.
    - a record that kept a valid description and still re-entered the work
      list, which happens only when a repair is due. A pending repair must
      run, so the gate never stands in front of it.
    - retained evidence older than the re-verification cadence, so nothing
      is skipped for ever.
    - more retained URLs than one candidate's fetch budget allows. The gate
      exists to save the run work, so it never spends more fetches than the
      stage would spend on the candidate itself.
    """
    if forced:
        return False, []
    if has_nonfallback_description(str(record.get("content_description", ""))):
        return False, []
    retained = _retained_adjudication_evidence(record)
    if retained is None:
        return False, []
    urls, digests = retained
    if len(urls) > MAX_FETCHES_PER_CANDIDATE:
        return False, []
    if _adjudication_evidence_is_stale(record, checked_date):
        return False, []
    for url, digest in zip(urls, digests, strict=True):
        classification = classifier.classify(url)
        try:
            fetched = fetcher.fetch(
                {
                    "url": url,
                    "classification": str(classification.get("classification", "unknown")),
                    "source_kind": str(classification.get("source_kind", "")),
                    "source_family": str(classification.get("source_family", "")),
                    "owner_id": str(classification.get("owner_id", "")),
                    "is_search_snippet": False,
                },
                allow_unclassified_host=True,
                **({"checked_date": checked_date} if checked_date else {}),
            )
        except (BoundedHTTPError, OSError, ValueError):
            return False, []
        if adjudication_evidence_digest(fetched.subject_text) != digest:
            return False, []
    return True, urls


def _fallback_would_only_overwrite_the_earlier_reason(
    decision: str,
    artifact: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]],
) -> bool:
    """True when applying this fallback would take away and add nothing.

    The apply gate resolves a rejected description to the standard fallback
    and writes the bounded reason with it. That is correct when the stage
    has something to add: text where there was none, or verified counting
    evidence the next run can compare against (issue #639).

    An adjudication fallback with no counting evidence, on a record that
    already holds the standard fallback, has neither. Applying it would
    replace whatever the earlier stage recorded. The grounded stage's
    reason is the one that names an actionable source problem, such as
    ``unsupported grounding evidence URL``, and the review shows it to the
    editor. Replacing it with ``ambiguity`` tells the editor less, and it
    only ever happens to records whose description already failed, which
    are exactly the records whose reason matters most (issue #640).

    The row still counts as a fallback, and the adjudication outcome is
    still reported in the preview next to its own reason code. Only the
    Candidate History write is left out.
    """
    if decision != DECISION_FALLBACK:
        return False
    if artifact.get("research_sources") or artifact.get("evidence_digests"):
        return False
    identity_key = str(artifact.get("identity_key", "")).strip()
    for record in history:
        if str(record.get("identity_key", "")).strip() == identity_key:
            return str(record.get("content_description", "")).strip() == DESCRIPTION_FALLBACK
    return False


def _adjudication_repair_summary(
    history: Sequence[Mapping[str, Any]],
    visibility_records: Sequence[Mapping[str, Any]],
    operation_date: str,
    rows: Sequence[Mapping[str, Any]],
    repair_selected_ids: set[str],
) -> dict[str, int]:
    """Summarize depth-repair progress with grounded-consistent semantics (#675).

    ``backlog`` counts visible thin-but-usable legacy records at selection
    time; ``selected`` counts repair rows the stage reached (automatic or
    explicitly named); ``attempted``/``improved``/``deferred_budget``/
    ``unresolved_insufficient_evidence`` mirror the grounded repair report
    over those rows.
    """
    from datetime import date as _date

    try:
        today = _date.fromisoformat(str(operation_date).strip())
    except ValueError:
        today = _date.today()
    hidden = {
        str(item.get("identity_key", "")).strip()
        for item in visibility_records
        if isinstance(item, Mapping) and str(item.get("visibility", "")).strip().casefold() == "hidden"
    }
    visible = [
        record
        for record in history
        if isinstance(record, Mapping) and str(record.get("identity_key", "")).strip() not in hidden
    ]
    partition = select_due_repairs(visible, today)
    rows_by_identity = {str(row.get("identity_key", "")).strip(): row for row in rows if isinstance(row, Mapping)}
    cooling_ids = {
        str(item.get("identity_key", "")).strip() for item in partition["cooling_down"] if isinstance(item, Mapping)
    }
    exhausted_ids = {
        str(item.get("identity_key", "")).strip() for item in partition["exhausted"] if isinstance(item, Mapping)
    }
    attempted = 0
    improved = 0
    deferred_budget = 0
    unresolved = 0
    for identity_key in repair_selected_ids:
        row = rows_by_identity.get(identity_key)
        if row is None:
            continue
        decision = str(row.get("decision", ""))
        if decision == DECISION_NOT_ATTEMPTED:
            deferred_budget += 1
            continue
        if decision in (DECISION_SKIPPED, DECISION_EVIDENCE_UNCHANGED):
            continue
        attempted += 1
        application = row.get("application", {})
        if isinstance(application, Mapping) and int(application.get("applied", 0) or 0) > 0:
            improved += 1
        elif decision in (DECISION_FALLBACK, DECISION_INVALID) and str(row.get("decision_reason", "")) in (
            REPAIR_EVIDENCE_SHORTFALL_REASONS
        ):
            unresolved += 1
    return {
        "backlog": len(partition["due"]) + len(partition["cooling_down"]) + len(partition["exhausted"]),
        "selected": len(repair_selected_ids),
        "attempted": attempted,
        "improved": improved,
        "deferred_budget": deferred_budget,
        # Named forcing can admit a cooling or exhausted record explicitly;
        # deferral counts only records the stage never reached.
        "deferred_cooldown": len(cooling_ids - repair_selected_ids),
        "deferred_exhausted": len(exhausted_ids - repair_selected_ids),
        "unresolved_insufficient_evidence": unresolved,
    }


def project_adjudicated_descriptions(
    history: Sequence[Mapping[str, Any]],
    output_dir: Path,
    store: StateStore,
) -> dict[str, Any]:
    """Project applied history descriptions onto the candidate snapshots (#671).

    The shared projection lives in ``description_projection``; the
    description backfill command uses the same one (issue #912).
    """
    return project_history_onto_snapshots(history, output_dir, store)


def _row_evidence_rank(row: Mapping[str, Any]) -> int:
    """Rank one adjudication row by how much it gives the record."""
    decision = str(row.get("decision", ""))
    if decision == DECISION_APPLY_TWO_SOURCE:
        return 4
    if decision == DECISION_APPLY_SINGLE_SOURCE:
        return 3
    if decision == DECISION_APPLY_UNVERIFIED:
        return 2
    if decision == DECISION_FALLBACK:
        artifact = row.get("artifact", {})
        retained = isinstance(artifact, Mapping) and bool(artifact.get("research_sources"))
        return 1 if retained else 0
    return -1


def _deep_row_improves(first: Mapping[str, Any], deep: Mapping[str, Any]) -> bool:
    """True when the deeper call's row should replace the first call's row.

    The deeper call wins only when it gives the record strictly more: a
    description, or verified evidence where the first call retained none.
    A tie keeps the first row, so a second decline never rewrites the
    reason the first call already reported. One decline is itself a
    finding: a deeper call that learns the book is not yet published wins
    over a first call that retained no more than it did, so the record is
    marked and not searched again next week (issue #791).
    """
    first_rank, deep_rank = _row_evidence_rank(first), _row_evidence_rank(deep)
    if deep_rank > first_rank:
        return True
    return (
        deep_rank == first_rank
        and str(deep.get("decision_reason", "")) == REASON_NOT_YET_PUBLISHED
        and str(first.get("decision_reason", "")) != REASON_NOT_YET_PUBLISHED
    )


def _forced_repair_work_item(record: dict[str, Any]) -> dict[str, Any]:
    """Build an explicitly forced repair work item from a history record.

    Named selection names the record the editor wants retried, so the
    item carries the same safe metadata-only shape as a selector-built
    item (scalar identity fields only, never source bodies) plus the
    ``depth_repair`` marker. The existing named-selection force already
    bypasses the evidence-unchanged gate for every named item; the marker
    additionally routes the row into repair accounting.
    """
    metadata: dict[str, Any] = {}
    for field_name in ("title", "publisher", "publication_year", "candidate_category", "short_description"):
        value = record.get(field_name)
        if isinstance(value, str) and value.strip():
            metadata[field_name] = value.strip()[:500]
    authors = record.get("primary_authors")
    if isinstance(authors, list):
        kept = [str(author).strip()[:200] for author in authors if str(author).strip()][:10]
        if kept:
            metadata["primary_authors"] = kept
    return {
        "identity_key": str(record.get("identity_key", "")),
        "title": str(record.get("title", "")),
        "metadata": metadata,
        "replaces_existing_description": True,
        "depth_repair": True,
    }


def _lifecycle_bands_by_identity(output_dir: Path) -> dict[str, str]:
    """Map identity key to lifecycle band from this run's candidates (#699).

    Candidate History carries no ``update_delta`` — the label describes one
    run's appearance, not durable state — so the band comes from the run's
    published ``scouting-candidates.json``. An identity absent from that
    file was not surfaced this run, which is exactly ``previously_seen``,
    so a missing or unreadable file degrades to "everything previously
    seen": the stage then orders as it did before rather than failing.
    """
    try:
        value = read_json_or_default(output_dir / "scouting-candidates.json", [])
    except (OSError, TypeError, ValueError):
        return {}
    if not isinstance(value, list):
        return {}
    bands: dict[str, str] = {}
    for item in value:
        if not isinstance(item, Mapping):
            continue
        identity_key = str(item.get("identity_key", "")).strip()
        if identity_key:
            bands[identity_key] = lifecycle_band(item)
    return bands


def _order_work_items_fresh_first_with_repairs(
    work_items: list[dict[str, Any]],
    history: Sequence[Mapping[str, Any]],
    lifecycle_bands: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Order by lifecycle band, then fresh work, then repair fairness.

    Lifecycle band is the outermost key (issue #699): new candidates are
    attempted before updated ones, and previously-seen candidates only
    with budget left over. Nothing is filtered — a previously-seen item
    keeps its place in the worklist, just behind the rows the editor
    reads. An absent band map orders exactly as before.

    Within a band, fresh work precedes repairs, fewest-attempts-first
    (#675). Work items carry only a ``depth_repair`` marker, so fairness
    sorts by the corresponding history record's shared repair key. Fresh
    items keep selector order; repairs follow fewest-attempts-first with
    an identity tie-break for determinism. Items without a history match
    sort last among repairs by identity key rather than crashing the run.
    """
    history_by_identity = {
        str(record.get("identity_key", "")).strip(): record
        for record in history
        if isinstance(record, Mapping) and str(record.get("identity_key", "")).strip()
    }

    def _repair_key(item: Mapping[str, Any]) -> tuple[tuple[int, str, str], str]:
        identity_key = str(item.get("identity_key", "")).strip()
        record = history_by_identity.get(identity_key)
        if record is None:
            return ((10**9, "", ""), identity_key)
        return (repair_sort_key(record), identity_key)

    # Unsourced stubs sit between the two (issue #788): a book with no text
    # at all comes first, a guess from the title next, and a sourced but
    # short description last. The repair policy does not schedule stubs on
    # this route, so nothing parks one; the same fairness key orders them
    # instead. Every stub attempt is logged, so a stub that keeps failing
    # sinks behind the ones not tried yet and the candidate cap cannot
    # admit the same prefix every week.
    fresh = [item for item in work_items if not item.get("depth_repair") and not item.get("unsourced_stub")]
    stubs = sorted((item for item in work_items if item.get("unsourced_stub")), key=_repair_key)
    repairs = sorted(
        (item for item in work_items if item.get("depth_repair") and not item.get("unsourced_stub")),
        key=_repair_key,
    )
    ordered = [*fresh, *stubs, *repairs]
    if not lifecycle_bands:
        return ordered
    # Stable: each band keeps the fresh-then-repairs order established
    # above, so banding adds a key without disturbing repair fairness.
    return sorted(
        ordered,
        key=lambda item: band_rank(
            lifecycle_bands.get(str(item.get("identity_key", "")).strip(), BAND_PREVIOUSLY_SEEN)
        ),
    )


def adjudicate_descriptions(
    state_dir: Path,
    output_dir: Path,
    *,
    limit: int | None = None,
    only: Sequence[str] | None = None,
    only_file: Path | None = None,
    effective_date: str | None = None,
    adapter: DescriptionAdjudicationAdapter | None = None,
    adapter_kind: str = "replay",
    preview_file: Path | None = None,
    stage_file: Path | None = None,
    fetcher: BoundedHTTPFetcher | None = None,
    classifier: SourceClassifier | None = None,
    apply: bool = False,
    stage_budget_seconds: float = ADJUDICATION_STAGE_BUDGET_SECONDS,
    max_workers: int = ADJUDICATION_MAX_WORKERS,
    monotonic: Callable[[], float] = time.monotonic,
    call_monotonic: Callable[[], float] = time.monotonic,
    operation_id: str | None = None,
    search_hints: Mapping[str, Sequence[str]] | None = None,
    snapshot_tracker: CandidateProgressSnapshot | None = None,
    deep_pass: bool = False,
    state_store: StateStore | None = None,
) -> dict[str, Any]:
    """Run the adjudication preview, and optionally apply accepted descriptions.

    Canonical state is read and written through *state_store*, the run's
    store, or a store opened over *state_dir*.

    Candidate selection reuses :func:`prepare_backfill_work_items`: no second
    selector is added. Before the adapter is called for a candidate, the
    retained evidence digests are compared against freshly fetched pages:
    when every cited page is unchanged the existing ``evidence_unchanged``
    outcome is reported, the agent is not called, and the text is not
    changed (issue #639). Every response that fails the schema is rejected
    with a bounded reason and does not stop the command. Every well-formed
    response is verified in code: each citation is canonicalized,
    re-fetched, checked for its printed title and authors, assigned a
    code-computed owner, and digested. Accepted descriptions are written
    through the existing description apply gate with ``agent-adjudicated``
    provenance and ``allow_overwrite_valid=False``: no second write path is
    added. Without ``apply`` the command writes its preview and changes
    nothing in the state directory. Verification re-fetches cited pages; pass a fetcher with a
    fake transport to stay offline.

    ``adapter_kind`` selects the adapter when no explicit ``adapter`` is
    injected: ``replay`` (the default, and the only kind the offline fixture
    run may reach) or ``codex`` (the production CLI adapter, chosen
    explicitly). An unknown kind is a command input error.

    Throughput (issue #651): the per-candidate loop runs on a bounded
    ``DaemonThreadPool`` at this stage's own worker count, sharing the one
    fetcher instance so per-host spacing stays intact. Only the
    per-candidate body runs in parallel; the sort, the apply-gate pass, and
    policy merging stay sequential, so the preview artifact is byte-identical
    whatever order candidates complete in. The stage takes a budget: a
    deadline is fixed once at stage start, and admission stops when the
    remaining time cannot fit one more call, reserving the existing
    per-call timeout rather than a second tuned number. Candidates that are
    never attempted are reported with ``not_attempted_stage_budget``, not
    dropped.

    Grounded search hints (issue #684): ``search_hints`` maps an identity
    key to URL-only starting points from the grounded stage. When ``None``
    (the default) hints load best-effort from the grounded stage artifact
    in the output directory; a missing artifact means no hints, never an
    error. Pass an explicit empty mapping to disable hints. Hints travel
    on the work item as ``search_hints`` and are never evidence:
    verification still re-fetches and checks every cited page in code.

    Live progress (issue #706): ``snapshot_tracker`` receives one
    per-candidate row (initial/begin/finish/final) published through the
    existing snapshot heartbeat, so the operator sees counts and running
    titles while the worker pool is busy. ``None`` (the default) keeps the
    stage silent, exactly as before.

    Deeper second call (issue #788): with ``deep_pass``, a book the first
    call leaves at the literal placeholder gets one more call whose work
    item carries ``search_depth="deep"``. The production adapter turns that
    into a search by ISBN and original-language title, publisher and
    bookshop pages first. The weekly stage enables it; the default omits
    the deeper pass. A timed-out call is retried once after the worklist,
    only when the stage can still reserve a full call. Call latency uses
    its own clock seam so instrumentation leaves admission checks alone.
    """
    from autoscout.generate_descriptions import _resolve_named_selection

    named_selection = only is not None or only_file is not None
    if named_selection and limit is not None:
        raise ValueError("--limit cannot be combined with --only or --only-file")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be a positive integer")
    if max_workers <= 0:
        raise ValueError("max_workers must be positive")
    # The stage clock: one deadline fixed once at stage start. Admission
    # stops when the remaining time cannot fit one more call.
    stage_deadline = monotonic() + float(stage_budget_seconds)

    store = state_store if state_store is not None else open_state_store(state_dir)
    raw_history = store.read(Collection.CANDIDATE_HISTORY)
    if not isinstance(raw_history, list) or not all(isinstance(item, Mapping) for item in raw_history):
        raise ValueError("Candidate History must be a list of objects")
    history = [dict(item) for item in raw_history]
    visibility_records = store.read(Collection.CANDIDATE_VISIBILITY)
    if not isinstance(visibility_records, list):
        visibility_records = []
    operation_date = effective_date or date.today().isoformat()

    # The residual route selects retained thin descriptions under the
    # shared repair policy (issue #675): due repairs join the worklist
    # with a depth_repair marker, while the legacy default still selects
    # missing text only. Every unsourced stub joins it too, every run
    # (issue #788): the deterministic stage fails on most of them, and a
    # stub that clears the depth floor was selected by no rule at all.
    resolved_hints: Mapping[str, Any] | None = search_hints
    if resolved_hints is None:
        try:
            resolved_hints = load_grounded_search_hints(output_dir)
        except (OSError, TypeError, ValueError):
            resolved_hints = {}
    work_items = prepare_backfill_work_items(
        history,
        visibility_records=visibility_records,
        include_depth_repair=True,
        include_unsourced_stubs=True,
        repair_as_of=operation_date,
        search_hints=resolved_hints,
        # An operator who names a record gets it searched now, whatever
        # its re-check date.
        include_awaiting_publication=named_selection,
    )
    deferred_items: list[dict[str, Any]] = []
    named_non_work_records: list[dict[str, Any]] = []
    named_forced_repair_keys: set[str] = set()
    if named_selection:
        selected = _resolve_named_selection(history, only=only, only_file=only_file)
        selected_keys = {str(record.get("identity_key", "")).strip() for record in selected}
        work_items = [item for item in work_items if str(item.get("identity_key", "")).strip() in selected_keys]
        work_keys = {str(item.get("identity_key", "")).strip() for item in work_items}
        named_non_work_records = [
            dict(record) for record in selected if str(record.get("identity_key", "")).strip() not in work_keys
        ]
        # Explicit selection forces repair timing (cooldown and
        # evidence-reuse), exactly like the existing named-selection force
        # for evidence-unchanged rows — but the attempt still counts toward
        # the shared exhaustion limit, so explicit retries cannot bypass
        # retry limits through this stage either (issue #675). Hidden and
        # editor-corrected records are never forced.
        hidden_keys = {
            str(item.get("identity_key", "")).strip()
            for item in visibility_records
            if isinstance(item, Mapping) and str(item.get("visibility", "")).strip().casefold() == "hidden"
        }
        for record in selected:
            identity_key = str(record.get("identity_key", "")).strip()
            if identity_key in work_keys or not identity_key:
                continue
            if identity_key in hidden_keys:
                continue
            if is_repair_candidate(dict(record)):
                work_items.append(_forced_repair_work_item(dict(record)))
                named_forced_repair_keys.add(identity_key)
                named_non_work_records = [
                    item for item in named_non_work_records if str(item.get("identity_key", "")).strip() != identity_key
                ]
        # Named selection keeps selector order: the operator named these
        # rows explicitly, so fairness reordering must not override that
        # intent. Budget order for named rows follows selection order.
    else:
        # Fresh-work-first / fewest-attempts-first before --limit and
        # budget admission (review finding on #674/#675): without this,
        # history order lets a previously-failed repair win a limit slot
        # over an untouched one. Fresh items keep selector order; repairs
        # follow fewest-attempts-first, mirroring weekly admission.
        # Lifecycle band outermost (issue #699): a --limit slot or the
        # stage clock must reach every new candidate before it starts a
        # previously-seen one.
        work_items = _order_work_items_fresh_first_with_repairs(
            work_items, history, _lifecycle_bands_by_identity(output_dir)
        )
        if limit is not None:
            deferred_items = work_items[limit:]
            work_items = work_items[:limit]

    # Grounded hints cover forced-repair items too: they bypass the selector
    # above, so attach here rather than only inside it (issue #684).
    _attach_grounded_search_hints(work_items, resolved_hints)

    candidate_id_by_identity = {_candidate_identity_key(record): _candidate_id(record) for record in history}
    record_by_identity = {_candidate_identity_key(record): record for record in history}

    if snapshot_tracker is not None:

        def _tracker_item(item: Mapping[str, Any]) -> tuple[str, str]:
            identity_key = str(item.get("identity_key", "")).strip()
            record = record_by_identity.get(identity_key) or {}
            return identity_key, str(record.get("title", "") or "")

        snapshot_tracker.initial([_tracker_item(item) for item in work_items])

    if adapter is not None:
        active_adapter: DescriptionAdjudicationAdapter = adapter
    elif adapter_kind == "replay":
        active_adapter = ReplayDescriptionAdjudicationAdapter()
    elif adapter_kind == "codex":
        active_adapter = CodexDescriptionAdjudicationAdapter()
    else:
        raise ValueError(f"unknown adapter kind: {adapter_kind!r}")
    active_fetcher = fetcher or BoundedHTTPFetcher()
    active_classifier = classifier or committed_source_classifier()
    call_seconds: list[float] = []
    timeout_count = 0
    call_lock = threading.Lock()
    timeout_retries: dict[str, Callable[[], None]] = {}

    def _not_attempted_row(candidate_id: str, identity_key: str) -> dict[str, Any]:
        """Report one candidate the stage clock never reached.

        The existing ``not_attempted_stage_budget`` reason is reused rather
        than a new code invented. The row carries no response and no
        artifact, so the apply gate leaves it alone exactly like an
        invalid, skipped, or evidence-unchanged row.
        """
        return {
            "candidate_id": candidate_id,
            "identity_key": identity_key,
            "response": {},
            "reason": REASON_NOT_ATTEMPTED_STAGE_BUDGET,
            "verification": {},
            "decision": DECISION_NOT_ATTEMPTED,
            "decision_reason": REASON_NOT_ATTEMPTED_STAGE_BUDGET,
            "artifact": {},
        }

    def _adjudicate_one(item: Mapping[str, Any]) -> dict[str, Any]:
        # The stage clock is checked again inside the worker, so a long run
        # stops starting new candidates when the remaining time cannot fit
        # one more call. The reservation is the existing per-call timeout,
        # not a second tuned number. A candidate that is admitted and then
        # times out is already a bounded per-candidate failure below.
        if monotonic() + ADJUDICATION_CALL_TIMEOUT_SECONDS > stage_deadline:
            identity_key = str(item.get("identity_key", "")).strip()
            candidate_id = candidate_id_by_identity.get(identity_key, "")
            return _not_attempted_row(candidate_id, identity_key)
        identity_key = str(item.get("identity_key", "")).strip()
        candidate_id = candidate_id_by_identity.get(identity_key, "")
        target_record = record_by_identity.get(identity_key)
        # Unverified text only fills a missing description or replaces a
        # guess from the title; it never competes with usable text.
        allow_unverified = target_record is not None and (
            not record_has_usable_description(target_record) or is_unsourced_stub(target_record)
        )
        # Only an ISBN that may prove identity: a learned, discovery-only
        # ISBN stays a search key (issue #531).
        candidate_isbn = identity_isbn(dict(target_record)) if target_record is not None else ""
        may_await_publication = target_record is not None and _record_may_await_publication(
            target_record, operation_date
        )

        def _call_agent(call_item: Mapping[str, Any]) -> dict[str, Any]:
            """Run one agent call and plan its application; never raises."""
            nonlocal timeout_count
            call_started = call_monotonic()
            timed_out = False
            try:
                raw_response = active_adapter.adjudicate(call_item)
            except Exception as exc:
                timed_out = isinstance(exc, CodexAdjudicationTimeoutError)
                raw_response = None
                failure = f"adjudication adapter failure: {type(exc).__name__}"
                if isinstance(exc, CodexAdjudicationAdapterError) and str(exc):
                    # The closed adapter errors carry only fixed text or an
                    # integer from this module - an exit code, a timeout
                    # length - so the detail is safe to retain and tells an
                    # operator which failure happened.
                    failure = f"{failure} ({str(exc)[:MAX_ADJUDICATION_REASON_LENGTH]})"
            else:
                failure = ""
            elapsed = max(0.0, call_monotonic() - call_started)
            with call_lock:
                call_seconds.append(elapsed)
                timeout_count += int(timed_out)
            if raw_response is None:
                valid, reason = False, failure or "adjudication adapter returned no response"
            else:
                valid, reason = validate_adjudication_response(raw_response)
            if valid:
                assert isinstance(raw_response, Mapping)
                # Both halves of the identity must hold: the canonical identity
                # key the adapter answered with, and a Candidate ID that history
                # actually carries for that key. A record with no Candidate ID
                # cannot be identified exactly, so it is never applied (#637).
                identity_matched = bool(candidate_id) and str(raw_response.get("identity_key", "")) == identity_key
                response: dict[str, Any] = _bounded_response(raw_response, identity_key=identity_key)
                row_reason = ""
                verification = verify_adjudication_response(
                    response,
                    candidate_id=candidate_id,
                    identity_key=identity_key,
                    fetcher=active_fetcher,
                    classifier=active_classifier,
                    checked_date=operation_date,
                    candidate_isbn=candidate_isbn,
                )
                plan = plan_adjudication_application(
                    response,
                    verification,
                    identity_matched=identity_matched,
                    candidate_id=candidate_id,
                    allow_unverified=allow_unverified,
                    may_await_publication=may_await_publication,
                )
            else:
                response = {}
                row_reason = (failure or reason)[:MAX_ADJUDICATION_REASON_LENGTH]
                verification = {}
                plan = {"decision": DECISION_INVALID, "reason": row_reason, "artifact": {}}
            return {
                "candidate_id": candidate_id,
                "identity_key": identity_key,
                "response": response,
                "reason": row_reason,
                "verification": verification,
                "decision": plan["decision"],
                "decision_reason": plan["reason"],
                "artifact": plan["artifact"],
                "_timed_out": timed_out,
            }

        def _deep_follow_up(row: dict[str, Any], *, allow_retry: bool) -> dict[str, Any]:
            """Deeper second call (issue #788) after a finished first call.

            A book the first call leaves at the literal placeholder gets one
            more search by ISBN and original-language title, publisher and
            bookshop pages first. Only a record with no usable text
            qualifies, so a stub or a short description is never searched
            twice in one run. The stage clock admits it by the same
            reservation as a first call. A first call recovered by a timeout
            retry comes here too (issue #865), but its own deeper call is not
            retried again.
            """
            if not (
                deep_pass
                and str(row["decision"]) in (DECISION_FALLBACK, DECISION_INVALID)
                # A book that is not yet published has no page a deeper
                # search could find.
                and str(row["decision_reason"]) != REASON_NOT_YET_PUBLISHED
                and record is not None
                and not record_has_usable_description(record)
                and monotonic() + ADJUDICATION_CALL_TIMEOUT_SECONDS <= stage_deadline
            ):
                return row
            deep_row = _call_agent({**item, SEARCH_DEPTH_FIELD: SEARCH_DEPTH_DEEP})
            deep_summary = {
                "first_decision": str(row["decision"]),
                "first_decision_reason": str(row["decision_reason"])[:MAX_ADJUDICATION_REASON_LENGTH],
                "decision": str(deep_row["decision"]),
                # Both calls keep their own reason, whichever one supplies
                # the row: a deeper call that times out must stay visible
                # beside a first call that merely declined.
                "decision_reason": str(deep_row["decision_reason"])[:MAX_ADJUDICATION_REASON_LENGTH],
            }
            if _deep_row_improves(row, deep_row):
                row = deep_row
            row["deep_pass"] = deep_summary
            if deep_row["_timed_out"] and allow_retry:
                _defer_retry({**item, SEARCH_DEPTH_FIELD: SEARCH_DEPTH_DEEP}, row, deep=True)
            return row

        def _defer_retry(
            call_item: Mapping[str, Any], final_row: dict[str, Any], *, deep: bool = False, deep_only: bool = False
        ) -> None:
            def retry() -> None:
                if monotonic() + ADJUDICATION_CALL_TIMEOUT_SECONDS > stage_deadline:
                    return
                # The candidate stays running until _retry_tracked finishes it,
                # after any deeper follow-up the recovered call may start.
                if snapshot_tracker is not None:
                    snapshot_tracker.begin(identity_key)
                retried = _call_agent(call_item)
                if deep:
                    summary = final_row["deep_pass"]
                    summary["decision"] = str(retried["decision"])
                    summary["decision_reason"] = str(retried["decision_reason"])[:MAX_ADJUDICATION_REASON_LENGTH]
                    if deep_only or _deep_row_improves(final_row, retried):
                        final_row.update(retried)
                        final_row["deep_pass"] = summary
                else:
                    if not retried["_timed_out"]:
                        retried = _deep_follow_up(retried, allow_retry=False)
                    final_row.update(retried)
                final_row["timeout_retry"] = {"decision": str(retried["decision"])}

            with call_lock:
                timeout_retries[identity_key] = retry

        # Evidence-unchanged gate: before calling the agent, re-fetch the
        # retained cited pages and compare each fresh evidence digest
        # against the retained one. When every cited page is unchanged, the
        # existing outcome is reported, the agent is not called, and the
        # text is not changed. Any changed or unfetchable page makes the
        # candidate eligible again (issue #639).
        record = record_by_identity.get(identity_key)
        # A due depth repair always reaches the agent: unchanged cited
        # pages must not suppress the rewrite the record was admitted for
        # (issue #675). A successful repair writes fresh evidence basis and
        # digests, so normal evidence reuse resumes by construction.
        repair_forced = bool(item.get("depth_repair") is True)
        # A record selected with a not-yet-published date is past its
        # re-check date, and the question is whether new pages exist now.
        # Unchanged old pages cannot answer that, so the agent is called.
        publication_recheck = record is not None and not_yet_published_date(record) is not None
        if record is not None and not repair_forced and not publication_recheck:
            unchanged, unchanged_urls = _adjudication_evidence_unchanged(
                record,
                fetcher=active_fetcher,
                classifier=active_classifier,
                forced=named_selection,
                checked_date=operation_date,
            )
            if (
                unchanged
                and deep_pass
                and not str(record.get(DEEP_SEARCH_DATE_FIELD, "") or "").strip()
                # Re-fetching the retained pages spent stage time, so the
                # clock is checked again; a call that no longer fits waits
                # for the next run behind the ordinary unchanged row.
                and monotonic() + ADJUDICATION_CALL_TIMEOUT_SECONDS <= stage_deadline
            ):
                # The retained decline came from the first search alone. Its
                # verdict is reused, so only the deeper call runs, once; the
                # date written afterwards lets evidence reuse resume.
                deep_row = _call_agent({**item, SEARCH_DEPTH_FIELD: SEARCH_DEPTH_DEEP})
                deep_row["deep_pass"] = {
                    "first_decision": DECISION_EVIDENCE_UNCHANGED,
                    "first_decision_reason": REASON_EVIDENCE_UNCHANGED,
                    "decision": str(deep_row["decision"]),
                    "decision_reason": str(deep_row["decision_reason"])[:MAX_ADJUDICATION_REASON_LENGTH],
                }
                if deep_row["_timed_out"]:
                    _defer_retry({**item, SEARCH_DEPTH_FIELD: SEARCH_DEPTH_DEEP}, deep_row, deep=True, deep_only=True)
                return deep_row
            if unchanged:
                return {
                    "candidate_id": candidate_id,
                    "identity_key": identity_key,
                    "response": {},
                    "reason": "",
                    "verification": {
                        "candidate_id": candidate_id,
                        "identity_key": identity_key,
                        "evidence_unchanged": True,
                        "evidence_urls": unchanged_urls,
                        "reason": REASON_EVIDENCE_UNCHANGED,
                    },
                    "decision": DECISION_EVIDENCE_UNCHANGED,
                    "decision_reason": REASON_EVIDENCE_UNCHANGED,
                    "artifact": {},
                }

        row = _call_agent(item)
        if row["_timed_out"]:
            _defer_retry(item, row)
            return row
        return _deep_follow_up(row, allow_retry=True)

    # Every candidate is submitted, and the single admission check lives in
    # the worker. Checking again before submitting would be a second copy of
    # the same predicate that could drift from it, and it would decide
    # nothing: submission is fast enough that the clock barely moves, so a
    # pre-submit check either admits the whole worklist or none of it, and
    # the none case already produces exactly these rows one per worker.
    # The per-candidate body shares the one fetcher instance, so
    # verification re-fetches and the evidence-unchanged gate stay spaced
    # under parallel candidates (issue #651).
    def _adjudicate_one_tracked(item: Mapping[str, Any]) -> dict[str, Any]:
        """Report one candidate to the heartbeat tracker around the body."""
        if snapshot_tracker is None:
            return _adjudicate_one(item)
        tracker_key = str(item.get("identity_key", "")).strip()
        snapshot_tracker.begin(tracker_key)
        try:
            return _adjudicate_one(item)
        finally:
            if tracker_key not in timeout_retries:
                snapshot_tracker.finish(tracker_key)

    rows: list[dict[str, Any]] = []
    if work_items:
        pool = DaemonThreadPool(max(1, int(max_workers)), thread_name_prefix="autoscout-description-adjudication-")
        try:
            futures = [pool.submit(_adjudicate_one_tracked, item) for item in work_items]
            try:
                collected = (
                    snapshot_tracker.collect(futures) if snapshot_tracker is not None else [f.result() for f in futures]
                )
            except Exception:
                if snapshot_tracker is not None:
                    snapshot_tracker.final(status="failed")
                raise
            rows.extend(collected)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    # Fresh candidates get the stage budget first. Each timed-out call gets
    # one exact replay afterwards, with the same full-call reservation. The
    # replays run on a pool collected through the tracker, so the heartbeat
    # keeps polling through a call that may take the full 240 s.
    def _retry_tracked(identity_key: str) -> None:
        try:
            timeout_retries[identity_key]()
        finally:
            if snapshot_tracker is not None:
                snapshot_tracker.finish(identity_key)

    if timeout_retries:
        retry_pool = DaemonThreadPool(
            max(1, int(max_workers)), thread_name_prefix="autoscout-description-adjudication-retry-"
        )
        try:
            # Retries keep the worklist's priority order (new books first,
            # repair fairness, named selection), so a short budget spends
            # its last full calls where the first pass would have.
            worklist_order = {str(item.get("identity_key", "")).strip(): index for index, item in enumerate(work_items)}
            retry_keys = sorted(timeout_retries, key=lambda key: (worklist_order.get(key, len(worklist_order)), key))
            retry_futures = [retry_pool.submit(_retry_tracked, key) for key in retry_keys]
            try:
                if snapshot_tracker is not None:
                    snapshot_tracker.collect(retry_futures)
                else:
                    for future in retry_futures:
                        future.result()
            except Exception:
                if snapshot_tracker is not None:
                    snapshot_tracker.final(status="failed")
                raise
        finally:
            retry_pool.shutdown(wait=False, cancel_futures=True)
    for row in rows:
        row.pop("_timed_out", None)
    if snapshot_tracker is not None:
        snapshot_tracker.final()
    for record in named_non_work_records:
        # An explicitly named candidate that needs no description work is
        # reported as skipped. The adapter is never consulted and the
        # record is never touched, so an editor-corrected or valid
        # description is preserved byte for byte by construction.
        rows.append(
            {
                "candidate_id": _candidate_id(record),
                "identity_key": _candidate_identity_key(record),
                "response": {},
                "reason": "",
                "verification": {},
                "decision": DECISION_SKIPPED,
                "decision_reason": _skip_reason_for_record(record),
                "artifact": {},
            }
        )
    rows.sort(key=lambda row: (row["candidate_id"], row["identity_key"]))

    # The preview runs every row through the real apply gate on a copy of
    # history first, so the preview shows exactly what applying would do.
    # The confirmed run repeats the same per-candidate calls on the real
    # history; only then is Candidate History written.
    gate_history: list[dict[str, Any]] = copy.deepcopy(history)
    for row in rows:
        decision = str(row["decision"])
        if decision in (
            DECISION_INVALID,
            DECISION_SKIPPED,
            DECISION_EVIDENCE_UNCHANGED,
            DECISION_NOT_ATTEMPTED,
        ):
            row["application"] = {
                "applied": 0,
                "skipped": 1 if decision == DECISION_SKIPPED else 0,
                "invalid": 1 if decision == DECISION_INVALID else 0,
                "fallback": 0,
                "unknown_identity": 0,
            }
            row["applied_detail"] = {}
            continue
        artifact = row["artifact"]
        if _fallback_would_only_overwrite_the_earlier_reason(decision, artifact, gate_history):
            row["application"] = {
                "applied": 0,
                "skipped": 0,
                "invalid": 0,
                "fallback": 1,
                "unknown_identity": 0,
            }
            row["applied_detail"] = {}
            continue
        gate_history, outcome = apply_description_artifacts(
            gate_history,
            [artifact],
            provenance=(
                ADJUDICATION_UNVERIFIED_PROVENANCE if decision == DECISION_APPLY_UNVERIFIED else ADJUDICATION_PROVENANCE
            ),
            effective_date=operation_date,
            allow_overwrite_valid=False,
            # Scoped repair permission (issue #675): a qualifying repair
            # replacement may overwrite thin usable text of an approved
            # legacy provenance. Every other valid description stays
            # protected — this flag enables no general overwrite.
            allow_repair_replacement=True,
            # A verified description replaces an unsourced stub of any
            # length; a rejected one leaves the stub in place (issue #788).
            allow_unsourced_replacement=True,
        )
        row["application"] = outcome
        row["applied_detail"] = _applied_detail(row, operation_date) if outcome.get("applied") else {}

    # Repair accounting (issue #675): rows admitted as depth repairs —
    # automatic (depth_repair work items) or explicitly named — log one
    # shared repair attempt when the agent was actually called, so
    # cross-stage retries obey the same cooldown and exhaustion limits as
    # grounded repairs. The log lands on the gate history, which the
    # confirmed run persists and the preview discards.
    repair_selected_ids = {
        str(item.get("identity_key", "")).strip() for item in work_items if item.get("depth_repair") is True
    } | set(named_forced_repair_keys)
    # A stub that clears the depth floor is not a repair, but its attempt
    # is logged the same way (issue #788): the log is the only memory that
    # lets the worklist order rotate stubs under the candidate cap. It
    # stays out of the repair summary, which counts depth repairs only.
    attempt_logged_ids = repair_selected_ids | {
        str(item.get("identity_key", "")).strip() for item in work_items if item.get("unsourced_stub") is True
    }
    gate_history_by_identity = {
        str(record.get("identity_key", "")).strip(): record
        for record in gate_history
        if isinstance(record, Mapping) and str(record.get("identity_key", "")).strip()
    }
    for row in rows:
        identity_key = str(row.get("identity_key", "")).strip()
        if identity_key not in attempt_logged_ids:
            continue
        if str(row.get("decision", "")) in (
            DECISION_SKIPPED,
            DECISION_EVIDENCE_UNCHANGED,
            DECISION_NOT_ATTEMPTED,
        ):
            continue
        target_record = gate_history_by_identity.get(identity_key)
        if target_record is None:
            continue
        if str(row.get("decision", "")) in ADJUDICATION_APPLY_DECISIONS:
            repair_outcome, repair_reason = "applied", ""
        else:
            repair_outcome, repair_reason = OUTCOME_ATTEMPTED_NO_DESCRIPTION, str(row.get("decision_reason", ""))
        record_repair_attempt(
            target_record,
            operation_date=operation_date,
            operation_id=str(operation_id or "").strip(),
            outcome=repair_outcome,
            reason_code=repair_reason,
            stage="adjudication",
        )

    # The deep-search date belongs to the verdict the record now holds. It
    # is written only when the deeper call answered, and every other agent
    # call clears it: a timed-out or invalid deeper call, and a first call
    # the stage clock left without its deeper call. Otherwise an old date
    # would outlive the evidence it was written for, and the fresh evidence
    # the first call just retained would match itself with the deeper
    # search still owed. Rows that called no agent leave it alone.
    for row in rows:
        if str(row.get("decision", "")) in (
            DECISION_SKIPPED,
            DECISION_EVIDENCE_UNCHANGED,
            DECISION_NOT_ATTEMPTED,
        ):
            continue
        target_record = gate_history_by_identity.get(str(row.get("identity_key", "")).strip())
        if target_record is None:
            continue
        deep_summary = row.get("deep_pass")
        if isinstance(deep_summary, Mapping) and str(deep_summary.get("decision", "")).strip() != DECISION_INVALID:
            target_record[DEEP_SEARCH_DATE_FIELD] = operation_date
        else:
            target_record.pop(DEEP_SEARCH_DATE_FIELD, None)
        # The not-yet-published date belongs to this run's verdict in the
        # same way: the verdict writes it, and any other answer clears it,
        # so the stages search the book again next run (issue #791).
        if str(row.get("decision_reason", "")) == REASON_NOT_YET_PUBLISHED:
            target_record[DESCRIPTION_NOT_YET_PUBLISHED_DATE_FIELD] = operation_date
            # The record shows the placeholder with this reason, whatever
            # it held. The apply gate keeps a stub when a fallback arrives
            # and keeps an earlier fallback reason, but a guess from the
            # title of a book nobody has published is not worth keeping,
            # and the editor must read one state for every such book.
            if not record_has_usable_description(target_record) or is_unsourced_stub(target_record):
                identity = str(target_record.get("identity_key", "")).strip()
                if is_unsourced_stub(target_record):
                    target_record["description_provenance"] = description_provenance_record(
                        identity, DESCRIPTION_FALLBACK, "fallback", applied_date=operation_date
                    )
                    target_record.pop("grounded_outcome", None)
                    # The gate reported this row as skipped because it
                    # kept the stub. The stub is gone now, so the row and
                    # the totals read from it say what happened.
                    row["application"] = {
                        "applied": 0,
                        "skipped": 0,
                        "invalid": 0,
                        "fallback": 1,
                        "unknown_identity": 0,
                    }
                target_record["content_description"] = DESCRIPTION_FALLBACK
                target_record["content_description_rejection_reason"] = REASON_NOT_YET_PUBLISHED
        else:
            target_record.pop(DESCRIPTION_NOT_YET_PUBLISHED_DATE_FIELD, None)
            # The reason goes with the date. The apply gate keeps an
            # earlier fallback reason when the new answer retains no
            # evidence, and this one names a verdict that no longer holds.
            if str(target_record.get("content_description_rejection_reason", "")) == REASON_NOT_YET_PUBLISHED:
                target_record.pop("content_description_rejection_reason", None)

    repair_summary = _adjudication_repair_summary(
        history, visibility_records, operation_date, rows, repair_selected_ids
    )

    preview_path = preview_file or output_dir / ADJUDICATION_PREVIEW_ARTIFACT_NAME
    stage_report_path = stage_file or output_dir / ADJUDICATION_STAGE_ARTIFACT_NAME
    # Explicitly skipped rows were never adjudicated, so they count toward
    # neither valid nor rejected. ``considered`` is the adjudicated rows.
    # Evidence-unchanged rows skipped the agent by digest comparison, so
    # they reuse the existing outcome under its own count (issue #639).
    # Not-attempted rows never started because the stage clock could not fit
    # one more call, so they reuse the existing reason under their own count
    # rather than counting as rejected (issue #651).
    valid_count = sum(1 for row in rows if str(row["decision"]) in (*ADJUDICATION_APPLY_DECISIONS, DECISION_FALLBACK))
    rejected_count = sum(1 for row in rows if str(row["decision"]) == DECISION_INVALID)
    unchanged_count = sum(1 for row in rows if str(row["decision"]) == DECISION_EVIDENCE_UNCHANGED)
    not_attempted_count = sum(1 for row in rows if str(row["decision"]) == DECISION_NOT_ATTEMPTED)
    # The gate ran on a copy of history for every row, so these counts
    # describe what applying would do. Only a run with ``apply`` actually
    # did it, so a dry run reports the same numbers under ``would_``
    # names and reports zero applied work.
    gate_applied = sum(int(row["application"].get("applied", 0)) for row in rows)
    gate_skipped = sum(int(row["application"].get("skipped", 0)) for row in rows)
    gate_fallback = sum(int(row["application"].get("fallback", 0)) for row in rows)
    resolved_operation_id = str(operation_id or "").strip()
    stage_report = build_adjudication_stage_report(
        operation_id=resolved_operation_id,
        operation_date=operation_date,
        status="ok",
        rows=rows,
        history=gate_history,
        stage_budget_seconds=float(stage_budget_seconds),
        call_timeout_seconds=float(ADJUDICATION_CALL_TIMEOUT_SECONDS),
        max_workers=int(max_workers),
        candidate_limit=limit,
        repair=repair_summary,
    )
    deferred_ids = [
        candidate_id_by_identity.get(str(item.get("identity_key", "")).strip(), "")
        or str(item.get("identity_key", "")).strip()
        for item in deferred_items
    ]
    stage_report["deferred_limit"] = {"count": len(deferred_ids), "candidate_ids": deferred_ids}
    stage_report["call_latency"] = call_latency_summary(call_seconds, timeout_count)
    preview_payload = {
        "artifact": "adjudicate-descriptions",
        "version": 1,
        "operation_date": operation_date,
        "applied": apply,
        "preview": rows,
        "counts": {
            "previewed": len(rows),
            "considered": valid_count + rejected_count,
            "valid": valid_count,
            "rejected": rejected_count,
            "applied": gate_applied if apply else 0,
            "would_apply": gate_applied,
            "skipped": gate_skipped,
            "fallback": gate_fallback,
            "evidence_unchanged": unchanged_count,
            "not_attempted": not_attempted_count,
        },
        "repair": dict(repair_summary),
    }
    base_result: dict[str, Any] = {
        "status": "ok",
        "operation_id": resolved_operation_id,
        "operation_date": operation_date,
        "considered": valid_count + rejected_count,
        "previewed": len(rows),
        "valid": valid_count,
        "rejected": rejected_count,
        "applied": gate_applied if apply else 0,
        "would_apply": gate_applied,
        "skipped": gate_skipped,
        "fallback": gate_fallback,
        "evidence_unchanged": unchanged_count,
        "not_attempted": not_attempted_count,
        "deferred_limit": len(deferred_ids),
        "preview_file": str(preview_path),
        "repair": dict(repair_summary),
        "description_adjudication_stage_report": stage_report,
    }
    # Persistence is the durable history write, and only that. A failure
    # there must stop the run at its own named stage.
    #
    # The report artifacts below are deliberately outside this guard. They
    # are diagnostics, and they are written after state is already durable,
    # so a failure to write one leaves nothing inconsistent. Calling it a
    # persistence failure would tell the editor the results were not saved
    # when they were, throw away the weekly review, and recommend repeating
    # an hour of agent work for a missing diagnostic file. A failure there
    # raises instead, and the workflow reports it as the ordinary stage
    # execution failure it is, and the run continues (issue #641).
    try:
        if apply:
            store.replace(Collection.CANDIDATE_HISTORY, gate_history)
    except Exception as exc:
        failed_report = dict(stage_report)
        failed_report["status"] = "persistence_failed"
        failed_report["failure_reason"] = f"description_adjudication_persistence_failure: {type(exc).__name__}"[
            :MAX_ADJUDICATION_REASON_LENGTH
        ]
        failed_result = dict(base_result)
        failed_result["status"] = "persistence_failed"
        failed_result["reason"] = "description_adjudication_persistence_failure"
        failed_result["description_adjudication_stage_report"] = failed_report
        return failed_result
    # Derived publication (issue #671): history is durable above, so project
    # the applied description facts onto the current/latest snapshots the
    # review and the post-enrichment refresh read. Only a confirmed run
    # projects — a preview must never publish what-if state. A failure here
    # raises as a stage execution failure, never as a persistence failure:
    # the results are saved, only the derived views are behind, and the
    # idempotent projection converges on the next run.
    description_projection: dict[str, Any] = {"current": None, "latest": None}
    if apply:
        description_projection = project_adjudicated_descriptions(gate_history, output_dir, store)
    base_result["description_projection"] = description_projection
    output_dir.mkdir(parents=True, exist_ok=True)
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(preview_path, preview_payload)
    stage_report_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(stage_report_path, stage_report)
    return base_result


class DescriptionAdjudicationStageService:
    """Stage-shaped entry seam for the weekly workflow (issue #640).

    The weekly workflow owns the canonical application boundary through
    ``adjudicate_descriptions(..., apply=True)``. This service adapts the
    per-candidate adapter seam to the stage seam the workflow calls:
    ``adjudicate_stage(output_dir, state_dir, effective_date, *,
    operation_id)``. Deterministic tests and the offline fixture run inject
    the replay adapter with a refusing fetcher; production injects the
    Codex adapter with the default bounded fetcher.
    """

    def __init__(
        self,
        *,
        adapter: DescriptionAdjudicationAdapter,
        fetcher: BoundedHTTPFetcher | None = None,
        classifier: SourceClassifier | None = None,
        limit: int | None = None,
        stage_budget_seconds: float = ADJUDICATION_STAGE_BUDGET_SECONDS,
        max_workers: int = ADJUDICATION_MAX_WORKERS,
        monotonic: Callable[[], float] = time.monotonic,
        search_hints: Mapping[str, Sequence[str]] | None = None,
        deep_pass: bool = False,
        scale_weekly_limit: bool = False,
    ) -> None:
        self._scale_weekly_limit = scale_weekly_limit
        self._deep_pass = bool(deep_pass)
        self._adapter = adapter
        self._fetcher = fetcher
        self._classifier = classifier
        self._limit = limit
        self._stage_budget_seconds = float(stage_budget_seconds)
        self._max_workers = int(max_workers)
        self._monotonic = monotonic
        self._search_hints = search_hints
        self._snapshot_progress_presentation: ProgressSnapshotPresentation | None = None
        self._snapshot_progress_failure_recorder: ProgressSnapshotFailureRecorder | None = None

    def set_snapshot_progress_presentation(self, presentation: ProgressSnapshotPresentation | None) -> None:
        """Bind the optional per-candidate snapshot heartbeat (issue #706)."""
        self._snapshot_progress_presentation = presentation

    def set_progress_snapshot_failure_recorder(self, recorder: ProgressSnapshotFailureRecorder | None) -> None:
        """Bind the optional snapshot-failure recorder (issue #706)."""
        self._snapshot_progress_failure_recorder = recorder

    def adjudicate_stage(
        self,
        output_dir: Path,
        state_dir: Path | None = None,
        effective_date: str | None = None,
        *,
        operation_id: str | None = None,
        state_store: StateStore | None = None,
    ) -> dict[str, Any]:
        resolved_state = state_dir if state_dir is not None else output_dir
        tracker: CandidateProgressSnapshot | None = None
        if self._snapshot_progress_presentation is not None or self._snapshot_progress_failure_recorder is not None:
            tracker = CandidateProgressSnapshot(
                output_dir,
                stage="description-adjudication",
                stage_budget_seconds=self._stage_budget_seconds,
                monotonic=self._monotonic,
                failure_recorder=self._snapshot_progress_failure_recorder,
                presentation=self._snapshot_progress_presentation,
            )
        limit = self._limit
        if self._scale_weekly_limit:
            from autoscout.description_stage_limits import description_stage_limits

            count = None
            try:
                artifact = json.loads((output_dir / GROUNDING_STAGE_ARTIFACT_NAME).read_text(encoding="utf-8"))
                report = artifact["report"]
                value = report["lifecycle"]["new_and_updated"]["eligible"]
                if (
                    (not operation_id or report.get("operation_id") == operation_id)
                    and type(value) is int
                    and value >= 0
                ):
                    count = value
            except (OSError, ValueError, TypeError, KeyError):
                pass
            limit = description_stage_limits(count).adjudication_candidate_limit
        return adjudicate_descriptions(
            resolved_state,
            output_dir,
            limit=limit,
            effective_date=effective_date,
            adapter=self._adapter,
            fetcher=self._fetcher,
            classifier=self._classifier,
            apply=True,
            stage_budget_seconds=self._stage_budget_seconds,
            max_workers=self._max_workers,
            monotonic=self._monotonic,
            operation_id=operation_id,
            search_hints=self._search_hints,
            snapshot_tracker=tracker,
            deep_pass=self._deep_pass,
            state_store=state_store,
        )


def adjudication_evidence_digest(subject_text: str) -> str:
    """Digest the fetched subject text of one verified page.

    The digest covers the normalized subject text only. No page body,
    article text, review prose, blurb, or book sample is retained.
    """
    normalized = _normalized_text(str(subject_text or ""))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _printed_title_present(printed_title: Any, page_text: str) -> bool:
    """Check that the claimed printed title occurs in the fetched page.

    Both normalizations the stage uses are tried, in the stage's order: the
    full claimed title first, then the work-level title. A source commonly
    prints the short title where the adapter cited ``Title: Subtitle``, and
    the subtitle is presentation metadata, so dropping such a citation would
    be a false drop, not a caught invention.
    """
    normalized_page = _normalized_token_text(page_text)
    if not normalized_page:
        return False
    claimed = _normalized_token_text(str(printed_title or ""))
    if claimed and claimed in normalized_page:
        return True
    work_level = _normalized_token_text(normalize_work_title(str(printed_title or "")))
    return bool(work_level) and work_level in normalized_page


def _printed_author_present(
    printed_author: Any,
    fetched_authors: Sequence[str],
    page_text: str,
    author_evidence_text: str = "",
) -> bool:
    """Check that one claimed printed author occurs in the fetched page.

    When the page states no author of its own, the text of its first author
    link counts too. A shop page often prints the author only as a link in a
    product-details list (issue #709). Nothing else on the page counts: a
    name in a "readers also bought" row or in running text is not evidence
    about this page's book, and a page that does state an author is never
    overridden by a link.
    """
    claimed_keys = _author_keys([str(printed_author or "")])
    if claimed_keys and claimed_keys & _author_keys(fetched_authors):
        return True
    claimed_token = _normalized_token_text(str(printed_author or ""))
    if bool(claimed_token) and claimed_token in _normalized_token_text(page_text):
        return True
    return not _page_states_any_author(fetched_authors) and page_prints_author(
        str(printed_author or ""), author_evidence_text
    )


def _page_states_any_author(fetched_authors: Sequence[str]) -> bool:
    """True when the page named an author to check a claim against.

    A page that names none cannot confirm or refute a printed author, so
    the citation is unverifiable rather than mismatched. Both still drop
    the citation, and only a mismatch is evidence that the adapter named
    the wrong book (issue #649).
    """
    return bool(_author_keys(fetched_authors))


def verify_adjudication_response(
    response: Mapping[str, Any],
    *,
    candidate_id: str = "",
    identity_key: str = "",
    fetcher: BoundedHTTPFetcher | None = None,
    classifier: SourceClassifier | None = None,
    checked_date: str | None = None,
    candidate_isbn: str = "",
) -> dict[str, Any]:
    """Check every cited page in code and drop any citation that fails.

    The five steps run in order per citation: canonicalize, re-fetch, check
    the printed title and authors, recompute owner independence, record a
    digest. A citation that fails the re-fetch or the printed-text check is
    dropped with a bounded reason code. The identity check reads the page's
    visible text as well as its title and metadata, and a page that prints
    ``candidate_isbn`` is this book without a title or author match (issue
    #791). A failed re-fetch is retried once. A citation with
    ``describes_this_work`` false survives verification but never counts
    toward the source total; its identity note is retained as bounded text.
    No page body is retained anywhere.
    """
    active_fetcher = fetcher or BoundedHTTPFetcher()
    active_classifier = classifier or committed_source_classifier()
    surviving: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    citations = response.get("citations", [])
    if not isinstance(citations, list):
        citations = []
    for citation in citations:
        if not isinstance(citation, Mapping):
            dropped.append({"url": "", "host": "", "reason": REASON_INVALID_SOURCE})
            continue
        surface_kind = str(citation.get("surface_kind", "")).strip()
        canonical_url = canonicalize_url(str(citation.get("url", "")))
        if not canonical_url:
            dropped.append(
                {
                    "url": str(citation.get("url", ""))[:MAX_ADJUDICATION_REASON_LENGTH],
                    "host": str(citation.get("host", ""))[:MAX_ADJUDICATION_REASON_LENGTH],
                    "reason": REASON_INVALID_SOURCE,
                }
            )
            continue
        parsed_hostname = urlsplit(canonical_url).hostname or ""
        canonical_host = canonicalize_hostname(parsed_hostname)
        # A Publishers Marketplace page describes a rights deal, not the
        # book: it is never fetched or counted as evidence (issue #947).
        if is_pm_url(canonical_url):
            dropped.append(
                {
                    "url": canonical_url,
                    "host": canonical_host,
                    "reason": REASON_SOURCE_DENYLISTED,
                    "describes_this_work": citation.get("describes_this_work") is True,
                }
            )
            continue
        # Classify before fetching, and send the real result. The page
        # metadata and the owner come from the committed owner policy, the
        # one the grounded stage uses, never from the adapter: a response
        # that mislabels its own surface must not be able to change how the
        # fetcher treats the page or whom it counts as (issue #1123).
        classification = active_classifier.classify(canonical_url)
        describes_this_work = citation.get("describes_this_work") is True
        # The owner policy's denylist is a quality filter (forums, summary
        # sites, pirate mirrors, general merchandise stores). The grounded
        # stage never reads such a page, so it is not evidence here either.
        if classification.get("tier") == TIER_DENYLIST:
            dropped.append(
                {
                    "url": canonical_url,
                    "host": canonical_host,
                    "reason": REASON_SOURCE_DENYLISTED,
                    "describes_this_work": describes_this_work,
                }
            )
            continue

        def drop(
            reason: str,
            *,
            url: str = canonical_url,
            host: str = canonical_host,
            describes: bool = describes_this_work,
            contradicted: bool = False,
        ) -> None:
            """Drop this citation, keeping whether it claimed to describe the work."""
            entry: dict[str, Any] = {"url": url, "host": host, "reason": reason, "describes_this_work": describes}
            if contradicted:
                entry["author_contradicted"] = True
            dropped.append(entry)

        fetched = None
        # One retry: a bookshop or publisher that refuses one plain request
        # often answers the next, and a failed fetch is not disproof.
        for _attempt in range(ADJUDICATION_VERIFICATION_FETCH_ATTEMPTS):
            try:
                fetched = active_fetcher.fetch(
                    {
                        "url": canonical_url,
                        "classification": str(classification.get("classification", "unknown")),
                        "source_kind": str(classification.get("source_kind", "")),
                        "source_family": str(classification.get("source_family", "")),
                        "owner_id": str(classification.get("owner_id", "")),
                        "is_search_snippet": False,
                    },
                    allow_unclassified_host=True,
                    **({"checked_date": checked_date} if checked_date else {}),
                )
            except (BoundedHTTPError, OSError):
                continue
            except ValueError:
                break
            break
        if fetched is None:
            drop(REASON_SEARCH_OR_FETCH_FAILURE)
            continue
        visible_text = fetched.identity_evidence_text
        metadata_text = " ".join(part for part in (fetched.title, fetched.display_text, fetched.subject_text) if part)
        printed_authors = citation.get("printed_authors", [])
        if not isinstance(printed_authors, list):
            printed_authors = []
        # The ISBN names one edition of one book, so a page that prints the
        # candidate's ISBN needs no title or author match (issue #791).
        isbn_confirmed = bool(plausible_isbn(candidate_isbn)) and (
            page_prints_isbn(candidate_isbn, fetched.isbn) or page_prints_isbn(candidate_isbn, visible_text)
        )
        # A page that states no author for its book and prints the title
        # may print the author anywhere in its visible text: a publisher
        # page often names the author only in a heading, and a review
        # states only its reviewer. A page that does state the book's
        # author is never overridden by its running text.
        author_in_visible_text = not (fetched.states_book_author and _page_states_any_author(fetched.primary_authors))
        title_printed = _printed_title_present(citation.get("printed_title", ""), f"{metadata_text} {visible_text}")
        authors_printed = not any(
            not _printed_author_present(author, fetched.primary_authors, metadata_text, fetched.author_evidence_text)
            and not (author_in_visible_text and page_prints_author_surname(str(author or ""), visible_text))
            for author in printed_authors
        )
        # A page that named no author could not refute the claim, so the
        # citation is unverifiable rather than mismatched. Both drop it;
        # only a mismatch says the adapter named the wrong book, and an
        # editor has to be able to tell them apart.
        author_contradicted = not authors_printed and _page_states_any_author(fetched.primary_authors)
        if isbn_confirmed:
            pass
        elif not title_printed:
            # The title is checked first and names the drop, but a page
            # that also names a different author still contradicts the
            # citation, and the unverified path must see that.
            drop(REASON_TITLE_MISMATCH, contradicted=author_contradicted)
            continue
        elif not authors_printed:
            drop(REASON_AUTHOR_MISMATCH if author_contradicted else REASON_AUTHOR_UNVERIFIABLE)
            continue
        identity_note = str(citation.get("identity_note", "") or "").strip()[:MAX_ADJUDICATION_IDENTITY_NOTE_LENGTH]
        # The derived owner: one shared owner for every host in the owner
        # policy's shared-owner class (storefronts and the catalogues it
        # lists), the registrable domain (after the equivalence table) for any
        # other host. A classifier that names no owner leaves the host as its
        # own owner.
        owner = str(classification.get("owner_id", "")).strip() or canonical_host
        surviving.append(
            {
                "url": canonical_url,
                "host": canonical_host,
                # The adapter's own label for the page. It is kept for the
                # preview and decides nothing.
                "surface_kind": surface_kind,
                "owner": owner,
                # How the owner was derived (``shared_owner`` or ``derived``),
                # so the preview can explain why two surviving citations did
                # or did not take two counting slots.
                "owner_tier": str(classification.get("tier", "")),
                "evidence_digest": adjudication_evidence_digest(fetched.subject_text),
                "describes_this_work": describes_this_work,
                "identity_note": identity_note,
                # How the page was tied to this book: its printed ISBN, or
                # the printed title and authors.
                "identity_proof": "isbn" if isbn_confirmed else "printed_text",
                # False only for a page the ISBN alone tied to the book: its
                # claimed title and authors were not found on it, so they
                # are not confirmed printed metadata.
                "printed_metadata_confirmed": title_printed and authors_printed,
                "counts_toward_independence": False,
            }
        )
    # One counting slot per derived owner, as in the grounded stage: two
    # storefronts share the publisher-supplied-copy owner and count once.
    seen_owners: set[str] = set()
    counting_urls: list[str] = []
    counting_digests: list[str] = []
    for entry in surviving:
        if not entry["describes_this_work"]:
            continue
        if entry["owner"] in seen_owners:
            continue
        entry["counts_toward_independence"] = True
        seen_owners.add(entry["owner"])
        counting_urls.append(entry["url"])
        counting_digests.append(entry["evidence_digest"])
    owner_count = len(counting_urls)
    # A dropped citation means the evidence was insufficient. That is
    # different from no sources existing, so the reason never claims
    # otherwise: fewer than two agreeing sources covers every shortfall.
    reason = "" if owner_count >= 2 else REASON_FEWER_THAN_TWO
    return {
        "candidate_id": candidate_id,
        "identity_key": identity_key,
        "resolved": response.get("resolved") is True,
        "confidence": str(response.get("confidence", "")),
        "surviving": surviving,
        "dropped": dropped,
        "counting_urls": counting_urls,
        # One digest per counting URL, in the same order. The applied record
        # retains both so the next run can compare the same evidence
        # (issue #639).
        "counting_digests": counting_digests,
        "owner_count": owner_count,
        "reason": reason,
    }


__all__ = [
    "ADJUDICATION_APPLY_DECISIONS",
    "ADJUDICATION_CALL_TIMEOUT_SECONDS",
    "ADJUDICATION_DECISIONS",
    "ADJUDICATION_MAX_WORKERS",
    "ADJUDICATION_PREVIEW_ARTIFACT_NAME",
    "ADJUDICATION_PROVENANCE",
    "ADJUDICATION_RESPONSE_SCHEMA",
    "ADJUDICATION_STAGE_BUDGET_SECONDS",
    "ADJUDICATION_UNVERIFIED_PROVENANCE",
    "CONFIDENCE_VALUES",
    "DECISION_APPLY_SINGLE_SOURCE",
    "DECISION_APPLY_TWO_SOURCE",
    "DECISION_APPLY_UNVERIFIED",
    "DECISION_EVIDENCE_UNCHANGED",
    "DECISION_FALLBACK",
    "DECISION_INVALID",
    "DECISION_NOT_ATTEMPTED",
    "DECISION_SKIPPED",
    "MAX_ADJUDICATION_CITATION_TEXT_LENGTH",
    "MAX_ADJUDICATION_IDENTITY_NOTE_LENGTH",
    "MAX_ADJUDICATION_REASON_LENGTH",
    "MAX_ADJUDICATION_RESPONSE_TEXT_LENGTH",
    "MAX_ADJUDICATION_RETAINED_CITATIONS",
    "REASON_AUTHOR_UNVERIFIABLE",
    "SURFACE_KINDS",
    "CodexAdjudicationAdapterError",
    "CodexAdjudicationExitError",
    "CodexAdjudicationResponseError",
    "CodexAdjudicationTimeoutError",
    "CodexDescriptionAdjudicationAdapter",
    "DescriptionAdjudicationStageService",
    "ReplayDescriptionAdjudicationAdapter",
    "adjudicate_descriptions",
    "adjudication_evidence_digest",
    "build_adjudication_prompt",
    "plan_adjudication_application",
    "validate_adjudication_response",
    "verify_adjudication_response",
]
