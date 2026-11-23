"""A Probation Source is named in violet in the HTML review.

The editor judges a new source by the books it brings, so the source column
shows such a source in its own colour, with the week of the trial in the
tooltip. Nothing else changes: no card is hidden and no source changes status.
"""

from __future__ import annotations

import io
import json
import re
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest
from state_store_support import seed_state

from autoscout.html_review import build_html_review
from autoscout.review_rendering import render_review
from autoscout.source_trial import TRIAL_SOURCES_FIELD
from autoscout.source_trial import row_trial_fields
from autoscout.source_trial import trial_mark_text
from autoscout.source_trial import trial_sources
from autoscout.source_trial import trial_week

TRIAL = "New Publisher List"
ESTABLISHED = "Established Prize List"
RUN_DATE = "2026-11-24"


def _source(name: str, **fields: Any) -> dict[str, Any]:
    return {
        "name": name,
        "family": "media/curated list",
        "url": f"https://example.test/{name.lower().replace(' ', '-')}",
        **fields,
    }


def _trial_source(**fields: Any) -> dict[str, Any]:
    return _source(
        TRIAL,
        lifecycle_status="probation",
        probation_started_at="2026-11-15",
        probation_evidence_window_weeks=3,
        **fields,
    )


def test_only_active_probation_sources_are_on_trial() -> None:
    trials = trial_sources(
        [
            _trial_source(),
            _source(ESTABLISHED, lifecycle_status="established"),
            _source("Legacy List"),
            _source("Stopped Trial", lifecycle_status="probation", active=False),
            _source("Retired List", lifecycle_status="retired", active=False),
        ]
    )

    assert set(trials) == {TRIAL}
    assert trials[TRIAL].started_at == "2026-11-15"
    assert trials[TRIAL].window_weeks == 3


@pytest.mark.parametrize(
    ("run_date", "week"),
    [("2026-11-15", 1), ("2026-11-21", 1), ("2026-11-22", 2), ("2026-12-05", 3), ("2026-12-06", 4)],
)
def test_the_trial_week_counts_from_the_start_date(run_date: str, week: int) -> None:
    assert trial_week("2026-11-15", run_date) == week


def test_the_week_is_unknown_without_a_start_date_and_never_precedes_the_first() -> None:
    assert trial_week("", RUN_DATE) == 0
    assert trial_week("2026-11-15", "") == 0
    assert trial_week("2026-11-15", "2026-11-01") == 1


def test_the_mark_names_the_source_and_the_week_of_its_window() -> None:
    entry = {"name": TRIAL, "started_at": "2026-11-15", "window_weeks": 3}

    assert trial_mark_text(entry, "2026-11-24") == f"новый источник: {TRIAL} · неделя 2 из 3"


def test_past_its_window_the_mark_asks_for_a_decision_and_stays() -> None:
    entry = {"name": TRIAL, "started_at": "2026-11-15", "window_weeks": 3}

    assert trial_mark_text(entry, "2026-12-06") == f"новый источник: {TRIAL} · срок проверки истёк, нужно решение"
    assert trial_mark_text(entry, "2027-03-01") == f"новый источник: {TRIAL} · срок проверки истёк, нужно решение"


def test_a_trial_without_dates_is_marked_without_a_week_and_never_overdue() -> None:
    assert trial_mark_text({"name": TRIAL, "started_at": "", "window_weeks": 3}, RUN_DATE) == (
        f"новый источник: {TRIAL}"
    )
    assert trial_mark_text({"name": TRIAL, "started_at": "2026-11-15", "window_weeks": 0}, "2027-03-01") == (
        f"новый источник: {TRIAL} · неделя 16"
    )


def test_a_row_names_the_probation_sources_reporting_its_book() -> None:
    trials = trial_sources([_trial_source()])

    assert [entry["name"] for entry in row_trial_fields([TRIAL, ESTABLISHED], trials)[TRIAL_SOURCES_FIELD]] == [TRIAL]
    assert row_trial_fields([ESTABLISHED], trials) == {TRIAL_SOURCES_FIELD: []}


# ── The rendered review ──────────────────────────────────────────────────────

TRIAL_ONLY_KEY = "trial only|ann alone"
CORROBORATED_KEY = "both lists|bo both"
PLAIN_KEY = "plain book|cy plain"


