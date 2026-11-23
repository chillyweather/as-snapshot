"""The run, report and review paths read and write canonical state through the State Store (#1069)."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest
from state_store_support import fail_database_commits
from state_store_support import read_state
from state_store_support import seed_state

from autoscout import pipeline
from autoscout.candidate_identity import candidate_id_for
from autoscout.review_rendering import render_review
from autoscout.state_store import json_backend
from autoscout.state_store import open_state_store

RUN_DATE = "2026-11-13"


def _record(title: str, **fields: Any) -> dict[str, Any]:
    identity_key = f"{title.lower()}|ann author"
    return {
        "identity_key": identity_key,
        "candidate_id": candidate_id_for(identity_key),
        "title": title,
        "primary_authors": ["Ann Author"],
        "priority_label": "C",
        "independent_signal_count": 1,
        "russian_edition_status": "not found in Russian",
        "visible_risk_status": "no visible risk",
        "first_seen_date": "2026-10-01",
        "last_seen_date": "2026-10-01",
        "content_description": "Книга о том, как устроена повседневная жизнь небольшого города.",
        "source_evidence": [
            {
                "source_name": "Active Prize List",
                "source_family": "media/curated list",
                "source_url": "https://example.test/active-prize-list",
                "evidence_note": "Listed by the source.",
            }
        ],
        **fields,
    }


def _write(directory: Path, name: str, value: Any) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_the_review_honours_an_empty_candidate_history_over_the_runs_copy(tmp_path):
    # Review finding on #1069 round 1: an emptied collection is not a missing one.
    output_dir, state_dir = tmp_path / "run", tmp_path / "state"
    _write(output_dir, "scouting-candidates.json", [])
    _write(output_dir, "candidate-history.json", [_record("Stale Book")])
    _write(output_dir, "run-summary.json", {"run_date": RUN_DATE, "editor_facing_count": 0})
    seed_state(state_dir, candidate_history=[])

    with redirect_stdout(io.StringIO()):
        assert render_review(output_dir, state_dir, effective_date=RUN_DATE) == 0

    assert "Stale Book" not in (output_dir / "weekly-review.md").read_text(encoding="utf-8")


def _field_update_fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    output_dir, state_dir = tmp_path / "run", tmp_path / "state"
    book = _record("Book")
    _write(output_dir, "scouting-candidates.json", [book])
    _write(output_dir, "candidate-history.json", [book])
    seed_state(state_dir, candidate_history=[book], latest_scouting_candidates=[book])
    return output_dir, state_dir, book


def _bytes(*paths: Path) -> list[bytes]:
    return [path.read_bytes() for path in paths]


def _publish_change(output_dir: Path, state_dir: Path, book: dict[str, Any]) -> None:
    changed = {**book, "visible_risk_status": "visible risk"}
    pipeline._publish_candidate_field_updates(
        output_dir,
        open_state_store(state_dir),
        [changed],
        {book["identity_key"]: {"visible_risk_status": "visible risk"}},
    )


def test_a_failed_run_file_publish_writes_no_state(tmp_path, monkeypatch):
    # Review finding on #1069 round 1: a retry must see the state it started from.
    output_dir, state_dir, book = _field_update_fixture(tmp_path)
    run_files = (output_dir / "scouting-candidates.json", output_dir / "candidate-history.json")
    before = _bytes(*run_files)

    def failing(paths: list[Path], contents: list[str], **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(pipeline, "publish_bundle", failing)
    with pytest.raises(OSError):
        _publish_change(output_dir, state_dir, book)

    assert read_state(state_dir, "candidate_history") == [book]
    assert read_state(state_dir, "latest_scouting_candidates") == [book]
    assert _bytes(*run_files) == before


def test_a_failed_state_commit_puts_the_run_files_back_without_writing_new_files(tmp_path, monkeypatch):
    # Review finding on #1069 round 2: the volume may accept no further writes.
    output_dir, state_dir, book = _field_update_fixture(tmp_path)
    run_files = (output_dir / "scouting-candidates.json", output_dir / "candidate-history.json")
    before = _bytes(*run_files)
    real_publish = pipeline.publish_bundle
    publishes = 0

    def publish_once(paths: list[Path], contents: list[str], **kwargs: Any) -> None:
        nonlocal publishes
        publishes += 1
        if publishes > 1:
            raise OSError("no space left on device")
        real_publish(paths, contents, **kwargs)

    def failing(paths: list[Path], contents: list[str], **kwargs: Any) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(pipeline, "publish_bundle", publish_once)
    monkeypatch.setattr(json_backend, "publish_bundle", failing)
    fail_database_commits(monkeypatch)
    with pytest.raises(OSError):
        _publish_change(output_dir, state_dir, book)

    monkeypatch.undo()
    assert read_state(state_dir, "candidate_history") == [book]
    assert _bytes(*run_files) == before
    assert sorted(path.name for path in output_dir.iterdir()) == sorted(path.name for path in run_files)


# The guard of the feedback marker, computed by the version before #1069 for
# the states below, every guarded file present: a marker sealed then must
# still match the same state. (A state whose profile file never existed, or
# that keeps the legacy profile file name, is the accepted one-time mismatch
# documented at the guard.)
GUARD_SEALED_BEFORE_THE_STATE_STORE = "e01f961c6a2bd971bbdc3bfb90401440e75a4a30c308a90d108d6732bed34b4f"
GUARD_SEALED_BEFORE_THE_STATE_STORE_EMPTY_PROFILE = "c7f518ac6d36eb1acb8604960e370ee543b69b50b8884e34e2c791d3af7f99fb"
GUARD_SEALED_BEFORE_THE_STATE_STORE_ALL_EMPTY = "58534b0401ab494d32abc9f88e88e611f00079b2a9ba8f4832496b53b27214df"

PROFILE_ENTRY = {
    "id": "taste-0001",
    "kind": "hard_rule",
    "scope": "author",
    "subject": "Some Author",
    "verbatim_quote": "Этого автора не показывай.",
    "provenance": "conversation",
    "created_at": "2026-11-14",
    "created_by": "editor",
    "status": "active",
}
RECORDS = {
    "candidate_history": [{"identity_key": "a|one", "title": "Альфа", "editor_status": "reject", "editor_note": "нет"}],
    "latest_scouting_candidates": [{"identity_key": "a|one", "title": "Альфа"}],
    "candidate_visibility": [],
    "source_proposals": [],
    "excluded_candidate_history": [{"identity_key": "x|y", "reason": "fiction"}],
}


@pytest.mark.parametrize(
    ("collections", "sealed_guard"),
    [
        ({**RECORDS, "editorial_profile": {"entries": [PROFILE_ENTRY]}}, GUARD_SEALED_BEFORE_THE_STATE_STORE),
        ({**RECORDS, "editorial_profile": {"entries": []}}, GUARD_SEALED_BEFORE_THE_STATE_STORE_EMPTY_PROFILE),
        (
            {**{name: [] for name in RECORDS}, "editorial_profile": {"entries": []}},
            GUARD_SEALED_BEFORE_THE_STATE_STORE_ALL_EMPTY,
        ),
    ],
    ids=["records-and-profile", "empty-profile", "all-empty"],
)
def test_a_feedback_marker_sealed_before_the_state_store_still_matches_unchanged_state(
    tmp_path, collections, sealed_guard
):
    # Review findings on #1069 rounds 2 and 3: an upgrade must not re-scout a sealed report.
    from autoscout.feedback_finalization import _state_guard_digest

    seed_state(tmp_path, **collections)

    assert _state_guard_digest(open_state_store(tmp_path)) == sealed_guard


def test_a_completed_run_with_empty_state_is_sealed_and_an_identical_rerun_is_a_feedback_noop(tmp_path):
    # Review finding on #1069 round 3: empty canonical state still has a guard.
    from autoscout.pipeline import run_pipeline
    from autoscout.pipeline_path import PipelinePath

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"run_date": RUN_DATE, "sources": []}), encoding="utf-8")
    output_dir, state_dir = tmp_path / "run", tmp_path / "state"

    first = run_pipeline(config, output_dir, state_dir)
    second = run_pipeline(config, output_dir, state_dir)

    assert first["pipeline_path"] == PipelinePath.SCOUTED
    assert read_state(state_dir, "candidate_history") == []
    assert second["pipeline_path"] == PipelinePath.FEEDBACK_NOOP
