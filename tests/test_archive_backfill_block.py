"""The books of an archive backfill wait for the editor in one block of the review.

A backfill adds a whole archive in one day, and each book keeps the date of
its archive issue. The record therefore carries a marker naming the batch and
the day it was applied; the review shows the marked books that have no editor
decision as their own block, and a weekly run leaves the block alone. Every
book, name and date here is invented.
"""

from __future__ import annotations

import csv
import io
import json
import re
import subprocess
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_snapshot

from autoscout import archive_backfill_migration
from autoscout.archive_backfill import ARCHIVE_BACKFILL_FIELD
from autoscout.archive_backfill import archive_backfill
from autoscout.archive_backfill import archive_backfill_marker
from autoscout.archive_backfill import archive_batch_label
from autoscout.archive_backfill import earliest_archive_backfill
from autoscout.archive_backfill_migration import mark_archive_backfill
from autoscout.cli.weekly import run_mvp
from autoscout.editorial_taste_profile import add_profile_entry
from autoscout.pw_history_backfill import backfill_pw_history
from autoscout.review_dates import long_russian_date
from autoscout.review_rendering import render_review
from autoscout.weekly_workflow import _run_review_stage

PW_SOURCE = "Publishers Weekly Nonfiction Reviews"
BATCH = "pw-archive-2026"
APPLIED = "2026-11-11"
MARKER = {"batch": BATCH, "applied_date": APPLIED}
DESCRIPTION = (
    "Книга исследует, как прибрежные поселения менялись под воздействием торговли, миграции и решений местных властей. "
    "Автор сопоставляет исторические документы с рассказами жителей и прослеживает, как экономические перемены влияли "
    "на повседневную жизнь, городское пространство и представления людей о своём регионе."
)
COAST = ("An Invented Coast", "Ada Example", "9780306406157", "2026-01-05")
HARBOUR = ("The Quiet Harbour", "Ben Sample", "9781861972712", "2026-03-09")
COAST_KEY = "an invented coast|ada example"
MARSH = ("Salt Marsh Years", "Cy Witness", "9780140449136", "2026-05-11")
HARBOUR_KEY = "the quiet harbour|ben sample"
MARSH_KEY = "salt marsh years|cy witness"
WEEKLY_KEY = "plain history|di plain"
# The heading of the date section the weekly book is listed under (#1057).
WEEKLY_DATE = "1 октября 2026"
NEW_KEY = "a fresh arrival|eve newcomer"


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _history_record(key: str, title: str, author: str, **fields: Any) -> dict[str, Any]:
    return {
        "identity_key": key,
        "title": title,
        "primary_authors": [author],
        "priority_label": "C",
        "independent_signal_count": 1,
        "russian_edition_status": "not found in Russian",
        "visible_risk_status": "no visible risk",
        "first_seen_date": "2026-10-01",
        "last_seen_date": "2026-10-01",
        "content_description": "Книга о том, как устроена повседневная жизнь небольшого города.",
        "source_evidence": [
            {
                "source_name": "Active Prize List",
                "source_family": "media/curated list",
                "source_url": "https://example.test/active-prize-list",
                "evidence_note": "Listed by the source.",
            }
        ],
        **fields,
    }


def _pw_row(book: tuple[str, str, str, str]) -> dict[str, Any]:
    title, author, isbn, issue_date = book
    return {
        "title": title,
        "primary_authors": [author],
        "publisher": "Imaginary Press",
        "price": "$28",
        "format": "hardcover",
        "page_count": 240,
        "starred": False,
        "isbn13": isbn,
        "subsection": "History",
        "agent": "",
        "agency": "",
        "publication_month": "Nov.",
        "issue_date": issue_date,
    }


