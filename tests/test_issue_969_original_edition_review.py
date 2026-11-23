from state_store_support import seed_state

from autoscout.html_review import _candidate_html
from autoscout.original_edition_presentation import format_original_edition


def edition(**changes):
    result = {
        "is_translation": "yes",
        "translation_evidence_url": "https://example.com/book",
        "status": "confirmed",
        "original_title": "Somewhere Towards the End",
        "original_language": "en",
        "language_confirmed": True,
        "original_first_published_year": 2008,
        "confirmation_url": "https://openlibrary.org/works/OL1W",
    }
    result.update(changes)
    return result


def english_row():
    return {
        "title": "Ahora",
        "primary_authors": ["Athill"],
        "english_title": "Somewhere Towards the End",
        "english_title_type": "sourced_published",
        "english_title_provenance": {
            "kind": "sourced_published",
            "research_sources": ["https://openlibrary.org/works/OL1W"],
            "mapping": {
                "original_title": "Ahora",
                "original_authors": ["Athill"],
                "published_title": "Somewhere Towards the End",
                "published_authors": ["Athill"],
            },
        },
    }


def test_confirmed_english_original_hides_matching_published_title():
    row = {**english_row(), "original_edition": edition()}
    text, url, _ = format_original_edition(row)
    assert text == "«Somewhere Towards the End» — английский, 2008"
    assert url == "https://openlibrary.org/works/OL1W"
    html = _candidate_html(row)
    assert "Оригинал" in html and "Open Library" in html
    assert "Английское название" not in html


def test_different_published_english_title_remains_visible():
    row = {**english_row(), "original_edition": edition(original_title="Different title")}
    assert "Английское название" in _candidate_html(row)


def test_unconfirmed_language_and_machine_english_title_are_shown():
    row = {
        **english_row(),
        "original_edition": edition(original_language="es", language_confirmed=False),
        "english_title_type": "generated_rendering",
        "english_title_provenance": {"kind": "generated_rendering", "research_sources": []},
    }
    html = _candidate_html(row)
    assert "испанский (язык не подтверждён)" in html
    assert "Английское название" in html and "(machine translation)" in html


def test_translation_without_original_shows_translator_evidence():
    row = {
        "original_edition": edition(
            status="not_found",
            original_title="",
            original_language="",
            language_confirmed=False,
            original_first_published_year=None,
            confirmation_url="",
        )
    }
    html = _candidate_html(row)
    assert "Перевод, оригинал не найден" in html
    assert "Страница с переводчиком" in html


def test_invalid_originals_are_hidden():
    for value in (None, {}, edition(original_title=""), edition(original_first_published_year="2008")):
        row = {"original_edition": value}
        assert format_original_edition(row) == ("", "", "")
        assert "Оригинал" not in _candidate_html(row)


def test_english_book_without_original_keeps_english_title_row():
    html = _candidate_html(english_row())
    assert "Оригинал" not in html
    assert (
        '<span class="title-evidence title-evidence--published">Somewhere Towards the End (published title)</span>'
        in html
    )
    assert "Английское название" in html


def render_candidate(tmp_path, original=None):
    from autoscout.json_io import write_json
    from autoscout.review_rendering import render_review

    candidate = {
        "identity_key": "ahora|athill",
        "title": "Ahora",
        "primary_authors": ["Athill"],
        "update_delta": "new",
        "source_evidence": [],
    }
    if original is not None:
        candidate["original_edition"] = original
    write_json(tmp_path / "scouting-candidates.json", [candidate])
    seed_state(tmp_path, candidate_history=[])
    write_json(tmp_path / "run-summary.json", {"run_date": "2026-01-15", "editor_facing_count": 1})
    assert render_review(tmp_path) == 0
    return (tmp_path / "weekly-review.md").read_text(encoding="utf-8")


