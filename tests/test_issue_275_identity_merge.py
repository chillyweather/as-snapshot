"""Acceptance tests for issue #275: robust Candidate identity merging.

Covers all review probes from the main model's findings A1-A10.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from state_store_support import read_state
from state_store_support import seed_state

from autoscout.identity import mention_canonical_rank
from autoscout.identity import mentions_are_identity_match
from autoscout.identity import normalize_author_display
from autoscout.identity import normalize_isbn
from autoscout.identity import normalized_identity_key
from autoscout.identity import pick_canonical_mention
from autoscout.pipeline import _build_candidate_from_mention_group
from autoscout.pipeline import _group_mentions_by_identity
from autoscout.pipeline import build_scouting_candidates
from autoscout.pipeline import run_pipeline


class AuthorDisplayNormalizationTests(unittest.TestCase):
    def test_comma_author_flips_to_first_last(self):
        self.assertEqual(normalize_author_display("Robbins, Mel"), "Mel Robbins")

    def test_plain_author_unchanged(self):
        self.assertEqual(normalize_author_display("Mel Robbins"), "Mel Robbins")

    def test_extra_whitespace_trimmed(self):
        self.assertEqual(normalize_author_display("  Robbins, Mel  "), "Mel Robbins")

    def test_single_name_with_comma_preserved(self):
        self.assertEqual(normalize_author_display("Robbins,"), "Robbins")


class IsbnNormalizationTests(unittest.TestCase):
    def test_strips_hyphens(self):
        self.assertEqual(normalize_isbn("978-1-4019-7136-6"), "9781401971366")

    def test_strips_spaces(self):
        self.assertEqual(normalize_isbn("978 1 4019 7136 6"), "9781401971366")

    def test_blank_is_empty(self):
        self.assertEqual(normalize_isbn(""), "")
        self.assertEqual(normalize_isbn("  "), "")

    def test_none_is_empty(self):
        self.assertEqual(normalize_isbn(None), "")


class MentionsAreIdentityMatchTests(unittest.TestCase):
    """Direct tests for the centralized mentions_are_identity_match."""

    def _m(self, title, authors, isbn=""):
        return {"title": title, "primary_authors": authors, "isbn": isbn}

    def test_exact_key_match(self):
        a = self._m("The Let Them Theory", ["Mel Robbins"])
        b = self._m("The Let Them Theory", ["Mel Robbins"])
        self.assertTrue(mentions_are_identity_match(a, b))

    def test_author_display_variant(self):
        a = self._m("The Let Them Theory", ["Mel Robbins"])
        b = self._m("The Let Them Theory", ["Robbins, Mel"])
        self.assertTrue(mentions_are_identity_match(a, b))

    def test_subtitle_insensitive(self):
        a = self._m("The Let Them Theory", ["Mel Robbins"])
        b = self._m("The Let Them Theory: Full Title", ["Mel Robbins"])
        self.assertTrue(mentions_are_identity_match(a, b))

    def test_isbn_match_overrides_divergent_titles(self):
        a = self._m("Book A", ["Alpha"], "9781401971366")
        b = self._m("Book A Extended", ["Alpha"], "9781401971366")
        self.assertTrue(mentions_are_identity_match(a, b))

    def test_isbn_match_overrides_divergent_authors(self):
        a = self._m("Same Title", ["Alpha"], "9781401971366")
        b = self._m("Same Title", ["Beta"], "9781401971366")
        self.assertTrue(mentions_are_identity_match(a, b))

    def test_no_match_divergent_isbn_and_authors(self):
        a = self._m("Same Title", ["Alpha"], "9780000000001")
        b = self._m("Same Title", ["Beta"], "9780000000002")
        self.assertFalse(mentions_are_identity_match(a, b))

    def test_shared_author_matches_with_other_contributors(self):
        a = self._m("Same Title", ["Alpha", "Gamma"], "")
        b = self._m("Same Title", ["Beta", "Gamma"], "")
        self.assertTrue(mentions_are_identity_match(a, b))

    def test_disjoint_authors_do_not_match_without_isbn(self):
        a = self._m("Same Title", ["Alpha"], "")
        b = self._m("Same Title", ["Beta"], "")
        self.assertFalse(mentions_are_identity_match(a, b))

    def test_empty_contributor_no_match(self):
        a = self._m("Same Title", [], "")
        b = self._m("Same Title", ["Alpha"], "")
        self.assertFalse(mentions_are_identity_match(a, b))

    def test_empty_contributor_both_no_match(self):
        a = self._m("Same Title", [], "")
        b = self._m("Same Title", [], "")
        self.assertFalse(mentions_are_identity_match(a, b))


class CanonicalMentionRankTests(unittest.TestCase):
    """Issue 1: deterministic canonical mention selection."""

    def test_narrower_contributor_preferred(self):
        a = {"title": "Book", "primary_authors": ["Alpha", "Beta"], "isbn": ""}
        b = {"title": "Book", "primary_authors": ["Alpha"], "isbn": ""}
        picked = pick_canonical_mention([a, b])
        self.assertEqual(picked["primary_authors"], ["Alpha"])

    def test_nonempty_preferred_over_empty(self):
        """Issue 1: nonempty contributor list ranks ahead of empty."""
        a = {"title": "Book", "primary_authors": ["Known Author"], "isbn": "9781"}
        b = {"title": "Unknown listing", "primary_authors": [], "isbn": "9781"}
        picked = pick_canonical_mention([a, b])
        self.assertNotEqual(picked["primary_authors"], [])
        self.assertEqual(picked["primary_authors"], ["Known Author"])
        self.assertEqual(picked["title"], "Book")

    def test_nonempty_preferred_empty_reversed(self):
        a = {"title": "Unknown listing", "primary_authors": [], "isbn": "9781"}
        b = {"title": "Book", "primary_authors": ["Known Author"], "isbn": "9781"}
        picked = pick_canonical_mention([a, b])
        self.assertNotEqual(picked["primary_authors"], [])
        self.assertEqual(picked["primary_authors"], ["Known Author"])

    def test_non_comma_display_preferred(self):
        a = {"title": "Book", "primary_authors": ["Robbins, Mel"], "isbn": ""}
        b = {"title": "Book", "primary_authors": ["Mel Robbins"], "isbn": ""}
        picked = pick_canonical_mention([a, b])
        self.assertEqual(picked["primary_authors"], ["Mel Robbins"])

    def test_bare_title_preferred(self):
        a = {"title": "Book: Subtitle", "primary_authors": ["Alpha"], "isbn": ""}
        b = {"title": "Book", "primary_authors": ["Alpha"], "isbn": ""}
        picked = pick_canonical_mention([a, b])
        self.assertEqual(picked["title"], "Book")

    def test_order_independent_both_directions(self):
        a = {"title": "Book", "primary_authors": ["Alpha"], "isbn": ""}
        b = {"title": "Book: Subtitle", "primary_authors": ["Robbins, Alpha"], "isbn": ""}
        picked_ab = pick_canonical_mention([a, b])
        picked_ba = pick_canonical_mention([b, a])
        self.assertEqual(picked_ab["title"], picked_ba["title"])
        self.assertEqual(picked_ab["primary_authors"], picked_ba["primary_authors"])

    def test_order_independent_full_fields_with_conflicting_metadata(self):
        """Issue 1: same normalized identity key but conflicting publisher/category.
        Canonical rank must be deterministic in both input orders and produce
        the same canonical title, authors, ISBN, publisher, category."""
        a = {
            "title": "Book",
            "primary_authors": ["Alpha"],
            "isbn": "9781",
            "publisher": "Press A",
            "candidate_category": "history",
        }
        b = {
            "title": "Book",
            "primary_authors": ["Alpha"],
            "isbn": "9781",
            "publisher": "Press B",
            "candidate_category": "science",
        }
        picked_ab = pick_canonical_mention([a, b])
        picked_ba = pick_canonical_mention([b, a])
        self.assertEqual(picked_ab["title"], picked_ba["title"])
        self.assertEqual(picked_ab["primary_authors"], picked_ba["primary_authors"])
        self.assertEqual(normalize_isbn(picked_ab.get("isbn", "")), normalize_isbn(picked_ba.get("isbn", "")))
        self.assertEqual(picked_ab.get("publisher"), picked_ba.get("publisher"))
        self.assertEqual(picked_ab.get("candidate_category"), picked_ba.get("candidate_category"))

    def test_mention_canonical_rank_public(self):
        """Issue 1: mention_canonical_rank is a public, importable function."""
        rank = mention_canonical_rank({"title": "X", "primary_authors": ["A"], "isbn": ""})
        self.assertIsInstance(rank, tuple)


class IdentityMergeUnitTests(unittest.TestCase):
    """A4, A5, A6: identity merge correctness."""

    def _make_mention(self, title, authors, isbn="", **kwargs):
        return {
            "title": title,
            "primary_authors": authors,
            "isbn": isbn,
            "source_name": kwargs.get("source_name", "Test"),
            "source_family": kwargs.get("source_family", "media"),
            "source_url": kwargs.get("source_url", "https://example.test/book"),
            "source_check_date": kwargs.get("source_check_date", "2026-07-14"),
            "signal_strength": kwargs.get("signal_strength", "minor mention"),
            "evidence_note": kwargs.get("evidence_note", "Test."),
        }

    def test_author_display_variants_merge(self):
        mentions = [
            self._make_mention("The Let Them Theory", ["Mel Robbins"], isbn="9781401971366"),
            self._make_mention("The Let Them Theory", ["Robbins, Mel"], isbn="9781401971366"),
        ]
        candidates, _ = build_scouting_candidates(mentions, "2026-07-14")
        self.assertEqual(len(candidates), 1)

    def test_title_subtitle_variants_merge(self):
        mentions = [
            self._make_mention("The Let Them Theory", ["Mel Robbins"]),
            self._make_mention("The Let Them Theory: Full", ["Mel Robbins"]),
        ]
        candidates, _ = build_scouting_candidates(mentions, "2026-07-14")
        self.assertEqual(len(candidates), 1)

    def test_shared_author_with_additional_contributor_merges(self):
        mentions = [
            self._make_mention("The Let Them Theory", ["Mel Robbins"]),
            self._make_mention("The Let Them Theory", ["Mel Robbins", "Sawyer Robbins"]),
        ]
        candidates, _ = build_scouting_candidates(mentions, "2026-07-14")
        self.assertEqual(len(candidates), 1)

    def test_overlapping_contributors_merge_on_any_shared_author(self):
        mentions = [
            self._make_mention("The Let Them Theory", ["Mel Robbins", "Alex Author"]),
            self._make_mention("The Let Them Theory", ["Mel Robbins", "Sawyer Robbins"]),
        ]
        candidates, _ = build_scouting_candidates(mentions, "2026-07-14")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(len(candidates[0].get("source_evidence", [])), 2)

    def test_empty_author_no_merge_with_same_title(self):
        """A3: empty contributor set on same title must NOT merge."""
        mentions = [
            self._make_mention("Same Title", ["Alpha"]),
            self._make_mention("Same Title", []),
        ]
        candidates, _ = build_scouting_candidates(mentions, "2026-07-14")
        self.assertEqual(len(candidates), 2)

    def test_late_isbn_connects_another_group(self):
        """A4: Book A/Alpha has blank ISBN, second has 978-1,
        Other Name/Beta has 9781 -> merge to one via ISBN."""
        mentions = [
            self._make_mention("Book A", ["Alpha"], isbn=""),
            self._make_mention("Book A", ["Alpha"], isbn="9781"),
            self._make_mention("Other Name", ["Beta"], isbn="9781"),
        ]
        candidates, _ = build_scouting_candidates(mentions, "2026-07-14")
        self.assertEqual(len(candidates), 1)

    def test_isbn_only_canonical_order_independent(self):
        """A5: Zulu/Zed and Alpha/A share 9781; order-independent result."""
        mentions_ab = [
            self._make_mention("Zulu", ["Zed"], isbn="9781"),
            self._make_mention("Alpha", ["A"], isbn="9781"),
        ]
        mentions_ba = list(reversed(mentions_ab))

        cand_ab, _ = build_scouting_candidates(mentions_ab, "2026-07-14")
        cand_ba, _ = build_scouting_candidates(mentions_ba, "2026-07-14")

        self.assertEqual(len(cand_ab), 1)
        self.assertEqual(len(cand_ba), 1)
        self.assertEqual(cand_ab[0].identity_key(), cand_ba[0].identity_key())
        self.assertEqual(cand_ab[0].get("title"), cand_ba[0].get("title"))
        self.assertEqual(normalize_isbn(cand_ab[0].get("isbn", "")), normalize_isbn(cand_ba[0].get("isbn", "")))

    def test_isbn_backfill(self):
        mentions = [
            self._make_mention("The Let Them Theory", ["Mel Robbins"]),
            self._make_mention("The Let Them Theory", ["Mel Robbins"], isbn="9781401971366"),
        ]
        candidates, _ = build_scouting_candidates(mentions, "2026-07-14")
        self.assertEqual(candidates[0].get("isbn"), "9781401971366")

    def test_isbn_no_overwrite_on_conflict(self):
        mentions = [
            self._make_mention("The Let Them Theory", ["Mel Robbins"], isbn="9781401971366"),
            self._make_mention("The Let Them Theory", ["Mel Robbins"], isbn="9789999999999"),
        ]
        candidates, _ = build_scouting_candidates(mentions, "2026-07-14")
        self.assertEqual(candidates[0].get("isbn"), "9781401971366")

    def test_source_evidence_preserved_across_merge(self):
        mentions = [
            self._make_mention("The Let Them Theory", ["Mel Robbins"], source_name="A", evidence_note="Note A."),
            self._make_mention("The Let Them Theory", ["Robbins, Mel"], source_name="B", evidence_note="Note B."),
        ]
        candidates, _ = build_scouting_candidates(mentions, "2026-07-14")
        self.assertEqual(len(candidates), 1)
        evidence = candidates[0].get("source_evidence")
        self.assertEqual(len(evidence), 2)

    def test_order_independence_mention_order(self):
        mentions_a = [
            self._make_mention("The Let Them Theory", ["Robbins, Mel"], isbn="9781401971366"),
            self._make_mention("The Let Them Theory", ["Mel Robbins"], isbn="978-1-4019-7136-6"),
            self._make_mention("The Let Them Theory: Full", ["Mel Robbins", "Sawyer Robbins"], isbn="9781401971366"),
        ]
        mentions_b = list(reversed(mentions_a))
        cand_a, _ = build_scouting_candidates(mentions_a, "2026-07-14")
        cand_b, _ = build_scouting_candidates(mentions_b, "2026-07-14")
        self.assertEqual(len(cand_a), len(cand_b))
        self.assertEqual(cand_a[0].identity_key(), cand_b[0].identity_key())


class ExplicitHistoryArgTests(unittest.TestCase):
    """A1: build_scouting_candidates receives explicit candidate_history."""

    def test_history_passed_explicitly(self):
        mentions = [
            {
                "title": "The Let Them Theory",
                "primary_authors": ["Robbins, Mel"],
                "isbn": "9781401971366",
                "source_name": "S",
                "source_family": "media",
                "source_url": "https://example.test/a",
                "source_check_date": "2026-07-14",
                "signal_strength": "minor mention",
                "evidence_note": "Current.",
            }
        ]
        history = [
            {
                "identity_key": "the let them theory|mel robbins",
                "title": "The Let Them Theory",
                "primary_authors": ["Mel Robbins"],
                "isbn": "9781401971366",
                "first_seen_date": "2026-07-07",
            }
        ]
        candidates, _ = build_scouting_candidates(mentions, "2026-07-14", candidate_history=history)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].identity_key(), "the let them theory|mel robbins")


class RunPipelineIsbnPersistenceTests(unittest.TestCase):
    """A2, A10: ISBN persists to candidate history across weeks."""

    def _write_config(self, path, candidate_history=None, isbn=""):
        config = {
            "sources": [
                {
                    "name": "Fixture",
                    "family": "award",
                    "url": "https://example.test",
                    "access_status": "open",
                    "extraction_method": "fixture",
                    "check_frequency": "weekly",
                    "fixture_candidates": [
                        {
                            "title": "Book X",
                            "primary_authors": ["Alex Example"],
                            "isbn": isbn,
                            "source_url": "https://example.test/x",
                            "signal_strength": "shortlist",
                            "evidence_note": "Test.",
                            "candidate_category": "popular science",
                        }
                    ],
                }
            ],
        }
        if candidate_history is not None:
            config["candidate_history"] = candidate_history
        path.write_text(json.dumps(config), encoding="utf-8")

    def test_isbn_persisted_to_candidate_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            self._write_config(config_path, isbn="9781401971366")
            output_dir = tmp_path / "run"
            output_dir.mkdir()
            run_pipeline(config_path, output_dir)
            history = json.loads((output_dir / "candidate-history.json").read_text())
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0].get("isbn"), "9781401971366")

    def test_isbn_does_not_overwrite_conflicting_historical(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            existing_history = [
                {
                    "identity_key": "book x|alex example",
                    "title": "Book X",
                    "primary_authors": ["Alex Example"],
                    "isbn": "9789999999999",
                    "first_seen_date": "2026-07-01",
                    "last_seen_date": "2026-07-01",
                }
            ]
            config_path = tmp_path / "config.json"
            self._write_config(config_path, candidate_history=existing_history, isbn="9781401971366")
            output_dir = tmp_path / "run"
            output_dir.mkdir()
            run_pipeline(config_path, output_dir)
            history = json.loads((output_dir / "candidate-history.json").read_text())
            self.assertEqual(history[0].get("isbn"), "9789999999999")

    def test_isbn_backfills_blank_historical_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            existing_history = [
                {
                    "identity_key": "book x|alex example",
                    "title": "Book X",
                    "primary_authors": ["Alex Example"],
                    "first_seen_date": "2026-07-01",
                    "last_seen_date": "2026-07-01",
                }
            ]
            config_path = tmp_path / "config.json"
            self._write_config(config_path, candidate_history=existing_history, isbn="9781401971366")
            output_dir = tmp_path / "run"
            output_dir.mkdir()
            run_pipeline(config_path, output_dir)
            history = json.loads((output_dir / "candidate-history.json").read_text())
            self.assertEqual(history[0].get("isbn"), "9781401971366")

    def test_run_pipeline_with_separate_state_preserves_historical_id(self):
        """A10: output_dir != state_dir, historical canonical ID preserved."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            state_dir.mkdir()
            existing_history = [
                {
                    "identity_key": "the let them theory|mel robbins",
                    "title": "The Let Them Theory",
                    "primary_authors": ["Mel Robbins"],
                    "first_seen_date": "2026-07-01",
                    "last_seen_date": "2026-07-01",
                }
            ]
            seed_state(state_dir, candidate_history=existing_history)
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "Fixture",
                                "family": "award",
                                "url": "https://example.test",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "fixture_candidates": [
                                    {
                                        "title": "The Let Them Theory",
                                        "primary_authors": ["Robbins, Mel"],
                                        "source_url": "https://example.test/a",
                                        "signal_strength": "shortlist",
                                        "evidence_note": "Test.",
                                        "candidate_category": "popular science",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output_dir = tmp_path / "run"
            output_dir.mkdir()
            run_pipeline(config_path, output_dir, state_dir=state_dir)
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text())
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["identity_key"], "the let them theory|mel robbins")

    def test_reconciliation_preserves_unique_keys(self):
        """A7: two current candidates must not get the same identity key."""
        history = [
            {"identity_key": "canonical|alpha", "title": "Shared Title", "primary_authors": ["Alpha"], "isbn": "9781"}
        ]
        mentions = [
            {
                "title": "Shared Title",
                "primary_authors": ["Alpha"],
                "isbn": "9781",
                "source_name": "S1",
                "source_family": "media",
                "source_url": "https://example.test/1",
                "source_check_date": "2026-07-14",
                "signal_strength": "minor mention",
                "evidence_note": "A.",
            },
            {
                "title": "Shared Title",
                "primary_authors": ["Beta"],
                "isbn": "9781",
                "source_name": "S2",
                "source_family": "media",
                "source_url": "https://example.test/2",
                "source_check_date": "2026-07-14",
                "signal_strength": "minor mention",
                "evidence_note": "B.",
            },
        ]
        candidates, _ = build_scouting_candidates(mentions, "2026-07-14", candidate_history=history)
        # Both share ISBN 9781 -> merge to one group via ISBN.
        # After reconciliation, the canonical key is "canonical|alpha".
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].identity_key(), "canonical|alpha")


