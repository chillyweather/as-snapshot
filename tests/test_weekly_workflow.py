"""Tests for the weekly-workflow orchestration seam (issue #265)."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from state_store_support import read_state

from autoscout import description_adjudication as description_adjudication_module
from autoscout import grounded_description_enrichment as grounded_description_enrichment_module
from autoscout import safe_artifacts
from autoscout.codex_recovery import ExtractionAttempt
from autoscout.codex_recovery import ExtractionStatus
from autoscout.codex_recovery import ProductionCodexAdapter
from autoscout.grounded_description import GroundedDescriptionStage
from autoscout.grounded_description import GroundedDescriptionStageService
from autoscout.grounded_description import SynthesisResult
from autoscout.runtime_budget import CAPTURE_RUNTIME_BUDGET_SECONDS
from autoscout.runtime_budget import PORTFOLIO_RUNTIME_BUDGET_SECONDS
from autoscout.weekly_report import WeeklyReportStore
from autoscout.weekly_workflow import WorkflowOutcome
from autoscout.weekly_workflow import _capture_is_current
from autoscout.weekly_workflow import _make_deterministic_adjudication
from autoscout.weekly_workflow import _make_production_enrichment
from autoscout.weekly_workflow import deterministic_adapters
from autoscout.weekly_workflow import run_weekly_workflow


# Issue #541: a production-shaped test must not reach the real Codex CLI.
# weekly_adapters() wires the real Stage 0 probes for production, so these
# tests override them.  The real probe spends one vendor request and reads the
# machine's installed CLI version, which would make the suite non-deterministic.
def _offline_stage_zero_codex_probe() -> dict[str, object]:
    return {
        "status": "pass",
        "reason_code": "codex_healthy",
        "message": "Codex toolchain is ready (offline test probe).",
        "model": "test-model",
        "cli_version": "test-cli",
        "detail": {},
    }


def _offline_stage_zero_discovery_probe() -> dict[str, object]:
    return {
        "status": "pass",
        "reason_code": "discovery_adapter_healthy",
        "message": "Discovery adapter is healthy (offline test probe).",
        "endpoint": "http://127.0.0.1:8899/search?q={query}",
        "detail": {},
    }


class _FixedClock:
    """Deterministic clock that always returns the same date."""

    def __init__(self, reference: date) -> None:
        self._reference = reference

    def today(self) -> date:
        return self._reference


def _write_codex_fixture_config(
    tmp_path: Path,
    *,
    readiness_purpose: str = "recurring-weekly",
    editor_row_target: int = 100,
) -> Path:
    config_path = tmp_path / "config.json"
    config = {
        "run_date": "2026-07-13",
        "readiness_purpose": readiness_purpose,
        "editor_row_target": editor_row_target,
        "pilot_row_min": editor_row_target,
        "pilot_row_max": editor_row_target,
        "pilot_mode": True,
        "sources": [
            {
                "name": "Test Source",
                "family": "award",
                "url": "https://example.test/source",
                "access_status": "open",
                "extraction_method": "codex-extraction-replay",
                "check_frequency": "weekly",
                "language": "en",
                "source_page_fixture_file": "test-capture.json",
                "extraction_response_file": "test-extraction.json",
            }
        ],
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


def _write_capture_fixture(tmp_path: Path, output_dir: Path) -> None:
    capture_file_abs = tmp_path / "test-capture.json"
    capture_file_abs.write_text(
        json.dumps(
            {
                "source_url": "https://example.test/source",
                "html": '<html><body><a href="https://example.test/book">Test</a></body></html> by Test Author',
            }
        ),
        encoding="utf-8",
    )


def _write_extraction_fixture(output_dir: Path) -> Path:
    worklist = json.loads((output_dir / "codex-extraction-worklist.json").read_text(encoding="utf-8"))
    item = worklist["work_items"][0]
    # Write the filename the worklist actually binds. A hardcoded hashless name
    # is never found by finalize_codex_run, so the source was silently withheld
    # and every assertion about it passed for the wrong reason (issue #382).
    result = output_dir / str(item["expected_extraction_file"])
    result.write_text(
        json.dumps(
            {
                "source_name": "Test Source",
                "source_url": "https://example.test/source",
                "operation_id": item["operation_id"],
                "capture_sha256": item["capture_sha256"],
                "entries": [
                    {
                        "title": "Test Book",
                        "primary_authors": ["Test Author"],
                        "source_url": "https://example.test/book",
                        "evidence_url": "https://example.test/book",
                        "evidence_note": "Test note.",
                        "candidate_category": "popular science",
                        "signal_type": "award shortlist",
                        "signal_strength": "shortlist",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return result


class WeeklyWorkflowIntegrationTests(unittest.TestCase):
    def test_production_stage_and_discovery_provider_share_vendor_limiter(self):
        service = _make_production_enrichment(discovery_endpoint="https://search.example.test/search?q={query}")

        self.assertIsNotNone(service.stage.rate_limiter)
        self.assertIs(service.stage.rate_limiter, service.stage.discovery.rate_limiter)

    def test_happy_path_completed_no_attention(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_codex_fixture_config(tmp_path)
            output_dir = tmp_path / "run"
            output_dir.mkdir()

            _write_capture_fixture(tmp_path, output_dir)

            adapters = deterministic_adapters()

            class GroundingSummaryEnrichment:
                def enrich(
                    self, output_dir, state_dir=None, effective_date=None, *, operation_id=None, state_store=None
                ):
                    return {
                        "status": "ok",
                        "grounded_description_counts": {
                            "grounded": 2,
                            "single_source": 1,
                            "attempted_no_description": 3,
                            "not_attempted": 4,
                        },
                        "grounded_description_reason_counts": {
                            "no_results": 2,
                            "subject_content_disagreement": 1,
                        },
                        "description_stage_report": {"attention_required": True},
                    }

            adapters.enrichment = GroundingSummaryEnrichment()
            adapters.capture.prepare(config_path, output_dir, live=False)

            worklist_path = output_dir / "codex-extraction-worklist.json"
            if not worklist_path.exists():
                raise AssertionError("Worklist was not created by capture")

            _write_extraction_fixture(output_dir)

            result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

            self.assertIn(
                result.outcome,
                {WorkflowOutcome.COMPLETED_NO_ATTENTION, WorkflowOutcome.COMPLETED_ATTENTION_REQUIRED},
            )
            self.assertTrue(result.review_available)
            self.assertGreater(len(result.log), 0)
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(
                set(summary["grounded_description_counts"]),
                {"grounded", "single_source", "attempted_no_description", "not_attempted", "evidence_unchanged"},
            )
            self.assertEqual(
                summary["grounded_description_counts"],
                {
                    "grounded": 2,
                    "single_source": 1,
                    "attempted_no_description": 3,
                    "not_attempted": 4,
                    "evidence_unchanged": 0,
                },
            )
            self.assertEqual(
                summary["grounded_description_reason_counts"],
                {"no_results": 2, "subject_content_disagreement": 1},
            )
            self.assertEqual(summary["grounded_description_counts"], result.grounded_description_counts)
            self.assertEqual(summary["grounded_description_reason_counts"], result.grounded_description_reason_counts)
            # Issue #641: problems are labelled by stage. The adjudication
            # stage adds none here: it declined a backlog candidate, which is
            # the backlog staying the backlog and never an attention fact.
            self.assertEqual(
                result.description_stage_problems,
                [
                    {
                        "stage": "grounded",
                        "status": "attention_required",
                        "reason": "description_stage_attention_required",
                    },
                ],
            )

    def test_still_fallback_backlog_without_current_events_does_not_require_attention(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_codex_fixture_config(tmp_path, editor_row_target=1)
            output_dir = tmp_path / "run"
            output_dir.mkdir()
            _write_capture_fixture(tmp_path, output_dir)
            adapters = deterministic_adapters()
            service = GroundedDescriptionStageService(GroundedDescriptionStage(stage_budget_seconds=0.0))

            class BacklogOnlyEnrichment:
                def enrich(
                    self, output_dir, state_dir=None, effective_date=None, *, operation_id=None, state_store=None
                ):
                    result = service.enrich(
                        output_dir,
                        state_dir,
                        effective_date,
                        operation_id=operation_id,
                    )
                    candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
                    for candidate in candidates:
                        candidate["content_description"] = "Это проверенное описание книги о памяти и обществе."
                        candidate["description_provenance"] = {"provenance": "grounded"}
                    (output_dir / "scouting-candidates.json").write_text(json.dumps(candidates), encoding="utf-8")
                    summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
                    coverage = {
                        "validated_count": 1,
                        "total_count": 1,
                        "fallback_count": 0,
                        "fallback_ids": [],
                    }
                    summary["description_coverage"] = coverage
                    pilot = json.loads((output_dir / "pilot-status.json").read_text(encoding="utf-8"))
                    pilot.setdefault("readiness_basis", {})["readiness_purpose"] = "recurring-weekly"
                    pilot["ready_for_editor"] = True
                    (output_dir / "pilot-status.json").write_text(json.dumps(pilot), encoding="utf-8")
                    (output_dir / "description-coverage.json").write_text(json.dumps(coverage), encoding="utf-8")
                    (output_dir / "run-summary.json").write_text(json.dumps(summary), encoding="utf-8")
                    return result

            adapters.enrichment = BacklogOnlyEnrichment()
            # The adjudication stage stays wired here on purpose: it reads
            # the same standing backlog, and this test is what proves its
            # declines do not turn a backlog into attention (issue #641).
            adapters.capture.prepare(config_path, output_dir, live=False)
            _write_extraction_fixture(output_dir)

            with patch("autoscout.pipeline.refresh_post_enrichment_artifacts", return_value={"status": "ok"}):
                result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

            self.assertEqual(result.outcome, WorkflowOutcome.COMPLETED_NO_ATTENTION)
            self.assertEqual(result.readiness_facts["readiness_purpose"], "recurring-weekly")
            self.assertEqual(result.description_stage_problems, [])
            self.assertEqual(result.description_stage_report["counts"]["still_fallback"], 1)
            self.assertEqual(result.description_stage_report["counts"]["failed"], 0)
            self.assertEqual(result.description_stage_report["counts"]["ambiguous"], 0)
            self.assertFalse(result.readiness_facts["attention_required"])
            stages = {entry["stage"] for entry in result.log}
            self.assertIn("review", stages)
            self.assertIn("health", stages)
            self.assertIn("repository-finalize", stages)

    def test_invalid_enrichment_input_stops_before_post_enrichment_stages(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_codex_fixture_config(tmp_path, editor_row_target=1)
            output_dir = tmp_path / "run"
            output_dir.mkdir()
            _write_capture_fixture(tmp_path, output_dir)
            adapters = deterministic_adapters()
            service = GroundedDescriptionStageService()

            class InvalidInputEnrichment:
                def enrich(
                    self, output_dir, state_dir=None, effective_date=None, *, operation_id=None, state_store=None
                ):
                    candidates_path = output_dir / "scouting-candidates.json"
                    candidates_path.write_text(json.dumps(["tck rejects"]), encoding="utf-8")
                    return service.enrich(
                        output_dir,
                        state_dir,
                        effective_date,
                        operation_id=operation_id,
                    )

            adapters.enrichment = InvalidInputEnrichment()
            adapters.capture.prepare(config_path, output_dir, live=False)
            _write_extraction_fixture(output_dir)

            with patch("autoscout.pipeline.refresh_post_enrichment_artifacts") as refresh:
                result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

            self.assertEqual(result.outcome, WorkflowOutcome.INTERVENTION_REQUIRED)
            self.assertEqual(result.failed_stage, "description-input")
            self.assertEqual(
                json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8")), ["tck rejects"]
            )
            failure_entries = [entry for entry in result.log if entry["stage"] == "description-input"]
            self.assertEqual(len(failure_entries), 1)
            self.assertEqual(failure_entries[0]["reason"], "invalid_scouting_candidates")
            refresh.assert_not_called()
            stages = {entry["stage"] for entry in result.log}
            self.assertNotIn("post-enrichment-refresh", stages)
            self.assertNotIn("review", stages)
            self.assertNotIn("health", stages)
            self.assertNotIn("repository-finalize", stages)
            persisted_log = json.loads((output_dir / "workflow-log.json").read_text(encoding="utf-8"))
            persisted_failures = [entry for entry in persisted_log if entry["stage"] == "description-input"]
            self.assertEqual(persisted_failures[0]["reason"], "invalid_scouting_candidates")
            serialized_failures = json.dumps(persisted_failures)
            self.assertNotIn("tck rejects", serialized_failures)
            self.assertNotIn("ValueError", serialized_failures)
            self.assertNotIn(str(output_dir), serialized_failures)
            from autoscout.russian_workflow import render_workflow_result

            operator_message = render_workflow_result(result, include_progress=False)
            self.assertIn("Димой", operator_message)
            self.assertIn("диагностики", operator_message)
            self.assertNotIn("Повторить поиск", operator_message)
            self.assertEqual(operator_message.count("?"), 1)
            self.assertNotIn("tck rejects", operator_message)
            self.assertNotIn(str(output_dir), operator_message)

    def test_unknown_description_input_reason_is_replaced_with_bounded_reason(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_codex_fixture_config(tmp_path, editor_row_target=1)
            output_dir = tmp_path / "run"
            output_dir.mkdir()
            _write_capture_fixture(tmp_path, output_dir)
            adapters = deterministic_adapters()

            class UnboundedInputEnrichment:
                def enrich(self, *_args, **_kwargs):
                    return {
                        "status": "input_failed",
                        "reason": "raw adapter exception at /tmp/private-input.json",
                    }

            adapters.enrichment = UnboundedInputEnrichment()
            adapters.capture.prepare(config_path, output_dir, live=False)
            _write_extraction_fixture(output_dir)

            with patch("autoscout.pipeline.refresh_post_enrichment_artifacts") as refresh:
                result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

            self.assertEqual(result.outcome, WorkflowOutcome.INTERVENTION_REQUIRED)
            self.assertEqual(result.failed_stage, "description-input")
            failure_entries = [entry for entry in result.log if entry["stage"] == "description-input"]
            self.assertEqual(failure_entries[0]["reason"], "description_input_failure")
            self.assertNotIn("raw adapter exception", json.dumps(result.log))
            self.assertNotIn("/tmp/private-input.json", json.dumps(result.log))
            refresh.assert_not_called()

    def test_grounded_enrichment_persists_before_review_and_stage_failure_is_distinct(self):
        class ReplayDiscovery:
            def discover(self, candidate, **_kwargs):
                return [
                    {
                        "url": f"https://publisher.example.test/{candidate['candidate_id']}",
                        "source_kind": "publisher",
                        "source_family": "publisher_copy",
                        "owner_id": "publisher-owner",
                        "classification": "classified",
                        "title": candidate["title"],
                        "primary_authors": candidate["primary_authors"],
                    },
                    {
                        "url": f"https://review.example.test/{candidate['candidate_id']}",
                        "source_kind": "review",
                        "source_family": "review",
                        "owner_id": "review-owner",
                        "classification": "classified",
                        "title": candidate["title"],
                        "primary_authors": candidate["primary_authors"],
                    },
                ]

        class ReplayFetch:
            def fetch(self, result, **kwargs):
                return {
                    "url": result.url,
                    "source_kind": result.source_kind,
                    "source_family": result.source_family,
                    "owner_id": result.owner_id,
                    "source_language": "en",
                    "title": result.title,
                    "primary_authors": list(result.primary_authors),
                    "subject_text": "A bounded account of memory and public life.",
                    "checked_date": kwargs.get("checked_date", "2026-07-13"),
                    "classification": "classified",
                }

        class ReplaySynthesizer:
            def synthesize(self, candidate, sources):
                del sources
                return SynthesisResult(
                    f"Книга {candidate['title']} о памяти и общественной жизни. "
                    "Второй фрагмент раскрывает документальную основу, называет проверенные "
                    "страницы и уточняет редакционный ракурс книги. "
                    "Третье предложение закрепляет вывод и показывает, почему книга попала в обзор."
                )

        def grounded_service():
            return GroundedDescriptionStageService(
                GroundedDescriptionStage(
                    discovery=ReplayDiscovery(),
                    fetch=ReplayFetch(),
                    synthesizer=ReplaySynthesizer(),
                    max_workers=1,
                )
            )

        def prepare_workspace(root: Path):
            config_path = _write_codex_fixture_config(root)
            output_dir = root / "run"
            state_dir = root / "state"
            output_dir.mkdir()
            state_dir.mkdir()
            _write_capture_fixture(root, output_dir)
            adapters = deterministic_adapters()
            adapters.enrichment = grounded_service()
            adapters.capture.prepare(config_path, output_dir, live=False)
            _write_extraction_fixture(output_dir)
            return config_path, output_dir, state_dir, adapters

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path, output_dir, state_dir, adapters = prepare_workspace(root)
            result = run_weekly_workflow(config_path, output_dir, state_dir, adapters=adapters)

            self.assertTrue(result.review_available)
            self.assertIn("review", {entry["stage"] for entry in result.log})
            self.assertIn("health", {entry["stage"] for entry in result.log})
            history = read_state(state_dir, "candidate_history")
            generated = next(item for item in history if item.get("title") == "Test Book")
            self.assertTrue(str(generated.get("content_description", "")).startswith("Книга Test Book"))
            self.assertEqual(generated["description_provenance"]["provenance"], "grounded")
            self.assertEqual(generated["description_provenance"]["applied_date"], "2000-01-01")
            self.assertIn("Книга Test Book", (output_dir / "weekly-review.html").read_text(encoding="utf-8"))
            self.assertEqual(result.description_stage_report["counts"]["applied"], 1)
            self.assertEqual(
                json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))["description_stage_report"][
                    "counts"
                ]["applied"],
                1,
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path, output_dir, state_dir, adapters = prepare_workspace(root)

            class BrokenStage:
                def run(self, *_args, **_kwargs):
                    raise RuntimeError("hidden stage failure")

            adapters.enrichment = GroundedDescriptionStageService(BrokenStage())
            result = run_weekly_workflow(config_path, output_dir, state_dir, adapters=adapters)
            stages = {entry["stage"]: entry for entry in result.log}
            self.assertEqual(result.outcome, WorkflowOutcome.COMPLETED_ATTENTION_REQUIRED)
            self.assertEqual(stages["enrichment"]["status"], "failed")
            self.assertTrue(result.review_available)
            self.assertIn("review", stages)
            self.assertIn("health", stages)
            self.assertTrue(result.description_stage_problems)
            self.assertEqual(result.description_stage_report["counts"]["failed"], 1)
            from autoscout.russian_workflow import render_workflow_result

            self.assertIn("Этап проверки описаний", render_workflow_result(result, include_progress=False))
            self.assertNotIn("hidden stage failure", json.dumps(result.log))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path, output_dir, state_dir, adapters = prepare_workspace(root)
            adapters.enrichment = grounded_service()
            original_replace = grounded_description_enrichment_module._staged_replace
            calls = 0

            def failing_replace(source: Path, destination: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("description persistence failure must stay bounded")
                original_replace(source, destination)

            grounded_description_enrichment_module._staged_replace = failing_replace
            try:
                result = run_weekly_workflow(config_path, output_dir, state_dir, adapters=adapters)
            finally:
                grounded_description_enrichment_module._staged_replace = original_replace

            self.assertEqual(result.outcome, WorkflowOutcome.INTERVENTION_REQUIRED)
            self.assertEqual(result.failed_stage, "description-persistence")
            persistence_entries = [entry for entry in result.log if entry.get("stage") == "description-persistence"]
            self.assertEqual(len(persistence_entries), 1)
            self.assertEqual(persistence_entries[0]["reason"], "description_persistence_failure")
            self.assertNotIn("description persistence failure must stay bounded", json.dumps(persistence_entries))
            self.assertNotIn(str(output_dir), json.dumps(persistence_entries))
            stages = {entry["stage"] for entry in result.log}
            self.assertNotIn("review", stages)
            self.assertNotIn("health", stages)
            self.assertNotIn("repository-finalize", stages)
            self.assertFalse(WeeklyReportStore(output_dir).transaction_marker_path().exists())
            persisted_log = json.loads((output_dir / "workflow-log.json").read_text(encoding="utf-8"))
            persisted_failures = [entry for entry in persisted_log if entry.get("stage") == "description-persistence"]
            self.assertEqual(len(persisted_failures), 1)
            self.assertEqual(persisted_failures[0]["reason"], "description_persistence_failure")
            self.assertEqual(persisted_log[-1]["stage"], "terminal")
            self.assertNotIn("description persistence failure must stay bounded", json.dumps(persisted_failures))
            self.assertNotIn(str(output_dir), json.dumps(persisted_failures))
            from autoscout.russian_workflow import intervention_message

            message = intervention_message(result.failed_stage or "")
            self.assertIn("результаты описаний", message)
            self.assertNotIn("grounded-description-stage.json", message)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path, output_dir, state_dir, adapters = prepare_workspace(root)
            adapters.enrichment = grounded_service()
            original_replace = grounded_description_enrichment_module._staged_replace
            original_recovery = safe_artifacts._trusted_recovery_replace
            destinations = [
                output_dir / "grounded-description-stage.json",
                state_dir / "grounded-description-queue.json",
                state_dir / "candidate-history.json",
                output_dir / "candidate-history.json",
                output_dir / "scouting-candidates.json",
            ]
            calls = 0

            # Canonical state travels in the State Store transaction around
            # the bundle (#1071), so the recovery that fails is the one of the
            # run's history copy, which the bundle replaced before it failed.
            def failing_replace(source: Path, destination: Path) -> None:
                nonlocal calls
                calls += 1
                if destination == destinations[4]:
                    raise OSError("weekly recovery failure must stay bounded")
                original_replace(source, destination)

            def failing_recovery(source: Path, destination: Path) -> None:
                if destination == destinations[3]:
                    raise OSError("trusted weekly recovery failure must stay bounded")
                original_recovery(source, destination)

            grounded_description_enrichment_module._staged_replace = failing_replace
            safe_artifacts._trusted_recovery_replace = failing_recovery
            try:
                result = run_weekly_workflow(config_path, output_dir, state_dir, adapters=adapters)
            finally:
                grounded_description_enrichment_module._staged_replace = original_replace
                safe_artifacts._trusted_recovery_replace = original_recovery

            self.assertEqual(result.outcome, WorkflowOutcome.INTERVENTION_REQUIRED)
            self.assertEqual(result.failed_stage, "description-recovery")
            recovery_entries = [entry for entry in result.log if entry.get("stage") == "description-recovery"]
            self.assertEqual(len(recovery_entries), 1)
            self.assertEqual(recovery_entries[0]["reason"], "description_persistence_recovery_failure")
            self.assertNotIn("weekly recovery failure must stay bounded", json.dumps(recovery_entries))
            self.assertNotIn("trusted weekly recovery failure must stay bounded", json.dumps(recovery_entries))
            self.assertNotIn(str(output_dir), json.dumps(recovery_entries))
            stages = {entry["stage"] for entry in result.log}
            self.assertNotIn("review", stages)
            self.assertNotIn("health", stages)
            self.assertNotIn("repository-finalize", stages)
            self.assertTrue(
                any(
                    path.name.endswith(".backup")
                    for directory in (output_dir, state_dir)
                    for path in directory.iterdir()
                )
            )
            persisted_log = json.loads((output_dir / "workflow-log.json").read_text(encoding="utf-8"))
            persisted_failures = [entry for entry in persisted_log if entry.get("stage") == "description-recovery"]
            self.assertEqual(len(persisted_failures), 1)
            self.assertEqual(persisted_failures[0]["reason"], "description_persistence_recovery_failure")
            from autoscout.russian_workflow import intervention_message

            message = intervention_message(result.failed_stage or "")
            self.assertIn("Димой", message)
            self.assertIn("диагностики", message)
            self.assertNotIn("Повторить поиск", message)
            self.assertEqual(message.count("?"), 1)

    def test_missing_worklist_is_intervention(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_codex_fixture_config(tmp_path)
            output_dir = tmp_path / "run"
            output_dir.mkdir()

            adapters = deterministic_adapters()
            result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

            self.assertEqual(result.outcome, WorkflowOutcome.INTERVENTION_REQUIRED)
            self.assertEqual(result.failed_stage, "codex")

    def test_missing_extraction_artifacts_completes_with_attention(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_codex_fixture_config(tmp_path)
            output_dir = tmp_path / "run"
            output_dir.mkdir()

            _write_capture_fixture(tmp_path, output_dir)

            adapters = deterministic_adapters()
            adapters.capture.prepare(config_path, output_dir, live=False)

            result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

            self.assertIn(
                result.outcome,
                {WorkflowOutcome.COMPLETED_ATTENTION_REQUIRED, WorkflowOutcome.COMPLETED_NO_ATTENTION},
            )

    def test_failed_extraction_timings_survive_in_workflow_log(self):
        class FailedCodex:
            def fulfill(self, output_dir):
                return {
                    "status": "failed",
                    "reason": "no_extraction_produced",
                    "duration_seconds": 3.0,
                    "runtime_budget_seconds": 12,
                    "source_timings": [
                        {"source_name": "Failed Source", "duration_seconds": 3.0, "status": "not_ready"}
                    ],
                    "diagnostics": [{"source_name": "Failed Source", "status": "timeout", "duration_seconds": 3.0}],
                }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_codex_fixture_config(tmp_path)
            output_dir = tmp_path / "run"
            output_dir.mkdir()
            _write_capture_fixture(tmp_path, output_dir)
            adapters = deterministic_adapters()
            adapters.codex = FailedCodex()

            result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

            self.assertEqual(result.outcome, WorkflowOutcome.INTERVENTION_REQUIRED)
            persisted = json.loads((output_dir / "workflow-log.json").read_text(encoding="utf-8"))
            codex_entries = [entry for entry in persisted if entry["stage"] == "codex"]
            self.assertEqual(len(codex_entries), 1)
            self.assertEqual(codex_entries[0]["detail"]["duration_seconds"], 3.0)
            summary = next(entry for entry in persisted if entry["stage"] == "source-processing")["detail"]
            self.assertEqual(summary["runtime_budget_seconds"], 162)
            self.assertEqual(summary["extraction"]["source_timings"][0]["source_name"], "Failed Source")

    def test_partial_timeout_is_persisted_and_rendered_as_one_named_caveat(self):
        from autoscout.russian_workflow import render_workflow_result

        class PartialCodex:
            def fulfill(self, output_dir):
                _write_extraction_fixture(output_dir)
                return {
                    "status": "ok",
                    "ready_items": 1,
                    "total_items": 2,
                    "duration_seconds": 2.0,
                    "runtime_budget_seconds": PORTFOLIO_RUNTIME_BUDGET_SECONDS,
                    "source_timings": [
                        {"source_name": "Test Source", "duration_seconds": 1.0, "status": "ready"},
                        {"source_name": "Slow Source", "duration_seconds": 1.0, "status": "not_ready"},
                    ],
                    "diagnostics": [
                        {
                            "source_name": "Slow Source",
                            "status": "timeout",
                            "duration_seconds": 1.0,
                            "api_key": "must-not-persist",
                        }
                    ],
                }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_codex_fixture_config(tmp_path)
            output_dir = tmp_path / "run"
            output_dir.mkdir()
            _write_capture_fixture(tmp_path, output_dir)
            adapters = deterministic_adapters()
            adapters.codex = PartialCodex()

            result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

            self.assertEqual(result.outcome, WorkflowOutcome.COMPLETED_ATTENTION_REQUIRED)
            self.assertEqual([p["source_name"] for p in result.source_problems], ["Slow Source"])
            persisted = json.loads((output_dir / "workflow-log.json").read_text(encoding="utf-8"))
            summary = next(entry for entry in persisted if entry["stage"] == "source-processing")["detail"]
            self.assertEqual(
                summary["runtime_budget_seconds"],
                CAPTURE_RUNTIME_BUDGET_SECONDS + PORTFOLIO_RUNTIME_BUDGET_SECONDS,
            )
            self.assertIn("source_timings", summary["capture"])
            self.assertEqual(len(summary["extraction"]["source_timings"]), 2)
            self.assertNotIn("must-not-persist", json.dumps(persisted))

            rendered = render_workflow_result(result)
            self.assertEqual(rendered.count("⚠"), 1)
            self.assertEqual(rendered.count("Slow Source"), 1)
            for forbidden in ("timeout", "retry", "page size", "порядок", "размер страницы"):
                self.assertNotIn(forbidden, rendered.lower())

    def test_partial_signal_metadata_mismatch_remains_a_named_source_problem(self):
        class PartialCodex:
            def fulfill(self, output_dir):
                _write_extraction_fixture(output_dir)
                return {
                    "status": "ok",
                    "ready_items": 1,
                    "total_items": 2,
                    "duration_seconds": 2.0,
                    "runtime_budget_seconds": PORTFOLIO_RUNTIME_BUDGET_SECONDS,
                    "source_timings": [
                        {"source_name": "Test Source", "duration_seconds": 1.0, "status": "ready"},
                        {"source_name": "Magnesia Litera", "duration_seconds": 1.0, "status": "not_ready"},
                    ],
                    "diagnostics": [
                        {
                            "source_name": "Magnesia Litera",
                            "status": "invalid_signal_metadata",
                            "duration_seconds": 1.0,
                        }
                    ],
                }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_codex_fixture_config(tmp_path)
            output_dir = tmp_path / "run"
            output_dir.mkdir()
            _write_capture_fixture(tmp_path, output_dir)
            adapters = deterministic_adapters()
            adapters.codex = PartialCodex()

            result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

            self.assertEqual(result.outcome, WorkflowOutcome.COMPLETED_ATTENTION_REQUIRED)
            self.assertEqual(
                result.source_problems,
                [
                    {
                        "source_name": "Magnesia Litera",
                        "status": "failed",
                        "reason": "invalid_signal_metadata",
                    }
                ],
            )

    def test_calibration_below_target_completed_with_attention(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_codex_fixture_config(
                tmp_path,
                readiness_purpose="seed-calibration",
                editor_row_target=100,
            )
            output_dir = tmp_path / "run"
            output_dir.mkdir()

            _write_capture_fixture(tmp_path, output_dir)

            adapters = deterministic_adapters()
            adapters.capture.prepare(config_path, output_dir, live=False)
            _write_extraction_fixture(output_dir)

            result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

            self.assertIn(
                result.outcome,
                {WorkflowOutcome.COMPLETED_NO_ATTENTION, WorkflowOutcome.COMPLETED_ATTENTION_REQUIRED},
            )
            self.assertEqual(result.readiness_facts["readiness_purpose"], "seed-calibration")

    def test_production_shaped_e2e_workflow_with_local_git_finalization(self):
        """End-to-end acceptance test invoking the public ``run-weekly`` CLI
        path against a temporary local Git repository / bare remote with
        observable commit and push effects."""

        class FakeExtractionAgent:
            def extract(self, item, capture_path, artifact_path):
                artifact_path.write_text(
                    json.dumps(
                        {
                            "source_name": item["source_name"],
                            "source_url": item["source_url"],
                            "operation_id": item["operation_id"],
                            "capture_sha256": item["capture_sha256"],
                            "entries": [
                                {
                                    "title": "Test Book",
                                    "primary_authors": ["Test Author"],
                                    "source_url": "https://example.test/book",
                                    "evidence_url": "https://example.test/book",
                                    "evidence_note": "Listed book.",
                                    "candidate_category": "popular science",
                                    "signal_type": "award shortlist",
                                    "signal_strength": "shortlist",
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                return ExtractionAttempt(ExtractionStatus.PRODUCED, True, 0)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            import subprocess

            # ── Bare remote ──
            bare_remote = tmp_path / "bare.git"
            bare_remote.mkdir()
            subprocess.run(
                ["git", "init", "--bare", "--initial-branch=main", str(bare_remote)],
                check=True,
                capture_output=True,
            )

            # ── Working repo with local identity ──
            repo_dir = tmp_path / "repo"
            repo_dir.mkdir()
            subprocess.run(
                ["git", "-C", str(repo_dir), "init", "--initial-branch=main"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repo_dir), "config", "user.name", "AutoScout CI"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repo_dir), "config", "user.email", "ci@autoscout.test"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repo_dir), "remote", "add", "origin", str(bare_remote)],
                check=True,
                capture_output=True,
            )
            (repo_dir / "README.md").write_text("# auto-scout test repo\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(repo_dir), "add", "README.md"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repo_dir), "commit", "-m", "initial commit"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repo_dir), "push", "--set-upstream", "origin", "main"],
                check=True,
                capture_output=True,
            )

            # ── Fixture config and capture inside the working repo ──
            config_path = _write_codex_fixture_config(repo_dir)
            output_dir = repo_dir / "run"
            output_dir.mkdir()
            _write_capture_fixture(repo_dir, output_dir)
            subprocess.run(
                ["git", "-C", str(repo_dir), "add", "config.json", "test-capture.json"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repo_dir), "commit", "-m", "test fixtures"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repo_dir), "push", "origin", "main"],
                check=True,
                capture_output=True,
            )

            # ── Build adapter bundle via ``weekly_adapters``, replace externals ──
            from autoscout.git_envelope import GitRepositoryAdapter
            from autoscout.weekly_workflow import _DeterministicCaptureAdapter
            from autoscout.weekly_workflow import weekly_adapters

            # Deterministic fake description enrichment agent so the test
            # never invokes the real Codex CLI/model.
            class FakeDescriptionAgent:
                def generate(self, work_items):
                    result = {}
                    for wi in work_items:
                        ik = str(wi.get("identity_key", ""))
                        result[ik] = (
                            "Увлекательное исследование современной науки, "
                            "написанное доступным языком для широкой аудитории."
                        )
                    return result

            adapters = weekly_adapters(repo_dir)
            adapters.stage_zero_codex_probe = _offline_stage_zero_codex_probe
            adapters.stage_zero_discovery_probe = _offline_stage_zero_discovery_probe
            adapters.capture = _DeterministicCaptureAdapter()
            adapters.codex = ProductionCodexAdapter(agent=FakeExtractionAgent())
            adapters.repository = GitRepositoryAdapter(
                repo_dir,
                expected_branch="main",
                expected_remote_url=str(bare_remote),
            )
            adapters.clock = _FixedClock(reference=date(2026, 7, 13))

            from autoscout.description_enrichment import DescriptionEnrichmentService

            adapters.enrichment = DescriptionEnrichmentService(agent=FakeDescriptionAgent())
            # weekly_adapters() wires the production adjudication stage, which
            # shells out to the real Codex CLI and fetches live pages. The
            # replay adapter with a no-network transport keeps the run offline
            # and the enriched description above untouched.
            adapters.adjudication = _make_deterministic_adjudication()
            # ISBN completion runs offline here: source links and list pages only (#979).
            from autoscout.isbn_completion import ProductionIsbnCompletionStage

            adapters.isbn_completion = ProductionIsbnCompletionStage(getter_factory=lambda: None)

            opened_calls: list[str] = []

            class _FakeOpenAdapter:
                def __init__(self, progress=None):
                    self.progress = progress

                def announce(self, stage):
                    if self.progress is not None:
                        self.progress(stage)

                def open_review(self, review_path):
                    opened_calls.append(str(review_path))
                    return {"status": "ok", "opened": True, "path": str(review_path)}

            adapters.presentation = _FakeOpenAdapter()

            # ── Baseline remote ref immediately before CLI invocation ──
            before_ref = subprocess.run(
                ["git", "-C", str(bare_remote), "rev-parse", "refs/heads/main"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

            # ── Patch the CLI factory so ``main`` receives our bundle ──
            from unittest import mock

            def factory(repo_path, progress=None, extraction_progress=None):
                adapters.presentation = _FakeOpenAdapter(progress)
                return adapters

            with mock.patch(
                "autoscout.weekly_workflow.weekly_adapters",
                side_effect=factory,
            ) as factory_mock:
                from io import StringIO

                from autoscout.__main__ import main

                captured_stdout = StringIO()
                with mock.patch("sys.stdout", captured_stdout):
                    rc = main(
                        [
                            "run-weekly",
                            "--config",
                            str(config_path),
                            "--output-dir",
                            str(output_dir),
                        ]
                    )

            # ── Assertions ──

            # 1. CLI return code signals success.
            self.assertEqual(rc, 0)

            # 2. CLI factory was called with the expected repo root.
            factory_mock.assert_called_once()
            self.assertEqual(factory_mock.call_args.args[0], repo_dir.resolve())
            self.assertIn("progress", factory_mock.call_args.kwargs)

            # 3. Russian editor-visible stdout.
            stdout = captured_stdout.getvalue()
            self.assertIn("Запускаю еженедельный поиск книг", stdout)
            russian_milestones = [
                "Проверяю источники",
                "Обрабатываю полученные данные",
                "Подготавливаю результаты поиска",
                "Формирую список кандидатов для обзора",
                "Проверяю качество результатов",
                "Готовлю итоговый обзор",
            ]
            for msg in russian_milestones:
                self.assertIn(msg, stdout, f"Missing Russian milestone: {msg}")
            self.assertIn("завершён", stdout)
            # Normal output omits implementation vocabulary.
            for term in ("JSON", "CSV", "Codex", "codex", "capture", "replay", "pipeline"):
                self.assertNotIn(term, stdout, f"Implementation term '{term}' leaked into editor output")

            # 4. Opening request was made.
            self.assertGreater(len(opened_calls), 0)

            # 5. Review HTML exists and is non-empty.
            review_path = output_dir / "weekly-review.html"
            self.assertTrue(review_path.exists(), f"Review HTML missing: {review_path}")
            self.assertGreater(review_path.stat().st_size, 0, "Review HTML is empty")

            # 6. Persisted terminal log.
            log_path = output_dir / "workflow-log.json"
            self.assertTrue(log_path.exists(), "workflow-log.json not persisted")
            persisted_log = json.loads(log_path.read_text(encoding="utf-8"))
            self.assertGreater(len(persisted_log), 0)
            self.assertEqual(persisted_log[-1]["stage"], "terminal")

            # 7. Pipeline artifacts exist on disk.
            for artifact_name in ["run-summary.json", "pilot-status.json"]:
                artifact_path = output_dir / artifact_name
                self.assertTrue(artifact_path.exists(), f"Missing artifact: {artifact_name}")

            # 8. Observable commit and push effects — the bare remote
            #    main ref must have advanced beyond the initial commit.
            after_ref = subprocess.run(
                ["git", "-C", str(bare_remote), "rev-parse", "refs/heads/main"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertNotEqual(after_ref, before_ref, "Bare remote ref did not advance")

            # Assert a generated approved output artifact is present in the
            # pushed remote tree.
            remote_files = subprocess.run(
                ["git", "-C", str(bare_remote), "ls-tree", "--name-only", "-r", "main"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
            self.assertIn(
                "run/weekly-review.html",
                remote_files,
                "weekly-review.html missing from pushed remote tree",
            )
            self.assertIn(
                "run/workflow-log.json",
                remote_files,
                "workflow-log.json missing from pushed remote tree",
            )

            # 9. No rejected artifact debris.
            self.assertFalse(list(output_dir.glob("*.rejected-*")))

            # 10. Enriched description in pushed candidate-history.json.
            self.assertIn(
                "run/candidate-history.json",
                remote_files,
                "candidate-history.json missing from pushed remote tree",
            )
            # Verify the enriched description is in the pushed review HTML
            # and that the enrichment diagnostic is in the remote tree.
            self.assertIn(
                "run/description-enrichment.json",
                remote_files,
                "description-enrichment.json missing from pushed remote tree",
            )
            self.assertIn(
                "run/description-coverage.json",
                remote_files,
                "description-coverage.json missing from pushed remote tree",
            )
            # 11. Read pushed blobs from the remote branch/tree and assert
            #     description coverage, enriched history, run-summary, final
            #     review, and sanitized diagnostic reflect the generated
            #     Russian description and full coverage in the pushed state.

            def _read_pushed_blob(path_in_run: str) -> str:
                """Read a blob from the pushed bare remote via ``git show``."""
                return subprocess.run(
                    ["git", "--git-dir", str(bare_remote), "show", f"main:run/{path_in_run}"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout

            # Pushed candidate-history.json must contain the enriched description.
            pushed_history = json.loads(_read_pushed_blob("candidate-history.json"))
            pushed_by_id = {str(rec.get("identity_key", "")): rec for rec in pushed_history if isinstance(rec, dict)}
            pushed_rec = pushed_by_id.get("test book|test author")
            self.assertIsNotNone(pushed_rec, "Test book missing from pushed candidate-history")
            self.assertEqual(
                pushed_rec.get("content_description", ""),
                "Увлекательное исследование современной науки, написанное доступным языком для широкой аудитории.",
            )
            pushed_prov = pushed_rec.get("description_provenance")
            self.assertEqual(
                pushed_prov.get("provenance") if isinstance(pushed_prov, dict) else None,
                "codex-generated",
            )

            # Pushed run-summary must have full description coverage (no fallbacks).
            pushed_rs = json.loads(_read_pushed_blob("run-summary.json"))
            pushed_dc = pushed_rs.get("description_coverage", {})
            self.assertEqual(pushed_dc.get("fallback_count", -1), 0, "Pushed run-summary has unexpected fallbacks")
            self.assertGreater(
                pushed_dc.get("validated_count", 0), 0, "Pushed run-summary has no validated descriptions"
            )

            # Pushed sanitized diagnostic: structure and facts, no raw prose.
            pushed_diag = json.loads(_read_pushed_blob("description-enrichment.json"))
            self.assertEqual(pushed_diag.get("stage"), "description-enrichment")
            self.assertIn("test book|test author", pushed_diag.get("applied_ids", []))
            self.assertEqual(len(pushed_diag.get("fallback_ids", [])), 0)
            # Diagnostic only stores IDs, counts, and attempt metadata — no raw
            # description prose. Validate structural facts.
            diag_str = json.dumps(pushed_diag)
            self.assertIn('"requested_ids"', diag_str)
            self.assertIn('"applied_ids"', diag_str)
            self.assertIn('"fallback_ids"', diag_str)
            self.assertIn('"attempt_log"', diag_str)
            self.assertIn('"attempts"', diag_str)
            # pushed diagnostic reflects 1 successful attempt with 1 applied.
            self.assertEqual(pushed_diag.get("attempts"), 1)
            self.assertEqual(pushed_diag.get("applied"), 1)

            # Pushed final review HTML contains the enriched description.
            pushed_review = _read_pushed_blob("weekly-review.html")
            self.assertIn("Увлекательное исследование", pushed_review)

    def test_deterministic_workflow_produces_identical_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_codex_fixture_config(tmp_path)

            output_a = tmp_path / "run-a"
            output_b = tmp_path / "run-b"
            output_a.mkdir()
            output_b.mkdir()

            _write_capture_fixture(tmp_path, output_a)
            _write_capture_fixture(tmp_path, output_b)

            adapters_a = deterministic_adapters()
            adapters_b = deterministic_adapters()

            adapters_a.capture.prepare(config_path, output_a, live=False)
            adapters_b.capture.prepare(config_path, output_b, live=False)

            _write_extraction_fixture(output_a)
            _write_extraction_fixture(output_b)

            # run-summary.json records the measured latency of each adjudication
            # call. The stage service does not forward its call timer, so the
            # call timer is pinned at the module seam: both runs then record
            # the same fixed latency whatever the machine load.
            real_adjudicate = description_adjudication_module.adjudicate_descriptions

            def adjudicate_with_fixed_call_timer(*args, **kwargs):
                return real_adjudicate(*args, call_monotonic=lambda: 0.0, **kwargs)

            with mock.patch.object(
                description_adjudication_module, "adjudicate_descriptions", adjudicate_with_fixed_call_timer
            ):
                result_a = run_weekly_workflow(config_path, output_a, adapters=adapters_a)
                result_b = run_weekly_workflow(config_path, output_b, adapters=adapters_b)

            self.assertEqual(result_a.outcome, result_b.outcome)

            for artifact_name in ["run-summary.json", "pilot-status.json"]:
                path_a = output_a / artifact_name
                path_b = output_b / artifact_name
                if path_a.exists() and path_b.exists():
                    self.assertEqual(path_a.read_bytes(), path_b.read_bytes(), f"{artifact_name} differs")

    def test_workflow_log_contains_stage_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_codex_fixture_config(tmp_path)
            output_dir = tmp_path / "run"
            output_dir.mkdir()

            _write_capture_fixture(tmp_path, output_dir)

            adapters = deterministic_adapters()
            adapters.capture.prepare(config_path, output_dir, live=False)
            _write_extraction_fixture(output_dir)

            result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

            stages = {entry["stage"] for entry in result.log}
            for stage in ["capture", "codex", "finalize", "pipeline", "health", "review"]:
                self.assertIn(stage, stages, f"Log missing stage: {stage}")

    def test_workflow_result_includes_delta_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_codex_fixture_config(tmp_path)
            output_dir = tmp_path / "run"
            output_dir.mkdir()

            _write_capture_fixture(tmp_path, output_dir)

            adapters = deterministic_adapters()
            adapters.capture.prepare(config_path, output_dir, live=False)
            _write_extraction_fixture(output_dir)

            result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

            self.assertIn("new", result.delta_counts)
            self.assertIn("unchanged", result.delta_counts)

    def test_workflow_result_includes_readiness_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_codex_fixture_config(tmp_path)
            output_dir = tmp_path / "run"
            output_dir.mkdir()

            _write_capture_fixture(tmp_path, output_dir)

            adapters = deterministic_adapters()
            adapters.capture.prepare(config_path, output_dir, live=False)
            _write_extraction_fixture(output_dir)

            result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

            self.assertIn("readiness_purpose", result.readiness_facts)
            self.assertIn("editor_facing_count", result.readiness_facts)

    def test_stale_committed_worklist_is_not_reused(self):
        """A worklist left over from a prior run_date must never be silently
        reused; the capture stage must run fresh for the current run_date.

        Issue #280: the effective date is now clock-authoritative. The
        deterministic clock returns 2000-01-01, so the regenerated worklist
        carries that date, not the stale config run_date."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_codex_fixture_config(tmp_path)
            output_dir = tmp_path / "run"
            output_dir.mkdir()

            (output_dir / "codex-extraction-worklist.json").write_text(
                json.dumps({"run_date": "2020-01-01", "work_items": [{"source_name": "Stale"}]}),
                encoding="utf-8",
            )

            _write_capture_fixture(tmp_path, output_dir)
            adapters = deterministic_adapters()
            result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

            capture_entry = next(entry for entry in result.log if entry["stage"] == "capture")
            self.assertNotEqual(capture_entry["detail"].get("status"), "worklist_matches_current_run")

            worklist = json.loads((output_dir / "codex-extraction-worklist.json").read_text(encoding="utf-8"))
            # The clock-authoritative date (2000-01-01 from deterministic clock) wins
            # over the stale config run_date (2026-07-13).
            self.assertEqual(worklist["run_date"], "2000-01-01")

    def test_capture_is_current_requires_matching_run_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_codex_fixture_config(tmp_path)
            output_dir = tmp_path / "run"
            output_dir.mkdir()

            self.assertFalse(_capture_is_current(output_dir, config_path))

            (output_dir / "codex-extraction-worklist.json").write_text(
                json.dumps({"run_date": "2020-01-01", "work_items": []}), encoding="utf-8"
            )
            self.assertFalse(_capture_is_current(output_dir, config_path))

            (output_dir / "codex-extraction-worklist.json").write_text(
                json.dumps({"run_date": "2026-07-13", "work_items": []}), encoding="utf-8"
            )
            # Matching a static config date is insufficient without today's
            # validated resume boundary and captured source identities.
            self.assertFalse(_capture_is_current(output_dir, config_path))


class WeeklyWorkflowAdapterTests(unittest.TestCase):
    def test_deterministic_adapters_are_not_none(self):
        adapters = deterministic_adapters()
        self.assertIsNotNone(adapters.capture)
        self.assertIsNotNone(adapters.codex)
        self.assertIsNotNone(adapters.repository)
        self.assertIsNotNone(adapters.presentation)
        self.assertIsNotNone(adapters.enrichment)
        self.assertIsNotNone(adapters.clock)

    def test_noop_repository_adapter_returns_ok(self):
        adapters = deterministic_adapters()
        repo = adapters.repository
        self.assertIsNotNone(repo)
        if repo is not None:
            self.assertEqual(repo.preflight(), {"status": "ok"})
            self.assertEqual(repo.update(), {"status": "ok", "pulled": False})
            self.assertEqual(repo.finalize(Path("/tmp")), {"status": "ok", "committed": False, "pushed": False})

    def test_noop_open_adapter_is_safe(self):
        adapters = deterministic_adapters()
        opener = adapters.presentation
        self.assertIsNotNone(opener)
        if opener is not None:
            result = opener.open_review(Path("/tmp/test.html"))
            self.assertEqual(result["opened"], False)
