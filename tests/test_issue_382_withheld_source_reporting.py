"""Issue #382 — a withheld source is a source problem, not a disabled source.

The 2026-08-05 live weekly run reported ``codex_workflow_health: healthy``,
``failed_source_count: 0`` and no warnings while 18 of 29 sources timed out and
four newly onboarded sources produced no extraction artifact at all. Those four
were reported as ``inactive`` with the reason ``source is disabled``, even
though no config disabled them.
"""

from __future__ import annotations

import json
from pathlib import Path

from autoscout.config import validate_config
from autoscout.run_health import codex_workflow_health
from autoscout.source_collection import SourceCollection
from autoscout.source_rules import EXTRACTION_ARTIFACT_MISSING
from autoscout.source_rules import PRECOLLECTED_ARTIFACT_UNAVAILABLE
from autoscout.source_rules import withheld_reason
from autoscout.source_rules import withheld_reason_message


def _collect(tmp_path: Path, sources: list[dict]) -> list[dict]:
    collection = SourceCollection(config_path=tmp_path / "config.json", output_dir=tmp_path, run_date="2026-08-05")
    return collection.collect(sources).source_runs


def test_operator_disabled_source_stays_inactive(tmp_path: Path) -> None:
    runs = _collect(
        tmp_path,
        [
            {
                "name": "Retired Award",
                "family": "award",
                "url": "https://example.test",
                "language": "en",
                "active": False,
            }
        ],
    )

    assert len(runs) == 1
    assert runs[0]["status"] == "inactive"
    assert runs[0]["reason"] == "source is disabled"


def test_withheld_source_is_reported_as_failed(tmp_path: Path) -> None:
    sources = [
        {
            "name": "Amazon.de Charts Meistgelesene Sachbücher",
            "family": "bestseller/ranking",
            "url": "https://example.test",
            "language": "en",
            "active": False,
            "inactive_reason": EXTRACTION_ARTIFACT_MISSING,
        }
    ]

    collection = SourceCollection(config_path=tmp_path / "config.json", output_dir=tmp_path, run_date="2026-08-05")
    result = collection.collect(sources)

    assert result.failed_source_count == 1
    assert result.source_runs[0]["status"] == "failed"
    assert "no artifact" in result.source_runs[0]["reason"]
    assert result.source_runs[0]["reason"] != "source is disabled"


def test_withheld_precollected_source_is_reported_as_failed(tmp_path: Path) -> None:
    sources = [
        {
            "name": "Nonfiction.fr Critiques",
            "family": "media/curated list",
            "url": "https://example.test",
            "language": "en",
            "active": False,
            "inactive_reason": PRECOLLECTED_ARTIFACT_UNAVAILABLE,
        }
    ]

    result = SourceCollection(config_path=tmp_path / "config.json", output_dir=tmp_path, run_date="2026-08-05").collect(
        sources
    )

    assert result.failed_source_count == 1
    assert result.source_runs[0]["status"] == "failed"
    assert "precollected" in result.source_runs[0]["reason"]


def test_withheld_source_makes_codex_workflow_health_unhealthy(tmp_path: Path) -> None:
    """The whole reporting chain must move, not only the source run record.

    The collection mode is derived from ``extraction_method`` rather than being
    overwritten afterwards, so this fails if the derivation stops classifying a
    withheld Codex source as Codex-operated.
    """
    sources = [
        {
            "name": "Amazon Charts Most Read Nonfiction",
            "family": "bestseller/ranking",
            "url": "https://example.test",
            "language": "en",
            "active": False,
            "inactive_reason": EXTRACTION_ARTIFACT_MISSING,
            "extraction_method": "codex-extraction-replay",
        }
    ]

    runs = _collect(tmp_path, sources)

    assert runs[0]["collection_mode"] == "codex-capture"
    assert codex_workflow_health(runs, {}) == "failed"


def test_inactive_source_without_marker_keeps_health_healthy(tmp_path: Path) -> None:
    runs = _collect(
        tmp_path,
        [
            {
                "name": "Off",
                "family": "award",
                "url": "https://example.test",
                "language": "en",
                "active": False,
                "extraction_method": "codex-extraction-replay",
            }
        ],
    )

    assert runs[0]["collection_mode"] == "codex-capture"
    assert codex_workflow_health(runs, {}) == "healthy"


