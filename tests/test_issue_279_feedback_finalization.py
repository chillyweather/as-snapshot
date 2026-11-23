"""Acceptance tests for issue #279: safe feedback finalization.

Covers:
- Feedback finalization preserves original weekly delta and lifecycle.
- 8-New / 21-Updated / 31-Unchanged fixture: exact delta counts before and after.
- New row stays New (including HTML Review-status rendering).
- Editor status/note persisted, all non-editor fields + history dates unchanged.
- Second run+render is byte-stable or semantically identical (idempotent).
- Unicode/multiline notes in state + CSV + HTML.
- Pending feedback archived (sorted, NOT deduplicated) and cleared exactly once.
- Full-pipeline behavior with fresh output_dir unchanged.
- Different-output/new-week runs retain existing full-pipeline behavior.
- Newly-staged feedback after prior finalization is consumed (not no-op'd).
- State guard: profile/visibility change invalidates no-op (observable).
- Mixed-date rejection: nonempty source-mentions with mixed dates → immediate skip.
- History/latest-candidates written in existing order (not re-sorted).
"""

from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_snapshot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "autoscout", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _fixture_mention(
    title: str,
    authors: list[str],
    source_name: str = "FixtureA",
    source_family: str = "award",
    signal: str = "shortlist",
    category: str = "popular science",
    isbn: str = "",
    source_url: str = "",
    run_date: str = "2026-07-15",
) -> dict:
    return {
        "title": title,
        "primary_authors": authors,
        "source_name": source_name,
        "source_family": source_family,
        "source_url": source_url or f"https://example.test/{title.replace(' ', '-').lower()}",
        "source_check_date": run_date,
        "signal_strength": signal,
        "evidence_note": f"Test mention for {title}.",
        "candidate_category": category,
        "isbn": isbn,
    }


def _build_60_candidate_fixture() -> tuple[list[dict], list[dict], set[str], set[str], set[str]]:
    """Build a fixture with 8 New, 21 Updated, 31 Unchanged (60 total).

    Returns (source_mentions, candidate_history,
             expected_new_ids, expected_updated_ids, expected_unchanged_ids).
    """
    # 8 New books (never seen before)
    new_books: list[dict] = []
    for i in range(1, 9):
        new_books.append(
            _fixture_mention(
                f"New Book {i}",
                [f"New Author {i}"],
                source_name="AwardSource",
                source_family="award",
                signal="winner" if i <= 2 else "shortlist",
                category="business" if i <= 4 else "popular science",
            )
        )

    # 21 Updated books (seen before — signal materially changed)
    updated_books: list[dict] = []
    for i in range(1, 22):
        updated_books.append(
            _fixture_mention(
                f"Updated Book {i}",
                [f"Updated Author {i}"],
                source_name="AwardSource",
                source_family="award",
                signal="winner",  # upgraded from shortlist in history
                category="health" if i <= 7 else "business",
            )
        )

    # 31 Unchanged books (same signal as history → unchanged)
    unchanged_books: list[dict] = []
    for i in range(1, 32):
        unchanged_books.append(
            _fixture_mention(
                f"Unchanged Book {i}",
                [f"Unchanged Author {i}"],
                source_name="AwardSource",
                source_family="award",
                signal="shortlist",  # same as history → unchanged
                category="popular science",
            )
        )

    # History for the 21 Updated books (old shortlist → current winner → upgraded)
    history: list[dict] = []
    for i in range(1, 22):
        title = f"Updated Book {i}"
        authors = [f"Updated Author {i}"]
        identity_key = f"updated book {i}|updated author {i}"
        history.append(
            {
                "identity_key": identity_key,
                "title": title,
                "primary_authors": authors,
                "first_seen_date": "2026-07-01",
                "last_seen_date": "2026-07-01",
                "priority_label": "C",
                "independent_signal_count": 1,
                "independent_signal_families": ["award"],
                "primary_signal_strength": "shortlist",
                "source_evidence": [
                    {
                        "source_name": "OldSource",
                        "source_family": "award",
                        "signal_strength": "shortlist",
                        "evidence_note": "Was shortlisted before.",
                        "source_check_date": "2026-07-01",
                        "evidence_url": f"https://example.test/old/updated-{i}",
                    }
                ],
            }
        )

    # History for the 31 Unchanged books (same signal → unchanged)
    for i in range(1, 32):
        title = f"Unchanged Book {i}"
        authors = [f"Unchanged Author {i}"]
        identity_key = f"unchanged book {i}|unchanged author {i}"
        history.append(
            {
                "identity_key": identity_key,
                "title": title,
                "primary_authors": authors,
                "first_seen_date": "2026-07-01",
                "last_seen_date": "2026-07-01",
                "last_material_change_date": "2026-07-01",
                "priority_label": "C",
                "independent_signal_count": 1,
                "independent_signal_families": ["award"],
                "primary_signal_strength": "shortlist",
                "source_evidence": [
                    {
                        "source_name": "OldSource",
                        "source_family": "award",
                        "signal_strength": "shortlist",
                        "evidence_note": "Same signal as before.",
                        "source_check_date": "2026-07-01",
                        "evidence_url": f"https://example.test/old/unchanged-{i}",
                    }
                ],
            }
        )

    expected_new_ids = {f"new book {i}|new author {i}" for i in range(1, 9)}
    expected_updated_ids = {f"updated book {i}|updated author {i}" for i in range(1, 22)}
    expected_unchanged_ids = {f"unchanged book {i}|unchanged author {i}" for i in range(1, 32)}

    all_mentions = new_books + updated_books + unchanged_books
    return all_mentions, history, expected_new_ids, expected_updated_ids, expected_unchanged_ids


def _build_60_candidate_scenario(
    base_dir: Path,
    state_dir: Path | None = None,
) -> tuple[Path, Path, Path]:
    """Set up a full pipeline run with 8 New + 21 Updated + 31 Unchanged candidates.

    Returns (config_path, output_dir, state_dir).
    """
    mentions, history, _, _, _ = _build_60_candidate_fixture()

    config = {
        "run_date": "2026-07-15",
        "sources": [
            {
                "name": "TestAwards",
                "family": "award",
                "url": "https://example.test/awards",
                "access_status": "open",
                "extraction_method": "fixture",
                "check_frequency": "weekly",
                "language": "en",
                "fixture_candidates": [
                    {
                        "title": m["title"],
                        "primary_authors": m["primary_authors"],
                        "source_url": m["source_url"],
                        "signal_strength": m["signal_strength"],
                        "evidence_note": m["evidence_note"],
                        "candidate_category": m["candidate_category"],
                    }
                    for m in mentions
                ],
            }
        ],
        "candidate_history": history,
    }

    config_path = base_dir / "config.json"
    _write_json(config_path, config)
    output_dir = base_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    if state_dir is None:
        state_dir = base_dir / "state"

    seed_state(state_dir, candidate_history=history)

    return config_path, output_dir, state_dir