def _backfilled_state(tmp_path: Path, *books: tuple[str, str, str, str]) -> tuple[Path, Path]:
    """A state with one weekly book, plus *books* added by the real PW history backfill."""
    state = tmp_path / "state"
    _write(state / "scouting-candidates.json", [])
    seed_state(state, candidate_history=[_history_record(WEEKLY_KEY, "Plain History", "Di Plain")])
    _write(state / "run-summary.json", {"run_date": "2026-11-10", "editor_facing_count": 0})
    rows = [_pw_row(book) for book in books]
    _write(
        tmp_path / "stage.json",
        {
            "schema_version": 1,
            "year": 2026,
            "rows": rows,
            "issues": [{"issue_date": issue} for issue in sorted({row["issue_date"] for row in rows})],
        },
    )
    _write(
        tmp_path / "descriptions.json",
        {
            "schema_version": 1,
            "descriptions": {
                row["isbn13"]: {
                    "isbn13": row["isbn13"],
                    "title": row["title"],
                    "primary_authors": row["primary_authors"],
                    "issue_date": row["issue_date"],
                    "review_sha256": "a" * 64,
                    "content_description": DESCRIPTION,
                }
                for row in rows
            },
        },
    )
    config = tmp_path / "config.json"
    _write(
        config,
        {
            "sources": [
                {
                    "name": PW_SOURCE,
                    "contract_family": "pw-issue-pdf",
                    "family": "media/curated list",
                    "url": "https://www.publishersweekly.com/pw/reviews/",
                }
            ]
        },
    )
    with redirect_stdout(io.StringIO()):
        applied = backfill_pw_history(
            tmp_path / "stage.json", tmp_path / "descriptions.json", config, state, state, apply=True
        )
    assert applied["status"] == "applied"
    assert applied["added"] == len(books)
    return state, config


def _history(state: Path) -> dict[str, dict[str, Any]]:
    return {record["identity_key"]: record for record in read_state(state, "candidate_history")}


def _render(state: Path, config: Path | None = None) -> None:
    with redirect_stdout(io.StringIO()):
        assert render_review(state, state, config_path=config) == 0


