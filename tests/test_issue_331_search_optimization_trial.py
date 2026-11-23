"""Focused offline tests for issue #331: Search Optimization trial preservation.

Covers the finalize_codex_run propagation of ``search_optimization_trial``,
the preserve-search-optimization-run public function and CLI command, the
deterministic archive layout, byte identity, manifest digests, refusal of
all invalid inputs, staging cleanup, crash-safe destination locking, staged
re-validation of the copied review and summary, and reuse of the preserved
reviews by the existing three-run comparison CLI. No network and no model
are used.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from autoscout.__main__ import main as cli_main
from autoscout.codex_workflow import finalize_codex_run
from autoscout.search_optimization_trial import MANIFEST_ARTIFACT_TYPE
from autoscout.search_optimization_trial import MANIFEST_FILENAME
from autoscout.search_optimization_trial import MANIFEST_SCHEMA_VERSION
from autoscout.search_optimization_trial import RUN_SUMMARY_FILENAME
from autoscout.search_optimization_trial import _StableDestinationLock
from autoscout.search_optimization_trial import preserve_search_optimization_run
from autoscout.search_optimization_trial import trial_destination_lock_path

REVIEW_JSON = "search-optimization-review.json"
REVIEW_MD = "search-optimization-review.md"
OPERATION_ID = "op-2026-07-04-abcdef123456"
EVIDENCE_DIGEST = "e" * 64

_DIAGNOSTIC_CLASS_IDS = (
    "already_known_in_normal_review",
    "reject_resurfaced_without_material_change",
    "probable_duplicate_identity_fragmentation",
    "scouting_candidate_definition_conflict",
    "missing_trial_structured_data",
)


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def _cohort_entry() -> dict[str, Any]:
    return {
        "count_in_top_cutoff": 0,
        "total_eligible_in_cohort": 0,
        "rate": None,
        "evidence_label": "",
    }


def _base_review(run_date: str) -> dict[str, Any]:
    """Return a minimal schema-4 review artifact that passes domain validation."""
    return {
        "schema_version": 4,
        "artifact_type": "search-optimization-review",
        "run_identity": run_date,
        "run_date": run_date,
        "canonical_effect": "none",
        "metrics": {
            "evidence_classification": {
                "observed_interested_count": 0,
                "threshold": 5,
                "is_exploratory": True,
            },
            "eligible_recall": {
                "canonical": {"recalled_count": 0, "eligible_count": 0, "recall_rate": None},
                "shadow": {"recalled_count": 0, "eligible_count": 0, "recall_rate": None},
                "identity_sets_identical": True,
            },
            "cohort_coverage": {
                "rank_25": {
                    "canonical": {"strong_interest": _cohort_entry(), "actionable_follow_up": _cohort_entry()},
                    "shadow": {"strong_interest": _cohort_entry(), "actionable_follow_up": _cohort_entry()},
                },
                "rank_50": {
                    "canonical": {"strong_interest": _cohort_entry(), "actionable_follow_up": _cohort_entry()},
                    "shadow": {"strong_interest": _cohort_entry(), "actionable_follow_up": _cohort_entry()},
                },
            },
            "density": {
                "rank_25": {
                    "canonical": {
                        "negative": {"count_in_top_cutoff": 0, "rows_inspected": 0, "rate": None},
                        "known_workflow_item": {"count_in_top_cutoff": 0, "rows_inspected": 0, "rate": None},
                    },
                    "shadow": {
                        "negative": {"count_in_top_cutoff": 0, "rows_inspected": 0, "rate": None},
                        "known_workflow_item": {"count_in_top_cutoff": 0, "rows_inspected": 0, "rate": None},
                    },
                },
                "rank_50": {
                    "canonical": {
                        "negative": {"count_in_top_cutoff": 0, "rows_inspected": 0, "rate": None},
                        "known_workflow_item": {"count_in_top_cutoff": 0, "rows_inspected": 0, "rate": None},
                    },
                    "shadow": {
                        "negative": {"count_in_top_cutoff": 0, "rows_inspected": 0, "rate": None},
                        "known_workflow_item": {"count_in_top_cutoff": 0, "rows_inspected": 0, "rate": None},
                    },
                },
            },
            "diversity": {
                "rank_25": {
                    "canonical": {"family_names": [], "family_count": 0},
                    "shadow": {"family_names": [], "family_count": 0},
                },
                "rank_50": {
                    "canonical": {"family_names": [], "family_count": 0},
                    "shadow": {"family_names": [], "family_count": 0},
                },
            },
            "median_ranks": {
                "strong_interest": {"eligible_count": 0, "canonical": None, "shadow": None, "evidence_label": ""},
                "actionable_follow_up": {
                    "eligible_count": 0,
                    "canonical": None,
                    "shadow": None,
                    "evidence_label": "",
                },
            },
            "workload": {
                "canonical_rows_to_retain_all_interested": None,
                "shadow_rows_to_retain_all_interested": None,
                "estimated_rows_avoided": None,
                "evidence_label": "exploratory",
                "already_known_exposure": {
                    "note": "None.",
                    "canonical_ak_in_interested_window": 0,
                    "shadow_ak_in_interested_window": 0,
                    "ak_rows_difference": 0,
                },
                "discovery_quality": {
                    "note": "None.",
                    "strong_interest_coverage_delta": None,
                    "strong_interest_coverage_delta_evidence_label": "",
                    "actionable_coverage_delta": None,
                    "strong_interest_median_rank_delta": None,
                    "strong_interest_median_rank_delta_evidence_label": "",
                    "actionable_median_rank_delta": None,
                },
            },
            "warnings": [],
        },
        "examples": {
            "selection_note": "No promoted or demoted candidates.",
            "promoted_count": 0,
            "demoted_count": 0,
            "promoted": [],
            "demoted": [],
        },
        "false_negative_audit": [],
        "candidate_quality_diagnostics": {
            "classes": {
                class_id: {"label": class_id, "count": 0, "rank_effect": "None.", "findings": []}
                for class_id in _DIAGNOSTIC_CLASS_IDS
            },
        },
        "source_allocation": {
            "recommendations": [
                {
                    "source_name": "TestSource",
                    "source_url": "https://example.test/source",
                    "allocation": "insufficient data",
                }
            ],
        },
        "candidates": [],
    }


def _make_run(
    tmp_path: Path,
    run_date: str,
    *,
    operation_id: str = OPERATION_ID,
    evidence_digest: str = EVIDENCE_DIGEST,
    summary_run_date: str | None = None,
    markdown_text: str | None = None,
) -> Path:
    """Create a valid trial run directory with the three evidence files."""
    run_dir = tmp_path / f"run-{run_date}"
    run_dir.mkdir(exist_ok=True)
    _write_json(run_dir / REVIEW_JSON, _base_review(run_date))
    (run_dir / REVIEW_MD).write_text(
        markdown_text or f"# Search Optimization Review {run_date}\n",
        encoding="utf-8",
    )
    _write_json(
        run_dir / RUN_SUMMARY_FILENAME,
        {
            "run_date": summary_run_date if summary_run_date is not None else run_date,
            "operation_id": operation_id,
            "evidence_digest": evidence_digest,
        },
    )
    return run_dir


def _staging_leftovers(archive_dir: Path) -> list[str]:
    """Return staging directory names left inside the archive directory."""
    if not archive_dir.is_dir():
        return []
    return [name for name in os.listdir(archive_dir) if name.startswith(".search-optimization-trial-")]


def _run_files_snapshot(run_dir: Path) -> dict[str, bytes]:
    return {name: (run_dir / name).read_bytes() for name in sorted(os.listdir(run_dir))}


def _archive_destination(archive_dir: Path, run_date: str, operation_id: str = OPERATION_ID) -> Path:
    return archive_dir / f"{run_date}-{operation_id[:12]}"


def _lock_path(archive_dir: Path, run_date: str = "2026-07-04", operation_id: str = OPERATION_ID) -> Path:
    destination = _archive_destination(archive_dir, run_date, operation_id)
    return trial_destination_lock_path(destination)


def _summary_with(run_dir: Path, **overrides: Any) -> None:
    """Rewrite run-summary.json with the given field overrides."""
    summary = json.loads(run_dir.joinpath(RUN_SUMMARY_FILENAME).read_text(encoding="utf-8"))
    summary.update(overrides)
    _write_json(run_dir / RUN_SUMMARY_FILENAME, summary)


# ── finalize_codex_run propagation (issue #331) ───────────────────────────


def _worklist_run(tmp_path: Path) -> Path:
    run_dir = tmp_path / "codex-run"
    run_dir.mkdir()
    _write_json(
        run_dir / "codex-extraction-worklist.json",
        {
            "run_date": "2026-07-04",
            "work_items": [
                {
                    "source_name": "Test Source",
                    "source_family": "test",
                    "source_url": "https://example.test/source",
                    "capture_file": "capture.json",
                    "expected_extraction_file": "extraction.json",
                    "source_metadata": {},
                }
            ],
            "precollected_sources": [],
        },
    )
    _write_json(run_dir / "extraction.json", {"mentions": []})
    return run_dir


def test_finalize_propagates_search_optimization_trial_true(tmp_path: Path) -> None:
    run_dir = _worklist_run(tmp_path)
    live_config = tmp_path / "live.json"
    _write_json(live_config, {"run_date": "2026-07-04", "sources": [], "search_optimization_trial": True})

    assert finalize_codex_run(run_dir, live_config) == 0

    replay = json.loads((run_dir / "codex-replay-config.json").read_text(encoding="utf-8"))
    assert replay["search_optimization_trial"] is True


def test_finalize_omits_search_optimization_trial_when_missing(tmp_path: Path) -> None:
    run_dir = _worklist_run(tmp_path)
    live_config = tmp_path / "live.json"
    _write_json(live_config, {"run_date": "2026-07-04", "sources": []})

    assert finalize_codex_run(run_dir, live_config) == 0

    replay = json.loads((run_dir / "codex-replay-config.json").read_text(encoding="utf-8"))
    assert "search_optimization_trial" not in replay


# ── Valid preservation ─────────────────────────────────────────────────────


def test_preserve_valid_run_creates_deterministic_archive(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, "2026-07-04")
    archive_dir = tmp_path / "archive"

    preserved = preserve_search_optimization_run(run_dir, archive_dir)

    destination = _archive_destination(archive_dir, "2026-07-04")
    assert preserved == destination
    assert destination.is_dir()
    assert set(os.listdir(destination)) == {REVIEW_JSON, REVIEW_MD, RUN_SUMMARY_FILENAME, MANIFEST_FILENAME}


def test_preserve_copies_files_byte_for_byte(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, "2026-07-04")
    run_dir.joinpath(REVIEW_MD).write_bytes(b"# \xf0\x9f\x93\x9a bytes \x00\xff\n")
    archive_dir = tmp_path / "archive"

    preserve_search_optimization_run(run_dir, archive_dir)

    destination = _archive_destination(archive_dir, "2026-07-04")
    for name in (REVIEW_JSON, REVIEW_MD, RUN_SUMMARY_FILENAME):
        assert (destination / name).read_bytes() == (run_dir / name).read_bytes()


def test_manifest_identity_values_and_sha256_hashes(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, "2026-07-04")
    archive_dir = tmp_path / "archive"

    preserve_search_optimization_run(run_dir, archive_dir)

    manifest_path = _archive_destination(archive_dir, "2026-07-04") / MANIFEST_FILENAME
    manifest_text = manifest_path.read_text(encoding="utf-8")
    assert manifest_text.endswith("\n")
    manifest = json.loads(manifest_text)
    assert list(manifest.keys()) == sorted(manifest.keys())
    assert manifest["artifact_type"] == MANIFEST_ARTIFACT_TYPE
    assert manifest["schema_version"] == MANIFEST_SCHEMA_VERSION
    assert manifest["run_date"] == "2026-07-04"
    assert manifest["operation_id"] == OPERATION_ID
    assert manifest["evidence_digest"] == EVIDENCE_DIGEST
    assert manifest["canonical_effect"] == "none"
    assert manifest["preservation_statement"].strip()
    assert set(manifest["files"]) == {REVIEW_JSON, REVIEW_MD, RUN_SUMMARY_FILENAME}
    for name, digest in manifest["files"].items():
        assert digest == hashlib.sha256((run_dir / name).read_bytes()).hexdigest()


def test_duplicate_preserve_refuses_without_replacing_first_archive(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, "2026-07-04")
    archive_dir = tmp_path / "archive"
    destination = _archive_destination(archive_dir, "2026-07-04")

    preserve_search_optimization_run(run_dir, archive_dir)
    first_bytes = {name: (destination / name).read_bytes() for name in os.listdir(destination)}

    with pytest.raises(ValueError, match="already exists"):
        preserve_search_optimization_run(run_dir, archive_dir)

    assert {name: (destination / name).read_bytes() for name in os.listdir(destination)} == first_bytes
    assert len(os.listdir(archive_dir)) == 1


def test_preserve_refuses_archive_inside_run_dir(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, "2026-07-04")
    with pytest.raises(ValueError, match="archive_dir must not be"):
        preserve_search_optimization_run(run_dir, run_dir / "archive")
    with pytest.raises(ValueError, match="archive_dir must not be"):
        preserve_search_optimization_run(run_dir, run_dir)


def test_preserve_refuses_unsafe_operation_id(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, "2026-07-04", operation_id="../../etc/passwd")
    with pytest.raises(ValueError, match="unsafe operation_id"):
        preserve_search_optimization_run(run_dir, tmp_path / "archive")


def test_preserve_refuses_control_characters_in_operation_id(tmp_path: Path) -> None:
    for index, bad in enumerate(("op\n2026", "op\t2026", "op\x07x")):
        run_dir = _make_run(tmp_path, "2026-07-04", operation_id=bad)
        with pytest.raises(ValueError, match="unsafe operation_id"):
            preserve_search_optimization_run(run_dir, tmp_path / f"archive-{index}")


def test_preserve_refuses_non_string_operation_id(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, "2026-07-04")
    _summary_with(run_dir, operation_id=12345)
    with pytest.raises(ValueError, match="operation_id must be a string"):
        preserve_search_optimization_run(run_dir, tmp_path / "archive")


def test_preserve_refuses_non_string_evidence_digest(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, "2026-07-04")
    _summary_with(run_dir, evidence_digest=["not", "a", "string"])
    with pytest.raises(ValueError, match="evidence_digest must be a string"):
        preserve_search_optimization_run(run_dir, tmp_path / "archive")


# ── Destination reservation (concurrent duplicate prevention) ────────────


def test_reservation_refuses_concurrent_preservation(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, "2026-07-04")
    archive_dir = tmp_path / "archive"
    lock_path = _lock_path(archive_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = _StableDestinationLock.try_acquire(lock_path, create=True)
    assert holder is not None
    try:
        with pytest.raises(ValueError, match="already in progress"):
            preserve_search_optimization_run(run_dir, archive_dir)
    finally:
        holder.release()

    assert not _archive_destination(archive_dir, "2026-07-04").exists()
    assert _staging_leftovers(archive_dir) == []


def test_reservation_released_after_success(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, "2026-07-04")
    archive_dir = tmp_path / "archive"
    lock_path = _lock_path(archive_dir)

    preserve_search_optimization_run(run_dir, archive_dir)

    # The persistent lock file stays outside the archive directory, but
    # it is no longer held, so a retry proceeds immediately.
    assert lock_path.is_file()
    assert lock_path.parent != archive_dir.resolve()
    retry = _StableDestinationLock.try_acquire(lock_path, create=False)
    assert retry is not None
    retry.release()
    assert _staging_leftovers(archive_dir) == []
    assert set(os.listdir(archive_dir)) == {f"2026-07-04-{OPERATION_ID[:12]}"}


def test_reservation_released_after_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A copy failure must release the lock and clean staging."""
    import shutil

    run_dir = _make_run(tmp_path, "2026-07-04")
    archive_dir = tmp_path / "archive"
    lock_path = _lock_path(archive_dir)
    calls = {"count": 0}
    original_copyfile = shutil.copyfile

    def _flaky_copyfile(src: Path, dst: Path) -> str:
        calls["count"] += 1
        if calls["count"] >= 2:
            raise OSError("simulated copy failure")
        return original_copyfile(src, dst)

    monkeypatch.setattr(shutil, "copyfile", _flaky_copyfile)

    with pytest.raises(OSError, match="simulated copy failure"):
        preserve_search_optimization_run(run_dir, archive_dir)

    retry = _StableDestinationLock.try_acquire(lock_path, create=False)
    assert retry is not None
    retry.release()
    assert _staging_leftovers(archive_dir) == []
    assert not _archive_destination(archive_dir, "2026-07-04").exists()


