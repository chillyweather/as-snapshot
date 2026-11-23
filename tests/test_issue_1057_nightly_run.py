"""The nightly scheduled run, the Kirkus timer and the morning report (#1057)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Any
from typing import ClassVar

import pytest

from autoscout import weekly_workflow
from autoscout.config import load_validated_config
from autoscout.doctor import _default_repo_inspect
from autoscout.git_envelope import GUARD_FILENAME
from autoscout.git_envelope import LOG_RESIDUE_DIR
from autoscout.git_envelope import LOG_RESIDUE_KEEP
from autoscout.git_envelope import GitRepositoryAdapter
from autoscout.kirkus_listing import KirkusError
from autoscout.kirkus_listing import attempt_path
from autoscout.kirkus_precollection import configured_source
from autoscout.kirkus_precollection import pass_lock_held
from autoscout.kirkus_precollection import weekly_stage
from autoscout.operator_notice import ENV_FROM
from autoscout.operator_notice import ENV_HOST
from autoscout.operator_notice import ENV_PASSWORD
from autoscout.operator_notice import ENV_PORT
from autoscout.operator_notice import ENV_TO
from autoscout.operator_notice import ENV_USER
from autoscout.operator_notice import GENERIC_REASON
from autoscout.operator_notice import Report
from autoscout.operator_notice import deliver
from autoscout.operator_notice import failed_run_reason
from autoscout.operator_notice import kirkus_line
from autoscout.operator_notice import run_report
from autoscout.toolchain_preflight import STAGE_ZERO_NAME
from autoscout.weekly_report import WeeklyReportStore
from autoscout.weekly_workflow import WeeklyWorkflow
from autoscout.weekly_workflow import WeeklyWorkflowResult
from autoscout.weekly_workflow import WorkflowOutcome
from autoscout.weekly_workflow import deterministic_adapters
from autoscout.weekly_workflow import run_weekly_workflow
from tests.test_issue_880_parallel_kirkus import _adapters as _kirkus_adapters
from tests.test_issue_880_parallel_kirkus import _config as _kirkus_run_config
from tests.test_issue_880_parallel_kirkus import _offline_other_capture  # noqa: F401  (autouse fixture)
from tests.test_issue_880_parallel_kirkus import _Pass
from tests.test_kirkus_listing import REFERENCE
from tests.test_kirkus_listing import _pass_config
from tests.test_kirkus_listing import site
from tests.test_weekly_workflow import _write_capture_fixture
from tests.test_weekly_workflow import _write_codex_fixture_config
from tests.test_weekly_workflow import _write_extraction_fixture

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOK_TITLE = "The Unmistakable Secret Book Title"


# ── Git helpers ────────────────────────────────────────────────────────────


def _git(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(path), capture_output=True, text=True, check=True).stdout.strip()


def _repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """A checkout on main that tracks a local bare remote, with a committed run log."""
    repo, remote = tmp_path / "repo", tmp_path / "remote.git"
    repo.mkdir()
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@test.test")
    _git(repo, "config", "user.name", "Test")
    # The residue folder is ignored, as in the repository's own .gitignore.
    (repo / ".gitignore").write_text(f"/{LOG_RESIDUE_DIR.as_posix()}/\n", encoding="utf-8")
    log = repo / "runs" / "weekly" / "workflow-log.json"
    log.parent.mkdir(parents=True)
    log.write_text('[{"stage": "terminal", "status": "completed/no-attention"}]\n', encoding="utf-8")
    (log.parent / "run-summary.json").write_text(json.dumps(COMMITTED_SUMMARY, indent=2) + "\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "--set-upstream", "origin", "main")
    return repo, log


def _adapter(repo: Path, log: Path) -> GitRepositoryAdapter:
    return GitRepositoryAdapter(repo, expected_branch="main", run_log_path=log)


FAILED_LOG = '[{"stage": "toolchain-preflight", "status": "failed"}]\n'
COMMITTED_SUMMARY = {"editor_facing_count": 3, "toolchain_preflight": {"overall_status": "pass"}}


# ── The log residue of an earlier failed run ───────────────────────────────


def test_the_residue_folder_is_ignored_by_this_repository():
    probe = REPO_ROOT / LOG_RESIDUE_DIR / "workflow-log-20261111T000000000000Z.json"
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", str(probe.relative_to(REPO_ROOT))],
        cwd=str(REPO_ROOT),
        check=False,
    )
    assert ignored.returncode == 0


def test_a_lone_modified_run_log_is_set_aside_and_restored_before_the_preflight(tmp_path):
    repo, log = _repo_with_remote(tmp_path)
    committed = log.read_text(encoding="utf-8")
    log.write_text(FAILED_LOG, encoding="utf-8")
    adapter = _adapter(repo, log)

    result = adapter.preflight()
    adapter.release_guard()

    assert result["status"] == "ok", result
    assert result["log_residue_set_aside"] is True
    assert log.read_text(encoding="utf-8") == committed
    copies = list((repo / LOG_RESIDUE_DIR).glob("workflow-log-*.json"))
    assert [copy.read_text(encoding="utf-8") for copy in copies] == [FAILED_LOG]
    assert _git(repo, "status", "--porcelain") == ""


def test_the_run_log_together_with_another_path_still_blocks_and_is_left_alone(tmp_path):
    repo, log = _repo_with_remote(tmp_path)
    log.write_text(FAILED_LOG, encoding="utf-8")
    (repo / "unrelated.txt").write_text("dirty", encoding="utf-8")
    adapter = _adapter(repo, log)

    result = adapter.preflight()
    adapter.release_guard()

    assert result["status"] == "unsafe"
    assert "dirty_working_tree" in result["reasons"]
    assert "log_residue_set_aside" not in result
    assert log.read_text(encoding="utf-8") == FAILED_LOG
    assert not (repo / LOG_RESIDUE_DIR).exists()


@pytest.mark.parametrize("modify_again", [False, True])
def test_a_staged_run_log_still_blocks_and_nothing_is_restored(tmp_path, modify_again):
    repo, log = _repo_with_remote(tmp_path)
    log.write_text(FAILED_LOG, encoding="utf-8")
    _git(repo, "add", str(log.relative_to(repo)))
    if modify_again:
        log.write_text('[{"stage": "capture", "status": "failed"}]\n', encoding="utf-8")
    worktree = log.read_text(encoding="utf-8")
    staged = _git(repo, "show", f":{log.relative_to(repo).as_posix()}")
    adapter = _adapter(repo, log)

    result = adapter.preflight()
    adapter.release_guard()

    assert result["status"] == "unsafe" and "dirty_working_tree" in result["reasons"]
    assert "log_residue_set_aside" not in result
    assert log.read_text(encoding="utf-8") == worktree
    assert _git(repo, "show", f":{log.relative_to(repo).as_posix()}") == staged
    assert not (repo / LOG_RESIDUE_DIR).exists()
    # The doctor agrees: a staged log is a repository blocker.
    assert "dirty_working_tree" in _default_repo_inspect(repo, "main", None, log.parent)(None)["reasons"]


def test_another_lone_modified_file_still_blocks(tmp_path):
    repo, log = _repo_with_remote(tmp_path)
    (repo / ".gitignore").write_text("changed\n", encoding="utf-8")

    result = _adapter(repo, log).preflight()

    assert result["status"] == "unsafe" and "dirty_working_tree" in result["reasons"]


def test_only_the_newest_residue_copies_are_kept(tmp_path):
    repo, log = _repo_with_remote(tmp_path)
    folder = repo / LOG_RESIDUE_DIR
    folder.mkdir(parents=True)
    for day in range(1, LOG_RESIDUE_KEEP + 1):
        (folder / f"workflow-log-202601{day:02d}T000000000000Z.json").write_text("[]", encoding="utf-8")
    log.write_text(FAILED_LOG, encoding="utf-8")
    adapter = _adapter(repo, log)

    assert adapter.preflight()["status"] == "ok"
    adapter.release_guard()

    copies = sorted(path.name for path in folder.glob("workflow-log-*.json"))
    assert len(copies) == LOG_RESIDUE_KEEP
    assert "workflow-log-20260101T000000000000Z.json" not in copies
    assert (folder / copies[-1]).read_text(encoding="utf-8") == FAILED_LOG


def test_the_doctor_does_not_report_a_lone_run_log_as_a_repository_blocker(tmp_path):
    repo, log = _repo_with_remote(tmp_path)
    log.write_text(FAILED_LOG, encoding="utf-8")

    status = _default_repo_inspect(repo, "main", None, log.parent)(None)

    assert status["status"] == "ok", status
    assert status["log_residue"] is True
    # Read-only: the doctor leaves the residue for the next run.
    assert log.read_text(encoding="utf-8") == FAILED_LOG
    (repo / "unrelated.txt").write_text("dirty", encoding="utf-8")
    assert "dirty_working_tree" in _default_repo_inspect(repo, "main", None, log.parent)(None)["reasons"]


def test_a_checkout_ahead_of_origin_does_not_block_and_the_next_finalize_pushes_it(tmp_path):
    repo, log = _repo_with_remote(tmp_path)
    (repo / "runs" / "weekly" / "candidate-history.json").write_text("{}", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "unpushed log-only commit")
    unpushed = _git(repo, "rev-parse", "HEAD")
    adapter = _adapter(repo, log)

    preflight = adapter.preflight()
    assert preflight["status"] == "ok", preflight
    assert preflight["ahead_count"] == 1
    assert adapter.update()["status"] == "ok"

    log.write_text('[{"stage": "terminal", "status": "completed/no-attention", "run": 2}]\n', encoding="utf-8")
    finalized = adapter.finalize(log.parent)
    adapter.release_guard()

    assert finalized["status"] == "ok" and finalized["pushed"] is True
    assert _git(repo, "rev-parse", "origin/main") == _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "merge-base", "--is-ancestor", unpushed, "origin/main") == ""


class _ResidueRepository:
    """A repository adapter whose preflight reports a set-aside residue."""

    def __init__(self) -> None:
        self.run_log_path: Path | None = None

    def set_run_log_path(self, path: Path) -> None:
        self.run_log_path = path

    def preflight(self, validated_artifact_allowlist=None):
        return {"status": "ok", "log_residue_set_aside": True}

    def update(self, validated_artifact_allowlist=None):
        return {"status": "ok"}

    def finalize(self, output_dir):
        return {"status": "ok"}

    def release_guard(self):
        pass


def _fixture_run(tmp_path: Path) -> tuple[Path, Path, Any]:
    config = _write_codex_fixture_config(tmp_path)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    _write_capture_fixture(tmp_path, output_dir)
    adapters = deterministic_adapters()
    adapters.capture.prepare(config, output_dir, live=False)
    _write_extraction_fixture(output_dir)
    return config, output_dir, adapters


def test_the_run_names_its_run_log_and_records_one_sanitized_set_aside_entry(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path)
    adapters.repository = _ResidueRepository()

    result = run_weekly_workflow(config, output_dir, adapters=adapters)

    assert result.outcome.value.startswith("completed/"), result.failed_stage
    assert adapters.repository.run_log_path == output_dir / "workflow-log.json"
    persisted = json.loads((output_dir / "workflow-log.json").read_text(encoding="utf-8"))
    entries = [entry for entry in persisted if entry["stage"] == "log-residue"]
    assert len(entries) == 1
    assert entries[0]["status"] == "set_aside"
    assert entries[0]["detail"] == {"reason": "earlier_workflow_log_set_aside"}


# ── The scheduled run ──────────────────────────────────────────────────────


class _RecordingPresentation:
    def __init__(self) -> None:
        self.opened: list[Path] = []

    def announce(self, stage: str) -> None:
        pass

    def open_review(self, review_path: Path) -> dict[str, Any]:
        self.opened.append(review_path)
        return {"status": "failed", "opened": False}


def test_a_clean_scheduled_run_completes_without_the_browser_stage(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path)
    adapters.presentation = _RecordingPresentation()

    result = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)

    assert result.outcome.value.startswith("completed/"), result.failed_stage
    assert adapters.presentation.opened == []
    assert not any(entry.get("stage") == "open-review" for entry in result.log)
    from autoscout.russian_workflow import open_failure_fallback
    from autoscout.russian_workflow import render_workflow_result

    assert open_failure_fallback() not in render_workflow_result(result, include_progress=False)


def test_a_manual_run_keeps_the_browser_stage(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path)
    adapters.presentation = _RecordingPresentation()

    run_weekly_workflow(config, output_dir, adapters=adapters)

    assert len(adapters.presentation.opened) == 1


def test_a_scheduled_run_on_a_date_that_already_completed_a_run_still_runs(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path)
    first = run_weekly_workflow(config, output_dir, adapters=adapters)
    assert first.outcome.value.startswith("completed/")

    second = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)

    assert second.outcome.value.startswith("completed/"), second.failed_stage
    assert any(entry.get("stage") == "operation-start" for entry in second.log)


class _HeldLockRepository(_ResidueRepository):
    def __init__(self, reason: str, *, owner_alive: bool | None = True) -> None:
        super().__init__()
        self.reason = reason
        self.owner_alive = owner_alive

    def preflight(self, validated_artifact_allowlist=None):
        return {"status": "unsafe", "reason": self.reason}

    def held_by_live_run(self) -> bool:
        return self.owner_alive is True


class _UnverifiedLockRepository(_ResidueRepository):
    """A repository adapter that reports a held lock but cannot tell whether its owner lives."""

    def preflight(self, validated_artifact_allowlist=None):
        return {"status": "unsafe", "reason": "guard_held"}


@pytest.mark.parametrize("reason", ["guard_held", "stale_guard_with_live_process"])
def test_a_scheduled_run_that_meets_a_live_run_is_skipped_and_writes_nothing(tmp_path, reason):
    config, output_dir, adapters = _fixture_run(tmp_path)
    (output_dir / "workflow-log.json").write_text("[]", encoding="utf-8")
    before = {path: path.read_bytes() for path in output_dir.rglob("*") if path.is_file()}
    adapters.repository = _HeldLockRepository(reason)

    result = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)

    assert result.outcome == WorkflowOutcome.SKIPPED_RUN_IN_PROGRESS
    assert {path: path.read_bytes() for path in output_dir.rglob("*") if path.is_file()} == before


def test_a_manual_run_that_meets_a_live_run_is_refused_as_today(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path)
    adapters.repository = _HeldLockRepository("guard_held")

    result = run_weekly_workflow(config, output_dir, adapters=adapters)

    assert result.outcome == WorkflowOutcome.INTERVENTION_REQUIRED
    assert result.failed_stage == "repository-preflight"


def _locked_repository(tmp_path: Path, *, pid: int, age_seconds: float = 0) -> Path:
    repo = tmp_path / "locked-repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    lock = repo / GUARD_FILENAME
    lock.write_text(str(pid), encoding="utf-8")
    if age_seconds:
        stamp = time.time() - age_seconds
        os.utime(lock, (stamp, stamp))
    return repo


def test_a_scheduled_run_skips_on_the_real_run_lock_before_reading_or_writing_anything(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path)
    repo = _locked_repository(tmp_path, pid=os.getpid())
    adapters.repository = GitRepositoryAdapter(repo)
    before = {path: path.read_bytes() for path in output_dir.rglob("*") if path.is_file()}

    result = WeeklyWorkflow(adapters, scheduled=True).run(config, output_dir)

    assert result.outcome == WorkflowOutcome.SKIPPED_RUN_IN_PROGRESS
    assert {path: path.read_bytes() for path in output_dir.rglob("*") if path.is_file()} == before
    # The lock belongs to the run in progress and stays.
    assert (repo / GUARD_FILENAME).read_text(encoding="utf-8") == str(os.getpid())


@pytest.mark.parametrize(
    "repository", [_HeldLockRepository("guard_held", owner_alive=False), _UnverifiedLockRepository()]
)
def test_a_scheduled_run_does_not_skip_on_a_lock_whose_owner_is_not_verifiably_alive(tmp_path, repository):
    config, output_dir, adapters = _fixture_run(tmp_path)
    adapters.repository = repository

    result = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)

    assert result.outcome == WorkflowOutcome.INTERVENTION_REQUIRED
    assert result.failed_stage == "repository-preflight"


@pytest.mark.parametrize("content", ["99999999", "not-a-pid", ""])
def test_a_young_lock_of_a_dead_or_unreadable_owner_is_refused_and_reported_not_skipped(
    tmp_path, monkeypatch, capsys, content
):
    """A run that crashed less than an hour before the timer must not cost a silent night."""
    import autoscout.cli.weekly as cli

    config, output_dir, adapters = _fixture_run(tmp_path)
    repo = _locked_repository(tmp_path, pid=0)
    (repo / GUARD_FILENAME).write_text(content, encoding="utf-8")
    adapters.repository = GitRepositoryAdapter(repo)
    monkeypatch.setattr(weekly_workflow, "weekly_adapters", lambda *args, **kwargs: adapters)
    transport = _Transport()

    code = cli._run_weekly_command(config, output_dir, scheduled=True, report_transport=transport)

    assert code == 1
    assert len(transport.sent) == 1
    assert "repository-preflight" in transport.sent[0][1] and "guard_held" in transport.sent[0][1]
    assert "Остановлено на этапе repository-preflight, причина: guard_held." in capsys.readouterr().out
    # The young lock is left to the stale-takeover rule, unchanged.
    assert (repo / GUARD_FILENAME).read_text(encoding="utf-8") == content


def test_a_lock_held_by_another_user_counts_as_alive(tmp_path):
    repo = _locked_repository(tmp_path, pid=12345)

    def eperm(pid: int, signal: int) -> None:
        raise PermissionError

    assert GitRepositoryAdapter(repo).held_by_live_run(pid_probe=eperm) is True


def test_a_scheduled_run_takes_over_a_stale_lock_of_a_dead_process(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path)
    repo = _locked_repository(tmp_path, pid=99999999, age_seconds=7200)
    adapters.repository = GitRepositoryAdapter(repo)

    result = WeeklyWorkflow(adapters, scheduled=True).run(config, output_dir)

    # Not skipped: the run goes on to judge the repository, which has no commit or upstream here.
    assert result.outcome == WorkflowOutcome.INTERVENTION_REQUIRED
    assert result.failed_stage in {"repository-preflight", "repository-update"}
    assert not (repo / GUARD_FILENAME).exists()


# ── The Kirkus pass of 01:30 seen from the run of 02:30 ────────────────────


def _record_attempt(config: Path, status: str) -> None:
    path = attempt_path(config, configured_source(load_validated_config(config)))
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"attempted_date": REFERENCE.isoformat(), "status": status}), encoding="utf-8")


def test_the_run_neither_starts_nor_waits_for_a_pass_still_running(tmp_path):
    config = _kirkus_run_config(tmp_path)
    _record_attempt(config, "started")
    stage = _Pass("before")
    adapters = _kirkus_adapters(stage)
    adapters.codex = deterministic_adapters().codex

    result = WeeklyWorkflow(adapters, scheduled=True).run(config, tmp_path / "run")

    assert stage.calls == 0
    assert adapters.capture.calls == ["all"]
    assert result.outcome.value.startswith("completed/"), result.failed_stage


def test_the_run_reads_the_result_of_the_pass_that_finished_before_it(tmp_path):
    config = _kirkus_run_config(tmp_path)
    fetch, _, _ = site()
    early = weekly_stage(config, tmp_path / "run", REFERENCE, fetch=fetch, pause=lambda seconds: None)
    assert early["status"] == "collected"
    stage = _Pass("before")
    stage.main_finished.set()
    stage.finished.set()
    adapters = _kirkus_adapters(stage)

    result = WeeklyWorkflow(adapters, scheduled=True).run(config, tmp_path / "run")

    assert stage.calls == 0
    assert adapters.capture.calls == ["all"]
    assert result.outcome.value.startswith("completed/"), result.failed_stage
    candidates = WeeklyReportStore(tmp_path / "run").read("scouting-candidates.json", default=[])
    assert any(candidate["title"] == "THE BULLWHIP'S ALGORITHM" for candidate in candidates)


# ── The scheduled Kirkus command ───────────────────────────────────────────


class _Transport:
    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.error = error

    def __call__(self, subject: str, body: str) -> None:
        self.sent.append((subject, body))
        if self.error is not None:
            raise self.error


def _no_report(monkeypatch) -> None:
    """The Kirkus pass sends nothing of its own: any delivery fails the test."""
    from autoscout import operator_notice

    def refuse(*args, **kwargs):
        raise AssertionError("the Kirkus pass sent a report")

    monkeypatch.setattr(operator_notice, "deliver", refuse)


def _kirkus_command(tmp_path, monkeypatch, outcome, *, scheduled=True, attempt=None):
    from autoscout import kirkus_precollection

    config = _pass_config(tmp_path)
    if attempt is not None:
        _record_attempt(config, attempt)
    calls = []

    def collect_pass(source, path, attempt, day, fetch, pause):
        calls.append(day)
        if isinstance(outcome, Exception):
            raise outcome
        return {"status": outcome}

    # The pass itself, inside the pass lock and the day check of run_pass.
    monkeypatch.setattr(kirkus_precollection, "_collect_pass", collect_pass)
    _no_report(monkeypatch)
    code = kirkus_precollection.collect_kirkus_command(config, scheduled=scheduled, today=REFERENCE)
    return code, calls


@pytest.mark.parametrize("attempt", ["started", "collected", "partial", "not_collected"])
def test_a_scheduled_pass_is_skipped_silently_when_a_pass_started_that_day(tmp_path, monkeypatch, capsys, attempt):
    code, calls = _kirkus_command(tmp_path, monkeypatch, "collected", attempt=attempt)

    assert code == 0 and calls == []
    assert len(capsys.readouterr().out.strip().splitlines()) == 1


def test_a_manual_pass_still_runs_after_a_pass_that_day(tmp_path, monkeypatch):
    code, calls = _kirkus_command(tmp_path, monkeypatch, "collected", scheduled=False, attempt="collected")

    assert code == 0 and calls == [REFERENCE]


def test_a_scheduled_pass_that_collects_sends_nothing(tmp_path, monkeypatch):
    code, calls = _kirkus_command(tmp_path, monkeypatch, "collected")

    assert code == 0 and calls == [REFERENCE]


@pytest.mark.parametrize(
    ("error", "expected_code", "reason"),
    [
        (KirkusError("kirkus_listing_refused", "the listing answered 403"), 2, "kirkus_listing_refused"),
        (RuntimeError(f"crashed on {BOOK_TITLE}"), 1, "unexpected_error"),
    ],
)
def test_a_failed_scheduled_pass_prints_its_failure_and_sends_nothing(
    tmp_path, monkeypatch, capsys, error, expected_code, reason
):
    code, _ = _kirkus_command(tmp_path, monkeypatch, error)

    assert code == expected_code
    out = capsys.readouterr().out
    assert f"Остановлено на этапе kirkus-precollection, причина: {reason}." in out
    assert BOOK_TITLE not in out


# ── The morning report of the scheduled run ────────────────────────────────

RUN_DATE = "2026-11-14"


def _failed_result(stage: str = STAGE_ZERO_NAME) -> WeeklyWorkflowResult:
    return WeeklyWorkflowResult(
        outcome=WorkflowOutcome.INTERVENTION_REQUIRED,
        failed_stage=stage,
        log=[{"stage": stage, "status": "failed", "reason": f"Codex usage limit while reading {BOOK_TITLE}"}],
        run_summary={"toolchain_preflight": {"codex": {"reason_code": "codex_usage_limit"}}},
    )


def _attention_result() -> WeeklyWorkflowResult:
    return WeeklyWorkflowResult(
        outcome=WorkflowOutcome.COMPLETED_ATTENTION_REQUIRED,
        attention_reasons=("sources", "descriptions"),
        delta_counts={"new": 4, "upgraded": 1, "changed": 1, "unchanged": 30},
        description_coverage={"validated_count": 35, "fallback_count": 5, "total_candidates": 40},
        source_problems=[
            {"source_name": "Kirkus", "status": "failed", "editor_detail": BOOK_TITLE, "title": BOOK_TITLE},
            {"source_name": "NPR", "status": "skipped"},
            {"source_name": "BookPage", "status": "unusual_count", "editor_detail": "3 вместо 20"},
        ],
        description_stage_problems=[{"stage": "grounded", "status": "failed", "title": BOOK_TITLE}],
    )


def _clean_result() -> WeeklyWorkflowResult:
    return WeeklyWorkflowResult(
        outcome=WorkflowOutcome.COMPLETED_NO_ATTENTION,
        delta_counts={"new": 7, "upgraded": 2, "changed": 1},
        description_coverage={"validated_count": 40, "fallback_count": 2, "total_candidates": 42},
    )


def _run_command(monkeypatch, result_or_error, *, scheduled=True, kirkus=None):
    import autoscout.cli.weekly as cli
    from autoscout import kirkus_precollection

    monkeypatch.setattr(weekly_workflow, "weekly_adapters", lambda *args, **kwargs: types.SimpleNamespace())
    monkeypatch.setattr(kirkus_precollection, "last_attempt", lambda config_path: kirkus)

    def fake_run(*args, **kwargs):
        if isinstance(result_or_error, BaseException):
            raise result_or_error
        assert kwargs.get("scheduled", False) is scheduled
        return result_or_error

    monkeypatch.setattr(cli, "run_weekly_workflow", fake_run)
    transport = _Transport()
    code = cli._run_weekly_command(
        Path("config.json"), Path("out"), run_date=RUN_DATE, scheduled=scheduled, report_transport=transport
    )
    return code, transport


def _the_report(transport: _Transport) -> tuple[str, str]:
    assert len(transport.sent) == 1, transport.sent
    subject, body = transport.sent[0]
    assert BOOK_TITLE not in subject + body
    assert body.endswith("Журнал: journalctl --user -u autoscout-run.service")
    return subject, body


def test_a_clean_scheduled_run_sends_one_report_saying_successful(monkeypatch):
    kirkus = {"attempted_date": RUN_DATE, "status": "collected", "reason_code": ""}
    code, transport = _run_command(monkeypatch, _clean_result(), kirkus=kirkus)

    assert code == 0
    subject, body = _the_report(transport)
    assert subject == f"AutoScout: ночной поиск {RUN_DATE} — успешно"
    assert body.splitlines()[:4] == [
        f"Ночной поиск {RUN_DATE} завершён успешно.",
        "Новых книг: 7. Обновлено книг: 3.",
        "Описаний на русском: 40 из 42.",
        "Сбор Kirkus: собран.",
    ]


def test_an_attention_required_scheduled_run_sends_one_report_naming_sources_without_titles(monkeypatch):
    code, transport = _run_command(monkeypatch, _attention_result())

    assert code == 0
    subject, body = _the_report(transport)
    assert subject == f"AutoScout: ночной поиск {RUN_DATE} — требует внимания"
    assert f"Ночной поиск {RUN_DATE} завершён, но требует внимания: источники, описания." in body
    assert (
        "Источники с проблемами: Kirkus (не удалось проверить), NPR (пропущено), BookPage (необычное число книг)."
        in body
    )
    assert "Описания с проблемами: 1." in body and "3 вместо 20" not in body


def test_a_stopped_scheduled_run_sends_one_report_naming_the_stage(monkeypatch, capsys):
    code, transport = _run_command(monkeypatch, _failed_result())

    assert code == 1
    subject, body = _the_report(transport)
    assert subject == f"AutoScout: ночной поиск {RUN_DATE} — остановлен"
    assert f"Остановлено на этапе {STAGE_ZERO_NAME}, причина: codex_usage_limit." in body
    assert "Новых книг" not in body
    out = capsys.readouterr().out
    assert f"Остановлено на этапе {STAGE_ZERO_NAME}, причина: codex_usage_limit." in out


def test_a_skipped_scheduled_run_sends_one_report_saying_skipped(monkeypatch, capsys):
    code, transport = _run_command(monkeypatch, WeeklyWorkflowResult(outcome=WorkflowOutcome.SKIPPED_RUN_IN_PROGRESS))

    assert code == 0
    subject, body = _the_report(transport)
    assert subject == f"AutoScout: ночной поиск {RUN_DATE} — пропущен"
    assert body.splitlines()[0] == f"Ночной поиск {RUN_DATE} пропущен: в это время уже шёл другой поиск."
    from autoscout.russian_workflow import scheduled_run_skipped_message

    assert capsys.readouterr().out.splitlines()[0] == scheduled_run_skipped_message()


@pytest.mark.parametrize(
    ("error", "code", "reason"),
    [(RuntimeError(f"broke on {BOOK_TITLE}"), 1, "unexpected_error"), (SystemExit(1), 1, "config_invalid")],
)
def test_an_exception_or_config_exit_in_a_scheduled_run_sends_one_stopped_report(
    monkeypatch, capsys, error, code, reason
):
    exit_code, transport = _run_command(monkeypatch, error)

    assert exit_code == code
    subject, body = _the_report(transport)
    assert subject.endswith("— остановлен")
    assert f"Остановлено на этапе unknown, причина: {reason}." in body
    assert BOOK_TITLE not in capsys.readouterr().out


@pytest.mark.parametrize(
    "result",
    [_clean_result(), _failed_result(), _attention_result(), RuntimeError("manual failure")],
)
def test_a_manual_run_never_sends_a_report(monkeypatch, result):
    _, transport = _run_command(monkeypatch, result, scheduled=False)

    assert transport.sent == []


@pytest.mark.parametrize(
    ("attempt", "line"),
    [
        (None, "Сбор Kirkus: источник не настроен."),
        ({}, "Сбор Kirkus: сегодня не проводился."),
        ({"attempted_date": "2026-11-13", "status": "collected"}, "Сбор Kirkus: сегодня не проводился."),
        ({"attempted_date": RUN_DATE, "status": "collected", "reason_code": ""}, "Сбор Kirkus: собран."),
        (
            {"attempted_date": RUN_DATE, "status": "partial", "reason_code": "kirkus_review_refused"},
            "Сбор Kirkus: прерван на полпути (kirkus_review_refused), следующий сбор продолжит.",
        ),
        (
            {"attempted_date": RUN_DATE, "status": "not_collected", "reason_code": "kirkus_listing_refused"},
            "Сбор Kirkus: не удался, причина: kirkus_listing_refused. Поиск взял последний готовый результат.",
        ),
        (
            {"attempted_date": RUN_DATE, "status": "not_collected", "reason_code": f"refused for {BOOK_TITLE}"},
            "Сбор Kirkus: не удался, причина: unrecognized_reason. Поиск взял последний готовый результат.",
        ),
        (
            {"attempted_date": RUN_DATE, "status": "started", "reason_code": ""},
            "Сбор Kirkus: начат и не завершён (ещё идёт или был прерван).",
        ),
        ({"attempted_date": RUN_DATE, "status": "odd"}, "Сбор Kirkus: состояние неизвестно."),
    ],
)
def test_the_kirkus_line_states_the_pass_of_the_night(attempt, line):
    assert kirkus_line(attempt, RUN_DATE) == line


def test_the_report_reads_the_real_kirkus_attempt_record(tmp_path):
    from autoscout.kirkus_precollection import last_attempt

    config = _pass_config(tmp_path)
    assert last_attempt(config) == {}
    _record_attempt(config, "not_collected")
    assert last_attempt(config) == {
        "attempted_date": REFERENCE.isoformat(),
        "status": "not_collected",
        "reason_code": "",
    }
    assert last_attempt(_invalid_config(tmp_path)) is None


def test_a_transport_error_does_not_change_the_exit_code(monkeypatch, capsys):
    import autoscout.cli.weekly as cli

    monkeypatch.setattr(weekly_workflow, "weekly_adapters", lambda *args, **kwargs: types.SimpleNamespace())
    monkeypatch.setattr(cli, "run_weekly_workflow", lambda *args, **kwargs: _failed_result())
    failing = _Transport(OSError("535 login refused for password hunter2"))

    code = cli._run_weekly_command(Path("config.json"), Path("out"), scheduled=True, report_transport=failing)

    assert code == 1
    out = capsys.readouterr().out
    assert "Отчёт не отправлен: OSError." in out
    assert "hunter2" not in out


def test_a_reason_that_is_not_a_token_is_sent_as_the_generic_reason():
    result = _failed_result(stage="capture")

    assert failed_run_reason(result) == GENERIC_REASON
    report = run_report(result, run_date=RUN_DATE, kirkus_attempt={})
    assert GENERIC_REASON in report.body and BOOK_TITLE not in report.body


def test_an_unconfigured_report_is_printed_and_not_sent(monkeypatch):
    import smtplib

    def refuse(*args, **kwargs):
        raise AssertionError("no SMTP connection without configuration")

    monkeypatch.setattr(smtplib, "SMTP", refuse)
    monkeypatch.setattr(smtplib, "SMTP_SSL", refuse)
    lines: list[str] = []

    status = deliver(Report("subject line", "body line"), environ={}, output=lines.append)

    assert status == "not_configured"
    assert lines[1:] == ["subject line", "body line"]


class _FakeSmtp:
    instances: ClassVar[list[_FakeSmtp]] = []

    def __init__(self, host: str, port: int, timeout: float, **kwargs: Any) -> None:
        self.calls: list[Any] = [("connect", host, port, timeout)]
        _FakeSmtp.instances.append(self)

    def __enter__(self) -> _FakeSmtp:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.calls.append("quit")

    def starttls(self, context: Any = None) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(("login", user))

    def send_message(self, message: Any) -> None:
        self.calls.append(("send", message["Subject"], message["To"], message.get_content().strip()))


@pytest.mark.parametrize(("port", "tls"), [("587", "starttls"), ("465", None)])
def test_the_smtp_transport_reads_its_settings_from_the_environment(monkeypatch, port, tls):
    import smtplib

    _FakeSmtp.instances = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSmtp)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSmtp)
    environ = {
        ENV_HOST: "smtp.example.test",
        ENV_PORT: port,
        ENV_USER: "robot",
        ENV_PASSWORD: "secret",
        ENV_FROM: "robot@example.test",
        ENV_TO: "operator@example.test, second@example.test",
    }

    assert deliver(Report("subject", "body"), environ=environ, output=lambda line: None) == "sent"

    calls = _FakeSmtp.instances[0].calls
    assert calls[0] == ("connect", "smtp.example.test", int(port), 15)
    assert ("starttls" in calls) == (tls is not None)
    assert ("login", "robot") in calls
    assert ("send", "subject", "operator@example.test, second@example.test", "body") in calls


def test_the_run_launcher_forwards_its_arguments(tmp_path):
    from tests.test_issue_270_271_launcher_and_codex_permission import _FakeRepo

    repo = _FakeRepo(tmp_path)
    repo.add_venv_python()

    result = subprocess.run(
        [str(repo.launcher), "--scheduled"], capture_output=True, text=True, cwd=str(repo.repo_root), check=False
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.split()[-1] == "--scheduled"


# ── A configuration that fails validation (SystemExit) ─────────────────────


def _invalid_config(tmp_path: Path) -> Path:
    config = tmp_path / "configs" / "invalid.json"
    config.parent.mkdir(exist_ok=True)
    config.write_text(json.dumps({"sources": "not a list"}), encoding="utf-8")
    return config


def test_a_scheduled_pass_with_an_invalid_config_prints_its_failure_and_sends_nothing(tmp_path, monkeypatch, capsys):
    from autoscout import kirkus_precollection

    _no_report(monkeypatch)

    code = kirkus_precollection.collect_kirkus_command(_invalid_config(tmp_path), scheduled=True, today=REFERENCE)

    assert code == 1
    assert "Остановлено на этапе kirkus-precollection, причина: config_invalid." in capsys.readouterr().out


def test_a_config_exit_inside_a_scheduled_pass_keeps_its_code_and_sends_nothing(tmp_path, monkeypatch):
    from autoscout import kirkus_precollection

    def exits(*_args):
        raise SystemExit(3)

    monkeypatch.setattr(kirkus_precollection, "_collect_pass", exits)
    _no_report(monkeypatch)

    code = kirkus_precollection.collect_kirkus_command(_pass_config(tmp_path), scheduled=True, today=REFERENCE)

    assert code == 3


def test_a_manual_pass_with_an_invalid_config_exits_as_before(tmp_path):
    from autoscout import kirkus_precollection

    with pytest.raises(SystemExit) as raised:
        kirkus_precollection.collect_kirkus_command(_invalid_config(tmp_path), today=REFERENCE)

    assert raised.value.code == 1


def _run_with_invalid_config(tmp_path, monkeypatch, *, scheduled):
    import autoscout.cli.weekly as cli

    monkeypatch.setattr(weekly_workflow, "weekly_adapters", lambda *args, **kwargs: deterministic_adapters())
    transport = _Transport()
    code = cli._run_weekly_command(
        _invalid_config(tmp_path), tmp_path / "run", scheduled=scheduled, report_transport=transport
    )
    return code, transport


def test_a_scheduled_run_with_an_invalid_config_sends_one_stopped_report(tmp_path, monkeypatch, capsys):
    code, transport = _run_with_invalid_config(tmp_path, monkeypatch, scheduled=True)

    assert code == 1
    assert len(transport.sent) == 1 and "config_invalid" in transport.sent[0][1]
    assert transport.sent[0][0].endswith("— остановлен")
    assert "Сбор Kirkus: источник не настроен." in transport.sent[0][1]
    assert "Остановлено на этапе unknown, причина: config_invalid." in capsys.readouterr().out


def test_a_manual_run_with_an_invalid_config_exits_as_before(tmp_path, monkeypatch):
    with pytest.raises(SystemExit) as raised:
        _run_with_invalid_config(tmp_path, monkeypatch, scheduled=False)

    assert raised.value.code == 1


def test_a_clean_system_exit_in_a_scheduled_run_is_not_a_failure(monkeypatch):
    import autoscout.cli.weekly as cli

    def exits_cleanly(*args, **kwargs):
        raise SystemExit(0)

    monkeypatch.setattr(weekly_workflow, "weekly_adapters", lambda *args, **kwargs: types.SimpleNamespace())
    monkeypatch.setattr(cli, "run_weekly_workflow", exits_cleanly)
    transport = _Transport()

    with pytest.raises(SystemExit) as raised:
        cli._run_weekly_command(Path("config.json"), Path("out"), scheduled=True, report_transport=transport)

    assert raised.value.code == 0 and transport.sent == []


# ── The residue of a Stage 0 failure: the log and the run summary ──────────


def _write_summary(log: Path, **changes: Any) -> Path:
    summary = log.parent / "run-summary.json"
    summary.write_text(json.dumps({**COMMITTED_SUMMARY, **changes}, indent=2) + "\n", encoding="utf-8")
    return summary


FAILED_TOOLCHAIN = {"overall_status": "fail", "codex": {"status": "fail", "reason_code": "codex_usage_limit"}}


def test_the_log_and_a_summary_changed_only_in_its_toolchain_key_are_set_aside_together(tmp_path):
    repo, log = _repo_with_remote(tmp_path)
    log.write_text(FAILED_LOG, encoding="utf-8")
    summary = _write_summary(log, toolchain_preflight=FAILED_TOOLCHAIN)
    failed_summary = summary.read_text(encoding="utf-8")
    assert _default_repo_inspect(repo, "main", None, log.parent)(None)["status"] == "ok"
    adapter = _adapter(repo, log)

    result = adapter.preflight()
    adapter.release_guard()

    assert result["status"] == "ok", result
    assert result["log_residue_files"] == ["run-summary.json", "workflow-log.json"]
    assert _git(repo, "status", "--porcelain") == ""
    folder = repo / LOG_RESIDUE_DIR
    (log_copy,) = folder.glob("workflow-log-*.json")
    (summary_copy,) = folder.glob("run-summary-*.json")
    assert log_copy.name.removeprefix("workflow-log-") == summary_copy.name.removeprefix("run-summary-")
    assert log_copy.read_text(encoding="utf-8") == FAILED_LOG
    assert summary_copy.read_text(encoding="utf-8") == failed_summary


def _blocks_and_restores_nothing(repo: Path, log: Path) -> None:
    before = {path: path.read_bytes() for path in log.parent.iterdir()}
    adapter = _adapter(repo, log)
    result = adapter.preflight()
    adapter.release_guard()
    assert result["status"] == "unsafe" and "dirty_working_tree" in result["reasons"], result
    assert {path: path.read_bytes() for path in log.parent.iterdir()} == before
    assert not (repo / LOG_RESIDUE_DIR).exists()
    assert "dirty_working_tree" in _default_repo_inspect(repo, "main", None, log.parent)(None)["reasons"]


def test_a_summary_with_another_changed_key_blocks_with_the_log(tmp_path):
    repo, log = _repo_with_remote(tmp_path)
    log.write_text(FAILED_LOG, encoding="utf-8")
    _write_summary(log, toolchain_preflight=FAILED_TOOLCHAIN, editor_facing_count=4)

    _blocks_and_restores_nothing(repo, log)


def test_a_summary_modified_alone_blocks(tmp_path):
    repo, log = _repo_with_remote(tmp_path)
    _write_summary(log, toolchain_preflight=FAILED_TOOLCHAIN)

    _blocks_and_restores_nothing(repo, log)


def test_a_staged_summary_blocks_with_the_log(tmp_path):
    repo, log = _repo_with_remote(tmp_path)
    log.write_text(FAILED_LOG, encoding="utf-8")
    summary = _write_summary(log, toolchain_preflight=FAILED_TOOLCHAIN)
    _git(repo, "add", str(summary.relative_to(repo)))

    _blocks_and_restores_nothing(repo, log)


def test_a_summary_that_does_not_parse_blocks_with_the_log(tmp_path):
    repo, log = _repo_with_remote(tmp_path)
    log.write_text(FAILED_LOG, encoding="utf-8")
    (log.parent / "run-summary.json").write_text("{not json", encoding="utf-8")

    _blocks_and_restores_nothing(repo, log)


def test_retention_counts_residue_sets_and_leaves_no_orphan(tmp_path):
    repo, log = _repo_with_remote(tmp_path)
    folder = repo / LOG_RESIDUE_DIR
    folder.mkdir(parents=True)
    for day in range(1, LOG_RESIDUE_KEEP + 1):
        stamp = f"202601{day:02d}T000000000000Z"
        (folder / f"workflow-log-{stamp}.json").write_text("[]", encoding="utf-8")
        (folder / f"run-summary-{stamp}.json").write_text("{}", encoding="utf-8")
    log.write_text(FAILED_LOG, encoding="utf-8")
    adapter = _adapter(repo, log)

    assert adapter.preflight()["status"] == "ok"
    adapter.release_guard()

    logs = {path.name.removeprefix("workflow-log-") for path in folder.glob("workflow-log-*.json")}
    summaries = {path.name.removeprefix("run-summary-") for path in folder.glob("run-summary-*.json")}
    assert len(logs) == LOG_RESIDUE_KEEP
    assert "20260101T000000000000Z.json" not in logs | summaries
    assert summaries <= logs


def _probe(status: str) -> Any:
    def probe() -> dict[str, Any]:
        healthy = status == "pass"
        return {
            "status": status,
            "reason_code": "codex_healthy" if healthy else "codex_usage_limit",
            "message": "Codex is healthy" if healthy else "Codex usage limit reached",
            "model": "",
            "cli_version": "",
            "detail": {},
        }

    return probe


def test_a_night_after_a_stage_zero_failure_is_not_blocked(tmp_path):
    repo, log = _repo_with_remote(tmp_path)
    output_dir = log.parent
    config = _write_codex_fixture_config(repo)
    _write_capture_fixture(repo, output_dir)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "config")
    _git(repo, "push")

    def adapters(status: str) -> Any:
        bundle = deterministic_adapters()
        bundle.repository = GitRepositoryAdapter(repo, expected_branch="main")
        bundle.stage_zero_codex_probe = _probe(status)
        return bundle

    first = run_weekly_workflow(config, output_dir, adapters=adapters("fail"), scheduled=True)
    assert first.failed_stage == STAGE_ZERO_NAME
    status = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo), capture_output=True, text=True, check=True)
    assert sorted(status.stdout.splitlines()) == [" M runs/weekly/run-summary.json", " M runs/weekly/workflow-log.json"]

    second = run_weekly_workflow(config, output_dir, adapters=adapters("pass"), scheduled=True)

    assert second.failed_stage != "repository-preflight", second.log
    residue = [entry for entry in second.log if entry.get("stage") == "log-residue"]
    assert len(residue) == 1 and residue[0]["status"] == "set_aside"
    assert residue[0]["detail"]["files"] == ["run-summary.json", "workflow-log.json"]
    (summary_copy,) = (repo / LOG_RESIDUE_DIR).glob("run-summary-*.json")
    archived = json.loads(summary_copy.read_text(encoding="utf-8"))
    assert archived["toolchain_preflight"]["codex"]["reason_code"] == "codex_usage_limit"
    assert len(list((repo / LOG_RESIDUE_DIR).glob("workflow-log-*.json"))) == 1


# ── The Kirkus pass lock ───────────────────────────────────────────────────


def _lock_for(config: Path) -> Path:
    from autoscout.kirkus_precollection import pass_lock_path

    return pass_lock_path(config, configured_source(load_validated_config(config)))


def test_two_passes_racing_on_the_lock_make_exactly_one_pass(tmp_path, monkeypatch, capsys):
    import threading

    from autoscout import kirkus_precollection
    from autoscout.kirkus_precollection import PASS_IN_PROGRESS
    from autoscout.kirkus_precollection import run_pass

    config = _pass_config(tmp_path)
    fetch, requests, _ = site()
    inside, release = threading.Event(), threading.Event()

    def slow_fetch(url):
        inside.set()
        assert release.wait(5)
        return fetch(url)

    first: dict[str, Any] = {}
    worker = threading.Thread(
        target=lambda: first.update(run_pass(config, reference=REFERENCE, fetch=slow_fetch, pause=lambda s: None))
    )
    worker.start()
    assert inside.wait(5)
    try:
        with pytest.raises(KirkusError) as second:
            run_pass(config, reference=REFERENCE, fetch=fetch, pause=lambda s: None)
        assert second.value.reason_code == PASS_IN_PROGRESS
        assert kirkus_precollection.collect_kirkus_command(config, scheduled=True, today=REFERENCE) == 0
        assert kirkus_precollection.collect_kirkus_command(config, today=REFERENCE) == 2
        out = capsys.readouterr().out
        assert kirkus_precollection.SCHEDULED_SKIP_RUNNING in out and PASS_IN_PROGRESS in out
        assert weekly_stage(config, tmp_path / "run", REFERENCE, fetch=fetch, pause=lambda s: None)["status"] == (
            "pass_in_progress"
        )
    finally:
        release.set()
        worker.join(5)
    assert first["status"] == "collected"
    listing_requests = [url for url in requests if "book-reviews" not in url]
    assert len(listing_requests) == len(set(listing_requests))
    assert not pass_lock_held(_lock_for(config))


def test_two_holders_in_one_process_cannot_both_hold_the_pass_lock(tmp_path):
    from autoscout.kirkus_precollection import _pass_lock

    lock = tmp_path / "data" / "kirkus-collection-pass.lock"

    with _pass_lock(lock) as first:
        assert first is True and pass_lock_held(lock)
        with _pass_lock(lock) as second:
            assert second is False
    assert not pass_lock_held(lock)
    with _pass_lock(lock) as again:
        assert again is True
    assert lock.exists()


_HOLDER = """
import fcntl, os, sys, time
descriptor = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o644)
fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
print("held", flush=True)
time.sleep(60)
"""


@pytest.mark.parametrize("ending", ["exit", "kill"])
def test_a_lock_held_by_another_process_is_free_as_soon_as_that_process_ends(tmp_path, ending):
    from autoscout.kirkus_precollection import PASS_IN_PROGRESS
    from autoscout.kirkus_precollection import run_pass

    config = _pass_config(tmp_path)
    lock = _lock_for(config)
    lock.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen([sys.executable, "-c", _HOLDER, str(lock)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        fetch, requests, _ = site()
        assert pass_lock_held(lock)
        with pytest.raises(KirkusError) as refused:
            run_pass(config, reference=REFERENCE, fetch=fetch, pause=lambda s: None)
        assert refused.value.reason_code == PASS_IN_PROGRESS and requests == []
    finally:
        if ending == "kill":
            holder.kill()
        else:
            holder.terminate()
        holder.wait(10)

    # The kernel let the lock go with the process; the file itself stays.
    assert lock.exists() and not pass_lock_held(lock)
    assert run_pass(config, reference=REFERENCE, fetch=fetch, pause=lambda s: None)["status"] == "collected"


def test_the_lock_is_released_after_a_failed_pass(tmp_path):
    from autoscout.kirkus_listing import listing_page_url
    from autoscout.kirkus_precollection import run_pass

    config = _pass_config(tmp_path)
    fetch, _, _ = site(refuse=(listing_page_url(1),))

    with pytest.raises(KirkusError):
        run_pass(config, reference=REFERENCE, fetch=fetch, pause=lambda s: None)
    assert not pass_lock_held(_lock_for(config))


def test_the_run_neither_starts_nor_waits_for_a_pass_holding_the_lock(tmp_path):
    from autoscout.kirkus_precollection import _pass_lock
    from autoscout.kirkus_precollection import kirkus_is_due

    config = _kirkus_run_config(tmp_path)
    stage = _Pass("before")
    adapters = _kirkus_adapters(stage)
    adapters.codex = deterministic_adapters().codex

    with _pass_lock(_lock_for(config)) as held:
        assert held and not kirkus_is_due(config, REFERENCE)
        result = WeeklyWorkflow(adapters, scheduled=True).run(config, tmp_path / "run")

    assert stage.calls == 0
    assert adapters.capture.calls == ["all"]
    assert result.outcome.value.startswith("completed/"), result.failed_stage


# ── A scheduled run whose update pulled new code (#1117) ──────────────────


class _PullingRepository(_ResidueRepository):
    def __init__(self) -> None:
        super().__init__()
        self.released = 0

    def update(self, validated_artifact_allowlist=None):
        return {"status": "ok", "pulled": True, "updated": 30}

    def release_guard(self):
        self.released += 1


def test_a_scheduled_run_whose_update_pulled_code_stops_before_writing_and_releases_the_lock(tmp_path, monkeypatch):
    monkeypatch.delenv(weekly_workflow.RESTARTED_ENV, raising=False)
    config, output_dir, adapters = _fixture_run(tmp_path)
    adapters.repository = _PullingRepository()
    log = output_dir / "workflow-log.json"
    before = log.read_bytes() if log.exists() else None

    result = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)

    assert result.outcome == WorkflowOutcome.RESTART_ON_UPDATED_CODE
    assert (log.read_bytes() if log.exists() else None) == before
    assert adapters.repository.released == 1


def test_a_restarted_or_manual_run_goes_on_after_pulling_code(tmp_path, monkeypatch):
    config, output_dir, adapters = _fixture_run(tmp_path)
    adapters.repository = _PullingRepository()
    assert run_weekly_workflow(config, output_dir, adapters=adapters).outcome.value.startswith("completed/")
    monkeypatch.setenv(weekly_workflow.RESTARTED_ENV, "1")
    result = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)
    assert result.outcome.value.startswith("completed/"), result.failed_stage


def test_the_command_restarts_a_run_on_updated_code_and_sends_no_report(monkeypatch):
    import autoscout.cli.weekly as cli

    monkeypatch.setattr(weekly_workflow, "weekly_adapters", lambda *args, **kwargs: types.SimpleNamespace())
    monkeypatch.setattr(
        cli,
        "run_weekly_workflow",
        lambda *a, **k: WeeklyWorkflowResult(outcome=WorkflowOutcome.RESTART_ON_UPDATED_CODE),
    )
    restarts, transport = [], _Transport()
    code = cli._run_weekly_command(
        Path("config.json"),
        Path("out"),
        run_date=RUN_DATE,
        scheduled=True,
        report_transport=transport,
        restart=lambda: restarts.append(1),
    )
    assert (code, restarts, transport.sent) == (0, [1], [])
