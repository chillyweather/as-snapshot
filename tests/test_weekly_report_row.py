from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from autoscout.weekly_report_row import EDITOR_FIELDNAMES
from autoscout.weekly_report_row import editor_row_from_candidate
from autoscout.weekly_report_row import evidence_links
from autoscout.weekly_report_row import publisher_year
from autoscout.weekly_report_row import write_editor_csv


def _has_cyrillic(value: str) -> bool:
    return any("А" <= char <= "я" or char == "ё" or char == "Ё" for char in value)


def _candidate(**overrides):
    base = {
        "title": "Test Title",
        "primary_authors": ["Author One"],
        "candidate_category": "biography",
        "publisher": "Test Publisher",
        "publication_year": "2024",
        "independent_signal_count": 1,
        "signal_summary": "1 independent signal: media.",
        "priority_label": "C",
        "short_description": "A test book.",
        "russian_edition_status": "not found in Russian",
        "russian_edition_url": "",
        "visible_risk_status": "no visible risk",
        "visible_risk_comment": "",
        "editor_status": "",
        "editor_note": "",
        "source_evidence": [
            {
                "source_name": "Source A",
                "source_family": "media",
                "source_url": "https://example.com/source-a",
                "evidence_url": "https://example.com/evidence-a",
                "source_check_date": "2024-06-01",
                "signal_strength": "major media list",
                "evidence_note": "Featured in summer roundup.",
            }
        ],
        "primary_signal": {
            "source_name": "Source A",
            "source_family": "media",
            "source_url": "https://example.com/source-a",
            "evidence_url": "https://example.com/evidence-a",
            "source_check_date": "2024-06-01",
            "signal_strength": "major media list",
            "evidence_note": "Featured in summer roundup.",
        },
    }
    base.update(overrides)
    return base


class EditorFieldnamesTests(unittest.TestCase):
    def test_field_count(self):
        self.assertEqual(len(EDITOR_FIELDNAMES), 29)

    def test_field_order(self):
        self.assertEqual(EDITOR_FIELDNAMES[0], "Title")
        self.assertEqual(EDITOR_FIELDNAMES[1], "Author")
        self.assertEqual(EDITOR_FIELDNAMES[2], "Annotation")
        self.assertEqual(EDITOR_FIELDNAMES[3], "Description")
        self.assertEqual(EDITOR_FIELDNAMES[4], "Description status")
        self.assertEqual(EDITOR_FIELDNAMES[5], "Signal count")
        self.assertEqual(EDITOR_FIELDNAMES[6], "Visible-risk status")
        self.assertEqual(EDITOR_FIELDNAMES[7], "Risk comment")
        self.assertEqual(EDITOR_FIELDNAMES[8], "English title")
        self.assertEqual(EDITOR_FIELDNAMES[9], "English author display")
        self.assertEqual(EDITOR_FIELDNAMES[10], "English annotation")
        self.assertEqual(EDITOR_FIELDNAMES[11], "Check date")
        self.assertEqual(EDITOR_FIELDNAMES[12], "Source")
        self.assertEqual(EDITOR_FIELDNAMES[13], "Source URL")
        self.assertEqual(EDITOR_FIELDNAMES[14], "Publisher / year")
        self.assertEqual(EDITOR_FIELDNAMES[15], "Category / topic")
        self.assertEqual(EDITOR_FIELDNAMES[16], "Why it entered the list")
        self.assertEqual(EDITOR_FIELDNAMES[17], "Signal summary")
        self.assertEqual(EDITOR_FIELDNAMES[18], "Priority label")
        self.assertEqual(EDITOR_FIELDNAMES[19], "Russian-edition status")
        self.assertEqual(EDITOR_FIELDNAMES[20], "Russian-edition URL")
        self.assertEqual(EDITOR_FIELDNAMES[21], "Links")
        self.assertEqual(EDITOR_FIELDNAMES[22], "Update delta")
        self.assertEqual(EDITOR_FIELDNAMES[23], "Lifecycle")
        self.assertEqual(EDITOR_FIELDNAMES[24], "Review context")
        self.assertEqual(EDITOR_FIELDNAMES[25], "Taste profile notes")
        self.assertEqual(EDITOR_FIELDNAMES[26], "Editor status")
        self.assertEqual(EDITOR_FIELDNAMES[27], "Editor note")
        self.assertEqual(EDITOR_FIELDNAMES[28], "Candidate ID")