def test_withheld_precollected_source_moves_counts_but_not_codex_health(tmp_path: Path) -> None:
    """A precollected source is not Codex-operated, so it cannot fail Codex health.

    ``codex_workflow_health`` reports on the Codex workflow specifically, and a
    precollected source derives a non-Codex collection mode. The source problem
    still reaches ``failed_source_count`` and ``major_warnings``, which is what
    the operator acts on. This asymmetry is intended, and pinned here so a future
    change to the derivation cannot make it silent by accident.
    """
    sources = [
        {
            "name": "Nonfiction.fr Critiques",
            "family": "media/curated list",
            "url": "https://example.test",
            "language": "en",
            "active": False,
            "inactive_reason": PRECOLLECTED_ARTIFACT_UNAVAILABLE,
            "recorded_mentions_file": "precollected-example.json",
        }
    ]

    result = SourceCollection(config_path=tmp_path / "config.json", output_dir=tmp_path, run_date="2026-08-05").collect(
        sources
    )

    assert result.failed_source_count == 1
    assert result.source_runs[0]["status"] == "failed"
    assert not str(result.source_runs[0]["collection_mode"]).startswith("codex")
    assert codex_workflow_health(result.source_runs, {}) == "absent"


def test_withheld_reason_helpers_ignore_unknown_and_missing_values() -> None:
    assert withheld_reason({}) is None
    assert withheld_reason({"inactive_reason": "operator-said-so"}) is None
    assert withheld_reason_message({}) == ""
    assert withheld_reason({"inactive_reason": EXTRACTION_ARTIFACT_MISSING}) == EXTRACTION_ARTIFACT_MISSING
    assert withheld_reason_message({"inactive_reason": EXTRACTION_ARTIFACT_MISSING})


def test_finalize_marks_missing_extraction_artifact(tmp_path: Path) -> None:
    """finalize_codex_run must record why it withheld the source."""
    from autoscout.codex_workflow import finalize_codex_run

    output_dir = tmp_path / "run"
    output_dir.mkdir()
    capture_name = "codex-capture-example-source.json"
    (output_dir / capture_name).write_text(json.dumps({"html": "<html></html>"}), encoding="utf-8")
    (output_dir / "codex-extraction-worklist.json").write_text(
        json.dumps(
            {
                "run_date": "2026-08-05",
                "work_items": [
                    {
                        "source_name": "Example Source",
                        "source_url": "https://example.test/list",
                        "source_family": "award",
                        "capture_file": capture_name,
                        "expected_extraction_file": "example-source-codex-extraction.json",
                        "source_metadata": {
                            "access_status": "open",
                            "check_frequency": "weekly",
                            "language": "en",
                            "notes": "",
                        },
                    }
                ],
                "precollected_sources": [],
            }
        ),
        encoding="utf-8",
    )

    finalize_codex_run(output_dir)

    config = json.loads((output_dir / "codex-replay-config.json").read_text(encoding="utf-8"))
    withheld = [source for source in config["sources"] if source.get("active") is False]
    assert len(withheld) == 1
    assert withheld[0]["inactive_reason"] == EXTRACTION_ARTIFACT_MISSING


def test_generated_replay_config_keys_pass_validation() -> None:
    """A generated replay config must satisfy the project's own validator.

    ``operation_id`` and ``recorded_mentions_sha256`` are capture-binding
    identity fields written onto precollected sources, so the documented
    ``run-mvp --config .../codex-replay-config.json`` fallback must accept them.
    """
    config = {
        "run_date": "2026-08-05",
        "sources": [
            {
                "name": "Nonfiction.fr Critiques",
                "family": "media/curated list",
                "url": "https://example.test",
                "language": "en",
                "active": False,
                "inactive_reason": PRECOLLECTED_ARTIFACT_UNAVAILABLE,
                "access_status": "open",
                "recorded_mentions_file": "precollected-example.json",
                "recorded_mentions_sha256": "0" * 64,
                "operation_id": "a" * 64,
            }
        ],
    }

    assert validate_config(config) == []
