"""Per-collector and routing tests for the Codex Extraction Collector
registry adapter.

These exercise ``CodexExtractionCollector`` directly through the
``Collector`` protocol with fixture files (no pipeline run), and assert
that ``source_candidates()`` reaches the same registered collector -- the
two seams described in PRD #231 / issue #236. ``codex_extraction_collector_result``
is a thin delegate to the same adapter, preserved because the pre-existing
end-to-end test suite (``tests/test_source_collection.py``) calls it
directly; these tests exercise the adapter itself and its registry
registration.
"""

from __future__ import annotations

import json
from pathlib import Path

from autoscout.source_collection import source_candidates
from autoscout.source_collectors import COLLECTOR_REGISTRY
from autoscout.source_collectors import CodexExtractionCollector
from autoscout.source_collectors import CollectorResult
from autoscout.source_collectors import CollectorRunError
from autoscout.source_collectors import codex_extraction_collector_result
from tests.fixtures import copy_fixture

RUN_DATE = "2026-07-07"


def _codex_source(**overrides) -> dict:
    source = {
        "name": "Codex Extracted Source",
        "family": "media/curated list",
        "url": "https://example.test/nonfiction",
        "access_status": "open",
        "check_frequency": "weekly",
        "language": "en",
    }
    source.update(overrides)
    return source


class CodexExtractionCollectorRegistryTests:
    """The registry owns the single codex-extraction-replay method value."""

    def test_registry_owns_the_codex_extraction_replay_method_value(self):
        assert "codex-extraction-replay" in COLLECTOR_REGISTRY

    def test_registration_is_a_codex_extraction_collector(self):
        assert isinstance(COLLECTOR_REGISTRY["codex-extraction-replay"], CodexExtractionCollector)


