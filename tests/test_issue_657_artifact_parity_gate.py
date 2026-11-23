"""Offline coverage for issue #657: the artifact parity gate must see the Weekly Review.

PR #656 left the Weekly Review delimiter row one cell wider than the header,
in both the shown and the hidden state. GFM does not recognise such a table,
so every candidate row rendered as raw pipe text. The gate passed because it
compared 14 run-mvp outputs and never rendered or compared weekly-review.md
or weekly-review.html, and because a listed-but-missing artifact printed SKIP
without failing.

The gate compares one stage at a time, and their order is the point:
``render-review`` rewrites ``editor-facing.csv``, so a comparison made after
the render cannot see the file the run wrote.

Every test here is offline and deterministic. The fixture run needs no
network and two render-review runs are byte-identical.
"""

from __future__ import annotations

import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

from autoscout.artifact_parity import ARTIFACTS
from autoscout.artifact_parity import REVIEW_ARTIFACTS
from autoscout.artifact_parity import RUN_ARTIFACTS
from autoscout.artifact_parity import compare_dirs
from autoscout.artifact_parity import compare_stage
from autoscout.artifact_parity import main as parity_main
from autoscout.pipeline import run_pipeline
from autoscout.review_rendering import render_review

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_CONFIG = REPO_ROOT / "configs" / "weekly-mvp-fixture.json"
LAUNCHER = REPO_ROOT / "scripts" / "check-artifact-parity"

# The 14 artifacts the gate compared before this ticket.
LEGACY_ARTIFACTS = (
    "editor-facing.csv",
    "russian-review-queue.csv",
    "scouting-candidates.json",
    "candidate-history.json",
    "source-mentions.json",
    "proposed-corrections.json",
    "memo-queue.json",
    "source-proposals.json",
    "suppressed-candidates.json",
    "internal-exclusions.json",
    "russian-edition-checks.json",
    "source-runs.json",
    "run-summary.json",
    "report-qa.json",
)

# The six run-mvp outputs that were produced but never compared.
NEWLY_LISTED_RUN_ARTIFACTS = (
    "derived-rule-suggestions.json",
    "derived-rule-suggestions.md",
    "hidden-by-rule.json",
    "internal-pre-suppression-candidates.json",
    "source-metrics.json",
    "source-review.md",
)

# Review outputs rendered inside the gate by render-review.
REVIEW_ONLY_ARTIFACTS = (
    "weekly-review.md",
    "weekly-review.html",
    "description-coverage.json",
)


def _build_two_runs(tmp_path: Path) -> tuple[Path, Path]:
    """Run the offline fixture twice and render the review in both directories."""
    baseline, comparison = _build_two_unrendered_runs(tmp_path)
    assert render_review(baseline) == 0
    assert render_review(comparison) == 0
    return baseline, comparison


def _build_two_unrendered_runs(tmp_path: Path) -> tuple[Path, Path]:
    """Run the offline fixture twice and leave the review unrendered."""
    baseline = tmp_path / "baseline-run"
    comparison = tmp_path / "comparison-run"
    run_pipeline(FIXTURE_CONFIG, baseline)
    run_pipeline(FIXTURE_CONFIG, comparison)
    return baseline, comparison


def _produced_names(directory: Path) -> set[str]:
    return {item.name for item in directory.iterdir()}


def _corrupt_delimiter_row(review_path: Path) -> None:
    """Reintroduce the PR #656 defect: delimiter row one cell wider than the header."""
    lines = review_path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.startswith("| # |"):
            assert (index + 1) < len(lines)
            delimiter = lines[index + 1]
            assert delimiter.endswith("|")
            lines[index + 1] = delimiter + "--- |"
            assert _cell_count(lines[index + 1]) == _cell_count(line) + 1
            break
    else:
        raise AssertionError("candidate table header not found")
    review_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _cell_count(row: str) -> int:
    return len(row.strip().strip("|").split("|"))


def test_gate_lists_review_files_and_six_unlisted_run_artifacts() -> None:
    for name in (*LEGACY_ARTIFACTS, *NEWLY_LISTED_RUN_ARTIFACTS, *REVIEW_ONLY_ARTIFACTS):
        assert name in ARTIFACTS, f"{name} must be compared by the gate"


def test_editor_csv_belongs_to_both_stages() -> None:
    """Two writers produce editor-facing.csv, so each one is its own promise."""
    assert "editor-facing.csv" in RUN_ARTIFACTS
    assert "editor-facing.csv" in REVIEW_ARTIFACTS