class PrimarySignalFieldTests(unittest.TestCase):
    def test_check_date_from_primary_signal(self):
        candidate = _candidate(
            primary_signal={
                "source_name": "Primary Source",
                "source_url": "https://example.com/primary",
                "source_check_date": "2024-07-15",
                "evidence_note": "Primary note.",
            },
            source_evidence=[
                {
                    "source_name": "Other Source",
                    "source_url": "https://example.com/other",
                    "source_check_date": "2024-01-01",
                    "evidence_note": "Other note.",
                },
                {
                    "source_name": "Primary Source",
                    "source_url": "https://example.com/primary",
                    "source_check_date": "2024-07-15",
                    "evidence_note": "Primary note.",
                },
            ],
        )
        row = editor_row_from_candidate(candidate)
        self.assertEqual(row["Check date"], "2024-07-15")

    def test_source_from_primary_signal(self):
        candidate = _candidate(
            primary_signal={
                "source_name": "Primary Source",
                "source_url": "https://example.com/primary",
                "source_check_date": "2024-07-15",
                "evidence_note": "Primary note.",
            },
            source_evidence=[
                {
                    "source_name": "Other Source",
                    "source_url": "https://example.com/other",
                    "source_check_date": "2024-01-01",
                    "evidence_note": "Other note.",
                },
            ],
        )
        row = editor_row_from_candidate(candidate)
        self.assertEqual(row["Source"], "Primary Source")

    def test_source_url_from_primary_signal(self):
        candidate = _candidate(
            primary_signal={
                "source_name": "Primary Source",
                "source_url": "https://example.com/primary",
                "source_check_date": "2024-07-15",
                "evidence_note": "Primary note.",
            },
            source_evidence=[
                {
                    "source_name": "Other Source",
                    "source_url": "https://example.com/other",
                    "source_check_date": "2024-01-01",
                    "evidence_note": "Other note.",
                },
            ],
        )
        row = editor_row_from_candidate(candidate)
        self.assertEqual(row["Source URL"], "https://example.com/primary")

    def test_why_it_entered_from_primary_signal_evidence_note(self):
        candidate = _candidate(
            primary_signal={
                "source_name": "Primary Source",
                "source_url": "https://example.com/primary",
                "source_check_date": "2024-07-15",
                "evidence_note": "Award shortlist mention.",
            },
            source_evidence=[
                {
                    "source_name": "Other Source",
                    "source_url": "https://example.com/other",
                    "source_check_date": "2024-01-01",
                    "evidence_note": "Media roundup.",
                },
            ],
        )
        row = editor_row_from_candidate(candidate)
        self.assertEqual(row["Why it entered the list"], "Award shortlist mention.")

    def test_one_row_per_candidate_not_per_signal(self):
        candidate = _candidate(
            source_evidence=[
                {
                    "source_name": "Source A",
                    "source_url": "https://example.com/a",
                    "source_check_date": "2024-06-01",
                    "evidence_note": "Note A.",
                },
                {
                    "source_name": "Source B",
                    "source_url": "https://example.com/b",
                    "source_check_date": "2024-06-02",
                    "evidence_note": "Note B.",
                },
                {
                    "source_name": "Source C",
                    "source_url": "https://example.com/c",
                    "source_check_date": "2024-06-03",
                    "evidence_note": "Note C.",
                },
            ],
        )
        row = editor_row_from_candidate(candidate)
        self.assertIsInstance(row, dict)
        self.assertEqual(row["Title"], "Test Title")

    def test_falls_back_to_first_evidence_when_no_primary_signal(self):
        candidate = _candidate(
            primary_signal=None,
            source_evidence=[
                {
                    "source_name": "Fallback Source",
                    "source_url": "https://example.com/fallback",
                    "source_check_date": "2024-03-01",
                    "evidence_note": "Fallback note.",
                },
            ],
        )
        candidate.pop("primary_signal")
        row = editor_row_from_candidate(candidate)
        self.assertEqual(row["Check date"], "2024-03-01")
        self.assertEqual(row["Source"], "Fallback Source")
        self.assertEqual(row["Source URL"], "https://example.com/fallback")
        self.assertEqual(row["Why it entered the list"], "Fallback note.")


