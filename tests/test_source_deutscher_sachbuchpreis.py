"""Deutscher Sachbuchpreis stable-landing current-edition tests (Issue #679).

The shared onboarding contract covers full metadata and replay fixtures for
post-runbook sources. This file pins genuinely source-specific facts for the
stable annual landing page: the year-neutral durable identity, the annual
stable-URL contract, the 2026 winner-plus-nominees evidence, and the
regression that the stale 2024 name with an empty extraction is a failure
while the retained 2026 replay page collects.
"""

from __future__ import annotations

import json
from pathlib import Path

from autoscout.source_collection import SourceCollection
from autoscout.source_collectors.base import page_links
from autoscout.source_page_identity import parse_heading_texts

ROOT = Path(__file__).resolve().parents[1]
PORTFOLIO_PATH = ROOT / "configs" / "current-weekly-portfolio.json"
PILOT_PATH = ROOT / "configs" / "current-weekly-pilot.json"
PILOT_LIVE_PATH = ROOT / "configs" / "current-weekly-pilot-live.json"
LIVE_PATH = ROOT / "configs" / "current-weekly-live.json"
FIXTURES_DIR = ROOT / "data" / "current-weekly-pilot"
PAGE_FIXTURE = FIXTURES_DIR / "deutscher-sachbuchpreis-page.json"
EXTRACTION_FIXTURE = FIXTURES_DIR / "deutscher-sachbuchpreis-extraction.json"

SOURCE_NAME = "Deutscher Sachbuchpreis"
STALE_NAME = "Deutscher Sachbuchpreis 2024"
SOURCE_URL = "https://www.deutscher-sachbuchpreis.de/"

# Official 2026 current-edition evidence from the stable landing-page capture
# (runs/current-weekly-pilot-russian/codex-capture-deutscher-sachbuchpreis-2024-6f39e69f.json).
# The homepage presents the 2026 winner plus seven nominees; the replay
# fixture retains the winner plus the first two nominees in document order.
EXPECTED_BOOKS = (
    (
        "Dreihundert Männer",
        "Konstantin Richter",
        "https://www.deutscher-sachbuchpreis.de/aktuelles-jahr/2026/dreihundert-maenner",
        "winner",
    ),
    (
        "Gespräche mit einem Toten",
        "Heike Behrend",
        "https://www.deutscher-sachbuchpreis.de/aktuelles-jahr/2026/gespraeche-mit-einem-toten",
        "shortlist",
    ),
    (
        "Szenario",
        "Florence Gaub",
        "https://www.deutscher-sachbuchpreis.de/aktuelles-jahr/2026/szenario",
        "shortlist",
    ),
)


def _portfolio_source() -> dict:
    portfolio = json.loads(PORTFOLIO_PATH.read_text(encoding="utf-8"))
    matches = [s for s in portfolio["sources"] if "Sachbuchpreis" in str(s.get("name", ""))]
    assert len(matches) == 1, f"expected one Sachbuchpreis portfolio entry, got {len(matches)}"
    return matches[0]


def _page() -> dict:
    return json.loads(PAGE_FIXTURE.read_text(encoding="utf-8"))


def _extraction() -> dict:
    return json.loads(EXTRACTION_FIXTURE.read_text(encoding="utf-8"))


def test_source_has_year_neutral_durable_identity() -> None:
    source = _portfolio_source()
    assert source["name"] == SOURCE_NAME
    assert "2024" not in source["name"]
    # The stale year-pinned identity must be gone as a source name in every config.
    for path in (PORTFOLIO_PATH, PILOT_PATH, PILOT_LIVE_PATH, LIVE_PATH):
        config = json.loads(path.read_text(encoding="utf-8"))
        names = [s.get("name", "") for s in config["sources"]]
        assert STALE_NAME not in names, f"stale name still present in {path.name}"
        assert names.count(SOURCE_NAME) == 1, f"{path.name} must contain the neutral name once"


def test_metadata_declares_annual_stable_url_and_current_edition_contract() -> None:
    source = _portfolio_source()
    assert source["family"] == "award"
    assert source["url"] == SOURCE_URL
    assert source["active"] is True
    assert source["language"] == "de"
    assert source.get("signal_family") == "award"
    assert source.get("refresh_cadence") == "annual"
    assert source.get("check_frequency") == "annual"
    url_stability = str(source.get("url_stability", ""))
    assert "stable" in url_stability.casefold()
    assert str(source.get("expected_category", "")).strip() != ""
    assert str(source.get("expected_page_heading", "")).strip() == "Die Nominierten"
    assert str(source.get("eligible_candidate_category", "")).strip() == "adult nonfiction"
    notes = str(source.get("extraction_notes", ""))
    assert "2026" in notes
    assert "stable" in notes.casefold()
    assert "winner" in notes.casefold()