class GroupMentionsByIdentityUnitTests(unittest.TestCase):
    """Direct tests for _group_mentions_by_identity."""

    def _m(self, title, authors, isbn="", **kwargs):
        return {
            "title": title,
            "primary_authors": authors,
            "isbn": isbn,
            "source_name": kwargs.get("source_name", "Test"),
            "source_family": kwargs.get("source_family", "media"),
            "source_url": kwargs.get("source_url", "https://example.test/book"),
            "source_check_date": kwargs.get("source_check_date", "2026-07-14"),
            "signal_strength": kwargs.get("signal_strength", "minor mention"),
            "evidence_note": kwargs.get("evidence_note", "Test."),
        }

    def test_exact_match_one_group(self):
        mentions = [self._m("Book", ["Alpha"]), self._m("Book", ["Alpha"])]
        excl, groups = _group_mentions_by_identity(mentions, "2026-07-14")
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 2)

    def test_isbn_connects_divergent_titles(self):
        mentions = [
            self._m("Book A", ["Alpha"], isbn="9781"),
            self._m("Other Name", ["Beta"], isbn="9781"),
        ]
        excl, groups = _group_mentions_by_identity(mentions, "2026-07-14")
        self.assertEqual(len(groups), 1)

    def test_groups_sorted_by_earliest_index(self):
        mentions = [
            self._m("Zebra", ["Zed"]),
            self._m("Apple", ["Alpha"]),
            self._m("Banana", ["Beta"]),
        ]
        excl, groups = _group_mentions_by_identity(mentions, "2026-07-14")
        self.assertEqual(len(groups), 3)
        titles = [g[0]["title"] for g in groups]
        self.assertEqual(titles, ["Zebra", "Apple", "Banana"])

    def test_shared_author_bridge_is_order_independent(self):
        """The shared-author group is stable when mention order is reversed."""
        base = [
            self._m("Shared", ["Mel", "A"]),
            self._m("Shared", ["Mel"]),
            self._m("Shared", ["Mel", "B"]),
        ]
        cand_fwd, _ = build_scouting_candidates(base, "2026-07-14")
        cand_rev, _ = build_scouting_candidates(list(reversed(base)), "2026-07-14")
        keys_fwd = {c.identity_key() for c in cand_fwd}
        keys_rev = {c.identity_key() for c in cand_rev}
        self.assertEqual(keys_fwd, keys_rev)
        self.assertEqual(len(cand_fwd), 1)
        self.assertEqual(len(cand_rev), 1)

    def test_shared_author_chain_merges_three_mentions(self):
        """All three mentions share Mel and therefore merge transitively."""
        mentions = [
            self._m("Shared", ["Mel"]),
            self._m("Shared", ["Mel", "A"]),
            self._m("Shared", ["Mel", "A", "B"]),
        ]
        excl, groups = _group_mentions_by_identity(mentions, "2026-07-14")
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 3)

    def test_shared_author_bridge_merges_in_build_scouting_candidates(self):
        """End-to-end: three mentions sharing Mel form one identity group."""
        mentions = [
            self._m("Shared", ["Mel", "A"]),
            self._m("Shared", ["Mel"]),
            self._m("Shared", ["Mel", "B"]),
        ]
        candidates, _ = build_scouting_candidates(mentions, "2026-07-14")
        self.assertEqual(len(candidates), 1)
        self.assertEqual({c.identity_key() for c in candidates}, {"shared|mel"})

    def test_isbn_still_connects_divergent_authors(self):
        """Shared nonblank ISBN must still bridge mutually incompatible
        contributor sets — ISBN is strong independent evidence."""
        mentions = [
            self._m("Shared", ["Mel", "A"], isbn="9781"),
            self._m("Shared", ["Mel"], isbn="9781"),
            self._m("Shared", ["Mel", "B"], isbn="9781"),
        ]
        excl, groups = _group_mentions_by_identity(mentions, "2026-07-14")
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 3)

    def test_shared_author_connects_late_supplier_with_divergent_contributors(self):
        """A late ISBN-bearing mention still joins through shared authors."""
        mentions = [
            self._m("Shared", ["Mel", "A"]),
            self._m("Shared", ["Mel"]),
            self._m("Shared", ["Mel", "B"], isbn="9781"),
        ]
        excl, groups = _group_mentions_by_identity(mentions, "2026-07-14")
        self.assertEqual(len(groups), 1)


