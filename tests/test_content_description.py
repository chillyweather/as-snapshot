"""Tests for Russian Content Description validation, fallback, coverage,
provenance, and backfill (issues #255-#260)."""

from __future__ import annotations

import unittest

from autoscout.content_description import DESCRIPTION_FALLBACK
from autoscout.content_description import apply_description_artifacts
from autoscout.content_description import description_coverage
from autoscout.content_description import description_provenance_record
from autoscout.content_description import has_copied_prose
from autoscout.content_description import is_russian_prose
from autoscout.content_description import is_too_long
from autoscout.content_description import prepare_backfill_work_items
from autoscout.content_description import validate_content_description
from autoscout.scouting_candidates import ScoutingCandidate


class ContentDescriptionValidationTests(unittest.TestCase):
    """Tests for issue #256: Reject unsafe and non-Russian Content Descriptions."""

    def test_valid_russian_prose_accepted(self):
        value = (
            "Мемуар и критика Кремниевой долины от опытной технологической журналистки, "
            "которая показывает рост крупных интернет-компаний."
        )
        validated, reason = validate_content_description(value)
        self.assertIsNotNone(validated)
        self.assertIsNone(reason)

    def test_russian_prose_with_latin_names_accepted(self):
        value = "Книга Джона Смита о роли NATO и США в современных конфликтах."
        validated, reason = validate_content_description(value)
        self.assertIsNotNone(validated)
        self.assertIsNone(reason)

    def test_three_cyrillic_characters_do_not_make_english_prose_russian(self):
        value = "Mostly English prose containing many ordinary words and ending with яяя"
        validated, reason = validate_content_description(value)
        self.assertIsNone(validated)
        self.assertIn("insufficiently Russian", reason)

    def test_cyrillic_punctuation_and_latin_inside_valid_russian_accepted(self):
        value = 'Автор — бывший сотрудник NASA — объясняет, почему "мягкая сила" важнее.'
        validated, reason = validate_content_description(value)
        self.assertIsNotNone(validated)
        self.assertIsNone(reason)

    def test_english_only_rejected(self):
        value = "A memoir and critique of Silicon Valley by an experienced tech journalist."
        validated, reason = validate_content_description(value)
        self.assertIsNone(validated)
        self.assertIn("English-only", reason)

    def test_whitespace_only_rejected(self):
        for value in ("", "   ", "\n\n  \t"):
            validated, reason = validate_content_description(value)
            self.assertIsNone(validated, f"Should reject: {value!r}")
            self.assertIsNotNone(reason)

    def test_non_string_rejected(self):
        for value in (None, 123, [], {}):
            validated, reason = validate_content_description(value)
            self.assertIsNone(validated, f"Should reject non-string: {type(value)}")
            self.assertIsNotNone(reason)

    def test_excessive_length_rejected(self):
        value = "А" * 2001
        validated, reason = validate_content_description(value)
        self.assertIsNone(validated)
        self.assertIn("excessive length", reason)

    def test_max_length_accepted(self):
        value = "А" * 2000
        validated, reason = validate_content_description(value)
        self.assertIsNotNone(validated)

    def test_copied_publisher_prose_rejected(self):
        # Russian text with multiple copied-text markers from publisher/review sources
        value = (
            "«Блестящая работа.» —Publishers Weekly. "
            "«Обязательное чтение.» —Booklist. "
            "«Шедевр.» —Library Journal. "
            "Это лучшая книга года по версии The New York Times."
        )
        validated, reason = validate_content_description(value)
        self.assertIsNone(validated)
        self.assertIn("copied", reason)

    def test_isolated_bibliographic_name_not_copied_text(self):
        # A single short marker in otherwise Russian prose is not copied text.
        value = (
            "Книга получила положительные отзывы Publishers Weekly и вошла в список "
            "бестселлеров New York Times. Автор исследует влияние технологий на общество."
        )
        validated, reason = validate_content_description(value)
        self.assertIsNotNone(validated, f"Should accept bibliographic names: {reason}")

    def test_whitespace_normalization_preserves_original_prose(self):
        value = "   Первое предложение. \n  Второе   предложение.  "
        validated, _reason = validate_content_description(value)
        self.assertEqual(validated, "Первое предложение. Второе предложение.")

    def test_normalization_does_not_stylistically_rewrite(self):
        value = "Автор — бывший сотрудник ЦРУ."  # em-dash preserved
        validated, _reason = validate_content_description(value)
        self.assertEqual(validated, value)

    def test_is_russian_prose_detects_cyrillic(self):
        self.assertTrue(is_russian_prose("Привет"))
        self.assertTrue(is_russian_prose("Hello мир"))
        self.assertFalse(is_russian_prose("Hello world"))
        self.assertFalse(is_russian_prose(""))

    def test_has_copied_prose_detects_multiple_markers(self):
        # English text with multiple copied-text markers across long enough sentences
        self.assertTrue(
            has_copied_prose(
                "Publishers Weekly called this book a brilliant achievement that will change how we think. "
                "Library Journal recommended it as essential reading for everyone interested in the subject."
            )
        )
        self.assertFalse(has_copied_prose("Single mention of Publishers Weekly is fine in a short note."))

    def test_is_too_long_checks_bound(self):
        self.assertTrue(is_too_long("А" * 2001))
        self.assertFalse(is_too_long("А" * 2000))


