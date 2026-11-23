"""A standalone PW backfill keeps the weekly report and existing history intact."""

import json
from pathlib import Path

from state_store_support import read_state
from state_store_support import seed_state

from autoscout.content_description import record_has_usable_description
from autoscout.pw_history_backfill import backfill_pw_history
from autoscout.review_rendering import assemble_cumulative_review

ROW = {
    "title": "An Invented Coast",
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
DESCRIPTION = (
    "Книга исследует, как прибрежные поселения менялись под воздействием торговли, миграции и решений местных властей. "
    "Автор сопоставляет исторические документы с рассказами жителей и прослеживает, как экономические перемены влияли "
    "на повседневную жизнь, городское пространство и представления людей о своём регионе."
)
ENGLISH_DESCRIPTION = (
    "This history traces how trade, housing, and local policy reshaped a coastal town. "
    "Drawing on municipal archives and residents' interviews, it connects economic decisions with changes in daily "
    "life and the community's relationship with the shoreline."
)


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_preview_then_apply_adds_only_new_history_with_description(tmp_path: Path, monkeypatch) -> None:
    stage = tmp_path / "stage.json"
    descriptions = tmp_path / "descriptions.json"
    config = tmp_path / "config.json"
    state = tmp_path / "state"
    output = tmp_path / "output"
    _write(stage, {"schema_version": 1, "year": 2026, "rows": [ROW], "issues": [{"issue_date": ROW["issue_date"]}]})
    _write(
        config,
        {
            "sources": [
                {
                    "name": "Publishers Weekly Nonfiction Reviews",
                    "contract_family": "pw-issue-pdf",
                    "family": "media/curated list",
                    "url": "https://www.publishersweekly.com/pw/reviews/",
                    "pw_excluded_subsections": ["Food & Cooking"],
                }
            ]
        },
    )
    earlier = {"identity_key": "earlier book|sam author", "title": "Earlier Book", "editor_status": "reject"}
    seed_state(state, candidate_history=[earlier])
    called = []
    monkeypatch.setattr("autoscout.pw_history_backfill.render_review", lambda *args, **kwargs: called.append(args) or 0)

    preview = backfill_pw_history(stage, descriptions, config, state, output)
    assert preview["new_candidates"] == 1
    assert preview["new_candidates_without_descriptions"] == 1
    assert read_state(state, "candidate_history") == [earlier]

    blocked = backfill_pw_history(stage, descriptions, config, state, output, apply=True)
    assert blocked["reason_code"] == "pw_nothing_ready"
    assert not called
    _write(
        descriptions,
        {
            "schema_version": 1,
            "descriptions": {
                ROW["isbn13"]: {
                    "isbn13": ROW["isbn13"],
                    "title": ROW["title"],
                    "primary_authors": ROW["primary_authors"],
                    "issue_date": ROW["issue_date"],
                    "review_sha256": "a" * 64,
                    "content_description": DESCRIPTION,
                }
            },
        },
    )
    applied = backfill_pw_history(stage, descriptions, config, state, output, apply=True)
    assert applied["status"] == "applied"
    assert applied["added"] == 1
    history = read_state(state, "candidate_history")
    assert earlier in history
    added = next(record for record in history if record.get("isbn") == ROW["isbn13"])
    assert added["content_description"] == DESCRIPTION
    assert added["first_seen_date"] == ROW["issue_date"]
    assert added["last_seen_date"] == ROW["issue_date"]
    # The review is rendered with the backfill's configuration, whose retired sources apply (#1025).
    assert called == [(output, state, None, config)]


def test_english_backfill_adds_history_and_editor_description(tmp_path: Path, monkeypatch) -> None:
    stage = tmp_path / "stage.json"
    descriptions = tmp_path / "english-descriptions.json"
    config = tmp_path / "config.json"
    state = tmp_path / "state"
    output = tmp_path / "output"
    _write(stage, {"schema_version": 1, "year": 2026, "rows": [ROW], "issues": [{"issue_date": ROW["issue_date"]}]})
    _write(
        config,
        {
            "sources": [
                {
                    "name": "Publishers Weekly Nonfiction Reviews",
                    "contract_family": "pw-issue-pdf",
                    "family": "media/curated list",
                    "url": "https://www.publishersweekly.com/pw/reviews/",
                }
            ]
        },
    )
    seed_state(state, candidate_history=[])
    _write(
        descriptions,
        {
            "schema_version": 1,
            "year": 2026,
            "language": "en",
            "descriptions": {
                ROW["isbn13"]: {
                    "isbn13": ROW["isbn13"],
                    "title": ROW["title"],
                    "primary_authors": ROW["primary_authors"],
                    "issue_date": ROW["issue_date"],
                    "review_sha256": "a" * 64,
                    "english_description": ENGLISH_DESCRIPTION,
                }
            },
        },
    )
    monkeypatch.setattr("autoscout.pw_history_backfill.render_review", lambda *args, **kwargs: 0)

    preview = backfill_pw_history(stage, descriptions, config, state, output)
    assert preview["new_candidates_ready"] == 1
    assert read_state(state, "candidate_history") == []

    applied = backfill_pw_history(stage, descriptions, config, state, output, apply=True)
    assert applied["status"] == "applied"
    history = read_state(state, "candidate_history")
    assert history[0]["content_description"] == ENGLISH_DESCRIPTION
    assert history[0]["description_provenance"]["language"] == "en"
    assert history[0]["description_provenance"]["source"] == "pw-local-history-backfill"
    assert record_has_usable_description(history[0])
    rows, _summary = assemble_cumulative_review([], history, [])
    assert rows[0]["editor_row"]["Description"] == ENGLISH_DESCRIPTION


def test_english_backfill_updates_existing_identity_without_isbn(tmp_path: Path, monkeypatch) -> None:
    stage = tmp_path / "stage.json"
    descriptions = tmp_path / "english-descriptions.json"
    config = tmp_path / "config.json"
    state = tmp_path / "state"
    existing = {
        "identity_key": "an invented coast|ada example",
        "title": ROW["title"],
        "primary_authors": ROW["primary_authors"],
        "editor_status": "reject",
    }
    _write(stage, {"schema_version": 1, "year": 2026, "rows": [ROW], "issues": [{"issue_date": ROW["issue_date"]}]})
    _write(
        config,
        {
            "sources": [
                {
                    "name": "Publishers Weekly Nonfiction Reviews",
                    "contract_family": "pw-issue-pdf",
                    "family": "media/curated list",
                    "url": "https://www.publishersweekly.com/pw/reviews/",
                }
            ]
        },
    )
    seed_state(state, candidate_history=[existing])
    _write(
        descriptions,
        {
            "schema_version": 1,
            "year": 2026,
            "language": "en",
            "descriptions": {
                ROW["isbn13"]: {
                    "isbn13": ROW["isbn13"],
                    "title": ROW["title"],
                    "primary_authors": ROW["primary_authors"],
                    "issue_date": ROW["issue_date"],
                    "review_sha256": "a" * 64,
                    "english_description": ENGLISH_DESCRIPTION,
                }
            },
        },
    )
    monkeypatch.setattr("autoscout.pw_history_backfill.render_review", lambda *args, **kwargs: 0)

    preview = backfill_pw_history(stage, descriptions, config, state, tmp_path / "output")
    assert preview["existing_descriptions_ready"] == 1
    applied = backfill_pw_history(stage, descriptions, config, state, tmp_path / "output", apply=True)
    assert applied["existing_descriptions_updated"] == 1
    history = read_state(state, "candidate_history")
    assert len(history) == 1
    assert history[0]["editor_status"] == "reject"
    assert history[0]["content_description"] == ENGLISH_DESCRIPTION
    assert record_has_usable_description(history[0])


def test_existing_isbn_with_ocr_title_is_reported_without_merging(tmp_path: Path) -> None:
    stage = tmp_path / "stage.json"
    descriptions = tmp_path / "descriptions.json"
    config = tmp_path / "config.json"
    state = tmp_path / "state"
    _write(stage, {"schema_version": 1, "year": 2026, "rows": [ROW], "issues": [{"issue_date": ROW["issue_date"]}]})
    _write(
        config,
        {
            "sources": [
                {
                    "name": "Publishers Weekly Nonfiction Reviews",
                    "contract_family": "pw-issue-pdf",
                    "family": "media/curated list",
                    "url": "https://www.publishersweekly.com/pw/reviews/",
                }
            ]
        },
    )
    existing = {
        "identity_key": "an invented coast|ada example",
        "title": "An Invented Coast",
        "primary_authors": ["Ada Example"],
        "isbn": ROW["isbn13"],
        "isbn_discovery_only": True,
        "editor_status": "reject",
    }
    seed_state(state, candidate_history=[existing])
    changed_row = {**ROW, "title": "An Inv ented Coast", "primary_authors": ["A da Example"]}
    _write(
        stage, {"schema_version": 1, "year": 2026, "rows": [changed_row], "issues": [{"issue_date": ROW["issue_date"]}]}
    )
    preview = backfill_pw_history(stage, descriptions, config, state, tmp_path / "output")
    assert preview["new_candidates"] == 0
    assert preview["deferred_isbn_identity_conflicts"] == 1
    assert preview["isbn_identity_conflict_details"][0]["isbn"] == ROW["isbn13"]
    assert read_state(state, "candidate_history") == [existing]


def test_apply_can_fill_existing_description_before_other_books_are_ready(tmp_path: Path, monkeypatch) -> None:
    stage = tmp_path / "stage.json"
    descriptions = tmp_path / "descriptions.json"
    config = tmp_path / "config.json"
    state = tmp_path / "state"
    new_row = {
        **ROW,
        "title": "Another Historical Book",
        "primary_authors": ["Ben Example"],
        "isbn13": "9781402894626",
    }
    _write(
        stage,
        {"schema_version": 1, "year": 2026, "rows": [ROW, new_row], "issues": [{"issue_date": ROW["issue_date"]}]},
    )
    _write(
        config,
        {
            "sources": [
                {
                    "name": "Publishers Weekly Nonfiction Reviews",
                    "contract_family": "pw-issue-pdf",
                    "family": "media/curated list",
                    "url": "https://www.publishersweekly.com/pw/reviews/",
                }
            ]
        },
    )
    existing = {
        "identity_key": "an invented coast|ada example",
        "title": ROW["title"],
        "primary_authors": ROW["primary_authors"],
        "isbn": ROW["isbn13"],
        "editor_status": "reject",
        "first_seen_date": "2025-12-01",
    }
    seed_state(state, candidate_history=[existing])
    _write(
        descriptions,
        {
            "schema_version": 1,
            "descriptions": {
                ROW["isbn13"]: {
                    "isbn13": ROW["isbn13"],
                    "title": ROW["title"],
                    "primary_authors": ROW["primary_authors"],
                    "issue_date": ROW["issue_date"],
                    "review_sha256": "a" * 64,
                    "content_description": DESCRIPTION,
                }
            },
        },
    )
    monkeypatch.setattr("autoscout.pw_history_backfill.render_review", lambda *args, **kwargs: 0)
    applied = backfill_pw_history(stage, descriptions, config, state, tmp_path / "output", apply=True)
    assert applied["added"] == 0
    assert applied["existing_descriptions_updated"] == 1
    assert applied["existing_evidence_updated"] == 1
    assert applied["new_candidates_without_descriptions"] == 1
    history = read_state(state, "candidate_history")
    assert len(history) == 1
    assert history[0]["editor_status"] == "reject"
    assert history[0]["first_seen_date"] == "2025-12-01"
    assert history[0]["content_description"] == DESCRIPTION
    assert history[0]["description_provenance"]["provenance"] == "grounded"
    assert history[0]["source_evidence"][0]["source_name"] == "Publishers Weekly Nonfiction Reviews"
