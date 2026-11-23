"""Tests for issue #280: the blessed weekly launcher must determine the
effective run date from the execution clock, not from stale config run_date.

The test injects 2026-07-15 as the clock date with a fixture config that
declares run_date 2026-07-04. Every generated artifact must expose or agree on
2026-07-15. An existing history record with an earlier first_seen_date must
keep its first_seen_date while last_seen_date advances to 2026-07-15.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import Any

from state_store_support import seed_state

from autoscout.review_rendering import render_review
from autoscout.run_health import run_health
from autoscout.weekly_workflow import WeeklyWorkflowAdapters
from autoscout.weekly_workflow import _capture_is_current
from autoscout.weekly_workflow import deterministic_adapters
from autoscout.weekly_workflow import run_weekly_workflow


class _FixedClock:
    def __init__(self, reference: date) -> None:
        self._reference = reference

    def today(self) -> date:
        return self._reference


def _write_fixture_config(
    tmp_path: Path,
    *,
    run_date: str = "2026-07-04",
    readiness_purpose: str = "recurring-weekly",
    editor_row_target: int = 100,
) -> tuple[Path, bytes]:
    config_path = tmp_path / "config.json"
    config = {
        "run_date": run_date,
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
    raw_bytes = json.dumps(config, sort_keys=True).encode("utf-8")
    config_path.write_bytes(raw_bytes)
    # Return the byte snapshot so the test can assert it never changes.
    return config_path, raw_bytes


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
    expected_file = item.get("expected_extraction_file", "test-source-codex-extraction.json")
    result = output_dir / expected_file
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
                        "evidence_locator": "Test Book by Test Author",
                        "evidence_note": "Test note.",
                        "candidate_category": "popular science",
                        "signal_type": "award",
                        "signal_strength": "shortlist",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return result


class Issue280WeeklyRunDateTests(unittest.TestCase):
    """Acceptance / regression suite: the blessed weekly path must be
    clock-authoritative — stale config run_date (2026-07-04) is overridden
    by the effective clock date (2026-07-15)."""

    EFFECTIVE_DATE = date(2026, 7, 15)
    EFFECTIVE_DATE_ISO = "2026-07-15"
    STALE_CONFIG_DATE = "2026-07-04"

    # ── Helper ─────────────────────────────────────────────────────────

    def _make_adapters(self, tmp_path: Path, config_path: Path, output_dir: Path) -> WeeklyWorkflowAdapters:
        # Build deterministic adapters and freeze the clock.
        adapters = deterministic_adapters()
        adapters.clock = _FixedClock(reference=self.EFFECTIVE_DATE)
        return adapters

    def _run_full_workflow(self, tmp_path: Path) -> Path:
        """Set up fixtures, run capture + extraction + full workflow, return output_dir."""
        config_path, _ = _write_fixture_config(tmp_path, run_date=self.STALE_CONFIG_DATE)
        output_dir = tmp_path / "run"
        output_dir.mkdir()

        _write_capture_fixture(tmp_path, output_dir)
        adapters = self._make_adapters(tmp_path, config_path, output_dir)
        adapters.capture.prepare(config_path, output_dir, live=False, effective_date=self.EFFECTIVE_DATE_ISO)
        _write_extraction_fixture(output_dir)

        run_weekly_workflow(config_path, output_dir, adapters=adapters)
        return output_dir

    # ── Test: config bytes unchanged ────────────────────────────────────

    def test_config_bytes_unchanged_after_workflow(self):
        """The input config file must remain byte-for-byte identical after a full workflow run."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path, initial_bytes = _write_fixture_config(tmp_path, run_date=self.STALE_CONFIG_DATE)
            output_dir = tmp_path / "run"
            output_dir.mkdir()

            _write_capture_fixture(tmp_path, output_dir)
            adapters = self._make_adapters(tmp_path, config_path, output_dir)
            adapters.capture.prepare(config_path, output_dir, live=False, effective_date=self.EFFECTIVE_DATE_ISO)
            _write_extraction_fixture(output_dir)

            run_weekly_workflow(config_path, output_dir, adapters=adapters)

            final_bytes = config_path.read_bytes()
            self.assertEqual(initial_bytes, final_bytes, "Config file was modified by the workflow")

    # ── Test: worklist, replay config, run summary agree on effective date ──

    def test_worklist_and_replay_config_have_effective_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = self._run_full_workflow(Path(tmp))

            # Worklist run_date
            worklist = json.loads((output_dir / "codex-extraction-worklist.json").read_text(encoding="utf-8"))
            self.assertEqual(
                worklist.get("run_date"), self.EFFECTIVE_DATE_ISO, "Worklist run_date does not match effective date"
            )

            # Replay config run_date
            replay = json.loads((output_dir / "codex-replay-config.json").read_text(encoding="utf-8"))
            self.assertEqual(
                replay.get("run_date"), self.EFFECTIVE_DATE_ISO, "Replay config run_date does not match effective date"
            )

    # ── Test: run-summary carries effective date ────────────────────────

    def test_run_summary_exposes_effective_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = self._run_full_workflow(Path(tmp))
            run_summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(
                run_summary.get("run_date"),
                self.EFFECTIVE_DATE_ISO,
                "run-summary.json run_date does not match effective date",
            )

    # ── Test: pilot-status / health metadata carries effective date ─────

    def test_pilot_status_exposes_effective_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = self._run_full_workflow(Path(tmp))
            pilot = json.loads((output_dir / "pilot-status.json").read_text(encoding="utf-8"))
            self.assertEqual(
                pilot.get("run_date"),
                self.EFFECTIVE_DATE_ISO,
                "pilot-status.json run_date does not match effective date",
            )

    # ── Test: source-runs entries carry effective check date ────────────

    def test_source_runs_carry_effective_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = self._run_full_workflow(Path(tmp))
            source_runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
            for src in source_runs:
                self.assertEqual(
                    src.get("source_check_date"),
                    self.EFFECTIVE_DATE_ISO,
                    f"source-runs entry {src.get('source_name')} has wrong source_check_date",
                )

    # ── Test: source mentions carry effective check date ────────────────

    def test_source_mentions_carry_effective_check_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = self._run_full_workflow(Path(tmp))
            source_mentions = json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8"))
            for mention in source_mentions:
                self.assertEqual(
                    mention.get("source_check_date"),
                    self.EFFECTIVE_DATE_ISO,
                    f"mention {mention.get('title')} has wrong source_check_date",
                )

    # ── Test: editor-facing CSV Check Date ──────────────────────────────

    def test_editor_facing_csv_check_date(self):
        import csv

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = self._run_full_workflow(Path(tmp))
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertGreater(len(rows), 0, "No editor-facing rows produced")
            for row in rows:
                self.assertEqual(
                    row.get("Check date"),
                    self.EFFECTIVE_DATE_ISO,
                    f"CSV row {row.get('Candidate ID')} has wrong Check date",
                )

    # ── Test: weekly-review.md renders the effective date ───────────────

    def test_weekly_review_md_includes_effective_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = self._run_full_workflow(Path(tmp))
            review_md = (output_dir / "weekly-review.md").read_text(encoding="utf-8")
            self.assertIn(self.EFFECTIVE_DATE_ISO, review_md, "weekly-review.md does not contain the effective date")

    # ── Test: weekly-review.html includes the effective date ────────────

    def test_weekly_review_html_includes_effective_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = self._run_full_workflow(Path(tmp))
            review_html = (output_dir / "weekly-review.html").read_text(encoding="utf-8")
            self.assertIn(
                self.EFFECTIVE_DATE_ISO, review_html, "weekly-review.html does not contain the effective date"
            )

    # ── Test: existing history record preserves first_seen_date ─────────

    def test_existing_history_preserves_earlier_first_seen_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = self._run_full_workflow(Path(tmp))
            candidate_history = json.loads((output_dir / "candidate-history.json").read_text(encoding="utf-8"))
            test_book = [rec for rec in candidate_history if rec.get("identity_key") == "test book|test author"]
            self.assertEqual(len(test_book), 1, f"Expected 1 history record for test book, got {len(test_book)}")
            rec = test_book[0]
            self.assertEqual(
                rec.get("first_seen_date"),
                self.EFFECTIVE_DATE_ISO,
                "New candidate should have first_seen_date == effective date",
            )
            self.assertEqual(
                rec.get("last_seen_date"),
                self.EFFECTIVE_DATE_ISO,
                "New candidate should have last_seen_date == effective date",
            )

    # ── Test: new candidate dates use effective date ────────────────────

    def test_new_candidate_dates_use_effective_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = self._run_full_workflow(Path(tmp))
            candidate_history = json.loads((output_dir / "candidate-history.json").read_text(encoding="utf-8"))
            for rec in candidate_history:
                if rec.get("first_seen_date") != self.EFFECTIVE_DATE_ISO:
                    self.fail(
                        f"History record {rec.get('identity_key')} has first_seen_date "
                        f"{rec.get('first_seen_date')}, expected {self.EFFECTIVE_DATE_ISO}"
                    )
                if rec.get("last_seen_date") != self.EFFECTIVE_DATE_ISO:
                    self.fail(
                        f"History record {rec.get('identity_key')} has last_seen_date "
                        f"{rec.get('last_seen_date')}, expected {self.EFFECTIVE_DATE_ISO}"
                    )

    # ── Test: existing history with earlier first_seen passed forward ───

    def test_existing_history_preserves_earlier_first_seen_across_runs(self):
        """When candidate_history is pre-populated with an earlier first_seen_date,
        the workflow preserves it and advances last_seen_date to the effective date."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path, _ = _write_fixture_config(tmp_path, run_date=self.STALE_CONFIG_DATE)
            output_dir = tmp_path / "run"
            output_dir.mkdir()
            state_dir = tmp_path / "state"
            state_dir.mkdir()

            # Pre-populate candidate history with an earlier first_seen_date.
            existing_history = [
                {
                    "identity_key": "test book|test author",
                    "title": "Test Book",
                    "primary_authors": ["Test Author"],
                    "first_seen_date": "2026-06-01",
                    "last_seen_date": "2026-06-01",
                }
            ]
            seed_state(state_dir, candidate_history=existing_history)

            _write_capture_fixture(tmp_path, output_dir)
            adapters = self._make_adapters(tmp_path, config_path, output_dir)
            adapters.capture.prepare(config_path, output_dir, live=False, effective_date=self.EFFECTIVE_DATE_ISO)
            _write_extraction_fixture(output_dir)

            run_weekly_workflow(config_path, output_dir, state_dir, adapters=adapters)

            candidate_history = json.loads((output_dir / "candidate-history.json").read_text(encoding="utf-8"))
            test_book = [rec for rec in candidate_history if rec.get("identity_key") == "test book|test author"]
            self.assertEqual(len(test_book), 1)
            rec = test_book[0]
            # first_seen_date must remain the earlier date.
            self.assertEqual(
                rec.get("first_seen_date"),
                "2026-06-01",
                f"Existing first_seen_date was overridden: {rec.get('first_seen_date')}",
            )
            # last_seen_date must advance to the effective date.
            self.assertEqual(
                rec.get("last_seen_date"),
                self.EFFECTIVE_DATE_ISO,
                f"Expected last_seen_date {self.EFFECTIVE_DATE_ISO}, got {rec.get('last_seen_date')}",
            )

    # ── Test: capture-current rejects stale worklist ────────────────────

    def test_capture_current_rejects_stale_config_run_date(self):
        """A worklist whose run_date is stale must be rejected even when
        every other resume boundary — operation_id, source identity, capture
        file, capture_sha256 — is valid and current. The run_date mismatch
        alone causes re-capture."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path, _ = _write_fixture_config(tmp_path, run_date=self.STALE_CONFIG_DATE)
            output_dir = tmp_path / "run"
            output_dir.mkdir()

            # Prepare with effective_date=2026-07-15 so the operation_id and
            # resume marker are bound to the effective clock date, not the
            # stale config date.
            _write_capture_fixture(tmp_path, output_dir)
            adapters = self._make_adapters(tmp_path, config_path, output_dir)
            adapters.capture.prepare(config_path, output_dir, live=False, effective_date=self.EFFECTIVE_DATE_ISO)

            # Confirm the worklist is current before mutation.
            self.assertTrue(
                _capture_is_current(output_dir, config_path, self.EFFECTIVE_DATE),
                "Freshly-prepared worklist should be current before run_date mutation",
            )

            # Mutate only the worklist's run_date to the stale config date,
            # keeping operation_id, capture_sha256, source identities,
            # capture file, and resume marker intact.
            worklist_path = output_dir / "codex-extraction-worklist.json"
            fresh_worklist = json.loads(worklist_path.read_text(encoding="utf-8"))
            self.assertEqual(
                fresh_worklist["run_date"],
                self.EFFECTIVE_DATE_ISO,
                "Precondition: worklist run_date should be effective date",
            )
            fresh_worklist["run_date"] = self.STALE_CONFIG_DATE
            worklist_path.write_text(json.dumps(fresh_worklist), encoding="utf-8")

            # After mutation, _capture_is_current must return False due to
            # the run_date mismatch alone.
            self.assertFalse(
                _capture_is_current(output_dir, config_path, self.EFFECTIVE_DATE),
                "Stale-dated worklist should NOT be considered current",
            )

            # The public workflow must recapture.
            result = run_weekly_workflow(config_path, output_dir, adapters=adapters)

            # Capture stage must have run fresh (not "worklist_matches_current_run").
            capture_entries = [e for e in result.log if e["stage"] == "capture"]
            self.assertGreater(len(capture_entries), 0)
            self.assertNotEqual(
                capture_entries[0]["detail"].get("status"),
                "worklist_matches_current_run",
                "Stale config-date worklist was incorrectly treated as current",
            )

            # The new worklist must carry the effective date.
            worklist = json.loads(worklist_path.read_text(encoding="utf-8"))
            self.assertEqual(worklist["run_date"], self.EFFECTIVE_DATE_ISO)

    # ── Test: capture-current rejects blank/missing worklist run_date ───

    def test_capture_current_rejects_blank_worklist_run_date(self):
        """A worklist with blank or missing run_date must be rejected,
        even when all other resume boundaries are valid."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path, _ = _write_fixture_config(tmp_path, run_date=self.STALE_CONFIG_DATE)
            output_dir = tmp_path / "run"
            output_dir.mkdir()

            _write_capture_fixture(tmp_path, output_dir)
            adapters = self._make_adapters(tmp_path, config_path, output_dir)
            adapters.capture.prepare(config_path, output_dir, live=False, effective_date=self.EFFECTIVE_DATE_ISO)

            # Remove the run_date field entirely.
            worklist_path = output_dir / "codex-extraction-worklist.json"
            fresh_worklist = json.loads(worklist_path.read_text(encoding="utf-8"))
            del fresh_worklist["run_date"]
            worklist_path.write_text(json.dumps(fresh_worklist), encoding="utf-8")

            self.assertFalse(
                _capture_is_current(output_dir, config_path, self.EFFECTIVE_DATE),
                "Worklist with missing run_date should NOT be current",
            )


# ── Deterministic Codex adapter for tests ─────────────────────────────────


class _DeterministicCodexAdapter:
    """Deterministic adapter that reads extraction artifacts from the output_dir."""

    def fulfill(self, output_dir: Path) -> dict[str, Any]:
        worklist_path = output_dir / "codex-extraction-worklist.json"
        if not worklist_path.exists():
            return {"status": "failed", "reason": "missing_worklist"}
        worklist = json.loads(worklist_path.read_text(encoding="utf-8"))
        items = worklist.get("work_items", [])
        if not items:
            return {"status": "failed", "reason": "empty_worklist"}
        ready = 0
        for item in items:
            expected = str(item.get("expected_extraction_file", ""))
            if expected and (output_dir / expected).exists():
                ready += 1
        return {"status": "ok", "ready_items": ready, "total_items": len(items)}


class HealthDateInvariantTests(unittest.TestCase):
    """``run_health`` date-consistency invariant: when an explicit
    effective_date is provided, every artifact date must agree or
    the health check must return non-zero."""

    EFFECTIVE_DATE = "2026-07-15"
    STALE_DATE = "2026-07-04"

    def _write_artifacts(
        self, output_dir: Path, run_summary_date: str, pilot_date: str, source_dates: list[str]
    ) -> None:
        """Write a minimal, health-passing artifact set with controlled dates."""
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "run-summary.json").write_text(
            json.dumps(
                {
                    "run_date": run_summary_date,
                    "readiness_purpose": "seed-calibration",
                    "editor_facing_count": 5,
                    "editor_row_target": 10,
                    "skipped_source_count": 0,
                    "failed_source_count": 0,
                    "ignored_source_count": 0,
                    "collection_mode": "fixture-live",
                }
            ),
            encoding="utf-8",
        )
        (output_dir / "pilot-status.json").write_text(
            json.dumps(
                {
                    "run_date": pilot_date,
                    "ready_for_editor": True,
                    "editor_facing_count": 5,
                    "readiness_basis": {"readiness_purpose": "seed-calibration", "editor_row_target": 10},
                    "caveats": [],
                    "shareable_artifacts": [],
                }
            ),
            encoding="utf-8",
        )
        (output_dir / "source-runs.json").write_text(
            json.dumps(
                [
                    {
                        "source_name": "Source A",
                        "status": "collected",
                        "collection_mode": "fixture-live",
                        "source_check_date": sd,
                    }
                    for sd in source_dates
                ]
            ),
            encoding="utf-8",
        )

    def test_health_passes_when_all_dates_agree(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "run"
            self._write_artifacts(output_dir, self.EFFECTIVE_DATE, self.EFFECTIVE_DATE, [self.EFFECTIVE_DATE])
            rc = run_health(output_dir, effective_date=self.EFFECTIVE_DATE)
            self.assertEqual(rc, 0, "Health should pass when all dates agree")

    def test_health_fails_on_run_summary_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "run"
            self._write_artifacts(output_dir, self.STALE_DATE, self.EFFECTIVE_DATE, [self.EFFECTIVE_DATE])
            rc = run_health(output_dir, effective_date=self.EFFECTIVE_DATE)
            self.assertNotEqual(rc, 0, "Health should fail when run-summary date mismatches")

    def test_health_fails_on_pilot_status_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "run"
            self._write_artifacts(output_dir, self.EFFECTIVE_DATE, self.STALE_DATE, [self.EFFECTIVE_DATE])
            rc = run_health(output_dir, effective_date=self.EFFECTIVE_DATE)
            self.assertNotEqual(rc, 0, "Health should fail when pilot-status date mismatches")

    def test_health_fails_on_source_runs_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "run"
            self._write_artifacts(output_dir, self.EFFECTIVE_DATE, self.EFFECTIVE_DATE, [self.STALE_DATE])
            rc = run_health(output_dir, effective_date=self.EFFECTIVE_DATE)
            self.assertNotEqual(rc, 0, "Health should fail when source-runs date mismatches")

    def test_health_fails_on_missing_run_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "run"
            self._write_artifacts(output_dir, "", self.EFFECTIVE_DATE, [self.EFFECTIVE_DATE])
            rc = run_health(output_dir, effective_date=self.EFFECTIVE_DATE)
            self.assertNotEqual(rc, 0, "Health should fail when run-summary has no run_date")

    def test_health_passes_without_effective_date(self):
        """Standalone run_health without effective_date must not enforce date invariants."""
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "run"
            self._write_artifacts(output_dir, self.STALE_DATE, self.STALE_DATE, [self.STALE_DATE])
            rc = run_health(output_dir)
            self.assertEqual(rc, 0, "Standalone health should pass without explicit effective_date")

    def test_health_fails_on_blank_source_check_dates(self):
        """source-runs entries with blank source_check_date must fail health."""
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "run"
            self._write_artifacts(output_dir, self.EFFECTIVE_DATE, self.EFFECTIVE_DATE, ["", self.EFFECTIVE_DATE])
            rc = run_health(output_dir, effective_date=self.EFFECTIVE_DATE)
            self.assertNotEqual(rc, 0, "Health should fail when blank source_check_dates are present")

    def test_health_fails_on_blank_source_mentions_dates(self):
        """source-mentions entries with blank/missing source_check_date must fail health."""
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "run"
            self._write_artifacts(output_dir, self.EFFECTIVE_DATE, self.EFFECTIVE_DATE, [self.EFFECTIVE_DATE])
            # Write a source-mentions.json with a blank source_check_date.
            (output_dir / "source-mentions.json").write_text(
                json.dumps([{"title": "Some Book", "source_check_date": ""}]),
                encoding="utf-8",
            )
            rc = run_health(output_dir, effective_date=self.EFFECTIVE_DATE)
            self.assertNotEqual(rc, 0, "Health should fail when source-mention check date is blank")


class RenderReviewDateInvariantTests(unittest.TestCase):
    """``render_review`` date-consistency: explicit effective_date that
    disagrees with a nonblank run-summary run_date must fail rendering."""

    EFFECTIVE_DATE = "2026-07-15"
    STALE_DATE = "2026-07-04"

    def test_render_review_passes_when_dates_agree(self):
        """Render succeeds when explicit date matches run-summary."""
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "run"
            output_dir.mkdir()
            # Write a minimal run-summary and scouting-candidates so the
            # render can proceed past the date check.
            (output_dir / "run-summary.json").write_text(
                json.dumps({"run_date": self.EFFECTIVE_DATE, "editor_facing_count": 0, "editor_row_target": 10}),
                encoding="utf-8",
            )
            (output_dir / "scouting-candidates.json").write_text("[]", encoding="utf-8")
            rc = render_review(output_dir, effective_date=self.EFFECTIVE_DATE)
            self.assertEqual(rc, 0, "Render should pass when dates agree")

    def test_render_review_fails_on_date_mismatch(self):
        """Render must fail when explicit effective_date disagrees with
        a nonblank run-summary run_date."""
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "run"
            output_dir.mkdir()
            (output_dir / "run-summary.json").write_text(
                json.dumps({"run_date": self.STALE_DATE, "editor_facing_count": 0, "editor_row_target": 10}),
                encoding="utf-8",
            )
            (output_dir / "scouting-candidates.json").write_text("[]", encoding="utf-8")
            rc = render_review(output_dir, effective_date=self.EFFECTIVE_DATE)
            self.assertNotEqual(rc, 0, "Render should fail when dates disagree")

    def test_render_review_passes_without_explicit_date(self):
        """Standalone render without explicit effective_date must succeed."""
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "run"
            output_dir.mkdir()
            (output_dir / "run-summary.json").write_text(
                json.dumps({"run_date": self.STALE_DATE, "editor_facing_count": 0, "editor_row_target": 10}),
                encoding="utf-8",
            )
            (output_dir / "scouting-candidates.json").write_text("[]", encoding="utf-8")
            rc = render_review(output_dir)
            self.assertEqual(rc, 0, "Standalone render should pass without explicit effective_date")

    def test_render_review_passes_when_run_summary_date_blank(self):
        """Render succeeds when explicit effective_date provided and run-summary has no run_date."""
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "run"
            output_dir.mkdir()
            (output_dir / "run-summary.json").write_text(
                json.dumps({"editor_facing_count": 0, "editor_row_target": 10}),
                encoding="utf-8",
            )
            (output_dir / "scouting-candidates.json").write_text("[]", encoding="utf-8")
            rc = render_review(output_dir, effective_date=self.EFFECTIVE_DATE)
            self.assertEqual(rc, 0, "Render should pass when run-summary has no run_date")


if __name__ == "__main__":
    unittest.main()
