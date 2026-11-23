"""The leftovers of a run that failed late are carried and published by the next run (#1161).

A run that fails after it saved its state, where saving the results starts,
leaves the run directory changed. The next run proves the directory holds
that run's leftovers, marks it as awaiting publication, carries everything,
and its finalization publishes it. Nothing is restored, removed or moved.
Anything it cannot prove still blocks, untouched.
"""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import pytest

import autoscout.git_envelope as envelope
from autoscout import weekly_workflow
from autoscout.doctor import DoctorSeams
from autoscout.doctor import diagnose
from autoscout.doctor import render_russian
from autoscout.git_envelope import LOG_RESIDUE_DIR
from autoscout.git_envelope import GitRepositoryAdapter
from autoscout.late_failure_leftovers import AWAITING_PUBLICATION_DIR
from autoscout.late_failure_leftovers import LATE_FAILURE_LEFTOVERS_STAGE
from autoscout.late_failure_leftovers import STATE_UNVERIFIED
from autoscout.late_failure_leftovers import LeftoverScope
from autoscout.late_failure_leftovers import leftover_scope
from autoscout.mvp_run_evidence import capture_is_current
from autoscout.operator_notice import run_report
from autoscout.state_store import EXPORT_CURRENT
from autoscout.state_store import SQLITE_BACKEND
from autoscout.state_store import STATE_BACKEND_ENV
from autoscout.state_store import Collection
from autoscout.state_store import export_json
from autoscout.state_store import inspect_state_store
from autoscout.state_store import open_state_store
from autoscout.state_store.sqlite_location import DATABASE_DIR_ENV
from autoscout.weekly_workflow import WeeklyWorkflowResult
from autoscout.weekly_workflow import WorkflowOutcome
from autoscout.weekly_workflow import deterministic_adapters
from autoscout.weekly_workflow import run_weekly_workflow
from tests.test_issue_1057_nightly_run import _git
from tests.test_issue_1057_nightly_run import _repo_with_remote
from tests.test_weekly_workflow import _write_capture_fixture
from tests.test_weekly_workflow import _write_codex_fixture_config
from tests.test_weekly_workflow import _write_extraction_fixture

FIRST_NIGHT = date(2026, 11, 10)
SECOND_NIGHT = date(2026, 11, 11)
THIRD_NIGHT = date(2026, 11, 12)
HIDDEN_BOOK = "editor hid this book|after the failed night"
SEARCH_OPTIMIZATION_REVIEW = "search-optimization-review.md"


class _Clock:
    def __init__(self, day: date) -> None:
        self._day = day

    def today(self) -> date:
        return self._day


def _status(repo: Path) -> list[str]:
    out = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(line for line in out.splitlines() if line)


def _adapters(repo: Path, day: date) -> Any:
    bundle = deterministic_adapters()
    bundle.repository = GitRepositoryAdapter(repo, expected_branch="main")
    bundle.clock = _Clock(day)
    return bundle


def _fail_late(monkeypatch: pytest.MonkeyPatch) -> None:
    """The run fails after its state is exported, where saving the results starts, having written a late report."""

    def fail(self: Any, run: Any) -> WeeklyWorkflowResult:
        (run.output_dir / "late-report.json").write_text(json.dumps({"at": run.log[-1]["at"]}), encoding="utf-8")
        return weekly_workflow._bad_outcome(
            WorkflowOutcome.INTERVENTION_REQUIRED,
            "artifact-secret-scan",
            run.log,
            log_path=run.log_path,
            reason="simulated_late_failure",
        )

    monkeypatch.setattr(weekly_workflow.WeeklyWorkflow, "_scan_artifacts_for_secrets", fail)


