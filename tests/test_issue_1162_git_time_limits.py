"""Time limits of the workflow's Git commands and of the scheduled services (#1162)."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from autoscout import code_gate
from autoscout import description_adjudication
from autoscout import description_stage_limits
from autoscout import editorial_fit_shadow
from autoscout import git_envelope
from autoscout import isbn_completion
from autoscout import original_edition_completion
from autoscout import pm_logline_description
from autoscout import runtime_budget
from autoscout import work_age
from autoscout.git_envelope import GIT_TIMED_OUT
from autoscout.git_envelope import GUARD_FILENAME
from autoscout.git_envelope import GUARD_STALE_SECONDS
from autoscout.git_envelope import GitRepositoryAdapter
from autoscout.git_envelope import GitTimedOut
from autoscout.weekly_workflow import _normalize_adapter_reason

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY = REPO_ROOT / "deploy" / "server"


def _run_git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def _repository(tmp_path: Path, remote_url: str | None = None) -> Path:
    """A checkout on ``main`` whose upstream is a local bare remote, or *remote_url*."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-q", "-b", "main")
    _run_git(repo, "config", "user.email", "test@test.test")
    _run_git(repo, "config", "user.name", "Test")
    (repo / ".gitkeep").write_text("", encoding="utf-8")
    _run_git(repo, "add", ".gitkeep")
    _run_git(repo, "commit", "-q", "-m", "initial")
    if remote_url is None:
        remote = tmp_path / "remote.git"
        _run_git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
        _run_git(repo, "remote", "add", "origin", str(remote))
        _run_git(repo, "push", "-q", "--set-upstream", "origin", "main")
    else:
        _run_git(repo, "remote", "add", "origin", remote_url)
        _run_git(repo, "config", "branch.main.remote", "origin")
        _run_git(repo, "config", "branch.main.merge", "refs/heads/main")
    return repo