class ContentDescriptionFallbackTests(unittest.TestCase):
    """Tests for issue #255: Render Russian Content Descriptions with safe fallback."""

    def test_editor_description_uses_fallback_when_content_description_empty(self):
        candidate = ScoutingCandidate.from_mapping(
            {
                "identity_key": "test|author",
                "title": "Test Book",
                "primary_authors": ["Author"],
                "short_description": "English short description.",
                "content_description": "",
                "source_evidence": [],
            }
        )
        desc = candidate.editor_description()
        self.assertEqual(desc, DESCRIPTION_FALLBACK)

    def test_editor_description_uses_content_description_when_present(self):
        candidate = ScoutingCandidate.from_mapping(
            {
                "identity_key": "test|author",
                "title": "Test Book",
                "primary_authors": ["Author"],
                "content_description": "Оригинальное русское описание книги.",
                "source_evidence": [],
            }
        )
        desc = candidate.editor_description()
        self.assertEqual(desc, "Оригинальное русское описание книги.")

    def test_editor_description_does_not_fall_back_to_short_description(self):
        candidate = ScoutingCandidate.from_mapping(
            {
                "identity_key": "test|author",
                "title": "Test Book",
                "primary_authors": ["Author"],
                "short_description": "English short description.",
                "source_evidence": [],
            }
        )
        desc = candidate.editor_description()
        self.assertEqual(desc, DESCRIPTION_FALLBACK)
        self.assertNotIn("English", desc)

    def test_editor_row_includes_fallback_description(self):
        candidate = ScoutingCandidate.from_mapping(
            {
                "identity_key": "test|author",
                "title": "Test Book",
                "primary_authors": ["Author"],
                "content_description": "",
                "source_evidence": [],
            }
        )
        row = candidate.editor_row()
        self.assertEqual(row["Description"], DESCRIPTION_FALLBACK)

    def test_fallback_remains_constant(self):
        self.assertEqual(DESCRIPTION_FALLBACK, "Описание требует дополнительной проверки")


