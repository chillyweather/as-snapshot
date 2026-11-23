"""Per-collector and routing tests for the Agent Extraction Collector
registry adapter.

These exercise ``AgentExtractionCollector`` directly through the
``Collector`` protocol with fixture files (no pipeline run), and assert
that ``source_candidates()`` reaches the same registered collector -- the
two seams described in PRD #231 / issue #236. The agent-extraction backend
and transport protocols (fake/fixture/real backends, injectable transport)
are untouched and exercised elsewhere; here we only drive them through a
configured fixture-replay ("fixture" backend selected automatically from a
configured response file) and a fake live backend.
"""

from __future__ import annotations

import json
from pathlib import Path

from autoscout.source_collection import source_candidates
from autoscout.source_collectors import COLLECTOR_REGISTRY
from autoscout.source_collectors import AgentExtractionCollector
from autoscout.source_collectors import CollectorResult
from autoscout.source_collectors import CollectorRunError
from autoscout.source_collectors import agent_extraction_prompt
from autoscout.source_collectors import extraction_contract
from tests.fixtures import copy_fixture

RUN_DATE = "2026-07-07"


def _agent_source(**overrides) -> dict:
    source = {
        "name": "Agent Extracted Source",
        "family": "media/curated list",
        "url": "https://example.test/nonfiction",
        "access_status": "open",
        "check_frequency": "weekly",
        "language": "en",
    }
    source.update(overrides)
    return source


class AgentExtractionCollectorRegistryTests:
    """The registry owns all three agent-extraction method values; the
    configured variant owns both non-live method values since they behave
    identically today."""

    def test_registry_owns_all_three_agent_extraction_method_values(self):
        assert {
            "agent-extraction",
            "agent-extraction-replay",
            "agent-extraction-live",
        }.issubset(set(COLLECTOR_REGISTRY))

    def test_each_agent_extraction_registration_is_an_agent_extraction_collector(self):
        for method, collector in COLLECTOR_REGISTRY.items():
            if method.startswith("agent-extraction"):
                assert isinstance(collector, AgentExtractionCollector)

    def test_replay_and_non_replay_share_the_configured_variant(self):
        assert COLLECTOR_REGISTRY["agent-extraction"].variant == "configured"
        assert COLLECTOR_REGISTRY["agent-extraction-replay"].variant == "configured"

    def test_live_method_owns_the_live_variant(self):
        assert COLLECTOR_REGISTRY["agent-extraction-live"].variant == "live"


class AgentExtractionCollectorDirectTests:
    """Exercise the agent-extraction collector directly through
    Collector.collect() with fixture files -- no pipeline run."""

    def test_configured_replay_happy_path(self, tmp_path: Path):
        page_path = copy_fixture("source-pages", "example-nonfiction-codex.json", tmp_path / "page.json")
        extraction_path = copy_fixture(
            "extraction-responses", "example-nonfiction-codex.json", tmp_path / "extraction.json"
        )
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _agent_source(
            extraction_method="agent-extraction-replay",
            source_page_fixture_file=str(page_path),
            extraction_response_file=str(extraction_path),
        )

        result = COLLECTOR_REGISTRY["agent-extraction-replay"].collect(config_path, tmp_path / "out", source, RUN_DATE)

        assert isinstance(result, CollectorResult)
        assert result.raw_mention_count == 1
        assert result.valid_mention_count == 1
        assert result.skipped_item_count == 0
        assert result.debug_metadata["collector_kind"] == "agent-extraction"
        assert result.mentions[0]["title"] == "Discovered Book"
        assert result.mentions[0]["primary_authors"] == ["Jane Author"]

    def test_agent_extraction_method_value_uses_same_variant_as_replay(self, tmp_path: Path):
        page_path = copy_fixture("source-pages", "example-nonfiction-codex.json", tmp_path / "page.json")
        extraction_path = copy_fixture(
            "extraction-responses", "example-nonfiction-codex.json", tmp_path / "extraction.json"
        )
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _agent_source(
            extraction_method="agent-extraction",
            source_page_fixture_file=str(page_path),
            extraction_response_file=str(extraction_path),
        )

        result = COLLECTOR_REGISTRY["agent-extraction"].collect(config_path, tmp_path / "out", source, RUN_DATE)

        assert result.mentions[0]["title"] == "Discovered Book"

    def test_live_variant_replays_configured_fixture_via_fake_backend(self, tmp_path: Path):
        page_path = tmp_path / "page.json"
        page_path.write_text(
            json.dumps(
                {
                    "source_page": "https://example.test/list",
                    "html": '<a href="https://example.test/books/live">Live Book</a> by Unknown Agent Extraction Author',
                }
            ),
            encoding="utf-8",
        )
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _agent_source(
            extraction_method="agent-extraction-live",
            enable_live=True,
            extraction_backend="fake",
            source_page_fixture_file=str(page_path),
        )

        result = COLLECTOR_REGISTRY["agent-extraction-live"].collect(config_path, tmp_path / "out", source, RUN_DATE)

        assert result.mentions[0]["source_url"] == "https://example.test/books/live"
        assert result.debug_metadata["collector_kind"] == "agent-extraction"
        assert result.debug_metadata["live_fixture_replay"] is True

    def test_live_variant_requires_enable_live(self, tmp_path: Path):
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _agent_source(extraction_method="agent-extraction-live")

        try:
            COLLECTOR_REGISTRY["agent-extraction-live"].collect(config_path, tmp_path / "out", source, RUN_DATE)
            raised = None
        except CollectorRunError as exc:
            raised = exc

        assert raised is not None
        assert raised.status == "skipped"

    def test_empty_extraction_raises_failed_collector_run_error(self, tmp_path: Path):
        page_path = copy_fixture("source-pages", "example-nonfiction-codex.json", tmp_path / "page.json")
        extraction_path = tmp_path / "extraction.json"
        extraction_path.write_text(json.dumps({"mentions": []}), encoding="utf-8")
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _agent_source(
            extraction_method="agent-extraction-replay",
            source_page_fixture_file=str(page_path),
            extraction_response_file=str(extraction_path),
        )

        try:
            COLLECTOR_REGISTRY["agent-extraction-replay"].collect(config_path, tmp_path / "out", source, RUN_DATE)
            raised = None
        except CollectorRunError as exc:
            raised = exc

        assert raised is not None
        assert raised.status == "failed"
        assert "no mentions" in str(raised)


