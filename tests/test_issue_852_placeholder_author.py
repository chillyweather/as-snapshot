"""Issue #852: placeholder text is never admitted as an author."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from autoscout.author_admission import PLACEHOLDER_AUTHOR_DROP_REASON
from autoscout.author_admission import PLACEHOLDER_AUTHOR_PHRASES
from autoscout.author_admission import is_placeholder_author
from autoscout.config import load_validated_config
from autoscout.identity import normalized_identity_key
from autoscout.source_collection import SourceCollection
from autoscout.source_collectors import agent_extraction_mentions_from_response
from autoscout.source_collectors import agent_mentions

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "configs/current-weekly-pilot.json"
RUN_DIR = Path(__file__).resolve().parent / "fixtures" / "issue-852-bookpage-2026-09-20"
CAPTURE = RUN_DIR / "codex-capture-bookpage-nonfiction-6c3244f3.json"
EXTRACTION = RUN_DIR / "bookpage-nonfiction-6c3244f3-codex-extraction.json"
RUN_DATE = "2026-09-20"


@pytest.mark.parametrize(
    "value",
    [
        "Не указан в HTML",
        "не указан",
        "Автор не указан",
        "Not stated",
        "not specified",
        "Unknown",
        "Unknown author",
        "N/A",
        "n/a.",
        "Anonymous source",
        "nicht angegeben",
        "Inconnu",
        "Desconocido",
        "Not stated on the page",
    ],
)
def test_placeholder_values_match_as_whole_values(value: str) -> None:
    assert is_placeholder_author(value)


@pytest.mark.parametrize(
    "value",
    [
        "Lena Andrews",
        "Harold Unknown",
        "Unknown Pleasures Collective",
        "Anonymous",
        "Nina Unbekannt",
        "Не указан Иванов",
        "",
        "   ",
    ],
)
def test_real_names_containing_a_listed_word_are_not_placeholders(value: str) -> None:
    assert not is_placeholder_author(value)


def test_every_listed_phrase_matches_itself() -> None:
    assert all(is_placeholder_author(phrase) for phrase in PLACEHOLDER_AUTHOR_PHRASES)


def _bookpage_source() -> dict[str, Any]:
    config = load_validated_config(CONFIG_PATH)
    source = next(source for source in config["sources"] if source["name"] == "BookPage Nonfiction")
    return {**source, "source_page_fixture_file": str(CAPTURE), "extraction_response_file": str(EXTRACTION)}


def test_replaying_the_2026_09_20_bookpage_capture_drops_the_placeholder_author() -> None:
    recorded = json.loads(EXTRACTION.read_text(encoding="utf-8"))["entries"]
    assert [entry["primary_authors"] for entry in recorded if entry["title"] == "Valiant Women"] == [
        ["Не указан в HTML"]
    ]

    result = SourceCollection(CONFIG_PATH, ROOT, RUN_DATE).collect([_bookpage_source()])

    run = result.source_runs[0]
    assert run["status"] == "collected"
    keys = {normalized_identity_key(mention) for mention in result.source_mentions}
    assert "valiant women|не указан в html" not in keys
    assert not any(mention["title"] == "Valiant Women" for mention in result.source_mentions)
    # Every other row of the capture is still admitted, with its authors as recorded.
    assert len(result.source_mentions) == len(recorded) - 1
    drops = [drop for drop in run["drop_reasons"] if drop["reason"] == PLACEHOLDER_AUTHOR_DROP_REASON]
    assert [drop["title"] for drop in drops] == ["Valiant Women"]


def _page_and_entry(authors: list[str], title: str = "Valiant Women") -> tuple[dict[str, Any], dict[str, Any]]:
    page = json.loads(CAPTURE.read_text(encoding="utf-8"))
    entry = next(
        entry for entry in json.loads(EXTRACTION.read_text(encoding="utf-8"))["entries"] if entry["title"] == title
    )
    return page, {**entry, "primary_authors": authors}


def test_a_placeholder_beside_a_real_author_keeps_only_the_person() -> None:
    # The page prints Jon Ronson; it never prints Lena Andrews, so Valiant
    # Women cannot carry this case since Issue #850.
    page, entry = _page_and_entry(["Jon Ronson", "Unknown"], title="The Castle")
    mentions, dropped = agent_extraction_mentions_from_response(
        page, {"mentions": [entry]}, _bookpage_source(), RUN_DATE, collector_kind="codex-extraction"
    )
    assert dropped == []
    assert [mention["primary_authors"] for mention in mentions] == [["Jon Ronson"]]


def test_an_author_optional_source_admits_the_row_with_no_author(monkeypatch: pytest.MonkeyPatch) -> None:
    page, entry = _page_and_entry(["Не указан в HTML"])
    source = _bookpage_source()

    mentions, dropped = agent_extraction_mentions_from_response(
        page, {"mentions": [entry]}, source, RUN_DATE, collector_kind="codex-extraction"
    )
    assert mentions == []
    assert [drop["reason"] for drop in dropped] == [PLACEHOLDER_AUTHOR_DROP_REASON]

    # Only the source's own author-optional declaration admits the row, and
    # then with no author rather than the placeholder.
    monkeypatch.setattr(agent_mentions, "mention_author_exempt", lambda _source: True)
    mentions, dropped = agent_extraction_mentions_from_response(
        page, {"mentions": [entry]}, source, RUN_DATE, collector_kind="codex-extraction"
    )
    assert dropped == []
    assert [mention["primary_authors"] for mention in mentions] == [[]]
