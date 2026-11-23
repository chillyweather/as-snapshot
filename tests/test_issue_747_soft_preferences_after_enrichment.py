"""Soft Preferences judge the final enriched candidates (issue #747).

The pipeline applies Soft Preferences before description enrichment, so a
book whose matching term arrives only with its Content Description used to
miss the nudge. Every case drives the full MVP pipeline with a real state
directory, publishes a description the way enrichment does, and runs the
post-enrichment refresh. Assertions stay on what the editor and the
operator can observe: the Scouting Candidate, Candidate History, the latest
snapshot, and the run summary.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_snapshot

from autoscout.cli.weekly import run_mvp
from autoscout.editorial_taste_profile import _candidate_matches_soft_preference
from autoscout.editorial_taste_profile import active_soft_preferences
from autoscout.editorial_taste_profile import load_taste_profile
from autoscout.editorial_taste_profile import save_taste_profile
from autoscout.pipeline import refresh_post_enrichment_artifacts
from autoscout.review_rendering import render_review
from autoscout.scouting_candidates import ScoutingCandidate

RUN_DATE = "2026-06-15"
REFRESHED_ARTIFACTS = ("scouting-candidates.json", "candidate-history.json", "run-summary.json")


def _book(title: str, **fields: Any) -> dict[str, Any]:
    slug = title.lower().replace(" ", "-")
    return {
        "title": title,
        "primary_authors": [f"{title.split()[0]} Example"],
        "source_url": f"https://example.test/books/{slug}",
        "signal_strength": "major media list",
        "candidate_category": "cooking",
        "evidence_note": "Included in a curated list of new books.",
        **fields,
    }


def _entry(ident: str, *, direction: str = "negative", **fields: Any) -> dict[str, Any]:
    return {
        "id": ident,
        "kind": "soft_preference",
        "scope": "topic",
        "subject": "кулинарные книги",
        "matching_terms": ["рецептов"],
        "direction": direction,
        "verbatim_quote": "Сборник семейных рецептов; [editor note withheld].",
        "provenance": "issue 747 fixture",
        "created_at": "2026-06-01",
        "created_by": "test",
        "status": "active",
        **fields,
    }


def _run(
    tmp_path: Path, books: list[dict[str, Any]], *entries: dict[str, Any], shared_dir: bool = False
) -> tuple[Path, Path, Path]:
    config_path = tmp_path / "sources.json"
    source = {
        "name": "Fixture Agent List",
        "family": "media/curated list",
        "url": "https://example.test/agent-list",
        "access_status": "open",
        "extraction_method": "fixture",
        "check_frequency": "weekly",
        "language": "en",
        "fixture_candidates": books,
    }
    config_path.write_text(json.dumps({"run_date": RUN_DATE, "sources": [source]}), encoding="utf-8")
    output_dir = tmp_path / "runs"
    state_dir = output_dir if shared_dir else tmp_path / "state"
    save_taste_profile(state_dir, {"entries": list(entries)})
    run_mvp(config_path, output_dir, state_dir)
    return config_path, output_dir, state_dir


@dataclass(frozen=True)
class _CandidateCopy:
    """One persisted copy of the candidates: the run's current snapshot, Candidate History or the latest snapshot."""

    name: str
    records: Callable[[], list[dict[str, Any]]]
    write: Callable[[list[dict[str, Any]]], None]
    fingerprint: Callable[[], bytes | None]