class FoundInRussianExclusionBoundaryTests(unittest.TestCase):
    def test_module_formats_found_in_russian_candidate_without_excluding(self):
        candidate = _candidate(
            russian_edition_status="found in Russian",
            russian_edition_url="https://example.ru/book",
        )
        row = editor_row_from_candidate(candidate)
        self.assertEqual(row["Russian-edition status"], "found in Russian")
        self.assertEqual(row["Russian-edition URL"], "https://example.ru/book")
        self.assertEqual(row["Title"], "Test Title")

    def test_module_formats_any_candidate_it_receives(self):
        candidates = [
            _candidate(title="Book A", russian_edition_status="not found in Russian"),
            _candidate(title="Book B", russian_edition_status="found in Russian"),
            _candidate(title="Book C", russian_edition_status="unclear"),
        ]
        rows = [editor_row_from_candidate(c) for c in candidates]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["Title"], "Book A")
        self.assertEqual(rows[1]["Title"], "Book B")
        self.assertEqual(rows[2]["Title"], "Book C")

    def test_module_has_no_exclusion_logic(self):
        candidate = _candidate(
            russian_edition_status="found in Russian",
            candidate_category="fiction",
        )
        row = editor_row_from_candidate(candidate)
        self.assertEqual(row["Russian-edition status"], "found in Russian")
        self.assertEqual(row["Category / topic"], "fiction")


