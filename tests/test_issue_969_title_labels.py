"""Issue #969: the review's English-title helper labels.

``original`` only means the helper title equals the source title, so a book
found only on non-English lists must not read as an English original. A
machine rendering is labelled in the editor's words.
"""

from __future__ import annotations

from autoscout.html_review import build_html_review
from autoscout.review_presentation import decision_support_items
from autoscout.review_rendering import _render_neutral_row
from autoscout.title_evidence import TITLE_TYPE_GENERATED_RENDERING
from autoscout.title_evidence import TITLE_TYPE_ORIGINAL
from autoscout.title_evidence import TITLE_TYPE_SOURCED_PUBLISHED
from autoscout.title_evidence import format_editor_english_title

SPANISH_ONLY = [{"source_name": "TodosTusLibros", "language": "es"}]
ENGLISH_ONLY = [{"source_name": "NPR", "language": "en"}]
MIXED_ENGLISH_SPANISH = [
    {"source_name": "NPR", "language": "en"},
    {"source_name": "TodosTusLibros", "language": "es"},
]


def _original(
    title: str = "Corona Nostra",
    source_evidence: list[dict[str, str]] | None = None,
) -> dict:
    record = {
        "identity_key": f"{title.casefold()}|author",
        "title": title,
        "primary_authors": ["Author"],
        "english_title": title,
        "english_title_type": TITLE_TYPE_ORIGINAL,
        "english_title_provenance": {"kind": TITLE_TYPE_ORIGINAL, "research_sources": []},
    }
    if source_evidence is not None:
        record["source_evidence"] = source_evidence
    return record


def _rendering() -> dict:
    return {
        "identity_key": "corona nostra|author",
        "title": "Corona Nostra",
        "primary_authors": ["Author"],
        "english_title": "Corona Nostra: A Novel",
        "english_title_type": TITLE_TYPE_GENERATED_RENDERING,
        "english_title_provenance": {"kind": TITLE_TYPE_GENERATED_RENDERING, "research_sources": []},
    }


def _sourced() -> dict:
    return {
        "identity_key": "das buch|philippa perry",
        "title": "Das Buch, von dem du dir wünschst, deine Eltern hätten es gelesen",
        "primary_authors": ["Philippa Perry"],
        "english_title": "The Book You Wish Your Parents Had Read",
        "english_title_type": TITLE_TYPE_SOURCED_PUBLISHED,
        "english_title_provenance": {
            "kind": TITLE_TYPE_SOURCED_PUBLISHED,
            "research_sources": ["https://publisher.example/books/parents"],
            "mapping": {
                "original_title": "Das Buch, von dem du dir wünschst, deine Eltern hätten es gelesen",
                "original_authors": ["Philippa Perry"],
                "published_title": "The Book You Wish Your Parents Had Read",
                "published_authors": ["Philippa Perry"],
            },
        },
    }


def _csv_row() -> dict[str, str]:
    return {
        "Title": "",
        "Author": "",
        "Editor status": "",
        "Editor note": "",
        "Candidate ID": "",
        "Update delta": "new",
        "Priority label": "",
        "Signal count": "1",
        "Russian-edition status": "",
        "Source": "Test Source",
        "Source URL": "",
    }


def _rendered(candidate: dict) -> dict:
    return _render_neutral_row(candidate, _csv_row())


def _summary() -> dict:
    return {
        "delta_counts": {"new": 1, "upgraded": 0, "changed": 0, "unchanged": 0},
        "hidden_by_rule_count": 0,
        "editor_rows_count": 1,
        "total_count": 1,
    }


def test_non_english_only_original_has_no_helper_title() -> None:
    record = _original(source_evidence=SPANISH_ONLY)
    assert format_editor_english_title(record) == ""


def test_non_english_only_original_has_no_english_title_row_in_html() -> None:
    html = build_html_review([_rendered(_original(source_evidence=SPANISH_ONLY))], _summary())
    assert "Английское название" not in html
    assert "Corona Nostra (original)" not in html


def test_english_source_original_keeps_the_original_label() -> None:
    record = _original(source_evidence=ENGLISH_ONLY)
    assert format_editor_english_title(record) == "Corona Nostra (original)"


def test_mixed_english_and_non_english_sources_keep_the_original_label() -> None:
    record = _original(source_evidence=MIXED_ENGLISH_SPANISH)
    assert format_editor_english_title(record) == "Corona Nostra (original)"


def test_missing_source_evidence_keeps_the_original_label() -> None:
    record = _original()
    assert "source_evidence" not in record
    assert format_editor_english_title(record) == "Corona Nostra (original)"


def test_generated_rendering_is_labelled_as_machine_translation_in_csv() -> None:
    rendered = _rendered(_rendering())
    assert rendered["editor_row"]["English title"] == "Corona Nostra: A Novel (machine translation)"


def test_generated_rendering_is_labelled_as_machine_translation_in_markdown() -> None:
    rendered = _rendered(_rendering())
    assert dict(decision_support_items(rendered))["Перевод названия"] == (
        "Corona Nostra: A Novel (machine translation)"
    )


def test_generated_rendering_is_labelled_as_machine_translation_in_html() -> None:
    html = build_html_review([_rendered(_rendering())], _summary())
    assert "Английское название" in html
    assert "Corona Nostra: A Novel (machine translation)" in html


def test_sourced_published_title_label_is_unchanged() -> None:
    record = _sourced()
    assert format_editor_english_title(record) == "The Book You Wish Your Parents Had Read (published title)"
    rendered = _rendered(record)
    assert rendered["editor_row"]["English title"] == ("The Book You Wish Your Parents Had Read (published title)")
    html = build_html_review([rendered], _summary())
    assert "The Book You Wish Your Parents Had Read (published title)" in html
