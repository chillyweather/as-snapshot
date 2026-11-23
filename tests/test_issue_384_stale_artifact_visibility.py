"""Issue #384 — a reused artifact is visible, and never passes as fresh.

On the 2026-08-05 live weekly run, 21 of 28 sources were served from artifacts
produced by an earlier operation, some 18 days old. ``finalize_codex_run``
judged readiness by ``extraction_path.exists()`` alone, so an artifact of any
age satisfied it. Every reused source was reported as fully collected, and the
editor received last week's numbers as this week's result: 0 new, 6 updated,
101 previously seen, and 433 of 433 Russian descriptions "complete".

Reuse itself stays permitted, by operator decision. What changes is that it is
bound, labelled, and counted, so a source can no longer stop extracting for
weeks while its candidates keep appearing.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from autoscout.codex_workflow import finalize_codex_run
from autoscout.reporting import major_warnings
from autoscout.run_health import codex_workflow_health
from autoscout.russian_workflow import completion_message
from autoscout.source_collection import SourceCollection

OPERATION_ID = "current-operation-f4245698"
EARLIER_OPERATION_ID = "earlier-operation-8dcb107a"
SOURCE_NAME = "PW Best Books 2025"
SOURCE_URL = "https://example.test/pw-best-books-2025"
ARTIFACT_FILE = "pw-best-books-2025-codex-extraction.json"
CAPTURE_FILE = "codex-capture-pw-best-books-2025.json"


def _run_dir(tmp_path: Path) -> tuple[Path, str]:
    capture_path = tmp_path / CAPTURE_FILE
    capture_path.write_text(
        json.dumps({"source_name": SOURCE_NAME, "source_url": SOURCE_URL, "html": "<ul></ul>"}),
        encoding="utf-8",
    )
    digest = hashlib.sha256(capture_path.read_bytes()).hexdigest()
    (tmp_path / "codex-extraction-worklist.json").write_text(
        json.dumps(
            {
                "run_date": "2026-08-05",
                "work_items": [
                    {
                        "source_name": SOURCE_NAME,
                        "source_url": SOURCE_URL,
                        "source_family": "media/curated list",
                        "operation_id": OPERATION_ID,
                        "capture_sha256": digest,
                        "capture_file": CAPTURE_FILE,
                        "expected_extraction_file": ARTIFACT_FILE,
                    }
                ],
                "precollected_sources": [],
            }
        ),
        encoding="utf-8",
    )
    return tmp_path, digest


def _write_artifact(tmp_path: Path, *, operation_id: str, capture_sha256: str) -> None:
    (tmp_path / ARTIFACT_FILE).write_text(
        json.dumps(
            {
                "source_name": SOURCE_NAME,
                "source_url": SOURCE_URL,
                "operation_id": operation_id,
                "capture_sha256": capture_sha256,
                "entries": [],
            }
        ),
        encoding="utf-8",
    )


def _replay_source(tmp_path: Path) -> dict:
    config = json.loads((tmp_path / "codex-replay-config.json").read_text(encoding="utf-8"))
    return config["sources"][0]


# ── Binding readiness to the current operation ────────────────────────────


def test_artifact_bound_to_this_operation_is_fresh(tmp_path: Path, capsys) -> None:
    run_dir, digest = _run_dir(tmp_path)
    _write_artifact(run_dir, operation_id=OPERATION_ID, capture_sha256=digest)

    assert finalize_codex_run(run_dir) == 0

    source = _replay_source(run_dir)
    assert source["artifact_freshness"] == "fresh"
    assert source["active"] is True
    out = capsys.readouterr().out
    assert "ready_sources=1" in out
    assert "reused_sources=0" in out


def test_artifact_from_an_earlier_operation_is_reused(tmp_path: Path, capsys) -> None:
    run_dir, digest = _run_dir(tmp_path)
    _write_artifact(run_dir, operation_id=EARLIER_OPERATION_ID, capture_sha256=digest)

    assert finalize_codex_run(run_dir) == 0

    source = _replay_source(run_dir)
    assert source["artifact_freshness"] == "reused"
    # Reuse stays permitted: the source still feeds the pipeline.
    assert source["active"] is True
    assert source["extraction_response_file"] == ARTIFACT_FILE
    out = capsys.readouterr().out
    assert "reused_sources=1" in out
    assert "ready_sources=0" in out


def test_artifact_bound_to_a_different_capture_is_reused(tmp_path: Path) -> None:
    """The same operation over a stale capture is still not this week's page."""
    run_dir, _ = _run_dir(tmp_path)
    _write_artifact(run_dir, operation_id=OPERATION_ID, capture_sha256="a-different-capture-digest")

    assert finalize_codex_run(run_dir) == 0
    assert _replay_source(run_dir)["artifact_freshness"] == "reused"


