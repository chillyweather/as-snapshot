from __future__ import annotations

from pathlib import Path

from state_store_support import seed_state

from autoscout.json_io import write_json
from autoscout.review_rendering import render_review


def test_render_review_explains_when_legacy_visible_risk_has_no_details(tmp_path: Path) -> None:
    candidate = {
        "identity_key": "the-spy-in-the-archive|gordon-corera",
        "title": "The Spy in the Archive",
        "primary_authors": ["Gordon Corera"],
        "update_delta": "new",
        "priority_label": "D",
        "independent_signal_count": 1,
        "russian_edition_status": "not found in Russian",
        "visible_risk_status": "clear visible risk",
        "editor_status": "already_known",
        "editor_note": "Already requested.",
        "first_seen_date": "2026-06-10",
        "last_seen_date": "2026-06-10",
        "content_description": "История попытки убийства сотрудника КГБ.",
        "source_evidence": [
            {
                "source_name": "Test Source",
                "source_url": "https://example.test/the-spy-in-the-archive",
                "evidence_note": "Public book metadata.",
            }
        ],
    }
    write_json(tmp_path / "scouting-candidates.json", [])
    seed_state(tmp_path, candidate_history=[candidate])
    write_json(tmp_path / "run-summary.json", {"run_date": "2026-07-28", "editor_facing_count": 1})

    assert render_review(tmp_path) == 0
    html = (tmp_path / "weekly-review.html").read_text(encoding="utf-8")

    assert ">Риск<" not in html
    assert "<dt>Категория риска</dt><dd>Требует уточнения</dd>" in html
    assert (
        "<dt>Обоснование риска</dt><dd>"
        '<span class="note">В сохранённых данных нет категории или подтверждающего объяснения. '
        "Риск необходимо проверить по источникам.</span></dd>"
    ) in html


def test_render_review_shows_localized_category_reason_and_evidence_link(tmp_path: Path) -> None:
    candidate = {
        "identity_key": "documented-risk|alex-example",
        "title": "Documented Risk",
        "primary_authors": ["Alex Example"],
        "update_delta": "new",
        "priority_label": "D",
        "independent_signal_count": 1,
        "russian_edition_status": "not found in Russian",
        "visible_risk_status": "clear visible risk",
        "visible_risk_category": "Russia/politics",
        "visible_risk_comment": "Book-content signal from public metadata requires human/legal review.",
        "risk_signal_scope": "book-content",
        "risk_evidence_url": "https://example.test/risk-evidence",
        "content_description": "Русское описание книги с документированным риском.",
        "source_evidence": [
            {
                "source_name": "Test Source",
                "source_url": "https://example.test/documented-risk",
                "evidence_note": "Public book metadata.",
            }
        ],
    }
    write_json(tmp_path / "scouting-candidates.json", [candidate])
    write_json(tmp_path / "run-summary.json", {"run_date": "2026-07-28", "editor_facing_count": 1})

    assert render_review(tmp_path) == 0
    html = (tmp_path / "weekly-review.html").read_text(encoding="utf-8")

    assert "<dt>Категория риска</dt><dd>Россия и современная политика</dd>" in html
    assert (
        "<dt>Обоснование риска</dt><dd>"
        '<span class="note">Публичные данные указывают на содержание самой книги; '
        "требуется редакционная или юридическая проверка.</span></dd>"
    ) in html
    assert "Book-content signal from public metadata requires human/legal review." in html
    assert (
        '<dt>Источник риска</dt><dd><a href="https://example.test/risk-evidence" '
        'target="_blank" rel="noopener noreferrer">Открыть подтверждающий источник</a></dd>'
    ) in html


def test_render_review_distinguishes_author_context_from_book_content(tmp_path: Path) -> None:
    candidate = {
        "identity_key": "author-context-risk|alex-example",
        "title": "Author Context Risk",
        "primary_authors": ["Alex Example"],
        "update_delta": "new",
        "priority_label": "C",
        "independent_signal_count": 1,
        "russian_edition_status": "not found in Russian",
        "visible_risk_status": "text check required",
        "visible_risk_category": "Russia/politics",
        "visible_risk_comment": "Author/context signal is indirect and needs verification.",
        "risk_signal_scope": "author-context",
        "content_description": "Русское описание книги с контекстным сигналом.",
        "source_evidence": [
            {
                "source_name": "Test Source",
                "source_url": "https://example.test/author-context",
                "evidence_note": "Public author metadata.",
            }
        ],
    }
    write_json(tmp_path / "scouting-candidates.json", [candidate])
    write_json(tmp_path / "run-summary.json", {"run_date": "2026-07-28", "editor_facing_count": 1})

    assert render_review(tmp_path) == 0
    html = (tmp_path / "weekly-review.html").read_text(encoding="utf-8")

    assert (
        "<dt>Обоснование риска</dt><dd>"
        '<span class="note">Сигнал относится к автору или публичному контексту, '
        "а не подтверждённому содержанию книги; необходимо проверить текст и источники.</span></dd>"
    ) in html
