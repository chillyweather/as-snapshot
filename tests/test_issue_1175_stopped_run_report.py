"""The morning report of a scheduled unit that systemd stopped, and the night's second attempt after it (#1175)."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import types
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from autoscout import operator_notice
from autoscout import weekly_workflow
from autoscout.git_envelope import GUARD_FILENAME
from autoscout.git_envelope import GitRepositoryAdapter
from autoscout.operator_notice import REPORT_RECORD_ENV
from autoscout.operator_notice import SECOND_ATTEMPT_JOURNAL
from autoscout.operator_notice import deliver
from autoscout.operator_notice import record_report
from autoscout.operator_notice import run_report
from autoscout.second_attempt import PRIOR_INTERRUPTED
from autoscout.second_attempt import PriorRun
from autoscout.second_attempt import report_line
from autoscout.weekly_workflow import WeeklyWorkflow
from autoscout.weekly_workflow import run_weekly_workflow
from tests.test_issue_880_parallel_kirkus import _offline_other_capture  # noqa: F401  (autouse fixture)
from tests.test_issue_1057_nightly_run import _fixture_run
from tests.test_issue_1057_nightly_run import _git
from tests.test_issue_1057_nightly_run import _HeldLockRepository
from tests.test_issue_1057_nightly_run import _Transport
from tests.test_issue_1160_second_attempt import DAY
from tests.test_issue_1160_second_attempt import LOG_RESIDUE_DIR
from tests.test_issue_1160_second_attempt import _changed

DEPLOY = Path(__file__).resolve().parent.parent / "deploy" / "server"
REPORTER = DEPLOY / "autoscout-stopped-report"


def _reporter_module() -> types.ModuleType:
    """The standalone reporter, loaded from its file as systemd runs it."""
    loader = importlib.machinery.SourceFileLoader("autoscout_stopped_report", str(REPORTER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


_reporter = _reporter_module()
UNIT_JOBS = _reporter.UNIT_JOBS
UnitEnd = _reporter.UnitEnd
report_stopped_unit = _reporter.main
report_needed = _reporter.report_needed
report_record_path = _reporter.report_record_path
WRAPPER = DEPLOY / "autoscout-scheduled-run"
STOPPED_TEMPLATE = "autoscout-stopped@.service"
INVOCATION = "0123456789abcdef0123456789abcdef"
# The longest run the run log history holds (#1175): a manual run on 2026-11-08, 5 h 49 min, whose extraction
# took 4 h. The longest scheduled run took 77 minutes and the usual one about 30. A limit must stay well above it.
LONGEST_MEASURED_RUN_MINUTES = 349


class _Killed(BaseException):
    """The process dying: no ``except Exception`` catches it, as SIGKILL runs nothing."""


def _end(result: str, exit_code: str = "killed", status: str = "TERM") -> UnitEnd:
    return UnitEnd(result=result, exit_code=exit_code, exit_status=status, invocation_id=INVOCATION)


# ── Whether a report was already sent for the stopped start ────────────────


@pytest.mark.parametrize("job", ["run", "retry"])
@pytest.mark.parametrize(
    "end",
    [_end("timeout"), _end("signal"), _end("oom-kill"), _end("exit-code", "exited", "1"), _end("core-dump")],
)
def test_a_search_that_ended_without_its_record_gets_the_report(tmp_path, job, end):
    assert report_needed(job, end, tmp_path / "record.json") is True


@pytest.mark.parametrize("job", ["run", "retry", "kirkus"])
@pytest.mark.parametrize("end", [_end("timeout"), _end("exit-code", "exited", "1"), _end("signal")])
def test_a_start_whose_report_is_recorded_gets_none(tmp_path, job, end):
    record = tmp_path / "record.json"
    record_report(record, "sent")

    assert report_needed(job, end, record) is False


@pytest.mark.parametrize("job", ["run", "retry", "kirkus"])
def test_a_start_whose_report_went_to_the_journal_gets_none(tmp_path, job):
    record = tmp_path / "record.json"
    record_report(record, "not_configured")

    assert report_needed(job, _end("timeout"), record) is False


@pytest.mark.parametrize("job", ["run", "retry", "kirkus"])
def test_a_unit_that_succeeded_gets_none(tmp_path, job):
    assert report_needed(job, UnitEnd(result="success", exit_code="exited", exit_status="0"), None) is False


@pytest.mark.parametrize("job", ["run", "retry"])
def test_a_start_whose_record_cannot_be_named_is_reported_rather_than_left_out(job):
    assert report_needed(job, _end("timeout"), None) is True


@pytest.mark.parametrize(
    ("end", "needed"),
    [
        (_end("timeout"), True),
        (_end("signal"), True),
        (_end("oom-kill"), True),
        # The wrapper exits with the code of a pass the kernel killed: 128 + the signal.
        (_end("exit-code", "exited", "137"), True),
        # A pass that ended with its own code sends nothing, as before: the run's report states it.
        (_end("exit-code", "exited", "2"), False),
        (_end("exit-code", "exited", "1"), False),
    ],
)
def test_the_kirkus_pass_is_reported_only_when_it_was_stopped(tmp_path, end, needed):
    assert report_needed("kirkus", end, tmp_path / "record.json") is needed


def test_the_record_is_written_after_the_report_whatever_its_delivery(tmp_path):
    record = tmp_path / "reports" / f"{INVOCATION}.json"
    environ = {REPORT_RECORD_ENV: str(record)}
    report = run_report(None, run_date="2026-11-21", kirkus_attempt=None)

    deliver(report, transport=_Transport(), environ=environ, output=lambda _: None)

    assert json.loads(record.read_text(encoding="utf-8"))["outcome"] == "sent"
    assert not report_needed("run", _end("exit-code", "exited", "1"), record)


def test_a_report_that_could_not_be_sent_does_not_stop_the_second_one(tmp_path):
    record = tmp_path / "reports" / f"{INVOCATION}.json"
    environ = {REPORT_RECORD_ENV: str(record)}
    report = run_report(None, run_date="2026-11-21", kirkus_attempt=None)

    outcome = deliver(report, transport=_Transport(RuntimeError("smtp")), environ=environ, output=lambda _: None)

    assert outcome == "failed"
    assert json.loads(record.read_text(encoding="utf-8"))["outcome"] == "failed"
    assert report_needed("run", _end("exit-code", "exited", "1"), record)


def test_a_report_printed_to_the_journal_counts_as_delivered(tmp_path):
    record = tmp_path / "reports" / f"{INVOCATION}.json"
    report = run_report(None, run_date="2026-11-21", kirkus_attempt=None)

    deliver(report, environ={REPORT_RECORD_ENV: str(record)}, output=lambda _: None)

    assert not report_needed("run", _end("timeout"), record)


@pytest.mark.parametrize("content", ["", "{not json", "[]", '{"outcome": "unknown"}'])
def test_an_unreadable_record_does_not_stop_the_report(tmp_path, content):
    record = tmp_path / f"{INVOCATION}.json"
    record.write_text(content, encoding="utf-8")

    assert report_needed("run", _end("timeout"), record)


def test_a_record_whose_cleanup_fails_changes_nothing_about_the_delivery(tmp_path, monkeypatch):
    record = tmp_path / "reports" / f"{INVOCATION}.json"
    report = run_report(None, run_date="2026-11-21", kirkus_attempt=None)

    def refused(*args, **kwargs):
        raise OSError(30, "read-only file system")

    monkeypatch.setattr(operator_notice.os, "replace", refused)
    monkeypatch.setattr(Path, "unlink", refused)

    outcome = deliver(report, transport=_Transport(), environ={REPORT_RECORD_ENV: str(record)}, output=lambda _: None)

    assert outcome == "sent"


def test_a_job_killed_while_sending_its_report_leaves_no_record_and_is_reported(tmp_path):
    record = tmp_path / "reports" / f"{INVOCATION}.json"
    report = run_report(None, run_date="2026-11-21", kirkus_attempt=None)

    with pytest.raises(_Killed):
        deliver(report, transport=_Transport(_Killed()), environ={REPORT_RECORD_ENV: str(record)})

    assert not record.exists()
    assert report_needed("run", _end("timeout"), record)


def test_a_record_killed_halfway_is_no_record(tmp_path, monkeypatch):
    record = tmp_path / "reports" / f"{INVOCATION}.json"

    def killed(*args):
        raise _Killed

    monkeypatch.setattr(operator_notice.os, "replace", killed)
    with pytest.raises(_Killed):
        record_report(record, "sent")
    monkeypatch.undo()

    assert not record.exists()
    assert report_needed("run", _end("timeout"), record)


def test_a_record_that_cannot_be_written_changes_nothing_about_the_delivery(tmp_path):
    blocker = tmp_path / "reports"
    blocker.write_text("a file where the folder should be", encoding="utf-8")
    report = run_report(None, run_date="2026-11-21", kirkus_attempt=None)
    environ = {REPORT_RECORD_ENV: str(blocker / f"{INVOCATION}.json")}

    assert deliver(report, transport=_Transport(), environ=environ, output=lambda _: None) == "sent"


def test_without_the_variable_no_record_is_written(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    report = run_report(None, run_date="2026-11-21", kirkus_attempt=None)

    deliver(report, transport=_Transport(), environ={}, output=lambda _: None)

    assert list(tmp_path.rglob("*")) == []


def test_records_older_than_the_keep_time_are_removed_when_one_is_written(tmp_path):
    folder = tmp_path / "reports"
    record_report(folder / "old.json", "sent")
    old = (folder / "old.json").stat().st_mtime - operator_notice.REPORT_RECORD_KEEP_SECONDS - 60
    os.utime(folder / "old.json", (old, old))

    record_report(folder / f"{INVOCATION}.json", "sent")

    assert sorted(path.name for path in folder.iterdir()) == [f"{INVOCATION}.json"]


# ── The wrapper names the record of each start ─────────────────────────────


def _wrapper(tmp_path: Path, invocation: str | None, *, importable: bool = True) -> subprocess.CompletedProcess[str]:
    repo, calls = tmp_path / "repo", tmp_path / "calls.txt"
    (repo / "scripts").mkdir(parents=True)
    for name in ("run-weekly", "doctor", "collect-kirkus"):
        (repo / "scripts" / name).write_text(
            '#!/usr/bin/env bash\necho "${AUTOSCOUT_REPORT_RECORD:-unset}" >> "$CALLS"\n', encoding="utf-8"
        )
        (repo / "scripts" / name).chmod(0o755)
    # A command that imports, or one whose import fails: the wrapper's launcher starts the checked-out code
    # only in the first case (#1169).
    (repo / "autoscout").mkdir()
    for name in ("__init__.py", "__main__.py"):
        text = "" if importable else "raise ImportError('broken checkout')\n"
        (repo / "autoscout" / name).write_text(text, encoding="utf-8")
    calls.write_text("", encoding="utf-8")
    environ = {key: value for key, value in os.environ.items() if not key.startswith(("AUTOSCOUT_", "PYTHON"))}
    environ.pop("INVOCATION_ID", None)
    environ.update(AUTOSCOUT_SCHEDULED_REPO=str(repo), CALLS=str(calls), HOME=str(tmp_path / "home"))
    if invocation is not None:
        environ["INVOCATION_ID"] = invocation
    return subprocess.run(["bash", str(WRAPPER)], env=environ, capture_output=True, text=True, check=False)


def _wrapper_record(tmp_path: Path, invocation: str | None) -> str:
    done = _wrapper(tmp_path, invocation)
    assert done.returncode == 0, done.stdout + done.stderr
    return (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()[0]


def test_a_start_the_launcher_reported_itself_gets_no_second_report(tmp_path):
    """Neither the checked-out code nor a last passing one can start: the launcher of #1169 reports and exits 1."""
    done = _wrapper(tmp_path, INVOCATION, importable=False)
    assert done.returncode == 1, done.stdout + done.stderr
    record = report_record_path(tmp_path / "home", INVOCATION)
    end = UnitEnd(result="exit-code", exit_code="exited", exit_status="1", invocation_id=INVOCATION)

    assert report_needed("run", end, record) is False
    assert report_needed("run", end, None, done.stdout.splitlines()) is False


