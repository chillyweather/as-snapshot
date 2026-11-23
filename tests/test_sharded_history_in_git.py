"""Readers of Candidate History as Git and other run directories hold it, in both layouts (#1073).

The Git envelope commits every book's file, deletions included; the modules
that read Candidate History of earlier commits read either layout, so they
work on commits from before the conversion and after it; the editorial
audit names and digests whichever layout the state has; the artifact parity
gate compares a sharded run directory with a single-file one by records; and
every file under the shard directory is protected from artifact writers.
"""

from __future__ import annotations

import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from state_store_support import SINGLE_FILE
from state_store_support import history_shards
from state_store_support import read_run_output
from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import use_layout

from autoscout import description_qualification
from autoscout.archive_backfill_migration import history_at
from autoscout.artifact_parity import compare_dirs
from autoscout.candidate_identity import candidate_id_for
from autoscout.editorial_audit import EditorialAuditRequest
from autoscout.editorial_audit import run_editorial_audit
from autoscout.git_envelope import GitRepositoryAdapter
from autoscout.safe_artifacts import SafeArtifactError
from autoscout.safe_artifacts import resolve_storage_destination
from autoscout.state_store import export_json
from autoscout.state_store.conversion import convert_to_sharded
from autoscout.state_store.layout import committed_candidate_history
from autoscout.state_store.layout import stored_history_digest

STATE = "runs/current-weekly-pilot-russian"


def _record(index: int, **fields: Any) -> dict[str, Any]:
    identity_key = f"book {index:04d}|author {index:04d}"
    return {
        "candidate_id": candidate_id_for(identity_key),
        "identity_key": identity_key,
        "title": f"Book {index}",
        "primary_authors": [f"Author {index}"],
        "editor_status": "",
        "editor_note": "",
        "first_seen_date": "2026-09-01",
        "last_seen_date": "2026-09-01",
        "source_evidence": [],
        **fields,
    }


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@test.test")
    _git(repo, "config", "user.name", "Test")
    (repo / ".gitkeep").write_text("", encoding="utf-8")
    _git(repo, "add", ".gitkeep")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "init", "-q", "--bare", str(tmp_path / "remote.git"))
    _git(repo, "remote", "add", "origin", str(tmp_path / "remote.git"))
    _git(repo, "push", "-q", "--set-upstream", "origin", "main")
    return repo


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _committed_files(repo: Path, prefix: str) -> set[str]:
    return set(_git(repo, "ls-tree", "-r", "--name-only", "HEAD", "--", prefix).split())


# ── The Git envelope ─────────────────────────────────────────────────────────


def test_finalize_commits_added_changed_and_deleted_book_files(tmp_path):
    repo = _repository(tmp_path)
    state_dir = repo / STATE
    use_layout(state_dir, SINGLE_FILE)
    records = [_record(index) for index in range(250)]
    seed_state(state_dir, candidate_history=records)
    _commit_all(repo, "single-file state")
    convert_to_sharded(state_dir)
    adapter = GitRepositoryAdapter(repo)

    first = adapter.finalize(state_dir)

    assert first["committed"] and first["pushed"], first
    assert f"{STATE}/candidate-history.json" not in _committed_files(repo, STATE)
    assert {path.removeprefix(f"{STATE}/") for path in _committed_files(repo, f"{STATE}/candidate-history")} == set(
        history_shards(state_dir)
    )

    buckets = Counter(record["candidate_id"][10:12] for record in records)
    lonely, changed = sorted(records, key=lambda record: buckets[record["candidate_id"][10:12]])[:2]
    assert buckets[lonely["candidate_id"][10:12]] == 1
    changed = {**changed, "editor_status": "reject"}
    added = _record(999)
    kept = [
        record for record in records if record["candidate_id"] not in {lonely["candidate_id"], changed["candidate_id"]}
    ]
    seed_state(state_dir, candidate_history=[*kept, changed, added])

    second = adapter.finalize(state_dir)

    assert second["committed"] and second["pushed"], second
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""
    assert committed_candidate_history(repo, "HEAD", STATE) == read_state(state_dir, "candidate_history")
    names = _git(repo, "show", "--no-renames", "--name-status", "--format=", "HEAD").split("\n")
    lonely_path = f"{STATE}/candidate-history/{lonely['candidate_id'][10:12]}/{lonely['candidate_id']}.json"
    assert f"D\t{lonely_path}" in names
    assert not (state_dir / "candidate-history" / lonely["candidate_id"][10:12]).exists()


