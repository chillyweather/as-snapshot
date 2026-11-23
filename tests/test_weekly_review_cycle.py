import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from pathlib import Path

from state_store_support import read_state
from state_store_support import seed_state

from autoscout.cli.weekly import run_mvp
from autoscout.codex_workflow import finalize_codex_run
from autoscout.codex_workflow import prepare_codex_extraction
from autoscout.editor_feedback import record_feedback
from autoscout.editor_feedback import record_visibility
from autoscout.editorial_taste_profile import add_profile_entry
from autoscout.editorial_taste_profile import revoke_profile_entry
from autoscout.review_rendering import render_review
from autoscout.run_health import run_health


def markdown_review_rows(review: str) -> dict[str, dict[str, str]]:
    """The Markdown review's candidate table, by title, each row as {header: cell}."""
    lines = [line for line in review.splitlines() if line.startswith("| ")]
    header = [cell.strip() for cell in lines[0].strip("|").split("|")]
    rows = [dict(zip(header, (cell.strip() for cell in line.strip("|").split("|")), strict=True)) for line in lines[1:]]
    return {row["Название"]: row for row in rows}


def write_fixture_source_config(
    config_path: Path,
    run_date: str,
    candidates: list[dict[str, object]],
    *,
    source_name: str = "Fixture Source",
    source_url: str = "https://example.test/fixture",
) -> None:
    config_path.write_text(
        json.dumps(
            {
                "run_date": run_date,
                "sources": [
                    {
                        "name": source_name,
                        "family": "media/curated list",
                        "url": source_url,
                        "access_status": "open",
                        "extraction_method": "fixture",
                        "language": "en",
                        "fixture_candidates": candidates,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


class _InProcessResult:
    __slots__ = ("returncode", "stderr", "stdout")

    def __init__(self, returncode: int, stderr: str = "", stdout: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


def run_autoscout(*args: str) -> _InProcessResult:
    """Run an autoscout command in-process, capturing stdout/stderr."""
    command = args[0] if args else ""
    kwargs: dict[str, object] = {}
    i = 1
    while i < len(args):
        key = args[i].lstrip("-").replace("-", "_")
        if key == "confirmed_by_editor":
            kwargs[key] = True
            i += 1
        elif i + 1 < len(args):
            kwargs[key] = args[i + 1]
            i += 2
        else:
            i += 1
    captured_out = io.StringIO()
    captured_err = io.StringIO()
    try:
        if command == "run-mvp":
            with redirect_stderr(captured_err):
                run_mvp(
                    Path(str(kwargs["config"])),
                    Path(str(kwargs["output_dir"])),
                    state_dir=Path(str(kwargs["state_dir"])) if kwargs.get("state_dir") else None,
                )
            return _InProcessResult(0, captured_err.getvalue())
        if command == "render-review":
            with redirect_stdout(captured_out), redirect_stderr(captured_err):
                exit_code = render_review(
                    Path(str(kwargs["output_dir"])),
                    state_dir=Path(str(kwargs["state_dir"])) if kwargs.get("state_dir") else None,
                )
            return _InProcessResult(exit_code, captured_err.getvalue(), captured_out.getvalue())
        if command == "record-feedback":
            exit_code = record_feedback(
                str(kwargs["identity_key"]),
                str(kwargs["status"]),
                str(kwargs.get("note", "")),
                Path(str(kwargs["state_dir"])),
                confirmed_by_editor=bool(kwargs.get("confirmed_by_editor", False)),
            )
            return _InProcessResult(exit_code)
        if command == "prepare-codex-extraction":
            with redirect_stdout(captured_out), redirect_stderr(captured_err):
                exit_code = prepare_codex_extraction(Path(str(kwargs["config"])), Path(str(kwargs["output_dir"])))
            return _InProcessResult(exit_code, captured_err.getvalue(), captured_out.getvalue())
        if command == "finalize-codex-run":
            with redirect_stdout(captured_out), redirect_stderr(captured_err):
                live_cfg = Path(str(kwargs["live_config"])) if kwargs.get("live_config") else None
                exit_code = finalize_codex_run(Path(str(kwargs["output_dir"])), live_cfg)
            return _InProcessResult(exit_code, captured_err.getvalue(), captured_out.getvalue())
        if command == "run-health":
            with redirect_stdout(captured_out):
                exit_code = run_health(Path(str(kwargs["output_dir"])))
            return _InProcessResult(exit_code, stdout=captured_out.getvalue())
        raise ValueError(f"Unknown command: {command}")
    except SystemExit as e:
        return _InProcessResult(e.code if e.code is not None else 1, captured_err.getvalue(), captured_out.getvalue())
    except Exception as e:
        return _InProcessResult(1, captured_err.getvalue() + "\n" + str(e), captured_out.getvalue())


class OperatorWorkflowDocTests(unittest.TestCase):
    def test_fixture_backed_review_feedback_cycle_consumes_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-06-27",
                        "sources": [
                            {
                                "name": "Workflow Test Source",
                                "family": "media/curated list",
                                "url": "https://example.test/workflow",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "language": "en",
                                "fixture_candidates": [
                                    {
                                        "title": "Workflow Candidate",
                                        "primary_authors": ["Alex Operator"],
                                        "source_url": "https://example.test/workflow/book",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Workflow test evidence.",
                                        "candidate_category": "adult nonfiction",
                                        "russian_edition_status": "not found in Russian",
                                        "visible_risk_status": "no visible risk",
                                        "visible_risk_status_source": "manual",
                                        "content_description": "Тестовое описание для рабочего цикла.",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            first_output = tmp_path / "run-1"
            result = run_autoscout(
                "run-mvp",
                "--config",
                str(config_path),
                "--output-dir",
                str(first_output),
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            result = run_autoscout(
                "render-review",
                "--output-dir",
                str(first_output),
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            review = (first_output / "weekly-review.md").read_text(encoding="utf-8")
            self.assertNotIn("workflow candidate&#124;alex operator", review)

            identity_key = "workflow candidate|alex operator"
            note = "Reject for this workflow test."
            result = run_autoscout(
                "record-feedback",
                "--identity-key",
                identity_key,
                "--status",
                "reject",
                "--note",
                note,
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            pending = read_state(state_dir, "pending_editor_rows")
            self.assertEqual(pending[0]["identity_key"], identity_key)

            second_output = tmp_path / "run-2"
            result = run_autoscout(
                "run-mvp",
                "--config",
                str(config_path),
                "--output-dir",
                str(second_output),
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            history = json.loads((second_output / "candidate-history.json").read_text(encoding="utf-8"))
            candidate = next(record for record in history if record["identity_key"] == identity_key)
            self.assertEqual(candidate["editor_status"], "reject")
            self.assertEqual(candidate["editor_note"], note)
            self.assertEqual(
                read_state(state_dir, "pending_editor_rows"),
                [],
            )
            suppressed = json.loads((second_output / "suppressed-candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(suppressed[0]["identity_key"], identity_key)

    def test_two_week_cumulative_review_retains_absent_candidate_across_formats(self):
        """The operator seam is run-mvp -> record-feedback -> run-mvp -> render-review."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            config_path = tmp_path / "sources.json"

            def write_config(run_date: str, candidates: list[dict[str, object]]) -> None:
                config_path.write_text(
                    json.dumps(
                        {
                            "run_date": run_date,
                            "sources": [
                                {
                                    "name": "Cumulative Test Source",
                                    "family": "media/curated list",
                                    "url": "https://example.test/cumulative",
                                    "access_status": "open",
                                    "extraction_method": "fixture",
                                    "language": "en",
                                    "fixture_candidates": candidates,
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )

            historical_candidate = {
                "title": "Remembered Candidate",
                "primary_authors": ["Alex Historical"],
                "source_url": "https://example.test/cumulative/remembered",
                "signal_strength": "major media list",
                "evidence_note": "The source selected this book.",
                "candidate_category": "adult nonfiction",
                "content_description": "Историческое описание кандидата.",
                "russian_edition_status": "not found in Russian",
                "visible_risk_status": "no visible risk",
                "visible_risk_status_source": "manual",
            }
            current_candidate = {
                "title": "Still Current",
                "primary_authors": ["Sam Current"],
                "source_url": "https://example.test/cumulative/current",
                "signal_strength": "shortlist",
                "evidence_note": "The current source selected this book.",
                "candidate_category": "adult nonfiction",
                "content_description": "Текущее описание кандидата.",
                "russian_edition_status": "not found in Russian",
                "visible_risk_status": "no visible risk",
                "visible_risk_status_source": "manual",
            }
            write_config("2026-06-27", [historical_candidate, current_candidate])
            first_output = tmp_path / "run-one"
            result = run_autoscout(
                "run-mvp",
                "--config",
                str(config_path),
                "--output-dir",
                str(first_output),
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            remembered_id = "remembered candidate|alex historical"
            result = run_autoscout(
                "record-feedback",
                "--identity-key",
                remembered_id,
                "--status",
                "interested",
                "--note",
                "Keep this on the watchlist.",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            # A legacy record exercises safe fallback rendering when newer
            # snapshot fields were not persisted by an older run.
            history = read_state(state_dir, "candidate_history")
            history.append(
                {
                    "identity_key": "legacy candidate|legacy author",
                    "title": "Legacy Candidate",
                    "last_seen_date": "2026-01-01",
                }
            )
            seed_state(state_dir, candidate_history=history)

            write_config("2026-07-04", [current_candidate])
            second_output = tmp_path / "run-two"
            result = run_autoscout(
                "run-mvp",
                "--config",
                str(config_path),
                "--output-dir",
                str(second_output),
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            second_summary = json.loads((second_output / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(second_summary["editor_facing_count"], 1)
            self.assertEqual(second_summary["raw_candidate_count"], 1)

            result = run_autoscout(
                "render-review",
                "--output-dir",
                str(second_output),
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            review = (second_output / "weekly-review.md").read_text(encoding="utf-8")
            html_review = (second_output / "weekly-review.html").read_text(encoding="utf-8")
            with (second_output / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                csv_rows = list(csv.DictReader(handle))
            identities = [row["Candidate ID"] for row in csv_rows]
            self.assertEqual(identities, ["still current|sam current", remembered_id, "legacy candidate|legacy author"])
            self.assertNotIn(remembered_id, review)
            self.assertNotIn(remembered_id, html_review)
            self.assertIn(remembered_id, "\n".join(str(row) for row in csv_rows))
            for representation in (review, html_review, "\n".join(str(row) for row in csv_rows)):
                self.assertIn("Legacy Candidate", representation)
            # Product rule changed by #1057 (authorized by the operator in the
            # issue on 2026-11-13): an absent book is listed under the date it
            # was added, not in a «Ранее просмотренные» group, and is not
            # marked updated.
            remembered_row = markdown_review_rows(review)["Remembered Candidate"]
            self.assertEqual((remembered_row["Добавлено"], remembered_row["Обновлено"]), ("2026-06-27", ""))
            self.assertIn("Keep this on the watchlist.", review)
            self.assertIn("https://example.test/cumulative/remembered", review)
            self.assertIn('<h2>27 июня 2026 <span class="count">', html_review)
            # The legacy record's last-seen date stands in for its missing first-seen date.
            self.assertIn('<h2>1 января 2026 <span class="count">(1)</span>', html_review)
            for representation in (review, html_review):
                self.assertNotIn("Ранее просмотренные", representation)
                self.assertNotIn("⬆ обновлено", representation)
            self.assertEqual(csv_rows[1]["Update delta"], "unchanged")
            self.assertEqual(csv_rows[1]["Lifecycle"], "Previously seen")
            self.assertEqual(csv_rows[1]["Editor status"], "interested")
            self.assertEqual(csv_rows[1]["Editor note"], "Keep this on the watchlist.")

            saved_review = review, html_review, (second_output / "editor-facing.csv").read_text(encoding="utf-8")
            result = run_autoscout(
                "render-review",
                "--output-dir",
                str(second_output),
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(saved_review[0], (second_output / "weekly-review.md").read_text(encoding="utf-8"))
            self.assertEqual(saved_review[1], (second_output / "weekly-review.html").read_text(encoding="utf-8"))
            self.assertEqual(saved_review[2], (second_output / "editor-facing.csv").read_text(encoding="utf-8"))

            final_history = read_state(state_dir, "candidate_history")
            remembered = next(record for record in final_history if record["identity_key"] == remembered_id)
            self.assertEqual(remembered["first_seen_date"], "2026-06-27")
            self.assertEqual(remembered["last_seen_date"], "2026-06-27")

    def test_two_week_review_orders_by_date_and_keeps_weekly_counts_separate(self):
        """Current changes lead the cumulative projection at the operator seam."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            config_path = tmp_path / "sources.json"

            def write_config(run_date: str, change_family: str, candidates: list[dict[str, object]]) -> None:
                config_path.write_text(
                    json.dumps(
                        {
                            "run_date": run_date,
                            "sources": [
                                {
                                    "name": "Stable Source",
                                    "family": "media/curated list",
                                    "url": "https://example.test/stable",
                                    "access_status": "open",
                                    "extraction_method": "fixture",
                                    "language": "en",
                                    "fixture_candidates": [
                                        candidate for candidate in candidates if candidate["group"] == "stable"
                                    ],
                                },
                                {
                                    "name": "Changing Source",
                                    "family": change_family,
                                    "url": "https://example.test/changing",
                                    "access_status": "open",
                                    "extraction_method": "fixture",
                                    "language": "en",
                                    "fixture_candidates": [
                                        candidate for candidate in candidates if candidate["group"] == "changing"
                                    ],
                                },
                            ],
                        }
                    ),
                    encoding="utf-8",
                )

            def candidate(title: str, author: str, group: str) -> dict[str, object]:
                return {
                    "group": group,
                    "title": title,
                    "primary_authors": [author],
                    "source_url": f"https://example.test/books/{title.lower().replace(' ', '-')}",
                    "signal_strength": "shortlist",
                    "evidence_note": f"Evidence for {title}.",
                    "candidate_category": "adult nonfiction",
                    "russian_edition_status": "not found in Russian",
                    "visible_risk_status": "no visible risk",
                    "visible_risk_status_source": "manual",
                    "content_description": f"Описание {title}.",
                }

            historical = candidate("Historical Candidate", "Alex Historical", "stable")
            unchanged = candidate("Unchanged Candidate", "Sam Stable", "stable")
            updated = candidate("Updated Candidate", "Bea Changed", "changing")
            write_config("2026-06-27", "media/curated list", [historical, unchanged, updated])
            first_output = tmp_path / "run-one"
            first = run_autoscout(
                "run-mvp",
                "--config",
                str(config_path),
                "--output-dir",
                str(first_output),
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(first.returncode, 0, first.stderr)

            historical_id = "historical candidate|alex historical"
            feedback = run_autoscout(
                "record-feedback",
                "--identity-key",
                historical_id,
                "--status",
                "interested",
                "--note",
                "Keep this historical candidate.",
                "--state-dir",
                str(state_dir),
                "--confirmed-by-editor",
            )
            self.assertEqual(feedback.returncode, 0, feedback.stderr)

            new_candidate = candidate("New Candidate", "Nina New", "stable")
            write_config("2026-07-04", "award", [unchanged, updated, new_candidate])
            second_output = tmp_path / "run-two"
            second = run_autoscout(
                "run-mvp",
                "--config",
                str(config_path),
                "--output-dir",
                str(second_output),
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            rendered = run_autoscout(
                "render-review",
                "--output-dir",
                str(second_output),
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(rendered.returncode, 0, rendered.stdout + rendered.stderr)

            with (second_output / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                csv_rows = list(csv.DictReader(handle))
            ids = [row["Candidate ID"] for row in csv_rows]
            self.assertEqual(
                ids,
                [
                    "new candidate|nina new",
                    "updated candidate|bea changed",
                    "unchanged candidate|sam stable",
                    historical_id,
                ],
            )
            self.assertEqual(
                [row["Update delta"] for row in csv_rows],
                ["new", "upgraded", "unchanged", "unchanged"],
            )
            self.assertEqual(
                [row["Lifecycle"] for row in csv_rows],
                ["New", "Updated", "Previously seen", "Previously seen"],
            )
            self.assertEqual(csv_rows[3]["Editor status"], "interested")
            self.assertEqual(csv_rows[3]["Editor note"], "Keep this historical candidate.")

            review = (second_output / "weekly-review.md").read_text(encoding="utf-8")
            html_review = (second_output / "weekly-review.html").read_text(encoding="utf-8")
            for representation in (review, html_review, "\n".join(str(row) for row in csv_rows)):
                self.assertLess(representation.index("New Candidate"), representation.index("Updated Candidate"))
                self.assertLess(representation.index("Updated Candidate"), representation.index("Unchanged Candidate"))
                self.assertLess(
                    representation.index("Unchanged Candidate"), representation.index("Historical Candidate")
                )
            # Product rule changed by #1057 (authorized by the operator in the
            # issue on 2026-11-13): the review is read by the date a book was
            # added, and its totals name the books of the latest run's date and
            # the books marked updated instead of the lifecycle groups.
            self.assertIn(
                "**Всего в обзоре**: добавлено в последний запуск — 1, с отметкой «обновлено» — 1, скрытых редактором — 0.",
                review,
            )
            self.assertIn(
                "Всего: добавлено в последний запуск — 1, с отметкой «обновлено» — 1, видимых — 4", html_review
            )
            self.assertIn('<h2>4 июля 2026 <span class="latest">последний запуск</span>', html_review)
            markdown_rows = markdown_review_rows(review)
            self.assertEqual(markdown_rows["Updated Candidate"]["Добавлено"], "2026-06-27")
            self.assertEqual(markdown_rows["Updated Candidate"]["Обновлено"], "2026-07-04")
            self.assertEqual(markdown_rows["New Candidate"]["Обновлено"], "")
            self.assertIn('<span class="upd">⬆ обновлено 4 июля</span>', html_review)

            summary = json.loads((second_output / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["raw_candidate_count"], 3)
            self.assertEqual(summary["editor_facing_count"], 3)
            contributions = {
                item["source_name"]: item["editor_facing_contribution"] for item in summary["source_contributions"]
            }
            self.assertEqual(contributions, {"Changing Source": 1, "Stable Source": 2})

    def test_complete_two_week_operator_workflow_is_cumulative_and_reversible(self):
        """Exercise the operator seam across lifecycle, state, and formats."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            config = root / "sources.json"

            def candidate(
                title: str, author: str, *, signal: str = "shortlist", category: str = "adult nonfiction"
            ) -> dict[str, object]:
                return {
                    "title": title,
                    "primary_authors": [author],
                    "source_url": f"https://example.test/books/{title.lower().replace(' ', '-')}",
                    "signal_strength": signal,
                    "evidence_note": f"Evidence for {title}.",
                    "candidate_category": category,
                    "content_description": f"Описание {title}.",
                    "russian_edition_status": "not found in Russian",
                    "visible_risk_status": "no visible risk",
                    "visible_risk_status_source": "manual",
                }

            def write_week_two_config() -> None:
                config.write_text(
                    json.dumps(
                        {
                            "run_date": "2026-07-04",
                            "sources": [
                                {
                                    "name": "Stable Source",
                                    "family": "media/curated list",
                                    "url": "https://example.test/stable",
                                    "access_status": "open",
                                    "extraction_method": "fixture",
                                    "language": "en",
                                    "fixture_candidates": [
                                        interested,
                                        unchanged,
                                        reject,
                                        known,
                                        hard_rule,
                                        new_candidate,
                                        russian_transition,
                                        exclusion_transition,
                                    ],
                                },
                                {
                                    "name": "Award Source",
                                    "family": "award",
                                    "url": "https://example.test/awards",
                                    "access_status": "open",
                                    "extraction_method": "fixture",
                                    "language": "en",
                                    "fixture_candidates": [updated],
                                },
                            ],
                        }
                    ),
                    encoding="utf-8",
                )

            def render(output: Path) -> tuple[list[str], list[str], list[str]]:
                self.assertEqual(render_review(output, state_dir), 0)
                with (output / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                    csv_ids = [row["Candidate ID"] for row in csv.DictReader(handle)]
                markdown = (output / "weekly-review.md").read_text(encoding="utf-8")
                html = (output / "weekly-review.html").read_text(encoding="utf-8")
                for identity in csv_ids:
                    self.assertNotIn(identity, markdown)
                    self.assertNotIn(identity, html)
                return csv_ids, [], []

            historical = candidate("Historical Candidate", "Alex Historical")
            unchanged = candidate("Unchanged Candidate", "Sam Stable")
            updated = candidate("Updated Candidate", "Bea Changed", signal="major media list")
            interested = candidate("Interested Candidate", "Casey Interested")
            reject = candidate("Rejected Candidate", "Drew Reject")
            known = candidate("Known Candidate", "Eli Known")
            watch = candidate("Watch Candidate", "Faye Watch")
            deeper = candidate("Deeper Candidate", "Gus Deeper")
            hard_rule = candidate("Rule Candidate", "Alex Rule", signal="winner")
            new_candidate = candidate("New Candidate", "Nina New")
            russian_transition = candidate("Russian Transition", "Rita Russian")
            exclusion_transition = candidate("Exclusion Transition", "Tara Excluded")

            # Week one records a substantial watchlist and all five editor statuses.
            week_one = [
                historical,
                unchanged,
                updated,
                interested,
                reject,
                known,
                watch,
                deeper,
                hard_rule,
                russian_transition,
                exclusion_transition,
            ]
            write_fixture_source_config(config, "2026-06-27", week_one, source_name="Week One Source")
            first_output = root / "week-one"
            run_mvp(config, first_output, state_dir)
            for identity, status, note in (
                ("rejected candidate|drew reject", "reject", "Do not pursue this."),
                ("known candidate|eli known", "already_known", "The publisher already tracks it."),
                ("interested candidate|casey interested", "interested", "Promising acquisition."),
                ("watch candidate|faye watch", "maybe_watch", "Watch for rights."),
                ("deeper candidate|gus deeper", "needs_deeper_analysis", "Prepare a memo."),
            ):
                self.assertEqual(record_feedback(identity, status, note, state_dir, confirmed_by_editor=True), 0)

            # Keep one deliberately old, incomplete record to exercise legacy compatibility.
            history = read_state(state_dir, "candidate_history")
            history.append(
                {
                    "identity_key": "legacy candidate|legacy author",
                    "title": "Legacy Candidate",
                    "last_seen_date": "2026-06-20",
                }
            )
            seed_state(state_dir, candidate_history=history)

            self.assertEqual(
                add_profile_entry(
                    state_dir,
                    kind="hard_rule",
                    scope="author",
                    subject="Alex Rule",
                    quote="Never show Alex Rule.",
                    provenance="editor conversation",
                    created_by="integration-test",
                    confirmed_by_editor=True,
                ),
                0,
            )

            # Week two has one New, one materially Updated, one current unchanged
            # candidate, while prior candidates are silent or suppressed.
            updated["signal_strength"] = "winner"
            updated["source_url"] = "https://example.test/awards/updated"
            updated["evidence_note"] = "A new award-family signal for Updated Candidate."
            russian_transition["russian_edition_status"] = "found in Russian"
            russian_transition["russian_edition_url"] = "https://example.test/ru/russian-transition"
            exclusion_transition["candidate_category"] = "fiction"
            write_week_two_config()
            second_output = root / "week-two"
            run_mvp(config, second_output, state_dir)
            ids, _, _ = render(second_output)

            self.assertEqual(
                ids[:4],
                [
                    "new candidate|nina new",
                    "updated candidate|bea changed",
                    "interested candidate|casey interested",
                    "unchanged candidate|sam stable",
                ],
            )
            # Product rule changed by #1025 (authorized by the operator at the
            # editor's request): the card an active Hard Rule hides is out of
            # the cumulative review, so it shows 12 cards where it showed 13.
            self.assertEqual(len(ids), 12)
            self.assertNotIn("rule candidate|alex rule", ids)
            self.assertIn("historical candidate|alex historical", ids)
            self.assertIn("legacy candidate|legacy author", ids)
            scouting_candidates = json.loads((second_output / "scouting-candidates.json").read_text())
            scouting_ids = {record["identity_key"] for record in scouting_candidates}
            self.assertNotIn("russian transition|rita russian", scouting_ids)
            self.assertNotIn("exclusion transition|tara excluded", scouting_ids)

            with (second_output / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = {row["Candidate ID"]: row for row in csv.DictReader(handle)}
            self.assertEqual(rows["new candidate|nina new"]["Lifecycle"], "New")
            self.assertEqual(rows["updated candidate|bea changed"]["Lifecycle"], "Updated")
            self.assertEqual(rows["interested candidate|casey interested"]["Lifecycle"], "Previously seen")
            self.assertEqual(rows["unchanged candidate|sam stable"]["Lifecycle"], "Previously seen")
            self.assertEqual(rows["interested candidate|casey interested"]["Editor status"], "interested")
            self.assertEqual(rows["interested candidate|casey interested"]["Editor note"], "Promising acquisition.")
            for identity, status, note in (
                ("rejected candidate|drew reject", "reject", "Do not pursue this."),
                ("known candidate|eli known", "already_known", "The publisher already tracks it."),
                ("watch candidate|faye watch", "maybe_watch", "Watch for rights."),
                ("deeper candidate|gus deeper", "needs_deeper_analysis", "Prepare a memo."),
            ):
                self.assertEqual(rows[identity]["Editor status"], status)
                self.assertEqual(rows[identity]["Editor note"], note)

            review = (second_output / "weekly-review.md").read_text(encoding="utf-8")
            html_review = (second_output / "weekly-review.html").read_text(encoding="utf-8")
            self.assertIn(
                "**За эту неделю**: новых — 1, улучшенных — 1, изменённых — 0, без изменений — 2, скрыто правилами — 1.",
                review,
            )
            self.assertIn(
                "**Всего в обзоре**: добавлено в последний запуск — 1, с отметкой «обновлено» — 1, скрытых редактором — 0.",
                review,
            )
            self.assertIn("**Скрыто из всего обзора**: правилами редактора — 1, из отключённых источников — 0.", review)
            self.assertNotIn("taste-0001", review)
            self.assertIn("Не входит в текущие рекомендации: решение редактора — «Не подходит»", review)
            self.assertIn("Не входит в текущие рекомендации: решение редактора — «Уже известно»", review)
            self.assertIn("Исключена из отбора: Есть русское издание", review)
            self.assertIn("Исключена из отбора: художественная литература", review)
            self.assertNotIn("taste-0001", html_review)
            self.assertIn("Исключена из отбора: Есть русское издание", html_review)
            self.assertIn("Исключена из отбора: художественная литература", html_review)

            summary = json.loads((second_output / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["raw_candidate_count"], 9)
            self.assertEqual(summary["editor_facing_count"], 4)
            self.assertEqual(
                summary["update_delta_counts"],
                {"new": 1, "upgraded": 1, "changed": 0, "unchanged": 2},
            )
            self.assertEqual(
                {item["source_name"]: item["editor_facing_contribution"] for item in summary["source_contributions"]},
                {"Award Source": 1, "Stable Source": 3},
            )

            # Hide exactly one historical candidate, then restore it with all context intact.
            self.assertEqual(
                record_visibility(
                    "watch candidate|faye watch",
                    "remove",
                    state_dir,
                    "No longer needed in this review.",
                    "editor conversation",
                ),
                0,
            )
            hidden_ids, _, _ = render(second_output)
            self.assertEqual(len(hidden_ids), 11)
            self.assertEqual(set(hidden_ids), set(ids) - {"watch candidate|faye watch"})
            self.assertNotIn("watch candidate|faye watch", hidden_ids)
            self.assertIn("interested candidate|casey interested", hidden_ids)
            hidden_review = (second_output / "weekly-review.md").read_text(encoding="utf-8")
            self.assertIn("скрытых редактором — 1", hidden_review)
            hidden_history = read_state(state_dir, "candidate_history")
            hidden_record = next(
                item for item in hidden_history if item["identity_key"] == "watch candidate|faye watch"
            )
            self.assertEqual(hidden_record["editor_note"], "Watch for rights.")
            self.assertEqual(hidden_record["source_evidence"][0]["source_url"], watch["source_url"])
            visibility = read_state(state_dir, "candidate_visibility")
            self.assertEqual(visibility[0]["visibility"], "hidden")
            self.assertEqual(visibility[0]["reason"], "No longer needed in this review.")
            self.assertEqual(len(visibility[0]["audit_log"]), 1)

            restored_ids, _, _ = render(second_output)
            self.assertNotIn("watch candidate|faye watch", restored_ids)
            self.assertEqual(
                record_visibility(
                    "watch candidate|faye watch", "restore", state_dir, "Bring it back.", "editor conversation"
                ),
                0,
            )
            restored_ids, _, _ = render(second_output)
            self.assertEqual(restored_ids, ids)
            self.assertEqual(len(restored_ids), 12)
            self.assertIn("watch candidate|faye watch", restored_ids)
            with (second_output / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                restored_rows = {row["Candidate ID"]: row for row in csv.DictReader(handle)}
            self.assertEqual(restored_rows["watch candidate|faye watch"]["Editor status"], "maybe_watch")
            self.assertEqual(restored_rows["watch candidate|faye watch"]["Editor note"], "Watch for rights.")

            # The same unchanged state rerenders byte-for-byte, including all formats.
            saved = {
                name: (second_output / name).read_text(encoding="utf-8")
                for name in ("weekly-review.md", "weekly-review.html", "editor-facing.csv")
            }
            render(second_output)
            for name, contents in saved.items():
                self.assertEqual(contents, (second_output / name).read_text(encoding="utf-8"))

            final_history = read_state(state_dir, "candidate_history")
            legacy_record = next(
                item for item in final_history if item["identity_key"] == "legacy candidate|legacy author"
            )
            self.assertEqual(legacy_record["first_seen_date"], "2026-06-20")
            russian_record = next(
                item for item in final_history if item["identity_key"] == "russian transition|rita russian"
            )
            self.assertEqual(russian_record["russian_edition_status"], "unclear")
            exclusions = read_state(state_dir, "excluded_candidate_history")
            exclusion_by_id = {item["identity_key"]: item for item in exclusions}
            self.assertEqual(
                exclusion_by_id["russian transition|rita russian"]["russian_edition_url"],
                "https://example.test/ru/russian-transition",
            )
            self.assertEqual(exclusion_by_id["exclusion transition|tara excluded"]["exclusion_reason"], "fiction")
            visibility = read_state(state_dir, "candidate_visibility")
            self.assertEqual(visibility[0]["visibility"], "visible")
            self.assertEqual(len(visibility[0]["audit_log"]), 2)

    def test_hard_rule_context_remains_auditable_in_cumulative_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            config = root / "sources.json"
            candidate = {
                "title": "Rule-bound Candidate",
                "primary_authors": ["Alex Rule"],
                "source_url": "https://example.test/rule-bound",
                "signal_strength": "winner",
                "evidence_note": "A configured source selected this book.",
                "candidate_category": "adult nonfiction",
                "russian_edition_status": "not found in Russian",
                "visible_risk_status": "no visible risk",
            }

            write_fixture_source_config(
                config,
                "2026-07-04",
                [candidate],
                source_name="Rule Test Source",
                source_url="https://example.test/rules",
            )
            first = run_autoscout(
                "run-mvp", "--config", str(config), "--output-dir", str(root / "first"), "--state-dir", str(state_dir)
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(
                add_profile_entry(
                    state_dir,
                    kind="hard_rule",
                    scope="author",
                    subject="Alex Rule",
                    quote="Never show Alex Rule.",
                    provenance="editor conversation",
                    created_by="test",
                    confirmed_by_editor=True,
                ),
                0,
            )

            write_fixture_source_config(
                config,
                "2026-07-11",
                [candidate],
                source_name="Rule Test Source",
                source_url="https://example.test/rules",
            )
            second_output = root / "second"
            second = run_autoscout(
                "run-mvp", "--config", str(config), "--output-dir", str(second_output), "--state-dir", str(state_dir)
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            hidden = json.loads((second_output / "hidden-by-rule.json").read_text(encoding="utf-8"))
            self.assertEqual(hidden[0]["hidden_by_rule_id"], "taste-0001")
            self.assertEqual(
                run_autoscout(
                    "render-review", "--output-dir", str(second_output), "--state-dir", str(state_dir)
                ).returncode,
                0,
            )

            review = (second_output / "weekly-review.md").read_text(encoding="utf-8")
            html = (second_output / "weekly-review.html").read_text(encoding="utf-8")
            with (second_output / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            # Product rule changed by #1025 (authorized by the operator at the
            # editor's request): while its Hard Rule is active, a card already
            # in Candidate History is out of the cumulative review too. Before,
            # it stayed there as a previously seen row with a rule note. The
            # audit moved to hidden-from-review.json.
            self.assertEqual(rows, [])
            for representation in (review, html):
                self.assertNotIn("taste-0001", representation)
                self.assertNotIn("Rule-bound Candidate", representation)
                self.assertIn("правилами редактора — 1", representation)
            (withheld,) = json.loads((second_output / "hidden-from-review.json").read_text(encoding="utf-8"))
            self.assertEqual(withheld["identity_key"], "rule bound candidate|alex rule")
            self.assertEqual(withheld["hidden_by"], "hard_rule")
            self.assertEqual(withheld["hidden_by_rule_id"], "taste-0001")
            self.assertIn("Never show Alex Rule.", withheld["reason"])

            self.assertEqual(
                revoke_profile_entry(
                    state_dir, "taste-0001", "No longer applicable.", "test", confirmed_by_editor=True
                ),
                0,
            )
            write_fixture_source_config(
                config,
                "2026-07-18",
                [],
                source_name="Rule Test Source",
                source_url="https://example.test/rules",
            )
            third_output = root / "third"
            third = run_autoscout(
                "run-mvp", "--config", str(config), "--output-dir", str(third_output), "--state-dir", str(state_dir)
            )
            self.assertEqual(third.returncode, 0, third.stderr)
            self.assertEqual(
                run_autoscout(
                    "render-review", "--output-dir", str(third_output), "--state-dir", str(state_dir)
                ).returncode,
                0,
            )
            with (third_output / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                third_rows = list(csv.DictReader(handle))
            # Revoking the rule returns the card, with the note that it was hidden.
            self.assertEqual([row["Candidate ID"] for row in third_rows], ["rule bound candidate|alex rule"])
            self.assertIn("Hard Rule taste-0001 applied", third_rows[0]["Review context"])
            self.assertNotIn("Current recommendation suppressed", third_rows[0]["Review context"])
            self.assertNotIn("Hard Rule taste-0001", third_rows[0]["Annotation"])
            self.assertNotIn("Current recommendation suppressed", third_rows[0]["Annotation"])
            third_review = (third_output / "weekly-review.md").read_text(encoding="utf-8")
            third_html = (third_output / "weekly-review.html").read_text(encoding="utf-8")
            for representation in (third_review, third_html):
                self.assertNotIn("taste-0001", representation)
                self.assertIn("Ранее скрыта редакторским правилом", representation)
            # Product rule changed by #1057: the returned card is listed under its
            # date without the updated mark, not in a «Ранее просмотренные» group.
            self.assertEqual(markdown_review_rows(third_review)["Rule-bound Candidate"]["Обновлено"], "")
            self.assertNotIn("Ранее просмотренные", third_review)
            self.assertFalse((third_output / "hidden-from-review.json").exists())

    def test_exclusion_only_candidate_stays_out_of_cumulative_editor_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            config = root / "sources.json"
            write_fixture_source_config(
                config,
                "2026-07-11",
                [
                    {
                        "title": "Never Previously Seen",
                        "primary_authors": ["Alex Excluded"],
                        "source_url": "https://example.test/excluded",
                        "russian_edition_status": "found in Russian",
                        "russian_edition_url": "https://example.test/ru/excluded",
                    }
                ],
            )
            output = root / "output"
            self.assertEqual(
                run_autoscout(
                    "run-mvp", "--config", str(config), "--output-dir", str(output), "--state-dir", str(state_dir)
                ).returncode,
                0,
            )
            self.assertEqual(
                run_autoscout("render-review", "--output-dir", str(output), "--state-dir", str(state_dir)).returncode,
                0,
            )

            with (output / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            review = (output / "weekly-review.md").read_text(encoding="utf-8")
            excluded_history = read_state(state_dir, "excluded_candidate_history")
            self.assertEqual(rows, [])
            self.assertNotIn("Never Previously Seen", review)
            self.assertEqual(excluded_history[0]["identity_key"], "never previously seen|alex excluded")

    def test_material_change_preserves_hard_rule_and_status_contexts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            config = root / "sources.json"
            candidate = {
                "title": "Rule-bound Rejection",
                "primary_authors": ["Alex Rule"],
                "source_url": "https://example.test/rule-bound-rejection",
                "signal_strength": "major media list",
                "evidence_note": "Initial rejection evidence.",
                "candidate_category": "adult nonfiction",
                "russian_edition_status": "not found in Russian",
            }
            write_fixture_source_config(config, "2026-07-04", [candidate])
            first_output = root / "first"
            self.assertEqual(
                run_autoscout(
                    "run-mvp", "--config", str(config), "--output-dir", str(first_output), "--state-dir", str(state_dir)
                ).returncode,
                0,
            )
            self.assertEqual(
                record_feedback(
                    "rule bound rejection|alex rule", "reject", "Not for us.", state_dir, confirmed_by_editor=True
                ),
                0,
            )
            self.assertEqual(
                add_profile_entry(
                    state_dir,
                    kind="hard_rule",
                    scope="author",
                    subject="Alex Rule",
                    quote="Never show Alex Rule.",
                    provenance="editor conversation",
                    created_by="test",
                    confirmed_by_editor=True,
                ),
                0,
            )

            config_data = json.loads(config.read_text(encoding="utf-8"))
            config_data["run_date"] = "2026-07-11"
            config_data["sources"].append(
                {
                    "name": "Fixture Awards",
                    "family": "award",
                    "url": "https://example.test/awards",
                    "access_status": "open",
                    "extraction_method": "fixture",
                    "check_frequency": "weekly",
                    "language": "en",
                    "notes": "Material-change fixture source.",
                    "fixture_candidates": [
                        {
                            **candidate,
                            "source_url": "https://example.test/awards/rule-bound-rejection",
                            "signal_strength": "winner",
                            "evidence_note": "New award-family evidence.",
                        }
                    ],
                }
            )
            config.write_text(json.dumps(config_data), encoding="utf-8")
            second_output = root / "second"
            self.assertEqual(
                run_autoscout(
                    "run-mvp",
                    "--config",
                    str(config),
                    "--output-dir",
                    str(second_output),
                    "--state-dir",
                    str(state_dir),
                ).returncode,
                0,
            )
            self.assertEqual(
                run_autoscout(
                    "render-review", "--output-dir", str(second_output), "--state-dir", str(state_dir)
                ).returncode,
                0,
            )

            with (second_output / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            # Product rule changed by #1025 (authorized by the operator at the
            # editor's request): a Material Change does not return a card its
            # Hard Rule still hides. The rule and the editor's decision stay
            # on record in the audit file and in Candidate History.
            self.assertEqual(rows, [])
            (withheld,) = json.loads((second_output / "hidden-from-review.json").read_text(encoding="utf-8"))
            self.assertEqual(withheld["hidden_by_rule_id"], "taste-0001")
            self.assertEqual(withheld["editor_status"], "reject")
            history = read_state(state_dir, "candidate_history")
            self.assertEqual(history[0]["hidden_by_rule_id"], "taste-0001")
            self.assertEqual(history[0]["editor_status"], "reject")

    def test_reject_and_already_known_context_remains_in_cumulative_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            config = root / "sources.json"
            candidates = [
                {
                    "title": "Rejected Historical",
                    "primary_authors": ["Alex Reject"],
                    "source_url": "https://example.test/rejected",
                    "signal_strength": "shortlist",
                    "evidence_note": "Rejection test evidence.",
                    "candidate_category": "adult nonfiction",
                    "russian_edition_status": "not found in Russian",
                },
                {
                    "title": "Known Historical",
                    "primary_authors": ["Bea Known"],
                    "source_url": "https://example.test/known",
                    "signal_strength": "shortlist",
                    "evidence_note": "Already-known test evidence.",
                    "candidate_category": "adult nonfiction",
                    "russian_edition_status": "not found in Russian",
                },
            ]
            config.write_text(
                json.dumps(
                    {
                        "run_date": "2026-07-04",
                        "sources": [
                            {
                                "name": "Suppression Test Source",
                                "family": "media/curated list",
                                "url": "https://example.test/suppression",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "language": "en",
                                "fixture_candidates": candidates,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            first_output = root / "first"
            self.assertEqual(
                run_autoscout(
                    "run-mvp", "--config", str(config), "--output-dir", str(first_output), "--state-dir", str(state_dir)
                ).returncode,
                0,
            )
            self.assertEqual(
                record_feedback(
                    "rejected historical|alex reject",
                    "reject",
                    "Do not pursue this.",
                    state_dir,
                    confirmed_by_editor=True,
                ),
                0,
            )
            self.assertEqual(
                record_feedback(
                    "known historical|bea known",
                    "already_known",
                    "The publisher already tracks it.",
                    state_dir,
                    confirmed_by_editor=True,
                ),
                0,
            )
            config_data = json.loads(config.read_text(encoding="utf-8"))
            config_data["run_date"] = "2026-07-11"
            config.write_text(json.dumps(config_data), encoding="utf-8")
            second_output = root / "second"
            self.assertEqual(
                run_autoscout(
                    "run-mvp",
                    "--config",
                    str(config),
                    "--output-dir",
                    str(second_output),
                    "--state-dir",
                    str(state_dir),
                ).returncode,
                0,
            )
            self.assertEqual(
                run_autoscout(
                    "render-review", "--output-dir", str(second_output), "--state-dir", str(state_dir)
                ).returncode,
                0,
            )
            review = (second_output / "weekly-review.md").read_text(encoding="utf-8")
            with (second_output / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual({row["Title"] for row in rows}, {"Rejected Historical", "Known Historical"})
            self.assertEqual({row["Editor status"] for row in rows}, {"reject", "already_known"})
            for row in rows:
                self.assertIn("Suppressed from current recommendations", row["Review context"])
                self.assertNotIn("Suppressed from current recommendations", row["Annotation"])
            self.assertIn("решение редактора — «Не подходит»", review)
            self.assertIn("решение редактора — «Уже известно»", review)

    def test_found_in_russian_transition_is_explicit_for_previous_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            config = root / "sources.json"
            candidate = {
                "title": "Russian Edition Transition",
                "primary_authors": ["Casey Transition"],
                "source_url": "https://example.test/transition",
                "signal_strength": "major media list",
                "evidence_note": "Transition test evidence.",
                "candidate_category": "adult nonfiction",
                "russian_edition_status": "not found in Russian",
            }

            def write_config(run_date: str, status: str | None, edition_url: str = "") -> None:
                current = dict(candidate)
                if status is None:
                    current.pop("russian_edition_status", None)
                else:
                    current["russian_edition_status"] = status
                # The second run must receive its positive status from the
                # candidate-scoped provider fixture below, not from source
                # metadata that has not been verified by a provider.
                config.write_text(
                    json.dumps(
                        {
                            "run_date": run_date,
                            "sources": [
                                {
                                    "name": "Russian Transition Source",
                                    "family": "media/curated list",
                                    "url": "https://example.test/transitions",
                                    "access_status": "open",
                                    "extraction_method": "fixture",
                                    "language": "en",
                                    "fixture_candidates": [current],
                                }
                            ],
                            "russian_edition_checks": (
                                [
                                    {
                                        "retailer": "LitRes",
                                        "fixture_results": [
                                            {
                                                "candidate_title": "Russian Edition Transition",
                                                "primary_authors": ["Casey Transition"],
                                                "match_type": "same-work Russian-language edition",
                                                "language": "Russian",
                                                "edition_url": edition_url,
                                            }
                                        ],
                                    }
                                ]
                                if edition_url
                                else []
                            ),
                        }
                    ),
                    encoding="utf-8",
                )

            write_config("2026-07-04", "not found in Russian")
            first_output = root / "first"
            self.assertEqual(
                run_autoscout(
                    "run-mvp", "--config", str(config), "--output-dir", str(first_output), "--state-dir", str(state_dir)
                ).returncode,
                0,
            )
            self.assertEqual(
                record_feedback(
                    "russian edition transition|casey transition",
                    "interested",
                    "Keep tracking this.",
                    state_dir,
                    confirmed_by_editor=True,
                ),
                0,
            )

            write_config("2026-07-11", None, "https://example.test/ru-edition")
            second_output = root / "second"
            self.assertEqual(
                run_autoscout(
                    "run-mvp",
                    "--config",
                    str(config),
                    "--output-dir",
                    str(second_output),
                    "--state-dir",
                    str(state_dir),
                ).returncode,
                0,
            )
            self.assertEqual(json.loads((second_output / "scouting-candidates.json").read_text(encoding="utf-8")), [])
            exclusions = read_state(state_dir, "excluded_candidate_history")
            self.assertEqual(exclusions[0]["exclusion_reason"], "found in Russian")
            self.assertEqual(exclusions[0]["russian_edition_url"], "https://example.test/ru-edition")
            self.assertEqual(
                run_autoscout(
                    "render-review", "--output-dir", str(second_output), "--state-dir", str(state_dir)
                ).returncode,
                0,
            )
            review = (second_output / "weekly-review.md").read_text(encoding="utf-8")
            html = (second_output / "weekly-review.html").read_text(encoding="utf-8")
            with (second_output / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["Russian-edition status"], "found in Russian")
            self.assertEqual(rows[0]["Russian-edition URL"], "https://example.test/ru-edition")
            for representation in (review, html):
                self.assertIn("Исключена из отбора: Есть русское издание", representation)
                self.assertIn("https://example.test/ru-edition", representation)
            self.assertIn("Excluded from acquisition: found in Russian", str(rows))
            self.assertIn("Интересно", review)
            self.assertIn("Keep tracking this.", review)

    def test_statuses_and_non_russian_exclusion_survive_source_absence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            config = root / "sources.json"

            def candidate(title: str, author: str, category: str = "adult nonfiction") -> dict[str, object]:
                return {
                    "title": title,
                    "primary_authors": [author],
                    "source_url": f"https://example.test/statuses/{title.lower().replace(' ', '-')}",
                    "signal_strength": "shortlist",
                    "evidence_note": f"Evidence for {title}.",
                    "candidate_category": category,
                    "russian_edition_status": "not found in Russian",
                    "visible_risk_status": "no visible risk",
                    "content_description": f"Описание {title}.",
                }

            watch = candidate("Watch Across Absence", "Alex Watch")
            deeper = candidate("Deeper Across Absence", "Bea Deeper")
            transition = candidate("Category Transition", "Casey Transition")
            write_fixture_source_config(
                config,
                "2026-07-04",
                [watch, deeper, transition],
                source_name="Cumulative Status Source",
                source_url="https://example.test/statuses",
            )
            first_output = root / "first"
            first = run_autoscout(
                "run-mvp", "--config", str(config), "--output-dir", str(first_output), "--state-dir", str(state_dir)
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(
                record_feedback(
                    "watch across absence|alex watch",
                    "maybe_watch",
                    "Keep watching.",
                    state_dir,
                    confirmed_by_editor=True,
                ),
                0,
            )
            self.assertEqual(
                record_feedback(
                    "deeper across absence|bea deeper",
                    "needs_deeper_analysis",
                    "Prepare a memo.",
                    state_dir,
                    confirmed_by_editor=True,
                ),
                0,
            )

            write_fixture_source_config(
                config,
                "2026-07-11",
                [candidate("Category Transition", "Casey Transition", "fiction")],
                source_name="Cumulative Status Source",
                source_url="https://example.test/statuses",
            )
            second_output = root / "second"
            second = run_autoscout(
                "run-mvp", "--config", str(config), "--output-dir", str(second_output), "--state-dir", str(state_dir)
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(
                run_autoscout(
                    "render-review", "--output-dir", str(second_output), "--state-dir", str(state_dir)
                ).returncode,
                0,
            )
            second_review = (second_output / "weekly-review.md").read_text(encoding="utf-8")
            with (second_output / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                second_rows = list(csv.DictReader(handle))
            self.assertEqual(
                {row["Candidate ID"] for row in second_rows},
                {
                    "watch across absence|alex watch",
                    "deeper across absence|bea deeper",
                    "category transition|casey transition",
                },
            )
            statuses = {row["Candidate ID"]: row for row in second_rows}
            self.assertEqual(statuses["watch across absence|alex watch"]["Editor status"], "maybe_watch")
            self.assertEqual(statuses["deeper across absence|bea deeper"]["Editor status"], "needs_deeper_analysis")
            self.assertEqual(
                statuses["category transition|casey transition"]["Russian-edition status"],
                "unclear",
            )
            self.assertIn("Наблюдать", second_review)
            self.assertIn("Нужен углублённый анализ", second_review)
            self.assertIn("Исключена из отбора: художественная литература", second_review)
            self.assertEqual(
                statuses["category transition|casey transition"]["Review context"],
                "Excluded from acquisition: fiction on 2026-07-11.",
            )
            self.assertNotIn(
                "Excluded from acquisition: fiction", statuses["category transition|casey transition"]["Annotation"]
            )

            write_fixture_source_config(
                config,
                "2026-07-18",
                [],
                source_name="Cumulative Status Source",
                source_url="https://example.test/statuses",
            )
            third_output = root / "third"
            third = run_autoscout(
                "run-mvp", "--config", str(config), "--output-dir", str(third_output), "--state-dir", str(state_dir)
            )
            self.assertEqual(third.returncode, 0, third.stderr)
            self.assertEqual(
                run_autoscout(
                    "render-review", "--output-dir", str(third_output), "--state-dir", str(state_dir)
                ).returncode,
                0,
            )
            third_review = (third_output / "weekly-review.md").read_text(encoding="utf-8")
            with (third_output / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                third_rows = list(csv.DictReader(handle))
            self.assertEqual(
                [row["Candidate ID"] for row in third_rows],
                [
                    "category transition|casey transition",
                    "deeper across absence|bea deeper",
                    "watch across absence|alex watch",
                ],
            )
            self.assertEqual(third_rows[1]["Editor status"], "needs_deeper_analysis")
            self.assertEqual(third_rows[2]["Editor status"], "maybe_watch")
            self.assertEqual(
                third_rows[0]["Review context"],
                "Excluded from acquisition: fiction on 2026-07-11.",
            )
            self.assertIn("Исключена из отбора: художественная литература", third_review)
            self.assertNotIn("internal-exclusions", third_review)

    def test_degraded_run_diagnostics_point_to_artifacts_without_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-06-27",
                        "pilot_mode": True,
                        "editor_row_target": 3,
                        "pilot_row_min": 3,
                        "pilot_row_max": 3,
                        "sources": [
                            {
                                "name": "Small Working Source",
                                "family": "media/curated list",
                                "url": "https://example.test/small",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "language": "en",
                                "fixture_candidates": [
                                    {
                                        "title": "Only Candidate",
                                        "primary_authors": ["Nina Source"],
                                        "source_url": "https://example.test/small/book",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Small source evidence.",
                                        "candidate_category": "adult nonfiction",
                                        "russian_edition_status": "not found in Russian",
                                        "visible_risk_status": "no visible risk",
                                        "visible_risk_status_source": "manual",
                                    }
                                ],
                            },
                            {
                                "name": "Paid Source",
                                "family": "rights/deal",
                                "url": "https://example.test/paid",
                                "access_status": "paid",
                                "extraction_method": "fixture",
                                "language": "en",
                                "notes": "Paid source is intentionally skipped.",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            output_dir = tmp_path / "run"
            result = run_autoscout(
                "run-mvp",
                "--config",
                str(config_path),
                "--output-dir",
                str(output_dir),
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            pilot_status = json.loads((output_dir / "pilot-status.json").read_text(encoding="utf-8"))
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            source_runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))

            self.assertFalse(pilot_status["ready_for_editor"])
            caveats_text = " ".join(pilot_status["caveats"])
            self.assertIn("configured source skipped", caveats_text)
            self.assertIn("fewer than target 3", caveats_text)
            self.assertEqual(summary["editor_facing_count"], 1)
            self.assertEqual(summary["skipped_source_count"], 1)
            self.assertEqual(summary["failed_source_count"], 0)
            self.assertIn("source scarcity", summary["shortfall_reason"])
            self.assertIn("configured source skipped", summary["shortfall_reason"])

            paid_run = next(run for run in source_runs if run["source_name"] == "Paid Source")
            self.assertEqual(paid_run["status"], "skipped")
            self.assertIn("paid", paid_run["reason"].lower())

            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)

    def test_full_codex_workflow_prepare_finalize_run_produces_expected_artifacts(self):
        """Integration: prepare-codex-extraction (live) → save mock Codex
        extraction artifacts → finalize-codex-run → run-mvp produces
        editor-facing output.  Proves the documented fresh-scout flow works
        end-to-end without API credentials, using fixture page data and
        deterministic extraction responses."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "runs"

            # Build a live config with one source that has a page fixture
            # (so prepare-codex-extraction can run without network).
            page_path = tmp_path / "page.json"
            page_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": '<a href="https://example.test/books/test-book">Test Book</a> by Alex Example',
                    }
                ),
                encoding="utf-8",
            )
            live_config_path = tmp_path / "live-config.json"
            live_config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-07-04",
                        "pilot_mode": True,
                        "editor_row_target": 10,
                        "pilot_row_min": 10,
                        "pilot_row_max": 10,
                        "sources": [
                            {
                                "name": "Test Source",
                                "family": "media/curated list",
                                "url": "https://example.test/list",
                                "active": True,
                                "access_status": "open",
                                "extraction_method": "codex-extraction-replay",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Integration test source.",
                                "source_page_fixture_file": str(page_path),
                            }
                        ],
                        "russian_edition_checks": [],
                    }
                ),
                encoding="utf-8",
            )

            # Step 3: prepare-codex-extraction (fixture file serves as captured page).
            result = run_autoscout(
                "prepare-codex-extraction",
                "--config",
                str(live_config_path),
                "--output-dir",
                str(output_dir),
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("captured_source_count=1", result.stdout)

            # Verify capture file and worklist were written.
            worklist_path = output_dir / "codex-extraction-worklist.json"
            self.assertTrue(worklist_path.exists())
            worklist = json.loads(worklist_path.read_text(encoding="utf-8"))
            self.assertEqual(len(worklist["work_items"]), 1)
            capture_file = worklist["work_items"][0]["capture_file"]
            self.assertTrue((output_dir / capture_file).exists())

            # Step 4: simulate Codex extraction — write a deterministic
            # extraction artifact using the expected naming convention
            # (slug + URL-hash disambiguator).
            work_item = worklist["work_items"][0]
            extraction_slug = work_item["expected_extraction_file"]
            extraction_path = output_dir / extraction_slug
            extraction_path.write_text(
                json.dumps(
                    {
                        # A conforming artifact echoes the identity of the work
                        # item it answers, so finalization can bind it to this
                        # operation rather than accept any file of any age
                        # (#384), and so the validator accepts it at all (#386).
                        # A standalone prepare stamps no operation_id or
                        # capture digest — the weekly workflow does that — so
                        # this artifact echoes only what the work item claims,
                        # and finalization reports it as unbound rather than
                        # guessing it fresh or stale.
                        "source_name": work_item["source_name"],
                        "source_url": work_item["source_url"],
                        "entries": [
                            {
                                "title": "Test Book",
                                "primary_authors": ["Alex Example"],
                                "source_url": "https://example.test/books/test-book",
                                "evidence_url": "https://example.test/books/test-book",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Integration test extraction.",
                                "candidate_category": "adult nonfiction",
                                "russian_edition_status": "not found in Russian",
                                "visible_risk_status": "no visible risk",
                                "visible_risk_status_source": "manual",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            # Step 5: finalize-codex-run generates the replay config.
            result = run_autoscout(
                "finalize-codex-run",
                "--output-dir",
                str(output_dir),
                "--live-config",
                str(live_config_path),
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("ready_sources=1", result.stdout)
            self.assertIn("pending_sources=0", result.stdout)

            replay_config_path = output_dir / "codex-replay-config.json"
            self.assertTrue(replay_config_path.exists())
            replay_config = json.loads(replay_config_path.read_text(encoding="utf-8"))
            self.assertEqual(len(replay_config["sources"]), 1)
            source = replay_config["sources"][0]
            self.assertEqual(source["extraction_method"], "codex-extraction-replay")
            self.assertEqual(source["source_page_fixture_file"], capture_file)
            self.assertEqual(source["extraction_response_file"], extraction_slug)
            self.assertEqual(source["codex_artifact_dir"], ".")
            self.assertTrue(replay_config["pilot_mode"])
            self.assertEqual(replay_config["editor_row_target"], 10)

            # Step 6: run-mvp ingests the replay config and produces output.
            state_dir = tmp_path / "state"
            result = run_autoscout(
                "run-mvp",
                "--config",
                str(replay_config_path),
                "--output-dir",
                str(output_dir),
                "--state-dir",
                str(state_dir),
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            # Verify pipeline artifacts.
            self.assertTrue((output_dir / "source-mentions.json").exists())
            self.assertTrue((output_dir / "scouting-candidates.json").exists())
            self.assertTrue((output_dir / "editor-facing.csv").exists())
            self.assertTrue((output_dir / "run-summary.json").exists())
            self.assertTrue((output_dir / "pilot-status.json").exists())

            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["Title"], "Test Book")

            # run-summary reports codex-capture.
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertIn("codex-capture", summary["collection_modes"])

            # run-health reports codex-capture.
            result = run_autoscout("run-health", "--output-dir", str(output_dir))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("collection_mode=codex-capture", result.stdout)
            self.assertIn("source_codex=", result.stdout)


if __name__ == "__main__":
    unittest.main()
