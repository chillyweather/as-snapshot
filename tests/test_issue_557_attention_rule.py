"""Tests for issue #557: a cumulative description backlog must never drive a per-run status.

``attention_required`` means "this run introduced or failed to resolve
something" (issue option 1). The standing corpus-wide description backlog
stays reported in ``description_coverage`` and the review, but it can no
longer force ``completed/attention-required`` on its own.
"""

from __future__ import annotations

import inspect
import json
import tempfile
import typing
import unittest
from pathlib import Path
from typing import Any

from state_store_support import seed_state

from autoscout.content_description import DESCRIPTION_FALLBACK
from autoscout.weekly_workflow import WorkflowOutcome
from autoscout.weekly_workflow import deterministic_adapters
from autoscout.weekly_workflow import run_weekly_workflow
from tests.test_issue_272_description_enrichment import FakeAgent
from tests.test_issue_272_description_enrichment import _make_adapters
from tests.test_issue_272_description_enrichment import _setup_run

# ── The extracted decision seam ───────────────────────────────────────────


class WeeklyRunAttentionRuleTests(unittest.TestCase):
    """The per-run attention rule takes per-run facts only."""

    def test_rule_is_importable(self):
        from autoscout.weekly_workflow import weekly_run_attention_rule

        self.assertTrue(callable(weekly_run_attention_rule))

    def test_reasons_name_each_this_run_fact_and_nothing_else(self):
        from autoscout.weekly_workflow import weekly_run_attention_reasons

        self.assertEqual(
            weekly_run_attention_reasons(
                readiness_purpose="recurring-weekly",
                recurring_attention_required=False,
                ready_for_editor=None,
                source_problem_count=0,
                description_stage_problem_count=0,
            ),
            (),
        )
        self.assertEqual(
            weekly_run_attention_reasons(
                readiness_purpose="recurring-weekly",
                recurring_attention_required=True,
                ready_for_editor=None,
                source_problem_count=0,
                description_stage_problem_count=0,
            ),
            ("readiness",),
        )
        self.assertEqual(
            weekly_run_attention_reasons(
                readiness_purpose="seed-calibration",
                recurring_attention_required=None,
                ready_for_editor=True,
                source_problem_count=2,
                description_stage_problem_count=3,
            ),
            ("sources", "descriptions"),
        )
        # The review's named case: readiness-only attention carries no
        # sources claim (2026-08-20 fixture state).
        self.assertEqual(
            weekly_run_attention_reasons(
                readiness_purpose="seed-calibration",
                recurring_attention_required=None,
                ready_for_editor=False,
                source_problem_count=0,
                description_stage_problem_count=0,
            ),
            ("readiness",),
        )

    def test_rule_agrees_with_reasons(self):
        from autoscout.weekly_workflow import weekly_run_attention_reasons
        from autoscout.weekly_workflow import weekly_run_attention_rule

        kwargs = {
            "readiness_purpose": "seed-calibration",
            "recurring_attention_required": None,
            "ready_for_editor": False,
            "source_problem_count": 1,
            "description_stage_problem_count": 1,
        }
        self.assertTrue(weekly_run_attention_rule(**kwargs))
        self.assertEqual(
            set(weekly_run_attention_reasons(**kwargs)),
            {"readiness", "sources", "descriptions"},
        )

    def test_recurring_run_with_clean_per_run_facts_is_green(self):
        from autoscout.weekly_workflow import weekly_run_attention_rule

        self.assertFalse(
            weekly_run_attention_rule(
                readiness_purpose="recurring-weekly",
                recurring_attention_required=False,
                ready_for_editor=None,
                source_problem_count=0,
                description_stage_problem_count=0,
            )
        )

    def test_no_parameter_carries_the_corpus_backlog(self):
        """The signature accepts per-run facts only; there is no fallback input."""
        from autoscout.weekly_workflow import weekly_run_attention_rule

        params = inspect.signature(weekly_run_attention_rule).parameters
        self.assertNotIn("fallback_count", params)
        self.assertNotIn("description_coverage", params)

    def test_failed_source_forces_attention_on_both_paths(self):
        from autoscout.weekly_workflow import weekly_run_attention_rule

        self.assertTrue(
            weekly_run_attention_rule(
                readiness_purpose="recurring-weekly",
                recurring_attention_required=False,
                ready_for_editor=None,
                source_problem_count=1,
                description_stage_problem_count=0,
            )
        )
        self.assertTrue(
            weekly_run_attention_rule(
                readiness_purpose="seed-calibration",
                recurring_attention_required=None,
                ready_for_editor=True,
                source_problem_count=2,
                description_stage_problem_count=0,
            )
        )

    def test_this_run_description_failures_force_attention(self):
        from autoscout.weekly_workflow import weekly_run_attention_rule

        self.assertTrue(
            weekly_run_attention_rule(
                readiness_purpose="recurring-weekly",
                recurring_attention_required=False,
                ready_for_editor=None,
                source_problem_count=0,
                description_stage_problem_count=3,
            )
        )

    def test_readiness_verdicts_still_bind(self):
        from autoscout.weekly_workflow import weekly_run_attention_rule

        self.assertTrue(
            weekly_run_attention_rule(
                readiness_purpose="recurring-weekly",
                recurring_attention_required=True,
                ready_for_editor=None,
                source_problem_count=0,
                description_stage_problem_count=0,
            )
        )
        self.assertTrue(
            weekly_run_attention_rule(
                readiness_purpose="seed-calibration",
                recurring_attention_required=None,
                ready_for_editor=False,
                source_problem_count=0,
                description_stage_problem_count=0,
            )
        )


