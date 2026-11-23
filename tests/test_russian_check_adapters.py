from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from autoscout.russian_editions import _BOOK24_SPEC
from autoscout.russian_editions import _LITRES_SPEC
from autoscout.russian_editions import _RSL_SPEC
from autoscout.russian_editions import _blocked_error
from autoscout.russian_editions import _infer_russian_match_type
from autoscout.russian_editions import _live_status_error
from autoscout.russian_editions import normalize_book24_result
from autoscout.russian_editions import normalize_litres_result
from autoscout.russian_editions import normalize_rsl_result
from autoscout.russian_editions import russian_check_adapter


class RussianCheckAdapterTests(unittest.TestCase):
    # -- Adapter discovery

    def test_litres_adapter_normalizes_recorded_retailer_result(self):
        check = {"retailer": "LitRes", "url": "https://www.litres.ru"}
        adapter = russian_check_adapter(check)

        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.name, "LitRes")
        self.assertEqual(adapter.source_type, "russian-retailer")
        self.assertIs(adapter.normalize_recorded_result, normalize_litres_result)

    def test_book24_adapter_normalizes_recorded_retailer_result(self):
        check = {"retailer": "Book24", "url": "https://book24.ru"}
        adapter = russian_check_adapter(check)

        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.name, "Book24")
        self.assertEqual(adapter.source_type, "russian-retailer")
        self.assertIs(adapter.normalize_recorded_result, normalize_book24_result)

    def test_rsl_adapter_normalizes_recorded_library_catalog_result(self):
        check = {"name": "RSL", "family": "russian-library-catalog"}
        adapter = russian_check_adapter(check)

        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.name, "RSL")
        self.assertEqual(adapter.source_type, "russian-library-catalog")
        self.assertIs(adapter.normalize_recorded_result, normalize_rsl_result)

    def test_ozon_configured_retailer_has_adapter_for_recorded_replay(self):
        check = {
            "retailer": "Ozon",
            "recorded_results": [
                {
                    "query": "Test Book",
                    "result_url": "https://www.ozon.ru/product/test-book/",
                    "result_title": "Test Book",
                    "candidate_title": "Test Book",
                    "primary_authors": ["A. Author"],
                    "match_type": "same-work Russian-language edition",
                    "evidence_note": "Ozon metadata-only replay result.",
                }
            ],
        }
        adapter = russian_check_adapter(check)

        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.name, "Ozon")
        self.assertEqual(adapter.source_type, "russian-retailer")
        results = adapter.recorded_results(Path("config.json"), check, "2026-01-01")
        self.assertEqual(results[0]["source_name"], "Ozon")
        self.assertEqual(results[0]["edition_url"], "https://www.ozon.ru/product/test-book/")

    def test_ozon_live_check_reports_unimplemented_adapter_instead_of_silent_zero(self):
        check = {"retailer": "Ozon", "enable_live": True}
        adapter = russian_check_adapter(check)

        self.assertIsNotNone(adapter)
        results, error = adapter.live_results(Path("config.json"), check, {"title": "Test Book"}, "2026-01-01")
        self.assertEqual(results, [])
        self.assertIn("Ozon live check has no implemented adapter", error)

    def test_unknown_check_has_no_adapter_and_remains_deferred(self):
        self.assertIsNone(russian_check_adapter({"retailer": "Russian Publisher Site"}))

    # -- Recorded replay: positive match

    def test_litres_recorded_replay_positive_match(self):
        check = {
            "retailer": "LitRes",
            "url": "https://www.litres.ru",
            "recorded_results": [
                {
                    "query": "Test Book",
                    "result_url": "https://www.litres.ru/book/test-book-123/",
                    "result_title": "Test Book",
                    "candidate_title": "Test Book",
                    "primary_authors": ["A. Author"],
                    "match_type": "same-work Russian-language edition",
                    "evidence_note": "LitRes lists the edition.",
                    "language": "Russian",
                    "format_clue": "ebook",
                    "check_date": "2026-01-01",
                }
            ],
        }
        adapter = russian_check_adapter(check)
        self.assertIsNotNone(adapter)

        results = adapter.recorded_results(Path("."), check, "2026-01-01")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["match_type"], "same-work Russian-language edition")
        self.assertEqual(results[0]["language"], "Russian")

    def test_book24_recorded_replay_positive_match(self):
        check = {
            "retailer": "Book24",
            "url": "https://book24.ru",
            "recorded_results": [
                {
                    "query": "Test Book",
                    "result_url": "https://book24.ru/product/test-book-456/",
                    "result_title": "Test Book",
                    "candidate_title": "Test Book",
                    "primary_authors": ["B. Writer"],
                    "match_type": "same-work Russian-language edition",
                    "evidence_note": "Book24 lists the edition.",
                    "language": "Russian",
                    "format_clue": "hardcover",
                    "check_date": "2026-02-01",
                }
            ],
        }
        adapter = russian_check_adapter(check)
        self.assertIsNotNone(adapter)

        results = adapter.recorded_results(Path("."), check, "2026-02-01")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source_type"], "russian-retailer")

    def test_rsl_recorded_replay_positive_match(self):
        check = {
            "name": "RSL",
            "family": "russian-library-catalog",
            "recorded_results": [
                {
                    "query": "Test Book",
                    "record_url": "https://search.rsl.ru/ru/record/01009999999",
                    "result_title": "Test Book",
                    "result_author": "C. Scholar",
                    "source_name": "RSL",
                    "check_date": "2026-03-01",
                    "match_type": "same-work Russian-language edition",
                    "evidence_note": "RSL catalog has the record.",
                }
            ],
        }
        adapter = russian_check_adapter(check)
        self.assertIsNotNone(adapter)

        results = adapter.recorded_results(Path("."), check, "2026-03-01")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source_type"], "russian-library-catalog")
        self.assertEqual(results[0]["match_type"], "same-work Russian-language edition")

    # -- Recorded replay: no match

    def test_litres_recorded_replay_no_match_different_title(self):
        check = {
            "retailer": "LitRes",
            "url": "https://www.litres.ru",
            "recorded_results": [
                {
                    "query": "Other Book",
                    "result_url": "https://www.litres.ru/book/other-book/",
                    "result_title": "Other Book",
                    "candidate_title": "Other Book",
                    "primary_authors": ["X. Author"],
                    "match_type": "same-work Russian-language edition",
                    "evidence_note": "Different book.",
                    "language": "Russian",
                    "format_clue": "ebook",
                    "check_date": "2026-01-01",
                }
            ],
        }
        adapter = russian_check_adapter(check)
        self.assertIsNotNone(adapter)

        results = adapter.recorded_results(Path("."), check, "2026-01-01")
        self.assertEqual(len(results), 1)
        # The result is recorded regardless — match filtering happens later
        # in the pipeline.  The adapter faithfully normalizes what is recorded.
        self.assertEqual(results[0]["candidate_title"], "Other Book")

    # -- Shared match inference

    def test_infer_match_type_positive_russian_edition(self):
        record = {
            "title": "Test Book",
            "author": "A. Author",
            "language": "Russian",
        }
        candidate = {
            "title": "Test Book",
            "primary_authors": ["A. Author"],
        }
        self.assertEqual(
            _infer_russian_match_type(record, candidate),
            "same-work Russian-language edition",
        )

    def test_infer_match_type_foreign_language(self):
        record = {
            "title": "Test Book",
            "author": "A. Author",
            "language": "English",
        }
        candidate = {
            "title": "Test Book",
            "primary_authors": ["A. Author"],
        }
        self.assertEqual(
            _infer_russian_match_type(record, candidate),
            "imported foreign-language listing",
        )

    def test_infer_match_type_ambiguous(self):
        record = {
            "title": "Different Title",
            "author": "Different Author",
            "language": "Russian",
        }
        candidate = {
            "title": "Test Book",
            "primary_authors": ["A. Author"],
        }
        self.assertEqual(
            _infer_russian_match_type(record, candidate),
            "ambiguous author/title match",
        )

    def test_infer_match_type_empty_author_defaults_to_match(self):
        record = {
            "title": "Test Book",
            "author": "",
            "language": "Russian",
        }
        candidate = {
            "title": "Test Book",
            "primary_authors": ["A. Author"],
        }
        self.assertEqual(
            _infer_russian_match_type(record, candidate),
            "same-work Russian-language edition",
        )

    # -- Blocked / error handling per adapter

    def test_litres_blocked_error_detects_captcha(self):
        self.assertTrue(_blocked_error(_LITRES_SPEC, "blocked by CAPTCHA-like response"))
        self.assertTrue(_blocked_error(_LITRES_SPEC, "HTTP 403 Forbidden"))

    def test_rsl_blocked_error_detects_access_block(self):
        self.assertTrue(_blocked_error(_RSL_SPEC, "blocked by CAPTCHA-like response"))
        self.assertTrue(_blocked_error(_RSL_SPEC, "HTTP 403 Forbidden"))
        self.assertFalse(_blocked_error(_RSL_SPEC, "some other error"))

    # -- Live status error messages are source-specific

    def test_live_status_error_includes_source_name(self):
        self.assertIn("LitRes", _live_status_error(_LITRES_SPEC, "blocked"))
        self.assertIn("LitRes", _live_status_error(_LITRES_SPEC, "captcha"))
        self.assertIn("LitRes", _live_status_error(_LITRES_SPEC, "timeout"))
        self.assertIn("LitRes", _live_status_error(_LITRES_SPEC, "failed"))

        self.assertIn("Book24", _live_status_error(_BOOK24_SPEC, "blocked"))
        self.assertIn("RSL", _live_status_error(_RSL_SPEC, "blocked"))

    # -- Fixture live results through the adapter

    def test_litres_fixture_live_results_positive_match(self):
        check = {
            "retailer": "LitRes",
            "url": "https://www.litres.ru",
            "fixture_live_results": [
                {
                    "query": "Test Book",
                    "result_url": "https://www.litres.ru/book/test/",
                    "result_title": "Test Book",
                    "candidate_title": "Test Book",
                    "primary_authors": ["A. Author"],
                    "match_type": "same-work Russian-language edition",
                    "language": "Russian",
                    "evidence_note": "Found on LitRes.",
                    "format_clue": "ebook",
                    "check_date": "2026-01-01",
                }
            ],
        }
        candidate = {
            "title": "Test Book",
            "primary_authors": ["A. Author"],
        }
        adapter = russian_check_adapter(check)
        self.assertIsNotNone(adapter)

        results, error = adapter.live_results(Path("."), check, candidate, "2026-01-01")
        self.assertEqual(error, "")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["match_type"], "same-work Russian-language edition")

    def test_litres_fixture_live_results_blocked_status(self):
        check = {
            "retailer": "LitRes",
            "url": "https://www.litres.ru",
            "fixture_live_results": [{"status": "captcha"}],
        }
        candidate = {
            "title": "Test Book",
            "primary_authors": ["A. Author"],
        }
        adapter = russian_check_adapter(check)
        self.assertIsNotNone(adapter)

        results, error = adapter.live_results(Path("."), check, candidate, "2026-01-01")
        self.assertNotEqual(error, "")
        self.assertIn("CAPTCHA", error)
        self.assertEqual(results, [])

    def test_rsl_fixture_live_results_blocked_status(self):
        check = {
            "name": "RSL",
            "family": "russian-library-catalog",
            "url": "https://search.rsl.ru/",
            "fixture_live_results": [{"status": "blocked"}],
        }
        candidate = {
            "title": "Test Book",
            "primary_authors": ["A. Author"],
        }
        adapter = russian_check_adapter(check)
        self.assertIsNotNone(adapter)

        results, error = adapter.live_results(Path("."), check, candidate, "2026-01-01")
        self.assertNotEqual(error, "")
        self.assertIn("access-denied", error)
        self.assertEqual(results, [])

    # -- Fixture live results from a file

    def test_litres_fixture_live_results_from_file(self):
        with TemporaryDirectory() as tmp:
            config_file = Path(tmp) / "config.json"
            config_file.touch()
            fixture_path = Path(tmp) / "litres-live-fixture.json"
            fixture_path.write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "query": "Test Book",
                                "result_url": "https://www.litres.ru/book/test/",
                                "result_title": "Test Book",
                                "candidate_title": "Test Book",
                                "primary_authors": ["A. Author"],
                                "match_type": "same-work Russian-language edition",
                                "language": "Russian",
                                "evidence_note": "From fixture file.",
                                "format_clue": "ebook",
                                "check_date": "2026-01-01",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            check = {
                "retailer": "LitRes",
                "url": "https://www.litres.ru",
                "live_response_files": ["litres-live-fixture.json"],
            }
            candidate = {
                "title": "Test Book",
                "primary_authors": ["A. Author"],
            }
            adapter = russian_check_adapter(check)
            self.assertIsNotNone(adapter)

            results, error = adapter.live_results(config_file, check, candidate, "2026-01-01")
            self.assertEqual(error, "")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["evidence_note"], "From fixture file.")

    # -- Adapter validates and normalizes via the spec seam

    def test_adapter_rejects_forbidden_fields_in_recorded_result(self):
        check = {
            "retailer": "LitRes",
            "url": "https://www.litres.ru",
            "recorded_results": [
                {
                    "query": "Test Book",
                    "result_url": "https://www.litres.ru/book/test/",
                    "result_title": "Test Book",
                    "candidate_title": "Test Book",
                    "primary_authors": ["A. Author"],
                    "match_type": "same-work Russian-language edition",
                    "evidence_note": "ok",
                    "language": "Russian",
                    "format_clue": "ebook",
                    "check_date": "2026-01-01",
                    "article_body": "This is forbidden full-text content.",
                }
            ],
        }
        adapter = russian_check_adapter(check)
        self.assertIsNotNone(adapter)

        with self.assertRaises(ValueError) as ctx:
            adapter.recorded_results(Path("."), check, "2026-01-01")
        self.assertIn("forbidden", str(ctx.exception))

    # -- All three adapters share the same recorded_records workflow

    def test_shared_recorded_records_workflow_uses_normalize_from_spec(self):
        """Demonstrate that all three sources go through the same code path."""
        rsl_check = {
            "name": "RSL",
            "family": "russian-library-catalog",
            "recorded_results": [
                {
                    "query": "Q",
                    "record_url": "https://rsl.example/1",
                    "result_title": "R",
                    "result_author": "A",
                    "source_name": "RSL",
                    "check_date": "2026-01-01",
                    "match_type": "same-work Russian-language edition",
                }
            ],
        }
        litres_check = {
            "retailer": "LitRes",
            "url": "https://www.litres.ru",
            "recorded_results": [
                {
                    "query": "Q",
                    "result_url": "https://litres.example/1",
                    "result_title": "R",
                    "candidate_title": "R",
                    "primary_authors": ["A"],
                    "match_type": "same-work Russian-language edition",
                    "evidence_note": "ok",
                    "language": "Russian",
                    "format_clue": "ebook",
                    "check_date": "2026-01-01",
                }
            ],
        }
        book24_check = {
            "retailer": "Book24",
            "url": "https://book24.ru",
            "recorded_results": [
                {
                    "query": "Q",
                    "result_url": "https://book24.example/1",
                    "result_title": "R",
                    "candidate_title": "R",
                    "primary_authors": ["A"],
                    "match_type": "same-work Russian-language edition",
                    "evidence_note": "ok",
                    "language": "Russian",
                    "format_clue": "hardcover",
                    "check_date": "2026-01-01",
                }
            ],
        }

        for check in [rsl_check, litres_check, book24_check]:
            adapter = russian_check_adapter(check)
            self.assertIsNotNone(adapter, f"No adapter for {check}")
            results = adapter.recorded_results(Path("."), check, "2026-01-01")
            self.assertEqual(len(results), 1, f"Wrong result count for {adapter.name}")
            self.assertEqual(results[0]["source_type"], adapter.source_type)
