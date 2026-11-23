from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autoscout.bookmarks_contract import capture_contains_bookmarks_listing_rows
from autoscout.bookmarks_contract import is_bookmarks_source_identity
from autoscout.extraction_completeness import extractor_empty_reason
from autoscout.helper_metadata import HelperMetadataError
from autoscout.helper_metadata import load_helper_metadata
from autoscout.partial_list import collected_list_refused
from autoscout.partial_list import gate_dropped_entries
from autoscout.partial_list import gate_dropped_ranks
from autoscout.partial_list import keeps_partial_list
from autoscout.partial_list import partial_list_marker
from autoscout.ranking_author_policy import mention_author_exempt
from autoscout.ranking_freshness import FINGERPRINT_DEBUG_FIELD
from autoscout.ranking_freshness import ranking_row_fingerprint
from autoscout.ranking_freshness import ranking_undated_declared
from autoscout.source_collectors.agent_mentions import agent_extraction_mentions_from_response
from autoscout.source_collectors.agent_mentions import claimed_rank
from autoscout.source_collectors.base import FORBIDDEN_COLLECTOR_RESPONSE_FIELDS
from autoscout.source_collectors.base import CollectorResult
from autoscout.source_collectors.base import CollectorRunError
from autoscout.source_collectors.base import _collector_error
from autoscout.source_collectors.base import capture_content_evidence
from autoscout.source_collectors.base import capture_normalized_mentions
from autoscout.source_collectors.base import extraction_entries
from autoscout.source_collectors.base import normalized_agent_mention
from autoscout.source_collectors.base import record_seen_index_items
from autoscout.source_collectors.base import validate_extraction_page_identity
from autoscout.source_page_identity import ranking_rows
from autoscout.source_proposals import normalize_source_identity


def resolve_codex_artifact_path(config_path: Path, source: dict[str, Any], raw_path: str) -> Path:
    """Resolve a path for Codex artifacts.  If the source configures a
    codex_artifact_dir, relative paths are resolved against that directory;
    otherwise they are resolved against config_path.parent."""
    path = Path(raw_path)
    if path.is_absolute():
        return path
    artifact_dir = source.get("codex_artifact_dir")
    if artifact_dir:
        base = Path(str(artifact_dir))
        if not base.is_absolute():
            base = config_path.parent / base
        return base / path
    return config_path.parent / path