def _snapshot_candidate_key_fields(candidates: list[dict]) -> list[dict]:
    """Extract key non-editor fields for comparison."""
    return [
        {
            "identity_key": c["identity_key"],
            "update_delta": c.get("update_delta", ""),
            "title": c.get("title", ""),
            "primary_authors": c.get("primary_authors", []),
            "source_evidence": json.dumps(c.get("source_evidence", []), sort_keys=True, ensure_ascii=False),
            "priority_label": c.get("priority_label", ""),
        }
        for c in candidates
    ]


def _history_date_snapshot(history: list[dict]) -> dict[str, dict[str, str]]:
    """Return {identity_key: {first_seen_date, last_seen_date, last_material_change_date}}."""
    return {
        str(r.get("identity_key", "")): {
            "first_seen_date": str(r.get("first_seen_date", "")),
            "last_seen_date": str(r.get("last_seen_date", "")),
            "last_material_change_date": str(r.get("last_material_change_date", "")),
        }
        for r in history
        if r.get("identity_key")
    }


# ---------------------------------------------------------------------------
# Unit test: date classification
# ---------------------------------------------------------------------------


class SourceMentionsDateStatusTests(unittest.TestCase):
    """Direct unit tests for _source_mentions_date_status."""

    def test_unavailable_when_file_absent(self):
        from autoscout.feedback_finalization import _SM_DATE_UNAVAILABLE
        from autoscout.feedback_finalization import _source_mentions_date_status

        with tempfile.TemporaryDirectory() as tmp:
            status, _ = _source_mentions_date_status(Path(tmp))
            self.assertEqual(status, _SM_DATE_UNAVAILABLE)

    def test_unavailable_when_empty_list(self):
        from autoscout.feedback_finalization import _SM_DATE_UNAVAILABLE
        from autoscout.feedback_finalization import _source_mentions_date_status

        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_json(d / "source-mentions.json", [])
            status, _ = _source_mentions_date_status(d)
            self.assertEqual(status, _SM_DATE_UNAVAILABLE)

    def test_uniform_when_all_same_date(self):
        from autoscout.feedback_finalization import _SM_DATE_UNIFORM
        from autoscout.feedback_finalization import _source_mentions_date_status

        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_json(
                d / "source-mentions.json",
                [
                    {"source_check_date": "2026-07-15", "title": "A"},
                    {"source_check_date": "2026-07-15", "title": "B"},
                ],
            )
            status, date_val = _source_mentions_date_status(d)
            self.assertEqual(status, _SM_DATE_UNIFORM)
            self.assertEqual(date_val, "2026-07-15")

    def test_mixed_when_different_dates(self):
        from autoscout.feedback_finalization import _SM_DATE_MIXED
        from autoscout.feedback_finalization import _source_mentions_date_status

        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_json(
                d / "source-mentions.json",
                [
                    {"source_check_date": "2026-07-15", "title": "A"},
                    {"source_check_date": "2026-07-16", "title": "B"},
                ],
            )
            status, _ = _source_mentions_date_status(d)
            self.assertEqual(status, _SM_DATE_MIXED)


