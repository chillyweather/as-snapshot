from __future__ import annotations

import gzip
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

import autoscout.source_collection as collection_module
import autoscout.source_collectors as collectors_module
from autoscout.codex_recovery import build_codex_extraction_prompt
from autoscout.codex_workflow import finalize_codex_run
from autoscout.codex_workflow import prepare_codex_extraction
from autoscout.config import SourceConfig
from autoscout.config import load_validated_config
from autoscout.json_io import read_json
from autoscout.model_qualification import qualification_work_item
from autoscout.pipeline import run_pipeline
from autoscout.source_collection import SourceCollection
from autoscout.source_collection import prepare_codex_extraction_worklist
from autoscout.source_metadata import SOURCE_METADATA_FIELDS
from autoscout.source_metadata import SOURCE_PAGE_IDENTITY_FIELDS

ROOT = Path(__file__).resolve().parents[1]
SOURCE_NAME = "Académie française Prix de l’essai"
SOURCE_URL = "https://www.academie-francaise.fr/prix-de-lessai"


def french_source() -> dict[str, Any]:
    config = load_validated_config(ROOT / "configs/current-weekly-pilot.json")
    source = dict(next(item for item in config["sources"] if item["name"] == SOURCE_NAME))
    for field in ("source_page_fixture_file", "extraction_response_file"):
        source[field] = str((ROOT / "configs" / source[field]).resolve())
    return source


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_typed_source_config_exposes_reusable_metadata() -> None:
    source = SourceConfig(
        name=SOURCE_NAME,
        url=SOURCE_URL,
        language="fr",
        country="France",
        market="France",
        expected_category="adult nonfiction/essay",
        signal_family="award",
        refresh_cadence="annual",
        url_stability="stable",
        extraction_notes="metadata only",
    )
    assert {field: getattr(source, field) for field in SOURCE_METADATA_FIELDS} == {
        "language": "fr",
        "country": "France",
        "market": "France",
        "expected_category": "adult nonfiction/essay",
        "signal_family": "award",
        "refresh_cadence": "annual",
        "url_stability": "stable",
        "extraction_notes": "metadata only",
    }


def test_recorded_collection_carries_declared_source_metadata(tmp_path: Path) -> None:
    mentions_file = tmp_path / "recorded.json"
    write_json(
        mentions_file,
        {
            "mentions": [
                {
                    "title": "Essai enregistré",
                    "primary_authors": ["Autrice Exemple"],
                    "source_url": SOURCE_URL,
                    "source_check_date": "2026-07-17",
                    "signal_type": "award",
                    "signal_strength": "winner",
                    "evidence_note": "Recorded metadata-only award evidence.",
                }
            ]
        },
    )
    source = {
        **french_source(),
        "extraction_method": "recorded",
        "source_page_fixture_file": "",
        "extraction_response_file": "",
        "recorded_mentions_file": str(mentions_file),
    }

    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-07-17").collect([source])

    mention = result.source_mentions[0]
    assert {field: mention[field] for field in SOURCE_METADATA_FIELDS} == {
        field: source[field] for field in SOURCE_METADATA_FIELDS
    }


def test_portfolio_prepare_finalize_carries_declared_metadata_and_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = french_source()
    config = tmp_path / "live.json"
    write_json(config, {"run_date": "2026-07-17", "sources": [source]})
    monkeypatch.setattr(
        collection_module,
        "codex_source_page",
        lambda *_args, **_kwargs: (
            {"source_page": SOURCE_URL, "resolved_url": SOURCE_URL, "html": "<h1>Prix de l’essai</h1>"},
            {"resolved_url": SOURCE_URL},
        ),
    )

    assert prepare_codex_extraction(config, tmp_path, effective_date="2026-07-17") == 0
    worklist_data = read_json(tmp_path / "codex-extraction-worklist.json")
    item = worklist_data["work_items"][0]
    assert {field: item["source_metadata"][field] for field in SOURCE_METADATA_FIELDS} == {
        field: source[field] for field in SOURCE_METADATA_FIELDS
    }
    assert item["source_metadata"]["lifecycle_status"] == "established"
    assert item["source_metadata"]["lifecycle_note"] == source["lifecycle_note"]
    write_json(tmp_path / item["expected_extraction_file"], {"mentions": []})

    assert finalize_codex_run(tmp_path) == 0
    replay_source = read_json(tmp_path / "codex-replay-config.json")["sources"][0]
    for field in (*SOURCE_METADATA_FIELDS, "lifecycle_status", "lifecycle_note"):
        assert replay_source[field] == source[field]


