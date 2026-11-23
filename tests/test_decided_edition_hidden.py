"""An undecided card is withheld when the editor already settled another edition of the book.

The link between the records is the one the review already shows as
«Другое издание». A decision that settles the book (already known, rejected)
keeps the other editions out of the review; a decision of interest does not.
Nothing is written to state, so dismissing the link or changing the decision
brings the card back.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from state_store_support import seed_state
from state_store_support import state_snapshot

from autoscout.json_io import write_json
from autoscout.review_hiding import DECIDED_EDITION_HIDDEN_LABEL
from autoscout.review_hiding import EDITOR_STATUSES_THAT_SETTLE_A_BOOK
from autoscout.review_hiding import HIDDEN_BY_DECIDED_EDITION
from autoscout.review_hiding import HIDDEN_FROM_REVIEW_ARTIFACT
from autoscout.review_rendering import render_review
from tests.test_issue_916_duplicate_signals import _published
from tests.test_issue_916_duplicate_signals import _record


def _render(tmp_path: Path, current: dict[str, Any], others: list[dict[str, Any]]) -> tuple[str, str, list[Any]]:
    state, output = tmp_path / "state", tmp_path / "output"
    state.mkdir(exist_ok=True)
    output.mkdir(exist_ok=True)
    current.update(update_delta="new", source_evidence=[])
    write_json(output / "scouting-candidates.json", [current])
    write_json(output / "run-summary.json", {"run_date": "2026-01-15", "editor_facing_count": 1})
    seed_state(state, candidate_history=[current, *others])
    before = state_snapshot(state)
    assert render_review(output, state_dir=state) == 0
    assert state_snapshot(state) == before
    hidden_path = output / HIDDEN_FROM_REVIEW_ARTIFACT
    hidden = json.loads(hidden_path.read_text(encoding="utf-8")) if hidden_path.exists() else []
    return (
        (output / "weekly-review.html").read_text(encoding="utf-8"),
        (output / "weekly-review.md").read_text(encoding="utf-8"),
        hidden,
    )


def _editions(status: str, note: str = "") -> tuple[dict[str, Any], dict[str, Any]]:
    translation = _published("Die 1%-Methode", "Atomic Habits", ["James Clear"])
    original = _published("Atomic Habits", "Atomic Habits", ["James Clear"])
    original.update(editor_status=status, editor_note=note)
    return translation, original


@pytest.mark.parametrize("status", sorted(EDITOR_STATUSES_THAT_SETTLE_A_BOOK))
def test_an_undecided_edition_of_a_settled_book_is_left_out_and_listed(tmp_path: Path, status: str) -> None:
    translation, original = _editions(status)

    html, markdown, hidden = _render(tmp_path, translation, [original])

    # The only mention left is the note on the decided card.
    assert html.count("Die 1%-Methode") == html.count("«Die 1%-Methode»") == 1
    assert "Die 1%-Methode" not in markdown
    (card,) = hidden
    assert card["identity_key"] == translation["identity_key"]
    assert card["hidden_by"] == HIDDEN_BY_DECIDED_EDITION
    assert [decided["editor_status"] for decided in card["decided_records"]] == [status]


def test_the_review_counts_the_cards_it_leaves_out(tmp_path: Path) -> None:
    translation, original = _editions("already_known")

    html, markdown, _ = _render(tmp_path, translation, [original])

    assert f"{DECIDED_EDITION_HIDDEN_LABEL} — 1" in html
    assert f"{DECIDED_EDITION_HIDDEN_LABEL} — 1" in markdown


def test_the_decided_card_names_the_edition_left_out(tmp_path: Path) -> None:
    translation, original = _editions("already_known")

    html, _, _ = _render(tmp_path, translation, [original])

    assert "Другие издания" in html
    assert "«Die 1%-Methode»" in html


@pytest.mark.parametrize("status", ["interested", "maybe_watch", "needs_deeper_analysis"])
def test_a_new_edition_of_a_book_she_wants_stays_in_the_review(tmp_path: Path, status: str) -> None:
    translation, original = _editions(status)

    html, _, hidden = _render(tmp_path, translation, [original])

    assert "Уже рассматривали другое издание: «Atomic Habits»" in html
    assert hidden == []
    assert DECIDED_EDITION_HIDDEN_LABEL not in html


def test_one_wanted_edition_keeps_the_card_beside_a_rejected_one(tmp_path: Path) -> None:
    translation, rejected = _editions("reject")
    wanted = _published("Hábitos atómicos", "Atomic Habits", ["James Clear"])
    wanted.update(editor_status="interested")

    _, _, hidden = _render(tmp_path, translation, [rejected, wanted])

    assert hidden == []


def test_a_card_she_decided_herself_stays_whatever_the_other_edition_says(tmp_path: Path) -> None:
    translation, original = _editions("reject")
    translation.update(editor_status="interested")

    _, _, hidden = _render(tmp_path, translation, [original])

    assert hidden == []


def test_a_duplicate_record_of_a_settled_book_is_left_out_too(tmp_path: Path) -> None:
    current = _record("Same title", ["Ann Writer"])
    previous = _record("Same title", ["Ann Writer"], identity_key="other-key", editor_status="already_known")

    _, _, hidden = _render(tmp_path, current, [previous])

    assert [card["hidden_by"] for card in hidden] == [HIDDEN_BY_DECIDED_EDITION]


def test_dismissing_the_link_brings_the_card_back(tmp_path: Path) -> None:
    translation, original = _editions("already_known")
    translation["candidate_id"], original["candidate_id"] = "candidate-a", "candidate-b"
    _, _, hidden = _render(tmp_path, translation, [original])
    assert len(hidden) == 1
    write_json(
        tmp_path / "state" / "probable-duplicate-dismissals.json",
        [{"candidate_ids": ["candidate-a", "candidate-b"]}],
    )

    html, _, hidden = _render(tmp_path, translation, [original])

    assert hidden == []
    assert DECIDED_EDITION_HIDDEN_LABEL not in html


def test_a_book_with_no_author_is_not_left_out_for_a_namesake(tmp_path: Path) -> None:
    current = _record("Home", [])
    rejected = _record("Home", ["Toni Morrison"], identity_key="home|toni morrison", editor_status="reject")

    html, _, hidden = _render(tmp_path, current, [rejected])

    assert hidden == []
    assert "Уже рассматривали" in html


def test_a_shared_surname_alone_does_not_leave_a_book_out(tmp_path: Path) -> None:
    current = _record("Home", ["Ali Smith"])
    rejected = _record("Home", ["Zadie Smith"], identity_key="home|zadie smith", editor_status="reject")

    html, _, hidden = _render(tmp_path, current, [rejected])

    assert hidden == []
    assert "Уже рассматривали" in html


def test_a_title_that_only_contains_the_other_does_not_leave_a_book_out(tmp_path: Path) -> None:
    current = _record("I’m Glad I’m Still Alive", ["Christopher Lloyd"])
    rejected = _record("Glad I'm Still Alive", ["Christopher Lloyd"], editor_status="reject")

    html, _, hidden = _render(tmp_path, current, [rejected])

    assert hidden == []
    assert "Уже рассматривали" in html


def test_another_volume_of_a_rejected_series_is_not_left_out(tmp_path: Path) -> None:
    current = _record("Letters: 1930-1940", ["Ann Writer"], identity_key="letters 1930|ann writer")
    rejected = _record("Letters: 1925-1975", ["Ann Writer"], identity_key="letters 1925|ann writer")
    rejected.update(editor_status="reject")

    html, _, hidden = _render(tmp_path, current, [rejected])

    assert hidden == []
    assert "Уже рассматривали" in html


def test_a_subtitle_on_one_side_only_still_names_the_same_book(tmp_path: Path) -> None:
    current = _record("Home: A Novel", ["Ann Writer"], identity_key="home a novel|ann writer")
    rejected = _record("Home", ["Ann Writer"], identity_key="home|ann writer", editor_status="reject")

    _, _, hidden = _render(tmp_path, current, [rejected])

    assert [card["hidden_by"] for card in hidden] == [HIDDEN_BY_DECIDED_EDITION]


def test_a_translated_volume_of_a_rejected_series_is_not_left_out(tmp_path: Path) -> None:
    current = _published("Cartas: 1930-1940", "Letters: 1930-1940", ["Ann Writer"])
    rejected = _record("Letters: 1925-1975", ["Ann Writer"], editor_status="reject")

    html, _, hidden = _render(tmp_path, current, [rejected])

    assert hidden == []
    assert "Уже рассматривали" in html


def test_a_translation_with_its_own_subtitle_is_left_out_when_the_english_title_matches(tmp_path: Path) -> None:
    current = _published("Hábitos: pequeños cambios", "Habits: Small Changes", ["Ann Writer"])
    rejected = _record("Habits: Small Changes", ["Ann Writer"], editor_status="reject")

    _, _, hidden = _render(tmp_path, current, [rejected])

    assert [card["hidden_by"] for card in hidden] == [HIDDEN_BY_DECIDED_EDITION]