class MixedDateFinalizationSkipsTests(unittest.TestCase):
    """Mixed source-mention dates must skip finalization."""

    def test_mixed_dates_skip_even_with_pending_feedback(self):
        """Stage pending feedback, tamper source mentions to mixed dates,
        run run-mvp — finalization must not apply (marker absent, delta recomputed)."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path, output_dir, state_dir = _build_60_candidate_scenario(base)

            # Initial run
            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )

            candidates = json.loads((output_dir / "scouting-candidates.json").read_text())
            new_candidate = next(c for c in candidates if c["update_delta"] == "new")

            # Record confirmed feedback
            _run(
                "record-feedback",
                "--identity-key",
                new_candidate["identity_key"],
                "--status",
                "interested",
                "--note",
                "Test mixed dates.",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            # Tamper source mentions to mixed dates
            mentions = json.loads((output_dir / "source-mentions.json").read_text())
            self.assertTrue(len(mentions) > 0, "Fixture must produce nonempty mentions")
            mentions[0]["source_check_date"] = "2026-07-16"  # different date
            _write_json(output_dir / "source-mentions.json", mentions)

            # Remove any prior marker so we observe a fresh result
            marker_path = output_dir / ".feedback-finalization-marker.json"
            if marker_path.exists():
                marker_path.unlink()

            # run-mvp — should fall through to full pipeline (skipped, not applied)
            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )

            # Full pipeline ran: marker is refreshed, source-mentions rewritten with uniform date
            self.assertTrue(marker_path.exists(), "Marker should be refreshed after full pipeline run")

            # Source mentions are now uniform (full pipeline rewrote them)
            mentions2 = json.loads((output_dir / "source-mentions.json").read_text())
            dates2 = {
                str(m.get("source_check_date", "")) for m in mentions2 if str(m.get("source_check_date", "")).strip()
            }
            self.assertEqual(
                len(dates2), 1, f"Source mentions should have uniform date after full pipeline, got {dates2}"
            )

            # Full pipeline consumed pending via normal path
            pending = read_state(state_dir, "pending_editor_rows")
            self.assertEqual(len(pending), 0)

            # Feedback must be in history (consumed by full pipeline, not in-place finalization)
            history = read_state(state_dir, "candidate_history")
            hist_record = next((r for r in history if r["identity_key"] == new_candidate["identity_key"]), None)
            self.assertIsNotNone(hist_record)
            self.assertEqual(hist_record.get("editor_status", ""), "interested")


# ---------------------------------------------------------------------------
# Core acceptance tests
# ---------------------------------------------------------------------------


class ExactDeltaCountsTests(unittest.TestCase):
    """60-row fixture: exactly 8 new, 21 upgraded, 31 unchanged."""

    def test_initial_run_produces_exact_delta_counts(self):
        """Run the full pipeline once — verify exact delta counts."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path, output_dir, state_dir = _build_60_candidate_scenario(base)

            result = _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            self.assertEqual(result.returncode, 0, f"Pipeline failed: {result.stderr}")

            candidates = json.loads((output_dir / "scouting-candidates.json").read_text())
            self.assertEqual(len(candidates), 60, f"Expected 60 candidates, got {len(candidates)}")

            delta_counts: dict[str, int] = {}
            for c in candidates:
                d = str(c.get("update_delta", "unknown"))
                delta_counts[d] = delta_counts.get(d, 0) + 1

            self.assertEqual(delta_counts.get("new", 0), 8)
            self.assertEqual(delta_counts.get("upgraded", 0), 21)
            self.assertEqual(delta_counts.get("unchanged", 0), 31)

    def test_feedback_finalization_preserves_exact_data(self):
        """Record feedback, run-mvp+render-review — all deltas, source artifacts,
        run-summary, history dates, and candidate fields preserved."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path, output_dir, state_dir = _build_60_candidate_scenario(base)

            # Phase 1: Initial full pipeline run
            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )

            # Capture EVERYTHING before finalization
            before_candidates = json.loads((output_dir / "scouting-candidates.json").read_text())
            before_mentions_bytes = (output_dir / "source-mentions.json").read_bytes()
            before_runs_bytes = (
                (output_dir / "source-runs.json").read_bytes() if (output_dir / "source-runs.json").exists() else b""
            )
            before_summary_bytes = (output_dir / "run-summary.json").read_bytes()
            before_order = [c["identity_key"] for c in before_candidates]
            before_key_fields = _snapshot_candidate_key_fields(before_candidates)
            before_history = read_state(state_dir, "candidate_history")
            before_history_dates = _history_date_snapshot(before_history)

            # Delta counts
            before_deltas: dict[str, int] = {}
            for c in before_candidates:
                before_deltas[str(c.get("update_delta", "unknown"))] = (
                    before_deltas.get(str(c.get("update_delta", "unknown")), 0) + 1
                )
            self.assertEqual(before_deltas.get("new", 0), 8)
            self.assertEqual(before_deltas.get("upgraded", 0), 21)
            self.assertEqual(before_deltas.get("unchanged", 0), 31)

            # Pick one New, one Updated, one Unchanged
            new_candidate = next(c for c in before_candidates if c["update_delta"] == "new")
            updated_candidate = next(c for c in before_candidates if c["update_delta"] == "upgraded")
            unchanged_candidate = next(c for c in before_candidates if c["update_delta"] == "unchanged")

            note_new = 'Интересная книга — надо \n"проверить" права.'
            note_upd = "Больше не актуально."
            note_unc = "Подумать."

            # Record feedback
            for identity_key, status, note in [
                (new_candidate["identity_key"], "interested", note_new),
                (updated_candidate["identity_key"], "reject", note_upd),
                (unchanged_candidate["identity_key"], "maybe_watch", note_unc),
            ]:
                _run(
                    "record-feedback",
                    "--identity-key",
                    identity_key,
                    "--status",
                    status,
                    "--note",
                    note,
                    "--state-dir",
                    str(state_dir),
                    "--confirmed-by-editor",
                )

            # Phase 2: run-mvp (feedback finalization) + render-review
            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            _run("render-review", "--output-dir", str(output_dir), "--state-dir", str(state_dir))

            # ── Immutable artifacts preserved byte-for-byte ──
            self.assertEqual(before_mentions_bytes, (output_dir / "source-mentions.json").read_bytes())
            if before_runs_bytes:
                self.assertEqual(before_runs_bytes, (output_dir / "source-runs.json").read_bytes())
            self.assertEqual(before_summary_bytes, (output_dir / "run-summary.json").read_bytes())

            # ── Candidate ordering preserved ──
            final_candidates = json.loads((output_dir / "scouting-candidates.json").read_text())
            self.assertEqual(before_order, [c["identity_key"] for c in final_candidates])

            # ── Key non-editor fields preserved ──
            self.assertEqual(before_key_fields, _snapshot_candidate_key_fields(final_candidates))

            # ── Delta counts unchanged ──
            final_deltas: dict[str, int] = {}
            for c in final_candidates:
                final_deltas[str(c.get("update_delta", "unknown"))] = (
                    final_deltas.get(str(c.get("update_delta", "unknown")), 0) + 1
                )
            self.assertEqual(final_deltas.get("new", 0), 8)
            self.assertEqual(final_deltas.get("upgraded", 0), 21)
            self.assertEqual(final_deltas.get("unchanged", 0), 31)

            # ── Every history date preserved ──
            after_history = read_state(state_dir, "candidate_history")
            after_history_dates = _history_date_snapshot(after_history)
            for ik, before_dates in before_history_dates.items():
                after_dates = after_history_dates.get(ik, {})
                self.assertEqual(
                    before_dates["first_seen_date"],
                    after_dates.get("first_seen_date", ""),
                    f"first_seen_date changed for {ik}",
                )
                self.assertEqual(
                    before_dates["last_seen_date"],
                    after_dates.get("last_seen_date", ""),
                    f"last_seen_date changed for {ik}",
                )
                self.assertEqual(
                    before_dates["last_material_change_date"],
                    after_dates.get("last_material_change_date", ""),
                    f"last_material_change_date changed for {ik}",
                )

            # ── Editor decisions applied ──
            new_final = next((c for c in final_candidates if c["identity_key"] == new_candidate["identity_key"]), None)
            self.assertIsNotNone(new_final)
            self.assertEqual(new_final["update_delta"], "new")
            self.assertEqual(new_final.get("editor_status", ""), "interested")
            self.assertEqual(new_final.get("editor_note", ""), note_new)

            # ── CSV: New stays "New" ──
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as f:
                csv_rows = list(csv.DictReader(f))
            csv_row = next((r for r in csv_rows if r.get("Candidate ID") == new_candidate["identity_key"]), None)
            self.assertIsNotNone(csv_row)
            self.assertEqual(csv_row.get("Lifecycle", ""), "New")
            self.assertEqual(csv_row.get("Update delta", ""), "new")
            self.assertEqual(csv_row.get("Editor note", ""), note_new)

            # ── HTML: Review-status = "New" and verbatim note ──
            html = (output_dir / "weekly-review.html").read_text(encoding="utf-8")
            escaped_title = re.escape(new_candidate["title"].lower())
            # Find the <details> block by presentation data, never internal identity.
            block_pattern = re.compile(
                rf'<details[^>]*\bdata-title="{escaped_title}"[^>]*>.*?</details>',
                re.DOTALL,
            )
            block_match = block_pattern.search(html)
            self.assertIsNotNone(block_match, f"HTML block not found for {new_candidate['identity_key']}")
            block = block_match.group(0)
            # Product rule changed by #1057 (authorized by the operator in the
            # issue on 2026-11-13): the card shows no lifecycle status; a new
            # book is one without the updated mark. The CSV keeps "New" above.
            self.assertNotIn('class="upd"', block, "A new candidate must not carry the updated mark")
            self.assertNotIn("Новые", block)
            # Verbatim note (newlines may be normalized, but key fragments survive)
            self.assertIn("проверить", block)
            self.assertIn("Интересная книга", block)

            # ── History: order preserved (no sort applied) ──
            before_hist_order = [r["identity_key"] for r in before_history]
            after_hist_order = [r["identity_key"] for r in after_history]
            self.assertEqual(before_hist_order, after_hist_order, "History reordered by finalization")

            # ── Pending cleared, archived has 3 entries ──
            pending = read_state(state_dir, "pending_editor_rows")
            self.assertEqual(len(pending), 0)
            archive = read_state(state_dir, "archived_editor_rows")
            self.assertGreaterEqual(len(archive), 3)


class SuccessiveFeedbackTests(unittest.TestCase):
    """New feedback after prior finalization must be consumed, not no-op'd."""

    def test_new_feedback_after_prior_finalization_is_consumed(self):
        """Finalize feedback A, stage feedback B, finalize again — B consumed, both persist."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path, output_dir, state_dir = _build_60_candidate_scenario(base)

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )

            candidates = json.loads((output_dir / "scouting-candidates.json").read_text())
            new_a = next(c for c in candidates if c["update_delta"] == "new")
            new_b = next(
                c for c in candidates if c["update_delta"] == "new" and c["identity_key"] != new_a["identity_key"]
            )

            # Feedback A
            _run(
                "record-feedback",
                "--identity-key",
                new_a["identity_key"],
                "--status",
                "interested",
                "--note",
                "Decision A.",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )

            pending = read_state(state_dir, "pending_editor_rows")
            self.assertEqual(len(pending), 0)

            candidates2 = json.loads((output_dir / "scouting-candidates.json").read_text())
            new_a_after = next((c for c in candidates2 if c["identity_key"] == new_a["identity_key"]), None)
            self.assertEqual(new_a_after.get("editor_status", ""), "interested")

            # Feedback B
            _run(
                "record-feedback",
                "--identity-key",
                new_b["identity_key"],
                "--status",
                "reject",
                "--note",
                "Decision B.",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            result = _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            self.assertEqual(result.returncode, 0)

            candidates_final = json.loads((output_dir / "scouting-candidates.json").read_text())
            self.assertEqual(
                next(c for c in candidates_final if c["identity_key"] == new_a["identity_key"]).get(
                    "editor_status", ""
                ),
                "interested",
            )
            self.assertEqual(
                next(c for c in candidates_final if c["identity_key"] == new_b["identity_key"]).get(
                    "editor_status", ""
                ),
                "reject",
            )

            pending2 = read_state(state_dir, "pending_editor_rows")
            self.assertEqual(len(pending2), 0)

            archive = read_state(state_dir, "archived_editor_rows")
            arch_ids = {r.get("identity_key", "") for r in archive}
            self.assertIn(new_a["identity_key"], arch_ids)
            self.assertIn(new_b["identity_key"], arch_ids)


class IdempotencyTests(unittest.TestCase):
    """Repeating safe finalization must be idempotent."""

    def test_second_pair_bytes_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path, output_dir, state_dir = _build_60_candidate_scenario(base)

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )

            candidates = json.loads((output_dir / "scouting-candidates.json").read_text())
            new_candidate = next(c for c in candidates if c["update_delta"] == "new")

            _run(
                "record-feedback",
                "--identity-key",
                new_candidate["identity_key"],
                "--status",
                "interested",
                "--note",
                "Test idempotency.",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            _run("render-review", "--output-dir", str(output_dir), "--state-dir", str(state_dir))

            scouting1 = (output_dir / "scouting-candidates.json").read_bytes()
            csv1 = (output_dir / "editor-facing.csv").read_bytes()
            html1 = (output_dir / "weekly-review.html").read_bytes()
            archive1 = state_snapshot(state_dir)

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            _run("render-review", "--output-dir", str(output_dir), "--state-dir", str(state_dir))

            # Artifacts byte-identical
            self.assertEqual(scouting1, (output_dir / "scouting-candidates.json").read_bytes())
            self.assertEqual(csv1, (output_dir / "editor-facing.csv").read_bytes())
            self.assertEqual(html1, (output_dir / "weekly-review.html").read_bytes())
            # Archive not duplicated
            self.assertEqual(archive1, state_snapshot(state_dir))
            # Pending empty
            pending = read_state(state_dir, "pending_editor_rows")
            self.assertEqual(len(pending), 0)

    def test_archive_not_duplicated_on_noop(self):
        """Second no-pending finalization must not append duplicate archive entries."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path, output_dir, state_dir = _build_60_candidate_scenario(base)

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text())
            new_candidate = next(c for c in candidates if c["update_delta"] == "new")

            _run(
                "record-feedback",
                "--identity-key",
                new_candidate["identity_key"],
                "--status",
                "interested",
                "--note",
                "Test.",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            archive_before = read_state(state_dir, "archived_editor_rows")
            self.assertGreaterEqual(len(archive_before), 1)

            # Second run with no pending → no-op
            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            archive_after = read_state(state_dir, "archived_editor_rows")

            self.assertEqual(len(archive_before), len(archive_after), "Archive duplicated on no-op run")

    def test_two_identical_staged_rows_preserved(self):
        """Two separately staged identical rows → two archive entries."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path, output_dir, state_dir = _build_60_candidate_scenario(base)

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text())
            new_candidate = next(c for c in candidates if c["update_delta"] == "new")
            ik = new_candidate["identity_key"]

            # Stage and confirm same feedback twice
            for _ in range(2):
                _run(
                    "record-feedback",
                    "--identity-key",
                    ik,
                    "--status",
                    "interested",
                    "--note",
                    "Same note.",
                    "--state-dir",
                    str(state_dir),
                    "--confirmed-by-editor",
                )

            self.assertEqual(len(read_state(state_dir, "pending_editor_rows")), 2)

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )

            archive = read_state(state_dir, "archived_editor_rows")
            matching = [r for r in archive if r.get("identity_key") == ik]
            self.assertEqual(len(matching), 2, "Two identical staged rows must produce two archive entries")


class StateGuardTests(unittest.TestCase):
    """State guard: profile/visibility changes must invalidate no-op (observable)."""

    def test_profile_change_causes_full_pipeline_run(self):
        """After finalization + profile change, run_mvp must run full pipeline
        (observable: marker replaces / source-mentions re-written)."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path, output_dir, state_dir = _build_60_candidate_scenario(base)

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text())
            new_candidate = next(c for c in candidates if c["update_delta"] == "new")

            _run(
                "record-feedback",
                "--identity-key",
                new_candidate["identity_key"],
                "--status",
                "interested",
                "--note",
                "Test.",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )

            # Snapshot marker + source-artifact timestamp
            marker_before = json.loads((output_dir / ".feedback-finalization-marker.json").read_text())
            source_mtime_before = (output_dir / "source-mentions.json").stat().st_mtime

            # Add profile entry (changes state guard)
            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "author",
                "--subject",
                "Some Author",
                "--quote",
                "Block this.",
                "--provenance",
                "test",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )

            # Observable: marker replaced (state_guard differs)
            marker_after = json.loads((output_dir / ".feedback-finalization-marker.json").read_text())
            self.assertNotEqual(
                marker_before.get("state_guard"),
                marker_after.get("state_guard"),
                "State guard must differ after profile change",
            )

            # Observable: full pipeline rewrote source artifacts (mtime changed)
            source_mtime_after = (output_dir / "source-mentions.json").stat().st_mtime
            self.assertNotEqual(
                source_mtime_before,
                source_mtime_after,
                "Full pipeline must rewrite source artifacts after state change",
            )

    def test_visibility_change_causes_full_pipeline_run(self):
        """After finalization + visibility change, full pipeline re-runs."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path, output_dir, state_dir = _build_60_candidate_scenario(base)

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text())
            new_candidate = next(c for c in candidates if c["update_delta"] == "new")

            _run(
                "record-feedback",
                "--identity-key",
                new_candidate["identity_key"],
                "--status",
                "interested",
                "--note",
                "Test.",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )

            marker_before = json.loads((output_dir / ".feedback-finalization-marker.json").read_text())
            source_mtime_before = (output_dir / "source-mentions.json").stat().st_mtime

            # Hide candidate (changes visibility state)
            _run(
                "record-visibility",
                "--identity-key",
                new_candidate["identity_key"],
                "--action",
                "hide",
                "--reason",
                "Test hide.",
                "--state-dir",
                str(state_dir),
            )

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )

            marker_after = json.loads((output_dir / ".feedback-finalization-marker.json").read_text())
            self.assertNotEqual(marker_before.get("state_guard"), marker_after.get("state_guard"))

            source_mtime_after = (output_dir / "source-mentions.json").stat().st_mtime
            self.assertNotEqual(source_mtime_before, source_mtime_after)

    def test_profile_change_with_pending_feedback_falls_through_to_full_pipeline(self):
        """State guard mismatch + pending feedback → fall through (NOT in-place).

        Scenario:
        1. Run pipeline, finalize feedback → marker written.
        2. Add a hard rule matching a fixture candidate (state guard differs).
        3. Stage new pending feedback on a DIFFERENT candidate.
        4. run-mvp → must fall through to full pipeline (in-place would
           consume feedback but silently ignore the profile rule).
        5. Assert full pipeline ran: matching candidate in hidden-by-rule and
           absent from CSV, source-mentions refreshed, AND both rounds of
           feedback persist in history + archive.
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path, output_dir, state_dir = _build_60_candidate_scenario(base)

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text())
            new_candidate = next(c for c in candidates if c["update_delta"] == "new")

            # Finalize first round of feedback
            _run(
                "record-feedback",
                "--identity-key",
                new_candidate["identity_key"],
                "--status",
                "interested",
                "--note",
                "Round 1.",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )

            # Snapshot marker state guard before mutation
            marker_before = json.loads((output_dir / ".feedback-finalization-marker.json").read_text())
            source_mtime_before = (output_dir / "source-mentions.json").stat().st_mtime

            # Pick a fixture candidate to match with the hard rule.
            # Use a distinct candidate from the one we'll give feedback to.
            profile_target = next(
                c
                for c in candidates
                if c["update_delta"] == "new" and c["identity_key"] != new_candidate["identity_key"]
            )
            target_authors = profile_target.get("primary_authors", [])
            self.assertTrue(target_authors, "Fixture candidate must have authors")
            target_author = target_authors[0]  # e.g. "New Author 3"

            # Add a hard rule that MATCHES this specific fixture candidate.
            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "author",
                "--subject",
                target_author,
                "--quote",
                "Hide because test.",
                "--provenance",
                "test",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            # Stage NEW pending feedback (so pending is non-empty)
            updated_candidate = next(c for c in candidates if c["update_delta"] == "upgraded")
            _run(
                "record-feedback",
                "--identity-key",
                updated_candidate["identity_key"],
                "--status",
                "reject",
                "--note",
                "Round 2 — after profile change.",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            # Remove candidate_history from config so the pipeline uses the
            # authoritative state history (which has the full 60 candidates
            # including the new rows and their editor_status from round 1).
            config = json.loads(config_path.read_text())
            config.pop("candidate_history", None)
            _write_json(config_path, config)

            # run-mvp: should SKIP fast path (state guard mismatch) and run full pipeline
            result = _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            self.assertEqual(result.returncode, 0)

            # Full pipeline observable: source-mentions rewritten (mtime changed)
            source_mtime_after = (output_dir / "source-mentions.json").stat().st_mtime
            self.assertNotEqual(
                source_mtime_before,
                source_mtime_after,
                "Full pipeline must rewrite source artifacts after state change + pending feedback",
            )

            # Full pipeline observable: marker refreshed (state_guard differs)
            marker_after = json.loads((output_dir / ".feedback-finalization-marker.json").read_text())
            self.assertNotEqual(
                marker_before.get("state_guard"),
                marker_after.get("state_guard"),
                "State guard must differ after profile change",
            )

            # Observable profile effect: matching candidate in hidden-by-rule
            hidden_by_rule = json.loads((output_dir / "hidden-by-rule.json").read_text())
            hidden_ids = {str(r.get("identity_key", "")) for r in hidden_by_rule}
            self.assertIn(
                profile_target["identity_key"], hidden_ids, "Hard-rule-matched candidate must appear in hidden-by-rule"
            )

            # Observable profile effect: matching candidate absent from CSV
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as f:
                csv_rows = list(csv.DictReader(f))
            hidden_in_csv = any(r.get("Candidate ID") == profile_target["identity_key"] for r in csv_rows)
            self.assertFalse(hidden_in_csv, "Hard-rule-matched candidate must be absent from editor-facing.csv")

            # Both rounds of feedback consumed
            pending = read_state(state_dir, "pending_editor_rows")
            self.assertEqual(len(pending), 0, "All pending feedback must be consumed")

            # Round 1 feedback still present
            history = read_state(state_dir, "candidate_history")
            r1 = next((r for r in history if r["identity_key"] == new_candidate["identity_key"]), None)
            self.assertIsNotNone(r1)
            self.assertEqual(r1.get("editor_status", ""), "interested")

            # Round 2 feedback present
            r2 = next((r for r in history if r["identity_key"] == updated_candidate["identity_key"]), None)
            self.assertIsNotNone(r2)
            self.assertEqual(r2.get("editor_status", ""), "reject")

            # Archive has both rounds
            archive = read_state(state_dir, "archived_editor_rows")
            arch_ids = {r.get("identity_key", "") for r in archive}
            self.assertIn(new_candidate["identity_key"], arch_ids)
            self.assertIn(updated_candidate["identity_key"], arch_ids)

    def test_visibility_change_with_pending_feedback_falls_through_to_full_pipeline(self):
        """State guard mismatch (visibility) + pending feedback → full pipeline.

        Scenario:
        1. Run pipeline, finalize feedback → marker written.
        2. Hide a candidate (changes visibility state).
        3. Stage new pending feedback.
        4. run-mvp → must fall through (in-place would ignore the hide).
        5. Assert full pipeline ran AND hidden candidate is absent from artifacts.
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path, output_dir, state_dir = _build_60_candidate_scenario(base)

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text())
            new_candidate = next(c for c in candidates if c["update_delta"] == "new")

            # Finalize first round
            _run(
                "record-feedback",
                "--identity-key",
                new_candidate["identity_key"],
                "--status",
                "interested",
                "--note",
                "Round 1.",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )

            marker_before = json.loads((output_dir / ".feedback-finalization-marker.json").read_text())
            source_mtime_before = (output_dir / "source-mentions.json").stat().st_mtime

            # Change state: hide the candidate
            _run(
                "record-visibility",
                "--identity-key",
                new_candidate["identity_key"],
                "--action",
                "hide",
                "--reason",
                "Hidden for test.",
                "--state-dir",
                str(state_dir),
            )

            # Stage new pending feedback on a different candidate
            updated_candidate = next(c for c in candidates if c["update_delta"] == "upgraded")
            _run(
                "record-feedback",
                "--identity-key",
                updated_candidate["identity_key"],
                "--status",
                "maybe_watch",
                "--note",
                "Round 2 — after hide.",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            # Remove candidate_history from config so the pipeline uses the
            # authoritative state history (with full 60 candidates).
            config = json.loads(config_path.read_text())
            config.pop("candidate_history", None)
            _write_json(config_path, config)

            result = _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            self.assertEqual(result.returncode, 0)

            # Full pipeline observable
            source_mtime_after = (output_dir / "source-mentions.json").stat().st_mtime
            self.assertNotEqual(source_mtime_before, source_mtime_after)

            marker_after = json.loads((output_dir / ".feedback-finalization-marker.json").read_text())
            self.assertNotEqual(marker_before.get("state_guard"), marker_after.get("state_guard"))

            # Pending consumed
            pending = read_state(state_dir, "pending_editor_rows")
            self.assertEqual(len(pending), 0)

            # Round 2 feedback applied to updated candidate
            history = read_state(state_dir, "candidate_history")
            r2 = next((r for r in history if r["identity_key"] == updated_candidate["identity_key"]), None)
            self.assertIsNotNone(r2)
            self.assertEqual(r2.get("editor_status", ""), "maybe_watch")

            # Hidden candidate NOT in editor-facing CSV (visibility applied)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as f:
                csv_rows = list(csv.DictReader(f))
            hidden_in_csv = any(r.get("Candidate ID") == new_candidate["identity_key"] for r in csv_rows)
            self.assertFalse(hidden_in_csv, "Hidden candidate must not appear in editor-facing.csv")

            # Hidden candidate still has feedback in history
            r1 = next((r for r in history if r["identity_key"] == new_candidate["identity_key"]), None)
            self.assertIsNotNone(r1)
            self.assertEqual(r1.get("editor_status", ""), "interested")


class EditorCsvPreservationTests(unittest.TestCase):
    """In-place CSV update must preserve row set, ordering, and non-editor data."""

    def test_hidden_candidate_stays_hidden_in_csv_after_finalization(self):
        """When editor-facing.csv already has a candidate hidden,
        in-place finalization must not resurrect the hidden row."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path, output_dir, state_dir = _build_60_candidate_scenario(base)

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text())

            # Pick two candidates: one to hide, one to give feedback
            to_hide = next(c for c in candidates if c["update_delta"] == "new")
            to_feedback = next(
                c for c in candidates if c["update_delta"] == "new" and c["identity_key"] != to_hide["identity_key"]
            )

            # Hide the first candidate
            _run(
                "record-visibility",
                "--identity-key",
                to_hide["identity_key"],
                "--action",
                "hide",
                "--reason",
                "Test hide.",
                "--state-dir",
                str(state_dir),
            )

            # Re-run full pipeline so hide takes effect → editor-facing.csv excludes it
            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )

            # Verify hidden candidate is absent from CSV
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as f:
                csv_before = list(csv.DictReader(f))
            hidden_in_csv = any(r.get("Candidate ID") == to_hide["identity_key"] for r in csv_before)
            self.assertFalse(hidden_in_csv, "Hidden candidate should be absent from CSV before finalization")

            # Now stage feedback for the other candidate (decision-only)
            _run(
                "record-feedback",
                "--identity-key",
                to_feedback["identity_key"],
                "--status",
                "interested",
                "--note",
                "Feedback for visible candidate.",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            # In-place finalization (same run_date, same output_dir)
            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )

            # CSV must still exclude the hidden candidate
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as f:
                csv_after = list(csv.DictReader(f))
            hidden_after = any(r.get("Candidate ID") == to_hide["identity_key"] for r in csv_after)
            self.assertFalse(hidden_after, "Hidden candidate must NOT reappear in CSV after in-place finalization")

            # The visible candidate's feedback must be in the CSV
            feedback_row = next((r for r in csv_after if r.get("Candidate ID") == to_feedback["identity_key"]), None)
            self.assertIsNotNone(feedback_row, "Feedback candidate must be in CSV")
            self.assertEqual(feedback_row.get("Editor status", ""), "interested")
            self.assertEqual(feedback_row.get("Editor note", ""), "Feedback for visible candidate.")

            # Row count unchanged (no new rows added)
            self.assertEqual(len(csv_before), len(csv_after), "CSV row count must be preserved")

    def test_csv_preserves_non_editor_data_and_order_after_finalization(self):
        """In-place finalization must preserve:
        - Row ordering (unchanged)
        - All non-editor columns (Title, Author, Annotation, etc.)
        - Multiline Unicode editor notes
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path, output_dir, state_dir = _build_60_candidate_scenario(base)

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text())

            # Pick three candidates with different deltas
            new_c = next(c for c in candidates if c["update_delta"] == "new")
            upgraded_c = next(
                c for c in candidates if c["update_delta"] == "upgraded" and c["identity_key"] != new_c["identity_key"]
            )
            unchanged_c = next(
                c
                for c in candidates
                if c["update_delta"] == "unchanged"
                and c["identity_key"] not in {new_c["identity_key"], upgraded_c["identity_key"]}
            )

            # Snapshot CSV before feedback
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as f:
                csv_before_rows = list(csv.DictReader(f))
            csv_before_ids = [r.get("Candidate ID", "") for r in csv_before_rows]

            # Snapshot non-editor columns for each row
            NON_EDITOR_COLS = {
                "Title",
                "Author",
                "Annotation",
                "Description",
                "Signal count",
                "Visible-risk status",
                "Risk comment",
                "English title",
                "English author display",
                "English annotation",
                "Check date",
                "Source",
                "Source URL",
                "Publisher / year",
                "Category / topic",
                "Why it entered the list",
                "Signal summary",
                "Priority label",
                "Russian-edition status",
                "Russian-edition URL",
                "Links",
                "Update delta",
                "Lifecycle",
                "Review context",
                "Taste profile notes",
                "Candidate ID",
            }
            csv_before_non_editor: dict[str, dict[str, str]] = {}
            for row in csv_before_rows:
                cid = row.get("Candidate ID", "")
                csv_before_non_editor[cid] = {k: row.get(k, "") for k in NON_EDITOR_COLS}

            # Stage feedback with multiline Unicode notes
            note_new = 'Интересная книга — надо \n"проверить" права.\nВторая строка.'
            note_upgraded = "Больше не актуально."

            _run(
                "record-feedback",
                "--identity-key",
                new_c["identity_key"],
                "--status",
                "interested",
                "--note",
                note_new,
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            _run(
                "record-feedback",
                "--identity-key",
                upgraded_c["identity_key"],
                "--status",
                "reject",
                "--note",
                note_upgraded,
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            # In-place finalization
            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )

            # Read CSV after finalization
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as f:
                csv_after_rows = list(csv.DictReader(f))
            csv_after_ids = [r.get("Candidate ID", "") for r in csv_after_rows]

            # ── Row ordering preserved ──
            self.assertEqual(csv_before_ids, csv_after_ids, "CSV row ordering must be preserved")

            # ── Non-editor columns preserved for every row ──
            csv_after_by_id = {r.get("Candidate ID", ""): r for r in csv_after_rows}
            for cid, before_cols in csv_before_non_editor.items():
                after_row = csv_after_by_id.get(cid)
                self.assertIsNotNone(after_row, f"Row {cid} missing after finalization")
                for col, before_val in before_cols.items():
                    after_val = after_row.get(col, "")
                    self.assertEqual(
                        before_val,
                        after_val,
                        f"Non-editor column '{col}' changed for {cid}",
                    )

            # ── Editor decisions applied ──
            new_row = csv_after_by_id.get(new_c["identity_key"])
            self.assertIsNotNone(new_row)
            self.assertEqual(new_row.get("Editor status", ""), "interested")
            self.assertEqual(new_row.get("Editor note", ""), note_new)

            upgraded_row = csv_after_by_id.get(upgraded_c["identity_key"])
            self.assertIsNotNone(upgraded_row)
            self.assertEqual(upgraded_row.get("Editor status", ""), "reject")
            self.assertEqual(upgraded_row.get("Editor note", ""), note_upgraded)

            # ── Unchanged candidate still has empty editor columns ──
            unchanged_row = csv_after_by_id.get(unchanged_c["identity_key"])
            self.assertIsNotNone(unchanged_row)
            self.assertEqual(unchanged_row.get("Editor status", ""), "")


class EditorCsvMalformedSkipsTests(unittest.TestCase):
    """Malformed editor-facing.csv must cause attempt_finalization to return
    ``skipped`` with no state/output mutation — candidate history, latest
    candidates, pending feedback, archive, scouting-candidates.json, output
    candidate-history, the CSV, and the marker are all byte-identical
    after a direct call."""

    # ── helpers ──

    @staticmethod
    def _snapshot_files(base: dict[str, Path]) -> dict[str, bytes | None]:
        """Return {key: file_bytes} for each path, or None when absent."""
        result: dict[str, bytes | None] = {}
        for key, p in base.items():
            if p.is_file():
                # Read marker as bytes too (it is JSON, but byte equality is stricter)
                result[key] = p.read_bytes()
            else:
                result[key] = None
        return result

    def _assert_unchanged(self, before: dict[str, bytes | None], after: dict[str, bytes | None], label: str) -> None:
        for key in before:
            self.assertEqual(
                before[key],
                after.get(key),
                f"{key} must be unchanged after skipped fast path ({label})",
            )
        # No new files appeared.
        self.assertEqual(set(before.keys()), set(after.keys()), f"No file set change ({label})")

    # ── actual regression tests ──

    def test_missing_required_header_causes_skip_and_byte_identical_state(self):
        """Corrupt the CSV (remove Editor status header), then call
        attempt_finalization directly.  Assert result is ``skipped`` and
        every tracked file is byte-identical to its pre-call snapshot."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path, output_dir, state_dir = _build_60_candidate_scenario(base)
            run_date = json.loads(config_path.read_text())["run_date"]

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text())
            new_candidate = next(c for c in candidates if c["update_delta"] == "new")

            # Stage decision-only pending feedback
            _run(
                "record-feedback",
                "--identity-key",
                new_candidate["identity_key"],
                "--status",
                "interested",
                "--note",
                "Test malformed CSV.",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            # Corrupt the CSV: remove "Editor status" header
            csv_lines = (output_dir / "editor-facing.csv").read_text(encoding="utf-8").splitlines()
            new_csv_lines = [line.replace(",Editor status,", ",") for line in csv_lines]
            (output_dir / "editor-facing.csv").write_text("\n".join(new_csv_lines), encoding="utf-8")

            # ── Snapshot everything ──
            tracked = {
                "scouting": output_dir / "scouting-candidates.json",
                "csv": output_dir / "editor-facing.csv",
                "marker": output_dir / ".feedback-finalization-marker.json",
                "history": state_dir / "candidate-history.json",
                "latest": state_dir / "latest-scouting-candidates.json",
                "pending": state_dir / "pending-returned-editor-rows.json",
                "archive": state_dir / "archived-returned-editor-rows.json",
                "out_history": output_dir / "candidate-history.json",
            }
            before = self._snapshot_files(tracked)

            # ── Direct call — no full-pipeline fallthrough ──
            from autoscout.feedback_finalization import attempt_finalization

            result = attempt_finalization(output_dir, state_dir, run_date)
            self.assertEqual(result, "skipped", "Must return 'skipped' — not 'applied' or 'noop'")

            after = self._snapshot_files(tracked)
            self._assert_unchanged(before, after, "missing header")

    def test_ragged_extra_cells_cause_skip_and_byte_identical_state(self):
        """Corrupt the CSV (ragged extra cell → None key), then call
        attempt_finalization directly.  Assert result is ``skipped`` and
        every tracked file is byte-identical to its pre-call snapshot."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path, output_dir, state_dir = _build_60_candidate_scenario(base)
            run_date = json.loads(config_path.read_text())["run_date"]

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text())
            new_candidate = next(c for c in candidates if c["update_delta"] == "new")

            _run(
                "record-feedback",
                "--identity-key",
                new_candidate["identity_key"],
                "--status",
                "interested",
                "--note",
                "Test ragged CSV.",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            # Corrupt the CSV: add an extra comma to a data row → ragged extra cell
            csv_text = (output_dir / "editor-facing.csv").read_text(encoding="utf-8")
            lines = csv_text.splitlines()
            if len(lines) > 1:
                lines[1] = lines[1] + ",extra,fields"
            (output_dir / "editor-facing.csv").write_text("\n".join(lines), encoding="utf-8")

            tracked = {
                "scouting": output_dir / "scouting-candidates.json",
                "csv": output_dir / "editor-facing.csv",
                "marker": output_dir / ".feedback-finalization-marker.json",
                "history": state_dir / "candidate-history.json",
                "latest": state_dir / "latest-scouting-candidates.json",
                "pending": state_dir / "pending-returned-editor-rows.json",
                "archive": state_dir / "archived-returned-editor-rows.json",
                "out_history": output_dir / "candidate-history.json",
            }
            before = self._snapshot_files(tracked)

            from autoscout.feedback_finalization import attempt_finalization

            result = attempt_finalization(output_dir, state_dir, run_date)
            self.assertEqual(result, "skipped")

            after = self._snapshot_files(tracked)
            self._assert_unchanged(before, after, "ragged cells")


class PipelineBoundaryTests(unittest.TestCase):
    """run-mvp must not call render-review internally."""

    def test_run_mvp_does_not_render_html_on_finalization(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path, output_dir, state_dir = _build_60_candidate_scenario(base)

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            html_path = output_dir / "weekly-review.html"
            if html_path.exists():
                html_path.unlink()

            candidates = json.loads((output_dir / "scouting-candidates.json").read_text())
            new_candidate = next(c for c in candidates if c["update_delta"] == "new")

            _run(
                "record-feedback",
                "--identity-key",
                new_candidate["identity_key"],
                "--status",
                "interested",
                "--note",
                "Boundary test.",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            self.assertFalse(html_path.exists(), "run-mvp must not produce weekly-review.html")

            _run("render-review", "--output-dir", str(output_dir), "--state-dir", str(state_dir))
            self.assertTrue(html_path.exists(), "render-review must produce weekly-review.html")


class RunDateMismatchTests(unittest.TestCase):
    """Different run dates must fall through to full pipeline."""

    def test_different_run_date_triggers_full_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path, output_dir, state_dir = _build_60_candidate_scenario(base)

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )

            config2_path = base / "config2.json"
            config2 = json.loads(config_path.read_text())
            config2["run_date"] = "2026-07-16"
            _write_json(config2_path, config2)

            output_dir2 = base / "output2"
            output_dir2.mkdir()
            result = _run(
                "run-mvp",
                "--config",
                str(config2_path),
                "--output-dir",
                str(output_dir2),
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(result.returncode, 0)
            self.assertTrue((output_dir2 / "source-mentions.json").exists())

            mentions = json.loads((output_dir2 / "source-mentions.json").read_text())
            dates = {str(m.get("source_check_date", "")) for m in mentions}
            self.assertIn("2026-07-16", dates)


class FreshOutputDirFullPipelineTests(unittest.TestCase):
    def test_fresh_output_dir_runs_full_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path, output_dir, state_dir = _build_60_candidate_scenario(base)
            self.assertFalse((output_dir / "scouting-candidates.json").exists())

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            self.assertTrue((output_dir / "source-mentions.json").exists())
            self.assertTrue((output_dir / "scouting-candidates.json").exists())


class FactualCorrectionFallthroughTests(unittest.TestCase):
    def test_pending_with_title_column_falls_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path, output_dir, state_dir = _build_60_candidate_scenario(base)

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text())
            new_candidate = next(c for c in candidates if c["update_delta"] == "new")

            pending = [
                {"identity_key": new_candidate["identity_key"], "Editor Status": "reject", "Title": "Corrected Title"}
            ]
            seed_state(state_dir, pending_editor_rows=pending)

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            candidates2 = json.loads((output_dir / "scouting-candidates.json").read_text())
            self.assertIsNotNone(
                next((c for c in candidates2 if c["identity_key"] == new_candidate["identity_key"]), None)
            )


class CandidateHistoryUpdateTests(unittest.TestCase):
    def test_history_and_latest_scouting_updated(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path, output_dir, state_dir = _build_60_candidate_scenario(base)

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text())
            new_candidate = next(c for c in candidates if c["update_delta"] == "new")
            ik = new_candidate["identity_key"]

            note = "Заметка редактора."
            _run(
                "record-feedback",
                "--identity-key",
                ik,
                "--status",
                "interested",
                "--note",
                note,
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )

            history = read_state(state_dir, "candidate_history")
            record = next((r for r in history if r["identity_key"] == ik), None)
            self.assertIsNotNone(record)
            self.assertEqual(record.get("editor_status", ""), "interested")
            self.assertEqual(record.get("editor_note", ""), note)

            latest = read_state(state_dir, "latest_scouting_candidates")
            self.assertTrue(latest)
            latest_record = next((r for r in latest if r["identity_key"] == ik), None)
            self.assertIsNotNone(latest_record)
            self.assertEqual(latest_record.get("editor_status", ""), "interested")
            self.assertEqual(latest_record.get("editor_note", ""), note)

    def test_history_order_preserved(self):
        """Write-back must not reorder state history or latest-candidates."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path, output_dir, state_dir = _build_60_candidate_scenario(base)

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text())
            new_candidate = next(c for c in candidates if c["update_delta"] == "new")

            # Snapshot original order
            hist_before = read_state(state_dir, "candidate_history")
            hist_order_before = [r["identity_key"] for r in hist_before]

            latest_before = read_state(state_dir, "latest_scouting_candidates")
            latest_order_before = [r["identity_key"] for r in latest_before]

            _run(
                "record-feedback",
                "--identity-key",
                new_candidate["identity_key"],
                "--status",
                "interested",
                "--note",
                "Order test.",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )

            hist_after = read_state(state_dir, "candidate_history")
            self.assertEqual(
                hist_order_before, [r["identity_key"] for r in hist_after], "History reordered by finalization"
            )

            latest_after = read_state(state_dir, "latest_scouting_candidates")
            self.assertEqual(
                latest_order_before,
                [r["identity_key"] for r in latest_after],
                "Latest-candidates reordered by finalization",
            )