def test_markdown_includes_confirmed_original_column(tmp_path):
    markdown = render_candidate(tmp_path, edition())
    assert "| Оригинал |" in markdown
    assert "«Somewhere Towards the End» — английский, 2008" in markdown


def test_markdown_without_original_keeps_legacy_header(tmp_path):
    markdown = render_candidate(tmp_path)
    header = next(line for line in markdown.splitlines() if line.startswith("| # | ID |"))
    assert header == (
        "| # | ID | Название | Автор | Добавлено | Обновлено | Контекст | Приоритет | Сигналы | "
        "Русское издание | Риск | Проверка риска | Комментарий о риске | Статус описания | "
        "Причина описания | Решение редактора | Источник | Заметка редактора |"
    )


def test_csv_without_original_writes_legacy_header(tmp_path):
    import csv

    from autoscout.weekly_report_row import EDITOR_FIELDNAMES
    from autoscout.weekly_report_row import write_editor_csv

    path = tmp_path / "editor.csv"
    write_editor_csv(path, [{"Title": "Ahora"}])
    with path.open(encoding="utf-8", newline="") as stream:
        assert next(csv.reader(stream)) == EDITOR_FIELDNAMES


def test_unknown_translation_not_found_has_no_output_in_any_format(tmp_path):
    import csv

    original = edition(
        is_translation="unknown",
        status="not_found",
        original_title="",
        original_language="",
        language_confirmed=False,
        original_first_published_year=None,
        confirmation_url="",
    )
    assert format_original_edition({"original_edition": original}) == ("", "", "")
    assert "Оригинал" not in _candidate_html({"original_edition": original})
    assert "Оригинал" not in render_candidate(tmp_path, original)
    with (tmp_path / "editor-facing.csv").open(encoding="utf-8", newline="") as stream:
        assert "Original edition" not in next(csv.reader(stream))


def test_neutral_row_preserves_original_and_optional_csv_field():
    from autoscout.review_rendering import _render_neutral_row
    from autoscout.weekly_report_row import editor_csv_fieldnames

    candidate = {"title": "Ahora", "primary_authors": ["Athill"], "original_edition": edition()}
    row = _render_neutral_row(
        candidate,
        {
            "Title": "",
            "Author": "",
            "Editor status": "",
            "Editor note": "",
            "Candidate ID": "",
            "Update delta": "new",
            "Priority label": "",
            "Signal count": "1",
            "Russian-edition status": "",
            "Source": "",
            "Source URL": "",
        },
    )
    assert (
        row["editor_row"]["Original edition"]
        == "«Somewhere Towards the End» — английский, 2008 (https://openlibrary.org/works/OL1W)"
    )
    assert "Original edition" in editor_csv_fieldnames([row["editor_row"]])
    candidate["original_edition"]["original_title"] = "changed"
    assert row["original_edition"]["original_title"] == "Somewhere Towards the End"
    assert "Original edition" not in editor_csv_fieldnames([{}])


def test_language_names_cover_canonical_languages():
    from autoscout.original_edition import OPEN_LIBRARY_LANGUAGE
    from autoscout.original_edition_presentation import _LANGUAGES

    assert set(_LANGUAGES) == set(OPEN_LIBRARY_LANGUAGE)


def test_inconsistent_or_unknown_language_is_hidden():
    for original in (
        edition(is_translation="unknown", translation_evidence_url=""),
        edition(original_language="xx"),
    ):
        assert format_original_edition({"original_edition": original}) == ("", "", "")


def test_neutral_row_discards_stale_original_column():
    from autoscout.review_rendering import _render_neutral_row
    from autoscout.weekly_report_row import editor_csv_fieldnames

    for original in (None, {}):
        candidate = {"title": "Ahora", "primary_authors": ["Athill"]}
        if original is not None:
            candidate["original_edition"] = original
        row = _render_neutral_row(candidate, {"Original edition": "stale"})["editor_row"]
        assert "Original edition" not in row
        assert "Original edition" not in editor_csv_fieldnames([row])
