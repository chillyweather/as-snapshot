"""Regression coverage for Issue #450 Amazon.de Codex extraction."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from autoscout.codex_recovery import ProductionCodexAdapter
from autoscout.codex_recovery import build_codex_extraction_prompt
from autoscout.config import load_validated_config
from autoscout.observed_extraction import validate_extraction_artifact
from autoscout.source_collection import SourceCollection
from autoscout.source_collectors import agent_extraction_prompt
from autoscout.source_collectors import extraction_contract

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "issue-450"
CAPTURE = FIXTURE_DIR / "amazon-de-chart-capture.json"
REJECTED_ARTIFACT = FIXTURE_DIR / "amazon-de-chart-rejected-artifact.json"
CONFIG = ROOT / "configs" / "current-weekly-portfolio.json"
RUN_DATE = "2026-08-13"
SOURCE_NAME = "Amazon.de Charts Meistgelesene Sachbücher"
SOURCE_URL = "https://www.amazon.de/charts"
CAPTURE_SHA256 = "cf80b528d092117dc3654d4714732204a91a5b965914ecfccc9fc70f7900f42d"
OPERATION_ID = "d034c7bed891178f14a4227e55ca312a4dcf66bd4076f42e4b15ad3085ec9374"
PROMPT_SEARCH_TRANSLATION_RULE = "search_translation must be nonblank and different from the authoritative title"
# A search_translation equal to the title carries no translation, so it counts
# as absent and the entry fails closed as missing helper metadata.
VALIDATOR_SEARCH_TRANSLATION_RULE = "helper metadata is missing: search_translation"


def _source(*, extraction_path: Path = REJECTED_ARTIFACT) -> dict[str, Any]:
    config = load_validated_config(CONFIG)
    configured = next(source for source in config["sources"] if source["name"] == SOURCE_NAME)
    return {
        **configured,
        "extraction_method": "codex-extraction-replay",
        "source_page_fixture_file": str(CAPTURE),
        "extraction_response_file": str(extraction_path),
    }


def _work_item() -> dict[str, Any]:
    source = _source()
    return {
        "source_name": SOURCE_NAME,
        "source_url": SOURCE_URL,
        "source_family": str(source["family"]),
        "operation_id": OPERATION_ID,
        "capture_sha256": CAPTURE_SHA256,
        "source_metadata": dict(source),
    }


def _read_artifact() -> dict[str, Any]:
    artifact = json.loads(REJECTED_ARTIFACT.read_text(encoding="utf-8"))
    assert isinstance(artifact, dict)
    return artifact


def _write_corrected_artifact(path: Path) -> None:
    artifact = _read_artifact()
    for entry in artifact["entries"]:
        entry["search_translation"] = f"Поиск: {entry['title']}"
    path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_german_amazon_chart_contract_explicitly_requires_distinct_search_translation() -> None:
    source = _source()
    contract = extraction_contract(
        source,
        source_page="https://www.amazon.de/charts/2026-08-09/mostread/nonfiction",
        capture_file=CAPTURE.name,
    )
    rules = " ".join(contract["rules"])
    prompt = agent_extraction_prompt(
        source,
        {"source_page": "https://www.amazon.de/charts/2026-08-09/mostread/nonfiction", "html": ""},
        1000,
    )
    codex_prompt = build_codex_extraction_prompt(_work_item())

    assert PROMPT_SEARCH_TRANSLATION_RULE in rules
    assert PROMPT_SEARCH_TRANSLATION_RULE in prompt
    assert PROMPT_SEARCH_TRANSLATION_RULE in codex_prompt


def test_preserved_rejected_amazon_chart_reproduces_helper_metadata_failure(tmp_path: Path) -> None:
    result = SourceCollection(CONFIG, tmp_path, RUN_DATE).collect([_source()])
    source_run = result.source_runs[0]

    assert source_run["status"] == "failed"
    assert source_run["raw_mention_count"] == 20
    assert source_run["valid_mention_count"] == 0
    assert VALIDATOR_SEARCH_TRANSLATION_RULE in source_run["reason"]
    assert source_run["debug_metadata"]["contract_rejected_count"] == 20
    assert {drop["reason"] for drop in source_run["debug_metadata"]["drop_reasons"]} == {
        f"Amazon Charts weekly {VALIDATOR_SEARCH_TRANSLATION_RULE}"
    }


def test_corrected_amazon_chart_collects_all_exact_ranks(tmp_path: Path) -> None:
    corrected = tmp_path / "amazon-de-chart-corrected.json"
    _write_corrected_artifact(corrected)

    result = SourceCollection(CONFIG, tmp_path, RUN_DATE).collect([_source(extraction_path=corrected)])
    source_run = result.source_runs[0]

    assert source_run["status"] == "collected"
    assert source_run["raw_mention_count"] == 20
    assert source_run["valid_mention_count"] == 20
    assert [int(mention["current_rank"]) for mention in result.source_mentions] == list(range(1, 21))


def test_corrected_artifact_validates_at_observed_and_production_seams(tmp_path: Path) -> None:
    corrected = tmp_path / "amazon-de-chart-corrected.json"
    _write_corrected_artifact(corrected)
    item = _work_item()

    observed = validate_extraction_artifact(corrected, item, CAPTURE)

    assert observed.valid is True
    assert observed.validated_count == 20
    assert ProductionCodexAdapter().validate_extraction_artifact(corrected, item, CAPTURE) is True


def test_codex_artifact_validation_rejects_wrong_capture_digest(tmp_path: Path) -> None:
    corrected = tmp_path / "amazon-de-chart-corrected.json"
    _write_corrected_artifact(corrected)
    artifact = json.loads(corrected.read_text(encoding="utf-8"))
    artifact["capture_sha256"] = "0" * 64
    wrong_digest = tmp_path / "amazon-de-chart-wrong-digest.json"
    wrong_digest.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
    item = _work_item()

    observed = validate_extraction_artifact(wrong_digest, item, CAPTURE)

    assert observed.valid is False
    assert ProductionCodexAdapter().validate_extraction_artifact(wrong_digest, item, CAPTURE) is False


def test_codex_artifact_validation_rejects_evidence_absent_from_capture(tmp_path: Path) -> None:
    corrected = tmp_path / "amazon-de-chart-corrected.json"
    _write_corrected_artifact(corrected)
    artifact = json.loads(corrected.read_text(encoding="utf-8"))
    artifact["entries"][0]["evidence_url"] = "/dp/0000000000/ref=not-in-capture"
    absent_evidence = tmp_path / "amazon-de-chart-absent-evidence.json"
    absent_evidence.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
    item = _work_item()

    observed = validate_extraction_artifact(absent_evidence, item, CAPTURE)

    # The row is refused; since #839 the gate keeps the other rows of the list.
    assert observed.valid is True
    assert observed.dropped_indexes == (0,)
    assert observed.dropped_rows[0]["reason"] == "unverifiable_provenance"
    assert ProductionCodexAdapter().validate_extraction_artifact(absent_evidence, item, CAPTURE) is False


def test_issue_450_capture_fixture_keeps_the_exact_identity_digest() -> None:
    assert hashlib.sha256(CAPTURE.read_bytes()).hexdigest() == CAPTURE_SHA256
    artifact = _read_artifact()
    assert {artifact[key] for key in ("source_name", "source_url", "operation_id", "capture_sha256")} == {
        SOURCE_NAME,
        SOURCE_URL,
        OPERATION_ID,
        CAPTURE_SHA256,
    }
