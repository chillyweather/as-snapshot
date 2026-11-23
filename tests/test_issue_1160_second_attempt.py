"""The night's second scheduled attempt (#1160)."""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from autoscout import weekly_workflow
from autoscout.__main__ import main as cli_main
from autoscout.git_envelope import LOG_RESIDUE_DIR
from autoscout.git_envelope import PROGRESS_SNAPSHOT_FILE
from autoscout.git_envelope import GitRepositoryAdapter
from autoscout.mvp_run_evidence import inspect
from autoscout.mvp_run_evidence import operation_id
from autoscout.operator_notice import SECOND_ATTEMPT_JOURNAL
from autoscout.operator_notice import run_report
from autoscout.second_attempt import GENERIC_REPORT_LINE
from autoscout.second_attempt import PRIOR_COMPLETED
from autoscout.second_attempt import PRIOR_INTERRUPTED
from autoscout.second_attempt import PRIOR_NONE
from autoscout.second_attempt import PRIOR_STOPPED
from autoscout.second_attempt import PRIOR_UNPUBLISHED
from autoscout.second_attempt import SECOND_ATTEMPT_ENV
from autoscout.second_attempt import PriorRun
from autoscout.second_attempt import already_completed_message
from autoscout.second_attempt import prior_run
from autoscout.second_attempt import report_line
from autoscout.second_attempt import skipped_live_run_message
from autoscout.weekly_workflow import WeeklyWorkflow
from autoscout.weekly_workflow import WeeklyWorkflowResult
from autoscout.weekly_workflow import WorkflowOutcome
from autoscout.weekly_workflow import _FixedClockAdapter
from autoscout.weekly_workflow import run_weekly_workflow
from tests.test_issue_880_parallel_kirkus import _offline_other_capture  # noqa: F401  (autouse fixture)
from tests.test_issue_1057_nightly_run import _fixture_run
from tests.test_issue_1057_nightly_run import _git
from tests.test_issue_1057_nightly_run import _HeldLockRepository
from tests.test_issue_1057_nightly_run import _ResidueRepository
from tests.test_issue_1057_nightly_run import _Transport


class _PublishingRepository(_ResidueRepository):
    """A repository whose HEAD holds the run log as written, as after a finalize that committed it."""

    def committed_text(self, path: Path) -> str | None:
        return path.read_text(encoding="utf-8") if path.exists() else None


class _UnpublishedRepository(_ResidueRepository):
    """A repository whose HEAD holds the run log of before the terminal: the publication was interrupted."""

    def __init__(self, committed: str) -> None:
        super().__init__()
        self.committed = committed

    def committed_text(self, path: Path) -> str | None:
        return self.committed


def _files(output_dir: Path) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in output_dir.rglob("*") if path.is_file()}


def _write_log(output_dir: Path, entries: list[dict]) -> None:
    (output_dir / "workflow-log.json").write_text(json.dumps(entries), encoding="utf-8")


# The deterministic adapters' clock: the date the fixture runs operate on.
DAY = _FixedClockAdapter().today()


def _today_id(config: Path) -> str:
    return operation_id(config, DAY)


# ── The decision, read from the run log of the date ────────────────────────


def test_a_date_with_no_stamped_run_has_no_prior_run(tmp_path):
    config, output_dir, _ = _fixture_run(tmp_path)

    assert prior_run(inspect(output_dir, config, DAY)) == PriorRun(PRIOR_NONE)


def test_a_completed_run_of_another_date_is_no_run_of_this_date(tmp_path):
    config, output_dir, _ = _fixture_run(tmp_path)
    yesterday = operation_id(config, date.fromordinal(DAY.toordinal() - 1))
    _write_log(output_dir, [{"stage": "terminal", "status": "completed/no-attention", "operation_id": yesterday}])

    assert prior_run(inspect(output_dir, config, DAY)).state == PRIOR_NONE


def test_a_legacy_unstamped_completed_log_is_no_run_of_this_date(tmp_path):
    config, output_dir, _ = _fixture_run(tmp_path)
    _write_log(output_dir, [{"stage": "terminal", "status": "completed/no-attention"}])

    assert prior_run(inspect(output_dir, config, DAY)).state == PRIOR_NONE


