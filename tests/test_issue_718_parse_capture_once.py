"""Issue #718: a collection pass parses each captured page once, not once per mention.

The grounding checks run once per extracted mention. Before #718 each one fed
the whole Source Capture to a fresh HTML parser, so a chart page with twenty
entries was parsed twenty times. The observation here is parsing work: a spy
on ``HTMLParser.feed`` counts how often each captured page is fed during
``SourceCollection.collect``.
"""

from __future__ import annotations

import json
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from autoscout.config import load_validated_config
from autoscout.source_collection import SourceCollection
from autoscout.source_collectors.base import contract_family
from autoscout.source_collectors.base import resolve_config_path
from autoscout.source_page_identity import ParsedCapture
from autoscout.source_page_identity import exact_book_list_mention_is_grounded

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "configs/current-weekly-pilot.json"
RUN_DATE = str(load_validated_config(CONFIG_PATH)["run_date"])
GROUNDING_FAMILIES = ("ranking-table", "audible-chart", "amazon-charts", "exact-book-list")
# A source whose stage publishes exactly one book cannot show parsing that
# grows with the mention count: the Baillie Gifford winner (#996).
SINGLE_ENTRY_SOURCES = frozenset({"Baillie Gifford Prize 2026 Winner"})


def _grounded_sources() -> list[dict[str, Any]]:
    config = load_validated_config(CONFIG_PATH)
    # A disabled source still has replay fixtures, so it is collected too.
    return [
        {**source, "active": True}
        for source in config["sources"]
        if contract_family(source) in GROUNDING_FAMILIES
        and source.get("extraction_response_file")
        and source["name"] not in SINGLE_ENTRY_SOURCES
    ]


def _entry_key(data: dict[str, Any]) -> str:
    return "mentions" if "mentions" in data else "entries"


def _feeds_while_collecting(monkeypatch: pytest.MonkeyPatch, source: dict[str, Any], output_dir: Path) -> Counter[str]:
    feeds: Counter[str] = Counter()
    original_feed = HTMLParser.feed

    def counting_feed(parser: HTMLParser, data: str) -> None:
        feeds[data] += 1
        original_feed(parser, data)

    with monkeypatch.context() as patch:
        patch.setattr(HTMLParser, "feed", counting_feed)
        SourceCollection(CONFIG_PATH, output_dir, RUN_DATE).collect([source])
    return feeds


def _with_first_entry_only(source: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    extraction_path = resolve_config_path(CONFIG_PATH, str(source["extraction_response_file"]))
    extraction = json.loads(extraction_path.read_text(encoding="utf-8"))
    extraction[_entry_key(extraction)] = extraction[_entry_key(extraction)][:1]
    single_path = tmp_path / "single-entry-extraction.json"
    single_path.write_text(json.dumps(extraction), encoding="utf-8")
    return {**source, "extraction_response_file": str(single_path)}


@pytest.mark.parametrize("source", _grounded_sources(), ids=lambda source: source["name"])
def test_page_parses_do_not_grow_with_the_mention_count(
    source: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page_path = resolve_config_path(CONFIG_PATH, str(source["source_page_fixture_file"]))
    page_html = json.loads(page_path.read_text(encoding="utf-8"))["html"]
    extraction_path = resolve_config_path(CONFIG_PATH, str(source["extraction_response_file"]))
    extraction = json.loads(extraction_path.read_text(encoding="utf-8"))
    assert len(extraction[_entry_key(extraction)]) > 1, "the fixture must extract more than one mention"

    all_entries = _feeds_while_collecting(monkeypatch, source, tmp_path / "all")
    one_entry = _feeds_while_collecting(monkeypatch, _with_first_entry_only(source, tmp_path), tmp_path / "one")

    # The captured page costs the same with every mention as with one.
    assert all_entries[page_html] == one_entry[page_html] > 0
    # No other page, such as a declared detail page, is fed more often than
    # any page is for one mention.
    assert max(all_entries.values()) <= max(one_entry.values())


def test_a_capture_of_another_page_is_refused() -> None:
    page = '<ul><li><a href="/book">A Book</a> by An Author</li></ul>'
    other = ParsedCapture("<p>Another page</p>", "https://example.test/list")

    with pytest.raises(ValueError, match="not the capture of this page"):
        exact_book_list_mention_is_grounded(
            page,
            "https://example.test/list",
            title="A Book",
            authors=["An Author"],
            evidence_url="https://example.test/book",
            parsed=other,
        )
