import csv
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from state_store_support import read_state
from state_store_support import state_snapshot

from autoscout.__main__ import main
from autoscout.state import load_state
from autoscout.state import save_state


class EditorVisibilityWorkflowTests(unittest.TestCase):
    def test_visibility_state_round_trips_through_state_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            visibility = [
                {
                    "identity_key": "book|author",
                    "visibility": "hidden",
                    "action": "hide",
                    "decision_date": "2026-07-19",
                    "reason": "Verbatim reason.",
                    "audit_provenance": "editor conversation",
                    "audit_log": [],
                }
            ]
            save_state(state_dir, [], [], [], [], candidate_visibility=visibility)
            self.assertEqual(load_state(state_dir)["candidate_visibility"], visibility)
            save_state(state_dir, [], [], [], [])
            self.assertEqual(load_state(state_dir)["candidate_visibility"], visibility)

    def test_hide_and_restore_use_feedback_state_and_all_review_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            config = root / "sources.json"
            candidate = {
                "title": "Hideable Candidate",
                "primary_authors": ["Alex Operator"],
                "source_url": "https://example.test/hideable",
                "signal_strength": "major media list",
                "evidence_note": "The source selected this book.",
                "candidate_category": "adult nonfiction",
                "content_description": "Описание hideable candidate.",
                "russian_edition_status": "not found in Russian",
                "visible_risk_status": "no visible risk",
                "visible_risk_status_source": "manual",
            }

            def write_config(run_date: str, fixture_candidates: list[dict[str, object]]) -> None:
                config.write_text(
                    json.dumps(
                        {
                            "run_date": run_date,
                            "sources": [
                                {
                                    "name": "Visibility Test Source",
                                    "family": "media/curated list",
                                    "url": "https://example.test/source",
                                    "access_status": "open",
                                    "extraction_method": "fixture",
                                    "check_frequency": "weekly",
                                    "language": "en",
                                    "fixture_candidates": fixture_candidates,
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )

            def command(*args: str) -> int:
                with redirect_stdout(StringIO()):
                    return int(main(list(args)))

            write_config("2026-07-12", [candidate])
            first_output = root / "first"
            self.assertEqual(
                command(
                    "run-mvp",
                    "--config",
                    str(config),
                    "--output-dir",
                    str(first_output),
                    "--state-dir",
                    str(state_dir),
                ),
                0,
            )
            identity_key = "hideable candidate|alex operator"
            self.assertEqual(
                command(
                    "record-feedback",
                    "--identity-key",
                    identity_key,
                    "--status",
                    "interested",
                    "--note",
                    "Keep this candidate.",
                    "--state-dir",
                    str(state_dir),
                    "--confirmed-by-editor",
                ),
                0,
            )

            write_config("2026-07-19", [])
            second_output = root / "second"
            self.assertEqual(
                command(
                    "run-mvp",
                    "--config",
                    str(config),
                    "--output-dir",
                    str(second_output),
                    "--state-dir",
                    str(state_dir),
                ),
                0,
            )
            self.assertEqual(
                command("render-review", "--output-dir", str(second_output), "--state-dir", str(state_dir)),
                0,
            )

            self.assertEqual(
                command(
                    "record-visibility",
                    "--identity-key",
                    identity_key,
                    "--action",
                    "remove",
                    "--reason",
                    "No longer in this review.",
                    "--provenance",
                    "editor conversation",
                    "--state-dir",
                    str(state_dir),
                ),
                0,
            )
            self.assertEqual(
                command("render-review", "--output-dir", str(second_output), "--state-dir", str(state_dir)),
                0,
            )
            review_text = (second_output / "weekly-review.md").read_text(encoding="utf-8")
            html_text = (second_output / "weekly-review.html").read_text(encoding="utf-8")
            for path_text in (review_text, html_text):
                self.assertNotIn("Hideable Candidate", path_text)
            # Totals wording changed by #1057 (authorized by the operator in the issue on 2026-11-13).
            self.assertIn(
                "добавлено в последний запуск — 0, с отметкой «обновлено» — 0, скрытых редактором — 1",
                review_text,
            )
            self.assertIn("скрытых редактором — 1", html_text)
            with (second_output / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                self.assertEqual(list(csv.DictReader(handle)), [])

            visibility = read_state(state_dir, "candidate_visibility")
            self.assertEqual(visibility[0]["visibility"], "hidden")
            self.assertEqual(visibility[0]["action"], "hide")
            self.assertEqual(visibility[0]["reason"], "No longer in this review.")
            self.assertEqual(visibility[0]["audit_provenance"], "editor conversation")
            history = read_state(state_dir, "candidate_history")
            remembered = next(record for record in history if record["identity_key"] == identity_key)
            self.assertEqual(remembered["editor_status"], "interested")
            self.assertEqual(remembered["editor_note"], "Keep this candidate.")
            self.assertEqual(remembered["source_evidence"][0]["source_url"], "https://example.test/hideable")

            hidden_state = state_snapshot(state_dir)["candidate_visibility"]

            # A normal pipeline run must apply explicit visibility to its direct
            # editor-facing CSV, while weekly deltas still describe all current
            # evidence before that filtering.
            write_config("2026-07-26", [candidate])
            third_output = root / "third"
            self.assertEqual(
                command(
                    "run-mvp",
                    "--config",
                    str(config),
                    "--output-dir",
                    str(third_output),
                    "--state-dir",
                    str(state_dir),
                ),
                0,
            )
            with (third_output / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                self.assertEqual(list(csv.DictReader(handle)), [])
            third_summary = json.loads((third_output / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(third_summary["update_delta_counts"]["unchanged"], 1)

            self.assertEqual(
                command(
                    "record-visibility",
                    "--identity-key",
                    identity_key,
                    "--action",
                    "hide",
                    "--state-dir",
                    str(state_dir),
                ),
                0,
            )
            self.assertEqual(hidden_state, state_snapshot(state_dir)["candidate_visibility"])
            self.assertEqual(
                command("render-review", "--output-dir", str(third_output), "--state-dir", str(state_dir)),
                0,
            )
            third_review = (third_output / "weekly-review.md").read_text(encoding="utf-8")
            self.assertNotIn("Hideable Candidate", third_review)
            self.assertIn(
                "**За эту неделю**: новых — 0, улучшенных — 0, изменённых — 0, без изменений — 1, скрыто правилами — 0.",
                third_review,
            )
            self.assertIn(
                "добавлено в последний запуск — 0, с отметкой «обновлено» — 0, скрытых редактором — 1",
                third_review,
            )

            self.assertEqual(
                command(
                    "record-visibility",
                    "--identity-key",
                    identity_key,
                    "--action",
                    "show-again",
                    "--reason",
                    "Bring it back.",
                    "--state-dir",
                    str(state_dir),
                ),
                0,
            )
            self.assertEqual(
                command("render-review", "--output-dir", str(third_output), "--state-dir", str(state_dir)),
                0,
            )
            restored_review = (third_output / "weekly-review.md").read_text(encoding="utf-8")
            self.assertIn("Hideable Candidate", restored_review)
            self.assertIn("https://example.test/hideable", restored_review)
            with (third_output / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["Candidate ID"], identity_key)
            self.assertEqual(rows[0]["Editor status"], "interested")
            self.assertEqual(rows[0]["Editor note"], "Keep this candidate.")
            restored = read_state(state_dir, "candidate_visibility")
            self.assertEqual(restored[0]["visibility"], "visible")
            self.assertEqual([item["action"] for item in restored[0]["audit_log"]], ["hide", "restore"])
