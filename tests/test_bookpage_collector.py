"""Per-collector and routing tests for the BookPage Collector registry adapter.

These exercise ``BookPageCollector`` directly through the ``Collector``
protocol with fixture files (no pipeline run), and assert that
``source_candidates()`` reaches the same registered collector and produces
identical results -- the two seams described in PRD #231 / issue #235.
"""

from __future__ import annotations

from pathlib import Path

from autoscout.source_collection import source_candidates
from autoscout.source_collectors import COLLECTOR_REGISTRY
from autoscout.source_collectors import BookPageCollector
from autoscout.source_collectors import CollectorResult
from autoscout.source_collectors import CollectorRunError
from tests.fixtures import copy_fixture

RUN_DATE = "2026-07-07"


def _bookpage_source(**overrides) -> dict:
    source = {
        "name": "BookPage Best Nonfiction",
        "family": "media/curated list",
        "url": "https://www.bookpage.com/best-books/best-nonfiction/",
        "access_status": "open",
        "check_frequency": "weekly",
    }
    source.update(overrides)
    return source


class BookPageCollectorRegistryTests:
    """The registry owns all three BookPage method values with the same
    adapter class, distinguished only by the registered variant."""

    def test_registry_has_all_three_bookpage_method_values(self):
        assert {
            "bookpage-recorded-response",
            "bookpage-source-page",
            "bookpage-live",
        }.issubset(set(COLLECTOR_REGISTRY))

    def test_each_bookpage_registration_is_a_book_page_collector(self):
        for method, collector in COLLECTOR_REGISTRY.items():
            if method.startswith("bookpage-"):
                assert isinstance(collector, BookPageCollector)

    def test_recorded_and_source_page_variants_are_distinct_registrations(self):
        recorded = COLLECTOR_REGISTRY["bookpage-recorded-response"]
        source_page = COLLECTOR_REGISTRY["bookpage-source-page"]
        live = COLLECTOR_REGISTRY["bookpage-live"]
        assert recorded.variant == "recorded-response"
        assert source_page.variant == "source-page"
        assert live.variant == "live"
        assert recorded is not source_page is not live


class BookPageCollectorDirectTests:
    """Exercise the BookPage collector directly through Collector.collect()
    with fixture files -- no pipeline run."""

    def test_recorded_response_happy_path(self, tmp_path: Path):
        response_path = copy_fixture("bookpage-responses", "recorded-happy.json", tmp_path / "response.json")
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _bookpage_source(
            extraction_method="bookpage-recorded-response",
            collector_response_file=str(response_path),
        )

        result = COLLECTOR_REGISTRY["bookpage-recorded-response"].collect(
            config_path, tmp_path / "out", source, RUN_DATE
        )

        assert isinstance(result, CollectorResult)
        assert result.raw_mention_count == 1
        assert result.valid_mention_count == 1
        assert result.skipped_item_count == 0
        assert result.debug_metadata["collector_kind"] == "bookpage"
        assert result.mentions[0]["title"] == "Collector Registry Field Guide"
        assert result.mentions[0]["primary_authors"] == ["Jordan Fixture"]

    def test_source_page_happy_path(self, tmp_path: Path):
        response_path = copy_fixture("bookpage-responses", "source-page-happy.json", tmp_path / "response.json")
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _bookpage_source(
            extraction_method="bookpage-source-page",
            source_page_fixture_file=str(response_path),
        )

        result = COLLECTOR_REGISTRY["bookpage-source-page"].collect(config_path, tmp_path / "out", source, RUN_DATE)

        assert result.mentions[0]["title"] == "Dispatcher's Almanac"
        assert result.debug_metadata["collector_kind"] == "bookpage"

    def test_live_variant_replays_configured_fixture_without_fetching(self, tmp_path: Path):
        response_path = copy_fixture("bookpage-responses", "live-replay-happy.json", tmp_path / "response.json")
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _bookpage_source(
            extraction_method="bookpage-live",
            enable_live=True,
            live_response_file=str(response_path),
        )

        result = COLLECTOR_REGISTRY["bookpage-live"].collect(config_path, tmp_path / "out", source, RUN_DATE)

        assert result.mentions[0]["title"] == "Registered Live Book"
        assert result.debug_metadata["live_fixture_replay"] is True

    def test_live_variant_requires_enable_live(self, tmp_path: Path):
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _bookpage_source(extraction_method="bookpage-live")

        try:
            COLLECTOR_REGISTRY["bookpage-live"].collect(config_path, tmp_path / "out", source, RUN_DATE)
            raised = None
        except CollectorRunError as exc:
            raised = exc

        assert raised is not None
        assert raised.status == "skipped"

    def test_captcha_blocked_response_raises_skipped_collector_run_error(self, tmp_path: Path):
        response_path = copy_fixture("bookpage-responses", "blocked-captcha.json", tmp_path / "response.json")
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _bookpage_source(
            extraction_method="bookpage-recorded-response",
            collector_response_file=str(response_path),
        )

        try:
            COLLECTOR_REGISTRY["bookpage-recorded-response"].collect(config_path, tmp_path / "out", source, RUN_DATE)
            raised = None
        except CollectorRunError as exc:
            raised = exc

        assert raised is not None
        assert raised.status == "skipped"
        assert "CAPTCHA" in str(raised)

    def test_missing_required_title_raises_failed_collector_run_error(self, tmp_path: Path):
        response_path = copy_fixture("bookpage-responses", "malformed-missing-title.json", tmp_path / "response.json")
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _bookpage_source(
            extraction_method="bookpage-recorded-response",
            collector_response_file=str(response_path),
        )

        try:
            COLLECTOR_REGISTRY["bookpage-recorded-response"].collect(config_path, tmp_path / "out", source, RUN_DATE)
            raised = None
        except CollectorRunError as exc:
            raised = exc

        assert raised is not None
        assert raised.status == "failed"
        assert "missing required title" in str(raised)


