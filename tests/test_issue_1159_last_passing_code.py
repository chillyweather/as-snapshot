"""A failed gate runs the last code that passed on the run server (#1159)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from autoscout import code_gate
from autoscout import code_gate_fallback
from autoscout import weekly_workflow
from autoscout.code_gate import GATE_DIR
from autoscout.code_gate import REASON_CHECK_FAILED
from autoscout.code_gate import REASON_CHECKOUT_CHANGED
from autoscout.code_gate import TMPDIR_ENV
from autoscout.code_gate import ProductionCodeGate
from autoscout.code_gate_fallback import FALLBACK_ENV
from autoscout.code_gate_fallback import SNAPSHOT_DIR
from autoscout.code_gate_fallback import STATUS_RAN_LAST_PASSED
from autoscout.git_envelope import GUARD_FILENAME
from autoscout.git_envelope import GitRepositoryAdapter
from autoscout.operator_notice import run_report
from autoscout.repository_root import REPOSITORY_ROOT_ENV
from autoscout.weekly_workflow import CODE_GATE_STAGE
from autoscout.weekly_workflow import LAST_PASSED_CODE_PLAN
from autoscout.weekly_workflow import WeeklyWorkflowResult
from autoscout.weekly_workflow import WorkflowOutcome
from autoscout.weekly_workflow import run_weekly_workflow
from tests.state_store_support import seed_state
from tests.state_store_support import state_snapshot
from tests.test_issue_1057_nightly_run import RUN_DATE
from tests.test_issue_1057_nightly_run import _fixture_run
from tests.test_issue_1057_nightly_run import _ResidueRepository
from tests.test_issue_1057_nightly_run import _run_command
from tests.test_issue_1057_nightly_run import _the_report
from tests.test_issue_1124_code_gate import FAILED
from tests.test_issue_1124_code_gate import _Check
from tests.test_issue_1124_code_gate import _checkout
from tests.test_issue_1124_code_gate import _commit
from tests.test_issue_1124_code_gate import _Gate
from tests.test_issue_1124_code_gate import _git
from tests.test_issue_1124_code_gate import _OrderedRepository
from tests.test_issue_1124_code_gate import _push_from_elsewhere
from tests.test_issue_1130_gate_updates_venv import _Venv

REPO_ROOT = Path(__file__).resolve().parents[1]

PYPROJECT = """[project]
name = "autoscout"
dependencies = ["requests==2.0"]

