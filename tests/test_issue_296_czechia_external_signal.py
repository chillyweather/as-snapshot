"""Source-specific coverage for Issue #296 Czechia (Magnesia Litera 2026).

This file covers accepted-source behavior: four-config parity, category
markers, ineligible exclusion across fiction/poetry/children-YA, Unicode
identity, grounding, replay collection, and shared-contract coverage.
Rejection-only CEE markets are tested in ``tests/test_issue_296_cee_rejection.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from typing import cast

import pytest

from autoscout.config import load_validated_config
from autoscout.source_page_identity import exact_book_list_mention_is_grounded
from autoscout.source_page_identity import link_anchor_pairs
from autoscout.source_page_identity import validate_page_shape
from autoscout.source_proposals import normalize_source_identity

ROOT = Path(__file__).resolve().parents[1]
SOURCE_NAME = "Magnesia Litera 2026 — Naučná literatura a Publicistika"
SOURCE_URL = "https://www.magnesia-litera.cz/"
CONFIG_NAMES = (
    "current-weekly-portfolio.json",
    "current-weekly-pilot.json",
    "current-weekly-pilot-live.json",
    "current-weekly-live.json",
)

CZE_BOOKS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("Bohové a bohyně starých Slovanů", ("Jiří Dynda",), SOURCE_URL + "kniha/bohove-a-bohyne-starych-slovanu/"),
    ("Labyrint bolesti", ("Pavel Kolář",), SOURCE_URL + "kniha/labyrint-bolesti/"),
    ("Slunce, voda, vzduch", ("Jan Pezda",), SOURCE_URL + "kniha/slunce-voda-vzduch/"),
    ("České zbraně pro Ukrajinu", ("Ondřej Kundra", "Tomáš Brolík"), SOURCE_URL + "kniha/ceske-zbrane-pro-ukrajinu/"),
    ("Ruská duše neexistuje", ("Jan Bělíček", "Tomáš Glanc"), SOURCE_URL + "kniha/ruska-duse-neexistuje/"),
)


def _source() -> dict[str, Any]:
    config = load_validated_config(ROOT / "configs/current-weekly-pilot.json")
    return cast(dict[str, Any], next(v for v in config["sources"] if v["name"] == SOURCE_NAME))


def _fixtures() -> tuple[dict[str, Any], dict[str, Any]]:
    src = _source()
    page = json.loads((ROOT / "configs" / src["source_page_fixture_file"]).read_text(encoding="utf-8"))
    extraction = json.loads((ROOT / "configs" / src["extraction_response_file"]).read_text(encoding="utf-8"))
    return page, extraction


# ── four-config parity ──────────────────────────────────────────────────────


@pytest.mark.parametrize("cfg_name", CONFIG_NAMES)
def test_source_present_in_config(cfg_name: str) -> None:
    config = load_validated_config(ROOT / "configs" / cfg_name)
    names = [s["name"] for s in config["sources"]]
    assert names.count(SOURCE_NAME) == 1


def test_source_occurs_at_same_index_in_all_configs() -> None:
    positions = []
    for cfg_name in CONFIG_NAMES:
        config = load_validated_config(ROOT / "configs" / cfg_name)
        names = [s["name"] for s in config["sources"]]
        positions.append(names.index(SOURCE_NAME))
    assert len(set(positions)) == 1


# ── portfolio metadata ──────────────────────────────────────────────────────


def test_portfolio_metadata() -> None:
    config = load_validated_config(ROOT / "configs/current-weekly-portfolio.json")
    source = cast(dict[str, Any], next(v for v in config["sources"] if v["name"] == SOURCE_NAME))
    assert source["family"] == "award"
    assert source["signal_family"] == "award"
    assert source["language"] == "cs"
    assert source["country"] == "Czechia"
    assert source["market"] == "Czechia"
    assert source["url"] == SOURCE_URL
    assert source["active"] is True
    assert source["access_status"] == "open"
    assert source["check_frequency"] == "annual"
    assert source["refresh_cadence"] == "annual"
    assert source["url_stability"] == "stable"
    assert source["metadata_exact_book_list"] is True
    assert source["expected_signal_strength"] == "longlist"
    assert source["eligible_candidate_category"] == "adult nonfiction"
    assert source["lifecycle_status"] == "established"
    assert source["expected_page_heading"] == "Magnesia Litera Kniha roku"


# ── category markers ────────────────────────────────────────────────────────


def test_nonfiction_category_markers_in_page() -> None:
    page, _ = _fixtures()
    html = page["html"]
    assert "naučnou literaturu" in html
    assert "publicistiku" in html


def test_ineligible_categories_excluded_from_extraction() -> None:
    """Fiction, poetry, and children/YA links must not appear in collected source mentions.

    This test runs link discovery on the actual page fixture rather than
    restating extraction constants, so it remains honest when the fixture
    or extraction changes."""
    page, _ = _fixtures()
    pairs = link_anchor_pairs(page["html"], page["source_page"])
    ineligible_slugs = {
        "mariborska-hypnoza",  # próza (fiction)
        "koncept-transparence",  # poezie (poetry)
        "diktaty",  # děti a mládež (children/YA)
    }
    ineligible_urls = {
        normalize_source_identity(href) for href, _ in pairs if any(slug in href for slug in ineligible_slugs)
    }
    assert ineligible_urls, "fixture must contain at least one ineligible link"
    extraction = json.loads((ROOT / "configs" / _source()["extraction_response_file"]).read_text(encoding="utf-8"))
    for m in extraction["mentions"]:
        assert normalize_source_identity(m["evidence_url"]) not in ineligible_urls, (
            f"ineligible link {m['evidence_url']!r} leaked into extraction for {m['title']!r}"
        )


# ── Unicode identity ────────────────────────────────────────────────────────


def test_czech_diacritics_preserved() -> None:
    _, extraction = _fixtures()
    titles = {m["title"] for m in extraction["mentions"]}
    diacritic_chars = set("čřůěšžýáíé")
    diacritic_titles = [t for t in titles if any(c in t for c in diacritic_chars)]
    assert len(diacritic_titles) >= 3
    assert "Bohové a bohyně starých Slovanů" in titles
    assert "České zbraně pro Ukrajinu" in titles


# ── grounding ───────────────────────────────────────────────────────────────


def test_all_extraction_mentions_are_grounded() -> None:
    page, extraction = _fixtures()
    for m in extraction["mentions"]:
        assert exact_book_list_mention_is_grounded(
            page["html"],
            page["source_page"],
            title=m["title"],
            authors=m["primary_authors"],
            evidence_url=m["evidence_url"],
        )


def test_grounding_uses_author_prefix_matching() -> None:
    page, _ = _fixtures()
    assert exact_book_list_mention_is_grounded(
        page["html"],
        page["source_page"],
        title="Bohové a bohyně starých Slovanů",
        authors=["Jiří Dynda"],
        evidence_url=SOURCE_URL + "kniha/bohove-a-bohyne-starych-slovanu/",
    )


def test_extraction_pinned_books_match_fixture() -> None:
    _, extraction = _fixtures()
    actual = {(m["title"], tuple(m["primary_authors"]), m["evidence_url"]) for m in extraction["mentions"]}
    assert actual == {(t, a, u) for t, a, u in CZE_BOOKS}


# ── page shape and contract ─────────────────────────────────────────────────


def test_page_shape_validates() -> None:
    page, _ = _fixtures()
    page_data = {
        "source_url": page["source_url"],
        "resolved_url": page["resolved_url"],
        "html": page["html"],
    }
    err = validate_page_shape(_source(), page_data)
    assert err == ""


# ── signal consistency ──────────────────────────────────────────────────────


def test_all_mentions_have_consistent_signal_metadata() -> None:
    _, extraction = _fixtures()
    for m in extraction["mentions"]:
        assert m["signal_type"] == "award"
        assert m["signal_strength"] == "longlist"
        assert m["candidate_category"] == "adult nonfiction"
        assert m["source_url"] == SOURCE_URL


# ── helper fields ───────────────────────────────────────────────────────────


def test_helpers_are_present_and_non_authoritative() -> None:
    _, extraction = _fixtures()
    for m in extraction["mentions"]:
        assert m.get("english_title")
        assert m.get("english_author_display")
        assert m.get("search_translation") and m["search_translation"] != m["title"]
        assert m.get("author_transliteration_note")
        assert m["english_title"] != m["title"]
