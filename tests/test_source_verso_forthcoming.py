"""Onboarding #936 source-specific replay contract for Verso — Forthcoming."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from autoscout.config import load_validated_config
from autoscout.publisher_frontlist import frontlist_cards
from autoscout.source_collection import SourceCollection
from autoscout.source_collectors.base import extraction_contract_violation
from autoscout.source_page_identity import validate_page_shape

ROOT = Path(__file__).resolve().parents[1]
NAME = "Verso — Forthcoming"
URL = "https://www.versobooks.com/en-gb/collections/forthcoming"


def _source(config_name: str = "current-weekly-pilot.json") -> dict[str, Any]:
    return next(
        source for source in load_validated_config(ROOT / "configs" / config_name)["sources"] if source["name"] == NAME
    )


def test_source_has_four_config_parity_and_probation() -> None:
    for filename in (
        "current-weekly-portfolio.json",
        "current-weekly-pilot.json",
        "current-weekly-pilot-live.json",
        "current-weekly-live.json",
    ):
        source = _source(filename)
        assert source["url"] == URL
        assert source["language"] == "en"
        assert source["contract_family"] == "publisher-frontlist"
        assert source["lifecycle_status"] == "probation"
        assert source["expected_page_heading"] == "Forthcoming"
        assert "#936" in source["lifecycle_note"]


def test_replay_retains_three_grounded_cards_and_collects_them(tmp_path: Path) -> None:
    source = _source()
    page = json.loads((ROOT / "configs" / source["source_page_fixture_file"]).read_text())
    extraction = json.loads((ROOT / "configs" / source["extraction_response_file"]).read_text())
    cards = frontlist_cards(page["html"], URL)
    assert len(cards) == 3
    assert validate_page_shape(source, page) == ""
    for mention in extraction["mentions"]:
        assert extraction_contract_violation(mention, mention, source, page) == ""
    result = SourceCollection(ROOT / "configs/current-weekly-pilot.json", tmp_path, "2026-11-03").collect([source])
    assert len(result.source_mentions) == 3, result.source_runs
    assert {mention["title"] for mention in result.source_mentions} == {card.title for card in cards}
    assert all(mention["english_title"] == mention["title"] for mention in result.source_mentions)
