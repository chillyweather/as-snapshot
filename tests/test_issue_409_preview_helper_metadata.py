"""Issue #409 - representative helper metadata is validated during preview.

The offline ``add-source`` preview must never ask the editor to confirm helper
metadata that the confirmed replay-fixture contract has to reject. Preview and
confirmed execution use exactly the same deterministic representative
selection, and the bounded representative set is capped at
``MAX_REPLAY_ENTRIES``.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_issue_339_source_onboarding import HEADING
from test_issue_339_source_onboarding import NAME
from test_issue_339_source_onboarding import URL
from test_issue_339_source_onboarding import _args
from test_issue_339_source_onboarding import _base_repo
from test_issue_339_source_onboarding import _git_snapshot
from test_issue_339_source_onboarding import _init_git
from test_issue_339_source_onboarding import _snapshot_tree
from test_issue_339_source_onboarding import _write_json

from autoscout import replay_fixtures
from autoscout.__main__ import main
from autoscout.source_exit_codes import EXIT_CONFIRMATION
from autoscout.source_exit_codes import EXIT_ERROR
from autoscout.source_exit_codes import EXIT_SUCCESS
from autoscout.source_feasibility import FeasibilityVerdict
from autoscout.source_feasibility import evaluate_captured_source_evidence
from autoscout.source_onboarding import OnboardingRunners

ROOT = Path(__file__).resolve().parents[1]

# One bounded exact list with five entries. The deterministic representative
# selection keeps only the first three, in document order.
BOUNDED_ENTRIES: tuple[tuple[str, str, str], ...] = (
    ("The Quiet Ledger", "Jane Author", f"{URL}/one"),
    ("Northern Currents", "John Writer", f"{URL}/two"),
    ("The Long Harbour", "Ann Editor", f"{URL}/three"),
    ("Paper Observatories", "Bob Proofreader", f"{URL}/four"),
    ("Salt and Signal", "Eve Reviewer", f"{URL}/five"),
)
SELECTED_ENTRIES = BOUNDED_ENTRIES[: replay_fixtures.MAX_REPLAY_ENTRIES]


def _five_entry_evidence() -> dict[str, Any]:
    items = "".join(
        f'<li><a href="{url}">{title}</a><span class="author"> {author} </span></li>'
        for title, author, url in BOUNDED_ENTRIES
    )
    return {
        "collection_mode": "source-candidate-probe",
        "source_url": URL,
        "source_page": URL,
        "source_name": NAME,
        "name": NAME,
        "signal_family": "award",
        "resolved_url": URL,
        "head_status": 200,
        "get_status": 200,
        "html": f"<!DOCTYPE html><html><body><main><h1>{HEADING}</h1><ul>{items}</ul></main></body></html>",
        "detected_heading": HEADING,
        "bounded_entry_count": len(BOUNDED_ENTRIES),
    }


def _helper_metadata(entries: tuple[tuple[str, str, str], ...]) -> dict[str, Any]:
    return {
        "version": 1,
        "source_url": URL,
        "language": "en",
        "entries": [
            {
                "title": title,
                "author": author,
                "evidence_url": url,
                "english_title": title,
                "search_translation": f"{title} (поиск)",
                "author_transliteration_note": f"{author} (транслитерация)",
            }
            for title, author, url in entries
        ],
    }


def _repo(tmp_path: Path, entries: tuple[tuple[str, str, str], ...] = SELECTED_ENTRIES) -> dict[str, Path]:
    paths = _base_repo(tmp_path)
    _write_json(paths["evidence"], _five_entry_evidence())
    _write_json(paths["helper"], _helper_metadata(entries))
    return paths


def _preview_args(paths: dict[str, Path]) -> list[str]:
    return _args(paths)


def _passing_runners() -> OnboardingRunners:
    return OnboardingRunners(
        replay=lambda *_args, **_kwargs: {"status": "passed"},
        contracts=lambda *_args, **_kwargs: {"status": "passed"},
        full_suite=lambda *_args, **_kwargs: {"status": "passed"},
    )


def _identities(values: Any) -> list[tuple[str, str, str]]:
    return [(item["title"], item["author"], item["evidence_url"]) for item in values]


def _worktree_snapshot(repo: Path) -> dict[Path, tuple[str, bytes | None, bool, int | None]]:
    """Snapshot the working tree only.

    Git's own metadata is asserted separately through ``_git_snapshot`` (HEAD,
    index, and status). Background Git maintenance may create or remove its own
    lock files under ``.git`` at any moment, so including that directory here
    would make the zero-mutation assertion flaky rather than stronger."""
    return {path: value for path, value in _snapshot_tree(repo).items() if not str(path).startswith(".git" + os.sep)}


def test_representative_limit_is_three_and_preview_reports_the_selection(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert replay_fixtures.MAX_REPLAY_ENTRIES == 3
    paths = _repo(tmp_path)

    assert main(_preview_args(paths)) == EXIT_CONFIRMATION
    report = json.loads(capsys.readouterr().out)

    probe_stage = next(stage for stage in report["stages"] if stage["stage"] == "probe")
    assert probe_stage["bounded_entry_count"] == len(BOUNDED_ENTRIES)
    assert _identities(report["representative_entries"]) == list(SELECTED_ENTRIES)


def test_valid_helper_sidecar_passes_preview_and_confirmed_execution(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _repo(tmp_path)
    _init_git(paths["repo"])
    preview_args = _preview_args(paths)

    assert main(preview_args) == EXIT_CONFIRMATION
    preview = json.loads(capsys.readouterr().out)
    helper_bytes = paths["helper"].read_bytes()

    monkeypatch.setattr(
        "autoscout.source_onboarding_transaction.default_onboarding_runners", lambda: _passing_runners()
    )
    assert (
        main([*preview_args, "--confirmation-token", preview["confirmation_token"], "--confirmed-by-editor"])
        == EXIT_SUCCESS
    )
    report = json.loads(capsys.readouterr().out)
    assert report["outcome"] == "completed"

    published_dir = paths["repo"] / "data" / "current-weekly-pilot"
    extraction_files = list(published_dir.glob("*extraction*.json"))
    assert len(extraction_files) == 1
    extraction = json.loads(extraction_files[0].read_text(encoding="utf-8"))
    published = [
        (mention["title"], mention["primary_authors"][0], mention["evidence_url"]) for mention in extraction["mentions"]
    ]
    # The confirmed operation publishes exactly the identities the preview
    # reported, in the same deterministic order.
    assert published == list(SELECTED_ENTRIES)
    assert published == _identities(preview["representative_entries"])

    sidecars = list(published_dir.glob("*-helper-metadata.json"))
    assert len(sidecars) == 1
    assert sidecars[0].read_bytes() == helper_bytes


def test_denied_network_confirmed_operation_reaches_one_nine_path_commit(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The #333 acceptance operation still commits exactly nine paths (#409)."""
    paths = _repo(tmp_path)
    _init_git(paths["repo"])
    preview_args = _preview_args(paths)

    assert main(preview_args) == EXIT_CONFIRMATION
    preview = json.loads(capsys.readouterr().out)

    def _denied_network(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the confirmed onboarding operation must make no network or model call")

    monkeypatch.setattr(socket, "socket", _denied_network)
    monkeypatch.setattr(socket, "create_connection", _denied_network)
    monkeypatch.setattr(socket, "getaddrinfo", _denied_network)
    monkeypatch.setattr(
        "autoscout.source_onboarding_transaction.default_onboarding_runners", lambda: _passing_runners()
    )

    assert (
        main([*preview_args, "--confirmation-token", preview["confirmation_token"], "--confirmed-by-editor"])
        == EXIT_SUCCESS
    )
    capsys.readouterr()

    committed = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
        cwd=paths["repo"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert sorted(committed) == sorted(
        [
            "configs/current-weekly-live.json",
            "configs/current-weekly-pilot-live.json",
            "configs/current-weekly-pilot.json",
            "configs/current-weekly-portfolio.json",
            "data/current-weekly-pilot/codex-capture-issue-339-example-list-917b00ac.json",
            "data/current-weekly-pilot/issue-339-example-list-917b00ac-codex-extraction.json",
            "data/current-weekly-pilot/issue-339-example-list-helper-metadata.json",
            "docs/issue-401-example-source-feasibility.md",
            "runs/current-weekly-pilot-russian/source-proposals.json",
        ]
    )
    assert len(committed) == 9
    assert (
        subprocess.run(
            ["git", "rev-list", "--count", "HEAD"], cwd=paths["repo"], check=True, capture_output=True, text=True
        ).stdout.strip()
        == "2"
    )


def test_repeated_confirmed_operation_reports_proposal_already_accepted(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _repo(tmp_path)
    _init_git(paths["repo"])
    preview_args = _preview_args(paths)

    assert main(preview_args) == EXIT_CONFIRMATION
    preview = json.loads(capsys.readouterr().out)
    monkeypatch.setattr(
        "autoscout.source_onboarding_transaction.default_onboarding_runners", lambda: _passing_runners()
    )
    confirmed = [*preview_args, "--confirmation-token", preview["confirmation_token"], "--confirmed-by-editor"]
    assert main(confirmed) == EXIT_SUCCESS
    capsys.readouterr()

    assert main(confirmed) == EXIT_ERROR
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "proposal_already_accepted"


def _mutated(kind: str) -> tuple[tuple[str, str, str], ...]:
    selected = list(SELECTED_ENTRIES)
    if kind == "stale":
        title, author, url = selected[0]
        selected[0] = ("A Retired Title", author, url)
        return tuple(selected)
    if kind == "missing":
        return tuple(selected[:-1])
    if kind == "extra":
        return tuple([*selected, BOUNDED_ENTRIES[3]])
    if kind == "every_bounded_entry":
        return BOUNDED_ENTRIES
    if kind == "duplicated":
        return tuple([selected[0], *selected])
    if kind == "reordered":
        return tuple([selected[1], selected[0], selected[2]])
    if kind == "mismatched_author":
        title, _author, url = selected[2]
        selected[2] = (title, "Ivan Substitute", url)
        return tuple(selected)
    if kind == "mismatched_evidence_url":
        title, author, _url = selected[1]
        selected[1] = (title, author, f"{URL}/substitute")
        return tuple(selected)
    raise AssertionError(f"unknown mutation: {kind}")


@pytest.mark.parametrize(
    ("kind", "reason_code"),
    [
        ("stale", "helper_metadata_identity_mismatch"),
        ("missing", "helper_metadata_identity_mismatch"),
        ("extra", "helper_metadata_identity_mismatch"),
        ("every_bounded_entry", "helper_metadata_identity_mismatch"),
        ("duplicated", "helper_metadata_duplicate_entry"),
        ("reordered", "helper_metadata_identity_mismatch"),
        ("mismatched_author", "helper_metadata_identity_mismatch"),
        ("mismatched_evidence_url", "helper_metadata_identity_mismatch"),
    ],
)
def test_disagreeing_helper_sidecar_fails_during_preview_without_mutation(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, kind: str, reason_code: str
) -> None:
    paths = _repo(tmp_path, _mutated(kind))
    _init_git(paths["repo"])
    before_tree = _worktree_snapshot(paths["repo"])
    before_git = _git_snapshot(paths["repo"])

    assert main(_preview_args(paths)) == EXIT_ERROR
    report = json.loads(capsys.readouterr().out)

    assert report["stage"] == "inputs"
    assert report["reason_code"] == reason_code
    assert report["next_action_code"]
    assert report["next_action"].strip()
    assert "confirmation_required" not in report
    assert "confirmation_token" not in report
    assert _worktree_snapshot(paths["repo"]) == before_tree
    assert _git_snapshot(paths["repo"]) == before_git
    assert not paths["proposal"].exists()
    assert not list((paths["repo"] / "data" / "current-weekly-pilot").glob("*"))


@pytest.mark.parametrize(
    ("mutation", "reason_code"),
    [
        ({"version": 2}, "helper_metadata_schema_invalid"),
        ({"source_url": "https://other.test/list"}, "helper_metadata_schema_invalid"),
        ({"language": "de"}, "helper_metadata_schema_invalid"),
        ({"entries": []}, "helper_metadata_schema_invalid"),
        ({"unexpected": "value"}, "helper_metadata_schema_invalid"),
    ],
)
def test_malformed_helper_sidecar_header_fails_during_preview(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, mutation: dict[str, Any], reason_code: str
) -> None:
    paths = _repo(tmp_path)
    metadata = _helper_metadata(SELECTED_ENTRIES)
    metadata.update(mutation)
    _write_json(paths["helper"], metadata)

    assert main(_preview_args(paths)) == EXIT_ERROR
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == reason_code
    assert report["next_action_code"]


def test_placeholder_helper_values_fail_during_preview(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    paths = _repo(tmp_path)
    metadata = _helper_metadata(SELECTED_ENTRIES)
    metadata["entries"][1]["search_translation"] = "TODO"
    _write_json(paths["helper"], metadata)

    assert main(_preview_args(paths)) == EXIT_ERROR
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "helper_metadata_entry_invalid"
    assert report["next_action_code"]


def test_unreadable_helper_sidecar_fails_during_preview(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    paths = _repo(tmp_path)
    paths["helper"].write_text("not json\n", encoding="utf-8")

    assert main(_preview_args(paths)) == EXIT_ERROR
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "helper_metadata_unreadable"
    assert report["next_action_code"]


def test_failing_preview_performs_no_network_model_or_subprocess_call(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _repo(tmp_path, _mutated("every_bounded_entry"))

    def _denied(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the offline preview must make no network, model, or subprocess call")

    monkeypatch.setattr(socket, "socket", _denied)
    monkeypatch.setattr(socket, "create_connection", _denied)
    monkeypatch.setattr(socket, "getaddrinfo", _denied)
    monkeypatch.setattr(subprocess, "run", _denied)
    monkeypatch.setattr(subprocess, "Popen", _denied)

    assert main(_preview_args(paths)) == EXIT_ERROR
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "helper_metadata_identity_mismatch"


def test_preview_and_confirmed_execution_share_one_representative_seam(tmp_path: Path) -> None:
    """The selection and agreement seams are public and shared (#409)."""
    verdict = evaluate_captured_source_evidence(_five_entry_evidence())
    selected = replay_fixtures.select_representative_entries(verdict)

    assert [(sample.title, sample.author, sample.url) for sample in selected] == list(SELECTED_ENTRIES)

    valid = tmp_path / "valid-helper.json"
    _write_json(valid, _helper_metadata(SELECTED_ENTRIES))
    replay_fixtures.validate_helper_sidecar(valid, url=URL, language="en", samples=selected)

    reordered = tmp_path / "reordered-helper.json"
    _write_json(reordered, _helper_metadata(_mutated("reordered")))
    with pytest.raises(replay_fixtures.ReplayFixtureError) as identity_error:
        replay_fixtures.validate_helper_sidecar(reordered, url=URL, language="en", samples=selected)
    assert identity_error.value.reason_code == replay_fixtures.REASON_HELPER_IDENTITY_MISMATCH


def test_helper_agreement_reports_evidence_without_representative_entries(tmp_path: Path) -> None:
    """An accepted verdict with no samples is a named input failure (#409)."""
    from autoscout import source_onboarding_flow as flow

    helper = tmp_path / "helper.json"
    _write_json(helper, _helper_metadata(SELECTED_ENTRIES))
    empty = FeasibilityVerdict(accepted=True, outcome="accept", reason_code="ok", reason="accepted", samples=())

    with pytest.raises(flow.policy.SourceOnboardingError) as error:
        flow.validate_helper_metadata_agreement(helper, source_url=URL, language="en", verdict=empty)

    assert error.value.stage == "inputs"
    assert error.value.reason_code == replay_fixtures.REASON_REPRESENTATIVE_ENTRIES_MISSING
    assert error.value.details == {}
    report, code = flow.failure_report([], error.value.stage, error.value.reason_code, error.value.message)
    assert code == EXIT_ERROR
    assert report["next_action_code"] == "replace_probe_evidence"


def test_correction_details_never_shadow_the_reported_failure_identity() -> None:
    """Details add correction evidence; they never rewrite the failure (#409)."""
    from autoscout import source_onboarding_flow as flow

    report, code = flow.failure_report(
        [],
        "inputs",
        replay_fixtures.REASON_HELPER_IDENTITY_MISMATCH,
        "helper metadata identities do not match",
        {"reason_code": "shadowed", "stage": "shadowed", "representative_entries": []},
    )

    assert code == EXIT_ERROR
    assert report["stage"] == "inputs"
    assert report["reason_code"] == replay_fixtures.REASON_HELPER_IDENTITY_MISMATCH
    assert report["representative_entries"] == []


@pytest.mark.parametrize(
    "kind",
    [
        "stale",
        "missing",
        "extra",
        "every_bounded_entry",
        "duplicated",
        "reordered",
        "mismatched_author",
        "mismatched_evidence_url",
    ],
)
def test_every_preview_refusal_is_also_a_confirmed_fixture_refusal(kind: str) -> None:
    """The preview refuses exactly what confirmed fixture generation refuses (#409)."""
    with pytest.raises(replay_fixtures.ReplayFixtureError) as generation_error:
        replay_fixtures.build_replay_fixture_pair(
            URL,
            "issue-409-example-list",
            _five_entry_evidence(),
            signal_strength="shortlist",
            candidate_category="adult nonfiction",
            source_name=NAME,
            signal_family="award",
            language="en",
            helper_metadata=_helper_metadata(_mutated(kind)),
        )
    assert generation_error.value.reason_code in {
        replay_fixtures.REASON_HELPER_IDENTITY_MISMATCH,
        replay_fixtures.REASON_HELPER_DUPLICATE_ENTRY,
    }
