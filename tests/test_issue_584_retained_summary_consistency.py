"""Public seams for retained probe-summary consistency during onboarding."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_issue_338_source_scaffold import _call as scaffold_call
from test_issue_338_source_scaffold import _ready_case
from test_issue_339_source_onboarding import _args
from test_issue_339_source_onboarding import _base_repo
from test_issue_339_source_onboarding import _evidence
from test_issue_339_source_onboarding import _git_snapshot
from test_issue_339_source_onboarding import _init_git
from test_issue_339_source_onboarding import _snapshot_tree
from test_issue_339_source_onboarding import _write_json

from autoscout import replay_fixtures
from autoscout.__main__ import main
from autoscout.source_exit_codes import EXIT_CONFIRMATION
from autoscout.source_exit_codes import EXIT_ERROR
from autoscout.source_feasibility import REASON_RETAINED_SUMMARY_INCONSISTENT
from autoscout.source_scaffold import ScaffoldError


def _stale_evidence(field: str, value: Any) -> dict[str, Any]:
    evidence = _evidence()
    evidence[field] = value
    return evidence


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("bounded_entry_count", 2),
        ("samples", [{"title": "Stale title", "author": "Jane Example", "url": "https://example.test/old"}]),
        ("detected_heading", "Stale heading"),
        ("outcome", "reject"),
    ],
)
def test_preview_rejects_stale_retained_summary_without_mutation(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, field: str, value: Any
) -> None:
    paths = _base_repo(tmp_path)
    _write_json(paths["evidence"], _stale_evidence(field, value))
    _init_git(paths["repo"])
    before_tree = _snapshot_tree(paths["repo"])
    before_git = _git_snapshot(paths["repo"])

    assert main(_args(paths)) == EXIT_ERROR
    report = json.loads(capsys.readouterr().out)

    assert report["stage"] == "probe"
    assert report["reason_code"] == REASON_RETAINED_SUMMARY_INCONSISTENT
    assert "confirmation_required" not in report
    assert "confirmation_token" not in report
    assert _snapshot_tree(paths["repo"]) == before_tree
    assert _git_snapshot(paths["repo"]) == before_git
    assert not paths["proposal"].exists()


def test_consistent_retained_summary_reaches_confirmation_without_mutation(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    paths = _base_repo(tmp_path)
    _init_git(paths["repo"])
    before_tree = _snapshot_tree(paths["repo"])
    before_git = _git_snapshot(paths["repo"])

    assert main(_args(paths)) == EXIT_CONFIRMATION
    report = json.loads(capsys.readouterr().out)

    assert report["confirmation_required"] is True
    assert _snapshot_tree(paths["repo"]) == before_tree
    assert _git_snapshot(paths["repo"]) == before_git


def test_replay_fixture_validation_rejects_stale_retained_summary(tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)
    evidence = _stale_evidence("bounded_entry_count", 2)

    with pytest.raises(replay_fixtures.ReplayFixtureError) as error:
        replay_fixtures.build_replay_fixture_pair(
            "https://example.test/issue-339-list",
            "issue-584-summary",
            evidence,
            source_name="Issue 339 Example List",
            signal_family="award",
            signal_strength="shortlist",
            candidate_category="adult nonfiction",
            language="en",
            helper_metadata_file=paths["helper"],
        )

    assert error.value.reason_code == REASON_RETAINED_SUMMARY_INCONSISTENT
    assert str(error.value) == "probe evidence entry count is stale or inconsistent with its HTML"


def test_scaffold_reuses_retained_summary_validation_without_config_mutation(tmp_path: Path) -> None:
    paths = _ready_case(tmp_path)
    evidence = json.loads(paths["evidence_path"].read_text(encoding="utf-8"))
    evidence["samples"] = [{"title": "Stale title", "author": "Mara Ellison", "url": "https://example.test/stale"}]
    _write_json(paths["evidence_path"], evidence)
    before = {key: path.read_bytes() for key, path in paths.items() if key.endswith("_config")}

    with pytest.raises(ScaffoldError, match="samples are stale or inconsistent"):
        scaffold_call(paths)

    assert {key: path.read_bytes() for key, path in paths.items() if key.endswith("_config")} == before