class DisplayFieldFormattingTests(unittest.TestCase):
    def test_publisher_and_year_both_present(self):
        candidate = _candidate(publisher="Penguin", publication_year="2024")
        self.assertEqual(publisher_year(candidate), "Penguin / 2024")

    def test_publisher_only(self):
        candidate = _candidate(publisher="Penguin", publication_year="")
        self.assertEqual(publisher_year(candidate), "Penguin")

    def test_year_only(self):
        candidate = _candidate(publisher="", publication_year="2024")
        self.assertEqual(publisher_year(candidate), "2024")

    def test_neither_publisher_nor_year(self):
        candidate = _candidate(publisher="", publication_year="")
        self.assertEqual(publisher_year(candidate), "")

    def test_publisher_year_in_row(self):
        candidate = _candidate(publisher="Harper", publication_year="2023")
        row = editor_row_from_candidate(candidate)
        self.assertEqual(row["Publisher / year"], "Harper / 2023")

    def test_multiple_authors_joined(self):
        candidate = _candidate(primary_authors=["Alice Smith", "Bob Jones", "Carol White"])
        row = editor_row_from_candidate(candidate)
        self.assertEqual(row["Author"], "Alice Smith, Bob Jones, Carol White")

    def test_single_author(self):
        candidate = _candidate(primary_authors=["Solo Author"])
        row = editor_row_from_candidate(candidate)
        self.assertEqual(row["Author"], "Solo Author")

    def test_empty_authors(self):
        candidate = _candidate(primary_authors=[])
        row = editor_row_from_candidate(candidate)
        self.assertEqual(row["Author"], "")

    def test_english_helper_fields_do_not_replace_original_metadata(self):
        candidate = _candidate(
            title="原題",
            primary_authors=["著者 名"],
            english_title="Original Title",
            english_author_display="Author Name",
            english_annotation="English-language scouting note.",
        )
        row = editor_row_from_candidate(candidate)
        self.assertEqual(row["Title"], "原題")
        self.assertEqual(row["Author"], "著者 名")
        self.assertEqual(row["English title"], "Original Title (unverified)")
        self.assertEqual(row["English author display"], "Author Name (transliteration)")
        self.assertEqual(row["English annotation"], "English-language scouting note.")

    def test_missing_english_helper_fields_are_blank(self):
        row = editor_row_from_candidate(_candidate())
        self.assertEqual(row["English title"], "")
        self.assertEqual(row["English author display"], "")
        self.assertEqual(row["English annotation"], "")

    def test_signal_count(self):
        candidate = _candidate(independent_signal_count=3)
        row = editor_row_from_candidate(candidate)
        self.assertEqual(row["Signal count"], "3")

    def test_signal_summary(self):
        candidate = _candidate(signal_summary="2 independent signals: award, media.")
        row = editor_row_from_candidate(candidate)
        self.assertEqual(row["Signal summary"], "2 independent signals: award, media.")

    def test_priority_label(self):
        candidate = _candidate(priority_label="A")
        row = editor_row_from_candidate(candidate)
        self.assertEqual(row["Priority label"], "A")

    def test_short_description(self):
        candidate = _candidate(
            short_description="A compelling biography.",
            candidate_category="",
            signal_summary="",
            primary_signal={"evidence_note": ""},
            source_evidence=[{"evidence_note": ""}],
        )
        row = editor_row_from_candidate(candidate)
        self.assertEqual(row["Annotation"], "A compelling biography.")

    def test_annotation_synthesizes_safe_metadata_when_available(self):
        candidate = _candidate(
            candidate_category="narrative history",
            short_description="A narrative account of a public archive.",
            public_data_status="insufficient",
            signal_summary="2 independent signals: award, media.",
            primary_signal={
                "source_name": "Major Review",
                "source_url": "https://example.com/review",
                "source_check_date": "2024-07-15",
                "evidence_note": "Selected for a major nonfiction list.",
            },
        )
        row = editor_row_from_candidate(candidate)
        annotation = row["Annotation"]
        self.assertIn("A narrative account of a public archive.", annotation)
        self.assertIn("narrative history", annotation)
        self.assertIn("Selected for a major nonfiction list.", annotation)
        self.assertIn("2 independent signals: award, media.", annotation)
        self.assertIn("Public metadata is insufficient", annotation)
        self.assertEqual(row["Why it entered the list"], "Selected for a major nonfiction list.")
        self.assertEqual(row["Signal summary"], "2 independent signals: award, media.")

    def test_description_prefers_russian_content_description_when_available(self):
        candidate = _candidate(
            candidate_category="business",
            short_description="Technology-industry memoir and analysis.",
            content_description=(
                "Мемуар и критика Кремниевой долины от опытной технологической журналистки, "
                "которая показывает рост крупных интернет-компаний, характеры влиятельных "
                "основателей и разрыв между ранним цифровым оптимизмом и социальной ценой платформенной власти."
            ),
            signal_summary="1 independent signal: media/curated list.",
            primary_signal={
                "source_name": "NPR",
                "source_url": "https://example.test/npr",
                "source_check_date": "2026-05-21",
                "evidence_note": "NPR staff nonfiction pick; technology memoir and critique.",
            },
        )
        row = editor_row_from_candidate(candidate)

        annotation = row["Annotation"]
        description = row["Description"]
        self.assertIn("Technology-industry memoir and analysis", annotation)
        self.assertIn("Кремниевой долины", description)
        self.assertIn("социальной ценой", description)
        self.assertTrue(_has_cyrillic(description))
        self.assertNotIn("Publishers Weekly", description)

    def test_annotation_is_bounded_for_spreadsheet_use(self):
        candidate = _candidate(
            candidate_category="narrative nonfiction about institutions and memory",
            short_description=" ".join(["Detailed"] * 80),
            signal_summary=" ".join(["Signal"] * 80),
            primary_signal={"evidence_note": " ".join(["Evidence"] * 80)},
        )
        row = editor_row_from_candidate(candidate)
        self.assertLessEqual(len(row["Annotation"]), 600)

    def test_russian_edition_status(self):
        candidate = _candidate(russian_edition_status="not found in Russian")
        row = editor_row_from_candidate(candidate)
        self.assertEqual(row["Russian-edition status"], "not found in Russian")

    def test_russian_edition_url(self):
        candidate = _candidate(russian_edition_url="https://ozon.ru/product/123")
        row = editor_row_from_candidate(candidate)
        self.assertEqual(row["Russian-edition URL"], "https://ozon.ru/product/123")

    def test_visible_risk_status(self):
        candidate = _candidate(visible_risk_status="clear visible risk")
        row = editor_row_from_candidate(candidate)
        self.assertEqual(row["Visible-risk status"], "clear visible risk")

    def test_risk_comment(self):
        candidate = _candidate(visible_risk_comment="Public data hints at political content.")
        row = editor_row_from_candidate(candidate)
        self.assertEqual(row["Risk comment"], "Public data hints at political content.")

    def test_clear_visible_risk_adds_annotation_caution_without_replacing_risk_fields(self):
        candidate = _candidate(
            visible_risk_status="clear visible risk",
            visible_risk_comment="Book-content signal requires review.",
        )
        row = editor_row_from_candidate(candidate)
        self.assertIn("Visible Risk: clear visible risk", row["Annotation"])
        self.assertIn("Book-content signal requires review.", row["Annotation"])
        self.assertEqual(row["Visible-risk status"], "clear visible risk")
        self.assertEqual(row["Risk comment"], "Book-content signal requires review.")

    def test_text_check_required_adds_annotation_caution(self):
        candidate = _candidate(
            visible_risk_status="text check required",
            visible_risk_comment="Public metadata is insufficient for configured risk screening.",
        )
        row = editor_row_from_candidate(candidate)
        self.assertIn("Visible Risk: text check required", row["Annotation"])
        self.assertIn(
            "Public metadata is insufficient for configured risk screening.",
            row["Annotation"],
        )
        self.assertEqual(row["Visible-risk status"], "text check required")

    def test_no_visible_risk_annotation_does_not_imply_clearance(self):
        candidate = _candidate(
            visible_risk_status="no visible risk",
            visible_risk_comment="No configured MVP risk signal found in public metadata.",
        )
        row = editor_row_from_candidate(candidate)
        annotation = row["Annotation"].lower()
        self.assertNotIn("visible risk: no visible risk", annotation)
        self.assertNotIn("legal clearance", annotation)
        self.assertNotIn("safe", annotation)
        self.assertEqual(row["Visible-risk status"], "no visible risk")
        self.assertEqual(row["Risk comment"], "No configured MVP risk signal found in public metadata.")

    def test_editor_status_round_trip(self):
        candidate = _candidate(editor_status="interested")
        row = editor_row_from_candidate(candidate)
        self.assertEqual(row["Editor status"], "interested")

    def test_editor_note_round_trip(self):
        candidate = _candidate(editor_note="Already in rights discussion.")
        row = editor_row_from_candidate(candidate)
        self.assertEqual(row["Editor note"], "Already in rights discussion.")

    def test_empty_editor_fields(self):
        candidate = _candidate(editor_status="", editor_note="")
        row = editor_row_from_candidate(candidate)
        self.assertEqual(row["Editor status"], "")
        self.assertEqual(row["Editor note"], "")