def test_the_wrapper_hands_the_job_the_record_the_stopped_unit_reads(tmp_path):
    assert _wrapper_record(tmp_path, INVOCATION) == str(report_record_path(tmp_path / "home", INVOCATION))


@pytest.mark.parametrize("invocation", [None, "", "../../etc", "0123"])
def test_the_wrapper_names_no_record_without_a_systemd_invocation_id(tmp_path, invocation):
    assert _wrapper_record(tmp_path, invocation) == "unset"


# ── The unit that sends the report ─────────────────────────────────────────


def _shown(result: str = "timeout", limit: str = "11h 30min") -> str:
    return "\n".join(
        [
            f"Result={result}",
            "ExecMainCode=2",
            "ExecMainStatus=15",
            f"InvocationID={INVOCATION}",
            f"TimeoutStartUSec={limit}",
            "ExecMainStartTimestamp=Sat 2026-11-21 02:30:01 IST",
        ]
    )


def _stopped(
    tmp_path: Path, unit: str, *, shown: str | None = None, environ: dict | None = None, journal: list | None = None
):
    transport, lines = _Transport(), []
    code = report_stopped_unit(
        [unit],
        environ=environ if environ is not None else {},
        show=lambda name: _shown() if shown is None else shown,
        journal=lambda invocation: list(journal or []),
        transport=transport,
        home=tmp_path / "home",
        today=lambda: date(2026, 11, 22),
        output=lines.append,
    )
    return code, transport.sent, lines


