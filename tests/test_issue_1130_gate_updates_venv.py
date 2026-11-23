"""The gate brings an outdated server .venv up to the pins instead of stopping the night (#1130)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from autoscout import code_gate
from autoscout import weekly_workflow
from autoscout.code_gate import GATE_DIR
from autoscout.code_gate import INSTALL_LOG_FILE
from autoscout.code_gate import REASON_ENVIRONMENT_UPDATE_FAILED
from autoscout.code_gate import REASON_ENVIRONMENT_UPDATE_TIMED_OUT
from autoscout.code_gate import RECORD_FILE
from autoscout.code_gate import ProductionCodeGate
from autoscout.code_gate import ServerVenv
from autoscout.code_gate import environment_plan
from autoscout.git_envelope import GUARD_FILENAME
from autoscout.git_envelope import GitRepositoryAdapter
from autoscout.operator_notice import run_report
from autoscout.russian_workflow import render_workflow_result
from autoscout.weekly_workflow import CODE_GATE_STAGE
from autoscout.weekly_workflow import ENVIRONMENT_UPDATED_ENV
from autoscout.weekly_workflow import RESTART_AFTER_ENVIRONMENT_UPDATE
from autoscout.weekly_workflow import RESTARTED_ENV
from autoscout.weekly_workflow import WeeklyWorkflowResult
from autoscout.weekly_workflow import WorkflowOutcome
from autoscout.weekly_workflow import run_weekly_workflow
from tests.test_issue_1057_nightly_run import BOOK_TITLE
from tests.test_issue_1057_nightly_run import RUN_DATE
from tests.test_issue_1057_nightly_run import _fixture_run
from tests.test_issue_1057_nightly_run import _run_command
from tests.test_issue_1057_nightly_run import _the_report
from tests.test_issue_1124_code_gate import _Check
from tests.test_issue_1124_code_gate import _checkout
from tests.test_issue_1124_code_gate import _commit
from tests.test_issue_1124_code_gate import _git
from tests.test_issue_1124_code_gate import _push_from_elsewhere
from tests.test_issue_1124_code_gate import _snapshot

PROJECT = {
    "dependencies": [],
    "optional-dependencies": {
        "pw": ["pypdf==6.21.4"],
        "dev": ["pytest==8.4.2", "pytest-xdist==3.8.0"],
    },
}
CURRENT = {"autoscout": "0.1.0", "pytest": "8.4.2", "pytest-xdist": "3.8.0", "pypdf": "6.21.4"}


def _pyproject(pytest_pin: str) -> str:
    return (
        '[project]\nname = "autoscout"\nversion = "0.1.0"\ndependencies = []\n\n'
        "[project.optional-dependencies]\n"
        'pw = ["pypdf==6.21.4"]\n'
        f'dev = ["pytest=={pytest_pin}", "pytest-xdist==3.8.0"]\n'
    )


class _Venv:
    """A stand-in for the checkout's .venv: *after* is what an install leaves, *code* its exit code."""

    def __init__(
        self,
        installed: dict[str, str] | None,
        *,
        after: dict[str, str] | None = None,
        code: int = 0,
        timeout: bool = False,
        exists: bool = True,
    ) -> None:
        self.packages = installed
        self.after = after
        self.code = code
        self.timeout = timeout
        self._exists = exists
        self.installs: list[dict[str, Any]] = []

    def exists(self) -> bool:
        return self._exists

    def installed(self) -> dict[str, str] | None:
        return None if self.packages is None else dict(self.packages)

    def install(self, extras, *, env, timeout):
        self.installs.append({"extras": extras, "env": dict(env), "timeout": timeout})
        if self.timeout:
            raise subprocess.TimeoutExpired(["pip"], timeout, output="Collecting pytest\n")
        if self.code == 0 and self.after is not None:
            self.packages = dict(self.after)
        return self.code, "pip output\n"


@pytest.fixture(autouse=True)
def _gate_env(tmp_path, monkeypatch):
    monkeypatch.setenv(code_gate.TMPDIR_ENV, str(tmp_path / "gate-tmp"))
    monkeypatch.delenv(RESTARTED_ENV, raising=False)
    monkeypatch.delenv(ENVIRONMENT_UPDATED_ENV, raising=False)


