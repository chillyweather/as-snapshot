"""The dated decision corpus for the editorial memory prototype (issue #929).

Each test builds a small Git repository whose commits are versions of the
canonical state, written through the State Store, and runs the export command
on it. Notes and books are invented.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from autoscout.__main__ import main
from autoscout.candidate_identity import candidate_id_for
from autoscout.editor_decisions import description_digest
from tests.state_store_support import seed_state
from tests.state_store_support import state_snapshot

DESCRIPTION_V1 = "Книга историка о том, как города Средиземноморья торговали солью и что из этого вышло."
DESCRIPTION_V2 = "Обновлённое описание: историк прослеживает соляную торговлю городов Средиземноморья."


def _record(identity_key: str, title: str, author: str, **fields: Any) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id_for(identity_key),
        "identity_key": identity_key,
        "title": title,
        "primary_authors": [author],
        "first_seen_date": "2026-07-01",
        "last_seen_date": "2026-07-01",
        "source_evidence": [{"source_name": "Example List", "evidence_url": f"https://example.org/{title}"}],
        **fields,
    }


def _aliases(*groups: tuple[str, list[str]], redirects: dict[str, str] | None = None) -> dict[str, Any]:
    aliases: dict[str, str] = {}
    candidates: dict[str, Any] = {}
    for canonical_key, keys in groups:
        candidate_id = candidate_id_for(canonical_key)
        candidates[candidate_id] = {"candidate_id": candidate_id, "canonical_identity_key": canonical_key}
        for key in [canonical_key, *keys]:
            aliases[key] = candidate_id
    return {
        "version": 2,
        "aliases": aliases,
        "candidates": candidates,
        "redirects": redirects or {},
        "durable_merges": [],
    }


class Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.state_dir = root / "runs" / "current-weekly-pilot-russian"
        self.state_dir.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.org")
        self.git("config", "user.name", "Test")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args: str, date: str = "2026-07-01T12:00:00+03:00") -> str:
        env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        completed = subprocess.run(
            ["git", "-C", str(self.root), *args], capture_output=True, text=True, check=True, env=env
        )
        return completed.stdout.strip()

    def commit(self, date: str, history: list[dict[str, Any]], aliases: dict[str, Any]) -> str:
        seed_state(self.state_dir, candidate_history=history, candidate_identity_aliases=aliases)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"state {date}", date=f"{date}T12:00:00+03:00")
        return self.git("rev-parse", "HEAD")

    def export(self, output_dir: Path, *extra: str) -> int:
        return main(
            ["export-decision-corpus", "--state-dir", str(self.state_dir), "--output-dir", str(output_dir), *extra]
        )


def _exported(output_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    (export_dir,) = [path for path in output_dir.iterdir() if path.is_dir()]
    records = [json.loads(line) for line in (export_dir / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
    manifest = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))
    return {record["title"]: record for record in records}, manifest


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path / "repo")


SALT_KEY = "salt cities|ann historian"


def _salt(**fields: Any) -> dict[str, Any]:
    return _record(SALT_KEY, "Salt Cities", "Ann Historian", **fields)


def test_a_changed_decision_shows_the_latest_and_keeps_the_earlier_one_with_its_own_date(
    repo: Repo, tmp_path: Path
) -> None:
    aliases = _aliases((SALT_KEY, []))
    repo.commit("2026-07-01", [_salt(content_description=DESCRIPTION_V1)], aliases)
    first = repo.commit(
        "2026-07-03",
        [_salt(content_description=DESCRIPTION_V1, editor_status="maybe_watch", editor_note="Посмотрю.")],
        aliases,
    )
    repo.commit(
        "2026-07-05",
        [_salt(content_description=DESCRIPTION_V2, editor_status="maybe_watch", editor_note="Посмотрю.")],
        aliases,
    )
    second = repo.commit(
        "2026-07-09",
        [_salt(content_description=DESCRIPTION_V2, editor_status="reject", editor_note="Слишком академично.")],
        aliases,
    )

    assert repo.export(tmp_path / "out") == 0

    records, manifest = _exported(tmp_path / "out")
    salt = records["Salt Cities"]
    assert (salt["editor_status"], salt["editor_note"], salt["decision_date"]) == (
        "reject",
        "Слишком академично.",
        "2026-07-09",
    )
    assert salt["decision_commit"] == second
    assert salt["seen"]["description"] == DESCRIPTION_V2
    assert salt["editor_decisions"] == [
        {
            "status": "maybe_watch",
            "note": "Посмотрю.",
            "decision_date": "2026-07-03",
            "description_digest": description_digest({"content_description": DESCRIPTION_V1}),
        },
        {
            "status": "reject",
            "note": "Слишком академично.",
            "decision_date": "2026-07-09",
            "description_digest": description_digest({"content_description": DESCRIPTION_V2}),
        },
    ]
    assert first != second
    assert (manifest["record_count"], manifest["dated_count"], manifest["undated_count"]) == (1, 1, 0)


def test_a_merged_identity_is_one_record_with_its_aliases_and_the_date_of_the_duplicates_decision(
    repo: Repo, tmp_path: Path
) -> None:
    duplicate_key = "salt cities|a historian"
    retired_id = candidate_id_for(duplicate_key)
    survivor_id = candidate_id_for(SALT_KEY)
    aliases = _aliases((SALT_KEY, [duplicate_key]), redirects={retired_id: survivor_id})
    duplicate = _record(duplicate_key, "Salt Cities", "A. Historian", content_description=DESCRIPTION_V1)
    repo.commit("2026-07-01", [_salt(), duplicate], aliases)
    repo.commit("2026-07-02", [_salt(), {**duplicate, "editor_status": "reject", "editor_note": "Нет."}], aliases)
    repo.commit(
        "2026-07-10",
        [_salt(content_description=DESCRIPTION_V1, editor_status="reject", editor_note="Нет.")],
        aliases,
    )

    assert repo.export(tmp_path / "out") == 0

    records, manifest = _exported(tmp_path / "out")
    salt = records["Salt Cities"]
    assert salt["candidate_id"] == survivor_id
    assert duplicate_key in salt["aliases"]
    assert salt["retired_candidate_ids"] == [retired_id]
    assert salt["decision_date"] == "2026-07-02"
    assert [entry["decision_date"] for entry in salt["editor_decisions"]] == ["2026-07-02"]
    assert manifest["record_count"] == 1


def test_a_merge_that_keeps_one_of_two_decisions_logs_it_again_dated_with_the_merge(repo: Repo, tmp_path: Path) -> None:
    duplicate_key = "salt cities|a historian"
    aliases = _aliases(
        (SALT_KEY, [duplicate_key]), redirects={candidate_id_for(duplicate_key): candidate_id_for(SALT_KEY)}
    )
    duplicate = _record(duplicate_key, "Salt Cities", "A. Historian")
    repo.commit("2026-07-01", [_salt(editor_status="interested", editor_note="Интересно."), duplicate], aliases)
    repo.commit(
        "2026-07-02",
        [
            _salt(editor_status="interested", editor_note="Интересно."),
            {**duplicate, "editor_status": "reject", "editor_note": ""},
        ],
        aliases,
    )
    repo.commit("2026-07-08", [_salt(editor_status="interested", editor_note="Интересно.")], aliases)

    assert repo.export(tmp_path / "out") == 0

    salt = _exported(tmp_path / "out")[0]["Salt Cities"]
    logged = [(entry["status"], entry["decision_date"]) for entry in salt["editor_decisions"]]
    assert logged == [("interested", None), ("reject", "2026-07-02"), ("interested", "2026-07-08")]
    assert salt["decision_date"] == "2026-07-08"


def test_a_bare_refusal_and_a_book_without_a_description_are_labelled_and_digested_as_such(
    repo: Repo, tmp_path: Path
) -> None:
    bare_key = "plain refusal|bo writer"
    rights_key = "rights book|cy writer"
    aliases = _aliases((bare_key, []), (rights_key, []))
    bare = _record(bare_key, "Plain Refusal", "Bo Writer")
    rights = _record(rights_key, "Rights Book", "Cy Writer", content_description=DESCRIPTION_V1)
    repo.commit("2026-07-01", [bare, rights], aliases)
    repo.commit(
        "2026-07-04",
        [
            {**bare, "editor_status": "reject", "editor_note": "Нет."},
            {
                **rights,
                "editor_status": "reject",
                "editor_note": "Хотелось бы, но права не продают.",
                "feedback_dimensions": {
                    "editorial_appeal": "appealing",
                    "rights_availability": "unavailable",
                    "quality_problem": "unspecified",
                    "information_needed": "unspecified",
                },
            },
        ],
        aliases,
    )

    assert repo.export(tmp_path / "out") == 0

    records, manifest = _exported(tmp_path / "out")
    plain = records["Plain Refusal"]
    assert plain["note_type"] == "bare_refusal"
    assert plain["seen"]["description"] == ""
    assert plain["seen"]["description_digest"] == ""
    assert plain["editor_decisions"][-1]["description_digest"] == ""
    assert records["Rights Book"]["note_type"] == "rights_availability"
    assert manifest["note_type_counts"] == {"bare_refusal": 1, "rights_availability": 1}


def test_undated_decisions_are_listed_with_a_reason_instead_of_a_date(repo: Repo, tmp_path: Path) -> None:
    old_key = "before tracking|di writer"
    branch_key = "from a branch|ed writer"
    late_key = "not committed|fa writer"
    aliases = _aliases((old_key, []), (branch_key, []), (late_key, []))
    old = _record(old_key, "Before Tracking", "Di Writer", editor_status="reject", editor_note="Старое решение.")
    branch = _record(branch_key, "From A Branch", "Ed Writer")
    late = _record(late_key, "Not Committed", "Fa Writer")
    repo.commit("2026-07-01", [old, branch, late], aliases)
    repo.git("checkout", "-q", "-b", "side")
    repo.commit("2026-07-02", [old, {**branch, "editor_status": "reject", "editor_note": "С ветки."}, late], aliases)
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "-m", "merge side", "side", date="2026-07-03T12:00:00+03:00")
    seed_state(
        repo.state_dir,
        candidate_history=[
            old,
            {**branch, "editor_status": "reject", "editor_note": "С ветки."},
            {**late, "editor_status": "interested", "editor_note": "Ещё не в Git."},
        ],
    )

    assert repo.export(tmp_path / "out") == 0

    records, manifest = _exported(tmp_path / "out")
    reasons = {title: (record["decision_date"], record["undated_reason"]) for title, record in records.items()}
    assert reasons == {
        "Before Tracking": (None, "present_in_oldest_version"),
        "From A Branch": (None, "first_seen_in_merge_commit"),
        "Not Committed": (None, "not_in_git_history"),
    }
    assert all(record["seen"] is None for record in records.values())
    assert manifest["undated_count"] == 3
    assert manifest["undated_by_reason"] == {
        "first_seen_in_merge_commit": 1,
        "not_in_git_history": 1,
        "present_in_oldest_version": 1,
    }
    assert manifest["state_matches_source_commit"] is False


def test_decisions_recorded_with_a_date_since_1053_continue_the_rebuilt_log(repo: Repo, tmp_path: Path) -> None:
    aliases = _aliases((SALT_KEY, []))
    repo.commit("2026-07-01", [_salt()], aliases)
    repo.commit("2026-07-02", [_salt(editor_status="maybe_watch", editor_note="Посмотрю.")], aliases)
    recorded = {"status": "reject", "note": "Нет.", "decision_date": "2026-07-05", "description_digest": ""}
    repo.commit(
        "2026-07-06",
        [
            _salt(
                editor_status="reject",
                editor_note="Нет.",
                editor_decisions=[
                    {"status": "maybe_watch", "note": "Посмотрю.", "decision_date": None, "description_digest": None},
                    recorded,
                ],
            )
        ],
        aliases,
    )

    assert repo.export(tmp_path / "out") == 0

    salt = _exported(tmp_path / "out")[0]["Salt Cities"]
    assert salt["editor_decisions"] == [
        {"status": "maybe_watch", "note": "Посмотрю.", "decision_date": "2026-07-02", "description_digest": ""},
        recorded,
    ]
    assert salt["decision_date"] == "2026-07-05"


def test_the_export_is_identical_on_a_second_run_and_leaves_the_state_untouched(repo: Repo, tmp_path: Path) -> None:
    aliases = _aliases((SALT_KEY, []))
    repo.commit("2026-07-01", [_salt(content_description=DESCRIPTION_V1)], aliases)
    repo.commit(
        "2026-07-02", [_salt(content_description=DESCRIPTION_V1, editor_status="reject", editor_note="Нет.")], aliases
    )
    before = state_snapshot(repo.state_dir)

    assert repo.export(tmp_path / "out") == 0
    (export_dir,) = list((tmp_path / "out").iterdir())
    first = {path.name: path.read_bytes() for path in export_dir.iterdir()}
    assert repo.export(tmp_path / "out") == 0
    second = {path.name: path.read_bytes() for path in export_dir.iterdir()}

    assert state_snapshot(repo.state_dir) == before
    assert repo.git("status", "--porcelain") == ""
    assert first["decisions.jsonl"] == second["decisions.jsonl"]

    def without_export_time(raw: bytes) -> dict[str, Any]:
        manifest = json.loads(raw)
        manifest.pop("exported_at")
        return manifest

    assert without_export_time(first["manifest.json"]) == without_export_time(second["manifest.json"])


@pytest.mark.parametrize("inside", ["runs", "configs", "runs/current-weekly-pilot-russian/corpus"])
def test_the_export_refuses_to_write_inside_canonical_directories(repo: Repo, inside: str) -> None:
    aliases = _aliases((SALT_KEY, []))
    repo.commit("2026-07-01", [_salt(editor_status="reject", editor_note="Нет.")], aliases)
    before = state_snapshot(repo.state_dir)

    assert repo.export(repo.root / inside) == 2

    assert state_snapshot(repo.state_dir) == before
    assert repo.git("status", "--porcelain") == ""


def test_an_uncommitted_return_to_an_earlier_decision_is_not_dated_with_the_earlier_one(
    repo: Repo, tmp_path: Path
) -> None:
    aliases = _aliases((SALT_KEY, []))
    repo.commit("2026-07-01", [_salt()], aliases)
    repo.commit("2026-07-02", [_salt(editor_status="maybe_watch", editor_note="Посмотрю.")], aliases)
    repo.commit("2026-07-04", [_salt(editor_status="reject", editor_note="Нет.")], aliases)
    seed_state(repo.state_dir, candidate_history=[_salt(editor_status="maybe_watch", editor_note="Посмотрю.")])

    assert repo.export(tmp_path / "out") == 0

    salt = _exported(tmp_path / "out")[0]["Salt Cities"]
    assert (salt["decision_date"], salt["undated_reason"]) == (None, "not_in_git_history")
    assert [entry["decision_date"] for entry in salt["editor_decisions"]] == ["2026-07-02", "2026-07-04", None]


def test_decisions_are_dated_across_the_change_from_one_history_file_to_one_file_per_book(
    repo: Repo, tmp_path: Path
) -> None:
    from autoscout.state_store.conversion import convert_to_sharded
    from tests.state_store_support import SINGLE_FILE
    from tests.state_store_support import history_shards
    from tests.state_store_support import use_layout

    aliases = _aliases((SALT_KEY, []))
    use_layout(repo.state_dir, SINGLE_FILE)
    repo.commit("2026-07-01", [_salt(content_description=DESCRIPTION_V1)], aliases)
    repo.commit(
        "2026-07-03",
        [_salt(content_description=DESCRIPTION_V1, editor_status="maybe_watch", editor_note="Посмотрю.")],
        aliases,
    )
    convert_to_sharded(repo.state_dir)
    assert history_shards(repo.state_dir)
    repo.git("add", "-A")
    repo.git("commit", "-q", "-m", "shard", date="2026-07-04T12:00:00+03:00")
    repo.commit(
        "2026-07-06",
        [_salt(content_description=DESCRIPTION_V1, editor_status="reject", editor_note="Слишком узко.")],
        aliases,
    )

    assert repo.export(tmp_path / "out") == 0
    records, manifest = _exported(tmp_path / "out")

    assert [(entry["status"], entry["decision_date"]) for entry in records["Salt Cities"]["editor_decisions"]] == [
        ("maybe_watch", "2026-07-03"),
        ("reject", "2026-07-06"),
    ]
    assert manifest["history_versions_read"] == 4