class EvidenceLinkAggregationTests(unittest.TestCase):
    def test_evidence_urls_from_source_evidence(self):
        candidate = _candidate(
            source_evidence=[
                {"source_url": "https://example.com/src1", "evidence_url": "https://example.com/ev1"},
                {"source_url": "https://example.com/src2", "evidence_url": "https://example.com/ev2"},
            ]
        )
        links = evidence_links(candidate)
        self.assertIn("https://example.com/ev1", links)
        self.assertIn("https://example.com/ev2", links)

    def test_fallback_to_source_url_when_evidence_url_absent(self):
        candidate = _candidate(
            source_evidence=[
                {"source_url": "https://example.com/src1", "evidence_url": ""},
            ]
        )
        links = evidence_links(candidate)
        self.assertIn("https://example.com/src1", links)

    def test_source_url_also_included_when_evidence_url_present(self):
        candidate = _candidate(
            source_evidence=[
                {"source_url": "https://example.com/src1", "evidence_url": "https://example.com/ev1"},
            ]
        )
        links = evidence_links(candidate)
        self.assertIn("https://example.com/ev1", links)
        self.assertIn("https://example.com/src1", links)

    def test_russian_edition_url_included(self):
        candidate = _candidate(
            russian_edition_url="https://ozon.ru/product/123",
            source_evidence=[],
        )
        links = evidence_links(candidate)
        self.assertIn("https://ozon.ru/product/123", links)

    def test_visible_risk_evidence_url_included(self):
        candidate = _candidate(
            risk_evidence_url="https://example.com/risk-evidence",
            source_evidence=[],
        )
        links = evidence_links(candidate)
        self.assertIn("https://example.com/risk-evidence", links)

    def test_duplicate_urls_emitted_only_once(self):
        candidate = _candidate(
            source_evidence=[
                {"source_url": "https://example.com/shared", "evidence_url": "https://example.com/shared"},
                {"source_url": "https://example.com/shared", "evidence_url": "https://example.com/shared"},
            ],
            russian_edition_url="https://example.com/shared",
            risk_evidence_url="https://example.com/shared",
        )
        links = evidence_links(candidate)
        self.assertEqual(links, "https://example.com/shared")

    def test_links_pipe_separated(self):
        candidate = _candidate(
            source_evidence=[
                {"source_url": "https://example.com/a", "evidence_url": "https://example.com/a"},
                {"source_url": "https://example.com/b", "evidence_url": "https://example.com/b"},
            ],
        )
        links = evidence_links(candidate)
        self.assertEqual(links, "https://example.com/a | https://example.com/b")

    def test_empty_links_when_no_evidence(self):
        candidate = _candidate(
            source_evidence=[],
            russian_edition_url="",
            risk_evidence_url="",
        )
        links = evidence_links(candidate)
        self.assertEqual(links, "")

    def test_links_in_row(self):
        candidate = _candidate(
            source_evidence=[
                {"source_url": "https://example.com/src", "evidence_url": "https://example.com/ev"},
            ],
            russian_edition_url="https://ozon.ru/123",
        )
        row = editor_row_from_candidate(candidate)
        self.assertIn("https://example.com/ev", row["Links"])
        self.assertIn("https://example.com/src", row["Links"])
        self.assertIn("https://ozon.ru/123", row["Links"])

    def test_evidence_url_preferred_over_source_url_for_first_position(self):
        candidate = _candidate(
            source_evidence=[
                {"source_url": "https://example.com/src", "evidence_url": "https://example.com/ev"},
            ],
        )
        links = evidence_links(candidate)
        self.assertTrue(links.startswith("https://example.com/ev"))