def test_a_search_stopped_at_its_limit_gets_one_stopped_report_naming_the_limit(tmp_path):
    code, sent, _ = _stopped(tmp_path, "autoscout-run.service")

    assert code == 0
    assert len(sent) == 1
    subject, body = sent[0]
    assert subject == "AutoScout: ночной поиск 2026-11-21 — остановлен"
    assert "по пределу времени 11 ч 30 мин (TimeoutStartSec)" in body.splitlines()[0]
    assert body.splitlines()[-1] == f"Журнал: {operator_notice.RUN_JOURNAL}"


def test_the_same_failure_is_reported_once_even_when_the_unit_runs_again(tmp_path):
    _stopped(tmp_path, "autoscout-run.service")

    code, sent, _ = _stopped(tmp_path, "autoscout-run.service")

    assert code == 0
    assert sent == []


def test_a_stopped_second_attempt_says_it_was_the_second(tmp_path):
    _, sent, _ = _stopped(tmp_path, "autoscout-run-retry.service", shown=_shown(limit="11h 30min"))

    subject, body = sent[0]
    assert subject == "AutoScout: ночной поиск 2026-11-21 — остановлен (повторная попытка)"
    assert body.splitlines()[1] == _reporter.STOPPED_RETRY_LINE
    assert "по пределу времени 11 ч 30 мин (TimeoutStartSec)" in body.splitlines()[0]
    assert body.splitlines()[-1] == f"Журнал: {SECOND_ATTEMPT_JOURNAL}"


