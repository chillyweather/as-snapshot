import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from state_store_support import seed_state

from autoscout import russian_editions as autoscout_main
from autoscout.cli.weekly import run_mvp as _cli_run_mvp
from autoscout.content_description import DESCRIPTION_FALLBACK
from tests.fixtures import copy_fixture

REPO_ROOT = Path(__file__).resolve().parents[1]


class _InProcessResult:
    __slots__ = ("returncode", "stderr", "stdout")

    def __init__(self, returncode: int, stderr: str = "", stdout: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


def has_cyrillic(value: str) -> bool:
    return any("А" <= char <= "я" or char == "ё" or char == "Ё" for char in value)


def run_pipeline(tmp_path: Path, config_path: Path, output_dir: Path | None = None):
    if output_dir is None:
        output_dir = tmp_path / "runs"
    captured = io.StringIO()
    try:
        with redirect_stderr(captured):
            _cli_run_mvp(config_path, output_dir)
        return _InProcessResult(0, captured.getvalue()), output_dir
    except SystemExit as e:
        return _InProcessResult(e.code if e.code is not None else 1, captured.getvalue()), output_dir
    except ValueError as e:
        return _InProcessResult(1, captured.getvalue() + str(e)), output_dir
    except Exception as e:
        return _InProcessResult(1, captured.getvalue() + "\n" + str(e)), output_dir


class MvpPipelineTests(unittest.TestCase):
    def test_editor_facing_csv_uses_stable_external_contract_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
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
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "The Useful Future",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/awards/useful-future",
                                        "signal_strength": "shortlist",
                                        "evidence_note": "Shortlisted for a fixture award.",
                                        "candidate_category": "popular science",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                reader = csv.reader(handle)
                header = next(reader)
            self.assertEqual(
                header,
                [
                    "Title",
                    "Author",
                    "Annotation",
                    "Description",
                    "Description status",
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
                    "Editor status",
                    "Editor note",
                    "Candidate ID",
                    "Durable Candidate ID",
                ],
            )

    def test_editor_csv_has_one_row_per_visible_candidate_with_primary_signal_and_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
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
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "The Useful Future",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/awards/useful-future",
                                        "signal_strength": "shortlist",
                                        "evidence_note": "Shortlisted for a fixture award.",
                                        "candidate_category": "popular science",
                                    }
                                ],
                            },
                            {
                                "name": "Fixture Ranking",
                                "family": "bestseller/ranking",
                                "url": "https://example.test/ranking",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "The Useful Future",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/ranking/useful-future",
                                        "signal_strength": "top ranking",
                                        "evidence_note": "Reached a top nonfiction ranking.",
                                        "candidate_category": "popular science",
                                    }
                                ],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["Check date"], "2026-05-21")
            self.assertEqual(rows[0]["Source"], "Fixture Ranking")
            self.assertEqual(rows[0]["Source URL"], "https://example.test/ranking/useful-future")
            self.assertEqual(rows[0]["Signal count"], "2")
            self.assertIn("award", rows[0]["Signal summary"])
            self.assertIn("bestseller/ranking", rows[0]["Signal summary"])
            self.assertNotIn("Source family", rows[0])
            self.assertEqual(
                rows[0]["Links"],
                "https://example.test/awards/useful-future | https://example.test/ranking/useful-future",
            )

    def test_mvp_pipeline_writes_editor_csv_and_run_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "Fixture Open Awards",
                                "family": "award",
                                "url": "https://example.test/awards",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source for MVP skeleton.",
                                "fixture_candidates": [
                                    {
                                        "title": "The Useful Future",
                                        "author": "Alex Example",
                                        "publisher": "Example Press",
                                        "publication_year": "2026",
                                        "topic": "popular science",
                                        "short_description": "A concise metadata-only description.",
                                        "why_it_entered": "Award shortlist signal.",
                                        "russian_edition_status": "not found in Russian",
                                        "visible_risk_status": "no visible risk",
                                        "visible_risk_status_source": "manual",
                                        "visible_risk_status_note": "Fixture row marked as reviewed for baseline contract testing.",
                                        "priority_label": "high",
                                        "source_url": "https://example.test/awards/useful-future",
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            weekly_report = output_dir / "editor-facing.csv"
            run_summary = output_dir / "run-summary.json"
            source_runs = output_dir / "source-runs.json"
            source_mentions = output_dir / "source-mentions.json"

            self.assertTrue(weekly_report.exists())
            self.assertTrue(run_summary.exists())
            self.assertTrue(source_runs.exists())
            self.assertTrue(source_mentions.exists())

            with weekly_report.open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["Title"] for row in rows], ["The Useful Future"])
            self.assertEqual(rows[0]["Source"], "Fixture Open Awards")
            self.assertEqual(rows[0]["Source URL"], "https://example.test/awards/useful-future")

            summary = json.loads(run_summary.read_text(encoding="utf-8"))
            self.assertEqual(summary["skipped_source_count"], 0)
            self.assertEqual(summary["failed_source_count"], 0)
            self.assertEqual(summary["ignored_source_count"], 0)
            self.assertEqual(summary["editor_facing_count"], 1)
            self.assertEqual(summary["editor_row_target"], 10)
            self.assertIn("fewer than 10", summary["shortfall_reason"])
            self.assertIn("no pipeline failure", summary["shortfall_reason"])
            self.assertIn("1 editor-facing rows produced; fewer than target 10", summary["major_warnings"])
            self.assertIn("source_contributions", summary)
            self.assertEqual(len(summary["source_contributions"]), 1)
            self.assertEqual(summary["source_contributions"][0]["source_name"], "Fixture Open Awards")
            self.assertEqual(summary["source_contributions"][0]["mention_count"], 1)
            self.assertEqual(summary["source_contributions"][0]["editor_facing_contribution"], 1)

    def test_open_configured_sources_produce_source_mentions(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-21",
                        "sources": [
                            {
                                "name": "Fixture Open Awards",
                                "family": "award",
                                "url": "https://example.test/awards",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "The Useful Future",
                                        "primary_authors": ["Alex Example"],
                                        "english_title": "The Useful Future",
                                        "english_author_display": "Alex Example",
                                        "english_annotation": "English helper note from source metadata.",
                                        "search_translation": "Полезное будущее",
                                        "author_transliteration_note": "Алекс Экземпл",
                                        "source_url": "https://example.test/awards/useful-future",
                                        "signal_type": "award",
                                        "signal_strength": "shortlist",
                                        "evidence_note": "Listed as a shortlist title on Fixture Open Awards.",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            mentions = json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8"))
            self.assertEqual(
                mentions,
                [
                    {
                        "title": "The Useful Future",
                        "primary_authors": ["Alex Example"],
                        "candidate_category": "",
                        "publisher": "",
                        "publication_year": "",
                        "publisher_fit": "",
                        "russian_edition_status": "",
                        "russian_edition_url": "",
                        "russian_edition_status_source": "",
                        "russian_edition_status_note": "",
                        "visible_risk_status": "",
                        "visible_risk_category": "",
                        "visible_risk_comment": "",
                        "visible_risk_status_source": "",
                        "visible_risk_status_note": "",
                        "public_data_status": "",
                        "short_description": "",
                        "content_description": "",
                        "english_title": "The Useful Future",
                        "english_title_type": "original",
                        "english_title_provenance": {"kind": "original", "research_sources": []},
                        "english_author_display": "Alex Example",
                        "english_annotation": "English helper note from source metadata.",
                        "risk_evidence_url": "",
                        "risk_signal_scope": "",
                        "publication_status": "",
                        "isbn": "",
                        "search_translation": "Полезное будущее",
                        "author_transliteration_note": "Алекс Экземпл",
                        "source_url": "https://example.test/awards/useful-future",
                        "evidence_url": "",
                        "evidence_locator": "",
                        "source_name": "Fixture Open Awards",
                        "source_family": "award",
                        "language": "en",
                        "source_check_date": "2026-05-21",
                        "signal_type": "award",
                        "signal_strength": "shortlist",
                        "evidence_note": "Listed as a shortlist title on Fixture Open Awards.",
                    }
                ],
            )

    def test_configured_sources_can_load_recorded_source_mentions_from_separate_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            recorded_path = tmp_path / "recorded-media.json"
            recorded_path.write_text(
                json.dumps(
                    {
                        "mentions": [
                            {
                                "title": "The Real List Book",
                                "primary_authors": ["Alex Example"],
                                "source_url": "https://example.test/media/real-list-book",
                                "source_check_date": "2026-05-21",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Included in an open media list as adult nonfiction.",
                                "candidate_category": "business",
                                "russian_edition_status": "not found in Russian",
                                "visible_risk_status": "no visible risk",
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
                        "sources": [
                            {
                                "name": "Recorded Media List",
                                "family": "media/curated list",
                                "url": "https://example.test/media",
                                "access_status": "open",
                                "extraction_method": "recorded",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Recorded source mention trial data.",
                                "recorded_mentions_files": [str(recorded_path)],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            mentions = json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8"))
            self.assertEqual(len(mentions), 1)
            self.assertEqual(mentions[0]["title"], "The Real List Book")
            self.assertEqual(mentions[0]["source_name"], "Recorded Media List")
            self.assertEqual(mentions[0]["source_family"], "media/curated list")
            self.assertEqual(mentions[0]["source_check_date"], "2026-05-21")
            self.assertEqual(mentions[0]["signal_type"], "media/curated list")
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["Title"] for row in rows], ["The Real List Book"])

    def test_configured_collector_fixture_source_produces_source_mentions(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-21",
                        "sources": [
                            {
                                "name": "Collector Media List",
                                "family": "media/curated list",
                                "url": "https://example.test/media",
                                "access_status": "open",
                                "extraction_method": "collector-fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic collector contract source.",
                                "collector_mentions": [
                                    {
                                        "title": "Collected Book",
                                        "primary_authors": ["Riley Example"],
                                        "source_url": "https://example.test/media#collected-book",
                                        "signal_type": "media list item",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Collected from a configured media list.",
                                        "candidate_category": "journalism",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            mentions = json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8"))
            self.assertEqual(len(mentions), 1)
            self.assertEqual(mentions[0]["title"], "Collected Book")
            self.assertEqual(mentions[0]["primary_authors"], ["Riley Example"])
            self.assertEqual(mentions[0]["source_name"], "Collector Media List")
            self.assertEqual(mentions[0]["source_family"], "media/curated list")
            self.assertEqual(mentions[0]["source_url"], "https://example.test/media#collected-book")
            self.assertEqual(mentions[0]["source_check_date"], "2026-05-21")
            self.assertEqual(mentions[0]["signal_type"], "media list item")
            self.assertEqual(mentions[0]["signal_strength"], "major media list")
            self.assertEqual(mentions[0]["evidence_note"], "Collected from a configured media list.")
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["Title"] for row in rows], ["Collected Book"])

    def test_invalid_collector_mentions_fail_before_report_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "Collector Media List",
                                "family": "media/curated list",
                                "url": "https://example.test/media",
                                "access_status": "open",
                                "extraction_method": "collector-fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic collector contract source.",
                                "collector_mentions": [
                                    {
                                        "title": "Missing Evidence Book",
                                        "primary_authors": ["Riley Example"],
                                        "source_url": "https://example.test/media#missing-evidence",
                                        "signal_type": "media list item",
                                        "signal_strength": "major media list",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, _output_dir = run_pipeline(tmp_path, config_path)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("collector mention Missing Evidence Book missing required field(s)", result.stderr)
            self.assertIn("evidence_note", result.stderr)

    def test_non_discovery_collector_sources_cannot_create_source_mentions(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-21",
                        "sources": [
                            {
                                "name": "Collector Topic Search",
                                "family": "topic search",
                                "url": "https://example.test/search",
                                "access_status": "open",
                                "extraction_method": "collector-fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Non-discovery collector source.",
                                "collector_mentions": [
                                    {
                                        "title": "Should Not Enter",
                                        "primary_authors": ["Riley Example"],
                                        "source_url": "https://example.test/search#should-not-enter",
                                        "signal_type": "topic search",
                                        "signal_strength": "minor mention",
                                        "evidence_note": "This should not create a Scouting Candidate.",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            mentions = json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8"))
            self.assertEqual(mentions, [])
            source_runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
            self.assertEqual(source_runs[0]["status"], "ignored")

    def test_bookpage_recorded_response_parses_to_safe_source_mentions(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            response_path = tmp_path / "bookpage-response.json"
            response_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://www.bookpage.com/best-books/best-nonfiction/",
                        "entries": [
                            {
                                "title": "BookPage Parsed Book",
                                "authors": ["Morgan Example"],
                                "publisher": "Example Press",
                                "publication_year": "2024",
                                "item_url": "https://www.bookpage.com/reviews/bookpage-parsed-book",
                                "evidence_locator": "BookPage Best Nonfiction 2024, entry for BookPage Parsed Book.",
                                "category": "history",
                                "summary": "Narrative nonfiction about a public archive.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-21",
                        "sources": [
                            {
                                "name": "BookPage Best Nonfiction",
                                "family": "media/curated list",
                                "url": "https://www.bookpage.com/best-books/best-nonfiction/",
                                "access_status": "open",
                                "extraction_method": "bookpage-recorded-response",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic BookPage collector response.",
                                "collector_response_file": str(response_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            mentions = json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8"))
            self.assertEqual(len(mentions), 1)
            self.assertEqual(mentions[0]["title"], "BookPage Parsed Book")
            self.assertEqual(mentions[0]["primary_authors"], ["Morgan Example"])
            self.assertEqual(mentions[0]["publisher"], "Example Press")
            self.assertEqual(mentions[0]["publication_year"], "2024")
            self.assertEqual(mentions[0]["source_name"], "BookPage Best Nonfiction")
            self.assertEqual(mentions[0]["source_family"], "media/curated list")
            self.assertEqual(mentions[0]["source_url"], "https://www.bookpage.com/reviews/bookpage-parsed-book")
            self.assertEqual(mentions[0]["evidence_url"], "https://www.bookpage.com/reviews/bookpage-parsed-book")
            self.assertEqual(
                mentions[0]["evidence_locator"],
                "BookPage Best Nonfiction 2024, entry for BookPage Parsed Book.",
            )
            self.assertEqual(mentions[0]["source_check_date"], "2026-05-21")
            self.assertEqual(mentions[0]["signal_type"], "media/curated list")
            self.assertEqual(mentions[0]["signal_strength"], "major media list")
            self.assertEqual(
                mentions[0]["evidence_note"],
                "BookPage best nonfiction selection; metadata-only entry for BookPage Parsed Book.",
            )
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["Title"] for row in rows], ["BookPage Parsed Book"])
            self.assertEqual(rows[0]["Publisher / year"], "Example Press / 2024")
            self.assertIn("BookPage Best Nonfiction 2024", rows[0]["Signal summary"])
            self.assertIn("https://www.bookpage.com/reviews/bookpage-parsed-book", rows[0]["Links"])

    def test_bookpage_recorded_response_rejects_copied_text_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            response_path = tmp_path / "bookpage-response.json"
            response_path.write_text(
                json.dumps(
                    {
                        "entries": [
                            {
                                "title": "Unsafe BookPage Book",
                                "authors": ["Morgan Example"],
                                "item_url": "https://www.bookpage.com/reviews/unsafe-book",
                                "article_body": "Copied source text should not enter AutoScout artifacts.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "BookPage Best Nonfiction",
                                "family": "media/curated list",
                                "url": "https://www.bookpage.com/best-books/best-nonfiction/",
                                "access_status": "open",
                                "extraction_method": "bookpage-recorded-response",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic BookPage collector response.",
                                "collector_response_file": str(response_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, _output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            source_runs = json.loads((_output_dir / "source-runs.json").read_text(encoding="utf-8"))
            self.assertEqual(source_runs[0]["status"], "failed")
            self.assertIn("BookPage recorded response stores forbidden copied text fields", source_runs[0]["reason"])
            self.assertIn("article_body", source_runs[0]["reason"])
            self.assertEqual(json.loads((_output_dir / "source-mentions.json").read_text(encoding="utf-8")), [])

    def test_bookpage_page_level_url_with_locator_remains_useful_without_new_csv_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            response_path = tmp_path / "bookpage-response.json"
            response_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://www.bookpage.com/best-books/best-nonfiction/",
                        "entries": [
                            {
                                "title": "Locator Only Book",
                                "authors": ["Morgan Example"],
                                "evidence_locator": "BookPage Best Nonfiction 2024, entry for Locator Only Book.",
                                "category": "biography",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-21",
                        "sources": [
                            {
                                "name": "BookPage Best Nonfiction",
                                "family": "media/curated list",
                                "url": "https://www.bookpage.com/best-books/best-nonfiction/",
                                "access_status": "open",
                                "extraction_method": "bookpage-recorded-response",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic BookPage collector response.",
                                "collector_response_file": str(response_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["Source URL"], "https://www.bookpage.com/best-books/best-nonfiction/")
            self.assertIn("BookPage Best Nonfiction 2024", rows[0]["Signal summary"])
            self.assertNotIn("Evidence locator", rows[0])

    def test_malformed_bookpage_collector_response_is_reported_as_failed_source_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            response_path = tmp_path / "bookpage-response.json"
            response_path.write_text(json.dumps({"entries": {"not": "a list"}}), encoding="utf-8")
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "BookPage Best Nonfiction",
                                "family": "media/curated list",
                                "url": "https://www.bookpage.com/best-books/best-nonfiction/",
                                "access_status": "open",
                                "extraction_method": "bookpage-recorded-response",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic BookPage collector response.",
                                "collector_response_file": str(response_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8")), [])
            source_runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
            self.assertEqual(source_runs[0]["status"], "failed")
            self.assertIn("BookPage recorded response entries must be a list", source_runs[0]["reason"])
            self.assertIn("collector_response_file", source_runs[0]["debug_metadata"])
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["failed_source_count"], 1)
            self.assertIn("1 configured source failed", summary["major_warnings"])

    def test_bookpage_source_page_fixture_parses_to_safe_source_mentions(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            response_path = tmp_path / "bookpage-page.json"
            response_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://www.bookpage.com/best-books/best-nonfiction/",
                        "html": """
                        <article class="book">
                          <a href="/books/source-page-book/">Source Page Book</a>
                          <span class="author">Jordan Example</span>
                        </article>
                        """,
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-06-08",
                        "sources": [
                            {
                                "name": "BookPage Best Nonfiction",
                                "family": "media/curated list",
                                "url": "https://www.bookpage.com/best-books/best-nonfiction/",
                                "access_status": "open",
                                "extraction_method": "bookpage-source-page",
                                "check_frequency": "weekly",
                                "language": "en",
                                "source_page_fixture_file": str(response_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            mentions = json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8"))
            self.assertEqual(len(mentions), 1)
            self.assertEqual(mentions[0]["title"], "Source Page Book")
            self.assertEqual(mentions[0]["primary_authors"], ["Jordan Example"])
            self.assertEqual(mentions[0]["source_url"], "https://www.bookpage.com/books/source-page-book/")
            self.assertEqual(mentions[0]["source_check_date"], "2026-06-08")
            self.assertEqual(mentions[0]["signal_strength"], "major media list")
            self.assertIn("Source Page Book", mentions[0]["evidence_note"])
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["Title"], "Source Page Book")
            self.assertIn("https://www.bookpage.com/books/source-page-book/", rows[0]["Links"])

    def test_npr_source_page_fixture_parses_to_safe_source_mentions(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            response_path = tmp_path / "npr-page.json"
            response_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://www.npr.org/2024/06/18/nonfiction-books-summer-2024",
                        "html": """
                        <article class="item">
                          <h2>Burn Book</h2>
                          <p class="byline">by Kara Swisher</p>
                        </article>
                        """,
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-06-10",
                        "sources": [
                            {
                                "name": "NPR Summer Nonfiction Staff Picks",
                                "family": "media/curated list",
                                "url": "https://www.npr.org/2024/06/18/nonfiction-books-summer-2024",
                                "access_status": "open",
                                "extraction_method": "npr-source-page",
                                "check_frequency": "weekly",
                                "language": "en",
                                "source_page_fixture_file": str(response_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            mentions = json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8"))
            self.assertEqual(len(mentions), 1)
            self.assertEqual(mentions[0]["title"], "Burn Book")
            self.assertEqual(mentions[0]["primary_authors"], ["Kara Swisher"])
            self.assertEqual(
                mentions[0]["source_url"],
                "https://www.npr.org/2024/06/18/nonfiction-books-summer-2024",
            )
            self.assertEqual(mentions[0]["source_check_date"], "2026-06-10")
            self.assertEqual(mentions[0]["signal_type"], "media/curated list")
            self.assertEqual(mentions[0]["signal_strength"], "major media list")
            self.assertIn("NPR", mentions[0]["evidence_note"])
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["Title"], "Burn Book")
            self.assertIn("NPR source page entry for Burn Book", rows[0]["Signal summary"])

    def test_npr_collector_source_run_records_health_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            response_path = tmp_path / "npr-page.json"
            response_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://www.npr.org/2024/06/18/nonfiction-books-summer-2024",
                        "html": """
                        <article class="item">
                          <h2>Grief Is for People</h2>
                          <p class="byline">by Sloane Crosley</p>
                        </article>
                        """,
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "NPR Summer Nonfiction Staff Picks",
                                "family": "media/curated list",
                                "url": "https://www.npr.org/2024/06/18/nonfiction-books-summer-2024",
                                "access_status": "open",
                                "extraction_method": "npr-source-page",
                                "check_frequency": "weekly",
                                "language": "en",
                                "source_page_fixture_file": str(response_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            source_runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
            self.assertEqual(source_runs[0]["status"], "collected")
            self.assertEqual(source_runs[0]["raw_mention_count"], 1)
            self.assertEqual(source_runs[0]["valid_mention_count"], 1)
            self.assertEqual(source_runs[0]["skipped_item_count"], 0)
            self.assertEqual(source_runs[0]["debug_metadata"]["collector_kind"], "npr")

    def test_npr_source_page_rejects_copied_text_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            response_path = tmp_path / "npr-page.json"
            response_path.write_text(
                json.dumps(
                    {
                        "entries": [
                            {
                                "title": "Unsafe NPR Book",
                                "authors": ["Morgan Example"],
                                "source_url": "https://www.npr.org/unsafe-book",
                                "article_body": "Copied source text should not enter AutoScout artifacts.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "NPR Summer Nonfiction Staff Picks",
                                "family": "media/curated list",
                                "url": "https://www.npr.org/2024/06/18/nonfiction-books-summer-2024",
                                "access_status": "open",
                                "extraction_method": "npr-source-page",
                                "check_frequency": "weekly",
                                "language": "en",
                                "source_page_fixture_file": str(response_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            source_runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
            self.assertEqual(source_runs[0]["status"], "failed")
            self.assertIn("NPR source page stores forbidden copied text fields", source_runs[0]["reason"])
            self.assertIn("article_body", source_runs[0]["reason"])
            self.assertEqual(json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8")), [])

    def test_malformed_npr_collector_response_is_reported_as_failed_source_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            response_path = tmp_path / "npr-page.json"
            response_path.write_text(json.dumps({"entries": {"not": "a list"}}), encoding="utf-8")
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "NPR Summer Nonfiction Staff Picks",
                                "family": "media/curated list",
                                "url": "https://www.npr.org/2024/06/18/nonfiction-books-summer-2024",
                                "access_status": "open",
                                "extraction_method": "npr-source-page",
                                "check_frequency": "weekly",
                                "language": "en",
                                "source_page_fixture_file": str(response_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8")), [])
            source_runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
            self.assertEqual(source_runs[0]["status"], "failed")
            self.assertIn("NPR source page entries must be a list", source_runs[0]["reason"])
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["failed_source_count"], 1)
            self.assertIn("1 configured source failed", summary["major_warnings"])

    def test_npr_blocked_fixture_is_reported_as_skipped_source_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            response_path = tmp_path / "npr-page.json"
            response_path.write_text(json.dumps({"status": "captcha"}), encoding="utf-8")
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "NPR Summer Nonfiction Staff Picks",
                                "family": "media/curated list",
                                "url": "https://www.npr.org/2024/06/18/nonfiction-books-summer-2024",
                                "access_status": "open",
                                "extraction_method": "npr-source-page",
                                "check_frequency": "weekly",
                                "language": "en",
                                "source_page_fixture_file": str(response_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8")), [])
            source_runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
            self.assertEqual(source_runs[0]["status"], "skipped")
            self.assertIn("CAPTCHA", source_runs[0]["reason"])
            self.assertEqual(source_runs[0]["debug_metadata"]["safe_debug_status"], "captcha")
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["skipped_source_count"], 1)
            self.assertIn("1 configured source skipped", summary["major_warnings"])

    def test_npr_live_opt_in_fixture_capture_can_be_replayed_without_live_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            response_path = tmp_path / "npr-live-response.json"
            response_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://www.npr.org/2024/06/18/nonfiction-books-summer-2024",
                        "html": """
                        <article class="item">
                          <h2>Live NPR Book</h2>
                          <p class="byline">by Sam Example</p>
                        </article>
                        """,
                    }
                ),
                encoding="utf-8",
            )
            live_config_path = tmp_path / "live.json"
            live_config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-06-10",
                        "sources": [
                            {
                                "name": "NPR Summer Nonfiction Staff Picks",
                                "family": "media/curated list",
                                "url": "https://www.npr.org/2024/06/18/nonfiction-books-summer-2024",
                                "access_status": "open",
                                "extraction_method": "npr-live",
                                "check_frequency": "weekly",
                                "language": "en",
                                "enable_live": True,
                                "live_response_file": str(response_path),
                                "capture_normalized_mentions_file": "captured-npr-mentions.json",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, live_config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            captured_path = output_dir / "captured-npr-mentions.json"
            self.assertTrue(captured_path.exists())
            captured = json.loads(captured_path.read_text(encoding="utf-8"))
            self.assertEqual(captured["mentions"][0]["title"], "Live NPR Book")
            replay_config_path = tmp_path / "replay.json"
            replay_config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "NPR Summer Nonfiction Staff Picks",
                                "family": "media/curated list",
                                "url": "https://www.npr.org/2024/06/18/nonfiction-books-summer-2024",
                                "access_status": "open",
                                "extraction_method": "recorded",
                                "check_frequency": "weekly",
                                "language": "en",
                                "recorded_mentions_file": str(captured_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            replay_result, replay_output_dir = run_pipeline(tmp_path, replay_config_path)

            self.assertEqual(replay_result.returncode, 0, replay_result.stderr)
            with (replay_output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["Title"] for row in rows], ["Live NPR Book"])

    def test_bookpage_collector_source_run_records_health_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            response_path = tmp_path / "bookpage-response.json"
            response_path.write_text(
                json.dumps(
                    {
                        "entries": [
                            {
                                "title": "Healthy BookPage Book",
                                "authors": ["Jordan Example"],
                                "item_url": "https://www.bookpage.com/books/healthy-book/",
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
                        "sources": [
                            {
                                "name": "BookPage Best Nonfiction",
                                "family": "media/curated list",
                                "url": "https://www.bookpage.com/best-books/best-nonfiction/",
                                "access_status": "open",
                                "extraction_method": "bookpage-recorded-response",
                                "check_frequency": "weekly",
                                "language": "en",
                                "collector_response_file": str(response_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            source_runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
            self.assertEqual(source_runs[0]["status"], "collected")
            self.assertEqual(source_runs[0]["raw_mention_count"], 1)
            self.assertEqual(source_runs[0]["valid_mention_count"], 1)
            self.assertEqual(source_runs[0]["skipped_item_count"], 0)
            self.assertEqual(source_runs[0]["debug_metadata"]["collector_kind"], "bookpage")

    def test_bookpage_blocked_fixture_is_reported_as_skipped_source_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            response_path = tmp_path / "bookpage-response.json"
            response_path.write_text(json.dumps({"status": "captcha"}), encoding="utf-8")
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "BookPage Best Nonfiction",
                                "family": "media/curated list",
                                "url": "https://www.bookpage.com/best-books/best-nonfiction/",
                                "access_status": "open",
                                "extraction_method": "bookpage-source-page",
                                "check_frequency": "weekly",
                                "language": "en",
                                "source_page_fixture_file": str(response_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8")), [])
            source_runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
            self.assertEqual(source_runs[0]["status"], "skipped")
            self.assertIn("CAPTCHA", source_runs[0]["reason"])
            self.assertEqual(source_runs[0]["debug_metadata"]["safe_debug_status"], "captcha")
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["skipped_source_count"], 1)
            self.assertIn("1 configured source skipped", summary["major_warnings"])

    def test_bookpage_live_opt_in_fixture_capture_can_be_replayed_without_live_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            response_path = tmp_path / "bookpage-live-response.json"
            response_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://www.bookpage.com/best-books/best-nonfiction/",
                        "html": """
                        <article class="book">
                          <a href="/books/live-captured-book/">Live Captured Book</a>
                          <span class="author">Sam Example</span>
                        </article>
                        """,
                    }
                ),
                encoding="utf-8",
            )
            live_config_path = tmp_path / "live.json"
            live_config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-06-08",
                        "sources": [
                            {
                                "name": "BookPage Best Nonfiction",
                                "family": "media/curated list",
                                "url": "https://www.bookpage.com/best-books/best-nonfiction/",
                                "access_status": "open",
                                "extraction_method": "bookpage-live",
                                "check_frequency": "weekly",
                                "language": "en",
                                "enable_live": True,
                                "live_response_file": str(response_path),
                                "capture_normalized_mentions_file": "captured-bookpage-mentions.json",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, live_config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            captured_path = output_dir / "captured-bookpage-mentions.json"
            self.assertTrue(captured_path.exists())
            captured = json.loads(captured_path.read_text(encoding="utf-8"))
            self.assertEqual(captured["mentions"][0]["title"], "Live Captured Book")
            replay_config_path = tmp_path / "replay.json"
            replay_config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "BookPage Best Nonfiction",
                                "family": "media/curated list",
                                "url": "https://www.bookpage.com/best-books/best-nonfiction/",
                                "access_status": "open",
                                "extraction_method": "recorded",
                                "check_frequency": "weekly",
                                "language": "en",
                                "recorded_mentions_file": str(captured_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            replay_result, replay_output_dir = run_pipeline(tmp_path, replay_config_path)

            self.assertEqual(replay_result.returncode, 0, replay_result.stderr)
            with (replay_output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["Title"] for row in rows], ["Live Captured Book"])

    def test_blocked_collector_source_is_reported_as_skipped_without_mentions(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "Blocked Collector",
                                "family": "media/curated list",
                                "url": "https://example.test/blocked",
                                "access_status": "blocked",
                                "extraction_method": "collector-fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Blocked by access controls.",
                                "collector_mentions": [
                                    {
                                        "title": "Should Not Collect",
                                        "primary_authors": ["Riley Example"],
                                        "source_url": "https://example.test/blocked#book",
                                        "signal_type": "media/curated list",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Blocked source should not be collected.",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8")), [])
            source_runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
            self.assertEqual(source_runs[0]["status"], "skipped")
            self.assertIn("blocked", source_runs[0]["reason"])
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["skipped_source_count"], 1)
            self.assertIn("1 configured source skipped", summary["major_warnings"])

    def test_invalid_recorded_source_mentions_fail_before_report_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            recorded_path = tmp_path / "recorded-media.json"
            recorded_path.write_text(
                json.dumps(
                    {
                        "mentions": [
                            {
                                "title": "Missing Author Book",
                                "source_url": "https://example.test/media/missing-author",
                                "source_check_date": "2026-05-21",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Included in an open media list.",
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
                        "sources": [
                            {
                                "name": "Recorded Media List",
                                "family": "media/curated list",
                                "url": "https://example.test/media",
                                "access_status": "open",
                                "extraction_method": "recorded",
                                "check_frequency": "weekly",
                                "language": "en",
                                "recorded_mentions_files": [str(recorded_path)],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("primary_authors", result.stderr)
            self.assertFalse((output_dir / "editor-facing.csv").exists())

    def test_recorded_source_mentions_validate_status_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            recorded_path = tmp_path / "recorded-media.json"
            recorded_path.write_text(
                json.dumps(
                    {
                        "mentions": [
                            {
                                "title": "Bad Status Book",
                                "primary_authors": ["Alex Example"],
                                "source_url": "https://example.test/media/bad-status",
                                "source_check_date": "2026-05-21",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Included in an open media list.",
                                "russian_edition_status": "probably absent",
                                "visible_risk_status": "safe",
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
                        "sources": [
                            {
                                "name": "Recorded Media List",
                                "family": "media/curated list",
                                "url": "https://example.test/media",
                                "access_status": "open",
                                "extraction_method": "recorded",
                                "check_frequency": "weekly",
                                "language": "en",
                                "recorded_mentions_file": str(recorded_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("russian_edition_status", result.stderr)
            self.assertIn("probably absent", result.stderr)
            self.assertFalse((output_dir / "editor-facing.csv").exists())

    def test_recorded_source_mentions_reject_copied_source_prose_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            recorded_path = tmp_path / "recorded-media.json"
            recorded_path.write_text(
                json.dumps(
                    {
                        "mentions": [
                            {
                                "title": "Copied Blurb Book",
                                "primary_authors": ["Alex Example"],
                                "source_url": "https://example.test/media/copied-blurb",
                                "source_check_date": "2026-05-21",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Included in an open media list.",
                                "content_description": "Оригинальное скаутское описание допустимо.",
                                "publisher_blurb": "Copied marketing prose should not enter artifacts.",
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
                        "sources": [
                            {
                                "name": "Recorded Media List",
                                "family": "media/curated list",
                                "url": "https://example.test/media",
                                "access_status": "open",
                                "extraction_method": "recorded",
                                "check_frequency": "weekly",
                                "language": "en",
                                "recorded_mentions_file": str(recorded_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("forbidden copied text field(s)", result.stderr)
            self.assertIn("publisher_blurb", result.stderr)
            self.assertFalse((output_dir / "editor-facing.csv").exists())

    def test_recorded_source_mentions_reject_english_only_content_description_before_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            recorded_path = tmp_path / "recorded-media.json"
            recorded_path.write_text(
                json.dumps(
                    {
                        "mentions": [
                            {
                                "title": "English Description Book",
                                "primary_authors": ["Alex Example"],
                                "source_url": "https://example.test/media/english-description",
                                "source_check_date": "2026-05-21",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Included in an open media list.",
                                "content_description": (
                                    "Original scouting prose explains the topic and editorial hook."
                                ),
                                "russian_edition_status": "not found in Russian",
                                "visible_risk_status": "no visible risk",
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
                        "sources": [
                            {
                                "name": "Recorded Media List",
                                "family": "media/curated list",
                                "url": "https://example.test/media",
                                "access_status": "open",
                                "extraction_method": "recorded",
                                "check_frequency": "weekly",
                                "language": "en",
                                "recorded_mentions_file": str(recorded_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            # The mention should succeed — bad description is cleared, not fatal (#256).
            self.assertEqual(result.returncode, 0)
            self.assertTrue((output_dir / "editor-facing.csv").exists())
            # The description column should contain the fallback.
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["Description"], DESCRIPTION_FALLBACK)
            self.assertEqual(rows[0]["Title"], "English Description Book")
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            coverage = summary["description_coverage"]
            self.assertEqual(coverage["validated_count"], 0)
            self.assertEqual(coverage["fallback_count"], 1)
            self.assertEqual(coverage["coverage"], "partial")
            self.assertEqual(
                coverage["invalid_reason_counts"],
                {"no Cyrillic content — English-only or insufficiently Russian text rejected": 1},
            )
            self.assertEqual(coverage["fallback_ids"], ["english description book|alex example"])

    def test_content_description_preserves_provenance_russian_and_risk_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            recorded_path = tmp_path / "recorded-media.json"
            recorded_path.write_text(
                json.dumps(
                    {
                        "mentions": [
                            {
                                "title": "Risk Context Book",
                                "primary_authors": ["Alex Example"],
                                "source_url": "https://example.test/media/risk-context",
                                "evidence_url": "https://example.test/media/risk-context#entry",
                                "evidence_locator": "Recorded media list, Risk Context Book entry.",
                                "source_check_date": "2026-05-21",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Included in an open media list for risk-context testing.",
                                "candidate_category": "public affairs",
                                "content_description": (
                                    "Оригинальная скаутская аннотация объясняет тему, ракурс "
                                    "и редакционный крючок, не подменяя происхождение данных или поля проверки."
                                ),
                                "russian_edition_status": "unclear",
                                "visible_risk_status": "text check required",
                                "visible_risk_comment": "Public metadata is insufficient for configured risk screening.",
                                "public_data_status": "insufficient",
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
                        "sources": [
                            {
                                "name": "Recorded Media List",
                                "family": "media/curated list",
                                "url": "https://example.test/media",
                                "access_status": "open",
                                "extraction_method": "recorded",
                                "check_frequency": "weekly",
                                "language": "en",
                                "recorded_mentions_file": str(recorded_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            row = rows[0]
            self.assertTrue(row["Description"].startswith("Оригинальная скаутская аннотация"))
            self.assertTrue(has_cyrillic(row["Description"]))
            self.assertEqual(row["Source"], "Recorded Media List")
            self.assertEqual(row["Source URL"], "https://example.test/media/risk-context")
            self.assertIn("Included in an open media list", row["Why it entered the list"])
            self.assertIn("Recorded media list, Risk Context Book entry", row["Signal summary"])
            self.assertIn("https://example.test/media/risk-context#entry", row["Links"])
            self.assertEqual(row["Russian-edition status"], "unclear")
            self.assertEqual(row["Visible-risk status"], "text check required")
            self.assertIn("Public metadata is insufficient", row["Risk comment"])
            self.assertIn("Visible Risk: text check required", row["Annotation"])

    def test_recorded_source_mentions_support_item_urls_and_locators(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            recorded_path = tmp_path / "recorded-media.json"
            recorded_path.write_text(
                json.dumps(
                    {
                        "mentions": [
                            {
                                "title": "Item URL Book",
                                "primary_authors": ["Alex Example"],
                                "source_url": "https://example.test/media/page",
                                "evidence_url": "https://example.test/media/page/item-url-book",
                                "source_check_date": "2026-05-21",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Listed on a media page with a direct item URL.",
                                "candidate_category": "business",
                            },
                            {
                                "title": "Locator Book",
                                "primary_authors": ["Sam Example"],
                                "source_url": "https://example.test/media/page",
                                "evidence_locator": "Section: Staff Picks, third item",
                                "source_check_date": "2026-05-21",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Listed on a media page with a locator but no item URL.",
                                "candidate_category": "business",
                            },
                            {
                                "title": "Fallback Book",
                                "primary_authors": ["Casey Example"],
                                "source_url": "https://example.test/media/page",
                                "source_check_date": "2026-05-21",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Listed on a media page without extra locator details.",
                                "candidate_category": "business",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "Recorded Media List",
                                "family": "media/curated list",
                                "url": "https://example.test/media/page",
                                "access_status": "open",
                                "extraction_method": "recorded",
                                "check_frequency": "weekly",
                                "language": "en",
                                "recorded_mentions_file": str(recorded_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            mentions = json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8"))
            mentions_by_title = {mention["title"]: mention for mention in mentions}
            self.assertEqual(
                mentions_by_title["Item URL Book"]["evidence_url"],
                "https://example.test/media/page/item-url-book",
            )
            self.assertEqual(mentions_by_title["Locator Book"]["evidence_locator"], "Section: Staff Picks, third item")
            self.assertEqual(mentions_by_title["Fallback Book"]["evidence_url"], "")

            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            rows_by_title = {row["Title"]: row for row in rows}
            self.assertIn("https://example.test/media/page/item-url-book", rows_by_title["Item URL Book"]["Links"])
            self.assertIn("Section: Staff Picks, third item", rows_by_title["Locator Book"]["Signal summary"])
            self.assertEqual(rows_by_title["Fallback Book"]["Links"], "https://example.test/media/page")

    def test_recorded_source_mentions_preserve_status_review_metadata_and_warn_on_uncertainty(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            recorded_path = tmp_path / "recorded-media.json"
            recorded_path.write_text(
                json.dumps(
                    {
                        "mentions": [
                            {
                                "title": "Reviewed Status Book",
                                "primary_authors": ["Alex Example"],
                                "source_url": "https://example.test/media/reviewed-status",
                                "source_check_date": "2026-05-21",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Recorded from a media page with review notes.",
                                "candidate_category": "business",
                                "russian_edition_status": "unclear",
                                "russian_edition_status_source": "manual",
                                "russian_edition_status_note": "Recorded from Dima review notes.",
                                "visible_risk_status": "text check required",
                                "visible_risk_status_source": "placeholder",
                                "visible_risk_status_note": "Public metadata is insufficient for a firm risk call.",
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
                        "sources": [
                            {
                                "name": "Recorded Media List",
                                "family": "media/curated list",
                                "url": "https://example.test/media",
                                "access_status": "open",
                                "extraction_method": "recorded",
                                "check_frequency": "weekly",
                                "language": "en",
                                "recorded_mentions_file": str(recorded_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            mentions = json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8"))
            self.assertEqual(mentions[0]["russian_edition_status_source"], "manual")
            self.assertEqual(mentions[0]["russian_edition_status_note"], "Recorded from Dima review notes.")
            self.assertEqual(mentions[0]["visible_risk_status_source"], "placeholder")
            self.assertEqual(
                mentions[0]["visible_risk_status_note"],
                "Public metadata is insufficient for a firm risk call.",
            )

            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertIn("unclear Russian-edition status", " ".join(summary["major_warnings"]))
            self.assertIn("unreviewed Visible Risk status", " ".join(summary["major_warnings"]))

    def test_recorded_media_list_trial_fixture_is_reviewable_metadata_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = Path("configs/recorded-media-trial.json")

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            mentions = json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(len(mentions), 10)
            self.assertLessEqual(len(mentions), 20)
            self.assertEqual(
                {mention["source_family"] for mention in mentions},
                {"media/curated list", "award", "bestseller/ranking"},
            )
            self.assertGreaterEqual(len({mention["source_name"] for mention in mentions}), 2)
            self.assertLessEqual(len({mention["source_name"] for mention in mentions}), 5)
            forbidden_fields = {
                "article_body",
                "page_body",
                "full_review",
                "copied_excerpt",
                "book_sample",
            }
            for mention in mentions:
                self.assertTrue(mention["source_url"].startswith("https://"))
                self.assertTrue(mention["evidence_note"])
                self.assertLessEqual(len(mention["evidence_note"]), 240)
                self.assertFalse(forbidden_fields & mention.keys())

            internal_exclusions = json.loads((output_dir / "internal-exclusions.json").read_text(encoding="utf-8"))
            self.assertIn(
                "found in Russian",
                {exclusion["exclusion_reason"] for exclusion in internal_exclusions},
            )
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            self.assertIn(
                "clear visible risk",
                {candidate.get("visible_risk_status", "") for candidate in candidates},
            )
            review_notes = Path("docs/recorded-media-trial.md")
            self.assertTrue(review_notes.exists())
            self.assertIn("Dima review", review_notes.read_text(encoding="utf-8"))

    def test_recorded_media_list_trial_preserves_weekly_report_contracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            result, output_dir = run_pipeline(tmp_path, Path("configs/recorded-media-trial.json"))

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                reader = csv.reader(handle)
                header = next(reader)
            self.assertEqual(
                header,
                [
                    "Title",
                    "Author",
                    "Annotation",
                    "Description",
                    "Description status",
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
                    "Editor status",
                    "Editor note",
                    "Candidate ID",
                    "Durable Candidate ID",
                ],
            )

            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["raw_candidate_count"], 16)
            self.assertEqual(summary["excluded_candidate_count"], 4)
            self.assertEqual(summary["editor_facing_count"], 10)
            self.assertEqual(summary["shortfall_reason"], "")
            self.assertEqual(
                summary["major_warnings"],
                [
                    "10 editor-facing rows have unclear Russian-edition status",
                    # Issue #714: every status was supplied with its mention,
                    # so the Visible Risk screen concluded on none of the rows.
                    "Visible Risk screen concluded for none of 10 editor-facing rows",
                ],
            )

            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(
                [],
                [row["Title"] for row in rows if not row["Publisher / year"].strip()],
            )
            self.assertNotIn(
                "The Anxious Generation: How the Great Rewiring of Childhood Is Causing an Epidemic of Mental Illness",
                {row["Title"] for row in rows},
            )
            risk_rows = [row for row in rows if row["Visible-risk status"] == "clear visible risk"]
            self.assertGreaterEqual(len(risk_rows), 1)
            self.assertTrue(all(row["Priority label"] == "D" for row in risk_rows))

            exclusions = json.loads((output_dir / "internal-exclusions.json").read_text(encoding="utf-8"))
            self.assertEqual(len(exclusions), 4)
            self.assertEqual(
                {exclusion["title"] for exclusion in exclusions},
                {
                    "The Anxious Generation: How the Great Rewiring of Childhood Is Causing an Epidemic of Mental Illness",
                    "Challenger: A True Story of Heroism and Disaster on the Edge of Space",
                    "Smoke and Ashes: Opium's Hidden Histories",
                },
            )
            self.assertTrue(all(exclusion["exclusion_reason"] == "found in Russian" for exclusion in exclusions))

    def test_recorded_media_list_trial_aggregates_multi_family_signals(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result, output_dir = run_pipeline(tmp_path, Path("configs/recorded-media-trial.json"))

            self.assertEqual(result.returncode, 0, result.stderr)
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            multi_signal_candidates = [
                candidate for candidate in candidates if candidate.get("independent_signal_count", 0) >= 2
            ]

            self.assertGreaterEqual(len(multi_signal_candidates), 2)
            self.assertIn(
                {"media/curated list", "award"},
                [set(candidate.get("independent_signal_families", [])) for candidate in multi_signal_candidates],
            )
            self.assertIn(
                {"media/curated list", "bestseller/ranking"},
                [set(candidate.get("independent_signal_families", [])) for candidate in multi_signal_candidates],
            )
            self.assertIn(
                "award",
                {
                    str(candidate.get("primary_signal", {}).get("source_family", ""))
                    for candidate in multi_signal_candidates
                },
            )

            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertIn("B", {row["Priority label"] for row in rows})

    def test_recorded_media_list_trial_writes_pilot_status_with_incomplete_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result, output_dir = run_pipeline(tmp_path, Path("configs/recorded-media-trial.json"))

            self.assertEqual(result.returncode, 0, result.stderr)
            pilot_status = json.loads((output_dir / "pilot-status.json").read_text(encoding="utf-8"))
            self.assertFalse(pilot_status["ready_for_editor"])
            self.assertEqual(pilot_status["editor_facing_count"], 10)
            self.assertEqual(pilot_status["csv_path"], "editor-facing.csv")
            self.assertIn("unclear Russian-edition status", " ".join(pilot_status["caveats"]))
            self.assertIn("Not ready to share with the editor friend", pilot_status["share_note"])
            self.assertEqual(
                pilot_status["shareable_artifacts"],
                [
                    {
                        "path": "editor-facing.csv",
                        "purpose": "editor-facing Weekly Report Rows for pilot review",
                    },
                    {
                        "path": "editor-review-guide.md",
                        "purpose": "plain-language review prompts and pilot caveats for the editor",
                    },
                ],
            )
            internal_paths = {artifact["path"] for artifact in pilot_status["internal_artifacts"]}
            self.assertIn("run-summary.json", internal_paths)
            self.assertIn("source-runs.json", internal_paths)
            self.assertIn("source-proposals.json", internal_paths)
            self.assertIn("internal-exclusions.json", internal_paths)
            self.assertIn("russian-edition-checks.json", internal_paths)
            self.assertEqual(pilot_status["readiness_basis"]["editor_facing_count"], 10)
            self.assertEqual(pilot_status["readiness_basis"]["editor_row_target"], 10)
            self.assertEqual(pilot_status["readiness_basis"]["pilot_row_min"], 8)
            self.assertEqual(pilot_status["readiness_basis"]["pilot_row_max"], 12)
            self.assertEqual(pilot_status["readiness_basis"]["shortfall_reason"], "")
            # Unclear Russian status plus the issue #714 inert-screen warning.
            self.assertEqual(pilot_status["readiness_basis"]["major_warning_count"], 2)
            self.assertTrue(pilot_status["readiness_basis"]["found_in_russian_exclusions_stay_internal"])
            self.assertTrue(pilot_status["readiness_basis"]["clear_visible_risk_rows_stay_visible"])
            self.assertIn("source_contributions", pilot_status["readiness_basis"])

    def test_recorded_media_trial_visible_rows_all_have_richer_russian_content_descriptions(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result, output_dir = run_pipeline(tmp_path, Path("configs/recorded-media-trial.json"))

            self.assertEqual(result.returncode, 0, result.stderr)
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(len(candidates), 10)
            missing = [
                candidate["title"]
                for candidate in candidates
                if not str(candidate.get("content_description", "")).strip()
            ]
            self.assertEqual(missing, [])
            for candidate in candidates:
                description = str(candidate["content_description"])
                self.assertGreaterEqual(description.count("."), 2, candidate["title"])
                self.assertLessEqual(len(description), 700, candidate["title"])
                self.assertTrue(has_cyrillic(description), candidate["title"])

            descriptions_by_title = {candidate["title"]: candidate["content_description"] for candidate in candidates}
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            for row in rows:
                self.assertTrue(
                    row["Description"].startswith(descriptions_by_title[row["Title"]]),
                    row["Title"],
                )
                self.assertTrue(has_cyrillic(row["Description"]), row["Title"])

    def test_recorded_media_trial_writes_editor_review_guide_for_pilot(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result, output_dir = run_pipeline(tmp_path, Path("configs/recorded-media-trial.json"))

            self.assertEqual(result.returncode, 0, result.stderr)
            pilot_status = json.loads((output_dir / "pilot-status.json").read_text(encoding="utf-8"))
            self.assertEqual(pilot_status["editor_review_guide_path"], "editor-review-guide.md")
            guide = (output_dir / "editor-review-guide.md").read_text(encoding="utf-8")
            self.assertIn("recorded scouting trial", guide)
            self.assertIn("not a production weekly report", guide)
            self.assertIn("not legal clearance", guide)
            self.assertIn("rights availability is not determined", guide)
            self.assertIn("configured checks and recorded fixtures", guide)
            for review_prompt in (
                "Are these plausible Scouting Candidates?",
                "Are Evidence Notes concise and useful?",
                "Are Source, Source URL, Signal Summary, and Links enough to verify provenance?",
                "Do Priority Labels match editorial intuition?",
                "Are Russian-edition statuses understandable?",
                "Are Visible Risk statuses and comments understandable?",
                "Are English helper fields useful where present?",
                "Does one Weekly Report Row per Scouting Candidate work for review?",
            ):
                self.assertIn(review_prompt, guide)

    def test_pilot_status_documents_feedback_intake_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-06-10",
                        "pilot_mode": True,
                        "report_qa": {
                            "reviewer": "Dima",
                            "status": "technical review",
                            "note": "Report-level QA is not editor feedback.",
                        },
                        "sources": [
                            {
                                "name": "Pilot Feedback Source",
                                "family": "media/curated list",
                                "url": "https://example.test/pilot",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "trial",
                                "language": "en",
                                "fixture_candidates": [
                                    {
                                        "title": "Pilot Feedback Book",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/pilot/book",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Included in a pilot media source.",
                                        "candidate_category": "business",
                                    }
                                ],
                            }
                        ],
                        "returned_editor_rows": [
                            {
                                "identity_key": "pilot feedback book|alex example",
                                "Title": "Pilot Feedback Book Corrected",
                                "Author": "Alex Example",
                                "Editor Status": "needs_deeper_analysis",
                                "Editor Note": "Please prepare a deeper note.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            pilot_status = json.loads((output_dir / "pilot-status.json").read_text(encoding="utf-8"))
            self.assertEqual(
                pilot_status["feedback_intake_contract"]["supported_editor_statuses"],
                ["interested", "maybe_watch", "reject", "already_known", "needs_deeper_analysis"],
            )
            self.assertEqual(
                pilot_status["feedback_intake_contract"]["authoritative_feedback_fields"],
                ["Editor Status", "Editor Note"],
            )
            self.assertEqual(
                pilot_status["feedback_intake_contract"]["generated_fact_edits"],
                "stored in proposed-corrections.json until explicitly accepted",
            )
            self.assertEqual(
                pilot_status["feedback_intake_contract"]["report_qa_artifact"],
                "report-qa.json",
            )
            self.assertIn("candidate-history.json", pilot_status["feedback_intake_contract"]["feedback_artifacts"])
            self.assertIn("proposed-corrections.json", pilot_status["feedback_intake_contract"]["feedback_artifacts"])
            self.assertIn("memo-queue.json", pilot_status["feedback_intake_contract"]["feedback_artifacts"])

            history = json.loads((output_dir / "candidate-history.json").read_text(encoding="utf-8"))
            self.assertEqual(history[0]["editor_status"], "needs_deeper_analysis")
            proposed = json.loads((output_dir / "proposed-corrections.json").read_text(encoding="utf-8"))
            self.assertEqual(proposed[0]["field"], "title")
            self.assertEqual(proposed[0]["status"], "proposed")
            report_qa = json.loads((output_dir / "report-qa.json").read_text(encoding="utf-8"))
            self.assertEqual(report_qa["status"], "technical review")

    def test_recorded_media_trial_uses_automated_russian_fixture_checks(self):
        config_text = Path("configs/recorded-media-trial.json").read_text(encoding="utf-8")
        self.assertNotIn("russian_edition_status_source", config_text)
        self.assertIn("russian_edition_checks", config_text)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result, output_dir = run_pipeline(tmp_path, Path("configs/recorded-media-trial.json"))

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 10)

            exclusions = json.loads((output_dir / "internal-exclusions.json").read_text(encoding="utf-8"))
            self.assertEqual(len(exclusions), 4)
            self.assertTrue(all(row["exclusion_reason"] == "found in Russian" for row in exclusions))

            russian_checks = json.loads((output_dir / "russian-edition-checks.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(len(russian_checks["candidate_checks"]), 10)
            self.assertIn(
                "found in Russian",
                {check["resolved_status"] for check in russian_checks["candidate_checks"]},
            )

    def test_obvious_duplicate_mentions_merge_into_one_work_level_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-21",
                        "sources": [
                            {
                                "name": "Fixture Open Awards",
                                "family": "award",
                                "url": "https://example.test/awards",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "The Useful Future",
                                        "primary_authors": ["Alex Example"],
                                        "english_title": "The Useful Future",
                                        "english_author_display": "Alex Example",
                                        "english_annotation": "English helper note from source metadata.",
                                        "search_translation": "Полезное будущее",
                                        "author_transliteration_note": "Алекс Экземпл",
                                        "source_url": "https://example.test/awards/useful-future",
                                        "signal_type": "award",
                                        "signal_strength": "shortlist",
                                        "evidence_note": "Listed as a shortlist title.",
                                        "candidate_category": "popular science",
                                    }
                                ],
                            },
                            {
                                "name": "Fixture Media List",
                                "family": "media/curated list",
                                "url": "https://example.test/media",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "The Useful Future",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/media/useful-future",
                                        "signal_type": "media/curated list",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Included in a media list.",
                                        "candidate_category": "popular science",
                                    }
                                ],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["identity_key"], "the useful future|alex example")
            self.assertEqual(candidates[0]["title"], "The Useful Future")
            self.assertEqual(candidates[0]["primary_authors"], ["Alex Example"])
            self.assertEqual(candidates[0]["english_title"], "The Useful Future")
            self.assertEqual(candidates[0]["english_author_display"], "Alex Example")
            self.assertEqual(candidates[0]["english_annotation"], "English helper note from source metadata.")
            self.assertEqual(
                candidates[0]["russian_search_notes"],
                {
                    "search_translation": "Полезное будущее",
                    "author_transliteration_note": "Алекс Экземпл",
                },
            )
            self.assertEqual(
                [evidence["source_name"] for evidence in candidates[0]["source_evidence"]],
                ["Fixture Open Awards", "Fixture Media List"],
            )
            with (output_dir / "editor-facing.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["Title"] for row in rows], ["The Useful Future"])
            self.assertEqual(rows[0]["English title"], "The Useful Future (original)")
            self.assertEqual(rows[0]["English author display"], "Alex Example (original)")
            self.assertEqual(rows[0]["English annotation"], "English helper note from source metadata.")

    def test_bookpage_recorded_response_preserves_english_helper_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            response_path = tmp_path / "bookpage.json"
            response_path.write_text(
                json.dumps(
                    {
                        "entries": [
                            {
                                "title": "原題",
                                "authors": ["著者 名"],
                                "english_title": "Original Title",
                                "english_author_display": "Author Name",
                                "english_annotation": "English helper note.",
                                "search_translation": "Поисковый перевод",
                                "author_transliteration_note": "Автор Имя",
                                "item_url": "https://example.test/bookpage/original",
                                "candidate_category": "history",
                                "signal_strength": "major media list",
                                "evidence_note": "Included in a recorded BookPage list.",
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
                        "sources": [
                            {
                                "name": "BookPage Recorded",
                                "family": "media/curated list",
                                "url": "https://example.test/bookpage",
                                "access_status": "open",
                                "extraction_method": "bookpage-recorded-response",
                                "check_frequency": "weekly",
                                "language": "ja",
                                "notes": "Recorded response source.",
                                "collector_response_file": str(response_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["Title"], "原題")
            self.assertEqual(rows[0]["Author"], "著者 名")
            self.assertEqual(rows[0]["English title"], "Original Title (machine translation)")
            self.assertEqual(rows[0]["English author display"], "Author Name (transliteration)")
            self.assertEqual(rows[0]["English annotation"], "English helper note.")

    def test_recorded_non_english_candidate_preserves_original_metadata_and_helper_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            recorded_path = tmp_path / "recorded-non-english.json"
            recorded_path.write_text(
                json.dumps(
                    {
                        "mentions": [
                            {
                                "title": "原題",
                                "primary_authors": ["著者 名"],
                                "english_title": "Original Title",
                                "english_author_display": "Author Name",
                                "english_annotation": "Generated English aid for editor review.",
                                "source_url": "https://example.test/non-english/original",
                                "source_check_date": "2026-05-21",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Included in a recorded non-English nonfiction list.",
                                "candidate_category": "narrative history",
                                "short_description": "Metadata-only note about a public-history work.",
                                "content_description": (
                                    "Книга по публичной истории с исходного рынка использует локальный японский "
                                    "пример, чтобы объяснить, как память, институты и гражданская идентичность "
                                    "формируют современный спор. Расширенное описание дает редактору контекст, "
                                    "не превращая английские вспомогательные поля в подтвержденные метаданные."
                                ),
                                "public_data_status": "sufficient",
                                "russian_edition_status": "not found in Russian",
                                "visible_risk_status": "no visible risk",
                                "search_translation": "Поисковый перевод",
                                "author_transliteration_note": "Автор Имя",
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
                        "sources": [
                            {
                                "name": "Recorded Non-English List",
                                "family": "media/curated list",
                                "url": "https://example.test/non-english",
                                "access_status": "open",
                                "extraction_method": "recorded",
                                "check_frequency": "weekly",
                                "language": "ja",
                                "notes": "Recorded non-English metadata fixture.",
                                "recorded_mentions_file": str(recorded_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["Title"], "原題")
            self.assertEqual(rows[0]["Author"], "著者 名")
            self.assertEqual(rows[0]["English title"], "Original Title (machine translation)")
            self.assertEqual(rows[0]["English author display"], "Author Name (transliteration)")
            self.assertEqual(rows[0]["English annotation"], "Generated English aid for editor review.")
            self.assertIn(
                "публичной истории",
                rows[0]["Description"],
            )
            self.assertIn("редактору контекст", rows[0]["Description"])
            self.assertTrue(has_cyrillic(rows[0]["Description"]))
            self.assertNotIn("説明", rows[0]["Description"])
            self.assertNotIn("Поисковый перевод", rows[0]["Title"])
            self.assertNotIn("Автор Имя", rows[0]["Author"])
            self.assertIn("narrative history", rows[0]["Annotation"])
            self.assertIn(
                "Included in a recorded non-English nonfiction list.",
                rows[0]["Annotation"],
            )
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(candidates[0]["title"], "原題")
            self.assertEqual(candidates[0]["primary_authors"], ["著者 名"])
            self.assertEqual(
                candidates[0]["russian_search_notes"],
                {
                    "search_translation": "Поисковый перевод",
                    "author_transliteration_note": "Автор Имя",
                },
            )

    def test_duplicate_source_family_links_count_as_one_independent_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-21",
                        "sources": [
                            {
                                "name": "Fixture Media List",
                                "family": "media/curated list",
                                "url": "https://example.test/media",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "The Useful Future",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/media/useful-future",
                                        "signal_type": "media/curated list",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Included in a major media list.",
                                        "candidate_category": "popular science",
                                    }
                                ],
                            },
                            {
                                "name": "Fixture Review Roundup",
                                "family": "media/curated list",
                                "url": "https://example.test/reviews",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "The Useful Future",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/reviews/useful-future",
                                        "signal_type": "media/curated list",
                                        "signal_strength": "minor mention",
                                        "evidence_note": "Mentioned in a review roundup.",
                                        "candidate_category": "popular science",
                                    }
                                ],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(candidates[0]["independent_signal_count"], 1)
            self.assertEqual(candidates[0]["independent_signal_families"], ["media/curated list"])
            self.assertEqual(len(candidates[0]["source_evidence"]), 2)
            self.assertEqual(
                candidates[0]["signal_summary"],
                "1 independent signal: media/curated list. "
                "Multiple links from the same family are preserved as evidence without increasing the count.",
            )
            with (output_dir / "editor-facing.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["Signal summary"], candidates[0]["signal_summary"])

    def test_primary_signal_uses_strongest_signal_and_counts_multiple_families(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
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
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "The Useful Future",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/awards/useful-future",
                                        "signal_type": "award",
                                        "signal_strength": "shortlist",
                                        "evidence_note": "Shortlisted for a fixture award.",
                                        "candidate_category": "popular science",
                                    }
                                ],
                            },
                            {
                                "name": "Fixture Ranking",
                                "family": "bestseller/ranking",
                                "url": "https://example.test/ranking",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "The Useful Future",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/ranking/useful-future",
                                        "signal_type": "bestseller/ranking",
                                        "signal_strength": "top ranking",
                                        "evidence_note": "Reached a top nonfiction ranking.",
                                        "candidate_category": "popular science",
                                    }
                                ],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(candidates[0]["independent_signal_count"], 2)
            self.assertEqual(candidates[0]["primary_signal"]["source_name"], "Fixture Ranking")
            self.assertEqual(candidates[0]["primary_signal"]["signal_strength"], "top ranking")
            with (output_dir / "editor-facing.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["Why it entered the list"], "Reached a top nonfiction ranking.")
            self.assertEqual(rows[0]["Source"], "Fixture Ranking")
            self.assertEqual(rows[0]["Source URL"], "https://example.test/ranking/useful-future")

    def test_primary_signal_tie_keeps_signal_that_caused_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
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
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "The Useful Future",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/awards/useful-future",
                                        "signal_strength": "shortlist",
                                        "evidence_note": "Shortlisted for a fixture award.",
                                        "candidate_category": "popular science",
                                    }
                                ],
                            },
                            {
                                "name": "Fixture Media",
                                "family": "media/curated list",
                                "url": "https://example.test/media",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "The Useful Future",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/media/useful-future",
                                        "signal_strength": "shortlist",
                                        "evidence_note": "Listed on a fixture media shortlist.",
                                        "candidate_category": "popular science",
                                    }
                                ],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(candidates[0]["primary_signal"]["source_name"], "Fixture Awards")
            with (output_dir / "editor-facing.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["Source"], "Fixture Awards")

    def test_priority_labels_respect_signal_count_russian_status_fit_and_visible_risk(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = copy_fixture("source-configs", "priority-labels-pipeline.json", tmp_path / "sources.json")

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            labels_by_title = {candidate["title"]: candidate["priority_label"] for candidate in candidates}
            self.assertEqual(labels_by_title["Clear Fit Two Signals"], "B")
            self.assertEqual(labels_by_title["Unclear Russian Strong Signals"], "B")
            self.assertEqual(labels_by_title["Risky Strong Signals"], "D")
            with (output_dir / "editor-facing.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual({row["Title"]: row["Priority label"] for row in rows}, labels_by_title)

    def test_sufficient_public_data_without_configured_risk_signal_is_no_visible_risk(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-21",
                        "sources": [
                            {
                                "name": "Fixture Business List",
                                "family": "media/curated list",
                                "url": "https://example.test/business",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "The Useful Company",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/business/useful-company",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Included in a management books list.",
                                        "candidate_category": "business",
                                        "public_data_status": "sufficient",
                                        "short_description": "A practical book about teams, incentives, and company culture.",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(candidates[0]["visible_risk_status"], "no visible risk")
            self.assertEqual(candidates[0]["visible_risk_category"], "")
            self.assertEqual(
                candidates[0]["visible_risk_comment"],
                "No configured MVP risk signal found in available public metadata; this is not legal clearance or a full-text review.",
            )
            with (output_dir / "editor-facing.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["Visible-risk status"], "no visible risk")
            self.assertEqual(rows[0]["Risk comment"], candidates[0]["visible_risk_comment"])

    def test_insufficient_public_data_requires_text_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "Fixture Ranking",
                                "family": "bestseller/ranking",
                                "url": "https://example.test/ranking",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "Sparse Metadata Book",
                                        "primary_authors": ["Dana Example"],
                                        "source_url": "https://example.test/ranking/sparse",
                                        "signal_strength": "top ranking",
                                        "evidence_note": "Ranked nonfiction book; no description or contents available.",
                                        "candidate_category": "popular science",
                                        "public_data_status": "insufficient",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(candidates[0]["visible_risk_status"], "text check required")
            self.assertEqual(candidates[0]["visible_risk_category"], "")
            self.assertEqual(
                candidates[0]["visible_risk_comment"],
                "Public metadata is insufficient for configured MVP risk screening; full text or richer public materials are needed.",
            )
            with (output_dir / "editor-facing.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["Visible-risk status"], "text check required")

    def test_weak_author_context_signal_requires_text_check_not_clear_visible_risk(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "Fixture Media List",
                                "family": "media/curated list",
                                "url": "https://example.test/media",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "A Careful Memoir",
                                        "primary_authors": ["Robin Example"],
                                        "source_url": "https://example.test/media/careful-memoir",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Media profile mentions the author's unverified anti-war activism; book description focuses on family history.",
                                        "candidate_category": "memoir",
                                        "public_data_status": "sufficient",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(candidates[0]["visible_risk_status"], "text check required")
            self.assertEqual(candidates[0]["visible_risk_category"], "war/anti-war/Russian Armed Forces")
            self.assertEqual(
                candidates[0]["visible_risk_comment"],
                "Author/context signal in public metadata is weak, indirect, or unverified; check the text before treating it as clear book-content risk.",
            )

    def test_credible_book_content_signal_is_clear_visible_risk_and_stays_in_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "Fixture Health List",
                                "family": "media/curated list",
                                "url": "https://example.test/health",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "The Microdosing Method",
                                        "primary_authors": ["Taylor Example"],
                                        "source_url": "https://example.test/health/microdosing-method",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Public description says the book provides a microdosing protocol for psychoactive substances.",
                                        "candidate_category": "health",
                                        "publisher_fit": "clear fit",
                                        "public_data_status": "sufficient",
                                        "risk_evidence_url": "https://example.test/health/microdosing-method",
                                        "risk_signal_scope": "book-content",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["Title"] for row in rows], ["The Microdosing Method"])
            self.assertEqual(rows[0]["Visible-risk status"], "clear visible risk")
            self.assertIn("microdosing", rows[0]["Risk comment"])
            self.assertEqual(rows[0]["Priority label"], "D")
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(
                candidates[0]["visible_risk_comment"],
                "Book-content signal from public metadata requires human/legal review: Public description says the book provides a microdosing protocol for psychoactive substances. Evidence: https://example.test/health/microdosing-method. Drug/microdosing screening treats the September 2025 drug-content rule as operationally relevant.",
            )

    def test_mvp_visible_risk_categories_are_limited_to_configured_categories(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = copy_fixture(
                "source-configs", "visible-risk-categories-pipeline.json", tmp_path / "sources.json"
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            categories_by_title = {candidate["title"]: candidate["visible_risk_category"] for candidate in candidates}
            self.assertEqual(categories_by_title["Russia Today"], "Russia/politics")
            self.assertEqual(categories_by_title["USSR And Nazi Comparisons"], "WWII/USSR/Nazi comparison")
            self.assertEqual(
                categories_by_title["Transition And Childfree Lives"],
                "non-traditional relationships/gender transition/rejection of childbearing",
            )
            self.assertEqual(categories_by_title["Suicide Prevention Handbook"], "")
            self.assertEqual(
                {candidate["visible_risk_category"] for candidate in candidates if candidate["visible_risk_category"]},
                {
                    "Russia/politics",
                    "WWII/USSR/Nazi comparison",
                    "non-traditional relationships/gender transition/rejection of childbearing",
                },
            )

    def test_returned_editor_feedback_preserves_raw_note_and_proposes_fact_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            raw_note = "Track for fall list.\nDo not summarize this note."
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
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "The Useful Future",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/awards/useful-future",
                                        "signal_strength": "shortlist",
                                        "evidence_note": "Shortlisted for a fixture award.",
                                        "candidate_category": "popular science",
                                    }
                                ],
                            }
                        ],
                        "returned_editor_rows": [
                            {
                                "identity_key": "the useful future|alex example",
                                "Editor Status": "maybe_watch",
                                "Editor Note": raw_note,
                                "Title": "The Useful Future: Corrected Subtitle",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            history = json.loads((output_dir / "candidate-history.json").read_text(encoding="utf-8"))
            self.assertEqual(history[0]["identity_key"], "the useful future|alex example")
            self.assertEqual(history[0]["editor_status"], "maybe_watch")
            self.assertEqual(history[0]["editor_note"], raw_note)
            self.assertEqual(history[0]["memo_queue_marker"], "")

            corrections = json.loads((output_dir / "proposed-corrections.json").read_text(encoding="utf-8"))
            self.assertEqual(
                corrections,
                [
                    {
                        "identity_key": "the useful future|alex example",
                        "field": "title",
                        "current_value": "The Useful Future",
                        "proposed_value": "The Useful Future: Corrected Subtitle",
                        "status": "proposed",
                    }
                ],
            )

    def test_editor_statuses_control_tracking_without_creating_unwanted_memo_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "Fixture Awards",
                                "family": "award",
                                "url": "https://example.test/awards",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "Interested Book",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/awards/interested",
                                    },
                                    {
                                        "title": "Watch Book",
                                        "primary_authors": ["Robin Example"],
                                        "source_url": "https://example.test/awards/watch",
                                    },
                                    {
                                        "title": "Deep Dive Book",
                                        "primary_authors": ["Taylor Example"],
                                        "source_url": "https://example.test/awards/deep",
                                    },
                                ],
                            }
                        ],
                        "returned_editor_rows": [
                            {
                                "identity_key": "interested book|alex example",
                                "Editor Status": "interested",
                                "Editor Note": "Promising, keep visible.",
                            },
                            {
                                "identity_key": "watch book|robin example",
                                "Editor Status": "maybe_watch",
                                "Editor Note": "Track, no memo.",
                            },
                            {
                                "identity_key": "deep dive book|taylor example",
                                "Editor Status": "needs_deeper_analysis",
                                "Editor Note": "Prepare deeper research when memo workflow exists.",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            history = json.loads((output_dir / "candidate-history.json").read_text(encoding="utf-8"))
            markers_by_identity = {record["identity_key"]: record["memo_queue_marker"] for record in history}
            self.assertEqual(markers_by_identity["interested book|alex example"], "")
            self.assertEqual(markers_by_identity["watch book|robin example"], "")
            self.assertEqual(
                markers_by_identity["deep dive book|taylor example"],
                "needs_deeper_analysis",
            )
            memo_queue = json.loads((output_dir / "memo-queue.json").read_text(encoding="utf-8"))
            self.assertEqual(
                memo_queue,
                [
                    {
                        "identity_key": "deep dive book|taylor example",
                        "title": "Deep Dive Book",
                        "editor_note": "Prepare deeper research when memo workflow exists.",
                        "status": "queued_for_future_memo",
                    }
                ],
            )

    def test_reject_and_already_known_suppress_normal_redisplay_without_material_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-21",
                        "candidate_history": [
                            {
                                "identity_key": "rejected book|alex example",
                                "editor_status": "reject",
                                "editor_note": "Not for us.",
                                "independent_signal_families": ["media/curated list"],
                                "primary_signal_strength": "major media list",
                                "russian_edition_status": "",
                                "visible_risk_status": "",
                            },
                            {
                                "identity_key": "known book|robin example",
                                "editor_status": "already_known",
                                "editor_note": "Already tracked by editor.",
                                "independent_signal_families": ["media/curated list"],
                                "primary_signal_strength": "major media list",
                                "russian_edition_status": "",
                                "visible_risk_status": "",
                            },
                        ],
                        "sources": [
                            {
                                "name": "Fixture Media",
                                "family": "media/curated list",
                                "url": "https://example.test/media",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "Rejected Book",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/media/rejected",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Another same-family media mention.",
                                    },
                                    {
                                        "title": "Known Book",
                                        "primary_authors": ["Robin Example"],
                                        "source_url": "https://example.test/media/known",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Another same-family media mention.",
                                    },
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows, [])
            suppressed = json.loads((output_dir / "suppressed-candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [(row["identity_key"], row["editor_status"], row["suppression_reason"]) for row in suppressed],
                [
                    ("rejected book|alex example", "reject", "suppressed by editor status without Material Change"),
                    (
                        "known book|robin example",
                        "already_known",
                        "suppressed by editor status without Material Change",
                    ),
                ],
            )

    def test_rejected_candidate_resurfaces_after_new_independent_signal_family(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "candidate_history": [
                            {
                                "identity_key": "rejected book|alex example",
                                "editor_status": "reject",
                                "editor_note": "Not for us before.",
                                "independent_signal_families": ["media/curated list"],
                                "primary_signal_strength": "major media list",
                                "russian_edition_status": "",
                                "visible_risk_status": "",
                            }
                        ],
                        "sources": [
                            {
                                "name": "Fixture Media",
                                "family": "media/curated list",
                                "url": "https://example.test/media",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "Rejected Book",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/media/rejected",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Same-family media mention.",
                                    }
                                ],
                            },
                            {
                                "name": "Fixture Awards",
                                "family": "award",
                                "url": "https://example.test/awards",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "Rejected Book",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/awards/rejected",
                                        "signal_strength": "shortlist",
                                        "evidence_note": "New award-family signal.",
                                    }
                                ],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["Title"] for row in rows], ["Rejected Book"])
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(candidates[0]["material_change_reasons"], ["new independent signal family"])
            suppressed = json.loads((output_dir / "suppressed-candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(suppressed, [])

    def test_same_family_duplicate_links_and_minor_enrichment_do_not_resurface_rejects(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "candidate_history": [
                            {
                                "identity_key": "rejected book|alex example",
                                "editor_status": "reject",
                                "editor_note": "Rejected before.",
                                "independent_signal_families": ["media/curated list"],
                                "primary_signal_strength": "major media list",
                                "russian_edition_status": "",
                                "visible_risk_status": "",
                            }
                        ],
                        "sources": [
                            {
                                "name": "Fixture Media",
                                "family": "media/curated list",
                                "url": "https://example.test/media",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "Rejected Book",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/media/rejected",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Same-family media mention with a refreshed short description.",
                                        "short_description": "Minor metadata enrichment.",
                                    },
                                    {
                                        "title": "Rejected Book",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/media/rejected-duplicate",
                                        "signal_strength": "minor mention",
                                        "evidence_note": "Duplicate same-family link.",
                                    },
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows, [])

    def test_duplicate_candidate_merge_preserves_content_description_from_later_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-06-10",
                        "sources": [
                            {
                                "name": "Fixture Ranking",
                                "family": "bestseller/ranking",
                                "url": "https://example.test/ranking",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "fixture_candidates": [
                                    {
                                        "title": "Merged Annotation Book",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/ranking/book",
                                        "signal_strength": "top ranking",
                                        "evidence_note": "Ranked on a nonfiction list.",
                                        "short_description": "Short fallback.",
                                    }
                                ],
                            },
                            {
                                "name": "Fixture Media",
                                "family": "media/curated list",
                                "url": "https://example.test/media",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "fixture_candidates": [
                                    {
                                        "title": "Merged Annotation Book",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/media/book",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Included in a media list.",
                                        "content_description": (
                                            "Более подробное скаутское описание раскрывает тему книги, "
                                            "отличительный ракурс и вероятный редакционный крючок."
                                        ),
                                    }
                                ],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(
                candidates[0]["content_description"],
                "Более подробное скаутское описание раскрывает тему книги, отличительный ракурс и вероятный редакционный крючок.",
            )
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertIn("Более подробное скаутское описание", rows[0]["Description"])
            self.assertTrue(has_cyrillic(rows[0]["Description"]))
            self.assertNotIn("Short fallback. Category/topic", rows[0]["Annotation"])

    def test_accepted_correction_explicit_reset_and_report_qa_are_material_change_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "candidate_history": [
                            {
                                "identity_key": "corrected book|alex example",
                                "editor_status": "reject",
                                "independent_signal_families": ["award"],
                                "primary_signal_strength": "shortlist",
                                "accepted_correction_changed_identity_or_status": True,
                            },
                            {
                                "identity_key": "reset book|robin example",
                                "editor_status": "already_known",
                                "independent_signal_families": ["award"],
                                "primary_signal_strength": "shortlist",
                                "explicit_reset": True,
                            },
                        ],
                        "report_qa": {
                            "reviewer": "Dima",
                            "status": "approved",
                            "note": "Technical review passed; not editor feedback.",
                        },
                        "sources": [
                            {
                                "name": "Fixture Awards",
                                "family": "award",
                                "url": "https://example.test/awards",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "Corrected Book",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/awards/corrected",
                                        "signal_strength": "shortlist",
                                    },
                                    {
                                        "title": "Reset Book",
                                        "primary_authors": ["Robin Example"],
                                        "source_url": "https://example.test/awards/reset",
                                        "signal_strength": "shortlist",
                                    },
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual({row["Title"] for row in rows}, {"Corrected Book", "Reset Book"})
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            reasons_by_title = {candidate["title"]: candidate["material_change_reasons"] for candidate in candidates}
            self.assertEqual(
                reasons_by_title["Corrected Book"],
                ["accepted correction changed identity or status"],
            )
            self.assertEqual(reasons_by_title["Reset Book"], ["explicit reset"])
            report_qa = json.loads((output_dir / "report-qa.json").read_text(encoding="utf-8"))
            self.assertEqual(
                report_qa,
                {
                    "reviewer": "Dima",
                    "status": "approved",
                    "note": "Technical review passed; not editor feedback.",
                },
            )
            history = json.loads((output_dir / "candidate-history.json").read_text(encoding="utf-8"))
            self.assertNotIn("Dima", {record.get("editor_status") for record in history})

    def test_strength_russian_status_and_visible_risk_changes_resurface_suppressed_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = copy_fixture(
                "source-configs", "status-change-resurface-pipeline.json", tmp_path / "sources.json"
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            reasons_by_title = {candidate["title"]: candidate["material_change_reasons"] for candidate in candidates}
            self.assertEqual(reasons_by_title["Stronger Book"], ["signal strength improved"])
            self.assertNotIn("Russian Changed", reasons_by_title)
            self.assertEqual(reasons_by_title["Risk Changed"], ["Visible Risk status changed"])
            suppressed = json.loads((output_dir / "suppressed-candidates.json").read_text(encoding="utf-8"))
            self.assertIn("Russian Changed", {candidate["title"] for candidate in suppressed})

    def test_signal_strength_breaks_priority_ties_between_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "Fixture Awards",
                                "family": "award",
                                "url": "https://example.test/awards",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "Shortlist Candidate",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/awards/shortlist",
                                        "signal_strength": "shortlist",
                                        "evidence_note": "Shortlisted for a fixture award.",
                                        "candidate_category": "popular science",
                                    },
                                    {
                                        "title": "Winner Candidate",
                                        "primary_authors": ["Robin Example"],
                                        "source_url": "https://example.test/awards/winner",
                                        "signal_strength": "winner",
                                        "evidence_note": "Won a fixture award.",
                                        "candidate_category": "popular science",
                                    },
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["Title"] for row in rows], ["Winner Candidate", "Shortlist Candidate"])

    def test_unsuitable_sources_are_reported_as_skipped_not_collected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "Paid Rights Database",
                                "family": "rights/deal",
                                "url": "https://example.test/paid-rights",
                                "access_status": "paid",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Subscription required; do not collect in MVP.",
                                "fixture_candidates": [
                                    {
                                        "title": "Should Not Be Collected",
                                        "source_url": "https://example.test/paid-rights/book",
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows, [])

            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["skipped_source_count"], 1)
            self.assertEqual(summary["raw_candidate_count"], 0)
            self.assertEqual(summary["editor_facing_count"], 0)

            source_runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
            self.assertEqual(source_runs[0]["status"], "skipped")
            self.assertIn("paid", source_runs[0]["reason"])

    def test_ineligible_mentions_are_internal_exclusions_not_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-21",
                        "sources": [
                            {
                                "name": "Fixture Media List",
                                "family": "media/curated list",
                                "url": "https://example.test/media",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "Narrow Certification Manual",
                                        "primary_authors": ["Dana Example"],
                                        "source_url": "https://example.test/media/manual",
                                        "signal_type": "media/curated list",
                                        "signal_strength": "minor mention",
                                        "evidence_note": "Listed in a tools roundup.",
                                        "candidate_category": "narrow manual",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            exclusions = json.loads((output_dir / "internal-exclusions.json").read_text(encoding="utf-8"))
            self.assertEqual(candidates, [])
            self.assertEqual(exclusions[0]["identity_key"], "narrow certification manual|dana example")
            self.assertEqual(exclusions[0]["exclusion_reason"], "narrow manual")
            self.assertEqual(exclusions[0]["exclusion_date"], "2026-05-21")
            self.assertEqual(exclusions[0]["evidence"][0]["source_name"], "Fixture Media List")

    def test_borderline_adult_nonfiction_remains_visible_with_lower_publisher_fit(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-21",
                        "sources": [
                            {
                                "name": "Fixture Public Affairs List",
                                "family": "media/curated list",
                                "url": "https://example.test/public-affairs",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "A Difficult Public Question",
                                        "primary_authors": ["Robin Example"],
                                        "source_url": "https://example.test/public-affairs/question",
                                        "signal_type": "media/curated list",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Included in a public-affairs list.",
                                        "candidate_category": "public affairs",
                                        "publisher_fit": "lower",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            exclusions = json.loads((output_dir / "internal-exclusions.json").read_text(encoding="utf-8"))
            self.assertEqual(candidates[0]["title"], "A Difficult Public Question")
            self.assertEqual(candidates[0]["publisher_fit"], "lower")
            self.assertEqual(exclusions, [])

    def test_similar_title_same_author_companion_work_is_not_merged_without_strong_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "Fixture Ranking",
                                "family": "bestseller/ranking",
                                "url": "https://example.test/ranking",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "The Useful Future",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/ranking/useful-future",
                                        "signal_type": "bestseller/ranking",
                                        "signal_strength": "top ranking",
                                        "evidence_note": "Ranked nonfiction book.",
                                        "candidate_category": "popular science",
                                    },
                                    {
                                        "title": "The Useful Future Workbook",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/ranking/useful-future-workbook",
                                        "signal_type": "bestseller/ranking",
                                        "signal_strength": "top ranking",
                                        "evidence_note": "Ranked companion workbook.",
                                        "candidate_category": "workbook",
                                    },
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [candidate["identity_key"] for candidate in candidates],
                [
                    "the useful future|alex example",
                    "the useful future workbook|alex example",
                ],
            )

    def test_inactive_sources_are_not_collected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "Inactive Open Awards",
                                "family": "award",
                                "url": "https://example.test/inactive-awards",
                                "active": False,
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Configured but disabled.",
                                "fixture_candidates": [
                                    {
                                        "title": "Inactive Candidate",
                                        "author": "Casey Example",
                                        "source_url": "https://example.test/inactive-awards/book",
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            mentions = json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8"))
            self.assertEqual(mentions, [])
            source_runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
            self.assertEqual(source_runs[0]["status"], "inactive")

    def test_found_in_russian_candidates_are_excluded_from_editor_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "Fixture Media List",
                                "family": "media/curated list",
                                "url": "https://example.test/media",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "Already Translated",
                                        "author": "Taylor Example",
                                        "russian_edition_status": "found in Russian",
                                        "russian_edition_url": "https://example.test/russian-edition",
                                        "source_url": "https://example.test/media/already-translated",
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows, [])

            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["raw_candidate_count"], 1)
            self.assertEqual(summary["excluded_candidate_count"], 1)
            self.assertEqual(summary["editor_facing_count"], 0)

    def test_configured_retailer_same_work_russian_edition_excludes_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-21",
                        "sources": [
                            {
                                "name": "Fixture Media List",
                                "family": "media/curated list",
                                "url": "https://example.test/media",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "Already Translated",
                                        "primary_authors": ["Taylor Example"],
                                        "source_url": "https://example.test/media/already-translated",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Included in a media list.",
                                        "candidate_category": "popular science",
                                    }
                                ],
                            }
                        ],
                        "russian_edition_checks": [
                            {
                                "retailer": "Ozon",
                                "url": "https://www.ozon.ru",
                                "fixture_results": [
                                    {
                                        "candidate_title": "Already Translated",
                                        "primary_authors": ["Taylor Example"],
                                        "match_type": "same-work Russian-language edition",
                                        "language": "Russian",
                                        "edition_url": "https://www.ozon.ru/product/already-translated-ru",
                                        "evidence_note": "Ozon lists a Russian-language edition of the same work.",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows, [])
            exclusions = json.loads((output_dir / "internal-exclusions.json").read_text(encoding="utf-8"))
            self.assertEqual(exclusions[0]["identity_key"], "already translated|taylor example")
            self.assertEqual(exclusions[0]["exclusion_reason"], "found in Russian")
            self.assertEqual(exclusions[0]["russian_edition_url"], "https://www.ozon.ru/product/already-translated-ru")
            self.assertEqual(exclusions[0]["evidence"][0]["retailer"], "Ozon")
            self.assertEqual(exclusions[0]["evidence"][0]["match_type"], "same-work Russian-language edition")

    def test_russian_check_artifact_records_each_checked_candidate_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-21",
                        "sources": [
                            {
                                "name": "Fixture Media List",
                                "family": "media/curated list",
                                "url": "https://example.test/media",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "Already Translated",
                                        "primary_authors": ["Taylor Example"],
                                        "source_url": "https://example.test/media/already-translated",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Included in a media list.",
                                        "candidate_category": "popular science",
                                    },
                                    {
                                        "title": "Open Opportunity",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/media/open-opportunity",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Included in a media list.",
                                        "candidate_category": "popular science",
                                    },
                                ],
                            }
                        ],
                        "russian_edition_checks": [
                            {
                                "retailer": "Ozon",
                                "url": "https://www.ozon.ru",
                                "fixture_results": [
                                    {
                                        "candidate_title": "Already Translated",
                                        "primary_authors": ["Taylor Example"],
                                        "match_type": "same-work Russian-language edition",
                                        "language": "Russian",
                                        "edition_url": "https://www.ozon.ru/product/already-translated-ru",
                                        "evidence_note": "Ozon lists a Russian-language edition of the same work.",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            russian_checks = json.loads((output_dir / "russian-edition-checks.json").read_text(encoding="utf-8"))
            self.assertEqual(
                russian_checks["retailer_runs"],
                [
                    {
                        "retailer": "Ozon",
                        "url": "https://www.ozon.ru",
                        "status": "checked",
                        "reason": "",
                        "fixture_result_count": 1,
                    }
                ],
            )
            candidate_checks = {check["identity_key"]: check for check in russian_checks["candidate_checks"]}
            self.assertEqual(
                set(candidate_checks),
                {"already translated|taylor example", "open opportunity|alex example"},
            )
            self.assertEqual(
                candidate_checks["already translated|taylor example"]["resolved_status"],
                "found in Russian",
            )
            self.assertEqual(
                candidate_checks["already translated|taylor example"]["evidence_url"],
                "https://www.ozon.ru/product/already-translated-ru",
            )
            self.assertEqual(
                candidate_checks["open opportunity|alex example"]["resolved_status"],
                "unclear",
            )
            self.assertEqual(
                candidate_checks["open opportunity|alex example"]["decision_reason"],
                "Russian-edition verification is incomplete",
            )

    def test_imported_foreign_language_retailer_listing_is_not_found_in_russian(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-21",
                        "sources": [
                            {
                                "name": "Fixture Ranking",
                                "family": "bestseller/ranking",
                                "url": "https://example.test/ranking",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "Imported Only",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/ranking/imported-only",
                                        "signal_strength": "top ranking",
                                        "evidence_note": "Ranked nonfiction book.",
                                        "candidate_category": "popular science",
                                    }
                                ],
                            }
                        ],
                        "russian_edition_checks": [
                            {
                                "retailer": "Labirint",
                                "url": "https://www.labirint.ru",
                                "fixture_results": [
                                    {
                                        "candidate_title": "Imported Only",
                                        "primary_authors": ["Alex Example"],
                                        "match_type": "imported foreign-language listing",
                                        "language": "English",
                                        "edition_url": "https://www.labirint.ru/books/imported-only-en",
                                        "evidence_note": "Labirint lists an imported English edition.",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["Title"] for row in rows], ["Imported Only"])
            self.assertEqual(rows[0]["Russian-edition status"], "not found in Russian")
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(candidates[0]["russian_edition_status"], "not found in Russian")
            self.assertEqual(
                candidates[0]["russian_edition_note"],
                "Configured retailer checks found only imported foreign-language listings, not Russian-language editions.",
            )

    def test_russian_retailer_skipped_and_failed_checks_are_reported_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
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
                                        "title": "Check Health Book",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/awards/check-health-book",
                                        "signal_strength": "shortlist",
                                        "evidence_note": "Shortlisted for a fixture award.",
                                        "candidate_category": "popular science",
                                    }
                                ],
                            }
                        ],
                        "russian_edition_checks": [
                            {
                                "retailer": "Ozon",
                                "url": "https://www.ozon.ru",
                                "access_status": "blocked",
                                "notes": "CAPTCHA during fixture run.",
                                "fixture_results": [],
                            },
                            {
                                "retailer": "Book24",
                                "url": "https://book24.ru",
                                "fixture_error": "fixture file is invalid JSON",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            russian_checks = json.loads((output_dir / "russian-edition-checks.json").read_text(encoding="utf-8"))
            self.assertEqual(
                {row["retailer"]: row["status"] for row in russian_checks["retailer_runs"]},
                {"Ozon": "blocked", "Book24": "failed"},
            )
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["russian_check_skipped_count"], 0)
            self.assertEqual(summary["russian_check_blocked_count"], 1)
            self.assertEqual(summary["russian_check_failed_count"], 1)
            self.assertNotIn("1 Russian retailer check skipped", summary["major_warnings"])
            self.assertIn("1 Russian retailer check failed", summary["major_warnings"])

    def test_current_weekly_pilot_uses_100_row_readiness_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            recorded_path = tmp_path / "current-recorded.json"
            mentions = [
                {
                    "title": f"Current Signal Book {index:02d}",
                    "primary_authors": [f"Author {index:02d}"],
                    "source_url": f"https://example.test/current/book-{index:02d}",
                    "source_check_date": "2026-06-10",
                    "signal_type": "media/curated list",
                    "signal_strength": "major media list",
                    "evidence_note": f"Recorded from configured current source item {index:02d}.",
                    "evidence_url": f"https://example.test/current/book-{index:02d}",
                    "candidate_category": "adult nonfiction",
                    "publisher_fit": "clear fit",
                    "russian_edition_status": "not found in Russian",
                    "visible_risk_status": "no visible risk",
                    "visible_risk_status_source": "manual",
                    "content_description": f"Оригинальное скаутское описание для текущей сигнальной книги {index:02d}. Второе предложение проверяет полноту русской аннотации.",
                }
                for index in range(1, 26)
            ]
            mentions.append(
                {
                    "title": "Current Signal Book 01",
                    "primary_authors": ["Author 01"],
                    "source_url": "https://example.test/current/same-family-duplicate",
                    "source_check_date": "2026-06-10",
                    "signal_type": "media/curated list",
                    "signal_strength": "minor mention",
                    "evidence_note": "Same-family duplicate evidence should merge without increasing signal count.",
                    "evidence_locator": "Current source page, duplicate entry for Current Signal Book 01.",
                    "candidate_category": "adult nonfiction",
                    "russian_edition_status": "not found in Russian",
                    "visible_risk_status": "no visible risk",
                    "visible_risk_status_source": "manual",
                }
            )
            mentions.append(
                {
                    "title": "Already Russian Current Book",
                    "primary_authors": ["Excluded Author"],
                    "source_url": "https://example.test/current/already-russian",
                    "source_check_date": "2026-06-10",
                    "signal_type": "media/curated list",
                    "signal_strength": "major media list",
                    "evidence_note": "Configured current source recorded this book, but Russian evidence excludes it.",
                    "candidate_category": "adult nonfiction",
                    "russian_edition_status": "found in Russian",
                    "russian_edition_url": "https://example.test/ru/already-russian",
                    "visible_risk_status": "no visible risk",
                    "visible_risk_status_source": "manual",
                }
            )
            recorded_path.write_text(json.dumps({"mentions": mentions}), encoding="utf-8")
            config_path = tmp_path / "current-pilot.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-06-10",
                        "pilot_mode": True,
                        "editor_row_target": 100,
                        "pilot_row_min": 100,
                        "pilot_row_max": 100,
                        "sources": [
                            {
                                "name": "Current Recorded Media",
                                "family": "media/curated list",
                                "url": "https://example.test/current",
                                "access_status": "open",
                                "extraction_method": "recorded",
                                "check_frequency": "weekly",
                                "language": "en",
                                "recorded_mentions_file": str(recorded_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["editor_row_target"], 100)
            self.assertEqual(summary["editor_facing_count"], 25)
            self.assertIn("fewer than 100 editor-facing rows produced", summary["shortfall_reason"])
            # Issue #714: every status was supplied with its mention, so the
            # Visible Risk screen concluded on none of the rows.
            self.assertEqual(len(summary["major_warnings"]), 3)
            self.assertIn("Visible Risk screen concluded for none of 25 editor-facing rows", summary["major_warnings"])
            self.assertIn("fewer than target 100", " ".join(summary["major_warnings"]))
            self.assertIn("25 editor-facing rows have unclear Russian-edition status", summary["major_warnings"])
            pilot_status = json.loads((output_dir / "pilot-status.json").read_text(encoding="utf-8"))
            self.assertFalse(pilot_status["ready_for_editor"])
            self.assertEqual(pilot_status["readiness_basis"]["editor_row_target"], 100)
            self.assertIn("fewer than target 100", pilot_status["caveats"][0])
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 25)
            self.assertEqual(rows[0]["Title"], "Current Signal Book 01")
            self.assertTrue(all(has_cyrillic(row["Description"]) for row in rows))
            self.assertIn("https://example.test/current/same-family-duplicate", rows[0]["Links"])
            exclusions = json.loads((output_dir / "internal-exclusions.json").read_text(encoding="utf-8"))
            self.assertEqual([exclusion["title"] for exclusion in exclusions], ["Already Russian Current Book"])

    def test_pilot_mode_caps_editor_rows_at_pilot_row_max_but_keeps_full_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            recorded_path = tmp_path / "current-recorded.json"
            mentions = [
                {
                    "title": f"Pilot Cap Book {index:02d}",
                    "primary_authors": [f"Author {index:02d}"],
                    "source_url": f"https://example.test/current/book-{index:02d}",
                    "source_check_date": "2026-06-10",
                    "signal_type": "media/curated list",
                    "signal_strength": "major media list",
                    "evidence_note": f"Recorded from configured current source item {index:02d}.",
                    "evidence_url": f"https://example.test/current/book-{index:02d}",
                    "candidate_category": "adult nonfiction",
                    "publisher_fit": "clear fit",
                    "russian_edition_status": "not found in Russian",
                    "visible_risk_status": "no visible risk",
                    "visible_risk_status_source": "manual",
                    "content_description": f"Оригинальное скаутское описание для книги {index:02d}. Второе предложение проверяет полноту русской аннотации.",
                }
                for index in range(1, 21)
            ]
            recorded_path.write_text(json.dumps({"mentions": mentions}), encoding="utf-8")
            config_path = tmp_path / "current-pilot.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-06-10",
                        "pilot_mode": True,
                        "editor_row_target": 12,
                        "pilot_row_min": 12,
                        "pilot_row_max": 12,
                        "sources": [
                            {
                                "name": "Current Recorded Media",
                                "family": "media/curated list",
                                "url": "https://example.test/current",
                                "access_status": "open",
                                "extraction_method": "recorded",
                                "check_frequency": "weekly",
                                "language": "en",
                                "recorded_mentions_file": str(recorded_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(len(candidates), 12)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 12)
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["editor_facing_count"], 12)
            # Truncation only trims the editor-facing slice: the full pre-truncation
            # set still reaches candidate-history snapshots, so trimmed candidates are
            # not lost and can resurface on a later run.
            history = json.loads((output_dir / "candidate-history.json").read_text(encoding="utf-8"))
            self.assertEqual(len(history), 20)

    def test_current_weekly_pilot_shortfall_distinguishes_source_scarcity(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "current-shortfall.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-06-10",
                        "pilot_mode": True,
                        "editor_row_target": 25,
                        "pilot_row_min": 25,
                        "pilot_row_max": 25,
                        "sources": [
                            {
                                "name": "Current Open Source",
                                "family": "media/curated list",
                                "url": "https://example.test/current",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "fixture_candidates": [
                                    {
                                        "title": "Only Current Book",
                                        "primary_authors": ["One Author"],
                                        "source_url": "https://example.test/current/only",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Only one configured current source item was available.",
                                    }
                                ],
                            },
                            {
                                "name": "Current Paid Source",
                                "family": "media/curated list",
                                "access_status": "paid",
                                "language": "en",
                                "notes": "Paywalled source logged instead of scraped.",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["editor_row_target"], 25)
            self.assertIn("source scarcity or source-boundary limits", summary["shortfall_reason"])
            self.assertIn("1 configured source skipped", summary["shortfall_reason"])
            self.assertIn("1 editor-facing rows produced; fewer than target 25", summary["major_warnings"])
            source_runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
            self.assertEqual([run["status"] for run in source_runs], ["collected", "skipped"])

    def test_checked_in_current_weekly_config_does_not_ship_placeholder_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result, output_dir = run_pipeline(tmp_path, Path("configs/current-weekly-pilot.json"))

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            combined_text = "\n".join("\n".join([row["Title"], row["Description"], row["Annotation"]]) for row in rows)
            self.assertNotIn("Current Pilot Signal", combined_text)
            self.assertNotIn("Original scouting note", combined_text)
            self.assertGreater(len(rows), 0)
            self.assertTrue(all(row["Title"].strip() for row in rows))
            self.assertTrue(all(row["Description"].strip() for row in rows))
            self.assertTrue(all(has_cyrillic(row["Description"]) for row in rows))
            pilot_status = json.loads((output_dir / "pilot-status.json").read_text(encoding="utf-8"))
            self.assertEqual(pilot_status["editor_facing_count"], len(rows))

    def test_ambiguous_retailer_match_resolves_to_unclear_not_found_in_russian(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
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
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "Almost Matched",
                                        "primary_authors": ["Robin Example"],
                                        "source_url": "https://example.test/awards/almost-matched",
                                        "signal_strength": "shortlist",
                                        "evidence_note": "Shortlisted for a fixture award.",
                                        "candidate_category": "popular science",
                                    }
                                ],
                            }
                        ],
                        "russian_edition_checks": [
                            {
                                "retailer": "LitRes",
                                "url": "https://www.litres.ru",
                                "fixture_results": [
                                    {
                                        "candidate_title": "Almost Matched",
                                        "primary_authors": ["Robin Example"],
                                        "match_type": "ambiguous author/title match",
                                        "language": "Russian",
                                        "edition_url": "https://www.litres.ru/book/almost-matched-ru",
                                        "evidence_note": "Title and author are similar but same-work identity is unclear.",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["Title"] for row in rows], ["Almost Matched"])
            self.assertEqual(rows[0]["Russian-edition status"], "unclear")
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(candidates[0]["russian_edition_status"], "unclear")
            self.assertEqual(
                candidates[0]["russian_edition_note"],
                "Configured retailer checks found ambiguous Russian-language matches; same-work identity needs review.",
            )

    def test_unclear_russian_status_writes_focused_review_queue_and_summary_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
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
                                        "title": "Needs Review",
                                        "primary_authors": ["Robin Example"],
                                        "source_url": "https://example.test/awards/needs-review",
                                        "signal_strength": "shortlist",
                                        "evidence_note": "Shortlisted for a fixture award.",
                                        "candidate_category": "popular science",
                                    },
                                    {
                                        "title": "No Review",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/awards/no-review",
                                        "signal_strength": "shortlist",
                                        "evidence_note": "Shortlisted for a fixture award.",
                                        "candidate_category": "popular science",
                                    },
                                ],
                            }
                        ],
                        "russian_edition_checks": [
                            {
                                "retailer": "LitRes",
                                "url": "https://www.litres.ru",
                                "fixture_results": [
                                    {
                                        "candidate_title": "Needs Review",
                                        "primary_authors": ["Robin Example"],
                                        "match_type": "ambiguous author/title match",
                                        "language": "Russian",
                                        "edition_url": "https://www.litres.ru/book/needs-review-ru",
                                        "evidence_note": "Title and author are similar but same-work identity is unclear.",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "russian-review-queue.csv").open(encoding="utf-8", newline="") as handle:
                queue = list(csv.DictReader(handle))
            self.assertEqual([row["Title"] for row in queue], ["Needs Review", "No Review"])
            self.assertEqual(queue[0]["Author"], "Robin Example")
            self.assertEqual(queue[0]["Possible match URL"], "https://www.litres.ru/book/needs-review-ru")
            self.assertIn("ambiguous", queue[0]["Review reason"])

            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["russian_check_unclear_count"], 2)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual({row["Title"] for row in rows}, {"Needs Review", "No Review"})

    def test_forthcoming_original_remains_unclear_with_incomplete_verification_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-21",
                        "sources": [
                            {
                                "name": "Fixture Forthcoming List",
                                "family": "media/curated list",
                                "url": "https://example.test/forthcoming",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "Future Original",
                                        "primary_authors": ["Dana Example"],
                                        "source_url": "https://example.test/forthcoming/future-original",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Included in a forthcoming books list.",
                                        "candidate_category": "popular science",
                                        "publication_status": "forthcoming",
                                    }
                                ],
                            }
                        ],
                        "russian_edition_checks": [
                            {
                                "retailer": "Ozon",
                                "url": "https://www.ozon.ru",
                                "fixture_results": [],
                            },
                            {
                                "retailer": "Book24",
                                "url": "https://book24.ru",
                                "fixture_results": [],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(candidates[0]["russian_edition_status"], "unclear")
            self.assertIn("empty fixture", candidates[0]["russian_edition_note"].lower())
            with (output_dir / "editor-facing.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["Russian-edition status"], "unclear")

    def test_generated_search_notes_do_not_overwrite_display_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-21",
                        "sources": [
                            {
                                "name": "Fixture Non-English List",
                                "family": "media/curated list",
                                "url": "https://example.test/non-english",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "fr",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "Le Monde Pratique",
                                        "primary_authors": ["Élodie Martin"],
                                        "english_title": "The Practical World",
                                        "source_url": "https://example.test/non-english/le-monde-pratique",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Included in a media list.",
                                        "candidate_category": "business",
                                        "search_translation": "Практический мир",
                                        "author_transliteration_note": "Элоди Мартен",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["Title"], "Le Monde Pratique")
            self.assertEqual(rows[0]["Author"], "Élodie Martin")
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(
                candidates[0]["russian_search_notes"],
                {
                    "search_translation": "Практический мир",
                    "author_transliteration_note": "Элоди Мартен",
                },
            )

    def test_russian_query_attempts_are_bounded_audit_evidence_not_display_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-21",
                        "sources": [
                            {
                                "name": "Fixture Non-English List",
                                "family": "media/curated list",
                                "url": "https://example.test/non-english",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "fr",
                                "fixture_candidates": [
                                    {
                                        "title": "Le Monde Pratique",
                                        "primary_authors": ["Elodie Martin"],
                                        "english_title": "The Practical World",
                                        "isbn": "9781234567890",
                                        "source_url": "https://example.test/non-english/le-monde-pratique",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Included in a media list.",
                                        "candidate_category": "business",
                                        "search_translation": "Практический мир",
                                        "author_transliteration_note": "Элоди Мартен",
                                    }
                                ],
                            }
                        ],
                        "russian_edition_checks": [
                            {
                                "retailer": "Book24",
                                "url": "https://book24.ru",
                                "fixture_results": [],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            russian_checks = json.loads((output_dir / "russian-edition-checks.json").read_text(encoding="utf-8"))
            attempts = russian_checks["candidate_checks"][0]["query_attempts"]
            self.assertEqual(
                attempts,
                [
                    {"kind": "title", "query": "Le Monde Pratique", "search_aid": False},
                    {"kind": "author", "query": "Elodie Martin", "search_aid": False},
                    {"kind": "title_plus_author", "query": "Le Monde Pratique Elodie Martin", "search_aid": False},
                    {"kind": "isbn", "query": "9781234567890", "search_aid": False},
                    {"kind": "search_translation", "query": "Практический мир", "search_aid": True},
                    {"kind": "author_transliteration_note", "query": "Элоди Мартен", "search_aid": True},
                ],
            )
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["Title"], "Le Monde Pratique")
            self.assertEqual(rows[0]["Author"], "Elodie Martin")

    def test_previously_found_in_russian_candidate_reenters_when_status_changes_to_unclear(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-21",
                        "excluded_candidate_history": [
                            {
                                "identity_key": "previously translated|taylor example",
                                "exclusion_reason": "found in Russian",
                                "russian_edition_url": "https://www.ozon.ru/product/old-match",
                            }
                        ],
                        "sources": [
                            {
                                "name": "Fixture Media List",
                                "family": "media/curated list",
                                "url": "https://example.test/media",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "Previously Translated",
                                        "primary_authors": ["Taylor Example"],
                                        "source_url": "https://example.test/media/previously-translated",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Included in a media list.",
                                        "candidate_category": "popular science",
                                    }
                                ],
                            }
                        ],
                        "russian_edition_checks": [
                            {
                                "retailer": "Ozon",
                                "url": "https://www.ozon.ru",
                                "fixture_results": [
                                    {
                                        "candidate_title": "Previously Translated",
                                        "primary_authors": ["Taylor Example"],
                                        "match_type": "ambiguous author/title match",
                                        "language": "Russian",
                                        "edition_url": "https://www.ozon.ru/product/new-ambiguous-match",
                                        "evidence_note": "Current Ozon match is ambiguous.",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["Title"] for row in rows], ["Previously Translated"])
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(candidates[0]["russian_edition_status"], "unclear")
            self.assertEqual(
                candidates[0]["reentry_reason"],
                "Russian status changed from found in Russian to unclear",
            )
            exclusions = json.loads((output_dir / "internal-exclusions.json").read_text(encoding="utf-8"))
            self.assertEqual(exclusions, [])

    def test_previously_found_in_russian_candidate_reenters_when_verification_remains_unclear(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-21",
                        "excluded_candidate_history": [
                            {
                                "identity_key": "previously translated|taylor example",
                                "exclusion_reason": "found in Russian",
                                "russian_edition_url": "https://www.ozon.ru/product/old-match",
                            }
                        ],
                        "sources": [
                            {
                                "name": "Fixture Media List",
                                "family": "media/curated list",
                                "url": "https://example.test/media",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "fixture_candidates": [
                                    {
                                        "title": "Previously Translated",
                                        "primary_authors": ["Taylor Example"],
                                        "source_url": "https://example.test/media/previously-translated",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Included in a media list.",
                                        "candidate_category": "popular science",
                                    }
                                ],
                            }
                        ],
                        "russian_edition_checks": [
                            {
                                "retailer": "Ozon",
                                "url": "https://www.ozon.ru",
                                "fixture_results": [],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["Title"] for row in rows], ["Previously Translated"])
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(candidates[0]["russian_edition_status"], "unclear")
            self.assertEqual(
                candidates[0]["reentry_reason"],
                "Russian status changed from found in Russian to unclear",
            )
            exclusions = json.loads((output_dir / "internal-exclusions.json").read_text(encoding="utf-8"))
            self.assertEqual(exclusions, [])

    def test_russian_publisher_site_checks_are_deferred_not_used_for_mvp_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
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
                                "notes": "Deterministic fixture source.",
                                "fixture_candidates": [
                                    {
                                        "title": "Publisher Site Match",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/awards/publisher-site-match",
                                        "signal_strength": "shortlist",
                                        "evidence_note": "Shortlisted for a fixture award.",
                                        "candidate_category": "popular science",
                                    }
                                ],
                            }
                        ],
                        "russian_edition_checks": [
                            {
                                "retailer": "Russian Publisher Site",
                                "url": "https://publisher.example.test",
                                "fixture_results": [
                                    {
                                        "candidate_title": "Publisher Site Match",
                                        "primary_authors": ["Alex Example"],
                                        "match_type": "same-work Russian-language edition",
                                        "language": "Russian",
                                        "edition_url": "https://publisher.example.test/book",
                                        "evidence_note": "Publisher site has a Russian edition page.",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["Title"] for row in rows], ["Publisher Site Match"])
            self.assertEqual(rows[0]["Russian-edition status"], "")
            check_runs = json.loads((output_dir / "russian-edition-checks.json").read_text(encoding="utf-8"))
            self.assertEqual(
                check_runs["retailer_runs"],
                [
                    {
                        "retailer": "Russian Publisher Site",
                        "url": "https://publisher.example.test",
                        "status": "deferred",
                        "reason": "Russian-edition MVP checks are scoped to Ozon, Labirint, LitRes, Chitai-gorod, and Book24",
                        "fixture_result_count": 1,
                    }
                ],
            )

    def test_general_web_or_topic_search_sources_cannot_create_mentions(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "Topic Search Seed",
                                "family": "topic search",
                                "url": "https://example.test/search?q=ai+leadership+books",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Targeted search may enrich later, but cannot create candidates.",
                                "fixture_candidates": [
                                    {
                                        "title": "Search-Only Book",
                                        "author": "Sam Example",
                                        "source_url": "https://example.test/search-only-book",
                                        "signal_type": "topic search",
                                        "signal_strength": "search result",
                                        "evidence_note": "Search result from a topic query.",
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            mentions = json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8"))
            self.assertEqual(mentions, [])
            with (output_dir / "editor-facing.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows, [])

    def test_audiobook_rankings_require_underlying_book_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "Fixture Audiobook Ranking",
                                "family": "audiobook ranking",
                                "url": "https://example.test/audio",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Deterministic audiobook fixture.",
                                "fixture_candidates": [
                                    {
                                        "title": "Book-Backed Audio",
                                        "author": "Morgan Example",
                                        "source_url": "https://example.test/audio/book-backed",
                                        "signal_type": "audiobook ranking",
                                        "signal_strength": "top ranking",
                                        "evidence_note": "Ranked audiobook for an underlying nonfiction book.",
                                        "underlying_work_type": "book",
                                    },
                                    {
                                        "title": "Audio-Only Lecture",
                                        "author": "Jordan Example",
                                        "source_url": "https://example.test/audio/lecture",
                                        "signal_type": "audiobook ranking",
                                        "signal_strength": "top ranking",
                                        "evidence_note": "Audio-only item without an underlying book/work.",
                                        "underlying_work_type": "audio_only",
                                    },
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            mentions = json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8"))
            self.assertEqual([mention["title"] for mention in mentions], ["Book-Backed Audio"])

    def test_rights_deal_mentions_are_represented_without_claiming_absence(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {
                                "name": "Fixture Rights News",
                                "family": "rights/deal",
                                "url": "https://example.test/rights-news",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Open rights/deal fixture.",
                                "fixture_candidates": [
                                    {
                                        "title": "Rights-Mentioned Book",
                                        "primary_authors": ["Riley Example"],
                                        "source_url": "https://example.test/rights-news/book",
                                        "signal_type": "rights/deal mention",
                                        "signal_strength": "deal mention",
                                        "evidence_note": "Open source reports a rights/deal mention.",
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            mentions = json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8"))
            self.assertEqual(mentions[0]["signal_type"], "rights/deal mention")
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(
                summary["rights_deal_evidence_note"],
                "absence of open rights/deal mentions is not evidence of no rights activity",
            )

    def test_stage4_replay_sources_flow_end_to_end_from_current_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "current-weekly"
            config_path = REPO_ROOT / "configs" / "current-weekly-pilot.json"
            result, _ = run_pipeline(tmp_path, config_path, output_dir=output_dir)

            self.assertEqual(result.returncode, 0, result.stderr)
            source_runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
            stage4_runs = {
                run["source_name"]: run
                for run in source_runs
                if run["source_name"]
                in {
                    "National Book Awards 2026 Nonfiction",
                    "Goodreads Choice Awards 2026 Nonfiction",
                    "Audiobooks.com Nonfiction Audiobook Bestsellers",
                }
            }
            self.assertEqual(
                set(stage4_runs),
                {
                    "National Book Awards 2026 Nonfiction",
                    "Goodreads Choice Awards 2026 Nonfiction",
                    "Audiobooks.com Nonfiction Audiobook Bestsellers",
                },
            )
            self.assertTrue(all(run["status"] == "collected" for run in stage4_runs.values()))
            expected_counts = {
                "National Book Awards 2026 Nonfiction": 1,
                # Issue #996: the 2026 replay carries the twenty opening-round nominees.
                "Goodreads Choice Awards 2026 Nonfiction": 20,
                "Audiobooks.com Nonfiction Audiobook Bestsellers": 2,
            }
            for name, run in stage4_runs.items():
                self.assertEqual(run["valid_mention_count"], expected_counts[name])

            mentions = json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8"))
            self.assertIn("reader-interest", {mention["source_family"] for mention in mentions})
            self.assertIn("award", {mention["source_family"] for mention in mentions})
            self.assertIn("audiobook ranking", {mention["source_family"] for mention in mentions})
            # Issue #766 retired the Publishers Marketplace Rights Offerings page; Issue #945 restores the
            # rights/deal family with the PM deals emails, so that source is the only one in the family.
            self.assertEqual(
                {mention["source_name"] for mention in mentions if mention["source_family"] == "rights/deal"},
                {"Publishers Marketplace Deals"},
            )
            self.assertEqual(
                next(run for run in source_runs if run["source_name"] == "Publishers Marketplace Rights Offerings")[
                    "status"
                ],
                "inactive",
            )

            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            summaries_by_title = {candidate["title"]: candidate["signal_summary"] for candidate in candidates}
            # Issue #996: a 2026 Goodreads nominee that no other replay source lists.
            self.assertIn("reader-interest", summaries_by_title["Birth Vibes"])
            self.assertIn(
                "award", summaries_by_title["The Midnight Special: The Secret Prison History of American Music"]
            )
            self.assertIn("audiobook ranking", summaries_by_title["Atomic Habits"])
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            # Issue #379: echo the configured target instead of a hardcoded number, so
            # tuning the row cap or the target does not force a test rebase. Read the
            # same resolved path the run used, not a second relative literal.
            pilot_config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["editor_row_target"], pilot_config["editor_row_target"])
            self.assertEqual(
                summary["rights_deal_evidence_note"],
                "absence of open rights/deal mentions is not evidence of no rights activity",
            )

    def test_signal_diversity_merges_three_families_and_same_family_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = copy_fixture("source-configs", "signal-diversity-pipeline.json", tmp_path / "sources.json")

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            diversity = next(candidate for candidate in candidates if candidate["title"] == "Signal Diversity Book")
            self.assertEqual(diversity["independent_signal_count"], 3)
            self.assertEqual(
                diversity["independent_signal_families"],
                ["award", "reader-interest", "audiobook ranking"],
            )
            self.assertEqual(len(diversity["source_evidence"]), 4)
            self.assertIn("award, reader-interest, audiobook ranking", diversity["signal_summary"])
            self.assertEqual(candidates[0]["title"], "Signal Diversity Book")
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["Title"] for row in rows], ["Signal Diversity Book", "Single Signal Book"])
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["editor_row_target"], 25)

    def test_new_independent_signal_family_resurfaces_rejected_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "candidate_history": [
                            {
                                "identity_key": "resurfaced book|alex example",
                                "editor_status": "reject",
                                "independent_signal_families": ["award"],
                                "primary_signal_strength": "reader choice winner",
                            }
                        ],
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
                                        "title": "Resurfaced Book",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/awards/book",
                                        "signal_strength": "shortlist",
                                    }
                                ],
                            },
                            {
                                "name": "Fixture Readers",
                                "family": "reader-interest",
                                "url": "https://example.test/readers",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "fixture_candidates": [
                                    {
                                        "title": "Resurfaced Book",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/readers/book",
                                        "signal_strength": "reader choice winner",
                                        "evidence_note": "New reader-interest signal.",
                                    }
                                ],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            self.assertEqual([candidate["title"] for candidate in candidates], ["Resurfaced Book"])
            self.assertEqual(candidates[0]["material_change_reasons"], ["new independent signal family"])
            suppressed = json.loads((output_dir / "suppressed-candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(suppressed, [])

    def test_weekly_mvp_fixture_run_exercises_integrated_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "weekly-run"
            result, _ = run_pipeline(tmp_path, Path("configs/weekly-mvp-fixture.json"), output_dir=output_dir)

            self.assertEqual(result.returncode, 0, result.stderr)
            expected_artifacts = {
                "candidate-history.json",
                "editor-facing.csv",
                "internal-exclusions.json",
                "internal-pre-suppression-candidates.json",
                "memo-queue.json",
                "proposed-corrections.json",
                "report-qa.json",
                "run-summary.json",
                "russian-edition-checks.json",
                "scouting-candidates.json",
                "source-mentions.json",
                "source-proposals.json",
                "source-runs.json",
                "source-metrics.json",
                "source-review.md",
                "derived-rule-suggestions.json",
                "derived-rule-suggestions.md",
                "russian-review-queue.csv",
                "suppressed-candidates.json",
                "hidden-by-rule.json",
            }
            self.assertEqual({path.name for path in output_dir.iterdir()}, expected_artifacts)

            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertLessEqual(len(rows), 10)
            self.assertEqual(
                [row["Title"] for row in rows],
                ["Clean Signals", "Resurfacing Title", "Risk Visible"],
            )
            self.assertEqual(rows[0]["Priority label"], "B")
            self.assertEqual(rows[0]["Editor status"], "interested")
            self.assertEqual(rows[2]["Visible-risk status"], "clear visible risk")
            self.assertEqual(rows[2]["Priority label"], "D")
            self.assertNotIn("Already Russian", [row["Title"] for row in rows])
            self.assertNotIn("Known Workflow", [row["Title"] for row in rows])
            self.assertNotIn("Should Never Discover", [row["Title"] for row in rows])

            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            resurfacing = next(candidate for candidate in candidates if candidate["title"] == "Resurfacing Title")
            self.assertEqual(resurfacing["material_change_reasons"], ["signal strength improved"])
            clean = next(candidate for candidate in candidates if candidate["title"] == "Clean Signals")
            self.assertEqual(clean["independent_signal_count"], 2)

            exclusions = json.loads((output_dir / "internal-exclusions.json").read_text(encoding="utf-8"))
            self.assertEqual(
                {row["exclusion_reason"] for row in exclusions},
                {"found in Russian", "fiction", "topic-only"},
            )
            suppressed = json.loads((output_dir / "suppressed-candidates.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [(row["identity_key"], row["editor_status"]) for row in suppressed],
                [("known workflow|sam example", "already_known")],
            )
            source_runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
            self.assertEqual(
                {row["source_name"]: row["status"] for row in source_runs},
                {
                    "Fixture Awards": "collected",
                    "Fixture Media": "collected",
                    "Topic Search Seed": "ignored",
                    "Paid Rights Database": "skipped",
                },
            )
            russian_checks = json.loads((output_dir / "russian-edition-checks.json").read_text(encoding="utf-8"))
            self.assertEqual(
                {row["retailer"]: row["status"] for row in russian_checks["retailer_runs"]},
                {"Ozon": "checked", "Russian Publisher Site": "deferred"},
            )
            history = json.loads((output_dir / "candidate-history.json").read_text(encoding="utf-8"))
            self.assertEqual(
                {row["identity_key"]: row.get("editor_status", "") for row in history},
                {
                    "known workflow|sam example": "already_known",
                    "resurfacing title|jamie example": "reject",
                    "clean signals|alex example": "interested",
                    "risk visible|morgan example": "needs_deeper_analysis",
                },
            )
            memo_queue = json.loads((output_dir / "memo-queue.json").read_text(encoding="utf-8"))
            self.assertEqual([row["identity_key"] for row in memo_queue], ["risk visible|morgan example"])
            report_qa = json.loads((output_dir / "report-qa.json").read_text(encoding="utf-8"))
            self.assertEqual(report_qa["status"], "fixture demo reviewed")

            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["skipped_source_count"], 1)
            self.assertEqual(summary["raw_candidate_count"], 8)
            self.assertEqual(summary["excluded_candidate_count"], 3)
            self.assertEqual(summary["editor_facing_count"], 3)
            self.assertEqual(
                summary["shortfall_reason"],
                "fewer than 10 editor-facing rows produced; source scarcity or source-boundary limits: "
                "1 configured source skipped, 1 non-discovery source ignored",
            )
            self.assertEqual(
                summary["major_warnings"],
                [
                    "1 configured source skipped",
                    "1 configured discovery source ignored",
                    "3 editor-facing rows produced; fewer than target 10",
                    "3 editor-facing rows have unclear Russian-edition status",
                ],
            )

    def test_bookpage_collector_demo_runs_end_to_end_without_live_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "bookpage-demo"
            result, _ = run_pipeline(tmp_path, Path("configs/bookpage-demo.json"), output_dir=output_dir)

            self.assertEqual(result.returncode, 0, result.stderr)
            expected = {
                "editor-facing.csv",
                "source-mentions.json",
                "scouting-candidates.json",
                "source-runs.json",
                "run-summary.json",
            }
            self.assertTrue(expected <= {path.name for path in output_dir.iterdir()})
            mentions = json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8"))
            self.assertEqual([mention["source_name"] for mention in mentions], ["BookPage Best Nonfiction"] * 10)
            self.assertTrue(all(mention["source_family"] == "media/curated list" for mention in mentions))
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(
                [row["Title"] for row in rows],
                [
                    "A Fatal Inheritance",
                    "Carson McCullers",
                    "Challenger",
                    "Free the Land",
                    "Grief Is for People",
                    "Madness",
                    "The Barn",
                    "The Unclaimed",
                    "There’s Always This Year",
                    "The Other Olympians",
                ],
            )
            self.assertEqual(rows[0]["Source"], "BookPage Best Nonfiction")
            self.assertIn("BookPage Best Nonfiction 2024", rows[0]["Signal summary"])
            self.assertIn("https://www.bookpage.com/books/a-fatal-inheritance/", rows[0]["Links"])
            self.assertEqual(rows[-1]["Visible-risk status"], "clear visible risk")
            self.assertEqual(rows[-1]["Priority label"], "D")
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["raw_candidate_count"], 10)
            self.assertEqual(summary["excluded_candidate_count"], 0)
            self.assertEqual(summary["editor_facing_count"], 10)
            self.assertEqual(summary["skipped_source_count"], 0)
            self.assertEqual(summary["failed_source_count"], 0)
            self.assertEqual(summary["shortfall_reason"], "")

    def test_rsl_recorded_catalog_result_excludes_same_work_russian_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            recorded_path = tmp_path / "rsl-results.json"
            recorded_path.write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "query": "Already Cataloged Taylor Example",
                                "record_url": "https://search.rsl.ru/record/1",
                                "result_title": "Already Cataloged",
                                "result_author": "Taylor Example",
                                "language_clue": "Russian",
                                "publication_data": "Moscow: Example, 2024",
                                "isbn": "9785000000001",
                                "source_name": "RSL",
                                "check_date": "2026-05-21",
                                "safe_debug_status": "recorded fixture",
                                "candidate_title": "Already Cataloged",
                                "primary_authors": ["Taylor Example"],
                                "match_type": "same-work Russian-language edition",
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
                        "sources": [
                            {
                                "name": "Fixture Media List",
                                "family": "media/curated list",
                                "url": "https://example.test/media",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "fixture_candidates": [
                                    {
                                        "title": "Already Cataloged",
                                        "primary_authors": ["Taylor Example"],
                                        "source_url": "https://example.test/media/already-cataloged",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Included in a media list.",
                                        "candidate_category": "popular science",
                                    }
                                ],
                            }
                        ],
                        "russian_edition_checks": [
                            {
                                "name": "RSL",
                                "family": "russian-library-catalog",
                                "url": "https://search.rsl.ru/",
                                "recorded_results_file": str(recorded_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8") as handle:
                self.assertEqual(list(csv.DictReader(handle)), [])
            exclusions = json.loads((output_dir / "internal-exclusions.json").read_text(encoding="utf-8"))
            self.assertEqual(exclusions[0]["exclusion_reason"], "found in Russian")
            self.assertEqual(exclusions[0]["evidence"][0]["source_type"], "russian-library-catalog")
            self.assertEqual(exclusions[0]["evidence"][0]["record_url"], "https://search.rsl.ru/record/1")
            russian_checks = json.loads((output_dir / "russian-edition-checks.json").read_text(encoding="utf-8"))
            self.assertEqual(russian_checks["retailer_runs"][0]["source_type"], "russian-library-catalog")
            self.assertEqual(
                russian_checks["candidate_checks"][0]["decision_reason"],
                "same-work Russian-language bibliographic record found in a configured library catalog",
            )

    def test_rsl_ambiguous_or_foreign_records_do_not_exclude_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
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
                                        "title": "Ambiguous Catalog",
                                        "primary_authors": ["Robin Example"],
                                        "source_url": "https://example.test/awards/ambiguous",
                                        "signal_strength": "shortlist",
                                        "evidence_note": "Shortlisted for a fixture award.",
                                    },
                                    {
                                        "title": "Foreign Catalog",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/awards/foreign",
                                        "signal_strength": "shortlist",
                                        "evidence_note": "Shortlisted for a fixture award.",
                                    },
                                ],
                            }
                        ],
                        "russian_edition_checks": [
                            {
                                "name": "RSL",
                                "family": "russian-library-catalog",
                                "url": "https://search.rsl.ru/",
                                "recorded_results": [
                                    {
                                        "query": "Ambiguous Catalog Robin Example",
                                        "record_url": "https://search.rsl.ru/record/2",
                                        "result_title": "Ambiguous Catalog",
                                        "result_author": "R. Example",
                                        "language_clue": "Russian",
                                        "publication_data": "Moscow: Example, 2020",
                                        "source_name": "RSL",
                                        "check_date": "2026-05-21",
                                        "safe_debug_status": "recorded fixture",
                                        "candidate_title": "Ambiguous Catalog",
                                        "primary_authors": ["Robin Example"],
                                        "match_type": "ambiguous author/title match",
                                    },
                                    {
                                        "query": "Foreign Catalog Alex Example",
                                        "record_url": "https://search.rsl.ru/record/3",
                                        "result_title": "Foreign Catalog",
                                        "result_author": "Alex Example",
                                        "language_clue": "English",
                                        "publication_data": "New York: Example, 2024",
                                        "source_name": "RSL",
                                        "check_date": "2026-05-21",
                                        "safe_debug_status": "recorded fixture",
                                        "candidate_title": "Foreign Catalog",
                                        "primary_authors": ["Alex Example"],
                                        "match_type": "imported foreign-language listing",
                                    },
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            statuses = {candidate["title"]: candidate["russian_edition_status"] for candidate in candidates}
            self.assertEqual(statuses["Ambiguous Catalog"], "unclear")
            self.assertEqual(statuses["Foreign Catalog"], "not found in Russian")
            with (output_dir / "russian-review-queue.csv").open(encoding="utf-8") as handle:
                review_rows = list(csv.DictReader(handle))
            self.assertEqual([row["Title"] for row in review_rows], ["Ambiguous Catalog"])

    def test_rsl_live_opt_in_capture_can_be_reused_without_live_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            base_source = {
                "name": "Fixture Media List",
                "family": "media/curated list",
                "url": "https://example.test/media",
                "access_status": "open",
                "extraction_method": "fixture",
                "check_frequency": "weekly",
                "language": "en",
                "fixture_candidates": [
                    {
                        "title": "Captured Catalog",
                        "primary_authors": ["Dana Example"],
                        "source_url": "https://example.test/media/captured",
                        "signal_strength": "major media list",
                        "evidence_note": "Included in a media list.",
                    }
                ],
            }
            live_config = tmp_path / "live.json"
            live_config.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-21",
                        "sources": [base_source],
                        "russian_edition_checks": [
                            {
                                "name": "RSL",
                                "family": "russian-library-catalog",
                                "url": "https://search.rsl.ru/",
                                "enable_live": True,
                                "capture_recorded_results_file": "captured-rsl.json",
                                "fixture_live_results": [
                                    {
                                        "query": "Captured Catalog Dana Example",
                                        "record_url": "https://search.rsl.ru/record/4",
                                        "result_title": "Captured Catalog",
                                        "result_author": "Dana Example",
                                        "language_clue": "Russian",
                                        "publication_data": "Moscow: Example, 2023",
                                        "source_name": "RSL",
                                        "check_date": "2026-05-21",
                                        "safe_debug_status": "captured from live fixture",
                                        "candidate_title": "Captured Catalog",
                                        "primary_authors": ["Dana Example"],
                                        "match_type": "same-work Russian-language edition",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, live_config)

            self.assertEqual(result.returncode, 0, result.stderr)
            captured = output_dir / "captured-rsl.json"
            self.assertTrue(captured.exists())
            captured_data = json.loads(captured.read_text(encoding="utf-8"))
            self.assertNotIn("page_body", captured_data["results"][0])
            reuse_config = tmp_path / "reuse.json"
            reuse_config.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-21",
                        "sources": [base_source],
                        "russian_edition_checks": [
                            {
                                "name": "RSL",
                                "family": "russian-library-catalog",
                                "url": "https://search.rsl.ru/",
                                "recorded_results_file": str(captured),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            reuse_result, reuse_output = run_pipeline(tmp_path, reuse_config)
            self.assertEqual(reuse_result.returncode, 0, reuse_result.stderr)
            reuse_exclusions = json.loads((reuse_output / "internal-exclusions.json").read_text(encoding="utf-8"))
            self.assertEqual(reuse_exclusions[0]["title"], "Captured Catalog")

    def test_rsl_live_opt_in_blocked_response_is_reported_as_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-21",
                        "sources": [
                            {
                                "name": "Fixture Media List",
                                "family": "media/curated list",
                                "url": "https://example.test/media",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "fixture_candidates": [
                                    {
                                        "title": "Live Failure Candidate",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/media/live-failure",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Included in a media list.",
                                    }
                                ],
                            }
                        ],
                        "russian_edition_checks": [
                            {
                                "name": "RSL",
                                "family": "russian-library-catalog",
                                "url": "https://search.rsl.ru/",
                                "enable_live": True,
                                "fixture_live_results": [
                                    {
                                        "query": "Live Failure Candidate Alex Example",
                                        "status": "captcha",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            russian_checks = json.loads((output_dir / "russian-edition-checks.json").read_text(encoding="utf-8"))
            self.assertEqual(russian_checks["retailer_runs"][0]["status"], "blocked")
            self.assertIn("CAPTCHA-like response", russian_checks["retailer_runs"][0]["reason"])
            candidate_checks = russian_checks["candidate_checks"]
            self.assertEqual(len(candidate_checks), 1)
            self.assertEqual(candidate_checks[0]["resolved_status"], "unclear")
            self.assertEqual(candidate_checks[0]["russian_edition_verification"]["provider_outcome"], "blocked")
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["russian_check_blocked_count"], 1)
            self.assertNotIn("1 Russian retailer check failed", summary["major_warnings"])

    def test_rsl_live_html_response_normalizes_metadata_only_results(self):
        candidate = {
            "title": "HTML Catalog",
            "primary_authors": ["Alex Example"],
        }
        body = """
        <html>
          <article>
            <a href="/record/html-catalog"><h3>HTML Catalog</h3></a>
            <p>Author: Alex Example | Language: Russian | Publication: Moscow: Example, 2024 | ISBN: 9785000000002</p>
          </article>
        </html>
        """

        results, error = autoscout_main.parse_rsl_live_response(
            body,
            "HTML Catalog Alex Example",
            candidate,
            {"name": "RSL", "url": "https://search.rsl.ru/"},
            "2026-05-21",
        )

        self.assertEqual(error, "")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["record_url"], "https://search.rsl.ru/record/html-catalog")
        self.assertEqual(results[0]["result_title"], "HTML Catalog")
        self.assertEqual(results[0]["result_author"], "Alex Example")
        self.assertEqual(results[0]["language_clue"], "Russian")
        self.assertEqual(results[0]["match_type"], "same-work Russian-language edition")
        self.assertNotIn("page_body", results[0])

    def test_rsl_live_json_response_normalizes_metadata_only_results(self):
        candidate = {
            "title": "JSON Catalog",
            "primary_authors": ["Dana Example"],
        }
        body = json.dumps(
            {
                "results": [
                    {
                        "url": "https://search.rsl.ru/record/json-catalog",
                        "title": "JSON Catalog",
                        "author": "Dana Example",
                        "language": "Russian",
                        "publication": "Moscow: Example, 2025",
                    }
                ]
            }
        )

        results, error = autoscout_main.parse_rsl_live_response(
            body,
            "JSON Catalog Dana Example",
            candidate,
            {"name": "RSL", "url": "https://search.rsl.ru/"},
            "2026-05-21",
        )

        self.assertEqual(error, "")
        self.assertEqual(results[0]["edition_url"], "https://search.rsl.ru/record/json-catalog")
        self.assertEqual(results[0]["source_type"], "russian-library-catalog")
        self.assertEqual(results[0]["safe_debug_status"], "live metadata parse")

    def test_rsl_live_changed_markup_reports_parse_failure(self):
        results, error = autoscout_main.parse_rsl_live_response(
            "<html><main><p>Unexpected unrelated page</p></main></html>",
            "Unexpected",
            {"title": "Unexpected", "primary_authors": ["Alex Example"]},
            {"name": "RSL", "url": "https://search.rsl.ru/"},
            "2026-05-21",
        )

        self.assertEqual(results, [])
        self.assertIn("markup could not be parsed safely", error)

    def test_rsl_live_incomplete_result_metadata_reports_safe_failure(self):
        results, error = autoscout_main.parse_rsl_live_response(
            '<html><article><a href="/record/incomplete"></a></article></html>',
            "Incomplete",
            {"title": "Incomplete", "primary_authors": ["Alex Example"]},
            {"name": "RSL", "url": "https://search.rsl.ru/"},
            "2026-05-21",
        )

        self.assertEqual(results, [])
        self.assertEqual(error, "")

    def test_rsl_live_search_container_with_incomplete_metadata_is_no_match_not_failure(self):
        results, error = autoscout_main.parse_rsl_live_response(
            """
            <html>
              <div class="row search-container" data-id="01015307921">
                <span class="js-item-maininfo" data-id="01015307921">1913, т. 4, № 2 // Известия Саратовского университета.</span>
                <a class="rsl-modal" href="/ru/record/01015307921">Описание</a>
              </div>
            </html>
            """,
            "Already Cataloged",
            {"title": "Already Cataloged", "primary_authors": ["Taylor Example"]},
            {"name": "RSL", "url": "https://search.rsl.ru/"},
            "2026-05-21",
        )

        self.assertEqual(results, [])
        self.assertEqual(error, "")

    def test_rsl_ajax_content_matches_cyrillic_title_and_reversed_author_order(self):
        body = json.dumps(
            {
                "content": """
                <div class="row search-container pt10" data-id="01009658033">
                  <b class="js-item-authorinfo" data-id="01009658033">Ньюпорт, Кэл.</b>
                  <span class="js-item-maininfo" data-id="01009658033">
                    Хватит мечтать, займись делом! [Текст] : почему важнее хорошо работать, чем искать хорошую работу : перевод с английского : [0+] / Кэл Ньюпорт. - Москва : Альпина паблишер, 2018. - 279, [2] с.; 17 см.; ISBN 978-5-9614-7002-4 : 3000 экз.
                  </span>
                  <a class="rsl-modal" href="/ru/record/01009658033">Описание</a>
                </div>
                """
            }
        )

        results, error = autoscout_main.parse_rsl_live_response(
            body,
            "Хватит мечтать, займись делом!",
            {"title": "Хватит мечтать, займись делом!", "primary_authors": ["Кэл Ньюпорт"]},
            {"name": "RSL", "url": "https://search.rsl.ru/"},
            "2026-05-29",
        )

        self.assertEqual(error, "")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["match_type"], "same-work Russian-language edition")
        self.assertEqual(results[0]["record_url"], "https://search.rsl.ru/ru/record/01009658033")
        self.assertLessEqual(len(results[0]["publication_data"]), 240)

    def test_rsl_source_family_cannot_create_scouting_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-21",
                        "sources": [
                            {
                                "name": "RSL",
                                "family": "russian-library-catalog",
                                "url": "https://search.rsl.ru/",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "fixture_candidates": [
                                    {
                                        "title": "Catalog Only",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://search.rsl.ru/record/5",
                                        "signal_strength": "minor mention",
                                        "evidence_note": "Catalog record only.",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8")), [])
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["ignored_source_count"], 1)

    def test_cyrillic_candidate_identity_is_preserved_for_russian_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-29",
                        "sources": [
                            {
                                "name": "Fixture Media List",
                                "family": "media/curated list",
                                "url": "https://example.test/media",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "ru",
                                "fixture_candidates": [
                                    {
                                        "title": "Хватит мечтать, займись делом!",
                                        "primary_authors": ["Кэл Ньюпорт"],
                                        "english_title": "Stop Dreaming, Get to Work!",
                                        "search_translation": "Хватит мечтать займись делом поисковая подсказка",
                                        "author_transliteration_note": "Кэл Ньюпорт",
                                        "source_url": "https://example.test/media/newport",
                                        "signal_strength": "minor mention",
                                        "evidence_note": "Included in a fixture media list.",
                                    }
                                ],
                            }
                        ],
                        "russian_edition_checks": [
                            {
                                "name": "RSL",
                                "family": "russian-library-catalog",
                                "url": "https://search.rsl.ru/",
                                "recorded_results": [],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            russian_checks = json.loads((output_dir / "russian-edition-checks.json").read_text(encoding="utf-8"))
            self.assertEqual(
                russian_checks["candidate_checks"][0]["identity_key"],
                "хватит мечтать займись делом|кэл ньюпорт",
            )

    def test_recorded_litres_results_are_normalized_as_safe_retailer_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            recorded_path = tmp_path / "litres-results.json"
            recorded_path.write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "query": "Recorded LitRes Book Dana Author",
                                "result_url": "https://www.litres.ru/book/recorded-litres-book-ru",
                                "result_title": "Recorded LitRes Book",
                                "result_author": "Dana Author",
                                "language_clue": "Russian",
                                "format_clue": "ebook",
                                "isbn": "9785000000001",
                                "candidate_title": "Recorded LitRes Book",
                                "primary_authors": ["Dana Author"],
                                "match_type": "same-work Russian-language edition",
                                "evidence_note": "Recorded LitRes metadata confirms same-work Russian-language edition.",
                                "safe_debug_status": "recorded LitRes metadata",
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
                        "run_date": "2026-05-29",
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
                                        "title": "Recorded LitRes Book",
                                        "primary_authors": ["Dana Author"],
                                        "source_url": "https://example.test/awards/recorded-litres-book",
                                        "signal_strength": "shortlist",
                                        "evidence_note": "Shortlisted for an award.",
                                        "candidate_category": "popular science",
                                    }
                                ],
                            }
                        ],
                        "russian_edition_checks": [
                            {
                                "retailer": "LitRes",
                                "url": "https://www.litres.ru",
                                "recorded_results_file": str(recorded_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                self.assertEqual(list(csv.DictReader(handle)), [])
            exclusions = json.loads((output_dir / "internal-exclusions.json").read_text(encoding="utf-8"))
            self.assertEqual(exclusions[0]["exclusion_reason"], "found in Russian")
            self.assertEqual(exclusions[0]["evidence"][0]["retailer"], "LitRes")
            self.assertEqual(exclusions[0]["evidence"][0]["source_type"], "russian-retailer")
            self.assertEqual(
                exclusions[0]["evidence"][0]["record_url"], "https://www.litres.ru/book/recorded-litres-book-ru"
            )
            self.assertEqual(exclusions[0]["evidence"][0]["query"], "Recorded LitRes Book Dana Author")
            self.assertEqual(exclusions[0]["evidence"][0]["isbn"], "9785000000001")

            russian_checks = json.loads((output_dir / "russian-edition-checks.json").read_text(encoding="utf-8"))
            self.assertEqual(russian_checks["retailer_runs"][0]["retailer"], "LitRes")
            self.assertEqual(russian_checks["retailer_runs"][0]["fixture_result_count"], 1)
            self.assertEqual(russian_checks["candidate_checks"][0]["resolved_status"], "found in Russian")

    def test_litres_live_json_fixture_excludes_same_work_russian_edition(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-29",
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
                                        "title": "Live LitRes Book",
                                        "primary_authors": ["Noa Author"],
                                        "source_url": "https://example.test/awards/live-litres",
                                        "signal_strength": "shortlist",
                                        "evidence_note": "Shortlisted for an award.",
                                        "candidate_category": "popular science",
                                    }
                                ],
                            }
                        ],
                        "russian_edition_checks": [
                            {
                                "retailer": "LitRes",
                                "url": "https://www.litres.ru",
                                "enable_live": True,
                                "fixture_live_results": [
                                    {
                                        "body": json.dumps(
                                            {
                                                "results": [
                                                    {
                                                        "url": "/book/noa-author/live-litres-book/",
                                                        "title": "Live LitRes Book",
                                                        "author": "Noa Author",
                                                        "language": "Russian",
                                                        "format": "ebook",
                                                    }
                                                ]
                                            }
                                        )
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                self.assertEqual(list(csv.DictReader(handle)), [])
            exclusions = json.loads((output_dir / "internal-exclusions.json").read_text(encoding="utf-8"))
            self.assertEqual(exclusions[0]["exclusion_reason"], "found in Russian")
            self.assertEqual(
                exclusions[0]["evidence"][0]["record_url"], "https://www.litres.ru/book/noa-author/live-litres-book/"
            )

            russian_checks = json.loads((output_dir / "russian-edition-checks.json").read_text(encoding="utf-8"))
            self.assertEqual(russian_checks["retailer_runs"][0]["status"], "checked")
            self.assertEqual(russian_checks["retailer_runs"][0]["fixture_result_count"], 1)
            self.assertEqual(russian_checks["candidate_checks"][0]["resolved_status"], "found in Russian")

    def test_litres_live_blocked_response_is_reported_as_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-29",
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
                                        "title": "Blocked Live Book",
                                        "primary_authors": ["Noa Author"],
                                        "source_url": "https://example.test/awards/blocked-live",
                                        "signal_strength": "shortlist",
                                        "evidence_note": "Shortlisted for an award.",
                                        "candidate_category": "popular science",
                                    }
                                ],
                            }
                        ],
                        "russian_edition_checks": [
                            {
                                "retailer": "LitRes",
                                "url": "https://www.litres.ru",
                                "enable_live": True,
                                "fixture_live_results": [{"status": "captcha"}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            russian_checks = json.loads((output_dir / "russian-edition-checks.json").read_text(encoding="utf-8"))
            self.assertEqual(russian_checks["retailer_runs"][0]["status"], "blocked")
            self.assertIn("CAPTCHA", russian_checks["retailer_runs"][0]["reason"])
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["russian_check_blocked_count"], 1)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["Title"] for row in rows], ["Blocked Live Book"])

    def test_litres_live_html_response_normalizes_metadata_only_results(self):
        html = """
        <html>
          <body>
            <article class="art-item">
              <a href="/book/noa-author/html-litres-book/">HTML LitRes Book</a>
              <span>Автор: Noa Author</span>
              <span>Язык: Russian</span>
              <span>Формат: ebook</span>
            </article>
          </body>
        </html>
        """

        results, error = autoscout_main.parse_litres_live_response(
            html,
            "HTML LitRes Book Noa Author",
            {"title": "HTML LitRes Book", "primary_authors": ["Noa Author"]},
            {"retailer": "LitRes", "url": "https://www.litres.ru"},
            "2026-05-29",
        )

        self.assertEqual(error, "")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["match_type"], "same-work Russian-language edition")
        self.assertEqual(results[0]["edition_url"], "https://www.litres.ru/book/noa-author/html-litres-book/")
        self.assertEqual(results[0]["result_title"], "HTML LitRes Book")
        self.assertEqual(results[0]["result_author"], "Noa Author")

    def test_recorded_book24_results_are_normalized_as_safe_retailer_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            recorded_path = tmp_path / "book24-results.json"
            recorded_path.write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "query": "Recorded Book24 Book Dana Author",
                                "result_url": "https://book24.ru/product/recorded-book24-book-ru/",
                                "result_title": "Recorded Book24 Book",
                                "result_author": "Dana Author",
                                "language_clue": "Russian",
                                "format_clue": "paperback",
                                "isbn": "9785000000002",
                                "candidate_title": "Recorded Book24 Book",
                                "primary_authors": ["Dana Author"],
                                "match_type": "same-work Russian-language edition",
                                "evidence_note": "Recorded Book24 metadata confirms same-work Russian-language edition.",
                                "safe_debug_status": "recorded Book24 metadata",
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
                        "run_date": "2026-05-29",
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
                                        "title": "Recorded Book24 Book",
                                        "primary_authors": ["Dana Author"],
                                        "source_url": "https://example.test/awards/recorded-book24-book",
                                        "signal_strength": "shortlist",
                                        "evidence_note": "Shortlisted for an award.",
                                        "candidate_category": "popular science",
                                    }
                                ],
                            }
                        ],
                        "russian_edition_checks": [
                            {
                                "retailer": "Book24",
                                "url": "https://book24.ru",
                                "recorded_results_file": str(recorded_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                self.assertEqual(list(csv.DictReader(handle)), [])
            exclusions = json.loads((output_dir / "internal-exclusions.json").read_text(encoding="utf-8"))
            self.assertEqual(exclusions[0]["exclusion_reason"], "found in Russian")
            self.assertEqual(exclusions[0]["evidence"][0]["retailer"], "Book24")
            self.assertEqual(
                exclusions[0]["evidence"][0]["record_url"], "https://book24.ru/product/recorded-book24-book-ru/"
            )
            self.assertEqual(exclusions[0]["evidence"][0]["isbn"], "9785000000002")

    def test_book24_live_json_fixture_excludes_same_work_russian_edition(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-29",
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
                                        "title": "Live Book24 Book",
                                        "primary_authors": ["Noa Author"],
                                        "source_url": "https://example.test/awards/live-book24",
                                        "signal_strength": "shortlist",
                                        "evidence_note": "Shortlisted for an award.",
                                        "candidate_category": "popular science",
                                    }
                                ],
                            }
                        ],
                        "russian_edition_checks": [
                            {
                                "retailer": "Book24",
                                "url": "https://book24.ru",
                                "enable_live": True,
                                "fixture_live_results": [
                                    {
                                        "body": json.dumps(
                                            {
                                                "products": [
                                                    {
                                                        "url": "/product/live-book24-book-ru/",
                                                        "title": "Live Book24 Book",
                                                        "author": "Noa Author",
                                                        "language": "Russian",
                                                        "format": "paperback",
                                                    }
                                                ]
                                            }
                                        )
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                self.assertEqual(list(csv.DictReader(handle)), [])
            exclusions = json.loads((output_dir / "internal-exclusions.json").read_text(encoding="utf-8"))
            self.assertEqual(exclusions[0]["exclusion_reason"], "found in Russian")
            self.assertEqual(
                exclusions[0]["evidence"][0]["record_url"], "https://book24.ru/product/live-book24-book-ru/"
            )

    def test_book24_live_html_response_normalizes_metadata_only_results(self):
        html = """
        <html>
          <body>
            <article class="product-card">
              <a href="/product/html-book24-book-ru/">HTML Book24 Book</a>
              <span>Автор: Noa Author</span>
              <span>Язык: Russian</span>
              <span>Формат: paperback</span>
            </article>
          </body>
        </html>
        """

        results, error = autoscout_main.parse_book24_live_response(
            html,
            "HTML Book24 Book Noa Author",
            {"title": "HTML Book24 Book", "primary_authors": ["Noa Author"]},
            {"retailer": "Book24", "url": "https://book24.ru"},
            "2026-05-29",
        )

        self.assertEqual(error, "")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["match_type"], "same-work Russian-language edition")
        self.assertEqual(results[0]["edition_url"], "https://book24.ru/product/html-book24-book-ru/")
        self.assertEqual(results[0]["result_title"], "HTML Book24 Book")
        self.assertEqual(results[0]["result_author"], "Noa Author")

    def test_book24_live_blocked_response_is_reported_as_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-05-29",
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
                                        "title": "Blocked Book24 Book",
                                        "primary_authors": ["Noa Author"],
                                        "source_url": "https://example.test/awards/blocked-book24",
                                        "signal_strength": "shortlist",
                                        "evidence_note": "Shortlisted for an award.",
                                        "candidate_category": "popular science",
                                    }
                                ],
                            }
                        ],
                        "russian_edition_checks": [
                            {
                                "retailer": "Book24",
                                "url": "https://book24.ru",
                                "enable_live": True,
                                "fixture_live_results": [{"status": "captcha"}],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            russian_checks = json.loads((output_dir / "russian-edition-checks.json").read_text(encoding="utf-8"))
            self.assertEqual(russian_checks["retailer_runs"][0]["status"], "blocked")
            self.assertIn("CAPTCHA", russian_checks["retailer_runs"][0]["reason"])
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["russian_check_blocked_count"], 1)


class RecurringReadinessTests(unittest.TestCase):
    def _write_config(self, tmp_path, readiness_purpose="recurring-weekly", **extra):
        config_path = tmp_path / "config.json"
        config = {
            "run_date": "2026-07-01",
            "readiness_purpose": readiness_purpose,
            "pilot_mode": True,
            "editor_row_target": 100,
            "pilot_row_min": 100,
            "pilot_row_max": 100,
            "sources": [
                {
                    "name": "Fixture Source",
                    "family": "award",
                    "url": "https://example.test/awards",
                    "access_status": "open",
                    "extraction_method": "fixture",
                    "check_frequency": "weekly",
                    "language": "en",
                    "notes": "Deterministic fixture.",
                    "fixture_candidates": [
                        {
                            "title": f"Book {i}",
                            "primary_authors": [f"Author {i}"],
                            "source_url": f"https://example.test/book-{i}",
                            "signal_strength": "shortlist",
                            "evidence_note": f"Shortlisted book {i}.",
                            "candidate_category": "popular science",
                        }
                        for i in range(1, 6)
                    ],
                }
            ],
            **extra,
        }
        config_path.write_text(json.dumps(config), encoding="utf-8")
        return config_path

    def test_recurring_below_100_rows_ready_when_sources_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_config(tmp_path)
            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            pilot_status = json.loads((output_dir / "pilot-status.json").read_text(encoding="utf-8"))
            self.assertTrue(pilot_status.get("ready_for_editor"))
            self.assertEqual(pilot_status["readiness_basis"]["readiness_purpose"], "recurring-weekly")
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["readiness_purpose"], "recurring-weekly")
            self.assertLess(summary["editor_facing_count"], 100)

    def test_recurring_zero_new_and_changed_ready_with_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / "state"
            state_dir.mkdir()
            history = [
                {
                    "identity_key": f"book {i}|author {i}",
                    "title": f"Book {i}",
                    "primary_authors": [f"Author {i}"],
                    "last_seen_date": "2026-06-24",
                    "first_seen_date": "2026-06-24",
                    "primary_signal_strength": "shortlist",
                    "independent_signal_families": ["award"],
                }
                for i in range(1, 6)
            ]
            seed_state(state_dir, candidate_history=history)
            config_path = self._write_config(tmp_path, candidate_history=history)
            output_dir = tmp_path / "run"
            _cli_run_mvp(config_path, output_dir, state_dir)

            pilot_status = json.loads((output_dir / "pilot-status.json").read_text(encoding="utf-8"))
            self.assertTrue(pilot_status.get("ready_for_editor"))
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            deltas = summary["update_delta_counts"]
            self.assertGreaterEqual(deltas.get("unchanged", 0) + deltas.get("new", 0), 1)

    def test_calibration_below_target_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_config(tmp_path, readiness_purpose="seed-calibration")
            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            pilot_status = json.loads((output_dir / "pilot-status.json").read_text(encoding="utf-8"))
            self.assertFalse(pilot_status.get("ready_for_editor"))
            self.assertEqual(pilot_status["readiness_basis"]["readiness_purpose"], "seed-calibration")

    def test_legacy_no_purpose_defaults_to_calibration(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-07-01",
                        "pilot_mode": True,
                        "editor_row_target": 100,
                        "pilot_row_min": 100,
                        "pilot_row_max": 100,
                        "sources": [
                            {
                                "name": "Fixture Source",
                                "family": "award",
                                "url": "https://example.test/awards",
                                "access_status": "open",
                                "extraction_method": "fixture",
                                "check_frequency": "weekly",
                                "language": "en",
                                "fixture_candidates": [
                                    {
                                        "title": "One Book",
                                        "primary_authors": ["Author One"],
                                        "source_url": "https://example.test/book-1",
                                        "signal_strength": "shortlist",
                                        "evidence_note": "Shortlisted.",
                                        "candidate_category": "popular science",
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)
            pilot_status = json.loads((output_dir / "pilot-status.json").read_text(encoding="utf-8"))
            self.assertEqual(pilot_status["readiness_basis"]["readiness_purpose"], "seed-calibration")
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["readiness_purpose"], "seed-calibration")

    def test_recurring_run_health_emits_structured_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_config(tmp_path)
            result, output_dir = run_pipeline(tmp_path, config_path)

            self.assertEqual(result.returncode, 0, result.stderr)

            import subprocess
            import sys

            proc = subprocess.run(
                [sys.executable, "-m", "autoscout", "run-health", "--output-dir", str(output_dir)],
                check=False,
                capture_output=True,
                text=True,
            )
            stdout = proc.stdout
            self.assertEqual(proc.returncode, 0, f"run-health failed: {proc.stderr}")
            self.assertIn("readiness_purpose=recurring-weekly", stdout)
            self.assertIn("recurring_health_classification=healthy", stdout)
            self.assertIn("recurring_attention_required=false", stdout)
            self.assertIn("recurring_new_count=", stdout)
            self.assertIn("recurring_updated_count=", stdout)
            self.assertIn("recurring_previously_seen_count=", stdout)

    def test_recurring_deterministic_identical_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = self._write_config(tmp_path)

            output_a = tmp_path / "run-a"
            output_b = tmp_path / "run-b"
            result_a, _ = run_pipeline(tmp_path, config_path, output_dir=output_a)
            result_b, _ = run_pipeline(tmp_path, config_path, output_dir=output_b)

            self.assertEqual(result_a.returncode, 0)
            self.assertEqual(result_b.returncode, 0)

            for key in ["run-summary.json", "pilot-status.json"]:
                self.assertEqual(
                    (output_a / key).read_bytes(),
                    (output_b / key).read_bytes(),
                    f"{key} not byte-identical across runs",
                )