[project.optional-dependencies]
dev = ["pytest==8.0"]
pw = ["pypdf==4.0"]
"""
MEETS_PINS = {"autoscout": "0.1", "requests": "2.0", "pytest": "8.0"}
PROTOCOL_MODULE = f"FALLBACK_PROTOCOL = {code_gate_fallback.FALLBACK_PROTOCOL}\n"


@pytest.fixture(autouse=True)
def _clean_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(TMPDIR_ENV, str(tmp_path / "gate-tmp"))
    monkeypatch.delenv(weekly_workflow.RESTARTED_ENV, raising=False)
    monkeypatch.delenv(FALLBACK_ENV, raising=False)


def _passed_checkout(tmp_path: Path, *, protocol: bool = True) -> tuple[Path, Path, str]:
    """A checkout whose current commit passed the gate here; returns it, its remote and that commit."""
    repo, remote = _checkout(tmp_path)
    _commit(repo, "pyproject.toml", PYPROJECT, "project")
    _commit(repo, "autoscout/__init__.py", "", "package")
    if protocol:
        _commit(repo, "autoscout/code_gate_fallback.py", PROTOCOL_MODULE, "fallback")
    _commit(repo, "autoscout/state_store/sqlite_schema.py", "MIGRATIONS = ('v1',)\n", "schema")
    _commit(repo, "configs/weekly.json", "{}\n", "config")
    _git(repo, "push", "-q", "origin", "main")
    assert code_gate.ensure_passed(repo, runner=_Check())["status"] == "passed"
    return repo, remote, _git(repo, "rev-parse", "HEAD")


def _plan(repo: Path, *, venv: Any | None = None, verdict: dict[str, Any] | None = None) -> dict[str, Any] | None:
    failed = verdict or {"status": "failed", "reason": REASON_CHECK_FAILED, "commit": _git(repo, "rev-parse", "HEAD")}
    return code_gate_fallback.plan(repo, failed, venv=venv or _Venv(dict(MEETS_PINS)))


# ── When the last passing code may run ─────────────────────────────────────


def test_new_code_that_fails_runs_the_last_passing_commits_package_from_a_snapshot(tmp_path):
    repo, _, passed = _passed_checkout(tmp_path)
    _commit(repo, "autoscout/module.py", "VALUE = 2\n", "new code")
    _commit(repo, "runs/weekly/run-summary.json", "{}\n", "auto-scout: weekly workflow state update")
    _commit(repo, "tests/test_module.py", "\n", "a test")
    _commit(repo, "docs/note.md", "note\n", "a doc")

    plan = _plan(repo)

    assert plan is not None and plan["status"] == "ready", plan
    assert plan["ran_commit"] == passed
    assert plan["failed_commit"] == _git(repo, "rev-parse", "HEAD")
    snapshot = repo / SNAPSHOT_DIR
    assert Path(plan["snapshot"]) == snapshot
    assert (snapshot / "autoscout" / "module.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert json.loads((snapshot / "snapshot.json").read_text(encoding="utf-8")) == {"commit": passed}
    # The run starts again with the checkout's .venv, whose packages met the pins, whatever Python decided.
    assert plan["python"] == str(repo / ".venv" / "bin" / "python")
    # Only the package is in the snapshot: configs and state are read from the checkout.
    assert sorted(path.name for path in snapshot.iterdir()) == ["autoscout", "snapshot.json"]


def test_no_recorded_pass_stops_as_before(tmp_path):
    repo, _ = _checkout(tmp_path)

    plan = _plan(repo)

    assert plan == {"status": "refused", "reason": code_gate_fallback.REFUSED_NO_PASS}
    assert not (repo / SNAPSHOT_DIR).exists()


def test_a_checkout_that_moved_while_the_gate_checked_it_is_not_judged(tmp_path):
    repo, _, _ = _passed_checkout(tmp_path)
    _commit(repo, "autoscout/module.py", "VALUE = 2\n", "the code the gate checked")
    checked = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "autoscout/module.py", "VALUE = 3\n", "a fix pulled during the check")

    plan = _plan(repo, verdict={"status": "failed", "reason": REASON_CHECK_FAILED, "commit": checked})

    assert plan == {"status": "refused", "reason": code_gate_fallback.REFUSED_CHECKOUT_MOVED}
    assert not (repo / SNAPSHOT_DIR).exists()


def test_a_failure_of_the_checkout_itself_is_not_covered(tmp_path):
    repo, _, _ = _passed_checkout(tmp_path)

    assert _plan(repo, verdict={"status": "failed", "reason": REASON_CHECKOUT_CHANGED, "commit": ""}) is None


def test_a_pass_recorded_before_the_fallback_existed_cannot_run(tmp_path):
    repo, _, _ = _passed_checkout(tmp_path, protocol=False)
    _commit(repo, "autoscout/module.py", "VALUE = 2\n", "new code")

    plan = _plan(repo)

    assert plan is not None and plan["reason"] == code_gate_fallback.REFUSED_PREDATES_FALLBACK


def test_a_new_commit_that_changes_a_config_stops(tmp_path):
    repo, _, _ = _passed_checkout(tmp_path)
    _commit(repo, "configs/weekly.json", '{"changed": true}\n', "a config change")

    plan = _plan(repo)

    assert plan is not None and plan["reason"] == code_gate_fallback.REFUSED_FILES_CHANGED
    assert plan["paths"] == ["configs/weekly.json"]
    assert not (repo / SNAPSHOT_DIR).exists()


def test_a_new_commit_that_changes_the_state_format_stops(tmp_path):
    repo, _, _ = _passed_checkout(tmp_path)
    _commit(repo, "autoscout/state_store/sqlite_schema.py", "MIGRATIONS = ('v1', 'v2')\n", "schema version 2")

    plan = _plan(repo)

    assert plan is not None and plan["reason"] == code_gate_fallback.REFUSED_STATE_STORE_CHANGED
    assert plan["paths"] == ["autoscout/state_store/sqlite_schema.py"]


def test_a_venv_already_updated_to_the_new_pins_stops(tmp_path):
    repo, _, _ = _passed_checkout(tmp_path)
    _commit(repo, "pyproject.toml", PYPROJECT.replace("requests==2.0", "requests==2.1"), "bump")

    plan = _plan(repo, venv=_Venv({**MEETS_PINS, "requests": "2.1"}))

    assert plan is not None and plan["reason"] == code_gate_fallback.REFUSED_VENV


def test_a_pass_records_the_extras_it_was_checked_with(tmp_path):
    repo, _ = _checkout(tmp_path)
    _commit(repo, "pyproject.toml", PYPROJECT, "project")

    code_gate.ensure_passed(repo, runner=_Check(), venv=_Venv({**MEETS_PINS, "pypdf": "4.0"}))

    record = json.loads((repo / GATE_DIR / code_gate.RECORD_FILE).read_text(encoding="utf-8"))
    assert record["extras"] == ["pw"]


def test_an_extra_the_pass_used_is_required_after_an_interrupted_install_forgot_it(tmp_path):
    """A killed install of newer code removed pypdf and rewrote extras.json without pw."""
    repo, _, _ = _passed_checkout(tmp_path)
    record_path = repo / GATE_DIR / code_gate.RECORD_FILE
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record_path.write_text(json.dumps({**record, "extras": ["pw"]}), encoding="utf-8")
    (repo / GATE_DIR / code_gate.EXTRAS_FILE).write_text('{"extras": []}', encoding="utf-8")
    _commit(repo, "autoscout/module.py", "VALUE = 2\n", "new code")

    without_pypdf = _plan(repo, venv=_Venv(dict(MEETS_PINS)))
    with_pypdf = _plan(repo, venv=_Venv({**MEETS_PINS, "pypdf": "4.0"}))

    assert without_pypdf is not None and without_pypdf["reason"] == code_gate_fallback.REFUSED_VENV
    assert with_pypdf is not None and with_pypdf["status"] == "ready"


def test_the_venv_packages_are_listed_without_an_inherited_search_path(tmp_path):
    calls: list[list[str]] = []

    def runner(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="{}")

    code_gate.ServerVenv(tmp_path, runner=runner).installed()

    assert calls[0][1] == "-I"


@pytest.mark.parametrize(
    ("marker", "same_interpreter", "status"),
    [
        ('python_version >= "3"', True, "refused"),
        ('python_version < "3"', True, "ready"),
        ('python_version < "3"', False, "refused"),
    ],
)
def test_a_missing_requirement_with_a_marker_stops_unless_its_marker_is_false_here(
    tmp_path, monkeypatch, marker, same_interpreter, status
):
    repo, _, _ = _passed_checkout(tmp_path)
    marked = PYPROJECT.replace('["requests==2.0"]', f"[\"requests==2.0\", 'colorama==0.4; {marker}']")
    _commit(repo, "pyproject.toml", marked, "a marked dependency")
    code_gate.ensure_passed(repo, runner=_Check())
    _commit(repo, "autoscout/module.py", "VALUE = 2\n", "new code")
    if same_interpreter:
        monkeypatch.setattr(sys, "prefix", str(repo / ".venv"))

    plan = _plan(repo)

    assert plan is not None and plan["status"] == status, plan


def test_the_gates_venv_update_reaches_the_started_again_runs_log(tmp_path):
    repo, _, _ = _passed_checkout(tmp_path)
    _commit(repo, "autoscout/module.py", "VALUE = 2\n", "new code")
    update = {
        "updated_at": "2026-11-20T00:00:00+00:00",
        "packages": [{"package": "pytest", "before": "8.0", "after": "8.1"}],
    }
    failed = {
        "status": "failed",
        "reason": REASON_CHECK_FAILED,
        "commit": _git(repo, "rev-parse", "HEAD"),
        "environment_update": update,
    }

    plan = _plan(repo, verdict=failed)
    assert plan is not None and plan["status"] == "ready"
    env = code_gate_fallback.environment(plan, repo, {})
    verdict = code_gate_fallback.confirm(
        repo, env, package_file=repo / SNAPSHOT_DIR / "autoscout" / "__init__.py", interpreter_prefix=repo / ".venv"
    )

    assert verdict["status"] == STATUS_RAN_LAST_PASSED and verdict["environment_update"] == update
    result = WeeklyWorkflowResult(
        outcome=WorkflowOutcome.COMPLETED_NO_ATTENTION,
        log=[{"stage": CODE_GATE_STAGE, "status": STATUS_RAN_LAST_PASSED, "detail": verdict}],
    )
    assert "pytest 8.0 → 8.1" in run_report(result, run_date=RUN_DATE, kirkus_attempt=None).body


def test_a_dev_pin_the_venv_moved_past_does_not_stop(tmp_path):
    repo, _, _ = _passed_checkout(tmp_path)
    _commit(repo, "pyproject.toml", PYPROJECT.replace("pytest==8.0", "pytest==8.1"), "bump a dev pin")

    plan = _plan(repo, venv=_Venv({**MEETS_PINS, "pytest": "8.1"}))

    assert plan is not None and plan["status"] == "ready", plan


# ── The started-again run confirms the plan ────────────────────────────────


def _ready(tmp_path: Path) -> tuple[Path, dict[str, Any], dict[str, str]]:
    repo, _, _ = _passed_checkout(tmp_path)
    _commit(repo, "autoscout/module.py", "VALUE = 2\n", "new code")
    plan = _plan(repo)
    assert plan is not None and plan["status"] == "ready"
    env = code_gate_fallback.environment(plan, repo, {})
    return repo, plan, env


def test_the_started_again_run_confirms_what_was_planned(tmp_path):
    repo, plan, env = _ready(tmp_path)

    verdict = code_gate_fallback.confirm(
        repo,
        env,
        package_file=repo / SNAPSHOT_DIR / "autoscout" / "__init__.py",
        interpreter_prefix=repo / ".venv",
    )

    assert verdict == {
        "status": STATUS_RAN_LAST_PASSED,
        "commit": plan["failed_commit"][:12],
        "ran_commit": plan["ran_commit"][:12],
        "reason": REASON_CHECK_FAILED,
        "passed_at": plan["passed_at"],
    }
    assert env[REPOSITORY_ROOT_ENV] == str(repo.resolve())
    assert env["PYTHONPATH"] == str(repo / SNAPSHOT_DIR) and env["PYTHONSAFEPATH"] == "1"


def test_the_started_again_run_stops_when_the_checkout_moved_or_the_package_is_not_the_snapshot(tmp_path):
    repo, _, env = _ready(tmp_path)
    in_snapshot = repo / SNAPSHOT_DIR / "autoscout" / "__init__.py"

    venv = repo / ".venv"

    not_snapshot = code_gate_fallback.confirm(
        repo, env, package_file=REPO_ROOT / "autoscout" / "__init__.py", interpreter_prefix=venv
    )
    other_python = code_gate_fallback.confirm(repo, env, package_file=in_snapshot, interpreter_prefix=Path(sys.prefix))
    _commit(repo, "autoscout/module.py", "VALUE = 3\n", "newer code")
    moved = code_gate_fallback.confirm(repo, env, package_file=in_snapshot, interpreter_prefix=venv)

    for verdict in (not_snapshot, other_python, moved):
        assert verdict["status"] == "failed"
        assert verdict["reason"] == REASON_CHECK_FAILED
        assert verdict["fallback_refused"] == code_gate_fallback.REFUSED_NOT_CONFIRMED


# ── The scheduled run ──────────────────────────────────────────────────────


class _FallbackGate(_Gate):
    def __init__(self, verdict: dict[str, Any], plan: dict[str, Any] | None, **kwargs: Any) -> None:
        super().__init__(verdict, **kwargs)
        self.plan = plan
        self.handed_over: dict[str, Any] | None = None

    def fallback_plan(self, verdict: dict[str, Any]) -> dict[str, Any] | None:
        self.calls.append("plan")
        return self.plan

    def confirm_fallback(self) -> dict[str, Any] | None:
        return self.handed_over


READY = {"status": "ready", "ran_commit": "a" * 40, "failed_commit": FAILED["commit"], "reason": REASON_CHECK_FAILED}


def test_a_failed_gate_with_a_runnable_pass_starts_again_on_it_before_any_state(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    seed_state(state_dir, candidate_history=[{"candidate_id": "c1", "title": "A Book"}])
    before = state_snapshot(state_dir)
    calls: list[str] = []
    adapters.repository = _OrderedRepository(calls, pulled=True)
    adapters.code_gate = _FallbackGate(FAILED, READY, calls=calls)

    result = run_weekly_workflow(config, output_dir, state_dir, adapters=adapters, scheduled=True)

    assert result.outcome == WorkflowOutcome.RUN_ON_LAST_PASSED_CODE
    assert result.run_summary[LAST_PASSED_CODE_PLAN] == READY
    assert calls == ["update", "gate", "plan", "release"]
    assert state_snapshot(state_dir) == before


def test_a_refused_fallback_stops_the_night_and_logs_why(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path)
    adapters.repository = _OrderedRepository([], pulled=False)
    refused = {"status": "refused", "reason": code_gate_fallback.REFUSED_FILES_CHANGED, "paths": ["configs/x.json"]}
    adapters.code_gate = _FallbackGate(FAILED, refused)

    result = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)

    assert result.outcome == WorkflowOutcome.INTERVENTION_REQUIRED
    failed = next(entry for entry in result.log if entry["stage"] == CODE_GATE_STAGE)
    assert failed["reason"] == REASON_CHECK_FAILED
    assert failed["detail"] == {
        "commit": FAILED["commit"][:12],
        "fallback_refused": code_gate_fallback.REFUSED_FILES_CHANGED,
        "fallback_refused_paths": ["configs/x.json"],
    }


def test_no_recorded_pass_logs_the_failed_gate_as_before(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path)
    adapters.repository = _OrderedRepository([], pulled=False)
    adapters.code_gate = _FallbackGate(FAILED, {"status": "refused", "reason": code_gate_fallback.REFUSED_NO_PASS})

    result = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)

    failed = next(entry for entry in result.log if entry["stage"] == CODE_GATE_STAGE)
    assert failed["detail"] == {"commit": FAILED["commit"][:12]}


def test_the_run_on_the_last_passing_code_goes_on_without_a_check_and_logs_it(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path)
    adapters.repository = _ResidueRepository()
    gate = _FallbackGate(FAILED, None)
    gate.handed_over = {
        "status": STATUS_RAN_LAST_PASSED,
        "commit": "0123456789ab",
        "ran_commit": "aaaaaaaaaaaa",
        "reason": REASON_CHECK_FAILED,
        "passed_at": "2026-11-19T00:00:00+00:00",
    }
    adapters.code_gate = gate

    result = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)

    assert result.outcome.value.startswith("completed/"), result.failed_stage
    assert "gate" not in gate.calls
    entry = next(entry for entry in result.log if entry["stage"] == CODE_GATE_STAGE)
    assert entry["status"] == STATUS_RAN_LAST_PASSED
    assert entry["detail"] == gate.handed_over
    stages = [entry["stage"] for entry in result.log]
    assert stages.index(CODE_GATE_STAGE) < stages.index("operation-start")


def test_a_started_again_run_that_cannot_confirm_stops_before_any_state(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path)
    adapters.repository = _ResidueRepository()
    gate = _FallbackGate(FAILED, None)
    gate.handed_over = {
        "status": "failed",
        "reason": REASON_CHECK_FAILED,
        "commit": FAILED["commit"],
        "fallback_refused": code_gate_fallback.REFUSED_NOT_CONFIRMED,
    }
    adapters.code_gate = gate

    result = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)

    assert result.failed_stage == CODE_GATE_STAGE
    assert "operation-start" not in [entry["stage"] for entry in result.log]


def test_the_command_starts_again_on_the_plan_and_sends_no_report(monkeypatch):
    import autoscout.cli.weekly as cli
    from tests.test_issue_1057_nightly_run import _Transport

    monkeypatch.setattr(weekly_workflow, "weekly_adapters", lambda *args, **kwargs: types.SimpleNamespace())
    result = WeeklyWorkflowResult(
        outcome=WorkflowOutcome.RUN_ON_LAST_PASSED_CODE, run_summary={LAST_PASSED_CODE_PLAN: READY}
    )
    monkeypatch.setattr(cli, "run_weekly_workflow", lambda *args, **kwargs: result)
    started: list[Any] = []
    transport = _Transport()

    code = cli._run_weekly_command(
        Path("config.json"),
        Path("out"),
        run_date=RUN_DATE,
        scheduled=True,
        report_transport=transport,
        run_last_passed=lambda plan, root: started.append(plan),
    )

    assert code == 0 and started == [READY] and transport.sent == []


# ── The real checkout, end to end ──────────────────────────────────────────


def test_a_failing_merge_starts_again_on_the_last_pass_and_never_moves_the_checkout(tmp_path):
    repo, remote, passed = _passed_checkout(tmp_path)
    _push_from_elsewhere(tmp_path, remote, "autoscout/module.py", "VALUE = 3\n")
    config, output_dir, adapters = _fixture_run(tmp_path)
    log = repo / "runs" / "weekly" / "workflow-log.json"
    adapters.repository = GitRepositoryAdapter(repo, expected_branch="main", run_log_path=log)
    adapters.code_gate = ProductionCodeGate(repo, runner=_Check(code=1), venv=_Venv(dict(MEETS_PINS)))

    result = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)

    assert result.outcome == WorkflowOutcome.RUN_ON_LAST_PASSED_CODE, result.failed_stage
    plan = result.run_summary[LAST_PASSED_CODE_PLAN]
    assert plan["ran_commit"] == passed
    # The production checkout stays on the new commit, level with origin, on main, clean.
    assert _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "origin/main") == plan["failed_commit"]
    assert _git(repo, "symbolic-ref", "--short", "HEAD") == "main"
    assert _git(repo, "status", "--porcelain") == ""
    assert not (repo / GUARD_FILENAME).exists()
    assert (repo / SNAPSHOT_DIR / "autoscout" / "module.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_the_started_again_process_imports_the_snapshot_and_reads_the_checkout(tmp_path):
    """The environment puts the snapshot first, also over the working directory and the editable install."""
    snapshot = tmp_path / "snapshot"
    shutil.copytree(REPO_ROOT / "autoscout", snapshot / "autoscout", ignore=shutil.ignore_patterns("__pycache__"))
    production = tmp_path / "production"
    production.mkdir()
    ready = {**READY, "snapshot": str(snapshot)}
    env = code_gate_fallback.environment(ready, production, os.environ)
    probe = (
        "import json, autoscout\n"
        "from autoscout.state_store import sqlite_location\n"
        "from autoscout import codex_model_pin\n"
        "print(json.dumps([autoscout.__file__, str(sqlite_location.DEFAULT_DATABASE_DIR),"
        " str(codex_model_pin.DEFAULT_PIN_PATH)]))\n"
    )

    proc = subprocess.run(
        [sys.executable, "-c", probe], cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=True
    )

    package, database_dir, pin = json.loads(proc.stdout)
    assert Path(package).resolve().is_relative_to(snapshot.resolve())
    assert Path(database_dir) == production.resolve() / "data" / "state-store"
    assert Path(pin) == production.resolve() / "configs" / "codex-model-pin.json"


def test_no_module_finds_the_checkout_from_its_own_location_but_through_the_override():
    """A module that read configs, data or state next to its own file would read the snapshot's (#1159)."""
    own_location = re.compile(r"__file__\)\.resolve\(\)\.(?:parents\[\d\]|parent\.parent)")
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in sorted((REPO_ROOT / "autoscout").rglob("*.py"))
        if path.name != "repository_root.py" and own_location.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