def _exact_list_prompt() -> str:
    source = _agent_source(
        family="award",
        signal_family="award",
        metadata_exact_book_list=True,
        expected_signal_strength="shortlist",
        eligible_candidate_category="adult nonfiction",
    )
    return agent_extraction_prompt(source, {"html": "<h1>Annual shortlist</h1>"}, 1000)


def test_exact_list_prompt_includes_grounding_and_category_instructions() -> None:
    prompt = _exact_list_prompt()
    for text in (
        "candidate_category",
        "adult nonfiction",
        "exact list-linked",
        "anchor text",
        "shortlist",
        "fiction",
        "narrow manual",
        "children/YA",
    ):
        assert text.casefold() in prompt.casefold()


def test_exact_list_prompt_includes_content_description() -> None:
    prompt = _exact_list_prompt()
    assert "content_description" in prompt
    assert "Write every content_description as concise original Russian prose" in prompt


def test_exact_list_prompt_omits_ranking_metadata_fields() -> None:
    prompt = _exact_list_prompt()
    for text in ("current_rank", "source_week", "ISBN", "YTD units"):
        assert text not in prompt


def test_non_english_ranking_contract_requires_non_authoritative_helpers() -> None:
    portuguese_source = _agent_source(
        contract_family="ranking-table",
        metadata_ranking_table=True,
        language="pt",
        eligible_candidate_category="adult nonfiction",
    )
    contract = extraction_contract(portuguese_source)
    shape = contract["required_output_shape"]["mentions"][0]
    rules = " ".join(contract["rules"]).casefold()

    assert set(("english_title", "search_translation", "author_transliteration_note")) <= set(
        contract["required_fields"]
    )
    assert set(("english_title", "search_translation", "author_transliteration_note")) <= set(shape)
    assert "non-authoritative" in rules
    assert "portuguese title and primary author are authoritative" in rules
    assert "do not invent canonical translations or transliterations" in rules
    assert "candidate_category" in contract["required_fields"]
    assert "candidate_category" in shape

    english_source = _agent_source(
        contract_family="ranking-table",
        metadata_ranking_table=True,
        language="en",
        eligible_candidate_category="adult nonfiction",
    )
    english_contract = extraction_contract(english_source)
    english_shape = english_contract["required_output_shape"]["mentions"][0]
    assert not set(("english_title", "search_translation", "author_transliteration_note")) & set(
        english_contract["required_fields"]
    )
    assert not set(("english_title", "search_translation", "author_transliteration_note")) & set(english_shape)


class SourceCandidatesRoutesAgentExtractionThroughRegistryTests:
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
        source = _agent_source(
            extraction_method="agent-extraction-replay",
            source_page_fixture_file=str(page_path),
            extraction_response_file=str(extraction_path),
        )

        direct = COLLECTOR_REGISTRY["agent-extraction-replay"].collect(config_path, tmp_path / "out", source, RUN_DATE)
        dispatched = source_candidates(config_path, tmp_path / "out", source, RUN_DATE)

        assert dispatched.raw_mention_count == direct.raw_mention_count
        assert dispatched.valid_mention_count == direct.valid_mention_count
        assert dispatched.skipped_item_count == direct.skipped_item_count
        assert dispatched.debug_metadata == direct.debug_metadata
        assert dispatched.mentions[0]["title"] == direct.mentions[0]["title"]

    def test_dispatch_propagates_collector_run_error_unchanged(self, tmp_path: Path):
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _agent_source(extraction_method="agent-extraction-live")

        try:
            source_candidates(config_path, tmp_path / "out", source, RUN_DATE)
            raised = None
        except CollectorRunError as exc:
            raised = exc

        assert raised is not None
        assert raised.status == "skipped"

    def test_dispatch_does_not_touch_non_agent_extraction_methods(self, tmp_path: Path):
        """An extraction_method absent from the agent-extraction
        registrations (e.g. NPR's) must not be picked up by this branch."""
        config_path = tmp_path / "sources.json"
        config_path.write_text("{}", encoding="utf-8")
        source = _agent_source(extraction_method="npr-source-page")

        dispatched = source_candidates(config_path, tmp_path / "out", source, RUN_DATE)

        assert dispatched.mentions == []
        assert dispatched.raw_mention_count == 0
