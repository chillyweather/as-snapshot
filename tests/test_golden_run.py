"""Determinism guard for the collector-registry refactor (issue #234, PRD #231).

Runs the full MVP Pipeline (``autoscout.pipeline.run_pipeline`` via the
``run_mvp`` CLI entry point) on a fixture config that exercises every
collection path the upcoming collector-registry refactor will touch:

- a BookPage-mode source (``bookpage-recorded-response``)
- an NPR-mode source (``npr-source-page``)
- an agent-extraction source in replay mode (``agent-extraction-replay``)
- a Codex-extraction-replay source (``codex-extraction-replay``)
- one source combining ``fixture_candidates``, ``collector_mentions``,
  and a ``recorded_mentions_file`` (Recorded Source Mentions)
- one inactive source, one skipped source, and one ignored
  (non-discovery-family) source

Every artifact ``run_pipeline`` produces is pinned byte-for-byte against
committed golden copies under ``tests/fixtures/golden-run/expected/``.
A second test proves the pipeline itself is deterministic by running it
twice on identical inputs into two fresh output directories and
comparing the two runs byte-for-byte -- independent of whether the
goldens happen to be stale.

Path normalization: none is required. Every fixture file referenced from
the config below is given as a path *relative to the config file*
(e.g. ``"bookpage-response.json"``), and the collectors in
``autoscout/source_collectors/`` record the unresolved, as-configured
string in ``debug_metadata`` (see e.g. ``configured_bookpage_response``,
which sets ``debug_metadata["response_file"] = str(raw_path)`` before
resolving it against ``config_path.parent``). Because the config and its
fixture files live side by side, that recorded string is always the same
short relative name regardless of which tmp directory pytest hands us,
so no artifact content is tmp-path-dependent and no scrubbing step is
needed. This is deliberate config shape, not luck -- pass an absolute
``source_page_fixture_file`` and this test would need a normalization
step before the golden comparison.
"""

from __future__ import annotations

import json
from pathlib import Path

from autoscout.cli.weekly import run_mvp

RUN_DATE = "2026-06-15"

GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "golden-run" / "expected"

# These artifacts are expected to be empty lists for this fixture config. They
# are kept as literals because identical fixture files are not allowed (#1022).
GOLDEN_LITERAL_BYTES = {
    "hidden-by-rule.json": b"[]\n",
    "internal-exclusions.json": b"[]\n",
    "memo-queue.json": b"[]\n",
    "proposed-corrections.json": b"[]\n",
    "source-proposals.json": b"[]\n",
    "suppressed-candidates.json": b"[]\n",
}

# The pre-suppression candidates equal the scouting candidates byte for byte.
# If the pipeline legitimately makes the two differ, remove this alias and
# commit both files (#1022).
GOLDEN_FILE_ALIAS = {
    "internal-pre-suppression-candidates.json": "scouting-candidates.json",
}