def test_the_check_never_inherits_the_snapshot_search_path(tmp_path, monkeypatch):
    repo, _ = _checkout(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "snapshot"))
    monkeypatch.setenv("PYTHONSAFEPATH", "1")
    check = _Check()

    code_gate.ensure_passed(repo, runner=check)

    assert "PYTHONPATH" not in check.calls[0]["env"] and "PYTHONSAFEPATH" not in check.calls[0]["env"]


def test_the_snapshot_folder_is_ignored_by_this_repository():
    proc = subprocess.run(
        ["git", "check-ignore", "-q", f"{SNAPSHOT_DIR.as_posix()}/autoscout/__init__.py"], cwd=REPO_ROOT, check=False
    )
    assert proc.returncode == 0 and SNAPSHOT_DIR.parent == GATE_DIR


# ── The morning report and the doctor ──────────────────────────────────────


def _ran_on_earlier_code(outcome: WorkflowOutcome = WorkflowOutcome.COMPLETED_NO_ATTENTION) -> WeeklyWorkflowResult:
    return WeeklyWorkflowResult(
        outcome=outcome,
        delta_counts={"new": 2, "upgraded": 0, "changed": 0},
        log=[
            {
                "stage": CODE_GATE_STAGE,
                "status": STATUS_RAN_LAST_PASSED,
                "detail": {
                    "status": STATUS_RAN_LAST_PASSED,
                    "commit": "0123456789ab",
                    "ran_commit": "abcdef012345",
                    "reason": REASON_CHECK_FAILED,
                },
            }
        ],
    )


