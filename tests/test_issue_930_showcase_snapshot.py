"""Issue #930: the public showcase snapshot never carries the editor's words.

Every test builds a small throwaway repository with one private note in its
state and checks where that note can and cannot end up.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from showcase_snapshot_support import NOTE
from showcase_snapshot_support import PROFILE_QUOTE
from showcase_snapshot_support import TAILNET_HOST
from showcase_snapshot_support import snapshot_files as _files
from showcase_snapshot_support import snapshot_repo as _repo
from showcase_snapshot_support import write_file as _write

from autoscout import showcase_snapshot
from autoscout.showcase_snapshot import WITHHELD_NOTE
from autoscout.showcase_snapshot import SnapshotLeakError
from autoscout.showcase_snapshot import build_snapshot


def test_a_fixture_with_a_real_note_is_dropped_with_the_tests_that_need_it(tmp_path: Path) -> None:
    output = tmp_path / "out"
    report = build_snapshot(_repo(tmp_path), output, source_commit="abc123")

    files = _files(output)
    assert "tests/fixtures/real-run/history.json" not in files
    assert "tests/fixtures/real-run/capture.html" not in files
    assert "tests/real_run_support.py" not in files
    assert "tests/test_real.py" not in files
    assert report.dropped["tests/test_real.py"] == "imports dropped module real_run_support"
    assert "tests/fixtures/synthetic/history.json" in files
    assert "tests/test_synthetic.py" in files


def test_the_rule_matching_fixtures_stay_private_without_the_live_state(tmp_path: Path) -> None:
    # The #1165 fixtures copy the editor's profile and history. No live
    # state holds their words here, so only the named exclusion keeps them out.
    repo = _repo(tmp_path)
    private = (
        "tests/fixtures/issue-1165-rule-matching/editorial-profile.json",
        "tests/fixtures/issue-1165-rule-matching/candidate-history.json",
        "tests/fixtures/issue-1165-rule-matching.provenance.md",
        "tests/test_issue_1165_prepared_rule_matcher.py",
    )
    for relative in private:
        _write(repo, relative, "{}\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    output = tmp_path / "out"
    build_snapshot(repo, output, source_commit="abc123")

    files = _files(output)
    assert not files.intersection(private)
    assert "tests/test_synthetic.py" in files


def test_a_quote_in_code_or_docs_is_withheld_in_place(tmp_path: Path) -> None:
    output = tmp_path / "out"
    build_snapshot(_repo(tmp_path), output, source_commit="abc123")

    audit = (output / "autoscout/audit.py").read_text(encoding="utf-8")
    assert NOTE not in audit
    assert WITHHELD_NOTE in audit
    assert 'OTHER = "kept"' in audit
    assert PROFILE_QUOTE not in (output / "docs/research.md").read_text(encoding="utf-8")


def test_infrastructure_addresses_are_redacted(tmp_path: Path) -> None:
    output = tmp_path / "out"
    report = build_snapshot(_repo(tmp_path), output, source_commit="abc123")

    assert TAILNET_HOST not in (output / "docs/ops.md").read_text(encoding="utf-8")
    assert "docs/ops.md" in report.redacted


def test_only_allow_listed_paths_are_copied(tmp_path: Path) -> None:
    output = tmp_path / "out"
    build_snapshot(_repo(tmp_path), output, source_commit="abc123")

    files = _files(output)
    assert not any(name.startswith(("runs/", "ru-sources/")) for name in files)
    assert "SNAPSHOT.md" in files
    assert "abc123" in (output / "SNAPSHOT.md").read_text(encoding="utf-8")
    assert (output / "README.md").read_text(encoding="utf-8").startswith("> **Public snapshot.**")


def test_the_build_is_deterministic(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first, second = tmp_path / "first", tmp_path / "second"
    build_snapshot(repo, first, source_commit="abc123")
    build_snapshot(repo, second, source_commit="abc123")

    assert _files(first) == _files(second)
    for name in _files(first):
        assert (first / name).read_bytes() == (second / name).read_bytes(), name


def test_the_final_gate_fails_closed_when_a_filter_misses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(showcase_snapshot, "redact", lambda text, fragments: text)

    with pytest.raises(SnapshotLeakError, match="autoscout/audit.py: editor-authored text"):
        build_snapshot(_repo(tmp_path), tmp_path / "out", source_commit="abc123")


def _bare_target(tmp_path: Path) -> Path:
    target = tmp_path / "public.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(target)], check=True)
    return target


def _commit_all(repo: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-q", "-m", "init"],
        cwd=repo,
        check=True,
    )


def _published_files(target: Path) -> set[str]:
    listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "main"], cwd=target, capture_output=True, text=True
    )
    return set(listing.stdout.split()) if listing.returncode == 0 else set()


@pytest.fixture
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(key, "showcase-test")
    for key in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, "showcase@example.test")
    monkeypatch.setattr(showcase_snapshot.shutil, "which", lambda name: None)


@pytest.mark.usefixtures("_git_identity")
def test_a_preview_pushes_nothing(tmp_path: Path) -> None:
    repo, target = _repo(tmp_path), _bare_target(tmp_path)
    _commit_all(repo)

    lines = showcase_snapshot.publish_snapshot(repo, str(target), tmp_path / "work", push=False)

    assert any(line.startswith("preview only") for line in lines)
    assert _published_files(target) == set()


@pytest.mark.usefixtures("_git_identity")
def test_push_publishes_the_snapshot_and_a_repeat_is_a_no_op(tmp_path: Path) -> None:
    repo, target = _repo(tmp_path), _bare_target(tmp_path)
    _commit_all(repo)

    showcase_snapshot.publish_snapshot(repo, str(target), tmp_path / "work", push=True)
    published = _published_files(target)
    again = showcase_snapshot.publish_snapshot(repo, str(target), tmp_path / "work2", push=True)

    assert "autoscout/audit.py" in published
    assert "SNAPSHOT.md" in published
    assert not any(name.startswith("runs/") for name in published)
    assert any("nothing to publish" in line for line in again)


@pytest.mark.usefixtures("_git_identity")
def test_uncommitted_changes_refuse_to_publish(tmp_path: Path) -> None:
    repo, target = _repo(tmp_path), _bare_target(tmp_path)
    _commit_all(repo)
    _write(repo, "README.md", "# Tool, edited\n")

    with pytest.raises(showcase_snapshot.PublishRefusedError, match="uncommitted"):
        showcase_snapshot.publish_snapshot(repo, str(target), tmp_path / "work", push=True)


RESTRICTED_LINE = (
    "The tool therefore screens for publicly visible signals of those subjects: the war in Ukraine, "
    "LGBT subject matter and gender transition, and a small number of other categories fixed in the "
    "configuration."
)


def test_the_public_readme_does_not_name_restricted_subjects(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "README.md", f"# Tool\n\n{RESTRICTED_LINE}\n")
    output = tmp_path / "out"

    build_snapshot(repo, output, source_commit="abc123")

    readme = (output / "README.md").read_text(encoding="utf-8")
    assert "LGBT" not in readme
    assert "a short list of restricted categories" in readme


def test_an_unmatched_readme_rewrite_fails_the_build(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "README.md", "# Tool\n\nIt screens for LGBT subject matter, reworded.\n")

    with pytest.raises(SnapshotLeakError, match="README.md: names a restricted subject"):
        build_snapshot(repo, tmp_path / "out", source_commit="abc123")