def test_stamped_entries_without_a_terminal_are_an_interrupted_run(tmp_path):
    config, output_dir, _ = _fixture_run(tmp_path)
    _write_log(output_dir, [{"stage": "capture", "status": "ok", "operation_id": _today_id(config)}])

    assert prior_run(inspect(output_dir, config, DAY)).state == PRIOR_INTERRUPTED


def test_a_stopped_run_names_its_stage_and_reason(tmp_path):
    config, output_dir, _ = _fixture_run(tmp_path)
    run_id = _today_id(config)
    _write_log(
        output_dir,
        [
            {"stage": "repository-update", "status": "failed", "reason": "fetch_failed", "operation_id": run_id},
            {"stage": "terminal", "status": "intervention-required", "operation_id": run_id},
        ],
    )

    assert prior_run(inspect(output_dir, config, DAY)) == PriorRun(
        PRIOR_STOPPED, stage="repository-update", reason="fetch_failed"
    )


@pytest.mark.parametrize("status", ["completed/no-attention", "completed/attention-required"])
def test_a_completed_run_of_this_date_is_completed(tmp_path, status):
    config, output_dir, _ = _fixture_run(tmp_path)
    _write_log(output_dir, [{"stage": "terminal", "status": status, "operation_id": _today_id(config)}])

    assert prior_run(inspect(output_dir, config, DAY)).state == PRIOR_COMPLETED


# ── The second attempt in the workflow ─────────────────────────────────────


def test_completed_today_the_second_attempt_does_nothing_and_writes_nothing(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path)
    adapters.repository = _PublishingRepository()
    first = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)
    assert first.outcome.value.startswith("completed/"), first.failed_stage
    before = _files(output_dir)

    result = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True, second_attempt=True)

    assert result.outcome == WorkflowOutcome.SKIPPED_ALREADY_COMPLETED
    assert _files(output_dir) == before


def test_a_completed_terminal_not_in_head_is_an_interrupted_publication_and_runs(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path)
    adapters.repository = _ResidueRepository()
    first = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)
    assert first.outcome.value.startswith("completed/"), first.failed_stage
    log = json.loads((output_dir / "workflow-log.json").read_text(encoding="utf-8"))
    committed = json.dumps([entry for entry in log if entry.get("stage") != "terminal"])
    adapters.repository = _UnpublishedRepository(committed)
    seen: list = []

    result = run_weekly_workflow(
        config, output_dir, adapters=adapters, scheduled=True, second_attempt=True, on_prior_run=seen.append
    )

    assert seen == [PriorRun(PRIOR_UNPUBLISHED)]
    assert result.outcome.value.startswith("completed/"), result.failed_stage


def test_a_completion_that_cannot_be_shown_committed_runs(tmp_path):
    """A repository adapter that cannot read HEAD proves no publication, so the night gets its run."""
    config, output_dir, adapters = _fixture_run(tmp_path)
    adapters.repository = _ResidueRepository()
    run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)

    result = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True, second_attempt=True)

    assert result.outcome.value.startswith("completed/"), result.failed_stage


def test_failed_today_the_second_attempt_runs_the_search(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path)
    adapters.repository = _HeldLockRepository("guard_held", owner_alive=False)
    first = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)
    assert first.outcome == WorkflowOutcome.INTERVENTION_REQUIRED
    adapters.repository = _ResidueRepository()

    result = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True, second_attempt=True)

    assert result.outcome.value.startswith("completed/"), result.failed_stage


def test_interrupted_today_the_second_attempt_runs_the_search(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path)
    _write_log(output_dir, [{"stage": "repository-preflight", "status": "ok", "operation_id": _today_id(config)}])
    adapters.repository = _ResidueRepository()

    result = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True, second_attempt=True)

    assert result.outcome.value.startswith("completed/"), result.failed_stage


def test_with_no_run_today_the_second_attempt_runs_the_search(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path)
    yesterday = operation_id(config, date.fromordinal(DAY.toordinal() - 1))
    _write_log(output_dir, [{"stage": "terminal", "status": "completed/no-attention", "operation_id": yesterday}])
    adapters.repository = _ResidueRepository()

    result = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True, second_attempt=True)

    assert result.outcome.value.startswith("completed/"), result.failed_stage


