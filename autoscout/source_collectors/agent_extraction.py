"""The registered agent-extraction collector and the inputs it reads (page capture and extraction response)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from autoscout.capture_bounds import CaptureWriteError
from autoscout.helper_metadata import HelperMetadataError
from autoscout.helper_metadata import load_helper_metadata
from autoscout.partial_list import partial_list_marker
from autoscout.ranking_freshness import FINGERPRINT_DEBUG_FIELD
from autoscout.ranking_freshness import ranking_row_fingerprint
from autoscout.ranking_freshness import ranking_undated_declared
from autoscout.source_collectors.agent_backends import configured_extraction_response
from autoscout.source_collectors.agent_mentions import agent_extraction_mentions_from_response
from autoscout.source_collectors.base import CollectorResult
from autoscout.source_collectors.base import FreshnessReference
from autoscout.source_collectors.base import _collector_error
from autoscout.source_collectors.base import capture_content_evidence
from autoscout.source_collectors.base import capture_extraction_response
from autoscout.source_collectors.base import capture_live_response
from autoscout.source_collectors.base import capture_normalized_mentions
from autoscout.source_collectors.base import collectors_pkg
from autoscout.source_collectors.base import record_seen_index_items
from autoscout.source_collectors.base import resolve_config_path
from autoscout.source_collectors.base import seen_index_capture_has_no_rows
from autoscout.source_collectors.base import validate_extraction_page_identity
from autoscout.source_page_identity import ranking_rows


@dataclass(frozen=True)
class AgentExtractionCollector:
    """Registered Collector adapter for Agent-Extracted Mention sources.

    One instance is registered per method variant it owns; the
    ``configured`` variant owns both ``agent-extraction`` and
    ``agent-extraction-replay`` (they behave identically today -- both read
    a configured page and extraction response fixture), and the ``live``
    variant owns ``agent-extraction-live``. The agent-extraction backend and
    transport seam (fake/fixture/real backends, injectable transport) sits
    untouched beneath this collector."""

    variant: str

    def collect(
        self,
        config_path: Path,
        output_dir: Path,
        source: dict[str, Any],
        run_date: str,
    ) -> CollectorResult:
        if self.variant == "live":
            page_data, extraction_data, debug_metadata = live_agent_extraction_inputs(
                config_path, output_dir, source, run_date
            )
        else:
            page_data, extraction_data, debug_metadata = configured_agent_extraction_inputs(
                config_path, source, run_date
            )
        # An undated recurring ranking proves currency by row movement, so the
        # collector observes the bounded fingerprint and the collection stage
        # judges it against retained state.  A collector that does not observe
        # leaves the field absent, and an undated source then fails closed.
        if ranking_undated_declared(source):
            debug_metadata[FINGERPRINT_DEBUG_FIELD] = ranking_row_fingerprint(
                ranking_rows(str(page_data.get("html", "")), source)
            )

        try:
            helper_index = load_helper_metadata(config_path, source)
        except HelperMetadataError as exc:
            raise _collector_error(
                "Agent extraction helper metadata failed closed",
                debug_metadata={
                    "collector_kind": "agent-extraction",
                    "safe_failure_status": exc.reason_code,
                },
            ) from exc
        mentions, dropped = agent_extraction_mentions_from_response(
            page_data, extraction_data, source, run_date, helper_index=helper_index
        )
        if not mentions and dropped:
            raise _collector_error(
                "Agent extraction failed: all extracted mentions were dropped by provenance guardrails",
                raw_mention_count=len(dropped),
                valid_mention_count=0,
                skipped_item_count=len(dropped),
                debug_metadata={
                    **debug_metadata,
                    "collector_kind": "agent-extraction",
                    "extracted_mention_count": len(dropped),
                    "validated_mention_count": 0,
                    "dropped_mention_count": len(dropped),
                    "drop_reasons": dropped,
                },
            )
        if not mentions and seen_index_capture_has_no_rows(source, page_data):
            # Every book on the captured pages was reported by an earlier run: nothing new is this source's
            # normal state, not a failed extraction.
            return CollectorResult(
                mentions=[],
                raw_mention_count=0,
                valid_mention_count=0,
                debug_metadata={
                    **debug_metadata,
                    "collector_kind": "agent-extraction",
                    "safe_failure_status": "healthy_empty",
                    "extracted_mention_count": 0,
                    "validated_mention_count": 0,
                },
            )
        if not mentions:
            raise _collector_error(
                "Agent extraction failed: extraction returned no mentions",
                raw_mention_count=0,
                valid_mention_count=0,
                debug_metadata={
                    **debug_metadata,
                    "collector_kind": "agent-extraction",
                    "extracted_mention_count": 0,
                    "validated_mention_count": 0,
                    "dropped_mention_count": 0,
                    "drop_reasons": [],
                },
            )

        debug_metadata.update(record_seen_index_items(config_path, source, page_data, mentions, len(dropped), run_date))
        if source.get("capture_normalized_mentions_file"):
            capture_normalized_mentions(config_path, output_dir, source, mentions)
        debug_metadata.update(
            {
                "collector_kind": "agent-extraction",
                "extracted_mention_count": len(mentions) + len(dropped),
                "validated_mention_count": len(mentions),
                "dropped_mention_count": len(dropped),
                "drop_reasons": dropped,
                **partial_list_marker(source, kept=len(mentions), dropped=len(dropped), gate_dropped=0),
            }
        )
        return CollectorResult(
            mentions=mentions,
            raw_mention_count=len(mentions) + len(dropped),
            valid_mention_count=len(mentions),
            skipped_item_count=len(dropped),
            debug_metadata=debug_metadata,
        )


def configured_agent_extraction_inputs(
    config_path: Path, source: dict[str, Any], run_date: str | None = None
) -> tuple[dict[str, Any], Any, dict[str, Any]]:
    page_data, page_debug = configured_source_page(config_path, source)
    incomplete_debug = validate_extraction_page_identity(
        source,
        page_data,
        collector_kind="agent-extraction",
        freshness_reference="page_reference_date",
    )
    extraction_data, extraction_debug = configured_extraction_response(config_path, source)
    # Issue #700 part A: the zero-contribution seam tells a shell capture from
    # an empty extraction by byte count, so every path that holds a capture
    # must attach it — not only the codex replay path. Without this the live
    # weekly run, which is where every source in #700 failed, reports
    # "unknown size" and the distinction exists only in fixtures.
    merged = {**page_debug, **extraction_debug, **capture_content_evidence(page_data), "live_fetch": False}
    merged.update(incomplete_debug)
    return page_data, extraction_data, merged


def live_agent_extraction_inputs(
    config_path: Path,
    output_dir: Path,
    source: dict[str, Any],
    run_date: str | None = None,
) -> tuple[dict[str, Any], Any, dict[str, Any]]:
    if not source.get("enable_live"):
        raise _collector_error(
            "Agent extraction live collection requires enable_live: true",
            status="skipped",
            debug_metadata={"enable_live": False, "collector_kind": "agent-extraction"},
        )
    freshness_reference: FreshnessReference
    if source.get("source_page_fixture_file") or source.get("live_response_file"):
        page_data, page_debug = configured_source_page(config_path, source)
        page_debug["live_fixture_replay"] = True
        freshness_reference = "page_reference_date"
    else:
        active_reference_date = date.fromisoformat(run_date) if run_date else None
        page_data, page_debug = collectors_pkg().fetch_agent_source_page(
            config_path,
            output_dir,
            source,
            reference_date=active_reference_date,
        )
        freshness_reference = "active_run_date"

    incomplete_debug = validate_extraction_page_identity(
        source,
        page_data,
        collector_kind="agent-extraction",
        run_date=run_date,
        freshness_reference=freshness_reference,
    )
    if "kirkus_dropped_rows" in page_debug:
        # The Kirkus fetch already merged every coverage record, including a failed pass of the run date
        # that the page itself cannot show; the page's own record would overwrite that caveat (#873).
        incomplete_debug = {}
    extraction_data, extraction_debug = collectors_pkg().live_agent_extraction_response(config_path, source, page_data)
    capture_mode = agent_extraction_capture_mode(page_debug, extraction_debug)
    if source.get("capture_live_response_file"):
        capture_live_response(config_path, output_dir, source, page_data, capture_mode)
    if source.get("capture_extraction_response_file"):
        try:
            capture_extraction_response(config_path, output_dir, source, extraction_data, capture_mode)
        except CaptureWriteError as exc:
            # The extraction response capture is optional observability, not
            # evidence: a bounded capture-write failure is recorded as a
            # diagnostic but never discards valid extracted mentions.
            extraction_debug["capture_extraction_response_write_failure"] = str(exc)
    return (
        page_data,
        extraction_data,
        {
            **page_debug,
            **extraction_debug,
            **incomplete_debug,
            # Issue #700 part A: this is the live path every #700 source ran
            # on; the capture it just fetched is the only place the shell/
            # empty-extraction distinction can come from.
            **capture_content_evidence(page_data),
            "live_fetch": True,
            "capture_collection_mode": capture_mode,
            "capture_live_response_file": str(source.get("capture_live_response_file", "")),
            "capture_extraction_response_file": str(source.get("capture_extraction_response_file", "")),
        },
    )


def agent_extraction_capture_mode(page_debug: dict[str, Any], extraction_debug: dict[str, Any]) -> str:
    backend_mode = str(extraction_debug.get("backend_mode", "")).strip().lower()
    if backend_mode == "real-live-replay":
        return "real-live-replay"
    if page_debug.get("live_fixture_replay"):
        return "live-fixture-replay"
    if backend_mode:
        return backend_mode
    return "live"


def configured_source_page(config_path: Path, source: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_path = source.get("source_page_fixture_file") or source.get("live_response_file")
    debug_metadata = {
        "source_page_fixture_file": str(source.get("source_page_fixture_file", "")),
        "live_response_file": str(source.get("live_response_file", "")),
    }
    if not raw_path:
        url = str(source.get("url", "")).strip()
        return {"source_page": url, "html": ""}, debug_metadata
    path = resolve_config_path(config_path, str(raw_path))
    debug_metadata["response_file"] = str(raw_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, str):
        return {"source_page": str(source.get("url", "")), "html": data}, debug_metadata
    if isinstance(data, dict):
        data.setdefault("source_page", str(source.get("url", "")))
        data.setdefault("html", "")
        return data, debug_metadata
    raise _collector_error("Agent extraction source page fixture must be an object or string")
