from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from state_store_support import fail_database_commits
from state_store_support import read_state
from state_store_support import seed_state

import autoscout.cli.weekly as cli
import autoscout.feedback_finalization as finalization
import autoscout.source_collection as source_collection
import autoscout.weekly_workflow as weekly_workflow
from autoscout.cli.common import _validate_iso_date
from autoscout.cli.weekly import _run_weekly_command
from autoscout.codex_recovery import ExtractionAttempt
from autoscout.codex_recovery import ExtractionStatus
from autoscout.codex_recovery import ProductionCodexAdapter
from autoscout.content_description import apply_description_artifacts
from autoscout.editor_feedback import record_feedback
from autoscout.editorial_taste_profile import matching_metadata_field
from autoscout.json_io import read_json
from autoscout.json_io import write_json
from autoscout.pipeline import run_pipeline
from autoscout.pipeline_path import PipelinePath
from autoscout.run_health import run_health
from autoscout.russian_workflow import intervention_message
from autoscout.scouting_candidates import ScoutingCandidate
from autoscout.state_store import json_backend
from autoscout.state_store import open_state_store


def _feedback_scenario(tmp_path: Path) -> tuple[Path, Path, str]:
    config = tmp_path / "config.json"
    output = tmp_path / "out"
    state = tmp_path / "state"
    write_json(
        config,
        {
            "run_date": "2026-01-01",
            "sources": [
                {
                    "name": "Fixture",
                    "family": "award",
                    "url": "https://example.test/source",
                    "access_status": "open",
                    "extraction_method": "fixture",
                    "check_frequency": "weekly",
                    "language": "en",
                    "fixture_candidates": [
                        {"title": "Deep Book", "primary_authors": ["A Writer"], "signal_strength": "winner"}
                    ],
                }
            ],
        },
    )
    run_pipeline(config, output, state)
    identity_key = "deep book|a writer"
    assert record_feedback(identity_key, "needs_deeper_analysis", "Memo note", state, True) == 0
    return output, state, identity_key


def test_needs_deeper_fast_path_and_archive_retry_are_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, state, identity_key = _feedback_scenario(tmp_path)
    real_publish = json_backend.publish_bundle
    failed_once = False

    # The archive and the cleared pending rows are written in one State Store
    # transaction, the last state write of finalization; it fails once here.
    def fail_pending_clear(paths: list[Path], contents: list[str], **kwargs: Any) -> None:
        nonlocal failed_once
        if any(path.name == "pending-returned-editor-rows.json" for path in paths) and not failed_once:
            failed_once = True
            raise OSError("simulated late failure")
        real_publish(paths, contents, **kwargs)

    monkeypatch.setattr(json_backend, "publish_bundle", fail_pending_clear)
    # Under SQLite the same write is finalization's second database commit, after the history's.
    fail_database_commits(monkeypatch, call=2, message="simulated late failure")
    with pytest.raises(OSError):
        finalization.attempt_finalization(output, state, "2026-01-01")
    assert read_state(state, "pending_editor_rows")
    assert read_json(output / "memo-queue.json") == [
        {
            "identity_key": identity_key,
            "title": "Deep Book",
            "editor_note": "Memo note",
            "status": "queued_for_future_memo",
        }
    ]

    # A crashed report transaction remains fail-closed. The operator must
    # clear the marker through the explicit repair path before retrying.
    (output / ".weekly-report-transaction.json").unlink()
    assert finalization.attempt_finalization(output, state, "2026-01-01") == "applied"
    assert len(read_state(state, "archived_editor_rows")) == 1
    assert len(read_json(output / "memo-queue.json")) == 1
    for collection in ("candidate_history", "latest_scouting_candidates"):
        record = next(row for row in read_state(state, collection) if row["identity_key"] == identity_key)
        assert record["memo_queue_marker"] == "needs_deeper_analysis"


def test_state_guard_frames_filenames_and_empty_files(tmp_path: Path) -> None:
    seed_state(tmp_path, candidate_history=[{"identity_key": "same"}])
    seed_state(tmp_path, latest_scouting_candidates=[])
    first = finalization._state_guard_digest(open_state_store(tmp_path))
    seed_state(tmp_path, candidate_history=[])
    seed_state(tmp_path, latest_scouting_candidates=[{"identity_key": "same"}])
    assert finalization._state_guard_digest(open_state_store(tmp_path)) != first


def test_punctuation_wrapped_topic_term_matches_visible_metadata() -> None:
    candidate = ScoutingCandidate({"title": "Исследование квир культуры", "primary_authors": ["Автор"]})
    assert matching_metadata_field(candidate, "«квир»") == "title"