def test_with_a_live_run_the_second_attempt_skips_and_writes_nothing(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path)
    _write_log(output_dir, [{"stage": "capture", "status": "ok", "operation_id": _today_id(config)}])
    adapters.repository = _HeldLockRepository("guard_held")
    before = _files(output_dir)

    result = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True, second_attempt=True)

    assert result.outcome == WorkflowOutcome.SKIPPED_RUN_IN_PROGRESS
    assert _files(output_dir) == before


def test_a_second_attempt_is_a_scheduled_start():
    with pytest.raises(ValueError):
        WeeklyWorkflow(scheduled=False, second_attempt=True)


def test_a_first_attempt_still_runs_on_a_date_that_already_completed(tmp_path):
    """The rule of #1057 is unchanged: only the second attempt asks whether the date completed."""
    config, output_dir, adapters = _fixture_run(tmp_path)
    adapters.repository = _ResidueRepository()
    run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)

    again = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)

    assert again.outcome.value.startswith("completed/"), again.failed_stage


# ── The command and the morning report ─────────────────────────────────────


def _command(monkeypatch, adapters, config, output_dir, *, second_attempt):
    import autoscout.cli.weekly as cli

    monkeypatch.setattr(weekly_workflow, "weekly_adapters", lambda *args, **kwargs: adapters)
    transport = _Transport()
    code = cli._run_weekly_command(
        config,
        output_dir,
        run_date=DAY.isoformat(),
        scheduled=True,
        second_attempt=second_attempt,
        report_transport=transport,
    )
    return code, transport


def test_one_report_per_attempt_that_runs_and_none_from_the_attempt_with_nothing_to_do(tmp_path, monkeypatch, capsys):
    config, output_dir, adapters = _fixture_run(tmp_path)
    today = DAY.isoformat()

    adapters.repository = _HeldLockRepository("guard_held", owner_alive=False)
    code, first = _command(monkeypatch, adapters, config, output_dir, second_attempt=False)
    assert code == 1
    assert len(first.sent) == 1
    assert first.sent[0][0] == f"AutoScout: ночной поиск {today} — остановлен"
    capsys.readouterr()

    adapters.repository = _PublishingRepository()
    code, second = _command(monkeypatch, adapters, config, output_dir, second_attempt=True)
    assert code == 0
    assert len(second.sent) == 1
    subject, body = second.sent[0]
    assert subject.startswith(f"AutoScout: ночной поиск {today} — ")
    assert subject.endswith("(повторная попытка)")
    assert body.splitlines()[1] == report_line(PriorRun(PRIOR_STOPPED, "repository-preflight", "guard_held"))
    assert body.splitlines()[-1] == f"Журнал: {SECOND_ATTEMPT_JOURNAL}"
    capsys.readouterr()

    code, third = _command(monkeypatch, adapters, config, output_dir, second_attempt=True)
    assert code == 0
    assert third.sent == []
    assert capsys.readouterr().out.splitlines() == [already_completed_message(today)]


def test_a_second_attempt_that_meets_a_live_run_prints_one_line_and_sends_nothing(tmp_path, monkeypatch, capsys):
    config, output_dir, adapters = _fixture_run(tmp_path)
    adapters.repository = _HeldLockRepository("guard_held")
    capsys.readouterr()

    code, transport = _command(monkeypatch, adapters, config, output_dir, second_attempt=True)

    assert code == 0
    assert transport.sent == []
    assert capsys.readouterr().out.splitlines() == [skipped_live_run_message()]


def test_a_second_attempt_stopped_before_reading_the_log_still_says_it_is_the_second(tmp_path, monkeypatch):
    import autoscout.cli.weekly as cli

    config, output_dir, adapters = _fixture_run(tmp_path)

    def broken(*args, **kwargs):
        raise RuntimeError("broke before the lock")

    monkeypatch.setattr(cli, "run_weekly_workflow", broken)

    code, transport = _command(monkeypatch, adapters, config, output_dir, second_attempt=True)

    assert code == 1
    assert len(transport.sent) == 1
    subject, body = transport.sent[0]
    assert subject.endswith("— остановлен (повторная попытка)")
    assert body.splitlines()[1] == GENERIC_REPORT_LINE