class BuildCandidateFromMentionGroupUnitTests(unittest.TestCase):
    """Direct unit tests for _build_candidate_from_mention_group."""

    def _m(self, title, authors, isbn="", **kwargs):
        return {
            "title": title,
            "primary_authors": authors,
            "isbn": isbn,
            "source_name": kwargs.get("source_name", "Test"),
            "source_family": kwargs.get("source_family", "media"),
            "source_url": kwargs.get("source_url", "https://example.test/book"),
            "source_check_date": kwargs.get("source_check_date", "2026-07-14"),
            "signal_strength": kwargs.get("signal_strength", "minor mention"),
            "evidence_note": kwargs.get("evidence_note", "Test."),
        }

    def test_isbn_backfill_from_later_mention(self):
        mentions = [self._m("Book", ["Alpha"]), self._m("Book", ["Alpha"], isbn="9781401971366")]
        key = normalized_identity_key(mentions[0])
        candidate = _build_candidate_from_mention_group(key, mentions)
        self.assertEqual(candidate.get("isbn"), "9781401971366")

    def test_isbn_no_overwrite_on_conflict(self):
        mentions = [self._m("Book", ["Alpha"], isbn="9781401971366"), self._m("Book", ["Alpha"], isbn="9789999999999")]
        key = normalized_identity_key(mentions[0])
        candidate = _build_candidate_from_mention_group(key, mentions)
        self.assertEqual(candidate.get("isbn"), "9781401971366")

    def test_evidence_from_all_mentions(self):
        mentions = [
            self._m("Book", ["Alpha"], source_name="S1", evidence_note="A."),
            self._m("Book", ["Alpha"], source_name="S2", evidence_note="B."),
        ]
        key = normalized_identity_key(mentions[0])
        candidate = _build_candidate_from_mention_group(key, mentions)
        self.assertEqual(len(candidate.get("source_evidence")), 2)