class _Night:
    """A checkout with a committed run, whose first night failed late."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        state_dir: bool = False,
        committed: str = "",
        published_first: bool = False,
        extracted: bool = False,
    ) -> None:
        self.repo, log = _repo_with_remote(tmp_path)
        self.output_dir = log.parent
        self.log = log
        self.state_dir = self.output_dir if state_dir else None
        self.config = _write_codex_fixture_config(self.repo)
        _write_capture_fixture(self.repo, self.output_dir)
        if committed:
            (self.output_dir / committed).write_text("committed earlier\n", encoding="utf-8")
        if extracted:
            # A capture and a valid extraction of the date, so a later start that
            # date finds the capture current and takes the same-operation resume path.
            deterministic_adapters().capture.prepare(
                self.config, self.output_dir, live=False, effective_date=FIRST_NIGHT.isoformat()
            )
            _write_extraction_fixture(self.output_dir)
        (self.repo / ".gitignore").write_text(
            f"/{LOG_RESIDUE_DIR.as_posix()}/\n/{AWAITING_PUBLICATION_DIR.as_posix()}/\nprogress-snapshot.json\n",
            encoding="utf-8",
        )
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-m", "config")
        _git(self.repo, "push")
        if published_first:
            # An earlier run of the same date completed and published its end.
            assert _completed(self.run(FIRST_NIGHT))
        with monkeypatch.context() as patch:
            _fail_late(patch)
            self.failed = self.run(FIRST_NIGHT)
        assert self.failed.failed_stage == "artifact-secret-scan", self.failed.log
        self.leftovers = _status(self.repo)

    def run(self, day: date) -> Any:
        return run_weekly_workflow(
            self.config, self.output_dir, self.state_dir, adapters=_adapters(self.repo, day), scheduled=True
        )

    def adapter(self) -> GitRepositoryAdapter:
        """The adapter of the next run, before its preflight."""
        adapter = GitRepositoryAdapter(self.repo, expected_branch="main", run_log_path=self.log)
        adapter.set_leftover_scope(leftover_scope(self.config, self.output_dir, self.state_dir))
        return adapter

    def preflight(self) -> dict[str, Any]:
        adapter = self.adapter()
        try:
            return adapter.preflight()
        finally:
            adapter.release_guard()

    def markers(self) -> list[Path]:
        return sorted((self.repo / AWAITING_PUBLICATION_DIR).glob("*.json"))

    def log_copies(self) -> list[Path]:
        return sorted((self.repo / LOG_RESIDUE_DIR).glob("workflow-log-*.json"))

    def snapshot(self) -> dict[str, bytes | None]:
        return {line: self._bytes(line) for line in _status(self.repo)}

    def assert_blocked_untouched(self, reason: str = "dirty_working_tree") -> None:
        """The next preflight blocks for *reason*, records nothing and leaves every file as it was."""
        before = self.snapshot()
        result = self.preflight()
        assert result["status"] == "unsafe" and reason in result["reasons"], result
        assert self.markers() == [] and self.log_copies() == []
        assert self.snapshot() == before

    def _bytes(self, status_line: str) -> bytes | None:
        path = self.repo / status_line[3:]
        return path.read_bytes() if path.is_file() and not path.is_symlink() else None


@pytest.fixture
def night(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Night:
    return _Night(tmp_path, monkeypatch)


def _completed(result: Any) -> bool:
    return result.outcome.value.startswith("completed/")


def _carried_entries(result: Any) -> list[dict[str, Any]]:
    return [entry for entry in result.log if entry.get("stage") == LATE_FAILURE_LEFTOVERS_STAGE]


# ── Carried and published ──────────────────────────────────────────────────


def test_a_late_failure_leaves_changed_reports_state_and_new_artifacts(night):
    assert " M runs/weekly/workflow-log.json" in night.leftovers
    assert {line[:2] for line in night.leftovers} == {" M", "??"}, night.leftovers


def test_the_leftovers_are_carried_and_the_next_run_publishes_them(night):
    second = night.run(SECOND_NIGHT)

    assert _completed(second), second.log
    (entry,) = _carried_entries(second)
    assert entry["status"] == "carried"
    assert entry["detail"]["earlier_operation_date"] == FIRST_NIGHT.isoformat()
    assert entry["detail"]["carried"] == len(night.leftovers)
    # Published: nothing is left behind, and nothing awaits publication.
    assert _status(night.repo) == []
    assert _git(night.repo, "rev-parse", "HEAD") == _git(night.repo, "rev-parse", "origin/main")
    assert night.markers() == []
    # The failed run's log is kept for diagnosis; nothing else is copied.
    (copy,) = night.log_copies()
    assert json.loads(copy.read_text(encoding="utf-8"))[-1]["status"] == "intervention-required"


def test_the_preflight_restores_removes_and_moves_nothing(night):
    before = night.snapshot()

    result = night.preflight()

    assert result["status"] == "ok" and result["dirty"] is False, result
    assert night.snapshot() == before
    assert len(night.markers()) == 1 and len(night.log_copies()) == 1


def test_a_file_a_person_dropped_into_the_run_directory_is_carried_and_committed(night):
    notes = night.output_dir / "editor-notes.txt"
    notes.write_text("a person's notes\n", encoding="utf-8")

    second = night.run(SECOND_NIGHT)

    assert _completed(second), second.log
    assert notes.read_text(encoding="utf-8") == "a person's notes\n"
    assert _git(night.repo, "show", "HEAD:runs/weekly/editor-notes.txt") == "a person's notes"


def test_a_carried_file_holding_a_credential_stops_the_publication(night):
    leaked = night.output_dir / "pasted.txt"
    leaked.write_text("token AKIAABCDEFGHIJKLMNOP\n", encoding="utf-8")

    second = night.run(SECOND_NIGHT)

    assert second.failed_stage == "artifact-secret-scan", second.log
    assert leaked.exists() and len(night.markers()) == 1
    assert "pasted.txt" not in _git(night.repo, "ls-tree", "-r", "--name-only", "HEAD")


def test_the_morning_report_says_the_earlier_results_were_saved(night):
    second = night.run(SECOND_NIGHT)

    body = run_report(second, run_date=SECOND_NIGHT.isoformat(), kirkus_attempt=None).body

    assert f"Сохранены и результаты незавершённого поиска {FIRST_NIGHT.isoformat()}" in body


def test_a_deletion_the_run_itself_makes_is_carried(tmp_path, monkeypatch):
    night = _Night(tmp_path, monkeypatch, committed=SEARCH_OPTIMIZATION_REVIEW)
    assert f" D runs/weekly/{SEARCH_OPTIMIZATION_REVIEW}" in night.leftovers

    second = night.run(SECOND_NIGHT)

    assert _completed(second), second.log
    assert SEARCH_OPTIMIZATION_REVIEW not in _git(night.repo, "ls-tree", "-r", "--name-only", "HEAD")


# ── Starts that stop before publishing ─────────────────────────────────────


def _fail_code_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(self: Any, run: Any) -> WeeklyWorkflowResult:
        return weekly_workflow._bad_outcome(
            WorkflowOutcome.INTERVENTION_REQUIRED,
            weekly_workflow.CODE_GATE_STAGE,
            run.log,
            log_path=run.log_path,
            reason="check_failed",
            operation_id=run.run_id,
        )

    monkeypatch.setattr(weekly_workflow.WeeklyWorkflow, "_pass_code_gate", fail)


def test_a_start_that_fails_early_reports_the_carry_and_does_not_block_the_next(night, monkeypatch):
    with monkeypatch.context() as patch:
        _fail_code_gate(patch)
        stopped = night.run(SECOND_NIGHT)
    assert stopped.failed_stage == weekly_workflow.CODE_GATE_STAGE
    assert [entry["status"] for entry in _carried_entries(stopped)] == ["carried"]
    report = run_report(stopped, run_date=SECOND_NIGHT.isoformat(), kirkus_attempt=None).body
    assert "ещё не сохранены; ничего не удалено" in report
    assert len(night.markers()) == 1

    third = night.run(THIRD_NIGHT)

    assert _completed(third), third.log
    assert _carried_entries(third)[0]["detail"]["earlier_operation_date"] == FIRST_NIGHT.isoformat()
    assert _status(night.repo) == [] and night.markers() == []


def test_a_run_restarted_on_pulled_code_carries_and_logs_the_leftovers(night, monkeypatch):
    monkeypatch.setattr(weekly_workflow.WeeklyWorkflow, "_pass_code_gate", lambda self, run: None)
    monkeypatch.delenv(weekly_workflow.RESTARTED_ENV, raising=False)

    class _Pulled(GitRepositoryAdapter):
        def update(self, validated_artifact_allowlist=None):
            return {"status": "ok", "pulled": True, "updated": 1}

    adapters = _adapters(night.repo, SECOND_NIGHT)
    adapters.repository = _Pulled(night.repo, expected_branch="main")
    first = run_weekly_workflow(night.config, night.output_dir, adapters=adapters, scheduled=True)
    assert first.outcome is WorkflowOutcome.RESTART_ON_UPDATED_CODE
    assert len(night.markers()) == 1

    restarted = night.run(SECOND_NIGHT)

    assert _completed(restarted), restarted.log
    assert len(_carried_entries(restarted)) == 1


def test_after_publication_an_ordinary_dirty_tree_blocks_again(night):
    assert _completed(night.run(SECOND_NIGHT))
    review = night.output_dir / "weekly-review.html"
    review.write_text(review.read_text(encoding="utf-8") + "<!-- hand edit -->\n", encoding="utf-8")

    result = night.preflight()

    assert result["status"] == "unsafe" and "dirty_working_tree" in result["reasons"], result
    assert night.markers() == []


def _entry(stage: str, status: str, at: str, operation: str = "x", **extra: Any) -> dict[str, Any]:
    return {"stage": stage, "status": status, "at": f"2026-11-10T{at}+00:00", "operation_id": operation, **extra}


# ── A later start on the same date ─────────────────────────────────────────


def _record_same_operation_retry(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Record whether each start took the same-operation resume path."""
    seen: list[bool] = []
    real = weekly_workflow.WeeklyWorkflow._update_repository

    def update(self: Any, run: Any) -> Any:
        seen.append(run.is_same_op_retry)
        return real(self, run)

    monkeypatch.setattr(weekly_workflow.WeeklyWorkflow, "_update_repository", update)
    return seen