@pytest.mark.parametrize(
    ("prior", "line"),
    [
        (
            PriorRun(PRIOR_NONE),
            "Это повторная попытка: первый запуск этой даты не состоялся или был прерван до первой записи в журнал.",
        ),
        (
            PriorRun(PRIOR_UNPUBLISHED),
            "Это повторная попытка: первый запуск этой даты был прерван, когда сохранял итог.",
        ),
        (PriorRun(PRIOR_INTERRUPTED), "Это повторная попытка: первый запуск этой даты был прерван и не завершился."),
        (
            PriorRun(PRIOR_STOPPED, "code-gate", "new_code_check_timed_out"),
            "Это повторная попытка: первый запуск этой даты не завершён. "
            "Остановлено на этапе code-gate, причина: new_code_check_timed_out.",
        ),
        (
            PriorRun(PRIOR_STOPPED, "capture", "Some Book Title"),
            "Это повторная попытка: первый запуск этой даты не завершён. "
            "Остановлено на этапе capture, причина: unrecognized_reason.",
        ),
    ],
)
def test_the_report_line_names_how_the_first_attempt_ended(prior, line):
    assert report_line(prior) == line


def test_a_first_attempt_report_is_unchanged():
    result = WeeklyWorkflowResult(outcome=WorkflowOutcome.COMPLETED_NO_ATTENTION)

    report = run_report(result, run_date="2026-11-20", kirkus_attempt=None)

    assert report.subject == "AutoScout: ночной поиск 2026-11-20 — успешно"
    assert "повторная" not in report.body
    assert report.body.splitlines()[-1] == "Журнал: journalctl --user -u autoscout-run.service"


@pytest.mark.parametrize(
    ("argv", "environ", "expected"),
    [
        (["--scheduled"], {SECOND_ATTEMPT_ENV: "1"}, True),
        (["--scheduled"], {}, False),
        (["--scheduled"], {SECOND_ATTEMPT_ENV: "yes"}, False),
        ([], {SECOND_ATTEMPT_ENV: "1"}, False),
    ],
)
def test_the_second_attempt_is_read_from_the_environment_of_a_scheduled_start(monkeypatch, argv, environ, expected):
    import autoscout.cli.weekly as cli

    monkeypatch.delenv(SECOND_ATTEMPT_ENV, raising=False)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    calls: list[dict] = []
    monkeypatch.setattr(cli, "_run_weekly_command", lambda *args, **kwargs: calls.append(kwargs) or 0)

    assert cli_main(["run-weekly", "--config", "c.json", "--output-dir", "out", *argv]) == 0
    assert calls[0]["second_attempt"] is expected


def test_a_stop_at_repository_finalize_is_named_with_its_own_reason(tmp_path):
    config, output_dir, _ = _fixture_run(tmp_path)
    run_id = _today_id(config)
    _write_log(
        output_dir,
        [
            {"stage": "capture", "status": "failed", "reason": "source_down", "operation_id": run_id},
            {"stage": "pipeline", "status": "ok", "operation_id": run_id},
            {"stage": "repository-finalize", "status": "failed", "operation_id": run_id},
            {"stage": "repository-finalize", "status": "failed", "reason": "push_rejected", "operation_id": run_id},
            {"stage": "terminal", "status": "intervention-required", "operation_id": run_id},
        ],
    )

    assert prior_run(inspect(output_dir, config, DAY)) == PriorRun(
        PRIOR_STOPPED, stage="repository-finalize", reason="push_rejected"
    )


@pytest.mark.parametrize("content", ["{not json", '{"stage": "terminal"}'])
def test_a_malformed_log_gives_the_generic_line_not_a_first_attempt_that_never_ran(tmp_path, monkeypatch, content):
    config, output_dir, adapters = _fixture_run(tmp_path)
    (output_dir / "workflow-log.json").write_text(content, encoding="utf-8")
    adapters.repository = _ResidueRepository()

    _, transport = _command(monkeypatch, adapters, config, output_dir, second_attempt=True)

    assert len(transport.sent) == 1
    assert transport.sent[0][1].splitlines()[1] == GENERIC_REPORT_LINE