def _csv_rows(state: Path) -> list[dict[str, str]]:
    with (state / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _html_groups(state: Path) -> dict[str, list[str]]:
    """Each section of the review's timeline (the "all" tab), by heading (a date or an archive block), with its titles."""
    html = (state / "weekly-review.html").read_text(encoding="utf-8")
    timeline = _timeline(html)
    groups: dict[str, list[str]] = {}
    for section in re.findall(r'<section class="group".*?</section>', timeline, re.DOTALL):
        heading = re.search(r"<h2>(.*?) <span", section)
        assert heading is not None
        groups[heading.group(1)] = re.findall(r'<span class="title">(.*?)</span>', section)
    return groups


def _timeline(html: str) -> str:
    """The "all" panel: the timeline of every book; «Обновлённые» holds second copies (#1063)."""
    match = re.search(r'<section class="tab-panel" id="tab-all".*?(?=<section class="tab-panel"|<footer>)', html, re.S)
    assert match is not None
    return match.group(0)


def _set_history(state: Path, key: str, **fields: Any) -> None:
    history = read_state(state, "candidate_history")
    for record in history:
        if record["identity_key"] == key:
            record.update(fields)
    seed_state(state, candidate_history=history)


# --- The marker. ---


def test_the_backfill_marks_each_record_it_adds_and_keeps_the_issue_date(tmp_path: Path) -> None:
    state, _config = _backfilled_state(tmp_path, COAST, HARBOUR)

    history = _history(state)
    today = date.today().isoformat()
    for key, book in ((COAST_KEY, COAST), (HARBOUR_KEY, HARBOUR)):
        assert history[key][ARCHIVE_BACKFILL_FIELD] == {"batch": BATCH, "applied_date": today}
        # The PW issue date stays where it was: it is editorial information.
        assert history[key]["first_seen_date"] == book[3]
        assert history[key]["source_evidence"][0]["source_check_date"] == book[3]
    # A record the backfill did not add carries no marker.
    assert ARCHIVE_BACKFILL_FIELD not in history[WEEKLY_KEY]


def test_a_malformed_marker_is_no_marker() -> None:
    assert archive_backfill({ARCHIVE_BACKFILL_FIELD: MARKER}) == MARKER
    assert archive_backfill({ARCHIVE_BACKFILL_FIELD: {"batch": BATCH}}) is None
    assert archive_backfill({ARCHIVE_BACKFILL_FIELD: {"batch": "PW Archive", "applied_date": APPLIED}}) is None
    assert archive_backfill({ARCHIVE_BACKFILL_FIELD: "pw-archive-2026"}) is None
    assert archive_backfill({}) is None
    with pytest.raises(ValueError, match="archive_backfill_applied_date_invalid"):
        archive_backfill_marker(BATCH, "last week")


def test_the_batch_has_a_russian_name() -> None:
    assert archive_batch_label(BATCH) == "Архив PW 2026"
    assert archive_batch_label("kirkus-archive-2025") == "Архив: kirkus-archive-2025"


def test_a_merged_record_keeps_the_earliest_marker() -> None:
    later = {ARCHIVE_BACKFILL_FIELD: {"batch": "pw-archive-2027", "applied_date": "2027-02-01"}}
    assert earliest_archive_backfill([{}, later, {ARCHIVE_BACKFILL_FIELD: MARKER}]) == MARKER
    assert earliest_archive_backfill([{}, {}]) is None


# --- The block in the review. ---


def test_the_review_shows_the_batch_as_its_own_block_with_a_count(tmp_path: Path) -> None:
    state, config = _backfilled_state(tmp_path, COAST, HARBOUR)
    _render(state, config)

    groups = _html_groups(state)
    # The batch came in today, after the weekly book's date, so its block comes first (#1057).
    assert list(groups) == ["Архив PW 2026", WEEKLY_DATE]
    assert sorted(groups["Архив PW 2026"]) == ["An Invented Coast", "The Quiet Harbour"]
    assert groups[WEEKLY_DATE] == ["Plain History"]
    html = (state / "weekly-review.html").read_text(encoding="utf-8")
    assert '<section class="group" data-group="archive" data-batch="pw-archive-2026">' in html
    assert 'Архив PW 2026 <span class="count">(2)</span>' in html
    assert "Без решения — 2 из 2 видимых книг архива." in html
    assert "с отметкой «обновлено» — 0, Архив PW 2026 без решения — 2, видимых — 3" in html

    markdown = (state / "weekly-review.md").read_text(encoding="utf-8")
    assert "**Архив PW 2026**: без решения — 2 из 2 видимых книг архива" in markdown
    table = [line for line in markdown.splitlines() if line.startswith("| ") and "An Invented" in line]
    assert "| Архив PW 2026 |" in table[0]
    # The Markdown keeps the block contiguous where the page places it, with its name in an «Архив» column (#1057).
    rows_md = [line for line in markdown.splitlines() if line.startswith("| ")]
    assert "| Добавлено | Обновлено | Архив |" in rows_md[0]
    assert [line.split(" | ")[2] for line in rows_md[1:]][-1] == "Plain History"
    assert "| 2026-10-01 |  |  |" in rows_md[-1]

    rows = _csv_rows(state)
    assert [row["Archive batch"] for row in rows] == ["Архив PW 2026", "Архив PW 2026", ""]
    # The block is contiguous, ahead of the previously seen books.
    assert rows[-1]["Candidate ID"] == WEEKLY_KEY


def test_a_review_without_archive_books_is_unchanged(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _write(state / "scouting-candidates.json", [])
    seed_state(state, candidate_history=[_history_record(WEEKLY_KEY, "Plain History", "Di Plain")])
    _write(state / "run-summary.json", {"run_date": "2026-11-10", "editor_facing_count": 0})
    _render(state)

    assert list(_html_groups(state)) == [WEEKLY_DATE]
    assert "Archive batch" not in _csv_rows(state)[0]
    assert "Архив" not in (state / "weekly-review.md").read_text(encoding="utf-8")
    html = (state / "weekly-review.html").read_text(encoding="utf-8")
    assert '<section class="group" data-group="archive"' not in html
    assert "<dt>Архив</dt>" not in html


def test_the_added_date_is_the_day_the_batch_came_in_not_the_issue_date(tmp_path: Path) -> None:
    """Two issues, one batch: the review keeps the batch together under the day it came in."""
    state, config = _backfilled_state(tmp_path, COAST, HARBOUR)
    _set_history(state, COAST_KEY, **{ARCHIVE_BACKFILL_FIELD: MARKER})
    _set_history(state, HARBOUR_KEY, **{ARCHIVE_BACKFILL_FIELD: MARKER})
    _render(state, config)

    html = (state / "weekly-review.html").read_text(encoding="utf-8")
    (block,) = re.findall(r'<section class="group" data-group="archive".*?</section>', html, re.DOTALL)
    archive_cards = re.findall(r"<details class=\"cand[^>]*>", block)
    assert len(archive_cards) == 2
    for card in archive_cards:
        assert f'data-added="{APPLIED}"' in card
    # The issue dates differ and neither is used as the added date.
    assert 'data-added="2026-01-05"' not in html
    assert 'data-added="2026-03-09"' not in html
    assert f'<span class="num c-added">{APPLIED}</span>' in html
    # The issue date stays on the card.
    assert "<dt>Архив</dt><dd>Архив PW 2026 · выпуск от 2026-01-05</dd>" in html
    assert "<dt>Архив</dt><dd>Архив PW 2026 · выпуск от 2026-03-09</dd>" in html
    # The block is one section, never split by the issue dates.
    assert list(_html_groups(state)) == ["Архив PW 2026", WEEKLY_DATE]


@pytest.mark.parametrize("other_batch_key", [COAST_KEY, HARBOUR_KEY, MARSH_KEY])
def test_two_batches_never_interleave_in_the_csv(tmp_path: Path, other_batch_key: str) -> None:
    state, config = _backfilled_state(tmp_path, COAST, HARBOUR, MARSH)
    _set_history(state, other_batch_key, archive_backfill={"batch": "pw-archive-2025", "applied_date": "2026-11-09"})
    _render(state, config)

    batches = [row["Archive batch"] for row in _csv_rows(state)]
    assert batches == ["Архив PW 2025", "Архив PW 2026", "Архив PW 2026", ""]


def test_the_block_note_names_the_first_day_of_a_batch_applied_over_two_days(tmp_path: Path) -> None:
    state, config = _backfilled_state(tmp_path, COAST, HARBOUR)
    _set_history(
        state, COAST_KEY, editor_status="interested", archive_backfill={"batch": BATCH, "applied_date": "2026-11-09"}
    )
    _render(state, config)

    html = (state / "weekly-review.html").read_text(encoding="utf-8")
    assert "Книги добавлены из архива одним пакетом 2026-11-09." in html


def test_a_decided_book_leaves_the_block_and_keeps_its_marker(tmp_path: Path) -> None:
    state, config = _backfilled_state(tmp_path, COAST, HARBOUR)
    _set_history(state, COAST_KEY, editor_status="interested")
    _render(state, config)

    groups = _html_groups(state)
    assert groups["Архив PW 2026"] == ["The Quiet Harbour"]
    # Decided, it joins the date the batch came in (#1057).
    assert groups[long_russian_date(date.today().isoformat())] == ["An Invented Coast"]
    html = (state / "weekly-review.html").read_text(encoding="utf-8")
    assert "Без решения — 1 из 2 видимых книг архива." in html
    by_key = {row["Candidate ID"]: row for row in _csv_rows(state)}
    assert by_key[COAST_KEY]["Archive batch"] == ""
    assert by_key[HARBOUR_KEY]["Archive batch"] == "Архив PW 2026"
    assert archive_backfill(_history(state)[COAST_KEY]) is not None


def test_hard_rules_and_editor_hides_still_withhold_archive_books(tmp_path: Path) -> None:
    state, config = _backfilled_state(tmp_path, COAST, HARBOUR)
    _set_history(state, COAST_KEY, content_description="Сборник: простые рецепты прибрежных поселений на каждый день.")
    with redirect_stdout(io.StringIO()):
        assert (
            add_profile_entry(
                state,
                kind="hard_rule",
                scope="topic",
                subject="кулинарные книги",
                quote="Кулинарные книги нам не нужны.",
                provenance="editor conversation",
                created_by="test",
                matching_terms=["рецепты"],
                confirmed_by_editor=True,
            )
            == 0
        )
    seed_state(state, candidate_visibility=[{"identity_key": HARBOUR_KEY, "visibility": "hidden"}])
    _render(state, config)

    assert list(_html_groups(state)) == [WEEKLY_DATE]
    assert [row["Candidate ID"] for row in _csv_rows(state)] == [WEEKLY_KEY]
    withheld = {record["identity_key"] for record in _read(state / "hidden-from-review.json")}
    assert withheld == {COAST_KEY}
    review = (state / "weekly-review.html").read_text(encoding="utf-8") + (state / "weekly-review.md").read_text(
        encoding="utf-8"
    )
    assert "An Invented Coast" not in review
    assert "The Quiet Harbour" not in review


# --- The block after a weekly run. ---


def _fixture_source(name: str, *books: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "family": "media/curated list",
        "url": f"https://example.test/{name.lower().replace(' ', '-')}",
        "access_status": "open",
        "extraction_method": "fixture",
        "check_frequency": "weekly",
        "language": "en",
        "fixture_candidates": list(books),
    }


def _fixture_book(title: str, author: str) -> dict[str, Any]:
    return {
        "title": title,
        "primary_authors": [author],
        "source_url": f"https://example.test/{title.lower().replace(' ', '-')}",
        "signal_strength": "winner",
        "evidence_note": "Listed by the source.",
        "candidate_category": "adult nonfiction",
        "russian_edition_status": "not found in Russian",
        "visible_risk_status": "no visible risk",
    }


def test_the_block_survives_a_weekly_run(tmp_path: Path) -> None:
    """The next run reports a new book and one of the archive books; neither archive book leaves the block."""
    state, _config = _backfilled_state(tmp_path, COAST, HARBOUR)
    before = _history(state)
    weekly_config = tmp_path / "weekly.json"
    _write(
        weekly_config,
        {
            "run_date": "2026-11-17",
            "sources": [
                _fixture_source(
                    "Weekly Prize List",
                    _fixture_book("A Fresh Arrival", "Eve Newcomer"),
                    # The weekly source cites a book the archive already added.
                    _fixture_book(COAST[0], COAST[1]),
                )
            ],
        },
    )

    with redirect_stdout(io.StringIO()):
        run_mvp(weekly_config, state, state)
        assert _run_review_stage(state, state, "2026-11-17", weekly_config)["returncode"] == 0

    after = _history(state)
    assert NEW_KEY in after
    assert after[COAST_KEY]["last_seen_date"] == "2026-11-17"
    for key in (COAST_KEY, HARBOUR_KEY):
        assert after[key][ARCHIVE_BACKFILL_FIELD] == before[key][ARCHIVE_BACKFILL_FIELD]
        assert after[key]["candidate_id"] == before[key]["candidate_id"]
        assert after[key]["first_seen_date"] == before[key]["first_seen_date"]

    groups = _html_groups(state)
    assert groups["17 ноября 2026"] == ["A Fresh Arrival"]
    assert sorted(groups["Архив PW 2026"]) == ["An Invented Coast", "The Quiet Harbour"]
    assert groups[WEEKLY_DATE] == ["Plain History"]
    html = (state / "weekly-review.html").read_text(encoding="utf-8")
    # The weekly source raised the archive book it cited: it stays in the block and carries the mark (#1057).
    assert (
        "добавлено в последний запуск — 1, с отметкой «обновлено» — 1, Архив PW 2026 без решения — 2, видимых — 4"
        in html
    )
    (block,) = re.findall(r'<section class="group" data-group="archive".*?</section>', html, re.DOTALL)
    assert re.search(r'>An Invented Coast</span><span class="upd">⬆ обновлено 17 ноября</span>', block)
    assert block.count('class="upd"') == 1
    rows = _csv_rows(state)
    assert rows[0]["Candidate ID"] == NEW_KEY
    assert {row["Candidate ID"] for row in rows if row["Archive batch"]} == {COAST_KEY, HARBOUR_KEY}
    assert "**Архив PW 2026**: без решения — 2 из 2 видимых книг архива" in (state / "weekly-review.md").read_text(
        encoding="utf-8"
    )


# --- The one-off migration for a batch applied before the marker existed. ---


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.test", *args],
        check=True,
        capture_output=True,
    )


def _repository(tmp_path: Path) -> tuple[Path, Path]:
    """A repository whose second commit adds two books and new evidence on an existing one."""
    repo = tmp_path / "repo"
    state = repo / "runs" / "weekly"
    existing = _history_record(WEEKLY_KEY, "Plain History", "Di Plain", candidate_id="candidate-0001")
    seed_state(state, candidate_history=[existing])
    _write(state / "scouting-candidates.json", [])
    _write(state / "run-summary.json", {"run_date": "2026-11-10", "editor_facing_count": 0})
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "before")
    gained = dict(existing)
    gained["source_evidence"] = [*existing["source_evidence"], {"source_name": PW_SOURCE}]
    added = [
        _history_record(COAST_KEY, COAST[0], COAST[1], candidate_id="candidate-0002", first_seen_date=COAST[3]),
        _history_record(HARBOUR_KEY, HARBOUR[0], HARBOUR[1], candidate_id="candidate-0003", first_seen_date=HARBOUR[3]),
    ]
    seed_state(state, candidate_history=[gained, *added])
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "backfill")
    _write(repo / "config.json", {"sources": []})
    return repo, state