class CodexExtractionCollectorDirectTests:
    """Exercise the Codex extraction collector directly through
    Collector.collect() with fixture files -- no pipeline run."""

    def test_replay_happy_path(self, tmp_path: Path):
        page_path = copy_fixture("source-pages", "example-nonfiction-codex.json", tmp_path / "page.json")
        extraction_path = copy_fixture(
            "extraction-responses", "example-nonfiction-codex.json", tmp_path / "extraction.json"
        )
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _codex_source(
            extraction_method="codex-extraction-replay",
            source_page_fixture_file=str(page_path),
            extraction_response_file=str(extraction_path),
        )

        result = COLLECTOR_REGISTRY["codex-extraction-replay"].collect(config_path, tmp_path / "out", source, RUN_DATE)

        assert isinstance(result, CollectorResult)
        assert result.raw_mention_count == 1
        assert result.valid_mention_count == 1
        assert result.skipped_item_count == 0
        assert result.debug_metadata["collector_kind"] == "codex-extraction"
        assert result.mentions[0]["title"] == "Discovered Book"
        assert result.mentions[0]["primary_authors"] == ["Jane Author"]

    def test_missing_required_field_drops_mention_and_raises_failed(self, tmp_path: Path):
        page_path = copy_fixture("source-pages", "example-nonfiction-codex.json", tmp_path / "page.json")
        extraction_path = tmp_path / "extraction.json"
        extraction_path.write_text(
            json.dumps(
                {
                    "mentions": [
                        {
                            "title": "Discovered Book",
                            "primary_authors": ["Jane Author"],
                            "source_url": "https://example.test/books/discovered",
                            "evidence_url": "https://example.test/books/discovered",
                            "signal_type": "media/curated list",
                            # signal_strength and evidence_note deliberately omitted.
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _codex_source(
            extraction_method="codex-extraction-replay",
            source_page_fixture_file=str(page_path),
            extraction_response_file=str(extraction_path),
        )

        try:
            COLLECTOR_REGISTRY["codex-extraction-replay"].collect(config_path, tmp_path / "out", source, RUN_DATE)
            raised = None
        except CollectorRunError as exc:
            raised = exc

        assert raised is not None
        assert raised.status == "failed"
        assert raised.debug_metadata["safe_failure_status"] == "missing_required_fields"

    def test_missing_extraction_response_file_raises_failed(self, tmp_path: Path):
        page_path = copy_fixture("source-pages", "example-nonfiction-codex.json", tmp_path / "page.json")
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _codex_source(
            extraction_method="codex-extraction-replay",
            source_page_fixture_file=str(page_path),
        )

        try:
            COLLECTOR_REGISTRY["codex-extraction-replay"].collect(config_path, tmp_path / "out", source, RUN_DATE)
            raised = None
        except CollectorRunError as exc:
            raised = exc

        assert raised is not None
        assert raised.status == "failed"
        assert raised.debug_metadata["safe_failure_status"] == "missing_extraction_artifact_config"

    def test_thin_delegate_matches_the_registered_adapter(self, tmp_path: Path):
        page_path = copy_fixture("source-pages", "example-nonfiction-codex.json", tmp_path / "page.json")
        extraction_path = copy_fixture(
            "extraction-responses", "example-nonfiction-codex.json", tmp_path / "extraction.json"
        )
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _codex_source(
            extraction_method="codex-extraction-replay",
            source_page_fixture_file=str(page_path),
            extraction_response_file=str(extraction_path),
        )

        via_delegate = codex_extraction_collector_result(config_path, tmp_path / "out", source, RUN_DATE)
        via_registry = COLLECTOR_REGISTRY["codex-extraction-replay"].collect(
            config_path, tmp_path / "out", source, RUN_DATE
        )

        assert via_delegate.mentions == via_registry.mentions
        assert via_delegate.debug_metadata == via_registry.debug_metadata


class SourceCandidatesRoutesCodexExtractionThroughRegistryTests:
    """source_candidates() is the dispatcher: it normalizes extraction_method
    once and must reach the same registered collector, producing identical
    results to calling the collector directly."""

    def test_dispatch_matches_direct_collector_call(self, tmp_path: Path):
        page_path = copy_fixture("source-pages", "example-nonfiction-codex.json", tmp_path / "page.json")
        extraction_path = copy_fixture(
            "extraction-responses", "example-nonfiction-codex.json", tmp_path / "extraction.json"
        )
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _codex_source(
            extraction_method="codex-extraction-replay",
            source_page_fixture_file=str(page_path),
            extraction_response_file=str(extraction_path),
        )

        direct = COLLECTOR_REGISTRY["codex-extraction-replay"].collect(config_path, tmp_path / "out", source, RUN_DATE)
        dispatched = source_candidates(config_path, tmp_path / "out", source, RUN_DATE)

        assert dispatched.raw_mention_count == direct.raw_mention_count
        assert dispatched.valid_mention_count == direct.valid_mention_count
        assert dispatched.skipped_item_count == direct.skipped_item_count
        assert dispatched.debug_metadata == direct.debug_metadata
        assert dispatched.mentions[0]["title"] == direct.mentions[0]["title"]

    def test_dispatch_propagates_collector_run_error_unchanged(self, tmp_path: Path):
        page_path = copy_fixture("source-pages", "example-nonfiction-codex.json", tmp_path / "page.json")
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _codex_source(
            extraction_method="codex-extraction-replay",
            source_page_fixture_file=str(page_path),
        )

        try:
            source_candidates(config_path, tmp_path / "out", source, RUN_DATE)
            raised = None
        except CollectorRunError as exc:
            raised = exc

        assert raised is not None
        assert raised.status == "failed"

    def test_dispatch_does_not_touch_non_codex_methods(self, tmp_path: Path):
        """An extraction_method absent from the codex registration (e.g.
        NPR's) must not be picked up by this branch."""
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _codex_source(extraction_method="npr-source-page")

        dispatched = source_candidates(config_path, tmp_path / "out", source, RUN_DATE)

        assert dispatched.mentions == []
        assert dispatched.raw_mention_count == 0
