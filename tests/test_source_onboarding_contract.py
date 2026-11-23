"""Mechanical enforcement of the source-onboarding runbook (issue #309).

The runbook (docs/source-onboarding-runbook.md) is instruction-level; this
test is the enforcement layer. Any source added to the weekly portfolio
after the runbook landed must carry the full onboarding metadata, enter the
lifecycle explicitly, and ship with existing replay fixtures. Sources that
predate the runbook are grandfathered in ``LEGACY_PORTFOLIO_SOURCES`` and
are exempt from the metadata contract (but not from fixture existence).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from source_contract_battery import REQUIRED_ONBOARDING_FIELDS
from source_contract_battery import assert_replay_overlay_contract
from source_contract_battery import assert_source_onboarding_metadata
from source_contract_battery import replay_input_fixture_paths

ROOT = Path(__file__).resolve().parents[1]
PORTFOLIO_PATH = ROOT / "configs/current-weekly-portfolio.json"
REPLAY_CONFIG_PATH = ROOT / "configs/current-weekly-pilot.json"

# Sources onboarded before the runbook existed. Grandfathered: they are not
# required to carry the full onboarding metadata contract. Do NOT add new
# names here — a new source belongs in the portfolio with full metadata per
# docs/source-onboarding-runbook.md. Remove a name once its metadata is
# backfilled.
LEGACY_PORTFOLIO_SOURCES = frozenset(
    {
        "Current Weekly Nonfiction Agent Extraction",
        "National Book Awards 2026 Nonfiction",
        "Goodreads Choice Awards 2025 Nonfiction",
        "Audiobooks.com Nonfiction Audiobook Bestsellers",
        "Publishers Weekly Hardcover Frontlist Nonfiction",
        "Publishers Marketplace Rights Offerings",
        "USA TODAY Bestselling Books Nonfiction",
        "Spiegel Bestseller Sachbuch",
        "Die Zeit Sachbuch-Bestenliste",
        "PEOPLE Books",
        "BookPage Nonfiction",
        "National Book Critics Circle Awards",
        "BookMarks (LitHub) – New Nonfiction",
        "PW Best Books 2025",
        "IndieBound Indie Next List",
    }
)


def _portfolio_sources() -> list[dict[str, Any]]:
    portfolio = json.loads(PORTFOLIO_PATH.read_text(encoding="utf-8"))
    sources: list[dict[str, Any]] = portfolio["sources"]
    return sources


def _replay_overlays_by_name() -> dict[str, dict[str, Any]]:
    replay = json.loads(REPLAY_CONFIG_PATH.read_text(encoding="utf-8"))
    return {str(source.get("name", "")): source for source in replay["sources"]}


def test_legacy_list_only_names_real_pre_runbook_sources() -> None:
    """The grandfather list must track the portfolio: no stale names, and no
    name that already satisfies the full contract (backfilled sources must be
    removed from the list so the contract starts applying to them)."""
    sources_by_name = {source["name"]: source for source in _portfolio_sources()}
    stale = LEGACY_PORTFOLIO_SOURCES - set(sources_by_name)
    assert not stale, f"legacy names no longer in the portfolio (remove them): {sorted(stale)}"
    backfilled = [
        name
        for name in LEGACY_PORTFOLIO_SOURCES
        if all(str(sources_by_name[name].get(field, "")).strip() for field in REQUIRED_ONBOARDING_FIELDS)
    ]
    assert not backfilled, (
        f"legacy sources now carry the full onboarding metadata; remove them from LEGACY_PORTFOLIO_SOURCES: "
        f"{sorted(backfilled)}"
    )


def test_new_portfolio_sources_carry_full_onboarding_metadata() -> None:
    for source in _portfolio_sources():
        name = source["name"]
        if name in LEGACY_PORTFOLIO_SOURCES:
            continue
        assert_source_onboarding_metadata(source)


def test_new_portfolio_sources_have_replay_overlay_with_input_fixtures() -> None:
    """Runbook section 4: replay fixtures first — every post-runbook source
    must be collectable from committed fixtures in the replay config."""
    overlays = _replay_overlays_by_name()
    for source in _portfolio_sources():
        name = source["name"]
        if name in LEGACY_PORTFOLIO_SOURCES:
            continue
        assert name in overlays, (
            f"portfolio source {name!r} has no overlay in {REPLAY_CONFIG_PATH.name}; "
            "new sources must be onboarded replay-first (docs/source-onboarding-runbook.md section 4)"
        )
        assert_replay_overlay_contract(source, overlays[name], base_dir=REPLAY_CONFIG_PATH.parent)


def test_replay_input_fixture_files_exist_for_all_sources() -> None:
    """Every input fixture referenced by the replay config must be committed —
    legacy sources included."""
    for name, overlay in _replay_overlays_by_name().items():
        for field, path in replay_input_fixture_paths(overlay, REPLAY_CONFIG_PATH.parent):
            assert path.exists(), f"replay source {name!r}: {field} points at missing file {path}"
