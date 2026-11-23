"""The nightly run executes only code that passed the check on the run server (#1124)."""

from __future__ import annotations

import json
import subprocess
import time
import types
from pathlib import Path
from typing import Any

import pytest

from autoscout import code_gate
from autoscout import weekly_workflow
from autoscout.code_gate import CHECK_LOG_FILE
from autoscout.code_gate import GATE_DIR
from autoscout.code_gate import KIRKUS_SKIPPED_UNCHECKED_LINE
from autoscout.code_gate import REASON_CHECK_FAILED
from autoscout.code_gate import REASON_CHECK_TIMED_OUT
from autoscout.code_gate import RECORD_FILE
from autoscout.code_gate import TMPDIR_ENV
from autoscout.code_gate import UNCHECKED_MANUAL_RUN_LINE
from autoscout.code_gate import ProductionCodeGate
from autoscout.git_envelope import GUARD_FILENAME
from autoscout.git_envelope import LOG_RESIDUE_DIR
from autoscout.git_envelope import GitRepositoryAdapter
from autoscout.operator_notice import run_report
from autoscout.weekly_workflow import CODE_GATE_STAGE
from autoscout.weekly_workflow import WeeklyWorkflowResult
from autoscout.weekly_workflow import WorkflowOutcome
from autoscout.weekly_workflow import run_weekly_workflow
from tests.test_issue_1057_nightly_run import BOOK_TITLE
from tests.test_issue_1057_nightly_run import RUN_DATE
from tests.test_issue_1057_nightly_run import _fixture_run
from tests.test_issue_1057_nightly_run import _kirkus_command
from tests.test_issue_1057_nightly_run import _PullingRepository
from tests.test_issue_1057_nightly_run import _ResidueRepository
from tests.test_issue_1057_nightly_run import _run_command
from tests.test_issue_1057_nightly_run import _the_report

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── A checkout with a remote ───────────────────────────────────────────────


def _git(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(path), capture_output=True, text=True, check=True).stdout.strip()


