"""Config/fixture-driven contract for every exact-book-list source (Issue #311)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from typing import cast

import pytest
from source_contract_battery import RUN_DATE
from source_contract_battery import ExactBookListCase
from source_contract_battery import _assert_helpers
from source_contract_battery import _card_rewrap_html
from source_contract_battery import assert_exact_book_list_contract

from autoscout.config import load_validated_config
from autoscout.json_io import read_json
from autoscout.replay_fixtures import build_replay_fixture_pair
from autoscout.replay_fixtures import run_capture_source_fixtures
from autoscout.source_collection import SourceCollection
from autoscout.source_collection import prepare_codex_extraction_worklist
from autoscout.source_collectors import codex_extraction_collector_result
from autoscout.source_collectors import extraction_contract_violation

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "configs"
CONFIG_NAMES = (
    "current-weekly-portfolio.json",
    "current-weekly-pilot.json",
    "current-weekly-pilot-live.json",
    "current-weekly-live.json",
)


def _cases() -> list[ExactBookListCase]:
    portfolio = load_validated_config(CONFIG_DIR / CONFIG_NAMES[0])
    pilot = load_validated_config(CONFIG_DIR / CONFIG_NAMES[1])
    # A retired source keeps its config entry so re-onboarding stays reversible,
    # but it never runs, so the contract battery does not cover it (#597).
    names = {
        s["name"] for s in portfolio["sources"] if s.get("metadata_exact_book_list") is True and s.get("active") is True
    }
    result = []
    for source in pilot["sources"]:
        if source["name"] not in names:
            continue
        resolved = dict(source)
        page_path = (CONFIG_DIR / resolved["source_page_fixture_file"]).resolve()
        extraction_path = (CONFIG_DIR / resolved["extraction_response_file"]).resolve()
        resolved["source_page_fixture_file"] = str(page_path)
        resolved["extraction_response_file"] = str(extraction_path)
        # Every fixture reference in a config is relative to the config file.
        # The battery re-homes the config into a temporary directory, so a
        # retained helper sidecar needs the same absolute rewrite as the page
        # and extraction fixtures. Without it a source that declares a sidecar
        # collects nothing and reports helper_metadata_missing.
        if resolved.get("helper_metadata_file"):
            resolved["helper_metadata_file"] = str((CONFIG_DIR / resolved["helper_metadata_file"]).resolve())
        result.append(ExactBookListCase(source["name"], resolved, read_json(page_path), read_json(extraction_path)))
    assert {case.name for case in result} == names
    return result


CASES = _cases()
MAGNESIA = next(case for case in CASES if case.name.startswith("Magnesia Litera"))


def _id(case: ExactBookListCase) -> str:
    return case.name.lower().replace(" ", "-")


@pytest.fixture(params=CASES, ids=_id)
def exact_list(request: pytest.FixtureRequest) -> ExactBookListCase:
    return cast(ExactBookListCase, request.param)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _copy_case(case: ExactBookListCase, tmp_path: Path, extraction: dict[str, Any] | None = None) -> dict[str, Any]:
    page_path, extraction_path = tmp_path / "page.json", tmp_path / "extraction.json"
    _write_json(page_path, case.page)
    _write_json(extraction_path, case.extraction if extraction is None else extraction)
    return case.replay_source(page_path, extraction_path)


def test_exact_source_occurs_once_with_four_config_parity(exact_list: ExactBookListCase) -> None:
    positions = []
    for filename in CONFIG_NAMES:
        config = load_validated_config(CONFIG_DIR / filename)
        names = [source["name"] for source in config["sources"]]
        assert names.count(exact_list.name) == 1
        positions.append(names.index(exact_list.name))
    assert len(set(positions)) == 1


def test_replay_overlay_method_and_fixture_paths(exact_list: ExactBookListCase) -> None:
    assert exact_list.source["extraction_method"] == "codex-extraction-replay"
    assert Path(exact_list.source["source_page_fixture_file"]).is_file()
    assert Path(exact_list.source["extraction_response_file"]).is_file()


def test_exact_list_source_contract_battery(
    exact_list: ExactBookListCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert_exact_book_list_contract(exact_list, tmp_path / "contract-battery", monkeypatch)


def test_card_rewrap_uses_run_year_when_publication_year_is_null() -> None:
    mention = {**MAGNESIA.extraction["mentions"][0], "publication_year": None}
    case = ExactBookListCase(MAGNESIA.name, MAGNESIA.source, MAGNESIA.page, {"mentions": [mention]})
    html = _card_rewrap_html(case)
    assert f'<p class="listing__meta">{RUN_DATE[:4]}</p>' in html
    assert ">None<" not in html


def _prepare(exact_list: ExactBookListCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, page: dict[str, Any]):
    source = {**exact_list.source, "source_page_fixture_file": "", "extraction_response_file": ""}
    config = tmp_path / "config.json"
    _write_json(config, {"run_date": RUN_DATE, "sources": [source]})
    import autoscout.source_collection as module

    monkeypatch.setattr(module, "codex_source_page", lambda *_a, **_k: (page, {"resolved_url": page["resolved_url"]}))
    return prepare_codex_extraction_worklist(config, tmp_path, RUN_DATE)


@pytest.mark.parametrize(
    "mutation,status",
    [("wrong-heading", "page_shape"), ("generic-heading", "page_shape"), ("wrong-target", "wrong_target")],
)
def test_prepare_rejects_invalid_page(
    exact_list: ExactBookListCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str, status: str
) -> None:
    page = dict(exact_list.page)
    if mutation == "wrong-target":
        page["resolved_url"] = "https://wrong.example.test/"
    else:
        page["html"] = (
            "<h1>Unrelated generic landing page</h1>"
            if mutation == "generic-heading"
            else "<h1>Definitely the wrong annual list</h1>"
        )
    result = _prepare(exact_list, tmp_path, monkeypatch, page)
    assert not result.work_items
    assert result.source_runs[0]["debug_metadata"]["safe_failure_status"] == status


def test_replay_rejects_wrong_heading(exact_list: ExactBookListCase, tmp_path: Path) -> None:
    page = {**exact_list.page, "html": "<h1>Generic source landing page</h1>"}
    page_path = tmp_path / "page.json"
    extraction_path = tmp_path / "extraction.json"
    _write_json(page_path, page)
    _write_json(extraction_path, exact_list.extraction)
    result = SourceCollection(tmp_path / "config.json", tmp_path, RUN_DATE).collect(
        [exact_list.replay_source(page_path, extraction_path)]
    )
    assert result.source_runs[0]["debug_metadata"]["safe_failure_status"] == "page_shape"


def test_exact_list_valid_case(exact_list: ExactBookListCase, tmp_path: Path) -> None:
    strict_chart = (
        exact_list.source.get("audible_explicit_chart") is True or exact_list.source.get("amazon_charts_weekly") is True
    )
    mentions = exact_list.extraction["mentions"] if strict_chart else [dict(exact_list.extraction["mentions"][0])]
    extraction = {"mentions": mentions}
    result = SourceCollection(tmp_path / "config.json", tmp_path, RUN_DATE).collect(
        [_copy_case(exact_list, tmp_path, extraction)]
    )
    expected_count = len(mentions) if strict_chart else 1
    assert result.source_runs[0]["valid_mention_count"] == expected_count


@pytest.mark.parametrize("value", [{"retained": "value"}, ["retained"], True, None, "unknown", "TODO", "N/A"])
def test_exact_list_live_contract_rejects_invalid_helper_values(value: Any) -> None:
    mention = dict(MAGNESIA.extraction["mentions"][0])
    mention["english_title"] = value
    reason = extraction_contract_violation(mention, mention, MAGNESIA.source, MAGNESIA.page)
    assert "helper metadata" in reason


@pytest.mark.parametrize("value", [{"retained": "value"}, ["retained"], True, None, "unknown", "TODO", "N/A"])
def test_contract_battery_rejects_invalid_helper_values(value: Any) -> None:
    extraction = json.loads(json.dumps(MAGNESIA.extraction))
    extraction["mentions"][0]["english_title"] = value
    broken = ExactBookListCase(MAGNESIA.name, MAGNESIA.source, MAGNESIA.page, extraction)
    with pytest.raises(AssertionError, match="helper metadata"):
        _assert_helpers(broken)


def test_invalid_magnesia_signal_metadata_is_reported_as_metadata_mismatch(tmp_path: Path) -> None:
    mention = dict(MAGNESIA.extraction["mentions"][0])
    mention["signal_type"] = "award_longlist"
    result = SourceCollection(tmp_path / "config.json", tmp_path, RUN_DATE).collect(
        [_copy_case(MAGNESIA, tmp_path, {"mentions": [mention]})]
    )

    run = result.source_runs[0]
    assert run["status"] == "failed"
    assert run["debug_metadata"]["safe_failure_status"] == "invalid_signal_metadata"
    assert run["drop_reasons"] == [
        {
            "title": mention["title"],
            "evidence_url": mention["evidence_url"],
            "reason": "exact book list signal_type must be award",
        }
    ]


# ── Generated replay fixture case (Issue #337) ───────────────────────────

GENERATED_SOURCE_URL = "https://example.test/generated-prize-shortlist"
GENERATED_SOURCE_NAME = "Generated Prize Shortlist"
GENERATED_SIGNAL_FAMILY = "award"
GENERATED_SIGNAL_STRENGTH = "shortlist"
GENERATED_CATEGORY = "adult nonfiction"
GENERATED_SLUG = "generated-prize-shortlist"
GENERATED_RUN_DATE = "2026-07-02"

_GENERATED_ENTRIES = (
    ("The Left Hand of Darkness", "Ursula K. Le Guin", "https://example.test/books/the-left-hand-of-darkness"),
    ("The Dispossessed", "Ursula K. Le Guin", "https://example.test/books/the-dispossessed"),
    ("Kindred", "Octavia E. Butler", "https://example.test/books/kindred"),
    ("The City & the City", "China Mieville", "https://example.test/books/the-city-and-the-city"),
)
_GENERATED_HELPERS = {
    "The Left Hand of Darkness": ("Левая рука тьмы", "Урсула К. Ле Гуин"),
    "The Dispossessed": ("Обездоленные", "Урсула К. Ле Гуин"),
    "Kindred": ("Родство", "Октавия Э. Батлер"),
}


def _generated_helper_metadata() -> dict[str, Any]:
    return {
        "version": 1,
        "source_url": GENERATED_SOURCE_URL,
        "language": "en",
        "entries": [
            {
                "title": title,
                "author": author,
                "evidence_url": url,
                "english_title": title,
                "search_translation": _GENERATED_HELPERS[title][0],
                "author_transliteration_note": _GENERATED_HELPERS[title][1],
            }
            for title, author, url in _GENERATED_ENTRIES[:3]
        ],
    }


def _generated_evidence() -> dict[str, Any]:
    """Local retained probe capture (Issue #334 shape); no live network."""
    items = "".join(
        f'<li><a href="{url}">{title}</a><span class="author"> {author} </span></li>'
        for title, author, url in _GENERATED_ENTRIES
    )
    html = (
        "<!DOCTYPE html><html><head><title>The Generated Prize announces 2026 shortlist</title></head>"
        "<body><main><h1>The Generated Prize announces 2026 shortlist</h1>"
        f'<ul class="shortlist">{items}</ul></main></body></html>'
    )
    return {
        "collection_mode": "source-candidate-probe",
        "source_url": GENERATED_SOURCE_URL,
        "source_name": GENERATED_SOURCE_NAME,
        "source_page": GENERATED_SOURCE_URL,
        "name": GENERATED_SOURCE_NAME,
        "signal_family": GENERATED_SIGNAL_FAMILY,
        # The trailing slash differs from the requested URL; it must never
        # leak into the generated page identity.
        "resolved_url": GENERATED_SOURCE_URL + "/",
        "head_status": 200,
        "get_status": 200,
        "html": html,
        "detected_heading": "The Generated Prize announces 2026 shortlist",
        "bounded_entry_count": len(_GENERATED_ENTRIES),
    }


def test_generated_replay_fixture_passes_exact_list_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    evidence_path = tmp_path / "evidence.json"
    _write_json(evidence_path, _generated_evidence())
    helper_metadata_path = tmp_path / "helper-metadata.json"
    _write_json(helper_metadata_path, _generated_helper_metadata())
    output_dir = tmp_path / "fixtures"
    report, code = run_capture_source_fixtures(
        GENERATED_SOURCE_URL,
        GENERATED_SLUG,
        evidence_path,
        output_dir=output_dir,
        signal_strength=GENERATED_SIGNAL_STRENGTH,
        candidate_category=GENERATED_CATEGORY,
        helper_metadata_file=helper_metadata_path,
    )
    assert code == 0
    assert report["status"] == "created"
    page_path = Path(report["page_output"])
    extraction_path = Path(report["extraction_output"])
    page = read_json(page_path)
    extraction = read_json(extraction_path)
    source = {
        "name": GENERATED_SOURCE_NAME,
        "url": GENERATED_SOURCE_URL,
        "family": GENERATED_SIGNAL_FAMILY,
        "signal_family": GENERATED_SIGNAL_FAMILY,
        "metadata_exact_book_list": True,
        "expected_signal_strength": GENERATED_SIGNAL_STRENGTH,
        "eligible_candidate_category": GENERATED_CATEGORY,
        "extraction_method": "codex-extraction-replay",
        "source_page_fixture_file": str(page_path),
        "extraction_response_file": str(extraction_path),
        "active": True,
        "access_status": "open",
        "check_frequency": "weekly",
        "language": "en",
        "country": "United States",
        "market": "United States",
        "expected_category": "Adult nonfiction",
        "refresh_cadence": "weekly",
        "url_stability": "stable category page",
        "extraction_notes": "Retained generated probe evidence for contract tests.",
        "expected_page_heading": "The Generated Prize announces 2026 shortlist",
        "lifecycle_status": "probation",
        "lifecycle_note": "Issue #337 generated replay contract fixture.",
        "notes": "Offline generated fixture only.",
    }
    case = ExactBookListCase(GENERATED_SOURCE_NAME, source, page, extraction)
    assert_exact_book_list_contract(case, tmp_path / "contract-battery", monkeypatch)

    # The generated fixture has an exact bounded count and capture binding.
    assert set(extraction) == {"source_name", "source_url", "operation_id", "capture_sha256", "mentions"}
    count = len(extraction["mentions"])
    assert count == 3
    assert extraction["capture_sha256"] == hashlib.sha256(page_path.read_bytes()).hexdigest()

    # The replay collector exposes the detailed generated-fixture diagnostics.
    result = codex_extraction_collector_result(tmp_path / "config.json", tmp_path / "out", source, GENERATED_RUN_DATE)
    assert len(result.mentions) == count
    assert result.debug_metadata["validated_mention_count"] == count
    assert result.debug_metadata["dropped_mention_count"] == 0
    assert result.debug_metadata["safe_failure_status"] == "healthy"

    for field in ("english_title", "search_translation", "author_transliteration_note"):
        for empty in (False, True):
            broken_extraction = json.loads(json.dumps(extraction))
            if empty:
                broken_extraction["mentions"][0][field] = ""
            else:
                broken_extraction["mentions"][0].pop(field)
            broken_case = ExactBookListCase(GENERATED_SOURCE_NAME, source, page, broken_extraction)
            with pytest.raises(AssertionError, match=field):
                assert_exact_book_list_contract(
                    broken_case,
                    tmp_path / f"broken-{field}-{'empty' if empty else 'removed'}",
                    monkeypatch,
                )


def test_generated_fixture_pairs_are_deterministic() -> None:
    first = build_replay_fixture_pair(
        GENERATED_SOURCE_URL,
        GENERATED_SLUG,
        _generated_evidence(),
        signal_strength=GENERATED_SIGNAL_STRENGTH,
        candidate_category=GENERATED_CATEGORY,
        helper_metadata=_generated_helper_metadata(),
    )
    second = build_replay_fixture_pair(
        GENERATED_SOURCE_URL,
        GENERATED_SLUG,
        _generated_evidence(),
        signal_strength=GENERATED_SIGNAL_STRENGTH,
        candidate_category=GENERATED_CATEGORY,
        helper_metadata=_generated_helper_metadata(),
    )
    assert first.page_bytes == second.page_bytes
    assert first.extraction_bytes == second.extraction_bytes
    assert first.capture_sha256 == second.capture_sha256
