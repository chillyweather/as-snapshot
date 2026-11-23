"""Description enrichment stage for the weekly workflow.

Coordinates Codex-generated Russian Content Descriptions for visible New
candidates with blank/fallback descriptions. Runs after the MVP Pipeline
and before the final review rendering. Persists accepted descriptions with
``codex-generated`` provenance; invalid/missing descriptions remain as
honest fallbacks.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Protocol
from urllib.parse import urlparse

from autoscout.autobiographical_context import autobiographical_subject_context_warning
from autoscout.autobiographical_context import parse_autobiographical_subject_context
from autoscout.autobiographical_context import verified_context_from_artifact
from autoscout.content_description import DESCRIPTION_FALLBACK
from autoscout.content_description import _bounded_search_hints
from autoscout.content_description import apply_description_artifacts
from autoscout.content_description import has_nonfallback_description
from autoscout.content_description import prepare_backfill_work_items
from autoscout.content_description import validate_content_description
from autoscout.grounded_description import MAX_SOURCE_SUBJECT_TEXT
from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.pm_logline_description import MAX_LOGLINE_CHARS
from autoscout.scouting_candidates import ScoutingCandidate
from autoscout.scouting_candidates import authoritative_exclusion_record
from autoscout.state_store import Collection
from autoscout.state_store import StateStore
from autoscout.state_store import open_run_output_store
from autoscout.state_store import open_state_store
from autoscout.weekly_report import WeeklyReportStore
from autoscout.weekly_report import read_run_history
from autoscout.weekly_report import write_run_history

MAX_RETRY_ATTEMPTS = 3
INVALID_SCOUTING_CANDIDATES_REASON = "invalid_scouting_candidates"
_MAX_FIELD_LENGTH = 500
_MAX_LIST_LENGTH = 20

# Authoritative work-type vocabulary for grounded Audiobooks.com descriptions.
_GROUNDING_WORK_TYPES = ("fiction", "nonfiction")

# Bounded rejection reasons for the Audiobooks.com grounding contract. These
# tokens are safe for diagnostics and state: they never carry rejected source prose.
GROUNDING_REASON_MISSING_EVIDENCE = "missing grounding evidence"
GROUNDING_REASON_UNSUPPORTED_URL = "unsupported grounding evidence URL"
GROUNDING_REASON_UNAVAILABLE = "unavailable grounding evidence"
GROUNDING_REASON_NO_SUBJECT = "subject not established by grounding evidence"
GROUNDING_REASON_NO_WORK_TYPE = "missing authoritative work-type metadata"
GROUNDING_REASON_NO_GENRE = "missing authoritative genre metadata"
GROUNDING_REASON_NO_DESCRIPTION = "missing grounded description"
# Bounded reason for a grounded description whose prose fails the Content
# Description contract: the literal standard fallback, English-only or
# copied text, excessive length, or any other rejected value. The gate runs
# before the description can be accepted or authoritative metadata applied.
GROUNDING_REASON_INVALID_PROSE = "invalid grounded description prose"
# Bounded reason for a timeout, process failure/exception, malformed
# response, or empty agent response: the attempt produced no usable artifact
# at all, so there is no grounding evidence to reject.
GROUNDING_REASON_AGENT_FAILURE = "grounding agent failure"


class DescriptionEnrichmentAgent(Protocol):
    """Generate Russian Content Descriptions for a batch of work items."""

    def generate(self, work_items: list[dict[str, Any]]) -> dict[str, Any]: ...


# ── Safe encoding helpers ────────────────────────────────────────────────


def _bound_str(value: Any, max_len: int = _MAX_FIELD_LENGTH) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:max_len]


def _bound_str_list(values: Any, max_len: int = _MAX_FIELD_LENGTH) -> list[str]:
    if not isinstance(values, list):
        return []
    return [_bound_str(item, max_len) for item in values[:_MAX_LIST_LENGTH] if isinstance(item, str) and item.strip()]


def _bound_authors(value: Any) -> list[str]:
    """Bound primary_authors: list of strings, each capped."""
    return _bound_str_list(value, 200)


def _encode_work_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Encode work items as JSON-safe structures for the agent.

    All model-facing fields are bounded to prevent accidental leak of
    raw source bodies or oversized structures. Only known safe metadata
    keys are forwarded; lists are length- and item-capped.
    """
    safe_metadata_keys = {
        "title",
        "primary_authors",
        "publisher",
        "publication_year",
        "candidate_category",
        "short_description",
        "autobiographical_subject_context",
        "signal_summary",
        "isbn",
    }

    def _encode_metadata(md: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for k, v in md.items():
            if k not in safe_metadata_keys:
                continue
            if k == "primary_authors":
                result[k] = _bound_authors(v)
            elif k == "autobiographical_subject_context":
                context = parse_autobiographical_subject_context(v)
                if context:
                    result[k] = context
            elif isinstance(v, str):
                result[k] = _bound_str(v)
            elif isinstance(v, list):
                result[k] = _bound_str_list(v)
            else:
                # Unknown shapes: skip entirely.
                continue
        return {k: v for k, v in result.items() if v}

    def _encode_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "source_name": _bound_str(evidence.get("source_name", ""), 200),
            "source_family": _bound_str(evidence.get("source_family", ""), 100),
            "evidence_url": _bound_str(evidence.get("evidence_url", ""), 500),
            "evidence_note": _bound_str(evidence.get("evidence_note", ""), 500),
        }
        context = parse_autobiographical_subject_context(evidence.get("autobiographical_subject_context"))
        if context:
            result["autobiographical_subject_context"] = context
        return {key: value for key, value in result.items() if value}

    def _bound_nested_evidence(value: Any, *, depth: int = 0) -> Any:
        """Recursively bound structured evidence before it reaches the model."""
        if depth > 4:
            return None
        if isinstance(value, dict):
            bounded: dict[str, Any] = {}
            for raw_key in sorted(value, key=lambda item: str(item))[:20]:
                key = _bound_str(raw_key, 100)
                if not key:
                    continue
                item = _bound_nested_evidence(value[raw_key], depth=depth + 1)
                if item is not None:
                    bounded[key] = item
            return bounded
        if isinstance(value, list | tuple):
            return [
                item
                for item in (_bound_nested_evidence(item, depth=depth + 1) for item in value[:10])
                if item is not None
            ]
        if isinstance(value, str):
            return _bound_str(value, 300)
        if isinstance(value, bool | int | float):
            return value
        return None

    def _encode_accepted_bounded_evidence(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        encoded: list[dict[str, Any]] = []
        allowed_keys = (
            "url",
            "source_kind",
            "source_family",
            "owner_id",
            "source_language",
            "title_evidence_route",
            "title_evidence",
            "checked_date",
            "raw_title",
            "raw_authors",
            "subject_key",
            "display_text",
            "isbn",
            "classification",
            "classification_evidence",
        )
        field_limits = {
            "url": 500,
            "source_kind": 300,
            "source_family": 300,
            "owner_id": 300,
            "source_language": 300,
            "title_evidence_route": 100,
            "checked_date": 50,
            "raw_title": 500,
            "subject_key": 500,
            "display_text": MAX_SOURCE_SUBJECT_TEXT,
            "isbn": 50,
            "classification": 50,
        }
        for raw in value[:10]:
            if not isinstance(raw, dict):
                continue
            bounded: dict[str, Any] = {}
            for key in allowed_keys:
                if key not in raw:
                    continue
                raw_value = raw[key]
                if key in {"title_evidence", "classification_evidence"}:
                    bounded_value = _bound_nested_evidence(raw_value)
                elif key == "raw_authors":
                    bounded_value = _bound_nested_evidence(raw_value)
                    if isinstance(bounded_value, list):
                        bounded_value = [_bound_str(author, 200) for author in bounded_value[:10]]
                elif key in field_limits:
                    bounded_value = _bound_str(raw_value, field_limits[key])
                else:
                    bounded_value = _bound_nested_evidence(raw_value)
                if bounded_value is not None:
                    bounded[key] = bounded_value
            if bounded:
                encoded.append(bounded)
        return encoded

    encoded_items: list[dict[str, Any]] = []
    for item in items:
        evidence_references = [
            _encode_evidence(ev) for ev in item.get("metadata", {}).get("source_evidence", []) if isinstance(ev, dict)
        ][:10]
        encoded_item: dict[str, Any] = {
            "identity_key": _bound_str(item.get("identity_key", ""), 200),
            "title": _bound_str(item.get("title", ""), 300),
            "metadata": _encode_metadata(item.get("metadata", {})),
            "evidence_references": evidence_references,
            "description_requirements": _bound_str_list(item.get("description_requirements", []), 2000),
            "replaces_existing_description": item.get("replaces_existing_description") is True,
        }
        # Grounded search hints share the canonical bounding helper (issue
        # #684): URL-only starting points, never evidence.
        search_hints = _bounded_search_hints(item.get("search_hints"))
        if search_hints:
            encoded_item["search_hints"] = search_hints
        # The PM deal logline (issue #950) is sent only by its own stage,
        # which runs only with the weekly-config switch on.
        deal_logline = _bound_str(item.get("deal_logline", ""), MAX_LOGLINE_CHARS)
        if deal_logline:
            encoded_item["deal_logline"] = deal_logline
        accepted_evidence = _encode_accepted_bounded_evidence(item.get("accepted_bounded_evidence"))
        if accepted_evidence:
            encoded_item["accepted_bounded_evidence"] = accepted_evidence
        grounding_contract = _grounding_contract_for_item(item)
        if grounding_contract:
            allowed = [ref["evidence_url"] for ref in evidence_references if ref.get("evidence_url")]
            encoded_item["grounding_contract"] = {**grounding_contract, "allowed_evidence_urls": allowed}
        encoded_items.append(encoded_item)
    return encoded_items


def _work_item_evidence_urls(item: dict[str, Any]) -> list[str]:
    """Return research-source URLs for legacy provenance evidence basis.

    Falls back from ``evidence_url`` to ``source_url`` so provenance keeps a
    reference even when a Source Mention lacks a book-level URL.
    """
    return [
        str(evidence.get("evidence_url", "") or evidence.get("source_url", "")).strip()
        for evidence in item.get("metadata", {}).get("source_evidence", [])
        if isinstance(evidence, dict) and (evidence.get("evidence_url") or evidence.get("source_url"))
    ]


def _grounding_evidence_urls(item: dict[str, Any]) -> list[str]:
    """Return direct existing ``evidence_url`` values only.

    The grounding contract must never admit a source/category URL fallback:
    a category page is not authoritative for the underlying work. When a
    Source Mention lacks a direct evidence URL the list stays empty and the
    grounding gate fails closed with a bounded reason.
    """
    return [
        str(evidence.get("evidence_url", "")).strip()
        for evidence in item.get("metadata", {}).get("source_evidence", [])
        if isinstance(evidence, dict) and str(evidence.get("evidence_url", "")).strip()
    ]


def _is_audiobooks_work_item(item: dict[str, Any]) -> bool:
    """True when a work item's evidence belongs to the Audiobooks.com retailer.

    The retailer category on an Audiobooks.com ranking page is not
    authoritative for the underlying work, so these items carry the grounded
    description contract. Detection is deterministic and scoped to the
    retailer: the family alone is not enough because Audible charts share the
    audiobook-ranking family.
    """
    for evidence in item.get("metadata", {}).get("source_evidence", []):
        if not isinstance(evidence, dict):
            continue
        source_name = str(evidence.get("source_name", "")).casefold()
        if "audiobooks.com" in source_name:
            return True
        raw_url = str(evidence.get("evidence_url") or evidence.get("source_url", "")).strip()
        if not raw_url:
            continue
        try:
            host = urlparse(raw_url).hostname or ""
        except ValueError:
            host = ""
        host = host.casefold()
        if host == "audiobooks.com" or host.endswith(".audiobooks.com"):
            return True
    return False


def _grounding_contract_for_item(item: dict[str, Any]) -> dict[str, Any] | None:
    """Return the bounded grounding contract for an Audiobooks.com item.

    The allowed evidence URLs are exactly the direct existing ``evidence_url``
    values already present in the work item - never a source/category URL
    fallback. The model may use only these URLs as authoritative evidence, and
    nothing else. When no direct evidence URL exists the allowed set is empty
    and every grounding attempt fails closed.
    """
    if not _is_audiobooks_work_item(item):
        return None
    return {
        "source": "audiobooks.com",
        "allowed_evidence_urls": _grounding_evidence_urls(item)[:10],
    }


def _grounding_rejection_reason(item: dict[str, Any], artifact: dict[str, Any]) -> str:
    """Return a bounded rejection reason for an Audiobooks.com artifact.

    Returns an empty string when the artifact satisfies the grounding
    contract: structured authoritative evidence tied to an allowed existing
    Evidence URL, an established subject, a closed work-type value, and
    original Russian prose that passes the Content Description validation.
    The literal standard fallback and any prose rejected by
    ``validate_content_description`` fail the gate so that
    ``apply_description_artifacts`` can never clear a rejection reason or
    persist authoritative work type/genre for them.
    """
    contract = _grounding_contract_for_item(item)
    if contract is None:
        return ""
    grounding = artifact.get("grounding_evidence")
    if not isinstance(grounding, dict) or not grounding:
        return GROUNDING_REASON_MISSING_EVIDENCE
    evidence_url = str(grounding.get("evidence_url", "")).strip()
    allowed = {str(url).strip() for url in contract["allowed_evidence_urls"]}
    if not evidence_url or evidence_url not in allowed:
        return GROUNDING_REASON_UNSUPPORTED_URL
    if grounding.get("evidence_available") is not True:
        return GROUNDING_REASON_UNAVAILABLE
    if not str(grounding.get("subject", "")).strip():
        return GROUNDING_REASON_NO_SUBJECT
    if str(grounding.get("work_type", "")).strip() not in _GROUNDING_WORK_TYPES:
        return GROUNDING_REASON_NO_WORK_TYPE
    if not str(grounding.get("genre", "")).strip():
        return GROUNDING_REASON_NO_GENRE
    description = str(artifact.get("content_description", "")).strip()
    if not description:
        return GROUNDING_REASON_NO_DESCRIPTION
    validated_description, _ = validate_content_description(description)
    if validated_description is None:
        # English-only, copied prose, excessive length, or any other
        # rejected content never satisfies the grounded description gate.
        return GROUNDING_REASON_INVALID_PROSE
    # The standard fallback is not grounded prose: accepting it as a
    # codex-generated description would hide the missing description. The
    # check runs on the whitespace-normalized validated result, so a
    # fallback with extra whitespace or line breaks cannot pass either.
    if not has_nonfallback_description(validated_description):
        return GROUNDING_REASON_INVALID_PROSE
    return ""


# ── Agent implementations ────────────────────────────────────────────────


# Why a description call gave no answer at all. A call that failed is not a
# model that declined: on 2026-11-10 every call failed on an unusable model
# and the stages recorded each book as declined.
CALL_FAILURE_TIMEOUT = "timeout"
CALL_FAILURE_NOT_STARTED = "codex_not_started"
CALL_FAILURE_MODEL_UNAVAILABLE = "model_unavailable"
CALL_FAILURE_USAGE_LIMIT = "usage_limit"
CALL_FAILURE_EXIT = "codex_exit_nonzero"
CALL_FAILURE_INVALID_RESPONSE = "invalid_response"
# The Claude API key file is missing or its key was rejected (Issue #1149).
CALL_FAILURE_KEY_UNUSABLE = "model_key_unusable"
# The Claude API backend refused a request that mentions publishersweekly.com:
# that book is not described on this backend, the others are (Issue #1149).
CALL_FAILURE_PW_TEXT_NOT_PERMITTED = "pw_text_not_permitted"
# The description backend may not receive a PM deal logline (Issue #1149):
# no logline of the run is sent to it, and no other provider is asked instead.
CALL_FAILURE_PM_TEXT_NOT_PERMITTED = "pm_text_not_permitted"
# Failures no other book of the same run would escape.
CALL_FAILURES_FOR_EVERY_BOOK = frozenset(
    {
        CALL_FAILURE_NOT_STARTED,
        CALL_FAILURE_MODEL_UNAVAILABLE,
        CALL_FAILURE_USAGE_LIMIT,
        CALL_FAILURE_KEY_UNUSABLE,
        CALL_FAILURE_PM_TEXT_NOT_PERMITTED,
    }
)


def call_failure_reason(result: Any) -> str:
    """Return the ``CALL_FAILURE_*`` code of a model call that did not answer, or ``""``.

    The backend names why the call failed (``result.failure``): the Codex CLI
    from its stderr markers, the Claude API from the HTTP status, so both
    land in the same codes.
    """
    from autoscout.model_client import ModelCallStatus
    from autoscout.model_client import ModelFailure

    failure = result.failure
    if result.status is ModelCallStatus.ANSWERED:
        return ""
    if result.status is ModelCallStatus.TIMEOUT:
        return CALL_FAILURE_TIMEOUT
    if failure is ModelFailure.PW_TEXT_NOT_PERMITTED:
        return CALL_FAILURE_PW_TEXT_NOT_PERMITTED
    if failure is ModelFailure.PM_TEXT_NOT_PERMITTED:
        return CALL_FAILURE_PM_TEXT_NOT_PERMITTED
    if failure in (ModelFailure.KEY_MISSING, ModelFailure.KEY_REJECTED):
        return CALL_FAILURE_KEY_UNUSABLE
    if failure is ModelFailure.MODEL_UNAVAILABLE:
        return CALL_FAILURE_MODEL_UNAVAILABLE
    if failure is ModelFailure.USAGE_LIMIT:
        return CALL_FAILURE_USAGE_LIMIT
    if result.status in (ModelCallStatus.NOT_STARTED, ModelCallStatus.REFUSED):
        return CALL_FAILURE_NOT_STARTED
    if result.status is ModelCallStatus.EXIT_NONZERO:
        return CALL_FAILURE_EXIT
    return CALL_FAILURE_INVALID_RESPONSE


class CodexDescriptionEnrichmentAgent:
    """Obtain Russian Content Descriptions from Codex in an isolated read-only sandbox."""

    def __init__(
        self, timeout_seconds: int = 180, *, model_pin: Any | None = None, purpose: str = "description_enrichment"
    ) -> None:
        from autoscout.codex_model_pin import load_description_model_pin

        self._timeout_seconds = timeout_seconds
        # The repository pin, never the operator's global Codex default (#731).
        self.model_pin = model_pin if model_pin is not None else load_description_model_pin()
        # The stage name the usage ledger records each call under (#1149).
        self.purpose = purpose

    @staticmethod
    def _build_prompt() -> str:
        """Build the model-facing prompt for one enrichment batch.

        The grounding paragraph states the bounded contract for
        Audiobooks.com items: only an exact existing direct Evidence URL
        from the item, no searching or browsing, no inference from the
        title alone, and bounded structured authoritative evidence.
        """
        return (
            "Read work_items.json. Each item needs a concise original Russian Content Description "
            "(2-5 sentences) describing the book's subject, angle, and editorial hook. "
            "Return ONLY a JSON object keyed by the exact identity_key from each item, "
            'with each value shaped as {"content_description": "...", '
            '"autobiographical_subject_context": {"opening_phrase": "...", '
            '"source_phrase": "...", "evidence_url": "..."}}. '
            "For non-autobiographical books, omit autobiographical_subject_context. "
            'Each value with a content_description may also carry "english_description": the same subject and '
            "angle in 2-5 sentences of original English prose from the same evidence. It is never shown to the "
            "editor; omit it rather than guess. "
            "Do not include copied reviews, "
            "blurbs, article bodies, excerpts, or samples. "
            "Do not invent evidence references or URLs. Obey each item's description_requirements. "
            "If autobiographical subject context is unsupported, omit only that context field and still return "
            "an otherwise valid content_description so the review can flag the missing context.\n"
            "For items that carry a grounding_contract field, the retailer category is not authoritative "
            "for the underlying work. Use only an exact existing direct Evidence URL listed in that item's "
            "grounding_contract.allowed_evidence_urls, or another already permitted URL supplied in that item, "
            "as the authoritative source for the description. "
            "You may fetch that one URL. You must not search, browse generally, follow links from it, "
            "or infer the subject, genre, or editorial angle from the title alone. "
            "Return for such an identity a JSON value shaped as "
            '{"content_description": "...", '
            '"grounding_evidence": {"evidence_url": "<exact allowed URL>", "evidence_available": true, '
            '"subject": "<short authoritative subject>", "work_type": "nonfiction"|"fiction", '
            '"genre": "<authoritative genre, e.g. Science Fiction>"}}. '
            "grounding_evidence.evidence_url must match one of the item's allowed_evidence_urls byte for byte. "
            "subject and genre must be short structured metadata taken from the evidence page, "
            "never copied review or blurb prose. "
            "When the evidence page is unavailable or shows no authoritative subject or work-type metadata, "
            "set evidence_available to false and omit content_description.\n"
        )

    def generate(
        self,
        work_items: list[dict[str, Any]],
        *,
        _prompt_override: str | None = None,
        _timeout_seconds: float | None = None,
        _on_failure: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Return the model's answers; ``{}`` when the call itself failed.

        An empty answer alone cannot tell a model that declined from a call
        that never reached it, so ``_on_failure`` receives one bounded
        reason code (``CALL_FAILURE_*``) whenever the call failed.
        """
        import json
        import tempfile

        from autoscout.model_client import ModelCallStatus
        from autoscout.model_client import ModelRequest
        from autoscout.model_client import model_client_for

        def failed(reason: str) -> dict[str, Any]:
            if _on_failure is not None:
                _on_failure(reason)
            return {}

        if not work_items:
            return {}
        configured_timeout = float(self._timeout_seconds)
        if configured_timeout <= 0:
            return {}
        if _timeout_seconds is None:
            timeout = configured_timeout
        elif _timeout_seconds <= 0:
            return {}
        else:
            timeout = min(configured_timeout, _timeout_seconds)

        encoded = _encode_work_items(work_items)
        with tempfile.TemporaryDirectory(prefix="autoscout-desc-enrich-") as temporary:
            isolated = Path(temporary)
            work_path = isolated / "work_items.json"
            response_path = isolated / "response.json"
            work_path.write_text(json.dumps(encoded, ensure_ascii=False), encoding="utf-8")

            prompt = _prompt_override if _prompt_override is not None else self._build_prompt()

            result = model_client_for(self.model_pin).run(
                ModelRequest(
                    prompt=prompt,
                    response_path=response_path,
                    model=self.model_pin.model,
                    reasoning_effort=self.model_pin.reasoning_effort,
                    working_dir=isolated,
                    # The Claude API backend sends the work items in the request.
                    input_files=("work_items.json",),
                    purpose=self.purpose,
                ),
                timeout=timeout,
            )
            if result.status is not ModelCallStatus.ANSWERED:
                return failed(call_failure_reason(result))

            payload = result.payload
            if not isinstance(payload, dict):
                return failed(CALL_FAILURE_INVALID_RESPONSE)

            known_ids = {str(item.get("identity_key", "")) for item in work_items}
            return {str(k): v for k, v in payload.items() if str(k) in known_ids and isinstance(v, str | dict)}


class NoOpDescriptionEnrichmentAgent:
    """Safe no-model/no-network agent that returns no descriptions."""

    def generate(self, work_items: list[dict[str, Any]]) -> dict[str, Any]:
        return {}


# ── Public enrichment service ────────────────────────────────────────────


class DescriptionEnrichmentService:
    """Enrich visible New candidates with blank/fallback Russian descriptions.

    Accepts an injected ``DescriptionEnrichmentAgent``. Deterministic tests
    inject ``NoOpDescriptionEnrichmentAgent``; production injects
    ``CodexDescriptionEnrichmentAgent``.
    """

    def __init__(self, agent: DescriptionEnrichmentAgent | None = None) -> None:
        self._agent = agent or NoOpDescriptionEnrichmentAgent()

    def enrich(
        self,
        output_dir: Path,
        state_dir: Path | None = None,
        effective_date: str | None = None,
        *,
        operation_id: str | None = None,
        state_store: StateStore | None = None,
    ) -> dict[str, Any]:
        del operation_id
        return run_enrichment(
            output_dir, state_dir, agent=self._agent, effective_date=effective_date, state_store=state_store
        )


# ── Retry and sanitization helpers ────────────────────────────────────────


def _sanitize_agent_response(
    batch: Any,
    known_ids: set[str],
    grounding_contracts: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return bounded description artifacts from untrusted agent output.

    Non-dict, exceptions, unknown keys, and non-string values are silently
    dropped. The caller treats this as an empty attempt for retry purposes.
    For items carrying a grounding contract, bounded ``grounding_evidence``
    fields are preserved so the caller can enforce the grounding gate; the
    evidence values stay bounded and never carry source prose.
    """
    if not isinstance(batch, dict):
        return {}
    sanitized: dict[str, dict[str, Any]] = {}
    for key, value in batch.items():
        identity_key = str(key)
        if identity_key not in known_ids:
            continue
        grounding: dict[str, Any] | None = None
        if isinstance(value, str):
            description = _bound_str(value)
            subject_context = None
        elif isinstance(value, dict):
            description = _bound_str(value.get("content_description", ""))
            parsed_context = parse_autobiographical_subject_context(value.get("autobiographical_subject_context"))
            subject_context = parsed_context
            contract = (grounding_contracts or {}).get(identity_key)
            if contract:
                raw_grounding = value.get("grounding_evidence")
                if isinstance(raw_grounding, dict):
                    grounding = {
                        "evidence_url": _bound_str(raw_grounding.get("evidence_url", ""), 500),
                        "evidence_available": raw_grounding.get("evidence_available") is True,
                        "subject": _bound_str(raw_grounding.get("subject", ""), 300),
                        "work_type": _bound_str(raw_grounding.get("work_type", ""), 50),
                        "genre": _bound_str(raw_grounding.get("genre", ""), 100),
                    }
        else:
            continue
        if not description and grounding is None:
            continue
        artifact: dict[str, Any] = {"content_description": description}
        english_description = _bound_str(value.get("english_description", "")) if isinstance(value, dict) else ""
        if description and english_description:
            artifact["english_description"] = english_description
        if subject_context:
            artifact["autobiographical_subject_context"] = subject_context
        if grounding is not None:
            artifact["grounding_evidence"] = grounding
        sanitized[identity_key] = artifact
    return sanitized


def _still_missing(
    work_items: list[dict[str, Any]],
    generated: dict[str, dict[str, Any]],
    candidate_history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return work items whose ID still has no valid accepted description."""
    history_by_id = {
        str(rec.get("identity_key", "")).strip(): rec for rec in candidate_history if isinstance(rec, dict)
    }
    remaining: list[dict[str, Any]] = []
    for wi in work_items:
        ik = str(wi.get("identity_key", "")).strip()
        record = history_by_id.get(ik, {})
        generated_artifact = generated.get(ik, {})
        desc = str(generated_artifact.get("content_description", "")).strip()
        if desc:
            validated, _ = validate_content_description(desc)
            if validated is not None:
                if wi.get("replaces_existing_description") is not True:
                    continue
                verified_context = verified_context_from_artifact(
                    record,
                    generated_artifact.get("autobiographical_subject_context"),
                )
                if not autobiographical_subject_context_warning(
                    record,
                    content_description=validated,
                    subject_context=verified_context,
                ):
                    continue
        existing = str(record.get("content_description", "")).strip()
        if has_nonfallback_description(existing) and wi.get("replaces_existing_description") is not True:
            continue
        remaining.append(wi)
    return remaining


def _generate_with_retry(
    agent: DescriptionEnrichmentAgent,
    work_items: list[dict[str, Any]],
    candidate_history: list[dict[str, Any]],
    *,
    max_attempts: int = MAX_RETRY_ATTEMPTS,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    """Generate descriptions with bounded retry for still-missing IDs.

    Agent responses are sanitized at every attempt. Exceptions are caught
    and treated as empty failed attempts so enrichment remains non-blocking.
    Audiobooks.com artifacts that fail the grounding gate are rejected with
    a bounded reason and the item stays missing for retry; a timeout,
    exception, malformed response, or empty agent response records the
    bounded agent-failure reason. A later valid attempt clears the prior
    transient rejection.
    """
    all_generated: dict[str, dict[str, Any]] = {}
    retry_log: list[dict[str, Any]] = []
    grounding_rejections: dict[str, str] = {}
    remaining = list(work_items)

    for attempt in range(1, max_attempts + 1):
        try:
            raw = agent.generate(remaining)
        except Exception:
            raw = {}
        # Scope accepted IDs to the *currently requested* batch so a
        # later retry cannot overwrite/reintroduce an already-successful ID
        # if an untrusted agent returns it unsolicited.
        remaining_ids = {str(wi.get("identity_key", "")).strip() for wi in remaining}
        remaining_by_id = {str(wi.get("identity_key", "")).strip(): wi for wi in remaining}
        grounding_contracts = {
            identity_key: contract
            for identity_key, wi in remaining_by_id.items()
            if (contract := _grounding_contract_for_item(wi)) is not None
        }
        batch_gen = _sanitize_agent_response(raw, remaining_ids, grounding_contracts=grounding_contracts)
        produced_ids = set(batch_gen)
        for identity_key in list(batch_gen):
            item = remaining_by_id.get(identity_key)
            if item is None:
                continue
            reason = _grounding_rejection_reason(item, batch_gen[identity_key])
            if reason:
                # The latest attempt's reason wins: a later attempt may
                # produce evidence the earlier one could not (Issue #391).
                grounding_rejections[identity_key] = reason
                del batch_gen[identity_key]
        for identity_key in batch_gen:
            grounding_rejections.pop(identity_key, None)
        # A timeout, process failure/exception, malformed response, or empty
        # agent response leaves the item with no usable artifact at all. Such
        # an Audiobooks.com item must still end with the standard fallback
        # and a bounded explicit rejection reason (Issue #391).
        for identity_key in grounding_contracts:
            if identity_key not in produced_ids:
                grounding_rejections[identity_key] = GROUNDING_REASON_AGENT_FAILURE
        all_generated.update(batch_gen)
        retry_log.append(
            {
                "attempt": attempt,
                "requested_ids": [str(wi.get("identity_key", "")) for wi in remaining],
                "returned_ids": sorted(batch_gen.keys()),
                "requested_count": len(remaining),
                "returned_count": len(batch_gen),
                "grounding_rejected_ids": sorted(grounding_rejections.keys()),
            }
        )
        remaining = _still_missing(work_items, all_generated, candidate_history)
        if not remaining:
            break

    return all_generated, retry_log, grounding_rejections


# ── Main enrichment entry point ──────────────────────────────────────────


def _publish_latest_candidates(
    store: StateStore,
    candidates: list[dict[str, Any]],
    history: list[dict[str, Any]],
    aliases: Mapping[str, Any],
    excluded_fiction: list[dict[str, Any]],
) -> None:
    """Refresh the latest Scouting Candidates after enrichment.

    A store without latest candidates gets the current candidates. Otherwise
    history's descriptions are projected onto them, and the books excluded
    as fiction leave them: an explicit membership change, not a projection
    side effect. Historical self-healing for this route only: a corrupt
    snapshot is replaced by the current candidates. The other routes fail
    closed instead; both behaviors are documented at
    ``description_projection.project_snapshot_file``.
    """
    from autoscout.description_projection import project_history_descriptions

    try:
        latest = store.read(Collection.LATEST_SCOUTING_CANDIDATES)
    except ValueError:
        latest = None
    if not isinstance(latest, list) or any(not isinstance(row, Mapping) for row in latest) or not latest:
        store.replace(Collection.LATEST_SCOUTING_CANDIDATES, candidates)
        return
    projected, _summary = project_history_descriptions(history, latest, aliases)
    excluded_keys = {
        str(record.get("identity_key", "")).strip()
        for record in excluded_fiction
        if isinstance(record, dict) and str(record.get("identity_key", "")).strip()
    }
    store.replace(
        Collection.LATEST_SCOUTING_CANDIDATES,
        [row for row in projected if str(row.get("identity_key", "")).strip() not in excluded_keys],
    )


def run_enrichment(
    output_dir: Path,
    state_dir: Path | None = None,
    *,
    agent: DescriptionEnrichmentAgent | None = None,
    effective_date: str | None = None,
    state_store: StateStore | None = None,
) -> dict[str, Any]:
    """Run the description enrichment stage.

    With a state directory, canonical state is read and written through
    *state_store*, the run's store, or a store opened over *state_dir*.
    Without one, Candidate History is the run's copy and visibility is read
    through a store over the output directory, as it always was.
    """
    store: StateStore | None = None
    if state_dir:
        store = state_store if state_store is not None else open_state_store(state_dir)

    report_store = WeeklyReportStore(output_dir)
    candidates_path = report_store.path("scouting-candidates.json")
    if not candidates_path.exists():
        return {"status": "skipped", "reason": "missing scouting-candidates.json", "enriched": 0, "fallback": 0}

    try:
        candidates = read_json(candidates_path, default=[])
    except (OSError, TypeError, ValueError):
        return {
            "status": "input_failed",
            "reason": INVALID_SCOUTING_CANDIDATES_REASON,
            "enriched": 0,
            "fallback": 0,
        }
    if not isinstance(candidates, list) or any(not isinstance(candidate, dict) for candidate in candidates):
        return {
            "status": "input_failed",
            "reason": INVALID_SCOUTING_CANDIDATES_REASON,
            "enriched": 0,
            "fallback": 0,
        }

    if store is not None:
        candidate_history = store.read(Collection.CANDIDATE_HISTORY)
        visibility_records = store.read(Collection.CANDIDATE_VISIBILITY)
    else:
        candidate_history = read_run_history(output_dir)
        visibility_records = open_run_output_store(output_dir).read(Collection.CANDIDATE_VISIBILITY)
    if not isinstance(candidate_history, list):
        candidate_history = []
    if not isinstance(visibility_records, list):
        visibility_records = []

    work_items = prepare_backfill_work_items(candidate_history, visibility_records=visibility_records)

    visible_new_ids: set[str] = set()
    for candidate in candidates:
        identity_key = str(candidate.get("identity_key", "")).strip()
        update_delta = str(candidate.get("update_delta", "")).strip()
        if identity_key and update_delta == "new":
            visible_new_ids.add(identity_key)

    hidden_ids: set[str] = {
        str(record.get("identity_key", "")).strip()
        for record in visibility_records
        if isinstance(record, dict) and str(record.get("visibility", "")).strip().lower() == "hidden"
    }

    work_items = [
        wi for wi in work_items if wi["identity_key"] in visible_new_ids and wi["identity_key"] not in hidden_ids
    ]

    if not work_items:
        return {"status": "ok", "enriched": 0, "fallback": 0, "candidates_considered": 0}

    _agent = agent or CodexDescriptionEnrichmentAgent()
    generated, retry_log, grounding_rejections = _generate_with_retry(_agent, work_items, candidate_history)

    artifacts: list[dict[str, Any]] = []
    for wi in work_items:
        identity_key = wi["identity_key"]
        generated_artifact = generated.get(identity_key, {})
        description = str(generated_artifact.get("content_description", "")).strip()
        subject_context = generated_artifact.get("autobiographical_subject_context")
        research_sources = _work_item_evidence_urls(wi)
        grounding = generated_artifact.get("grounding_evidence")
        grounding_evidence_url = str(grounding.get("evidence_url", "")).strip() if isinstance(grounding, dict) else ""
        work_type = str(grounding.get("work_type", "")).strip() if isinstance(grounding, dict) else ""
        genre = str(grounding.get("genre", "")).strip() if isinstance(grounding, dict) else ""
        artifacts.append(
            {
                "identity_key": identity_key,
                "content_description": description if description else "",
                "autobiographical_subject_context": subject_context if description else None,
                "research_sources": research_sources if description else [],
                "grounding_evidence_url": grounding_evidence_url if description else "",
                "authoritative_work_type": work_type if description else "",
                "authoritative_genre": genre if description else "",
                # Validated by the apply gate, which drops it when invalid
                # without touching the Russian description (issue #727).
                "english_description": str(generated_artifact.get("english_description", "")) if description else "",
                # The bounded grounding rejection reason travels with the
                # standard fallback into candidate state (Issue #391).
                "rejection_reason": grounding_rejections.get(identity_key, ""),
            }
        )

    updated_history, results = apply_description_artifacts(
        candidate_history,
        artifacts,
        provenance="codex-generated",
        effective_date=effective_date,
    )

    if store is not None:
        store.replace(Collection.CANDIDATE_HISTORY, updated_history)
    write_run_history(output_dir, state_dir, updated_history, store)

    # Shared identity-aware projection (issue #671): history is authoritative
    # for description facts. Membership, order, and unrelated fields are
    # preserved; stale description metadata the history no longer holds is
    # cleared rather than left beside fresh text.
    from autoscout.description_projection import project_history_descriptions

    legacy_aliases: dict[str, Any] = {}
    if store is not None:
        try:
            loaded_aliases = store.read(Collection.CANDIDATE_IDENTITY_ALIASES)
            if isinstance(loaded_aliases, dict):
                legacy_aliases = loaded_aliases
        except (OSError, ValueError):
            legacy_aliases = {}
    candidates, legacy_projection = project_history_descriptions(updated_history, candidates, legacy_aliases)

    history_by_id = {
        str(record.get("identity_key", "")): record for record in updated_history if isinstance(record, dict)
    }
    for candidate in candidates:
        identity_key = str(candidate.get("identity_key", "")).strip()
        hist_record = history_by_id.get(identity_key)
        if hist_record:
            subject_context = hist_record.get("autobiographical_subject_context")
            if subject_context:
                candidate["autobiographical_subject_context"] = subject_context
            # Structured authoritative metadata is persisted; the free-form
            # subject statement is acceptance-gate evidence only and never
            # enters candidate state (Issue #391).
            for field_name in ("authoritative_work_type", "authoritative_genre"):
                if hist_record.get(field_name):
                    candidate[field_name] = hist_record[field_name]
            if hist_record.get("authoritative_genre"):
                candidate["candidate_category"] = str(hist_record.get("candidate_category", "")).strip()

    # An authoritative pure-fiction result replaces the retailer category on
    # the candidate and then passes through the canonical exclusion rule. The
    # work leaves the current Scouting Candidates and review; one canonical
    # exclusion-record builder keeps the authoritative evidence and the
    # grounding Evidence URL (Issue #391).
    excluded_fiction: list[dict[str, Any]] = []
    kept_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        identity_key = str(candidate.get("identity_key", "")).strip()
        hist_record = history_by_id.get(identity_key, {})
        entity = ScoutingCandidate.from_mapping(candidate)
        exclusion_reason = entity.exclusion_reason()
        if exclusion_reason:
            exclusion_record = authoritative_exclusion_record(entity, exclusion_reason, effective_date or "")
            provenance = hist_record.get("description_provenance")
            grounding_url = (
                str(provenance.get("grounding_evidence_url", "")).strip() if isinstance(provenance, dict) else ""
            )
            if grounding_url:
                exclusion_record["grounding_evidence_url"] = grounding_url
            excluded_fiction.append(exclusion_record)
            continue
        kept_candidates.append(candidate)
    candidates = kept_candidates

    if excluded_fiction:
        exclusions_path = output_dir / "internal-exclusions.json"
        existing_exclusions = read_json(exclusions_path, default=[])
        if not isinstance(existing_exclusions, list):
            existing_exclusions = []
        existing_exclusions = [record for record in existing_exclusions if isinstance(record, dict)]
        write_json(exclusions_path, existing_exclusions + excluded_fiction)
        if store is not None:
            from autoscout.pipeline import merge_excluded_candidate_history

            existing_excluded = store.read(Collection.EXCLUDED_CANDIDATE_HISTORY)
            if not isinstance(existing_excluded, list):
                existing_excluded = []
            store.replace(
                Collection.EXCLUDED_CANDIDATE_HISTORY,
                merge_excluded_candidate_history(existing_excluded, excluded_fiction),
            )

    write_json(candidates_path, candidates)

    if store is not None:
        _publish_latest_candidates(store, candidates, updated_history, legacy_aliases, excluded_fiction)

    fallback_ids = [
        str(wi["identity_key"]) for wi in work_items if _is_fallback_after(updated_history, str(wi["identity_key"]))
    ]
    applied_ids = [
        str(wi["identity_key"]) for wi in work_items if _is_enriched_after(updated_history, str(wi["identity_key"]))
    ]
    diagnostic = {
        "stage": "description-enrichment",
        "requested_ids": [str(wi.get("identity_key", "")) for wi in work_items],
        "applied_ids": applied_ids,
        "applied": results["applied"],
        "fallback_ids": fallback_ids,
        "fallback": results["fallback"],
        "skipped": results["skipped"],
        "attempts": len(retry_log),
        "attempt_log": retry_log,
        "grounding_rejection_reasons": dict(sorted(grounding_rejections.items())),
        "excluded_fiction_ids": sorted(record["identity_key"] for record in excluded_fiction),
    }
    WeeklyReportStore(output_dir).write_json("description-enrichment.json", diagnostic)

    from autoscout.content_description import description_coverage

    visible_candidates = [
        candidate for candidate in candidates if str(candidate.get("identity_key", "")).strip() not in hidden_ids
    ]
    cov = description_coverage(visible_candidates)

    # The pipeline's run-summary, pilot status, and source metrics were
    # written before enrichment. The canonical post-enrichment refresh seam
    # recomputes them from the final candidates; no ad hoc field patching
    # here (Issue #391). The workflow invokes the seam after this stage.

    return {
        "status": "ok",
        "enriched": results["applied"],
        "fallback": results["fallback"],
        "candidates_considered": len(work_items),
        "diagnostic": diagnostic,
        "coverage": cov,
    }


def _is_fallback_after(history: list[dict[str, Any]], identity_key: str) -> bool:
    for rec in history:
        if str(rec.get("identity_key", "")).strip() == identity_key:
            return str(rec.get("content_description", "")).strip() == DESCRIPTION_FALLBACK
    return False


def _is_enriched_after(history: list[dict[str, Any]], identity_key: str) -> bool:
    for rec in history:
        if str(rec.get("identity_key", "")).strip() == identity_key:
            desc = str(rec.get("content_description", "")).strip()
            prov = rec.get("description_provenance")
            prov_str = str(prov.get("provenance", "") if isinstance(prov, dict) else prov or "")
            return has_nonfallback_description(desc) and prov_str == "codex-generated"
    return False