def test_a_stopped_kirkus_pass_gets_its_own_report(tmp_path):
    _, sent, _ = _stopped(tmp_path, "autoscout-kirkus.service", shown=_shown(limit="2h"))

    subject, body = sent[0]
    assert subject == "AutoScout: сбор Kirkus 2026-11-21 — остановлен"
    assert "2 ч" in body.splitlines()[0]
    assert body.splitlines()[-1] == f"Журнал: {_reporter.KIRKUS_JOURNAL}"


def test_a_kirkus_pass_that_exited_with_its_own_code_gets_none(tmp_path):
    environ = {"MONITOR_SERVICE_RESULT": "exit-code", "MONITOR_EXIT_CODE": "exited", "MONITOR_EXIT_STATUS": "2"}

    _, sent, _ = _stopped(tmp_path, "autoscout-kirkus.service", environ=environ)

    assert sent == []


def test_the_facts_systemd_hands_the_unit_win_over_the_properties_of_a_later_start(tmp_path):
    other = "f" * 32
    record_report(report_record_path(tmp_path / "home", other), "sent")
    environ = {"MONITOR_SERVICE_RESULT": "timeout", "MONITOR_INVOCATION_ID": other}

    _, sent, _ = _stopped(tmp_path, "autoscout-run.service", shown=_shown(result="success"), environ=environ)

    assert sent == []


def test_without_the_unit_properties_the_report_still_goes_out_dated_today(tmp_path):
    _, sent, _ = _stopped(tmp_path, "autoscout-run.service", shown="", environ={"MONITOR_SERVICE_RESULT": "timeout"})

    subject, body = sent[0]
    assert subject == "AutoScout: ночной поиск 2026-11-22 — остановлен"
    assert "по пределу времени (TimeoutStartSec)" in body


@pytest.mark.parametrize("argv", [[], ["autoscout-review.service"], ["autoscout-run.service", "x"]])
def test_an_unknown_unit_is_refused(tmp_path, argv):
    transport = _Transport()
    code = report_stopped_unit(
        argv, environ={}, show=lambda _: "", journal=lambda _: [], transport=transport, output=lambda _: None
    )
    assert code == 64
    assert transport.sent == []


def test_a_scheduled_run_that_sent_its_report_and_failed_gets_no_second_one(tmp_path, monkeypatch):
    import autoscout.cli.weekly as cli

    config, output_dir, adapters = _fixture_run(tmp_path)
    adapters.repository = _HeldLockRepository("guard_held", owner_alive=False)
    monkeypatch.setattr(weekly_workflow, "weekly_adapters", lambda *args, **kwargs: adapters)
    monkeypatch.setenv(REPORT_RECORD_ENV, str(report_record_path(tmp_path / "home", INVOCATION)))
    own = _Transport()

    code = cli._run_weekly_command(config, output_dir, run_date=DAY.isoformat(), scheduled=True, report_transport=own)

    assert code == 1 and len(own.sent) == 1
    environ = {"MONITOR_SERVICE_RESULT": "exit-code", "MONITOR_EXIT_CODE": "exited", "MONITOR_EXIT_STATUS": "1"}
    _, sent, _ = _stopped(tmp_path, "autoscout-run.service", environ={**environ, "MONITOR_INVOCATION_ID": INVOCATION})
    assert sent == []


# ── The unit files ─────────────────────────────────────────────────────────


def _unit(name: str) -> dict[str, list[str]]:
    """The settings of a unit file, each key with every value it is given, in order."""
    settings: dict[str, list[str]] = {}
    for line in (DEPLOY / name).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        key, _, value = line.partition("=")
        settings.setdefault(key.strip(), []).append(value.strip())
    return settings


def _hours(limit: str) -> float:
    parts = re.findall(r"(\d+)(h|min)", limit)
    assert "".join(amount + unit for amount, unit in parts) == limit, limit
    return sum(int(amount) * {"h": 1, "min": 1 / 60}[unit] for amount, unit in parts)


def _timer_hour(name: str) -> float:
    calendar = _unit(name)["OnCalendar"][0]
    hour, minute = calendar.split()[1].split(":")
    return int(hour) + int(minute) / 60