class WriteEditorCsvTests(unittest.TestCase):
    def test_csv_uses_lf_only_line_endings(self):
        row = {field: f"value-{i}" for i, field in enumerate(EDITOR_FIELDNAMES)}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "editor-facing.csv"
            write_editor_csv(path, [row])
            data = path.read_bytes()
        self.assertNotIn(b"\r\n", data)
        self.assertEqual(data.count(b"\n"), 2)

    def test_csv_header_matches_fieldnames(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "editor-facing.csv"
            write_editor_csv(path, [])
            with path.open(encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader)
            self.assertEqual(header, EDITOR_FIELDNAMES)

    def test_csv_writes_row_values(self):
        row = {field: f"value-{i}" for i, field in enumerate(EDITOR_FIELDNAMES)}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "editor-facing.csv"
            write_editor_csv(path, [row])
            with path.open(encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
            self.assertEqual(len(rows), 1)
            for field in EDITOR_FIELDNAMES:
                self.assertEqual(rows[0][field], row[field])

    def test_csv_column_order_preserved(self):
        candidate = _candidate()
        row = editor_row_from_candidate(candidate)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "editor-facing.csv"
            write_editor_csv(path, [row])
            with path.open(encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader)
                values = next(reader)
            for i, field in enumerate(EDITOR_FIELDNAMES):
                self.assertEqual(header[i], field)
                self.assertEqual(values[i], row[field])


if __name__ == "__main__":
    unittest.main()
