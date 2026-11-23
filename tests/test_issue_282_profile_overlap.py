"""Acceptance tests for issue #282: Editorial Taste Profile overlap detection.

Covers:
- Exact duplicate detection: same kind/scope/normalized subject/behavior => no new ID
- Probable overlap with Russian existing + English new bridging via explicit term
- Multiple overlaps all emitted
- keep-existing: no mutation
- replace-existing: preview no mutation, confirmed atomic revoke+new with audit links
- Partial/stale replacement IDs rejected
- add-distinct: two-phase then confirmed creation
- Same vocabulary under materially different scope remains directly creatable
- Generic "books" alone does not create probable overlap
- Standalone revoke: preview then confirmed apply
- Byte-identical state with --confirmed-by-editor without resolution
- All existing tests pass after revoke boundary changes
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from state_store_support import read_state
from state_store_support import state_snapshot


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "autoscout", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _profile_entries(state_dir: Path) -> list[dict]:
    return read_state(state_dir, "editorial_profile")["entries"]


def _snapshot_profile_bytes(state_dir: Path) -> tuple[bytes | None, bytes | None]:
    """The profile's stored bytes, and the state database's content under SQLite."""
    snapshot = state_snapshot(state_dir)
    return snapshot["editorial_profile"], snapshot["state_database"]


# ---------------------------------------------------------------------------
# 1. Exact duplicate detection
# ---------------------------------------------------------------------------


class ExactDuplicateTests(unittest.TestCase):
    def test_exact_duplicate_hard_rule_author_no_new_id(self):
        """Adding same kind/scope/subject hard rule reports exact duplicate, code 0, no mutation."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            # Add first entry
            r1 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "author",
                "--subject",
                "Blocked Author",
                "--quote",
                "Never show this author.",
                "--provenance",
                "editor chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            self.assertEqual(r1.returncode, 0)
            self.assertIn("profile_entry_created=true", r1.stdout)
            first_id = r1.stdout.split("id=")[1].split("\n")[0]

            # Add duplicate
            r2 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "author",
                "--subject",
                "Blocked Author",
                "--quote",
                "Different wording but same subject.",  # different quote
                "--provenance",
                "different chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            self.assertEqual(r2.returncode, 0)
            self.assertIn("profile_entry_created=false", r2.stdout)
            self.assertIn("confirmation_required=false", r2.stdout)
            self.assertIn("exact_duplicate_exists_no_new_effect_needed", r2.stdout)
            self.assertIn("exact_duplicates=1", r2.stdout)
            self.assertIn(first_id, r2.stdout)

            # Only one entry in profile
            entries = _profile_entries(state_dir)
            self.assertEqual(len(entries), 1)

    def test_exact_duplicate_topic_hard_rule_same_effective_terms(self):
        """Exact duplicate topic hard rule: same subject + same matching_terms."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            r1 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer studies",
                "--quote",
                "No queer books.",
                "--provenance",
                "editor chat",
                "--match-term",
                "queer",
                "--match-term",
                "квир",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            self.assertEqual(r1.returncode, 0)

            r2 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer studies",
                "--quote",
                "Different quote, same effect.",
                "--provenance",
                "other chat",
                "--match-term",
                "queer",
                "--match-term",
                "квир",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            self.assertEqual(r2.returncode, 0)
            self.assertIn("exact_duplicate_exists_no_new_effect_needed", r2.stdout)

            entries = _profile_entries(state_dir)
            self.assertEqual(len(entries), 1)

    def test_exact_duplicate_soft_preference_same_direction(self):
        """Exact duplicate soft preference: same subject + same direction."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "soft-preference",
                "--scope",
                "author",
                "--subject",
                "Good Author",
                "--quote",
                "Great writer.",
                "--provenance",
                "editor chat",
                "--direction",
                "positive",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            r2 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "soft-preference",
                "--scope",
                "author",
                "--subject",
                "Good Author",
                "--quote",
                "Awesome!",
                "--provenance",
                "another chat",
                "--direction",
                "positive",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            self.assertEqual(r2.returncode, 0)
            self.assertIn("exact_duplicate_exists_no_new_effect_needed", r2.stdout)

            entries = _profile_entries(state_dir)
            self.assertEqual(len(entries), 1)

    def test_exact_duplicate_not_detected_with_different_scope(self):
        """Same subject under different scope is not a duplicate."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "author",
                "--subject",
                "Test Subject",
                "--quote",
                "Rule for author.",
                "--provenance",
                "editor chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            r2 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "publisher",
                "--subject",
                "Test Subject",
                "--quote",
                "Rule for publisher.",
                "--provenance",
                "editor chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            self.assertEqual(r2.returncode, 0)
            self.assertIn("profile_entry_created=true", r2.stdout)

            entries = _profile_entries(state_dir)
            self.assertEqual(len(entries), 2)


# ---------------------------------------------------------------------------
# 2. Probable overlap: Russian existing + English new with explicit term
# ---------------------------------------------------------------------------


class ProbableOverlapBridgingTests(unittest.TestCase):
    def test_russian_existing_english_new_with_kvir_term(self):
        """Legacy Russian subject overlaps new English subject via explicit 'квир' term."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            # Seed legacy entry — no matching_terms (uses subject as effective term).
            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "[editor note withheld]",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "[editor note withheld]",
                "--quote",
                "[editor note withheld]",
                "--provenance",
                "Codex chat, 2026-07-14",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            # New entry with explicit matching_terms bridging 'квир'.
            r2 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer studies",
                "--quote",
                "No queer topics.",
                "--provenance",
                "editor chat",
                "--match-term",
                "queerness",
                "--match-term",
                "queer",
                "--match-term",
                "квир",
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(r2.returncode, 3)
            self.assertIn("profile_entry_created=false", r2.stdout)
            self.assertIn("reason=probable_overlap_unresolved", r2.stdout)
            self.assertIn("probable_overlaps=1", r2.stdout)
            self.assertIn("taste-0001", r2.stdout)
            self.assertIn("[editor note withheld]", r2.stdout)
            self.assertIn("[editor note withheld]", r2.stdout)
            self.assertIn("probable_overlap", r2.stdout)
            self.assertIn("keep-existing", r2.stdout)
            self.assertIn("replace-existing", r2.stdout)
            self.assertIn("add-distinct", r2.stdout)

            # Zero mutation
            entries = _profile_entries(state_dir)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["status"], "active")

    def test_confirmed_by_editor_without_resolution_still_code_3(self):
        """Passing --confirmed-by-editor without resolving probable overlap still returns code 3."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "[editor note withheld]",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "[editor note withheld]",
                "--quote",
                "проблемная тематика",
                "--provenance",
                "editor chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            before = _snapshot_profile_bytes(state_dir)

            r2 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer studies",
                "--quote",
                "No queer books.",
                "--provenance",
                "editor chat",
                "--match-term",
                "queer",
                "--match-term",
                "квир",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
                # No overlap-resolution — unresolved.
            )
            self.assertEqual(r2.returncode, 3)
            self.assertIn("probable_overlap_unresolved", r2.stdout)

            # Byte-identical state
            after = _snapshot_profile_bytes(state_dir)
            self.assertEqual(before, after)

    def test_multiple_overlaps_all_emitted(self):
        """When multiple existing entries overlap, all are listed."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            # Two existing topic rules: one with 'queer' in subject, one with 'квир' in subject.
            # The new entry uses both tokens via explicit matching terms.
            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer art",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "queer art",
                "--quote",
                "First quote.",
                "--provenance",
                "chat 1",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "квир-тематика",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "квир-тематика",
                "--quote",
                "Second quote.",
                "--provenance",
                "chat 2",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            # New entry with matching terms that bridge both.
            r3 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer studies",
                "--quote",
                "Third quote.",
                "--provenance",
                "chat 3",
                "--match-term",
                "queer",
                "--match-term",
                "квир",
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(r3.returncode, 3)
            self.assertIn("probable_overlaps=2", r3.stdout)
            self.assertIn("queer art", r3.stdout)
            self.assertIn("квир-тематика", r3.stdout)

            # Both IDs listed
            ids_in_output = 0
            for entry in _profile_entries(state_dir):
                if entry["id"] in r3.stdout:
                    ids_in_output += 1
            self.assertEqual(ids_in_output, 2)


