from __future__ import annotations

import copy
from pathlib import Path

import pytest
from state_store_support import read_state
from state_store_support import seed_state
from state_store_support import state_snapshot

from autoscout.candidate_history_migration import migrate_candidate_history
from autoscout.candidate_identity import candidate_id_for
from autoscout.identity import normalize_author_display
from autoscout.identity import normalized_identity_key
from autoscout.pipeline import build_scouting_candidates
from autoscout.pipeline import suppress_candidates_without_material_change
from autoscout.review_rendering import assemble_cumulative_review

SOURCE_URL = "https://example.test/issue-451"


def mention(
    title: str,
    authors: list[str],
    *,
    source_family: str = "media/curated list",
) -> dict[str, object]:
    return {
        "title": title,
        "primary_authors": authors,
        "source_name": "Issue #451 fixture source",
        "source_family": source_family,
        "source_url": SOURCE_URL,
        "evidence_url": SOURCE_URL,
        "source_check_date": "2026-08-20",
        "signal_type": source_family,
        "signal_strength": "winner",
        "evidence_note": "Issue #451 regression fixture.",
    }


def evidence(family: str, author: str) -> dict[str, str]:
    return {
        "source_name": f"Issue #451 {family} source",
        "source_family": family,
        "source_url": SOURCE_URL,
        "evidence_url": SOURCE_URL,
        "signal_strength": "winner",
        "evidence_note": f"Evidence for {author}.",
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Dr. med. Sheila de Liz", "Sheila de Liz"),
        ("Dr med. Sheila de Liz", "Sheila de Liz"),
        ("Dr. med Sheila de Liz", "Sheila de Liz"),
        ("Dr med Sheila de Liz", "Sheila de Liz"),
        ("Dr.med. Sheila de Liz", "Sheila de Liz"),
        ("Dr.med Sheila de Liz", "Sheila de Liz"),
        ("X. Y. Example", "X. Y. Example"),
    ],
)
def test_german_medical_credential_allowlist_is_conservative(raw: str, expected: str) -> None:
    assert normalize_author_display(raw) == expected


def test_german_medical_credential_keeps_raw_author_display() -> None:
    raw_author = "Dr. med. Sheila de Liz"
    candidates, exclusions = build_scouting_candidates(
        [mention("Woman on Fire", [raw_author])],
        "2026-08-20",
    )

    assert exclusions == []
    assert candidates[0].get("primary_authors") == [raw_author]
    assert candidates[0].identity_key() == "woman on fire|sheila de liz"
    assert normalized_identity_key(mention("Woman on Fire", ["Sheila de Liz"])) == normalized_identity_key(
        mention("Woman on Fire", [raw_author])
    )


def test_current_german_credential_resolves_existing_reject_durable_identity() -> None:
    history = [
        {
            "identity_key": "woman on fire|sheila de liz",
            "candidate_id": candidate_id_for("woman on fire|sheila de liz"),
            "title": "Woman on Fire",
            "primary_authors": ["Sheila de Liz"],
            "editor_status": "reject",
            "editor_note": "Already rejected in the editorial review.",
            "independent_signal_families": ["media/curated list"],
            "primary_signal_strength": "winner",
            "priority_label": "C",
            "source_evidence": [evidence("media/curated list", "Sheila de Liz")],
        }
    ]
    aliases = {
        "version": 1,
        "aliases": {"woman on fire|sheila de liz": candidate_id_for("woman on fire|sheila de liz")},
        "candidates": {
            candidate_id_for("woman on fire|sheila de liz"): {
                "candidate_id": candidate_id_for("woman on fire|sheila de liz"),
                "canonical_identity_key": "woman on fire|sheila de liz",
                "title": "Woman on Fire",
                "primary_authors": ["Sheila de Liz"],
            }
        },
    }

    candidates, exclusions = build_scouting_candidates(
        [mention("Woman on Fire", ["Dr. med. Sheila de Liz"])],
        "2026-08-20",
        candidate_history=history,
        candidate_identity_aliases=aliases,
    )
    visible, suppressed = suppress_candidates_without_material_change(candidates, history, aliases)

    assert exclusions == []
    assert visible == []
    assert suppressed == [
        {
            "identity_key": "woman on fire|sheila de liz",
            "title": "Woman on Fire",
            "editor_status": "reject",
            "suppression_reason": "suppressed by editor status without Material Change",
        }
    ]
    assert candidates[0].get("candidate_id") == candidate_id_for("woman on fire|sheila de liz")