def test_a_late_failure_leaves_no_current_capture_so_a_same_date_start_does_not_resume(tmp_path, monkeypatch):
    """A run past its pipeline rewrote its source runs with their final statuses, so its capture is not current."""
    night = _Night(tmp_path, monkeypatch, extracted=True)

    assert capture_is_current(night.output_dir, night.config, FIRST_NIGHT) is False


def test_a_second_start_on_the_same_date_carries_the_leftovers_and_publishes(tmp_path, monkeypatch):
    night = _Night(tmp_path, monkeypatch, extracted=True)
    seen = _record_same_operation_retry(monkeypatch)

    second = night.run(FIRST_NIGHT)

    assert _completed(second), second.log
    assert seen == [False]
    (entry,) = _carried_entries(second)
    assert entry["detail"]["earlier_operation_date"] == FIRST_NIGHT.isoformat()
    assert _status(night.repo) == [] and night.markers() == []
    assert _git(night.repo, "rev-parse", "HEAD") == _git(night.repo, "rev-parse", "origin/main")


def test_a_same_date_run_published_then_one_failed_late_then_a_third_start_is_carried(tmp_path, monkeypatch):
    night = _Night(tmp_path, monkeypatch, published_first=True, extracted=True)
    assert " M runs/weekly/workflow-log.json" in night.leftovers
    seen = _record_same_operation_retry(monkeypatch)

    third = night.run(FIRST_NIGHT)

    assert _completed(third), third.log
    assert seen == [False]
    assert len(_carried_entries(third)) == 1
    assert _status(night.repo) == [] and night.markers() == []


