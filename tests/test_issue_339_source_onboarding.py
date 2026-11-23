"""Offline CLI acceptance tests for Issue #339 URL source onboarding."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from state_store_support import seed_state
from state_store_support import state_databases

from autoscout.__main__ import main
from autoscout.source_onboarding import OnboardingRunners
from autoscout.source_onboarding_models import OnboardingIssue
from autoscout.source_proposal_commands import record_source_proposal
from autoscout.state_store import export_json

URL = "https://example.test/issue-339-list"
NAME = "Issue 339 Example List"
HEADING = "Issue 339 Nonfiction List"
ROOT = Path(__file__).resolve().parents[1]


def _evidence(outcome: str = "accept") -> dict[str, Any]:
    html = (
        f"<h1>{HEADING}</h1><ul>"
        f'<li><a href="{URL}/book">The Example Book</a><span class="author">Jane Example</span></li>'
        "</ul>"
    )
    value: dict[str, Any] = {
        "collection_mode": "source-candidate-probe",
        "source_url": URL,
        "source_page": URL,
        "source_name": NAME,
        "name": NAME,
        "signal_family": "award",
        "resolved_url": URL,
        "head_status": 200,
        "get_status": 200,
        "html": html,
        "detected_heading": HEADING,
        "bounded_entry_count": 1,
    }
    if outcome != "accept":
        value["html"] = '<h1>JavaScript list</h1><script id="__NEXT_DATA__">{}</script>'
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _base_repo(tmp_path: Path) -> dict[str, Path]:
    repo = tmp_path / "repo"
    (repo / "configs").mkdir(parents=True)
    (repo / "data" / "current-weekly-pilot").mkdir(parents=True)
    (repo / "runs" / "current-weekly-pilot-russian").mkdir(parents=True)
    seed_state(repo / "runs" / "current-weekly-pilot-russian")
    (repo / "docs").mkdir()
    portfolio = repo / "configs" / "current-weekly-portfolio.json"
    _write_json(portfolio, {"sources": [{"name": "Existing", "url": "https://existing.test", "language": "en"}]})
    for filename in ("current-weekly-pilot.json", "current-weekly-pilot-live.json", "current-weekly-live.json"):
        _write_json(
            repo / "configs" / filename,
            {"run_date": "2026-07-18", "weekly_portfolio": portfolio.name, "sources": [{"name": "Existing"}]},
        )
    evidence = repo / "data" / "source-candidate-captures" / "issue-339-retained.json"
    _write_json(evidence, _evidence())
    helper = repo / "helper.json"
    _write_json(
        helper,
        {
            "version": 1,
            "source_url": URL,
            "language": "en",
            "entries": [
                {
                    "title": "The Example Book",
                    "author": "Jane Example",
                    "evidence_url": f"{URL}/book",
                    "english_title": "The Example Book",
                    "search_translation": "Пример книги",
                    "author_transliteration_note": "Джейн Пример",
                }
            ],
        },
    )
    feasibility = repo / "feasibility-draft.md"
    feasibility.write_text(
        "# Issue #401 feasibility evidence\n\n"
        "Source: Issue 339 Example List\n\n"
        f"URL: {URL}\n\n"
        "The retained evidence confirms a server-rendered exact adult nonfiction list.\n",
        encoding="utf-8",
    )
    return {
        "repo": repo,
        "evidence": evidence,
        "helper": helper,
        "feasibility": feasibility,
        "proposal": repo / "runs" / "current-weekly-pilot-russian" / "source-proposals.json",
        "portfolio": portfolio,
        "pilot": repo / "configs" / "current-weekly-pilot.json",
        "pilot_live": repo / "configs" / "current-weekly-pilot-live.json",
        "live": repo / "configs" / "current-weekly-live.json",
    }


def _git(repo: Path, *args: str) -> None:
    result = subprocess.run(["git", *args], cwd=repo, check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _init_git(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")


def _args(paths: dict[str, Path], *, include_run_date: bool = True) -> list[str]:
    args = [
        "add-source",
        "--url",
        URL,
        "--probe-evidence",
        str(paths["evidence"]),
        "--helper-metadata",
        str(paths["helper"]),
        "--feasibility-evidence",
        str(paths["feasibility"]),
        "--feasibility-doc",
        str(paths["repo"] / "docs" / "issue-401-example-source-feasibility.md"),
        "--onboarding-issue",
        "401",
        "--reason",
        "The accepted list is a stable adult nonfiction signal.",
        "--source-name",
        NAME,
        "--family",
        "award",
        "--signal-family",
        "award",
        "--access-status",
        "open",
        "--check-frequency",
        "weekly",
        "--language",
        "en",
        "--country",
        "US",
        "--market",
        "adult nonfiction",
        "--expected-category",
        "adult nonfiction",
        "--refresh-cadence",
        "weekly",
        "--url-stability",
        "stable category page",
        "--extraction-notes",
        "Metadata-only exact titles and primary authors with grounded book links.",
        "--expected-page-heading",
        HEADING,
        "--expected-signal-strength",
        "shortlist",
        "--eligible-candidate-category",
        "adult nonfiction",
        "--lifecycle-note",
        "Issue #401 active and reversible during probation.",
        "--notes",
        "Issue #401 accepted Source Proposal; replay-first onboarding.",
        "--proposals-file",
        str(paths["proposal"]),
        "--portfolio-config",
        str(paths["portfolio"]),
        "--pilot-config",
        str(paths["pilot"]),
        "--pilot-live-config",
        str(paths["pilot_live"]),
        "--live-config",
        str(paths["live"]),
        "--repo-root",
        str(paths["repo"]),
    ]
    if include_run_date:
        args.extend(["--run-date", "2026-07-18"])
    return args


def test_every_probe_option_in_the_source_onboarding_runbook_is_a_real_probe_parser_option() -> None:
    runbook = (ROOT / "docs" / "source-onboarding-runbook.md").read_text(encoding="utf-8")
    probe_command_match = re.search(r"```bash\n(\./scripts/add-source probe.*?\n)```", runbook, flags=re.DOTALL)
    assert probe_command_match is not None
    documented_options = set(re.findall(r"(?<!\w)--[a-z][a-z-]*", probe_command_match.group(1)))
    assert "--ledger-markdown" not in documented_options

    launcher = (ROOT / "scripts" / "probe-source").read_text(encoding="utf-8")
    target_match = re.search(r'-m autoscout ([a-z-]+) "\$@"', launcher)
    assert target_match is not None
    assert target_match.group(1) == "probe-source-candidate"

    add_source_launcher = (ROOT / "scripts" / "add-source").read_text(encoding="utf-8")
    assert "scripts/probe-source" in add_source_launcher

    target_source = ast.parse((ROOT / "autoscout" / "cli" / "onboarding.py").read_text(encoding="utf-8"))
    supported: set[str] = set()
    for node in ast.walk(target_source):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "add_argument" or not isinstance(node.func.value, ast.Name):
            continue
        if node.func.value.id != "source_probe_parser":
            continue
        supported.update(
            value.value
            for value in node.args
            if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value.startswith("--")
        )
    assert documented_options <= supported


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Issue #40 is the onboarding record.", True),
        ("Issue #401 is a different record.", False),
        ("https://example.test/issues/40", False),
        ("issue-40 is only a filename token.", False),
        ("Issue 40 has no explicit hash.", False),
        ("Issue #40x is a different token.", False),
    ],
)
def test_onboarding_issue_matching_requires_exact_hash_token(text: str, expected: bool) -> None:
    assert OnboardingIssue.parse("40").matches(text) is expected


def _is_git_internal(relative: Path) -> bool:
    return ".git" in relative.parts


def _snapshot_files(root: Path) -> dict[Path, bytes]:
    return {
        path: path.read_bytes()
        for path in root.rglob("*")
        if not _is_git_internal(path.relative_to(root)) and path.is_file()
    }


def _snapshot_tree(root: Path) -> dict[Path, tuple[str, bytes | None, bool, int | None]]:
    snapshot: dict[Path, tuple[str, bytes | None, bool, int | None]] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if _is_git_internal(relative):
            continue
        if path.is_symlink():
            snapshot[relative] = ("symlink", path.readlink().encode(), True, None)
        elif path.is_dir():
            snapshot[relative] = ("directory", None, False, None)
        else:
            snapshot[relative] = ("file", path.read_bytes(), False, path.stat().st_nlink)
    for name, checksum in state_databases().items():
        snapshot[Path(name)] = ("state database", checksum, False, None)
    return snapshot


def test_snapshot_helpers_never_read_git_internals(tmp_path: Path) -> None:
    """Pin the #516 fix so it cannot be reverted in silence.

    The flaky failures came from background ``git gc`` writing into ``.git``
    between the two snapshots of a side-effect assertion.  That is a timing
    condition, so no deterministic test can reproduce the failure itself.
    This asserts the property instead: the helpers never look inside ``.git``.

    The first assertion proves the fixture really does contain git internals,
    so the other two cannot pass vacuously.
    """
    paths = _base_repo(tmp_path)
    _init_git(paths["repo"])
    root = paths["repo"]

    assert any(_is_git_internal(path.relative_to(root)) for path in root.rglob("*"))
    assert not any(_is_git_internal(path.relative_to(root)) for path in _snapshot_files(root))
    assert not any(_is_git_internal(relative) for relative in _snapshot_tree(root))


def _git_snapshot(repo: Path) -> tuple[str, bytes, str]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    index_path = subprocess.run(
        ["git", "rev-parse", "--git-path", "index"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    index = (repo / index_path).read_bytes()
    status = subprocess.run(["git", "status", "--short"], cwd=repo, check=True, capture_output=True, text=True).stdout
    return head, index, status


def test_add_source_uses_a_fixed_fixture_directory_without_override_options(
    capsys: Any,
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["add-source", "--help"])
    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "--fixture-dir" not in help_text
    assert "--data-dir" not in help_text


def _issue_40_args(paths: dict[str, Path]) -> list[str]:
    args = _args(paths)
    args[args.index("401")] = "40"
    for value in (
        "Issue #401 active and reversible during probation.",
        "Issue #401 accepted Source Proposal; replay-first onboarding.",
    ):
        index = args.index(value)
        args[index] = value.replace("#401", "#40")
    destination = str(paths["repo"] / "docs" / "issue-401-example-source-feasibility.md")
    args[args.index(destination)] = str(paths["repo"] / "docs" / "issue-40-example-source-feasibility.md")
    paths["feasibility"].write_text(
        paths["feasibility"].read_text(encoding="utf-8").replace("#401", "#40"), encoding="utf-8"
    )
    return args


def test_public_cli_requires_exact_issue_in_notes_and_feasibility_evidence(capsys: Any, tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)
    args = _issue_40_args(paths)
    notes_index = args.index("Issue #40 accepted Source Proposal; replay-first onboarding.")
    args[notes_index] = "Issue #401 is not this onboarding issue."

    assert main(args) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "onboarding_issue_missing"

    paths = _base_repo(tmp_path / "second")
    args = _issue_40_args(paths)
    paths["feasibility"].write_text(
        paths["feasibility"].read_text(encoding="utf-8").replace("#40", "https://example.test/issues/40"),
        encoding="utf-8",
    )

    assert main(args) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "feasibility_issue_missing"


@pytest.mark.parametrize(
    ("relative_path", "content", "reason_code"),
    [
        ("tests/test_source_issue339.py", None, "source_test_missing"),
        (
            "tests/test_source_issue339.py",
            "def test_source_specific_marker():\n    assert True\n",
            "source_test_invalid",
        ),
        ("tests/test_source_issue339.py", '"""Issue #400 source-specific checks."""\n', "source_test_invalid"),
        (
            "tests/test_source_issue339.py",
            'VALUE = "Issue #401 source-specific checks."\n"""not a module docstring"""\n',
            "source_test_invalid",
        ),
        ("tests/source_issue339.py", '"""Issue #401 source-specific checks."""\n', "source_test_invalid"),
        ("tests/nested/test_source_issue339.py", '"""Issue #401 source-specific checks."""\n', "source_test_invalid"),
        ("tests/test_source_issue339.py", '"""Issue #401 source-specific checks.\n', "source_test_invalid"),
    ],
)
def test_public_cli_rejects_invalid_source_specific_tests(
    capsys: Any, tmp_path: Path, relative_path: str, content: str | None, reason_code: str
) -> None:
    paths = _base_repo(tmp_path)
    source_test = paths["repo"] / relative_path
    if content is not None:
        source_test.parent.mkdir(parents=True, exist_ok=True)
        source_test.write_text(content, encoding="utf-8")
    before = _snapshot_files(paths["repo"])

    assert main([*_args(paths), "--source-specific-test", str(source_test)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == reason_code
    assert _snapshot_files(paths["repo"]) == before
    assert not list(paths["repo"].glob("**/source-proposals.json"))


def test_public_cli_accepts_normalized_exact_issue_and_filename(capsys: Any, tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)
    args = _issue_40_args(paths)
    args[args.index("40")] = "#040"

    assert main(args) == 3
    report = json.loads(capsys.readouterr().out)
    assert report["onboarding_request"]["onboarding_issue"] == "40"
    assert report["artifact_identity"]["feasibility_evidence"]["destination"].endswith(
        "docs/issue-40-example-source-feasibility.md"
    )


@pytest.mark.parametrize("status", ["rejected", "infeasible"])
def test_source_proposal_accept_restores_legacy_status_behavior(capsys: Any, tmp_path: Path, status: str) -> None:
    state_dir = tmp_path / "state"
    config = tmp_path / "config.json"
    _write_json(config, {"sources": []})
    proposal, _ = record_source_proposal(
        state_dir,
        source_url=URL,
        proposed_by="editor",
        reason="terminal proposal",
        status=status,
    )
    code = main(
        [
            "source-proposal",
            "accept",
            "--id",
            proposal["id"],
            "--state-dir",
            str(state_dir),
            "--config",
            str(config),
            "--language",
            "en",
        ]
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "source_proposal_status=accepted" in output
    assert "not_found" not in output


def test_source_proposal_accept_keeps_valid_behavior(capsys: Any, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    config = tmp_path / "config.json"
    _write_json(config, {"sources": []})
    proposal, _ = record_source_proposal(
        state_dir,
        source_url=URL,
        proposed_by="editor",
        reason="valid proposal",
        source_name=NAME,
        suggested_signal_family="award",
    )

    code = main(
        [
            "source-proposal",
            "accept",
            "--id",
            proposal["id"],
            "--state-dir",
            str(state_dir),
            "--config",
            str(config),
            "--language",
            "en",
        ]
    )

    output = capsys.readouterr().out
    assert code == 0
    assert "source_proposal_status=accepted" in output
    assert json.loads(config.read_text(encoding="utf-8"))["sources"][0]["url"] == URL


def test_rejected_probe_is_structured_and_has_no_proposal(capsys: Any, tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)
    evidence = _evidence("defer")
    evidence["head_status"] = 403
    evidence["get_status"] = None
    _write_json(paths["evidence"], evidence)

    code = main(_args(paths))

    report = json.loads(capsys.readouterr().out)
    assert code == 5
    assert report["stage"] == "probe"
    assert report["outcome"] == "reject"
    assert report["exit_code"] == 5
    assert report["next_action_code"] == "record_reject"
    assert report["next_action"]
    assert list(paths["repo"].glob("**/source-proposals.json")) == []
    assert not list((paths["repo"] / "data" / "current-weekly-pilot").glob("*codex-*.json"))


@pytest.mark.parametrize("collection_mode", [None, "", "codex-capture"])
def test_public_cli_rejects_non_probe_collection_mode_without_any_mutation(
    capsys: Any, tmp_path: Path, collection_mode: str | None
) -> None:
    paths = _base_repo(tmp_path)
    evidence = _evidence()
    if collection_mode is None:
        evidence.pop("collection_mode")
    else:
        evidence["collection_mode"] = collection_mode
    _write_json(paths["evidence"], evidence)
    before = _snapshot_files(paths["repo"])

    assert main(_args(paths)) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["stage"] == "probe"
    assert report["reason_code"] == "collection_mode_invalid"
    assert _snapshot_files(paths["repo"]) == before
    assert not list(paths["repo"].glob("**/source-proposals.json"))


def test_public_cli_rejects_noncanonical_proposal_path_without_any_mutation(capsys: Any, tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)
    custom_proposals = paths["proposal"].with_name("custom-proposals.json")
    args = [str(custom_proposals) if value == str(paths["proposal"]) else value for value in _args(paths)]
    before = _snapshot_files(paths["repo"])

    assert main(args) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "proposal_file_invalid"
    assert _snapshot_files(paths["repo"]) == before
    assert not custom_proposals.exists()
    assert not (custom_proposals.parent / "source-proposals.json").exists()


def test_public_cli_rejects_symlinked_proposal_without_replacing_it(capsys: Any, tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)
    sibling = paths["proposal"].with_name("custom.json")
    _write_json(sibling, [])
    paths["proposal"].symlink_to(sibling.name)
    _init_git(paths["repo"])
    before_files = _snapshot_files(paths["repo"])
    before_link = paths["proposal"].readlink()
    before_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=paths["repo"], check=True, capture_output=True, text=True
    ).stdout.strip()

    assert main(_args(paths)) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "proposal_file_invalid"
    assert paths["proposal"].is_symlink()
    assert paths["proposal"].readlink() == before_link
    assert sibling.read_bytes() == b"[]\n"
    assert _snapshot_files(paths["repo"]) == before_files
    assert (
        subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=paths["repo"], check=True, capture_output=True, text=True
        ).stdout.strip()
        == before_head
    )
    assert (
        subprocess.run(
            ["git", "status", "--short"], cwd=paths["repo"], check=True, capture_output=True, text=True
        ).stdout
        == ""
    )


def test_public_cli_rejects_hard_linked_proposal_without_replacing_it(capsys: Any, tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)
    sibling = paths["proposal"].with_name("custom.json")
    _write_json(sibling, [])
    paths["proposal"].hardlink_to(sibling)
    _init_git(paths["repo"])
    before_files = _snapshot_files(paths["repo"])
    before_inode = paths["proposal"].stat().st_ino
    before_nlink = paths["proposal"].stat().st_nlink
    before_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=paths["repo"], check=True, capture_output=True, text=True
    ).stdout.strip()

    assert main(_args(paths)) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "proposal_file_invalid"
    assert not paths["proposal"].is_symlink()
    assert paths["proposal"].stat().st_ino == before_inode
    assert paths["proposal"].stat().st_nlink == before_nlink == 2
    assert sibling.stat().st_ino == before_inode
    assert _snapshot_files(paths["repo"]) == before_files
    assert (
        subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=paths["repo"], check=True, capture_output=True, text=True
        ).stdout.strip()
        == before_head
    )
    assert (
        subprocess.run(
            ["git", "status", "--short"], cwd=paths["repo"], check=True, capture_output=True, text=True
        ).stdout
        == ""
    )


