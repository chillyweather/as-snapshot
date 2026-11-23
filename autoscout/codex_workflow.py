"""Codex capture → worklist → replay workflow.

This module isolates the Codex prepare/finalize CLI workflow from the rest
of the CLI wiring.  The two entry points are:

* :func:`prepare_codex_extraction` -- produces codex-extraction-worklist.json + codex-capture-*.json
* :func:`finalize_codex_run` -- produces codex-replay-config.json
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import Any

from autoscout.artifact_freshness import FRESH
from autoscout.artifact_freshness import REUSED
from autoscout.artifact_freshness import UNBOUND
from autoscout.artifact_freshness import ArtifactFreshness
from autoscout.artifact_freshness import is_stale_artifact_freshness
from autoscout.collection_schedule import declared_check_frequency
from autoscout.collection_schedule import with_not_due_entries
from autoscout.config import resolve_weekly_portfolio
from autoscout.config import validate_config
from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.progress_snapshot import ProgressSnapshotFailureRecorder
from autoscout.progress_snapshot import ProgressSnapshotPresentation
from autoscout.runtime_budget import CAPTURE_RUNTIME_BUDGET_SECONDS
from autoscout.runtime_budget import CAPTURE_SOURCE_RETRY_TIMEOUT_SECONDS
from autoscout.runtime_budget import CAPTURE_SOURCE_TIMEOUT_SECONDS
from autoscout.source_collection import codex_extraction_slug
from autoscout.source_collection import prepare_codex_extraction_worklist
from autoscout.source_metadata import SOURCE_ARTIFACT_METADATA_FIELDS
from autoscout.source_rules import EXTRACTION_ARTIFACT_MISSING
from autoscout.source_rules import EXTRACTION_ARTIFACT_REJECTED
from autoscout.source_rules import EXTRACTION_ARTIFACT_SAFETY_STOP
from autoscout.source_rules import PRECOLLECTED_ARTIFACT_UNAVAILABLE


def _extraction_outcomes(output_dir: Path) -> dict[str, dict[str, Any]]:
    """Index the extraction round's per-source outcomes by source name.

    Absent for runs whose extraction predates the record, and for any run whose
    extraction never reached its terminal bookkeeping. Both cases degrade to the
    coarser "no artifact" reason rather than failing finalization.
    """
    from autoscout.codex_recovery import EXTRACTION_OUTCOMES_FILE

    path = output_dir / EXTRACTION_OUTCOMES_FILE
    if not path.is_file():
        return {}
    try:
        data = read_json(path, default={})
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    outcomes = data.get("outcomes")
    if not isinstance(outcomes, list):
        return {}
    return {
        str(record.get("source_name", "")): record
        for record in outcomes
        if isinstance(record, dict) and record.get("source_name")
    }


def _rejection_detail(record: dict[str, Any]) -> str:
    """Describe a refused artifact in one clause, for the withholding reason."""
    parts: list[str] = []
    diagnostic_class = str(record.get("diagnostic_class", "")).strip()
    if diagnostic_class:
        parts.append(diagnostic_class)
    raw_count = record.get("raw_extracted_count")
    if isinstance(raw_count, int) and raw_count > 0:
        parts.append(f"{raw_count} extracted mentions discarded")
    return "; ".join(parts)


def _resolve_extraction_slug(output_dir: Path, item: dict[str, Any]) -> str:
    """Name the extraction artifact this work item resolves to.

    Prefer the worklist's expected filename; fall back to hashless for older
    worklists that lack it, then to the hashed form.

    One resolver, because two would drift. The digest and finalization must
    agree on which file a work item means, or the digest silently stops
    covering an artifact finalization is really consuming (Issue #388).
    """
    expected = str(item.get("expected_extraction_file", "")).strip()
    if expected:
        return expected
    source_name = str(item.get("source_name", ""))
    hashless = codex_extraction_slug(source_name, "")
    if (output_dir / hashless).exists():
        return hashless
    return codex_extraction_slug(source_name, str(item.get("source_url", "")))


_IDENTITY_FIELDS = ("operation_id", "capture_sha256")


def _artifact_binding(extraction_path: Path, item: dict[str, Any]) -> ArtifactFreshness:
    """Classify an artifact as ``fresh``, ``reused``, or ``unbound``.

    Readiness used to mean nothing more than "the file exists", so an artifact
    of any age satisfied it and a source could stop extracting for weeks while
    its candidates kept appearing (Issue #384). An artifact echoes back the
    operation and capture it was produced for, so the binding can be checked
    rather than assumed.

    The check binds only on what the work item actually claims. The weekly
    workflow stamps both identity fields onto every work item before extraction
    (``bind_worklist_to_operation``), so there the check is complete. A
    standalone ``prepare-codex-extraction`` stamps neither, and there is then no
    evidence either way: that is ``unbound``, which is reported as its own
    state rather than being guessed into one of the other two.

    An unreadable artifact, or one that omits an identity the work item does
    claim, is reused rather than fresh. Claiming freshness is the one answer
    that cannot be recovered from later.
    """
    claimed = [field for field in _IDENTITY_FIELDS if str(item.get(field, ""))]
    if not claimed:
        return UNBOUND
    try:
        data = read_json(extraction_path, default=None)
    except (json.JSONDecodeError, OSError):
        return REUSED
    if not isinstance(data, dict):
        return REUSED
    if any(str(data.get(field, "")) != str(item[field]) for field in claimed):
        return REUSED
    return FRESH


def applied_capture_runtime_budget(output_dir: Path, default: float = CAPTURE_RUNTIME_BUDGET_SECONDS) -> float:
    """Return the capture budget the prepared worklist actually applied.

    The capture stage derives its budget from the configured portfolio, so the
    number a capture adapter reports has to come from the run rather than from
    a constant that may not be what the stage ran under (issue #717).
    """
    worklist = read_json(output_dir / "codex-extraction-worklist.json", default={})
    if not isinstance(worklist, dict):
        return default
    try:
        applied = float(worklist.get("runtime_budget_seconds", 0.0) or 0.0)
    except (TypeError, ValueError):
        return default
    return applied if applied > 0 else default


def prepare_codex_extraction(
    config_path: Path,
    output_dir: Path,
    *,
    live: bool = False,
    effective_date: str | None = None,
    monotonic: Any = time.monotonic,
    # ``None`` means derive the stage budget from what the portfolio declares.
    # This default used to be the flat base constant, which made the issue #595
    # derivation inert in production: the worklist only derives when it is
    # handed no budget (issue #717).
    runtime_budget_seconds: float | None = None,
    per_source_timeout_seconds: float = CAPTURE_SOURCE_TIMEOUT_SECONDS,
    retry_source_timeout_seconds: float = CAPTURE_SOURCE_RETRY_TIMEOUT_SECONDS,
    sleep: Any = time.sleep,
    progress_snapshot_failure_recorder: ProgressSnapshotFailureRecorder | None = None,
    snapshot_progress_presentation: ProgressSnapshotPresentation | None = None,
    kirkus_mode: str = "all",
    # Issue #1057: the weekly run skips annual and monthly sources outside
    # their collection weekday. A standalone capture collects every source.
    apply_collection_schedule: bool = False,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    config = read_json(config_path, default={})
    # Issue #280: explicit effective_date (from the clock-authoritative launcher)
    # wins over the stale config run_date. Standalone callers that omit it
    # keep the existing config/fallback behaviour.
    run_date = effective_date or str(config.get("run_date") or date.today().isoformat())
    previous: dict[str, Any] = {}
    previous_runs: list[dict[str, Any]] = []
    if kirkus_mode == "only":
        previous = read_json(output_dir / "codex-extraction-worklist.json", default={})
        if previous.get("run_date") != run_date or not previous.get("kirkus_pending"):
            raise ValueError("Kirkus append requires this operation's deferred worklist")
        previous_runs = read_json(output_dir / "source-runs.json", default=[])

    worklist = prepare_codex_extraction_worklist(
        config_path,
        output_dir,
        run_date,
        live=live,
        monotonic=monotonic,
        runtime_budget_seconds=runtime_budget_seconds,
        per_source_timeout_seconds=per_source_timeout_seconds,
        retry_source_timeout_seconds=retry_source_timeout_seconds,
        sleep=sleep,
        progress_snapshot_failure_recorder=progress_snapshot_failure_recorder,
        snapshot_progress_presentation=snapshot_progress_presentation,
        kirkus_mode=kirkus_mode,
        apply_collection_schedule=apply_collection_schedule,
    )
    payload = {
        **previous,
        "run_date": worklist.run_date,
        "work_items": previous.get("work_items", []) + worklist.work_items,
        "precollected_sources": previous.get("precollected_sources", []) + worklist.precollected_sources,
        "not_due_sources": previous.get("not_due_sources", []) + worklist.not_due_sources,
        "runtime_budget_seconds": previous.get("runtime_budget_seconds", 0) + worklist.runtime_budget_seconds,
    }
    if kirkus_mode != "all":
        payload["kirkus_pending"] = kirkus_mode == "defer"
    # Publish the bound late captures and their source rows together. A
    # resume can finish the separate source-runs write without recapturing.
    replaced = {run["source_name"] for run in worklist.source_runs}
    runs = [run for run in previous_runs if run.get("source_name") not in replaced] + worklist.source_runs
    if kirkus_mode == "only":
        from autoscout.mvp_run_evidence import bind_worklist_payload

        payload["kirkus_source_runs"] = worklist.source_runs
        if previous.get("operation_id"):
            bind_worklist_payload(output_dir, payload, previous["operation_id"])
    write_json(output_dir / "codex-extraction-worklist.json", payload)
    write_json(output_dir / "source-runs.json", runs)

    print(f"captured_source_count={worklist.captured_source_count}")
    print(f"skipped_source_count={worklist.skipped_source_count}")
    print(f"failed_source_count={worklist.failed_source_count}")
    print(f"worklist_items={len(worklist.work_items)}")
    print("collection_mode=codex-capture")
    print(f"worklist_file={output_dir / 'codex-extraction-worklist.json'}")
    print(f"source_runs_file={output_dir / 'source-runs.json'}")
    return 0


def finalize_codex_run(output_dir: Path, live_config: Path | None = None, effective_date: str | None = None) -> int:
    """Scan a Codex extraction run output directory for capture and extraction
    artifacts, then generate a codex-replay-config.json that run-mvp can ingest.

    For each work item in codex-extraction-worklist.json, looks for the
    extraction artifact named in the item's expected_extraction_file field
    (falling back to hashless/hashed slugs for older worklists).
    Sources with both a capture file and an extraction artifact become
    codex-extraction-replay entries in the generated config.
    """
    worklist_path = output_dir / "codex-extraction-worklist.json"
    if not worklist_path.exists():
        print("error=missing_worklist")
        print(f"expected={worklist_path}")
        print("Run prepare-codex-extraction first to produce a worklist.")
        return 1

    try:
        worklist_data = read_json(worklist_path, default={})
    except json.JSONDecodeError as exc:
        print(f"error=malformed_worklist_json: {exc.msg}")
        return 1

    work_items = worklist_data.get("work_items", [])
    precollected_sources = worklist_data.get("precollected_sources", [])
    if not work_items and not precollected_sources and not worklist_data.get("not_due_sources"):
        print("error=empty_worklist")
        return 1

    # Pull pilot settings from the live config when available.
    pilot_settings: dict[str, Any] = {}
    if live_config is not None and live_config.exists():
        try:
            lc = read_json(live_config, default={})
            if isinstance(lc, dict):
                lc = resolve_weekly_portfolio(lc, live_config)
            # Validate the resolved live config so portfolio overlays don't
            # produce false missing-name warnings.
            live_errors = validate_config(lc)
            if live_errors:
                print(f"live-config validation warning ({len(live_errors)} issue(s))", file=__import__("sys").stderr)
                for error in live_errors:
                    print(f"  - {error}", file=__import__("sys").stderr)
            for key in (
                "pilot_mode",
                "editor_row_target",
                "pilot_row_min",
                "pilot_row_max",
                "report_qa",
                "russian_edition_checks",
                # Issue #331: opt-in Search Optimization trial. A live config
                # that sets the flag true produces trial artifacts on replay;
                # a config that omits it keeps the trial disabled.
                "search_optimization_trial",
            ):
                if key in lc:
                    pilot_settings[key] = lc[key]
        except (json.JSONDecodeError, OSError):
            pass

    # Issue #280: explicit effective_date wins over stale worklist run_date.
    run_date = effective_date or str(worklist_data.get("run_date") or date.today().isoformat())
    sources: list[dict[str, Any]] = []
    ready = 0
    reused = 0
    pending = 0
    rejected = 0
    pending_files: list[tuple[str, str]] = []
    rejected_files: list[tuple[str, str]] = []
    outcomes = _extraction_outcomes(output_dir)
    blocked_outcomes = [
        outcome
        for outcome in outcomes.values()
        if str(outcome.get("terminal_class", ""))
        in {
            "unfinished",
            "portfolio_capacity_stop",
            "execution_safety_stop",
        }
    ]
    if blocked_outcomes:
        print(f"error=unfinished_extraction_outcomes; count={len(blocked_outcomes)}; replay_config_not_written")
        return 1

    for item in work_items:
        source_name = str(item.get("source_name", ""))
        capture_file = str(item.get("capture_file", ""))

        extraction_slug = _resolve_extraction_slug(output_dir, item)
        extraction_path = output_dir / extraction_slug

        source_entry: dict[str, Any] = {
            "name": source_name,
            "family": str(item.get("source_family", "")),
            "url": str(item.get("source_url", "")),
            "active": True,
            "access_status": str(item.get("source_metadata", {}).get("access_status", "open")),
            "extraction_method": "codex-extraction-replay",
            "check_frequency": declared_check_frequency(item.get("source_metadata", {})),
            "notes": str(item.get("source_metadata", {}).get("notes", "")),
            "source_page_fixture_file": capture_file,
            "codex_artifact_dir": ".",
        }
        metadata = item.get("source_metadata", {})
        if isinstance(metadata, dict):
            for field in SOURCE_ARTIFACT_METADATA_FIELDS:
                if field in metadata:
                    source_entry[field] = metadata[field]

        if extraction_path.exists():
            source_entry["extraction_response_file"] = extraction_slug
            # Reuse stays permitted, by operator decision, but never passes as
            # fresh collection: the label travels with the source so every
            # downstream report can separate this week's evidence from an
            # earlier run's (Issue #384).
            binding = _artifact_binding(extraction_path, item)
            source_entry["artifact_freshness"] = binding
            if is_stale_artifact_freshness(binding):
                reused += 1
            else:
                ready += 1
        else:
            outcome = outcomes.get(source_name, {})
            source_entry["active"] = False
            if str(outcome.get("outcome", "")) == "rejected":
                detail = _rejection_detail(outcome)
                source_entry["inactive_reason"] = EXTRACTION_ARTIFACT_REJECTED
                if detail:
                    source_entry["withheld_detail"] = detail
                source_entry["notes"] = (
                    str(source_entry["notes"]) + " Codex extraction artifact was refused by validation."
                ).strip()
                rejected += 1
                rejected_files.append((source_name, detail))
            elif str(outcome.get("terminal_class", "")) == "execution_safety_stop":
                source_entry["inactive_reason"] = EXTRACTION_ARTIFACT_SAFETY_STOP
                source_entry["notes"] = (
                    str(source_entry["notes"]) + " Codex extraction stopped at its absolute execution safety ceiling."
                ).strip()
            else:
                source_entry["inactive_reason"] = EXTRACTION_ARTIFACT_MISSING
                source_entry["notes"] = (
                    str(source_entry["notes"]) + " Codex extraction artifact not yet saved."
                ).strip()
                # Only a genuinely absent artifact is pending. Saving the file
                # again is the right next step there and the wrong one for a
                # refusal, where an artifact did arrive and was refused on its
                # content: re-saving the same bytes under the same name only
                # earns the same refusal.
                pending += 1
                pending_files.append((source_name, extraction_slug))

        sources.append(source_entry)

    for precollected in precollected_sources:
        if not isinstance(precollected, dict):
            continue
        artifact = str(precollected.get("recorded_mentions_file", "")).strip()
        source_entry = dict(precollected)
        if not artifact or not (output_dir / artifact).is_file():
            source_entry["active"] = False
            source_entry["inactive_reason"] = PRECOLLECTED_ARTIFACT_UNAVAILABLE
            source_entry["notes"] = (
                str(source_entry.get("notes", "")) + " Precollected metadata artifact is unavailable."
            ).strip()
            pending += 1
        else:
            ready += 1
        sources.append(source_entry)

    config: dict[str, Any] = {
        "run_date": run_date,
        "sources": with_not_due_entries(sources, worklist_data.get("not_due_sources")),
        **pilot_settings,
    }
    # The replay config names the operation it replays, so the pipeline can ask
    # "have I already scouted this operation?" instead of inferring it from the
    # run date. Two runs on one date are two operations, and the date cannot
    # tell them apart (Issue #388).
    worklist_operation_id = str(worklist_data.get("operation_id", "")).strip()
    if worklist_operation_id:
        config["operation_id"] = worklist_operation_id
    # Validate the generated replay config before writing it.
    replay_errors = validate_config(config)
    if replay_errors:
        print(
            f"generated replay config validation warning ({len(replay_errors)} issue(s))", file=__import__("sys").stderr
        )
        for error in replay_errors:
            print(f"  - {error}", file=__import__("sys").stderr)

    config_path = output_dir / "codex-replay-config.json"
    write_json(config_path, config)

    print(f"ready_sources={ready}")
    print(f"reused_sources={reused}")
    print(f"pending_sources={pending}")
    print(f"rejected_sources={rejected}")
    print(f"total_sources={len(sources)}")
    print(f"replay_config={config_path}")
    if reused:
        print(f"note={reused} source(s) served from an earlier run's artifact rather than fresh extraction.")
    # Gated on the list, not on the count. A withheld precollected source also
    # counts as pending but names no extraction artifact, so gating on the
    # count printed a note introducing a list that was not there.
    if pending_files:
        print(
            "note=Save each pending Codex extraction artifact under the exact filename below, then re-run finalize-codex-run."
        )
        for source_name, extraction_slug in pending_files:
            print(f"pending_artifact={extraction_slug} ({source_name})")
    if rejected:
        print(
            "note=Each refused artifact below did arrive and was refused on its content. "
            "Read its class, then fix the extraction; re-saving the same bytes earns the same refusal. "
            "The refused bytes are preserved under $TMPDIR/autoscout-rejected-artifacts/."
        )
        for source_name, detail in rejected_files:
            print(f"rejected_artifact={source_name} ({detail or 'no class recorded'})")
    # Reused artifacts are still artifacts, so the "nothing to work from" note
    # must count them. Only their freshness is in question, not their presence.
    if ready + reused == 0:
        print("note=No extraction artifacts found. Provide Codex extraction outputs before running run-mvp.")
    return 0