def test_a_resume_allowlist_and_the_carried_paths_combine(night):
    """Were a same-operation retry's capture still current, its allowlist would hide the run log; the proof sees it."""
    adapter = night.adapter()
    try:
        result = adapter.preflight(validated_artifact_allowlist={night.log.resolve()})
    finally:
        adapter.release_guard()

    assert result["status"] == "ok" and result["dirty"] is False, result
    assert len(night.markers()) == 1


def _retry_check(tmp_path: Path, log: list[dict[str, Any]]) -> Any:
    """``_check_retry_evidence`` of a start on FIRST_NIGHT whose run directory holds *log*, stamped with its operation."""
    config = _write_codex_fixture_config(tmp_path)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    adapters = deterministic_adapters()
    adapters.clock = _Clock(FIRST_NIGHT)
    workflow = weekly_workflow.WeeklyWorkflow(adapters)
    run = workflow._open_run(config, output_dir, None)
    stamped = [{**entry, "operation_id": run.run_id} for entry in log]
    (output_dir / "workflow-log.json").write_text(json.dumps(stamped), encoding="utf-8")
    return workflow._check_retry_evidence(run)


DEFERRED_KIRKUS_START = [
    _entry("repository-preflight", "ok", "00:30:00"),
    _entry("capture", "ok", "00:35:00"),
    _entry("codex-main", "ok", "00:40:00"),
    _entry("kirkus-precollection", "ok", "00:41:00"),
]


