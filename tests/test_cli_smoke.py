import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from state_store_support import read_run_output
from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_snapshot

from autoscout.candidate_identity import candidate_id_for
from autoscout.json_io import write_json
from autoscout.state import load_state
from autoscout.state import save_state


def run_autoscout(*args):
    return subprocess.run(
        [sys.executable, "-m", "autoscout", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def write_fixture_config(path, fixture_candidates, **extra):
    path.write_text(
        json.dumps(
            {
                "run_date": "2026-05-21",
                "sources": [
                    {
                        "name": "Fixture Awards",
                        "family": "award",
                        "url": "https://example.test/awards",
                        "access_status": "open",
                        "extraction_method": "fixture",
                        "check_frequency": "weekly",
                        "language": "en",
                        "fixture_candidates": fixture_candidates,
                    }
                ],
                **extra,
            }
        ),
        encoding="utf-8",
    )


class CliSmokeTests(unittest.TestCase):
    def test_run_mvp_writes_expected_artifact_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "weekly-run"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "autoscout",
                    "run-mvp",
                    "--config",
                    "configs/weekly-mvp-fixture.json",
                    "--output-dir",
                    str(output_dir),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                {path.name for path in output_dir.iterdir() if path.is_file()},
                {
                    "source-mentions.json",
                    "report-qa.json",
                    "editor-facing.csv",
                    "russian-review-queue.csv",
                    "scouting-candidates.json",
                    "candidate-history.json",
                    "proposed-corrections.json",
                    "memo-queue.json",
                    "source-proposals.json",
                    "suppressed-candidates.json",
                    "hidden-by-rule.json",
                    "internal-exclusions.json",
                    "internal-pre-suppression-candidates.json",
                    "russian-edition-checks.json",
                    "source-runs.json",
                    "source-metrics.json",
                    "source-review.md",
                    "derived-rule-suggestions.json",
                    "derived-rule-suggestions.md",
                    "run-summary.json",
                },
            )
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertGreater(summary["editor_facing_count"], 0)
            self.assertIn("major_warnings", summary)

    def test_run_mvp_preserves_source_proposals_without_collecting_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "weekly-run"
            state_dir = tmp_path / "state"
            state_dir.mkdir()
            seed_state(
                state_dir,
                source_proposals=[
                    {
                        "id": "source-proposed-test",
                        "source_url": "https://proposed.test/list",
                        "normalized_source": "https://proposed.test/list",
                        "proposed_by": "agent",
                        "proposal_date": "2026-06-12",
                        "reason": "Repeatedly cited.",
                        "supporting_evidence": [],
                        "suggested_signal_family": "media/curated list",
                        "editor_endorsement": None,
                        "status": "proposed",
                        "decision_reason": "",
                    }
                ],
            )
            config_path = tmp_path / "sources.json"
            write_fixture_config(config_path, [])

            result = run_autoscout(
                "run-mvp",
                "--config",
                str(config_path),
                "--output-dir",
                str(output_dir),
                "--state-dir",
                str(state_dir),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            source_runs = json.loads((output_dir / "source-runs.json").read_text())
            self.assertEqual([run["url"] for run in source_runs], ["https://example.test/awards"])
            proposals = read_state(state_dir, "source_proposals")
            self.assertEqual(proposals[0]["source_url"], "https://proposed.test/list")

    def test_source_proposal_add_records_manual_proposal_without_collecting_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            config_path = tmp_path / "sources.json"
            write_fixture_config(config_path, [])

            result = run_autoscout(
                "source-proposal",
                "add",
                "--source-url",
                "https://www.scandinavian.test/nonfiction/",
                "--name",
                "Scandinavian Nonfiction",
                "--proposed-by",
                "agent; editor",
                "--reason",
                "Requested during Scandinavian source research.",
                "--signal-family",
                "media/curated list",
                "--access-status",
                "open",
                "--caveats",
                "Needs adult nonfiction fit check.",
                "--state-dir",
                str(state_dir),
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("source_proposal_created=true", result.stdout)
            self.assertIn("configured_source_added=false", result.stdout)
            proposals = read_state(state_dir, "source_proposals")
            self.assertEqual(len(proposals), 1)
            self.assertEqual(proposals[0]["source_name"], "Scandinavian Nonfiction")
            self.assertEqual(proposals[0]["access_status"], "open")
            self.assertEqual(proposals[0]["caveats"], "Needs adult nonfiction fit check.")
            self.assertEqual(proposals[0]["suggested_signal_family"], "media/curated list")

            output_dir = tmp_path / "weekly-run"
            run = run_autoscout(
                "run-mvp",
                "--config",
                str(config_path),
                "--output-dir",
                str(output_dir),
                "--state-dir",
                str(state_dir),
            )

            self.assertEqual(run.returncode, 0, run.stderr)
            source_runs = json.loads((output_dir / "source-runs.json").read_text())
            self.assertEqual([source["url"] for source in source_runs], ["https://example.test/awards"])
            run_proposals = json.loads((output_dir / "source-proposals.json").read_text())
            self.assertEqual(run_proposals[0]["source_url"], "https://www.scandinavian.test/nonfiction/")

    def test_source_proposal_add_merges_duplicate_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            first = run_autoscout(
                "source-proposal",
                "add",
                "--source-url",
                "https://www.ideas.test/list/",
                "--reason",
                "First reason.",
                "--state-dir",
                str(state_dir),
            )
            second = run_autoscout(
                "source-proposal",
                "add",
                "--source-url",
                "https://ideas.test/list",
                "--reason",
                "Second reason.",
                "--caveats",
                "Needs stability check.",
                "--state-dir",
                str(state_dir),
            )

            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertIn("source_proposal_created=false", second.stdout)
            proposals = read_state(state_dir, "source_proposals")
            self.assertEqual(len(proposals), 1)
            self.assertEqual(proposals[0]["reason"], "First reason.; Second reason.")
            self.assertEqual(proposals[0]["caveats"], "Needs stability check.")

    def test_ingest_feedback_source_ideas_creates_editor_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            feedback_csv = tmp_path / "feedback.csv"
            feedback_csv.write_text(
                "Candidate ID,Editor Status,Editor Note\n",
                encoding="utf-8",
            )
            source_ideas_csv = tmp_path / "source-ideas.csv"
            source_ideas_csv.write_text(
                "Source URL,Reason,Suggested signal family\n"
                "https://ideas.test/list,Editor knows this list,media/curated list\n",
                encoding="utf-8",
            )

            result = run_autoscout(
                "ingest-feedback",
                "--csv",
                str(feedback_csv),
                "--source-ideas-csv",
                str(source_ideas_csv),
                "--state-dir",
                str(state_dir),
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("source_idea_rows=1", result.stdout)
            proposals = read_state(state_dir, "source_proposals")
            self.assertEqual(len(proposals), 1)
            self.assertEqual(proposals[0]["proposed_by"], "editor")
            self.assertEqual(proposals[0]["reason"], "Editor knows this list")
            self.assertEqual(proposals[0]["suggested_signal_family"], "media/curated list")

    def test_ingest_feedback_source_ideas_records_editor_input_on_existing_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            state_dir.mkdir()
            seed_state(
                state_dir,
                source_proposals=[
                    {
                        "id": "source-ideas-test-list",
                        "source_url": "https://ideas.test/list",
                        "normalized_source": "https://ideas.test/list",
                        "proposed_by": "agent",
                        "proposal_date": "2026-06-12",
                        "reason": "Repeatedly cited.",
                        "supporting_evidence": [{"candidate": "A"}],
                        "suggested_signal_family": "media/curated list",
                        "editor_endorsement": None,
                        "status": "proposed",
                        "decision_reason": "",
                    }
                ],
            )
            feedback_csv = tmp_path / "feedback.csv"
            feedback_csv.write_text("Candidate ID,Editor Status,Editor Note\n", encoding="utf-8")
            source_ideas_csv = tmp_path / "source-ideas.csv"
            source_ideas_csv.write_text(
                "Source URL,Reason,Editor endorsement,Editor note\n"
                "https://www.ideas.test/list/,Recognized by editor,Dismiss,Too narrow\n",
                encoding="utf-8",
            )

            result = run_autoscout(
                "ingest-feedback",
                "--csv",
                str(feedback_csv),
                "--source-ideas-csv",
                str(source_ideas_csv),
                "--state-dir",
                str(state_dir),
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            proposals = read_state(state_dir, "source_proposals")
            self.assertEqual(len(proposals), 1)
            self.assertEqual(proposals[0]["status"], "proposed")
            self.assertEqual(proposals[0]["proposed_by"], "agent; editor")
            self.assertEqual(
                proposals[0]["editor_endorsement"],
                {"value": "dismissed", "note": "Too narrow"},
            )

    def test_ingest_feedback_with_source_ideas_preserves_candidate_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            state_dir.mkdir()
            seed_state(state_dir, latest_scouting_candidates=[{"identity_key": "known-book|alex"}])
            feedback_csv = tmp_path / "feedback.csv"
            feedback_csv.write_text(
                "Candidate ID,Editor Status,Editor Note\nknown-book|alex,Interested,Keep watching\n",
                encoding="utf-8",
            )
            source_ideas_csv = tmp_path / "source-ideas.csv"
            source_ideas_csv.write_text(
                "Source URL,Reason\nhttps://ideas.test/list,Good list\n",
                encoding="utf-8",
            )

            result = run_autoscout(
                "ingest-feedback",
                "--csv",
                str(feedback_csv),
                "--source-ideas-csv",
                str(source_ideas_csv),
                "--state-dir",
                str(state_dir),
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            pending = read_state(state_dir, "pending_editor_rows")
            self.assertEqual(pending[0]["identity_key"], "known-book|alex")
            self.assertEqual(pending[0]["Editor Status"], "interested")

    def test_run_mvp_creates_agent_source_proposal_from_repeated_collected_references(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            output_dir = tmp_path / "weekly-run"
            config_path = tmp_path / "sources.json"
            write_fixture_config(
                config_path,
                [
                    {
                        "title": "First Book",
                        "author": "Alex Example",
                        "source_url": "https://configured.test/first",
                        "evidence_note": "Configured evidence cites a useful outside list.",
                        "russian_edition_status": "not found in Russian",
                        "referenced_source_urls": ["https://outside.test/list"],
                    },
                    {
                        "title": "Second Book",
                        "author": "Bea Example",
                        "source_url": "https://configured.test/second",
                        "evidence_note": "Configured evidence also cites the outside list.",
                        "russian_edition_status": "not found in Russian",
                        "referenced_source_urls": ["https://www.outside.test/list/"],
                    },
                ],
            )

            result = run_autoscout(
                "run-mvp",
                "--config",
                str(config_path),
                "--output-dir",
                str(output_dir),
                "--state-dir",
                str(state_dir),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            proposals = read_state(state_dir, "source_proposals")
            self.assertEqual(len(proposals), 1)
            self.assertEqual(proposals[0]["proposed_by"], "agent")
            self.assertEqual(proposals[0]["source_url"], "https://outside.test/list")
            self.assertEqual(len(proposals[0]["supporting_evidence"]), 2)
            self.assertIn("already-collected evidence", proposals[0]["reason"])
            run_proposals = json.loads((output_dir / "source-proposals.json").read_text())
            self.assertEqual(run_proposals, proposals)
            summary = json.loads((output_dir / "run-summary.json").read_text())
            self.assertEqual(summary["pending_source_proposal_count"], 1)
            self.assertEqual(summary["new_source_proposal_count"], 1)

    def test_run_mvp_does_not_create_agent_source_proposal_from_single_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            output_dir = tmp_path / "weekly-run"
            config_path = tmp_path / "sources.json"
            write_fixture_config(
                config_path,
                [
                    {
                        "title": "First Book",
                        "author": "Alex Example",
                        "source_url": "https://configured.test/first",
                        "evidence_note": "Configured evidence cites a useful outside list.",
                        "russian_edition_status": "not found in Russian",
                        "referenced_source_urls": ["https://outside.test/list"],
                    }
                ],
            )

            result = run_autoscout(
                "run-mvp",
                "--config",
                str(config_path),
                "--output-dir",
                str(output_dir),
                "--state-dir",
                str(state_dir),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            proposals = read_state(state_dir, "source_proposals")
            self.assertEqual(proposals, [])

    def test_run_mvp_keeps_rejected_source_proposal_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            state_dir.mkdir()
            seed_state(
                state_dir,
                source_proposals=[
                    {
                        "id": "source-outside-test-list",
                        "source_url": "https://outside.test/list",
                        "normalized_source": "https://outside.test/list",
                        "proposed_by": "agent",
                        "proposal_date": "2026-06-12",
                        "reason": "Not useful.",
                        "supporting_evidence": [],
                        "suggested_signal_family": "media/curated list",
                        "editor_endorsement": None,
                        "status": "rejected",
                        "decision_reason": "Too broad.",
                    }
                ],
            )
            output_dir = tmp_path / "weekly-run"
            config_path = tmp_path / "sources.json"
            write_fixture_config(
                config_path,
                [
                    {
                        "title": "First Book",
                        "author": "Alex Example",
                        "source_url": "https://configured.test/first",
                        "evidence_note": "Configured evidence cites outside list.",
                        "russian_edition_status": "not found in Russian",
                        "referenced_source_urls": ["https://outside.test/list"],
                    },
                    {
                        "title": "Second Book",
                        "author": "Bea Example",
                        "source_url": "https://configured.test/second",
                        "evidence_note": "Configured evidence also cites outside list.",
                        "russian_edition_status": "not found in Russian",
                        "referenced_source_urls": ["https://outside.test/list"],
                    },
                ],
            )

            result = run_autoscout(
                "run-mvp",
                "--config",
                str(config_path),
                "--output-dir",
                str(output_dir),
                "--state-dir",
                str(state_dir),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            proposals = read_state(state_dir, "source_proposals")
            self.assertEqual(len(proposals), 1)
            self.assertEqual(proposals[0]["status"], "rejected")
            self.assertEqual(proposals[0]["supporting_evidence"], [])

    def test_source_proposal_accept_writes_agent_extraction_config_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            state_dir.mkdir()
            proposal = {
                "id": "source-outside-test-list",
                "source_url": "https://outside.test/list",
                "normalized_source": "https://outside.test/list",
                "proposed_by": "agent",
                "proposal_date": "2026-06-12",
                "reason": "Repeatedly cited.",
                "supporting_evidence": [],
                "suggested_signal_family": "media/curated list",
                "editor_endorsement": None,
                "status": "proposed",
                "decision_reason": "",
            }
            seed_state(state_dir, source_proposals=[proposal])
            config_path = tmp_path / "sources.json"
            config_path.write_text(json.dumps({"sources": []}), encoding="utf-8")

            result = run_autoscout(
                "source-proposal",
                "accept",
                "--id",
                "source-outside-test-list",
                "--state-dir",
                str(state_dir),
                "--config",
                str(config_path),
                "--name",
                "Outside List",
                "--language",
                "en",
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            config = json.loads(config_path.read_text())
            self.assertEqual(
                config["sources"][0],
                {
                    "name": "Outside List",
                    "family": "media/curated list",
                    "url": "https://outside.test/list",
                    "access_status": "open",
                    "extraction_method": "agent-extraction",
                    "check_frequency": "weekly",
                    "language": "en",
                    "lifecycle_status": "probation",
                    "lifecycle_note": "Newly accepted source proposal; review source metrics before promote or retire.",
                },
            )
            proposals = read_state(state_dir, "source_proposals")
            self.assertEqual(proposals[0]["status"], "accepted")

    def test_source_proposal_accept_uses_proposal_name_when_accept_name_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            state_dir.mkdir()
            proposal = {
                "id": "source-scandinavian-nonfiction-test",
                "source_url": "https://scandinavian.test/nonfiction",
                "normalized_source": "https://scandinavian.test/nonfiction",
                "proposed_by": "agent",
                "proposal_date": "2026-06-27",
                "reason": "Manual research source.",
                "supporting_evidence": [],
                "suggested_signal_family": "media/curated list",
                "source_name": "Scandinavian Nonfiction",
                "access_status": "open",
                "caveats": "Needs adult nonfiction fit check.",
                "editor_endorsement": None,
                "status": "proposed",
                "decision_reason": "",
            }
            seed_state(state_dir, source_proposals=[proposal])
            config_path = tmp_path / "sources.json"
            config_path.write_text(json.dumps({"sources": []}), encoding="utf-8")

            result = run_autoscout(
                "source-proposal",
                "accept",
                "--id",
                "source-scandinavian-nonfiction-test",
                "--state-dir",
                str(state_dir),
                "--config",
                str(config_path),
                "--language",
                "en",
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            config = json.loads(config_path.read_text())
            self.assertEqual(config["sources"][0]["name"], "Scandinavian Nonfiction")

    def test_source_proposal_accept_does_not_duplicate_existing_config_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            state_dir.mkdir()
            seed_state(
                state_dir,
                source_proposals=[
                    {
                        "id": "source-outside-test-list",
                        "source_url": "https://outside.test/list",
                        "normalized_source": "https://outside.test/list",
                        "proposed_by": "agent",
                        "proposal_date": "2026-06-12",
                        "reason": "Repeatedly cited.",
                        "supporting_evidence": [],
                        "suggested_signal_family": "media/curated list",
                        "editor_endorsement": None,
                        "status": "proposed",
                        "decision_reason": "",
                    }
                ],
            )
            config_path = tmp_path / "sources.json"
            # Use deliberately non-sorted key order that would be reordered by write_json.
            config_path.write_text(
                "{\n"
                '  "run_date": "2026-05-21",\n'
                '  "sources": [\n'
                "    {\n"
                '      "check_frequency": "weekly",\n'
                '      "extraction_method": "agent-extraction",\n'
                '      "family": "media/curated list",\n'
                '      "url": "https://www.outside.test/list/",\n'
                '      "name": "Existing Outside List",\n'
                '      "access_status": "open"\n'
                "    }\n"
                "  ]\n"
                "}",
                encoding="utf-8",
            )
            config_bytes_before = config_path.read_bytes()

            result = run_autoscout(
                "source-proposal",
                "accept",
                "--id",
                "source-outside-test-list",
                "--state-dir",
                str(state_dir),
                "--config",
                str(config_path),
                "--language",
                "en",
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                config_path.read_bytes(),
                config_bytes_before,
                "Config file should be byte-identical when URL is already configured",
            )
            config = json.loads(config_path.read_text())
            self.assertEqual(len(config["sources"]), 1)
            proposals = read_state(state_dir, "source_proposals")
            self.assertEqual(proposals[0]["status"], "accepted")

    def test_source_proposal_reject_requires_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            state_dir.mkdir()
            seed_state(
                state_dir,
                source_proposals=[
                    {
                        "id": "source-outside-test-list",
                        "source_url": "https://outside.test/list",
                        "normalized_source": "https://outside.test/list",
                        "proposed_by": "agent",
                        "proposal_date": "2026-06-12",
                        "reason": "Repeatedly cited.",
                        "supporting_evidence": [],
                        "suggested_signal_family": "media/curated list",
                        "editor_endorsement": None,
                        "status": "proposed",
                        "decision_reason": "",
                    }
                ],
            )

            result = run_autoscout(
                "source-proposal",
                "reject",
                "--id",
                "source-outside-test-list",
                "--state-dir",
                str(state_dir),
                "--reason",
                "",
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("reject_reason_required", result.stdout)

    def test_source_proposal_reject_suppresses_future_duplicate_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            state_dir.mkdir()
            seed_state(
                state_dir,
                source_proposals=[
                    {
                        "id": "source-outside-test-list",
                        "source_url": "https://outside.test/list",
                        "normalized_source": "https://outside.test/list",
                        "proposed_by": "agent",
                        "proposal_date": "2026-06-12",
                        "reason": "Repeatedly cited.",
                        "supporting_evidence": [],
                        "suggested_signal_family": "media/curated list",
                        "editor_endorsement": None,
                        "status": "proposed",
                        "decision_reason": "",
                    }
                ],
            )

            reject = run_autoscout(
                "source-proposal",
                "reject",
                "--id",
                "source-outside-test-list",
                "--state-dir",
                str(state_dir),
                "--reason",
                "Too broad",
            )
            self.assertEqual(reject.returncode, 0, reject.stdout + reject.stderr)
            config_path = tmp_path / "sources.json"
            write_fixture_config(
                config_path,
                [
                    {
                        "title": "First Book",
                        "author": "Alex Example",
                        "source_url": "https://configured.test/first",
                        "evidence_note": "Configured evidence cites outside list.",
                        "russian_edition_status": "not found in Russian",
                        "referenced_source_urls": ["https://outside.test/list"],
                    },
                    {
                        "title": "Second Book",
                        "author": "Bea Example",
                        "source_url": "https://configured.test/second",
                        "evidence_note": "Configured evidence also cites outside list.",
                        "russian_edition_status": "not found in Russian",
                        "referenced_source_urls": ["https://outside.test/list"],
                    },
                ],
            )

            run = run_autoscout(
                "run-mvp",
                "--config",
                str(config_path),
                "--output-dir",
                str(tmp_path / "weekly-run"),
                "--state-dir",
                str(state_dir),
            )

            self.assertEqual(run.returncode, 0, run.stderr)
            proposals = read_state(state_dir, "source_proposals")
            self.assertEqual(len(proposals), 1)
            self.assertEqual(proposals[0]["status"], "rejected")
            self.assertEqual(proposals[0]["decision_reason"], "Too broad")

    def test_source_proposal_accept_unsuitable_source_writes_skipped_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            state_dir.mkdir()
            seed_state(
                state_dir,
                source_proposals=[
                    {
                        "id": "source-paid-test-list",
                        "source_url": "https://paid.test/list",
                        "normalized_source": "https://paid.test/list",
                        "proposed_by": "editor",
                        "proposal_date": "2026-06-12",
                        "reason": "Good paid source.",
                        "supporting_evidence": [],
                        "suggested_signal_family": "rights/deal",
                        "editor_endorsement": None,
                        "status": "proposed",
                        "decision_reason": "",
                    }
                ],
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text(json.dumps({"sources": []}), encoding="utf-8")

            result = run_autoscout(
                "source-proposal",
                "accept",
                "--id",
                "source-paid-test-list",
                "--state-dir",
                str(state_dir),
                "--config",
                str(config_path),
                "--access-status",
                "paid",
                "--access-reason",
                "Subscription required",
                "--language",
                "en",
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            config = json.loads(config_path.read_text())
            self.assertEqual(config["sources"][0]["access_status"], "paid")
            self.assertEqual(config["sources"][0]["notes"], "Subscription required")
            proposals = read_state(state_dir, "source_proposals")
            self.assertEqual(proposals[0]["status"], "infeasible")
            self.assertEqual(proposals[0]["decision_reason"], "Subscription required")

    def test_accepted_unsuitable_source_is_reported_as_skipped_source_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            state_dir.mkdir()
            seed_state(
                state_dir,
                source_proposals=[
                    {
                        "id": "source-paid-test-list",
                        "source_url": "https://paid.test/list",
                        "normalized_source": "https://paid.test/list",
                        "proposed_by": "editor",
                        "proposal_date": "2026-06-12",
                        "reason": "Good paid source.",
                        "supporting_evidence": [],
                        "suggested_signal_family": "rights/deal",
                        "editor_endorsement": None,
                        "status": "proposed",
                        "decision_reason": "",
                    }
                ],
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text(json.dumps({"run_date": "2026-05-21", "sources": []}), encoding="utf-8")
            accept = run_autoscout(
                "source-proposal",
                "accept",
                "--id",
                "source-paid-test-list",
                "--state-dir",
                str(state_dir),
                "--config",
                str(config_path),
                "--access-status",
                "paid",
                "--access-reason",
                "Subscription required",
                "--language",
                "en",
            )
            self.assertEqual(accept.returncode, 0, accept.stdout + accept.stderr)

            run = run_autoscout(
                "run-mvp",
                "--config",
                str(config_path),
                "--output-dir",
                str(tmp_path / "weekly-run"),
                "--state-dir",
                str(state_dir),
            )

            self.assertEqual(run.returncode, 0, run.stderr)
            source_runs = json.loads((tmp_path / "weekly-run" / "source-runs.json").read_text())
            self.assertEqual(source_runs[0]["status"], "skipped")
            self.assertEqual(source_runs[0]["reason"], "paid: Subscription required")

    def test_infeasible_source_proposal_suppresses_future_duplicate_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            state_dir.mkdir()
            seed_state(
                state_dir,
                source_proposals=[
                    {
                        "id": "source-outside-test-list",
                        "source_url": "https://outside.test/list",
                        "normalized_source": "https://outside.test/list",
                        "proposed_by": "agent",
                        "proposal_date": "2026-06-12",
                        "reason": "Paid source.",
                        "supporting_evidence": [],
                        "suggested_signal_family": "media/curated list",
                        "editor_endorsement": None,
                        "status": "infeasible",
                        "decision_reason": "Blocked by access controls.",
                    }
                ],
            )
            config_path = tmp_path / "sources.json"
            write_fixture_config(
                config_path,
                [
                    {
                        "title": "First Book",
                        "author": "Alex Example",
                        "source_url": "https://configured.test/first",
                        "evidence_note": "Configured evidence cites outside list.",
                        "russian_edition_status": "not found in Russian",
                        "referenced_source_urls": ["https://outside.test/list"],
                    },
                    {
                        "title": "Second Book",
                        "author": "Bea Example",
                        "source_url": "https://configured.test/second",
                        "evidence_note": "Configured evidence also cites outside list.",
                        "russian_edition_status": "not found in Russian",
                        "referenced_source_urls": ["https://outside.test/list"],
                    },
                ],
            )

            result = run_autoscout(
                "run-mvp",
                "--config",
                str(config_path),
                "--output-dir",
                str(tmp_path / "weekly-run"),
                "--state-dir",
                str(state_dir),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            proposals = read_state(state_dir, "source_proposals")
            self.assertEqual(len(proposals), 1)
            self.assertEqual(proposals[0]["status"], "infeasible")
            self.assertEqual(proposals[0]["supporting_evidence"], [])

    def test_run_mvp_with_agent_extraction_replay_source_writes_standard_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            page_path = tmp_path / "page.json"
            page_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": '<a href="https://example.test/books/agent-book">Agent Book</a> by Alex Example',
                    }
                ),
                encoding="utf-8",
            )
            extraction_path = tmp_path / "extraction.json"
            extraction_path.write_text(
                json.dumps(
                    {
                        "mentions": [
                            {
                                "title": "Agent Book",
                                "primary_authors": ["Alex Example"],
                                "source_url": "https://example.test/books/agent-book",
                                "source_check_date": "2026-05-21",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Agent extraction found this metadata-only source mention.",
                                "russian_edition_status": "not found in Russian",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-21",
                        "editor_row_target": 1,
                        "sources": [
                            {
                                "name": "Agent Source",
                                "family": "media/curated list",
                                "url": "https://example.test/list",
                                "access_status": "open",
                                "extraction_method": "agent-extraction-replay",
                                "check_frequency": "weekly",
                                "language": "en",
                                "source_page_fixture_file": str(page_path),
                                "extraction_response_file": str(extraction_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output_dir = tmp_path / "weekly-run"

            result = run_autoscout(
                "run-mvp",
                "--config",
                str(config_path),
                "--output-dir",
                str(output_dir),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                {path.name for path in output_dir.iterdir() if path.is_file()},
                {
                    "source-mentions.json",
                    "report-qa.json",
                    "editor-facing.csv",
                    "russian-review-queue.csv",
                    "scouting-candidates.json",
                    "candidate-history.json",
                    "proposed-corrections.json",
                    "memo-queue.json",
                    "source-proposals.json",
                    "suppressed-candidates.json",
                    "hidden-by-rule.json",
                    "internal-exclusions.json",
                    "internal-pre-suppression-candidates.json",
                    "russian-edition-checks.json",
                    "source-runs.json",
                    "source-metrics.json",
                    "source-review.md",
                    "derived-rule-suggestions.json",
                    "derived-rule-suggestions.md",
                    "run-summary.json",
                },
            )
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["Title"] for row in rows], ["Agent Book"])
            source_runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
            self.assertEqual(source_runs[0]["debug_metadata"]["collector_kind"], "agent-extraction")
            self.assertEqual(source_runs[0]["extracted_mention_count"], 1)

    def test_run_mvp_with_live_agent_extraction_fake_backend_captures_replay_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            page_path = tmp_path / "live-page.json"
            page_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": '<a href="https://example.test/books/live-agent-book">Live Agent Book</a>',
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            live_output = tmp_path / "live-run"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-21",
                        "editor_row_target": 1,
                        "sources": [
                            {
                                "name": "Live Agent Source",
                                "family": "media/curated list",
                                "url": "https://example.test/list",
                                "access_status": "open",
                                "extraction_method": "agent-extraction-live",
                                "extraction_backend": "fake",
                                "enable_live": True,
                                "check_frequency": "weekly",
                                "language": "en",
                                "source_page_fixture_file": str(page_path),
                                "capture_live_response_file": str(tmp_path / "captured-page.json"),
                                "capture_extraction_response_file": str(tmp_path / "captured-extraction.json"),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = run_autoscout("run-mvp", "--config", str(config_path), "--output-dir", str(live_output))

            self.assertEqual(result.returncode, 0, result.stderr)
            source_runs = json.loads((live_output / "source-runs.json").read_text(encoding="utf-8"))
            self.assertEqual(source_runs[0]["collection_mode"], "fake-live")
            self.assertEqual(source_runs[0]["extracted_mention_count"], 1)
            summary = json.loads((live_output / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["collection_mode"], "fake-live")
            self.assertTrue((tmp_path / "captured-page.json").exists())
            self.assertTrue((tmp_path / "captured-extraction.json").exists())

            replay_config_path = tmp_path / "replay-sources.json"
            replay_config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-21",
                        "editor_row_target": 1,
                        "sources": [
                            {
                                "name": "Live Agent Source",
                                "family": "media/curated list",
                                "url": "https://example.test/list",
                                "access_status": "open",
                                "extraction_method": "agent-extraction-replay",
                                "check_frequency": "weekly",
                                "language": "en",
                                "source_page_fixture_file": str(tmp_path / "captured-page.json"),
                                "extraction_response_file": str(tmp_path / "captured-extraction.json"),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            replay_output = tmp_path / "replay-run"

            replay_result = run_autoscout(
                "run-mvp", "--config", str(replay_config_path), "--output-dir", str(replay_output)
            )

            self.assertEqual(replay_result.returncode, 0, replay_result.stderr)
            live_mentions = json.loads((live_output / "source-mentions.json").read_text(encoding="utf-8"))
            replay_mentions = json.loads((replay_output / "source-mentions.json").read_text(encoding="utf-8"))
            self.assertEqual(replay_mentions, live_mentions)

    def test_run_mvp_live_agent_extraction_failures_degrade_to_source_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            page_path = tmp_path / "page.json"
            page_path.write_text(
                json.dumps({"source_page": "https://example.test/list", "html": "<p>No matching link</p>"}),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-21",
                        "editor_row_target": 1,
                        "sources": [
                            {
                                "name": "Dropped Live Source",
                                "family": "media/curated list",
                                "url": "https://example.test/list",
                                "language": "en",
                                "access_status": "open",
                                "extraction_method": "agent-extraction-live",
                                "extraction_backend": "fake",
                                "enable_live": True,
                                "source_page_fixture_file": str(page_path),
                                "fake_extraction_response": {
                                    "mentions": [
                                        {
                                            "title": "Dropped Book",
                                            "primary_authors": ["Alex Example"],
                                            "source_url": "https://other.test/book",
                                            "evidence_url": "https://other.test/book",
                                            "evidence_note": "Metadata-only but unverifiable.",
                                        }
                                    ]
                                },
                            },
                            {
                                "name": "Fixture Awards",
                                "family": "award",
                                "url": "https://example.test/awards",
                                "language": "en",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "fixture_candidates": [
                                    {
                                        "title": "Surviving Book",
                                        "primary_authors": ["Robin Example"],
                                        "source_url": "https://example.test/awards/surviving",
                                    }
                                ],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output_dir = tmp_path / "run"

            result = run_autoscout("run-mvp", "--config", str(config_path), "--output-dir", str(output_dir))

            self.assertEqual(result.returncode, 0, result.stderr)
            source_runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
            self.assertEqual(source_runs[0]["status"], "failed")
            self.assertIn("all extracted mentions were dropped", source_runs[0]["reason"])
            self.assertEqual(source_runs[0]["dropped_mention_count"], 1)
            self.assertEqual(source_runs[1]["status"], "collected")
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["failed_source_count"], 1)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["Title"] for row in rows], ["Surviving Book"])

    def test_state_dir_carries_candidate_history_forward(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            write_fixture_config(
                config_path,
                [
                    {
                        "title": "Remembered Book",
                        "primary_authors": ["Alex Example"],
                        "source_url": "https://example.test/awards/book",
                        "russian_edition_status": "not found in Russian",
                    }
                ],
                editor_row_target=1,
            )
            state_dir = tmp_path / "state"

            for output_name in ("run-one", "run-two"):
                result = run_autoscout(
                    "run-mvp",
                    "--config",
                    str(config_path),
                    "--output-dir",
                    str(tmp_path / output_name),
                    "--state-dir",
                    str(state_dir),
                )
                self.assertEqual(result.returncode, 0, result.stderr)

            history = read_state(state_dir, "candidate_history")
            self.assertEqual(history[0]["identity_key"], "remembered book|alex example")
            self.assertEqual(history[0]["last_seen_date"], "2026-05-21")
            summary = json.loads((tmp_path / "run-two" / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(
                summary["major_warnings"],
                [
                    "1 editor-facing rows have unclear Russian-edition status",
                    # Issue #714: the only row carries a title and nothing
                    # else, so the Visible Risk screen could not conclude.
                    "Visible Risk screen concluded for none of 1 editor-facing rows",
                ],
            )

    def test_ingest_feedback_stages_anomalies_and_run_consumes_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-21",
                        "sources": [
                            {
                                "name": "Fixture Awards",
                                "family": "award",
                                "url": "https://example.test/awards",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "fixture_candidates": [
                                    {
                                        "title": "Rejected Book",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/awards/rejected",
                                    },
                                    {
                                        "title": "Deep Book",
                                        "primary_authors": ["Robin Example"],
                                        "source_url": "https://example.test/awards/deep",
                                    },
                                    {
                                        "title": "Edited Book",
                                        "primary_authors": ["Taylor Example"],
                                        "source_url": "https://example.test/awards/edited",
                                    },
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            first_run = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "autoscout",
                    "run-mvp",
                    "--config",
                    str(config_path),
                    "--output-dir",
                    str(tmp_path / "run-one"),
                    "--state-dir",
                    str(state_dir),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(first_run.returncode, 0, first_run.stderr)

            feedback_csv = tmp_path / "feedback.csv"
            with feedback_csv.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["Title", "Author", "Editor status", "Editor note", "Candidate ID"],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "Title": "Rejected Book",
                        "Author": "Alex Example",
                        "Editor status": " Reject ",
                        "Editor note": "No fit.",
                        "Candidate ID": "rejected book|alex example",
                    }
                )
                writer.writerow(
                    {
                        "Title": "Deep Book",
                        "Author": "Robin Example",
                        "Editor status": "needs deeper analysis",
                        "Editor note": "Memo later.",
                        "Candidate ID": "deep book|robin example",
                    }
                )
                writer.writerow(
                    {
                        "Title": "Edited Book Revised",
                        "Author": "Taylor Example",
                        "Editor status": "mispelled",
                        "Editor note": "Keep exact note.",
                        "Candidate ID": "edited book|taylor example",
                    }
                )
                writer.writerow(
                    {
                        "Title": "Missing Book",
                        "Author": "No One",
                        "Editor status": "interested",
                        "Editor note": "Unknown identity.",
                        "Candidate ID": "missing book|no one",
                    }
                )
                writer.writerow({"Title": "", "Author": "", "Editor status": "", "Editor note": "", "Candidate ID": ""})

            ingest = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "autoscout",
                    "ingest-feedback",
                    "--csv",
                    str(feedback_csv),
                    "--state-dir",
                    str(state_dir),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(ingest.returncode, 1)
            self.assertIn("invalid_status_rows=1", ingest.stdout)
            self.assertIn("unmatched_rows=1", ingest.stdout)

            second_run = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "autoscout",
                    "run-mvp",
                    "--config",
                    str(config_path),
                    "--output-dir",
                    str(tmp_path / "run-two"),
                    "--state-dir",
                    str(state_dir),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(second_run.returncode, 0, second_run.stderr)
            with (tmp_path / "run-two" / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertNotIn("Rejected Book", [row["Title"] for row in rows])
            self.assertEqual(
                read_state(state_dir, "pending_editor_rows"),
                [],
            )
            self.assertEqual(
                json.loads((tmp_path / "run-two" / "memo-queue.json").read_text(encoding="utf-8"))[0]["identity_key"],
                "deep book|robin example",
            )
            self.assertEqual(
                json.loads((tmp_path / "run-two" / "proposed-corrections.json").read_text(encoding="utf-8"))[0][
                    "proposed_value"
                ],
                "Edited Book Revised",
            )

            third_run = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "autoscout",
                    "run-mvp",
                    "--config",
                    str(config_path),
                    "--output-dir",
                    str(tmp_path / "run-three"),
                    "--state-dir",
                    str(state_dir),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(third_run.returncode, 0, third_run.stderr)
            self.assertEqual(
                json.loads((tmp_path / "run-three" / "memo-queue.json").read_text(encoding="utf-8")),
                [],
            )

    def test_feedback_for_absent_candidate_persists_until_candidate_returns(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            vanishing_book = {
                "title": "Vanishing Book",
                "primary_authors": ["Alex Example"],
                "source_url": "https://example.test/awards/vanishing",
            }
            other_book = {
                "title": "Other Book",
                "primary_authors": ["Robin Example"],
                "source_url": "https://example.test/awards/other",
            }
            config_with_book = tmp_path / "with-book.json"
            config_without_book = tmp_path / "without-book.json"
            write_fixture_config(config_with_book, [vanishing_book, other_book])
            write_fixture_config(config_without_book, [other_book])

            first_run = run_autoscout(
                "run-mvp",
                "--config",
                str(config_with_book),
                "--output-dir",
                str(tmp_path / "run-one"),
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(first_run.returncode, 0, first_run.stderr)

            feedback_csv = tmp_path / "feedback.csv"
            with feedback_csv.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["Title", "Editor status", "Editor note", "Candidate ID"])
                writer.writeheader()
                writer.writerow(
                    {
                        "Title": "Vanishing Book",
                        "Editor status": "reject",
                        "Editor note": "Not for us.",
                        "Candidate ID": "vanishing book|alex example",
                    }
                )
            ingest = run_autoscout("ingest-feedback", "--csv", str(feedback_csv), "--state-dir", str(state_dir))
            self.assertEqual(ingest.returncode, 0, ingest.stdout)

            second_run = run_autoscout(
                "run-mvp",
                "--config",
                str(config_without_book),
                "--output-dir",
                str(tmp_path / "run-two"),
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(second_run.returncode, 0, second_run.stderr)
            history = read_state(state_dir, "candidate_history")
            rejected = next(record for record in history if record["identity_key"] == "vanishing book|alex example")
            self.assertEqual(rejected["editor_status"], "reject")
            self.assertEqual(rejected["editor_note"], "Not for us.")

            third_run = run_autoscout(
                "run-mvp",
                "--config",
                str(config_with_book),
                "--output-dir",
                str(tmp_path / "run-three"),
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(third_run.returncode, 0, third_run.stderr)
            with (tmp_path / "run-three" / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                titles = [row["Title"] for row in csv.DictReader(handle)]
            self.assertNotIn("Vanishing Book", titles)
            suppressed = json.loads((tmp_path / "run-three" / "suppressed-candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(suppressed[0]["identity_key"], "vanishing book|alex example")

    def test_excluded_candidate_history_accumulates_across_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            excluded_book = {
                "title": "Found Book",
                "primary_authors": ["Alex Example"],
                "source_url": "https://example.test/awards/found",
                "russian_edition_status": "found in Russian",
                "russian_edition_url": "https://example.test/ru/found",
            }
            other_book = {
                "title": "Other Book",
                "primary_authors": ["Robin Example"],
                "source_url": "https://example.test/awards/other",
            }
            config_with_excluded = tmp_path / "with-excluded.json"
            config_without_excluded = tmp_path / "without-excluded.json"
            write_fixture_config(config_with_excluded, [excluded_book, other_book])
            write_fixture_config(config_without_excluded, [other_book])

            for config_path, output_name in (
                (config_with_excluded, "run-one"),
                (config_without_excluded, "run-two"),
            ):
                result = run_autoscout(
                    "run-mvp",
                    "--config",
                    str(config_path),
                    "--output-dir",
                    str(tmp_path / output_name),
                    "--state-dir",
                    str(state_dir),
                )
                self.assertEqual(result.returncode, 0, result.stderr)

            excluded_history = read_state(state_dir, "excluded_candidate_history")
            record = next(record for record in excluded_history if record["identity_key"] == "found book|alex example")
            self.assertEqual(record["exclusion_reason"], "found in Russian")

    def test_write_json_sorts_object_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.json"
            write_json(path, {"z": 1, "a": 2, "m": 3})
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            keys = list(data.keys())
            self.assertEqual(keys, sorted(keys))

    def test_save_state_sorts_records_by_identity_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            record_z = {
                "candidate_id": candidate_id_for("zen book|author z"),
                "identity_key": "zen book|author z",
                "title": "Zen Book",
            }
            record_a = {
                "candidate_id": candidate_id_for("alpha book|author a"),
                "identity_key": "alpha book|author a",
                "title": "Alpha Book",
            }
            record_m = {
                "candidate_id": candidate_id_for("middle book|author m"),
                "identity_key": "middle book|author m",
                "title": "Middle Book",
            }
            proposal_z = {"id": "source-z", "source_url": "https://z.test"}
            proposal_a = {"id": "source-a", "source_url": "https://a.test"}

            save_state(
                state_dir,
                candidate_history=[record_z, record_a, record_m],
                excluded_candidate_history=[record_m, record_z],
                scouting_candidates=[record_a, record_m, record_z],
                consumed_feedback=[],
                source_proposals=[proposal_z, proposal_a],
            )

            for collection, expected_keys in [
                ("candidate_history", ["alpha book|author a", "middle book|author m", "zen book|author z"]),
                ("excluded_candidate_history", ["middle book|author m", "zen book|author z"]),
                (
                    "latest_scouting_candidates",
                    ["alpha book|author a", "middle book|author m", "zen book|author z"],
                ),
            ]:
                data = read_state(state_dir, collection)
                actual = [r["identity_key"] for r in data]
                self.assertEqual(actual, expected_keys, f"{collection} not sorted")
            proposals = read_state(state_dir, "source_proposals")
            self.assertEqual([proposal["id"] for proposal in proposals], ["source-a", "source-z"])

    def test_state_roundtrip_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            record = {
                "candidate_id": candidate_id_for("book|author"),
                "title": "Book",
                "identity_key": "book|author",
                "last_seen_date": "2026-06-01",
            }
            proposal = {"source_url": "https://example.test", "id": "source-example-test"}

            save_state(
                state_dir,
                candidate_history=[record],
                excluded_candidate_history=[],
                scouting_candidates=[],
                consumed_feedback=[],
                source_proposals=[proposal],
            )

            before = state_snapshot(state_dir)
            loaded = load_state(state_dir)
            save_state(
                state_dir,
                candidate_history=loaded["candidate_history"],
                excluded_candidate_history=loaded["excluded_candidate_history"],
                scouting_candidates=[],
                consumed_feedback=[],
                source_proposals=loaded["source_proposals"],
            )

            self.assertEqual(state_snapshot(state_dir), before)

    def test_write_json_produces_consistent_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            path_a = Path(tmp) / "a.json"
            path_b = Path(tmp) / "b.json"
            data = {"b": 2, "a": 1}
            write_json(path_a, data)
            write_json(path_b, data)
            self.assertEqual(path_a.read_text(encoding="utf-8"), path_b.read_text(encoding="utf-8"))

    def test_record_feedback_writes_pending_feedback_and_converges_with_ingest(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "Test Source",
                                "family": "media/curated list",
                                "url": "https://example.test",
                                "language": "en",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "fixture_candidates": [
                                    {
                                        "title": "Candidate Book",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/book",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Test evidence.",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            # First run to create candidate history
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "autoscout",
                    "run-mvp",
                    "--config",
                    str(config_path),
                    "--output-dir",
                    str(tmp_path / "runs"),
                    "--state-dir",
                    str(state_dir),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            # Record feedback via new command
            status = "interested"
            note = "Looks promising for adult nonfiction."
            identity_key = "candidate book|alex example"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "autoscout",
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
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("recorded_feedback", result.stdout)
            self.assertIn(identity_key, result.stdout)

            # Verify feedback is staged in pending feedback
            state = load_state(state_dir)
            pending = state["pending_feedback"]
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["identity_key"], identity_key)
            self.assertEqual(pending[0]["Editor Status"], status)
            self.assertEqual(pending[0]["Editor Note"], note)

            # Second run consumes the feedback
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "autoscout",
                    "run-mvp",
                    "--config",
                    str(config_path),
                    "--output-dir",
                    str(tmp_path / "runs-2"),
                    "--state-dir",
                    str(state_dir),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            # Candidate history now reflects the interested status
            history = read_run_output(tmp_path / "runs-2", "candidate_history")
            candidate = next(c for c in history if c["identity_key"] == identity_key)
            self.assertEqual(candidate["editor_status"], status)
            self.assertEqual(candidate["editor_note"], note)

            # Pending feedback is cleared after consumption
            state = load_state(state_dir)
            self.assertEqual(len(state["pending_feedback"]), 0)

    def test_record_feedback_rejects_unsupported_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(parents=True)
            # Seed candidate history so identity is known
            seed_state(
                state_dir,
                candidate_history=[{"identity_key": "test|author"}],
                excluded_candidate_history=[],
                pending_editor_rows=[],
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "autoscout",
                    "record-feedback",
                    "--identity-key",
                    "test|author",
                    "--status",
                    "not_a_real_status",
                    "--state-dir",
                    str(state_dir),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unsupported editor status", result.stdout)

    def test_record_feedback_rejects_unknown_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(parents=True)

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "autoscout",
                    "record-feedback",
                    "--identity-key",
                    "nonexistent|unknown",
                    "--status",
                    "interested",
                    "--state-dir",
                    str(state_dir),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unknown candidate identity", result.stdout)

    def test_render_review_generates_markdown_from_run_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "Test Source",
                                "family": "media/curated list",
                                "url": "https://example.test",
                                "language": "en",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "fixture_candidates": [
                                    {
                                        "title": "Review Book One",
                                        "primary_authors": ["Alex Example With A Very Long Name"],
                                        "source_url": "https://example.test/book1",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Test evidence.",
                                        "russian_edition_status": "not found in Russian",
                                        "visible_risk_status": "no visible risk",
                                        "visible_risk_status_source": "manual",
                                        "content_description": "Тестовое описание книги.",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            output_dir = tmp_path / "runs"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "autoscout",
                    "run-mvp",
                    "--config",
                    str(config_path),
                    "--output-dir",
                    str(output_dir),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            result = subprocess.run(
                [sys.executable, "-m", "autoscout", "render-review", "--output-dir", str(output_dir)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Review rendered to", result.stdout)

            review = (output_dir / "weekly-review.md").read_text(encoding="utf-8")
            self.assertIn("Еженедельный обзор AutoScout", review)
            self.assertIn("Review Book One", review)
            self.assertIn("Alex Example With A Very Long Name", review)
            self.assertNotIn("review book one&#124;alex example with a very long name", review)
            self.assertIn("Что делать редактору", review)
            self.assertNotIn("record-feedback", review)

            html_review = (output_dir / "weekly-review.html").read_text(encoding="utf-8")
            self.assertTrue(html_review.lstrip().startswith("<!doctype html>"))
            self.assertIn("Еженедельный обзор AutoScout", html_review)
            self.assertIn("Review Book One", html_review)
            self.assertIn("За эту неделю", html_review)
            self.assertNotIn("record-feedback", html_review)

    def test_run_health_summarizes_healthy_short_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "runs"
            output_dir.mkdir()
            (output_dir / "pilot-status.json").write_text(
                json.dumps(
                    {
                        "ready_for_editor": False,
                        "editor_facing_count": 1,
                        "caveats": ["1 editor-facing rows produced; fewer than target 3"],
                        "shareable_artifacts": [{"path": "editor-facing.csv"}],
                    }
                ),
                encoding="utf-8",
            )
            (output_dir / "run-summary.json").write_text(
                json.dumps(
                    {
                        "editor_facing_count": 1,
                        "editor_row_target": 3,
                        "skipped_source_count": 0,
                        "failed_source_count": 0,
                        "shortfall_reason": "source scarcity from configured source evidence",
                        "source_contributions": [{"source_name": "Small Source", "editor_facing_contribution": 1}],
                        "description_coverage": {
                            "coverage": "partial",
                            "validated_count": 0,
                            "fallback_count": 1,
                            "invalid_reason_counts": {"non-string value rejected": 1},
                            "fallback_ids": ["book|author"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            (output_dir / "source-runs.json").write_text(
                json.dumps(
                    [
                        {
                            "source_name": "Small Source",
                            "status": "collected",
                            "collection_mode": "codex-capture",
                            "debug_metadata": {
                                "collector_kind": "codex-extraction",
                                "extracted_mention_count": 1,
                                "validated_mention_count": 1,
                                "dropped_mention_count": 0,
                                "field_dropped_count": 0,
                                "provenance_dropped_count": 0,
                            },
                        }
                    ]
                ),
                encoding="utf-8",
            )
            (output_dir / "weekly-review.md").write_text("# Review\n", encoding="utf-8")

            result = run_autoscout("run-health", "--output-dir", str(output_dir))

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("run_health_status=not_ready", result.stdout)
            self.assertIn("classification=source scarcity", result.stdout)
            self.assertIn("codex_workflow_health=healthy", result.stdout)
            self.assertIn("source_volume_status=below_target_no_failures", result.stdout)
            self.assertIn(
                "readiness_blocker=Codex workflow ran cleanly but configured source evidence produced only 1 editor-facing rows (target 3); no source or pipeline failures were recorded.",
                result.stdout,
            )
            self.assertIn("editor_facing_count=1", result.stdout)
            self.assertIn("editor_row_target=3", result.stdout)
            self.assertIn("description_coverage=partial", result.stdout)
            self.assertIn("description_validated_count=0", result.stdout)
            self.assertIn("description_fallback_count=1", result.stdout)
            self.assertIn("description_invalid_reasons={non-string value rejected: 1}", result.stdout)
            self.assertIn("description_fallback_candidate=book|author", result.stdout)
            self.assertIn("artifact=pilot-status.json:", result.stdout)
            self.assertIn("shareable_artifact=weekly-review.md:", result.stdout)
            self.assertIn(
                "source_codex=Small Source|collected||extracted=1|validated=1|dropped=0|field_dropped=0|provenance_dropped=0",
                result.stdout,
            )

    def test_run_health_reports_skipped_and_failed_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "runs"
            output_dir.mkdir()
            (output_dir / "pilot-status.json").write_text(
                json.dumps(
                    {
                        "ready_for_editor": False,
                        "editor_facing_count": 2,
                        "caveats": ["configured source skipped"],
                        "shareable_artifacts": [],
                    }
                ),
                encoding="utf-8",
            )
            (output_dir / "run-summary.json").write_text(
                json.dumps(
                    {
                        "collection_mode": "mixed",
                        "collection_modes": ["live", "replay"],
                        "editor_facing_count": 2,
                        "editor_row_target": 3,
                        "skipped_source_count": 1,
                        "failed_source_count": 1,
                        "shortfall_reason": "configured source skipped or failed",
                        "source_contributions": [],
                    }
                ),
                encoding="utf-8",
            )
            (output_dir / "source-runs.json").write_text(
                json.dumps(
                    [
                        {"source_name": "Paid Source", "status": "skipped", "reason": "paid"},
                        {
                            "source_name": "Broken Source",
                            "status": "failed",
                            "reason": "malformed response",
                            "collection_mode": "real-live",
                            "debug_metadata": {
                                "backend_name": "real",
                                "backend_mode": "real-live",
                                "safe_failure_status": "malformed",
                                "capture_live_response_file": "broken-page-capture.json",
                                "capture_extraction_response_file": "broken-extraction-capture.json",
                            },
                        },
                    ]
                ),
                encoding="utf-8",
            )

            result = run_autoscout("run-health", "--output-dir", str(output_dir))

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("classification=source health problem", result.stdout)
            self.assertIn("collection_mode=mixed", result.stdout)
            self.assertIn("collection_modes=live,replay", result.stdout)
            self.assertIn("source_mode=Broken Source|real-live", result.stdout)
            self.assertIn(
                "source_backend=Broken Source|real|real-live|malformed|broken-page-capture.json|broken-extraction-capture.json",
                result.stdout,
            )
            self.assertIn("skipped_source_count=1", result.stdout)
            self.assertIn("failed_source_count=1", result.stdout)
            self.assertIn("source_problem=Paid Source|skipped|paid", result.stdout)
            self.assertIn("source_problem=Broken Source|failed|malformed response", result.stdout)

    def test_run_health_missing_artifacts_reports_clear_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "runs"
            output_dir.mkdir()
            (output_dir / "run-summary.json").write_text("{}", encoding="utf-8")

            result = run_autoscout("run-health", "--output-dir", str(output_dir))

            self.assertEqual(result.returncode, 1)
            self.assertIn("run_health_status=incomplete", result.stdout)
            self.assertIn("missing_artifact=pilot-status.json", result.stdout)
            self.assertIn("missing_artifact=source-runs.json", result.stdout)

    def test_record_feedback_preserves_verbatim_editor_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            state_dir.mkdir(parents=True)
            # Seed candidate history

            seed_state(
                state_dir,
                candidate_history=[{"identity_key": "note test|author"}],
                excluded_candidate_history=[],
                pending_editor_rows=[],
            )

            verbatim_note = 'This has *asterisks*, commas, and "quotes".\nLine breaks too.'
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "autoscout",
                    "record-feedback",
                    "--identity-key",
                    "note test|author",
                    "--status",
                    "maybe_watch",
                    "--note",
                    verbatim_note,
                    "--state-dir",
                    str(state_dir),
                    "--confirmed-by-editor",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            state = load_state(state_dir)
            self.assertEqual(len(state["pending_feedback"]), 1)
            self.assertEqual(state["pending_feedback"][0]["Editor Note"], verbatim_note)