# Every artifact autoscout.pipeline.run_pipeline writes for this config.
# pilot_mode is enabled in the fixture config so the two conditional
# artifacts (editor-review-guide.md, pilot-status.json) are covered too.
ARTIFACT_NAMES = [
    "source-mentions.json",
    "report-qa.json",
    "editor-facing.csv",
    "russian-review-queue.csv",
    "scouting-candidates.json",
    "candidate-history.json",
    "proposed-corrections.json",
    "memo-queue.json",
    "source-proposals.json",
    "suppressed-candidates.json",
    "hidden-by-rule.json",
    "internal-exclusions.json",
    "internal-pre-suppression-candidates.json",
    "russian-edition-checks.json",
    "source-runs.json",
    "source-metrics.json",
    "source-review.md",
    "derived-rule-suggestions.json",
    "derived-rule-suggestions.md",
    "run-summary.json",
    "editor-review-guide.md",
    "pilot-status.json",
]


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _build_config(tmp_path: Path) -> Path:
    """Write a self-contained fixture config plus every input file it
    references, all under *tmp_path*, and return the config path.

    Every source below exercises exactly one of the collection paths the
    collector-registry refactor (PRD #231) will touch.
    """
    # -- BookPage: bookpage-recorded-response -----------------------------
    _write_json(
        tmp_path / "bookpage-response.json",
        {
            "source_page": "https://www.bookpage.com/best-books/best-nonfiction/",
            "entries": [
                {
                    "title": "BookPage Golden Book",
                    "authors": ["Morgan Golden"],
                    "publisher": "Golden Press",
                    "publication_year": "2024",
                    "item_url": "https://www.bookpage.com/reviews/bookpage-golden-book",
                    "evidence_locator": "BookPage Best Nonfiction 2024, entry for BookPage Golden Book.",
                    "category": "history",
                    "summary": "Narrative nonfiction about a golden-run fixture archive.",
                }
            ],
        },
    )

    # -- NPR: npr-source-page ----------------------------------------------
    _write_json(
        tmp_path / "npr-page.json",
        {
            "source_page": "https://www.npr.org/2024/06/18/golden-run-nonfiction-summer-2024",
            "html": ('<article class="item"><h2>NPR Golden Book</h2><p class="byline">by Kara Golden</p></article>'),
        },
    )

    # -- Agent extraction: agent-extraction-replay -------------------------
    _write_json(
        tmp_path / "agent-page.json",
        {
            "source_page": "https://example.test/golden/agent-list",
            "html": '<a href="/books/agent-golden-book">Agent Golden Book</a> by Riley Golden',
        },
    )
    _write_json(
        tmp_path / "agent-extraction.json",
        {
            "mentions": [
                {
                    "title": "Agent Golden Book",
                    "primary_authors": ["Riley Golden"],
                    "source_url": "https://example.test/books/agent-golden-book",
                    "signal_strength": "major media list",
                    "evidence_note": "Listed on a configured source page.",
                }
            ]
        },
    )

    # -- Codex extraction: codex-extraction-replay -------------------------
    _write_json(
        tmp_path / "codex-page.json",
        {
            "source_page": "https://example.test/golden/codex-nonfiction",
            "html": "<a href='/books/codex-golden-book'>Codex Golden Book</a> by Jordan Golden",
        },
    )
    _write_json(
        tmp_path / "codex-extraction.json",
        {
            "mentions": [
                {
                    "title": "Codex Golden Book",
                    "primary_authors": ["Jordan Golden"],
                    "source_url": "https://example.test/books/codex-golden-book",
                    "evidence_url": "https://example.test/books/codex-golden-book",
                    "signal_type": "media/curated list",
                    "signal_strength": "major media list",
                    "evidence_note": "Codex-extracted metadata-only mention from the captured configured page.",
                    "candidate_category": "history",
                    "publisher": "Golden Press",
                    "publication_year": "2024",
                    "short_description": "A metadata-only description for the golden run.",
                }
            ]
        },
    )

    # -- Recorded Source Mentions, layered with fixture_candidates and ----
    # -- collector_mentions on the same source ------------------------------
    _write_json(
        tmp_path / "recorded-mentions.json",
        [
            {
                "title": "Recorded Golden Book",
                "primary_authors": ["Riley Recorded"],
                "source_url": "https://example.test/recorded",
                "source_check_date": "2026-06-10",
                "signal_type": "media/curated list",
                "signal_strength": "major media list",
                "evidence_note": "Recorded from a configured source page.",
            }
        ],
    )

    sources = [
        {
            "name": "BookPage Best Nonfiction",
            "family": "media/curated list",
            "url": "https://www.bookpage.com/best-books/best-nonfiction/",
            "access_status": "open",
            "extraction_method": "bookpage-recorded-response",
            "check_frequency": "weekly",
            "language": "en",
            "notes": "Deterministic BookPage collector response.",
            "collector_response_file": "bookpage-response.json",
        },
        {
            "name": "NPR Golden Run Nonfiction Staff Picks",
            "family": "media/curated list",
            "url": "https://www.npr.org/2024/06/18/golden-run-nonfiction-summer-2024",
            "access_status": "open",
            "extraction_method": "npr-source-page",
            "check_frequency": "weekly",
            "language": "en",
            "source_page_fixture_file": "npr-page.json",
        },
        {
            "name": "Agent Extraction Golden List",
            "family": "media/curated list",
            "url": "https://example.test/golden/agent-list",
            "access_status": "open",
            "extraction_method": "agent-extraction-replay",
            "check_frequency": "weekly",
            "language": "en",
            "source_page_fixture_file": "agent-page.json",
            "extraction_response_file": "agent-extraction.json",
        },
        {
            "name": "Codex Extraction Golden Nonfiction",
            "family": "media/curated list",
            "url": "https://example.test/golden/codex-nonfiction",
            "access_status": "open",
            "extraction_method": "codex-extraction-replay",
            "check_frequency": "weekly",
            "language": "en",
            "source_page_fixture_file": "codex-page.json",
            "extraction_response_file": "codex-extraction.json",
        },
        {
            "name": "Fixture Collector Recorded Golden Source",
            "family": "media/curated list",
            "url": "https://example.test/golden/combined",
            "access_status": "open",
            "check_frequency": "weekly",
            "language": "en",
            "fixture_candidates": [
                {
                    "title": "Fixture Golden Book",
                    "primary_authors": ["Alex Fixture"],
                    "source_url": "https://example.test/fixture/golden-book",
                    "signal_strength": "winner",
                    "evidence_note": "Won a configured award.",
                }
            ],
            "collector_mentions": [
                {
                    "title": "Collector Golden Book",
                    "primary_authors": ["Casey Collector"],
                    "source_url": "https://example.test/collector/golden-book",
                    "signal_type": "media/curated list",
                    "signal_strength": "major media list",
                    "evidence_note": "Collected through a configured collector.",
                }
            ],
            "recorded_mentions_file": "recorded-mentions.json",
        },
        {
            "name": "Inactive Golden Source",
            "family": "award",
            "active": False,
            "access_status": "open",
            "language": "en",
            "fixture_candidates": [{"title": "Hidden Inactive Book"}],
        },
        {
            "name": "Paid Golden Source",
            "family": "award",
            "access_status": "paid",
            "language": "en",
            "fixture_candidates": [{"title": "Hidden Skipped Book"}],
        },
        {
            "name": "Topic Search Golden Source",
            "family": "topic search",
            "access_status": "open",
            "language": "en",
            "fixture_candidates": [{"title": "Hidden Ignored Book"}],
        },
    ]

    config_path = tmp_path / "config.json"
    _write_json(
        config_path,
        {
            "run_date": RUN_DATE,
            "pilot_mode": True,
            "sources": sources,
        },
    )
    return config_path