def test_a_same_date_start_after_a_late_failure_passes_the_kirkus_capture_guard(tmp_path):
    late = [*DEFERRED_KIRKUS_START, _entry("pipeline", "ok", "00:50:00"), _entry("terminal", "x", "01:00:00")]

    assert _retry_check(tmp_path, late) is None


def test_an_interrupted_kirkus_capture_still_stops_a_same_date_start(tmp_path):
    result = _retry_check(tmp_path, DEFERRED_KIRKUS_START)

    assert result is not None and result.failed_stage == "capture"


def test_an_earlier_start_that_saved_state_does_not_excuse_a_later_interrupted_capture(tmp_path):
    earlier = [*DEFERRED_KIRKUS_START, _entry("pipeline", "ok", "00:50:00")]
    later = [{**entry, "at": entry["at"].replace("T00:", "T02:")} for entry in DEFERRED_KIRKUS_START]

    result = _retry_check(tmp_path, [*earlier, *later])

    assert result is not None and result.failed_stage == "capture"


def test_a_start_the_next_day_after_a_same_date_publication_and_late_failure_is_carried(tmp_path, monkeypatch):
    night = _Night(tmp_path, monkeypatch, published_first=True)

    second = night.run(SECOND_NIGHT)

    assert _completed(second), second.log
    assert _carried_entries(second)[0]["detail"]["earlier_operation_date"] == FIRST_NIGHT.isoformat()


# ── The log rule ───────────────────────────────────────────────────────────


PUBLISHED_RUN = [
    _entry("repository-preflight", "ok", "00:30:00"),
    _entry("pipeline", "ok", "00:40:00"),
]
PUBLISHED_END = _entry("terminal", "completed/no-attention", "00:50:00")
LATER_RUN = [
    _entry("repository-preflight", "ok", "03:00:00"),
    _entry("pipeline", "ok", "03:10:00"),
    _entry("terminal", "intervention-required", "03:20:00"),
]


def _failed_run_of(tmp_path: Path, tree: list[dict[str, Any]], head: list[dict[str, Any]]) -> Any:
    from autoscout.late_failure_leftovers import _failed_run

    (tmp_path / "workflow-log.json").write_text(json.dumps(tree), encoding="utf-8")

    def git(_repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["git", *args], 0, stdout=json.dumps(head), stderr="")

    return _failed_run(tmp_path, (" M", "workflow-log.json"), git)


def test_the_log_rule_accepts_a_later_run_after_a_published_end_of_the_same_operation(tmp_path):
    assert _failed_run_of(tmp_path, [*PUBLISHED_RUN, *LATER_RUN], [*PUBLISHED_RUN, PUBLISHED_END]) == ("x", "")


def test_the_log_rule_refuses_a_published_end_newer_than_the_unpublished_preflight(tmp_path):
    late_end = _entry("terminal", "completed/no-attention", "03:05:00")

    assert _failed_run_of(tmp_path, [*PUBLISHED_RUN, *LATER_RUN], [*PUBLISHED_RUN, late_end]) is None


def test_the_log_rule_refuses_an_unpublished_entry_before_published_ones(tmp_path):
    stray = _entry("capture", "ok", "00:35:00")

    assert _failed_run_of(tmp_path, [stray, *PUBLISHED_RUN, *LATER_RUN], [*PUBLISHED_RUN, PUBLISHED_END]) is None


def test_the_log_rule_refuses_unpublished_entries_of_two_operations(tmp_path):
    mixed = [LATER_RUN[0], {**LATER_RUN[1], "operation_id": "y"}, LATER_RUN[2]]

    assert _failed_run_of(tmp_path, [*PUBLISHED_RUN, *mixed], [*PUBLISHED_RUN, PUBLISHED_END]) is None


def test_the_log_rule_refuses_when_nothing_is_unpublished(tmp_path):
    assert _failed_run_of(tmp_path, [*PUBLISHED_RUN, PUBLISHED_END], [*PUBLISHED_RUN, PUBLISHED_END]) is None


# ── What still blocks ──────────────────────────────────────────────────────


def test_an_unrelated_dirty_file_outside_the_run_directory_still_blocks(night):
    (night.repo / "notes.txt").write_text("somebody's work\n", encoding="utf-8")

    night.assert_blocked_untouched()


def test_a_file_outside_the_run_directory_blocks_while_the_directory_awaits_publication(night):
    assert night.preflight()["status"] == "ok"
    (night.repo / "notes.txt").write_text("somebody's work\n", encoding="utf-8")

    result = night.preflight()

    assert result["status"] == "unsafe" and result["dirty_files"] == ["notes.txt"], result


