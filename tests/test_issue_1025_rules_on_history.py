"""Hard Rules and source retirement reach cards already in Candidate History (#1025).

The cumulative review is what the editor reads, and most of it is Candidate
History. A Hard Rule used to hide only the candidates of the run that matched
it, and retiring a source only stopped collection, so the editor kept seeing
books she had asked to exclude.
"""

from __future__ import annotations

import csv
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_snapshot

from autoscout.cli.weekly import run_mvp
from autoscout.editorial_taste_profile import add_profile_entry
from autoscout.editorial_taste_profile import revoke_profile_entry
from autoscout.editorial_taste_profile import set_profile_entry_terms
from autoscout.pw_history_backfill import backfill_pw_history
from autoscout.review_rendering import render_review

RETIRED_SOURCE = "Retired Catalogue"
ACTIVE_SOURCE = "Active Prize List"
COOKBOOK_KEY = "weeknight table|ada cook"
RETIRED_ONLY_KEY = "catalogue novelty|ben retired"
MIXED_KEY = "twice reported|cy mixed"
PLAIN_KEY = "plain history|di plain"


def _evidence(source_name: str) -> dict[str, Any]:
    return {
        "source_name": source_name,
        "source_family": "media/curated list",
        "source_url": f"https://example.test/{source_name.lower().replace(' ', '-')}",
        "evidence_note": "Listed by the source.",
    }


def _record(identity_key: str, title: str, author: str, sources: list[str], **fields: Any) -> dict[str, Any]:
    return {
        "identity_key": identity_key,
        "title": title,
        "primary_authors": [author],
        "priority_label": "C",
        "independent_signal_count": 1,
        "russian_edition_status": "not found in Russian",
        "visible_risk_status": "no visible risk",
        "first_seen_date": "2026-10-01",
        "last_seen_date": "2026-10-01",
        "content_description": "Книга о том, как устроена повседневная жизнь небольшого города.",
        "source_evidence": [_evidence(name) for name in sources],
        **fields,
    }


def _history() -> list[dict[str, Any]]:
    return [
        _record(
            COOKBOOK_KEY,
            "Weeknight Table",
            "Ada Cook",
            [ACTIVE_SOURCE],
            content_description="Сборник: простые рецепты на каждый день для большой семьи.",
        ),
        _record(RETIRED_ONLY_KEY, "Catalogue Novelty", "Ben Retired", [RETIRED_SOURCE]),
        _record(MIXED_KEY, "Twice Reported", "Cy Mixed", [RETIRED_SOURCE, ACTIVE_SOURCE]),
        _record(PLAIN_KEY, "Plain History", "Di Plain", [ACTIVE_SOURCE]),
    ]


def _source(name: str, **fields: Any) -> dict[str, Any]:
    return {
        "name": name,
        "family": "media/curated list",
        "url": f"https://example.test/{name.lower().replace(' ', '-')}",
        **fields,
    }


def _retired(name: str = RETIRED_SOURCE, **fields: Any) -> dict[str, Any]:
    return _source(name, active=False, lifecycle_status="retired", retired_cards_hidden=True, **fields)


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _state(tmp_path: Path, history: list[dict[str, Any]] | None = None) -> Path:
    state = tmp_path / "state"
    _write(state / "scouting-candidates.json", [])
    seed_state(state, candidate_history=_history() if history is None else history)
    _write(state / "run-summary.json", {"run_date": "2026-11-10", "editor_facing_count": 0})
    return state


def _config(tmp_path: Path, *sources: dict[str, Any]) -> Path:
    path = tmp_path / "config.json"
    _write(path, {"sources": list(sources)})
    return path


