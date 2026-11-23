"""A scheduled start survives a checked-out version that cannot even be imported (#1169)."""

from __future__ import annotations

import ast
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import types
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from autoscout import code_gate
from autoscout import code_gate_fallback
from autoscout import operator_notice
from autoscout import weekly_workflow
from autoscout.code_gate import GATE_DIR
from autoscout.code_gate import REASON_CHECK_FAILED
from autoscout.code_gate import RECORD_FILE
from autoscout.code_gate import ProductionCodeGate
from autoscout.code_gate_fallback import FALLBACK_ENV
from autoscout.code_gate_fallback import START_ENV
from autoscout.code_gate_fallback import START_SNAPSHOT_DIR
from autoscout.git_envelope import GUARD_FILENAME
from autoscout.git_envelope import GitRepositoryAdapter
from autoscout.operator_notice import run_report
from autoscout.repository_root import REPOSITORY_ROOT_ENV
from autoscout.weekly_workflow import CODE_GATE_STAGE
from autoscout.weekly_workflow import LAST_PASSED_CODE_PLAN
from autoscout.weekly_workflow import RESTARTED_ENV
from autoscout.weekly_workflow import WorkflowOutcome
from autoscout.weekly_workflow import run_weekly_workflow
from tests.test_issue_1057_nightly_run import _fixture_run
from tests.test_issue_1124_code_gate import _Check
from tests.test_issue_1124_code_gate import _checkout
from tests.test_issue_1124_code_gate import _commit
from tests.test_issue_1124_code_gate import _Gate
from tests.test_issue_1124_code_gate import _git
from tests.test_issue_1124_code_gate import _OrderedRepository
from tests.test_issue_1124_code_gate import _push_from_elsewhere
from tests.test_issue_1130_gate_updates_venv import _Venv
from tests.test_issue_1159_last_passing_code import MEETS_PINS
from tests.test_issue_1159_last_passing_code import PYPROJECT
from tests.test_issue_1159_last_passing_code import READY

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "deploy" / "server" / "autoscout-scheduled-start"
WRAPPER = REPO_ROOT / "deploy" / "server" / "autoscout-scheduled-run"
BROKEN_MAIN = "raise ImportError('a module the command imports is gone')\n"
PROTOCOLS = (
    f"FALLBACK_PROTOCOL = {code_gate_fallback.FALLBACK_PROTOCOL}\n"
    f"START_PROTOCOL = {code_gate_fallback.START_PROTOCOL}\n"
)
# The command of the passing commit: it records how it was started, never what it was asked to do.
RECORDING_MAIN = """import json, os, sys
import autoscout
if __name__ == "__main__":
    keys = ("PYTHONPATH", "PYTHONSAFEPATH", "AUTOSCOUT_REPOSITORY_ROOT", "AUTOSCOUT_START_FROM_LAST_PASSED",
            "AUTOSCOUT_SECOND_ATTEMPT")
    with open(os.environ["RECORD"], "w", encoding="utf-8") as out:
        json.dump({"argv": sys.argv[1:], "package": autoscout.__file__, "cwd": os.getcwd(),
                   "env": {key: os.environ.get(key) for key in keys}}, out)
"""
STUB = """#!/usr/bin/env bash
echo "$(basename "$0") [$*] second_attempt=${AUTOSCOUT_SECOND_ATTEMPT:-unset}" >> "$CALLS"
"""


@pytest.fixture(autouse=True)
def _clean_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(code_gate.TMPDIR_ENV, str(tmp_path / "gate-tmp"))
    for name in (RESTARTED_ENV, FALLBACK_ENV, START_ENV, weekly_workflow.ENVIRONMENT_UPDATED_ENV):
        monkeypatch.delenv(name, raising=False)


