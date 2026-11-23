"""The Russian-edition finding is a mark in the review, never an exclusion (#1181)."""

import csv
import json
import re
from pathlib import Path

import pytest
from state_store_support import seed_state

from autoscout.html_review import _candidate_html
from autoscout.json_io import write_json
from autoscout.review_rendering import render_review
from autoscout.russian_edition_presentation import format_russian_edition

ROOT = Path(__file__).resolve().parents[1]
RSL_URL = "https://search.rsl.ru/ru/record/01010101010"
LITRES_URL = "https://www.litres.ru/book/dzheyms-klir/atomnye-privychki-123/"
LIVELIB_URL = "https://www.livelib.ru/book/1003608943-atomnye-privychki"


def confirmed(**changes):
    record = {
        "status": "confirmed",
        "russian_title": "Атомные привычки",
        "russian_author": "Джеймс Клир",
        "publisher": "Питер",
        "year": 2020,
        "isbn": "978-5-4461-1430-6",
        "evidence_url": RSL_URL,
        "sources": ["rsl"],
        "confidence": "medium",
        "checked_on": "2026-11-21",
    }
    record.update(changes)
    return record


def litres_confirmed(**changes):
    return confirmed(evidence_url=LITRES_URL, sources=["litres"], **changes)


def unconfirmed(**changes):
    record = {
        "status": "unconfirmed",
        "russian_title": "Атомные привычки",
        "russian_author": "Джеймс Клир",
        "evidence_url": LIVELIB_URL,
        "checked_on": "2026-11-21",
    }
    record.update(changes)
    return record


def mark(record):
    return format_russian_edition({"russian_edition": record})


# ── What the mark says ────────────────────────────────────────────────────


def test_confirmed_with_publisher_and_year():
    assert mark(confirmed()) == ("Есть русское издание: «Атомные привычки» — Питер, 2020", RSL_URL, "Каталог РГБ")


def test_confirmed_with_year_only():
    assert mark(confirmed(publisher=""))[0] == "Есть русское издание: «Атомные привычки» — 2020"


def test_confirmed_with_neither_publisher_nor_year():
    assert mark(confirmed(publisher="", year=None))[0] == "Есть русское издание: «Атомные привычки»"


def test_litres_evidence_is_labelled_litres():
    assert mark(litres_confirmed())[1:] == (LITRES_URL, "ЛитРес")


def test_unconfirmed_names_livelib_and_says_the_catalogues_did_not_confirm():
    assert mark(unconfirmed()) == (
        "Возможно, есть русское издание: «Атомные привычки» (каталоги не подтвердили)",
        LIVELIB_URL,
        "LiveLib",
    )


@pytest.mark.parametrize(
    "record",
    [
        None,
        {},
        {"status": "no_find", "checked_on": "2026-11-21"},
        confirmed(evidence_url="https://example.com/book"),
        confirmed(russian_title=""),
        unconfirmed(evidence_url=RSL_URL),
        {**confirmed(), "extra": "field"},
    ],
    ids=["none", "empty", "no_find", "foreign_evidence", "no_title", "unconfirmed_on_catalogue", "extra_key"],
)
def test_no_find_missing_or_invalid_record_shows_nothing(record):
    assert mark(record) == ("", "", "")
    assert "русское издание" not in _candidate_html({"russian_edition": record}).casefold()


# ── HTML ──────────────────────────────────────────────────────────────────


def test_html_marked_book_shows_the_mark_on_the_row_and_links_the_evidence():
    html = _candidate_html({"title": "Atomic Habits", "russian_edition": confirmed()})
    summary, card = html.split("</summary>", 1)
    assert '<span class="ru-ed ru-ed--confirmed">Есть русское издание</span>' in summary
    assert "Атомные привычки" not in summary and "Питер" not in summary
    assert "Есть русское издание: «Атомные привычки» — Питер, 2020" in card
    assert f'href="{RSL_URL}"' in card and ">Каталог РГБ</a>" in card


def test_html_unconfirmed_row_mark_is_short_and_the_card_holds_the_details():
    summary, card = _candidate_html({"russian_edition": unconfirmed()}).split("</summary>", 1)
    assert '<span class="ru-ed ru-ed--unconfirmed">Возможно, есть русское издание</span>' in summary
    assert "Атомные привычки" not in summary
    assert "Возможно, есть русское издание: «Атомные привычки» (каталоги не подтвердили)" in card