def test_every_scheduled_unit_hands_its_failure_to_the_stopped_unit():
    assert sorted(UNIT_JOBS) == sorted(
        path.name for path in DEPLOY.glob("*.service") if "OnFailure" in _unit(path.name)
    )
    for unit in UNIT_JOBS:
        assert _unit(unit)["OnFailure"] == [STOPPED_TEMPLATE.replace("@.", "@%n.")]


def test_the_stopped_unit_runs_the_installed_script_with_nothing_of_the_checkout():
    stopped = _unit(STOPPED_TEMPLATE)
    run = _unit("autoscout-run.service")
    checkout = run["WorkingDirectory"][0]
    assert "OnFailure" not in stopped
    assert stopped["Type"] == ["oneshot"]
    # Installed beside the wrapper, which the services start from %h/.local/bin.
    assert stopped["ExecStart"] == [f"/usr/bin/python3 %h/.local/bin/{REPORTER.name} %i"]
    assert run["ExecStart"][0].startswith("%h/.local/bin/autoscout-scheduled-run")
    assert all(checkout not in value for values in stopped.values() for value in values)
    # The same date and the same mail settings as the units it reports.
    assert [value for value in stopped["Environment"] if value.startswith("TZ=")] == [
        value for value in run["Environment"] if value.startswith("TZ=")
    ]
    assert stopped["EnvironmentFile"] == run["EnvironmentFile"]
    assert stopped["TimeoutStartSec"]


def test_the_reporter_uses_the_standard_library_alone():
    import ast

    tree = ast.parse(REPORTER.read_text(encoding="utf-8"))
    imported = {
        (node.module if isinstance(node, ast.ImportFrom) else alias.name).split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in (node.names if isinstance(node, ast.Import) else [node])
    }
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}


def test_the_reporters_copies_of_the_shared_values_equal_the_packages():
    for name in (
        "ENV_HOST", "ENV_PORT", "ENV_USER", "ENV_PASSWORD", "ENV_FROM", "ENV_TO", "DEFAULT_SMTP_PORT", "SMTPS_PORT",
        "SMTP_TIMEOUT_SECONDS", "RUN_JOURNAL", "SECOND_ATTEMPT_JOURNAL", "SECOND_ATTEMPT_SUBJECT", "VERDICT_STOPPED",
        "REPORT_SENT_LINE", "REPORT_NOT_CONFIGURED_PREFIX",
    ):  # fmt: skip
        assert getattr(_reporter, name) == getattr(operator_notice, name), name
    # The night's subject and journal line are the package's own.
    package = operator_notice.stopped_run_report("x", run_date="2026-11-21", kirkus_attempt=None, second_attempt="L")
    ours = _reporter._night_report("2026-11-21", ["first"], "L")
    assert ours.subject == package.subject
    assert ours.body.splitlines()[1:] == package.body.splitlines()[1:2] + package.body.splitlines()[-1:]


def test_the_record_the_package_writes_is_the_one_the_reporter_reads(tmp_path):
    record = tmp_path / "record.json"
    for outcome, delivered in (("sent", True), ("not_configured", True), ("failed", False)):
        record_report(record, outcome)
        assert _reporter.report_recorded(record) is delivered


def test_the_first_attempt_stops_early_enough_for_the_second_to_run_the_same_day_without_cutting_a_healthy_run():
    run_limit = _hours(_unit("autoscout-run.service")["TimeoutStartSec"][0])
    retry_limit = _hours(_unit("autoscout-run-retry.service")["TimeoutStartSec"][0])
    start = _timer_hour("autoscout-run.timer")

    assert "autoscout-run.service" in _unit("autoscout-run-retry.service")["After"]
    # Well above the longest run ever measured, for both attempts.
    assert min(run_limit, retry_limit) * 60 >= 1.5 * LONGEST_MEASURED_RUN_MINUTES
    # A second attempt that waited for a first one stopped at its limit ends the same day it began, and
    # before the next night's run, with at least an hour to spare.
    assert start + run_limit < 24
    assert start + run_limit + retry_limit <= 24 + start - 1


# ── A second attempt after a first one killed mid-run ──────────────────────


def _published(tmp_path: Path):
    """A checkout and its origin, whose run log is committed, as on the server."""
    remote, repo = tmp_path / "remote.git", tmp_path / "repo"
    repo.mkdir()
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@test.test")
    _git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text(f"/{LOG_RESIDUE_DIR.as_posix()}/\n/data/\n/{GUARD_FILENAME}\n", encoding="utf-8")
    config, output_dir, adapters = _fixture_run(repo)
    (output_dir / "workflow-log.json").write_text("[]\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "--set-upstream", "origin", "main")
    return repo, config, output_dir, adapters


def _kill_first_attempt(monkeypatch, repo, config, output_dir, adapters, after_stage: str, *, lock_age: float) -> None:
    """Run the first attempt and kill it after *after_stage*, as SIGKILL would: no log entry, no lock release."""
    stages = WeeklyWorkflow._stages

    def dying(self):
        for stage in stages(self):
            yield stage
            if stage.__name__ == after_stage:

                def killed(run):
                    raise _Killed

                yield killed

    with monkeypatch.context() as patch:
        patch.setattr(WeeklyWorkflow, "_stages", lambda self: tuple(dying(self)))
        patch.setattr(WeeklyWorkflow, "_record_escaping_exception", lambda *args, **kwargs: None)
        patch.setattr(GitRepositoryAdapter, "release_guard", lambda self: None)
        adapters.repository = GitRepositoryAdapter(repo, expected_branch="main")
        with pytest.raises(_Killed):
            run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)
    lock = repo / GUARD_FILENAME
    lock.write_text(str(_dead_pid()), encoding="utf-8")
    written = lock.stat().st_mtime - lock_age
    os.utime(lock, (written, written))


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