def _commit(repo: Path, rel: str, text: str, message: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _git(repo, "add", rel)
    _git(repo, "commit", "-m", message)


def _checkout(tmp_path: Path) -> tuple[Path, Path]:
    """A checkout on main tracking a bare remote, with code and a run log under runs/."""
    repo, remote = tmp_path / "repo", tmp_path / "remote.git"
    repo.mkdir()
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@test.test")
    _git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text(f"/{LOG_RESIDUE_DIR.as_posix()}/\n/{GATE_DIR.as_posix()}/\n", encoding="utf-8")
    (repo / "autoscout").mkdir()
    (repo / "autoscout" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    log = repo / "runs" / "weekly" / "workflow-log.json"
    log.parent.mkdir(parents=True)
    log.write_text('[{"stage": "terminal", "status": "completed/no-attention"}]\n', encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "--set-upstream", "origin", "main")
    return repo, remote


def _push_from_elsewhere(tmp_path: Path, remote: Path, rel: str, text: str) -> None:
    """Another machine pushes one commit to main: a merge, or the server's own state commit."""
    other = tmp_path / f"other-{len(list(tmp_path.iterdir()))}"
    _git(tmp_path, "clone", "-q", str(remote), str(other))
    _git(other, "config", "user.email", "test@test.test")
    _git(other, "config", "user.name", "Test")
    _commit(other, rel, text, f"change {rel}")
    _git(other, "push", "-q", "origin", "main")


class _Check:
    """A stand-in for ./scripts/check: records each call and exits with *code*."""

    def __init__(self, code: int = 0, *, timeout: bool = False) -> None:
        self.code = code
        self.timeout = timeout
        self.calls: list[dict[str, Any]] = []

    def __call__(self, args, **kwargs):
        tmpdir = Path(kwargs["env"]["TMPDIR"])
        self.calls.append({"args": args, "env": kwargs["env"], "tmpdir_existed": tmpdir.is_dir()})
        (tmpdir / "autoscout-bundle-0.lock").write_text("", encoding="utf-8")
        if self.timeout:
            raise subprocess.TimeoutExpired(args, kwargs["timeout"], output="[check] ok    Ruff format\n")
        out = "[check] PASS\n" if self.code == 0 else "[check] FAIL  Pytest suite\n"
        return subprocess.CompletedProcess(args, self.code, stdout=out)


@pytest.fixture(autouse=True)
def _gate_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv(TMPDIR_ENV, str(tmp_path / "gate-tmp"))
    monkeypatch.delenv(weekly_workflow.RESTARTED_ENV, raising=False)


# ── What counts as new code ────────────────────────────────────────────────


def test_a_commit_only_under_runs_keeps_the_code_version_and_any_other_commit_changes_it(tmp_path):
    repo, _ = _checkout(tmp_path)
    first = code_gate.code_version(repo)
    _commit(repo, "runs/weekly/workflow-log.json", "[]\n", "auto-scout: weekly workflow state update")
    after_state = code_gate.code_version(repo)
    _commit(repo, "configs/x.json", "{}\n", "a config change")
    after_code = code_gate.code_version(repo)

    assert first and after_state and after_code
    assert after_state.commit != first.commit
    assert after_state.fingerprint == first.fingerprint
    assert after_code.fingerprint != first.fingerprint


def test_the_record_folder_is_ignored_by_this_repository():
    proc = subprocess.run(
        ["git", "check-ignore", "-q", f"{GATE_DIR.as_posix()}/{RECORD_FILE}"], cwd=REPO_ROOT, check=False
    )
    assert proc.returncode == 0


# ── The gate ───────────────────────────────────────────────────────────────


def test_a_version_is_checked_once_and_a_pass_is_recorded(tmp_path):
    repo, _ = _checkout(tmp_path)
    check = _Check()

    first = code_gate.ensure_passed(repo, runner=check)
    _commit(repo, "runs/weekly/workflow-log.json", "[]\n", "auto-scout: weekly workflow state update")
    second = code_gate.ensure_passed(repo, runner=check)

    assert first["status"] == second["status"] == "passed"
    assert (first["checked"], second["checked"]) == (True, False)
    assert len(check.calls) == 1
    record = json.loads((repo / GATE_DIR / RECORD_FILE).read_text(encoding="utf-8"))
    assert record["commit"] == first["commit"]
    assert check.calls[0]["args"] == [str(repo / "scripts" / "check")]


def test_a_failed_check_records_nothing_keeps_its_output_and_is_checked_again(tmp_path):
    repo, _ = _checkout(tmp_path)
    failing = _Check(code=1)

    first = code_gate.ensure_passed(repo, runner=failing)
    second = code_gate.ensure_passed(repo, runner=failing)

    assert first == {"status": "failed", "reason": REASON_CHECK_FAILED, "commit": _git(repo, "rev-parse", "HEAD")}
    assert second["status"] == "failed" and len(failing.calls) == 2
    assert not (repo / GATE_DIR / RECORD_FILE).exists()
    assert "FAIL  Pytest suite" in (repo / GATE_DIR / CHECK_LOG_FILE).read_text(encoding="utf-8")
    assert code_gate.ensure_passed(repo, runner=_Check())["status"] == "passed"


def test_a_server_venv_behind_the_dev_pins_has_its_own_reason_and_fix(tmp_path):
    repo, _ = _checkout(tmp_path)

    def stale(args, **kwargs):
        out = "auto-scout: this checkout's .venv does not match the dev pins in pyproject.toml\n"
        return subprocess.CompletedProcess(args, 1, stdout=out)

    verdict = code_gate.ensure_passed(repo, runner=stale)
    result = _gate_failure()
    result.log[-1]["reason"] = verdict["reason"]
    body = run_report(result, run_date=RUN_DATE, kirkus_attempt=None).body

    assert verdict["reason"] == code_gate.REASON_ENVIRONMENT_OUT_OF_DATE
    assert 'pip install -e ".[dev]"' in body and "./scripts/code-gate --run" in body


def test_a_checkout_with_uncommitted_changes_is_never_recorded_as_passed(tmp_path):
    repo, _ = _checkout(tmp_path)
    (repo / "autoscout" / "module.py").write_text("VALUE = 99\n", encoding="utf-8")
    check = _Check()

    verdict = code_gate.run_check(repo, runner=check)

    assert verdict["reason"] == code_gate.REASON_CHECKOUT_CHANGED
    assert check.calls == [] and not code_gate.is_passed(repo)


def test_a_check_that_times_out_fails_the_gate(tmp_path):
    repo, _ = _checkout(tmp_path)

    verdict = code_gate.ensure_passed(repo, runner=_Check(timeout=True))

    assert verdict["status"] == "failed" and verdict["reason"] == REASON_CHECK_TIMED_OUT
    assert "timed out" in (repo / GATE_DIR / CHECK_LOG_FILE).read_text(encoding="utf-8")


def test_the_check_gets_its_own_temporary_folder_and_none_of_the_service_variables(tmp_path, monkeypatch):
    repo, _ = _checkout(tmp_path)
    monkeypatch.setenv("AUTOSCOUT_NOTIFY_SMTP_PASSWORD", "secret")
    monkeypatch.setenv("AUTOSCOUT_GROUNDED_DISCOVERY_ENDPOINT", "http://127.0.0.1:8899/search?q={query}")
    check = _Check()

    code_gate.ensure_passed(repo, runner=check)

    env = check.calls[0]["env"]
    assert not [key for key in env if key.startswith("AUTOSCOUT_")]
    tmpdir = Path(env["TMPDIR"])
    assert check.calls[0]["tmpdir_existed"]
    assert tmpdir.parent == tmp_path / "gate-tmp"
    # The folder and every lock file the check left in it are gone (#1119).
    assert not tmpdir.exists()


# ── The scheduled run ──────────────────────────────────────────────────────


class _Gate:
    """A code gate whose verdict is fixed; it records the order of calls."""

    def __init__(self, verdict: dict[str, Any], status: str = "passed", calls: list[str] | None = None) -> None:
        self.verdict = verdict
        self._status = status
        self.calls = calls if calls is not None else []
        self.announced: list[str] = []

    def status(self) -> dict[str, Any]:
        self.calls.append("status")
        return {"status": self._status, "commit": "a" * 40}

    def ensure_passed(self) -> dict[str, Any]:
        self.calls.append("gate")
        return self.verdict

    def announce(self, line: str) -> None:
        self.announced.append(line)


FAILED = {"status": "failed", "reason": REASON_CHECK_FAILED, "commit": "0123456789abcdef" * 2 + "01234567"}


class _OrderedRepository(_PullingRepository):
    def __init__(self, calls: list[str], *, pulled: bool) -> None:
        super().__init__()
        self.calls = calls
        self.pulled = pulled

    def update(self, validated_artifact_allowlist=None):
        self.calls.append("update")
        return {"status": "ok", "pulled": self.pulled}

    def release_guard(self):
        self.calls.append("release")
        super().release_guard()


def _snapshot(folder: Path) -> dict[str, bytes]:
    return {str(path.relative_to(folder)): path.read_bytes() for path in sorted(folder.rglob("*")) if path.is_file()}


@pytest.mark.parametrize("pulled", [True, False])
def test_a_failing_gate_stops_the_scheduled_run_before_any_state_and_releases_the_lock(tmp_path, pulled):
    config, output_dir, adapters = _fixture_run(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "candidate-history.json").write_text("{}\n", encoding="utf-8")
    before = _snapshot(state_dir)
    calls: list[str] = []
    adapters.repository = _OrderedRepository(calls, pulled=pulled)
    adapters.code_gate = _Gate(FAILED, calls=calls)

    result = run_weekly_workflow(config, output_dir, state_dir, adapters=adapters, scheduled=True)

    assert result.outcome == WorkflowOutcome.INTERVENTION_REQUIRED
    assert result.failed_stage == CODE_GATE_STAGE
    assert calls == ["update", "gate", "release"]
    assert _snapshot(state_dir) == before
    stages = [entry["stage"] for entry in result.log]
    assert "operation-start" not in stages
    failed = [entry for entry in result.log if entry["stage"] == CODE_GATE_STAGE]
    assert failed[0]["reason"] == REASON_CHECK_FAILED
    assert failed[0]["detail"] == {"commit": FAILED["commit"][:12]}


def test_a_scheduled_run_that_pulled_code_checks_it_before_the_restart(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path)
    calls: list[str] = []
    adapters.repository = _OrderedRepository(calls, pulled=True)
    adapters.code_gate = _Gate({"status": "passed", "commit": "b" * 40, "checked": True}, calls=calls)

    result = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)

    assert result.outcome == WorkflowOutcome.RESTART_ON_UPDATED_CODE
    assert calls == ["update", "gate", "release"]


def test_a_passing_gate_lets_the_scheduled_run_go_on_and_is_logged(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path)
    adapters.repository = _ResidueRepository()
    adapters.code_gate = _Gate({"status": "passed", "commit": "c" * 40, "checked": False})

    result = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)

    assert result.outcome.value.startswith("completed/"), result.failed_stage
    entries = [entry for entry in result.log if entry["stage"] == CODE_GATE_STAGE]
    assert entries[0]["status"] == "passed"
    assert entries[0]["detail"] == {"status": "passed", "commit": "c" * 12, "checked": False, "passed_at": ""}
    stages = [entry["stage"] for entry in result.log]
    assert stages.index(CODE_GATE_STAGE) < stages.index("operation-start")


