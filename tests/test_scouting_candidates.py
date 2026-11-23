import unittest

from autoscout import pipeline as autoscout_main
from autoscout.candidate_schema import CANDIDATE_MENTION_FIELD_SCHEMA
from autoscout.candidate_schema import candidate_fields_from_mention
from autoscout.candidate_schema import source_mention_fields_from_candidate
from autoscout.candidate_verification import apply_reentry_note
from autoscout.candidate_verification import apply_russian_edition_verification
from autoscout.pipeline import build_scouting_candidates
from autoscout.scouting_candidates import SCHEMA_DECLARED_ONE_SIDE_ONLY_FIELDS
from autoscout.scouting_candidates import ScoutingCandidate
from autoscout.scouting_candidates import append_source_evidence
from autoscout.weekly_report_row import editor_row_from_candidate


class ScoutingCandidateRecordTests(unittest.TestCase):
    def test_scouting_candidate_record_preserves_evidence_for_report_row(self):
        mention = {
            "title": "Useful Book",
            "primary_authors": ["Alex Example"],
            "candidate_category": "business",
            "publisher_fit": "clear fit",
            "russian_edition_status": "not found in Russian",
            "visible_risk_status": "no visible risk",
            "source_name": "Fixture Awards",
            "source_family": "award",
            "source_url": "https://example.test/awards/useful-book",
            "source_check_date": "2026-06-10",
            "signal_strength": "winner",
            "evidence_note": "Won a configured award.",
        }
        evidence = autoscout_main.source_evidence(mention)

        candidate = ScoutingCandidate.from_source_mention(
            mention,
            identity_key="useful book|alex example",
            evidence=evidence,
            russian_search_notes={},
        ).to_dict()
        autoscout_main.add_signal_aggregation(candidate)

        row = editor_row_from_candidate(candidate)
        self.assertEqual(row["Title"], "Useful Book")
        self.assertEqual(row["Source"], "Fixture Awards")
        self.assertEqual(row["Why it entered the list"], "Won a configured award.")

    def test_duplicate_candidate_append_keeps_one_candidate_with_two_links(self):
        candidate = ScoutingCandidate.from_source_mention(
            {
                "title": "Useful Book",
                "primary_authors": ["Alex Example"],
                "source_name": "Source One",
                "source_family": "media/curated list",
                "source_url": "https://example.test/one",
                "source_check_date": "2026-06-10",
                "signal_strength": "major media list",
                "evidence_note": "First mention.",
            },
            identity_key="useful book|alex example",
            evidence={
                "source_name": "Source One",
                "source_family": "media/curated list",
                "source_url": "https://example.test/one",
                "source_check_date": "2026-06-10",
                "signal_strength": "major media list",
                "evidence_note": "First mention.",
            },
            russian_search_notes={},
        ).to_dict()

        append_source_evidence(
            candidate,
            {
                "source_name": "Source Two",
                "source_family": "media/curated list",
                "source_url": "https://example.test/two",
                "source_check_date": "2026-06-10",
                "signal_strength": "major media list",
                "evidence_note": "Second mention.",
            },
        )

        self.assertEqual(candidate["identity_key"], "useful book|alex example")
        self.assertEqual(len(candidate["source_evidence"]), 2)

    def test_found_in_russian_exclusion_remains_internal(self):
        mention = {
            "title": "Already Russian",
            "primary_authors": ["Alex Example"],
            "russian_edition_status": "found in Russian",
        }

        self.assertEqual(autoscout_main.exclusion_reason_for(mention), "found in Russian")

    def test_schema_drives_candidate_and_mention_projections(self):
        mention = {
            "title": "Shared Shape",
            "primary_authors": ["Alex Example"],
            "candidate_category": "history",
            "publisher": "Shape Press",
            "publication_year": "2026",
            "source_url": "https://example.test/source",
            "source_check_date": "2026-06-10",
            "signal_type": "media/curated list",
            "signal_strength": "minor mention",
            "evidence_note": "Listed in a source.",
        }
        candidate = ScoutingCandidate.from_source_mention(mention).to_dict()
        projected_mention = source_mention_fields_from_candidate(
            {"name": "Fixture", "family": "media/curated list"}, candidate, "2026-06-10"
        )

        schema_fields = {field.name for field in CANDIDATE_MENTION_FIELD_SCHEMA}
        mention_to_candidate_fields = {
            field.name
            for field in CANDIDATE_MENTION_FIELD_SCHEMA
            if field.mention_to_candidate and not field.candidate_omit_when_empty
        }
        mergeable_fields = {field.name for field in CANDIDATE_MENTION_FIELD_SCHEMA if field.merge_to_existing_candidate}
        self.assertEqual(set(candidate_fields_from_mention(mention)), mention_to_candidate_fields)
        self.assertNotIn("description_provenance", candidate)
        self.assertEqual(
            candidate_fields_from_mention({**mention, "description_provenance": "grounded"})["description_provenance"],
            "grounded",
        )
        self.assertTrue(set(projected_mention).issubset(schema_fields))
        self.assertNotIn("isbn", mergeable_fields)
        self.assertIn("source_url", SCHEMA_DECLARED_ONE_SIDE_ONLY_FIELDS)
        self.assertIn("identity_key", SCHEMA_DECLARED_ONE_SIDE_ONLY_FIELDS)

    def test_duplicate_merge_backfills_isbn_from_later_mention(self):
        """When an earlier mention has no ISBN, a later mention's ISBN is
        backfilled into the merged candidate."""
        source_mentions = [
            {
                "title": "Same Work",
                "primary_authors": ["Alex Example"],
                "source_name": "Source One",
                "source_family": "media",
                "source_url": "https://example.test/one",
                "source_check_date": "2026-06-10",
                "signal_strength": "minor mention",
                "evidence_note": "First mention.",
            },
            {
                "title": "Same Work",
                "primary_authors": ["Alex Example"],
                "isbn": "9780000000000",
                "source_name": "Source Two",
                "source_family": "media",
                "source_url": "https://example.test/two",
                "source_check_date": "2026-06-10",
                "signal_strength": "minor mention",
                "evidence_note": "Second mention.",
            },
        ]

        candidates, _exclusions = build_scouting_candidates(source_mentions, "2026-06-10")

        self.assertEqual(candidates[0].get("isbn"), "9780000000000")

    def test_title_with_and_without_subtitle_aggregates_as_one_work(self):
        common = {
            "primary_authors": ["Nate Silver"],
            "candidate_category": "business",
            "source_family": "media/curated list",
            "source_check_date": "2026-07-11",
            "signal_strength": "major media list",
            "evidence_note": "Listed by a configured source.",
        }
        source_mentions = [
            {
                **common,
                "title": "On the Edge",
                "source_name": "Source One",
                "source_url": "https://example.test/one",
            },
            {
                **common,
                "title": "On the Edge: The Art of Risking Everything",
                "source_name": "Source Two",
                "source_url": "https://example.test/two",
            },
        ]

        candidates, _exclusions = build_scouting_candidates(source_mentions, "2026-07-11")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].identity_key(), "on the edge|nate silver")
        self.assertEqual(len(candidates[0].get("source_evidence")), 2)

    def test_entity_answers_identity_aggregation_and_exclusion(self):
        mention = {
            "title": "  The Signal Book! ",
            "primary_authors": ["Dana Writer"],
            "candidate_category": "history",
            "source_name": "Awards",
            "source_family": "award",
            "source_url": "https://example.test/award",
            "source_check_date": "2026-06-10",
            "signal_strength": "winner",
            "evidence_note": "Won coverage first.",
        }
        candidate = ScoutingCandidate.from_source_mention(mention)
        candidate.append_source_evidence(
            {
                "source_name": "Awards Follow-up",
                "source_family": "award",
                "source_url": "https://example.test/award-2",
                "source_check_date": "2026-06-10",
                "signal_strength": "winner",
                "evidence_note": "Same-family duplicate winner.",
            }
        )
        candidate.append_source_evidence(
            {
                "source_name": "Reviews",
                "source_family": "media/curated list",
                "source_url": "https://example.test/review",
                "source_check_date": "2026-06-10",
                "signal_strength": "shortlist",
                "evidence_note": "Second independent signal.",
            }
        )
        candidate.apply_signal_aggregation()

        self.assertEqual(candidate.identity_key(), "the signal book|dana writer")
        self.assertEqual(candidate.get("independent_signal_count"), 2)
        self.assertEqual(candidate.primary_signal()["source_name"], "Awards")
        self.assertEqual(ScoutingCandidate.exclusion_reason_for_mapping({"candidate_category": "fiction"}), "fiction")

    def test_priority_preserves_existing_scope_condition_for_clear_visible_risk_floor(self):
        candidate = ScoutingCandidate.from_mapping(
            {
                "identity_key": "risk|writer",
                "title": "Risk Book",
                "primary_authors": ["Dana Writer"],
                "publisher_fit": "clear fit",
                "russian_edition_status": "not found in Russian",
                "visible_risk_status": "clear visible risk",
                "risk_signal_scope": "author-context",
                "source_evidence": [
                    {"source_family": "award", "signal_strength": "winner", "evidence_note": "Winner."},
                    {"source_family": "media", "signal_strength": "shortlist", "evidence_note": "Second signal."},
                ],
            }
        )
        candidate.apply_signal_aggregation()

        self.assertEqual(candidate.get("priority_label"), "B")

    def test_priority_and_visible_risk_are_entity_behaviour(self):
        candidate = ScoutingCandidate.from_mapping(
            {
                "identity_key": "risk|writer",
                "title": "Risk Book",
                "primary_authors": ["Dana Writer"],
                "publisher_fit": "clear fit",
                "russian_edition_status": "not found in Russian",
                "risk_signal_scope": "book-content",
                "risk_evidence_url": "https://example.test/risk",
                "public_data_status": "sufficient",
                "source_evidence": [
                    {"source_family": "award", "signal_strength": "winner", "evidence_note": "A war chapter is cited."},
                    {"source_family": "media", "signal_strength": "shortlist", "evidence_note": "Second signal."},
                ],
            }
        )
        candidate.apply_visible_risk_screening()
        candidate.apply_signal_aggregation()

        self.assertEqual(candidate.get("visible_risk_status"), "clear visible risk")
        self.assertEqual(candidate.priority_label(), "D")
        self.assertEqual(candidate.sort_key()[0], 3)

    def test_priority_label_a_requires_two_independent_signals_clear_fit_and_no_risk(self):
        candidate = ScoutingCandidate.from_mapping(
            {
                "identity_key": "top pick|writer",
                "title": "Top Pick",
                "primary_authors": ["Dana Writer"],
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

    def test_priority_label_b_requires_two_independent_signals_and_shortlist_or_better_strength(self):
        candidate = ScoutingCandidate.from_mapping(
            {
                "identity_key": "strong contender|writer",
                "title": "Strong Contender",
                "primary_authors": ["Dana Writer"],
                "publisher_fit": "unclear fit",
                "russian_edition_status": "not found in Russian",
                "source_evidence": [
                    {"source_family": "award", "signal_strength": "shortlist", "evidence_note": "Shortlisted."},
                    {"source_family": "media", "signal_strength": "minor mention", "evidence_note": "Second signal."},
                ],
            }
        )
        candidate.apply_signal_aggregation()

        self.assertEqual(candidate.priority_label(), "B")

    def test_priority_label_c_requires_only_one_independent_signal(self):
        candidate = ScoutingCandidate.from_mapping(
            {
                "identity_key": "lone signal|writer",
                "title": "Lone Signal",
                "primary_authors": ["Dana Writer"],
                "source_evidence": [
                    {"source_family": "media", "signal_strength": "minor mention", "evidence_note": "One mention."},
                ],
            }
        )
        candidate.apply_signal_aggregation()

        self.assertEqual(candidate.priority_label(), "C")

    def test_priority_label_d_when_no_independent_signal_evidence(self):
        candidate = ScoutingCandidate.from_mapping(
            {
                "identity_key": "no signal|writer",
                "title": "No Signal",
                "primary_authors": ["Dana Writer"],
                "source_evidence": [],
            }
        )
        candidate.apply_signal_aggregation()

        self.assertEqual(candidate.priority_label(), "D")

    def test_sort_key_breaks_ties_on_signal_strength_within_same_priority_and_signal_count(self):
        winner = ScoutingCandidate.from_mapping(
            {
                "identity_key": "winner book|writer",
                "title": "Winner Book",
                "primary_authors": ["Dana Writer"],
                "source_evidence": [
                    {"source_family": "media", "signal_strength": "winner", "evidence_note": "Won an award."},
                ],
            }
        )
        winner.apply_signal_aggregation()
        minor_mention = ScoutingCandidate.from_mapping(
            {
                "identity_key": "minor mention book|writer",
                "title": "Minor Mention Book",
                "primary_authors": ["Dana Writer"],
                "source_evidence": [
                    {"source_family": "media", "signal_strength": "minor mention", "evidence_note": "Briefly noted."},
                ],
            }
        )
        minor_mention.apply_signal_aggregation()

        # Both land in the same priority tier (C) with the same Independent Signal
        # count of 1; only Signal Strength differs, and that is the tie-break.
        self.assertEqual(winner.priority_label(), "C")
        self.assertEqual(minor_mention.priority_label(), "C")
        self.assertLess(winner.sort_key(), minor_mention.sort_key())

    def test_visible_risk_screening_requires_text_check_for_weak_author_context_signal(self):
        candidate = ScoutingCandidate.from_mapping(
            {
                "identity_key": "shadow claim|writer",
                "title": "Shadow Claim",
                "primary_authors": ["Dana Writer"],
                "risk_signal_scope": "author-context",
                "public_data_status": "sufficient",
                "source_evidence": [
                    {
                        "source_family": "media",
                        "signal_strength": "minor mention",
                        "evidence_note": ("An unverified author interview claim links the book to the war in passing."),
                    },
                ],
            }
        )
        candidate.apply_visible_risk_screening()

        self.assertEqual(candidate.get("visible_risk_status"), "text check required")
        self.assertEqual(candidate.get("visible_risk_category"), "war/anti-war/Russian Armed Forces")
        self.assertIn("weak, indirect, or unverified", candidate.get("visible_risk_comment"))

    def test_visible_risk_screening_flags_text_check_when_public_data_is_insufficient(self):
        candidate = ScoutingCandidate.from_mapping(
            {
                "identity_key": "thin record|writer",
                "title": "Thin Record",
                "primary_authors": ["Dana Writer"],
                "public_data_status": "insufficient",
                "source_evidence": [
                    {"source_family": "media", "signal_strength": "minor mention", "evidence_note": "Brief listing."},
                ],
            }
        )
        candidate.apply_visible_risk_screening()

        self.assertEqual(candidate.get("visible_risk_status"), "text check required")
        self.assertEqual(candidate.get("visible_risk_category"), "")
        self.assertIn("insufficient", candidate.get("visible_risk_comment"))

    def test_visible_risk_screening_clears_when_public_data_is_sufficient_and_no_category_matches(self):
        candidate = ScoutingCandidate.from_mapping(
            {
                "identity_key": "clean record|writer",
                "title": "Clean Record",
                "primary_authors": ["Dana Writer"],
                "public_data_status": "sufficient",
                "source_evidence": [
                    {"source_family": "media", "signal_strength": "minor mention", "evidence_note": "Brief listing."},
                ],
            }
        )
        candidate.apply_visible_risk_screening()

        self.assertEqual(candidate.get("visible_risk_status"), "no visible risk")
        self.assertEqual(candidate.get("visible_risk_category"), "")
        self.assertIn("No configured MVP risk signal found", candidate.get("visible_risk_comment"))

    def test_russian_status_transition_reentry_and_report_row_are_entity_behaviour(self):
        candidate = ScoutingCandidate.from_mapping(
            {
                "identity_key": "translated|author",
                "title": "Translated",
                "primary_authors": ["Author"],
                "source_evidence": [
                    {
                        "source_name": "Source",
                        "source_family": "media",
                        "source_url": "https://example.test/source",
                        "source_check_date": "2026-06-10",
                        "signal_strength": "minor mention",
                        "evidence_note": "Mentioned.",
                    }
                ],
            }
        )
        apply_russian_edition_verification(
            candidate.data,
            {
                "check_coverage": "complete",
                "provider_outcome": "ambiguous",
                "confidence": "low",
                "checked_sources": ["Fixture"],
                "checked_at": "2026-06-10",
                "query_timestamp": "2026-06-10",
                "evidence_urls": ["https://example.test/ru"],
                "providers": [
                    {
                        "name": "Fixture",
                        "source_type": "russian-retailer",
                        "status": "checked",
                        "outcome": "ambiguous",
                        "reason": "ambiguous title or author match",
                        "checked_at": "2026-06-10",
                        "evidence_urls": ["https://example.test/ru"],
                        "evidence": [
                            {
                                "match_type": "ambiguous author/title match",
                                "language": "Russian",
                                "url": "https://example.test/ru",
                            }
                        ],
                        "result_count": 1,
                    }
                ],
                "reason": "Fixture: ambiguous title or author match",
            },
        )
        apply_reentry_note(candidate.data, {"translated|author": {"exclusion_reason": "found in Russian"}})
        candidate.apply_signal_aggregation()

        row = candidate.editor_row()
        self.assertEqual(candidate.get("russian_edition_status"), "unclear")
        self.assertEqual(candidate.get("reentry_reason"), "Russian status changed from found in Russian to unclear")
        self.assertEqual(row["Source"], "Source")
        self.assertEqual(row["Why it entered the list"], "Mentioned.")


def _screened(**fields):
    candidate = ScoutingCandidate.from_mapping(
        {
            "identity_key": "screened|writer",
            "title": "Screened Book",
            "primary_authors": ["Dana Writer"],
            **fields,
        }
    )
    candidate.apply_visible_risk_screening()
    return candidate


def _with_note(note):
    return {"source_evidence": [{"source_family": "media", "signal_strength": "minor mention", "evidence_note": note}]}


class VisibleRiskKeywordMatchingTests(unittest.TestCase):
    """Keywords match whole words, not substrings (issue #714)."""

    def test_award_is_not_a_war_category_match(self):
        candidate = _screened(**_with_note("National Book Award for Nonfiction 2025 longlist"))

        self.assertEqual(candidate.visible_risk_category(), "")
        self.assertFalse(candidate.get("visible_risk_status"))

    def test_substrings_inside_other_words_do_not_match(self):
        for note in (
            "Sold at every drugstore.",
            "A rewarding read with warmth.",
            "Edward Warren looks forward.",
            "Awards season favourite.",
        ):
            with self.subTest(note=note):
                self.assertEqual(_screened(**_with_note(note)).visible_risk_category(), "")

    def test_whole_words_plurals_and_preserved_forms_still_match(self):
        cases = {
            "A history of the war.": "war/anti-war/Russian Armed Forces",
            "Two world wars, retold.": "war/anti-war/Russian Armed Forces",
            "An anti-war memoir.": "war/anti-war/Russian Armed Forces",
            "Putin's inner circle.": "Russia/politics",
            "Russian politics after 1991.": "Russia/politics",
            "A Russian family saga.": "Russia/politics",
            "How the Nazis rose.": "WWII/USSR/Nazi comparison",
            "LGBTQ+ histories.": "non-traditional relationships/gender transition/rejection of childbearing",
            "The science of drugs.": "drugs/microdosing/psychoactive substances",
            "Psychedelics and the brain.": "drugs/microdosing/psychoactive substances",
            "Wartime propaganda and modern warfare.": "war/anti-war/Russian Armed Forces",
            "The rise of Nazism.": "WWII/USSR/Nazi comparison",
        }
        for note, category in cases.items():
            with self.subTest(note=note):
                self.assertEqual(_screened(**_with_note(note)).visible_risk_category(), category)


class VisibleRiskScreenCoverageTests(unittest.TestCase):
    def test_keyword_hint_without_collector_fields_requires_a_text_check(self):
        candidate = _screened(**_with_note("A history of the war."))

        self.assertEqual(candidate.get("visible_risk_status"), "text check required")
        self.assertEqual(candidate.get("visible_risk_category"), "war/anti-war/Russian Armed Forces")
        self.assertEqual(candidate.get("visible_risk_screen_coverage")["coverage"], "partial")

    def test_placeholder_evidence_note_is_not_evidence(self):
        candidate = _screened(**_with_note("Configured source mention."))

        self.assertEqual(candidate.get("visible_risk_screen_coverage")["coverage"], "partial")
        self.assertEqual(candidate.get("visible_risk_screen_coverage")["evidence_fields"], ["title"])
        self.assertFalse(candidate.get("visible_risk_status"))

    def test_description_fallback_text_is_not_evidence(self):
        candidate = _screened(content_description="Описание требует дополнительной проверки")

        self.assertEqual(candidate.get("visible_risk_screen_coverage")["coverage"], "partial")
        self.assertEqual(candidate.get("visible_risk_screen_coverage")["evidence_fields"], ["title"])
        self.assertFalse(candidate.get("visible_risk_status"))

    def test_russian_content_description_is_a_screening_input(self):
        candidate = _screened(content_description="Книга о жизни портового города и его жителей.")

        self.assertEqual(
            candidate.get("visible_risk_screen_coverage")["evidence_fields"], ["title", "content_description"]
        )
        self.assertEqual(candidate.get("visible_risk_screen_coverage")["coverage"], "screened")

    def test_evidence_notes_alone_do_not_conclude(self):
        candidate = _screened(**_with_note("Included in a curated list of new history books."))

        self.assertEqual(candidate.get("visible_risk_screen_coverage")["coverage"], "partial")
        self.assertFalse(candidate.get("visible_risk_status"))

    def test_a_description_without_a_title_is_partial(self):
        from autoscout.visible_risk_coverage import coverage_for_evidence
        from autoscout.visible_risk_coverage import validate_visible_risk_screen_coverage

        record = coverage_for_evidence(["short_description", "evidence_notes"])

        self.assertEqual(record["coverage"], "partial")
        validate_visible_risk_screen_coverage(record)
        with self.assertRaises(ValueError):
            validate_visible_risk_screen_coverage(
                {
                    **record,
                    "coverage": "screened",
                    "reason": coverage_for_evidence(["title", "short_description"])["reason"],
                }
            )

    def test_english_content_description_counts_as_evidence(self):
        candidate = _screened(content_description="A history of dock workers.")

        self.assertEqual(
            candidate.get("visible_risk_screen_coverage")["evidence_fields"], ["title", "content_description"]
        )
        self.assertEqual(candidate.get("visible_risk_status"), "no visible risk")

    def test_supplied_status_is_kept_and_the_screen_records_not_run(self):
        candidate = _screened(visible_risk_status="no visible risk", **_with_note("A history of the war."))

        self.assertEqual(candidate.get("visible_risk_status"), "no visible risk")
        self.assertEqual(
            candidate.get("visible_risk_screen_coverage"),
            {
                "coverage": "not_run",
                "evidence_fields": [],
                "reason": "A Visible Risk status was supplied with the Source Mention; the screen did not run.",
            },
        )

    def test_a_screen_error_is_recorded_as_failed_coverage(self):
        candidate = _screened(source_evidence=[None])

        self.assertEqual(
            candidate.get("visible_risk_screen_coverage"),
            {"coverage": "failed", "evidence_fields": [], "reason": "The screen raised AttributeError."},
        )
        self.assertEqual(candidate.get("visible_risk_status"), "")

    def test_a_collector_supplied_coverage_record_never_reaches_the_candidate(self):
        mention = {
            "title": "Claimed Coverage",
            "primary_authors": ["Dana Writer"],
            "visible_risk_screen_coverage": {"coverage": "screened", "evidence_fields": [], "reason": "claimed"},
        }

        self.assertNotIn("visible_risk_screen_coverage", candidate_fields_from_mention(mention))

    def test_coverage_vocabulary_is_closed(self):
        from autoscout.visible_risk_coverage import coverage_for_evidence
        from autoscout.visible_risk_coverage import validate_visible_risk_screen_coverage

        validate_visible_risk_screen_coverage(coverage_for_evidence(["title", "short_description"]))
        invalid_records = (
            {"coverage": "complete", "evidence_fields": ["title"], "reason": "x"},
            {"coverage": "screened", "evidence_fields": ["title"], "reason": coverage_for_evidence(["x"])["reason"]},
            {
                **coverage_for_evidence(["title", "short_description"]),
                "evidence_fields": ["short_description", "title"],
            },
            {**coverage_for_evidence(["title"]), "evidence_fields": ["title", "isbn"]},
            {"coverage": "not_run", "evidence_fields": ["title"], "reason": "x"},
            {"coverage": "failed", "evidence_fields": [], "reason": "The screen raised a long story."},
            {**coverage_for_evidence(["title"]), "extra": True},
        )
        for record in invalid_records:
            with self.subTest(record=record), self.assertRaises(ValueError):
                validate_visible_risk_screen_coverage(record)


class VisibleRiskRescreenTests(unittest.TestCase):
    """A later Content Description lets a partial screen conclude (issue #714)."""

    def test_partial_candidate_concludes_once_a_description_arrives(self):
        candidate = _screened(priority_label="C", **_with_note("Included in a curated list."))
        self.assertEqual(candidate.get("visible_risk_screen_coverage")["coverage"], "partial")

        candidate.data["content_description"] = "Книга о жизни портового города и его жителей."

        self.assertTrue(candidate.rescreen_visible_risk())
        self.assertEqual(candidate.get("visible_risk_status"), "no visible risk")
        self.assertEqual(candidate.get("visible_risk_screen_coverage")["coverage"], "screened")
        self.assertEqual(candidate.get("priority_label"), "C")

    def test_rescreen_without_new_evidence_changes_nothing(self):
        candidate = _screened(**_with_note("Included in a curated list."))

        self.assertFalse(candidate.rescreen_visible_risk())

    def test_only_partial_coverage_is_screened_again(self):
        supplied = _screened(visible_risk_status="no visible risk")
        screened = _screened(short_description="A social history of a port city.")
        for candidate in (supplied, screened):
            with self.subTest(coverage=candidate.get("visible_risk_screen_coverage")["coverage"]):
                candidate.data["content_description"] = "Книга о том, как war меняет язык."
                self.assertFalse(candidate.rescreen_visible_risk())

    def test_a_new_hint_takes_an_a_priority_off_a(self):
        candidate = _screened(priority_label="A", **_with_note("Included in a curated list."))
        candidate.data["content_description"] = "Книга о том, как war меняет язык."

        self.assertTrue(candidate.rescreen_visible_risk())
        self.assertEqual(candidate.get("visible_risk_status"), "text check required")
        self.assertNotEqual(candidate.get("priority_label"), "A")