class ContentDescriptionCoverageTests(unittest.TestCase):
    """Tests for issue #257: Report Russian-description coverage in run health."""

    def test_complete_coverage_when_all_have_valid_descriptions(self):
        candidates = [
            {"identity_key": "a|1", "content_description": "Описание один."},
            {"identity_key": "b|2", "content_description": "Описание два."},
        ]
        coverage = description_coverage(candidates)
        self.assertEqual(coverage["coverage"], "complete")
        self.assertEqual(coverage["validated_count"], 2)
        self.assertEqual(coverage["fallback_count"], 0)

    def test_partial_coverage_when_some_missing(self):
        candidates = [
            {"identity_key": "a|1", "content_description": "Описание один."},
            {"identity_key": "b|2", "content_description": ""},
            {"identity_key": "c|3", "content_description": "English only"},
        ]
        coverage = description_coverage(candidates)
        self.assertEqual(coverage["coverage"], "partial")
        self.assertEqual(coverage["validated_count"], 1)
        self.assertEqual(coverage["fallback_count"], 2)

    def test_all_fallback_when_no_descriptions(self):
        candidates = [
            {"identity_key": "a|1", "content_description": ""},
            {"identity_key": "b|2"},
        ]
        coverage = description_coverage(candidates)
        self.assertEqual(coverage["coverage"], "partial")
        self.assertEqual(coverage["validated_count"], 0)
        self.assertEqual(coverage["fallback_count"], 2)

    def test_invalid_reason_counts(self):
        candidates = [
            {"identity_key": "a|1", "content_description": "English only text"},
            {"identity_key": "b|2", "content_description": "More English"},
            {"identity_key": "c|3", "content_description": "A" * 2001},
        ]
        coverage = description_coverage(candidates)
        self.assertIn(
            "no Cyrillic content — English-only or insufficiently Russian text rejected",
            coverage["invalid_reason_counts"],
        )
        self.assertTrue(
            any("excessive length" in key for key in coverage["invalid_reason_counts"]),
            f"Expected excessive length key in: {coverage['invalid_reason_counts']}",
        )

    def test_fallback_ids_recorded(self):
        candidates = [
            {"identity_key": "a|1", "content_description": ""},
            {"identity_key": "b|2", "content_description": "English"},
            {"identity_key": "c|3", "content_description": "Описание один."},
        ]
        coverage = description_coverage(candidates)
        self.assertIn("a|1", coverage["fallback_ids"])
        self.assertIn("b|2", coverage["fallback_ids"])
        self.assertNotIn("c|3", coverage["fallback_ids"])

    def test_rendered_fallback_is_counted_as_fallback(self):
        coverage = description_coverage([{"identity_key": "a|1", "content_description": DESCRIPTION_FALLBACK}])
        self.assertEqual(coverage["validated_count"], 0)
        self.assertEqual(coverage["fallback_count"], 1)
        self.assertEqual(coverage["coverage"], "partial")

    def test_intake_rejection_reason_is_counted_for_blank_description(self):
        reason = "no Cyrillic content — English-only or insufficiently Russian text rejected"
        coverage = description_coverage(
            [
                {
                    "identity_key": "a|1",
                    "content_description": "",
                    "content_description_rejection_reason": reason,
                }
            ]
        )
        self.assertEqual(coverage["invalid_reason_counts"], {reason: 1})

    def test_empty_candidate_list(self):
        coverage = description_coverage([])
        self.assertEqual(coverage["coverage"], "complete")
        self.assertEqual(coverage["total_candidates"], 0)
        self.assertEqual(coverage["fallback_count"], 0)


class ContentDescriptionProvenanceTests(unittest.TestCase):
    """Tests for issue #258: Preserve Russian descriptions across cumulative runs."""

    def test_provenance_record_contains_all_fields(self):
        record = description_provenance_record(
            "book|author",
            "Описание книги.",
            "codex-generated",
            evidence_basis=["https://example.test/evidence"],
            applied_date="2026-07-12",
        )
        self.assertEqual(record["identity_key"], "book|author")
        self.assertEqual(record["content_description"], "Описание книги.")
        self.assertEqual(record["provenance"], "codex-generated")
        self.assertEqual(record["applied_date"], "2026-07-12")
        self.assertEqual(record["evidence_basis"], ["https://example.test/evidence"])

    def test_unknown_provenance_falls_back_to_backfilled(self):
        record = description_provenance_record("key", "desc", "invalid-provenance")
        self.assertEqual(record["provenance"], "backfilled")

    def test_provenance_values_are_validated(self):
        for provenance in ("codex-generated", "fallback", "backfilled", "editor-corrected"):
            record = description_provenance_record("key", "desc", provenance)
            self.assertEqual(record["provenance"], provenance)