def _launcher_module() -> types.ModuleType:
    loader = importlib.machinery.SourceFileLoader("autoscout_scheduled_start", str(LAUNCHER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


# ── The launcher, on a checkout whose command cannot be imported ────────────


def _server_checkout(tmp_path: Path, *, protocol: str = PROTOCOLS, record_pass: bool = True) -> tuple[Path, str]:
    """A checkout whose recorded pass has a working command, now on a commit whose command import raises."""
    repo, _ = _checkout(tmp_path)
    ignored = (repo / ".gitignore").read_text(encoding="utf-8") + "/.venv/\n__pycache__/\n"
    _commit(repo, ".gitignore", ignored, "ignore what the server keeps")
    _commit(repo, "autoscout/__init__.py", "", "package")
    _commit(repo, "autoscout/__main__.py", RECORDING_MAIN, "command")
    _commit(repo, "autoscout/code_gate_fallback.py", protocol, "protocols")
    passed = _git(repo, "rev-parse", "HEAD")
    if record_pass:
        (repo / GATE_DIR).mkdir(parents=True, exist_ok=True)
        (repo / GATE_DIR / RECORD_FILE).write_text(json.dumps({"commit": passed}), encoding="utf-8")
    for name in ("run-weekly", "doctor"):
        (repo / "scripts").mkdir(exist_ok=True)
        (repo / "scripts" / name).write_text(STUB, encoding="utf-8")
        (repo / "scripts" / name).chmod(0o755)
    _git(repo, "add", "scripts")
    _commit(repo, "autoscout/__main__.py", BROKEN_MAIN, "a merge that breaks the command")
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").symlink_to(sys.executable)
    return repo, passed


def _launch(
    tmp_path: Path, repo: Path, job: str = "run", **extra: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    record, calls = tmp_path / "record.json", tmp_path / "calls.txt"
    calls.write_text("", encoding="utf-8")
    environ = {key: value for key, value in os.environ.items() if not key.startswith(("AUTOSCOUT_", "PYTHON"))}
    environ.update(RECORD=str(record), CALLS=str(calls), HOME=str(tmp_path / "home"), **extra)
    done = subprocess.run(
        [sys.executable, "-I", str(LAUNCHER), job, str(repo)],
        cwd=repo,
        env=environ,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    return done, record


def _calls(tmp_path: Path) -> list[str]:
    return (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()


def test_a_checkout_that_starts_runs_the_same_command_as_before(tmp_path):
    repo, _ = _server_checkout(tmp_path)
    _commit(repo, "autoscout/__main__.py", "", "a fix")

    done, record = _launch(tmp_path, repo)

    assert done.returncode == 0, done.stdout + done.stderr
    assert _calls(tmp_path) == ["run-weekly [--scheduled] second_attempt=unset"]
    assert done.stdout == "" and not record.exists()
    assert not (repo / START_SNAPSHOT_DIR).exists()


def test_a_checkout_whose_command_cannot_be_imported_starts_from_the_last_pass(tmp_path):
    repo, passed = _server_checkout(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")

    done, record = _launch(tmp_path, repo, AUTOSCOUT_SECOND_ATTEMPT="1")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "cannot start (ImportError)" in done.stdout
    assert _calls(tmp_path) == []
    started = json.loads(record.read_text(encoding="utf-8"))
    snapshot = (repo / START_SNAPSHOT_DIR).resolve()
    assert started["argv"] == [
        "run-weekly",
        "--config",
        "configs/current-weekly-pilot-live.json",
        "--output-dir",
        "runs/current-weekly-pilot-russian",
        "--state-dir",
        "runs/current-weekly-pilot-russian",
        "--run-date",
        date.today().isoformat(),
        "--scheduled",
    ]
    assert Path(started["package"]).resolve().is_relative_to(snapshot)
    assert Path(started["cwd"]).resolve() == repo.resolve()
    assert started["env"] == {
        "PYTHONPATH": str(snapshot),
        "PYTHONSAFEPATH": "1",
        REPOSITORY_ROOT_ENV: str(repo.resolve()),
        START_ENV: str(code_gate_fallback.START_PROTOCOL),
        "AUTOSCOUT_SECOND_ATTEMPT": "1",
    }
    assert json.loads((snapshot / "snapshot.json").read_text(encoding="utf-8")) == {"commit": passed}
    # The production checkout is never moved.
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize(
    ("broken", "kind"),
    [
        ("raise SystemExit(0)\n", "ExitedWhileImported"),
        ("import os\nos.write(2, bytes([255, 254]))\nraise ImportError()\n", "ImportError"),
    ],
)
def test_an_import_that_exits_quietly_or_writes_undecodable_bytes_still_starts_from_the_last_pass(
    tmp_path, broken, kind
):
    repo, _ = _server_checkout(tmp_path)
    _commit(repo, "autoscout/__main__.py", broken, "another broken command")

    done, record = _launch(tmp_path, repo)

    assert done.returncode == 0, done.stdout + done.stderr
    assert f"cannot start ({kind})" in done.stdout
    assert record.exists() and _calls(tmp_path) == []


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ({"record_pass": False}, "no_recorded_pass"),
        ({"protocol": "FALLBACK_PROTOCOL = 1\n"}, "last_pass_predates_start"),
    ],
)
def test_a_night_that_cannot_start_from_the_last_pass_either_sends_its_report(tmp_path, setup, reason):
    repo, _ = _server_checkout(tmp_path, **setup)

    done, record = _launch(tmp_path, repo)

    assert done.returncode == 1
    assert not record.exists() and _calls(tmp_path) == []
    today = date.today().isoformat()
    assert f"AutoScout: ночной поиск {today} — остановлен\n" in done.stdout
    assert f"Ночной поиск {today} не начат: установленная версия кода не запускается (ImportError)." in done.stdout
    assert f"({reason})." in done.stdout
    assert done.stdout.rstrip().endswith("Журнал: journalctl --user -u autoscout-run.service")


def test_a_last_pass_that_cannot_be_imported_either_is_reported_not_started(tmp_path):
    repo, _ = _server_checkout(tmp_path)
    (repo / GATE_DIR / RECORD_FILE).write_text(json.dumps({"commit": _git(repo, "rev-parse", "HEAD")}), "utf-8")

    done, record = _launch(tmp_path, repo, AUTOSCOUT_SECOND_ATTEMPT="1")

    assert done.returncode == 1 and not record.exists()
    assert "— остановлен (повторная попытка)" in done.stdout
    assert "(last_pass_cannot_start)." in done.stdout
    assert done.stdout.rstrip().endswith("Журнал: journalctl --user -u autoscout-run-retry.service")


def test_the_doctor_runs_when_the_checkout_starts_and_is_skipped_in_one_line_otherwise(tmp_path):
    repo, _ = _server_checkout(tmp_path)

    skipped, _ = _launch(tmp_path, repo, "doctor")
    _commit(repo, "autoscout/__main__.py", "", "a fix")
    ran, _ = _launch(tmp_path, repo, "doctor")

    assert skipped.returncode == 0
    assert "Traceback" not in skipped.stdout
    assert skipped.stdout.splitlines()[-1] == "=== doctor skipped: the checked-out version cannot start (ImportError)"
    assert ran.returncode == 0 and _calls(tmp_path) == ["doctor [] second_attempt=unset"]


def test_the_wrapper_starts_the_search_and_the_doctor_through_the_launcher(tmp_path):
    repo, _ = _server_checkout(tmp_path)
    _commit(repo, "autoscout/__main__.py", "", "a fix")
    environ = {key: value for key, value in os.environ.items() if not key.startswith(("AUTOSCOUT_", "PYTHON"))}
    environ.update(AUTOSCOUT_SCHEDULED_REPO=str(repo), CALLS=str(tmp_path / "calls.txt"), HOME=str(tmp_path / "h"))
    (tmp_path / "calls.txt").write_text("", encoding="utf-8")

    done = subprocess.run(["bash", str(WRAPPER)], env=environ, capture_output=True, text=True, check=False)

    assert done.returncode == 0, done.stdout + done.stderr
    assert _calls(tmp_path) == ["run-weekly [--scheduled] second_attempt=unset", "doctor [] second_attempt=unset"]
    assert "not installed" not in done.stdout


# ── The launcher's copies stay equal to the package ────────────────────────


def test_the_launcher_imports_nothing_but_the_standard_library():
    tree = ast.parse(LAUNCHER.read_text(encoding="utf-8"))
    imported = {
        name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for name in ([alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""])
    }

    assert imported <= set(sys.stdlib_module_names) | {"__future__"}


def test_the_launchers_copies_of_the_shared_values_equal_the_packages():
    from autoscout import second_attempt

    launcher = _launcher_module()

    assert launcher.START_PROTOCOL == code_gate_fallback.START_PROTOCOL
    assert launcher.START_ENV == code_gate_fallback.START_ENV
    assert launcher.START_SNAPSHOT_DIR == code_gate_fallback.START_SNAPSHOT_DIR
    assert launcher.SNAPSHOT_STAMP == code_gate_fallback.SNAPSHOT_STAMP
    assert launcher.RECORD_FILE == GATE_DIR / RECORD_FILE
    assert launcher.REPOSITORY_ROOT_ENV == REPOSITORY_ROOT_ENV
    assert launcher.SECOND_ATTEMPT_ENV == second_attempt.SECOND_ATTEMPT_ENV
    assert launcher.REFUSED_NO_PASS == code_gate_fallback.REFUSED_NO_PASS
    assert launcher.REFUSED_UNAVAILABLE == code_gate_fallback.REFUSED_UNAVAILABLE
    assert launcher.PROTOCOL_MODULE == "autoscout/code_gate_fallback.py"
    for name in ("ENV_HOST", "ENV_PORT", "ENV_USER", "ENV_PASSWORD", "ENV_FROM", "ENV_TO"):
        assert getattr(launcher, name) == getattr(operator_notice, name)
    for name in ("DEFAULT_SMTP_PORT", "SMTPS_PORT", "SMTP_TIMEOUT_SECONDS", "RUN_JOURNAL", "SECOND_ATTEMPT_JOURNAL"):
        assert getattr(launcher, name) == getattr(operator_notice, name)
    assert launcher.SECOND_ATTEMPT_SUBJECT == operator_notice.SECOND_ATTEMPT_SUBJECT
    assert launcher.VERDICT_STOPPED == operator_notice.VERDICT_STOPPED
    for name in ("REPORT_RECORD_ENV", "REPORT_SENT_LINE", "REPORT_NOT_CONFIGURED_PREFIX"):
        assert getattr(launcher, name) == getattr(operator_notice, name)


def test_this_code_declares_the_start_protocol_the_launcher_looks_for():
    launcher = _launcher_module()
    source = (REPO_ROOT / "autoscout" / "code_gate_fallback.py").read_text(encoding="utf-8")

    assert launcher._DECLARES_PROTOCOL.search(source)
    assert code_gate_fallback.declares_protocol(source)


def test_the_launcher_starts_the_snapshot_as_the_fallback_does():
    launcher = _launcher_module()
    repo, snapshot = Path("/srv/checkout"), Path("/srv/checkout/data/code-gate/start")
    base = {"PATH": "/usr/bin", "PYTHONPATH": "/elsewhere"}

    started = launcher.start_environment(repo, snapshot, base)
    fallback = code_gate_fallback.environment({**READY, "snapshot": str(snapshot)}, repo, base)

    assert {k: v for k, v in started.items() if k != START_ENV} == {
        k: v for k, v in fallback.items() if k != FALLBACK_ENV
    }


def test_the_launcher_runs_the_same_arguments_as_scripts_run_weekly():
    launcher = _launcher_module()
    script = (REPO_ROOT / "scripts" / "run-weekly").read_text(encoding="utf-8")
    exec_line = script[script.index('exec "${INTERPRETER}" -m autoscout run-weekly') :]
    arguments = exec_line.split("--run-date")[0].replace("\\", " ").split()[5:]

    assert tuple(arguments) == launcher.RUN_WEEKLY_ARGS


# ── The last passing code, started for a checkout that could not start ─────


PASSED = {"status": "passed", "reason": "", "commit": "0123456789abcdef" * 2 + "01234567", "checked": True}


def test_a_start_from_the_last_pass_whose_checkout_passes_restarts_on_it_before_any_state(tmp_path, monkeypatch):
    config, output_dir, adapters = _fixture_run(tmp_path)
    calls: list[str] = []
    adapters.repository = _OrderedRepository(calls, pulled=False)
    adapters.code_gate = _Gate(PASSED, calls=calls)
    monkeypatch.setenv(START_ENV, "1")

    result = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)

    assert result.outcome == WorkflowOutcome.RESTART_ON_UPDATED_CODE
    assert calls == ["update", "gate", "release"]
    assert not (output_dir / "workflow-log.json").exists()


def test_an_ordinary_start_on_code_that_passes_goes_on_as_before(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path)
    adapters.repository = _OrderedRepository([], pulled=False)
    adapters.code_gate = _Gate(PASSED)

    result = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)

    assert result.outcome.value.startswith("completed/"), result.failed_stage


def test_the_restart_leaves_the_snapshot_for_the_checked_out_code(monkeypatch):
    import autoscout.cli.weekly as cli

    monkeypatch.setenv(START_ENV, "1")
    monkeypatch.setenv("PYTHONPATH", "/checkout/data/code-gate/start")
    monkeypatch.setenv("PYTHONSAFEPATH", "1")
    monkeypatch.setenv(REPOSITORY_ROOT_ENV, "/checkout")
    started: list[dict[str, str]] = []
    monkeypatch.setattr(os, "execv", lambda path, argv: started.append(dict(os.environ)))

    cli._restart_on_updated_code()

    (env,) = started
    assert env[RESTARTED_ENV] == "1"
    assert not {START_ENV, "PYTHONPATH", "PYTHONSAFEPATH", REPOSITORY_ROOT_ENV} & set(env)


def test_leaving_the_start_snapshot_changes_nothing_in_any_other_run():
    environ = {"PYTHONPATH": "/somewhere", REPOSITORY_ROOT_ENV: "/checkout"}

    code_gate_fallback.leave_start_snapshot(environ)

    assert environ == {"PYTHONPATH": "/somewhere", REPOSITORY_ROOT_ENV: "/checkout"}


def test_a_fallback_from_the_start_snapshot_is_an_ordinary_fallback():
    env = code_gate_fallback.environment({**READY, "snapshot": "/s"}, Path("/checkout"), {START_ENV: "1"})

    assert START_ENV not in env and FALLBACK_ENV in env


def _unstartable_checkout(tmp_path: Path, *, changed_config: bool) -> tuple[Path, Path, str]:
    """The server checkout after a failed night: a pass is recorded, a merge whose command import raises is on main."""
    repo, remote = _checkout(tmp_path)
    _commit(repo, "pyproject.toml", PYPROJECT, "project")
    _commit(repo, "autoscout/__init__.py", "", "package")
    _commit(repo, "autoscout/__main__.py", "", "command")
    _commit(repo, "autoscout/code_gate_fallback.py", PROTOCOLS, "protocols")
    _commit(repo, "configs/weekly.json", "{}\n", "config")
    _git(repo, "push", "-q", "origin", "main")
    assert code_gate.ensure_passed(repo, runner=_Check())["status"] == "passed"
    passed = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "autoscout/__main__.py", BROKEN_MAIN, "a merge that breaks the command")
    if changed_config:
        _commit(repo, "configs/weekly.json", '{"new": 1}\n', "a config change")
    _git(repo, "push", "-q", "origin", "main")
    # Another merge, still broken, arrives before the night: the start must pull it.
    _push_from_elsewhere(tmp_path, remote, "autoscout/module.py", "VALUE = 2\n")
    return repo, remote, passed


def _start_from_last_pass(tmp_path: Path, repo: Path, check: _Check, monkeypatch) -> Any:
    config, output_dir, adapters = _fixture_run(tmp_path)
    log = repo / "runs" / "weekly" / "workflow-log.json"
    adapters.repository = GitRepositoryAdapter(repo, expected_branch="main", run_log_path=log)
    adapters.code_gate = ProductionCodeGate(repo, runner=check, venv=_Venv(dict(MEETS_PINS)))
    monkeypatch.setenv(START_ENV, "1")
    return run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)