def test_the_first_attempt_is_carried_across_a_restart_on_updated_code(tmp_path, monkeypatch):
    import autoscout.cli.weekly as cli
    from autoscout.second_attempt import PRIOR_RUN_ENV

    config, output_dir, adapters = _fixture_run(tmp_path)
    run_id = _today_id(config)
    _write_log(
        output_dir,
        [
            {"stage": "repository-update", "status": "failed", "reason": "fetch_failed", "operation_id": run_id},
            {"stage": "terminal", "status": "intervention-required", "operation_id": run_id},
        ],
    )
    monkeypatch.setenv(PRIOR_RUN_ENV, "placeholder")
    monkeypatch.delenv(PRIOR_RUN_ENV)
    monkeypatch.setattr(weekly_workflow, "weekly_adapters", lambda *args, **kwargs: adapters)
    outcomes = iter(
        [
            WeeklyWorkflowResult(outcome=WorkflowOutcome.RESTART_ON_UPDATED_CODE),
            WeeklyWorkflowResult(outcome=WorkflowOutcome.INTERVENTION_REQUIRED, failed_stage="capture"),
        ]
    )

    def workflow(config_path, output_dir, *args, **kwargs):
        # What the workflow reads under the lock: the failed log, then the committed one restored in its place.
        kwargs["on_prior_run"](prior_run(inspect(output_dir, config_path, DAY)))
        return next(outcomes)

    monkeypatch.setattr(cli, "run_weekly_workflow", workflow)
    restarts: list[bool] = []
    transport = _Transport()

    def run() -> int:
        return cli._run_weekly_command(
            config,
            output_dir,
            run_date=DAY.isoformat(),
            scheduled=True,
            second_attempt=True,
            report_transport=transport,
            restart=lambda: restarts.append(True),
        )

    assert run() == 0
    assert restarts == [True] and transport.sent == []
    # The first start's preflight set the failed log aside and restored the committed one.
    _write_log(output_dir, [])
    run()

    assert len(transport.sent) == 1
    assert transport.sent[0][1].splitlines()[1] == report_line(
        PriorRun(PRIOR_STOPPED, "repository-update", "fetch_failed")
    )


@pytest.mark.parametrize(
    "value", ["", "{", "[]", '{"state": "odd"}', '{"state": "stopped", "stage": 3}', '{"state": "none", "reason": []}']
)
def test_a_malformed_handed_first_attempt_is_unknown(value):
    from autoscout.second_attempt import decode

    assert decode(value) is None


def test_a_handed_first_attempt_round_trips():
    from autoscout.second_attempt import decode
    from autoscout.second_attempt import encode

    prior = PriorRun(PRIOR_STOPPED, "code-gate", "new_code_failed_check")

    assert decode(encode(prior)) == prior


def test_a_toolchain_stop_is_named_with_the_probe_reason_code(tmp_path):
    from autoscout.toolchain_preflight import STAGE_ZERO_NAME

    config, output_dir, _ = _fixture_run(tmp_path)
    run_id = _today_id(config)
    _write_log(
        output_dir,
        [
            {
                "stage": STAGE_ZERO_NAME,
                "status": "failed",
                "detail": {"codex": {"reason_code": "codex_usage_limit"}},
                "operation_id": run_id,
            },
            {"stage": STAGE_ZERO_NAME, "status": "failed", "reason": "Codex is not ready.", "operation_id": run_id},
            {"stage": "terminal", "status": "intervention-required", "operation_id": run_id},
        ],
    )

    assert prior_run(inspect(output_dir, config, DAY)) == PriorRun(
        PRIOR_STOPPED, stage=STAGE_ZERO_NAME, reason="codex_usage_limit"
    )