def test_a_manual_run_on_unchecked_code_is_not_blocked_but_says_so(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path)
    gate = _Gate(FAILED, status="not_checked")
    adapters.code_gate = gate

    result = run_weekly_workflow(config, output_dir, adapters=adapters)

    assert result.outcome.value.startswith("completed/"), result.failed_stage
    assert "gate" not in gate.calls
    assert gate.announced == [UNCHECKED_MANUAL_RUN_LINE]
    entries = [entry for entry in result.log if entry["stage"] == CODE_GATE_STAGE]
    assert entries[0]["status"] == "not_checked"


def test_a_manual_run_on_code_that_passed_says_nothing(tmp_path):
    config, output_dir, adapters = _fixture_run(tmp_path)
    gate = _Gate(FAILED, status="passed")
    adapters.code_gate = gate

    run_weekly_workflow(config, output_dir, adapters=adapters)

    assert gate.announced == []


# ── The real checkout, end to end ──────────────────────────────────────────


def _real_adapters(tmp_path: Path, repo: Path, check: _Check):
    config, output_dir, adapters = _fixture_run(tmp_path)
    log = repo / "runs" / "weekly" / "workflow-log.json"
    adapters.repository = GitRepositoryAdapter(repo, expected_branch="main", run_log_path=log)
    adapters.code_gate = ProductionCodeGate(repo, runner=check)
    return config, output_dir, adapters