# ---------------------------------------------------------------------------
# 3. keep-existing
# ---------------------------------------------------------------------------


class KeepExistingTests(unittest.TestCase):
    def test_keep_existing_no_mutation(self):
        """keep-existing resolves overlap with zero mutation."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "квир-тематика",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "квир-тематика",
                "--quote",
                "Existing rule.",
                "--provenance",
                "chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            before = _snapshot_profile_bytes(state_dir)

            r2 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer studies",
                "--quote",
                "New rule proposal.",
                "--provenance",
                "chat",
                "--match-term",
                "queer",
                "--match-term",
                "квир",
                "--state-dir",
                str(state_dir),
                "--overlap-resolution",
                "keep-existing",
            )
            self.assertEqual(r2.returncode, 0)
            self.assertIn("profile_entry_created=false", r2.stdout)
            self.assertIn("keep_existing_probable_overlap_rules_preserved", r2.stdout)

            after = _snapshot_profile_bytes(state_dir)
            self.assertEqual(before, after)


# ---------------------------------------------------------------------------
# 4. replace-existing: preview + confirmed
# ---------------------------------------------------------------------------


class ReplaceExistingTests(unittest.TestCase):
    def test_replace_existing_preview_no_mutation(self):
        """replace-existing preview returns code 3, zero mutation."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "квир-тематика",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "квир-тематика",
                "--quote",
                "Existing quote.",
                "--provenance",
                "chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            before = _snapshot_profile_bytes(state_dir)

            r2 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer studies",
                "--quote",
                "Replacement quote.",
                "--provenance",
                "new chat",
                "--match-term",
                "queer",
                "--match-term",
                "квир",
                "--state-dir",
                str(state_dir),
                "--overlap-resolution",
                "replace-existing",
                "--replace-id",
                "taste-0001",
                "--replacement-reason",
                "Better coverage.",
            )
            self.assertEqual(r2.returncode, 3)
            self.assertIn("replace_existing_preview", r2.stdout)
            self.assertIn("confirmation_required=true", r2.stdout)
            self.assertIn("taste-0001", r2.stdout)

            after = _snapshot_profile_bytes(state_dir)
            self.assertEqual(before, after)

    def test_replace_existing_confirmed_atomic_revoke_and_add(self):
        """Confirmed replace-existing revokes old and creates new with audit links."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "квир-тематика",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "квир-тематика",
                "--quote",
                "Existing quote.",
                "--provenance",
                "chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            r2 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer studies",
                "--quote",
                "Replacement quote.",
                "--provenance",
                "new chat",
                "--match-term",
                "queer",
                "--match-term",
                "квир",
                "--state-dir",
                str(state_dir),
                "--overlap-resolution",
                "replace-existing",
                "--replace-id",
                "taste-0001",
                "--replacement-reason",
                "Better coverage with explicit terms.",
                "--confirmed-by-editor",
            )
            self.assertEqual(r2.returncode, 0)
            self.assertIn("profile_entry_created=true", r2.stdout)
            self.assertIn("revoked=taste-0001", r2.stdout)
            self.assertIn("replaces=", r2.stdout)
            self.assertIn("taste-0001", r2.stdout)

            entries = _profile_entries(state_dir)
            # Two entries: old (revoked) + new (active)
            self.assertEqual(len(entries), 2)

            old_entry = next((e for e in entries if e["id"] == "taste-0001"), None)
            self.assertIsNotNone(old_entry)
            self.assertEqual(old_entry["status"], "revoked")
            self.assertEqual(old_entry["verbatim_quote"], "Existing quote.")  # preserved
            self.assertEqual(old_entry["provenance"], "chat")  # preserved
            self.assertEqual(old_entry["revocation_reason"], "Better coverage with explicit terms.")
            self.assertIn("replaced_by", old_entry)
            self.assertEqual(old_entry["replaced_by"], "taste-0002")

            new_entry = next((e for e in entries if e["id"] == "taste-0002"), None)
            self.assertIsNotNone(new_entry)
            self.assertEqual(new_entry["status"], "active")
            self.assertEqual(new_entry["verbatim_quote"], "Replacement quote.")
            self.assertEqual(new_entry["subject"], "queer studies")
            self.assertIn("replaces", new_entry)
            self.assertEqual(new_entry["replaces"], ["taste-0001"])
            self.assertEqual(new_entry["matching_terms"], ["queer", "квир"])

    def test_replace_existing_requires_replacement_reason(self):
        """replace-existing without --replacement-reason returns code 2."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "квир-тематика",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "квир-тематика",
                "--quote",
                "Existing.",
                "--provenance",
                "chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            r2 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer studies",
                "--quote",
                "New.",
                "--provenance",
                "chat",
                "--match-term",
                "queer",
                "--match-term",
                "квир",
                "--state-dir",
                str(state_dir),
                "--overlap-resolution",
                "replace-existing",
                "--replace-id",
                "taste-0001",
                # No --replacement-reason
            )
            self.assertEqual(r2.returncode, 2)
            self.assertIn("replacement_reason_required", r2.stdout)

    def test_replace_existing_partial_id_rejected(self):
        """Replacing only some of the probable overlap IDs is rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "квир-тематика",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "квир-тематика",
                "--quote",
                "First.",
                "--provenance",
                "chat 1",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer films",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "queer films",
                "--quote",
                "Second.",
                "--provenance",
                "chat 2",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            before = _snapshot_profile_bytes(state_dir)

            # Only replace one of two overlapping IDs.
            r3 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer art",
                "--quote",
                "Third.",
                "--provenance",
                "chat 3",
                "--match-term",
                "queer",
                "--state-dir",
                str(state_dir),
                "--overlap-resolution",
                "replace-existing",
                "--replace-id",
                "taste-0001",  # missing taste-0002
                "--replacement-reason",
                "Missing one.",
            )
            self.assertEqual(r3.returncode, 2)
            self.assertIn("replace_ids_must_match_all_current_probable_overlap_ids", r3.stdout)
            self.assertIn("missing_ids", r3.stdout)

            after = _snapshot_profile_bytes(state_dir)
            self.assertEqual(before, after)

    def test_replace_existing_stale_id_rejected(self):
        """Passing an ID that isn't a probable overlap is rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "квир-тематика",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "квир-тематика",
                "--quote",
                "First.",
                "--provenance",
                "chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            before = _snapshot_profile_bytes(state_dir)

            r2 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer art",
                "--quote",
                "Second.",
                "--provenance",
                "chat",
                "--match-term",
                "queer",
                "--match-term",
                "квир",
                "--state-dir",
                str(state_dir),
                "--overlap-resolution",
                "replace-existing",
                "--replace-id",
                "taste-0001",  # covers the overlap
                "--replace-id",
                "taste-0099",  # stale/nonexistent
                "--replacement-reason",
                "Testing.",
            )
            self.assertEqual(r2.returncode, 2)
            self.assertIn("stale_or_extra_ids", r2.stdout)

            after = _snapshot_profile_bytes(state_dir)
            self.assertEqual(before, after)

    def test_replace_existing_empty_replace_ids_rejected(self):
        """replace-existing with no --replace-id returns code 2."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "квир-тематика",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "квир-тематика",
                "--quote",
                "Existing.",
                "--provenance",
                "chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            r2 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer studies",
                "--quote",
                "New.",
                "--provenance",
                "chat",
                "--match-term",
                "queer",
                "--match-term",
                "квир",
                "--state-dir",
                str(state_dir),
                "--overlap-resolution",
                "replace-existing",
                "--replacement-reason",
                "No IDs provided.",
            )
            self.assertEqual(r2.returncode, 2)
            self.assertIn("replace_existing_requires_explicit_replace_ids", r2.stdout)


# ---------------------------------------------------------------------------
# 5. add-distinct: two-phase then confirmed
# ---------------------------------------------------------------------------


class AddDistinctTests(unittest.TestCase):
    def test_add_distinct_preview_code_3_zero_mutation(self):
        """add-distinct preview returns code 3, zero mutation."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "квир-тематика",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "квир-тематика",
                "--quote",
                "Existing.",
                "--provenance",
                "chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            before = _snapshot_profile_bytes(state_dir)

            r2 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer studies",
                "--quote",
                "Distinct new rule.",
                "--provenance",
                "new chat",
                "--match-term",
                "queer",
                "--match-term",
                "квир",
                "--state-dir",
                str(state_dir),
                "--overlap-resolution",
                "add-distinct",
            )
            self.assertEqual(r2.returncode, 3)
            self.assertIn("add_distinct_preview", r2.stdout)
            self.assertIn("probable_overlap", r2.stdout)

            after = _snapshot_profile_bytes(state_dir)
            self.assertEqual(before, after)

    def test_add_distinct_confirmed_creates_new_entry(self):
        """Confirmed add-distinct creates a new entry alongside existing."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "квир-тематика",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "квир-тематика",
                "--quote",
                "First quote.",
                "--provenance",
                "chat 1",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            r2 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer studies",
                "--quote",
                "Second quote.",
                "--provenance",
                "chat 2",
                "--match-term",
                "queer",
                "--match-term",
                "квир",
                "--state-dir",
                str(state_dir),
                "--overlap-resolution",
                "add-distinct",
                "--confirmed-by-editor",
            )
            self.assertEqual(r2.returncode, 0)
            self.assertIn("profile_entry_created=true", r2.stdout)

            entries = _profile_entries(state_dir)
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0]["status"], "active")  # preserved
            self.assertEqual(entries[1]["status"], "active")  # new


# ---------------------------------------------------------------------------
# 6. Different scope remains directly creatable
# ---------------------------------------------------------------------------


class DifferentScopeNoOverlapTests(unittest.TestCase):
    def test_same_vocabulary_different_scope_creatable(self):
        """Same vocabulary under different scope does not trigger overlap detection."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "author",
                "--subject",
                "Queer Author",
                "--quote",
                "Bad author.",
                "--provenance",
                "chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            r2 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer studies",
                "--quote",
                "Bad topic.",
                "--provenance",
                "chat",
                "--match-term",
                "queer",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            self.assertEqual(r2.returncode, 0)
            self.assertIn("profile_entry_created=true", r2.stdout)

            entries = _profile_entries(state_dir)
            self.assertEqual(len(entries), 2)

    def test_same_vocabulary_different_kind_creatable(self):
        """Same subject, different kind (hard-rule vs soft-preference) is creatable."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "author",
                "--subject",
                "Shared Subject",
                "--quote",
                "Hard rule quote.",
                "--provenance",
                "chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            r2 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "soft-preference",
                "--scope",
                "author",
                "--subject",
                "Shared Subject",
                "--quote",
                "Soft pref quote.",
                "--provenance",
                "chat",
                "--direction",
                "positive",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            self.assertEqual(r2.returncode, 0)
            self.assertIn("profile_entry_created=true", r2.stdout)


# ---------------------------------------------------------------------------
# 7. Generic "books" alone does not create probable overlap
# ---------------------------------------------------------------------------


class GenericWordNoOverlapTests(unittest.TestCase):
    def test_generic_books_token_no_overlap(self):
        """'history books' and 'science books' should NOT overlap on 'books'."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "history books",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "history books",
                "--quote",
                "No history.",
                "--provenance",
                "chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            r2 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "science books",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "science books",
                "--quote",
                "No science.",
                "--provenance",
                "chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            self.assertEqual(r2.returncode, 0)
            self.assertIn("profile_entry_created=true", r2.stdout)

            entries = _profile_entries(state_dir)
            self.assertEqual(len(entries), 2)

    def test_generic_topic_word_no_overlap(self):
        """'topic one' and 'topic two' should NOT overlap on 'topic'."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "medical topics",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "medical topics",
                "--quote",
                "No medical.",
                "--provenance",
                "chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            r2 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "historical topics",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "historical topics",
                "--quote",
                "No history.",
                "--provenance",
                "chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            self.assertEqual(r2.returncode, 0)
            self.assertIn("profile_entry_created=true", r2.stdout)


# ---------------------------------------------------------------------------
# 8. Standalone revoke: preview then confirmed
# ---------------------------------------------------------------------------


class StandaloneRevokeTests(unittest.TestCase):
    def test_revoke_preview_without_confirmation_returns_code_3(self):
        """Revoke preview returns code 3, zero mutation."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "author",
                "--subject",
                "Test Author",
                "--quote",
                "Test quote.",
                "--provenance",
                "chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            before = _snapshot_profile_bytes(state_dir)

            r2 = _run(
                "editorial-profile",
                "revoke",
                "--id",
                "taste-0001",
                "--reason",
                "No longer needed.",
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(r2.returncode, 3)
            self.assertIn("confirmation_required=true", r2.stdout)
            self.assertIn("Test Author", r2.stdout)
            self.assertIn("Test quote.", r2.stdout)
            self.assertIn("No longer needed.", r2.stdout)

            after = _snapshot_profile_bytes(state_dir)
            self.assertEqual(before, after)

    def test_revoke_confirmed_applies_revocation(self):
        """Confirmed revoke sets status to revoked."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "author",
                "--subject",
                "Test Author",
                "--quote",
                "Test quote.",
                "--provenance",
                "chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            r2 = _run(
                "editorial-profile",
                "revoke",
                "--id",
                "taste-0001",
                "--reason",
                "No longer needed.",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            self.assertEqual(r2.returncode, 0)
            self.assertIn("profile_entry_revoked=true", r2.stdout)

            entries = _profile_entries(state_dir)
            self.assertEqual(entries[0]["status"], "revoked")
            self.assertEqual(entries[0]["revocation_reason"], "No longer needed.")

    def test_revoke_not_found_returns_code_1(self):
        """Revoking a non-existent entry returns code 1."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            r = _run(
                "editorial-profile",
                "revoke",
                "--id",
                "taste-0099",
                "--reason",
                "Does not exist.",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            self.assertEqual(r.returncode, 1)
            self.assertIn("profile_entry_not_found", r.stdout)

    def test_revoke_already_revoked_returns_code_1(self):
        """Revoking an already-revoked entry returns code 1 with clear message."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "author",
                "--subject",
                "Test Author",
                "--quote",
                "Test quote.",
                "--provenance",
                "chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            _run(
                "editorial-profile",
                "revoke",
                "--id",
                "taste-0001",
                "--reason",
                "First revocation.",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            r3 = _run(
                "editorial-profile",
                "revoke",
                "--id",
                "taste-0001",
                "--reason",
                "Second attempt.",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            self.assertEqual(r3.returncode, 1)
            self.assertIn("profile_entry_already_revoked", r3.stdout)

    def test_revoke_preview_shows_subject_and_verbatim_quote(self):
        """Revoke preview includes id, subject, verbatim_quote, reason."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "soft-preference",
                "--scope",
                "author",
                "--subject",
                "Kind Author",
                "--quote",
                "Very kind writer — keep them!",
                "--provenance",
                "chat",
                "--direction",
                "positive",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            r2 = _run(
                "editorial-profile",
                "revoke",
                "--id",
                "taste-0001",
                "--reason",
                "Changed my mind.",
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(r2.returncode, 3)
            self.assertIn("id=taste-0001", r2.stdout)
            self.assertIn("subject=Kind Author", r2.stdout)
            self.assertIn("verbatim_quote=Very kind writer — keep them!", r2.stdout)
            self.assertIn("revocation_reason=Changed my mind.", r2.stdout)
            self.assertIn("confirmation_required=true", r2.stdout)


# ---------------------------------------------------------------------------
# 9. Validation: optional audit fields
# ---------------------------------------------------------------------------


class AuditFieldValidationTests(unittest.TestCase):
    def test_replaces_audit_field_validated(self):
        """'replaces' must be a list of non-blank strings."""
        from autoscout.editorial_taste_profile import TasteProfileError
        from autoscout.editorial_taste_profile import validate_taste_profile

        with self.assertRaises(TasteProfileError):
            validate_taste_profile(
                {
                    "entries": [
                        {
                            "id": "taste-0001",
                            "kind": "hard_rule",
                            "scope": "author",
                            "subject": "X",
                            "verbatim_quote": "q",
                            "provenance": "p",
                            "created_at": "2026-01-01",
                            "created_by": "agent",
                            "status": "active",
                            "replaces": ["", "bad"],  # blank strings
                        }
                    ]
                }
            )

    def test_replaced_by_audit_field_validated(self):
        """'replaced_by' must be a non-blank string."""
        from autoscout.editorial_taste_profile import TasteProfileError
        from autoscout.editorial_taste_profile import validate_taste_profile

        with self.assertRaises(TasteProfileError):
            validate_taste_profile(
                {
                    "entries": [
                        {
                            "id": "taste-0001",
                            "kind": "hard_rule",
                            "scope": "author",
                            "subject": "X",
                            "verbatim_quote": "q",
                            "provenance": "p",
                            "created_at": "2026-01-01",
                            "created_by": "agent",
                            "status": "active",
                            "replaced_by": "",  # blank
                        }
                    ]
                }
            )

    def test_valid_replaces_and_replaced_by_accepted(self):
        """Valid audit fields pass validation."""
        from autoscout.editorial_taste_profile import validate_taste_profile

        # Should not raise.
        validate_taste_profile(
            {
                "entries": [
                    {
                        "id": "taste-0001",
                        "kind": "hard_rule",
                        "scope": "author",
                        "subject": "X",
                        "verbatim_quote": "q",
                        "provenance": "p",
                        "created_at": "2026-01-01",
                        "created_by": "agent",
                        "status": "revoked",
                        "revoked_at": "2026-02-01",
                        "revoked_by": "editor",
                        "revocation_reason": "r",
                        "replaced_by": "taste-0002",
                    },
                    {
                        "id": "taste-0002",
                        "kind": "hard_rule",
                        "scope": "author",
                        "subject": "Y",
                        "verbatim_quote": "q2",
                        "provenance": "p2",
                        "created_at": "2026-02-01",
                        "created_by": "agent",
                        "status": "active",
                        "replaces": ["taste-0001"],
                    },
                ]
            }
        )


# ---------------------------------------------------------------------------
# 10. Resolution flag rejected when no probable overlap
# ---------------------------------------------------------------------------


class ResolutionFlagWithoutOverlapTests(unittest.TestCase):
    def test_overlap_resolution_rejected_when_no_overlap(self):
        """Passing --overlap-resolution when no overlap exists is rejected with code 2."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            # No existing entries at all.
            r = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "author",
                "--subject",
                "Unique Author",
                "--quote",
                "Unique quote.",
                "--provenance",
                "chat",
                "--state-dir",
                str(state_dir),
                "--overlap-resolution",
                "replace-existing",
                "--replace-id",
                "taste-0099",
                "--replacement-reason",
                "No overlap.",
            )
            self.assertEqual(r.returncode, 2)
            self.assertIn("overlap_resolution_supplied_without_probable_overlap", r.stdout)

    def test_replace_id_rejected_with_keep_existing_resolution(self):
        """Passing --replace-id with keep-existing resolution is rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer art",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "queer art",
                "--quote",
                "Existing.",
                "--provenance",
                "chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            r = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer studies",
                "--quote",
                "New.",
                "--provenance",
                "chat",
                "--match-term",
                "queer",
                "--state-dir",
                str(state_dir),
                "--overlap-resolution",
                "keep-existing",
                "--replace-id",
                "taste-0001",
            )
            self.assertEqual(r.returncode, 2)
            self.assertIn("replace_ids_only_valid_with_replace_existing_resolution", r.stdout)

    def test_replacement_reason_rejected_with_add_distinct_resolution(self):
        """Passing --replacement-reason with add-distinct resolution is rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer art",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "queer art",
                "--quote",
                "Existing.",
                "--provenance",
                "chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            r = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer studies",
                "--quote",
                "New.",
                "--provenance",
                "chat",
                "--match-term",
                "queer",
                "--state-dir",
                str(state_dir),
                "--overlap-resolution",
                "add-distinct",
                "--replacement-reason",
                "Should be rejected.",
            )
            self.assertEqual(r.returncode, 2)
            self.assertIn("replacement_reason_only_valid_with_replace_existing_resolution", r.stdout)

    def test_replace_id_rejected_with_probable_overlap_and_no_resolution(self):
        """Finding 1 (round 2): --replace-id with probable overlap but no
        --overlap-resolution is rejected (code 2, zero mutation)."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer art",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "queer art",
                "--quote",
                "Existing.",
                "--provenance",
                "chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            before = _snapshot_profile_bytes(state_dir)

            r = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer studies",
                "--quote",
                "New.",
                "--provenance",
                "chat",
                "--match-term",
                "queer",
                "--state-dir",
                str(state_dir),
                "--replace-id",
                "taste-0001",
            )
            self.assertEqual(r.returncode, 2)
            self.assertIn("replace_ids_only_valid_with_replace_existing_resolution", r.stdout)

            after = _snapshot_profile_bytes(state_dir)
            self.assertEqual(before, after)

    def test_replacement_reason_rejected_with_probable_overlap_and_no_resolution(self):
        """Finding 1 (round 2): --replacement-reason with probable overlap but
        no --overlap-resolution is rejected (code 2, zero mutation)."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer art",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "queer art",
                "--quote",
                "Existing.",
                "--provenance",
                "chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            before = _snapshot_profile_bytes(state_dir)

            r = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer studies",
                "--quote",
                "New.",
                "--provenance",
                "chat",
                "--match-term",
                "queer",
                "--state-dir",
                str(state_dir),
                "--replacement-reason",
                "Unspecified resolution.",
            )
            self.assertEqual(r.returncode, 2)
            self.assertIn("replacement_reason_only_valid_with_replace_existing_resolution", r.stdout)

            after = _snapshot_profile_bytes(state_dir)
            self.assertEqual(before, after)

    def test_invalid_overlap_resolution_value_rejected(self):
        """Invalid --overlap-resolution value returns code 2."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            r = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "author",
                "--subject",
                "Any Author",
                "--quote",
                "Quote.",
                "--provenance",
                "chat",
                "--state-dir",
                str(state_dir),
                "--overlap-resolution",
                "bogus-resolution",
            )
            self.assertEqual(r.returncode, 2)
            self.assertIn("invalid_overlap_resolution", r.stdout)