def test_the_workflow_reports_the_earlier_run_it_read_under_the_lock(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path)
    adapters.repository = _HeldLockRepository("guard_held", owner_alive=False)
    run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)
    adapters.repository = _ResidueRepository()
    seen: list = []

    run_weekly_workflow(
        config, output_dir, adapters=adapters, scheduled=True, second_attempt=True, on_prior_run=seen.append
    )

    assert seen == [PriorRun(PRIOR_STOPPED, "repository-preflight", "guard_held")]


def test_an_unknown_first_attempt_is_handed_over_as_unknown(monkeypatch):
    from autoscout.second_attempt import PRIOR_RUN_ENV
    from autoscout.second_attempt import FirstAttempt

    environ: dict[str, str] = {}
    FirstAttempt(None, handed=False).hand_over(environ)
    restarted = FirstAttempt.from_environ(environ)
    # The restarted start reads the log restored in place of the unreadable one; that is not the earlier run.
    restarted.seen(PriorRun(PRIOR_NONE))

    assert environ[PRIOR_RUN_ENV] == "unknown"
    assert restarted.line() == GENERIC_REPORT_LINE


def test_the_second_attempt_mark_survives_the_restart_on_updated_code(tmp_path, monkeypatch):
    import os

    import autoscout.cli.weekly as cli
    from autoscout.second_attempt import PRIOR_RUN_ENV
    from autoscout.weekly_workflow import RESTARTED_ENV

    config, output_dir, adapters = _fixture_run(tmp_path)
    for name in (SECOND_ATTEMPT_ENV, PRIOR_RUN_ENV, RESTARTED_ENV):
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)
    monkeypatch.setenv(SECOND_ATTEMPT_ENV, "1")
    monkeypatch.setattr(weekly_workflow, "weekly_adapters", lambda *args, **kwargs: adapters)

    def workflow(*args, **kwargs):
        kwargs["on_prior_run"](PriorRun(PRIOR_INTERRUPTED))
        return WeeklyWorkflowResult(outcome=WorkflowOutcome.RESTART_ON_UPDATED_CODE)

    monkeypatch.setattr(cli, "run_weekly_workflow", workflow)
    executed: list[dict[str, str]] = []
    monkeypatch.setattr(os, "execv", lambda *args: executed.append(dict(os.environ)))

    cli._run_weekly_command(config, output_dir, run_date=DAY.isoformat(), scheduled=True, second_attempt=True)

    assert len(executed) == 1
    assert executed[0][SECOND_ATTEMPT_ENV] == "1"
    assert executed[0][PRIOR_RUN_ENV] == '{"reason": "", "stage": "", "state": "interrupted"}'


# ── An interrupted publication, on a real repository ───────────────────────


class _Killed(BaseException):
    """The process dying: no ``except Exception`` of the workflow catches it."""