def _migrate(repo: Path, state: Path, *, apply: bool = False) -> dict[str, Any]:
    with redirect_stdout(io.StringIO()):
        return archive_backfill_migration.run(
            state,
            state,
            repo / "config.json",
            before_ref="HEAD^",
            after_ref="HEAD",
            batch=BATCH,
            applied_date=APPLIED,
            apply=apply,
        )


def test_the_migration_previews_then_marks_only_the_records_the_commit_added(tmp_path: Path) -> None:
    repo, state = _repository(tmp_path)
    untouched = state_snapshot(state)

    preview = _migrate(repo, state)
    assert preview["status"] == "preview"
    assert (preview["added_between_refs"], preview["to_mark"], preview["missing_from_history"]) == (2, 2, 0)
    # The record that only gained evidence is counted and left out.
    assert preview["existing_records_with_changed_evidence_not_marked"] == 1
    assert state_snapshot(state) == untouched
    assert not (state / "weekly-review.html").exists()

    applied = _migrate(repo, state, apply=True)
    assert (applied["status"], applied["marked"]) == ("applied", 2)
    history = _history(state)
    assert history[COAST_KEY][ARCHIVE_BACKFILL_FIELD] == MARKER
    assert history[HARBOUR_KEY][ARCHIVE_BACKFILL_FIELD] == MARKER
    assert ARCHIVE_BACKFILL_FIELD not in history[WEEKLY_KEY]
    # Nothing but the marker changed.
    for record in json.loads(untouched["candidate_history"]):
        stored = dict(history[record["identity_key"]])
        stored.pop(ARCHIVE_BACKFILL_FIELD, None)
        assert stored == record
    assert sorted(_html_groups(state)["Архив PW 2026"]) == ["An Invented Coast", "The Quiet Harbour"]

    again = _migrate(repo, state, apply=True)
    assert (again["status"], again["marked"], again["already_marked"]) == ("applied", 0, 2)