class SourceCandidatesRoutesBookPageThroughRegistryTests:
    """source_candidates() is the dispatcher: it normalizes extraction_method
    once and must reach the same registered collector, producing identical
    results to calling the collector directly."""

    def test_dispatch_matches_direct_collector_call(self, tmp_path: Path):
        response_path = copy_fixture("bookpage-responses", "recorded-happy.json", tmp_path / "response.json")
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _bookpage_source(
            extraction_method="bookpage-recorded-response",
            collector_response_file=str(response_path),
        )

        direct = COLLECTOR_REGISTRY["bookpage-recorded-response"].collect(
            config_path, tmp_path / "out", source, RUN_DATE
        )
        dispatched = source_candidates(config_path, tmp_path / "out", source, RUN_DATE)

        assert dispatched.raw_mention_count == direct.raw_mention_count
        assert dispatched.valid_mention_count == direct.valid_mention_count
        assert dispatched.skipped_item_count == direct.skipped_item_count
        assert dispatched.debug_metadata == direct.debug_metadata
        assert len(dispatched.mentions) == len(direct.mentions)
        assert dispatched.mentions[0]["title"] == direct.mentions[0]["title"]

    def test_dispatch_normalizes_extraction_method_case_and_whitespace(self, tmp_path: Path):
        response_path = copy_fixture("bookpage-responses", "recorded-happy.json", tmp_path / "response.json")
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _bookpage_source(
            extraction_method="  BookPage-Recorded-Response  ",
            collector_response_file=str(response_path),
        )

        dispatched = source_candidates(config_path, tmp_path / "out", source, RUN_DATE)

        assert dispatched.raw_mention_count == 1
        assert dispatched.mentions[0]["title"] == "Collector Registry Field Guide"

    def test_dispatch_selects_source_page_variant(self, tmp_path: Path):
        response_path = copy_fixture("bookpage-responses", "source-page-happy.json", tmp_path / "response.json")
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _bookpage_source(
            extraction_method="bookpage-source-page",
            source_page_fixture_file=str(response_path),
        )

        dispatched = source_candidates(config_path, tmp_path / "out", source, RUN_DATE)

        assert dispatched.mentions[0]["title"] == "Dispatcher's Almanac"

    def test_dispatch_selects_live_variant(self, tmp_path: Path):
        response_path = copy_fixture("bookpage-responses", "live-replay-happy.json", tmp_path / "response.json")
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _bookpage_source(
            extraction_method="bookpage-live",
            enable_live=True,
            live_response_file=str(response_path),
        )

        dispatched = source_candidates(config_path, tmp_path / "out", source, RUN_DATE)

        assert dispatched.mentions[0]["title"] == "Registered Live Book"

    def test_dispatch_propagates_collector_run_error_unchanged(self, tmp_path: Path):
        response_path = copy_fixture("bookpage-responses", "blocked-captcha.json", tmp_path / "response.json")
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _bookpage_source(
            extraction_method="bookpage-recorded-response",
            collector_response_file=str(response_path),
        )

        try:
            source_candidates(config_path, tmp_path / "out", source, RUN_DATE)
            raised = None
        except CollectorRunError as exc:
            raised = exc

        assert raised is not None
        assert raised.status == "skipped"

    def test_dispatch_does_not_touch_non_bookpage_methods(self, tmp_path: Path):
        """An extraction_method absent from COLLECTOR_REGISTRY (e.g. NPR's)
        must not be picked up by the BookPage dispatch branch."""
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _bookpage_source(extraction_method="npr-source-page")

        dispatched = source_candidates(config_path, tmp_path / "out", source, RUN_DATE)

        assert dispatched.mentions == []
        assert dispatched.raw_mention_count == 0