# ── Readers of earlier commits ───────────────────────────────────────────────


def test_committed_history_reads_the_same_records_before_and_after_the_conversion(tmp_path):
    repo = _repository(tmp_path)
    state_dir = repo / STATE
    use_layout(state_dir, SINGLE_FILE)
    seed_state(state_dir, candidate_history=[_record(2), _record(1)])
    before = _commit_all(repo, "single file")
    convert_to_sharded(state_dir)
    after = _commit_all(repo, "sharded")

    assert committed_candidate_history(repo, before, STATE) == committed_candidate_history(repo, after, STATE)
    assert committed_candidate_history(repo, after, STATE) == [_record(1), _record(2)]
    assert committed_candidate_history(repo, after, "runs/elsewhere") is None


def test_the_archive_marker_reads_a_single_file_commit_and_a_sharded_one(tmp_path):
    repo = _repository(tmp_path)
    state_dir = repo / STATE
    use_layout(state_dir, SINGLE_FILE)
    seed_state(state_dir, candidate_history=[_record(1)])
    before = _commit_all(repo, "before the batch")
    convert_to_sharded(state_dir)
    seed_state(state_dir, candidate_history=[_record(1), _record(2)])
    after = _commit_all(repo, "the batch, sharded")

    assert history_at(repo, before, state_dir) == [_record(1)]
    assert history_at(repo, after, state_dir) == [_record(1), _record(2)]
    with pytest.raises(ValueError, match="archive_marker_ref_unreadable"):
        history_at(repo, "no-such-revision", state_dir)


def test_the_description_benchmark_reads_history_of_a_sharded_commit(tmp_path):
    repo = _repository(tmp_path)
    seed_state(repo / STATE, candidate_history=[_record(1, content_description="Описание.")])
    commit = _commit_all(repo, "sharded")

    history = description_qualification._history_at(description_qualification._git_show, repo, commit, STATE)

    assert description_qualification._history_index(history)["book 0001|author 0001"]["content_description"] == (
        "Описание."
    )


# ── The editorial audit ──────────────────────────────────────────────────────


@pytest.mark.parametrize("sharded", [False, True], ids=["single-file", "sharded"])
def test_the_audit_baseline_names_and_digests_the_layout_the_state_has(tmp_path, sharded):
    state_dir = tmp_path / "state"
    if not sharded:
        use_layout(state_dir, SINGLE_FILE)
    seed_state(state_dir, candidate_history=[_record(1, editor_status="reject", editor_note="Нет.")])

    report, code = run_editorial_audit(
        EditorialAuditRequest(state_dir=state_dir, output_dir=tmp_path / "out", run_date="2026-09-09")
    )

    assert code == 0, report
    baseline = report["evidence_baseline"]
    assert baseline["history_path"].endswith("candidate-history" if sharded else "candidate-history.json")
    assert baseline["history_sha256"] == stored_history_digest(state_dir) is not None
    assert baseline["history_records"] == 1


def test_the_history_digest_changes_with_any_book_file(tmp_path):
    seed_state(tmp_path, candidate_history=[_record(1), _record(2)])
    digest = stored_history_digest(tmp_path)

    seed_state(tmp_path, candidate_history=[_record(1), _record(2, editor_status="reject")])
    assert stored_history_digest(tmp_path) != digest
    seed_state(tmp_path, candidate_history=[_record(1), _record(2)])
    assert stored_history_digest(tmp_path) == digest
    seed_state(tmp_path, candidate_history=[_record(1)])
    assert stored_history_digest(tmp_path) != digest


# ── Artifact parity ──────────────────────────────────────────────────────────


def test_parity_compares_a_sharded_run_directory_with_a_single_file_one_by_records(tmp_path):
    single, sharded = tmp_path / "single", tmp_path / "sharded"
    use_layout(single, SINGLE_FILE)
    seed_state(single, candidate_history=[_record(1), _record(2)])
    seed_state(sharded, candidate_history=[_record(2), _record(1)])

    assert compare_dirs(single, sharded, ["candidate-history.json"]).ok

    seed_state(sharded, candidate_history=[_record(1)])
    result = compare_dirs(single, sharded, ["candidate-history.json"])
    assert [entry.status for entry in result.entries] == ["differ"]


