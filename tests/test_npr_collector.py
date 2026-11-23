"""Per-collector and routing tests for the NPR Collector registry adapter.

These exercise ``NPRCollector`` directly through the ``Collector`` protocol
with fixture files (no pipeline run), and assert that ``source_candidates()``
reaches the same registered collector -- the two seams described in PRD #231
/ issue #236.
"""

from __future__ import annotations

from pathlib import Path

from autoscout.source_collection import source_candidates
from autoscout.source_collectors import COLLECTOR_REGISTRY
from autoscout.source_collectors import CollectorResult
from autoscout.source_collectors import CollectorRunError
from autoscout.source_collectors import NPRCollector
from tests.fixtures import copy_fixture

RUN_DATE = "2026-07-07"


def _npr_source(**overrides) -> dict:
    source = {
        "name": "NPR Books We Love",
        "family": "media/curated list",
        "url": "https://www.npr.org/books-we-love/",
        "access_status": "open",
        "check_frequency": "weekly",
    }
    source.update(overrides)
    return source


class NPRCollectorRegistryTests:
    """The registry owns both NPR method values with the same adapter
    class, distinguished only by the registered variant."""

    def test_registry_owns_both_npr_method_values(self):
        assert {"npr-source-page", "npr-live"}.issubset(set(COLLECTOR_REGISTRY))

    def test_each_npr_registration_is_an_npr_collector(self):
        for method, collector in COLLECTOR_REGISTRY.items():
            if method.startswith("npr-"):
                assert isinstance(collector, NPRCollector)

    def test_source_page_and_live_variants_are_distinct_registrations(self):
        source_page = COLLECTOR_REGISTRY["npr-source-page"]
        live = COLLECTOR_REGISTRY["npr-live"]
        assert source_page.variant == "source-page"
        assert live.variant == "live"
        assert source_page is not live


class NPRCollectorDirectTests:
    """Exercise the NPR collector directly through Collector.collect() with
    fixture files -- no pipeline run."""

    def test_source_page_happy_path(self, tmp_path: Path):
        response_path = copy_fixture("npr-responses", "recorded-happy.json", tmp_path / "response.json")
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _npr_source(
            extraction_method="npr-source-page",
            source_page_fixture_file=str(response_path),
        )

        result = COLLECTOR_REGISTRY["npr-source-page"].collect(config_path, tmp_path / "out", source, RUN_DATE)

        assert isinstance(result, CollectorResult)
        assert result.raw_mention_count == 1
        assert result.valid_mention_count == 1
        assert result.skipped_item_count == 0
        assert result.debug_metadata["collector_kind"] == "npr"
        assert result.mentions[0]["title"] == "NPR Registry Field Notes"
        assert result.mentions[0]["primary_authors"] == ["Casey Fixture"]

    def test_live_variant_replays_configured_fixture_without_fetching(self, tmp_path: Path):
        response_path = copy_fixture("npr-responses", "live-replay-happy.json", tmp_path / "response.json")
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _npr_source(
            extraction_method="npr-live",
            enable_live=True,
            live_response_file=str(response_path),
        )

        result = COLLECTOR_REGISTRY["npr-live"].collect(config_path, tmp_path / "out", source, RUN_DATE)

        assert result.mentions[0]["title"] == "Registered Live NPR Book"
        assert result.debug_metadata["live_fixture_replay"] is True

    def test_live_variant_requires_enable_live(self, tmp_path: Path):
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _npr_source(extraction_method="npr-live")

        try:
            COLLECTOR_REGISTRY["npr-live"].collect(config_path, tmp_path / "out", source, RUN_DATE)
            raised = None
        except CollectorRunError as exc:
            raised = exc

        assert raised is not None
        assert raised.status == "skipped"

    def test_captcha_blocked_response_raises_skipped_collector_run_error(self, tmp_path: Path):
        response_path = copy_fixture("bookpage-responses", "blocked-captcha.json", tmp_path / "response.json")
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _npr_source(
            extraction_method="npr-source-page",
            source_page_fixture_file=str(response_path),
        )

        try:
            COLLECTOR_REGISTRY["npr-source-page"].collect(config_path, tmp_path / "out", source, RUN_DATE)
            raised = None
        except CollectorRunError as exc:
            raised = exc

        assert raised is not None
        assert raised.status == "skipped"
        assert "CAPTCHA" in str(raised)


class SourceCandidatesRoutesNPRThroughRegistryTests:
    """source_candidates() is the dispatcher: it normalizes extraction_method
    once and must reach the same registered collector, producing identical
    results to calling the collector directly."""

    def test_dispatch_matches_direct_collector_call(self, tmp_path: Path):
        response_path = copy_fixture("npr-responses", "recorded-happy.json", tmp_path / "response.json")
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _npr_source(
            extraction_method="npr-source-page",
            source_page_fixture_file=str(response_path),
        )

        direct = COLLECTOR_REGISTRY["npr-source-page"].collect(config_path, tmp_path / "out", source, RUN_DATE)
        dispatched = source_candidates(config_path, tmp_path / "out", source, RUN_DATE)

        assert dispatched.raw_mention_count == direct.raw_mention_count
        assert dispatched.valid_mention_count == direct.valid_mention_count
        assert dispatched.skipped_item_count == direct.skipped_item_count
        assert dispatched.debug_metadata == direct.debug_metadata
        assert dispatched.mentions[0]["title"] == direct.mentions[0]["title"]

    def test_dispatch_selects_live_variant(self, tmp_path: Path):
        response_path = copy_fixture("npr-responses", "live-replay-happy.json", tmp_path / "response.json")
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _npr_source(
            extraction_method="npr-live",
            enable_live=True,
            live_response_file=str(response_path),
        )

        dispatched = source_candidates(config_path, tmp_path / "out", source, RUN_DATE)

        assert dispatched.mentions[0]["title"] == "Registered Live NPR Book"

    def test_dispatch_propagates_collector_run_error_unchanged(self, tmp_path: Path):
        response_path = copy_fixture("bookpage-responses", "blocked-captcha.json", tmp_path / "response.json")
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _npr_source(
            extraction_method="npr-source-page",
            source_page_fixture_file=str(response_path),
        )

        try:
            source_candidates(config_path, tmp_path / "out", source, RUN_DATE)
            raised = None
        except CollectorRunError as exc:
            raised = exc

        assert raised is not None
        assert raised.status == "skipped"

    def test_dispatch_does_not_touch_non_npr_methods(self, tmp_path: Path):
        """An extraction_method absent from the NPR registrations (e.g.
        BookPage's) must not be picked up by the NPR dispatch branch."""
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _npr_source(extraction_method="bookpage-recorded-response")

        dispatched = source_candidates(config_path, tmp_path / "out", source, RUN_DATE)

        assert dispatched.mentions == []
        assert dispatched.raw_mention_count == 0
