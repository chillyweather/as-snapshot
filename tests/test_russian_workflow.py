"""End-to-end acceptance tests for Russian editor workflow (#268)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from autoscout.russian_workflow import completion_message
from autoscout.russian_workflow import intervention_message
from autoscout.russian_workflow import stage_message
from autoscout.russian_workflow import workflow_started_message
from autoscout.weekly_workflow import WorkflowOutcome


class RussianContractTests(unittest.TestCase):
    def test_workflow_started_message_is_in_russian(self):
        msg = workflow_started_message()
        self.assertIn("Запускаю", msg)


class RussianRenderingTests(unittest.TestCase):
    def test_completion_no_attention_message(self):
        msg = completion_message(
            outcome="completed/no-attention",
            new_count=5,
            updated_count=2,
            attention_required=False,
            review_available=True,
        )
        self.assertIn("завершён успешно", msg)
        self.assertIn("Новых книг: 5", msg)
        self.assertIn("Обновлено книг: 2", msg)

    def test_completion_attention_required_message(self):
        msg = completion_message(
            outcome="completed/attention-required",
            new_count=3,
            attention_required=True,
            review_available=True,
        )
        self.assertIn("Требуется ваше внимание", msg)

    def test_completion_reports_grounding_counts_and_russian_reason_breakdown(self):
        msg = completion_message(
            outcome="completed/attention-required",
            grounded_description_counts={
                "grounded": 4,
                "single_source": 2,
                "attempted_no_description": 3,
                "not_attempted": 1,
            },
            grounded_description_reason_counts={
                "source_identity_disagreement": 1,
                "subject_content_disagreement": 1,
                "not_attempted_stage_budget": 1,
            },
        )
        self.assertIn("подтверждено несколькими источниками - 4", msg)
        self.assertIn("по одному источнику - 2", msg)
        self.assertIn("без описания - 3", msg)
        self.assertIn("поиск не запускался - 1", msg)
        self.assertIn("Источники противоречат друг другу - 2", msg)
        self.assertIn("Поиск источников еще не запускался - 1", msg)

    def test_intervention_message_identifies_editor_or_dima(self):
        msg = intervention_message(failed_stage="repository-preflight")
        self.assertIn("Димой", msg)

        msg2 = intervention_message(failed_stage="codex")
        self.assertIn("проверьте", msg2.lower())

    def test_stage_messages_are_russian(self):
        for stage in ["capture", "codex", "finalize", "pipeline", "health", "review"]:
            msg = stage_message(stage)
            self.assertTrue(any("А" <= c <= "я" for c in msg), f"Stage {stage} not in Russian: {msg}")

    def test_zero_new_books_not_failure(self):
        msg = completion_message(
            outcome="completed/no-attention",
            new_count=0,
            updated_count=0,
            attention_required=False,
        )
        self.assertIn("нормально", msg)

    def test_source_problem_message(self):
        from autoscout.russian_workflow import source_problem_message

        msg = source_problem_message(skipped=2, failed=1)
        self.assertIn("источников не удалось проверить", msg)
        self.assertIn("источников пропущено", msg)

    def test_source_problem_message_names_sources(self):
        from autoscout.russian_workflow import source_problem_message

        msg = source_problem_message(
            [
                {"source_name": "BookPage", "status": "failed"},
                {"source_name": "NPR", "status": "skipped"},
            ]
        )
        self.assertIn("BookPage", msg)
        self.assertIn("NPR", msg)

    def test_render_workflow_result_includes_open_failure_fallback(self):
        from types import SimpleNamespace

        from autoscout.russian_workflow import open_failure_fallback
        from autoscout.russian_workflow import render_workflow_result

        fake_result = SimpleNamespace(
            outcome="completed/no-attention",
            failed_stage=None,
            readiness_facts={"attention_required": False},
            delta_counts={"new": 0, "unchanged": 0},
            source_problems=[],
            review_available=True,
            log=[
                {"stage": "review", "status": "ok"},
                {"stage": "open-review", "status": "failed", "detail": {"opened": False}},
            ],
        )

        msg = render_workflow_result(fake_result)
        self.assertIn(open_failure_fallback(), msg)

    def test_render_workflow_result_zero_counts_shown(self):
        from types import SimpleNamespace

        from autoscout.russian_workflow import render_workflow_result

        fake_result = SimpleNamespace(
            outcome="completed/attention-required",
            failed_stage=None,
            readiness_facts={"attention_required": True},
            delta_counts={"new": 0, "unchanged": 3},
            source_problems=[{"source_name": "NPR", "status": "skipped"}],
            review_available=True,
            log=[],
        )

        msg = render_workflow_result(fake_result)
        self.assertIn("Новых книг: 0", msg)
        self.assertIn("NPR", msg)


class RussianWorkflowAcceptanceTests(unittest.TestCase):
    def _write_acceptance_config(self, tmp_path: Path) -> Path:
        config_path = tmp_path / "config.json"
        config = {
            "run_date": "2026-07-13",
            "readiness_purpose": "recurring-weekly",
            "pilot_mode": True,
            "editor_row_target": 100,
            "pilot_row_min": 100,
            "pilot_row_max": 100,
            "sources": [
                {
                    "name": "Acceptance Source",
                    "family": "award",
                    "url": "https://example.test/awards",
                    "access_status": "open",
                    "extraction_method": "fixture",
                    "check_frequency": "weekly",
                    "language": "en",
                    "fixture_candidates": [
                        {
                            "title": "Acceptance Book",
                            "primary_authors": ["Acceptance Author"],
                            "source_url": "https://example.test/book",
                            "signal_strength": "shortlist",
                            "evidence_note": "Test note.",
                            "candidate_category": "popular science",
                        }
                    ],
                }
            ],
        }
        config_path.write_text(json.dumps(config), encoding="utf-8")
        return config_path

    def test_run_mvp_produces_weekly_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_acceptance_config(tmp_path)
            output_dir = tmp_path / "run"
            output_dir.mkdir()

            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "autoscout",
                    "run-mvp",
                    "--config",
                    str(config_path),
                    "--output-dir",
                    str(output_dir),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, f"Failed: {proc.stderr}")
            self.assertTrue((output_dir / "editor-facing.csv").exists())
            self.assertTrue((output_dir / "pilot-status.json").exists())
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["readiness_purpose"], "recurring-weekly")

    def test_russian_messages_match_outcome(self):
        complete_msg = completion_message(
            outcome=WorkflowOutcome.COMPLETED_NO_ATTENTION.value,
            new_count=3,
            updated_count=1,
            attention_required=False,
            review_available=True,
        )
        self.assertIn("завершён успешно", complete_msg)
        self.assertIn("Новых книг: 3", complete_msg)
        self.assertIn("Обновлено книг: 1", complete_msg)

        attention_msg = completion_message(
            outcome=WorkflowOutcome.COMPLETED_ATTENTION_REQUIRED.value,
            new_count=0,
            attention_required=True,
            review_available=True,
        )
        self.assertIn("Требуется ваше внимание", attention_msg)

        intervention_msg = intervention_message(failed_stage="repository-preflight")
        self.assertIn("Димой", intervention_msg)

    def test_progress_stages_are_all_russian(self):
        stages = ["capture", "codex", "finalize", "pipeline", "health", "review"]
        for stage in stages:
            msg = stage_message(stage)
            self.assertTrue(len(msg) > 0)
            has_cyrillic = any("А" <= c <= "я" or c == "ё" or c == "Ё" for c in msg)
            self.assertTrue(has_cyrillic, f"No Cyrillic in stage message for '{stage}': {msg}")