def test_html_confirmed_and_unconfirmed_marks_look_different():
    confirmed_html = _candidate_html({"russian_edition": confirmed()})
    unconfirmed_html = _candidate_html({"russian_edition": unconfirmed()})
    assert 'class="ru-ed ru-ed--confirmed"' in confirmed_html
    assert 'class="ru-ed ru-ed--unconfirmed"' in unconfirmed_html
    assert "ru-ed--unconfirmed" not in confirmed_html and "ru-ed--confirmed" not in unconfirmed_html
    assert f'href="{LIVELIB_URL}"' in unconfirmed_html and ">LiveLib</a>" in unconfirmed_html


def test_html_escapes_text_from_the_record():
    html = _candidate_html({"russian_edition": confirmed(russian_title="Привычки <b>&</b>", publisher="<i>Питер</i>")})
    assert "Привычки &lt;b&gt;&amp;&lt;/b&gt;" in html and "&lt;i&gt;Питер&lt;/i&gt;" in html
    assert "<b>&</b>" not in html and "<i>Питер</i>" not in html


def test_html_unmarked_book_says_nothing_about_russian_editions():
    html = _candidate_html({"title": "Atomic Habits"})
    assert "ru-ed" not in html.split("<style", 1)[0] and "русское издание" not in html.casefold()


# ── Markdown, CSV and the whole review ────────────────────────────────────


LINE_BREAKS = ["\r\n", "\r", "\n", "\u2028", "\u2029", "\t"]


@pytest.mark.parametrize("line_break", LINE_BREAKS, ids=["crlf", "cr", "lf", "line_sep", "paragraph_sep", "tab"])
def test_a_line_break_in_the_title_or_publisher_becomes_one_space(line_break):
    record = confirmed(russian_title=f"Атомные{line_break}привычки", publisher=f"Питер{line_break}{line_break}Пресс")
    assert mark(record)[0] == "Есть русское издание: «Атомные привычки» — Питер Пресс, 2020"
    unconfirmed_text = mark(unconfirmed(russian_title=f"Атомные{line_break}привычки"))[0]
    assert unconfirmed_text == "Возможно, есть русское издание: «Атомные привычки» (каталоги не подтвердили)"


