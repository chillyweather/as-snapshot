"""Regression coverage for Issue #436 chart evidence-note filtering."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from autoscout.config import load_validated_config
from autoscout.source_collection import SourceCollection
from autoscout.source_collectors import extraction_contract_violation
from autoscout.source_collectors.base import chart_evidence_note_has_unsafe_data

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "configs/current-weekly-pilot.json"
FIXTURE_DIR = ROOT / "tests/fixtures/issue-432-chart-baseline"  # stored once (#1022)
AUDIBLE_EXTRACTION = (
    FIXTURE_DIR / "audible-bestselling-computers---technology-audiobooks-b6b4652c-codex-extraction.json"
)
AUDIBLE_CAPTURE = FIXTURE_DIR / "codex-capture-audible-bestselling-computers---technology-audiobooks-b6b4652c.json"
RUN_DATE = "2026-08-13"


def _source(name: str, *, stable_audible_fixture: bool = False) -> dict[str, Any]:
    config = load_validated_config(CONFIG_PATH)
    source = next(source for source in config["sources"] if source["name"] == name)
    if stable_audible_fixture:
        return {
            **source,
            "source_page_fixture_file": str(AUDIBLE_CAPTURE),
            "extraction_response_file": str(AUDIBLE_EXTRACTION),
        }
    return {
        **source,
        "source_page_fixture_file": str(
            ROOT / "data/current-weekly-pilot/amazon-charts-most-read-nonfiction-page.json"
        ),
        "extraction_response_file": str(
            ROOT / "data/current-weekly-pilot/amazon-charts-most-read-nonfiction-extraction.json"
        ),
    }


def _fixture_entry(source: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    page = json.loads(Path(source["source_page_fixture_file"]).read_text(encoding="utf-8"))
    extraction = json.loads(Path(source["extraction_response_file"]).read_text(encoding="utf-8"))
    entries = extraction.get("entries", extraction.get("mentions", []))
    return dict(entries[4] if source.get("audible_explicit_chart") else entries[0]), page


@pytest.mark.parametrize(
    "note",
    [
        "Chart subtitle identifies a sales-prospecting guide.",
        "Chart entry lists a book about sales.",
        "Rank 5 for the week; memoir of a units manager.",
        "Chart page lists rank, title, and author.",
        "The book is about a sales manager.",
        "The book describes copies made by medieval scribes.",
        "A memoir of a sales manager.",
        "Book about the copies made by medieval scribes.",
        "Audible chart rank 5; exact title, By line, and direct /pd/ product link appear in the capture.",
        "Книга о продажах и управлении.",
        "Страница премии указывает произведение лауреата.",
    ],
)
@pytest.mark.parametrize(
    "name, stable_audible_fixture",
    [
        ("Audible Bestselling Computers & Technology Audiobooks", True),
        ("Amazon Charts Most Read Nonfiction", False),
    ],
)
def test_chart_contract_accepts_subject_metadata_notes(name: str, stable_audible_fixture: bool, note: str) -> None:
    source = _source(name, stable_audible_fixture=stable_audible_fixture)
    entry, page = _fixture_entry(source)
    entry["evidence_note"] = note

    assert extraction_contract_violation(entry, entry, source, page) == ""


@pytest.mark.parametrize(
    "note",
    [
        "Sold 400,000 copies in its first year.",
        "Reviews call it the book of the year.",
        "Print sales of 120,000 units.",
        "12 weeks on the list, 200,000 copies sold.",
        'Review quotation: "A gripping, unforgettable account."',
        'The blurb says "an instant classic".',
        "Sales rose sharply this week.",
        "Units sold reached 50,000.",
        "Description text copied from the page.",
        "Promotional prose: An essential, groundbreaking must-read guide.",
        "Chart sales were high.",
    ],
)
@pytest.mark.parametrize(
    "name, stable_audible_fixture",
    [
        ("Audible Bestselling Computers & Technology Audiobooks", True),
        ("Amazon Charts Most Read Nonfiction", False),
    ],
)
def test_chart_contract_rejects_data_review_and_promotional_notes(
    name: str, stable_audible_fixture: bool, note: str
) -> None:
    source = _source(name, stable_audible_fixture=stable_audible_fixture)
    entry, page = _fixture_entry(source)
    entry["evidence_note"] = note

    reason = extraction_contract_violation(entry, entry, source, page)

    assert "evidence_note" in reason


@pytest.mark.parametrize(
    "name, stable_audible_fixture",
    [
        ("Audible Bestselling Computers & Technology Audiobooks", True),
        ("Amazon Charts Most Read Nonfiction", False),
    ],
)
def test_chart_contract_accepts_a_normalized_quote_of_its_own_title(name: str, stable_audible_fixture: bool) -> None:
    source = _source(name, stable_audible_fixture=stable_audible_fixture)
    entry, page = _fixture_entry(source)
    entry["evidence_note"] = f'Chart lists the work as " {entry["title"].upper()}! ".'

    assert extraction_contract_violation(entry, entry, source, page) == ""


def test_audible_chart_accepts_the_exact_fanatical_prospecting_title_quote() -> None:
    source = _source("Audible Bestselling Computers & Technology Audiobooks", stable_audible_fixture=True)
    entry, page = _fixture_entry(source)
    entry["evidence_note"] = 'Chart lists the work as "Fanatical Prospecting".'

    assert extraction_contract_violation(entry, entry, source, page) == ""


def test_chart_guard_accepts_a_matching_title_before_data_context_scan() -> None:
    assert not chart_evidence_note_has_unsafe_data('Chart lists "Sales 101".', "Sales 101")


@pytest.mark.parametrize(
    "name, stable_audible_fixture, quoted_title",
    [
        ("Audible Bestselling Computers & Technology Audiobooks", True, "Other Work"),
        ("Amazon Charts Most Read Nonfiction", False, "Fanatical Prospecting"),
    ],
)
def test_chart_contract_rejects_a_quote_for_a_different_work(
    name: str, stable_audible_fixture: bool, quoted_title: str
) -> None:
    source = _source(name, stable_audible_fixture=stable_audible_fixture)
    entry, page = _fixture_entry(source)
    entry["evidence_note"] = f'Chart lists the work as "{quoted_title}".'

    reason = extraction_contract_violation(entry, entry, source, page)

    assert "evidence_note" in reason


@pytest.mark.parametrize(
    "name, stable_audible_fixture",
    [
        ("Audible Bestselling Computers & Technology Audiobooks", True),
        ("Amazon Charts Most Read Nonfiction", False),
    ],
)
def test_chart_contract_rejects_title_quote_mixed_with_a_blurb_quote(name: str, stable_audible_fixture: bool) -> None:
    source = _source(name, stable_audible_fixture=stable_audible_fixture)
    entry, page = _fixture_entry(source)
    entry["evidence_note"] = f'Chart lists the work as "{entry["title"]}"; the blurb says "an instant classic".'

    reason = extraction_contract_violation(entry, entry, source, page)

    assert "evidence_note" in reason


@pytest.mark.parametrize(
    "name, stable_audible_fixture",
    [
        ("Audible Bestselling Computers & Technology Audiobooks", True),
        ("Amazon Charts Most Read Nonfiction", False),
    ],
)
def test_chart_contract_rejects_a_blurb_marker_even_for_a_title_quote(name: str, stable_audible_fixture: bool) -> None:
    source = _source(name, stable_audible_fixture=stable_audible_fixture)
    entry, page = _fixture_entry(source)
    entry["evidence_note"] = f'The blurb says "{entry["title"]}".'

    reason = extraction_contract_violation(entry, entry, source, page)

    assert "evidence_note" in reason


@pytest.mark.parametrize(
    "note",
    [
        "Chart section titled Must-Read Nonfiction.",
        "Groundbreaking Research prize, 2026 laureate.",
        "Book about advertising copy in the 1960s.",
    ],
)
@pytest.mark.parametrize(
    "name, stable_audible_fixture",
    [
        ("Audible Bestselling Computers & Technology Audiobooks", True),
        ("Amazon Charts Most Read Nonfiction", False),
    ],
)
def test_chart_contract_accepts_ambiguous_metadata_and_topic_phrases(
    name: str, stable_audible_fixture: bool, note: str
) -> None:
    source = _source(name, stable_audible_fixture=stable_audible_fixture)
    entry, page = _fixture_entry(source)
    entry["evidence_note"] = note

    assert extraction_contract_violation(entry, entry, source, page) == ""


@pytest.mark.parametrize(
    "note",
    [
        "Sold four hundred thousand copies.",
        "Sales topped a million.",
        "Outsold every other title this year.",
        "The bestselling business title of the decade.",
    ],
)
@pytest.mark.parametrize(
    "name, stable_audible_fixture",
    [
        ("Audible Bestselling Computers & Technology Audiobooks", True),
        ("Amazon Charts Most Read Nonfiction", False),
    ],
)
def test_chart_contract_documents_worded_sales_claims_as_accepted_gaps(
    name: str, stable_audible_fixture: bool, note: str
) -> None:
    """The deterministic guard does not infer sales claims from number words."""
    source = _source(name, stable_audible_fixture=stable_audible_fixture)
    entry, page = _fixture_entry(source)
    entry["evidence_note"] = note

    assert extraction_contract_violation(entry, entry, source, page) == ""


def test_chart_contract_accepts_the_current_fixture_metadata_notes() -> None:
    for name, stable_audible_fixture in (
        ("Audible Bestselling Computers & Technology Audiobooks", True),
        ("Amazon Charts Most Read Nonfiction", False),
    ):
        source = _source(name, stable_audible_fixture=stable_audible_fixture)
        entry, page = _fixture_entry(source)

        assert extraction_contract_violation(entry, entry, source, page) == ""


def test_d212_audible_computers_artifact_replays_all_twenty_mentions() -> None:
    source = _source("Audible Bestselling Computers & Technology Audiobooks", stable_audible_fixture=True)
    extraction = json.loads(AUDIBLE_EXTRACTION.read_text(encoding="utf-8"))
    expected_entries = extraction["entries"]
    expected_ranks = [int(entry["current_rank"]) for entry in expected_entries]

    result = SourceCollection(CONFIG_PATH, ROOT, RUN_DATE).collect([source])

    assert len(expected_entries) == 20
    assert expected_ranks == list(range(1, 21))
    assert result.source_runs[0]["status"] == "collected"
    assert result.source_runs[0]["valid_mention_count"] == len(expected_entries)
    assert [int(mention["current_rank"]) for mention in result.source_mentions] == expected_ranks
    assert len(result.source_mentions) == len(expected_entries)


def test_d212_audible_artifact_is_replayed_without_editing_the_extraction() -> None:
    source = _source("Audible Bestselling Computers & Technology Audiobooks", stable_audible_fixture=True)
    before = AUDIBLE_EXTRACTION.read_bytes()

    SourceCollection(CONFIG_PATH, ROOT, RUN_DATE).collect([source])

    assert AUDIBLE_EXTRACTION.read_bytes() == before