class _DiesPublishingTheLog(GitRepositoryAdapter):
    """Commits and pushes the run, then dies in the finalize that publishes the terminal log."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.finalizes = 0

    def finalize(self, output_dir: Path) -> dict:
        self.finalizes += 1
        if self.finalizes == 2:
            raise _Killed
        return super().finalize(output_dir)


def _published_repository(tmp_path: Path) -> tuple[Path, Path, Path, object]:
    remote, repo = tmp_path / "remote.git", tmp_path / "repo"
    repo.mkdir()
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@test.test")
    _git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text(f"/{LOG_RESIDUE_DIR.as_posix()}/\n", encoding="utf-8")
    config, output_dir, adapters = _fixture_run(repo)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "--set-upstream", "origin", "main")
    return repo, config, output_dir, adapters


def _changed(repo: Path) -> set[str]:
    """The changed paths, without the progress snapshot, which the repository adapter never counts."""
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo, capture_output=True, text=True, check=True
    )
    return {line[3:] for line in status.stdout.splitlines() if Path(line[3:]).name != PROGRESS_SNAPSHOT_FILE}


def test_a_run_killed_while_publishing_its_terminal_is_run_again_and_published(tmp_path):
    repo, config, output_dir, adapters = _published_repository(tmp_path)
    adapters.repository = _DiesPublishingTheLog(repo, expected_branch="main")
    with pytest.raises(_Killed):
        run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)
    # The run's state is committed and pushed; only the run log with its terminal is left in the tree.
    assert _changed(repo) == {"run/workflow-log.json"}
    assert prior_run(inspect(output_dir, config, DAY)).completed
    seen: list = []
    adapters.repository = GitRepositoryAdapter(repo, expected_branch="main")

    second = run_weekly_workflow(
        config, output_dir, adapters=adapters, scheduled=True, second_attempt=True, on_prior_run=seen.append
    )

    assert seen == [PriorRun(PRIOR_UNPUBLISHED)]
    assert second.outcome.value.startswith("completed/"), (second.failed_stage, second.log[-3:])
    # The lone modified run log got through the preflight by the #1057 set-aside.
    assert ("log-residue", "set_aside") in [(entry["stage"], entry["status"]) for entry in second.log]
    assert _changed(repo) == set()
    committed = json.loads(_git(repo, "show", "HEAD:run/workflow-log.json"))
    assert committed[-1]["stage"] == "terminal" and committed[-1]["status"].startswith("completed/")
    assert _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "origin/main")

    third = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True, second_attempt=True)

    assert third.outcome == WorkflowOutcome.SKIPPED_ALREADY_COMPLETED


# ── The wrapper the timers call ─────────────────────────────────────────────

WRAPPER = Path(__file__).resolve().parent.parent / "deploy" / "server" / "autoscout-scheduled-run"
STUB = """#!/usr/bin/env bash
echo "$(basename "$0") [$*] second_attempt=${AUTOSCOUT_SECOND_ATTEMPT:-unset}" >> "$CALLS"
"""


def _run_wrapper(tmp_path: Path, job: list[str], hhmm: str) -> list[str]:
    """Run the wrapper on stub launchers, with the clock's HHMM fixed; the calls it made, in order."""
    import os
    import shutil

    repo, bin_dir, calls = tmp_path / "repo", tmp_path / "bin", tmp_path / "calls.txt"
    (repo / "scripts").mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(exist_ok=True)
    for name in ("run-weekly", "doctor", "collect-kirkus"):
        (repo / "scripts" / name).write_text(STUB, encoding="utf-8")
        (repo / "scripts" / name).chmod(0o755)
    # A command that imports: the wrapper's launcher starts the checked-out code only then (#1169).
    (repo / "autoscout").mkdir(exist_ok=True)
    for name in ("__init__.py", "__main__.py"):
        (repo / "autoscout" / name).write_text("", encoding="utf-8")
    real_date = shutil.which("date")
    (bin_dir / "date").write_text(
        f'#!/usr/bin/env bash\n[[ "$1" == "+%H%M" ]] && {{ echo "{hhmm}"; exit 0; }}\nexec {real_date} "$@"\n',
        encoding="utf-8",
    )
    (bin_dir / "date").chmod(0o755)
    calls.write_text("", encoding="utf-8")
    environ = {key: value for key, value in os.environ.items() if not key.startswith("AUTOSCOUT_")}
    environ.update(
        AUTOSCOUT_SCHEDULED_REPO=str(repo),
        CALLS=str(calls),
        HOME=str(tmp_path / "home"),
        PATH=f"{bin_dir}{os.pathsep}{environ.get('PATH', '')}",
    )
    done = subprocess.run(["bash", str(WRAPPER), *job], env=environ, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    return calls.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("hhmm", ["0430", "0515", "1300"])
def test_the_wrapper_marks_the_retry_in_the_environment_of_run_weekly_alone(tmp_path, hhmm):
    assert _run_wrapper(tmp_path, ["retry"], hhmm) == [
        "run-weekly [--scheduled] second_attempt=1",
        "doctor [] second_attempt=unset",
    ]


@pytest.mark.parametrize("hhmm", ["0000", "0100", "0429"])
def test_the_wrapper_skips_a_retry_started_before_its_time_as_an_earlier_nights(tmp_path, hhmm):
    assert _run_wrapper(tmp_path, ["retry"], hhmm) == []


def test_the_wrapper_runs_the_first_attempt_unmarked_at_any_time(tmp_path):
    assert _run_wrapper(tmp_path, [], "0100") == [
        "run-weekly [--scheduled] second_attempt=unset",
        "doctor [] second_attempt=unset",
    ]