def _project_checkout(tmp_path: Path, pytest_pin: str = "8.4.2") -> tuple[Path, Path]:
    repo, remote = _checkout(tmp_path)
    _commit(repo, "pyproject.toml", _pyproject(pytest_pin), "pyproject")
    _git(repo, "push", "-q", "origin", "main")
    return repo, remote


# ── What the .venv lacks ───────────────────────────────────────────────────


def test_a_venv_on_the_pins_needs_nothing_and_keeps_the_extras_it_uses():
    plan = environment_plan(PROJECT, CURRENT)

    assert plan.stale == ()
    assert plan.extras == ("dev", "pw")


def test_a_bumped_or_new_dev_pin_is_behind_and_an_unused_extra_is_never_added():
    project = {**PROJECT, "optional-dependencies": {**PROJECT["optional-dependencies"]}}
    project["optional-dependencies"]["dev"] = ["pytest==8.5.0", "pytest-xdist==3.8.0", "Mypy_Extensions==1.2.0"]
    without_pw = {name: version for name, version in CURRENT.items() if name != "pypdf"}

    plan = environment_plan(project, without_pw)

    assert plan.extras == ("dev",)
    assert plan.stale == (("pytest", "8.5.0", "8.4.2"), ("Mypy_Extensions", "1.2.0", ""))


def test_a_bumped_pin_of_an_extra_in_use_and_a_missing_runtime_dependency_are_behind():
    project = {
        "dependencies": ["requests>=2"],
        "optional-dependencies": {"pw": ["pypdf[crypto]==6.22.0"], "dev": ["pytest==8.4.2"]},
    }

    plan = environment_plan(project, {"PyTest": "8.4.2", "pypdf": "6.21.4"})

    assert plan.extras == ("dev", "pw")
    assert plan.stale == (("requests", "", ""), ("pypdf", "6.22.0", "6.21.4"))


def test_a_missing_project_is_behind_and_pins_compare_as_pip_compares_them():
    project = {"name": "autoscout", "dependencies": ["six==1.0", "attrs==25.*"], "optional-dependencies": {}}

    plan = environment_plan(project, {"six": "1.0.0", "attrs": "25.3.0"})

    assert plan.stale == (("autoscout", "", ""),)


def test_a_requirement_with_a_marker_is_compared_only_when_installed():
    project = {"dependencies": ["appnope==0.1.4; sys_platform == 'darwin'"], "optional-dependencies": {}}

    assert environment_plan(project, {}).stale == ()
    assert environment_plan(project, {"appnope": "0.1.3"}).stale == (("appnope", "0.1.4", "0.1.3"),)


def test_an_extra_seen_in_use_stays_installed_after_a_killed_install_removed_its_packages(tmp_path):
    repo, remote = _project_checkout(tmp_path)
    code_gate.ensure_passed(repo, runner=_Check(), venv=_Venv(CURRENT))
    _push_from_elsewhere(tmp_path, remote, "pyproject.toml", _pyproject("8.5.0"))
    _git(repo, "pull", "-q", "--ff-only")
    without_pypdf = {name: version for name, version in CURRENT.items() if name != "pypdf"}
    venv = _Venv(without_pypdf, after={**CURRENT, "pytest": "8.5.0"})

    verdict = code_gate.ensure_passed(repo, runner=_Check(), venv=venv)

    assert verdict["status"] == "passed"
    assert venv.installs[0]["extras"] == ("dev", "pw")


# ── The update inside the gate ─────────────────────────────────────────────


def test_a_venv_behind_the_pins_is_updated_logged_and_then_checked(tmp_path):
    repo, _ = _project_checkout(tmp_path, pytest_pin="8.5.0")
    venv = _Venv(CURRENT, after={**CURRENT, "pytest": "8.5.0", "iniconfig": "2.3.1"})
    check = _Check()

    verdict = code_gate.ensure_passed(repo, runner=check, venv=venv)

    assert verdict["status"] == "passed" and verdict["checked"] is True
    assert venv.installs[0]["extras"] == ("dev", "pw")
    assert venv.installs[0]["timeout"] == code_gate.INSTALL_TIMEOUT_SECONDS
    assert len(check.calls) == 1
    changes = [
        {"package": "iniconfig", "before": "", "after": "2.3.1"},
        {"package": "pytest", "before": "8.4.2", "after": "8.5.0"},
    ]
    assert verdict["environment_update"]["packages"] == changes
    record = json.loads((repo / GATE_DIR / RECORD_FILE).read_text(encoding="utf-8"))
    assert record["environment_update"]["packages"] == changes
    log = (repo / GATE_DIR / INSTALL_LOG_FILE).read_text(encoding="utf-8")
    assert "pytest: want 8.5.0, have 8.4.2" in log and "pytest: 8.4.2 -> 8.5.0" in log