def test_the_servers_own_state_commit_does_not_run_the_check_but_a_merge_does(tmp_path):
    repo, remote = _checkout(tmp_path)
    check = _Check()
    code_gate.ensure_passed(repo, runner=check)
    assert len(check.calls) == 1
    config, output_dir, adapters = _real_adapters(tmp_path, repo, check)

    _push_from_elsewhere(tmp_path, remote, "runs/weekly/run-summary.json", "{}\n")
    state_only = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)
    _push_from_elsewhere(tmp_path, remote, "autoscout/module.py", "VALUE = 2\n")
    new_code = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)

    assert state_only.outcome == new_code.outcome == WorkflowOutcome.RESTART_ON_UPDATED_CODE
    assert len(check.calls) == 2


def test_new_code_that_fails_the_check_stops_the_night_on_the_real_checkout(tmp_path):
    repo, remote = _checkout(tmp_path)
    code_gate.ensure_passed(repo, runner=_Check())
    started_on = _git(repo, "rev-parse", "HEAD")
    _push_from_elsewhere(tmp_path, remote, "autoscout/module.py", "VALUE = 3\n")
    failing = _Check(code=1)
    config, output_dir, adapters = _real_adapters(tmp_path, repo, failing)

    result = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)

    assert result.outcome == WorkflowOutcome.INTERVENTION_REQUIRED
    assert result.failed_stage == CODE_GATE_STAGE
    assert len(failing.calls) == 1
    assert not (repo / GUARD_FILENAME).exists()
    # The rejected commit stays checked out, level with origin, and is not recorded.
    assert _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "origin/main")
    assert _git(repo, "rev-parse", "HEAD") != started_on
    assert _git(repo, "status", "--porcelain") == ""
    assert not code_gate.is_passed(repo)

    # The next night checks the rejected code again.
    again = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)
    assert again.failed_stage == CODE_GATE_STAGE and len(failing.calls) == 2


