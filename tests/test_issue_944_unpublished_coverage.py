"""Books awaiting publication are no gap in description coverage (issue #944).

A book that is not out yet cannot have a description. The #791 rule already
reports such books on their own line («Ещё не опубликованы: N»), but the
headline «Описаний на русском: X из N» still counted them in N, and the
per-source caveat «Внимание: <source> — без описания X из Y новых книг»
counted them as the source's losses. With the PM source they arrive in the
hundreds, so both now leave them out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from state_store_support import seed_state

from autoscout.content_description import DESCRIPTION_FALLBACK
from autoscout.content_description import DESCRIPTION_NOT_YET_PUBLISHED_DATE_FIELD
from autoscout.content_description import coverage_denominator
from autoscout.content_description import description_coverage
from autoscout.description_diagnostics import refresh_description_diagnostics
from autoscout.html_review import build_html_review
from autoscout.json_io import write_json
from autoscout.review_rendering import render_review
from autoscout.russian_workflow import completion_message

VALID_TEXT = (
    "Книга о памяти, обществе и общественной жизни маленького города. "
    "Автор соединяет репортаж с историческим контекстом и показывает, "
    "как прошлое формирует настоящее."
)
REGULAR = "Regular Source"
DEALS = "Deals Source"


def _book(key: str, source: str, *, described: bool = False, waiting: bool = False) -> dict[str, Any]:
    book: dict[str, Any] = {
        "identity_key": f"{key}|alex author",
        "title": key,
        "primary_authors": ["Alex Author"],
        "update_delta": "new",
        "content_description": VALID_TEXT if described else DESCRIPTION_FALLBACK,
        "source_evidence": [{"source_name": source, "source_url": f"https://example.test/{key}"}],
    }
    if waiting:
        book[DESCRIPTION_NOT_YET_PUBLISHED_DATE_FIELD] = "2026-11-01"
    return book


def _acceptance_week() -> list[dict[str, Any]]:
    """40 regular new books, 30 of them described, and 90 awaiting publication."""
    regular = [_book(f"regular-{index}", REGULAR, described=index < 30) for index in range(40)]
    waiting = [_book(f"deal-{index}", DEALS, waiting=True) for index in range(90)]
    return regular + waiting


def test_the_headline_leaves_waiting_books_out_of_n() -> None:
    coverage = description_coverage(_acceptance_week())

    assert coverage["total_candidates"] == 130
    assert coverage["not_yet_published_count"] == 90
    assert coverage["coverage_denominator"] == 40
    message = completion_message("completed/no-attention", description_coverage=coverage)
    assert "Описаний на русском: 30 из 40." in message
    assert "Требуют проверки: 10." in message
    assert "Ещё не опубликованы: 90." in message


def test_completion_message_review_and_html_state_the_same_numbers(tmp_path: Path) -> None:
    books = _acceptance_week()
    write_json(tmp_path / "scouting-candidates.json", books)
    seed_state(tmp_path, candidate_history=books)
    write_json(tmp_path / "run-summary.json", {"run_date": "2026-11-02", "editor_facing_count": len(books)})

    assert render_review(tmp_path) == 0

    coverage = description_coverage(books)
    message = completion_message("completed/no-attention", description_coverage=coverage)
    markdown = (tmp_path / "weekly-review.md").read_text(encoding="utf-8")
    html = (tmp_path / "weekly-review.html").read_text(encoding="utf-8")
    assert "Описаний на русском: 30 из 40." in message
    assert "проверено — 30 из 40, требуют дополнения — 10, ещё не опубликованы — 90." in markdown
    assert "проверено — 30 из 40, требуют дополнения — 10, ещё не опубликованы — 90." in html


def test_a_source_whose_books_all_await_publication_gets_no_caveat() -> None:
    summary: dict[str, Any] = {}

    refresh_description_diagnostics(summary, _acceptance_week(), {}, {})

    assert "description_caveats" not in summary
    sources = description_coverage(_acceptance_week())["fallback_by_source"]
    assert [source["source_name"] for source in sources] == [REGULAR]


def test_a_source_whose_published_books_lack_descriptions_still_gets_the_caveat() -> None:
    books = [_book(f"deal-{index}", DEALS, waiting=index < 7) for index in range(10)]
    books.append(_book("described", DEALS, described=True))
    summary: dict[str, Any] = {}

    refresh_description_diagnostics(summary, books, {}, {})

    # Seven books wait for publication; three of the four published lack a description.
    assert summary["description_caveats"] == [f"Внимание: {DEALS} — без описания 3 из 4 новых книг."]


def test_a_week_of_only_waiting_books_states_no_empty_headline() -> None:
    coverage = description_coverage([_book(f"deal-{index}", DEALS, waiting=True) for index in range(3)])

    message = completion_message("completed/no-attention", description_coverage=coverage)

    assert "Описаний на русском" not in message
    assert "Ещё не опубликованы: 3." in message


def test_coverage_written_before_the_key_reads_the_same_n() -> None:
    legacy_791 = {"validated_count": 30, "fallback_count": 100, "total_candidates": 130, "not_yet_published_count": 90}
    legacy = {"validated_count": 4, "fallback_count": 1, "total_candidates": 5}

    assert coverage_denominator(legacy_791) == 40
    assert coverage_denominator(legacy) == 5
    assert coverage_denominator({"validated_count": 4, "fallback_count": 1}) == 5
    assert "Описаний на русском: 4 из 5." in completion_message("completed/no-attention", description_coverage=legacy)
    html = build_html_review([], {"cumulative_counts": {"description_coverage": legacy_791}})
    assert "проверено — 30 из 40" in html