# ── Protected paths ──────────────────────────────────────────────────────────


def test_every_file_under_the_shard_directory_is_protected_from_artifact_writers(tmp_path):
    seed_state(tmp_path / "data", candidate_history=[_record(1)])
    [shard] = history_shards(tmp_path / "data")
    shard_path = tmp_path / "data" / shard
    alias = tmp_path / "alias"
    alias.symlink_to(shard_path.parent, target_is_directory=True)
    link = tmp_path / "link.json"
    link.symlink_to(shard_path)

    for destination, kind in (
        (shard_path, "file"),
        (shard_path.parent / "new.json", "file"),
        (tmp_path / "data" / "candidate-history", "directory"),
        (alias / "new.json", "file"),
        (link, "file"),
    ):
        with pytest.raises(SafeArtifactError):
            resolve_storage_destination(destination, kind, "test artifact")


# ── The secret scan before publication (#287) ────────────────────────────────


def test_the_secret_scan_before_publication_reads_every_book_file(tmp_path):
    from autoscout.weekly_workflow import _scan_output_dir_for_secrets

    token = "ghp_" + ("FAKE1234" * 5)
    seed_state(tmp_path, candidate_history=[_record(1), _record(2, editor_note=f"token {token}")])
    [_clean, leaking] = sorted(history_shards(tmp_path), key=lambda name: "FAKE1234" in (tmp_path / name).read_text())

    assert _scan_output_dir_for_secrets(tmp_path) == [leaking]


def test_the_doctor_s_secret_scan_reads_every_book_file(tmp_path):
    from autoscout.doctor import _secret_scan_flagged

    token = "ghp_" + ("FAKE1234" * 5)
    seed_state(tmp_path, candidate_history=[_record(1, editor_note=f"token {token}")])

    assert _secret_scan_flagged(tmp_path) == list(history_shards(tmp_path))


# ── A run without state on a directory that keeps the shards ─────────────────


def test_a_run_without_state_writes_its_history_to_shards_its_output_already_holds(tmp_path):
    from autoscout.weekly_report import keeps_run_history_copy
    from autoscout.weekly_report import read_run_history
    from autoscout.weekly_report import write_run_history

    output_dir = tmp_path / "run"
    seed_state(output_dir, candidate_history=[_record(1)])

    assert keeps_run_history_copy(output_dir, None) is False
    write_run_history(output_dir, None, [_record(1, editor_status="reject"), _record(2)])

    assert not (output_dir / "candidate-history.json").exists()
    assert read_run_history(output_dir) == [_record(1, editor_status="reject"), _record(2)]
    assert read_run_output(output_dir, "candidate_history") == [_record(1, editor_status="reject"), _record(2)]

    plain = tmp_path / "plain"
    plain.mkdir()
    write_run_history(plain, None, [_record(3)])
    assert read_run_history(plain) == [_record(3)]
    assert (plain / "candidate-history.json").is_file()


def test_a_run_without_state_refuses_an_output_directory_with_both_layouts(tmp_path):
    from autoscout.state_store import StateStoreError
    from autoscout.weekly_report import read_run_history

    seed_state(tmp_path, candidate_history=[_record(1)])
    (tmp_path / "candidate-history.json").write_text("[]\n", encoding="utf-8")

    with pytest.raises(StateStoreError, match="shard-candidate-history"):
        read_run_history(tmp_path)


def test_a_field_update_without_state_updates_the_shards_its_output_holds(tmp_path):
    from autoscout import pipeline
    from autoscout.json_io import write_json

    output_dir = tmp_path / "run"
    book = _record(1)
    seed_state(output_dir, candidate_history=[book])
    write_json(output_dir / "scouting-candidates.json", [book])
    changed = {**book, "visible_risk_status": "visible risk"}

    pipeline._publish_candidate_field_updates(
        output_dir, None, [changed], {book["identity_key"]: {"visible_risk_status": "visible risk"}}
    )

    assert not (output_dir / "candidate-history.json").exists()
    assert read_run_output(output_dir, "candidate_history") == [changed]