# ---------------------------------------------------------------------------
# 11. Finding 1: Topic union-token detection and same-subject/different-terms
# ---------------------------------------------------------------------------


class TopicUnionTokenTests(unittest.TestCase):
    def test_different_topic_subjects_shared_union_token_overlap(self):
        """Topic rules with different subjects but union-token overlap are detected.

        Existing: subject "queer art" (no matching_terms → effective=["queer art"])
        Proposed: subject "queer studies" + matching_terms ["queerness"]

        The union of subject tokens + effective terms:
        - Existing: {"queer", "art", "queer", "art"} → {"queer", "art"}
        - Proposed: {"queer", "studies", "queerness"}

        Shared non-generic: {"queer"} → probable overlap.
        """
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer art",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "queer art",
                "--quote",
                "First quote.",
                "--provenance",
                "chat 1",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            r2 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer studies",
                "--quote",
                "Second quote.",
                "--provenance",
                "chat 2",
                "--match-term",
                "queerness",
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(r2.returncode, 3)
            self.assertIn("probable_overlaps=1", r2.stdout)
            self.assertIn("queer art", r2.stdout)
            self.assertIn("shared_tokens", r2.stdout)

    def test_same_topic_subject_different_matching_terms_probable_overlap(self):
        """Same topic subject with different matching_terms → probable overlap (finding 1).

        After exact-duplicate check passes (because matching_terms differ),
        the same-subject check should catch this as probable overlap.
        """
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer studies",
                "--quote",
                "First quote.",
                "--provenance",
                "chat 1",
                "--match-term",
                "queer",
                "--match-term",
                "квир",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            r2 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer studies",
                "--quote",
                "Second quote.",
                "--provenance",
                "chat 2",
                "--match-term",
                "queerness",
                "--match-term",
                "lgbtq",
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(r2.returncode, 3)
            self.assertIn("probable_overlaps=1", r2.stdout)
            self.assertIn("queer studies", r2.stdout)
            # Overlap detected — may be via token overlap or same_subject_different_behavior.
            self.assertTrue("shared_tokens" in r2.stdout or "same_subject_different_behavior" in r2.stdout)

    def test_same_subject_soft_preference_different_direction_probable_overlap(self):
        """Same soft-preference subject with different direction → probable overlap."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "soft-preference",
                "--scope",
                "author",
                "--subject",
                "John Smith",
                "--quote",
                "Good author.",
                "--provenance",
                "chat 1",
                "--direction",
                "positive",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            r2 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "soft-preference",
                "--scope",
                "author",
                "--subject",
                "John Smith",
                "--quote",
                "Bad author now.",
                "--provenance",
                "chat 2",
                "--direction",
                "negative",
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(r2.returncode, 3)
            self.assertIn("probable_overlaps=1", r2.stdout)
            # Can be caught by either token overlap or same_subject_different_behavior.
            self.assertTrue("shared_tokens" in r2.stdout or "same_subject_different_behavior" in r2.stdout)

    def test_same_topic_subject_hard_rule_different_matching_terms_detected(self):
        """Same topic hard-rule subject with truly different matching terms (no shared token).

        This tests the fallback: token-union overlap catches the case where
        subjects are the same (so tokens overlap). When subjects are identical
        but matching_terms share no tokens, Check 2 catches it.
        Actually, with same subject on topic, token-level Check 1 always fires
        because the subject is tokenized.  But when subject differs and only
        matching_terms differ, union tokens cover it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            # Existing: subject "algorithms", matching_terms ["algorithms", "complexity"]
            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "algorithms",
                "--quote",
                "First.",
                "--provenance",
                "chat 1",
                "--match-term",
                "algorithms",
                "--match-term",
                "complexity",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            # New: subject "algorithms" (same), matching_terms ["data-structures"]
            # Token union: {"algorithms", "complexity"} ∩ {"algorithms", "data-structures"} = {"algorithms"}
            r2 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "algorithms",
                "--quote",
                "Second.",
                "--provenance",
                "chat 2",
                "--match-term",
                "data-structures",
                "--state-dir",
                str(state_dir),
            )
            # Since subjects are "algorithms" (same), exact-duplicate check compares
            # {"algorithms", "complexity"} vs {"algorithms", "data-structures"} — not equal,
            # so exact duplicate fails. Then token overlap finds {"algorithms"}.
            self.assertEqual(r2.returncode, 3)
            self.assertIn("probable_overlaps=1", r2.stdout)

    def test_different_topic_subjects_no_overlap_no_shared_token(self):
        """Topic rules with no shared meaningful token are not flagged."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "science books",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "science books",
                "--quote",
                "No science.",
                "--provenance",
                "chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            r2 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "history books",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "history books",
                "--quote",
                "No history.",
                "--provenance",
                "chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            self.assertEqual(r2.returncode, 0)
            self.assertIn("profile_entry_created=true", r2.stdout)


# ---------------------------------------------------------------------------
# 12. Finding 2: Overlap previews include full proposal info
# ---------------------------------------------------------------------------


class OverlapPreviewIncludesFullProposalTests(unittest.TestCase):
    def test_unresolved_overlap_preview_includes_matching_terms_and_effect(self):
        """Unresolved probable-overlap preview still prints matching_terms and effect."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer art",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "queer art",
                "--quote",
                "Existing.",
                "--provenance",
                "chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            r2 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer studies",
                "--quote",
                "New proposal.",
                "--provenance",
                "new chat",
                "--match-term",
                "queer",
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(r2.returncode, 3)
            # Must contain the normal proposal preview fields.
            self.assertIn("kind=Hard Rule", r2.stdout)
            self.assertIn("subject=queer studies", r2.stdout)
            self.assertIn("verbatim_quote=New proposal.", r2.stdout)
            self.assertIn("matching_terms=", r2.stdout)
            self.assertIn("effect=hide matching future candidates", r2.stdout)
            # Must also contain overlap info.
            self.assertIn("probable_overlaps=1", r2.stdout)
            self.assertIn("probable_overlap_unresolved", r2.stdout)

    def test_replace_existing_preview_includes_full_proposal(self):
        """Replace-existing preview includes full proposal + replacement details."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer art",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "queer art",
                "--quote",
                "Existing.",
                "--provenance",
                "chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            r2 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer studies",
                "--quote",
                "Replacement proposal.",
                "--provenance",
                "new chat",
                "--match-term",
                "queer",
                "--match-term",
                "квир",
                "--state-dir",
                str(state_dir),
                "--overlap-resolution",
                "replace-existing",
                "--replace-id",
                "taste-0001",
                "--replacement-reason",
                "Better coverage.",
            )
            self.assertEqual(r2.returncode, 3)
            # Full proposal info present.
            self.assertIn("kind=Hard Rule", r2.stdout)
            self.assertIn("subject=queer studies", r2.stdout)
            self.assertIn("matching_terms=", r2.stdout)
            self.assertIn("effect=hide matching future candidates", r2.stdout)
            # Replacement-specific info.
            self.assertIn("replace_existing_preview", r2.stdout)
            self.assertIn("replace_ids=", r2.stdout)
            self.assertIn("Better coverage.", r2.stdout)
            self.assertIn("probable_overlaps=1", r2.stdout)

    def test_add_distinct_preview_includes_full_proposal(self):
        """Add-distinct preview includes full proposal + overlap context."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer art",
                # Topic entries require explicit matching_terms (#551).
                "--match-term",
                "queer art",
                "--quote",
                "Existing.",
                "--provenance",
                "chat",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )

            r2 = _run(
                "editorial-profile",
                "add",
                "--kind",
                "hard-rule",
                "--scope",
                "topic",
                "--subject",
                "queer studies",
                "--quote",
                "Distinct proposal.",
                "--provenance",
                "new chat",
                "--match-term",
                "queer",
                "--state-dir",
                str(state_dir),
                "--overlap-resolution",
                "add-distinct",
            )
            self.assertEqual(r2.returncode, 3)
            # Full proposal info present.
            self.assertIn("kind=Hard Rule", r2.stdout)
            self.assertIn("subject=queer studies", r2.stdout)
            self.assertIn("verbatim_quote=Distinct proposal.", r2.stdout)
            self.assertIn("matching_terms=", r2.stdout)
            self.assertIn("effect=hide matching future candidates", r2.stdout)
            # Overlap context.
            self.assertIn("add_distinct_preview", r2.stdout)
            self.assertIn("probable_overlaps=1", r2.stdout)
            self.assertIn("resolution=add_distinct", r2.stdout)


# ---------------------------------------------------------------------------
# 13. Finding 4: Empty replaces list rejected
# ---------------------------------------------------------------------------


class EmptyReplacesListRejectedTests(unittest.TestCase):
    def test_empty_replaces_list_raises_validation_error(self):
        """An empty 'replaces' list is rejected by validate_taste_profile."""
        from autoscout.editorial_taste_profile import TasteProfileError
        from autoscout.editorial_taste_profile import validate_taste_profile

        with self.assertRaises(TasteProfileError):
            validate_taste_profile(
                {
                    "entries": [
                        {
                            "id": "taste-0001",
                            "kind": "hard_rule",
                            "scope": "author",
                            "subject": "X",
                            "verbatim_quote": "q",
                            "provenance": "p",
                            "created_at": "2026-01-01",
                            "created_by": "agent",
                            "status": "active",
                            "replaces": [],  # empty list
                        }
                    ]
                }
            )


if __name__ == "__main__":
    unittest.main()