def _render(state: Path, config: Path | None = None) -> set[str]:
    """Render the review and return the identity keys the editor sees."""
    with redirect_stdout(io.StringIO()):
        assert render_review(state, state, config_path=config) == 0
    with (state / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
        return {row["Candidate ID"] for row in csv.DictReader(handle)}


def _withheld(state: Path) -> dict[str, dict[str, Any]]:
    records = json.loads((state / "hidden-from-review.json").read_text(encoding="utf-8"))
    return {record["identity_key"]: record for record in records}


def _add_cookbook_rule(state: Path, *, confirmed: bool = True) -> str:
    """Add the cookbook Hard Rule and return what the command printed."""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = add_profile_entry(
            state,
            kind="hard_rule",
            scope="topic",
            subject="кулинарные книги",
            quote="Кулинарные книги нам не нужны.",
            provenance="editor conversation",
            created_by="test",
            matching_terms=["рецепты", "cookbook*"],
            confirmed_by_editor=confirmed,
        )
    assert code == (0 if confirmed else 3)
    return buffer.getvalue()


def test_a_rule_added_after_a_record_entered_history_hides_it_at_the_next_render(tmp_path: Path) -> None:
    state = _state(tmp_path)
    assert _render(state) == {COOKBOOK_KEY, RETIRED_ONLY_KEY, MIXED_KEY, PLAIN_KEY}
    assert not (state / "hidden-from-review.json").exists()

    _add_cookbook_rule(state)

    assert _render(state) == {RETIRED_ONLY_KEY, MIXED_KEY, PLAIN_KEY}
    withheld = _withheld(state)
    assert set(withheld) == {COOKBOOK_KEY}
    assert withheld[COOKBOOK_KEY]["hidden_by"] == "hard_rule"
    assert withheld[COOKBOOK_KEY]["hidden_by_rule_id"] == "taste-0001"
    assert "Кулинарные книги нам не нужны." in withheld[COOKBOOK_KEY]["reason"]
    review = (state / "weekly-review.md").read_text(encoding="utf-8")
    assert "**Скрыто из всего обзора**: правилами редактора — 1, из отключённых источников — 0." in review
    assert "Weeknight Table" not in review
    assert "Weeknight Table" not in (state / "weekly-review.html").read_text(encoding="utf-8")


def test_revoking_the_rule_returns_the_card(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _add_cookbook_rule(state)
    assert COOKBOOK_KEY not in _render(state)

    with redirect_stdout(io.StringIO()):
        assert revoke_profile_entry(state, "taste-0001", "Changed her mind.", "test", confirmed_by_editor=True) == 0

    assert COOKBOOK_KEY in _render(state)
    # The audit file stops naming a card that is back in the review.
    assert _withheld(state) == {}
    assert "Скрыто из всего обзора" not in (state / "weekly-review.md").read_text(encoding="utf-8")


def test_the_add_preview_reports_the_history_cards_the_rule_will_hide(tmp_path: Path) -> None:
    history = _history()
    history[0]["editor_status"] = "reject"
    state = _state(tmp_path, history)
    before = state_snapshot(state)

    preview = _add_cookbook_rule(state, confirmed=False)

    lines = preview.splitlines()
    assert "history_matches=1" in lines
    (match_line,) = [line for line in lines if line.startswith("history_match=")]
    match = json.loads(match_line.removeprefix("history_match="))
    assert match["identity_key"] == COOKBOOK_KEY
    assert match["title"] == "Weeknight Table"
    assert match["editor_status"] == "reject"
    assert "рецепты" in match["match"]
    # The preview changes nothing.
    assert state_snapshot(state) == before
    assert not (state / "editorial-profile.json").exists()


def test_the_set_terms_preview_reports_the_history_cards_too(tmp_path: Path) -> None:
    state = _state(tmp_path)
    seed_state(
        state,
        editorial_profile={
            "entries": [
                {
                    "id": "taste-0001",
                    "kind": "hard_rule",
                    "scope": "topic",
                    "subject": "кулинарные книги",
                    "verbatim_quote": "Кулинарные книги нам не нужны.",
                    "provenance": "editor conversation",
                    "created_at": "2026-06-01",
                    "created_by": "test",
                    "status": "active",
                }
            ]
        },
    )
    # A topic rule without terms matches nothing, so the card is still shown.
    assert COOKBOOK_KEY in _render(state)

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        assert set_profile_entry_terms(state, "taste-0001", ["рецепты"]) == 3
    assert "history_matches=1" in buffer.getvalue().splitlines()

    with redirect_stdout(io.StringIO()):
        assert set_profile_entry_terms(state, "taste-0001", ["рецепты"], confirmed_by_editor=True) == 0
    assert COOKBOOK_KEY not in _render(state)


def test_a_record_added_by_the_pw_history_backfill_is_hidden_by_an_active_rule(tmp_path: Path) -> None:
    state = _state(tmp_path, [_record(PLAIN_KEY, "Plain History", "Di Plain", [ACTIVE_SOURCE])])
    _add_cookbook_rule(state)
    row = {
        "title": "An Invented Kitchen",
        "primary_authors": ["Ada Example"],
        "publisher": "Imaginary Press",
        "price": "$28",
        "format": "hardcover",
        "page_count": 240,
        "starred": False,
        "isbn13": "9780306406157",
        "subsection": "History",
        "agent": "",
        "agency": "",
        "publication_month": "Nov.",
        "issue_date": "2026-01-05",
    }
    description = (
        "Автор собрала семейные рецепты прибрежных поселений и рассказывает, как торговля и миграция меняли "
        "местную кухню. Книга сопоставляет исторические документы с рассказами жителей и показывает, как "
        "экономические перемены влияли на повседневную жизнь и представления людей о своём регионе."
    )
    stage = tmp_path / "stage.json"
    descriptions = tmp_path / "descriptions.json"
    _write(stage, {"schema_version": 1, "year": 2026, "rows": [row], "issues": [{"issue_date": row["issue_date"]}]})
    _write(
        descriptions,
        {
            "schema_version": 1,
            "descriptions": {
                row["isbn13"]: {
                    "isbn13": row["isbn13"],
                    "title": row["title"],
                    "primary_authors": row["primary_authors"],
                    "issue_date": row["issue_date"],
                    "review_sha256": "a" * 64,
                    "content_description": description,
                }
            },
        },
    )
    config = _config(
        tmp_path,
        {
            "name": "Publishers Weekly Nonfiction Reviews",
            "contract_family": "pw-issue-pdf",
            "family": "media/curated list",
            "url": "https://www.publishersweekly.com/pw/reviews/",
        },
    )

    with redirect_stdout(io.StringIO()):
        applied = backfill_pw_history(stage, descriptions, config, state, state, apply=True)

    assert applied["status"] == "applied"
    assert applied["added"] == 1
    history = read_state(state, "candidate_history")
    added = next(record for record in history if record.get("isbn") == row["isbn13"])
    # The backfill writes the record without consulting the rules; the review does.
    assert "hidden_by_rule_id" not in added
    with (state / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
        shown = {csv_row["Candidate ID"] for csv_row in csv.DictReader(handle)}
    assert shown == {PLAIN_KEY}
    (withheld,) = _withheld(state).values()
    assert withheld["identity_key"] == added["identity_key"]
    assert withheld["hidden_by_rule_id"] == "taste-0001"


def test_a_card_reported_only_by_retired_sources_is_hidden(tmp_path: Path) -> None:
    state = _state(tmp_path)
    config = _config(tmp_path, _retired(), _source(ACTIVE_SOURCE))

    assert _render(state, config) == {COOKBOOK_KEY, MIXED_KEY, PLAIN_KEY}

    withheld = _withheld(state)
    assert set(withheld) == {RETIRED_ONLY_KEY}
    assert withheld[RETIRED_ONLY_KEY]["hidden_by"] == "retired_source"
    assert withheld[RETIRED_ONLY_KEY]["retired_sources"] == [RETIRED_SOURCE]
    assert RETIRED_SOURCE in withheld[RETIRED_ONLY_KEY]["reason"]
    review = (state / "weekly-review.md").read_text(encoding="utf-8")
    assert "**Скрыто из всего обзора**: правилами редактора — 0, из отключённых источников — 1." in review


def test_a_card_with_one_retired_and_one_active_source_stays_with_both_sources(tmp_path: Path) -> None:
    state = _state(tmp_path)
    config = _config(tmp_path, _retired(), _source(ACTIVE_SOURCE))

    assert MIXED_KEY in _render(state, config)

    with (state / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
        (mixed,) = [row for row in csv.DictReader(handle) if row["Candidate ID"] == MIXED_KEY]
    assert RETIRED_SOURCE in mixed["Source"] + mixed["Links"]
    stored = read_state(state, "candidate_history")
    (record,) = [item for item in stored if item["identity_key"] == MIXED_KEY]
    assert [evidence["source_name"] for evidence in record["source_evidence"]] == [RETIRED_SOURCE, ACTIVE_SOURCE]


def test_a_retired_only_card_returns_when_an_active_source_reports_the_book(tmp_path: Path) -> None:
    state = _state(tmp_path)
    config = _config(tmp_path, _retired(), _source(ACTIVE_SOURCE))
    assert RETIRED_ONLY_KEY not in _render(state, config)

    history = read_state(state, "candidate_history")
    for record in history:
        if record["identity_key"] == RETIRED_ONLY_KEY:
            record["source_evidence"].append(_evidence(ACTIVE_SOURCE))
    seed_state(state, candidate_history=history)

    assert RETIRED_ONLY_KEY in _render(state, config)
    assert _withheld(state) == {}


def test_restoring_the_source_returns_its_cards(tmp_path: Path) -> None:
    state = _state(tmp_path)
    assert RETIRED_ONLY_KEY not in _render(state, _config(tmp_path, _retired(), _source(ACTIVE_SOURCE)))

    # The operator switches the source back on and leaves the withdrawal mark in place.
    restored = _source(RETIRED_SOURCE, active=True, lifecycle_status="probation", retired_cards_hidden=True)

    assert RETIRED_ONLY_KEY in _render(state, _config(tmp_path, restored, _source(ACTIVE_SOURCE)))
    assert _withheld(state) == {}


def test_retirement_alone_does_not_withdraw_a_source_s_cards(tmp_path: Path) -> None:
    """A source retired for a technical reason keeps its cards in the review."""
    state = _state(tmp_path)
    retired_without_mark = _source(RETIRED_SOURCE, active=False, lifecycle_status="retired")

    shown = _render(state, _config(tmp_path, retired_without_mark, _source(ACTIVE_SOURCE)))

    assert RETIRED_ONLY_KEY in shown
    assert not (state / "hidden-from-review.json").exists()


def test_editor_decisions_survive_and_a_card_in_work_stays_after_retirement(tmp_path: Path) -> None:
    history = [
        _record("rejected|a", "Rejected", "A", [RETIRED_SOURCE], editor_status="reject", editor_note="Не наше."),
        _record("known|b", "Known", "B", [RETIRED_SOURCE], editor_status="already_known"),
        _record("deeper|c", "Deeper", "C", [RETIRED_SOURCE], editor_status="needs_deeper_analysis"),
        _record("interested|d", "Interested", "D", [RETIRED_SOURCE], editor_status="interested"),
        _record("watch|e", "Watch", "E", [RETIRED_SOURCE], editor_status="maybe_watch"),
    ]
    state = _state(tmp_path, history)
    before = state_snapshot(state)

    shown = _render(state, _config(tmp_path, _retired()))

    # Hiding is a projection: Candidate History, with every decision, is untouched.
    assert state_snapshot(state) == before
    # A book the editor is still working with is her own reason to keep seeing it.
    assert shown == {"deeper|c", "interested|d", "watch|e"}
    withheld = _withheld(state)
    assert set(withheld) == {"rejected|a", "known|b"}
    assert withheld["rejected|a"]["editor_status"] == "reject"


def test_a_hard_rule_hides_a_card_whatever_the_editor_decided_and_keeps_the_decision(tmp_path: Path) -> None:
    history = _history()
    history[0]["editor_status"] = "interested"
    history[0]["editor_note"] = "Посмотреть ещё раз."
    state = _state(tmp_path, history)
    _add_cookbook_rule(state)
    before = state_snapshot(state)

    assert COOKBOOK_KEY not in _render(state)

    assert state_snapshot(state) == before
    assert _withheld(state)[COOKBOOK_KEY]["editor_status"] == "interested"


def test_a_current_candidate_from_a_retired_source_is_hidden_before_the_next_run(tmp_path: Path) -> None:
    """The last run's candidates predate the retirement; the render still hides them."""
    record = _record(RETIRED_ONLY_KEY, "Catalogue Novelty", "Ben Retired", [RETIRED_SOURCE], update_delta="new")
    state = _state(tmp_path, [record])
    _write(state / "scouting-candidates.json", [record])

    assert _render(state, _config(tmp_path, _retired())) == set()
    assert set(_withheld(state)) == {RETIRED_ONLY_KEY}


def test_a_render_without_a_configuration_withdraws_no_source_but_applies_the_rules(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _add_cookbook_rule(state)

    assert _render(state) == {RETIRED_ONLY_KEY, MIXED_KEY, PLAIN_KEY}


def test_the_doctor_rerender_applies_the_configuration_s_retired_sources(tmp_path: Path) -> None:
    from autoscout.doctor_repairs import _default_review_renderer

    state = _state(tmp_path)
    config = _config(tmp_path, _retired(), _source(ACTIVE_SOURCE))

    code, _stdout = _default_review_renderer(state, state, "2026-11-10", config_path=config)

    assert code == 0
    assert set(_withheld(state)) == {RETIRED_ONLY_KEY}


def test_a_configuration_that_cannot_be_read_fails_the_render(tmp_path: Path) -> None:
    """A wrong path must not read as "no source is retired" and return the cards."""
    state = _state(tmp_path)
    buffer = io.StringIO()

    with redirect_stdout(buffer):
        code = render_review(state, state, config_path=tmp_path / "missing.json")

    assert code == 1
    assert "error=review_config_invalid" in buffer.getvalue()
    assert not (state / "editor-facing.csv").exists()


def test_a_current_candidate_a_new_rule_matches_is_hidden_before_the_next_run(tmp_path: Path) -> None:
    record = _history()[0] | {"update_delta": "new"}
    state = _state(tmp_path, [record])
    _write(state / "scouting-candidates.json", [record])
    _add_cookbook_rule(state)

    assert _render(state) == set()
    assert _withheld(state)[COOKBOOK_KEY]["hidden_by_rule_id"] == "taste-0001"


def _fixture_source(name: str, *books: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return _source(
        name,
        access_status="open",
        extraction_method="fixture",
        check_frequency="weekly",
        language="en",
        fixture_candidates=list(books),
        **fields,
    )


def test_a_book_an_active_source_reported_in_an_earlier_run_is_not_retired_only(tmp_path: Path) -> None:
    """A weekly snapshot replaces a record's evidence, so the earlier sources are remembered."""
    book = {
        "title": "Reported Twice",
        "primary_authors": ["Eve Earlier"],
        "source_url": "https://example.test/reported-twice",
        "signal_strength": "winner",
        "evidence_note": "Listed by the source.",
        "candidate_category": "adult nonfiction",
        "russian_edition_status": "not found in Russian",
        "visible_risk_status": "no visible risk",
    }
    key = "reported twice|eve earlier"
    config = tmp_path / "weekly.json"
    output = tmp_path / "run"

    def run(run_date: str, *sources: dict[str, Any]) -> dict[str, Any]:
        _write(config, {"run_date": run_date, "sources": list(sources)})
        with redirect_stdout(io.StringIO()):
            run_mvp(config, output, output)
        (record,) = read_state(output, "candidate_history")
        return record

    first = run("2026-10-01", _fixture_source(ACTIVE_SOURCE, book), _fixture_source(RETIRED_SOURCE))
    assert [evidence["source_name"] for evidence in first["source_evidence"]] == [ACTIVE_SOURCE]

    # The next week only the catalogue reports the book, with a stronger signal.
    second = run(
        "2026-10-08",
        _fixture_source(ACTIVE_SOURCE),
        _fixture_source(RETIRED_SOURCE, book | {"signal_strength": "shortlist"}),
    )
    assert [evidence["source_name"] for evidence in second["source_evidence"]] == [RETIRED_SOURCE]
    assert second["earlier_source_names"] == [ACTIVE_SOURCE]

    retired = _retired(access_status="open", extraction_method="fixture", check_frequency="weekly", language="en")
    _write(config, {"run_date": "2026-10-08", "sources": [_fixture_source(ACTIVE_SOURCE), retired]})

    assert key in _render(output, config)

    # The same book with no earlier source is withdrawn.
    history = read_state(output, "candidate_history")
    del history[0]["earlier_source_names"]
    seed_state(output, candidate_history=history)
    assert key not in _render(output, config)


def test_the_weekly_review_stage_renders_with_the_run_s_configuration(tmp_path: Path) -> None:
    from autoscout.weekly_workflow import _run_review_stage

    state = _state(tmp_path)
    config = _config(tmp_path, _retired(), _source(ACTIVE_SOURCE))

    with redirect_stdout(io.StringIO()):
        assert _run_review_stage(state, state, "2026-11-10", config)["returncode"] == 0

    assert set(_withheld(state)) == {RETIRED_ONLY_KEY}


def test_a_current_candidate_counts_the_sources_its_history_record_gained(tmp_path: Path) -> None:
    """An identity merge or the PW backfill adds evidence to the history record only."""
    current = _record(RETIRED_ONLY_KEY, "Catalogue Novelty", "Ben Retired", [RETIRED_SOURCE], update_delta="new")
    stored = _record(RETIRED_ONLY_KEY, "Catalogue Novelty", "Ben Retired", [RETIRED_SOURCE, ACTIVE_SOURCE])
    state = _state(tmp_path, [stored])
    _write(state / "scouting-candidates.json", [current])

    assert _render(state, _config(tmp_path, _retired(), _source(ACTIVE_SOURCE))) == {RETIRED_ONLY_KEY}