def test_the_migration_follows_a_renamed_key_and_reports_a_missing_one() -> None:
    before: list[dict[str, Any]] = []
    after = [
        {"identity_key": "old key|a", "candidate_id": "candidate-1"},
        {"identity_key": "gone|b", "candidate_id": "candidate-2"},
    ]
    history = [{"identity_key": "new key|a", "candidate_id": "candidate-1"}]

    updated, report = mark_archive_backfill(before, after, history, batch=BATCH, applied_date=APPLIED)

    assert updated[0][ARCHIVE_BACKFILL_FIELD] == MARKER
    assert (report["to_mark"], report["matched_by_candidate_id"]) == (1, 1)
    assert report["missing_identity_keys"] == ["gone|b"]
    # The caller's history is not modified in place.
    assert ARCHIVE_BACKFILL_FIELD not in history[0]


def test_the_migration_leaves_a_record_marked_for_another_batch() -> None:
    other = {"batch": "pw-archive-2025", "applied_date": "2025-12-01"}
    after = [{"identity_key": "k|a", "candidate_id": "candidate-1"}]
    history = [{"identity_key": "k|a", "candidate_id": "candidate-1", ARCHIVE_BACKFILL_FIELD: other}]

    updated, report = mark_archive_backfill([], after, history, batch=BATCH, applied_date=APPLIED)

    assert updated[0][ARCHIVE_BACKFILL_FIELD] == other
    assert (report["to_mark"], report["marked_for_another_batch"]) == (0, 1)


def test_the_migration_refuses_to_write_during_a_weekly_run(tmp_path: Path, monkeypatch) -> None:
    repo, state = _repository(tmp_path)
    untouched = state_snapshot(state)
    monkeypatch.setattr(archive_backfill_migration, "weekly_run_in_progress", lambda _state_dir: True)

    blocked = _migrate(repo, state, apply=True)

    assert (blocked["status"], blocked["reason_code"]) == ("blocked", "weekly_run_in_progress")
    assert state_snapshot(state) == untouched


def test_the_migration_fails_on_a_revision_git_does_not_have(tmp_path: Path) -> None:
    repo, state = _repository(tmp_path)
    with pytest.raises(ValueError, match="archive_marker_ref_unreadable"):
        archive_backfill_migration.run(
            state,
            state,
            repo / "config.json",
            before_ref="no-such-revision",
            after_ref="HEAD",
            batch=BATCH,
            applied_date=APPLIED,
        )
