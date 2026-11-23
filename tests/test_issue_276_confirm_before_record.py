"""Acceptance tests for issue #276: confirm-before-record for conversational
candidate feedback and Editorial Taste Profile entries.

Covers:
- record-feedback preview (code 3, zero mutation)
- record-feedback confirmed (code 0, stages feedback)
- editorial-profile add preview (code 3, zero mutation, hard/soft effects distinguished)
- editorial-profile add confirmed (code 0, writes entry)
- Deep House scenario: neither rejection nor Hard Rule before confirmation
- Contract/docs tests — Russian confirmation question, exact identity, no Git
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_databases

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_candidate_state(
    state_dir: Path,
    identity_key: str,
    title: str = "Test Book",
    authors: list[str] | None = None,
    english_title: str = "",
) -> None:
    """Seed candidate history with a minimal known identity."""
    authors = authors or ["Test Author"]
    state_dir.mkdir(parents=True, exist_ok=True)
    existing = read_state(state_dir, "candidate_history")
    existing.append(
        {
            "identity_key": identity_key,
            "title": title,
            "primary_authors": authors,
            "english_title": english_title,
            "last_seen_date": "2026-07-14",
        }
    )
    seed_state(state_dir, candidate_history=existing)
    # Also seed latest-scouting-candidates for the Deep House scenario
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


def _profile_entries(state_dir: Path) -> list[dict]:
    from autoscout.editorial_taste_profile import load_taste_profile

    return load_taste_profile(state_dir).get("entries", [])


def _visibility(state_dir: Path) -> list[dict]:
    return read_state(state_dir, "candidate_visibility")


# ---------------------------------------------------------------------------
# Acceptance Test 1: record-feedback preview
# ---------------------------------------------------------------------------


class RecordFeedbackPreviewTests(unittest.TestCase):
    def test_preview_without_confirmation_returns_code_3_and_no_state_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            identity_key = "deep house|e.n. anonymous"
            _write_candidate_state(
                state_dir,
                identity_key,
                title="Deep House",
                authors=["E.N. Anonymous"],
                english_title="Deep House",
            )

            # Ensure state files start from a known baseline
            baseline_pending = len(_pending_feedback(state_dir))

            result = _run(
                "record-feedback",
                "--identity-key",
                identity_key,
                "--status",
                "reject",
                "--note",
                "Suspicious provenance — reject outright.",
                "--state-dir",
                str(state_dir),
            )

            self.assertEqual(
                result.returncode, 3, f"Expected code 3, got {result.returncode}\n{result.stdout}\n{result.stderr}"
            )
            self.assertIn("confirmation_required=true", result.stdout)
            self.assertIn("candidate_title=Deep House", result.stdout)
            self.assertIn("candidate_author=E.N. Anonymous", result.stdout)
            self.assertIn(f"candidate_identity_key={identity_key}", result.stdout)
            self.assertIn("intended_status=reject", result.stdout)
            self.assertIn("editor_note=Suspicious provenance — reject outright.", result.stdout)
            self.assertIn("action=stage_pending_feedback", result.stdout)

            # No state mutation
            self.assertEqual(len(_pending_feedback(state_dir)), baseline_pending)
            self.assertEqual(len(_visibility(state_dir)), 0)

    def test_preview_prints_english_title_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            identity_key = "переведённая книга|автор"
            _write_candidate_state(
                state_dir,
                identity_key,
                title="Переведённая Книга",
                authors=["Автор"],
                english_title="Translated Book",
            )

            result = _run(
                "record-feedback",
                "--identity-key",
                identity_key,
                "--status",
                "interested",
                "--note",
                "Worth tracking.",
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(result.returncode, 3)
            self.assertIn("candidate_english_title=Translated Book", result.stdout)

    def test_confirmed_record_feedback_stages_exact_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            identity_key = "confirmed test|author"
            _write_candidate_state(state_dir, identity_key, title="Confirmed Test")

            note = 'VerbatiM — with "quotes", commas, and line\nbreaks.'
            result = _run(
                "record-feedback",
                "--identity-key",
                identity_key,
                "--status",
                "maybe_watch",
                "--note",
                note,
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("recorded_feedback", result.stdout)
            self.assertIn(identity_key, result.stdout)

            pending = _pending_feedback(state_dir)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["identity_key"], identity_key)
            self.assertEqual(pending[0]["Editor Status"], "maybe_watch")
            self.assertEqual(pending[0]["Editor Note"], note)

    def test_preview_invalid_status_still_fails_normally(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            _write_candidate_state(state_dir, "test|author")

            result = _run(
                "record-feedback",
                "--identity-key",
                "test|author",
                "--status",
                "bogus",
                "--state-dir",
                str(state_dir),
            )
            # Should fail with code 1 (validation), not code 3 (confirmation)
            self.assertEqual(result.returncode, 1)
            self.assertIn("Unsupported editor status", result.stdout)

    def test_preview_unknown_identity_still_fails_normally(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            result = _run(
                "record-feedback",
                "--identity-key",
                "nonexistent|unknown",
                "--status",
                "interested",
                "--state-dir",
                str(state_dir),
            )
            # Should fail with code 1 (validation), not code 3 (confirmation)
            self.assertEqual(result.returncode, 1)
            self.assertIn("Unknown candidate identity", result.stdout)


# ---------------------------------------------------------------------------
# Acceptance Test 3: editorial-profile add preview
# ---------------------------------------------------------------------------


class EditorialProfilePreviewTests(unittest.TestCase):
    def test_hard_rule_preview_without_confirmation_returns_code_3_and_no_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            result = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "author",
                "--subject",
                "E.N. Anonymous",
                "--quote",
                "Никогда. Категорически нет. Абсолютно исключено.",
                "--provenance",
                "editor chat 2026-07-14",
                "--state-dir",
                str(state_dir),
            )

            self.assertEqual(
                result.returncode, 3, f"Expected code 3, got {result.returncode}\n{result.stdout}\n{result.stderr}"
            )
            self.assertIn("confirmation_required=true", result.stdout)
            self.assertIn("kind=Hard Rule", result.stdout)
            self.assertIn("scope=author", result.stdout)
            self.assertIn("subject=E.N. Anonymous", result.stdout)
            self.assertIn("verbatim_quote=Никогда. Категорически нет. Абсолютно исключено.", result.stdout)
            self.assertIn("effect=hide matching future candidates", result.stdout)
            self.assertIn("hint=re-run with --confirmed-by-editor", result.stdout)

            # No profile entry was written
            self.assertEqual(len(_profile_entries(state_dir)), 0)

    def test_soft_preference_preview_shows_ranking_effect_not_hiding(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            result = _run(
                "editorial-profile",
                "add",
                "--kind",
                "soft-preference",
                "--scope",
                "topic",
                "--subject",
                "science",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "science",
                "--quote",
                "Больше научных книг — это приоритет.",
                "--provenance",
                "editor chat 2026-07-14",
                "--direction",
                "positive",
                "--state-dir",
                str(state_dir),
            )

            self.assertEqual(result.returncode, 3)
            self.assertIn("kind=Soft Preference", result.stdout)
            self.assertIn("effect=ranking adjustment", result.stdout)
            self.assertIn("positive (nudge priority up one step)", result.stdout)
            self.assertIn("does not hide candidates", result.stdout)

            # No profile entry was written
            self.assertEqual(len(_profile_entries(state_dir)), 0)

    def test_soft_preference_negative_preview_shows_downward_nudge(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            result = _run(
                "editorial-profile",
                "add",
                "--kind",
                "soft-preference",
                "--scope",
                "topic",
                "--subject",
                "memoir",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "memoir",
                "--quote",
                "Мемуаров и так много, не в приоритете.",
                "--provenance",
                "editor chat",
                "--direction",
                "negative",
                "--state-dir",
                str(state_dir),
            )

            self.assertEqual(result.returncode, 3)
            self.assertIn("negative (nudge priority down one step)", result.stdout)
            self.assertEqual(len(_profile_entries(state_dir)), 0)

    def test_confirmed_hard_rule_entry_writes_exact_verbatim_quote(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            quote = 'Никогда. Категорически нет. "Абсолютно" исключено.'

            result = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "author",
                "--subject",
                "E.N. Anonymous",
                "--quote",
                quote,
                "--provenance",
                "editor chat 2026-07-14",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("profile_entry_created=true", result.stdout)

            entries = _profile_entries(state_dir)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["verbatim_quote"], quote)
            self.assertEqual(entries[0]["kind"], "hard_rule")
            self.assertEqual(entries[0]["status"], "active")

    def test_confirmed_soft_preference_entry_writes_all_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            quote = "Больше научных книг — это приоритет."

            result = _run(
                "editorial-profile",
                "add",
                "--kind",
                "soft-preference",
                "--scope",
                "topic",
                "--subject",
                "science",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "science",
                "--quote",
                quote,
                "--provenance",
                "editor chat",
                "--direction",
                "positive",
                "--description",
                "Science books are high-value acquisitions.",
                "--example-books",
                "Brief History of Time, The Selfish Gene",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("profile_entry_created=true", result.stdout)

            entries = _profile_entries(state_dir)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["verbatim_quote"], quote)
            self.assertEqual(entries[0]["kind"], "soft_preference")
            self.assertEqual(entries[0]["direction"], "positive")
            self.assertEqual(entries[0]["description"], "Science books are high-value acquisitions.")
            self.assertEqual(entries[0]["example_books"], ["Brief History of Time", "The Selfish Gene"])


# ---------------------------------------------------------------------------
# Acceptance Test 5: Deep House scenario
# ---------------------------------------------------------------------------


class DeepHouseScenarioTests(unittest.TestCase):
    def test_neither_rejection_nor_hard_rule_exists_before_confirmation(self):
        """Deep House interaction: preview shows both effects; nothing written before confirmation."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            identity_key = "deep house|e.n. anonymous"
            _write_candidate_state(
                state_dir,
                identity_key,
                title="Deep House",
                authors=["E.N. Anonymous"],
                english_title="Deep House",
            )

            # Phase 1: Preview the rejection
            feedback_preview = _run(
                "record-feedback",
                "--identity-key",
                identity_key,
                "--status",
                "reject",
                "--note",
                "Сомнительное происхождение — отклоняем.",
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(feedback_preview.returncode, 3)
            self.assertIn("intended_status=reject", feedback_preview.stdout)
            self.assertIn(identity_key, feedback_preview.stdout)

            # Phase 1: Preview the Hard Rule
            rule_preview = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "author",
                "--subject",
                "E.N. Anonymous",
                "--quote",
                "Никогда. Категорически нет.",
                "--provenance",
                "editor conversation about Deep House",
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(rule_preview.returncode, 3)
            self.assertIn("kind=Hard Rule", rule_preview.stdout)
            self.assertIn("effect=hide matching future candidates", rule_preview.stdout)

            # NEITHER rejection NOR Hard Rule exists at this point
            self.assertEqual(len(_pending_feedback(state_dir)), 0)
            self.assertEqual(len(_profile_entries(state_dir)), 0)

            # Phase 2: Apply both effects after confirmation
            feedback_applied = _run(
                "record-feedback",
                "--identity-key",
                identity_key,
                "--status",
                "reject",
                "--note",
                "Сомнительное происхождение — отклоняем.",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            self.assertEqual(feedback_applied.returncode, 0)

            rule_applied = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "author",
                "--subject",
                "E.N. Anonymous",
                "--quote",
                "Никогда. Категорически нет.",
                "--provenance",
                "editor conversation about Deep House",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            self.assertEqual(rule_applied.returncode, 0)

            # NOW both exist
            pending = _pending_feedback(state_dir)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["identity_key"], identity_key)
            self.assertEqual(pending[0]["Editor Status"], "reject")

            entries = _profile_entries(state_dir)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["subject"], "E.N. Anonymous")
            self.assertEqual(entries[0]["kind"], "hard_rule")

    def test_only_confirmed_effects_are_applied(self):
        """When two effects are previewed but only one confirmed, only that one applies."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            identity_key = "partial confirm|author"
            _write_candidate_state(state_dir, identity_key, title="Partial Confirm")

            # Preview both
            feedback_preview = _run(
                "record-feedback",
                "--identity-key",
                identity_key,
                "--status",
                "reject",
                "--note",
                "Reject this one.",
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(feedback_preview.returncode, 3)

            rule_preview = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "author",
                "--subject",
                "Author",
                "--quote",
                "Block this author.",
                "--provenance",
                "editor chat",
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(rule_preview.returncode, 3)

            # Neither exists yet
            self.assertEqual(len(_pending_feedback(state_dir)), 0)
            self.assertEqual(len(_profile_entries(state_dir)), 0)

            # Apply only the rejection (editor confirmed rejection, not the rule)
            feedback_applied = _run(
                "record-feedback",
                "--identity-key",
                identity_key,
                "--status",
                "reject",
                "--note",
                "Reject this one.",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            self.assertEqual(feedback_applied.returncode, 0)

            # Only feedback exists, rule does not
            self.assertEqual(len(_pending_feedback(state_dir)), 1)
            self.assertEqual(len(_profile_entries(state_dir)), 0)


# ---------------------------------------------------------------------------
# Review finding 1: mkdir before confirmation gate
# ---------------------------------------------------------------------------


class ZeroMkdirTests(unittest.TestCase):
    def _state_file_bytes(self, state_dir: Path) -> dict[str, bytes]:
        """Snapshot every regular file in and under state_dir, keyed by relative path."""
        snap: dict[str, bytes] = {}
        if not state_dir.exists():
            return snap
        for root, _dirs, files in os.walk(str(state_dir)):
            root_path = Path(root)
            for name in sorted(files):
                file_path = root_path / name
                rel = file_path.relative_to(state_dir)
                snap[str(rel)] = file_path.read_bytes()
        snap.update(state_databases())
        return snap

    def _assert_bytes_unchanged(self, before: dict[str, bytes], after: dict[str, bytes]) -> None:
        """Fail if any file changed or appeared/disappeared."""
        removed = set(before) - set(after)
        added = set(after) - set(before)
        changed: list[str] = []
        for key in set(before) & set(after):
            if before[key] != after[key]:
                changed.append(key)
        failures: list[str] = []
        if removed:
            failures.append(f"files removed: {removed}")
        if added:
            failures.append(f"files added: {added}")
        if changed:
            failures.append(f"bytes changed: {changed}")
        if failures:
            self.fail("; ".join(failures))

        # Covered by test_invalid_preview_does_not_create_directory and
        # test_unknown_identity_preview_does_not_create_directory below.

    def test_feedback_preview_preserves_all_existing_state_bytes(self):
        """A feedback preview leaves every existing state file byte-for-byte unchanged."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            identity_key = "byte test|author"
            _write_candidate_state(state_dir, identity_key, title="Byte Test")

            # Seed an additional state file so we have multiple files to snapshot.
            seed_state(state_dir, candidate_visibility=[{"identity_key": "other|someone", "visibility": "hidden"}])

            before = self._state_file_bytes(state_dir)
            self.assertEqual(read_state(state_dir, "candidate_history")[0]["identity_key"], identity_key)

            result = _run(
                "record-feedback",
                "--identity-key",
                identity_key,
                "--status",
                "reject",
                "--note",
                "Byte-snapshot test.",
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(result.returncode, 3)

            after = self._state_file_bytes(state_dir)
            self._assert_bytes_unchanged(before, after)

    def test_profile_preview_preserves_all_existing_state_bytes(self):
        """A profile-entry preview leaves every existing state file byte-for-byte unchanged."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            # Seed an existing profile with a prior entry.
            state_dir.mkdir(parents=True)
            seed_state(
                state_dir,
                editorial_profile={
                    "entries": [
                        {
                            "id": "taste-0001",
                            "kind": "hard_rule",
                            "scope": "author",
                            "subject": "Existing Author",
                            "verbatim_quote": "Existing rule.",
                            "provenance": "previous chat",
                            "created_at": "2026-01-01",
                            "created_by": "agent",
                            "status": "active",
                        }
                    ]
                },
            )

            before = self._state_file_bytes(state_dir)
            self.assertEqual(len(read_state(state_dir, "editorial_profile")["entries"]), 1)

            result = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "author",
                "--subject",
                "New Author",
                "--quote",
                "New rule preview.",
                "--provenance",
                "new chat",
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(result.returncode, 3)

            after = self._state_file_bytes(state_dir)
            self._assert_bytes_unchanged(before, after)

    def test_invalid_preview_does_not_create_directory(self):
        """An invalid-status preview (code 1) against a non-existent state dir does not create it."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "nonexistent-state"
            self.assertFalse(state_dir.exists())

            result = _run(
                "record-feedback",
                "--identity-key",
                "anything",
                "--status",
                "bogus",
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(result.returncode, 1)
            self.assertFalse(state_dir.exists())

    def test_unknown_identity_preview_does_not_create_directory(self):
        """An unknown-identity preview (code 1) against a non-existent state dir does not create it."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "nonexistent-state"
            self.assertFalse(state_dir.exists())

            result = _run(
                "record-feedback",
                "--identity-key",
                "nonexistent|unknown",
                "--status",
                "interested",
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(result.returncode, 1)
            self.assertFalse(state_dir.exists())


# ---------------------------------------------------------------------------
# Review finding 2: verbatim quote preservation
# ---------------------------------------------------------------------------


class VerbatimQuotePreservationTests(unittest.TestCase):
    def test_verbatim_quote_with_leading_trailing_whitespace_and_newline(self):
        """Preview and stored verbatim_quote retain exact whitespace, punctuation, Unicode, newlines."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            # A quote with leading/trailing spaces, newline, Unicode, and punctuation.
            quote = "  Не надо. \nКатегорически — «нет»!  "

            # Phase 1: preview
            preview = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "author",
                "--subject",
                "Quoted Author",
                "--quote",
                quote,
                "--provenance",
                "editor chat",
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(preview.returncode, 3)
            # Preview must show the exact quote, not a stripped version.
            self.assertIn(f"verbatim_quote={quote}", preview.stdout)
            # No profile entry written yet.
            self.assertEqual(len(_profile_entries(state_dir)), 0)

            # Phase 2: apply
            applied = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "author",
                "--subject",
                "Quoted Author",
                "--quote",
                quote,
                "--provenance",
                "editor chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            self.assertEqual(applied.returncode, 0)

            entries = _profile_entries(state_dir)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["verbatim_quote"], quote)

    def test_verbatim_quote_with_unicode_em_dash_and_smart_quotes(self):
        """Unicode including em dashes and smart quotes is preserved exactly."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            quote = "Нет — “этого” автора не хочу."

            applied = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "author",
                "--subject",
                "Unicode Author",
                "--quote",
                quote,
                "--provenance",
                "editor chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            self.assertEqual(applied.returncode, 0)
            entries = _profile_entries(state_dir)
            self.assertEqual(entries[0]["verbatim_quote"], quote)


# ---------------------------------------------------------------------------
# Review finding 3: legacy string primary_authors
# ---------------------------------------------------------------------------


class LegacyAuthorDisplayTests(unittest.TestCase):
    def test_legacy_string_primary_authors_not_joined_character_by_character(self):
        """When primary_authors is a string (not a list), author display shows the string intact."""
        from autoscout.editor_feedback import _safe_author_display

        self.assertEqual(_safe_author_display(["First", "Second"]), "First, Second")
        self.assertEqual(_safe_author_display("Single Author"), "Single Author")
        self.assertEqual(_safe_author_display([]), "")
        self.assertEqual(_safe_author_display(""), "")
        self.assertNotEqual(_safe_author_display("Single Author"), "S, i, n, g, l, e,  , A, u, t, h, o, r")

    def test_preview_print_author_display_for_legacy_string_record(self):
        """Preview readback of a legacy candidate with string primary_authors shows it correctly."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(parents=True)
            # Legacy record where primary_authors is a plain string.
            history = [
                {
                    "identity_key": "legacy book|old author",
                    "title": "Legacy Book",
                    "primary_authors": "Old Author",
                    "last_seen_date": "2026-01-01",
                }
            ]
            seed_state(state_dir, candidate_history=history)

            result = _run(
                "record-feedback",
                "--identity-key",
                "legacy book|old author",
                "--status",
                "reject",
                "--note",
                "Legacy test.",
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(result.returncode, 3)
            self.assertIn("candidate_author=Old Author", result.stdout)


if __name__ == "__main__":
    unittest.main()
