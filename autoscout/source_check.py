"""Capture-only source health probe (check-sources command).

Answers "which configured sources are dead, alive, or need fixing right now"
without running extraction, the model backend, or the weekly workflow. It
reuses the same capture pass as ``prepare-codex-extraction`` — including the
wrong-target and expected-page-heading guards — and renders one verdict line
per source. It never touches weekly state: captures go to a throwaway
directory unless the caller asks to keep them.
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from datetime import date
from pathlib import Path

from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.source_collection import prepare_codex_extraction_worklist

# Statuses that mean "the source needs attention", as opposed to healthy
# ("codex-captured", "precollected") or deliberately not collected
# ("inactive", "skipped", "ignored").
_FAILURE_STATUSES = {"failed"}

# Maximum rank tokens rendered inline in the per-source verdict line, so a
# short ranking page stays diagnosable after its capture is discarded
# (Issue #665) without unbounded output.
_MAX_DIAGNOSTIC_RANKS_SHOWN = 12


def _probe_reference_date(config: Mapping[str, object], *, live: bool, effective_date: str | None) -> str:
    """Return the ISO reference date one probe pass must judge freshness by.

    A live probe reads pages as they stand now, so week-freshness and
    reporting-period checks must judge them against the clock. A configured
    ``run_date`` is a pinned replay label and must never decide whether a live
    page is current (Issue #612): a stale label pushes every dated chart out of
    its recency window and fails a healthy source closed. An explicit caller
    date still wins, so a caller can pin the clock.
    """
    if effective_date:
        return effective_date
    if live:
        return date.today().isoformat()
    return str(config.get("run_date") or date.today().isoformat())


def _ranking_diagnostics_note(debug: Mapping[str, object]) -> str:
    """Render bounded rank diagnostics for a verdict line.

    The default health check discards captures, so the structured
    ``raw_row_count``/``missing_ranks`` debug fields (Issue #665) would
    otherwise be lost. Only counts and rank tokens are rendered; titles,
    authors, URLs, and identifiers never reach this output. An accepted
    Issue #668 incomplete capture renders its coverage the same way with an
    explicit ``incomplete`` marker so partial coverage never reads as full.
    """
    raw = debug.get("raw_row_count")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return ""
    parts = [f"rows={raw}"]
    if debug.get("incomplete_ranking") is True:
        parts.append("incomplete")
        expected = debug.get("expected_raw_row_count")
        if isinstance(expected, int) and not isinstance(expected, bool):
            parts.append(f"expected={expected}")
    missing = debug.get("missing_ranks")
    if isinstance(missing, list) and all(isinstance(value, str) for value in missing):
        total = debug.get("missing_rank_count")
        total_count = total if isinstance(total, int) and not isinstance(total, bool) else len(missing)
        shown = [value for value in missing[:_MAX_DIAGNOSTIC_RANKS_SHOWN] if value.isdigit()]
        if shown:
            rendered = ",".join(shown)
            remaining = total_count - len(shown)
            if remaining > 0:
                rendered += f"…(+{remaining} more)"
            parts.append(f"missing={rendered}")
        else:
            parts.append(f"missing=({total_count})")
    if debug.get("duplicate_ranks") is True:
        parts.append("duplicates=yes")
    if debug.get("observed_ranks_truncated") is True or debug.get("missing_ranks_truncated") is True:
        parts.append("ranks-truncated")
    return " [" + " ".join(parts) + "]"


def check_sources(
    config_path: Path,
    output_dir: Path | None = None,
    *,
    live: bool = True,
    effective_date: str | None = None,
) -> int:
    """Probe every configured source and print a per-source verdict table.

    Returns 1 when any source failed the capture pass, 0 otherwise. Skipped,
    inactive, and ignored sources are reported but are not failures — they
    are deliberate configuration states.
    """
    config = read_json(config_path, default={})
    run_date = _probe_reference_date(config, live=live, effective_date=effective_date)

    if output_dir is None:
        with tempfile.TemporaryDirectory(prefix="autoscout-check-sources-") as scratch:
            worklist = prepare_codex_extraction_worklist(config_path, Path(scratch), run_date, live=live)
        capture_note = "captures discarded (pass --output-dir to keep them)"
    else:
        output_dir.mkdir(parents=True, exist_ok=True)
        worklist = prepare_codex_extraction_worklist(config_path, output_dir, run_date, live=live)
        write_json(output_dir / "source-runs.json", worklist.source_runs)
        capture_note = f"captures and source-runs.json kept in {output_dir}"

    print(f"source check ({'live' if live else 'fixture replay'}) — {run_date}")
    name_width = max((len(str(run.get("source_name", ""))) for run in worklist.source_runs), default=0)
    status_counts: dict[str, int] = {}
    for run in worklist.source_runs:
        status = str(run.get("status", "unknown"))
        status_counts[status] = status_counts.get(status, 0) + 1
        name = str(run.get("source_name", "?"))
        debug = run.get("debug_metadata") or {}
        if isinstance(debug, Mapping):
            note = str(run.get("reason") or debug.get("safe_failure_status") or "")
            note = f"{note}{_ranking_diagnostics_note(debug)}".rstrip()
        else:
            note = str(run.get("reason") or "")
        duration = run.get("duration_seconds")
        timing = f"{duration:.1f}s" if isinstance(duration, int | float) else ""
        line = f"{status:14} {name:{name_width}} {timing:>6}"
        print(f"{line}  {note}".rstrip())

    failed_count = sum(status_counts.get(status, 0) for status in _FAILURE_STATUSES)
    summary = " ".join(
        f"{status}={count}" for status, count in sorted(status_counts.items()) if status not in _FAILURE_STATUSES
    )
    print(f"checked={len(worklist.source_runs)} {summary} failed={failed_count}".replace("  ", " "))
    print(capture_note)
    return 1 if failed_count else 0
