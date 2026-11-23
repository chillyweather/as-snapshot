"""Issue #404 - the confirmed onboarding validation stage must be runnable.

Three regressions are covered:

* the staged repository copy carries its own throwaway Git context, so
  repository tests that assert Git facts pass inside the stage;
* a newly onboarded market source does not break the #302 market-expansion
  criterion, because that criterion is derived rather than frozen; and
* a failing validation adapter keeps the validation stages that already
  passed in the structured report.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from autoscout.source_onboarding_transaction import GIT_LOCATION_ENVIRONMENT_VARIABLES
from autoscout.source_onboarding_transaction import canonical_git_environment
from autoscout.source_onboarding_transaction import stage_copy
from autoscout.source_onboarding_transaction import stage_git_environment
from autoscout.source_onboarding_transaction import stage_process_environment
from autoscout.source_onboarding_transaction import staged_git
from tests.test_issue_302_multilingual_portfolio import BASELINE_MARKET_EXPANSION_SOURCES
from tests.test_issue_302_multilingual_portfolio import US_MARKET_KEYS
from tests.test_issue_302_multilingual_portfolio import _market_expansion_source_names
from tests.test_issue_302_multilingual_portfolio import _market_key
from tests.test_issue_302_multilingual_portfolio import _resolve_canonical_sources

ROOT = Path(__file__).resolve().parents[1]

# The two repository tests that need a real Git context. Before #404 they were
# the reason no confirmed onboarding operation could reach persistence.
GIT_DEPENDENT_NODE_IDS = (
    "tests/test_issue_341_monthly_source_discovery.py::test_default_monthly_output_is_ignored_by_git",
    "tests/test_issue_399_source_onboarding_rereview.py::"
    "test_real_add_source_probe_help_delegates_without_files_or_network",
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, check=False, capture_output=True, text=True)


def test_stage_copy_gives_the_stage_a_clean_throwaway_git_context(tmp_path: Path) -> None:
    source = tmp_path / "repo"
    (source / "data").mkdir(parents=True)
    (source / ".gitignore").write_text("/data/derived/\n", encoding="utf-8")
    (source / "data" / "kept.json").write_text("{}\n", encoding="utf-8")

    stage = stage_copy(source)
    try:
        assert (stage / ".git").is_dir()
        inside = _git(stage, "rev-parse", "--is-inside-work-tree")
        assert inside.returncode == 0
        assert inside.stdout.strip() == "true"
        # One commit makes the staged working tree clean, exactly like the
        # canonical repository when the operator runs the command.
        status = _git(stage, "status", "--porcelain=v1", "--untracked-files=all")
        assert status.returncode == 0
        assert status.stdout == ""
        # The copied .gitignore is effective, so derived-output checks work.
        assert _git(stage, "check-ignore", "--no-index", "data/derived/report.json").returncode == 0
        assert _git(stage, "check-ignore", "--no-index", "data/kept.json").returncode == 1
        # The throwaway repository is local only.
        assert _git(stage, "remote").stdout.strip() == ""
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def test_stage_copy_never_copies_the_canonical_git_directory(tmp_path: Path) -> None:
    source = tmp_path / "repo"
    source.mkdir()
    (source / "marker.txt").write_text("kept\n", encoding="utf-8")
    assert _git(source, "init", "-q").returncode == 0
    (source / ".git" / "CANONICAL-MARKER").write_text("canonical\n", encoding="utf-8")

    stage = stage_copy(source)
    try:
        assert (stage / "marker.txt").read_text(encoding="utf-8") == "kept\n"
        assert not (stage / ".git" / "CANONICAL-MARKER").exists()
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def test_git_dependent_repository_tests_pass_inside_a_staged_copy() -> None:
    """The staged full suite must not fail for a missing Git context (#404)."""
    stage = stage_copy(ROOT)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *GIT_DEPENDENT_NODE_IDS],
            cwd=stage,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def test_staged_git_inherits_no_host_or_user_configuration(tmp_path: Path) -> None:
    """The stage must read no system, global, or user Git configuration (#404)."""
    source = tmp_path / "repo"
    source.mkdir()
    (source / "marker.txt").write_text("kept\n", encoding="utf-8")

    stage = stage_copy(source)
    try:
        # Plain git, with the host environment: the isolation must live in the
        # staged repository itself, not only in the commands this module runs.
        # Per gitcredentials(7) an empty value resets the helper list, so the
        # staged repository's own empty entry must be the last one configured
        # and every inherited helper is discarded.
        origins = _git(stage, "config", "--show-origin", "--get-all", "credential.helper")
        entries = [line for line in origins.stdout.split("\n") if line]
        assert entries, origins.stdout
        last_origin, _, last_value = entries[-1].partition("\t")
        assert last_value == "", origins.stdout
        assert last_origin.endswith(".git/config"), origins.stdout
        assert _git(stage, "config", "--get", "credential.helper").stdout.strip() == ""
        # The identity is the stage's own, never the operator's.
        assert _git(stage, "config", "--get", "user.email").stdout.strip() == "onboarding-stage@autoscout.invalid"
        assert _git(stage, "config", "--get", "core.hooksPath").stdout.strip().endswith(".auto-scout-stage-git-hooks")
        # The staged command path reads no inherited helper at all.
        assert staged_git(stage, "config", "--get-all", "credential.helper").stdout.strip() == ""
        # System and global configuration are switched off, and the resolved
        # home is a stage-only path that is never created.
        environment = stage_git_environment(stage)
        assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
        assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
        assert environment["GIT_CONFIG_SYSTEM"] == os.devnull
        assert environment["GIT_TERMINAL_PROMPT"] == "0"
        assert not Path(environment["HOME"]).exists()
        # No inherited GIT_* variable survives into the staged environment.
        inherited = {name for name in environment if name.startswith("GIT_")}
        assert inherited == {
            "GIT_ASKPASS",
            "GIT_ATTR_NOSYSTEM",
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_NOSYSTEM",
            "GIT_CONFIG_SYSTEM",
            "GIT_TERMINAL_PROMPT",
        }
        # No hook can run: the hooks path is a stage-only path that never exists.
        hooks_path = Path(_git(stage, "config", "--get", "core.hooksPath").stdout.strip())
        assert hooks_path.parent == stage
        assert not hooks_path.exists()
        assert not list((stage / ".git" / "hooks").glob("*")) or all(
            path.suffix == ".sample" for path in (stage / ".git" / "hooks").glob("*")
        )
        # No remote, no worktree registration, no alternate object store.
        assert _git(stage, "remote").stdout.strip() == ""
        assert "worktrees" not in _git(stage, "worktree", "list").stdout
        assert not (stage / ".git" / "objects" / "info" / "alternates").exists()
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def test_staged_git_ignores_hostile_git_environment_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GIT_DIR, GIT_WORK_TREE, and GIT_INDEX_FILE must not reach canonical metadata (#404)."""
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    assert subprocess.run(["git", "init", "-q"], cwd=canonical, check=False).returncode == 0
    canonical_git_dir = canonical / ".git"
    hook = canonical_git_dir / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f"#!/bin/sh\ntouch {tmp_path / 'HOOK-RAN'}\n", encoding="utf-8")
    hook.chmod(0o755)
    hostile_index = canonical_git_dir / "HOSTILE-INDEX"

    source = tmp_path / "repo"
    source.mkdir()
    (source / "marker.txt").write_text("kept\n", encoding="utf-8")

    monkeypatch.setenv("GIT_DIR", str(canonical_git_dir))
    monkeypatch.setenv("GIT_WORK_TREE", str(canonical))
    monkeypatch.setenv("GIT_INDEX_FILE", str(hostile_index))

    stage = stage_copy(source)
    try:
        # The stage built its own repository, not the canonical one.
        assert (stage / ".git").is_dir()
        resolved_git_dir = Path(staged_git(stage, "rev-parse", "--absolute-git-dir").stdout.strip())
        assert resolved_git_dir == (stage / ".git").resolve()
        assert staged_git(stage, "log", "--oneline").returncode == 0
        # A staged validation subprocess resolves the stage too, so a staged
        # test that shells out to git cannot reach canonical metadata.
        staged_run = subprocess.run(
            ["git", "rev-parse", "--absolute-git-dir"],
            cwd=stage,
            check=False,
            capture_output=True,
            text=True,
            env=stage_process_environment(),
        )
        assert Path(staged_run.stdout.strip()) == (stage / ".git").resolve()
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    # Nothing reached the canonical repository.
    assert not (tmp_path / "HOOK-RAN").exists()
    assert not hostile_index.exists()
    assert subprocess.run(["git", "log"], cwd=canonical, check=False, capture_output=True).returncode != 0