def test_grounded_enrichment_without_state_keeps_the_shards_of_its_output(tmp_path):
    from test_issue_865_description_timeouts import RUN_DATE
    from test_issue_865_description_timeouts import URL
    from test_issue_865_description_timeouts import Clock
    from test_issue_865_description_timeouts import Discovery
    from test_issue_865_description_timeouts import Fetch
    from test_issue_865_description_timeouts import SlowSynthesizer

    from autoscout.grounded_description import GroundedDescriptionStage
    from autoscout.grounded_description_enrichment import enrich_grounded_descriptions
    from autoscout.json_io import write_json
    from autoscout.pipeline import run_pipeline

    config = tmp_path / "config.json"
    source = {
        "name": "Slow Source",
        "family": "media/curated list",
        "url": "https://example.test/list",
        "access_status": "open",
        "extraction_method": "fixture",
        "language": "en",
        "fixture_candidates": [
            {
                "title": "Work One",
                "primary_authors": ["Alex Author"],
                "source_url": URL,
                "candidate_category": "nonfiction",
            }
        ],
    }
    write_json(config, {"run_date": RUN_DATE, "sources": [source]})
    out = tmp_path / "run"
    run_pipeline(config, out, out)
    # The state the run wrote, in the files a later run without state finds (its export under SQLite).
    export_json(out)
    assert history_shards(out) and not (out / "candidate-history.json").exists()
    clock = Clock()
    stage = GroundedDescriptionStage(
        discovery=Discovery(), fetch=Fetch(), synthesizer=SlowSynthesizer(clock), monotonic=clock, max_workers=1
    )

    enrich_grounded_descriptions(stage, out, None, RUN_DATE, operation_id="no-state")

    assert not (out / "candidate-history.json").exists()
    assert [record["title"] for record in read_run_output(out, "candidate_history")] == ["Work One"]


def test_finalize_commits_beside_a_deletion_already_staged(tmp_path):
    repo = _repository(tmp_path)
    state_dir = repo / STATE
    seed_state(state_dir, candidate_history=[_record(1), _record(2)])
    _commit_all(repo, "sharded state")
    [first, second] = sorted(history_shards(state_dir))
    _git(repo, "rm", "-q", f"{STATE}/{first}")
    (state_dir / second).write_text((state_dir / second).read_text(encoding="utf-8") + " ", encoding="utf-8")

    result = GitRepositoryAdapter(repo).finalize(state_dir)

    assert result["committed"] and result["pushed"], result
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""
    assert _committed_files(repo, f"{STATE}/candidate-history") == {f"{STATE}/{second}"}


def test_finalize_commits_beside_a_rename_already_staged(tmp_path):
    repo = _repository(tmp_path)
    state_dir = repo / STATE
    seed_state(state_dir, candidate_history=[_record(1)])
    (state_dir / "old.json").write_text("[]\n", encoding="utf-8")
    _commit_all(repo, "sharded state")
    _git(repo, "mv", f"{STATE}/old.json", f"{STATE}/new.json")
    seed_state(state_dir, candidate_history=[_record(1, editor_status="reject")])

    result = GitRepositoryAdapter(repo).finalize(state_dir)

    assert result["committed"] and result["pushed"], result
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""
    assert committed_candidate_history(repo, "HEAD", STATE) == [_record(1, editor_status="reject")]
    assert f"{STATE}/new.json" in _committed_files(repo, STATE)


def test_finalize_stops_before_committing_when_staging_fails(tmp_path, monkeypatch):
    from autoscout import git_envelope

    repo = _repository(tmp_path)
    seed_state(repo / STATE, candidate_history=[_record(1)])
    head = _git(repo, "rev-parse", "HEAD")
    real_git = git_envelope._git

    def failing_add(repo_path: Path, *args: str) -> Any:
        if args[:1] == ("update-index",):
            return subprocess.CompletedProcess(["git", *args], 128, "", "fatal: simulated")
        return real_git(repo_path, *args)

    monkeypatch.setattr(git_envelope, "_git", failing_add)
    result = GitRepositoryAdapter(repo).finalize(repo / STATE)

    assert (result["status"], result["reason"]) == ("unsafe", "stage_failed")
    assert _git(repo, "rev-parse", "HEAD") == head
