"""Issue #680 acceptance criterion 1: structured annual metadata, validated.

Every active annual source declares its URL stability, its refresh cadence,
and one `annual_edition` object: the edition it is configured for, the
official landing page where a new edition is announced, the release window,
and the award stage it tracks. Configuration refuses a malformed object, so
the audit never judges a source on a half-declared window. A source without
the object still loads, because the weekly run must never fail on it; the
audit reports it instead, and the portfolio test below keeps it from being
added that way.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from source_contract_battery import assert_annual_source_metadata

from autoscout.annual_edition import ANNUAL_STAGES
from autoscout.annual_edition import is_annual_source
from autoscout.annual_edition import parse_annual_edition
from autoscout.config import resolve_weekly_portfolio_or_raise
from autoscout.config import validate_config
from autoscout.source_rules import is_active_source

ROOT = Path(__file__).resolve().parents[1]
PORTFOLIO = ROOT / "configs" / "current-weekly-portfolio.json"
CONFIGS = ROOT / "configs"


def _source(**edition_overrides: Any) -> dict[str, Any]:
    edition: dict[str, Any] = {
        "edition_year": 2026,
        "landing_url": "https://example.test/prize/",
        "release_window": {"opens": "09-01", "closes": "09-30"},
        "stages": ["longlist", "shortlist", "winner"],
        "tracked_stage": "winner",
    }
    edition.update(edition_overrides)
    return {
        "name": "A Prize 2026",
        "url": "https://example.test/prize/2026",
        "language": "en",
        "check_frequency": "annual",
        "refresh_cadence": "annual",
        "url_stability": "year-specific",
        "annual_edition": edition,
    }


def _errors(source: dict[str, Any]) -> list[str]:
    return validate_config({"sources": [source]})


def test_a_complete_declaration_validates_and_parses() -> None:
    source = _source(
        edition_marker="{year} A Prize",
        next_edition_url="https://example.test/prize/{year}",
        stage_markers={"shortlist": "The shortlist", "winner": "The winner"},
    )
    source["annual_edition"]["release_window"]["calendar_year_offset"] = -1

    assert _errors(source) == []
    parsed = parse_annual_edition(source)
    assert parsed is not None
    assert parsed.release_window.opens == (9, 1)
    assert parsed.release_window.calendar_year_offset == -1
    assert parsed.stage_markers == (("shortlist", "The shortlist"), ("winner", "The winner"))


def test_an_absent_declaration_is_not_a_config_error() -> None:
    """The weekly run must never fail on missing annual metadata."""
    source = _source()
    del source["annual_edition"]

    assert _errors(source) == []
    assert parse_annual_edition(source) is None


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda e: e.pop("landing_url"), "missing: landing_url"),
        (lambda e: e.update(edition_year="2026"), "four-digit year"),
        (lambda e: e.update(edition_year=True), "four-digit year"),
        (lambda e: e.update(landing_url="http://example.test/"), "https URL"),
        (lambda e: e.update(release_window={"opens": "9-1", "closes": "09-30"}), "MM-DD"),
        (lambda e: e.update(release_window={"opens": "02-30", "closes": "03-01"}), "calendar day"),
        (lambda e: e.update(release_window={"opens": "02-29", "closes": "03-01"}), "calendar day"),
        (lambda e: e.update(release_window={"opens": "10-01", "closes": "09-30"}), "must not be after"),
        (
            lambda e: e.update(release_window={"opens": "09-01", "closes": "09-30", "calendar_year_offset": 2}),
            "from -1 to 1",
        ),
        (lambda e: e.update(release_window={"opens": "09-01", "closes": "09-30", "year": 1}), "unknown key"),
        (lambda e: e.update(stages=[]), "non-empty list"),
        (lambda e: e.update(stages=["longlist", "grand prize"]), "may contain only"),
        (lambda e: e.update(stages=["winner", "winner"]), "must not repeat"),
        (lambda e: e.update(tracked_stage="finalists"), "tracked_stage"),
        (lambda e: e.update(edition_marker="A Prize"), "{year}"),
        (lambda e: e.update(edition_marker="{year} {stage}"), "no placeholder other than"),
        (lambda e: e.update(next_edition_url="https://example.test/prize/latest"), "{year}"),
        (lambda e: e.update(next_edition_url="ftp://example.test/{year}"), "https URL"),
        (lambda e: e.update(stage_markers={"finalists": "Finalists"}), "not one of the declared stages"),
        (lambda e: e.update(stage_markers={"winner": " "}), "nonblank"),
        (lambda e: e.update(edition="2026"), "unknown key"),
    ],
)
def test_a_malformed_declaration_is_a_config_error(change: Any, expected: str) -> None:
    source = _source()
    change(source["annual_edition"])

    errors = _errors(source)

    assert len(errors) == 1
    assert expected in errors[0]


def test_the_declared_edition_must_agree_with_award_year() -> None:
    assert _errors({**_source(), "award_year": 2026}) == []
    errors = _errors({**_source(), "award_year": 2025})
    assert len(errors) == 1 and "disagrees with award_year 2025" in errors[0]


def test_a_declaration_on_a_non_annual_source_is_refused() -> None:
    source = {**_source(), "check_frequency": "weekly", "refresh_cadence": "weekly"}

    assert any("requires an annual" in error for error in _errors(source))


def test_the_declaration_survives_portfolio_resolution_and_overlays(tmp_path: Path) -> None:
    """A run config overlays the portfolio by name; an overlay that does not
    mention the declaration must leave it intact."""
    portfolio = {"sources": [_source()]}
    (tmp_path / "portfolio.json").write_text(json.dumps(portfolio), encoding="utf-8")
    raw = {"weekly_portfolio": "portfolio.json", "sources": [{"name": "A Prize 2026", "enable_live": True}]}

    resolved = resolve_weekly_portfolio_or_raise(raw, tmp_path / "config.json")

    assert resolved["sources"][0]["annual_edition"] == portfolio["sources"][0]["annual_edition"]
    assert validate_config(resolved) == []


def test_the_stage_vocabulary_is_fixed() -> None:
    assert ANNUAL_STAGES == ("longlist", "semifinalists", "shortlist", "finalists", "nominees", "winner", "list")


# ── The real portfolio ──────────────────────────────────────────────────────


def _portfolio_sources() -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = json.loads(PORTFOLIO.read_text(encoding="utf-8"))["sources"]
    return sources


def test_every_active_annual_source_declares_valid_annual_metadata() -> None:
    """#680 criterion 1: a future annual source cannot be added without it."""
    annual = [s for s in _portfolio_sources() if is_active_source(dict(s)) and is_annual_source(s)]

    assert len(annual) == 15
    for source in annual:
        assert_annual_source_metadata(source)


def test_every_run_config_resolves_the_portfolio_metadata_unchanged() -> None:
    """The four run configs overlay the portfolio; none may drop or rewrite
    a source's annual declaration."""
    by_name = {s["name"]: s.get("annual_edition") for s in _portfolio_sources()}
    for name in ("current-weekly-pilot.json", "current-weekly-pilot-live.json", "current-weekly-live.json"):
        path = CONFIGS / name
        raw = json.loads(path.read_text(encoding="utf-8"))
        resolved = resolve_weekly_portfolio_or_raise(copy.deepcopy(raw), path)
        for source in resolved["sources"]:
            if source.get("name") in by_name:
                assert source.get("annual_edition") == by_name[source["name"]], (name, source["name"])
