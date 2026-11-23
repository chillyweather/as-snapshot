"""Contract tests for issues #273 and #274 (review findings B11-B15)."""

from __future__ import annotations

import types
import unittest

from autoscout.russian_workflow import render_workflow_result


class Issue274NormalOutputContractTests(unittest.TestCase):
    def test_render_workflow_result_hides_technical_terms(self):
        fake_result = types.SimpleNamespace(
            outcome="completed/no-attention",
            failed_stage=None,
            readiness_facts={"attention_required": False},
            delta_counts={"new": 2, "upgraded": 1, "unchanged": 3},
            source_problems=[],
            review_available=True,
            log=[
                {"stage": "capture", "status": "ok"},
                {"stage": "pipeline", "status": "ok"},
                {"stage": "review", "status": "ok"},
                {"stage": "open-review", "status": "ok", "detail": {"opened": True}},
            ],
        )
        msg = render_workflow_result(fake_result)
        for term in ("pipeline", "Codex", "codex", "JSON", "CSV", "capture", "replay"):
            self.assertNotIn(term, msg, f"Technical term '{term}' leaked: {msg}")

    def test_render_workflow_result_hides_developer_diagnostics(self):
        """B15: explicit commit hash, branch, remote, pushed, dirty, test-count
        values in log detail must not leak into editor output."""
        fake_result = types.SimpleNamespace(
            outcome="completed/attention-required",
            failed_stage=None,
            readiness_facts={"attention_required": True},
            delta_counts={"new": 0, "unchanged": 5},
            source_problems=[{"source_name": "NPR", "status": "skipped"}],
            review_available=True,
            log=[
                {"stage": "capture", "status": "ok"},
                {"stage": "pipeline", "status": "ok"},
                {
                    "stage": "health",
                    "status": "ok",
                    "detail": {
                        "test_count": 42,
                        "commit_hash": "abc123def456",
                        "branch": "feature/merge-test",
                        "remote": "origin",
                        "pushed": True,
                        "dirty": "no",
                        "worktree_clean": True,
                    },
                },
                {"stage": "review", "status": "ok"},
                {"stage": "open-review", "status": "ok", "detail": {"opened": True}},
            ],
        )
        msg = render_workflow_result(fake_result)
        for term in (
            "test_count",
            "42",
            "abc123def456",
            "feature/merge-test",
            "origin",
            "pushed",
            "dirty",
            "worktree_clean",
        ):
            self.assertNotIn(term, msg, f"Dev diagnostic '{term}' leaked: {msg}")

    def test_render_workflow_result_preserves_source_caveats(self):
        fake_result = types.SimpleNamespace(
            outcome="completed/attention-required",
            failed_stage=None,
            readiness_facts={"attention_required": True},
            delta_counts={"new": 1, "upgraded": 0, "unchanged": 0},
            source_problems=[{"source_name": "BookPage", "status": "failed"}],
            review_available=True,
            log=[
                {"stage": "pipeline", "status": "ok"},
                {"stage": "open-review", "status": "ok", "detail": {"opened": True}},
            ],
        )
        msg = render_workflow_result(fake_result)
        self.assertIn("BookPage", msg)
        self.assertIn("не удалось", msg.lower())

    def test_render_workflow_result_shows_editorial_counts(self):
        fake_result = types.SimpleNamespace(
            outcome="completed/no-attention",
            failed_stage=None,
            readiness_facts={"attention_required": False},
            delta_counts={"new": 3, "upgraded": 2, "changed": 1, "unchanged": 0},
            source_problems=[],
            review_available=True,
            log=[
                {"stage": "pipeline", "status": "ok"},
                {"stage": "open-review", "status": "ok", "detail": {"opened": True}},
            ],
        )
        msg = render_workflow_result(fake_result)
        self.assertIn("Новых книг: 3", msg)
        self.assertIn("Обновлено книг: 3", msg)

    def test_render_workflow_result_shows_description_coverage(self):
        fake_result = types.SimpleNamespace(
            outcome="completed/no-attention",
            failed_stage=None,
            readiness_facts={"attention_required": False},
            delta_counts={"new": 3, "unchanged": 0},
            source_problems=[],
            review_available=True,
            description_coverage={"validated_count": 2, "fallback_count": 1, "total_candidates": 3},
            log=[
                {"stage": "pipeline", "status": "ok"},
                {"stage": "open-review", "status": "ok", "detail": {"opened": True}},
            ],
        )
        msg = render_workflow_result(fake_result)
        self.assertIn("Описаний на русском", msg)
        self.assertIn("2 из 3", msg)
        self.assertIn("Требуют проверки", msg)