def test_the_first_night_after_deployment_checks_the_code_it_restarted_on(tmp_path, monkeypatch):
    """The old code, which has no gate, pulled this change and restarted on it: no record exists yet."""
    repo, _ = _checkout(tmp_path)
    monkeypatch.setenv(weekly_workflow.RESTARTED_ENV, "1")
    check = _Check()
    config, output_dir, adapters = _real_adapters(tmp_path, repo, check)

    result = run_weekly_workflow(config, output_dir, adapters=adapters, scheduled=True)

    assert len(check.calls) == 1
    stages = [entry["stage"] for entry in result.log]
    assert CODE_GATE_STAGE in stages
    if "operation-start" in stages:
        assert stages.index(CODE_GATE_STAGE) < stages.index("operation-start")
    entry = next(entry for entry in result.log if entry["stage"] == CODE_GATE_STAGE)
    assert entry["status"] == "passed" and entry["detail"]["checked"] is True


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="reads process states from /proc")
def test_a_check_that_times_out_is_killed_with_every_process_it_started(tmp_path):
    pid_file = tmp_path / "child.pid"
    script = f"sleep 60 & echo $! > {pid_file}; wait"

    with pytest.raises(subprocess.TimeoutExpired):
        code_gate._run_in_own_group(
            ["bash", "-c", script], timeout=1, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )

    child = int(pid_file.read_text(encoding="utf-8"))
    assert _stopped_within(child, seconds=10)