def test_invalid_source_timeout_fails_only_that_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "config.json"
    sources = [
        {
            "name": name,
            "family": "award",
            "url": f"https://example.test/{index}",
            "active": True,
            "access_status": "open",
            "extraction_method": "codex-extraction-replay",
            "check_frequency": "weekly",
            "language": "en",
            "timeout_seconds": timeout,
        }
        for index, (name, timeout) in enumerate((("Bad", math.inf), ("Good", 2)))
    ]
    write_json(config, {"sources": sources})
    monkeypatch.setattr(
        source_collection,
        "codex_source_page",
        lambda config_path, output_dir, source, live=False: ({"html": "", "source_page": source["url"]}, {}),
    )
    result = source_collection.prepare_codex_extraction_worklist(config, tmp_path / "out", "2026-01-01")
    assert [(run["source_name"], run["status"]) for run in result.source_runs] == [
        ("Bad", "failed"),
        ("Good", "codex-captured"),
    ]
    assert result.failed_source_count == 1
    assert result.source_runs[0]["reason"] == "invalid source timeout_seconds"
    assert len(result.work_items) == 1


@pytest.mark.parametrize("artifact,value", [("source-runs.json", [1]), ("pilot-status.json", [])])
def test_health_rejects_corrupt_top_level_or_list_rows(tmp_path: Path, artifact: str, value: object) -> None:
    write_json(tmp_path / "pilot-status.json", {"ready_for_editor": True})
    write_json(tmp_path / "run-summary.json", {})
    write_json(tmp_path / "source-runs.json", [])
    write_json(tmp_path / artifact, value)
    assert run_health(tmp_path) == 1


