"""Issue #678: structured feedback dimensions.

Covers:
- The dimension vocabulary module: bounded choices, defaults, validation.
- record-feedback preview shows the exact structured dimensions (code 3, zero mutation).
- Confirmed record-feedback records the dimensions atomically with the verbatim note.
- Bare refusals («Нет.») stay explicitly unspecified and refuse derived dimensions.
- Feedback finalization applies dimensions to candidates/history decision-only.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from state_store_support import read_state
from state_store_support import seed_state

from autoscout.editorial_audit import BARE_REFUSAL
from autoscout.feedback_dimensions import DIMENSION_CHOICES
from autoscout.feedback_dimensions import FeedbackDimensionError
from autoscout.feedback_dimensions import has_specified_dimensions
from autoscout.feedback_dimensions import validate_note_dimensions
from tests.issue_678_helpers import make_dims as _dims


def _write_candidate_state(state_dir: Path, identity_key: str, title: str = "Test Book") -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    existing = read_state(state_dir, "candidate_history")
    existing.append(
        {
            "identity_key": identity_key,
            "title": title,
            "primary_authors": ["Test Author"],
            "last_seen_date": "2026-09-11",
        }
    )
    seed_state(state_dir, candidate_history=existing)
    if not read_state(state_dir, "latest_scouting_candidates"):
        seed_state(state_dir, latest_scouting_candidates=existing)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "autoscout", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _pending_feedback(state_dir: Path) -> list[dict]:
    return read_state(state_dir, "pending_editor_rows")


# ---------------------------------------------------------------------------
# Dimension vocabulary module
# ---------------------------------------------------------------------------


class DimensionVocabularyTests(unittest.TestCase):
    def test_all_five_audit_dimensions_have_bounded_choices(self) -> None:
        # The four recordable dimensions; the fifth (final editorial
        # disposition) is the existing editor status, presented as a
        # dimension in the preview but stored as Editor Status.
        self.assertEqual(
            set(DIMENSION_CHOICES),
            {"editorial_appeal", "rights_availability", "quality_problem", "information_needed"},
        )
        self.assertEqual(DIMENSION_CHOICES["editorial_appeal"], ("appealing", "not_appealing", "unspecified"))
        self.assertEqual(
            DIMENSION_CHOICES["rights_availability"],
            ("available", "unavailable", "unknown", "unspecified"),
        )
        self.assertEqual(
            DIMENSION_CHOICES["quality_problem"],
            ("none", "description_missing", "wrong_category", "bad_links", "unusable_title", "other", "unspecified"),
        )
        self.assertEqual(
            DIMENSION_CHOICES["information_needed"],
            ("none", "purchase_status", "rights_status", "russian_edition", "description", "other", "unspecified"),
        )

    def test_blank_values_default_to_unspecified(self) -> None:
        dims = _dims()
        self.assertEqual(dims, {name: "unspecified" for name in DIMENSION_CHOICES})
        self.assertFalse(has_specified_dimensions(dims))

    def test_explicit_values_are_canonicalized(self) -> None:
        dims = _dims(
            editorial_appeal="appealing",
            rights_availability="unavailable",
            quality_problem="description_missing",
            information_needed="purchase_status",
        )
        self.assertEqual(
            dims,
            {
                "editorial_appeal": "appealing",
                "rights_availability": "unavailable",
                "quality_problem": "description_missing",
                "information_needed": "purchase_status",
            },
        )
        self.assertTrue(has_specified_dimensions(dims))

    def test_unknown_value_raises(self) -> None:
        with self.assertRaises(FeedbackDimensionError):
            _dims(editorial_appeal="bogus")

    def test_bare_refusal_must_stay_unspecified(self) -> None:
        self.assertEqual(BARE_REFUSAL, "Нет.")
        dims = _dims(editorial_appeal="appealing")
        with self.assertRaises(FeedbackDimensionError):
            validate_note_dimensions(BARE_REFUSAL, dims)
        # A bare refusal with all-unspecified dimensions is fine.
        validate_note_dimensions(BARE_REFUSAL, _dims())
        # A substantive note may carry dimensions.
        validate_note_dimensions("Нравится, но права не продают.", dims)

    def test_bare_refusal_whitespace_variants_are_still_bare(self) -> None:
        dims = _dims(rights_availability="available")
        with self.assertRaises(FeedbackDimensionError):
            validate_note_dimensions("  Нет.  ", dims)


# ---------------------------------------------------------------------------
# record-feedback CLI: preview and confirm
# ---------------------------------------------------------------------------


class RecordFeedbackDimensionsPreviewTests(unittest.TestCase):
    def test_preview_shows_all_dimension_lines_and_disposition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            identity_key = "dimension book|author"
            _write_candidate_state(state_dir, identity_key)

            result = _run(
                "record-feedback",
                "--identity-key",
                identity_key,
                "--status",
                "reject",
                "--note",
                "Слишком приторная подача.",
                "--appeal",
                "appealing",
                "--quality-problem",
                "other",
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            self.assertIn("confirmation_required=true", result.stdout)
            self.assertIn("dimension_editorial_appeal=appealing", result.stdout)
            self.assertIn("dimension_rights_availability=unspecified", result.stdout)
            self.assertIn("dimension_quality_problem=other", result.stdout)
            self.assertIn("dimension_information_needed=unspecified", result.stdout)
            self.assertIn("final_disposition=reject", result.stdout)
            self.assertIn("editor_note=Слишком приторная подача.", result.stdout)
            # Zero mutation.
            self.assertEqual(_pending_feedback(state_dir), [])

    def test_preview_without_flags_shows_unspecified_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            identity_key = "plain book|author"
            _write_candidate_state(state_dir, identity_key)
            result = _run(
                "record-feedback",
                "--identity-key",
                identity_key,
                "--status",
                "interested",
                "--note",
                "Interesting.",
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(result.returncode, 3)
            for name in DIMENSION_CHOICES:
                self.assertIn(f"dimension_{name}=unspecified", result.stdout)
            self.assertIn("final_disposition=interested", result.stdout)

    def test_invalid_dimension_value_fails_validation_not_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            identity_key = "invalid dim|author"
            _write_candidate_state(state_dir, identity_key)
            result = _run(
                "record-feedback",
                "--identity-key",
                identity_key,
                "--status",
                "reject",
                "--note",
                "Note.",
                "--appeal",
                "bogus",
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("Unsupported", result.stdout)
            self.assertEqual(_pending_feedback(state_dir), [])

    def test_bare_refusal_with_dimensions_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            identity_key = "bare refusal|author"
            _write_candidate_state(state_dir, identity_key)
            result = _run(
                "record-feedback",
                "--identity-key",
                identity_key,
                "--status",
                "reject",
                "--note",
                BARE_REFUSAL,
                "--appeal",
                "not_appealing",
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("bare_refusal_must_stay_unspecified", result.stdout)
            self.assertEqual(_pending_feedback(state_dir), [])

    def test_bare_refusal_without_dimensions_records_unspecified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            identity_key = "bare refusal plain|author"
            _write_candidate_state(state_dir, identity_key)
            result = _run(
                "record-feedback",
                "--identity-key",
                identity_key,
                "--status",
                "reject",
                "--note",
                BARE_REFUSAL,
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            pending = _pending_feedback(state_dir)
            self.assertEqual(len(pending), 1)
            self.assertNotIn("Feedback Dimensions", pending[0])


class RecordFeedbackDimensionsConfirmTests(unittest.TestCase):
    def test_confirmed_records_dimensions_atomically_with_verbatim_note(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            identity_key = "confirmed dims|author"
            _write_candidate_state(state_dir, identity_key)
            note = "Нравится, но, похоже, мы её уже купили. Я проверю."
            result = _run(
                "record-feedback",
                "--identity-key",
                identity_key,
                "--status",
                "needs_deeper_analysis",
                "--note",
                note,
                "--appeal",
                "appealing",
                "--rights",
                "unknown",
                "--info-needed",
                "purchase_status",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            pending = _pending_feedback(state_dir)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["identity_key"], identity_key)
            self.assertEqual(pending[0]["Editor Status"], "needs_deeper_analysis")
            self.assertEqual(pending[0]["Editor Note"], note)
            self.assertEqual(
                pending[0]["Feedback Dimensions"],
                {
                    "editorial_appeal": "appealing",
                    "rights_availability": "unknown",
                    "quality_problem": "unspecified",
                    "information_needed": "purchase_status",
                },
            )

    def test_confirmed_without_dimensions_writes_no_dimension_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            identity_key = "no dims|author"
            _write_candidate_state(state_dir, identity_key)
            result = _run(
                "record-feedback",
                "--identity-key",
                identity_key,
                "--status",
                "reject",
                "--note",
                "Plain rejection.",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            pending = _pending_feedback(state_dir)
            self.assertEqual(len(pending), 1)
            self.assertNotIn("Feedback Dimensions", pending[0])

    def test_bare_refusal_confirmed_without_dimensions_produces_no_dimension_key(self) -> None:
        """Bare refusals stay explicitly unspecified: no dimension dict, no derived taste rule."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            identity_key = "bare refusal confirmed|author"
            _write_candidate_state(state_dir, identity_key)
            result = _run(
                "record-feedback",
                "--identity-key",
                identity_key,
                "--status",
                "reject",
                "--note",
                BARE_REFUSAL,
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            pending = _pending_feedback(state_dir)
            self.assertEqual(pending[0]["Editor Note"], BARE_REFUSAL)
            self.assertNotIn("Feedback Dimensions", pending[0])


# ---------------------------------------------------------------------------
# Feedback finalization carries dimensions decision-only
# ---------------------------------------------------------------------------


class FinalizationDimensionTests(unittest.TestCase):
    def _finalize(self, state_dir: Path, output_dir: Path) -> str:
        from autoscout.feedback_finalization import attempt_finalization

        return attempt_finalization(output_dir, state_dir, "2026-09-11")

    def _seed_report(self, output_dir: Path, identity_key: str) -> None:
        from autoscout.weekly_report import WeeklyReportStore
        from autoscout.weekly_report_row import EDITOR_FIELDNAMES

        output_dir.mkdir(parents=True, exist_ok=True)
        store = WeeklyReportStore(output_dir)
        candidates = [
            {
                "identity_key": identity_key,
                "title": "Dimension Book",
                "primary_authors": ["Test Author"],
            }
        ]
        store.write_json("scouting-candidates.json", candidates)
        store.write_json("source-mentions.json", [])
        csv_row = dict.fromkeys(EDITOR_FIELDNAMES, "")
        csv_row.update(
            {
                "Title": "Dimension Book",
                "Candidate ID": identity_key,
                "Editor status": "",
                "Editor note": "",
            }
        )
        store.write_editor_csv([csv_row])
        store.write_json(
            "run-summary.json",
            {"run_date": "2026-09-11", "editor_facing_count": 1, "raw_candidate_count": 1},
        )

    def test_finalization_applies_dimensions_to_candidates_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            output_dir = Path(tmp) / "output"
            identity_key = "finalized dims|author"
            _write_candidate_state(state_dir, identity_key)
            self._seed_report(output_dir, identity_key)
            dims = {
                "editorial_appeal": "appealing",
                "rights_availability": "unavailable",
                "quality_problem": "unspecified",
                "information_needed": "unspecified",
            }
            seed_state(
                state_dir,
                pending_editor_rows=[
                    {
                        "identity_key": identity_key,
                        "Editor Status": "already_known",
                        "Editor Note": "Идеальный кандидат, но права не продают.",
                        "Feedback Dimensions": dims,
                    }
                ],
            )
            result = self._finalize(state_dir, output_dir)
            self.assertEqual(result, "applied")

            history = read_state(state_dir, "candidate_history")
            record = next(r for r in history if r["identity_key"] == identity_key)
            self.assertEqual(record["editor_status"], "already_known")
            self.assertEqual(record["feedback_dimensions"], dims)

            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            candidate = next(c for c in candidates if c["identity_key"] == identity_key)
            self.assertEqual(candidate["feedback_dimensions"], dims)

            # Pending cleared; dimensions archived with the row.
            self.assertEqual(_pending_feedback(state_dir), [])
            archive = read_state(state_dir, "archived_editor_rows")
            self.assertEqual(archive[0]["Feedback Dimensions"], dims)

    def test_finalization_still_applies_when_only_dimensions_present(self) -> None:
        """A dimensions-only row (no status change) is still a decision and finalizes."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            output_dir = Path(tmp) / "output"
            identity_key = "dims only|author"
            _write_candidate_state(state_dir, identity_key)
            self._seed_report(output_dir, identity_key)
            seed_state(
                state_dir,
                pending_editor_rows=[
                    {
                        "identity_key": identity_key,
                        "Feedback Dimensions": {
                            "editorial_appeal": "unspecified",
                            "rights_availability": "unknown",
                            "quality_problem": "unspecified",
                            "information_needed": "rights_status",
                        },
                    }
                ],
            )
            result = self._finalize(state_dir, output_dir)
            self.assertEqual(result, "applied")
            history = read_state(state_dir, "candidate_history")
            record = next(r for r in history if r["identity_key"] == identity_key)
            self.assertEqual(record["feedback_dimensions"]["information_needed"], "rights_status")

    def test_factual_row_with_dimensions_still_skips_finalization(self) -> None:
        """Dimensions do not launder a factual-correction row into decision-only finalization."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            output_dir = Path(tmp) / "output"
            identity_key = "factual dims|author"
            _write_candidate_state(state_dir, identity_key)
            self._seed_report(output_dir, identity_key)
            seed_state(
                state_dir,
                pending_editor_rows=[
                    {
                        "identity_key": identity_key,
                        "Title": "Corrected Title",
                        "Feedback Dimensions": {
                            "editorial_appeal": "unspecified",
                            "rights_availability": "unspecified",
                            "quality_problem": "unspecified",
                            "information_needed": "unspecified",
                        },
                    }
                ],
            )
            result = self._finalize(state_dir, output_dir)
            self.assertEqual(result, "skipped")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Full-pipeline feedback consumption carries dimensions too (Issue #678)
# ---------------------------------------------------------------------------


class FullPipelineDimensionTests(unittest.TestCase):
    """When safe finalization is skipped, the full pipeline consumes pending
    rows via apply_editor_feedback. Dimensions must survive that path too."""

    def test_apply_editor_feedback_applies_dimensions_to_history_and_candidate(self) -> None:
        from autoscout.pipeline import apply_editor_feedback

        identity_key = "pipeline dims|author"
        candidates = [{"identity_key": identity_key, "title": "Pipeline Book", "primary_authors": ["Test Author"]}]
        history = [{"identity_key": identity_key, "title": "Pipeline Book", "primary_authors": ["Test Author"]}]
        dims = {
            "editorial_appeal": "appealing",
            "rights_availability": "unknown",
            "quality_problem": "unspecified",
            "information_needed": "purchase_status",
        }
        rows = [
            {
                "identity_key": identity_key,
                "Editor Status": "needs_deeper_analysis",
                "Editor Note": "Проверю покупку.",
                "Feedback Dimensions": dims,
            }
        ]
        new_history, _corrections, _queue = apply_editor_feedback(candidates, history, rows, "2026-09-11")
        record = next(r for r in new_history if r["identity_key"] == identity_key)
        self.assertEqual(record["feedback_dimensions"], dims)

    def test_history_snapshot_projection_keeps_dimensions_authoritative(self) -> None:
        from autoscout.pipeline import update_candidate_history_snapshots

        identity_key = "snapshot dims|author"
        dims = {
            "editorial_appeal": "unspecified",
            "rights_availability": "unavailable",
            "quality_problem": "unspecified",
            "information_needed": "unspecified",
        }
        candidates = [
            {
                "identity_key": identity_key,
                "title": "Snapshot Book",
                "primary_authors": ["Test Author"],
                "editor_status": "already_known",
                "editor_note": "note",
                "feedback_dimensions": dims,
            }
        ]
        history = [{"identity_key": identity_key, "title": "Snapshot Book", "primary_authors": ["Test Author"]}]
        new_history = update_candidate_history_snapshots(history, candidates, "2026-09-11")
        record = next(r for r in new_history if r["identity_key"] == identity_key)
        self.assertEqual(record["feedback_dimensions"], dims)


# ---------------------------------------------------------------------------
# Bare-refusal breadth and exact-value validation (review fixes)
# ---------------------------------------------------------------------------


class BareRefusalBreadthTests(unittest.TestCase):
    def test_short_bare_refusal_forms_are_all_recognized(self) -> None:
        from autoscout.feedback_dimensions import BARE_REFUSALS
        from autoscout.feedback_dimensions import is_bare_refusal

        for form in ("Нет.", "Нет", "нет.", "нет", "No.", "No", "no.", "no"):
            self.assertIn(form, BARE_REFUSALS)
            self.assertTrue(is_bare_refusal(form))
            self.assertTrue(is_bare_refusal(f"  {form}  "))

    def test_substantive_notes_starting_with_a_refusal_word_are_not_bare(self) -> None:
        from autoscout.feedback_dimensions import is_bare_refusal

        for note in ("Нет, но описание поправить.", "Нет прав.", "Not now."):
            self.assertFalse(is_bare_refusal(note), note)

    def test_each_short_bare_refusal_form_refuses_dimensions_via_cli(self) -> None:
        for form in ("Нет", "нет.", "No."):
            with tempfile.TemporaryDirectory() as tmp:
                state_dir = Path(tmp) / "state"
                identity_key = f"bare {form}|author"
                _write_candidate_state(state_dir, identity_key)
                result = _run(
                    "record-feedback",
                    "--identity-key",
                    identity_key,
                    "--status",
                    "reject",
                    "--note",
                    form,
                    "--appeal",
                    "appealing",
                    "--state-dir",
                    str(state_dir),
                )
                self.assertEqual(result.returncode, 1, form)
                self.assertIn("bare_refusal_must_stay_unspecified", result.stdout)
                self.assertEqual(_pending_feedback(state_dir), [])

    def test_substantive_refusal_note_allows_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            identity_key = "substantive refusal|author"
            _write_candidate_state(state_dir, identity_key)
            result = _run(
                "record-feedback",
                "--identity-key",
                identity_key,
                "--status",
                "reject",
                "--note",
                "Нет, но описание поправить.",
                "--quality-problem",
                "description_missing",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            pending = _pending_feedback(state_dir)
            self.assertEqual(pending[0]["Feedback Dimensions"]["quality_problem"], "description_missing")


class ExactValueValidationTests(unittest.TestCase):
    def test_values_are_accepted_exactly_with_no_normalization(self) -> None:
        # Issue #678 review fix: no casefold/separator normalization —
        # values must match the declared vocabulary exactly.
        with self.assertRaises(FeedbackDimensionError):
            _dims(editorial_appeal="Not-Appealing")
        with self.assertRaises(FeedbackDimensionError):
            _dims(quality_problem="Description Missing")
        with self.assertRaises(FeedbackDimensionError):
            _dims(rights_availability="UNAVAILABLE")

    def test_declared_values_still_accepted(self) -> None:
        dims = _dims(
            editorial_appeal="not_appealing",
            rights_availability="unavailable",
            quality_problem="description_missing",
            information_needed="purchase_status",
        )
        self.assertEqual(dims["editorial_appeal"], "not_appealing")
        self.assertEqual(dims["rights_availability"], "unavailable")


class DimensionHelperTests(unittest.TestCase):
    def test_dimensions_from_row_and_record_read_their_own_shapes(self) -> None:
        from autoscout.feedback_dimensions import dimensions_from_record
        from autoscout.feedback_dimensions import dimensions_from_row

        row = {"identity_key": "k", "Feedback Dimensions": {"editorial_appeal": "appealing"}}
        record = {"identity_key": "k", "feedback_dimensions": {"editorial_appeal": "appealing"}}
        self.assertEqual(dimensions_from_row(row), {"editorial_appeal": "appealing"})
        self.assertIsNone(dimensions_from_row(record))
        self.assertEqual(dimensions_from_record(record), {"editorial_appeal": "appealing"})
        self.assertIsNone(dimensions_from_record(row))
        # Defensive copies: mutating the result must not touch the source.
        dims = dimensions_from_row(row)
        assert dims is not None
        dims["editorial_appeal"] = "not_appealing"
        self.assertEqual(row["Feedback Dimensions"]["editorial_appeal"], "appealing")