def render(tmp_path, *records):
    """Render a review of one book per record; a ``None`` record leaves the field out."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    candidates = []
    for number, record in enumerate(records, start=1):
        candidate = {
            "identity_key": f"book-{number}|author-{number}",
            "title": f"Book {number}",
            "primary_authors": [f"Author {number}"],
            "update_delta": "new",
            "priority_label": "B" if number % 2 else "C",
            "source_evidence": [],
        }
        if number == 1:
            candidate["editor_status"] = "interested"
        if number == 2:
            # An updated book without a decision: the "updated" tab and the undecided filter.
            candidate.update(update_delta="changed", last_material_change_date="2026-11-20")
        if record is not None:
            candidate["russian_edition"] = record
        candidates.append(candidate)
    write_json(tmp_path / "scouting-candidates.json", candidates)
    seed_state(tmp_path, candidate_history=[])
    write_json(tmp_path / "run-summary.json", {"run_date": "2026-11-21", "editor_facing_count": len(candidates)})
    assert render_review(tmp_path) == 0
    with (tmp_path / "editor-facing.csv").open(encoding="utf-8", newline="") as stream:
        csv_rows = list(csv.DictReader(stream))
    return (
        (tmp_path / "weekly-review.md").read_text(encoding="utf-8"),
        (tmp_path / "weekly-review.html").read_text(encoding="utf-8"),
        csv_rows,
    )


def table(markdown):
    lines = [line for line in markdown.splitlines() if line.startswith("| ")]
    header = [cell.strip() for cell in lines[0].strip("|").split("|")]
    return [dict(zip(header, (cell.strip() for cell in line.strip("|").split("|")), strict=True)) for line in lines[1:]]


def test_markdown_column_appears_only_when_some_row_has_a_mark(tmp_path):
    markdown, _, _ = render(tmp_path / "marked", None, confirmed())
    rows = table(markdown)
    assert [row["Поиск русского издания"] for row in rows if row["Название"] == "Book 2"] == [
        f"Есть русское издание: «Атомные привычки» — Питер, 2020 [Каталог РГБ]({RSL_URL})"
    ]
    assert [row["Поиск русского издания"] for row in rows if row["Название"] == "Book 1"] == [""]
    unmarked, _, _ = render(tmp_path / "unmarked", None, {"status": "no_find", "checked_on": "2026-11-21"})
    assert "Поиск русского издания" not in table(unmarked)[0]


def test_csv_column_holds_the_mark_and_is_empty_for_an_unmarked_book(tmp_path):
    _, _, rows = render(tmp_path / "marked", None, unconfirmed())
    by_title = {row["Title"]: row for row in rows}
    assert by_title["Book 2"]["Russian-edition finding"] == (
        f"Возможно, есть русское издание: «Атомные привычки» (каталоги не подтвердили) ({LIVELIB_URL})"
    )
    assert by_title["Book 1"]["Russian-edition finding"] == ""
    _, _, unmarked = render(tmp_path / "unmarked", None, None)
    assert "Russian-edition finding" not in unmarked[0]


def cards_by_tab(html):
    """Each tab of the page with the opening tag of every card in it, in page order."""
    panels = re.split(r'<section class="tab-panel" id="', html)[1:]
    return {panel.split('"', 1)[0]: re.findall(r'<details class="cand[^>]*>', panel) for panel in panels}


def test_a_confirmed_book_keeps_its_place_tabs_and_priority(tmp_path):
    plain_md, plain_html, plain_csv = render(tmp_path / "plain", None, None, None)
    marked_md, marked_html, marked_csv = render(tmp_path / "marked", None, confirmed(), None)
    assert "Есть русское издание" in marked_html
    assert cards_by_tab(marked_html) == cards_by_tab(plain_html)
    tabs = cards_by_tab(plain_html)
    assert set(tabs) == {"tab-all", "tab-updated"} and len(tabs["tab-updated"]) == 1
    assert {'data-decided="yes"' in card for card in tabs["tab-all"]} == {True, False}
    for plain_row, marked_row in zip(table(plain_md), table(marked_md), strict=True):
        marked_row.pop("Поиск русского издания")
        assert marked_row == plain_row
    for plain_row, marked_row in zip(plain_csv, marked_csv, strict=True):
        marked_row.pop("Russian-edition finding")
        assert marked_row == plain_row


# ── The live configs ──────────────────────────────────────────────────────


@pytest.mark.parametrize("config_name", ["current-weekly-live.json", "current-weekly-pilot-live.json"])
def test_live_config_leaves_no_empty_fixture_note(tmp_path, config_name):
    from autoscout.pipeline import apply_russian_edition_checks
    from autoscout.russian_editions import prepare_russian_edition_checks

    config_path = ROOT / "configs" / config_name
    checks = json.loads(config_path.read_text(encoding="utf-8"))["russian_edition_checks"]
    prepared = prepare_russian_edition_checks(config_path, tmp_path, checks, "2026-11-21")
    candidate = {"identity_key": "atomic habits|james clear", "title": "Atomic Habits", "primary_authors": ["Clear"]}
    apply_russian_edition_checks([candidate], prepared, "2026-11-21", config_path, tmp_path)
    assert "Empty fixture" not in json.dumps(candidate)


# ── The Markdown mark links its evidence ──────────────────────────────────


def test_markdown_mark_links_the_evidence_with_its_label(tmp_path):
    markdown, _, _ = render(tmp_path / "confirmed", confirmed(), unconfirmed(), litres_confirmed())
    cells = {row["Название"]: row["Поиск русского издания"] for row in table(markdown)}
    assert cells["Book 1"] == f"Есть русское издание: «Атомные привычки» — Питер, 2020 [Каталог РГБ]({RSL_URL})"
    assert cells["Book 2"] == (
        f"Возможно, есть русское издание: «Атомные привычки» (каталоги не подтвердили) [LiveLib]({LIVELIB_URL})"
    )
    assert cells["Book 3"].endswith(f" [ЛитРес]({LITRES_URL})")


def test_markdown_mark_with_a_pipe_or_brackets_in_the_title_keeps_the_row_and_the_link(tmp_path):
    markdown, _, _ = render(tmp_path, confirmed(russian_title="Привычки | [том 1]", publisher="", year=None), None)
    rows = table(markdown)  # strict zip: a stray pipe would make the row wider than the header
    cell = next(row["Поиск русского издания"] for row in rows if row["Название"] == "Book 1")
    assert cell == f"Есть русское издание: «Привычки &#124; \\[том 1\\]» [Каталог РГБ]({RSL_URL})"


# ── Candidate History's finding wins over a stale snapshot ────────────────


def render_with_history(tmp_path, snapshot_record, history_record):
    tmp_path.mkdir(parents=True, exist_ok=True)
    candidate = {
        "identity_key": "book-1|author-1",
        "candidate_id": "cand-1",
        "title": "Book 1",
        "primary_authors": ["Author 1"],
        "update_delta": "new",
        "priority_label": "B",
        "source_evidence": [],
    }
    history = {key: candidate[key] for key in ("identity_key", "candidate_id", "title", "primary_authors")}
    if snapshot_record is not None:
        candidate["russian_edition"] = snapshot_record
    if history_record is not None:
        history["russian_edition"] = history_record
    write_json(tmp_path / "scouting-candidates.json", [candidate])
    seed_state(tmp_path, candidate_history=[history])
    write_json(tmp_path / "run-summary.json", {"run_date": "2026-11-21", "editor_facing_count": 1})
    assert render_review(tmp_path, state_dir=tmp_path) == 0
    with (tmp_path / "editor-facing.csv").open(encoding="utf-8", newline="") as stream:
        csv_rows = list(csv.DictReader(stream))
    return (
        (tmp_path / "weekly-review.md").read_text(encoding="utf-8"),
        (tmp_path / "weekly-review.html").read_text(encoding="utf-8"),
        csv_rows,
    )


def test_a_stale_snapshot_does_not_hide_the_finding_history_holds(tmp_path):
    markdown, html, rows = render_with_history(tmp_path, {"status": "no_find", "checked_on": "2026-11-01"}, confirmed())
    text = "Есть русское издание: «Атомные привычки» — Питер, 2020"
    assert table(markdown)[0]["Поиск русского издания"] == f"{text} [Каталог РГБ]({RSL_URL})"
    assert 'class="ru-ed ru-ed--confirmed"' in html and f'href="{RSL_URL}"' in html
    assert rows[0]["Russian-edition finding"] == f"{text} ({RSL_URL})"


def test_the_snapshot_finding_shows_when_history_has_none(tmp_path):
    markdown, html, rows = render_with_history(tmp_path, unconfirmed(), None)
    assert table(markdown)[0]["Поиск русского издания"].endswith(f"[LiveLib]({LIVELIB_URL})")
    assert 'class="ru-ed ru-ed--unconfirmed"' in html
    assert rows[0]["Russian-edition finding"].startswith("Возможно, есть русское издание")


# ── Old "Empty fixture" notes do not outlive the removed checks ───────────


def write_pipeline_config(path, russian_checks, title="Truthful Check Book"):
    slug = title.casefold().replace(" ", "-")
    write_json(
        path,
        {
            "run_date": "2026-11-21",
            "sources": [
                {
                    "name": "Fixture Discovery",
                    "family": "media/curated list",
                    "url": "https://example.test/discovery",
                    "access_status": "open",
                    "extraction_method": "fixture",
                    "check_frequency": "weekly",
                    "fixture_candidates": [
                        {
                            "title": title,
                            "primary_authors": ["Alex Example"],
                            "source_url": f"https://example.test/discovery/{slug}",
                            "signal_strength": "major media list",
                            "evidence_note": "Fixture discovery mention.",
                            "candidate_category": "popular science",
                        }
                    ],
                }
            ],
            "russian_edition_checks": russian_checks,
        },
    )


def run_artifacts_text(output_dir, state_dir):
    from state_store_support import read_state

    texts = [path.read_text(encoding="utf-8") for path in sorted(output_dir.iterdir()) if path.is_file()]
    return "\n".join(texts) + json.dumps(read_state(state_dir, "candidate_history"), ensure_ascii=False)


def test_a_rediscovered_book_drops_an_inherited_empty_fixture_verification(tmp_path):
    from state_store_support import read_state

    from autoscout.pipeline import run_pipeline

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    seed_state(state_dir, candidate_history=[])
    old_config, new_config = tmp_path / "old.json", tmp_path / "new.json"
    write_pipeline_config(old_config, [{"retailer": "Ozon", "fixture_results": []}])
    write_pipeline_config(new_config, [])
    run_pipeline(old_config, tmp_path / "old-run", state_dir=state_dir)
    assert "Empty fixture is not proof of absence" in json.dumps(read_state(state_dir, "candidate_history"))

    run_pipeline(new_config, tmp_path / "new-run", state_dir=state_dir)

    assert (tmp_path / "new-run" / "russian-review-queue.csv").exists()
    assert "Empty fixture" not in run_artifacts_text(tmp_path / "new-run", state_dir)
    candidate = json.loads((tmp_path / "new-run" / "scouting-candidates.json").read_text(encoding="utf-8"))[0]
    assert candidate["russian_edition_verification"]["providers"] == []
    assert candidate["russian_edition_status"] == "unclear"


def test_a_rediscovered_book_keeps_a_verified_russian_edition_when_no_checks_are_configured(tmp_path):
    from state_store_support import read_state

    from autoscout.pipeline import run_pipeline

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    seed_state(state_dir, candidate_history=[])
    old_config, new_config = tmp_path / "old.json", tmp_path / "new.json"
    edition_url = "https://www.litres.ru/book/truthful-check-book-1/"
    write_pipeline_config(
        old_config,
        [
            {
                "retailer": "LitRes",
                "fixture_results": [
                    {
                        "candidate_title": "Truthful Check Book",
                        "primary_authors": ["Alex Example"],
                        "match_type": "same-work Russian-language edition",
                        "language": "Russian",
                        "edition_url": edition_url,
                    }
                ],
            }
        ],
    )
    write_pipeline_config(new_config, [])
    run_pipeline(old_config, tmp_path / "old-run", state_dir=state_dir)
    before = read_state(state_dir, "excluded_candidate_history")[0]
    assert before["exclusion_reason"] == "found in Russian"

    run_pipeline(new_config, tmp_path / "new-run", state_dir=state_dir)

    after = json.loads((tmp_path / "new-run" / "internal-exclusions.json").read_text(encoding="utf-8"))[0]
    assert after["exclusion_reason"] == "found in Russian"
    assert after["russian_edition_url"] == before["russian_edition_url"] == edition_url
    assert after["evidence"] == before["evidence"]


# ── A line break in the catalogue title keeps every format on one line ────


@pytest.mark.parametrize("line_break", ["\r\n", "\r"], ids=["crlf", "cr"])
def test_a_line_break_in_the_record_keeps_one_row_in_every_format(tmp_path, line_break):
    from autoscout.review_row_resolution import read_returned_rows

    record = confirmed(russian_title=f"Атомные{line_break}привычки", publisher=f"Питер{line_break}Пресс")
    markdown, html, csv_rows = render(tmp_path, record)
    text = "Есть русское издание: «Атомные привычки» — Питер Пресс, 2020"
    assert [row["Поиск русского издания"] for row in table(markdown)] == [f"{text} [Каталог РГБ]({RSL_URL})"]
    assert [row["Russian-edition finding"] for row in csv_rows] == [f"{text} ({RSL_URL})"]
    line = re.search(r"Есть русское издание: [^<]*", html)
    assert line is not None and text in line.group(0)
    assert not any(character in line.group(0) for character in "\r\n\u2028\u2029")
    returned = read_returned_rows(tmp_path / "weekly-review.md")
    assert [row.title for row in returned] == ["Book 1"]


# ── A verification in which no provider ran prints no diagnostics ─────────


def test_a_book_not_collected_again_shows_no_old_empty_fixture_diagnostics(tmp_path):
    state_dir = tmp_path / "state"
    review = run_twice(
        tmp_path, state_dir, [{"retailer": "Ozon", "fixture_results": []}], second_title="Another Fixture Book"
    )
    assert "Truthful Check Book" in review["weekly-review.md"]
    for name, text in review.items():
        assert "Empty fixture" not in text, name
        assert "фикстур" not in text, name
    # The history of the untouched book is left as it was.
    from state_store_support import read_state

    assert "Empty fixture is not proof of absence" in json.dumps(read_state(state_dir, "candidate_history"))


def test_a_book_not_collected_again_keeps_the_diagnostics_of_a_real_check(tmp_path):
    checks = [
        {
            "retailer": "Ozon",
            "fixture_results": [
                {
                    "candidate_title": "Truthful Check Book",
                    "primary_authors": ["Alex Example"],
                    "status": "no_match",
                }
            ],
        }
    ]
    review = run_twice(tmp_path, tmp_path / "state", checks, second_title="Another Fixture Book")
    section = review["weekly-review.md"].split("### Операционная проверка русского издания", 1)[1]
    line = next(line for line in section.splitlines() if line.startswith("- Truthful Check Book"))
    assert line.endswith("полностью; совпадение не найдено; Ozon: совпадение не найдено")


def test_a_check_of_this_run_that_did_not_run_still_names_its_provider(tmp_path):
    """The counterpart: a not-run verification dated in the run being rendered keeps its provider (#616)."""
    from autoscout.pipeline import run_pipeline

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    seed_state(state_dir, candidate_history=[])
    config = tmp_path / "config.json"
    write_pipeline_config(config, [{"retailer": "Ozon", "fixture_results": []}])
    run_pipeline(config, tmp_path / "run", state_dir=state_dir, effective_date=CURRENT_RUN_DATE)
    assert render_review(tmp_path / "run", state_dir=state_dir, config_path=config) == 0
    markdown = (tmp_path / "run" / "weekly-review.md").read_text(encoding="utf-8")
    section = markdown.split("### Операционная проверка русского издания", 1)[1]
    line = next(line for line in section.splitlines() if line.startswith("- Truthful Check Book"))
    assert "не запускалась" in line
    assert "Ozon" in line


EARLIER_RUN_DATE = "2026-11-14"
CURRENT_RUN_DATE = "2026-11-21"


def run_twice(tmp_path, state_dir, first_checks, second_title):
    """Collect the book with ``first_checks``, then run once more without it and without checks."""
    from autoscout.pipeline import run_pipeline

    state_dir.mkdir()
    seed_state(state_dir, candidate_history=[])
    old_config, new_config = tmp_path / "old.json", tmp_path / "new.json"
    write_pipeline_config(old_config, first_checks)
    write_pipeline_config(new_config, [], title=second_title)
    run_pipeline(old_config, tmp_path / "old-run", state_dir=state_dir, effective_date=EARLIER_RUN_DATE)
    run_pipeline(new_config, tmp_path / "new-run", state_dir=state_dir, effective_date=CURRENT_RUN_DATE)
    assert render_review(tmp_path / "new-run", state_dir=state_dir, config_path=new_config) == 0
    return {
        name: (tmp_path / "new-run" / name).read_text(encoding="utf-8")
        for name in ("weekly-review.md", "weekly-review.html", "editor-facing.csv")
    }


# ── An evidence address with whitespace or a control character is invalid ─


@pytest.mark.parametrize(
    "bad_url",
    [f"{RSL_URL}\r", f"{RSL_URL}\rx", "https://search.rsl.ru/ru/record/0101 0101", f"{RSL_URL}\t", f"{RSL_URL}\x00"],
    ids=["trailing_cr", "inner_cr", "space", "tab", "nul"],
)
def test_an_evidence_address_with_whitespace_or_a_control_character_shows_no_mark(tmp_path, bad_url):
    from autoscout.russian_edition import validate_russian_edition

    record = confirmed(evidence_url=bad_url)
    assert not validate_russian_edition(record)
    assert not validate_russian_edition(unconfirmed(evidence_url=bad_url.replace(RSL_URL, LIVELIB_URL)))
    markdown, html, csv_rows = render(tmp_path, record)
    assert "Поиск русского издания" not in table(markdown)[0]
    assert "русское издание" not in html.casefold()
    assert all(not row.get("Russian-edition finding") for row in csv_rows)


@pytest.mark.parametrize("separator", ["\n", "\u0085", " ", " "], ids=["lf", "nel", "line_sep", "para_sep"])
def test_an_evidence_address_with_any_line_break_is_invalid(separator):
    from autoscout.russian_edition import validate_russian_edition

    assert not validate_russian_edition(confirmed(evidence_url=f"{RSL_URL}{separator}"))


# ── Catalogue text is shown literally in the Markdown preview ─────────────


def test_markdown_mark_escapes_html_and_markdown_characters_from_the_record(tmp_path):
    title = "<b>Привычки</b> & *жирный* _курсив_ `код` [том] \\"
    record = confirmed(russian_title=title, publisher="Питер_Пресс*", year=2020)
    markdown, html, csv_rows = render(tmp_path, record, None)
    rows = table(markdown)  # strict zip: the row is as wide as the header
    cell = next(row["Поиск русского издания"] for row in rows if row["Название"] == "Book 1")
    assert cell == (
        "Есть русское издание: «&lt;b&gt;Привычки&lt;/b&gt; &amp; \\*жирный\\* \\_курсив\\_ \\`код\\` \\[том\\] \\\\»"
        f" — Питер\\_Пресс\\*, 2020 [Каталог РГБ]({RSL_URL})"
    )
    text = f"Есть русское издание: «{title}» — Питер_Пресс*, 2020"
    assert [row["Russian-edition finding"] for row in csv_rows if row["Title"] == "Book 1"] == [f"{text} ({RSL_URL})"]
    assert "&lt;b&gt;Привычки&lt;/b&gt; &amp; *жирный* _курсив_ `код` [том] \\" in html
    assert "<b>Привычки</b>" not in html
