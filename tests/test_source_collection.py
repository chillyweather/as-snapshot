import csv
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from pathlib import Path

import autoscout.source_collectors as source_collectors
from autoscout.cli.weekly import run_mvp as _cli_run_mvp
from autoscout.codex_workflow import prepare_codex_extraction
from autoscout.config import resolve_weekly_portfolio
from autoscout.pipeline import signal_strength_rank
from autoscout.run_health import run_health as _cli_run_health
from autoscout.source_collection import SourceCollection
from autoscout.source_collection import codex_capture_filename
from autoscout.source_collection import prepare_codex_extraction_worklist
from autoscout.source_collectors import CollectorRunError
from autoscout.source_collectors import codex_extraction_collector_result
from tests.fixtures import copy_fixture

REPO_ROOT = Path(__file__).resolve().parents[1]


class _IPCResult:
    __slots__ = ("returncode", "stderr", "stdout")

    def __init__(self, returncode: int, stderr: str = "", stdout: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


def _run_mvp(config_path: Path, output_dir: Path) -> _IPCResult:
    """Run MVP pipeline in-process, returning a result-like object."""
    captured = io.StringIO()
    try:
        with redirect_stderr(captured):
            _cli_run_mvp(config_path, output_dir, state_dir=output_dir.parent / f".{output_dir.name}-state")
        return _IPCResult(0, captured.getvalue())
    except SystemExit as e:
        return _IPCResult(e.code if e.code is not None else 1, captured.getvalue())
    except ValueError as e:
        return _IPCResult(1, captured.getvalue() + str(e))
    except Exception as e:
        return _IPCResult(1, captured.getvalue() + "\n" + str(e))


def _run_health(output_dir: Path) -> _IPCResult:
    """Run health check in-process, returning a result-like object with captured stdout."""
    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            exit_code = _cli_run_health(output_dir)
        return _IPCResult(exit_code, stdout=captured.getvalue())
    except Exception as e:
        return _IPCResult(1, stdout=captured.getvalue(), stderr=str(e))


def _load_config(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return resolve_weekly_portfolio(raw, path)


class RegisteredExtractionMethodsTests(unittest.TestCase):
    def test_registered_extraction_methods_is_sorted_and_matches_registry_keys(self):
        methods = source_collectors.registered_extraction_methods()
        self.assertEqual(methods, sorted(source_collectors.COLLECTOR_REGISTRY))
        self.assertIn("bookpage-recorded-response", methods)
        self.assertIn("codex-extraction-replay", methods)


class SourceCollectionTests(unittest.TestCase):
    def test_collects_fixture_recorded_and_collector_mentions_through_one_interface(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            recorded_path = tmp_path / "recorded.json"
            recorded_path.write_text(
                json.dumps(
                    [
                        {
                            "title": "Recorded Book",
                            "primary_authors": ["Riley Example"],
                            "source_url": "https://example.test/recorded",
                            "source_check_date": "2026-06-10",
                            "signal_type": "media/curated list",
                            "signal_strength": "major media list",
                            "evidence_note": "Recorded from a configured source page.",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")
            sources = [
                {
                    "name": "Fixture Source",
                    "family": "award",
                    "url": "https://example.test/fixture",
                    "access_status": "open",
                    "language": "en",
                    "extraction_method": "fixture",
                    "fixture_candidates": [
                        {
                            "title": "Fixture Book",
                            "primary_authors": ["Alex Example"],
                            "source_url": "https://example.test/fixture/book",
                            "signal_strength": "winner",
                            "evidence_note": "Won a configured award.",
                        }
                    ],
                },
                {
                    "name": "Collector Source",
                    "family": "media/curated list",
                    "url": "https://example.test/collector",
                    "access_status": "open",
                    "language": "en",
                    "extraction_method": "collector-fixture",
                    "collector_mentions": [
                        {
                            "title": "Collector Book",
                            "primary_authors": ["Casey Example"],
                            "source_url": "https://example.test/collector/book",
                            "signal_type": "media/curated list",
                            "signal_strength": "major media list",
                            "evidence_note": "Collected through a configured collector.",
                        }
                    ],
                },
                {
                    "name": "Recorded Source",
                    "family": "media/curated list",
                    "url": "https://example.test/recorded",
                    "access_status": "open",
                    "language": "en",
                    "extraction_method": "recorded",
                    "recorded_mentions_file": str(recorded_path),
                },
            ]

            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(sources)

            self.assertEqual(
                {mention["title"] for mention in result.source_mentions},
                {"Fixture Book", "Collector Book", "Recorded Book"},
            )
            self.assertEqual(len(result.source_runs), 3)
            self.assertTrue(all(run["status"] == "collected" for run in result.source_runs))

    def test_casing_variant_russian_status_is_counted_as_excluded_during_collection(self):
        """Issue #238 regression: a mention whose russian_edition_status differs only
        in casing ("Found in Russian" rather than the canonical "found in Russian")
        must be counted as excluded during source-level collection, exactly like the
        canonically-cased status is. Before the SSOT migration, collect() compared
        exact-case and undercounted this mention as valid rather than excluded."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")
            sources = [
                {
                    "name": "Casing Variant Source",
                    "family": "award",
                    "url": "https://example.test/casing-variant",
                    "access_status": "open",
                    "language": "en",
                    "extraction_method": "fixture",
                    "fixture_candidates": [
                        {
                            "title": "Casing Variant Book",
                            "primary_authors": ["Alex Example"],
                            "source_url": "https://example.test/casing-variant/book",
                            "signal_strength": "winner",
                            "evidence_note": "Won a configured award.",
                            # Casing variant of the canonical "found in Russian".
                            "russian_edition_status": "Found in Russian",
                        }
                    ],
                }
            ]

            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(sources)

            self.assertEqual({mention["title"] for mention in result.source_mentions}, {"Casing Variant Book"})
            self.assertEqual(result.source_level_excluded_count, 1)
            run = result.source_runs[0]
            self.assertEqual(run["excluded_candidate_count"], 1)
            self.assertEqual(run["editor_facing_count"], 0)

    def test_unrecognized_extraction_method_with_no_inputs_fails_source_run(self):
        """Issue #237: a source whose extraction_method matches no registered
        collector and has no fixture/collector-mention/recorded inputs must
        surface as a failed source-run record naming the unrecognized value,
        not silently collect zero mentions."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = {
                "name": "Typo'd Source",
                "family": "award",
                "url": "https://example.test/typo",
                "access_status": "open",
                "language": "en",
                "extraction_method": "npr-sorce-page",
            }

            result = SourceCollection(tmp_path / "sources.json", tmp_path / "runs", "2026-06-10").collect([source])

            self.assertEqual(result.source_mentions, [])
            self.assertEqual(result.failed_source_count, 1)
            self.assertEqual(len(result.source_runs), 1)
            run = result.source_runs[0]
            self.assertEqual(run["status"], "failed")
            self.assertIn("npr-sorce-page", run["reason"])
            for method in source_collectors.registered_extraction_methods():
                self.assertIn(method, run["reason"])

    def test_source_run_completes_and_counts_unrecognized_method_alongside_others(self):
        """The run itself still completes (graceful degradation, ADR 0007):
        an unrecognized-method source is counted as failed while a sibling
        source with fixture inputs still collects successfully."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")
            sources = [
                {
                    "name": "Typo'd Source",
                    "family": "award",
                    "access_status": "open",
                    "language": "en",
                    "extraction_method": "npr-sorce-page",
                },
                {
                    "name": "Fixture-Only Source",
                    "family": "award",
                    "access_status": "open",
                    "language": "en",
                    "fixture_candidates": [
                        {
                            "title": "Fixture-Only Book",
                            "primary_authors": ["Alex Example"],
                            "source_url": "https://example.test/fixture-only",
                            "signal_strength": "winner",
                            "evidence_note": "Won a configured award.",
                        }
                    ],
                },
            ]

            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(sources)

            self.assertEqual(result.failed_source_count, 1)
            self.assertEqual(len(result.source_runs), 2)
            self.assertEqual(result.source_runs[0]["status"], "failed")
            self.assertEqual(result.source_runs[1]["status"], "collected")
            self.assertEqual(
                {mention["title"] for mention in result.source_mentions},
                {"Fixture-Only Book"},
            )

    def test_fixture_only_source_with_no_registered_method_still_collects(self):
        """A source with fixture candidates and no registered method (or no
        method at all) must remain a successful collection exactly as before
        -- fixture/collector-mention/recorded inputs are legitimate
        collection on their own."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")
            source = {
                "name": "Deterministic Trial Source",
                "family": "award",
                "access_status": "open",
                "language": "en",
                "fixture_candidates": [
                    {
                        "title": "Trial Book",
                        "primary_authors": ["Riley Example"],
                        "source_url": "https://example.test/trial",
                        "signal_strength": "winner",
                        "evidence_note": "Deterministic trial run input.",
                    }
                ],
            }

            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect([source])

            self.assertEqual(result.failed_source_count, 0)
            self.assertEqual(result.source_runs[0]["status"], "collected")
            self.assertEqual([m["title"] for m in result.source_mentions], ["Trial Book"])

    def test_missing_recorded_mentions_file_reports_missing_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            missing_path = tmp_path / "missing-recorded.json"
            source = {
                "name": "Recorded Source",
                "family": "media/curated list",
                "url": "https://example.test/source",
                "access_status": "open",
                "language": "en",
                "recorded_mentions_file": str(missing_path),
            }

            result = SourceCollection(config_path, tmp_path / "out", "2026-01-01").collect([source])

            self.assertEqual(result.failed_source_count, 1)
            self.assertEqual(result.source_runs[0]["status"], "failed")
            self.assertIn(str(missing_path), result.source_runs[0]["reason"])
            self.assertNotIn("recorded source mentions must be a list", result.source_runs[0]["reason"])

    def test_skipped_inactive_and_non_discovery_sources_do_not_create_mentions(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result = SourceCollection(tmp_path / "sources.json", tmp_path / "runs", "2026-06-10").collect(
                [
                    {
                        "name": "Paid Source",
                        "family": "award",
                        "access_status": "paid",
                        "language": "en",
                        "fixture_candidates": [{"title": "Hidden"}],
                    },
                    {
                        "name": "Inactive Source",
                        "family": "award",
                        "active": False,
                        "access_status": "open",
                        "language": "en",
                        "fixture_candidates": [{"title": "Hidden"}],
                    },
                    {
                        "name": "Topic Search",
                        "family": "topic search",
                        "access_status": "open",
                        "language": "en",
                        "fixture_candidates": [{"title": "Hidden"}],
                    },
                ]
            )

            self.assertEqual(result.source_mentions, [])
            self.assertEqual(result.skipped_source_count, 1)
            self.assertEqual(result.ignored_source_count, 1)
            self.assertEqual([run["status"] for run in result.source_runs], ["skipped", "inactive", "ignored"])

    def test_agent_extraction_replay_collects_validated_mentions_with_health_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            page_path = tmp_path / "page.json"
            page_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": '<a href="/books/useful-book">Useful Book</a> by Alex Example',
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
                                "title": "Useful Book",
                                "primary_authors": ["Alex Example"],
                                "source_url": "https://example.test/books/useful-book",
                                "signal_strength": "major media list",
                                "evidence_note": "Listed on a configured source page.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(
                [
                    {
                        "name": "Agent Source",
                        "family": "media/curated list",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "agent-extraction-replay",
                        "source_page_fixture_file": str(page_path),
                        "extraction_response_file": str(extraction_path),
                    }
                ]
            )

            self.assertEqual([mention["title"] for mention in result.source_mentions], ["Useful Book"])
            run = result.source_runs[0]
            self.assertEqual(run["status"], "collected")
            self.assertEqual(run["raw_mention_count"], 1)
            self.assertEqual(run["valid_mention_count"], 1)
            self.assertEqual(run["extracted_mention_count"], 1)
            self.assertEqual(run["dropped_mention_count"], 0)
            self.assertEqual(run["debug_metadata"]["collector_kind"], "agent-extraction")

    def test_stage4_accepted_sources_collect_new_signal_families_from_replay(self):
        config_path = REPO_ROOT / "configs" / "current-weekly-pilot.json"
        config = _load_config(config_path)
        stage4_names = {
            "National Book Awards 2026 Nonfiction",
            "Goodreads Choice Awards 2026 Nonfiction",
            "Audiobooks.com Nonfiction Audiobook Bestsellers",
            "Publishers Marketplace Rights Offerings",
        }
        # Issue #766: IndieBound and Publishers Marketplace are retired; the
        # collector is still measured on a copy that is switched on.
        sources = [{**source, "active": True} for source in config["sources"] if source.get("name") in stage4_names]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-13").collect(sources)

        mentions_by_family = {mention["source_family"]: mention for mention in result.source_mentions}
        self.assertEqual(
            set(mentions_by_family),
            {"award", "reader-interest", "audiobook ranking", "rights/deal"},
        )
        for mention in mentions_by_family.values():
            self.assertGreater(signal_strength_rank({"signal_strength": mention["signal_strength"]}), 0)
        self.assertEqual(result.failed_source_count, 0)
        self.assertEqual(result.skipped_source_count, 0)
        self.assertEqual(len(result.source_runs), 4)
        self.assertTrue(all(run["status"] == "collected" for run in result.source_runs))
        expected_counts = {
            "National Book Awards 2026 Nonfiction": 1,
            # Issue #996: the 2026 replay carries the twenty opening-round nominees.
            "Goodreads Choice Awards 2026 Nonfiction": 20,
            "Audiobooks.com Nonfiction Audiobook Bestsellers": 2,
            "Publishers Marketplace Rights Offerings": 1,
        }
        for run in result.source_runs:
            self.assertEqual(run["valid_mention_count"], expected_counts[run["source_name"]])

    def test_goodreads_configured_url_is_canonical_readers_favorite(self):
        """Goodreads Choice Awards 2026 Nonfiction must use the canonical
        readers-favorite-nonfiction-books-2026 slug, not the
        best-nonfiction-books-2026 alias that only redirects to it (Issue #996
        rolled the source over from the 2025 slugs)."""
        from autoscout.source_proposals import normalize_source_identity

        CANONICAL_URL = "https://www.goodreads.com/choiceawards/readers-favorite-nonfiction-books-2026"
        OBSOLETE_URL = "https://www.goodreads.com/choiceawards/best-nonfiction-books-2026"

        config_path = REPO_ROOT / "configs" / "current-weekly-pilot.json"
        config = _load_config(config_path)
        source = next(
            source for source in config["sources"] if source.get("name") == "Goodreads Choice Awards 2026 Nonfiction"
        )

        # 1. Configured URL must be the canonical slug.
        assert normalize_source_identity(source["url"]) == normalize_source_identity(CANONICAL_URL), (
            f"Configured URL {source['url']!r} does not match canonical {CANONICAL_URL!r}"
        )

        # 2. Replay page fixture must resolve to the same canonical identity.
        page_path = config_path.parent / source["source_page_fixture_file"]
        page = json.loads(page_path.read_text(encoding="utf-8"))
        assert normalize_source_identity(str(page.get("resolved_url", ""))) == normalize_source_identity(
            CANONICAL_URL
        ), f"Replay page resolved_url {page.get('resolved_url')!r} does not match canonical {CANONICAL_URL!r}"

        # 3. The obsolete slug must cause a wrong_target failure at the replay seam.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bad_page_path = tmp_path / "bad-page.json"
            bad_page_path.write_text(
                json.dumps(
                    {
                        "source_page": OBSOLETE_URL,
                        "resolved_url": OBSOLETE_URL,
                        "html": "<h1>Best Nonfiction 2026</h1>",
                    }
                ),
                encoding="utf-8",
            )
            bad_extraction_path = tmp_path / "bad-extraction.json"
            bad_extraction_path.write_text(
                json.dumps(
                    {
                        "mentions": [
                            {
                                "title": "Test Book",
                                "primary_authors": ["Test Author"],
                                "evidence_url": OBSOLETE_URL,
                                "signal_type": "reader-interest",
                                "signal_strength": "reader choice winner",
                                "evidence_note": "Test note.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            bad_source = {
                **source,
                "url": CANONICAL_URL,
                "source_page_fixture_file": str(bad_page_path),
                "extraction_response_file": str(bad_extraction_path),
            }
            with self.assertRaises(CollectorRunError) as ctx:
                codex_extraction_collector_result(tmp_path / "config.json", tmp_path, bad_source, "2026-07-19")
            self.assertEqual(
                ctx.exception.debug_metadata.get("safe_failure_status"),
                "wrong_target",
            )

    def test_stage4_audiobook_replay_keeps_underlying_book_work_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            page_path = tmp_path / "page.json"
            page_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/audio",
                        "html": '<a href="https://example.test/audiobook/book-backed/1">Audio ranking</a> by Morgan Example, Jordan Example',
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
                                "title": "Book-Backed Audio",
                                "primary_authors": ["Morgan Example"],
                                "source_url": "https://example.test/audio",
                                "evidence_url": "https://example.test/audiobook/book-backed/1",
                                "signal_type": "audiobook ranking",
                                "signal_strength": "minor mention",
                                "evidence_note": "Listed in implicit retailer bestselling order.",
                                "underlying_work_type": "book",
                                "candidate_category": "adult nonfiction",
                                "publication_status": "published",
                            },
                            {
                                "title": "Audio-Only Lecture",
                                "primary_authors": ["Jordan Example"],
                                "source_url": "https://example.test/audio",
                                "evidence_url": "https://example.test/audiobook/book-backed/1",
                                "signal_type": "audiobook ranking",
                                "signal_strength": "minor mention",
                                "evidence_note": "Listed in implicit retailer bestselling order.",
                                "underlying_work_type": "audio_only",
                                "candidate_category": "adult nonfiction",
                                "publication_status": "published",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-13").collect(
                [
                    {
                        "name": "Audio Replay",
                        "family": "audiobook ranking",
                        "url": "https://example.test/audio",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "agent-extraction-replay",
                        "source_page_fixture_file": str(page_path),
                        "extraction_response_file": str(extraction_path),
                    }
                ]
            )

        self.assertEqual([mention["title"] for mention in result.source_mentions], ["Book-Backed Audio"])
        self.assertEqual(result.source_runs[0]["raw_mention_count"], 2)
        self.assertEqual(result.source_runs[0]["valid_mention_count"], 1)
        self.assertEqual(result.source_runs[0]["skipped_item_count"], 1)
        self.assertEqual(result.source_runs[0]["editor_facing_count"], 1)

    def test_agent_extraction_drops_unverified_evidence_urls_and_reports_reasons(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            page_path = tmp_path / "page.json"
            page_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": '<a href="https://example.test/books/kept">Kept Book</a> by Alex Example, Robin Example',
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
                                "title": "Kept Book",
                                "primary_authors": ["Alex Example"],
                                "source_url": "https://example.test/books/kept",
                                "signal_strength": "major media list",
                                "evidence_note": "Listed on a configured source page.",
                            },
                            {
                                "title": "Dropped Book",
                                "primary_authors": ["Robin Example"],
                                "source_url": "https://elsewhere.test/books/dropped",
                                "signal_strength": "major media list",
                                "evidence_note": "Not actually linked from the source page.",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(
                [
                    {
                        "name": "Agent Source",
                        "family": "media/curated list",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "agent-extraction-replay",
                        "source_page_fixture_file": str(page_path),
                        "extraction_response_file": str(extraction_path),
                    }
                ]
            )

            self.assertEqual([mention["title"] for mention in result.source_mentions], ["Kept Book"])
            run = result.source_runs[0]
            self.assertEqual(run["raw_mention_count"], 2)
            self.assertEqual(run["valid_mention_count"], 1)
            self.assertEqual(run["dropped_mention_count"], 1)
            self.assertEqual(run["drop_reasons"][0]["title"], "Dropped Book")

    def test_agent_extraction_fails_when_all_mentions_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            page_path = tmp_path / "page.json"
            page_path.write_text(
                json.dumps({"source_page": "https://example.test/list", "html": ""}),
                encoding="utf-8",
            )
            extraction_path = tmp_path / "extraction.json"
            extraction_path.write_text(
                json.dumps(
                    {
                        "mentions": [
                            {
                                "title": "Dropped Book",
                                "primary_authors": ["Robin Example"],
                                "source_url": "https://elsewhere.test/books/dropped",
                                "signal_strength": "major media list",
                                "evidence_note": "Not linked from source page.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(
                [
                    {
                        "name": "Agent Source",
                        "family": "media/curated list",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "agent-extraction-replay",
                        "source_page_fixture_file": str(page_path),
                        "extraction_response_file": str(extraction_path),
                    }
                ]
            )

            self.assertEqual(result.source_mentions, [])
            self.assertEqual(result.failed_source_count, 1)
            self.assertEqual(result.source_runs[0]["status"], "failed")
            self.assertEqual(result.source_runs[0]["dropped_mention_count"], 1)

    def test_agent_extraction_rejects_copied_text_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            page_path = tmp_path / "page.json"
            page_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": '<a href="https://example.test/books/bad">Bad Book</a> by Alex Example',
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
                                "title": "Bad Book",
                                "primary_authors": ["Alex Example"],
                                "source_url": "https://example.test/books/bad",
                                "signal_strength": "major media list",
                                "evidence_note": "Listed on a configured source page.",
                                "review": "Copied review text is forbidden.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(
                [
                    {
                        "name": "Agent Source",
                        "family": "media/curated list",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "agent-extraction-replay",
                        "source_page_fixture_file": str(page_path),
                        "extraction_response_file": str(extraction_path),
                    }
                ]
            )

            self.assertEqual(result.failed_source_count, 1)
            self.assertIn("forbidden copied text fields", result.source_runs[0]["reason"])

    def test_agent_extraction_reports_malformed_and_empty_responses(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            page_path = tmp_path / "page.json"
            page_path.write_text(
                json.dumps({"source_page": "https://example.test/list", "html": ""}),
                encoding="utf-8",
            )
            malformed_path = tmp_path / "malformed.json"
            malformed_path.write_text(json.dumps({"mentions": {"bad": "shape"}}), encoding="utf-8")
            empty_path = tmp_path / "empty.json"
            empty_path.write_text(json.dumps({"mentions": []}), encoding="utf-8")
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(
                [
                    {
                        "name": "Malformed Agent Source",
                        "family": "media/curated list",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "agent-extraction-replay",
                        "source_page_fixture_file": str(page_path),
                        "extraction_response_file": str(malformed_path),
                    },
                    {
                        "name": "Empty Agent Source",
                        "family": "media/curated list",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "agent-extraction-replay",
                        "source_page_fixture_file": str(page_path),
                        "extraction_response_file": str(empty_path),
                    },
                ]
            )

            self.assertEqual(result.failed_source_count, 2)
            self.assertIn("mentions must be a list", result.source_runs[0]["reason"])
            self.assertIn("returned no mentions", result.source_runs[1]["reason"])

    def test_agent_extraction_status_response_can_skip_blocked_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            page_path = tmp_path / "page.json"
            page_path.write_text(
                json.dumps({"source_page": "https://example.test/list", "html": ""}),
                encoding="utf-8",
            )
            extraction_path = tmp_path / "blocked.json"
            extraction_path.write_text(json.dumps({"status": "blocked"}), encoding="utf-8")
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(
                [
                    {
                        "name": "Blocked Agent Source",
                        "family": "media/curated list",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "agent-extraction-replay",
                        "source_page_fixture_file": str(page_path),
                        "extraction_response_file": str(extraction_path),
                    }
                ]
            )

            self.assertEqual(result.skipped_source_count, 1)
            self.assertEqual(result.source_runs[0]["status"], "skipped")
            self.assertIn("blocked", result.source_runs[0]["reason"])

    def test_agent_extraction_live_requires_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(
                [
                    {
                        "name": "Agent Source",
                        "family": "media/curated list",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "agent-extraction-live",
                    }
                ]
            )

            self.assertEqual(result.skipped_source_count, 1)
            self.assertEqual(result.source_runs[0]["status"], "skipped")
            self.assertIn("requires enable_live", result.source_runs[0]["reason"])

    def test_extraction_capture_bound_failure_keeps_valid_mentions_and_collected_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            page_path = tmp_path / "page.json"
            page_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": '<a href="https://example.test/books/live">Live Book</a> by Alex Example',
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
                                "title": "Live Book",
                                "primary_authors": ["Alex Example"],
                                "source_url": "https://example.test/books/live",
                                "signal_strength": "major media list",
                                "evidence_note": "Captured from a live extraction response.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")
            # The capture destination sits inside a parent directory that does
            # not exist yet: the bounded writer creates parents only when a
            # capture actually reaches the write step.
            capture_path = tmp_path / "nested" / "captured-extraction.json"

            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(
                [
                    {
                        "name": "Agent Source",
                        "family": "media/curated list",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "agent-extraction-live",
                        "enable_live": True,
                        "source_page_fixture_file": str(page_path),
                        "extraction_response_file": str(extraction_path),
                        "capture_extraction_response_file": str(capture_path),
                        "max_response_bytes": 100,
                    }
                ]
            )

            # The optional extraction response capture exceeded its bound, but
            # the extraction itself was valid: the mention survives and the
            # source run stays collected with a path-free diagnostic.
            self.assertEqual([mention["title"] for mention in result.source_mentions], ["Live Book"])
            self.assertEqual(result.failed_source_count, 0)
            run = result.source_runs[0]
            self.assertEqual(run["status"], "collected")
            diagnostic = run["debug_metadata"]["capture_extraction_response_write_failure"]
            self.assertIn("extraction capture", diagnostic)
            self.assertIn("limit after serialization", diagnostic)
            self.assertNotIn(str(tmp_path), diagnostic)
            self.assertNotIn("/", diagnostic)
            self.assertFalse(capture_path.exists())
            self.assertFalse(capture_path.parent.exists())

    def test_live_response_capture_bound_failure_fails_source_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            page_path = tmp_path / "page.json"
            page_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": '<a href="https://example.test/books/live">' + "x" * 2_000 + "</a>",
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
                                "title": "Live Book",
                                "primary_authors": ["Alex Example"],
                                "source_url": "https://example.test/books/live",
                                "signal_strength": "major media list",
                                "evidence_note": "Captured from a live extraction response.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")
            capture_path = tmp_path / "captured-page.json"

            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(
                [
                    {
                        "name": "Agent Source",
                        "family": "media/curated list",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "agent-extraction-live",
                        "enable_live": True,
                        "source_page_fixture_file": str(page_path),
                        "extraction_response_file": str(extraction_path),
                        "capture_live_response_file": str(capture_path),
                        "max_response_bytes": 100,
                    }
                ]
            )

            # The live response capture is evidence, so its bound failure stays
            # fail-closed: the source fails and no mention is published.
            self.assertEqual(result.source_mentions, [])
            self.assertEqual(result.failed_source_count, 1)
            self.assertEqual(result.source_runs[0]["status"], "failed")
            self.assertFalse(capture_path.exists())

    def test_agent_extraction_live_fixture_capture_replays_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            page_path = tmp_path / "page.json"
            page_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": '<a href="https://example.test/books/live">Live Book</a> by Alex Example',
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
                                "title": "Live Book",
                                "primary_authors": ["Alex Example"],
                                "source_url": "https://example.test/books/live",
                                "signal_strength": "major media list",
                                "evidence_note": "Captured from a live extraction response.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")
            capture_path = tmp_path / "captured-extraction.json"

            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(
                [
                    {
                        "name": "Agent Source",
                        "family": "media/curated list",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "agent-extraction-live",
                        "enable_live": True,
                        "source_page_fixture_file": str(page_path),
                        "extraction_response_file": str(extraction_path),
                        "capture_extraction_response_file": str(capture_path),
                    }
                ]
            )

            self.assertEqual([mention["title"] for mention in result.source_mentions], ["Live Book"])
            self.assertTrue(capture_path.exists())
            captured = json.loads(capture_path.read_text(encoding="utf-8"))
            self.assertEqual(captured["collection_mode"], "live-fixture-replay")

            replay_result = SourceCollection(config_path, tmp_path / "runs-2", "2026-06-10").collect(
                [
                    {
                        "name": "Agent Source",
                        "family": "media/curated list",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "agent-extraction-replay",
                        "source_page_fixture_file": str(page_path),
                        "extraction_response_file": str(capture_path),
                    }
                ]
            )
            self.assertEqual(replay_result.source_mentions, result.source_mentions)

    def test_agent_extraction_real_backend_uses_deterministic_transport_and_replays(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            page_path = tmp_path / "page.json"
            page_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": '<a href="https://example.test/books/real">Real Book</a> by Riley Model',
                    }
                ),
                encoding="utf-8",
            )
            response_path = tmp_path / "real-response.json"
            response_path.write_text(
                json.dumps(
                    {
                        "mentions": [
                            {
                                "title": "Real Book",
                                "primary_authors": ["Riley Model"],
                                "source_url": "https://example.test/books/real",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Structured metadata extracted from the configured source page.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")
            capture_path = tmp_path / "captured-real-response.json"

            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(
                [
                    {
                        "name": "Agent Source",
                        "family": "media/curated list",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "agent-extraction-live",
                        "enable_live": True,
                        "extraction_backend": "real",
                        "enable_real_backend": True,
                        "credential_env": "AUTOSCOUT_TEST_MODEL_KEY",
                        "source_page_fixture_file": str(page_path),
                        "real_extraction_response_file": str(response_path),
                        "capture_extraction_response_file": str(capture_path),
                        "max_model_input_chars": 1000,
                    }
                ]
            )

            self.assertEqual([mention["title"] for mention in result.source_mentions], ["Real Book"])
            run = result.source_runs[0]
            # Deterministic transport (a configured response file) is a replay of
            # the real path, not a live model call, so it must not report bare
            # "real-live".
            self.assertEqual(run["collection_mode"], "real-live-replay")
            self.assertEqual(run["debug_metadata"]["backend_name"], "real")
            self.assertEqual(run["debug_metadata"]["backend_mode"], "real-live-replay")
            self.assertEqual(run["debug_metadata"]["real_extraction_transport"], "deterministic")
            self.assertEqual(run["debug_metadata"]["credential_env"], "AUTOSCOUT_TEST_MODEL_KEY")
            self.assertNotIn("api_key", json.dumps(run["debug_metadata"]).lower())
            self.assertTrue(capture_path.exists())
            captured = json.loads(capture_path.read_text(encoding="utf-8"))
            self.assertEqual(captured["collection_mode"], "real-live-replay")

            replay_result = SourceCollection(config_path, tmp_path / "runs-2", "2026-06-10").collect(
                [
                    {
                        "name": "Agent Source",
                        "family": "media/curated list",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "agent-extraction-replay",
                        "source_page_fixture_file": str(page_path),
                        "extraction_response_file": str(capture_path),
                    }
                ]
            )
            self.assertEqual(replay_result.source_mentions, result.source_mentions)

    def test_agent_extraction_real_backend_calls_injected_transport_for_live_model_path(self):
        class DeterministicTransport:
            def __init__(self) -> None:
                self.calls = []

            def extract(
                self,
                *,
                api_key,
                backend_name,
                source,
                page_data,
                max_chars,
                max_output_tokens,
                timeout_seconds,
            ):
                self.calls.append(
                    {
                        "api_key": api_key,
                        "backend_name": backend_name,
                        "source_page": page_data["source_page"],
                        "html": page_data["html"],
                        "max_chars": max_chars,
                        "max_output_tokens": max_output_tokens,
                        "timeout_seconds": timeout_seconds,
                    }
                )
                return {
                    "mentions": [
                        {
                            "title": "Live Model Book",
                            "primary_authors": ["Morgan Model"],
                            "source_url": "https://example.test/books/live-model",
                            "signal_type": "media/curated list",
                            "signal_strength": "major media list",
                            "evidence_note": "Structured metadata extracted from the fetched page.",
                        }
                    ]
                }, {"real_extraction_transport": "deterministic-test", "model": "test-model"}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")
            page_capture_path = tmp_path / "captured-page.json"
            extraction_capture_path = tmp_path / "captured-extraction.json"
            transport = DeterministicTransport()

            original_transport = source_collectors.REAL_AGENT_EXTRACTION_TRANSPORT
            original_fetch = source_collectors.fetch_agent_source_page
            original_secret = os.environ.get("AUTOSCOUT_TEST_MODEL_KEY")

            def fake_fetch(config_path, output_dir, source, *, reference_date=None):
                return {
                    "source_page": source["url"],
                    "html": '<a href="https://example.test/books/live-model">Live Model Book</a> by Morgan Model',
                }, {"source_url": source["url"]}

            try:
                os.environ["AUTOSCOUT_TEST_MODEL_KEY"] = "secret-value"
                source_collectors.REAL_AGENT_EXTRACTION_TRANSPORT = transport
                source_collectors.fetch_agent_source_page = fake_fetch

                result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(
                    [
                        {
                            "name": "Agent Source",
                            "family": "media/curated list",
                            "url": "https://example.test/list",
                            "access_status": "open",
                            "language": "en",
                            "extraction_method": "agent-extraction-live",
                            "enable_live": True,
                            "extraction_backend": "real",
                            "enable_real_backend": True,
                            "credential_env": "AUTOSCOUT_TEST_MODEL_KEY",
                            "capture_live_response_file": str(page_capture_path),
                            "capture_extraction_response_file": str(extraction_capture_path),
                            "max_model_input_chars": 1000,
                            "model_timeout_seconds": 12,
                        }
                    ]
                )
                page_capture_mode = json.loads(page_capture_path.read_text(encoding="utf-8"))["collection_mode"]
                extraction_capture_mode = json.loads(extraction_capture_path.read_text(encoding="utf-8"))[
                    "collection_mode"
                ]
            finally:
                source_collectors.REAL_AGENT_EXTRACTION_TRANSPORT = original_transport
                source_collectors.fetch_agent_source_page = original_fetch
                if original_secret is None:
                    os.environ.pop("AUTOSCOUT_TEST_MODEL_KEY", None)
                else:
                    os.environ["AUTOSCOUT_TEST_MODEL_KEY"] = original_secret

        self.assertEqual([mention["title"] for mention in result.source_mentions], ["Live Model Book"])
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0]["api_key"], "secret-value")
        self.assertEqual(transport.calls[0]["max_output_tokens"], 1200)
        run = result.source_runs[0]
        self.assertEqual(run["collection_mode"], "real-live")
        self.assertEqual(run["debug_metadata"]["backend_mode"], "real-live")
        self.assertEqual(run["debug_metadata"]["real_extraction_transport"], "deterministic-test")
        self.assertEqual(run["debug_metadata"]["model_max_output_tokens"], 1200)
        self.assertEqual(page_capture_mode, "real-live")
        self.assertEqual(extraction_capture_mode, "real-live")

    def test_agent_extraction_real_backend_compacts_oversized_html_before_transport(self):
        class DeterministicTransport:
            def __init__(self) -> None:
                self.calls = []

            def extract(
                self,
                *,
                api_key,
                backend_name,
                source,
                page_data,
                max_chars,
                max_output_tokens,
                timeout_seconds,
            ):
                self.calls.append(
                    {
                        "api_key": api_key,
                        "html": page_data["html"],
                        "max_chars": max_chars,
                    }
                )
                return {
                    "mentions": [
                        {
                            "title": "Live Model Book",
                            "primary_authors": ["Morgan Model"],
                            "source_url": "https://example.test/books/live-model",
                            "signal_type": "media/curated list",
                            "signal_strength": "major media list",
                            "evidence_note": "Structured metadata extracted from the fetched page.",
                        }
                    ]
                }, {"real_extraction_transport": "deterministic-test", "model": "test-model"}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")
            transport = DeterministicTransport()

            original_transport = source_collectors.REAL_AGENT_EXTRACTION_TRANSPORT
            original_fetch = source_collectors.fetch_agent_source_page
            original_secret = os.environ.get("AUTOSCOUT_TEST_MODEL_KEY")

            def fake_fetch(config_path, output_dir, source, *, reference_date=None):
                return {
                    "source_page": source["url"],
                    "html": (
                        "<html><body>"
                        '<a href="https://example.test/books/live-model">Live Model Book</a> by Morgan Model'
                        f"<script>{'x' * 5000}</script>"
                        f"<p>{'visible ' * 100}</p>"
                        "</body></html>"
                    ),
                }, {"source_url": source["url"]}

            try:
                os.environ["AUTOSCOUT_TEST_MODEL_KEY"] = "secret-value"
                source_collectors.REAL_AGENT_EXTRACTION_TRANSPORT = transport
                source_collectors.fetch_agent_source_page = fake_fetch

                result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(
                    [
                        {
                            "name": "Agent Source",
                            "family": "media/curated list",
                            "url": "https://example.test/list",
                            "access_status": "open",
                            "language": "en",
                            "extraction_method": "agent-extraction-live",
                            "enable_live": True,
                            "extraction_backend": "real",
                            "enable_real_backend": True,
                            "credential_env": "AUTOSCOUT_TEST_MODEL_KEY",
                            "max_model_input_chars": 120,
                        }
                    ]
                )
            finally:
                source_collectors.REAL_AGENT_EXTRACTION_TRANSPORT = original_transport
                source_collectors.fetch_agent_source_page = original_fetch
                if original_secret is None:
                    os.environ.pop("AUTOSCOUT_TEST_MODEL_KEY", None)
                else:
                    os.environ["AUTOSCOUT_TEST_MODEL_KEY"] = original_secret

        self.assertEqual([mention["title"] for mention in result.source_mentions], ["Live Model Book"])
        self.assertEqual(len(transport.calls), 1)
        self.assertLessEqual(len(transport.calls[0]["html"]), 120)
        self.assertNotIn("<script>", transport.calls[0]["html"])
        self.assertEqual(transport.calls[0]["max_chars"], 120)
        run = result.source_runs[0]
        self.assertEqual(run["status"], "collected")
        self.assertEqual(run["collection_mode"], "real-live")
        self.assertEqual(run["debug_metadata"]["safe_failure_status"], "ok")
        self.assertGreater(run["debug_metadata"]["raw_model_input_chars"], 120)
        self.assertLessEqual(run["debug_metadata"]["model_input_chars"], 120)
        self.assertTrue(run["debug_metadata"]["model_input_compacted"])
        self.assertTrue(run["debug_metadata"]["model_input_truncated"])

    def test_agent_extraction_real_backend_misconfiguration_degrades_to_source_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            page_path = tmp_path / "page.json"
            page_path.write_text(
                json.dumps({"source_page": "https://example.test/list", "html": ""}),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(
                [
                    {
                        "name": "Agent Source",
                        "family": "media/curated list",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "agent-extraction-live",
                        "enable_live": True,
                        "extraction_backend": "real",
                        "source_page_fixture_file": str(page_path),
                    }
                ]
            )

            self.assertEqual(result.skipped_source_count, 1)
            run = result.source_runs[0]
            self.assertEqual(run["status"], "skipped")
            self.assertIn("disabled", run["reason"])
            self.assertEqual(run["collection_mode"], "real-live-unavailable")
            self.assertEqual(run["debug_metadata"]["backend_mode"], "real-live-unavailable")
            self.assertEqual(run["debug_metadata"]["safe_failure_status"], "disabled")

    def test_agent_extraction_real_backend_bad_numeric_controls_fail_one_source(self):
        # A non-numeric or non-positive bounded control must degrade to a single
        # failed source-health record, not raise an uncaught ValueError that
        # aborts collection for every other source.
        for field, value, expected_status in (
            ("max_model_input_chars", "120k", "invalid_input_limit"),
            ("max_model_input_chars", 0, "invalid_input_limit"),
            ("model_max_output_tokens", "many", "invalid_output_limit"),
            ("model_max_output_tokens", 0, "invalid_output_limit"),
            ("model_timeout_seconds", "soon", "invalid_timeout"),
        ):
            with self.subTest(field=field, value=value):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    page_path = tmp_path / "page.json"
                    page_path.write_text(
                        json.dumps(
                            {
                                "source_page": "https://example.test/list",
                                "html": '<a href="https://example.test/books/ok">OK Book</a> by Riley Model',
                            }
                        ),
                        encoding="utf-8",
                    )
                    response_path = tmp_path / "real-response.json"
                    response_path.write_text(
                        json.dumps(
                            {
                                "mentions": [
                                    {
                                        "title": "OK Book",
                                        "primary_authors": ["Riley Model"],
                                        "source_url": "https://example.test/books/ok",
                                        "signal_type": "media/curated list",
                                        "signal_strength": "major media list",
                                        "evidence_note": "Metadata-only mention.",
                                    }
                                ]
                            }
                        ),
                        encoding="utf-8",
                    )
                    config_path = tmp_path / "sources.json"
                    config_path.write_text("{}", encoding="utf-8")

                    healthy = {
                        "name": "Healthy Source",
                        "family": "media/curated list",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "agent-extraction-replay",
                        "source_page_fixture_file": str(page_path),
                        "extraction_response_file": str(response_path),
                    }
                    broken = {
                        "name": "Agent Source",
                        "family": "media/curated list",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "agent-extraction-live",
                        "enable_live": True,
                        "extraction_backend": "real",
                        "enable_real_backend": True,
                        "credential_env": "AUTOSCOUT_TEST_MODEL_KEY",
                        "source_page_fixture_file": str(page_path),
                        "real_extraction_response_file": str(response_path),
                        field: value,
                    }

                    # Should not raise: the bad source is recorded, the healthy
                    # source still runs.
                    result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect([broken, healthy])

                    self.assertEqual(result.failed_source_count, 1)
                    run = result.source_runs[0]
                    self.assertEqual(run["source_name"], "Agent Source")
                    self.assertEqual(run["status"], "failed")
                    self.assertEqual(run["debug_metadata"]["safe_failure_status"], expected_status)
                    self.assertEqual(len(result.source_runs), 2)

    def test_agent_extraction_drop_reasons_distinguish_failure_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            page_with_links = tmp_path / "page.json"
            page_with_links.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": '<a href="https://example.test/books/kept">Kept</a> by Robin Example, Jordan Example',
                    }
                ),
                encoding="utf-8",
            )
            page_empty_html = tmp_path / "page-empty.json"
            page_empty_html.write_text(
                json.dumps({"source_page": "https://example.test/list", "html": ""}),
                encoding="utf-8",
            )

            extraction_path = tmp_path / "extraction.json"
            extraction_path.write_text(
                json.dumps(
                    {
                        "mentions": [
                            {
                                "title": "Cross-Domain Book",
                                "primary_authors": ["Robin Example"],
                                "source_url": "https://elsewhere.test/books/dropped",
                                "evidence_url": "https://elsewhere.test/books/dropped",
                                "signal_strength": "major media list",
                                "evidence_note": "Evidence URL from different domain not found on source page.",
                            },
                            {
                                "title": "Same-Domain Unlinked Book",
                                "primary_authors": ["Jordan Example"],
                                "source_url": "https://example.test/books/not-linked",
                                "evidence_url": "https://example.test/books/not-linked",
                                "signal_strength": "major media list",
                                "evidence_note": "Same-domain URL that does not appear as a link on source page.",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            # Page with links: both cross-domain and same-domain-not-linked should be dropped
            # with different reasons.
            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(
                [
                    {
                        "name": "Agent Source",
                        "family": "media/curated list",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "agent-extraction-replay",
                        "source_page_fixture_file": str(page_with_links),
                        "extraction_response_file": str(extraction_path),
                    }
                ]
            )

            self.assertEqual(result.source_mentions, [])
            run = result.source_runs[0]
            self.assertEqual(run["dropped_mention_count"], 2)
            dropped_titles = {reason["title"]: reason["reason"] for reason in run["drop_reasons"]}
            self.assertIn("Cross-Domain Book", dropped_titles)
            self.assertIn("domain differs from source", dropped_titles["Cross-Domain Book"])
            self.assertIn("Same-Domain Unlinked Book", dropped_titles)
            self.assertEqual(
                dropped_titles["Same-Domain Unlinked Book"],
                "evidence URL not found on source page",
            )

            # Empty HTML page prevents verification entirely.
            result = SourceCollection(config_path, tmp_path / "runs-2", "2026-06-10").collect(
                [
                    {
                        "name": "Agent Source",
                        "family": "media/curated list",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "agent-extraction-replay",
                        "source_page_fixture_file": str(page_empty_html),
                        "extraction_response_file": str(extraction_path),
                    }
                ]
            )

            self.assertEqual(result.source_mentions, [])
            run = result.source_runs[0]
            self.assertEqual(run["dropped_mention_count"], 2)
            dropped_titles = {reason["title"]: reason["reason"] for reason in run["drop_reasons"]}
            self.assertIn("cannot verify evidence URL", dropped_titles["Cross-Domain Book"])
            self.assertIn("cannot verify evidence URL", dropped_titles["Same-Domain Unlinked Book"])

    def test_agent_extraction_same_domain_verification_allows_relative_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            page_path = tmp_path / "page.json"
            page_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": (
                            '<a href="https://example.test/books/kept">Kept Book</a>'
                            '<a href="/books/relative-kept">Relative Book</a>'
                            "<p>by Alex Example, Robin Example, Jordan Example</p>"
                        ),
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
                                "title": "Absolute URL Book",
                                "primary_authors": ["Alex Example"],
                                "source_url": "https://example.test/books/kept",
                                "signal_strength": "major media list",
                                "evidence_note": "Absolute same-domain evidence URL.",
                            },
                            {
                                "title": "Relative URL Book",
                                "primary_authors": ["Robin Example"],
                                "source_url": "https://example.test/books/relative-kept",
                                "signal_strength": "major media list",
                                "evidence_note": "Relative URL resolved against source page.",
                            },
                            {
                                "title": "Page-Level Signal Book",
                                "primary_authors": ["Jordan Example"],
                                "source_url": "https://example.test/list",
                                "signal_strength": "major media list",
                                "evidence_note": "Page-level signal where evidence_url equals source_page.",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(
                [
                    {
                        "name": "Agent Source",
                        "family": "media/curated list",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "agent-extraction-replay",
                        "source_page_fixture_file": str(page_path),
                        "extraction_response_file": str(extraction_path),
                    }
                ]
            )

            self.assertEqual(
                [mention["title"] for mention in result.source_mentions],
                ["Absolute URL Book", "Relative URL Book", "Page-Level Signal Book"],
            )
            run = result.source_runs[0]
            self.assertEqual(run["extracted_mention_count"], 3)
            self.assertEqual(run["dropped_mention_count"], 0)
            self.assertEqual(run["valid_mention_count"], 3)

    def test_agent_extraction_source_run_always_records_drop_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            page_path = tmp_path / "page.json"
            page_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": '<a href="https://example.test/books/kept">Kept Book</a> by Alex Example',
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
                                "title": "Kept Book",
                                "primary_authors": ["Alex Example"],
                                "source_url": "https://example.test/books/kept",
                                "signal_strength": "major media list",
                                "evidence_note": "Verified mention.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(
                [
                    {
                        "name": "Agent Source",
                        "family": "media/curated list",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "agent-extraction-replay",
                        "source_page_fixture_file": str(page_path),
                        "extraction_response_file": str(extraction_path),
                    }
                ]
            )

            self.assertEqual(len(result.source_mentions), 1)
            run = result.source_runs[0]
            self.assertEqual(run["extracted_mention_count"], 1)
            self.assertEqual(run["valid_mention_count"], 1)
            self.assertEqual(run["dropped_mention_count"], 0)
            self.assertEqual(run["drop_reasons"], [])
            self.assertNotIn("review", run["reason"])
            self.assertNotIn("blurb", run["reason"])
            self.assertNotIn("article_body", run["reason"])

    def test_people_books_source_produces_valid_source_mentions(self):
        config_path = REPO_ROOT / "configs" / "current-weekly-pilot.json"
        config = _load_config(config_path)
        sources = [source for source in config["sources"] if source.get("name") == "PEOPLE Books"]
        self.assertEqual(len(sources), 1)
        source = sources[0]
        self.assertEqual(source["family"], "media/curated list")
        self.assertEqual(source["extraction_method"], "codex-extraction-replay")
        self.assertTrue(source["active"])
        # Stable source identity, not pinned to a single monthly article
        self.assertEqual(source["url"], "https://people.com/books/")
        self.assertIn("latest-list", source.get("notes", "").lower())
        self.assertIn("landing page", source.get("notes", "").lower())

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(sources)

        self.assertGreater(len(result.source_mentions), 0)
        for mention in result.source_mentions:
            self.assertEqual(mention["source_family"], "media/curated list")
            self.assertEqual(mention["source_name"], "PEOPLE Books")
            self.assertIn(mention["signal_strength"], {"major media list", "minor mention"})
            self.assertTrue(mention["title"])
            self.assertTrue(mention["primary_authors"])
            self.assertTrue(mention["evidence_note"])
            # Evidence note references the discovered list article, not pinned URL
            self.assertIn("June 2026", mention["evidence_note"])
            self.assertTrue(
                mention["evidence_url"].startswith("https://people.com"),
                f"Evidence URL should be on people.com: {mention['evidence_url']}",
            )
            # Only adult nonfiction; fiction is excluded
            self.assertEqual(mention.get("candidate_category"), "adult nonfiction")

        run = result.source_runs[0]
        self.assertEqual(run["status"], "collected")
        self.assertEqual(run["extracted_mention_count"], len(result.source_mentions))
        self.assertEqual(run["dropped_mention_count"], 0)
        self.assertEqual(run["debug_metadata"]["collector_kind"], "codex-extraction")
        self.assertEqual(result.failed_source_count, 0)

    def test_people_books_latest_list_discovery_uses_stable_identity_not_pinned_article(self):
        """PEOPLE uses stable source identity; list article is discovered each run."""
        config_path = REPO_ROOT / "configs" / "current-weekly-pilot.json"
        config = _load_config(config_path)
        source = next(source for source in config["sources"] if source.get("name") == "PEOPLE Books")
        # Source URL is the stable landing page, not a specific monthly article
        self.assertEqual(source["url"], "https://people.com/books/")
        self.assertNotIn("june-2026", source["url"])
        self.assertNotIn("best-nonfiction", source["url"])

        # The extraction response fixture contains a discovered_list metadata section
        extraction_path = config_path.parent / source["extraction_response_file"]
        extraction = json.loads(extraction_path.read_text(encoding="utf-8"))
        self.assertIn("discovered_list", extraction)
        self.assertIn("article_url", extraction["discovered_list"])
        self.assertIn("discovery_note", extraction["discovered_list"])
        self.assertIn("current-month", extraction["discovered_list"]["discovery_note"])
        self.assertIn("landing page", extraction["discovered_list"]["discovery_note"])

        # The page fixture is the landing page with a latest-list article link
        page_path = config_path.parent / source["source_page_fixture_file"]
        page = json.loads(page_path.read_text(encoding="utf-8"))
        self.assertIn("latest-list", page["html"])
        self.assertIn("best-nonfiction-books-june-2026", page["html"])

    def test_people_books_flows_into_weekly_run_with_csv_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "weekly"
            result = _run_mvp(Path("configs/current-weekly-pilot.json"), output_dir)
            self.assertEqual(result.returncode, 0, result.stderr)

            mentions = json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8"))
            people_mentions = [mention for mention in mentions if mention.get("source_name") == "PEOPLE Books"]
            self.assertGreater(len(people_mentions), 0)
            for mention in people_mentions:
                self.assertEqual(mention["source_family"], "media/curated list")
                self.assertTrue(
                    mention.get("candidate_category", "") == "adult nonfiction",
                    f"PEOPLE mention should be adult nonfiction: {mention['title']}",
                )
                # Evidence note references the latest-list article discovered from landing page
                self.assertIn("June 2026", mention["evidence_note"])

            source_runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
            people_run = next(run for run in source_runs if run["source_name"] == "PEOPLE Books")
            self.assertEqual(people_run["status"], "collected")
            self.assertEqual(people_run["extracted_mention_count"], len(people_mentions))
            self.assertEqual(people_run["dropped_mention_count"], 0)

            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows[0]), 30, "CSV column count must remain stable")
            people_titles = {mention["title"] for mention in people_mentions}
            matched_titles = {row["Title"] for row in rows if row["Title"] in people_titles}
            self.assertGreater(len(matched_titles), 0)
            for row in rows:
                if row["Title"] in people_titles:
                    self.assertIn("media/curated list", row.get("Signal summary", ""))
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            people_evidence = [
                evidence
                for candidate in candidates
                if candidate["title"] in people_titles
                for evidence in candidate["source_evidence"]
                if evidence["source_name"] == "PEOPLE Books"
            ]
            self.assertGreater(len(people_evidence), 0)
            self.assertTrue(all("June 2026" in evidence["evidence_note"] for evidence in people_evidence))

    def test_bookpage_agent_extraction_produces_valid_source_mentions(self):
        config_path = REPO_ROOT / "configs" / "current-weekly-pilot.json"
        config = _load_config(config_path)
        sources = [source for source in config["sources"] if source.get("name") == "BookPage Nonfiction"]
        self.assertEqual(len(sources), 1)
        source = sources[0]
        self.assertTrue(source["active"])
        self.assertEqual(source["extraction_method"], "codex-extraction-replay")
        self.assertEqual(source["family"], "media/curated list")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(sources)

        self.assertGreater(len(result.source_mentions), 0)
        for mention in result.source_mentions:
            self.assertEqual(mention["source_family"], "media/curated list")
            self.assertIn(mention["signal_strength"], {"major media list", "minor mention"})
            self.assertTrue(mention["title"])
            self.assertTrue(mention["primary_authors"])
            self.assertTrue(mention["evidence_note"])
            self.assertTrue(
                mention["evidence_url"].startswith("https://www.bookpage.com"),
                f"Evidence URL should be on bookpage.com: {mention['evidence_url']}",
            )

        run = result.source_runs[0]
        self.assertEqual(run["status"], "collected")
        self.assertEqual(run["debug_metadata"]["collector_kind"], "codex-extraction")
        self.assertEqual(run["dropped_mention_count"], 0)
        self.assertEqual(result.failed_source_count, 0)

    def test_bookpage_agent_extraction_rejects_copied_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            page_path = tmp_path / "page.json"
            page_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://www.bookpage.com/genres/nonfiction/",
                        "html": '<a href="https://www.bookpage.com/books/stolen-copy/">Stolen Copy Book</a> by Alex Example',
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
                                "title": "Stolen Copy Book",
                                "primary_authors": ["Alex Example"],
                                "source_url": "https://www.bookpage.com/books/stolen-copy/",
                                "signal_strength": "major media list",
                                "evidence_note": "This book was reviewed.",
                                "review": "This is a full copied review that should be rejected.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(
                [
                    {
                        "name": "BookPage Nonfiction",
                        "family": "media/curated list",
                        "url": "https://www.bookpage.com/genres/nonfiction/",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "agent-extraction-replay",
                        "source_page_fixture_file": str(page_path),
                        "extraction_response_file": str(extraction_path),
                    }
                ]
            )

            self.assertEqual(result.failed_source_count, 1)
            self.assertIn("forbidden copied text fields", result.source_runs[0]["reason"])
            self.assertIn("review", result.source_runs[0]["reason"])

    def test_bookpage_agent_extraction_drops_unverified_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            page_path = tmp_path / "page.json"
            page_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://www.bookpage.com/genres/nonfiction/",
                        "html": '<a href="https://www.bookpage.com/books/kept/">Kept Book</a> by Alex Example, Robin Example',
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
                                "title": "Kept Book",
                                "primary_authors": ["Alex Example"],
                                "source_url": "https://www.bookpage.com/books/kept/",
                                "signal_strength": "major media list",
                                "evidence_note": "Linked from BookPage.",
                            },
                            {
                                "title": "Hallucinated Book",
                                "primary_authors": ["Robin Example"],
                                "source_url": "https://www.bookpage.com/books/not-on-page/",
                                "evidence_url": "https://www.bookpage.com/books/not-on-page/",
                                "signal_strength": "major media list",
                                "evidence_note": "Not actually on the source page.",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(
                [
                    {
                        "name": "BookPage Nonfiction",
                        "family": "media/curated list",
                        "url": "https://www.bookpage.com/genres/nonfiction/",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "agent-extraction-replay",
                        "source_page_fixture_file": str(page_path),
                        "extraction_response_file": str(extraction_path),
                    }
                ]
            )

            self.assertEqual([mention["title"] for mention in result.source_mentions], ["Kept Book"])
            run = result.source_runs[0]
            self.assertEqual(run["dropped_mention_count"], 1)
            self.assertEqual(run["drop_reasons"][0]["title"], "Hallucinated Book")
            self.assertIn("not found on source page", run["drop_reasons"][0]["reason"])

    def test_usatoday_bestseller_source_produces_valid_mentions_and_health_counts(self):
        config_path = REPO_ROOT / "configs" / "current-weekly-pilot.json"
        config = _load_config(config_path)
        sources = [source for source in config["sources"] if "USA TODAY" in source.get("name", "")]
        self.assertEqual(len(sources), 1)
        source = sources[0]
        self.assertEqual(source["family"], "bestseller/ranking")
        self.assertEqual(source["extraction_method"], "codex-extraction-replay")
        self.assertTrue(source["active"])

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(sources)

        self.assertGreater(len(result.source_mentions), 0)
        for mention in result.source_mentions:
            self.assertEqual(mention["source_family"], "bestseller/ranking")
            self.assertEqual(mention["signal_type"], "bestseller/ranking")
            self.assertIn(mention["signal_strength"], {"sustained bestseller", "top ranking"})
            self.assertTrue(mention["title"])
            self.assertTrue(mention["primary_authors"])
            self.assertTrue(mention["evidence_note"])
            self.assertTrue(
                mention["evidence_url"].startswith("https://www.usatoday.com"),
                f"Evidence URL should be on usatoday.com: {mention['evidence_url']}",
            )
            self.assertTrue(any("А" <= c <= "я" or c in {"ё", "Ё"} for c in mention.get("content_description", "")))

        run = result.source_runs[0]
        self.assertEqual(run["status"], "collected")
        self.assertEqual(run["extracted_mention_count"], len(result.source_mentions))
        self.assertEqual(run["dropped_mention_count"], 0)
        self.assertEqual(run["debug_metadata"]["collector_kind"], "codex-extraction")
        self.assertEqual(result.failed_source_count, 0)

    def test_usatoday_bestseller_flows_into_weekly_run_with_csv_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "weekly"
            result = _run_mvp(Path("configs/current-weekly-pilot.json"), output_dir)
            self.assertEqual(result.returncode, 0, result.stderr)

            mentions = json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8"))
            usa_today_mentions = [
                mention
                for mention in mentions
                if mention.get("source_name") == "USA TODAY Bestselling Books Nonfiction"
            ]
            self.assertGreater(len(usa_today_mentions), 0)
            for mention in usa_today_mentions:
                self.assertEqual(mention["source_family"], "bestseller/ranking")
                self.assertEqual(mention["signal_type"], "bestseller/ranking")

            source_runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
            usa_today_run = next(
                run for run in source_runs if run["source_name"] == "USA TODAY Bestselling Books Nonfiction"
            )
            self.assertEqual(usa_today_run["status"], "collected")
            self.assertEqual(usa_today_run["extracted_mention_count"], len(usa_today_mentions))
            self.assertIn("bestseller/ranking", usa_today_run["family"])

            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows[0]), 30, "CSV column count must remain stable")
            usa_today_titles = {mention["title"] for mention in usa_today_mentions}
            matched_titles = {row["Title"] for row in rows if row["Title"] in usa_today_titles}
            self.assertGreater(len(matched_titles), 0)
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            usa_evidence = [
                evidence
                for candidate in candidates
                if candidate["title"] in usa_today_titles
                for evidence in candidate["source_evidence"]
                if evidence["source_name"] == "USA TODAY Bestselling Books Nonfiction"
            ]
            self.assertGreater(len(usa_evidence), 0)
            self.assertTrue(all(evidence["signal_type"] == "bestseller/ranking" for evidence in usa_evidence))

    def test_spiegel_non_english_source_preserves_original_metadata(self):
        config_path = REPO_ROOT / "configs" / "current-weekly-pilot.json"
        config = _load_config(config_path)
        sources = [source for source in config["sources"] if source.get("name") == "Spiegel Bestseller Sachbuch"]
        self.assertEqual(len(sources), 1)
        source = sources[0]
        self.assertTrue(source["active"])
        self.assertEqual(source["extraction_method"], "codex-extraction-replay")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(sources)

        self.assertEqual(len(result.source_mentions), 4)
        # Original German titles preserved
        titles = [mention["title"] for mention in result.source_mentions]
        self.assertIn("Das Schweigen der Frauen", titles)
        self.assertIn("Warum wir denken, was wir denken", titles)
        self.assertIn("Die Zerbrechlichkeit der Welt", titles)
        self.assertIn("Der Weg zur Selbstbefreiung", titles)

        for mention in result.source_mentions:
            # English helper fields present but not overwriting original
            if mention["title"] == "Das Schweigen der Frauen":
                self.assertEqual(mention["english_title"], "The Silence of Women")
                self.assertEqual(mention["english_author_display"], "Bascha Mika")
                # Original German title and author remain primary
                self.assertEqual(mention["title"], "Das Schweigen der Frauen")
                self.assertEqual(mention["primary_authors"], ["Bascha Mika"])
                # Search notes are notes, not display metadata
                self.assertEqual(mention["search_translation"], "Молчание женщин")
                self.assertEqual(mention["author_transliteration_note"], "Баша Мика")
            # Content description is Russian prose
            self.assertTrue(
                any("А" <= c <= "я" or c in {"ё", "Ё"} for c in mention.get("content_description", "")),
                f"content_description must be Russian: {mention.get('content_description', '')}",
            )
            # Source family correct
            self.assertEqual(mention["source_family"], "bestseller/ranking")

    def test_spiegel_non_english_flows_into_weekly_run_preserving_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "weekly"
            result = _run_mvp(Path("configs/current-weekly-pilot.json"), output_dir)
            self.assertEqual(result.returncode, 0, result.stderr)

            mentions = json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8"))
            spiegel_mentions = [
                mention for mention in mentions if mention.get("source_name") == "Spiegel Bestseller Sachbuch"
            ]
            self.assertEqual(len(spiegel_mentions), 4)

            for mention in spiegel_mentions:
                # Original German title preserved in source mentions
                self.assertIn(
                    mention["title"],
                    {
                        "Das Schweigen der Frauen",
                        "Warum wir denken, was wir denken",
                        "Die Zerbrechlichkeit der Welt",
                        "Der Weg zur Selbstbefreiung",
                    },
                )
                # English helper fields are present but secondary
                self.assertTrue(mention.get("english_title"))
                # search_translation and author_transliteration_note are present
                self.assertTrue(mention.get("search_translation"))
                self.assertTrue(mention.get("author_transliteration_note"))

            source_runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
            spiegel_run = next(run for run in source_runs if run["source_name"] == "Spiegel Bestseller Sachbuch")
            self.assertEqual(spiegel_run["status"], "collected")
            self.assertEqual(spiegel_run["extracted_mention_count"], 4)
            self.assertEqual(spiegel_run["dropped_mention_count"], 0)

            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows[0]), 30, "CSV column count must remain stable")

            # Issue #379: assert the invariant, not a cap-derived count. A collected
            # source with eligible mentions reaches the editor sheet, and every row it
            # contributes preserves its German display metadata. Pinning an absolute
            # count here tracked the row cap instead of behaviour and was rebased by
            # both #366 and #374.
            spiegel_titles = {mention["title"] for mention in spiegel_mentions}
            spiegel_rows = [row for row in rows if row["Title"] in spiegel_titles]
            self.assertEqual(len(spiegel_rows), spiegel_run["editor_facing_count"])
            self.assertGreater(len(spiegel_rows), 0, "a collected source with eligible mentions must reach the editor")
            self.assertLessEqual(len(spiegel_rows), spiegel_run["extracted_mention_count"])

            for row in spiegel_rows:
                title = row["Title"]
                # Original German title displayed as primary Title
                self.assertIn(title, spiegel_titles)
                # English helper fields are populated on every row, not just one.
                # Issue #379: the pre-#374 block checked one known title only, which
                # left the "helpers populated" invariant unasserted for the rest.
                self.assertTrue(row["English title"].strip(), f"{title} has no English title helper")
                self.assertTrue(row["English author display"].strip(), f"{title} has no English author display")
                if title == "Das Schweigen der Frauen":
                    self.assertEqual(row["English title"], "The Silence of Women (machine translation)")
                    self.assertEqual(row["English author display"], "Bascha Mika (original)")
                # Description is Russian prose
                self.assertTrue(any("А" <= c <= "я" or c in {"ё", "Ё"} for c in row["Description"]))
                # Russian-edition status determined from evidence, not generated
                # translation. Issue #379: the empty string is no longer accepted,
                # because it let a missing status pass as valid.
                self.assertIn(
                    row["Russian-edition status"],
                    {"not found in Russian", "found in Russian", "unclear"},
                )

    def test_spiegel_non_english_search_notes_do_not_overwrite_display_metadata(self):
        """Prove that Search Translation and Author Transliteration Note are query notes only."""
        config_path = REPO_ROOT / "configs" / "current-weekly-pilot.json"
        config = _load_config(config_path)
        sources = [source for source in config["sources"] if source.get("name") == "Spiegel Bestseller Sachbuch"]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(sources)

        first = result.source_mentions[0]
        # Title and author are original German, NOT the transliterated or translated values
        self.assertEqual(first["title"], "Das Schweigen der Frauen")
        self.assertEqual(first["primary_authors"], ["Bascha Mika"])
        # search_translation is stored but does not become the display title
        self.assertEqual(first["search_translation"], "Молчание женщин")
        self.assertNotEqual(first["title"], first["search_translation"])
        # author_transliteration_note is stored but does not become display author
        self.assertEqual(first["author_transliteration_note"], "Баша Мика")
        self.assertNotEqual(first["primary_authors"], [first["author_transliteration_note"]])

    # --- #147 Deutscher Sachbuchpreis (repaired #679: 2024 -> 2026 current edition) ---

    def test_deutscher_sachbuchpreis_produces_award_signals_with_german_metadata(self):
        config_path = REPO_ROOT / "configs" / "current-weekly-pilot.json"
        config = _load_config(config_path)
        sources = [source for source in config["sources"] if source.get("name") == "Deutscher Sachbuchpreis"]
        self.assertEqual(len(sources), 1)
        source = sources[0]
        self.assertEqual(source["family"], "award")
        self.assertTrue(source["active"])

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(sources)

        # Issue #679: stable-landing repair proves the 2026 current edition
        # (winner plus two nominees as the three representative entries),
        # not the stale 2024 four-book list.
        self.assertEqual(len(result.source_mentions), 3)
        strengths = {mention["signal_strength"] for mention in result.source_mentions}
        self.assertIn("winner", strengths)
        self.assertIn("shortlist", strengths)

        for mention in result.source_mentions:
            self.assertEqual(mention["source_family"], "award")
            self.assertEqual(mention["signal_type"], "award")
            # Original German title preserved
            self.assertTrue(mention["title"])
            self.assertTrue(mention["primary_authors"])
            # Evidence URL verifiable from source page
            self.assertTrue(
                mention["evidence_url"].startswith("https://www.deutscher-sachbuchpreis.de"),
                f"Evidence URL should be on deutscher-sachbuchpreis.de: {mention['evidence_url']}",
            )
            # Russian content description present
            self.assertTrue(any("А" <= c <= "я" or c in {"ё", "Ё"} for c in mention.get("content_description", "")))
            # English helper fields present but secondary
            self.assertTrue(mention.get("english_title"))

        run = result.source_runs[0]
        self.assertEqual(run["status"], "collected")
        self.assertEqual(run["extracted_mention_count"], 3)
        self.assertEqual(run["dropped_mention_count"], 0)
        self.assertEqual(result.failed_source_count, 0)

    def test_deutscher_sachbuchpreis_flows_into_weekly_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "weekly"
            result = _run_mvp(Path("configs/current-weekly-pilot.json"), output_dir)
            self.assertEqual(result.returncode, 0, result.stderr)

            mentions = json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8"))
            dsp_mentions = [m for m in mentions if m.get("source_name") == "Deutscher Sachbuchpreis"]
            # Issue #679: three 2026 representative entries (winner + two nominees).
            self.assertEqual(len(dsp_mentions), 3)
            self.assertTrue(all(m["source_family"] == "award" for m in dsp_mentions))

            source_runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
            dsp_run = next(r for r in source_runs if r["source_name"] == "Deutscher Sachbuchpreis")
            self.assertEqual(dsp_run["status"], "collected")
            self.assertEqual(dsp_run["extracted_mention_count"], 3)

            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows[0]), 30)
            dsp_titles = {m["title"] for m in dsp_mentions}
            dsp_rows = [r for r in rows if r["Title"] in dsp_titles]
            self.assertGreater(
                len(dsp_rows), 0, f"At least one Deutscher Sachbuchpreis candidate should appear; got {len(dsp_rows)}"
            )
            for row in dsp_rows:
                self.assertIn("award", row.get("Signal summary", ""))

    # --- #148 Die Zeit Sachbuch-Bestenliste ---

    def test_zeit_bestenliste_produces_curated_list_signals_with_german_metadata(self):
        config_path = REPO_ROOT / "configs" / "current-weekly-pilot.json"
        config = _load_config(config_path)
        sources = [source for source in config["sources"] if source.get("name") == "Die Zeit Sachbuch-Bestenliste"]
        self.assertEqual(len(sources), 1)
        source = sources[0]
        self.assertEqual(source["family"], "media/curated list")
        # Issue #764: the source is retired (operator decision of 2026-10-06);
        # the site refuses the collector and the list is subscriber content.
        self.assertFalse(source["active"])
        self.assertEqual(source["lifecycle_status"], "retired")

        # The replay fixture still proves the German metadata contract, so it
        # is collected here from a copy of the source that is switched on.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(
                [{**source, "active": True}]
            )

        # Five mentions extracted: four adult-nonfiction Sachbuch entries plus one
        # fiction novel picked up from the page's related-recommendations section.
        self.assertEqual(len(result.source_mentions), 5)
        nonfiction_mentions = [
            mention for mention in result.source_mentions if mention.get("candidate_category") != "fiction"
        ]
        fiction_mentions = [
            mention for mention in result.source_mentions if mention.get("candidate_category") == "fiction"
        ]
        self.assertEqual(len(nonfiction_mentions), 4)
        self.assertEqual(len(fiction_mentions), 1)
        self.assertEqual(fiction_mentions[0]["title"], "Tyll")
        self.assertEqual(fiction_mentions[0]["candidate_category"], "fiction")

        for mention in nonfiction_mentions:
            self.assertEqual(mention["source_family"], "media/curated list")
            self.assertEqual(mention["signal_type"], "media/curated list")
            self.assertIn(mention["signal_strength"], {"major media list", "minor mention"})
            self.assertTrue(mention["evidence_url"].startswith("https://www.zeit.de"))
            # Original German title preserved, not overwritten by English/translated
            self.assertTrue(mention["title"])
            self.assertTrue(mention["primary_authors"])
            self.assertNotEqual(mention["title"], mention.get("search_translation", ""))
            # Russian content description
            self.assertTrue(any("А" <= c <= "я" or c in {"ё", "Ё"} for c in mention.get("content_description", "")))

        run = result.source_runs[0]
        self.assertEqual(run["status"], "collected")
        self.assertEqual(run["extracted_mention_count"], 5)
        self.assertEqual(run["dropped_mention_count"], 0)

    def test_retired_zeit_bestenliste_stays_out_of_the_weekly_run(self):
        """Issue #764: the source is retired, so the weekly run records it as
        inactive and takes nothing from its replay fixture."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "weekly"
            result = _run_mvp(Path("configs/current-weekly-pilot.json"), output_dir)
            self.assertEqual(result.returncode, 0, result.stderr)

            mentions = json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8"))
            zeit_mentions = [m for m in mentions if m.get("source_name") == "Die Zeit Sachbuch-Bestenliste"]
            self.assertEqual(zeit_mentions, [])

            source_runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
            zeit_run = next(r for r in source_runs if r["source_name"] == "Die Zeit Sachbuch-Bestenliste")
            self.assertEqual(zeit_run["status"], "inactive")
            self.assertEqual(zeit_run["extracted_mention_count"], 0)

    # --- #149 Russian-edition query safety for German candidates ---

    def test_german_candidate_search_aids_never_become_edition_facts(self):
        """Search Translation and Author Transliteration Note are query aids, not edition facts."""
        config_path = REPO_ROOT / "configs" / "current-weekly-pilot.json"
        config = _load_config(config_path)
        sources = [
            source
            for source in config["sources"]
            if source.get("name") in {"Deutscher Sachbuchpreis", "Die Zeit Sachbuch-Bestenliste"}
        ]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(sources)

        for mention in result.source_mentions:
            # Search Translation is present but does NOT equal the title
            if mention.get("search_translation"):
                self.assertNotEqual(
                    mention["title"],
                    mention["search_translation"],
                    f"search_translation must not equal title for {mention['title']}",
                )
            # Author Transliteration Note is present but does NOT replace primary_authors
            if mention.get("author_transliteration_note"):
                self.assertNotEqual(
                    mention["primary_authors"],
                    [mention["author_transliteration_note"]],
                    f"author_transliteration_note must not replace primary_authors for {mention['title']}",
                )
            # Russian-edition status uses evidence, not generated search aid
            self.assertIn(
                mention.get("russian_edition_status", ""),
                {"not found in Russian", "found in Russian", "unclear", ""},
            )
            # English helper fields don't overwrite original title
            if mention.get("english_title"):
                if mention["title"].strip().lower() == mention["english_title"].strip().lower():
                    # A proper-noun title can be spelled identically in both
                    # languages ("Tyll" is the published English title too).
                    # The helper then equals the title without replacing it,
                    # so the search aid must still differ instead.
                    self.assertTrue(
                        mention.get("search_translation"),
                        f"search_translation must be present for {mention['title']}",
                    )
                    self.assertNotEqual(
                        mention["title"],
                        mention["search_translation"],
                        f"search_translation must not equal title for {mention['title']}",
                    )
                else:
                    self.assertNotEqual(
                        mention["title"].strip().lower(),
                        mention["english_title"].strip().lower(),
                        "English title should not equal original German title",
                    )

    def test_german_candidate_russian_edition_requires_real_evidence_not_search_aid(self):
        """Found in Russian requires real evidence, not generated translation match."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            recorded_path = tmp_path / "german-candidates.json"

            # German candidate with search aids but NO real Russian edition evidence
            mentions = [
                {
                    "title": "Die Verwandlung der Welt",
                    "primary_authors": ["Jürgen Osterhammel"],
                    "source_url": "https://www.deutscher-sachbuchpreis.de/archiv/2024/shortlist/die-verwandlung-der-welt",
                    "source_check_date": "2026-06-10",
                    "signal_type": "award",
                    "signal_strength": "shortlist",
                    "evidence_note": "Deutscher Sachbuchpreis 2024 shortlist.",
                    "candidate_category": "adult nonfiction",
                    "russian_edition_status": "not found in Russian",
                    "visible_risk_status": "no visible risk",
                    "visible_risk_status_source": "manual",
                    "content_description": "Немецкий научно-популярный бестселлер о глобальной истории XIX века.",
                    "english_title": "The Transformation of the World",
                    "english_author_display": "Jürgen Osterhammel",
                    "search_translation": "Преображение мира",
                    "author_transliteration_note": "Юрген Остерхаммель",
                },
                {
                    "title": "Was ist eigentlich Faschismus?",
                    "primary_authors": ["Hedwig Richter", "Bernd Ulrich"],
                    "source_url": "https://www.deutscher-sachbuchpreis.de/archiv/2024/shortlist/was-ist-eigentlich-faschismus",
                    "source_check_date": "2026-06-10",
                    "signal_type": "award",
                    "signal_strength": "shortlist",
                    "evidence_note": "Deutscher Sachbuchpreis 2024 shortlist.",
                    "candidate_category": "adult nonfiction",
                    "russian_edition_status": "unclear",
                    "visible_risk_status": "no visible risk",
                    "visible_risk_status_source": "manual",
                    "content_description": "Немецкий бестселлер об исторических корнях фашизма.",
                    "english_title": "What Is Fascism, Actually?",
                    "english_author_display": "Hedwig Richter & Bernd Ulrich",
                    "search_translation": "Что такое фашизм на самом деле",
                    "author_transliteration_note": "Хедвиг Рихтер, Бернд Ульрих",
                },
                {
                    "title": "Unsere Welt neu denken",
                    "primary_authors": ["Maja Göpel"],
                    "source_url": "https://www.zeit.de/kultur/literatur/sachbuch-bestenliste/unsere-welt-neu-denken",
                    "source_check_date": "2026-06-10",
                    "signal_type": "media/curated list",
                    "signal_strength": "major media list",
                    "evidence_note": "Die Zeit Sachbuch-Bestenliste entry.",
                    "candidate_category": "adult nonfiction",
                    "visible_risk_status": "no visible risk",
                    "visible_risk_status_source": "manual",
                    "content_description": "Немецкий бестселлер о новом экономическом мышлении.",
                    "english_title": "Rethinking Our World",
                    "english_author_display": "Maja Göpel",
                    "search_translation": "Переосмысливая наш мир",
                    "author_transliteration_note": "Майя Гёпель",
                },
            ]
            recorded_path.write_text(json.dumps({"mentions": mentions}), encoding="utf-8")

            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-06-10",
                        "sources": [
                            {
                                "name": "German Candidates Test",
                                "family": "award",
                                "url": "https://www.deutscher-sachbuchpreis.de/",
                                "access_status": "open",
                                "language": "de",
                                "extraction_method": "recorded",
                                "recorded_mentions_file": str(recorded_path),
                            }
                        ],
                        "russian_edition_checks": [
                            {
                                "retailer": "Ozon",
                                "url": "https://www.ozon.ru",
                                "fixture_results": [
                                    {
                                        "candidate_title": "Unsere Welt neu denken",
                                        "primary_authors": ["Maja Göpel"],
                                        "match_type": "same-work Russian-language edition",
                                        "language": "Russian",
                                        "edition_url": "https://www.ozon.ru/product/unsere-welt-neu-denken-ru",
                                        "evidence_note": "Ozon lists a Russian-language edition of the same work.",
                                    }
                                ],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = _run_mvp(config_path, tmp_path / "runs")
            self.assertEqual(result.returncode, 0, result.stderr)

            candidates = json.loads((tmp_path / "runs" / "scouting-candidates.json").read_text(encoding="utf-8"))
            ver = next(c for c in candidates if c["title"] == "Die Verwandlung der Welt")
            fas = next(c for c in candidates if c["title"] == "Was ist eigentlich Faschismus?")
            self.assertNotIn("Unsere Welt neu denken", {candidate["title"] for candidate in candidates})

            # Search Translation is stored in russian_search_notes, not as top-level field
            self.assertEqual(ver["title"], "Die Verwandlung der Welt")
            self.assertEqual(ver.get("russian_search_notes", {}).get("search_translation"), "Преображение мира")
            self.assertNotEqual(ver["title"], ver.get("russian_search_notes", {}).get("search_translation", ""))

            # Author Transliteration Note is stored in russian_search_notes, authors stay original
            self.assertEqual(fas["primary_authors"], ["Hedwig Richter", "Bernd Ulrich"])
            self.assertEqual(
                fas.get("russian_search_notes", {}).get("author_transliteration_note"),
                "Хедвиг Рихтер, Бернд Ульрих",
            )

            # Russian-edition status uses evidence, not generated search aid
            self.assertEqual(ver["russian_edition_status"], "unclear")
            self.assertEqual(fas["russian_edition_status"], "unclear")

            # Editor-facing CSV preserves German title/author display
            with (tmp_path / "runs" / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            ver_row = next(r for r in rows if r["Title"] == "Die Verwandlung der Welt")
            self.assertEqual(ver_row["Title"], "Die Verwandlung der Welt")
            self.assertEqual(ver_row["English title"], "The Transformation of the World (machine translation)")
            self.assertEqual(ver_row["English author display"], "Jürgen Osterhammel (original)")
            self.assertEqual(ver_row["Russian-edition status"], "unclear")
            self.assertNotIn("Unsere Welt neu denken", {row["Title"] for row in rows})

            exclusions = json.loads((tmp_path / "runs" / "internal-exclusions.json").read_text(encoding="utf-8"))
            found = next(exclusion for exclusion in exclusions if exclusion["title"] == "Unsere Welt neu denken")
            self.assertEqual(found["exclusion_reason"], "found in Russian")
            self.assertEqual(found["russian_edition_url"], "https://www.ozon.ru/product/unsere-welt-neu-denken-ru")
            self.assertEqual(found["evidence"][0]["match_type"], "same-work Russian-language edition")

            russian_checks = json.loads((tmp_path / "runs" / "russian-edition-checks.json").read_text(encoding="utf-8"))
            unsere_check = next(
                check for check in russian_checks["candidate_checks"] if check["title"] == "Unsere Welt neu denken"
            )
            self.assertEqual(unsere_check["resolved_status"], "found in Russian")
            self.assertEqual(
                unsere_check["evidence"][0]["url"],
                "https://www.ozon.ru/product/unsere-welt-neu-denken-ru",
            )

    def test_casing_variant_russian_status_excluded_from_editor_facing_csv(self):
        """Issue #238 regression: a mention whose russian_edition_status differs only
        in casing from the canonical "found in Russian" must be excluded from the
        editor-facing CSV, consistent with it also being counted as excluded during
        source-level collection (see the companion collect()-level regression test
        above). Both paths now go through the shared russian_status predicates."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-06-10",
                        "sources": [
                            {
                                "name": "Casing Variant Source",
                                "family": "award",
                                "url": "https://example.test/casing-variant",
                                "access_status": "open",
                                "language": "en",
                                "extraction_method": "fixture",
                                "fixture_candidates": [
                                    {
                                        "title": "Casing Variant Book",
                                        "primary_authors": ["Alex Example"],
                                        "source_url": "https://example.test/casing-variant/book",
                                        "signal_strength": "winner",
                                        "evidence_note": "Won a configured award.",
                                        # Casing variant of the canonical "found in Russian".
                                        "russian_edition_status": "Found in Russian",
                                    }
                                ],
                            }
                        ],
                        "russian_edition_checks": [],
                    }
                ),
                encoding="utf-8",
            )

            result = _run_mvp(config_path, tmp_path / "runs")
            self.assertEqual(result.returncode, 0, result.stderr)

            with (tmp_path / "runs" / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertNotIn("Casing Variant Book", {row["Title"] for row in rows})

            exclusions = json.loads((tmp_path / "runs" / "internal-exclusions.json").read_text(encoding="utf-8"))
            excluded_titles = {exclusion["title"] for exclusion in exclusions}
            self.assertIn("Casing Variant Book", excluded_titles)
            found = next(exclusion for exclusion in exclusions if exclusion["title"] == "Casing Variant Book")
            self.assertEqual(found["exclusion_reason"], "found in Russian")

            source_runs = json.loads((tmp_path / "runs" / "source-runs.json").read_text(encoding="utf-8"))
            run = next(run for run in source_runs if run["source_name"] == "Casing Variant Source")
            self.assertEqual(run["excluded_candidate_count"], 1)

    # --- #146 first real German nonfiction source stream: cross-cutting contract ---

    def test_german_source_drops_mention_with_reason(self):
        """Source health: a German agent-extraction source drops a mention without a
        verifiable evidence URL and carries a reason, while valid mentions still collect."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            copy_fixture("source-pages", "german-bestenliste.json", tmp_path / "german-page.json")
            copy_fixture("extraction-responses", "german-bestenliste-drop.json", tmp_path / "german-extraction.json")
            # Issue #645: German generic source requires helper fields.
            extraction_file = tmp_path / "german-extraction.json"
            payload = json.loads(extraction_file.read_text(encoding="utf-8"))
            helpers = {
                "Valides Buch": {
                    "english_title": "Valid Book",
                    "search_translation": "Валидная книга",
                    "author_transliteration_note": "Анна Шмидт",
                },
                "Ohne Beweis": {
                    "english_title": "Without Proof",
                    "search_translation": "Без доказательств",
                    "author_transliteration_note": "Бернд Тест",
                },
            }
            for entry in payload.get("mentions", []):
                entry.update(helpers.get(entry.get("title", ""), {}))
            extraction_file.write_text(json.dumps(payload), encoding="utf-8")
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-06-10",
                        "sources": [
                            {
                                "name": "Beispiel Deutsche Sachbuchliste",
                                "family": "media/curated list",
                                "url": "https://www.example-german-source.test/bestenliste",
                                "access_status": "open",
                                "language": "de",
                                "extraction_method": "agent-extraction-replay",
                                "source_page_fixture_file": "german-page.json",
                                "extraction_response_file": "german-extraction.json",
                                "notes": "German source health test: one valid plus one dropped mention.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(
                json.loads(config_path.read_text(encoding="utf-8"))["sources"]
            )

        self.assertEqual(result.failed_source_count, 0)
        self.assertEqual(result.skipped_source_count, 0)
        # Only the verifiable mention becomes a Source Mention.
        self.assertEqual(len(result.source_mentions), 1)
        self.assertEqual(result.source_mentions[0]["title"], "Valides Buch")

        run = result.source_runs[0]
        self.assertEqual(run["status"], "collected")
        self.assertEqual(run["extracted_mention_count"], 2)
        self.assertEqual(run["valid_mention_count"], 1)
        self.assertEqual(run["dropped_mention_count"], 1)
        self.assertEqual(len(run["drop_reasons"]), 1)
        self.assertEqual(run["drop_reasons"][0]["title"], "Ohne Beweis")
        self.assertIn("evidence URL", run["drop_reasons"][0]["reason"])

    def test_german_skipped_source_does_not_crash_weekly_run(self):
        """Source health: a blocked/paid German source becomes a Skipped Source with a
        reason and does not crash the weekly run; a working German source still collects."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            copy_fixture("source-pages", "german-bestenliste.json", tmp_path / "german-page.json")
            copy_fixture("extraction-responses", "german-bestenliste-skip.json", tmp_path / "german-extraction.json")
            # Issue #645: German generic source requires helper fields.
            extraction_file = tmp_path / "german-extraction.json"
            payload = json.loads(extraction_file.read_text(encoding="utf-8"))
            for entry in payload.get("mentions", []):
                if entry.get("title") == "Valides Buch":
                    entry.update(
                        {
                            "english_title": "Valid Book",
                            "search_translation": "Валидная книга",
                            "author_transliteration_note": "Анна Шмидт",
                        }
                    )
            extraction_file.write_text(json.dumps(payload), encoding="utf-8")
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-06-10",
                        "sources": [
                            {
                                "name": "Beispiel Deutsche Sachbuchliste",
                                "family": "media/curated list",
                                "url": "https://www.example-german-source.test/bestenliste",
                                "access_status": "open",
                                "language": "de",
                                "extraction_method": "agent-extraction-replay",
                                "source_page_fixture_file": "german-page.json",
                                "extraction_response_file": "german-extraction.json",
                                "notes": "Working German source.",
                            },
                            {
                                "name": "Bezahlte Deutsche Rechtequelle",
                                "family": "rights/deal",
                                "url": "https://www.example-german-paid.test/rechte",
                                "access_status": "paid",
                                "language": "de",
                                "extraction_method": "agent-extraction-replay",
                                "notes": "Paid German source that must be skipped, not collected.",
                            },
                        ],
                        "russian_edition_checks": [],
                    }
                ),
                encoding="utf-8",
            )

            result = _run_mvp(config_path, tmp_path / "runs")
            self.assertEqual(result.returncode, 0, result.stderr)

            source_runs = json.loads((tmp_path / "runs" / "source-runs.json").read_text(encoding="utf-8"))
            by_name = {run["source_name"]: run for run in source_runs}
            self.assertEqual(by_name["Beispiel Deutsche Sachbuchliste"]["status"], "collected")
            self.assertEqual(by_name["Bezahlte Deutsche Rechtequelle"]["status"], "skipped")
            self.assertIn("paid", by_name["Bezahlte Deutsche Rechtequelle"]["reason"].lower())

            summary = json.loads((tmp_path / "runs" / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["skipped_source_count"], 1)
            self.assertEqual(summary["failed_source_count"], 0)

            with (tmp_path / "runs" / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows[0]), 30, "CSV column count must remain stable")
            self.assertIn("Valides Buch", {row["Title"] for row in rows})

    def test_german_stream_contributes_to_weekly_target_and_preserves_contracts(self):
        """Weekly pipeline seam: the German stream contributes editor-facing rows toward
        the 100-row target via source_contributions, while the CSV column count and
        Candidate ID (work identity) behavior stay unchanged for German candidates."""
        # Issue #764 retired Die Zeit Sachbuch-Bestenliste, the third German source.
        german_source_names = {
            "Spiegel Bestseller Sachbuch",
            "Deutscher Sachbuchpreis",
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "weekly"
            result = _run_mvp(Path("configs/current-weekly-pilot.json"), output_dir)
            self.assertEqual(result.returncode, 0, result.stderr)

            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            german_contributions = [
                contribution
                for contribution in summary["source_contributions"]
                if contribution["source_name"] in german_source_names
            ]
            self.assertEqual(len(german_contributions), 2)
            # Issue #379: assert the invariant, not a cap-derived mix. Every collected
            # German source with eligible mentions reaches the editor sheet, and no
            # source contributes more rows than it extracted. The previous absolute
            # counts tracked the 100-row cap and were rebased by both #366 and #374.
            extracted_by_source = {
                run["source_name"]: run["extracted_mention_count"]
                for run in json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
            }
            for contribution in german_contributions:
                name = contribution["source_name"]
                rows_contributed = contribution["editor_facing_contribution"]
                self.assertGreater(rows_contributed, 0, f"{name} produced no editor-facing rows")
                self.assertLessEqual(rows_contributed, extracted_by_source[name], f"{name} exceeded its extraction")
            german_total = sum(c["editor_facing_contribution"] for c in german_contributions)

            # German intake widens the pool but does not lower evidence standards.
            # editor_row_target is a floor for the shortfall warning, not a ceiling on
            # rows, so the row count may exceed it once the cap allows (#379). The row
            # count must still sit inside the configured pilot band.
            pilot_config = _load_config(REPO_ROOT / "configs" / "current-weekly-pilot.json")
            self.assertGreaterEqual(summary["editor_facing_count"], pilot_config["pilot_row_min"])
            self.assertLessEqual(summary["editor_facing_count"], pilot_config["pilot_row_max"])
            self.assertGreaterEqual(summary["editor_facing_count"], german_total)
            self.assertEqual(summary["failed_source_count"], 0)
            if summary["editor_facing_count"] < summary["editor_row_target"]:
                self.assertIn("configured source evidence", summary["shortfall_reason"])

            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            # Regression: CSV column count remains stable.
            self.assertEqual(len(rows[0]), 30, "CSV column count must remain stable")

            winner_row = next(row for row in rows if row["Title"] == "Dreihundert Männer")
            # Regression: Candidate ID uses the same normalized title|author work-identity
            # scheme as English candidates, not a German-specific format.
            # Issue #679: the 2024 winner (Ein Steinpilz für die Ewigkeit) rolled
            # to the 2026 winner; the identity scheme itself is unchanged.
            self.assertEqual(winner_row["Candidate ID"], "dreihundert männer|konstantin richter")
            # German candidates keep Russian content descriptions and English helper fields.
            self.assertTrue(any("А" <= c <= "я" or c in {"ё", "Ё"} for c in winner_row["Description"]))
            self.assertEqual(winner_row["English title"], "Three Hundred Men (machine translation)")
            # Original German title is the primary Title, not the English helper.
            self.assertNotEqual(winner_row["Title"], winner_row["English title"])

    # --- #199 config and collection tests ---

    def test_issue199_sources_are_configured_in_live_config(self):
        """Issue #199: new sources are present in the live config with correct metadata."""
        live_config_path = REPO_ROOT / "configs" / "current-weekly-pilot-live.json"
        config = _load_config(live_config_path)
        issue199_names = {
            "National Book Critics Circle Awards",
            "BookMarks (LitHub) – New Nonfiction",
            "PW Best Books 2026",
            "IndieBound Indie Next List",
        }
        sources_by_name = {source["name"]: source for source in config["sources"] if source["name"] in issue199_names}
        self.assertEqual(
            set(sources_by_name),
            issue199_names,
            "All Issue #199 sources must be present in the live config",
        )
        for source in sources_by_name.values():
            # Issue #766 retired IndieBound as a low-yield source.
            self.assertEqual(source["active"], source["name"] != "IndieBound Indie Next List")
            self.assertEqual(source["access_status"], "open")
            self.assertEqual(source["extraction_method"], "codex-extraction-replay")
            self.assertTrue(source["url"].startswith("http"))
            self.assertIn(source["family"], {"award", "media/curated list"})
            self.assertIn(source["check_frequency"], {"weekly", "monthly", "annual"})
            self.assertIn("Issue #199", source.get("notes", ""))

    def test_issue199_sources_are_configured_in_replay_config(self):
        """Issue #199: new sources are present in the replay config with fixture paths."""
        config_path = REPO_ROOT / "configs" / "current-weekly-pilot.json"
        config = _load_config(config_path)
        issue199_names = {
            "National Book Critics Circle Awards",
            "BookMarks (LitHub) – New Nonfiction",
            "PW Best Books 2026",
            "IndieBound Indie Next List",
        }
        sources_by_name = {source["name"]: source for source in config["sources"] if source["name"] in issue199_names}
        self.assertEqual(set(sources_by_name), issue199_names)
        for name, source in sources_by_name.items():
            # Issue #766 retired IndieBound as a low-yield source; its fixtures stay.
            self.assertEqual(source["active"], name != "IndieBound Indie Next List")
            self.assertEqual(source["extraction_method"], "codex-extraction-replay")
            page_fixture = str(source.get("source_page_fixture_file", ""))
            extraction_fixture = str(source.get("extraction_response_file", ""))
            self.assertTrue(
                page_fixture.endswith(".json"),
                f"{name}: source_page_fixture_file must be a .json file",
            )
            self.assertTrue(
                extraction_fixture.endswith(".json"),
                f"{name}: extraction_response_file must be a .json file",
            )
            # Fixture files must exist on disk.
            page_path = config_path.parent / page_fixture
            extraction_path = config_path.parent / extraction_fixture
            self.assertTrue(page_path.exists(), f"Missing page fixture: {page_path}")
            self.assertTrue(extraction_path.exists(), f"Missing extraction fixture: {extraction_path}")

    def test_nbcc_awards_collects_six_award_mentions(self):
        """Issue #199: NBCC awards source produces valid award-family mentions."""
        config_path = REPO_ROOT / "configs" / "current-weekly-pilot.json"
        config = _load_config(config_path)
        sources = [source for source in config["sources"] if source["name"] == "National Book Critics Circle Awards"]
        self.assertEqual(len(sources), 1)
        source = sources[0]
        self.assertEqual(source["family"], "award")
        self.assertTrue(source["active"])

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(sources)

        self.assertGreaterEqual(len(result.source_mentions), 4)
        for mention in result.source_mentions:
            self.assertEqual(mention["source_family"], "award")
            self.assertEqual(mention["signal_type"], "award")
            self.assertIn(mention["signal_strength"], {"winner", "shortlist"})
            self.assertTrue(mention["title"])
            self.assertTrue(mention["primary_authors"])
            self.assertEqual(mention["source_name"], "National Book Critics Circle Awards")
        run = result.source_runs[0]
        self.assertEqual(run["status"], "collected")
        self.assertEqual(run["debug_metadata"]["collector_kind"], "codex-extraction")
        self.assertEqual(result.failed_source_count, 0)

    def test_bookmarks_nonfiction_collects_media_curated_list_mentions(self):
        """Issue #199: BookMarks non-fiction source produces valid media/curated list mentions."""
        config_path = REPO_ROOT / "configs" / "current-weekly-pilot.json"
        config = _load_config(config_path)
        sources = [source for source in config["sources"] if source["name"] == "BookMarks (LitHub) – New Nonfiction"]
        self.assertEqual(len(sources), 1)
        source = sources[0]
        self.assertEqual(source["family"], "media/curated list")
        self.assertTrue(source["active"])

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(sources)

        self.assertGreater(len(result.source_mentions), 10)
        for mention in result.source_mentions:
            self.assertEqual(mention["source_family"], "media/curated list")
            self.assertIn(mention["signal_strength"], {"major media list", "minor mention"})
            self.assertTrue(mention["title"])
            self.assertTrue(mention["primary_authors"])
            self.assertTrue(mention["evidence_url"].startswith("https://bookmarks.reviews"))
        run = result.source_runs[0]
        self.assertEqual(run["status"], "collected")
        self.assertEqual(run["debug_metadata"]["collector_kind"], "codex-extraction")
        self.assertEqual(run["dropped_mention_count"], 0)
        self.assertEqual(result.failed_source_count, 0)

    def test_pw_best_books_collects_media_curated_list_mentions(self):
        """Issue #199: the PW Best Books source, rolled over to 2026 (Issue #996), produces valid media/curated list mentions."""
        config_path = REPO_ROOT / "configs" / "current-weekly-pilot.json"
        config = _load_config(config_path)
        sources = [source for source in config["sources"] if source["name"] == "PW Best Books 2026"]
        self.assertEqual(len(sources), 1)
        source = sources[0]
        self.assertEqual(source["family"], "media/curated list")
        self.assertTrue(source["active"])

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(sources)

        self.assertGreater(len(result.source_mentions), 8)
        for mention in result.source_mentions:
            self.assertEqual(mention["source_family"], "media/curated list")
            self.assertIn(mention["signal_strength"], {"major media list", "minor mention"})
            self.assertTrue(mention["title"])
            self.assertTrue(mention["primary_authors"])
            self.assertEqual(
                mention["evidence_url"],
                "https://best-books.publishersweekly.com/pw/best-books/2026",
            )
        run = result.source_runs[0]
        self.assertEqual(run["status"], "collected")
        self.assertEqual(run["debug_metadata"]["collector_kind"], "codex-extraction")
        self.assertEqual(result.failed_source_count, 0)

    def test_indiebound_collects_media_curated_list_mentions(self):
        """Issue #199: IndieBound Indie Next List source produces valid media/curated list mentions."""
        config_path = REPO_ROOT / "configs" / "current-weekly-pilot.json"
        config = _load_config(config_path)
        sources = [source for source in config["sources"] if source["name"] == "IndieBound Indie Next List"]
        self.assertEqual(len(sources), 1)
        source = sources[0]
        self.assertEqual(source["family"], "media/curated list")
        # Issue #766 retired the source; its collector is measured on a copy that is switched on.
        self.assertFalse(source["active"])
        sources = [{**source, "active": True}]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result = SourceCollection(config_path, tmp_path / "runs", "2026-06-10").collect(sources)

        self.assertGreater(len(result.source_mentions), 15)
        for mention in result.source_mentions:
            self.assertEqual(mention["source_family"], "media/curated list")
            self.assertIn(mention["signal_strength"], {"major media list", "minor mention"})
            self.assertTrue(mention["title"])
            self.assertTrue(mention["primary_authors"])
        run = result.source_runs[0]
        self.assertEqual(run["status"], "collected")
        self.assertEqual(run["debug_metadata"]["collector_kind"], "codex-extraction")
        self.assertEqual(result.failed_source_count, 0)

    def test_issue199_sources_contribute_to_csv_and_flows(self):
        """Issue #199: new sources flow through to editor-facing CSV and contribute to target."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "weekly"
            result = _run_mvp(Path("configs/current-weekly-pilot.json"), output_dir)
            self.assertEqual(result.returncode, 0, result.stderr)

            mentions = json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8"))
            # Issue #766 retired IndieBound; it is checked as inactive below.
            issue199_names = [
                "National Book Critics Circle Awards",
                "BookMarks (LitHub) – New Nonfiction",
                "PW Best Books 2026",
            ]
            for name in issue199_names:
                source_mentions = [mention for mention in mentions if mention.get("source_name") == name]
                self.assertGreater(
                    len(source_mentions),
                    0,
                    f"{name} must produce at least one source mention",
                )

            source_runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
            for name in issue199_names:
                run = next(run for run in source_runs if run["source_name"] == name)
                self.assertEqual(run["status"], "collected", f"{name} must be collected")
                self.assertGreater(
                    run.get("extracted_mention_count", 0),
                    0,
                    f"{name} must have extracted mentions",
                )

            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["failed_source_count"], 0)
            # Source expansion still yields a substantial weekly list after
            # same-work title/subtitle variants are deduplicated.
            self.assertGreaterEqual(summary["editor_facing_count"], 85)

            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows[0]), 30, "CSV column count must remain stable")
            self.assertGreaterEqual(len(rows), 85)

    def test_issue201_high_volume_extractions_are_metadata_only_adult_nonfiction(self):
        """Issue #201: audited high-volume fixtures avoid obvious leakage and
        copied long-form text while preserving honest source-run filtering."""
        audited = {
            "USA TODAY Bestselling Books Nonfiction": "usatoday-nonfiction-bestsellers-extraction.json",
            "PEOPLE Books": "people-books-extraction.json",
            "BookPage Nonfiction": "bookpage-nonfiction-extraction.json",
            "Publishers Marketplace Rights Offerings": "publishersmarketplace-rights-extraction.json",
            "BookMarks (LitHub) – New Nonfiction": "bookmarks-nonfiction-extraction.json",
            "IndieBound Indie Next List": "indiebound-indie-next-extraction.json",
        }
        forbidden_fields = {
            "article_body",
            "page_body",
            "full_body",
            "full_description",
            "description",
            "full_review",
            "review",
            "publisher_blurb",
            "jacket_copy",
            "copied_excerpt",
            "long_excerpt",
            "book_sample",
            "sample_text",
        }
        ineligible_categories = {"fiction", "children", "childrens", "children's", "ya", "young adult"}

        for source_name, fixture_name in audited.items():
            extraction_path = REPO_ROOT / "data" / "current-weekly-pilot" / fixture_name
            extraction = json.loads(extraction_path.read_text(encoding="utf-8"))
            mentions = extraction["mentions"]
            self.assertGreater(len(mentions), 0, f"{source_name} should have audited mentions")
            seen_identities: set[tuple[str, tuple[str, ...]]] = set()
            for mention in mentions:
                self.assertFalse(forbidden_fields & set(mention), f"{source_name} has copied text fields")
                category = str(mention.get("candidate_category", "")).lower()
                category_tokens = {token.strip() for token in category.replace("/", " ").split()}
                self.assertFalse(
                    category_tokens & ineligible_categories,
                    f"{source_name} leaked ineligible category: {category}",
                )
                self.assertLessEqual(
                    len(str(mention.get("evidence_note", ""))),
                    220,
                    f"{source_name} evidence note should stay metadata-only",
                )
                identity = (
                    str(mention.get("title", "")).casefold(),
                    tuple(str(author).casefold() for author in mention.get("primary_authors", [])),
                )
                self.assertNotIn(identity, seen_identities, f"duplicate mention in {source_name}: {identity}")
                seen_identities.add(identity)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result = _run_mvp(Path("configs/current-weekly-pilot.json"), tmp_path / "weekly")
            self.assertEqual(result.returncode, 0, result.stderr)
            source_runs = json.loads((tmp_path / "weekly" / "source-runs.json").read_text(encoding="utf-8"))
            for source_name in audited:
                run = next(run for run in source_runs if run["source_name"] == source_name)
                # Issue #766 retired these two; their audited fixtures above still hold.
                if source_name in {"IndieBound Indie Next List", "Publishers Marketplace Rights Offerings"}:
                    self.assertEqual(run["status"], "inactive")
                    continue
                self.assertEqual(run["status"], "collected")
                self.assertIn("dropped_mention_count", run)
                self.assertGreaterEqual(run["extracted_mention_count"], run["valid_mention_count"])


class CodexExtractionWorklistTests(unittest.TestCase):
    """Tests for Issue #192: Prepare Codex extraction worklists."""

    def test_capture_redacts_structural_security_markers_but_preserves_metadata(self):
        """Capture sanitization removes embedded security values without
        damaging book metadata, evidence URLs, prose, or boolean flags."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "codex-out"
            config_path = tmp_path / "sources.json"
            page = tmp_path / "page.json"
            page.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/nonfiction",
                        "html": """
                            <META NAME="CSRF-TOKEN" CONTENT="csrf-secret-value">
                            <meta name="csrf-param" content="authenticity_token">
                            <INPUT TYPE="hidden" NAME="CSRFToken" VALUE="input-secret-value">
                            <input type="hidden" name="ordinary_field" value="Token Economy">
                            <script>
                              window.Mntl.csrfInit('mntl-secret-value', false);
                              googletag.pubads().setTargeting("grsession", "session-identifier");
                              </script>
                            <script src="/analytics.js?apiKey=query-public-key"></script>
                            <script>e.config.apiKey="config-public-key"; e.config.apiKey=unquoted-public-key; password=!1;</script>
                            <a href="https://example.test/books/token-economy">Token Economy</a>
                            <span>Original editorial prose remains available for extraction.</span>
                        """,
                    }
                ),
                encoding="utf-8",
            )
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-07-04",
                        "sources": [
                            {
                                "name": "Structural Security Test Source",
                                "family": "media/curated list",
                                "url": "https://example.test/nonfiction",
                                "access_status": "open",
                                "language": "en",
                                "extraction_method": "agent-extraction-live",
                                "source_page_fixture_file": str(page),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            worklist = prepare_codex_extraction_worklist(config_path, output_dir, "2026-07-04")
            capture_path = output_dir / worklist.work_items[0]["capture_file"]
            captured_html = json.loads(capture_path.read_text(encoding="utf-8"))["html"]

            for secret_value in (
                "csrf-secret-value",
                "input-secret-value",
                "mntl-secret-value",
                "session-identifier",
                "query-public-key",
                "config-public-key",
                "unquoted-public-key",
            ):
                self.assertNotIn(secret_value, captured_html)
            captured_html_casefolded = captured_html.casefold()
            self.assertIn('name="csrf-token"', captured_html_casefolded)
            self.assertIn('name="csrftoken"', captured_html_casefolded)
            self.assertIn("Token Economy", captured_html)
            self.assertIn("https://example.test/books/token-economy", captured_html)
            self.assertIn("Original editorial prose remains available for extraction.", captured_html)
            self.assertIn("password=!1", captured_html)
            self.assertIn("REDACTED", captured_html)

    def test_produces_worklist_and_captures_for_active_discovery_sources_with_fixtures(self):
        """Active discovery sources with fixture page data produce capture files and
        worklist items without making network calls."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "codex-out"
            config_path = tmp_path / "sources.json"

            page_a = tmp_path / "page-a.json"
            page_a.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list-a",
                        "html": "<a href='/book-a'>Book A</a>",
                    }
                ),
                encoding="utf-8",
            )
            page_b = tmp_path / "page-b.json"
            page_b.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list-b",
                        "html": "<a href='/book-b'>Book B</a>",
                    }
                ),
                encoding="utf-8",
            )

            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-07-04",
                        "sources": [
                            {
                                "name": "Source A",
                                "family": "award",
                                "url": "https://example.test/list-a",
                                "access_status": "open",
                                "extraction_method": "agent-extraction-live",
                                "check_frequency": "weekly",
                                "language": "en",
                                "source_page_fixture_file": str(page_a),
                            },
                            {
                                "name": "Source B",
                                "family": "media/curated list",
                                "url": "https://example.test/list-b",
                                "access_status": "open",
                                "extraction_method": "agent-extraction-live",
                                "check_frequency": "weekly",
                                "language": "en",
                                "source_page_fixture_file": str(page_b),
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            worklist = prepare_codex_extraction_worklist(config_path, output_dir, "2026-07-04")

            self.assertEqual(worklist.captured_source_count, 2)
            self.assertEqual(worklist.skipped_source_count, 0)
            self.assertEqual(worklist.failed_source_count, 0)
            self.assertEqual(len(worklist.work_items), 2)
            self.assertEqual(len(worklist.source_runs), 2)

            self.assertEqual(worklist.work_items[0]["source_name"], "Source A")
            self.assertEqual(worklist.work_items[0]["source_family"], "award")
            self.assertEqual(worklist.work_items[0]["extraction_method"], "codex")
            self.assertIn("capture_file", worklist.work_items[0])
            self.assertIn("source_metadata", worklist.work_items[0])

            # Worklist must not include secrets or API credentials.
            worklist_json = json.dumps(worklist.work_items, ensure_ascii=False)
            self.assertNotIn("api_key", worklist_json.lower())
            self.assertNotIn("AUTOSCOUT_AGENT_EXTRACTION_API_KEY", worklist_json)

            # Capture files exist and contain expected data.
            capture_a = output_dir / worklist.work_items[0]["capture_file"]
            capture_b = output_dir / worklist.work_items[1]["capture_file"]
            self.assertTrue(capture_a.exists())
            self.assertTrue(capture_b.exists())

            captured_a = json.loads(capture_a.read_text(encoding="utf-8"))
            self.assertEqual(captured_a["collection_mode"], "codex-capture")
            self.assertIn("html", captured_a)
            self.assertIn("<a href='/book-a'>Book A</a>", json.dumps(captured_a))

            # Source runs report codex-captured status with capture metadata.
            for run in worklist.source_runs:
                self.assertEqual(run["status"], "codex-captured")
                # Top-level collection_mode must reflect codex capture so
                # run-health and reporting can distinguish this mode.
                self.assertEqual(run["collection_mode"], "codex-capture")
                self.assertEqual(run["debug_metadata"]["collection_mode"], "codex-capture")
                self.assertIn("capture_file", run["debug_metadata"])

    def test_skipped_inactive_and_non_discovery_sources_do_not_become_work_items(self):
        """Skipped, inactive, and non-discovery source families are recorded
        as source runs but never become Codex worklist items."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "codex-out"
            config_path = tmp_path / "sources.json"

            page = tmp_path / "page.json"
            page.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": "<a href='/book'>Book</a>",
                    }
                ),
                encoding="utf-8",
            )

            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-07-04",
                        "sources": [
                            {
                                "name": "Paid Source",
                                "family": "award",
                                "url": "https://paid.test",
                                "access_status": "paid",
                                "language": "en",
                                "extraction_method": "fixture",
                                "source_page_fixture_file": str(page),
                            },
                            {
                                "name": "Inactive Source",
                                "family": "award",
                                "url": "https://inactive.test",
                                "active": False,
                                "access_status": "open",
                                "language": "en",
                                "extraction_method": "fixture",
                                "source_page_fixture_file": str(page),
                            },
                            {
                                "name": "Topic Search",
                                "family": "topic search",
                                "url": "https://search.test",
                                "access_status": "open",
                                "language": "en",
                                "extraction_method": "fixture",
                                "source_page_fixture_file": str(page),
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            worklist = prepare_codex_extraction_worklist(config_path, output_dir, "2026-07-04")

            self.assertEqual(worklist.captured_source_count, 0)
            self.assertEqual(worklist.skipped_source_count, 1)
            self.assertEqual(worklist.failed_source_count, 0)
            self.assertEqual(len(worklist.work_items), 0)
            self.assertEqual(len(worklist.source_runs), 3)

            statuses = {run["status"] for run in worklist.source_runs}
            self.assertEqual(statuses, {"skipped", "inactive", "ignored"})

    def test_blocked_and_failing_sources_degrade_to_source_health_records(self):
        """Blocked, CAPTCHA-like, and paid sources degrade to source-health
        records without worklist items."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "codex-out"
            config_path = tmp_path / "sources.json"

            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-07-04",
                        "sources": [
                            {
                                "name": "Blocked Source",
                                "family": "award",
                                "url": "https://blocked.test",
                                "access_status": "blocked",
                                "language": "en",
                                "extraction_method": "fixture",
                                "notes": "CAPTCHA wall prevents automated access.",
                            },
                            {
                                "name": "Fragile Source",
                                "family": "media/curated list",
                                "url": "https://fragile.test",
                                "access_status": "fragile",
                                "language": "en",
                                "extraction_method": "fixture",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            worklist = prepare_codex_extraction_worklist(config_path, output_dir, "2026-07-04")

            self.assertEqual(worklist.captured_source_count, 0)
            self.assertEqual(worklist.skipped_source_count, 2)
            self.assertEqual(worklist.failed_source_count, 0)
            self.assertEqual(len(worklist.work_items), 0)
            self.assertEqual(len(worklist.source_runs), 2)
            for run in worklist.source_runs:
                self.assertEqual(run["status"], "skipped")
                self.assertEqual(run["collection_mode"], "codex-capture")

    def test_recaptcha_script_assets_do_not_make_source_page_blocked(self):
        """Ordinary source pages may load reCAPTCHA assets without being a
        CAPTCHA wall; only challenge copy should degrade source health."""
        ordinary_page = """
            <html><head>
              <script src="https://www.google.com/recaptcha/api.js"></script>
            </head><body><h1>National Book Awards 2025 Nonfiction</h1></body></html>
        """
        challenge_page = """
            <html><body>Please verify you are human and complete the CAPTCHA to continue.</body></html>
        """

        self.assertFalse(source_collectors.likely_blocked_response(ordinary_page))
        self.assertTrue(source_collectors.likely_blocked_response(challenge_page))

    def test_captcha_strings_in_embedded_script_config_do_not_trigger_blocked(self):
        """Issue #787: Squarespace embeds form-validation strings in a script
        block on every content page.  They are configuration, not a challenge
        shown to the reader; the same copy in the page body still is."""
        squarespace_page = """
            <html><head>
              <script data-name="static-context">Static.SQUARESPACE_CONTEXT = {"captchaSettings":
              {"enabledForDonations":false},"captchaEnterpriseContext":{"recaptchaEnterpriseSiteKey":"x"},
              "missingRequiredQuestion":"Missing a required question.",
              "captchaFailure":"Captcha validation failed. Please try again."};</script>
              <style>.captcha-required { display: none }</style>
            </head><body><h1>Independent Press Top 40 Nonfiction</h1>
              <a href="/s/independentpresstop40nonfiction-1.csv">Download</a></body></html>
        """
        visible_challenge = """
            <html><head><script>var settings = {"theme": "light"};</script></head>
            <body><p>Captcha validation required to continue.</p></body></html>
        """
        unterminated_script = '<html><body><h1>List</h1><script>var m = "captcha required";'

        self.assertFalse(source_collectors.likely_blocked_response(squarespace_page))
        self.assertTrue(source_collectors.likely_blocked_response(visible_challenge))
        self.assertFalse(source_collectors.likely_blocked_response(unterminated_script))

    def test_benign_data_sitekey_form_does_not_trigger_blocked(self):
        """A page with a passive data-sitekey attribute on a form element
        is not a CAPTCHA wall — only real interstitials should degrade
        source health.  data-sitekey alone, without a widget container class,
        is common on ordinary forms."""
        form_page = """
            <html><body>
              <h1>Newsletter Signup</h1>
              <form>
                <input type="hidden" data-sitekey="6Lc_publickey">
                <input type="email" placeholder="you@example.test">
                <button>Subscribe</button>
              </form>
            </body></html>
        """
        self.assertFalse(source_collectors.likely_blocked_response(form_page))

    def test_benign_cloudflare_challenge_platform_script_does_not_trigger_blocked(self):
        """A page that loads Cloudflare's challenge-platform script (present
        on many Cloudflare-proxied sites) is not a CAPTCHA wall."""
        cloudflare_page = """
            <html><head>
              <script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>
            </head><body><h1>Publishers Marketplace</h1></body></html>
        """
        self.assertFalse(source_collectors.likely_blocked_response(cloudflare_page))

    def test_interstitial_widget_markers_without_captcha_text_still_blocked(self):
        """A real hCaptcha/Cloudflare challenge interstitial where the widget
        mark-up and challenge text are in separate HTML elements should still
        be detected as blocked."""
        # Cloudflare challenge interstitial: cf-challenge class present even
        # when the "verify you are human" text is in a different element.
        cf_interstitial = """
            <html><body>
              <div class="cf-challenge">
                <span>Just a moment...</span>
              </div>
              <div id="challenge-form">
                <div class="h-captcha" data-sitekey="..."></div>
              </div>
            </body></html>
        """
        self.assertTrue(source_collectors.likely_blocked_response(cf_interstitial))

        # hCaptcha interstitial: widget class present, challenge text nearby.
        hcaptcha_interstitial = """
            <html><body>
              <p>Please complete the security check to access the site.</p>
              <div class="h-captcha" data-sitekey="..."></div>
            </body></html>
        """
        self.assertTrue(source_collectors.likely_blocked_response(hcaptcha_interstitial))

    def test_current_weekly_live_config_classifies_issue_198_sources(self):
        """Issue #198 source-health decisions are encoded in the live config
        without widening beyond configured-source capture."""
        config = _load_config(REPO_ROOT / "configs/current-weekly-pilot-live.json")
        sources = {source["name"]: source for source in config["sources"]}

        self.assertEqual(
            sources["Audiobooks.com Nonfiction Audiobook Bestsellers"]["url"],
            "https://www.audiobooks.com/browse/non-fiction",
        )
        self.assertNotIn("Libro.fm Nonfiction Audiobook Bestsellers", sources)
        self.assertEqual(
            sources["Spiegel Bestseller Sachbuch"]["url"],
            "https://www.spiegel.de/kultur/literatur/spiegel-bestseller-hardcover-a-1025428.html",
        )
        self.assertEqual(
            sources["Die Zeit Sachbuch-Bestenliste"]["url"],
            "https://www.zeit.de/feuilleton/literatur/2026-06/sachbuch-bestenliste-juli-2026-literatur-buecher",
        )

        for source_name in (
            "National Book Awards 2026 Nonfiction",
            "Audiobooks.com Nonfiction Audiobook Bestsellers",
            "Spiegel Bestseller Sachbuch",
        ):
            source = sources[source_name]
            self.assertTrue(source["active"])
            self.assertEqual(source["access_status"], "open")
            self.assertEqual(source["extraction_method"], "codex-extraction-replay")
            self.assertTrue(
                "Issue #198" in source.get("notes", "") or "Issue #308" in source.get("notes", ""),
                f"Expected issue reference in notes: {source['notes']}",
            )

        # Issue #764 retired Die Zeit: the site refuses the collector.
        zeit = sources["Die Zeit Sachbuch-Bestenliste"]
        self.assertFalse(zeit["active"])

        # Issue #826 retired the AP News article: one news story, not a list.
        ap_article = sources["Current Weekly Nonfiction Agent Extraction"]
        self.assertFalse(ap_article["active"])
        self.assertEqual(ap_article["lifecycle_status"], "retired")
        self.assertEqual(zeit["lifecycle_status"], "retired")
        self.assertEqual(zeit["extraction_method"], "codex-extraction-replay")

    def test_issue_199_expansion_sources_are_configured_for_live_and_replay(self):
        """Issue #199 source expansion stays configured-source-only and has
        deterministic replay fixtures for contribution checks."""
        live_config = _load_config(REPO_ROOT / "configs/current-weekly-pilot-live.json")
        replay_config = _load_config(REPO_ROOT / "configs/current-weekly-pilot.json")
        live_sources = {source["name"]: source for source in live_config["sources"]}
        replay_sources = {source["name"]: source for source in replay_config["sources"]}
        expected = {
            "National Book Critics Circle Awards": ("award", "annual"),
            "BookMarks (LitHub) – New Nonfiction": ("media/curated list", "weekly"),
            "PW Best Books 2026": ("media/curated list", "annual"),
            "IndieBound Indie Next List": ("media/curated list", "monthly"),
        }

        for source_name, (family, frequency) in expected.items():
            self.assertIn(source_name, live_sources)
            self.assertIn(source_name, replay_sources)
            for source in (live_sources[source_name], replay_sources[source_name]):
                # Issue #766 retired IndieBound as a low-yield source; its fixtures stay.
                self.assertEqual(source["active"], source_name != "IndieBound Indie Next List")
                self.assertEqual(source["access_status"], "open")
                self.assertEqual(source["family"], family)
                self.assertEqual(source["check_frequency"], frequency)
                self.assertEqual(source["extraction_method"], "codex-extraction-replay")
                self.assertIn("Issue #199", source["notes"])
                self.assertIn("metadata", source["notes"].lower())
            for fixture_key in ("source_page_fixture_file", "extraction_response_file"):
                fixture_path = REPO_ROOT / "configs" / replay_sources[source_name][fixture_key]
                self.assertTrue(fixture_path.exists(), f"missing {fixture_key} for {source_name}")

    def test_captured_pages_are_output_dir_relative(self):
        """Capture files live inside the output directory, relative to it."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "codex-out"
            config_path = tmp_path / "sources.json"

            page = tmp_path / "page.json"
            page.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": "<a href='/book'>Book</a>",
                    }
                ),
                encoding="utf-8",
            )

            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-07-04",
                        "sources": [
                            {
                                "name": "Discovery Source",
                                "family": "award",
                                "url": "https://example.test/list",
                                "access_status": "open",
                                "language": "en",
                                "extraction_method": "agent-extraction-live",
                                "source_page_fixture_file": str(page),
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            worklist = prepare_codex_extraction_worklist(config_path, output_dir, "2026-07-04")

            capture_file = worklist.work_items[0]["capture_file"]
            # The capture_file in the worklist is just a filename, not an absolute path.
            self.assertFalse(capture_file.startswith("/"))
            self.assertFalse(str(output_dir) in capture_file, "capture_file in worklist must be relative to output_dir")
            # But the capture actually lives under output_dir.
            self.assertTrue((output_dir / capture_file).exists())

    def test_worklist_item_contains_bounded_source_evidence_for_codex(self):
        """Each worklist item includes source metadata and a capture file reference
        sufficient for Codex to produce Source Mentions."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "codex-out"
            config_path = tmp_path / "sources.json"

            page = tmp_path / "page.json"
            page.write_text(
                json.dumps(
                    {
                        "source_page": "https://realsite.test/nonfiction",
                        "html": "<a href='/books/important-work'>Important Work</a> by A. Author",
                    }
                ),
                encoding="utf-8",
            )

            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-07-04",
                        "sources": [
                            {
                                "name": "Real Nonfiction List",
                                "family": "media/curated list",
                                "url": "https://realsite.test/nonfiction",
                                "access_status": "open",
                                "extraction_method": "agent-extraction-live",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "This site lists nonfiction bestsellers weekly.",
                                "source_page_fixture_file": str(page),
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            worklist = prepare_codex_extraction_worklist(config_path, output_dir, "2026-07-04")

            item = worklist.work_items[0]
            # Essential source identity fields.
            self.assertEqual(item["source_name"], "Real Nonfiction List")
            self.assertEqual(item["source_family"], "media/curated list")
            self.assertEqual(item["source_url"], "https://realsite.test/nonfiction")
            self.assertEqual(item["extraction_method"], "codex")

            # Source metadata for Codex context.
            self.assertEqual(item["source_metadata"]["access_status"], "open")
            self.assertEqual(item["source_metadata"]["check_frequency"], "weekly")
            self.assertIn("nonfiction", item["source_metadata"]["notes"])

            # The capture file in the worklist is a relative filename.
            self.assertTrue(item["capture_file"].endswith(".json"))
            self.assertIn("real-nonfiction-list", item["capture_file"])

            # The capture file on disk contains the HTML.
            capture_path = output_dir / item["capture_file"]
            captured = json.loads(capture_path.read_text(encoding="utf-8"))
            self.assertIn("Important Work", captured["html"])
            self.assertEqual(captured["source_url"], "https://realsite.test/nonfiction")

            # Worklist and capture must not contain secrets.
            worklist_json = json.dumps(worklist.work_items, ensure_ascii=False)
            capture_json = json.dumps(captured, ensure_ascii=False)
            for secret_marker in ("api_key", "credential", "token", "secret", "password"):
                self.assertNotIn(secret_marker, worklist_json.lower())
                self.assertNotIn(secret_marker, capture_json.lower())

    def test_cli_command_produces_expected_artifacts(self):
        """The prepare-codex-extraction CLI command writes worklist and source-runs
        artifacts, and reports captured/skipped/failed counts."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "codex-out"
            config_path = tmp_path / "sources.json"

            page = tmp_path / "page.json"
            page.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": "<a href='/book'>Book</a>",
                    }
                ),
                encoding="utf-8",
            )

            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-07-04",
                        "sources": [
                            {
                                "name": "Good Source",
                                "family": "award",
                                "url": "https://example.test/list",
                                "access_status": "open",
                                "language": "en",
                                "extraction_method": "agent-extraction-live",
                                "source_page_fixture_file": str(page),
                            },
                            {
                                "name": "Paid Source",
                                "family": "rights/deal",
                                "url": "https://paid.test",
                                "access_status": "paid",
                                "language": "en",
                                "extraction_method": "fixture",
                            },
                            {
                                "name": "Disabled Source",
                                "family": "award",
                                "url": "https://off.test",
                                "active": False,
                                "access_status": "open",
                                "language": "en",
                                "extraction_method": "fixture",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            captured = io.StringIO()
            with redirect_stdout(captured):
                prepare_codex_extraction(config_path, output_dir)
            result = _IPCResult(0, stdout=captured.getvalue())
            self.assertIn("captured_source_count=1", result.stdout)
            self.assertIn("skipped_source_count=1", result.stdout)
            self.assertIn("failed_source_count=0", result.stdout)
            self.assertIn("worklist_items=1", result.stdout)
            self.assertIn("collection_mode=codex-capture", result.stdout)

            # Verify artifacts exist.
            worklist_path = output_dir / "codex-extraction-worklist.json"
            source_runs_path = output_dir / "source-runs.json"
            self.assertTrue(worklist_path.exists())
            self.assertTrue(source_runs_path.exists())

            worklist = json.loads(worklist_path.read_text(encoding="utf-8"))
            self.assertEqual(worklist["run_date"], "2026-07-04")
            self.assertEqual(len(worklist["work_items"]), 1)
            self.assertEqual(worklist["work_items"][0]["source_name"], "Good Source")

            source_runs = json.loads(source_runs_path.read_text(encoding="utf-8"))
            self.assertEqual(len(source_runs), 3)
            statuses = {run["status"] for run in source_runs}
            self.assertEqual(statuses, {"codex-captured", "skipped", "inactive"})

    def test_worklist_preserves_configured_source_only_discovery(self):
        """The worklist only contains configured discovery sources.
        Non-discovery source families (e.g., topic search) do not become work items."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "codex-out"
            config_path = tmp_path / "sources.json"

            page = tmp_path / "page.json"
            page.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": "<a href='/book'>Book</a>",
                    }
                ),
                encoding="utf-8",
            )

            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-07-04",
                        "sources": [
                            {
                                "name": "Discovery Award",
                                "family": "award",
                                "url": "https://example.test/award",
                                "access_status": "open",
                                "language": "en",
                                "extraction_method": "agent-extraction-live",
                                "source_page_fixture_file": str(page),
                            },
                            {
                                "name": "Not Discovery",
                                "family": "topic search",
                                "url": "https://search.test",
                                "access_status": "open",
                                "language": "en",
                                "extraction_method": "fixture",
                                "source_page_fixture_file": str(page),
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            worklist = prepare_codex_extraction_worklist(config_path, output_dir, "2026-07-04")

            self.assertEqual(len(worklist.work_items), 1)
            self.assertEqual(worklist.work_items[0]["source_name"], "Discovery Award")

            not_discovery_run = next(r for r in worklist.source_runs if r["source_name"] == "Not Discovery")
            self.assertEqual(not_discovery_run["status"], "ignored")

    def test_worklist_includes_expected_extraction_file(self):
        """Each worklist item includes an expected_extraction_file field so
        finalize-codex-run can map work items to their extraction artifacts."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "codex-out"
            config_path = tmp_path / "sources.json"

            page = tmp_path / "page.json"
            page.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": "<a href='/books/expected'>Expected Book</a>",
                    }
                ),
                encoding="utf-8",
            )

            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-07-04",
                        "sources": [
                            {
                                "name": "Expected Source",
                                "family": "media/curated list",
                                "url": "https://example.test/list",
                                "access_status": "open",
                                "extraction_method": "agent-extraction-live",
                                "check_frequency": "weekly",
                                "language": "en",
                                "notes": "Test.",
                                "source_page_fixture_file": str(page),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            worklist = prepare_codex_extraction_worklist(config_path, output_dir, "2026-07-04")

            item = worklist.work_items[0]
            self.assertIn("expected_extraction_file", item)
            expected = item["expected_extraction_file"]
            self.assertTrue(expected.endswith(".json"))
            self.assertIn("expected-source", expected)
            self.assertIn("codex-extraction", expected)

    def test_codex_extraction_slug_strips_url_whitespace(self):
        """URL whitespace is stripped before hashing so extraction slugs are
        deterministic regardless of trailing spaces in configuration."""
        from autoscout.source_collection import codex_extraction_slug

        slug_clean = codex_extraction_slug("Test", "https://example.test/page")
        slug_trailing = codex_extraction_slug("Test", "https://example.test/page  ")
        self.assertEqual(slug_clean, slug_trailing, "trailing whitespace should produce the same slug")

        slug_leading = codex_extraction_slug("Test", "  https://example.test/page")
        self.assertEqual(slug_clean, slug_leading, "leading whitespace should produce the same slug")

    def test_codex_capture_filename_sanitizes_source_name(self):
        """Capture filenames sanitize the source name to a safe filesystem slug."""
        test_sources = [
            ("Simple Name", "codex-capture-simple-name.json"),
            ("Source with Spaces & Special Chars!", "codex-capture-source-with-spaces---special-chars.json"),
            ("  Leading/Trailing  ", "codex-capture-leading-trailing.json"),
        ]
        for source_name, expected_filename in test_sources:
            filename = codex_capture_filename({"name": source_name})
            self.assertEqual(filename, expected_filename)

    def test_codex_capture_filename_avoids_collisions_on_same_name_different_url(self):
        """Sources with identical names but different URLs produce distinct
        capture filenames to prevent overwrites."""
        source_a = {"name": "Best Nonfiction", "url": "https://site-a.test/list"}
        source_b = {"name": "Best Nonfiction", "url": "https://site-b.test/list"}
        filename_a = codex_capture_filename(source_a)
        filename_b = codex_capture_filename(source_b)
        self.assertNotEqual(filename_a, filename_b, "same name + different URL must produce distinct filenames")
        self.assertIn("best-nonfiction", filename_a)
        self.assertIn("best-nonfiction", filename_b)

    def test_codex_capture_filename_includes_url_hash_when_url_present(self):
        """When a URL is configured, the filename includes a short URL hash for
        uniqueness across same-named sources."""
        filename = codex_capture_filename(
            {
                "name": "Weekly List",
                "url": "https://example.test/weekly",
            }
        )
        # Should contain both the slug and a hash segment.
        self.assertIn("weekly-list", filename)
        parts = filename.removesuffix(".json").split("-")
        # Last segment is the 8-char hex hash.
        hash_segment = parts[-1]
        self.assertEqual(len(hash_segment), 8, f"expected 8-char hex hash, got '{hash_segment}'")
        self.assertTrue(all(c in "0123456789abcdef" for c in hash_segment), f"expected hex hash, got '{hash_segment}'")

    def test_http_403_fetch_failure_degrades_to_skipped_source_health(self):
        """A live HTTP 403 response degrades to a skipped source-health record
        through the Codex worklist path."""
        from unittest.mock import patch

        from autoscout.source_collectors import CollectorRunError

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "codex-out"
            config_path = tmp_path / "sources.json"

            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-07-04",
                        "sources": [
                            {
                                "name": "HTTP 403 Source",
                                "family": "award",
                                "url": "https://forbidden.test",
                                "access_status": "open",
                                "extraction_method": "agent-extraction-live",
                                "check_frequency": "weekly",
                                "language": "en",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with patch("autoscout.source_collection.codex_source_page") as mock_fetch:
                mock_fetch.side_effect = CollectorRunError(
                    "Agent extraction live collection blocked by HTTP 403",
                    status="skipped",
                    debug_metadata={"http_status": "403", "collector_kind": "agent-extraction"},
                )
                worklist = prepare_codex_extraction_worklist(config_path, output_dir, "2026-07-04")

            self.assertEqual(worklist.skipped_source_count, 1)
            self.assertEqual(worklist.failed_source_count, 0)
            self.assertEqual(worklist.captured_source_count, 0)
            self.assertEqual(len(worklist.work_items), 0)
            self.assertEqual(len(worklist.source_runs), 1)
            run = worklist.source_runs[0]
            self.assertEqual(run["status"], "skipped")
            self.assertIn("HTTP 403", run["reason"])
            self.assertEqual(run["collection_mode"], "codex-capture")
            self.assertEqual(run["debug_metadata"]["http_status"], "403")

    def test_http_404_fetch_failure_degrades_to_failed_source_health(self):
        """A live HTTP 404 fetch failure degrades to a failed source-health record
        through the Codex worklist path."""
        from unittest.mock import patch

        from autoscout.source_collectors import CollectorRunError

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "codex-out"
            config_path = tmp_path / "sources.json"

            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-07-04",
                        "sources": [
                            {
                                "name": "HTTP 404 Source",
                                "family": "media/curated list",
                                "url": "https://notfound.test",
                                "access_status": "open",
                                "extraction_method": "agent-extraction-live",
                                "check_frequency": "weekly",
                                "language": "en",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with patch("autoscout.source_collection.codex_source_page") as mock_fetch:
                mock_fetch.side_effect = CollectorRunError(
                    "Agent extraction live collection failed: HTTP 404",
                    status="failed",
                    debug_metadata={"http_status": "404", "collector_kind": "agent-extraction"},
                )
                worklist = prepare_codex_extraction_worklist(config_path, output_dir, "2026-07-04")

            self.assertEqual(worklist.skipped_source_count, 0)
            self.assertEqual(worklist.failed_source_count, 1)
            self.assertEqual(worklist.captured_source_count, 0)
            self.assertEqual(len(worklist.work_items), 0)
            self.assertEqual(len(worklist.source_runs), 1)
            run = worklist.source_runs[0]
            self.assertEqual(run["status"], "failed")
            self.assertIn("HTTP 404", run["reason"])
            self.assertEqual(run["collection_mode"], "codex-capture")
            self.assertEqual(run["debug_metadata"]["http_status"], "404")

    def test_generic_fetch_failure_degrades_to_failed_source_health(self):
        """A generic URLError/timeout fetch failure degrades to a failed
        source-health record through the Codex worklist path."""
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "codex-out"
            config_path = tmp_path / "sources.json"

            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-07-04",
                        "sources": [
                            {
                                "name": "Unreachable Source",
                                "family": "award",
                                "url": "https://unreachable.test",
                                "access_status": "open",
                                "extraction_method": "agent-extraction-live",
                                "check_frequency": "weekly",
                                "language": "en",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with patch("autoscout.source_collection.codex_source_page") as mock_fetch:
                mock_fetch.side_effect = OSError("nodename nor servname provided, or not known")
                worklist = prepare_codex_extraction_worklist(config_path, output_dir, "2026-07-04")

            self.assertEqual(worklist.skipped_source_count, 0)
            self.assertEqual(worklist.failed_source_count, 1)
            self.assertEqual(worklist.captured_source_count, 0)
            self.assertEqual(len(worklist.work_items), 0)
            self.assertEqual(len(worklist.source_runs), 1)
            run = worklist.source_runs[0]
            self.assertEqual(run["status"], "failed")
            self.assertIn("nodename", run["reason"])
            self.assertEqual(run["collection_mode"], "codex-capture")

    def test_api_backed_transport_not_invoked_in_codex_worklist_path(self):
        """Regression: the API-backed real transport is never invoked during
        Codex capture/worklist preparation, even for real-backend sources."""
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "codex-out"
            config_path = tmp_path / "sources.json"

            page = tmp_path / "page.json"
            page.write_text(
                json.dumps(
                    {
                        "source_page": "https://realsite.test/list",
                        "html": "<a href='/book'>Book</a>",
                    }
                ),
                encoding="utf-8",
            )

            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-07-04",
                        "sources": [
                            {
                                "name": "Real Backend Source",
                                "family": "media/curated list",
                                "url": "https://realsite.test/list",
                                "access_status": "open",
                                "extraction_method": "agent-extraction-live",
                                "check_frequency": "weekly",
                                "language": "en",
                                # The source is configured with a real backend
                                # and real credentials, but the Codex path must
                                # never touch the API transport.
                                "extraction_backend": "real",
                                "enable_real_backend": True,
                                "credential_env": "AUTOSCOUT_AGENT_EXTRACTION_API_KEY",
                                "source_page_fixture_file": str(page),
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with patch("autoscout.source_collectors.RealAgentExtractionBackend.extract") as mock_extract:
                worklist = prepare_codex_extraction_worklist(config_path, output_dir, "2026-07-04")

            # The API backend's extract method must never be called.
            mock_extract.assert_not_called()

            # The worklist still processes the source correctly via fixture replay.
            self.assertEqual(worklist.captured_source_count, 1)
            self.assertEqual(len(worklist.work_items), 1)
            self.assertEqual(worklist.work_items[0]["source_name"], "Real Backend Source")

            # Verify capture was written with codex-capture mode, not real-live.
            capture_path = output_dir / worklist.work_items[0]["capture_file"]
            captured = json.loads(capture_path.read_text(encoding="utf-8"))
            self.assertEqual(captured["collection_mode"], "codex-capture")

            # Source run must report codex-capture, not real-live.
            run = worklist.source_runs[0]
            self.assertEqual(run["collection_mode"], "codex-capture")


class CodexExtractionIngestTests(unittest.TestCase):
    """Tests for Issue #193: Ingest Codex extraction artifacts."""

    def test_ingest_valid_codex_extraction_produces_source_mentions(self):
        """A source with extraction_method: codex-extraction-replay and valid
        capture + extraction artifacts produces validated Source Mentions."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/nonfiction",
                        "html": "<a href='/books/good-work'>Good Work</a> by A. Author",
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
                                "title": "Good Work",
                                "primary_authors": ["A. Author"],
                                "source_url": "https://example.test/books/good-work",
                                "evidence_url": "https://example.test/books/good-work",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Listed on the configured Codex-scouted nonfiction page.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = codex_extraction_collector_result(
                config_path,
                tmp_path / "runs",
                {
                    "name": "Codex Source",
                    "family": "media/curated list",
                    "url": "https://example.test/nonfiction",
                    "access_status": "open",
                    "language": "en",
                    "extraction_method": "codex-extraction-replay",
                    "source_page_fixture_file": str(capture_path),
                    "extraction_response_file": str(extraction_path),
                },
                "2026-07-04",
            )

            self.assertEqual(len(result.mentions), 1)
            mention = result.mentions[0]
            self.assertEqual(mention["title"], "Good Work")
            self.assertEqual(mention["primary_authors"], ["A. Author"])
            self.assertEqual(result.debug_metadata["collector_kind"], "codex-extraction")
            self.assertEqual(result.debug_metadata["validated_mention_count"], 1)
            self.assertEqual(result.debug_metadata["dropped_mention_count"], 0)

    def test_ingest_preserves_validation_guardrails(self):
        """Codex extraction artifacts undergo the same validation as agent
        extraction: forbidden copied-text fields and evidence URL provenance."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # Capture with only one book link.
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/nonfiction",
                        "html": "<a href='/books/present-work'>Present Work</a> by A. Author, B. Author",
                    }
                ),
                encoding="utf-8",
            )

            # Extraction has one valid and one dropped (unverifiable) mention.
            extraction_path = tmp_path / "extraction.json"
            extraction_path.write_text(
                json.dumps(
                    {
                        "mentions": [
                            {
                                "title": "Present Work",
                                "primary_authors": ["A. Author"],
                                "source_url": "https://example.test/books/present-work",
                                "evidence_url": "https://example.test/books/present-work",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Found on the configured page.",
                            },
                            {
                                "title": "Missing Work",
                                "primary_authors": ["B. Author"],
                                "source_url": "https://other.test/books/missing-work",
                                "evidence_url": "https://other.test/books/missing-work",
                                "signal_type": "media/curated list",
                                "signal_strength": "minor mention",
                                "evidence_note": "Not present in the captured page.",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = codex_extraction_collector_result(
                config_path,
                tmp_path / "runs",
                {
                    "name": "Codex Source",
                    "family": "media/curated list",
                    "url": "https://example.test/nonfiction",
                    "access_status": "open",
                    "language": "en",
                    "extraction_method": "codex-extraction-replay",
                    "source_page_fixture_file": str(capture_path),
                    "extraction_response_file": str(extraction_path),
                },
                "2026-07-04",
            )

            self.assertEqual(len(result.mentions), 1, "only one mention should pass evidence URL verification")
            self.assertEqual(result.mentions[0]["title"], "Present Work")
            self.assertEqual(result.skipped_item_count, 1)
            self.assertEqual(result.raw_mention_count, 2)
            self.assertEqual(result.valid_mention_count, 1)
            self.assertEqual(result.debug_metadata["dropped_mention_count"], 1)
            self.assertEqual(result.debug_metadata["drop_reasons"][0]["title"], "Missing Work")

    def test_ingest_rejects_forbidden_copied_text_fields(self):
        """Codex extraction artifacts containing forbidden copied-text fields
        (reviews, bodies, descriptions) are rejected with a CollectorRunError."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/nonfiction",
                        "html": "<a href='/books/trouble'>Trouble</a> by C. Author",
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
                                "title": "Trouble",
                                "primary_authors": ["C. Author"],
                                "source_url": "https://example.test/books/trouble",
                                "evidence_url": "https://example.test/books/trouble",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Listed on the page.",
                                "review": "This book is a masterpiece of modern nonfiction.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(CollectorRunError) as ctx:
                codex_extraction_collector_result(
                    config_path,
                    tmp_path / "runs",
                    {
                        "name": "Codex Source",
                        "family": "media/curated list",
                        "url": "https://example.test/nonfiction",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "codex-extraction-replay",
                        "source_page_fixture_file": str(capture_path),
                        "extraction_response_file": str(extraction_path),
                    },
                    "2026-07-04",
                )

            self.assertIn("forbidden", str(ctx.exception).lower())
            self.assertEqual(ctx.exception.debug_metadata.get("safe_failure_status"), "forbidden_copied_text")

    def test_ingest_empty_extraction_raises_collector_run_error(self):
        """An extraction artifact with no mentions raises a CollectorRunError
        reporting zero mentions rather than silently succeeding."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/empty",
                        "html": "<p>No books here.</p> by Jane Author",
                    }
                ),
                encoding="utf-8",
            )
            extraction_path = tmp_path / "extraction.json"
            extraction_path.write_text(json.dumps({"mentions": []}), encoding="utf-8")
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(CollectorRunError) as ctx:
                codex_extraction_collector_result(
                    config_path,
                    tmp_path / "runs",
                    {
                        "name": "Empty Source",
                        "family": "award",
                        "url": "https://example.test/empty",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "codex-extraction-replay",
                        "source_page_fixture_file": str(capture_path),
                        "extraction_response_file": str(extraction_path),
                    },
                    "2026-07-04",
                )

            self.assertIn("no mentions", str(ctx.exception).lower())
            self.assertEqual(ctx.exception.raw_mention_count, 0)

    def test_full_pipeline_from_codex_replay_to_editor_artifacts(self):
        """A complete run-mvp with codex-extraction-replay sources produces
        source-mentions, scouting-candidates, editor-facing CSV, run-summary,
        and source-runs with codex-capture collection mode."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/nonfiction",
                        "html": ("<a href='/books/discovered'>Discovered Book</a> by Jane Author"),
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
                                "title": "Discovered Book",
                                "primary_authors": ["Jane Author"],
                                "source_url": "https://example.test/books/discovered",
                                "evidence_url": "https://example.test/books/discovered",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Codex-extracted metadata-only mention from the captured configured page.",
                                "candidate_category": "history",
                                "publisher": "Test Press",
                                "publication_year": "2024",
                                "short_description": "A metadata-only description for testing.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            config_path = tmp_path / "sources.json"
            output_dir = tmp_path / "weekly-out"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-07-04",
                        "pilot_mode": True,
                        "editor_row_target": 10,
                        "pilot_row_min": 1,
                        "pilot_row_max": 50,
                        "report_qa": {"reviewer": "test", "status": "pilot", "note": "Codex ingest test."},
                        "sources": [
                            {
                                "name": "Codex Ingested Source",
                                "family": "media/curated list",
                                "url": "https://example.test/nonfiction",
                                "active": True,
                                "access_status": "open",
                                "extraction_method": "codex-extraction-replay",
                                "check_frequency": "weekly",
                                "language": "en",
                                "source_page_fixture_file": str(capture_path),
                                "extraction_response_file": str(extraction_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = _run_mvp(config_path, output_dir)
            self.assertEqual(result.returncode, 0, result.stderr)

            # Artifact files exist.
            self.assertTrue((output_dir / "source-mentions.json").exists())
            self.assertTrue((output_dir / "scouting-candidates.json").exists())
            self.assertTrue((output_dir / "editor-facing.csv").exists())
            self.assertTrue((output_dir / "run-summary.json").exists())
            self.assertTrue((output_dir / "pilot-status.json").exists())
            self.assertTrue((output_dir / "source-runs.json").exists())

            # Source mentions contain the ingested book.
            mentions = json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8"))
            self.assertEqual(len(mentions), 1)
            self.assertEqual(mentions[0]["title"], "Discovered Book")
            self.assertEqual(mentions[0]["primary_authors"], ["Jane Author"])

            # Scouting candidates are generated.
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["title"], "Discovered Book")

            # Editor-facing CSV has the candidate.
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertGreaterEqual(len(rows), 1)
            discovered_row = next(r for r in rows if r["Title"] == "Discovered Book")
            self.assertEqual(discovered_row["Author"], "Jane Author")

            # Run summary reports codex-capture mode.
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertIn("codex-capture", summary["collection_modes"])

            # Source runs report codex-capture mode.
            source_runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
            self.assertEqual(len(source_runs), 1)
            self.assertEqual(source_runs[0]["status"], "collected")
            self.assertEqual(source_runs[0]["collection_mode"], "codex-capture")
            self.assertEqual(source_runs[0]["debug_metadata"]["collector_kind"], "codex-extraction")

    def test_pipeline_replayable_without_network_or_codex(self):
        """Once capture and extraction artifacts exist, the pipeline run is
        deterministic and replayable without fetching pages or calling Codex."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/nonfiction",
                        "html": "<a href='/books/replay'>Replay Book</a> by Z. Author",
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
                                "title": "Replay Book",
                                "primary_authors": ["Z. Author"],
                                "source_url": "https://example.test/books/replay",
                                "evidence_url": "https://example.test/books/replay",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Replayable metadata-only mention.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            source_config = {
                "name": "Replay Source",
                "family": "media/curated list",
                "url": "https://example.test/nonfiction",
                "access_status": "open",
                "language": "en",
                "extraction_method": "codex-extraction-replay",
                "source_page_fixture_file": str(capture_path),
                "extraction_response_file": str(extraction_path),
            }
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            # Two runs with identical artifacts produce identical results.
            run1 = SourceCollection(config_path, tmp_path / "runs-1", "2026-07-04").collect([source_config])
            run2 = SourceCollection(config_path, tmp_path / "runs-2", "2026-07-04").collect([source_config])

            self.assertEqual(
                json.dumps(run1.source_mentions, sort_keys=True),
                json.dumps(run2.source_mentions, sort_keys=True),
            )
            run1_status = run1.source_runs[0]["status"]
            run2_status = run2.source_runs[0]["status"]
            self.assertEqual(run1_status, run2_status)
            self.assertEqual(run1_status, "collected")

    def test_collection_mode_reported_as_codex_capture_in_source_runs(self):
        """Source runs from codex-extraction-replay report collection_mode
        as codex-capture so run-health can distinguish the mode."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": "<a href='/books/a'>Book A</a> by A. Author",
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
                                "title": "Book A",
                                "primary_authors": ["A. Author"],
                                "source_url": "https://example.test/books/a",
                                "evidence_url": "https://example.test/books/a",
                                "signal_type": "award",
                                "signal_strength": "winner",
                                "evidence_note": "Codex-extracted winner.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = SourceCollection(config_path, tmp_path / "runs", "2026-07-04").collect(
                [
                    {
                        "name": "Codex Source",
                        "family": "award",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "codex-extraction-replay",
                        "source_page_fixture_file": str(capture_path),
                        "extraction_response_file": str(extraction_path),
                    }
                ]
            )

            run = result.source_runs[0]
            self.assertEqual(run["collection_mode"], "codex-capture")
            self.assertEqual(run["status"], "collected")
            self.assertEqual(run["debug_metadata"]["collector_kind"], "codex-extraction")

    def test_codex_replay_source_with_missing_capture_raises_error(self):
        """A codex-extraction-replay source without a source_page_fixture_file
        fails because evidence URLs cannot be verified against empty HTML."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Valid extraction artifact with all required fields.
            extraction_path = tmp_path / "extraction.json"
            extraction_path.write_text(
                json.dumps(
                    {
                        "mentions": [
                            {
                                "title": "Book",
                                "primary_authors": ["A. Author"],
                                "source_url": "https://example.test/books/book",
                                "evidence_url": "https://example.test/books/book",
                                "signal_type": "award",
                                "signal_strength": "winner",
                                "evidence_note": "Found on the source page.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            # No source_page_fixture_file → empty HTML in configured_source_page.
            # All evidence URLs fail verification → CollectorRunError raised.
            result = SourceCollection(config_path, tmp_path / "runs", "2026-07-04").collect(
                [
                    {
                        "name": "Missing Capture",
                        "family": "award",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "codex-extraction-replay",
                        "extraction_response_file": str(extraction_path),
                    }
                ]
            )

            self.assertEqual(result.failed_source_count, 1)
            self.assertEqual(result.source_runs[0]["status"], "failed")
            self.assertIn("dropped", result.source_runs[0]["reason"].lower())
            self.assertEqual(result.source_runs[0]["collection_mode"], "codex-capture")

    # --- Review-fix tests (gpt-5.5) ---

    def test_codex_artifact_dir_resolves_worklist_paths_from_run_output(self):
        """When codex_artifact_dir is configured, relative capture and extraction
        paths are resolved against that directory instead of config_path.parent.
        This allows direct ingestion from the #192 prepare-codex-extraction
        output without copying files."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Simulate the #192 output directory structure.
            run_output = tmp_path / "weekly-runs"
            run_output.mkdir()
            capture_path = run_output / "codex-capture-test-source-a1b2c3d4.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/nonfiction",
                        "html": "<a href='/books/worklist-book'>Worklist Book</a> by W. Author",
                    }
                ),
                encoding="utf-8",
            )
            extraction_path = run_output / "codex-extraction-test-source.json"
            extraction_path.write_text(
                json.dumps(
                    {
                        "mentions": [
                            {
                                "title": "Worklist Book",
                                "primary_authors": ["W. Author"],
                                "source_url": "https://example.test/books/worklist-book",
                                "evidence_url": "https://example.test/books/worklist-book",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Codex-extracted from the worklist capture.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            # Config is in a different directory; uses relative paths + codex_artifact_dir.
            config_dir = tmp_path / "configs"
            config_dir.mkdir()
            config_path = config_dir / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            source = {
                "name": "Worklist Source",
                "family": "media/curated list",
                "url": "https://example.test/nonfiction",
                "access_status": "open",
                "language": "en",
                "extraction_method": "codex-extraction-replay",
                # Relative paths — resolved against codex_artifact_dir, not config dir.
                "source_page_fixture_file": "codex-capture-test-source-a1b2c3d4.json",
                "extraction_response_file": "codex-extraction-test-source.json",
                "codex_artifact_dir": str(run_output),
            }

            result = codex_extraction_collector_result(config_path, run_output / "ingest", source, "2026-07-04")

            self.assertEqual(len(result.mentions), 1)
            self.assertEqual(result.mentions[0]["title"], "Worklist Book")
            self.assertEqual(result.debug_metadata["collector_kind"], "codex-extraction")

    def test_codex_artifact_dir_falls_back_when_relative_and_no_artifact_dir(self):
        """Without codex_artifact_dir, relative paths fall back to
        config_path.parent (backward-compatible)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_dir = tmp_path / "configs"
            config_dir.mkdir()
            # Place artifacts alongside the config file (config_path.parent).
            capture_path = config_dir / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/nonfiction",
                        "html": "<a href='/books/fallback-book'>Fallback Book</a> by F. Author",
                    }
                ),
                encoding="utf-8",
            )
            extraction_path = config_dir / "extraction.json"
            extraction_path.write_text(
                json.dumps(
                    {
                        "mentions": [
                            {
                                "title": "Fallback Book",
                                "primary_authors": ["F. Author"],
                                "source_url": "https://example.test/books/fallback-book",
                                "evidence_url": "https://example.test/books/fallback-book",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Fallback path test.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            config_path = config_dir / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = codex_extraction_collector_result(
                config_path,
                tmp_path / "runs",
                {
                    "name": "Fallback Source",
                    "family": "media/curated list",
                    "url": "https://example.test/nonfiction",
                    "access_status": "open",
                    "language": "en",
                    "extraction_method": "codex-extraction-replay",
                    "source_page_fixture_file": "capture.json",
                    "extraction_response_file": "extraction.json",
                },
                "2026-07-04",
            )

            self.assertEqual(len(result.mentions), 1)
            self.assertEqual(result.mentions[0]["title"], "Fallback Book")

    def test_codex_artifact_dir_dot_resolves_to_config_parent(self):
        """When codex_artifact_dir is "." (as finalize-codex-run emits),
        relative capture and extraction paths resolve against the directory
        containing the config file (config_path.parent). This catches the
        replay-config path-doubling bug where a relative run-directory path
        was resolved against itself, producing a nonexistent nested path."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Simulate a run output directory — config and artifacts live here.
            run_dir = tmp_path / "runs" / "current-weekly-pilot-russian"
            run_dir.mkdir(parents=True)

            capture_file = "codex-capture-test-source-a1b2c3d4.json"
            (run_dir / capture_file).write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/nonfiction",
                        "html": "<a href='/books/dot-book'>Dot Book</a> by D. Author, G. Author",
                    }
                ),
                encoding="utf-8",
            )

            extraction_file = "test-source-codex-extraction.json"
            (run_dir / extraction_file).write_text(
                json.dumps(
                    {
                        "mentions": [
                            {
                                "title": "Dot Book",
                                "primary_authors": ["D. Author"],
                                "source_url": "https://example.test/books/dot-book",
                                "evidence_url": "https://example.test/books/dot-book",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Dot-dir resolution test.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            # Config inside the run dir (as finalize-codex-run generates).
            config_path = run_dir / "codex-replay-config.json"
            config_path.write_text("{}", encoding="utf-8")

            source = {
                "name": "Dot Source",
                "family": "media/curated list",
                "url": "https://example.test/nonfiction",
                "access_status": "open",
                "language": "en",
                "extraction_method": "codex-extraction-replay",
                "source_page_fixture_file": capture_file,
                "extraction_response_file": extraction_file,
                "codex_artifact_dir": ".",
            }

            result = codex_extraction_collector_result(config_path, run_dir / "ingest", source, "2026-07-04")

            self.assertEqual(len(result.mentions), 1)
            self.assertEqual(result.mentions[0]["title"], "Dot Book")
            self.assertEqual(result.debug_metadata["collector_kind"], "codex-extraction")

    def test_malformed_required_fields_become_dropped_not_crash(self):
        """Codex extraction artifacts with missing required Source Mention fields
        are dropped individually as skipped records rather than crashing the
        entire run.  Partial successes (some valid mentions) still flow through."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/nonfiction",
                        "html": ("<a href='/books/good-one'>Good One</a> by G. Author, B. Author"),
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
                                # Valid mention.
                                "title": "Good One",
                                "primary_authors": ["G. Author"],
                                "source_url": "https://example.test/books/good-one",
                                "evidence_url": "https://example.test/books/good-one",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Valid Codex-extracted mention.",
                            },
                            {
                                # Malformed: missing evidence_note and signal_strength.
                                "title": "Bad Mention",
                                "primary_authors": ["B. Author"],
                                "source_url": "https://example.test/books/bad",
                                "evidence_url": "https://example.test/books/bad",
                                "signal_type": "media/curated list",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = codex_extraction_collector_result(
                config_path,
                tmp_path / "runs",
                {
                    "name": "Mixed Source",
                    "family": "media/curated list",
                    "url": "https://example.test/nonfiction",
                    "access_status": "open",
                    "language": "en",
                    "extraction_method": "codex-extraction-replay",
                    "source_page_fixture_file": str(capture_path),
                    "extraction_response_file": str(extraction_path),
                },
                "2026-07-04",
            )

            # Only the valid mention survives; the malformed one is dropped.
            self.assertEqual(len(result.mentions), 1)
            self.assertEqual(result.mentions[0]["title"], "Good One")
            self.assertEqual(result.valid_mention_count, 1)
            self.assertEqual(result.raw_mention_count, 2)
            self.assertEqual(result.skipped_item_count, 1)
            self.assertGreaterEqual(result.debug_metadata["dropped_mention_count"], 1)
            drop_reasons = result.debug_metadata["drop_reasons"]
            bad_drop = next(r for r in drop_reasons if r["title"] == "Bad Mention")
            self.assertIn("missing required field", bad_drop["reason"])

            # Run does not crash — we get a valid CollectorResult, not an exception.
            self.assertEqual(result.debug_metadata["collector_kind"], "codex-extraction")

    def test_forbidden_copied_text_failure_tagged_as_codex_extraction(self):
        """When Codex extraction artifacts contain forbidden copied-text fields,
        the failure debug metadata is tagged with collector_kind: codex-extraction
        (not agent-extraction)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/nonfiction",
                        "html": "<a href='/books/forbidden-book'>Forbidden Book</a>",
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
                                "title": "Forbidden Book",
                                "primary_authors": ["X. Author"],
                                "source_url": "https://example.test/books/forbidden-book",
                                "evidence_url": "https://example.test/books/forbidden-book",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Valid metadata.",
                                "full_review": "This is a lengthy copyrighted review copied verbatim.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(CollectorRunError) as ctx:
                codex_extraction_collector_result(
                    config_path,
                    tmp_path / "runs",
                    {
                        "name": "Codex Source",
                        "family": "media/curated list",
                        "url": "https://example.test/nonfiction",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "codex-extraction-replay",
                        "source_page_fixture_file": str(capture_path),
                        "extraction_response_file": str(extraction_path),
                    },
                    "2026-07-04",
                )

            self.assertIn("forbidden", str(ctx.exception).lower())
            self.assertEqual(
                ctx.exception.debug_metadata.get("collector_kind"),
                "codex-extraction",
                "forbidden-text failure must be tagged as codex-extraction, not agent-extraction",
            )
            self.assertEqual(
                ctx.exception.debug_metadata.get("safe_failure_status"),
                "forbidden_copied_text",
            )

    # --- #194 hardening tests ---

    def test_malformed_json_capture_file_reports_malformed_json_status(self):
        """A malformed JSON capture file raises CollectorRunError with
        safe_failure_status: malformed_json."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text("{not valid json", encoding="utf-8")
            extraction_path = tmp_path / "extraction.json"
            extraction_path.write_text(json.dumps({"mentions": []}), encoding="utf-8")
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(CollectorRunError) as ctx:
                codex_extraction_collector_result(
                    config_path,
                    tmp_path / "runs",
                    {
                        "name": "Bad Capture",
                        "family": "award",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "codex-extraction-replay",
                        "source_page_fixture_file": str(capture_path),
                        "extraction_response_file": str(extraction_path),
                    },
                    "2026-07-04",
                )

            self.assertEqual(
                ctx.exception.debug_metadata.get("safe_failure_status"),
                "malformed_json",
            )
            self.assertEqual(
                ctx.exception.debug_metadata.get("collector_kind"),
                "codex-extraction",
            )
            self.assertIn("malformed", str(ctx.exception).lower())

    def test_malformed_json_extraction_file_reports_malformed_json_status(self):
        """A malformed JSON extraction file raises CollectorRunError with
        safe_failure_status: malformed_json."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": "<a href='/book'>Book</a>",
                    }
                ),
                encoding="utf-8",
            )
            extraction_path = tmp_path / "extraction.json"
            extraction_path.write_text("{broken json[[[", encoding="utf-8")
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(CollectorRunError) as ctx:
                codex_extraction_collector_result(
                    config_path,
                    tmp_path / "runs",
                    {
                        "name": "Bad Extraction",
                        "family": "award",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "codex-extraction-replay",
                        "source_page_fixture_file": str(capture_path),
                        "extraction_response_file": str(extraction_path),
                    },
                    "2026-07-04",
                )

            self.assertEqual(
                ctx.exception.debug_metadata.get("safe_failure_status"),
                "malformed_json",
            )
            self.assertEqual(
                ctx.exception.debug_metadata.get("collector_kind"),
                "codex-extraction",
            )

    def test_all_dropped_by_missing_fields_reports_missing_required_fields_status(self):
        """When all Codex mentions are dropped by required-field validation,
        the safe_failure_status is missing_required_fields."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": "<a href='/books/a'>A</a> by A. Author, B. Author",
                    }
                ),
                encoding="utf-8",
            )
            extraction_path = tmp_path / "extraction.json"
            # All mentions missing signal_strength and evidence_note.
            extraction_path.write_text(
                json.dumps(
                    {
                        "mentions": [
                            {
                                "title": "A",
                                "primary_authors": ["A. Author"],
                                "source_url": "https://example.test/books/a",
                                "evidence_url": "https://example.test/books/a",
                                "signal_type": "award",
                            },
                            {
                                "title": "B",
                                "primary_authors": ["B. Author"],
                                "source_url": "https://example.test/books/b",
                                "evidence_url": "https://example.test/books/b",
                                "signal_type": "award",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(CollectorRunError) as ctx:
                codex_extraction_collector_result(
                    config_path,
                    tmp_path / "runs",
                    {
                        "name": "All Missing Fields",
                        "family": "award",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "codex-extraction-replay",
                        "source_page_fixture_file": str(capture_path),
                        "extraction_response_file": str(extraction_path),
                    },
                    "2026-07-04",
                )

            self.assertEqual(
                ctx.exception.debug_metadata.get("safe_failure_status"),
                "missing_required_fields",
            )
            self.assertEqual(ctx.exception.debug_metadata.get("field_dropped_count"), 2)
            self.assertEqual(ctx.exception.debug_metadata.get("provenance_dropped_count"), 0)

    def test_all_dropped_by_provenance_reports_unverifiable_evidence_status(self):
        """When all Codex mentions are dropped by evidence URL provenance,
        the safe_failure_status is unverifiable_evidence_urls."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            # Page has no book links at all.
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": "<p>No links here.</p> by G. Author",
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
                                "title": "Ghost Book",
                                "primary_authors": ["G. Author"],
                                "source_url": "https://example.test/books/ghost",
                                "evidence_url": "https://example.test/books/ghost",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "This URL does not appear on the captured page.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(CollectorRunError) as ctx:
                codex_extraction_collector_result(
                    config_path,
                    tmp_path / "runs",
                    {
                        "name": "All Dropped Prov",
                        "family": "media/curated list",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "codex-extraction-replay",
                        "source_page_fixture_file": str(capture_path),
                        "extraction_response_file": str(extraction_path),
                    },
                    "2026-07-04",
                )

            self.assertEqual(
                ctx.exception.debug_metadata.get("safe_failure_status"),
                "unverifiable_evidence_urls",
            )
            self.assertEqual(ctx.exception.debug_metadata.get("field_dropped_count"), 0)
            self.assertEqual(ctx.exception.debug_metadata.get("provenance_dropped_count"), 1)

    def test_mixed_drops_report_combined_failure_status(self):
        """When some mentions fail field validation and others fail provenance,
        the safe_failure_status reflects both issues."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": "<p>No links here either.</p> by A. Author, B. Author",
                    }
                ),
                encoding="utf-8",
            )
            extraction_path = tmp_path / "extraction.json"
            extraction_path.write_text(
                json.dumps(
                    {
                        "mentions": [
                            # Has required fields but evidence URL won't verify.
                            {
                                "title": "URL Drop",
                                "primary_authors": ["A. Author"],
                                "source_url": "https://example.test/books/a",
                                "evidence_url": "https://example.test/books/a",
                                "signal_type": "award",
                                "signal_strength": "winner",
                                "evidence_note": "Won.",
                            },
                            # Missing required fields (no evidence_note, no signal_strength).
                            {
                                "title": "Field Drop",
                                "primary_authors": ["B. Author"],
                                "source_url": "https://example.test/books/b",
                                "evidence_url": "https://example.test/books/b",
                                "signal_type": "award",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(CollectorRunError) as ctx:
                codex_extraction_collector_result(
                    config_path,
                    tmp_path / "runs",
                    {
                        "name": "Mixed Drops",
                        "family": "award",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "codex-extraction-replay",
                        "source_page_fixture_file": str(capture_path),
                        "extraction_response_file": str(extraction_path),
                    },
                    "2026-07-04",
                )

            self.assertEqual(
                ctx.exception.debug_metadata.get("safe_failure_status"),
                "missing_required_fields_and_unverifiable_evidence",
            )
            self.assertGreater(ctx.exception.debug_metadata.get("field_dropped_count", 0), 0)
            self.assertGreater(ctx.exception.debug_metadata.get("provenance_dropped_count", 0), 0)

    def test_empty_extraction_reports_empty_extraction_status(self):
        """An extraction artifact with zero mentions reports
        safe_failure_status: empty_extraction."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": "<p>Empty page.</p>",
                    }
                ),
                encoding="utf-8",
            )
            extraction_path = tmp_path / "extraction.json"
            extraction_path.write_text(json.dumps({"mentions": []}), encoding="utf-8")
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(CollectorRunError) as ctx:
                codex_extraction_collector_result(
                    config_path,
                    tmp_path / "runs",
                    {
                        "name": "Empty Source",
                        "family": "award",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "codex-extraction-replay",
                        "source_page_fixture_file": str(capture_path),
                        "extraction_response_file": str(extraction_path),
                    },
                    "2026-07-04",
                )

            self.assertEqual(
                ctx.exception.debug_metadata.get("safe_failure_status"),
                "empty_extraction",
            )
            self.assertEqual(ctx.exception.debug_metadata.get("extracted_mention_count"), 0)

    def test_missing_extraction_file_reports_clean_failure(self):
        """When no extraction_response_file is configured, the failure
        debug metadata has safe_failure_status and collector_kind."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": "<a href='/book'>Book</a>",
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(CollectorRunError) as ctx:
                codex_extraction_collector_result(
                    config_path,
                    tmp_path / "runs",
                    {
                        "name": "No Extraction File",
                        "family": "award",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "codex-extraction-replay",
                        "source_page_fixture_file": str(capture_path),
                        # No extraction_response_file.
                    },
                    "2026-07-04",
                )

            self.assertEqual(
                ctx.exception.debug_metadata.get("collector_kind"),
                "codex-extraction",
            )
            self.assertIn("unavailable", str(ctx.exception).lower())

    def test_source_runs_debug_metadata_excludes_secrets_and_raw_prompts(self):
        """Debug metadata on source runs for Codex-operated sources must never
        contain secrets, raw prompts, or copied source bodies."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": "<a href='/books/safe'>Safe Book</a> by S. Author",
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
                                "title": "Safe Book",
                                "primary_authors": ["S. Author"],
                                "source_url": "https://example.test/books/safe",
                                "evidence_url": "https://example.test/books/safe",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Metadata-only mention.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = SourceCollection(config_path, tmp_path / "runs", "2026-07-04").collect(
                [
                    {
                        "name": "Safe Source",
                        "family": "media/curated list",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "codex-extraction-replay",
                        "source_page_fixture_file": str(capture_path),
                        "extraction_response_file": str(extraction_path),
                    }
                ]
            )

            run = result.source_runs[0]
            run_json = json.dumps(run, ensure_ascii=False).lower()
            for secret_marker in ("api_key", "credential", "token", "secret", "password", "authorization", "bearer"):
                self.assertNotIn(
                    secret_marker, run_json, f"source_runs debug_metadata must not contain '{secret_marker}'"
                )
            # Safe debug metadata fields are present.
            self.assertEqual(run["debug_metadata"]["collector_kind"], "codex-extraction")
            self.assertEqual(run["debug_metadata"]["validated_mention_count"], 1)

    def test_run_health_output_includes_codex_source_diagnostics(self):
        """run-health must emit source_codex= diagnostics for Codex-operated
        sources, distinguishing them from real-live/replay/fixture modes."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "weekly-out"
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": "<a href='/books/health-book'>Health Book</a> by H. Author",
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
                                "title": "Health Book",
                                "primary_authors": ["H. Author"],
                                "source_url": "https://example.test/books/health-book",
                                "evidence_url": "https://example.test/books/health-book",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Codex-extracted metadata-only mention.",
                                "candidate_category": "history",
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
                        "run_date": "2026-07-04",
                        "pilot_mode": True,
                        "editor_row_target": 10,
                        "pilot_row_min": 1,
                        "pilot_row_max": 50,
                        "report_qa": {"reviewer": "test", "status": "ok"},
                        "sources": [
                            {
                                "name": "Codex Health Source",
                                "family": "media/curated list",
                                "url": "https://example.test/list",
                                "active": True,
                                "access_status": "open",
                                "extraction_method": "codex-extraction-replay",
                                "check_frequency": "weekly",
                                "language": "en",
                                "source_page_fixture_file": str(capture_path),
                                "extraction_response_file": str(extraction_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = _run_mvp(config_path, output_dir)
            self.assertEqual(result.returncode, 0, result.stderr)

            health = _run_health(output_dir)
            self.assertEqual(health.returncode, 0, health.stderr)

            # run-health must include codex-specific diagnostics.
            self.assertIn("source_codex=", health.stdout)
            self.assertIn("Codex Health Source", health.stdout)
            self.assertIn("collected", health.stdout)
            # Collection modes must show codex-capture, not real-live or replay.
            self.assertIn("codex-capture", health.stdout)
            self.assertNotIn("real-live", health.stdout)

            # source_mode= must show codex-capture for the source.
            self.assertIn("source_mode=Codex Health Source|codex-capture", health.stdout)

    def test_run_health_distinguishes_failed_codex_source_from_real_live(self):
        """run-health reports codex-specific safe_failure_status for a
        failed Codex source, not real-live/replay backend diagnostics."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "weekly-out"
            # Malformed extraction → failed codex source.
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": "<a href='/books/x'>X</a> by A. Author",
                    }
                ),
                encoding="utf-8",
            )
            extraction_path = tmp_path / "extraction.json"
            extraction_path.write_text("not valid json at all", encoding="utf-8")
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-07-04",
                        "pilot_mode": True,
                        "editor_row_target": 10,
                        "pilot_row_min": 1,
                        "pilot_row_max": 50,
                        "report_qa": {"reviewer": "test", "status": "ok"},
                        "sources": [
                            {
                                "name": "Malformed Codex Source",
                                "family": "media/curated list",
                                "url": "https://example.test/list",
                                "active": True,
                                "access_status": "open",
                                "extraction_method": "codex-extraction-replay",
                                "check_frequency": "weekly",
                                "language": "en",
                                "source_page_fixture_file": str(capture_path),
                                "extraction_response_file": str(extraction_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = _run_mvp(config_path, output_dir)
            self.assertEqual(result.returncode, 0, result.stderr)

            health = _run_health(output_dir)
            self.assertEqual(health.returncode, 0, health.stderr)

            # source_codex= must report the failure with malformed_json status.
            self.assertIn("source_codex=", health.stdout)
            self.assertIn("Malformed Codex Source", health.stdout)
            self.assertIn("malformed_json", health.stdout)
            # Must NOT contain real-live backend diagnostics.
            self.assertNotIn("real-live", health.stdout)
            self.assertNotIn("source_backend=", health.stdout)

    # --- #194 review-fix tests ---

    def test_missing_capture_file_reports_missing_capture_file_status(self):
        """When the configured capture file does not exist on disk,
        CollectorRunError is raised with safe_failure_status: missing_capture_file."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            extraction_path = tmp_path / "extraction.json"
            extraction_path.write_text(
                json.dumps(
                    {
                        "mentions": [
                            {
                                "title": "Book",
                                "primary_authors": ["A. Author"],
                                "source_url": "https://example.test/books/book",
                                "evidence_url": "https://example.test/books/book",
                                "signal_type": "award",
                                "signal_strength": "winner",
                                "evidence_note": "A note.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(CollectorRunError) as ctx:
                codex_extraction_collector_result(
                    config_path,
                    tmp_path / "runs",
                    {
                        "name": "Missing Capture",
                        "family": "award",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "codex-extraction-replay",
                        "source_page_fixture_file": "/nonexistent/path/capture.json",
                        "extraction_response_file": str(extraction_path),
                    },
                    "2026-07-04",
                )

            self.assertEqual(
                ctx.exception.debug_metadata.get("safe_failure_status"),
                "missing_capture_file",
            )
            self.assertEqual(
                ctx.exception.debug_metadata.get("collector_kind"),
                "codex-extraction",
            )
            self.assertIn("not found", str(ctx.exception).lower())

    def test_missing_extraction_file_reports_missing_extraction_file_status(self):
        """When the configured extraction file does not exist on disk,
        CollectorRunError is raised with safe_failure_status: missing_extraction_file."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": "<a href='/book'>Book</a>",
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(CollectorRunError) as ctx:
                codex_extraction_collector_result(
                    config_path,
                    tmp_path / "runs",
                    {
                        "name": "Missing Extraction",
                        "family": "award",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "codex-extraction-replay",
                        "source_page_fixture_file": str(capture_path),
                        "extraction_response_file": "/nonexistent/path/extraction.json",
                    },
                    "2026-07-04",
                )

            self.assertEqual(
                ctx.exception.debug_metadata.get("safe_failure_status"),
                "missing_extraction_file",
            )
            self.assertEqual(
                ctx.exception.debug_metadata.get("collector_kind"),
                "codex-extraction",
            )
            self.assertIn("not found", str(ctx.exception).lower())

    def test_missing_extraction_response_config_reports_missing_config_status(self):
        """When no extraction_response_file is configured at all,
        safe_failure_status is missing_extraction_artifact_config (not blank)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": "<a href='/book'>Book</a>",
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(CollectorRunError) as ctx:
                codex_extraction_collector_result(
                    config_path,
                    tmp_path / "runs",
                    {
                        "name": "No Extraction Config",
                        "family": "award",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "codex-extraction-replay",
                        "source_page_fixture_file": str(capture_path),
                        # No extraction_response_file at all.
                    },
                    "2026-07-04",
                )

            self.assertEqual(
                ctx.exception.debug_metadata.get("safe_failure_status"),
                "missing_extraction_artifact_config",
            )
            self.assertEqual(
                ctx.exception.debug_metadata.get("collector_kind"),
                "codex-extraction",
            )

    def test_signal_strength_missing_from_raw_artifact_is_dropped(self):
        """When signal_strength is missing from the raw Codex artifact,
        the mention is dropped as a required-field failure — it is NOT
        silently defaulted to 'minor mention'."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": "<a href='/books/weak'>Weak Book</a> by W. Author",
                    }
                ),
                encoding="utf-8",
            )
            extraction_path = tmp_path / "extraction.json"
            # signal_strength is intentionally omitted.
            extraction_path.write_text(
                json.dumps(
                    {
                        "mentions": [
                            {
                                "title": "Weak Book",
                                "primary_authors": ["W. Author"],
                                "source_url": "https://example.test/books/weak",
                                "evidence_url": "https://example.test/books/weak",
                                "signal_type": "media/curated list",
                                "evidence_note": "Listed on the page but no strength given.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(CollectorRunError) as ctx:
                codex_extraction_collector_result(
                    config_path,
                    tmp_path / "runs",
                    {
                        "name": "No Signal Strength",
                        "family": "media/curated list",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "codex-extraction-replay",
                        "source_page_fixture_file": str(capture_path),
                        "extraction_response_file": str(extraction_path),
                    },
                    "2026-07-04",
                )

            self.assertEqual(
                ctx.exception.debug_metadata.get("safe_failure_status"),
                "missing_required_fields",
            )
            # The dropped mention's reason must mention signal_strength.
            drop_reasons = ctx.exception.debug_metadata.get("drop_reasons", [])
            self.assertTrue(
                any("signal_strength" in r.get("reason", "") for r in drop_reasons),
                f"drop_reasons should mention signal_strength: {drop_reasons}",
            )

    def test_source_check_date_auto_filled_does_not_cause_required_field_drop(self):
        """source_check_date is auto-filled from run_date and is NOT required
        in the raw Codex artifact — its absence should not cause a drop."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": "<a href='/books/dated'>Dated Book</a> by D. Author",
                    }
                ),
                encoding="utf-8",
            )
            extraction_path = tmp_path / "extraction.json"
            # No source_check_date — it is auto-filled.
            extraction_path.write_text(
                json.dumps(
                    {
                        "mentions": [
                            {
                                "title": "Dated Book",
                                "primary_authors": ["D. Author"],
                                "source_url": "https://example.test/books/dated",
                                "evidence_url": "https://example.test/books/dated",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Metadata-only mention without explicit date.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = codex_extraction_collector_result(
                config_path,
                tmp_path / "runs",
                {
                    "name": "Auto Date Source",
                    "family": "media/curated list",
                    "url": "https://example.test/list",
                    "access_status": "open",
                    "language": "en",
                    "extraction_method": "codex-extraction-replay",
                    "source_page_fixture_file": str(capture_path),
                    "extraction_response_file": str(extraction_path),
                },
                "2026-07-04",
            )

            self.assertEqual(len(result.mentions), 1)
            self.assertEqual(result.mentions[0]["title"], "Dated Book")

    def test_extraction_status_blocked_reported_as_skipped_with_source_blocked_status(self):
        """An extraction artifact like {status: "blocked"} is reported as
        a skipped source with safe_failure_status: source_blocked, not
        silently treated as empty_extraction."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://blocked.test",
                        "html": "",
                    }
                ),
                encoding="utf-8",
            )
            extraction_path = tmp_path / "extraction.json"
            extraction_path.write_text(
                json.dumps(
                    {
                        "status": "blocked",
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(CollectorRunError) as ctx:
                codex_extraction_collector_result(
                    config_path,
                    tmp_path / "runs",
                    {
                        "name": "Status Blocked",
                        "family": "award",
                        "url": "https://blocked.test",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "codex-extraction-replay",
                        "source_page_fixture_file": str(capture_path),
                        "extraction_response_file": str(extraction_path),
                    },
                    "2026-07-04",
                )

            self.assertEqual(ctx.exception.status, "skipped")
            self.assertEqual(
                ctx.exception.debug_metadata.get("safe_failure_status"),
                "source_blocked",
            )
            self.assertEqual(
                ctx.exception.debug_metadata.get("extraction_status"),
                "blocked",
            )
            self.assertIn("blocked", str(ctx.exception).lower())

    def test_extraction_status_error_reported_as_failed_with_extraction_error_status(self):
        """An extraction artifact like {status: "timeout"} is reported as
        a failed source with safe_failure_status: extraction_status_error."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://error.test",
                        "html": "",
                    }
                ),
                encoding="utf-8",
            )
            extraction_path = tmp_path / "extraction.json"
            extraction_path.write_text(
                json.dumps(
                    {
                        "status": "timeout",
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(CollectorRunError) as ctx:
                codex_extraction_collector_result(
                    config_path,
                    tmp_path / "runs",
                    {
                        "name": "Status Timeout",
                        "family": "award",
                        "url": "https://error.test",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "codex-extraction-replay",
                        "source_page_fixture_file": str(capture_path),
                        "extraction_response_file": str(extraction_path),
                    },
                    "2026-07-04",
                )

            self.assertEqual(ctx.exception.status, "failed")
            self.assertEqual(
                ctx.exception.debug_metadata.get("safe_failure_status"),
                "extraction_status_error",
            )
            self.assertEqual(
                ctx.exception.debug_metadata.get("extraction_status"),
                "timeout",
            )
            self.assertIn("timeout", str(ctx.exception).lower())

    def test_extraction_status_ok_accepted_as_valid(self):
        """Codex extraction with status: "ok" is treated as a valid response
        and its mentions are deserialized, not treated as an error."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": "<a href='/books/ok-book'>OK Book</a> by O. Author",
                    }
                ),
                encoding="utf-8",
            )
            extraction_path = tmp_path / "extraction.json"
            extraction_path.write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "mentions": [
                            {
                                "title": "OK Book",
                                "primary_authors": ["O. Author"],
                                "source_url": "https://example.test/books/ok-book",
                                "evidence_url": "https://example.test/books/ok-book",
                                "signal_type": "award",
                                "signal_strength": "winner",
                                "evidence_note": "Valid with status ok.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = codex_extraction_collector_result(
                config_path,
                tmp_path / "runs",
                {
                    "name": "OK Status",
                    "family": "award",
                    "url": "https://example.test/list",
                    "access_status": "open",
                    "language": "en",
                    "extraction_method": "codex-extraction-replay",
                    "source_page_fixture_file": str(capture_path),
                    "extraction_response_file": str(extraction_path),
                },
                "2026-07-04",
            )

            self.assertEqual(len(result.mentions), 1)
            self.assertEqual(result.mentions[0]["title"], "OK Book")

    def test_extraction_status_success_accepted_as_valid(self):
        """Codex extraction with status: "success" is treated as a valid
        response."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": "<a href='/books/success-book'>Success Book</a> by S. Author",
                    }
                ),
                encoding="utf-8",
            )
            extraction_path = tmp_path / "extraction.json"
            extraction_path.write_text(
                json.dumps(
                    {
                        "status": "success",
                        "mentions": [
                            {
                                "title": "Success Book",
                                "primary_authors": ["S. Author"],
                                "source_url": "https://example.test/books/success-book",
                                "evidence_url": "https://example.test/books/success-book",
                                "signal_type": "bestseller/ranking",
                                "signal_strength": "top ranking",
                                "evidence_note": "Valid with status success.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = codex_extraction_collector_result(
                config_path,
                tmp_path / "runs",
                {
                    "name": "Success Status",
                    "family": "bestseller/ranking",
                    "url": "https://example.test/list",
                    "access_status": "open",
                    "language": "en",
                    "extraction_method": "codex-extraction-replay",
                    "source_page_fixture_file": str(capture_path),
                    "extraction_response_file": str(extraction_path),
                },
                "2026-07-04",
            )

            self.assertEqual(len(result.mentions), 1)
            self.assertEqual(result.mentions[0]["title"], "Success Book")

    def test_extraction_status_complete_accepted_as_valid(self):
        """Codex extraction with status: "complete" is treated as a valid
        response."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": "<a href='/books/complete-book'>Complete Book</a> by C. Author",
                    }
                ),
                encoding="utf-8",
            )
            extraction_path = tmp_path / "extraction.json"
            extraction_path.write_text(
                json.dumps(
                    {
                        "status": "complete",
                        "mentions": [
                            {
                                "title": "Complete Book",
                                "primary_authors": ["C. Author"],
                                "source_url": "https://example.test/books/complete-book",
                                "evidence_url": "https://example.test/books/complete-book",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Valid with status complete.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = codex_extraction_collector_result(
                config_path,
                tmp_path / "runs",
                {
                    "name": "Complete Status",
                    "family": "media/curated list",
                    "url": "https://example.test/list",
                    "access_status": "open",
                    "language": "en",
                    "extraction_method": "codex-extraction-replay",
                    "source_page_fixture_file": str(capture_path),
                    "extraction_response_file": str(extraction_path),
                },
                "2026-07-04",
            )

            self.assertEqual(len(result.mentions), 1)
            self.assertEqual(result.mentions[0]["title"], "Complete Book")

    def test_extraction_mentions_null_is_malformed(self):
        """mentions: null is diagnosed as malformed by shape, not as
        empty_extraction based on falsiness."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": "<a href='/books/null-book'>Null Book</a>",
                    }
                ),
                encoding="utf-8",
            )
            extraction_path = tmp_path / "extraction.json"
            extraction_path.write_text(json.dumps({"mentions": None}), encoding="utf-8")
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(CollectorRunError) as ctx:
                codex_extraction_collector_result(
                    config_path,
                    tmp_path / "runs",
                    {
                        "name": "Null Mentions",
                        "family": "award",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "codex-extraction-replay",
                        "source_page_fixture_file": str(capture_path),
                        "extraction_response_file": str(extraction_path),
                    },
                    "2026-07-04",
                )

            self.assertEqual(
                ctx.exception.debug_metadata.get("safe_failure_status"),
                "malformed_mentions",
            )

    def test_extraction_mentions_empty_object_is_malformed(self):
        """mentions: {} is diagnosed as malformed by shape, not silently
        accepted."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": "<a href='/books/dict-book'>Dict Book</a>",
                    }
                ),
                encoding="utf-8",
            )
            extraction_path = tmp_path / "extraction.json"
            extraction_path.write_text(json.dumps({"mentions": {}}), encoding="utf-8")
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(CollectorRunError) as ctx:
                codex_extraction_collector_result(
                    config_path,
                    tmp_path / "runs",
                    {
                        "name": "Dict Mentions",
                        "family": "award",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "codex-extraction-replay",
                        "source_page_fixture_file": str(capture_path),
                        "extraction_response_file": str(extraction_path),
                    },
                    "2026-07-04",
                )

            self.assertEqual(
                ctx.exception.debug_metadata.get("safe_failure_status"),
                "malformed_mentions",
            )

    def test_extraction_mentions_empty_string_is_malformed(self):
        """mentions: "" is diagnosed as malformed by shape, not as
        empty_extraction based on falsiness."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": "<a href='/books/str-book'>Str Book</a> by A. Author",
                    }
                ),
                encoding="utf-8",
            )
            extraction_path = tmp_path / "extraction.json"
            extraction_path.write_text(json.dumps({"mentions": ""}), encoding="utf-8")
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(CollectorRunError) as ctx:
                codex_extraction_collector_result(
                    config_path,
                    tmp_path / "runs",
                    {
                        "name": "String Mentions",
                        "family": "award",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "codex-extraction-replay",
                        "source_page_fixture_file": str(capture_path),
                        "extraction_response_file": str(extraction_path),
                    },
                    "2026-07-04",
                )

            self.assertEqual(
                ctx.exception.debug_metadata.get("safe_failure_status"),
                "malformed_mentions",
            )

    def test_run_health_source_codex_reports_missing_file_status(self):
        """run-health source_codex= diagnostics include safe_failure_status
        for missing capture/extraction files."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "weekly-out"
            extraction_path = tmp_path / "extraction.json"
            extraction_path.write_text(
                json.dumps(
                    {
                        "mentions": [
                            {
                                "title": "Book",
                                "primary_authors": ["A. Author"],
                                "source_url": "https://example.test/books/book",
                                "evidence_url": "https://example.test/books/book",
                                "signal_type": "award",
                                "signal_strength": "winner",
                                "evidence_note": "Note.",
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
                        "run_date": "2026-07-04",
                        "pilot_mode": True,
                        "editor_row_target": 10,
                        "pilot_row_min": 1,
                        "pilot_row_max": 50,
                        "report_qa": {"reviewer": "test", "status": "ok"},
                        "sources": [
                            {
                                "name": "Missing File Source",
                                "family": "award",
                                "url": "https://example.test/list",
                                "active": True,
                                "access_status": "open",
                                "extraction_method": "codex-extraction-replay",
                                "check_frequency": "weekly",
                                "language": "en",
                                "source_page_fixture_file": "/nonexistent/capture.json",
                                "extraction_response_file": str(extraction_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = _run_mvp(config_path, output_dir)
            self.assertEqual(result.returncode, 0, result.stderr)

            health = _run_health(output_dir)
            self.assertEqual(health.returncode, 0, health.stderr)

            self.assertIn("source_codex=", health.stdout)
            self.assertIn("Missing File Source", health.stdout)
            self.assertIn("missing_capture_file", health.stdout)

    def test_partial_success_field_drop_reports_counts_in_debug_metadata(self):
        """When one mention is valid and another is dropped by missing-field
        validation, the successful CollectorResult debug_metadata includes
        field_dropped_count and provenance_dropped_count."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": ("<a href='/books/valid'>Valid Book</a> by V. Author"),
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
                                "title": "Valid Book",
                                "primary_authors": ["V. Author"],
                                "source_url": "https://example.test/books/valid",
                                "evidence_url": "https://example.test/books/valid",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Valid Codex mention.",
                            },
                            {
                                # Missing signal_strength and evidence_note.
                                "title": "Field-Dropped Book",
                                "primary_authors": ["F. Author"],
                                "source_url": "https://example.test/books/dropped",
                                "evidence_url": "https://example.test/books/dropped",
                                "signal_type": "media/curated list",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = codex_extraction_collector_result(
                config_path,
                tmp_path / "runs",
                {
                    "name": "Partial Field Drop",
                    "family": "media/curated list",
                    "url": "https://example.test/list",
                    "access_status": "open",
                    "language": "en",
                    "extraction_method": "codex-extraction-replay",
                    "source_page_fixture_file": str(capture_path),
                    "extraction_response_file": str(extraction_path),
                },
                "2026-07-04",
            )

            # Partial success: one mention flows through.
            self.assertEqual(len(result.mentions), 1)
            self.assertEqual(result.mentions[0]["title"], "Valid Book")
            self.assertEqual(result.valid_mention_count, 1)
            self.assertEqual(result.raw_mention_count, 2)
            self.assertEqual(result.skipped_item_count, 1)

            # Debug metadata includes specific drop counts.
            md = result.debug_metadata
            self.assertEqual(md["dropped_mention_count"], 1)
            self.assertEqual(md["field_dropped_count"], 1, "field_dropped_count must be 1 for the missing-field drop")
            self.assertEqual(md["provenance_dropped_count"], 0, "provenance_dropped_count must be 0 (only field drop)")

    def test_partial_success_provenance_drop_reports_counts_in_debug_metadata(self):
        """When one mention is valid and another is dropped by unverifiable
        evidence URL, the successful CollectorResult debug_metadata has
        provenance_dropped_count."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            # Page only has link for the valid book.
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": "<a href='/books/kept'>Kept Book</a> by K. Author, P. Author",
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
                                "title": "Kept Book",
                                "primary_authors": ["K. Author"],
                                "source_url": "https://example.test/books/kept",
                                "evidence_url": "https://example.test/books/kept",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Found on the captured page.",
                            },
                            {
                                # All fields valid but evidence URL not in page.
                                "title": "Provenance-Dropped Book",
                                "primary_authors": ["P. Author"],
                                "source_url": "https://other.test/books/absent",
                                "evidence_url": "https://other.test/books/absent",
                                "signal_type": "media/curated list",
                                "signal_strength": "minor mention",
                                "evidence_note": "Not found anywhere in the captured HTML.",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = codex_extraction_collector_result(
                config_path,
                tmp_path / "runs",
                {
                    "name": "Partial Prov Drop",
                    "family": "media/curated list",
                    "url": "https://example.test/list",
                    "access_status": "open",
                    "language": "en",
                    "extraction_method": "codex-extraction-replay",
                    "source_page_fixture_file": str(capture_path),
                    "extraction_response_file": str(extraction_path),
                },
                "2026-07-04",
            )

            # Partial success: one mention flows through.
            self.assertEqual(len(result.mentions), 1)
            self.assertEqual(result.mentions[0]["title"], "Kept Book")
            self.assertEqual(result.valid_mention_count, 1)
            self.assertEqual(result.raw_mention_count, 2)
            self.assertEqual(result.skipped_item_count, 1)

            # Debug metadata has provenance drop count, zero field drops.
            md = result.debug_metadata
            self.assertEqual(md["dropped_mention_count"], 1)
            self.assertEqual(md["field_dropped_count"], 0)
            self.assertEqual(
                md["provenance_dropped_count"], 1, "provenance_dropped_count must be 1 for the evidence-URL drop"
            )

    def test_run_health_source_codex_reports_partial_drop_counts(self):
        """When a source has partial success (valid + dropped mentions),
        run-health source_codex= reports the specific drop counts."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "weekly-out"
            capture_path = copy_fixture("source-pages", "example-list-partial.json", tmp_path / "capture.json")
            extraction_path = copy_fixture(
                "extraction-responses", "example-partial-drop.json", tmp_path / "extraction.json"
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-07-04",
                        "pilot_mode": True,
                        "editor_row_target": 10,
                        "pilot_row_min": 1,
                        "pilot_row_max": 50,
                        "report_qa": {"reviewer": "test", "status": "ok"},
                        "sources": [
                            {
                                "name": "Partial Drop Source",
                                "family": "media/curated list",
                                "url": "https://example.test/list",
                                "active": True,
                                "access_status": "open",
                                "extraction_method": "codex-extraction-replay",
                                "check_frequency": "weekly",
                                "language": "en",
                                "source_page_fixture_file": str(capture_path),
                                "extraction_response_file": str(extraction_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = _run_mvp(config_path, output_dir)
            self.assertEqual(result.returncode, 0, result.stderr)

            # Source runs debug_metadata must have the drop counts.
            source_runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
            self.assertEqual(len(source_runs), 1)
            md = source_runs[0]["debug_metadata"]
            self.assertEqual(md["dropped_mention_count"], 1)
            self.assertEqual(md["field_dropped_count"], 1)
            self.assertEqual(md["provenance_dropped_count"], 0)

            health = _run_health(output_dir)
            self.assertEqual(health.returncode, 0, health.stderr)

            # run-health source_codex= must report the drop counts (not zeros).
            self.assertIn("source_codex=", health.stdout)
            self.assertIn("Partial Drop Source", health.stdout)
            # Source is collected (partial success, not failed).
            self.assertIn("|collected|", health.stdout)
            # Drop counts must reflect the actual drops.
            self.assertIn("dropped=1", health.stdout)
            self.assertIn("field_dropped=1", health.stdout)

    def test_forbidden_text_guardrail_fires_before_missing_field_drop(self):
        """When a Codex entry contains both forbidden copied text
        (e.g. full_review) AND missing required fields, the forbidden-text
        guardrail takes priority: the run fails with
        safe_failure_status=forbidden_copied_text, not missing_required_fields."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": "<a href='/books/hybrid'>Hybrid Book</a> by H. Author",
                    }
                ),
                encoding="utf-8",
            )
            extraction_path = tmp_path / "extraction.json"
            # Entry has full_review (forbidden) AND is missing signal_strength.
            extraction_path.write_text(
                json.dumps(
                    {
                        "mentions": [
                            {
                                "title": "Hybrid Book",
                                "primary_authors": ["H. Author"],
                                "source_url": "https://example.test/books/hybrid",
                                "evidence_url": "https://example.test/books/hybrid",
                                "signal_type": "media/curated list",
                                "evidence_note": "Found on the page.",
                                "full_review": "This is a lengthy copyrighted review copied verbatim into the Codex artifact.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(CollectorRunError) as ctx:
                codex_extraction_collector_result(
                    config_path,
                    tmp_path / "runs",
                    {
                        "name": "Forbidden + Missing",
                        "family": "media/curated list",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "codex-extraction-replay",
                        "source_page_fixture_file": str(capture_path),
                        "extraction_response_file": str(extraction_path),
                    },
                    "2026-07-04",
                )

            # Must be forbidden_copied_text, NOT missing_required_fields.
            self.assertEqual(
                ctx.exception.debug_metadata.get("safe_failure_status"),
                "forbidden_copied_text",
                "forbidden-text guardrail must fire before field-drop filtering",
            )
            self.assertEqual(
                ctx.exception.debug_metadata.get("collector_kind"),
                "codex-extraction",
            )
            self.assertIn("forbidden", str(ctx.exception).lower())
            self.assertIn("full_review", str(ctx.exception.debug_metadata.get("forbidden_fields", [])))

    def test_forbidden_article_body_with_valid_required_fields_still_caught(self):
        """An entry with all required fields present but containing article_body
        still raises forbidden_copied_text — the guardrail is not bypassed
        by fully-formed entries."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": "<a href='/books/article'>Article Book</a> by A. Author",
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
                                "title": "Article Book",
                                "primary_authors": ["A. Author"],
                                "source_url": "https://example.test/books/article",
                                "evidence_url": "https://example.test/books/article",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Valid metadata.",
                                "article_body": "The full article text copied here verbatim.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(CollectorRunError) as ctx:
                codex_extraction_collector_result(
                    config_path,
                    tmp_path / "runs",
                    {
                        "name": "Article Body Source",
                        "family": "media/curated list",
                        "url": "https://example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "codex-extraction-replay",
                        "source_page_fixture_file": str(capture_path),
                        "extraction_response_file": str(extraction_path),
                    },
                    "2026-07-04",
                )

            self.assertEqual(
                ctx.exception.debug_metadata.get("safe_failure_status"),
                "forbidden_copied_text",
            )
            self.assertIn("article_body", str(ctx.exception.debug_metadata.get("forbidden_fields", [])))

    # --- Issue #197: JSON-embedded URL provenance ---

    def test_json_embedded_product_url_passes_provenance(self):
        """USA TODAY-style capture: book-level productURL embedded in JSON
        structured data (not an <a href>). Evidence URL matches a productURL
        value → provenance should pass."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Simulate a SPA page where book links are in embedded JSON, not hrefs.
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://www.usatoday.com/booklist/nonfiction",
                        "html": (
                            '<script id="__NEXT_DATA__">'
                            '{"props":{"books":['
                            '{"title":"Regime Change",'
                            '"productURL":"https://bookshop.org/a/7775/9781668067246",'
                            '"authors":["Haberman, Maggie"]}'
                            "]}}</script>"
                            '<a href="/booklist">Booklist Home</a>'
                        ),
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
                                "title": "Regime Change",
                                "primary_authors": ["Haberman, Maggie"],
                                "source_url": "https://www.usatoday.com/booklist/nonfiction",
                                "evidence_url": "https://bookshop.org/a/7775/9781668067246",
                                "signal_type": "bestseller/ranking",
                                "signal_strength": "top ranking",
                                "evidence_note": "USA TODAY bestseller #1.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = codex_extraction_collector_result(
                config_path,
                tmp_path / "runs",
                {
                    "name": "USA TODAY",
                    "family": "bestseller/ranking",
                    "url": "https://www.usatoday.com/booklist/nonfiction",
                    "access_status": "open",
                    "language": "en",
                    "extraction_method": "codex-extraction-replay",
                    "source_page_fixture_file": str(capture_path),
                    "extraction_response_file": str(extraction_path),
                },
                "2026-07-04",
            )

            self.assertEqual(len(result.mentions), 1)
            self.assertEqual(result.mentions[0]["title"], "Regime Change")
            self.assertEqual(result.mentions[0]["evidence_url"], "https://bookshop.org/a/7775/9781668067246")
            self.assertEqual(result.valid_mention_count, 1)
            self.assertEqual(result.raw_mention_count, 1)
            self.assertEqual(result.debug_metadata["collector_kind"], "codex-extraction")

    def test_json_embedded_url_absent_from_capture_still_rejected(self):
        """Evidence URL that does not appear anywhere in the capture
        (neither href nor JSON-embedded) is still rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://www.usatoday.com/booklist/nonfiction",
                        "html": ('<script>{"productURL":"https://bookshop.org/a/7775/9781668067246"}</script>'),
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
                                "title": "Missing Book",
                                "primary_authors": ["M. Author"],
                                "source_url": "https://www.usatoday.com/booklist/nonfiction",
                                # This URL is NOT in the capture.
                                "evidence_url": "https://bookshop.org/a/7775/9789999999999",
                                "signal_type": "bestseller/ranking",
                                "signal_strength": "top ranking",
                                "evidence_note": "Should be dropped.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(CollectorRunError) as ctx:
                codex_extraction_collector_result(
                    config_path,
                    tmp_path / "runs",
                    {
                        "name": "USA TODAY",
                        "family": "bestseller/ranking",
                        "url": "https://www.usatoday.com/booklist/nonfiction",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "codex-extraction-replay",
                        "source_page_fixture_file": str(capture_path),
                        "extraction_response_file": str(extraction_path),
                    },
                    "2026-07-04",
                )

            self.assertEqual(
                ctx.exception.debug_metadata.get("safe_failure_status"),
                "unverifiable_evidence_urls",
            )

    def test_json_embedded_mixed_valid_invalid_reports_correct_counts(self):
        """Some mentions with evidence URLs present in JSON-embedded fields,
        some without → valid ones flow through, invalid ones are dropped, and
        counts (validated, dropped, provenance_dropped) are correct."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://www.usatoday.com/booklist/nonfiction",
                        "html": (
                            "<script>"
                            '{"books":['
                            '{"title":"Book A","productURL":"https://bookshop.org/a/7775/9781111111111"},'
                            '{"title":"Book B","productURL":"https://bookshop.org/a/7775/9782222222222"}'
                            "]}</script>"
                            "<p>by A. Author, B. Author, C. Author</p>"
                        ),
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
                                "title": "Book A — present",
                                "primary_authors": ["A. Author"],
                                "source_url": "https://www.usatoday.com/booklist/nonfiction",
                                "evidence_url": "https://bookshop.org/a/7775/9781111111111",
                                "signal_type": "bestseller/ranking",
                                "signal_strength": "top ranking",
                                "evidence_note": "Valid — productURL present.",
                            },
                            {
                                "title": "Book B — present",
                                "primary_authors": ["B. Author"],
                                "source_url": "https://www.usatoday.com/booklist/nonfiction",
                                "evidence_url": "https://bookshop.org/a/7775/9782222222222",
                                "signal_type": "bestseller/ranking",
                                "signal_strength": "top ranking",
                                "evidence_note": "Valid — productURL present.",
                            },
                            {
                                "title": "Book C — absent",
                                "primary_authors": ["C. Author"],
                                "source_url": "https://www.usatoday.com/booklist/nonfiction",
                                "evidence_url": "https://bookshop.org/a/7775/9783333333333",
                                "signal_type": "bestseller/ranking",
                                "signal_strength": "top ranking",
                                "evidence_note": "Should be dropped — URL absent.",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = codex_extraction_collector_result(
                config_path,
                tmp_path / "runs",
                {
                    "name": "USA TODAY Mixed",
                    "family": "bestseller/ranking",
                    "url": "https://www.usatoday.com/booklist/nonfiction",
                    "access_status": "open",
                    "language": "en",
                    "extraction_method": "codex-extraction-replay",
                    "source_page_fixture_file": str(capture_path),
                    "extraction_response_file": str(extraction_path),
                },
                "2026-07-04",
            )

            self.assertEqual(len(result.mentions), 2)
            self.assertEqual(result.mentions[0]["title"], "Book A — present")
            self.assertEqual(result.mentions[1]["title"], "Book B — present")
            self.assertEqual(result.valid_mention_count, 2)
            self.assertEqual(result.raw_mention_count, 3)
            self.assertEqual(result.skipped_item_count, 1)
            self.assertEqual(result.debug_metadata.get("validated_mention_count"), 2)
            self.assertEqual(result.debug_metadata.get("dropped_mention_count"), 1)
            self.assertEqual(result.debug_metadata.get("provenance_dropped_count"), 1)

    def test_href_extraction_still_works_alongside_json_embedded_urls(self):
        """Existing HTML anchor href extraction continues to work even when
        JSON-embedded URL extraction is also active."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://www.bookpage.com/genres/nonfiction/",
                        "html": (
                            '<a href="/reviews/href-book">Href Book</a>'
                            "<script>"
                            '{"books":['
                            '{"title":"Json Book",'
                            '"productURL":"https://bookshop.org/a/7775/9781111111111"}'
                            "]}</script>"
                            "<p>by H. Author, J. Author</p>"
                        ),
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
                                "title": "Href Book",
                                "primary_authors": ["H. Author"],
                                "source_url": "https://www.bookpage.com/genres/nonfiction/",
                                "evidence_url": "https://www.bookpage.com/reviews/href-book",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "From href.",
                            },
                            {
                                "title": "Json Book",
                                "primary_authors": ["J. Author"],
                                "source_url": "https://www.bookpage.com/genres/nonfiction/",
                                "evidence_url": "https://bookshop.org/a/7775/9781111111111",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "From JSON productURL.",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = codex_extraction_collector_result(
                config_path,
                tmp_path / "runs",
                {
                    "name": "Mixed Mode",
                    "family": "media/curated list",
                    "url": "https://www.bookpage.com/genres/nonfiction/",
                    "access_status": "open",
                    "language": "en",
                    "extraction_method": "codex-extraction-replay",
                    "source_page_fixture_file": str(capture_path),
                    "extraction_response_file": str(extraction_path),
                },
                "2026-07-04",
            )

            self.assertEqual(len(result.mentions), 2)
            titles = {m["title"] for m in result.mentions}
            self.assertEqual(titles, {"Href Book", "Json Book"})

    def test_json_escaped_url_normalized_before_comparison(self):
        """JSON-escaped URLs (\\/ for /, \u0026 for &) in embedded fields
        are normalized before provenance comparison."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            # Simulate JSON with escaped slashes and unicode escapes.
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://www.usatoday.com/booklist/nonfiction",
                        "html": (
                            '<script>{"productURL":"https:\\/\\/bookshop.org\\/a\\/7775\\/9781668067246"}</script>'
                            "<p>by E. Author</p>"
                        ),
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
                                "title": "Escaped URL Book",
                                "primary_authors": ["E. Author"],
                                "source_url": "https://www.usatoday.com/booklist/nonfiction",
                                # Normal (non-escaped) form in the extraction.
                                "evidence_url": "https://bookshop.org/a/7775/9781668067246",
                                "signal_type": "bestseller/ranking",
                                "signal_strength": "top ranking",
                                "evidence_note": "URL should match after unescaping.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = codex_extraction_collector_result(
                config_path,
                tmp_path / "runs",
                {
                    "name": "Escaped URL Source",
                    "family": "bestseller/ranking",
                    "url": "https://www.usatoday.com/booklist/nonfiction",
                    "access_status": "open",
                    "language": "en",
                    "extraction_method": "codex-extraction-replay",
                    "source_page_fixture_file": str(capture_path),
                    "extraction_response_file": str(extraction_path),
                },
                "2026-07-04",
            )

            self.assertEqual(len(result.mentions), 1)
            self.assertEqual(result.mentions[0]["title"], "Escaped URL Book")

    def test_json_field_urls_handles_unicode_slash_escapes(self):
        """_json_field_urls extracts URLs where forward slashes are JSON
        unicode-escaped as \\u002F.  Because json.loads itself decodes
        \\u002F to /, the integration path through a capture JSON file
        cannot preserve this form; we test the extraction function directly
        with raw HTML containing literal \\u002F characters."""
        from autoscout.source_collectors import _json_field_urls

        # Raw HTML where productURL uses \u002F for every / character.
        html = (
            r"<script>"
            r'{"productURL":"https:\u002F\u002Fbookshop.org'
            r'\u002Fa\u002F7775\u002F9781668067246"}'
            r"</script>"
        )
        urls = _json_field_urls(html)

        self.assertIn("https://bookshop.org/a/7775/9781668067246", urls)

        # Absent URL still not extracted.
        self.assertNotIn("https://bookshop.org/a/7775/9789999999999", urls)

    def test_json_unescape_handles_unicode_slash(self):
        """_json_unescape decodes \\u002F to /."""
        from autoscout.source_collectors import _json_unescape

        self.assertEqual(
            _json_unescape(r"https:\u002F\u002Fbookshop.org\u002Fa\u002F7775"),
            "https://bookshop.org/a/7775",
        )

    def test_url_and_canonical_url_fields_recognized(self):
        """Evidence URLs embedded in 'url' and 'canonicalUrl' JSON fields
        pass provenance, not only 'productURL'."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://example.test/list",
                        "html": (
                            "<script>"
                            '{"books":['
                            '{"title":"URL Book",'
                            '"url":"https://example.test/books/url-book"},'
                            '{"title":"Canonical Book",'
                            '"canonicalUrl":"https://example.test/books/canonical-book"}'
                            "]}</script>"
                            "<p>by U. Author, C. Author</p>"
                        ),
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
                                "title": "URL Book",
                                "primary_authors": ["U. Author"],
                                "source_url": "https://example.test/list",
                                "evidence_url": "https://example.test/books/url-book",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "From 'url' field.",
                            },
                            {
                                "title": "Canonical Book",
                                "primary_authors": ["C. Author"],
                                "source_url": "https://example.test/list",
                                "evidence_url": "https://example.test/books/canonical-book",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "From 'canonicalUrl' field.",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = codex_extraction_collector_result(
                config_path,
                tmp_path / "runs",
                {
                    "name": "URL + canonicalUrl Source",
                    "family": "media/curated list",
                    "url": "https://example.test/list",
                    "access_status": "open",
                    "language": "en",
                    "extraction_method": "codex-extraction-replay",
                    "source_page_fixture_file": str(capture_path),
                    "extraction_response_file": str(extraction_path),
                },
                "2026-07-04",
            )

            self.assertEqual(len(result.mentions), 2)
            titles = {m["title"] for m in result.mentions}
            self.assertEqual(titles, {"URL Book", "Canonical Book"})

    def test_json_embedded_evidence_flows_to_candidate_and_report_artifacts(self):
        """End-to-end: a codex-extraction-replay source with evidence URLs
        embedded in captured JSON (not href) flows through the full pipeline
        into scouting candidates, editor-facing CSV, run-summary, and
        source-runs artifacts."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = copy_fixture("source-pages", "usatoday-spa-booklist.json", tmp_path / "capture.json")
            extraction_path = copy_fixture(
                "extraction-responses", "usatoday-spa-extraction.json", tmp_path / "extraction.json"
            )
            config_path = tmp_path / "sources.json"
            output_dir = tmp_path / "weekly-out"
            config_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-07-04",
                        "pilot_mode": True,
                        "editor_row_target": 10,
                        "pilot_row_min": 1,
                        "pilot_row_max": 50,
                        "report_qa": {"reviewer": "test", "status": "pilot"},
                        "sources": [
                            {
                                "name": "SPA Booklist Source",
                                "family": "bestseller/ranking",
                                "url": "https://www.usatoday.com/booklist/nonfiction",
                                "active": True,
                                "access_status": "open",
                                "extraction_method": "codex-extraction-replay",
                                "check_frequency": "weekly",
                                "language": "en",
                                "source_page_fixture_file": str(capture_path),
                                "extraction_response_file": str(extraction_path),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = _run_mvp(config_path, output_dir)
            self.assertEqual(result.returncode, 0, result.stderr)

            # All expected artifacts exist.
            self.assertTrue((output_dir / "source-mentions.json").exists())
            self.assertTrue((output_dir / "scouting-candidates.json").exists())
            self.assertTrue((output_dir / "editor-facing.csv").exists())
            self.assertTrue((output_dir / "run-summary.json").exists())
            self.assertTrue((output_dir / "pilot-status.json").exists())
            self.assertTrue((output_dir / "source-runs.json").exists())

            # Source mentions contain the JSON-provenanced book.
            mentions = json.loads((output_dir / "source-mentions.json").read_text(encoding="utf-8"))
            self.assertEqual(len(mentions), 1)
            self.assertEqual(mentions[0]["title"], "SPA Book")
            self.assertEqual(mentions[0]["evidence_url"], "https://bookshop.org/a/7775/9781234567890")

            # Scouting candidates are generated from the mention.
            candidates = json.loads((output_dir / "scouting-candidates.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["title"], "SPA Book")

            # Editor-facing CSV has the candidate.
            with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertGreaterEqual(len(rows), 1)
            spa_row = next(r for r in rows if r["Title"] == "SPA Book")
            self.assertEqual(spa_row["Author"], "Spa Author")

            # Run summary reports codex-capture mode.
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertIn("codex-capture", summary["collection_modes"])

            # Source runs report codex-capture with the JSON-provenanced mention.
            source_runs = json.loads((output_dir / "source-runs.json").read_text(encoding="utf-8"))
            self.assertEqual(len(source_runs), 1)
            self.assertEqual(source_runs[0]["status"], "collected")
            self.assertEqual(source_runs[0]["collection_mode"], "codex-capture")

    def test_evidence_url_apex_and_www_treated_as_same_domain(self):
        """Evidence URLs on the same site with or without www are accepted."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://www.example.test/list",
                        "html": "<a href='https://www.example.test/books/kept'>Kept Book</a> by A. Author",
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
                                "title": "Apex URL Book",
                                "primary_authors": ["A. Author"],
                                "source_url": "https://example.test/list",
                                "evidence_url": "https://example.test/books/kept",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Apex-domain URL matches www source page.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = codex_extraction_collector_result(
                config_path,
                tmp_path / "runs",
                {
                    "name": "WWW Source",
                    "family": "media/curated list",
                    "url": "https://www.example.test/list",
                    "access_status": "open",
                    "language": "en",
                    "extraction_method": "codex-extraction-replay",
                    "source_page_fixture_file": str(capture_path),
                    "extraction_response_file": str(extraction_path),
                },
                "2026-07-04",
            )

            self.assertEqual(len(result.mentions), 1)
            self.assertEqual(result.mentions[0]["title"], "Apex URL Book")

    def test_evidence_url_sibling_subdomains_treated_as_different(self):
        """Sibling subdomains (e.g. books.example.test vs www.example.test)
        are deliberately treated as different domains.  Only apex-vs-www is
        normalized."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            # The capture page only links to www-prefixed URLs; the evidence
            # URL on a sibling subdomain is NOT present as a link on the page.
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://www.example.test/list",
                        "html": "<a href='https://www.example.test/books/kept'>Kept</a> by S. Author, R. Author",
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
                                "title": "Sibling Subdomain Book",
                                "primary_authors": ["S. Author"],
                                "source_url": "https://www.example.test/list",
                                "evidence_url": "https://books.example.test/title",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Sibling subdomain — different domain.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            with self.assertRaises(CollectorRunError) as ctx:
                codex_extraction_collector_result(
                    config_path,
                    tmp_path / "runs",
                    {
                        "name": "Sibling Source",
                        "family": "media/curated list",
                        "url": "https://www.example.test/list",
                        "access_status": "open",
                        "language": "en",
                        "extraction_method": "codex-extraction-replay",
                        "source_page_fixture_file": str(capture_path),
                        "extraction_response_file": str(extraction_path),
                    },
                    "2026-07-04",
                )

            self.assertEqual(
                ctx.exception.debug_metadata.get("safe_failure_status"),
                "unverifiable_evidence_urls",
            )

    def test_generic_json_url_from_tracker_domain_rejected(self):
        """A generic JSON 'url' field pointing to an analytics/tracker domain
        is not accepted as an evidence URL (same_domain check rejects it)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://www.example.test/booklist",
                        "html": (
                            "<script>"
                            '{"url":"https://analytics.tracker.test/collect?p=123"}'
                            "</script>"
                            '<a href="/books/real-book">Real Book</a>'
                            "<p>by R. Author</p>"
                        ),
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
                                "title": "Real Book",
                                "primary_authors": ["R. Author"],
                                "source_url": "https://www.example.test/booklist",
                                "evidence_url": "https://www.example.test/books/real-book",
                                "signal_type": "media/curated list",
                                "signal_strength": "major media list",
                                "evidence_note": "Tracked on the real page.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = codex_extraction_collector_result(
                config_path,
                tmp_path / "runs",
                {
                    "name": "Tracker Filtered",
                    "family": "media/curated list",
                    "url": "https://www.example.test/booklist",
                    "access_status": "open",
                    "language": "en",
                    "extraction_method": "codex-extraction-replay",
                    "source_page_fixture_file": str(capture_path),
                    "extraction_response_file": str(extraction_path),
                },
                "2026-07-04",
            )

            # The tracker URL is in the capture's JSON but not same-domain,
            # so it should not interfere — the real book via href should pass.
            self.assertEqual(len(result.mentions), 1)
            self.assertEqual(result.mentions[0]["title"], "Real Book")

    def test_product_url_to_external_site_accepted(self):
        """Product/book URL fields (productURL) pointing to external book
        retailers are still accepted as evidence URLs, even though they are
        on a different domain than the source page."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            capture_path = tmp_path / "capture.json"
            capture_path.write_text(
                json.dumps(
                    {
                        "source_page": "https://www.example.test/booklist",
                        "html": (
                            '<script>{"productURL":"https://bookshop.org/a/7775/9781668067246"}</script> by E. Author'
                        ),
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
                                "title": "External Product Book",
                                "primary_authors": ["E. Author"],
                                "source_url": "https://www.example.test/booklist",
                                "evidence_url": "https://bookshop.org/a/7775/9781668067246",
                                "signal_type": "bestseller/ranking",
                                "signal_strength": "top ranking",
                                "evidence_note": "Product URL to external retailer — accepted.",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "sources.json"
            config_path.write_text("{}", encoding="utf-8")

            result = codex_extraction_collector_result(
                config_path,
                tmp_path / "runs",
                {
                    "name": "Product URL Source",
                    "family": "bestseller/ranking",
                    "url": "https://www.example.test/booklist",
                    "access_status": "open",
                    "language": "en",
                    "extraction_method": "codex-extraction-replay",
                    "source_page_fixture_file": str(capture_path),
                    "extraction_response_file": str(extraction_path),
                },
                "2026-07-04",
            )

            # productURL to external retailer should pass provenance check.
            self.assertEqual(len(result.mentions), 1)
            self.assertEqual(result.mentions[0]["title"], "External Product Book")