def test_public_cli_rejects_symlinked_proposal_ancestor_without_creating_state(capsys: Any, tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)
    real_state = paths["proposal"].parent
    alias = paths["repo"] / "runs" / "weekly-state-alias"
    alias.symlink_to(real_state, target_is_directory=True)
    proposal = alias / "source-proposals.json"
    args = [str(proposal) if value == str(paths["proposal"]) else value for value in _args(paths)]
    before = _snapshot_files(paths["repo"])

    assert main(args) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "proposal_file_invalid"
    assert alias.is_symlink()
    assert alias.readlink() == real_state
    assert not (real_state / "source-proposals.json").exists()
    assert _snapshot_files(paths["repo"]) == before


def test_deferred_probe_is_structured_and_has_no_proposal(capsys: Any, tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)
    _write_json(paths["evidence"], _evidence("defer"))

    code = main(_args(paths))

    report = json.loads(capsys.readouterr().out)
    assert code == 4
    assert report["stage"] == "probe"
    assert report["outcome"] == "defer"
    assert report["exit_code"] == 4
    assert report["next_action"]
    assert list(paths["repo"].glob("**/source-proposals.json")) == []


def test_rejects_traversal_slug_and_helper_destination_substitution(capsys: Any, tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)

    assert main([*_args(paths), "--slug", "../escape"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "invalid_slug"
    assert not (paths["repo"].parent / "escape-helper-metadata.json").exists()

    expected_helper = paths["repo"] / "data" / "current-weekly-pilot" / "issue-339-example-list-helper-metadata.json"
    wrong_helper = paths["repo"] / "data" / "current-weekly-pilot" / "other-helper-metadata.json"
    assert main([*_args(paths), "--helper-metadata-destination", str(wrong_helper)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "helper_metadata_destination_invalid"
    assert not expected_helper.exists()


def test_rejects_substituted_feasibility_destination_and_non_numeric_issue(capsys: Any, tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)

    assert (
        main(
            [
                *_args(paths),
                "--onboarding-issue",
                "issue-401",
            ]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "onboarding_issue_required"

    wrong_destination = paths["repo"] / "docs" / "issue-401-example.md"
    assert main([*_args(paths), "--feasibility-doc", str(wrong_destination)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "feasibility_destination_invalid"


@pytest.mark.parametrize("option", ["--ledger", "--ledger-markdown", "--durable-artifact"])
def test_removed_add_source_probe_output_options_are_unrecognized(option: str) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["add-source", option, "outside-onboarding-scope"])
    assert exc_info.value.code == 2


def test_one_source_only_and_payload_mismatch_are_fail_closed(capsys: Any, tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)
    assert main([*_args(paths), "--url", URL + "/second"]) == 1
    capsys.readouterr()
    _init_git(paths["repo"])
    assert main([*_args(paths), "--run-date", "2026-07-18"]) == 3
    preview = json.loads(capsys.readouterr().out)
    changed = [*_args(paths), "--run-date", "2026-07-18"]
    changed[changed.index("The accepted list is a stable adult nonfiction signal.")] = "A changed operator reason."
    assert main([*changed, "--confirmation-token", preview["confirmation_token"], "--confirmed-by-editor"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "confirmation_payload_mismatch"
    assert not list((paths["repo"] / "data" / "current-weekly-pilot").glob("*codex-*.json"))


def test_dirty_canonical_config_blocks_confirmation(capsys: Any, tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)
    _init_git(paths["repo"])
    paths["pilot"].write_text(paths["pilot"].read_text(encoding="utf-8") + "\n", encoding="utf-8")
    preview_args = [*_args(paths), "--run-date", "2026-07-18"]
    assert main(preview_args) == 3
    preview = json.loads(capsys.readouterr().out)
    before = _snapshot_tree(paths["repo"])

    assert main([*preview_args, "--confirmation-token", preview["confirmation_token"], "--confirmed-by-editor"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "canonical_destination_dirty"
    assert report["next_action_code"] == "clear_canonical_changes"
    assert _snapshot_tree(paths["repo"]) == before
    assert (
        subprocess.run(
            ["git", "rev-list", "--count", "HEAD"], cwd=paths["repo"], check=True, capture_output=True, text=True
        ).stdout.strip()
        == "1"
    )


def test_accept_preview_records_only_proposed_source_and_requires_confirmation(capsys: Any, tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)
    before_configs = {key: paths[key].read_bytes() for key in ("portfolio", "pilot", "pilot_live", "live")}

    code = main(_args(paths))

    report = json.loads(capsys.readouterr().out)
    assert code == 3
    assert report["stage"] == "proposal"
    assert report["outcome"] == "confirmation_required"
    assert report["confirmation_required"] is True
    assert report["next_action"]
    assert not paths["proposal"].exists()
    request = report["onboarding_request"]
    assert request["metadata"]["country"] == "US"
    assert request["onboarding_issue"] == "401"
    assert request["run_date"]
    config_identity = {item["path"]: item["sha256"] for item in request["artifact_identity"]["baseline_configs"]}
    assert config_identity[str(paths["portfolio"])] == hashlib.sha256(before_configs["portfolio"]).hexdigest()
    assert config_identity[str(paths["pilot"])] == hashlib.sha256(before_configs["pilot"]).hexdigest()
    assert config_identity[str(paths["pilot_live"])] == hashlib.sha256(before_configs["pilot_live"]).hexdigest()
    assert config_identity[str(paths["live"])] == hashlib.sha256(before_configs["live"]).hexdigest()
    assert {key: paths[key].read_bytes() for key in ("portfolio", "pilot", "pilot_live", "live")} == before_configs
    assert not list((paths["repo"] / "data" / "current-weekly-pilot").glob("*codex-*.json"))


def test_absent_proposal_state_stays_absent_until_confirmed_staging(
    capsys: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _base_repo(tmp_path)
    absent_proposal = paths["repo"] / "new-source-state" / "source-proposals.json"
    _init_git(paths["repo"])
    preview_args = [str(absent_proposal) if value == str(paths["proposal"]) else value for value in _args(paths)]
    preview_args.extend(["--run-date", "2026-07-18"])

    assert main(preview_args) == 3
    preview = json.loads(capsys.readouterr().out)
    assert preview["artifact_identity"]["proposal_state"] == {
        "path": str(absent_proposal),
        "present": False,
        "sha256": None,
    }
    assert (
        preview["onboarding_request"]["artifact_identity"]["proposal_state"]
        == preview["artifact_identity"]["proposal_state"]
    )
    assert not absent_proposal.exists()
    assert not absent_proposal.parent.exists()

    runners = OnboardingRunners(
        replay=lambda *_args, **_kwargs: {"status": "passed"},
        contracts=lambda *_args, **_kwargs: {"status": "passed"},
        full_suite=lambda *_args, **_kwargs: {"status": "passed"},
    )
    monkeypatch.setattr("autoscout.source_onboarding_transaction.default_onboarding_runners", lambda: runners)
    assert (
        main(
            [
                *preview_args,
                "--confirmation-token",
                preview["confirmation_token"],
                "--confirmed-by-editor",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["outcome"] == "completed"
    assert absent_proposal.exists()


def test_input_byte_change_after_preview_blocks_confirmation(capsys: Any, tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)
    _init_git(paths["repo"])
    preview_args = [*_args(paths), "--run-date", "2026-07-18"]
    assert main(preview_args) == 3
    preview = json.loads(capsys.readouterr().out)
    paths["helper"].write_text(paths["helper"].read_text(encoding="utf-8") + "\n", encoding="utf-8")

    assert main([*preview_args, "--confirmation-token", preview["confirmation_token"], "--confirmed-by-editor"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "confirmation_payload_mismatch"
    assert (
        subprocess.run(
            ["git", "rev-list", "--count", "HEAD"], cwd=paths["repo"], check=True, capture_output=True, text=True
        ).stdout.strip()
        == "1"
    )


def test_changed_preview_mismatch_is_side_effect_free_and_confirmation_uses_reason_a(
    capsys: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _base_repo(tmp_path)
    _init_git(paths["repo"])
    reason_a = "Reason A is the prepared onboarding reason."
    reason_b = "Reason B must not enter proposal state."
    args_a = _args(paths)
    args_a[args_a.index("The accepted list is a stable adult nonfiction signal.")] = reason_a
    args_a.extend(["--run-date", "2026-07-18"])
    args_b = list(args_a)
    args_b[args_b.index(reason_a)] = reason_b

    assert main(args_a) == 3
    preview = json.loads(capsys.readouterr().out)
    before = _snapshot_tree(paths["repo"])

    assert main([*args_b, "--confirmation-token", preview["confirmation_token"], "--confirmed-by-editor"]) == 1
    mismatch = json.loads(capsys.readouterr().out)
    assert mismatch["reason_code"] == "confirmation_payload_mismatch"
    assert _snapshot_tree(paths["repo"]) == before

    runners = OnboardingRunners(
        replay=lambda *_args, **_kwargs: {"status": "passed"},
        contracts=lambda *_args, **_kwargs: {"status": "passed"},
        full_suite=lambda *_args, **_kwargs: {"status": "passed"},
    )
    monkeypatch.setattr("autoscout.source_onboarding_transaction.default_onboarding_runners", lambda: runners)
    assert main([*args_a, "--confirmation-token", preview["confirmation_token"], "--confirmed-by-editor"]) == 0
    capsys.readouterr()
    committed = subprocess.run(
        ["git", "show", "HEAD:runs/current-weekly-pilot-russian/source-proposals.json"],
        cwd=paths["repo"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    committed_proposal = json.loads(committed)[0]
    assert committed_proposal["reason"] == reason_a
    assert reason_b not in committed


def test_exact_repeated_preview_is_byte_idempotent(capsys: Any, tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)
    args = [*_args(paths), "--run-date", "2026-07-18"]

    assert main(args) == 3
    first = json.loads(capsys.readouterr().out)
    first_tree = _snapshot_tree(paths["repo"])

    assert main(args) == 3
    second = json.loads(capsys.readouterr().out)
    assert second["confirmation_required"] is True
    assert second["onboarding_request"] == first["onboarding_request"]
    assert second["confirmation_token"] == first["confirmation_token"]
    assert _snapshot_tree(paths["repo"]) == first_tree


def test_confirmation_requires_explicit_run_date_even_with_preview_token(
    capsys: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _base_repo(tmp_path)
    _init_git(paths["repo"])
    preview_args = [*_args(paths), "--run-date", "2026-07-18"]
    assert main(preview_args) == 3
    preview = json.loads(capsys.readouterr().out)
    assert preview["effective_run_date"] == "2026-07-18"
    confirmation_args = [argument for argument in preview_args if argument not in {"--run-date", "2026-07-18"}]

    assert (
        main(
            [
                *confirmation_args,
                "--confirmation-token",
                preview["confirmation_token"],
                "--confirmed-by-editor",
            ]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "run_date_required"


def test_changed_manual_proposal_after_preview_blocks_confirmation_without_mutation(
    capsys: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _base_repo(tmp_path)
    reason_a = "Reason A is the prepared onboarding reason."
    reason_b = "Reason B was not shown in the preview."
    record_source_proposal(
        paths["proposal"].parent,
        source_url=URL,
        proposed_by="editor",
        reason=reason_a,
        proposal_date="2026-07-18",
        suggested_signal_family="award",
        source_name=NAME,
        status="proposed",
    )
    export_json(paths["proposal"].parent)
    _init_git(paths["repo"])
    preview_args = [*_args(paths), "--run-date", "2026-07-18"]
    preview_args[preview_args.index("The accepted list is a stable adult nonfiction signal.")] = reason_a
    assert main(preview_args) == 3
    preview = json.loads(capsys.readouterr().out)
    proposal_identity = preview["artifact_identity"]["proposal_state"]
    assert proposal_identity["present"] is True
    assert proposal_identity["sha256"] == hashlib.sha256(paths["proposal"].read_bytes()).hexdigest()
    assert preview["onboarding_request"]["artifact_identity"] == preview["artifact_identity"]

    proposal_state = json.loads(paths["proposal"].read_text(encoding="utf-8"))
    proposal_state[0]["reason"] = reason_b
    _write_json(paths["proposal"], proposal_state)
    after_edit_tree = _snapshot_tree(paths["repo"])
    after_edit_git = _git_snapshot(paths["repo"])

    runners = OnboardingRunners(
        replay=lambda *_args, **_kwargs: {"status": "passed"},
        contracts=lambda *_args, **_kwargs: {"status": "passed"},
        full_suite=lambda *_args, **_kwargs: {"status": "passed"},
    )
    monkeypatch.setattr("autoscout.source_onboarding_transaction.default_onboarding_runners", lambda: runners)
    assert (
        main(
            [
                *preview_args,
                "--confirmation-token",
                preview["confirmation_token"],
                "--confirmed-by-editor",
            ]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "confirmation_payload_mismatch"
    assert report["next_action_code"] == "repreview_payload"
    assert _snapshot_tree(paths["repo"]) == after_edit_tree
    assert _git_snapshot(paths["repo"]) == after_edit_git


@pytest.mark.parametrize("mutation", ["created", "removed", "replaced"])
def test_proposal_state_creation_removal_and_replacement_block_confirmation(
    capsys: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutation: str
) -> None:
    paths = _base_repo(tmp_path)
    reason_a = "Reason A is the prepared onboarding reason."
    if mutation != "created":
        record_source_proposal(
            paths["proposal"].parent,
            source_url=URL,
            proposed_by="editor",
            reason=reason_a,
            proposal_date="2026-07-18",
            suggested_signal_family="award",
            source_name=NAME,
            status="proposed",
        )
        export_json(paths["proposal"].parent)
    _init_git(paths["repo"])
    preview_args = [*_args(paths), "--run-date", "2026-07-18"]
    if mutation != "created":
        preview_args[preview_args.index("The accepted list is a stable adult nonfiction signal.")] = reason_a
    assert main(preview_args) == 3
    preview = json.loads(capsys.readouterr().out)
    if mutation == "created":
        _write_json(paths["proposal"], [])
    elif mutation == "removed":
        paths["proposal"].unlink()
    else:
        _write_json(paths["proposal"], [])
    after_edit_tree = _snapshot_tree(paths["repo"])
    after_edit_git = _git_snapshot(paths["repo"])

    runners = OnboardingRunners(
        replay=lambda *_args, **_kwargs: {"status": "passed"},
        contracts=lambda *_args, **_kwargs: {"status": "passed"},
        full_suite=lambda *_args, **_kwargs: {"status": "passed"},
    )
    monkeypatch.setattr("autoscout.source_onboarding_transaction.default_onboarding_runners", lambda: runners)
    assert (
        main(
            [
                *preview_args,
                "--confirmation-token",
                preview["confirmation_token"],
                "--confirmed-by-editor",
            ]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "confirmation_payload_mismatch"
    assert report["next_action_code"] == "repreview_payload"
    assert _snapshot_tree(paths["repo"]) == after_edit_tree
    assert _git_snapshot(paths["repo"]) == after_edit_git


def test_manual_proposed_source_proposal_is_attached_and_onboarded(
    capsys: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _base_repo(tmp_path)
    proposal, _ = record_source_proposal(
        paths["proposal"].parent,
        source_url=URL,
        proposed_by="editor",
        reason="Manually prepared proposal.",
        proposal_date="2026-07-18",
        suggested_signal_family="award",
        source_name=NAME,
        status="proposed",
    )
    export_json(paths["proposal"].parent)
    assert proposal["status"] == "proposed"
    _init_git(paths["repo"])
    args = [*_args(paths), "--run-date", "2026-07-18"]
    assert main(args) == 3
    preview = json.loads(capsys.readouterr().out)
    assert preview["proposal_id"] == proposal["id"]
    assert "onboarding_request" not in json.loads(paths["proposal"].read_text(encoding="utf-8"))[0]

    runners = OnboardingRunners(
        replay=lambda *_args, **_kwargs: {"status": "passed"},
        contracts=lambda *_args, **_kwargs: {"status": "passed"},
        full_suite=lambda *_args, **_kwargs: {"status": "passed"},
    )
    monkeypatch.setattr("autoscout.source_onboarding_transaction.default_onboarding_runners", lambda: runners)
    assert main([*args, "--confirmation-token", preview["confirmation_token"], "--confirmed-by-editor"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["outcome"] == "completed"


def test_commit_failure_restores_canonical_files_and_git_index(
    capsys: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _base_repo(tmp_path)
    _init_git(paths["repo"])
    preview_args = [*_args(paths), "--run-date", "2026-07-18"]
    assert main(preview_args) == 3
    preview = json.loads(capsys.readouterr().out)
    before = {key: paths[key].read_bytes() for key in ("portfolio", "pilot", "pilot_live", "live")}
    index = subprocess.run(
        ["git", "rev-parse", "--git-path", "index"], cwd=paths["repo"], check=True, capture_output=True, text=True
    ).stdout.strip()
    index_path = paths["repo"] / index
    index_before = index_path.read_bytes()
    import autoscout.source_onboarding_transaction as module

    runners = OnboardingRunners(
        replay=lambda *_args, **_kwargs: {"status": "passed"},
        contracts=lambda *_args, **_kwargs: {"status": "passed"},
        full_suite=lambda *_args, **_kwargs: {"status": "passed"},
    )
    monkeypatch.setattr(module, "default_onboarding_runners", lambda: runners)
    real_git = module.git

    def fail_commit(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        if args[:2] == ("commit", "-m"):
            return subprocess.CompletedProcess(["git", *args], 1, "", "commit failed")
        return real_git(repo, *args)

    monkeypatch.setattr(module, "git", fail_commit)
    assert main([*preview_args, "--confirmation-token", preview["confirmation_token"], "--confirmed-by-editor"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "git_commit_failed"
    assert report["next_action_code"] == "repair_git_commit"
    assert {key: paths[key].read_bytes() for key in before} == before
    assert index_path.read_bytes() == index_before
    assert (
        subprocess.run(
            ["git", "rev-list", "--count", "HEAD"], cwd=paths["repo"], check=True, capture_output=True, text=True
        ).stdout.strip()
        == "1"
    )


@pytest.mark.parametrize(
    "target", ["proposal_file", "source_specific_test", "probe_evidence", "helper_input", "feasibility_input"]
)
def test_validation_cannot_mutate_any_publishable_destination(
    capsys: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: str
) -> None:
    paths = _base_repo(tmp_path)
    _init_git(paths["repo"])
    source_test = paths["repo"] / "tests" / "test_source_issue339.py"
    source_test.parent.mkdir(parents=True, exist_ok=True)
    source_test.write_text(
        '"""Issue #401 source-specific checks."""\n\n\ndef test_source_specific_marker():\n    assert True\n',
        encoding="utf-8",
    )
    args = [*_args(paths), "--source-specific-test", str(source_test)]
    assert main([*args, "--run-date", "2026-07-18"]) == 3
    preview = json.loads(capsys.readouterr().out)
    before = _snapshot_files(paths["repo"])

    def mutate(_stage_root: Path, staged_paths: Mapping[str, Any], _source: Mapping[str, Any]) -> dict[str, str]:
        destination = staged_paths[target]
        assert isinstance(destination, Path)
        destination.write_bytes(b"unexpected validation mutation\n")
        return {"status": "passed"}

    runners = OnboardingRunners(
        replay=mutate,
        contracts=lambda *_args, **_kwargs: {"status": "passed"},
        full_suite=lambda *_args, **_kwargs: {"status": "passed"},
    )
    monkeypatch.setattr("autoscout.source_onboarding_transaction.default_onboarding_runners", lambda: runners)

    assert (
        main(
            [
                *args,
                "--run-date",
                "2026-07-18",
                "--confirmation-token",
                preview["confirmation_token"],
                "--confirmed-by-editor",
            ]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "canonical_mutation"
    assert _snapshot_files(paths["repo"]) == before
    assert (
        subprocess.run(
            ["git", "rev-list", "--count", "HEAD"], cwd=paths["repo"], check=True, capture_output=True, text=True
        ).stdout.strip()
        == "1"
    )
    assert not list((paths["repo"] / "data" / "current-weekly-pilot").glob("*codex-*.json"))
    assert not (paths["repo"] / "docs" / "issue-401-example-source-feasibility.md").exists()


@pytest.mark.parametrize("mutation_stage", ["replay", "contracts", "validation"])
def test_validation_cannot_mutate_configs_or_fixture_destinations(
    capsys: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutation_stage: str
) -> None:
    paths = _base_repo(tmp_path)
    _init_git(paths["repo"])
    preview_args = [*_args(paths), "--run-date", "2026-07-18"]
    assert main(preview_args) == 3
    preview = json.loads(capsys.readouterr().out)
    before = {key: paths[key].read_bytes() for key in ("portfolio", "pilot", "pilot_live", "live")}

    def mutate_stage(_stage_root: Path, staged_paths: Mapping[str, Any], _source: Mapping[str, Any]) -> dict[str, Any]:
        for path in (
            *staged_paths["config_paths"].all,
            staged_paths["page_fixture"],
            staged_paths["extraction_fixture"],
        ):
            path.write_bytes(b"unexpected validation mutation\n")
        return {"status": "passed"}

    def passing(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {"status": "passed"}

    if mutation_stage == "validation":

        def mutate_full(stage_root: Path) -> dict[str, Any]:
            for filename in (
                "current-weekly-portfolio.json",
                "current-weekly-pilot.json",
                "current-weekly-pilot-live.json",
                "current-weekly-live.json",
            ):
                (stage_root / "configs" / filename).write_bytes(b"unexpected validation mutation\n")
            return {"status": "passed"}

        runners = OnboardingRunners(replay=passing, contracts=passing, full_suite=mutate_full)
    else:
        runners = OnboardingRunners(
            replay=mutate_stage if mutation_stage == "replay" else passing,
            contracts=mutate_stage if mutation_stage == "contracts" else passing,
            full_suite=passing,
        )
    monkeypatch.setattr("autoscout.source_onboarding_transaction.default_onboarding_runners", lambda: runners)

    assert (
        main(
            [
                *preview_args,
                "--confirmation-token",
                preview["confirmation_token"],
                "--confirmed-by-editor",
            ]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "canonical_mutation"
    assert {key: paths[key].read_bytes() for key in ("portfolio", "pilot", "pilot_live", "live")} == before


@pytest.mark.parametrize(
    ("failed_stage", "reason_code", "action_code"),
    [
        ("replay", "replay_validation_failed", "repair_replay_fixtures"),
        ("contracts", "contract_validation_failed", "repair_source_contracts"),
        ("validation", "full_validation_failed", "repair_full_validation"),
    ],
)
def test_runner_failures_have_stage_actions_and_publish_nothing(
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failed_stage: str,
    reason_code: str,
    action_code: str,
) -> None:
    paths = _base_repo(tmp_path)
    _init_git(paths["repo"])
    preview_args = [*_args(paths), "--run-date", "2026-07-18"]
    assert main(preview_args) == 3
    preview = json.loads(capsys.readouterr().out)
    before = _snapshot_tree(paths["repo"])

    def passing(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {"status": "passed"}

    def failed(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "failed", "returncode": 0, "reason": f"{failed_stage} failed"}

    runners = OnboardingRunners(
        replay=failed if failed_stage == "replay" else passing,
        contracts=failed if failed_stage == "contracts" else passing,
        full_suite=(lambda _stage_root: failed()) if failed_stage == "validation" else (lambda _stage_root: passing()),
    )
    monkeypatch.setattr("autoscout.source_onboarding_transaction.default_onboarding_runners", lambda: runners)

    assert (
        main(
            [
                *preview_args,
                "--confirmation-token",
                preview["confirmation_token"],
                "--confirmed-by-editor",
            ]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert report["stage"] == failed_stage
    assert report["reason_code"] == reason_code
    assert report["next_action_code"] == action_code
    assert _snapshot_tree(paths["repo"]) == before
    assert (
        subprocess.run(
            ["git", "rev-list", "--count", "HEAD"], cwd=paths["repo"], check=True, capture_output=True, text=True
        ).stdout.strip()
        == "1"
    )


def test_confirmed_validation_failure_leaves_canonical_bundle_unpublished(
    capsys: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _base_repo(tmp_path)
    _init_git(paths["repo"])
    preview_args = [*_args(paths), "--run-date", "2026-07-18"]
    assert main(preview_args) == 3
    preview = json.loads(capsys.readouterr().out)
    before = {key: paths[key].read_bytes() for key in ("portfolio", "pilot", "pilot_live", "live")}
    runners = OnboardingRunners(
        replay=lambda *_args, **_kwargs: {
            "status": "failed",
            "returncode": 0,
            "reason": "replay fixture is invalid",
        },
        contracts=lambda *_args, **_kwargs: {"status": "passed"},
        full_suite=lambda *_args, **_kwargs: {"status": "passed"},
    )
    monkeypatch.setattr("autoscout.source_onboarding_transaction.default_onboarding_runners", lambda: runners)

    assert main([*preview_args, "--confirmation-token", preview["confirmation_token"], "--confirmed-by-editor"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["stage"] == "replay"
    assert {key: paths[key].read_bytes() for key in before} == before
    assert not list((paths["repo"] / "data" / "current-weekly-pilot").glob("*.json"))
    assert not (paths["repo"] / "docs" / "issue-339-source-feasibility.md").exists()
    assert (
        subprocess.run(
            ["git", "rev-list", "--count", "HEAD"], cwd=paths["repo"], check=True, capture_output=True, text=True
        ).stdout.strip()
        == "1"
    )


def test_unrelated_dirty_path_blocks_confirmation_without_further_mutation(capsys: Any, tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)
    _init_git(paths["repo"])
    preview_args = [*_args(paths), "--run-date", "2026-07-18"]
    assert main(preview_args) == 3
    preview = json.loads(capsys.readouterr().out)
    before = _snapshot_tree(paths["repo"])
    unrelated = paths["repo"] / "unrelated.txt"
    unrelated.write_text("user change\n", encoding="utf-8")

    assert main([*preview_args, "--confirmation-token", preview["confirmation_token"], "--confirmed-by-editor"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "unrelated_dirty_files"
    assert _snapshot_tree(paths["repo"]) == before | {Path("unrelated.txt"): ("file", b"user change\n", False, 1)}
    assert not list((paths["repo"] / "data" / "current-weekly-pilot").glob("*.json"))


def test_repeated_confirmed_onboarding_stops_after_acceptance(
    capsys: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _base_repo(tmp_path)
    _init_git(paths["repo"])
    _git(paths["repo"], "commit", "--allow-empty", "-qm", "auto-scout: onboard source Other (#400) operation=other")
    preview_args = [*_args(paths), "--run-date", "2026-07-18"]
    assert main(preview_args) == 3
    preview = json.loads(capsys.readouterr().out)
    runners = OnboardingRunners(
        replay=lambda *_args, **_kwargs: {"status": "passed"},
        contracts=lambda *_args, **_kwargs: {"status": "passed"},
        full_suite=lambda *_args, **_kwargs: {"status": "passed"},
    )
    monkeypatch.setattr("autoscout.source_onboarding_transaction.default_onboarding_runners", lambda: runners)
    confirmation = [
        *preview_args,
        "--confirmation-token",
        preview["confirmation_token"],
        "--confirmed-by-editor",
    ]
    assert main(confirmation) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["commit"]["committed"] is True
    assert main(confirmation) == 1
    second = json.loads(capsys.readouterr().out)
    assert second["reason_code"] == "proposal_already_accepted"
    assert (
        subprocess.run(
            ["git", "rev-list", "--count", "HEAD"], cwd=paths["repo"], check=True, capture_output=True, text=True
        ).stdout.strip()
        == "3"
    )


def test_explicit_source_specific_test_is_staged_and_committed(
    capsys: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _base_repo(tmp_path)
    _init_git(paths["repo"])
    (paths["repo"] / "tests").mkdir()
    source_test = paths["repo"] / "tests" / "test_source_issue339.py"
    source_test.write_text(
        '"""Issue #401 source-specific checks."""\n\n\ndef test_source_specific_marker():\n    assert True\n',
        encoding="utf-8",
    )
    args = [*_args(paths), "--source-specific-test", str(source_test), "--run-date", "2026-07-18"]
    assert main(args) == 3
    preview = json.loads(capsys.readouterr().out)
    runners = OnboardingRunners(
        replay=lambda *_args, **_kwargs: {"status": "passed"},
        contracts=lambda *_args, **_kwargs: {"status": "passed"},
        full_suite=lambda *_args, **_kwargs: {"status": "passed"},
    )
    monkeypatch.setattr("autoscout.source_onboarding_transaction.default_onboarding_runners", lambda: runners)
    assert main([*args, "--confirmation-token", preview["confirmation_token"], "--confirmed-by-editor"]) == 0
    capsys.readouterr()
    commit_files = subprocess.run(
        ["git", "show", "--format=", "--name-only", "HEAD"],
        cwd=paths["repo"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert "tests/test_source_issue339.py" in commit_files


def test_add_source_launcher_is_executable_and_uses_offline_canonical_command() -> None:
    launcher = ROOT / "scripts" / "add-source"
    assert launcher.is_file()
    assert launcher.stat().st_mode & 0o111
    text = launcher.read_text(encoding="utf-8")
    assert 'exec "${INTERPRETER}" -m autoscout add-source "$@"' in text
    assert "probe-source" in text
    assert "git push" not in text
    result = subprocess.run([str(launcher), "--help"], cwd=ROOT, check=False, capture_output=True, text=True)
    assert result.returncode == 0
    assert "--confirmation-token" in result.stdout
    assert "--fixture-dir" not in result.stdout


def test_successful_preview_is_completely_side_effect_free(capsys: Any, tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)
    _init_git(paths["repo"])
    before_tree = _snapshot_tree(paths["repo"])
    before_git = _git_snapshot(paths["repo"])

    assert main([*_args(paths), "--run-date", "2026-07-18"]) == 3
    report = json.loads(capsys.readouterr().out)

    assert report["confirmation_required"] is True
    assert report["confirmation_token"]
    assert report["onboarding_request"]["run_date"] == "2026-07-18"
    assert _snapshot_tree(paths["repo"]) == before_tree
    assert _git_snapshot(paths["repo"]) == before_git


def test_confirmation_requires_token_without_mutation(capsys: Any, tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)
    _init_git(paths["repo"])
    preview_args = [*_args(paths), "--run-date", "2026-07-18"]
    assert main(preview_args) == 3
    capsys.readouterr()
    before = _snapshot_tree(paths["repo"])

    assert main([*preview_args, "--confirmed-by-editor"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "confirmation_token_required"
    assert report["next_action_code"] == "provide_confirmation_token"
    assert _snapshot_tree(paths["repo"]) == before


def test_confirmation_token_is_bound_to_the_complete_request(capsys: Any, tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)
    _init_git(paths["repo"])
    preview_args = [*_args(paths), "--run-date", "2026-07-18"]
    assert main(preview_args) == 3
    preview = json.loads(capsys.readouterr().out)
    before = _snapshot_tree(paths["repo"])

    changed = list(preview_args)
    changed[changed.index("The accepted list is a stable adult nonfiction signal.")] = "Reason B."
    assert (
        main(
            [
                *changed,
                "--run-date",
                "2026-07-18",
                "--confirmation-token",
                preview["confirmation_token"],
                "--confirmed-by-editor",
            ]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "confirmation_payload_mismatch"
    assert _snapshot_tree(paths["repo"]) == before


def test_failure_reports_have_concrete_distinct_actions(capsys: Any, tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)
    assert main([*_args(paths), "--slug", "../escape"]) == 1
    slug_report = json.loads(capsys.readouterr().out)
    assert main([*_args(paths), "--helper-metadata", str(paths["repo"] / "missing.json")]) == 1
    input_report = json.loads(capsys.readouterr().out)

    assert slug_report["reason_code"] == "invalid_slug"
    assert slug_report["next_action_code"] == "fix_slug"
    assert input_report["reason_code"] == "helper_metadata_missing"
    assert input_report["next_action_code"] == "provide_helper_metadata"
    assert slug_report["next_action"] != input_report["next_action"]
    assert slug_report["next_action_code"] != input_report["next_action_code"]
    assert "Resolve the reported onboarding failure before retrying." not in {
        slug_report["next_action"],
        input_report["next_action"],
    }


def test_arbitrary_in_repository_probe_evidence_is_rejected_without_mutation(capsys: Any, tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)
    arbitrary = paths["repo"] / "misc" / "probe.json"
    arbitrary.parent.mkdir(parents=True)
    arbitrary.write_bytes(paths["evidence"].read_bytes())
    args = [str(arbitrary) if value == str(paths["evidence"]) else value for value in _args(paths)]
    before = _snapshot_tree(paths["repo"])

    assert main([*args, "--run-date", "2026-07-18"]) == 3
    report = json.loads(capsys.readouterr().out)
    assert report["confirmation_required"] is True
    assert _snapshot_tree(paths["repo"]) == before


def test_probe_defer_has_terminal_exit_distinct_from_confirmation(capsys: Any, tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)
    _write_json(paths["evidence"], _evidence("defer"))
    assert main(_args(paths)) == 4
    defer_report = json.loads(capsys.readouterr().out)
    assert defer_report["outcome"] == "defer"
    assert defer_report["exit_code"] == 4

    paths = _base_repo(tmp_path / "accepted")
    assert main([*_args(paths), "--run-date", "2026-07-18"]) == 3
    confirmation_report = json.loads(capsys.readouterr().out)
    assert confirmation_report["outcome"] == "confirmation_required"
    assert confirmation_report["exit_code"] == 3


def test_already_accepted_proposal_stops_before_confirmation_or_mutation(capsys: Any, tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)
    proposal, _ = record_source_proposal(
        paths["proposal"].parent,
        source_url=URL,
        proposed_by="editor",
        reason="Already onboarded.",
        source_name=NAME,
        suggested_signal_family="award",
        status="accepted",
    )
    export_json(paths["proposal"].parent)
    _init_git(paths["repo"])
    before = _snapshot_tree(paths["repo"])

    assert main([*_args(paths), "--run-date", "2026-07-18"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "proposal_already_accepted"
    assert _snapshot_tree(paths["repo"]) == before
    assert proposal["status"] == "accepted"

    assert (
        main(
            [
                *_args(paths),
                "--run-date",
                "2026-07-18",
                "--confirmation-token",
                "not-the-preview-token",
                "--confirmed-by-editor",
            ]
        )
        == 1
    )
    confirmation_report = json.loads(capsys.readouterr().out)
    assert confirmation_report["reason_code"] == "proposal_already_accepted"
    assert _snapshot_tree(paths["repo"]) == before


def test_default_validation_adapters_run_against_minimal_staged_contract_files(capsys: Any, tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)
    tests_dir = paths["repo"] / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_source_onboarding_contract.py").write_text(
        "def test_minimal_onboarding_contract():\n    assert True\n", encoding="utf-8"
    )
    (tests_dir / "test_exact_book_list_source_contract.py").write_text(
        "def test_minimal_exact_book_list_contract():\n    assert True\n", encoding="utf-8"
    )
    _init_git(paths["repo"])
    preview_args = [*_args(paths), "--run-date", "2026-07-18"]
    assert main(preview_args) == 3
    preview = json.loads(capsys.readouterr().out)

    assert (
        main(
            [
                *preview_args,
                "--confirmation-token",
                preview["confirmation_token"],
                "--confirmed-by-editor",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert [
        stage["stage"] for stage in report["stages"] if stage["stage"] in {"replay", "contracts", "validation"}
    ] == [
        "replay",
        "contracts",
        "validation",
    ]
    assert all(
        stage["outcome"] == "passed"
        for stage in report["stages"]
        if stage["stage"] in {"replay", "contracts", "validation"}
    )
    assert report["commit"]["committed"] is True
    assert (
        subprocess.run(
            ["git", "rev-list", "--count", "HEAD"], cwd=paths["repo"], check=True, capture_output=True, text=True
        ).stdout.strip()
        == "2"
    )


def test_confirmed_path_can_use_injected_validation_and_commits_exact_bundle(
    capsys: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _base_repo(tmp_path)
    _init_git(paths["repo"])
    preview_code = main([*_args(paths), "--run-date", "2026-07-18"])
    assert preview_code == 3
    preview = json.loads(capsys.readouterr().out)

    runners = OnboardingRunners(
        replay=lambda *_args, **_kwargs: {"status": "passed"},
        contracts=lambda *_args, **_kwargs: {"status": "passed"},
        full_suite=lambda *_args, **_kwargs: {"status": "passed"},
    )
    monkeypatch.setattr("autoscout.source_onboarding_transaction.default_onboarding_runners", lambda: runners)

    code = main(
        [
            *_args(paths),
            "--run-date",
            "2026-07-18",
            "--confirmation-token",
            preview["confirmation_token"],
            "--confirmed-by-editor",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["outcome"] == "completed"
    assert report["exit_code"] == 0
    assert report["commit"]["committed"] is True
    status = subprocess.run(
        ["git", "status", "--short"], cwd=paths["repo"], check=False, capture_output=True, text=True
    )
    assert status.stdout == ""
    commit_files = subprocess.run(
        ["git", "show", "--format=", "--name-only", "HEAD"],
        cwd=paths["repo"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert {
        "configs/current-weekly-portfolio.json",
        "configs/current-weekly-pilot.json",
        "configs/current-weekly-pilot-live.json",
        "configs/current-weekly-live.json",
        "runs/current-weekly-pilot-russian/source-proposals.json",
        "data/current-weekly-pilot/issue-339-example-list-helper-metadata.json",
        "docs/issue-401-example-source-feasibility.md",
    }.issubset(set(commit_files))
    fixture_files = {
        path for path in commit_files if path.startswith("data/current-weekly-pilot/") and "helper-metadata" not in path
    }
    assert len(fixture_files) == 2
    assert any(
        path.startswith("data/current-weekly-pilot/codex-capture-issue-339-example-list-") for path in fixture_files
    )
    assert any(path.endswith("-codex-extraction.json") for path in fixture_files)
    assert (
        subprocess.run(
            ["git", "show", "-s", "--format=%an", "HEAD"],
            cwd=paths["repo"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "Test"
    )
    assert not any(
        "Co-authored-by" in line
        for line in subprocess.run(
            ["git", "show", "-s", "--format=%B", "HEAD"], cwd=paths["repo"], check=True, capture_output=True, text=True
        ).stdout.splitlines()
    )


_PASSING_RUNNERS = OnboardingRunners(
    replay=lambda *_args, **_kwargs: {"status": "passed"},
    contracts=lambda *_args, **_kwargs: {"status": "passed"},
    full_suite=lambda *_args, **_kwargs: {"status": "passed"},
)


def _committed_pin(paths: dict[str, Path], relative: str = "tests/test_portfolio_size.py") -> Path:
    """Commit a composition pin that holds for the one-source base portfolio."""
    pin = paths["repo"] / relative
    pin.parent.mkdir(parents=True, exist_ok=True)
    pin.write_text(_portfolio_size_pin(1), encoding="utf-8")
    return pin


def _portfolio_size_pin(size: int) -> str:
    return (
        "import json\n"
        "from pathlib import Path\n\n\n"
        "def test_portfolio_size():\n"
        '    portfolio = Path(__file__).resolve().parents[1] / "configs" / "current-weekly-portfolio.json"\n'
        f'    assert len(json.loads(portfolio.read_text(encoding="utf-8"))["sources"]) == {size}\n'
    )


def _head_files(repo: Path) -> set[str]:
    shown = subprocess.run(
        ["git", "show", "--format=", "--name-only", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    )
    return set(shown.stdout.splitlines())


def test_declared_composition_pin_is_validated_and_committed_with_the_bundle(
    capsys: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #664: one confirmed invocation lands a source whose arrival moves a pin."""
    paths = _base_repo(tmp_path)
    pin = _committed_pin(paths)
    pin.chmod(0o755)
    _init_git(paths["repo"])
    pin.write_text(_portfolio_size_pin(2), encoding="utf-8")
    args = [*_args(paths), "--onboarding-artifact", str(pin)]
    assert main(args) == 3
    preview = json.loads(capsys.readouterr().out)
    identity = preview["onboarding_request"]["artifact_identity"]["onboarding_artifacts"]
    assert identity == [{"path": str(pin.resolve()), "sha256": hashlib.sha256(pin.read_bytes()).hexdigest()}]

    def full_suite(stage_root: Path) -> Mapping[str, Any]:
        # The real staged run: the moved pin and the new source are judged together.
        from autoscout.source_onboarding_transaction import pytest_result

        return pytest_result(stage_root, ("tests/test_portfolio_size.py",))

    runners = OnboardingRunners(_PASSING_RUNNERS.replay, _PASSING_RUNNERS.contracts, full_suite)
    monkeypatch.setattr("autoscout.source_onboarding_transaction.default_onboarding_runners", lambda: runners)

    assert main([*args, "--confirmation-token", preview["confirmation_token"], "--confirmed-by-editor"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["commit"]["committed"] is True
    assert "tests/test_portfolio_size.py" in report["commit"]["paths"]
    assert "tests/test_portfolio_size.py" in _head_files(paths["repo"])
    assert pin.read_text(encoding="utf-8") == _portfolio_size_pin(2)
    assert pin.stat().st_mode & 0o777 == 0o755
    tracked = subprocess.run(
        ["git", "ls-files", "--stage", "tests/test_portfolio_size.py"],
        cwd=paths["repo"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert tracked.stdout.startswith("100755 ")
    status = subprocess.run(
        ["git", "status", "--short"], cwd=paths["repo"], check=False, capture_output=True, text=True
    )
    assert status.stdout == ""


def test_undeclared_pin_change_still_blocks_confirmation(capsys: Any, tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)
    pin = _committed_pin(paths)
    other = _committed_pin(paths, "tests/test_other_pin.py")
    _init_git(paths["repo"])
    pin.write_text(_portfolio_size_pin(2), encoding="utf-8")
    other.write_text(_portfolio_size_pin(3), encoding="utf-8")
    args = [*_args(paths), "--onboarding-artifact", str(pin)]
    assert main(args) == 3
    preview = json.loads(capsys.readouterr().out)
    before = _snapshot_tree(paths["repo"])

    assert main([*args, "--confirmation-token", preview["confirmation_token"], "--confirmed-by-editor"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "unrelated_dirty_files"
    assert "tests/test_other_pin.py" in report["reason"]
    assert "tests/test_portfolio_size.py" not in report["reason"]
    assert "--onboarding-artifact" in report["next_action"]
    assert _snapshot_tree(paths["repo"]) == before


@pytest.mark.parametrize("mode_only_change", [False, True])
def test_unchanged_onboarding_artifact_blocks_confirmation(capsys: Any, tmp_path: Path, mode_only_change: bool) -> None:
    paths = _base_repo(tmp_path)
    pin = _committed_pin(paths)
    _init_git(paths["repo"])
    if mode_only_change:
        pin.chmod(0o755)
    args = [*_args(paths), "--onboarding-artifact", str(pin)]
    assert main(args) == 3
    preview = json.loads(capsys.readouterr().out)
    before = _snapshot_tree(paths["repo"])

    assert main([*args, "--confirmation-token", preview["confirmation_token"], "--confirmed-by-editor"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "onboarding_artifact_not_dirty"
    assert report["next_action_code"] == "drop_unchanged_onboarding_artifact"
    assert _snapshot_tree(paths["repo"]) == before


def test_onboarding_artifact_bytes_and_set_are_bound_to_the_confirmation_token(capsys: Any, tmp_path: Path) -> None:
    paths = _base_repo(tmp_path)
    pin = _committed_pin(paths)
    _init_git(paths["repo"])
    pin.write_text(_portfolio_size_pin(2), encoding="utf-8")
    declared = [*_args(paths), "--onboarding-artifact", str(pin)]
    assert main(declared) == 3
    preview = json.loads(capsys.readouterr().out)
    assert main(_args(paths)) == 3
    undeclared = json.loads(capsys.readouterr().out)
    assert undeclared["confirmation_token"] != preview["confirmation_token"]
    confirmation = ["--confirmation-token", preview["confirmation_token"], "--confirmed-by-editor"]

    assert main([*_args(paths), *confirmation]) == 1
    assert json.loads(capsys.readouterr().out)["reason_code"] == "confirmation_payload_mismatch"
    pin.write_text(_portfolio_size_pin(3), encoding="utf-8")
    assert main([*declared, *confirmation]) == 1
    assert json.loads(capsys.readouterr().out)["reason_code"] == "confirmation_payload_mismatch"
    commits = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"], cwd=paths["repo"], check=True, capture_output=True, text=True
    )
    assert commits.stdout.strip() == "1"


@pytest.mark.parametrize(
    "relative_path",
    [
        "autoscout/module.py",
        "configs/current-weekly-portfolio.json",
        "unrelated.txt",
        "tests",
        "tests/missing_pin.py",
        "tests/linked_pin.py",
        "tests/test_portfolio_size.py:tests/test_portfolio_size.py",
        "tests/test_source_issue339.py",
    ],
)
def test_public_cli_rejects_invalid_onboarding_artifacts(capsys: Any, tmp_path: Path, relative_path: str) -> None:
    paths = _base_repo(tmp_path)
    repo = paths["repo"]
    _committed_pin(paths)
    (repo / "autoscout").mkdir()
    (repo / "autoscout" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "unrelated.txt").write_text("user change\n", encoding="utf-8")
    (repo / "tests" / "linked_pin.py").symlink_to(repo / "tests" / "test_portfolio_size.py")
    source_test = repo / "tests" / "test_source_issue339.py"
    source_test.write_text('"""Issue #401 source-specific checks."""\n', encoding="utf-8")
    before = _snapshot_files(repo)
    args = [*_args(paths), "--source-specific-test", str(source_test)]
    for artifact in relative_path.split(":"):
        args.extend(["--onboarding-artifact", str(repo / artifact)])

    assert main(args) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "onboarding_artifact_invalid"
    assert report["next_action_code"] == "fix_onboarding_artifact"
    assert _snapshot_files(repo) == before
    assert not list(repo.glob("**/source-proposals.json"))


@pytest.mark.parametrize("claimed", ["evidence", "helper", "feasibility", "proposal"])
def test_onboarding_artifact_cannot_be_another_onboarding_input(capsys: Any, tmp_path: Path, claimed: str) -> None:
    """Probe evidence and the other inputs never enter the commit as a declared artifact."""
    paths = _base_repo(tmp_path)
    repo = paths["repo"]
    moved = {
        "evidence": repo / "tests" / "source-candidate-captures" / "issue-339-retained.json",
        "helper": repo / "tests" / "helper.json",
        "feasibility": repo / "tests" / "feasibility-draft.md",
        "proposal": repo / "tests" / "source-proposals.json",
    }[claimed]
    moved.parent.mkdir(parents=True, exist_ok=True)
    if claimed == "proposal":
        moved.write_text("[]\n", encoding="utf-8")
    else:
        paths[claimed].rename(moved)
    paths[claimed] = moved
    before = _snapshot_files(repo)

    assert main([*_args(paths), "--onboarding-artifact", str(moved)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "onboarding_artifact_invalid"
    assert _snapshot_files(repo) == before


def test_validation_cannot_mutate_a_declared_onboarding_artifact(
    capsys: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = _base_repo(tmp_path)
    pin = _committed_pin(paths)
    _init_git(paths["repo"])
    pin.write_text(_portfolio_size_pin(2), encoding="utf-8")
    args = [*_args(paths), "--onboarding-artifact", str(pin)]
    assert main(args) == 3
    preview = json.loads(capsys.readouterr().out)
    before = _snapshot_files(paths["repo"])

    def mutate(_stage_root: Path, staged_paths: Mapping[str, Any], _source: Mapping[str, Any]) -> dict[str, str]:
        (staged_pin,) = staged_paths["onboarding_artifacts"]
        staged_pin.write_text(_portfolio_size_pin(9), encoding="utf-8")
        return {"status": "passed"}

    runners = OnboardingRunners(mutate, _PASSING_RUNNERS.contracts, _PASSING_RUNNERS.full_suite)
    monkeypatch.setattr("autoscout.source_onboarding_transaction.default_onboarding_runners", lambda: runners)

    assert main([*args, "--confirmation-token", preview["confirmation_token"], "--confirmed-by-editor"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "canonical_mutation"
    assert _snapshot_files(paths["repo"]) == before