@pytest.mark.parametrize("variable", sorted(GIT_LOCATION_ENVIRONMENT_VARIABLES))
def test_canonical_git_environment_drops_repository_relocating_variables(
    variable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(variable, "/hostile/path")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    environment = canonical_git_environment()
    assert variable not in environment
    assert "GIT_CONFIG_COUNT" not in environment
    # The operator's own identity configuration is deliberately preserved, so
    # a canonical commit still has a real author.
    assert "PATH" in environment


def test_market_expansion_criterion_accepts_a_newly_onboarded_market_source() -> None:
    """A legitimately onboarded market source needs no manual test edit (#404)."""
    onboarded = {
        "name": "Issue 404 Example Market List",
        "url": "https://issue-404.test/list",
        "active": True,
        "lifecycle_status": "probation",
        "market": "United Kingdom",
    }
    sources = [*_resolve_canonical_sources(), onboarded]
    derived = _market_expansion_source_names(sources)

    assert onboarded["name"] in derived
    # The baseline stays a floor, so a known market source cannot silently
    # disappear, while an addition no longer fails the criterion.
    assert BASELINE_MARKET_EXPANSION_SOURCES <= derived


@pytest.mark.parametrize(
    "market",
    [
        "United States",
        "united states",
        "UNITED STATES",
        "  United States  ",
        "United  States",
        "\tUnited\nStates ",
        "United States of America",
        "united states of america",
        "US",
        "us",
        "U.S.",
        "USA",
        "u.s.a.",
    ],
)
def test_us_market_labels_never_enter_the_market_expansion_cohort(market: str) -> None:
    """No case, spacing, punctuation, or equivalent US label escapes the US cohort (#404)."""
    sources = [
        {
            "name": "Issue 404 US Variant",
            "active": True,
            "lifecycle_status": "probation",
            "market": market,
        }
    ]
    assert _market_key(market) in US_MARKET_KEYS
    assert _market_expansion_source_names(sources) == set()


@pytest.mark.parametrize("market", ["United Kingdom", "  france ", "Czechia", "Canada/United States"])
def test_non_us_market_labels_stay_in_the_market_expansion_cohort(market: str) -> None:
    sources = [
        {
            "name": "Issue 404 Non US Variant",
            "active": True,
            "lifecycle_status": "probation",
            "market": market,
        }
    ]
    assert _market_expansion_source_names(sources) == {"Issue 404 Non US Variant"}


@pytest.mark.parametrize("market", [None, "", "   ", 1, True, ["United Kingdom"]])
def test_absent_or_non_string_market_is_not_a_market_expansion_source(market: Any) -> None:
    sources = [
        {
            "name": "Issue 404 Unmarked",
            "active": True,
            "lifecycle_status": "probation",
            "market": market,
        }
    ]
    assert _market_expansion_source_names(sources) == set()


def test_market_expansion_criterion_still_catches_a_removed_baseline_source() -> None:
    sources = [
        source for source in _resolve_canonical_sources() if source.get("name") not in BASELINE_MARKET_EXPANSION_SOURCES
    ]
    derived = _market_expansion_source_names(sources)
    assert not BASELINE_MARKET_EXPANSION_SOURCES <= derived


def _runner(status: str, reason: str = "") -> Any:
    def run(*_arguments: Any) -> Mapping[str, Any]:
        result: dict[str, Any] = {"status": status, "returncode": 0 if status == "passed" else 1}
        if reason:
            result["reason"] = reason
        return result

    return run


def test_failed_validation_keeps_the_validation_stages_that_passed(tmp_path: Path) -> None:
    """A late validation failure must not hide the earlier passed stages (#404)."""
    from autoscout import source_onboarding_flow as flow

    stages: list[dict[str, Any]] = []
    runners = flow.OnboardingRunners(
        replay=_runner("passed"),
        contracts=_runner("passed"),
        full_suite=_runner("failed", "full suite failed"),
    )

    prepared = SimpleNamespace(input_paths={})
    staged = SimpleNamespace(
        probe_evidence=tmp_path / "probe.json",
        helper_input=tmp_path / "helper.json",
        feasibility_input=tmp_path / "feasibility.md",
        mapping={},
    )

    try:
        flow._run_guarded_validation(
            runners,
            prepared,  # type: ignore[arg-type]
            tmp_path,
            staged,  # type: ignore[arg-type]
            {"name": "Issue 404 Example Market List"},
            {},
            stages,
        )
    except flow.policy.SourceOnboardingError as exc:
        assert exc.stage == "validation"
        assert exc.reason_code == "full_validation_failed"
    else:  # pragma: no cover - the runner is forced to fail
        raise AssertionError("the failing full suite must raise")

    assert [stage["stage"] for stage in stages] == ["replay", "contracts"]
    assert all(stage["outcome"] == "passed" for stage in stages)

    report, code = flow.failure_report(stages, "validation", "full_validation_failed", "full suite failed")
    assert code != 0
    assert report["stage"] == "validation"
    reported = [stage["stage"] for stage in report["stages"]]
    assert reported == ["replay", "contracts", "validation"]
    assert report["stages"][0]["outcome"] == "passed"
    assert report["stages"][1]["outcome"] == "passed"
    assert report["stages"][2]["outcome"] == "failed"