def test_the_start_from_the_last_pass_pulls_checks_and_falls_back_when_allowed(tmp_path, monkeypatch):
    """The acceptance test of #1169, on the side of the code the launcher starts."""
    repo, _, passed = _unstartable_checkout(tmp_path, changed_config=False)
    check = _Check(code=1)

    result = _start_from_last_pass(tmp_path, repo, check, monkeypatch)

    assert result.outcome == WorkflowOutcome.RUN_ON_LAST_PASSED_CODE, result.failed_stage
    plan = result.run_summary[LAST_PASSED_CODE_PLAN]
    assert plan["ran_commit"] == passed
    # It pulled the night's merge and checked that commit; the checkout stays on it, level with origin.
    assert _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "origin/main") == plan["failed_commit"]
    assert len(check.calls) == 1
    assert _git(repo, "status", "--porcelain") == ""
    assert not (repo / GUARD_FILENAME).exists()
    # The run it starts is an ordinary run on the last passing code, which sends the night's report (#1159).
    env = code_gate_fallback.environment(plan, repo, dict(os.environ))
    assert START_ENV not in env and json.loads(env[FALLBACK_ENV])["ran_commit"] == passed


def test_a_refused_fallback_after_a_start_from_the_last_pass_reports_why(tmp_path, monkeypatch):
    repo, _, _ = _unstartable_checkout(tmp_path, changed_config=True)

    result = _start_from_last_pass(tmp_path, repo, _Check(code=1), monkeypatch)

    assert result.outcome == WorkflowOutcome.INTERVENTION_REQUIRED
    failed = next(entry for entry in result.log if entry["stage"] == CODE_GATE_STAGE)
    assert failed["reason"] == REASON_CHECK_FAILED
    assert failed["detail"]["fallback_refused"] == code_gate_fallback.REFUSED_FILES_CHANGED
    assert failed["detail"]["fallback_refused_paths"] == ["configs/weekly.json"]
    report = run_report(result, run_date="2026-11-21", kirkus_attempt=None)
    assert report.subject == "AutoScout: ночной поиск 2026-11-21 — остановлен"
    assert "Предыдущая версия кода, прошедшая проверку, тоже не запускалась" in report.body
    assert not (repo / GUARD_FILENAME).exists()