class CrossWeekHistoricalIdentityTests(unittest.TestCase):
    """A7, A10: cross-week reconciliation."""

    def _m(self, title, authors, isbn="", **kwargs):
        return {
            "title": title,
            "primary_authors": authors,
            "isbn": isbn,
            "source_name": kwargs.get("source_name", "Test"),
            "source_family": kwargs.get("source_family", "media"),
            "source_url": kwargs.get("source_url", "https://example.test/book"),
            "source_check_date": kwargs.get("source_check_date", "2026-07-14"),
            "signal_strength": kwargs.get("signal_strength", "minor mention"),
            "evidence_note": kwargs.get("evidence_note", "Test."),
        }

    def test_reconcile_to_compatible_historical_identity(self):
        mentions = [self._m("The Let Them Theory", ["Robbins, Mel"], isbn="9781401971366")]
        history = [
            {
                "identity_key": "the let them theory|mel robbins",
                "title": "The Let Them Theory",
                "primary_authors": ["Mel Robbins"],
                "isbn": "9781401971366",
                "first_seen_date": "2026-07-07",
            }
        ]
        candidates, _ = build_scouting_candidates(mentions, "2026-07-14", candidate_history=history)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].identity_key(), "the let them theory|mel robbins")
        self.assertEqual(candidates[0].get("title"), "The Let Them Theory")
        self.assertEqual(candidates[0].get("primary_authors"), ["Mel Robbins"])

    def test_no_reconcile_when_ambiguous(self):
        mentions = [self._m("The Let Them Theory", ["Mel Robbins"], isbn="9781401971366")]
        history = [
            {
                "identity_key": "the let them theory|mel robbins",
                "title": "The Let Them Theory",
                "primary_authors": ["Mel Robbins"],
                "isbn": "9781401971366",
            },
            {
                "identity_key": "the let them theory|mel robbins & sawyer robbins",
                "title": "The Let Them Theory",
                "primary_authors": ["Mel Robbins", "Sawyer Robbins"],
                "isbn": "9781401971366",
            },
        ]
        candidates, _ = build_scouting_candidates(mentions, "2026-07-14", candidate_history=history)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].identity_key(), "the let them theory|mel robbins")

    def test_isbn_match_overrides_divergent_title(self):
        """A7: matching ISBN alone is independently strong evidence."""
        mentions = [self._m("Some Odd Title", ["Alex Example"], isbn="9781401971366")]
        history = [
            {
                "identity_key": "the let them theory|mel robbins",
                "title": "The Let Them Theory",
                "primary_authors": ["Mel Robbins"],
                "isbn": "9781401971366",
            }
        ]
        candidates, _ = build_scouting_candidates(mentions, "2026-07-14", candidate_history=history)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].identity_key(), "the let them theory|mel robbins")

    def test_shared_author_merge_reconciles_to_historical_identity(self):
        """Shared-author grouping leaves one current candidate to reconcile."""
        history = [{"identity_key": "shared|mel", "title": "Shared", "primary_authors": ["Mel"], "isbn": ""}]
        mentions = [
            self._m("Shared", ["Mel", "A"]),
            self._m("Shared", ["Mel", "B"]),
        ]
        candidates, _ = build_scouting_candidates(mentions, "2026-07-14", candidate_history=history)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].identity_key(), "shared|mel")

    def test_shared_author_merge_reconciliation_is_order_independent(self):
        """Reconciliation remains stable when shared-author mentions are reversed."""
        history = [{"identity_key": "shared|mel", "title": "Shared", "primary_authors": ["Mel"], "isbn": ""}]
        base_mentions = [
            self._m("Shared", ["Mel", "A"]),
            self._m("Shared", ["Mel", "B"]),
        ]
        cand_ab, _ = build_scouting_candidates(base_mentions, "2026-07-14", candidate_history=history)
        cand_ba, _ = build_scouting_candidates(list(reversed(base_mentions)), "2026-07-14", candidate_history=history)
        self.assertEqual(len(cand_ab), 1)
        self.assertEqual(len(cand_ba), 1)
        keys_ab = {c.identity_key() for c in cand_ab}
        keys_ba = {c.identity_key() for c in cand_ba}
        self.assertEqual(keys_ab, {"shared|mel"})
        self.assertEqual(keys_ba, keys_ab)