class NewStaysNewInRenderTests(unittest.TestCase):
    def test_new_lifecycle_in_csv_and_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path, output_dir, state_dir = _build_60_candidate_scenario(base)

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text())
            new_candidate = next(c for c in candidates if c["update_delta"] == "new")

            note = "New stays new."
            _run(
                "record-feedback",
                "--identity-key",
                new_candidate["identity_key"],
                "--status",
                "interested",
                "--note",
                note,
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            _run("render-review", "--output-dir", str(output_dir), "--state-dir", str(state_dir))

            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as f:
                csv_rows = list(csv.DictReader(f))
            csv_row = next((r for r in csv_rows if r.get("Candidate ID") == new_candidate["identity_key"]), None)
            self.assertEqual(csv_row.get("Lifecycle", ""), "New")
            self.assertEqual(csv_row.get("Update delta", ""), "new")

            html = (output_dir / "weekly-review.html").read_text(encoding="utf-8")
            escaped_title = re.escape(new_candidate["title"].lower())
            block = re.search(rf'<details[^>]*\bdata-title="{escaped_title}"[^>]*>.*?</details>', html, re.DOTALL)
            self.assertIsNotNone(block, "Candidate HTML block not found")
            # Product rule changed by #1057: no lifecycle status on the card; a new book carries no updated mark.
            self.assertNotIn('class="upd"', block.group(0))
            self.assertIn(note, block.group(0))


class RenderAfterFinalizationTests(unittest.TestCase):
    def test_explicit_render_after_finalization(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path, output_dir, state_dir = _build_60_candidate_scenario(base)

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text())
            new_candidate = next(c for c in candidates if c["update_delta"] == "new")

            note = "Рендер после финализации."
            _run(
                "record-feedback",
                "--identity-key",
                new_candidate["identity_key"],
                "--status",
                "interested",
                "--note",
                note,
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            render_result = _run("render-review", "--output-dir", str(output_dir), "--state-dir", str(state_dir))
            self.assertEqual(render_result.returncode, 0)

            html = (output_dir / "weekly-review.html").read_text(encoding="utf-8")
            self.assertIn(note, html)
            self.assertIn("Интересно", html)

    def test_markdown_review_mentions_8_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path, output_dir, state_dir = _build_60_candidate_scenario(base)

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text())
            for i, c in enumerate(candidates):
                if c["update_delta"] == "new" and i < 3:
                    _run(
                        "record-feedback",
                        "--identity-key",
                        c["identity_key"],
                        "--status",
                        "interested",
                        "--note",
                        f"Test {i}.",
                        "--state-dir",
                        str(state_dir),
                        "--confirmed-by-editor",
                    )

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            _run("render-review", "--output-dir", str(output_dir), "--state-dir", str(state_dir))

            md_review = (output_dir / "weekly-review.md").read_text(encoding="utf-8")
            self.assertIn("новых — 8", md_review)


class ArchiveDeterministicTests(unittest.TestCase):
    def test_archive_sorted_by_identity_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config_path, output_dir, state_dir = _build_60_candidate_scenario(base)

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text())
            new_a = next(c for c in candidates if c["update_delta"] == "new")
            new_b = next(
                c for c in candidates if c["update_delta"] == "new" and c["identity_key"] != new_a["identity_key"]
            )

            for ik in (new_a["identity_key"], new_b["identity_key"]):
                _run(
                    "record-feedback",
                    "--identity-key",
                    ik,
                    "--status",
                    "interested",
                    "--note",
                    "Test.",
                    "--state-dir",
                    str(state_dir),
                    "--confirmed-by-editor",
                )

            _run(
                "run-mvp", "--config", str(config_path), "--output-dir", str(output_dir), "--state-dir", str(state_dir)
            )

            archive = read_state(state_dir, "archived_editor_rows")
            ids = [r.get("identity_key", "") for r in archive]
            self.assertEqual(ids, sorted(ids))


if __name__ == "__main__":
    unittest.main()
