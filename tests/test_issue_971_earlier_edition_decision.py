"""Earlier edition matching uses sourced, author-compatible evidence."""

from state_store_support import seed_state

from autoscout.probable_duplicates import probable_duplicate_pairs
from tests.test_issue_916_duplicate_signals import ALIASES
from tests.test_issue_916_duplicate_signals import _published
from tests.test_issue_916_duplicate_signals import _record


def test_shared_published_title_and_author_compatibility() -> None:
    first = _published("German title", "Original title", ["Ann Writer"])
    second = _published("Spanish title", "Original title", ["Ann Writer"])
    (pair,) = probable_duplicate_pairs([first, second], ALIASES)["pairs"]
    assert pair["kind"] == "possible_editions"
    assert "shared_english_title" in pair["signals"]
    other = _published("Spanish title", "Original title", ["Bob Other"])
    assert not probable_duplicate_pairs([first, other], ALIASES)["pairs"]


def test_shared_title_dismissal_and_merged_id_are_skipped() -> None:
    first = _published("German title", "Original title", ["Ann Writer"])
    second = _published("Spanish title", "Original title", ["Ann Writer"])
    first["candidate_id"] = "candidate-a"
    second["candidate_id"] = "candidate-b"
    assert not probable_duplicate_pairs([first, second], ALIASES, [{"candidate_ids": ["candidate-a", "candidate-b"]}])[
        "pairs"
    ]
    second["candidate_id"] = "candidate-a"
    assert not probable_duplicate_pairs([first, second], ALIASES)["pairs"]


def test_confirmed_original_title_links_compatible_authors() -> None:
    from tests.test_issue_969_original_edition_review import edition

    first = _record("Ahora", ["Ann Writer"], original_edition=edition())
    second = _record("Somewhere Towards the End", ["Ann Writer"])
    (pair,) = probable_duplicate_pairs([first, second], ALIASES)["pairs"]
    assert pair["kind"] == "possible_editions"
    assert "original_title" in pair["signals"]


def test_invalid_or_unconfirmed_original_does_not_link() -> None:
    from tests.test_issue_969_original_edition_review import edition

    second = _record("Somewhere Towards the End", ["Ann Writer"])
    for original in ({}, edition(status="not_found")):
        first = _record("Ahora", ["Ann Writer"], original_edition=original)
        assert not probable_duplicate_pairs([first, second], ALIASES)["pairs"]


def test_original_title_does_not_link_incompatible_authors() -> None:
    from tests.test_issue_969_original_edition_review import edition

    first = _record("Ahora", ["Ann Writer"], original_edition=edition())
    second = _record("Somewhere Towards the End", ["Bob Other"])
    assert not probable_duplicate_pairs([first, second], ALIASES)["pairs"]


def test_translation_and_shared_title_signals_are_unioned() -> None:
    first = _published("German title", "Original title", ["Ann Writer"])
    second = _published("Original title", "Original title", ["Ann Writer"])
    (pair,) = probable_duplicate_pairs([first, second], ALIASES)["pairs"]
    assert {"translated_title", "shared_english_title"} <= set(pair["signals"])


def test_review_displays_decision_in_all_formats_without_changing_state(tmp_path) -> None:
    import hashlib

    from autoscout.json_io import write_json
    from autoscout.review_rendering import render_review

    state = tmp_path / "state"
    output = tmp_path / "output"
    state.mkdir()
    output.mkdir()
    current = _published("German title", "Original title", ["Ann Writer"])
    current.update(update_delta="new", source_evidence=[])
    previous = _published("Spanish title", "Original title", ["Ann Writer"])
    previous.update(editor_status="interested", editor_note="A note")
    write_json(output / "scouting-candidates.json", [current])
    write_json(output / "run-summary.json", {"run_date": "2026-01-15", "editor_facing_count": 1})
    seed_state(state, candidate_history=[current, previous])
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in state.iterdir()}
    assert render_review(output, state_dir=state) == 0
    assert before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in state.iterdir()}
    for filename in ("weekly-review.md", "weekly-review.html", "editor-facing.csv"):
        text = (output / filename).read_text(encoding="utf-8")
        assert "Уже рассматривали другое издание: «Spanish title»" in text
        assert "A note" in text