KILL_POINTS = [
    "_update_repository",
    "_check_toolchain",
    "_capture",
    "_extract_main_batch",
    "_run_pipeline",
    "_enrich_descriptions",
    "_adjudicate_descriptions",
    "_render_review",
    "_export_state",
    "_scan_artifacts_for_secrets",
]


@pytest.mark.parametrize("lock_age", [0, 10 * 3600], ids=["young-lock", "old-lock"])
@pytest.mark.parametrize("after_stage", KILL_POINTS)
def test_a_second_attempt_after_a_first_killed_mid_run_completes_and_publishes(
    tmp_path, monkeypatch, after_stage, lock_age
):
    repo, config, output_dir, adapters = _published(tmp_path)
    _kill_first_attempt(monkeypatch, repo, config, output_dir, adapters, after_stage, lock_age=lock_age)
    seen: list = []
    adapters.repository = GitRepositoryAdapter(repo, expected_branch="main")

    second = run_weekly_workflow(
        config, output_dir, adapters=adapters, scheduled=True, second_attempt=True, on_prior_run=seen.append
    )

    assert second.outcome.value.startswith("completed/"), (second.failed_stage, second.log[-3:])
    assert seen == [PriorRun(PRIOR_INTERRUPTED)]
    assert _changed(repo) == set()
    assert _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "origin/main")
    assert not (repo / GUARD_FILENAME).exists()


def test_a_first_attempt_still_refuses_a_young_lock_of_a_dead_owner(tmp_path, monkeypatch):
    repo, config, output_dir, adapters = _published(tmp_path)
    _kill_first_attempt(monkeypatch, repo, config, output_dir, adapters, "_capture", lock_age=0)
    adapters.repository = GitRepositoryAdapter(repo, expected_branch="main")

    first = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)

    assert first.failed_stage == "repository-preflight"
    assert [entry.get("reason") for entry in first.log if entry.get("status") == "failed"] == ["guard_held"]


@pytest.mark.parametrize(
    ("probe_error", "taken_over"),
    [(ProcessLookupError(), True), (PermissionError(), False), (OSError(5, "io"), False), (None, False)],
    ids=["dead", "another-user", "unknown", "alive"],
)
def test_only_a_provably_dead_owner_loses_its_lock(tmp_path, probe_error, taken_over):
    _git(tmp_path, "init", "-b", "main")
    (tmp_path / GUARD_FILENAME).write_text("4242", encoding="utf-8")

    def probe(pid, signal):
        if probe_error is not None:
            raise probe_error

    adapter = GitRepositoryAdapter(tmp_path)
    assert adapter.take_over_dead_guard(pid_probe=probe) is taken_over
    assert (tmp_path / GUARD_FILENAME).exists() is not taken_over


@pytest.mark.parametrize("content", ["", "not-a-pid"])
def test_a_malformed_lock_is_never_taken_over(tmp_path, content):
    _git(tmp_path, "init", "-b", "main")
    (tmp_path / GUARD_FILENAME).write_text(content, encoding="utf-8")

    assert GitRepositoryAdapter(tmp_path).take_over_dead_guard() is False
    assert (tmp_path / GUARD_FILENAME).exists()


def test_a_night_with_a_stopped_first_attempt_sends_two_reports_that_agree(tmp_path, monkeypatch):
    import autoscout.cli.weekly as cli

    repo, config, output_dir, adapters = _published(tmp_path)
    _kill_first_attempt(monkeypatch, repo, config, output_dir, adapters, "_enrich_descriptions", lock_age=10 * 3600)
    _, first, _ = _stopped(tmp_path, "autoscout-run.service")
    adapters.repository = GitRepositoryAdapter(repo, expected_branch="main")
    monkeypatch.setattr(weekly_workflow, "weekly_adapters", lambda *args, **kwargs: adapters)
    # The fixture's run directory is not under runs/, so the real gate would count it as new code.
    monkeypatch.setattr(weekly_workflow, "bind_code_gate", lambda *args, **kwargs: None)
    monkeypatch.chdir(repo)
    second = _Transport()

    code = cli._run_weekly_command(
        config, output_dir, run_date=DAY.isoformat(), scheduled=True, second_attempt=True, report_transport=second
    )

    assert code == 0
    assert len(first) == 1 and len(second.sent) == 1
    (stopped_subject, stopped_body), (retry_subject, retry_body) = first[0], second.sent[0]
    assert stopped_subject.endswith("— остановлен")
    assert "вторая попытка" in stopped_body.lower()
    assert retry_subject.endswith("(повторная попытка)")
    assert " — остановлен" not in retry_subject
    assert retry_body.splitlines()[1] == report_line(PriorRun(PRIOR_INTERRUPTED))