def test_stage_lists_match_what_the_two_commands_actually_write(tmp_path: Path) -> None:
    """The listed set and the produced set must stay equal.

    Issue #657 happened because six produced artifacts drifted out of the list
    unnoticed. Comparing the two sets in both directions is what stops the next
    new artifact from escaping the gate the same way.
    """
    run_dir = tmp_path / "drift-run"
    run_pipeline(FIXTURE_CONFIG, run_dir)

    assert _produced_names(run_dir) == set(RUN_ARTIFACTS)

    assert render_review(run_dir) == 0

    assert _produced_names(run_dir) == set(ARTIFACTS)
    assert set(REVIEW_ARTIFACTS) <= _produced_names(run_dir)


def test_render_review_rewrites_the_editor_csv_the_run_wrote(tmp_path: Path) -> None:
    """The reason the run stage has to be compared first.

    render-review rebuilds editor-facing.csv from candidate history, so the
    file the run wrote is gone once the review is rendered.
    """
    run_dir = tmp_path / "rewrite-run"
    run_pipeline(FIXTURE_CONFIG, run_dir)
    from_run = (run_dir / "editor-facing.csv").read_bytes()

    assert render_review(run_dir) == 0

    assert (run_dir / "editor-facing.csv").read_bytes() != from_run


def test_run_stage_catches_a_difference_the_review_stage_cannot_see(tmp_path: Path) -> None:
    """A difference in the run's own editor-facing.csv must fail the run stage.

    Rendering the review overwrites that file, so the same difference is gone
    by the time the review stage runs. Comparing only after the render would
    let run-mvp nondeterminism through, which is what this ordering prevents.
    """
    baseline, comparison = _build_two_unrendered_runs(tmp_path)
    csv_path = comparison / "editor-facing.csv"
    csv_path.write_bytes(csv_path.read_bytes() + b"Injected,Row\n")

    run_result = compare_stage(baseline, comparison, "run")
    assert not run_result.ok
    entry = next(item for item in run_result.entries if item.name == "editor-facing.csv")
    assert entry.status == "differ"

    assert render_review(baseline) == 0
    assert render_review(comparison) == 0
    assert compare_stage(baseline, comparison, "review").ok, "the render erases the difference"


def test_gate_passes_on_unmodified_two_run_comparison(tmp_path: Path) -> None:
    baseline, comparison = _build_two_unrendered_runs(tmp_path)

    run_result = compare_stage(baseline, comparison, "run")
    assert run_result.ok
    assert run_result.passed == len(RUN_ARTIFACTS)

    assert render_review(baseline) == 0
    assert render_review(comparison) == 0

    review_result = compare_stage(baseline, comparison, "review")
    assert review_result.ok
    assert review_result.passed == len(REVIEW_ARTIFACTS)


def test_two_render_review_runs_stay_byte_identical(tmp_path: Path) -> None:
    baseline, comparison = _build_two_runs(tmp_path)

    for name in REVIEW_ARTIFACTS:
        assert (baseline / name).read_bytes() == (comparison / name).read_bytes()


def test_missing_listed_artifact_fails_gate(tmp_path: Path) -> None:
    baseline, comparison = _build_two_runs(tmp_path)
    (comparison / "source-metrics.json").unlink()

    result = compare_stage(baseline, comparison, "run")

    assert not result.ok
    assert result.failed >= 1
    entry = next(item for item in result.entries if item.name == "source-metrics.json")
    assert entry.status != "ok"


def test_missing_on_both_sides_fails_gate_instead_of_skip(tmp_path: Path) -> None:
    baseline, comparison = _build_two_runs(tmp_path)
    (baseline / "hidden-by-rule.json").unlink()
    (comparison / "hidden-by-rule.json").unlink()

    result = compare_stage(baseline, comparison, "run")

    assert not result.ok
    entry = next(item for item in result.entries if item.name == "hidden-by-rule.json")
    assert entry.status != "ok"


def test_real_difference_in_each_newly_compared_artifact_fails_gate(tmp_path: Path) -> None:
    for stage, names in (("run", NEWLY_LISTED_RUN_ARTIFACTS), ("review", REVIEW_ONLY_ARTIFACTS)):
        for name in names:
            baseline_dir = tmp_path / f"base-{name}"
            comparison_dir = tmp_path / f"comp-{name}"
            baseline_dir.mkdir(parents=True, exist_ok=True)
            comparison_dir.mkdir(parents=True, exist_ok=True)
            # Seed both sides with identical bytes for every artifact, then corrupt one.
            for artifact in ARTIFACTS:
                (baseline_dir / artifact).write_bytes(b"same\n")
                (comparison_dir / artifact).write_bytes(b"same\n")
            with (comparison_dir / name).open("ab") as handle:
                handle.write(b"different\n")

            result = compare_stage(baseline_dir, comparison_dir, stage)

            assert not result.ok, f"difference in {name} must fail the {stage} stage"
            entry = next(item for item in result.entries if item.name == name)
            assert entry.status != "ok"