def test_original_and_translated_signals_are_unioned() -> None:
    from tests.test_issue_969_original_edition_review import edition

    first = _published("Ahora", "Somewhere Towards the End", ["Ann Writer"])
    first["original_edition"] = edition()
    second = _record("Somewhere Towards the End", ["Ann Writer"])
    (pair,) = probable_duplicate_pairs([first, second], ALIASES)["pairs"]
    assert {"translated_title", "original_title"} <= set(pair["signals"])


def _render(tmp_path, current, previous, inherited=None):
    import csv

    from autoscout.json_io import write_json
    from autoscout.review_rendering import render_review

    state, output = tmp_path / "state", tmp_path / "output"
    state.mkdir()
    output.mkdir()
    current.update(update_delta="new", source_evidence=[])
    write_json(output / "scouting-candidates.json", [current])
    write_json(output / "run-summary.json", {"run_date": "2026-01-15", "editor_facing_count": 1})
    seed_state(state, candidate_history=[current, *previous])
    if inherited is not None:
        assert render_review(output, state_dir=state) == 0
        path = output / "editor-facing.csv"
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            existing = list(reader)
            fields = list(reader.fieldnames)
        existing[0].update(inherited)
        fields.extend(key for key in inherited if key not in fields)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(existing)
    assert render_review(output, state_dir=state) == 0
    html = (output / "weekly-review.html").read_text(encoding="utf-8")
    markdown = (output / "weekly-review.md").read_text(encoding="utf-8")
    with (output / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return html, markdown, reader.fieldnames, list(reader)


def test_probable_duplicate_wording_in_all_formats(tmp_path) -> None:
    current = _record("Same title", ["Ann Writer"])
    previous = _record("Same title", ["Ann Writer"], identity_key="other-key", editor_status="interested")
    html, markdown, _, rows = _render(tmp_path, current, [previous])
    message = "Уже рассматривали эту книгу в другой записи: «Same title» — Интересно"
    assert message in html
    assert message in markdown
    assert rows[0]["Earlier edition decision"] == message


def test_decisions_are_sorted_by_title_and_identity(tmp_path) -> None:
    current = _published("Current", "Original", ["Ann Writer"])
    later = _published("Zulu", "Original", ["Ann Writer"])
    earlier = _published("Alpha", "Original", ["Ann Writer"])
    later.update(editor_status="interested", editor_note="later")
    earlier.update(editor_status="interested", editor_note="earlier")
    html, markdown, _, rows = _render(tmp_path, current, [later, earlier])
    first = "Уже рассматривали другое издание: «Alpha» — Интересно. Заметка: earlier"
    second = "Уже рассматривали другое издание: «Zulu» — Интересно. Заметка: later"
    assert first + "<br>" + second in html
    assert first + "; " + second in markdown
    assert rows[0]["Earlier edition decision"] == first + "; " + second


def test_undecided_link_has_no_decision(tmp_path) -> None:
    current = _published("Current", "Original", ["Ann Writer"])
    previous = _published("Previous", "Original", ["Ann Writer"])
    html, markdown, fields, _ = _render(tmp_path, current, [previous])
    assert "Уже рассматривали" not in html
    assert "Уже рассматривали" not in markdown
    assert "Earlier edition decision" not in fields


def test_unlinked_review_retains_legacy_shape(tmp_path) -> None:
    html, markdown, fields, _ = _render(tmp_path, _record("Current", ["Ann Writer"]), [])
    assert "Другое издание" not in html
    header = next(line for line in markdown.splitlines() if line.startswith("| # |"))
    assert (
        header
        == "| # | ID | Название | Автор | Добавлено | Обновлено | Контекст | Приоритет | Сигналы | Русское издание | Риск | Проверка риска | Комментарий о риске | Статус описания | Причина описания | Решение редактора | Источник | Заметка редактора |"
    )
    assert "Earlier edition decision" not in fields


def test_stale_inherited_csv_decision_is_removed(tmp_path) -> None:
    current = _record("Current", ["Ann Writer"])
    _, _, fields, rows = _render(tmp_path, current, [], {"Earlier edition decision": "stale value"})
    assert "Earlier edition decision" not in fields
    assert all("stale value" not in row.values() for row in rows)