def test_the_install_gets_its_own_temporary_folder_and_none_of_the_service_variables(tmp_path, monkeypatch):
    repo, _ = _project_checkout(tmp_path, pytest_pin="8.5.0")
    monkeypatch.setenv("AUTOSCOUT_NOTIFY_SMTP_PASSWORD", "secret")
    venv = _Venv(CURRENT, after={**CURRENT, "pytest": "8.5.0"})

    code_gate.ensure_passed(repo, runner=_Check(), venv=venv)

    env = venv.installs[0]["env"]
    assert not [key for key in env if key.startswith("AUTOSCOUT_")]
    assert Path(env["TMPDIR"]).parent == tmp_path / "gate-tmp"
    assert not Path(env["TMPDIR"]).exists()


def test_a_venv_on_the_pins_is_not_touched(tmp_path):
    repo, _ = _project_checkout(tmp_path)
    venv = _Venv(CURRENT)

    verdict = code_gate.ensure_passed(repo, runner=_Check(), venv=venv)

    assert verdict["status"] == "passed" and "environment_update" not in verdict
    assert venv.installs == []


@pytest.mark.parametrize(
    "venv",
    [_Venv(CURRENT, exists=False), _Venv(None)],
    ids=["no .venv", "packages cannot be listed"],
)
def test_without_a_judgeable_venv_nothing_is_installed_and_the_check_decides(tmp_path, venv):
    repo, _ = _project_checkout(tmp_path, pytest_pin="8.5.0")
    check = _Check()

    verdict = code_gate.ensure_passed(repo, runner=check, venv=venv)

    assert venv.installs == [] and len(check.calls) == 1
    assert verdict["status"] == "passed"


@pytest.mark.parametrize(
    ("venv", "reason"),
    [
        (_Venv(CURRENT, code=1), REASON_ENVIRONMENT_UPDATE_FAILED),
        (_Venv(CURRENT, after=CURRENT), REASON_ENVIRONMENT_UPDATE_FAILED),
        (_Venv(CURRENT, timeout=True), REASON_ENVIRONMENT_UPDATE_TIMED_OUT),
    ],
    ids=["pip fails", "pip exits 0 but the venv is still behind", "pip times out"],
)
def test_a_failed_install_has_its_own_reason_runs_no_check_and_records_nothing(tmp_path, venv, reason):
    repo, _ = _project_checkout(tmp_path, pytest_pin="8.5.0")
    check = _Check()

    verdict = code_gate.ensure_passed(repo, runner=check, venv=venv)

    assert verdict == {"status": "failed", "reason": reason, "commit": _git(repo, "rev-parse", "HEAD")}
    assert check.calls == []
    assert not (repo / GATE_DIR / RECORD_FILE).exists()
    assert (repo / GATE_DIR / INSTALL_LOG_FILE).is_file()


def test_an_update_followed_by_a_failing_check_still_names_the_update(tmp_path):
    repo, _ = _project_checkout(tmp_path, pytest_pin="8.5.0")
    venv = _Venv(CURRENT, after={**CURRENT, "pytest": "8.5.0"})

    verdict = code_gate.ensure_passed(repo, runner=_Check(code=1), venv=venv)

    assert verdict["reason"] == code_gate.REASON_CHECK_FAILED
    assert verdict["environment_update"]["packages"][0]["package"] == "pytest"


# ── The real .venv ─────────────────────────────────────────────────────────


class _Recorder:
    def __init__(self, stdout: str = "", code: int = 0) -> None:
        self.stdout = stdout
        self.code = code
        self.calls: list[dict[str, Any]] = []

    def __call__(self, args, **kwargs):
        self.calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, self.code, stdout=self.stdout)


