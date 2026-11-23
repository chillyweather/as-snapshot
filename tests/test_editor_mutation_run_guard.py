"""Editor changes cannot be acknowledged then overwritten by a live run."""

from __future__ import annotations

import errno
import os
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock

import pytest
from state_store_support import seed_state
from state_store_support import state_databases

from autoscout.__main__ import main
from autoscout.git_envelope import GUARD_FILENAME
from autoscout.git_envelope import weekly_run_in_progress


@dataclass(frozen=True)
class Command:
    argv: str
    handler: str
    preview_flag: str | None = None


# Each entry is a public mutation path, with syntactically valid input. Input
# artifacts deliberately do not exist: refusal must precede even reading them.
COMMANDS = [
    Command("run-mvp --config INPUT --output-dir OUTPUT", "autoscout.cli.weekly.run_mvp"),
    Command(
        "record-feedback --identity-key book|author --status interested --confirmed-by-editor",
        "autoscout.cli.feedback.record_feedback",
        "--confirmed-by-editor",
    ),
    Command(
        "record-feedback-batch --decisions INPUT --digest abc --confirmed-by-editor",
        "autoscout.cli.feedback.record_feedback_batch",
        "--confirmed-by-editor",
    ),
    Command("record-visibility --identity-key book|author --action hide", "autoscout.cli.feedback.record_visibility"),
    Command("ingest-feedback --csv INPUT", "autoscout.cli.feedback.ingest_feedback"),
    Command(
        "editorial-profile add --kind hard-rule --scope author --subject Author --quote q --provenance p --confirmed-by-editor",
        "autoscout.cli.profile.add_profile_entry",
        "--confirmed-by-editor",
    ),
    Command(
        "editorial-profile revoke --id taste-0001 --reason q --confirmed-by-editor",
        "autoscout.cli.profile.revoke_profile_entry",
        "--confirmed-by-editor",
    ),
    Command(
        "editorial-profile set-terms --id taste-0001 --match-term topic --confirmed-by-editor",
        "autoscout.cli.profile.set_profile_entry_terms",
        "--confirmed-by-editor",
    ),
    Command(
        "source-proposal add --source-url https://example.test --reason q", "autoscout.cli.sources.add_source_proposal"
    ),
    Command(
        "source-proposal accept --id proposal-0001 --config INPUT --language en",
        "autoscout.cli.sources.accept_source_proposal",
    ),
    Command("source-proposal reject --id proposal-0001 --reason q", "autoscout.cli.sources.reject_source_proposal"),
    Command(
        "source-proposal reconsider --id proposal-0001 --ledger INPUT --reason q --reconsidered-by editor --confirmed-by-editor",
        "autoscout.cli.sources.reconsider_source_proposal",
        "--confirmed-by-editor",
    ),
    Command(
        "merge-candidate-identity --correction-file INPUT",
        "autoscout.cli.identity._merge_candidate_identity",
        "--dry-run",
    ),
    Command(
        "probable-duplicates --dismiss book|author --dismiss book|authors --reason q --confirmed-by-editor",
        "autoscout.probable_duplicates.probable_duplicates_command",
        "--confirmed-by-editor",
    ),
    Command("generate-descriptions --output-dir OUTPUT", "autoscout.generate_descriptions.generate_descriptions"),
    Command(
        "adjudicate-descriptions --output-dir OUTPUT --apply",
        "autoscout.description_adjudication.adjudicate_descriptions",
        "--apply",
    ),
    Command(
        "apply-description-backfill --artifacts-file INPUT", "autoscout.cli.descriptions._apply_description_backfill"
    ),
    Command(
        "apply-english-title-backfill --artifacts-file INPUT --confirm",
        "autoscout.cli.descriptions._apply_english_title_backfill",
        "--confirm",
    ),
    Command(
        "refresh-russian-editions --preview-file INPUT --apply",
        "autoscout.cli.collection.apply_refresh_preview",
        "--apply",
    ),
    Command("migrate-candidate-history --apply", "autoscout.cli.identity._migrate_candidate_history", "--apply"),
    Command("add-source --confirmed-by-editor", "autoscout.cli.onboarding.add_source_command", "--confirmed-by-editor"),
]


def _argv(command: Command, state: Path) -> list[str]:
    return [
        str(state.parent / token.lower()) if token in {"INPUT", "OUTPUT"} else token for token in command.argv.split()
    ] + ["--state-dir", str(state)]


