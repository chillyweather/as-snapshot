import unittest

from autoscout.source_mentions import SourceMention


class SourceMentionTests(unittest.TestCase):
    def test_fixture_candidate_normalizes_through_source_mention_record(self):
        mention = SourceMention.from_candidate(
            {
                "name": "Fixture Source",
                "family": "media/curated list",
                "url": "https://example.test/list",
                "notes": "Fixture source note.",
            },
            {
                "title": "Useful Book",
                "author": "Alex Example",
                "source_url": "https://example.test/list/useful-book",
            },
            "2026-06-10",
        ).to_dict()

        self.assertEqual(mention["title"], "Useful Book")
        self.assertEqual(mention["primary_authors"], ["Alex Example"])
        self.assertEqual(mention["source_family"], "media/curated list")
        self.assertEqual(mention["signal_type"], "media/curated list")
        self.assertEqual(mention["signal_strength"], "minor mention")
        self.assertEqual(mention["evidence_note"], "Fixture source note.")

    def test_collector_mention_requires_safe_signal_metadata(self):
        with self.assertRaisesRegex(ValueError, "missing required field"):
            SourceMention.from_collector(
                {"name": "Collector Source", "family": "media/curated list"},
                {
                    "title": "Useful Book",
                    "primary_authors": ["Alex Example"],
                    "source_url": "https://example.test/list/useful-book",
                },
                "2026-06-10",
            )

    def test_recorded_mention_rejects_copied_source_text(self):
        with self.assertRaisesRegex(ValueError, "forbidden copied text"):
            SourceMention.from_recorded(
                {"name": "Recorded Source", "family": "media/curated list"},
                {
                    "title": "Useful Book",
                    "primary_authors": ["Alex Example"],
                    "source_url": "https://example.test/list/useful-book",
                    "source_check_date": "2026-06-10",
                    "signal_type": "media/curated list",
                    "signal_strength": "major media list",
                    "evidence_note": "Included in a configured source list.",
                    "publisher_blurb": "Copied source text.",
                },
            )

    def test_non_empty_content_description_must_look_russian(self):
        # English-only content_description is silently cleared (not rejected),
        # per #256: description rejection must not hide the candidate.
        mention = SourceMention.from_recorded(
            {"name": "Recorded Source", "family": "media/curated list"},
            {
                "title": "Useful Book",
                "primary_authors": ["Alex Example"],
                "source_url": "https://example.test/list/useful-book",
                "source_check_date": "2026-06-10",
                "signal_type": "media/curated list",
                "signal_strength": "major media list",
                "evidence_note": "Included in a configured source list.",
                "content_description": "English-only scouting prose should fail intake.",
            },
        ).to_dict()

        self.assertEqual(mention["content_description"], "")
        self.assertIn("English-only", mention["content_description_rejection_reason"])

    def test_content_description_is_normalized_at_intake(self):
        mention = SourceMention.from_recorded(
            {"name": "Recorded Source", "family": "media/curated list"},
            {
                "title": "Useful Book",
                "primary_authors": ["Alex Example"],
                "source_url": "https://example.test/list/useful-book",
                "source_check_date": "2026-06-10",
                "signal_type": "media/curated list",
                "signal_strength": "major media list",
                "evidence_note": "Included in a configured source list.",
                "content_description": "  Русское   описание\nкниги.  ",
            },
        ).to_dict()

        self.assertEqual(mention["content_description"], "Русское описание книги.")
        self.assertNotIn("content_description_rejection_reason", mention)

    def test_non_string_content_description_resolves_to_fallback_state(self):
        mention = SourceMention.from_recorded(
            {"name": "Recorded Source", "family": "media/curated list"},
            {
                "title": "Useful Book",
                "primary_authors": ["Alex Example"],
                "source_url": "https://example.test/list/useful-book",
                "source_check_date": "2026-06-10",
                "signal_type": "media/curated list",
                "signal_strength": "major media list",
                "evidence_note": "Included in a configured source list.",
                "content_description": ["Русский текст"],
            },
        ).to_dict()

        self.assertEqual(mention["content_description"], "")
        self.assertEqual(mention["content_description_rejection_reason"], "non-string value rejected")

    def test_empty_content_description_keeps_existing_fallback_contract(self):
        mention = SourceMention.from_recorded(
            {"name": "Recorded Source", "family": "media/curated list"},
            {
                "title": "Useful Book",
                "primary_authors": ["Alex Example"],
                "source_url": "https://example.test/list/useful-book",
                "source_check_date": "2026-06-10",
                "signal_type": "media/curated list",
                "signal_strength": "major media list",
                "evidence_note": "Included in a configured source list.",
                "content_description": "",
            },
        ).to_dict()

        self.assertEqual(mention["content_description"], "")

    def test_search_notes_remain_notes_not_display_metadata(self):
        mention = SourceMention.from_candidate(
            {
                "name": "Fixture Source",
                "family": "media/curated list",
                "url": "https://example.test/list",
            },
            {
                "title": "Original Title",
                "primary_authors": ["Original Author"],
                "source_url": "https://example.test/list/original",
                "signal_strength": "major media list",
                "evidence_note": "Included in a configured source list.",
                "search_translation": "Generated Russian Search",
                "author_transliteration_note": "Generated Author Search",
            },
            "2026-06-10",
        ).to_dict()

        self.assertEqual(mention["title"], "Original Title")
        self.assertEqual(mention["primary_authors"], ["Original Author"])
        self.assertEqual(mention["search_translation"], "Generated Russian Search")
        self.assertEqual(mention["author_transliteration_note"], "Generated Author Search")