def test_the_takeover_judges_the_lock_under_the_reclaim_lock(tmp_path):
    """A lock another process writes while the takeover waits is judged afresh, never removed unseen."""
    import fcntl
    import threading

    _git(tmp_path, "init", "-b", "main")
    lock = tmp_path / GUARD_FILENAME
    lock.write_text(str(_dead_pid()), encoding="utf-8")
    held = os.open(str(tmp_path), os.O_RDONLY)
    fcntl.flock(held, fcntl.LOCK_EX)
    outcome: list[bool] = []
    worker = threading.Thread(target=lambda: outcome.append(GitRepositoryAdapter(tmp_path).take_over_dead_guard()))
    worker.start()
    worker.join(timeout=1)
    assert worker.is_alive(), "the takeover waits for the reclaim lock"
    # Meanwhile a manual run replaced the dead lock with its own.
    lock.write_text(str(os.getpid()), encoding="utf-8")
    os.close(held)
    worker.join(timeout=30)

    assert outcome == [False]
    assert lock.read_text(encoding="utf-8") == str(os.getpid())


# ── Codex round 2 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "line", [operator_notice.REPORT_SENT_LINE, f"{operator_notice.REPORT_NOT_CONFIGURED_PREFIX} (нет переменных ...)"]
)
def test_a_report_the_journal_of_the_start_shows_delivered_is_not_sent_again(tmp_path, line):
    """Code that writes no record, such as the last passing code of an earlier version (#1159), prints this line."""
    environ = {"MONITOR_SERVICE_RESULT": "exit-code", "MONITOR_EXIT_CODE": "exited", "MONITOR_EXIT_STATUS": "1"}

    journal = ["Запускаю ...", line, "AutoScout: ночной поиск 2026-11-21 — остановлен"]

    _, sent, _ = _stopped(tmp_path, "autoscout-run.service", environ=environ, journal=journal)

    assert sent == []


def test_a_report_printed_without_mail_settings_counts_only_with_its_subject_in_the_journal(tmp_path):
    """A job killed before flushing the report it printed may leave its first line alone."""
    line = f"{operator_notice.REPORT_NOT_CONFIGURED_PREFIX} (нет переменных ...)"

    _, sent, _ = _stopped(tmp_path, "autoscout-run.service", journal=["Запускаю ...", line])

    assert len(sent) == 1


def test_the_report_is_flushed_to_the_journal_before_its_record_is_written(tmp_path, monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(operator_notice.sys.stdout, "flush", lambda: order.append("flush"))
    monkeypatch.setattr(operator_notice, "record_report", lambda path, outcome: order.append("record"))
    report = run_report(None, run_date="2026-11-21", kirkus_attempt=None)

    deliver(report, environ={REPORT_RECORD_ENV: str(tmp_path / "r.json")}, output=lambda _: None)

    assert order == ["flush", "record"]


def test_a_first_attempt_killed_between_its_finalize_and_its_pipeline_still_stops_the_second(tmp_path, monkeypatch):
    """Known limit (#1175): the replay config the finalize rewrote is not proven the run's own, so nothing is
    overwritten; the second attempt stops at the preflight, reports «остановлен» and changes nothing."""
    repo, config, output_dir, adapters = _published(tmp_path)
    _kill_first_attempt(monkeypatch, repo, config, output_dir, adapters, "_finalize", lock_age=10 * 3600)
    before = _changed(repo)
    adapters.repository = GitRepositoryAdapter(repo, expected_branch="main")

    second = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True, second_attempt=True)

    assert second.failed_stage == "repository-preflight"
    assert [entry.get("reason") for entry in second.log if entry.get("status") == "failed"] == ["dirty_working_tree"]
    assert "run/codex-replay-config.json" in before


def test_a_journal_that_shows_only_a_failed_sending_does_not_stop_the_report(tmp_path):
    _, sent, _ = _stopped(tmp_path, "autoscout-run.service", journal=["Отчёт не отправлен: SMTPException."])

    assert len(sent) == 1


def test_the_journal_line_is_the_one_deliver_prints():
    report = run_report(None, run_date="2026-11-21", kirkus_attempt=None)
    sent: list[str] = []
    unconfigured: list[str] = []

    deliver(report, transport=_Transport(), environ={}, output=sent.append)
    deliver(report, environ={}, output=unconfigured.append)

    assert report_needed("run", _end("timeout"), None, sent) is False
    assert report_needed("run", _end("timeout"), None, unconfigured) is False
    assert report_needed("run", _end("timeout"), None, unconfigured[:1]) is True