def test_stale_lock_file_does_not_block_retry(tmp_path: Path) -> None:
    """A leftover lock file from a crash never blocks a retry."""
    run_dir = _make_run(tmp_path, "2026-07-04")
    archive_dir = tmp_path / "archive"
    lock_path = _lock_path(archive_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("leftover from a crashed process", encoding="utf-8")

    preserved = preserve_search_optimization_run(run_dir, archive_dir)

    assert preserved.is_dir()
    assert lock_path.is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX advisory locks only")
def test_crashed_holder_does_not_block_retry(tmp_path: Path) -> None:
    """A process crash releases the advisory lock, so a retry succeeds."""
    import subprocess
    import sys

    run_dir = _make_run(tmp_path, "2026-07-04")
    archive_dir = tmp_path / "archive"
    lock_path = _lock_path(archive_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    crash_code = (
        "import fcntl, os\n"
        f"fd = os.open({str(lock_path)!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "os._exit(1)\n"
    )
    crashed = subprocess.run([sys.executable, "-c", crash_code], capture_output=True)
    assert crashed.returncode == 1

    preserved = preserve_search_optimization_run(run_dir, archive_dir)

    assert preserved.is_dir()
    assert lock_path.is_file()


def test_reservation_recheck_refuses_already_published_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A destination published before lock acquisition is never overwritten.

    Simulates the race window: the pre-check sees no destination, then
    another caller publishes, then this caller acquires the lock. The
    post-acquisition re-check must refuse instead of publishing over
    the existing archive.
    """
    import autoscout.search_optimization_trial as trial_module

    run_dir = _make_run(tmp_path, "2026-07-04")
    archive_dir = tmp_path / "archive"
    destination = _archive_destination(archive_dir, "2026-07-04")
    original_acquire = trial_module._acquire_destination_lock

    def _publish_then_acquire(lock_path: Path, destination_name: str) -> Any:
        destination.mkdir(parents=True, exist_ok=True)
        return original_acquire(lock_path, destination_name)

    monkeypatch.setattr(trial_module, "_acquire_destination_lock", _publish_then_acquire)

    with pytest.raises(ValueError, match="already exists"):
        preserve_search_optimization_run(run_dir, archive_dir)

    retry = _StableDestinationLock.try_acquire(_lock_path(archive_dir), create=False)
    assert retry is not None
    retry.release()
    assert _staging_leftovers(archive_dir) == []
    assert destination.is_dir()
    assert os.listdir(destination) == []


# ── Refused inputs ─────────────────────────────────────────────────────────


def test_preserve_missing_each_required_file(tmp_path: Path) -> None:
    for missing in (REVIEW_JSON, REVIEW_MD, RUN_SUMMARY_FILENAME):
        run_dir = _make_run(tmp_path, "2026-07-04")
        run_dir.joinpath(missing).unlink()
        archive_dir = tmp_path / f"archive-{missing}"
        with pytest.raises(ValueError, match="Missing required"):
            preserve_search_optimization_run(run_dir, archive_dir)
        assert _staging_leftovers(archive_dir) == []


def test_preserve_malformed_review_json(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, "2026-07-04")
    run_dir.joinpath(REVIEW_JSON).write_text("{not json", encoding="utf-8")
    archive_dir = tmp_path / "archive"

    with pytest.raises(ValueError, match="Malformed"):
        preserve_search_optimization_run(run_dir, archive_dir)
    assert _staging_leftovers(archive_dir) == []


def test_preserve_malformed_summary_json(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, "2026-07-04")
    run_dir.joinpath(RUN_SUMMARY_FILENAME).write_text("[1, 2", encoding="utf-8")
    archive_dir = tmp_path / "archive"

    with pytest.raises(ValueError, match="Malformed"):
        preserve_search_optimization_run(run_dir, archive_dir)
    assert _staging_leftovers(archive_dir) == []


def test_preserve_invalid_review_schema(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, "2026-07-04")
    review = json.loads(run_dir.joinpath(REVIEW_JSON).read_text(encoding="utf-8"))
    review["artifact_type"] = "not-a-review"
    _write_json(run_dir / REVIEW_JSON, review)
    archive_dir = tmp_path / "archive"

    with pytest.raises(ValueError, match="Invalid search-optimization-review.json"):
        preserve_search_optimization_run(run_dir, archive_dir)
    assert _staging_leftovers(archive_dir) == []


def test_preserve_non_object_summary(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, "2026-07-04")
    _write_json(run_dir / RUN_SUMMARY_FILENAME, ["not", "an", "object"])
    archive_dir = tmp_path / "archive"

    with pytest.raises(ValueError, match="must be a JSON object"):
        preserve_search_optimization_run(run_dir, archive_dir)


def test_preserve_blank_operation_id(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, "2026-07-04", operation_id="   ")
    with pytest.raises(ValueError, match="blank operation_id"):
        preserve_search_optimization_run(run_dir, tmp_path / "archive")


def test_preserve_blank_evidence_digest(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, "2026-07-04", evidence_digest="")
    with pytest.raises(ValueError, match="blank evidence_digest"):
        preserve_search_optimization_run(run_dir, tmp_path / "archive")


def test_preserve_blank_and_malformed_summary_run_date(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, "2026-07-04", summary_run_date="")
    with pytest.raises(ValueError, match="blank run_date"):
        preserve_search_optimization_run(run_dir, tmp_path / "archive")

    run_dir = _make_run(tmp_path, "2026-07-05", summary_run_date="not-a-date")
    with pytest.raises(ValueError, match="malformed run_date"):
        preserve_search_optimization_run(run_dir, tmp_path / "archive")


def test_preserve_review_summary_run_date_mismatch(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, "2026-07-04", summary_run_date="2026-07-11")
    with pytest.raises(ValueError, match="does not match review run_date"):
        preserve_search_optimization_run(run_dir, tmp_path / "archive")


def test_preserve_refuses_existing_destination_symlink(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, "2026-07-04")
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    destination = _archive_destination(archive_dir, "2026-07-04")
    destination.symlink_to(tmp_path / "elsewhere", target_is_directory=True)

    with pytest.raises(ValueError, match="already exists"):
        preserve_search_optimization_run(run_dir, archive_dir)


def test_preserve_refuses_dangling_destination_symlink(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, "2026-07-04")
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    destination = _archive_destination(archive_dir, "2026-07-04")
    destination.symlink_to(tmp_path / "does-not-exist", target_is_directory=True)

    with pytest.raises(ValueError, match="already exists"):
        preserve_search_optimization_run(run_dir, archive_dir)


# ── Source immutability and staging cleanup ───────────────────────────────


def test_source_run_remains_byte_identical(tmp_path: Path) -> None:
    run_dir = _make_run(tmp_path, "2026-07-04")
    before = _run_files_snapshot(run_dir)

    preserve_search_optimization_run(run_dir, tmp_path / "archive")

    assert _run_files_snapshot(run_dir) == before


def test_failed_preserve_leaves_no_staging_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure during staging must clean the staging directory."""
    import shutil

    run_dir = _make_run(tmp_path, "2026-07-04")
    archive_dir = tmp_path / "archive"
    calls = {"count": 0}
    original_copyfile = shutil.copyfile

    def _flaky_copyfile(src: Path, dst: Path) -> str:
        calls["count"] += 1
        if calls["count"] >= 2:
            raise OSError("simulated copy failure")
        return original_copyfile(src, dst)

    monkeypatch.setattr(shutil, "copyfile", _flaky_copyfile)

    with pytest.raises(OSError, match="simulated copy failure"):
        preserve_search_optimization_run(run_dir, archive_dir)

    assert _staging_leftovers(archive_dir) == []
    assert not _archive_destination(archive_dir, "2026-07-04").exists()


# ── Reuse by the existing three-run comparison CLI ────────────────────────


def test_manifest_digest_describes_published_bytes_not_mutated_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source change after copy must not change the published digest."""
    import shutil

    run_dir = _make_run(tmp_path, "2026-07-04")
    archive_dir = tmp_path / "archive"
    original_copyfile = shutil.copyfile

    def _copy_then_mutate_source(src: Path, dst: Path) -> str:
        result = original_copyfile(src, dst)
        if src.name == REVIEW_MD:
            src.write_bytes(b"# mutated after copy\n")
        return result

    monkeypatch.setattr(shutil, "copyfile", _copy_then_mutate_source)

    preserve_search_optimization_run(run_dir, archive_dir)

    destination = _archive_destination(archive_dir, "2026-07-04")
    manifest = json.loads((destination / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    published_bytes = (destination / REVIEW_MD).read_bytes()
    assert manifest["files"][REVIEW_MD] == hashlib.sha256(published_bytes).hexdigest()
    assert manifest["files"][REVIEW_MD] != hashlib.sha256((run_dir / REVIEW_MD).read_bytes()).hexdigest()
    assert published_bytes == b"# Search Optimization Review 2026-07-04\n"
    assert _staging_leftovers(archive_dir) == []


def test_staged_review_mutation_during_copy_refuses_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A source review mutated during the copy must fail before publish."""
    import shutil

    run_dir = _make_run(tmp_path, "2026-07-04")
    archive_dir = tmp_path / "archive"
    original_copyfile = shutil.copyfile

    def _corrupt_staged_review(src: Path, dst: Path) -> str:
        if src.name == REVIEW_JSON:
            dst.write_text("{not json", encoding="utf-8")
            return str(dst)
        return original_copyfile(src, dst)

    monkeypatch.setattr(shutil, "copyfile", _corrupt_staged_review)

    with pytest.raises(ValueError, match="Malformed staged search-optimization-review"):
        preserve_search_optimization_run(run_dir, archive_dir)

    assert not _archive_destination(archive_dir, "2026-07-04").exists()
    assert _staging_leftovers(archive_dir) == []


def test_staged_review_schema_mutation_during_copy_refuses_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A staged review that fails domain validation must not publish."""
    import shutil

    run_dir = _make_run(tmp_path, "2026-07-04")
    archive_dir = tmp_path / "archive"
    original_copyfile = shutil.copyfile

    def _swap_staged_review(src: Path, dst: Path) -> str:
        if src.name == REVIEW_JSON:
            review = json.loads(src.read_text(encoding="utf-8"))
            review["artifact_type"] = "not-a-review"
            _write_json(dst, review)
            return str(dst)
        return original_copyfile(src, dst)

    monkeypatch.setattr(shutil, "copyfile", _swap_staged_review)

    with pytest.raises(ValueError, match="Invalid staged search-optimization-review.json"):
        preserve_search_optimization_run(run_dir, archive_dir)

    assert not _archive_destination(archive_dir, "2026-07-04").exists()
    assert _staging_leftovers(archive_dir) == []


@pytest.mark.parametrize(
    ("field", "staged_value", "error_fragment"),
    [
        ("run_date", "2026-07-11", "does not match the preserved run date"),
        ("operation_id", "op-changed-000000000000", "does not match the preserved operation_id"),
        ("evidence_digest", "f" * 64, "does not match the preserved evidence_digest"),
    ],
)
def test_staged_identity_change_during_copy_refuses_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    staged_value: str,
    error_fragment: str,
) -> None:
    """A staged identity change must fail before publish."""
    import shutil

    run_dir = _make_run(tmp_path, "2026-07-04")
    archive_dir = tmp_path / "archive"
    original_copyfile = shutil.copyfile

    def _swap_staged_identity(src: Path, dst: Path) -> str:
        if src.name == REVIEW_JSON and field == "run_date":
            review = json.loads(src.read_text(encoding="utf-8"))
            review["run_identity"] = staged_value
            review["run_date"] = staged_value
            _write_json(dst, review)
            return str(dst)
        if src.name == RUN_SUMMARY_FILENAME:
            summary = json.loads(src.read_text(encoding="utf-8"))
            summary[field] = staged_value
            _write_json(dst, summary)
            return str(dst)
        return original_copyfile(src, dst)

    monkeypatch.setattr(shutil, "copyfile", _swap_staged_identity)

    with pytest.raises(ValueError, match=error_fragment):
        preserve_search_optimization_run(run_dir, archive_dir)

    assert not _archive_destination(archive_dir, "2026-07-04").exists()
    assert _staging_leftovers(archive_dir) == []


def test_staged_validation_uses_public_domain_facade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The staged review is re-validated through the public domain facade."""
    import autoscout.search_optimization_trial as trial_module
    from autoscout.search_optimization import SearchOptimizationReview as PublicReview

    run_dir = _make_run(tmp_path, "2026-07-04")
    archive_dir = tmp_path / "archive"
    calls = {"count": 0}

    class _SpyReview(PublicReview):
        @classmethod
        def from_mapping(cls, mapping: Any) -> _SpyReview:
            calls["count"] += 1
            return PublicReview(mapping)

    monkeypatch.setattr(trial_module, "SearchOptimizationReview", _SpyReview)

    preserve_search_optimization_run(run_dir, archive_dir)

    # One call validates the source review, one call validates the staged
    # copy before the atomic publish.
    assert calls["count"] == 2
    assert _staging_leftovers(archive_dir) == []


def test_preserved_reviews_feed_three_run_comparison_cli(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    preserved_paths: list[Path] = []
    for run_date in ("2026-08-01", "2026-08-08", "2026-08-15"):
        run_dir = _make_run(tmp_path, run_date)
        preserved = preserve_search_optimization_run(run_dir, archive_dir)
        preserved_paths.append(preserved / REVIEW_JSON)

    out = tmp_path / "comparison"
    exit_code = cli_main(
        [
            "compare-search-optimization",
            "--review",
            str(preserved_paths[0]),
            "--review",
            str(preserved_paths[1]),
            "--review",
            str(preserved_paths[2]),
            "--output-dir",
            str(out),
        ]
    )

    assert exit_code == 0
    assert (out / "search-optimization-comparison.json").is_file()
    assert (out / "search-optimization-comparison.md").is_file()


# ── CLI command seam ──────────────────────────────────────────────────────


def test_cli_preserve_success_prints_preserved_dir(tmp_path: Path, capsys) -> None:
    run_dir = _make_run(tmp_path, "2026-07-04")
    archive_dir = tmp_path / "archive"

    exit_code = cli_main(
        ["preserve-search-optimization-run", "--run-dir", str(run_dir), "--archive-dir", str(archive_dir)]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert f"preserved_dir={archive_dir / ('2026-07-04-' + OPERATION_ID[:12])}" in captured.out


def test_cli_preserve_failure_returns_one_concise_error(tmp_path: Path, capsys) -> None:
    run_dir = _make_run(tmp_path, "2026-07-04")
    run_dir.joinpath(REVIEW_JSON).unlink()
    archive_dir = tmp_path / "archive"

    exit_code = cli_main(
        ["preserve-search-optimization-run", "--run-dir", str(run_dir), "--archive-dir", str(archive_dir)]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out.startswith("error=")
    assert "Traceback" not in captured.out
    assert _staging_leftovers(archive_dir) == []


# ── Offline boundary ──────────────────────────────────────────────────────


def test_preservation_module_has_no_network_or_model_imports() -> None:
    """The preservation module must stay offline and model-free."""
    root = Path(__file__).resolve().parents[1]
    source = (root / "autoscout" / "search_optimization_trial.py").read_text(encoding="utf-8")
    for forbidden in ("requests", "urllib", "httpx", "http.client", "socket", "openai", "anthropic"):
        assert forbidden not in source