def test_the_install_is_the_checked_out_project_with_its_extras_and_a_time_limit(tmp_path):
    recorder = _Recorder(stdout="Successfully installed pytest-8.5.0\n")
    venv = ServerVenv(tmp_path, runner=recorder)

    code, output = venv.install(("dev", "pw"), env={"PATH": "/bin"}, timeout=12)

    call = recorder.calls[0]
    assert call["args"][0] == str(tmp_path / ".venv" / "bin" / "python")
    assert call["args"][1:4] == ["-m", "pip", "install"]
    assert call["args"][-2:] == ["-e", ".[dev,pw]"]
    assert call["cwd"] == tmp_path and call["timeout"] == 12
    assert (code, output) == (0, "Successfully installed pytest-8.5.0\n")


def test_the_installed_packages_are_read_from_the_venvs_own_interpreter(tmp_path):
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    python.chmod(0o755)
    venv = ServerVenv(tmp_path)

    installed = venv.installed()

    assert venv.exists()
    assert installed is not None and installed.get("pytest") == __import__("pytest").__version__


def test_an_unreadable_package_list_is_none():
    assert ServerVenv(Path("/nonexistent"), runner=_Recorder(stdout="not json")).installed() is None
    assert ServerVenv(Path("/nonexistent"), runner=_Recorder(code=1)).installed() is None


# ── The scheduled run ──────────────────────────────────────────────────────


def _real_adapters(tmp_path: Path, repo: Path, check: _Check, venv: _Venv):
    config, output_dir, adapters = _fixture_run(tmp_path)
    log = repo / "runs" / "weekly" / "workflow-log.json"
    adapters.repository = GitRepositoryAdapter(repo, expected_branch="main", run_log_path=log)
    adapters.code_gate = ProductionCodeGate(repo, runner=check, venv=venv)
    return config, output_dir, adapters


def test_a_night_that_pulls_changed_dev_pins_updates_the_venv_passes_the_gate_and_scans(tmp_path, monkeypatch):
    repo, remote = _project_checkout(tmp_path)
    venv = _Venv(CURRENT, after={**CURRENT, "pytest": "8.5.0"})
    check = _Check()
    code_gate.ensure_passed(repo, runner=check, venv=venv)
    _push_from_elsewhere(tmp_path, remote, "pyproject.toml", _pyproject("8.5.0"))
    config, output_dir, adapters = _real_adapters(tmp_path, repo, check, venv)

    first = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)

    assert first.outcome == WorkflowOutcome.RESTART_ON_UPDATED_CODE
    assert first.run_summary == {RESTART_AFTER_ENVIRONMENT_UPDATE: True}
    assert len(venv.installs) == 1 and len(check.calls) == 2
    assert not (repo / GUARD_FILENAME).exists()

    # The command starts the run again, as _run_weekly_command does.
    monkeypatch.setenv(RESTARTED_ENV, "1")
    monkeypatch.setenv(ENVIRONMENT_UPDATED_ENV, "1")
    second = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)

    assert len(venv.installs) == 1 and len(check.calls) == 2
    stages = [entry["stage"] for entry in second.log]
    assert stages.index(CODE_GATE_STAGE) < stages.index("operation-start")
    entry = next(entry for entry in second.log if entry["stage"] == CODE_GATE_STAGE)
    assert entry["status"] == "passed" and entry["detail"]["checked"] is False
    assert entry["detail"]["environment_update"]["packages"] == [
        {"package": "pytest", "before": "8.4.2", "after": "8.5.0"}
    ]
    body = run_report(second, run_date=RUN_DATE, kirkus_attempt=None).body
    assert "Окружение .venv на сервере обновлено ночью под новый код: pytest 8.4.2 → 8.5.0." in body


def test_a_failed_install_stops_the_night_before_any_state_and_releases_the_lock(tmp_path):
    repo, remote = _project_checkout(tmp_path)
    code_gate.ensure_passed(repo, runner=_Check(), venv=_Venv(CURRENT))
    _push_from_elsewhere(tmp_path, remote, "pyproject.toml", _pyproject("8.5.0"))
    check = _Check()
    config, output_dir, adapters = _real_adapters(tmp_path, repo, check, _Venv(CURRENT, code=1))
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "candidate-history.json").write_text("{}\n", encoding="utf-8")
    before = _snapshot(state_dir)

    result = run_weekly_workflow(config, output_dir, state_dir, adapters=adapters, scheduled=True)

    assert result.outcome == WorkflowOutcome.INTERVENTION_REQUIRED
    assert result.failed_stage == CODE_GATE_STAGE
    assert check.calls == []
    assert _snapshot(state_dir) == before
    assert not (repo / GUARD_FILENAME).exists()
    assert "operation-start" not in [entry["stage"] for entry in result.log]
    failed = next(entry for entry in result.log if entry["stage"] == CODE_GATE_STAGE)
    assert failed["reason"] == REASON_ENVIRONMENT_UPDATE_FAILED
    assert "Не удалось обновить окружение Python на сервере" in render_workflow_result(result)