def _snapshot(root: Path) -> dict[str, bytes]:
    return {**{str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}, **state_databases()}


@pytest.fixture
def state(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    state = tmp_path / "nested" / "state"
    state.mkdir(parents=True)
    # Invalid JSON proves that the refusal happens before state is read.
    for name in (
        "candidate-history",
        "candidate-visibility",
        "source-proposals",
        "editorial-profile",
        "candidate-identity-aliases",
        "pending-returned-editor-rows",
        "archived-returned-editor-rows",
        "latest-scouting-candidates",
        "probable-duplicate-dismissals",
    ):
        (state / f"{name}.json").write_bytes(b"retained state, not JSON\n")
    return state


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: c.argv)
@pytest.mark.parametrize("lock", [str(os.getppid()), "malformed", "99999999999999999999999999999999999999"])
def test_mutations_refuse_before_reading_or_touching_state(command, lock, state, capsys):
    root = state.parents[1]
    (root / GUARD_FILENAME).write_text(lock)
    before = _snapshot(root)

    assert main(_argv(command, state)) == 6
    assert capsys.readouterr().out.splitlines() == [
        "reason_code=weekly_run_in_progress",
        "Идёт еженедельный поиск. Изменение не сохранено — повторите после его завершения.",
    ]
    assert _snapshot(root) == before


class _EmptyStore:
    """A State Store holding no records, for the handlers whose own reads these tests fake."""

    def read(self, collection: object) -> list[object]:
        return []


def _dead_pid(pid: int, signal: int) -> None:
    raise ProcessLookupError(errno.ESRCH, "provably dead")


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: c.argv)
@pytest.mark.parametrize("guard", ["absent", "dead", "self", "outside"])
def test_uncontended_mutations_reach_the_existing_handler(command, guard, state, monkeypatch):
    root = state.parents[1]
    if guard == "outside":
        (root / ".git").rmdir()
    if guard != "absent":
        (root / GUARD_FILENAME).write_text(str(os.getpid() if guard == "self" else os.getppid()))
    if guard == "dead":
        monkeypatch.setattr("autoscout.git_envelope.os.kill", _dead_pid)

    # These seams prevent network/model usage and isolate CLI dispatch from
    # each command's existing validation and persistence tests.
    handler = Mock(return_value=0)
    monkeypatch.setattr(command.handler, handler)
    if command.argv.startswith("refresh-russian-editions"):
        monkeypatch.setattr(
            "autoscout.cli.collection.read_json", lambda path, default: {"rows": []} if path.name == "input" else []
        )
        monkeypatch.setattr("autoscout.cli.collection.open_state_store", lambda state_dir: _EmptyStore())
        handler.return_value = {"matched_rows": 0, "changed_records": 0}
    if command.argv.startswith("adjudicate-descriptions"):
        handler.return_value = dict.fromkeys(
            [
                "previewed",
                "considered",
                "valid",
                "rejected",
                "applied",
                "skipped",
                "fallback",
                "evidence_unchanged",
                "not_attempted",
                "preview_file",
            ],
            0,
        )
    assert main(_argv(command, state)) == 0
    handler.assert_called_once()


@pytest.mark.parametrize("command", [c for c in COMMANDS if c.preview_flag], ids=lambda c: c.argv)
def test_preview_dispatch_remains_available(command, state, monkeypatch):
    (state.parents[1] / GUARD_FILENAME).write_text(str(os.getppid()))
    handler = Mock(return_value=3)
    monkeypatch.setattr(command.handler, handler)
    argv = _argv(command, state)
    if command.preview_flag == "--dry-run":
        argv.append("--dry-run")
    else:
        argv.remove(command.preview_flag)
    expected = 3
    # These previews have their own result contracts, rather than code 3.
    if command.preview_flag in {"--dry-run", "--apply"}:
        expected = 0
        handler.return_value = 0
    elif command.preview_flag == "--confirm":
        expected = 2
        handler.return_value = 2
    if command.argv.startswith("refresh-russian-editions"):
        monkeypatch.setattr(
            "autoscout.cli.collection.read_json", lambda path, default: {"rows": []} if path.name == "input" else []
        )
        monkeypatch.setattr("autoscout.cli.collection.open_state_store", lambda state_dir: _EmptyStore())
    if command.argv.startswith("adjudicate-descriptions"):
        handler.return_value = dict.fromkeys(
            [
                "previewed",
                "considered",
                "valid",
                "rejected",
                "would_apply",
                "skipped",
                "fallback",
                "evidence_unchanged",
                "not_attempted",
                "preview_file",
            ],
            0,
        )
    before = _snapshot(state.parents[1])
    assert main(argv) == expected
    if command.argv.startswith("refresh-russian-editions"):
        handler.assert_not_called()
    else:
        handler.assert_called_once()
    assert _snapshot(state.parents[1]) == before


