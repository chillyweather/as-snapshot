"""Issue #302 — Verify the aggregate multilingual Weekly Portfolio.

One portfolio-level deterministic capture/replay integration scenario
through ``run_pipeline`` and ``render_review``.  Runs the full resolved
canonical ``current-weekly-pilot.json`` portfolio plus representative
edge fixtures in one aggregate weekly cycle, then a second cycle for
cumulative history persistence.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from state_store_support import sync_state

from autoscout.json_io import read_json
from autoscout.pipeline import run_pipeline
from autoscout.review_rendering import render_review
from autoscout.run_health import recurring_readiness
from autoscout.state_store import export_json

ROOT = Path(__file__).resolve().parents[1]
PILOT_CONFIG = ROOT / "configs" / "current-weekly-pilot.json"
PORTFOLIO_CONFIG = ROOT / "configs" / "current-weekly-portfolio.json"

# ── Per #302: canonical market-aware sources outside the US ─────────────────
# A market-expansion source is an active non-US source, on probation or
# established: approving a source after probation keeps it in the set.
# This set is the regression floor, not the complete expected result. Every
# name here must stay an active market-expansion source that collects. A
# source that a later #333 onboarding operation adds joins the derived set
# and is held to exactly the same collection property, so onboarding does not
# need a manual edit here (#404).
MARKET_EXPANSION_LIFECYCLE_STATUSES = {"probation", "established"}
BASELINE_MARKET_EXPANSION_SOURCES: set[str] = {
    "Académie française Prix de l\u2019essai",
    "Nonfiction.fr Critiques",
    "Premio Strega Saggistica 2026 Cinquina",
    "Baillie Gifford Prize 2026 Winner",
    "TodosTusLibros Librerías Recomiendan No Ficción",
    "Magnesia Litera 2026 \u2014 Nau\u010dná literatura a Publicistika",
    "Cundill History Prize 2026 Longlist",
    "Amazon.de Charts Meistgelesene Sachbücher",
}


# ── Helpers ──────────────────────────────────────────────────────────────────


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _resolve_canonical_sources() -> list[dict[str, Any]]:
    """Merge portfolio identity with pilot extraction routing for every source,
    resolving all fixture paths to absolute."""
    portfolio: dict[str, Any] = json.loads(PORTFOLIO_CONFIG.read_text(encoding="utf-8"))
    pilot: dict[str, Any] = json.loads(PILOT_CONFIG.read_text(encoding="utf-8"))

    port_by_name: dict[str, dict[str, Any]] = {s["name"]: dict(s) for s in portfolio["sources"]}
    pilot_by_name: dict[str, dict[str, Any]] = {s["name"]: dict(s) for s in pilot["sources"]}

    resolved: list[dict[str, Any]] = []
    for name in port_by_name:
        merged = {**port_by_name[name], **pilot_by_name.get(name, {})}
        for field in (
            "source_page_fixture_file",
            "extraction_response_file",
            "rss_fixture_file",
            "review_pages_fixture_file",
            "seen_guids_fixture_file",
            "helper_metadata_file",
        ):
            raw = merged.pop(field, "")
            if raw:
                merged[field] = str((ROOT / "configs" / raw).resolve())
        resolved.append(merged)
    return resolved


# Every supported way of naming the US market, after deterministic
# normalization. A source must not leave the US cohort through case,
# whitespace, punctuation, or an equivalent US label (#404).
US_MARKET_KEYS = frozenset({"united states", "united states of america", "us", "usa"})


def _market_key(value: Any) -> str:
    """Normalize one market label deterministically.

    Whitespace runs collapse, case folds, and dots drop, so ``United States``,
    ``united  states``, ``U.S.``, and ``USA`` all reduce to a comparable key.
    A missing, blank, or non-string market returns the empty key and never
    counts as a market label."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).casefold().replace(".", "")


def _market_expansion_source_names(sources: list[dict[str, Any]]) -> set[str]:
    """Return source names that are active, probation or established, non-US market sources."""
    return {
        s["name"]
        for s in sources
        if s.get("active", True) is not False
        and s.get("lifecycle_status") in MARKET_EXPANSION_LIFECYCLE_STATUSES
        and _market_key(s.get("market"))
        and _market_key(s.get("market")) not in US_MARKET_KEYS
    }


