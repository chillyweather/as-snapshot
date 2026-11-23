"""Artifact parity gate: byte-compare two offline run directories.

The gate runs in two stages, and their order is the point.

``run-mvp`` writes twenty artifacts. ``render-review`` then writes three more
into the same directory and rewrites ``editor-facing.csv`` from candidate
history: the run writes three rows, the render writes four and fills the editor
decision columns. A comparison made after the render therefore never sees the
file the run wrote, and nondeterminism in the run's own CSV writer goes through
unnoticed.

The run stage compares the twenty run artifacts before either directory is
rendered. The review stage compares the four artifacts the render owns
afterwards. ``editor-facing.csv`` belongs to both stages because two different
writers produce it, and each one is a separate promise.

Every stage is fail-closed. A listed artifact is a promise to compare, so a
missing file fails the gate instead of printing SKIP, and the verdict can never
read PASSED while an artifact went uncompared.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from autoscout.state_store.layout import CANDIDATE_HISTORY_FILE
from autoscout.state_store.layout import stored_history_bytes

# Everything ``run-mvp`` writes. The first fourteen names are the list the gate
# compared before issue #657, kept in their original order so the history of the
# list stays readable. The last six were produced but never compared.
RUN_ARTIFACTS: tuple[str, ...] = (
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
    "derived-rule-suggestions.json",
    "derived-rule-suggestions.md",
    "hidden-by-rule.json",
    "internal-pre-suppression-candidates.json",
    "source-metrics.json",
    "source-review.md",
)

# Everything ``render-review`` writes into the same directory. It adds three
# files and replaces ``editor-facing.csv``.
REVIEW_ARTIFACTS: tuple[str, ...] = (
    "editor-facing.csv",
    "weekly-review.md",
    "weekly-review.html",
    "description-coverage.json",
)

STAGE_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "run": RUN_ARTIFACTS,
    "review": REVIEW_ARTIFACTS,
}

# Every file the two commands produce together. The parity gate compares one
# stage at a time, so this list exists for the drift test that holds the two
# stage lists to what a run actually writes.
ARTIFACTS: tuple[str, ...] = tuple(dict.fromkeys((*RUN_ARTIFACTS, *REVIEW_ARTIFACTS)))


@dataclass(frozen=True)
class ArtifactEntry:
    name: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class ParityResult:
    entries: tuple[ArtifactEntry, ...]
    passed: int
    failed: int
    stage: str = ""

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.passed == len(self.entries)


def _read_bytes(path: Path) -> bytes | None:
    try:
        if not path.is_file():
            return _sharded_history_bytes(path)
        return path.read_bytes()
    except OSError:
        return None


def _sharded_history_bytes(path: Path) -> bytes | None:
    """Candidate History of a run directory that is a sharded state directory, in its single-file form (#1073).

    A run whose output directory is its state directory keeps Candidate
    History one file per book and writes no single-file copy; the shards
    compare as the single file they hold, so a run on single-file state and
    a run on sharded state still compare byte for byte.
    """
    if path.name != CANDIDATE_HISTORY_FILE:
        return None
    try:
        return stored_history_bytes(path.parent)
    except ValueError:
        return None


def compare_dirs(
    baseline: Path,
    comparison: Path,
    artifacts: Sequence[str],
    stage: str = "",
) -> ParityResult:
    """Compare the named artifacts byte for byte. Offline and deterministic.

    The caller names the list. The comparator holds no default, so no call can
    check a weaker list than it meant to.
    """
    entries: list[ArtifactEntry] = []
    passed = 0
    failed = 0
    for name in artifacts:
        baseline_bytes = _read_bytes(baseline / name)
        comparison_bytes = _read_bytes(comparison / name)
        if baseline_bytes is not None and comparison_bytes is not None:
            if baseline_bytes == comparison_bytes:
                entries.append(ArtifactEntry(name, "ok"))
                passed += 1
            else:
                entries.append(ArtifactEntry(name, "differ", "bytes differ"))
                failed += 1
        elif baseline_bytes is None and comparison_bytes is None:
            entries.append(ArtifactEntry(name, "missing_both", "not produced on either side"))
            failed += 1
        elif baseline_bytes is None:
            entries.append(ArtifactEntry(name, "missing_baseline", "only comparison side present"))
            failed += 1
        else:
            entries.append(ArtifactEntry(name, "missing_comparison", "only baseline side present"))
            failed += 1
    return ParityResult(entries=tuple(entries), passed=passed, failed=failed, stage=stage)


def compare_stage(baseline: Path, comparison: Path, stage: str) -> ParityResult:
    """Compare one named stage of the gate."""
    try:
        artifacts = STAGE_ARTIFACTS[stage]
    except KeyError:
        raise ValueError(f"unknown parity stage: {stage}") from None
    return compare_dirs(baseline, comparison, artifacts, stage=stage)


def format_report(result: ParityResult) -> str:
    label = f" ({result.stage} stage)" if result.stage else ""
    lines = [f"=== Artifact parity check{label} ==="]
    for entry in result.entries:
        if entry.status == "ok":
            lines.append(f"  OK  {entry.name}")
        elif entry.status == "differ":
            lines.append(f"  FAIL {entry.name} (bytes differ)")
        elif entry.status == "missing_both":
            lines.append(f"  FAIL {entry.name} (not produced on either side)")
        elif entry.status == "missing_baseline":
            lines.append(f"  FAIL {entry.name} (only comparison side present)")
        else:
            lines.append(f"  FAIL {entry.name} (only baseline side present)")
    lines.append("")
    lines.append(f"Passed: {result.passed}  Failed: {result.failed}")
    if result.ok:
        lines.append(f"ARTIFACT PARITY GATE PASSED{label}")
    else:
        lines.append(f"ARTIFACT PARITY GATE FAILED{label}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Byte-compare two offline run directories.")
    parser.add_argument(
        "--stage",
        required=True,
        choices=sorted(STAGE_ARTIFACTS),
        help=(
            "Which promise to check. Run the 'run' stage before render-review touches "
            "either directory, and the 'review' stage after it."
        ),
    )
    parser.add_argument("baseline", type=Path)
    parser.add_argument("comparison", type=Path)
    args = parser.parse_args(argv)
    result = compare_stage(args.baseline, args.comparison, args.stage)
    print(format_report(result))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