@pytest.mark.parametrize("job", ["run", "retry", "kirkus"])
def test_a_job_systemd_could_not_start_is_reported_as_not_started(tmp_path, job):
    end = UnitEnd(result="exit-code", exit_code="exited", exit_status="203", invocation_id=INVOCATION)
    unit = {value: key for key, value in UNIT_JOBS.items()}[job]
    environ = {"MONITOR_SERVICE_RESULT": "exit-code", "MONITOR_EXIT_CODE": "exited", "MONITOR_EXIT_STATUS": "203"}

    assert report_needed(job, end, None) is True
    _, sent, _ = _stopped(tmp_path, unit, environ=environ)
    assert "systemd не смог запустить задание (код 203)" in sent[0][1]
    assert "убит" not in sent[0][1]


@pytest.mark.parametrize("content", ["-1", "0", str(2**40)])
def test_a_lock_naming_no_single_process_is_never_taken_over(tmp_path, content):
    _git(tmp_path, "init", "-b", "main")
    (tmp_path / GUARD_FILENAME).write_text(content, encoding="utf-8")

    def gone(pid, signal):
        raise ProcessLookupError

    assert GitRepositoryAdapter(tmp_path).take_over_dead_guard(pid_probe=gone) is False
    assert (tmp_path / GUARD_FILENAME).exists()


def test_the_stopped_report_promises_the_second_attempts_report_only_for_a_search_not_completed(tmp_path):
    _, sent, _ = _stopped(tmp_path, "autoscout-run.service")

    assert "если поиск этой даты не завершён" in sent[0][1]


# ── The start launcher's own report (#1169) ────────────────────────────────


def _launcher_delivery(tmp_path, monkeypatch, environ: dict[str, str], smtp: Any = None) -> tuple[Path, list[str]]:
    from tests.test_issue_1169_start_from_last_passed import _launcher_module

    launcher = _launcher_module()
    record = tmp_path / "reports" / f"{INVOCATION}.json"
    for key in [key for key in os.environ if key.startswith("AUTOSCOUT_")]:
        monkeypatch.delenv(key)
    for key, value in {**environ, REPORT_RECORD_ENV: str(record)}.items():
        monkeypatch.setenv(key, value)
    if smtp is not None:
        monkeypatch.setattr(launcher.smtplib, "SMTP", smtp)
    printed: list[str] = []
    monkeypatch.setattr(launcher, "say", printed.append)
    launcher.deliver("AutoScout: ночной поиск 2026-11-21 — остановлен", "Ночной поиск не запустился.")
    return record, printed


class _FakeSmtp:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def starttls(self, **kwargs) -> None:
        pass

    def send_message(self, message) -> None:
        pass


class _RefusingSmtp(_FakeSmtp):
    def send_message(self, message) -> None:
        raise OSError("refused")


MAIL = {
    "AUTOSCOUT_NOTIFY_SMTP_HOST": "mail.test",
    "AUTOSCOUT_NOTIFY_FROM": "a@test.test",
    "AUTOSCOUT_NOTIFY_TO": "b@test.test",
}


@pytest.mark.parametrize(("environ", "smtp"), [(MAIL, _FakeSmtp), ({}, None)], ids=["sent", "journal-only"])
def test_a_report_the_start_launcher_delivered_is_not_sent_again(tmp_path, monkeypatch, environ, smtp):
    record, printed = _launcher_delivery(tmp_path, monkeypatch, environ, smtp)
    end = UnitEnd(result="exit-code", exit_code="exited", exit_status="1", invocation_id=INVOCATION)

    # Either piece of evidence alone is enough: the record, and the line in the journal of the start.
    assert report_needed("run", end, record) is False
    assert report_needed("run", end, None, printed) is False
    environ = {"MONITOR_SERVICE_RESULT": "exit-code", "MONITOR_EXIT_CODE": "exited", "MONITOR_EXIT_STATUS": "1"}
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    record.parent.parent.mkdir(parents=True, exist_ok=True)
    home_record = report_record_path(tmp_path / "home", INVOCATION)
    home_record.parent.mkdir(parents=True, exist_ok=True)
    record.replace(home_record)
    _, sent, _ = _stopped(tmp_path, "autoscout-run.service", environ={**environ, "MONITOR_INVOCATION_ID": INVOCATION})
    assert sent == []


def test_a_report_the_start_launcher_could_not_send_is_followed_by_the_stopped_one(tmp_path, monkeypatch):
    record, printed = _launcher_delivery(tmp_path, monkeypatch, MAIL, _RefusingSmtp)
    end = UnitEnd(result="exit-code", exit_code="exited", exit_status="1", invocation_id=INVOCATION)

    assert json.loads(record.read_text(encoding="utf-8"))["outcome"] == "failed"
    assert report_needed("run", end, record, printed) is True