def test_migration_dry_run_merges_german_credential_but_blocks_alias_conflict(tmp_path: Path) -> None:
    stable_id = candidate_id_for("woman on fire|sheila de liz")
    legacy_id = candidate_id_for("woman on fire|dr med sheila de liz")
    history = [
        {
            "identity_key": "woman on fire|sheila de liz",
            "candidate_id": stable_id,
            "title": "Woman on Fire",
            "primary_authors": ["Sheila de Liz"],
            "editor_status": "reject",
            "editor_note": "Already rejected in the editorial review.",
            "source_evidence": [evidence("media/curated list", "Sheila de Liz")],
        },
        {
            "identity_key": "woman on fire|dr med sheila de liz",
            "candidate_id": legacy_id,
            "title": "Woman on Fire",
            "primary_authors": ["Dr. med. Sheila de Liz"],
            "source_evidence": [evidence("award", "Dr. med. Sheila de Liz")],
        },
    ]
    aliases = {
        "version": 1,
        "aliases": {
            "woman on fire|sheila de liz": stable_id,
            "woman on fire|dr med sheila de liz": legacy_id,
        },
        "candidates": {
            stable_id: {"candidate_id": stable_id},
            legacy_id: {"candidate_id": legacy_id},
        },
    }
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    seed_state(state_dir, candidate_history=history, candidate_identity_aliases=aliases)
    state_before = state_snapshot(state_dir)

    report, changed = migrate_candidate_history(state_dir)

    assert changed is False
    assert report["projected_history_count"] == 1
    projected = report["projected_history"][0]
    assert projected["editor_status"] == "reject"
    assert projected["editor_note"] == "Already rejected in the editorial review."
    # The State Store keeps Candidate History sorted by identity key, and the merge follows that order.
    stored = read_state(state_dir, "candidate_history")
    assert projected["source_evidence"] == stored[0]["source_evidence"] + stored[1]["source_evidence"]
    assert report["apply_blocked"] is True
    assert report["alias_conflict_count"] > 0
    assert any(conflict["reason"] == "multiple_durable_ids" for conflict in report["alias_conflicts"])
    assert state_snapshot(state_dir) == state_before


@pytest.mark.parametrize(
    ("title", "authors"),
    [
        ("Elon Musk", ["Ashlee Vance", "Walter Isaacson"]),
        ("Black Arms to Hold You Up", ["Thomas J. Sugrue", "Ben Passmore"]),
        ("How to Be Well", ["Kate Bowler", "Amy Larocca"]),
        ("As a Jew", ["Sarah Hurwitz", "Dara Horn"]),
    ],
)
def test_legitimate_same_title_collision_groups_stay_separate(title: str, authors: list[str]) -> None:
    candidates, exclusions = build_scouting_candidates(
        [mention(title, [author]) for author in authors],
        "2026-08-20",
    )

    assert exclusions == []
    assert len(candidates) == len(authors)
    assert {candidate.identity_key() for candidate in candidates} == {
        normalized_identity_key(mention(title, [author])) for author in authors
    }


def test_award_no_work_citation_is_excluded_before_identity_grouping() -> None:
    invalid_mentions = [
        mention("Ensemble de son œuvre", ["First Laureate"], source_family="award"),
        mention("Ensemble de son œuvre", ["Second Laureate"], source_family="award"),
    ]
    candidates, exclusions = build_scouting_candidates(
        [
            *invalid_mentions,
            mention("Ensemble: A Book", ["Specific Author"], source_family="award"),
        ],
        "2026-08-20",
    )

    assert [candidate.get("title") for candidate in candidates] == ["Ensemble: A Book"]
    assert len(exclusions) == 2
    assert {record["primary_authors"][0] for record in exclusions} == {"First Laureate", "Second Laureate"}
    assert {record["exclusion_reason"] for record in exclusions} == {"award citation names no specific work"}


def test_non_award_generic_title_is_not_excluded() -> None:
    candidates, exclusions = build_scouting_candidates(
        [mention("Ensemble de son œuvre", ["Generic Author"], source_family="media/curated list")],
        "2026-08-20",
    )

    assert len(candidates) == 1
    assert exclusions == []


def test_legacy_invalid_award_history_is_filtered_only_from_review_projection() -> None:
    history = [
        {
            "identity_key": "ensemble de son œuvre|first laureate",
            "title": "Ensemble de son œuvre",
            "primary_authors": ["First Laureate"],
            "independent_signal_families": ["award"],
            "source_evidence": [evidence("award", "First Laureate")],
        },
        {
            "identity_key": "ensemble de son œuvre|generic author",
            "title": "Ensemble de son œuvre",
            "primary_authors": ["Generic Author"],
            "independent_signal_families": ["media/curated list"],
            "source_evidence": [evidence("media/curated list", "Generic Author")],
        },
    ]
    before = copy.deepcopy(history)

    rows, _summary = assemble_cumulative_review([], history, [])

    assert [row["identity_key"] for row in rows] == ["ensemble de son œuvre|generic author"]
    assert history == before