def test_artifact_without_identity_fields_is_reused_not_fresh(tmp_path: Path) -> None:
    """Unprovable freshness must never be reported as freshness."""
    run_dir, _ = _run_dir(tmp_path)
    (run_dir / ARTIFACT_FILE).write_text(json.dumps({"entries": []}), encoding="utf-8")

    assert finalize_codex_run(run_dir) == 0
    assert _replay_source(run_dir)["artifact_freshness"] == "reused"


def test_unreadable_artifact_is_reused_not_fresh(tmp_path: Path) -> None:
    run_dir, _ = _run_dir(tmp_path)
    (run_dir / ARTIFACT_FILE).write_text("{ not json", encoding="utf-8")

    assert finalize_codex_run(run_dir) == 0
    assert _replay_source(run_dir)["artifact_freshness"] == "reused"


def test_work_item_claiming_no_identity_is_unbound_not_reused(tmp_path: Path, capsys) -> None:
    """A standalone prepare stamps no identity, so there is nothing to bind.

    Only the weekly workflow stamps operation_id and capture_sha256 onto its
    work items. Reporting the standalone operator path as stale would be a
    false alarm, and reporting it as fresh would be an unearned claim.
    """
    run_dir, digest = _run_dir(tmp_path)
    worklist_path = run_dir / "codex-extraction-worklist.json"
    worklist = json.loads(worklist_path.read_text(encoding="utf-8"))
    for field in ("operation_id", "capture_sha256"):
        worklist["work_items"][0].pop(field)
    worklist_path.write_text(json.dumps(worklist), encoding="utf-8")
    _write_artifact(run_dir, operation_id="", capture_sha256="")

    assert finalize_codex_run(run_dir) == 0

    assert _replay_source(run_dir)["artifact_freshness"] == "unbound"
    out = capsys.readouterr().out
    assert "ready_sources=1" in out
    assert "reused_sources=0" in out


def test_partially_claimed_identity_still_binds_on_what_is_claimed(tmp_path: Path) -> None:
    """A claimed field is checked even when its companion is absent."""
    run_dir, digest = _run_dir(tmp_path)
    worklist_path = run_dir / "codex-extraction-worklist.json"
    worklist = json.loads(worklist_path.read_text(encoding="utf-8"))
    worklist["work_items"][0].pop("operation_id")
    worklist_path.write_text(json.dumps(worklist), encoding="utf-8")
    _write_artifact(run_dir, operation_id=EARLIER_OPERATION_ID, capture_sha256="a-stale-capture-digest")

    assert finalize_codex_run(run_dir) == 0
    assert _replay_source(run_dir)["artifact_freshness"] == "reused"


def test_a_fully_reused_run_is_not_reported_as_having_no_artifacts(tmp_path: Path, capsys) -> None:
    run_dir, digest = _run_dir(tmp_path)
    _write_artifact(run_dir, operation_id=EARLIER_OPERATION_ID, capture_sha256=digest)

    finalize_codex_run(run_dir)

    assert "No extraction artifacts found" not in capsys.readouterr().out


# ── Reporting reuse distinctly ────────────────────────────────────────────


def _collectable_source(**overrides: object) -> dict:
    """A source that really produces a mention, so status reflects collection."""
    return {
        "name": SOURCE_NAME,
        "family": "media/curated list",
        "url": SOURCE_URL,
        "access_status": "open",
        "fixture_candidates": [
            {
                "title": "A Reused Golden Book",
                "primary_authors": ["Alex Fixture"],
                "source_url": SOURCE_URL,
                "signal_strength": "major media list",
                "evidence_note": "Listed on a configured source page.",
            }
        ],
        **overrides,
    }


def test_reused_source_run_is_reported_distinctly(tmp_path: Path) -> None:
    result = SourceCollection(config_path=tmp_path / "config.json", output_dir=tmp_path, run_date="2026-08-05").collect(
        [_collectable_source(artifact_freshness="reused")]
    )

    run = result.source_runs[0]
    assert run["status"] == "reused"
    assert "earlier operation" in run["reason"]
    # Reuse is not a failure: the source produced mentions.
    assert result.failed_source_count == 0