class _Gate:
    def __init__(self, verdict: dict[str, Any]) -> None:
        self.verdict = verdict

    def status(self) -> dict[str, Any]:
        return {"status": "not_checked", "commit": "a" * 40}

    def ensure_passed(self) -> dict[str, Any]:
        return self.verdict

    def announce(self, line: str) -> None:
        pass


class _Repository:
    def __init__(self, *, pulled: bool) -> None:
        self.pulled = pulled

    def set_run_log_path(self, path):
        pass

    def preflight(self, validated_artifact_allowlist=None):
        return {"status": "ok"}

    def update(self, validated_artifact_allowlist=None):
        return {"status": "ok", "pulled": self.pulled}

    def finalize(self, output_dir):
        return {"status": "ok"}

    def release_guard(self):
        pass


UPDATE = {"updated_at": "2026-11-18T00:31:00+00:00", "packages": [{"package": "ruff", "before": "1", "after": "2"}]}


def test_an_update_without_new_commits_restarts_the_run_once(tmp_path, monkeypatch):
    config, output_dir, adapters = _fixture_run(tmp_path)
    adapters.repository = _Repository(pulled=False)
    adapters.code_gate = _Gate({"status": "passed", "commit": "b" * 40, "checked": True, "environment_update": UPDATE})

    first = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)
    monkeypatch.setenv(RESTARTED_ENV, "1")
    monkeypatch.setenv(ENVIRONMENT_UPDATED_ENV, "1")
    again = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)

    assert first.outcome == WorkflowOutcome.RESTART_ON_UPDATED_CODE
    assert again.outcome.value.startswith("completed/"), again.failed_stage


def test_a_start_restarted_on_pulled_code_that_then_installs_restarts_once_more(tmp_path, monkeypatch):
    config, output_dir, adapters = _fixture_run(tmp_path)
    adapters.repository = _Repository(pulled=True)
    adapters.code_gate = _Gate({"status": "passed", "commit": "b" * 40, "checked": True, "environment_update": UPDATE})
    monkeypatch.setenv(RESTARTED_ENV, "1")

    second = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)

    assert second.outcome == WorkflowOutcome.RESTART_ON_UPDATED_CODE
    assert second.run_summary == {RESTART_AFTER_ENVIRONMENT_UPDATE: True}


def test_an_update_recorded_on_an_earlier_night_is_not_reported_again(tmp_path, monkeypatch):
    config, output_dir, adapters = _fixture_run(tmp_path)
    adapters.repository = _Repository(pulled=False)
    adapters.code_gate = _Gate({"status": "passed", "commit": "b" * 40, "checked": False, "environment_update": UPDATE})
    monkeypatch.setenv(RESTARTED_ENV, "1")

    result = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)

    entry = next(entry for entry in result.log if entry["stage"] == CODE_GATE_STAGE)
    assert "environment_update" not in entry["detail"]
    assert "Окружение .venv" not in run_report(result, run_date=RUN_DATE, kirkus_attempt=None).body


def test_a_manual_run_never_installs(tmp_path):
    repo, _ = _project_checkout(tmp_path, pytest_pin="8.5.0")
    venv = _Venv(CURRENT, after={**CURRENT, "pytest": "8.5.0"})
    check = _Check()
    config, output_dir, adapters = _real_adapters(tmp_path, repo, check, venv)

    run_weekly_workflow(config, output_dir, adapters=adapters)

    assert venv.installs == [] and check.calls == []