def test_page_identity_fields_portable_through_prepare_finalize_but_not_declared_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Page identity fields (expected_page_heading, expected_sort_label) are
    capture/replay config, not declared Source Mention metadata.
    They must flow through prepare/finalize but never appear in mentions."""
    source = {
        **french_source(),
        "expected_page_heading": "Prix de l\u2019essai",
        "expected_sort_label": "",
    }
    config = tmp_path / "config.json"
    write_json(config, {"run_date": "2026-07-17", "sources": [source]})
    monkeypatch.setattr(
        collection_module,
        "codex_source_page",
        lambda *_args, **_kwargs: (
            {"source_page": SOURCE_URL, "resolved_url": SOURCE_URL, "html": "<h1>Prix de l\u2019essai</h1>"},
            {"resolved_url": SOURCE_URL},
        ),
    )

    # Prepare: page identity fields must appear in worklist source_metadata.
    assert prepare_codex_extraction(config, tmp_path, effective_date="2026-07-17") == 0
    worklist_data = read_json(tmp_path / "codex-extraction-worklist.json")
    item = worklist_data["work_items"][0]
    for field in SOURCE_PAGE_IDENTITY_FIELDS:
        assert item["source_metadata"][field] == source[field]

    # Finalize: page identity fields preserved in replay config source.
    write_json(tmp_path / item["expected_extraction_file"], {"mentions": []})
    assert finalize_codex_run(tmp_path) == 0
    replay_source = read_json(tmp_path / "codex-replay-config.json")["sources"][0]
    for field in SOURCE_PAGE_IDENTITY_FIELDS:
        assert replay_source[field] == source[field]

    # But declared_source_metadata and mentions do NOT carry page identity.
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-07-17").collect([french_source()])
    mention = result.source_mentions[0]
    for field in SOURCE_PAGE_IDENTITY_FIELDS:
        assert field not in mention, f"{field} must not leak into Source Mentions"


def test_deterministic_pipeline_preserves_unicode_identity_helpers_and_metrics(tmp_path: Path) -> None:
    source = french_source()
    config = tmp_path / "replay.json"
    write_json(config, {"run_date": "2026-07-17", "sources": [source], "russian_edition_checks": []})
    output = tmp_path / "output"

    run_pipeline(config, output, state_dir=tmp_path / "state")

    mention = read_json(output / "source-mentions.json")[0]
    candidate = read_json(output / "scouting-candidates.json")[0]
    run = read_json(output / "source-runs.json")[0]
    metric = read_json(output / "source-metrics.json")["metrics"][0]
    assert mention["title"] == candidate["title"] == "L’Atelier de la grâce"
    assert mention["primary_authors"] == candidate["primary_authors"] == ["Anne-Marie Lecoq", "Alain Mérot"]
    assert candidate["identity_key"] == "l atelier de la grâce|anne marie lecoq & alain mérot"
    assert candidate["english_title"] == "The Workshop of Grace"
    assert mention["search_translation"] == "Мастерская благодати"
    assert candidate["russian_search_notes"]["search_translation"] == "Мастерская благодати"
    assert candidate["title"] not in {
        candidate["english_title"],
        candidate["russian_search_notes"]["search_translation"],
    }
    evidence = candidate["source_evidence"][0]
    assert evidence["language"] == "fr" and evidence["expected_category"] == "adult nonfiction/essay"
    assert run["debug_metadata"]["safe_failure_status"] == "healthy"
    assert metric["language"] == "fr" and metric["lifecycle_status"] == "established"


BENCHMARK_CASE = ROOT / "data/model-qualification/benchmark/cases/academie-francaise-prix-de-lessai"
LAUREATE_ROW = re.compile(
    r'<div class="academie-laureate">\s*<span class="date">(\d{4})</span>.*?<em>(.*?)</em>', re.DOTALL
)


def test_source_declares_current_edition_scope_to_the_extractor() -> None:
    """The laureate page is cumulative; the source contributes only its
    current edition, and the extractor is told so (Issue #751)."""
    source = french_source()
    notes = source["extraction_notes"]
    assert "Extract only the current edition" in notes
    assert "greatest year on the page" in notes
    assert "never a row of an earlier year" in notes
    assert "list the laureate of every such row in primary_authors, in page order" in notes
    assert "without the page's civility prefix (M., Mme)" in notes
    assert "Set source_url and evidence_url to the configured page URL" in notes

    prompt = build_codex_extraction_prompt(qualification_work_item(source, "capture.json", "0" * 64))
    assert notes in prompt
    assert "source_metadata fields are binding extraction constraints" in prompt


def test_frozen_capture_is_cumulative_and_its_reference_is_the_current_edition() -> None:
    capture = json.loads(gzip.decompress((BENCHMARK_CASE / "capture.json.gz").read_bytes()))
    rows = [(int(year), " ".join(title.split())) for year, title in LAUREATE_ROW.findall(capture["html"])]
    years = {year for year, _title in rows}
    assert len(years) > 40 and min(years) < 1980

    current_titles = {title for year, title in rows if year == max(years)}
    reference = read_json(BENCHMARK_CASE / "reference.json")
    assert {entry["title"] for entry in reference["entries"]} == current_titles == {"L’Atelier de la grâce"}
    assert {entry["evidence_url"] for entry in reference["entries"]} == {SOURCE_URL}


def make_replay_source(tmp_path: Path, name: str, capture: Any, extraction: Any) -> dict[str, Any]:
    capture_path = tmp_path / f"{name}-capture.json"
    extraction_path = tmp_path / f"{name}-extraction.json"
    if isinstance(capture, str):
        capture_path.write_text(capture, encoding="utf-8")
    else:
        write_json(capture_path, capture)
    if isinstance(extraction, str):
        extraction_path.write_text(extraction, encoding="utf-8")
    else:
        write_json(extraction_path, extraction)
    return {
        "name": name,
        "family": "award",
        "url": f"https://example.test/{name}",
        "active": True,
        "access_status": "open",
        "check_frequency": "annual",
        "language": "en",
        "extraction_method": "codex-extraction-replay",
        "source_page_fixture_file": str(capture_path),
        "extraction_response_file": str(extraction_path),
    }


@pytest.mark.parametrize(
    ("case", "capture", "extraction", "status", "safe_status"),
    [
        (
            "wrong",
            {"source_name": "wrong", "source_url": "https://example.test/wrong", "resolved_url": "https://other.test/"},
            {"mentions": []},
            "failed",
            "wrong_target",
        ),
        ("plain-empty", {}, {"mentions": []}, "failed", "empty_extraction"),
        ("blocked", {}, {"status": "blocked", "mentions": []}, "skipped", "source_blocked"),
        ("malformed", {}, "{", "failed", "malformed_json"),
        (
            "provenance",
            {},
            {
                "mentions": [
                    {
                        "title": "Bad",
                        "primary_authors": ["A"],
                        "source_url": "https://evil.test/x",
                        "evidence_url": "https://evil.test/x",
                        "signal_type": "award",
                        "signal_strength": "winner",
                        "evidence_note": "Bad provenance.",
                    }
                ]
            },
            "failed",
            "unverifiable_evidence_urls",
        ),
    ],
)
def test_bad_source_health_is_distinct_and_does_not_suppress_french_candidate(
    tmp_path: Path, case: str, capture: Any, extraction: Any, status: str, safe_status: str
) -> None:
    bad = make_replay_source(tmp_path, case, capture, extraction)
    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-07-17").collect([bad, french_source()])

    bad_run = result.source_runs[0]
    assert bad_run["status"] == status
    assert bad_run["debug_metadata"]["safe_failure_status"] == safe_status
    assert any(mention["title"] == "L’Atelier de la grâce" for mention in result.source_mentions)


def test_authoritative_healthy_empty_requires_exact_capture_contract(tmp_path: Path) -> None:
    name = "healthy-empty"
    url = f"https://example.test/{name}"
    capture = {"source_name": name, "source_url": url, "resolved_url": url, "html": ""}
    source = make_replay_source(tmp_path, name, capture, {})
    # An annual award page stands empty between announcements, and declares it.
    # An undeclared zero is a failure now, because a source that publishes a
    # complete list and publishes none is broken (#464).
    source["may_report_no_entries"] = True
    source["may_report_no_entries_reason"] = "an annual award page lists no laureate between announcements"
    capture_bytes = Path(source["source_page_fixture_file"]).read_bytes()
    write_json(
        Path(source["extraction_response_file"]),
        {
            "source_name": name,
            "source_url": url,
            "operation_id": "op-1",
            "capture_sha256": hashlib.sha256(capture_bytes).hexdigest(),
            "entries": [],
        },
    )

    result = SourceCollection(tmp_path / "config.json", tmp_path, "2026-07-17").collect([source])
    assert result.source_runs[0]["status"] == "collected"
    assert result.source_runs[0]["debug_metadata"]["safe_failure_status"] == "healthy_empty"
    # A zero never carries an empty reason, declared or not (#464).
    assert "an annual award page lists no laureate between announcements" in result.source_runs[0]["reason"]

    capture.pop("source_name")
    write_json(Path(source["source_page_fixture_file"]), capture)
    failed = SourceCollection(tmp_path / "config.json", tmp_path, "2026-07-17").collect([source])
    assert failed.source_runs[0]["debug_metadata"]["safe_failure_status"] == "empty_extraction"


def test_prepare_rejects_wrong_target_before_capture_and_keeps_healthy_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrong = {**french_source(), "name": "Wrong target", "url": "https://example.test/prize"}
    healthy = french_source()
    config = tmp_path / "config.json"
    write_json(config, {"sources": [wrong, healthy]})

    def pages(
        _config: Path, _output: Path, source: dict[str, Any], **_kwargs: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        resolved = "https://unrelated.test/" if source["name"] == "Wrong target" else source["url"]
        return {"source_page": source["url"], "resolved_url": resolved, "html": "metadata"}, {"resolved_url": resolved}

    monkeypatch.setattr(collection_module, "codex_source_page", pages)
    result = prepare_codex_extraction_worklist(config, tmp_path, "2026-07-17")
    assert [item["source_name"] for item in result.work_items] == [SOURCE_NAME]
    wrong_run = next(run for run in result.source_runs if run["source_name"] == "Wrong target")
    assert wrong_run["debug_metadata"]["safe_failure_status"] == "wrong_target"
    assert not any(path.name.startswith("codex-capture-wrong-target") for path in tmp_path.iterdir())


def test_prepare_marks_http_block_as_source_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = {**french_source(), "source_page_fixture_file": "", "extraction_response_file": ""}
    config = tmp_path / "config.json"
    write_json(config, {"sources": [source]})

    def blocked(*_args: Any, **_kwargs: Any) -> Any:
        raise HTTPError(SOURCE_URL, 403, "blocked", {}, None)

    monkeypatch.setattr(collectors_module, "urlopen", blocked)
    result = prepare_codex_extraction_worklist(config, tmp_path, "2026-07-17", live=True)
    assert result.source_runs[0]["status"] == "skipped"
    assert result.source_runs[0]["debug_metadata"]["safe_failure_status"] == "source_blocked"