def test_a_changed_config_still_blocks(night):
    night.config.write_text(night.config.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    night.assert_blocked_untouched()


def test_a_staged_change_in_the_run_directory_still_blocks(night):
    _git(night.repo, "add", "runs/weekly/run-summary.json")

    night.assert_blocked_untouched()


def test_a_staged_change_blocks_while_the_directory_awaits_publication(night):
    assert night.preflight()["status"] == "ok"
    _git(night.repo, "add", "runs/weekly/run-summary.json")

    result = night.preflight()

    assert result["status"] == "unsafe" and "runs/weekly/run-summary.json" in result["dirty_files"], result


def test_a_deletion_the_run_never_makes_still_blocks(night):
    (night.output_dir / "run-summary.json").unlink()

    night.assert_blocked_untouched()


def test_a_run_log_that_shows_no_failed_run_still_blocks(night):
    _git(night.repo, "checkout", "HEAD", "--", "runs/weekly/workflow-log.json")

    night.assert_blocked_untouched()


@pytest.mark.parametrize("stage", ["repository-preflight", "pipeline"])
def test_a_failed_run_without_its_preflight_or_its_pipeline_proves_nothing(night, stage):
    log = json.loads(night.log.read_text(encoding="utf-8"))
    night.log.write_text(json.dumps([entry for entry in log if entry.get("stage") != stage]), encoding="utf-8")

    night.assert_blocked_untouched()


def test_a_run_log_head_already_holds_proves_nothing(night):
    """A run whose entries, its end included, are all in ``HEAD``'s log did not fail before publishing."""
    failed_log = night.log.read_bytes()
    _git(night.repo, "add", "runs/weekly/workflow-log.json")
    _git(night.repo, "commit", "-m", "published log")
    night.log.write_bytes(failed_log + b"\n")

    night.assert_blocked_untouched()


def test_the_backend_marker_is_never_carried(night):
    (night.output_dir / "state-backend.json").write_text('{"backend": "sqlite"}\n', encoding="utf-8")

    night.assert_blocked_untouched()


def test_a_symbolic_link_in_the_run_directory_still_blocks(night):
    (night.output_dir / "linked.json").symlink_to(night.config)

    night.assert_blocked_untouched()


def test_a_checkout_on_another_branch_is_refused_with_nothing_recorded(night):
    _git(night.repo, "checkout", "-q", "-b", "elsewhere")

    night.assert_blocked_untouched(reason="unexpected_branch")


def _scoped(night: _Night, state_dir: Path, verdict: str) -> dict[str, Any]:
    adapter = GitRepositoryAdapter(night.repo, expected_branch="main", run_log_path=night.log)
    adapter.set_leftover_scope(LeftoverScope(night.output_dir, state_dir, lambda: verdict))
    try:
        return adapter.preflight()
    finally:
        adapter.release_guard()


def test_a_separate_state_directory_is_not_carried(night):
    result = _scoped(night, night.repo / "state", "files")

    assert result["status"] == "unsafe" and night.markers() == []


def test_a_state_store_that_cannot_stand_behind_its_files_blocks(night):
    result = _scoped(night, night.output_dir, STATE_UNVERIFIED)

    assert result["status"] == "unsafe" and night.markers() == []


def test_a_marker_that_cannot_be_written_records_nothing(night, monkeypatch):
    monkeypatch.setattr(envelope, "mark_awaiting_publication", lambda *_args: False)
    before = night.snapshot()

    result = night.preflight()

    assert result == {"status": "unsafe", "reason": "late_failure_leftovers_not_recorded"}
    assert night.markers() == [] and night.snapshot() == before


def test_a_new_run_directory_whose_log_was_never_committed_is_carried(night):
    _git(night.repo, "rm", "-q", "--cached", "runs/weekly/workflow-log.json")
    _git(night.repo, "commit", "-q", "-m", "stop tracking the log")

    result = night.preflight()

    assert result["status"] == "ok", result
    assert night.log.exists() and len(night.markers()) == 1


# ── The State Store under SQLite ───────────────────────────────────────────


@pytest.fixture
def sqlite_night(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Night:
    monkeypatch.setenv(STATE_BACKEND_ENV, SQLITE_BACKEND)
    monkeypatch.setenv(DATABASE_DIR_ENV, str(tmp_path / "databases"))
    return _Night(tmp_path, monkeypatch, state_dir=True)


def _record_feedback(state_dir: Path) -> None:
    """A confirmed editor action between the nights: the store is written, then exported, as every command does."""
    store = open_state_store(state_dir)
    visibility = list(store.read(Collection.CANDIDATE_VISIBILITY))
    store.replace(Collection.CANDIDATE_VISIBILITY, [*visibility, {"identity_key": HIDDEN_BOOK, "visibility": "hidden"}])
    export_json(state_dir)


def _hidden(records: Any) -> bool:
    return any(record.get("identity_key") == HIDDEN_BOOK for record in records)


def test_under_sqlite_editor_feedback_after_the_failed_run_is_kept_and_published(sqlite_night):
    _record_feedback(sqlite_night.output_dir)

    second = sqlite_night.run(SECOND_NIGHT)

    assert _completed(second), second.log
    assert _hidden(open_state_store(sqlite_night.output_dir).read(Collection.CANDIDATE_VISIBILITY))
    assert _hidden(json.loads(_git(sqlite_night.repo, "show", "HEAD:runs/weekly/candidate-visibility.json")))
    assert _status(sqlite_night.repo) == []


def test_under_sqlite_the_database_and_its_export_agree_after_the_preflight(sqlite_night):
    _record_feedback(sqlite_night.output_dir)

    assert sqlite_night.preflight()["status"] == "ok"

    inspection = inspect_state_store(sqlite_night.output_dir)
    assert inspection.backend == SQLITE_BACKEND
    assert inspection.export_status == EXPORT_CURRENT and inspection.consistent


def test_under_sqlite_feedback_while_the_directory_awaits_publication_is_published(sqlite_night, monkeypatch):
    with monkeypatch.context() as patch:
        _fail_code_gate(patch)
        assert sqlite_night.run(SECOND_NIGHT).failed_stage == weekly_workflow.CODE_GATE_STAGE
    _record_feedback(sqlite_night.output_dir)

    third = sqlite_night.run(THIRD_NIGHT)

    assert _completed(third), third.log
    assert _hidden(json.loads(_git(sqlite_night.repo, "show", "HEAD:runs/weekly/candidate-visibility.json")))


def test_under_sqlite_a_hand_edited_state_file_blocks(sqlite_night):
    visibility = sqlite_night.output_dir / "candidate-visibility.json"
    visibility.write_text(json.dumps([{"identity_key": "hand|edit", "visibility": "hidden"}]), encoding="utf-8")

    sqlite_night.assert_blocked_untouched()


# ── The doctor ─────────────────────────────────────────────────────────────


def _doctor(night: _Night, day: date = SECOND_NIGHT) -> Any:
    seams = DoctorSeams(
        today=lambda: day,
        expected_branch="main",
        expected_remote_url=None,
        codex_probe=lambda: {"codex_available": True},
        codex_compat_probe=lambda: {"codex_compatible": True},
    )
    return diagnose(night.config, night.output_dir, night.state_dir, seams=seams)


def test_the_doctor_says_the_unsaved_results_will_be_saved_and_touches_nothing(night):
    before = night.snapshot()

    report = _doctor(night)

    assert report.diagnosis.value == "healthy", report.facts
    assert report.facts["late_failure_leftovers"]["status"] == "pending"
    text = render_russian(report)
    assert f"поиск {FIRST_NIGHT.isoformat()} не успел сохранить результаты" in text
    assert "ещё не запускался" not in text
    assert night.snapshot() == before and night.markers() == []


def test_the_doctor_promises_nothing_on_a_checkout_the_preflight_refuses(night):
    _git(night.repo, "checkout", "-q", "-b", "elsewhere")

    report = _doctor(night)

    assert report.diagnosis.value == "repository-blocker"
    assert "late_failure_leftovers" not in report.facts


def test_the_doctor_says_the_earlier_results_were_saved_after_the_run(night):
    night.run(SECOND_NIGHT)

    report = _doctor(night)

    assert report.facts["late_failure_leftovers"] == {
        "status": "published",
        "earlier_operation_date": FIRST_NIGHT.isoformat(),
    }
    assert "Сохранены и результаты незавершённого поиска" in render_russian(report)
