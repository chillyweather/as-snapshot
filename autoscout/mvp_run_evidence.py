"""Operation-stamped evidence for one weekly MVP Pipeline run.

This module owns the interpretation of retry and completion evidence.
Callers do not slice workflow logs or decide which repository-finalize entry
wins.  Legacy unstamped entries may be interpreted for backward-compatible
Diagnosis output, but they never authorize resume ownership, allowlists, or
stale-operation exemptions. Stamped evidence is required for those decisions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.source_run_status import KIRKUS_DEFERRED_SKIP_REASON
from autoscout.weekly_report import WeeklyReportStore


@dataclass(frozen=True)
class MvpRunEvidence:
    """Typed, read-only interpretation of the current run evidence."""

    operation_id: str
    operation_date: str
    capture_current: bool
    log: tuple[dict[str, Any], ...]
    terminal: dict[str, Any] | None
    latest_repository_finalization: dict[str, Any] | None
    allowlist: frozenset[Path]
    legacy_evidence_present: bool = False
    stamped_current: bool = False

    @property
    def has_log(self) -> bool:
        return bool(self.log)

    @property
    def has_completed_outcome(self) -> bool:
        return bool(self.terminal and str(self.terminal.get("status", "")).startswith("completed/"))

    @property
    def has_terminal_intervention(self) -> bool:
        return bool(self.terminal and self.terminal.get("status") == "intervention-required")

    @property
    def has_work(self) -> bool:
        return bool(self.log)

    @property
    def persistence_ok(self) -> bool:
        return bool(
            self.latest_repository_finalization and self.latest_repository_finalization.get("status") in {"ok", "noop"}
        )

    @property
    def persistence_failed(self) -> bool:
        return self.latest_repository_finalization is not None and not self.persistence_ok

    @property
    def repository_finalize_statuses(self) -> tuple[str, ...]:
        return tuple(str(entry.get("status", "")) for entry in self.log if entry.get("stage") == "repository-finalize")

    @property
    def resumable(self) -> bool:
        # A terminal completion always wins.  This also makes a completed
        # same-day operation non-resumable even when its capture marker stays.
        return self.capture_current and not self.has_completed_outcome

    @property
    def needs_resume(self) -> bool:
        return self.resumable or (self.has_work and self.has_terminal_intervention)

    @property
    def terminal_status(self) -> str:
        return str(self.terminal.get("status", "")) if self.terminal else ""

    @property
    def failed_stage(self) -> str:
        for entry in reversed(self.log):
            stage = str(entry.get("stage", ""))
            status = str(entry.get("status", ""))
            if status == "failed" and stage not in {"terminal", "outcome-candidate", "repository-finalize"}:
                return stage
        for entry in reversed(self.log):
            stage = str(entry.get("stage", ""))
            if stage in {"capture", "codex", "finalize", "pipeline", "enrichment", "review", "health"}:
                if str(entry.get("status", "")) != "ok":
                    return stage
        return ""


# ---------------------------------------------------------------------------
# Operation identity and safe artifact ownership
# ---------------------------------------------------------------------------


def operation_id(config_path: Path, operation_date: date | None = None) -> str:
    """Return the identity for the date and resolved source portfolio."""
    from autoscout.config import resolve_weekly_portfolio

    config = read_json(config_path, default={})
    if not isinstance(config, dict):
        return ""
    config = resolve_weekly_portfolio(config, config_path)
    sources = [
        {
            "name": source.get("name"),
            "family": source.get("family"),
            "url": source.get("url"),
            "active": source.get("active", True),
            "extraction_method": source.get("extraction_method"),
        }
        for source in config.get("sources", [])
        if isinstance(source, dict)
    ]
    identity = {"operation_date": (operation_date or date.today()).isoformat(), "sources": sources}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def safe_artifact_path(output_dir: Path, value: Any) -> Path | None:
    """Resolve a relative artifact path only when it stays in output_dir."""
    if not isinstance(value, str) or not value.strip():
        return None
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    try:
        root = output_dir.resolve(strict=True)
        candidate = (root / relative).resolve(strict=False)
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate


def bind_worklist_to_operation(output_dir: Path, run_id: str) -> None:
    """Stamp captured worklist entries and bind their exact bytes."""
    worklist_path = output_dir / "codex-extraction-worklist.json"
    worklist = read_json(worklist_path, default={})
    if not isinstance(worklist, dict):
        return
    bind_worklist_payload(output_dir, worklist, run_id)
    write_json(worklist_path, worklist)


def bind_worklist_payload(output_dir: Path, worklist: dict[str, Any], run_id: str) -> None:
    """Bind captures before publishing a late append in one atomic write."""
    for item in worklist.get("work_items", []):
        if not isinstance(item, dict):
            continue
        capture_path = safe_artifact_path(output_dir, item.get("capture_file"))
        if capture_path is not None and capture_path.is_file():
            item["operation_id"] = run_id
            item["capture_sha256"] = hashlib.sha256(capture_path.read_bytes()).hexdigest()
    for source in worklist.get("precollected_sources", []):
        if not isinstance(source, dict):
            continue
        artifact_path = safe_artifact_path(output_dir, source.get("recorded_mentions_file"))
        if artifact_path is not None and artifact_path.is_file():
            source["operation_id"] = run_id
            source["recorded_mentions_sha256"] = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    worklist["operation_id"] = run_id


KIRKUS_DEFERRED_REASON = KIRKUS_DEFERRED_SKIP_REASON


def capture_source_runs(output_dir: Path) -> list[dict[str, Any]]:
    """Read capture rows, completing only a provable interrupted Kirkus append.

    The worklist commits the bound captures and their rows atomically. The
    separate source-runs write may lag; only its exact deferred Kirkus row
    may be replaced. All other disagreements remain invalid evidence.
    """
    runs = WeeklyReportStore(output_dir).read("source-runs.json", default=None)
    worklist = read_json(output_dir / "codex-extraction-worklist.json", default={})
    if not isinstance(runs, list) or any(not isinstance(row, dict) for row in runs):
        raise ValueError("invalid capture source runs")
    late_runs = worklist.get("kirkus_source_runs", [])
    if not isinstance(late_runs, list) or any(not isinstance(row, dict) for row in late_runs):
        raise ValueError("invalid Kirkus capture rows")
    for late in late_runs:
        identity = tuple(late.get(key) for key in ("source_name", "family", "url"))
        matches = [row for row in runs if tuple(row.get(key) for key in ("source_name", "family", "url")) == identity]
        if len(matches) != 1:
            raise ValueError("Kirkus capture row identity mismatch")
        prior = matches[0]
        if prior != late:
            if (
                prior.get("status") != "skipped"
                or prior.get("reason") != KIRKUS_DEFERRED_REASON
                or prior.get("source_check_date") != worklist.get("run_date")
            ):
                raise ValueError("Kirkus capture row mismatch")
            runs = [late if row is prior else row for row in runs]
    return runs


def _is_due(source: dict[str, Any], config: dict[str, Any], operation_date: str) -> bool:
    """Whether *source* is due on *operation_date* under the configuration as it is now (#1057).

    A stored ``not-due`` row is current evidence only while the schedule still
    says so: a changed collection weekday or frequency makes the source due.
    """
    from autoscout.collection_schedule import collection_weekday
    from autoscout.collection_schedule import is_source_due
    from autoscout.collection_schedule import source_check_frequency

    return is_source_due(source_check_frequency(source), date.fromisoformat(operation_date), collection_weekday(config))


def _capture_is_current(output_dir: Path, config_path: Path, operation_date: date | None = None) -> bool:
    """Validate all capture and replay identity evidence fail-closed."""
    try:
        from autoscout.codex_recovery import ProductionCodexAdapter
        from autoscout.config import resolve_weekly_portfolio

        run_id = operation_id(config_path, operation_date)
        if not run_id:
            return False
        expected_date = (operation_date or date.today()).isoformat()
        marker = read_json(output_dir / "codex-workflow-resume.json", default=None)
        worklist = read_json(output_dir / "codex-extraction-worklist.json", default=None)
        source_runs = capture_source_runs(output_dir)
        raw_config = read_json(config_path, default=None)
        if not all(isinstance(value, dict) for value in (marker, worklist, raw_config)):
            return False
        if not isinstance(source_runs, list):
            return False
        if marker.get("run_id") != run_id or worklist.get("operation_id") != run_id:
            return False
        completed_stages = marker.get("completed_stages")
        validated_artifacts = marker.get("validated_artifacts", [])
        if not isinstance(completed_stages, list) or "capture" not in completed_stages:
            return False
        if not isinstance(validated_artifacts, list) or any(not isinstance(v, dict) for v in validated_artifacts):
            return False
        if worklist.get("run_date") != expected_date:
            return False

        config = resolve_weekly_portfolio(raw_config, config_path)
        sources = config.get("sources")
        if not isinstance(sources, list) or not sources or any(not isinstance(s, dict) for s in sources):
            return False
        identities = [(s.get("name"), s.get("family"), s.get("url")) for s in sources]
        if any(not all(isinstance(v, str) and v for v in identity) for identity in identities):
            return False
        if len(set(identities)) != len(identities):
            return False
        configured = dict(zip(identities, sources, strict=True))
        from autoscout.source_collectors.base import contract_family

        kirkus_ids = {
            identity
            for identity, source in configured.items()
            if source.get("active", True) and contract_family(source) == "kirkus-coming-soon"
        }
        if worklist.get("kirkus_source_runs") and "kirkus_pending" not in worklist:
            return False
        if "kirkus_pending" in worklist:
            if type(worklist["kirkus_pending"]) is not bool or not kirkus_ids:
                return False
            late_runs = worklist.get("kirkus_source_runs", [])
            late_ids = [(row.get("source_name"), row.get("family"), row.get("url")) for row in late_runs]
            if late_runs and (
                worklist["kirkus_pending"] or set(late_ids) != kirkus_ids or len(late_ids) != len(kirkus_ids)
            ):
                return False

        runs: dict[tuple[str, str, str], dict[str, Any]] = {}
        for source_run in source_runs:
            if not isinstance(source_run, dict):
                return False
            identity = (source_run.get("source_name"), source_run.get("family"), source_run.get("url"))
            if identity not in configured or identity in runs or source_run.get("source_check_date") != expected_date:
                return False
            runs[identity] = source_run
        if set(runs) != set(configured):
            return False

        raw_items = worklist.get("work_items")
        precollected = worklist.get("precollected_sources")
        if not isinstance(raw_items, list) or not isinstance(precollected, list):
            return False
        item_ids: set[tuple[str, str, str]] = set()
        validator = ProductionCodexAdapter()
        for item in raw_items:
            if not isinstance(item, dict) or item.get("operation_id") != run_id:
                return False
            identity = (item.get("source_name"), item.get("source_family"), item.get("source_url"))
            if identity not in configured or identity in item_ids:
                return False
            item_ids.add(identity)
            capture = safe_artifact_path(output_dir, item.get("capture_file"))
            if capture is None or not capture.is_file():
                return False
            digest = item.get("capture_sha256")
            if not isinstance(digest, str) or hashlib.sha256(capture.read_bytes()).hexdigest() != digest:
                return False

            expected_name = item.get("expected_extraction_file")
            if not isinstance(expected_name, str) or not expected_name:
                return False
            extraction = safe_artifact_path(output_dir, expected_name)
            if extraction is None:
                return False
            evidence = [
                value
                for value in validated_artifacts
                if value.get("source_url") == identity[2]
                and value.get("capture_file") == item.get("capture_file")
                and value.get("expected_extraction_file") == expected_name
                and value.get("run_id") == run_id
            ]
            if evidence:
                if len(evidence) != 1 or not extraction.is_file():
                    return False
                artifact_digest = evidence[0].get("artifact_sha256")
                if not isinstance(artifact_digest, str) or not artifact_digest:
                    return False
                if hashlib.sha256(extraction.read_bytes()).hexdigest() != artifact_digest:
                    return False
                if not validator._validate_extraction_artifact(extraction, item, capture):
                    return False

        pre_ids: set[tuple[str, str, str]] = set()
        for entry in precollected:
            if not isinstance(entry, dict) or entry.get("operation_id") != run_id:
                return False
            identity = (entry.get("name"), entry.get("family"), entry.get("url"))
            if identity not in configured or identity in pre_ids:
                return False
            pre_ids.add(identity)
            artifact = safe_artifact_path(output_dir, entry.get("recorded_mentions_file"))
            digest = entry.get("recorded_mentions_sha256")
            if artifact is None or not artifact.is_file() or not isinstance(digest, str):
                return False
            if hashlib.sha256(artifact.read_bytes()).hexdigest() != digest:
                return False

        if item_ids & pre_ids:
            return False
        artifact_ids = item_ids | pre_ids
        for identity, source in configured.items():
            status = runs[identity].get("status")
            if identity in kirkus_ids:
                deferred = status == "skipped" and runs[identity].get("reason") == KIRKUS_DEFERRED_REASON
                if deferred != worklist.get("kirkus_pending", False):
                    return False
            active = source.get("active", True)
            if not active:
                if status != "inactive" or identity in artifact_ids:
                    return False
            elif status == "codex-captured":
                if identity not in item_ids:
                    return False
            elif status == "precollected":
                if identity not in pre_ids:
                    return False
            elif status in {"skipped", "failed", "ignored", "not-due"}:
                if identity in artifact_ids or (status == "not-due" and _is_due(source, config, expected_date)):
                    return False
            else:
                return False
        return True
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, AttributeError):
        return False


def validated_artifact_allowlist(output_dir: Path, config_path: Path, operation_date: date | None = None) -> set[Path]:
    """Return exact workflow-owned paths only for a validated current run."""
    if not _capture_is_current(output_dir, config_path, operation_date):
        return set()
    try:
        worklist = read_json(output_dir / "codex-extraction-worklist.json", default={})
        marker = read_json(output_dir / "codex-workflow-resume.json", default={})
        allowlist: set[Path] = set()
        for name in (
            "codex-extraction-worklist.json",
            "codex-workflow-resume.json",
            "codex-extraction-outcomes.json",
            "source-runs.json",
            "workflow-log.json",
        ):
            path = (
                WeeklyReportStore(output_dir).path("source-runs.json").resolve()
                if name == "source-runs.json"
                else safe_artifact_path(output_dir, name)
            )
            if path is not None and path.is_file():
                allowlist.add(path)
        from autoscout.codex_recovery import ProductionCodexAdapter

        run_id = marker["run_id"]
        validator = ProductionCodexAdapter()
        for item in worklist["work_items"]:
            capture = safe_artifact_path(output_dir, item.get("capture_file"))
            if capture is not None and capture.is_file():
                allowlist.add(capture)
            expected = item.get("expected_extraction_file")
            extraction = safe_artifact_path(output_dir, expected)
            evidence = [
                entry
                for entry in marker["validated_artifacts"]
                if entry.get("run_id") == run_id
                and entry.get("source_url") == item.get("source_url")
                and entry.get("capture_file") == item.get("capture_file")
                and entry.get("expected_extraction_file") == expected
            ]
            if extraction is None or not extraction.is_file():
                continue
            if not evidence and "kirkus_pending" in worklist:
                # An interrupted extractor may have published a valid result
                # just before recording its marker. Use the same independent
                # validation as fulfill(), bound to this operation's capture.
                if validator._validate_extraction_artifact(extraction, item, capture):
                    allowlist.add(extraction)
                continue
            if len(evidence) != 1:
                continue
            digest = evidence[0].get("artifact_sha256")
            if (
                isinstance(digest, str)
                and hashlib.sha256(extraction.read_bytes()).hexdigest() == digest
                and validator._validate_extraction_artifact(extraction, item, capture)
            ):
                allowlist.add(extraction)
        for entry in worklist["precollected_sources"]:
            artifact = safe_artifact_path(output_dir, entry.get("recorded_mentions_file"))
            if artifact is not None and artifact.is_file():
                allowlist.add(artifact)
        if worklist.get("kirkus_pending"):
            from autoscout.config import resolve_weekly_portfolio
            from autoscout.kirkus_listing import retained_capture_fields
            from autoscout.kirkus_listing import validate_page_identity
            from autoscout.source_collection import codex_capture_filename
            from autoscout.source_collectors.base import contract_family

            config = resolve_weekly_portfolio(read_json(config_path), config_path)
            for source in config["sources"]:
                if not source.get("active", True) or contract_family(source) != "kirkus-coming-soon":
                    continue
                # A process can die after writing the late capture but before
                # committing its work item. Exempt only this source's exact,
                # independently validated current-date capture, never a glob.
                capture = safe_artifact_path(output_dir, codex_capture_filename(source))
                if capture is None or not capture.is_file():
                    continue
                page = read_json(capture)
                if page.get("source_name") != source["name"] or page.get("source_url") != source["url"]:
                    return set()
                validate_page_identity(page, source, worklist["run_date"])
                if any(page.get(key) != value for key, value in retained_capture_fields(page, source).items()):
                    return set()
                allowlist.add(capture)
        return allowlist
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, AttributeError):
        return set()


# ---------------------------------------------------------------------------
# Workflow-log interpretation
# ---------------------------------------------------------------------------


def read_sanitized_log(output_dir: Path) -> tuple[list[dict[str, Any]], bool]:
    """Read the log and report whether legacy unstamped entries were found."""
    path = output_dir / "workflow-log.json"
    try:
        raw = read_json(path, default=[])
    except (OSError, RuntimeError, TypeError, ValueError):
        return [], False
    if not isinstance(raw, list):
        return [], False
    entries = [entry for entry in raw if isinstance(entry, dict)]
    return entries, any("operation_id" not in entry for entry in entries)


def _interpret_log(
    log: list[dict[str, Any]],
    operation_id_value: str,
    operation_date_value: str,
    *,
    allow_legacy: bool,
) -> MvpRunEvidence:
    """Interpret one already-loaded log without duplicating evidence rules."""
    entries = [entry for entry in log if isinstance(entry, dict)]
    stamped_current = any(entry.get("operation_id") == operation_id_value for entry in entries)
    if stamped_current:
        selected = [entry for entry in entries if entry.get("operation_id") == operation_id_value]
    elif allow_legacy and entries and all("operation_id" not in entry for entry in entries):
        selected = legacy_operation_log(entries)
    else:
        selected = []

    selected_tuple = tuple(selected)
    terminal = next(
        (entry for entry in reversed(selected_tuple) if entry.get("stage") == "terminal"),
        None,
    )
    finalizations = [entry for entry in selected_tuple if entry.get("stage") == "repository-finalize"]
    return MvpRunEvidence(
        operation_id=operation_id_value,
        operation_date=operation_date_value,
        capture_current=False,
        log=selected_tuple,
        terminal=terminal,
        latest_repository_finalization=finalizations[-1] if finalizations else None,
        allowlist=frozenset(),
        legacy_evidence_present=any("operation_id" not in entry for entry in entries),
        stamped_current=stamped_current,
    )


def evidence_from_log(
    log: list[dict[str, Any]],
    operation_id_value: str,
    operation_date_value: str = "",
    *,
    allow_legacy: bool = False,
) -> MvpRunEvidence:
    """Interpret a trusted supplied log for one known operation.

    ``allow_legacy`` exists only for trusted in-memory workflow results, not
    for persisted files.  Persisted compatibility interpretation is selected
    by :func:`inspect` and never authorizes a resume or artifact allowlist.
    """
    return _interpret_log(log, operation_id_value, operation_date_value, allow_legacy=allow_legacy)


def inspect(output_dir: Path, config_path: Path, operation_date: date | None = None) -> MvpRunEvidence:
    """Return one typed, read-only snapshot of current operation evidence."""
    effective_date = operation_date or date.today()
    run_id = operation_id(config_path, effective_date)
    raw_log, legacy = read_sanitized_log(output_dir)
    evidence = _interpret_log(
        raw_log,
        run_id,
        effective_date.isoformat(),
        allow_legacy=bool(raw_log) and not any("operation_id" in entry for entry in raw_log),
    )
    current = _capture_is_current(output_dir, config_path, effective_date)
    allowlist = validated_artifact_allowlist(output_dir, config_path, effective_date) if current else set()
    return MvpRunEvidence(
        operation_id=evidence.operation_id,
        operation_date=evidence.operation_date,
        capture_current=current,
        log=evidence.log,
        terminal=evidence.terminal,
        latest_repository_finalization=evidence.latest_repository_finalization,
        allowlist=frozenset(allowlist),
        legacy_evidence_present=legacy,
        stamped_current=evidence.stamped_current,
    )


def legacy_operation_log(log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compatibility projection for old diagnostic helper callers.

    This projection is not used for authorization.  New callers must use
    :func:`inspect`, which requires operation-stamped evidence.
    """
    terminals = [index for index, entry in enumerate(log) if entry.get("stage") == "terminal"]
    if not terminals:
        return log
    if terminals[-1] == len(log) - 1:
        start = terminals[-2] + 1 if len(terminals) >= 2 else 0
        return log[start:]
    return log[terminals[-1] + 1 :]


def capture_is_current(output_dir: Path, config_path: Path, operation_date: date | None = None) -> bool:
    """Public read-only capture-currentness check."""
    return _capture_is_current(output_dir, config_path, operation_date)


def artifact_allowlist(output_dir: Path, config_path: Path, operation_date: date | None = None) -> frozenset[Path]:
    """Public read-only ownership allowlist."""
    return frozenset(validated_artifact_allowlist(output_dir, config_path, operation_date))


def read_snapshot(output_dir: Path, config_path: Path, operation_date: date | None = None) -> MvpRunEvidence:
    """Named alias for the typed evidence snapshot interface."""
    return inspect(output_dir, config_path, operation_date)


def stamp_entry(entry: dict[str, Any], operation_id_value: str) -> dict[str, Any]:
    """Return an entry with the operation identity attached."""
    stamped = dict(entry)
    stamped["operation_id"] = operation_id_value
    return stamped


# Compatibility name for callers that used the old private helper.
_capture_is_current_for_compat = _capture_is_current