def configured_codex_extraction_inputs(
    config_path: Path, source: dict[str, Any]
) -> tuple[dict[str, Any], Any, dict[str, Any]]:
    """Read capture page and extraction response for Codex replay,
    resolving relative paths via codex_artifact_dir when configured."""
    raw_page_path = source.get("source_page_fixture_file") or source.get("live_response_file")
    page_debug: dict[str, Any] = {
        "source_page_fixture_file": str(source.get("source_page_fixture_file", "")),
        "live_response_file": str(source.get("live_response_file", "")),
    }
    if not raw_page_path:
        url = str(source.get("url", "")).strip()
        page_data = {"source_page": url, "html": ""}
    else:
        page_path = resolve_codex_artifact_path(config_path, source, str(raw_page_path))
        page_debug["response_file"] = str(raw_page_path)
        try:
            raw_text = page_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise _collector_error(
                f"Codex capture file not found: {raw_page_path}",
                debug_metadata={
                    "collector_kind": "codex-extraction",
                    "safe_failure_status": "missing_capture_file",
                    "missing_file": str(raw_page_path),
                },
            )
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise _collector_error(
                f"Codex extraction capture file is malformed JSON: {exc.msg}",
                debug_metadata={
                    "collector_kind": "codex-extraction",
                    "safe_failure_status": "malformed_json",
                    "malformed_file": str(raw_page_path),
                },
            )
        if isinstance(data, str):
            page_data = {"source_page": str(source.get("url", "")), "html": data}
        elif isinstance(data, dict):
            data.setdefault("source_page", str(source.get("url", "")))
            data.setdefault("html", "")
            page_data = data
        else:
            raise _collector_error(
                "Codex extraction capture fixture must be an object or string",
                debug_metadata={
                    "collector_kind": "codex-extraction",
                    "safe_failure_status": "malformed_capture_format",
                },
            )

    raw_extraction_path = (
        source.get("extraction_response_file")
        or source.get("collector_response_file")
        or source.get("live_extraction_response_file")
    )
    extraction_debug: dict[str, Any] = {
        "extraction_response_file": str(source.get("extraction_response_file", "")),
        "collector_response_file": str(source.get("collector_response_file", "")),
        "live_extraction_response_file": str(source.get("live_extraction_response_file", "")),
    }
    if not raw_extraction_path:
        raise _collector_error(
            "Codex extraction backend unavailable: configure extraction_response_file for replay",
            debug_metadata={
                "collector_kind": "codex-extraction",
                "safe_failure_status": "missing_extraction_artifact_config",
            },
        )
    extraction_path = resolve_codex_artifact_path(config_path, source, str(raw_extraction_path))
    extraction_debug["extraction_response_path"] = str(raw_extraction_path)
    try:
        raw_text = extraction_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise _collector_error(
            f"Codex extraction file not found: {raw_extraction_path}",
            debug_metadata={
                "collector_kind": "codex-extraction",
                "safe_failure_status": "missing_extraction_file",
                "missing_file": str(raw_extraction_path),
            },
        )
    try:
        extraction_data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise _collector_error(
            f"Codex extraction response file is malformed JSON: {exc.msg}",
            debug_metadata={
                "collector_kind": "codex-extraction",
                "safe_failure_status": "malformed_json",
                "malformed_file": str(raw_extraction_path),
            },
        )
    if isinstance(extraction_data, dict) and "response" in extraction_data and extraction_data.get("collection_mode"):
        extraction_data = extraction_data["response"]
    return (
        page_data,
        extraction_data,
        {
            **page_debug,
            **extraction_debug,
            # Only when a capture file was actually configured and read. With
            # none configured the page body above is a synthesized empty
            # string, and reporting that as "the capture held no page content
            # (0 bytes)" would name a capture defect for what is a missing
            # configuration — the same conflation issue #700 exists to remove.
            **(capture_content_evidence(page_data) if raw_page_path else {}),
            "live_fetch": False,
        },
    )


# Fields Codex must provide explicitly (source_check_date is auto-filled).
_CODEX_REQUIRED_FIELDS = {
    "title",
    "signal_type",
    "signal_strength",
    "evidence_note",
}

# Canonical missing-field names in the sanitized aggregate, in stable order.
# ``primary_authors`` is satisfied by either ``primary_authors`` or
# ``authors``; ``source_url`` is satisfied by ``source_url``,
# ``evidence_url``, or ``item_url``.
CODEX_CANONICAL_MISSING_FIELDS = (
    "title",
    "signal_type",
    "signal_strength",
    "evidence_note",
    "primary_authors",
    "source_url",
)


@dataclass(frozen=True)
class MissingRequiredFieldsAggregate:
    """Typed sanitized aggregate for the missing-required-fields diagnostic.

    Holds exact counts only: how many raw entries were examined, how many
    were rejected, and how often each canonical required field was missing.
    It never stores titles, author names, URLs, prose, raw output,
    credentials, or tokens.
    """

    examined_entries: int
    rejected_entries: int
    missing_field_counts: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "examined_entries": self.examined_entries,
            "rejected_entries": self.rejected_entries,
            "missing_field_counts": dict(self.missing_field_counts),
        }


def _codex_missing_required_fields(entry: dict[str, Any], *, author_optional: bool = False) -> list[str]:
    """Return the canonical required-field names missing from one raw entry.

    source_check_date is exempt — it is always auto-filled from run_date.
    Primary authors are satisfied by either 'primary_authors' or 'authors';
    the URL requirement by 'source_url', 'evidence_url', or 'item_url'.
    """
    from autoscout.source_mentions import source_mention_field_present

    missing = sorted(field for field in _CODEX_REQUIRED_FIELDS if not source_mention_field_present(entry, field))
    # primary_authors OR authors must be present.
    has_primary = source_mention_field_present(entry, "primary_authors")
    has_authors = isinstance(entry.get("authors"), list) and any(str(a).strip() for a in entry.get("authors", []))
    if not has_primary and not has_authors and not author_optional:
        missing.append("primary_authors")
    # source_url, evidence_url, or item_url must be present.
    has_source = bool(str(entry.get("source_url", "") or "").strip())
    has_evidence = bool(str(entry.get("evidence_url", "") or "").strip())
    has_item = bool(str(entry.get("item_url", "") or "").strip())
    if not has_source and not has_evidence and not has_item:
        missing.append("source_url")
    return sorted(missing)