def test_replay_fixture_proves_official_2026_nominees() -> None:
    page = _page()
    assert page["source_name"] == SOURCE_NAME
    assert page["source_url"] == SOURCE_URL
    assert page["source_page"] == SOURCE_URL
    assert page["resolved_url"] == SOURCE_URL
    html = page["html"]
    assert "Die Nominierten" in html
    assert "Dreihundert Männer" in html
    headings = parse_heading_texts(html)
    assert "Die Nominierten" in headings
    extraction = _extraction()
    mentions = extraction["mentions"]
    assert len(mentions) == 3
    for (title, author, url, strength), mention in zip(EXPECTED_BOOKS, mentions, strict=True):
        assert mention["title"] == title
        assert mention["primary_authors"] == [author]
        assert mention["evidence_url"] == url
        assert mention["source_url"] == SOURCE_URL
        assert mention["signal_type"] == "award"
        assert mention["signal_strength"] == strength
        assert mention["candidate_category"] == "adult nonfiction"
        assert str(mention.get("evidence_note", "")).strip() != ""
        assert any("А" <= c <= "я" or c in {"ё", "Ё"} for c in mention.get("content_description", ""))
        # Non-English helper contract: helpers present, nonblank, searchable.
        assert str(mention.get("english_title", "")).strip() != ""
        assert mention["english_title"] != title
        assert str(mention.get("search_translation", "")).strip() != ""
        assert mention["search_translation"] != title
        assert str(mention.get("author_transliteration_note", "")).strip() != ""


def test_evidence_urls_are_official_and_verifiable() -> None:
    page = _page()
    links = page_links(page["html"], page["source_page"])
    for mention in _extraction()["mentions"]:
        url = mention["evidence_url"]
        assert url.startswith("https://www.deutscher-sachbuchpreis.de/aktuelles-jahr/2026/")
        assert url in links, f"evidence URL not verifiable on the retained page: {url}"


def test_replay_collects_without_zero_entry_failure(tmp_path) -> None:
    from autoscout.config import resolve_weekly_portfolio
    from autoscout.json_io import read_json

    raw = read_json(PILOT_PATH)
    merged = resolve_weekly_portfolio(raw, PILOT_PATH)
    source = next(s for s in merged["sources"] if s.get("name") == SOURCE_NAME)
    source = dict(source)
    source["source_page_fixture_file"] = str(PAGE_FIXTURE.resolve())
    source["extraction_response_file"] = str(EXTRACTION_FIXTURE.resolve())
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-07-18").collect([source])
    run = result.source_runs[0]
    assert run["status"] == "collected"
    assert run["debug_metadata"]["safe_failure_status"] == "healthy"
    assert run["valid_mention_count"] == 3
    assert len(result.source_mentions) == 3
    assert run["reason"] == ""


def test_empty_extraction_on_complete_2026_page_fails_closed(tmp_path) -> None:
    """The zero-entry guardrail: an empty extraction on the complete 2026 page
    is the Issue #679 live failure and must stay a failure, so the retained
    non-empty 2026 extraction above is what repairs it."""
    from autoscout.config import resolve_weekly_portfolio
    from autoscout.json_io import read_json

    raw = read_json(PILOT_PATH)
    merged = resolve_weekly_portfolio(raw, PILOT_PATH)
    source = next(s for s in merged["sources"] if s.get("name") == SOURCE_NAME)
    source = dict(source)
    page_path = tmp_path / "page.json"
    page_path.write_text(PAGE_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    empty_path = tmp_path / "empty-extraction.json"
    empty_path.write_text(json.dumps({"mentions": []}, ensure_ascii=False), encoding="utf-8")
    source["source_page_fixture_file"] = str(page_path)
    source["extraction_response_file"] = str(empty_path)
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-07-18").collect([source])
    run = result.source_runs[0]
    assert run["status"] == "failed"
    assert run["valid_mention_count"] == 0
    # Either the collector's empty-extraction refusal or the complete-list
    # zero-contribution outcome: both are failed, never a silent healthy zero.
    assert run["debug_metadata"]["safe_failure_status"] in ("empty_extraction", "healthy_empty")
    assert run["reason"] != ""