def test_proof_delimiter_defect_fails_gate_while_legacy_list_passes(tmp_path: Path) -> None:
    """Reintroduce the #656 delimiter defect and show the new gate fails.

    The legacy 14-artifact list still passes on the same pair of directories,
    which is exactly the hole this ticket closes. The assertion on the legacy
    list documents the hole; the assertion on the review stage proves the fix.
    """
    baseline, comparison = _build_two_runs(tmp_path)
    _corrupt_delimiter_row(comparison / "weekly-review.md")

    legacy_result = compare_dirs(baseline, comparison, LEGACY_ARTIFACTS)
    assert legacy_result.ok, "legacy list misses the review defect by design"

    result = compare_stage(baseline, comparison, "review")
    assert not result.ok
    entry = next(item for item in result.entries if item.name == "weekly-review.md")
    assert entry.status != "ok"


def test_cli_verdict_cannot_read_passed_while_an_artifact_went_uncompared(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline, comparison = _build_two_runs(tmp_path)
    (comparison / "weekly-review.html").unlink()

    code = parity_main(["--stage", "review", str(baseline), str(comparison)])

    assert code == 1
    out = capsys.readouterr().out
    assert "ARTIFACT PARITY GATE FAILED" in out
    assert "ARTIFACT PARITY GATE PASSED" not in out
    # One line per artifact keeps the report readable.
    compared_lines = [line for line in out.splitlines() if line.strip().startswith(("OK", "FAIL"))]
    assert len(compared_lines) == len(REVIEW_ARTIFACTS)


def test_cli_passes_on_unmodified_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    baseline, comparison = _build_two_runs(tmp_path)

    for stage in ("run", "review"):
        assert parity_main(["--stage", stage, str(baseline), str(comparison)]) == 0
        out = capsys.readouterr().out
        assert f"ARTIFACT PARITY GATE PASSED ({stage} stage)" in out


def test_cli_requires_a_stage(tmp_path: Path) -> None:
    """No default stage, so no call can check a weaker list than it meant to."""
    with pytest.raises(SystemExit) as excinfo:
        parity_main([str(tmp_path / "a"), str(tmp_path / "b")])
    assert excinfo.value.code == 2


def test_compare_stage_rejects_an_unknown_stage(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown parity stage"):
        compare_stage(tmp_path, tmp_path, "everything")


# ── The launcher ───────────────────────────────────────────────────────────


def test_launcher_exists_and_is_executable() -> None:
    assert LAUNCHER.is_file(), f"Launcher missing: {LAUNCHER}"
    assert LAUNCHER.stat().st_mode & stat.S_IXUSR


def test_launcher_has_bash_shebang() -> None:
    first_line = LAUNCHER.read_text(encoding="utf-8").splitlines()[0]
    assert first_line.startswith("#!") and "bash" in first_line


def test_launcher_execs_the_canonical_module_from_the_repository_root(tmp_path: Path) -> None:
    """Copy the real launcher into a fake repo and control the interpreter.

    The stub interpreter reports its working directory and arguments, so the
    test needs no real virtualenv and no network.
    """
    repo_root = tmp_path / "repo"
    scripts_dir = repo_root / "scripts"
    scripts_dir.mkdir(parents=True)
    launcher = scripts_dir / "check-artifact-parity"
    launcher.write_text(LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8")
    launcher.chmod(LAUNCHER.stat().st_mode)

    venv_bin = repo_root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    stub_python = venv_bin / "python"
    stub_python.write_text(
        textwrap.dedent("""\
            #!/usr/bin/env bash
            echo "CWD=$(pwd)"
            echo "ARGS=$*"
        """),
        encoding="utf-8",
    )
    stub_python.chmod(0o755)

    outside = tmp_path / "somewhere" / "else"
    outside.mkdir(parents=True)
    result = subprocess.run(
        [str(launcher), "--stage", "run", "/tmp/baseline-run", "/tmp/comparison-run"],
        capture_output=True,
        text=True,
        cwd=str(outside),
    )

    assert result.returncode == 0, result.stderr
    assert f"CWD={repo_root}" in result.stdout
    assert "ARGS=-m autoscout.artifact_parity --stage run /tmp/baseline-run /tmp/comparison-run" in result.stdout


def test_launcher_fails_cleanly_without_an_interpreter(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    scripts_dir = repo_root / "scripts"
    scripts_dir.mkdir(parents=True)
    launcher = scripts_dir / "check-artifact-parity"
    launcher.write_text(LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8")
    launcher.chmod(LAUNCHER.stat().st_mode)

    # A PATH that holds bash and nothing else: the shebang still resolves, and
    # no python3 is reachable.
    bash_only_bin = tmp_path / "bash-only-bin"
    bash_only_bin.mkdir()
    (bash_only_bin / "bash").symlink_to("/bin/bash")
    result = subprocess.run(
        [str(launcher), "--stage", "run", "a", "b"],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        env={"PATH": str(bash_only_bin)},
    )

    assert result.returncode == 1
    assert "no usable Python interpreter found" in result.stderr