EARLIER_CODE_LINE = (
    "Поиск шёл на предыдущей версии кода, прошедшей проверку на этом сервере: "
    "новая версия не прошла проверку ./scripts/check. Следующий ночной запуск проверит новый код снова."
)


def test_the_morning_report_says_the_night_ran_on_the_earlier_version_without_hashes(monkeypatch):
    code, transport = _run_command(monkeypatch, _ran_on_earlier_code())

    assert code == 0
    subject, body = _the_report(transport)
    assert subject == f"AutoScout: ночной поиск {RUN_DATE} — требует внимания"
    assert body.splitlines()[:2] == [
        f"Ночной поиск {RUN_DATE} завершён, но требует внимания: новый код не прошёл проверку.",
        EARLIER_CODE_LINE,
    ]
    assert not re.search(r"[0-9a-f]{7,}", subject + body)


def test_the_morning_report_of_a_refused_fallback_says_why_the_earlier_version_did_not_run():
    result = WeeklyWorkflowResult(
        outcome=WorkflowOutcome.INTERVENTION_REQUIRED,
        failed_stage=CODE_GATE_STAGE,
        log=[
            {
                "stage": CODE_GATE_STAGE,
                "status": "failed",
                "reason": REASON_CHECK_FAILED,
                "detail": {"commit": "0123456789ab", "fallback_refused": "new_commit_changes_state_store"},
            }
        ],
    )

    body = run_report(result, run_date=RUN_DATE, kirkus_attempt=None).body

    assert (
        "Предыдущая версия кода, прошедшая проверку, тоже не запускалась: новая версия меняет код хранилища "
        "состояния, и совместимость данных не доказана." in body
    )


def test_the_doctor_says_the_last_night_ran_on_the_earlier_version(tmp_path):
    from autoscout import doctor

    output_dir = tmp_path / "run"
    output_dir.mkdir()
    (output_dir / "workflow-log.json").write_text(json.dumps(_ran_on_earlier_code().log), encoding="utf-8")
    report = doctor.DoctorReport(diagnosis=doctor.DiagnosisClass.HEALTHY, verdict="AutoScout в порядке.")
    report.facts["code_gate"] = doctor._ran_on_earlier_code(output_dir)

    assert report.facts["code_gate"] == {"status": STATUS_RAN_LAST_PASSED, "reason": REASON_CHECK_FAILED}
    assert doctor.render_russian(report).splitlines()[:2] == ["AutoScout в порядке.", EARLIER_CODE_LINE]


def test_the_doctor_says_nothing_of_it_after_a_night_on_checked_code(tmp_path):
    from autoscout import doctor

    output_dir = tmp_path / "run"
    output_dir.mkdir()
    log = [{"stage": CODE_GATE_STAGE, "status": "passed", "detail": {"status": "passed"}}]
    (output_dir / "workflow-log.json").write_text(json.dumps(log), encoding="utf-8")

    assert doctor._ran_on_earlier_code(output_dir) is None
