"""Hard Rules judge the final enriched candidates.

The pipeline applies Hard Rules before description enrichment. A topic Hard
Rule with Russian matching terms reads only Russian fields, which in practice
means the Content Description, so a book whose matching term arrives only
with its description stayed visible against an explicit editor rule. Every
case drives the full MVP pipeline with a real state directory, publishes a
description the way enrichment does, and runs the post-enrichment refresh.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_snapshot

from autoscout.cli.weekly import run_mvp
from autoscout.editorial_taste_profile import save_taste_profile
from autoscout.pipeline import refresh_post_enrichment_artifacts
from autoscout.review_rendering import render_review

RUN_DATE = "2026-06-15"
LATE_TITLE = "Fields of Grain"
LATE_DESCRIPTION = "История коллективизации на Украине и голода начала 1930-х годов."


def _book(title: str, **fields: Any) -> dict[str, Any]:
    slug = title.lower().replace(" ", "-")
    return {
        "title": title,
        "primary_authors": [f"{title.split()[0]} Example"],
        "source_url": f"https://example.test/books/{slug}",
        "signal_strength": "major media list",
        "candidate_category": "history",
        "evidence_note": "Included in a curated list of new books.",
        **fields,
    }


def _rule(**fields: Any) -> dict[str, Any]:
    return {
        "id": "taste-0001",
        "kind": "hard_rule",
        "scope": "topic",
        "subject": "Украина",
        "matching_terms": ["Украине"],
        "verbatim_quote": "Книги об Украине мы не берём.",
        "provenance": "hard rule fixture",
        "created_at": "2026-06-01",
        "created_by": "test",
        "status": "active",
        **fields,
    }


LATE = _book(LATE_TITLE)
UNRELATED = _book("The Patient Harbour")


def _run(tmp_path: Path, *entries: dict[str, Any], shared_dir: bool = False) -> tuple[Path, Path, Path]:
    config_path = tmp_path / "sources.json"
    source = {
        "name": "Fixture Agent List",
        "family": "media/curated list",
        "url": "https://example.test/agent-list",
        "access_status": "open",
        "extraction_method": "fixture",
        "check_frequency": "weekly",
        "language": "en",
        "fixture_candidates": [LATE, UNRELATED],
    }
    config_path.write_text(json.dumps({"run_date": RUN_DATE, "sources": [source]}), encoding="utf-8")
    output_dir = tmp_path / "runs"
    state_dir = output_dir if shared_dir else tmp_path / "state"
    save_taste_profile(state_dir, {"entries": list(entries)})
    run_mvp(config_path, output_dir, state_dir)
    return config_path, output_dir, state_dir


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _titles(path: Path) -> set[str]:
    return {record["title"] for record in _read(path)}


def _publish_description(output_dir: Path, state_dir: Path) -> None:
    """Write the Content Description to every candidate copy, as enrichment does."""

    def describe(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for record in records:
            if record["title"] == LATE_TITLE:
                record["content_description"] = LATE_DESCRIPTION
        return records

    # A run whose output is its sharded state directory keeps no separate history copy (#1073).
    for path in (output_dir / "scouting-candidates.json", output_dir / "candidate-history.json"):
        if not path.is_file():
            continue
        records = describe(_read(path))
        path.write_text(json.dumps(records, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for collection in ("candidate_history", "latest_scouting_candidates"):
        seed_state(state_dir, **{collection: describe(read_state(state_dir, collection))})


def _refresh(config_path: Path, output_dir: Path, state_dir: Path) -> None:
    assert refresh_post_enrichment_artifacts(config_path, output_dir, state_dir)["status"] == "ok"


def _effect(output_dir: Path) -> dict[str, Any]:
    (effect,) = [
        effect
        for effect in _read(output_dir / "run-summary.json")["editorial_taste_profile_effects"]
        if effect["id"] == "taste-0001"
    ]
    return effect


@pytest.mark.parametrize("shared_dir", [False, True], ids=["separate-state", "shared-state"])
def test_a_rule_matching_only_the_late_description_hides_the_candidate(tmp_path: Path, shared_dir: bool) -> None:
    config_path, output_dir, state_dir = _run(tmp_path, _rule(), shared_dir=shared_dir)
    assert _titles(output_dir / "scouting-candidates.json") == {LATE_TITLE, "The Patient Harbour"}
    assert _read(output_dir / "hidden-by-rule.json") == []

    _publish_description(output_dir, state_dir)
    _refresh(config_path, output_dir, state_dir)

    assert _titles(output_dir / "scouting-candidates.json") == {"The Patient Harbour"}
    assert {record["title"] for record in read_state(state_dir, "latest_scouting_candidates")} == {
        "The Patient Harbour"
    }
    (hidden,) = _read(output_dir / "hidden-by-rule.json")
    assert hidden["title"] == LATE_TITLE
    assert hidden["hidden_by_rule_id"] == "taste-0001"
    assert "Книги об Украине мы не берём." in hidden["reason"]
    # Hidden, not forgotten: Candidate History keeps the record and the rule.
    history = {record["title"]: record for record in read_state(state_dir, "candidate_history")}
    assert history[LATE_TITLE]["hidden_by_rule_id"] == "taste-0001"
    assert history[LATE_TITLE]["hidden_by_rule_reason"] == hidden["reason"]
    assert history[LATE_TITLE]["last_hidden_by_rule_date"] == RUN_DATE
    assert "hidden_by_rule_id" not in history["The Patient Harbour"]
    summary = _read(output_dir / "run-summary.json")
    assert summary["hidden_by_rule_count"] == 1
    effect = _effect(output_dir)
    assert (effect["match_count"], effect["hidden_count"], effect["status"]) == (1, 1, "matched")


def test_the_review_counts_the_late_hidden_candidate_and_names_its_rule(tmp_path: Path) -> None:
    config_path, output_dir, state_dir = _run(tmp_path, _rule(), shared_dir=True)
    _publish_description(output_dir, state_dir)
    _refresh(config_path, output_dir, state_dir)

    assert render_review(output_dir, state_dir) == 0

    review = (output_dir / "weekly-review.md").read_text(encoding="utf-8")
    assert "скрыто правилами — 1" in review
    # Product rule changed by #1025 (authorized by the operator at the editor's
    # request): the hidden card is no longer a previously seen row with a rule
    # note; while the rule is active it is out of the review, and
    # hidden-from-review.json names the rule.
    assert not [line for line in review.splitlines() if f"| {LATE_TITLE} |" in line]
    assert "**Скрыто из всего обзора**: правилами редактора — 1, из отключённых источников — 0." in review
    (withheld,) = _read(output_dir / "hidden-from-review.json")
    assert (withheld["title"], withheld["hidden_by_rule_id"]) == (LATE_TITLE, "taste-0001")


def test_a_repeated_refresh_changes_nothing(tmp_path: Path) -> None:
    config_path, output_dir, state_dir = _run(tmp_path, _rule(), shared_dir=True)
    _publish_description(output_dir, state_dir)
    _refresh(config_path, output_dir, state_dir)
    names = ("scouting-candidates.json", "hidden-by-rule.json", "run-summary.json")
    before = {name: (output_dir / name).read_bytes() for name in names}
    state_before = state_snapshot(state_dir)

    _refresh(config_path, output_dir, state_dir)

    assert {name: (output_dir / name).read_bytes() for name in names} == before
    assert state_snapshot(state_dir) == state_before


def test_a_revoked_rule_hides_nothing(tmp_path: Path) -> None:
    config_path, output_dir, state_dir = _run(
        tmp_path,
        _rule(status="revoked", revoked_at="2026-06-10", revoked_by="test", revocation_reason="editor changed mind"),
        shared_dir=True,
    )
    _publish_description(output_dir, state_dir)
    _refresh(config_path, output_dir, state_dir)

    assert _titles(output_dir / "scouting-candidates.json") == {LATE_TITLE, "The Patient Harbour"}
    assert _read(output_dir / "hidden-by-rule.json") == []
    assert _read(output_dir / "run-summary.json")["hidden_by_rule_count"] == 0


def test_without_a_matching_description_nothing_is_hidden(tmp_path: Path) -> None:
    config_path, output_dir, state_dir = _run(tmp_path, _rule(), shared_dir=True)
    before = state_snapshot(state_dir)

    _refresh(config_path, output_dir, state_dir)

    assert _titles(output_dir / "scouting-candidates.json") == {LATE_TITLE, "The Patient Harbour"}
    assert _read(output_dir / "hidden-by-rule.json") == []
    assert state_snapshot(state_dir) == before
