import tempfile
import unittest
from pathlib import Path

from state_store_support import read_state

from autoscout.source_proposal_commands import record_source_proposal
from autoscout.source_proposals import merge_proposal_attempt
from autoscout.source_proposals import proposal_from_attempt


class SourceProposalTests(unittest.TestCase):
    def test_record_source_proposal_persists_expected_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)

            proposal, created = record_source_proposal(
                state_dir,
                source_url="https://www.example.test/lists/",
                proposed_by="agent",
                proposal_date="2026-06-12",
                reason="Repeatedly cited by configured source evidence.",
                supporting_evidence=[{"source_url": "https://configured.test/item"}],
                suggested_signal_family="media/curated list",
            )

            self.assertTrue(created)
            self.assertEqual(proposal["status"], "proposed")
            self.assertEqual(proposal["normalized_source"], "https://example.test/lists")
            persisted = read_state(state_dir, "source_proposals")
            self.assertEqual(persisted, [proposal])

    def test_record_source_proposal_persists_records_sorted_by_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)

            record_source_proposal(
                state_dir,
                source_url="https://z.example.test",
                proposed_by="editor",
                proposal_date="2026-06-12",
                reason="Later id.",
            )
            record_source_proposal(
                state_dir,
                source_url="https://a.example.test",
                proposed_by="editor",
                proposal_date="2026-06-12",
                reason="Earlier id.",
            )

            persisted = read_state(state_dir, "source_proposals")
            self.assertEqual(
                [proposal["id"] for proposal in persisted],
                ["source-a-example-test", "source-z-example-test"],
            )

    def test_duplicate_proposal_attempt_merges_instead_of_duplicating(self):
        existing = proposal_from_attempt(
            source_url="https://www.example.test/source/",
            proposed_by="agent",
            proposal_date="2026-06-12",
            reason="First reason.",
            supporting_evidence=[{"candidate": "A"}],
        )
        attempt = proposal_from_attempt(
            source_url="https://example.test/source",
            proposed_by="editor",
            proposal_date="2026-06-13",
            reason="Second reason.",
            supporting_evidence=[{"candidate": "B"}],
            editor_endorsement={"value": "endorsed", "note": "Known source."},
        )

        proposals, proposal, created = merge_proposal_attempt([existing], attempt)

        self.assertFalse(created)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposal["proposed_by"], "agent; editor")
        self.assertEqual(proposal["reason"], "First reason.; Second reason.")
        self.assertEqual(proposal["supporting_evidence"], [{"candidate": "A"}, {"candidate": "B"}])
        self.assertEqual(proposal["editor_endorsement"], {"value": "endorsed", "note": "Known source."})

    def test_rejected_or_infeasible_proposals_suppress_duplicate_attempts(self):
        for status in ("rejected", "infeasible"):
            with self.subTest(status=status):
                existing = proposal_from_attempt(
                    source_url="https://example.test/source",
                    proposed_by="agent",
                    reason="Bad idea.",
                    status=status,
                    decision_reason="Not usable.",
                )
                attempt = proposal_from_attempt(
                    source_url="https://www.example.test/source/",
                    proposed_by="editor",
                    reason="Try again.",
                )

                proposals, proposal, created = merge_proposal_attempt([existing], attempt)

                self.assertFalse(created)
                self.assertEqual(proposals, [existing])
                self.assertEqual(proposal["decision_reason"], "Not usable.")
