"""Onboarding #901 source-specific replay contract for NuevosLibros — Ensayo."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from autoscout.config import load_validated_config
from autoscout.nuevoslibros_frontlist import frontlist_cards
from autoscout.source_collection import SourceCollection
from autoscout.source_collectors.base import extraction_contract_violation
from autoscout.source_page_identity import validate_page_shape

ROOT = Path(__file__).resolve().parents[1]
NAME = "NuevosLibros — Ensayo"
URL = "https://nuevoslibros.com/genero/ensayo/"


def _source(config_name: str = "current-weekly-pilot.json") -> dict[str, Any]:
    return next(
        source for source in load_validated_config(ROOT / "configs" / config_name)["sources"] if source["name"] == NAME
    )


def test_source_has_four_config_parity_and_is_retired() -> None:
    for filename in (
        "current-weekly-portfolio.json",
        "current-weekly-pilot.json",
        "current-weekly-pilot-live.json",
        "current-weekly-live.json",
    ):
        source = _source(filename)
        assert source["url"] == URL
        assert source["language"] == "es"
        assert source["contract_family"] == "nuevoslibros-frontlist"
        # Retired at the editor's request on 2026-11-10 (docs/nuevoslibros-retirement-2026-11-10.md);
        # the settings stay so the source can be restored.
        assert source["active"] is False
        assert source["lifecycle_status"] == "retired"
        assert source["retired_at"] == "2026-11-10"
        assert source["expected_page_heading"] == "Novedades de ensayo"
        assert "#901" in source["lifecycle_note"]


def test_replay_retains_three_grounded_cards_and_collects_them(tmp_path: Path) -> None:
    # The source is retired; its collector is still measured on a copy that is switched on.
    source = {**_source(), "active": True}
    page = json.loads((ROOT / "configs" / source["source_page_fixture_file"]).read_text())
    extraction = json.loads((ROOT / "configs" / source["extraction_response_file"]).read_text())
    cards = frontlist_cards(page["html"], URL)
    assert len(cards) == 3
    assert validate_page_shape(source, page) == ""
    for mention in extraction["mentions"]:
        assert extraction_contract_violation(mention, mention, source, page) == ""
    result = SourceCollection(ROOT / "configs/current-weekly-pilot.json", tmp_path, "2026-11-02").collect([source])
    assert len(result.source_mentions) == 3, result.source_runs
    assert {mention["title"] for mention in result.source_mentions} == {card.title for card in cards}
    assert all(mention["english_title"] != mention["title"] for mention in result.source_mentions)