class ContentDescriptionBackfillTests(unittest.TestCase):
    """Tests for issue #259: Backfill historical Russian Content Descriptions."""

    def test_prepare_work_items_selects_blank_and_fallback_only(self):
        history = [
            {
                "identity_key": "a|1",
                "title": "Book A",
                "content_description": "",
                "source_evidence": [{"source_url": "https://example.test/a", "evidence_note": "Mention A."}],
            },
            {
                "identity_key": "b|2",
                "title": "Book B",
                "content_description": DESCRIPTION_FALLBACK,
                "source_evidence": [{"source_url": "https://example.test/b", "evidence_note": "Mention B."}],
            },
            {
                "identity_key": "c|3",
                "title": "Book C",
                "content_description": "Описание есть.",
                "description_provenance": {"provenance": "codex-generated", "applied_date": "2026-07-01"},
                "source_evidence": [{"source_url": "https://example.test/c", "evidence_note": "Mention C."}],
            },
            {
                "identity_key": "d|4",
                "title": "Book D",
                "content_description": "Исправленное описание.",
                "description_provenance": {"provenance": "editor-corrected", "applied_date": "2026-07-01"},
                "source_evidence": [],
            },
        ]
        items = prepare_backfill_work_items(history)
        self.assertEqual(len(items), 2)
        ids = {item["identity_key"] for item in items}
        self.assertIn("a|1", ids)
        self.assertIn("b|2", ids)
        self.assertNotIn("c|3", ids)  # Has valid, non-fallback
        self.assertNotIn("d|4", ids)  # Editor-corrected

    def test_prepare_work_items_exclude_unsafe_fields(self):
        history = [
            {
                "identity_key": "a|1",
                "title": "Book A",
                "content_description": "",
                "article_body": "should not appear",
                "publisher_blurb": "should not appear",
                "source_evidence": [{"source_url": "https://example.test/a", "evidence_note": "Mention A."}],
            },
        ]
        items = prepare_backfill_work_items(history)
        self.assertEqual(len(items), 1)
        metadata = items[0]["metadata"]
        self.assertNotIn("article_body", metadata)
        self.assertNotIn("publisher_blurb", metadata)
        self.assertIn("source_evidence", metadata)

    def test_apply_backfill_rejects_unknown_identity(self):
        history = [{"identity_key": "a|1", "title": "Book A", "content_description": ""}]
        artifacts = [{"identity_key": "unknown|identity", "content_description": "Новое описание."}]
        updated, results = apply_description_artifacts(history, artifacts)
        self.assertEqual(results["unknown_identity"], 1)
        self.assertEqual(results["applied"], 0)

    def test_apply_backfill_applies_valid_description(self):
        history = [{"identity_key": "a|1", "title": "Book A", "content_description": ""}]
        artifacts = [
            {
                "identity_key": "a|1",
                "content_description": "Новое русское описание.",
                "research_sources": ["https://publisher.example.test/book"],
            }
        ]
        updated, results = apply_description_artifacts(history, artifacts)
        self.assertEqual(results["applied"], 1)
        self.assertEqual(updated[0]["content_description"], "Новое русское описание.")
        self.assertIsInstance(updated[0]["description_provenance"], dict)
        self.assertEqual(updated[0]["description_provenance"]["provenance"], "backfilled")
        self.assertEqual(
            updated[0]["description_provenance"]["evidence_basis"],
            ["https://publisher.example.test/book"],
        )

    def test_apply_backfill_resolves_invalid_to_fallback(self):
        history = [{"identity_key": "a|1", "title": "Book A", "content_description": ""}]
        artifacts = [{"identity_key": "a|1", "content_description": "English only text"}]
        updated, results = apply_description_artifacts(history, artifacts)
        self.assertEqual(results["fallback"], 1)
        self.assertEqual(updated[0]["content_description"], DESCRIPTION_FALLBACK)
        self.assertIsInstance(updated[0]["description_provenance"], dict)
        self.assertEqual(updated[0]["description_provenance"]["provenance"], "fallback")

    def test_apply_backfill_skips_editor_corrected(self):
        history = [
            {
                "identity_key": "a|1",
                "title": "Book A",
                "content_description": "Исправленное редактором описание.",
                "description_provenance": {"provenance": "editor-corrected", "applied_date": "2026-07-01"},
            }
        ]
        artifacts = [{"identity_key": "a|1", "content_description": "Новое описание."}]
        updated, results = apply_description_artifacts(history, artifacts)
        self.assertEqual(results["skipped"], 1)
        self.assertEqual(updated[0]["content_description"], "Исправленное редактором описание.")
        self.assertEqual(updated[0]["description_provenance"]["provenance"], "editor-corrected")

    def test_apply_backfill_skips_existing_valid_by_default(self):
        history = [
            {
                "identity_key": "a|1",
                "title": "Book A",
                "content_description": "Существующее описание.",
                "description_provenance": {"provenance": "codex-generated", "applied_date": "2026-07-01"},
            }
        ]
        artifacts = [{"identity_key": "a|1", "content_description": "Новое другое описание."}]
        updated, results = apply_description_artifacts(history, artifacts)
        self.assertEqual(results["skipped"], 1)
        self.assertEqual(updated[0]["content_description"], "Существующее описание.")

    def test_apply_backfill_overwrites_with_flag(self):
        history = [
            {
                "identity_key": "a|1",
                "title": "Book A",
                "content_description": "Существующее описание.",
                "description_provenance": {"provenance": "codex-generated", "applied_date": "2026-07-01"},
            }
        ]
        artifacts = [{"identity_key": "a|1", "content_description": "Новое описание с флагом."}]
        updated, results = apply_description_artifacts(history, artifacts, allow_overwrite_valid=True)
        self.assertEqual(results["applied"], 1)
        self.assertEqual(updated[0]["content_description"], "Новое описание с флагом.")
        self.assertEqual(updated[0]["description_provenance"]["provenance"], "backfilled")

    def test_apply_backfill_is_idempotent(self):
        history = [
            {
                "identity_key": "a|1",
                "title": "Book A",
                "content_description": "Уже бэкфилленное описание.",
                "description_provenance": {"provenance": "backfilled", "applied_date": "2026-07-01"},
            }
        ]
        artifacts = [{"identity_key": "a|1", "content_description": "Уже бэкфилленное описание."}]
        updated, results = apply_description_artifacts(history, artifacts)
        self.assertEqual(results["skipped"], 1)

    def test_apply_backfill_replaces_fallback_with_valid(self):
        history = [
            {
                "identity_key": "a|1",
                "title": "Book A",
                "content_description": DESCRIPTION_FALLBACK,
                "description_provenance": "fallback",
            }
        ]
        artifacts = [{"identity_key": "a|1", "content_description": "Новое валидное описание."}]
        updated, results = apply_description_artifacts(history, artifacts)
        self.assertEqual(results["applied"], 1)
        self.assertEqual(updated[0]["content_description"], "Новое валидное описание.")

    def test_apply_backfill_replaces_blank_with_valid(self):
        history = [{"identity_key": "a|1", "title": "Book A", "content_description": ""}]
        artifacts = [{"identity_key": "a|1", "content_description": "Новое описание."}]
        updated, results = apply_description_artifacts(history, artifacts)
        self.assertEqual(results["applied"], 1)
        self.assertEqual(updated[0]["content_description"], "Новое описание.")

    def test_apply_backfill_invalid_items_fail_independently(self):
        history = [
            {"identity_key": "a|1", "title": "Book A", "content_description": ""},
            {"identity_key": "b|2", "title": "Book B", "content_description": ""},
        ]
        artifacts = [
            {"identity_key": "a|1", "content_description": "English only"},
            {"identity_key": "b|2", "content_description": "Валидное русское описание."},
        ]
        updated, results = apply_description_artifacts(history, artifacts)
        self.assertEqual(results["applied"], 1)
        self.assertEqual(results["fallback"], 1)
        updated_a = next(r for r in updated if r["identity_key"] == "a|1")
        updated_b = next(r for r in updated if r["identity_key"] == "b|2")
        self.assertEqual(updated_a["content_description"], DESCRIPTION_FALLBACK)
        self.assertEqual(updated_b["content_description"], "Валидное русское описание.")

    def test_apply_backfill_preserves_non_description_state(self):
        history = [
            {
                "identity_key": "a|1",
                "title": "Book A",
                "primary_authors": ["Author A"],
                "editor_status": "interested",
                "editor_note": "Keep.",
                "russian_edition_status": "not found in Russian",
                "visible_risk_status": "no visible risk",
                "content_description": "",
            }
        ]
        artifacts = [{"identity_key": "a|1", "content_description": "Описание книги."}]
        updated, _results = apply_description_artifacts(history, artifacts)
        self.assertEqual(updated[0]["title"], "Book A")
        self.assertEqual(updated[0]["editor_status"], "interested")
        self.assertEqual(updated[0]["editor_note"], "Keep.")
        self.assertEqual(updated[0]["russian_edition_status"], "not found in Russian")
        self.assertEqual(updated[0]["visible_risk_status"], "no visible risk")