def _codex_replay_source(
    fixture_dir: Path,
    name: str,
    family: str,
    url: str,
    capture: dict[str, Any] | str,
    extraction: dict[str, Any] | str,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a codex-extraction-replay source, writing capture/extraction
    into *fixture_dir* as absolute-path JSON files."""
    capture_path = fixture_dir / f"{name}-capture.json"
    extraction_path = fixture_dir / f"{name}-extraction.json"
    if isinstance(capture, str):
        capture_path.write_text(capture, encoding="utf-8")
    else:
        _write_json(capture_path, capture)
    if isinstance(extraction, str):
        extraction_path.write_text(extraction, encoding="utf-8")
    else:
        _write_json(extraction_path, extraction)
    result: dict[str, Any] = {
        "name": name,
        "family": family,
        "url": url,
        "active": True,
        "access_status": "open",
        "check_frequency": "annual",
        "language": "en",
        "extraction_method": "codex-extraction-replay",
        "source_page_fixture_file": str(capture_path),
        "extraction_response_file": str(extraction_path),
    }
    if extra:
        result.update(extra)
    return result


_FORBIDDEN_VALUE = "This is a publisher's blurb that should never appear in a serialized mention."


def _assert_forbidden_value_absent(base_dir: Path, label: str) -> None:
    """Assert *_FORBIDDEN_VALUE* is not in any JSON or CSV file under *base_dir*."""
    for path in sorted(base_dir.rglob("*")):
        if path.is_dir():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        assert _FORBIDDEN_VALUE not in text, f"{label}/{path.name}: leaked forbidden copied prose"


# ── Nordic / CEE Unicode fixtures ────────────────────────────────────────────

_NORDIC_TITLE = "Alternativa fakta: Om kunskapen och dess fiender"
_NORDIC_AUTHOR = "\u00c5sa Wikforss"
_POLISH_TITLE = "S\u0142awomir Mro\u017cek. Biografia"
_POLISH_AUTHOR = "Ma\u0142gorzata I. Niemczy\u0144ska"

NORDIC_CEE_CAPTURE: dict[str, Any] = {
    "source_name": "nordic-cee-fixture",
    "source_url": "https://example.test/nordic",
    "resolved_url": "https://example.test/nordic",
    "html": f'<a href="https://example.test/nordic/1">Bok 1</a> av {_NORDIC_AUTHOR}'
    f' <a href="https://example.test/nordic/2">Bok 2</a> {_POLISH_AUTHOR}',
}

NORDIC_CEE_EXTRACTION: dict[str, Any] = {
    "mentions": [
        {
            "title": _NORDIC_TITLE,
            "primary_authors": [_NORDIC_AUTHOR],
            "source_url": "https://example.test/nordic",
            "source_check_date": "2026-07-17",
            "signal_type": "media/curated list",
            "signal_strength": "minor mention",
            "evidence_note": "Swedish adult-nonfiction work with Nordic Unicode title and author.",
            "evidence_url": "https://example.test/nordic/1",
            "candidate_category": "adult nonfiction",
            "english_title": "Alternative Facts: On Knowledge and Its Enemies",
            "search_translation": "Альтернативные факты: О знании и его врагах",
            "author_transliteration_note": "Оса Викфорсс",
        },
        {
            "title": _POLISH_TITLE,
            "primary_authors": [_POLISH_AUTHOR],
            "source_url": "https://example.test/nordic",
            "source_check_date": "2026-07-17",
            "signal_type": "media/curated list",
            "signal_strength": "minor mention",
            "evidence_note": "Polish-language biography with Central/Eastern European Unicode.",
            "evidence_url": "https://example.test/nordic/2",
            "candidate_category": "adult nonfiction",
            "english_title": "S\u0142awomir Mro\u017cek: A Biography",
            "search_translation": "Славомир Мрожек. Биография",
            "author_transliteration_note": "Малгожата И. Немчиньская",
        },
    ],
}

# ── Variant-titles fixture (short vs colon-subtitle) ─────────────────────────

_SHORT_TITLE = "The Vanishing Glacier"
_LONG_TITLE = "The Vanishing Glacier: Climate Change and the Race to Save the Alps"

VARIANT_CAPTURE_A: dict[str, Any] = {
    "source_name": "variant-source-a",
    "source_url": "https://example.test/variant-a",
    "resolved_url": "https://example.test/variant-a",
    "html": '<a href="https://example.test/variant-a/1">Book</a> by Elena Moretti',
}
VARIANT_CAPTURE_B: dict[str, Any] = {
    "source_name": "variant-source-b",
    "source_url": "https://example.test/variant-b",
    "resolved_url": "https://example.test/variant-b",
    "html": '<a href="https://example.test/variant-b/1">Book</a> by Elena Moretti',
}
VARIANT_CAPTURE_C: dict[str, Any] = {
    "source_name": "variant-source-c",
    "source_url": "https://example.test/variant-c",
    "resolved_url": "https://example.test/variant-c",
    "html": '<a href="https://example.test/variant-c/1">Book</a> by Elena Moretti',
}

VARIANT_EXTRACTION_A: dict[str, Any] = {
    "mentions": [
        {
            "title": _SHORT_TITLE,
            "primary_authors": ["Elena Moretti"],
            "source_url": "https://example.test/variant-a",
            "source_check_date": "2026-07-17",
            "signal_type": "media/curated list",
            "signal_strength": "major media list",
            "evidence_note": "Curated-list short-title mention.",
            "evidence_url": "https://example.test/variant-a/1",
            "candidate_category": "adult nonfiction",
        }
    ]
}
VARIANT_EXTRACTION_B: dict[str, Any] = {
    "mentions": [
        {
            "title": _LONG_TITLE,
            "primary_authors": ["Elena Moretti"],
            "source_url": "https://example.test/variant-b",
            "source_check_date": "2026-07-17",
            "signal_type": "media/curated list",
            "signal_strength": "minor mention",
            "evidence_note": "Second curated-list mention with colon-subtitle.",
            "evidence_url": "https://example.test/variant-b/1",
            "candidate_category": "adult nonfiction",
        }
    ]
}
VARIANT_EXTRACTION_C: dict[str, Any] = {
    "mentions": [
        {
            "title": _LONG_TITLE,
            "primary_authors": ["Elena Moretti"],
            "source_url": "https://example.test/variant-c",
            "source_check_date": "2026-07-17",
            "signal_type": "award",
            "signal_strength": "shortlist",
            "evidence_note": "Award-shortlist mention — different family.",
            "evidence_url": "https://example.test/variant-c/1",
            "candidate_category": "adult nonfiction",
        }
    ]
}

# ── Mixed-list / ineligible / unverifiable-provenance fixture ────────────────

FILTERING_CAPTURE: dict[str, Any] = {
    "source_name": "filtering-fixture",
    "source_url": "https://example.test/filtering",
    "resolved_url": "https://example.test/filtering",
    "html": (
        '<a href="https://example.test/filtering/good">Good</a> '
        '<a href="https://example.test/filtering/fiction">Fiction</a> '
        "<p>by Real Author, Fiction Writer</p>"
    ),
}

FILTERING_EXTRACTION: dict[str, Any] = {
    "mentions": [
        {
            "title": "A Good Nonfiction Book",
            "primary_authors": ["Real Author"],
            "source_url": "https://example.test/filtering",
            "source_check_date": "2026-07-17",
            "signal_type": "media/curated list",
            "signal_strength": "minor mention",
            "evidence_note": "Healthy row.",
            "evidence_url": "https://example.test/filtering/good",
            "candidate_category": "adult nonfiction",
        },
        {
            "title": "A Fiction Novel",
            "primary_authors": ["Fiction Writer"],
            "source_url": "https://example.test/filtering",
            "source_check_date": "2026-07-17",
            "signal_type": "media/curated list",
            "signal_strength": "minor mention",
            "evidence_note": "This is fiction — should be excluded.",
            "evidence_url": "https://example.test/filtering/fiction",
            "candidate_category": "fiction",
        },
        {
            "title": "Unverifiable Provenance Book",
            "primary_authors": ["Bad Source"],
            "source_url": "https://example.test/filtering",
            "source_check_date": "2026-07-17",
            "signal_type": "media/curated list",
            "signal_strength": "minor mention",
            "evidence_note": "Unverifiable evidence URL.",
            "evidence_url": "https://evil.test/phishing",
            "candidate_category": "adult nonfiction",
        },
    ],
}

# ── Forbidden copied-text fixture ────────────────────────────────────────────

FORBIDDEN_CAPTURE: dict[str, Any] = {
    "source_name": "forbidden-text-fixture",
    "source_url": "https://example.test/forbidden",
    "resolved_url": "https://example.test/forbidden",
    "html": '<a href="https://example.test/forbidden/book">Book</a>',
}

FORBIDDEN_EXTRACTION: dict[str, Any] = {
    "mentions": [
        {
            "title": "Book With Description",
            "primary_authors": ["Author Name"],
            "source_url": "https://example.test/forbidden",
            "source_check_date": "2026-07-17",
            "signal_type": "award",
            "signal_strength": "shortlist",
            "evidence_note": "Contains forbidden copied text.",
            "evidence_url": "https://example.test/forbidden/book",
            "candidate_category": "adult nonfiction",
            "description": _FORBIDDEN_VALUE,
        }
    ]
}

# ── Health-edge fixtures ─────────────────────────────────────────────────────

HEALTH_CASES: list[tuple[str, dict[str, Any], Any, str, str]] = [
    (
        "healthy-empty",
        {
            "source_name": "healthy-empty",
            "source_url": "https://example.test/healthy-empty",
            "resolved_url": "https://example.test/healthy-empty",
            "html": "",
        },
        None,
        "collected",
        "healthy_empty",
    ),
    (
        "wrong-target",
        {
            "source_name": "wrong-target",
            "source_url": "https://example.test/wrong",
            "resolved_url": "https://other-domain.test/",
        },
        {"mentions": []},
        "failed",
        "wrong_target",
    ),
    (
        "empty-extraction",
        {},
        {},
        "failed",
        "empty_extraction",
    ),
    (
        "source-blocked",
        {"source_name": "source-blocked", "source_url": "https://example.test/blocked"},
        {"status": "blocked", "mentions": []},
        "skipped",
        "source_blocked",
    ),
    (
        "malformed-json",
        {"source_name": "malformed-json", "source_url": "https://example.test/malformed"},
        "{",
        "failed",
        "malformed_json",
    ),
]


def _build_health_source(fixture_dir: Path, case_name: str, capture: dict[str, Any], extraction: Any) -> dict[str, Any]:
    url = f"https://example.test/{case_name}"
    if not capture.get("source_url"):
        capture = {**capture, "source_url": url, "resolved_url": url}
    return _codex_replay_source(fixture_dir, case_name, "award", url, capture, extraction)


def _fill_healthy_empty_extraction(source: dict[str, Any]) -> None:
    # An award page that stands empty between announcements declares it. An
    # undeclared zero is a failure now, because a source that publishes a
    # complete list and publishes none is broken (#464).
    source["may_report_no_entries"] = True
    source["may_report_no_entries_reason"] = "an award page lists no entry between announcements"
    capture_path = Path(source["source_page_fixture_file"])
    capture_bytes = capture_path.read_bytes()
    extraction_path = Path(source["extraction_response_file"])
    _write_json(
        extraction_path,
        {
            "source_name": source.get("source_name", "healthy-empty"),
            "source_url": source.get("source_url", "https://example.test/healthy-empty"),
            "operation_id": "op-1",
            "capture_sha256": hashlib.sha256(capture_bytes).hexdigest(),
            "entries": [],
        },
    )


# ── Build helpers ────────────────────────────────────────────────────────────


def _build_edge_sources(fixture_dir: Path) -> list[dict[str, Any]]:
    """Return synthetic edge-fixture sources written into *fixture_dir*."""
    sources: list[dict[str, Any]] = []

    sources.append(
        _codex_replay_source(
            fixture_dir,
            "nordic-cee-fixture",
            "media/curated list",
            "https://example.test/nordic",
            NORDIC_CEE_CAPTURE,
            NORDIC_CEE_EXTRACTION,
            # Swedish-led mixed Nordic/CEE fixture: its entries already carry
            # valid helpers, so the non-English declaration preserves behavior.
            extra={"language": "sv"},
        )
    )
    sources.append(
        _codex_replay_source(
            fixture_dir,
            "variant-source-a",
            "media/curated list",
            "https://example.test/variant-a",
            VARIANT_CAPTURE_A,
            VARIANT_EXTRACTION_A,
        )
    )
    sources.append(
        _codex_replay_source(
            fixture_dir,
            "variant-source-b",
            "media/curated list",
            "https://example.test/variant-b",
            VARIANT_CAPTURE_B,
            VARIANT_EXTRACTION_B,
        )
    )
    sources.append(
        _codex_replay_source(
            fixture_dir,
            "variant-source-c",
            "award",
            "https://example.test/variant-c",
            VARIANT_CAPTURE_C,
            VARIANT_EXTRACTION_C,
        )
    )
    sources.append(
        _codex_replay_source(
            fixture_dir,
            "filtering-fixture",
            "media/curated list",
            "https://example.test/filtering",
            FILTERING_CAPTURE,
            FILTERING_EXTRACTION,
        )
    )
    sources.append(
        _codex_replay_source(
            fixture_dir,
            "forbidden-text-fixture",
            "award",
            "https://example.test/forbidden",
            FORBIDDEN_CAPTURE,
            FORBIDDEN_EXTRACTION,
        )
    )
    for case_name, capture, extraction, _status, _safe in HEALTH_CASES:
        sources.append(_build_health_source(fixture_dir, case_name, dict(capture), extraction or {}))

    # Fill healthy-empty extraction after all captures written.
    for s in sources:
        if s["name"] == "healthy-empty":
            _fill_healthy_empty_extraction(s)
            break

    return sources


# ── Test: single aggregate scenario ──────────────────────────────────────────


def test_aggregate_multilingual_portfolio(tmp_path: Path) -> None:
    """One end-to-end scenario covering all #302 acceptance criteria."""
    # ── Build ──
    r1_work = tmp_path / "run1"
    r1_work.mkdir()
    canonical = _resolve_canonical_sources()
    edge_sources = _build_edge_sources(r1_work)
    all_sources = canonical + edge_sources

    r1_config = r1_work / "config.json"
    r1_output = r1_work / "output"
    r1_state = r1_work / "state"

    _write_json(
        r1_config,
        {
            "run_date": "2026-07-17",
            "sources": all_sources,
            "russian_edition_checks": [
                {"retailer": "Ozon", "url": "https://www.ozon.ru", "fixture_results": []},
                {"retailer": "LitRes", "url": "https://www.litres.ru", "fixture_results": []},
                {"retailer": "Book24", "url": "https://book24.ru", "fixture_results": []},
            ],
        },
    )

    # === Run 1: aggregate pipeline ===
    run_pipeline(r1_config, r1_output, state_dir=r1_state)

    source_runs: list[dict[str, Any]] = read_json(r1_output / "source-runs.json")
    candidates: list[dict[str, Any]] = read_json(r1_output / "scouting-candidates.json")
    mentions: list[dict[str, Any]] = read_json(r1_output / "source-mentions.json")
    metrics: dict[str, Any] = read_json(r1_output / "source-metrics.json")
    run_summary: dict[str, Any] = read_json(r1_output / "run-summary.json")
    internal_exclusions: list[dict[str, Any]] = read_json(r1_output / "internal-exclusions.json")
    russian_checks: dict[str, Any] = read_json(r1_output / "russian-edition-checks.json")

    # ── Criterion 1: every market-expansion source collects ──
    # The property is derived, so a legitimately onboarded market source
    # passes without a manual edit. The baseline set stays a floor, so a
    # known market-expansion source cannot silently disappear (#404).
    actual_market_expansion = _market_expansion_source_names(all_sources)
    missing_baseline = BASELINE_MARKET_EXPANSION_SOURCES - actual_market_expansion
    assert not missing_baseline, (
        f"Market expansion sources regressed: {sorted(missing_baseline)} are no longer active "
        f"sources outside the US; got {sorted(actual_market_expansion)}"
    )
    assert actual_market_expansion, "no market expansion source is configured"
    for name in sorted(actual_market_expansion):
        run = next((r for r in source_runs if r["source_name"] == name), None)
        assert run is not None, f"{name}: missing source run"
        assert run["status"] == "collected", f"{name}: unexpected status {run['status']}"

    # ── Criterion 2: Unicode titles/authors survive; helpers non-authoritative ──

    # Nordic: Swedish adult-nonfiction
    nordic = next((c for c in candidates if c["title"] == _NORDIC_TITLE), None)
    assert nordic is not None, "Nordic (Swedish) candidate missing"
    assert nordic["primary_authors"] == [_NORDIC_AUTHOR]
    assert nordic["english_title"] == "Alternative Facts: On Knowledge and Its Enemies"
    assert nordic["english_title"] != nordic["title"]

    # CEE: Polish biography
    polish = next((c for c in candidates if c["title"] == _POLISH_TITLE), None)
    assert polish is not None, "CEE (Polish) candidate missing"
    assert polish["primary_authors"] == [_POLISH_AUTHOR]
    assert "S\u0142awomir" in polish["title"]
    assert "Ma\u0142gorzata" in str(polish["primary_authors"])
    assert polish["english_title"] != polish["title"]

    # Accented Latin from canonical French/Italian/Spanish.
    fr = next((c for c in candidates if "Atelier de la gr" in c["title"]), None)
    assert fr is not None, "French candidate missing"
    assert "Anne-Marie Lecoq" in str(fr.get("primary_authors", []))
    assert fr["english_title"] != fr["title"]

    it = next((c for c in candidates if "Alfabit" in c.get("title", "")), None)
    assert it is not None, "Italian candidate missing"
    assert it["english_title"] != it["title"]

    es = next((c for c in candidates if "sensibilidades" in c.get("title", "").lower()), None)
    assert es is not None, "Spanish candidate missing"
    assert es["english_title"] != es["title"]

    for c in candidates:
        langs = {e.get("language", "") for e in c.get("source_evidence", [])}
        if "en" not in langs or len(langs) > 1:
            english = c.get("english_title", "")
            if english:
                # The English helper is non-authoritative. English-original
                # proper-noun titles in non-English charts (e.g. "Voldemort"
                # in the Audible.de chart) legitimately keep the same form.
                assert english.strip()

    # ── Criterion 3: Variant titles reconcile to one candidate ──
    variant = next((c for c in candidates if c.get("title") in {_SHORT_TITLE, _LONG_TITLE}), None)
    assert variant is not None, "Variant-titles candidate missing"
    evidence_urls = {e["evidence_url"] for e in variant.get("source_evidence", [])}
    assert "https://example.test/variant-a/1" in evidence_urls
    assert "https://example.test/variant-b/1" in evidence_urls
    assert "https://example.test/variant-c/1" in evidence_urls

    # ── Criterion 4: Same-family dedup + cross-family corroboration ──
    # Sources A and B share "media/curated list"; C is "award".
    unique_families = variant.get("independent_signal_families", [])
    assert variant["independent_signal_count"] == 2, (
        f"Expected 2 independent signals (media/curated list + award), "
        f"got {variant['independent_signal_count']}: {unique_families}"
    )
    assert set(unique_families) == {"media/curated list", "award"}

    for evidence in variant.get("source_evidence", []):
        assert evidence.get("signal_strength", "") != ""
        assert evidence.get("source_family", "") != ""

    award_ev = [e for e in variant.get("source_evidence", []) if e.get("source_family") == "award"]
    assert award_ev and award_ev[0]["signal_strength"] == "shortlist"

    # ── Criterion 5a: Ineligible rows excluded + per-row drop reasons ──
    excl_titles = {e.get("title", "") for e in internal_exclusions}
    assert "A Fiction Novel" in excl_titles
    fiction_excl = next(e for e in internal_exclusions if e.get("title") == "A Fiction Novel")
    assert "fiction" in str(fiction_excl.get("exclusion_reason", "")).lower()

    filtering_run = next(r for r in source_runs if r["source_name"] == "filtering-fixture")
    assert filtering_run["status"] == "collected"
    drop_reasons = filtering_run.get("debug_metadata", {}).get("drop_reasons", [])
    drop_titles = [d.get("title", "") for d in drop_reasons]
    assert any("Unverifiable" in t for t in drop_titles), "Unverifiable provenance row must have drop reason"

    # ── Criterion 5b: Forbidden copied text → failure, no leak ──
    forbidden_run = next(r for r in source_runs if r["source_name"] == "forbidden-text-fixture")
    assert forbidden_run["status"] == "failed"
    assert forbidden_run["debug_metadata"]["safe_failure_status"] == "forbidden_copied_text"
    forbidden_titles = {
        c["title"]
        for c in candidates
        if any(e.get("source_name") == "forbidden-text-fixture" for e in c.get("source_evidence", []))
    }
    assert not forbidden_titles, "Forbidden-text source must not leak into candidates"
    # Exact forbidden value absent from all serialized artifacts.
    _assert_forbidden_value_absent(r1_output, "run1")

    # ── Criterion 6: All health cases in one run, distinct statuses ──
    for case_name, _capture, _extraction, exp_status, exp_safe in HEALTH_CASES:
        run = next((r for r in source_runs if r["source_name"] == case_name), None)
        assert run is not None, f"Health case {case_name}: missing run"
        assert run["status"] == exp_status, f"{case_name}: expected status={exp_status}, got {run['status']}"
        assert run["debug_metadata"]["safe_failure_status"] == exp_safe, (
            f"{case_name}: expected safe_failure_status={exp_safe}"
        )

    assert any("Atelier de la gr" in c["title"] for c in candidates), (
        "French candidate missing despite health-edge failures"
    )

    # ── Criterion 7: Russian edition checking ──
    russian_cc: list[dict[str, Any]] = russian_checks.get("candidate_checks", [])
    assert len(russian_cc) > 0
    # Exact identity keys must have Russian check entries.
    for c in (nordic, polish, fr, it, es):
        ckey = c["identity_key"]
        check = next((rc for rc in russian_cc if rc.get("identity_key") == ckey), None)
        assert check is not None, f"Missing Russian check for {c['title']} ({ckey})"
        assert check.get("resolved_status") == "unclear", (
            f"{c['title']}: expected 'unclear', got {check.get('resolved_status')}"
        )
        verification = check.get("russian_edition_verification", {})
        assert verification.get("check_coverage") == "not_run"
        assert verification.get("provider_outcome") is None
        assert "verification is incomplete" in str(check.get("decision_reason", "")), (
            f"{c['title']}: reason must explain incomplete verification"
        )

    # ── Criterion 8: All sources in metrics with lifecycle ──
    metric_names = {m["source_name"] for m in metrics["metrics"]}
    for name in {s["name"] for s in all_sources}:
        assert name in metric_names, f"'{name}' missing from source-metrics.json"
    inactive_names = {s["name"] for s in all_sources if s.get("active") is not True}
    for metric in metrics["metrics"]:
        assert "lifecycle_status" in metric
        if metric["source_name"] in inactive_names:
            # A retired source stays visible in metrics, and says why (#597).
            assert metric.get("active") is False
            assert metric["lifecycle_status"] == "retired"
            continue
        assert metric.get("active") is True
        if metric["source_name"] in actual_market_expansion:
            assert metric["lifecycle_status"] in MARKET_EXPANSION_LIFECYCLE_STATUSES

    source_review = (r1_output / "source-review.md").read_text(encoding="utf-8")
    assert "no source is added, retired, or promoted automatically" in source_review.lower()

    # ── Criterion 9: Reporting distinguishes health ──
    ready = bool(run_summary.get("editor_facing_count", 0) > 0)
    rec = recurring_readiness(
        r1_output,
        ready,
        run_summary.get("editor_facing_count", 0),
        run_summary.get("skipped_source_count", 0),
        run_summary.get("failed_source_count", 0),
        source_runs=source_runs,
    )
    problem_names = {p["source_name"] for p in rec.get("problem_sources", [])}
    assert "source-blocked" in problem_names
    assert "wrong-target" in problem_names
    assert "forbidden-text-fixture" in problem_names
    assert "malformed-json" in problem_names
    assert "empty-extraction" in problem_names
    assert "healthy-empty" not in problem_names, "healthy_empty must not be an operational problem"

    skipped_names = {p["source_name"] for p in rec.get("problem_sources", []) if p.get("status") == "skipped"}
    failed_names = {p["source_name"] for p in rec.get("problem_sources", []) if p.get("status") == "failed"}
    assert "source-blocked" in skipped_names
    assert "wrong-target" in failed_names or "empty-extraction" in failed_names

    # ── Criterion 10: No country-specific parser or taxonomy change ──
    edge_prefixes = ("nordic", "variant", "filtering", "forbidden", "healthy", "wrong", "empty", "source", "malformed")
    for run in source_runs:
        if run["source_name"].startswith(edge_prefixes):
            assert run.get("extraction_method") in {"codex-extraction-replay"}

    VALID_SIGNAL_TYPES = {
        "award",
        "media/curated list",
        "bestseller/ranking",
        "reader-interest",
        "audiobook ranking",
        "rights/deal",
    }
    for mention in mentions:
        assert mention.get("signal_type", "") in VALID_SIGNAL_TYPES

    # === Run 2: second cycle with Premio Strega healthy-empty ===
    r2_work = tmp_path / "run2"
    r2_work.mkdir()
    r2_fixtures = tmp_path / "run2_fixtures"
    r2_fixtures.mkdir()

    # Re-resolve canonical sources fresh for run 2, replacing the Premio Strega
    # source's capture/extraction with a healthy-empty artifact.
    canonical_r2 = _resolve_canonical_sources()
    strega_name = "Premio Strega Saggistica 2026 Cinquina"
    strega_src = next(s for s in canonical_r2 if s["name"] == strega_name)
    strega_url = strega_src["url"]

    # Build healthy-empty capture/extraction for the Premio Strega source.
    # Must satisfy page-shape validation (expected_page_heading) while being empty.
    strega_expected_heading = str(strega_src.get("expected_page_heading", ""))
    strega_he_capture: dict[str, Any] = {
        "source_name": strega_name,
        "source_url": strega_url,
        "resolved_url": strega_url,
        "html": f"<h1>{strega_expected_heading}</h1>" if strega_expected_heading else "",
    }
    capture_path = r2_fixtures / "strega-healthy-empty-capture.json"
    extraction_path = r2_fixtures / "strega-healthy-empty-extraction.json"
    _write_json(capture_path, strega_he_capture)
    capture_bytes = capture_path.read_bytes()
    _write_json(
        extraction_path,
        {
            "source_name": strega_name,
            "source_url": strega_url,
            "operation_id": "op-2",
            "capture_sha256": hashlib.sha256(capture_bytes).hexdigest(),
            "entries": [],
        },
    )

    # Override the Premio Strega source with healthy-empty artifacts.
    for s in canonical_r2:
        if s["name"] == strega_name:
            s["source_page_fixture_file"] = str(capture_path)
            s["extraction_response_file"] = str(extraction_path)
            # The annual cinquina stands empty between announcements. The
            # declaration is what makes that a correct result rather than a
            # named failure (#464).
            s["may_report_no_entries"] = True
            s["may_report_no_entries_reason"] = "the annual cinquina lists no book between announcements"
            break

    r2_config = r2_work / "config.json"
    r2_output = r2_work / "output"
    r2_state = r2_work / "state"

    _write_json(
        r2_config,
        {
            "run_date": "2026-07-24",
            "sources": canonical_r2,
            "russian_edition_checks": [
                {"retailer": "Ozon", "url": "https://www.ozon.ru", "fixture_results": []},
                {"retailer": "LitRes", "url": "https://www.litres.ru", "fixture_results": []},
                {"retailer": "Book24", "url": "https://book24.ru", "fixture_results": []},
            ],
        },
    )

    # Copy run-1 state to carry candidate history forward: its files, written by the export under SQLite.
    export_json(r1_state)
    r2_state.mkdir(parents=True, exist_ok=True)
    for fname in [
        "candidate-history.json",
        "excluded-candidate-history.json",
        "pending-returned-editor-rows.json",
        "candidate-visibility.json",
        "editorial-profile.json",
        "source-proposals.json",
    ]:
        src = r1_state / fname
        if src.exists():
            shutil.copy2(src, r2_state / fname)
    # Candidate History one file per book (#1073).
    if (r1_state / "candidate-history").is_dir():
        shutil.copytree(r1_state / "candidate-history", r2_state / "candidate-history")
    sync_state(r2_state)

    run_pipeline(r2_config, r2_output, state_dir=r2_state)

    # ── Run 2 assertions ──
    r2_source_runs: list[dict[str, Any]] = read_json(r2_output / "source-runs.json")
    r2_candidates: list[dict[str, Any]] = read_json(r2_output / "scouting-candidates.json")

    strega_run_r2 = next(r for r in r2_source_runs if r["source_name"] == strega_name)
    assert strega_run_r2["status"] == "collected"
    assert strega_run_r2["debug_metadata"]["safe_failure_status"] == "healthy_empty"

    # The Italian Strega candidates from run 1 must NOT be in current candidates.
    it_titles_r1 = {
        c["title"] for c in candidates if any(e.get("language") == "it" for e in c.get("source_evidence", []))
    }
    it_titles_r2 = {c["title"] for c in r2_candidates}
    assert not it_titles_r1 & it_titles_r2, (
        f"Italian candidates from run 1 must be absent from run 2: {it_titles_r1 & it_titles_r2}"
    )

    # Render cumulative review.
    render_review(r2_output, state_dir=r2_state)

    review_md = (r2_output / "weekly-review.md").read_text(encoding="utf-8")
    # The French candidate from run 1 still appears.
    assert "Atelier" in review_md, "French candidate missing from cumulative review"

    # Exact Italian title from run 1 must appear as previously seen.
    ITALIAN_TITLE_RUN1 = "Alfabit. L\u2019italiano digitale dagli SMS all\u2019IA"
    assert ITALIAN_TITLE_RUN1 in review_md, (
        f"Italian run-1 candidate '{ITALIAN_TITLE_RUN1}' missing from cumulative review"
    )

    # History: Italian candidates have last_seen_date from run 1.
    r2_history: list[dict[str, Any]] = read_json(r2_output / "candidate-history.json")
    it_history = [h for h in r2_history if any(e.get("language") == "it" for e in h.get("source_evidence", []))]
    assert it_history
    for h in it_history:
        assert h.get("last_seen_date") == "2026-07-17", (
            f"Italian candidate {h.get('title')}: last_seen_date must be run 1 date"
        )

    # French candidate (active in both runs) updated to run 2 date.
    fr_history = [h for h in r2_history if h.get("identity_key", "").startswith("l atelier")]
    assert fr_history
    for h in fr_history:
        assert h["last_seen_date"] == "2026-07-24"
