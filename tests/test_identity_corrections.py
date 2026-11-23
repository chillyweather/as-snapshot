from __future__ import annotations

import json
from pathlib import Path

import pytest
from state_store_support import read_state
from state_store_support import seed_state

from autoscout.__main__ import main
from autoscout.identity_corrections import apply_identity_correction
from autoscout.identity_corrections import plan_identity_correction


def _write(path: Path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def _correction():
    return {
        "source_identity_keys": ["a short|wrong", "long title|wrong"],
        "canonical": {
            "identity_key": "long title|correct",
            "title": "Long Title",
            "primary_authors": ["Correct Author"],
        },
    }


def test_identity_correction_merges_history_and_rewrites_state_references(tmp_path):
    seed_state(
        tmp_path,
        candidate_history=[
            {
                "identity_key": "a short|wrong",
                "last_seen_date": "2026-07-01",
                "source_evidence": [{"source_url": "a", "signal_strength": "minor mention"}],
                "independent_signal_families": ["media"],
                "independent_signal_count": 1,
            },
            {
                "identity_key": "long title|wrong",
                "first_seen_date": "2026-06-01",
                "last_seen_date": "2026-06-10",
                "editor_status": "interested",
                "editor_note": "Keep",
                "source_evidence": [{"source_url": "b", "signal_strength": "winner"}],
                "independent_signal_families": ["award"],
                "independent_signal_count": 1,
            },
        ],
    )
    seed_state(tmp_path, candidate_visibility=[{"identity_key": "a short|wrong", "visibility": "hidden"}])
    seed_state(tmp_path, pending_editor_rows=[{"identity_key": "long title|wrong", "Editor Note": "Pending"}])
    seed_state(
        tmp_path,
        latest_scouting_candidates=[
            {"identity_key": "a short|wrong", "source_evidence": [{"source_url": "a"}]},
            {"identity_key": "long title|wrong", "source_evidence": [{"source_url": "b"}]},
        ],
    )

    report = apply_identity_correction(tmp_path, _correction())

    history = read_state(tmp_path, "candidate_history")
    assert len(history) == 1
    record = history[0]
    assert record["identity_key"] == "long title|correct"
    assert record["title"] == "Long Title"
    assert record["primary_authors"] == ["Correct Author"]
    assert record["editor_status"] == "interested"
    assert record["editor_note"] == "Keep"
    assert record["first_seen_date"] == "2026-06-01"
    assert record["last_seen_date"] == "2026-07-01"
    assert record["source_evidence"] == [
        {"source_url": "a", "signal_strength": "minor mention"},
        {"source_url": "b", "signal_strength": "winner"},
    ]
    assert record["independent_signal_families"] == ["media", "award"]
    assert record["independent_signal_count"] == 2
    assert record["primary_signal"] == {"source_url": "b", "signal_strength": "winner"}
    assert record["primary_signal_strength"] == "winner"
    assert read_state(tmp_path, "candidate_visibility")[0]["identity_key"] == "long title|correct"
    assert read_state(tmp_path, "pending_editor_rows")[0]["identity_key"] == "long title|correct"
    latest = read_state(tmp_path, "latest_scouting_candidates")
    assert len(latest) == 1
    assert latest[0]["identity_key"] == "long title|correct"
    assert latest[0]["source_evidence"] == [{"source_url": "a"}, {"source_url": "b"}]
    # Issue #559 widened the preview contract: the dry run must show the
    # canonical identity, every merged source key, preserved editor state, the
    # canonical description, the explicit description resolution, and the
    # durable Candidate IDs the merge retires. The exact-shape assertion is
    # kept as a tripwire on that whole contract.
    assert report == {
        "canonical_identity_key": "long title|correct",
        "canonical_title": "Long Title",
        "canonical_authors": ["Correct Author"],
        "source_identity_keys": ["a short|wrong", "long title|wrong"],
        "merged_history_records": 2,
        "rewritten_references": 4,
        "preserved_editor_status": "interested",
        "preserved_editor_note": "Keep",
        "content_description": "",
        "description_resolution": None,
        "survivor_candidate_id": "",
        "retired_candidate_ids": [],
    }


def test_identity_correction_merges_equivalent_description_provenance(tmp_path):
    description = "Одинаковое русское описание."
    seed_state(
        tmp_path,
        candidate_history=[
            {
                "identity_key": "a short|wrong",
                "content_description": description,
                "description_provenance": {
                    "identity_key": "a short|wrong",
                    "content_description": description,
                    "provenance": "backfilled",
                    "applied_date": "2026-07-12",
                    "evidence_basis": ["https://example.test/a"],
                },
            },
            {
                "identity_key": "long title|wrong",
                "content_description": description,
                "description_provenance": {
                    "identity_key": "long title|wrong",
                    "content_description": description,
                    "provenance": "backfilled",
                    "applied_date": "2026-07-13",
                    "evidence_basis": ["https://example.test/b"],
                },
            },
        ],
    )

    apply_identity_correction(tmp_path, _correction())

    record = read_state(tmp_path, "candidate_history")[0]
    provenance = record["description_provenance"]
    assert provenance["identity_key"] == "long title|correct"
    assert provenance["applied_date"] == "2026-07-12"
    assert provenance["evidence_basis"] == ["https://example.test/a", "https://example.test/b"]


def test_identity_correction_dry_run_does_not_write(tmp_path):
    original = [{"identity_key": "a short|wrong"}, {"identity_key": "long title|wrong"}]
    seed_state(tmp_path, candidate_history=original)

    report = apply_identity_correction(tmp_path, _correction(), dry_run=True)

    assert read_state(tmp_path, "candidate_history") == original
    assert report["merged_history_records"] == 2


def test_identity_correction_cli_applies_artifact(tmp_path, capsys):
    seed_state(tmp_path, candidate_history=[{"identity_key": "a short|wrong"}, {"identity_key": "long title|wrong"}])
    correction_path = tmp_path / "correction.json"
    _write(correction_path, _correction())

    result = main(
        [
            "merge-candidate-identity",
            "--state-dir",
            str(tmp_path),
            "--correction-file",
            str(correction_path),
        ]
    )

    assert result == 0
    assert "identity_correction=applied" in capsys.readouterr().out
    history = read_state(tmp_path, "candidate_history")
    assert [record["identity_key"] for record in history] == ["long title|correct"]


def test_identity_correction_rejects_conflicting_editor_state(tmp_path):
    seed_state(
        tmp_path,
        candidate_history=[
            {"identity_key": "a short|wrong", "editor_status": "interested"},
            {"identity_key": "long title|wrong", "editor_status": "reject"},
        ],
    )

    with pytest.raises(ValueError, match="conflicting editor_status"):
        plan_identity_correction(tmp_path, _correction())


def test_identity_merge_preserves_earliest_first_seen_date(tmp_path):
    """Merging two history records keeps the earliest first_seen_date."""
    seed_state(
        tmp_path,
        candidate_history=[
            {
                "identity_key": "a short|wrong",
                "first_seen_date": "2026-04-01",
                "last_seen_date": "2026-05-01",
            },
            {
                "identity_key": "long title|wrong",
                "first_seen_date": "2026-06-01",
                "last_seen_date": "2026-07-01",
            },
        ],
    )

    apply_identity_correction(tmp_path, _correction())

    record = read_state(tmp_path, "candidate_history")[0]
    assert record["first_seen_date"] == "2026-04-01"


def test_identity_merge_preserves_latest_last_material_change_date(tmp_path):
    """Merging two history records keeps the latest last_material_change_date."""
    seed_state(
        tmp_path,
        candidate_history=[
            {
                "identity_key": "a short|wrong",
                "first_seen_date": "2026-04-01",
                "last_seen_date": "2026-05-01",
                "last_material_change_date": "2026-04-15",
            },
            {
                "identity_key": "long title|wrong",
                "first_seen_date": "2026-06-01",
                "last_seen_date": "2026-07-01",
                "last_material_change_date": "2026-06-20",
            },
        ],
    )

    apply_identity_correction(tmp_path, _correction())

    record = read_state(tmp_path, "candidate_history")[0]
    assert record["last_material_change_date"] == "2026-06-20"