def test_the_command_marks_a_restart_after_an_update_for_the_next_start(monkeypatch):
    import autoscout.cli.weekly as cli

    # Recorded first, so the value the command sets is removed after the test.
    monkeypatch.setenv(ENVIRONMENT_UPDATED_ENV, "")
    seen: list[str | None] = []
    restart_result = WeeklyWorkflowResult(
        outcome=WorkflowOutcome.RESTART_ON_UPDATED_CODE, run_summary={RESTART_AFTER_ENVIRONMENT_UPDATE: True}
    )
    monkeypatch.setattr(
        weekly_workflow, "weekly_adapters", lambda *args, **kwargs: __import__("types").SimpleNamespace()
    )
    monkeypatch.setattr(cli, "run_weekly_workflow", lambda *args, **kwargs: restart_result)

    cli._run_weekly_command(
        Path("config.json"),
        Path("out"),
        run_date=RUN_DATE,
        scheduled=True,
        restart=lambda: seen.append(os.environ.get(ENVIRONMENT_UPDATED_ENV)),
    )

    assert seen == ["1"]


# ── The morning report ─────────────────────────────────────────────────────


def _failed_install(reason: str) -> WeeklyWorkflowResult:
    return WeeklyWorkflowResult(
        outcome=WorkflowOutcome.INTERVENTION_REQUIRED,
        failed_stage=CODE_GATE_STAGE,
        log=[
            {"stage": "capture", "status": "ok", "detail": BOOK_TITLE},
            {"stage": CODE_GATE_STAGE, "status": "failed", "reason": reason, "detail": {"commit": "0123456789ab"}},
        ],
    )


@pytest.mark.parametrize("reason", [REASON_ENVIRONMENT_UPDATE_FAILED, REASON_ENVIRONMENT_UPDATE_TIMED_OUT])
def test_the_morning_report_of_a_failed_install_says_the_environment_not_the_code_failed(monkeypatch, reason):
    code, transport = _run_command(monkeypatch, _failed_install(reason))

    assert code == 1
    subject, body = _the_report(transport)
    assert subject.endswith("— остановлен")
    assert f"причина: {reason}." in body
    assert "окружение .venv на сервере отстало от версий в pyproject.toml" in body
    assert "Сам код не виноват" in body and "данные не изменены" in body


def test_the_report_names_only_package_tokens_and_cuts_a_long_list():
    packages = [{"package": f"pkg{n}", "before": "1.0", "after": "2.0"} for n in range(10)]
    packages.append({"package": BOOK_TITLE, "before": "1", "after": "2"})
    result = WeeklyWorkflowResult(
        outcome=WorkflowOutcome.COMPLETED_NO_ATTENTION,
        log=[
            {
                "stage": CODE_GATE_STAGE,
                "status": "passed",
                "detail": {"environment_update": {"updated_at": "x", "packages": packages}},
            }
        ],
    )

    body = run_report(result, run_date=RUN_DATE, kirkus_attempt=None).body

    assert BOOK_TITLE not in body
    assert "pkg7 1.0 → 2.0 и ещё 2." in body and "pkg8" not in body


# ── ./scripts/code-gate --run by hand ──────────────────────────────────────


def test_code_gate_run_by_hand_updates_the_venv_the_same_way(tmp_path, monkeypatch, capsys):
    repo, _ = _project_checkout(tmp_path, pytest_pin="8.5.0")
    venv = _Venv(CURRENT, after={**CURRENT, "pytest": "8.5.0"})
    monkeypatch.setattr(code_gate, "ServerVenv", lambda root: venv)
    real_run_check = code_gate.run_check
    monkeypatch.setattr(code_gate, "run_check", lambda root: real_run_check(root, runner=_Check()))

    code = code_gate.code_gate_command(repo, run=True)

    out = capsys.readouterr().out
    assert code == 0 and len(venv.installs) == 1
    assert "updated .venv to the pins: pytest 8.4.2 -> 8.5.0." in out
    assert "passed ./scripts/check" in out


def test_code_gate_run_by_hand_says_when_the_install_failed(tmp_path, monkeypatch, capsys):
    repo, _ = _project_checkout(tmp_path, pytest_pin="8.5.0")
    monkeypatch.setattr(code_gate, "ServerVenv", lambda root: _Venv(CURRENT, code=1))

    code = code_gate.code_gate_command(repo, run=True)

    out = capsys.readouterr().out
    assert code == 1
    assert f"could not update .venv to the pins ({REASON_ENVIRONMENT_UPDATE_FAILED})" in out
