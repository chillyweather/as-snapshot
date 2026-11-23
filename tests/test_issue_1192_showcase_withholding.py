"""Issue #1192: the public snapshot withholds the live source setup and PW/PM content.

Each test adds files to the #930 throwaway repository and checks whether they
reach the snapshot, and that the final gate catches them when a filter misses.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from showcase_snapshot_support import snapshot_files
from showcase_snapshot_support import snapshot_repo
from showcase_snapshot_support import write_file

from autoscout import showcase_snapshot
from autoscout.showcase_snapshot import PUBLISHED_CONFIGS
from autoscout.showcase_snapshot import SnapshotLeakError
from autoscout.showcase_snapshot import build_snapshot

LIVE_CONFIG = "configs/current-weekly-live.json"
NEW_CONFIG = "configs/added-later.json"
PW_FIXTURE = "tests/fixtures/pw-run/capture.json"
PW_SIBLING = "tests/fixtures/pw-run/notes.md"
PW_TEST = "tests/test_pw_run.py"


def _add(repo: Path, files: dict[str, str]) -> Path:
    for relative, text in files.items():
        write_file(repo, relative, text)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    return repo


def _with_configs(tmp_path: Path) -> Path:
    configs = {name: "{}\n" for name in PUBLISHED_CONFIGS}
    configs[LIVE_CONFIG] = '{"sources": ["live"]}\n'
    configs[NEW_CONFIG] = "{}\n"
    return _add(snapshot_repo(tmp_path), configs)


def _with_research(tmp_path: Path) -> Path:
    return _add(
        snapshot_repo(tmp_path),
        {
            "docs/research/source-options.md": "Which sources to configure.\n",
            "docs/source-quality/yield.md": "Source yield.\n",
            "docs/issue-289-french-source-feasibility.md": "Feasibility.\n",
            "docs/glossary-notes.md": "An ordinary doc.\n",
        },
    )


def _with_pw_fixture(tmp_path: Path, capture: str) -> Path:
    return _add(
        snapshot_repo(tmp_path),
        {
            PW_FIXTURE: capture,
            PW_SIBLING: "A retained run.\n",
            PW_TEST: 'FIXTURE = "tests/fixtures/pw-run"\n',
        },
    )


def test_only_the_listed_configs_are_published(tmp_path: Path) -> None:
    output = tmp_path / "out"
    build_snapshot(_with_configs(tmp_path), output, source_commit="abc123")

    published = {name for name in snapshot_files(output) if name.startswith("configs/")}
    assert published == set(PUBLISHED_CONFIGS)


def test_source_research_and_feasibility_reports_are_not_published(tmp_path: Path) -> None:
    output = tmp_path / "out"
    build_snapshot(_with_research(tmp_path), output, source_commit="abc123")

    files = snapshot_files(output)
    assert not any(name.startswith(("docs/research/", "docs/source-quality/")) for name in files)
    assert "docs/issue-289-french-source-feasibility.md" not in files
    assert "docs/glossary-notes.md" in files
    assert "docs/research.md" in files


@pytest.mark.parametrize(
    "capture",
    [
        json.dumps({"url": "https://www.PublishersWeekly.com/pw/by-topic/review.html"}),
        json.dumps({"url": "https://www.publishersmarketplace.com/deals/"}),
        # A JSON escape must not hide the domain.
        '{"url": "https:\\/\\/www.publishersweekly\\u002ecom\\/pw\\/review.html"}',
    ],
    ids=["pw", "pm", "json-escaped"],
)
def test_a_fixture_with_licensed_content_is_dropped_with_the_tests_that_need_it(tmp_path: Path, capture: str) -> None:
    output = tmp_path / "out"
    report = build_snapshot(_with_pw_fixture(tmp_path, capture), output, source_commit="abc123")

    files = snapshot_files(output)
    assert PW_FIXTURE not in files
    assert PW_SIBLING not in files
    assert PW_TEST not in files
    assert report.dropped[PW_FIXTURE] == "fixture contains Publishers Weekly or Publishers Marketplace content"
    assert report.dropped[PW_TEST] == "uses dropped fixture pw-run"
    assert "tests/fixtures/synthetic/history.json" in files
    assert "tests/test_synthetic.py" in files


def test_the_final_gate_fails_on_a_private_config_when_selection_misses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _with_configs(tmp_path)
    selected = showcase_snapshot.allowed_files
    monkeypatch.setattr(showcase_snapshot, "allowed_files", lambda root: [*selected(root), LIVE_CONFIG])

    with pytest.raises(SnapshotLeakError, match=f"{LIVE_CONFIG}: outside the published allow-list"):
        build_snapshot(repo, tmp_path / "out", source_commit="abc123")


def test_the_final_gate_fails_on_source_research_when_selection_misses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _with_research(tmp_path)
    research = "docs/research/source-options.md"
    selected = showcase_snapshot.allowed_files
    monkeypatch.setattr(showcase_snapshot, "allowed_files", lambda root: [*selected(root), research])

    with pytest.raises(SnapshotLeakError, match=f"{research}: outside the published allow-list"):
        build_snapshot(repo, tmp_path / "out", source_commit="abc123")


def test_the_final_gate_fails_on_a_licensed_fixture_when_the_filter_misses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _with_pw_fixture(tmp_path, json.dumps({"url": "https://www.publishersweekly.com/pw/review.html"}))
    reason = showcase_snapshot.fixture_drop_reason
    monkeypatch.setattr(
        showcase_snapshot,
        "fixture_drop_reason",
        lambda name, text, fragments: None if name == PW_FIXTURE else reason(name, text, fragments),
    )

    with pytest.raises(SnapshotLeakError, match=f"{PW_FIXTURE}: fixture with licensed content"):
        build_snapshot(repo, tmp_path / "out", source_commit="abc123")


def test_the_report_counts_dropped_test_modules(tmp_path: Path) -> None:
    repo = _with_pw_fixture(tmp_path, json.dumps({"url": "https://www.publishersweekly.com/"}))
    output, report_path = tmp_path / "out", tmp_path / "report.json"

    report = build_snapshot(repo, output, source_commit="abc123")

    # real_run_support.py and test_real.py fall with the editor-note fixture,
    # test_pw_run.py with the licensed one.
    assert report.dropped_test_modules == 3
    showcase_snapshot._write_report(report_path, report)
    assert json.loads(report_path.read_text(encoding="utf-8"))["dropped_test_modules"] == 3


def _committed_repo_with_pw_fixture(tmp_path: Path) -> Path:
    repo = _with_pw_fixture(tmp_path, json.dumps({"url": "https://www.publishersweekly.com/"}))
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-q", "-m", "init"],
        cwd=repo,
        check=True,
    )
    return repo


def test_the_build_summary_carries_the_dropped_test_count(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _committed_repo_with_pw_fixture(tmp_path)

    exit_code = showcase_snapshot.main(["--repo-root", str(repo), "--output-dir", str(tmp_path / "out")])

    assert exit_code == 0
    assert "(3 test modules)" in capsys.readouterr().out


def test_the_publish_preview_carries_the_dropped_test_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _committed_repo_with_pw_fixture(tmp_path)
    target = tmp_path / "public.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(target)], check=True)
    monkeypatch.setattr(showcase_snapshot.shutil, "which", lambda name: None)

    lines = showcase_snapshot.publish_snapshot(repo, str(target), tmp_path / "work", push=False)

    assert "(3 test modules)" in lines[0]