def test_a_fix_pulled_by_the_start_from_the_last_pass_is_checked_and_restarted_on(tmp_path, monkeypatch):
    repo, remote, _ = _unstartable_checkout(tmp_path, changed_config=False)
    _push_from_elsewhere(tmp_path, remote, "autoscout/__main__.py", "")
    check = _Check(code=0)

    result = _start_from_last_pass(tmp_path, repo, check, monkeypatch)

    assert result.outcome == WorkflowOutcome.RESTART_ON_UPDATED_CODE, result.failed_stage
    assert (repo / "autoscout" / "__main__.py").read_text(encoding="utf-8") == ""
    assert code_gate.is_passed(repo) and len(check.calls) == 1


def test_a_snapshot_of_the_same_commit_is_reused_and_one_of_another_is_replaced(tmp_path):
    repo, passed = _server_checkout(tmp_path)
    launcher = _launcher_module()
    snapshot = launcher.write_snapshot(repo, passed)
    marker = snapshot / "autoscout" / "kept.txt"
    marker.write_text("", encoding="utf-8")

    assert launcher.write_snapshot(repo, passed) == snapshot and marker.exists()

    head = _git(repo, "rev-parse", "HEAD")
    assert launcher.write_snapshot(repo, head) == snapshot and not marker.exists()
    assert json.loads((snapshot / "snapshot.json").read_text(encoding="utf-8")) == {"commit": head}


def test_a_relative_checkout_path_is_resolved(tmp_path):
    repo, _ = _server_checkout(tmp_path)
    record, calls = tmp_path / "record.json", tmp_path / "calls.txt"
    calls.write_text("", encoding="utf-8")
    environ = {key: value for key, value in os.environ.items() if not key.startswith(("AUTOSCOUT_", "PYTHON"))}
    environ.update(RECORD=str(record), CALLS=str(calls), HOME=str(tmp_path / "home"))

    done = subprocess.run(
        [sys.executable, "-I", str(LAUNCHER), "run", repo.name],
        cwd=tmp_path,
        env=environ,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert record.exists()