def _timing_out_on(monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    """Every ``git <command>`` runs over its limit; every other command runs for real."""
    real_git = git_envelope._git

    def git(repo_path: Path, *args: str) -> Any:
        if args[:1] == (command,):
            raise GitTimedOut(f"git {command} ran over its limit")
        return real_git(repo_path, *args)

    monkeypatch.setattr(git_envelope, "_git", git)


def _gone(pid: int, within: float = 10.0) -> bool:
    """Whether *pid* has exited (a zombie counts) within *within* seconds."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if not Path("/proc/self/stat").exists():
            # No procfs: a zombie counts as alive here, but its new parent reaps it at once.
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
        else:
            try:
                state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            except (FileNotFoundError, ProcessLookupError):
                return True
            if state in {"Z", "X"}:
                return True
        time.sleep(0.05)
    return False


def _wait_for(path: Path, within: float = 10.0) -> None:
    deadline = time.monotonic() + within
    while not (path.exists() and path.read_text().strip()):
        if time.monotonic() > deadline:
            raise AssertionError(f"{path.name} was never written")
        time.sleep(0.05)


# ── A command over its limit ──────────────────────────────────────────────────


def test_a_hanging_fetch_is_stopped_with_its_helpers_and_preflight_reports_git_timed_out(tmp_path, monkeypatch):
    # The transport helper ignores SIGTERM, like a stuck helper might, and
    # starts a child of its own: both must be gone once the call returns.
    marks = tmp_path / "marks"
    marks.mkdir()
    helper = tmp_path / "hanging-ssh"
    helper.write_text(
        "#!/bin/sh\n"
        "trap '' TERM\n"
        f'echo "$GIT_TERMINAL_PROMPT" > "{marks}/prompt"\n'
        "sleep 600 &\n"
        f'echo $! > "{marks}/child"\n'
        f'echo $$ > "{marks}/helper"\n'
        "wait\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    monkeypatch.setenv("GIT_SSH_COMMAND", str(helper))
    monkeypatch.delenv("GIT_TERMINAL_PROMPT", raising=False)
    # Only the network limit is short, so the fetch is held to the network limit.
    monkeypatch.setattr(git_envelope, "GIT_NETWORK_TIMEOUT_SECONDS", 2)
    monkeypatch.setattr(git_envelope, "GIT_LOCAL_TIMEOUT_SECONDS", 120)
    monkeypatch.setattr(git_envelope, "_GIT_TERMINATE_GRACE_SECONDS", 0.5)
    repo = _repository(tmp_path, remote_url="ssh://hang.invalid/repo.git")
    adapter = GitRepositoryAdapter(repo)

    started = time.monotonic()
    try:
        result = adapter.preflight()
    finally:
        adapter.release_guard()

    assert (result["status"], result["reason"]) == ("unsafe", GIT_TIMED_OUT)
    assert _normalize_adapter_reason(result) == GIT_TIMED_OUT
    assert time.monotonic() - started < 60
    for name in ("helper", "child"):
        _wait_for(marks / name)
        assert _gone(int((marks / name).read_text())), f"the {name} process outlived the timed-out git"
    assert (marks / "prompt").read_text().strip() == "0"


def _hanging_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, on_term: str) -> Path:
    """Make ``git fetch`` hang in an ssh transport helper that runs *on_term* on SIGTERM; returns its marks folder."""
    marks = tmp_path / "marks"
    marks.mkdir()
    helper = tmp_path / "hanging-ssh"
    helper.write_text(
        "#!/bin/sh\n"
        f"trap '{on_term}' TERM\n"
        "sleep 600 &\n"
        f'echo $! > "{marks}/child"\n'
        f'echo $$ > "{marks}/helper"\n'
        "wait\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    monkeypatch.setenv("GIT_SSH_COMMAND", str(helper))
    return marks


def test_a_helper_still_cleaning_up_after_git_exited_gets_the_whole_grace(tmp_path, monkeypatch):
    # Git itself exits on SIGTERM at once; its helper needs a second to clean up.
    marks = tmp_path / "marks"
    _hanging_helper(tmp_path, monkeypatch, on_term=f'sleep 1; echo done > "{marks}/cleaned"; exit 0')
    monkeypatch.setattr(git_envelope, "GIT_NETWORK_TIMEOUT_SECONDS", 2)
    monkeypatch.setattr(git_envelope, "_GIT_TERMINATE_GRACE_SECONDS", 5)
    repo = _repository(tmp_path, remote_url="ssh://hang.invalid/repo.git")

    with pytest.raises(GitTimedOut):
        git_envelope._git(repo, "fetch", "origin", "main")

    assert (marks / "cleaned").read_text().strip() == "done"
    assert _gone(int((marks / "helper").read_text()))


def test_an_interrupted_command_is_stopped_before_the_interrupt_goes_on(tmp_path, monkeypatch):
    # Ctrl-C reaches Python only: Git runs in its own session.
    marks = _hanging_helper(tmp_path, monkeypatch, on_term="exit 0")
    monkeypatch.setattr(git_envelope, "_GIT_TERMINATE_GRACE_SECONDS", 0.5)
    repo = _repository(tmp_path, remote_url="ssh://hang.invalid/repo.git")

    def interrupt(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGALRM, interrupt)
    signal.setitimer(signal.ITIMER_REAL, 1.5)
    try:
        with pytest.raises(KeyboardInterrupt):
            git_envelope._git(repo, "fetch", "origin", "main")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)

    for name in ("helper", "child"):
        assert _gone(int((marks / name).read_text()), within=0.5), f"the {name} process outlived the interrupt"


def test_a_terminated_run_stops_its_git_command_and_then_ends_as_sigterm_ends_it(tmp_path):
    # A manual run ended by SIGTERM: Git, in its own session, would not get it.
    marks = tmp_path / "marks"
    marks.mkdir()
    helper = tmp_path / "hanging-ssh"
    helper.write_text(
        f'#!/bin/sh\nsleep 600 &\necho $! > "{marks}/child"\necho $$ > "{marks}/helper"\nwait\n', encoding="utf-8"
    )
    helper.chmod(0o755)
    repo = _repository(tmp_path, remote_url="ssh://hang.invalid/repo.git")
    run = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; from autoscout import git_envelope;"
            "git_envelope._git(Path(sys.argv[1]), 'fetch', 'origin', 'main')",
            str(repo),
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "GIT_SSH_COMMAND": str(helper)},
    )
    try:
        _wait_for(marks / "child")
        run.send_signal(signal.SIGTERM)
        assert run.wait(timeout=30) == -signal.SIGTERM
    finally:
        if run.poll() is None:
            run.kill()
    for name in ("helper", "child"):
        assert _gone(int((marks / name).read_text())), f"the {name} process outlived the terminated run"


def test_a_sigterm_during_the_stopping_of_a_timed_out_command_does_not_cut_it_short(tmp_path):
    # The helper ignores SIGTERM, so only the SIGKILL after the grace ends it;
    # the run's own SIGTERM arrives inside that grace.
    marks = tmp_path / "marks"
    marks.mkdir()
    helper = tmp_path / "hanging-ssh"
    helper.write_text(
        f'#!/bin/sh\ntrap \'\' TERM\nsleep 600 &\necho $! > "{marks}/child"\necho $$ > "{marks}/helper"\nwait\n',
        encoding="utf-8",
    )
    helper.chmod(0o755)
    repo = _repository(tmp_path, remote_url="ssh://hang.invalid/repo.git")
    run = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; from autoscout import git_envelope;"
            "git_envelope.GIT_NETWORK_TIMEOUT_SECONDS = 1; git_envelope._GIT_TERMINATE_GRACE_SECONDS = 3;"
            "git_envelope._git(Path(sys.argv[1]), 'fetch', 'origin', 'main')",
            str(repo),
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "GIT_SSH_COMMAND": str(helper)},
    )
    try:
        _wait_for(marks / "child")
        time.sleep(2)
        run.send_signal(signal.SIGTERM)
        assert run.wait(timeout=30) == -signal.SIGTERM
    finally:
        if run.poll() is None:
            run.kill()
    for name in ("helper", "child"):
        assert _gone(int((marks / name).read_text())), f"the {name} process outlived the terminated run"


class _AuthRequired(BaseHTTPRequestHandler):
    challenges = 0

    def do_GET(self) -> None:
        type(self).challenges += 1
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="test"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        pass


def test_a_missing_credential_fails_at_once_without_any_askpass_program(tmp_path, monkeypatch):
    marks = tmp_path / "marks"
    marks.mkdir()
    askpass = tmp_path / "askpass"
    askpass.write_text(f'#!/bin/sh\necho asked > "{marks}/asked"\nsleep 600\n', encoding="utf-8")
    askpass.chmod(0o755)
    empty_config = tmp_path / "gitconfig"
    empty_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_ASKPASS", str(askpass))
    monkeypatch.setenv("SSH_ASKPASS", str(askpass))
    for proxy in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(proxy, raising=False)
    monkeypatch.setenv("no_proxy", "*")
    _AuthRequired.challenges = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _AuthRequired)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        repo = _repository(tmp_path, remote_url=f"http://127.0.0.1:{server.server_address[1]}/repo.git")
        _run_git(repo, "config", "core.askPass", str(askpass))
        started = time.monotonic()
        result = git_envelope._git(repo, "fetch", "origin", "main")
    finally:
        server.shutdown()
        server.server_close()

    assert _AuthRequired.challenges >= 1, "the fetch never reached the server's credential challenge"
    assert result.returncode != 0
    assert time.monotonic() - started < 30
    assert not (marks / "asked").exists()


def test_update_reports_a_pull_over_its_limit(tmp_path, monkeypatch):
    repo = _repository(tmp_path)
    other = tmp_path / "other"
    _run_git(tmp_path, "clone", "-q", str(tmp_path / "remote.git"), str(other))
    _run_git(other, "-c", "user.email=t@t.t", "-c", "user.name=T", "commit", "-q", "--allow-empty", "-m", "newer")
    _run_git(other, "push", "-q", "origin", "main")
    head = _run_git(repo, "rev-parse", "HEAD")
    _timing_out_on(monkeypatch, "pull")

    result = GitRepositoryAdapter(repo).update()

    assert (result["status"], result["reason"]) == ("unsafe", GIT_TIMED_OUT)
    assert _run_git(repo, "rev-parse", "HEAD") == head


def test_finalize_reports_a_commit_over_its_limit(tmp_path, monkeypatch):
    repo = _repository(tmp_path)
    output = repo / "runs" / "current"
    output.mkdir(parents=True)
    (output / "workflow-log.json").write_text("[]\n", encoding="utf-8")
    _timing_out_on(monkeypatch, "commit")

    result = GitRepositoryAdapter(repo).finalize(output)

    assert (result["status"], result["reason"]) == ("unsafe", GIT_TIMED_OUT)


def test_finalize_reports_a_push_over_its_limit_and_keeps_the_commit(tmp_path, monkeypatch):
    repo = _repository(tmp_path)
    output = repo / "runs" / "current"
    output.mkdir(parents=True)
    (output / "workflow-log.json").write_text("[]\n", encoding="utf-8")
    _timing_out_on(monkeypatch, "push")

    result = GitRepositoryAdapter(repo).finalize(output)

    assert (result["status"], result["reason"]) == ("push_failed", GIT_TIMED_OUT)
    assert result["committed"] is True and result["pushed"] is False
    assert result["commit_hash"] == _run_git(repo, "rev-parse", "HEAD")


def test_local_inspection_reports_a_status_over_its_limit(tmp_path, monkeypatch):
    repo = _repository(tmp_path)
    _timing_out_on(monkeypatch, "status")

    result = GitRepositoryAdapter(repo).inspect_local()

    assert (result["status"], result["reason"]) == ("unsafe", GIT_TIMED_OUT)


# ── A run the service limit killed ────────────────────────────────────────────


def test_a_run_killed_by_the_service_limit_leaves_a_lock_that_is_not_a_live_run(tmp_path):
    repo = _repository(tmp_path)
    # A run holds the lock until systemd's SIGTERM ends it, as at TimeoutStartSec.
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time; from pathlib import Path;"
            "from autoscout.git_envelope import GitRepositoryAdapter;"
            "assert GitRepositoryAdapter(Path(sys.argv[1])).acquire_guard().acquired;"
            "time.sleep(600)",
            str(repo),
        ],
        cwd=REPO_ROOT,
    )
    lock = repo / GUARD_FILENAME
    try:
        _wait_for(lock)
        assert GitRepositoryAdapter(repo).held_by_live_run() is True
    finally:
        holder.send_signal(signal.SIGTERM)
        holder.wait(timeout=30)

    next_night = GitRepositoryAdapter(repo)
    assert lock.read_text().strip() == str(holder.pid)
    assert next_night.held_by_live_run() is False
    # The next night is hours later: the dead holder's lock is taken over.
    old = time.time() - GUARD_STALE_SECONDS - 60
    os.utime(lock, (old, old))
    try:
        assert next_night.acquire_guard().acquired is True
    finally:
        next_night.release_guard()


# ── The service limits ────────────────────────────────────────────────────────


def _timeout_start_seconds(unit: Path) -> float:
    values = re.findall(r"^TimeoutStartSec=(\S+)\s*$", unit.read_text(encoding="utf-8"), flags=re.MULTILINE)
    assert len(values) == 1, f"{unit.name} sets TimeoutStartSec once"
    total = 0.0
    for amount, suffix in re.findall(r"(\d+)(h|min|s)", values[0]):
        total += int(amount) * {"h": 3600, "min": 60, "s": 1}[suffix]
    assert total > 0
    return total


def test_the_run_service_limit_never_cuts_a_run_within_its_budgets_and_ends_before_the_next_night():
    stage_budgets = (
        runtime_budget.WEEKLY_OPERATION_CEILING_SECONDS
        + isbn_completion.STAGE_BUDGET_SECONDS
        + description_stage_limits.GROUNDED_BUDGET_CEILING_SECONDS
        + description_adjudication.ADJUDICATION_STAGE_BUDGET_SECONDS
        + pm_logline_description.STAGE_BUDGET_SECONDS
        + original_edition_completion.STAGE_BUDGET_SECONDS
        + work_age.STAGE_BUDGET_SECONDS
        + editorial_fit_shadow.DEFAULT_STAGE_BUDGET_SECONDS
    )
    # Up to three starts (two restarts), each with the gate's install and
    # check, the preflight's fetch and the update's fetch and pull; then two
    # finalizations, each a fetch and a push. Every one may run up to its limit.
    starts = 3
    code_gate_limits = starts * (code_gate.CHECK_TIMEOUT_SECONDS + code_gate.INSTALL_TIMEOUT_SECONDS)
    git_limits = (starts * 3 + 2 * 2) * git_envelope.GIT_NETWORK_TIMEOUT_SECONDS

    limit = _timeout_start_seconds(DEPLOY / "autoscout-run.service")

    assert limit > stage_budgets + code_gate_limits + git_limits
    assert limit < 20 * 3600


def test_the_kirkus_service_limit_is_above_the_pass_allowance_and_ends_before_the_next_pass():
    limit = _timeout_start_seconds(DEPLOY / "autoscout-kirkus.service")

    assert limit > 2 * runtime_budget.KIRKUS_PASS_RUNTIME_BUDGET_SECONDS
    assert limit < 20 * 3600