# ── Acceptance at the public workflow seam ────────────────────────────────


class StandingBacklogOutcomeTests(unittest.TestCase):
    def test_standing_backlog_without_this_run_problems_ends_no_attention(self):
        """A clean run over a corpus with an inherited gap ends completed/no-attention."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path, output_dir = _setup_run(
                tmp_path,
                readiness_purpose="seed-calibration",
                editor_row_target=1,
            )
            state_dir = tmp_path / "state"
            state_dir.mkdir()
            # A gap inherited from an earlier week: this run never touches it.
            seed_state(
                state_dir,
                candidate_history=[
                    {
                        "candidate_id": "old book|old author",
                        "identity_key": "old book|old author",
                        "title": "Old Book",
                        "primary_authors": ["Old Author"],
                        "first_seen_date": "2026-06-01",
                        "last_seen_date": "2026-07-01",
                        "content_description": DESCRIPTION_FALLBACK,
                    }
                ],
            )

            agent = FakeAgent({"test book|test author": "Увлекательное исследование современной науки."})
            result = run_weekly_workflow(
                config_path,
                output_dir,
                state_dir=state_dir,
                adapters=_make_adapters(agent=agent),
            )

            self.assertEqual(result.outcome, WorkflowOutcome.COMPLETED_NO_ATTENTION)
            self.assertFalse(result.readiness_facts.get("attention_required", True))
            # The inherited gap stays visible, unchanged in meaning.
            self.assertEqual(result.description_coverage.get("fallback_count", 0), 1)
            self.assertIn("old book|old author", result.description_coverage.get("fallback_ids", []))

    def test_this_run_grounding_failure_keeps_attention(self):
        """A candidate this run attempted and left without a description stays red."""
        from autoscout.grounded_description import GroundedDescriptionStage
        from autoscout.grounded_description import GroundedDescriptionStageService
        from autoscout.grounded_description import SynthesisResult

        class SelectiveDiscovery:
            """Grounds every candidate except the one named by identity key."""

            def __init__(self, failing_identity_key: str):
                self._failing = failing_identity_key

            def discover(self, candidate: dict[str, Any], **_kwargs: Any):
                if str(candidate.get("identity_key")) == self._failing:
                    return []
                title = str(candidate.get("title", ""))
                authors = list(candidate.get("primary_authors", []))
                return [
                    {
                        "url": f"https://publisher.example.test/{self._failing}",
                        "source_kind": "publisher",
                        "source_family": "publisher_copy",
                        "owner_id": f"publisher-{self._failing}",
                        "classification": "classified",
                        "title": title,
                        "primary_authors": authors,
                    },
                    {
                        "url": f"https://review.example.test/{self._failing}",
                        "source_kind": "review",
                        "source_family": "review",
                        "owner_id": f"review-{self._failing}",
                        "classification": "classified",
                        "title": title,
                        "primary_authors": authors,
                    },
                ]

        class ReplayFetch:
            def fetch(self, result: Any, **kwargs: Any) -> dict[str, Any]:
                return {
                    "url": result.url,
                    "source_kind": result.source_kind,
                    "source_family": result.source_family,
                    "owner_id": result.owner_id,
                    "source_language": "en",
                    "title": result.title,
                    "primary_authors": list(result.primary_authors),
                    "subject_text": "A bounded account of memory, society, and public life.",
                    "checked_date": kwargs.get("checked_date", "2026-07-13"),
                    "classification": "classified",
                }

        class ReplaySynthesizer:
            def synthesize(self, candidate: dict[str, Any], sources: Any) -> SynthesisResult:
                del sources
                return SynthesisResult(f"Книга {candidate['title']} о памяти, обществе и общественной жизни.")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path, output_dir = _setup_run(
                tmp_path,
                entries=[
                    {
                        "title": "Book A",
                        "primary_authors": ["Author A"],
                        "source_url": "https://example.test/book-a",
                        "evidence_url": "https://example.test/book-a",
                        "evidence_note": "Note A.",
                        "candidate_category": "history",
                        "signal_type": "media mention",
                        "signal_strength": "shortlist",
                    },
                    {
                        "title": "Book B",
                        "primary_authors": ["Author B"],
                        "source_url": "https://example.test/book-b",
                        "evidence_url": "https://example.test/book-b",
                        "evidence_note": "Note B.",
                        "candidate_category": "science",
                        "signal_type": "award",
                        "signal_strength": "shortlist",
                    },
                ],
                extra_urls=["https://example.test/book-a", "https://example.test/book-b"],
            )

            adapters = deterministic_adapters()
            adapters.enrichment = GroundedDescriptionStageService(
                GroundedDescriptionStage(
                    discovery=SelectiveDiscovery("book b|author b"),
                    fetch=ReplayFetch(),
                    synthesizer=ReplaySynthesizer(),
                    max_workers=1,
                )
            )
            result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

            self.assertEqual(result.outcome, WorkflowOutcome.COMPLETED_ATTENTION_REQUIRED)
            self.assertTrue(result.readiness_facts.get("attention_required", False))
            stage_report = getattr(result, "description_stage_report", {}) or {}
            self.assertTrue(stage_report.get("attention_required"))
            self.assertGreaterEqual(int(stage_report.get("counts", {}).get("failed", 0)), 1)


# ── Operator-facing Russian message ───────────────────────────────────────


class OperatorMessageTests(unittest.TestCase):
    def test_message_names_count_and_fix_when_this_run_fails(self):
        from autoscout.russian_workflow import completion_message

        msg = completion_message(
            outcome="completed/attention-required",
            new_count=2,
            attention_required=True,
            review_available=True,
            description_stage_failures=1,
        )
        self.assertIn("1", msg)
        self.assertIn("обзор", msg)
        self.assertIn("повторит", msg)
        self.assertNotIn("Часть описаний требует проверки", msg)

    def test_plural_forms_of_the_failure_count(self):
        from autoscout.russian_workflow import completion_message

        one = completion_message(
            outcome="completed/attention-required",
            attention_required=True,
            description_stage_failures=1,
        )
        self.assertIn("для 1 книги", one)
        many = completion_message(
            outcome="completed/attention-required",
            attention_required=True,
            description_stage_failures=5,
        )
        self.assertIn("для 5 книг", many)
        eleven = completion_message(
            outcome="completed/attention-required",
            attention_required=True,
            description_stage_failures=11,
        )
        self.assertIn("для 11 книг", eleven)
        twenty_one = completion_message(
            outcome="completed/attention-required",
            attention_required=True,
            description_stage_failures=21,
        )
        self.assertIn("для 21 книги", twenty_one)

    def test_standing_backlog_alone_shows_numbers_not_alarm(self):
        """A falling backlog with a green run reports numbers, not attention."""
        from autoscout.russian_workflow import completion_message

        msg = completion_message(
            outcome="completed/no-attention",
            new_count=3,
            attention_required=False,
            review_available=True,
            description_coverage={
                "validated_count": 580,
                "fallback_count": 53,
                "total_candidates": 633,
                "coverage": "partial",
            },
        )
        self.assertIn("Описаний на русском: 580 из 633", msg)
        self.assertIn("Требуют проверки: 53.", msg)
        self.assertNotIn("Часть описаний требует проверки", msg)
        self.assertNotIn("Требуется ваше внимание", msg)

    def test_source_driven_attention_keeps_legacy_line(self):
        from autoscout.russian_workflow import completion_message

        msg = completion_message(
            outcome="completed/attention-required",
            new_count=2,
            attention_required=True,
            review_available=True,
            description_coverage={
                "validated_count": 2,
                "fallback_count": 0,
                "total_candidates": 2,
                "coverage": "complete",
            },
        )
        self.assertIn("Часть источников не была проверена", msg)

    def test_readiness_only_attention_does_not_blame_sources(self):
        """The 2026-08-20 shape: not-ready with zero source problems (issue #557)."""
        from autoscout.russian_workflow import completion_message

        msg = completion_message(
            outcome="completed/attention-required",
            new_count=2,
            attention_required=True,
            review_available=True,
            attention_reasons=("readiness",),
        )
        self.assertIn("Проверка качества результатов завершилась с замечаниями", msg)
        self.assertNotIn("Часть источников не была проверена", msg)

    def test_source_attention_names_sources_line_when_reasons_given(self):
        from autoscout.russian_workflow import completion_message

        msg = completion_message(
            outcome="completed/attention-required",
            attention_required=True,
            attention_reasons=("sources",),
        )
        self.assertIn("Часть источников не была проверена", msg)
        self.assertNotIn("Проверка качества результатов", msg)

    def test_description_failures_outrank_other_reasons(self):
        from autoscout.russian_workflow import completion_message

        msg = completion_message(
            outcome="completed/attention-required",
            attention_required=True,
            description_stage_failures=2,
            attention_reasons=("sources", "descriptions"),
        )
        self.assertIn("для 2 книг", msg)
        self.assertNotIn("Часть источников не была проверена", msg)


# ── Replay of the ten runs quoted by issue #557 ───────────────────────────


class TenHistoricalRunsReplayTests(unittest.TestCase):
    """Replay the issue's ten committed states against the new rule.

    Facts come from the committed fixture (extracted once from the state
    commits); nothing asserts against the moving ``runs/`` alias.
    """

    FIXTURE = Path(__file__).resolve().parent / "fixtures" / "issue-557-attention-replay" / "historical-runs.json"

    # Outcome each state earns under the new rule, decided ONLY by its
    # recorded per-run facts. Three states go green; seven stay red for
    # named reasons that have nothing to do with the description backlog.
    EXPECTED: typing.ClassVar[dict[str, str]] = {
        "2026-08-05": "attention-required",
        "2026-08-08": "no-attention",
        "2026-08-12": "attention-required",
        "2026-08-13": "attention-required",
        "2026-08-13-b": "attention-required",
        "2026-08-14": "no-attention",
        "2026-08-15": "attention-required",
        "2026-08-17": "no-attention",
        "2026-08-20": "attention-required",
        "2026-08-21": "attention-required",
    }

    # The exact reasons each red state earns, so the editor-facing narrative
    # reports facts instead of a generic symptom.
    EXPECTED_REASONS: typing.ClassVar[dict[str, frozenset[str]]] = {
        "2026-08-05": frozenset({"readiness", "sources"}),
        "2026-08-08": frozenset(),
        "2026-08-12": frozenset({"sources"}),
        "2026-08-13": frozenset({"sources"}),
        "2026-08-13-b": frozenset({"readiness", "sources"}),
        "2026-08-14": frozenset(),
        "2026-08-15": frozenset({"readiness", "sources"}),
        "2026-08-17": frozenset(),
        # Readiness-only: stays red without any sources claim (review finding).
        "2026-08-20": frozenset({"readiness"}),
        "2026-08-21": frozenset({"readiness", "sources"}),
    }

    def setUp(self):
        self.rows = json.loads(self.FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(len(self.rows), 10)

    @staticmethod
    def _source_problem_count(row: dict) -> int:
        """Mirror weekly_workflow consolidation: named rows win, else counts."""
        if row["source_problems"]:
            return len(row["source_problems"])
        return int(row["skipped_source_count"] or 0) + int(row["failed_source_count"] or 0)

    def test_fixture_covers_every_quoted_backlog_number(self):
        quoted = {
            ("2026-08-05", 0),
            ("2026-08-08", 82),
            ("2026-08-12", 82),
            ("2026-08-13", 77),
            ("2026-08-13-b", 78),
            ("2026-08-14", 74),
            ("2026-08-15", 74),
            ("2026-08-17", 59),
            ("2026-08-20", 54),
            ("2026-08-21", 53),
        }
        actual = {(row["issue_label"], int(row["fallback_count"])) for row in self.rows}
        self.assertEqual(actual, quoted)

    def test_replay_matches_expected_outcomes(self):
        from autoscout.weekly_workflow import weekly_run_attention_reasons

        outcomes = {}
        reasons = {}
        for row in self.rows:
            state_reasons = weekly_run_attention_reasons(
                readiness_purpose=str(row["readiness_purpose"]),
                recurring_attention_required=None,
                ready_for_editor=row["ready_for_editor"],
                source_problem_count=self._source_problem_count(row),
                description_stage_problem_count=int(row["description_stage_problem_count"]),
            )
            label = row["issue_label"]
            reasons[label] = frozenset(state_reasons)
            outcomes[label] = "attention-required" if state_reasons else "no-attention"
        self.assertEqual(outcomes, self.EXPECTED)
        self.assertEqual(reasons, self.EXPECTED_REASONS)

    def test_green_states_have_clean_per_run_facts_and_red_earned_reasons(self):
        greens = [row for row in self.rows if self.EXPECTED[row["issue_label"]] == "no-attention"]
        for row in greens:
            self.assertIs(row["ready_for_editor"], True, row["issue_label"])
            self.assertEqual(self._source_problem_count(row), 0, row["issue_label"])
        reds = [row for row in self.rows if self.EXPECTED[row["issue_label"]] == "attention-required"]
        for row in reds:
            earned = row["ready_for_editor"] is not True or self._source_problem_count(row) > 0
            self.assertTrue(earned, f"{row['issue_label']} stays red without a per-run reason")

    def test_recorded_outcomes_agree_where_history_exists(self):
        """Every state with a recorded terminal outcome was attention-required under the old rule."""
        recorded = [(row["issue_label"], row["recorded_outcome"]) for row in self.rows if row["recorded_outcome"]]
        self.assertTrue(recorded)
        for label, outcome in recorded:
            self.assertEqual(outcome, "completed/attention-required", label)