@pytest.mark.parametrize("action", ["record-feedback", "add", "revoke", "set-terms"])
def test_real_two_phase_previews_return_three_without_mutation(action, state, capsys):
    # The previews read state, so the fixture's unreadable files go; the seeded state replaces them.
    for path in state.glob("*.json"):
        path.unlink()
    (state.parents[1] / GUARD_FILENAME).write_text(str(os.getppid()))
    seed_state(
        state,
        candidate_history=[
            {"identity_key": "book|author", "title": "Book", "primary_authors": ["Author"]},
        ],
    )
    seed_state(
        state,
        editorial_profile={
            "entries": [
                {
                    "id": "taste-0001",
                    "kind": "hard_rule",
                    "scope": "topic",
                    "subject": "old-topic",
                    "verbatim_quote": "quote",
                    "provenance": "conversation",
                    "created_at": "2026-01-01",
                    "created_by": "editor",
                    "status": "active",
                }
            ]
        },
    )
    seed_state(state, latest_scouting_candidates=[], candidate_visibility=[])
    command = next(
        c
        for c in COMMANDS
        if c.argv.startswith("record-feedback" if action == "record-feedback" else f"editorial-profile {action}")
    )
    argv = _argv(command, state)
    argv.remove("--confirmed-by-editor")
    before = _snapshot(state.parents[1])
    assert main(argv) == 3
    assert "confirmation_required=true" in capsys.readouterr().out
    assert _snapshot(state.parents[1]) == before


@pytest.mark.parametrize("git_entry", ["file", "directory"])
def test_resolved_state_path_finds_repository_and_does_not_run_git(tmp_path, git_entry, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    if git_entry == "file":
        (root / ".git").write_text("gitdir: elsewhere")
    else:
        (root / ".git").mkdir()
    (root / GUARD_FILENAME).write_text(str(os.getppid()))
    (root / "state").mkdir()
    link = tmp_path / "linked-state"
    link.symlink_to(root / "state", target_is_directory=True)
    monkeypatch.setattr("autoscout.git_envelope._git", Mock(side_effect=AssertionError("no Git")))
    assert weekly_run_in_progress(link / "not-created-yet") is True


@pytest.mark.parametrize("failure", [PermissionError(errno.EPERM, "alive"), OSError(errno.EIO, "unknown")])
def test_ambiguous_or_other_user_pid_fails_closed(state, failure):
    (state.parents[1] / GUARD_FILENAME).write_text(str(os.getppid()))

    def probe(pid, signal):
        raise failure

    assert weekly_run_in_progress(state, pid_probe=probe) is True


def test_unreadable_and_non_utf8_locks_fail_closed(state, monkeypatch):
    lock = state.parents[1] / GUARD_FILENAME
    lock.write_bytes(b"\xff")
    assert weekly_run_in_progress(state) is True
    read_text = Path.read_text

    def unreadable(path, *args, **kwargs):
        if path == lock:
            raise PermissionError("unreadable")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    assert weekly_run_in_progress(state) is True


@pytest.mark.parametrize("option", ["default", "proposal-file", "proposals-file", "proposal-state-dir"])
def test_add_source_guards_all_state_path_forms(option, state, capsys):
    root = state.parents[1]
    (root / GUARD_FILENAME).write_text(str(os.getppid()))
    argv = ["add-source", "--repo-root", str(root), "--confirmed-by-editor"]
    if option in {"proposal-file", "proposals-file"}:
        argv.extend([f"--{option}", str(state / "source-proposals.json")])
    elif option == "proposal-state-dir":
        argv.extend(["--proposal-state-dir", str(state)])
    before = _snapshot(root)
    assert main(argv) == 6
    assert "reason_code=weekly_run_in_progress" in capsys.readouterr().out
    assert _snapshot(root) == before
