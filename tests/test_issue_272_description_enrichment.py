"""Tests for issue #272: Description enrichment in the weekly workflow.

Acceptance tests at the public weekly-workflow seam.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from state_store_support import read_state
from state_store_support import seed_state

from autoscout.content_description import DESCRIPTION_FALLBACK
from autoscout.weekly_workflow import WorkflowOutcome
from autoscout.weekly_workflow import deterministic_adapters
from autoscout.weekly_workflow import run_weekly_workflow

# ── Shared helpers ────────────────────────────────────────────────────────


def _write_config(
    tmp_path: Path,
    *,
    readiness_purpose: str = "recurring-weekly",
    editor_row_target: int = 100,
) -> Path:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
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
                        "extraction_response_file": "test-source-codex-extraction.json",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return config_path


def _write_capture(tmp_path: Path, *, extra_urls: list[str] | None = None) -> None:
    urls = ["https://example.test/book"] + (extra_urls or [])
    links = " ".join(f'<a href="{url}">Link</a>' for url in urls)
    # The page names every author the extraction fixtures in this module use.
    links += " <p>by Test Author, Author A, Author B, Author X, Good Author, Bad Author</p>"
    (tmp_path / "test-capture.json").write_text(
        json.dumps(
            {
                "source_url": "https://example.test/source",
                "html": f"<html><body>{links}</body></html>",
            }
        ),
        encoding="utf-8",
    )


def _write_extraction(output_dir: Path, *, entries: list | None = None) -> Path:
    worklist = json.loads((output_dir / "codex-extraction-worklist.json").read_text(encoding="utf-8"))
    item = worklist["work_items"][0]
    expected_file = str(item.get("expected_extraction_file", "")) or "test-source-codex-extraction.json"
    result = output_dir / expected_file
    if entries is None:
        entries = [
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
        ]
    result.write_text(
        json.dumps(
            {
                "source_name": "Test Source",
                "source_url": "https://example.test/source",
                "operation_id": item["operation_id"],
                "capture_sha256": item["capture_sha256"],
                "entries": entries,
            }
        ),
        encoding="utf-8",
    )
    return result


def _setup_run(
    tmp_path: Path,
    *,
    readiness_purpose: str = "recurring-weekly",
    editor_row_target: int = 100,
    entries: list | None = None,
    extra_urls: list[str] | None = None,
) -> tuple[Path, Path]:
    config_path = _write_config(tmp_path, readiness_purpose=readiness_purpose, editor_row_target=editor_row_target)
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    _write_capture(tmp_path, extra_urls=extra_urls)
    adapters = deterministic_adapters()
    adapters.capture.prepare(config_path, output_dir, live=False)
    _write_extraction(output_dir, entries=entries)
    return config_path, output_dir


def _make_adapters(*, agent=None):
    from autoscout.description_enrichment import DescriptionEnrichmentService
    from autoscout.description_enrichment import NoOpDescriptionEnrichmentAgent

    a = deterministic_adapters()
    effective = agent if agent is not None else NoOpDescriptionEnrichmentAgent()
    a.enrichment = DescriptionEnrichmentService(agent=effective)
    # The adjudication stage stays wired here on purpose. This suite pins
    # the issue #557 rule that a standing backlog never forces attention,
    # and adjudication reads the whole backlog every week, so this suite is
    # what proves its declines do not drive the outcome (issue #641).
    return a


# ── Test agents ───────────────────────────────────────────────────────────


class FakeAgent:
    """Returns descriptions from a mapping."""

    def __init__(self, descriptions: dict[str, str] | None = None):
        self._descriptions = descriptions or {}
        self.calls: list[list] = []

    def generate(self, work_items):
        self.calls.append(list(work_items))
        result = {}
        for wi in work_items:
            ik = str(wi.get("identity_key", ""))
            result[ik] = self._descriptions.get(ik, f"Описание книги «{wi.get('title', '')}» на русском языке.")
        return result


class SpyRepositoryAdapter:
    """Records finalize calls and snapshots enriched state at finalize time."""

    def __init__(self, *, expected_dir: Path | None = None):
        self.finalize_calls: list = []

    def preflight(self, validated_artifact_allowlist=None):
        return {"status": "ok"}

    def update(self, validated_artifact_allowlist=None):
        return {"status": "ok", "pulled": False}

    def finalize(self, output_dir: Path):
        snapshot = {
            "output_dir": str(output_dir),
            "has_review": (output_dir / "weekly-review.html").exists(),
            "has_coverage": (output_dir / "description-coverage.json").exists(),
            "has_diagnostic": (output_dir / "description-enrichment.json").exists(),
            "has_candidate_history": (output_dir / "candidate-history.json").exists(),
            "has_run_summary": (output_dir / "run-summary.json").exists(),
        }
        # Snapshot key content at finalize time to prove ordering.
        if snapshot["has_run_summary"]:
            rs = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            snapshot["run_summary_dc"] = rs.get("description_coverage", {})
            snapshot["run_summary_dc_fallback"] = rs.get("description_coverage", {}).get("fallback_count", -1)
        if snapshot["has_candidate_history"]:
            hist = json.loads((output_dir / "candidate-history.json").read_text(encoding="utf-8"))
            snapshot["history_identity_keys"] = [rec.get("identity_key") for rec in hist if isinstance(rec, dict)]
            for rec in hist:
                if isinstance(rec, dict) and rec.get("content_description", "") != DESCRIPTION_FALLBACK:
                    snapshot["first_enriched_desc"] = rec.get("content_description", "")
                    break
        self.finalize_calls.append(snapshot)
        return {"status": "ok", "committed": True, "pushed": True}

    def release_guard(self):
        pass


# ── Acceptance tests ──────────────────────────────────────────────────────


class EnrichmentAcceptanceTests(unittest.TestCase):
    """Acceptance tests at the public weekly-workflow seam."""

    def test_full_coverage_completes_no_attention(self):
        """All descriptions enriched, no other attention reason → COMPLETED_NO_ATTENTION."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Use seed-calibration with target=1 so the single row meets target.
            config_path, output_dir = _setup_run(
                tmp_path,
                readiness_purpose="seed-calibration",
                editor_row_target=1,
            )

            agent = FakeAgent({"test book|test author": "Увлекательное исследование современной науки."})
            result = run_weekly_workflow(config_path, output_dir, adapters=_make_adapters(agent=agent))

            self.assertEqual(result.outcome, WorkflowOutcome.COMPLETED_NO_ATTENTION)
            self.assertFalse(result.readiness_facts.get("attention_required", True))

    def test_partial_coverage_stays_reported_without_forcing_attention(self):
        """A this-run rejection keeps the fallback reported but never forces attention (issue #557).

        The per-run grounding-failure channel lives in the grounded stage
        report (#571); the cumulative fallback_count is a corpus fact that
        stays visible without driving the outcome.
        """
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

            agent = FakeAgent(
                {
                    "book a|author a": "Увлекательное историческое исследование.",
                    "book b|author b": "English-only — should be rejected.",
                }
            )
            result = run_weekly_workflow(config_path, output_dir, adapters=_make_adapters(agent=agent))

            self.assertEqual(result.outcome, WorkflowOutcome.COMPLETED_NO_ATTENTION)
            self.assertFalse(result.readiness_facts.get("attention_required", True))
            self.assertEqual(result.description_coverage.get("fallback_count", 0), 1)
            self.assertTrue(result.review_available)

            history = json.loads((output_dir / "candidate-history.json").read_text(encoding="utf-8"))
            a = next((r for r in history if r["identity_key"] == "book a|author a"), None)
            b = next((r for r in history if r["identity_key"] == "book b|author b"), None)
            self.assertEqual(a["content_description"], "Увлекательное историческое исследование.")
            self.assertEqual(b["content_description"], DESCRIPTION_FALLBACK)

            rs = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(rs.get("description_coverage", {}).get("fallback_count", 0), 1)

    def test_preserves_existing_valid_description_agent_not_called(self):
        """Extraction supplies valid Russian content_description → agent not called."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path, output_dir = _setup_run(
                tmp_path,
                entries=[
                    {
                        "title": "Test Book",
                        "primary_authors": ["Test Author"],
                        "source_url": "https://example.test/book",
                        "evidence_url": "https://example.test/book",
                        "evidence_note": "Listed book.",
                        "candidate_category": "popular science",
                        "signal_type": "award shortlist",
                        "signal_strength": "shortlist",
                        "content_description": "Существующее русское описание из источника.",
                    }
                ],
            )

            agent = FakeAgent({"test book|test author": "Это описание не должно использоваться."})
            run_weekly_workflow(config_path, output_dir, adapters=_make_adapters(agent=agent))

            self.assertEqual(len(agent.calls), 0, "Agent was called but should have been skipped")
            history = json.loads((output_dir / "candidate-history.json").read_text(encoding="utf-8"))
            rec = next((r for r in history if r["identity_key"] == "test book|test author"), None)
            self.assertIn("Существующее русское описание", str(rec.get("content_description", "")))
            self.assertNotIn("не должно использоваться", str(rec.get("content_description", "")))

    def test_canonical_state_dir_contains_enriched_description(self):
        """With separate state_dir, enriched history and latest-scouting contain the description."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            state_dir.mkdir()
            config_path, output_dir = _setup_run(tmp_path)

            agent = FakeAgent({"test book|test author": "Глубокое исследование на русском языке."})
            run_weekly_workflow(
                config_path,
                output_dir,
                state_dir=state_dir,
                adapters=_make_adapters(agent=agent),
            )

            hist = read_state(state_dir, "candidate_history")
            rec = next((r for r in hist if r["identity_key"] == "test book|test author"), None)
            self.assertEqual(rec["content_description"], "Глубокое исследование на русском языке.")
            prov = rec.get("description_provenance")
            self.assertEqual(prov.get("provenance"), "codex-generated")

            latest = read_state(state_dir, "latest_scouting_candidates")
            lc = next((r for r in latest if r["identity_key"] == "test book|test author"), None)
            self.assertEqual(lc["content_description"], "Глубокое исследование на русском языке.")

    def test_repository_finalization_snapshots_enriched_state(self):
        """Spy repository snapshots enriched files at finalize time — proves ordering."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path, output_dir = _setup_run(tmp_path)

            agent = FakeAgent({"test book|test author": "Финальное описание книги."})
            adapters = _make_adapters(agent=agent)
            spy = SpyRepositoryAdapter()
            adapters.repository = spy

            run_weekly_workflow(config_path, output_dir, adapters=adapters)

            self.assertEqual(len(spy.finalize_calls), 2)  # two-pass finalization
            # First finalize snapshot.
            snap = spy.finalize_calls[0]
            self.assertTrue(snap["has_review"], "Review missing at first finalize")
            self.assertTrue(snap["has_coverage"], "Coverage missing at first finalize")
            self.assertTrue(snap["has_diagnostic"], "Diagnostic missing at first finalize")
            self.assertTrue(snap["has_candidate_history"], "History missing at first finalize")
            self.assertTrue(snap["has_run_summary"], "Run-summary missing at first finalize")
            self.assertEqual(snap["run_summary_dc_fallback"], 0)
            self.assertIn("Финальное описание книги", snap.get("first_enriched_desc", ""))

    def test_bounded_retry_recovers_valid_on_second_attempt(self):
        """Invalid first attempt, valid second → both IDs enriched."""
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
                        "evidence_note": "Note.",
                        "candidate_category": "history",
                        "signal_type": "media mention",
                        "signal_strength": "shortlist",
                    },
                    {
                        "title": "Book B",
                        "primary_authors": ["Author B"],
                        "source_url": "https://example.test/book-b",
                        "evidence_url": "https://example.test/book-b",
                        "evidence_note": "Note.",
                        "candidate_category": "science",
                        "signal_type": "award",
                        "signal_strength": "shortlist",
                    },
                ],
                extra_urls=["https://example.test/book-a", "https://example.test/book-b"],
            )

            class TwoPhaseAgent:
                def __init__(self):
                    self.calls: list[list] = []
                    self._attempt = 0

                def generate(self, work_items):
                    self._attempt += 1
                    self.calls.append(list(work_items))
                    if self._attempt == 1:
                        return {str(wi.get("identity_key", "")): "English only invalid text." for wi in work_items}
                    return {str(wi.get("identity_key", "")): "Валидное русское описание." for wi in work_items}

            agent = TwoPhaseAgent()
            run_weekly_workflow(config_path, output_dir, adapters=_make_adapters(agent=agent))

            self.assertEqual(len(agent.calls), 2)
            first_req = {str(wi.get("identity_key", "")) for wi in agent.calls[0]}
            self.assertIn("book a|author a", first_req)
            self.assertIn("book b|author b", first_req)

            history = json.loads((output_dir / "candidate-history.json").read_text(encoding="utf-8"))
            for ik in ("book a|author a", "book b|author b"):
                rec = next((r for r in history if r["identity_key"] == ik), None)
                self.assertEqual(rec["content_description"], "Валидное русское описание.")

            diag = json.loads((output_dir / "description-enrichment.json").read_text(encoding="utf-8"))
            self.assertEqual(diag["attempts"], 2)
            self.assertNotIn("English only invalid text", json.dumps(diag))

    def test_successful_id_not_re_requested_on_retry(self):
        """One valid, one invalid in first attempt → only invalid re-requested."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path, output_dir = _setup_run(
                tmp_path,
                entries=[
                    {
                        "title": "Good Book",
                        "primary_authors": ["Good Author"],
                        "source_url": "https://example.test/book-a",
                        "evidence_url": "https://example.test/book-a",
                        "evidence_note": "Note.",
                        "candidate_category": "history",
                        "signal_type": "media mention",
                        "signal_strength": "shortlist",
                    },
                    {
                        "title": "Bad Book",
                        "primary_authors": ["Bad Author"],
                        "source_url": "https://example.test/book-b",
                        "evidence_url": "https://example.test/book-b",
                        "evidence_note": "Note.",
                        "candidate_category": "science",
                        "signal_type": "award",
                        "signal_strength": "shortlist",
                    },
                ],
                extra_urls=["https://example.test/book-a", "https://example.test/book-b"],
            )

            class SelectiveAgent:
                def __init__(self):
                    self.calls: list[list] = []
                    self._attempt = 0

                def generate(self, work_items):
                    self._attempt += 1
                    self.calls.append(list(work_items))
                    result = {}
                    for wi in work_items:
                        ik = str(wi.get("identity_key", ""))
                        if ik == "good book|good author":
                            result[ik] = "Валидное описание хорошей книги."
                        elif self._attempt == 2:
                            result[ik] = "Валидное описание плохой книги."
                        else:
                            result[ik] = "English invalid."
                    return result

            agent = SelectiveAgent()
            run_weekly_workflow(config_path, output_dir, adapters=_make_adapters(agent=agent))

            self.assertEqual(len(agent.calls), 2)
            second_ids = {str(wi.get("identity_key", "")) for wi in agent.calls[1]}
            self.assertNotIn("good book|good author", second_ids, "Successful ID re-requested on retry")
            self.assertIn("bad book|bad author", second_ids, "Failing ID missing from retry")

    def test_retry_sanitizes_unsolicited_id_from_previous_success(self):
        """Attempt 1 succeeds for A, fails B. Attempt 2 returns unsolicited
        replacement for A + valid B → A keeps first validated description,
        A not requested on retry, B succeeds."""
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
                        "evidence_note": "Note.",
                        "candidate_category": "history",
                        "signal_type": "media mention",
                        "signal_strength": "shortlist",
                    },
                    {
                        "title": "Book B",
                        "primary_authors": ["Author B"],
                        "source_url": "https://example.test/book-b",
                        "evidence_url": "https://example.test/book-b",
                        "evidence_note": "Note.",
                        "candidate_category": "science",
                        "signal_type": "award",
                        "signal_strength": "shortlist",
                    },
                ],
                extra_urls=["https://example.test/book-a", "https://example.test/book-b"],
            )

            FIRST_VALID_A = "Первое валидное описание книги А."
            UNSOLICITED_A = "Злонамеренное описание — не должно примениться."
            VALID_B = "Валидное описание книги Б."

            class UnsolicitedRetryAgent:
                def __init__(self):
                    self.calls: list[list] = []
                    self._attempt = 0

                def generate(self, work_items):
                    self._attempt += 1
                    self.calls.append(list(work_items))
                    if self._attempt == 1:
                        return {
                            "book a|author a": FIRST_VALID_A,
                            "book b|author b": "English only — invalid.",
                        }
                    # Attempt 2: returns unsolicited replacement for A + valid B.
                    return {
                        "book a|author a": UNSOLICITED_A,
                        "book b|author b": VALID_B,
                    }

            agent = UnsolicitedRetryAgent()
            run_weekly_workflow(config_path, output_dir, adapters=_make_adapters(agent=agent))

            # Two calls: first both, retry only for B.
            self.assertEqual(len(agent.calls), 2)
            first_ids = {str(wi.get("identity_key", "")) for wi in agent.calls[0]}
            self.assertIn("book a|author a", first_ids)
            self.assertIn("book b|author b", first_ids)
            second_ids = {str(wi.get("identity_key", "")) for wi in agent.calls[1]}
            self.assertNotIn("book a|author a", second_ids, "A should not be re-requested")
            self.assertIn("book b|author b", second_ids, "B should be re-requested")

            history = json.loads((output_dir / "candidate-history.json").read_text(encoding="utf-8"))
            a = next((r for r in history if r["identity_key"] == "book a|author a"), None)
            b = next((r for r in history if r["identity_key"] == "book b|author b"), None)
            self.assertEqual(a["content_description"], FIRST_VALID_A, "A must keep its first validated description")
            self.assertEqual(b["content_description"], VALID_B, "B must get its valid description from retry")

            # Diagnostic must not leak the unsolicited prose.
            diag = json.loads((output_dir / "description-enrichment.json").read_text(encoding="utf-8"))
            diag_str = json.dumps(diag)
            self.assertNotIn("Злонамеренное описание", diag_str)

    def test_retry_handles_agent_exception_non_blocking(self):
        """Agent that raises → treated as empty attempt; retry continues."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path, output_dir = _setup_run(
                tmp_path,
                entries=[
                    {
                        "title": "Book X",
                        "primary_authors": ["Author X"],
                        "source_url": "https://example.test/book-a",
                        "evidence_url": "https://example.test/book-a",
                        "evidence_note": "Note.",
                        "candidate_category": "history",
                        "signal_type": "media mention",
                        "signal_strength": "shortlist",
                    },
                ],
                extra_urls=["https://example.test/book-a"],
            )

            class ExceptionThenValidAgent:
                def __init__(self):
                    self.calls: list[list] = []
                    self._attempt = 0

                def generate(self, work_items):
                    self._attempt += 1
                    self.calls.append(list(work_items))
                    if self._attempt == 1:
                        raise RuntimeError("transient failure")
                    return {str(wi.get("identity_key", "")): "Валидное описание." for wi in work_items}

            agent = ExceptionThenValidAgent()
            result = run_weekly_workflow(config_path, output_dir, adapters=_make_adapters(agent=agent))

            self.assertIn(
                result.outcome, {WorkflowOutcome.COMPLETED_NO_ATTENTION, WorkflowOutcome.COMPLETED_ATTENTION_REQUIRED}
            )
            self.assertEqual(len(agent.calls), 2)

            history = json.loads((output_dir / "candidate-history.json").read_text(encoding="utf-8"))
            rec = next((r for r in history if r["identity_key"] == "book x|author x"), None)
            self.assertEqual(rec["content_description"], "Валидное описание.")

    def test_retry_sanitizes_unknown_ids_and_non_dict_response(self):
        """Agent returns non-dict or unknown IDs → sanitized, non-blocking."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path, output_dir = _setup_run(
                tmp_path,
                entries=[
                    {
                        "title": "Book X",
                        "primary_authors": ["Author X"],
                        "source_url": "https://example.test/book-a",
                        "evidence_url": "https://example.test/book-a",
                        "evidence_note": "Note.",
                        "candidate_category": "history",
                        "signal_type": "media mention",
                        "signal_strength": "shortlist",
                    },
                ],
                extra_urls=["https://example.test/book-a"],
            )

            class StrangeAgent:
                def __init__(self):
                    self.calls: list[list] = []

                def generate(self, work_items):
                    self.calls.append(list(work_items))
                    if len(self.calls) == 1:
                        return [1, 2, 3]  # non-dict
                    # Unknown key, oversized value
                    return {
                        "unknown|id": "Some text",
                        "book x|author x": "A" * 10000,
                    }

            agent = StrangeAgent()
            run_weekly_workflow(config_path, output_dir, adapters=_make_adapters(agent=agent))

            # Should have retried since first attempt was non-dict.
            self.assertGreaterEqual(len(agent.calls), 2)
            # Unknown keys dropped, oversized value bounded.
            diag = json.loads((output_dir / "description-enrichment.json").read_text(encoding="utf-8"))
            diag_str = json.dumps(diag)
            self.assertNotIn("unknown|id", diag_str)
            self.assertNotIn("Some text", diag_str)

    def test_russian_completion_says_descriptions_need_review(self):
        """Attention from this-run description failures names count and fix (issue #557)."""
        from autoscout.russian_workflow import completion_message

        msg = completion_message(
            outcome="completed/attention-required",
            new_count=2,
            attention_required=True,
            review_available=True,
            description_coverage={
                "validated_count": 1,
                "fallback_count": 1,
                "total_candidates": 2,
                "coverage": "partial",
            },
            description_stage_failures=1,
        )
        self.assertIn("для 1 книги", msg)
        self.assertIn("обзор", msg)
        self.assertIn("повторит", msg)
        self.assertNotIn("Часть источников не была проверена", msg)

    def test_russian_completion_says_sources_when_no_description_fallback(self):
        """When attention is source-driven without description fallback, legacy message applies."""
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
        self.assertNotIn("Часть описаний требует проверки", msg)

    def test_no_open_review_leakage_in_progress_rendering(self):
        """open-review not in _PROGRESS_STAGES, not leaked by render_workflow_result."""
        from autoscout.russian_workflow import _PROGRESS_STAGES
        from autoscout.russian_workflow import render_workflow_result

        self.assertNotIn("open-review", _PROGRESS_STAGES)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path, output_dir = _setup_run(tmp_path)
            result = run_weekly_workflow(config_path, output_dir, adapters=_make_adapters())
            rendered = render_workflow_result(result)
            self.assertNotIn("open-review", rendered)

    def test_enrichment_before_review_before_health_stage_order(self):
        """Pipeline → enrichment → review → health in log order."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path, output_dir = _setup_run(tmp_path)
            result = run_weekly_workflow(config_path, output_dir, adapters=_make_adapters())
            order = [e["stage"] for e in result.log if e["stage"] in {"pipeline", "enrichment", "review", "health"}]
            self.assertLess(order.index("pipeline"), order.index("enrichment"))
            self.assertLess(order.index("enrichment"), order.index("review"))
            self.assertLess(order.index("review"), order.index("health"))

    def test_enrichment_diagnostic_has_ids_not_raw_text(self):
        """Diagnostic contains IDs, never raw rejected prose."""
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
                        "evidence_note": "Note.",
                        "candidate_category": "history",
                        "signal_type": "media mention",
                        "signal_strength": "shortlist",
                    },
                ],
                extra_urls=["https://example.test/book-a"],
            )
            agent = FakeAgent({"book a|author a": "English only — should be rejected."})
            run_weekly_workflow(config_path, output_dir, adapters=_make_adapters(agent=agent))
            diag = json.loads((output_dir / "description-enrichment.json").read_text(encoding="utf-8"))
            self.assertIn("requested_ids", diag)
            self.assertIn("fallback_ids", diag)
            self.assertIn("applied_ids", diag)
            diag_str = json.dumps(diag)
            self.assertNotIn("English only", diag_str)


class EncodeWorkItemsSafetyTests(unittest.TestCase):
    """Narrow safety tests for _encode_work_items."""

    def test_authors_bounded(self):
        from autoscout.description_enrichment import _encode_work_items

        items = [
            {
                "identity_key": "id|author",
                "title": "Title",
                "metadata": {
                    "primary_authors": ["A" * 1000, "B"],
                    "source_evidence": [{"source_name": "S", "evidence_note": "N"}],
                },
            }
        ]
        encoded = _encode_work_items(items)
        authors = encoded[0]["metadata"]["primary_authors"]
        self.assertEqual(len(authors), 2)
        self.assertLessEqual(len(authors[0]), 200)

    def test_unknown_keys_excluded(self):
        from autoscout.description_enrichment import _encode_work_items

        items = [
            {
                "identity_key": "id",
                "title": "T",
                "metadata": {
                    "raw_body": "should not appear",
                    "article_text": "also excluded",
                    "publisher": "Press",
                },
            }
        ]
        encoded = _encode_work_items(items)
        md = encoded[0]["metadata"]
        self.assertNotIn("raw_body", md)
        self.assertNotIn("article_text", md)
        self.assertIn("publisher", md)

    def test_evidence_references_bounded(self):
        from autoscout.description_enrichment import _encode_work_items

        items = [
            {
                "identity_key": "id",
                "title": "T",
                "metadata": {
                    "source_evidence": [
                        {"source_name": "X" * 1000, "evidence_note": "N", "extra": "should be dropped"},
                    ],
                },
            }
        ]
        encoded = _encode_work_items(items)
        refs = encoded[0]["evidence_references"]
        self.assertEqual(len(refs), 1)
        self.assertLessEqual(len(refs[0]["source_name"]), 200)
        self.assertNotIn("extra", refs[0])

    def test_large_list_truncated(self):
        from autoscout.description_enrichment import _encode_work_items

        items = [
            {
                "identity_key": "id",
                "title": "T",
                "metadata": {
                    "source_evidence": [{"source_name": f"S{i}"} for i in range(50)],
                },
            }
        ]
        encoded = _encode_work_items(items)
        self.assertLessEqual(len(encoded[0]["evidence_references"]), 10)


class DescriptionEnrichmentModuleTests(unittest.TestCase):
    """Unit tests for the description_enrichment module."""

    def test_run_enrichment_skips_missing_scouting(self):
        from autoscout.description_enrichment import run_enrichment

        with tempfile.TemporaryDirectory() as tmp:
            result = run_enrichment(Path(tmp))
            self.assertEqual(result["status"], "skipped")

    def test_run_enrichment_rejects_structurally_invalid_candidates(self):
        from autoscout.description_enrichment import run_enrichment

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "scouting-candidates.json").write_text(
                json.dumps(["tck rejects"]),
                encoding="utf-8",
            )

            result = run_enrichment(tmp_path)

            self.assertEqual(result["status"], "input_failed")
            self.assertEqual(result["reason"], "invalid_scouting_candidates")
            self.assertNotIn("tck rejects", json.dumps(result))

    def test_run_enrichment_no_work_items_when_all_have_descriptions(self):
        from autoscout.description_enrichment import run_enrichment

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "scouting-candidates.json").write_text(
                json.dumps(
                    [
                        {
                            "identity_key": "book|author",
                            "update_delta": "new",
                            "content_description": "Существующее описание.",
                            "source_evidence": [],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            seed_state(
                tmp_path,
                candidate_history=[
                    {
                        "identity_key": "book|author",
                        "content_description": "Существующее описание.",
                        "description_provenance": {"provenance": "codex-generated"},
                        "source_evidence": [],
                    }
                ],
            )
            result = run_enrichment(tmp_path)
            self.assertEqual(result["candidates_considered"], 0)

    def test_run_enrichment_selects_only_new_blank(self):
        from autoscout.description_enrichment import run_enrichment

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "scouting-candidates.json").write_text(
                json.dumps(
                    [
                        {
                            "identity_key": "new_blank|author",
                            "update_delta": "new",
                            "content_description": "",
                            "source_evidence": [],
                        },
                        {
                            "identity_key": "new_valid|author",
                            "update_delta": "new",
                            "content_description": "Уже есть.",
                            "source_evidence": [],
                        },
                        {
                            "identity_key": "old_blank|author",
                            "update_delta": "unchanged",
                            "content_description": "",
                            "source_evidence": [],
                        },
                    ]
                ),
                encoding="utf-8",
            )
            seed_state(
                tmp_path,
                candidate_history=[
                    {"identity_key": "new_blank|author", "content_description": "", "source_evidence": []},
                    {"identity_key": "new_valid|author", "content_description": "Уже есть.", "source_evidence": []},
                    {"identity_key": "old_blank|author", "content_description": "", "source_evidence": []},
                ],
            )

            class _CountingAgent:
                def generate(self, work_items):
                    return {wi["identity_key"]: f"Описание: {wi.get('title', '')}." for wi in work_items}

            result = run_enrichment(tmp_path, agent=_CountingAgent())
            self.assertEqual(result["candidates_considered"], 1)
            self.assertEqual(result["enriched"], 1)


if __name__ == "__main__":
    unittest.main()
