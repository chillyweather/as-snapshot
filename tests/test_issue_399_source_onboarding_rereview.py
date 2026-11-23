"""Focused regression tests for the Issue #399 onboarding re-review fixes."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from test_issue_339_source_onboarding import _args
from test_issue_339_source_onboarding import _base_repo
from test_issue_339_source_onboarding import _evidence
from test_issue_339_source_onboarding import _init_git
from test_issue_339_source_onboarding import _write_json

from autoscout import source_onboarding_policy as policy
from autoscout.__main__ import main
from autoscout.source_exit_codes import EXIT_CONFIRMATION
from autoscout.source_exit_codes import EXIT_DEFER
from autoscout.source_exit_codes import EXIT_ERROR
from autoscout.source_exit_codes import EXIT_REJECT
from autoscout.source_exit_codes import EXIT_SUCCESS
from autoscout.source_onboarding import OnboardingRunners
from autoscout.source_onboarding_models import ConfigPaths
from autoscout.source_onboarding_models import OnboardingIssue
from autoscout.source_onboarding_transaction import runner_passed

ROOT = Path(__file__).resolve().parents[1]


def test_offline_source_exit_contract_is_separate_from_batch_probe() -> None:
    assert (EXIT_SUCCESS, EXIT_ERROR, EXIT_CONFIRMATION, EXIT_DEFER, EXIT_REJECT) == (0, 1, 3, 4, 5)


def test_runner_status_is_authoritative() -> None:
    assert runner_passed({"status": "passed", "returncode": 1}) is False
    assert runner_passed({"status": "failed", "returncode": 0}) is False
    assert runner_passed({"returncode": 0}) is True
    assert runner_passed({"status": "passed"}) is True
    assert runner_passed({"status": "passed", "returncode": 0}) is True
    assert runner_passed({"status": "unknown", "returncode": 0}) is False
    assert runner_passed({"status": "failed"}) is False
    assert runner_passed({"returncode": 1}) is False


def test_onboarding_issue_and_config_paths_are_frozen_values() -> None:
    issue = OnboardingIssue("#040")
    configs = ConfigPaths(Path("portfolio"), Path("pilot"), Path("pilot-live"), Path("live"))

    assert issue.number == "40"
    assert issue.reference == "#40"
    assert configs.all == (Path("portfolio"), Path("pilot"), Path("pilot-live"), Path("live"))
    with pytest.raises(FrozenInstanceError):
        issue.number = "41"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        configs.portfolio = Path("other")  # type: ignore[misc]


def test_accepted_add_source_requires_explicit_run_date(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)

    assert main(_args(paths, include_run_date=False)) == EXIT_ERROR
    report = __import__("json").loads(capsys.readouterr().out)
    assert report["reason_code"] == "run_date_required"


def test_add_source_url_is_single_use(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["add-source", "--url", "https://one.example", "--url", "https://two.example"]) == EXIT_ERROR
    assert "may only be used once" in capsys.readouterr().err


def test_add_source_launcher_rejects_repeated_url_without_network() -> None:
    result = subprocess.run(
        [
            str(ROOT / "scripts" / "add-source"),
            "--url",
            "https://one.example",
            "--url=https://two.example",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )

    assert result.returncode == EXIT_ERROR
    assert "may only be used once" in result.stderr


def test_real_add_source_probe_help_delegates_without_files_or_network() -> None:
    before = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    result = subprocess.run(
        ["./scripts/add-source", "probe", "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    after = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert result.returncode == 0
    assert "usage: autoscout probe-source-candidate" in result.stdout
    assert "--url" in result.stdout
    assert result.stderr == ""
    assert after == before


def test_add_source_probe_launcher_validates_url_count_before_delegation(tmp_path: Path) -> None:
    launcher_root = tmp_path / "launcher-repo"
    scripts = launcher_root / "scripts"
    scripts.mkdir(parents=True)
    add_source = ROOT / "scripts" / "add-source"
    shutil.copy2(add_source, scripts / "add-source")
    (scripts / "add-source").chmod(0o755)
    marker = tmp_path / "delegated-arguments.txt"
    probe = scripts / "probe-source"
    probe.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > {marker}\n",
        encoding="utf-8",
    )
    probe.chmod(0o755)

    def invoke(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(scripts / "add-source"), "probe", *arguments],
            cwd=launcher_root,
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": os.environ["PATH"]},
        )

    missing = invoke("--name", "missing")
    repeated = invoke("--url", "https://one.example", "--url=https://two.example")
    assert missing.returncode == EXIT_ERROR
    assert repeated.returncode == EXIT_ERROR
    assert not marker.exists()

    delegated_value = invoke("--url", "https://value.example", "--name", "kept")
    assert delegated_value.returncode == 0
    assert marker.read_text(encoding="utf-8").splitlines() == [
        "--url",
        "https://value.example",
        "--name",
        "kept",
    ]

    delegated = invoke("--name", "kept", "--url=https://one.example", "--signal-family", "award")
    assert delegated.returncode == 0
    assert marker.read_text(encoding="utf-8").splitlines() == [
        "--name",
        "kept",
        "--url=https://one.example",
        "--signal-family",
        "award",
    ]


def test_missing_probe_evidence_uses_the_probe_failure_action(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["add-source", "--url", "https://one.example"]) == EXIT_ERROR
    report = __import__("json").loads(capsys.readouterr().out)
    assert report["stage"] == "probe"
    assert report["reason_code"] == "probe_evidence_missing"
    assert report["next_action_code"] == "provide_probe_evidence"


def test_onboarding_commit_excludes_separate_probe_outputs(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _base_repo(tmp_path)
    _init_git(paths["repo"])

    retained = _evidence()
    retained["operator_retained"] = "modified after Git initialization"
    _write_json(paths["evidence"], retained)
    ledger = paths["repo"] / "data" / "source-candidate-ledger.json"
    ledger_md = ledger.with_suffix(".md")
    _write_json(ledger, {"retained": True})
    ledger_md.write_text("retained ledger\n", encoding="utf-8")
    ledger_before = ledger.read_bytes()
    ledger_md_before = ledger_md.read_bytes()

    args = [*_args(paths), "--run-date", "2026-07-18"]
    assert main(args) == EXIT_CONFIRMATION
    preview = __import__("json").loads(capsys.readouterr().out)
    runners = OnboardingRunners(
        replay=lambda *_args, **_kwargs: {"status": "passed"},
        contracts=lambda *_args, **_kwargs: {"status": "passed"},
        full_suite=lambda *_args, **_kwargs: {"status": "passed"},
    )
    monkeypatch.setattr("autoscout.source_onboarding_transaction.default_onboarding_runners", lambda: runners)
    assert main([*args, "--confirmation-token", preview["confirmation_token"], "--confirmed-by-editor"]) == EXIT_SUCCESS
    capsys.readouterr()

    committed = subprocess.run(
        ["git", "show", "--format=", "--name-only", "HEAD"],
        cwd=paths["repo"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    expected = {
        "configs/current-weekly-portfolio.json",
        "configs/current-weekly-pilot.json",
        "configs/current-weekly-pilot-live.json",
        "configs/current-weekly-live.json",
        "runs/current-weekly-pilot-russian/source-proposals.json",
        "data/current-weekly-pilot/issue-339-example-list-helper-metadata.json",
        "docs/issue-401-example-source-feasibility.md",
    }
    fixtures = {
        path for path in committed if path.startswith("data/current-weekly-pilot/") and "helper-metadata" not in path
    }
    assert set(committed) == expected | fixtures
    assert len(fixtures) == 2
    assert str(paths["evidence"].relative_to(paths["repo"])) not in committed
    assert "data/source-candidate-ledger.json" not in committed
    assert "data/source-candidate-ledger.md" not in committed
    assert ledger.read_bytes() == ledger_before
    assert ledger_md.read_bytes() == ledger_md_before
    status = subprocess.run(
        ["git", "status", "--short"], cwd=paths["repo"], check=True, capture_output=True, text=True
    ).stdout
    assert "?? data/source-candidate-ledger.json" in status
    assert "?? data/source-candidate-ledger.md" in status


def test_safe_regular_file_policy_is_explicit_for_missing_and_existing_paths(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    regular = tmp_path / "regular.json"
    regular.write_text("{}\n", encoding="utf-8")
    directory = tmp_path / "directory"
    directory.mkdir()
    symlink = tmp_path / "symlink.json"
    symlink.symlink_to(regular)
    assert policy.is_safe_regular_file(missing) is False
    assert policy.is_safe_regular_file(missing, allow_missing=True) is True
    assert policy.is_safe_regular_file(regular) is True
    assert policy.is_safe_regular_file(directory) is False
    assert policy.is_safe_regular_file(symlink, allow_missing=True) is False

    hardlink = tmp_path / "hardlink.json"
    hardlink.hardlink_to(regular)
    assert policy.is_safe_regular_file(hardlink) is False


def test_safe_regular_file_policy_fails_closed_on_oserror(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fail_lstat(_path: Path) -> object:
        raise OSError("inspection failed")

    # The patch is scoped to the assertion: the tmp_path cleanup that follows
    # the test walks the tree with os.lstat and must see the real one.
    with monkeypatch.context() as patched:
        patched.setattr(policy.os, "lstat", fail_lstat)
        assert policy.is_safe_regular_file(tmp_path / "artifact.json", allow_missing=True) is False


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_canonical_probe_ledger_output_requires_safe_regular_file(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, kind: str
) -> None:
    paths = _base_repo(tmp_path)
    _init_git(paths["repo"])
    ledger = paths["repo"] / "data" / "source-candidate-ledger.json"
    original = tmp_path / "original-ledger.json"
    _write_json(original, {"retained": True})
    if kind == "symlink":
        ledger.symlink_to(original)
    else:
        ledger.hardlink_to(original)

    args = [*_args(paths), "--run-date", "2026-07-18"]
    assert main(args) == EXIT_CONFIRMATION
    preview = __import__("json").loads(capsys.readouterr().out)
    assert main([*args, "--confirmation-token", preview["confirmation_token"], "--confirmed-by-editor"]) == EXIT_ERROR
    report = __import__("json").loads(capsys.readouterr().out)
    assert report["reason_code"] == "probe_output_invalid"


def test_custom_probe_ledger_output_remains_unrelated_dirty(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)
    _init_git(paths["repo"])
    custom = paths["repo"] / "data" / "source-candidate-ledger-custom.json"
    custom_md = custom.with_suffix(".md")
    _write_json(custom, {"retained": True})
    custom_md.write_text("custom retained ledger\n", encoding="utf-8")
    custom_before = custom.read_bytes()
    custom_md_before = custom_md.read_bytes()

    args = [*_args(paths), "--run-date", "2026-07-18"]
    assert main(args) == EXIT_CONFIRMATION
    preview = __import__("json").loads(capsys.readouterr().out)
    assert main([*args, "--confirmation-token", preview["confirmation_token"], "--confirmed-by-editor"]) == EXIT_ERROR
    report = __import__("json").loads(capsys.readouterr().out)
    assert report["reason_code"] == "unrelated_dirty_files"
    assert custom.read_bytes() == custom_before
    assert custom_md.read_bytes() == custom_md_before
