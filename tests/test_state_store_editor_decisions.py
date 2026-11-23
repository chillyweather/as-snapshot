"""The editor-decision paths go through the State Store, in transactions (#1070).

A preview, a dry run and a read-only command leave every stored byte as it
was. A confirmed action applies all its state changes in one store
transaction: a failure part way through leaves the state exactly as before,
and the same command succeeds when repeated.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from state_store_support import fail_database_commits
from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_snapshot

from autoscout import editor_feedback
from autoscout import identity_corrections
from autoscout import safe_artifacts
from autoscout.__main__ import main
from autoscout.editor_feedback import ingest_feedback
from autoscout.editor_feedback import record_feedback
from autoscout.editor_feedback import record_visibility
from autoscout.editorial_audit import EditorialAuditRequest
from autoscout.editorial_audit import run_editorial_audit
from autoscout.history_identity_replay import main as replay_history_main
from autoscout.identity_corrections import apply_identity_correction
from autoscout.json_io import serialize_json
from autoscout.state_store import json_backend

SHORT_KEY = "a short|wrong"
LONG_KEY = "long title|wrong"
CANONICAL_KEY = "long title|correct"
RUN_FILES = ("scouting-candidates.json", "memo-queue.json")


def _record(identity_key: str, candidate_id: str, title: str, **fields: object) -> dict[str, object]:
    return {
        "identity_key": identity_key,
        "candidate_id": candidate_id,
        "title": title,
        "primary_authors": ["Wrong Author"],
        "first_seen_date": "2026-06-01",
        "last_seen_date": "2026-07-01",
        "source_evidence": [{"source_url": f"https://example.test/{candidate_id}"}],
        **fields,
    }


def _state(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    history = [
        _record(SHORT_KEY, "candidate-aaaaaaaaaaaaaaaaaaaaaaaa", "A Short"),
        _record(
            LONG_KEY,
            "candidate-bbbbbbbbbbbbbbbbbbbbbbbb",
            "Long Title",
            editor_status="interested",
            editor_note="Беру.",
        ),
    ]
    seed_state(
        state_dir,
        candidate_history=history,
        latest_scouting_candidates=[dict(record) for record in history],
        candidate_visibility=[{"identity_key": SHORT_KEY, "visibility": "hidden", "audit_log": []}],
        candidate_identity_aliases={
            "version": 1,
            "aliases": {
                SHORT_KEY: "candidate-aaaaaaaaaaaaaaaaaaaaaaaa",
                LONG_KEY: "candidate-bbbbbbbbbbbbbbbbbbbbbbbb",
            },
            "candidates": {
                "candidate-aaaaaaaaaaaaaaaaaaaaaaaa": {"candidate_id": "candidate-aaaaaaaaaaaaaaaaaaaaaaaa"},
                "candidate-bbbbbbbbbbbbbbbbbbbbbbbb": {"candidate_id": "candidate-bbbbbbbbbbbbbbbbbbbbbbbb"},
            },
        },
        editorial_profile={
            "entries": [
                {
                    "id": "taste-0001",
                    "kind": "hard_rule",
                    "scope": "author",
                    "subject": "Someone Else",
                    "verbatim_quote": "Не берём.",
                    "provenance": "test",
                    "created_at": "2026-11-01",
                    "created_by": "agent",
                    "status": "active",
                }
            ]
        },
    )
    return state_dir


def _correction() -> dict[str, object]:
    return {
        "source_identity_keys": [SHORT_KEY, LONG_KEY],
        "canonical": {"identity_key": CANONICAL_KEY, "title": "Long Title", "primary_authors": ["Correct Author"]},
    }


def _with_run_files(state_dir: Path) -> dict[str, bytes]:
    """The run files a weekly run leaves in the state directory, which a merge also rewrites."""
    (state_dir / "scouting-candidates.json").write_text(
        serialize_json(read_state(state_dir, "latest_scouting_candidates")), encoding="utf-8"
    )
    (state_dir / "memo-queue.json").write_text(serialize_json([]), encoding="utf-8")
    return _run_files(state_dir)


def _run_files(state_dir: Path) -> dict[str, bytes]:
    return {name: (state_dir / name).read_bytes() for name in RUN_FILES}


def _fail_on_replace(monkeypatch: pytest.MonkeyPatch, call: int) -> None:
    """Fail the *call*-th file the State Store moves into place when a transaction commits."""
    real_publish_bundle = json_backend.publish_bundle
    calls = {"count": 0}

    def failing_replace(source: Path, destination: Path) -> None:
        calls["count"] += 1
        if calls["count"] == call:
            raise OSError("no space left on device")
        safe_artifacts.staged_replace(source, destination)

    def publish_bundle(paths, contents, **_kwargs):  # type: ignore[no-untyped-def]
        return real_publish_bundle(paths, contents, replace=failing_replace)

    monkeypatch.setattr(json_backend, "publish_bundle", publish_bundle)


# --- Previews, dry runs and read-only commands write nothing -----------------


def test_previews_dry_runs_and_read_only_commands_leave_every_stored_byte(tmp_path: Path) -> None:
    state_dir = _state(tmp_path)
    state = str(state_dir)
    correction = tmp_path / "correction.json"
    correction.write_text(json.dumps(_correction()), encoding="utf-8")
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps([{"ID": "candidate-aaaaaaaaaaaaaaaaaaaaaaaa"}]), encoding="utf-8")
    before = state_snapshot(state_dir)

    commands = [
        (
            [
                "record-feedback",
                "--identity-key",
                SHORT_KEY,
                "--status",
                "reject",
                "--note",
                "Нет.",
                "--state-dir",
                state,
            ],
            3,
        ),
        (
            [
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "author",
                "--subject",
                "Wrong Author",
                "--quote",
                "Этого автора не берём.",
                "--provenance",
                "test",
                "--state-dir",
                state,
            ],
            3,
        ),
        (["editorial-profile", "revoke", "--id", "taste-0001", "--reason", "Передумала.", "--state-dir", state], 3),
        (["editorial-profile", "list", "--state-dir", state], 0),
        (["merge-candidate-identity", "--state-dir", state, "--correction-file", str(correction), "--dry-run"], 0),
        (["resolve-review-rows", "--state-dir", state, "--rows", str(rows)], 0),
        (["probable-duplicates", "--state-dir", state], 0),
        (["migrate-candidate-history", "--state-dir", state], 0),
    ]
    for argv, expected in commands:
        assert main(argv) == expected, argv
        assert state_snapshot(state_dir) == before, argv

    assert replay_history_main(["--state-dir", state]) == 0
    _report, code = run_editorial_audit(
        EditorialAuditRequest(state_dir=state_dir, output_dir=tmp_path / "audit", run_date="2026-11-14")
    )
    assert code == 0
    assert state_snapshot(state_dir) == before


# --- A confirmed feedback action is one transaction ---------------------------


def test_feedback_import_that_fails_part_way_stages_neither_rows_nor_source_ideas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = _state(tmp_path)
    feedback_csv = tmp_path / "feedback.csv"
    feedback_csv.write_text(f"Candidate ID,Editor Status,Editor Note\n{SHORT_KEY},reject,Нет.\n", encoding="utf-8")
    ideas_csv = tmp_path / "ideas.csv"
    ideas_csv.write_text(
        "Source URL,Reason\nhttps://one.example.test/,Хороший список\nhttps://two.example.test/,Ещё один\n",
        encoding="utf-8",
    )
    before = state_snapshot(state_dir)
    real_record_source_proposal = editor_feedback.record_source_proposal
    calls = {"count": 0}

    def failing_second_proposal(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("no space left on device")
        return real_record_source_proposal(*args, **kwargs)

    # The returned row and the first source idea are staged when the second fails.
    monkeypatch.setattr(editor_feedback, "record_source_proposal", failing_second_proposal)
    with pytest.raises(OSError):
        ingest_feedback(feedback_csv, state_dir, ideas_csv)

    assert calls["count"] == 2
    assert state_snapshot(state_dir) == before

    monkeypatch.undo()
    assert ingest_feedback(feedback_csv, state_dir, ideas_csv) == 0
    assert [row["identity_key"] for row in read_state(state_dir, "pending_editor_rows")] == [SHORT_KEY]
    assert len(read_state(state_dir, "source_proposals")) == 2


def test_confirmed_feedback_and_visibility_that_fail_to_commit_leave_state_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = _state(tmp_path)
    before = state_snapshot(state_dir)

    def failing_publish(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise OSError("no space left on device")

    monkeypatch.setattr(json_backend, "publish_single", failing_publish)
    fail_database_commits(monkeypatch)
    with pytest.raises(OSError):
        record_feedback(SHORT_KEY, "reject", "Нет.", state_dir, confirmed_by_editor=True)
    with pytest.raises(OSError):
        record_visibility(LONG_KEY, "hide", state_dir, reason="Скрыть.")
    assert state_snapshot(state_dir) == before

    monkeypatch.undo()
    assert record_feedback(SHORT_KEY, "reject", "Нет.", state_dir, confirmed_by_editor=True) == 0
    assert record_visibility(LONG_KEY, "hide", state_dir, reason="Скрыть.") == 0
    assert read_state(state_dir, "pending_editor_rows")[0]["Editor Note"] == "Нет."
    assert {row["identity_key"]: row["visibility"] for row in read_state(state_dir, "candidate_visibility")} == {
        SHORT_KEY: "hidden",
        LONG_KEY: "hidden",
    }


# --- A confirmed identity merge is one transaction ----------------------------


def test_merge_whose_state_commit_fails_part_way_leaves_state_and_run_files_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = _state(tmp_path)
    run_files = _with_run_files(state_dir)
    before = state_snapshot(state_dir)

    # The run files are published first; the state commit then fails on its second file.
    _fail_on_replace(monkeypatch, call=2)
    fail_database_commits(monkeypatch)
    with pytest.raises(OSError):
        apply_identity_correction(state_dir, _correction())

    assert state_snapshot(state_dir) == before
    assert _run_files(state_dir) == run_files

    monkeypatch.undo()
    apply_identity_correction(state_dir, _correction())
    assert [record["identity_key"] for record in read_state(state_dir, "candidate_history")] == [CANONICAL_KEY]
    assert read_state(state_dir, "candidate_identity_aliases")["redirects"]


def test_merge_whose_run_files_fail_to_publish_writes_no_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = _state(tmp_path)
    run_files = _with_run_files(state_dir)
    before = state_snapshot(state_dir)
    calls = {"count": 0}

    def failing_second_run_file(source: Path, destination: Path) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("no space left on device")
        safe_artifacts.staged_replace(source, destination)

    monkeypatch.setattr(identity_corrections, "_replace_state_file", failing_second_run_file)
    with pytest.raises(OSError):
        apply_identity_correction(state_dir, _correction())

    assert calls["count"] == 2
    assert state_snapshot(state_dir) == before
    assert _run_files(state_dir) == run_files