def _stopped_within(pid: int, *, seconds: float) -> bool:
    """Whether the sleep with *pid* is gone or a zombie within *seconds*; SIGKILL lands asynchronously."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError, IndexError):
            return True
        # A zombie has an empty command line; another command means the id was reused.
        if state == "Z" or not cmdline.startswith(b"sleep"):
            return True
        time.sleep(0.05)
    return False


# ── The morning report ─────────────────────────────────────────────────────


def _gate_failure() -> WeeklyWorkflowResult:
    return WeeklyWorkflowResult(
        outcome=WorkflowOutcome.INTERVENTION_REQUIRED,
        failed_stage=CODE_GATE_STAGE,
        log=[
            {"stage": "capture", "status": "ok", "detail": BOOK_TITLE},
            {
                "stage": CODE_GATE_STAGE,
                "status": "failed",
                "reason": REASON_CHECK_FAILED,
                "detail": {"commit": "0123456789ab"},
            },
        ],
    )


def test_the_morning_report_of_a_failed_gate_names_the_reason_and_the_commit(monkeypatch, capsys):
    code, transport = _run_command(monkeypatch, _gate_failure())

    assert code == 1
    subject, body = _the_report(transport)
    assert subject == f"AutoScout: ночной поиск {RUN_DATE} — остановлен"
    assert f"Остановлено на этапе {CODE_GATE_STAGE}, причина: {REASON_CHECK_FAILED}." in body
    assert "Новый код (коммит 0123456789ab) не прошёл ./scripts/check на сервере." in body
    assert "данные не изменены" in body


def test_a_commit_that_is_not_a_hex_id_never_reaches_the_report():
    result = _gate_failure()
    result.log[-1]["detail"] = {"commit": BOOK_TITLE}

    body = run_report(result, run_date=RUN_DATE, kirkus_attempt=None).body

    assert BOOK_TITLE not in body and "коммит неизвестен" in body


def test_the_weekly_command_binds_the_real_gate(monkeypatch):
    import autoscout.cli.weekly as cli

    bound: list[Any] = []
    git = GitRepositoryAdapter(REPO_ROOT, expected_branch="main")
    monkeypatch.setattr(
        weekly_workflow, "weekly_adapters", lambda *args, **kwargs: types.SimpleNamespace(repository=git)
    )

    def fake_run(*args, **kwargs):
        bound.append(kwargs["adapters"].code_gate)
        return _gate_failure()

    monkeypatch.setattr(cli, "run_weekly_workflow", fake_run)
    cli._run_weekly_command(Path("config.json"), Path("out"), run_date=RUN_DATE)

    assert isinstance(bound[0], ProductionCodeGate)


def test_a_run_without_the_git_repository_adapter_gets_no_gate():
    adapters = weekly_workflow.deterministic_adapters()

    weekly_workflow.bind_code_gate(adapters, REPO_ROOT, print)

    assert adapters.code_gate is None


# ── The Kirkus pass at 01:30 ───────────────────────────────────────────────


def test_a_scheduled_kirkus_pass_on_unchecked_code_makes_no_request(tmp_path, monkeypatch, capsys):
    from autoscout import kirkus_precollection

    real = kirkus_precollection.collect_kirkus_command
    monkeypatch.setattr(
        kirkus_precollection,
        "collect_kirkus_command",
        lambda config, **kwargs: real(config, **kwargs, code_passed=lambda: False),
    )

    code, calls = _kirkus_command(tmp_path, monkeypatch, "collected")

    assert code == 0 and calls == []
    assert capsys.readouterr().out.strip() == KIRKUS_SKIPPED_UNCHECKED_LINE


def test_a_manual_kirkus_pass_is_not_gated(tmp_path, monkeypatch):
    from autoscout import kirkus_precollection

    real = kirkus_precollection.collect_kirkus_command
    monkeypatch.setattr(
        kirkus_precollection,
        "collect_kirkus_command",
        lambda config, **kwargs: real(config, **kwargs, code_passed=lambda: False),
    )

    code, calls = _kirkus_command(tmp_path, monkeypatch, "collected", scheduled=False)

    assert code == 0 and len(calls) == 1


def _kirkus_handler(tmp_path, monkeypatch, *, passed: bool):
    import argparse

    from autoscout import kirkus_precollection
    from autoscout.cli.collection import _handle_collect_kirkus
    from tests.test_kirkus_listing import _pass_config

    repo, _ = _checkout(tmp_path)
    if passed:
        code_gate.ensure_passed(repo, runner=_Check())
    config = _pass_config(repo)
    calls = []
    monkeypatch.setattr(kirkus_precollection, "_collect_pass", lambda *args: calls.append(1) or {"status": "collected"})
    monkeypatch.chdir(tmp_path)
    code = _handle_collect_kirkus(argparse.Namespace(config=config, scheduled=True))
    return code, calls


def test_the_kirkus_command_reads_the_record_of_the_checkout_holding_its_config(tmp_path, monkeypatch, capsys):
    code, calls = _kirkus_handler(tmp_path, monkeypatch, passed=False)

    assert code == 0 and calls == []
    assert KIRKUS_SKIPPED_UNCHECKED_LINE in capsys.readouterr().out


def test_the_kirkus_command_runs_on_code_that_passed(tmp_path, monkeypatch):
    code, calls = _kirkus_handler(tmp_path, monkeypatch, passed=True)

    assert calls == [1]


# ── ./scripts/code-gate ────────────────────────────────────────────────────


def test_the_command_says_whether_the_checkout_passed(tmp_path, capsys):
    repo, _ = _checkout(tmp_path)

    assert code_gate.code_gate_command(repo, run=False) == 1
    assert "has not passed" in capsys.readouterr().out
    code_gate.ensure_passed(repo, runner=_Check())
    assert code_gate.code_gate_command(repo, run=False) == 0
    assert "has passed here" in capsys.readouterr().out


def test_every_registered_command_is_offered_by_the_cli_and_the_launcher_reaches_the_gate(tmp_path):
    from autoscout.__main__ import _COMMAND_ORDER
    from autoscout.__main__ import _commands

    assert sorted(_COMMAND_ORDER) == sorted(_commands())
    assert len(_COMMAND_ORDER) == len(set(_COMMAND_ORDER))
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [str(repo_root / "scripts" / "code-gate"), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--run" in result.stdout