class GuardedIdentityCorrectionPreservationTests(unittest.TestCase):
    def test_guarded_correction_preserves_history_and_feedback(self):
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            state_dir.mkdir()
            history = [
                {
                    "identity_key": "the let them theory|mel robbins",
                    "title": "The Let Them Theory",
                    "primary_authors": ["Mel Robbins"],
                },
                {
                    "identity_key": "the let them theory|robbins mel",
                    "title": "The Let Them Theory",
                    "primary_authors": ["Robbins, Mel"],
                    "editor_status": "interested",
                    "editor_note": "Keep",
                },
            ]
            seed_state(state_dir, candidate_history=history)
            seed_state(
                state_dir,
                candidate_visibility=[{"identity_key": "the let them theory|robbins mel", "visibility": "hidden"}],
            )
            correction = {
                "source_identity_keys": ["the let them theory|mel robbins", "the let them theory|robbins mel"],
                "canonical": {
                    "identity_key": "the let them theory|mel robbins",
                    "title": "The Let Them Theory",
                    "primary_authors": ["Mel Robbins"],
                },
            }
            correction_path = tmp_path / "correction.json"
            correction_path.write_text(json.dumps(correction), encoding="utf-8")
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "autoscout",
                    "merge-candidate-identity",
                    "--state-dir",
                    str(state_dir),
                    "--correction-file",
                    str(correction_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("identity_correction=applied", proc.stdout)
            new_history = read_state(state_dir, "candidate_history")
            self.assertEqual(len(new_history), 1)
            self.assertEqual(new_history[0]["identity_key"], "the let them theory|mel robbins")
            self.assertEqual(new_history[0]["editor_status"], "interested")
            vis = read_state(state_dir, "candidate_visibility")
            self.assertEqual(vis[0]["identity_key"], "the let them theory|mel robbins")
