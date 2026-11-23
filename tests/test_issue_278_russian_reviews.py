from __future__ import annotations

import csv
from pathlib import Path

from state_store_support import seed_state

from autoscout.json_io import write_json
from autoscout.review_presentation import EDITOR_STATUS_LABELS
from autoscout.review_presentation import EXCLUSION_CATEGORY_LABELS
from autoscout.review_presentation import LIFECYCLE_LABELS
from autoscout.review_presentation import RISK_STATUS_LABELS
from autoscout.review_presentation import RUSSIAN_STATUS_LABELS
from autoscout.review_presentation import editor_status_label
from autoscout.review_presentation import lifecycle_label
from autoscout.review_presentation import review_context
from autoscout.review_presentation import risk_status_label
from autoscout.review_presentation import russian_status_label
from autoscout.review_rendering import render_review
from autoscout.scouting_candidates import INELIGIBLE_CANDIDATE_CATEGORIES


def test_weekly_reviews_are_russian_and_do_not_leak_technical_identity(tmp_path: Path) -> None:
    """#278 keeps the tabular Markdown risk status and compact HTML details separate."""
    identity = "secret-book|secret-author"
    historical_identity = "historical-secret|archived-author"
    diagnostic_reason = "Traceback: /Users/editor/repository branch main commit abc123 raw command output"
    candidate = {
        "identity_key": identity,
        "title": "Полезная книга",
        "primary_authors": ["Анна Автор"],
        "update_delta": "new",
        "priority_label": "A",
        "independent_signal_count": 2,
        "russian_edition_status": "not found in Russian",
        "visible_risk_status": "text check required",
        "visible_risk_category": "drugs/microdosing/psychoactive substances",
        "risk_signal_scope": "book-content",
        "risk_evidence_url": "https://risk.example.test/evidence",
        "visible_risk_comment": "Проверить спорный фрагмент.",
        "editor_status": "needs_deeper_analysis",
        "content_description": "Русское описание полезной книги для редактора.",
        "source_evidence": [{"source_name": "Источник", "source_url": "https://example.test/book"}],
    }
    write_json(tmp_path / "scouting-candidates.json", [candidate])
    seed_state(
        tmp_path,
        candidate_history=[
            {
                "identity_key": historical_identity,
                "title": "Архивная книга",
                "primary_authors": ["Архивный Автор"],
                "content_description": "Проверенное русское описание архивной книги.",
                "hidden_by_rule_id": "taste-secret-42",
                "hidden_by_rule_reason": diagnostic_reason,
                "last_hidden_by_rule_date": "2026-01-14",
                "last_seen_date": "2026-01-14",
            }
        ],
    )
    write_json(tmp_path / "run-summary.json", {"run_date": "2026-01-15", "editor_facing_count": 1})

    assert render_review(tmp_path) == 0
    markdown = (tmp_path / "weekly-review.md").read_text(encoding="utf-8")
    html = (tmp_path / "weekly-review.html").read_text(encoding="utf-8")

    for review in (markdown, html):
        assert identity not in review
        assert historical_identity not in review
        for leaked in (
            "Candidate ID",
            "identity-key",
            "record-feedback",
            "autoscout ",
            "needs_deeper_analysis",
            "not found in Russian",
            "text check required",
            "Review status",
            "This week",
        ):
            assert leaked not in review
        assert "Полезная книга" in review
        assert "Анна Автор" in review
        assert "Нужен углублённый анализ" in review
        assert "Проверить спорный фрагмент." in review
        assert "Архивная книга" in review
        assert "Ранее скрыта редакторским правилом (2026-01-14)." in review
        assert "проверено — 2 из 2" in review
        assert "Traceback" not in review
        assert "/Users/editor/repository" not in review
        assert "branch main" not in review
        assert "raw command output" not in review
        assert "taste-secret-42" not in review
        assert "чат" in review

    assert "Русское издание не найдено" in markdown
    assert "Русское издание не найдено" not in html

    # Markdown is the tabular report, so it keeps the localized risk status.
    assert "Нужно проверить текст" in markdown
    # HTML intentionally omits the duplicate status detail row and keeps the
    # compact expanded risk details: category, rationale, comment, and evidence.
    assert "Нужно проверить текст" not in html
    assert "Статус риска" not in html
    assert "Категория риска" in html
    assert "наркотики, микродозинг и психоактивные вещества" in html
    assert "Обоснование риска" in html
    assert "Публичные данные указывают на содержание самой книги" in html
    assert "Комментарий о риске" in html
    assert "Проверить спорный фрагмент." in html
    assert "risk.example.test/evidence" in html
    assert "data-id-key" not in html
    with (tmp_path / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["Candidate ID"] == identity
    assert row["Editor status"] == "needs_deeper_analysis"


def test_all_status_values_are_localized_and_unknown_values_fail_safe() -> None:
    for canonical, translated in LIFECYCLE_LABELS.items():
        assert lifecycle_label(canonical) == translated
    for canonical, translated in EDITOR_STATUS_LABELS.items():
        assert editor_status_label(canonical) == translated
    for canonical, translated in RUSSIAN_STATUS_LABELS.items():
        assert russian_status_label(canonical) == translated
    for canonical, translated in RISK_STATUS_LABELS.items():
        assert risk_status_label(canonical) == translated

    assert russian_status_label("unclear") == "Статус русского издания неясен"
    assert lifecycle_label("internal_lifecycle") == "Неизвестный статус обзора"
    assert editor_status_label("internal_decision") == "Неизвестное решение редактора"
    assert russian_status_label("internal_russian") == "Неизвестный статус русского издания"
    assert risk_status_label("internal_risk") == "Неизвестный статус риска"


def test_exclusion_category_labels_match_ineligible_candidate_categories() -> None:
    """EXCLUSION_CATEGORY_LABELS keys must stay in sync with INELIGIBLE_CANDIDATE_CATEGORIES."""
    assert set(EXCLUSION_CATEGORY_LABELS) == INELIGIBLE_CANDIDATE_CATEGORIES


def test_review_context_translates_known_categories_and_drops_unknown_diagnostics() -> None:
    assert review_context("Excluded from acquisition: fiction on 2026-07-11.") == (
        "Исключена из отбора: художественная литература (2026-07-11)."
    )
    for category in INELIGIBLE_CANDIDATE_CATEGORIES:
        rendered = review_context(f"Excluded from acquisition: {category}.")
        assert rendered.startswith("Исключена из отбора: ")
        assert category not in rendered
    assert review_context("Traceback: /repo/path branch main commit abc raw output") == ""