def codex_missing_field_aggregate(
    entries: list[Any], *, author_optional: bool = False
) -> MissingRequiredFieldsAggregate | None:
    """Return the sanitized missing-required-field aggregate for raw entries.

    Returns None when no entry was rejected. ``examined_entries`` is the raw
    entry count, ``rejected_entries`` counts the entries that failed
    required-field validation, and ``missing_field_counts`` counts each
    canonical missing field name across rejected entries. The result is
    deterministic and content-free.
    """
    if not entries:
        return None
    examined = len(entries)
    rejected = 0
    counts: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            rejected += 1
            for field in CODEX_CANONICAL_MISSING_FIELDS:
                counts[field] = counts.get(field, 0) + 1
            continue
        missing = _codex_missing_required_fields(entry, author_optional=author_optional)
        if not missing:
            continue
        rejected += 1
        for field in missing:
            counts[field] = counts.get(field, 0) + 1
    if rejected == 0:
        return None
    return MissingRequiredFieldsAggregate(
        examined_entries=examined,
        rejected_entries=rejected,
        missing_field_counts=counts,
    )


def _validate_codex_raw_required_fields(
    entries: list[dict[str, Any]], *, author_optional: bool = False
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Check raw Codex extraction entries for required Source Mention fields.
    Validates BEFORE defaults are applied so missing signal_strength,
    evidence_note, etc. are caught as artifact quality issues.
    source_check_date is exempt — it is always auto-filled from run_date.
    Primary authors are satisfied by either 'primary_authors' or 'authors'."""
    valid: list[dict[str, Any]] = []
    dropped: list[dict[str, str]] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            dropped.append(
                {
                    "title": f"mention {index}",
                    "evidence_url": "",
                    "reason": "entry is not an object",
                }
            )
            continue
        missing = _codex_missing_required_fields(entry, author_optional=author_optional)
        if missing:
            dropped.append(
                {
                    "title": str(entry.get("title") or f"mention {index}"),
                    "evidence_url": str(entry.get("evidence_url", "")),
                    "reason": f"missing required field(s): {', '.join(missing)}",
                }
            )
            continue
        valid.append(entry)
    return valid, dropped


def _codex_drop_failure_status(field_dropped: list[dict[str, str]], provenance_dropped: list[dict[str, str]]) -> str:
    """Determine the primary safe_failure_status when all mentions were dropped."""
    if any(
        "exact book list" in str(drop.get("reason", "")).casefold()
        and any(
            field_name in str(drop.get("reason", "")).casefold() for field_name in ("signal_type", "signal_strength")
        )
        for drop in provenance_dropped
    ):
        return "invalid_signal_metadata"
    if field_dropped and provenance_dropped:
        return "missing_required_fields_and_unverifiable_evidence"
    if field_dropped:
        return "missing_required_fields"
    return "unverifiable_evidence_urls"


@dataclass(frozen=True)
class CodexExtractionCollector:
    """Registered Collector adapter for Codex extraction replay sources.

    Owns the single ``codex-extraction-replay`` method value: it reads a
    configured capture page and extraction-response artifact (resolved via
    ``codex_artifact_dir`` when configured), applies the same forbidden-field
    and provenance guardrails as agent extraction, and validates Codex's raw
    entries against the required Source Mention fields before normalizing
    them. The collector never inspects the source's ``extraction_method``
    itself -- the dispatcher's registry lookup already guarantees it only
    runs for a source it owns."""

    def collect(
        self,
        config_path: Path,
        output_dir: Path,
        source: dict[str, Any],
        run_date: str,
    ) -> CollectorResult:
        page_data, extraction_data, debug_metadata = configured_codex_extraction_inputs(config_path, source)

        incomplete_debug = validate_extraction_page_identity(
            source,
            page_data,
            collector_kind="codex-extraction",
            freshness_reference="page_reference_date",
        )
        if incomplete_debug:
            debug_metadata.update(incomplete_debug)

        # An undated recurring ranking proves currency by row movement.  The
        # collector only observes; the collection stage judges the fingerprint
        # against retained state and fails a frozen list closed.
        if ranking_undated_declared(source):
            debug_metadata[FINGERPRINT_DEBUG_FIELD] = ranking_row_fingerprint(
                ranking_rows(str(page_data.get("html", "")), source)
            )

        if isinstance(extraction_data, dict) and extraction_data.get("status"):
            status = str(extraction_data.get("status", "")).strip().lower()
            if status in {"captcha", "blocked", "access-denied"}:
                raise _collector_error(
                    f"Codex extraction skipped: source or backend reported {status}",
                    status="skipped",
                    debug_metadata={
                        **debug_metadata,
                        "collector_kind": "codex-extraction",
                        "safe_failure_status": "source_blocked",
                        "extraction_status": status,
                    },
                )
            if status in {"ok", "success", "complete"}:
                # Valid extraction status — continue to deserialize mentions.
                pass
            else:
                raise _collector_error(
                    f"Codex extraction failed: response reported error status {status}",
                    debug_metadata={
                        **debug_metadata,
                        "collector_kind": "codex-extraction",
                        "safe_failure_status": "extraction_status_error",
                        "extraction_status": status,
                    },
                )

        # Extract raw entries from the extraction data.
        raw_entries: list[dict[str, Any]] = []
        entries = extraction_entries(extraction_data)
        if isinstance(entries, list):
            raw_entries = entries
        else:
            # Diagnose by shape, not truthiness: null, {}, "", and any other
            # non-list value are all malformed mentions.
            raise _collector_error(
                "Codex extraction response mentions must be a list",
                raw_mention_count=0,
                valid_mention_count=0,
                debug_metadata={
                    **debug_metadata,
                    "collector_kind": "codex-extraction",
                    "safe_failure_status": "malformed_mentions",
                },
            )

        forbidden_entries = [
            entry
            for entry in raw_entries
            if isinstance(entry, dict) and (FORBIDDEN_COLLECTOR_RESPONSE_FIELDS & set(entry))
        ]
        if forbidden_entries:
            forbidden_fields = sorted(
                {field for entry in forbidden_entries for field in (FORBIDDEN_COLLECTOR_RESPONSE_FIELDS & set(entry))}
            )
            raise _collector_error(
                f"Codex extraction response stores forbidden copied text fields: {forbidden_fields}",
                raw_mention_count=len(raw_entries),
                valid_mention_count=0,
                skipped_item_count=len(forbidden_entries),
                debug_metadata={
                    **debug_metadata,
                    "collector_kind": "codex-extraction",
                    "safe_failure_status": "forbidden_copied_text",
                    "forbidden_fields": forbidden_fields,
                    "extracted_mention_count": len(raw_entries),
                    "validated_mention_count": 0,
                    "dropped_mention_count": len(forbidden_entries),
                },
            )

        # Validate required fields on raw entries BEFORE defaults are applied,
        # so missing signal_strength, evidence_note, etc. are caught as
        # Codex artifact quality issues, not silently filled with placeholders.
        # Only source_check_date is exempt — it is always set from run_date.
        author_optional = mention_author_exempt(source)
        field_valid, field_dropped = _validate_codex_raw_required_fields(raw_entries, author_optional=author_optional)
        # Rows the artifact gate already refused travel in the published
        # artifact; they count toward every 80% rule below (Issue #839).
        gate_dropped = gate_dropped_entries(extraction_data)
        valid_ids = {id(entry) for entry in field_valid}
        upstream_dropped_ranks = gate_dropped_ranks(extraction_data) + [
            claimed_rank(entry) for entry in raw_entries if id(entry) not in valid_ids
        ]

        def bookmarks_field_failure() -> CollectorRunError:
            aggregate = codex_missing_field_aggregate(raw_entries, author_optional=author_optional)
            return _collector_error(
                "Codex extraction failed: BookMarks artifact contains entries with missing required fields",
                raw_mention_count=len(raw_entries),
                valid_mention_count=0,
                skipped_item_count=len(raw_entries),
                debug_metadata={
                    **debug_metadata,
                    "collector_kind": "codex-extraction",
                    "safe_failure_status": "missing_required_fields",
                    "extracted_mention_count": len(raw_entries),
                    "validated_mention_count": 0,
                    "dropped_mention_count": len(raw_entries),
                    "field_dropped_count": len(field_dropped),
                    "provenance_dropped_count": 0,
                    "drop_reasons": field_dropped,
                    "validation_missing_required_fields": aggregate.to_dict() if aggregate is not None else None,
                },
            )

        bookmarks_rows = is_bookmarks_source_identity(source) and bool(field_dropped)
        extracted_rows = len(raw_entries) + len(gate_dropped)
        if bookmarks_rows and not keeps_partial_list(len(field_valid), extracted_rows):
            raise bookmarks_field_failure()

        # Normalize only the field-valid entries (fills source_check_date,
        # remaps field names like authors→primary_authors).
        normalized_entries = [
            normalized_agent_mention(entry, source, run_date) for entry in field_valid if isinstance(entry, dict)
        ]
        if source.get("audible_explicit_chart") is True:
            for entry in normalized_entries:
                entry.pop("retailer_discovery", None)
                entry.pop("source_check_date", None)
        elif source.get("amazon_charts_weekly") is True:
            for entry in normalized_entries:
                entry.pop("source_check_date", None)

        extraction_data_for_validation = (
            {"mentions": normalized_entries, "_code_normalized": True} if normalized_entries else normalized_entries
        )

        try:
            helper_index = load_helper_metadata(config_path, source)
        except HelperMetadataError as exc:
            raise _collector_error(
                "Codex extraction helper metadata failed closed",
                raw_mention_count=len(raw_entries),
                valid_mention_count=0,
                skipped_item_count=0,
                debug_metadata={
                    **debug_metadata,
                    "collector_kind": "codex-extraction",
                    "safe_failure_status": exc.reason_code,
                },
            ) from exc
        mentions, provenance_dropped = agent_extraction_mentions_from_response(
            page_data,
            extraction_data_for_validation,
            source,
            run_date,
            collector_kind="codex-extraction",
            helper_index=helper_index,
            upstream_dropped_ranks=upstream_dropped_ranks,
        )

        all_dropped = gate_dropped + field_dropped + provenance_dropped
        # The 80% rule counts every drop, including the collector's own.
        if collected_list_refused(
            len(mentions), extracted_rows, gate_dropped=len(gate_dropped), bookmarks_field_dropped=bookmarks_rows
        ):
            if bookmarks_rows:
                raise bookmarks_field_failure()
            raise _collector_error(
                "Codex extraction failed: fewer than 80% of the extracted mentions passed validation",
                raw_mention_count=extracted_rows,
                valid_mention_count=len(mentions),
                skipped_item_count=len(all_dropped),
                debug_metadata={
                    **debug_metadata,
                    "collector_kind": "codex-extraction",
                    "safe_failure_status": "partial_list_below_threshold",
                    "extracted_mention_count": extracted_rows,
                    "validated_mention_count": len(mentions),
                    "dropped_mention_count": len(all_dropped),
                    "drop_reasons": all_dropped,
                },
            )

        if not mentions and all_dropped:
            failure_status = _codex_drop_failure_status(field_dropped, provenance_dropped)
            raise _collector_error(
                "Codex extraction failed: all extracted mentions were dropped by validation guardrails",
                raw_mention_count=len(all_dropped),
                valid_mention_count=0,
                skipped_item_count=len(all_dropped),
                debug_metadata={
                    **debug_metadata,
                    "collector_kind": "codex-extraction",
                    "safe_failure_status": failure_status,
                    "extracted_mention_count": len(all_dropped),
                    "validated_mention_count": 0,
                    "dropped_mention_count": len(all_dropped),
                    "field_dropped_count": len(field_dropped),
                    "provenance_dropped_count": len(provenance_dropped),
                    "drop_reasons": all_dropped,
                },
            )
        if not mentions:
            # The extractor's own statement of why it returned nothing is kept
            # beside the verdict, so an abstention reads differently from an
            # empty page in the run record (Issue #740).
            empty_reason = extractor_empty_reason(extraction_data) if not raw_entries else ""
            if empty_reason:
                debug_metadata = {**debug_metadata, "extractor_empty_reason": empty_reason}
            if authoritative_empty_codex_artifact(config_path, source, page_data, extraction_data):
                return CollectorResult(
                    mentions=[],
                    raw_mention_count=0,
                    valid_mention_count=0,
                    debug_metadata={
                        **debug_metadata,
                        "collector_kind": "codex-extraction",
                        "safe_failure_status": "healthy_empty",
                        "extracted_mention_count": 0,
                        "validated_mention_count": 0,
                    },
                )
            raise _collector_error(
                "Codex extraction failed: extraction returned no mentions",
                raw_mention_count=0,
                valid_mention_count=0,
                debug_metadata={
                    **debug_metadata,
                    "collector_kind": "codex-extraction",
                    "safe_failure_status": "empty_extraction",
                    "extracted_mention_count": 0,
                    "validated_mention_count": 0,
                    "dropped_mention_count": 0,
                    "drop_reasons": [],
                },
            )

        debug_metadata.update(
            record_seen_index_items(config_path, source, page_data, mentions, len(all_dropped), run_date)
        )
        if source.get("capture_normalized_mentions_file"):
            capture_normalized_mentions(config_path, output_dir, source, mentions)
        debug_metadata.update(
            {
                "collector_kind": "codex-extraction",
                "extracted_mention_count": len(mentions) + len(all_dropped),
                "validated_mention_count": len(mentions),
                "dropped_mention_count": len(all_dropped),
                "field_dropped_count": len(field_dropped),
                "provenance_dropped_count": len(provenance_dropped),
                "drop_reasons": all_dropped,
                "safe_failure_status": "healthy",
                **({"gate_dropped_count": len(gate_dropped)} if gate_dropped else {}),
                **partial_list_marker(
                    source, kept=len(mentions), dropped=len(all_dropped), gate_dropped=len(gate_dropped)
                ),
            }
        )
        return CollectorResult(
            mentions=mentions,
            raw_mention_count=len(mentions) + len(all_dropped),
            valid_mention_count=len(mentions),
            skipped_item_count=len(all_dropped),
            debug_metadata=debug_metadata,
        )


def authoritative_empty_codex_artifact(
    config_path: Path,
    source: dict[str, Any],
    page_data: dict[str, Any],
    extraction_data: Any,
) -> bool:
    """Accept empty production artifacts only with their complete capture binding."""
    if not isinstance(extraction_data, dict):
        return False
    present_lists = [extraction_data[key] for key in ("entries", "mentions") if key in extraction_data]
    if not present_lists or any(value != [] for value in present_lists):
        return False
    operation_id = str(extraction_data.get("operation_id", "")).strip()
    capture_sha256 = str(extraction_data.get("capture_sha256", "")).strip()
    source_name = str(source.get("name", ""))
    source_url = str(source.get("url", ""))
    if not operation_id or not capture_sha256 or not source_name or not source_url:
        return False
    if extraction_data.get("source_name") != source_name or extraction_data.get("source_url") != source_url:
        return False
    if page_data.get("source_name") != source_name or page_data.get("source_url") != source_url:
        return False
    resolved_url = str(page_data.get("resolved_url", "")).strip()
    if resolved_url and normalize_source_identity(resolved_url) != normalize_source_identity(source_url):
        return False
    raw_capture = source.get("source_page_fixture_file") or source.get("live_response_file")
    if not raw_capture:
        return False
    capture_path = resolve_codex_artifact_path(config_path, source, str(raw_capture))
    try:
        digest = hashlib.sha256(capture_path.read_bytes()).hexdigest()
    except OSError:
        return False
    if digest != capture_sha256:
        return False
    return not capture_contains_bookmarks_listing_rows(source, page_data)


def codex_extraction_collector_result(
    config_path: Path,
    output_dir: Path,
    source: dict[str, Any],
    run_date: str,
) -> CollectorResult:
    """Thin delegate to the registered ``codex-extraction-replay`` adapter.

    Preserved because existing tests call this function directly (the
    per-collector test seam described in PRD #231); the dispatcher in
    ``source_candidates()`` reaches the same adapter through
    ``COLLECTOR_REGISTRY``."""
    return CodexExtractionCollector().collect(config_path, output_dir, source, run_date)