def _record(identity_key: str, title: str, author: str, sources: list[str]) -> dict[str, Any]:
    return {
        "identity_key": identity_key,
        "title": title,
        "primary_authors": [author],
        "priority_label": "C",
        "independent_signal_count": 1,
        "russian_edition_status": "not found in Russian",
        "visible_risk_status": "no visible risk",
        "first_seen_date": "2026-11-15",
        "last_seen_date": "2026-11-15",
        "content_description": "Книга о том, как устроена повседневная жизнь небольшого города.",
        "source_evidence": [
            {
                "source_name": name,
                "source_family": "media/curated list",
                "source_url": f"https://example.test/{name.lower().replace(' ', '-')}",
                "evidence_note": "Listed by the source.",
            }
            for name in sources
        ],
    }


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _rendered_cards(tmp_path: Path, *sources: dict[str, Any], with_config: bool = True) -> dict[str, str]:
    """Render the review and return each card's opening tag and summary, by title."""
    state = tmp_path / "state"
    _write(state / "scouting-candidates.json", [])
    seed_state(
        state,
        candidate_history=[
            _record(TRIAL_ONLY_KEY, "Trial Only", "Ann Alone", [TRIAL]),
            _record(CORROBORATED_KEY, "Both Lists", "Bo Both", [TRIAL, ESTABLISHED]),
            _record(PLAIN_KEY, "Plain Book", "Cy Plain", [ESTABLISHED]),
        ],
    )
    _write(state / "run-summary.json", {"run_date": RUN_DATE, "editor_facing_count": 0})
    config = tmp_path / "config.json"
    _write(config, {"sources": list(sources)})
    with redirect_stdout(io.StringIO()):
        assert render_review(state, state, config_path=config if with_config else None) == 0
    html = next(state.glob("*.html")).read_text(encoding="utf-8")
    cards = re.findall(r"<details class=\"cand[^>]*>\s*<summary>.*?</summary>", html, flags=re.S)
    by_title = {}
    for title in ("Trial Only", "Both Lists", "Plain Book"):
        # Every tab repeats the card; they are rendered alike.
        by_title[title] = next(card for card in cards if f">{title}<" in card)
    return by_title


def test_the_review_names_a_probation_source_in_its_own_colour(tmp_path: Path) -> None:
    cards = _rendered_cards(tmp_path, _trial_source(), _source(ESTABLISHED, lifecycle_status="established"))
    tooltip = f'title="новый источник: {TRIAL} · неделя 2 из 3"'

    assert 'class="source-cell source-cell--trial"' in cards["Trial Only"]
    assert tooltip in cards["Trial Only"]
    assert "source-cell--trial" not in cards["Plain Book"]
    assert f'title="{ESTABLISHED}"' in cards["Plain Book"]
    # The colour is the whole mark: nothing is added under the title or to the row.
    assert all("новый источник" not in card.replace(tooltip, "") for card in cards.values())
    assert all(card.startswith('<details class="cand"') for card in cards.values())


def test_a_card_is_coloured_by_the_source_it_shows(tmp_path: Path) -> None:
    cards = _rendered_cards(tmp_path, _trial_source(), _source(ESTABLISHED, lifecycle_status="established"))
    shown = re.search(r'class="source-cell[^"]*" title="([^"]*)"', cards["Both Lists"]).group(1)

    # A book two sources reported shows one of them; only a shown trial source is violet.
    assert ("source-cell--trial" in cards["Both Lists"]) == shown.startswith("новый источник")


def test_ending_probation_removes_the_colour(tmp_path: Path) -> None:
    cards = _rendered_cards(
        tmp_path,
        _source(TRIAL, lifecycle_status="established"),
        _source(ESTABLISHED, lifecycle_status="established"),
    )

    assert all("source-cell--trial" not in card for card in cards.values())


def test_a_render_without_a_configuration_colours_nothing(tmp_path: Path) -> None:
    cards = _rendered_cards(tmp_path, _trial_source(), with_config=False)

    assert all("source-cell--trial" not in card for card in cards.values())


def test_the_colour_leaves_the_editors_decision_styling_alone() -> None:
    row = {
        "idx": 1,
        "identity_key": "book-one|alex-example",
        "title": "Book One",
        "authors": "Alex Example",
        "priority": "A",
        "signal_count": "1",
        "editor_status": "interested",
        "source": TRIAL,
        "source_url": "https://example.test/new",
        TRIAL_SOURCES_FIELD: [{"name": TRIAL, "started_at": "2026-11-15", "window_weeks": 3}],
    }
    summary = {
        "run_date": RUN_DATE,
        "delta_counts": {"new": 1, "upgraded": 0, "changed": 0, "unchanged": 0},
        "hidden_by_rule_count": 0,
        "editor_rows_count": 1,
        "total_count": 1,
    }

    html = build_html_review([row], summary)

    assert 'class="cand cand--decision-1"' in html
    assert 'class="source-cell source-cell--trial"' in html