def test_freshly_collected_source_keeps_the_collected_status(tmp_path: Path) -> None:
    result = SourceCollection(config_path=tmp_path / "config.json", output_dir=tmp_path, run_date="2026-08-05").collect(
        [_collectable_source(artifact_freshness="fresh")]
    )

    assert result.source_runs[0]["status"] == "collected"
    assert result.source_runs[0]["reason"] == ""


def test_source_without_freshness_label_keeps_the_collected_status(tmp_path: Path) -> None:
    """Paths that never produce a Codex artifact are unaffected."""
    result = SourceCollection(config_path=tmp_path / "config.json", output_dir=tmp_path, run_date="2026-08-05").collect(
        [_collectable_source()]
    )

    assert result.source_runs[0]["status"] == "collected"


def test_reuse_downgrades_codex_workflow_health(tmp_path: Path) -> None:
    runs = [
        {"source_name": "fresh", "status": "collected", "collection_mode": "codex-capture"},
        {"source_name": SOURCE_NAME, "status": "reused", "collection_mode": "codex-capture"},
    ]

    assert codex_workflow_health(runs, {}) == "stale-evidence"


def test_a_failure_still_outranks_reuse(tmp_path: Path) -> None:
    runs = [
        {"source_name": SOURCE_NAME, "status": "reused", "collection_mode": "codex-capture"},
        {"source_name": "broken", "status": "failed", "collection_mode": "codex-capture"},
    ]

    assert codex_workflow_health(runs, {}) == "failed"


def test_an_all_fresh_run_stays_healthy() -> None:
    runs = [{"source_name": "fresh", "status": "collected", "collection_mode": "codex-capture"}]

    assert codex_workflow_health(runs, {}) == "healthy"


def test_a_skip_masks_stale_evidence_in_the_health_scalar() -> None:
    """One scalar cannot carry two unordered conditions, so this is recorded.

    A skipped source outranks reuse, so a run with both reports ``degraded``
    and the reuse never reaches this value. That is only acceptable because
    reuse is reported unconditionally elsewhere, which the next test pins.
    """
    runs = [
        {"source_name": "reused", "status": "reused", "collection_mode": "codex-capture"},
        {"source_name": "skipped", "status": "skipped", "collection_mode": "codex-capture"},
    ]

    assert codex_workflow_health(runs, {}) == "degraded"


def test_reuse_is_reported_even_when_the_health_scalar_masks_it() -> None:
    """The guarantee that makes the masking above tolerable."""
    warnings = major_warnings(
        skipped_source_count=1,
        failed_source_count=0,
        ignored_source_count=0,
        editor_facing_count=107,
        editor_row_target=100,
        reused_source_count=21,
        collecting_source_count=28,
    )

    assert "21 of 28" in warnings[0]


def test_reuse_share_is_the_first_major_warning() -> None:
    warnings = major_warnings(
        skipped_source_count=0,
        failed_source_count=7,
        ignored_source_count=0,
        editor_facing_count=107,
        editor_row_target=100,
        reused_source_count=21,
        collecting_source_count=28,
    )

    assert "21 of 28" in warnings[0]
    assert "not all from this run" in warnings[0]


def test_no_reuse_adds_no_warning() -> None:
    warnings = major_warnings(
        skipped_source_count=0,
        failed_source_count=0,
        ignored_source_count=0,
        editor_facing_count=107,
        editor_row_target=100,
    )

    assert not any("earlier run" in warning for warning in warnings)


# ── Telling the editor ────────────────────────────────────────────────────


def test_editor_summary_qualifies_the_counts_before_stating_them() -> None:
    """The 2026-08-05 summary, as the editor should have received it."""
    message = completion_message(
        outcome="completed/attention-required",
        new_count=0,
        updated_count=6,
        previously_seen=101,
        description_coverage={"validated_count": 433, "fallback_count": 0, "total_candidates": 433},
        reused_source_count=21,
        collecting_source_count=28,
    )

    lines = message.splitlines()
    assert "21 из 28" in lines[1]
    assert "предыдущего запуска" in lines[1]
    # The qualification must precede the counts it qualifies, or the editor
    # reads "Новых книг: 0" as this week's finding.
    assert lines.index("Новых книг: 0.") > 1
    assert any("не только к этой неделе" in line for line in lines)


def test_editor_summary_is_unchanged_when_nothing_was_reused() -> None:
    message = completion_message(
        outcome="completed/no-attention",
        new_count=3,
        updated_count=1,
    )

    assert "предыдущего запуска" not in message
    assert message.splitlines()[1] == "Новых книг: 3."