def _diff_excerpt(expected: bytes, actual: bytes, context: int = 2) -> str:
    """Return a readable excerpt of where *expected* and *actual* first
    diverge, without dumping either file in full."""
    try:
        expected_lines = expected.decode("utf-8").splitlines()
        actual_lines = actual.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return f"binary content differs: {len(expected)} bytes expected, {len(actual)} bytes actual"

    for index, (expected_line, actual_line) in enumerate(zip(expected_lines, actual_lines, strict=False)):
        if expected_line != actual_line:
            lo = max(0, index - context)
            expected_excerpt = "\n".join(f"  - {line}" for line in expected_lines[lo : index + context + 1])
            actual_excerpt = "\n".join(f"  + {line}" for line in actual_lines[lo : index + context + 1])
            return f"first differing line {index + 1}:\nexpected:\n{expected_excerpt}\nactual:\n{actual_excerpt}"

    if len(expected_lines) != len(actual_lines):
        return (
            f"content matches up to line {min(len(expected_lines), len(actual_lines))}, "
            f"but expected has {len(expected_lines)} lines and actual has {len(actual_lines)} lines"
        )
    return "content differs but no line-level difference was found (whitespace-only?)"


def test_golden_run_matches_committed_artifacts(tmp_path):
    config_path = _build_config(tmp_path)
    output_dir = tmp_path / "run"

    run_mvp(config_path, output_dir)

    mismatches = []
    for name in ARTIFACT_NAMES:
        actual_path = output_dir / name
        assert actual_path.exists(), f"expected artifact was not produced: {name}"
        expected_bytes = GOLDEN_LITERAL_BYTES.get(name)
        if expected_bytes is None:
            expected_bytes = (GOLDEN_DIR / GOLDEN_FILE_ALIAS.get(name, name)).read_bytes()
        actual_bytes = actual_path.read_bytes()
        if actual_bytes != expected_bytes:
            mismatches.append(f"--- {name} ---\n{_diff_excerpt(expected_bytes, actual_bytes)}")

    assert not mismatches, "golden-run artifact(s) drifted from tests/fixtures/golden-run/expected/:\n\n" + (
        "\n\n".join(mismatches)
    )

    produced_names = {path.name for path in output_dir.iterdir() if path.is_file()}
    assert produced_names == set(ARTIFACT_NAMES), (
        f"produced artifact set changed: extra={produced_names - set(ARTIFACT_NAMES)}, "
        f"missing={set(ARTIFACT_NAMES) - produced_names}"
    )


def test_golden_run_is_deterministic_across_two_runs(tmp_path):
    config_path = _build_config(tmp_path)
    output_dir_a = tmp_path / "run-a"
    output_dir_b = tmp_path / "run-b"

    run_mvp(config_path, output_dir_a)
    run_mvp(config_path, output_dir_b)

    mismatches = []
    for name in ARTIFACT_NAMES:
        bytes_a = (output_dir_a / name).read_bytes()
        bytes_b = (output_dir_b / name).read_bytes()
        if bytes_a != bytes_b:
            mismatches.append(f"--- {name} ---\n{_diff_excerpt(bytes_a, bytes_b)}")

    assert not mismatches, (
        "running the pipeline twice on identical inputs produced different bytes "
        "(non-determinism, not just a stale golden):\n\n" + "\n\n".join(mismatches)
    )