def test_direct_invalid_weekly_date_is_sanitized(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run_weekly_command(tmp_path / "config.json", tmp_path / "out", run_date="not-a-date") == 1
    output = capsys.readouterr().out
    assert "not-a-date" not in output
    assert output.count("?") == 1
    assert "Traceback" not in output


def test_historical_only_feedback_queues_then_status_away_removes_row(tmp_path: Path) -> None:
    _output, state, identity_key = _feedback_scenario(tmp_path)
    seed_state(state, pending_editor_rows=[])
    config = read_json(tmp_path / "config.json")
    config["sources"][0]["fixture_candidates"] = [
        {"title": "Current Book", "primary_authors": ["Current Author"], "signal_strength": "winner"}
    ]
    write_json(tmp_path / "config.json", config)
    output = tmp_path / "historical-only-output"
    run_pipeline(tmp_path / "config.json", output, state)
    assert identity_key not in {row["identity_key"] for row in read_json(output / "scouting-candidates.json")}
    assert record_feedback(identity_key, "needs_deeper_analysis", "Memo note", state, True) == 0

    assert finalization.attempt_finalization(output, state, "2026-01-01") == "applied"
    assert read_json(output / "memo-queue.json") == [
        {
            "identity_key": identity_key,
            "title": "Deep Book",
            "editor_note": "Memo note",
            "status": "queued_for_future_memo",
        }
    ]
    assert record_feedback(identity_key, "reject", "Memo note", state, True) == 0
    assert finalization.attempt_finalization(output, state, "2026-01-01") == "applied"
    assert read_json(output / "memo-queue.json") == []


def test_completed_stale_archive_transaction_does_not_swallow_future_identical_event(tmp_path: Path) -> None:
    output, state, identity_key = _feedback_scenario(tmp_path)
    assert finalization.attempt_finalization(output, state, "2026-01-01") == "applied"
    archived = read_state(state, "archived_editor_rows")
    row = archived[0]
    pending_digest = hashlib.sha256(json.dumps([row], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    transaction = state / ".feedback-archive-transaction.json"
    write_json(transaction, {"pending_digest": pending_digest, "desired_archive": archived})

    assert finalization.attempt_finalization(output, state, "2026-01-01") == "noop"
    assert not transaction.exists()
    assert record_feedback(identity_key, "needs_deeper_analysis", "Memo note", state, True) == 0
    assert finalization.attempt_finalization(output, state, "2026-01-01") == "applied"
    assert len(read_state(state, "archived_editor_rows")) == 2


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def _capture_sources() -> list[dict[str, object]]:
    return [
        {
            "name": name,
            "family": "award",
            "url": f"https://example.test/{index}",
            "active": True,
            "access_status": "open",
            "extraction_method": "codex-extraction-replay",
            "check_frequency": "weekly",
            "language": "en",
        }
        for index, name in enumerate(("First", "Second", "Third"))
    ]


def test_capture_budget_expiry_records_every_later_eligible_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.json"
    write_json(config, {"sources": _capture_sources()})
    clock = _Clock()

    def capture(config_path: Path, output_dir: Path, source: dict, *, live: bool = False):
        clock.value = 2
        return {"html": "", "source_page": source["url"]}, {}

    monkeypatch.setattr(source_collection, "codex_source_page", capture)
    result = source_collection.prepare_codex_extraction_worklist(
        config, tmp_path / "out", "2026-01-01", monotonic=clock, runtime_budget_seconds=1
    )
    assert result.captured_source_count == 1
    assert len(result.work_items) == 1
    assert result.work_items[0]["source_name"] == "First"
    assert [(run["source_name"], run["status"], run["reason"]) for run in result.source_runs] == [
        ("First", "codex-captured", ""),
        # Issue #595: the reason names the bound that was exhausted, because a
        # derived budget differs per portfolio and a bare reason cannot be read.
        ("Second", "failed", "capture budget exhausted after 1.0s"),
        ("Third", "failed", "capture budget exhausted after 1.0s"),
    ]
    assert result.failed_source_count == 2


class _DeadlineAgent:
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock

    def extract(self, item, capture_path, artifact_path):
        raise AssertionError("deadline-aware method expected")

    def extract_with_timeout(self, item, capture_path, artifact_path, *, timeout_seconds):
        self.clock.value += timeout_seconds
        return ExtractionAttempt(ExtractionStatus.COMMAND_FAILED, False)


def test_fair_share_gives_every_item_initial_attempt(tmp_path: Path) -> None:
    """With fair-share allocation every item gets an initial attempt before
    any source starves. Both sources see command_failed, not budget_exhausted."""
    items = []
    for index in range(2):
        capture = tmp_path / f"capture-{index}.json"
        write_json(capture, {"source_url": f"https://example.test/{index}", "html": ""})
        items.append(
            {
                "source_name": f"Source {index}",
                "source_url": f"https://example.test/{index}",
                "capture_file": capture.name,
                "expected_extraction_file": f"result-{index}.json",
                "operation_id": "op",
                "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
            }
        )
    write_json(tmp_path / "codex-extraction-worklist.json", {"work_items": items})
    clock = _Clock()
    result = ProductionCodexAdapter(
        _DeadlineAgent(clock), monotonic=clock, runtime_budget_seconds=1, max_attempts=1, max_workers=1
    ).fulfill(tmp_path)
    assert len(result["diagnostics"]) == 2
    assert [d["status"] for d in result["diagnostics"]] == ["command_failed", "budget_exhausted"]


@pytest.mark.parametrize("value", ["2026-01-02", "2000-02-29"])
def test_validate_iso_date_accepts_exact_valid_format(value: str) -> None:
    assert _validate_iso_date(value) == value


@pytest.mark.parametrize("value", ["2026-1-02", "2026-01-2", "2026-02-30", "bad"])
def test_validate_iso_date_rejects_malformed_values(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        _validate_iso_date(value)


def test_cli_malformed_run_date_has_clean_argparse_failure() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "autoscout", "run-weekly", "--config", "x", "--output-dir", "y", "--run-date", "bad"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Traceback" not in result.stderr + result.stdout


def test_unexpected_weekly_exception_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    adapters = SimpleNamespace(clock=None, progress=None)
    monkeypatch.setattr(weekly_workflow, "weekly_adapters", lambda root: adapters)
    monkeypatch.setattr(
        cli, "run_weekly_workflow", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("/secret/path"))
    )
    assert _run_weekly_command(tmp_path / "config.json", tmp_path / "out") == 1
    output = capsys.readouterr().out
    assert "/secret/path" not in output
    assert "Traceback" not in output
    assert output.count("?") == 1


@pytest.mark.parametrize(
    "stage",
    [
        "repository-preflight",
        "repository-update",
        "repository-finalize",
        "description-recovery",
        "capture",
        "codex",
        "finalize",
        "pipeline",
        "enrichment",
        "review",
        "health",
        "unknown",
        "",
    ],
)
def test_all_intervention_messages_have_one_action_and_question(stage: str) -> None:
    message = intervention_message(stage)
    assert message.count("Рекомендую") == 1
    assert message.count("?") == 1
    assert "Traceback" not in message
    assert "/Users/" not in message
    assert ".json" not in message


def _healthy_artifacts(output: Path, effective_date: str) -> None:
    write_json(output / "pilot-status.json", {"ready_for_editor": True, "run_date": effective_date})
    write_json(output / "run-summary.json", {"run_date": effective_date, "editor_facing_count": 0})
    write_json(output / "source-runs.json", [])


def test_health_rejects_non_dict_source_mention_cleanly(tmp_path: Path) -> None:
    _healthy_artifacts(tmp_path, "2026-01-01")
    write_json(tmp_path / "source-mentions.json", [1])
    assert run_health(tmp_path, "2026-01-01") == 1


@pytest.mark.parametrize(
    "description,expected_kind",
    [("Достаточно длинное проверенное описание книги для теста.", "codex-generated"), ("", "fallback")],
)
def test_enrichment_provenance_uses_effective_date(description: str, expected_kind: str) -> None:
    history = [{"identity_key": "book|author", "content_description": ""}]
    updated, _ = apply_description_artifacts(
        history,
        [{"identity_key": "book|author", "content_description": description}],
        provenance="codex-generated",
        effective_date="2040-04-05",
    )
    assert updated[0]["description_provenance"]["provenance"] == expected_kind
    assert updated[0]["description_provenance"]["applied_date"] == "2040-04-05"


class _FrozenClock:
    def today(self) -> date:
        return date(2040, 4, 5)


class _RecordingEnrichment:
    def __init__(self) -> None:
        self.effective_date = None
        self.operation_id = None

    def enrich(
        self,
        output_dir: Path,
        state_dir: Path | None = None,
        effective_date: str | None = None,
        *,
        operation_id: str | None = None,
        state_store: Any = None,
    ):
        self.effective_date = effective_date
        self.operation_id = operation_id
        return {"status": "ok", "enriched": 0, "fallback": 0}


def test_weekly_workflow_passes_effective_date_to_enrichment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "config.json"
    output = tmp_path / "out"
    write_json(config, {"sources": []})
    adapters = weekly_workflow.deterministic_adapters()
    enrichment = _RecordingEnrichment()
    adapters.enrichment = enrichment
    adapters.clock = _FrozenClock()
    monkeypatch.setattr(weekly_workflow, "_capture_is_current", lambda *args: True)
    adapters.codex = SimpleNamespace(
        fulfill=lambda output_dir: {"status": "ok", "duration_seconds": 0, "source_timings": []}
    )

    def finalize(output_dir: Path, live_config: Path, effective_date: str):
        write_json(output_dir / "codex-replay-config.json", {"sources": []})
        return {"returncode": 0}

    def pipeline(config_path: Path, output_dir: Path, state_dir: Path | None, effective_date: str, **kwargs: Any):
        write_json(output_dir / "pilot-status.json", {"ready_for_editor": True, "run_date": effective_date})
        write_json(output_dir / "run-summary.json", {"editor_facing_count": 0, "run_date": effective_date})
        write_json(output_dir / "source-runs.json", [])
        return {"returncode": 0, "pipeline_path": PipelinePath.SCOUTED}

    monkeypatch.setattr(weekly_workflow, "_run_finalize_stage", finalize)
    monkeypatch.setattr(weekly_workflow, "_run_pipeline_stage", pipeline)
    monkeypatch.setattr(
        weekly_workflow,
        "_run_review_stage",
        lambda output_dir, *_args, **_kwargs: {
            "returncode": 0,
            "html_exists": True,
            "html_path": str(output_dir / "weekly-review.html"),
        },
    )
    monkeypatch.setattr(weekly_workflow, "_run_health_stage", lambda output_dir, effective_date: {"returncode": 0})
    output.mkdir()
    (output / "weekly-review.html").write_text("<html></html>", encoding="utf-8")
    weekly_workflow.run_weekly_workflow(config, output, tmp_path / "state", adapters=adapters)
    assert enrichment.effective_date == "2040-04-05"
    assert enrichment.operation_id


@pytest.mark.parametrize("applied_date", ["2025-01-01", ""])
def test_health_rejects_new_candidate_stale_or_missing_description_date(tmp_path: Path, applied_date: str) -> None:
    _healthy_artifacts(tmp_path, "2026-01-01")
    write_json(
        tmp_path / "scouting-candidates.json",
        [
            {
                "identity_key": "new|author",
                "update_delta": "new",
                "description_provenance": {"provenance": "codex-generated", "applied_date": applied_date},
            }
        ],
    )
    assert run_health(tmp_path, "2026-01-01") == 1


def test_health_allows_old_provenance_on_unchanged_candidate(tmp_path: Path) -> None:
    _healthy_artifacts(tmp_path, "2026-01-01")
    write_json(
        tmp_path / "scouting-candidates.json",
        [
            {
                "identity_key": "old|author",
                "update_delta": "unchanged",
                "description_provenance": {"provenance": "codex-generated", "applied_date": "2025-01-01"},
            }
        ],
    )
    assert run_health(tmp_path, "2026-01-01") == 0