def _candidate_copies(output_dir: Path, state_dir: Path) -> tuple[_CandidateCopy, _CandidateCopy, _CandidateCopy]:
    current = output_dir / "scouting-candidates.json"

    def state_copy(collection: str) -> _CandidateCopy:
        return _CandidateCopy(
            name=collection,
            records=lambda: read_state(state_dir, collection),
            write=lambda records: seed_state(state_dir, **{collection: records}),
            fingerprint=lambda: state_snapshot(state_dir)[collection],
        )

    return (
        _CandidateCopy(
            name=current.name,
            records=lambda: json.loads(current.read_text(encoding="utf-8")),
            write=lambda records: current.write_text(
                json.dumps(records, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            ),
            fingerprint=current.read_bytes,
        ),
        state_copy("candidate_history"),
        state_copy("latest_scouting_candidates"),
    )


def _publish_description(output_dir: Path, state_dir: Path, title: str, description: str) -> None:
    """Write a Content Description to every candidate copy, as enrichment does."""
    for copy in _candidate_copies(output_dir, state_dir):
        records = copy.records()
        for record in records:
            if record["title"] == title:
                record["content_description"] = description
        copy.write(records)


def _by_title(source: Path | _CandidateCopy) -> dict[str, dict[str, Any]]:
    records = source.records() if isinstance(source, _CandidateCopy) else json.loads(source.read_text(encoding="utf-8"))
    return {record["title"]: record for record in records}


def _rows_by_title(output_dir: Path) -> dict[str, dict[str, str]]:
    with (output_dir / "editor-facing.csv").open(encoding="utf-8", newline="") as handle:
        return {row["Title"]: row for row in csv.DictReader(handle)}


def _effects(output_dir: Path) -> dict[str, dict[str, Any]]:
    summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
    return {effect["id"]: effect for effect in summary["editorial_taste_profile_effects"]}


EARLY = _book("Family Table", short_description="Сборник семейных рецептов на каждый день.")
LATE = _book("Anytime Snacks")
UNRELATED = _book("The Patient Harbour", candidate_category="history")
LATE_DESCRIPTION = "Книга рецептов для быстрых перекусов и ужинов."


def test_a_term_found_only_in_the_enriched_description_receives_the_nudge_and_note(tmp_path: Path) -> None:
    config_path, output_dir, state_dir = _run(tmp_path, [EARLY, LATE, UNRELATED], _entry("taste-0013"))
    before = _by_title(output_dir / "scouting-candidates.json")
    assert before["Family Table"]["priority_label"] == "D"
    assert before["Anytime Snacks"]["priority_label"] == "C"
    assert not before["Anytime Snacks"].get("editorial_profile_notes")
    assert _effects(output_dir)["taste-0013"]["match_count"] == 1

    _publish_description(output_dir, state_dir, "Anytime Snacks", LATE_DESCRIPTION)
    assert refresh_post_enrichment_artifacts(config_path, output_dir, state_dir)["status"] == "ok"

    for copy in _candidate_copies(output_dir, state_dir):
        late = _by_title(copy)["Anytime Snacks"]
        assert late["priority_label"] == "D", copy.name
        assert late["editorial_profile_notes"] == (
            "[editor note withheld] "
            "“Сборник семейных рецептов; [editor note withheld].”"
        ), copy.name
        assert late["content_description"] == LATE_DESCRIPTION, copy.name
    after = _by_title(output_dir / "scouting-candidates.json")
    assert after["The Patient Harbour"] == before["The Patient Harbour"]
    assert _effects(output_dir)["taste-0013"] == {
        "id": "taste-0013",
        "kind": "soft_preference",
        "scope": "topic",
        "subject": "кулинарные книги",
        "matching_terms": ["рецептов"],
        "can_match": True,
        "match_count": 2,
        "hidden_count": 0,
        "priority_steps_caused": 2,
        "notes_count": 2,
        "status": "matched",
    }


def test_preview_and_applied_match_counts_agree_on_the_final_candidates(tmp_path: Path) -> None:
    config_path, output_dir, state_dir = _run(tmp_path, [EARLY, LATE, UNRELATED], _entry("taste-0013"))
    _publish_description(output_dir, state_dir, "Anytime Snacks", LATE_DESCRIPTION)
    refresh_post_enrichment_artifacts(config_path, output_dir, state_dir)

    # The profile preview matches against the latest snapshot.
    (preference,) = active_soft_preferences(load_taste_profile(state_dir))
    latest = read_state(state_dir, "latest_scouting_candidates")
    preview_matches = [
        record["title"]
        for record in latest
        if _candidate_matches_soft_preference(ScoutingCandidate(record), preference)
    ]

    assert sorted(preview_matches) == ["Anytime Snacks", "Family Table"]
    assert _effects(output_dir)["taste-0013"]["match_count"] == len(preview_matches)


def test_an_earlier_nudge_is_not_applied_a_second_time(tmp_path: Path) -> None:
    config_path, output_dir, state_dir = _run(tmp_path, [EARLY, LATE], _entry("taste-0020", direction="positive"))
    assert _by_title(output_dir / "scouting-candidates.json")["Family Table"]["priority_label"] == "B"

    _publish_description(output_dir, state_dir, "Anytime Snacks", LATE_DESCRIPTION)
    refresh_post_enrichment_artifacts(config_path, output_dir, state_dir)

    candidates = _by_title(output_dir / "scouting-candidates.json")
    assert candidates["Family Table"]["priority_label"] == "B"
    assert candidates["Family Table"]["editorial_profile_notes"].count("Soft preference taste-0020") == 1
    assert candidates["Anytime Snacks"]["priority_label"] == "B"
    effect = _effects(output_dir)["taste-0020"]
    assert (effect["match_count"], effect["priority_steps_caused"], effect["notes_count"]) == (2, 2, 2)


def test_the_refresh_changes_nothing_when_enrichment_changed_nothing(tmp_path: Path) -> None:
    config_path, output_dir, state_dir = _run(tmp_path, [EARLY, LATE, UNRELATED], _entry("taste-0013"))
    candidate_copies = _candidate_copies(output_dir, state_dir)
    before = [copy.fingerprint() for copy in candidate_copies]
    effects_before = _effects(output_dir)

    refresh_post_enrichment_artifacts(config_path, output_dir, state_dir)

    assert [copy.fingerprint() for copy in candidate_copies] == before
    assert _effects(output_dir) == effects_before


def test_a_second_refresh_is_byte_identical(tmp_path: Path) -> None:
    config_path, output_dir, state_dir = _run(tmp_path, [EARLY, LATE, UNRELATED], _entry("taste-0013"))
    _publish_description(output_dir, state_dir, "Anytime Snacks", LATE_DESCRIPTION)
    refresh_post_enrichment_artifacts(config_path, output_dir, state_dir)
    paths = [output_dir / name for name in REFRESHED_ARTIFACTS]
    state_copies = _candidate_copies(output_dir, state_dir)[1:]
    first = [path.read_bytes() for path in paths] + [copy.fingerprint() for copy in state_copies]

    refresh_post_enrichment_artifacts(config_path, output_dir, state_dir)

    assert [path.read_bytes() for path in paths] + [copy.fingerprint() for copy in state_copies] == first


def test_a_legacy_topic_preference_without_terms_stays_inert_after_enrichment(tmp_path: Path) -> None:
    legacy = _entry("taste-0003", subject="рецептов")
    del legacy["matching_terms"]
    config_path, output_dir, state_dir = _run(tmp_path, [LATE], legacy)

    _publish_description(output_dir, state_dir, "Anytime Snacks", LATE_DESCRIPTION)
    refresh_post_enrichment_artifacts(config_path, output_dir, state_dir)

    late = _by_title(output_dir / "scouting-candidates.json")["Anytime Snacks"]
    assert late["priority_label"] == "C"
    assert not late.get("editorial_profile_notes")
    effect = _effects(output_dir)["taste-0003"]
    assert (effect["status"], effect["match_count"]) == ("cannot_match", 0)


def test_a_hard_rule_keeps_the_match_count_from_before_hiding(tmp_path: Path) -> None:
    hard_rule = _entry("taste-0001", kind="hard_rule", scope="author", subject="Family Example")
    del hard_rule["direction"], hard_rule["matching_terms"]
    config_path, output_dir, state_dir = _run(tmp_path, [EARLY, LATE], hard_rule, _entry("taste-0013"))
    recorded = _effects(output_dir)["taste-0001"]
    assert (recorded["match_count"], recorded["hidden_count"]) == (1, 1)

    _publish_description(output_dir, state_dir, "Anytime Snacks", LATE_DESCRIPTION)
    refresh_post_enrichment_artifacts(config_path, output_dir, state_dir)

    assert _effects(output_dir)["taste-0001"] == recorded
    assert _effects(output_dir)["taste-0013"]["match_count"] == 1


def test_the_weekly_launcher_layout_with_one_directory_for_output_and_state(tmp_path: Path) -> None:
    config_path, output_dir, state_dir = _run(tmp_path, [EARLY, LATE], _entry("taste-0013"), shared_dir=True)
    assert output_dir == state_dir

    _publish_description(output_dir, state_dir, "Anytime Snacks", LATE_DESCRIPTION)
    assert refresh_post_enrichment_artifacts(config_path, output_dir, state_dir)["status"] == "ok"

    for copy in _candidate_copies(output_dir, state_dir):
        assert _by_title(copy)["Anytime Snacks"]["priority_label"] == "D", copy.name


def test_the_review_states_the_notes_of_the_final_candidates(tmp_path: Path) -> None:
    positive = _entry("taste-0020", direction="positive")
    opposing = _entry("taste-0021", matching_terms=["перекусов"], subject="перекусы")
    config_path, output_dir, state_dir = _run(tmp_path, [EARLY, LATE, UNRELATED], positive, opposing)
    assert "nudged priority from C to B" in _rows_by_title(output_dir)["Family Table"]["Taste profile notes"]
    assert _rows_by_title(output_dir)["Anytime Snacks"]["Taste profile notes"] == ""

    # Enrichment gives one book its first match and gives the other a
    # description that also matches the opposing preference.
    _publish_description(output_dir, state_dir, "Anytime Snacks", "Книга рецептов на каждый день.")
    _publish_description(output_dir, state_dir, "Family Table", "Сборник рецептов и перекусов.")
    refresh_post_enrichment_artifacts(config_path, output_dir, state_dir)
    assert render_review(output_dir, state_dir) == 0

    rows = _rows_by_title(output_dir)
    candidates = _by_title(output_dir / "scouting-candidates.json")
    for title in ("Family Table", "Anytime Snacks", "The Patient Harbour"):
        assert rows[title]["Taste profile notes"] == candidates[title].get("editorial_profile_notes", ""), title
        assert rows[title]["Priority label"] == candidates[title]["priority_label"], title
    assert rows["Anytime Snacks"]["Priority label"] == "B"
    assert "nudged priority from C to B" in rows["Anytime Snacks"]["Taste profile notes"]
    assert rows["Family Table"]["Priority label"] == "C"
    assert "nudged" not in rows["Family Table"]["Taste profile notes"]
    assert "matching preferences conflict" in rows["Family Table"]["Taste profile notes"]
    # The notes stay in the review's data and CSV; the HTML card no longer
    # shows them (operator's decision, 2026-11-16).
    html = (output_dir / "weekly-review.html").read_text(encoding="utf-8")
    assert "taste-0021 (negative) matched but did not change priority" not in html


def test_a_positive_nudge_does_not_reach_a_when_visible_risk_needs_a_text_check() -> None:
    from autoscout.editorial_taste_profile import apply_soft_preferences
    from autoscout.editorial_taste_profile import reapply_soft_preferences

    def candidate(visible_risk_status: str) -> ScoutingCandidate:
        entity = ScoutingCandidate(
            {
                "title": "Family Table",
                "primary_authors": ["Family Example"],
                "short_description": "Сборник семейных рецептов на каждый день.",
                "visible_risk_status": visible_risk_status,
                "source_evidence": [
                    {"source_family": "award", "signal_strength": "shortlist"},
                    {"source_family": "media/curated list", "signal_strength": "major media list"},
                ],
            }
        )
        entity.data["priority_label"] = entity.priority_label()
        return entity

    profile = {"entries": [_entry("taste-0020", direction="positive")]}
    clean, flagged, rescreened = (
        candidate("no visible risk"),
        candidate("text check required"),
        candidate("text check required"),
    )
    assert [entity.data["priority_label"] for entity in (clean, flagged)] == ["B", "B"]

    effects = apply_soft_preferences([clean, flagged], profile)
    # The post-enrichment pass meets a candidate the rescreen returned to B.
    reapply_soft_preferences([rescreened], profile)

    assert clean.data["priority_label"] == "A"
    assert flagged.data["priority_label"] == "B"
    assert rescreened.data["priority_label"] == "B"
    assert flagged.data["editorial_profile_notes"] == (
        "Soft preference taste-0020 (positive) matched but did not change priority (already B): "
        "“Сборник семейных рецептов; [editor note withheld].”"
    )
    assert effects["taste-0020"] == {"match_count": 2, "priority_steps_caused": 1, "notes_count": 2}