class ContentDescriptionEndToEndTests(unittest.TestCase):
    """Integration tests for issue #260: Verify the Russian-description lifecycle end to end."""

    def test_valid_description_flows_through_editor_row(self):
        """A valid Russian description appears in the editor row."""
        candidate = ScoutingCandidate.from_mapping(
            {
                "identity_key": "test|author",
                "title": "Test Book",
                "primary_authors": ["Author"],
                "content_description": "Оригинальное русское описание.",
                "source_evidence": [
                    {
                        "source_name": "Source",
                        "source_family": "media",
                        "source_url": "https://example.test/source",
                        "source_check_date": "2026-07-12",
                        "signal_strength": "minor mention",
                        "evidence_note": "Mentioned.",
                    }
                ],
            }
        )
        row = candidate.editor_row()
        self.assertEqual(row["Description"], "Оригинальное русское описание.")
        self.assertNotEqual(row["Description"], DESCRIPTION_FALLBACK)

    def test_missing_description_produces_fallback_in_editor_row(self):
        """A missing description resolves to the fallback."""
        candidate = ScoutingCandidate.from_mapping(
            {
                "identity_key": "test|author",
                "title": "Test Book",
                "primary_authors": ["Author"],
                "source_evidence": [],
            }
        )
        row = candidate.editor_row()
        self.assertEqual(row["Description"], DESCRIPTION_FALLBACK)

    def test_fallback_candidate_remains_visible_with_all_metadata(self):
        """A fallback candidate keeps its ID, priority, risk, and editor state."""
        candidate = ScoutingCandidate.from_mapping(
            {
                "identity_key": "test|author",
                "title": "Test Book",
                "primary_authors": ["Author"],
                "content_description": "",
                "publisher": "Test Press",
                "publication_year": "2026",
                "candidate_category": "history",
                "russian_edition_status": "not found in Russian",
                "visible_risk_status": "no visible risk",
                "priority_label": "B",
                "editor_status": "interested",
                "editor_note": "Follow up.",
                "source_evidence": [
                    {
                        "source_name": "Source",
                        "source_family": "media",
                        "source_url": "https://example.test/source",
                        "source_check_date": "2026-07-12",
                        "signal_strength": "shortlist",
                        "evidence_note": "Shortlisted.",
                    }
                ],
            }
        )
        row = candidate.editor_row()
        self.assertEqual(row["Description"], DESCRIPTION_FALLBACK)
        self.assertEqual(row["Title"], "Test Book")
        self.assertEqual(row["Candidate ID"], "test|author")
        self.assertEqual(row["Priority label"], "B")
        self.assertEqual(row["Visible-risk status"], "no visible risk")
        self.assertEqual(row["Editor status"], "interested")
        self.assertEqual(row["Editor note"], "Follow up.")
        self.assertEqual(row["Russian-edition status"], "not found in Russian")

    def test_description_rejection_does_not_deprioritize_candidate(self):
        """An otherwise valid candidate stays at its priority even with a fallback description."""
        candidate = ScoutingCandidate.from_mapping(
            {
                "identity_key": "test|author",
                "title": "Test Book",
                "primary_authors": ["Author"],
                "content_description": "",
                "publisher_fit": "clear fit",
                "russian_edition_status": "not found in Russian",
                "visible_risk_status": "no visible risk",
                "source_evidence": [
                    {"source_family": "award", "signal_strength": "winner", "evidence_note": "Winner."},
                    {"source_family": "media", "signal_strength": "shortlist", "evidence_note": "Second signal."},
                ],
            }
        )
        candidate.apply_signal_aggregation()
        self.assertEqual(candidate.priority_label(), "A")
        self.assertEqual(candidate.editor_description(), DESCRIPTION_FALLBACK)

    def test_title_and_metadata_unchanged_by_description_handling(self):
        """Original title, authors, publisher are unchanged by description handling."""
        candidate = ScoutingCandidate.from_mapping(
            {
                "identity_key": "original title|john doe",
                "title": "Original Title",
                "primary_authors": ["John Doe"],
                "publisher": "Original Press",
                "publication_year": "2026",
                "content_description": "",
                "english_title": "English Helper Title",
                "english_author_display": "John Doe",
                "search_translation": "Поисковый перевод",
                "author_transliteration_note": "Джон Доу",
                "source_evidence": [
                    {
                        "source_name": "Source",
                        "source_family": "media",
                        "source_url": "https://example.test/source",
                        "source_check_date": "2026-07-12",
                        "signal_strength": "minor mention",
                        "evidence_note": "Mentioned.",
                    }
                ],
            }
        )
        row = candidate.editor_row()
        self.assertEqual(row["Title"], "Original Title")
        self.assertEqual(row["Author"], "John Doe")
        self.assertEqual(row["English title"], "English Helper Title (unverified)")
        self.assertEqual(row["English author display"], "John Doe (original)")
        self.assertEqual(row["Description"], DESCRIPTION_FALLBACK)

    def test_search_translation_separate_from_description(self):
        """Search Translation remains separate from Content Description."""
        candidate = ScoutingCandidate.from_mapping(
            {
                "identity_key": "test|author",
                "title": "Original Title",
                "primary_authors": ["Author"],
                "content_description": "Русское описание.",
                "search_translation": "Поисковый перевод названия",
                "source_evidence": [],
            }
        )
        row = candidate.editor_row()
        self.assertEqual(row["Description"], "Русское описание.")
        self.assertNotIn("Поисковый перевод", row["Description"])

    def test_backfill_preserves_non_description_state_fields(self):
        """Backfill updates only description and provenance, nothing else."""
        history = [
            {
                "identity_key": "a|1",
                "title": "Original Title",
                "primary_authors": ["Original Author"],
                "publisher": "Original Press",
                "editor_status": "interested",
                "editor_note": "Promising.",
                "russian_edition_status": "not found in Russian",
                "visible_risk_status": "no visible risk",
                "priority_label": "B",
                "content_description": "",
            }
        ]
        artifacts = [{"identity_key": "a|1", "content_description": "Новое русское описание."}]
        updated, _results = apply_description_artifacts(history, artifacts)
        r = updated[0]
        self.assertEqual(r["title"], "Original Title")
        self.assertEqual(r["primary_authors"], ["Original Author"])
        self.assertEqual(r["publisher"], "Original Press")
        self.assertEqual(r["editor_status"], "interested")
        self.assertEqual(r["editor_note"], "Promising.")
        self.assertEqual(r["russian_edition_status"], "not found in Russian")
        self.assertEqual(r["visible_risk_status"], "no visible risk")
        self.assertEqual(r["priority_label"], "B")
        self.assertEqual(r["content_description"], "Новое русское описание.")
        self.assertIsInstance(r["description_provenance"], dict)
        self.assertEqual(r["description_provenance"]["provenance"], "backfilled")
