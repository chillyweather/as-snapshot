from __future__ import annotations

from pathlib import Path

from autoscout.json_io import write_json
from autoscout.review_rendering import render_review


def _candidate(**overrides):
    candidate = {
        "identity_key": "final-fantasy-vii-rebirth|square-enix",
        "title": "Final Fantasy VII Rebirth: Material Ultimania",
        "primary_authors": ["Square Enix"],
        "update_delta": "new",
        "priority_label": "C",
        "independent_signal_count": 1,
        "russian_edition_status": "not found in Russian",
        "content_description": "Иллюстрированный производственный архив видеоигры.",
        "description_provenance": {
            "provenance": "codex-generated",
            "evidence_basis": ["https://example.test/hardcover-nonfiction-ranking"],
        },
        "source_evidence": [
            {
                "source_name": "Publishers Weekly Hardcover Frontlist Nonfiction",
                "source_url": "https://example.test/hardcover-nonfiction-ranking",
                "evidence_url": "https://example.test/hardcover-nonfiction-ranking",
                "evidence_note": "Ranked on the weekly nonfiction list.",
            }
        ],
    }
    candidate.update(overrides)
    return candidate


def _render(tmp_path: Path, candidate: dict) -> str:
    write_json(tmp_path / "scouting-candidates.json", [candidate])
    write_json(tmp_path / "run-summary.json", {"run_date": "2026-07-28", "editor_facing_count": 1})
    assert render_review(tmp_path) == 0
    return (tmp_path / "weekly-review.html").read_text(encoding="utf-8")


def test_ranking_evidence_is_not_labelled_as_a_book_page(tmp_path: Path) -> None:
    html = _render(tmp_path, _candidate())

    assert "<dt>Страница книги</dt>" not in html
    assert "<dt>Основной источник</dt>" in html
    assert "Publishers Weekly Hardcover Frontlist Nonfiction" in html
    assert (
        '<a href="https://example.test/hardcover-nonfiction-ranking" target="_blank" rel="noopener noreferrer">'
    ) in html


def test_unverified_publisher_provenance_is_not_a_book_page(tmp_path: Path) -> None:
    html = _render(
        tmp_path,
        _candidate(publisher_provenance="https://publisher.example.test/final-fantasy-vii-rebirth"),
    )

    assert "<dt>Страница книги</dt>" not in html
    assert "publisher.example.test" not in html
    assert (
        '<dt>Основной источник</dt><dd><a href="https://example.test/hardcover-nonfiction-ranking" '
        'target="_blank" rel="noopener noreferrer">Publishers Weekly Hardcover Frontlist Nonfiction</a></dd>'
    ) in html
    assert html.count("<a ") == html.count('target="_blank" rel="noopener noreferrer"')
